# Checking and fixing how a chemical figure is displayed

These checks cover overlaps, hidden atom symbols, and the [rules](rules.md). They do not evaluate whether the molecular graph or reaction mechanism is scientifically correct.

## Detectors

`check_rules.py` has 58 detectors:

| Checks | Detectors |
|---|---|
| Overlaps and spacing | `overlap_text_text`, `overlap_text_bond`, `overlap_arrow_text`, `overlap_arrow_bond`, `overlap_arrow_arrow`, `overlap_charge`, `bond_crossing`, `species_gap`, `plus_place` |
| Structure, labels and saved files | `graph`, `hydrogens`, `labels_drawn`, `carbon_labels`, `hydrogen_display`, `isotopes`, `cdxml`, `frame` |
| Stereochemistry | `stereochemistry`, `stereo_display` |
| Curved arrows, lone pairs and radicals | `arrows_count`, `arrows_ends`, `arrows_lone_pair`, `arrows_head`, `arrows_color`, `arrows_shape`, `arrows_clear`, `arrows_steps`, `electron_flow`, `lone_pair_geometry`, `radical_marks`, `radical_direction` |
| Charges | `charges`, `charge_circled`, `charge_own_atom`, `charge_size`, `charge_open`, `ion_text` |
| Atom numbers | `number_size`, `number_place`, `number_clear`, `number_on_h` |
| Bonds and angles | `chain_angles`, `four_bond`, `ring_substituents`, `label_bonds`, `one_label_bonds`, `bond_lengths`, `partial_dashes`, `delocalized_bonds`, `hbond_dots` |
| Figure | `column_arrows`, `reaction_kinds`, `ts_brackets`, `colors`, `residue_codes`, `figure_style`, `coordination`, `metal_geometry` |

Each detector runs only when its target feature is present in the figure (e.g., `charge_size` runs only when charges are present); the summary lists applicable and non-applicable detectors. With `--positive-control`, which the regression test always uses, every active detector must also catch a planted error.

Departures from preferred angles, bond lengths, radical directions, and electron balance, a `+` not placed between two species (`plus_place`), and a curved arrow within 0.035 bond lengths (0.5 pt) of ink that is not its own start or end (`arrows_clear`) produce warnings. An unpaired electron that comes from the bracket H count rather than from `radicals` (`radical_marks`), mixed residue codes, charge-circle styles, or partial-line styles cause failures. Folded hydrogens, coordination H counts, and TS H counts are preserved in the saved files and verified when read back.

To re-check a drawn directory without a browser: `python check_rules.py DIR [--positive-control] [--json OUT.json]`. It reads `DIR/.state.json` and `DIR/figure.svg`.

## Stereochemistry after layout changes

Once all positioning, hand coordinates, reflections, and bond lengthening are applied, `stereochemistry` compares the R/S and E/Z configurations from the SMILES against the saved coordinates and wedges, both after reading back the saved Octet data and when reading the CDXML alone. Note that keeping the same wedge on a mirrored layout inverts the configuration. Atom maps are used only to match atoms and are cleared before `rdCIPLabeler` assigns priorities. `expected_stereo` and `STEREO` lines expand known abbreviations (such as Ph, Me, Ac, and OPP); a label that depends on a stub of unknown structure (e.g., a residue or Ado) raises an input error until that group is written in full. Missing or contradictory stereochemistry fails the check, whereas unspecified double-bond geometry is unconstrained.

## Hydrogen-bond lines

For an `hbonds` overlap, the `FIX` line points to `python scripts/fix_hbonds.py INPUT --out NEW_DIR`. The script tests both the H and heavy-atom endpoints on the same donor, along with small turns, flips, or offsets of the smaller residue or water fragment, while preserving the substrate and existing hand-coordinate limits. At most three repair candidates are drawn; to pass, a candidate must reduce hydrogen-bond findings, worsen no other checks, and pass all positive controls. The original input remains unchanged: an improved `input.hbonds.json` is saved alongside it (numbered if the file already exists), and the changes are recorded under `hbond_repair` in `NEW_DIR/.state.json`.

## Showing the C of CO₂

`carbon_labels` fails when a required carbon symbol is omitted: on an isolated carbon, a linear carbon between two double bonds (such as the C in O=C=O), or a carbon in a two-carbon fragment. `labels_drawn` fails when a label is not drawn or when a visible carbon becomes hidden in the saved file.

## Color

- Check: After saving and reloading, `colors` verifies that every atom, bond, and residue name matches its requested input color (none = black) and that the SVG uses the requested colors and no others. Arrows are always `#000000`, and bonds between differently colored parts are black.
- Contrast: Below a 4.5:1 contrast ratio against white, 10 pt text is hard to read; the scripts record a `note:` in `.state.json` → `summary` below 4.5:1 for text labels and below 3:1 for lines. Among the Okabe–Ito colors, only blue reaches 4.5:1 (vermilion: 3.87, bluish green: 3.42, reddish purple: 3.06, orange: 2.25, sky blue: 2.31); `#C35600`, `#008762`, and `#A46186` preserve the original hues at 4.5:1.
