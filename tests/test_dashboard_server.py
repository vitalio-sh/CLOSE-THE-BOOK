import hashlib
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest

from scripts.serve_dashboard import make_server


class DashboardServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'dashboard').mkdir()
        (self.root / 'outputs').mkdir()
        (self.root / 'dashboard/index.html').write_text('<html>Dashboard</html>')
        self.submission = self.root / 'outputs/candidate.csv.gz'
        self.submission.write_bytes(b'compressed submission fixture')
        self.metadata = {'sources': {'submission': {'path': 'outputs/candidate.csv.gz'}},
                         'pipeline': {'sha256': hashlib.sha256(self.submission.read_bytes()).hexdigest()}}
        self.save_metadata()
        self.server = make_server(self.root, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def save_metadata(self):
        (self.root / 'dashboard/data.json').write_text(json.dumps(self.metadata))

    def request(self, path, method='GET', headers=None):
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_allowlist_and_verified_download(self):
        self.assertEqual(self.server.server_address[0], '127.0.0.1')
        self.assertEqual(self.request('/')[0], 200)
        status, headers, payload = self.request('/submission')
        self.assertEqual(status, 200)
        self.assertEqual(payload, self.submission.read_bytes())
        self.assertEqual(headers['Content-Type'], 'application/gzip')
        self.assertIn('filename="candidate.csv.gz"', headers['Content-Disposition'])
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(self.request('/submission', 'HEAD')[2], b'')

    def test_traversal_repository_and_directory_listing_denied(self):
        for path in ['/../outputs/candidate.csv.gz', '/%2e%2e/outputs/candidate.csv.gz',
                     '/dashboard/', '/outputs/', '/.git/config', '/data/workspace/live/bank_transactions.csv.gz']:
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 403)

    def test_changed_submission_is_not_downloaded(self):
        self.submission.write_bytes(b'changed')
        status, _, payload = self.request('/submission')
        self.assertEqual(status, 409)
        self.assertNotEqual(payload, b'changed')

    def test_outside_outputs_and_symlinks_denied(self):
        private = self.root / 'private.csv.gz'
        private.write_bytes(b'secret')
        for target in ['private.csv.gz', 'outputs/../private.csv.gz']:
            self.metadata['sources']['submission']['path'] = target
            self.save_metadata()
            self.assertEqual(self.request('/submission')[0], 403)
        (self.root / 'outputs/link.csv.gz').symlink_to(private)
        self.metadata['sources']['submission']['path'] = 'outputs/link.csv.gz'
        self.save_metadata()
        self.assertEqual(self.request('/submission')[0], 403)
        (self.root / 'dashboard/app.js').symlink_to(private)
        self.assertEqual(self.request('/app.js')[0], 403)

    def test_rebinding_host_and_mutations_denied(self):
        self.assertEqual(self.request('/', headers={'Host': 'untrusted.example'})[0], 403)
        self.assertEqual(self.request('/submission', 'POST')[0], 501)


if __name__ == '__main__':
    unittest.main()
