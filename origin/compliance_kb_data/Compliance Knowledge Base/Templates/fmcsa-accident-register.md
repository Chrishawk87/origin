---
template_id: fmcsa-accident-register
type: form
pulls_from:
  - 49-cfr-part-390-usdot-registration-mcs-150-maintenance
citation: "49 CFR 390.15 (Accident Register)"
placeholders:
  - COMPANY_NAME
  - COMPANY_ADDRESS
grounding_rule: >
  49 CFR 390.15(b) requires a motor carrier to maintain an accident register listing DOT-recordable
  accidents. An accident is DOT-recordable (390.5) if it involved a CMV on a highway and resulted in
  a fatality, a bodily injury treated away from the scene, or a vehicle towed from the scene due to
  disabling damage. Keep the register plus copies of any accident reports for 3 years after each
  accident. Do not alter the required columns; fill only the blanks.
---

<table class="oms-lh"><tr>
<td style="width:64%"><div class="client-name">{{COMPANY_NAME}}</div><div class="client-addr">{{COMPANY_ADDRESS}}</div></td>
<td class="doc-meta"><b>Accident Register</b><br>49 CFR 390.15(b)<br>Period: ________</td>
</tr></table>
<div class="oms-rule"></div><div class="oms-rule2"></div>

# Accident Register (49 CFR 390.15)

Carrier: {{COMPANY_NAME}}  ·  USDOT #: ____________

*Record every DOT-recordable accident. An accident is recordable when a CMV on a highway is involved and there is a fatality, an injury requiring treatment away from the scene, or a vehicle towed from the scene due to disabling damage (390.5). Keep this register and copies of related reports for **3 years** after each accident.*

| Date | Location (city / state) | Driver name | Fatalities | Injuries | Hazmat released? (other than fuel from the CMV's own tank) |
|---|---|---|---|---|---|
|  |  |  |  |  | ☐ Y ☐ N |
|  |  |  |  |  | ☐ Y ☐ N |
|  |  |  |  |  | ☐ Y ☐ N |
|  |  |  |  |  | ☐ Y ☐ N |
|  |  |  |  |  | ☐ Y ☐ N |

*Attach to each entry: copies of accident reports required by state or other governmental entities, or insurers.*

Maintained by: ____________________________  Title: ____________  Date: __________
