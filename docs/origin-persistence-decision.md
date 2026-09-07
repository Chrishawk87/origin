# Origin — Stage 7 Persistence Decision

**Status:** Decided — 2026-09-07
**Decision:** Keep the file-based JSON source of truth + derived indexes. Do **not**
migrate to a database yet. Revisit only when a measured trigger below fires.
**Basis:** the benchmark in `bench_persistence.py`, run offline at 100 / 1,000 /
10,000 synthetic tenant companies.

---

## Why this stage exists

Stage 7 of the evolve-not-rewrite roadmap is deliberately a *decision*, not a
migration. The roadmap's rule is: keep file-based JSON if it holds at scale;
migrate only the **derived index** (never the source-of-truth JSON) into
`platform_db` if — and only if — multi-tenant query load demands it. The exit
criterion is "one deliberate, documented persistence decision — reached with
data, not by default." This document is that decision, and the data behind it.

The trust story that file-based buys us is worth protecting: every derived layer
(spine, requirements, training) is rebuildable from the JSON, so if an index is
ever wrong we delete and recompute — there is no schema migration, no forked
source of truth, and the offline "all LLM keys unset → still works" guarantee is
never at risk. We give that up only for a measured reason.

## What was measured

`bench_persistence.py` synthesizes a representative multi-tenant population
(varied trade / NAICS / state / headcount / hazard mix / prequal platform),
writes one JSON file per company, then times the paths that would feel query-load
pressure first. Each scale runs in its own subprocess against a throwaway
`ORIGIN_DATA_DIR`, fully offline. Results:

| companies | write/co (ms) | list_all (ms) | req query mean (ms) | req query p95 (ms) | spine rebuild (s) | spine nodes | spine edges | source JSON on disk | spine index on disk |
|----------:|--------------:|--------------:|--------------------:|-------------------:|------------------:|------------:|------------:|--------------------:|--------------------:|
|       100 |         0.066 |          1.62 |               0.218 |              0.209 |             0.13  |       6,176 |      13,993 |               48 KB |              4.9 MB |
|     1,000 |         0.059 |         16.04 |               0.190 |              0.196 |             2.30  |      61,122 |     141,240 |              488 KB |             49.0 MB |
|    10,000 |         0.056 |        166.87 |               0.193 |              0.196 |            22.65  |     605,755 |   1,401,439 |              4.8 MB |            486.6 MB |

## What the numbers say

The path users actually hit interactively is **the per-company requirement /
evidence query** — "what applies to this company and why," a single company's
view for an owner or a GC. It reads one JSON file and derives the answer, so it
is O(1) in the number of tenants: **~0.19 ms, flat, from 100 to 10,000
companies.** Tenant count does not degrade the query a user waits on. This is the
decisive result.

GC-scoped views (Stage 3) only ever read the companies that GC owns, so a tenant
never pays for the whole portfolio. The only all-tenant read is the **owner's
global `list_all()`** superuser view, which scans every file and therefore scales
linearly: 1.6 ms → 16 ms → 167 ms across the three scales. At 10,000 companies
that is 167 ms — noticeable but still usable, and it is one screen for one
internal user, not a per-tenant request.

The one genuinely expensive operation is the **full spine rebuild** — by design
it walks every source collection and re-derives every node and edge, so it is
O(all data): 0.13 s → 2.3 s → 22.6 s. But the spine is *read* from its
pre-materialized `nodes.jsonl` / `edges.jsonl`; the O(n) cost is only the rebuild,
which runs on demand or on a schedule, never inside a request. A ~23-second
rebuild at 10,000 tenants is fine as a periodic job and only becomes a problem if
it has to run near-continuously.

Storage is a non-issue for the source of truth: **4.8 MB of JSON for 10,000
companies.** The derived spine index is heavier on disk (487 MB at 10k) but it is
disposable and rebuildable, not the thing we are protecting.

## Decision

Keep file-based JSON as the source of truth and keep the spine / requirements /
training layers as derived, rebuildable indexes over it. At the scale Origin will
realistically reach — hundreds to low thousands of tenant companies — every
interactive path stays sub-millisecond to low-tens-of-milliseconds, storage is
trivial, and the rebuild cost stays comfortably inside a periodic job. Migrating
now would trade away the rebuildable-from-JSON trust story for a scaling problem
we do not have.

## Triggers that would flip this decision

Migration is warranted only when one of these is **measured**, not anticipated.
In every case the move is the same bounded one the roadmap prescribes: lift the
**derived index** (spine nodes/edges, and/or a company-summary table) into
`platform_db` behind a flag so it can be updated incrementally instead of fully
rebuilt — while the JSON stays the source of truth and the offline self-test
still passes against the file path.

1. **Spine rebuild can't stay a periodic job.** Full rebuild exceeds ~10 s *and*
   must run more than a few times per hour (e.g. near-real-time graph freshness
   becomes a product requirement). Move the index into a queryable store and
   update it incrementally on write. Rough correspondence in this benchmark:
   ~5,000 companies.

2. **The owner's global portfolio scan gets interactive and slow.** All-tenant
   `list_all()` exceeds ~250 ms at p95 on a screen someone loads repeatedly.
   Materialize a company-summary table (the index), leave the per-company JSON
   as truth. Rough correspondence: ~15,000 companies.

3. **Real write concurrency contention appears.** File locking or atomic-swap
   races show up under genuine concurrent multi-tenant writes. This is the file
   model's real ceiling and is independent of raw counts — if it appears, it
   forces the index (and possibly a write-ahead path) into `platform_db`
   regardless of how few companies exist.

Until one of these fires, file-based stays. Re-run `bench_persistence.py` when the
live tenant count crosses ~1,000 and again near ~5,000 to confirm the curve still
matches this projection, or to catch the first trigger early.

## How to reproduce

```
# from the repo root, offline (no external model keys)
python3 bench_persistence.py 100 1000 10000 --json bench.json
```

The script never touches real data — it points `ORIGIN_DATA_DIR` at a throwaway
directory, refuses to run if any model key is set, and cleans up after itself.
