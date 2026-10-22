# Whole-net connector qualification

2026-10-21. Source base: `814252b40b11b2411e3031cea990899e6e2fbd32`.
Agent-510 verification with Sol fixture implementation and independent review.

The proposed isolated repair worker will use
`router/py_router/net_rescue.py::_attempt_net_at_geometry`. The earlier
competing-route experiment called `route_net_with_obstacles` directly, so it did
not establish that the whole-net connector closes multiple terminal groups.
This verification exercises that existing connector without changing it.

## Measured behavior

Three bounded scratch jobs exercised the actual owned A* search on a synthetic
corridor board. Each job routed A, then B in the same parsed board model. The
cases cover two-terminal nets, a three-terminal B net, and unequal scalar widths
(A 0.20 mm, B 0.25 mm). Each job had a 20-second process timeout and 100,000
iterations per edge; no parameter search or supplied solution path was used.

All three jobs passed in 1.47–1.67 seconds each, including fixture emission and
the child's checks. These timings are diagnostic observations, not production
budgets or comparisons against a target-board baseline. In the multi-pin case,
B required two committed connector edges to join its three terminal groups.
The unequal-width case retained the full per-net width map and selected each
net's scalar map geometry with `get_net_track_width` before routing it.

Parser IDs were deliberately shifted away from native KiCad net codes. Native
emission resolved net names, and counted native copper geometry matched the
connector's returned geometry. Final native widths were 200,000 and 250,000 nm
in the unequal-width case. The source project, fixed copper, pad identities,
keepouts, outline, enabled layers and rules stayed unchanged.

Native terminal partitions covered every electrical pad UUID. The final A and B
nets each had one native component; the originally connected FIXED net remained
connected. Original terminal equivalences survived. Final native DRC reported
zero unconnected items and zero errors. Each output's warning records exactly
matched its own input: six library warnings in the ordinary and unequal-width
cases; seven library warnings and one silk-overlap warning in the constructed
multi-pin input. No warning was introduced by routing.

Scratch commands, settings, source/kernel identity, boards, native partitions
and DRC reports are retained under
`~/.local/share/agent-ee/reviews/2026-10-21-krt93-connector-agent510/`.
The repository tests preserve these scenarios for subsequent worker development.

The portable tests reuse the repository's `RoutingBoard` factory, whose text and
footprint metadata differ from the original diagnostic generator. Their input
warning totals are 13 for ordinary/unequal-width cases and 15 for multi-pin;
they compare complete warning records against each corresponding input, not the
earlier generator's totals. Initial execution passed all three cases in 8.70
seconds with no skips. Review subsequently tightened process-group cleanup and
removed an assertion about the internal edge-list length; native connectivity
is the required behavior. The final focused run passed all three cases in 9.16
seconds with no skips. Root reviewed the test source and accepted these two
corrections; independent Sol review verified the retained diagnostic claims and
their stated limits. No production algorithm files changed.

Run the focused gate with an existing numeric interpreter and owned extension:

```sh
mkdir -p /tmp/kicad-route-connector-check
KERNEL_PYTHON=/absolute/path/to/existing/numeric/python \
KERNEL_EXTENSION=/absolute/path/to/owned/libgrid_router.so \
KICAD_ROUTE_TEST_OUT=/tmp/kicad-route-connector-check \
PYTHONPATH=src:tests /usr/bin/python3 -m pytest -q tests/test_repair_connector.py
```

The interpreter and extension overrides are existing kernel-test conventions;
the Python routing sources must come from the tested checkout. Missing runtime
support is an explicit skip, so qualification requires three passes and zero
skips. Neither installation nor a target-board routing campaign is needed.

## What this establishes

The existing whole-net connector is a demonstrated starting point for the
bounded root-then-victims worker. Its internal completion flag is insufficient
for acceptance: tests independently inspect the emitted board with native KiCad.
This covers actual search, multiple terminal groups, net identity rebinding and
scalar width selection; no mocks substitute for those operations.

This is not a repair transaction or public repair command. There is no custody
manifest consumer, victim removal, configuration-evidence replay, timeout
rollback, atomic publication or campaign selection in these tests. The earlier
fixture separately established B-first blockage and successful displacement;
these cases start after that removal and qualify the specific connector.

No vias, copper zones, differential pairs or variable per-layer widths are
exercised. The native DRC runs are not the complete wrapper's refill/parity/pour
grading pipeline. No target-board completion or runtime claim follows.

## Required zone integration

The retained real `route_demo` checkpoint includes the `GND_B` zone on B.Cu;
the target board also depends on planes. A zone-free worker can be an initial
test stage but cannot qualify those boards or complete the repair milestone.

Reuse `cli.kicad_drc(..., refill_and_save=True)` to establish a saved, refilled
scratch stage after authorized removal, within the same whole-trial deadline.
Verify its non-fill geometry, copper custody and exact rules against the removal
receipt; record the legitimate change from the stripped-board hash to the
refilled-stage hash. Parse this new board in a fresh routing child.

`pour_guard.install_pour_barriers` already distinguishes a supplied parent's
barrier list from recomputation. Its polygons are derived from fill and scope,
not immutable physical policy. A worker must deliberately qualify which barriers
can be retained and which require recomputation on its refilled board. It must
not silently reuse stale fill or clear the barriers to improve routing.

Final native refill and grading against the original reference, plus preservation
of the incumbent's native terminal equivalences, remain mandatory for every net,
including unselected plane nets. Model connectivity in
`_net_component_info`/`check_net_connectivity` is search guidance; it cannot
certify the final filled-zone connectivity. Plane support needs a real zone-cut
negative case and a legal signal-repair positive before qualification.

K93/K94 remain incomplete. Agent-449 owns worker dispatch and budget under
agent-508; these are verifier-owned regressions for an existing connector.
