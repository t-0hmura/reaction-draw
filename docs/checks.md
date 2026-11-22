# Checking and fixing how a chemical figure is displayed

These checks cover overlaps, hidden atom symbols, and the [rules](rules.md). They do not evaluate whether the molecular graph or reaction mechanism is scientifically correct, except that a residue name must stand at its residue's own side chain.

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
| Figure | `column_arrows`, `reaction_kinds`, `ts_brackets`, `colors`, `residue_codes`, `residue_once`, `figure_style`, `coordination`, `metal_geometry` |

A curved arrow's body may cross the line of a plain bond or a hydrogen bond (`overlap_arrow_bond` and `arrows_clear` do not count it) when it crosses that line once, through its middle half, at 30° or more, with no letter within 2 pt of the crossing; an arrow that runs along, grazes, or crosses near an atom or a letter, a wedge, hash or wavy bond, and any head on a line still count.

Each detector runs only when its target feature is present in the figure (e.g., `charge_size` runs only when charges are present); the summary lists applicable and non-applicable detectors.

Departures from preferred angles, bond lengths, radical directions, and electron balance, a `+` not placed between two species (`plus_place`), a curved arrow within 0.035 bond lengths (0.5 pt) of ink that is not its own start or end (`arrows_clear`), and a local curved arrow that crosses another bond of its own atom to keep its concave side toward that atom (`overlap_arrow_bond`) produce warnings. An unpaired electron that comes from the bracket H count rather than from `radicals`, or a radical dot nearer another atom or another piece of its own label (such as a shown H) than its own atom (`radical_marks`), mixed residue codes, charge-circle styles, or partial-line styles cause failures. Folded hydrogens, coordination H counts, and TS H counts are preserved in the saved files and verified when read back.

To re-check a drawn directory without a browser: `python check_rules.py DIR [--json OUT.json]`. It reads `DIR/.state.json` and `DIR/figure.svg`.

## Stereochemistry after layout changes

Once all positioning, hand coordinates, reflections, and bond lengthening are applied, `stereochemistry` compares the R/S and E/Z configurations from the SMILES against the saved coordinates and wedges, both after reading back the saved Octet data and when reading the CDXML alone. Keeping the same wedge on a mirrored layout inverts the configuration. Atom maps are used only to match atoms and are cleared before `rdCIPLabeler` assigns priorities. `expected_stereo` and `STEREO` lines expand known abbreviations (such as Ph, Me, Ac, and OPP); a label that depends on a stub of unknown structure (e.g., a residue or Ado) raises an input error until that group is written in full. Missing or contradictory stereochemistry fails the check, whereas unspecified double-bond geometry is unconstrained.

## Hydrogen-bond lines

For an `hbonds` overlap, the `FIX` line points to `python scripts/fix_hbonds.py INPUT --out NEW_DIR`. The script tests both the H and heavy-atom endpoints on the same donor, along with small turns, flips, or offsets of the smaller residue or water fragment, while preserving the substrate and existing hand-coordinate limits. At most three repair candidates are drawn; to pass, a candidate must reduce hydrogen-bond findings and worsen no other checks. The original input remains unchanged: an improved `input.hbonds.json` is saved alongside it (numbered if the file already exists), and the changes are recorded under `hbond_repair` in `NEW_DIR/.state.json`.

## Curved arrow shape

`arrows_shape` fails when a curved arrow is a loop (more than 2.0 times as long as the distance between its ends) or nearly straight (less than 1.06 times, an arc bulging less than 0.15 of that distance).

`arrows_ends` fails when an arrow's tail or head at a bond lies outside the middle half of the bond's drawn line. A short hook from a bond onto one of its own atoms (N–H to N) is exempt, but its head must reach the atom from the bond's side, within 70 degrees of the bond, instead of curling round past the atom. Any other head at an atom (including the atom that receives a forming bond's pair) must point, as drawn, within 45 degrees of the middle of that atom's symbol instead of passing beside it, and stay at least 1 pt clear of that atom's own bonds instead of covering one.

## Forming-bond arrows

- The two fishhooks of one forming bond (two radicals pairing) end at one point: `electron_flow` fails when their tips are more than 0.15 bond lengths apart.
- `arrows_ends` fails when the head of a forming-bond arrow is nearer another atom than both atoms of its gap, since it reads as pointing at that atom. Two atoms that are already bonded are a bond end, not a gap: the head goes to the middle of the bond that gains order.

## Names and labels

- `overlap_text_text` also fails when a free text and another name, or the residue names of two species, stand closer than 0.6 bond lengths, since a reader cannot tell which name labels which group. The layout keeps 0.8 bond lengths and does not place a text level with an atom label.
- `overlap_text_text` also fails when the letters of two atoms that are not bonded (nor bonded to one atom, as the two O of a carboxylate) stand closer than 5 pt, `overlap_text_bond` when an atom's letters stand closer than 5 pt to a bond of its molecule that ends neither at that atom nor at an atom bonded to it (except in a transition state, drawn in perspective), and `overlap_charge` when a charge circle stands closer than 5 pt to another atom's letters or to a bond that does not end at the circle's atom; `species_gap` fails when two molecules stand closer than half a bond length (7.2 pt).
- `one_label_bonds` also fails when a bond meets the H of a label (NH₂ with its bond on the H side). The heavy atom stands at the bond (H₂N), and a reacting lone pair goes above or below it.
- `residue_codes` also fails when what is drawn beyond a residue name is not that residue's side chain, such as His drawn without its imidazole ring. A main-chain piece and a name that stands for the whole residue are not checked.

## Showing the C of CO₂

`carbon_labels` fails when a required carbon symbol is omitted: on an isolated carbon, a linear carbon between two double bonds (such as the C in O=C=O), or a carbon in a two-carbon fragment. `labels_drawn` fails when a label is not drawn or when a visible carbon becomes hidden in the saved file.

## Color

- Check: After saving and reloading, `colors` verifies that every atom, bond, and residue name matches its requested input color (none = black) and that the SVG uses the requested colors and no others. Arrows are always `#000000`, and bonds between differently colored parts are black.
- Contrast: Below a 4.5:1 contrast ratio against white, 10 pt text is hard to read; the scripts record a `note:` in `.state.json` → `summary` below 4.5:1 for text labels and below 3:1 for lines. Among the Okabe–Ito colors, only blue reaches 4.5:1 (vermilion: 3.87, bluish green: 3.42, reddish purple: 3.06, orange: 2.25, sky blue: 2.31); `#C35600`, `#008762`, and `#A46186` preserve the original hues at 4.5:1.
