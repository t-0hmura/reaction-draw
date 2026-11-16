#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Regression test: draw every reaction template and every input in tests/specs/, and fail unless each drawing passes
all checks and its positive control (every active detector catches the error planted for it).

    python tests/run_regression.py [-j JOBS] [--out DIR] [NAME ...]

NAME limits the run to inputs whose file name (without .json) contains it.  Each drawing goes to DIR/NAME with its log
in DIR/NAME.log.  Exit status 0 when every drawing passes, 1 otherwise.
"""
import argparse
import concurrent.futures
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DRAW = ROOT / 'skills' / 'reaction-draw' / 'scripts' / 'draw_figure.py'
INPUTS = sorted((ROOT / 'skills' / 'reaction-draw' / 'templates').glob('*.json')) + sorted((ROOT / 'tests' / 'specs').glob('*.json'))
CONTROL = re.compile(r'^POSITIVE CONTROL: (\d+)/(\d+) planted errors detected; (\d+)/(\d+) detectors covered', re.M)


def run(spec, out):
    name = spec.stem if spec.parent.name == 'specs' else 'template_' + spec.stem
    log = out / (name + '.log')
    start = time.time()
    with open(log, 'w') as fh:
        rc = subprocess.call([sys.executable, str(DRAW), str(spec), '--out', str(out / name)], stdout=fh, stderr=subprocess.STDOUT)
    text = log.read_text(errors='replace')
    result = next((l for l in text.splitlines() if l.startswith('RESULT:')), 'RESULT: (none)')
    control = CONTROL.search(text)
    ok = rc == 0 and result == 'RESULT: all checks pass' and bool(control) and control.group(1) == control.group(2) \
        and control.group(3) == control.group(4)
    why = '' if ok else (result if result != 'RESULT: all checks pass' else 'positive control: ' + (control.group(0) if control else 'missing'))
    return name, ok, time.time() - start, why


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('-j', '--jobs', type=int, default=os.cpu_count() or 2)
    ap.add_argument('--out', default='regression-out')
    ap.add_argument('names', nargs='*')
    args = ap.parse_args()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    specs = [s for s in INPUTS if not args.names or any(n in s.stem for n in args.names)]
    failed = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for name, ok, seconds, why in pool.map(lambda s: run(s, out), specs):
            print('%-4s %-28s %6.1f s %s' % ('ok' if ok else 'FAIL', name, seconds, why), flush=True)
            if not ok:
                failed.append(name)
    print('%d of %d drawings pass' % (len(specs) - len(failed), len(specs)))
    if failed:
        print('failed: ' + ', '.join(failed) + ' (logs in %s)' % out)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
