# SPDX-License-Identifier: Apache-2.0
"""Bounded, chemistry-preserving alternatives, only for a drawing that failed its checks.

The cheap search uses the layout's label boxes; acceptance uses every rule on the real
saved/reloaded drawing. Rings stay rigid, bridges move as whole branches, and wedges are
recomputed from the original SMILES. Explicit coordinates may move at most 0.3 bonds.
"""
import copy
import itertools
import math
import re

import numpy as np

import check_rules as rules
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


def _arrow_chords(result, figure):
    """{panel: [(from refs, to refs)]} of the curved arrows that the failed overlap_arrow_bond findings name, and of
    every curved arrow in a panel where a hydrogen bond crossed a bond (moves that uncross it keep the arrows' ways)."""
    ids = {m.group(1) for x in result['checks'].get('overlap_arrow_bond', {}).get('findings', [])
           for m in re.finditer(r'arrow (\S+) touches bond', x)}
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
        collision += total(4 * np.maximum(0.0, gap - _box_gaps(boxes[:, i], boxes[:, j])) ** 2)
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
    """(arrow, H ref) for each H at an end of a curved arrow that touched a bond."""
    ids = {m.group(1) for x in result['checks'].get('overlap_arrow_bond', {}).get('findings', [])
           for m in re.finditer(r'arrow (\S+) touches bond', x)}
    return [(a['id'], r) for p in figure.resolved['panels'] for a in p.get('curved', []) if a.get('id') in ids
            for r in dict.fromkeys(a['from']['refs'] + a['to']['refs'])
            if (figure.atoms.get(r) or {}).get('el') == 'H']


def _h_turns(lay, result, figure, original):
    """An H at an end of a curved arrow that touched a bond, turned about its atom into the open gap that faces the
    arrow's other end (an H a base takes), and the base's molecule moved next to it, 35 degrees off the line of the
    bond (so that the arrow's head points at the H, not along its bond) and clear of the rest:
    ({species: places}, notes), or None."""
    moved, notes = {}, []
    arrows = {a['id']: a for p in figure.resolved['panels'] for a in p.get('curved', [])}
    for aid, ref in _arrow_hs(result, figure):
        sid, m = ref.rsplit(':', 1)[0], int(ref.rsplit(':', 1)[1])
        mol = lay.mol.get(sid)
        if mol is None or m not in mol.idx or len(mol.adj[mol.idx[m]]) != 1:
            continue
        a = arrows[aid]
        others = [r for r in dict.fromkeys(a['from']['refs'] + a['to']['refs'])
                  if r.rsplit(':', 1)[0] != sid and r.rsplit(':', 1)[0] in lay.xy]
        if not others:
            continue
        osid, om = others[0].rsplit(':', 1)[0], int(others[0].rsplit(':', 1)[1])
        if osid in moved or om not in lay.xy[osid]:
            continue
        xy = dict(moved.get(sid, lay.xy[sid]))
        root = mol.map[next(iter(mol.adj[mol.idx[m]]))]
        c = xy[root]
        dirs = [layout.ang(xy[q][0] - c[0], xy[q][1] - c[1]) for q in mol.nb_maps(root) if q != m]
        if len(dirs) < 2:
            continue
        target = lay.xy[osid][om]
        want = layout.ang(target[0] - c[0], target[1] - c[1])
        mids = [(st + g / 2.0) % 360 for st, g in layout.gaps(dirs) if g >= 100]
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
    affected = _refs(result, figure.atoms)
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
    if turned:
        starts.insert(0, (dict(start_xy, **turned[0]), start_notes + turned[1], chords))
        # first drawn as it is, then searched further from it
        lay.xy = copy.deepcopy(starts[0][0])
        lay.notes = list(dict.fromkeys(lay_notes + ['automatic layout: ' + n for n in starts[0][1]]))
        emitted.add(tuple((s, m, round(x, 4), round(y, 4)) for s, xy in lay.xy.items() for m, (x, y) in xy.items()))
        yield specutil._assemble(spec, lay.arrange())
    for start, first_notes, lay.arrow_chords in starts:
        lay.xy, notes, lay.notes = copy.deepcopy(start), list(first_notes), list(lay_notes)
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
                        found[pid] = _best_move(lay, pid, gap, roots, original)
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

