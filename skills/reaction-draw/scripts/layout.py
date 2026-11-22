#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Automatic 2D layout of a format-2 spec: atom positions of every species, panels in rows, titles, reaction arrows,
texts and boxes.  Used by specutil.resolve(); needs RDKit for stereo and for species with rings.

Units are bond lengths (1.0 = 14.4 pt in ACS style); x grows to the right and y DOWN.  Angles are as seen on the page:
degrees counterclockwise from the right (90 = up).  The rules applied are listed under "Layout" in the repository's docs/backend.md.
"""
import collections
import functools
import itertools
import math
import re

CONTACT_BASE = 1.3          # distance between the two atoms of a contact (bond lengths), +0.2 per labeled atom
FISHHOOK_ROOM = 1.0         # more between two species a fishhook joins: its tail sits on a dot, and its half head meets the other fishhook's tip there
HBOND_WEIGHT = 1.5          # weight of a hydrogen-bond contact (contacts)
ARROW_GAP = 0.75            # space between a reaction arrow and the drawings on either side
ARROW_MIN = 3.0             # shortest reaction arrow
PANEL_GAP = 2.0             # space between two panels that no arrow joins
ROW_GAP = 1.2               # space between rows
TITLE_GAP = 0.85            # from the lowest drawing of a row to the middle of its titles
TITLE_SEP = 0.7             # least space between the titles of two neighboring panels of a row (about 10 pt)
ROW_WIDTH_PT = 504.0        # the ACS double column (7 in, draw_figure.ACS_COLUMNS_PT): without "rows" in the input,
                            # panels wrap into rows that fit it, two or more panels to a row
SPECIES_GAP = 0.5           # white space kept between two species of a panel (bond lengths; check_rules.det_species_gap)
PAIR_PAD = 0.27             # how far the dots of a reacting pair reach outside the label box (bond lengths; 3 pt and a margin for the label estimate)
NEAR_DIST = 1.3             # default distance of a "near" placement of a species (atom to its middle)
TURN_LABELED = False        # True: a labeled chain atom (an imine NH) beside a branched group on a ring atom turns away
                            # as a labeled end atom does (a layout draw_figure.retry_turn tries for a figure whose
                            # letters were drawn too close)
TEXT_NEAR_GAP = 0.8         # default distance of a "near" text: from the atom to the near edge of the text
TEXT_APART = 0.8            # white space kept between two names of different molecules (texts, residue names), so
                            # that each reads as its own molecule's (check_rules.TEXT_APART_BONDS is the failing limit)
FORM_FOLD = 2.5             # a bond formed inside one molecule whose atoms stand farther apart (bond lengths) is folded
FORM_NEAR = 2.0             # to bring them this near (fold_forming)
CAP = 0.716                 # cap height of Arial per em
SUBSCRIPT_DROP = 0.1        # how far a label's subscript digits hang below its box of capitals (bonds)

# Arial (= Helvetica, Liberation Sans) advance widths per 1000 em, ASCII
_ARIAL = dict(zip(' !"#$%&\'()*+,-./', (278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278)))
_ARIAL.update({c: 556 for c in '0123456789'})
_ARIAL.update(dict(zip(':;<=>?@', (278, 278, 584, 584, 584, 556, 1015))))
_ARIAL.update(dict(zip('ABCDEFGHIJKLMNOPQRSTUVWXYZ', (667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
                                                     667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611))))
_ARIAL.update(dict(zip('[\\]^_`', (278, 278, 278, 469, 556, 333))))
_ARIAL.update(dict(zip('abcdefghijklmnopqrstuvwxyz', (556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
                                                     556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500))))
_ARIAL.update({'{': 334, '|': 260, '}': 334, '~': 584, '′': 191, '″': 382, '−': 584, '–': 556})


class LayoutError(ValueError):
    """The spec cannot be laid out; the message says why."""


# ------------------------------------------------------------------------------------------ geometry
def vec(deg):
    r = math.radians(deg)
    return (math.cos(r), -math.sin(r))


def ang(dx, dy):
    return math.degrees(math.atan2(-dy, dx)) % 360.0


HAND_LENGTH_FAR = 0.6      # bonds: hand coordinates this far from one bond apart are repaired (place) ...
HAND_COLLAPSE_DEG = 60     # ... or 0.25 bond off with a bond angle at either atom under this (a squeezed-in atom); a
                           # long bond kept on purpose with standard angles (a 1.5-bond acyl carbon) stays as given
HAND_REPAIR_ELEMENTS = {'H', 'B', 'C', 'N', 'O', 'F', 'Si', 'P', 'S', 'Cl', 'Se', 'Br', 'I'}


def adiff(a, b):
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def add(p, q, s=1.0):
    return (p[0] + s * q[0], p[1] + s * q[1])


def dist(p, q):
    return math.hypot(p[0] - q[0], p[1] - q[1])


def gaps(dirs):
    """Angular gaps between sorted directions: [(start, size)] going counterclockwise."""
    ds = sorted(d % 360.0 for d in dirs)
    out = []
    for k, d in enumerate(ds):
        nxt = ds[(k + 1) % len(ds)]
        out.append((d, (nxt - d) % 360.0 or 360.0))
    return out


def near_candidates(direction, d):
    """Directions and distances tried for a "near" placement: the asked direction first, then turning away from it in
    30 degree steps (all twelve directions when none is asked); the asked distance first, then a little farther."""
    if direction is None:
        dirs = [270.0, 90.0, 0.0, 180.0, 300.0, 240.0, 60.0, 120.0, 330.0, 30.0, 210.0, 150.0]
    else:
        d0 = float(direction)
        dirs = [d0] + [(d0 + s * k) % 360.0 for k in (30, 60, 90, 120, 150) for s in (1, -1)] + [(d0 + 180.0) % 360.0]
    return dirs, [d, d + 0.4, d + 0.8, d + 1.2]


def rigid_fit(old, new):
    """The turn (or mirror image) and shift that carries the points old onto new, as a function of one point."""
    ms = list(old)
    cx, cy = sum(old[m][0] for m in ms) / len(ms), sum(old[m][1] for m in ms) / len(ms)
    nx, ny = sum(new[m][0] for m in ms) / len(ms), sum(new[m][1] for m in ms) / len(ms)
    best = None
    for f in (1, -1):                       # f = -1 mirrors y first; points in a straight row take the plain turn
        a = sum((old[m][0]-cx)*(new[m][0]-nx) + f*(old[m][1]-cy)*(new[m][1]-ny) for m in ms)
        b = sum((old[m][0]-cx)*(new[m][1]-ny) - f*(old[m][1]-cy)*(new[m][0]-nx) for m in ms)
        t = math.atan2(b, a)
        c, sn = math.cos(t), math.sin(t)
        go = lambda p, c=c, sn=sn, f=f: (nx + c*(p[0]-cx) - sn*f*(p[1]-cy), ny + sn*(p[0]-cx) + c*f*(p[1]-cy))
        err = sum(dist(go(old[m]), new[m]) for m in ms)
        if best is None or err < best[0] - 1e-6:
            best = (err, go)
    return best[1]


def _box_gap(a, b):
    """Signed gap between two boxes (x1, y1, x2, y2): > 0 apart, < 0 overlapping (as check_rules.box_gap)."""
    dx = max(a[0] - b[2], b[0] - a[2])
    dy = max(a[1] - b[3], b[1] - a[3])
    if dx >= 0 or dy >= 0:
        return math.hypot(max(dx, 0.0), max(dy, 0.0))
    return max(dx, dy)


def text_width(text, size_pt, bond_pt=14.4):
    """Width of a plain text in Arial at size_pt, in bond lengths (digits after a letter in a label count as subscripts)."""
    return sum(_ARIAL.get(ch, 556) for ch in text) / 1000.0 * size_pt / bond_pt


def label_width(text, size_pt=10.0, bond_pt=14.4):
    w = 0.0
    for k, ch in enumerate(text):
        small = ch.isdigit() and k > 0 and text[k - 1].isalpha() or (ch.isdigit() and k > 0 and text[k - 1].isdigit())
        w += _ARIAL.get(ch, 556) / 1000.0 * size_pt * (0.75 if small else 1.0)
    return w / bond_pt


@functools.lru_cache(maxsize=4096)
def _label_metrics(text, size_pt, bond_pt):
    """(plain text, width, width of its first letter, whether a subscript digit follows a letter) of a label text, for
    label_box: they depend on the text alone, and the layout asks for the boxes of the same labels at many places."""
    import specutil
    plain = specutil.plain_text(text)
    return (plain, label_width(plain, size_pt, bond_pt), text_width(plain[:1], size_pt, bond_pt),
            re.search(r'[A-Za-z)]\d', plain) is not None)


# ------------------------------------------------------------------------------------------ one molecule
class Mol(object):
    """Graph of one species with atom indices in SMILES order."""

    def __init__(self, sp):
        self.sp = sp
        g = sp['graph']
        self.atoms = g['atoms']
        self.n = len(self.atoms)
        self.map = [a['map'] for a in self.atoms]
        self.idx = sp['map_to_index']
        self.adj = [[] for _ in range(self.n)]
        self.order = {}
        for b in g['bonds']:
            self.adj[b['a']].append(b['b'])
            self.adj[b['b']].append(b['a'])
            self.order[frozenset((b['a'], b['b']))] = b['order']
        self.is_h = [a['symbol'] == 'H' and not a['dummy'] for a in self.atoms]
        self.ring_bonds = self._ring_bonds()
        self.in_ring = [any(frozenset((i, j)) in self.ring_bonds for j in self.adj[i]) for i in range(self.n)]
        self.stereogenic = {i for i, a in enumerate(self.atoms) if a.get('chiral')}
        try:
            import stereochemistry
            potential = stereochemistry.potential_centers(sp['id'], sp['smiles_written'], relaxed=bool(sp.get('ts')))
            self.stereogenic.update(i for i, mp in enumerate(self.map) if '%s:%d' % (sp['id'], mp) in potential)
        except (LayoutError, ImportError, ValueError):
            pass

    def _ring_bonds(self):
        ring = set()
        for e in self.order:
            a, b = tuple(e)
            seen, stack = {a}, [a]
            while stack:
                x = stack.pop()
                for y in self.adj[x]:
                    if frozenset((x, y)) == e or y in seen:
                        continue
                    seen.add(y)
                    stack.append(y)
            if b in seen:
                ring.add(e)
        return ring

    def nb_maps(self, m):
        return {self.map[j] for j in self.adj[self.idx[m]]}

    def is_linear(self, i):
        """A carbon or nitrogen sp center with two neighbors (two double bonds: O=C=O, C=C=O, allenes, azides; or a triple
        bond): its two bonds are 180 degrees apart.  (S=O=S-type centers such as SO2 are bent: not listed.)"""
        if self.atoms[i]['symbol'] not in ('C', 'N') or self.atoms[i]['dummy']:
            return False
        orders = [self.order[frozenset((i, j))] for j in self.adj[i]]
        return len(orders) == 2 and (any(o == 3 for o in orders) or sum(1 for o in orders if o == 2) == 2)

    def labeled(self, i):
        a = self.atoms[i]
        return (a['dummy'] or a['symbol'] != 'C' or a['map'] in self.sp['show'] or a['map'] in self.sp['label'] or self.n == 1
                or not self.adj[i] or self.is_linear(i) or len(self.adj[i]) == 1 and len(self.adj[self.adj[i][0]]) == 1
                and a['symbol'] == 'C' and self.atoms[self.adj[i][0]]['symbol'] == 'C')

    def subtree(self, i, parent):
        out, stack = [], [(i, parent)]
        while stack:
            x, p = stack.pop()
            out.append(x)
            stack.extend((y, x) for y in self.adj[x] if y != p and y not in out)
        return out

    def longest_path(self, nodes=None):
        nodes = [i for i in range(self.n) if not self.is_h[i]] if nodes is None else nodes
        if not nodes:
            nodes = list(range(self.n))
        allowed = set(nodes)

        def bfs(s):
            prev, far, q = {s: None}, s, collections.deque([s])
            while q:
                x = q.popleft()
                for y in sorted(self.adj[x]):
                    if y in allowed and y not in prev:
                        prev[y] = x
                        q.append(y)
            depth = {}
            for v in prev:
                d, w = 0, v
                while prev[w] is not None:
                    d, w = d + 1, prev[w]
                depth[v] = d
            far = max(depth, key=lambda v: (depth[v], -v))
            return far, prev, depth
        best = None
        for s in sorted(allowed):
            if len([y for y in self.adj[s] if y in allowed]) > 1:
                continue                                    # paths start at ends of the heavy-atom tree
            far, prev, depth = bfs(s)
            cand = (depth[far], -s, -far)
            if best is None or cand > best[0]:
                path, w = [], far
                while w is not None:
                    path.append(w)
                    w = prev[w]
                best = (cand, path[::-1])
        if best is None:
            return [min(allowed)]
        path = best[1]
        return path if path[0] <= path[-1] else path[::-1]


def slot_dirs(fixed, k, side=1):
    """Directions for k more bonds on an atom whose other bonds point along `fixed` (degrees): the standard trigonal,
    four-bond and linear geometry.  May return more candidates than k (the caller chooses).  Two new bonds and none fixed
    (a water): bent, 120 degrees apart.  One fixed bond and three
    new ones: the four-bond layout, the first direction continuing the chain 120 degrees from the fixed bond (turning
    to `side`, +1 or -1) and the other two 60 degrees apart, each 90 degrees from its chain neighbor."""
    n = len(fixed) + k
    if k <= 0:
        return []
    if not fixed and k == 2:
        return [210.0, 330.0]                                # bent: two bonds on a lone atom (water) are 120 degrees apart
    if not fixed:
        return [(90.0 + 360.0 * j / max(k, 1) + (30.0 if k == 1 else 0)) % 360.0 for j in range(k)] if k > 1 else [0.0, 180.0, 90.0, 270.0]
    if len(fixed) == 1:
        f = fixed[0]
        if n == 2:
            return [(f + 120) % 360, (f - 120) % 360]
        if n == 3:
            return [(f + 120) % 360, (f - 120) % 360]
        if n == 4:
            return [(f + side * 120) % 360, (f + side * 210) % 360, (f + side * 270) % 360]
        return [(f + 360.0 * j / n) % 360 for j in range(1, n)]
    gs = sorted(gaps(fixed), key=lambda g: -g[1])
    big_start, big = gs[0]
    if len(fixed) == 2 and n == 4 and big >= 200:
        o = (big_start + big / 2.0) % 360.0                 # outer bisector of the chain angle: the four-bond layout
        return [(o - 30) % 360, (o + 30) % 360]
    if len(fixed) == 2 and n == 4:
        a, b = fixed
        return [(a + 90) % 360, (a - 90) % 360]
    if k == 1:
        return [(big_start + big / 2.0) % 360.0]
    return [(big_start + big * (j + 1) / (k + 1)) % 360.0 for j in range(k)]


def zigzag(mol, phase=1, cis=()):
    """Positions {atom index: (x, y)} of an acyclic species: main chain as a zigzag to the right from (0, 0).
    cis: chain bonds (frozensets of indices) whose chain neighbors must be cis (Z double bonds in the chain)."""
    path = mol.longest_path()
    pos = {path[0]: (0.0, 0.0)}
    dirs = []
    d = 30.0 * phase
    if len(path) > 2 and all(mol.is_linear(path[k]) for k in range(1, len(path) - 1)):
        d = 0.0                                                # a straight chain (O=C=O, a nitrile) lies flat
    t = None
    for k in range(len(path) - 1):
        if k > 0 and not mol.is_linear(path[k]):               # an sp center goes straight on: no turn
            # the turn at chain atom k: opposite to the previous turn (zigzag), the same one across a cis bond (k-1, k)
            if t is None:
                t = -60.0 * phase
            elif frozenset((path[k - 1], path[k])) not in cis:
                t = -t
            d = (d + t) % 360.0
        dirs.append(d)
        pos[path[k + 1]] = add(pos[path[k]], vec(d))
    in_path = set(path)
    for k, i in enumerate(path):
        back = (dirs[k - 1] + 180.0) % 360.0 if k > 0 else None
        fwd = dirs[k] if k < len(dirs) else None
        fixed = [x for x in (back, fwd) if x is not None]
        kids = [j for j in mol.adj[i] if j not in in_path and j not in pos]
        _place_kids(mol, i, kids, fixed, pos, prefer_turn=None)
    return pos, path


def _place_kids(mol, i, kids, fixed, pos, prefer_turn=None):
    """Place the children `kids` of atom i (bonds of i already used point along `fixed`) and grow their subtrees."""
    if not kids:
        return
    heavy = sorted([j for j in kids if not mol.is_h[j]], key=lambda j: (-len(mol.subtree(j, i)), j))
    light = sorted(j for j in kids if mol.is_h[j])
    order = heavy + light

    def room(dg):
        p = add(pos[i], vec(dg))
        return min([dist(p, q) for j, q in pos.items() if j != i] or [9.0])
    cands = slot_dirs(fixed, len(order))
    if len(fixed) == 1 and len(order) == 1 and mol.is_linear(i):
        cands = [(fixed[0] + 180.0) % 360.0]                   # sp center: the other bond points straight on
    elif len(fixed) == 1 and len(order) == 3:
        # the four-bond layout turns either way: the side with more room (the first side when equal)
        other = slot_dirs(fixed, 3, -1)
        if round(min(room(dg) for dg in other), 3) > round(min(room(dg) for dg in cands), 3):
            cands = other
    if len(cands) > len(order):
        # choose the candidates farthest from what is already placed (then the first ones)
        cands = sorted(cands, key=lambda dg: -round(room(dg), 3))[:len(order)]
    for j, dg in zip(order, cands):
        pos[j] = add(pos[i], vec(dg))
        _grow(mol, j, i, dg, pos)


def _grow(mol, i, parent, d_in, pos):
    """Grow the subtree of i (placed, reached from parent along d_in) as a zigzag pointing away from the rest."""
    kids = [j for j in mol.adj[i] if j != parent and j not in pos]
    if not kids:
        return
    back = (d_in + 180.0) % 360.0
    heavy = sorted([j for j in kids if not mol.is_h[j]], key=lambda j: (-len(mol.subtree(j, i)), j))
    light = sorted(j for j in kids if mol.is_h[j])
    if heavy:
        # continuation of the chain: turn +-60 degrees to the side with more room
        opts = [(d_in + 60.0) % 360.0, (d_in - 60.0) % 360.0]
        if mol.is_linear(i):
            opts = [d_in % 360.0]                              # an sp center goes straight on

        def room(dg):
            p = add(pos[i], vec(dg))
            return min([dist(p, q) for j, q in pos.items() if j not in (i,)] or [9.0])
        cont = max(opts, key=lambda dg: (round(room(dg), 3), -opts.index(dg)))
        first = heavy[0]
        pos[first] = add(pos[i], vec(cont))
        rest = heavy[1:] + light
        _place_kids(mol, i, rest, [back, cont], pos)
        _grow(mol, first, i, cont, pos)
    else:
        _place_kids(mol, i, light, [back], pos)


def _rdkit():
    try:
        from rdkit import Chem
        from rdkit.Chem import rdDepictor
    except ImportError:
        raise LayoutError('the automatic layout needs RDKit (pip install rdkit); or give "xy" for every atom')
    return Chem, rdDepictor


def _rd_mol(mol, with_stereo_smiles=None):
    """RDKit molecule in the species' atom order (dummy atoms as *, explicit H atoms kept)."""
    Chem, _ = _rdkit()
    if with_stereo_smiles:
        ps = Chem.SmilesParserParams()
        ps.removeHs = False
        ps.sanitize = not mol.sp.get('ts')
        m = Chem.MolFromSmiles(with_stereo_smiles, ps)
        if m is not None and m.GetNumAtoms() == mol.n:
            if mol.sp.get('ts'):
                m.UpdatePropertyCache(strict=False)
                Chem.FastFindRings(m)
            # Kekule serialization can reorder atoms; coordinates and wedges must keep their atom-map correspondence.
            index = {a.GetAtomMapNum(): a.GetIdx() for a in m.GetAtoms()}
            if set(index) != set(mol.map):
                raise LayoutError('%s: atom maps changed while resolving the stereo SMILES' % mol.sp['id'])
            order = [index[mp] for mp in mol.map]
            return Chem.RenumberAtoms(m, order) if order != list(range(mol.n)) else m
    rw = Chem.RWMol()
    for a in mol.atoms:
        at = Chem.Atom(0 if a['dummy'] else a['symbol'])
        at.SetFormalCharge(int(a['charge']))
        at.SetIsotope(int(a.get('isotope') or 0))
        at.SetNumRadicalElectrons(int(a.get('radical') or 0))
        at.SetNoImplicit(True)
        at.SetNumExplicitHs(int(a['h'] or 0))
        rw.AddAtom(at)
    bt = {1: Chem.BondType.SINGLE, 2: Chem.BondType.DOUBLE, 3: Chem.BondType.TRIPLE, 1.5: Chem.BondType.AROMATIC}
    for e, o in mol.order.items():
        a, b = sorted(e)
        rw.AddBond(a, b, bt.get(o, Chem.BondType.SINGLE))
    m = rw.GetMol()
    m.UpdatePropertyCache(strict=False)
    Chem.FastFindRings(m)
    return m


def _cip_double_bonds(m):
    from rdkit.Chem import rdCIPLabeler
    try:
        rdCIPLabeler.AssignCIPLabels(m)
    except Exception:
        return {}
    out = {}
    for b in m.GetBonds():
        if b.HasProp('_CIPCode') and b.GetBondTypeAsDouble() == 2.0:
            out[frozenset((b.GetBeginAtomIdx(), b.GetEndAtomIdx()))] = b.GetProp('_CIPCode')
    return out


def _stereo_wanted(mol):
    if '/' not in mol.sp['smiles_written'] and '\\' not in mol.sp['smiles_written']:
        return {}
    m = _rd_mol(mol, mol.sp['smiles_written'])
    return _cip_double_bonds(m)


def _stereo_drawn(mol, pos):
    Chem, _ = _rdkit()
    m = Chem.Mol(_rd_mol(mol))
    try:
        Chem.SanitizeMol(m, Chem.SanitizeFlags.SANITIZE_ALL ^ Chem.SanitizeFlags.SANITIZE_PROPERTIES)
    except Exception:
        pass
    conf = Chem.Conformer(mol.n)
    conf.Set3D(False)
    for i, (x, y) in pos.items():
        conf.SetAtomPosition(i, (x, -y, 0.0))
    m.AddConformer(conf, assignId=True)
    Chem.DetectBondStereochemistry(m)
    Chem.AssignStereochemistry(m, cleanIt=True, force=True)
    return _cip_double_bonds(m)


def depict(sp, force_rdkit=False):
    """Shape of one species in its own frame: {map: (x, y)} (bond length 1.0, y down) and the main chain (maps)."""
    mol = Mol(sp)
    if mol.n == 1:
        return {mol.map[0]: (0.0, 0.0)}, [mol.map[0]]
    intra = [(mol.idx[a], mol.idx[b]) for a, b in sp.get('intra_hbonds') or [] if a in mol.idx and b in mol.idx]
    if not mol.ring_bonds and not intra:
        want = _stereo_wanted(mol)
        best = None
        for phase in (1, -1):
            cis = set()
            for _ in range(4):
                pos, path = zigzag(mol, phase, cis)
                if not want:
                    break
                got = _stereo_drawn(mol, pos)
                wrong = [e for e, c in want.items() if got.get(e) not in (None, c)]
                if not wrong:
                    break
                for e in wrong:
                    a, b = tuple(e)
                    if a in path and b in path:
                        cis ^= {e}
            score = sum(pos[j][1] for i in path for j in mol.adj[i] if j not in path and not mol.is_h[j])
            if best is None or score < best[0] - 1e-6:
                best = (score, pos, path)
        _, pos, path = best
        pos = iupac_positions(mol, pos)
        return {mol.map[i]: p for i, p in pos.items()}, [mol.map[i] for i in path]
    # rings: RDKit CoordGen, turned so that the longest chain runs to the right, heavy branches up
    Chem, rdDepictor = _rdkit()
    m = _rd_mol(mol, sp['smiles_written'])
    if intra:
        # a hydrogen bond inside the species is two bonds through a stand-in atom while the 2D shape is made: the H faces
        # its acceptor across the ring they close, about the dotted-bond span apart
        rw = Chem.RWMol(m)
        for a, b in intra:
            x = rw.AddAtom(Chem.Atom(0))
            rw.AddBond(a, x, Chem.BondType.SINGLE)
            rw.AddBond(x, b, Chem.BondType.SINGLE)
        m = rw.GetMol()
        m.UpdatePropertyCache(strict=False)
        Chem.FastFindRings(m)
    rdDepictor.SetPreferCoordGen(True)
    if force_rdkit:
        rdDepictor.Compute2DCoords(m, forceRDKit=True)
    else:
        rdDepictor.Compute2DCoords(m)
    conf = m.GetConformer()
    raw = {i: (conf.GetAtomPosition(i).x, -conf.GetAtomPosition(i).y) for i in range(mol.n)}
    lens = sorted(dist(raw[a], raw[b]) for a, b in (tuple(e) for e in mol.order))
    s = lens[len(lens) // 2] or 1.0
    raw = {i: (x / s, y / s) for i, (x, y) in raw.items()}
    raw = transform(raw, lattice_turn(mol, raw, key=lambda i: i))     # on the 30-degree lattice before the turns below
    path = mol.longest_path()
    best = None
    for rot in range(0, 360, 30):
        for mirror in (False, True):
            p = transform(raw, rot, mirror)
            w = (p[path[-1]][0] - p[path[0]][0], p[path[-1]][1] - p[path[0]][1])
            tilt = adiff(ang(*w), 0.0) if math.hypot(*w) > 1e-6 else 0.0
            up = sum(p[j][1] for i in path for j in mol.adj[i] if j not in path and not mol.is_h[j])
            cand = (round(tilt / 30.0), up, rot, mirror)
            if best is None or cand < best[0]:
                best = (cand, p)
    pos = best[1]
    pos = iupac_positions(mol, pos)
    x0, y0 = pos[0]
    return {mol.map[i]: (x - x0, y - y0) for i, (x, y) in pos.items()}, [mol.map[i] for i in path]


def iupac_positions(mol, positions, kept=()):
    """IUPAC GR-4: move acyclic branches rigidly; do not distort a ring to place a substituent.  kept: four-bond atoms
    carried over from the previous panel, left as they stand when their bonds already form the 60/60/120/120 layout with
    a terminal bond horizontal or vertical (what the four_bond check asks)."""
    import stereochemistry
    pos = dict(positions)

    def direction(i, j):
        return ang(pos[j][0] - pos[i][0], pos[j][1] - pos[i][1])

    def move(i, j, degree):
        branch = mol.subtree(j, i)
        if i in branch:
            return
        origin = pos[i]
        delta = degree - direction(i, j)
        rotated = transform({k: (pos[k][0] - origin[0], pos[k][1] - origin[1]) for k in branch}, delta)
        for k, q in rotated.items():
            pos[k] = add(origin, q)

    intra = [(mol.idx[a], mol.idx[b]) for a, b in mol.sp.get('intra_hbonds') or [] if a in mol.idx and b in mol.idx]
    for i in range(mol.n):
        nb = mol.adj[i]
        if mol.atoms[i]['map'] in mol.sp.get('geometry', {}):
            continue
        if len(nb) > 4 and not mol.in_ring[i] and not mol.atoms[i].get('chiral') and not mol.sp.get('geometry'):
            ordered = sorted(nb, key=lambda j: direction(i, j))
            best = None
            for reverse in (False, True):
                sequence = ordered[::-1] if reverse else ordered
                for shift in range(len(nb)):
                    at = {j: (90 + 360 * ((k + shift) % len(nb)) / len(nb)) % 360 for k, j in enumerate(sequence)}
                    cost = sum(adiff(direction(i, j), at[j]) ** 2 * len(mol.subtree(j, i)) for j in nb)
                    if best is None or cost < best[0]:
                        best = (cost, at)
            for j, degree in best[1].items():
                move(i, j, degree)
            continue
        if len(nb) != 4 or mol.in_ring[i]:
            continue
        stereo = i in mol.stereogenic
        terminal = [j for j in nb if len(mol.adj[j]) == 1]
        if i in kept:
            ds = sorted(direction(i, j) for j in nb)
            spans = sorted((ds[(k + 1) % 4] - ds[k]) % 360 for k in range(4))
            if (max(abs(a - b) for a, b in zip(spans, (60, 60, 120, 120))) <= 2
                    and any(min(adiff(direction(i, j), a) for a in (0, 90, 180, 270)) <= 2 for j in terminal)):
                continue
        # a hydrogen bond inside the molecule whose ring runs through this atom (its H beyond one bond, its acceptor
        # beyond another): those two bonds stand next to each other, so that the other two point out of the ring and
        # its dotted line can pass clear of them
        ends = [[j for j in nb if k in mol.subtree(j, i)] for pair in intra if i not in pair for k in pair]
        closing = [(ends[k][0], ends[k + 1][0]) for k in range(0, len(ends), 2) if ends[k] and ends[k + 1] and ends[k] != ends[k + 1]]
        doubles = [j for j in nb if mol.order[frozenset((i, j))] == 2]
        square = not stereo and (len(terminal) == 4 or not terminal and all(len(mol.adj[j]) >= 3 for j in nb) or len(doubles) >= 2)
        small = len(terminal) in (1, 3) and not square
        pattern = [0, 90, 180, 270] if square else [0, 60, 180, 300] if small else [0, 60, 150, 270]
        best = None
        for rotation in range(0, 360, 30):
            if small and rotation % 90:
                continue
            angles = [(rotation + a) % 360 for a in pattern]
            for perm in itertools.permutations(nb):
                at = dict(zip(perm, angles))
                if square and len(doubles) == 2 and adiff(at[doubles[0]], at[doubles[1]]) < 179:
                    continue
                if small and perm[0] not in terminal:
                    continue
                # Two chain neighbors form 120 degrees; the two simple ends form 60 degrees.
                chain = [j for j in nb if j not in terminal]
                if not square and not small and len(chain) == 2 and (adiff(at[chain[0]], at[chain[1]]) != 120
                        or len(terminal) == 2 and adiff(at[terminal[0]], at[terminal[1]]) != 60):
                    continue
                cost = sum(adiff(direction(i, j), at[j]) ** 2 * len(mol.subtree(j, i)) for j in nb)
                around = sorted(nb, key=lambda j: at[j])
                cost += 1e9 * sum((around.index(a) - around.index(b)) % 4 == 2 for a, b in closing)
                if small and len(terminal) == 1:
                    # Preserve the side of a unique small group (often the
                    # reacting explicit H), choosing its nearest axial seat.
                    nearest_axis = round(direction(i, terminal[0]) / 90) * 90
                    cost += 200 * adiff(at[terminal[0]], nearest_axis) ** 2
                candidate, owner = {i: pos[i]}, {i: i}
                for j in nb:
                    branch = mol.subtree(j, i)
                    points = transform({k: (pos[k][0] - pos[i][0], pos[k][1] - pos[i][1]) for k in branch}, at[j] - direction(i, j))
                    for k, p in points.items():
                        candidate[k], owner[k] = add(pos[i], p), j
                for a, b in itertools.combinations(candidate, 2):
                    if owner[a] == owner[b] or b in mol.adj[a]:
                        continue
                    gap = .65 if mol.labeled(a) and mol.labeled(b) else .4
                    cost += 1e7 * max(0, gap - dist(candidate[a], candidate[b])) ** 2
                if best is None or cost < best[0]:
                    best = (cost, at)
        if best:
            for j, degree in best[1].items():
                move(i, j, degree)

    for i in range(mol.n):
        ring = [j for j in mol.adj[i] if frozenset((i, j)) in mol.ring_bonds]
        exo = [j for j in mol.adj[i] if j not in ring]
        if len(ring) == 3 and len(exo) == 1 and len(mol.adj[exo[0]]) == 1:
            fused, bridged = stereochemistry.ring_junctions(mol.sp['id'], mol.sp['smiles_written'], relaxed=bool(mol.sp.get('ts')))
            ref = '%s:%d' % (mol.sp['id'], mol.map[i])
            for edge in fused:
                if ref not in edge or ref in bridged:
                    continue
                far = next(r for r in edge if r != ref)
                j = mol.idx[int(far.rsplit(':', 1)[1])]
                degree = direction(i, j)
                if min(adiff(degree, a) for a in (90, 270)) <= 2:
                    move(i, exo[0], degree + 180)
        if len(ring) != 2 or len(exo) not in (1, 2):
            continue
        start, size = max(gaps([direction(i, j) for j in ring]), key=lambda g: g[1])
        bisector = (start + size / 2) % 360
        snap = round(bisector / 30) * 30 % 360
        if adiff(snap, bisector) <= 5:
            bisector = snap
        if len(exo) == 1:
            move(i, exo[0], bisector)
            continue
        sizes = {j: len(mol.subtree(j, i)) for j in exo}
        # Ring size from the shortest ring path between the two ring neighbors.
        queue, seen, ring_size = collections.deque([(ring[0], 2)]), {i, ring[0]}, None
        while queue:
            j, length = queue.popleft()
            if j == ring[1]:
                ring_size = length
                break
            for k in mol.adj[j]:
                if k not in seen and frozenset((j, k)) in mol.ring_bonds:
                    seen.add(k)
                    queue.append((k, length + 1))
        if ring_size == 6 and min(sizes.values()) == 1 and max(sizes.values()) > 1:
            large = max(exo, key=lambda j: sizes[j])
            small = next(j for j in exo if j != large)
            move(i, large, bisector)
            choices = [(bisector - size / 4) % 360, (bisector + size / 4) % 360]
            def room(degree):
                p = add(pos[i], vec(degree))
                return min([dist(p, q) for k, q in pos.items() if k not in (i, small)] or [9])
            degree = max(choices, key=lambda d: (round(room(d), 3), math.cos(math.radians(d)), math.sin(math.radians(d))))
            axis = round(degree / 90) * 90 % 360
            move(i, small, axis if adiff(axis, degree) <= 5 else degree)
        else:
            spread = size / 3 if all(len(mol.adj[j]) >= 3 for j in exo) else 60
            options = [(bisector - spread / 2, bisector + spread / 2), (bisector + spread / 2, bisector - spread / 2)]
            angles = list(min(options, key=lambda ds: sum(adiff(direction(i, j), d) for j, d in zip(exo, ds))))
            if spread == 60:
                # a labeled end atom (NH3+) beside a branched group (CO2-): its label would meet the group's labels,
                # so it turns 30 degrees away (legibility first, GR-4.2.3); a labeled chain atom (an imine NH) too
                # under TURN_LABELED
                for k, j in enumerate(exo):
                    if mol.labeled(j) and (len(mol.adj[j]) == 1 or TURN_LABELED and len(mol.adj[j]) == 2) \
                            and len(mol.adj[exo[1 - k]]) >= 3:
                        away = 1 if (angles[k] - angles[1 - k]) % 360 < 180 else -1
                        angles[k] = (angles[k] + 30 * away) % 360
            for j, degree in zip(exo, angles):
                move(i, j, degree)
    # A forced exterior bisector can expose an old folded branch. Reflect whole
    # acyclic branches about their root bond to clear it without changing angles.
    def clutter(points):
        score = 0.0
        for a, b in itertools.combinations(range(mol.n), 2):
            if b in mol.adj[a]:
                continue
            gap = .65 if mol.labeled(a) and mol.labeled(b) else .4
            score += max(0, gap - dist(points[a], points[b])) ** 2
        for i in range(mol.n):
            if not mol.labeled(i):
                continue
            p = points[i]
            for edge in mol.order:
                a, b = tuple(edge)
                if i in edge:
                    continue
                q, r = points[a], points[b]
                dx, dy = r[0] - q[0], r[1] - q[1]
                t = max(0, min(1, ((p[0] - q[0]) * dx + (p[1] - q[1]) * dy) / (dx * dx + dy * dy or 1)))
                score += max(0, .3 - dist(p, (q[0] + t * dx, q[1] + t * dy))) ** 2
        return score
    for _ in range(3):
        current, best = clutter(pos), None
        if current < 1e-9:
            break
        for edge in mol.order:
            if edge in mol.ring_bonds:
                continue
            a, b = tuple(edge)
            for root, child in ((a, b), (b, a)):
                side = mol.subtree(child, root)
                if len(side) < 2 or len(side) > mol.n / 2:
                    continue
                p, q = pos[root], pos[child]
                length = dist(p, q)
                ux, uy = (q[0] - p[0]) / length, (q[1] - p[1]) / length
                candidate = dict(pos)
                for k in side:
                    x, y = pos[k][0] - p[0], pos[k][1] - p[1]
                    projection = x * ux + y * uy
                    candidate[k] = (p[0] + 2 * projection * ux - x, p[1] + 2 * projection * uy - y)
                score = clutter(candidate)
                if score < current - 1e-9 and (best is None or score < best[0]):
                    best = (score, candidate)
        if best is None:
            break
        pos = best[1]
    return pos


def iupac_stereobonds(mol, xy, styles):
    """Prefer adjacent wedge/hash pairs on four explicit neighbors; validate their CIP assignment."""
    import stereochemistry
    sid = mol.sp['id']
    (wanted, _), _, stubs = stereochemistry.expected(sid, mol.sp['smiles_written'], relaxed=bool(mol.sp.get('ts')))
    refs = {i: '%s:%d' % (sid, mol.map[i]) for i in range(mol.n)}
    atoms = {refs[i]: {'el': 'C' if a['dummy'] else a['symbol'], 'charge': a['charge'], 'h': a['h'],
                      'x': xy[mol.map[i]][0], 'y': xy[mol.map[i]][1]} for i, a in enumerate(mol.atoms)}

    def labels(candidate):
        edges = []
        for e, order in mol.order.items():
            i, j = sorted(e)
            st, (a, b) = candidate.get(frozenset((mol.map[i], mol.map[j])), ('plain', (mol.map[i], mol.map[j])))
            edges.append({'a': '%s:%d' % (sid, a), 'b': '%s:%d' % (sid, b), 'order': order, 'style': st})
        return stereochemistry.perceived(atoms, edges, stubs, relaxed=bool(mol.sp.get('ts')))[0]

    result = dict(styles)
    for i, atom in enumerate(mol.atoms):
        if refs[i] not in wanted:
            continue
        nb = mol.adj[i]
        simple = [j for j in nb if j not in mol.stereogenic and frozenset((i, j)) not in mol.ring_bonds
                  and mol.order[frozenset((i, j))] == 1]
        choices = []
        small_ring_substituent = (mol.in_ring[i] and len(nb) == 4 and len(simple) == 2
                                 and sorted(len(mol.subtree(j, i)) for j in simple)[0] == 1
                                 and sorted(len(mol.subtree(j, i)) for j in simple)[1] > 1)
        if mol.in_ring[i] and len(simple) == 1:
            choices = [(simple[0],)]
        elif small_ring_substituent:
            choices = [(min(simple, key=lambda j: len(mol.subtree(j, i))),)]
        elif len(nb) == 4 and len(simple) >= 2:
            for a, b in itertools.combinations(simple, 2):
                pa, pb, p = xy[mol.map[a]], xy[mol.map[b]], xy[atom['map']]
                separation = adiff(ang(pa[0] - p[0], pa[1] - p[1]), ang(pb[0] - p[0], pb[1] - p[1]))
                if abs(separation - 60) <= 10:
                    choices.append((a, b))
        elif len(nb) == 3:
            # the implicit H must stay readable: a stereobond to a branched substituent leaves it ambiguous (the rule
            # stereo_display checks), so such a neighbor comes last; then the smallest substituent first
            rings = {k for k in range(mol.n) if mol.in_ring[k]}
            wide = lambda j: stereochemistry.wide_end(i, j, lambda x: mol.adj[x], lambda x: 'C' if mol.atoms[x]['dummy'] else mol.atoms[x]['symbol'], rings)
            choices = [(j,) for j in sorted(simple, key=lambda j: (wide(j), len(mol.subtree(j, i))))]
        for choice in choices:
            accepted = None
            for first in ('wedge', 'hash'):
                candidate = {e: v for e, v in result.items() if not (v[0] in ('wedge', 'hash') and v[1][0] == atom['map'])}
                for k, j in enumerate(choice):
                    candidate[frozenset((atom['map'], mol.map[j]))] = (first if k == 0 else 'hash' if first == 'wedge' else 'wedge', (atom['map'], mol.map[j]))
                try:
                    have = labels(candidate)
                except (ValueError, RuntimeError):
                    continue
                if have.get(refs[i]) == wanted[refs[i]] and all(have.get(r) == value for r, value in wanted.items()):
                    accepted = candidate
                    break
            if accepted is not None:
                result = accepted
                break
    return result


def transform(pos, rot=0.0, mirror=False, flip=None):
    """Turn a {key: (x, y)} shape by rot degrees (counterclockwise on the page) after mirroring it up-down (mirror)
    and/or applying flip ('x' left-right, 'y' up-down, 'xy')."""
    c, s = math.cos(math.radians(rot)), math.sin(math.radians(rot))
    out = {}
    for k, (x, y) in pos.items():
        if flip and 'x' in flip:
            x = -x
        if flip and 'y' in flip:
            y = -y
        if mirror:
            y = -y
        # rotation on the page (y down): x' = x c + y s, y' = -x s + y c
        out[k] = (x * c + y * s, -x * s + y * c)
    return out


def six_rings(mol):
    """The six-membered rings of a molecule (tuples of atom indices in ring order)."""
    rings = {}
    for e in mol.ring_bonds:
        a, b = tuple(e)
        stack = [(b, (b,))]
        while stack:
            x, path = stack.pop()
            for y in mol.adj[x]:
                if y == a and len(path) == 5:
                    rings.setdefault(frozenset(path + (a,)), path + (a,))
                elif y != a and y not in path and len(path) < 5:
                    stack.append((y, path + (y,)))
    return list(rings.values())


def lattice_turn(mol, pos, key=None):
    """Degrees (within +-15, counterclockwise on the page) that put a molecule's bonds on the 30-degree lattice of
    standard drawings, so that its rings and chains run level or upright: the bonds of its most regular six-membered ring
    (a ring a reaction left undistorted) if it has one, else its bonds between heavy atoms.  pos is keyed by
    key(atom index), the atom's map by default."""
    key = key or (lambda i: mol.map[i])
    def phase(bonds):
        sx = sy = 0.0
        n = 0
        for i, j in bonds:
            a, b = pos.get(key(i)), pos.get(key(j))
            if a is None or b is None or dist(a, b) < 1e-6:
                continue
            t = math.radians(ang(b[0] - a[0], b[1] - a[1]) * 12)
            sx, sy, n = sx + math.cos(t), sy + math.sin(t), n + 1
        return (math.hypot(sx, sy) / n if n else 0.0), sx, sy
    def doubles(r):               # among equally regular rings, the one with more double bonds (an aromatic ring)
        return sum(1 for i, j in zip(r, r[1:] + r[:1]) if float(mol.order[frozenset((i, j))]) > 1)
    rings = [(round(ph[0], 2), doubles(r), ph) for r in six_rings(mol) for ph in [phase(list(zip(r, r[1:] + r[:1])))]]
    best = max(rings, key=lambda x: x[:2])[2] if rings else phase([tuple(e) for e in mol.order if not any(mol.is_h[k] for k in e)])
    if best[0] < 1e-6:
        return 0.0
    turn = -math.degrees(math.atan2(best[2], best[1])) / 12
    return turn if abs(turn) > 0.5 else 0.0


def straighten(mol, pos, key=None):
    """pos turned about its middle onto the 30-degree lattice (lattice_turn)."""
    turn = lattice_turn(mol, pos, key)
    if not turn or not pos:
        return pos
    cx, cy = sum(x for x, _ in pos.values()) / len(pos), sum(y for _, y in pos.values()) / len(pos)
    out = transform({k: (x - cx, y - cy) for k, (x, y) in pos.items()}, turn)
    return {k: (x + cx, y + cy) for k, (x, y) in out.items()}


# ------------------------------------------------------------------------------------------ the figure
class Layout(object):
    def __init__(self, spec):
        self.spec = spec
        self.style = spec['style']
        self.panels = spec['panels']
        self.pidx = {p['id']: k for k, p in enumerate(self.panels)}
        self.sp = spec['species_by_id']
        self.mol = {sid: Mol(s) for sid, s in self.sp.items()}
        self.xy = {}                    # sid -> {map: (x, y)} in its panel's frame
        self.chain = {}                 # sid -> main chain maps
        self.kept_centers = {}          # sid -> four-bond atoms derive kept as they stood in the previous panel
        self.vacated = set()            # (panel, map): an atom whose former partner (a kept product) stepped aside for it
        self.shapes = {}                # key -> (shape, chain) of the first depiction
        self.notes = []
        self.contact_maps = collections.defaultdict(set)     # sid -> maps used as contact / arrow ends
        self.next_panel = {}
        for r in spec['reaction_arrows']:
            self.next_panel.setdefault(r['from'], r['to'])
        for k, p in enumerate(self.panels[:-1]):
            self.next_panel.setdefault(p['id'], self.panels[k + 1]['id'])
        self.prev_panel = {v: k for k, v in self.next_panel.items()}
        for p in self.panels:                     # atoms that face another molecule (they stay put when a bond is lengthened)
            for sa, ea, sb, eb, _ in self.contacts(p['id']):
                for sid, (m, m2) in ((sa, ea), (sb, eb)):
                    self.contact_maps[sid].add(m)
                    if m2 is not None:
                        self.contact_maps[sid].add(m2)
        self.moves = {sid: self._moves(sid) for sid in self.sp}

    # ---- basic shapes
    def shape(self, sid):
        sp = self.sp[sid]
        if sp['key'] not in self.shapes:
            self.shapes[sp['key']] = depict(sp)
        shape, chain = self.shapes[sp['key']]
        mol = self.mol[sid]
        if set(shape) != set(mol.idx):           # first depicted from another appearance: depict this one on its own
            shape, chain = depict(sp)
        return dict(shape), list(chain)

    def panel_points(self, pid, skip=None):
        """[(point, labeled)] of everything placed in a panel (atoms and points along bonds)."""
        pts = []
        for key in self.panels[self.pidx[pid]]['species']:
            sid = '%s.%s' % (pid, key)
            if sid == skip or sid not in self.xy:
                continue
            pts += self.points_of(sid, self.xy[sid])
        return pts

    def pair_sources(self, sid):
        """Maps of the atoms of sid that a curved arrow of its panel starts from: their pair or unpaired-electron dots."""
        return {ar['from']['atom'] for ar in self.panels[self.pidx[self.sp[sid]['panel']]]['arrows']
                if ar['from']['sid'] == sid and ar['from']['atom2'] is None}

    def ink(self, sid, xy, pads=False, skip=()):
        """What a species draws, for spacing: ([points along its bare bonds and at its bare vertices], [label boxes]).
        With pads, the box of an atom an arrow starts from grows by PAIR_PAD on every side: its dots go on a side chosen later."""
        mol = self.mol[sid]
        ex = self.expanded(sid, xy)
        src = self.pair_sources(sid) if pads else set()
        grow = lambda b: (b[0] - PAIR_PAD, b[1] - PAIR_PAD, b[2] + PAIR_PAD, b[3] + PAIR_PAD)
        labeled = [mol.labeled(i) for i in range(mol.n)]
        boxes = [grow(self.label_box(sid, mol.map[i], ex)) if mol.map[i] in src else self.label_box(sid, mol.map[i], ex)
                 for i in range(mol.n) if labeled[i] and mol.map[i] not in skip]
        boxes += [grow(self.label_box(sid, mol.map[i], ex)) for i in range(mol.n)
                  if not labeled[i] and mol.map[i] in src and mol.map[i] not in skip]
        pts = [ex[mol.map[i]] for i in range(mol.n) if not labeled[i] and mol.map[i] not in skip]
        for e in mol.order:
            a, b = (mol.map[i] for i in e)
            pts += [(ex[a][0] + t * (ex[b][0] - ex[a][0]), ex[a][1] + t * (ex[b][1] - ex[a][1])) for t in (0.25, 0.5, 0.75)]
        return pts, boxes

    def panel_ink(self, pid, skip=None, pads=False, ends=None):
        """The ink of everything placed in a panel except species skip, with the hydrogen-bond lines between them.
        ends: {species: atoms} left out (the far ends of skip's hydrogen bonds)."""
        pts, boxes = [], []
        for key in self.panels[self.pidx[pid]]['species']:
            o = '%s.%s' % (pid, key)
            if o != skip and o in self.xy:
                p, b = self.ink(o, self.xy[o], pads, (ends or {}).get(o, ()))
                pts += p
                boxes += b
        return pts, boxes + self.hbond_line_boxes(pid, skip)

    def shortfall(self, sid, xy, other, pads=False, skip=()):
        """Sum of squares of how much species sid at xy falls short of SPECIES_GAP from the ink other (panel_ink)."""
        g = SPECIES_GAP
        pts, boxes = self.ink(sid, xy, pads, skip)
        if not pts and not boxes:        # a lone atom that is a hydrogen bond's end: nothing left to keep apart
            return 0.0
        xs = [p[0] for p in pts] + [b[0] for b in boxes] + [b[2] for b in boxes]
        ys = [p[1] for p in pts] + [b[1] for b in boxes] + [b[3] for b in boxes]
        lo_x, hi_x, lo_y, hi_y = min(xs) - g, max(xs) + g, min(ys) - g, max(ys) + g
        opts = [q for q in other[0] if lo_x <= q[0] <= hi_x and lo_y <= q[1] <= hi_y]
        oboxes = [c for c in other[1] if c[2] >= lo_x and c[0] <= hi_x and c[3] >= lo_y and c[1] <= hi_y]
        out = 0.0
        for p in pts:
            for q in opts:
                d = dist(p, q)
                if d < g:
                    out += (g - d) ** 2
            for c in oboxes:
                d = _box_gap((p[0], p[1], p[0], p[1]), c)
                if d < g:
                    out += (g - d) ** 2
        for b in boxes:
            for q in opts:
                d = _box_gap(b, (q[0], q[1], q[0], q[1]))
                if d < g:
                    out += (g - d) ** 2
            for c in oboxes:
                d = _box_gap(b, c)
                if d < g:
                    out += (g - d) ** 2
        return out

    def hbond_line_boxes(self, pid, skip=None):
        """Small boxes along each hydrogen bond drawn between two placed species of a panel (neither of them skip)."""
        boxes = []
        for a, b in self.panels[self.pidx[pid]]['hbonds']:
            if skip in (a['sid'], b['sid']) or a['sid'] not in self.xy or b['sid'] not in self.xy or a['sid'] == b['sid']:
                continue
            pa, pb = self.xy[a['sid']][a['atom']], self.xy[b['sid']][b['atom']]
            for k in range(2, 9):
                x, y = pa[0] + k / 10.0 * (pb[0] - pa[0]), pa[1] + k / 10.0 * (pb[1] - pa[1])
                boxes.append((x - 0.03, y - 0.03, x + 0.03, y + 0.03))
        return boxes

    def _moves(self, sid):
        """{bond (frozenset of maps): (moved map, its side as a set of maps)} for every chain bond between two labels: the
        backend lengthens these bonds by moving one side; the side without the atoms that face other molecules moves (else
        the smaller side)."""
        mol = self.mol[sid]
        out = {}
        for e in mol.order:
            if e in mol.ring_bonds:
                continue
            i, j = sorted(e)
            if not (mol.labeled(i) and mol.labeled(j)):
                continue
            si, sj = mol.subtree(i, j), mol.subtree(j, i)
            ci = any(mol.map[k] in self.contact_maps[sid] for k in si)
            cj = any(mol.map[k] in self.contact_maps[sid] for k in sj)
            move, side = (j, sj) if ci and not cj else (i, si) if cj and not ci else ((i, si) if len(si) <= len(sj) else (j, sj))
            other = i if move == j else j
            out[frozenset((mol.map[i], mol.map[j]))] = (mol.map[move], mol.map[other], {mol.map[k] for k in side})
        return out

    def expanded(self, sid, xy):
        """Collision and panel measurements use the actual, unextended coordinates."""
        return dict(xy)

    def points_of(self, sid, xy):
        mol = self.mol[sid]
        xy = self.expanded(sid, xy)
        pts = [(xy[mol.map[i]], mol.labeled(i)) for i in range(mol.n)]
        for e in mol.order:
            a, b = (mol.map[i] for i in e)
            for t in (0.25, 0.5, 0.75):
                pts.append(((xy[a][0] + t * (xy[b][0] - xy[a][0]), xy[a][1] + t * (xy[b][1] - xy[a][1])), False))
        return pts

    def label_box(self, sid, m, xy, name=False):
        """Approximate box (x1, y1, x2, y2) of the label of atom m, or a small box around a skeletal vertex.  name: a
        residue name as wide as its caption is drawn (its digits full size), for the room kept between names."""
        import specutil
        mol = self.mol[sid]
        a = mol.atoms[mol.idx[m]]
        text = specutil.label_text(self.sp[sid], a, xy)
        x, y = xy[m]
        if not text:
            return (x - 0.12, y - 0.12, x + 0.12, y + 0.12)
        size = self.style['font_size_pt']
        h = CAP * size / self.style['bond_length_pt']
        plain, w, first, sub = _label_metrics(text, size, self.style['bond_length_pt'])
        kind = specutil.atom_kind(self.sp[sid], a)
        if kind == 'atom' and re.match(r'^%s\d+[\'"\u2032\u2033]*$' % re.escape(a['symbol']), plain):
            # an atom numbered with its element (O3, N1'): the number follows the symbol at full size, and may sit a
            # little low to clear a bond (octet_draw.number_after_seat), so it hangs like a subscript
            w, sub = text_width(plain, size, self.style['bond_length_pt']), True
        # a subscript (the 3 of CH3, the 2 of NH2) hangs below the line by about a tenth of a bond
        low = SUBSCRIPT_DROP if kind != 'residue' and sub else 0.0
        nbrs = [xy[mol.map[j]] for j in mol.adj[mol.idx[m]]]
        dx = sum(p[0] - x for p in nbrs)
        if kind == 'residue':                      # beside the root vertex, away from its bond (octet_draw.text_side)
            if name:
                w = text_width(plain, size, self.style['bond_length_pt'])
            dy = sum(p[1] - y for p in nbrs)
            if nbrs and abs(dx) < 0.3 * math.hypot(dx, dy):   # a steep bond: the name centred above or below the vertex
                if dy > 0:
                    return (x - w / 2, y - 0.15 - h, x + w / 2, y - 0.15)
                return (x - w / 2, y + 0.15, x + w / 2, y + 0.15 + h)
            if dx <= 0:
                return (x + 0.15, y - h / 2, x + 0.15 + w, y + h / 2)
            return (x - 0.15 - w, y - h / 2, x - 0.15, y + h / 2)
        if kind == 'atom' and a['symbol'] != 'H' and plain.startswith(a['symbol'] + 'H') and len(nbrs) >= 2:
            # Octet stacks the H of a label (NH) above or below the symbol when its two or more bonds point steeply
            # down or up (render.ts: steeper than 70 degrees from horizontal)
            vx = sum((p[0] - x) / (dist(p, (x, y)) or 1.0) for p in nbrs)
            vy = sum((p[1] - y) / (dist(p, (x, y)) or 1.0) for p in nbrs)
            if math.hypot(vx, vy) > 1e-6 and abs(vy) > abs(vx) * math.tan(math.radians(70)):
                sym = text_width(a['symbol'], size, self.style['bond_length_pt'])
                hw = max(sym, label_width(plain.replace(a['symbol'], '', 1), size, self.style['bond_length_pt']))
                if vy > 0:                         # bonds go down: the H on top
                    return (x - hw / 2, y - h / 2 - 1.25 * h, x + hw / 2, y + h / 2 + 0.05 + low)
                return (x - hw / 2, y - h / 2 - 0.05, x + hw / 2, y + h / 2 + 1.25 * h + low)
        if plain[:1] == 'H' and a['symbol'] != 'H' and kind == 'atom' or (kind == 'abbrev' and dx > 0.25):
            return (x + first / 2 - w, y - h / 2 - 0.05, x + first / 2, y + h / 2 + 0.05 + low)
        return (x - first / 2, y - h / 2 - 0.05, x - first / 2 + w, y + h / 2 + 0.05 + low)

    def bbox(self, sids):
        xs, ys = [], []
        for sid in sids:
            xy = self.expanded(sid, self.xy[sid])
            for m in xy:
                b = self.label_box(sid, m, xy)
                xs += [b[0], b[2]]
                ys += [b[1], b[3]]
        return (min(xs), min(ys), max(xs), max(ys)) if xs else (0.0, 0.0, 0.0, 0.0)

    # ---- placement
    def place(self, sid, xy):
        sp = self.sp[sid]
        if sp.get('geometry'):
            from iupac_features import geometry_layout
            xy = geometry_layout(sp, xy)
        if sp['offset']:
            xy = {m: (x + sp['offset'][0], y + sp['offset'][1]) for m, (x, y) in xy.items()}
            if not sp['xy']:
                # an offset keeps SPECIES_GAP, counting the dots of reacting pairs, from what this panel already holds
                cleared = self.clear_of_panel(sid, xy, pads=True)
                if cleared is not xy:
                    self.notes.append('%s: offset left it closer than half a bond to another molecule; moved it clear' % sid)
                xy = cleared
        for m, p in sp['xy'].items():
            xy[m] = p
        mol = self.mol[sid]
        # A partial xy overlay can move an anchored group away from its free
        # neighbor. Restore that bridge's standard length by translating only
        # the unconstrained branch; rings and explicit coordinates stay intact.
        fixed = {mol.idx[m] for m in sp['xy']}
        for edge in mol.order:
            a, b = tuple(edge)
            if (a in fixed) == (b in fixed):
                continue
            anchor, free = (a, b) if a in fixed else (b, a)
            branch = set(mol.subtree(free, anchor))
            if anchor in branch or fixed.intersection(branch):
                continue
            origin, end = xy[mol.map[anchor]], xy[mol.map[free]]
            length = dist(origin, end)
            if length < 1e-8 or abs(length - 1) <= 1e-4:
                continue
            shift = ((end[0] - origin[0]) * (1 / length - 1), (end[1] - origin[1]) * (1 / length - 1))
            for i in branch:
                xy[mol.map[i]] = add(xy[mol.map[i]], shift)
        # Hand coordinates that hold two bonded atoms far from one bond apart (a misplaced atom, not a ring closure, a
        # transition state or a bond to a metal; a guanidinium N given 1.73 bonds from its C, 30 degrees from the other
        # N): the atom on the smaller side comes back to one bond.  At an atom with three neighbors the largest branch
        # stays and the other two take the two 120-degree seats in the order the hand coordinates gave them; elsewhere
        # the moved branch takes the widest gap.  Each moved branch is shifted (its own bonds keep their directions, so
        # an H aimed at a partner stays aimed) or turned with the move, whichever leaves the wider angle at the atom.
        for edge in sorted(mol.order, key=sorted):
            a, b = sorted(edge)
            if a not in fixed or b not in fixed or edge in mol.ring_bonds or sp.get('ts') \
                    or any(mol.atoms[k]['dummy'] or mol.atoms[k]['symbol'] not in HAND_REPAIR_ELEMENTS for k in (a, b)):
                continue
            off = abs(dist(xy[mol.map[a]], xy[mol.map[b]]) - 1)

            def squeezed(i, j):
                d = ang(xy[mol.map[j]][0] - xy[mol.map[i]][0], xy[mol.map[j]][1] - xy[mol.map[i]][1])
                return any(adiff(d, ang(xy[mol.map[k]][0] - xy[mol.map[i]][0], xy[mol.map[k]][1] - xy[mol.map[i]][1]))
                           < HAND_COLLAPSE_DEG for k in mol.adj[i] if k != j)
            if off <= 0.25 or off <= HAND_LENGTH_FAR and not (squeezed(a, b) or squeezed(b, a)):
                continue
            anchor, free = (a, b) if len(mol.subtree(b, a)) <= len(mol.subtree(a, b)) else (b, a)
            if anchor in mol.subtree(free, anchor):
                continue
            o = xy[mol.map[anchor]]
            towards = lambda j: ang(xy[mol.map[j]][0] - o[0], xy[mol.map[j]][1] - o[1])
            was = dist(o, xy[mol.map[free]])
            nb = mol.adj[anchor]
            stay = max(nb, key=lambda j: (len(mol.subtree(j, anchor)), -j))
            if len(nb) == 3 and stay != free and not any(frozenset((anchor, j)) in mol.ring_bonds for j in nb):
                kids = sorted((j for j in nb if j != stay), key=lambda j: (towards(j) - towards(stay)) % 360)
                seats = {j: (towards(stay) + 120 * (k + 1)) % 360 for k, j in enumerate(kids)}
            else:
                st, g = max(gaps([towards(j) for j in nb if j != free]), key=lambda t: t[1]) if len(nb) > 1 else (towards(free), 0.0)
                seats = {free: (st + g / 2.0) % 360 if len(nb) > 1 else towards(free)}
            for kid, target in seats.items():
                rel = {mol.map[k]: (xy[mol.map[k]][0] - o[0], xy[mol.map[k]][1] - o[1]) for k in mol.subtree(kid, anchor)}
                fp = rel[mol.map[kid]]
                shifted = {m: add(add(q, fp, -1), vec(target)) for m, q in rel.items()}
                turned = transform(rel, target - ang(fp[0], fp[1]))
                tp = turned[mol.map[kid]]
                turned = {m: add(q, tp, 1 / math.hypot(tp[0], tp[1]) - 1) for m, q in turned.items()}

                def widest(shape, kid=kid):
                    f = shape[mol.map[kid]]
                    return min([adiff(ang(-f[0], -f[1]), ang(shape[mol.map[j]][0] - f[0], shape[mol.map[j]][1] - f[1]))
                                for j in mol.adj[kid] if j != anchor] or [180.0])
                for m, q in (shifted if widest(shifted) >= widest(turned) - 1e-6 else turned).items():
                    xy[m] = add(o, q)
            self.notes.append('%s: hand coordinates put %d %.2f bonds from %d; set the bonds of %d one bond long in standard seats'
                              % (sid, mol.map[free], was, mol.map[anchor], mol.map[anchor]))
        positioned = iupac_positions(mol, {i: xy[mol.map[i]] for i in range(mol.n)},
                                     {mol.idx[m] for m in self.kept_centers.get(sid, ())})
        changed = [dist(xy[mol.map[i]], positioned[i]) for i in range(mol.n) if mol.map[i] in sp['xy']
                   and dist(xy[mol.map[i]], positioned[i]) > 1e-4]
        if changed:
            self.notes.append('%s: IUPAC geometry adjusted %d hand coordinates (at most %.3f bonds)' % (sid, len(changed), max(changed)))
        xy = {mol.map[i]: p for i, p in positioned.items()}
        if sp['xy']:
            # hand coordinates keep their shape, but not on top of what the panel already holds (the molecule placed
            # first can move when its own hand coordinates are made regular)
            cleared = self.clear_of_panel(sid, xy, pads=True, hbond_ends=True)
            if cleared is not xy:
                self.notes.append('%s: hand coordinates stood closer than half a bond to another molecule; moved it clear' % sid)
            xy = cleared
        self.xy[sid] = xy

    def reference(self, sid):
        """The placed appearance of the same molecule this one copies: the nearest earlier panel, else the nearest later one."""
        sp = self.sp[sid]
        here = self.pidx[sp['panel']]
        cands = []
        for p in self.panels:
            other = '%s.%s' % (p['id'], sp['key'])
            if other != sid and other in self.xy:
                k = self.pidx[p['id']]
                cands.append(((0 if k < here else 1, abs(k - here)), other))
        return min(cands)[1] if cands else None

    def before(self, sid):
        """Where this molecule's atoms last stood in an earlier panel, whatever molecule they were part of there (a
        transition state, drawn in perspective, is skipped): ({map: place}, [(map, place of an atom it was bonded to
        there and is not bonded to in this molecule, that atom's map)])."""
        sp, mol = self.sp[sid], self.mol[sid]
        prev = self.prev_panel.get(sp['panel'])
        was, broken, seen = {}, [], {sp['panel']}
        while prev is not None and prev not in seen and not was:     # reaction arrows may close a loop
            seen.add(prev)
            for key in self.panels[self.pidx[prev]]['species']:
                other = '%s.%s' % (prev, key)
                if other not in self.xy or self.sp[other].get('ts'):
                    continue
                for m in mol.idx:
                    if m in self.xy[other]:
                        was[m] = self.xy[other][m]
                        broken += [(m, self.xy[other][q], q) for q in self.mol[other].nb_maps(m) if q not in mol.idx]
            prev = self.prev_panel.get(prev)
        return was, broken

    def history(self, sid):
        """A molecule that is new under this name but whose atoms stood in an earlier panel as part of other molecules
        (a leaving group, a released product): its own depiction laid over the places those atoms had, then moved out
        along the broken bond until it is a contact distance from the atom it left and clear of what stands in the
        panel (the group that took its place). None without such atoms or when the spec places it."""
        sp, mol = self.sp[sid], self.mol[sid]
        if sp['near'] is not None:
            return None
        was, broken = self.before(sid)
        if not was:
            return None
        kept = None if sp.get('intra_hbonds') else self.kept_shape(sid, was)
        pos = kept or self.refit(sid, was)
        if broken:
            m, q, _ = broken[0]
            if kept:
                # it keeps its orientation and steps sideways, away from where the atoms it broke from stood, until those
                # atoms (the leaving OH) can stay there at a contact distance from every atom of it
                away = (1.0 if pos[m][0] >= q[0] else -1.0, 0.0)
                room = lambda at: all(dist(p, qq) >= CONTACT_BASE + 0.2 * mol.labeled(mol.idx[k]) + 0.2 - 1e-9
                                      for k, p in at.items() if k != m for _, qq, _ in broken)
                step = 0.0
                self.vacated |= {(sp['panel'], qm_) for _, _, qm_ in broken}
            elif (sp['panel'], m) in self.vacated:       # the product stepped aside for it: it stays, clear of the rest
                return self.clear_of_panel(sid, pos)
            else:
                d = dist(pos[m], q) or 1.0
                away = ((pos[m][0] - q[0]) / d, (pos[m][1] - q[1]) / d)
                room = None
                step = max(0.0, CONTACT_BASE + 0.2 * mol.labeled(mol.idx[m]) - d)
            crowd = self.crowding(sid)
            for _ in range(24):
                moved = {k: add(p, away, step) for k, p in pos.items()}
                if crowd(moved) <= 1e-9 and (room is None or room(moved)):
                    break
                step += 0.25
            pos = moved
        return pos

    def anchor_frame(self, sid):
        shape, chain = self.shape(sid)
        mol = self.mol[sid]
        sp = self.sp[sid]
        shape = transform(shape, sp['rotate'], False, sp['flip'])
        x0, y0 = shape[mol.map[0]]
        self.chain[sid] = chain
        return {m: (x - x0, y - y0) for m, (x, y) in shape.items()}

    def derive(self, sid, ref_sid):
        """Copy the placement of another appearance of the same molecule; re-place the reacting atoms' end groups."""
        mol, rmol = self.mol[sid], self.mol[ref_sid]
        ref = self.xy[ref_sid]
        common = [m for m in mol.idx if m in rmol.idx and m in ref]
        if self.sp[sid].get('intra_hbonds') and len(common) >= 2:
            # a hydrogen bond inside it shapes this molecule: its own drawing, laid over the earlier places
            return self.refit(sid, {m: ref[m] for m in common})
        pos = {m: ref[m] for m in common}
        self.chain[sid] = [m for m in self.chain.get(ref_sid, []) if m in mol.idx]
        reacting = [m for m in common if mol.nb_maps(m) != rmol.nb_maps(m)]

        def settled(r):
            """A reacting atom with at most three bonds whose bonds from before already stand at the standard angle:
            its end groups stay where they were, and a new bond takes the open place.  So does a trigonal atom that gains
            a fourth bond with one or three terminal neighbors (an amine N methylated where its lone pair stood): the
            new bond halves one of its 120-degree gaps, the standard 60/60/120/120 four-bond layout."""
            nb = mol.nb_maps(r)
            before = [n for n in nb if n in rmol.nb_maps(r) and n in ref]
            if len(nb) == 4 and len(before) == 3 and mol.idx[r] not in mol.stereogenic and not mol.in_ring[mol.idx[r]]:
                if sum(1 for n in nb if len(mol.nb_maps(n)) == 1) not in (1, 3):
                    return False
            elif len(nb) > 3 or not before:
                return False
            if len(before) == 1:
                return True
            dirs = sorted(ang(ref[n][0] - ref[r][0], ref[n][1] - ref[r][1]) for n in before)
            turns = [(dirs[(k + 1) % len(dirs)] - dirs[k]) % 360 for k in range(len(dirs))]
            want = 180 if len(nb) == 2 and mol.is_linear(mol.idx[r]) else 120
            return abs(min(turns) - want) <= 10 if len(before) == 2 else all(abs(t - 120) <= 10 for t in turns)
        free_terminal = {m for m in common for r in reacting
                         if mol.nb_maps(m) == {r} and len(mol.nb_maps(r)) > 1 and m not in reacting and not settled(r)}
        self.kept_centers[sid] = {r for r in reacting if len(mol.nb_maps(r)) == 4 and settled(r)}
        for m in free_terminal:
            pos.pop(m)
        old = {m: ref[m] for m in free_terminal}
        for r in sorted(reacting, key=lambda r: -len([n for n in mol.nb_maps(r) if n not in pos])):
            fixed = [n for n in mol.nb_maps(r) if n in pos]
            free = sorted(n for n in mol.nb_maps(r) if n not in pos)
            if not free:
                continue
            self.assign_slots(sid, r, fixed, free, pos, old)
        # new atoms further out (attached to new atoms): grow them
        for _ in range(mol.n):
            todo = [m for m in mol.idx if m not in pos]
            if not todo:
                break
            progressed = False
            for m in todo:
                placed_nb = [n for n in mol.nb_maps(m) if n in pos]
                if placed_nb:
                    r = placed_nb[0]
                    fixed = [n for n in mol.nb_maps(r) if n in pos]
                    free = sorted(n for n in mol.nb_maps(r) if n not in pos)
                    self.assign_slots(sid, r, fixed, free, pos, old)
                    progressed = True
                    break
            if not progressed:
                shape, _ = self.shape(sid)
                for m in todo:
                    pos[m] = shape[m]
                break
        if not mol.sp.get('ts') and any(abs(dist(pos[mol.map[i]], pos[mol.map[j]]) - 1.0) > 0.45 for i, j in map(tuple, mol.order)):
            # a bond formed between atoms that were far apart (a rearrangement): draw the molecule anew and lay it over
            # the reference by the atoms whose neighbors did not change
            kept = [m for m in common if m not in reacting]
            return self.keep_ez(sid, self.keep_sides(sid, rmol, self.refit(sid, {m: ref[m] for m in (kept if len(kept) >= 2 else common)})))
        return self.keep_ez(sid, self.keep_sides(sid, rmol, pos))

    def keep_ez(self, sid, pos):
        """A double bond whose E/Z the inherited places got wrong (one formed where a single bond stood): the smaller side
        of one of its ends turned over that bond, as kept_shape does.  Ring bonds and hand-placed atoms stay."""
        mol = self.mol[sid]
        want = _stereo_wanted(mol)
        if not want or any(m not in pos for m in mol.idx):
            return pos
        at = {mol.idx[m]: p for m, p in pos.items() if m in mol.idx}
        hand = {mol.idx[m] for m in self.sp[sid]['xy'] if m in mol.idx}
        for _ in range(len(want)):
            got = _stereo_drawn(mol, at)
            wrong = [e for e, c in want.items() if got.get(e) not in (None, c) and e not in mol.ring_bonds]
            if not wrong:
                break
            a, b = tuple(wrong[0])
            sides = []
            for end, other in ((a, b), (b, a)):
                atoms = set()
                for n in mol.adj[end]:
                    if n != other:
                        atoms |= set(mol.subtree(n, end))
                if atoms and not atoms & hand and other not in atoms:
                    sides.append((len(atoms), end, other, atoms))
            if not sides:
                break
            _, end, other, atoms = min(sides, key=lambda t: t[0])
            (px, py), (qx, qy) = at[end], at[other]
            ux, uy = qx - px, qy - py
            n2 = ux * ux + uy * uy or 1.0
            for k in atoms:
                x, y = at[k]
                t = ((x - px) * ux + (y - py) * uy) / n2
                at[k] = (2 * (px + t * ux) - x, 2 * (py + t * uy) - y)
            self.notes.append('%s: turned %s over the %d=%d bond for its E/Z' % (
                sid, ', '.join(str(mol.map[k]) for k in sorted(atoms)), mol.map[end], mol.map[other]))
        return {mol.map[i]: p for i, p in at.items()}

    def keep_sides(self, sid, rmol, pos):
        """A group that joins this molecule here and was part of another molecule in the previous panel (CO2 taken up
        as a carboxylate) keeps the side each of its atoms had there: mirrored across its new bond when that fits better."""
        mol = self.mol[sid]
        for c in mol.idx:
            if c in rmol.idx:
                continue
            for p in mol.nb_maps(c):
                if p not in rmol.idx or frozenset((mol.idx[p], mol.idx[c])) in mol.ring_bonds:
                    continue
                group, stack = {c}, [c]
                while stack:
                    for n in mol.nb_maps(stack.pop()):
                        if n != p and n not in group:
                            group.add(n)
                            stack.append(n)
                was = {m: self.came_from(sid, m) for m in group}
                if (len(group) < 3 or any(m in rmol.idx or was[m] is None or m not in pos for m in group)
                        or any(mol.idx[m] in mol.stereogenic for m in group)):
                    continue
                (px, py), (cx, cy) = pos[p], pos[c]
                ux, uy = cx - px, cy - py
                n2 = ux * ux + uy * uy or 1.0
                def mirror(q):
                    t = ((q[0] - px) * ux + (q[1] - py) * uy) / n2
                    fx, fy = px + t * ux, py + t * uy
                    return (2 * fx - q[0], 2 * fy - q[1])
                def miss(at):
                    return sum(adiff(ang(at[m][0] - at[c][0], at[m][1] - at[c][1]),
                                     ang(was[m][0] - was[c][0], was[m][1] - was[c][1])) for m in group if m != c)
                flipped = {m: mirror(pos[m]) for m in group}
                if miss(flipped) < miss(pos) - 1e-6:
                    pos.update(flipped)
        return pos

    def kept_shape(self, sid, was):
        """The places the atoms had, when every atom of this molecule stood in the earlier panel, each bond there is a
        bond length and a double bond's E/Z needs the smaller side of one end turned over that bond (the CH3 of
        crotonaldehyde to where the leaving OH stood): then its own depiction would be tilted to fit.  None otherwise
        (refit then lays the depiction over the places)."""
        sp, mol = self.sp[sid], self.mol[sid]
        if mol.ring_bonds or any(m not in was for m in mol.idx):
            return None
        pos = {mol.idx[m]: p for m, p in was.items() if m in mol.idx}
        if any(abs(dist(pos[i], pos[j]) - 1.0) > 0.45 for i, j in map(tuple, mol.order)):
            return None
        want = _stereo_wanted(mol)
        if not want:
            return None
        turned = []
        for _ in range(len(want) + 1):
            got = _stereo_drawn(mol, pos)
            wrong = [e for e, c in want.items() if got.get(e) not in (None, c)]
            if not wrong:
                if not turned:
                    return None
                self.notes += ['%s: kept the places of the earlier panel; turned %s over the %d=%d bond for its E/Z' % t
                               for t in turned]
                _, chain = depict(sp)
                self.chain[sid] = chain
                return {mol.map[i]: p for i, p in pos.items()}
            a, b = tuple(wrong[0])
            sides = []
            for end, other in ((a, b), (b, a)):
                atoms = set()
                for n in mol.adj[end]:
                    if n != other:
                        atoms |= set(mol.subtree(n, end))
                sides.append((len(atoms), end, other, atoms))
            _, end, other, atoms = min(sides, key=lambda t: t[0])
            turned.append((sid, ', '.join(str(mol.map[k]) for k in sorted(atoms)), mol.map[end], mol.map[other]))
            (px, py), (qx, qy) = pos[end], pos[other]
            ux, uy = qx - px, qy - py
            n2 = ux * ux + uy * uy or 1.0
            for k in atoms:
                x, y = pos[k]
                t = ((x - px) * ux + (y - py) * uy) / n2
                pos[k] = (2 * (px + t * ux) - x, 2 * (py + t * uy) - y)
        return None

    def refit(self, sid, target):
        """This molecule's own depiction (from this panel's bonds, not the shared one), turned (and mirrored if that fits
        better) and moved onto the target points."""
        sp = self.sp[sid]
        shape, chain = depict(sp)
        shape = transform(shape, sp['rotate'], False, sp['flip'])
        self.chain[sid] = chain
        keys = [m for m in target if m in shape]
        best = None
        for mirror in (False, True):
            src = {m: (x, -y if mirror else y) for m, (x, y) in shape.items()}
            ca = tuple(sum(src[m][k] for m in keys) / len(keys) for k in (0, 1))
            cb = tuple(sum(target[m][k] for m in keys) / len(keys) for k in (0, 1))
            sx = sum((src[m][0] - ca[0]) * (target[m][1] - cb[1]) - (src[m][1] - ca[1]) * (target[m][0] - cb[0]) for m in keys)
            cx = sum((src[m][0] - ca[0]) * (target[m][0] - cb[0]) + (src[m][1] - ca[1]) * (target[m][1] - cb[1]) for m in keys)
            t = math.atan2(sx, cx)
            c, s = math.cos(t), math.sin(t)
            out = {m: (cb[0] + (x - ca[0]) * c - (y - ca[1]) * s, cb[1] + (x - ca[0]) * s + (y - ca[1]) * c) for m, (x, y) in src.items()}
            err = sum(dist(out[m], target[m]) ** 2 for m in keys)
            if best is None or err < best[0] - 1e-9:
                best = (err, out)
        # a molecule laid over its earlier places keeps that turn when a curved arrow reaches another molecule from it:
        # turning it would break the room the arrow has to its partner
        return best[1] if sp['xy'] or self.arrow_ends_to_others(sid) else straighten(self.mol[sid], best[1])

    def arrow_ends_to_others(self, sid):
        """The atoms (maps) of this molecule at the ends of curved arrows that reach another molecule in its panel
        (a forming bond between two molecules counts both of its atoms)."""
        out = set()
        for ar in self.panels[self.pidx[self.sp[sid]['panel']]]['arrows']:
            pairs = []
            for e in (ar['from'], ar['to']):
                for m in e.get('members') or [e]:
                    pairs += [(m['sid'], m['atom'])] + ([(m['sid'], m['atom2'])] if m.get('atom2') is not None else [])
            if len({x for x, _ in pairs}) > 1:
                out.update(a for x, a in pairs if x == sid)
        return out

    def assign_slots(self, sid, r, fixed, free, pos, old):
        rp = pos[r]
        fdirs = [ang(pos[n][0] - rp[0], pos[n][1] - rp[1]) for n in fixed]
        cands = slot_dirs(fdirs, len(free))
        if len(fdirs) == 3 and len(free) == 1:          # a fourth bond on a trigonal atom: any of its wide gaps
            cands = [(st + g / 2.0) % 360 for st, g in gaps(fdirs) if g >= 100] or cands
        if len(cands) < len(free):
            cands = cands + [(c + 180.0) % 360 for c in cands]
        cand_sets = [cands]
        if len(fdirs) == 1 and len(free) == 3:          # the four-bond layout, turning either way
            cand_sets.append(slot_dirs(fdirs, 3, -1))
        pid = self.sp[sid]['panel']
        panel = self.panels[self.pidx[pid]]
        prefer = {}
        for n in free:
            if n in old:
                prefer[n] = [(ang(old[n][0] - rp[0], old[n][1] - rp[1]), 0.3)]
            else:
                came = self.came_from(sid, n)
                if came is not None:
                    prefer[n] = [(ang(came[0] - rp[0], came[1] - rp[1]), 1.0)]
            base = self.base_of(sid, n)
            if base is not None:
                prefer.setdefault(n, []).append((ang(base[0] - rp[0], base[1] - rp[1]), 1.0))
        others = [p for p, _ in self.panel_points(pid, skip=sid)] + [pos[m] for m in pos if m != r]
        for key in panel['species']:               # molecules not placed yet will take the place they had before
            other = '%s.%s' % (pid, key)
            if other != sid and other not in self.xy and self.reference(other):
                others += [p for p, _ in self.points_of(self.reference(other), self.xy[self.reference(other)])]
        best = None
        for perm in (p for cs in cand_sets for p in itertools.permutations(cs, len(free))):
            cost = 0.0
            dirs = dict(zip(free, perm))
            for n, dg in dirs.items():
                for want, w in prefer.get(n, []):
                    cost += w * adiff(dg, want)
                p = add(rp, vec(dg))
                for q in others:
                    d = dist(p, q)
                    if d < 0.9:
                        cost += 200.0 * (0.9 - d)
            alld = dict(zip(fixed, fdirs))
            alld.update(dirs)
            for ar in panel['arrows']:
                ends = [e for e in (ar['from'], ar['to']) if e['sid'] == sid and e['atom2'] is not None and r in (e['atom'], e['atom2'])]
                if len(ends) == 2:
                    x = ends[0]['atom2'] if ends[0]['atom'] == r else ends[0]['atom']
                    y = ends[1]['atom2'] if ends[1]['atom'] == r else ends[1]['atom']
                    if x in alld and y in alld:
                        a1, a2 = alld[x], alld[y]
                        span = (a2 - a1) % 360.0
                        lo, size = (a1, span) if span <= 180 else (a2, 360.0 - span)
                        between = [n for n, d in alld.items() if n not in (x, y) and 0 < (d - lo) % 360.0 < size]
                        if between:
                            cost += 100.0
            if best is None or cost < best[0] - 1e-9:
                best = (cost, dirs)
        for n, dg in best[1].items():
            pos[n] = add(rp, vec(dg))
            self.grow_new(sid, n, r, dg, pos)

    def grow_new(self, sid, n, parent, dg, pos):
        """Place the not-yet-placed atoms behind n (a group that is new here) as a zigzag pointing along dg."""
        mol = self.mol[sid]
        ipos = {mol.idx[m]: p for m, p in pos.items()}
        _grow(mol, mol.idx[n], mol.idx[parent], dg, ipos)
        for i, p in ipos.items():
            pos.setdefault(mol.map[i], p)

    def came_from(self, sid, m):
        """Where atom m was in the previous panel when it belonged to another molecule there."""
        sp = self.sp[sid]
        prev = self.prev_panel.get(sp['panel'])
        if prev is None:
            return None
        for key in self.panels[self.pidx[prev]]['species']:
            other = '%s.%s' % (prev, key)
            if key != sp['key'] and other in self.xy and m in self.xy[other]:
                return self.xy[other][m]
        return None

    def base_of(self, sid, m):
        """For an H that a curved arrow of this panel takes (arrow ending at it from another molecule): where that arrow starts."""
        sp = self.sp[sid]
        if self.mol[sid].atoms[self.mol[sid].idx[m]]['symbol'] != 'H':
            return None
        for ar in self.panels[self.pidx[sp['panel']]]['arrows']:
            t, f = ar['to'], ar['from']
            if t['sid'] == sid and t['atom'] == m and t['atom2'] is None and f['sid'] != sid and f['sid'] in self.xy:
                return self.xy[f['sid']][f['atom']]
        return None

    # ---- contacts between molecules of a panel
    def contacts(self, pid):
        """[(sid_a, end_a, sid_b, end_b, weight)] with end = (map, map2 or None)."""
        panel = self.panels[self.pidx[pid]]
        out = []
        nxt = self.next_panel.get(pid)
        if nxt is not None:
            where = {}
            for key in panel['species']:
                sid = '%s.%s' % (pid, key)
                for m in self.mol[sid].idx:
                    where.setdefault(m, sid)
            for key in self.panels[self.pidx[nxt]]['species']:
                nmol = self.mol['%s.%s' % (nxt, key)]
                for e in nmol.order:
                    a, b = (nmol.map[i] for i in e)
                    sa, sb = where.get(a), where.get(b)
                    if sa and sb and sa != sb:
                        out.append((sa, (a, None), sb, (b, None), 3.0))
        for ar in panel['arrows']:
            f, t = ar['from'], ar['to']
            if f['sid'] != t['sid']:
                w = 2.0 if f['atom2'] is None and t['atom2'] is None else 1.0
                out.append((f['sid'], (f['atom'], f['atom2']), t['sid'], (t['atom'], t['atom2']), w))
        for h in panel['hbonds']:
            if h[0]['sid'] != h[1]['sid']:
                out.append((h[0]['sid'], (h[0]['atom'], None), h[1]['sid'], (h[1]['atom'], None), HBOND_WEIGHT))
        have = {frozenset(((sa, ea), (sb, eb))) for sa, ea, sb, eb, _ in out}
        for sa, ma, sb, mb in self.open_gaps(pid):
            if frozenset(((sa, (ma, None)), (sb, (mb, None)))) not in have:
                out.append((sa, (ma, None), sb, (mb, None), 3.0))
        return out

    def end_point(self, sid, end, xy):
        m, m2 = end
        if m2 is None:
            return xy[m]
        return ((xy[m][0] + xy[m2][0]) / 2.0, (xy[m][1] + xy[m2][1]) / 2.0)

    def open_dirs(self, sid, end, xy):
        """Free directions at a contact end: bisectors of the gaps between its bonds (an atom), or the bond's normals."""
        mol = self.mol[sid]
        m, m2 = end
        if m2 is not None:
            a, b = xy[m], xy[m2]
            d = ang(b[0] - a[0], b[1] - a[1])
            return [(d + 90) % 360, (d - 90) % 360]
        p = xy[m]
        dirs = [ang(xy[n][0] - p[0], xy[n][1] - p[1]) for n in mol.nb_maps(m) if n in xy]
        if not dirs:
            return None
        if len(dirs) == 1:
            return [(dirs[0] + 180.0) % 360]
        gs = gaps(dirs)
        top = max(g for _, g in gs)
        return [(s + g / 2.0) % 360 for s, g in gs if g >= top - 1.0]

    def contact_dist(self, sid_a, end_a, sid_b, end_b):
        n = 0
        for sid, (m, m2) in ((sid_a, end_a), (sid_b, end_b)):
            if m2 is None and self.mol[sid].labeled(self.mol[sid].idx[m]):
                n += 1
        ends = {(sid_a, m) for m in end_a if m is not None} | {(sid_b, m) for m in end_b if m is not None}
        def refs(ar):                            # the atoms an arrow joins (both atoms of a forming bond it points to)
            return {(r['sid'], r[k]) for e in (ar['from'], ar['to']) for r in (e.get('members') or [e])
                    for k in ('atom', 'atom2') if r[k] is not None}
        hook = any(ar.get('electrons') == 1 and {sid_a, sid_b} <= {s for s, _ in refs(ar)} and refs(ar) & ends
                   for ar in self.panels[self.pidx[self.sp[sid_a]['panel']]]['arrows'])
        return CONTACT_BASE + 0.2 * n + (FISHHOOK_ROOM if hook else 0.0)

    def open_gaps(self, pid):
        """[(sid_a, map_a, sid_b, map_b)]: forming bonds between two molecules whose arrows point at the middle of the
        gap (fishhooks, or a pair from a bond whose atoms are both outside the gap), not at a receiving atom."""
        out = []
        for ar in self.panels[self.pidx[pid]]['arrows']:
            ms = ar['to'].get('members') or []
            if len(ms) != 2 or ms[0]['sid'] == ms[1]['sid']:
                continue
            src = {(ar['from']['sid'], ar['from'][k]) for k in ('atom', 'atom2') if ar['from'][k] is not None}
            if ar['electrons'] == 2 and sum((m['sid'], m['atom']) not in src for m in ms) == 1:
                continue                         # the head goes to the atom that receives the pair
            g = (ms[0]['sid'], ms[0]['atom'], ms[1]['sid'], ms[1]['atom'])
            if g not in out:
                out.append(g)
        return out

    def pair_misread(self, sid, xy):
        """True when the middle of an open gap of this molecule (open_gaps) is nearer to another atom of the two
        molecules than to the gap's own atoms: the arrow heads there read as pointing at that atom."""
        for sa, ma, sb, mb in self.open_gaps(self.sp[sid]['panel']):
            if sid not in (sa, sb):
                continue
            (sid_, m), (osid, om) = ((sa, ma), (sb, mb)) if sa == sid else ((sb, mb), (sa, ma))
            if osid not in self.xy:
                continue
            p, q = xy[m], self.xy[osid][om]
            mid = ((p[0] + q[0]) / 2.0, (p[1] + q[1]) / 2.0)
            own = dist(p, q) / 2.0
            others = [v for k, v in xy.items() if k != m] + [v for k, v in self.xy[osid].items() if k != om]
            if others and min(dist(mid, v) for v in others) < own:
                return True
        return False

    def place_by_contact(self, sid, links):
        """links: [(my_end, other_sid, other_end, weight)].  Returns the best placement {map: (x, y)}."""
        sp = self.sp[sid]
        shape, chain = self.shape(sid)
        self.chain[sid] = chain
        shape = transform(shape, sp['rotate'], False, sp['flip'])
        fixed_orientation = bool(sp['flip']) or bool(sp['rotate'])
        links = sorted(links, key=lambda l: -l[3])
        my_end, osid, oend, weight = links[0]
        apt = self.end_point(osid, oend, self.xy[osid])
        ua = self.open_dirs(osid, oend, self.xy[osid])
        d = self.contact_dist(osid, oend, sid, my_end)
        pid = sp['panel']
        other_ink = self.panel_ink(pid, skip=sid)
        partner_ink = self.ink(osid, self.xy[osid])
        placed_sids = [o for o in ('%s.%s' % (pid, k) for k in self.panels[self.pidx[pid]]['species']) if o in self.xy and o != sid]
        base_box = self.bbox(placed_sids) if placed_sids else None
        best = None
        rots = [0] if fixed_orientation else list(range(0, 360, 30))
        mirrors = [False] if fixed_orientation else [False, True]
        for rot in rots:
            for mirror in mirrors:
                cand = transform(shape, rot, mirror)
                ub = self.open_dirs(sid, my_end, cand)
                if ub is None:                      # a lone atom (ion, water O): any side; sit in front of the partner
                    ub = [(ua[0] + 180.0) % 360] if ua else [90.0]
                for ub_dir in ub:
                    bpt = self.end_point(sid, my_end, cand)
                    target = add(apt, vec(ub_dir), -d)
                    shift = (target[0] - bpt[0], target[1] - bpt[1])
                    xy = {m: (x + shift[0], y + shift[1]) for m, (x, y) in cand.items()}
                    score = 0.0
                    if ua:
                        face = ang(target[0] - apt[0], target[1] - apt[1])
                        score += 1.0 * min(adiff(face, u) for u in ua)
                    t = rot % 180
                    score += 2.0 * min(t, 180 - t)
                    score += 300.0 * self.shortfall(sid, xy, other_ink)
                    ex = self.expanded(sid, xy)
                    mine = [self.label_box(sid, m, ex) for m in ex]
                    for my2, osid2, oend2, w in links[1:]:
                        want = self.contact_dist(osid2, oend2, sid, my2)
                        got = dist(self.end_point(sid, my2, xy), self.end_point(osid2, oend2, self.xy[osid2]))
                        score += 10.0 * w * abs(got - want)
                    if base_box:
                        xs = [b[0] for b in mine] + [b[2] for b in mine] + [base_box[0], base_box[2]]
                        ys = [b[1] for b in mine] + [b[3] for b in mine] + [base_box[1], base_box[3]]
                        score += 1.5 * (max(xs) - min(xs)) * (max(ys) - min(ys)) ** 0.5
                    mol = self.mol[sid]
                    for m in sp['residue']:
                        i = mol.idx[m]
                        if mol.adj[i]:                  # a residue label reads beside its root: avoid a vertical root bond
                            q = xy[mol.map[mol.adj[i][0]]]
                            dv = ang(xy[m][0] - q[0], xy[m][1] - q[1])
                            if min(adiff(dv, 90), adiff(dv, 270)) < 20:
                                score += 40.0
                    key = (round(score, 6), rot, mirror)
                    if best is None or key < best[0]:
                        best = (key, xy, ub_dir)
        xy, ub_dir = best[1], best[2]
        # two an arrow or a forming bond joins: where their letters or bonds come within SPECIES_GAP of each other
        # (a written CH3 facing its attacker), the molecule steps out along the same line, by at most one bond length,
        # into no more crowding than it had
        if weight != HBOND_WEIGHT and self.shortfall(sid, xy, partner_ink) > 1e-9:
            base = self.shortfall(sid, xy, other_ink)
            for k in range(1, 11):
                out = vec(ub_dir)
                moved = {m: (x - out[0] * k / 10.0, y - out[1] * k / 10.0) for m, (x, y) in xy.items()}
                if self.shortfall(sid, moved, partner_ink) <= 1e-9 and self.shortfall(sid, moved, other_ink) <= base + 1e-9:
                    return moved
        return xy

    def crowding(self, sid):
        """A function xy -> how much this molecule, placed at xy, runs into what stands in its panel (atoms, bonds, label
        boxes, frames it is not in); 0 when it is clear."""
        sp = self.sp[sid]
        other_ink = self.panel_ink(sp['panel'], skip=sid)
        # frames drawn around groups of this panel that this species is not in: stay clear of them (and out of them)
        frames = []
        for fr in self.panels[self.pidx[sp['panel']]].get('boxes') or []:
            fs = ['%s.%s' % (sp['panel'], k) for k in fr['species']]
            if sid not in fs and all(o in self.xy for o in fs):
                b = self.bbox(fs)
                frames.append((b[0] - fr['pad'] - 0.3, b[1] - fr['pad'] - 0.3, b[2] + fr['pad'] + 0.3, b[3] + fr['pad'] + 0.3))

        def measure(xy):
            coll = self.shortfall(sid, xy, other_ink)
            ex = self.expanded(sid, xy)
            for b1 in (self.label_box(sid, m, ex) for m in ex):
                for b2 in frames:
                    ox = min(b1[2], b2[2]) - max(b1[0], b2[0])
                    oy = min(b1[3], b2[3]) - max(b1[1], b2[1])
                    if ox > 0 and oy > 0:
                        coll += 1.0 + ox * oy
            return coll
        return measure

    def follow_contact(self, sid, xy, links):
        """A molecule kept where it stood in an earlier panel (derive) follows the atom it touches there (a hydrogen
        bond's H, an arrow's end) when that atom has moved since: shifted as that atom was, keeping its side, or, where
        that runs into what is placed, turned and shifted as that atom and the atoms bonded to it were.  links:
        [(my_end, other_sid, other_end, weight)], the strongest first.  A molecule the spec places (offset, xy, near)
        stays as placed."""
        sp = self.sp[sid]
        if sp.get('offset') or sp['xy'] or sp['near'] is not None:
            return xy
        ref_panel = self.sp[self.reference(sid)]['panel']
        for _, osid, oend, _ in sorted(links, key=lambda l: -l[3]):
            oref = '%s.%s' % (ref_panel, self.sp[osid]['key'])
            m = oend[0]
            if oref not in self.xy or m not in self.xy[oref] or m not in self.xy[osid]:
                continue
            if dist(self.xy[oref][m], self.xy[osid][m]) <= 0.3:
                return xy
            dx, dy = self.xy[osid][m][0] - self.xy[oref][m][0], self.xy[osid][m][1] - self.xy[oref][m][1]
            shifted = {k: (p[0] + dx, p[1] + dy) for k, p in xy.items()}
            if self.shortfall(sid, shifted, self.panel_ink(self.sp[sid]['panel'], skip=sid)) <= 1e-9:
                return shifted
            pts = [m] + [n for n in self.mol[osid].nb_maps(m) if n in self.xy[oref] and n in self.xy[osid]]
            go = rigid_fit({k: self.xy[oref][k] for k in pts}, {k: self.xy[osid][k] for k in pts})
            return {k: go(p) for k, p in xy.items()}
        return xy

    def hbond_ends(self, sid):
        """(own atoms, {other species: atoms}) at the two ends of sid's hydrogen bonds to other molecules."""
        own, ends = set(), {}
        for a, b in self.panels[self.pidx[self.sp[sid]['panel']]]['hbonds']:
            for x, y in ((a, b), (b, a)):
                if x['sid'] == sid and y['sid'] != sid:
                    own.add(x['atom'])
                    ends.setdefault(y['sid'], set()).add(y['atom'])
        return own, ends

    def clear_of_panel(self, sid, xy, pads=False, hbond_ends=False):
        """A molecule kept where it stood in another panel (derive) moves only when it now stands closer than SPECIES_GAP
        to what is placed in this panel: the shortest shift (0.1 to 2 bonds, every 30 degrees) that clears it, else the
        one that leaves it least short.  pads: count the dots of reacting pairs (see ink).  hbond_ends: the two end atoms
        of a hydrogen bond to another molecule are left out, as check_rules.det_species_gap leaves them out."""
        pid = self.sp[sid]['panel']
        own, ends = self.hbond_ends(sid) if hbond_ends else (set(), {})
        other = self.panel_ink(pid, skip=sid, pads=pads, ends=ends)
        if self.shortfall(sid, xy, other, pads, own) <= 1e-9:
            return xy
        best = None
        for k in range(1, 21):
            d = k / 10.0
            for dg in range(0, 360, 30):
                u = vec(dg)
                moved = {m: (x + u[0] * d, y + u[1] * d) for m, (x, y) in xy.items()}
                short = self.shortfall(sid, moved, other, pads, own)
                key = (round(short, 6), d)
                if best is None or key < best[0]:
                    best = (key, moved)
            if best[0][0] <= 1e-9:
                break
        return best[1]

    def place_near(self, sid):
        sp = self.sp[sid]
        near = sp['near']
        osid = '%s.%s' % (sp['panel'], near['species'])
        if osid not in self.xy:
            return None
        shape, chain = self.shape(sid)
        self.chain[sid] = chain
        shape = transform(shape, sp['rotate'], False, sp['flip'])
        cx = sum(p[0] for p in shape.values()) / len(shape)
        cy = sum(p[1] for p in shape.values()) / len(shape)
        apt = self.xy[osid][near['atom']]
        d = float(near['dist']) if near.get('dist') is not None else NEAR_DIST
        dirs, dists = near_candidates(near.get('dir'), d)
        crowd = self.crowding(sid)
        best = None
        for k, dg in enumerate(dirs):
            for j, dd0 in enumerate(dists):
                c = add(apt, vec(dg), dd0)
                xy = {m: (x - cx + c[0], y - cy + c[1]) for m, (x, y) in shape.items()}
                coll = crowd(xy)
                # the asked place if it is free, else the free place nearest to it (direction steps count with distance steps)
                key = (coll > 1e-9, k + j, k, round(coll, 6))
                if best is None or key < best[0]:
                    best = (key, xy, dg, dd0)
        if best[0][1] > 0 and near.get('dir') is not None:
            self.notes.append('%s: the place asked by "near" (%.0f degrees, %.2f) is taken; put at %.0f degrees, %.2f'
                              % (sid, float(near['dir']), d, best[2] % 360.0, best[3]))
        return best[1]

    def place_free(self, sid):
        sp = self.sp[sid]
        shape, chain = self.shape(sid)
        self.chain[sid] = chain
        shape = transform(shape, sp['rotate'], False, sp['flip'])
        pid = sp['panel']
        placed = [o for o in ('%s.%s' % (pid, k) for k in self.panels[self.pidx[pid]]['species']) if o in self.xy and o != sid]
        tmp = dict(shape)
        self.xy[sid] = tmp
        box = self.bbox([sid])
        del self.xy[sid]
        if not placed:
            return {m: (x - box[0], y) for m, (x, y) in shape.items()}
        pb = self.bbox(placed)
        dx = pb[2] + 1.0 - box[0]
        dy = (pb[1] + pb[3]) / 2.0 - (box[1] + box[3]) / 2.0
        return {m: (x + dx, y + dy) for m, (x, y) in shape.items()}

    # ---- the whole figure
    def run(self):
        order = [p['id'] for p in self.panels]
        for pid in order:
            panel = self.panels[self.pidx[pid]]
            keys = panel['species']
            if not keys:
                continue
            anchor = '%s.%s' % (pid, keys[0])
            ref = self.reference(anchor)
            self.place(anchor, self.derive(anchor, ref) if ref else self.history(anchor) or self.anchor_frame(anchor))
            if not ref:
                self.fold_forming(anchor)
            # partners joined by contacts, nearest first
            links = self.contacts(pid)
            for _ in range(len(keys)):
                progress = False
                for key in keys[1:]:
                    sid = '%s.%s' % (pid, key)
                    if sid in self.xy:
                        continue
                    mine = []
                    for sa, ea, sb, eb, w in links:
                        if sa == sid and sb in self.xy:
                            mine.append((ea, sb, eb, w))
                        elif sb == sid and sa in self.xy:
                            mine.append((eb, sa, ea, w))
                    if not mine:
                        continue
                    ref = self.reference(sid)
                    # the retry that brings a molecule an arrow joins next to the arrow's other end places it there,
                    # also where it would keep its place from an earlier panel; it waits for that other molecule
                    if self.sp[sid]['near'] is not None and self.sp[sid].get('near_retry'):
                        near = self.place_near(sid)
                        if near is not None:
                            self.place(sid, near)
                            progress = True
                        continue
                    if ref and self.pidx[self.sp[ref]['panel']] < self.pidx[pid]:
                        kept = self.derive(sid, ref)
                        followed = self.follow_contact(sid, kept, mine)
                        if followed is not kept and self.shortfall(sid, followed, self.panel_ink(pid, skip=sid)) > 1e-9:
                            followed = self.place_by_contact(sid, mine)    # turned with its contact it runs into others
                        elif self.pair_misread(sid, followed):
                            followed = self.place_by_contact(sid, mine)    # its arrow's head would point at a third atom
                        self.place(sid, self.clear_of_panel(sid, followed))
                    else:
                        was = self.history(sid)
                        if was is not None and self.pair_misread(sid, was):
                            was = None                                 # its arrow's head would point at a third atom
                        self.place(sid, was or self.place_by_contact(sid, mine))
                    progress = True
                if not progress:
                    break
        for _ in range(2):
            for pid in order:
                for key in self.panels[self.pidx[pid]]['species']:
                    sid = '%s.%s' % (pid, key)
                    if sid in self.xy:
                        continue
                    ref = self.reference(sid)
                    if ref and not self.sp[sid].get('near_retry'):
                        self.place(sid, self.clear_of_panel(sid, self.derive(sid, ref)))
                    elif self.sp[sid]['near'] is not None:
                        xy = self.place_near(sid)
                        if xy is not None:
                            self.place(sid, xy)
        for pid in order:
            for key in self.panels[self.pidx[pid]]['species']:
                sid = '%s.%s' % (pid, key)
                if sid not in self.xy:
                    ref = self.reference(sid)
                    self.place(sid, self.clear_of_panel(sid, self.derive(sid, ref)) if ref else self.history(sid) or self.place_free(sid))
        self.fit_intra_hbonds()
        self.iupac_hbond_spacing()
        from layout_repair import seat_satellites      # residues held only by hydrogen bonds, where a dotted line crosses
        seat_satellites(self)
        from settle import settle      # one score for every panel decides the last places
        settle(self)
        return self.arrange()

    def fit_intra_hbonds(self):
        """A hydrogen bond inside one species (a residue's backbone N-H to an oxyanion of the same chain): on the path
        between the H and its acceptor, reflect the part beyond each atom about the bond toward the acceptor, or turn the
        branch holding the H by 120 degrees, keeping the change that brings the H to the dotted-bond span the IUPAC
        spacing asks for without crowding other atoms.  Hand-placed atoms do not move."""
        bpt = self.style['bond_length_pt']
        height = CAP * self.style['font_size_pt'] / bpt
        margin = self.style['label_margin_pt'] / bpt
        for panel in self.panels:
            for a, b in panel['hbonds']:
                if a['sid'] != b['sid']:
                    continue
                sid, mol = a['sid'], self.mol[a['sid']]
                hand = set(self.sp[sid]['xy'])
                h, acc = mol.idx[a['atom']], mol.idx[b['atom']]
                prev, todo = {h: None}, [h]
                while todo:
                    x = todo.pop(0)
                    for y in mol.adj[x]:
                        if y not in prev:
                            prev[y] = x
                            todo.append(y)
                if acc not in prev:
                    continue
                path = [acc]
                while path[-1] != h:
                    path.append(prev[path[-1]])
                path.reverse()                                # h ... acc
                others = [q for s2 in self.xy if s2 != sid and self.sp[s2]['panel'] == panel['id'] for q in self.xy[s2].values()]
                bonded = {frozenset(e) for e in mol.order}
                def crossed(p1, p2, q1, q2):
                    def side(o, a_, b_):
                        return (a_[0] - o[0]) * (b_[1] - o[1]) - (a_[1] - o[1]) * (b_[0] - o[0])
                    return (side(p1, p2, q1) * side(p1, p2, q2) < 0) and (side(q1, q2, p1) * side(q1, q2, p2) < 0)
                def wanted(xy):
                    pa, pb = xy[a['atom']], xy[b['atom']]
                    d = dist(pa, pb) or 1.0
                    u = ((pb[0] - pa[0]) / d, (pb[1] - pa[1]) / d)
                    cut = 0.0
                    for e in (a, b):
                        box = self.label_box(sid, e['atom'], xy)
                        if box is not None:
                            cut += (box[2] - box[0]) / 2 * abs(u[0]) + (box[3] - box[1]) / 2 * abs(u[1]) + margin
                    return 2 * height + cut + .05
                def score(xy):
                    d = dist(xy[a['atom']], xy[b['atom']])
                    w = wanted(xy)
                    crowd = 0.0
                    maps = list(xy)
                    for i, m in enumerate(maps):
                        for n in maps[i + 1:]:
                            if {m, n} == {a['atom'], b['atom']} or frozenset((mol.idx[m], mol.idx[n])) in bonded:
                                continue
                            crowd += max(0.0, 0.75 - dist(xy[m], xy[n])) * 20
                        crowd += sum(max(0.0, 0.9 - dist(xy[m], q)) * 20 for q in others)
                    # the dotted line crosses no bond of this species
                    ends = (xy[a['atom']], xy[b['atom']])
                    crowd += 10 * sum(crossed(ends[0], ends[1], xy[mol.map[i]], xy[mol.map[j]])
                                      for i, j in map(tuple, mol.order) if not {mol.map[i], mol.map[j]} & {a['atom'], b['atom']})
                    return max(0.0, d - w) + 5 * max(0.0, w - d) + crowd
                xy = self.fold_chain(sid, path, score, depth=3)
                if xy is not None:
                    self.xy[sid] = xy
                    self.notes.append('%s: turned toward its hydrogen bond %d-%d' % (sid, a['atom'], b['atom']))

    def fold_chain(self, sid, path, score, rigid_double=False, depth=0):
        """Fold species sid along path (atom indices from the end that moves to the other end): reflect the part beyond
        each atom about the bond toward the other end, or turn or reflect the branch holding the moving end, keeping each
        change that lowers score(xy); the folded coordinates, or None when nothing lowered it.  Ring bonds stay; with
        rigid_double, no change pivots at a double bond either (its E/Z stays).  Hand-placed atoms do not move.  depth:
        first every sequence of up to that many changes is scored (a fold whose single changes each score worse)."""
        mol = self.mol[sid]
        hand = set(self.sp[sid]['xy'])
        acc = path[-1]
        def reflect(xy, idxs, p, q):
            (px, py), (qx, qy) = xy[mol.map[p]], xy[mol.map[q]]
            ux, uy = qx - px, qy - py
            n2 = ux * ux + uy * uy or 1.0
            out = dict(xy)
            for k in idxs:
                x, y = xy[mol.map[k]]
                t = ((x - px) * ux + (y - py) * uy) / n2
                out[mol.map[k]] = (2 * (px + t * ux) - x, 2 * (py + t * uy) - y)
            return out
        def turn(xy, idxs, r, deg):
            c = xy[mol.map[r]]
            out = dict(xy)
            moved = transform({mol.map[k]: (xy[mol.map[k]][0] - c[0], xy[mol.map[k]][1] - c[1]) for k in idxs}, deg)
            out.update({m: (c[0] + x, c[1] + y) for m, (x, y) in moved.items()})
            return out
        def fixed(e):
            return e in mol.ring_bonds or rigid_double and mol.order.get(e, 1) >= 2
        def changes(xy, k):
            r, toward, back = path[k], path[k - 1], path[k + 1]
            beyond = [i for i in mol.subtree(r, back) if i != r]
            if acc in beyond or fixed(frozenset((r, back))):
                return []
            branch = mol.subtree(toward, r)
            cands = [reflect(xy, beyond, r, back)]
            if acc not in branch and not fixed(frozenset((r, toward))):
                cands += [reflect(xy, branch, r, back), turn(xy, branch, r, 120), turn(xy, branch, r, -120)]
            return cands
        xy = dict(self.xy[sid])
        best = score(xy)
        start = best
        level, seen = [xy], set()
        for _ in range(depth):
            nxt = []
            for here in level:
                for k in range(1, len(path) - 1):
                    for cand in changes(here, k):
                        key = tuple(sorted((m, round(q[0], 3), round(q[1], 3)) for m, q in cand.items()))
                        if key in seen or any(dist(cand[m], here[m]) > 1e-6 for m in hand):
                            continue
                        seen.add(key)
                        nxt.append(cand)
                        s_ = score(cand)
                        if s_ < best - 1e-6:
                            best, xy = s_, cand
            level = nxt
        for _ in range(4):
            improved = False
            for k in range(1, len(path) - 1):
                for cand in changes(xy, k):
                    if any(dist(cand[m], xy[m]) > 1e-6 for m in hand):
                        continue
                    s_ = score(cand)
                    if s_ < best - 1e-6:
                        best, xy, improved = s_, cand, True
            if not improved:
                break
        return xy if best < start - 1e-6 else None

    def fold_forming(self, sid):
        """A bond a curved arrow forms inside one molecule (a [3,3] shift, a ring closure) whose two atoms stand more than
        FORM_FOLD bond lengths apart: fold the chain between them (fold_chain) to bring them within FORM_NEAR, without
        crowding the molecule or crossing its own bonds, before its partners are placed by their contacts."""
        if self.sp[sid]['xy']:
            return
        mol = self.mol[sid]
        bonded = {frozenset(e) for e in mol.order}
        def crossed(p1, p2, q1, q2):
            def side(o, a_, b_):
                return (a_[0] - o[0]) * (b_[1] - o[1]) - (a_[1] - o[1]) * (b_[0] - o[0])
            return (side(p1, p2, q1) * side(p1, p2, q2) < 0) and (side(q1, q2, p1) * side(q1, q2, p2) < 0)
        for ar in self.panels[self.pidx[self.sp[sid]['panel']]]['arrows']:
            members = ar['to'].get('members') or []
            if len(members) != 2 or any(m['sid'] != sid for m in members):
                continue
            ma, mb = members[0]['atom'], members[1]['atom']
            if dist(self.xy[sid][ma], self.xy[sid][mb]) <= FORM_FOLD:
                continue
            def score(xy):
                d = dist(xy[ma], xy[mb])
                crowd = 0.0
                maps = list(xy)
                for i, m in enumerate(maps):
                    for n in maps[i + 1:]:
                        if {m, n} == {ma, mb} or frozenset((mol.idx[m], mol.idx[n])) in bonded:
                            continue
                        crowd += max(0.0, 0.9 - dist(xy[m], xy[n])) * 20
                crowd += 10 * sum(crossed(xy[ma], xy[mb], xy[mol.map[i]], xy[mol.map[j]])
                                  for i, j in map(tuple, mol.order) if not {mol.map[i], mol.map[j]} & {ma, mb})
                # the arrow drawn along the new bond needs its way clear: no other atom near the middle of the line
                (ax, ay), (bx, by) = xy[ma], xy[mb]
                for m in maps:
                    if m in (ma, mb):
                        continue
                    for t in (.25, .5, .75):
                        crowd += max(0.0, 0.5 - dist(xy[m], (ax + t * (bx - ax), ay + t * (by - ay)))) * 10
                return max(0.0, d - FORM_NEAR) + 5 * max(0.0, 1.2 - d) + crowd
            best = None
            for start, goal in ((ma, mb), (mb, ma)):      # the end on the chain moves; a ring end stays
                h, acc = mol.idx[start], mol.idx[goal]
                prev, todo = {h: None}, [h]
                while todo:
                    x = todo.pop(0)
                    for y in mol.adj[x]:
                        if y not in prev:
                            prev[y] = x
                            todo.append(y)
                if acc not in prev:
                    continue
                path = [acc]
                while path[-1] != h:
                    path.append(prev[path[-1]])
                path.reverse()
                xy = self.fold_chain(sid, path, score, rigid_double=True)
                if xy is not None and (best is None or score(xy) < score(best)):
                    best = xy
            if best is not None:
                self.xy[sid] = best
                self.notes.append('%s: folded toward its forming bond %d-%d' % (sid, ma, mb))

    def iupac_hbond_spacing(self):
        """Leave a two-label-height visible dotted span, moving whole species only."""
        bpt = self.style['bond_length_pt']
        height = CAP * self.style['font_size_pt'] / bpt
        margin = self.style['label_margin_pt'] / bpt
        for panel in self.panels:
            if not panel['species']:
                continue
            anchor = panel['id'] + '.' + panel['species'][0]
            original = {sid: next(iter(xy.values())) for sid, xy in self.xy.items() if self.sp[sid]['panel'] == panel['id']}
            for _ in range(24):
                changed = False
                for a, b in panel['hbonds']:
                    if a['sid'] == b['sid']:
                        continue
                    pa, pb = self.xy[a['sid']][a['atom']], self.xy[b['sid']][b['atom']]
                    length = dist(pa, pb)
                    if length < .01:
                        continue
                    u = ((pb[0] - pa[0]) / length, (pb[1] - pa[1]) / length)
                    cuts = []
                    for end, point in ((a, pa), (b, pb)):
                        box = self.label_box(end['sid'], end['atom'], self.xy[end['sid']])
                        if box is None:
                            cuts.append(0)
                        else:
                            # Projection is conservative for a rectangular glyph box.
                            cuts.append((box[2] - box[0]) / 2 * abs(u[0]) + (box[3] - box[1]) / 2 * abs(u[1]) + margin)
                    wanted = 2 * height + sum(cuts) + .05
                    if length >= wanted:
                        continue
                    movable = b if a['sid'] == anchor else a if b['sid'] == anchor else min((a, b), key=lambda e: self.mol[e['sid']].n)
                    sign = 1 if movable is b else -1
                    shift = (sign * u[0] * (wanted - length), sign * u[1] * (wanted - length))
                    self.xy[movable['sid']] = {m: add(p, shift) for m, p in self.xy[movable['sid']].items()}
                    changed = True
                if not changed:
                    break
            moved = [sid for sid, point in original.items() if dist(point, next(iter(self.xy[sid].values()))) > 1e-4]
            if moved:
                self.notes.append('panel %s: IUPAC dotted-bond span moved whole species %s' % (panel['id'], ', '.join(moved)))

    def anchor_midline(self, pid):
        keys = self.panels[self.pidx[pid]]['species']
        if not keys:
            return 0.0
        sid = '%s.%s' % (pid, keys[0])
        chain = [m for m in self.chain.get(sid, []) if m in self.xy[sid]] or list(self.xy[sid])
        ys = [self.xy[sid][m][1] for m in chain]
        return (min(ys) + max(ys)) / 2.0

    def place_texts(self, pid):
        """The panel's texts in the panel frame: first a "+" for each plus pair, in the middle of the gap; "xy" as given;
        "near" at the asked direction and distance when that is free of the panel's atoms, bonds and earlier texts, else
        at the free place nearest to it, a name preferring a place nearer its own molecule than any other (so that it
        does not read as the name of another molecule's atom).  Each gets its box."""
        from specutil import plain_text as plain
        bpt = self.style['bond_length_pt']
        p = self.panels[self.pidx[pid]]
        sids = ['%s.%s' % (pid, k) for k in p['species'] if '%s.%s' % (pid, k) in self.xy]
        pts = self.panel_points(pid)
        for a, b in p['hbonds']:
            pa, pb = self.xy[a['sid']][a['atom']], self.xy[b['sid']][b['atom']]
            count = max(2, int(dist(pa, pb) / .15))
            pts += [(add(pa, (pb[0] - pa[0], pb[1] - pa[1]), k / count), False) for k in range(count + 1)]
        boxes, names = [], []
        for sid in sids:
            ex = self.expanded(sid, self.xy[sid])
            boxes += [self.label_box(sid, m, ex) for m in ex]
            names += self.residue_boxes(sid, ex)
        out = []
        size = self.style['font_size_pt']
        w, h = text_width('+', size, bpt), CAP * size / bpt
        for a, b in self.plus_pairs(p):             # "+" in the middle of the gap, at the middle of the heights both reach
            ba, bb = self.bbox([a]), self.bbox([b])
            lo, hi = max(ba[1], bb[1]), min(ba[3], bb[3])    # the heights both reach, else halfway between them
            x, y = (ba[2] + bb[0]) / 2.0, (lo + hi) / 2.0 if lo <= hi else (ba[1] + ba[3] + bb[1] + bb[3]) / 4.0
            out.append({'text': '+', 'x': x, 'y': y, 'size': size, 'align': 'center', 'box': (x - w / 2.0, y - h / 2.0, x + w / 2.0, y + h / 2.0)})
        for t in p['texts']:
            w, h = text_width(plain(t['text']), t['size'], bpt), CAP * t['size'] / bpt
            shift = {'center': 0.0, 'left': w / 2.0, 'right': -w / 2.0}[t['align']]    # anchor x -> middle of the box: x + shift

            def box_at(x, y):
                return (x + shift - w / 2.0, y - h / 2.0, x + shift + w / 2.0, y + h / 2.0)
            if 'xy' in t:
                x, y = t['xy']
            else:
                r = t['near']
                apt = self.xy[r['sid']][r['atom']]
                # a name (letters or digits, not a "+") prefers a place nearer its own molecule than any other
                own_pts = [q for q, _ in self.points_of(r['sid'], self.xy[r['sid']])] \
                    if r['sid'] in self.xy and any(ch.isalnum() for ch in plain(t['text'])) else []
                other_pts = [q for sid in sids if sid != r['sid'] for q, _ in self.points_of(sid, self.xy[sid])]

                def gap_to(b, qs):
                    return min((math.hypot(max(b[0] - q[0], 0.0, q[0] - b[2]), max(b[1] - q[1], 0.0, q[1] - b[3]))
                                for q in qs), default=float('inf'))
                d = float(t['dist']) if t.get('dist') is not None else TEXT_NEAR_GAP
                dirs, dists = near_candidates(t.get('dir'), d)
                best = None
                for k, dg in enumerate(dirs):
                    for j, dd in enumerate(dists):
                        u = vec(dg)                   # dd is the gap from the atom to the near edge of the text
                        c = add(apt, u, dd + abs(u[0]) * w / 2.0 + abs(u[1]) * h / 2.0)
                        x, y = c[0] - shift, c[1]
                        b = box_at(x, y)
                        coll, near = 0.0, 0.0
                        for q, lab in pts:
                            g = max(b[0] - q[0], q[0] - b[2], b[1] - q[1], q[1] - b[3])
                            need = 0.45 if lab else 0.25
                            if g < need:
                                coll += (need - g) ** 2
                        for b2 in boxes + [o['box'] for o in out]:
                            ox = min(b[2], b2[2]) - max(b[0], b2[0])
                            oy = min(b[3], b2[3]) - max(b[1], b2[1])
                            if ox > -0.15 and oy > -0.15:
                                coll += (ox + 0.15) * (oy + 0.15) * 4
                        # other names (texts, residue names) TEXT_APART away, and atom labels at its height (read on
                        # as one string with it), wished but not required
                        for b2 in names + [o['box'] for o in out if o['text'] != '+']:
                            near += max(0.0, TEXT_APART - _box_gap(b, b2)) ** 2
                        for b2 in boxes:
                            if b2[2] - b2[0] > 0.3 and min(b[3], b2[3]) - max(b[1], b2[1]) > 0:
                                near += max(0.0, TEXT_APART - _box_gap(b, b2)) ** 2
                        # a free place nearest the asked one, apart from other names if any place is; when every place
                        # touches something, the least touched
                        foreign = bool(own_pts) and gap_to(b, other_pts) < gap_to(b, own_pts) - 1e-6
                        key = (coll > 1e-9, round(coll, 6) if coll > 1e-9 else 0.0, foreign, near > 1e-9,
                               round(near, 6) if near > 1e-9 else 0.0, k + j, k)
                        if best is None or key < best[0]:
                            best = (key, (x, y), dg, dd, k + j)
                (x, y) = best[1]
                if best[4] > 0 and t.get('dir') is not None:
                    self.notes.append('panel %s text %r: the place asked by "near" (%.0f degrees, %.2f) is taken; put at '
                                      '%.0f degrees, %.2f' % (pid, t['text'], float(t['dir']), d, best[2] % 360.0, best[3]))
            out.append({'text': t['text'], 'x': x, 'y': y, 'size': t['size'], 'align': t['align'], 'box': box_at(x, y),
                        **({'near': '%s:%s' % (t['near']['sid'], t['near']['atom'])} if 'xy' not in t else {})})
        return out

    def residue_boxes(self, sid, ex):
        """Boxes of the residue names a placed species shows (label_box of its residue atoms, as wide as drawn)."""
        import specutil
        mol = self.mol[sid]
        return [self.label_box(sid, m, ex, name=True) for m in ex
                if m in mol.idx and specutil.atom_kind(self.sp[sid], mol.atoms[mol.idx[m]]) == 'residue']

    def name_room(self, p):
        """Residue names of two molecules at one height keep TEXT_APART between them: the right molecule, and every
        molecule whose box starts at or right of it, move right."""
        pid = p['id']
        for _ in range(2 * len(p['species'])):
            names = []
            for key in p['species']:
                sid = '%s.%s' % (pid, key)
                if sid in self.xy:
                    names += [(sid, b) for b in self.residue_boxes(sid, self.expanded(sid, self.xy[sid]))]
            move = None
            for (sa, ba), (sb, bb) in itertools.combinations(names, 2):
                if sa == sb or ba[1] > bb[3] or bb[1] > ba[3]:
                    continue
                if ba[0] > bb[0]:
                    (sa, ba), (sb, bb) = (sb, bb), (sa, ba)
                short = TEXT_APART - (bb[0] - ba[2])
                start = self.bbox([sb])[0]
                if short > 1e-6 and self.bbox([sa])[0] < start - 1e-9:
                    move = (short, start, sa)
                    break
            if move is None:
                return
            short, start, keep = move
            for key in p['species']:
                o = '%s.%s' % (pid, key)
                if o != keep and o in self.xy and self.bbox([o])[0] >= start - 1e-9:
                    self.xy[o] = {m: (x + short, y) for m, (x, y) in self.xy[o].items()}

    def plus_pairs(self, p):
        """The species pairs of a panel's plus, each as (left, right): as written ("a + b"), or the other way round
        where the second already stands left of the first at a height the first reaches."""
        out = []
        for a, b in p.get('plus', []):
            sa, sb = '%s.%s' % (p['id'], a), '%s.%s' % (p['id'], b)
            ba, bb = self.bbox([sa]), self.bbox([sb])
            level = bb[1] <= ba[3] and bb[3] >= ba[1]
            out.append((sb, sa) if level and bb[0] + bb[2] < ba[0] + ba[2] else (sa, sb))
        return out

    def plus_room(self, p):
        """Room for each "+" of a panel (plus): the right species, and every species whose box starts at or right of
        it, move right until the gap holds the sign with SPECIES_GAP on each side.  A right species at no height the
        left one reaches (a leaving group kept above its old partner) moves alone to the left one's middle height."""
        bpt = self.style['bond_length_pt']
        need = text_width('+', self.style['font_size_pt'], bpt) + 2 * SPECIES_GAP
        for a, b in self.plus_pairs(p):
            ba, bb = self.bbox([a]), self.bbox([b])
            short = need - (bb[0] - ba[2])
            if bb[1] > ba[3] or bb[3] < ba[1]:
                dy = (ba[1] + ba[3] - bb[1] - bb[3]) / 2.0
                self.xy[b] = {m: (x + max(0.0, short), y + dy) for m, (x, y) in self.xy[b].items()}
                continue
            if short <= 1e-9:
                continue
            for key in p['species']:
                o = '%s.%s' % (p['id'], key)
                if o != a and self.bbox([o])[0] >= bb[0] - 1e-9:
                    self.xy[o] = {m: (x + short, y) for m, (x, y) in self.xy[o].items()}

    def wrap_rows(self, order, pboxes, need, title_gap):
        """Rows for panels the input did not arrange: one row when it fits ROW_WIDTH_PT, else the fewest rows of two or
        more panels each (reading order kept) whose widest row fits, balanced by width (the narrowest widest row; on a
        tie, the earlier rows longer).  When no split fits, the split with the narrowest widest row.  Widths follow
        arrange(): panel boxes, reaction arrows as long as the longest one ending in the row, an arrow coming into a
        row from the row before it at the row's left."""
        arrows = self.spec['reaction_arrows']
        n, limit = len(order), ROW_WIDTH_PT / self.style['bond_length_pt']

        def width(row, first):
            inside = set(row)
            L = max([max([need[(r['from'], r['to'])]] + [title_gap(a, b) - 2 * ARROW_GAP for a, b in zip(row, row[1:])
                                                          if {a, b} == {r['from'], r['to']} and r.get('length') is None])
                      for r in arrows if r['to'] in inside] or [ARROW_MIN])
            x = 2 * ARROW_GAP + L if not first and any(r['to'] == row[0] and r['from'] not in inside for r in arrows) else 0.0
            for prev, pid in zip(row, row[1:]):
                joined = any({r['from'], r['to']} == {prev, pid} for r in arrows)
                x += (pboxes[prev][2] - pboxes[prev][0]) + ((2 * ARROW_GAP + L) if joined else max(PANEL_GAP, title_gap(prev, pid)))
            return x + pboxes[row[-1]][2] - pboxes[row[-1]][0]
        if n < 4 or width(order, True) <= limit:
            return [list(order)]
        best = None
        for k in range(2, n // 2 + 1):
            for cuts in itertools.combinations(range(2, n - 1), k - 1):
                bounds = (0,) + cuts + (n,)
                if any(b - a < 2 for a, b in zip(bounds, bounds[1:])):
                    continue
                rows = [list(order[a:b]) for a, b in zip(bounds, bounds[1:])]
                key = (round(max(width(r, i == 0) for i, r in enumerate(rows)), 6), [-len(r) for r in rows])
                if best is None or key < best[0]:
                    best = (key, rows)
            if best[0][0] <= limit:
                break
        return best[1]

    def arrange(self):
        spec = self.spec
        bpt = self.style['bond_length_pt']
        size = self.style['font_size_pt']
        pboxes, ptexts = {}, {}
        for p in self.panels:
            self.plus_room(p)
            self.name_room(p)
            sids = ['%s.%s' % (p['id'], k) for k in p['species']]
            box = self.bbox(sids) if sids else (0.0, -0.5, 2.0, 0.5)
            ptexts[p['id']] = self.place_texts(p['id'])
            for t in ptexts[p['id']]:                 # the panel's box holds its texts too
                box = (min(box[0], t['box'][0]), min(box[1], t['box'][1]), max(box[2], t['box'][2]), max(box[3], t['box'][3]))
            pboxes[p['id']] = box
            for frame in p['boxes']:
                if frame.get('ts'):
                    bx = self.bbox(['%s.%s' % (p['id'], k) for k in frame['species']])
                    # Reserve room for brackets and the upper-right double dagger.
                    box = (min(box[0], bx[0] - .4), min(box[1], bx[1] - 1.0),
                           max(box[2], bx[2] + 1.0), max(box[3], bx[3] + .4))
            pboxes[p['id']] = box
        # reaction arrows: length per row = longest label + margins (at least ARROW_MIN)
        arrows = spec['reaction_arrows']
        need = {}
        for r in arrows:
            w = max([text_width(t, size, bpt) for t in (r.get('above'), r.get('below')) if t] or [0.0])
            need[(r['from'], r['to'])] = max(ARROW_MIN, w + 0.8) if r.get('length') is None else float(r['length'])
        # titles (centered under each panel's anchor, below) of two neighbors in a row keep TITLE_SEP between them:
        # the gap between the two panels' boxes this needs
        from specutil import plain_text
        title_pt = float(self.style.get('title_size_pt') or size)

        def title_span(pid):
            p = self.panels[self.pidx[pid]]
            if not p.get('title'):
                return None
            ab = self.bbox(['%s.%s' % (pid, p['species'][0])]) if p['species'] else pboxes[pid]
            cx, w = (ab[0] + ab[2]) / 2.0, text_width(plain_text(p['title']), title_pt, bpt)
            return cx - w / 2.0, cx + w / 2.0

        def title_gap(prev, pid):
            a, b = title_span(prev), title_span(pid)
            return 0.0 if a is None or b is None else TITLE_SEP + (a[1] - pboxes[prev][2]) + (pboxes[pid][0] - b[0])
        rows = spec['rows'] if (spec.get('raw') or {}).get('rows') else self.wrap_rows(spec['rows'][0], pboxes, need, title_gap)
        row_of = {pid: ri for ri, row in enumerate(rows) for pid in row}
        col_of = {pid: ci for row in rows for ci, pid in enumerate(row)}
        column_arrows = {(r['from'], r['to']) for r in arrows
                         if abs(row_of[r['from']] - row_of[r['to']]) == 1
                         and col_of[r['from']] == col_of[r['to']]}
        for row in rows:                               # an arrow without a given length grows to keep the titles apart
            for prev, pid in zip(row, row[1:]):
                for r in arrows:
                    if {r['from'], r['to']} == {prev, pid} and r.get('length') is None:
                        need[(r['from'], r['to'])] = max(need[(r['from'], r['to'])], title_gap(prev, pid) - 2 * ARROW_GAP)
        row_len = {}                                   # every arrow of a row gets the same length
        for (a, b), L in need.items():
            row_len[row_of[b]] = max(row_len.get(row_of[b], 0.0), L)
        offsets = {}
        y_cursor = 0.0
        row_boxes = []
        for ri, row in enumerate(rows):
            x_cursor = 0.0
            top = min(pboxes[p][1] for p in row)
            bottom = max(pboxes[p][3] for p in row)
            dy = 0.0 if ri == 0 else y_cursor - top
            for ci, pid in enumerate(row):
                box = pboxes[pid]
                incoming = [r for r in arrows if r['to'] == pid]
                left_arrow = None
                if ci == 0:
                    for r in incoming:
                        if row_of[r['from']] != ri and col_of[r['from']] != ci:
                            left_arrow = r
                if ci > 0:
                    prev = row[ci - 1]
                    joined = [r for r in arrows if {r['from'], r['to']} == {prev, pid}]
                    gap = (2 * ARROW_GAP + row_len.get(ri, ARROW_MIN)) if joined else max(PANEL_GAP, title_gap(prev, pid))
                    x_cursor = offsets[prev][0] + pboxes[prev][2] + gap
                elif left_arrow is not None:
                    x_cursor = 2 * ARROW_GAP + row_len.get(ri, ARROW_MIN)
                offsets[pid] = (x_cursor - box[0], dy)
            row_boxes.append((top + dy, bottom + dy))
            # A vertical arrow needs its full length below the title and above the next panel.
            column_lengths = [need[(a, b)] for a, b in column_arrows if min(row_of[a], row_of[b]) == ri]
            row_gap = max([ROW_GAP] + [L + 0.6 for L in column_lengths])
            y_cursor = bottom + dy + TITLE_GAP + 0.5 + row_gap
        # titles: one height per row, centered under each panel's anchor
        titles = {}
        for ri, row in enumerate(rows):
            ty = row_boxes[ri][1] + TITLE_GAP
            for pid in row:
                p = self.panels[self.pidx[pid]]
                if p['species']:
                    ab = self.bbox(['%s.%s' % (pid, p['species'][0])])
                    cx = (ab[0] + ab[2]) / 2.0
                else:
                    cx = (pboxes[pid][0] + pboxes[pid][2]) / 2.0
                titles[pid] = (cx + offsets[pid][0], ty)
        # move everything into the figure frame
        xy_out = {}
        for sid, xy in self.xy.items():
            ox, oy = offsets[self.sp[sid]['panel']]
            xy_out[sid] = {m: (x + ox, y + oy) for m, (x, y) in xy.items()}
        # reaction arrows
        rarrows = []
        for r in arrows:
            a, b = r['from'], r['to']
            (oxa, oya), (oxb, oyb) = offsets[a], offsets[b]
            ba = (pboxes[a][0] + oxa, pboxes[a][1] + oya, pboxes[a][2] + oxa, pboxes[a][3] + oya)
            bb = (pboxes[b][0] + oxb, pboxes[b][1] + oyb, pboxes[b][2] + oxb, pboxes[b][3] + oyb)
            L = row_len.get(row_of[b], ARROW_MIN)
            if (a, b) in column_arrows:
                # Column layout takes precedence over old hand-positioned endpoints. Use the common x range,
                # not one panel's anchor: the anchor can move when the reacting part changes between rows.
                x = (max(ba[0], bb[0]) + min(ba[2], bb[2])) / 2.0
                upper, lower_box = (a, bb) if row_of[a] < row_of[b] else (b, ba)
                y1, y2 = titles[upper][1] + 0.6, lower_box[1] - 0.5
                if row_of[a] > row_of[b]:
                    y1, y2 = y2, y1
                rarrows.append({'kind': r['kind'], 'x1': x, 'y1': y1, 'x2': x, 'y2': y2,
                                'above': r.get('above'), 'below': r.get('below'), 'column_panels': [a, b]})
            elif 'from_pt' in r and 'to_pt' in r:         # end points given as an atom of the figure plus an offset
                (x1, y1), (x2, y2) = [(xy_out[r[k]['sid']][r[k]['atom']][0] + r[k]['dxy'][0],
                                       xy_out[r[k]['sid']][r[k]['atom']][1] + r[k]['dxy'][1]) for k in ('from_pt', 'to_pt')]
                rarrows.append({'kind': r['kind'], 'x1': x1, 'y1': y1, 'x2': x2, 'y2': y2, 'above': r.get('above'), 'below': r.get('below')})
            elif row_of[a] == row_of[b] and col_of[b] == col_of[a] + 1:
                y = (self.anchor_midline(a) + oya + self.anchor_midline(b) + oyb) / 2.0
                x1 = ba[2] + ARROW_GAP
                x2 = bb[0] - ARROW_GAP
                rarrows.append({'kind': r['kind'], 'x1': x1, 'y1': y, 'x2': x2, 'y2': y, 'above': r.get('above'), 'below': r.get('below')})
            elif row_of[a] == row_of[b] and col_of[a] == col_of[b] + 1:
                y = (self.anchor_midline(a) + oya + self.anchor_midline(b) + oyb) / 2.0
                rarrows.append({'kind': r['kind'], 'x1': ba[0] - ARROW_GAP, 'y1': y, 'x2': bb[2] + ARROW_GAP, 'y2': y,
                                'above': r.get('above'), 'below': r.get('below')})
            else:
                y = self.anchor_midline(b) + oyb
                rarrows.append({'kind': r['kind'], 'x1': bb[0] - ARROW_GAP - L, 'y1': y, 'x2': bb[0] - ARROW_GAP, 'y2': y,
                                'above': r.get('above'), 'below': r.get('below')})
        for ra, r in zip(rarrows, arrows):                # one drawn arrow per asked arrow, in order
            ra['from'], ra['to'] = r['from'], r['to']
            if r.get('summary'):
                ra['summary'] = True
            if r.get('weight') == 'bold':
                ra['weight'] = 'bold'
            if r.get('ratio') is not None:
                ra['ratio'] = float(r['ratio'])
        # texts and boxes per panel
        out_panels = {}
        for p in self.panels:
            pid = p['id']
            ox, oy = offsets[pid]
            texts = [{'text': t['text'], 'x': t['x'] + ox, 'y': t['y'] + oy, 'size': t['size'], 'align': t['align'],
                      **({'near': t['near']} if t.get('near') else {})} for t in ptexts[pid]]
            boxes = []
            for b in p['boxes']:
                sids = ['%s.%s' % (pid, k) for k in b['species']]
                bx = self.bbox(sids)
                boxes.append({'x1': bx[0] + ox - b['pad'], 'y1': bx[1] + oy - b['pad'], 'x2': bx[2] + ox + b['pad'], 'y2': bx[3] + oy + b['pad'], 'kind': b.get('kind', 'box'),
                              **({'ts': b['ts'], 'ts_species': sids} if b.get('ts') else {})})
            out_panels[pid] = {'title': titles.get(pid), 'texts': texts, 'boxes': boxes,
                               'bbox': (pboxes[pid][0] + ox, pboxes[pid][1] + oy, pboxes[pid][2] + ox, pboxes[pid][3] + oy),
                               'offset': offsets[pid]}
        return {'xy': xy_out, 'panels': out_panels, 'reaction_arrows': rarrows, 'notes': self.notes,
                'bond_style': self.bond_styles(xy_out), 'move': self.move_hints(xy_out)}

    # ---- bond styles (wedges from the SMILES chirality, or given) and which end moves when a bond is lengthened
    def bond_styles(self, xy_out):
        out = {}
        for sid, sp in self.sp.items():
            mol = self.mol[sid]
            styles = {}
            if any(a.get('chiral') for a in mol.atoms):
                try:
                    Chem, _ = _rdkit()
                    m = _rd_mol(mol, sp['smiles_written'])
                    conf = Chem.Conformer(mol.n)
                    for i in range(mol.n):
                        x, y = xy_out[sid][mol.map[i]]
                        conf.SetAtomPosition(i, (x, -y, 0.0))
                    m.AddConformer(conf, assignId=True)
                    Chem.WedgeMolBonds(m, m.GetConformer())
                    for b in m.GetBonds():
                        d = b.GetBondDir()
                        if d in (Chem.BondDir.BEGINWEDGE, Chem.BondDir.BEGINDASH):
                            a1, a2 = mol.map[b.GetBeginAtomIdx()], mol.map[b.GetEndAtomIdx()]
                            styles[frozenset((a1, a2))] = ('wedge' if d == Chem.BondDir.BEGINWEDGE else 'hash', (a1, a2))
                    styles = iupac_stereobonds(mol, xy_out[sid], styles)
                except LayoutError:
                    self.notes.append('%s: chirality given but RDKit is missing: no wedges drawn' % sid)
            for (a, b), st in sp['bond_style'].items():
                styles[frozenset((a, b))] = (st, (a, b))
            # An unknown alkene end with two substituents needs both single bonds wavy.
            for (a, b), st in sp['bond_style'].items():
                if st != 'wavy':
                    continue
                for end in (a, b):
                    i = mol.idx[end]
                    if any(mol.order[frozenset((i, j))] == 2 for j in mol.adj[i]):
                        singles = [j for j in mol.adj[i] if mol.order[frozenset((i, j))] == 1]
                        for j in singles:
                            if j not in mol.stereogenic:
                                styles[frozenset((end, mol.map[j]))] = ('wavy', (end, mol.map[j]))
            out[sid] = styles
        return out

    def move_hints(self, xy_out):
        return {(sid, e): v[0] for sid, moves in self.moves.items() for e, v in moves.items()}


def compute(spec):
    """Layout of a loaded format-2 spec (specutil.load_spec): see Layout.arrange for the result."""
    return Layout(spec).run()
