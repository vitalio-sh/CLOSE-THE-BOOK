# Video — read-aloud script (Team 10, about 4 minutes, 720p)

Each scene goes with one slide in `docs/video_slides.html`; press → to move to the next one. Read slowly, one sentence per breath. The script is about 520 words, so roughly 4 minutes.

---

## Slide 1 — Title (0:00–0:10)
> Hi, we are Team 10: Vitalii, Luke, Ivan and Marina.
> This is our solution for "Close the Books".

## Slide 2 — The problem (0:10–0:35)
> Every company must match its bank lines to its invoices.
> Today, accountants often do this by hand, in an ERP like SAP.
> Our fake group has seventeen companies and twelve currencies.
> Every six months: about two hundred ten thousand bank lines, and one million open invoices.
> For each line, we say which invoices it pays, and how much. Or what it is.
> And we flag ten types of exceptions.

## Slide 3 — Key facts live in documents (0:35–0:55)
> Why is it hard?
> On the left, an invoice. Ten percent is kept as a guarantee. Only the PDF says this.
> On the right, a payment advice from a customer: thirty-seven invoices and three deductions.
> The bank line says none of this.

## Slide 4 — One bank line, 37 invoices (0:55–1:10)
> This is one single bank line: one hundred nine thousand euros.
> Our solver reads the advice, splits the line into thirty-seven invoices, and flags the three disputes.

## Slide 5 — Phase one: alone, then compare (1:10–1:35)
> We started individually.
> Each of us built the best prototype we could, alone.
> Then we compared them fairly: same test period with the correct answers, same official scorer.
> One prototype was the best overall, but others were better in specific areas.
> So we split the work.

## Slide 6 — Phase two: own an area, with Claude (1:35–2:25)
> Each of us took ownership of one area.
> Vitalii coordinated with a Claude orchestrator. Many agents worked in parallel, each on one thing.
> One agent reviewed every branch.
> About every ninety minutes, we checked together, and merged the best parts into a new version.
> Every change was measured with the official scorer. Nothing was kept unless it was measured.
> Claude built and tested the solution. But at solve time there is no AI: just Python, fast and reproducible.
> Luke tested the returned direct debits and found a bug. The fix is in the final version.
> Marina had the "entity-first" idea: look for the payer inside the right company first.
> Ivan built his own pipeline and reviewed our rules.

## Slide 7 — Key insights (2:25–3:00)
> We learned four things.
> One: the score counts pairs of bank line and invoice. So big documents, like batches and advices, matter most. Documents first.
> Two: advices often arrive after the money. So we link everything first, and match after.
> Three: abstain rather than guess. On the official live data, our precision was ninety-nine point eight two percent.
> Four: each exception class can be worth up to two points, so even rare ones matter.

## Slide 8 — How it works (3:00–3:25)
> The solution is a chain of clear rules.
> Batches, then payment advices, then matching with exceptions, then facts from PDFs, and finally categories.
> Our main rule: never force a match.

## Slide 9 — Results (3:25–3:45)
> The final version, on the main branch, reached ninety-five point two seven on the official live leaderboard.
> That is in the top three.
> We also did an additional temporal validation on March 2025, a month we never used for tuning. It is not an official score.

## Slide 10 — Next steps (3:45–3:57)
> Next, we would improve returned direct debits, English bank words for the new South African company, and more facts from PDFs.

## Slide 11 — Thank you (3:57–4:00)
> Thank you from Team 10!

---

### Pronunciation help
- **reconciliation**: re-con-si-li-AY-shun
- **orchestrator**: OR-kes-tray-tor
- **entity**: EN-ti-tee
- **precision**: pre-SI-zhun
- **ninety-five point two seven**: 95.27
