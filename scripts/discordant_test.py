#!/usr/bin/env python3
"""Exact binomial (sign) test on discordant transactions for candidate acceptance.

Each transaction that changes correctness between the baseline and the candidate is a
discordant pair: a fix (wrong -> right) or a regression (right -> wrong). Under H0 (the
change is noise) fixes ~ Binomial(fixes + regressions, 0.5). Reports the one-sided p-value
P(X >= fixes) and the two-sided p-value (McNemar exact). Standard library only.

Usage:
  scripts/discordant_test.py FIXES REGRESSIONS
  scripts/discordant_test.py --compare-json compare_X_vs_Y_october.json [more.json ...]
     (reads diff.n_fixes / diff.n_regressions written by compare_task.py; pools several windows)
"""
import argparse
import json
from math import comb


def one_sided(fixes, regressions):
    n = fixes + regressions
    if n == 0:
        return 1.0
    return sum(comb(n, k) for k in range(fixes, n + 1)) / 2 ** n


def two_sided(fixes, regressions):
    n = fixes + regressions
    if n == 0:
        return 1.0
    lo = min(fixes, regressions)
    return min(1.0, 2 * sum(comb(n, k) for k in range(0, lo + 1)) / 2 ** n)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('counts', nargs='*', type=int, help='FIXES REGRESSIONS')
    ap.add_argument('--compare-json', nargs='+', default=())
    ap.add_argument('--alpha', type=float, default=0.05)
    args = ap.parse_args()
    rows = []
    if args.compare_json:
        for path in args.compare_json:
            diff = json.load(open(path))['diff']
            rows.append((path, int(diff['n_fixes']), int(diff['n_regressions'])))
    elif len(args.counts) == 2:
        rows.append(('counts', args.counts[0], args.counts[1]))
    else:
        ap.error('give FIXES REGRESSIONS or --compare-json')
    for name, f, r in rows:
        print(f'{name}: fixes {f} regressions {r}  one-sided p {one_sided(f, r):.3g}  two-sided p {two_sided(f, r):.3g}')
    if len(rows) > 1:
        f = sum(x[1] for x in rows); r = sum(x[2] for x in rows)
        print(f'pooled: fixes {f} regressions {r}  one-sided p {one_sided(f, r):.3g}  two-sided p {two_sided(f, r):.3g}')
    f = sum(x[1] for x in rows); r = sum(x[2] for x in rows)
    verdict = 'ACCEPT' if one_sided(f, r) < args.alpha and f > r else 'NOT SIGNIFICANT'
    print(f'verdict (one-sided, alpha {args.alpha}): {verdict}')


if __name__ == '__main__':
    main()
