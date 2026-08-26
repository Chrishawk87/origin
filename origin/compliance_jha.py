"""Job Hazard Analysis (JHA) companion generator for Origin.

Every field-work written program in the Asset Library can carry a matching JHA
companion: a document that breaks each primary job into its task steps and lists,
for every step, the potential hazards, the controls / safe work practices, and
the required PPE. JHAs are CLIENT-branded (the contractor's own company fields)
exactly like the programs — Origin never appears on them.

Authoring model
---------------
Each JHA is authored to a specific standard and stored in ``JHA_LIBRARY`` keyed
by the KB program id it accompanies (the same id used for ``program-<id>``
masters). A JHA definition is a dict:

    {
      "subtitle": "Excavation & Trenching Operations",
      "legend":  [(category, description), ...],   # optional; hazard families
      "jobs":    [ (number, title, [ (step, [hazards], [controls], ppe) ]) ],
    }

``render_jha(program_id)`` returns the inner HTML (cover + how-to + job-specific
fill-in + job matrices + crew acknowledgment). The caller wraps it with
``compliance.wrap_document`` so it inherits the shared premium stylesheet and the
neutral page-number footer. Client fields stay as ``{{...}}`` placeholder tokens
the portal fills on assign.

Coverage grows in batches: as JHAs are authored for more field programs, add
them to ``JHA_LIBRARY`` — no other code changes are needed; ``has_jha`` and the
library sync in ``compliance.py`` pick them up automatically.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

# A step is (step name, [hazards], [controls], required PPE)
Step = Tuple[str, List[str], List[str], str]
# A job is (number, title, [steps])
Job = Tuple[str, str, List[Step]]


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# The default hazard-category legend (shown on the How-to page). Individual JHAs
# may override it with a "legend" key tuned to their trade.
_DEFAULT_LEGEND: List[Tuple[str, str]] = [
    ("Struck-By / Caught-Between", "Moving equipment, swinging loads, falling material, pinch points."),
    ("Falls", "Falls to a lower level, slips/trips, falls from access equipment."),
    ("Electrical", "Contact with energized lines/equipment, overhead power lines."),
    ("Atmospheric", "Oxygen deficiency, toxic gases, flammable vapors, engine exhaust."),
    ("Environmental", "Weather, heat / cold stress, noise, dust / silica."),
    ("Manual / Ergonomic", "Lifting, awkward postures, repetitive motion."),
    ("Public / Traffic", "Vehicle traffic, pedestrians, adjacent structures."),
]


# ── Rendering ────────────────────────────────────────────────────────────────
def _cover(title: str, subtitle: str, citation: str, program_title: str,
           program_std: str) -> str:
    std = _esc(citation or "")
    assoc = _esc(program_title or "the associated written program")
    if program_std:
        assoc += f" ({_esc(program_std)})"
    rows = [
        ("Revision", "1.0"),
        ("Effective Date", "{{EFFECTIVE_DATE}}"),
        ("Associated Program", assoc),
        ("Regulatory Basis", std or "See program body"),
        ("Review Cycle", "Annual / upon scope change"),
        ("Prepared / Reviewed By", "{{PROGRAM_ADMINISTRATOR}}, {{ADMIN_TITLE}}"),
        ("Classification", "Controlled &mdash; Internal Use"),
    ]
    ctrl = "".join(f"<tr><td class='k'>{k}</td><td class='v'>{v}</td></tr>"
                   for k, v in rows)
    std_line = f"<div class='std'>{std}</div>" if std else ""
    # subtitle is authored (may carry entities like &amp;) — insert raw.
    sub_line = f"<div class='subtitle'>{subtitle}</div>" if subtitle else ""
    return (
        "<div class='cover'>"
        "<div class='brandbar'>"
        "<div class='client'>{{COMPANY_NAME}}</div>"
        "<div class='client-addr'>{{COMPANY_ADDRESS}}</div>"
        "</div>"
        "<div class='kicker'>JOB HAZARD ANALYSIS</div>"
        f"<div class='title'>{_esc(title)}</div>"
        f"{sub_line}{std_line}"
        "<div class='coverrule'></div>"
        f"<div class='ctrl'><table class='ctrl-t'>{ctrl}</table></div>"
        "<div class='notice'><b>CONTROLLED DOCUMENT.</b> This Job Hazard Analysis is the "
        "property of {{COMPANY_NAME}} and supports its written safety program. It must be "
        "reviewed with the crew before work begins and re-evaluated whenever site conditions, "
        "scope, or personnel change. Printed copies are uncontrolled.</div>"
        "</div>"
    )


def _how_to(legend: List[Tuple[str, str]]) -> str:
    leg = "".join(
        f"<tr><td class='k'>{cat}</td><td>{desc}</td></tr>"
        for cat, desc in (legend or _DEFAULT_LEGEND)
    )
    return (
        "<div class='pb'>"
        "<h2>How to Use This Job Hazard Analysis</h2>"
        "<p>A Job Hazard Analysis (JHA) breaks each phase of the work into its individual "
        "task steps, identifies the hazards present at every step, and specifies the controls "
        "and personal protective equipment (PPE) required to perform the step safely. This JHA "
        "covers all primary jobs associated with this scope of work; not every job will apply "
        "to every project.</p>"
        "<ul>"
        "<li>The competent person selects the applicable primary jobs for the scope of work "
        "and reviews them with the crew during the pre-job / tailgate meeting.</li>"
        "<li>Each worker signs the acknowledgment sheet confirming they understand the hazards "
        "and controls.</li>"
        "<li>If a new hazard, task, or condition arises that is not covered here, stop work and "
        "update the JHA before proceeding.</li>"
        "</ul>"
        "<h2>Hazard Categories Covered</h2>"
        f"<table class='legend'>{leg}</table>"
        "<div class='callout warning'><span class='lbl'>STOP-WORK AUTHORITY</span>"
        "Any worker may stop the job at any time for a suspected hazard, without fear of "
        "reprisal. Work does not resume until the competent person has corrected the condition."
        "</div>"
        "</div>"
    )


def _job_specifics() -> str:
    def fld(label: str, lines: int = 1) -> str:
        blanks = "<br/>".join(["&nbsp;"] * lines)
        return f"<td class='k'>{label}</td><td class='fill'>{blanks}</td>"

    grid = (
        f"<tr>{fld('Project / Job name')}{fld('Date')}</tr>"
        f"<tr>{fld('Location / Site')}{fld('Job / PO number')}</tr>"
        f"<tr>{fld('Competent person')}{fld('Supervisor')}</tr>"
        f"<tr>{fld('Scope of work today', 2)}{fld('Applicable primary jobs (from matrix)', 2)}</tr>"
        f"<tr>{fld('Permits required (dig / CS / hot work)')}{fld('Reviewed by')}</tr>"
    )
    spec_rows = [
        "Equipment in use", "Soil / ground conditions", "Utilities present / located",
        "Adjacent structures / loads", "Weather / environmental",
        "Emergency muster / hospital", "Other site-specific hazards",
        "Additional controls added",
    ]
    specs = ""
    for i, r in enumerate(spec_rows):
        lines = 3 if i >= 6 else 2
        blanks = "<br/>".join(["&nbsp;"] * lines)
        specs += f"<tr><td class='k'>{r}</td><td class='fill'>{blanks}</td></tr>"
    return (
        "<div class='pb'>"
        "<h2>Job-Specific Details "
        "<span style='font-size:10px;font-weight:normal;color:#7a8a99'>"
        "(Complete on site before work begins)</span></h2>"
        "<p>The competent person completes this section with the crew during the pre-job / "
        "tailgate meeting to tailor this JHA to the actual site, equipment, and conditions. "
        "Update it whenever conditions change.</p>"
        f"<table class='specs'>{grid}</table>"
        "<div class='subhead'>Site-Specific Hazards &amp; Conditions</div>"
        f"<table class='specs'>{specs}</table>"
        "</div>"
    )


def _cell(items: List[str]) -> str:
    # Authored content carries intentional HTML entities (&amp;, &ge;, &frac12;)
    # — insert raw, never re-escape.
    return "".join(f"&bull; {x}<br/>" for x in items)


def _job_block(num: str, title: str, steps: List[Step]) -> str:
    head = (f"<tr><td colspan='4' style='padding:0;border:none'>"
            f"<div class='jobhead'>Job {num} &nbsp;&mdash;&nbsp; {title}</div></td></tr>")
    rows = ("<tr>"
            "<th style='width:19%'>Task Step</th>"
            "<th style='width:29%'>Potential Hazards</th>"
            "<th style='width:37%'>Controls / Safe Work Practices</th>"
            "<th style='width:15%'>Required PPE</th></tr>")
    for i, (step, hz, ct, ppe) in enumerate(steps):
        alt = " class='alt'" if i % 2 else ""
        rows += (f"<tr{alt}><td class='step'>{step}</td>"
                 f"<td class='hz'>{_cell(hz)}</td>"
                 f"<td>{_cell(ct)}</td><td>{_esc(ppe)}</td></tr>")
    return f"<table class='jha'>{head}{rows}</table>"


def _matrices(jobs: List[Job]) -> str:
    html = ("<div class='pb'><h2>Job Hazard Analysis Matrix</h2>"
            "<p>The following matrices cover every primary job associated with this scope of "
            "work. The competent person selects the jobs applicable to the project and reviews "
            "them with the crew.</p>")
    for num, title, steps in jobs:
        html += _job_block(num, title, steps)
    return html + "</div>"


def _ack() -> str:
    rows = ""
    for i in range(1, 13):
        rows += (f"<tr><td style='width:6%;text-align:center'>{i}</td>"
                 f"<td style='width:36%;height:20px'>&nbsp;</td>"
                 f"<td style='width:34%'>&nbsp;</td>"
                 f"<td style='width:24%'>&nbsp;</td></tr>")
    return (
        "<div class='pb'>"
        "<h2>Crew Acknowledgment</h2>"
        "<p>By signing below, each worker confirms they have reviewed this Job Hazard Analysis, "
        "understand the hazards and required controls for the tasks they will perform, and agree "
        "to stop work and notify the competent person if conditions change or a new hazard is "
        "identified.</p>"
        "<p><b>Project / Location:</b> ______________________________ &nbsp;&nbsp;"
        "<b>Date:</b> ______________ &nbsp;&nbsp; "
        "<b>Competent Person:</b> ______________________________</p>"
        "<table class='ack'>"
        "<tr><th style='width:6%;text-align:center'>#</th>"
        "<th style='width:36%'>Print Name</th>"
        "<th style='width:34%'>Signature</th>"
        "<th style='width:24%'>Company / Trade</th></tr>"
        f"{rows}</table>"
        "<p style='margin-top:12px;font-size:9px;color:#64748b'>Retain the signed acknowledgment "
        "with the project safety file. Re-brief and re-sign whenever the scope, crew, or site "
        "conditions change.</p>"
        "</div>"
    )


def render_jha(program_id: str) -> Optional[str]:
    """Return the inner HTML for the JHA that accompanies ``program_id``, or None
    if no JHA has been authored for that program yet. Wrap the result with
    ``compliance.wrap_document`` to produce the stored/printable master."""
    jha = JHA_LIBRARY.get(program_id)
    if not jha:
        return None

    title = "Job Hazard Analysis"
    subtitle = jha.get("subtitle", "")
    citation = jha.get("citation", "")

    program_title, program_std = "", ""
    try:
        from . import compliance_kb as _kb
        rec = _kb.get(program_id) or {}
        program_title = rec.get("title", "")
        program_std = rec.get("citation", "")
        if not citation:
            citation = program_std
    except Exception:
        pass

    return (
        _cover(title, subtitle, citation, program_title, program_std)
        + _how_to(jha.get("legend", []))
        + _job_specifics()
        + _matrices(jha.get("jobs", []))
        + _ack()
    )


def has_jha(program_id: str) -> bool:
    return program_id in JHA_LIBRARY


def list_jha_ids() -> List[str]:
    return list(JHA_LIBRARY.keys())


def jha_title(program_id: str) -> str:
    jha = JHA_LIBRARY.get(program_id) or {}
    sub = jha.get("subtitle", "")
    return f"Job Hazard Analysis — {sub}" if sub else "Job Hazard Analysis"


# ═════════════════════════════════════════════════════════════════════════════
# Authored JHAs. Keyed by KB program id. Batch 1 begins with the highest-risk
# field trades; more are appended as they are authored and reviewed.
# ═════════════════════════════════════════════════════════════════════════════
JHA_LIBRARY: Dict[str, dict] = {}

# ── Excavation & Trenching (29 CFR 1926 Subpart P) ──────────────────────────
JHA_LIBRARY["29-cfr-1926-subpart-p-1926-651-652-excavation-and-trenching"] = {
    "subtitle": "Excavation &amp; Trenching Operations",
    "legend": [
        ("Cave-in / Collapse", "Soil movement, wall failure, spoil sliding back into the excavation."),
        ("Struck-By / Caught-Between", "Equipment, swinging loads, falling material, pinch points."),
        ("Falls", "Falls into the excavation, slips/trips at the edge, falls from access equipment."),
        ("Utilities", "Contact with underground gas, electric, water, sewer, fiber; overhead power lines."),
        ("Atmospheric", "Oxygen deficiency, toxic gases (H2S, CO), flammable vapors, engine exhaust."),
        ("Environmental", "Water accumulation, weather, heat / cold stress, noise, dust / silica."),
        ("Manual / Ergonomic", "Lifting, shoveling, awkward postures, repetitive motion."),
        ("Public / Traffic", "Vehicle traffic, pedestrians, adjacent structures."),
    ],
    "jobs": [
        ("1", "Pre-Job Planning &amp; Site Assessment", [
            ("Review scope, drawings &amp; permits",
             ["Incomplete/incorrect information leading to unsafe assumptions", "Unknown subsurface conditions"],
             ["Obtain and review site drawings, soil reports, and dig permits",
              "Identify depth, length, soil type, water table, adjacent loads",
              "Confirm required permits (dig, confined space, hot work) are in place"],
             "Standard PPE"),
            ("Conduct site walk &amp; hazard survey",
             ["Overhead power lines", "Adjacent structures/foundations", "Unstable ground, prior disturbance", "Traffic and public exposure"],
             ["Walk the route; note overhead lines and maintain clearance",
              "Identify structures within the influence zone",
              "Flag soft/disturbed ground and standing water",
              "Plan traffic control and exclusion zones"],
             "Hi-vis vest, hard hat, safety-toe boots"),
            ("Designate competent person &amp; hold pre-job meeting",
             ["Unclear roles/authority", "Crew unaware of hazards"],
             ["Designate a qualified competent person in writing",
              "Review this JHA and the safety program with all crew",
              "Confirm emergency contacts, muster point, and nearest hospital"],
             "Standard PPE"),
        ]),
        ("2", "Utility Location &amp; Marking", [
            ("Request public/private locates (811)",
             ["Unmarked or mislocated utilities", "Damaged gas/electric/fiber lines"],
             ["Call 811 and confirm all locates complete before digging",
              "Arrange private locates for on-site/utility-owned lines",
              "Do not dig until all tickets are positive-response confirmed"],
             "Standard PPE"),
            ("Verify &amp; expose utilities (potholing / hand dig)",
             ["Contact with energized/pressurized lines", "Struck-by hand tools", "Silica dust from hydro/air-vac"],
             ["Hand-dig or use vacuum excavation within the tolerance zone",
              "Maintain required clearance from marked lines",
              "Expose and physically confirm depth/location of each utility"],
             "Cut-resistant gloves, safety glasses, hi-vis, hard hat"),
            ("Maintain overhead line clearance",
             ["Electrocution from equipment contact with overhead lines"],
             ["Identify voltage and maintain minimum approach distance",
              "Use spotter and physical barriers/goalposts",
              "De-energize or insulate lines where clearance cannot be met"],
             "Standard PPE"),
        ]),
        ("3", "Mobilization &amp; Equipment Setup", [
            ("Deliver &amp; stage equipment/materials",
             ["Struck-by during offloading", "Caught-between trailer and load", "Traffic hazards"],
             ["Establish laydown/exclusion zones with spotters",
              "Chock and secure trailers; controlled offloading",
              "Set up traffic control before staging on roadways"],
             "Hi-vis, hard hat, safety-toe boots, gloves"),
            ("Inspect excavator/equipment (pre-use)",
             ["Equipment failure", "Hydraulic leaks/fire", "Uncontrolled movement"],
             ["Complete documented pre-use inspection",
              "Tag-out defective equipment",
              "Confirm backup alarms, mirrors, ROPS/FOPS functional"],
             "Standard PPE"),
            ("Position equipment relative to the dig",
             ["Overloading trench edge causing collapse", "Tip-over on soft/uneven ground", "Struck-by swing radius"],
             ["Keep equipment/spoil back min. 2 ft from edge",
              "Set on stable, level ground; use mats if needed",
              "Barricade the swing radius; use a spotter"],
             "Hi-vis, hard hat"),
        ]),
        ("4", "Excavation / Digging Operations", [
            ("Operate excavator / trench digging",
             ["Struck-by bucket/swing", "Caught-between machine and object", "Cave-in of open wall", "Utility strike"],
             ["Keep all personnel out of the swing radius and bucket path",
              "No personnel in trench during active digging",
              "Use spotter; maintain eye contact/hand signals",
              "Dig to soil-appropriate slope; stop at marked utilities"],
             "Hi-vis, hard hat, safety-toe boots"),
            ("Hand excavation / trimming",
             ["Overexertion/strains", "Struck-by falling material", "Contact with utilities"],
             ["Rotate tasks; proper lifting/shoveling technique",
              "Keep sidewalls trimmed; scale loose material",
              "Hand tools only near located utilities"],
             "Gloves, safety glasses, hi-vis, hard hat"),
            ("Monitor soil &amp; spoil during dig",
             ["Progressive wall failure", "Spoil sliding back into cut", "Tension cracks"],
             ["Competent person observes for cracks, spalling, water",
              "Maintain spoil setback; slope or bench as required",
              "Downgrade soil class if disturbed/vibrated/wet"],
             "Standard PPE"),
        ]),
        ("5", "Spoil Pile &amp; Material Management", [
            ("Place and maintain spoil piles",
             ["Spoil sliding into excavation", "Surcharge load causing collapse", "Struck-by rolling material"],
             ["Keep spoil min. 2 ft from edge (further for deep cuts)",
              "Slope spoil away from the excavation",
              "Barricade spoil piles from foot traffic"],
             "Hi-vis, hard hat, boots"),
            ("Manage stored materials/pipe near edge",
             ["Rolling/sliding pipe", "Surcharge loading", "Trip hazards"],
             ["Chock/secure pipe and materials",
              "Stage heavy materials outside the influence zone",
              "Maintain clear walkways"],
             "Gloves, hi-vis, boots"),
        ]),
        ("6", "Protective Systems &mdash; Sloping &amp; Benching", [
            ("Classify soil &amp; select slope",
             ["Incorrect classification leading to collapse"],
             ["Competent person performs one visual + one manual test",
              "Apply max allowable slope for soil type (A 3/4:1, B 1:1, C 1&frac12;:1)",
              "Re-classify after rain, vibration, or disturbance"],
             "Standard PPE"),
            ("Cut slopes / benches",
             ["Wall failure during cutting", "Struck-by equipment", "Loose material fall"],
             ["Cut from the top down to the required angle",
              "Keep personnel clear of the machine and wall",
              "Scale loose rock/soil before entry"],
             "Hi-vis, hard hat, boots, gloves"),
        ]),
        ("7", "Protective Systems &mdash; Shoring &amp; Shielding", [
            ("Install trench box / shield",
             ["Cave-in during installation", "Struck-by/caught-between box and wall", "Overhead lifting hazards"],
             ["Install from outside; never enter unprotected trench",
              "Use tag lines; keep hands/feet clear of pinch points",
              "Rated rigging; certified operator; spotter for the lift",
              "Shield extended min. 18 in above the excavation where sloped"],
             "Hard hat, hi-vis, gloves, boots"),
            ("Install hydraulic/timber shoring",
             ["Collapse during setup", "Struck-by falling components", "Pressurized hydraulic failure"],
             ["Follow manufacturer/tabulated data for spacing",
              "Install top-down, remove bottom-up",
              "Inspect hoses/cylinders before use"],
             "Hard hat, hi-vis, gloves, safety glasses"),
            ("Inspect protective system before each use",
             ["Undetected damage/failure"],
             ["Competent person inspects boxes/shoring each shift",
              "Remove damaged components from service",
              "Verify system rated for depth and soil"],
             "Standard PPE"),
        ]),
        ("8", "Access &amp; Egress", [
            ("Install ladders/ramps/stairs",
             ["Falls entering/exiting", "Ladder failure", "Excessive travel distance in emergency"],
             ["Provide egress within 25 ft of workers in trenches &ge;4 ft",
              "Secure ladders; extend 3 ft above the landing",
              "Inspect access equipment before use"],
             "Hard hat, hi-vis, gloves, boots"),
            ("Control edge / opening",
             ["Falls into excavation", "Material kicked onto workers below"],
             ["Barricade or cover openings; toe boards where needed",
              "Keep the edge clear of tools/debris",
              "Warning barricades/lighting at night"],
             "Standard PPE"),
        ]),
        ("9", "Entry &amp; Work Inside the Excavation", [
            ("Authorize entry",
             ["Entry into unprotected/unsafe trench", "Unknown atmosphere"],
             ["Competent person authorizes entry only after inspection",
              "Confirm protective system in place and egress available",
              "Test atmosphere where required (see Job 10)"],
             "Full PPE per task"),
            ("Perform work in the trench (pipe, tie-in, etc.)",
             ["Cave-in", "Struck-by material/loads from above", "Awkward postures/strains", "Contact with utilities"],
             ["Stay within the protected zone (box/shored area)",
              "No one under suspended loads; use tag lines",
              "Pass tools/materials, don't throw",
              "Rotate crews to limit fatigue"],
             "Hard hat, hi-vis, gloves, safety glasses; H2S monitor if required"),
            ("Continuous monitoring during occupancy",
             ["Deteriorating conditions", "Rising water/atmosphere change"],
             ["Competent person monitors throughout the shift",
              "Re-inspect after rain or any hazard-increasing event",
              "Evacuate immediately on any warning sign"],
             "Standard PPE + monitor"),
        ]),
        ("10", "Atmospheric Monitoring &amp; Ventilation", [
            ("Test atmosphere before &amp; during entry",
             ["Oxygen deficiency/enrichment", "Toxic gas (H2S, CO)", "Flammable atmosphere"],
             ["Test O2, LEL, H2S/CO before entry for cuts &gt;4 ft or where expected",
              "Continuous monitoring while occupied",
              "Set alarms per exposure limits; evacuate on alarm"],
             "Calibrated 4-gas monitor, standard PPE"),
            ("Ventilate as required",
             ["Accumulation of hazardous atmosphere", "Engine exhaust entering trench"],
             ["Provide mechanical ventilation where needed",
              "Keep combustion engines/exhaust away from the opening",
              "Use respiratory protection when controls insufficient"],
             "Ventilation equipment; respirator if required"),
        ]),
        ("11", "Water Control / Dewatering", [
            ("Set up &amp; operate dewatering pumps",
             ["Working in accumulated water", "Electrical shock from pumps", "Undermining of walls"],
             ["No entry with water present unless controls in place",
              "GFCI-protected, grounded equipment; inspect cords",
              "Competent person monitors water-removal equipment"],
             "Boots, gloves, hi-vis, hard hat"),
            ("Manage surface water &amp; runoff",
             ["Water flowing into the cut destabilizing walls", "Slip hazards"],
             ["Divert surface water with berms/ditching",
              "Re-inspect and re-classify soil after water intrusion",
              "Maintain pumps and discharge away from the excavation"],
             "Standard PPE"),
        ]),
        ("12", "Material Handling &amp; Hoisting Over the Excavation", [
            ("Rig &amp; hoist pipe/materials into trench",
             ["Struck-by falling/swinging load", "Caught-between load and wall", "Rigging failure"],
             ["Qualified rigger/operator; rated, inspected rigging",
              "Tag lines to control the load; no one under the load",
              "Clear the trench of non-essential personnel during lifts"],
             "Hard hat, hi-vis, gloves, boots"),
            ("Guide load to position",
             ["Pinch points", "Loss of load control"],
             ["Use tag lines and hand signals; keep hands clear",
              "Stable footing; positioned out of the line of fire",
              "Lower slowly under operator control"],
             "Gloves, hard hat, hi-vis, boots"),
        ]),
        ("13", "Backfilling &amp; Compaction", [
            ("Remove protective system &amp; backfill",
             ["Collapse during shield removal", "Struck-by equipment/material", "Caught-between"],
             ["Remove shoring/box from the bottom up as backfill rises",
              "No personnel in trench during backfill/removal",
              "Spotter for equipment; controlled placement of fill"],
             "Hi-vis, hard hat, gloves, boots"),
            ("Operate compaction equipment",
             ["Noise", "Vibration/HAVS", "Dust/silica", "Struck-by/caught"],
             ["Hearing protection; limit exposure time",
              "Wet methods/controls for dust; maintain equipment",
              "Keep bystanders clear of compactor path"],
             "Hearing protection, dust mask/respirator, gloves, hi-vis"),
        ]),
        ("14", "Traffic Control &amp; Public Protection", [
            ("Set up traffic control",
             ["Struck-by vehicles", "Public/pedestrian entry into work zone"],
             ["Deploy signs, cones, barricades per traffic control plan",
              "Flaggers where required; hi-vis at all times",
              "Fence/barricade the excavation from the public"],
             "Class 2/3 hi-vis, hard hat, boots"),
            ("Maintain barricades &amp; night protection",
             ["Falls by public/workers", "Vehicle intrusion after hours"],
             ["Hard barricade open excavations; cover where possible",
              "Warning lights/reflectors for low light",
              "Inspect barricades each shift"],
             "Hi-vis, hard hat"),
        ]),
        ("15", "Site Restoration &amp; Demobilization", [
            ("Final grade &amp; restore",
             ["Struck-by equipment", "Slips/trips", "Manual handling strains"],
             ["Spotters for equipment; controlled operations",
              "Clear debris; maintain walking surfaces",
              "Team lifts / mechanical aids for heavy items"],
             "Hi-vis, hard hat, gloves, boots"),
            ("Demobilize equipment &amp; secure site",
             ["Struck-by/caught during loading", "Traffic hazards", "Overlooked open holes"],
             ["Controlled loading with spotters; secure loads",
              "Maintain traffic control until clear",
              "Confirm all excavations closed/barricaded before leaving"],
             "Hi-vis, hard hat, gloves, boots"),
        ]),
    ],
}

# ── Fall Protection (Construction) (29 CFR 1926 Subpart M) ───────────────────
JHA_LIBRARY["29-cfr-1926-subpart-m-1926-501-503-fall-protection-construction"] = {
    "subtitle": "Fall Protection &mdash; Construction Operations",
    "legend": [
        ("Falls to Lower Level", "Unprotected edges, holes, leading edges, roofs, elevated work surfaces."),
        ("Falls on Same Level", "Slips, trips, cluttered walking/working surfaces."),
        ("Struck-By / Falling Objects", "Dropped tools/material onto workers below."),
        ("Equipment Failure", "Defective harness, lanyard, anchor, connector, or guardrail."),
        ("Suspension Trauma", "Prolonged suspension in a harness after a fall."),
        ("Access / Egress", "Falls entering/exiting elevated work areas or through openings."),
        ("Environmental", "Wind, weather, heat / cold, poor lighting."),
    ],
    "jobs": [
        ("1", "Pre-Job Planning &amp; Fall-Hazard Survey", [
            ("Identify fall hazards &amp; select systems",
             ["Unrecognized fall exposures &ge;6 ft", "Wrong system for the task"],
             ["Survey the work area for edges, holes, leading edges, and elevations &ge;6 ft",
              "Select the appropriate system (guardrail, PFAS, safety net, hole cover)",
              "Document anchor locations rated 5,000 lb per worker or engineered 2:1"],
             "Standard PPE"),
            ("Develop &amp; review fall-protection / rescue plan",
             ["No prompt rescue capability", "Crew unaware of the plan"],
             ["Prepare a written fall-protection and prompt-rescue plan",
              "Review this JHA and the plan with all affected workers",
              "Confirm rescue equipment and trained rescuers are on site"],
             "Standard PPE"),
        ]),
        ("2", "Inspect Fall-Protection Equipment", [
            ("Inspect harness, lanyard &amp; connectors (pre-use)",
             ["Undetected webbing/hardware damage", "Wrong or incompatible connectors"],
             ["Competent person and each user inspect PFAS before every use",
              "Check webbing for cuts/fraying/burns; hardware for cracks/corrosion",
              "Remove damaged or previously arrested equipment from service"],
             "Full-body harness, gloves"),
            ("Verify anchors &amp; SRLs",
             ["Anchor failure", "Improper SRL use/orientation"],
             ["Confirm anchor rating and installation before connecting",
              "Use SRLs/lanyards per the manufacturer for the anchor location",
              "Limit free fall to 6 ft (or per device); verify fall clearance below"],
             "Harness, connectors"),
        ]),
        ("3", "Install Guardrail Systems", [
            ("Erect guardrails at edges &amp; openings",
             ["Falls during installation", "Incorrect rail height/strength"],
             ["Top rail 42 in &plusmn;3 in; midrail; withstand 200 lb outward force",
              "Tie off during installation where exposed to a fall",
              "Add toe boards where workers pass or work below"],
             "Harness (during install), hard hat, gloves"),
            ("Protect holes &amp; skylights",
             ["Fall through hole/skylight", "Cover displaced or not rated"],
             ["Cover holes with secured, marked covers rated 2x the max load",
              "Guard or screen skylights; never use unrated covers",
              "Label covers 'HOLE' / 'COVER' and secure against displacement"],
             "Standard PPE"),
        ]),
        ("4", "Personal Fall Arrest System (PFAS) Use", [
            ("Don &amp; connect PFAS",
             ["Improper fit", "Connecting to a non-rated anchor", "Excessive free fall / swing fall"],
             ["Adjust harness snugly; dorsal D-ring between shoulder blades",
              "Connect only to rated anchors; keep the connection high",
              "Position anchor to minimize swing fall; verify clearance to the level below"],
             "Full-body harness, lanyard/SRL, hard hat"),
            ("Work while tied off",
             ["Falling objects to workers below", "Snag/entanglement", "Loss of 100% tie-off during moves"],
             ["Use tool tethers/bags; barricade the area below",
              "Route lanyards clear of edges and moving equipment",
              "Use twin-leg lanyards to maintain 100% tie-off when repositioning"],
             "Harness, connectors, gloves, hard hat"),
        ]),
        ("5", "Leading-Edge &amp; Low-Slope Roof Work", [
            ("Work at leading edges",
             ["Fall from the advancing edge", "Trip on materials/debris"],
             ["Use guardrail, PFAS, or safety net at leading edges",
              "Where infeasible, use a written controlled-access zone plan",
              "Keep the deck clear of debris and materials"],
             "Harness/SRL, hard hat, boots"),
            ("Low-slope roofing near edges",
             ["Fall over the roof edge", "Wind gusts"],
             ["Warning line + safety monitor, or conventional protection per distance",
              "Suspend work in high wind or slippery conditions",
              "Establish and mark controlled-access zones as required"],
             "Harness where required, hi-vis, hard hat"),
        ]),
        ("6", "Access / Egress to Elevated Areas", [
            ("Transition on/off the work surface",
             ["Falls during transition", "Loss of tie-off at access points"],
             ["Provide safe access (ladder/stair/lift) inspected before use",
              "Maintain continuous tie-off through the transition where exposed",
              "Keep three points of contact on ladders"],
             "Harness, gloves, boots, hard hat"),
        ]),
        ("7", "Emergency &amp; Rescue", [
            ("Respond to a fall / perform rescue",
             ["Delayed rescue causing suspension trauma", "Rescuer exposure"],
             ["Activate the pre-planned prompt-rescue procedure immediately",
              "Use retrieval/rescue equipment; do not rely solely on 911",
              "Provide suspension-trauma relief straps; monitor the worker"],
             "Rescue kit, harness, first-aid"),
        ]),
    ],
}

# ── Scaffolding (29 CFR 1926 Subpart L) ──────────────────────────────────────
JHA_LIBRARY["29-cfr-1926-subpart-l-1926-451-454-scaffolding"] = {
    "subtitle": "Scaffold Erection, Use &amp; Dismantling",
    "legend": [
        ("Falls", "Falls from the platform, during erection/dismantling, or from access."),
        ("Scaffold Collapse", "Overloading, inadequate footing, missing components, improper ties."),
        ("Struck-By / Falling Objects", "Dropped components, tools, and materials onto workers below."),
        ("Electrical", "Contact with overhead power lines during erection or use."),
        ("Access / Egress", "Climbing frames, unsafe ladders, gaps at the platform."),
        ("Manual / Ergonomic", "Handling frames, planks, and mud sills."),
        ("Environmental", "Wind, weather, poor footing, lighting."),
    ],
    "jobs": [
        ("1", "Planning &amp; Design", [
            ("Plan scaffold type, capacity &amp; location",
             ["Overload/undersized scaffold", "Overhead power line proximity", "Unstable ground"],
             ["Qualified person designs the scaffold for the intended load (4:1)",
              "Maintain clearance from power lines (min. 10 ft for &le;300V configs, more for higher)",
              "Assess ground bearing; plan sills/base plates"],
             "Standard PPE"),
            ("Pre-job meeting &amp; competent-person designation",
             ["Unclear roles", "Crew unaware of hazards"],
             ["Designate a competent person for supervision and inspection",
              "Review this JHA and the erection sequence with the crew"],
             "Standard PPE"),
        ]),
        ("2", "Foundation &amp; Base Setup", [
            ("Set base plates, mud sills &amp; screw jacks",
             ["Settlement/tip-over", "Manual handling strains"],
             ["Set on firm, level ground; use base plates on mud sills",
              "Level and plumb the first lift; never use unstable blocking",
              "Team lift heavy sills; proper lifting technique"],
             "Gloves, hard hat, boots, hi-vis"),
        ]),
        ("3", "Erection", [
            ("Erect frames/standards &amp; braces",
             ["Falls during erection", "Struck-by components", "Collapse from missing bracing"],
             ["Competent person supervises erection",
              "Install all braces; do not skip components",
              "Use fall protection during erection where feasible per competent person"],
             "Harness where required, hard hat, gloves, boots"),
            ("Install ties / guys / bracing",
             ["Scaffold tip-over", "Falls while tying"],
             ["Tie to the structure at required vertical/horizontal intervals",
              "Maintain height-to-base ratio &le;4:1 unless tied/guyed",
              "Tie off while installing ties above safe height"],
             "Harness, hard hat, gloves"),
        ]),
        ("4", "Planking &amp; Decking", [
            ("Install platform planks",
             ["Fall through gaps", "Plank failure/uplift", "Tripping"],
             ["Fully plank the work platform; gaps &le;1 in",
              "Use scaffold-grade planks; overlap/cleat ends against uplift",
              "Plank extends 6&ndash;12 in past supports; inspect for defects"],
             "Harness where required, gloves, boots"),
        ]),
        ("5", "Guardrails &amp; Falling-Object Protection", [
            ("Install guardrails &amp; toe boards",
             ["Falls from platform &ge;10 ft", "Objects falling to workers below"],
             ["Guardrails on open sides/ends &ge;10 ft; top rail ~42 in, midrail",
              "Install toe boards; screens/nets where workers pass below",
              "Barricade the area beneath the scaffold"],
             "Hard hat, gloves, harness during install"),
        ]),
        ("6", "Access", [
            ("Provide safe access to platforms",
             ["Falls from climbing frames", "Unsafe ladder use"],
             ["Provide stair towers, ladders, or ramps &mdash; never climb cross-braces",
              "Secure access ladders; maintain three points of contact",
              "Access points free of materials and debris"],
             "Gloves, boots, hard hat"),
        ]),
        ("7", "Use, Loading &amp; Inspection", [
            ("Load and work from the scaffold",
             ["Overloading", "Falls", "Struck-by hoisted material"],
             ["Do not exceed the rated load; distribute material evenly",
              "Keep guardrails in place; maintain fall protection where required",
              "Control hoisted materials with tag lines; no one beneath the load"],
             "Hard hat, hi-vis, gloves; harness where required"),
            ("Competent-person inspection &amp; tagging",
             ["Use of a damaged/altered scaffold", "Unknown status after weather"],
             ["Competent person inspects before each shift and after any event",
              "Tag scaffold status (green/yellow/red); do not use red-tagged",
              "Remove from service and repair defects before reuse"],
             "Standard PPE"),
        ]),
        ("8", "Dismantling", [
            ("Dismantle in reverse sequence",
             ["Collapse from premature component removal", "Falls", "Struck-by dropped components"],
             ["Competent person supervises; dismantle top-down in reverse order",
              "Maintain fall protection and ties until no longer needed",
              "Lower components; never drop; keep the area below barricaded"],
             "Harness, hard hat, gloves, boots"),
        ]),
    ],
}

# ── Cranes &amp; Derricks in Construction (29 CFR 1926 Subpart CC) ───────────
JHA_LIBRARY["29-cfr-1926-subpart-cc-1926-1400-1442-cranes-and-derricks-in-construction"] = {
    "subtitle": "Crane &amp; Rigging Lifting Operations",
    "legend": [
        ("Struck-By / Caught-Between", "Swinging loads, boom, counterweight rotation, pinch points."),
        ("Load Drop / Rigging Failure", "Overload, defective rigging, improper hitch, two-blocking."),
        ("Crane Tip-Over / Collapse", "Unstable ground, out-of-level, exceeding the load chart."),
        ("Electrical", "Contact with overhead power lines."),
        ("Falls", "Falls during assembly/disassembly and access to the cab."),
        ("Environmental", "Wind, poor visibility, weather affecting lift stability."),
    ],
    "jobs": [
        ("1", "Lift Planning", [
            ("Develop the lift plan",
             ["Exceeding capacity", "Unknown load weight", "Power line exposure"],
             ["Qualified person prepares the lift plan and load chart review",
              "Confirm load weight, radius, and configuration vs. capacity",
              "Identify power lines and plan clearances/encroachment prevention"],
             "Standard PPE"),
            ("Pre-lift meeting &amp; roles",
             ["Unclear roles/signals", "Crew unaware of hazards"],
             ["Assign qualified operator, rigger, and signal person",
              "Review this JHA, the lift plan, and signals with the crew",
              "Establish a single designated signal person"],
             "Standard PPE"),
        ]),
        ("2", "Ground Conditions &amp; Setup", [
            ("Assess ground &amp; set outriggers/mats",
             ["Tip-over from soft/uneven ground", "Settlement"],
             ["Confirm adequate, level ground bearing per the plan",
              "Fully extend and set outriggers/stabilizers on rated mats",
              "Level the crane within the manufacturer's tolerance"],
             "Hard hat, hi-vis, gloves, boots"),
            ("Assemble / disassemble (A/D)",
             ["Struck-by/caught during A/D", "Falls", "Boom collapse"],
             ["A/D director supervises per manufacturer procedures",
              "Use fall protection for elevated A/D work",
              "Keep personnel clear of pinch points and suspended components"],
             "Harness where required, hard hat, gloves"),
        ]),
        ("3", "Power Line Clearance", [
            ("Prevent power line contact",
             ["Electrocution from boom/load contact"],
             ["Maintain the minimum approach distance for the voltage",
              "Use dedicated spotter, range-limiting devices, or de-energize",
              "Assume all lines energized until confirmed otherwise"],
             "Standard PPE"),
        ]),
        ("4", "Rigging the Load", [
            ("Inspect &amp; select rigging",
             ["Rigging failure", "Wrong sling angle/capacity"],
             ["Inspect slings/shackles/hooks before use; remove defects",
              "Select rigging rated for the load and sling angle",
              "Verify hook latches and correct hitch for the load"],
             "Gloves, hard hat, hi-vis"),
            ("Attach &amp; balance the load",
             ["Load shift/drop", "Pinch points", "Tag line entanglement"],
             ["Rig to the center of gravity; verify balance with a trial lift",
              "Attach tag lines to control rotation; keep hands clear",
              "Confirm the load is free before the lift"],
             "Gloves, hard hat, hi-vis, boots"),
        ]),
        ("5", "Lifting &amp; Load Movement", [
            ("Hoist and move the load",
             ["Struck-by swinging load", "Two-blocking", "Caught in swing radius/counterweight"],
             ["No personnel under or in the path of the load",
              "Barricade the counterweight swing radius",
              "Anti-two-block functional; smooth, controlled movements on signal"],
             "Hard hat, hi-vis, gloves, boots"),
            ("Land &amp; release the load",
             ["Load tip/roll on landing", "Pinch points during release"],
             ["Land on stable, prepared blocking",
              "Keep hands clear; release rigging only when stable",
              "Maintain signal-person control throughout"],
             "Gloves, hard hat, hi-vis, boots"),
        ]),
        ("6", "Adverse Conditions &amp; Shutdown", [
            ("Monitor weather &amp; secure the crane",
             ["Wind-induced instability", "Uncontrolled movement when idle"],
             ["Suspend lifts in high wind/poor visibility per the plan",
              "Secure the load/boom and set controls before leaving",
              "Follow shutdown/securing procedure at end of shift"],
             "Standard PPE"),
        ]),
    ],
}

# ── Confined Spaces in Construction (29 CFR 1926 Subpart AA) ─────────────────
JHA_LIBRARY["29-cfr-1926-subpart-aa-1926-1200-1213-confined-spaces-in-construction"] = {
    "subtitle": "Permit-Required Confined Space Entry (Construction)",
    "legend": [
        ("Atmospheric", "Oxygen deficiency/enrichment, toxic gas (H2S, CO), flammable vapors."),
        ("Engulfment", "Liquids or fine solids that can bury or suffocate."),
        ("Configuration / Entrapment", "Converging walls, sloping floors, inwardly tapering spaces."),
        ("Physical", "Mechanical/electrical energy, moving parts, temperature extremes."),
        ("Falls", "Falls entering/exiting through vertical openings."),
        ("Rescue", "Delayed or improvised rescue; rescuer becoming a victim."),
    ],
    "jobs": [
        ("1", "Identify &amp; Evaluate Confined Spaces", [
            ("Identify and classify the space",
             ["Unrecognized permit-required space", "Unknown hazards"],
             ["Competent person identifies confined spaces and hazards",
              "Classify as permit-required (PRCS) where hazards exist",
              "Post/label spaces; coordinate with the controlling contractor"],
             "Standard PPE"),
        ]),
        ("2", "Permit &amp; Isolation", [
            ("Complete the entry permit",
             ["Entry without authorization", "Uncontrolled hazards"],
             ["Entry supervisor completes and signs the permit before entry",
              "Verify all conditions/equipment listed on the permit",
              "Permit posted at entry; canceled when the job ends or on hazard change"],
             "Standard PPE"),
            ("Isolate energy &amp; material inflow",
             ["Release of hazardous energy/material during entry"],
             ["Lock/tag/blank/blind lines feeding the space",
              "Isolate mechanical and electrical energy sources",
              "Drain, purge, and secure the space before entry"],
             "LOTO devices, gloves, standard PPE"),
        ]),
        ("3", "Atmospheric Testing &amp; Ventilation", [
            ("Test the atmosphere",
             ["Oxygen deficiency/enrichment", "Toxic/flammable atmosphere"],
             ["Test O2, LEL, then toxics (H2S/CO) with a calibrated meter",
              "Test before entry and continuously monitor during occupancy",
              "Do not enter until readings are within acceptable limits"],
             "Calibrated 4-gas monitor"),
            ("Ventilate the space",
             ["Accumulation of hazardous atmosphere"],
             ["Provide continuous mechanical ventilation",
              "Retest after ventilating and periodically thereafter",
              "Position blowers to avoid drawing in exhaust/contaminants"],
             "Ventilation equipment"),
        ]),
        ("4", "Entry Setup &amp; Roles", [
            ("Assign attendant, entrant &amp; supervisor",
             ["No attendant / unmonitored entry", "Untrained personnel"],
             ["Assign a trained attendant stationed outside at all times",
              "Confirm entrants and supervisor understand duties and signals",
              "Maintain continuous entrant-attendant communication"],
             "Standard PPE, communication device"),
            ("Set up access &amp; retrieval",
             ["Falls through vertical openings", "No retrieval capability"],
             ["Use tripod/davit with retrieval line and full-body harness",
              "Provide safe access (ladder/stairs) inspected before use",
              "Attach retrieval line unless it creates a greater hazard"],
             "Full-body harness, retrieval line, hard hat"),
        ]),
        ("5", "Entry &amp; Work", [
            ("Enter and perform work",
             ["Atmospheric change", "Engulfment", "Contact with energy/moving parts", "Heat stress"],
             ["Enter only with a valid permit and acceptable readings",
              "Maintain continuous monitoring and ventilation",
              "Evacuate immediately on alarm, symptoms, or attendant order",
              "Manage heat/cold exposure; rotate entrants"],
             "Harness, monitor, task-specific PPE"),
            ("Attendant monitoring",
             ["Loss of contact", "Unauthorized entry", "Delayed evacuation"],
             ["Attendant monitors conditions and entrants continuously",
              "Order evacuation on any warning sign; never enter to rescue",
              "Keep unauthorized persons out; log entrants in/out"],
             "Communication device, standard PPE"),
        ]),
        ("6", "Rescue &amp; Emergency", [
            ("Perform non-entry / summon rescue",
             ["Rescuer becoming a victim", "Delayed rescue"],
             ["Use non-entry (retrieval-line) rescue whenever possible",
              "Summon the pre-arranged trained rescue service on emergency",
              "Never perform unplanned entry rescue; follow the rescue plan"],
             "Retrieval system, rescue kit, SCBA if required"),
        ]),
    ],
}


# ── Construction category — remaining field programs ──────────────────────────
JHA_LIBRARY["29-cfr-1926-subpart-r-1926-750-761-steel-erection"] = {
    "subtitle": "Structural Steel Erection Operations",
    "legend": [
        ("Falls", "Falls from connecting points, leading edges, and open-web steel."),
        ("Struck-By / Caught-Between", "Swinging loads, moving steel, crane counterweights."),
        ("Structural Collapse", "Unstable columns/beams before bracing; overload of connections."),
        ("Rigging / Hoisting", "Dropped loads, rigging failure, load contact."),
        ("Electrical", "Contact with overhead power lines during hoisting."),
    ],
    "jobs": [
        ("1", "Site Layout &amp; Pre-Planning", [
            ("Verify site readiness &amp; approvals",
             ["Inadequate foundations", "Unstable ground for cranes"],
             ["Obtain written notification that concrete/anchor bolts have cured to strength",
              "Confirm adequate access roads and firm, level crane setup areas",
              "Hold a pre-erection planning meeting covering the erection sequence"],
             "Hard hat, safety glasses, gloves, high-visibility vest"),
        ]),
        ("2", "Crane &amp; Hoisting Setup", [
            ("Set up and stabilize the crane",
             ["Crane tip-over", "Contact with overhead power lines"],
             ["Set crane on firm level ground with mats and fully extended outriggers",
              "Maintain minimum clearance from overhead power lines; use a spotter",
              "Verify load charts, rated capacity, and inspection before lifting"],
             "Hard hat, safety glasses, gloves, high-visibility vest"),
        ]),
        ("3", "Rigging &amp; Hoisting Steel", [
            ("Rig and hoist structural members",
             ["Dropped load", "Rigging failure", "Struck by swinging load"],
             ["Use a qualified rigger and inspected slings/shackles rated for the load",
              "Use tag lines to control the load; keep workers clear of the swing radius",
              "Do not walk or work under a suspended load"],
             "Hard hat, safety glasses, gloves, high-visibility vest"),
        ]),
        ("4", "Connecting &amp; Bolting-Up", [
            ("Make initial connections",
             ["Falls from connecting points", "Struck by member during landing"],
             ["Connectors tie off at heights &gt; 15 ft (and per employer policy &ge; 6 ft)",
              "Install a minimum of two bolts per connection before releasing the load",
              "Use controlled load-landing; guide members with tag lines"],
             "Full-body harness, positioning lanyard, hard hat, gloves"),
            ("Torque and final bolt-up",
             ["Falls", "Pinch points", "Dropped tools"],
             ["Work from stable footing, boom lift, or floated platform tied off",
              "Secure/tether tools to prevent dropped objects",
              "Verify bolt pattern and torque per the erection drawings"],
             "Full-body harness, hard hat, gloves, tool tethers"),
        ]),
        ("5", "Decking &amp; Metal Deck Installation", [
            ("Install and secure metal decking",
             ["Falls through openings/leading edge", "Slips on loose deck", "Wind"],
             ["Establish a controlled decking zone; guard/cover openings immediately",
              "Secure deck bundles; do not stack near leading edge",
              "Tack/fasten deck as laid; stop work in high winds"],
             "Full-body harness, hard hat, gloves"),
        ]),
        ("6", "Column &amp; Beam Stability / Guying", [
            ("Brace and stabilize erected steel",
             ["Structural collapse", "Overload before bracing complete"],
             ["Guy or brace columns until permanent bracing is installed",
              "Do not remove temporary bracing until the frame is self-supporting",
              "Competent person inspects stability before releasing the crane"],
             "Hard hat, safety glasses, gloves, high-visibility vest"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1926-subpart-x-1926-1053-1060-ladders-construction"] = {
    "subtitle": "Portable &amp; Fixed Ladder Use",
    "legend": [
        ("Falls", "Falls from ladders, overreaching, sliding/tipping ladders."),
        ("Electrical", "Metal ladder contact with energized equipment/overhead lines."),
        ("Struck-By", "Dropped tools, falling objects near the ladder base."),
        ("Ergonomic", "Awkward posture, overexertion carrying/positioning ladders."),
    ],
    "jobs": [
        ("1", "Ladder Selection &amp; Inspection", [
            ("Select and inspect the ladder",
             ["Defective ladder", "Wrong ladder for the task", "Conductive ladder near power"],
             ["Select a ladder rated for the load and duty; correct length for the task",
              "Use non-conductive (fiberglass) ladders near electrical hazards",
              "Inspect for cracked rails, missing feet, damaged rungs before each use; remove defective ladders"],
             "Hard hat, safety glasses, gloves"),
        ]),
        ("2", "Setup &amp; Positioning", [
            ("Position and secure the ladder",
             ["Ladder slides/tips", "Set up on unstable ground"],
             ["Set extension ladders at a 4:1 pitch on firm, level footing",
              "Extend &ge; 3 ft above the landing; secure top and bottom or have it held",
              "Do not place in front of doorways unless barricaded/locked"],
             "Hard hat, safety glasses, gloves"),
        ]),
        ("3", "Climbing &amp; Descending", [
            ("Climb and descend safely",
             ["Falls", "Loss of grip", "Overloading"],
             ["Face the ladder and maintain three points of contact",
              "One person on the ladder at a time; do not carry loads by hand while climbing",
              "Raise/lower tools and materials with a hand line"],
             "Hard hat, safety glasses, gloves, non-slip footwear"),
        ]),
        ("4", "Working from the Ladder", [
            ("Perform work from the ladder",
             ["Overreaching/tipping", "Dropped objects", "Electrical contact"],
             ["Keep the body centered between the rails; do not overreach",
              "Do not stand on the top two rungs of a stepladder",
              "Keep clearances from energized parts; use a working platform for extended tasks"],
             "Hard hat, safety glasses, gloves, tool lanyards"),
        ]),
        ("5", "Storage &amp; Removal from Service", [
            ("Store or tag out ladders",
             ["Reuse of a damaged ladder", "Struck-by during handling"],
             ["Tag and remove defective ladders from service immediately",
              "Store ladders on racks, supported to prevent sagging/warping",
              "Use safe carrying techniques; watch for overhead lines when carrying"],
             "Gloves, hard hat, safety glasses"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1926-404-b-1-assured-equipment-grounding-conductor-program-gfci-temporary-power"] = {
    "subtitle": "Temporary Electrical Power &amp; GFCI Protection",
    "legend": [
        ("Electrical", "Shock and electrocution from faults, damaged cords, wet conditions."),
        ("Fire", "Overloaded circuits, damaged insulation, improper connections."),
        ("Struck-By / Trip", "Cords across walkways, energized equipment."),
    ],
    "jobs": [
        ("1", "Temporary Power Setup", [
            ("Install temporary power distribution",
             ["Shock/electrocution", "Improper grounding", "Overload/fire"],
             ["Qualified person installs distribution per NEC and manufacturer specs",
              "Verify equipment grounding conductor is continuous and bonded",
              "Protect boxes/panels from weather; size circuits for the load"],
             "Voltage-rated gloves, safety glasses, hard hat"),
        ]),
        ("2", "GFCI Protection / AEGCP Testing", [
            ("Provide fault protection",
             ["Ground fault", "Undetected damaged conductor"],
             ["Provide GFCI protection on all 120V, single-phase, 15/20A receptacles",
              "Where GFCI is not feasible, implement the Assured Equipment Grounding Conductor Program",
              "Test equipment grounding conductors on the required schedule and tag tested equipment"],
             "Voltage-rated gloves, safety glasses"),
        ]),
        ("3", "Cord &amp; Tool Inspection", [
            ("Inspect cords and tools before use",
             ["Damaged insulation", "Missing ground pin", "Shock"],
             ["Inspect cords/tools for damage, cut insulation, missing ground pin before each use",
              "Remove damaged equipment from service and tag it",
              "Use only cords rated for hard/extra-hard service outdoors"],
             "Safety glasses, gloves"),
        ]),
        ("4", "Use of Temporary Power", [
            ("Operate tools on temporary power",
             ["Wet-condition shock", "Trip hazards", "Overload"],
             ["Keep connections out of standing water; use covered/weatherproof devices",
              "Route cords to avoid walkways/traffic; elevate or protect crossings",
              "Do not daisy-chain power strips or overload circuits"],
             "Voltage-rated gloves as required, safety glasses, non-slip footwear"),
        ]),
        ("5", "Maintenance &amp; Removal", [
            ("De-energize and remove temporary power",
             ["Contact with energized parts", "Stored energy"],
             ["De-energize and lock out before servicing distribution equipment",
              "Verify absence of voltage before working on conductors",
              "Remove temporary wiring promptly when no longer needed"],
             "Voltage-rated gloves, safety glasses, hard hat"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1926-1153-respirable-crystalline-silica-written-exposure-control-plan-construction"] = {
    "subtitle": "Respirable Crystalline Silica Exposure Control",
    "legend": [
        ("Respiratory", "Inhalation of respirable crystalline silica dust (silicosis, cancer)."),
        ("Environmental", "Visible dust migration to other workers and the public."),
        ("Housekeeping", "Accumulated dust re-entrained by cleaning/traffic."),
        ("Manual / Ergonomic", "Handling of water/vacuum equipment and materials."),
    ],
    "jobs": [
        ("1", "Task Assessment &amp; Table 1 Selection", [
            ("Identify silica-generating tasks",
             ["Unrecognized exposure", "Wrong control method"],
             ["Competent person identifies tasks that disturb silica-containing material",
              "Select the matching Table 1 control (or perform exposure assessment)",
              "Implement the written exposure control plan for the task"],
             "Half-mask respirator as specified, safety glasses, gloves"),
        ]),
        ("2", "Engineering Controls (Water / Vacuum)", [
            ("Set up dust controls",
             ["Dry cutting dust", "Ineffective/failed control"],
             ["Use integrated water delivery or on-tool HEPA dust collection per Table 1",
              "Verify adequate water flow / vacuum airflow before starting",
              "Maintain tools and filters; do not defeat the control"],
             "Respirator per Table 1, safety glasses, gloves"),
        ]),
        ("3", "Cutting / Grinding / Drilling", [
            ("Perform dust-generating work",
             ["Respirable silica inhalation", "Dust exposure to nearby workers"],
             ["Operate with the engineering control running continuously",
              "Position workers upwind; restrict/limit access to the dust area",
              "Rotate tasks and limit duration per the exposure control plan"],
             "Respirator per Table 1, safety glasses, gloves, hearing protection as needed"),
        ]),
        ("4", "Respiratory Protection &amp; Housekeeping", [
            ("Use respirators and control dust",
             ["Improper respirator use", "Re-entrained dust"],
             ["Use respirators specified for the task; users fit-tested and medically cleared",
              "Never dry sweep or use compressed air on silica dust",
              "Use HEPA vacuuming or wet methods for cleanup"],
             "Fit-tested respirator, safety glasses, gloves"),
        ]),
        ("5", "Cleanup &amp; Waste", [
            ("Clean the area and manage waste",
             ["Dust exposure during cleanup", "Contaminated clothing"],
             ["HEPA-vacuum or wet-wipe surfaces and equipment",
              "Contain and dispose of silica waste to prevent re-suspension",
              "Provide wash facilities; do not carry dust home on clothing"],
             "Respirator as needed, gloves, safety glasses"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1926-62-lead-in-construction"] = {
    "subtitle": "Lead Exposure Control in Construction",
    "legend": [
        ("Respiratory / Ingestion", "Inhalation of lead fume/dust; hand-to-mouth ingestion."),
        ("Environmental", "Lead dust migration and contamination of soil/adjacent areas."),
        ("Housekeeping", "Accumulated lead dust re-entrained by traffic/cleaning."),
        ("Waste", "Hazardous lead-containing waste handling and disposal."),
    ],
    "jobs": [
        ("1", "Exposure Assessment", [
            ("Determine lead exposure",
             ["Unknown/underestimated exposure", "Unprotected work"],
             ["Assume presumptive exposure for listed trigger tasks until assessed",
              "Conduct exposure monitoring; apply the initial protective measures",
              "Implement the written compliance program for the task"],
             "Respirator per assessment, disposable coveralls, gloves"),
        ]),
        ("2", "Engineering &amp; Work Practice Controls", [
            ("Set up controls before disturbing lead",
             ["Airborne lead fume/dust", "Spread of contamination"],
             ["Use local exhaust ventilation, wet methods, or containment as feasible",
              "Establish a regulated area with warning signs and limited access",
              "Prohibit eating, drinking, and smoking in the work area"],
             "Respirator, disposable coveralls, gloves, safety glasses"),
        ]),
        ("3", "Disturbing Lead-Containing Material", [
            ("Perform cutting/grinding/torch/abrasive work",
             ["High lead fume from heat/abrasion", "Fire from torch work"],
             ["Prefer methods that minimize fume/dust; avoid dry abrasive where possible",
              "Provide LEV at the point of generation; monitor exposures",
              "Control ignition sources for torch/heat tasks"],
             "Respirator per exposure, coveralls, gloves, face shield as needed"),
        ]),
        ("4", "Hygiene &amp; Decontamination", [
            ("Decontaminate personnel",
             ["Ingestion of lead", "Take-home contamination"],
             ["Provide change areas, hand/face wash, and showers where required",
              "Remove and store contaminated clothing separately; do not blow off with air",
              "Wash before eating; leave work clothes on site for laundering"],
             "Disposable coveralls, gloves"),
        ]),
        ("5", "Waste Handling &amp; Cleanup", [
            ("Clean the area and manage lead waste",
             ["Re-entrained dust", "Improper waste disposal"],
             ["HEPA-vacuum and wet-clean; never dry sweep or use compressed air",
              "Containerize and label lead waste; characterize and dispose per RCRA",
              "Verify clearance before releasing the area"],
             "Respirator as needed, gloves, coveralls"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1926-1101-asbestos-in-construction"] = {
    "subtitle": "Asbestos Disturbance &amp; Abatement",
    "legend": [
        ("Respiratory", "Inhalation of asbestos fibers (asbestosis, mesothelioma, cancer)."),
        ("Regulated Area", "Uncontrolled fiber release beyond containment."),
        ("Housekeeping", "Re-entrained fibers from dry debris/clothing."),
        ("Waste", "Improperly contained asbestos-containing waste material."),
    ],
    "jobs": [
        ("1", "Survey &amp; Classification", [
            ("Identify and classify the work",
             ["Unknown asbestos-containing material", "Wrong class of work"],
             ["Review the asbestos survey; treat suspect materials as ACM until tested",
              "Classify the work (Class I&ndash;IV) and assign a competent person",
              "Assume/monitor exposures and apply the required controls for the class"],
             "Respirator per class, disposable coveralls, gloves"),
        ]),
        ("2", "Regulated Area Setup / Containment", [
            ("Establish containment and access control",
             ["Fiber release to adjacent areas", "Unauthorized entry"],
             ["Post the regulated area; restrict to authorized, trained workers",
              "Establish negative-pressure enclosure and decon unit where required",
              "Use critical barriers and HEPA filtration as specified"],
             "Respirator, coveralls, gloves, boots"),
        ]),
        ("3", "Removal / Disturbance Work", [
            ("Remove or disturb ACM",
             ["Airborne asbestos fibers", "Dry removal releasing fibers"],
             ["Wet the material; use amended water and HEPA-filtered tools",
              "Avoid dry removal, breaking, or aggressive methods",
              "Bag material at the point of removal; minimize handling"],
             "Respirator per class, coveralls, gloves"),
        ]),
        ("4", "Decontamination", [
            ("Decontaminate workers and equipment",
             ["Fiber take-home", "Cross-contamination"],
             ["Use the three-stage decon procedure; HEPA-vacuum suits before removal",
              "Do not remove contaminated PPE outside the decon area",
              "Shower where required; leave contaminated clothing in containment"],
             "Respirator until decon complete, disposable coveralls, gloves"),
        ]),
        ("5", "Waste Disposal &amp; Clearance", [
            ("Package waste and clear the area",
             ["Release from torn bags", "Premature re-occupancy"],
             ["Double-bag/label asbestos waste in leak-tight containers; dispose at an approved site",
              "Perform visual inspection and clearance air sampling where required",
              "Release the area only after clearance criteria are met"],
             "Respirator, coveralls, gloves"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1926-subpart-t-1926-850-860-demolition"] = {
    "subtitle": "Demolition Operations",
    "legend": [
        ("Structural Collapse", "Premature/uncontrolled collapse of walls, floors, structures."),
        ("Falls", "Falls through openings, leading edges, unstable surfaces."),
        ("Struck-By / Caught-Between", "Falling debris, moving equipment, flying material."),
        ("Utilities / Energy", "Live electrical, gas, water, and stored energy."),
        ("Atmospheric / Dust", "Silica, asbestos, lead, hazardous atmospheres."),
        ("Fire", "Torch/hot work, residual flammables."),
    ],
    "jobs": [
        ("1", "Engineering Survey &amp; Utility Isolation", [
            ("Survey the structure and isolate utilities",
             ["Unexpected collapse", "Live utilities", "Hidden hazards"],
             ["Competent person completes a written engineering survey before demolition",
              "Locate, shut off, cap, and control all utilities before work begins",
              "Identify and abate hazardous materials (asbestos/lead) first"],
             "Hard hat, safety glasses, gloves, high-visibility vest"),
        ]),
        ("2", "Preparation &amp; Access", [
            ("Prepare the site and access routes",
             ["Falls into openings", "Unauthorized entry", "Struck-by"],
             ["Barricade the demolition zone; control public and worker access",
              "Cover/guard floor openings and shaft openings not in use",
              "Provide safe access/egress; brace weakened structures"],
             "Hard hat, safety glasses, gloves, fall protection as needed"),
        ]),
        ("3", "Manual / Mechanical Demolition", [
            ("Demolish walls, floors, and structures",
             ["Uncontrolled collapse", "Struck by falling material", "Equipment rollover"],
             ["Demolish in a planned top-down sequence; do not undermine supports",
              "Keep workers clear of equipment swing and drop zones; use spotters",
              "Do not overload floors with debris or equipment"],
             "Hard hat, safety glasses, gloves, hearing protection, high-visibility vest"),
        ]),
        ("4", "Debris Handling &amp; Chutes", [
            ("Remove debris safely",
             ["Falling debris", "Dust exposure", "Chute failure"],
             ["Use enclosed chutes for debris dropped more than 20 ft; gate the discharge",
              "Wet debris to control dust; keep the drop area barricaded",
              "Do not exceed floor load limits with accumulated debris"],
             "Hard hat, safety glasses, gloves, respirator/dust mask as needed"),
        ]),
        ("5", "Below-Grade / Selective Demolition", [
            ("Perform below-grade or selective work",
             ["Wall/embankment collapse", "Atmospheric hazards", "Confined space"],
             ["Shore or brace walls and excavations per the survey/competent person",
              "Test atmospheres in enclosed/below-grade areas before entry",
              "Apply confined space procedures where applicable"],
             "Hard hat, gloves, gas monitor as needed, fall/entry protection"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1926-95-106-personal-protective-equipment-construction"] = {
    "subtitle": "Personal Protective Equipment Selection &amp; Use",
    "legend": [
        ("Head / Eye / Face", "Impact, penetration, flying particles, splash, arc."),
        ("Hand / Foot", "Cuts, punctures, crushing, chemical, electrical."),
        ("Hearing", "Noise exposure above action levels."),
        ("Respiratory", "Dusts, fumes, vapors, oxygen-deficient atmospheres."),
        ("Fall Protection", "Falls to a lower level."),
    ],
    "jobs": [
        ("1", "Hazard Assessment &amp; PPE Selection", [
            ("Assess hazards and select PPE",
             ["Wrong or missing PPE", "Unassessed hazards"],
             ["Competent person performs a documented PPE hazard assessment per task",
              "Select PPE rated for the specific hazard and properly fitted",
              "Train workers on selection, use, limitations, and care"],
             "Base PPE: hard hat, safety glasses, gloves, safety-toe boots"),
        ]),
        ("2", "Head, Eye &amp; Face Protection", [
            ("Protect the head, eyes, and face",
             ["Impact/penetration", "Flying particles", "Chemical splash", "Arc flash"],
             ["Wear ANSI-rated hard hats where overhead/impact hazards exist",
              "Wear ANSI Z87 eye protection; add face shields for grinding/chemical/arc tasks",
              "Match lens/shade to the task (welding, cutting, chemical)"],
             "Hard hat, Z87 safety glasses/goggles, face shield as needed"),
        ]),
        ("3", "Hand &amp; Foot Protection", [
            ("Protect hands and feet",
             ["Cuts/punctures", "Chemical contact", "Crushing", "Electrical"],
             ["Select cut/chemical/voltage-rated gloves matched to the hazard",
              "Wear safety-toe (and metatarsal/EH-rated where needed) footwear",
              "Inspect gloves/boots before use; replace when damaged"],
             "Task-rated gloves, safety-toe boots"),
        ]),
        ("4", "Hearing &amp; Respiratory Protection", [
            ("Protect hearing and respiratory system",
             ["Noise-induced hearing loss", "Inhalation of dusts/fumes/vapors"],
             ["Provide hearing protection where noise meets/exceeds the action level",
              "Use respirators only under a respiratory protection program (fit-test, medical, training)",
              "Match respirator/cartridge to the contaminant and concentration"],
             "Hearing protection, fit-tested respirator as required"),
        ]),
        ("5", "Inspection, Care &amp; Replacement", [
            ("Maintain PPE",
             ["Use of defective PPE", "Degraded protection"],
             ["Inspect PPE before each use; remove damaged/expired items from service",
              "Clean, store, and maintain PPE per the manufacturer",
              "Replace PPE that no longer provides the intended protection"],
             "Applicable task PPE"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1926-350-354-welding-cutting-brazing-hot-work-construction"] = {
    "subtitle": "Welding, Cutting &amp; Hot Work",
    "legend": [
        ("Fire / Explosion", "Sparks, slag, hot work near combustibles/flammables."),
        ("Burns", "Contact with hot metal, sparks, molten slag, UV."),
        ("Fumes / Atmospheric", "Metal fumes, shielding gases, oxygen displacement."),
        ("Electrical", "Shock from welding circuits and damaged leads."),
        ("Radiation", "Arc UV/IR affecting eyes and skin."),
        ("Compressed Gas", "Cylinder rupture, uncontrolled release, backflash."),
    ],
    "jobs": [
        ("1", "Hot Work Permit &amp; Fire Watch", [
            ("Authorize the hot work",
             ["Fire from unpermitted work", "No fire watch"],
             ["Complete a hot work permit; verify no safer alternative exists",
              "Assign a trained fire watch with an extinguisher during and 30+ min after",
              "Test for flammable atmospheres before starting where required"],
             "Welding PPE, fire extinguisher on hand"),
        ]),
        ("2", "Area Prep &amp; Fire Prevention", [
            ("Prepare the work area",
             ["Ignition of combustibles", "Sparks to lower levels"],
             ["Remove or shield combustibles within 35 ft; cover openings and drains",
              "Use fire blankets/spark containment; wet down as appropriate",
              "Never perform hot work on containers that held flammables until made safe"],
             "Welding PPE, fire blankets"),
        ]),
        ("3", "Compressed Gas &amp; Equipment Setup", [
            ("Set up gas and welding equipment",
             ["Cylinder rupture", "Flashback", "Electrical shock"],
             ["Secure cylinders upright; caps on when moving; store fuel/oxygen apart",
              "Use flashback arrestors/check valves; inspect hoses and leads",
              "Ground the work; keep welding leads dry and undamaged"],
             "Welding gloves, safety glasses, leathers"),
        ]),
        ("4", "Welding / Cutting Operations", [
            ("Perform welding and cutting",
             ["Burns", "Arc radiation", "Sparks/slag", "Electric shock"],
             ["Wear the correct shade filter and full welding PPE",
              "Screen the arc to protect nearby workers",
              "Keep body/clothing dry; do not weld in wet conditions"],
             "Welding hood with correct shade, leathers, gloves, respirator as needed"),
        ]),
        ("5", "Ventilation &amp; Fume Control", [
            ("Control welding fumes",
             ["Metal fume inhalation", "Oxygen displacement in enclosed areas"],
             ["Provide local exhaust or general ventilation for the process/base metal",
              "Use respiratory protection for coated metals or confined areas",
              "Monitor atmospheres in enclosed/confined spaces"],
             "Fit-tested respirator as required, welding PPE"),
        ]),
        ("6", "Post-Work / Fire Watch", [
            ("Secure the area after hot work",
             ["Smoldering fire after work", "Hot metal contact"],
             ["Maintain fire watch for at least 30 minutes after completion",
              "Inspect the area and adjacent/opposite sides for smoldering",
              "Shut off/secure gas supplies and bleed lines"],
             "Fire extinguisher, welding gloves"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1926-400-449-electrical-safety-construction"] = {
    "subtitle": "Electrical Safety in Construction",
    "legend": [
        ("Shock / Electrocution", "Contact with energized conductors and equipment."),
        ("Arc Flash / Burn", "Arc blast, thermal burns from faults."),
        ("Fire", "Overloads, faults, improper wiring."),
        ("Overhead Lines", "Contact by equipment, materials, or workers."),
        ("Struck-By", "Blast pressure, ejected parts."),
    ],
    "jobs": [
        ("1", "Identify Energized Systems / Overhead Lines", [
            ("Identify electrical hazards",
             ["Unrecognized energized equipment", "Overhead power lines"],
             ["Identify and mark energized systems and overhead line locations",
              "Maintain required clearances from overhead lines; use spotters",
              "Only qualified persons work on/near exposed energized parts"],
             "Voltage-rated gloves, arc-rated clothing, hard hat, safety glasses"),
        ]),
        ("2", "De-energize &amp; Lockout/Tagout", [
            ("De-energize before work",
             ["Unexpected energization", "Stored energy"],
             ["De-energize and apply lockout/tagout to the energy source",
              "Test for absence of voltage with a rated tester before touching",
              "Discharge stored energy (capacitors); ground where required"],
             "Voltage-rated gloves, rated voltage tester, LOTO devices"),
        ]),
        ("3", "Temporary Wiring &amp; GFCI", [
            ("Manage temporary electrical supply",
             ["Ground fault", "Damaged cords", "Wet conditions"],
             ["Provide GFCI protection or implement the AEGCP",
              "Inspect cords/tools; remove damaged equipment from service",
              "Keep connections out of water; protect from damage/traffic"],
             "Voltage-rated gloves as needed, safety glasses"),
        ]),
        ("4", "Working On / Near Energized Equipment", [
            ("Perform energized work (only when justified)",
             ["Shock", "Arc flash/blast", "Burns"],
             ["Perform work de-energized unless infeasible and justified in writing",
              "Establish an arc-flash/shock boundary; use rated PPE and insulated tools",
              "Use an attendant and an emergency response plan for energized work"],
             "Arc-rated suit/face shield, voltage-rated gloves, insulated tools"),
        ]),
        ("5", "Restoration &amp; Verification", [
            ("Re-energize safely",
             ["Premature re-energization", "Faulty connection"],
             ["Verify work complete, tools/personnel clear before removing LOTO",
              "Reinstall guards/covers; test circuits before returning to service",
              "Only the authorized person removes their own lock/tag"],
             "Voltage-rated gloves, safety glasses"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1926-700-706-concrete-masonry-construction"] = {
    "subtitle": "Concrete &amp; Masonry Construction",
    "legend": [
        ("Struck-By / Caught-Between", "Formwork/shoring collapse, rebar, pump lines, block."),
        ("Chemical", "Wet concrete/mortar skin burns and eye injury."),
        ("Falls", "Falls from formwork, walls, and elevated placement."),
        ("Manual / Ergonomic", "Lifting rebar/block, awkward placement postures."),
        ("Environmental", "Silica dust from cutting block/concrete."),
    ],
    "jobs": [
        ("1", "Formwork &amp; Shoring Erection", [
            ("Erect formwork and shoring",
             ["Formwork/shoring collapse", "Falls", "Struck-by"],
             ["Erect per drawings designed/approved by a qualified person",
              "Brace and inspect shoring before and during placement",
              "Provide fall protection at elevated forms"],
             "Hard hat, safety glasses, gloves, fall protection as needed"),
        ]),
        ("2", "Reinforcing Steel (Rebar) Placement", [
            ("Place and tie rebar",
             ["Impalement on protruding rebar", "Cuts", "Ergonomic strain"],
             ["Cap or bend exposed rebar ends to prevent impalement",
              "Use proper lifting/team lifts; stage materials to reduce carrying",
              "Guard against falls onto vertical rebar"],
             "Hard hat, gloves, safety glasses, safety-toe boots"),
        ]),
        ("3", "Concrete Placement &amp; Pumping", [
            ("Place concrete",
             ["Skin/eye burns from wet concrete", "Pump line whip/blockage", "Struck-by bucket"],
             ["Wear waterproof gloves/boots; rinse skin contact immediately",
              "Secure pump lines; relieve blockages per procedure; keep clear of the discharge",
              "Communicate with the pump/crane operator; control the bucket"],
             "Rubber gloves, rubber boots, safety glasses/face shield, hard hat"),
        ]),
        ("4", "Formwork Stripping / Shore Removal", [
            ("Strip forms and remove shores",
             ["Premature removal collapse", "Falling forms", "Nail puncture"],
             ["Do not strip forms/shores until concrete reaches required strength",
              "Remove and stack forms in a controlled sequence; keep clear below",
              "Remove or bend over protruding nails immediately"],
             "Hard hat, gloves, safety glasses, safety-toe boots"),
        ]),
        ("5", "Masonry Wall Construction", [
            ("Build masonry walls",
             ["Wall collapse during construction", "Struck-by falling block", "Silica dust"],
             ["Establish a limited-access zone on unsupported walls per the standard",
              "Brace walls until permanently supported",
              "Use wet cutting/dust control when sawing block"],
             "Hard hat, gloves, safety glasses, dust protection as needed"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1926-900-914-blasting-use-of-explosives-construction"] = {
    "subtitle": "Blasting &amp; Use of Explosives",
    "legend": [
        ("Explosion / Detonation", "Premature or accidental detonation."),
        ("Fly Rock", "Ejected rock and debris from the blast."),
        ("Fire", "Ignition sources near explosives."),
        ("Atmospheric", "Toxic post-blast fumes in enclosed/below-grade areas."),
        ("Storage / Security", "Theft, unauthorized access, improper storage."),
    ],
    "jobs": [
        ("1", "Storage &amp; Transport of Explosives", [
            ("Store and move explosives",
             ["Accidental detonation", "Theft/unauthorized access"],
             ["Only a licensed/authorized blaster handles explosives",
              "Store in approved, locked magazines; keep detonators separate from explosives",
              "Transport in approved vehicles; maintain inventory and security"],
             "Cotton/anti-static clothing, no spark-producing items"),
        ]),
        ("2", "Loading the Blast Holes", [
            ("Load charges",
             ["Premature detonation", "Static/stray current ignition"],
             ["Stop all drilling within the blast area before loading",
              "Prohibit smoking, open flames, and radio transmitters near loading",
              "Use non-sparking tamping tools; follow the loading plan"],
             "Anti-static clothing, gloves, hard hat"),
        ]),
        ("3", "Connecting &amp; Wiring the Shot", [
            ("Connect the initiation system",
             ["Stray current initiation", "Faulty circuit"],
             ["Keep the circuit shunted/disconnected until ready to fire",
              "Protect against stray/static current; test the circuit with a blasting galvanometer",
              "Follow manufacturer instructions for the initiation system"],
             "Anti-static clothing, gloves"),
        ]),
        ("4", "Firing the Blast", [
            ("Clear the area and fire",
             ["Fly rock injury", "Personnel in blast zone", "Misfire"],
             ["Sound audible warning signals; verify all personnel are clear and sheltered",
              "Guard access roads and post guards at the danger zone",
              "Fire only on the blaster's command from a protected location"],
             "Hard hat, hearing protection, high-visibility vest"),
        ]),
        ("5", "Post-Blast Inspection &amp; Misfires", [
            ("Inspect after the blast",
             ["Toxic fumes", "Undetonated charges (misfires)"],
             ["Wait the required time; ventilate and test atmosphere before re-entry",
              "Only the blaster inspects; handle misfires per procedure",
              "Do not resume work until the area is declared safe"],
             "Gas monitor as needed, hard hat, gloves"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1926-950-968-power-transmission-distribution-construction"] = {
    "subtitle": "Power Line Construction &amp; Maintenance",
    "legend": [
        ("Electrical", "Contact, induced voltage, step/touch potential, flashover."),
        ("Falls", "Falls from poles, structures, and aerial devices."),
        ("Struck-By", "Falling conductors, hardware, and equipment."),
        ("Rigging / Hoisting", "Dropped loads during stringing and framing."),
        ("Environmental", "Weather, heat/cold, remote-site response."),
    ],
    "jobs": [
        ("1", "Job Briefing &amp; Clearances", [
            ("Hold the job briefing",
             ["Unrecognized hazards", "Working without a clearance"],
             ["Conduct a documented job briefing covering hazards and procedures",
              "Obtain and verify clearances/switching orders before work",
              "Confirm minimum approach distances for the voltage"],
             "Arc-rated clothing, voltage-rated gloves, hard hat"),
        ]),
        ("2", "De-energize, Test &amp; Ground", [
            ("Isolate and ground the line",
             ["Contact with energized line", "Induced voltage", "Re-energization"],
             ["De-energize, test for absence of voltage, and apply protective grounds",
              "Treat lines as energized until tested and grounded",
              "Install grounds to control induced and fault voltage at the work site"],
             "Voltage-rated gloves/sleeves, grounding cluster, rated tester"),
        ]),
        ("3", "Pole / Structure Climbing &amp; Access", [
            ("Climb poles and structures",
             ["Falls", "Pole failure", "Aerial device tip-over"],
             ["Inspect and test poles for integrity before climbing",
              "Use fall protection (climbing system/harness) continuously at height",
              "Set aerial devices on firm level ground with outriggers; maintain line clearance"],
             "Full-body harness, climbing equipment, hard hat, voltage-rated gloves"),
        ]),
        ("4", "Working On / Near Energized Lines", [
            ("Perform energized-line work (when required)",
             ["Electrocution", "Flashover", "Burns"],
             ["Only qualified line workers using rated cover-up and live-line tools",
              "Maintain minimum approach distances; insulate/isolate the worker",
              "Use the barehand or hot-stick method per the approved procedure"],
             "Voltage-rated gloves/sleeves, arc-rated suit, insulated tools/covers"),
        ]),
        ("5", "Stringing &amp; Rigging Conductors", [
            ("String and sag conductors",
             ["Dropped/backlashing conductor", "Struck-by", "Contact with adjacent energized lines"],
             ["Use rated pulling/tensioning equipment and communication signals",
              "Keep clear of conductors under tension; use guard structures at crossings",
              "Ground conductors and equipment to control induced voltage"],
             "Hard hat, voltage-rated gloves, high-visibility vest, gloves"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1926-52-occupational-noise-exposure-construction"] = {
    "subtitle": "Occupational Noise Exposure Control",
    "legend": [
        ("Noise / Hearing", "Exposure above permissible limits causing hearing loss."),
        ("Communication", "Masking of alarms, signals, and warnings by noise."),
        ("Environmental", "High-noise equipment and enclosed reflective areas."),
    ],
    "jobs": [
        ("1", "Noise Exposure Assessment", [
            ("Assess noise exposure",
             ["Unrecognized overexposure", "Unprotected workers"],
             ["Identify high-noise tasks/equipment and monitor exposure levels",
              "Compare exposures to permissible/action levels",
              "Establish the hearing conservation measures for affected work"],
             "Hearing protection, safety glasses, hard hat"),
        ]),
        ("2", "Engineering &amp; Administrative Controls", [
            ("Reduce noise at the source",
             ["Continued overexposure", "Ineffective controls"],
             ["Apply engineering controls (mufflers, enclosures, maintenance) where feasible",
              "Use administrative controls: rotate tasks, limit exposure time, distance",
              "Post high-noise areas and restrict unnecessary access"],
             "Hearing protection, standard PPE"),
        ]),
        ("3", "Hearing Protection Use", [
            ("Use hearing protection",
             ["Inadequate attenuation", "Improper fit/use"],
             ["Provide hearing protectors with adequate NRR for the exposure",
              "Train workers on fit and use; double protection for very high noise",
              "Replace worn/damaged protectors"],
             "Earplugs and/or earmuffs, standard PPE"),
        ]),
        ("4", "Audiometric Monitoring &amp; Training", [
            ("Monitor hearing and train",
             ["Undetected hearing shift", "Untrained workers"],
             ["Provide baseline/annual audiograms for exposed workers where required",
              "Train on noise hazards, protector use, and program requirements",
              "Follow up on standard threshold shifts"],
             "Hearing protection as required"),
        ]),
    ],
}


# ── General Industry category (29 CFR 1910) — field programs ───────────────────
JHA_LIBRARY["29-cfr-1910-95-occupational-noise-exposure-hearing-conservation-program"] = {
    "subtitle": "Hearing Conservation Program Operations",
    "legend": [
        ("Noise / Hearing", "Exposure at or above the action level causing hearing loss."),
        ("Communication", "Masking of alarms and warning signals."),
        ("Environmental", "High-noise machinery and reflective enclosed areas."),
    ],
    "jobs": [
        ("1", "Noise Monitoring &amp; Enrollment", [
            ("Identify and monitor noise exposure",
             ["Unrecognized overexposure", "Workers not enrolled"],
             ["Conduct noise monitoring to identify exposures at/above the action level",
              "Enroll affected employees in the hearing conservation program",
              "Notify employees of their exposure results"],
             "Hearing protection, standard PPE"),
        ]),
        ("2", "Engineering &amp; Administrative Controls", [
            ("Reduce noise exposure",
             ["Continued overexposure", "Ineffective controls"],
             ["Apply feasible engineering controls (enclosures, maintenance, damping)",
              "Use administrative controls (rotation, scheduling, distance)",
              "Post high-noise areas and limit access"],
             "Hearing protection, standard PPE"),
        ]),
        ("3", "Hearing Protector Use &amp; Fit", [
            ("Provide and fit protectors",
             ["Inadequate attenuation", "Improper fit"],
             ["Offer a selection of protectors with adequate NRR",
              "Train on insertion/fit; use dual protection for very high noise",
              "Replace worn or damaged protectors"],
             "Earplugs and/or earmuffs"),
        ]),
        ("4", "Audiometric Testing &amp; Training", [
            ("Test hearing and train",
             ["Undetected threshold shift", "Untrained workers"],
             ["Provide baseline and annual audiograms; evaluate standard threshold shifts",
              "Train annually on noise hazards, protectors, and testing",
              "Follow up with re-fit or medical referral on confirmed shifts"],
             "Hearing protection"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1910-120-hazardous-waste-operations-and-emergency-response-hazwoper"] = {
    "subtitle": "Hazardous Waste Operations &amp; Emergency Response",
    "legend": [
        ("Chemical", "Contact/inhalation of hazardous substances and unknowns."),
        ("Atmospheric", "Oxygen deficiency, toxic/flammable atmospheres."),
        ("Fire / Explosion", "Ignition of flammable/reactive materials."),
        ("Physical", "Heat stress in PPE, drum handling, slips/trips."),
        ("Environmental", "Release/spread of contamination."),
    ],
    "jobs": [
        ("1", "Site Characterization &amp; Planning", [
            ("Characterize the site and plan",
             ["Unknown hazards", "Inadequate protection"],
             ["Conduct site characterization; identify substances and hazards",
              "Develop the site safety and health plan and select PPE levels (A&ndash;D)",
              "Only trained (HAZWOPER-certified) personnel perform the work"],
             "PPE level per hazard assessment, air monitoring"),
        ]),
        ("2", "Zone Setup &amp; Air Monitoring", [
            ("Establish work zones and monitor air",
             ["Contamination spread", "Undetected atmosphere"],
             ["Establish exclusion, contamination reduction, and support zones",
              "Continuously monitor the atmosphere (O2, LEL, toxics)",
              "Control entry/exit; restrict access to trained personnel"],
             "Level-appropriate PPE, calibrated air monitor"),
        ]),
        ("3", "Drum &amp; Container Handling", [
            ("Handle drums and containers",
             ["Release/spill", "Reactive incompatibles", "Overpressure"],
             ["Inspect, stage, and open containers with remote/non-sparking tools",
              "Segregate incompatible materials; use spill containment",
              "Do not handle bulging/pressurized drums without a plan"],
             "Chemical-resistant suit, gloves, face/eye protection, respirator"),
        ]),
        ("4", "Decontamination", [
            ("Decontaminate personnel and equipment",
             ["Cross-contamination", "Take-home exposure"],
             ["Follow the established decon line before leaving the exclusion zone",
              "Manage decon fluids and disposables as contaminated waste",
              "Monitor for heat stress; rotate and rehydrate"],
             "PPE until decon complete"),
        ]),
        ("5", "Emergency Response", [
            ("Respond to releases",
             ["Uncontrolled release", "Responder exposure"],
             ["Follow the emergency response plan; only trained responders act",
              "Establish command, isolate the area, and account for personnel",
              "Use appropriate PPE/respiratory protection for the release"],
             "Respirator/SCBA as required, chemical PPE"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1910-134-respiratory-protection-program"] = {
    "subtitle": "Respiratory Protection Program Use",
    "legend": [
        ("Respiratory", "Inhalation of dusts, fumes, vapors, gases, oxygen deficiency."),
        ("Fit / Seal", "Face-seal leakage defeating protection."),
        ("Physiological", "Breathing resistance, heat, medical limitations."),
    ],
    "jobs": [
        ("1", "Hazard Assessment &amp; Selection", [
            ("Select the correct respirator",
             ["Wrong respirator for the hazard", "IDLH atmosphere"],
             ["Assess the contaminant and concentration; select the proper respirator/cartridge",
              "Use supplied-air/SCBA for IDLH or oxygen-deficient atmospheres",
              "Only use respirators under the written program"],
             "Selected respirator"),
        ]),
        ("2", "Medical Clearance &amp; Fit Testing", [
            ("Clear and fit the user",
             ["Medically unfit user", "Poor face seal"],
             ["Obtain medical evaluation/clearance before use",
              "Perform qualitative/quantitative fit testing for tight-fitting respirators",
              "Prohibit facial hair that breaks the seal"],
             "Fit-tested respirator"),
        ]),
        ("3", "Donning, Seal Check &amp; Use", [
            ("Don and use the respirator",
             ["Seal leakage", "Cartridge breakthrough"],
             ["Perform a user seal check every time before entering the area",
              "Change cartridges/filters per the schedule or breakthrough indicators",
              "Leave the area if breathing resistance, odor, or dizziness occurs"],
             "Fit-tested respirator, task PPE"),
        ]),
        ("4", "Cleaning, Inspection &amp; Storage", [
            ("Maintain the respirator",
             ["Degraded/contaminated respirator", "Failure in use"],
             ["Inspect before and after use; remove defective units from service",
              "Clean and disinfect after use; replace worn parts",
              "Store in a clean, dry, protected location"],
             "Standard PPE"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1910-146-permit-required-confined-spaces-general-industry"] = {
    "subtitle": "Permit-Required Confined Space Entry (General Industry)",
    "legend": [
        ("Atmospheric", "Oxygen deficiency/enrichment, toxic gas, flammable vapors."),
        ("Engulfment", "Liquids or fine solids that can bury or suffocate."),
        ("Configuration / Entrapment", "Converging walls, sloping floors, tapering spaces."),
        ("Physical", "Mechanical/electrical energy, moving parts, temperature."),
        ("Rescue", "Delayed/improvised rescue; rescuer becoming a victim."),
    ],
    "jobs": [
        ("1", "Identify &amp; Permit the Space", [
            ("Identify and authorize entry",
             ["Unrecognized permit space", "Entry without authorization"],
             ["Identify and label permit-required confined spaces",
              "Entry supervisor completes and signs the permit before entry",
              "Post the permit at the entry; cancel on job end or hazard change"],
             "Standard PPE"),
        ]),
        ("2", "Isolation &amp; Lockout", [
            ("Isolate energy and material inflow",
             ["Release of hazardous energy/material"],
             ["Lock/tag/blank/blind all energy and material sources",
              "Drain, purge, and secure the space before entry",
              "Verify isolation before anyone enters"],
             "LOTO devices, gloves, standard PPE"),
        ]),
        ("3", "Atmospheric Testing &amp; Ventilation", [
            ("Test and ventilate",
             ["Oxygen deficiency", "Toxic/flammable atmosphere"],
             ["Test O2, LEL, then toxics with a calibrated meter before entry",
              "Provide continuous mechanical ventilation and monitoring",
              "Do not enter until readings are acceptable"],
             "Calibrated 4-gas monitor, ventilation equipment"),
        ]),
        ("4", "Entry, Attendant &amp; Retrieval", [
            ("Enter with roles and retrieval set",
             ["Unmonitored entry", "Falls", "No retrieval capability"],
             ["Station a trained attendant outside at all times with communication",
              "Use harness and retrieval line via tripod/davit unless a greater hazard",
              "Continuously monitor; evacuate on any alarm or symptom"],
             "Full-body harness, retrieval line, monitor, comms"),
        ]),
        ("5", "Rescue &amp; Emergency", [
            ("Summon or perform rescue",
             ["Rescuer becoming a victim", "Delayed rescue"],
             ["Use non-entry retrieval rescue whenever possible",
              "Summon the pre-arranged trained rescue service on emergency",
              "Never perform unplanned entry rescue"],
             "Retrieval system, rescue kit, SCBA if required"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1910-147-control-of-hazardous-energy-lockout-tagout"] = {
    "subtitle": "Control of Hazardous Energy (Lockout/Tagout)",
    "legend": [
        ("Stored / Released Energy", "Electrical, mechanical, hydraulic, pneumatic, thermal, gravity."),
        ("Unexpected Startup", "Energization or motion during servicing."),
        ("Struck-By / Caught-Between", "Moving parts, stored spring/gravity energy."),
    ],
    "jobs": [
        ("1", "Identify Energy Sources", [
            ("Identify all energy sources",
             ["Missed energy source", "Unexpected energization"],
             ["Use equipment-specific energy control procedures",
              "Identify all energy sources (electrical, mechanical, hydraulic, pneumatic, thermal, gravity)",
              "Notify affected employees before lockout"],
             "Standard PPE"),
        ]),
        ("2", "Shutdown &amp; Isolation", [
            ("Shut down and isolate",
             ["Energy remaining in the system", "Partial isolation"],
             ["Shut down equipment using normal stopping procedures",
              "Isolate each energy source (disconnect, valve, block)",
              "Apply locks and tags to each isolating device"],
             "Locks, tags, hasps, standard PPE"),
        ]),
        ("3", "Release Stored Energy &amp; Verify", [
            ("De-energize and verify zero energy",
             ["Stored/residual energy", "Unverified isolation"],
             ["Release/restrain stored energy (bleed lines, block, discharge capacitors)",
              "Verify zero-energy state by test before work begins",
              "Each worker applies their own lock"],
             "Voltage tester, blocking devices, gloves"),
        ]),
        ("4", "Servicing &amp; Group Lockout", [
            ("Service under lockout",
             ["Startup during service", "Group coordination failure"],
             ["Keep locks in place during all servicing/maintenance",
              "Use a group lockout box for multi-person work",
              "Protect against restart of adjacent equipment"],
             "Task PPE, personal locks"),
        ]),
        ("5", "Restoring Energy", [
            ("Return equipment to service",
             ["Premature re-energization", "Personnel in danger zone"],
             ["Verify tools/guards reinstalled and personnel clear",
              "Remove locks/tags only by the person who applied them",
              "Notify affected employees before re-energizing"],
             "Standard PPE"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1910-178-powered-industrial-trucks-forklift-program"] = {
    "subtitle": "Powered Industrial Truck (Forklift) Operations",
    "legend": [
        ("Struck-By / Caught-Between", "Pedestrians, racking, tip-over, falling loads."),
        ("Tip-Over", "Overload, high speed, uneven surfaces, raised loads."),
        ("Atmospheric", "Carbon monoxide from internal-combustion trucks indoors."),
        ("Falls", "Falling from forks/elevated platforms."),
    ],
    "jobs": [
        ("1", "Operator Qualification &amp; Inspection", [
            ("Qualify operator and inspect truck",
             ["Untrained operator", "Defective truck"],
             ["Only trained, evaluated, and authorized operators drive",
              "Perform a documented pre-shift inspection; remove defective trucks from service",
              "Match the truck type to the environment (e.g., rated for hazardous areas)"],
             "Hard hat, high-visibility vest, safety-toe boots, seat belt"),
        ]),
        ("2", "Load Handling", [
            ("Pick up and place loads",
             ["Overload/tip-over", "Falling load", "Racking collapse"],
             ["Stay within the truck load capacity and load center",
              "Tilt back and carry loads low; secure unstable loads",
              "Do not exceed rack capacities; place loads squarely"],
             "Hard hat, high-visibility vest, safety-toe boots"),
        ]),
        ("3", "Traveling &amp; Pedestrian Control", [
            ("Travel with the truck",
             ["Striking pedestrians", "Collisions", "Blind corners"],
             ["Maintain safe speed; sound horn at intersections/blind spots",
              "Keep forks low; travel in reverse when the load blocks the view",
              "Separate pedestrian and truck routes; make eye contact"],
             "High-visibility vest, hard hat, seat belt"),
        ]),
        ("4", "Elevating Personnel &amp; Refueling/Charging", [
            ("Elevate personnel / refuel or charge",
             ["Falls from forks", "CO buildup", "Battery acid/hydrogen", "Fire"],
             ["Only use an approved secured work platform to elevate personnel",
              "Ventilate for internal-combustion trucks; monitor CO indoors",
              "Charge batteries in ventilated areas; refuel with the engine off, no ignition"],
             "Fall protection on platform, face shield/gloves for battery/fuel"),
        ]),
        ("5", "Parking &amp; Shutdown", [
            ("Park the truck safely",
             ["Unintended movement", "Struck-by"],
             ["Lower forks, neutral, set brake, and shut off when unattended",
              "Do not park on inclines or block exits/equipment",
              "Chock or secure on a grade if unavoidable"],
             "Standard PPE"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1910-331-335-electrical-safety-related-work-practices"] = {
    "subtitle": "Electrical Safety-Related Work Practices",
    "legend": [
        ("Shock / Electrocution", "Contact with energized conductors and parts."),
        ("Arc Flash / Blast", "Thermal burns and pressure from arcing faults."),
        ("Fire", "Faults, overloads, improper equipment."),
        ("Stored Energy", "Capacitors and other stored electrical energy."),
    ],
    "jobs": [
        ("1", "Job Planning &amp; Qualification", [
            ("Plan the electrical task",
             ["Unqualified worker", "Unrecognized hazard"],
             ["Only qualified persons work on/near exposed energized parts",
              "Determine shock and arc-flash boundaries and required PPE",
              "Plan the task and hold a job briefing before starting"],
             "Arc-rated clothing, voltage-rated gloves, safety glasses"),
        ]),
        ("2", "De-energize &amp; Lockout", [
            ("Create an electrically safe condition",
             ["Unexpected energization", "Stored energy"],
             ["De-energize and apply lockout/tagout",
              "Test for absence of voltage with a rated tester before contact",
              "Discharge stored energy and apply safety grounds where needed"],
             "Voltage-rated gloves, rated tester, LOTO devices"),
        ]),
        ("3", "Energized Work (When Justified)", [
            ("Perform energized work",
             ["Shock", "Arc flash/blast", "Burns"],
             ["Perform work de-energized unless infeasible and justified per an energized work permit",
              "Use rated PPE, insulated tools, and maintain approach boundaries",
              "Use a second qualified person/attendant as required"],
             "Arc-rated suit/hood, voltage-rated gloves, insulated tools"),
        ]),
        ("4", "Restoration", [
            ("Return to service",
             ["Premature re-energization", "Faulty work"],
             ["Reinstall guards/covers; confirm personnel and tools clear",
              "Only the authorized person removes their lock/tag",
              "Test before returning equipment to service"],
             "Voltage-rated gloves, safety glasses"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1910-252-welding-cutting-and-brazing-hot-work"] = {
    "subtitle": "Welding, Cutting &amp; Brazing / Hot Work (General Industry)",
    "legend": [
        ("Fire / Explosion", "Sparks, slag near combustibles/flammables."),
        ("Burns", "Hot metal, sparks, molten slag, UV skin burns."),
        ("Fumes / Atmospheric", "Metal fumes, gases, oxygen displacement."),
        ("Electrical", "Shock from welding circuits and leads."),
        ("Compressed Gas", "Cylinder rupture, backflash."),
    ],
    "jobs": [
        ("1", "Hot Work Permit &amp; Fire Watch", [
            ("Authorize and watch the work",
             ["Fire from unpermitted work", "No fire watch"],
             ["Issue a hot work permit; verify no safer alternative",
              "Assign a fire watch with an extinguisher during and 30+ min after",
              "Test atmospheres before work where flammables may be present"],
             "Welding PPE, extinguisher on hand"),
        ]),
        ("2", "Area Preparation", [
            ("Prepare the work area",
             ["Ignition of combustibles", "Sparks to lower areas"],
             ["Remove/shield combustibles within 35 ft; cover openings/drains",
              "Never cut/weld on containers that held flammables until made safe",
              "Provide fire blankets and spark containment"],
             "Welding PPE, fire blankets"),
        ]),
        ("3", "Equipment &amp; Cylinder Setup", [
            ("Set up equipment and cylinders",
             ["Cylinder rupture", "Flashback", "Shock"],
             ["Secure cylinders upright with caps when moving; separate fuel/oxygen",
              "Use flashback arrestors/check valves; inspect hoses and leads",
              "Ground the work; keep leads dry and undamaged"],
             "Welding gloves, leathers, safety glasses"),
        ]),
        ("4", "Welding / Cutting &amp; Ventilation", [
            ("Perform hot work with fume control",
             ["Burns", "Arc radiation", "Fume inhalation"],
             ["Wear correct shade and full welding PPE; screen the arc",
              "Provide local exhaust/ventilation for the process and base metal",
              "Use respiratory protection for coated metals or confined areas"],
             "Welding hood with correct shade, leathers, gloves, respirator as needed"),
        ]),
        ("5", "Post-Work", [
            ("Secure after hot work",
             ["Smoldering fire", "Hot metal contact"],
             ["Maintain fire watch 30+ minutes; inspect adjacent/opposite areas",
              "Shut off and bleed gas supplies; secure cylinders",
              "Allow hot metal to cool or barricade it"],
             "Welding gloves, extinguisher"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1910-1030-bloodborne-pathogens-exposure-control-plan"] = {
    "subtitle": "Bloodborne Pathogens Exposure Control",
    "legend": [
        ("Biological", "Contact with blood/OPIM carrying bloodborne pathogens."),
        ("Sharps", "Needlesticks and cuts from contaminated sharps."),
        ("Contamination", "Surface, clothing, and take-home contamination."),
    ],
    "jobs": [
        ("1", "Exposure Determination &amp; Training", [
            ("Identify exposure and train",
             ["Unrecognized exposure", "Untrained worker"],
             ["Identify job tasks with occupational exposure to blood/OPIM",
              "Train workers and offer the hepatitis B vaccination",
              "Follow universal precautions&mdash;treat all blood/OPIM as infectious"],
             "Gloves, standard PPE"),
        ]),
        ("2", "Work Practice &amp; Engineering Controls", [
            ("Use controls to prevent exposure",
             ["Sharps injury", "Splash exposure"],
             ["Use sharps with engineered injury protection; do not recap needles",
              "Dispose of sharps in labeled puncture-resistant containers",
              "Use splash protection where spatter is possible"],
             "Gloves, gown, face/eye protection as needed"),
        ]),
        ("3", "Handling Contaminated Materials &amp; Cleanup", [
            ("Clean and handle contaminated items",
             ["Contact with contamination", "Spread of pathogens"],
             ["Clean/disinfect surfaces with appropriate disinfectant",
              "Handle contaminated laundry/waste in labeled bags/containers",
              "Do not pick up broken glass by hand; use mechanical means"],
             "Gloves, gown, face/eye protection"),
        ]),
        ("4", "Exposure Incident Response", [
            ("Respond to an exposure incident",
             ["Delayed treatment", "Untracked exposure"],
             ["Wash the area immediately; report the incident right away",
              "Provide confidential post-exposure medical evaluation and follow-up",
              "Document the route of exposure and source where known"],
             "Gloves, standard PPE"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1910-22-30-walking-working-surfaces-fall-protection-general-industry"] = {
    "subtitle": "Walking-Working Surfaces &amp; Fall Protection (General Industry)",
    "legend": [
        ("Falls", "Falls to a lower level from elevated surfaces and openings."),
        ("Slips / Trips", "Wet, cluttered, or uneven walking surfaces."),
        ("Struck-By", "Falling objects through openings/edges."),
        ("Access", "Unsafe ladders, stairs, and fixed ladders."),
    ],
    "jobs": [
        ("1", "Surface Inspection &amp; Housekeeping", [
            ("Maintain safe walking surfaces",
             ["Slips/trips", "Structural weakness"],
             ["Inspect walking-working surfaces; keep them clean, dry, and clear",
              "Correct/mark damaged surfaces; ensure adequate load capacity",
              "Provide drainage or non-slip measures for wet areas"],
             "Safety-toe non-slip footwear, standard PPE"),
        ]),
        ("2", "Guarding Openings &amp; Edges", [
            ("Protect open sides and holes",
             ["Falls through openings", "Falls from edges"],
             ["Guard floor holes/openings with covers or guardrails",
              "Provide standard guardrails at elevated open sides &ge; 4 ft",
              "Install toeboards where objects could fall on workers below"],
             "Standard PPE"),
        ]),
        ("3", "Elevated Work &amp; Fall Protection", [
            ("Work at height",
             ["Falls from height", "Improper anchorage"],
             ["Use guardrails, or personal fall arrest with a rated anchor at &ge; 4 ft",
              "Inspect harness/lanyard before use; ensure adequate fall clearance",
              "Use fixed-ladder fall protection systems where required"],
             "Full-body harness/lanyard, hard hat, non-slip footwear"),
        ]),
        ("4", "Stairs, Ladders &amp; Access", [
            ("Use stairs and fixed ladders",
             ["Falls on stairs/ladders", "Defective access"],
             ["Maintain handrails and stair rails; keep access clear",
              "Inspect fixed/portable ladders; remove defective ones from service",
              "Maintain three points of contact on ladders"],
             "Non-slip footwear, standard PPE"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1910-157-portable-fire-extinguishers"] = {
    "subtitle": "Portable Fire Extinguisher Use &amp; Maintenance",
    "legend": [
        ("Fire", "Incipient-stage fires; wrong extinguisher for the class."),
        ("Burns / Smoke", "Heat and products of combustion."),
        ("Physical", "Handling and mounting of extinguishers."),
    ],
    "jobs": [
        ("1", "Selection &amp; Placement", [
            ("Provide the right extinguishers",
             ["Wrong extinguisher class", "Inaccessible units"],
             ["Select extinguishers matched to the hazard class (A/B/C/D/K)",
              "Mount and identify units with unobstructed access and travel distance",
              "Keep areas around units clear"],
             "Standard PPE"),
        ]),
        ("2", "Inspection &amp; Maintenance", [
            ("Inspect and maintain units",
             ["Discharged/defective extinguisher", "Overdue service"],
             ["Perform monthly visual inspections and annual maintenance",
              "Recharge/hydrotest per schedule; tag and record service",
              "Replace damaged or discharged units immediately"],
             "Standard PPE"),
        ]),
        ("3", "Use on an Incipient Fire", [
            ("Fight an incipient fire (if trained)",
             ["Fire growth beyond control", "Burns/smoke inhalation", "Trapped"],
             ["Only trained personnel attempt incipient fires; sound the alarm first",
              "Keep an exit at your back; use the PASS technique",
              "Evacuate if the fire grows, smoke builds, or the extinguisher empties"],
             "N/A &mdash; evacuate if unsafe"),
        ]),
        ("4", "Training", [
            ("Train users",
             ["Improper use", "Hesitation/panic"],
             ["Train employees on extinguisher use upon assignment and annually",
              "Cover fire classes, PASS technique, and when to evacuate",
              "Document training"],
             "Standard PPE"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1910-269-electric-power-generation-transmission-distribution"] = {
    "subtitle": "Electric Power Generation, Transmission &amp; Distribution",
    "legend": [
        ("Electrical", "Contact, induced voltage, step/touch potential, arc flash."),
        ("Falls", "Falls from poles, structures, and aerial devices."),
        ("Struck-By", "Falling conductors, hardware, equipment."),
        ("Rigging / Hoisting", "Dropped loads during framing and stringing."),
    ],
    "jobs": [
        ("1", "Job Briefing &amp; Clearances", [
            ("Brief the job and obtain clearances",
             ["Unrecognized hazards", "Work without clearance"],
             ["Hold a documented job briefing on hazards and procedures",
              "Obtain switching/clearance orders before work",
              "Confirm minimum approach distances for the voltage"],
             "Arc-rated clothing, voltage-rated gloves, hard hat"),
        ]),
        ("2", "De-energize, Test &amp; Ground", [
            ("Isolate and ground",
             ["Contact with energized line", "Induced voltage", "Re-energization"],
             ["De-energize, test for absence of voltage, and apply protective grounds",
              "Treat lines as energized until tested and grounded",
              "Install grounds to control induced/fault voltage at the work site"],
             "Voltage-rated gloves/sleeves, grounds, rated tester"),
        ]),
        ("3", "Climbing &amp; Aerial Access", [
            ("Access poles and structures",
             ["Falls", "Pole/structure failure", "Aerial device tip-over"],
             ["Inspect/test poles before climbing; use fall protection continuously",
              "Set aerial devices on firm level ground with outriggers",
              "Maintain line clearances during positioning"],
             "Full-body harness, climbing gear, hard hat, voltage-rated gloves"),
        ]),
        ("4", "Energized-Line Work", [
            ("Perform live-line work (when required)",
             ["Electrocution", "Flashover", "Burns"],
             ["Only qualified line workers with rated cover-up and live-line tools",
              "Maintain minimum approach distances; insulate/isolate the worker",
              "Follow the approved hot-stick or barehand procedure"],
             "Voltage-rated gloves/sleeves, arc-rated suit, insulated tools"),
        ]),
        ("5", "Stringing &amp; Rigging", [
            ("String and rig conductors",
             ["Dropped/backlash conductor", "Struck-by", "Adjacent energized lines"],
             ["Use rated pulling/tensioning gear and signals",
              "Keep clear of conductors under tension; guard structures at crossings",
              "Ground conductors/equipment to control induced voltage"],
             "Hard hat, voltage-rated gloves, high-visibility vest"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1910-179-cranes-derricks-hoists-slings-general-industry-rigging"] = {
    "subtitle": "Cranes, Hoists &amp; Rigging (General Industry)",
    "legend": [
        ("Struck-By / Caught-Between", "Swinging/dropped loads, pinch points."),
        ("Rigging Failure", "Overloaded or damaged slings, shackles, hooks."),
        ("Overhead", "Contact with power lines and structures."),
        ("Mechanical", "Hoist/crane defects, two-blocking."),
    ],
    "jobs": [
        ("1", "Equipment &amp; Rigging Inspection", [
            ("Inspect crane, hoist, and rigging",
             ["Defective equipment", "Damaged slings/hooks"],
             ["Perform pre-use and periodic inspections; remove defective gear",
              "Verify rated capacity and inspection tags on slings/shackles/hooks",
              "Only qualified operators/riggers perform lifts"],
             "Hard hat, safety glasses, gloves, safety-toe boots"),
        ]),
        ("2", "Lift Planning &amp; Rigging the Load", [
            ("Plan and rig the lift",
             ["Overload", "Unbalanced/shifting load", "Dropped load"],
             ["Determine load weight and center of gravity; stay within capacity",
              "Select and attach rigging for the load; protect against sharp edges",
              "Use tag lines; confirm the load is balanced before lifting"],
             "Hard hat, gloves, safety glasses, high-visibility vest"),
        ]),
        ("3", "Hoisting &amp; Moving the Load", [
            ("Hoist and move the load",
             ["Struck by swinging load", "Two-blocking", "Power line contact"],
             ["Keep personnel clear of and never under the suspended load",
              "Use standard hand signals/qualified signal person",
              "Maintain clearance from overhead power lines and structures"],
             "Hard hat, gloves, safety glasses, high-visibility vest"),
        ]),
        ("4", "Landing &amp; Securing", [
            ("Land and de-rig the load",
             ["Caught-between", "Load tip-over"],
             ["Land on stable dunnage; keep hands clear of pinch points",
              "De-rig only when the load is stable and supported",
              "Store rigging properly after use"],
             "Hard hat, gloves, safety-toe boots"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1910-106-flammable-liquids"] = {
    "subtitle": "Flammable Liquids Handling &amp; Storage",
    "legend": [
        ("Fire / Explosion", "Ignition of vapors; static discharge."),
        ("Chemical", "Skin/eye contact, inhalation of vapors."),
        ("Atmospheric", "Vapor accumulation in low/enclosed areas."),
        ("Spill / Environmental", "Release to floor, drains, and soil."),
    ],
    "jobs": [
        ("1", "Storage &amp; Segregation", [
            ("Store flammable liquids",
             ["Vapor ignition", "Incompatible storage"],
             ["Store in approved flammable-storage cabinets/rooms with proper limits",
              "Keep away from ignition sources and incompatible materials",
              "Provide ventilation and fire protection for storage areas"],
             "Chemical-resistant gloves, safety glasses"),
        ]),
        ("2", "Dispensing &amp; Transfer", [
            ("Dispense and transfer liquids",
             ["Static-spark ignition", "Splash/spill", "Vapor inhalation"],
             ["Bond and ground containers during transfer",
              "Use approved safety cans/pumps; transfer in ventilated areas",
              "Eliminate ignition sources; no smoking/open flame"],
             "Chemical-resistant gloves, face/eye protection, respirator as needed"),
        ]),
        ("3", "Use at the Work Area", [
            ("Use flammable liquids in work",
             ["Fire", "Vapor buildup", "Skin/eye contact"],
             ["Keep only the minimum quantity needed at the point of use",
              "Maintain ventilation; monitor for vapor accumulation in low areas",
              "Keep an extinguisher available; control ignition sources"],
             "Chemical-resistant gloves, safety glasses, extinguisher nearby"),
        ]),
        ("4", "Spill Response &amp; Waste", [
            ("Respond to spills",
             ["Fire from spill", "Environmental release"],
             ["Stop the source; eliminate ignition; ventilate the area",
              "Contain and absorb with compatible spill materials; keep from drains",
              "Manage waste and used absorbent as hazardous waste"],
             "Chemical-resistant gloves/suit, face/eye protection, respirator as needed"),
        ]),
    ],
}


# ── General Industry — chemical &amp; specialty programs ──────────────────────
JHA_LIBRARY["29-cfr-1910-119-process-safety-management-of-highly-hazardous-chemicals-psm"] = {
    "subtitle": "Process Safety Management &mdash; Highly Hazardous Chemicals",
    "legend": [
        ("Chemical Release", "Loss of containment of highly hazardous chemicals."),
        ("Fire / Explosion", "Ignition of flammable/reactive process materials."),
        ("Stored Energy", "Pressure, temperature, and mechanical energy in process."),
        ("Atmospheric", "Toxic exposure from leaks and openings."),
    ],
    "jobs": [
        ("1", "Process Review &amp; Permitting", [
            ("Review process safety information before work",
             ["Unknown process hazards", "Work without authorization"],
             ["Review process safety information and hazards before the task",
              "Obtain required permits (hot work, line break, confined space)",
              "Apply operating procedures and management-of-change requirements"],
             "Task PPE per the process hazard"),
        ]),
        ("2", "Line/Equipment Opening &amp; Isolation", [
            ("Open lines and equipment safely",
             ["Release of hazardous chemical", "Stored pressure/energy"],
             ["Isolate, depressurize, drain, and purge before opening lines/equipment",
              "Apply lockout/tagout and blinds/blanks; verify zero energy",
              "Test the atmosphere before and during the work"],
             "Chemical PPE, gas monitor, LOTO devices"),
        ]),
        ("3", "Maintenance on Process Equipment", [
            ("Perform mechanical integrity work",
             ["Chemical exposure", "Ignition", "Equipment failure"],
             ["Follow safe work practices and mechanical integrity procedures",
              "Control ignition sources; maintain ventilation",
              "Use pre-startup safety review before returning to service"],
             "Chemical-resistant PPE, respirator as required"),
        ]),
        ("4", "Emergency Response &amp; Shutdown", [
            ("Respond to a process upset/release",
             ["Escalating release", "Responder exposure"],
             ["Follow emergency shutdown and response procedures",
              "Isolate, evacuate, and account for personnel",
              "Only trained responders act with proper PPE"],
             "SCBA/supplied air as required, chemical suit"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1910-1200-hazard-communication-hazcom-ghs"] = {
    "subtitle": "Hazard Communication (HazCom / GHS)",
    "legend": [
        ("Chemical", "Skin/eye contact, inhalation, ingestion of hazardous chemicals."),
        ("Fire / Reactivity", "Flammable, oxidizer, and reactive chemical hazards."),
        ("Information", "Unlabeled containers, missing safety data sheets."),
    ],
    "jobs": [
        ("1", "Inventory, Labels &amp; SDS", [
            ("Maintain chemical information",
             ["Unknown chemical hazards", "Unlabeled containers"],
             ["Maintain a chemical inventory and accessible safety data sheets (SDS)",
              "Ensure all containers are labeled per GHS; label secondary containers",
              "Review the SDS before using an unfamiliar chemical"],
             "Gloves, safety glasses per SDS"),
        ]),
        ("2", "Safe Chemical Handling", [
            ("Handle and use chemicals",
             ["Skin/eye contact", "Inhalation", "Incompatible mixing"],
             ["Use PPE and controls specified on the SDS/label",
              "Provide ventilation; avoid mixing incompatible chemicals",
              "Keep containers closed; use the minimum quantity needed"],
             "SDS-specified gloves, eye/face protection, respirator as needed"),
        ]),
        ("3", "Storage &amp; Segregation", [
            ("Store chemicals",
             ["Fire/reaction from incompatible storage", "Spill"],
             ["Segregate incompatibles (acids/bases, oxidizers/flammables)",
              "Store flammables in approved cabinets; provide secondary containment",
              "Keep storage areas ventilated, labeled, and orderly"],
             "Gloves, safety glasses"),
        ]),
        ("4", "Spill Response &amp; Training", [
            ("Respond to spills and train workers",
             ["Exposure during cleanup", "Untrained workers"],
             ["Follow SDS spill procedures; use compatible spill kit and PPE",
              "Train employees on hazards, labels, SDS, and controls",
              "Report and document spills and exposures"],
             "Chemical-resistant gloves, eye/face protection, respirator as needed"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1910-1450-laboratory-chemical-hygiene-plan"] = {
    "subtitle": "Laboratory Chemical Hygiene",
    "legend": [
        ("Chemical", "Exposure to lab chemicals, carcinogens, reproductive toxins."),
        ("Fire / Reactivity", "Flammables, pyrophorics, oxidizers, explosives."),
        ("Physical", "Compressed gas, cryogens, glassware, sharps."),
        ("Contamination", "Surface and personnel contamination."),
    ],
    "jobs": [
        ("1", "Planning &amp; Chemical Hygiene", [
            ("Plan lab work under the CHP",
             ["Unassessed hazards", "Improper procedures"],
             ["Follow the Chemical Hygiene Plan and standard operating procedures",
              "Review SDS and identify particularly hazardous substances",
              "Designate and consult the chemical hygiene officer"],
             "Lab coat, safety glasses/goggles, gloves"),
        ]),
        ("2", "Fume Hood &amp; Engineering Controls", [
            ("Work with hazardous chemicals",
             ["Inhalation exposure", "Fire/explosion"],
             ["Perform work with volatile/hazardous chemicals in a certified fume hood",
              "Verify hood airflow before use; keep the sash at the working height",
              "Use designated areas for particularly hazardous substances"],
             "Lab coat, chemical goggles, gloves, respirator if required"),
        ]),
        ("3", "Handling, Storage &amp; Compressed Gas", [
            ("Handle and store lab materials",
             ["Incompatible reaction", "Cylinder/cryogen hazard", "Glass cuts"],
             ["Segregate incompatibles; store flammables/acids/bases properly",
              "Secure compressed gas cylinders; handle cryogens with proper PPE",
              "Inspect glassware; use mechanical means for broken glass"],
             "Lab coat, goggles, cryo/chemical gloves, face shield as needed"),
        ]),
        ("4", "Waste, Decon &amp; Emergency", [
            ("Manage waste and respond to spills",
             ["Chemical waste exposure", "Uncontrolled spill"],
             ["Collect and label chemical waste by compatibility",
              "Know locations of eyewash/safety shower and spill kits",
              "Follow spill and exposure response procedures"],
             "Lab coat, goggles, gloves"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1910-1001-asbestos-general-industry"] = {
    "subtitle": "Asbestos Exposure Control (General Industry)",
    "legend": [
        ("Respiratory", "Inhalation of asbestos fibers (asbestosis, mesothelioma)."),
        ("Regulated Area", "Uncontrolled fiber release beyond containment."),
        ("Housekeeping", "Re-entrained fibers from dry debris/clothing."),
        ("Waste", "Improperly contained asbestos-containing waste."),
    ],
    "jobs": [
        ("1", "Identify &amp; Assess", [
            ("Identify asbestos and assess exposure",
             ["Unknown asbestos material", "Unprotected work"],
             ["Identify presumed/known asbestos-containing material before disturbance",
              "Assess exposures; apply the required work class and controls",
              "Only trained workers perform asbestos work"],
             "Respirator per exposure, coveralls, gloves"),
        ]),
        ("2", "Regulated Area &amp; Controls", [
            ("Establish controls",
             ["Fiber release", "Unauthorized entry"],
             ["Establish a regulated area with warning signs and access control",
              "Use wet methods, HEPA local exhaust, and enclosures as required",
              "Prohibit eating/drinking/smoking in the area"],
             "Respirator, disposable coveralls, gloves"),
        ]),
        ("3", "Disturbance / Maintenance Work", [
            ("Disturb ACM during maintenance",
             ["Airborne fibers", "Dry handling"],
             ["Wet the material; use HEPA-filtered tools; avoid breaking/aggressive methods",
              "Bag material at the point of removal; minimize handling",
              "Monitor exposures during the work"],
             "Respirator per exposure, coveralls, gloves"),
        ]),
        ("4", "Decon, Housekeeping &amp; Waste", [
            ("Decontaminate and dispose",
             ["Take-home contamination", "Re-entrained dust"],
             ["HEPA-vacuum suits; use decon procedures; never dry sweep or use compressed air",
              "Double-bag and label asbestos waste; dispose at an approved site",
              "Verify cleanup before releasing the area"],
             "Respirator, coveralls, gloves"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1910-1025-lead-general-industry"] = {
    "subtitle": "Lead Exposure Control (General Industry)",
    "legend": [
        ("Respiratory / Ingestion", "Inhalation of lead fume/dust; hand-to-mouth ingestion."),
        ("Environmental", "Lead dust migration and surface contamination."),
        ("Housekeeping", "Accumulated lead dust re-entrained by traffic/cleaning."),
        ("Waste", "Hazardous lead-containing waste."),
    ],
    "jobs": [
        ("1", "Exposure Assessment", [
            ("Assess lead exposure",
             ["Underestimated exposure", "Unprotected work"],
             ["Conduct exposure monitoring; apply initial protective measures for trigger tasks",
              "Implement the written compliance program",
              "Provide medical surveillance/blood-lead monitoring where required"],
             "Respirator per exposure, coveralls, gloves"),
        ]),
        ("2", "Engineering &amp; Work-Practice Controls", [
            ("Control lead exposure",
             ["Airborne lead fume/dust", "Contamination spread"],
             ["Use local exhaust ventilation and wet/enclosed methods as feasible",
              "Establish a regulated area; prohibit eating/drinking/smoking",
              "Substitute lower-exposure methods where possible"],
             "Respirator, coveralls, gloves, safety glasses"),
        ]),
        ("3", "Lead Disturbance / Hot Work", [
            ("Cut, grind, or torch lead materials",
             ["High fume from heat/abrasion", "Fire"],
             ["Prefer low-fume methods; provide LEV at the point of generation",
              "Control ignition sources for torch/heat work",
              "Monitor exposures during the task"],
             "Respirator per exposure, coveralls, gloves, face shield as needed"),
        ]),
        ("4", "Hygiene, Housekeeping &amp; Waste", [
            ("Decontaminate and manage waste",
             ["Ingestion", "Take-home lead", "Improper disposal"],
             ["Provide wash/change facilities; wash before eating; leave work clothes on site",
              "HEPA-vacuum and wet-clean; never dry sweep or use compressed air",
              "Containerize/label lead waste; characterize and dispose per RCRA"],
             "Respirator as needed, gloves, coveralls"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1910-1026-hexavalent-chromium-general-industry"] = {
    "subtitle": "Hexavalent Chromium Exposure Control",
    "legend": [
        ("Respiratory", "Inhalation of Cr(VI) fume/dust/mist (lung cancer, ulceration)."),
        ("Skin / Eye", "Contact causing ulcers, dermatitis, eye damage."),
        ("Housekeeping", "Re-entrained Cr(VI) dust; surface contamination."),
        ("Waste", "Chromium-containing waste handling."),
    ],
    "jobs": [
        ("1", "Identify Sources &amp; Assess", [
            ("Identify Cr(VI) tasks",
             ["Unrecognized Cr(VI) exposure", "Unprotected work"],
             ["Identify tasks generating Cr(VI): welding stainless, painting, plating, thermal cutting",
              "Assess exposures; establish controls to keep exposure below the PEL",
              "Provide medical surveillance where required"],
             "Respirator per exposure, gloves, safety glasses"),
        ]),
        ("2", "Engineering Controls", [
            ("Control Cr(VI) at the source",
             ["Airborne fume/dust/mist", "Continued overexposure"],
             ["Use local exhaust ventilation on welding/grinding/plating operations",
              "Enclose or isolate high-exposure processes",
              "Maintain and verify ventilation performance"],
             "Respirator as required, gloves, face/eye protection"),
        ]),
        ("3", "Performing the Work", [
            ("Weld, grind, paint, or plate",
             ["Inhalation", "Skin/eye ulceration"],
             ["Position workers upwind; keep LEV running throughout",
              "Prevent skin/eye contact with chromate solutions and dust",
              "Limit access to the regulated/controlled area"],
             "Respirator, chemical-resistant gloves, face shield, welding PPE as applicable"),
        ]),
        ("4", "Hygiene, Housekeeping &amp; Waste", [
            ("Decontaminate and clean",
             ["Ingestion", "Re-entrained dust", "Contaminated PPE"],
             ["Provide wash facilities; wash before eating; no eating in work areas",
              "HEPA-vacuum or wet methods; never dry sweep or use compressed air",
              "Containerize/label Cr(VI) waste and contaminated PPE"],
             "Respirator as needed, gloves"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1910-1027-cadmium-general-industry"] = {
    "subtitle": "Cadmium Exposure Control",
    "legend": [
        ("Respiratory", "Inhalation of cadmium fume/dust (kidney damage, lung cancer)."),
        ("Ingestion", "Hand-to-mouth ingestion of cadmium dust."),
        ("Housekeeping", "Re-entrained cadmium dust; surface contamination."),
        ("Waste", "Cadmium-containing waste."),
    ],
    "jobs": [
        ("1", "Identify Sources &amp; Assess", [
            ("Identify cadmium tasks",
             ["Unrecognized exposure", "Unprotected work"],
             ["Identify tasks: welding/brazing cadmium-coated metal, cutting, soldering, plating",
              "Assess exposures; establish controls to stay below the PEL",
              "Provide medical surveillance where required"],
             "Respirator per exposure, gloves, safety glasses"),
        ]),
        ("2", "Engineering Controls &amp; Regulated Area", [
            ("Control cadmium at the source",
             ["Airborne fume/dust", "Contamination spread"],
             ["Provide local exhaust ventilation for fume/dust-generating tasks",
              "Establish a regulated area; prohibit eating/drinking/smoking",
              "Verify ventilation before starting"],
             "Respirator as required, gloves, face/eye protection"),
        ]),
        ("3", "Performing the Work", [
            ("Weld, braze, cut, or solder",
             ["High cadmium fume from heat", "Inhalation"],
             ["Never torch-cut/weld cadmium-plated metal without LEV and respiratory protection",
              "Position workers upwind; keep LEV running throughout",
              "Limit access to the regulated area"],
             "Respirator, welding/leather gloves, face shield, welding PPE as applicable"),
        ]),
        ("4", "Hygiene, Housekeeping &amp; Waste", [
            ("Decontaminate and clean",
             ["Ingestion", "Take-home dust", "Improper disposal"],
             ["Provide wash/change facilities; wash before eating; no eating in the area",
              "HEPA-vacuum or wet-clean; never dry sweep or use compressed air",
              "Containerize/label cadmium waste and contaminated PPE"],
             "Respirator as needed, gloves"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1910-1048-formaldehyde"] = {
    "subtitle": "Formaldehyde Exposure Control",
    "legend": [
        ("Respiratory", "Inhalation of formaldehyde vapor (irritant, sensitizer, carcinogen)."),
        ("Skin / Eye", "Contact causing irritation, burns, sensitization."),
        ("Fire", "Formaldehyde solutions/vapors can be flammable."),
        ("Contamination", "Spills and surface contamination."),
    ],
    "jobs": [
        ("1", "Identify Sources &amp; Assess", [
            ("Identify formaldehyde tasks",
             ["Unrecognized exposure", "Sensitized workers"],
             ["Identify sources: preserving/embalming, resins, labs, treated products",
              "Assess exposures against the PEL/STEL; establish controls",
              "Provide medical surveillance/removal for signs of sensitization"],
             "Respirator per exposure, chemical goggles, gloves"),
        ]),
        ("2", "Ventilation &amp; Engineering Controls", [
            ("Control formaldehyde vapor",
             ["Inhalation", "Vapor accumulation"],
             ["Use fume hoods/local exhaust for formaldehyde processes",
              "Verify ventilation; keep containers closed when not in use",
              "Establish a regulated area where the STEL/PEL may be exceeded"],
             "Respirator as required, goggles, chemical gloves"),
        ]),
        ("3", "Handling &amp; Use", [
            ("Handle formaldehyde solutions",
             ["Skin/eye burns", "Sensitization", "Fire"],
             ["Prevent skin/eye contact; use chemical-resistant PPE",
              "Keep away from ignition sources; control flammable vapor",
              "Provide eyewash/safety shower nearby"],
             "Chemical goggles/face shield, chemical gloves, apron, respirator as needed"),
        ]),
        ("4", "Spill Response &amp; Waste", [
            ("Respond to spills",
             ["Vapor exposure during cleanup", "Environmental release"],
             ["Ventilate; use compatible spill kit and PPE; control ignition",
              "Contain and neutralize/absorb; keep out of drains",
              "Manage waste per regulations; document exposures"],
             "Respirator, chemical suit/gloves, goggles"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1910-1028-benzene"] = {
    "subtitle": "Benzene Exposure Control",
    "legend": [
        ("Respiratory", "Inhalation of benzene vapor (leukemia, blood disorders)."),
        ("Fire / Explosion", "Highly flammable vapor; static ignition."),
        ("Skin / Eye", "Contact/absorption; irritation."),
        ("Atmospheric", "Vapor accumulation in tanks and low areas."),
    ],
    "jobs": [
        ("1", "Identify Sources &amp; Assess", [
            ("Identify benzene exposure",
             ["Unrecognized exposure", "Unprotected work"],
             ["Identify sources: crude/gasoline handling, tank gauging, sampling, refining",
              "Assess exposures against the PEL/STEL; establish regulated areas",
              "Provide medical surveillance where required"],
             "Respirator per exposure, chemical gloves, safety glasses"),
        ]),
        ("2", "Vapor &amp; Ignition Control", [
            ("Control vapor and ignition sources",
             ["Vapor inhalation", "Fire/explosion", "Static discharge"],
             ["Use closed systems, LEV, and vapor recovery where feasible",
              "Bond and ground during transfer; eliminate ignition sources",
              "Monitor atmospheres; ventilate tanks and low areas before entry"],
             "Respirator/supplied air as required, chemical gloves, FR clothing"),
        ]),
        ("3", "Sampling / Gauging / Tank Work", [
            ("Sample, gauge, or enter benzene service",
             ["Peak vapor exposure", "Confined space", "Skin contact"],
             ["Stand upwind; use closed sampling/gauging methods where possible",
              "Apply confined space entry procedures for tanks/vessels",
              "Prevent skin contact and absorption"],
             "Supplied-air respirator for entry, chemical suit/gloves, gas monitor"),
        ]),
        ("4", "Spill Response &amp; Hygiene", [
            ("Respond to spills and decontaminate",
             ["Fire from spill", "Continued exposure"],
             ["Stop the source; eliminate ignition; ventilate; contain the spill",
              "Keep spills from drains; manage as hazardous waste",
              "Remove contaminated clothing; wash exposed skin"],
             "Respirator, chemical suit/gloves, goggles"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1910-1053-respirable-crystalline-silica-general-industry"] = {
    "subtitle": "Respirable Crystalline Silica Control (General Industry)",
    "legend": [
        ("Respiratory", "Inhalation of respirable crystalline silica (silicosis, cancer)."),
        ("Environmental", "Dust migration to other workers/areas."),
        ("Housekeeping", "Accumulated dust re-entrained by cleaning/traffic."),
    ],
    "jobs": [
        ("1", "Exposure Assessment", [
            ("Assess silica exposure",
             ["Unrecognized exposure", "Overexposure above PEL"],
             ["Identify silica-generating tasks; assess exposures against the PEL",
              "Implement the written exposure control plan",
              "Provide medical surveillance for highly exposed workers"],
             "Respirator per exposure, safety glasses, gloves"),
        ]),
        ("2", "Engineering Controls", [
            ("Control dust at the source",
             ["Airborne silica dust", "Failed control"],
             ["Use wet methods or local exhaust/dust collection on tools/processes",
              "Verify water flow/vacuum airflow; maintain filters",
              "Isolate/enclose high-dust processes where feasible"],
             "Respirator as required, safety glasses, gloves"),
        ]),
        ("3", "Performing Dusty Work", [
            ("Perform silica-generating work",
             ["Silica inhalation", "Exposure to nearby workers"],
             ["Keep engineering controls running throughout",
              "Position workers upwind; restrict access to the dust area",
              "Rotate/limit duration per the control plan"],
             "Respirator, safety glasses, gloves, hearing protection as needed"),
        ]),
        ("4", "Housekeeping &amp; Cleanup", [
            ("Clean up silica dust",
             ["Re-entrained dust", "Contaminated clothing"],
             ["HEPA-vacuum or wet methods; never dry sweep or use compressed air",
              "Contain and dispose of silica waste to prevent re-suspension",
              "Provide wash facilities; do not carry dust home"],
             "Respirator as needed, gloves, safety glasses"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1910-1096-ionizing-radiation"] = {
    "subtitle": "Ionizing Radiation Safety",
    "legend": [
        ("Radiation", "External exposure and internal contamination from sources."),
        ("Contamination", "Spread of radioactive material."),
        ("Access", "Uncontrolled entry to restricted/high-radiation areas."),
    ],
    "jobs": [
        ("1", "Source Control &amp; Planning", [
            ("Plan work with radiation sources",
             ["Unrecognized source", "Overexposure"],
             ["Work under the radiation safety program and radiation safety officer",
              "Only trained/authorized personnel handle sources or enter areas",
              "Plan work to keep doses ALARA (as low as reasonably achievable)"],
             "Dosimeter, standard PPE"),
        ]),
        ("2", "Area Posting &amp; Access Control", [
            ("Establish restricted areas",
             ["Uncontrolled exposure", "Unauthorized entry"],
             ["Post radiation/high-radiation areas; control and log access",
              "Use time, distance, and shielding to reduce exposure",
              "Survey areas and verify boundaries before work"],
             "Dosimeter, survey meter, standard PPE"),
        ]),
        ("3", "Working with Sources / Radiography", [
            ("Perform radiation work",
             ["External dose", "Source loss/exposure device failure"],
             ["Maintain distance and shielding; minimize time near the source",
              "Verify source retraction with a survey after each exposure (radiography)",
              "Never handle a source directly; use remote handling tools"],
             "Dosimeter, survey meter, task PPE"),
        ]),
        ("4", "Contamination Control &amp; Emergency", [
            ("Control contamination and respond",
             ["Internal contamination", "Lost/stuck source"],
             ["Survey personnel/areas for contamination; decontaminate as needed",
              "Follow emergency procedures for a stuck/lost source",
              "Report incidents and monitor doses"],
             "Dosimeter, contamination PPE as required"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1910-268-telecommunications"] = {
    "subtitle": "Telecommunications Work (Poles, Towers &amp; Manholes)",
    "legend": [
        ("Falls", "Falls from poles, towers, aerial lifts, and ladders."),
        ("Electrical", "Contact with power lines and energized equipment."),
        ("Atmospheric", "Manhole/vault confined-space and gas hazards."),
        ("Struck-By / RF", "Traffic, falling tools, RF exposure on towers."),
    ],
    "jobs": [
        ("1", "Job Planning &amp; Access", [
            ("Plan the task and set up access",
             ["Unrecognized hazards", "Traffic exposure"],
             ["Identify hazards (power, traffic, RF, atmospheres) before starting",
              "Set up traffic control/work-zone protection as needed",
              "Verify pole/structure integrity before climbing"],
             "Hard hat, high-visibility vest, safety glasses, gloves"),
        ]),
        ("2", "Pole / Tower Climbing", [
            ("Climb poles and towers",
             ["Falls", "Structure failure", "RF overexposure"],
             ["Use fall protection continuously; inspect climbing equipment",
              "Verify RF is de-energized/reduced or maintain safe distance on towers",
              "Test pole integrity; use aerial lifts on firm ground with outriggers"],
             "Full-body harness, climbing gear, hard hat, RF monitor as needed"),
        ]),
        ("3", "Manhole / Vault Entry", [
            ("Enter manholes and vaults",
             ["Oxygen deficiency/toxic gas", "Engulfment", "Electrical"],
             ["Test the atmosphere and ventilate before and during entry",
              "Apply confined space procedures; use attendant and retrieval",
              "Control traffic around the opening; guard the opening"],
             "Gas monitor, harness/retrieval, ventilation, gloves"),
        ]),
        ("4", "Working Near Power &amp; Cables", [
            ("Work near power and pull cable",
             ["Electrical contact", "Strain/ergonomic", "Struck-by"],
             ["Maintain clearances from power conductors; treat lines as energized",
              "Use proper cable-pulling techniques and equipment",
              "Protect against falling tools; keep the public clear"],
             "Voltage-rated gloves as needed, hard hat, gloves, high-visibility vest"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1910-266-logging-operations"] = {
    "subtitle": "Logging Operations",
    "legend": [
        ("Struck-By", "Falling trees/limbs, rolling logs, chainsaw kickback."),
        ("Caught-Between", "Machinery, logs, and equipment."),
        ("Cuts / Amputation", "Chainsaws and processing equipment."),
        ("Terrain / Environmental", "Uneven ground, weather, remote location."),
    ],
    "jobs": [
        ("1", "Planning &amp; Site Assessment", [
            ("Assess the site and plan felling",
             ["Unrecognized hazards", "Struck-by hazards"],
             ["Assess terrain, weather, lean, and hazards before work",
              "Maintain safe distances (2 tree-lengths) between workers",
              "Establish communication and emergency/first-aid provisions on site"],
             "Hard hat, eye/face screen, hearing protection, cut-resistant chaps"),
        ]),
        ("2", "Felling", [
            ("Fell trees",
             ["Struck by falling tree/limb", "Kickback", "Barber-chair split"],
             ["Use proper undercut/back-cut and hinge; plan the escape path",
              "Clear the retreat path; watch for overhead/dead limbs (widow-makers)",
              "Keep others out of the felling area"],
             "Hard hat, face screen, hearing protection, cut-resistant chaps, gloves"),
        ]),
        ("3", "Limbing &amp; Bucking", [
            ("Limb and buck logs",
             ["Chainsaw cuts/kickback", "Rolling/springing logs"],
             ["Maintain firm footing; keep the saw at proper position; avoid the kickback zone",
              "Watch for spring poles and log roll; stand on the uphill side",
              "Do not cut above shoulder height"],
             "Cut-resistant chaps/boots, hard hat, face screen, hearing protection, gloves"),
        ]),
        ("4", "Skidding, Yarding &amp; Loading", [
            ("Move and load logs",
             ["Struck by/caught in machinery", "Rolling logs", "Rollover"],
             ["Keep clear of machine travel paths and cable/rigging under tension",
              "Use ROPS/seat belts on machines; communicate with operators",
              "Secure loads; keep workers out of the loading zone"],
             "Hard hat, high-visibility vest, hearing protection, safety-toe boots"),
        ]),
    ],
}


JHA_LIBRARY["29-cfr-1910-272-grain-handling-facilities"] = {
    "subtitle": "Grain Handling Facility Operations",
    "legend": [
        ("Fire / Explosion", "Combustible grain dust deflagration."),
        ("Engulfment", "Suffocation in bins, silos, and flowing grain."),
        ("Atmospheric", "Oxygen deficiency, fumigants, toxic gases."),
        ("Mechanical", "Augers, sweeps, and moving equipment."),
    ],
    "jobs": [
        ("1", "Dust Control &amp; Hot Work", [
            ("Control combustible dust and ignition",
             ["Dust explosion", "Fire from hot work"],
             ["Control grain dust accumulation with housekeeping and dust collection",
              "Use a hot work permit; remove/wet dust before hot work",
              "Control ignition sources; bond/ground where needed"],
             "Standard PPE, extinguisher for hot work"),
        ]),
        ("2", "Bin / Silo Entry", [
            ("Enter grain storage structures",
             ["Engulfment in flowing grain", "Oxygen deficiency/toxic atmosphere"],
             ["Turn off and lock out all grain-moving equipment before entry",
              "Test/ventilate the atmosphere; use permit-required confined space procedures",
              "Never walk down grain; use a harness, lifeline, and attendant"],
             "Full-body harness, lifeline, gas monitor, attendant"),
        ]),
        ("3", "Equipment Operation &amp; Maintenance", [
            ("Operate and service equipment",
             ["Caught in augers/sweeps", "Unexpected startup"],
             ["Guard augers/sweeps; never enter with the sweep auger energized",
              "Apply lockout/tagout before servicing equipment",
              "Keep clear of moving parts and pinch points"],
             "Standard PPE, LOTO devices, gloves"),
        ]),
        ("4", "Fumigation &amp; Emergency", [
            ("Handle fumigants and respond to emergencies",
             ["Fumigant exposure", "Engulfment rescue"],
             ["Only trained applicators handle fumigants; post and control treated areas",
              "Use the emergency action plan; never enter a bin to rescue without protection",
              "Provide rescue equipment and trained rescue procedures"],
             "Respirator as required for fumigants, harness/retrieval for rescue"),
        ]),
    ],
}
