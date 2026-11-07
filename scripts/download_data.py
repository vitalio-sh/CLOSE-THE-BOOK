#!/usr/bin/env python3
"""Download public event packages, verify SHA-256, optionally extract."""
import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
from urllib.request import urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BASE = 'https://hackathon.usetalky.com'

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def download(item):
    target = ROOT / 'data/archives' / item['url'].split('/')[-1]
    target.parent.mkdir(parents=True, exist_ok=True)
    expected = item['sha256']
    if not expected:
        raise ValueError('Missing published checksum: ' + item['id'])
    if not target.exists() or digest(target) != expected:
        part = target.with_name(target.name + '.part')
        if not part.exists() or digest(part) != expected:
            url = item['url'] if item['url'].startswith('https://') else BASE + item['url']
            subprocess.run(['curl', '-fL', '--retry', '5', '-sS', url, '-o', str(part)], check=True)
        if digest(part) != expected:
            raise ValueError('Checksum mismatch: ' + str(part))
        part.replace(target)
    print('Verified ' + target.name, flush=True)
    return {'id': item['id'], 'path': str(target.relative_to(ROOT)),
            'bytes': target.stat().st_size, 'sha256': expected}

def extract(item):
    source = ROOT / item['path']
    dest = ROOT / 'data/raw' / item['id']
    marker = dest / '.archive-sha256'
    if marker.exists() and marker.read_text().strip() == item['sha256']:
        return
    dest.mkdir(parents=True, exist_ok=True)
    def safe(name):
        p = (dest / name).resolve()
        if dest.resolve() not in p.parents and p != dest.resolve():
            raise ValueError('Unsafe archive path: ' + name)
    if source.suffix == '.zip':
        with zipfile.ZipFile(source) as archive:
            for member in archive.infolist():
                safe(member.filename)
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError('Archive symlink is not supported')
            archive.extractall(dest)
    else:
        with tarfile.open(source, 'r:gz') as archive:
            for member in archive.getmembers():
                safe(member.name)
                if not (member.isfile() or member.isdir()):
                    raise ValueError('Unsupported archive member: ' + member.name)
            archive.extractall(dest)
    marker.write_text(item['sha256'] + '\n')
    print('Extracted ' + item['id'], flush=True)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refresh', action='store_true', help='Refresh official availability and checksums')
    parser.add_argument('--extract', action='store_true', help='Extract docs, full TRAIN, LIVE and available FINAL')
    parser.add_argument('--include-lite', action='store_true', help='Also extract the redundant TRAIN lite package')
    args = parser.parse_args()
    config_path = ROOT / 'docs/source/config.json'
    if args.refresh:
        with urlopen(BASE + '/config.json', timeout=60) as response:
            config = json.load(response)
        config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + '\n')
    config = json.loads(config_path.read_text())
    available = [d for d in config['downloads'] if d['available']]
    for item in config['downloads']:
        if not item['available']:
            print('Not released: ' + item['id'], flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(download, available))
    (ROOT / 'data/manifest.json').write_text(json.dumps(results, indent=2) + '\n')
    if args.extract:
        for item in results:
            if item['id'] != 'train-lite' or args.include_lite:
                extract(item)
        workspace = ROOT / 'data/workspace'
        workspace.mkdir(exist_ok=True)
        for name, target in [('masters', '../raw/docs/masters'), ('sample', '../raw/docs/sample'),
                             ('starter', '../../vendor/starter'), ('train', '../raw/train/train'),
                             ('live', '../raw/live/live'), ('final', '../raw/final/final')]:
            link = workspace / name
            if (workspace / target).exists() and not link.exists() and not link.is_symlink():
                link.symlink_to(target, target_is_directory=True)
        inventory = {}
        for directory in sorted((ROOT / 'data/raw').iterdir()):
            count = size = 0
            for parent, _, files in os.walk(directory):
                for name in files:
                    if name == '.archive-sha256':
                        continue
                    count += 1
                    size += (Path(parent) / name).stat().st_size
            inventory[directory.name] = {'files': count, 'bytes': size,
                                        'contents': sorted(p.name for p in directory.iterdir() if not p.name.startswith('.'))}
        (ROOT / 'data/inventory.json').write_text(json.dumps(inventory, indent=2) + '\n')

if __name__ == '__main__':
    main()
