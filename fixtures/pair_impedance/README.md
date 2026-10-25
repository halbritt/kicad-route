# Nominal differential microstrip software article

Original generated four-layer fixture, agent-545, 2026-10-24. This is a tooling
qualification article, not a product circuit or a manufacturing release.
Two fixed signal terminals and two through-hole GND anchors; an In1.Cu plane
was produced and filled by agent-ee's normal plane producer. Native input and
original rules are retained. Sources and first failed/successful probes are in
agent-ee `build/impedance`.

Agent-selected assumptions: 100 ohms differential +/-10%, bare microstrip
model; F.Cu only over GND/In1.Cu; 0.035 mm copper, 0.2 mm prepreg Er=4.2,
1.06 mm core, symmetric lower stack. Native minimum width remains 0.25 mm,
nominal gap 0.35 mm. Synthesis selects 0.325537 mm width. KiCad's independent
coupled-microstrip calculator estimates 102.18 ohms at 1 MHz for this geometry;
its exact source and compiled calculation are retained with the probes.

The declared 3 mm maximum total terminal-escape length per member separates
nominally coupled trunks from the launches; the native 6 mm uncoupled rule is
unchanged. This is an explicit software coverage assumption, not an owner
product requirement. The initial exploratory 0.5 mm limit failed at 2.514 mm
and remains a rejection control. No claim applies the 100-ohm trunk model to
these terminal escapes, pads, coatings, vias or physical supplier tolerances.
