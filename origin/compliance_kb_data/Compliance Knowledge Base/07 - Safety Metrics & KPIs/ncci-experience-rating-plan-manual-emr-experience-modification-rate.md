---
id: ncci-experience-rating-plan-manual-emr-experience-modification-rate
title: EMR — Experience Modification Rate
category: 07 - Safety Metrics & KPIs
citation: NCCI Experience Rating Plan Manual
written_program: Reference
agencies: ISN: Y, Avetta: Y, Veriforce: Y, PEC: Y
source: https://www.ncci.com/Articles/Documents/UW_ABC_Exp_Rating.pdF
template: 
---

# EMR — Experience Modification Rate

**Citation:** NCCI Experience Rating Plan Manual
**Category:** 07 - Safety Metrics & KPIs
**Written program required:** Reference
**Agencies commonly requiring (Y=commonly, C=client/scope-driven):** ISN: Y, Avetta: Y, Veriforce: Y, PEC: Y

## Applicability / Trigger

A workers' compensation premium multiplier assigned to any employer whose payroll/premium exceeds the state eligibility threshold. Compares an employer's ACTUAL losses to EXPECTED losses for its class codes over a 3-year experience period (lagged ~1 year, most recent policy year excluded). 1.00 = industry-average; below 1.00 = credit (safer than peers), above 1.00 = debit. Rated by NCCI in ~35+ states; independent bureaus rate the rest (e.g., California/WCIRB, New York/NYCIRB, Pennsylvania, Delaware, Michigan, Minnesota, New Jersey, Wisconsin, and monopolistic states OH/ND/WA/WY).

## Formula

Mod = ( Ap + W·Ae + B ) / ( Ep + W·Ee + B )
  Ap = actual primary losses (capped at split point)
  Ae = actual excess losses (above split point)
  Ep = expected primary losses (= expected losses × D-ratio)
  Ee = expected excess losses (= expected losses − expected primary)
  W  = weighting value (rises with employer size)
  B  = ballast value (stabilizes toward 1.00)
Expected losses by class = (Payroll ÷ 100) × ELR, summed across all class codes.
NCCI's simplified/split-plan form (post-2017): Mod ≈ ( Ap + Ee ) ÷ E, where the primary/excess weighting is pre-built into the bureau's limited rating factors.

## Worked Example

Employer with $200,000 expected losses; $88,100 actual primary and $75,640 actual excess (total actual $163,740); W = 0.16, B = $45,900. Plug primary at full weight and excess at the weighted rate into the formula → a mod near or below 1.00 yields a premium credit. A $100,000 manual premium × EMR 0.85 = $85,000 (saves $15,000/yr); × EMR 1.30 = $130,000.

## Required Elements / Components

- Payroll by class code for the 3-year experience period
- Expected Loss Rate (ELR) per $100 payroll, published by the rating bureau per class code
- D-Ratio (discount ratio) per class code — splits expected losses into primary vs. excess
- Split point — the per-claim dollar cap dividing each claim into primary (capped, full weight) and excess (dampened) loss
- Actual incurred losses, split into primary and excess (medical-only claims reduced to 30% of primary)
- Weighting value (W) and Ballast value (B) from the bureau credibility tables, based on total expected losses

## Benchmarks / Standards

1.00 = industry average. Many oil & gas / construction operators require EMR < 1.00 to bid; some petrochemical owners cut off at 1.0 hard, others allow up to ~1.25 with a corrective plan. Below 0.80 is considered excellent. EMR is one of the three stats (with TRIR and DART) that prequalification platforms score.

## Common Failure Points (why reviewers reject it)

- Payroll misclassified to a higher-hazard class code, inflating expected losses
- Open/reserved claims carried at inflated reserves not reviewed before unit-stat filing
- Failing to dispute unit statistical data errors within the correction window
- Claim frequency (many small claims) — hurts more than one large claim because primary losses carry full weight
- Submitting an EMR without the matching rating-bureau worksheet when a hiring client asks for verification

## Authoritative Source

https://www.ncci.com/Articles/Documents/UW_ABC_Exp_Rating.pdF

## Notes for the Agent

EMR is calculated by the rating bureau/carrier, not self-reported — but contractors must UNDERSTAND it to (a) reduce it and (b) explain an elevated mod to a hiring client. Reduction levers: aggressive return-to-work/light-duty (converts lost-time claims to medical-only), claims management and reserve review, safety program that cuts FREQUENCY, and auditing payroll class codes. The single most impactful because frequency drives primary losses.
