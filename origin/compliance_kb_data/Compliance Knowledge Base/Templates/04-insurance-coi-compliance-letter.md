---
template_id: 04-insurance-coi-compliance-letter
purpose: Letter from the contractor (or its broker) confirming the Certificate of Insurance and required endorsements meet the hiring client's contractual insurance requirements, naming the exact endorsement forms.
pulls_from:
  - acord-25-certificate-of-liability-insurance-certificate-of-insurance-coi-acord-25
  - iso-cg-00-01-occurrence-form-commercial-general-liability-cgl
  - iso-cg-20-10-ongoing-cg-20-37-completed-ops-additional-insured-endorsement-ai
  - iso-cg-20-01-primary-non-contributory-endorsement-p-nc
  - iso-cg-24-04-gl-wc-00-03-13-workers-comp-waiver-of-subrogation-wos
placeholders:
  - COMPANY_NAME
  - BROKER_NAME
  - DATE
  - HIRING_CLIENT
  - PROJECT_NAME
  - CGL_LIMITS
  - AUTO_LIMITS
  - WC_EL_LIMITS
  - UMBRELLA_LIMITS
  - CARRIER_AMBEST
  - POLICY_PERIOD
grounding_rule: Name the exact ISO/WC endorsement form numbers from the Insurance KB entries. Enforce the rule that the certificate alone is not proof — the endorsements must be attached. Never state Additional Insured applies to Workers' Compensation.
---

{{DATE}}

{{HIRING_CLIENT}}
RE: Evidence of Insurance and Endorsement Compliance — {{PROJECT_NAME}}

To Whom It May Concern:

On behalf of {{COMPANY_NAME}}, we confirm that the enclosed ACORD 25 Certificate of Liability Insurance and the attached policy endorsements satisfy the insurance requirements for {{PROJECT_NAME}}. Coverage is placed with a carrier rated {{CARRIER_AMBEST}} (A.M. Best) for the policy period {{POLICY_PERIOD}}.

Coverage summary:

- Commercial General Liability (ISO CG 00 01, occurrence): {{CGL_LIMITS}}
- Commercial Automobile Liability: {{AUTO_LIMITS}}
- Workers' Compensation & Employer's Liability: {{WC_EL_LIMITS}}
- Umbrella / Excess Liability (follow-form): {{UMBRELLA_LIMITS}}

Required endorsements attached (not merely referenced on the certificate):

- Additional Insured — Ongoing Operations: ISO CG 20 10
- Additional Insured — Products/Completed Operations: ISO CG 20 37
- Primary and Non-Contributory: ISO CG 20 01
- Waiver of Subrogation (General Liability): ISO CG 24 04
- Waiver of Subrogation (Workers' Compensation): WC 00 03 13
- Advance notice of cancellation to the certificate holder as required by contract

{{HIRING_CLIENT}} is named as Additional Insured on the General Liability, Automobile, and Umbrella policies, on a primary and non-contributory basis, with waiver of subrogation, except that Additional Insured status does not apply to Workers' Compensation (where the risk transfer is the WC 00 03 13 waiver of subrogation).

Please contact the undersigned broker with any questions.

{{BROKER_NAME}}
On behalf of {{COMPANY_NAME}}
