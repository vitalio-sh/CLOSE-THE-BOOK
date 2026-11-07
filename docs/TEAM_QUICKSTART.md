# Team quickstart

Private repository: https://github.com/vitalio-sh/CLOSE-THE-BOOK . Each teammate needs collaborator access before cloning. Bulk datasets are not stored in Git.

## Requirements

Python 3.10+ (development tested on 3.12), curl. The core pipeline uses the standard library. Optional `pdftotext` improves PDF remittance handling; without it PDF coverage is reduced. No model API key is needed for inference.

## Download and run

From repository root:

```sh
python3 scripts/download_data.py --extract
python3 -m unittest discover -s tests -v
python3 -m recon.run --data data/workspace --phase sample --out outputs/sample.csv.gz
python3 vendor/starter/score_train.py outputs/sample.csv.gz --data data/workspace --sample
python3 -m recon.run --data data/workspace --phase live --out outputs/submission_live.csv.gz
```

Use your Python 3.10+ executable (e.g. `python3.12`) if system `python3` is older.

Output includes the five-column submission CSV, a `.report.json` with independent structural validation, runtime and checksum, and a `.audit.jsonl` with module attribution. Keep all of these; only upload the CSV to the platform. `ok` in the validation report confirms structure/coverage, not correctness of the inferred matches.

No automatic submission is performed. A teammate with the team token must submit through the official platform and record the server outcome. FINAL permits only one valid submission. Do not confuse a generated file with an accepted result.

## FINAL

After its official release:

```sh
python3 scripts/download_data.py --refresh --extract
python3 -m recon.run --data data/workspace --phase final --out outputs/submission_final.csv.gz
```

The loader uses `final/masters_final` when present and initializes opening balances from the phase snapshot. Review the report, coverage, runtime, file size and exceptions before the single FINAL submission. These commands are prepared; FINAL has not yet been tested because its data is not released.

## Team boundaries

- `recon/context.py`, `run.py`, `validate.py`: integration, phase loading, balances, CSV and validation.
- `recon/matching.py`: ordinary payments and no-item categories.
- `recon/batches.py`: payment runs, direct debit, card and cheque groups.
- `recon/documents.py`: payment advice extraction and matching.
- `vendor/starter/`: unmodified organizer baseline and scorer.

Use separate branches. Do not concatenate independently produced submissions: modules propose a whole interpretation of one transaction and the runner chooses one, then updates balances centrally.

## Claude reviewer

An authenticated Claude Code CLI can run `bash scripts/review_with_claude.sh`. It explicitly requests `claude-opus-5-5` with `xhigh`, has only Read/Glob/Grep tools and writes its response under ignored `outputs/reviews/`. It does not change code or submit files. Availability depends on your Claude account.

## Evaluation caveat

The sample is a development set. A high sample score is not an independent validation or a predicted LIVE/FINAL score. Use the official LIVE feedback and a separate chronological TRAIN evaluation before claiming generalization. Current modules are heuristic and incomplete, particularly for standalone invoice-PDF clauses, missing historical closed items, and ambiguous payment groups.

## Optional PDF facts

The default fast path already parses payment-advice documents. Standalone invoice-PDF facts are separate and opt-in:

```sh
python3 -m recon.run --phase sample --pdf-facts --pdf-max-documents 100 --pdf-budget-seconds 30 --out outputs/sample_pdf.csv.gz
```

The budget limits active PDF/cache processing time; indexes and the rest of reconciliation still take time. Exhaustion returns no new facts, not invented matches. Inspect `pdf_facts` in the report. Do not enable a larger budget near the FINAL deadline without a timing rehearsal.

See [measured results](RESULTS.md) and [Russian team plan](TEAM_PLAN_RU.md). Historical closed invoices with explicit phase references are restored as **zero-balance candidates** for duplicate/return recognition; catalog membership alone is not proof of a match.
