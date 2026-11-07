# Measured results — 7 November 2026

These are local measurements. Neither release file has been uploaded to the organizer; LIVE accuracy is unknown without hidden labels.

| Check | Result | Meaning |
|---|---:|---|
| Organizer starter on SAMPLE | 11.28 / 100 | Reference baseline |
| Release solver on SAMPLE | 78.84 / 100 | Official local scorer with real SAMPLE golden; 8,499 transactions |
| October 2024 chronological window | 79.23 / 100 | 32,859 transactions; older frozen code and oracle prior-state caveats below |
| Release LIVE coverage | 203,083 / 203,083 | Every transaction represented; structure only |
| Release LIVE output | 605,836 rows | 26,247,327 uncompressed bytes; below official limit |
| Release LIVE gzip size | 6,306,687 bytes | Upload candidate size |
| Release LIVE generation | 198.25 seconds | Local machine; excludes final structural validation |
| Full regression suite | 74 tests passed | Includes inference safeguards, publication and dashboard/server checks |

SAMPLE allocation precision is 99.7502%, recall 68.3025%, transaction accuracy 83.2569%, and exception macro-F1 0.665870. Its output has 26,404 rows, 1,133,674 uncompressed bytes, and generated in 19.97 seconds. SAMPLE was used during development and is not an unbiased estimate of generalization. It has no authoritative opening snapshot, which limits evaluation of remainder and release cases. Optional bounded invoice-PDF facts produced the same sample score in an earlier comparison, so that mode remains opt-in.

## Release artifacts and reproducibility

Both runs used identical source hashes. Neither run detected source changes. Both independent validation and the organizer parser pass. The organizer parser receives empty labels for LIVE format checks, and its fabricated score is discarded.

| Artifact | SHA-256 of gzip bytes |
|---|---|
| `outputs/sample_release.csv.gz` | `043f33f7ef13c80bd0a3ec03d1770326891e1f7bf3938449b7c145680c59ae00` |
| `outputs/submission_live_release.csv.gz` | `d9d802084e014420cf58540289b8a3639309fd20b0ef46f76b65992fd21f8aa0` |

Each CSV has `.audit.jsonl` and `.report.json` sidecars. Reports record module counts, source hashes, CSV and audit hashes, validation, master-file coverage, and opening invoice metadata coverage. Tracked report copies are `reports/sample_run.json`, `reports/sample_score.json`, and `reports/live_release.json`. Earlier `live_ready` artifacts are historical fallbacks, not the current release.

Gzip headers have zero timestamps and no filename. The runner writes candidates in a temporary directory, checks them, and only then replaces the output. A validation failure or source change preserves the existing output/audit/report bundle and writes a separate `.failed.report.json`. Each file rename is atomic; the report is replaced last and its hashes identify the matching bundle. A publication exception rolls back earlier replacements. This is not a claim of a filesystem transaction across all three filenames or crash recovery from a power loss between renames.

## LIVE evidence and unresolved cases

All 6,889 remittance advices were parsed, including 178 changed-layout XLSX files containing 30,225 detail rows. There were zero parse errors, unsupported files, or unresolved/ambiguous short-reference rows in this run. Reading a document successfully does not establish that every allocation is correct.

The release restored 151,591 explicitly referenced historical identities at zero balance. All 229,534 opening AR invoices have known payment terms after historical enrichment. FINAL can override individual master files while the others fall back to the base directory; missing/empty required categories or payment terms stop the run before inference.

| Producing module | LIVE transactions |
|---|---:|
| Structured batches | 60,944 |
| Reference/party/amount matching | 95,795 |
| Remittance documents | 6,528 |
| Non-invoice categories | 13,788 |
| Unidentified | 26,028 |

Central validation rejected six batch proposals and allowed later fallback logic to handle those transactions. Unidentified cases and multi-party confirming remain material limitations. Full coverage means every transaction has an output row; it does not mean every transaction has been reconciled successfully.

## October window assumptions

`scripts/evaluate_window.py` freezes the source used for its experiment. Invoice issue dates are limited to the end of the window. Golden rows strictly before October 1 reconstruct prior bank-settlement state. Inside October, balances follow predictions, not target labels. October labels are loaded for scoring only after the output file is closed.

This is not a pure end-to-end holdout: prior settlements are oracle state, non-bank cancellations are not reconstructed, and organizer masters are later snapshots. Its full historical catalogue includes closed identities, unlike the release's targeted identity recovery. The older evaluation runner did not use production's central proposal validator. It covers one month, not the six-month LIVE/FINAL horizon, and it predates the latest fixes. The test used 4.65 million invoice/credit records and about 15.76 GB peak memory; total runtime was 112.93 seconds, inference 50.69 seconds.

Allocation precision was 98.96%, recall 73.13%, transaction accuracy 87.03%. This historical experiment is retained with its frozen source and is not presented as an evaluation of the current release. No score guarantees for LIVE/FINAL follow from it.

## Independent review

Claude Code was invoked as `claude-opus-5-5`, effort `xhigh`, with read-only tools. The architecture review is archived in `docs/reviews/CLAUDE_ARCHITECTURE.md`; the implementation review and per-finding addressed/deferred/disputed decisions are archived in `docs/reviews/CLAUDE_CODE_REVIEW.md`. Recommendations were checked before adoption, including rejecting invoice-due-date regrouping that contradicted observed confirming inputs.

## Remaining event work

- Submit an authorized LIVE candidate with the team token and inspect the official score.
- Download FINAL when released at 18:00 Madrid, then run and validate against that phase. Only one FINAL submission is allowed.
- Complete the video and explanation, provide the required repository access, and submit project materials by the event deadlines.
