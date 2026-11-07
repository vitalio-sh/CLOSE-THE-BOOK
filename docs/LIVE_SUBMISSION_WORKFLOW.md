# Verified LIVE submission workflow

`scripts/submit_live.py` sends only a completed, validated LIVE CSV. There is no FINAL mode. Publishing a dashboard summary is a separate operation and does not submit anything to the organizers.

Run the local preflight first:

```sh
python3 scripts/submit_live.py \
  --submission outputs/CANDIDATE/submission.csv.gz \
  --candidate candidate/IMMUTABLE-TAG \
  --evidence outputs/platform/evidence/CANDIDATE.json \
  --dry-run
```

After the preflight succeeds, the same command without `--dry-run` performs the authorized LIVE submission. It reads `PUBLISHTOKEN` from the process environment or the root `.env`; never put the token in a command, report, commit or chat. `--dry-run` makes no organizer request and reserves no upload slot.

The default SQLite registry is `outputs/platform/progress.sqlite3`. Exact CSV, audit and report bytes are archived under `outputs/platform/artifacts/<CSV SHA-256>/<original report SHA-256>/` before a request can reserve a slot. `original_report.json` retains the original report bytes and their SHA; a separate normalized report points to the archived sibling files. Archive writes use temporary files and atomic replacement. Both report copies preserve the source-module hashes even if a later solver run overwrites its original output path.

## Evidence contract

The evidence file must contain:

- `schema_version: 1`;
- `candidate` and `candidate_commit`: a Git reference and its full pinned commit;
- `baseline_candidate` and `baseline_commit`: the comparison baseline and its pinned commit;
- `candidate_source_hashes`: the exact `source_hashes` mapping from the LIVE run report;
- `validation: {"reviewed": true, "no_label_leakage": true}`;
- `checks`: at least SAMPLE and one distinct TRAIN date range.

Each check supplies `dataset` (`sample` or `train:YYYY-MM-DD:YYYY-MM-DD`), `evidence_path`, `evidence_sha256`, `evidence_window`, `input_manifest_sha256`, `scorer_sha256`, and flat `baseline` / `candidate` objects containing `score`, `precision`, `recall`, `txn_accuracy` and `exception_macro_f1`.

`evidence_path` references a comparison report in the existing T1/T2 format: root `candidate_sha`, `before_sha` and `windows`; each window has `before_tag`, `after_tag`, `before_score`, `after_score`, `file_hashes`, `scorer`, `scorer_sha256`, and `tag_source_hashes_verified: true`. Scores are read back from that hashed comparison report rather than trusting the repeated numbers in the evidence file. The input-manifest digest is SHA-256 of canonical JSON (`sort_keys=True`, `separators=(',', ':')`) of `file_hashes` excluding `before` and `after`. Independent benchmarks must use distinct input manifests and window identities. The scorer's current bytes must match its recorded hash.

Every reported LIVE source module is compared with the pinned Git commit. Baseline tags and comparison commits are pinned too. Before a new reservation, the baseline source must match the latest scored artifact in the registry. This prevents selecting a conveniently weaker old baseline. An optional `parent_evidence_path` is hash-checked, but it does not bypass this direct-baseline requirement.

All checked local scores must be non-decreasing, at least one must improve, and allocation precision may drop by no more than 0.002 (0.2 percentage points). Transaction accuracy can decrease when a reviewed exception-class improvement increases the official weighted score; the exact tradeoff stays in the evidence. The evidence review is a human/agent engineering judgment, not a cryptographic endorsement or proof against all label leakage.

## Safety and recovery

One local file lock serializes all uploader invocations. SQLite also permits only one submission attempt per exact CSV hash. The script verifies team identity, current LIVE acceptance, quota and minimum interval. The upload-size check uses **uncompressed** bytes, not gzip size. Both the internal validator and organizer-format validator must have passed, transaction coverage must be complete, and CSV/audit hashes must match.

The reservation intent and unique filename are recorded before requesting an upload URL. `sub_id` is saved immediately when returned. Presigned URLs, form credentials and tokens are never written into the registry or reports. The multipart upload sends the file last. Scoring is polled no faster than every five seconds, for at most ten minutes per invocation.

To resume an interrupted attempt, invoke the same command for the same artifact. The script retrieves its existing submission ID; it never creates a replacement slot automatically. If a reservation response was lost, it reconciles the unique filename against platform history. If an upload itself has an uncertain outcome and the server cannot confirm scoring, the attempt stops for investigation rather than uploading blindly. Existing results can still be read after LIVE closes; a closed phase cannot queue a new score.

Official responses are retained in immutable `reports/live_official_<attempt>_<response digest>.json` files and in the SQLite event/submission history. A lower official score remains visible as a regression; it must not be presented as a new best score. No retry or local improvement authorizes FINAL.
