---
template_id: 02-written-program-certification-statement
purpose: Signed management certification page attached to the front of any written safety program, affirming it is implemented and reviewed. Reviewers look for an accountable signature and a review date.
pulls_from:
  - 29-cfr-1904-recording-and-reporting-occupational-injuries-and-illnesses
placeholders:
  - COMPANY_NAME
  - PROGRAM_TITLE
  - CITATION
  - RESPONSIBLE_PERSON
  - RESPONSIBLE_TITLE
  - EFFECTIVE_DATE
  - LAST_REVIEW_DATE
  - NEXT_REVIEW_DATE
  - SIGNATURE_NAME
  - SIGNATURE_TITLE
grounding_rule: CITATION must be the exact citation of the matching KB entry. The certification must name a real accountable person and a review cadence; do not leave the responsible-person field generic.
---

CERTIFICATION OF WRITTEN SAFETY PROGRAM

Program: {{PROGRAM_TITLE}}
Governing standard: {{CITATION}}
Company: {{COMPANY_NAME}}

{{COMPANY_NAME}} certifies that the above written program has been developed to meet the requirements of {{CITATION}}, has been implemented at all applicable operations, and is communicated to affected employees through the training described within it.

Program administrator responsible for implementation and maintenance: {{RESPONSIBLE_PERSON}}, {{RESPONSIBLE_TITLE}}.

Effective date: {{EFFECTIVE_DATE}}
Last reviewed: {{LAST_REVIEW_DATE}}
Next scheduled review: {{NEXT_REVIEW_DATE}}

This program is reviewed at least annually and whenever operations, equipment, or the governing regulation change.

Certified by:

_______________________________
{{SIGNATURE_NAME}}, {{SIGNATURE_TITLE}}
{{COMPANY_NAME}}
