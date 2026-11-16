# Octet Draw backend

For maintainers. The backend lives in `skills/reaction-draw/scripts/octet/`. `octet_draw.py` renders a **resolved figure** (`resolved/1`, produced by `specutil.resolve()`) using Octet Draw into `figure.oct`, `figure.cdxml`, `figure.svg`, `figure.png`, `figure_small.png`, `report.json`, and `reload.json`. Every step that depends on actual glyph metrics happens here (bond lengthening, charges, arrows, atom numbers, boxes, and titles). `draw_figure.py` invokes it, and `check_rules.py` validates the output ([checks](checks.md)).

`dist/octet-adapter.html` bundles `adapter.ts` with the Octet Draw source at commit `934719abf213e0a8c9725256338bcbc0f305cab6` (version 1.1.1; [SamChemMan/Octet-Draw](https://github.com/SamChemMan/Octet-Draw), Apache-2.0); see `skills/reaction-draw/third_party/octet-draw/NOTICE` for bundled files and local changes. Node.js is required only when [rebuilding the page](#rebuilding-the-page).

The page renders text in Arial or Chrome's fallback (Liberation Sans or Arimo if installed, or another system font), while the exported SVG specifies Arial. `font_check` compares rendered letter and digit widths against Arial's advance widths (Octet's `arial.ts`); a deviation over 1% fails `frame`.

## Script files

In `skills/reaction-draw/scripts/`:

| File | Role |
|---|---|
| `draw_figure.py` | End-to-end pipeline: input → layout → Octet Draw → save, reload, export → checks → previews, `summary.txt` |
| `specutil.py` | Input parsing, color definitions, and `resolve()` (produces the resolved figure) |
| `spec_validation.py` | Input validation; compares expected stereochemistry against map-free SMILES labels before layout |
| `layout.py` | Automatic 2D layout of an input file (uses RDKit for stereochemistry and rings) |
| `layout_repair.py` | Bounded layout alternatives, attempted only after a failed check |
| `check_rules.py` | Runs applicable checks from 58 declared detectors (supports `--positive-control`) |
| `svg_geometry.py` | SVG path parsing, glyph bounding boxes, and distance calculations for `check_rules.py` |
| `stereochemistry.py` | CIP stereochemistry comparison between SMILES and saved coordinates/wedges |
| `octet/` | Octet Draw backend scripts and prebuilt adapter page |

## draw_figure.py

Common options and exit codes: [scripts/README.md](../skills/reaction-draw/scripts/README.md#draw_figurepy).

```bash
python draw_figure.py INPUT.json --out DIR [--dpi 600 | --png-width N] [--browser-cmd "ssh HOST /path/to/python"]
                      [--positive-control] [--quiet] [--octet CHECKOUT --node-modules DIR]
```

| Stage | Writes into `DIR` |
|---|---|
| resolve | `resolved.json`: fully positioned layout ([`resolved/1`](#the-resolved-figure-resolved1)) |
| draw | `figure.oct` (CDXML plus Octet's JSON), `figure.cdxml` (for ChemDraw), `figure.svg`, `figure.png` (600 dpi at true size, on white), `figure_small.png` (150 dpi), `report.json` (drawing measurements), `reload.json` (reloaded `.oct` and `.cdxml` data) |
| check | `checks.json`, `summary.txt`: check results, positive control, backend problems, layout notes, stage timings, true dimensions, and whether the figure fits a single (3.25 in) or double (7 in) ACS column |

Each run overwrites only managed output files in `DIR`. When a check fails, the pipeline first tries bounded adjustments ([automatic retries](#automatic-retries)). Given the same browser and fonts on a single machine, identical inputs produce byte-identical files.

| Option | Meaning |
|---|---|
| `--png-width` | Set `figure.png` width in px instead of using `--dpi` |
| `--browser-cmd` | Run the browser step on another host that shares the same filesystem paths (Python with Playwright); pass to both `--check-env` and the full run |
| `--positive-control` | Also run the checkers' self-test (the regression test does) |
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
| `options` (`draw()` only) | Retry parameters passed by `draw_figure.py`: `repairCharges`, `repairArrows` (with `repairArrowIds`, `reverseCurved`), and `repairNumbers` (with `ownMarginPx`) widen the candidate search; `fixedCurved` and `fixedNumbers` lock saved arrow routes and number positions |

Exit codes: `0` when rendering succeeds (any layout/drawing failures are printed and recorded under `problems` in `report.json`); `2` on invalid input or a failed browser step.

## Layout

`layout.py` follows the layout [rules](rules.md), normalizing all bond lengths to `1.0`:

1. The longest chain runs left to right, chains of *sp* centers (O=C=O, allenes, ketenes, nitriles, alkynes) are drawn straight, and *Z* double bonds are drawn *cis*. Ring molecules use RDKit's 2D layout, rotated so the longest chain is horizontal.
2. In subsequent panels, only reacting atoms and their terminal groups are repositioned; rearranged molecules are laid out anew and aligned over their unchanged atoms.
3. The first species in a panel is the anchor, and any species sharing an `id` with another panel copies its position. A departed leaving group starts at its former atomic coordinates and shifts outward along the broken bond until clear. A species linked to an already-placed species by a curved arrow, forming bond, or hydrogen bond is oriented to face it; all other species follow `near` or are placed to the right.
4. Panels are arranged in rows with reaction arrows between them and titles aligned at a uniform height beneath each row.

## What happens

1. Octet document (`octet_draw.py`): Flips the *y*-axis (Octet uses *y*-up), preserves every atom's `ref` in the file, and converts labels, abbreviations, residue names, bond styles, lone pairs, texts, and atom numbers into Octet objects.
2. Page: Loads `dist/octet-adapter.html`, or, with `--octet`, a build cached under a hash of its sources.
3. In the page (`adapter.ts`; `octet_run.py` launches one headless browser per call): Lengthens only bonds whose visible stroke would otherwise be hidden by labels; places each circled charge into the first open slot of eight around its atom; expands boxes; routes curved arrows across a distance field of existing ink and text; and places atom numbers beside their atoms and titles under their panels. It then saves (`writeOct`) and reloads (`readOct`) the document before rendering, exporting, and measuring the reloaded state. `figure.cdxml` is written by Octet's CDXML writer (`toCdxml`) and read back in a fresh page using only Octet's CDXML reader.

## Automatic retries

`draw_figure.py` retries only after a failed check, logging each attempt in `summary.txt`:

1. After a layout, overlap, or arrow-endpoint failure, `layout_repair.py` evaluates up to 3 bounded alternatives (keeping rings rigid, moving branches as intact units, and shifting manual coordinates by at most 0.3 bond lengths). It keeps an alternative only if it fails fewer checks overall, has no stereochemistry finding, and adds no findings to basic checks (`graph`, `charges`, `cdxml`, etc.).
2. The backend searches additional charge, arrow, and number candidates for the failed checks, keeping a change only if no detector's violation count increases.
3. A blocked intramolecular forming-bond arrow receives a rigid rotation around a bridge along its path, preserving bond lengths and stereochemistry without adding new findings.
4. An arrow from a lone pair with no clear route triggers small rotations or shifts of its parent molecule, kept only if no detector gains a finding.

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
- Bonds: `a` is the narrow end of a wedge or hash; `move` is the endpoint (along with its attached side) that shifts when lengthened (the side facing away from neighboring molecules). Styles also include `partial` (dashed single bond, TS only) and `1.5` (bond type 2 with a dashed second line forced to the inner/ring side, preferring the ring with more `1.5` or `partial` bonds when equally small rings tie); their resolved `order` retains the SMILES single/double bond order.
- Transition states (TS): TS atoms carry `ts: true`; their automatic `brackets` box carries `ts: <species-id>` with a `‡` caption at its upper right. TS H counts are saved in the native `figure-ts-h` annotation and as CDXML `NumHydrogens`.
- Boxes, texts, and arrows: Box `kind` is `box` (default), `rounded`, `oval`, or `brackets`, with optional `color` and `dashed` flags. For texts and titles, `(x, y)` is the vertical midpoint of capital letters, and `x` is the left, center, or right anchor per `align` (titles: center), with optional text `color`. A title with `null` `x`/`y` is placed beneath its panel. Reaction arrows may include an `id`.

## Files

- `figure.svg` roles: `data-role="bond"` (structure ink, including `data-kind="hbond"` and `data-kind="box"`), `data-role="mark"` (circled charges, lone pairs), and `data-role="arrow" data-id="<id>"`; captions are `<text>` elements.
- `report.json` (in pt, SVG frame, *x* right, *y* down; `check_rules.py` measures the gaps itself):
  - `atoms`: `{ref: x_pt, y_pt, label_box_pt, moved_pt}`
  - `bonds`: `[a, b, order (0 with hbond: true), length_pt, visible_line_pt, labels, lengthened, length_before_pt, target_pt]`
  - `curved`: `[id, kind, electrons, color, start_head, end_head, start_pt, end_pt, start_ref, end_ref ({kind, refs, distance_pt}: resolved target at each end), bend, length_pt, requested]`
  - `numbers`: `[ref, text, box_pt (glyph ink), size_pt, placed, own_gap_pt, nearest_other_gap_pt, direction_deg]`
  - `charges`: `[ref, charge, kind (circle | text), center_pt, outer_d_pt, default_outer_d_pt, sign_bars, angle_deg]`
  - `texts`: `[text, role (title | text | condition | residue), caption, box_pt, size_pt]` (atom numbers appear only under `numbers`); `label_pieces`: `[ref, text, box_pt (per-glyph ink of C, H, 3 in CH3), size_pt]`; `straight_arrows`
  - Pipeline steps: `lengthening`, `charge_placement`, `boxes_grown`, `routes`, `number_placement`, `titles`
  - `font_check`: `{font, family, checked, measured_px, arial_px, ratio}` (measures a 100 px test line against Arial's advance widths)
  - Diagnostics and metadata: `problems`, `notes`, `reload {lost, cdxml_file_lost}`, `page_errors`, `browser`, `adapter_page`, `files`, `timings_s`
- `reload.json` (the saved `.oct` read back, in the resolved frame): `atoms [ref, el, text, shown, chemical, charge, circled, h, pairs, color, align, charge_angle, x, y]`, `bonds [a, b, order, style, color]`, `hbonds`, `curved [id, kind (curved | fishhook), from, to, bend, color, start, end]` (arrowheads), `straight`, `captions` (with `role`), `boxes`, `numbers`, `annotations`, `style`, `lost` (properties Octet could not preserve), and `cdxml_only` (the equivalent output when reading `figure.cdxml` standalone, as ChemDraw users see it).
- `.oct` format: CDXML + `<!--Octet 1 <hash> <base64 of Octet's JSON>-->`; `figure.cdxml` is the same CDXML without that comment.

## Rebuilding the page

Required only after modifying `adapter.ts` or updating the Octet Draw commit. Needs Node.js 20 or newer, esbuild (`npm install`; tested with 0.28.2), and an Octet Draw checkout. In `skills/reaction-draw/scripts/octet/`:

```bash
git clone https://github.com/SamChemMan/Octet-Draw && git -C Octet-Draw checkout 934719abf213e0a8c9725256338bcbc0f305cab6
node build.mjs --octet Octet-Draw --out dist/octet-adapter.html --record dist/octet-adapter.json
```

`build.mjs` warns if the checkout is on a different commit. `dist/octet-adapter.json` records the commit hash, esbuild version, SHA-256 digests of the built page and `adapter.ts`, and byte sizes per source file; `octet_draw.py` warns when `adapter.ts` no longer matches the prebuilt page. Update `skills/reaction-draw/third_party/octet-draw/NOTICE` whenever bundled files change. To test changes without rebuilding `dist/`, pass `--octet Octet-Draw` (which builds into the cache).

## Regression test

`python tests/run_regression.py` draws every template and every input in `tests/specs/`, and fails unless each drawing passes all checks and its positive control. GitHub Actions runs it on every push to `main` and every pull request (`.github/workflows/regression.yml`).

## Limits

- At a crowded atom (e.g., three or four bonds, an arrow, and a nearby label), an atom number may have no valid position that satisfies all rules (`backend problem: number ... breaks a rule`); `number_place` then warns, or fails if the number ends up closer to another atom.
- A curved arrow between two bonds sharing an atom is short when the bonds themselves are short.
