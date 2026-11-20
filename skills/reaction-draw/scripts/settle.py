"""Places of the molecules of each panel chosen by one score, as in map label placement.

Layout.run calls settle last, before arrange.  In each panel of two or more molecules, a local search starts from
where the molecules stand and, each round, prices every move at once with numpy: a rigid shift of a molecule after
the first, the shortest shift that clears it of the others, a seat fitted to its hydrogen bonds, the seats of two
molecules whose dotted lines cross, and a terminal group holding a hydrogen bond turned over its single bond (alone or
with its partners re-seated).  The few best moves are priced with the whole score and the best is taken while it
lowers it.  Where the molecules stood before is only the start: a move is never penalized for its size.

The score: 100 per predicted crossing or contact of a dotted line (with a bond, another dotted line, another atom's
label, or a curved arrow's chord) and per curved arrow whose chord passes a third molecule; 2000 per bond the white
between two molecules falls short of SPECIES_GAP (the species_gap check's measure); 100 per bond a dotted span falls
short of the IUPAC span; 100 per bond reacting atoms stand beyond reach of each other; 10 x weight per bond
hydrogen-bonded atoms stand beyond their contact distance; and, for the moves priced, the letters' collisions
(layout_repair.panel_cost) and the texts placed after settle (_text_cost).

A panel named in CHOICE also gets its other settled layouts (_candidates), which draw_figure.retry_settle draws when
the drawing of the best one fails.
"""

import collections
import math

import numpy as np

import layout
import layout_repair as lr

ROUNDS = 8              # moves taken per panel at most
SEATS = 8               # seats per molecule tried (the best by the seat step's own ranking)
SHIFTS = (0.3, 0.7)     # rigid shifts (bonds) in 8 directions
PRICED = 5              # moves of each round priced with the whole panel score (the letters' collisions too)
CHUNK = 256             # rows scored per numpy pass
GAP = 2000              # score per bond the white between two molecules falls short of SPECIES_GAP
ARROW_BAND = 0.25       # a dotted line, or a third molecule's bond or label, this near (bonds) the chord of a curved
                        # arrow meets the arrow's bulge
REACH = 2.0             # reacting atoms farther apart than their contact distance plus this (bonds) leave their arrow
REACH_W = 100           # no clear route: score per bond beyond it
LABEL_CLEAR = 0.1       # a dotted line's drawn part closer than this to another atom's label: a contact (bonds);
                        # the label's white cuts its dots
QUICK_ROUNDS = 4            # rounds of the quick search that settles each other candidate
SEATS_TRIED = 3             # seats a hydrogen-bonded molecule starts from for other candidates, in a panel without reacting pairs
ORBIT = range(60, 360, 60)  # turns (degrees) of a reacting molecule about its partner's end tried for other candidates
CHOICE = {}                 # {panel id: rank}: which settled layout to keep, 0 the best; a panel named here also gets its
                            # other candidates listed in lay.settle_found (specutil.resolve_candidates sets it)
_SETTLED = collections.OrderedDict()
_FOUND = collections.OrderedDict()     # settle's distinct layouts per panel input, for the ranks retry_settle asks     # the result per panel input: retries lay out the same panels again


# ---------------------------------------------------------------- geometry in arrays

def _seg_seg(a, b, c, d):
    """Distance between segments a-b and c-d (arrays [..., 2] that broadcast); 0 when they cross."""
    def cross(u, v):
        return u[..., 0] * v[..., 1] - u[..., 1] * v[..., 0]
    def pt_seg(p, s, e):
        se = e - s
        n = np.maximum((se ** 2).sum(-1), 1e-18)
        t = np.clip(((p - s) * se).sum(-1) / n, 0.0, 1.0)
        q = s + t[..., None] * se
        return np.hypot(p[..., 0] - q[..., 0], p[..., 1] - q[..., 1])
    d1, d2 = cross(b - a, c - a), cross(b - a, d - a)
    d3, d4 = cross(d - c, a - c), cross(d - c, b - c)
    hit = (d1 * d2 < 0) & (d3 * d4 < 0)
    near = np.minimum(np.minimum(pt_seg(a, c, d), pt_seg(b, c, d)), np.minimum(pt_seg(c, a, b), pt_seg(d, a, b)))
    return np.where(hit, 0.0, near)


def _seg_box(a, b, box):
    """Distance between segment a-b and box (x1, y1, x2, y2) (arrays that broadcast); 0 when they meet."""
    x1, y1, x2, y2 = box[..., 0], box[..., 1], box[..., 2], box[..., 3]
    inside = ((x1 <= a[..., 0]) & (a[..., 0] <= x2) & (y1 <= a[..., 1]) & (a[..., 1] <= y2)) | \
             ((x1 <= b[..., 0]) & (b[..., 0] <= x2) & (y1 <= b[..., 1]) & (b[..., 1] <= y2))
    corners = [np.stack(c, -1) for c in ((x1, y1), (x2, y1), (x2, y2), (x1, y2))]
    d = np.minimum.reduce([_seg_seg(a, b, corners[k], corners[(k + 1) % 4]) for k in range(4)])
    return np.where(inside, 0.0, d)


def _exit(p, q, box):
    """How far along p -> q (a fraction) the segment leaves box, which holds p (0 when it does not)."""
    x1, y1, x2, y2 = box[..., 0], box[..., 1], box[..., 2], box[..., 3]
    inside = (x1 <= p[..., 0]) & (p[..., 0] <= x2) & (y1 <= p[..., 1]) & (p[..., 1] <= y2)
    t = np.ones(p.shape[:-1])
    for k, lo, hi in ((0, x1, x2), (1, y1, y2)):
        d = q[..., k] - p[..., k]
        with np.errstate(divide='ignore', invalid='ignore'):
            t = np.where(d > 1e-12, np.minimum(t, (hi - p[..., k]) / d), t)
            t = np.where(d < -1e-12, np.minimum(t, (lo - p[..., k]) / d), t)
    return np.where(inside, np.maximum(0.0, t), 0.0)


# ---------------------------------------------------------------- one panel

class Panel(object):
    """The species of one panel, their candidate places as arrays, and the fixed index lists the score reads."""

    def __init__(self, lay, pid, sids, links, reach=()):
        self.lay, self.pid, self.sids, self.links, self.reach = lay, pid, sids, links, reach
        self.margin = lay.style['label_margin_pt'] / lay.style['bond_length_pt']
        self.height = layout.CAP * lay.style['font_size_pt'] / lay.style['bond_length_pt']
        self.info = {}
        for sid in sids:
            mol = lay.mol[sid]
            maps = [mol.map[i] for i in range(mol.n)]
            labeled = [i for i in range(mol.n) if mol.labeled(i)]
            bonds = np.array([sorted(e) for e in mol.order], dtype=int).reshape(-1, 2)
            self.info[sid] = {'maps': maps, 'pos': {m: k for k, m in enumerate(maps)}, 'labeled': labeled,
                              'plain': [i for i in range(mol.n) if not mol.labeled(i)], 'bonds': bonds}
        self.hb = [(a['sid'], a['atom'], b['sid'], b['atom']) for a, b in lay.panels[lay.pidx[pid]]['hbonds']]
        self.hb_pairs = {frozenset(((sa, ma), (sb, mb))) for sa, ma, sb, mb in self.hb if sa != sb}
        # curved arrows as the chords between their ends: a dotted line across one is a crossing
        self.chords = []
        for ar in lay.panels[lay.pidx[pid]]['arrows']:
            ends = []
            for e in (ar['from'], ar['to']):
                ends.append([(r['sid'], m) for r in lr._ends(e) for m in (r['atom'], r.get('atom2')) if m is not None])
            self.chords.append(tuple(ends))
        self._item_cache = {}

    def geometry(self, sid, places):
        """(atoms (C, n, 2), label boxes (C, l, 4)) of species sid at each of places."""
        inf = self.info[sid]
        P = np.array([[xy[m] for m in inf['maps']] for xy in places], dtype=float)
        B = np.array([[self.lay.label_box(sid, inf['maps'][i], xy) for i in inf['labeled']] for xy in places],
                     dtype=float).reshape(len(places), len(inf['labeled']), 4)
        return P, B

    def score(self, present, geo, rows):
        """The pair part of the score for each row of rows (R, len(present)): row r puts species present[k] at its
        candidate rows[r, k] (geo[sid] = (atoms, boxes) over its candidates)."""
        out = np.zeros(len(rows))
        for start in range(0, len(rows), CHUNK):
            out[start:start + CHUNK] = self._score(present, geo, rows[start:start + CHUNK])
        return out

    def _score(self, present, geo, rows):
        R = len(rows)
        off, aoff, refs, lab_ref, lab_atom, bonds, sp_atom = {}, {}, {}, [], [], [], []
        pts, boxes = [], []
        n_at = n_lab = 0
        for k, sid in enumerate(present):
            inf = self.info[sid]
            P, B = geo[sid]
            pts.append(P[rows[:, k]])
            boxes.append(B[rows[:, k]])
            off[sid], aoff[sid] = n_at, n_lab
            for j, m in enumerate(inf['maps']):
                refs[(sid, m)] = n_at + j
            sp_atom += [k] * len(inf['maps'])
            for j, i in enumerate(inf['labeled']):
                lab_ref.append((sid, inf['maps'][i]))
                lab_atom.append(n_at + i)
            bonds.append(inf['bonds'] + n_at)
            n_at += len(inf['maps'])
            n_lab += len(inf['labeled'])
        pts = np.concatenate(pts, axis=1)
        boxes = np.concatenate(boxes, axis=1) if n_lab else np.zeros((R, 0, 4))
        bonds = np.concatenate(bonds) if bonds else np.zeros((0, 2), dtype=int)
        lab_atom = np.array(lab_atom, dtype=int)
        box_of = {a: j for j, a in enumerate(lab_atom.tolist())}
        lines = [(refs[(sa, ma)], refs[(sb, mb)], sa != sb) for sa, ma, sb, mb in self.hb
                 if (sa, ma) in refs and (sb, mb) in refs]
        total = np.zeros(R)
        short_sum = np.zeros(R)
        sp_atom = np.array(sp_atom, dtype=int)
        for ends_from, ends_to in self.chords:
            if not all(r in refs for r in ends_from + ends_to):
                continue
            own = [present.index(sid) for sid in {r[0] for r in ends_from + ends_to}]
            a = pts[:, [refs[r] for r in ends_from]].mean(axis=1)[:, None]
            b = pts[:, [refs[r] for r in ends_to]].mean(axis=1)[:, None]
            if len(bonds):
                e = np.nonzero(~np.isin(sp_atom[bonds[:, 0]], own))[0]
                if len(e):
                    total += (_seg_seg(a, b, pts[:, bonds[e, 0]], pts[:, bonds[e, 1]]) < ARROW_BAND).sum(axis=1)
            if n_lab:
                j = np.nonzero(~np.isin(sp_atom[lab_atom], own))[0]
                if len(j):
                    total += (_seg_box(a, b, boxes[:, j]) < ARROW_BAND).sum(axis=1)
        if lines:
            la = np.array([l[0] for l in lines]); lb = np.array([l[1] for l in lines])
            p, q = pts[:, la], pts[:, lb]                                  # (R, H, 2)
            # crossings with bonds that do not share an end
            if len(bonds):
                keep = ~((bonds[None, :, 0] == la[:, None]) | (bonds[None, :, 0] == lb[:, None]) |
                         (bonds[None, :, 1] == la[:, None]) | (bonds[None, :, 1] == lb[:, None]))
                h, e = np.nonzero(keep)
                d = _seg_seg(p[:, h], q[:, h], pts[:, bonds[e, 0]], pts[:, bonds[e, 1]])
                total += (d < lr.HB_CROSS).sum(axis=1)
            # crossings with other dotted lines that do not share an end
            h1, h2 = np.triu_indices(len(lines), 1)
            if len(h1):
                ok = ~((la[h1] == la[h2]) | (la[h1] == lb[h2]) | (lb[h1] == la[h2]) | (lb[h1] == lb[h2]))
                h1, h2 = h1[ok], h2[ok]
                if len(h1):
                    d = _seg_seg(p[:, h1], q[:, h1], p[:, h2], q[:, h2])
                    total += (d < lr.HB_CROSS).sum(axis=1)
            # crossings with the chords of curved arrows whose ends are not the line's ends
            for ends_from, ends_to in self.chords:
                if not all(r in refs for r in ends_from + ends_to):
                    continue
                mine = np.array([refs[r] for r in ends_from + ends_to])
                h = np.nonzero(~(np.isin(la, mine) | np.isin(lb, mine)))[0]
                if len(h):
                    a = pts[:, [refs[r] for r in ends_from]].mean(axis=1)[:, None]
                    b = pts[:, [refs[r] for r in ends_to]].mean(axis=1)[:, None]
                    total += (_seg_seg(a, b, p[:, h], q[:, h]) < ARROW_BAND).sum(axis=1)
            # the drawn part of each line against other atoms' labels
            if n_lab:
                length = np.hypot(q[..., 0] - p[..., 0], q[..., 1] - p[..., 1])
                ba = np.array([box_of.get(a, -1) for a in la]); bb = np.array([box_of.get(b, -1) for b in lb])
                ta = np.where(ba >= 0, _exit(p, q, boxes[:, np.maximum(ba, 0)]), 0.0)
                tb = np.where(bb >= 0, _exit(q, p, boxes[:, np.maximum(bb, 0)]), 0.0)
                with np.errstate(divide='ignore', invalid='ignore'):
                    ta = ta + self.margin / np.maximum(length, 1e-9)
                    tb = tb + self.margin / np.maximum(length, 1e-9)
                drawn = (ta + tb < 1.0) & (length > 1e-9)
                u = p + ta[..., None] * (q - p)
                v = q + tb[..., None] * (p - q)
                h, j = np.nonzero(~((lab_atom[None, :] == la[:, None]) | (lab_atom[None, :] == lb[:, None])))
                d = _seg_box(u[:, h], v[:, h], boxes[:, j])
                total += ((d < LABEL_CLEAR) & drawn[:, h]).sum(axis=1)
                # dotted spans between two molecules short of the IUPAC span
                cross = np.array([l[2] for l in lines])
                if cross.any():
                    ux = np.abs((q[..., 0] - p[..., 0]) / np.maximum(length, 1e-9))
                    uy = np.abs((q[..., 1] - p[..., 1]) / np.maximum(length, 1e-9))
                    want = np.full(length.shape, 2 * self.height)
                    for bx in (ba, bb):
                        has = bx >= 0
                        bxx = boxes[:, np.maximum(bx, 0)]
                        add = (bxx[..., 2] - bxx[..., 0]) / 2 * ux + (bxx[..., 3] - bxx[..., 1]) / 2 * uy + self.margin
                        want = want + np.where(has, add, 0.0)
                    short = np.where(length < .01, 1.0, np.maximum(0.0, want - length))
                    short_sum = (short * cross).sum(axis=1)
            total = 100.0 * total + 100.0 * short_sum
        else:
            total = 100.0 * total
        # white short of half a bond between two molecules
        total += GAP * self._gap(present, pts, boxes)
        # reacting atoms out of reach of each other
        for sa, ea, sb, eb, lim in self.reach:
            if sa in present and sb in present:
                pa = pts[:, refs[(sa, ea[0])]] if ea[1] is None else (pts[:, refs[(sa, ea[0])]] + pts[:, refs[(sa, ea[1])]]) / 2
                pb = pts[:, refs[(sb, eb[0])]] if eb[1] is None else (pts[:, refs[(sb, eb[0])]] + pts[:, refs[(sb, eb[1])]]) / 2
                total += REACH_W * np.maximum(0.0, np.hypot(pa[:, 0] - pb[:, 0], pa[:, 1] - pb[:, 1]) - lim)
        # joined atoms beyond their contact distance
        for sa, ea, sb, eb, w, want in self.links:
            if sa in present and sb in present:
                pa = pts[:, refs[(sa, ea[0])]] if ea[1] is None else (pts[:, refs[(sa, ea[0])]] + pts[:, refs[(sa, ea[1])]]) / 2
                pb = pts[:, refs[(sb, eb[0])]] if eb[1] is None else (pts[:, refs[(sb, eb[0])]] + pts[:, refs[(sb, eb[1])]]) / 2
                got = np.hypot(pa[:, 0] - pb[:, 0], pa[:, 1] - pb[:, 1])
                total += 10.0 * w * np.maximum(0.0, got - want)
        return total

    def _items(self, present):
        """Per molecule of present, the ink the gap reads: (kind, a, b, t, atom) rows, kind 0 a bare atom (point a),
        1 a point at t along bond a-b, 2 the label box a; atom names the (sid, map) for the exempt hydrogen-bond pairs."""
        key = tuple(present)
        if key not in self._item_cache:
            refs, n_at, lab = {}, 0, 0
            for sid in present:
                for j, m in enumerate(self.info[sid]['maps']):
                    refs[(sid, m)] = n_at + j
                n_at += len(self.info[sid]['maps'])
            out = []
            for sid in present:
                inf, rows = self.info[sid], []
                for i in inf['plain']:
                    rows.append((0, refs[(sid, inf['maps'][i])], 0, 0.0, (sid, inf['maps'][i])))
                for a, b in inf['bonds']:
                    for t in (0.25, 0.5, 0.75):
                        rows.append((1, refs[(sid, inf['maps'][a])], refs[(sid, inf['maps'][b])], t, None))
                for i in inf['labeled']:
                    rows.append((2, lab, 0, 0.0, (sid, inf['maps'][i])))
                    lab += 1
                out.append(rows)
            self._item_cache[key] = out
        return self._item_cache[key]

    def _gap_arrays(self, present):
        """Per molecule of present, (kind, a, b, t) arrays of _items; per pair of molecules, the mask of their
        hydrogen-bonded atom pairs (None when they have none)."""
        key = ('arrays',) + tuple(present)
        if key not in self._item_cache:
            items = self._items(present)
            arrays = [tuple(np.array([r[k] for r in rows]) for k in range(4)) for rows in items]
            exempts = {}
            for x in range(len(items)):
                for y in range(x + 1, len(items)):
                    m = np.array([[u is not None and v is not None and frozenset((u, v)) in self.hb_pairs
                                   for _, _, _, _, v in items[y]] for _, _, _, _, u in items[x]], dtype=bool)
                    exempts[(x, y)] = m if m.any() else None
            self._item_cache[key] = (arrays, exempts)
        return self._item_cache[key]

    def _gap(self, present, pts, boxes):
        """Sum over pairs of molecules of how far the white between their closest ink falls short of SPECIES_GAP
        (bonds): what check_rules.det_species_gap measures, the two ends of a hydrogen bond between them exempt."""
        R, g = len(pts), layout.SPECIES_GAP
        shapes = []
        arrays, exempts = self._gap_arrays(present)
        for rows, (kind, a, b, t) in zip(self._items(present), arrays):
            bx = np.zeros((R, len(rows), 4))
            k = kind == 0
            bx[:, k, 0:2] = bx[:, k, 2:4] = pts[:, a[k]]
            k = kind == 1
            mid = pts[:, a[k]] + t[k][None, :, None] * (pts[:, b[k]] - pts[:, a[k]])
            bx[:, k, 0:2] = bx[:, k, 2:4] = mid
            k = kind == 2
            if k.any():
                bx[:, k] = boxes[:, a[k]]
            shapes.append(bx)
        out = np.zeros(R)
        for x in range(len(shapes)):
            for y in range(x + 1, len(shapes)):
                A, B = shapes[x], shapes[y]
                if not A.shape[1] or not B.shape[1]:
                    continue
                lo, hi = A[..., :2].min(1), A[..., 2:].max(1)
                lo2, hi2 = B[..., :2].min(1), B[..., 2:].max(1)
                near = np.nonzero(((lo - g < hi2) & (lo2 - g < hi)).all(axis=1))[0]
                if not len(near):
                    continue
                d = lr._box_gaps(A[near][:, :, None], B[near][:, None])
                exempt = exempts[(x, y)]
                if exempt is not None:
                    d[:, exempt] = np.inf
                out[near] += np.maximum(0.0, g - d.reshape(len(near), -1).min(axis=1))
        return out


def _hb_links(lay, pid, sid):
    """[(my map, other sid, other map)] of sid's hydrogen bonds to other molecules of the panel."""
    return [(b['atom'], a['sid'], a['atom']) if b['sid'] == sid else (a['atom'], b['sid'], b['atom'])
            for a, b in lay.panels[lay.pidx[pid]]['hbonds'] if sid in (a['sid'], b['sid']) and a['sid'] != b['sid']]


def _flip_groups(lay, sid):
    """Terminal groups of molecule sid that hold a hydrogen bond to another molecule and may turn over their single
    bond (a carboxylate's two O atoms change places): [(root map, parent map, maps of the group)].  Rings,
    stereocenters, hand coordinates and molecules the input turns or mirrors stay."""
    sp, mol = lay.sp[sid], lay.mol[sid]
    if sp['rotate'] or sp['flip']:
        return []
    own, _ = lay.hbond_ends(sid)
    out, seen = [], set()
    for m in sorted(own):
        i = mol.idx[m]
        for x in [i] + list(mol.adj[i]):
            for y in mol.adj[x]:
                if (x, y) in seen or frozenset((x, y)) in mol.ring_bonds or mol.order.get(frozenset((x, y))) != 1:
                    continue
                side = set(mol.subtree(x, y))
                if y in side or i not in side or len([k for k in side if not mol.is_h[k]]) > 6:
                    continue
                if side & mol.stereogenic or any(mol.map[k] in sp['xy'] for k in side):
                    continue
                seen.add((x, y))
                out.append((mol.map[x], mol.map[y], [mol.map[k] for k in side]))
    return out


def _group_turned(xy, group):
    """xy with the group (root, parent, maps) reflected over the parent-root bond axis."""
    root, parent, maps = group
    p, q = xy[parent], xy[root]
    length = layout.dist(p, q)
    if length < 1e-6:
        return xy
    u = ((q[0] - p[0]) / length, (q[1] - p[1]) / length)
    new = dict(xy)
    for m in maps:
        d = (xy[m][0] - p[0], xy[m][1] - p[1])
        t = d[0] * u[0] + d[1] * u[1]
        new[m] = (p[0] + 2 * t * u[0] - d[0], p[1] + 2 * t * u[1] - d[1])
    return new


# ---------------------------------------------------------------- moves and the search

def _shift(xy, dg, d):
    u = layout.vec(dg)
    return {m: (x + u[0] * d, y + u[1] * d) for m, (x, y) in xy.items()}


def _moves(lay, pid, sids, seats_now):
    """[({species: places}, kind)] tried in one round."""
    moves = []
    for sid in sids[1:]:
        if lay.sp[sid]['xy']:
            continue
        xy = lay.xy[sid]
        moves.extend(({sid: _shift(xy, dg, d)}, 'shift') for dg in range(0, 360, 45) for d in SHIFTS)
        # the shortest shift that leaves half a bond of white to the rest of the panel, for a molecule deep in another
        clear = lay.clear_of_panel(sid, xy, hbond_ends=True)
        if clear is not xy:
            moves.append(({sid: clear}, 'clear'))
        moves.extend(({sid: place}, 'seat') for place in seats_now(sid))
    pairs = []
    for kind, one, two in lr.hbond_trouble(lay, pid):
        if kind == 'hbond':
            s1, s2 = (next((r[0] for r in line if r[0] != sids[0]), None) for line in (one, two))
            if s1 and s2 and s1 != s2 and {s1, s2} not in pairs:
                pairs.append({s1, s2})
    for pair in pairs:
        s1, s2 = sorted(pair)
        a, b = seats_now(s1), seats_now(s2)
        if a and b:
            moves.extend(({s1: x, s2: y}, 'pair') for x in [lay.xy[s1]] + a for y in [lay.xy[s2]] + b)
    for sid in sids:
        if lay.sp[sid]['xy'] and sid != sids[0]:
            continue
        for group in _flip_groups(lay, sid):
            place = _group_turned(lay.xy[sid], group)
            moves.append(({sid: place}, 'flip'))
            partners = sorted({o for _, o, _ in _hb_links(lay, pid, sid) if o != sids[0] and not lay.sp[o]['xy']})
            if partners:
                was = lay.xy[sid]
                lay.xy[sid] = place
                change = {sid: place}
                for o in partners:
                    best = seats_now(o)
                    if best:
                        change[o] = best[0]
                lay.xy[sid] = was
                if len(change) > 1:
                    moves.append((change, 'flip+seats'))
    return moves


def _key(lay, pid):
    panel = lay.panels[lay.pidx[pid]]
    sids = ['%s.%s' % (pid, k) for k in panel['species'] if '%s.%s' % (pid, k) in lay.xy]
    return (lr._seat_key(lay, pid, {}),
            tuple((s, tuple(sorted((m, round(x, 6), round(y, 6)) for m, (x, y) in lay.xy[s].items()))) for s in sids))


def _sig(xy):
    return tuple(sorted((m, round(x, 4), round(y, 4)) for m, (x, y) in xy.items()))


def settle(lay):
    keep = {k: lay.__dict__[k] for k in ('arrow_chords', 'hbond_panels', 'number_sizes') if k in lay.__dict__}
    try:
        for p in lay.panels:
            _settle_panel(lay, p, keep)
    finally:
        for k in ('arrow_chords', 'hbond_panels', 'number_sizes'):
            lay.__dict__.pop(k, None)
        lay.__dict__.update(keep)


def _text_cost(lay, pid):
    """GAP per bond a text of the panel, where place_texts puts it after settle, stands short of TEXT_APART from
    another molecule's residue name or another text, or short of 0.05 from an atom label."""
    p = lay.panels[lay.pidx[pid]]
    if not p['texts']:
        return 0.0
    notes = len(lay.notes)
    texts = [t for t in lay.place_texts(pid) if t['text'] != '+']
    del lay.notes[notes:]
    names, labels = [], []
    for key in p['species']:
        sid = '%s.%s' % (pid, key)
        if sid in lay.xy:
            ex = lay.expanded(sid, lay.xy[sid])
            names += [(sid, b) for b in lay.residue_boxes(sid, ex)]
            labels += [b for b in (lay.label_box(sid, m, ex) for m in ex) if b[2] - b[0] > 0.3]
    cost = 0.0
    for i, t in enumerate(texts):
        own = t.get('near', '').rsplit(':', 1)[0]
        for b in [b for sid, b in names if sid != own] + [o['box'] for j, o in enumerate(texts) if j != i]:
            cost += max(0.0, layout.TEXT_APART - layout._box_gap(t['box'], b))
        cost += sum(max(0.0, 0.05 - layout._box_gap(t['box'], b)) for b in labels)
    return GAP * cost


def _whole(lay, panel):
    """The whole panel score where the molecules stand: the pair part, the letters' collisions (panel_cost) and the
    texts placed after settle (_text_cost)."""
    geo = {s: panel.geometry(s, [lay.xy[s]]) for s in panel.sids}
    return (float(panel.score(panel.sids, geo, np.zeros((1, len(panel.sids)), dtype=int))[0]) + lr.panel_cost(lay, panel.pid)
            + _text_cost(lay, panel.pid))


def _pairs(lay, pid, sids):
    """[(fixed sid, fixed end, moving sid, moving end)] of molecules joined by a curved arrow or a forming bond: of the
    two, the later one in the panel moves, unless it is the first, placed by hand, or turned or mirrored by the input."""
    hb = {(a['sid'], a['atom'], b['sid'], b['atom']) for a, b in lay.panels[lay.pidx[pid]]['hbonds']}
    free = lambda sid: sid != sids[0] and not lay.sp[sid]['xy'] and not lay.sp[sid]['rotate'] and not lay.sp[sid]['flip']
    out = {}
    for sa, ea, sb, eb, w in lay.contacts(pid):
        if (sa, ea[0], sb, eb[0]) in hb or (sb, eb[0], sa, ea[0]) in hb or sa not in sids or sb not in sids:
            continue
        if sids.index(sa) > sids.index(sb):
            sa, ea, sb, eb = sb, eb, sa, ea
        if not free(sb):
            if not free(sa):
                continue
            sa, ea, sb, eb = sb, eb, sa, ea
        out.setdefault(sb, (sa, ea, sb, eb))
    return list(out.values())


def _turn(xy, c, deg, mirror_through=None):
    """xy turned by deg about c; with mirror_through, then mirrored over the line from c through that point."""
    a = math.radians(deg)
    ca, sa = math.cos(a), math.sin(a)
    out = {m: (c[0] + (x - c[0]) * ca - (y - c[1]) * sa, c[1] + (x - c[0]) * sa + (y - c[1]) * ca) for m, (x, y) in xy.items()}
    if mirror_through is not None:
        q = mirror_through(out)
        n = math.hypot(q[0] - c[0], q[1] - c[1]) or 1.0
        u = ((q[0] - c[0]) / n, (q[1] - c[1]) / n)
        out = {m: (c[0] + 2 * ((x - c[0]) * u[0] + (y - c[1]) * u[1]) * u[0] - (x - c[0]),
                   c[1] + 2 * ((x - c[0]) * u[0] + (y - c[1]) * u[1]) * u[1] - (y - c[1])) for m, (x, y) in out.items()}
    return out


def _handed(xy):
    """+1 or -1: the turning sense of the molecule's first three atoms that are not on one line (its mirror image flips it)."""
    pts = list(xy.values())
    for i in range(2, len(pts)):
        a, b, c = pts[0], pts[1], pts[i]
        cr = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        if abs(cr) > 1e-6:
            return 1 if cr > 0 else -1
    return 1


def _signature(lay, pairs, start):
    """Which candidate this is: for each reacting pair, the direction (60 degree sector) from the fixed end to the
    moving end and whether the moving molecule is mirrored against where it started."""
    out = []
    for fs, fe, ms, me in pairs:
        p, q = lay.end_point(fs, fe, lay.xy[fs]), lay.end_point(ms, me, lay.xy[ms])
        sector = int(round(math.degrees(math.atan2(q[1] - p[1], q[0] - p[0])) / 60.0)) % 6
        out.append((ms, sector, _handed(lay.xy[ms]) * _handed(start[ms])))
    return tuple(out)


def _descend(lay, pid, sids, panel, seats_now, quick=False):
    """Settle's local search from where the molecules stand: (score, the moves taken).  quick (other candidates, which
    the drawing judges): each round takes the move with the lowest pair part, without pricing the letters, and the whole
    score is taken once at the end."""
    if quick:
        part = lambda: float(panel.score(sids, {s: panel.geometry(s, [lay.xy[s]]) for s in sids},
                                         np.zeros((1, len(sids)), dtype=int))[0])
        score, moved = part(), []
        for _ in range(QUICK_ROUNDS):
            moves = _moves(lay, pid, sids, seats_now)
            if not moves:
                break
            places = {s: [lay.xy[s]] for s in sids}
            rows = np.zeros((len(moves), len(sids)), dtype=int)
            for r, (change, _) in enumerate(moves):
                for s, xy in change.items():
                    places[s].append(xy)
                    rows[r, sids.index(s)] = len(places[s]) - 1
            got = panel.score(sids, {s: panel.geometry(s, places[s]) for s in sids}, rows)
            r = int(np.argmin(got))
            if got[r] >= score - 1e-6:
                break
            score = float(got[r])
            lay.xy.update(moves[r][0])
            moved.append(moves[r][1])
        return _whole(lay, panel), moved
    score = _whole(lay, panel)
    moved = []
    for _ in range(ROUNDS):
        moves = _moves(lay, pid, sids, seats_now)
        if not moves:
            break
        # every species' candidate places: where it stands (0) and the places the moves give it
        places = {s: [lay.xy[s]] for s in sids}
        rows = np.zeros((len(moves), len(sids)), dtype=int)
        for r, (change, _) in enumerate(moves):
            for s, xy in change.items():
                k = sids.index(s)
                places[s].append(xy)
                rows[r, k] = len(places[s]) - 1
        geo = {s: panel.geometry(s, places[s]) for s in sids}
        part = panel.score(sids, geo, rows)
        best = None
        for r in np.argsort(part, kind='stable')[:PRICED]:
            change, kind = moves[int(r)]
            was = {s: lay.xy[s] for s in change}
            lay.xy.update(change)
            sc = _whole(lay, panel)
            lay.xy.update(was)
            if sc < score - 1e-6 and (best is None or sc < best[0]):
                best = (sc, change, kind)
        if best is None:
            break
        score, change, kind = best
        lay.xy.update(change)
        moved.append('%s (%s)' % ('+'.join(sorted(change)), kind))
    return score, moved


def _candidates(lay, pid, sids, panel, seats_now, start, first):
    """The settled layouts of the panel, distinct by _signature, best first after the one settle keeps (first): each
    reacting molecule turned about its partner's end by ORBIT, and mirrored when it has no stereocenter, then settled."""
    pairs = _pairs(lay, pid, sids)
    keep = {s: dict(lay.xy[s]) for s in sids}
    found = {_signature(lay, pairs, start): (first[0], keep)}
    for fs, fe, ms, me in pairs:
        c = lay.end_point(fs, fe, start[fs])
        mirrors = (False, True) if not lay.mol[ms].stereogenic else (False,)
        for deg in [0] + list(ORBIT):
            for mirror in mirrors:
                if not deg and not mirror:
                    continue
                lay.xy.update({s: dict(xy) for s, xy in start.items()})
                lay.xy[ms] = _turn(start[ms], c, deg, (lambda xy: lay.end_point(ms, me, xy)) if mirror else None)
                sc, _ = _descend(lay, pid, sids, panel, seats_now, quick=True)
                sig = _signature(lay, pairs, start)
                if sig not in found or sc < found[sig][0] - 1e-6:
                    found[sig] = (sc, {s: dict(lay.xy[s]) for s in sids})
    if not pairs:
        # no reacting pair to turn: the hydrogen-bonded molecules started from their other seats instead, told apart by
        # where each ends (half-bond cells of its middle)
        def cells():
            return tuple((s, round(sum(x for x, _ in lay.xy[s].values()) / len(lay.xy[s]) * 2),
                          round(sum(y for _, y in lay.xy[s].values()) / len(lay.xy[s]) * 2)) for s in sids[1:])
        lay.xy.update(keep)
        found = {cells(): (first[0], keep)}
        for ms in sids[1:]:
            lay.xy.update({s: dict(xy) for s, xy in start.items()})
            for place in seats_now(ms)[1:SEATS_TRIED]:
                lay.xy.update({s: dict(xy) for s, xy in start.items()})
                lay.xy[ms] = place
                sc, _ = _descend(lay, pid, sids, panel, seats_now, quick=True)
                sig = cells()
                if sig not in found or sc < found[sig][0] - 1e-6:
                    found[sig] = (sc, {s: dict(lay.xy[s]) for s in sids})
    lay.xy.update(keep)
    first_sig = next(iter(found))
    rest = sorted((v for k, v in found.items() if k != first_sig), key=lambda v: v[0])
    return [found[first_sig]] + rest


def _settle_panel(lay, p, ctx):
    pid = p['id']
    sids = [s for s in ('%s.%s' % (pid, k) for k in p['species']) if s in lay.xy]
    if len(sids) < 2:
        return
    base = _key(lay, pid)
    key = (base, CHOICE.get(pid))
    if key in _SETTLED and (pid not in CHOICE or base in _FOUND):
        if pid in CHOICE:
            lay.__dict__.setdefault('settle_found', {})[pid] = _FOUND[base]
        places, note = _SETTLED[key]
        lay.xy.update({s: dict(xy) for s, xy in places.items()})
        if note:
            lay.notes.append(note)
        return
    lay.arrow_chords, lay.hbond_panels, lay.number_sizes = {pid: lr._chords(lay, pid)}, {pid}, ctx.get('number_sizes', {})
    # how far hydrogen-bonded atoms stand beyond their contact distance; reacting pairs stand where place() put them
    # for their arrows, which this score does not see
    links = [(a['sid'], (a['atom'], None), b['sid'], (b['atom'], None), layout.HBOND_WEIGHT,
              lay.contact_dist(a['sid'], (a['atom'], None), b['sid'], (b['atom'], None)))
             for a, b in p['hbonds'] if a['sid'] != b['sid']]
    # reacting pairs (arrows and forming bonds between two molecules) within reach of each other
    hb = {(a['sid'], a['atom'], b['sid'], b['atom']) for a, b in p['hbonds']}
    reach = list({(sa, ea, sb, eb): (sa, ea, sb, eb, lay.contact_dist(sa, ea, sb, eb) + REACH)
                  for sa, ea, sb, eb, w in lay.contacts(pid)
                  if (sa, ea[0], sb, eb[0]) not in hb and (sb, eb[0], sa, ea[0]) not in hb}.values())
    panel = Panel(lay, pid, sids, links, reach)
    seat_cache = {}
    def seats_now(sid):
        sp, hb = lay.sp[sid], _hb_links(lay, pid, sid)
        if not hb or sp['xy'] or sp['rotate'] or sp['flip']:
            return []
        # seats are fitted to where the partners stand: a shift of the molecule itself does not change them
        xy = lay.xy[sid]
        x0, y0 = next(iter(xy.values()))
        shape = _sig({m: (x - x0, y - y0) for m, (x, y) in xy.items()})
        k = (sid, shape, tuple(_sig(lay.xy[o]) for o in sorted({o for _, o, _ in hb})))
        if k not in seat_cache:
            seat_cache[k] = [place for _, place, i in lr._seat_options(lay, pid, sid, hb)[:SEATS] if i]
        return seat_cache[k]
    score0 = _whole(lay, panel)
    start = {s: dict(lay.xy[s]) for s in sids}
    score, moved = _descend(lay, pid, sids, panel, seats_now)
    if pid in CHOICE:
        if base not in _FOUND:
            _FOUND[base] = _candidates(lay, pid, sids, panel, seats_now, start, (score, None))
            while len(_FOUND) > 50:
                _FOUND.popitem(last=False)
        found = _FOUND[base]
        lay.__dict__.setdefault('settle_found', {})[pid] = found
        rank = min(int(CHOICE[pid]), len(found) - 1)
        if rank:
            score, places = found[rank]
            lay.xy.update({s: dict(xy) for s, xy in places.items()})
            moved.append('candidate %d of %d' % (rank, len(found)))
    note = ('panel %s: settled %s (score %.0f -> %.0f)' % (pid, ', '.join(dict.fromkeys(moved)), score0, score)
            if moved else None)
    if note:
        lay.notes.append(note)
    _SETTLED[key] = ({s: dict(lay.xy[s]) for s in sids}, note)
    while len(_SETTLED) > 200:
        _SETTLED.popitem(last=False)
