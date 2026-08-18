---
template_id: 06-written-program-skeleton
purpose: Fill-in skeleton for ANY OSHA/DOT written program. The agent inserts the matching KB entry's required_elements as section headers, then drafts company-specific content under each.
pulls_from:
  - <KB_ENTRY_ID>
placeholders:
  - COMPANY_NAME
  - PROGRAM_TITLE
  - CITATION
  - RESPONSIBLE_PERSON
  - RESPONSIBLE_TITLE
  - SCOPE
  - REQUIRED_ELEMENTS (from KB entry)
  - TRAINING_REQ (from KB entry)
  - RECORDKEEPING_REQ (from KB entry)
  - EFFECTIVE_DATE
grounding_rule: REQUIRED_ELEMENTS, TRAINING_REQ, and RECORDKEEPING_REQ must be copied verbatim from the matching KB entry's required_elements, training, and recordkeeping fields. Every element in the KB entry must appear as a section; before returning, check the draft against the entry's failure_points.
---

{{PROGRAM_TITLE}}
{{COMPANY_NAME}} — Written Safety Program
Governing standard: {{CITATION}}
Effective date: {{EFFECTIVE_DATE}}

1. Purpose and Scope
This program establishes {{COMPANY_NAME}}'s procedures to comply with {{CITATION}}. It applies to: {{SCOPE}}.

2. Responsibilities
Program administrator: {{RESPONSIBLE_PERSON}}, {{RESPONSIBLE_TITLE}}, is responsible for implementation, review, and recordkeeping.

3. Program Elements
For each required element below, describe the company-specific procedure, who performs it, and how it is documented:

{{REQUIRED_ELEMENTS}}

4. Training
{{TRAINING_REQ}}

5. Recordkeeping and Retention
{{RECORDKEEPING_REQ}}

6. Program Review
This program is reviewed at least annually and whenever operations, equipment, or {{CITATION}} change.
