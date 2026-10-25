# Native bus-routing article

This generated software qualification board has eight parallel nets D0–D7
between fixed native 2.54 mm headers, a third terminal on D3, and a separate
parallel AUX connection. A routing-only rule area on F.Cu obstructs the upper
lanes. The stock header modules and their library license are captured locally.

The native project requires 0.25 mm track width and 0.2 mm class clearance;
its board floors and rule severities remain authoritative. The 70 by 65 mm
outline and fixed footprint coordinates are part of the input. A 20 mm bus
endpoint-clustering radius and minimum three members are agent-selected search
preferences, not electrical acceptance criteria.

Exercise with `kicad-route run --dir fixtures/bus_routing --bus
--bus-detection-radius 20 --bus-min-nets 3 --diff-pairs auto --grid-step .1`.
The final saved board must have no opens or native findings and preserve input
constraints. Actual backend diagnostics should identify D0–D7 as a geometric
bus, exclude AUX and connect all three D3 terminals. Geometric grouping does
not establish electrical bus identity, timing, impedance or manufacturability.
