"""sector_content.py — industry-specific content layer for written programs.

Origin's written-program templates (compliance_kb.render_program) are correct for
every trade, but their Purpose/Scope and per-element guidance are industry-neutral
by default. A Lockout/Tagout program reads the same whether the client is a roofing
contractor or a machine shop — yet the *hazards, tasks, equipment, and work
environments* those two trades face are completely different.

This module supplies the missing industry layer. For each NAICS sector in
``naics_map.json`` it holds an authored **sector profile**: the real scope language,
the characteristic hazards, the typical field operations, the equipment/materials,
the work environments, and the PPE emphasis for that industry. ``render_program``
takes an optional ``sector`` and uses this profile to:

  * write an industry-specific **Scope & Application** paragraph (real prose), and
  * write an industry **Hazard Profile** section (the hazards that trade actually
    faces), and
  * anchor every required-element prompt to that industry's operations and hazards,

so a converted client's programs read as though they were written for their trade —
leaving only the company name and exact scope of work as minor tweaks.

Nothing here changes the required elements, citations, or the send-gate — it only
contextualizes the drafting guidance. Sector keys match ``naics_map.json`` exactly
(e.g. "23" construction, "31-33" manufacturing, "48-49" transportation).
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional


# ── the 11 authored industry profiles ────────────────────────────────────────
# Each profile:
#   label        human sector name (mirrors naics_map)
#   scope        authored Scope & Application paragraph (real industry prose)
#   operations   the primary field operations/tasks this trade performs
#   hazards      the characteristic hazards this trade actually faces
#   environments the settings the work happens in
#   equipment    the equipment / materials in play
#   ppe          the baseline PPE emphasis for the trade
SECTORS: Dict[str, dict] = {
    "11": {
        "label": "Agriculture, Forestry, Fishing & Hunting",
        "scope": (
            "{{COMPANY_NAME}} performs agricultural, forestry, and land-management work "
            "including field and harvest operations, logging and timber handling, grain "
            "and material handling, and the operation of tractors, harvesters, chainsaws, "
            "and powered machinery in outdoor and remote settings."),
        "operations": ["field and harvest operations", "logging and timber felling/handling",
                       "grain and bulk material handling", "tractor and mobile-equipment operation",
                       "chainsaw and brush-cutting work", "livestock and land management"],
        "hazards": ["rollover and runover by mobile equipment", "entanglement in PTO and powered machinery",
                    "struck-by falling trees and limbs", "grain engulfment and confined-space atmospheres",
                    "noise, dust, and chemical (pesticide) exposure", "heat and cold stress in the field"],
        "environments": ["open fields", "wooded/logging sites", "grain storage and handling facilities",
                         "remote locations far from emergency services"],
        "equipment": ["tractors and harvesters", "chainsaws and skidders", "augers and grain conveyors",
                      "PTO-driven implements"],
        "ppe": "safety-toe boots, hi-vis, hearing protection, chaps and face screen for saw work, "
               "and respiratory protection for dusts/chemicals",
    },
    "21": {
        "label": "Mining, Quarrying, and Oil & Gas Extraction",
        "scope": (
            "{{COMPANY_NAME}} performs upstream and midstream oilfield and extraction services "
            "— well servicing, drilling/completion support, wireline and workover, pipeline and "
            "facility construction, and quarry/aggregate operations — frequently on operator "
            "locations governed by ISN/Avetta prequalification and operator safety requirements."),
        "operations": ["well servicing, drilling, and completion support", "wireline and workover operations",
                       "pipeline and facility construction", "pressure and flowline work",
                       "quarry, aggregate, and material extraction", "simultaneous operations (SIMOPS) on live locations"],
        "hazards": ["H2S and toxic/flammable atmospheres", "high-pressure release and stored energy",
                    "dropped objects and line-of-fire on the rig floor", "struck-by and caught-between with heavy equipment",
                    "process-safety and hot-work ignition sources", "fatigue from long shifts and remote driving"],
        "environments": ["active well sites and operator locations", "drilling and workover rigs",
                         "pipeline right-of-way", "quarries and aggregate pits"],
        "equipment": ["well-servicing rigs and wireline units", "pressure pumps and flow iron",
                      "cranes and rigging", "gas detection and SCBA/SABA"],
        "ppe": "FR clothing, H2S monitors, SCBA/escape packs, impact gloves, and metatarsal boots",
    },
    "22": {
        "label": "Utilities (electric, gas, water) & Pipeline Operators",
        "scope": (
            "{{COMPANY_NAME}} performs utility construction and maintenance — overhead and "
            "underground electric transmission/distribution, gas and water distribution, "
            "substation work, and pipeline operation and maintenance — often as a qualified "
            "contractor working near or on energized systems under operator qualification rules."),
        "operations": ["overhead and underground line work", "substation construction and maintenance",
                       "gas and water main installation and repair", "pipeline operation, inspection, and repair",
                       "energized and de-energized electrical work", "confined-space entry in vaults and manholes"],
        "hazards": ["contact with energized conductors and arc flash", "electrocution and induced voltage",
                    "confined-space atmospheres in vaults/manholes", "trenching and excavation cave-in",
                    "struck-by traffic in the work zone", "gas release and ignition"],
        "environments": ["overhead lines and poles", "substations and switchyards",
                         "underground vaults and manholes", "pipeline right-of-way and roadways"],
        "equipment": ["bucket trucks and digger derricks", "rubber goods and hotline tools",
                      "grounding sets and voltage detectors", "excavation equipment"],
        "ppe": "arc-rated FR clothing, rubber insulating gloves/sleeves, class-rated hard hats, and hi-vis",
    },
    "23": {
        "label": "Construction",
        "scope": (
            "{{COMPANY_NAME}} performs commercial and industrial construction — site work, "
            "excavation, concrete/masonry, structural erection, and finishing trades — on active "
            "job sites where crews work at height, around heavy equipment, and alongside other "
            "trades under a site-specific safety plan."),
        "operations": ["excavation and trenching", "work at height (roofs, leading edges, steel)",
                       "scaffold erection and use", "crane and rigging operations",
                       "concrete, masonry, and formwork", "demolition and renovation"],
        "hazards": ["falls to a lower level (the leading construction fatality)", "excavation cave-in and engulfment",
                    "struck-by equipment, vehicles, and falling objects", "caught-in/between and crush",
                    "electrocution from overhead lines and temporary power", "silica, lead, and asbestos exposure"],
        "environments": ["active construction sites", "trenches and excavations",
                         "elevated work surfaces and scaffolds", "roadway and work-zone conditions"],
        "equipment": ["excavators, loaders, and cranes", "scaffolds and aerial lifts",
                      "personal fall-arrest systems", "powder-actuated and power tools"],
        "ppe": "hard hat, safety-toe boots, hi-vis, eye protection, and fall-arrest harness where exposed to falls",
    },
    "31-33": {
        "label": "Manufacturing",
        "scope": (
            "{{COMPANY_NAME}} operates a manufacturing/fabrication facility where employees "
            "run production machinery, material-handling equipment, and process lines. Work "
            "involves machine operation and servicing, welding and fabrication, chemical "
            "handling, and powered-industrial-truck traffic inside the plant."),
        "operations": ["production machine operation", "machine setup, servicing, and clearing jams",
                       "welding, cutting, and fabrication", "forklift and material-handling operations",
                       "chemical mixing and process handling", "maintenance of powered equipment"],
        "hazards": ["amputation and caught-in at unguarded machine points of operation", "unexpected machine energization during service",
                    "forklift struck-by and tip-over", "chemical exposure and combustible dust",
                    "noise-induced hearing loss", "ergonomic strain from repetitive assembly"],
        "environments": ["production floor and assembly lines", "fabrication and weld shops",
                         "chemical storage and mixing areas", "warehouse and shipping/receiving"],
        "equipment": ["presses, lathes, mills, and conveyors", "forklifts and powered trucks",
                      "welding and cutting equipment", "machine guards and interlocks"],
        "ppe": "safety glasses/face shield, hearing protection, cut-resistant gloves, metatarsal boots, and welding PPE",
    },
    "42": {
        "label": "Wholesale Trade / Distribution",
        "scope": (
            "{{COMPANY_NAME}} operates a wholesale distribution/warehouse where employees "
            "receive, store, pick, and ship product using forklifts, pallet jacks, and racking "
            "systems, with high volumes of manual material handling and dock/trailer activity."),
        "operations": ["receiving and put-away", "order picking and packing", "forklift and pallet-jack operation",
                       "loading and unloading trailers at the dock", "racking and storage management"],
        "hazards": ["forklift struck-by and pedestrian collisions", "falls from dock edges and trailers",
                    "material falling from height/racking", "manual-handling strains and repetitive lifting",
                    "trailer creep and dock-plate hazards"],
        "environments": ["warehouse floor and racking aisles", "loading docks and trailers",
                         "cold storage where applicable"],
        "equipment": ["forklifts and reach trucks", "pallet jacks and dock plates", "pallet racking and conveyors"],
        "ppe": "hi-vis, safety-toe boots, and gloves for manual handling",
    },
    "48-49": {
        "label": "Transportation & Warehousing (motor carriers, hazmat, marine terminals)",
        "scope": (
            "{{COMPANY_NAME}} performs commercial motor-carrier transportation and warehousing — "
            "operating CMVs on public roads, loading and securing cargo, and, where applicable, "
            "handling hazardous materials and marine-terminal/longshoring cargo — under FMCSA "
            "(49 CFR) driver-qualification, hours-of-service, and vehicle-maintenance rules."),
        "operations": ["commercial motor-vehicle operation", "cargo loading, securement, and unloading",
                       "pre-/post-trip inspections (DVIR)", "hazardous-materials transport where applicable",
                       "yard, dock, and terminal operations"],
        "hazards": ["vehicle crashes and roadway exposure", "driver fatigue and hours-of-service violations",
                    "struck-by/caught-between during coupling and loading", "falls from trailers and cab access",
                    "cargo shift and load-securement failure", "hazmat release in transport"],
        "environments": ["public roadways and highways", "loading docks and terminals",
                         "cargo yards and marine terminals"],
        "equipment": ["tractors, trailers, and CMVs", "load-securement devices and dunnage",
                      "forklifts and yard equipment"],
        "ppe": "hi-vis, safety-toe boots, and gloves; fall protection where mounting/dismounting trailers",
    },
    "56": {
        "label": "Waste Management & Remediation / Environmental Services",
        "scope": (
            "{{COMPANY_NAME}} performs environmental, remediation, and waste-management services — "
            "hazardous-waste site cleanup, spill response, decontamination, tank and vessel work, "
            "and abatement — under HAZWOPER (29 CFR 1910.120) with formal air monitoring and "
            "site-control requirements."),
        "operations": ["hazardous-waste site cleanup and remediation", "emergency spill response",
                       "decontamination and site control", "tank, vessel, and confined-space entry",
                       "asbestos/lead abatement where applicable"],
        "hazards": ["exposure to hazardous substances and unknown chemistries", "oxygen-deficient/toxic confined-space atmospheres",
                    "dermal and inhalation exposure requiring decon", "heat stress in chemical-protective clothing",
                    "fire/explosion from flammable waste"],
        "environments": ["contaminated sites and exclusion zones", "confined spaces and tanks",
                         "spill/response scenes", "transfer and storage facilities"],
        "equipment": ["air monitoring and detection instruments", "chemical-protective clothing and SCBA",
                      "decontamination stations", "spill-control and containment supplies"],
        "ppe": "level-appropriate chemical protective clothing (A–D), respiratory protection/SCBA, and chemical gloves",
    },
    "62": {
        "label": "Health Care & Social Assistance",
        "scope": (
            "{{COMPANY_NAME}} provides health-care and patient-care services where employees "
            "face bloodborne-pathogen and infectious-disease exposure, hazardous-drug handling, "
            "patient-handling ergonomics, and workplace-violence risk in clinical settings."),
        "operations": ["direct patient care and handling", "handling sharps, blood, and body fluids",
                       "hazardous-drug preparation and administration", "laboratory and diagnostic work",
                       "infection prevention and isolation"],
        "hazards": ["bloodborne-pathogen and sharps exposure", "infectious-disease/airborne transmission",
                    "hazardous-drug (chemotherapy) exposure", "musculoskeletal injury from patient handling",
                    "workplace violence from patients/visitors"],
        "environments": ["clinical and patient-care areas", "laboratories and pharmacies",
                         "isolation and procedure rooms"],
        "equipment": ["sharps-safety devices and containers", "biosafety cabinets and closed-system transfer devices",
                      "patient-lift and mobility equipment", "respirators and PPE"],
        "ppe": "gloves, gowns, eye/face protection, and fit-tested respirators for airborne precautions",
    },
    "81": {
        "label": "Repair & Maintenance / Other Services",
        "scope": (
            "{{COMPANY_NAME}} performs equipment repair, mechanical, HVAC, and facility "
            "maintenance services — servicing powered machinery and building systems that must be "
            "de-energized, entered, or hot-worked — often at customer sites under the customer's "
            "site rules."),
        "operations": ["mechanical and equipment repair", "HVAC and refrigeration service",
                       "facility and building-system maintenance", "welding/cutting and fabrication repair",
                       "confined-space and electrical servicing"],
        "hazards": ["unexpected energization during service (LOTO)", "electrical shock and arc flash",
                    "confined-space atmospheres in equipment/vessels", "hot-work ignition and burns",
                    "compressed-gas and refrigerant hazards"],
        "environments": ["customer facilities and equipment rooms", "rooftops and mechanical spaces",
                         "shops and service bays"],
        "equipment": ["hand and power tools", "lockout devices and meters", "welding/cutting sets",
                      "compressed-gas cylinders"],
        "ppe": "safety glasses, gloves, arc-rated PPE for electrical work, and hearing protection",
    },
    "51": {
        "label": "Information / Telecommunications",
        "scope": (
            "{{COMPANY_NAME}} performs telecommunications construction and maintenance — "
            "aerial and underground cable/fiber, tower and antenna work, and central-office/"
            "outside-plant service — near energized power and at height."),
        "operations": ["aerial cable and fiber installation", "tower and antenna climbing/rigging",
                       "underground and manhole cable work", "central-office and outside-plant service"],
        "hazards": ["falls from towers, poles, and aerial platforms", "contact with power lines during telecom work",
                    "confined-space atmospheres in manholes/vaults", "RF exposure near active antennas",
                    "struck-by traffic in the work zone"],
        "environments": ["towers and poles", "manholes and vaults", "central offices", "roadway work zones"],
        "equipment": ["climbing and fall-arrest gear", "aerial lifts and cable rigs",
                      "gas detection", "RF monitors"],
        "ppe": "fall-arrest harness with tower rating, hard hat, hi-vis, and RF protection near antennas",
    },
}


def sector_keys() -> List[str]:
    """Every sector key with an authored profile (matches naics_map)."""
    return list(SECTORS.keys())


def has_sector(sector_key: Optional[str]) -> bool:
    return bool(sector_key) and sector_key in SECTORS


def label(sector_key: str) -> str:
    return (SECTORS.get(sector_key) or {}).get("label", "")


def profile(sector_key: Optional[str]) -> Optional[dict]:
    """Raw authored profile for a sector (label/scope/operations/hazards/
    environments/equipment/ppe), or None. Consumers that need the fields
    directly (e.g. the JHA renderer, which builds HTML rather than markdown)
    use this instead of reaching into SECTORS."""
    if not sector_key:
        return None
    return SECTORS.get(sector_key)


def oxford(items: List[str]) -> str:
    """Public Oxford-comma join, for consumers building their own prose."""
    return _oxford(items)


def _oxford(items: List[str]) -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def scope_block(sector_key: str) -> List[str]:
    """Authored, industry-specific Scope & Application markdown lines for a sector.
    These replace the generic 'It applies to: {{SCOPE}}' line with real trade prose,
    while keeping {{COMPANY_NAME}} and a {{SCOPE}} token for the minor final tweak."""
    p = SECTORS.get(sector_key)
    if not p:
        return []
    return [
        p["scope"],
        "",
        f"*Primary operations covered:* {_oxford(p['operations'])}.",
        "",
        "Specific scope of work for this program: {{SCOPE}}.",
    ]


def hazard_profile_block(sector_key: str) -> List[str]:
    """An authored industry Hazard Profile section — the hazards this trade actually
    faces — so the program reads as written for the client's industry."""
    p = SECTORS.get(sector_key)
    if not p:
        return []
    lines = [
        "## Industry Hazard Profile",
        f"The following hazards are characteristic of {{{{COMPANY_NAME}}}}'s "
        f"{p['label'].split('(')[0].strip().lower()} operations and are the conditions this "
        "program is written to control:",
        "",
    ]
    lines += [f"- {h}" for h in p["hazards"]]
    lines += [
        "",
        f"*Typical work environments:* {_oxford(p['environments'])}.",
        f"*Equipment and materials in scope:* {_oxford(p['equipment'])}.",
        f"*Baseline PPE for this trade:* {p['ppe']}.",
        "",
    ]
    return lines


# Element topics that map to a sharper industry cue than the generic hazard list.
# Keyed loosely; matched by substring against the element's leading label.
def _element_industry_cue(sector_key: str, element: str) -> str:
    p = SECTORS.get(sector_key) or {}
    hz = p.get("hazards", [])
    ops = p.get("operations", [])
    return (f"reflecting the hazards our crews actually face — {_oxford(hz[:3])} — "
            f"during {_oxford(ops[:2])}")


def element_prompt(sector_key: str, element: str) -> Optional[str]:
    """An industry-anchored fill-in prompt for one required element. Returns a
    ``[[...]]`` guidance string that names the trade's operations and hazards so the
    drafter completes it in the client's real context (or None if no sector)."""
    if not has_sector(sector_key):
        return None
    cue = _element_industry_cue(sector_key, element)
    return ("[[Describe how {{COMPANY_NAME}} meets this element in practice — "
            f"{cue}. Name the procedure, who is responsible, the equipment/forms "
            "used, and how it is documented.]]")
