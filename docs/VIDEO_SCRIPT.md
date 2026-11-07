# Video script — Team 10, "Close the Books" (4.5 min, 720p)

Each scene lists the screen content, the words to say and the duration. Total: about 4 min 30 s.

> Draft. Final wording, who records each scene and the delivery are Vitalii's decision.

---

## Scene 1 — The problem (0:00–0:35)
**Screen:** Title "Team 10 — Close the Books", the four names, then a few raw bank lines from `bank_transactions` next to a list of open invoices.

**Say:**
"Hi, we are Team 10: Vitalii, Luke, Ivan and Marina. Every month an accountant must match thousands of bank lines to invoices. Here is a single line: a SEPA batch of 400 customers. Here is a transfer that names no invoice, only a truncated payer name. Grupo Valtermia has 17 companies, 12 currencies and 13 banks. We had to say what each line pays, for how much, and what went wrong."

## Scene 2 — Why it is hard (0:35–1:10)
**Screen:** Examples side by side:
- a PDF reading "anula y sustituye a la factura…";
- a remittance advice arriving 5 days after the cash;
- an MD06 direct-debit return.

**Say:**
"The key facts are scattered across files. Some are only in PDFs: instalments, replaced invoices, advances. Remittance advices arrive days later. Banks convert currencies at their own rate. And FINAL drifts: a new South African company in rand, a new bank file format, renamed customers. A solution learned by heart on LIVE would break."

## Scene 3 — Our approach (1:10–2:10)
**Screen:** A diagram of the rule chain:

```
batch files → remittance advices → references + payer → exceptions → PDF facts → categories
```

Show the matching module in the repo briefly.

**Say:**
"We built a deterministic chain of rules, from the strongest evidence to the weakest. First, the batch files: direct debits, cards, cheques and payment runs are expanded to their invoices. Then remittance advices, then invoice references and payer identity, including parent companies and confirming banks. Then the exceptions, exactly as the policy defines them: discounts within terms, FX differences against ECB rates, partial payments, returns. Finally, facts from the PDFs and categories looked up from TRAIN history. One principle above all: never force a match. When the evidence is ambiguous, we abstain. And no model runs at solve time: the solver is plain Python. Claude helped us build, test and coordinate it."

## Scene 4 — How we worked: agents and validation (2:10–3:15)
**Screen:**
- Vitalii's orchestrator coordinating several Claude agents;
- a list of the integration rounds r6 … r14;
- the `make_holdout.py` command and its per-segment output;
- the `RESULTS.md` reason-code table.

**Say:**
"Vitalii ran a Claude orchestrator that coordinates several agents. Each of us worked on our own branch, and the best ideas were integrated in rounds, from r6 to r14. To avoid fooling ourselves, Luke built a fair holdout: TRAIN cut at the end of March, and April to June turned into a LIVE-shaped phase, so any solution runs unchanged and is scored per segment. Before each integration we ran adversarial checks. For direct-debit returns, round r6 was right on 1,796 of 1,835. The errors were concentrated in MD06 refund requests. Unit tests that encode the policy found a precedence bug, and the fix is in the final version. An independent check on March 2025, a month we never tuned on, moved from 93.51 to 94.20 between rounds r9 and r11. That is the same gain as on the live leaderboard. Marina contributed entity-first payer identification: when several customers match, only those of the company that owns the bank account are ranked, so a same-named customer abroad can't win. Ivan built an independent pipeline and reviewed our rules; the issues he found were fixed."

## Scene 5 — Results (3:15–3:50)
**Screen:** A chart or table of the LIVE scores over the day:

```
81.13 → 84.06 → 87.81 → 92.96 → 93.41 → 94.76 → 94.82 → 94.87 → 94.91
```

Show a leaderboard screenshot at 2nd place.

**Say:**
"On the live leaderboard we went from 81 in the morning to 94.91 with round 14, second place. Each round was checked on held-out data first. For FINAL we checked the drift: we simulated the new ZA01 company in rand with updated masters, and the solver runs cleanly. The FINAL score is hidden until the reveal."

## Scene 6 — Next steps and close (3:50–4:30)
**Screen:** Four bullet points, then the GitHub link and the team names.

**Say:**
"With more time, we would do four things. First, measure how much the re-presentation lookahead fixes the MD06 refund errors we found on round 6. Second, link late remittance advices better. Third, handle noisy references and many-to-many payments. Fourth, teach the category rules English bank wording for the new South African company, so a line like 'bank charges' becomes a bank fee. The code, the holdout tools and the validation reports are on GitHub. Thank you!"
