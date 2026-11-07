# Video — read-aloud script (Team 10, under 5 minutes, 720p)

One scene per slide, in `docs/video_slides.html`; press → to move to the next slide. Read slowly, one sentence per breath. The script is about 600 words, so roughly 4 min 40 s at a calm pace.

---

## Slide 1 — Title (0:00–0:15)
> Hi, we are Team 10: Vitalii, Luke, Ivan and Marina.
> This is our solution for "Close the Books".

## Slide 2 — The problem (0:15–0:45)
> Every company must match its bank lines to its invoices.
> Today, accountants often do this by hand, in an ERP like SAP.
> Our fake group has seventeen companies and twelve currencies.
> Every six months: about two hundred ten thousand bank lines, and one million open invoices.
> For each bank line, we say which invoices it pays, and how much. Or what it is.
> And we flag ten types of exceptions.

## Slide 3 — Key facts live in documents (0:45–1:05)
> Here is why it is hard.
> On the left, an invoice. Ten percent is kept as a guarantee. This is written only in the PDF.
> On the right, a customer's payment advice. Thirty-seven invoices, and three deductions for penalties and marketing.
> The bank line says none of this.

## Slide 4 — One bank line, 37 invoices (1:05–1:25)
> This is one single bank line: one hundred nine thousand euros.
> Our solver reads the advice and splits it into thirty-seven invoices.
> And it flags the three disputed amounts.

## Slide 5 — Phase one: alone, then compare (1:25–2:00)
> We started individually.
> Each of us built the best prototype we could, alone.
> Then we compared them fairly: same test period with the correct answers, same official scorer.
> One prototype was clearly the best.
> But others were better in specific areas, like direct debits, or lines without an invoice.
> We discussed this, and we split the work.

## Slide 6 — Phase two: own an area, check often (2:00–2:45)
> Each of us took ownership of one area.
> Vitalii coordinated with a Claude orchestrator.
> Many agents worked in parallel, each on one thing.
> We used GitHub issues for tasks and results.
> About every ninety minutes, we checked together.
> Then we merged the best parts into a new version, up to version eighteen.
> Luke tested the returned direct debits, and found a bug. The fix is in the final version.
> Marina had the "entity-first" idea: look for the payer inside the right company first.
> Ivan built his own pipeline and reviewed our rules. The problems he found were fixed.

## Slide 7 — Key insights (2:45–3:20)
> One: the score counts pairs of bank line and invoice. So big documents, like batches and advices, count more than thousands of small lines. Documents first.
> Two: payment advices often arrive after the money. So we link everything first, and match after.
> Three: it is better to abstain than to guess. On the official live data, our precision was ninety-nine point eight two percent.
> Four: each exception class can be worth up to two points, so even rare ones matter.

## Slide 8 — How it works (3:20–3:50)
> The solution is a chain of clear rules.
> First, batches. Then payment advices.
> Then matching, with exceptions.
> Then facts from PDFs. And finally, categories for lines without an invoice.
> Two principles: never force a match.
> And no AI runs at solve time.

## Slide 9 — Results (3:50–4:20)
> On the live leaderboard, we reached ninety-four point nine one. Rank two.
> We also did an additional temporal validation on March 2025, a month we never used for tuning. This is not an official score.
> From version nine to version eleven, the gain was the same as on the official leaderboard.
> And version fourteen had no regressions.
> The final version, eighteen, is on the main branch.

## Slide 10 — How we used Claude (4:20–4:42)
> Claude built, tested and coordinated the solution.
> But at solve time, it is plain Python: fast and reproducible.
> One agent reviewed every branch.
> Every change was measured with the official scorer. Nothing was kept unless it was measured.

## Slide 11 — Next steps (4:42–4:55)
> Next, we would improve three things.
> Returned direct debits.
> English bank words for the new South African company.
> And more facts that only appear in PDFs.

## Slide 12 — Thank you (4:55–5:00)
> Thank you from Team 10!

---

### Pronunciation help
- **reconciliation**: re-con-si-li-AY-shun
- **orchestrator**: OR-kes-tray-tor
- **entity**: EN-ti-tee
- **precision**: pre-SI-zhun
- **ninety-four point nine one**: 94.91
