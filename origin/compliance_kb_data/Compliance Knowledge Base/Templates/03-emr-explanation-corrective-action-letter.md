---
template_id: 03-emr-explanation-corrective-action-letter
purpose: Letter explaining an elevated Experience Modification Rate to a hiring client and documenting the corrective actions in progress. Used when EMR exceeds the client's threshold (often 1.00).
pulls_from:
  - ncci-experience-rating-plan-manual-emr-experience-modification-rate
placeholders:
  - COMPANY_NAME
  - CONTACT_NAME
  - CONTACT_TITLE
  - DATE
  - HIRING_CLIENT
  - EMR_VALUE
  - EMR_YEAR
  - DRIVING_CLAIMS_SUMMARY
  - CORRECTIVE_ACTIONS
  - RTW_PROGRAM_STATUS
  - PROJECTED_EMR
grounding_rule: Describe EMR mechanics exactly as in the EMR KB entry (primary/excess, 3-year lagged period, frequency drives primary losses). Do not fabricate a projected EMR figure; use the value the user supplies or a stated range.
---

{{DATE}}

{{HIRING_CLIENT}} — Contractor Prequalification
RE: Experience Modification Rate — {{COMPANY_NAME}}

To Whom It May Concern:

Our current Experience Modification Rate is {{EMR_VALUE}} for the {{EMR_YEAR}} rating period. We are providing this letter to explain the factors behind the current mod and the corrective actions {{COMPANY_NAME}} has implemented.

The EMR compares our actual workers' compensation losses to the expected losses for our classification codes over a three-year experience period (lagged one year). The current rating is primarily driven by: {{DRIVING_CLAIMS_SUMMARY}}. Because the experience rating formula weights claim frequency through primary losses, our corrective focus is on eliminating recurrence and managing open claims.

Corrective actions in place:

{{CORRECTIVE_ACTIONS}}

Return-to-work / light-duty program: {{RTW_PROGRAM_STATUS}}. This program converts would-be lost-time claims to medical-only where medically appropriate, which reduces both DART and future primary losses.

As the affected policy years roll out of the experience period and these controls take effect, we project the mod to move toward {{PROJECTED_EMR}}. We are committed to safe execution on {{HIRING_CLIENT}}'s work and welcome a discussion of our safety management system.

Respectfully,

{{CONTACT_NAME}}
{{CONTACT_TITLE}}, {{COMPANY_NAME}}
