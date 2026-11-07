#!/usr/bin/env python3
"""Prepare a static, read-only dashboard snapshot for the registered Site."""
import argparse
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'outputs/hosted-dashboard')
    args = parser.parse_args()
    output = args.output.resolve()
    manifest = output / '.openai/hosting.json'
    if not manifest.exists():
        raise SystemExit('Register the Site and persist .openai/hosting.json before packaging.')
    config = json.loads(manifest.read_text())
    if not config.get('project_id') or config.get('static', {}).get('directory') != 'dist':
        raise SystemExit('Expected a registered Site with static.directory=dist.')
    dist = output / 'dist'
    dist.mkdir(exist_ok=True)
    for filename in ('index.html', 'style.css', 'app.js'):
        shutil.copyfile(ROOT / 'dashboard' / filename, dist / filename)
    data = json.loads((ROOT / 'dashboard/data.json').read_text())
    if not data['pipeline'].get('hash_verified'):
        raise SystemExit('Export a verified snapshot before publishing.')
    data['hosting'] = {'public_snapshot': True, 'candidate_download': False}
    (dist / 'data.json').write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')) + '\n')
    html = (dist / 'index.html').read_text().replace('Local run snapshot', 'Published run snapshot')
    html = html.replace('<title>', '<meta name="robots" content="noindex,nofollow"><title>', 1)
    (dist / 'index.html').write_text(html)
    print(json.dumps({'project_id': config['project_id'], 'directory': str(dist), 'examples': len(data['transactions'])}))

if __name__ == '__main__':
    main()
