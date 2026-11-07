"""Local, append-audited provenance registry. No organiser credentials belong here."""
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit
import uuid

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / 'outputs/platform/progress.sqlite3'


class DuplicateSubmission(ValueError):
    def __init__(self, existing):
        self.existing = existing
        super().__init__('Artifact already has a submission attempt: ' + existing['local_attempt_id'])


def now():
    return datetime.now(timezone.utc).isoformat()


def file_sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1048576), b''):
            h.update(block)
    return h.hexdigest()


def read_report(path):
    value, _ = json.JSONDecoder().raw_decode(Path(path).read_text().lstrip())
    if not isinstance(value, dict):
        raise ValueError('Report must contain a JSON object')
    return value


def scrub(value):
    """Conservative credential stripping before any caller-controlled JSON is stored."""
    if isinstance(value, dict):
        return {str(k): ('[REDACTED]' if re.search(
            r'token|authorization|password|secret|api.?key|presign|^upload$|^fields$|credential|^environment$|^env$|^command$', str(k), re.I)
            else scrub(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(x) for x in value]
    if isinstance(value, str):
        if value.startswith(('https://', 'http://')):
            u = urlsplit(value)
            if u.username or u.query:
                return '[REDACTED_URL]'
        return re.sub(r'(?i)Bearer\s+\S+|\b(?:ghp_|github_pat_|sk-ant-|sk-proj-)[A-Za-z0-9_-]+',
                      '[REDACTED]', value)
    return value


def encode(value):
    return json.dumps(scrub(value), sort_keys=True, separators=(',', ':'), allow_nan=False)


def sha_value(value, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
        raise ValueError('Expected a SHA-256 digest')
    return value


def metric(value, upper=100):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= upper:
        raise ValueError('Invalid numeric metric')
    return value


def rowdict(row):
    if row is None:
        return None
    result = dict(row)
    for key in result:
        if key.endswith('_json') and result[key] is not None:
            result[key] = json.loads(result[key])
    return result


def candidate_sources(candidate):
    manifest = json.loads(candidate['source_manifest_json']) if isinstance(candidate['source_manifest_json'], str) else candidate['source_manifest_json']
    return manifest.get('source_hashes', manifest)


def artifact_has_sources(db, artifact, sources):
    if not sources:
        return False
    if sources == json.loads(artifact['source_hashes_json']):
        return True
    return any(json.loads(row['report_json']).get('source_hashes') == sources for row in db.execute(
        'SELECT report_json FROM artifact_reports WHERE artifact_sha=?', (artifact['sha256'],)))


def check_candidate_artifact(db, candidate, artifact):
    if not artifact_has_sources(db, artifact, candidate_sources(candidate)):
        raise ValueError('Candidate source manifest does not match artifact')


SCHEMA = '''
CREATE TABLE IF NOT EXISTS artifacts (
 sha256 TEXT PRIMARY KEY, path TEXT NOT NULL, size INTEGER NOT NULL, phase TEXT NOT NULL,
 report_sha TEXT NOT NULL, report_path TEXT NOT NULL, run_id TEXT, source_hashes_json TEXT NOT NULL,
 created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS artifact_reports (
 artifact_sha TEXT NOT NULL REFERENCES artifacts(sha256), report_sha TEXT NOT NULL,
 report_path TEXT NOT NULL, report_json TEXT NOT NULL, observed_at TEXT NOT NULL,
 PRIMARY KEY(artifact_sha,report_sha));
CREATE TABLE IF NOT EXISTS candidates (
 candidate_id TEXT PRIMARY KEY, tag TEXT, owner TEXT, source_manifest_json TEXT NOT NULL,
 evidence_json TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evaluations (
 evidence_sha TEXT PRIMARY KEY, candidate_id TEXT REFERENCES candidates(candidate_id),
 artifact_sha TEXT REFERENCES artifacts(sha256), dataset_id TEXT NOT NULL,
 input_manifest_sha TEXT, scorer_sha TEXT, score REAL NOT NULL, precision REAL, recall REAL,
 txn_accuracy REAL, exception_macro_f1 REAL, report_path TEXT NOT NULL,
 evidence_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS submissions (
 local_attempt_id TEXT PRIMARY KEY, artifact_sha TEXT UNIQUE NOT NULL REFERENCES artifacts(sha256),
 phase TEXT NOT NULL CHECK(phase='LIVE'), candidate_id TEXT REFERENCES candidates(candidate_id),
 sub_id TEXT UNIQUE, filename TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL, official_score REAL, response_json TEXT, source_report TEXT);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, kind TEXT NOT NULL,
 entity_id TEXT NOT NULL, detail_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS checkpoints (
 key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS comparison_reports (
 evidence_sha TEXT PRIMARY KEY, report_path TEXT NOT NULL, before_candidate_id TEXT,
 after_candidate_id TEXT, report_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS evaluations_no_update BEFORE UPDATE ON evaluations BEGIN SELECT RAISE(ABORT,'evaluations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS evaluations_no_delete BEFORE DELETE ON evaluations BEGIN SELECT RAISE(ABORT,'evaluations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS artifacts_no_update BEFORE UPDATE ON artifacts BEGIN SELECT RAISE(ABORT,'artifacts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS artifacts_no_delete BEFORE DELETE ON artifacts BEGIN SELECT RAISE(ABORT,'artifacts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS artifact_reports_no_update BEFORE UPDATE ON artifact_reports BEGIN SELECT RAISE(ABORT,'artifact reports are immutable'); END;
CREATE TRIGGER IF NOT EXISTS artifact_reports_no_delete BEFORE DELETE ON artifact_reports BEGIN SELECT RAISE(ABORT,'artifact reports are immutable'); END;
CREATE TRIGGER IF NOT EXISTS comparisons_no_update BEFORE UPDATE ON comparison_reports BEGIN SELECT RAISE(ABORT,'comparison reports are immutable'); END;
CREATE TRIGGER IF NOT EXISTS comparisons_no_delete BEFORE DELETE ON comparison_reports BEGIN SELECT RAISE(ABORT,'comparison reports are immutable'); END;
'''


class Ledger:
    def __init__(self, db_path=DEFAULT_DB):
        self.path = Path(db_path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript(SCHEMA)

    def _connect(self):
        db = sqlite3.connect(str(self.path), timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=30000')
        db.execute('PRAGMA foreign_keys=ON')
        return db

    @contextmanager
    def transaction(self):
        db = self._connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _event(db, kind, entity_id, detail=None):
        return db.execute('INSERT INTO events(timestamp,kind,entity_id,detail_json) VALUES(?,?,?,?)',
                          (now(), kind, str(entity_id), encode(detail or {}))).lastrowid

    def event(self, kind, entity_id, detail=None):
        with self.transaction() as db:
            return self._event(db, kind, entity_id, detail)

    def observe_once(self, key, kind, entity_id, detail=None):
        """Atomically record a deduplication checkpoint and its corresponding event."""
        with self.transaction() as db:
            if db.execute('SELECT 1 FROM checkpoints WHERE key=?', (key,)).fetchone():
                return False
            db.execute('INSERT INTO checkpoints VALUES(?,?,?)', (key, encode(detail or {}), now()))
            self._event(db, kind, entity_id, detail)
            return True

    def register_artifact(self, path, report_path=None):
        path = Path(path).resolve()
        report_path = Path(report_path or str(path) + '.report.json').resolve()
        report_digest = file_sha(report_path)
        report = read_report(report_path)
        actual = file_sha(path)
        expected = report.get('sha256') or report.get('output_sha256') or report.get('submission_sha256')
        if sha_value(expected, required=True) != actual:
            raise ValueError('Artifact SHA-256 does not match report')
        if report.get('validation', {}).get('ok') is not True:
            raise ValueError('Artifact has not passed validation')
        if report.get('source_changed_during_run') is True or report.get('checked_inputs_changed'):
            raise ValueError('Source or inputs changed during run')
        phase = str(report.get('phase', 'train' if report.get('start') and report.get('end') else '')).upper()
        if phase not in {'SAMPLE', 'TRAIN', 'LIVE', 'FINAL'}:
            raise ValueError('Artifact phase is unknown')
        if phase in {'LIVE', 'FINAL'} and report.get('official_structure', {}).get('ok') is not True:
            raise ValueError('LIVE artifact requires successful official format validation')
        values = (actual, str(path), path.stat().st_size, phase, report_digest, str(report_path),
                  report.get('run_id'), encode(report.get('source_hashes', {})), report.get('finished_at') or now())
        if file_sha(report_path) != report_digest or file_sha(path) != actual:
            raise ValueError('Artifact or report changed while registering')
        with self.transaction() as db:
            existing = db.execute('SELECT * FROM artifacts WHERE sha256=?', (actual,)).fetchone()
            if existing and existing['phase'] != phase:
                raise ValueError('Identical artifact digest has conflicting phase')
            if not existing:
                db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?)', values)
                self._event(db, 'artifact_registered', actual, {'path': str(path), 'report_sha': report_digest})
            inserted = db.execute('INSERT OR IGNORE INTO artifact_reports VALUES(?,?,?,?,?)',
                                  (actual, report_digest, str(report_path), encode(report), now()))
            if inserted.rowcount:
                self._event(db, 'artifact_report_recorded', actual, {'report_sha': report_digest})
            return rowdict(db.execute('SELECT * FROM artifacts WHERE sha256=?', (actual,)).fetchone())

    def register_candidate(self, candidate_id=None, tag=None, owner=None, source_manifest=None,
                           evidence=None, status='discovered'):
        source_json = encode(source_manifest or {})
        evidence_json = encode(evidence or {})
        identity = source_json if source_manifest else encode({'tag': tag, 'evidence': evidence})
        candidate_id = candidate_id or 'candidate-' + hashlib.sha256(identity.encode()).hexdigest()[:24]
        if not source_manifest and not evidence and not tag:
            raise ValueError('Candidate requires source, evidence or a tag')
        with self.transaction() as db:
            old = db.execute('SELECT * FROM candidates WHERE candidate_id=?', (candidate_id,)).fetchone()
            if old:
                if old['source_manifest_json'] != source_json or old['evidence_json'] != evidence_json:
                    raise ValueError('Candidate identity already exists with different evidence')
                return rowdict(old)
            stamp = now()
            db.execute('INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?)',
                       (candidate_id, tag, owner, source_json, evidence_json, status, stamp, stamp))
            self._event(db, 'candidate_registered', candidate_id, {'tag': tag, 'status': status})
            return rowdict(db.execute('SELECT * FROM candidates WHERE candidate_id=?', (candidate_id,)).fetchone())

    def set_candidate_status(self, candidate_id, status, detail=None):
        with self.transaction() as db:
            changed = db.execute('UPDATE candidates SET status=?,updated_at=? WHERE candidate_id=?',
                                 (status, now(), candidate_id))
            if not changed.rowcount:
                raise ValueError('Unknown candidate')
            self._event(db, 'candidate_status', candidate_id, {'status': status, 'detail': detail})

    def record_evaluation(self, candidate_id, dataset_id, report_path, input_manifest_sha=None,
                          scorer_sha=None, artifact_sha=None):
        path = Path(report_path).resolve()
        evidence_sha = file_sha(path)
        report = read_report(path)
        scored = report.get('score') if isinstance(report.get('score'), dict) else report
        if scored.get('ok') is not True or scored.get('score') is None:
            raise ValueError('Evaluation must contain a successful actual score')
        if not dataset_id or str(dataset_id).upper().startswith(('LIVE', 'FINAL')):
            raise ValueError('Local evaluation needs a named SAMPLE/TRAIN dataset')
        embedded_sha = report.get('output_sha256') or report.get('submission_sha256') or report.get('sha256')
        if embedded_sha and artifact_sha and embedded_sha != artifact_sha:
            raise ValueError('Evaluation belongs to another artifact')
        artifact_sha = artifact_sha or embedded_sha
        input_manifest_sha = sha_value(input_manifest_sha or report.get('input_manifest_sha256'))
        if scorer_sha is None:
            scorer_sha = next((v for k, v in report.get('source_hashes', {}).items()
                               if Path(k).name == 'bankrec_score.py'), None)
        scorer_sha = sha_value(scorer_sha)
        metrics = scored.get('metrics', {})
        values = (evidence_sha, candidate_id, artifact_sha, dataset_id, input_manifest_sha, scorer_sha,
                  metric(scored['score']), metric(metrics.get('precision', metrics.get('allocation_precision')), 1),
                  metric(metrics.get('recall', metrics.get('allocation_recall')), 1),
                  metric(metrics.get('txn_accuracy'), 1), metric(metrics.get('exception_macro_f1'), 1),
                  str(path), encode(report), now())
        if file_sha(path) != evidence_sha:
            raise ValueError('Evaluation changed while registering')
        with self.transaction() as db:
            if candidate_id is not None:
                candidate = db.execute('SELECT * FROM candidates WHERE candidate_id=?', (candidate_id,)).fetchone()
                if not candidate:
                    raise ValueError('Unknown candidate')
                if not artifact_sha and not report.get('source_hashes'):
                    raise ValueError('Score-only report cannot be associated to a candidate without artifact evidence')
                if report.get('source_hashes') and candidate_sources(candidate) != report['source_hashes']:
                    raise ValueError('Evaluation source manifest does not match candidate')
            if artifact_sha:
                artifact = db.execute('SELECT * FROM artifacts WHERE sha256=?', (artifact_sha,)).fetchone()
                if not artifact:
                    raise ValueError('Register the evaluation artifact first')
                if artifact['phase'] in {'LIVE', 'FINAL'}:
                    raise ValueError('Local score cannot be attached to LIVE/FINAL artifact')
                if report.get('source_hashes') and not artifact_has_sources(db, artifact, report['source_hashes']):
                    raise ValueError('Evaluation source manifest does not match artifact')
                if candidate_id is not None:
                    check_candidate_artifact(db, candidate, artifact)
            old = db.execute('SELECT * FROM evaluations WHERE evidence_sha=?', (evidence_sha,)).fetchone()
            if old:
                if any(old[k] != v for k, v in [('candidate_id', candidate_id), ('artifact_sha', artifact_sha),
                         ('dataset_id', dataset_id), ('input_manifest_sha', input_manifest_sha), ('scorer_sha', scorer_sha)]):
                    raise ValueError('Evaluation evidence is immutable and already associated differently')
                return rowdict(old)
            db.execute('INSERT INTO evaluations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', values)
            self._event(db, 'evaluation_recorded', evidence_sha, {'candidate_id': candidate_id,
                        'dataset_id': dataset_id, 'score': scored['score']})
            return rowdict(db.execute('SELECT * FROM evaluations WHERE evidence_sha=?', (evidence_sha,)).fetchone())

    def get_submission(self, artifact_sha):
        with closing(self._connect()) as db:
            return rowdict(db.execute('SELECT * FROM submissions WHERE artifact_sha=?', (artifact_sha,)).fetchone())

    def get_artifact(self, artifact_sha):
        with closing(self._connect()) as db:
            return rowdict(db.execute('SELECT * FROM artifacts WHERE sha256=?', (artifact_sha,)).fetchone())

    def get_candidate(self, candidate_id):
        with closing(self._connect()) as db:
            return rowdict(db.execute('SELECT * FROM candidates WHERE candidate_id=?', (candidate_id,)).fetchone())

    def begin_submission(self, artifact_sha, filename, candidate_id=None, phase='LIVE'):
        if phase != 'LIVE':
            raise ValueError('Only LIVE submissions are allowed')
        if Path(filename).name != filename or not filename.endswith(('.csv', '.csv.gz')):
            raise ValueError('Submission filename must be a CSV basename')
        with self.transaction() as db:
            old = db.execute('SELECT * FROM submissions WHERE artifact_sha=?', (artifact_sha,)).fetchone()
            if old:
                raise DuplicateSubmission(rowdict(old))
            artifact = db.execute('SELECT * FROM artifacts WHERE sha256=?', (artifact_sha,)).fetchone()
            if not artifact or artifact['phase'] != 'LIVE':
                raise ValueError('Register a validated LIVE artifact first')
            if candidate_id:
                candidate = db.execute('SELECT * FROM candidates WHERE candidate_id=?', (candidate_id,)).fetchone()
                if not candidate:
                    raise ValueError('Unknown candidate')
                check_candidate_artifact(db, candidate, artifact)
            attempt, stamp = str(uuid.uuid4()), now()
            db.execute('INSERT INTO submissions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (attempt, artifact_sha, phase, candidate_id, None, filename, 'reserved', stamp,
                        stamp, None, None, artifact['report_path']))
            self._event(db, 'submission_reserved', attempt, {'artifact_sha': artifact_sha, 'phase': phase})
            return rowdict(db.execute('SELECT * FROM submissions WHERE local_attempt_id=?', (attempt,)).fetchone())

    def update_submission(self, local_attempt_id, status, sub_id=None, official_score=None,
                          response=None, response_json=None):
        if response is not None and response_json is not None:
            raise ValueError('Use response or response_json, not both')
        response = response if response is not None else response_json
        if isinstance(response, str):
            response = json.loads(response)
        score = metric(official_score)
        with self.transaction() as db:
            old = db.execute('SELECT * FROM submissions WHERE local_attempt_id=?', (local_attempt_id,)).fetchone()
            if not old:
                raise ValueError('Unknown submission attempt')
            if old['sub_id'] and sub_id and old['sub_id'] != sub_id:
                raise ValueError('Submission platform ID cannot change')
            if old['status'] in {'scored', 'invalid', 'error', 'received'} and status != old['status']:
                raise ValueError('Terminal submission cannot regress')
            encoded = encode(response) if response is not None else old['response_json']
            resulting_score = score if score is not None else old['official_score']
            if old['official_score'] is not None and resulting_score != old['official_score']:
                raise ValueError('Official score is immutable')
            if (old['status'], old['sub_id'], old['official_score'], old['response_json']) == (
                    status, sub_id or old['sub_id'], resulting_score, encoded):
                return rowdict(old)
            db.execute('UPDATE submissions SET status=?,sub_id=?,official_score=?,response_json=?,updated_at=? WHERE local_attempt_id=?',
                       (status, sub_id or old['sub_id'], resulting_score, encoded, now(), local_attempt_id))
            self._event(db, 'submission_updated', local_attempt_id, {'status': status,
                        'sub_id': sub_id or old['sub_id'], 'official_score': resulting_score,
                        'response': response})
            return rowdict(db.execute('SELECT * FROM submissions WHERE local_attempt_id=?', (local_attempt_id,)).fetchone())

    def link_submission_candidate(self, local_attempt_id, candidate_id):
        with self.transaction() as db:
            submission = db.execute('SELECT * FROM submissions WHERE local_attempt_id=?', (local_attempt_id,)).fetchone()
            candidate = db.execute('SELECT * FROM candidates WHERE candidate_id=?', (candidate_id,)).fetchone()
            if not submission or not candidate:
                raise ValueError('Unknown submission or candidate')
            artifact = db.execute('SELECT * FROM artifacts WHERE sha256=?', (submission['artifact_sha'],)).fetchone()
            check_candidate_artifact(db, candidate, artifact)
            if submission['candidate_id'] and submission['candidate_id'] != candidate_id:
                raise ValueError('Submission candidate association cannot change')
            if not submission['candidate_id']:
                db.execute('UPDATE submissions SET candidate_id=?,updated_at=? WHERE local_attempt_id=?',
                           (candidate_id, now(), local_attempt_id))
                self._event(db, 'submission_candidate_linked', local_attempt_id, {'candidate_id': candidate_id})

    def import_official_report(self, path, candidate_id=None):
        path = Path(path).resolve()
        record = read_report(path)
        response = record.get('response', record)
        if not isinstance(response, dict):
            raise ValueError('Official response must be an object')
        if record.get('phase', response.get('phase')) != 'LIVE':
            raise ValueError('Only LIVE submission reports can be imported')
        if response.get('phase') not in (None, 'LIVE'):
            raise ValueError('Official response phase conflicts with LIVE report')
        if record.get('sub_id') and response.get('sub_id') and record['sub_id'] != response['sub_id']:
            raise ValueError('Official response submission ID conflicts with wrapper')
        if response.get('status', record.get('stage')) == 'scored':
            if response.get('score') is None:
                raise ValueError('Scored official response must include a finite score')
            metric(response['score'])
        source = record.get('source') or record.get('artifact_path')
        if not source:
            raise ValueError('Official report needs a local artifact path; no guessed association')
        artifact_path = Path(source)
        if not artifact_path.is_absolute():
            artifact_path = ROOT / artifact_path
        artifact = self.register_artifact(artifact_path, record.get('report_path'))
        if record.get('sha256') and record['sha256'] != artifact['sha256']:
            raise ValueError('Official report artifact digest does not match')
        if response.get('file_sha256') and response['file_sha256'] != artifact['sha256']:
            raise ValueError('Official response artifact digest does not match')
        existing = self.get_submission(artifact['sha256'])
        if not existing:
            try:
                existing = self.begin_submission(artifact['sha256'], record.get('filename', response.get('filename', artifact_path.name)), candidate_id=candidate_id)
            except DuplicateSubmission as duplicate:
                existing = duplicate.existing
        if candidate_id:
            self.link_submission_candidate(existing['local_attempt_id'], candidate_id)
        created_at = response.get('created_at') or record.get('started_at')
        if created_at:
            datetime.fromisoformat(created_at.replace('Z', '+00:00'))
            with self.transaction() as db:
                if existing['created_at'] != created_at:
                    db.execute('UPDATE submissions SET created_at=? WHERE local_attempt_id=?', (created_at, existing['local_attempt_id']))
                    self._event(db, 'submission_original_time_imported', existing['local_attempt_id'], {'created_at': created_at})
        result = self.update_submission(existing['local_attempt_id'], response.get('status', record.get('stage', 'unknown')),
                                      sub_id=response.get('sub_id', record.get('sub_id')),
                                      official_score=response.get('score'), response=response)
        self.import_observation(path, 'official_imported', {'sub_id': result['sub_id'], 'artifact_sha': artifact['sha256']})
        return result

    def import_observation(self, path, outcome, detail=None):
        path = Path(path).resolve()
        evidence_sha = file_sha(path)
        key = 'import:' + evidence_sha + ':' + outcome
        with self.transaction() as db:
            if db.execute('SELECT 1 FROM checkpoints WHERE key=?', (key,)).fetchone():
                return {'outcome': outcome, 'evidence_sha': evidence_sha, 'already_recorded': True}
            value = {'path': str(path), 'outcome': outcome, 'evidence_sha': evidence_sha, 'detail': detail}
            db.execute('INSERT INTO checkpoints VALUES(?,?,?)', (key, encode(value), now()))
            self._event(db, 'report_import_' + outcome, evidence_sha, value)
        return value

    def import_comparison(self, path):
        path = Path(path).resolve()
        report = read_report(path)
        evidence_sha = file_sha(path)
        if not isinstance(report.get('windows'), dict) or not report['windows']:
            raise ValueError('Comparison requires evaluation windows')
        # Older review summaries have no verified source/scorer binding. Retain
        # those reports as observations, never silently upgrade them to evidence.
        if any(window.get('tag_source_hashes_verified') is not True for window in report['windows'].values()):
            return self.import_observation(path, 'unassociated_comparison', report)
        for label, window in report['windows'].items():
            sha_value(window.get('scorer_sha256') or window.get('file_hashes', {}).get('official_scorer'), required=True)
            for side in ('before', 'after'):
                sha_value(window.get('file_hashes', {}).get(side), required=True)
                metric(window.get(side + '_score', {}).get('score'))
                if window.get(side + '_score', {}).get('score') is None:
                    raise ValueError('Comparison score is missing')
        if file_sha(path) != evidence_sha:
            raise ValueError('Comparison report changed while registering')
        for sha_key in ('before_sha', 'candidate_sha'):
            commit = report.get(sha_key)
            if not isinstance(commit, str) or not re.fullmatch('[0-9a-f]{40}', commit):
                raise ValueError('Comparison must identify exact source commits')
        candidates = []
        for tag_key, sha_key in [('before_tag', 'before_sha'), ('candidate_tag', 'candidate_sha')]:
            commit = report.get(sha_key)
            candidate = self.register_candidate(candidate_id='git-' + commit, tag=report.get(tag_key),
                                                source_manifest={'git_commit': commit}, status='evaluated')
            candidates.append(candidate['candidate_id'])
        with self.transaction() as db:
            old = db.execute('SELECT * FROM comparison_reports WHERE evidence_sha=?', (evidence_sha,)).fetchone()
            if old:
                return rowdict(old)
            db.execute('INSERT INTO comparison_reports VALUES(?,?,?,?,?,?)',
                       (evidence_sha, str(path), *candidates, encode(report), now()))
            self._event(db, 'comparison_recorded', evidence_sha, {'before': candidates[0], 'after': candidates[1],
                        'windows': list(report['windows'])})
            return rowdict(db.execute('SELECT * FROM comparison_reports WHERE evidence_sha=?', (evidence_sha,)).fetchone())

    def import_report(self, path, candidate_id=None, dataset_id=None, owner=None):
        """Recognise existing reports; preserve unassociated scores as orphan evidence."""
        path = Path(path).resolve()
        report = read_report(path)
        if (report.get('response', {}).get('sub_id') or report.get('sub_id')) and report.get('phase') == 'LIVE':
            return {'kind': 'official', 'submission': self.import_official_report(path, candidate_id)}
        if report.get('windows') and report.get('before_sha') and report.get('candidate_sha'):
            return {'kind': 'comparison', 'comparison': self.import_comparison(path)}
        artifact = None
        if report.get('validation') and (report.get('sha256') or report.get('output_sha256')):
            name = report.get('artifacts', {}).get('submission')
            if name:
                if Path(name).name != name:
                    raise ValueError('Artifact must be a sibling filename')
                artifact_path = path.parent / name
            elif path.name.endswith('.report.json'):
                artifact_path = Path(str(path)[:-len('.report.json')])
            else:
                artifact_path = path.parent / 'submission.csv.gz'
            artifact = self.register_artifact(artifact_path, path)
        if artifact and report.get('source_hashes'):
            candidate = self.register_candidate(candidate_id=candidate_id, owner=owner,
                                                source_manifest=report['source_hashes'], status='validated')
            candidate_id = candidate['candidate_id']
        elif candidate_id:
            # An explicit ID still needs an artifact; a filename is not source provenance.
            if not artifact:
                candidate_id = None
        scored = report.get('score') if isinstance(report.get('score'), dict) else report
        evaluation = None
        if scored.get('ok') is True and isinstance(scored.get('score'), (int, float)):
            if dataset_id is None:
                if report.get('start') and report.get('end'):
                    dataset_id = 'TRAIN:' + report['start'] + ':' + report['end']
                elif report.get('phase') in ('sample', 'train'):
                    dataset_id = report['phase'].upper()
                else:
                    dataset_id = 'UNSPECIFIED'
            evaluation = self.record_evaluation(candidate_id, dataset_id, path,
                                                artifact_sha=artifact['sha256'] if artifact else None)
        if artifact or evaluation:
            self.import_observation(path, 'imported', {'artifact_sha': artifact['sha256'] if artifact else None,
                                                     'evaluation_sha': evaluation['evidence_sha'] if evaluation else None})
            return {'kind': 'run', 'artifact': artifact, 'evaluation': evaluation, 'candidate_id': candidate_id}
        return self.import_observation(path, 'unassociated', report)

    def checkpoint(self, key, value=None, write=False):
        if not write:
            with closing(self._connect()) as db:
                row = db.execute('SELECT value_json FROM checkpoints WHERE key=?', (key,)).fetchone()
                return json.loads(row[0]) if row else None
        with self.transaction() as db:
            db.execute('INSERT INTO checkpoints VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at',
                       (key, encode(value), now()))
            self._event(db, 'checkpoint_updated', key, value)
        return scrub(value)

    def status(self):
        with closing(self._connect()) as db:
            counts = {table: db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                      for table in ['artifacts', 'candidates', 'evaluations', 'submissions', 'events', 'checkpoints', 'comparison_reports']}
            submissions = [rowdict(r) for r in db.execute('SELECT * FROM submissions ORDER BY created_at DESC')]
            evaluations = [rowdict(r) for r in db.execute('SELECT * FROM evaluations ORDER BY created_at DESC LIMIT 100')]
            local = [dict(r) for r in db.execute('SELECT dataset_id,input_manifest_sha,scorer_sha,MAX(score) AS best_score,COUNT(*) AS evaluations FROM evaluations GROUP BY dataset_id,input_manifest_sha,scorer_sha')]
            return {'database': str(self.path), 'counts': counts, 'submissions': submissions,
                    'evaluations': evaluations, 'local_benchmarks': local,
                    'latest_live': submissions[0] if submissions else None,
                    'best_live': db.execute('SELECT MAX(official_score) FROM submissions WHERE status="scored"').fetchone()[0]}

    def integrity_check(self):
        with closing(self._connect()) as db:
            checks = [r[0] for r in db.execute('PRAGMA integrity_check')]
            foreign = [tuple(r) for r in db.execute('PRAGMA foreign_key_check')]
            return {'ok': checks == ['ok'] and not foreign, 'integrity': checks, 'foreign_keys': foreign}

    def backup(self, destination):
        target = Path(destination).resolve()
        if target == self.path or target.exists():
            raise ValueError('Backup destination must be a new file')
        target.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as source, closing(sqlite3.connect(str(target))) as dest:
            source.backup(dest)
        return {'path': str(target), 'sha256': file_sha(target), 'size': target.stat().st_size}

    snapshot = status
