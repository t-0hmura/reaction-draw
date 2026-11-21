# SPDX-License-Identifier: Apache-2.0
"""Bounded, chemistry-preserving alternatives, only for a drawing that failed its checks.

The cheap search uses the layout's label boxes; acceptance uses every rule on the real
saved/reloaded drawing. Rings stay rigid, bridges move as whole branches, and wedges are
recomputed from the original SMILES. Explicit coordinates may move at most 0.3 bonds.
"""
import collections
import copy
import hashlib
import itertools
import math
import re

import numpy as np

import check_rules as rules
import svg_geometry as co
import layout
import specutil

KINDS = {'chain_angles', 'four_bond', 'number_place', 'number_clear', 'label_bonds',
         'one_label_bonds', 'bond_lengths', 'charge_own_atom', 'arrows_ends', 'ring_substituents', 'hbond_dots', 'lone_pair_geometry', 'bond_crossing'}
MAX_DRAWS = 3
MAX_MOVES = 12
HAND_LIMIT = 0.3


def relevant(result):
    return any(n in KINDS or n.startswith('overlap_') for n in result['failed'])


def _refs(result, atoms):
    text = ' '.join(s for n in result['failed'] for s in result['checks'][n]['findings'])
    return {r for r in atoms if re.search(r'(?<![\w.])' + re.escape(r) + r'(?!\d)', text)}


def _geometry(lay, sid, xy):
    mol = lay.mol[sid]
    penalty = 0.0
    for e in mol.order:
        i, j = tuple(e)
        length = layout.dist(xy[mol.map[i]], xy[mol.map[j]])
        penalty += 1000 * max(0.0, abs(length - 1) - rules.LENGTH_TOL_PT / lay.style['bond_length_pt']) ** 2
    exceptions = lay.panels[lay.pidx[lay.sp[sid]['panel']]]['angle_exceptions']
    for i, nb in enumerate(mol.adj):
        if mol.in_ring[i] or mol.is_linear(i) or mol.map[i] in lay.sp[sid]['residue']:
            continue
        ref = sid + ':' + str(mol.map[i])
        if ref in exceptions:
            continue
        p = xy[mol.map[i]]
        dirs = {j: layout.ang(xy[mol.map[j]][0] - p[0], xy[mol.map[j]][1] - p[1]) for j in nb}
        if len(nb) in (2, 3):
            ds = sorted(dirs.values())
            angles = [layout.adiff(*ds)] if len(nb) == 2 else [(ds[(k+1) % 3] - ds[k]) % 360 for k in range(3)]
            penalty += sum(max(0.0, abs(a - 120) - rules.ANGLE_TOL) ** 2 for a in angles)
        elif len(nb) == 4:
            ends = [j for j in nb if len(mol.adj[j]) == 1]
            doubles = [j for j in nb if mol.order[frozenset((i, j))] == 2]
            square = i not in mol.stereogenic and (len(ends) == 4 or len(doubles) >= 2
                     or not ends and all(len(mol.adj[j]) >= 3 for j in nb))
            wanted = [90] * 4 if square else [60, 60, 120, 120] if len(ends) in (1, 3) else [60, 90, 90, 120]
            ds = sorted(dirs.values())
            actual = sorted((ds[(k + 1) % 4] - ds[k]) % 360 for k in range(4))
            penalty += sum(max(0, abs(a - b) - rules.ANGLE_TOL) ** 2 for a, b in zip(actual, wanted))
    return penalty


def _number_room(lay, pid, points, labels, bonds):
    """Guide the search toward free, unambiguous seats for every requested number.

    RDKit already requires NumPy. Batch the grid so this heuristic is cheaper than
    a redraw; acceptance still uses the unchanged checkers on the saved drawing.
    """
    targets = lay.number_sizes.get(pid, ())
    if not targets:
        return 0.0
    import numpy as np
    bpt = lay.style['bond_length_pt']
    positions, boxes = dict(points), dict(labels)
    total = 0.0
    def box_gap(grid, others):
        dx = np.maximum(0, np.maximum(others[None,:,0]-grid[:,None,2], grid[:,None,0]-others[None,:,2]))
        dy = np.maximum(0, np.maximum(others[None,:,1]-grid[:,None,3], grid[:,None,1]-others[None,:,3]))
        return np.hypot(dx,dy)
    for ref, width, height in targets:
        p = positions[ref]
        centers = lay.number_grid + np.array(p)
        grid = np.column_stack((centers[:,0]-width/2, centers[:,1]-height/2,
                                centers[:,0]+width/2, centers[:,1]+height/2))
        own_box = boxes.get(ref, (*p, *p))
        own = box_gap(grid, np.array([own_box]))[:,0]
        near = [(r,q) for r,q in points if r!=ref and layout.dist(p,q)<3.0]
        other_boxes = np.array([boxes.get(r, (*q,*q)) for r,q in near])
        nearest = box_gap(grid, other_boxes).min(axis=1) if len(other_boxes) else np.full(len(grid),99.)
        letters = np.array([boxes[r] for r,_ in near if r in boxes])
        letter_gap = box_gap(grid, letters).min(axis=1) if len(letters) else np.full(len(grid),99.)
        samples = [(a[0]+t*(b[0]-a[0]), a[1]+t*(b[1]-a[1])) for _,_,a,b in bonds
                   if min(layout.dist(p,a),layout.dist(p,b))<2.0 for t in (0,.2,.4,.6,.8,1)]
        ink_boxes = np.array([(*q,*q) for q in samples])
        ink_gap = box_gap(grid, ink_boxes).min(axis=1) if len(ink_boxes) else np.full(len(grid),99.)
        penalty = np.maximum(0,own+(rules.NUMBER_OWN_MARGIN_PT+.5)/bpt-nearest)**2
        penalty += np.maximum(0,rules.NUMBER_LETTER_CLEAR_PT/bpt-letter_gap)**2
        penalty += np.maximum(0,(rules.NUMBER_CLEAR_PT+lay.style['line_width_pt']/2)/bpt-ink_gap)**2
        slack = np.minimum(nearest-own-(rules.NUMBER_OWN_MARGIN_PT+.5)/bpt,
                           np.minimum(letter_gap-rules.NUMBER_LETTER_CLEAR_PT/bpt,
                                      ink_gap-(rules.NUMBER_CLEAR_PT+lay.style['line_width_pt']/2)/bpt))
        penalty += .001/(np.maximum(0,slack)+.05)
        total += float(penalty.min())
    return 1000*total


def _blockers(lay, pid):
    """Bonds that stand between the two bonds a bond-to-bond arrow joins at their common atom: the arrow runs over them
    (the cost below sees labels only, so a drawing with an arrow over a bond looked perfect to it)."""
    count = 0
    for ar in lay.panels[lay.pidx[pid]]['arrows']:
        f, t = ar['from'], ar['to']
        if f['sid'] != t['sid'] or f['atom2'] is None or t['atom2'] is None:
            continue
        shared = {f['atom'], f['atom2']} & {t['atom'], t['atom2']}
        if len(shared) != 1:
            continue
        r = shared.pop()
        x = f['atom2'] if f['atom'] == r else f['atom']
        y = t['atom2'] if t['atom'] == r else t['atom']
        nb = lay.mol[f['sid']].nb_maps(r)
        xy = lay.xy[f['sid']]
        if x not in nb or y not in nb:
            continue
        dirs = {m: layout.ang(xy[m][0] - xy[r][0], xy[m][1] - xy[r][1]) for m in nb}
        span = (dirs[y] - dirs[x]) % 360.0
        lo, size = (dirs[x], span) if span <= 180 else (dirs[y], 360.0 - span)
        count += sum(1 for m, d in dirs.items() if m not in (x, y) and 0 < (d - lo) % 360.0 < size)
    return count


def _box_gaps(a, b):
    """check_rules.box_gap of each row of a with the same row of b."""
    dx = np.maximum(a[..., 0] - b[..., 2], b[..., 0] - a[..., 2])
    dy = np.maximum(a[..., 1] - b[..., 3], b[..., 1] - a[..., 3])
    return np.where((dx >= 0) | (dy >= 0), np.hypot(np.maximum(dx, 0.0), np.maximum(dy, 0.0)), np.maximum(dx, dy))


def _pt_box_gaps(x, y, boxes):
    """check_rules.pt_box_gap of every point (x, y) (rows, points) against every box (rows, boxes, 4): (rows, boxes,
    points)."""
    b0, b1, b2, b3 = (boxes[..., k, None] for k in range(4))
    x, y = x[..., None, :], y[..., None, :]
    dx = np.maximum(np.maximum(b0 - x, 0.0), x - b2)
    dy = np.maximum(np.maximum(b1 - y, 0.0), y - b3)
    depth = -np.minimum(np.minimum(x - b0, b2 - x), np.minimum(y - b1, b3 - y))
    return np.where((dx == 0) & (dy == 0), depth, np.hypot(dx, dy))


def _pt_in_boxes(x, y, boxes):
    """Whether one of the points (x, y) (rows, points) lies in each box (rows, boxes, 4), edges included: (rows, boxes).
    The same as _pt_box_gaps(x, y, boxes).min(axis=2) <= 0, without measuring the distances."""
    b0, b1, b2, b3 = (boxes[..., k, None] for k in range(4))
    x, y = x[..., None, :], y[..., None, :]
    return ((b0 <= x) & (x <= b2) & (b1 <= y) & (y <= b3)).any(axis=2)


def _seg_gaps(a, b, p, q):
    """_seg_gap of segment a-b against each segment p[k]-q[k]."""
    def side(u, v, w):
        return (v[..., 0] - u[..., 0]) * (w[..., 1] - u[..., 1]) - (v[..., 1] - u[..., 1]) * (w[..., 0] - u[..., 0])
    cross = (side(a, b, p) * side(a, b, q) < 0) & (side(p, q, a) * side(p, q, b) < 0)
    def to_seg(w, u, v):
        dx, dy = v[..., 0] - u[..., 0], v[..., 1] - u[..., 1]
        l2 = dx * dx + dy * dy
        t = np.where(l2 == 0, 0.0, np.clip(((w[..., 0] - u[..., 0]) * dx + (w[..., 1] - u[..., 1]) * dy)
                                           / np.where(l2 == 0, 1.0, l2), 0.0, 1.0))
        return np.hypot(w[..., 0] - (u[..., 0] + t * dx), w[..., 1] - (u[..., 1] + t * dy))
    d = np.minimum(np.minimum(to_seg(a, p, q), to_seg(b, p, q)), np.minimum(to_seg(p, a, b), to_seg(q, a, b)))
    return np.where(cross, 0.0, d)


def _crossed(figure):
    """{arrow id: atoms of the bonds its body crosses}: crossings that check_rules.py accepts (D192) but that a better
    layout may avoid; when another check fails, the layout search sees them as it sees a touched bond."""
    out = {}
    try:
        for aid, shapes in figure.scene()['arrows']:
            for (kind, atoms) in rules._arrow_crossings(figure, aid, shapes):
                if kind == 'bond':
                    out.setdefault(aid, set()).update(atoms)
    except Exception:
        return {}
    return out


def _arrow_chords(result, figure):
    """{panel: [(from refs, to refs)]} of the curved arrows that the failed overlap_arrow_bond or overlap_arrow_text
    findings name (an arrow kept from its way by a bond, a dotted line or a letter), and of
    every curved arrow in a panel where a hydrogen bond crossed a bond (moves that uncross it keep the arrows' ways)."""
    ids = {m.group(1) for n in ('overlap_arrow_bond', 'overlap_arrow_text') for x in result['checks'].get(n, {}).get('findings', [])
           for m in re.finditer(r'arrow (\S+) touches (?:h?bond|label)', x)} | set(_crossed(figure))
    out = {}
    for p in figure.resolved['panels']:
        for a in p.get('curved', []):
            if a.get('id') in ids or p['id'] in _hbond_crossed(result):
                out.setdefault(p['id'], []).append((list(a['from']['refs']), list(a['to']['refs'])))
    return out


def _hbond_crossed(result):
    """The panels where a hydrogen bond's line crossed a bond or another hydrogen bond's line."""
    return {m.group(1) for x in result['checks'].get('bond_crossing', {}).get('findings', [])
            for m in re.finditer(r'^hbond ([\w-]+)\.', x)}


def _species_part(lay, sid):
    """A species' own share of the cost (geometry, atoms, label boxes, bonds), kept while its coordinates stay the same:
    a move changes one species of the panel."""
    raw = lay.xy[sid]
    cache = lay.__dict__.setdefault('_cost_parts', {})
    try:
        key = (sid, tuple(raw.items()))
        part = cache.get(key)
    except TypeError:
        key, part = None, None
    if part is None:
        mol = lay.mol[sid]
        xy = lay.expanded(sid, raw)
        points, labels, bonds = [], [], []
        for i, atom in enumerate(mol.atoms):
            ref = sid + ':' + str(atom['map'])
            points.append((ref, xy[atom['map']]))
            if mol.labeled(i):
                labels.append((ref, lay.label_box(sid, atom['map'], xy)))
        for e in mol.order:
            a, b = (mol.map[i] for i in e)
            bonds.append((sid+':'+str(a), sid+':'+str(b), xy[a], xy[b]))
        part = (_geometry(lay, sid, raw), points, labels, bonds)
        if key is not None:
            if len(cache) > 100000:
                cache.clear()
            cache[key] = part
    return part


def _panel_lists(lay, pid):
    """The panel's geometry penalty and its atoms, label boxes and bonds."""
    labels, points, bonds = [], [], []
    geometry = 0.0
    for key in lay.panels[lay.pidx[pid]]['species']:
        g, p, l, b = _species_part(lay, pid + '.' + key)
        geometry += g
        points += p
        labels += l
        bonds += b
    return geometry, points, labels, bonds


def _name_owners(lay, labels):
    """For each label, the species whose residue name it is, or None (an atom label)."""
    cache = lay.__dict__.setdefault('_name_owners', {})
    key = tuple(ref for ref, _ in labels)
    if key not in cache:
        out = []
        for ref in key:
            sid, m = ref.rsplit(':', 1)
            mol = lay.mol[sid]
            out.append(sid if specutil.atom_kind(lay.sp[sid], mol.atoms[mol.idx[int(m)]]) == 'residue' else None)
        cache[key] = out
    return cache[key]


def _collisions(lay, pid, gap, points, labels, bonds, pts, boxes):
    """The collision sum of the panel for each row of pts (rows, atoms, 2) and boxes (rows, labels, 4): the same atoms,
    labels and bonds at other places.  In arrays, since the search prices every move it tries."""
    rows = len(pts)
    def total(x):
        return x.reshape(rows, -1).sum(axis=1)
    idx = {ref: k for k, (ref, _) in enumerate(points)}
    own = np.array([idx[ref] for ref, _ in labels], dtype=int)
    ends = np.array([(idx[a], idx[b]) for a, b, _, _ in bonds], dtype=int).reshape(-1, 2)
    P, Q = pts[:, ends[:, 0]], pts[:, ends[:, 1]]
    collision = np.zeros(rows)
    if len(labels):
        i, j = np.triu_indices(len(labels), 1)
        apart = _box_gaps(boxes[:, i], boxes[:, j])
        collision += total(4 * np.maximum(0.0, gap - apart) ** 2)
        # residue names of two molecules keep the layout's room between names, or neither reads as its own
        owners = _name_owners(lay, labels)
        names = np.array([owners[a] is not None and owners[b] is not None and owners[a] != owners[b]
                          for a, b in zip(i.tolist(), j.tolist())], dtype=bool)
        if names.any():
            collision += total(np.where(names, 4 * np.maximum(0.0, layout.TEXT_APART - apart) ** 2, 0.0))
        # Sample the center and edges, not just bond endpoints: a label may cover its middle.  Only a label and a bond
        # whose boxes come within the margin can add anything.
        mine = (ends[None, :, 0] == own[:, None]) | (ends[None, :, 1] == own[:, None])
        span = np.concatenate((np.minimum(P, Q), np.maximum(P, Q)), axis=2)
        k, l, e = np.nonzero((_box_gaps(boxes[:, :, None], span[:, None]) < gap + .05) & ~mine)
        if len(k):
            t = np.array([0, .2, .4, .5, .6, .8, 1])
            p, q = P[k, e], Q[k, e]
            g = _pt_box_gaps(p[:, None, 0] + t * (q[:, None, 0] - p[:, None, 0]),
                             p[:, None, 1] + t * (q[:, None, 1] - p[:, None, 1]), boxes[k, l][:, None])
            collision += np.bincount(k, (np.maximum(0.0, gap + .05 - g) ** 2).reshape(len(k), -1).sum(axis=1), rows)
    i, j = np.triu_indices(pts.shape[1], 1)
    d = pts[:, i] - pts[:, j]
    collision += total(np.maximum(0.0, .35 - np.hypot(d[..., 0], d[..., 1])) ** 2)
    # a hydrogen bond whose line crossed a bond: every line of the panel's hydrogen bonds against the other bonds and
    # the labels
    if pid in getattr(lay, 'hbond_panels', ()):
        lines = [(idx[ra], idx[rb]) for ra, rb in (('%s:%s' % (ha['sid'], ha['atom']), '%s:%s' % (hb['sid'], hb['atom']))
                                                 for ha, hb in lay.panels[lay.pidx[pid]]['hbonds'])
                 if ra in idx and rb in idx]
        if lines:
            lines = np.array(lines, dtype=int)
            a, b = pts[:, lines[:, 0]], pts[:, lines[:, 1]]
            mine = (ends[None, :, :, None] == lines[:, None, None, :]).any(axis=(2, 3))
            g = _seg_gaps(a[:, :, None], b[:, :, None], P[:, None], Q[:, None])
            collision += total(np.where(mine, 0.0, 4 * np.maximum(0.0, .2 - g) ** 2))
            # and against each other, two lines that share no atom
            u, v = np.triu_indices(len(lines), 1)
            apart = ~(lines[u][:, :, None] == lines[v][:, None, :]).any(axis=(1, 2))
            if apart.any():
                u, v = u[apart], v[apart]
                g = _seg_gaps(a[:, u], b[:, u], a[:, v], b[:, v])
                collision += total(4 * np.maximum(0.0, .2 - g) ** 2)
            if len(labels):
                t = np.array([.15, .3, .5, .7, .85])
                g = _pt_box_gaps((a[:, :, None, 0] + t * (b[:, :, None, 0] - a[:, :, None, 0])).reshape(rows, -1),
                                 (a[:, :, None, 1] + t * (b[:, :, None, 1] - a[:, :, None, 1])).reshape(rows, -1), boxes)
                mine = (own[:, None, None] == lines[None, :, :]).any(axis=2)
                collision += total(np.where(np.repeat(mine, len(t), axis=1), 0.0, 4 * np.maximum(0.0, .12 - g) ** 2))
    # a curved arrow that touched a bond: the bonds near the line between its ends, which its route has to pass
    for ends_from, ends_to in getattr(lay, 'arrow_chords', {}).get(pid, ()):
        if not all(r in idx for r in ends_from + ends_to):
            continue
        a = pts[:, [idx[r] for r in ends_from]].mean(axis=1)
        b = pts[:, [idx[r] for r in ends_to]].mean(axis=1)
        mine = set(idx[r] for r in ends_from + ends_to)
        other = np.array([e0 not in mine and e1 not in mine for e0, e1 in ends.tolist()], dtype=bool)
        collision += total(4 * np.maximum(0.0, .3 - _seg_gaps(a[:, None], b[:, None], P[:, other], Q[:, other])) ** 2)
    return collision


def _panel_cost(lay, pid, gap=0.14):
    """Conservative letters versus letters and non-own bonds, including separate species."""
    geometry, points, labels, bonds = _panel_lists(lay, pid)
    pts = np.array([p for _, p in points], dtype=float).reshape(1, -1, 2)
    boxes = np.array([box for _, box in labels], dtype=float).reshape(1, -1, 4)
    collision = float(_collisions(lay, pid, gap, points, labels, bonds, pts, boxes)[0])
    return 1000 * geometry + 100 * collision + 500 * _blockers(lay, pid) + _number_room(lay, pid, points, labels, bonds)


def _shifted_costs(lay, pid, gap, sid, places):
    """_panel_cost with species sid at each of places, the pair terms of all of them in one array pass."""
    keep, rows = lay.xy[sid], []
    for xy in places:
        lay.xy[sid] = xy
        geometry, points, labels, bonds = _panel_lists(lay, pid)
        rows.append((geometry, [q for _, q in points], [box for _, box in labels], _blockers(lay, pid),
                     _number_room(lay, pid, points, labels, bonds)))
    lay.xy[sid] = keep
    pts = np.array([r[1] for r in rows], dtype=float).reshape(len(rows), -1, 2)
    boxes = np.array([r[2] for r in rows], dtype=float).reshape(len(rows), -1, 4)
    collision = _collisions(lay, pid, gap, points, labels, bonds, pts, boxes)
    return [1000 * g + 100 * float(c) + 500 * blockers + room for (g, _, _, blockers, room), c in zip(rows, collision)]


def _allowed(lay, sid, xy, original):
    return all(layout.dist(xy[m], original[sid][m]) <= HAND_LIMIT + 1e-8 for m in lay.sp[sid]['xy'])


def _turn(xy, root, side, deg, mirror=False, axis=0.0):
    p = xy[root]
    out = dict(xy)
    if mirror:
        u = layout.vec(axis)
        for m in side:
            x, y = xy[m][0] - p[0], xy[m][1] - p[1]
            dot = x*u[0] + y*u[1]
            out[m] = (p[0] + 2*dot*u[0] - x, p[1] + 2*dot*u[1] - y)
    else:
        delta = {m: (xy[m][0]-p[0], xy[m][1]-p[1]) for m in side}
        out.update({m: (p[0]+x, p[1]+y) for m, (x,y) in layout.transform(delta, deg).items()})
    return out


def _unit_rings(mol, xy):
    """Project distorted ring bonds onto unit lengths; move attached trees with their root.

    A complicated bridged graph may still defeat a depiction engine. This bounded
    distance projection changes only its 2D coordinates; acceptance still requires
    the real bond-length, overlap and stereo checks.
    """
    ring = [(mol.map[i], mol.map[j]) for i,j in (tuple(e) for e in mol.ring_bonds)]
    out = dict(xy)
    for _ in range(400):
        worst = 0.0
        for a,b in ring:
            dx,dy = out[b][0]-out[a][0], out[b][1]-out[a][1]
            length = math.hypot(dx,dy)
            if length < 1e-8:
                continue
            worst = max(worst,abs(length-1))
            sx,sy = .45*(length-1)*dx/length, .45*(length-1)*dy/length
            out[a] = (out[a][0]+sx,out[a][1]+sy)
            out[b] = (out[b][0]-sx,out[b][1]-sy)
        if worst < 1e-5:
            break
    ring_maps = {m for e in ring for m in e}
    for root in sorted(ring_maps):
        i = mol.idx[root]
        dx,dy = out[root][0]-xy[root][0],out[root][1]-xy[root][1]
        for j in mol.adj[i]:
            if frozenset((i,j)) in mol.ring_bonds:
                continue
            side = {mol.map[k] for k in mol.subtree(j,i)}
            if not side.intersection(ring_maps):
                for m in side:
                    out[m] = (xy[m][0]+dx,xy[m][1]+dy)
    return out


def _alternatives(lay, sid, roots):
    mol, xy = lay.mol[sid], lay.xy[sid]
    for i in roots:
        root, nb = mol.map[i], mol.adj[i]
        if len(nb) < 2:
            continue
        sides = {j: {mol.map[k] for k in mol.subtree(j, i)} for j in nb
                 if frozenset((i, j)) not in mol.ring_bonds}
        if not sides:
            continue
        p = xy[root]
        dirs = {j: layout.ang(xy[mol.map[j]][0]-p[0], xy[mol.map[j]][1]-p[1]) for j in nb}
        # Preserve the largest/core side; use standard slots for every other whole branch.
        anchor = max(nb, key=lambda j: (j not in sides, len(sides.get(j, ())), -j))
        kids = [j for j in nb if j != anchor]
        if all(j in sides for j in kids) and len(nb) <= 4 and not mol.in_ring[i]:
            cand_sets = [layout.slot_dirs([dirs[anchor]], len(kids), s) for s in (1, -1)]
            for cs in cand_sets:
                for perm in itertools.permutations(cs, len(kids)):
                    out = dict(xy)
                    for j, dg in zip(kids, perm):
                        out = _turn(out, root, sides[j], dg-dirs[j])
                    yield out, '%s:%s standard bond slots' % (sid, root)
        for j, side in sides.items():
            if len(side) > mol.n / 2:
                continue
            # Flip a terminal group about its root bond; bond angles and ring shapes stay intact.
            out = _turn(xy, root, side, 0, mirror=True, axis=dirs[j])
            yield out, '%s:%s-%s branch reflection' % (sid, root, mol.map[j])
        for j, k in itertools.combinations(sorted(sides), 2):
            if len(sides[j]) > mol.n / 2 or len(sides[k]) > mol.n / 2:
                continue
            out = _turn(xy, root, sides[j], dirs[k]-dirs[j])
            out = _turn(out, root, sides[k], dirs[j]-dirs[k])
            yield out, '%s:%s swapped branch directions %s/%s' % (sid, root, mol.map[j], mol.map[k])


def _repair_lengths(lay, original):
    notes = []
    tolerance = (rules.LENGTH_TOL_PT - .01) / lay.style['bond_length_pt']
    for sid, mol in lay.mol.items():
        xy = lay.xy[sid]
        # Inheritance can turn a new ring into very long bonds. Redepict the SAME graph,
        # preserving stereo from its SMILES, rather than stretching copied coordinates.
        bad_ring = any(abs(layout.dist(xy[mol.map[i]], xy[mol.map[j]])-1) > tolerance
                       for i, j in (tuple(e) for e in mol.ring_bonds))
        if bad_ring:
            fresh, chain = layout.depict(lay.sp[sid])
            if any(abs(layout.dist(fresh[mol.map[i]], fresh[mol.map[j]])-1) > tolerance
                   for i,j in (tuple(e) for e in mol.ring_bonds)):
                alternative, alt_chain = layout.depict(lay.sp[sid], force_rdkit=True)
                def error(shape):
                    return sum((layout.dist(shape[mol.map[i]], shape[mol.map[j]])-1)**2
                               for i,j in (tuple(e) for e in mol.ring_bonds))
                if error(alternative) < error(fresh):
                    fresh, chain = alternative, alt_chain
            fresh = _unit_rings(mol, fresh)
            a = mol.map[0]
            b = mol.map[mol.adj[0][0]] if mol.adj[0] else a
            old_d = layout.ang(xy[b][0]-xy[a][0], xy[b][1]-xy[a][1])
            new_d = layout.ang(fresh[b][0]-fresh[a][0], fresh[b][1]-fresh[a][1])
            shifted = layout.transform({m:(x-fresh[a][0], y-fresh[a][1]) for m,(x,y) in fresh.items()}, old_d-new_d)
            candidate = {m:(x+xy[a][0], y+xy[a][1]) for m,(x,y) in shifted.items()}
            if not lay.sp[sid]['xy']:          # turned onto the 30-degree lattice, not left at the angle of one bond
                candidate = layout.straighten(mol, candidate)
            if _allowed(lay, sid, candidate, original):
                lay.xy[sid], lay.chain[sid] = candidate, chain
                notes.append(sid + ': redepicted changed ring topology from the original SMILES')
        xy = lay.xy[sid]
        for e in mol.order:
            if e in mol.ring_bonds:
                continue
            i, j = tuple(e)
            a, b = mol.map[i], mol.map[j]
            length = layout.dist(xy[a], xy[b])
            if abs(length-1) < tolerance or length < 1e-6:
                continue
            si, sj = mol.subtree(i,j), mol.subtree(j,i)
            move, fixed, side = (a,b,si) if len(si)<len(sj) else (b,a,sj)
            dx, dy = xy[move][0]-xy[fixed][0], xy[move][1]-xy[fixed][1]
            shift = (dx*(1/length-1), dy*(1/length-1))
            maps = {mol.map[k] for k in side}
            candidate = {m:(x+shift[0], y+shift[1]) if m in maps else (x,y) for m,(x,y) in xy.items()}
            if _allowed(lay, sid, candidate, original):
                lay.xy[sid] = xy = candidate
                notes.append('%s:%s-%s restored unit root bond' % (sid,a,b))
    return notes


def _number_nudges(lay, result, original):
    """A label-only cost can be zero while a number reads as a neighboring atom.

    Try small turns of the crowding neighbor's whole branch. Rings stay rigid,
    explicit coordinates keep HAND_LIMIT and the real drawing must pass all checks.
    """
    crowded = [n for n in result['tables']['numbers']
               if n['margin_pt'] <= rules.NUMBER_OWN_MARGIN_PT
               and n['nearest_other'].startswith('atom ')]
    base = copy.deepcopy(lay.xy)
    for deg in (5, -5, 9.5, -9.5):
        lay.xy = copy.deepcopy(base)
        notes = []
        for n in crowded:
            sid, mp = n['ref'].rsplit(':', 1)
            rival_sid, rival_mp = n['nearest_other'][5:].rsplit(':', 1)
            if sid != rival_sid:
                continue
            mol, xy = lay.mol[sid], lay.xy[sid]
            i, j = mol.idx[int(mp)], mol.idx[int(rival_mp)]
            if j not in mol.adj[i] or frozenset((i, j)) in mol.ring_bonds:
                continue
            side, root = mol.subtree(j, i), mol.map[i]
            if len(side) > mol.n/2:
                side, root = mol.subtree(i, j), mol.map[j]
            candidate = _turn(xy, root, {mol.map[k] for k in side}, deg)
            if (_allowed(lay, sid, candidate, original)
                    and _geometry(lay, sid, candidate) <= _geometry(lay, sid, xy) + 1e-8):
                lay.xy[sid] = candidate
                notes.append('%s: opened number seat at %s by turning branch %s degrees' % (sid, mp, deg))
        if notes:
            yield notes
    lay.xy = base


def _followers(lay, pid, sid, old, new):
    """Species placed against atoms of sid that this alternative moves go with those atoms, turned and shifted as
    their branch is, so a partner stays where it touches (an arrow's end, a bond of the next panel, a hydrogen bond).
    Only a partner whose every contact lies on one moved branch of sid; never the panel's first species or a
    species with hand coordinates.  Returns {species: new places}."""
    mol = lay.mol[sid]
    moved = {m for m in new if layout.dist(new[m], old[m]) > 1e-6}
    if not moved:
        return {}
    contacts = lay.contacts(pid)
    out = {}
    for key in lay.panels[lay.pidx[pid]]['species'][1:]:
        f = pid + '.' + key
        if f == sid or f not in lay.xy or lay.sp[f]['xy']:
            continue
        touch = [(sb, eb) if sa == f else (sa, ea) for sa, ea, sb, eb, _ in contacts if f in (sa, sb) and sa != sb]
        if not touch or any(o != sid for o, _ in touch):
            continue
        atoms = {m for _, e in touch for m in e if m is not None}
        if not atoms <= moved:
            continue
        branch, todo = set(), list(atoms)       # the moved branch these atoms lie on, and the atoms it hangs from
        while todo:
            m = todo.pop()
            if m not in branch:
                branch.add(m)
                todo += [n for n in mol.nb_maps(m) if n in moved]
        pts = branch | {n for m in branch for n in mol.nb_maps(m) if n not in moved}
        go = layout.rigid_fit({m: old[m] for m in pts}, {m: new[m] for m in pts})
        out[f] = {m: go(p) for m, p in lay.xy[f].items()}
    return out


def _arrow_hs(result, figure):
    """(arrow, H ref) for each H at an end of a curved arrow that touched a bond, a dotted line or a letter, or that
    crosses a bond (accepted, D192, but an H turned toward the arrow's other end may spare the crossing)."""
    ids = {m.group(1) for n in ('overlap_arrow_bond', 'overlap_arrow_text') for x in result['checks'].get(n, {}).get('findings', [])
           for m in re.finditer(r'arrow (\S+) touches (?:h?bond|label)', x)} | set(_crossed(figure))
    return [(a['id'], r) for p in figure.resolved['panels'] for a in p.get('curved', []) if a.get('id') in ids
            for r in dict.fromkeys(a['from']['refs'] + a['to']['refs'])
            if (figure.atoms.get(r) or {}).get('el') == 'H']


def _h_turns(lay, result, figure, original):
    """An H at an end of a curved arrow that touched a bond, turned about its atom into the open gap that faces the
    arrow's other end (an H a base takes), and the base's molecule moved next to it, 35 degrees off the line of the
    bond (so that the arrow's head points at the H, not along its bond) and clear of the rest.  When the other end is
    on the same molecule (a proton passed within it, as in a gem-diamine), the H alone turns, kept 0.75 bond from the
    molecule's other atoms: ({species: places}, notes), or None."""
    moved, notes = {}, []
    arrows = {a['id']: a for p in figure.resolved['panels'] for a in p.get('curved', [])}
    for aid, ref in _arrow_hs(result, figure):
        sid, m = ref.rsplit(':', 1)[0], int(ref.rsplit(':', 1)[1])
        mol = lay.mol.get(sid)
        if mol is None or m not in mol.idx or len(mol.adj[mol.idx[m]]) != 1:
            continue
        a = arrows[aid]
        ends = list(dict.fromkeys(a['from']['refs'] + a['to']['refs']))
        others = [r for r in ends if r.rsplit(':', 1)[0] != sid and r.rsplit(':', 1)[0] in lay.xy]
        if not others:
            xy = dict(moved.get(sid, lay.xy[sid]))
            root = mol.map[next(iter(mol.adj[mol.idx[m]]))]
            inner = [int(r.rsplit(':', 1)[1]) for r in ends if r.rsplit(':', 1)[0] == sid
                     and int(r.rsplit(':', 1)[1]) not in (m, root) and int(r.rsplit(':', 1)[1]) in xy]
            c = xy[root]
            dirs = [layout.ang(xy[q][0] - c[0], xy[q][1] - c[1]) for q in mol.nb_maps(root) if q != m]
            mids = [(st + g / 2.0) % 360 for st, g in layout.gaps(dirs) if g >= 100] if len(dirs) >= 2 and inner else []
            if not mids:
                continue
            t = xy[inner[0]]
            best = min(mids, key=lambda d: layout.adiff(d, layout.ang(t[0] - c[0], t[1] - c[1])))
            if layout.adiff(best, layout.ang(xy[m][0] - c[0], xy[m][1] - c[1])) < 30:
                continue
            u = layout.vec(best)
            xy[m] = (c[0] + u[0], c[1] + u[1])
            if min(layout.dist(xy[m], xy[q]) for q in xy if q not in (m, root)) < 0.75 or not _allowed(lay, sid, xy, original):
                continue
            moved[sid] = xy
            notes.append('%s: H %d turned toward %s:%d for arrow %s' % (sid, m, sid, inner[0], aid))
            continue
        osid, om = others[0].rsplit(':', 1)[0], int(others[0].rsplit(':', 1)[1])
        if osid in moved or om not in lay.xy[osid]:
            continue
        xy = dict(moved.get(sid, lay.xy[sid]))
        root = mol.map[next(iter(mol.adj[mol.idx[m]]))]
        c = xy[root]
        dirs = [layout.ang(xy[q][0] - c[0], xy[q][1] - c[1]) for q in mol.nb_maps(root) if q != m]
        if not dirs:
            continue
        target = lay.xy[osid][om]
        want = layout.ang(target[0] - c[0], target[1] - c[1])
        # an atom with one other bond (the O of a phenol or an alcohol): either seat 120 degrees from that bond
        mids = [(st + g / 2.0) % 360 for st, g in layout.gaps(dirs) if g >= 100] if len(dirs) > 1 else \
            [(dirs[0] + 120) % 360, (dirs[0] - 120) % 360]
        if not mids:
            continue
        best = min(mids, key=lambda d: layout.adiff(d, want))
        if layout.adiff(best, layout.ang(xy[m][0] - c[0], xy[m][1] - c[1])) < 30:
            continue
        u = layout.vec(best)
        xy[m] = (c[0] + u[0], c[1] + u[1])
        omol = lay.mol[osid]
        d = layout.CONTACT_BASE + 0.2 * mol.labeled(mol.idx[m]) + 0.2 * omol.labeled(omol.idx[om])
        mine = [lay.label_box(sid, q, xy) for q in xy if q != m]

        def room(at):
            theirs = [lay.label_box(osid, q, at) for q in at]
            return min(max(p[0] - q[2], q[0] - p[2], p[1] - q[3], q[1] - p[3]) for p in mine for q in theirs)
        oxy = None
        for extra in (0.0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0):
            tries = []
            for off in (35, -35):
                v = layout.vec(best + off)
                shift = (xy[m][0] + v[0] * (d + extra) - target[0], xy[m][1] + v[1] * (d + extra) - target[1])
                at = {q: (x + shift[0], y + shift[1]) for q, (x, y) in lay.xy[osid].items()}
                tries.append((room(at), at))
            gap, at = max(tries, key=lambda t: t[0])
            if gap >= 0.6:           # more than half a bond from the rest of the molecule
                oxy = at
                break
        if oxy is None or not (_allowed(lay, sid, xy, original) and _allowed(lay, osid, oxy, original)):
            continue
        moved[sid], moved[osid] = xy, oxy
        notes.append('%s: H %d turned toward %s for arrow %s' % (sid, m, others[0], aid))
    return (moved, notes) if moved else None


def draw_limit(result, figure):
    """How many candidates retry_layout draws: MAX_DRAWS, twice that when a curved arrow touched a bond, and MAX_DRAWS
    more when such an arrow ends at an H."""
    return MAX_DRAWS * ((2 if _arrow_chords(result, figure) else 1) + (1 if _arrow_hs(result, figure) else 0))


def _best_move(lay, pid, gap, roots, original):
    """The move of one species of the panel that lowers its cost most: (change, species, places, reason, followers),
    or None."""
    before = _panel_cost(lay, pid, gap)
    if before < 1e-7:
        return None
    best = None
    for sid in (s for s in roots if lay.sp[s]['panel'] == pid and roots[s]):
        xy = lay.xy[sid]
        for candidate, reason in _alternatives(lay, sid, roots[sid]):
            if not _allowed(lay, sid, candidate, original):
                continue
            follow = _followers(lay, pid, sid, xy, candidate)
            kept = {f: lay.xy[f] for f in follow}
            lay.xy[sid] = candidate
            lay.xy.update(follow)
            after = _panel_cost(lay, pid, gap)
            lay.xy.update(kept)
            if after < before-1e-6 and (best is None or after-before < best[0]):
                best = (after-before, sid, candidate, reason, follow)
        lay.xy[sid] = xy
    # A copied partner can occupy a changed substrate's space. Translate it
    # rigidly; arrange() then widens the panel and reanchors arrows and texts.
    keys = lay.panels[lay.pidx[pid]]['species']
    for key in keys[1:]:
        sid = pid + '.' + key
        xy = lay.xy[sid]
        places = []
        for dg in range(0,360,45):
            u = layout.vec(dg)
            for distance in (.1, .2, .3, .4, .8, 1.6):
                candidate = {m:(x+u[0]*distance,y+u[1]*distance) for m,(x,y) in xy.items()}
                if _allowed(lay,sid,candidate,original):
                    places.append((candidate, sid+': moved partner to clear the panel'))
        # turned or mirrored in place, for a partner whose hydrogen bond's line crosses: after the shifts
        if pid in getattr(lay, 'hbond_panels', ()):
            cx, cy = (sum(q[k] for q in xy.values()) / len(xy) for k in (0, 1))
            centered = {m: (x - cx, y - cy) for m, (x, y) in xy.items()}
            for deg, flip in [(d, None) for d in (30, -30, 60, -60, 90, -90, 120, -120, 150, -150, 180)] + [(0, 'x'), (0, 'y')]:
                candidate = {m: (x + cx, y + cy) for m, (x, y) in layout.transform(centered, deg, flip=flip).items()}
                if _allowed(lay, sid, candidate, original):
                    places.append((candidate, sid + (': mirrored partner %s to clear the panel' % (
                        'left-right' if flip == 'x' else 'up-down') if flip else ': turned partner %+d deg to clear the panel' % deg)))
        costs = _shifted_costs(lay, pid, gap, sid, [c for c, _ in places]) if places else ()
        for (candidate, reason), after in zip(places, costs):
            if after < before-1e-6 and (best is None or after-before < best[0]):
                best = (after-before,sid,candidate,reason,{})
        lay.xy[sid] = xy
    return best


def candidates(spec, result, figure):
    """Yield at most draw_limit real-drawing candidates; no changes to the caller's spec."""
    if not relevant(result):
        return
    lay = layout.Layout(spec)
    lay.run()
    lay.number_sizes = {}
    lay.arrow_chords = _arrow_chords(result, figure)
    lay.hbond_panels = _hbond_crossed(result)
    if (set(result['failed']).intersection({'number_place', 'number_clear'})
            and not set(result['failed']).intersection({'graph', 'stereochemistry', 'cdxml'})):
        import numpy as np
        lay.number_grid = np.array([(r*layout.vec(d)[0],r*layout.vec(d)[1])
                                   for d in range(0,360,10) for r in (.3,.4,.5,.6,.7,.8,1.,1.2)])
        bpt = lay.style['bond_length_pt']
        crowded_panels = set()
        for n in figure.report['numbers']:
            sid, mp = n['ref'].rsplit(':', 1)
            if len(lay.mol[sid].adj[lay.mol[sid].idx[int(mp)]]) == 4:
                crowded_panels.add(lay.sp[sid]['panel'])
        for n in figure.report['numbers']:
            if n['box'] and n['ref'].split('.')[0] in crowded_panels:
                b=n['box']
                lay.number_sizes.setdefault(n['ref'].split('.')[0], []).append((n['ref'],(b[2]-b[0])/bpt,(b[3]-b[1])/bpt))
    original = copy.deepcopy(lay.xy)
    notes = _repair_lengths(lay, original)
    affected = _refs(result, figure.atoms) | {r for atoms in _crossed(figure).values() for r in atoms}
    roots = {}
    for sid, mol in lay.mol.items():
        ids = {mol.idx[int(r.rsplit(':',1)[1])] for r in affected if r.rsplit(':',1)[0] == sid}
        # A labeled endpoint may collide because its grandparent's substituents face inward.
        for _ in range(2):
            ids |= {j for i in list(ids) for j in mol.adj[i]}
        roots[sid] = sorted(ids)
    emitted = set()
    # with a curved arrow that touched a bond, the moves are first searched with the bonds along its way counted,
    # then again without them (the ordinary search)
    chords, start_xy, start_notes, lay_notes = lay.arrow_chords, copy.deepcopy(lay.xy), list(notes), list(lay.notes)
    starts = [(start_xy, start_notes, c) for c in ([chords, {}] if chords else [{}])]
    # an H at an arrow's end turned toward the arrow's other end, as the first start of the search
    turned = _h_turns(lay, result, figure, original) if chords else None
    kept = {}
    if turned:
        starts.insert(0, (dict(start_xy, **turned[0]), start_notes + turned[1], chords))
        # the search from that start leaves the bonds of an atom whose H was turned as they are (re-seating them in
        # standard slots turns the H back)
        for sid, xy in turned[0].items():
            mol = lay.mol[sid]
            for m, p in xy.items():
                i = mol.idx[m]
                if mol.is_h[i] and len(mol.adj[i]) == 1 and layout.dist(p, start_xy[sid][m]) > 1e-6:
                    kept.setdefault(sid, set()).add(mol.adj[i][0])
        # first drawn as it is, then searched further from it
        lay.xy = copy.deepcopy(starts[0][0])
        lay.notes = list(dict.fromkeys(lay_notes + ['automatic layout: ' + n for n in starts[0][1]]))
        emitted.add(tuple((s, m, round(x, 4), round(y, 4)) for s, xy in lay.xy.items() for m, (x, y) in xy.items()))
        yield specutil._assemble(spec, lay.arrange())
    for k, (start, first_notes, lay.arrow_chords) in enumerate(starts):
        lay.xy, notes, lay.notes = copy.deepcopy(start), list(first_notes), list(lay_notes)
        use = {s: [i for i in r if i not in kept.get(s, ())] for s, r in roots.items()} if turned and k == 0 else roots
        for attempt in range(MAX_DRAWS):
            changed = False
            gap = .14 + .06 * attempt
            # panels do not share species, so a panel's best move stays the same until a move in that panel; the
            # moves are applied one at a time, best first over all panels
            found = {}
            for _ in range(MAX_MOVES):
                best = None
                for pid in lay.pidx:
                    if pid not in found:
                        found[pid] = _best_move(lay, pid, gap, use, original)
                    if found[pid] is not None and (best is None or found[pid][0] < best[0]):
                        best = found[pid]
                if best is None:
                    break
                _, sid, candidate, reason, follow = best
                found.pop(lay.sp[sid]['panel'], None)
                lay.xy[sid] = candidate
                lay.xy.update(follow)
                if follow:
                    reason += ' (%s moved with it)' % ', '.join(sorted(follow))
                notes.append(reason)
                changed = True
            if not changed and not notes:
                break
            signature = tuple((s,m,round(x,4),round(y,4)) for s,xy in lay.xy.items() for m,(x,y) in xy.items())
            if signature in emitted:
                continue
            emitted.add(signature)
            hand = [(s,m,layout.dist(lay.xy[s][m],original[s][m])) for s in lay.sp for m in lay.sp[s]['xy']
                    if layout.dist(lay.xy[s][m],original[s][m]) > 1e-6]
            lay.notes = list(dict.fromkeys(lay.notes + ['automatic layout: '+n for n in notes]))
            if hand:
                lay.notes.append('automatic layout: hand coordinates nudged by at most %.3f bond lengths (limit %.1f)' %
                                 (max(d for _,_,d in hand), HAND_LIMIT))
            yield specutil._assemble(spec, lay.arrange())
    # A failed number's own neighborhood: the label-only search above does not see it, so its candidates come after
    # the ordinary ones too (one drawing fixed the arrow, the next opens the number's seat).
    if not set(result['failed']).intersection({'graph', 'stereochemistry', 'cdxml'}):
        initial_notes = list(lay.notes)
        for nudges in _number_nudges(lay, result, original):
            lay.notes = initial_notes + ['automatic layout: '+n for n in nudges]
            hand = [layout.dist(lay.xy[s][m],original[s][m]) for s in lay.sp for m in lay.sp[s]['xy']]
            if hand:
                lay.notes.append('automatic layout: hand coordinates nudged by at most %.3f bond lengths (limit %.1f)' %
                                 (max(hand), HAND_LIMIT))
            yield specutil._assemble(spec, lay.arrange())



# ------------------------------------------------------------------ hydrogen-bond satellites
# A satellite takes part in its panel only through hydrogen bonds (a residue held by dotted lines): not the panel's
# first species, no curved arrow at it, no bond formed or broken with another species, nothing given about its place.
HB_CROSS = 0.05             # a dotted line closer than this to a bond or to another dotted line: a crossing (bonds)
HB_LABEL = 0.0              # a dotted line closer than this to another atom's label box: a contact (bonds)


def _ends(e):
    return e.get('members') or [e]


def satellites(lay, pid):
    """{sid: [(my map, other sid, other map)]} of the panel's satellites and their hydrogen bonds to other species."""
    panel = lay.panels[lay.pidx[pid]]
    keys = panel['species']
    if not keys:
        return {}
    sids = [pid + '.' + k for k in keys]
    busy = {sids[0]}
    busy |= {r['sid'] for ar in panel['arrows'] for e in (ar['from'], ar['to']) for r in _ends(e)}
    busy |= {r['sid'] for pair in panel.get('pseudobonds') or [] for r in pair if isinstance(r, dict) and 'sid' in r}
    for eta in panel.get('eta') or []:
        for r in [eta.get('metal')] + list(eta.get('atoms') or []):
            if isinstance(r, dict) and 'sid' in r:
                busy.add(r['sid'])
    where = {}
    for sid in sids:
        for m in lay.mol[sid].idx:
            where.setdefault(m, sid)
    for other in (lay.next_panel.get(pid), lay.prev_panel.get(pid)):      # a bond formed or broken between two species
        if other is None or other == pid:
            continue
        for key in lay.panels[lay.pidx[other]]['species']:
            mol = lay.mol[other + '.' + key]
            for e in mol.order:
                a, b = (mol.map[i] for i in e)
                sa, sb = where.get(a), where.get(b)
                if sa and sb and sa != sb:
                    busy |= {sa, sb}
    out = {}
    for sid in sids:
        sp = lay.sp[sid]
        if sid in busy or sp['xy'] or sp['offset'] or sp['rotate'] or sp['flip'] or sp['near'] is not None:
            continue
        links = [(b['atom'], a['sid'], a['atom']) if b['sid'] == sid else (a['atom'], b['sid'], b['atom'])
                 for a, b in panel['hbonds'] if sid in (a['sid'], b['sid']) and a['sid'] != b['sid']]
        if links:
            out[sid] = links
    return out


def _seg_box(p, q, box):
    """Gap between segment p-q and box (x1, y1, x2, y2): 0 when they meet."""
    x1, y1, x2, y2 = box
    if any(x1 <= r[0] <= x2 and y1 <= r[1] <= y2 for r in (p, q)):
        return 0.0
    corners = ((x1, y1), (x2, y1), (x2, y2), (x1, y2))
    return min(co.dist_seg_seg(p, q, corners[k], corners[(k + 1) % 4]) for k in range(4))


def _exit(p, q, box):
    """How far along p -> q (a fraction) the segment leaves box, which holds p (0 when it does not)."""
    x1, y1, x2, y2 = box
    if not (x1 <= p[0] <= x2 and y1 <= p[1] <= y2):
        return 0.0
    t = 1.0
    for k, lo, hi in ((0, x1, x2), (1, y1, y2)):
        d = q[k] - p[k]
        if d > 1e-12:
            t = min(t, (hi - p[k]) / d)
        elif d < -1e-12:
            t = min(t, (lo - p[k]) / d)
    return max(0.0, t)


def _panel_geometry(lay, pid, xy=None):
    """({(sid, map): point}, [((sid, map), label box)], [((sid, a), (sid, b))]) of what is placed in a panel, with
    xy ({sid: places}) standing in for some species."""
    pts, labels, bonds = {}, [], []
    for key in lay.panels[lay.pidx[pid]]['species']:
        sid = pid + '.' + key
        raw = (xy or {}).get(sid, lay.xy.get(sid))
        if raw is None:
            continue
        mol = lay.mol[sid]
        at = lay.expanded(sid, raw)
        for i, a in enumerate(mol.atoms):
            pts[(sid, a['map'])] = at[a['map']]
            if mol.labeled(i):
                labels.append(((sid, a['map']), lay.label_box(sid, a['map'], at)))
        for e in mol.order:
            a, b = (mol.map[i] for i in e)
            bonds.append(((sid, a), (sid, b)))
    return pts, labels, bonds


def _dotted(lay, p, q, boxes):
    """The drawn part of a dotted line between atoms at p and q: from the edge of each end's label box plus the label
    margin."""
    margin = lay.style['label_margin_pt'] / lay.style['bond_length_pt']
    length = layout.dist(p, q)
    if length < 1e-9:
        return p, q
    ta = _exit(p, q, boxes[0]) if boxes[0] else 0.0
    tb = _exit(q, p, boxes[1]) if boxes[1] else 0.0
    ta, tb = ta + margin / length, tb + margin / length
    if ta + tb >= 1.0:
        mid = ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2)
        return mid, mid
    return ((p[0] + ta * (q[0] - p[0]), p[1] + ta * (q[1] - p[1])), (q[0] + tb * (p[0] - q[0]), q[1] + tb * (p[1] - q[1])))


def hbond_trouble(lay, pid, xy=None, cross=None, touch=None):
    """What the layout predicts for the panel's hydrogen bonds: [(kind, line, other)] with kind 'bond' (the line
    crosses a bond), 'hbond' (another line) or 'label' (it touches another atom's label box)."""
    cross = HB_CROSS if cross is None else cross
    touch = HB_LABEL if touch is None else touch
    pts, labels, bonds = _panel_geometry(lay, pid, xy)
    box = dict(labels)
    lines = [((a['sid'], a['atom']), (b['sid'], b['atom'])) for a, b in lay.panels[lay.pidx[pid]]['hbonds']
             if (a['sid'], a['atom']) in pts and (b['sid'], b['atom']) in pts]
    out = []
    for k, (ra, rb) in enumerate(lines):
        p, q = pts[ra], pts[rb]
        for c, d in bonds:
            if c not in (ra, rb) and d not in (ra, rb) and co.dist_seg_seg(p, q, pts[c], pts[d]) < cross:
                out.append(('bond', (ra, rb), (c, d)))
        for rc, rd in lines[k + 1:]:
            if not {rc, rd} & {ra, rb} and co.dist_seg_seg(p, q, pts[rc], pts[rd]) < cross:
                out.append(('hbond', (ra, rb), (rc, rd)))
        u, v = _dotted(lay, p, q, (box.get(ra), box.get(rb)))
        if layout.dist(u, v) < 1e-9:
            continue
        for r, b in labels:
            if r not in (ra, rb) and _seg_box(u, v, b) <= touch:
                out.append(('label', (ra, rb), r))
    return out


SEAT_CANDIDATES = 400       # rigid seats tried per satellite
SEAT_EXACT = 20             # of them, the cheapest whose crossings and contacts are counted exactly
SEAT_FIT = 0.35             # a seat fitted to several hydrogen bonds misses each wanted end by at most this (bonds)
SEAT_SWEEPS = 2             # sweeps over the satellites after the first
SLACK = 1e-6                # rounding margin of the quick tests that rule out, before an exact one, what it would reject


def _hb_length(lay, box_a, box_b, u):
    """The length iupac_hbond_spacing asks of a hydrogen bond along unit u between atoms with label boxes box_a and
    box_b: a visible dotted span of two label heights plus each end's label and margin."""
    bpt = lay.style['bond_length_pt']
    height = layout.CAP * lay.style['font_size_pt'] / bpt
    margin = lay.style['label_margin_pt'] / bpt
    cut = sum((b[2] - b[0]) / 2 * abs(u[0]) + (b[3] - b[1]) / 2 * abs(u[1]) + margin for b in (box_a, box_b))
    return 2 * height + cut + .05


def _bond_dirs(lay, sid, m, xy):
    p = xy[m]
    return [layout.ang(xy[n][0] - p[0], xy[n][1] - p[1]) for n in lay.mol[sid].nb_maps(m) if n in xy]


def _top_dirs(lay, sid, m, xy, k=3):
    """The k freest directions at atom m: each gap's bisector, widest gap first, and for a gap of 150 degrees or more
    also 45 degrees either side of it."""
    dirs = _bond_dirs(lay, sid, m, xy)
    if not dirs:
        return [0.0, 120.0, 240.0][:k]
    out = []
    for start, size in sorted(layout.gaps(dirs), key=lambda g: -g[1]):
        mid = (start + size / 2.0) % 360
        out.append(mid)
        if size >= 150:
            out += [(mid + 45) % 360, (mid - 45) % 360]
    return out[:k]


def _axes(lay, sid, m, xy):
    """Directions at atom m of the satellite that should point at its hydrogen-bond partner: along the D-H bond for an
    H, else the bisectors of its widest gaps; [None] for a lone atom."""
    mol = lay.mol[sid]
    nb = [n for n in mol.nb_maps(m) if n in xy]
    if not nb:
        return [None]
    if mol.is_h[mol.idx[m]] and len(nb) == 1:
        d = xy[nb[0]]
        return [layout.ang(xy[m][0] - d[0], xy[m][1] - d[1])]
    return lay.open_dirs(sid, (m, None), xy) or [None]


def _turned(rel, rot, mirror):
    return layout.transform(rel, rot, mirror)


def _fit(old, new):
    """Turn (no mirror) and shift carrying points old onto new {key: point}: (function of a point, largest miss)."""
    ks = list(old)
    cx, cy = sum(old[k][0] for k in ks) / len(ks), sum(old[k][1] for k in ks) / len(ks)
    nx, ny = sum(new[k][0] for k in ks) / len(ks), sum(new[k][1] for k in ks) / len(ks)
    a = sum((old[k][0] - cx) * (new[k][0] - nx) + (old[k][1] - cy) * (new[k][1] - ny) for k in ks)
    b = sum((old[k][0] - cx) * (new[k][1] - ny) - (old[k][1] - cy) * (new[k][0] - nx) for k in ks)
    t = math.atan2(b, a)
    c, s = math.cos(t), math.sin(t)
    go = lambda p: (nx + c * (p[0] - cx) - s * (p[1] - cy), ny + s * (p[0] - cx) + c * (p[1] - cy))
    return go, max(layout.dist(go(old[k]), new[k]) for k in ks)


def _fitting(parts, shape):
    """The combinations of parts (itertools.product order; each part {my: target}) that _fit may carry within SEAT_FIT
    of their targets.  The fit of every combination is measured at once in arrays (whose rounding differs from _fit's
    in the last bits, hence SLACK) and _fit runs only on these, since few of the many combinations fit."""
    if not all(parts):
        return
    combos = np.indices([len(ways) for ways in parts]).reshape(len(parts), -1).T
    keys = [list(ways[0]) for ways in parts]           # the atoms of one option are the same in each of its parts
    start = np.cumsum([0] + [len(k) for k in keys])
    new = np.zeros((len(combos), start[-1], 2))
    old = np.zeros((start[-1], 2))
    for i, ways in enumerate(parts):
        rows = np.array([[part[my] for my in keys[i]] for part in ways], dtype=float)
        new[:, start[i]:start[i + 1]] = rows[combos[:, i]]
        old[start[i]:start[i + 1]] = [shape[my] for my in keys[i]]
    do = old - old.mean(axis=0)
    dn = new - new.mean(axis=1)[:, None]
    a = (do[:, 0] * dn[..., 0] + do[:, 1] * dn[..., 1]).sum(axis=1)
    b = (do[:, 0] * dn[..., 1] - do[:, 1] * dn[..., 0]).sum(axis=1)
    t = np.arctan2(b, a)
    c, s = np.cos(t)[:, None], np.sin(t)[:, None]
    miss = np.hypot(c * do[:, 0] - s * do[:, 1] - dn[..., 0], s * do[:, 0] + c * do[:, 1] - dn[..., 1]).max(axis=1)
    # where the turn is barely defined, rounding may turn it far: those are fitted too
    for k in np.nonzero((miss <= SEAT_FIT + SLACK) | (np.hypot(a, b) < SLACK))[0]:
        yield tuple(ways[j] for ways, j in zip(parts, combos[k]))


def _seat_candidates(lay, sid, links):
    """Rigid seats of satellite sid for its hydrogen bonds links [(my map, other sid, other map)]: [(places, family)]."""
    xy = lay.xy[sid]
    out = []
    mine_box = lambda m, at: lay.label_box(sid, m, at)
    other_box = {(o, om): lay.label_box(o, om, lay.xy[o]) for _, o, om in links}
    # 2. several hydrogen bonds on different atoms of the satellite: their ends fitted to free places at the partners
    per_atom = collections.OrderedDict()
    for my, o, om in links:
        per_atom.setdefault(my, (o, om))
    if len(per_atom) >= 2:
        groups = collections.OrderedDict()         # partner atom -> the satellite's atoms bonded to it
        for my in list(per_atom)[:4]:
            groups.setdefault(per_atom[my], []).append(my)
        options = []                               # per partner atom: [[(my, partner point, unit, partner box)]]
        for (o, om), mine in groups.items():
            pa = lay.xy[o][om]
            if len(mine) == 2:                     # two of its atoms (two H) at one partner atom: on either side of a
                bonds = _bond_dirs(lay, o, om, lay.xy[o])       # free direction of it, as far apart as they stand
                h = layout.dist(xy[mine[0]], xy[mine[1]])
                ways = []
                for d in range(0, 360, 15):
                    if all(layout.adiff(d, b) >= 30 for b in bonds):
                        half = math.degrees(math.asin(min(1.0, h / (2 * _hb_length(lay, mine_box(mine[0], xy), other_box[(o, om)], layout.vec(d))))))
                        for a, b in ((mine[0], mine[1]), (mine[1], mine[0])):
                            ways.append([(a, pa, layout.vec(d + half), other_box[(o, om)]),
                                         (b, pa, layout.vec(d - half), other_box[(o, om)])])
                options.append(ways)
            else:
                for my in mine:
                    options.append([[(my, pa, layout.vec(d), other_box[(o, om)])] for d in _top_dirs(lay, o, om, lay.xy[o])])
        for mirror in (False, True):
            shape = _turned(xy, 0.0, mirror)
            boxes = {my: mine_box(my, shape) for my in per_atom}     # the same for every combination below
            for scale in (1.0, 1.15):
                # each part's targets once: a combination only puts them together
                parts = [[{my: layout.add(pa, u, scale * _hb_length(lay, boxes[my], box, u)) for my, pa, u, box in part}
                          for part in ways] for ways in options]
                for combo in _fitting(parts, shape):
                    targets = {my: t for part in combo for my, t in part.items()}
                    go, miss = _fit({my: shape[my] for my in targets}, targets)
                    if miss <= SEAT_FIT:
                        out.append(({m: go(p) for m, p in shape.items()}, 'fit'))
    # 3. two hydrogen bonds at one atom of the satellite: the atom where two circles meet, the rest turned about it
    for my in dict.fromkeys(l[0] for l in links):
        partners = [(o, om) for m2, o, om in links if m2 == my]
        for (o1, m1), (o2, m2) in itertools.combinations(partners, 2):
            p1, p2 = lay.xy[o1][m1], lay.xy[o2][m2]
            d = layout.dist(p1, p2)
            if d < 1e-6:
                continue
            for mirror in (False, True):
                base = _turned({m: (p[0] - xy[my][0], p[1] - xy[my][1]) for m, p in xy.items()}, 0.0, mirror)
                for rot in range(0, 360, 15):
                    rel = _turned(base, rot, False)
                    box = mine_box(my, rel)
                    # the lengths along the line from each partner to the middle of the two (then refined once)
                    mid = ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2)
                    for side in (1, -1):
                        x = mid
                        for _ in range(2):
                            u1 = ((x[0] - p1[0]), (x[1] - p1[1]))
                            u2 = ((x[0] - p2[0]), (x[1] - p2[1]))
                            n1, n2 = math.hypot(*u1) or 1.0, math.hypot(*u2) or 1.0
                            r1 = _hb_length(lay, box, other_box[(o1, m1)], (u1[0] / n1, u1[1] / n1))
                            r2 = _hb_length(lay, box, other_box[(o2, m2)], (u2[0] / n2, u2[1] / n2))
                            a = (r1 * r1 - r2 * r2 + d * d) / (2 * d)
                            h2 = r1 * r1 - a * a
                            if h2 < 0:
                                break
                            ex, ey = (p2[0] - p1[0]) / d, (p2[1] - p1[1]) / d
                            h = math.sqrt(h2)
                            x = (p1[0] + a * ex - side * h * ey, p1[1] + a * ey + side * h * ex)
                        else:
                            out.append(({m: (x[0] + q[0], x[1] + q[1]) for m, q in rel.items()}, 'circles'))
    # 1. one hydrogen bond leading: the satellite's atom on a free direction of the partner's atom, pointing back at it
    ways = []                        # (link, delta, mirror, axis, direction), each seated at two lengths
    for n, (my, o, om) in enumerate(links):
        bonds = _bond_dirs(lay, o, om, lay.xy[o])
        free = [d for d in range(0, 360, 15) if all(layout.adiff(d, b) >= 30 for b in bonds)]
        axes = _axes(lay, sid, my, xy)
        for delta in (0, 15, -15, 30, -30):
            for mirror in (False, True):
                for a in axes:
                    ways += [(n, delta, mirror, a, d) for d in free]
                    if a is None:
                        break
    room = max(0, SEAT_CANDIDATES - len(out))
    picks = range(2 * len(ways))
    if len(picks) > room:            # spread the cut over the whole list (it is ordered by turn, then mirror)
        step = len(picks) / float(room) if room else 0
        picks = [int(k * step) for k in range(room)] if room else []
    singles, bases, made = [], {}, {}
    for k in picks:                  # only the seats the cut keeps are built
        n, delta, mirror, a, d = ways[k // 2]
        my, o, om = links[n]
        if k // 2 not in made:
            if (n, mirror) not in bases:
                bases[(n, mirror)] = _turned({m: (p[0] - xy[my][0], p[1] - xy[my][1]) for m, p in xy.items()}, 0.0, mirror)
            want = (d + 180 + delta) % 360
            rot = 0.0 if a is None else want - (-a if mirror else a)
            rel = _turned(bases[(n, mirror)], rot, False)
            u = layout.vec(d)
            made[k // 2] = (rel, u, _hb_length(lay, mine_box(my, rel), other_box[(o, om)], u))
        rel, u, length = made[k // 2]
        at = layout.add(lay.xy[o][om], u, (1.0, 1.15)[k % 2] * length)
        singles.append(({m: (at[0] + q[0], at[1] + q[1]) for m, q in rel.items()}, 'single'))
    return (out + singles)[:SEAT_CANDIDATES]


def _chords(lay, pid):
    """[(from refs, to refs)] of every curved arrow of the panel, as _collisions reads them."""
    out = []
    for ar in lay.panels[lay.pidx[pid]]['arrows']:
        ends = []
        for e in (ar['from'], ar['to']):
            refs = []
            for r in _ends(e):
                refs.append('%s:%s' % (r['sid'], r['atom']))
                if r.get('atom2') is not None:
                    refs.append('%s:%s' % (r['sid'], r['atom2']))
            ends.append(refs)
        out.append(tuple(ends))
    return out


def _seat_context(lay, pid, sid, links):
    """What the seat terms of satellite sid read from the rest of the panel, which stays put while it moves."""
    mol = lay.mol[sid]
    panel = lay.panels[lay.pidx[pid]]
    def centroid(refs):
        pts = [lay.xy[s][m] for s, m in refs if s in lay.xy and m in lay.xy[s]]
        return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)) if pts else None
    arrow_dirs = collections.defaultdict(list)       # (sid, map) at an arrow end -> direction to the other end
    chords = []
    for ar in panel['arrows']:
        ends = []
        for e in (ar['from'], ar['to']):
            refs = []
            for r in _ends(e):
                refs.append((r['sid'], r['atom']))
                if r.get('atom2') is not None:
                    refs.append((r['sid'], r['atom2']))
            ends.append(refs)
        a, b = centroid(ends[0]), centroid(ends[1])
        if a is None or b is None:
            continue
        chords.append((a, b, set(ends[0]) | set(ends[1])))
        for refs, there in ((ends[0], b), (ends[1], a)):
            for s, m in refs:
                if s in lay.xy and m in lay.xy[s] and layout.dist(lay.xy[s][m], there) > 1e-6:
                    p = lay.xy[s][m]
                    arrow_dirs[(s, m)].append(layout.ang(there[0] - p[0], there[1] - p[1]))
    rows = []
    for my, o, om in links:
        omol = lay.mol[o]
        donor_mine = mol.is_h[mol.idx[my]] and len(mol.adj[mol.idx[my]]) == 1
        donor_other = omol.is_h[omol.idx[om]] and len(omol.adj[omol.idx[om]]) == 1
        oxy = lay.xy[o]
        rows.append({'my': my, 'o': o, 'om': om, 'q': oxy[om], 'box': lay.label_box(o, om, oxy),
                     'my_d': mol.map[mol.adj[mol.idx[my]][0]] if donor_mine else None,
                     'o_d': oxy[omol.map[omol.adj[omol.idx[om]][0]]] if donor_other else None,
                     'o_bonds': [] if donor_other else _bond_dirs(lay, o, om, oxy),
                     'arrow': arrow_dirs.get((o, om), [])})
    # the same residue in the previous panel, seen from the same partner atom: keep its side
    prev = None
    pp = lay.prev_panel.get(pid)
    if pp is not None:
        psid = pp + '.' + lay.sp[sid]['key']
        for my, o, om in links:
            posid = pp + '.' + lay.sp[o]['key']
            if psid in lay.xy and posid in lay.xy and om in lay.xy[posid]:
                q = lay.xy[posid][om]
                rel = {m: (p[0] - q[0], p[1] - q[1]) for m, p in lay.xy[psid].items() if m in lay.xy[sid]}
                if rel:
                    prev = (o, om, rel)
                    break
    return {'rows': rows, 'chords': chords, 'prev': prev}


ARROW_BOW = 0.4             # how far a curved arrow may bow out from the line between its ends, per its length


def _ink_points(lay, sid, xy):
    mol = lay.mol[sid]
    pts = [xy[m] for m in mol.map]
    for e in mol.order:
        a, b = (mol.map[i] for i in e)
        pts.append(((xy[a][0] + xy[b][0]) / 2, (xy[a][1] + xy[b][1]) / 2))
    return pts


def _seat_terms(lay, sid, places, ctx):
    """Line length, direction, arrow seat (with the room each curved arrow bows into) and previous-panel terms of each
    seat."""
    out = []
    # a chord farther than this from a line on either axis adds nothing to it
    reach = [(min(a[0], b[0]) - .3 - SLACK, min(a[1], b[1]) - .3 - SLACK, max(a[0], b[0]) + .3 + SLACK,
              max(a[1], b[1]) + .3 + SLACK) for a, b, _ in ctx['chords']]
    for xy in places:
        pen = 0.0
        boxes = {}
        for r in ctx['rows']:
            p, q = xy[r['my']], r['q']
            got = layout.dist(p, q)
            if got < 1e-9:
                pen += 1e4
                continue
            u = ((p[0] - q[0]) / got, (p[1] - q[1]) / got)
            if r['my'] not in boxes:
                boxes[r['my']] = lay.label_box(sid, r['my'], xy)
            want = _hb_length(lay, boxes[r['my']], r['box'], u)
            pen += 2000.0 * max(0.0, want - got) ** 2 + 20.0 * max(0.0, got - 1.3 * want) ** 2
            out_dir = layout.ang(u[0], u[1])                  # from the partner to the satellite
            back = (out_dir + 180.0) % 360
            if r['my_d'] is not None:                         # D-H...A bent
                d = xy[r['my_d']]
                bend = 180.0 - layout.adiff(layout.ang(d[0] - p[0], d[1] - p[1]), back)
                pen += 0.05 * max(0.0, bend - 30.0) ** 2
            else:                                             # the satellite's acceptor: the line clear of its bonds
                bonds = _bond_dirs(lay, sid, r['my'], xy)
                if bonds:
                    pen += 0.05 * max(0.0, 40.0 - min(layout.adiff(back, b) for b in bonds)) ** 2
            if r['o_d'] is not None:
                d = r['o_d']
                bend = 180.0 - layout.adiff(layout.ang(d[0] - q[0], d[1] - q[1]), out_dir)
                pen += 0.05 * max(0.0, bend - 30.0) ** 2
            elif r['o_bonds']:
                pen += 0.05 * max(0.0, 40.0 - min(layout.adiff(out_dir, b) for b in r['o_bonds'])) ** 2
            for a in r['arrow']:                              # the partner atom an arrow starts or ends at
                pen += 0.05 * max(0.0, 60.0 - layout.adiff(out_dir, a)) ** 2
            for (a, b, refs), box in zip(ctx['chords'], reach):
                if (sid, r['my']) not in refs and (r['o'], r['om']) not in refs and not (
                        max(p[0], q[0]) < box[0] or min(p[0], q[0]) > box[2] or max(p[1], q[1]) < box[1] or min(p[1], q[1]) > box[3]):
                    pen += 400.0 * max(0.0, .3 - co.dist_seg_seg(p, q, a, b)) ** 2
        ink = None
        for a, b, refs in ctx['chords']:                     # keep out of the room a curved arrow bows into
            c2 = (b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2
            if c2 < .25:
                continue
            c = math.sqrt(c2)
            ink = _ink_points(lay, sid, xy) if ink is None else ink
            for x in ink:
                t = ((x[0] - a[0]) * (b[0] - a[0]) + (x[1] - a[1]) * (b[1] - a[1])) / c2
                if 0.0 < t < 1.0:
                    d = abs((x[0] - a[0]) * (b[1] - a[1]) - (x[1] - a[1]) * (b[0] - a[0])) / c
                    w = ARROW_BOW * c * 4 * t * (1 - t) + .35
                    if d < w:
                        pen += 10.0 * (w - d) ** 2
        if ctx['prev'] is not None:
            o, om, rel = ctx['prev']
            q = lay.xy[o][om]
            pen += 10.0 * sum((xy[m][0] - q[0] - v[0]) ** 2 + (xy[m][1] - q[1] - v[1]) ** 2 for m, v in rel.items()) / len(rel)
        out.append(pen)
    return out


def _shortfalls(lay, sid, places, other):
    """lay.shortfall of each seat, in arrays."""
    g = layout.SPECIES_GAP
    opts = np.array(other[0], dtype=float).reshape(-1, 2)
    oboxes = np.array(other[1], dtype=float).reshape(-1, 4)
    out = []
    for k in range(0, len(places), 50):
        inks = [lay.ink(sid, xy) for xy in places[k:k + 50]]
        P = np.array([i[0] for i in inks], dtype=float).reshape(len(inks), -1, 2)
        B = np.array([i[1] for i in inks], dtype=float).reshape(len(inks), -1, 4)
        lo = np.minimum(P.min(axis=(0, 1)) if P.shape[1] else np.inf, B[..., :2].min(axis=(0, 1)) if B.shape[1] else np.inf) - g
        hi = np.maximum(P.max(axis=(0, 1)) if P.shape[1] else -np.inf, B[..., 2:].max(axis=(0, 1)) if B.shape[1] else -np.inf) + g
        q = opts[(opts[:, 0] >= lo[0]) & (opts[:, 0] <= hi[0]) & (opts[:, 1] >= lo[1]) & (opts[:, 1] <= hi[1])]
        c = oboxes[(oboxes[:, 2] >= lo[0]) & (oboxes[:, 0] <= hi[0]) & (oboxes[:, 3] >= lo[1]) & (oboxes[:, 1] <= hi[1])]
        s = np.zeros(len(inks))
        Pb = np.concatenate((P, P), axis=2)                   # points as boxes
        qb = np.concatenate((q, q), axis=1)
        for a in (Pb, B):
            for b in (qb, c):
                if a.shape[1] and len(b):
                    s += _near_gaps(a, b, g).sum(axis=(1, 2))
        out += list(s)
    return out


def _near_gaps(a, b, g):
    """max(0, g - box gap) ** 2 of each box of a (rows, n, 4) with each box of b (m, 4): (rows, n, m).  The gap is
    measured only for the pairs that come within g (and SLACK) along both axes, few of them; the others add 0."""
    dx = np.maximum(a[:, :, None, 0] - b[:, 2], b[:, 0] - a[:, :, None, 2])
    dy = np.maximum(a[:, :, None, 1] - b[:, 3], b[:, 1] - a[:, :, None, 3])
    out = np.zeros(dx.shape)
    near = (dx < g + SLACK) & (dy < g + SLACK)
    dx, dy = dx[near], dy[near]
    d = np.where((dx >= 0) | (dy >= 0), np.hypot(np.maximum(dx, 0.0), np.maximum(dy, 0.0)), np.maximum(dx, dy))
    out[near] = np.maximum(0.0, g - d) ** 2
    return out


def _apart(p, q, box, gap):
    """Whether segment p-q stays farther than gap (and SLACK) from box (x1, y1, x2, y2) along x or y: then neither
    _seg_box nor dist_seg_seg finds them within gap."""
    g = gap + SLACK
    return (max(p[0], q[0]) < box[0] - g or min(p[0], q[0]) > box[2] + g
            or max(p[1], q[1]) < box[1] - g or min(p[1], q[1]) > box[3] + g)


def _sat_count(lay, pid, sid, xy, links):
    """(hydrogen-bond, body) intersections that involve satellite sid at places xy: its dotted lines against every bond,
    line and label of the panel, the other lines against its bonds and labels; its bonds against the other species'
    bonds and labels, its labels against theirs, and 1 more when it comes closer than SPECIES_GAP to them."""
    pts, labels, bonds = _panel_geometry(lay, pid, {sid: xy})
    box = dict(labels)
    lines = [((a['sid'], a['atom']), (b['sid'], b['atom'])) for a, b in lay.panels[lay.pidx[pid]]['hbonds']
             if (a['sid'], a['atom']) in pts and (b['sid'], b['atom']) in pts]
    mine_b = [b for b in bonds if b[0][0] == sid]
    other_b = [b for b in bonds if b[0][0] != sid]
    mine_l = [l for l in labels if l[0][0] == sid]
    other_l = [l for l in labels if l[0][0] != sid]
    hb = body = 0
    for k, (ra, rb) in enumerate(lines):
        p, q = pts[ra], pts[rb]
        own = sid in (ra[0], rb[0])
        lo = (min(p[0], q[0]) - .1, min(p[1], q[1]) - .1)
        hi = (max(p[0], q[0]) + .1, max(p[1], q[1]) + .1)
        near = lambda c, d: not (max(pts[c][0], pts[d][0]) < lo[0] or min(pts[c][0], pts[d][0]) > hi[0]
                                 or max(pts[c][1], pts[d][1]) < lo[1] or min(pts[c][1], pts[d][1]) > hi[1])
        for c, d in (bonds if own else mine_b):
            if c not in (ra, rb) and d not in (ra, rb) and near(c, d) and co.dist_seg_seg(p, q, pts[c], pts[d]) < HB_CROSS:
                hb += 1
        for rc, rd in lines[k + 1:]:
            if (own or sid in (rc[0], rd[0])) and not {rc, rd} & {ra, rb} and near(rc, rd) \
                    and co.dist_seg_seg(p, q, pts[rc], pts[rd]) < HB_CROSS:
                hb += 1
        u, v = _dotted(lay, p, q, (box.get(ra), box.get(rb)))
        if layout.dist(u, v) < 1e-9:
            continue
        for r, b in (labels if own else mine_l):
            if r not in (ra, rb) and b[2] >= lo[0] and b[0] <= hi[0] and b[3] >= lo[1] and b[1] <= hi[1] \
                    and not _apart(u, v, b, HB_LABEL) and _seg_box(u, v, b) <= HB_LABEL:
                hb += 1
    if mine_b or mine_l:
        xs = [pts[r][0] for r in pts if r[0] == sid]
        ys = [pts[r][1] for r in pts if r[0] == sid]
        lo, hi = (min(xs) - 1.0, min(ys) - 1.0), (max(xs) + 1.0, max(ys) + 1.0)
        inside = lambda p: lo[0] <= p[0] <= hi[0] and lo[1] <= p[1] <= hi[1]
        ob = [(c, d) for c, d in other_b if inside(pts[c]) or inside(pts[d])]
        ol = [(r, b) for r, b in other_l if inside(((b[0] + b[2]) / 2, (b[1] + b[3]) / 2))]
        spans = [(min(pts[c][0], pts[d][0]), min(pts[c][1], pts[d][1]), max(pts[c][0], pts[d][0]), max(pts[c][1], pts[d][1]))
                 for c, d in ob]
        for a, b in mine_b:
            for (c, d), span in zip(ob, spans):
                if not _apart(pts[a], pts[b], span, HB_CROSS) and co.dist_seg_seg(pts[a], pts[b], pts[c], pts[d]) < HB_CROSS:
                    body += 1
            for r, bx in ol:
                if not _apart(pts[a], pts[b], bx, 0.0) and _seg_box(pts[a], pts[b], bx) <= 0.0:
                    body += 1
        for r, bx in mine_l:
            for c, d in ob:
                if not _apart(pts[c], pts[d], bx, 0.0) and _seg_box(pts[c], pts[d], bx) <= 0.0:
                    body += 1
            for r2, b2 in ol:
                if layout._box_gap(bx, b2) < 0.0:
                    body += 1
    return hb, body


def _line_estimate(lay, pid, sid, places):
    """For each seat, about how many bonds, dotted lines and labels of the panel the satellite's dotted lines cross or
    touch, and how many dotted lines of the others cross its bonds (in arrays; the exact count comes later for the
    best seats)."""
    panel = lay.panels[lay.pidx[pid]]
    mol = lay.mol[sid]
    maps = list(mol.map)
    col = {m: k for k, m in enumerate(maps)}
    X = np.array([[xy[m] for m in maps] for xy in places], dtype=float)
    mine = [(a['atom'], (b['sid'], b['atom'])) if a['sid'] == sid else (b['atom'], (a['sid'], a['atom']))
            for a, b in panel['hbonds'] if sid in (a['sid'], b['sid']) and a['sid'] != b['sid']]
    others = [((a['sid'], a['atom']), (b['sid'], b['atom'])) for a, b in panel['hbonds']
              if sid not in (a['sid'], b['sid']) and a['sid'] in lay.xy and b['sid'] in lay.xy]
    obonds = []
    for key in panel['species']:
        o = pid + '.' + key
        if o == sid or o not in lay.xy:
            continue
        om = lay.mol[o]
        obonds += [((o, om.map[i]), (o, om.map[j])) for i, j in map(tuple, om.order)]
    own = [(mol.map[i], mol.map[j]) for i, j in map(tuple, mol.order)]
    _, labels, _ = _panel_geometry(lay, pid)
    labels = [(r, b) for r, b in labels if r[0] != sid]
    labeled = [mol.map[i] for i in range(mol.n) if mol.labeled(i)]
    t = np.array([.25, .375, .5, .625, .75])
    count = np.zeros(len(places))
    for my, (o, m) in mine:
        p = X[:, col[my]]
        q = np.array(lay.xy[o][m], dtype=float)
        # the line's middle against the other species' label boxes and its own letters (a box around each)
        sx = p[:, 0, None] + t * (q[0] - p[:, 0, None])
        sy = p[:, 1, None] + t * (q[1] - p[:, 1, None])
        boxes = np.array([b for r, b in labels if r != (o, m)], dtype=float).reshape(-1, 4)
        if len(boxes):
            count += _pt_in_boxes(sx, sy, np.broadcast_to(boxes, (len(places),) + boxes.shape)).sum(axis=1)
        near = [col[k] for k in labeled if k != my]
        if near:
            c = X[:, near]
            mbox = np.concatenate((c - (.3, .25), c + (.3, .25)), axis=2)
            count += _pt_in_boxes(sx, sy, mbox).sum(axis=1)
        rows = [b for b in obonds if (o, m) not in b] + [l for l in others if (o, m) not in l]
        if rows:
            P = np.array([lay.xy[a[0]][a[1]] for a, _ in rows], dtype=float)
            Q = np.array([lay.xy[b[0]][b[1]] for _, b in rows], dtype=float)
            g = _seg_gaps(p[:, None], np.broadcast_to(q, p.shape)[:, None], P[None], Q[None])
            count += (g < HB_CROSS).sum(axis=1)
        body = [(a, b) for a, b in own if my not in (a, b)]
        if body:
            P = X[:, [col[a] for a, _ in body]]
            Q = X[:, [col[b] for _, b in body]]
            g = _seg_gaps(p[:, None], np.broadcast_to(q, p.shape)[:, None], P, Q)
            count += (g < HB_CROSS).sum(axis=1)
    if others and own:
        P = X[:, [col[a] for a, _ in own]]
        Q = X[:, [col[b] for _, b in own]]
        for a, b in others:
            pa = np.array(lay.xy[a[0]][a[1]], dtype=float)
            pb = np.array(lay.xy[b[0]][b[1]], dtype=float)
            g = _seg_gaps(np.broadcast_to(pa, P.shape), np.broadcast_to(pb, P.shape), P, Q)
            count += (g < HB_CROSS).sum(axis=1)
    return count


SEAT_SHORT = 150            # seats whose distance to the other species is measured, after a first cheap ranking
SEAT_PRICED = 60            # of them, the seats priced with the whole panel's cost


def _seat_best(lay, pid, sid, links):
    """The best rigid seat of satellite sid: (key, places, index) with key (intersections, cost); index 0 is the
    current seat."""
    return _seat_options(lay, pid, sid, links)[0]


def _seat_options(lay, pid, sid, links):
    """The rigid seats of satellite sid whose crossings are counted exactly, best first: [(key, places, index)]."""
    cur = lay.xy[sid]
    places = [cur] + [c for c, _ in _seat_candidates(lay, sid, links)]
    ctx = _seat_context(lay, pid, sid, links)
    other = lay.panel_ink(pid, skip=sid)
    terms = _seat_terms(lay, sid, places, ctx)
    rough = _line_estimate(lay, pid, sid, places)
    order = [0] + sorted(range(1, len(places)), key=lambda k: terms[k] + 300.0 * rough[k])[:SEAT_SHORT]
    short = dict(zip(order, _shortfalls(lay, sid, [places[k] for k in order], other)))
    first = [0] + sorted(order[1:], key=lambda k: terms[k] + 300.0 * short[k] + 300.0 * rough[k])[:SEAT_PRICED]
    base = dict(zip(first, _shifted_costs(lay, pid, .14, sid, [places[k] for k in first])))
    cost = {k: base[k] + terms[k] + 300.0 * short[k] for k in first}
    picks = [0] + sorted(first[1:], key=lambda k: cost[k] + 300.0 * rough[k])[:SEAT_EXACT]
    out = []
    for k in picks:
        hb, body = _sat_count(lay, pid, sid, places[k], links)
        out.append(((hb + body + (1 if short[k] > .01 else 0), round(cost[k], 6)), places[k], k))
    return sorted(out, key=lambda o: o[0])


_SEATS = collections.OrderedDict()       # the seat step's result per panel input: retries lay out the same panels again


def _seat_key(lay, pid, sats):
    """Everything the seat step of a panel reads: its hydrogen bonds, arrows and species with their places, and the
    places in the previous panel."""
    panel = lay.panels[lay.pidx[pid]]
    parts = [pid, repr(panel['hbonds']), repr(panel['arrows']), repr(sorted(lay.style.items())), repr(sorted(sats.items()))]
    for q in (pid, lay.prev_panel.get(pid)):
        if q is None:
            continue
        for key in lay.panels[lay.pidx[q]]['species']:
            sid = q + '.' + key
            if sid in lay.xy:
                sp = lay.sp[sid]
                parts.append((sid, repr(sp['graph']), repr(sorted(sp['residue'].items())), repr(sorted(sp['label'].items())),
                              repr(sorted(sp['abbrev'].items())), repr(sp['show']),
                              tuple(sorted((m, round(x, 6), round(y, 6)) for m, (x, y) in lay.xy[sid].items()))))
    return hashlib.sha1(repr(parts).encode()).hexdigest()


def panel_cost(lay, pid):
    """_panel_cost of one panel as the layout stands, outside seat_satellites (which sets the same inputs)."""
    keep = {k: lay.__dict__[k] for k in ('arrow_chords', 'hbond_panels', 'number_sizes') if k in lay.__dict__}
    try:
        lay.arrow_chords, lay.hbond_panels, lay.number_sizes = {pid: _chords(lay, pid)}, {pid}, keep.get('number_sizes', {})
        return _panel_cost(lay, pid)
    finally:
        for k in ('arrow_chords', 'hbond_panels', 'number_sizes'):
            lay.__dict__.pop(k, None)
        lay.__dict__.update(keep)


def seat_satellites(lay):
    """Layout.run, after the dotted-bond spacing: in a panel where the layout predicts that a hydrogen bond's dotted line
    crosses a bond or another line, or touches a label (hbond_trouble), each satellite (a residue held only by
    hydrogen bonds) is put on the rigid seat that crosses and touches least, most hydrogen bonds first, then swept
    again; the panel keeps the new seats only when the predicted count goes down.  Hand-placed species, species an
    arrow or a bond joins and the panel's first species stay."""
    keep = {k: lay.__dict__[k] for k in ('arrow_chords', 'hbond_panels', 'number_sizes') if k in lay.__dict__}
    try:
        for p in lay.panels:
            pid = p['id']
            sats = {s: l for s, l in satellites(lay, pid).items() if s in lay.xy and all(o in lay.xy for _, o, _ in l)}
            if not sats:
                continue
            before = hbond_trouble(lay, pid)
            if not before:
                continue
            key = _seat_key(lay, pid, sats)
            if key in _SEATS:
                seats, note = _SEATS[key]
                lay.xy.update({s: dict(xy) for s, xy in seats.items()})
                if note:
                    lay.notes.append(note)
                continue
            lay.arrow_chords = {pid: _chords(lay, pid)}
            lay.hbond_panels = {pid}
            lay.number_sizes = keep.get('number_sizes', {})
            start = {s: lay.xy[s] for s in sats}
            body0 = sum(_sat_count(lay, pid, s, lay.xy[s], l)[1] for s, l in sats.items())
            first = [s for s, l in sats.items() if any(o not in sats for _, o, _ in l)]
            later = [s for s in sats if s not in first]
            order = sorted(first, key=lambda s: -len(sats[s])) + sorted(later, key=lambda s: -len(sats[s]))
            for sweep in range(1 + SEAT_SWEEPS):
                moved = False
                for sid in order:
                    if sweep and sum(_sat_count(lay, pid, sid, lay.xy[sid], sats[sid])) == 0:
                        continue                # the sweeps again: only a satellite that still crosses or touches
                    # first, a satellite tied to the rest is seated for those ties only: one tied only to satellites
                    # comes after it, to where it then stands
                    links = [l for l in sats[sid] if sweep or sid in later or l[1] not in later] or sats[sid]
                    best = _seat_best(lay, pid, sid, links)
                    if best is not None and best[2] != 0:
                        lay.xy[sid] = best[1]
                        moved = True
                if not moved:
                    break
            after = hbond_trouble(lay, pid)
            body1 = sum(_sat_count(lay, pid, s, lay.xy[s], l)[1] for s, l in sats.items())
            changed = sorted(s for s in sats if lay.xy[s] is not start[s])
            note = None
            if changed and len(after) < len(before) and body1 <= body0:
                note = ('panel %s: hydrogen-bonded %s re-seated (predicted dotted-line crossings and label contacts '
                        '%d -> %d)' % (pid, ', '.join(changed), len(before), len(after)))
                lay.notes.append(note)
            else:
                lay.xy.update(start)
            _SEATS[key] = ({s: dict(lay.xy[s]) for s in sats}, note)
            while len(_SEATS) > 200:
                _SEATS.popitem(last=False)
    finally:
        for k in ('arrow_chords', 'hbond_panels', 'number_sizes'):
            lay.__dict__.pop(k, None)
        lay.__dict__.update(keep)
