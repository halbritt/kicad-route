# Paired layer-transition software article

Two fixed connectors on a four-layer board, with orthogonal launches. An
outer-layer track/via keepout forces routing through inner copper. This is the
native article captured from agent-ee's asymmetric-pair generator plus a native
rule area. It has no circuit, impedance, return-path or manufacturing claim.

Agent-selected qualification constraints: 0.25 mm tracks, 0.35 mm nominal pair
gap, 0.2 mm clearance, 70 mm maximum native length, 0.15 mm native skew and
6 mm maximum uncoupled length. The project and rule files are authoritative.
Native electrical length and summed planar copper length are different metrics.

`tests/test_pair_transition.py` copies this input, runs the public router and
independently grades the saved files. No source input is modified by the tests.
