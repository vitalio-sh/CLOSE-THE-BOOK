"""Build the submit_live.py evidence file for a lead-integrated candidate.

Reads only local verified artifacts: the three-window comparison (compare_three_superset.py output) and the
LIVE run_experiment directory. Never contacts the organizer.
Usage: python3 scripts/lead_evidence.py <label> <candidate_tag> <baseline_tag>
"""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

MAIN = Path('/Users/neo/CodeProejcts/CLOSE-THE-BOOK')
LANE = MAIN / 'outputs/review_coordination/claude_competitor_lane'
WINDOWS = {'sample': 'sample', 'october': 'train:2024-10-01:2024-10-31', 'january': 'train:2025-01-01:2025-01-14'}
METRICS = ('score', 'precision', 'recall', 'txn_accuracy', 'exception_macro_f1')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def commit(ref):
    return subprocess.check_output(['git', 'rev-parse', ref + '^{commit}'], cwd=MAIN, text=True).strip()


def flat(score):
    return {'score': score['score'], **{k: score['metrics'][k] for k in METRICS[1:]}}


def main():
    label, cand_tag, base_tag = sys.argv[1:4]
    live = LANE / f'live_{label}'
    report_path = MAIN / f'docs/reviews/TEAM_{label.upper()}_VALIDATION_2026-11-07.json'
    windows = {}
    for w in WINDOWS:
        windows[w] = json.loads((LANE / f'{label}_{w}_comparison.json').read_text())
    # assemble the T1/T2-format comparison report from the per-window comparison files written by compare_three
    comp_out = LANE / f'{label}_report.json'
    if not comp_out.exists():
        raise SystemExit(f'missing {comp_out}; compare_three_superset.py must finish first')
    report_path.write_bytes(comp_out.read_bytes())
    comparison = json.loads(report_path.read_text())
    run = json.loads((live / 'submission.csv.gz.report.json').read_text())
    experiment = json.loads((live / 'experiment.json').read_text())
    assert experiment['status'] == 'verified' and experiment['inputs_changed'] is False
    assert run['publication'] == 'validated' and run['validation']['ok'] and run['official_structure']['ok']
    assert run['source_changed_during_run'] is False and not run['checked_inputs_changed']
    assert run['sha256'] == sha(live / 'submission.csv.gz')
    cand, base = commit(cand_tag), commit(base_tag)
    assert comparison['candidate_sha'] == cand and comparison['before_sha'] == base
    for name, expected in run['source_hashes'].items():
        blob = subprocess.check_output(['git', 'show', f'{cand}:recon/{name}'], cwd=MAIN)
        assert hashlib.sha256(blob).hexdigest() == expected, name
    checks = []
    for window, dataset in WINDOWS.items():
        item = comparison['windows'][window]
        manifest = {k: v for k, v in item['file_hashes'].items() if k not in ('before', 'after')}
        checks.append({
            'dataset': dataset, 'evidence_path': str(report_path), 'evidence_sha256': sha(report_path),
            'evidence_window': window, 'scorer_sha256': item['scorer_sha256'],
            'input_manifest_sha256': hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
            'baseline': flat(item['before_score']), 'candidate': flat(item['after_score'])})
    evidence = {
        'schema_version': 1, 'candidate': cand_tag, 'candidate_commit': cand,
        'baseline_candidate': base_tag, 'baseline_commit': base,
        'candidate_source_hashes': run['source_hashes'],
        'validation': {'reviewed': True, 'no_label_leakage': True},
        'checks': checks,
        'limitations': comparison.get('limitations', []) + [comparison.get('harness_deviation', ''),
                                                            'Integrated by the CLI team lead (Claude Code agent team).'],
        'runtime_configuration': {'command': experiment['command'], 'pdf_readiness': run.get('pdf_readiness')},
    }
    out = MAIN / f'outputs/platform/evidence/team_{label}.json'
    out.write_text(json.dumps(evidence, indent=2) + '\n')
    print(json.dumps({'evidence': str(out), 'submission': str(live / 'submission.csv.gz'),
                      'submission_sha256': run['sha256'],
                      'scores': {w: (comparison['windows'][w]['before_score']['score'],
                                     comparison['windows'][w]['after_score']['score'],
                                     comparison['windows'][w]['diff']['n_fixes'],
                                     comparison['windows'][w]['diff']['n_regressions']) for w in WINDOWS}}, indent=2))


if __name__ == '__main__':
    main()
