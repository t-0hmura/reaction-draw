#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Check a drawn figure against the drawing rules and against the figure it was drawn from.

  python check_rules.py OUTDIR [--json OUT.json] [--quiet]

OUTDIR is what draw_figure.py writes: .state.json (resolved layout, backend measurements, saved data and checks)
and figure.svg. No browser is
needed.  Exit status 0 when every check passes, 1 when a check fails, 2 on bad input.

Each check is a detector (DETECTORS below, described in the repository's docs/checks.md); only the detectors that the
figure's features make relevant run.

The report and reload formats are documented in the repository's docs/backend.md (the backend writes them). Coordinates
of report are points in the frame of the SVG's user units; resolved is in bond lengths (y down).
"""
import argparse
import collections
import copy
import itertools
import json
import math
import re
import statistics
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import svg_geometry as co  # noqa: E402
import stereochemistry as stereo  # noqa: E402
sys.path.insert(1, str(Path(__file__).resolve().parent / "octet"))
from octet_draw import delocalized_ring, ring_side  # noqa: E402  (the backend's ring choice for 1.5 bonds)

ANGLE_TOL = 10.0               # degrees from 120 (chains) and from the four-bond layout
LENGTH_TOL_PT = 0.3            # a standard bond may differ this much from style.bond_length_pt
STRETCH_MAX = 1.2              # a bond a short local curved arrow sits on may grow to this many standard lengths
LINE_SHOW, LINE_DOT = 0.6, 0.25  # two-label bonds (bond lengths): line shown after lengthening; below LINE_DOT a line reads as a dot (adapter.ts)
CIRCLE_TOL_PT = 0.15           # outer diameter of a charge circle against the default
CIRCLE_RATIO = 0.34            # Octet: circle radius (center line) = 0.34 x label size
CHARGE_GAP_MAX_PT = 3.0        # a circle this close (or closer) to its own symbol letter is next to it
CHARGE_CLEAR_PT = 0.5          # a circle keeps this much from other ink, arrows and other atoms' text
CIRCLE_OWN_OVERLAP_PT = 0.5    # a circle may reach this far into its own letter's glyph box (the box's corners are
                               # empty for round letters such as O); deeper, it covers the letter
NUMBER_CLEAR_PT = 1.2          # an atom number keeps this much white from bonds, arrows, circles and other texts (glyph ink
                               # to ink, measured exactly here; place numbers with a little more room)
NUMBER_OWN_MARGIN_PT = 1.5     # the nearest other atom / number / circle is at least this much farther than the own atom
NUMBER_LETTER_CLEAR_PT = 3.0   # no other atom's letters closer than this to a number (not between two labels)
SIZE_TOL_PT = 0.05
ARC_RATIO_MAX = 2.0            # a curved arrow's length / the distance between its ends (half circle 1.57; 220 degrees 2.0)
COLUMN_ANGLE_TOL = 2.0         # degrees from vertical for reaction arrows joining panels in one column
FONT_TOL = 0.01                # text widths of the drawing font against Arial's (measured: Liberation Sans 1.000, the
                               # fallback sans-serif font of a machine without it 1.07)
CAP = 0.716                    # cap height of Arial per em
LONE_PAIR_TAIL_MAX_PT = 2.0     # tail to the midpoint of its drawn pair, including the visible white gap

DETECTORS = ('overlap_text_text', 'overlap_text_bond', 'overlap_arrow_text', 'overlap_arrow_bond', 'overlap_arrow_arrow',
             'overlap_charge', 'graph', 'stereochemistry', 'charges', 'hydrogens', 'labels_drawn', 'arrows_count', 'arrows_ends', 'arrows_lone_pair', 'arrows_head', 'arrows_color',
             'arrows_shape', 'column_arrows', 'chain_angles', 'four_bond', 'charge_circled', 'charge_own_atom', 'charge_size', 'ion_text',
             'number_size', 'number_place', 'number_clear', 'number_on_h', 'label_bonds', 'one_label_bonds', 'bond_lengths',
             'colors', 'cdxml', 'frame', 'partial_dashes', 'delocalized_bonds', 'ts_brackets',
             'charge_open', 'lone_pair_geometry', 'ring_substituents', 'carbon_labels', 'stereo_display', 'hbond_dots',
             'radical_marks', 'electron_flow', 'isotopes', 'hydrogen_display', 'coordination', 'metal_geometry', 'residue_codes', 'residue_once', 'reaction_kinds', 'radical_direction', 'figure_style', 'species_gap', 'bond_crossing', 'plus_place',
             'arrows_steps', 'arrows_clear')
# A declaration is an OR of features; undeclared extensions run always.
DETECTOR_FEATURES = {
    'overlap_text_text': ('always',), 'overlap_text_bond': ('bonds',),
    'overlap_arrow_text': ('arrows',), 'overlap_arrow_bond': ('arrows',), 'overlap_arrow_arrow': ('arrows',),
    'overlap_charge': ('charges',), 'graph': ('always',), 'stereochemistry': ('stereo',),
    'charges': ('charges',), 'hydrogens': ('always',), 'labels_drawn': ('always',),
    'arrows_count': ('arrows',), 'arrows_ends': ('curved',), 'arrows_lone_pair': ('lone_pairs',),
    'arrows_head': ('arrows',), 'arrows_color': ('arrows',), 'arrows_shape': ('curved',),
    'column_arrows': ('multi_panel',), 'chain_angles': ('bonds',), 'four_bond': ('four_bond',),
    'charge_circled': ('charges',), 'charge_own_atom': ('charges',), 'charge_size': ('charges',), 'ion_text': ('ions',),
    'number_size': ('numbers',), 'number_place': ('numbers',), 'number_clear': ('numbers',), 'number_on_h': ('numbers',),
    'label_bonds': ('bonds',), 'one_label_bonds': ('bonds',), 'bond_lengths': ('bonds',),
    'colors': ('color',), 'cdxml': ('always',), 'frame': ('always',),
    'partial_dashes': ('partial',), 'delocalized_bonds': ('delocalized',), 'ts_brackets': ('ts',),
    'charge_open': ('charges',), 'lone_pair_geometry': ('lone_pairs',), 'ring_substituents': ('rings',),
    'carbon_labels': ('carbon',), 'stereo_display': ('stereo',), 'hbond_dots': ('hbonds',),
    'radical_marks': ('radicals',), 'electron_flow': ('curved',), 'isotopes': ('isotopes',),
    'hydrogen_display': ('explicit_h',), 'coordination': ('metals',), 'metal_geometry': ('metals',),
    'residue_codes': ('residues',), 'residue_once': ('residues', 'residue_texts'), 'reaction_kinds': ('reaction_arrows',),
    'radical_direction': ('radicals',), 'figure_style': ('always',), 'species_gap': ('species_pairs',), 'bond_crossing': ('bonds',), 'plus_place': ('plus',),
    'arrows_steps': ('reaction_arrows',), 'arrows_clear': ('curved',),
}


def figure_features(resolved):
    """Derive applicability from resolved chemistry and requested display, never from a filename."""
    from iupac_features import METALS
    features = {'always'}
    panels = resolved.get('panels', [])
    atoms = [a for p in panels for a in p['atoms']]
    bonds = [b for p in panels for b in p['bonds']]
    curved = [a for p in panels for a in p.get('curved', [])]
    degrees = collections.Counter(r for b in bonds for r in (b['a'], b['b']))
    flags = {
        'bonds': bool(bonds), 'carbon': any(a['el'] == 'C' and a.get('kind', 'atom') == 'atom' for a in atoms),
        'four_bond': any(n >= 4 for n in degrees.values()), 'charges': any(a.get('charge') for a in atoms),
        'ions': any(a.get('ion_text') for a in atoms), 'numbers': any(a.get('number') for a in atoms),
        'radicals': any(a.get('radical') for a in atoms), 'isotopes': any(a.get('isotope') for a in atoms),
        'explicit_h': any(a['el'] == 'H' for a in atoms), 'residues': any(a.get('kind') == 'residue' for a in atoms),
        'residue_texts': any(RESIDUE_NAME.search(t.get('text') or '') for p in panels for t in p.get('texts', [])),
        'ts': any(a.get('ts') for a in atoms), 'partial': any(b.get('style') == 'partial' for b in bonds),
        'delocalized': any(b.get('style') == '1.5' for b in bonds),
        'hbonds': any(p.get('hbonds') or p.get('pseudobonds') for p in panels),
        'lone_pairs': any(p.get('lone_pairs') for p in panels),
        'curved': bool(curved), 'reaction_arrows': bool(resolved.get('reaction_arrows')),
        'arrows': bool(curved or resolved.get('reaction_arrows')), 'multi_panel': len(panels) > 1,
        'species_pairs': any(len({a['species'] for a in p['atoms']}) > 1 for p in panels),
        'plus': any(t.get('text') == '+' for p in panels for t in p.get('texts', [])),
        'color': bool(resolved.get('colors')) or any(a.get('color') for a in atoms) or any(b.get('color') for b in bonds),
        'metals': any(a['el'] in METALS or a.get('oxidation') is not None or a.get('center') for a in atoms)
                  or any(b.get('coord') or b.get('style') == 'coord' for b in bonds)
                  or any(p.get('metal_geometry') or p.get('eta') or p.get('polyhedron') for p in panels),
    }
    # Stereo display includes unspecified potential centers and explicitly wavy double bonds.
    flags['stereo'] = any(b.get('style') in ('wedge', 'hash', 'wavy') for b in bonds)
    for sid, smiles in (resolved.get('stereo_sources') or {}).items():
        relaxed = any(a.get('ts') for a in atoms if a.get('species') == sid) or flags['metals']
        labels = stereo.expected(sid, smiles, relaxed=relaxed)[0]
        if any(labels) or stereo.potential_centers(sid, smiles, relaxed=relaxed):
            flags['stereo'] = True
    # A graph cycle is sufficient for ring-substituent applicability.
    parent = {}
    def root(a):
        parent.setdefault(a, a)
        if parent[a] != a:
            parent[a] = root(parent[a])
        return parent[a]
    flags['rings'] = False
    for b in bonds:
        x, y = root(b['a']), root(b['b'])
        if x == y:
            flags['rings'] = True
        else:
            parent[x] = y
    features.update(k for k, present in flags.items() if present)
    return features


def applicable_detectors(f):
    features = getattr(f, 'features', None)
    if features is None:
        features = figure_features(f.resolved)
        f.features = features
    return [name for name in DETECTORS if features.intersection(DETECTOR_FEATURES.get(name, ('always',)))]


def audit_lines(features):
    """Return the applicable human audit, keeping the checklist's wording."""
    rows = ['AUDIT (check by eye):']
    for line in (Path(__file__).resolve().parent.parent / 'references' / 'audit-checklist.md').read_text(encoding='utf-8').splitlines():
        if not line.startswith('- [ ] '):
            continue
        match = re.search(r' \[features: ([^]]+)\]$', line)
        tags = match.group(1).split(',') if match else ['always']
        if features.intersection(tags):
            rows.append(line[:match.start()] if match else line)
    return rows


def warning_finding(name, message):
    if name in ('chain_angles', 'ring_substituents', 'radical_direction', 'species_gap', 'plus_place', 'arrows_clear'):
        return True
    if name == 'four_bond':
        return 'axis-aligned four-bond stereocenter' not in message
    if name == 'overlap_arrow_bond':
        return ' at its own atom ' in message
    if name == 'number_place':
        return 'margin under' in message or 'between two labels' in message
    if name == 'number_clear':
        return 'overlaps' not in message
    if name == 'arrows_lone_pair':
        return 'tail is not at a drawn lone pair' in message
    if name == 'lone_pair_geometry':
        return 'does not face the target' in message or 'not above/below/left/right' in message
    if name == 'stereo_display':
        return any(s in message for s in ('vertical opposite', 'not adjacent at 60', 'three or more stereobonds', 'two same-type wedges',
                                         'ring-fusion stereobond', 'move the stereobond away'))
    if name in ('label_bonds', 'one_label_bonds', 'bond_lengths'):
        return 'pt long' in message
    if name == 'hbond_dots':
        return 'twice the label height' in message
    if name == 'metal_geometry':
        return 'angles do not match' in message and 'linear' not in message
    if name == 'electron_flow':
        return any(s in message for s in ('homolysis needs', 'a new bond needs', 'more than two electrons leave'))
    if name == 'radical_marks':
        return any(s in message for s in ('before or inside', 'radical-ion charge must follow'))
    return False


SINGLE_KINDS = ('single', 'reaction')    # a "single" reaction arrow of the resolved figure is Octet's "reaction" arrow


class InputError(Exception):
    """Missing or unreadable input (exit status 2)."""


# ------------------------------------------------------------------------------------------ small geometry
def box_gap(a, b):
    """Signed gap between two boxes (x1, y1, x2, y2): > 0 apart, < 0 overlapping (depth of the smaller overlap)."""
    dx = max(a[0] - b[2], b[0] - a[2])
    dy = max(a[1] - b[3], b[1] - a[3])
    if dx >= 0 or dy >= 0:
        return math.hypot(max(dx, 0.0), max(dy, 0.0))
    return max(dx, dy)


def pt_box_gap(p, b):
    dx = max(b[0] - p[0], 0.0, p[0] - b[2])
    dy = max(b[1] - p[1], 0.0, p[1] - b[3])
    if dx == 0 and dy == 0:
        return -min(p[0] - b[0], b[2] - p[0], p[1] - b[1], b[3] - p[1])
    return math.hypot(dx, dy)


def box_shape(b, desc=''):
    pts = [(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])]
    return co.Shape('glyph', 'poly', pts, True, True, 0.0, desc, quad=True)


def disk_shape(c, r, desc=''):
    pts = [(c[0] + r * math.cos(2 * math.pi * k / 32), c[1] + r * math.sin(2 * math.pi * k / 32)) for k in range(32)]
    return co.Shape('mark', 'poly', pts, True, True, 0.0, desc)


def shape_clear(a, b, overlap_only=False):
    # Shape.bbox includes stroke half-widths; keep another 1 pt for contact checks.
    # Distance-based checks keep the original cutoff and exact nearby clearances.
    if co.bbox_gap(a.bbox, b.bbox) > (1.0 if overlap_only else 30):
        return 99.0
    return co.clearance(a, b)


def _union_bbox(shapes):
    if not shapes:
        return None
    return (min(s.bbox[0] for s in shapes), min(s.bbox[1] for s in shapes), max(s.bbox[2] for s in shapes), max(s.bbox[3] for s in shapes))


def _far(box, b, limit):
    """True when the bbox of every shape inside box (a union of their bboxes) is more than limit from bbox b: then
    co.bbox_gap is above limit for each of them too."""
    return box is not None and max(box[0] - b[2], b[0] - box[2], box[1] - b[3], b[1] - box[3]) > limit


SLACK = 1e-6    # pt added to the limit of the array prefilters below, so that rounding never drops a box an exact test keeps


def _boxes(items):
    """An (n, 4) array of boxes (x1, y1, x2, y2): the bboxes of shapes, or boxes as they are."""
    return np.array([getattr(x, 'bbox', x) for x in items], dtype=float).reshape(-1, 4)


def _near_pairs(a, b, limit):
    """For each row of the (n, 4) box array a, the indices (ascending) of the rows of b that may lie within limit of it:
    neither axis gap is above limit (plus SLACK).  Two boxes, and anything inside them (shapes and their clearance),
    are never nearer than either axis gap, so every box that a distance test keeps at limit is among these: a loop that
    runs its exact test over them finds what it finds over all of b, in the same order, and the many far boxes cost
    one array pass instead of one call each."""
    out = [[] for _ in range(len(a))]
    if len(a) and len(b):
        dx = np.maximum(b[None, :, 0] - a[:, None, 2], a[:, None, 0] - b[None, :, 2])
        dy = np.maximum(b[None, :, 1] - a[:, None, 3], a[:, None, 1] - b[None, :, 3])
        rows, cols = np.nonzero(~((dx > limit + SLACK) | (dy > limit + SLACK)))   # a NaN is kept for the exact test
        for i, j in zip(rows.tolist(), cols.tolist()):
            out[i].append(j)
    return out


def _near(boxes, b, limit):
    """_near_pairs for one box b (a point is the box (x, y, x, y))."""
    return _near_pairs(np.array([b], dtype=float), boxes, limit)[0]


def _seg_box(p, q):
    """Box of the segment p-q."""
    return (min(p[0], q[0]), min(p[1], q[1]), max(p[0], q[0]), max(p[1], q[1]))


def _center(b):
    return ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0)


def _min_clear(a, shapes, limit, overlap_only=True):
    """min(shape_clear(a, s, overlap_only) for s in shapes) where that is limit or less: a piece more than limit from a
    along x or y keeps more than limit from it, and 99 stands in for it.  Like min(), it fails on no shapes."""
    return min([shape_clear(a, s, overlap_only) for s in shapes if not _far(s.bbox, a.bbox, limit + SLACK)]
               or [99.0 for _ in shapes[:1]])


def angle_of(p, q):
    return math.degrees(math.atan2(-(q[1] - p[1]), q[0] - p[0])) % 360.0


def adiff(a, b):
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def norm_color(c):
    if c is None:
        return None
    c = str(c).strip().lower()
    m = re.match(r'^rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)$', c)
    if m:
        c = '#%02x%02x%02x' % tuple(int(x) for x in m.groups())
    if c in ('', 'black', '#000', '#000000', 'none'):
        return None
    return c


def _num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _box(v):
    if v is None:
        return None
    if len(v) != 4 or any(_num(x) is None for x in v):
        return None
    x1, y1, x2, y2 = (float(x) for x in v)
    return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))


def _pt(v):
    if v is None:
        return None
    return (float(v[0]), float(v[1]))


def _lost_count(v):
    """Octet's "lost" (what a reader could not keep), {kind: count} -> a count."""
    return sum(int(x) if isinstance(x, (int, float)) else 1 for x in (v or {}).values())


def _refs(v):
    """What an arrow end reads as, {'kind', 'refs': [...]} -> tuple of refs (sorted for bonds)."""
    if v is None:
        return None
    v = list(v['refs'])
    return tuple(sorted(v)) if len(v) == 2 else tuple(v)


# ------------------------------------------------------------------------------------------ inputs
class Figure(object):
    """Everything the detectors read, in points (report frame) where geometry is involved."""

    def __init__(self, resolved, report, reload, svg_text, name='figure'):
        if resolved.get('format') != 'resolved/1':
            raise InputError('the resolved figure must have "format": "resolved/1"')
        self.name = name
        self.resolved = resolved
        self.style = resolved['style']
        self.bond_pt = float(self.style.get('bond_length_pt', 14.4))
        self.pair_tail_max = LONE_PAIR_TAIL_MAX_PT * self.bond_pt / 14.4   # the tail's distance from its pair grows with the bond length
        self.size = float(self.style.get('font_size_pt', 10.0))      # label size
        self.atoms = {}
        self.bonds = []
        self.curved = []
        self.exceptions = {}
        self.hbonds, self.lone_pairs = [], []
        for p in resolved['panels']:
            for a in p['atoms']:
                self.atoms[a['ref']] = a
            for b in p['bonds']:
                self.bonds.append(b)
            for c in p['curved']:
                self.curved.append(c)
            self.exceptions.update(p.get('angle_exceptions') or {})
            self.hbonds += [tuple(h) for h in p.get('hbonds') or []]
            self.lone_pairs += list(p.get('lone_pairs') or [])
        self.nbrs = {r: [] for r in self.atoms}
        for b in self.bonds:
            self.nbrs[b['a']].append(b['b'])
            self.nbrs[b['b']].append(b['a'])
        self.features = figure_features(resolved)
        reported = {c['id'] for c in (report or {}).get('curved', [])}
        self.unrouted = {m[1] for problem in (report or {}).get('problems', [])
                         if (m := re.fullmatch(r'arrow (\S+): no route found', problem)) and m[1] not in reported}
        self.report = self._norm_report(report or {})
        self.routes = {r.get('id'): r for r in (report or {}).get('routes', [])}   # the router's own measures, by arrow
        self.reload = self._norm_reload(reload or {})
        self.svg_text = svg_text
        self._scene = None

    # ---- normalization of the backend's files (the Octet backend's report.json and reload.json, docs/backend.md)
    def _norm_report(self, rep):
        out = {'atoms': {}, 'bonds': [], 'curved': [], 'numbers': [], 'charges': [], 'lone_pairs': [], 'texts': [], 'pieces': {},
               'timings_s': rep.get('timings_s') or {}, 'font': rep.get('font_check')}
        for ref, a in (rep.get('atoms') or {}).items():
            out['atoms'][ref] = {'xy': (float(a['x_pt']), float(a['y_pt'])), 'box': _box(a.get('label_box_pt'))}
        out['radicals'] = copy.deepcopy(rep.get('radicals') or [])
        for b in rep.get('bonds') or []:
            if b.get('hidden_h'):
                continue
            if b.get('hbond') or b.get('order') == 0:          # hydrogen bonds are listed with the bonds, order 0
                continue
            out['bonds'].append({'key': tuple(sorted((b['a'], b['b']))), 'a': b['a'], 'b': b['b'],
                                 'length': _num(b.get('length_pt')), 'visible': _num(b.get('visible_line_pt')),
                                 'lengthened': bool(b.get('lengthened')),
                                 'stretched_for': b.get('stretched_for'), 'stretch_ratio': _num(b.get('stretch_ratio'))})
        for c in rep.get('curved') or []:
            out['curved'].append({'id': c.get('id'), 'start': _pt(c.get('start_pt')), 'end': _pt(c.get('end_pt')),
                                  'start_ref': _refs(c.get('start_ref')), 'end_ref': _refs(c.get('end_ref')),
                                  'length': _num(c.get('length_pt'))})
        for n in rep.get('numbers') or []:
            out['numbers'].append({'ref': n.get('ref'), 'text': n.get('text'), 'box': _box(n.get('box_pt')),
                                   'size': _num(n.get('size_pt'))})
        for pair in rep.get('lone_pairs') or []:
            out['lone_pairs'].append({'ref': pair.get('ref'), 'center': _pt(pair.get('center_pt')),
                                      'radius': _num(pair.get('radius_pt'), .09 * self.size), 'dots': [_pt(p) for p in pair.get('dots_pt', [])]})
        for c in rep.get('charges') or []:
            out['charges'].append({'ref': c.get('ref'), 'kind': c.get('kind'), 'center': _pt(c.get('center_pt')),
                                   'outer_d': _num(c.get('outer_d_pt'))})
        for t in rep.get('texts') or []:
            if t.get('role') == 'number':                        # numbers are read from "numbers"
                continue
            out['texts'].append({'text': t.get('text'), 'box': _box(t.get('box_pt')), 'role': t.get('role'), 'ref': t.get('ref')})
        for p in rep.get('label_pieces') or []:                  # glyph ink of each piece of an atom label
            box = _box(p.get('box_pt'))
            if p.get('ref') and box:
                out['pieces'].setdefault(p['ref'], []).append({'text': p.get('text') or '', 'box': box, 'size': _num(p.get('size_pt'))})
        return out

    def _norm_reload(self, rl, nested=False):
        out = {'atoms': {}, 'bonds': {}, 'arrows': [], 'numbers': [], 'captions': [], 'present': bool(rl),
               'lost': _lost_count(rl.get('lost')), 'lost_raw': rl.get('lost') or None, 'cdxml': None,
               'boxes': copy.deepcopy(rl.get('boxes') or [])}
        out['hbonds'] = copy.deepcopy(rl.get('hbonds') or [])
        out['delocalized'] = copy.deepcopy(rl.get('delocalized') or [])
        for a in rl.get('atoms') or []:
            h = a.get('h')
            out['atoms'][a['ref']] = {'el': a.get('el'), 'label': a.get('text'),
                                 'charge': int(a.get('charge') or 0), 'circled': a.get('circled'), 'h': None if h is None else int(h),
                                 'color': norm_color(a.get('color')), 'kind': a.get('kind'),
                                 'shown': a.get('shown'), 'x': a.get('x'), 'y': a.get('y'), 'pairs': int(a.get('pairs') or 0),
                                 **{key: a.get(key) for key in ('isotope', 'radical', 'hidden_h', 'center', 'oxidation')}}
        for b in rl.get('bonds') or []:
            out['bonds'][tuple(sorted((b['a'], b['b'])))] = {'order': _num(b.get('order')), 'style': b.get('style', 'plain'),
                                                         'color': norm_color(b.get('color')),
                                                         'first': b['a']}   # the narrow end of a wedge or hash
        # arrows: Octet's reload lists "curved" (kind curved | fishhook; heads "start" and "end") and "straight"
        for a in rl.get('curved') or []:
            okind = a.get('kind') or 'curved'
            out['arrows'].append({'kind': 'curved', 'okind': okind, 'id': a.get('id'),
                                  'head': a.get('end') or ('half' if okind == 'fishhook' else 'full'),
                                  'tail': a.get('start') or 'none', 'color': norm_color(a.get('color', '#000000'))})
        for a in rl.get('straight') or []:
            out['arrows'].append({'kind': a.get('kind') or 'reaction', 'okind': a.get('kind'), 'id': a.get('id'),
                                  'segments': copy.deepcopy(a.get('segments') or []),
                                  'head': a.get('head') or 'full', 'tail': 'none',
                                  'coords': tuple(_num(a.get(k)) for k in ('x1', 'y1', 'x2', 'y2')),
                                  'color': norm_color(a.get('color', '#000000'))})
        for n in rl.get('numbers') or []:
            out['numbers'].append({'ref': n.get('ref'), 'text': n.get('text'), 'size': _num(n.get('size'))})
        for c in rl.get('captions') or []:
            out['captions'].append({'text': c.get('text'), 'role': c.get('role'), 'size': _num(c.get('size')),
                                    'color': norm_color(c.get('color')), 'x': c.get('x'), 'y': c.get('y')})
        if not nested and isinstance(rl.get('cdxml_only'), dict):
            out['cdxml'] = self._norm_reload(rl['cdxml_only'], nested=True)
        return out

    # ---- geometry in points
    def xy(self, ref):
        a = self.report['atoms'].get(ref)
        if a:
            return a['xy']
        r = self.atoms[ref]
        return (r['x'] * self.bond_pt, r['y'] * self.bond_pt)

    def label_box(self, ref):
        a = self.report['atoms'].get(ref)
        return a['box'] if a else None

    def label_parts(self, ref):
        """Glyph-ink boxes of an atom's label: its pieces (C, H, 3 of CH3) when the report lists them, else the label box."""
        pieces = self.report['pieces'].get(ref)
        if pieces:
            return [p['box'] for p in pieces]
        box = self.label_box(ref)
        return [box] if box else []

    def gap_to_atom(self, box, ref):
        """Gap from a box to an atom as a reader sees it: to the nearest glyph of its label, or to its point."""
        parts = self.label_parts(ref) if self.labeled(ref) else []
        if parts:
            return min(box_gap(box, b) for b in parts)
        return pt_box_gap(self.xy(ref), box)

    def symbol_box(self, ref):
        """Box of the element symbol letter(s) of a labeled atom, or None for a skeletal atom: the glyph ink of the
        label piece that is the symbol (the report's label_pieces), else an estimate centered on the atom."""
        a = self.atoms[ref]
        if not a.get('label') or not self.label_box(ref):
            return None
        x, y = self.xy(ref)
        pieces = self.report['pieces'].get(ref) or []
        cands = [p for p in pieces if p['text'] == a['el']] if a.get('kind', 'atom') == 'atom' else pieces
        if cands:
            return min(cands, key=lambda p: pt_box_gap((x, y), p['box']))['box']
        sym = a['el'] if a.get('kind', 'atom') == 'atom' else a['label'][:1]
        w = sum(_ARIAL.get(ch, 600) for ch in sym) / 1000.0 * self.size
        h = CAP * self.size
        return (x - w / 2, y - h / 2, x + w / 2, y + h / 2)

    def labeled(self, ref):
        return bool(self.atoms[ref].get('label'))

    def scene(self):
        if self._scene is None:
            self._scene = read_svg_ink(self.svg_text) if self.svg_text else {'bonds': [], 'arrows': [], 'marks': []}
            self._assign_ink()
        return self._scene

    def _assign_ink(self):
        """Give every ink polygon an owner, including measured radical dots."""
        sc = self._scene
        circles = [(c['ref'], c['center'], (c['outer_d'] or 7.4) / 2.0) for c in self.report['charges'] if c['kind'] == 'circle' and c['center']]
        segs = [((b['a'], b['b']), self.xy(b['a']), self.xy(b['b'])) for b in self.bonds if b['a'] in self.atoms and b['b'] in self.atoms]
        hsegs = [(h, self.xy(h[0]), self.xy(h[1])) for h in self.hbonds if h[0] in self.atoms and h[1] in self.atoms]
        ink = sc['bonds'] + sc['marks']
        centers = [(sum(p[0] for p in s.pts) / len(s.pts), sum(p[1] for p in s.pts) / len(s.pts)) for s in ink]
        points = _boxes([(cx, cy, cx, cy) for cx, cy in centers])
        # the center lines a center may lie near (within the limit of the box around each line), measured exactly below
        near_bonds = _near_pairs(points, _boxes([_seg_box(p, q) for _, p, q in segs]), 0.3 * self.bond_pt)
        near_hbonds = _near_pairs(points, _boxes([_seg_box(p, q) for _, p, q in hsegs]), 0.15 * self.bond_pt)
        for s, (cx, cy), bond_rows, hbond_rows in zip(ink, centers, near_bonds, near_hbonds):
            s.owner = None
            if s.cls == 'mark':
                for mark in self.report['radicals']:
                    if mark.get('center_pt') and math.dist((cx, cy), mark['center_pt']) <= mark['radius_pt'] + 0.6:
                        s.owner = ('radical', mark['ref'])
                        break
            if s.owner:
                continue
            for ref, c, r in circles:
                if math.hypot(cx - c[0], cy - c[1]) <= r + 0.6:
                    s.owner = ('charge', ref)
                    break
            if s.owner:
                continue
            best = None
            for key, p, q in (segs[i] for i in bond_rows):
                d = co.dist_pt_seg(cx, cy, p[0], p[1], q[0], q[1])
                if d < 0.3 * self.bond_pt and (best is None or d < best[0]):
                    best = (d, ('bond', key))
            for key, p, q in (hsegs[i] for i in hbond_rows):
                d = co.dist_pt_seg(cx, cy, p[0], p[1], q[0], q[1])
                if d < 0.15 * self.bond_pt and (best is None or d < best[0]):
                    best = (d, ('hbond', key))
            if best:
                s.owner = best[1]
                continue
            for ref in self.lone_pairs:
                if ref in self.atoms and math.hypot(cx - self.xy(ref)[0], cy - self.xy(ref)[1]) < 0.9 * self.bond_pt:
                    s.owner = ('lone_pair', ref)
                    break

    def texts(self):
        """[(owner, kind, text, box)]: atom labels by glyph piece (owner = atom ref), numbers (owner = atom ref, kind
        'number'), other texts."""
        out = []
        for ref, a in self.report['atoms'].items():
            if a['box'] and ref in self.atoms:
                pieces = self.report['pieces'].get(ref)
                if pieces:
                    out += [(ref, 'label', p['text'], p['box']) for p in pieces]
                else:
                    out.append((ref, 'label', self.atoms[ref].get('label') or '', a['box']))
        for n in self.report['numbers']:
            if n['box']:
                out.append((n['ref'], 'number', n['text'], n['box']))
        residues = self.residue_atoms()
        residue = {label for _, label in residues}
        for t in self.report['texts']:
            if not t['box']:
                continue
            owner = t.get('ref')
            if owner is None and (t.get('role') == 'residue' or t['text'] in residue):
                owner = self.residue_atom(t, residues)
            if owner is not None and t.get('role') == 'residue' and any(o == owner and k == 'label' for o, k, _, _ in out):
                continue                                # a residue name the report gives both as a label and as a text
            out.append((owner, 'text', t['text'], t['box']))
        return out

    def residue_atoms(self):
        """[(ref, label)] of the residue atoms."""
        return [(r, a['label']) for r, a in self.atoms.items() if a.get('kind') == 'residue']

    def residue_atom(self, t, residues=None):
        """The atom a residue name (a text of the report) belongs to: the nearest residue atom of that name, or None.
        residues: residue_atoms(), when the caller has it."""
        cands = [r for r, label in (self.residue_atoms() if residues is None else residues) if label == t['text']]
        return min(cands, key=lambda r: pt_box_gap(self.xy(r), t['box'])) if cands else None


_ARIAL = {'C': 722, 'H': 722, 'N': 722, 'O': 778, 'S': 667, 'P': 667, 'F': 611, 'I': 278, 'B': 667, 'K': 667, 'M': 833,
          'g': 556, 'l': 222, 'a': 556, 'r': 333, 'e': 556, 'i': 222, 'n': 556, 'u': 556, 'E': 667, 'A': 667, 'd': 556, 'o': 556}


def read_svg_ink(svg_text):
    """Bond ink, marks and arrows of the backend's SVG, in points: {'bonds': [Shape], 'marks': [Shape], 'arrows':
    [(id, [Shape])]}.  Every drawn path carries data-role (bond | mark | arrow); texts are read from the report."""
    try:
        root = ET.fromstring(svg_text.encode('utf-8'))
    except ET.ParseError as e:
        raise InputError('cannot parse the SVG: %s' % e)
    vb = co.nums(root.get('viewBox'))
    w_pt = co.length_pt(root.get('width'))
    k = (w_pt / vb[2]) if (len(vb) == 4 and vb[2] and w_pt) else 0.75
    out = {'bonds': [], 'marks': []}
    arrows = {}

    def walk(el, scale):
        for ch in el:
            tag = co.local(ch.tag)
            if tag == 'g':
                walk(ch, scale * co.group_scale(ch.get('transform')))
            if tag != 'path':
                continue
            role, shapes = ch.get('data-role'), []
            for pts, closed in co.parse_path(ch.get('d')):
                q = [(x * scale * k, y * scale * k) for x, y in pts]
                if closed and len(q) > 2:
                    shapes.append(co.Shape(role, 'poly', q, True, True, 0.0, ''))
                else:
                    shapes.append(co.Shape(role, 'line', q, False, False, 0.3, ''))
            if role == 'arrow':
                arrows.setdefault(ch.get('data-id'), []).extend(shapes)
            else:
                out['marks' if role == 'mark' else 'bonds'].extend(shapes)
    walk(root, 1.0)
    out['arrows'] = list(arrows.items())
    return out


# ------------------------------------------------------------------------------------------ detectors
TEXT_APART_BONDS = 0.6      # least white space between two names (texts, residue names), in bond lengths; the layout
                            # keeps 0.8 (layout.TEXT_APART)


def det_overlap_text_text(f):
    items = f.texts()
    out = []
    # two names close together read as naming one molecule
    # (a free text and any other name, or residue names of two molecules; residue names of one molecule each sit at
    # the end of their own bond)
    names = [t for t in f.report['texts'] if t['box'] and t.get('role') in ('residue', 'text') and t['text'] != '+']
    residues = f.residue_atoms()
    owners = [f.residue_atom(t, residues) if t.get('role') == 'residue' else None for t in names]   # a residue name's atom
    seen = set()
    for (a, oa), (b, ob) in itertools.combinations(zip(names, owners), 2):
        g = box_gap(a['box'], b['box'])
        if oa and ob and oa.rsplit(':', 1)[0] == ob.rsplit(':', 1)[0]:
            continue
        if a['text'] != b['text'] and 0 <= g < TEXT_APART_BONDS * f.bond_pt and (a['text'], b['text']) not in seen:
            seen.add((a['text'], b['text']))          # once per pair of names, as in its first panel
            out.append('names %r%s and %r%s stand %.1f pt apart, closer than %.1f pt: which name labels which group '
                       'is unclear' % (a['text'], ' (%s)' % oa if oa else '', b['text'], ' (%s)' % ob if ob else '', g,
                                       TEXT_APART_BONDS * f.bond_pt))
    boxes = _boxes([box for _, _, _, box in items])
    for i, cands in enumerate(_near_pairs(boxes, boxes, 0.0)):      # boxes that overlap (box_gap < 0) are among these
        for j in (j for j in cands if j > i):
            (oa, ka, ta, ba), (ob, kb, tb, bb) = items[i], items[j]
            if oa is not None and oa == ob and ka == kb == 'label':
                continue
            g = box_gap(ba, bb)
            if g < 0:
                out.append('%s %r%s and %s %r%s overlap by %.2f pt' % (
                    ka, ta, ' (' + oa + ')' if oa else '', kb, tb, ' (' + ob + ')' if ob else '', -g))
    return out


def _own_ink(owner, s):
    """Ink that may approach its own atom label, including its radical dots."""
    if owner is None or s.owner is None:
        return False
    kind, key = s.owner
    if kind == 'bond':
        return owner in key
    if kind in ('charge', 'lone_pair', 'radical'):
        return key == owner
    if kind == 'hbond':
        return owner in key
    return False


def det_overlap_text_bond(f):
    sc = f.scene()
    ink = sc['bonds'] + sc['marks']
    texts = [(owner, kind, text, box_shape(box)) for owner, kind, text, box in f.texts()
             if kind != 'number']                       # numbers: number_clear (stricter)
    out = []
    # a clearance of 0 or less needs overlapping bboxes
    for (owner, kind, text, tb), cands in zip(texts, _near_pairs(_boxes([t[3] for t in texts]), _boxes(ink), 0.0)):
        for s in (ink[i] for i in cands):
            if _own_ink(owner, s):
                continue
            c = shape_clear(tb, s)
            if c <= 0.0:
                out.append('%s %r%s touches %s (%.2f pt)' % (kind, text, ' (' + owner + ')' if owner else '', _ink_name(s), c))
    return out


def _ink_name(s):
    if s.owner is None:
        return 'ink near (%.1f, %.1f)' % (s.pts[0][0], s.pts[0][1])
    kind, key = s.owner
    return '%s %s' % (kind, '-'.join(key) if isinstance(key, tuple) else key)


def det_overlap_arrow_text(f):
    sc = f.scene()
    out = []
    texts = [(kind, text, box_shape(box)) for _, kind, text, box in f.texts()]
    text_boxes = _boxes([tb for _, _, tb in texts])
    for aid, shapes in sc['arrows']:
        box = _union_bbox(shapes)
        for kind, text, tb in (texts[i] for i in _near(text_boxes, box, 1.0)) if box else texts:
            if _far(box, tb.bbox, 1.0):
                continue
            c = min(shape_clear(s, tb, overlap_only=True) for s in shapes)
            if c <= 0.0:
                out.append('arrow %s touches %s %r (%.2f pt)' % (aid, kind, text, c))
    return out


def _arrow_hbond(f, aid):
    """The two atoms a curved arrow joins when it runs from one atom to another atom or to the gap between it and
    another (a proton taken across a hydrogen bond): a hydrogen bond between those same two atoms meets the arrow at
    both its ends, so their contact cannot be avoided.  None for other arrows."""
    arrow = next((c for c in f.curved if c['id'] == aid), None)
    if arrow is None or arrow['from'].get('kind') != 'atom' or arrow['to'].get('kind') not in ('atom', 'gap'):
        return None
    atoms = frozenset(arrow['from']['refs']) | frozenset(arrow['to']['refs'])
    return atoms if len(atoms) == 2 else None


def _arrow_pivot(f, aid):
    """The atom a local curved arrow turns about (a bond to one of its own atoms, an atom's pair into one of its own bonds,
    or a bond into a bond sharing an atom with it); None for other arrows."""
    arrow = next((c for c in f.curved if c['id'] == aid), None)
    if arrow is None:
        return None
    a, b = set(arrow['from'].get('refs') or []), set(arrow['to'].get('refs') or [])
    shared = a & b
    return next(iter(shared)) if len(shared) == 1 and max(len(a), len(b)) == 2 else None


def det_overlap_arrow_bond(f):
    sc = f.scene()
    ink = sc['bonds'] + sc['marks']
    ink_boxes = _boxes(ink)
    out = []
    for aid, shapes in sc['arrows']:
        box = _union_bbox(shapes)
        joined = _arrow_hbond(f, aid)
        pivot = _arrow_pivot(f, aid)
        # a clearance of 0 or less needs overlapping bboxes: only those pieces and ink are measured (99 for the rest)
        for s in (ink[i] for i in _near(ink_boxes, box, 0.0)) if box else ink:
            if joined and s.owner and s.owner[0] == 'hbond' and frozenset(s.owner[1]) == joined:
                continue                            # the hydrogen bond the arrow runs along, between its own two ends
            c = min([shape_clear(a, s, overlap_only=True) for a in shapes if not _far(a.bbox, s.bbox, SLACK)]
                    or [99.0 for _ in shapes[:1]])
            if c <= 0.0:
                if pivot and s.owner and s.owner[0] == 'bond' and pivot in s.owner[1]:
                    # a local arrow keeps its concave side to its atom; when another bond of that atom lies in the way,
                    # no route on that side clears it (a warning)
                    out.append('arrow %s crosses %s at its own atom %s (%.2f pt)' % (aid, _ink_name(s), pivot, c))
                else:
                    out.append('arrow %s touches %s (%.2f pt)' % (aid, _ink_name(s), c))
    return out


def det_overlap_arrow_arrow(f):
    arrows = f.scene()['arrows']
    out = []
    boxes = [_union_bbox(shapes) for _, shapes in arrows]
    for i in range(len(arrows)):
        for j in range(i + 1, len(arrows)):
            if boxes[j] is not None and _far(boxes[i], boxes[j], 1.0):
                continue
            c = min(shape_clear(a, b, overlap_only=True) for a in arrows[i][1] for b in arrows[j][1])
            if c <= 0.0:
                out.append('arrows %s and %s touch (%.2f pt)' % (arrows[i][0], arrows[j][0], c))
    return out


def _circles(f):
    return [c for c in f.report['charges'] if c['kind'] == 'circle' and c['center'] and c['outer_d']]


def det_overlap_charge(f):
    sc, texts = f.scene(), f.texts()
    ink = sc['bonds'] + sc['marks']
    ink_boxes, text_boxes = _boxes(ink), _boxes([box_shape(box) for _, _, _, box in texts])
    circles = _circles(f)
    out = []
    # only ink, arrows and texts within CHARGE_CLEAR_PT of a circle's bbox can be closer than that
    for c in circles:
        disk = disk_shape(c['center'], c['outer_d'] / 2.0)
        for s in (ink[i] for i in _near(ink_boxes, disk.bbox, CHARGE_CLEAR_PT)):
            if s.owner == ('charge', c['ref']):
                continue
            g = shape_clear(disk, s, overlap_only=True)
            if g < CHARGE_CLEAR_PT:
                out.append('circle of %s is %.2f pt from %s' % (c['ref'], g, _ink_name(s)))
        for aid, shapes in sc['arrows']:
            g = _min_clear(disk, shapes, CHARGE_CLEAR_PT)
            if g < CHARGE_CLEAR_PT:
                out.append('circle of %s is %.2f pt from arrow %s' % (c['ref'], g, aid))
        for owner, kind, text, box in (texts[i] for i in _near(text_boxes, disk.bbox, CHARGE_CLEAR_PT)):
            if owner == c['ref'] and kind == 'label':
                g = shape_clear(disk, box_shape(box), overlap_only=True)
                if g < -CIRCLE_OWN_OVERLAP_PT:
                    out.append('circle of %s overlaps its own label by %.2f pt' % (c['ref'], -g))
                continue
            g = shape_clear(disk, box_shape(box), overlap_only=True)
            if g < CHARGE_CLEAR_PT:
                out.append('circle of %s is %.2f pt from %s %r' % (c['ref'], g, kind, text))
        for d in circles:
            if d is not c and d['ref'] > c['ref']:
                g = math.hypot(c['center'][0] - d['center'][0], c['center'][1] - d['center'][1]) - (c['outer_d'] + d['outer_d']) / 2
                if g < CHARGE_CLEAR_PT:
                    out.append('circles of %s and %s are %.2f pt apart' % (c['ref'], d['ref'], g))
    return out


def det_graph(f):
    rl = f.reload
    if not rl['present']:
        return ['reload.json is missing: the saved file was not read back']
    out = []
    want, have = set(f.atoms), set(rl['atoms'])
    if want - have:
        out.append('atoms missing after reload: %s' % sorted(want - have)[:12])
    if have - want:
        out.append('atoms after reload that the figure does not have: %s' % sorted(have - want)[:12])
    for ref in sorted(want & have):
        a, g = f.atoms[ref], rl['atoms'][ref]
        if g['el'] is not None and g['el'] != a['el']:
            out.append('%s: element %s after reload, %s asked' % (ref, g['el'], a['el']))
        if a.get('kind') == 'abbrev' and g['label'] != a['label']:
            out.append('%s: label %r after reload, %r asked' % (ref, g['label'], a['label']))
        if a.get('ts') and g['label'] and g['label'] != a.get('label'):
            out.append('%s: TS label %r after reload, %r asked' % (ref, g['label'], a.get('label')))
    out += _residue_names(f, rl, 'after reload')
    for ref, a in sorted(f.atoms.items()):
        if a.get('kind') == 'residue':
            small = [p['text'] for p in f.report['pieces'].get(ref, []) if p['size'] is not None and p['size'] < 0.9 * f.size]
            if small:
                out.append('residue %s (%s) is drawn as an atom label: %s became subscript; a residue name is plain text '
                           'beside its root' % (a['label'], ref, ', '.join(small)))
    wb = {tuple(sorted((b['a'], b['b']))): b for b in f.bonds}
    if set(wb) - set(rl['bonds']):
        out.append('bonds missing after reload: %s' % sorted(set(wb) - set(rl['bonds']))[:8])
    if set(rl['bonds']) - set(wb):
        out.append('bonds after reload that the figure does not have: %s' % sorted(set(rl['bonds']) - set(wb))[:8])
    for key in sorted(set(wb) & set(rl['bonds'])):
        o = rl['bonds'][key]['order']
        if not order_ok(2 if wb[key].get('style') == '1.5' else wb[key]['order'], o):
            out.append('bond %s-%s: order %s after reload, %s asked' % (key[0], key[1], o, wb[key]['order']))
    out += _kekule_doubles(wb, rl['bonds'], 'after reload')
    out += _style_issues(wb, rl['bonds'], 'after reload')
    return out


def det_stereochemistry(f):
    out = []
    for when, rl in (('after .oct reload', f.reload), ('CDXML alone', f.reload.get('cdxml'))):
        if rl is None or not rl['present']:
            out.append(when + ': no saved structure to check stereochemistry')
            continue
        bonds = [dict(b, a=b['first'], b=next(ref for ref in pair if ref != b['first']))
                 for pair, b in rl['bonds'].items()]
        out += stereo.compare(f.resolved, rl['atoms'], bonds, when)
    return out


def _style_issues(wb, got, when):
    """Wedge, hash and bold bonds as asked (none = plain); a wedge or hash also with its narrow end at the asked atom
    (the resolved bond's "a"; the reloaded bond's first atom)."""
    out = []
    for key in sorted(set(wb) & set(got)):
        b, g = wb[key], got[key]
        want, have = b.get('style') or 'plain', g.get('style') or 'plain'
        want = 'dashed' if want in ('partial', '1.5') else 'plain' if want == 'coord' else want
        if want == 'plain' and have == 'dashed' and float(b['order']) == 1.5:
            continue                                    # an aromatic bond drawn without a Kekule structure (dashed line)
        if have != want:
            out.append('bond %s-%s: %s %s, %s asked' % (key[0], key[1], have, when, want))
        elif want in ('wedge', 'hash') and g.get('first') is not None and g['first'] != b['a']:
            out.append('bond %s-%s: %s with its narrow end at %s %s, at %s asked' % (key[0], key[1], want, g['first'], when, b['a']))
    return out


def order_ok(asked, got):
    """A bond order read back against the one asked.  Order 1.5 (aromatic) is drawn as a Kekule structure (1 or 2;
    Octet draws its own delocalized type as one plain line), so 1, 1.5 and 2 are all accepted for it."""
    if got is None:
        return True
    if float(asked) == 1.5:
        return float(got) in (1.0, 1.5, 2.0)
    return abs(float(got) - float(asked)) <= 1e-6


def _kekule_doubles(wb, got, when):
    """Bonds asked as 1.5 and drawn as 1 or 2: no atom may get two double bonds among them."""
    doubles = collections.Counter()
    for key, b in wb.items():
        g = got.get(key)
        if float(b['order']) == 1.5 and b.get('style') != '1.5' and g and g['order'] is not None and float(g['order']) == 2.0:
            doubles.update(key)
    return ['%s: atom %s has %d double bonds among its aromatic (1.5) bonds %s' % ('order 1.5', r, n, when)
            for r, n in sorted(doubles.items()) if n > 1]


def _residue_names(f, rl, when):
    """Every residue name asked for is in the reloaded file: as a residue caption or as its root atom's text."""
    want = collections.Counter(a['label'] for a in f.atoms.values() if a.get('kind') == 'residue')
    if not want:
        return []
    have = collections.Counter(c['text'] for c in rl['captions'] if c['role'] == 'residue')
    have.update(g['label'] for r, g in rl['atoms'].items() if g['label'] and f.atoms.get(r, {}).get('kind') == 'residue')
    return ['residue name %r: %d %s, %d asked' % (k, have.get(k, 0), when, n) for k, n in sorted(want.items()) if have.get(k, 0) < n]


def det_charges(f):
    out = []
    for ref, a in f.atoms.items():
        g = f.reload['atoms'].get(ref)
        if g is not None and g['charge'] != int(a.get('charge') or 0):
            out.append('%s: charge %+d after reload, %+d asked' % (ref, g['charge'], int(a.get('charge') or 0)))
    return out


def det_hydrogens(f):
    out = []
    for ref, a in f.atoms.items():
        g = f.reload['atoms'].get(ref)
        if g is None or g['h'] is None or a.get('kind', 'atom') != 'atom':
            continue
        if g['h'] != int(a.get('h') or 0):
            out.append('%s: %d implicit H after reload, %d asked' % (ref, g['h'], int(a.get('h') or 0)))
    return out


def det_labels_drawn(f):
    out = []
    for ref, a in f.atoms.items():
        if not a.get('label') or a.get('kind', 'atom') == 'residue':      # a residue's name is a caption beside its root
            continue
        if not f.label_box(ref):
            out.append('%s: label %r is not drawn (the drawing has no label box for it)' % (ref, a['label']))
            continue
        g = f.reload['atoms'].get(ref)
        if g is not None and g.get('shown') is False and a.get('el') == 'C' and a.get('kind', 'atom') == 'atom':
            out.append('%s: carbon %r is not shown in the saved file (Octet hides carbon symbols unless shown)' % (ref, a['label']))
    return out


def _arrow_counts(f, rl, when):
    out = []
    curved = [a for a in rl['arrows'] if a['kind'] == 'curved']
    straight = [a for a in rl['arrows'] if a['kind'] != 'curved']
    expected = sum(c['id'] not in f.unrouted for c in f.curved)
    if len(curved) != expected:
        out.append('%d curved arrows %s, %d asked' % (len(curved), when, expected))
    want = [r.get('kind') or 'single' for r in f.resolved['reaction_arrows']]
    if len(straight) != len(want):
        out.append('%d reaction arrows %s, %d asked' % (len(straight), when, len(want)))
    else:
        kind = lambda k: 'single' if k in SINGLE_KINDS else k
        got = sorted(kind(a['kind']) for a in straight)
        if got != sorted(kind(k) for k in want):
            out.append('reaction arrows %s: %s, asked %s' % (when, ', '.join(got), ', '.join(sorted(kind(k) for k in want))))
    return out


def det_arrows_count(f):
    out = _arrow_counts(f, f.reload, 'after reload')
    expected = sum(c['id'] not in f.unrouted for c in f.curved)
    if len(f.report['curved']) != expected:
        out.append('the report lists %d curved arrows, %d asked' % (len(f.report['curved']), expected))
    return out


def det_arrows_shape(f):
    """A curved arrow is an arc from its start to its end, not a loop around other parts of the drawing."""
    out = []
    for c in f.report['curved']:
        if c.get('length') is None or c['start'] is None or c['end'] is None:
            continue
        chord = math.hypot(c['end'][0] - c['start'][0], c['end'][1] - c['start'][1])
        if c['length'] > ARC_RATIO_MAX * max(chord, 0.5):
            out.append('%s: %.1f pt of arrow for %.1f pt between its ends (%.1f times; at most %.1f): a loop, not an arc'
                       % (c['id'], c['length'], chord, c['length'] / max(chord, 0.5), ARC_RATIO_MAX))
    return out


def det_column_arrows(f):
    out = []
    saved = [('after reload', f.reload), ('CDXML alone', f.reload.get('cdxml'))]
    for i, r in enumerate(f.resolved['reaction_arrows']):
        if not r.get('column_panels'):
            continue
        samples = [('layout', tuple(r[k] for k in ('x1', 'y1', 'x2', 'y2')))]
        for when, rl in saved:
            if rl is None:
                continue
            straight = [a for a in rl['arrows'] if a['kind'] != 'curved']
            coords = straight[i].get('coords') if i < len(straight) else None
            if not coords or any(v is None for v in coords):
                out.append('reaction arrow %d %s: missing column-arrow endpoints' % (i + 1, when))
            else:
                samples.append((when, coords))
        for when, (x1, y1, x2, y2) in samples:
            deviation = math.degrees(math.atan2(abs(x2 - x1), abs(y2 - y1)))
            if y2 == y1 or deviation > COLUMN_ANGLE_TOL:
                out.append('reaction arrow %d (%s) %s: %.2f degrees from vertical (at most %.1f)'
                           % (i + 1, ' -> '.join(r['column_panels']), when, deviation, COLUMN_ANGLE_TOL))
    return out


HEAD_AIM_DEG = 90     # an arrow head that stops HEAD_AIM_REACH_PT or more from the symbol it ends at points within
HEAD_AIM_REACH_PT = 3.5   # this of the symbol's middle (a nearer head may arrive from the side)
HEAD_REACH_PT = 5.0   # an arrow head ends within this of the symbol of the atom it points at
BARE_HEAD_REACH = 0.25   # bond lengths: an arrow head ends within this of a bare vertex it points at


def _own_symbol_box(f, ref):
    """Ink box of the atom's own element symbol in its label (the O of OH), else the label box; None for a bare vertex."""
    el = f.atoms[ref].get('el')
    own = [p['box'] for p in f.report['pieces'].get(ref, []) if p['text'] == el and p.get('box')]
    if own:
        return min(own, key=lambda b: pt_box_gap(f.xy(ref), b))
    return f.label_box(ref) if f.labeled(ref) else None


def _reach(f, pt, ref):
    """Gap in pt from an arrow end to the symbol of an atom (to the vertex of an unlabeled one)."""
    box = _own_symbol_box(f, ref)
    return pt_box_gap(pt, box) if box else math.dist(pt, f.xy(ref))


def _receiver(c):
    """The atom that receives the electron pair of a bond-forming arrow: the atom of its gap that is not in its source."""
    if c['electrons'] != 2 or c['to']['kind'] != 'gap':
        return None
    left = [r for r in c['to']['refs'] if r not in c['from']['refs']]
    return left[0] if len(left) == 1 else None


def _nearest_end(f, p, kind):
    if kind == 'atom':
        best = min((r for r in f.atoms if not f.atoms[r].get('hidden_h')), key=lambda r: math.hypot(p[0] - f.xy(r)[0], p[1] - f.xy(r)[1]))
        return (best,)
    best, bd = None, None
    for b in f.bonds:
        pa, pb = f.xy(b['a']), f.xy(b['b'])
        d = co.dist_pt_seg(p[0], p[1], pa[0], pa[1], pb[0], pb[1])
        if bd is None or d < bd:
            best, bd = tuple(sorted((b['a'], b['b']))), d
    return best


def _bond_reading(f, pt):
    """The drawn bond whose middle part (0.2 to 0.8 of its length) is nearest to a point, and that distance."""
    best, bd = None, float('inf')
    for b in f.bonds:
        if f.atoms[b['a']].get('hidden_h') or f.atoms[b['b']].get('hidden_h'):
            continue
        pa, pb = f.xy(b['a']), f.xy(b['b'])
        dx, dy = pb[0] - pa[0], pb[1] - pa[1]
        t = ((pt[0] - pa[0]) * dx + (pt[1] - pa[1]) * dy) / (dx * dx + dy * dy or 1)
        if 0.2 < t < 0.8:
            d = co.dist_pt_seg(pt[0], pt[1], pa[0], pa[1], pb[0], pb[1])
            if d < bd:
                best, bd = (b['a'], b['b']), d
    return best, bd


def _head_dir(f, aid, tip):
    """Unit vector along which the head of curved arrow aid points: from the middle of its other corners to its tip."""
    for k, shapes in f.scene()['arrows']:
        for s in shapes if k == aid else ():
            if s.closed and len(s.pts) >= 3 and any(math.dist(q, tip) < 0.1 for q in s.pts):
                rest = [q for q in s.pts if math.dist(q, tip) >= 0.1]
                dx, dy = tip[0] - sum(q[0] for q in rest) / len(rest), tip[1] - sum(q[1] for q in rest) / len(rest)
                n = math.hypot(dx, dy)
                return (dx / n, dy / n) if n else None
    return None


def _ray_box(p, u, box):
    """Distance along the ray p + s*u (s >= 0) to where it enters box, or None when it misses."""
    lo, hi = 0.0, float('inf')
    for k in (0, 1):
        if abs(u[k]) < 1e-12:
            if not box[k] <= p[k] <= box[k + 2]:
                return None
        else:
            t1, t2 = (box[k] - p[k]) / u[k], (box[k + 2] - p[k]) / u[k]
            lo, hi = max(lo, min(t1, t2)), min(hi, max(t1, t2))
    return lo if lo <= hi else None


def _ray_seg(p, u, a, b):
    """Distance along the ray p + s*u (s >= 0) to segment a-b, or None when it misses."""
    ex, ey = b[0] - a[0], b[1] - a[1]
    den = u[0] * ey - u[1] * ex
    if abs(den) < 1e-12:
        return None
    s = ((a[0] - p[0]) * ey - (a[1] - p[1]) * ex) / den
    w = ((a[0] - p[0]) * u[1] - (a[1] - p[1]) * u[0]) / den
    return s if s >= 0 and 0 <= w <= 1 else None


def _drawn_segment(f, a, b):
    """The drawn part of bond a-b: its center line less the label margin around a labeled end."""
    pa, pb = f.xy(a), f.xy(b)
    margin = float(f.style['label_margin_pt'])

    def start(r, p, q):
        parts, t = f.label_parts(r) if f.labeled(r) else [], 0.0
        while parts and t < 1.0 and min(pt_box_gap((p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t), x) for x in parts) <= margin:
            t += 0.01
        return t
    s, e = start(a, pa, pb), 1.0 - start(b, pb, pa)
    return (pa[0] + (pb[0] - pa[0]) * s, pa[1] + (pb[1] - pa[1]) * s), (pa[0] + (pb[0] - pa[0]) * e, pa[1] + (pb[1] - pa[1]) * e)


def _reads_as_bond(f, c, pt, ref):
    """A finding when the head of arrow c, aimed at atom ref, is nearer to the middle of a bond than to that atom's symbol,
    unless the head points at the symbol: carried on, it meets the symbol before the drawn bond line (the Zn of Zn-OH
    reached from above, beside its 2+)."""
    bond, d = _bond_reading(f, pt)
    reach = _reach(f, pt, ref)
    if bond and d < reach:
        u, box = _head_dir(f, c['id'], pt), _own_symbol_box(f, ref) if f.labeled(ref) else None
        if u and box:
            sb, sl = _ray_box(pt, u, box), _ray_seg(pt, u, *_drawn_segment(f, bond[0], bond[1]))
            if sb is not None and (sl is None or sb < sl):
                return None
        return '%s: head is %.1f pt from bond %s-%s and %.1f pt from %s: it reads as pointing at the bond' % (c['id'], d, bond[0], bond[1], reach, ref)
    # a head beside the symbol whose line, carried on, misses the symbol and meets one of the atom's own bonds within a
    # bond length reads as pointing at that bond (4-OT: the N-H -> N arrow of Pro1 aimed at the ring N-C bond)
    u, box = _head_dir(f, c['id'], pt), _own_symbol_box(f, ref) if f.labeled(ref) else None
    if u and box and _ray_box(pt, u, box) is None:
        # a head that stops short of the symbol and points away from it reaches no atom
        cx, cy = (box[0] + box[2]) / 2 - pt[0], (box[1] + box[3]) / 2 - pt[1]
        if reach > HEAD_AIM_REACH_PT and math.hypot(cx, cy) > 1e-9 \
                and (u[0] * cx + u[1] * cy) / math.hypot(cx, cy) < math.cos(math.radians(HEAD_AIM_DEG)):
            return '%s: head points %.0f degrees away from the symbol of %s' % (
                c['id'], math.degrees(math.acos(max(-1.0, min(1.0, (u[0] * cx + u[1] * cy) / math.hypot(cx, cy))))), ref)
        for b in f.bonds:
            if ref in (b['a'], b['b']) and not (f.atoms[b['a']].get('hidden_h') or f.atoms[b['b']].get('hidden_h')):
                sl = _ray_seg(pt, u, *_drawn_segment(f, b['a'], b['b']))
                if sl is not None and sl <= f.bond_pt:
                    return '%s: head points at bond %s-%s (%.1f pt ahead), not at the symbol of %s' % (c['id'], b['a'], b['b'], sl, ref)
    return None


def det_arrows_ends(f):
    out = []
    by_id = {c['id']: c for c in f.report['curved']}
    for c in f.curved:
        r = by_id.get(c['id'])
        if r is None:
            if c['id'] in f.unrouted:
                out.append('arrow %s: no route found from %s to %s' % (
                    c['id'], ', '.join(c['from']['refs']), ', '.join(c['to']['refs'])))
            else:
                out.append('curved arrow %s is not in the report' % c['id'])
            continue
        for side, want_end, got_ref, pt in (('start', c['from'], r['start_ref'], r['start']), ('end', c['to'], r['end_ref'], r['end'])):
            if want_end['kind'] == 'gap':
                receiver = _receiver(c)
                if receiver:
                    if pt is None or _reach(f, pt, receiver) > (HEAD_REACH_PT if f.labeled(receiver) else BARE_HEAD_REACH * f.bond_pt):
                        out.append('%s: head does not reach %s, the atom that receives the electron pair' % (c['id'], receiver))
                    elif (reading := _reads_as_bond(f, c, pt, receiver)):
                        out.append(reading)
                    continue
                a, b = (f.xy(ref) for ref in want_end['refs'])
                if pt is None or math.dist(pt, ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)) > .4 * math.dist(a, b):
                    out.append('%s: head lies outside its forming-bond gap' % c['id'])
                continue
            if side == 'start' and c['electrons'] == 1 and want_end['kind'] == 'atom':
                continue  # the unpaired-electron mark is checked by electron_flow
            want = _refs(want_end)
            if got_ref is not None and tuple(sorted(got_ref)) != tuple(sorted(want)):
                out.append('%s %s: the backend put it at %s, asked %s' % (c['id'], side, '-'.join(got_ref), '-'.join(want)))
            if pt is not None:
                at_pair = (side == 'start' and want_end['kind'] == 'atom' and c['electrons'] == 2 and
                           any(p['ref'] == want[0] and p['center'] and math.dist(pt, p['center']) <= f.pair_tail_max
                               for p in f.report['lone_pairs']))
                if at_pair:
                    continue  # the drawn electron source identifies its atom; arrows_lone_pair checks it independently
                near = _nearest_end(f, pt, want_end['kind'])
                if tuple(sorted(near)) != tuple(sorted(want)):
                    out.append('%s %s point is nearest to %s, not to %s' % (c['id'], side, '-'.join(near), '-'.join(want)))
                elif side == 'end' and want_end['kind'] == 'atom':
                    reach = _reach(f, pt, want[0])
                    if reach > (HEAD_REACH_PT if f.labeled(want[0]) else BARE_HEAD_REACH * f.bond_pt):
                        out.append('%s end: head stops %.1f pt from the symbol of %s' % (c['id'], reach, want[0]))
                    elif (reading := _reads_as_bond(f, c, pt, want[0])):
                        out.append(reading)
    return out


def det_arrows_lone_pair(f):
    out = []
    by_id = {c['id']: c for c in f.report['curved']}
    for arrow in f.curved:
        if arrow['electrons'] == 1 or arrow['from']['kind'] != 'atom':
            continue
        ref = arrow['from']['refs'][0]
        pairs = [p for p in f.report['lone_pairs'] if p['ref'] == ref and p['center'] and len(p['dots']) == 2 and all(p['dots'])]
        tail = (by_id.get(arrow['id']) or {}).get('start')
        if not pairs or (tail is None and arrow['id'] not in f.unrouted):
            out.append('%s: atom %s has no measured drawn lone pair or arrow tail' % (arrow['id'], ref))
        elif tail is not None and min(math.dist(tail, p['center']) for p in pairs) > f.pair_tail_max:
            out.append('%s: tail is not at a drawn lone pair of %s (at most %.1f pt from its midpoint)' % (arrow['id'], ref, f.pair_tail_max))
        for when, saved in (('after reload', f.reload), ('CDXML alone', f.reload.get('cdxml'))):
            if saved is not None and saved['atoms'].get(ref, {}).get('pairs', 0) != f.lone_pairs.count(ref):
                out.append('%s: %s drawn lone-pair count of %s differs from lone_pairs' % (arrow['id'], when, ref))
    return out


def det_arrows_head(f):
    out = []
    want = {c['id']: ('full' if c['electrons'] == 2 else 'half') for c in f.curved}
    curved = [a for a in f.reload['arrows'] if a['kind'] == 'curved']
    by_id = {a['id']: a for a in curved if a.get('id') is not None}
    for k, c in enumerate(f.curved):
        a = by_id.get(c['id']) or (curved[k] if k < len(curved) else None)
        if a is None:
            continue
        head = str(a['head'] or '').lower()
        ok = head == want[c['id']] or (want[c['id']] == 'half' and head in ('half', 'fishhook', 'barb', 'harpoon'))
        if not ok:
            out.append('%s: head %r, %s asked (%d electron%s)' % (c['id'], a['head'], want[c['id']], c['electrons'], 's' if c['electrons'] == 2 else ''))
        if str(a['tail']).lower() not in ('none', '', 'false'):
            out.append('%s: a head at the start (%r)' % (c['id'], a['tail']))
    for a in f.reload['arrows']:
        if a['kind'] == 'stoich':
            continue  # two headless lines, checked by reaction_kinds
        if a['kind'] != 'curved' and str(a['head'] or 'full').lower() not in ('full', 'equilibrium', 'half'):
            out.append('a reaction arrow has head %r' % a['head'])
    return out


def det_arrows_color(f):
    out = []
    for a in f.reload['arrows']:
        if a['color'] is not None:
            out.append('%s arrow %s is %s, not #000000' % (a['kind'], a.get('id') or '', a['color']))
    for aid, c in _svg_arrow_colors(f):
        out.append('SVG: arrow %s is drawn in %s' % (aid, c))
    return out


def _svg_arrow_colors(f):
    if not f.svg_text:
        return []
    root = ET.fromstring(f.svg_text.encode('utf-8'))
    bad = []
    for el in root.iter():
        if (el.get('data-role') or '') == 'arrow':
            for v in _paints(el):
                c = norm_color(v)
                if c is not None and c not in ('#ffffff',):
                    bad.append((el.get('data-id') or el.get('data-arrow') or '?', c))
    return bad


def _ring_bonds(f):
    """Bonds on a ring, as sorted ref pairs: their two atoms stay joined without them."""
    out = set()
    for b in f.bonds:
        a, c = b['a'], b['b']
        seen, stack = {a}, [a]
        while stack:
            x = stack.pop()
            for y in f.nbrs[x]:
                if (x, y) in ((a, c), (c, a)) or y in seen:
                    continue
                seen.add(y)
                stack.append(y)
        if c in seen:
            out.add(tuple(sorted((a, c))))
    return out


def _in_ring(f):
    """Atoms on a ring (their angles are the ring's, not a chain's)."""
    ring = set()
    for key in _ring_bonds(f):
        ring |= set(key)
    return ring


def _linear(f, ref):
    orders = [float(b['order']) for b in f.bonds if ref in (b['a'], b['b'])]
    return any(o == 3 for o in orders) or sum(1 for o in orders if o == 2) >= 2


def det_chain_angles(f, report_all=False):
    from iupac_features import METALS
    ring = _in_ring(f)
    out, rows = [], []
    for ref, nb in f.nbrs.items():
        if f.atoms[ref]['el'] in METALS or f.atoms[ref].get('kind') == 'center':
            continue
        if ref in ring or len(nb) not in (2, 3) or _linear(f, ref) or f.atoms[ref].get('kind') == 'residue':
            continue
        p = f.xy(ref)
        dirs = sorted(angle_of(p, f.xy(n)) for n in nb)
        if len(nb) == 2:
            a = adiff(dirs[0], dirs[1])
            devs = [abs(a - 120.0)]
            angles = [a]
        else:
            angles = [((dirs[(k + 1) % 3] - dirs[k]) % 360.0) for k in range(3)]
            devs = [abs(a - 120.0) for a in angles]
        rows.append((ref, angles, max(devs)))
        if max(devs) > ANGLE_TOL:
            why = f.exceptions.get(ref)
            if why is None:
                out.append('%s: angles %s deviate %.1f degrees from 120' % (ref, '/'.join('%.0f' % a for a in angles), max(devs)))
    return (out, rows) if report_all else out


def _four_bond_atoms(f):
    """Atoms with four bonds, not on a ring, with two chain bonds and two end groups, or with one chain bond and three
    end groups (a tetrahedral intermediate, a tert-butyl group): then one end group continues the chain."""
    from iupac_features import METALS
    ring = _in_ring(f)
    for ref, nb in f.nbrs.items():
        if f.atoms[ref]['el'] in METALS or f.atoms[ref].get('kind') == 'center':
            continue
        if len(nb) != 4 or ref in ring:
            continue
        chain = [n for n in nb if len(f.nbrs[n]) > 1]
        ends = [n for n in nb if len(f.nbrs[n]) == 1]
        if len(chain) in (1, 2) and len(chain) + len(ends) == 4:
            yield ref, chain, ends


def det_four_bond(f):
    out = []
    ring = _in_ring(f)
    stereo_centers = _stereo_centers(f)
    metal_centers = {g['center'] for p in f.resolved['panels'] for g in p.get('metal_geometry', [])}
    for ref, nb in f.nbrs.items():
        if ref in metal_centers or f.atoms[ref].get('kind') == 'center':
            continue
        if len(nb) > 4 and ref not in ring and ref not in stereo_centers and not f.atoms[ref].get('geometry'):
            ds = sorted(angle_of(f.xy(ref), f.xy(n)) for n in nb)
            spans = [(ds[(k + 1) % len(ds)] - ds[k]) % 360 for k in range(len(ds))]
            if max(abs(d - 360 / len(ds)) for d in spans) > ANGLE_TOL or min(adiff(d, 90) for d in ds) > 2:
                out.append('%s: more than four bonds need equal angles and one vertical bond unless a configuration is specified' % ref)
            continue
        if len(nb) != 4 or ref in ring:
            continue
        dirs = sorted(angle_of(f.xy(ref), f.xy(n)) for n in nb)
        actual = sorted((dirs[(k + 1) % 4] - dirs[k]) % 360 for k in range(4))
        terminal = [n for n in nb if len(f.nbrs[n]) == 1]
        doubles = [n for n in nb if any({b['a'], b['b']} == {ref, n} and b['order'] == 2 for b in f.bonds)]
        square = ref not in stereo_centers and (len(terminal) == 4 or len(doubles) >= 2
                  or not terminal and all(len(f.nbrs[n]) >= 3 for n in nb))
        wanted = [90, 90, 90, 90] if square else [60, 60, 120, 120] if len(terminal) in (1, 3) else [60, 90, 90, 120]
        if max(abs(a - b) for a, b in zip(actual, wanted)) > ANGLE_TOL:
            out.append('%s: four-bond angles %s; required %s' % (ref, '/'.join('%.0f' % a for a in actual), '/'.join(map(str, wanted))))
        if square and len(doubles) == 2 and abs(adiff(angle_of(f.xy(ref), f.xy(doubles[0])), angle_of(f.xy(ref), f.xy(doubles[1]))) - 180) > ANGLE_TOL:
            out.append('%s: its two double bonds must be opposite' % ref)
        if ref in stereo_centers and all(min(adiff(d, a) for a in (0, 90, 180, 270)) < 2 for d in dirs):
            out.append('%s: axis-aligned four-bond stereocenter' % ref)
        if not square and len(terminal) in (1, 3) and not any(min(adiff(angle_of(f.xy(ref), f.xy(n)), a) for a in (0, 90, 180, 270)) <= ANGLE_TOL for n in terminal):
            out.append('%s: the small terminal substituent must have a horizontal/vertical bond when free' % ref)
    return out


def default_circle_d(f):
    return 2 * CIRCLE_RATIO * f.size + float(f.style.get('line_width_pt', 0.6))


def _charges_by_ref(f):
    out = {}
    for c in f.report['charges']:
        out.setdefault(c['ref'], []).append(c)
    return out


def det_charge_circled(f):
    out = []
    by = _charges_by_ref(f)
    for ref, a in f.atoms.items():
        if a.get('circled'):
            kinds = [c['kind'] for c in by.get(ref, [])]
            if 'circle' not in kinds:
                out.append('%s: the charge %+d is %s, not a circle' % (ref, a['charge'], kinds[0] if kinds else 'not drawn'))
        g = f.reload['atoms'].get(ref)
        if a.get('circled') and g is not None and g.get('circled') is False:
            out.append('%s: not circled after reload' % ref)
    return out


def det_charge_size(f):
    want = default_circle_d(f)
    return ['%s: circle %.2f pt across, the default is %.2f pt' % (c['ref'], c['outer_d'], want)
            for c in _circles(f) if abs(c['outer_d'] - want) > CIRCLE_TOL_PT]


def det_charge_own_atom(f):
    out = []
    for c in _circles(f):
        ref = c['ref']
        if ref not in f.atoms:
            out.append('circle for unknown atom %s' % ref)
            continue
        ctr, r = c['center'], c['outer_d'] / 2.0
        # nearest atom to the circle: labeled atoms by the glyphs of their label, skeletal atoms by their point
        def gap_to(x):
            parts = f.label_parts(x) if f.labeled(x) else []
            if parts:
                return min(pt_box_gap(ctr, b) for b in parts) - r
            return math.hypot(ctr[0] - f.xy(x)[0], ctr[1] - f.xy(x)[1]) - r
        own = gap_to(ref)
        others = sorted((gap_to(x), x) for x in f.atoms if x != ref)
        if others and others[0][0] < own:
            out.append('%s: the circle is nearer to %s (%.1f pt) than to its own atom (%.1f pt)' % (ref, others[0][1], others[0][0], own))
            continue
        sym = f.symbol_box(ref)
        if sym is not None:
            gs = pt_box_gap(ctr, sym) - r
            if gs > CHARGE_GAP_MAX_PT:
                out.append('%s: the circle is %.1f pt from its symbol letter (more than %.1f: not next to it, e.g. after its H)' % (ref, gs, CHARGE_GAP_MAX_PT))
            box = f.label_box(ref)
            tail = _tail_box(box, sym)
            if tail is not None and pt_box_gap(ctr, tail) < pt_box_gap(ctr, sym):
                out.append('%s: the circle sits by the hydrogens of %r, not by the %s' % (ref, f.atoms[ref]['label'], f.atoms[ref]['el']))
        else:
            if own > 0.6 * f.bond_pt:
                out.append('%s: the circle is %.1f pt from its (skeletal) atom' % (ref, own))
    return out


def _tail_box(box, sym):
    """The part of a label box beside the symbol letter (the hydrogens), or None for a one-letter label."""
    if box is None or sym is None:
        return None
    if box[2] - sym[2] > 2.0:
        return (sym[2], box[1], box[2], box[3])
    if sym[0] - box[0] > 2.0:
        return (box[0], box[1], sym[0], box[3])
    return None


def det_ion_text(f):
    out = []
    by = _charges_by_ref(f)
    for ref, a in f.atoms.items():
        if a.get('charge') and (a.get('ion_text') or abs(int(a['charge'])) >= 2):
            kinds = [c['kind'] for c in by.get(ref, [])]
            if 'circle' in kinds or a.get('circled'):
                out.append('%s: the charge %+d must be superscript text, not a circle' % (ref, a['charge']))
            elif kinds and 'text' not in kinds:
                out.append('%s: the charge %+d is drawn as %s' % (ref, a['charge'], kinds))
    return out


def det_number_size(f):
    want = float(f.style.get('number_size_pt', 8.0))
    out = ['number %r of %s is %.2f pt, %.2f asked' % (n['text'], n['ref'], n['size'], want)
           for n in f.report['numbers'] if n['size'] is not None and abs(n['size'] - want) > SIZE_TOL_PT]
    out += ['number %r of %s is %.2f pt after reload' % (n['text'], n['ref'], n['size'])
            for n in f.reload['numbers'] if n['size'] is not None and abs(n['size'] - want) > SIZE_TOL_PT]
    return out


def det_number_on_h(f):
    out = []
    drawn = {}
    for n in f.report['numbers']:
        drawn.setdefault(n['ref'], []).append(n)
    for ref, ns in drawn.items():
        a = f.atoms.get(ref)
        for n in ns:
            if a is None:
                out.append('number %r on an atom the figure does not have (%s)' % (n['text'], ref))
            elif a['el'] == 'H' and a.get('label'):
                out.append('number %r on the H %s drawn as a letter' % (n['text'], ref))
            elif not a.get('number'):
                out.append('number %r on %s, which asks for none' % (n['text'], ref))
            elif n['text'] != a['number']:
                out.append('number of %s reads %r, %r asked' % (ref, n['text'], a['number']))
        if a is not None and a.get('number') and len(ns) > 1:
            out.append('%d numbers drawn on %s' % (len(ns), ref))
    for ref, a in f.atoms.items():
        if a.get('number') and ref not in drawn:
            out.append('number %r of %s is not drawn' % (a['number'], ref))
    return out


def det_number_place(f, rows=None):
    out = []
    numbers = [n for n in f.report['numbers'] if n['box'] and n['ref'] in f.atoms]
    circles = _circles(f)
    for n in numbers:
        ref, box = n['ref'], n['box']
        cx = (box[0] + box[2]) / 2.0
        sym = f.symbol_box(ref)
        p = f.xy(ref)
        own = box_gap(box, sym) if sym is not None else pt_box_gap(p, box)
        # (a) on the side of the symbol: not past the symbol letter on the side of the hydrogens
        tail = _tail_box(f.label_box(ref), sym)
        side_ok = True
        if tail is not None:
            if tail[0] >= sym[2] - 0.01 and cx > sym[2]:
                side_ok = False
            if tail[2] <= sym[0] + 0.01 and cx < sym[0]:
                side_ok = False
        # (b) clearly nearest to its own atom (other atoms by the glyphs of their labels, or their points)
        others = [(f.gap_to_atom(box, x), 'atom %s' % x) for x in f.atoms if x != ref]
        others += [(box_gap(box, m['box']), 'number of %s' % m['ref']) for m in numbers if m is not n]
        others += [(max(0.0, pt_box_gap(c['center'], box) - c['outer_d'] / 2.0), 'circle of %s' % c['ref']) for c in circles if c['ref'] != ref]
        og, ow = min(others) if others else (99.0, '')
        margin = og - own
        # (c) not between labels: no other atom's letters close by
        letters = min([box_gap(box, b) for x in f.atoms if x != ref and f.labeled(x) for b in f.label_parts(x)] or [99.0])
        if rows is not None:
            rows.append({'number': n['text'], 'ref': ref, 'own_gap_pt': round(own, 2), 'nearest_other': ow,
                         'nearest_other_gap_pt': round(og, 2), 'margin_pt': round(margin, 2), 'other_letters_pt': round(letters, 2),
                         'symbol_side': side_ok})
        if not side_ok:
            out.append('number %r of %s is past its symbol, on the side of its hydrogens (%r)' % (n['text'], ref, f.atoms[ref]['label']))
        if margin < 0:
            out.append('number %r of %s is nearer %s (%.1f pt) than its own atom (%.1f pt)' % (n['text'], ref, ow, og, own))
        elif margin < NUMBER_OWN_MARGIN_PT:
            out.append('number %r of %s: %s is %.1f pt away, its own atom %.1f pt (margin under %.1f pt)' % (
                n['text'], ref, ow, og, own, NUMBER_OWN_MARGIN_PT))
        if letters < NUMBER_LETTER_CLEAR_PT:
            out.append('number %r of %s is %.1f pt from another atom\'s letters (between two labels)' % (n['text'], ref, letters))
    return out


def det_number_clear(f):
    sc = f.scene()
    out = []
    texts = f.texts()
    ink = sc['bonds'] + sc['marks']
    ink_boxes = _boxes(ink)
    for n in f.report['numbers']:
        if not n['box']:
            continue
        nb = box_shape(n['box'])
        worst = (99.0, '')
        # ink and arrows more than NUMBER_CLEAR_PT from the number along x or y keep more than NUMBER_CLEAR_PT
        for s in (ink[i] for i in _near(ink_boxes, nb.bbox, NUMBER_CLEAR_PT)):
            worst = min(worst, (shape_clear(nb, s), _ink_name(s)))
        for aid, shapes in sc['arrows']:
            worst = min(worst, (_min_clear(nb, shapes, NUMBER_CLEAR_PT, overlap_only=False), 'arrow %s' % aid))
        for c in _circles(f):
            worst = min(worst, (pt_box_gap(c['center'], n['box']) - c['outer_d'] / 2.0, 'circle of %s' % c['ref']))
        for owner, kind, text, box in texts:
            if kind == 'number' and owner == n['ref'] and box == n['box']:
                continue
            worst = min(worst, (box_gap(n['box'], box), '%s %r' % (kind, text)))
        if worst[0] < 0:
            out.append('number %r of %s overlaps %s by %.2f pt' % (n['text'], n['ref'], worst[1], -worst[0]))
        elif worst[0] < NUMBER_CLEAR_PT:
            out.append('number %r of %s is %.2f pt from %s (needs %.1f)' % (n['text'], n['ref'], worst[0], worst[1], NUMBER_CLEAR_PT))
    return out


def _bond_kind(f, b):
    la, lb = f.labeled(b['a']), f.labeled(b['b'])
    return 2 if la and lb else 1 if la or lb else 0


def _report_bonds(f):
    by = {r['key']: r for r in f.report['bonds']}
    return [(b, by.get(tuple(sorted((b['a'], b['b']))))) for b in f.bonds
            if not any(f.atoms[r].get('hidden_h') or f.atoms[r].get('center') for r in (b['a'], b['b']))]


def _stretch_ok(f, b, r):
    """A bond grown for the local curved arrow named in the report (one that starts or ends at this bond), for the seat
    of the number of one of its atoms, or for room between crowded labels, to at most STRETCH_MAX standard lengths."""
    if r.get('stretched_for') in ('number', 'room'):
        return ((r['stretched_for'] == 'room' or any((f.atoms.get(x) or {}).get('number') for x in (b['a'], b['b'])))
                and r['length'] is not None and r['length'] <= STRETCH_MAX * f.bond_pt + LENGTH_TOL_PT)
    arrow = next((c for c in f.curved if c['id'] == r.get('stretched_for')), None)
    ends = [e['refs'] for e in (arrow['from'], arrow['to']) if e['kind'] == 'bond'] if arrow else []
    return (any(set(x) == {b['a'], b['b']} for x in ends) and r['length'] is not None
            and r['length'] <= STRETCH_MAX * f.bond_pt + LENGTH_TOL_PT)


def one_label_reference(f):
    vis = [r['visible'] for b, r in _report_bonds(f) if r and r['visible'] and _bond_kind(f, b) == 1 and float(b['order']) == 1
           and not r['lengthened'] and not r.get('stretched_for')]
    return statistics.median(vis) if vis else None


def det_label_bonds(f):
    out = []
    for b, r in _report_bonds(f):
        if _bond_kind(f, b) != 2:
            continue
        name = '%s-%s' % (b['a'], b['b'])
        if r is None or r['length'] is None or r['visible'] is None:
            out.append('%s: missing standard-length/visible-line measurement' % name)
            continue
        # longer than standard only as far as needed to show LINE_SHOW of line (a dot-like line, or a bond a curved arrow uses)
        if abs(r['length'] - f.bond_pt) > LENGTH_TOL_PT and r['visible'] > LINE_SHOW * f.bond_pt + LENGTH_TOL_PT \
                and not _stretch_ok(f, b, r):
            out.append('%s (two labels): %.2f pt long; standard is %.1f pt' % (name, r['length'], f.bond_pt))
        if r['visible'] < LINE_DOT * f.bond_pt - LENGTH_TOL_PT and tuple(sorted((b['a'], b['b']))) not in _ring_bonds(f):
            out.append('%s (two labels): its line shows only %.1f pt and reads as a dot' % (name, r['visible']))
    return out


def det_one_label_bonds(f):
    out = []
    for b, r in _report_bonds(f):
        if _bond_kind(f, b) != 1:
            continue
        if r is None or r['length'] is None or r['visible'] is None:
            out.append('%s-%s: missing standard-length/visible-line measurement' % (b['a'], b['b']))
            continue
        if r['visible'] <= float(f.style['line_width_pt']):
            out.append('%s-%s (one label): no readable visible bond line' % (b['a'], b['b']))
        name = '%s-%s' % (b['a'], b['b'])
        if (r['lengthened'] or (r['length'] is not None and abs(r['length'] - f.bond_pt) > LENGTH_TOL_PT)) and not _stretch_ok(f, b, r):
            out.append('%s (label at one end): %.2f pt long%s; it keeps the standard %.1f pt' % (
                name, r['length'] or float('nan'), ', marked lengthened' if r['lengthened'] else '', f.bond_pt))
        # a label with H (NH2, OH) meets its bond with the heavy atom: the H written toward the bond read as bonded
        for ref, other in ((b['a'], b['b']), (b['b'], b['a'])):
            a = f.atoms[ref]
            if a.get('kind', 'atom') != 'atom' or a['el'] in ('C', 'H') or not a.get('label'):
                continue
            sym = f.symbol_box(ref)
            hs = [p['box'] for p in f.report['pieces'].get(ref) or [] if p['text'] == 'H']
            if sym is None or not hs:
                continue
            h = min(hs, key=lambda q: box_gap(q, sym))
            ux, uy = (h[0] + h[2] - sym[0] - sym[2]) / 2.0, (h[1] + h[3] - sym[1] - sym[3]) / 2.0
            (x0, y0), (x1, y1) = f.xy(ref), f.xy(other)
            vx, vy = x1 - x0, y1 - y0
            if math.hypot(ux, uy) > 1e-6 and math.hypot(vx, vy) > 1e-6 and (ux * vx + uy * vy) / math.hypot(ux, uy) / math.hypot(vx, vy) > 0.7:
                out.append('%s: the bond meets the H of label %s at %s; the heavy atom stands at the bond' % (name, a['label'], ref))
    return out


def det_bond_lengths(f):
    out = []
    for b, r in _report_bonds(f):
        if r is None:
            out.append('%s-%s: not in the report' % (b['a'], b['b']))
            continue
        if _bond_kind(f, b) != 0 or r['length'] is None:
            continue
        if abs(r['length'] - f.bond_pt) > LENGTH_TOL_PT and not _stretch_ok(f, b, r):
            out.append('%s-%s: %.2f pt long, the standard is %.1f pt' % (b['a'], b['b'], r['length'], f.bond_pt))
    return out


def det_colors(f):
    out = []
    for ref, a in f.atoms.items():
        g = f.reload['atoms'].get(ref)
        if g is not None and norm_color(a.get('color')) != g['color']:
            out.append('%s: color %s after reload, %s asked' % (ref, g['color'] or 'black', norm_color(a.get('color')) or 'black'))
    for b in f.bonds:
        g = f.reload['bonds'].get(tuple(sorted((b['a'], b['b']))))
        if g is not None and norm_color(b.get('color')) != g['color']:
            out.append('bond %s-%s: color %s after reload, %s asked' % (b['a'], b['b'], g['color'] or 'black', norm_color(b.get('color')) or 'black'))
    for ref, a in f.atoms.items():                    # a residue's color is on its name
        caps = [c for c in f.reload['captions'] if c['role'] == 'residue' and c['text'] == a.get('label')]
        if a.get('kind') == 'residue' and caps and norm_color(a.get('color')) not in [c['color'] for c in caps]:
            out.append('residue name %r: color %s after reload, %s asked' % (a['label'], '/'.join(sorted({c['color'] or 'black' for c in caps})),
                                                                             norm_color(a.get('color')) or 'black'))
    if f.svg_text:
        want = {norm_color(a.get('color')) for a in f.atoms.values()} | {norm_color(b.get('color')) for b in f.bonds}
        want.discard(None)
        used = _svg_colors(f.svg_text)
        for c in sorted(want - set(used)):
            out.append('SVG: the color %s asked for is not used' % c)
        for c in sorted(set(used) - want - {'#ffffff'}):
            out.append('SVG: %d element(s) use %s, which nothing asks for' % (used[c], c))
    return out


def _paints(el):
    """The fill and the stroke of an SVG element, from its style attribute before its own attributes."""
    st = {k.strip(): v.strip() for k, v in (kv.split(':', 1) for kv in (el.get('style') or '').split(';') if ':' in kv)}
    return st.get('fill', el.get('fill')), st.get('stroke', el.get('stroke'))


def _svg_colors(svg_text):
    root = ET.fromstring(svg_text.encode('utf-8'))
    used = {}
    for el in root.iter():
        tag = co.local(el.tag)
        for v in _paints(el):
            c = norm_color(v)
            if c is None or (tag == 'rect' and c == '#ffffff') or (tag == 'svg'):
                continue
            used[c] = used.get(c, 0) + 1
    return used


def det_cdxml(f):
    """figure.cdxml (what ChemDraw opens), read back by Octet's CDXML reader without Octet's JSON layer."""
    if not f.reload['present']:
        return []                                       # graph reports the missing reload
    c = f.reload['cdxml']
    if c is None:
        return ['no CDXML-only reload ("cdxml_only" in reload.json): the file for ChemDraw was not read back']
    out = []
    if c['lost']:
        out.append('CDXML alone: the reader lost %d item(s): %s' % (c['lost'], json.dumps(c['lost_raw'], ensure_ascii=False)[:200]))
    want, have = set(f.atoms), set(c['atoms'])
    if want - have:
        out.append('CDXML alone: atoms missing: %s' % sorted(want - have)[:12])
    if have - want:
        out.append('CDXML alone: atoms the figure does not have: %s' % sorted(have - want)[:12])
    for ref in sorted(want & have):
        a, g = f.atoms[ref], c['atoms'][ref]
        q = int(a.get('charge') or 0)
        # an abbreviation is written as a label ChemDraw does not read as chemistry; the reader may still take an element
        # from its first letters (P1 as phosphorus), so it is checked by its text below
        if g['el'] is not None and g['el'] != a['el'] and a.get('kind') != 'abbrev':
            out.append('CDXML alone: %s is %s, %s asked' % (ref, g['el'], a['el']))
        if g['charge'] != q:
            out.append('CDXML alone: %s has charge %+d, %+d asked' % (ref, g['charge'], q))
        if a.get('kind') == 'abbrev' and g['label'] != a['label']:
            out.append('CDXML alone: %s reads %r, %r asked' % (ref, g['label'], a['label']))
        if a.get('ts') and a.get('kind', 'atom') == 'atom' and g['h'] != int(a.get('h') or 0):
            out.append('CDXML alone: TS %s has %s H, %s asked' % (ref, g['h'], a.get('h') or 0))
        if a.get('circled') and g.get('circled') is False:
            out.append('CDXML alone: the charge of %s is not circled' % ref)
        if g['color'] != norm_color(a.get('color')):
            out.append('CDXML alone: %s is %s, %s asked' % (ref, g['color'] or 'black', norm_color(a.get('color')) or 'black'))
    wb = {tuple(sorted((b['a'], b['b']))): b for b in f.bonds}
    if set(wb) - set(c['bonds']):
        out.append('CDXML alone: bonds missing: %s' % sorted(set(wb) - set(c['bonds']))[:8])
    if set(c['bonds']) - set(wb):
        out.append('CDXML alone: bonds the figure does not have: %s' % sorted(set(c['bonds']) - set(wb))[:8])
    for key in sorted(set(wb) & set(c['bonds'])):
        g, b = c['bonds'][key], wb[key]
        if not order_ok(2 if b.get('style') == '1.5' else b['order'], g['order']):
            out.append('CDXML alone: bond %s-%s has order %s, %s asked' % (key[0], key[1], g['order'], b['order']))
        if g['color'] != norm_color(b.get('color')):
            out.append('CDXML alone: bond %s-%s is %s, %s asked' % (key[0], key[1], g['color'] or 'black', norm_color(b.get('color')) or 'black'))
    out += ['CDXML alone: ' + x for x in _kekule_doubles(wb, c['bonds'], 'in the CDXML alone')]
    out += ['CDXML alone: ' + x for x in _style_issues(wb, c['bonds'], 'in the CDXML alone')]
    out += _arrow_counts(f, c, 'in the CDXML alone')
    for a in c['arrows']:
        if a['color'] is not None:
            out.append('CDXML alone: %s arrow %s is %s' % (a['kind'], a.get('id') or '', a['color']))
    heads = collections.Counter(str(a['head']).lower() for a in c['arrows'] if a['kind'] == 'curved')
    full = collections.Counter(str(a['head']).lower() for a in f.reload['arrows'] if a['kind'] == 'curved')
    if heads != full:
        out.append('CDXML alone: curved arrow heads %s, the full file has %s' % (dict(heads), dict(full)))
    # texts (titles, conditions, free texts, residue names) and numbers: the same as in the full file
    caps = collections.Counter((x['text'], x['role']) for x in c['captions'])
    full_caps = collections.Counter((x['text'], x['role']) for x in f.reload['captions'])
    for (text, role), n in sorted((full_caps - caps).items(), key=str):
        out.append('CDXML alone: %s %r missing (%d)' % (role or 'text', text, n))
    for (text, role), n in sorted((caps - full_caps).items(), key=str):
        out.append('CDXML alone: %s %r that the full file does not have (%d)' % (role or 'text', text, n))
    out += _residue_names(f, c, 'in the CDXML alone')
    nums = {(n['ref'], n['text']) for n in c['numbers']}
    for ref, a in sorted(f.atoms.items()):
        if a.get('number') and not (a['el'] == 'H' and a.get('label')) and (ref, a['number']) not in nums:
            out.append('CDXML alone: number %r of %s missing' % (a['number'], ref))
    want_size = float(f.style.get('number_size_pt', 8.0))
    for n in c['numbers']:
        if n['size'] is not None and abs(n['size'] - want_size) > SIZE_TOL_PT:
            out.append('CDXML alone: number %r of %s is %.2f pt' % (n['text'], n['ref'], n['size']))
    return out


def _frame_bonds(f):
    """Single bonds whose middle lies on their own ink: those between two skeletal atoms, else (a figure without one,
    such as CH3-I) every plain single bond (a bond at a label still shows its middle)."""
    visible = [b for b in f.bonds if not any(f.atoms[r].get('hidden_h') for r in (b['a'], b['b']))]
    plain = [b for b in visible if _bond_kind(f, b) == 0 and float(b['order']) == 1 and b.get('style') not in ('partial', '1.5')]
    return plain or [b for b in visible if float(b['order']) == 1 and (b.get('style') or 'plain') in ('plain', 'coord')]


def det_frame(f):
    """The report's atom positions must lie on the SVG's bond ink (same frame, same drawing), and the text must have
    been measured in the font the SVG names (the backend's font_check: Arial, or a font with Arial's widths)."""
    sc = f.scene()
    if not f.svg_text:
        return ['figure.svg is missing']
    out = []
    fc = f.report.get('font') or {}
    if fc.get('checked') and _num(fc.get('ratio')) is not None and abs(float(fc['ratio']) - 1.0) > FONT_TOL:
        out.append('the text was measured in a font %.1f %% %s than %s (the browser has no %s or a font with its widths, '
                   'such as Liberation Sans): labels, gaps and visible lines differ from the SVG shown in %s' % (
                       abs(float(fc['ratio']) - 1.0) * 100, 'wider' if float(fc['ratio']) > 1 else 'narrower',
                       fc.get('font'), fc.get('font'), fc.get('font')))
    plain = _frame_bonds(f)
    if not plain or not sc['bonds']:
        return out
    miss = 0
    mids = []
    for b in plain:
        pa, pb = f.xy(b['a']), f.xy(b['b'])
        mids.append(((pa[0] + pb[0]) / 2.0, (pa[1] + pb[1]) / 2.0))
    # only the ink whose bbox is within 3 pt of a bond's middle along x and y can pass dist_pt_box < 3
    for mid, cands in zip(mids, _near_pairs(_boxes([m + m for m in mids]), _boxes(sc['bonds']), 3.0)):
        ink = [s for s in (sc['bonds'][i] for i in cands) if co.dist_pt_box(mid, s.bbox) < 3.0]
        near = min(co.dist_pt_seg(mid[0], mid[1], *s.pts[i], *s.pts[(i + 1) % len(s.pts)])
                   for s in ink for i in range(len(s.pts))) if ink else 99.0
        if near > 1.0:
            miss += 1
    if miss > len(plain) / 2.0:
        out.append('%d of %d plain bonds have no SVG ink at their reported position: report.json and figure.svg do not use '
                   'the same frame (points in the SVG user frame) or are not the same drawing' % (miss, len(plain)))
    return out


def _axis_rows(p, q, shapes):
    """Each shape projected on the line p-q: (lowest and highest along it, mean and spread across it, shape)."""
    length = math.dist(p, q)
    ux, uy = (q[0] - p[0]) / length, (q[1] - p[1]) / length
    rows = []
    for s in shapes:
        along = [(x - p[0]) * ux + (y - p[1]) * uy for x, y in s.pts]
        across = [-(x - p[0]) * uy + (y - p[1]) * ux for x, y in s.pts]
        rows.append((min(along), max(along), sum(across) / len(across), max(across) - min(across), s))
    return rows


def _joined_marks(shapes):
    """The backend can cut one mark into polygons that share an edge; those pieces are read as one mark (their hull)."""
    root = list(range(len(shapes)))

    def top(i):
        while root[i] != i:
            i = root[i]
        return i
    for i, j in itertools.combinations(range(len(shapes)), 2):
        if sum(any(math.dist(v, w) < .01 for w in shapes[j].pts) for v in shapes[i].pts) >= 2:
            root[top(j)] = top(i)
    out = []
    for r in sorted({top(i) for i in range(len(shapes))}):
        part = [s for i, s in enumerate(shapes) if top(i) == r]
        if len(part) == 1:
            out.append(part[0])
            continue
        pts = sorted({v for s in part for v in s.pts})

        def half(seq):
            h = []
            for v in seq:
                while len(h) > 1 and (h[-1][0] - h[-2][0]) * (v[1] - h[-2][1]) - (h[-1][1] - h[-2][1]) * (v[0] - h[-2][0]) <= 0:
                    h.pop()
                h.append(v)
            return h[:-1]
        mark = co.Shape(part[0].cls, 'poly', half(pts) + half(pts[::-1]), True, True, 0.0, '')
        mark.owner = part[0].owner
        out.append(mark)
    return out


def _styled_ink(f, b):
    """Each mark of this bond (the saved SVG polygons, a cut mark joined), projected along/across its axis."""
    p, q = f.xy(b['a']), f.xy(b['b'])
    own = (('bond', (b['a'], b['b'])), ('bond', (b['b'], b['a'])))
    return _axis_rows(p, q, _joined_marks([s for s in f.scene()['bonds'] if s.owner in own]))


def _dash_findings(f, rows, style):
    """Marks of a dashed or dotted line: at least three, apart, none solid; round at the line width when dotted."""
    if len(rows) < 3:
        return ['fewer than three visible marks']
    ordered = sorted(rows, key=lambda r: r[0])
    lengths = [r[1] - r[0] for r in ordered]
    widths = [r[3] for r in ordered]
    gaps = [b[0] - a[1] for a, b in zip(ordered, ordered[1:])]
    line = float(f.style['line_width_pt'])
    if style == 'dotted' and any(abs(a - b) > .15 or a > 1.35 * line or a < .65 * line for a, b in zip(lengths, widths)):
        return ['partial-line marks are not round dots at the line width']
    if any(length >= .7 * f.bond_pt for length in lengths):
        return ['partial line is solid rather than dotted/dashed']
    if min(gaps) <= 0:
        return ['line marks touch']
    return []


def _styled_label_clear(f, rows):
    return any(shape_clear(box_shape(box), row[4]) <= 0
               for _, kind, _, box in f.texts() if kind == 'label' for row in rows)


def det_partial_dashes(f):
    out = []
    for b in f.bonds:
        if b.get('style') != 'partial':
            continue
        rows = _styled_ink(f, b)
        bad = _dash_findings(f, rows, f.resolved.get('drawing', {}).get('partial_bonds', 'dashed'))
        if any(abs(r[2]) > .25 or r[3] > float(f.style['line_width_pt']) + .2 for r in rows):
            bad.append('partial-bond marks are not on the bond axis')
        if _styled_label_clear(f, rows):
            bad.append('a partial bond is too close to a label')
        out += ['%s-%s: %s' % (b['a'], b['b'], msg) for msg in bad]
    return out


def _inner_side(f, b):
    """Side of bond b that faces its ring (the backend's choice: octet_draw.delocalized_ring and ring_side, which turns
    away from a partial bond leaving an end at a narrow angle), else its neighbors."""
    a, z = b['a'], b['b']
    adj = {r: [n for n in f.nbrs[r] if frozenset((r, n)) != frozenset((a, z))] for r in f.nbrs}
    styles = {frozenset((e['a'], e['b'])): e.get('style') for e in f.bonds}
    ring = delocalized_ring(adj, a, z, styles)
    p, q = f.xy(a), f.xy(z)
    if ring:
        def cross(r):
            return (q[0] - p[0]) * (f.xy(r)[1] - p[1]) - (q[1] - p[1]) * (f.xy(r)[0] - p[0])
        def angle(end, r):
            o, t = (p, q) if end == a else (q, p)
            d = math.degrees(math.atan2(f.xy(r)[1] - o[1], f.xy(r)[0] - o[0]) - math.atan2(t[1] - o[1], t[0] - o[0])) % 360
            return min(d, 360 - d)
        value = ring_side(ring, styles, cross, angle)
        return math.copysign(1, value) if abs(value) > .01 else 0
    neighbors = [r for r in f.nbrs[a] + f.nbrs[z] if r not in (a, z)]
    cross = sum((q[0] - p[0]) * (f.xy(r)[1] - p[1]) -
                (q[1] - p[1]) * (f.xy(r)[0] - p[0]) for r in neighbors)
    return math.copysign(1, cross) if abs(cross) > .01 else None


def det_delocalized_bonds(f):
    out = []
    for b in f.bonds:
        if b.get('style') != '1.5':
            continue
        rows = _styled_ink(f, b)
        solid = [r for r in rows if abs(r[2]) < .3]
        dashed = [r for r in rows if abs(r[2]) >= .3]
        bad = _dash_findings(f, dashed, f.resolved.get('drawing', {}).get('partial_bonds', 'dashed'))
        if len(solid) != 1 or (solid and solid[0][1] - solid[0][0] < .4 * f.bond_pt):
            bad.append('missing continuous solid line on the bond axis')
        if dashed:
            spacing = f.bond_pt * float(f.style['double_spacing'])
            side = _inner_side(f, b)
            if any(abs(abs(r[2]) - spacing) > .25 or r[3] > float(f.style['line_width_pt']) + .2 for r in dashed):
                bad.append('second line is not parallel at the double-bond spacing')
            if side is not None and any(r[2] * side <= 0 for r in dashed):
                bad.append('dotted second line is outside its ring')
            if max(r[2] for r in dashed) - min(r[2] for r in dashed) > .25:
                bad.append('dots are not on one side of the solid line')
        if _styled_label_clear(f, rows):
            bad.append('a 1.5 bond is too close to a label')
        out += ['%s-%s: %s' % (b['a'], b['b'], msg) for msg in bad]
    return out


def det_ts_brackets(f):
    out = []
    groups = collections.defaultdict(list)
    for ref, a in f.atoms.items():
        if a.get('ts'):
            groups[a['species']].append(ref)
    for sid, refs in groups.items():
        for when, rl in (('reload', f.reload), ('CDXML', f.reload.get('cdxml'))):
            if not rl:
                out.append('%s: no %s TS frame' % (sid, when))
                continue
            if any(r not in rl['atoms'] for r in refs):
                out.append('%s: %s is missing TS atoms' % (sid, when))
                continue
            def encloses(b):
                return b.get('kind') == 'brackets' and all(
                    min(b['x1'], b['x2']) < rl['atoms'][r]['x'] < max(b['x1'], b['x2']) and
                    min(b['y1'], b['y2']) < rl['atoms'][r]['y'] < max(b['y1'], b['y2']) for r in refs)
            frames = [b for b in rl['boxes'] if encloses(b)]
            if len(frames) != 1:
                out.append('%s: %s needs exactly one square bracket pair' % (sid, when))
                continue
            b = frames[0]
            left, top, right, bottom = min(b['x1'], b['x2']), min(b['y1'], b['y2']), max(b['x1'], b['x2']), max(b['y1'], b['y2'])
            marks = [c for c in rl['captions'] if c['text'] == '‡' and c.get('x') is not None
                     and 0 < c['x'] - right < 1 and abs(c['y'] - top) < 1]
            if len(marks) != 1:
                out.append('%s: %s needs one upper-right double dagger' % (sid, when))
            if when != 'reload':
                continue
            p = f.xy(refs[0]); a = rl['atoms'][refs[0]]
            dx, dy = p[0] - a['x'] * f.bond_pt, p[1] - a['y'] * f.bond_pt
            left, right = left * f.bond_pt + dx, right * f.bond_pt + dx
            top, bottom = top * f.bond_pt + dy, bottom * f.bond_pt + dy
            nib = min(.25 * f.bond_pt, (right - left) / 3)
            points = [(left + nib, top), (left, top), (left, bottom), (left + nib, bottom),
                      (right - nib, top), (right, top), (right, bottom), (right - nib, bottom)]
            bracket = [co.Shape('bond', 'line', points[i:i + 2], False, False,
                                float(f.style['line_width_pt']) / 2, 'TS bracket') for i in (0, 1, 2, 4, 5, 6)]
            # Check the exported ink as well as the saved shape.
            if any(min((co.dist_pt_box(((s.pts[0][0] + s.pts[1][0]) / 2, (s.pts[0][1] + s.pts[1][1]) / 2), ink.bbox)
                        for ink in f.scene()['bonds']), default=99) > .5 for s in bracket):
                out.append('%s: bracket ink is missing from SVG' % sid)
            for ref in refs:
                shapes = [box_shape(box) for box in f.label_parts(ref)] or [disk_shape(f.xy(ref), .3)]
                if any(shape_clear(s, edge) < 1.0 for s in shapes for edge in bracket):
                    out.append('%s: brackets touch atom %s' % (sid, ref))
            if any(shape_clear(box_shape(box), edge) <= 0 for _, _, text, box in f.texts()
                   if text != '‡' for edge in bracket):
                out.append('%s: brackets touch text' % sid)
            mark_boxes = [t['box'] for t in f.report['texts'] if t['text'] == '‡' and t['box']
                          and right < _center(t['box'])[0] < right + f.bond_pt and abs(_center(t['box'])[1] - top) < f.bond_pt]
            if len(mark_boxes) != 1:
                out.append('%s: double dagger is not drawn at upper right' % sid)
            else:
                mark = box_shape(mark_boxes[0])
                others = f.scene()['bonds'] + f.scene()['marks']
                others += [s for _, ss in f.scene()['arrows'] for s in ss]
                others += [box_shape(box) for _, _, text, box in f.texts() if text != '‡']
                if any(shape_clear(mark, s) < 1.0 for s in others):
                    out.append('%s: double dagger touches other ink/text' % sid)
    return out


# IUPAC geometry detectors are kept together to make independent feature merges local.
def _stereo_relaxed(f):
    """{species: read from its SMILES without sanitizing}: a transition state, or a species with a coordination bond."""
    ts = {a.get('species') for a in f.atoms.values() if a.get('ts')}
    coord = [r for b in f.bonds if b.get('coord') or b.get('style') == 'coord' for r in (b['a'], b['b'])]
    return {sid: sid in ts or any(r.startswith(sid + ':') for r in coord) for sid in f.resolved.get('stereo_sources') or {}}


def _stereo_centers(f):
    out, relaxed = set(), _stereo_relaxed(f)
    for sid, smiles in (f.resolved.get('stereo_sources') or {}).items():
        out.update(stereo.potential_centers(sid, smiles, relaxed=relaxed[sid]))
    return out


def det_charge_open(f):
    out, texts = [], f.texts()
    for c in f.report['charges']:
        ref = c['ref']
        if not c['center'] or ref not in f.atoms:
            continue
        dirs = [angle_of(f.xy(ref), f.xy(n)) for n in f.nbrs[ref] if not f.atoms[n].get('hidden_h')]
        degree = angle_of(f.xy(ref), c['center'])
        if dirs and min(adiff(degree, d) for d in dirs) < 25:
            out.append('%s: charge is in a bond direction instead of an open sector' % ref)
        if c['kind'] != 'circle':
            signs = [p for p in f.report['pieces'].get(ref, []) if re.match(r'^\d*[+−–-]$', p['text']) and p['box']]
            for sign in signs:
                shape = box_shape(sign['box'])
                obstacles = f.scene()['bonds'] + f.scene()['marks'] + [s for _, ss in f.scene()['arrows'] for s in ss]
                if any(shape_clear(shape, s, overlap_only=True) < CHARGE_CLEAR_PT for s in obstacles):
                    out.append('%s: charge text touches a bond, mark or arrow' % ref)
                for owner, kind, text, box in texts:
                    if owner != ref and box_gap(sign['box'], box) < CHARGE_CLEAR_PT:
                        out.append('%s: charge text touches %s %r' % (ref, kind, text))
    return out


def _seat_disks(f, point, distance, degree, radius):
    """The two dot disks of a lone pair seated at degree (0, 90, 180 or 270) and distance from point."""
    t = math.radians(degree)
    center = (point[0] + distance * math.cos(t), point[1] - distance * math.sin(t))
    dots = [(center[0] + sign * .19 * f.size * math.sin(t), center[1] + sign * .19 * f.size * math.cos(t)) for sign in (-1, 1)]
    return [disk_shape(dot, radius) for dot in dots]


def _seat_free(disks, own_dots, radius, labels, ink):
    """True when the dot disks keep CHARGE_CLEAR_PT from every label shape and from all bond ink and marks but the
    drawn dots own_dots (ink lying wholly within radius + 0.05 of one of them); labels and ink are (shapes, _boxes of
    them).  Only what lies within CHARGE_CLEAR_PT of a disk's bbox can be closer than that, so only that is measured."""
    near_labels, near_ink = (_near_pairs(_boxes(disks), boxes, CHARGE_CLEAR_PT) for _, boxes in (labels, ink))
    for disk, label_rows, ink_rows in zip(disks, near_labels, near_ink):
        obstacles = [labels[0][i] for i in label_rows] + [
            s for s in (ink[0][i] for i in ink_rows)
            if not (s.pts and any(all(math.dist(p, dot) <= radius + .05 for p in s.pts) for dot in own_dots))]
        if not all(shape_clear(disk, obstacle, overlap_only=True) >= CHARGE_CLEAR_PT for obstacle in obstacles):
            return False
    return True


def det_lone_pair_geometry(f):
    out = []
    labels = [(text, box_shape(box)) for _, kind, text, box in f.texts() if kind == 'label']
    label_shapes = [shape for _, shape in labels]
    bonds, ink = f.scene()['bonds'], f.scene()['bonds'] + f.scene()['marks']
    label_boxes, bond_boxes = _boxes(label_shapes), _boxes(bonds)
    seat_obstacles = ((label_shapes, label_boxes), (ink, _boxes(ink)))
    for pair in f.report['lone_pairs']:
        ref, center = pair['ref'], pair['center']
        if ref not in f.atoms or not center:
            out.append('lone pair has no measured atom/position')
            continue
        degree = angle_of(f.xy(ref), center)
        radius = pair.get('radius') or .09 * f.size
        # a seat above, below, left or right blocks a diagonal pair only when none of the four is free
        if min(adiff(degree, d) for d in (0, 90, 180, 270)) > 1 and any(
                _seat_free(_seat_disks(f, f.xy(ref), math.dist(f.xy(ref), center), d, radius), pair['dots'], radius, *seat_obstacles)
                for d in (0, 90, 180, 270)):
            out.append('%s: lone pair at %.1f degrees, not above/below/left/right' % (ref, degree))
        if len(pair['dots']) != 2:
            out.append('%s: lone pair does not have two visible dots' % ref)
        for dot in pair['dots']:
            shape = disk_shape(dot, radius)
            # a clearance under 0 needs overlapping bboxes
            for text, label in (labels[i] for i in _near(label_boxes, shape.bbox, 0.0)):
                if shape_clear(shape, label, overlap_only=True) < 0:
                    out.append('%s: lone pair touches label %r' % (ref, text))
            for s in (bonds[i] for i in _near(bond_boxes, shape.bbox, 0.0)):
                if s.owner and s.owner[0] in ('bond', 'hbond') and shape_clear(shape, s, overlap_only=True) < 0:
                    out.append('%s: lone pair touches bond ink' % ref)
    for ref in set(f.lone_pairs):
        if sum(p['ref'] == ref for p in f.report['lone_pairs']) != f.lone_pairs.count(ref):
            out.append('%s: measured lone-pair count differs from requested count' % ref)
    arrows = {a['id']: a for a in f.report['curved']}
    for arrow in f.curved:
        if arrow['electrons'] != 2 or arrow['from']['kind'] != 'atom':
            continue
        ref = arrow['from']['refs'][0]
        pairs = [p for p in f.report['lone_pairs'] if p['ref'] == ref and p['center']]
        if not pairs:
            continue
        tail = (arrows.get(arrow['id']) or {}).get('start')
        pair = min(pairs, key=lambda p: math.dist(p['center'], tail)) if tail else pairs[0]
        point = f.xy(ref)
        target = tuple(sum(f.xy(r)[k] for r in arrow['to']['refs']) / len(arrow['to']['refs']) for k in (0, 1))
        want, actual = angle_of(point, target), angle_of(point, pair['center'])
        radius, distance = pair.get('radius') or .09 * f.size, math.dist(point, pair['center'])
        for degree in (0, 90, 180, 270):
            if adiff(degree, want) >= adiff(actual, want) - 1:
                continue
            t = math.radians(degree)
            outside = (point[0] + (distance + .13 * f.bond_pt) * math.cos(t), point[1] - (distance + .13 * f.bond_pt) * math.sin(t))
            if min(f.atoms, key=lambda r: math.dist(outside, f.xy(r))) != ref:
                continue
            if _seat_free(_seat_disks(f, point, distance, degree, radius), [dot for own in pairs for dot in own['dots']],
                          radius, *seat_obstacles):
                out.append('%s: reacting lone pair does not face the target although the %.0f-degree axial seat is free' % (ref, degree))
                break
    return out


def _branch_size(f, ref, parent):
    seen, stack = {parent, ref}, [ref]
    while stack:
        for n in f.nbrs[stack.pop()]:
            if n not in seen:
                seen.add(n)
                stack.append(n)
    return len(seen) - 1


def det_ring_substituents(f):
    ring = {frozenset(e) for e in _ring_bonds(f)}
    out = []
    for ref, nb in f.nbrs.items():
        rn = [n for n in nb if frozenset((ref, n)) in ring]
        exo = [n for n in nb if n not in rn]
        if len(rn) != 2 or len(exo) not in (1, 2):
            continue
        ds = sorted(angle_of(f.xy(ref), f.xy(n)) for n in rn)
        span = (ds[1] - ds[0]) % 360
        start, gap = (ds[0], span) if span >= 180 else (ds[1], 360 - span)
        bisector = (start + gap / 2) % 360
        snapped = round(bisector / 30) * 30 % 360
        if adiff(bisector, snapped) <= 5:
            bisector = snapped
        actual = [angle_of(f.xy(ref), f.xy(n)) for n in exo]
        if len(exo) == 1:
            if adiff(actual[0], bisector) > 6:
                out.append('%s: single ring substituent does not bisect the exterior angle' % ref)
            continue
        queue, seen, size = collections.deque([(rn[0], 2)]), {ref, rn[0]}, None
        while queue:
            node, depth = queue.popleft()
            if node == rn[1]:
                size = depth
                break
            for n in f.nbrs[node]:
                if n not in seen and frozenset((node, n)) in ring:
                    seen.add(n)
                    queue.append((n, depth + 1))
        counts = [_branch_size(f, n, ref) for n in exo]
        if size == 6 and min(counts) == 1 and max(counts) > 1:
            large = counts.index(max(counts))
            if adiff(actual[large], bisector) > 6 or abs(adiff(actual[0], actual[1]) - gap / 4) > 6:
                out.append('%s: six-ring large substituent must bisect outward, small one the adjacent angle' % ref)
        else:
            wanted = gap / 3 if all(len(f.nbrs[n]) >= 3 for n in exo) else 60
            if abs(adiff(*actual) - wanted) > 6 and not (size == 4 and abs(adiff(*actual) - 90) <= 6):
                out.append('%s: two ring substituents %.1f degrees apart, required %.1f' % (ref, adiff(*actual), wanted))
    return out


def det_carbon_labels(f):
    out = []
    for ref, atom in f.atoms.items():
        if atom['el'] != 'C' or atom.get('kind', 'atom') != 'atom':
            continue
        nb = f.nbrs[ref]
        needed = not nb or len(nb) == 2 and all(b['order'] == 2 for b in f.bonds if ref in (b['a'], b['b'])) and abs(adiff(angle_of(f.xy(ref), f.xy(nb[0])), angle_of(f.xy(ref), f.xy(nb[1]))) - 180) < 2
        if len(nb) == 1 and len(f.nbrs[nb[0]]) == 1 and f.atoms[nb[0]]['el'] == 'C':
            needed = True
        if needed and (not atom.get('label') or not f.label_box(ref)):
            out.append('%s: isolated/collinear/one-bond carbon must be labeled' % ref)
    return out


def det_stereo_display(f):
    out, centers = [], _stereo_centers(f)
    specified, fused, bridged = set(), set(), set()
    relaxed = _stereo_relaxed(f)
    for sid, smiles in (f.resolved.get('stereo_sources') or {}).items():
        is_ts = relaxed[sid]
        specified.update(stereo.expected(sid, smiles, relaxed=is_ts)[0][0])
        junctions, bridge = stereo.ring_junctions(sid, smiles, relaxed=is_ts)
        fused.update(junctions)
        bridged.update(bridge)
    ring_edges = _ring_bonds(f)
    ring_atoms = {r for key in ring_edges for r in key}           # _in_ring
    for ref in centers:
        edges = [b for b in f.bonds if ref in (b['a'], b['b'])]
        directed = [b for b in edges if b.get('style') in ('wedge', 'hash') and b['a'] == ref]
        junctions = [edge for edge in fused if ref in edge]
        if ref in specified and junctions and ref not in bridged:
            exo = [b for b in edges if tuple(sorted((b['a'], b['b']))) not in ring_edges]
            if not exo and f.atoms[ref].get('h', 0):
                out.append('%s: make H explicit at the ring fusion and wedge that exocyclic bond' % ref)
            if exo and not any(b in directed for b in exo):
                out.append('%s: put the ring-fusion stereobond on its exocyclic substituent' % ref)
            for edge in junctions:
                far = next(r for r in edge if r != ref)
                degree = angle_of(f.xy(ref), f.xy(far))
                if min(adiff(degree, a) for a in (90, 270)) <= 2:
                    for b in exo:
                        end = b['b'] if b['a'] == ref else b['a']
                        if len(f.nbrs[end]) == 1 and adiff(angle_of(f.xy(ref), f.xy(end)), degree + 180) > 6:
                            out.append('%s: the small substituent must be vertical opposite the vertical fusion bond' % ref)
        if any(b.get('style') in ('bold', 'hashed') for b in edges):
            out.append('%s: bold/unwedged hash is not a stereobond' % ref)
        if len(directed) >= 3:
            out.append('%s: three or more stereobonds at one center' % ref)
        if len(directed) == 2:
            if directed[0]['style'] == directed[1]['style'] and len(edges) == 3:
                out.append('%s: a three-bond center has two same-type wedges' % ref)
            if directed[0]['style'] == directed[1]['style'] and len(edges) == 4:
                out.append('%s: adjacent stereobonds must be one solid and one hashed wedge' % ref)
            degree = adiff(*(angle_of(f.xy(ref), f.xy(b['b'])) for b in directed))
            if len(edges) == 4 and abs(degree - 60) > 10 and abs(degree - 90) > 10:
                out.append('%s: its wedge/hash pair is not adjacent at 60 degrees (90 acceptable)' % ref)
        if len(edges) == 4 and len(directed) == 1 and ref not in ring_atoms:
            plain = [angle_of(f.xy(ref), f.xy(b['b'] if b['a'] == ref else b['a'])) for b in edges if b not in directed]
            plain.sort()
            gaps = [(plain[(k + 1) % 3] - plain[k]) % 360 for k in range(3)]
            if max(gaps) >= 180 - 1:
                out.append('%s: three acyclic plain bonds lie in a semicircle' % ref)
        for b in directed:
            far = b['b']
            if far in centers and any(n not in centers and tuple(sorted((ref, n))) not in ring_edges for n in f.nbrs[ref]):
                out.append('%s: move the stereobond away from the adjacent stereocenter' % ref)
            if far in centers and f.atoms[ref].get('h', 0) and not any(f.atoms[n]['el'] == 'H' for n in f.nbrs[ref]):
                out.append('%s: H must be explicit when a stereobond joins another stereocenter' % ref)
            if f.atoms[ref].get('h', 0) and len(f.nbrs[ref]) == 3:
                heavy = [n for n in f.nbrs[far] if f.atoms[n]['el'] != 'H']
                tert_butyl = f.atoms[far]['el'] == 'C' and len(heavy) == 4 and all(len(f.nbrs[n]) == 1 for n in heavy if n != ref)
                simple_ring = far in ring_atoms and all(len(f.nbrs[n]) == 2 for n in heavy if n in ring_atoms)
                if len(heavy) > 2 and not tert_butyl and not simple_ring:
                    out.append('%s: implicit H is ambiguous at the wide-end substituent; make H explicit' % ref)
    for b in f.bonds:
        if b.get('style') == 'wavy' and (b['order'] != 1 or b['a'] in specified or b['b'] in specified):
            out.append('%s-%s: put unknown configuration on a single bond away from specified stereocenters' % (b['a'], b['b']))
        if b.get('style') == 'wavy':
            for ref in (b['a'], b['b']):
                edges = [e for e in f.bonds if ref in (e['a'], e['b'])]
                if any(e['order'] == 2 for e in edges):
                    for edge in edges:
                        far = edge['b'] if edge['a'] == ref else edge['a']
                        if edge is b and far in centers:
                            out.append('%s: an unknown double-bond marker must not join a stereogenic substituent' % ref)
                        if edge['order'] == 1 and far not in centers and edge.get('style') != 'wavy':
                            out.append('%s: both substituents at the unknown double-bond end must be wavy' % ref)
    return out


def det_hbond_dots(f):
    out = []
    for pair in f.hbonds:
        p, q = (f.xy(r) for r in pair)
        rows = _axis_rows(p, q, [s for s in f.scene()['bonds'] if s.owner and s.owner[0] == 'hbond' and set(s.owner[1]) == set(pair)])
        bad = _dash_findings(f, rows, 'dotted')
        visible = max((r[1] for r in rows), default=0) - min((r[0] for r in rows), default=0)
        if visible < 2 * CAP * f.size - LENGTH_TOL_PT:
            bad.append('visible dotted span %.2f pt is less than twice the label height %.2f pt' % (visible, 2 * CAP * f.size))
        out += ['hbond %s-%s: %s' % (pair[0], pair[1], msg) for msg in bad]
    return out


# IUPAC chemical-feature detectors share one block to keep geometry changes independent.
def inferred_radicals(f):
    return ['%s: %d unpaired electron(s) come from the bracket H count, charge and bonds, not from radicals' % (ref, a['radical'])
            for ref, a in f.atoms.items() if a.get('radical') and a.get('radical_inferred')]


def det_radical_marks(f):
    out = inferred_radicals(f)
    for ref, a in f.atoms.items():
        n = int(a.get('radical') or 0)
        marks = [m for m in f.report['radicals'] if m.get('ref') == ref]
        if sum(m.get('multiplier', 1) for m in marks) != n:
            out.append('%s: %d unpaired electrons requested, %d measured marks' % (ref, n, len(marks)))
        for when, saved in (('native', f.reload), ('CDXML', f.reload.get('cdxml'))):
            if saved and int(saved['atoms'].get(ref, {}).get('radical') or 0) != n:
                out.append('%s: %s lost the unpaired-electron count' % (ref, when))
        pieces = [p['box'] for p in f.report['pieces'].get(ref, []) if '•' not in p['text'] and not re.fullmatch(r'\d*[+–−-]', p['text'])]
        box = (min(p[0] for p in pieces), min(p[1] for p in pieces), max(p[2] for p in pieces), max(p[3] for p in pieces)) if pieces else f.label_box(ref)
        for mark in marks:
            pt = mark.get('center_pt')
            if pt and box and box[0] + .5 < pt[0] < box[2] - .5 and box[1] + .5 < pt[1] < box[3] - .5:
                out.append('%s: unpaired-electron dot is before or inside its label' % ref)
        # a dot reads as on the nearest letter: it must be nearer its own atom's symbol (or point) than any other atom
        # and than the other pieces of its own label (a written H, a count)
        sym = f.symbol_box(ref)
        for mark in marks:
            pt = mark.get('center_pt')
            if not pt:
                continue
            own = pt_box_gap(pt, sym) if sym else math.dist(pt, f.xy(ref))
            near = [(pt_box_gap(pt, p['box']), 'its %s' % p['text']) for p in f.report['pieces'].get(ref, [])
                    if sym and tuple(p['box']) != tuple(sym) and '•' not in p['text'] and not re.fullmatch(r'\d*[+–−-]', p['text'])]
            for x in f.atoms:
                if x != ref:
                    parts = f.label_parts(x) if f.labeled(x) else []
                    near.append((min(pt_box_gap(pt, b) for b in parts) if parts else math.dist(pt, f.xy(x)), x))
            gap, who = min(near) if near else (math.inf, None)
            if gap < own:
                out.append('%s: unpaired-electron dot is nearer %s (%.1f pt) than its own atom (%.1f pt)' % (ref, who, gap, own))
        if len(marks) == 2 and math.dist(marks[0]['center_pt'], marks[1]['center_pt']) < .5 * f.size:
            out.append('%s: adjacent dots look like a lone pair' % ref)
        if n and a.get('charge'):
            pieces = f.report['pieces'].get(ref, [])
            dots = [p for p in pieces if '•' in p['text']]
            signs = [p for p in pieces if p['text'] in ('+', '–', '−', '-') or re.fullmatch(r'\d+[+–−-]', p['text'])]
            if dots and signs and min(p['box'][0] for p in signs) < max(p['box'][2] for p in dots) - .5:
                out.append('%s: radical-ion charge must follow the dot' % ref)
    return out


def det_electron_flow(f):
    from iupac_features import electron_flow_errors
    out = electron_flow_errors(f.curved, atoms=f.atoms, bonds=f.bonds, pair_counts=collections.Counter(f.lone_pairs), advisory=True)
    rows = {c['id']: c for c in f.report['curved']}
    used_pairs = collections.Counter()
    for c in f.curved:
        row = rows.get(c['id'], {})
        if c['electrons'] == 2 and c['from']['kind'] == 'atom' and row.get('start'):
            pairs = [(i, m['center']) for i, m in enumerate(f.report['lone_pairs']) if m['ref'] == c['from']['refs'][0] and m['center']]
            if pairs:
                nearest = min(pairs, key=lambda m: math.dist(row['start'], m[1]))
                used_pairs[nearest[0]] += 1
        if c['id'] not in f.unrouted and c['to']['kind'] == 'gap' and not _receiver(c):   # arrows_ends checks that a pair's head reaches its receiving atom
            a, b = (f.xy(r) for r in c['to']['refs'])
            end = row.get('end')
            if end is None:
                out.append('%s: forming-bond arrow has no head' % c['id'])
            else:
                dx, dy = b[0] - a[0], b[1] - a[1]
                t = ((end[0] - a[0]) * dx + (end[1] - a[1]) * dy) / max(dx * dx + dy * dy, 1e-8)
                if not .25 <= t <= .75 or co.dist_pt_seg(end[0], end[1], a[0], a[1], b[0], b[1]) > .2 * f.bond_pt:
                    out.append('%s: arrow head is outside the forming-bond gap' % c['id'])
    if any(n > 1 for n in used_pairs.values()):
        out.append('more than one full-headed arrow starts at the same drawn lone pair')
    return out


ARROW_CLEAR_BOND = 0.035       # arrows_clear: bond lengths; an arrow this close to ink that is not its own start or end reads as
                               # touching it (0.5 pt at the ACS bond length 14.4 pt)


def _step_bonds(f, pid):
    """Atoms (by map) drawn once in a panel, its bonds {frozenset(maps): order} without coordination bonds,
    the pairs aromatic in its SMILES, and whether it is a transition state."""
    panel = next(p for p in f.resolved['panels'] if p['id'] == pid)
    mp = lambda ref: ref.rsplit(':', 1)[1]
    count = collections.Counter(mp(a['ref']) for a in panel['atoms'])
    atoms = {mp(a['ref']) for a in panel['atoms'] if count[mp(a['ref'])] == 1}
    bonds, coord = {}, set()
    for b in panel['bonds']:
        pair = frozenset((mp(b['a']), mp(b['b'])))
        if pair <= atoms:
            if b.get('coord') or b.get('style') == 'coord':
                coord.add(pair)
            else:
                bonds[pair] = b.get('order', 1)
    ts = any(a.get('ts') for a in panel['atoms']) or any(b.get('style') == 'partial' for b in panel['bonds'])
    panel = dict(panel, state={mp(a['ref']): (a.get('charge') or 0, a.get('h'), a.get('el'), a.get('kind', 'atom'))
                               for a in panel['atoms'] if mp(a['ref']) in atoms})
    aromatic = set()
    for sid, smiles in (f.resolved.get('stereo_sources') or {}).items():
        if sid.split('.', 1)[0] == pid:
            aromatic |= stereo.aromatic_pairs(smiles)
    return panel, atoms, bonds, coord, aromatic, ts


def det_arrows_steps(f):
    """A bond that forms, breaks or changes order between two panels joined by a reaction arrow needs a curved
    arrow in the first panel: a breaking bond is the source of an arrow that ends at one of its atoms (or at a
    bond or forming-bond gap that includes one), and a forming bond is the two-atom destination of an arrow
    (or joins the atoms of a lone-pair-to-atom arrow).  "summary": true on the reaction arrow skips the step."""
    steps = [r for r in f.resolved.get('reaction_arrows') or [] if r.get('from') and r.get('to')]
    if not steps:
        return []
    mp = lambda ref: ref.rsplit(':', 1)[1]
    info = {pid: _step_bonds(f, pid) for pid in {r[k] for r in steps for k in ('from', 'to')}}
    nxt = collections.defaultdict(list)
    for r in steps:
        nxt[r['from']].append(r)
    out = []
    def twice(pid):        # maps drawn more than once: their atoms cannot be matched between the panels
        panel = next(p for p in f.resolved['panels'] if p['id'] == pid)
        count = collections.Counter(mp(a['ref']) for a in panel['atoms'])
        return sorted((m for m, n in count.items() if n > 1), key=lambda x: int(x) if x.isdigit() else 0)
    for r in steps:
        if r.get('summary') or r['kind'] in ('stoich', 'retro') or info[r['from']][5]:
            continue
        ends = [r['to']] if not info[r['to']][5] else [n['to'] for n in nxt[r['to']] if not n.get('summary')]
        panel, atoms, bonds, coord, aromatic, _ = info[r['from']]
        for end in ends:
            if end not in info:
                info[end] = _step_bonds(f, end)
            _, atoms2, bonds2, coord2, aromatic2, ts2 = info[end]
            if ts2:
                continue
            dup = sorted(set(twice(r['from']) + twice(end)), key=lambda x: int(x) if x.isdigit() else 0)
            if dup:
                out.append('panel %s -> %s: atom map%s %s %s used by more than one species, so the bonds that change in this step '
                           'cannot be matched; give every atom of a panel its own map' % (
                               r['from'], end, 's' if len(dup) > 1 else '', ', '.join(dup), 'are' if len(dup) > 1 else 'is'))
                continue
            arrows = list(panel.get('curved', [])) + (list(info[r['to']][0].get('curved', [])) if end != r['to'] else [])
            uncovered = set()
            for pair in sorted(set(bonds) | set(bonds2), key=lambda q: sorted(int(x) if x.isdigit() else 0 for x in q)):
                if not pair <= atoms & atoms2 or pair in coord or pair in coord2:
                    continue
                o1, o2 = bonds.get(pair, 0), bonds2.get(pair, 0)
                if o1 == o2 or (o1 and o2 and (pair in aromatic and pair in aromatic2 or 1.5 in (o1, o2))):
                    continue
                ok = False
                for c in arrows:
                    fm, tm = {mp(x) for x in c['from']['refs']}, {mp(x) for x in c['to']['refs']}
                    if o2 < o1 and c['from']['kind'] == 'bond' and fm == pair and (
                            (c['to']['kind'] == 'atom' and tm <= pair) or (c['to']['kind'] in ('gap', 'bond') and tm & pair)):
                        ok = True
                    if o2 > o1 and ((c['to']['kind'] in ('gap', 'bond') and tm == pair)
                                    or (c['from']['kind'] == 'atom' and c['to']['kind'] == 'atom' and fm | tm == pair)):
                        ok = True
                if not ok:
                    uncovered |= pair
                    a, b = sorted(pair, key=lambda x: int(x) if x.isdigit() else 0)
                    what = 'forms' if not o1 else 'breaks' if not o2 else 'changes from order %g to %g' % (o1, o2)
                    out.append('panel %s -> %s: bond %s-%s %s, but no curved arrow in panel %s %s it' % (
                        r['from'], end, a, b, what, r['from'], 'draws' if o2 > o1 else 'starts at'))
            out += _step_charges(r['from'], end, arrows, panel['state'], info[end][0]['state'], bonds, bonds2, coord | coord2, uncovered)
    return out


def _step_charges(pid, end, arrows, state, state2, bonds, bonds2, coord, uncovered):
    """Formal charges the two-electron arrows of a step give its atoms, against the next panel: a pair leaving a lone
    pair or a bond raises the charge of its atom(s), a pair arriving as a lone pair or a bond lowers it.  An arrow to
    one atom forms a bond where the next panel has one; otherwise it is a new lone pair."""
    from iupac_features import METALS
    if not arrows or any(c.get('electrons', 2) != 2 for c in arrows):
        return []
    mp = lambda ref: ref.rsplit(':', 1)[1]
    grows = lambda x, y: bonds2.get(frozenset((x, y)), 0) > bonds.get(frozenset((x, y)), 0)
    dq, touched = collections.Counter(), set()
    for c in arrows:
        fm, tm = [mp(x) for x in c['from']['refs']], [mp(x) for x in c['to']['refs']]
        touched.update(fm + tm)
        for x in fm:                                   # the pair leaves: lone pair (-2 e) or bond (-1 bond each end)
            dq[x] += 2 if c['from']['kind'] == 'atom' else 1
        if c['to']['kind'] in ('gap', 'bond'):
            for x in tm:
                dq[x] -= 1
        else:
            b = tm[0]
            partner = next((x for x in fm if x != b and grows(x, b)), None)
            if partner is not None:
                dq[b] -= 1
                dq[partner] -= 1
            else:
                dq[b] -= 2
    out = []
    for x in sorted(touched, key=lambda v: int(v) if v.isdigit() else 0):
        if x not in state or x not in state2:
            continue
        (q1, h1, el, kind), (q2, h2, _, kind2) = state[x], state2[x]
        if kind != 'atom' or kind2 != 'atom' or el in METALS or h1 != h2 or x in uncovered or any(x in pair for pair in coord):
            continue
        if q1 + dq[x] != q2:
            out.append('panel %s -> %s: the curved arrows leave atom %s with charge %+d, but panel %s draws %+d; check '
                       'the direction of the arrows at this atom (for a proton transfer, draw the base\'s lone pair to the H '
                       'and the H-X bond to X)' % (pid, end, x, q1 + dq[x], end, q2))
    return out


def det_arrows_clear(f):
    """A curved arrow keeps more than ARROW_CLEAR_BOND bond lengths of white from ink it does not start from or point at (saved SVG, as
    the overlap checks measure).  The plain bonds, lone pair, charge and letters of its own source and target atoms are not
    counted: a tail sits on its lone pair and a head ends 0.15 bond from a bare vertex, where that atom's bonds meet.  A
    hashed wedge is counted even when it is the arrow's own: it is as wide as its marks, and an arrow along them is
    hard to read."""
    limit = ARROW_CLEAR_BOND * f.bond_pt
    sc = f.scene()
    style = {frozenset((b['a'], b['b'])): b.get('style') for b in f.bonds}
    texts = [(owner, '%s %r' % (kind, text), box_shape(box)) for owner, kind, text, box in f.texts()]
    ink = sc['bonds'] + sc['marks']
    pieces = [(other, s) for other, others in sc['arrows'] for s in others]
    ink_boxes, text_boxes, piece_boxes = _boxes(ink), _boxes([tb for _, _, tb in texts]), _boxes([s for _, s in pieces])
    out = []
    for aid, shapes in sc['arrows']:
        arrow = next((c for c in f.curved if c['id'] == aid), None)
        if arrow is None:
            continue
        own = set(arrow['from']['refs'] + arrow['to']['refs'])
        near = (99.0, '')
        box = _union_bbox(shapes)
        def look(s, what):
            if _far(box, s.bbox, limit):
                return near                             # no shape within the limit: (99.0, what) is not below near
            gap = min((shape_clear(a, s) for a in shapes if co.bbox_gap(a.bbox, s.bbox) <= limit), default=99.0)
            return min(near, (gap, what))
        # look() keeps near for every ink, text and arrow _far from the arrow: only those near its bbox are looked at
        for s in (ink[i] for i in (_near(ink_boxes, box, limit) if box else ())):
            kind, key = s.owner or (None, None)
            hashed = kind == 'bond' and style.get(frozenset(key)) == 'hash'
            if hashed or not (own.intersection(key) if kind in ('bond', 'hbond') else key in own):
                near = look(s, ('hashed ' if hashed else '') + _ink_name(s))
        for owner, what, tb in (texts[i] for i in (_near(text_boxes, box, limit) if box else ())):
            if owner not in own:
                near = look(tb, what)
        for other, s in (pieces[i] for i in (_near(piece_boxes, box, limit) if box else ())):
            if other != aid:
                near = look(s, 'arrow %s' % other)
        if near[0] <= limit:
            out.append('arrow %s: %.2f pt from %s, which reads as touching it' % (aid, near[0], near[1]))
    return out


def det_isotopes(f):
    out = []
    for ref, a in f.atoms.items():
        isotope = int(a.get('isotope') or 0)
        for when, saved in (('native', f.reload), ('CDXML', f.reload.get('cdxml'))):
            if saved and int(saved['atoms'].get(ref, {}).get('isotope') or 0) != isotope:
                out.append('%s: %s isotope differs from mass %d' % (ref, when, isotope))
        if isotope:
            pieces = f.report['pieces'].get(ref, [])
            mass = [p for p in pieces if p['text'] == str(isotope)]
            symbol = [p for p in pieces if p['text'] == a['el']]
            if not mass or not symbol or mass[0]['box'][0] >= symbol[0]['box'][0] or mass[0]['box'][1] >= symbol[0]['box'][1]:
                out.append('%s: isotope mass must be a visible superscript left of its symbol' % ref)
    return out


def det_hydrogen_display(f):
    out = []
    touched = {r for c in f.curved for end in ('from', 'to') for r in c[end]['refs']} | {r for pair in f.hbonds for r in pair}
    for ref, a in f.atoms.items():
        if a['el'] != 'H':
            continue
        hidden = bool(a.get('hidden_h'))
        for when, saved in (('native', f.reload), ('CDXML', f.reload.get('cdxml'))):
            if saved and bool(saved['atoms'].get(ref, {}).get('hidden_h')) != hidden:
                out.append('%s: %s hydrogen display differs' % (ref, when))
        if hidden and (ref in touched or f.label_box(ref)):
            out.append('%s: a reacting H was hidden or a folded H still has a label' % ref)
        if not hidden and not f.label_box(ref):
            out.append('%s: the required explicit H is not visible' % ref)
    return out


def det_coordination(f):
    from iupac_features import roman
    out = []
    for panel in f.resolved['panels']:
        for saved in (f.reload, f.reload.get('cdxml')):
            if not saved:
                continue
            for pair in panel.get('pseudobonds', []):
                if tuple(sorted(pair)) not in {tuple(sorted(h)) for h in saved['hbonds']}:
                    out.append('pseudobond %s must be a distinct dotted line' % '-'.join(pair))
            for eta in panel.get('eta', []):
                if eta.get('path') and not any(c['center'] == eta['center'] and c['kind'] in ('curve', 'cut') and c['points'] >= 2 and c['head'] == c['tail'] == 'none' for c in saved['delocalized']):
                    out.append('%s: open haptic ligand lost its headless delocalization curve' % eta['center'])
                if eta.get('delocalized'):
                    edges = [b for pair, b in saved['bonds'].items() if all(ref in eta['atoms'] for ref in pair)]
                    if not edges or any(b['order'] != 1.5 for b in edges):
                        out.append('%s: a delocalized haptic ligand must use a curve, not alternating double bonds' % eta['center'])
    for b in f.bonds:
        if b.get('style') != 'coord' and not b.get('coord'):
            continue
        key = tuple(sorted((b['a'], b['b'])))
        for when, saved in (('native', f.reload), ('CDXML', f.reload.get('cdxml'))):
            got = saved['bonds'].get(key) if saved else None
            style = 'plain' if b.get('style') == 'coord' else b.get('style', 'plain')
            if got is None or got['style'] != style or got['order'] != 1 or style not in ('plain', 'wedge', 'hash'):
                out.append('%s: %s coordination bond must keep its plain line or specified stereobond' % ('-'.join(key), when))
        for ref in key:
            a = f.atoms[ref]
            if a.get('preserve_h') and (a.get('display_h', a.get('h')) or 0):
                pieces = f.report['pieces'].get(ref, [])
                if not any(p['text'] == 'H' for p in pieces):
                    out.append('%s: hydrogens of the coordinated atom are not shown' % ref)
    for ref, a in f.atoms.items():
        if a.get('center'):
            for saved in (f.reload, f.reload.get('cdxml')):
                if saved and sorted(saved['atoms'].get(ref, {}).get('center') or []) != sorted(a['center']):
                    out.append('%s: multicenter attachment lost its ligand atoms' % ref)
        if a.get('oxidation') is not None and not any(t['role'] == 'oxidation' and t['text'] == roman(a['oxidation']) for t in f.report['texts']):
            out.append('%s: oxidation state is not drawn as a roman numeral' % ref)
    return out


def det_metal_geometry(f):
    from iupac_features import GEOMETRIES
    out = []
    for panel in f.resolved['panels']:
        for g in panel.get('metal_geometry', []):
            p = f.xy(g['center'])
            directions = [angle_of(p, f.xy(ref)) for ref in g['ligands']]
            expected = [d for d, _ in GEOMETRIES[g['kind']]]
            if g['kind'] in ('square_planar', 'square_pyramidal4', 'see_saw'):
                ordered = sorted(zip(directions, g['ligands']))
                for i, (degree, ref) in enumerate(ordered):
                    other, far = ordered[(i + 1) % len(ordered)]
                    styles = [next((b.get('style', 'plain') for b in f.bonds if set((b['a'], b['b'])) == set((g['center'], end))), 'plain') for end in (ref, far)]
                    if (other - degree) % 360 >= 180 and styles[0] != styles[1]:
                        out.append('%s: forbidden adjacent metal bonds span 180 degrees with different bond types' % g['center'])
            # Translation, rotation and scale do not change the template's angular separations.
            for i in range(len(directions)):
                for j in range(i):
                    if abs(adiff(directions[i], directions[j]) - adiff(expected[i], expected[j])) > 2:
                        out.append('%s: angles do not match %s' % (g['center'], g['kind']))
                        break
    return list(dict.fromkeys(out))


def _side_chain_piece(f, ref):
    """{atom: bonds from the residue atom ref} of what is drawn beyond a residue name, up to 7 bonds: not through a
    metal, a coordination bond, a hydrogen or another residue or cut end."""
    from iupac_features import METALS
    coord = {frozenset((b['a'], b['b'])) for b in f.bonds if b.get('coord') or b.get('style') in ('coord', 'dative')}
    depth, todo = {ref: 0}, [ref]
    while todo:
        r = todo.pop(0)
        if depth[r] >= 7:
            continue
        for n in f.nbrs.get(r, ()):
            a = f.atoms[n]
            if n in depth or frozenset((r, n)) in coord or a['el'] in METALS or a['el'] == 'H' or a.get('kind') in ('residue', 'cut'):
                continue
            depth[n] = depth[r] + 1
            todo.append(n)
    del depth[ref]
    return depth


def _small_rings(f, atoms):
    """The 5- and 6-membered rings among atoms (frozensets of refs)."""
    rings = set()
    for a in atoms:
        for b in f.nbrs.get(a, ()):
            if b not in atoms or b < a:
                continue
            paths = [[b]]                               # shortest ways back from b to a without the bond a-b
            for _ in range(5):
                nxt = []
                for path in paths:
                    for n in f.nbrs.get(path[-1], ()):
                        if n == a and len(path) >= 4:
                            rings.add(frozenset(path + [a]))
                        elif n in atoms and n not in path and n != a:
                            nxt.append(path + [n])
                paths = nxt
    return {r for r in rings if len(r) in (5, 6)}


def _side_chain_ok(f, code, piece):
    """True when the drawn piece beyond a residue name carries that residue's own side-chain group (None: not checked)."""
    el = lambda r: f.atoms[r]['el']
    within = lambda k: {r for r, d in piece.items() if d <= k}
    def acid(k, partner):                        # a C bonded to an O and to `partner` (O: carboxyl, N: amide)
        return any(el(r) == 'C' and any(el(n) == 'O' for n in f.nbrs[r])
                   and sum(1 for n in f.nbrs[r] if el(n) in ('O', partner)) >= 2 for r in within(k))
    rings = _small_rings(f, within(7)) if code in ('His', 'Trp', 'Phe', 'Tyr') else set()
    if code == 'His':          # imidazole: its two N apart (not a pyrazole), joined to the chain at C4 (beside one N only)
        for r in rings:
            if len(r) == 5 and sum(el(x) == 'N' for x in r) == 2 and min(piece[x] for x in r) <= 3 \
                    and not any(el(x) == 'N' and el(n) == 'N' for x in r for n in f.nbrs[x] if n in r):
                first = [x for x in r if piece[x] == min(piece[y] for y in r)]
                if all(el(x) == 'C' and sum(1 for n in f.nbrs[x] if n in r and el(n) == 'N') == 1 for x in first):
                    return True
        return False
    if code == 'Trp':
        return any(len(r) == 5 and sum(el(x) == 'N' for x in r) == 1 and any(len(r & q) == 2 and len(q) == 6 for q in rings)
                   for r in rings)
    if code in ('Phe', 'Tyr'):
        six = [r for r in rings if len(r) == 6 and all(el(x) == 'C' for x in r) and min(piece[x] for x in r) <= 3]
        return any(any(el(n) == 'O' and n not in r for x in r for n in f.nbrs[x]) for r in six) if code == 'Tyr' else bool(six)
    if code in ('Ser', 'Thr', 'Cys', 'Sec'):
        return any(el(r) == {'Cys': 'S', 'Sec': 'Se'}.get(code, 'O') for r in within(2))
    if code == 'Met':
        return any(el(r) == 'S' for r in within(3))
    if code == 'Lys':
        return any(el(r) == 'N' for r in within(5))
    if code == 'Arg':
        return any(el(r) == 'C' and sum(1 for n in f.nbrs[r] if el(n) == 'N') >= 3 for r in within(5))
    if code in ('Asp', 'Glu'):
        return acid(2 if code == 'Asp' else 3, 'O')
    if code in ('Asn', 'Gln'):
        return acid(2 if code == 'Asn' else 3, 'N')
    return None


SIDE_CHAIN_NAMES = {'His': 'CH2 on C4 of an imidazole ring', 'Trp': 'CH2 and an indole', 'Phe': 'CH2 and a phenyl ring',
                    'Tyr': 'CH2 and a 4-hydroxyphenyl ring', 'Ser': 'CH2OH', 'Thr': 'CH(OH)CH3', 'Cys': 'CH2SH',
                    'Sec': 'CH2SeH', 'Met': 'CH2CH2SCH3', 'Lys': '(CH2)4NH2', 'Arg': '(CH2)3 and a guanidine',
                    'Asp': 'CH2COO', 'Glu': 'CH2CH2COO', 'Asn': 'CH2CONH2', 'Gln': 'CH2CH2CONH2'}


def det_residue_codes(f):
    styles = set()
    out, seen = [], set()
    for ref, a in f.atoms.items():
        if a.get('kind') != 'residue':
            continue
        label = a.get('label') or ''
        for match in re.finditer(r'(?<![A-Za-z])([A-Z][a-z]{2}|[A-Z])[- ]?\d+', label):
            styles.add('three-letter' if len(match.group(1)) == 3 else 'one-letter')
        # what is drawn beyond a residue name is that residue's own side chain (a main-chain piece is not checked)
        m = RESIDUE_NAME.search(label)
        if not m or m.group(1) not in SIDE_CHAIN_NAMES:
            continue
        if any(f.atoms[n]['el'] == 'N' or (f.atoms[n]['el'] == 'C' and any(
                f.atoms[o]['el'] == 'O' and n in (b['a'], b['b']) and o in (b['a'], b['b']) and float(b['order']) == 2
                for b in f.bonds for o in f.nbrs.get(n, ()))) for n in f.nbrs.get(ref, ())):
            continue
        if (label, ref.split('.', 1)[-1]) in seen:     # once for the same residue in every panel
            continue
        seen.add((label, ref.split('.', 1)[-1]))
        piece = _side_chain_piece(f, ref)
        if len(piece) >= 2 and _side_chain_ok(f, m.group(1), piece) is False:
            out.append('residue %s at %s: the atoms drawn beyond it are not %s\'s side chain (%s)' % (
                label, ref, m.group(1), SIDE_CHAIN_NAMES[m.group(1)]))
    if len(styles) > 1:
        out.insert(0, 'mixed one-letter and three-letter residue codes in one figure')
    return out


RESIDUE_NAME = re.compile(r'(?<![A-Za-z])([A-Z][a-z]{2})[- ]?(\d+)(?![\d])')


def _residue_marks(f):
    """{panel: [(name, code, number, species, backbone)]}: the residue names a panel shows, as residue labels (backbone
    when the dummy root is bonded to an N or a carbonyl C, i.e. the piece is main chain) and as texts naming a
    three-letter residue (the species of the atom the text is put near; backbone when that species is cut)."""
    out = collections.defaultdict(list)
    def carbonyl(r):
        return f.atoms[r]['el'] == 'C' and any(
            r in (b['a'], b['b']) and f.atoms[b['b'] if b['a'] == r else b['a']]['el'] == 'O' and float(b['order']) == 2
            for b in f.bonds)
    for ref, a in f.atoms.items():
        if a.get('kind') != 'residue' or not a.get('label'):
            continue
        m = RESIDUE_NAME.search(a['label']) or re.search(r'(?<![A-Za-z])([A-Z])[- ]?(\d+)(?![\d])', a['label'])
        backbone = any(f.atoms[n]['el'] == 'N' or carbonyl(n) for n in f.nbrs.get(ref, ()))
        out[ref.split('.', 1)[0]].append((a['label'], m.group(1) if m else None, int(m.group(2)) if m else None,
                                          ref.rsplit(':', 1)[0], backbone))
    for p in f.resolved['panels']:
        for t in p.get('texts', []):
            species = (t.get('near') or '').rsplit(':', 1)[0] or None
            cut = species is not None and any(r.rsplit(':', 1)[0] == species and a.get('kind') == 'cut' for r, a in f.atoms.items())
            for m in RESIDUE_NAME.finditer(t.get('text') or ''):
                out[p['id']].append((m.group(0), m.group(1), int(m.group(2)), species, cut))
    return out


def det_residue_once(f):
    out = []
    for pid, marks in sorted(_residue_marks(f).items()):
        count = collections.Counter(name for name, *_ in marks)
        for name, n in sorted(count.items()):
            if n > 1:
                where = sorted({s or 'text' for nm, _, _, s, _ in marks if nm == name})
                out.append('panel %s: residue %s is shown %d times (%s); show it once' % (pid, name, n, ', '.join(where)))
        pieces = [(c, k, s, nm) for nm, c, k, s, bb in marks if bb and k is not None]
        for (c1, k1, s1, n1), (c2, k2, s2, n2) in itertools.combinations(sorted(set(pieces), key=lambda x: x[1]), 2):
            if abs(k1 - k2) == 1 and s1 != s2 and (len(c1) == 3) == (len(c2) == 3):
                out.append('panel %s: consecutive residues %s and %s are drawn with their backbone as separate pieces '
                           '(%s, %s); join them into one species' % (pid, n1, n2, s1, s2))
    return out


def det_reaction_kinds(f):
    from iupac_features import REACTION_KINDS
    out = []
    for r in f.resolved['reaction_arrows']:
        if r.get('kind', 'single') not in REACTION_KINDS:
            out.append('unknown reaction-arrow kind %s' % r.get('kind'))
    for when, saved in (('native', f.reload), ('CDXML', f.reload.get('cdxml'))):
        if saved:
            out += _arrow_counts(f, saved, when)
            for arrow in saved['arrows']:
                if arrow['kind'] not in ('both', 'stoich'):
                    continue
                segments = arrow.get('segments') or []
                kind = 'reaction' if arrow['kind'] == 'both' else 'line'
                head = 'full' if arrow['kind'] == 'both' else 'none'
                if len(segments) != 2 or any(s['kind'] != kind or s.get('head') != head or s.get('tail') != 'none' for s in segments):
                    out.append('%s: %s needs two %s segments with %s heads' % (when, arrow['kind'], kind, head))
                elif arrow['kind'] == 'both':
                    a, b = segments
                    dot = (a['x2'] - a['x1']) * (b['x2'] - b['x1']) + (a['y2'] - a['y1']) * (b['y2'] - b['y1'])
                    if dot >= 0:
                        out.append('%s: both reaction arrows must point in opposite directions' % when)
    return out


def det_radical_direction(f):
    out = []
    measured = {a['id']: a for a in f.report['curved']}
    for arrow in f.curved:
        if arrow['electrons'] != 1 or arrow['from']['kind'] != 'atom':
            continue
        ref = arrow['from']['refs'][0]
        marks = [m for m in f.report['radicals'] if m.get('ref') == ref and m.get('center_pt')]
        row = measured.get(arrow['id'], {})
        if not marks or not row.get('start'):
            out.append('%s: fishhook tail has no measured unpaired-electron dot' % arrow['id'])
            continue
        dot = min((m['center_pt'] for m in marks), key=lambda p: math.dist(p, row['start']))
        target = tuple(sum(f.xy(r)[k] for r in arrow['to']['refs']) / len(arrow['to']['refs']) for k in (0, 1))
        if adiff(angle_of(f.xy(ref), dot), angle_of(f.xy(ref), target)) > 90:
            out.append('%s: reacting unpaired-electron dot faces away from its target' % arrow['id'])
        if math.dist(dot, row['start']) > LONE_PAIR_TAIL_MAX_PT:
            out.append('%s: fishhook tail is off its drawn unpaired-electron dot' % arrow['id'])
    return out


def det_figure_style(f):
    out, choices = [], f.resolved.get('drawing', {'charge_circles': True, 'partial_bonds': 'dashed'})
    kinds = set()
    for c in f.report['charges']:
        a = f.atoms.get(c['ref'], {})
        if abs(a.get('charge', 0)) == 1 and not a.get('ion_text') and not a.get('radical') and a.get('kind', 'atom') == 'atom':
            kinds.add(c['kind'])
    want = 'circle' if choices.get('charge_circles', True) else 'text'
    if len(kinds) > 1:
        out.append('mixed circled and uncircled structure charges in one figure')
    elif kinds and kinds != {want}:
        out.append('structure charge style differs from figure-wide drawing.charge_circles')
    for b in f.bonds:
        if b.get('style') not in ('partial', '1.5'):
            continue
        rows = _styled_ink(f, b)
        marks = rows if b['style'] == 'partial' else [r for r in rows if abs(r[2]) >= .3]
        if not marks:
            continue  # the bond detector reports the missing ink
        dotted = all(abs(r[1] - r[0] - r[3]) <= .15 for r in marks)
        if dotted != (choices.get('partial_bonds', 'dashed') == 'dotted'):
            out.append('%s-%s: partial-line style differs from figure-wide drawing.partial_bonds' % (b['a'], b['b']))
    return out


def det_species_gap(f):
    """Two species of a panel closer than half a bond length of white; the ends of a hydrogen bond are exempt."""
    limit = 0.5 * f.bond_pt
    groups = collections.defaultdict(list)
    def add(ref, shape):
        if ref in f.atoms:
            groups[f.atoms[ref]['species']].append((ref, shape))
    for ref, a in f.atoms.items():
        if not a.get('hidden_h'):
            parts = f.label_parts(ref)
            for box in parts:
                add(ref, box_shape(box))
            if not parts:
                add(ref, disk_shape(f.xy(ref), float(f.style['line_width_pt']) / 2))
    for c in f.report['charges']:
        if c['kind'] == 'circle' and c['center']:
            add(c['ref'], disk_shape(c['center'], c['outer_d'] / 2))
    for pair in f.report['lone_pairs']:
        for dot in pair['dots']:
            add(pair['ref'], disk_shape(dot, pair['radius']))
    for shape in f.scene()['bonds']:
        if shape.owner and shape.owner[0] == 'bond':
            add(shape.owner[1][0], shape)
    exempt = {frozenset(h) for h in f.hbonds}
    boxes = {sp: _boxes([shape for _, shape in group]) for sp, group in groups.items()}
    out = []
    for panel in f.resolved['panels']:
        species = list(dict.fromkeys(a['species'] for a in panel['atoms'] if not a.get('hidden_h')))
        for i, sa in enumerate(species):
            for sb in species[i + 1:]:
                gap, pair = limit, None
                # gap never exceeds limit: only the pairs within limit along x and y can pass bbox_gap < gap
                near = _near_pairs(boxes.get(sa, _boxes([])), boxes.get(sb, _boxes([])), limit)
                for (ra, a), cands in zip(groups[sa], near):
                    for rb, b in (groups[sb][k] for k in cands):
                        if frozenset((ra, rb)) not in exempt and co.bbox_gap(a.bbox, b.bbox) < gap:
                            g = shape_clear(a, b)
                            if g < gap:
                                gap, pair = g, (ra, rb)
                if pair:
                    out.append('%s and %s are %.1f pt apart (under half a bond, %.1f pt)' % (pair[0], pair[1], gap, limit))
    return out


def det_plus_place(f):
    """A "+" text that does not stand in the gap between two species of one panel, at a height each of them reaches
    (within half a bond length)."""
    reach = 0.5 * f.bond_pt
    boxes = collections.defaultdict(list)
    for ref, a in f.atoms.items():
        if not a.get('hidden_h'):
            x, y = f.xy(ref)
            boxes[a['species']] += f.label_parts(ref) or [(x, y, x, y)]
    for shape in f.scene()['bonds']:
        if shape.owner and shape.owner[0] == 'bond' and shape.owner[1][0] in f.atoms:
            boxes[f.atoms[shape.owner[1][0]]['species']].append(shape.bbox)
    span = {sp: (min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs)) for sp, bs in boxes.items()}
    out = []
    for t in f.report['texts']:
        if t['text'] != '+' or not t['box'] or t.get('role') not in (None, 'text'):
            continue
        x0, y0, x1, y1 = t['box']
        cy = (y0 + y1) / 2
        if not any(sa.split('.', 1)[0] == sb.split('.', 1)[0] and a[2] <= x0 and b[0] >= x1 and a[1] - reach <= cy <= a[3] + reach
                   and b[1] - reach <= cy <= b[3] + reach for sa, a in span.items() for sb, b in span.items() if sa != sb):
            out.append('"+" at (%.1f, %.1f) pt does not stand between two species of its panel at a height both reach'
                       % ((x0 + x1) / 2, cy))
    return out


def det_bond_crossing(f):
    """Two bonds of a panel that cross, a hydrogen bond that crosses a bond or another hydrogen bond, or two unbonded
    atoms closer than 0.4 bond length. Transition states (drawn in perspective) and eta centers are exempt."""
    def free(ref):
        a = f.atoms[ref]
        return not a.get('ts') and not a.get('hidden_h') and a.get('kind') != 'center'
    def panel(ref):
        return ref.split('.', 1)[0]
    bonds = [b for b in f.bonds if b['a'] in f.atoms and b['b'] in f.atoms and free(b['a']) and free(b['b'])]
    lines = [h for h in f.hbonds if all(r in f.atoms for r in h)]
    bond_boxes = _boxes([_seg_box(f.xy(b['a']), f.xy(b['b'])) for b in bonds])
    line_boxes = _boxes([_seg_box(f.xy(h[0]), f.xy(h[1])) for h in lines])
    # two segments are no closer than their boxes: only the pairs whose boxes are under 0.5 pt apart are measured
    out = []
    for i, (b, cands) in enumerate(zip(bonds, _near_pairs(bond_boxes, bond_boxes, 0.5))):
        for c in (bonds[k] for k in cands if k > i):
            if panel(b['a']) == panel(c['a']) and not {b['a'], b['b']} & {c['a'], c['b']} and \
                    co.dist_seg_seg(f.xy(b['a']), f.xy(b['b']), f.xy(c['a']), f.xy(c['b'])) < 0.5:
                out.append('bonds %s-%s and %s-%s cross' % (b['a'], b['b'], c['a'], c['b']))
    for h, cands in zip(lines, _near_pairs(line_boxes, bond_boxes, 0.5)):
        for c in (bonds[k] for k in cands):
            if panel(c['a']) == panel(h[0]) and not set(h) & {c['a'], c['b']} and \
                    co.dist_seg_seg(f.xy(h[0]), f.xy(h[1]), f.xy(c['a']), f.xy(c['b'])) < 0.5:
                out.append('hbond %s-%s crosses bond %s-%s' % (h[0], h[1], c['a'], c['b']))
    for i, h in enumerate(lines):
        for g in lines[i + 1:]:
            if panel(g[0]) == panel(h[0]) and not set(h) & set(g) and \
                    co.dist_seg_seg(f.xy(h[0]), f.xy(h[1]), f.xy(g[0]), f.xy(g[1])) < 0.5:
                out.append('hbond %s-%s crosses hbond %s-%s' % (h[0], h[1], g[0], g[1]))
    bonded = {frozenset((b['a'], b['b'])) for b in f.bonds}
    refs = [r for r in f.atoms if free(r)]
    limit = 0.4 * f.bond_pt
    points = _boxes([(*f.xy(r), *f.xy(r)) for r in refs])
    for i, (ra, cands) in enumerate(zip(refs, _near_pairs(points, points, limit))):
        for rb in (refs[k] for k in cands if k > i):
            if panel(ra) == panel(rb) and frozenset((ra, rb)) not in bonded and math.dist(f.xy(ra), f.xy(rb)) < limit:
                out.append('%s and %s are %.1f pt apart (under 0.4 bond, %.1f pt)' % (ra, rb, math.dist(f.xy(ra), f.xy(rb)), limit))
    return out


DETECTOR_FUNCS = {name: globals()['det_' + name] for name in DETECTORS}


def _spec_edit(f, refs):
    """One spec edit that gives the named atoms room: shift the species placed later in the panel one bond length away
    from the other one or, when one species is named, turn it where it first appears."""
    sids = list(dict.fromkeys(f.atoms[r]['species'] for r in refs))
    if not sids:
        return 'move the neighboring species one bond length away with offset'
    panel = next(p for p in f.resolved['panels'] if any(a['species'] == sids[0] for a in p['atoms']))
    order = list(dict.fromkeys(a['species'] for a in panel['atoms']))
    sids = sorted((x for x in sids if x in order), key=order.index)
    key = sids[-1][len(panel['id']) + 1:]
    if len(sids) == 1:
        if sum(a['species'] == sids[0] for a in panel['atoms']) == 1:
            return 'move species %s in panel %s with offset' % (key, panel['id'])
        first = next(p['id'] for p in f.resolved['panels'] if any(a['species'] == '%s.%s' % (p['id'], key) for a in p['atoms']))
        return ('set "rotate": 30 on species %s in panel %s (then -30; if both fail, abbreviate a non-reacting group beside '
                'these atoms or split the step into two panels)' % (key, first))
    (x0, y0), (x1, y1) = ([sum(a[k] for a in panel['atoms'] if a['species'] == x) / sum(a['species'] == x for a in panel['atoms'])
                           for k in ('x', 'y')] for x in (sids[0], sids[-1]))
    step = [1 if x1 >= x0 else -1, 0] if abs(x1 - x0) >= abs(y1 - y0) else [0, 1 if y1 >= y0 else -1]
    return 'add %s to "offset" of species %s in panel %s' % (json.dumps(step), key, panel['id'])


def _arrow_in_own_species(f, finding):
    """True when an arrow-bond overlap lies inside one species: the arrow's atoms and the bond all belong to it."""
    m = re.match(r'arrow (\S+) touches bond (\S+)-(\S+) ', finding)
    if not m:
        return False
    bond = [m.group(2), m.group(3)]
    panel = bond[0].split('.', 1)[0] + '.'
    arrow = next((c for c in f.curved if c['id'] == m.group(1)
                  and any(r.startswith(panel) for k in ('from', 'to') for r in c[k]['refs'])), None)
    if arrow is None:
        return False
    refs = [r for k in ('from', 'to') for r in arrow[k]['refs']] + bond
    return all(r in f.atoms for r in refs) and len({f.atoms[r]['species'] for r in refs}) == 1


def repair_hints(f, name, findings):
    """Translate detector findings back to spec locations, with one concrete edit per finding ({move}: _spec_edit)."""
    no_fix = ('the input cannot fix this (a drawing or saving defect): '
              'abbreviate a non-reacting group or split the step into two panels')
    actions = {
        'partial_dashes': 'restore the partial bond style; keep at least three round dots and separate adjacent labels',
        'radical_marks': 'set the atom radicals count and keep each dot after its complete label',
        'electron_flow': 'start fishhooks at drawn unpaired electrons or supply both electrons of a bond; point new bonds at a two-atom gap',
        'isotopes': 'keep the isotope mass in bracket SMILES and redraw its superscript',
        'hydrogen_display': 'keep reacting and stereochemical H explicit; fold only nonreacting H into its neighbor',
        'coordination': 'use coord plain bonds, explicit ligand H, and the correct eta attachment atoms',
        'metal_geometry': 'use geometry with ligand order matching the specified coordination template',
        'residue_codes': 'use one residue code style in the whole figure (three-letter by default)',
        'residue_once': 'show each residue once per panel: draw a residue whose backbone takes part, with its side chain if that takes part too, and consecutive residues drawn with their backbone, as one species; end the backbone with dummy atoms listed in "cut" (wavy truncation marks) and name each residue with a text near its alpha carbon; a residue whose side chain alone takes part keeps its "residue" label',
        'reaction_kinds': 'choose single, equilibrium, resonance, both, stoich or retro and redraw',
        'delocalized_bonds': 'restore the 1.5 bond style; keep the dashed line parallel and inside the ring',
        'ts_brackets': 'use ts: true and leave room around the automatic brackets and upper-right double dagger',
        'arrows_ends': 'check this arrow\'s from/to maps; then {move} so that the requested atom/bond is nearest to the endpoint',
        'arrows_lone_pair': 'put the reacting atom in lone_pairs and redraw this arrow from that pair; for an atom without a lone pair, start the arrow at a bond',
        'charge_own_atom': 'keep the charge on this atom; draw a blocking implicit H explicitly as [H:n] with a fresh map, or {move}',
        'chain_angles': 'remove xy overrides on this chain segment to restore 120-degree angles, or move its connected branch as a unit',
        'bond_crossing': 'remove xy overrides on these atoms so the automatic layout places the branch, or give the crowded branch its own side with xy on its root atom; bonds and hydrogen bonds must not cross (for a hydrogen bond, run scripts/fix_hbonds.py first)',
        'four_bond': 'remove xy overrides on this atom and its neighbors so the two chain bonds are 120 degrees apart and the other two 60 degrees apart',
        'overlap_text_text': 'move a named free text with xy/near; for atom labels {move}; keep the required atom labels and numbers',
        'overlap_text_bond': 'move a named free text with xy/near; for an atom label {move}',
        'number_place': 'keep the requested number in numbers; {move} to leave room beside its own atom',
        'number_clear': 'keep the number in numbers; {move} to give its glyphs clearance from the named ink',
        'bond_lengths': 'remove the xy override at the named bond ends, or move the whole branch so that this bond keeps one standard bond length',
        'label_bonds': 'remove the xy override that stretches or shortens this bond and redraw; the scripts lengthen a two-label bond themselves where its line would be a dot, a curved arrow uses it or a charge sits squeezed between the labels',
        'one_label_bonds': 'restore one standard bond length between these atoms; remove conflicting xy overrides and redraw',
        'graph': 'check the named species\' SMILES and bond_style; if they are right, ' + no_fix,
        'cdxml': 'check this species\' abbrev/residue element and bond_style; if they are right, ' + no_fix,
        'stereochemistry': 'remove any bond_style override that contradicts the SMILES; if the STEREO lines agree with the source, ' + no_fix,
        'frame': 'run --check-env with the same --browser-cmd; install Arial or Liberation Sans if its font-width check fails, then redraw',
        'arrows_shape': '{move} to allow a short arc between the arrow\'s from/to maps',
        'column_arrows': no_fix,
        'arrows_count': no_fix,
        'arrows_head': 'set this arrow\'s electrons to the intended 1 or 2 and redraw',
        'arrows_steps': ('add the missing curved arrow in the named panel: from the breaking bond ("species:a-b") to the atom that keeps '
                         'its electrons, and to a forming bond as a two-atom "to"; if the source itself draws this step without '
                         'curved arrows, set "summary": true on that reaction arrow'),
        'arrows_color': no_fix,
        'number_on_h': 'keep the required number on its heavy atom in numbers; express a moving H as a mapped atom without a number',
        'number_size': no_fix,
        'charges': 'correct the named atom\'s bracket charge in SMILES and redraw',
        'hydrogens': 'correct the bracket H count or explicitly map a moving H in SMILES, then redraw',
        'labels_drawn': 'add the required carbon map to this species\' show list, keep its label, and redraw',
        'colors': 'check this atom or bond in colors and redraw',
        'charge_circled': 'write the single charge on its actual atom in SMILES and redraw with the default circle',
        'charge_size': no_fix,
        'ion_text': 'write the monatomic ion or multiple charge in its SMILES and redraw as superscript charge text',
        'overlap_charge': '{move} so the default charge circle clears the named ink',
        'overlap_arrow_text': 'move a named free text with xy/near, or {move}',
        'overlap_arrow_bond': '{move} so the arrow\'s route clears the named bond',
        'overlap_arrow_arrow': '{move} to leave distinct routes',
        'charge_open': '{move} so the charge has an open side',
        'lone_pair_geometry': '{move} so a seat above, below, left or right of the atom is free',
        'carbon_labels': 'add this carbon\'s map to the species\' show list',
        'stereo_display': 'follow the finding: write the named H as [H:n] with a fresh map, or remove the bond_style override at this center so that the scripts choose the wedges',
        'figure_style': 'set drawing.charge_circles and drawing.partial_bonds once for the whole figure and remove label or bond_style entries that draw another style',
    }
    hints = []
    for finding in findings:
        refs = [r for r in f.atoms if re.search(r'(?<![\w.])' + re.escape(r) + r'(?!\d)', finding)]
        refs.sort(key=finding.index)
        places, named = [], []
        for ref in refs:
            sid, mp = ref.rsplit(':', 1)
            panel = next((p['id'] for p in f.resolved['panels'] if any(a['ref'] == ref for a in p['atoms'])), '?')
            key = sid[len(panel) + 1:] if sid.startswith(panel + '.') else sid
            places.append('panel %s, species %s, atom %s (%s)' % (panel, key, mp, f.atoms[ref]['el']))
        for panel in f.resolved['panels']:
            for arrow in panel['curved']:
                if re.search(r'(?<![\w-])' + re.escape(arrow['id']) + r'(?![\w-])', finding):
                    places.append('panel %s, arrow %s' % (panel['id'], arrow['id']))
                    named += arrow['from']['refs'] + arrow['to']['refs']
            for i, text in enumerate(panel.get('texts', [])):
                if repr(text['text']) in finding:
                    places.append('panel %s, texts[%d]' % (panel['id'], i))
            title = panel.get('title')
            if title and repr(title['text']) in finding:
                places.append('panel %s, title' % panel['id'])
        if not places:
            places = ['panels %s (figure-wide %s)' % (', '.join(p['id'] for p in f.resolved['panels']), name)]
        if name == 'arrows_ends' and 'no route found' in finding:
            places = [place for place in places if ', arrow ' in place]
        action = ('remove this arrow\'s manual bend and any plus along its path; bring its ends closer with near between species, '
                  'or move the receiving branch with xy within one species' if name == 'arrows_ends' and 'no route found' in finding else
                  'pair each H with the acceptor on its own side (swap the two acceptors when they are equivalent, such as the '
                  'two O of a carboxylate), or turn the donor group with xy so that the lines do not cross'
                  if ' crosses hbond ' in finding else
                  'run `python scripts/fix_hbonds.py INPUT --out NEW_DIR` once; if the line still overlaps, follow the hydrogen-bond '
                  'note under "Hand placement" in scripts/README.md' if 'hbond ' in finding else
                  'first add the missing H to the bracket atom in the SMILES; only when the source draws an unpaired electron on '
                  'this atom, list it in the species\' radicals' if 'not from radicals' in finding else
                  'the arrow and the bond are in one species, so turning that species keeps the overlap: change this arrow\'s '
                  '"bend" (the opposite sign, or a larger value up to 1.6); if it still touches, move the bond\'s branch with xy, '
                  'abbreviate a non-reacting group beside these atoms, or split the step into two panels'
                  if name == 'overlap_arrow_bond' and _arrow_in_own_species(f, finding) else
                  'give the named text a "near" direction away from the other name, or move the molecule of a residue '
                  'name with offset' if name == 'overlap_text_text' and finding.startswith('names ') else
                  'draw the side chain the residue name stands for in the SMILES between the name and the reacting '
                  'atom' if name == 'residue_codes' and 'side chain' in finding else actions.get(name, no_fix))
        action = action.replace('{move}', _spec_edit(f, refs or named))
        hints.append('%s: %s; %s. Action: %s.' % (name, '; '.join(dict.fromkeys(places)), finding, action))
    return hints


INPUT_CHECKS = ('arrows_steps', 'residue_codes', 'residue_once', 'radical_marks', 'electron_flow')


def run_checks(f, input_only=False):
    """{'checks': {name: {'ok', 'findings'}}, 'ok', 'failed': [names], 'tables': {...}}."""
    res = {}
    applicable = applicable_detectors(f)
    if input_only:
        applicable = [n for n in applicable if n in INPUT_CHECKS]
    for name in DETECTORS:
        if name not in applicable:
            res[name] = {'ok': True, 'findings': [], 'warnings': [], 'applicable': False}
            continue
        try:
            if input_only and name == 'radical_marks':
                found = inferred_radicals(f)
            elif input_only and name == 'electron_flow':
                from iupac_features import electron_flow_errors
                found = electron_flow_errors(f.curved, atoms=f.atoms, bonds=f.bonds,
                                             pair_counts=collections.Counter(f.lone_pairs), advisory=True)
            else:
                found = DETECTOR_FUNCS[name](f)
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as e:
            found = ['the check could not run: %s: %s' % (type(e).__name__, e)]
        warnings = [x for x in found if warning_finding(name, x)]
        found = [x for x in found if not warning_finding(name, x)]
        res[name] = {'ok': not found, 'findings': found, 'warnings': warnings, 'applicable': True}
        if found:
            res[name]['repairs'] = repair_hints(f, name, found)
    angles_rows = det_chain_angles(f, report_all=True)[1] if 'chain_angles' in applicable else []
    num_rows = []
    if 'number_place' in applicable:
        det_number_place(f, num_rows)
    ring = _ring_bonds(f)
    tables = {
        'angles_over_tolerance': [{'ref': r, 'angles': [round(a, 1) for a in ang], 'max_deviation': round(d, 1),
                                   'allowed': f.exceptions.get(r)} for r, ang, d in angles_rows if d > ANGLE_TOL],
        'max_chain_angle_deviation': round(max([d for _, _, d in angles_rows] or [0.0]), 2),
        'four_bond_atoms': [{'ref': r, 'directions': {n: round(angle_of(f.xy(r), f.xy(n)), 1) for n in c + e}}
                            for r, c, e in _four_bond_atoms(f)],
        'one_label_visible_line_pt': one_label_reference(f),
        'label_bond_reference_pt': f.bond_pt,
        'lengthened_bonds': [{'bond': '%s-%s' % (b['a'], b['b']), 'length_pt': r['length'], 'visible_line_pt': r['visible'],
                              'length_bond_lengths': round(r['length'] / f.bond_pt, 3) if r['length'] else None,
                              'ring': tuple(sorted((b['a'], b['b']))) in ring}
                             for b, r in _report_bonds(f) if r and _bond_kind(f, b) == 2],
        'numbers': num_rows,
        'circles': [{'ref': c['ref'], 'outer_d_pt': c['outer_d']} for c in _circles(f)],
        'default_circle_d_pt': round(default_circle_d(f), 3),
    }
    failed = [n for n in DETECTORS if not res[n]['ok']]
    return {'figure': f.name, 'features': sorted(f.features), 'applicable': applicable,
            'not_applicable': [n for n in DETECTORS if n not in applicable],
            'checks': res, 'ok': not failed, 'failed': failed, 'tables': tables}


# ------------------------------------------------------------------------------------------ command line
def load_state(outdir):
    path = Path(outdir) / '.state.json'
    try:
        state = json.loads(path.read_text(encoding='utf-8'))
    except OSError as e:
        raise InputError('cannot read %s: %s' % (path, e))
    except ValueError as e:
        raise InputError('bad JSON: %s' % e)
    return state


def load_figure(outdir):
    d = Path(outdir)
    try:
        svg = (d / 'figure.svg').read_text(encoding='utf-8')
    except OSError:
        svg = None
    try:
        state = load_state(d)
        res, rep, rl = state['resolved'], state['report'], state.get('reload', {})
    except KeyError as e:
        raise InputError('%s/.state.json is missing %s' % (d, e))
    return Figure(res, rep, rl, svg, name=str(d))


def summary_lines(result):
    lines = []
    c = result['checks']
    names = result.get('applicable', list(c))
    excluded = result.get('not_applicable', [])
    lines.append('checks: %d/%d pass (%d not applicable%s)' % (
        sum(c[n]['ok'] for n in names), len(names), len(excluded),
        ': ' + ', '.join(excluded) if excluded else ''))
    for name in names:
        for message in c[name].get('warnings', []):
            lines.append('WARNING: %s: %s' % (name, message))
    t = result['tables']
    one = '%.2f pt' % t['one_label_visible_line_pt'] if t['one_label_visible_line_pt'] else 'n/a'
    lines.append('  largest chain-angle deviation %.1f deg; four-bond atoms %d; one-label visible line %s; circles %s pt (default %.2f)' % (
        t['max_chain_angle_deviation'], len(t['four_bond_atoms']), one,
        ', '.join(sorted({'%.2f' % x['outer_d_pt'] for x in t['circles']})) or 'none', t['default_circle_d_pt']))
    for lb in t['lengthened_bonds']:
        lines.append('  label-label bond %s: %s pt long (%s bond lengths), visible line %s pt%s' % (
            lb['bond'], lb['length_pt'], lb['length_bond_lengths'], lb['visible_line_pt'],
            ' (ring bond: kept at the standard length)' if lb.get('ring') else ''))
    for n in DETECTORS:
        repairs = [hint.partition('. Action: ') for hint in c[n].get('repairs', [])]
        shown = set()                                 # each distinct action once, at the first finding it applies to
        visible = [r for i, r in enumerate(repairs) if i < 8 or 'no route found' in r[0]]
        for hint, _, action in visible:
            action = action.rstrip('.')
            lines.append('  FIX ' + hint + '.' + (' Action: %s.' % action if action and (action not in shown or 'no route found' in hint) else ''))
            shown.add(action)
        if len(repairs) > len(visible):
            lines.append('  FIX %s: (%d more)' % (n, len(repairs) - len(visible)))
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description='Check a drawn figure against the drawing rules (see the module docstring).')
    ap.add_argument('outdir', help='directory written by draw_figure.py')
    ap.add_argument('--json', help='write all results here')
    ap.add_argument('--quiet', action='store_true', help='summary line only')
    args = ap.parse_args(argv)
    try:
        fig = load_figure(args.outdir)
        result = run_checks(fig)
    except InputError as e:
        print('check_rules.py: %s' % e, file=sys.stderr)
        return 2
    lines = summary_lines(result)
    print('\n'.join(lines[:1] if args.quiet else lines))
    if args.json:
        Path(args.json).write_text(json.dumps({'result': result}, ensure_ascii=False, indent=1), encoding='utf-8')
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
