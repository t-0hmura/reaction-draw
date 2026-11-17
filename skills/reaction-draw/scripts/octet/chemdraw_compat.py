"""ChemDraw's reading of Octet's CDXML, made to show the drawing (figure.cdxml), and the way back for Octet's reader.

adapt() rewrites what ChemDraw 26 draws differently from the figure (measured on all regression figures):
  - lone pairs, unpaired electrons and circled charges at the places the figure draws them (Octet writes a symbol at a
    recomputed angle and a second point 7.5 pt to its left; ChemDraw draws a lone pair's dots at those two points);
  - hydrogen bonds as rows of filled dots (ChemDraw reads a DottedHydrogen bond as a covalent single bond);
  - hidden hydrogens folded into their labeled parent (NumHydrogens and the label text), which ChemDraw otherwise drops;
  - a transition state's skeletal carbons without NumHydrogens (ChemDraw writes them out as CH2), and the dashed line of
    a 1.5 bond on the side the figure draws it;
  - coordination bonds as dative bonds from the ligand to the metal;
  - one-line captions with room enough not to wrap.
Every element changed, removed or added is recorded in an annotation; restore() puts the file back as Octet wrote it,
so the read-back check (Octet's CDXML reader) sees the same document.  No change touches what the figure means.
"""
import json
import math
import re
import xml.etree.ElementTree as ET
from collections import defaultdict

KEY = 'reaction-draw-chemdraw'
NUM = re.compile(r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[-+]?\d+)?', re.I)
CAPTION_ROOM = 0.12          # each side of a one-line caption's box, as a share of its width
TS_HASH = '1.10'             # pt between the dashes of a transition state's partial bond


def _parse(cdxml):
    """(the prolog before <CDXML, kept as written; the root element)."""
    at = cdxml.find('<CDXML')
    return cdxml[:at], ET.fromstring(cdxml[at:])


def _nums(s):
    return [float(v) for v in NUM.findall(s or '')]


def _fmt(vals):
    return ' '.join('%.2f' % v for v in vals)


def _fit(pairs):
    """x' = a x + b for one axis by least squares; pairs [(x, x')]."""
    n = len(pairs)
    mx, my = sum(p[0] for p in pairs) / n, sum(p[1] for p in pairs) / n
    sxx = sum((p[0] - mx) ** 2 for p in pairs)
    a = sum((p[0] - mx) * (p[1] - my) for p in pairs) / sxx if sxx > 1e-9 else 1.0
    return a, my - a * mx


def adapt(cdxml, svg, report, refs, extra=None):
    """The CDXML text ChemDraw shows as the figure.  report: the adapter's report (pt, the SVG's frame); refs: the atom
    refs by AtomNumber; extra: {'coord': [[k, k]], 'ts': [k]} (AtomNumbers of coordination bonds and of TS atoms)."""
    extra = extra or {}
    head, root = _parse(cdxml)
    page = root.find('page')
    if page is None:
        return cdxml
    parent = {c: p for p in root.iter() for c in p}
    by_id = {e.get('id'): e for e in root.iter() if e.get('id')}
    nodes = [n for n in root.iter('n') if n.get('AtomNumber') is not None]
    ref_of = {n.get('id'): refs[int(n.get('AtomNumber'))] for n in nodes if int(n.get('AtomNumber')) < len(refs)}
    node_of = {r: i for i, r in ref_of.items()}
    record = {'replaced': [], 'removed': [], 'added': []}
    replaced = set()
    next_id = max(int(i) for i in by_id if i.isdigit()) + 1

    def keep(e):                                   # remember an element before its first change
        if e.get('id') not in replaced:
            replaced.add(e.get('id'))
            record['replaced'].append([e.get('id'), ET.tostring(e, encoding='unicode')])

    def remove(e):
        p = parent[e]
        if not p.get('id') and p is not root:
            raise ValueError('no id on the parent of %s' % e.get('id'))
        record['removed'].append([p.get('id') or '', list(p).index(e), ET.tostring(e, encoding='unicode')])
        p.remove(e)

    def add(tag, **attrs):
        nonlocal next_id
        e = ET.SubElement(page, tag, id=str(next_id), **attrs)
        record['added'].append(str(next_id))
        next_id += 1
        return e

    # report pt -> page coordinates, from the atoms
    atoms = report.get('atoms') or {}
    pts = [(atoms[ref_of[n.get('id')]], _nums(n.get('p'))) for n in nodes
           if ref_of.get(n.get('id')) in atoms and len(_nums(n.get('p'))) >= 2]
    if len(pts) < 2:
        return cdxml
    ax, bx = _fit([(a['x_pt'], p[0]) for a, p in pts])
    ay, by = _fit([(a['y_pt'], p[1]) for a, p in pts])
    to_page = lambda q: (ax * q[0] + bx, ay * q[1] + by)
    scale = (abs(ax) + abs(ay)) / 2

    # lone pairs, unpaired electrons and circled charges where the figure draws them
    pairs = defaultdict(list)
    for lp in report.get('lone_pairs') or []:
        pairs[lp['ref']].append(lp)
    radicals = defaultdict(list)
    for r in report.get('radicals') or []:
        radicals[r['ref']].append(r)
    charges = {c['ref']: c for c in report.get('charges') or [] if c.get('kind') == 'circle'}
    page_of = {n.get('id'): _nums(n.get('p')) for n in nodes}
    used = set()
    for g in list(root.iter('graphic')):
        if g.get('GraphicType') != 'Symbol':
            continue
        box, kind = _nums(g.get('BoundingBox')), g.get('SymbolType')
        if len(box) != 4:
            continue
        rep = g.find('represent')
        if rep is not None:
            owner = rep.get('object')
        else:                                       # a lone pair names no atom: the nearest atom with a lone pair left
            left = [node_of[r] for r, rows in pairs.items() if r in node_of and any((r, 'p', k) not in used for k in range(len(rows)))]
            owner = min(left, key=lambda i: math.dist(page_of[i], box[:2]), default=None)
        ref = ref_of.get(owner)
        if ref is None:
            continue
        if kind in ('LonePair', 'LonePairBar') and rep is None:
            cands = [lp for k, lp in enumerate(pairs[ref]) if (ref, 'p', k) not in used]
            if not cands:
                continue
            k, lp = min(((pairs[ref].index(lp), lp) for lp in cands), key=lambda t: math.dist(to_page(t[1]['center_pt']), box[:2]))
            used.add((ref, 'p', k))
            keep(g)
            a, b = (to_page(d) for d in lp['dots_pt'])
            g.set('BoundingBox', _fmt(a + b))
        elif kind == 'Electron':
            cands = [r for k, r in enumerate(radicals[ref]) if (ref, 'r', k) not in used]
            if not cands:
                continue
            k, r = min(((radicals[ref].index(r), r) for r in cands), key=lambda t: math.dist(to_page(t[1]['center_pt']), box[:2]))
            used.add((ref, 'r', k))
            keep(g)
            u, v = to_page(r['center_pt'])
            g.set('BoundingBox', _fmt((u, v, box[2] + u - box[0], box[3] + v - box[1])))
        elif kind in ('CirclePlus', 'CircleMinus') and ref in charges:
            keep(g)
            u, v = to_page(charges[ref]['center_pt'])
            g.set('BoundingBox', _fmt((u, v, box[2] + u - box[0], box[3] + v - box[1])))

    # hydrogen bonds: rows of filled dots where the figure draws them; the bonds leave the covalent graph
    hb = [b for b in root.iter('b') if b.get('Display') == 'DottedHydrogen']
    protected = {b.get(k) for b in hb for k in ('B', 'E')}
    if hb:
        try:
            svg_root = ET.fromstring(svg)
        except ET.ParseError:
            svg_root = None
        dots = []
        if svg_root is not None:
            for g in svg_root.iter('{http://www.w3.org/2000/svg}g'):
                s = _nums(g.get('transform') or '')
                k = s[0] if (g.get('transform') or '').startswith('scale') and s else 1.0
                for path in g.iter('{http://www.w3.org/2000/svg}path'):
                    if path.get('data-kind') != 'hbond':
                        continue
                    for poly in path.get('d', '').split('Z'):
                        v = _nums(poly)
                        if len(v) < 4:
                            continue
                        xs, ys = v[::2], v[1::2]
                        dots.append(((max(xs) + min(xs)) * k / 2, (max(ys) + min(ys)) * k / 2, (max(xs) - min(xs)) * k / 2))
                break
        if dots:
            for b in hb:
                remove(b)
            for z, (x, y, r) in enumerate(dots):
                u, v = to_page((x, y))
                rr = r * scale
                add('graphic', Z=str(12000 + z), GraphicType='Oval', OvalType='Circle Filled', LineWidth='0',
                    BoundingBox=_fmt((u + rr, v, u, v)))

    # hidden hydrogens folded into their labeled parent
    folded = defaultdict(list)
    ordering = {i for q in root.iter('n') for i in (q.get('BondOrdering') or '').split()}
    for b in list(root.iter('b')):
        if b.get('Visible') != 'no' or b.get('Display') or b.get('Order') not in (None, '1') or b.get('id') in ordering:
            continue
        for h_id, p_id in ((b.get('B'), b.get('E')), (b.get('E'), b.get('B'))):
            h, p = by_id.get(h_id), by_id.get(p_id)
            if h is None or p is None or h.get('Element') != '1' or h.get('Visible') != 'no' or h_id in protected:
                continue
            if any(h.get(t) for t in ('Charge', 'Isotope', 'Radical')) or p.find('t') is None:
                continue
            folded[p].append((b, h))
            break
    for p, hs in folded.items():
        t = p.find('t')
        label = ''.join(t.itertext())
        m = re.fullmatch(r'(H\d?)?([A-Z][a-z]?)(H\d?)?([−+\-])?', label)
        if not m or m.group(2) == 'H':
            continue
        count = int(p.get('NumHydrogens', '0')) + len(hs)
        if not 1 <= count <= 4:
            continue
        hpart = 'H' + (str(count) if count > 1 else '')
        # the H on the side the figure writes it: left when the drawn label lies left of the atom
        drawn = atoms.get(ref_of.get(p.get('id')), {})
        box = drawn.get('label_box_pt')
        left = (box[0] + box[2]) / 2 < drawn['x_pt'] - 1.0 if box and 'x_pt' in drawn else bool(m.group(1)) or p.get('LabelDisplay') == 'Right'
        text = (hpart + m.group(2) if left else m.group(2) + hpart) + (m.group(4) or '')
        keep(p)
        p.set('NumHydrogens', str(count))
        runs = t.findall('s')
        font = dict(runs[0].attrib) if runs else {}
        for c in list(t):
            t.remove(c)
        ET.SubElement(t, 's', font).text = text
        for b, h in hs:
            remove(b)
            remove(h)

    # a transition state's skeletal carbons, and the dashed line of a 1.5 bond on the side the figure draws it (Octet
    # writes the side in its own y-up frame; the page's y is down, so ChemDraw reads it mirrored)
    ts = {str(k) for k in extra.get('ts') or []}
    for n in nodes:
        if n.get('AtomNumber') in ts and n.find('t') is None and n.get('NumHydrogens') is not None:
            keep(n)
            del n.attrib['NumHydrogens']
    for b in root.iter('b'):
        partial = b.get('Order') == '1.5' and b.get('Display2') == 'Dash'
        if not (partial or b.get('Display') == 'Dash'):
            continue
        keep(b)
        b.set('HashSpacing', TS_HASH)
        side = b.get('DoublePosition')
        if b.get('Order') == '1.5' and side in ('Left', 'Right'):
            b.set('DoublePosition', 'Right' if side == 'Left' else 'Left')

    # coordination bonds: dative, from the ligand to the metal
    for pair in extra.get('coord') or []:
        ids = [node_of.get(refs[k]) for k in pair if k < len(refs)]
        for b in root.iter('b'):
            if set((b.get('B'), b.get('E'))) != set(ids) or None in ids:
                continue
            metal = [i for i in ids if by_id[i].get('AtomNumber') == str(pair[1])][0]
            keep(b)
            b.set('B', ids[0] if ids[0] != metal else ids[1])
            b.set('E', metal)
            b.set('Order', 'dative')

    # one-line captions with room enough not to wrap
    in_node = {t for n in root.iter('n') for t in n.iter('t')}
    for t in root.iter('t'):
        if t in in_node or '\n' in ''.join(t.itertext()) or len(_nums(t.get('BoundingBox'))) != 4:
            continue
        x0, y0, x1, y1 = _nums(t.get('BoundingBox'))
        keep(t)
        w = x1 - x0
        t.set('BoundingBox', _fmt((x0 - CAPTION_ROOM * w, y0, x1 + CAPTION_ROOM * w, y1)))

    ET.SubElement(page, 'annotation', Keyword=KEY, Content=json.dumps(record, ensure_ascii=False, separators=(',', ':')))
    return head + ET.tostring(root, encoding='unicode')


def restore(cdxml):
    """The CDXML as Octet wrote it, from a file adapt() made (any other file comes back unchanged)."""
    if KEY not in cdxml:
        return cdxml
    head, root = _parse(cdxml)
    page = root.find('page')
    note = next((a for a in page.iter('annotation') if a.get('Keyword') == KEY), None) if page is not None else None
    if note is None:
        return cdxml
    record = json.loads(note.get('Content'))
    page.remove(note)
    parent = {c: p for p in root.iter() for c in p}
    added = set(record['added'])
    for e in [e for e in root.iter() if e.get('id') in added]:
        parent[e].remove(e)
    by_id = {e.get('id'): e for e in root.iter() if e.get('id')}
    for pid, index, xml in reversed(record['removed']):
        host = by_id.get(pid) if pid else root
        e = ET.fromstring(xml)
        host.insert(index, e)
        for x in e.iter():
            if x.get('id'):
                by_id[x.get('id')] = x
    parent = {c: p for p in root.iter() for c in p}
    for eid, xml in record['replaced']:
        e = by_id.get(eid)
        if e is None:
            continue
        p = parent[e]
        i = list(p).index(e)
        p.remove(e)
        p.insert(i, ET.fromstring(xml))
    return head + ET.tostring(root, encoding='unicode')
