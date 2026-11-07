# Team 10 — Close the Books

**Team:** Vitalii (lead and integrator), Luke Mininno, Ivan (eltriki-X), Marina Moncayola.
**Repository:** https://github.com/vitalio-sh/CLOSE-THE-BOOK. Final solution: `review/team-integ-r14` (8b00ac7).

## Problem
Grupo Valtermia has 17 companies and 12 currencies. Each half-year has about 210k bank lines and about 1M open invoices. For each bank line, the solver must say which invoices it pays and how much, or what the line is if it pays none. It must also flag 10 exception types.

The task is hard for three reasons:
- references are cut or mistyped;
- one payment can cover hundreds of invoices;
- key facts live only in PDFs and payment advices.

## How we worked
1. **Alone first.** Each member studied the challenge and built the best prototype they could.
2. **Fair comparison.** We built a test period from April to June 2025, with the correct answers. Every prototype ran on the same data and was scored by the official scorer, per segment. One prototype was best overall, and others were stronger in specific areas, such as direct debits or non-invoice lines.
3. **One area each.** Every member took ownership of one area of improvement. Vitalii coordinated with a Claude orchestrator that ran parallel agents, each focused on one thing, and tasks and results went through GitHub issues.
4. **Regular checks and integration.** Roughly every 90 minutes we reviewed what worked, what was wrong and what to improve next. The best parts were then integrated into a new version, in rounds r6 → r14. It was continuous iteration and learning.

**Credits in the final version:**
- Luke: adversarial validation of returned direct debits, MD06 tests and the bug they exposed (fixed in 6c29aaa).
- Marina Moncayola: entity-first payer filter (dc4690e). When several customers match, only those of the company that owns the bank account are ranked. For ES00 the payment-factory subsidiaries count too.
- Ivan: an independent pipeline, review findings and porting candidates.

## Solution
A **deterministic chain of rules**, from the strongest evidence to the weakest:
1. batches (direct debits, cards, cheques, payment runs);
2. payment advices;
3. matching, with exceptions;
4. PDF facts;
5. categories for lines that pay no invoice.

**Never force a match. No AI at run time.**

## Key insights
- **Documents first.** The score counts (line, invoice) pairs, so big documents (advices, batches) outweigh thousands of small lines.
- **Link globally before matching.** Advices arrive after the cash.
- **Abstain rather than guess.** Final precision is 99.8%.
- **Rare exceptions are cheap wins.** Each exception type is worth about 2 points.

## Results
- **Official LIVE:** 81.13 → 84.06 → 87.81 → 92.96 → 93.41 → 94.76 → 94.82 → 94.87 → **94.91 (rank 2)**.
- **Independent check on March 2025**, a month never tuned on (holdout, *not official*):
  - r9 93.51 → r11 94.20 → r14 94.31;
  - the r9 → r11 gain (+0.69) matched the official LIVE gain exactly;
  - r14 had no segment regressions.

  So the solution generalizes rather than overfits.
- The FINAL score is hidden until the reveal.

## Stack and how we used Claude
- **Solve time:** no LLM. Deterministic Python (standard library) plus `pdftotext`: fast and reproducible.
- **Building, testing and coordinating:**
  - Claude Code, with Claude Opus as the main coding agent and Claude Sonnet as read-only sub-agents;
  - Vitalii's Claude orchestrator ran parallel lanes, and a liaison agent reviewed the branches;
  - regression gates with the official scorer: nothing was kept unless it was measured;
  - ideas from any member were ported and credited within the hour (e.g. dc4690e).

## Next steps
- **MD06 direct-debit returns.** Linking refund requests to the right monthly collection.
- **English bank wording for ZA01.** For example, "BANK CHARGES" currently falls back to CUSTOMER_REFUND instead of BANK_FEES.
- **More PDF-only facts.**
