# Video — read-aloud script (Team 10, under 5 minutes, 720p)

One scene per slide, in `docs/video_slides.html`; press → to move to the next slide. Read slowly, one sentence per breath. The script is about 590 words, so roughly 4 min 30 s at a calm pace.

---

## Slide 1 — Title (0:00–0:15)
> Hi, we are Team 10: Vitalii, Luke, Ivan and Marina.
> This is our solution for "Close the Books".

## Slide 2 — The problem (0:15–0:50)
> The challenge is bank reconciliation.
> A fake international group has seventeen companies and twelve currencies.
> Every six months there are about two hundred ten thousand bank lines, and about one million open invoices.
> For each bank line, we must say which invoices it pays, and how much.
> Or, if it pays no invoice, what it is.
> We must also flag ten types of exceptions.
> It is hard: references are cut or wrong.
> One payment can cover hundreds of invoices.
> And some key facts are only in PDFs and payment advices.

## Slide 3 — Phase one: alone, then compare (0:50–1:30)
> We started individually.
> Each of us studied the challenge and built the best prototype we could, alone.
> Then we compared the prototypes, in a fair way.
> We built a test period with the correct answers: April to June 2025.
> Every prototype ran on the same data, with the official scorer.
> One prototype was clearly the best.
> But others were better in specific areas, like direct debits, or lines without an invoice.
> We discussed this, and we split the work.

## Slide 4 — Phase two: own an area, check often (1:30–2:20)
> Each of us took ownership of one area.
> Vitalii coordinated with a Claude orchestrator.
> Many agents worked in parallel, each on one thing.
> We used GitHub issues for tasks and results.
> About every ninety minutes, we checked together.
> What works? What is wrong? What next?
> Then we merged the best parts into a new version, up to version fourteen.
> Luke tested the returned direct debits, and found a bug. The fix is in the final version.
> Marina had the "entity-first" idea: look for the payer inside the right company first.
> Ivan built his own pipeline and reviewed our rules. The problems he found were fixed.

## Slide 5 — Key insights (2:20–3:00)
> We learned four important things.
> One: the score counts pairs of bank line and invoice. So big documents, like batches and advices, count more than thousands of small lines. Documents first.
> Two: payment advices often arrive after the money. So we link everything first, and match after.
> Three: it is better to abstain than to guess. Our precision is ninety-nine point eight percent.
> Four: each exception type is worth about two points, so even rare ones matter.

## Slide 6 — How it works (3:00–3:35)
> The solution is a chain of clear rules.
> First, batches. Then payment advices.
> Then matching, with exceptions.
> Then facts from PDFs. And finally, categories for lines without an invoice.
> Two principles: never force a match.
> And no AI runs at solve time.

## Slide 7 — Results (3:35–4:10)
> On the live leaderboard, we reached ninety-four point nine one. Rank two.
> But we wanted to be sure we did not overfit.
> So we checked March 2025, a month we never used for tuning. This is not an official score.
> From version nine to version eleven, the gain was the same as on the official leaderboard.
> And version fourteen had no regressions.
> So the solution generalizes.

## Slide 8 — How we used Claude (4:10–4:35)
> Claude built, tested and coordinated the solution.
> But at solve time, it is plain Python: fast and reproducible.
> Agents worked in parallel lanes, and one agent reviewed every branch.
> Every change was measured with the official scorer. Nothing was kept unless it was measured.
> Good ideas from any member were added within the hour.

## Slide 9 — Next steps (4:35–4:50)
> Next, we would improve three things.
> Returned direct debits.
> English bank words for the new South African company.
> And more facts that only appear in PDFs.

## Slide 10 — Thank you (4:50–4:55)
> Thank you from Team 10!

---

### Pronunciation help
- **reconciliation**: re-con-si-li-AY-shun
- **orchestrator**: OR-kes-tray-tor
- **entity**: EN-ti-tee
- **precision**: pre-SI-zhun
- **ninety-four point nine one**: 94.91
