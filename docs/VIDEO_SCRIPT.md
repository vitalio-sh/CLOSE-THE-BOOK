# Video script — Team 10, "Close the Books" (4.5 min, 720p)

Each scene lists the screen content, the words to say and the duration. Total: about 4 min 30 s. [DA CONFERMARE: who records each scene]

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
"We built a deterministic chain of rules, from the strongest evidence to the weakest. First, the batch files: direct debits, cards, cheques and payment runs are expanded to their invoices. Then remittance advices, then invoice references and payer identity, including parent companies and confirming banks. Then the exceptions, exactly as the policy defines them: discounts within terms, FX differences against ECB rates, partial payments, returns. Finally, facts from the PDFs and categories learned from TRAIN. One principle above all: never force a match. When the evidence is ambiguous, we abstain."

## Scene 4 — How we worked: agents and validation (2:10–3:15)
**Screen:**
- Vitalii's orchestrator coordinating several Claude agents;
- a list of the integration rounds r6 … r11;
- the `make_holdout.py` command and its per-segment output;
- the `RESULTS.md` reason-code table.

**Say:**
"Vitalii ran a Claude orchestrator that coordinates several agents. Each of us worked on our own branch, and the best ideas were integrated in rounds, from r6 to r11. To avoid fooling ourselves, Luke built a fair holdout: TRAIN cut at the end of March, and April to June turned into a LIVE-shaped phase, so any solution runs unchanged and is scored per segment. Before each integration we ran adversarial checks. For direct-debit returns, the solver was right on 1,796 of 1,835. The errors were concentrated in MD06 refund requests, and unit tests that encode the policy found a precedence bug, which we fixed. Marina contributed entity-first payer resolution, and Ivan the categories learned from TRAIN history."

## Scene 5 — Results (3:15–3:50)
**Screen:** A chart or table of the LIVE scores over the day:

```
81.13 → 84.06 → 87.81 → 92.96 → 93.41 → 94.76
```

Show a leaderboard screenshot at 2nd place.

**Say:**
"On the live leaderboard we went from 81 in the morning to 94.76 with round 11, second place at 16:19. Every step was first confirmed on the holdout. For FINAL we checked the drift: the new ZA01 company in rand, the updated masters, and the new bank format." [DA CONFERMARE: result of the FINAL readiness check and the FINAL score, if known]

## Scene 6 — Next steps and close (3:50–4:30)
**Screen:** Three bullet points, then the GitHub link and the team names.

**Say:**
"With more time, we would do three things. First, link MD06 refunds to the right monthly collection through re-presentations: today they cause 27 wrong returns and 25 false duplicate payments. Second, link late remittance advices better. Third, handle noisy references and many-to-many payments. The code, the holdout tools and the validation reports are on GitHub. Thank you!"
