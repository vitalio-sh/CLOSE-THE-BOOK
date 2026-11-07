# Close the Books — Team 10

Talky × Anthropic hackathon, 7 November 2026, La Nave, Madrid.
Reconcile Grupo Valtermia's synthetic bank transactions against invoices, explain differences, and flag unidentified movements.

## Data setup

Requires Python 3.9+ and curl. No third-party packages are needed to download data.

```sh
python3 scripts/download_data.py --extract
```

All currently published packages are downloaded and SHA-256 checked against the official manifest. Full TRAIN, LIVE, docs, masters and sample data are extracted. TRAIN lite is retained as an archive because it duplicates the full TRAIN tables; add `--include-lite` to extract it separately.

```text
data/
  archives/             Original verified ZIP/tar.gz packages (ignored by Git)
  raw/
    docs/               Official docs, masters/, sample/, starter/
    train/              Full TRAIN archive contents
    live/               LIVE archive contents
  workspace/            Symlinks providing masters/, sample/, train/, live/, starter/
  manifest.json         Download paths, actual sizes and SHA-256 hashes
  inventory.json        Extracted file counts, sizes and top-level contents
docs/
  official/             Original organizer README, policies, format and dictionary
  source/config.json    Snapshot of the official download manifest
vendor/starter/         Unmodified organizer baseline and scoring scripts
scripts/download_data.py
```

For the official starter, use `cd data/workspace` and follow its README (Python 3.10+). Full TRAIN is at `data/raw/train/train/`; LIVE is at `data/raw/live/live/`.

Files retain their original archive layout and gzip compression. Consult `data/inventory.json` for exact paths. Raw data and generated outputs are intentionally excluded from Git; teammates can reproduce the download from this repository.

## FINAL release

The official manifest marked FINAL unavailable at setup time. Scheduled release: **7 November at 18:00 Europe/Madrid**. Once released, run:

```sh
python3 scripts/download_data.py --refresh --extract
```

This refreshes availability/checksums and downloads newly published packages. It also detects updates to previously downloaded archives. No background polling is configured.

## Start reading

1. [English challenge overview](docs/official/README_EN.md)
2. [Reconciliation policies](docs/official/POLICIES.md)
3. [Data dictionary](docs/official/DATA_DICTIONARY.md)
4. [Submission format](docs/official/FORMAT.md)
5. [Official baseline and scorer](vendor/starter/README.md)

## Event deadlines (Europe/Madrid)

- 18:00: FINAL dataset release.
- 19:30: reconciliation submission deadline; only one scored FINAL submission.
- 19:45: project video, GitHub URL and explanation deadline.
- 20:30: final leaderboard reveal.

The repository is private. Arrange jury access when delivering the project. Keep team tokens and registration credentials outside Git (for example in an ignored `.env` file).

Source: https://hackathon.usetalky.com/datos
