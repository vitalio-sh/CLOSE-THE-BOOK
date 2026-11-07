#!/usr/bin/env python3
"""Package the dashboard Worker, public assets and last verified seed snapshot."""
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
        raise SystemExit('Reuse the registered Site and its .openai/hosting.json.')
    config = json.loads(manifest.read_text())
    if not config.get('project_id'):
        raise SystemExit('Expected a registered Site.')
    worker_source = ROOT / 'dashboard/worker.mjs'
    if not worker_source.exists():
        raise SystemExit('Missing dashboard/worker.mjs')
    data = json.loads((ROOT / 'dashboard/data.json').read_text())
    if not data['pipeline'].get('hash_verified'):
        raise SystemExit('Export a verified snapshot before publishing.')
    data['hosting'] = {'public_snapshot': True, 'candidate_download': False, 'auto_refresh': True}
    data['pipeline'].setdefault('run_id', 'legacy-' + data['pipeline']['sha256'])
    data['pipeline'].setdefault('started_at', data['sources']['submission']['modified_at'])
    data['pipeline'].setdefault('finished_at', data['sources']['submission']['modified_at'])
    data['hosting']['published_at'] = data['generated_at']
    assets = {}
    for filename in ('index.html', 'style.css', 'app.js'):
        assets['/' + filename] = (ROOT / 'dashboard' / filename).read_text()
    html = assets['/index.html'].replace('Local run snapshot', 'Automatically updated')
    html = html.replace('<title>', '<meta name="robots" content="noindex,nofollow"><title>', 1)
    assets['/index.html'] = html
    progress_source = (ROOT / 'dashboard/progress.mjs').read_text()
    # The deploy output is a single self-contained ES module.
    worker_code = worker_source.read_text().replace("import {createProgressService, storeRun, recordSnapshotRun} from './progress.mjs';\n", '')
    worker = progress_source + '\n' + worker_code + '\nconst ASSETS = ' + json.dumps(assets,ensure_ascii=False) + ';\n'
    worker += 'const SEED = ' + json.dumps(data,ensure_ascii=False,separators=(',', ':')) + ';\n'
    worker += 'export default createWorker(ASSETS, SEED);\n'
    source_dir = output / 'worker'
    source_dir.mkdir(exist_ok=True)
    (source_dir / 'index.js').write_text(worker)
    config.pop('static', None)
    config['r2'] = 'BUCKET'
    config['d1'] = None
    manifest.write_text(json.dumps(config, indent=2) + '\n')
    dist = output / 'dist'
    if dist.exists():
        shutil.rmtree(dist)  # Owned generated build directory, never source or data.
    (dist / 'server').mkdir(parents=True)
    (dist / 'server/index.js').write_text(worker)
    (dist / '.openai').mkdir()
    shutil.copyfile(manifest, dist / '.openai/hosting.json')
    (output / 'package.json').write_text(json.dumps({'private':True,'type':'module'})+'\n')
    print(json.dumps({'project_id':config['project_id'],'directory':str(dist),
                      'examples':len(data['transactions']),'runtime':'worker','storage':'R2'}))


if __name__ == '__main__':
    main()
