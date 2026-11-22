# IUPAC drawing rules applied by the scripts

The `reaction-draw` scripts apply these rules automatically. Rules decided by the agent are in `skills/reaction-draw/references/iupac-rules.md`.

## Text, lines, color, size

- Use a plain roman font that either matches the surrounding text or differs from it clearly.
- Set text at 8–14 pt in print (under 5 pt is not acceptable); lengthen bonds when enlarging text.
- Leave text unformatted; for emphasis, prefer bold over italics or underlining.
- Use a single font and style throughout a diagram; mixing them is not acceptable except for deliberate emphasis.
- Use a uniform line width close to the text stroke width; avoid lines under 0.5 pt, and use thicker lines only for emphasis or perspective.
- Treat bonds forming an angle of 170–190° as collinear.
- Draw structures in the same color as the surrounding content; avoid other colors except for emphasis.
- When coloring by element, prefer traditional molecular model colors (O red, N blue, Cl green); contradicting them (e.g., yellow O, red N, blue S) is not acceptable.

## Bonds

- Draw most bonds at a consistent length long enough to remain clear between labels; avoid lengths under twice the label height.
- Draw bonds as solid lines roughly matching the label stroke width; avoid widths over four times or under a quarter of that stroke.
- Do not let bond ends touch unless both bonds connect to the same atom.
- Typically space hashes, dashes, and wave crests two to four bond widths apart, with at least three visible per bond and consistent spacing throughout.
- Strongly avoid terminal bonds shorter than the standard length.
- Draw multiple-bond segments parallel and spaced within 33% of the bond length.
- Draw an offset double bond with one segment on the bond axis and the other usually shortened at each end along the angle bisector of the adjacent bond (unless that end is unsubstituted or trans-substituted); extend a centered double bond to meet the adjacent bonds at both ends.
- Offset a double bond toward the side with more substituents; center it when one end has two substituents and the other has none. With three substituents, centering is acceptable only when the two on one end are identical (or nearly so) and the lone substituent's atom is labeled.
- Offset acyclic trans double bonds to either side; offset endocyclic double bonds toward the ring center.
- Offset tetrasubstituted double bonds toward the ring (at a ring fusion, toward the ring with more double bonds); center only acyclic double bonds with identical substituents on at least one end; place all three double bonds of an alternating six-membered ring inside the ring.
- Draw coordination bonds as plain single bonds; dashed bonds and dative arrows are not acceptable.
- Show all H atoms on both atoms of a coordination bond even when the resulting valences appear non-standard; adding formal charges to adjust valence is not acceptable.
- Draw coordination to a contiguous π system as a single plain bond to the system; individual bonds to each atom, depictions implying a regular covalent bond, and dashed bonds are not acceptable.
- Draw a coordinated π system with alternating single and double bonds only if it has a single such bond pattern (e.g., butadiene); otherwise use a curve, including for benzene.
- Draw partial bonds (weak associations such as hydrogen bonds) as dotted lines; dashed lines are acceptable only where dots cannot be drawn.
- Use at least three dots per dotted bond, and avoid lengths under twice the label height.

## Atom labels

- Capitalize element symbols properly.
- Place H directly after the element symbol (or before it when bonds attach only on the right) with a subscript count; a labeled atom written without H bears no hydrogens.
- Treat an unlabeled atom as carbon with implicit H atoms filling its valence to four.
- Leave carbon unlabeled when it has at least two bonds, except always label an isolated carbon (no bonds) and a carbon with two identical collinear bonds.
- Label both carbons when ethane, ethene, or ethyne is drawn as one bond.
- Write an isotope's mass number as a leading superscript on the element symbol; D and T are acceptable, though preferably not in diagrams that also contain isotopes of other elements.
- Write an oxidation state as a trailing superscript roman numeral (or 0); omit it when obvious from the structure.
- Place the center of the bonded symbol (the first letter of a multi-letter label, the last letter when bonds attach only on the right, or the midpoint of a symmetric label) where an unlabeled bond would end, aim each bond at that center, and stop the bond at the label boundary.
- Reverse a label when bonds attach only on its right or to avoid overlap; subscript counts and charges remain to the right of their symbols.
- Never let a label obscure a bond; minimize overlap with all objects.
- Use a multi-line stacked label only on atoms with at least two bonds, keeping charges, isotopes, and counts on their symbol's line; prefer a single line if overlap is equal. Multi-atom labels with several non-H symbols are not acceptable on atoms with more than three other bonds.

## Orientation

- Rotate a structure freely in the plane (its meaning is unchanged, except in Fischer projections); when flipping a structure or fragment, swap solid and hashed wedges.
- Orient the main axis of a clearly elongated structure horizontally based on the overall chain direction, even if individual bonds are not at multiples of 30°; prefer the zigzag chain form (the linear form is acceptable).
- Draw rings of eight or fewer atoms as regular polygons.

## Substituent positions

- Draw two bonds collinear if and only if they are collinear in 3D, and angled if and only if they are bent in 3D.
- Draw two single bonds (or one single and one double bond) at 120° in a trans-like zigzag; avoid arbitrary bends.
- Preserve a double bond's cis or trans configuration in the relative orientation of its adjacent bonds.
- Draw truly collinear AX₂ centers at 180°.
- At an atom isoelectronic with carbon that has two double bonds (or one single and one triple bond), draw the bonds 180° apart.
- Draw three bonds at equal 120° angles (counting a double bond as one bond).
- At a four-bonded atom with two chain bonds and two terminal groups, set the chain bonds at 120°, the terminal groups at 60° opposite them, and the remaining angles at 90°; place the wedge and hash on the 60° pair, with the larger substituent on the right.
- Use this 60°/120° geometry also when only one neighbor continues the chain: set the 120° angle between the chain bond and a terminal carbon, or set the 60° angle between two identical non-carbon terminal groups.
- Draw four bonds at equal 90° angles when at least two are double bonds (equally preferred to the 60°/120° style; place two double bonds 180° apart unless overlap forbids), when all four neighbors are terminal, or when every neighbor bonds to at least two other atoms.
- With one small and three large neighbors, place the small neighbor at 60° between two large ones and the third large neighbor opposite at 120°, orienting the bond to the terminal substituent vertically or horizontally when overlap permits; use this style also when three neighbors are terminal and one is not, or when three terminal neighbors are identical.
- Aligning four bonds at a stereogenic center to the vertical and horizontal axes is not acceptable.
- For atoms with more than four bonds, draw one bond vertical and distribute the rest at equal angles; where stereochemical configuration matters, use the appropriate configuration styles.
- Direct unconstrained side chains outward from the center of the diagram.
- Draw a single substituent on an exterior ring atom bisecting the larger angle between its ring bonds (regardless of ring size or bond order); if within 5° of a multiple of 30°, snap it to that multiple.
- Separate two exocyclic substituents on a ring atom by 60° (regardless of bond order) rather than dividing the exterior angle equally; 90° is acceptable on four-membered rings, and equal division should be used when both substituents have two or more further bonds. A labeled end substituent (such as NH₃⁺) beside a branched one turns 30° further away so that their letters do not meet; a labeled chain atom (such as an imine NH) does so only when the drawing finds their letters too close.
- On a six-membered ring atom with one small and one large substituent, place the large substituent on the exterior angle bisector and the small one bisecting the adjacent angle (vertically or horizontally when possible) on the less crowded side, else right, else up; other ring sizes keep the 60° separation.
- Exact overlap of atoms or bonds is never acceptable; legibility overrides every other rule.
- Avoid swapping substituents across unequal angles (e.g., in a 120°/90°/30°/90° arrangement, swapping the 90° pair is not acceptable); never swap substituents if doing so changes configuration.
- To resolve crowding, lengthen a bond no further than necessary before bending, but prefer slight bending over lengthening a bond to twice its standard length or more; for two symmetric clashes, lengthen or bend both bonds equally by a smaller amount.

## Charges, lone pairs, unpaired electrons

- Draw charges, unpaired electrons, and lone pairs either as separate objects or as part of the label text.
- Place each charge clearly beside its own atom, never nearer another, and at the top right when legible; do not alter the diagram to make room.
- Indicate a positive charge with `+` after the element symbol, formatted as a superscript in text labels.
- Write a negative charge as a minus sign, preferring an en dash over a hyphen.
- Circled single charges are acceptable; multiple charges in a single circle are not acceptable.
- Write multiple charges as a number followed by the sign (`2+`, `3–`) only when the magnitude exceeds 1; `+2`, `++`, and `1–` are not acceptable.
- In a label combining one non-H symbol with H atoms, place a text charge after all H symbols (never immediately before or after the non-H symbol), or place it separately above or below the non-H symbol.
- In a multi-symbol or abbreviated label, place the charge after the last symbol (reversing the placement in a reversed label), never inside the label.
- Avoid labels that would require an interior charge; draw the atoms explicitly, or place the charge separately above or below its own symbol.
- Place lone pairs close to the atom, directly above, below, left, or right of the label, with the two dots parallel to the nearest side; strongly avoid other angles unless unavoidable.
- Drawing lone pairs as solid lines is not acceptable.
- Prefer placing an unpaired-electron dot beside the complete element/H label, and place a reacting dot on the side facing its fishhook target; departures produce warnings.
- A radical carbon may be left unlabeled, though labeling it is preferred.
- Represent multiple unpaired electrons with a multiplier before the dot at the top right (`2•`) or with well-separated dots; two adjacent dots denote a lone pair.
- In radical ions, place the charge immediately after the dot (`•+`, `•–`); for monocharged polyradicals, enclose the multiplier and dot in parentheses.

## Delocalization

- Without delocalization curves, any valid alternating arrangement of double bonds is acceptable.

## Salts

- Draw ionic structures with explicit charges and a space, not a bond, between the ions.

## Dative bonds and charge-separated forms

- Draw nitro groups in their singly charge-separated form (or as NO₂), optionally with a curve showing delocalization; two double bonds, a dative bond, or further charge separation are not acceptable.

## Tautomers

- Draw the specific tautomer intended; a diagram represents only the tautomer drawn.

## Annotations

- Place an annotation close to its target atom, bond, or structure and well clear of other objects.
- Typically place atom annotations in the largest open space beside the atom (or inside the ring for multiple ring atoms) and opposite any wedge for stereo labels; keep them nearer their own atom than any other, about half a capital's height away and less than a full height.
- Make annotations visually distinct from atom labels (using a smaller size or a different font, style, or color), and prevent small numerals beside labels from reading as subscript counts or superscript isotopes.
- Typically place structure annotations below the structure (avoiding left or right in schemes), nearer their own structure than any other, 0.5 to 2 bond lengths away, at least label size, and visually distinct from both atom labels and atom annotations.

## Pseudobonds and coordination polyhedra

- Draw pseudobonds clearly distinct from covalent bonds.
- For complexes with seven or more ligands, usually draw a polyhedron of pseudobonds between donor atoms with no central atom; do not mix equatorial pseudobonds with explicit axial bonds (as in TBPY-5 or PBPY-7).

## Stereochemistry: plain bonds, wedges, hashes, wavy bonds

- Draw single bonds at non-stereogenic atoms as plain thin lines; avoid stereobonds there (though acceptable to emphasize 3D, as for prochiral groups).
- Interpret plain bonds at a stereogenic atom as lying in the plane of the page; maximize the number of plain bonds.
- In simple and fused rings, use stereobonds only on acyclic substituents; use endocyclic stereobonds only when no alternative exists (e.g., spiro centers) or when all acyclic neighbors are stereocenters.
- Draw a solid wedge for a bond pointing toward the viewer and a hashed wedge for a bond pointing away, with the narrow end at the stereogenic atom.
- Use only hashed wedges for bonds below the plane; unwedged hashed bonds and reversed wedges are acceptable only on atoms with at most one other non-H substituent, and different hash styles must never be mixed in one diagram.
- Never draw an unwedged hashed bond between two stereocenters.
- Do not use unwedged bold bonds to show configuration at stereogenic atoms; reserve them for perspective.
- Reserve dashed bonds for partial bonds (including hydrogen bonds); never use them for configuration.
- Use a constant-amplitude wavy bond for unknown configuration (avoid wedged wavy bonds); use a plain bond where no emphasis is needed, including for mixtures.
- Strongly avoid stereobonds between two stereocenters; omit or reposition them instead. When unavoidable, prefer solid wedges (a hashed wedge is acceptable), treat only the narrow-end atom as specified, add stereobonds to adjacent centers of known configuration, and prefer the central-atom style with the fewest stereobonds.
- Treat a CIP descriptor as a supplement to, never a replacement for, wedges and hashes; the descriptor and drawing must agree.
- Preferred four-bond stereocenter pattern: two adjacent plain bonds (< 180°, ideally 120°) and one solid and one hashed wedge adjacent (ideally 60°), with their angle bisectors collinear.
- Avoid four-bond patterns with three or more wedges, and three-bond patterns with one plain bond and two wedges of the same type.
- Never draw a tetrahedral center using formally ambiguous patterns or geometries that read as square planar, square pyramidal, see-saw, trigonal planar, or T-shaped.
- Drawing three plain bonds with one wedge inside a reflex angle (≥ 180°) is acceptable only when all three plain bonds belong to a small fused ring system (or, rarely, to relieve congestion when two are in a ring); it is not acceptable for general use or when all four substituents are acyclic.
- Preferred three-bond stereocenter pattern (with an implicit H): two plain bonds (< 180°, ideally 120°) and one wedge bisecting the angle opposite them, with the wedged substituent graphically smallest.
- Omit H at a stereocenter only when unambiguous: the atom at the wide end of the wedge has at most two non-H attachments (including the wedge) or is a small non-stereogenic group (e.g., unsubstituted ring, tert-butyl).
- Never omit H when the atom at the wide end of the wedge is itself a stereocenter; draw the H explicitly.
- At a ring atom with one exocyclic non-H substituent, leave H implicit and draw the substituent on a wedge bisecting the exterior ring angle.
- At a ring atom with two simple exocyclic substituents, use one solid and one hashed wedge ~60° apart, centered on the exterior ring angle bisector; if one substituent is complex, connect it with a plain bond and place the wedge or hash on the simpler one.
- At ring-fusion stereocenters, wedge the exocyclic substituent (making H explicit if needed). With a vertical fusion bond, place a small substituent vertically opposite (even inside the ring), draw any other fusion bond as plain if its wedge would conflict with an opposite wedge, and never use this vertical orientation in bridged systems.

## Stereochemistry: non-tetrahedral centers and metal complexes

- Draw linear centers at 180° (horizontal when isolated) and angular centers off 180° (horizontal with bonds at ±30° when isolated).
- Never draw a trigonal pyramidal atom with an explicit lone pair at the end of a bond; if a lone pair is needed (e.g., in a mechanism), place it immediately beside the atom.
- Use the metal-center template angles as defaults; warn on departures except for forbidden depictions and linear AX₂ centers.
- Never draw a square planar, four-coordinate square pyramidal, or see-saw center with a pair of adjacent bonds ≥ 180° apart and of different bond types.

## Stereochemistry: perspective, double bonds, other configurations, mixtures

- Prefer plain bonds for double-bond substituents; H may be omitted and lone pairs are usually omitted, with any omitted substituent understood to lie opposite the drawn substituent on the same atom.
- Keep acyclic double-bond substituent angles near 120°; never place two substituents of the same atom on the same side of the double bond.
- Draw ring double bonds in the intended configuration regardless of viewing orientation; assume cis in rings of six or fewer atoms, and draw larger rings with the intended E or Z geometry even if non-convex.
- For unspecified double-bond configuration, use either a linear drawing (one substituent per end, double bond horizontal) or wavy bonds to the substituents (one wavy bond on a monosubstituted end, both wavy on a disubstituted end, plain toward a stereogenic neighbor, and never connecting the double bond to a stereocenter with a wavy bond); only when neither is possible, draw both isomers joined by "and/or".
- Crossed double bonds are not acceptable in general.
- Without extra indicators, a diagram represents the specific entity drawn: stereobonds at every center mean a single stereoisomer, whereas none means unknown configuration.

## Reaction schemes and curved arrows

- Use curly arrows to show electron movement in a reaction or to generate resonance forms.
- Start the arrow tail where the electron pair originates (a lone pair, σ bond, or π bond) and point the head to where the pair goes.
- Use a single-headed (fishhook) curly arrow for the movement of an unpaired electron.
- Use curly arrows for both intramolecular and intermolecular electron movement.
- Use `→` for a net forward reaction and `⇌` for an equilibrium.
