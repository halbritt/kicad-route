# Nominal symmetric differential stripline software article

Generated 2026-10-25 by agent-545 from the prior outer-layer article. This is a
routing qualification article, not a product circuit or manufacturing release.
It has two fixed two-pin 2.54 mm through-hole headers and two GND anchors. The
header geometry and library module are captured unchanged from the installed
KiCad library; the included KiCad Libraries license applies to that module.

The signal pair uses In1.Cu between filled GND planes on F.Cu and In2.Cu.
Four 0.035 mm copper layers and three nominal 0.486667 mm dielectrics total
1.6 mm (the centre dielectric is 0.486666 mm to retain that exact total).
Each dielectric declares Er=4.2. Native 0.25 mm minimum width, 0.35 mm gap,
0.34--0.36 mm native gap rule, 0.15 mm skew and 6 mm uncoupled rule remain.

Agent-selected software assumptions: 100 ohms differential +/-10%, nominal
homogeneous symmetric stripline, and at most 4 mm total terminal escape per
member. The model solves 0.315869 mm width for 100.1537 ohms. The launch pads,
through-hole barrels and terminal escapes are excluded from that impedance
claim. Both adjacent references must cover the saved track copper, except for
the net's own PTH pads plus the smallest explicit zone clearance, bounded by
native polygon error plus 0.001 mm (0.006 mm here). A larger terminal opening
or a slot elsewhere is a failure even within the escape-length allowance.

The TI LVDS Owner's Manual SNLA187 p39 and TLK1XX SLVA531A p16 use conflicting
height notation in their coupling formula diagrams. With the same geometry,
the two interpretations produce 96.94 and 106.23 ohms respectively. Both are
within the declared nominal tolerance, but neither proves physical impedance.
Sources: https://www.ti.com/lit/pdf/snla187 and https://www.ti.com/lit/pdf/slva531.
Exact captured sources, calculations, failed probes and comparison results are
retained in agent-ee build/stripline; no supplier/physical validation is claimed.

```
bin/kicad-route run --dir fixtures/pair_stripline --backend builtin \
  --diff-pairs auto --pair-layers In1.Cu --pair-impedance 100 \
  --impedance-tolerance 10 --impedance-reference GND \
  --impedance-uncoupled 4 --grid-step .025 --budget 180
```

The coupled corridor, including the gap between the two traces, must also
have uninterrupted reference on both layers. This catches a slit that does
not intersect either trace. The native regression includes that negative.
