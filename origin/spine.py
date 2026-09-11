"""Origin — Evidence Spine (Stage 1).

A DERIVED, REBUILDABLE index over the file-based JSON collections that are the
source of truth. The spine never holds a compliance fact of its own: every node
and edge is reconstructed from companies/, audits/, capa/, citations/ by
``rebuild_spine()``. If the index is ever wrong, delete it and rebuild — the
source JSON is untouched. Fully offline; consults no model.

This is the "index, don't fork" rule from origin-data-model.md made real: the
compliance chain (source → requirement → evidence → corrective action →
verification) becomes queryable across engines, without moving the source of
truth into a database and without touching the offline no-LLM guarantee.

Node types today (what the live data already supports):
    company · finding · capa · citation · requirement · review · source (a cited standard)
Edge types today (all directional, all traceable):
    company     --has_finding----->  finding
    company     --has_capa-------->  capa
    company     --has_citation---->  citation
    company     --has_requirement->  requirement
    company     --has_review------>  review
    finding     --triggers------->  capa
    finding     --violates------->  source
    capa        --cited_by------->  source
    citation    --cited_by------->  source
    requirement --cited_by------->  source
    review      --about---------->  finding

The requirement layer is derived, on the fly, from the company profile by the
Applicable Requirements Engine (requirements_engine.py); a requirement and the
evidence that satisfies it meet at the shared source-standard node, which is what
makes "which requirements have no evidence" answerable from this one index.

The review layer (Stage 5) is likewise derived: the human-review queue store
(review_engine.py) holds one item per audit finding that needs a human decision,
and each becomes a review node hung off its company and pointed at the finding it
is about — so "what is waiting on a reviewer, and for which finding" is answerable
from this one index too.

Materialized as nodes.jsonl + edges.jsonl + meta.json under
ORIGIN_DATA_DIR/spine, rewritten atomically on rebuild (temp dir + rename).
Training / Evidence node types are reserved for later stages (training matrix,
evidence vault); they are added here only when the live data supports them, never
speculatively.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:  # match the other engines: prefer the shared path, fall back to env.
    from .paths import DATA_DIR
except Exception:  # pragma: no cover
    DATA_DIR = Path(os.environ.get("ORIGIN_DATA_DIR") or (Path.home() / ".origin"))

SPINE_DIR = DATA_DIR / "spine"
NODES_FILE = SPINE_DIR / "nodes.jsonl"
EDGES_FILE = SPINE_DIR / "edges.jsonl"
META_FILE = SPINE_DIR / "meta.json"

# The relationship vocabulary the spine understands today. Kept explicit so a
# typo can never silently invent an edge type.
REL_HAS_FINDING = "has_finding"
REL_HAS_CAPA = "has_capa"
REL_HAS_CITATION = "has_citation"
REL_HAS_REQUIREMENT = "has_requirement"
REL_HAS_REVIEW = "has_review"
REL_TRIGGERS = "triggers"
REL_VIOLATES = "violates"
REL_CITED_BY = "cited_by"
REL_ABOUT = "about"
# Abatement (attorney feature) relations.
REL_HAS_ITEM = "has_item"          # matter → citation item
REL_ABATED_BY = "abated_by"        # citation item → corrective action (capa)
REL_EVIDENCED_BY = "evidenced_by"  # citation item → evidence


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return s or "unknown"


def _norm_std(citation: str) -> str:
    """Normalize a standard citation string into a stable source-node id, so
    '29 CFR 1926.501' and '29 cfr 1926.501 ' collapse to the same source."""
    return re.sub(r"\s+", " ", (citation or "").strip()).upper()


def _split_std_refs(citation: str) -> List[str]:
    """Split a compound requirement citation into individual standard references
    that match how findings/CAPAs store their source citation, so a requirement
    and its evidence resolve to the SAME source node.

    '29 CFR 1910.1053 / 1926.1153' -> ['29 CFR 1910.1053', '29 CFR 1926.1153']
    '29 CFR 1910.331-335 / NFPA 70E' -> ['29 CFR 1910.331-335', 'NFPA 70E']

    Bare part numbers after a slash ('1926.1153') get the '29 CFR' prefix
    re-attached so they normalize identically to the way a finding cites them.
    """
    out: List[str] = []
    for part in re.split(r"\s*[/;]\s*", citation or ""):
        p = part.strip()
        if not p:
            continue
        if re.match(r"^\d{3,4}[.\-]", p):        # e.g. '1926.1153' or '1926-501'
            p = "29 CFR " + p
        out.append(p)
    return out


# ── node / edge accumulation ─────────────────────────────────────────────────
def _nid(ntype: str, ident: str) -> str:
    return f"{ntype}:{ident}"


def _add_node(nodes: Dict[str, Dict[str, Any]], ntype: str, ident: str,
              label: str = "", **attrs: Any) -> str:
    """Insert-or-enrich a node. Later attributes fill blanks but never clobber a
    value that's already set, so a stub created by an edge is upgraded when the
    real record is read, in any order."""
    key = _nid(ntype, ident)
    node = nodes.get(key)
    if node is None:
        node = {"id": key, "type": ntype, "ident": ident, "label": label or ident}
        nodes[key] = node
    elif label and node.get("label") in ("", node.get("ident")):
        node["label"] = label
    for k, v in attrs.items():
        if v in (None, "", [], {}):
            continue
        node.setdefault(k, v)
    return key


def _add_edge(edges: Dict[Tuple[str, str, str], Dict[str, str]],
              src: str, rel: str, dst: str) -> None:
    edges[(src, rel, dst)] = {"src": src, "rel": rel, "dst": dst}


# ── the rebuild (source JSON → derived index) ────────────────────────────────
def rebuild_spine() -> Dict[str, Any]:
    """Reconstruct every node and edge purely from the source JSON collections
    and atomically replace the on-disk index. Offline, no model. Never mutates a
    source collection. Returns a stats dict."""
    nodes: Dict[str, Dict[str, Any]] = {}
    edges: Dict[Tuple[str, str, str], Dict[str, str]] = {}

    # 1. Companies — the anchor of every chain.
    for c in _companies():
        cid = (c.get("company_id") or _slug(c.get("company", ""))).strip()
        if not cid:
            continue
        _add_node(nodes, "company", cid, label=c.get("company", cid),
                  industry=c.get("industry", ""), naics=c.get("naics", ""),
                  state=c.get("state", ""), headcount=c.get("headcount"))

    # 2. Audits → findings. A finding violates a standard and may trigger a CAPA.
    for a in _audits():
        cid = (a.get("company_id") or "").strip()
        company = a.get("company", "")
        if cid:
            _add_node(nodes, "company", cid, label=company or cid)
        for f in (a.get("findings") or []):
            if not isinstance(f, dict):
                continue
            fid = (f.get("finding_id") or "").strip()
            if not fid:
                continue
            _add_node(nodes, "finding", fid,
                      label=f.get("title") or "Finding",
                      severity=f.get("severity", ""), audit_id=a.get("id", ""))
            if cid:
                _add_edge(edges, _nid("company", cid), REL_HAS_FINDING,
                          _nid("finding", fid))
            std = f.get("standard")
            citation = std.get("citation", "") if isinstance(std, dict) else (
                std if isinstance(std, str) else "")
            if citation:
                sid = _norm_std(citation)
                _add_node(nodes, "source", sid, label=citation,
                          title=std.get("standard_title", "") if isinstance(std, dict) else "",
                          url=std.get("url", "") if isinstance(std, dict) else "")
                _add_edge(edges, _nid("finding", fid), REL_VIOLATES,
                          _nid("source", sid))
            capa_id = (f.get("capa_id") or "").strip()
            if capa_id:
                _add_node(nodes, "capa", capa_id, label="CAPA")
                _add_edge(edges, _nid("finding", fid), REL_TRIGGERS,
                          _nid("capa", capa_id))

    # 3. CAPAs — corrective actions, cited back to their source standard(s).
    for k in _capas():
        kid = (k.get("id") or "").strip()
        if not kid:
            continue
        cid = (k.get("company_id") or "").strip()
        _add_node(nodes, "capa", kid, label=k.get("title") or "CAPA",
                  stage=k.get("stage", ""), severity=k.get("finding_severity", ""),
                  human_review_required=k.get("human_review_required", False))
        if cid:
            _add_node(nodes, "company", cid, label=k.get("company", cid))
            _add_edge(edges, _nid("company", cid), REL_HAS_CAPA, _nid("capa", kid))
        refs = list(k.get("source_refs") or [])
        if k.get("standard"):
            refs.append({"citation": k.get("standard")})
        for ref in refs:
            citation = ref.get("citation", "") if isinstance(ref, dict) else ""
            if not citation:
                continue
            sid = _norm_std(citation)
            _add_node(nodes, "source", sid, label=citation,
                      title=ref.get("title", "") if isinstance(ref, dict) else "",
                      url=ref.get("url", "") if isinstance(ref, dict) else "")
            _add_edge(edges, _nid("capa", kid), REL_CITED_BY, _nid("source", sid))

    # 4. Citations — the 12-part analysis records, cited to their standard.
    for ct in _citations():
        ctid = (ct.get("id") or "").strip()
        if not ctid:
            continue
        facts = ct.get("input") or {}
        company = (facts.get("company") or "").strip()
        standard = (facts.get("standard") or facts.get("section") or "").strip()
        _add_node(nodes, "citation", ctid, label=standard or "Citation",
                  confidence=ct.get("confidence"))
        if company:
            cid = _slug(company)
            _add_node(nodes, "company", cid, label=company)
            _add_edge(edges, _nid("company", cid), REL_HAS_CITATION,
                      _nid("citation", ctid))
        if standard:
            sid = _norm_std(standard)
            _add_node(nodes, "source", sid, label=standard)
            _add_edge(edges, _nid("citation", ctid), REL_CITED_BY,
                      _nid("source", sid))

    # 5. Requirements — the Applicable Requirements Engine derives, from each
    #    company profile, the classified set of standards that apply and WHY.
    #    Each becomes a requirement node cited back to its source standard(s), so
    #    a requirement meets the evidence that satisfies it at the shared source.
    for c in _companies():
        cid = (c.get("company_id") or _slug(c.get("company", ""))).strip()
        if not cid:
            continue
        for req in _requirements_for(c):
            rid = (req.get("requirement_id") or "").strip()
            if not rid:
                continue
            _add_node(nodes, "requirement", rid,
                      label=req.get("title") or "Requirement",
                      classification=req.get("classification", ""),
                      basis=req.get("basis", ""),
                      category=req.get("category", ""),
                      citation=req.get("citation", ""),
                      why=req.get("why", ""))
            _add_edge(edges, _nid("company", cid), REL_HAS_REQUIREMENT,
                      _nid("requirement", rid))
            for ref in _split_std_refs(req.get("citation", "")):
                sid = _norm_std(ref)
                if not sid:
                    continue
                _add_node(nodes, "source", sid, label=ref)
                _add_edge(edges, _nid("requirement", rid), REL_CITED_BY,
                          _nid("source", sid))

    # 6. Review items — the human-review queue (Stage 5) made queryable. Each
    #    pending/decided item hangs off its company and points at the finding it
    #    is about, so the finding node it references is shared with the audit chain.
    for rv in _reviews():
        rid = (rv.get("item_id") or "").strip()
        if not rid:
            continue
        cid = (rv.get("company_id") or "").strip()
        _add_node(nodes, "review", rid, label=rv.get("title") or "Review item",
                  status=rv.get("status", ""), reason=rv.get("reason", ""),
                  severity=rv.get("severity", ""),
                  reviewer=rv.get("reviewer", ""),
                  decision=rv.get("decision", ""))
        if cid:
            _add_node(nodes, "company", cid, label=rv.get("company", cid))
            _add_edge(edges, _nid("company", cid), REL_HAS_REVIEW,
                      _nid("review", rid))
        fid = (rv.get("ref_id") or "").strip()
        if fid and rv.get("source_type") == "audit_finding":
            _add_node(nodes, "finding", fid, label=rv.get("title") or "Finding")
            _add_edge(edges, _nid("review", rid), REL_ABOUT, _nid("finding", fid))

    # 7. Abatement matters — the OSHA-defense feature. Each matter anchors its
    #    citation items; each item points at the corrective actions abating it,
    #    the evidence that documents it, and the regulatory source it cites. This
    #    joins the attorney chain to the rest of the graph at the shared source
    #    and capa nodes, without any matter ever being authoritative.
    for mt in _matters():
        mid = (mt.get("id") or "").strip()
        if not mid:
            continue
        _add_node(nodes, "matter", mid,
                  label=mt.get("client_name") or "Matter",
                  status=mt.get("status", ""),
                  inspection=mt.get("osha_inspection_number", ""),
                  firm_slug=mt.get("firm_slug", ""))
        ev_by_item = _matter_evidence_by_item(mid)
        for it in (mt.get("citation_items") or []):
            iid = (it.get("item_id") or "").strip()
            if not iid:
                continue
            std = (it.get("standard") or "").strip()
            _add_node(nodes, "citation_item", iid,
                      label=std or "Citation item",
                      classification=it.get("classification", ""),
                      verified=bool(it.get("verified")))
            _add_edge(edges, _nid("matter", mid), REL_HAS_ITEM,
                      _nid("citation_item", iid))
            if std:
                sid = _norm_std(std)
                _add_node(nodes, "source", sid, label=std)
                _add_edge(edges, _nid("citation_item", iid), REL_CITED_BY,
                          _nid("source", sid))
            for cid in (it.get("corrective_action_ids") or []):
                cid = (cid or "").strip()
                if not cid:
                    continue
                _add_node(nodes, "capa", cid, label="CAPA")
                _add_edge(edges, _nid("citation_item", iid), REL_ABATED_BY,
                          _nid("capa", cid))
            for ev in ev_by_item.get(iid, []):
                evid = (ev.get("evidence_id") or "").strip()
                if not evid:
                    continue
                _add_node(nodes, "evidence", evid,
                          label=ev.get("original_filename") or "Evidence",
                          verification_status=ev.get("verification_status", ""),
                          role=ev.get("role", ""))
                _add_edge(edges, _nid("citation_item", iid), REL_EVIDENCED_BY,
                          _nid("evidence", evid))

    stats = _write_index(list(nodes.values()), list(edges.values()))
    return stats


# ── atomic materialization ───────────────────────────────────────────────────
def _write_index(node_list: List[Dict[str, Any]],
                 edge_list: List[Dict[str, str]]) -> Dict[str, Any]:
    SPINE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="origin-spine-", dir=str(SPINE_DIR.parent)))
    try:
        (tmp / "nodes.jsonl").write_text(
            "".join(json.dumps(n, ensure_ascii=False) + "\n" for n in node_list),
            encoding="utf-8")
        (tmp / "edges.jsonl").write_text(
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in edge_list),
            encoding="utf-8")
        node_types: Dict[str, int] = {}
        for n in node_list:
            node_types[n["type"]] = node_types.get(n["type"], 0) + 1
        edge_rels: Dict[str, int] = {}
        for e in edge_list:
            edge_rels[e["rel"]] = edge_rels.get(e["rel"], 0) + 1
        meta = {
            "rebuilt_at": _now(),
            "nodes": len(node_list),
            "edges": len(edge_list),
            "node_types": node_types,
            "edge_rels": edge_rels,
            "derived": True,           # never authoritative; rebuildable from JSON
        }
        (tmp / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        # Swap the freshly-built files in. Rename-per-file is atomic on the volume.
        for name in ("nodes.jsonl", "edges.jsonl", "meta.json"):
            os.replace(str(tmp / name), str(SPINE_DIR / name))
        return meta
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── source-collection readers (public engine APIs; each isolated) ────────────
def _companies() -> List[Dict[str, Any]]:
    try:
        from . import company_profile as cp
        return cp.list_all()
    except Exception:
        return []


def _audits() -> List[Dict[str, Any]]:
    try:
        from . import audit_engine as ae
        if hasattr(ae, "_load_all"):
            return ae._load_all()
        return ae.list_recent(limit=1000000)
    except Exception:
        return []


def _capas() -> List[Dict[str, Any]]:
    try:
        from . import capa as ca
        if hasattr(ca, "_load_all"):
            return ca._load_all()
        return ca.list_recent(limit=1000000)
    except Exception:
        return []


def _citations() -> List[Dict[str, Any]]:
    try:
        from . import citation_engine as ce
        return ce.list_recent(limit=1000000)
    except Exception:
        return []


def _requirements_for(company_rec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Derive the classified requirement set for one company profile via the
    Applicable Requirements Engine. Isolated: if that engine is unavailable or
    errors, the spine still rebuilds without requirement nodes."""
    try:
        from . import requirements_engine as re_eng
        return re_eng.requirements_from_profile(company_rec).get("requirements", [])
    except Exception:
        return []


def _reviews() -> List[Dict[str, Any]]:
    """Read the human-review queue store. Isolated: if the review engine is
    unavailable or errors, the spine still rebuilds without review nodes."""
    try:
        from . import review_engine as rv
        if hasattr(rv, "_load_all"):
            return rv._load_all()
    except Exception:
        pass
    return []


def _matters() -> List[Dict[str, Any]]:
    """Read every abatement matter. Isolated: if the abatement engine is
    unavailable or errors, the spine still rebuilds without matter nodes."""
    try:
        from . import abatement_matter as am
        if hasattr(am, "_load_all"):
            return am._load_all()
    except Exception:
        pass
    return []


def _matter_evidence_by_item(matter_id: str) -> Dict[str, List[Dict[str, Any]]]:
    """Group a matter's evidence by citation_item_id. Isolated + best-effort."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    try:
        from . import evidence_vault as ev
        for e in ev.list_evidence(matter_id):
            out.setdefault(e.get("citation_item_id", ""), []).append(e)
    except Exception:
        pass
    return out


# ── read side ────────────────────────────────────────────────────────────────
def _load_lines(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _ensure_built() -> None:
    """Read paths auto-rebuild if the index has never been materialized, so a
    fresh volume answers correctly on first query."""
    if not META_FILE.exists():
        rebuild_spine()


def stats() -> Dict[str, Any]:
    _ensure_built()
    try:
        return json.loads(META_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"nodes": 0, "edges": 0, "node_types": {}, "edge_rels": {}}


def get_graph(limit: int = 2000) -> Dict[str, Any]:
    _ensure_built()
    nodes = _load_lines(NODES_FILE)[:limit]
    edges = _load_lines(EDGES_FILE)[:limit]
    return {"nodes": nodes, "edges": edges, "meta": stats()}


def chain_for_company(company_id: str) -> Dict[str, Any]:
    """The evidence subgraph anchored on one company: the company plus every
    finding / CAPA / citation reachable from it, and the source standards those
    point to. This is the queryable 'show me the whole chain' view."""
    _ensure_built()
    cid = _slug(company_id)
    anchor = _nid("company", cid)
    all_nodes = {n["id"]: n for n in _load_lines(NODES_FILE)}
    all_edges = _load_lines(EDGES_FILE)

    if anchor not in all_nodes:
        return {"company_id": cid, "found": False, "nodes": [], "edges": []}

    # Directed reachability outward from the company (company → finding/capa/
    # citation → source), a bounded walk over a small per-company neighborhood.
    keep = {anchor}
    frontier = [anchor]
    adj: Dict[str, List[Dict[str, str]]] = {}
    for e in all_edges:
        adj.setdefault(e["src"], []).append(e)
    while frontier:
        cur = frontier.pop()
        for e in adj.get(cur, []):
            if e["dst"] not in keep:
                keep.add(e["dst"])
                frontier.append(e["dst"])

    sub_nodes = [all_nodes[nid] for nid in keep if nid in all_nodes]
    sub_edges = [e for e in all_edges if e["src"] in keep and e["dst"] in keep]
    return {
        "company_id": cid,
        "found": True,
        "company": all_nodes[anchor].get("label", cid),
        "counts": {
            "findings": sum(1 for n in sub_nodes if n["type"] == "finding"),
            "capas": sum(1 for n in sub_nodes if n["type"] == "capa"),
            "citations": sum(1 for n in sub_nodes if n["type"] == "citation"),
            "requirements": sum(1 for n in sub_nodes if n["type"] == "requirement"),
            "reviews": sum(1 for n in sub_nodes if n["type"] == "review"),
            "sources": sum(1 for n in sub_nodes if n["type"] == "source"),
        },
        "nodes": sub_nodes,
        "edges": sub_edges,
    }


# ── routes (read-only, gated by _auth under /api/*, isolated + non-fatal) ─────
def register_spine(app) -> None:
    """Attach evidence-spine routes. Mirrors every other SIE module: isolated,
    non-fatal, fully offline. The spine is derived — /rebuild recomputes it from
    the source JSON; it is never a second source of truth."""
    from fastapi import Body
    from fastapi.responses import JSONResponse

    @app.post("/api/spine/rebuild")
    def spine_rebuild(body: dict = Body(default=None)):
        try:
            return {"ok": True, "meta": rebuild_spine()}
        except Exception as exc:  # never 500 the tool
            return JSONResponse({"error": str(exc)}, status_code=200)

    @app.get("/api/spine/stats")
    def spine_stats():
        try:
            return {"ok": True, "meta": stats()}
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=200)

    @app.get("/api/spine/company/{company_id}")
    def spine_company(company_id: str):
        try:
            return {"ok": True, **chain_for_company(company_id)}
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=200)

    @app.get("/api/spine/graph")
    def spine_graph(limit: int = 2000):
        try:
            return {"ok": True, **get_graph(limit=limit)}
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=200)
