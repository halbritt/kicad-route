# Layered nominal-delay qualification article

Two single-ended paths and a ground connection on a six-layer, 1.6 mm board.
J1/J2 (DQ0) starts unrouted on the outside; J3/J4 (DQ1) already has 44 mm on
In2.Cu, two 2 mm F.Cu escapes and two F.Cu-to-In2.Cu transitions through vias.
The pads and footprints are fixed. GND planes occupy In1.Cu and In3.Cu.

The authored requirement is at most 1 ps nominal useful-path skew. Source
permittivity is an **agent-selected qualification assumption**, homogeneous
Er=4; it is not a supplier stackup or fabrication acceptance. All copper is
35 um. Dielectrics, top to bottom, are 0.2/0.2/0.59/0.2/0.2 mm.

At width 0.25 mm over the 0.2 mm outer dielectric, the nominal uncoated
microstrip approximation is
`Er_eff=(Er+1)/2+(Er-1)/(2*sqrt(1+12*h/w))`, hence 5.739552911 ps/mm.
Homogeneous stripline and the via model use `sqrt(Er)/0.299792458`, hence
6.671281904 ps/mm. These manually declared native profile values are rounded
only to KiCad's attosecond/mm unit. The calculator's inactive impedance target
is zero; this article imposes no impedance target or signal-integrity claim.

KiCad's used F.Cu-to-In2.Cu via height is 0.4875 mm: full outer copper,
0.2+0.035+0.2 mm between layers, and half the inner endpoint copper. DQ1's
expected delay is therefore `44*inner + 4*outer + 2*0.4875*inner`, or
322.999115 ps. Counting each complete 1.6 mm via barrel would be wrong.
Native final `fromTo` measurements independently select the useful geometry.

Run `python3 fixtures/delay_matching/generate.py` with KiCad 10 pcbnew to
regenerate native inputs and library dependencies. UUIDs may change; preserve
specific inputs during comparisons. Use the public command in
[the matching guide](../../docs/length-groups.md#nominal-propagation-delay).
The agent-ee fixture preparation supplies the same planes as captured plane
intent for its supported plane producer; existing-pour placement remains a
separate limitation. No accepted manufacturing package is claimed for this
qualification article.
