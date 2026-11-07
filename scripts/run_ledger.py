#!/usr/bin/env python3
"""Inspect and backfill the local reconciliation provenance SQLite registry."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recon.ledger import DEFAULT_DB, Ledger, read_report, scrub


def scan_paths(root):
    for folder, directories, files in os.walk(root):
        directories[:] = [d for d in directories if d not in {'.git', 'node_modules', '.venv', 'data'}]
        for name in sorted(files):
            if (name.endswith('.report.json') or name in {'report.json', 'score.json', 'score.txt', 'experiment.json'}
                    or 'VALIDATION' in name and name.endswith('.json')
                    or name.endswith('_submission.json') or 'official' in name and name.endswith('.json')):
                yield Path(folder) / name


def import_paths(ledger, paths, candidate_id=None, dataset_id=None, owner=None):
    results = []
    for path in paths:
        try:
            result = ledger.import_report(path, candidate_id, dataset_id, owner)
            results.append({'path': str(path), 'status': 'imported', 'kind': result.get('kind', result.get('outcome'))})
        except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as error:
            detail = {'error': str(error), 'type': type(error).__name__}
            if Path(path).is_file():
                ledger.import_observation(path, 'rejected', detail)
            results.append({'path': str(path), 'status': 'rejected', **scrub(detail)})
    return {'imported': sum(x['status'] == 'imported' for x in results),
            'rejected': sum(x['status'] == 'rejected' for x in results), 'results': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default=str(DEFAULT_DB))
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init')
    status = commands.add_parser('status')
    status.add_argument('--compact', action='store_true')
    commands.add_parser('integrity-check')
    backup = commands.add_parser('backup')
    backup.add_argument('--out', required=True)
    for name in ('import-report', 'import-reports'):
        imp = commands.add_parser(name)
        imp.add_argument('--path', nargs='+', required=True)
        imp.add_argument('--candidate-id')
        imp.add_argument('--dataset-id')
        imp.add_argument('--owner')
    scan = commands.add_parser('scan')
    scan.add_argument('--path', nargs='+', default=['outputs', 'reports', 'docs/reviews'])
    scan.add_argument('--owner')
    candidate = commands.add_parser('candidate')
    candidate.add_argument('--id')
    candidate.add_argument('--tag')
    candidate.add_argument('--owner')
    candidate.add_argument('--source-manifest', help='JSON file with exact source hash mapping')
    candidate.add_argument('--evidence', help='JSON evidence file')
    candidate.add_argument('--status', default='discovered')
    evaluation = commands.add_parser('evaluation')
    evaluation.add_argument('--report', required=True)
    evaluation.add_argument('--dataset-id', required=True)
    evaluation.add_argument('--candidate-id')
    evaluation.add_argument('--artifact-sha')
    evaluation.add_argument('--input-manifest-sha')
    evaluation.add_argument('--scorer-sha')
    args = parser.parse_args()
    try:
        ledger = Ledger(args.db)
        if args.command == 'init':
            result = {'database': str(ledger.path), **ledger.integrity_check()}
        elif args.command == 'status':
            result = ledger.status()
            if args.compact:
                result.pop('evaluations')
                result['submissions'] = [{k: v for k, v in row.items() if k != 'response_json'} for row in result['submissions']]
                if result['latest_live']:
                    result['latest_live'].pop('response_json', None)
        elif args.command == 'integrity-check':
            result = ledger.integrity_check()
        elif args.command == 'backup':
            result = ledger.backup(args.out)
        elif args.command in {'import-report', 'import-reports'}:
            result = import_paths(ledger, args.path, args.candidate_id, args.dataset_id, args.owner)
        elif args.command == 'scan':
            paths = sorted({p.resolve() for root in args.path for p in
                            ([Path(root)] if Path(root).is_file() else scan_paths(root))})
            result = import_paths(ledger, paths, owner=args.owner)
            result['database'] = str(ledger.path)
        elif args.command == 'candidate':
            result = ledger.register_candidate(args.id, args.tag, args.owner,
                read_report(args.source_manifest) if args.source_manifest else None,
                read_report(args.evidence) if args.evidence else None, args.status)
        else:
            result = ledger.record_evaluation(args.candidate_id, args.dataset_id, args.report,
                        args.input_manifest_sha, args.scorer_sha, args.artifact_sha)
    except (ValueError, OSError, sqlite3.Error) as error:
        parser.exit(1, str(scrub(str(error))) + '\n')
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
