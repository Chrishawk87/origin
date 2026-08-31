"""
hazcom.py — Hazard Communication chemical-inventory builder (29 CFR 1910.1200).

The honest version of the "every SDS in the USA" ask. We do NOT — and legally
cannot — warehouse every product's Safety Data Sheet: SDSs are product-specific,
manufacturer-authored, copyrighted, and number in the millions (HazCom/GHS makes
the *manufacturer* the author of record). "MSDS" is the retired pre-2012 term;
GHS replaced it with "SDS" in 2012.

What actually creates compliance value — and what ISN/Avetta/Veriforce/PEC look
for — is three things this module builds:

  1. A real written Hazard Communication program (29 CFR 1910.1200(e)) with the
     company's OWN chemical list embedded, not a generic template.
  2. A chemical INVENTORY the company builds from the products it actually uses,
     each item classified by GHS hazard so the company knows what it's handling.
  3. SDS-collection guidance — where each SDS comes from and how to keep the set
     current and accessible each shift — plus a flag when a product triggers a
     *substance-specific* OSHA standard (silica, lead, benzene, hex-chrome, etc.)
     that requires its own written program beyond HazCom. That trigger is the
     single most-missed thing in a contractor's HazCom file.

`identify_chemical(name)` matches a product/substance name against a curated
reference of the chemicals common to construction, oil & gas, and industrial
trades. `build_inventory(profile)` returns the classified inventory + SDS
checklist + state Right-to-Know overlay. `render_hazcom_program(profile)`
produces the fillable written program with the inventory table in place.

This layer plugs into the same brain as the rest of Origin: the base HazCom
program record lives in corpus.jsonl (id
`29-cfr-1910-1200-hazard-communication-hazcom-ghs`) and is rendered by
compliance_kb.render_program; this module adds the interactive inventory on top.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from . import compliance_kb as kb

HAZCOM_PROGRAM_ID = "29-cfr-1910-1200-hazard-communication-hazcom-ghs"

# ─────────────────────────────────────────────────────────────────────────────
# GHS pictogram vocabulary (the nine 1910.1200 App C pictograms) — plain labels
# the front-end can render as chips.
# ─────────────────────────────────────────────────────────────────────────────
PICTOGRAMS = {
    "flame": "Flammable",
    "health_hazard": "Health hazard (carcinogen / target-organ / sensitizer)",
    "exclamation": "Irritant / harmful",
    "corrosion": "Corrosive",
    "skull": "Acute toxicity (fatal/toxic)",
    "gas_cylinder": "Gas under pressure",
    "flame_over_circle": "Oxidizer",
    "exploding_bomb": "Explosive / self-reactive",
    "environment": "Aquatic / environmental hazard",
}

# ─────────────────────────────────────────────────────────────────────────────
# Substance-specific OSHA standards (29 CFR 1910 Subpart Z / 1926). When a
# product contains one of these, HazCom is NOT enough — the substance's own
# standard applies (exposure assessment, written compliance/exposure-control
# plan, medical surveillance, regulated areas, etc.). Flagging this is the whole
# point: it's the most commonly missed obligation in a contractor's file.
# ─────────────────────────────────────────────────────────────────────────────
SUBSTANCE_STANDARDS = {
    "silica":       {"citation": "29 CFR 1910.1053 / 1926.1153", "program_id": "29-cfr-1926-1153-respirable-crystalline-silica-written-exposure-control-plan-construction", "needs": "Written exposure control plan, air monitoring, medical surveillance."},
    "lead":         {"citation": "29 CFR 1910.1025 / 1926.62",   "program_id": "", "needs": "Exposure assessment, compliance program, medical surveillance, hygiene facilities."},
    "benzene":      {"citation": "29 CFR 1910.1028",             "program_id": "", "needs": "Exposure monitoring, regulated areas, medical surveillance."},
    "hex_chrome":   {"citation": "29 CFR 1910.1026 / 1926.1126", "program_id": "", "needs": "Exposure determination, written compliance program, medical surveillance."},
    "cadmium":      {"citation": "29 CFR 1910.1027 / 1926.1127", "program_id": "", "needs": "Exposure monitoring, compliance program, medical surveillance."},
    "formaldehyde": {"citation": "29 CFR 1910.1048",             "program_id": "", "needs": "Exposure monitoring, regulated areas, medical surveillance."},
    "methylene_chloride": {"citation": "29 CFR 1910.1052",       "program_id": "", "needs": "Exposure monitoring, regulated areas, medical surveillance."},
    "asbestos":     {"citation": "29 CFR 1910.1001 / 1926.1101", "program_id": "", "needs": "Exposure assessment, regulated areas, medical surveillance, licensed abatement."},
    "isocyanates":  {"citation": "29 CFR 1910.1200 + NEP",       "program_id": "", "needs": "Respiratory protection, skin protection, medical surveillance recommended (sensitizer)."},
}

# ─────────────────────────────────────────────────────────────────────────────
# Curated hazard reference for products/substances common to the trades Origin
# serves. This is NOT a substitute for the manufacturer SDS — it is a
# classification aid that tells the user what a product is, what it triggers, and
# what SDS to go collect. Aliases let free-text product names match.
# ─────────────────────────────────────────────────────────────────────────────
def _c(names, cas, signal, pics, hazards, organs, special, ppe, sds_from):
    return {"names": names, "cas": cas, "signal": signal, "pictograms": pics,
            "hazards": hazards, "target_organs": organs, "special": special,
            "ppe": ppe, "sds_from": sds_from}

CHEM_REF: List[Dict[str, Any]] = [
    _c(["acetone"], "67-64-1", "Danger", ["flame", "exclamation"],
       ["Highly flammable liquid and vapor", "Causes serious eye irritation", "May cause drowsiness or dizziness"],
       ["Eyes", "Respiratory system", "CNS"], None,
       "Nitrile gloves, safety glasses, ventilation; keep from ignition sources.", "Product manufacturer / supplier"),
    _c(["toluene", "toluol"], "108-88-3", "Danger", ["flame", "health_hazard", "exclamation"],
       ["Highly flammable", "May damage fertility/unborn child", "Aspiration hazard", "CNS effects"],
       ["CNS", "Reproductive system", "Liver", "Kidneys"], None,
       "Respirator per exposure, nitrile gloves, ventilation.", "Product manufacturer / supplier"),
    _c(["xylene", "xylol"], "1330-20-7", "Danger", ["flame", "exclamation"],
       ["Flammable", "Harmful in contact with skin/inhaled", "Skin/eye irritation"],
       ["CNS", "Eyes", "Skin", "Respiratory system"], None,
       "Nitrile gloves, eye protection, ventilation.", "Product manufacturer / supplier"),
    _c(["mek", "methyl ethyl ketone", "2-butanone"], "78-93-3", "Danger", ["flame", "exclamation"],
       ["Highly flammable", "Serious eye irritation", "May cause drowsiness/dizziness"],
       ["Eyes", "Respiratory system", "CNS"], None,
       "Nitrile gloves, splash goggles, ventilation.", "Product manufacturer / supplier"),
    _c(["methanol", "methyl alcohol", "wood alcohol"], "67-56-1", "Danger", ["flame", "skull", "health_hazard"],
       ["Highly flammable", "Toxic if swallowed/inhaled/skin contact", "Damages organs (eyes/CNS)"],
       ["Eyes", "CNS", "Optic nerve"], None,
       "Chemical gloves, goggles, respirator if airborne; toxic — no skin contact.", "Product manufacturer / supplier"),
    _c(["isopropyl alcohol", "ipa", "isopropanol", "rubbing alcohol"], "67-63-0", "Danger", ["flame", "exclamation"],
       ["Highly flammable", "Serious eye irritation", "May cause drowsiness/dizziness"],
       ["Eyes", "Respiratory system"], None,
       "Gloves, eye protection, keep from ignition.", "Product manufacturer / supplier"),
    _c(["gasoline", "gas", "petrol", "unleaded"], "86290-81-5", "Danger", ["flame", "health_hazard", "exclamation", "environment"],
       ["Extremely flammable", "May be fatal if swallowed and enters airways", "Suspected carcinogen (benzene content)", "Toxic to aquatic life"],
       ["CNS", "Blood", "Skin", "Respiratory system"], "benzene",
       "No skin contact, ventilation, bonding/grounding when transferring; contains benzene.", "Product manufacturer / supplier"),
    _c(["diesel", "diesel fuel", "off-road diesel", "#2 diesel"], "68476-34-6", "Danger", ["flame", "health_hazard", "exclamation", "environment"],
       ["Flammable", "Aspiration hazard — fatal if swallowed/enters airways", "Suspected carcinogen", "Skin irritation"],
       ["Skin", "Respiratory system", "Lungs"], None,
       "Nitrile gloves, avoid prolonged skin contact; diesel exhaust is a listed carcinogen.", "Product manufacturer / supplier"),
    _c(["crude oil", "crude"], "8002-05-9", "Danger", ["flame", "health_hazard", "exclamation", "environment"],
       ["Flammable", "Suspected carcinogen", "May contain H2S", "Aspiration hazard"],
       ["Skin", "Blood", "Respiratory system"], "benzene",
       "H2S monitoring, FR clothing, no skin contact; treat as benzene-containing.", "Operator / product manufacturer"),
    _c(["benzene"], "71-43-2", "Danger", ["flame", "health_hazard", "skull"],
       ["Highly flammable", "Causes cancer (leukemia)", "May cause genetic defects", "Toxic to blood-forming organs"],
       ["Blood", "Bone marrow", "CNS"], "benzene",
       "SUBSTANCE-SPECIFIC STANDARD — exposure monitoring + medical surveillance required.", "Product manufacturer / supplier"),
    _c(["hydrogen sulfide", "h2s", "sour gas"], "7783-06-4", "Danger", ["flame", "skull", "gas_cylinder"],
       ["Extremely flammable gas", "Fatal if inhaled", "May displace oxygen"],
       ["Respiratory system", "CNS"], None,
       "Continuous gas monitoring, SCBA for rescue, H2S-specific training; deadly at low ppm.", "Operator / gas supplier"),
    _c(["sulfuric acid", "battery acid"], "7664-93-9", "Danger", ["corrosion"],
       ["Causes severe skin burns and eye damage", "Corrosive to metals"],
       ["Skin", "Eyes", "Respiratory system"], None,
       "Acid-resistant gloves/apron, face shield, eyewash station nearby.", "Product manufacturer / supplier"),
    _c(["hydrochloric acid", "muriatic acid", "hcl"], "7647-01-0", "Danger", ["corrosion", "exclamation"],
       ["Causes severe skin burns and eye damage", "May cause respiratory irritation"],
       ["Skin", "Eyes", "Respiratory system"], None,
       "Acid-resistant gloves, goggles/face shield, ventilation, eyewash nearby.", "Product manufacturer / supplier"),
    _c(["sodium hydroxide", "caustic soda", "lye"], "1310-73-2", "Danger", ["corrosion"],
       ["Causes severe skin burns and eye damage", "Corrosive to metals"],
       ["Skin", "Eyes"], None,
       "Chemical gloves, face shield, apron; eyewash/shower nearby.", "Product manufacturer / supplier"),
    _c(["portland cement", "cement", "concrete mix", "mortar", "grout"], "65997-15-1", "Danger", ["corrosion", "exclamation", "health_hazard"],
       ["Causes skin/eye burns (wet cement is caustic)", "May cause respiratory irritation", "Contains crystalline silica"],
       ["Skin", "Eyes", "Lungs"], "silica",
       "Waterproof gloves/boots, eye protection; dry cutting/grinding = silica exposure.", "Product manufacturer / supplier"),
    _c(["silica", "crystalline silica", "quartz", "silica sand", "sand"], "14808-60-7", "Danger", ["health_hazard"],
       ["Causes cancer (lung) by inhalation", "Causes damage to lungs (silicosis) by prolonged inhalation"],
       ["Lungs", "Respiratory system"], "silica",
       "SUBSTANCE-SPECIFIC STANDARD — written exposure control plan + air monitoring required.", "Product manufacturer / supplier"),
    _c(["lead", "lead paint", "leaded"], "7439-92-1", "Danger", ["health_hazard", "environment"],
       ["May damage fertility/unborn child", "Causes damage to organs (blood/nervous system)", "Toxic to aquatic life"],
       ["Blood", "CNS", "Kidneys", "Reproductive system"], "lead",
       "SUBSTANCE-SPECIFIC STANDARD — exposure assessment + medical surveillance required.", "Product manufacturer / supplier"),
    _c(["welding fume", "welding fumes", "weld fume"], None, "Danger", ["health_hazard", "exclamation"],
       ["May contain manganese, hex-chrome, nickel", "Respiratory/lung effects", "Possible carcinogen (hex-chrome on stainless)"],
       ["Lungs", "CNS", "Respiratory system"], "hex_chrome",
       "Local exhaust ventilation, appropriate respirator; stainless welding = hex-chrome standard.", "Consumable/electrode manufacturer"),
    _c(["carbon monoxide", "co"], "630-08-0", "Danger", ["flame", "skull", "gas_cylinder"],
       ["Extremely flammable gas", "Fatal if inhaled", "May damage unborn child"],
       ["Blood", "CNS", "Cardiovascular"], None,
       "CO monitoring, ventilation of engine exhaust in enclosed spaces.", "Gas supplier / generated on-site"),
    _c(["propane", "lpg", "liquefied petroleum gas"], "74-98-6", "Danger", ["flame", "gas_cylinder"],
       ["Extremely flammable gas", "May cause frostbite", "Gas under pressure"],
       ["Respiratory system (asphyxiant)"], None,
       "No ignition sources, leak detection, cylinder handling per storage rules.", "Gas supplier"),
    _c(["acetylene"], "74-86-2", "Danger", ["flame", "gas_cylinder"],
       ["Extremely flammable gas", "May react explosively", "Gas under pressure"],
       ["Respiratory system (asphyxiant)"], None,
       "Flashback arrestors, upright storage, no copper fittings; hot-work permit.", "Gas supplier"),
    _c(["oxygen", "o2", "compressed oxygen"], "7782-44-7", "Danger", ["flame_over_circle", "gas_cylinder"],
       ["May cause/intensify fire (oxidizer)", "Gas under pressure"],
       ["N/A (oxidizer)"], None,
       "Keep oil/grease away, separate storage from fuel gases 20 ft or barrier.", "Gas supplier"),
    _c(["nitrogen", "n2"], "7727-37-9", "Warning", ["gas_cylinder"],
       ["Gas under pressure", "May displace oxygen and cause asphyxiation"],
       ["Respiratory system (asphyxiant)"], None,
       "Oxygen monitoring in confined/enclosed spaces.", "Gas supplier"),
    _c(["argon", "ar"], "7440-37-1", "Warning", ["gas_cylinder"],
       ["Gas under pressure", "May displace oxygen (asphyxiant)"],
       ["Respiratory system (asphyxiant)"], None,
       "Ventilation, O2 monitoring in confined spaces.", "Gas supplier"),
    _c(["ammonia", "anhydrous ammonia", "nh3"], "7664-41-7", "Danger", ["skull", "corrosion", "gas_cylinder", "environment"],
       ["Toxic if inhaled", "Causes severe skin burns and eye damage", "Gas under pressure"],
       ["Respiratory system", "Eyes", "Skin"], None,
       "Full-face respirator/SCBA, chemical suit, ammonia monitoring.", "Product manufacturer / supplier"),
    _c(["chlorine", "cl2"], "7782-50-5", "Danger", ["skull", "corrosion", "flame_over_circle", "gas_cylinder", "environment"],
       ["Fatal if inhaled", "Causes severe burns", "Oxidizer", "Very toxic to aquatic life"],
       ["Respiratory system", "Eyes", "Skin"], None,
       "Gas monitoring, SCBA for response, corrosion-resistant PPE.", "Product manufacturer / supplier"),
    _c(["ethylene glycol", "antifreeze", "coolant"], "107-21-1", "Warning", ["exclamation", "health_hazard"],
       ["Harmful if swallowed", "May cause organ damage (kidneys) through prolonged exposure"],
       ["Kidneys", "CNS"], None,
       "Nitrile gloves, eye protection; keep from children/animals (sweet taste, toxic).", "Product manufacturer / supplier"),
    _c(["brake cleaner", "brake clean"], None, "Danger", ["flame", "exclamation", "health_hazard"],
       ["Flammable (or non-flammable chlorinated variant)", "May cause drowsiness/dizziness", "Some contain tetrachloroethylene"],
       ["CNS", "Respiratory system", "Liver"], None,
       "Ventilation, gloves; check whether product is chlorinated (different hazards).", "Product manufacturer / supplier"),
    _c(["carburetor cleaner", "carb cleaner"], None, "Danger", ["flame", "exclamation"],
       ["Highly flammable", "Eye/skin/respiratory irritation", "CNS effects"],
       ["CNS", "Eyes", "Respiratory system"], None,
       "Gloves, eye protection, ventilation, keep from ignition.", "Product manufacturer / supplier"),
    _c(["mineral spirits", "paint thinner", "stoddard solvent", "white spirit"], "64475-85-0", "Warning", ["flame", "health_hazard", "exclamation"],
       ["Flammable", "Aspiration hazard", "Skin/eye irritation", "May cause drowsiness"],
       ["CNS", "Skin", "Respiratory system"], None,
       "Nitrile gloves, ventilation, keep from ignition.", "Product manufacturer / supplier"),
    _c(["naphtha", "vm&p naphtha"], "64742-89-8", "Danger", ["flame", "health_hazard", "exclamation"],
       ["Highly flammable", "Aspiration hazard", "May cause drowsiness/dizziness"],
       ["CNS", "Respiratory system"], None,
       "Ventilation, gloves, bonding/grounding, no ignition.", "Product manufacturer / supplier"),
    _c(["hexane", "n-hexane"], "110-54-3", "Danger", ["flame", "health_hazard", "exclamation", "environment"],
       ["Highly flammable", "May damage nervous system (prolonged)", "Aspiration hazard"],
       ["CNS", "Peripheral nerves"], None,
       "Ventilation, gloves, respirator per exposure.", "Product manufacturer / supplier"),
    _c(["sodium hypochlorite", "bleach", "chlorine bleach"], "7681-52-9", "Warning", ["corrosion", "exclamation", "environment"],
       ["Causes skin/eye irritation or burns", "Releases toxic gas if mixed with acids/ammonia", "Harmful to aquatic life"],
       ["Skin", "Eyes", "Respiratory system"], None,
       "Gloves, eye protection; NEVER mix with acids or ammonia.", "Product manufacturer / supplier"),
    _c(["degreaser", "industrial degreaser", "purple power", "simple green"], None, "Warning", ["corrosion", "exclamation"],
       ["May cause skin/eye irritation or burns (alkaline)", "Varies widely by product"],
       ["Skin", "Eyes"], None,
       "Gloves, eye protection; verify pH/ingredients on the specific product SDS.", "Product manufacturer / supplier"),
    _c(["epoxy", "epoxy resin", "epoxy coating"], None, "Warning", ["exclamation", "health_hazard", "environment"],
       ["Skin/eye irritation", "May cause allergic skin reaction (sensitizer)", "Harmful to aquatic life"],
       ["Skin", "Respiratory system"], None,
       "Nitrile gloves, eye protection, ventilation; sensitizer — avoid repeated skin contact.", "Product manufacturer / supplier"),
    _c(["polyurethane", "spray foam", "sprayfoam", "isocyanate", "mdi", "tdi", "spray polyurethane foam", "spf"], None, "Danger", ["exclamation", "health_hazard"],
       ["May cause allergy/asthma symptoms if inhaled (respiratory sensitizer)", "Skin/eye/respiratory irritation"],
       ["Respiratory system", "Skin", "Lungs"], "isocyanates",
       "Supplied-air/respirator during spray, skin protection; isocyanate sensitizer.", "Product manufacturer / supplier"),
    _c(["spray paint", "aerosol paint", "coating", "primer", "enamel"], None, "Danger", ["flame", "exclamation", "health_hazard"],
       ["Flammable aerosol", "May cause drowsiness/dizziness", "May contain isocyanates/hex-chrome (check SDS)"],
       ["CNS", "Respiratory system", "Skin"], None,
       "Ventilation, respirator for spraying, no ignition; verify pigments on SDS.", "Product manufacturer / supplier"),
    _c(["wd-40", "penetrating oil", "pb blaster", "liquid wrench"], None, "Warning", ["flame", "health_hazard", "exclamation"],
       ["Flammable aerosol", "Aspiration hazard", "May cause drowsiness"],
       ["CNS", "Respiratory system"], None,
       "Ventilation, keep from ignition, avoid prolonged skin contact.", "Product manufacturer / supplier"),
    _c(["loctite", "threadlocker", "anaerobic adhesive", "super glue", "cyanoacrylate"], None, "Warning", ["exclamation"],
       ["Skin/eye irritation", "Bonds skin instantly (cyanoacrylate)", "May cause allergic reaction"],
       ["Skin", "Eyes", "Respiratory system"], None,
       "Gloves, eye protection, ventilation.", "Product manufacturer / supplier"),
    _c(["propylene glycol"], "57-55-6", "Warning", ["exclamation"],
       ["Low hazard — mild eye/skin irritation possible"],
       ["Eyes", "Skin"], None,
       "Basic gloves/eye protection; generally low hazard.", "Product manufacturer / supplier"),
    _c(["formaldehyde", "formalin"], "50-00-0", "Danger", ["flame", "health_hazard", "skull", "corrosion"],
       ["Causes cancer", "Toxic if inhaled/swallowed/skin contact", "Causes severe burns", "Skin sensitizer"],
       ["Respiratory system", "Skin", "Eyes"], "formaldehyde",
       "SUBSTANCE-SPECIFIC STANDARD — exposure monitoring + medical surveillance.", "Product manufacturer / supplier"),
    _c(["methylene chloride", "dichloromethane", "dcm", "paint stripper"], "75-09-2", "Danger", ["health_hazard", "exclamation"],
       ["Suspected carcinogen", "May cause drowsiness/dizziness", "Forms carbon monoxide in the body"],
       ["CNS", "Cardiovascular", "Liver"], "methylene_chloride",
       "SUBSTANCE-SPECIFIC STANDARD — exposure monitoring + regulated areas.", "Product manufacturer / supplier"),
]

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())).strip()


# Fast alias index, keyed on the NORMALIZED alias so "WD-40" and "wd 40" agree.
_ALIAS_INDEX: Dict[str, Dict[str, Any]] = {}
for _rec in CHEM_REF:
    for _n in _rec["names"]:
        _ALIAS_INDEX[_norm(_n)] = _rec


def identify_chemical(name: str) -> Dict[str, Any]:
    """Match a free-text product/substance name to the hazard reference.

    Conservative on purpose: a WRONG classification is worse than none in a
    compliance tool, so we only match on an exact normalized alias or on a
    whole-phrase alias appearing in the query (with a length guard so short
    aliases like "co"/"gas"/"ar" match exactly and never bleed into words like
    "coating" or "natural gas"). Anything else returns an honest
    "read-the-SDS" stub instead of guessing.
    """
    q = _norm(name)
    if not q:
        return {"matched": False, "query": name, "note": "Enter a product or chemical name."}

    # 1) Exact normalized alias.
    if q in _ALIAS_INDEX:
        return _profile(name, _ALIAS_INDEX[q], "exact")

    # 2) Whole-phrase alias inside the query, word-boundary matched. Multiword
    #    aliases always qualify; single-word aliases must be >= 4 chars so tiny
    #    tokens don't false-match.
    qpadded = f" {q} "
    best = None
    best_len = 0
    for alias, rec in _ALIAS_INDEX.items():
        multiword = " " in alias
        if not multiword and len(alias) < 4:
            continue
        if f" {alias} " in qpadded and len(alias) > best_len:
            best, best_len = rec, len(alias)
    if best:
        return _profile(name, best, "phrase")

    return {
        "matched": False,
        "query": name,
        "signal": "Unknown — read the SDS",
        "pictograms": [],
        "hazards": ["Not in the quick-reference. Classify this product from its manufacturer Safety Data Sheet (Sections 2 and 9)."],
        "special": None,
        "sds_from": "Product manufacturer / supplier",
        "note": "Origin doesn't guess hazards. Pull the supplier SDS and record the GHS signal word, pictograms, and hazard statements from Section 2.",
    }


def _profile(query: str, rec: Dict[str, Any], match: str) -> Dict[str, Any]:
    special = rec.get("special")
    special_info = SUBSTANCE_STANDARDS.get(special) if special else None
    return {
        "matched": True,
        "match_type": match,
        "query": query,
        "canonical": rec["names"][0],
        "cas": rec.get("cas"),
        "signal": rec.get("signal"),
        "pictograms": [{"key": p, "label": PICTOGRAMS.get(p, p)} for p in rec.get("pictograms", [])],
        "hazards": rec.get("hazards", []),
        "target_organs": rec.get("target_organs", []),
        "ppe": rec.get("ppe"),
        "sds_from": rec.get("sds_from"),
        "special": special,
        "special_standard": special_info,
    }


# ─────────────────────────────────────────────────────────────────────────────
# State Right-to-Know / HazCom overlays. State-plan states can add obligations
# on top of federal HazCom. This surfaces the ones that carry a distinct written
# requirement so the program isn't federal-only.
# ─────────────────────────────────────────────────────────────────────────────
STATE_RTK = {
    "MN": {"name": "Minnesota Employee Right-to-Know (ERTK)",
           "citation": "Minn. Rules ch. 5206 / Minn. Stat. 182.65-.675",
           "adds": "Annual training on hazardous substances, harmful physical agents, and infectious agents; ERTK covers more than federal HazCom.",
           "program_id": "mn-5206-ertk"},
    "NJ": {"name": "New Jersey Worker & Community Right-to-Know Act",
           "citation": "N.J.S.A. 34:5A / N.J.A.C. 8:59",
           "adds": "Public-sector employers must label containers with NJ RTK survey names and maintain a Right-to-Know Survey/Hazardous Substance Fact Sheets.",
           "program_id": ""},
    "PA": {"name": "Pennsylvania Worker & Community Right-to-Know Act",
           "citation": "35 P.S. 7301-7320",
           "adds": "Public employers must post/keep Hazardous Substance Survey Forms and provide the state RTK poster.",
           "program_id": ""},
    "CA": {"name": "California HazCom (Cal/OSHA) + Prop 65",
           "citation": "8 CCR 5194 / Prop 65 (H&S 25249.5)",
           "adds": "Cal/OSHA HazCom mirrors GHS; Prop 65 adds warning obligations for listed chemicals (separate from HazCom).",
           "program_id": ""},
    "WA": {"name": "Washington HazCom (WISHA)",
           "citation": "WAC 296-901",
           "adds": "State-plan HazCom rule; align program with WAC 296-901 GHS provisions.",
           "program_id": ""},
    "OR": {"name": "Oregon HazCom (Oregon OSHA)",
           "citation": "OAR 437 Div 2/Z (1910.1200 adopted)",
           "adds": "State-plan adoption of federal HazCom; verify Oregon OSHA amendments.",
           "program_id": ""},
}


def state_overlay(state: Optional[str]) -> Optional[Dict[str, Any]]:
    if not state:
        return None
    return STATE_RTK.get(state.strip().upper())


# ─────────────────────────────────────────────────────────────────────────────
# Intake schema for the front-end.
# ─────────────────────────────────────────────────────────────────────────────
def intake_schema() -> Dict[str, Any]:
    return {
        "fields": [
            {"id": "company", "type": "text", "q": "Company name"},
            {"id": "administrator", "type": "text", "q": "Program administrator (name & title)",
             "help": "The person responsible for the written HazCom program."},
            {"id": "state", "type": "text", "q": "State (optional, 2-letter)",
             "help": "Pulls in state Right-to-Know obligations where they exist (MN, NJ, PA, CA, WA, OR)."},
            {"id": "sds_location", "type": "text", "q": "Where are SDSs kept / accessed?",
             "help": "e.g. binder in the shop + digital library on the shared drive; must be reachable every shift."},
        ],
        "chemical_fields": [
            {"id": "product", "type": "text", "q": "Product / chemical name"},
            {"id": "manufacturer", "type": "text", "q": "Manufacturer / supplier"},
            {"id": "location", "type": "text", "q": "Where used / stored"},
            {"id": "quantity", "type": "text", "q": "Typical quantity on hand"},
        ],
        "reference_count": len(CHEM_REF),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Inventory build.
# ─────────────────────────────────────────────────────────────────────────────
def build_inventory(profile: Dict[str, Any]) -> Dict[str, Any]:
    """Take a company profile + a list of products and return a classified
    chemical inventory, the SDS-collection checklist, substance-specific-standard
    triggers, and the state Right-to-Know overlay."""
    company = (profile.get("company") or "").strip()
    state = (profile.get("state") or "").strip().upper() or None
    chems = profile.get("chemicals") or []
    if isinstance(chems, str):
        chems = [c.strip() for c in re.split(r"[\n,]", chems) if c.strip()]

    items: List[Dict[str, Any]] = []
    triggers: Dict[str, Dict[str, Any]] = {}
    unmatched: List[str] = []

    for c in chems:
        if isinstance(c, str):
            name, manuf, loc, qty = c, "", "", ""
        else:
            name = (c.get("product") or c.get("name") or "").strip()
            manuf = (c.get("manufacturer") or "").strip()
            loc = (c.get("location") or "").strip()
            qty = (c.get("quantity") or "").strip()
        if not name:
            continue
        prof = identify_chemical(name)
        item = {
            "product": name, "manufacturer": manuf, "location": loc, "quantity": qty,
            "matched": prof.get("matched", False),
            "canonical": prof.get("canonical"),
            "cas": prof.get("cas"),
            "signal": prof.get("signal"),
            "pictograms": prof.get("pictograms", []),
            "hazards": prof.get("hazards", []),
            "target_organs": prof.get("target_organs", []),
            "ppe": prof.get("ppe"),
            "sds_from": prof.get("sds_from", "Product manufacturer / supplier"),
            "sds_on_file": bool(c.get("sds_on_file")) if isinstance(c, dict) else False,
            "special": prof.get("special"),
            "special_standard": prof.get("special_standard"),
        }
        items.append(item)
        if not prof.get("matched"):
            unmatched.append(name)
        sp = prof.get("special")
        if sp and sp not in triggers:
            info = dict(SUBSTANCE_STANDARDS.get(sp, {}))
            info["substance"] = sp
            info["from_product"] = name
            triggers[sp] = info

    overlay = state_overlay(state)

    # SDS-collection checklist — the actionable to-do the contractor works.
    missing_sds = [i["product"] for i in items if not i.get("sds_on_file")]

    return {
        "company": company,
        "state": state,
        "administrator": profile.get("administrator", ""),
        "sds_location": profile.get("sds_location", ""),
        "item_count": len(items),
        "items": items,
        "unmatched": unmatched,
        "unmatched_count": len(unmatched),
        "substance_triggers": list(triggers.values()),
        "trigger_count": len(triggers),
        "state_overlay": overlay,
        "sds_checklist": {
            "total": len(items),
            "on_file": len(items) - len(missing_sds),
            "missing": missing_sds,
        },
        "hazcom_program_id": HAZCOM_PROGRAM_ID,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Render the written HazCom program with the company's inventory embedded.
# ─────────────────────────────────────────────────────────────────────────────
def render_hazcom_program(profile: Dict[str, Any], sector: str = "") -> str:
    """Build a fillable written Hazard Communication program (1910.1200(e))
    with the company's actual chemical inventory table in place. Falls back to
    the base KB program text for the boilerplate sections."""
    inv = build_inventory(profile)
    company = inv["company"] or "{{COMPANY_NAME}}"
    admin = inv["administrator"] or "{{PROGRAM_ADMINISTRATOR}}"
    sds_loc = inv["sds_location"] or "{{SDS_LOCATION}}"

    lines: List[str] = []
    lines.append(f"# Written Hazard Communication Program — {company}")
    lines.append("**Governing standard:** 29 CFR 1910.1200 (construction cross-reference 29 CFR 1926.59)  ")
    lines.append(f"**Program administrator:** {admin}  ")
    lines.append(f"**SDS location / access:** {sds_loc}  ")
    lines.append("**Effective date:** {{EFFECTIVE_DATE}}")
    lines.append("")
    lines.append("## 1. Purpose and Scope")
    lines.append("This program ensures that the hazards of all chemicals produced or used at "
                 f"{company} are evaluated, and that hazard information is transmitted to all "
                 "affected employees. It applies to every workplace where employees may be "
                 "exposed to hazardous chemicals under normal conditions of use or in a foreseeable emergency.")
    lines.append("")
    lines.append("## 2. Program Administrator")
    lines.append(f"{admin} is responsible for maintaining this written program, the chemical "
                 "inventory, and the Safety Data Sheet collection; for ensuring container "
                 "labeling; and for employee training.")
    lines.append("")
    lines.append("## 3. Chemical Inventory")
    lines.append("The following hazardous chemicals are known to be present. The inventory is "
                 "reviewed and updated whenever a new chemical is introduced or one is removed.")
    lines.append("")
    if inv["items"]:
        lines.append("| # | Product | Manufacturer | Where used/stored | GHS signal | Primary hazards | SDS on file |")
        lines.append("|---|---------|--------------|-------------------|-----------|-----------------|-------------|")
        for i, it in enumerate(inv["items"], 1):
            haz = "; ".join(it.get("hazards", [])[:2]) or "See SDS Section 2"
            sig = it.get("signal") or "See SDS"
            sds = "Yes" if it.get("sds_on_file") else "NEEDED"
            lines.append(f"| {i} | {it['product']} | {it.get('manufacturer') or '—'} | "
                         f"{it.get('location') or '—'} | {sig} | {haz} | {sds} |")
    else:
        lines.append("_List every hazardous chemical by the identity used on its SDS. "
                     "Add products in the inventory builder to populate this table._")
    lines.append("")
    if inv["substance_triggers"]:
        lines.append("## 4. Substance-Specific Standards Triggered")
        lines.append("One or more products above are regulated by their own OSHA standard in "
                     "addition to HazCom. HazCom alone does **not** satisfy these — each requires "
                     "its own written program, exposure assessment, and (often) medical surveillance:")
        lines.append("")
        for t in inv["substance_triggers"]:
            lines.append(f"- **{t.get('substance', '').replace('_', ' ').title()}** "
                         f"({t.get('from_product', '')}) — {t.get('citation', '')}: {t.get('needs', '')}")
        lines.append("")
        sec = 5
    else:
        sec = 4
    lines.append(f"## {sec}. Container Labeling")
    lines.append("All incoming containers must retain the manufacturer's GHS label (product "
                 "identifier, signal word, pictograms, hazard and precautionary statements, "
                 "supplier information). Workplace/secondary containers are labeled with the "
                 "product identifier and hazard information (words, pictures, or symbols that "
                 "provide general hazard information). The program administrator verifies labeling "
                 "on receipt and periodically thereafter.")
    lines.append("")
    lines.append(f"## {sec + 1}. Safety Data Sheets")
    lines.append(f"An SDS is maintained for every hazardous chemical in the inventory, kept at "
                 f"{sds_loc} and accessible to every employee on every shift without barrier. "
                 "When a new chemical arrives, its SDS is obtained from the manufacturer/supplier "
                 "before or at the time of first shipment and added to the collection. Missing or "
                 "outdated SDSs are requested from the supplier immediately.")
    lines.append("")
    lines.append(f"## {sec + 2}. Employee Information and Training")
    lines.append("Employees are trained at initial assignment and whenever a new chemical hazard "
                 "is introduced. Training covers: the requirements of 1910.1200; operations where "
                 "hazardous chemicals are present; the location of this written program, the "
                 "inventory, and the SDSs; methods to detect a release; the physical and health "
                 "hazards of the chemicals; protective measures (work practices, PPE, emergency "
                 "procedures); and how to read labels and SDSs.")
    lines.append("")
    lines.append(f"## {sec + 3}. Non-Routine Tasks and Unlabeled Pipes")
    lines.append("Before non-routine tasks (e.g., tank/vessel entry, line breaking), the program "
                 "administrator informs affected employees of the chemical hazards and required "
                 "protective measures. Hazards of chemicals in unlabeled pipes are communicated "
                 "prior to work in the area.")
    lines.append("")
    lines.append(f"## {sec + 4}. Multi-Employer Worksites")
    lines.append("On shared worksites, this company provides other on-site employers with access "
                 "to its SDSs, informs them of precautionary measures, and identifies its labeling "
                 "system. Host/controlling-employer SDSs are obtained for chemicals our employees "
                 "may be exposed to.")
    lines.append("")
    if inv.get("state_overlay"):
        ov = inv["state_overlay"]
        lines.append(f"## {sec + 5}. State Right-to-Know — {ov['name']}")
        lines.append(f"**{ov['citation']}.** {ov['adds']} This program is maintained to satisfy "
                     "the state requirement in addition to federal HazCom.")
        lines.append("")
    lines.append("---")
    lines.append("_Generated by Origin. This is documentation support, not legal advice. "
                 "Verify chemical classifications against each product's current manufacturer SDS._")
    return "\n".join(lines)
