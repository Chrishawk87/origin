#!/usr/bin/env python3
"""Origin — Stage 7 persistence benchmark (measure, then decide).

Stage 7 of the evolve-not-rewrite roadmap is deliberately a DECISION, not a
migration: keep the file-based JSON + derived index if it holds at scale; move
only the *derived index* (never the source-of-truth JSON) into platform_db if,
and only if, multi-tenant query load demands it. The roadmap's exit criterion is
"one deliberate, documented persistence decision — reached with data, not by
default." This harness produces that data.

It stands entirely apart from the running app (like selftest_sie.py and
osha_export.py): it points ORIGIN_DATA_DIR at a throwaway directory, synthesizes
a representative multi-tenant company population at several scales, and times the
persistence + derived-read paths that would feel query-load pressure first:

  * write throughput  — company_profile.upsert() (one JSON per company)
  * portfolio scan    — company_profile.list_all() (owner console hot path: read
                        every company file every page load)
  * requirement query — requirements_engine.requirements_for(cid) (the derived
                        "what applies and why" answer for one company)
  * spine rebuild     — spine.rebuild_spine() (the heaviest derived operation:
                        walks every source collection to rebuild the whole index)
  * on-disk footprint — bytes under companies/ and spine/

Fully offline: every external model key is stripped before import, so an
accidental model call fails loudly instead of silently "passing" on a live key.
Nothing here writes to real data or to the repo — only to a temp dir that is
removed on exit.

Usage:
    python3 bench_persistence.py                # default scales: 100 1000 10000
    python3 bench_persistence.py 100 1000       # custom scales
    python3 bench_persistence.py --json out.json
"""

from __future__ import annotations

import os
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

# ── Isolate persistence BEFORE importing origin. The engines read
# ORIGIN_DATA_DIR at import time, so this must run first. Each scale gets its own
# fresh subdirectory (see _fresh_data_dir) so scales never contaminate each
# other. ────────────────────────────────────────────────────────────────────
_ROOT = tempfile.mkdtemp(prefix="origin-bench-")
os.environ["ORIGIN_DATA_DIR"] = _ROOT

# ── Prove the offline guarantee: strip every external model key. ─────────────
_LLM_KEYS = (
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GROK_API_KEY", "XAI_API_KEY",
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "DEEPSEEK_API_KEY", "MISTRAL_API_KEY",
)
for _k in _LLM_KEYS:
    os.environ.pop(_k, None)

# ── A representative multi-tenant population. Real companies vary by trade,
# jurisdiction, size, hazard mix, and which prequal platform hires them — the
# variety is what makes the requirement/spine derivation do real work rather
# than deriving the same cached answer N times. ──────────────────────────────
_ACTIVITY_KEYS = [
    "confined_space", "forklifts", "cranes_rigging", "hot_work", "respirators",
    "silica", "lead", "asbestos", "excavation", "fall_exposure", "electrical",
    "loto", "noise", "bloodborne", "hazwoper", "psm", "fire_extinguishers",
]
_TRADES = [
    ("Electrical contractor", "238210"), ("Roofing contractor", "238160"),
    ("Excavation & site prep", "238910"), ("Commercial plumbing", "238220"),
    ("Structural steel erection", "238120"), ("Oil & gas well servicing", "213112"),
    ("Industrial painting/coatings", "238320"), ("HVAC mechanical", "238220"),
    ("Concrete & masonry", "238110"), ("General building construction", "236220"),
    ("Fabricated metal manufacturing", "332710"), ("Water/sewer line", "237110"),
]
_STATES = ["TX", "CA", "OK", "LA", "NM", "PA", "OH", "WA", "AZ", "CO", "NC", "GA"]
_OPERATORS = ["ISN", "Avetta", "PEC", "Veriforce", "Browz", None]


def _fresh_data_dir() -> Path:
    """A clean ORIGIN_DATA_DIR for one scale run, so file counts and rebuild
    times reflect exactly this scale and nothing carried over. Re-points the env
    var that the (already imported) engines read on every filesystem call."""
    d = Path(tempfile.mkdtemp(prefix="origin-bench-scale-", dir=_ROOT))
    os.environ["ORIGIN_DATA_DIR"] = str(d)
    return d


def _synth_profile(i: int, rng: random.Random) -> dict:
    trade, naics = rng.choice(_TRADES)
    n_acts = rng.randint(2, 7)
    acts = {k: True for k in rng.sample(_ACTIVITY_KEYS, n_acts)}
    op = rng.choice(_OPERATORS)
    return {
        "company": f"{trade.split()[0]} Co {i:05d}",
        "company_id": f"bench-co-{i:05d}",
        "industry": trade,
        "naics": naics,
        "state": rng.choice(_STATES),
        "headcount": rng.choice([8, 15, 25, 40, 75, 120, 250]),
        "activities": acts,
        "operators": [op] if op else [],
        "customer_platforms": [op] if op else [],
    }


def _dir_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n:.1f} GB"


def run_scale(n: int, sample: int = 200, seed: int = 7) -> dict:
    """Populate `n` companies in a fresh data dir and time each hot path.

    Called in its own subprocess (see main), so the origin engines import once,
    cleanly, against this scale's data dir — no cross-scale module/env staleness.
    """
    data_dir = _fresh_data_dir()
    from origin import company_profile, spine, requirements_engine  # noqa: E402

    rng = random.Random(seed)

    # 1) WRITE — create N company profiles (one JSON each).
    t0 = time.perf_counter()
    cids = []
    for i in range(n):
        rec = company_profile.upsert(_synth_profile(i, rng), by="bench")
        cids.append(rec["company_id"])
    write_s = time.perf_counter() - t0

    companies_dir = data_dir / "companies"

    # 2) PORTFOLIO SCAN — list_all() reads every file. Owner console hot path.
    t0 = time.perf_counter()
    listed = company_profile.list_all()
    list_s = time.perf_counter() - t0
    assert len(listed) == n, f"list_all returned {len(listed)}, expected {n}"

    # 3) REQUIREMENT QUERY — derived "what applies and why" for one company.
    #    Sample K random companies; report mean and p95 per-call latency.
    picks = rng.sample(cids, min(sample, len(cids)))
    per_call = []
    for cid in picks:
        t0 = time.perf_counter()
        r = requirements_engine.requirements_for(cid)
        per_call.append(time.perf_counter() - t0)
        assert r and r["required_count"] >= 0
    req_mean_ms = statistics.mean(per_call) * 1000
    req_p95_ms = (statistics.quantiles(per_call, n=20)[18] * 1000
                  if len(per_call) >= 20 else max(per_call) * 1000)

    # 4) SPINE REBUILD — the heaviest derived op: rebuild the whole index by
    #    walking every source collection (companies + requirements derivation).
    t0 = time.perf_counter()
    meta = spine.rebuild_spine()
    spine_s = time.perf_counter() - t0

    spine_dir = data_dir / "spine"
    comp_bytes = _dir_bytes(companies_dir)
    spine_bytes = _dir_bytes(spine_dir)

    return {
        "companies": n,
        "write_total_s": round(write_s, 3),
        "write_per_co_ms": round(write_s / n * 1000, 3),
        "list_all_ms": round(list_s * 1000, 2),
        "req_mean_ms": round(req_mean_ms, 3),
        "req_p95_ms": round(req_p95_ms, 3),
        "spine_rebuild_s": round(spine_s, 3),
        "spine_nodes": meta.get("nodes"),
        "spine_edges": meta.get("edges"),
        "companies_bytes": comp_bytes,
        "companies_disk": _fmt_bytes(comp_bytes),
        "spine_bytes": spine_bytes,
        "spine_disk": _fmt_bytes(spine_bytes),
    }


def _print_table(rows: list) -> None:
    cols = [
        ("companies", "companies", 9),
        ("write_per_co_ms", "write/co ms", 11),
        ("list_all_ms", "list_all ms", 11),
        ("req_mean_ms", "req mean ms", 11),
        ("req_p95_ms", "req p95 ms", 10),
        ("spine_rebuild_s", "spine s", 9),
        ("spine_nodes", "nodes", 9),
        ("spine_edges", "edges", 9),
        ("companies_disk", "co disk", 10),
        ("spine_disk", "spine disk", 11),
    ]
    header = "  ".join(f"{title:>{w}}" for _, title, w in cols)
    print(header)
    print("-" * len(header))
    for r in rows:
        print("  ".join(f"{str(r[key]):>{w}}" for key, _, w in cols))


def _run_single(n: int) -> int:
    """Worker mode: run ONE scale in this process and print its result as a JSON
    line. Invoked as a subprocess by the orchestrator so each scale imports the
    origin engines exactly once, cleanly, against its own data dir."""
    import json
    t0 = time.perf_counter()
    row = run_scale(n)
    row["_elapsed_s"] = round(time.perf_counter() - t0, 1)
    print("RESULT " + json.dumps(row))
    return 0


def main(argv: list) -> int:
    import json
    import subprocess

    json_out = None
    if "--json" in argv:
        i = argv.index("--json")
        json_out = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]

    # Worker mode — one scale, this process.
    if "--single" in argv:
        i = argv.index("--single")
        return _run_single(int(argv[i + 1]))

    scales = [int(a) for a in argv[1:]] or [100, 1000, 10000]

    if not all(not os.environ.get(k) for k in _LLM_KEYS):
        print("REFUSING TO RUN: an external model key is set; this benchmark "
              "must be offline.", file=sys.stderr)
        return 2

    print(f"Origin persistence benchmark — offline, scales={scales}")
    print("(each scale runs in its own subprocess for clean isolation)\n")
    rows = []
    for n in scales:
        # Fresh subprocess per scale: the origin engines read ORIGIN_DATA_DIR at
        # import time, so a new process is the cleanest way to guarantee each
        # scale starts from an empty, independent data dir.
        proc = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "--single", str(n)],
            capture_output=True, text=True,
        )
        line = next((l for l in proc.stdout.splitlines()
                     if l.startswith("RESULT ")), None)
        if line is None:
            print(f"  scale {n}: FAILED\n{proc.stdout}\n{proc.stderr}",
                  file=sys.stderr)
            return 1
        row = json.loads(line[len("RESULT "):])
        rows.append(row)
        print(f"  scale {n:>6} done in {row['_elapsed_s']}s  "
              f"(spine {row['spine_rebuild_s']}s, "
              f"list_all {row['list_all_ms']}ms, "
              f"req mean {row['req_mean_ms']}ms)")
    print()
    _print_table(rows)

    if json_out:
        Path(json_out).write_text(json.dumps(rows, indent=2), encoding="utf-8")
        print(f"\nwrote {json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
