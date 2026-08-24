---
template_id: program-29-cfr-1910-331-335-electrical-safety-related-work-practices
type: written_program
pulls_from:
  - 29-cfr-1910-331-335-electrical-safety-related-work-practices
  - nfpa-70e-electrical-safety-program-esp
  - nfpa-70e-electrically-safe-work-condition-eswc
  - nfpa-70e-shock-risk-assessment-approach-boundaries
  - nfpa-70e-arc-flash-risk-assessment-incident-energy
  - nfpa-70e-arc-rated-ppe-selection
  - nfpa-70e-energized-electrical-work-permit-eewp
  - nfpa-70e-arc-flash-equipment-labeling
  - nfpa-70e-qualified-unqualified-person-training
citation: "29 CFR 1910.331-.335 (NFPA 70E aligned)"
placeholders:
  - COMPANY_NAME
  - COMPANY_ADDRESS
  - PROGRAM_ADMINISTRATOR
  - ADMIN_TITLE
  - ADMIN_PHONE
  - ADMIN_EMAIL
  - EFFECTIVE_DATE
  - SCOPE
  - LAST_REVIEW_DATE
  - NEXT_REVIEW_DATE
  - SIGNATURE_NAME
  - SIGNATURE_TITLE
  - SIGNATURE_DATE
grounding_rule: >
  Requirements, thresholds, and procedures are stated in the company's own words with the
  governing OSHA and NFPA 70E section citations. NFPA 70E is copyrighted — do NOT paste 70E
  table values (e.g., approach-boundary distances in Table 130.4, PPE ensembles in Table
  130.7(C)(15)). Cite the table and have the reader look it up in their licensed copy of the
  current 70E. Fill each [[...]] prompt with the company's own procedure. Before submitting,
  confirm none of the reviewer rejection reasons in the final section apply.
---

<table class="oms-lh"><tr>
<td style="width:64%"><div class="client-name">{{COMPANY_NAME}}</div><div class="client-addr">{{COMPANY_ADDRESS}}</div></td>
<td class="doc-meta"><b>Electrical Safety Program</b><br>29 CFR 1910.331-.335 · NFPA 70E<br>Effective {{EFFECTIVE_DATE}}</td>
</tr></table>
<div class="oms-rule"></div><div class="oms-rule2"></div>

# Electrical Safety Program (NFPA 70E Aligned)
**Program administrator:** {{PROGRAM_ADMINISTRATOR}}, {{ADMIN_TITLE}} ({{ADMIN_PHONE}} / {{ADMIN_EMAIL}})

## 1. Purpose and Scope
This Electrical Safety Program establishes {{COMPANY_NAME}}'s safe electrical work practices in compliance with OSHA 29 CFR 1910.331–.335 and using NFPA 70E as the consensus method for shock and arc-flash protection. It applies to all employees and subcontractors who work on, near, or with electrical conductors or circuit parts operating at **50 volts or more**, whether energized or being placed in an electrically safe condition.

Scope of covered work at {{COMPANY_NAME}}: {{SCOPE}}.

The controlling principle of this program is simple: **work is performed de-energized.** Energized work is permitted only where de-energizing introduces a greater hazard or is infeasible, and only under a permit as described in Section 7.

## 2. Responsibilities
{{PROGRAM_ADMINISTRATOR}} ({{ADMIN_TITLE}}) owns this program: maintaining it, keeping the arc-flash study and equipment labels current, training and authorizing qualified persons, keeping the records in Section 11, and auditing the program per Section 12. Supervisors enforce it in the field and authorize energized-work permits. Employees follow it, use the required PPE, and stop work and report any condition outside the program.

## 3. Definitions — Qualified and Unqualified Persons
*(OSHA 1910.332; NFPA 70E 110.6)*

A **qualified person** has been trained and has demonstrated the skills and knowledge to identify and avoid the electrical hazards of the specific equipment and task, determine the nominal voltage, use the required PPE and insulated tools, and apply approach boundaries. Qualification is **task- and equipment-specific** — a person may be qualified on one system and unqualified on another. Only qualified persons may cross the restricted approach boundary or perform energized work.

An **unqualified person** must remain outside the limited approach boundary unless escorted by a qualified person and protected. 

[[List the job titles {{COMPANY_NAME}} treats as qualified persons, and how qualification is granted and revoked.]]

## 4. Establishing an Electrically Safe Work Condition (ESWC)
*(OSHA 1910.333; NFPA 70E 120; ties to LOTO under 1910.147)*

Before any circuit is treated as de-energized, a qualified person establishes and verifies an ESWC using these steps:

1. Determine **all** possible sources of supply (use current drawings and labels; watch for back-feeds and second sources).
2. Open the disconnecting device for each source; where possible, **visually verify** the contacts are open.
3. Release stored electrical energy (capacitors) and stored mechanical energy (springs).
4. Apply **lockout/tagout** per the company LOTO procedure.
5. **Test before touch:** using an adequately rated meter, prove the meter on a known live source, test all phases for absence of voltage, then prove the meter again.
6. Ground where induced voltage or stored energy is a hazard.

[[Describe {{COMPANY_NAME}}'s ESWC/LOTO procedure, the meter(s) used, and who is authorized to establish an ESWC. Reference your Lockout/Tagout program.]]

## 5. Shock Risk Assessment and Approach Boundaries
*(NFPA 70E 130.4)*

Before exposure to an energized part at 50 V or more, a qualified person performs a shock risk assessment: identify the nominal voltage, determine the **limited approach boundary** and the **restricted approach boundary** for that voltage, and select shock PPE (voltage-rated rubber insulating gloves with leather protectors, insulated tools).

> Boundary distances are specified by voltage in NFPA 70E Table 130.4(E)(a)/(b). Look up the distances for your voltage in your licensed copy of the current 70E — this program does not reproduce the table.

[[State how {{COMPANY_NAME}} looks up boundaries, the voltage classes of gloves stocked, and the glove test cycle (typically retested every 6 months).]]

## 6. Arc-Flash Risk Assessment and Incident Energy
*(NFPA 70E 130.5; IEEE 1584)*

Before work inside the arc-flash boundary, a qualified person performs an arc-flash risk assessment:

- Determine the **arc-flash boundary** — the distance at which incident energy equals **1.2 cal/cm²** (the onset of a second-degree burn).
- Determine the required PPE by **one** method, never mixed on the same task:
  - **(a) Incident-energy analysis** — calculate incident energy in cal/cm² (commonly via an IEEE 1584 engineering study) and select arc-rated PPE with an arc rating at or above it; or
  - **(b) The PPE-category (table) method** in NFPA 70E 130.7(C)(15).
- Account for equipment condition and maintenance — an un-maintained breaker that fails to clear quickly raises incident energy.

[[State whether {{COMPANY_NAME}} uses an incident-energy study or the PPE-category method, who performed/maintains the study, and the re-study trigger (system changes).]]

## 7. Energized Electrical Work Permit (EEWP)
*(NFPA 70E 130.2; OSHA 1910.333(a))*

Energized work inside the restricted approach or arc-flash boundary requires a written **Energized Electrical Work Permit** authorized by management **before** work begins. The permit documents the work, the **justification that de-energizing is infeasible or introduces a greater hazard** (production convenience is not a valid justification), the shock and arc-flash assessment results, the PPE, and a job briefing.

**Exemption:** testing, troubleshooting, and voltage measurement by a qualified person in appropriate PPE do not require a written EEWP, but still require the risk assessment and PPE.

[[State who at {{COMPANY_NAME}} may authorize an EEWP and where completed permits are filed. {{COMPANY_NAME}} uses the Energized Electrical Work Permit form maintained with this program.]]

## 8. Arc-Rated PPE and Insulated Tools
*(NFPA 70E 130.7; OSHA 1910.335)*

PPE is selected to the assessed hazard: arc-rated (AR) clothing/equipment with an arc rating (ATPV or EBT in cal/cm²) at or above the incident energy — or the ensemble for the selected PPE category — covering body, head, face (AR face shield with balaclava or arc-flash hood), and hands. Voltage-rated rubber insulating gloves with leather protectors are used for shock. **No meltable synthetics** (nylon, polyester, acetate) next to the skin. PPE is inspected before use; AR garments are kept clean and un-modified.

> PPE ensembles by category are in NFPA 70E Table 130.7(C)(15) — refer to your licensed copy; this program does not reproduce it.

[[List the AR clothing/PPE {{COMPANY_NAME}} issues, the inspection routine, and the rubber-goods test cycle.]]

## 9. Equipment Arc-Flash Labeling
*(OSHA 1910.303(e)-(f); NFPA 70E 130.5(H))*

Equipment likely to be examined, adjusted, serviced, or maintained while energized is field-labeled with nominal voltage, the arc-flash boundary, and at least one of: available incident energy with working distance, minimum arc rating of clothing, or the required PPE category. Labels are legible, durable, and updated when the study changes.

[[State who maintains labels at {{COMPANY_NAME}} and how re-labeling is triggered when the electrical system changes.]]

## 10. Job Briefing
*(NFPA 70E 110.5(H))*

Before energized work, the qualified person conducts a job briefing covering the hazards, the procedure and special precautions, the energy-source controls, the boundaries, the PPE, and the emergency response.

[[Attach or reference {{COMPANY_NAME}}'s job-briefing form.]]

## 11. Training
*Standard requirement:* Qualified persons are trained and must demonstrate the ability to identify and avoid energized parts, determine nominal voltage, use the required PPE and insulated tools, and apply approach boundaries. Unqualified persons are trained in the related practices necessary for their safety.

Retrain **at least every 3 years**, and whenever equipment or procedures change, when work practices are not being followed, or when an audit finds a deficiency. Employees exposed to shock hazards are trained in emergency response, including release of a victim and CPR.

[[State who trains, how often, the topics, how proficiency is demonstrated, and how training is documented (roster, certificates, LMS).]]

## 12. Recordkeeping, Retention, and Program Audit
Records kept: this written program; the arc-flash / incident-energy study and equipment-label schedule; completed EEWPs; qualified-person roster and training records with retrain-due dates; and PPE / rubber-goods inspection and test records.

**Program audit:** this program is audited at least **every 3 years**, and field work practices are observed at least **annually** (NFPA 70E 110.5(M)); deficiencies are corrected and drive retraining.

This program is reviewed at least annually and whenever operations, equipment, or the governing standards change. Last reviewed: {{LAST_REVIEW_DATE}}. Next review due: {{NEXT_REVIEW_DATE}}.

## 13. Reviewer Rejection Checklist — confirm NONE of these apply before submitting
- [ ] No arc-flash study and no PPE-category basis — boundaries are guessed
- [ ] Equipment not labeled with arc-flash boundary and incident energy (or PPE category)
- [ ] Energized work permitted without a permit or documented infeasibility justification
- [ ] Incident-energy method and PPE-category table method mixed on the same task
- [ ] Qualified-person training not documented or older than the 3-year retrain cycle
- [ ] No annual field observation / no 3-year program audit

## Management Certification
I certify that this Electrical Safety Program is implemented at {{COMPANY_NAME}} and reviewed on the cadence stated above.

Signature: ______________________________  
Name: {{SIGNATURE_NAME}}  
Title: {{SIGNATURE_TITLE}}  
Date: {{SIGNATURE_DATE}}
