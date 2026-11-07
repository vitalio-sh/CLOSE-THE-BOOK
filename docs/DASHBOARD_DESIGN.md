# Reconciliation dashboard: design research and implementation brief

Reviewed 7 November 2026. Scope: a local, read-only financial-operations interface for real reconciliation output, with transaction inspection and evidence. This is a product design brief, not a claim of SAP/IBM compliance.

## Actual skill versus industry guidance

**Actual agent skill read in full:** Anthropic's official [frontend-design / SKILL.md](https://raw.githubusercontent.com/anthropics/skills/main/skills/frontend-design/SKILL.md), from the [anthropics/skills repository](https://github.com/anthropics/skills/tree/main/skills/frontend-design). It asks for subject-specific design, a compact palette/type/layout plan before implementation, real content, restrained visual emphasis, purposeful interaction, plain action labels, keyboard focus and screenshot-based self-critique. It explicitly treats generic card grids, decorative gradients, automatic reveal animations and fashionable typography as defaults to question. This is broad design judgement, not a specialized accounting workflow skill. Read as reference; nothing was installed or executed.

**Industry design guidelines, not installable skills:**

- [SAP Fiori Flexible Column Layout](https://www.sap.com/design-system/fiori-design-web/v1-136/page-types/page-layouts/flexible-column-layout-web-component/): appropriate for a list → detail relationship. A selected transaction and its details fit. The same pattern is not intended for unrelated dashboard pages or splitting one object arbitrarily across columns. On narrow screens the detail becomes a single full-width view.
- [SAP Reprocess Bank Statement Items](https://help.sap.com/docs/PRODUCT_ID/918bca53037f408f91a2295d04ac16bc/bc1fe656fe590950e10000000a44147b.html): a real finance workflow separates automatic processing from a worklist of items needing reprocessing, with item status, reasons and attachments. Our read-only demo can explain these states; it must not pretend to post or clear ledger entries.
- [IBM Carbon data table guidelines](https://www.carbondesignsystem.com/building-blocks/core/components/data-table/guidelines): tables support sorting, search/filter toolbar, pagination and progressive disclosure of details. Give dense data enough width; don't bury tables in cramped nested cards. Keep row/header heights consistent. Expand short supplementary content; use a detail view when content becomes substantial.
- [Carbon data-table keyboard guidance](https://v10.carbondesignsystem.com/components/data-table/accessibility/): sorting controls are keyboard reachable and activated by Enter/Space; interactive controls retain normal tab order. This is older-version guidance, used for interaction principles only, not current component API claims.

No random skill marketplace package, script, dependency, or global agent configuration was installed. No claim that SAP or IBM publishes the specialized agent skill we need.

## Recommended product direction — our synthesis

Audience: a treasury analyst or hackathon reviewer asking three questions:

1. What did the run process, and what still needs investigation?
2. Why was this particular payment assigned to these invoices?
3. What is known to be correct, and what is only the engine's proposed result?

The screen should behave like an operations console. Its distinctive element is an inspectable payment-to-documents explanation, not a large promotional headline or invented business metric.

### Information hierarchy

```text
Close the Books       Dataset: LIVE       Run timestamp      CSV / Run report
Run facts: 203,083 processed | assigned to items | categorized | unresolved
[Transactions] [Processing steps] [Measured quality]

Search transaction / document / counterparty   Entity   Currency   Outcome
Transaction queue                            Selected transaction
Date / party / reference / signed amount      Bank line + full source text
Outcome / number of allocations               Allocations, currency, exception
Pagination and dataset scope                  Evidence + reason + limitations
```

Exact counts above are placeholders for layout only; renderer must obtain them from the selected run, not copy this brief. Dataset-wide counts and the visible representative subset must have distinct labels.

### Workflow and honest states

- Use `Assigned to documents`, `Categorized`, `Unidentified` for engine outcomes. `Reconciled correctly` requires comparison with labels, and therefore belongs only to labeled evaluation data.
- A structurally valid CSV is not proof of accounting correctness. Show `Format checks passed` separately from measured accuracy.
- LIVE and FINAL do not have local golden labels. Display `Official score not available` unless a real server response exists; never fill in a sample score as LIVE score.
- Do not show a confidence percentage without a calibrated scoring mechanism. A named rule and a source file are better evidence than an invented gauge.
- A representative transaction set is a demo selection, not the whole searchable database. State `Showing N representative transactions from the run`. Filter counts must refer to the actually loaded set.
- Hide actions that do not exist. A static page may open details or download prepared output; it must not show fake `Approve`, `Re-run`, `Post to ledger`, or `Submit FINAL` buttons.
- Preserve selected transaction and filters when switching between overview and drilldown, unless a user explicitly changes dataset.

### Transaction detail

Top: bank transaction ID, booking date, entity/account, signed bank amount and ISO currency. Follow with the complete bank description in a readable wrapping block.

Allocation table: printed document number, internal item ID, party, allocated amount, item currency, exception. Explain that allocation amounts are positive even for credit notes/returns. A payment may have several allocation rows.

Evidence: module/rule that actually produced the result; document/registry path if recorded; source facts. A reconstructed explanation from final rows must say `Explanation from recorded allocations`, not falsely claim an audited decision trace. If the trace only names a module, state that detailed evidence was not captured.

For FX, present bank and item currencies separately. For credit notes, show their economic effect without changing the positive amount required by the submission format. Avoid a naive `bank amount − sum(allocation amounts)` difference for mixed currencies, credit notes, accepted discounts, or card settlements.

### Numbers and totals

- Right-align amounts; use tabular numerals and consistent two-decimal formatting. Put ISO currency beside an amount or in a clearly associated column.
- Group aggregates by currency. Never sum EUR, USD and PLN into a single unlabeled money total.
- Distinguish signed net flow from absolute transaction volume. Negative cash is not automatically an error and should not automatically appear red.
- Show measured quality with phase, evaluation window, sample size and run version. Development sample and independent holdout are separate observations.
- Counts and percentages use an explicit denominator. `100% covered` means every transaction has an output row; it does not mean 100% correct matching.

### Visual direction

Suggested palette: white surface `#FFFFFF`, cool canvas `#F3F5F7`, text `#17232E`, subdued text `#54616D`, action/selection blue `#175CD3`, border `#D7DEE5`. Outcome colors are semantic and accompanied by text; reserve warning/red for actual warnings or validation failures. Check actual contrast after implementation.

Use one readable sans-serif family, system fonts for local/offline reliability; avoid extra font downloads for this prototype. Body/table text 14–16px; restrained title 24–28px. Data IDs can use monospace selectively, not every label. Use a 40px row rhythm as a starting point, with more room for touch layouts. One selected-row treatment and a modest 6px corner radius are sufficient. No hero illustration, ambient gradients, oversized empty cards, floating ornaments or page-load choreography.

## Acceptance / QA checklist

- Can a reviewer identify dataset, run time, scope and unresolved count within five seconds?
- Can they find and open a transaction, then explain its invoice allocation using visible evidence?
- Can keyboard-only users search, filter, sort, open a detail and return to the queue without lost focus?
- Do empty search results offer a clear filter reset? Does missing evidence have an explicit neutral message?
- Are long IDs, multilingual names, negative amounts, multi-invoice payments and FX cases readable without silent truncation?
- Is detail usable on a laptop and a narrow/mobile viewport, without an unreadable miniature table?
- Are totals separated by currency, and labels clear about processed versus correctly matched?
- Are LIVE/FINAL score, confidence, approval, runtime status and evidence all factual?
- Do process steps describe the actual pipeline rather than a fabricated animated live execution?
- Do counts, selected rows, downloadable CSV and run report refer to the same run?
- Is screen-reader structure semantic (main/nav/table/headings/buttons), with visible focus and status conveyed by text as well as color?
- Inspect screenshots at desktop and narrow widths, and remove decorative elements that distract from those tasks.
