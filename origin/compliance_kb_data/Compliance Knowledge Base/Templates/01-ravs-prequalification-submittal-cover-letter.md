---
template_id: 01-ravs-prequalification-submittal-cover-letter
purpose: Cover letter transmitting a contractor's safety program package to a prequalification reviewer (ISN RAVS, Avetta, Veriforce, PEC) or a hiring client.
pulls_from:
  - isnetworld-ravs-isnetworld-ravs-review-process
  - avetta-avetta-prequalification-process
  - veriforce-pec-veriforce-pec-prequalification-process
placeholders:
  - COMPANY_NAME
  - COMPANY_ADDRESS
  - CONTACT_NAME
  - CONTACT_TITLE
  - CONTACT_EMAIL
  - CONTACT_PHONE
  - DATE
  - PLATFORM
  - HIRING_CLIENT
  - PROGRAM_LIST
  - EMR_VALUE
  - TRIR_3YR
  - DART_3YR
grounding_rule: Every program in PROGRAM_LIST must exist as a KB entry and be cited by its exact citation. Never state an agency 'approved' a template; state the program 'meets the requirements of [citation].'
---

{{DATE}}

{{PLATFORM}} Review Team
RE: Contractor Safety Program Submission — {{COMPANY_NAME}} (for {{HIRING_CLIENT}})

To the Review Team:

{{COMPANY_NAME}} respectfully submits the enclosed written health and safety programs and supporting documentation for review under the {{PLATFORM}} prequalification process. Each program has been written to meet the requirements of its governing OSHA/DOT standard and the requirements communicated by {{HIRING_CLIENT}}.

Enclosed for your review:

{{PROGRAM_LIST}}

Supporting safety performance data:

- Experience Modification Rate (EMR): {{EMR_VALUE}}
- Total Recordable Incident Rate (TRIR), last three years: {{TRIR_3YR}}
- DART Rate, last three years: {{DART_3YR}}
- OSHA 300A summaries and a Certificate of Insurance with required endorsements are included in the package.

Each written program identifies the responsible person, the training and recordkeeping obligations, and the specific regulatory citation it satisfies. We have addressed every required element in full so the program can be verified against the underlying standard on first review.

Please contact me directly with any questions, and we will respond promptly to keep the review on schedule.

Respectfully,

{{CONTACT_NAME}}
{{CONTACT_TITLE}}, {{COMPANY_NAME}}
{{CONTACT_EMAIL}} | {{CONTACT_PHONE}}
{{COMPANY_ADDRESS}}
