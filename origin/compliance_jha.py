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
