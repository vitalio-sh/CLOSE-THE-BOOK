#!/usr/bin/env python3
"""Retry publication of an actual validated run, preserving its original identity."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recon.dashboard_publish import publish_dashboard, publish_outbox


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--submission')
    source.add_argument('--snapshot', help='Retry a generated outbox JSON with its matching .status.json')
    parser.add_argument('--data', default='data/workspace')
    args = parser.parse_args()
    status = publish_outbox(args.snapshot) if args.snapshot else publish_dashboard(args.submission, args.data)
    print(json.dumps(status))
    return 0 if status['state'] in ('published', 'superseded', 'skipped') else 1


if __name__ == '__main__':
    raise SystemExit(main())
