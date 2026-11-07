# Video — read-aloud script (Team 10, about 4.5 minutes, 720p)

Read slowly. One sentence, one breath. About 560 words, so roughly 4.5 minutes at a calm pace.

---

## Scene 1 — Intro and problem (0:00–0:40)
**Show:** a title slide ("Team 10 — Close the Books" and the four names), then a few bank lines next to a list of invoices.

**Say:**
> Hi, we are Team 10: Vitalii, Luke, Ivan and Marina.
> Our challenge was bank reconciliation.
> A fake international group has seventeen companies, twelve currencies and thirteen banks.
> There are almost two million bank movements.
> For each movement, we must find the invoices it pays.
> We must explain any difference, like a discount, a bank fee or a currency change.
> And if we cannot identify a payment, we must say so, not guess.

## Scene 2 — Phase one: four prototypes (0:40–1:25)
**Show:** the GitHub branch list (one branch per person), then the comparison table from `docs/TEAM_UPDATE_1500.md`.

**Say:**
> We started individually.
> Each of us studied the challenge and built the best prototype we could, alone.
> Then we compared the prototypes.
> To compare them fairly, we built a test period with the correct answers: April to June 2025.
> Every prototype ran on the same data, with the same official scorer.
> We saw clear strengths and weaknesses.
> One prototype was best overall.
> Others were better on direct debits, or on lines without an invoice.
> We discussed this and decided how to split the work.

## Scene 3 — Phase two: split, coordinate, check (1:25–2:30)
**Show:** Vitalii's orchestrator screen, the GitHub issue with task assignments, and the list of integration rounds r6 to r14.

**Say:**
> Everyone took ownership of one area of improvement.
> Vitalii coordinated with a Claude orchestrator.
> It ran many agents in parallel.
> Each agent focused on one thing only.
> We used GitHub issues to assign tasks and share results.
> About every hour and a half, we checked together.
> What works? What is wrong? What do we improve next?
> Then we merged the best parts into a new version. We did this fourteen times.
> Some examples.
> Luke wrote tests and checked the returned direct debits. He found a bug, and the fix is in the final version.
> Marina had the "entity-first" idea: look for the payer inside the right company first. So a customer abroad with the same name cannot win.
> Ivan built an independent pipeline and reviewed our rules. The problems he found were fixed.
> It was continuous iteration and learning.

## Scene 4 — How the solution works (2:30–3:25)
**Show:** a simple diagram:

```
batches → payment advices → matching with exceptions → PDFs → categories
```

**Say:**
> The solution is a chain of clear rules.
> First, batches: one bank line can contain hundreds of payments, and we open them.
> Second, payment advices from customers, sometimes arriving days later.
> Third, matching by invoice number, payer name or bank account. Here we also flag exceptions, like partial payments or returned payments.
> Fourth, facts that only appear in PDFs, like instalments or replaced invoices.
> Fifth, a category for lines without an invoice, like bank fees or taxes.
> Our main rule: never force a match. If we are not sure, we do not guess.
> And no AI model runs during the solution. It is plain Python, plus a tool that reads PDFs.
> We used Claude to build it, test it and coordinate the team.

## Scene 5 — Results (3:25–4:05)
**Show:** the LIVE scores over the day, then the leaderboard at second place.

```
81.13 → 84.06 → 87.81 → 92.96 → 93.41 → 94.76 → 94.82 → 94.87 → 94.91
```

**Say:**
> On the live leaderboard, we started at eighty-one.
> We finished at ninety-four point nine one, in second place.
> We also checked one more month, March 2025, that we never used for tuning.
> There, the score improved by the same amount as on the leaderboard. So we did not just learn the test by heart.
> Then we froze the code.
> Before the final dataset, we tested a fake new company in South African rand. The solution ran without problems.

## Scene 6 — Next steps and close (4:05–4:35)
**Show:** four short points, then the GitHub link and the team names.

**Say:**
> With more time, we would do four things.
> One: measure how many returned direct debits our newest rule fixes.
> Two: link late payment advices better.
> Three: handle payments with messy references.
> Four: teach the categories English bank words, for the new South African company.
> Thank you from Team 10!

---

### Pronunciation help
- **reconciliation**: re-con-si-li-AY-shun
- **orchestrator**: OR-kes-tray-tor
- **ninety-four point nine one**: 94.91
- **entity**: EN-ti-tee
