#!/usr/bin/env python3
"""Freeze code, fingerprint inputs, and run into a new, isolated artifact folder.

No competition submission is performed. A release pointer is promoted only after
success and unchanged input fingerprints; interrupted runs remain unpromoted.
"""
import argparse
import hashlib
import json
import os
import platform
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


def sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def input_manifest(root, phase, pdf_facts=False):
    """Hash consumed input families, never scoring labels or unrelated exports."""
    root = Path(root)
    history = [p for p in ('train', 'live') if p != phase and (phase == 'final' or p == 'train')]
    if phase == 'train':
        history = []
    files = set((root / 'masters').glob('*.csv'))
    if phase == 'final':
        files.update((root / phase / 'masters_final').glob('*.csv'))
    names = ('ar_invoices.csv.gz', 'ap_invoices.csv.gz', 'credit_notes.csv.gz')
    for p in [phase] + history:
        folder = root / p
        files.update(folder / n for n in names if (folder / n).is_file())
        # T4 can consume prior-phase advice indexes and bodies. Hash all eligible
        # phase families conservatively so added/deleted/edited sources are detected.
        files.update(f for f in (folder / 'remittance_advices').rglob('*') if f.is_file())
        for group in ('payment_runs', 'direct_debit_batches', 'card_settlements', 'cheque_deposits'):
            files.update((folder / group).glob('*.csv'))
        if pdf_facts:
            files.update(f for f in (folder / 'pdfs').rglob('*') if f.is_file())
    for name in ('bank_transactions.csv.gz', 'open_items_at_start.csv.gz'):
        if (root / phase / name).is_file():
            files.add(root / phase / name)
    return {str(p.relative_to(root)): {'bytes': p.stat().st_size, 'sha256': sha256(p)}
            for p in sorted(files) if not p.name.startswith('._')}


def freeze_code(source, destination):
    files = list((source / 'recon').glob('*.py')) + [source / 'vendor/starter/bankrec_score.py']
    before = {str(p.relative_to(source)): sha256(p) for p in files}
    for p in files:
        target = destination / p.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
    if any(sha256(source / p) != h or sha256(destination / p) != h for p, h in before.items()):
        raise RuntimeError('Source changed while freezing; retry with a stable version')
    return before


def promote(pointer, run_dir):
    """One atomic pointer, never three independently replaced output files."""
    pointer = Path(pointer).resolve()
    report_path = run_dir / 'experiment.json'
    report = json.loads(report_path.read_text())
    if report.get('status') != 'verified':
        raise ValueError('Only a verified experiment can be promoted')
    mandatory = {'submission.csv.gz', 'submission.csv.gz.audit.jsonl', 'submission.csv.gz.report.json'}
    hashes = report.get('artifact_hashes', {})
    if set(hashes) != mandatory or any(not (run_dir / name).is_file() for name in mandatory):
        raise ValueError('Verified run must contain the complete submission/audit/report bundle')
    if sha256(run_dir / 'submission.csv.gz') != report['submission_sha256']:
        raise ValueError('Submission changed after verification')
    for name, expected in hashes.items():
        if sha256(run_dir / name) != expected:
            raise ValueError(f'Artifact changed after verification: {name}')
    release = json.loads((run_dir / 'submission.csv.gz.report.json').read_text())
    if (release.get('publication') != 'validated' or not release.get('validation', {}).get('ok')
            or not release.get('official_structure', {}).get('ok')
            or release.get('sha256') != hashes['submission.csv.gz']
            or release.get('audit_sha256') != hashes['submission.csv.gz.audit.jsonl']):
        raise ValueError('Release report does not validate this artifact bundle')
    readiness = release.get('pdf_readiness')
    if readiness and readiness.get('config', {}).get('strict'):
        sys.path.insert(0, str(ROOT))
        from recon.pdf_runtime import verify_snapshot
        verify_snapshot(readiness)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    temporary = pointer.with_name(pointer.name + '.' + uuid.uuid4().hex + '.tmp')
    temporary.write_text(json.dumps({'run': str(run_dir), 'manifest_sha256': sha256(report_path),
                                     'submission_sha256': report['submission_sha256']}, indent=2) + '\n')
    os.replace(temporary, pointer)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', default=str(ROOT / 'data/workspace'))
    parser.add_argument('--code-root', default=str(ROOT))
    parser.add_argument('--phase', choices=('sample', 'train', 'live', 'final'), default='sample')
    parser.add_argument('--out-dir', required=True, help='Must not already exist')
    parser.add_argument('--modules', default='batches,documents,matching')
    parser.add_argument('--pdf-facts', action='store_true')
    parser.add_argument('--hash-pdf-inputs', action='store_true', help='Hash PDF evidence for a PDF-disabled ablation baseline')
    parser.add_argument('--pdf-max-documents', type=int, default=100)
    parser.add_argument('--pdf-budget-seconds', type=float, default=30)
    parser.add_argument('--promote', help='Optional local JSON pointer to this verified run')
    sys.path.insert(0, str(ROOT))
    from recon import pdf_runtime
    pdf_runtime.add_options(parser, base=False)
    args = parser.parse_args(argv)
    pdf_runtime.check_options(args)
    if args.pdf_facts:
        args.pdf_cache_dir = str(Path(args.pdf_cache_dir or ROOT/'outputs/pdf_facts_cache').resolve())
    started = time.monotonic()
    run_dir = Path(args.out_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    data = Path(args.data).absolute()
    snapshot = run_dir / 'source'
    code = freeze_code(Path(args.code_root).resolve(), snapshot)
    inputs = input_manifest(data, args.phase, args.pdf_facts or args.hash_pdf_inputs)
    output = run_dir / 'submission.csv.gz'
    command = [sys.executable, '-m', 'recon.run', '--data', str(data), '--phase', args.phase,
               '--out', str(output), '--modules', args.modules]
    if args.pdf_facts:
        command += ['--pdf-facts', '--pdf-max-documents', str(args.pdf_max_documents),
                    '--pdf-budget-seconds', str(args.pdf_budget_seconds),
                    '--pdf-cache-dir', args.pdf_cache_dir,
                    '--pdf-max-party-documents', str(args.pdf_max_party_documents)]
        if args.pdf_prewarm: command.append('--pdf-prewarm')
        if args.require_pdf_complete: command.append('--require-pdf-complete')
    report = {'status': 'running', 'phase': args.phase, 'source_hashes': code,
              'python_version': sys.version, 'platform': platform.platform(),
              'input_manifest': inputs, 'command': command,
              'environment': {name: os.environ.get(name) for name in
                              ('RECON_DOCUMENT_CACHE_DIR', 'RECON_DOCUMENT_WORKERS')},
              'limitations': ['Inputs are hashed before and after, not copied or locked.',
                              'Optional time-budgeted PDF mode can depend on cache state and machine speed.']}
    manifest = run_dir / 'experiment.json'
    manifest.write_text(json.dumps(report, indent=2) + '\n')
    print(f'Frozen experiment: {run_dir}', flush=True)
    with (run_dir / 'run.log').open('w') as log:
        result = subprocess.run(command, cwd=snapshot, stdout=log, stderr=subprocess.STDOUT,
                                env={**os.environ, 'DASHBOARD_PUBLISH_ENABLED': 'false'})
    report['returncode'] = result.returncode
    report['inputs_changed'] = inputs != input_manifest(data, args.phase, args.pdf_facts or args.hash_pdf_inputs)
    report['seconds_end_to_end'] = round(time.monotonic() - started, 3)
    report['status'] = 'failed'
    if result.returncode == 0 and not report['inputs_changed']:
        release = json.loads(Path(str(output) + '.report.json').read_text())
        report['submission_sha256'] = sha256(output)
        if (release.get('publication') == 'validated' and release.get('sha256') == report['submission_sha256']
                and release.get('audit_sha256') == sha256(Path(str(output) + '.audit.jsonl'))):
            report['status'] = 'verified'
            report['artifact_hashes'] = {p.name: sha256(p) for p in
                                        (output, Path(str(output) + '.audit.jsonl'), Path(str(output) + '.report.json'))}
    manifest.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({k: report[k] for k in ('status', 'returncode', 'inputs_changed', 'seconds_end_to_end')}), flush=True)
    if report['status'] != 'verified':
        raise SystemExit(1)
    if args.promote:
        promote(Path(args.promote), run_dir)
    return report


if __name__ == '__main__':
    main()
