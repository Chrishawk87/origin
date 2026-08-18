---
template_id: 05-incident-rate-summary-letter
purpose: Letter presenting three-year TRIR/DART performance to a hiring client, showing the calculation and benchmarking against the applicable BLS/NAICS average.
pulls_from:
  - 29-cfr-1904-recordability-bls-soii-benchmarks-trir-total-recordable-incident-rate
  - 29-cfr-1904-7-bls-soii-benchmarks-dart-days-away-restricted-or-transferred-rate
placeholders:
  - COMPANY_NAME
  - CONTACT_NAME
  - CONTACT_TITLE
  - DATE
  - HIRING_CLIENT
  - NAICS_CODE
  - NAICS_TRIR_BENCHMARK
  - YEAR1
  - Y1_CASES
  - Y1_DART
  - Y1_HOURS
  - Y1_TRIR
  - Y1_DARTRATE
  - YEAR2
  - Y2_TRIR
  - Y2_DARTRATE
  - YEAR3
  - Y3_TRIR
  - Y3_DARTRATE
grounding_rule: Use hours actually worked, not scheduled hours, and reconcile to the 300A. Pull the CURRENT BLS benchmark for the specific NAICS at generation time; do not hardcode a stale figure.
---

{{DATE}}

{{HIRING_CLIENT}} — Contractor Prequalification
RE: Three-Year Safety Performance Summary — {{COMPANY_NAME}}

To Whom It May Concern:

{{COMPANY_NAME}} submits its OSHA incident-rate performance for the three most recent complete years. All rates are calculated from our OSHA 300/300A logs using the standard formula (cases x 200,000 / hours actually worked).

| Year | TRIR | DART |
|------|------|------|
| {{YEAR1}} | {{Y1_TRIR}} | {{Y1_DARTRATE}} |
| {{YEAR2}} | {{Y2_TRIR}} | {{Y2_DARTRATE}} |
| {{YEAR3}} | {{Y3_TRIR}} | {{Y3_DARTRATE}} |

Sample calculation ({{YEAR1}}): ({{Y1_CASES}} recordable cases x 200,000) / {{Y1_HOURS}} hours = {{Y1_TRIR}} TRIR. DART cases {{Y1_DART}} yield a DART rate of {{Y1_DARTRATE}}.

Benchmark: our operations fall under NAICS {{NAICS_CODE}}, for which the most recent BLS Survey of Occupational Injuries and Illnesses reports an industry-average TRIR of approximately {{NAICS_TRIR_BENCHMARK}}. Our rates are presented for direct comparison against that benchmark.

These figures reconcile to the OSHA 300A summaries included in our submission. We are happy to provide the underlying logs on request.

Respectfully,

{{CONTACT_NAME}}
{{CONTACT_TITLE}}, {{COMPANY_NAME}}
