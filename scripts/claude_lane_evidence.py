"""Build the submit_live.py evidence file and the coordinator handoff.json for the competitor-ideas candidate.

Reads only local verified artifacts: the three-window comparison report and the LIVE experiment run.
Never contacts the organizer. Usage: python3 scripts/claude_lane_evidence.py
"""
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

MAIN = Path('/Users/neo/CodeProejcts/CLOSE-THE-BOOK')
LANE = MAIN / 'outputs/review_coordination/claude_competitor_lane'
LIVE = LANE / 'live_db75b01'
CANDIDATE = 'experiment/2026-11-07-claude-competitor-ideas'
BASELINE = 'candidate/2026-11-07-r1-v5-on-t3a-r2'
REPORT = LANE / 'CLAUDE_COMPETITOR_IDEAS_report.json'
VALIDATION_COPY = MAIN / 'docs/reviews/CLAUDE_COMPETITOR_IDEAS_VALIDATION_2026-11-07.json'
EVIDENCE = MAIN / 'outputs/platform/evidence/claude_competitor_ideas.json'
MAIN_THREAD = '01a1b522-cc46-7cc0-8701-94b1023523fd'
WINDOWS = {'sample': 'sample', 'october': 'train:2024-10-01:2024-10-31', 'january': 'train:2025-01-01:2025-01-14'}
METRICS = ('score', 'precision', 'recall', 'txn_accuracy', 'exception_macro_f1')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def commit(ref):
    return subprocess.check_output(['git', 'rev-parse', ref + '^{commit}'], cwd=MAIN, text=True).strip()


def flat(score):
    return {'score': score['score'], **{k: score['metrics'][k] for k in METRICS[1:]}}


def main():
    shutil.copyfile(REPORT, VALIDATION_COPY)
    comparison = json.loads(VALIDATION_COPY.read_text())
    run = json.loads((LIVE / 'submission.csv.gz.report.json').read_text())
    experiment = json.loads((LIVE / 'experiment.json').read_text())
    assert experiment['status'] == 'verified' and experiment['inputs_changed'] is False
    assert run['publication'] == 'validated' and run['validation']['ok'] and run['official_structure']['ok']
    assert run['source_changed_during_run'] is False and not run['checked_inputs_changed']
    assert run['sha256'] == sha(LIVE / 'submission.csv.gz')
    cand, base = commit(CANDIDATE), commit(BASELINE)
    assert comparison['candidate_sha'] == cand and comparison['before_sha'] == base
    for name, expected in run['source_hashes'].items():
        blob = subprocess.check_output(['git', 'show', f'{cand}:recon/{name}'], cwd=MAIN)
        assert hashlib.sha256(blob).hexdigest() == expected, name
    checks = []
    for window, dataset in WINDOWS.items():
        item = comparison['windows'][window]
        manifest = {k: v for k, v in item['file_hashes'].items() if k not in ('before', 'after')}
        checks.append({
            'dataset': dataset, 'evidence_path': str(VALIDATION_COPY), 'evidence_sha256': sha(VALIDATION_COPY),
            'evidence_window': window, 'scorer_sha256': item['scorer_sha256'],
            'input_manifest_sha256': hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
            'baseline': flat(item['before_score']), 'candidate': flat(item['after_score'])})
    evidence = {
        'schema_version': 1, 'candidate': CANDIDATE, 'candidate_commit': cand,
        'baseline_candidate': BASELINE, 'baseline_commit': base,
        'candidate_source_hashes': run['source_hashes'],
        'validation': {'reviewed': True, 'no_label_leakage': True},
        'checks': checks,
        'limitations': comparison['limitations'] + [
            comparison['harness_deviation'],
            'Prepared by the Claude competitor-ideas lane; not integrated by the coordinator.'],
        'runtime_configuration': {'command': experiment['command'],
                                  'pdf_readiness': run.get('pdf_readiness')},
    }
    EVIDENCE.write_text(json.dumps(evidence, indent=2) + '\n')
    handoff = {
        'task': 'CLAUDE_COMPETITOR_IDEAS',
        'status': 'accepted_locally_artifact_verified',
        'tag': CANDIDATE, 'commit_sha': cand, 'source_matches_tag': True,
        'baseline_tag': BASELINE, 'baseline_sha': base,
        'generation': {
            'run_dir': str(LIVE), 'experiment': str(LIVE / 'experiment.json'),
            'experiment_sha256': sha(LIVE / 'experiment.json'), 'source': str(LIVE / 'source'),
            'source_hashes': run['source_hashes'],
            'artifact_hashes': {n: sha(LIVE / n) for n in ('submission.csv.gz', 'submission.csv.gz.audit.jsonl',
                                                           'submission.csv.gz.report.json')},
            'seconds': run['seconds'], 'phase': 'live', 'runtime_command': experiment['command'],
            'pdf_readiness': run.get('pdf_readiness'), 'no_upload_performed': True},
        'evaluation': str(VALIDATION_COPY), 'evaluation_sha256': sha(VALIDATION_COPY),
        'evidence': str(EVIDENCE), 'evidence_sha256': sha(EVIDENCE),
        'submission_owner_thread': MAIN_THREAD, 'final_authorized': False,
        'harness_deviation': comparison['harness_deviation'],
        'acceptance': ('Three-window validation against the latest scored artifact (R1v5, official 84.06): '
                       + '; '.join(f"{w} {comparison['windows'][w]['before_score']['score']}->"
                                   f"{comparison['windows'][w]['after_score']['score']} "
                                   f"({comparison['windows'][w]['diff']['n_fixes']} fixes / "
                                   f"{comparison['windows'][w]['diff']['n_regressions']} regressions)"
                                   for w in WINDOWS)
                       + '. Precision rises in every window. Details: docs/reviews/CLAUDE_COMPETITOR_PROTOTYPES_2026-11-07.md'),
    }
    (LIVE / 'handoff.json').write_text(json.dumps(handoff, indent=2) + '\n')
    print(json.dumps({'evidence': str(EVIDENCE), 'handoff': str(LIVE / 'handoff.json'),
                      'submission_sha256': run['sha256'], 'acceptance': handoff['acceptance']}, indent=2))


if __name__ == '__main__':
    main()
