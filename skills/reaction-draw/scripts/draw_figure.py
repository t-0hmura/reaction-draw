#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Draw a figure from an input file in one command: layout, Octet Draw, save and reload, SVG and PNG, every check.

  python draw_figure.py INPUT.json --out DIR [--dpi 600 | --png-width N] [--browser-cmd "ssh HOST PYTHON"]
                        [--positive-control] [--quiet] [--octet PATH --node-modules PATH]
  python draw_figure.py A.json B.json C.json --out DIR     (versions drawn at once, each in DIR/<name>; BEST names one)
  python draw_figure.py --check-env [--browser-cmd "ssh HOST PYTHON"]

Exit status (of the BEST version when several are drawn): 0 when every check passes (and, with --positive-control, the positive control finds every planted error), 1 when a check fails,
2 when the input file is wrong or a stage cannot run. Stages, outputs and options: the repository's docs/backend.md.
"""
import argparse
import collections
import concurrent.futures
import copy
import json
import math
import os
import signal
import sys
import time
import platform
import re
import shutil
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True               # an installed skill folder may be read-only
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / 'octet'))
import specutil  # noqa: E402
import check_rules  # noqa: E402
import stereochemistry as stereo  # noqa: E402

if os.name == 'nt':
    import ctypes
    import msvcrt
    from ctypes import wintypes
    _KERNEL32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _KERNEL32.LockFile.argtypes = [wintypes.HANDLE] + [wintypes.DWORD] * 4
    _KERNEL32.LockFile.restype = wintypes.BOOL
else:
    import fcntl

SMALL_DPI = 150
MAX_PAIR_DRAWS = 30        # drawings of the axial-pair moves per run; the regression inputs that need them use at most 28
PAIR_WORKERS = min(4, int(os.environ.get('REACTION_DRAW_WORKERS') or os.cpu_count() or 1))   # candidates drawn side by side
ACS_COLUMNS_PT = ((234, 'single column (3.25 in)'), (504, 'double column (7 in)'))
START = time.time()
LAST_PROGRESS = None
SPEC_NEXT = 'NEXT: fix each input error in the input file and rerun the same command; do not stop.'
# what a run writes into DIR; the files of an earlier run are removed first, so that a failed run leaves no stale figure
# or verdict behind (.state.json then says why it failed)
OUTPUTS = ('figure.cdxml', 'figure.svg', 'figure.png', 'figure_small.png', '.state.json')


def _progress(message, always=False):
    """Print the first drawing and then at most one stage update per minute."""
    global LAST_PROGRESS
    now = time.time()
    if always or LAST_PROGRESS is None or now - LAST_PROGRESS >= 60:
        print('progress (%3.0f s): %s' % (now - START, message), file=sys.stderr, flush=True)
        LAST_PROGRESS = now


def _write_state(out, **parts):
    state = check_rules.load_state(out) if (out / '.state.json').is_file() else {}
    state.update(parts)
    (out / '.state.json').write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding='utf-8')


def _lock(path):
    """Open and lock `path` without waiting (BlockingIOError when another process holds it). The OS releases the lock when
    the file closes or the process ends, even if it is killed; the file is never truncated or written."""
    handle = open(path, 'a+b')
    try:
        if os.name == 'nt':
            if not _KERNEL32.LockFile(msvcrt.get_osfhandle(handle.fileno()), 0, 0, 1, 0):
                code = ctypes.get_last_error()
                raise BlockingIOError(code, 'locked') if code == 33 else ctypes.WinError(code)   # 33: ERROR_LOCK_VIOLATION
        else:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise
    return handle


def run(spec_path, out_dir, png_width=None, octet_dir=None, node_modules=None, browser_cmd=None, positive_control=False,
        draw=None, log=print, dpi=None):
    """Draw and check while holding the output-directory lock."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    try:
        lock = _lock(out / '.draw.lock')  # noqa: F841 (held until this function returns or the process ends)
    except OSError:
        message = 'input error: another draw_figure.py is writing %s; wait for it to finish or use another --out' % out
        log(message)
        return 2, {'error': message}
    return _run(spec_path, out_dir, png_width, octet_dir, node_modules, browser_cmd, positive_control, draw, log, dpi)


def _failed(out, what, message, notes, times, log, stereo_lines=()):
    """A run that stopped before its checks: the state says where and why; returns its lines."""
    if what == 'input error':
        lines = list(stereo_lines) + ['input error: ' + problem for problem in message]
    else:
        lines = ['%s: %s' % (what, message)] + ['note: %s' % n for n in notes]
        lines.extend(stereo_lines)
    if times:
        lines.append('stages (s): ' + ', '.join('%s %.1f' % (k, v) for k, v in times.items()))
    lines.append('RESULT: FAILED (%s)' % what)
    if what == 'input error':
        lines.append(SPEC_NEXT)
    _write_state(out, summary=lines)
    for line in lines:
        log(line)
    return lines


def _backend():
    try:
        import octet_draw
    except ImportError as e:
        raise RuntimeError('cannot load the Octet backend (octet/octet_draw.py): %s' % e)
    if not hasattr(octet_draw, 'draw'):
        raise RuntimeError('octet/octet_draw.py has no draw(resolved, out_dir, ...) function')
    return octet_draw.draw


def size_line(out):
    """True size of figure.svg, the ACS column it fits (a note only) and the PNG sizes."""
    m = re.search(r'<svg[^>]*?\bwidth="([\d.]+)pt"[^>]*?\bheight="([\d.]+)pt"', (out / 'figure.svg').read_text(encoding='utf-8'))
    if not m:
        return 'size: figure.svg has no width and height in pt'
    w, h = float(m.group(1)), float(m.group(2))
    fit = next(('fits the ACS ' + name for limit, name in ACS_COLUMNS_PT if w <= limit),
               'wider than the ACS double column (7 in), so it will be reduced')
    pngs = []
    for name in ('figure.png', 'figure_small.png'):
        if (out / name).exists():
            pw, ph = specutil.png_size((out / name).read_bytes())
            pngs.append('%s %d x %d px (%.0f dpi)' % (name, pw, ph, pw / w * 72))
    return 'size: %.0f x %.0f pt (%.2f x %.2f in), %s; %s' % (w, h, w / 72, h / 72, fit, ', '.join(pngs))


def _counts(checked):
    return {n: len(c['findings']) for n, c in checked['checks'].items()}


def _gaps(checked):
    """{pair of species: pt} from the species_gap warnings (two species closer than half a bond)."""
    out = {}
    for w in checked['checks'].get('species_gap', {}).get('warnings', []):
        m = re.match(r'^(\S+):\d+ and (\S+):\d+ are (-?[\d.]+) pt apart', w)
        if m:
            key = frozenset(m.group(1, 2))
            out[key] = min(out.get(key, 1e9), float(m.group(3)))
    return out


def _closer(result, checked):
    """True when a candidate brings two species closer than half a bond where they were not, or closer still."""
    before, after = _gaps(result), _gaps(checked)
    return any(k not in before or g < before[k] - 0.05 for k, g in after.items())


def _tighter(result, checked):
    """True when a candidate has more curved arrows close to other ink (arrows_clear warnings) than the checked figure."""
    def count(r):
        return len(r['checks'].get('arrows_clear', {}).get('warnings', []))
    return count(checked) > count(result)


def _no_gain(result):
    """Accept a candidate when no detector gains a finding, the total falls, and no two species come closer than half a
    bond where they were not."""
    before = _counts(result)
    def accept(checked):
        after = _counts(checked)
        return (all(after[n] <= before[n] for n in before) and sum(after.values()) < sum(before.values())
                and not _closer(result, checked) and not _tighter(result, checked))
    return accept


def _fixed_curved(saved, keep):
    """Saved routes of the curved arrows whose id passes keep(id), to be drawn again unchanged."""
    return {c['id']: {'from': [c['from'][0], -c['from'][1]], 'to': [c['to'][0], -c['to'][1]], 'bend': c.get('bend', 0),
                      'start': c.get('start'), 'end': c.get('end')}
            for c in saved.get('curved', []) if keep(c.get('id'))}


def _keep_saved_marks(atoms, saved, skip_prefix):
    """Keep the saved label alignment and charge angle of atoms outside the panel being changed."""
    for kept in saved.get('atoms', []):
        if not kept['ref'].startswith(skip_prefix):
            atoms[kept['ref']]['native_align'] = kept.get('align')
            if kept.get('charge_angle') is not None:
                atoms[kept['ref']]['charge_angle'] = kept['charge_angle']


def _more_crossings(resolved, fig):
    """True when the moved coordinates themselves hold more crossing bonds or too-close atoms than the checked figure's:
    bond_crossing would gain a finding, so the candidate is not drawn."""
    def count(res):
        return len(check_rules.det_bond_crossing(check_rules.Figure(res, {}, {}, None)))
    return count(resolved) > count(fig.resolved)


def _names(aid, text):
    """True when text names the arrow id as a whole word (A1, not the A1 of A10)."""
    return re.search(r'(?<![\w-])' + re.escape(aid) + r'(?![\w-])', text) is not None


def _retryable(result):
    """Retry only if at least one finding can change with automatic placement (of the hydrogen-bond findings, a line
    that crosses a bond or another hydrogen bond's line)."""
    return any(not (name in ('arrows_steps', 'residue_codes', 'hbond_dots')
                    or name == 'radical_marks' and 'not from radicals' in finding
                    or name == 'arrows_ends' and finding.startswith('arrow ') and ': no route found from ' in finding
                    or ('hbond ' in finding or 'hydrogen bond' in finding)
                    and not (name == 'bond_crossing' and finding.startswith('hbond ')
                             and (' crosses bond ' in finding or ' crosses hbond ' in finding)))
               for name in result['failed'] for finding in result['checks'][name]['findings'])


def _candidate(resolved, out, fig, draw, draw_args, options, accept, stats, stage, keep_resolved=False):
    """Draw one retry candidate in a scratch folder and check it; copy its files into out when accept(checks).
    Returns (figure, checks, accepted, error); error names why nothing could be drawn or checked."""
    return _candidates([(resolved, options)], out, fig, draw, draw_args, accept, stats, stage, keep_resolved)[0]


def _candidates(jobs, out, fig, draw, draw_args, accept, stats, stage, keep_resolved=False, fit=None):
    """Draw the (resolved, options) jobs side by side and check each; copy the files of the first accepted job (and,
    with fit, one whose figure fit(figure) takes) into out.
    Returns one (figure, checks, accepted, error) per job, accepted True for that first job only."""
    def draw_and_check(job, dest):
        _progress(stage)
        try:
            draw(job[0], dest, options=job[1], **draw_args)
            candidate = check_rules.load_figure(dest)
            candidate.name = fig.name
            return candidate, check_rules.run_checks(candidate), None
        except Exception as exc:
            return None, None, '%s: %s' % (type(exc).__name__, exc)
    stats['draws'] += len(jobs)
    with tempfile.TemporaryDirectory(prefix='.retry-', dir=str(out)) as scratch:
        dests = [Path(scratch) / str(i) for i in range(len(jobs))]
        for dest in dests:
            dest.mkdir()
        with concurrent.futures.ThreadPoolExecutor(min(len(jobs), PAIR_WORKERS)) as pool:
            checked_jobs = list(pool.map(draw_and_check, jobs, dests))
        results, adopted = [], False
        for dest, (candidate, checked, error) in zip(dests, checked_jobs):
            ok = error is None and not adopted and accept(checked) and (fit is None or fit(candidate))
            if ok:
                if keep_resolved:
                    _write_state(dest, resolved=check_rules.load_state(out)['resolved'])
                for name in OUTPUTS:
                    if (dest / name).is_file():
                        shutil.copyfile(dest / name, out / name)
                adopted = True
            results.append((candidate, checked, ok, error))
        return results


def retry_placements(resolved, out, fig, result, draw, draw_args, stats):
    """Try extra backend candidates only after an exact rule fails; keep checks and passing figures unchanged."""
    import copy
    notes, accepted = [], {}
    for targets, flag in ((('charge_own_atom', 'overlap_charge', 'charge_open'), 'repairCharges'),
                          (('arrows_ends', 'arrows_shape', 'overlap_arrow_text', 'overlap_arrow_bond', 'overlap_arrow_arrow'), 'repairArrows'),
                          (('number_place', 'number_clear'), 'repairNumbers')):
        if not _retryable(result):
            break
        failed = [name for name in targets if name in result['failed']]
        if not failed:
            continue
        options = dict(accepted, **{flag: True})
        findings = ' '.join(x for name in failed for x in result['checks'][name]['findings'])
        if flag == 'repairNumbers':
            # Leave 0.5 pt for the glyph-box change on save/reload. Check thresholds
            # stay unchanged; only the failed drawing's placement search is wider.
            options['ownMarginPx'] = 8.5
        if flag == 'repairArrows':
            if 'overlap_arrow_arrow' in failed:
                options['reverseCurved'] = True
                options['hookSeats'] = True
            options['repairArrowIds'] = [c['id'] for p in resolved['panels'] for c in p['curved']
                                         if _names(c['id'], findings)]
            if not options['repairArrowIds']:
                options.pop('repairArrowIds')
        saved = check_rules.load_state(out)['reload']
        retry_resolved = copy.deepcopy(fig.resolved)
        atoms = {a['ref']: a for p in retry_resolved['panels'] for a in p['atoms']}
        for kept in saved.get('atoms', []):
            a = atoms[kept['ref']]
            a['native_align'] = kept.get('align')
            if flag == 'repairCharges' and kept['ref'] in findings:
                a.pop('charge_angle', None)
            elif kept.get('charge_angle') is not None:
                a['charge_angle'] = kept['charge_angle']
        repair_ids = set(options.get('repairArrowIds', [])) if flag == 'repairArrows' else set()
        options['fixedCurved'] = _fixed_curved(saved, lambda i: i not in repair_ids)
        options['fixedNumbers'] = {n['ref']: n for n in saved.get('numbers', [])
                                   if flag != 'repairNumbers' or n['ref'] not in findings}
        before = _counts(result)
        def better(checked):
            after = _counts(checked)
            return all(after[n] <= before[n] for n in before) and any(after[n] < before[n] for n in failed)
        candidate, checked, ok, error = _candidate(retry_resolved, out, fig, draw, draw_args, options, better,
                                                 stats, 'retry_placements', keep_resolved=True)
        if error:
            notes.append('extra placement candidates (%s): %s; kept the original drawing' % (', '.join(failed), error))
            continue
        notes.append('extra placement candidates (%s): %s; findings %d -> %d' % (
            ', '.join(failed), 'kept the first improved drawing (no detector gained findings)' if ok else 'kept the original drawing',
            sum(before[n] for n in failed), sum(_counts(checked if ok else result)[n] for n in failed)))
        if ok:
            fig, result, accepted = candidate, checked, options
            HOOK_SEATS['on'] = HOOK_SEATS['on'] or bool(options.get('hookSeats'))
    return fig, result, notes


def retry_iupac_pairs(out, fig, result, draw, draw_args, stats):
    """A free axial pair may need a small rigid turn of its molecule for a clear arrow.

    Keep connectivity, bond lengths, internal angles and stereo. Every candidate is
    checked on saved ink; keep it only if no detector gains a finding.
    """
    import copy
    import math
    notes = []
    targets = {'arrows_ends', 'arrows_shape', 'overlap_arrow_bond', 'overlap_arrow_text', 'overlap_arrow_arrow', 'overlap_text_text', 'overlap_text_bond'}
    for round_number in range(1, 4):
        if not _retryable(result) or not targets.intersection(result['failed']):
            break
        findings = ' '.join(x for n in targets for x in result['checks'][n]['findings'])
        sources = [(c['from']['refs'][0], c['id']) for p in fig.resolved['panels'] for c in p['curved']
                   if c['from']['kind'] == 'atom' and c['electrons'] == 2
                   and (_names(c['id'], findings)
                        or c['from']['refs'][0] in findings)]
        accepted = False
        for ref, aid in dict.fromkeys(sources):
            # Retain checked routes in other panels while this panel is adjusted.
            panel_id = next(p['id'] for p in fig.resolved['panels'] if any(c['id'] == aid for c in p['curved']))
            all_findings = ' '.join(x for check in result['checks'].values() for x in check['findings'])
            untouched = {c['id'] for p in fig.resolved['panels'] if p['id'] != panel_id for c in p['curved']
                         if not _names(c['id'], all_findings)}
            saved = check_rules.load_state(out)['reload']
            fixed = _fixed_curved(saved, lambda i: i in untouched)
            fixed_numbers = {n['ref']: n for n in saved.get('numbers', [])
                             if not n['ref'].startswith(panel_id + '.') and n['ref'] not in all_findings}
            motions = ([(-90, 0, .7), (90, 0, .7), (0, 0, .1), (0, 0, -.1)]
                       if {'overlap_text_text', 'overlap_text_bond'}.intersection(result['failed']) else [])
            motions += [(d, 0, 0) for d in (-5, 5, -10, 10)]
            motions += [(0, distance * math.cos(math.radians(d)), -distance * math.sin(math.radians(d)))
                        for distance in (.1, .2) for d in range(0, 360, 45)]
            # A neighboring label can block every short axial route. A quarter
            # turn and at most one bond of separation keep the molecule intact.
            motions += [(d, sx, sy) for d in (-90, 90) for sx, sy in
                        ((0, .7), (.7, 0), (0, -.7), (-.7, 0), (1, 0), (-1, 0), (0, 1), (0, -1))]
            trials = [(d, sx, sy, repair) for d, sx, sy in motions
                      for repair in ((False, True) if abs(d) in (5, 10) else (True,))]
            jobs = []
            for degree, sx, sy, repair in trials:
                resolved = copy.deepcopy(fig.resolved)
                atoms = {a['ref']: a for p in resolved['panels'] for a in p['atoms']}
                _keep_saved_marks(atoms, saved, panel_id + '.')
                root, sid = atoms[ref], atoms[ref]['species']
                x0, y0 = root['x'], root['y']
                co, si = math.cos(math.radians(degree)), math.sin(math.radians(degree))
                for a in atoms.values():
                    if a['species'] == sid:
                        a.pop('native_align', None)
                        dx, dy = a['x'] - x0, a['y'] - y0
                        a['x'], a['y'] = x0 + dx * co + dy * si + sx, y0 - dx * si + dy * co + sy
                if _more_crossings(resolved, fig):
                    continue
                options = {'repairArrows': True, 'repairCharges': repair, 'repairNumbers': repair, 'ownMarginPx': 8.5,
                           'reverseCurved': 'overlap_arrow_arrow' in result['failed'], 'fixedCurved': fixed, 'fixedNumbers': fixed_numbers}
                jobs.append(((resolved, options), (degree, sx, sy, sid)))
            drawn = 0
            while jobs and not accepted:
                room = MAX_PAIR_DRAWS - stats['draws']
                if room <= 0:
                    notes.append('axial-pair moves stopped after %d drawings' % MAX_PAIR_DRAWS)
                    return fig, result, notes
                batch, jobs = jobs[:min(PAIR_WORKERS, room)], jobs[min(PAIR_WORKERS, room):]
                checked_jobs = _candidates([job for job, _ in batch], out, fig, draw, draw_args, _no_gain(result),
                                           stats, 'retry_iupac_pairs')
                for (_, (degree, sx, sy, sid)), (candidate, checked, ok, error) in zip(batch, checked_jobs):
                    drawn += 1
                    if error:
                        notes.append('axial-pair candidate %s %+g deg: %s' % (sid, degree, error))
                    elif ok:
                        stats['accepted'].append({'arrow': aid, 'round': round_number, 'candidate': drawn})
                        notes.append('axial lone pair for %s: rigid move of %s (%+g deg, offset %.3f/%.3f bonds); findings %d -> %d' %
                                     (aid, sid, degree, sx, sy, sum(_counts(result).values()), sum(_counts(checked).values())))
                        fig, result, accepted = candidate, checked, True
                        break
            if accepted:
                break
        if not accepted or result['ok']:
            break
    return fig, result, notes


def retry_forming_branch(out, fig, result, draw, draw_args, stats):
    """Open a blocked intramolecular forming-bond corridor by rigid bridge rotation.

    Keep every bond length and branch configuration; accept only a saved drawing
    that removes a finding without adding one to any detector.
    """
    import collections
    notes = []
    if 'overlap_arrow_bond' not in result['failed']:
        return fig, result, notes
    findings = ' '.join(result['checks']['overlap_arrow_bond']['findings'])
    saved = check_rules.load_state(out)['reload']
    for panel in fig.resolved['panels']:
        graph = collections.defaultdict(set)
        for b in panel['bonds']:
            graph[b['a']].add(b['b'])
            graph[b['b']].add(b['a'])
        for arrow in panel['curved']:
            if not _names(arrow['id'], findings) or arrow['to']['kind'] != 'gap':
                continue
            left, right = arrow['to']['refs']
            if left not in graph or right not in graph:
                continue
            # A bridge on the shortest path to the receiving atom defines a rigid branch.
            queue, previous = collections.deque([left]), {left: None}
            while queue and right not in previous:
                here = queue.popleft()
                for neighbor in sorted(graph[here]):
                    if neighbor not in previous:
                        previous[neighbor] = here
                        queue.append(neighbor)
            if right not in previous:
                continue
            path = [right]
            while previous[path[-1]] is not None:
                path.append(previous[path[-1]])
            path.reverse()
            # (pivot, root, other end): the bridge next to the receiving atom first, then the giving side's bridges
            # from the giving atom on (a chorismate side chain turned about its ring atom)
            turns = [(path[-2], path[-1], left)] if len(path) > 2 else []
            turns += [(path[k + 1], path[k], right) for k in range(1, len(path) - 1)]
            for pivot, root, other in turns:
                branch, queue = {root}, collections.deque([root])
                while queue:
                    here = queue.popleft()
                    for neighbor in graph[here] - {pivot} - branch:
                        branch.add(neighbor)
                        queue.append(neighbor)
                if other in branch or any(n != root and n in graph[pivot] for n in branch):
                    continue  # a ring is never cut or deformed
                found = _turn_branch(out, fig, result, draw, draw_args, saved, panel, graph, arrow, pivot, branch, notes, stats)
                if found:
                    return found
    return fig, result, notes


def _turn_branch(out, fig, result, draw, draw_args, saved, panel, graph, arrow, pivot, branch, notes, stats):
    """Draw the branch turned rigidly about pivot by 120, -120, 60, -60 and 180 degrees (and with the terminal arms of an
    S, P or N exchanged); (fig, result, notes) of the first drawing that removes a finding without adding one, else None."""
    import copy
    import math
    untouched = {c['id'] for p in fig.resolved['panels'] if p['id'] != panel['id'] for c in p['curved']}
    fixed = _fixed_curved(saved, lambda i: i in untouched)
    fixed_numbers = {n['ref']: n for n in saved.get('numbers', []) if not n['ref'].startswith(panel['id'] + '.')}
    for degree, swap_ends in ((d, swap) for d in (120, -120, 60, -60, 180) for swap in (False, True)):
        resolved = copy.deepcopy(fig.resolved)
        atoms = {a['ref']: a for p in resolved['panels'] for a in p['atoms']}
        x0, y0 = atoms[pivot]['x'], atoms[pivot]['y']
        _keep_saved_marks(atoms, saved, panel['id'] + '.')
        co, si = math.cos(math.radians(degree)), math.sin(math.radians(degree))
        for ref in branch:
            a = atoms[ref]
            dx, dy = a['x'] - x0, a['y'] - y0
            a['x'], a['y'] = x0 + co * dx + si * dy, y0 - si * dx + co * dy
        for ref in graph:
            atoms[ref].pop('charge_angle', None)
            atoms[ref].pop('native_align', None)
        if swap_ends:
            swapped = False
            for ref in sorted(branch):
                leaves = sorted(n for n in graph[ref] if len(graph[n]) == 1 and n in branch)
                if len(leaves) != 2 or atoms[ref]['el'] not in ('S', 'P', 'N'):
                    continue
                a, b = (atoms[n] for n in leaves)
                center = atoms[ref]
                da, db = math.hypot(a['x'] - center['x'], a['y'] - center['y']), math.hypot(b['x'] - center['x'], b['y'] - center['y'])
                if min(da, db) <= 0:
                    continue
                ax, ay, bx, by = a['x'], a['y'], b['x'], b['y']
                a['x'], a['y'] = center['x'] + (bx - center['x']) * da / db, center['y'] + (by - center['y']) * da / db
                b['x'], b['y'] = center['x'] + (ax - center['x']) * db / da, center['y'] + (ay - center['y']) * db / da
                swapped = True
            if not swapped:
                continue
        if _more_crossings(resolved, fig):
            continue
        options = {'fixedCurved': fixed, 'fixedNumbers': fixed_numbers, 'repairArrows': True,
                   'repairArrowIds': [c['id'] for c in panel['curved']], 'ownMarginPx': 8.5}
        candidate, checked, ok, error = _candidate(resolved, out, fig, draw, draw_args, options, _no_gain(result),
                                                 stats, 'retry_forming_branch')
        if error:
            notes.append('forming-bond corridor %s %+d deg: %s' % (arrow['id'], degree, error))
        elif ok:
            return candidate, checked, notes + ['forming-bond corridor %s: rigid branch about %s rotated %+d degrees%s; all bond lengths and checked configurations retained' % (arrow['id'], pivot, degree, ', terminal-arm directions exchanged' if swap_ends else '')]
    return None


HBOND_FLIP_MAX = 6          # mirrored hydrogen-bonded groups drawn side by side
HBOND_FLIP_CHECKS = ('overlap_text_bond', 'overlap_text_text', 'overlap_arrow_bond', 'overlap_arrow_text',
                     'overlap_arrow_arrow', 'bond_crossing', 'arrows_ends', 'arrows_shape')
HBOND_FLIP_KEEP = ('graph', 'stereochemistry', 'charges', 'hydrogens', 'labels_drawn', 'arrows_count', 'arrows_head',
                   'arrows_color', 'colors', 'cdxml', 'frame')


def _mirrored(resolved, pid, group, axis):
    """A copy of resolved in which the species of group in panel pid are mirrored left-right (axis 'x') or up-down
    ('y') about the middle of their extent; wedges and hashes swap so that every configuration stays, a manual bend
    between two of their atoms turns the other way, and a text put near one of their atoms goes along."""
    r = copy.deepcopy(resolved)
    p = next(q for q in r['panels'] if q['id'] == pid)
    atoms = [a for a in p['atoms'] if a['species'] in group]
    k = 'x' if axis == 'x' else 'y'
    c = (min(a[k] for a in atoms) + max(a[k] for a in atoms)) / 2
    refs = {a['ref'] for a in atoms}
    for a in atoms:
        a[k] = round(2 * c - a[k], 4)
    for b in p['bonds']:
        if b['a'] in refs and b['b'] in refs and b.get('style') in ('wedge', 'hash'):
            b['style'] = 'hash' if b['style'] == 'wedge' else 'wedge'
    for arrow in p['curved']:
        if arrow.get('bend') is not None and all(x in refs for x in arrow['from']['refs'] + arrow['to']['refs']):
            arrow['bend'] = -arrow['bend']
    for t in p.get('texts', []):
        if t.get('near') in refs:
            t[k] = round(2 * c - t[k], 4)
    return r


def _hbond_groups(resolved, pids):
    """(panel, species, group) for each species of the panels pids that takes part in a hydrogen bond: the group is it
    and the species joined to it by hydrogen bonds alone (no arrow or other hydrogen bond ties them elsewhere), most
    atoms first."""
    out = []
    for p in resolved['panels']:
        if p['id'] not in pids or not p.get('hbonds'):
            continue
        of = lambda ref: ref.rsplit(':', 1)[0]
        hb, tied = collections.defaultdict(set), collections.defaultdict(set)
        for a, b in p['hbonds']:
            hb[of(a)].add(of(b))
            hb[of(b)].add(of(a))
        for arrow in p['curved']:
            ends = {of(x) for x in arrow['from']['refs'] + arrow['to']['refs']}
            for e in ends:
                tied[e] |= ends - {e}
        if any(b.get('ts') for b in p.get('boxes', [])):
            continue
        size = collections.Counter(a['species'] for a in p['atoms'])
        for sid in sorted(hb, key=lambda x: -size[x]):
            group = {sid} | {t for t in hb[sid] if t != sid and hb[t] <= {sid} and not tied[t] - {sid}}
            out.append((p['id'], sid, group, size[sid] + sum(size[t] for t in group - {sid})))
    out.sort(key=lambda g: -g[3])
    return [(pid, sid, group) for pid, sid, group, _ in out]


def retry_hbond_flips(out, fig, result, draw, draw_args, stats):
    """When a panel with hydrogen bonds fails an overlap, crossing or arrow check: draw its hydrogen-bonded groups
    mirrored to the other side (left-right and up-down, at most HBOND_FLIP_MAX side by side) and keep the one that most
    lowers the failed checks and findings, gains none in HBOND_FLIP_KEEP and brings no two species closer."""
    failed = [n for n in HBOND_FLIP_CHECKS if n in result['failed']]
    if not failed:
        return fig, result, []
    panels = {q['id'] for q in fig.resolved['panels']}
    pids = {m.group(1) for n in failed for x in result['checks'][n]['findings']
            for m in re.finditer(r'(?<![\w.])([\w-]+)\.[\w-]+:\d+', x) if m.group(1) in panels}
    groups = _hbond_groups(fig.resolved, pids)
    jobs = [((pid,), sid, group, axis) for pid, sid, group in groups for axis in ('y', 'x')]
    # a hydrogen bond's line that crosses a bond or another one: first the smaller species of the crossing, mirrored in
    # every panel where it crosses
    size = collections.Counter(a['species'] for p in fig.resolved['panels'] for a in p['atoms'])
    crossed = collections.defaultdict(set)
    for x in result['checks']['bond_crossing']['findings']:
        if x.startswith('hbond ') and (' crosses bond ' in x or ' crosses hbond ' in x):
            named = dict.fromkeys(r.rsplit(':', 1)[0] for r in fig.atoms
                                  if re.search(r'(?<![\w.])' + re.escape(r) + r'(?!\d)', x))
            largest = max(named, key=lambda sid: size[sid])
            for sid in named:
                if sid != largest:
                    crossed[sid.split('.', 1)[1]].add(sid.split('.', 1)[0])
    first = [(tuple(sorted(crossed[key])), sorted(crossed[key])[0] + '.' + key, {key}, axis)
             for key in sorted(crossed, key=lambda k: size[sorted(crossed[k])[0] + '.' + k]) for axis in ('y', 'x')]
    jobs = (first + jobs)[:HBOND_FLIP_MAX]
    if not jobs:
        return fig, result, []
    def rank(checked):
        return len(checked['failed']), sum(len(c['findings']) for c in checked['checks'].values())
    def better(checked):
        return (not checked['checks']['stereochemistry']['findings']
                and all(set(checked['checks'][n]['findings']) <= set(result['checks'][n]['findings']) for n in HBOND_FLIP_KEEP)
                and rank(checked) < rank(result) and not _closer(result, checked)
                and not any('no route found' in x and x not in result['checks']['arrows_ends']['findings']
                            for x in checked['checks']['arrows_ends']['findings']))
    def mirrored(pids, group, axis):
        r = fig.resolved
        for pid in pids:
            r = _mirrored(r, pid, {pid + '.' + g.split('.', 1)[-1] if '.' not in g or g.split('.', 1)[0] not in pids else g
                                   for g in group}, axis)
        return r
    drawn = [(mirrored(pids, group, axis), None) for pids, sid, group, axis in jobs]
    results = _candidates(drawn, out, fig, draw, draw_args, lambda checked: False, stats, 'retry_hbond_flips')
    scored = [(rank(checked), k) for k, (_, checked, _, error) in enumerate(results) if error is None and better(checked)]
    if not scored:
        return fig, result, []
    _, k = min(scored)
    candidate, checked, ok, _ = _candidate(drawn[k][0], out, fig, draw, draw_args, None, better, stats, 'retry_hbond_flips')
    if not ok:
        return fig, result, []
    pids, sid, group, axis = jobs[k]
    return candidate, checked, ['hydrogen-bonded group %s mirrored %s in panel%s %s' % (
        ', '.join(sorted(g.split('.', 1)[-1] for g in group)), 'left-right' if axis == 'x' else 'up-down',
        's' if len(pids) > 1 else '', ', '.join(pids))]


HOOK_SEATS = {'on': False}     # a kept drawing of this run needed hookSeats (two arrows touched): later retries keep it


def _repair_options(fig, result):
    """The wider searches for a retry candidate: arrows and numbers, and charges only when a charge check failed (a wider
    charge search moves charges that were fine and takes the seats of numbers); hookSeats once a kept drawing used it."""
    charges = bool(set(result['failed']) & {'charge_own_atom', 'overlap_charge', 'charge_open'})
    options = {'repairCharges': charges, 'repairNumbers': True, 'repairArrows': True}
    if HOOK_SEATS['on'] or 'overlap_arrow_arrow' in result['failed']:
        options['hookSeats'] = True
    return options


NEAR_DIRS = (None, 0, 90, 180, 270)     # places tried for a molecule brought next to its arrow's other end


def _moved_into(resolved, other, pid, mover, anchor):
    """A copy of resolved in which the atoms of species mover in panel pid stand where other puts them, relative to
    the atom anchor; the rest keeps what the earlier retries made of it.  None when an atom is missing."""
    r = copy.deepcopy(resolved)
    p = next(q for q in r['panels'] if q['id'] == pid)
    new = {a['ref']: a for q in other['panels'] if q['id'] == pid for a in q['atoms']}
    old = {a['ref']: a for a in p['atoms']}
    if anchor not in new or anchor not in old:
        return None
    dx, dy = old[anchor]['x'] - new[anchor]['x'], old[anchor]['y'] - new[anchor]['y']
    for a in p['atoms']:
        if a['species'] == mover:
            if a['ref'] not in new:
                return None
            a['x'], a['y'] = round(new[a['ref']]['x'] + dx, 4), round(new[a['ref']]['y'] + dy, 4)
            a.pop('native_align', None)
    return r


def retry_route_near(spec, out, fig, result, draw, draw_args, stats):
    """When a curved arrow between two molecules finds no route or touches a bond or another arrow: put the smaller
    molecule (not one placed by hand) next to the arrow's atom in the other one ("near", in the free place nearest to each of NEAR_DIRS, its offset dropped),
    draw these side by side, and keep the one that most lowers the failed checks and findings, gains none in
    HBOND_FLIP_KEEP and brings no two species closer.  The arrow then has a short way."""
    lost = [m.group(1) for x in result['checks']['arrows_ends']['findings'] for m in re.finditer(r'arrow (\S+): no route found', x)]
    lost += [m.group(1) for x in result['checks']['overlap_arrow_bond']['findings'] for m in re.finditer(r'arrow (\S+) touches bond', x)]
    lost += [i for x in result['checks']['overlap_arrow_arrow']['findings'] for m in re.finditer(r'arrows (\S+) and (\S+) touch', x)
             for i in m.groups()]
    if not lost:
        return fig, result, [], spec
    size = collections.Counter(a['species'] for p in fig.resolved['panels'] for a in p['atoms'])
    options = _repair_options(fig, result)
    jobs, moves, specs, pairs = [], [], [], []
    for p in fig.resolved['panels']:
        for a in p.get('curved', []):
            if a.get('id') not in lost:
                continue
            ends = [r for r in a['from']['refs'] + a['to']['refs']]
            sids = list(dict.fromkeys(r.rsplit(':', 1)[0] for r in ends))
            if len(sids) != 2:
                continue
            mover, other = sorted(sids, key=lambda x: size[x])
            sp = spec['species_by_id'].get(mover)
            target = next((r for r in a['to']['refs'] + a['from']['refs'] if r.startswith(other + ':')), None)
            if sp is None or sp['xy'] or target is None:
                continue
            # a molecule that an arrow joins to a third one too (a water between a base and a carbonyl): first next to
            # that third atom, toward this arrow's other end, so that both arrows stay short
            xy = {at['ref']: (at['x'], at['y']) for at in p['atoms']}
            third = [r for c in p.get('curved', []) if c is not a
                     for refs in [c['from']['refs'] + c['to']['refs']] if any(x.startswith(mover + ':') for x in refs)
                     for r in refs if not r.startswith((mover + ':', other + ':')) and r in xy]
            places = [(third[0], round(math.degrees(math.atan2(xy[third[0]][1] - xy[target][1],
                                                               xy[target][0] - xy[third[0]][0])) % 360))
                      ] if third and target in xy else []
            own = next((r for r in a['from']['refs'] + a['to']['refs'] if r.startswith(mover + ':')), None)
            for anchor, direction in places + [(target, d) for d in NEAR_DIRS]:
                changed = copy.deepcopy(spec)
                c = changed['species_by_id'][mover]
                c['near'] = {'species': anchor.rsplit(':', 1)[0].split('.', 1)[1], 'atom': int(anchor.rsplit(':', 1)[1]),
                             'dir': direction, 'dist': None}
                c['offset'] = None
                c['near_retry'] = True      # placed so even where it would keep its place from an earlier panel
                if direction is not None and own is not None:
                    # turned so that its atom of this arrow faces the arrow's other end
                    import layout
                    shape = layout.transform(layout.Layout(changed).shape(mover)[0], 0.0, False, c.get('flip'))
                    m = int(own.rsplit(':', 1)[1])
                    if m in shape and len(shape) > 1:
                        cx, cy = (sum(q[k] for q in shape.values()) / len(shape) for k in (0, 1))
                        face = direction if anchor != target else (direction + 180) % 360
                        c['rotate'] = round((face - layout.ang(shape[m][0] - cx, shape[m][1] - cy)) % 360, 1)
                try:
                    moved = _moved_into(fig.resolved, specutil.resolve(changed), p['id'], mover, anchor)
                except Exception:
                    continue
                if moved is None:
                    continue
                jobs.append((moved, options))
                specs.append(changed)
                moves.append((a['id'], mover, anchor.rsplit(':', 1)[0], direction))
                pairs.append((own, target))
    if not jobs:
        return fig, result, [], spec
    def rank(checked):
        return len(checked['failed']), sum(len(c['findings']) for c in checked['checks'].values())
    def better(checked):
        return (not checked['checks']['stereochemistry']['findings']
                and all(set(checked['checks'][n]['findings']) <= set(result['checks'][n]['findings']) for n in HBOND_FLIP_KEEP)
                and rank(checked) < rank(result) and not _closer(result, checked))
    results = _candidates(jobs, out, fig, draw, draw_args, lambda checked: False, stats, 'retry_route_near')
    def reach(k, candidate):           # among equal ranks and warnings, the shortest way between the arrow's molecules
        own, target = pairs[k]
        try:
            return math.dist(candidate.xy(own), candidate.xy(target))
        except Exception:
            return float('inf')
    def on_h(k, candidate):                      # the molecule put on the side where the other atom writes its H
        own, target = pairs[k]
        try:
            (tx, ty), (ox, oy) = candidate.xy(target), candidate.xy(own)
            hs = [q['box'] for q in candidate.report['pieces'].get(target) or [] if q['text'] == 'H']
        except Exception:
            return 0
        to = math.atan2(oy - ty, ox - tx)
        return int(any(abs((math.atan2((b[1] + b[3]) / 2 - ty, (b[0] + b[2]) / 2 - tx) - to + math.pi) % (2 * math.pi) - math.pi)
                       < math.radians(60) for b in hs))
    scored = [(rank(checked), on_h(k, candidate), sum(len(c.get('warnings', [])) for c in checked['checks'].values()),
               reach(k, candidate), k)
              for k, (candidate, checked, _, error) in enumerate(results) if error is None and better(checked)]
    if not scored:
        return fig, result, [], spec
    k = min(scored)[-1]
    candidate, checked, ok, _ = _candidate(jobs[k][0], out, fig, draw, draw_args, jobs[k][1], better, stats, 'retry_route_near')
    if not ok:
        return fig, result, [], spec
    aid, mover, other, direction = moves[k]
    return candidate, checked, ['arrow %s: %s put next to %s%s' % (
        aid, mover, other, '' if direction is None else ' (%d degrees)' % direction)], specs[k]


def retry_layout(spec, out, fig, result, draw, draw_args, stats):
    """Try bounded structural alternatives on failure, accepting only independently checked drawings."""
    import layout_repair
    notes = []
    protected = ('graph', 'stereochemistry', 'charges', 'hydrogens', 'labels_drawn', 'arrows_count',
                 'arrows_head', 'arrows_color', 'colors', 'cdxml', 'frame', 'bond_crossing')
    def rank(checked):
        return len(checked['failed']), sum(len(c['findings']) for c in checked['checks'].values())
    alternatives = iter(layout_repair.candidates(spec, result, fig))
    rejected, limit = 0, layout_repair.draw_limit(result, fig)
    for attempt in range(1, limit + 1):
        if not _retryable(result):
            break
        try:
            resolved = next(alternatives)
        except StopIteration:
            break
        except Exception as exc:
            notes.append('automatic layout search: %s; kept the checked drawing' % exc)
            break
        options = _repair_options(fig, result)
        if not set(result['failed']).intersection({'arrows_ends', 'arrows_shape'}):
            options['ownMarginPx'] = 8.5
        def better(checked, result=result):
            # a hydrogen bond's crossing line counts with the findings: one that moves elsewhere may still be fewer
            kept = lambda r, n: {x for x in r['checks'][n]['findings'] if not (n == 'bond_crossing' and x.startswith('hbond '))}
            safe = not checked['checks']['stereochemistry']['findings'] and all(
                kept(checked, n) <= kept(result, n) for n in protected)
            return safe and rank(checked) < rank(result) and not _closer(result, checked)
        candidate, checked, ok, error = _candidate(resolved, out, fig, draw, draw_args, options, better, stats, 'retry_layout')
        if error:
            notes.append('automatic layout candidate %d: %s; kept the checked drawing' % (attempt, error))
            continue
        notes.append('automatic layout candidate %d: %s; failed checks %d -> %d, findings %d -> %d; remaining: %s' % (
            attempt, 'accepted' if ok else 'rejected', len(result['failed']), len(checked['failed']),
            rank(result)[1], rank(checked)[1], ', '.join(checked['failed']) or 'none'))
        if ok:
            notes.extend(n for n in resolved.get('notes', []) if n not in fig.resolved.get('notes', []))
            fig, result = candidate, checked
        rejected = 0 if ok else rejected + 1
        if result['ok'] or not layout_repair.relevant(result) or rejected >= limit:
            break
    return fig, result, notes


def _local_arrows(fig, result):
    """The local curved arrows (a bond's electrons moving onto one of its own atoms, or an atom's pair moving into one of
    its own bonds) that come close to another arrow (an arrows_clear warning between two arrows, two arrows touching, or
    a loop), with the local arrows that share an atom with them."""
    near = set()
    for w in result['checks'].get('arrows_clear', {}).get('warnings', []):
        m = re.match(r'^arrow (\S+): -?[\d.]+ pt from arrow (\S+),', w)
        if m:
            near.update(m.groups())
    for x in result['checks'].get('overlap_arrow_arrow', {}).get('findings', []):
        m = re.match(r'^arrows (\S+) and (\S+) touch', x)
        if m:
            near.update(m.groups())
    for x in result['checks'].get('arrows_shape', {}).get('findings', []):
        if 'a loop, not an arc' in x:
            near.add(x.split(':', 1)[0])
    local = {}
    for panel in fig.resolved['panels']:
        for c in panel['curved']:
            a, b = c['from'], c['to']
            if (a['kind'] == 'bond' and b['kind'] == 'atom' and b['refs'][0] in a['refs']) or \
                    (a['kind'] == 'atom' and b['kind'] == 'bond' and a['refs'][0] in b['refs']):
                local[c['id']] = set(a['refs']) | set(b['refs'])
    chosen = {i for i in near if i in local}
    return sorted(chosen | {i for i, atoms in local.items() if any(atoms & local[j] for j in chosen)})


STRETCH_RATIOS = (1.05, 1.1, 1.15, 1.2)    # a local arrow's bond grows only as far as its short hook needs
HOOK_MAX_LEN = 0.85                        # bond lengths: a longer route is a wide arc, not a short hook
HOOK_AIM_DEG = 60                          # a hook's head turned further from its atom's center does not point at it


def _clear_of_arrows(result, ids):
    """No arrows_clear warning, touching arrows or loop names one of the arrows ids."""
    ids = set(ids)
    for w in result['checks'].get('arrows_clear', {}).get('warnings', []):
        m = re.match(r'^arrow (\S+): -?[\d.]+ pt from arrow (\S+),', w)
        if m and ids & set(m.groups()):
            return False
    for x in result['checks'].get('overlap_arrow_arrow', {}).get('findings', []):
        m = re.match(r'^arrows (\S+) and (\S+) touch', x)
        if m and ids & set(m.groups()):
            return False
    return not any('a loop, not an arc' in x and x.split(':', 1)[0] in ids
                   for x in result['checks'].get('arrows_shape', {}).get('findings', []))


def _hooks_fit(fig, ids):
    """Each of the arrows ids is short (at most HOOK_MAX_LEN), and a head at a lettered atom points at that atom (turned
    at most HOOK_AIM_DEG from the direction to its center)."""
    for i in ids:
        if fig.routes.get(i, {}).get('length_bonds', HOOK_MAX_LEN + 1) > HOOK_MAX_LEN:
            return False
        c = next((c for c in fig.curved if c['id'] == i), None)
        r = next((r for r in fig.report['curved'] if r['id'] == i), None)
        if not c or not r or c['to']['kind'] != 'atom' or r['end'] is None or not fig.labeled(c['to']['refs'][0]):
            continue
        u, o = check_rules._head_dir(fig, i, r['end']), fig.xy(c['to']['refs'][0])
        v = (o[0] - r['end'][0], o[1] - r['end'][1])
        n = math.hypot(*v)
        if not u or not n or math.degrees(math.acos(max(-1.0, min(1.0, (u[0] * v[0] + u[1] * v[1]) / n)))) > HOOK_AIM_DEG:
            return False
    return True


def retry_short_hooks(out, fig, result, draw, draw_args, stats):
    """Redraw the local arrows that come close to another arrow as short hooks (adapter option compactArrows), at the
    standard bond length and with their bonds grown to each of STRETCH_RATIOS, side by side.  Keep the first of these
    (the shortest bonds first) whose hooks fit (_hooks_fit) and clear those arrows, else the
    standard-length one with short hooks; either only when no check gains a finding and the findings or the
    arrows_clear warnings fall.  Returns (figure, checks, the adapter options kept
    or {})."""
    ids = _local_arrows(fig, result)
    if not ids:
        return fig, result, {}
    before, warned = _counts(result), len(result['checks'].get('arrows_clear', {}).get('warnings', []))
    def accept(checked):
        after = _counts(checked)
        fewer = len(checked['checks'].get('arrows_clear', {}).get('warnings', []))
        return (all(after.get(n, 0) <= before.get(n, 0) for n in after) and not _closer(result, checked)
                and (sum(after.values()) < sum(before.values()) or fewer < warned))
    options = [{'compactArrows': ids}] + [{'compactArrows': ids, 'stretchArrows': {'ids': ids, 'ratio': r}} for r in STRETCH_RATIOS]
    def short(candidate):
        return _hooks_fit(candidate, ids)
    results = _candidates([(fig.resolved, o) for o in options], out, fig, draw, draw_args,
                          lambda checked: accept(checked) and _clear_of_arrows(checked, ids), stats, 'retry_short_hooks',
                          fit=short)
    for opts, (candidate, checked, ok, _) in zip(options, results):
        if ok:
            return candidate, checked, opts
    first, checked = results[0][0], results[0][1]
    if checked is not None and accept(checked) and short(first):     # no bond length clears them: the standard length
        candidate, checked, ok, _ = _candidate(fig.resolved, out, fig, draw, draw_args, options[0], accept, stats,
                                               'retry_short_hooks')
        if ok:
            return candidate, checked, options[0]
    return fig, result, {}


def _hook_note(short):
    ratio = short.get('stretchArrows', {}).get('ratio')
    return 'short hooks: %s%s' % (', '.join(short['compactArrows']), ' (bonds %.2f standard lengths)' % ratio if ratio else '')

def _run(spec_path, out_dir, png_width=None, octet_dir=None, node_modules=None, browser_cmd=None, positive_control=False,
        draw=None, log=print, dpi=None):
    """The whole pipeline; returns (exit status, summary dict).  draw: the backend function (default octet_draw.draw)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for name in OUTPUTS:                        # the files of an earlier run in DIR
        if (out / name).is_file():
            (out / name).unlink()
    for scratch in list(out.glob('.retry-*')) + list(out.glob('.octet-*')):   # left by a run that was stopped
        shutil.rmtree(scratch, ignore_errors=True)
    times = {}
    stereo_lines = []
    t0 = time.time()
    try:
        spec = specutil.load_spec(spec_path)
        data, _ = specutil._read_json(spec_path)
        resolved = specutil.resolve(spec)
        if 'stereo' in check_rules.figure_features(resolved):
            stereo_lines = stereo.report(specutil.stereo_sources(spec), data)
    except specutil.SpecError as e:
        from spec_validation import inspect_spec
        try:
            data, _ = specutil._read_json(spec_path)
            _, sources = inspect_spec(data)
            stereo_lines = stereo.report(sources, data)
        except specutil.SpecError:
            pass
        _failed(out, 'input error', e.problems, [], times, log, stereo_lines)
        return 2, {'error': str(e)}
    except Exception as e:                      # layout errors (e.g. RDKit missing) are reported, not raised
        _failed(out, 'layout error', '%s: %s' % (type(e).__name__, e), [], times, log, stereo_lines)
        return 2, {'error': str(e)}
    _write_state(out, resolved=resolved)
    times['resolve'] = time.time() - t0
    notes = list(resolved.get('notes', []))     # kept in the state's summary with the result
    HOOK_SEATS['on'] = False
    retry_counts = {stage: {'draws': 0} for stage in
                    ('retry_layout', 'retry_placements', 'retry_forming_branch', 'retry_iupac_pairs', 'retry_hbond_flips',
                     'retry_route_near')}
    retry_counts['retry_iupac_pairs']['accepted'] = []
    preflight = check_rules.run_checks(check_rules.Figure(resolved, {}, {}, None), input_only=True)
    if not preflight['ok']:
        times['total'] = time.time() - t0
        lines = check_rules.summary_lines(preflight) + stereo_lines + ['RESULT: CHECKS FAILED']
        _write_state(out, checks={'result': preflight, 'positive_control': None, 'times_s': times,
                                 'retry_counts': retry_counts}, summary=lines)
        for line in lines:
            log(line)
        return 1, {'ok': False, 'times_s': times, 'failed': preflight['failed'], 'positive_control': None}
    t1 = time.time()
    native_backend = draw is None
    _progress('first drawing')
    try:
        draw = draw or _backend()
        draw(resolved, out, png_width=png_width, octet_dir=octet_dir, node_modules=node_modules, browser_cmd=browser_cmd, dpi=dpi)
    except Exception as e:
        _failed(out, 'drawing failed', '%s: %s' % (type(e).__name__, e), notes, times, log, stereo_lines)
        return 2, {'error': str(e), 'times_s': times}
    times['draw'] = time.time() - t1
    t2 = time.time()
    try:
        fig = check_rules.load_figure(out)
        result = check_rules.run_checks(fig)
        short = {}
        if native_backend:
            retry_counts['retry_short_hooks'] = {'draws': 0}
            fig, result, short = retry_short_hooks(out, fig, result, draw, {
                'png_width': png_width, 'dpi': dpi, 'octet_dir': octet_dir, 'node_modules': node_modules,
                'browser_cmd': browser_cmd}, retry_counts['retry_short_hooks'])
            if short:
                notes.append(_hook_note(short))
                plain = draw         # the later retries keep these arrows short
                def draw(resolved, dest, options=None, **kw):
                    return plain(resolved, dest, options=dict(options or {}, **short), **kw)
        if native_backend and any(n in result['failed'] for n in HBOND_FLIP_CHECKS):
            # before the other retries: a finding at a hydrogen bond is not theirs to fix
            fig, result, retried = retry_hbond_flips(out, fig, result, draw, {
                'png_width': png_width, 'dpi': dpi, 'octet_dir': octet_dir, 'node_modules': node_modules,
                'browser_cmd': browser_cmd}, retry_counts['retry_hbond_flips'])
            notes.extend(retried)
        if native_backend and _retryable(result):
            _progress('fixing automatically: %s' % ', '.join(result['failed']), always=True)
            retry_started = time.time()
            retry_args = {'png_width': png_width, 'dpi': dpi, 'octet_dir': octet_dir,
                          'node_modules': node_modules, 'browser_cmd': browser_cmd}
            numbers_only = set(result['failed']) <= {'number_place', 'number_clear'}
            try:
                # Resolve saved arrow/number placement before trying molecular motions.
                fig, result, retried = retry_placements(resolved, out, fig, result, draw, retry_args, retry_counts['retry_placements'])
                notes.extend(retried)
                placed = fig
                # an edit of the input (a molecule put next to its arrow's other end) comes before the layout search,
                # which then starts from the edited input
                if (any('no route found' in x for x in result['checks']['arrows_ends']['findings'])
                        or 'overlap_arrow_bond' in result['failed'] or 'overlap_arrow_arrow' in result['failed']):
                    fig, result, retried, spec = retry_route_near(spec, out, fig, result, draw, retry_args,
                                                                  retry_counts['retry_route_near'])
                    notes.extend(retried)
                fig, result, retried = retry_layout(spec, out, fig, result, draw, retry_args, retry_counts['retry_layout'])
                notes.extend(retried)
                if _retryable(result):
                    fig, result, retried = retry_forming_branch(out, fig, result, draw, retry_args, retry_counts['retry_forming_branch'])
                    notes.extend(retried)
                fig, result, retried = retry_iupac_pairs(out, fig, result, draw, retry_args, retry_counts['retry_iupac_pairs'])
                notes.extend(retried)
                if not numbers_only or fig is not placed:
                    fig, result, retried = retry_placements(fig.resolved, out, fig, result, draw, retry_args, retry_counts['retry_placements'])
                    notes.extend(retried)
            except Exception as exc:     # a fault in a retry keeps the last accepted figure and its findings
                notes.append('automatic retry stopped: %s: %s' % (type(exc).__name__, exc))
            resolved = fig.resolved
            times['placement_retry'] = time.time() - retry_started
        if native_backend and not short:     # arrows that the retries brought close
            fig, result, short = retry_short_hooks(out, fig, result, draw, {
                'png_width': png_width, 'dpi': dpi, 'octet_dir': octet_dir, 'node_modules': node_modules,
                'browser_cmd': browser_cmd}, retry_counts['retry_short_hooks'])
            if short:
                notes.append(_hook_note(short))
                resolved = fig.resolved
        pc = check_rules.positive_control(fig, checked=result['checks']) if positive_control else None
    except check_rules.InputError as e:
        _failed(out, 'checks could not run', str(e), notes, times, log, stereo_lines)
        return 2, {'error': str(e), 'times_s': times}
    times['check'] = time.time() - t2 - times.get('placement_retry', 0)
    times['total'] = time.time() - t0
    backend_times = fig.report.get('timings_s') or {}
    ok = result['ok'] and (pc is None or pc['ok'])
    lines = check_rules.summary_lines(result, pc)
    lines.extend(stereo_lines)
    lines.append('stages (s): ' + ', '.join('%s %.1f' % (k, v) for k, v in times.items()))
    if backend_times:
        lines.append('backend (s): ' + ', '.join('%s %.1f' % (k, float(v)) for k, v in backend_times.items()
                                                 if isinstance(v, (int, float)) and not isinstance(v, bool)))
    raw = {}
    try:
        raw = check_rules.load_state(out)['report']
    except (check_rules.InputError, KeyError):
        pass
    for p in raw.get('problems') or []:
        lines.append('backend problem: %s' % p)
    for n in raw.get('notes') or []:
        lines.append('backend note: %s' % n)
    for n in notes:
        lines.append('note: %s' % n)
    if (out / 'figure.svg').exists():
        lines.append(size_line(out))
    files = sorted(p.name for p in out.iterdir() if p.is_file() and not p.name.startswith('.'))
    lines.append('files: ' + ', '.join(files))
    lines.append('RESULT: %s' % ('all checks pass' if ok else 'CHECKS FAILED'))
    audit = check_rules.audit_lines(fig.features)
    _write_state(out, checks={'result': result, 'positive_control': pc, 'times_s': times,
                             'backend_times_s': backend_times, 'files': files, 'retry_counts': retry_counts},
                 summary=lines + audit)
    for line in lines + (audit if ok else []):       # on a pass the audit items are the agent's next step
        if not (ok and line.startswith('WARNING:') and not line.startswith('WARNING: arrows_clear:')):
            log(line)
    return (0 if ok else 1), {'ok': ok, 'times_s': times, 'failed': result['failed'],
                              'positive_control': None if pc is None else '%d/%d' % (pc['detected'], pc['planted'])}


def check_environment(browser_cmd=None, octet_dir=None, node_modules=None, log=print):
    problems = []
    log('Python: ' + platform.python_version())
    if sys.version_info < (3, 9):
        problems.append('Python: install Python 3.9 or newer and run this script with it')
    try:
        import rdkit
        from rdkit.Chem import rdCIPLabeler  # noqa: F401
        log('RDKit: ' + rdkit.__version__)
    except ImportError:
        problems.append('RDKit: install in this Python: python -m pip install rdkit')
    try:
        import octet_draw
        result = octet_draw.check_environment(browser_cmd, octet_dir, node_modules)
        if result.get('browser'):
            log('Browser: ' + result['browser'] + ' (tiny page OK)')
        font = result.get('font_check')
        if font:
            log('Font widths: %s ratio %.4f (%s)' % (font['font'], font['ratio'], 'OK' if font.get('ok') else 'FAILED'))
        problems.extend(result.get('problems', []))
        if not result.get('ok') and not result.get('problems'):
            problems.append('browser: probe failed; install Playwright, Chromium and Arial or Liberation Sans in the browser environment')
    except Exception as exc:
        problems.append('drawing backend: %s' % exc)
    for problem in problems:
        log('MISSING: ' + problem)
    log('ENVIRONMENT OK' if not problems else 'ENVIRONMENT FAILED')
    return 2 if problems else 0


def run_versions(specs, out, options, log=print):
    """Draw several versions of one input at once, each in out/<name>/, print each result and name the best one:
    a passing version with the fewest WARNING lines, else the one with the fewest FIX lines."""
    import subprocess
    names = [p.stem for p in specs]
    if len(set(names)) < len(names):
        log('input error: give the versions different file names')
        return 2
    out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, REACTION_DRAW_WORKERS=str(max(1, (os.cpu_count() or 1) // len(specs))))
    jobs = [subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(spec), '--out', str(out / name)] + options,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            for spec, name in zip(specs, names)]
    pool = concurrent.futures.ThreadPoolExecutor(len(jobs))
    try:                                           # read the pipes while waiting, and stop the drawings if this run is stopped
        outputs = [pool.submit(job.communicate) for job in jobs]
        _progress('drawing %d versions' % len(jobs))
        while not all(f.done() for f in outputs):
            time.sleep(2)
            _progress('drawing %d versions' % len(jobs))
    finally:
        for job in jobs:
            if job.poll() is None:
                job.terminate()
        pool.shutdown()
    results = []
    for spec, name, job, output in zip(specs, names, jobs, outputs):
        lines, err = (part.splitlines() for part in output.result())
        if not any(x.startswith(('RESULT', 'input error', 'layout error', 'drawing failed', 'checks could not')) for x in lines):
            lines += [x for x in err if not x.startswith('progress')][-5:]   # a crash shows its last lines
        audit = [x for x in lines if x.startswith(('AUDIT', '- [ ]'))]
        lines = [x for x in lines if x not in audit]
        rank = ((0, sum(x.startswith('WARNING:') for x in lines)) if job.returncode == 0 else
                (1, sum(x.startswith('FIX ') for x in lines)) if job.returncode == 1 else (2, 0))
        results.append((rank, spec, name, job.returncode, lines, audit))
    for _, spec, _, _, lines, _ in results:
        log('== %s' % spec.name)
        for line in lines:
            log(line)
    rank, spec, name, code, _, audit = min(results, key=lambda r: r[0])
    log('BEST: %s (%s; figure in %s)' % (spec.name, 'all checks pass' if code == 0 else 'CHECKS FAILED' if code == 1 else 'no figure',
                                         out / name))
    for line in audit:
        log(line)
    return code


def main(argv=None):
    ap = argparse.ArgumentParser(description=(__doc__ or '').split('\n\n')[0])
    ap.add_argument('spec', type=Path, nargs='*', metavar='INPUT.json', help='one input, or several versions of it to draw at once')
    ap.add_argument('--out', type=Path, help='output directory (full run only)')
    ap.add_argument('--check-env', action='store_true', help='probe Python, RDKit, browser and font widths; no input file needed')
    ap.add_argument('--dpi', type=float, help='figure.png resolution at the true size (default: the input\'s drawing.dpi, else 600)')
    ap.add_argument('--png-width', type=int, help='figure.png width in px instead of --dpi')
    ap.add_argument('--octet', help='build the drawing page from this Octet Draw checkout (needs Node.js and esbuild; '
                                    'default: the prebuilt page in octet/dist)')
    ap.add_argument('--node-modules', help='node_modules holding esbuild, with --octet')
    ap.add_argument('--browser-cmd', help='command that runs the browser part elsewhere, e.g. "ssh HOST /path/to/python"')
    ap.add_argument('--positive-control', action='store_true', help='also run the checker\'s positive control (the regression test does)')
    ap.add_argument('--quiet', action='store_true', help='print the result, stereo notes and audit')
    args = ap.parse_args(argv)

    def interrupted(signum, frame):
        print('progress: stopped by a signal; DIR/.state.json has no final summary, so its figure files are not checked', file=sys.stderr, flush=True)
        sys.exit(143)                              # unwinds the scratch folders and the browser job
    signal.signal(signal.SIGTERM, interrupted)
    if args.check_env:
        return check_environment(args.browser_cmd, args.octet, args.node_modules)
    if not args.spec:
        ap.error('INPUT.json is required unless --check-env is used')
    if args.out is None:
        ap.error('--out DIR is required for a full run')

    def log(line):
        if (line.lstrip().startswith(('RESULT', 'FIX ', 'STEREO', 'AUDIT', '- [ ]', 'POSITIVE CONTROL', 'NEXT:', 'WARNING:',
                                     'input error', 'layout error',
                                     'drawing failed', 'checks could not', 'MISSED planted'))
                and (not args.quiet or not line.lstrip().startswith(('FIX ', 'POSITIVE CONTROL')))):
            print(line, flush=True)
    if len(args.spec) > 1:
        options = [x for flag, value in (('--dpi', args.dpi), ('--png-width', args.png_width), ('--octet', args.octet),
                                         ('--node-modules', args.node_modules), ('--browser-cmd', args.browser_cmd))
                   if value is not None for x in (flag, str(value))]
        options += ['--positive-control'] * args.positive_control + ['--quiet'] * args.quiet
        return run_versions(args.spec, args.out, options, log=print)
    status, _ = run(args.spec[0], args.out, args.png_width, args.octet, args.node_modules, args.browser_cmd,
                    positive_control=args.positive_control, log=log, dpi=args.dpi)
    return status


if __name__ == '__main__':
    sys.exit(main())
