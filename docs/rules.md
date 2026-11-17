# Rules

IUPAC rules ([decided by the agent](../skills/reaction-draw/references/iupac-rules.md), [applied by the scripts](iupac-rules-script.md)) take precedence over this skill's conventions.

## Source and workflow

- Preserve the source's chemistry and protonation states; ask if anything is left unspecified.
- Before writing the input file, list the reaction steps (including catalyst or cofactor regeneration), residues, and curved arrows.
- Start from a template, keep its atom maps, and record the source figure and page.
- Use the bundled scripts; do not write your own parser, checker, or renderer.
- Let the scripts generate the initial layout; fix issues in the input file, never in the rendered drawing.
- On a failure, draw three revised versions of the input at once (for example, abbreviate non-reacting groups or split the step into two panels) and continue from the best one. After two such rounds without a pass, hand over the best figure and say that it fails the checks (unless the user asks otherwise).
- After a pass, revise what still reads badly in the same way (three versions at once, at most two rounds unless the user asks otherwise), and hand over the passing version that reads best.

## Size and layout

- ACS settings: Arial 10 pt, bond length 14.4 pt, line width 0.6 pt, bold width 2 pt, double-bond spacing 18%, label margin 1.6 pt, hash spacing 2.5 pt.
- Scale the entire figure uniformly to preserve its proportions.
- Use the standard bond length throughout, including in rings and between labels; lengthen a bond between two labels only enough to keep the bond line visible.
- Draw chains at 120° angles and maintain standard geometry at each center.
- Resolve overlaps by moving atoms or labels, never by hiding them or covering them with white boxes.
- Keep each molecule's position and orientation consistent across panels; place an incoming group on the side it approaches from.
- Maintain at least half a bond length (7.2 pt) of white space between molecules.
- Arrange the figure from left to right, wrapping into rows; align the panels and make all reaction arrows within a row the same length.

## Symbols, charges, and labels

- Show the C in CO₂ and any carbons that require an explicit symbol (such as methyl groups); leave other skeletal carbons unlabeled.
- Condense hydrogens into labels (OH, NH₃); draw H atoms explicitly when they react or define stereochemistry.
- Circle single charges by default, or leave them all uncircled to match the source; write ionic charges as superscripts with magnitude before sign (Mg²⁺).
- Place each charge next to its atom, clear of bonds and labels.
- Number only the atoms that require it (those referenced in the text or a product name, or moving between molecules), and never number H atoms; use primes (′ and ″) for atoms in the second and third molecules.
- Label residues with the three-letter code and number (Glu170), or the one-letter code (E170) when requested or used by the source; use a single style per figure.
- Draw the atoms, bonds, and charges of a residue's reacting group in full, with the residue label at the root of the side chain.
- Show each residue once per panel. Draw a residue whose backbone takes part (with its side chain when that takes part too), and consecutive residues shown with their backbone, as one chain cut at both ends with wavy truncation marks, and name each residue with a text near its α carbon.

## Arrows and lone pairs

- A full arrowhead moves two electrons; a fishhook moves one.
- Start each curved arrow at the electron source (a lone pair, bond, or radical dot) and end it at the destination.
- Draw only the lone pairs that react, not full Lewis structures.
- Draw all curved and reaction arrows in black.
- Reaction arrows: → reaction, ⇌ equilibrium, ↔ resonance, ⇄ both directions, = stoichiometric relation.

## Stereochemistry

- Specify the expected *R*/*S* configuration of every stereocenter based on the source paper, the compound's standard name, or a database.
- The α-carbons of the 20 standard L-amino acids are *S* (except cysteine, which is *R*, and glycine, which is achiral).
- Drawn wedges and hashes must match the input *R*/*S* and *E*/*Z* configurations, including after the file is saved and reopened; mirror images are rejected.

## Transition states and hydrogen bonds

- Draw a transition state only upon request, enclosed in square brackets with ‡ at the upper right.
- Use dashed lines for partial bonds in a transition state and dotted lines for hydrogen bonds.
- Draw each hydrogen bond from the donor's H atom; if it crosses a label or bond, move the smaller fragment (usually the residue).

## Radicals, metal complexes, and isotopes

- Use the dedicated input keys for radicals, coordination bonds, η-ligands, oxidation states, and isotopes; never simulate them with charges, extra bonds, or plain text.
- Place a reacting radical dot facing the target of its fishhook arrow.
- Draw coordination bonds as plain single bonds, using a single bond between a metal and a π system.

## Color

- Use black throughout unless color is requested.
- Apply element colors only to heteroatom labels; keep C and H black.
- When coloring by source molecule, use a color-blind-safe palette; keep arrows, titles, and numbers black.
- Maintain a contrast ratio against white of at least 4.5:1 for text and 3:1 for lines.

## Checks and delivery

- The scripts check overlaps, crossings, charges, and labels, and re-read saved files to verify them against the input.
- Deliver a figure only when all automated checks pass and visual inspection confirms that the chemistry matches the source.
- Deliver `.cdxml`, SVG, and PNG (600 dpi at actual size) files with a 2–4 line caption stating the source, certainty, and abbreviations.

## References

1. J. Brecher. “Graphical representation standards for chemical structure diagrams (IUPAC Recommendations 2008).” Pure Appl. Chem. 80 (2008), 277–410. [doi:10.1351/pac200880020277](https://doi.org/10.1351/pac200880020277).
2. ACS. [Preparing Manuscript Graphics](https://pubsapp.acs.org/paragonplus/submission/general/graphics_prep.html), “Chemical Structures” (2006).
3. J. McMurry. [Organic Chemistry, 10th ed.](https://openstax.org/books/organic-chemistry/pages/dedication-and-preface), OpenStax, 2023.
4. W3C. [WCAG 2.2](https://www.w3.org/TR/WCAG22/), criteria 1.4.3 and 1.4.11.
5. J. Brecher. “Graphical representation of stereochemical configuration (IUPAC Recommendations 2006).” Pure Appl. Chem. 78 (2006), 1897–1970. [doi:10.1351/pac200678101897](https://doi.org/10.1351/pac200678101897).
6. C. L. Perrin et al. “Glossary of terms used in physical organic chemistry (IUPAC Recommendations 2021).” Pure Appl. Chem. 94 (2022), 353–534. [doi:10.1515/pac-2018-1010](https://doi.org/10.1515/pac-2018-1010).
7. IUPAC Gold Book: [curly arrows](https://doi.org/10.1351/goldbook.08160), [electron pushing](https://doi.org/10.1351/goldbook.08166), [resonance form](https://doi.org/10.1351/goldbook.08205), [chemical reaction equation](https://doi.org/10.1351/goldbook.C01034).
8. Template structures: [PLP](https://www.ebi.ac.uk/chebi/searchId.do?chebiId=CHEBI:597326), [PLP aldimines and carbanion stabilization](https://pmc.ncbi.nlm.nih.gov/articles/PMC5392587/), [NAD⁺](https://www.ebi.ac.uk/chebi/searchId.do?chebiId=CHEBI:57540), [NADH](https://www.ebi.ac.uk/chebi/searchId.do?chebiId=CHEBI:57945), [ATP](https://www.ebi.ac.uk/chebi/searchId.do?chebiId=CHEBI:30616), [acetyl-CoA](https://www.ebi.ac.uk/chebi/searchId.do?chebiId=CHEBI:57288) (ChEBI and PMC).
