# Octet Draw backend

For maintainers. The backend lives in `skills/reaction-draw/scripts/octet/`. `octet_draw.py` renders a **resolved figure** (`resolved/1`, produced by `specutil.resolve()`) using Octet Draw into `figure.cdxml`, `figure.svg`, `figure.png`, and `figure_small.png`, with internal data in `.state.json`. Every step that depends on actual glyph metrics happens here (bond lengthening, charges, arrows, atom numbers, boxes, and titles). `draw_figure.py` invokes it, and `check_rules.py` validates the output ([checks](checks.md)).

`dist/octet-adapter.html` bundles `adapter.ts` with the Octet Draw source at commit `934719abf213e0a8c9725256338bcbc0f305cab6` (version 1.1.1; [SamChemMan/Octet-Draw](https://github.com/SamChemMan/Octet-Draw), Apache-2.0); see `skills/reaction-draw/third_party/octet-draw/NOTICE` for bundled files and local changes. Node.js is required only when [rebuilding the page](#rebuilding-the-page).

The page renders text in Arial or Chrome's fallback (Liberation Sans or Arimo if installed, or another system font), while the exported SVG specifies Arial. `font_check` compares rendered letter and digit widths against Arial's advance widths (Octet's `arial.ts`); a deviation over 1% fails `frame`.

## Script files

In `skills/reaction-draw/scripts/`:

| File | Role |
|---|---|
| `draw_figure.py` | End-to-end pipeline: input → layout → Octet Draw → save, reload, export → checks → previews, `.state.json` |
| `specutil.py` | Input parsing, color definitions, and `resolve()` (produces the resolved figure) |
| `spec_validation.py` | Input validation; compares expected stereochemistry against map-free SMILES labels before layout |
| `layout.py` | Automatic 2D layout of an input file (uses RDKit for stereochemistry and rings) |
| `layout_repair.py` | Bounded layout alternatives, attempted only after a failed check |
| `settle.py` | Final molecule placement in each panel (chosen by a single score) and alternative panel layouts for the first retry |
| `check_rules.py` | Runs applicable checks from 58 declared detectors |
| `svg_geometry.py` | SVG path parsing, glyph bounding boxes, and distance calculations for `check_rules.py` |
| `stereochemistry.py` | CIP stereochemistry comparison between SMILES and saved coordinates/wedges |
| `octet/` | Octet Draw backend scripts and prebuilt adapter page |

## draw_figure.py

Common options and exit codes: [scripts/README.md](../skills/reaction-draw/scripts/README.md#draw_figurepy).

```bash
python draw_figure.py INPUT.json --out DIR [--dpi 600 | --png-width N] [--browser-cmd "ssh HOST /path/to/python"]
                      [--quiet] [--octet CHECKOUT --node-modules DIR]
python draw_figure.py A.json B.json C.json --out DIR --versions   # a layout revision, or on request: versions drawn at once, each in DIR/<file name>
```

| Stage | Writes into `DIR` |
|---|---|
| resolve | `.state.json` → `resolved`: fully positioned layout ([`resolved/1`](#the-resolved-figure-resolved1)) |
| draw | `figure.cdxml` (for ChemDraw), `figure.svg`, `figure.png` (600 dpi at true size, on white), `figure_small.png` (150 dpi); `.state.json` → `report` (drawing measurements), `reload` (reloaded Octet and CDXML data) |
| check | `.state.json` → `checks`, `summary` (a list of lines): check results, backend problems, layout notes, stage timings, true dimensions, and whether the figure fits a single (3.25 in) or double (7 in) ACS column |

Each run overwrites only managed output files in `DIR`. When a check fails, the pipeline first tries bounded adjustments ([automatic retries](#automatic-retries)). Given the same browser and fonts on a single machine, identical inputs produce byte-identical files.

| Option | Meaning |
|---|---|
| `--png-width` | Set `figure.png` width in px instead of using `--dpi` |
| `--browser-cmd` | Run the browser step on another host that shares the same filesystem paths (Python with Playwright); pass to both `--check-env` and the full run |
| `--octet`, `--node-modules` | Build the page from an Octet Draw checkout instead of using the prebuilt bundle ([rebuilding](#rebuilding-the-page)) |

The environment variables `OCTET_DRAW_BROWSER_CMD`, `OCTET_DRAW_CHECKOUT`, `OCTET_DRAW_NODE_MODULES`, and `OCTET_DRAW_CACHE` set the corresponding defaults. An optional `style` block in the input overrides the default ACS dimensions: `font`, `font_size_pt` (10), `bond_length_pt` (14.4), `line_width_pt` (0.6), `bold_width_pt` (2.0), `double_spacing` (0.18), `label_margin_pt` (1.6), `hash_spacing_pt` (2.5), `number_size_pt` (8), `number_font`, and `title_size_pt` (10).

## Run

```bash
python octet_draw.py resolved.json --out out
```

```python
import octet_draw
report = octet_draw.draw(resolved_dict_or_path, 'out', dpi=600)
```

`--out`, `--dpi`, `--png-width`, `--browser-cmd`, `--octet`, and `--node-modules` work the same as in `draw_figure.py` (`out_dir`, `dpi`, `png_width`, `browser_cmd`, `octet_dir`, and `node_modules` in `draw()`). Additional options:

| Option (CLI / `draw()`) | Meaning |
|---|---|
| `--small-dpi N` / `small_dpi` | Resolution for `figure_small.png` (default 150; 0 disables generation) |
| `--browser` / `browser` | `auto` (system Chrome, falling back to Playwright's Chromium), `chrome`, `chromium`, or an executable path |
| `--cache DIR` / `cache_dir` | With `--octet`: build cache directory (defaults to `OCTET_DRAW_CACHE`; if unset, `$XDG_CACHE_HOME/reaction-draw` or `~/.cache/reaction-draw`) |
| `--node PATH` / `node` | With `--octet`: path to the `node` executable |
| `options` (`draw()` only) | Retry parameters passed by `draw_figure.py`: `repairCharges`, `repairArrows` (with `repairArrowIds`, `reverseCurved`, and `hookSeats`, which keeps a reacting lone pair off the side where a local arrow's hook comes onto the same atom), and `repairNumbers` (with `ownMarginPx`) widen the candidate search; `fixedCurved` and `fixedNumbers` lock saved arrow routes and number positions |

Exit codes: `0` when rendering succeeds (any layout/drawing failures are printed and recorded under `report.problems` in `.state.json`); `2` on invalid input or a failed browser step.

## Layout

`layout.py` follows the layout [rules](rules.md), normalizing all bond lengths to `1.0`:

1. The longest chain runs left to right, chains of *sp* centers (O=C=O, allenes, ketenes, nitriles, alkynes) are drawn straight, and *Z* double bonds are drawn *cis*. Ring molecules use RDKit's 2D layout, rotated so the longest chain runs to the right and then turned by up to 15° so that the bonds lie on the 30° lattice of standard drawings (rings and chains level or upright). A stereocenter whose other bonds cannot take a wedge or hash (ring bonds, multiple bonds, a transition state's partial or 1.5 bonds, and bonds to another stereocenter) shows its H as a bond, and the stereobond goes on it.
2. In subsequent panels, only reacting atoms and their terminal groups are repositioned; rearranged molecules are laid out anew and aligned over their unchanged atoms. So is a molecule with a hydrogen bond inside it (a residue's backbone N–H to the oxyanion it holds): its H faces the acceptor across the ring they close, a four-bond atom of that ring keeps its two ring bonds side by side (its other two bonds point out of the ring), and the branches between them turn until the dotted line has its span and crosses no bond.
3. The first species in a panel is the anchor, and any species sharing an `id` with another panel copies its position. A departed leaving group starts at its former atomic coordinates and shifts outward along the broken bond until clear. A species linked to an already-placed species by a curved arrow, forming bond, or hydrogen bond is oriented to face it; all other species follow `near` or are placed to the right.
4. Finally, in each panel with two or more molecules, `settle.py` adjusts molecule positions to minimize a single score using translations, seats fitted to hydrogen bonds, and flips of hydrogen-bonded terminal groups around their single bond (or around a double bond to an end whose two other atoms are alike, as in =NH2+, where the flip only swaps them). When the search stops while a dotted line is still predicted to cross or touch, each group holding an end of that line is flipped and the molecules hydrogen-bonded to the group are re-seated one after another (or, when no flip helps, each molecule holding an end of that line is put on each of its seats), and the search runs again from there; this is kept only when the score drops (at most twice per panel). The score accounts for dotted-line crossings, contacts, and spans, clearance between molecules, distance between reacting atoms, and label collisions. Initial positions are only a starting point, and moves are not penalized for distance.
5. Panels are arranged in rows with reaction arrows between them and titles aligned at a uniform height beneath each row.

## What happens

1. Octet document (`octet_draw.py`): Flips the *y*-axis (Octet uses *y*-up), preserves every atom's `ref` in the file, and converts labels, abbreviations, residue names, bond styles, lone pairs, texts, and atom numbers into Octet objects.
2. Page: Loads `dist/octet-adapter.html`, or, with `--octet`, a build cached under a hash of its sources.
3. In the page (`adapter.ts`; `octet_run.py` launches one headless browser per call): Lengthens only bonds whose visible stroke would otherwise be hidden by labels (residue names and texts placed `near` an atom move with it); keeps a label's written H on the side the same atom takes in the other panels, unless that puts them on a caption; places each circled charge into the first open slot of eight around its atom; expands boxes; routes curved arrows across a distance field of existing ink and text, turning a local arrow's concave side toward its own atom or bond even when it must cross another bond of that atom; and places atom numbers beside their atoms and titles under their panels. It then saves (`writeOct`) and reloads (`readOct`) the document before rendering, exporting, and measuring the reloaded state. `figure.cdxml` is written by Octet's CDXML writer (`toCdxml`) with enough pages for the drawing, and `chemdraw_compat.py` then adapts it to how ChemDraw reads it: lone pair and radical dots and circled charges where the figure draws them, hydrogen bonds as rows of dots, hidden hydrogens folded into their labels, transition-state carbons without H counts with the dashes of partial bonds on the side the figure draws them, coordination bonds as dative bonds, and room for one-line captions. An annotation records each change; the fresh page undoes them, except the moved marks, and reads the file back using only Octet's CDXML reader.

## Automatic retries

`draw_figure.py` retries after a failed check (step 0 also after an `arrows_clear` warning between two arrows), logging each attempt in `.state.json` → `summary`:

0. Right after the first drawing (and again after the retries below), a local curved arrow (a bond's electrons moving onto one of its own atoms, or an atom's pair moving into one of its own bonds) that comes close to another arrow (an `arrows_clear` warning between two arrows, two arrows touching, or a loop) is redrawn as a short hook, together with the local arrows that share an atom with it. Every local arrow bulges away from its own bond, so the concave side of the arc faces the bond and the atom. The hooks are drawn at the standard bond length and with their bonds (outside rings) grown to 1.05, 1.10, 1.15 and 1.20 standard lengths, side by side; the shortest bond whose hooks stay short (at most 0.85 bond lengths), point at their atoms and clear the other arrows is kept, else the standard length. The short hooks are kept, also in the later retries, only if no check gains a finding and the findings or the `arrows_clear` warnings fall.

1. When two molecules are drawn closer than half a bond (`species_gap`), the `settle.py` layout search first runs again with the white space asked between that pair raised by the shortfall the drawing showed plus 0.1, 0.35, or 0.6 bond lengths; the three layouts are drawn side by side, and this runs at most twice. When letters or a charge circle stand too close, a layout in which a labeled chain atom (an imine NH) beside a branched group on a ring atom turns 30° away from it is also drawn if that moves any atom. When an overlap, crossing, arrow, charge, or lone-pair check (a lone pair touching bond ink) fails, the panels cited in its findings are redrawn using the next distinct candidates from the `settle.py` layout search. To generate candidates, each molecule linked to another by a curved arrow or forming bond is rotated around its partner's end in 60° steps (and mirrored if it has no stereocenter) before re-settling; in panels without such a pair, hydrogen-bonded molecules instead start from their alternative seats. The seven candidates drawn first are regrouped as in step 4 of Layout while a dotted line still crosses or touches. Three candidates are drawn side by side, followed by four more if none passes. Candidates are ranked by fewest failed checks, then fewest findings, and finally search order. The best candidate is kept, and the later steps start from it, if it adds no stereochemistry findings and has fewer failed checks than the previous drawing (or as many failed checks with fewer findings).
2. The backend searches additional charge, arrow, and number candidates for the failed checks, keeping a change only if no detector's violation count increases. Charges are searched more widely only when a charge check failed. For a failed number on an atom whose label writes H (CH2), the H on each other side of its symbol is drawn beside the plain retry; so are drawings in which the bonds of the numbered atom grow to 1.1 and 1.2 standard lengths (the atom stays, its neighbours' sides move out, and the arrows and numbers of its panel are placed again). The one with the fewest findings is kept, and later retries keep its sides and grown bonds.
3. After a layout, overlap, or arrow-endpoint failure, `layout_repair.py` evaluates up to 3 bounded alternatives (keeping rings rigid, moving branches as intact units, and shifting manual coordinates by at most 0.3 bond lengths). When a curved arrow touches a bond or a hydrogen-bond line crosses a bond, it evaluates up to 6: first counting the bonds near the line between the ends of that arrow (of every curved arrow in the panel, for a crossed hydrogen bond), then without these lines. When such an arrow ends at an H, it first turns that H about its atom into the open gap facing the arrow's other end and moves the other molecule next to it, 35 degrees off the line of the bond and clear of the rest; this layout is drawn as it is, then searched further (up to 3 more alternatives). In a panel with a crossed hydrogen bond, both passes count each hydrogen-bond line against bonds, labels and the other hydrogen-bond lines, and also turn or mirror a partner species in place. It keeps an alternative only if it fails fewer checks overall, has no stereochemistry finding, and adds no findings to basic checks (`graph`, `charges`, `cdxml`, etc.). When step 1 kept a candidate that still fails, the alternatives of the search's best layout are evaluated after these and judged the same way.
4. A blocked intramolecular forming-bond arrow receives a rigid rotation around a bridge along its path, preserving bond lengths and stereochemistry without adding new findings.
5. An arrow from a lone pair with no clear route triggers small rotations or shifts of its parent molecule, kept only if no detector gains a finding (up to four candidates are drawn at once, at most 30 drawings per run).
6. Last, also when every check passes: where the letters of two atoms of one molecule that are not bonded (nor bonded to one atom), an atom's letters and a bond of the molecule that ends neither at it nor at an atom bonded to it, or a charge circle and another atom's letters or bonds, stand closer than 5 pt, or the numbers, lone pairs, or radical dots of two atoms (bonded atoms included) stand closer than 3 pt to each other or to a bond of the molecule, open-chain bonds on the way between them grow to 1.1, 1.2, 1.3, or 1.4 standard lengths, the least growth among the best predictions. Before drawing, the script predicts the room each growth gives (one or two bonds, either side moving) by moving the drawn ink as the backend moves the atoms, and draws the four best predictions at once, no bond grown in more than two of them; a pair closer than 5 pt counts its shortfall five times, and a growth that brings any pair closer is not drawn. The arrows and numbers of those panels are placed again, and a drawing is kept only if no check gains a finding, no two molecules or arrows come closer, no pair comes closer, and the summed shortfall falls by at least 0.5 pt (or to zero when the sum is less than 0.5 pt), or by enough to part the least crowded pair closer than 5 pt. When a batch gives no more room, the next growths are drawn, up to 10 drawings (16 when a check fails) apart from the 30 candidates of the stages above. When the only failed checks are such letter or charge-circle gaps, this step also runs before step 1. When an atom of a crowded pair shows an H in its label, that H may be drawn on another side (left or right, and above or below for an atom of two or more bonds): one such drawing joins each batch as long as the tightest pair stands closer than 5 pt, and the other sides are drawn when no growth is left.
7. When a check still fails because an arrow end cannot sit near the middle of its bond, a short hook curls past its atom, or a number stands nearer another atom than its own, open-chain bonds there grow to 1.25 and 1.4 standard lengths (at most 8 drawings at once): the arrow end's bond together with the arrow's other bond when both meet at one atom, then that bond alone; the hook's bond; or each bond of the numbered atom. The drawing with the fewest findings is kept if it has fewer findings than before, no check gains one, and no two molecules come closer.

Placement is checked again after these moves. All stages together, apart from the layouts of step 1, draw at most 30 candidates per run; after that, only a candidate already chosen is drawn again, and the best drawing so far is kept. `checks.retry_counts` records the drawings of each stage and, for the last stage, the position of each accepted candidate.

## The resolved figure (`resolved/1`)

Produced by `specutil.resolve()` (in bond-length units where `1.0` = `style.bond_length_pt`; *x* right, *y* down, in a single coordinate frame for the figure):

```text
{"format": "resolved/1", "name", "source", "spec_format": 2,
 "style": {font, font_size_pt, bond_length_pt, line_width_pt, bold_width_pt, double_spacing, label_margin_pt,
           hash_spacing_pt, number_size_pt, number_font, title_size_pt},
 "panels": [{"id", "title": {"text", "x", "y"} | null,
             "atoms": [{"ref": "<panel>.<species>:<map>", "el", "label", "x", "y", "charge", "circled", "ion_text",
                        "number", "color", "kind", "h", "species"}],
             "bonds": [{"a", "b", "order": 1 | 2 | 3 | 1.5, "style": "plain" | "wedge" | "hash" | "bold" | "partial" | "1.5", "color", "move"}],
             "curved": [{"id", "from": {"kind": "atom" | "bond", "refs": [...]}, "to": {...}, "electrons": 1 | 2}],
             "hbonds": [[ref, ref]], "lone_pairs": [ref], "boxes": [{"x1", "y1", "x2", "y2", "kind"}],
             "texts": [{"text", "x", "y", "size", "align"}], "angle_exceptions": {ref: reason}}],
 "reaction_arrows": [{"kind": "single" | "equilibrium", "x1", "y1", "x2", "y2", "above", "below", "weight"?: "bold", "ratio"?: number}],
 "colors": the input's colors block or null, "notes": [...]}
```

- Atoms: `el` is the element Octet draws (for abbreviations, the attachment atom, e.g., `O` for `OPP`; for residue roots, `C` or `*`/`R`); `label` is the displayed text (`null` for skeletal carbon); `kind` is `atom`, `abbrev`, or `residue`; `h` is the SMILES implicit H count; `circled` indicates a circled unit charge; `ion_text` formats an ion or 2+ charge as text; `number` is the atom number string or `null`; optional `charge_angle` specifies the circle position in degrees counterclockwise.
- Bonds: `a` is the narrow end of a wedge or hash; `move` is the endpoint (along with its attached side) that shifts when lengthened (the side facing away from neighboring molecules). Styles also include `partial` (dashed single bond, TS only) and `1.5` (bond type 2 with a dashed second line forced to the inner/ring side, preferring the ring with more `1.5` or `partial` bonds when equally small rings tie); their resolved `order` retains the SMILES single/double bond order. A double bond with an end in a hydrogen bond takes its second line's side from the covalent bonds only (Octet would count the dotted line), so a C=O whose O takes hydrogen bonds stays centred.
- Transition states (TS): TS atoms carry `ts: true`; their automatic `brackets` box carries `ts: <species-id>` with a `‡` caption at its upper right. TS H counts are saved in the native `figure-ts-h` annotation and as CDXML `NumHydrogens`.
- Boxes, texts, and arrows: Box `kind` is `box` (default), `rounded`, `oval`, or `brackets`, with optional `color` and `dashed` flags. For texts and titles, `(x, y)` is the vertical midpoint of capital letters, and `x` is the left, center, or right anchor per `align` (titles: center), with optional text `color`. A title with `null` `x`/`y` is placed beneath its panel. Reaction arrows may include an `id`.

## Files

- `figure.svg` roles: `data-role="bond"` (structure ink, including `data-kind="hbond"` and `data-kind="box"`), `data-role="mark"` (circled charges, lone pairs), and `data-role="arrow" data-id="<id>"`; captions are `<text>` elements.
- `.state.json` → `report` (in pt, SVG frame, *x* right, *y* down; `check_rules.py` measures the gaps itself):
  - `atoms`: `{ref: x_pt, y_pt, label_box_pt, moved_pt}`
  - `bonds`: `[a, b, order (0 with hbond: true), length_pt, visible_line_pt, labels, lengthened, length_before_pt, target_pt, stretched_for, stretch_ratio]` (`stretched_for` names the local arrow whose bond was grown)
  - `curved`: `[id, kind, electrons, color, start_head, end_head, start_pt, end_pt, start_ref, end_ref ({kind, refs, distance_pt}: resolved target at each end), bend, length_pt, requested]`
  - `numbers`: `[ref, text, box_pt (glyph ink), size_pt, placed, own_gap_pt, nearest_other_gap_pt, direction_deg]`
  - `charges`: `[ref, charge, kind (circle | text), center_pt, outer_d_pt, default_outer_d_pt, sign_bars, angle_deg]`
  - `texts`: `[text, role (title | text | condition | residue), caption, box_pt, size_pt]` (atom numbers appear only under `numbers`); `label_pieces`: `[ref, text, box_pt (per-glyph ink of C, H, 3 in CH3), size_pt]`; `straight_arrows`
  - Pipeline steps: `lengthening`, `charge_placement`, `boxes_grown`, `routes`, `number_placement`, `titles`
  - `font_check`: `{font, family, checked, measured_px, arial_px, ratio}` (measures a 100 px test line against Arial's advance widths)
  - Diagnostics and metadata: `problems`, `notes`, `reload {lost, cdxml_file_lost}`, `page_errors`, `browser`, `adapter_page`, `files`, `timings_s`
- `.state.json` → `reload` (the saved Octet data read back in memory, in the resolved frame): `atoms [ref, el, text, shown, chemical, charge, circled, h, pairs, color, align, charge_angle, x, y]`, `bonds [a, b, order, style, color]`, `hbonds`, `curved [id, kind (curved | fishhook), from, to, bend, color, start, end]` (arrowheads), `straight`, `captions` (with `role`), `boxes`, `numbers`, `annotations`, `style`, `lost` (properties Octet could not preserve), and `cdxml_only` (the equivalent output when reading `figure.cdxml` standalone, as ChemDraw users see it).

## Rebuilding the page

Required only after modifying `adapter.ts` or updating the Octet Draw commit. Needs Node.js 20 or newer, esbuild (`npm install`; tested with 0.28.2), and an Octet Draw checkout. In `skills/reaction-draw/scripts/octet/`:

```bash
git clone https://github.com/SamChemMan/Octet-Draw && git -C Octet-Draw checkout 934719abf213e0a8c9725256338bcbc0f305cab6
node build.mjs --octet Octet-Draw --out dist/octet-adapter.html --record dist/octet-adapter.json
```

`build.mjs` warns if the checkout is on a different commit. `dist/octet-adapter.json` records the commit hash, esbuild version, SHA-256 digests of the built page and `adapter.ts`, and byte sizes per source file; `octet_draw.py` warns when `adapter.ts` no longer matches the prebuilt page. Update `skills/reaction-draw/third_party/octet-draw/NOTICE` whenever bundled files change. To test changes without rebuilding `dist/`, pass `--octet Octet-Draw` (which builds into the cache).

## Regression test

`python tests/run_regression.py` draws every template and every input in `tests/specs/`, and fails unless each drawing passes all checks. `python tests/test_lock.py` checks that a second drawing into the same output directory is refused while one is running. GitHub Actions runs both on Linux and on Windows (in a venv) on every push to `main` and every pull request (`.github/workflows/regression.yml`).

## Limits

- At a crowded atom (e.g., three or four bonds, an arrow, and a nearby label), an atom number may have no valid position that satisfies all rules (`backend problem: number ... breaks a rule`); `number_place` then warns, or fails if the number ends up closer to another atom.
- A curved arrow between two bonds sharing an atom is short when the bonds themselves are short.
