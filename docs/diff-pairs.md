# Differential pairs with the built-in router

`kicad-route run --backend builtin --diff-pairs auto` routes differential pairs before
the remaining selected nets. Without `--diff-pairs`, the existing single-ended
adapter and its command line are unchanged. The option also passes through
`campaign` and corpus replay, like the other routing options.

Detection uses KiCad's native loader and each net's effective net class. It matches
case-sensitive `_P`/`_N` and `+`/`-` suffixes, keeping the entire sheet path and
requiring the same suffix convention on both members. Bare `P`/`N` is recognized
only when both names exist (for example `TXP`/`TXN`); `VIN` alone stays ordinary.
Other backend-specific aliases, auto-generated `Net-(...)` names, and lowercase
suffixes are not detected. Class settings alone do not designate a pair: KiCad
also supplies those defaults on ordinary nets.

Before routing, exit 4 refuses:

- an unpaired `_P`, `_N`, `+`, or `-` net in the selected scope;
- a selection containing only one member of a detected pair;
- members in different effective classes, or without matching positive native
  `diff_pair_width` and `diff_pair_gap` settings;
- a pair gap below either member's class clearance, which the built-in router would otherwise
  raise silently, or fewer than two pads on either member;
- `--default-clearance` larger than a pair gap, which the built-in router would also raise;
- a backend other than the built-in router, a missing installed `route_diff.py`, or
  `--backend-arg` with this option. Verbatim backend arguments could override
  pair selection or geometry. `--backend-env` remains available and recorded.

Ordinary pair routing gives each pair its own `route_diff.py` process with explicit class width and gap,
unchanged project rules, no DRC-settings writeback, no ground-via generation,
no stub layer swaps, and existing copper protected according to `--protect`.
Pairs run in sorted positive-name order and share the attempt's backend budget.
Explicit [coupled length groups](length-groups.md#coupled-pair-to-pair-length-matching)
keep selected pairs in one process so tuning can use all their centerlines;
final native rules still grade every pair and useful path.
The adapter passes `--no-short-pair-deferral`: the backend's estimated short
length cannot waive the explicitly selected coupling policy. A short pair gets
a coupled attempt with the same native geometry rules. This flag does not make
a failed attempt acceptable or turn hybrid terminal escapes into coupled ones.
Cleanup also trims generated track tails that pass through a terminal pad and
end beyond it. The cut stays on the existing centerline inside actual pad
copper on the same layer, and must preserve terminal connectivity. Locked and
protected input copper remains unchanged. Native verification still grades
the saved result, including dangling copper and pair-gap rules.
The adapter accepts only a complete coupled report for both members; it discards
partial, hybrid-terminal, deferred and single-ended fallback output, including
any collateral edits. Missing summaries/pair reports and process failures are process failures
(exit 1). A normal pair-routing failure leaves the previous board available for
native grading (exit 2 when otherwise valid). There is no single-ended retry of
failed pair members. A timeout killed by the wrapper remains exit 1.

Accepted pair copper is temporarily locked for all later passes. Every pair
member, including failed members, is excluded from the ordinary `route.py`
selection. Lock flags are restored before grading. Pairs already connected in
the input are preserved and measured with `backend_status: ALREADY_CONNECTED`;
this records existing connectivity, not a claim that this invocation constructed
coupled copper. Native DRC and all preservation checks still grade the candidate.
New pair tracks use their native **diff-pair width** (or the explicitly requested
impedance-solved width), rather than the class's ordinary track width, for the
net-class geometry preservation check.

## Pair evidence

Opt-in results have a top-level `pairs` array in `result.json` and a table in
`RESULT.md`. The array is present even when empty or the process fails. Ordinary
runs omit it. `validate` measures the current candidate again and includes the
same array in its record. Pair results carry `schema: "kicad-route/route/2"`;
re-validation with pair evidence carries `kicad-route/route-validate/2`. Both use
[`schemas/result-2.json`](../schemas/result-2.json), including when the array is
empty or measurements are unavailable. Runs without the option retain
`kicad-route/route/1` (and re-validation `/route-validate/1`).

| Field of each pair | Meaning |
|---|---|
| `p`, `n` | Exact positive and negative net names |
| `netclass` | Native effective class shared by the members |
| `width_mm`, `gap_mm` | Native class leg width and copper-edge gap passed explicitly to the built-in router, in mm; these are router settings, not measured impedance |
| `initially_connected` | When available, whether the input's native baseline DRC had no open item on either member; recorded in `attempt.json`'s pair plan, outside the fingerprinted `settings.pairs` |
| `routed` | Both members have native connectivity evidence, accepted coupled routing (or preserved input connectivity), available lengths, and no native gap finding |
| `length_p`, `length_n` | Sum of native planar track/arc lengths on each member in mm, rounded to 6 decimals; includes existing copper and branches, excludes via barrels and pad traversal; `null` without a measurement |
| `skew` | Absolute difference of those lengths in mm, rounded to 6 decimals, or `null` |
| `gap_violations` | Number of native `diff_pair_gap_out_of_range` findings involving either member, regardless of severity or baseline status; unidentified gap findings count against every pair; `null` if DRC did not run |
| `status` | `ROUTED`, `INCOMPLETE`, or `NOT_RUN` when native evidence is unavailable |
| `backend_status` | `COUPLED`, `ALREADY_CONNECTED`, `REJECTED`, `PROCESS_FAILED`, `BUDGET_EXHAUSTED`, or `NOT_RUN` |
| `backend_report` | The pair pass's full `JSON_SUMMARY` diagnostic from the built-in router, or `null`; never a substitute for native grading |

Class pair settings supply router defaults. To enforce a specific gap range,
declare a `diff_pair_gap` constraint in the project's `.kicad_dru`.
Zero `gap_violations` means native DRC reported none under the unchanged input
rules; it does not assert a coupling percentage, a skew limit or impedance.
The test fixture supplies an explicit native gap rule.

## Layer transitions

`--pair-layers F.Cu In1.Cu In2.Cu B.Cu` permits new pair tracks on those
enabled native copper layers. The default remains `F.Cu B.Cu`. Duplicate,
disabled or non-copper layers are refused, as is use without `--diff-pairs auto`.
The selection is recorded in `settings.pair_layers` and forwarded through
campaign/corpus replay. Final validation independently rejects new pair tracks
outside the selection. Preserved input copper may occupy other layers;
through-vias span the board even when not every layer is selected for tracks.

Straight via transitions retain the pair centerline and symmetric fanout.
Cleanup precedes intra-pair and AC matching so those tuners consume cleaned
geometry. This does not qualify AC, inter-pair or propagation-time matching.
The native `fixtures/pair_transition` article and `tests/test_pair_transition.py`
exercise a four-layer board with orthogonal launches and an outer-layer barrier:
the coupled route must use inner copper, preserve authored rules, and satisfy
both native DRC and the separate planar-copper matching tolerance. Outer-only
routing and changed-layer revalidation are negative controls.

The opt-in `settings` adds `diff_pairs: "auto"` and `pairs` (the deterministic pair
plan). `attempt.json` carries `schema: "kicad-route/attempt/2"` and adds `pairs`
and `pair_routing` to the base `attempt/1` key set. Without the option, the attempt
carries `kicad-route/attempt/1` and omits both keys. The exact key sets are listed
in [interface.md](interface.md#attempt-and-result-key-sets). `pair_routing` is keyed
by positive net name with `status` and optional backend `report`. Re-validation reads these
receipts; it cannot turn a rejected single-ended fallback into coupled success.
Native settings and lengths are in `facts/pair-facts.json` and
`validate/pair-facts.json`: a net-name map with `netclass`, integer-nanometre
`width`, `gap`, `clearance`, native `length` in nanometres, and `pad_count`.
`validate/pairs.json` holds the graded array. Both validation receipts are hashed
in `evidence.records` and included by `publish`. Work logs and intermediate boards
remain under `work/diff-NNN/`, `work/single/`, and `work/diff-out/`.

`tests/test_diff_pairs.py` builds a two-terminal CAN fixture with a native gap
rule, verifies a later ordinary-net pass, checks native DRC and preservation,
re-validates the pair, and tests an impassable keepout wall and detection refusals.

## Nominal impedance on one signal layer

An opt-in impedance request uses the declared native stackup to solve a pair
width before routing. The solved width feeds the same offset, fanout and obstacle
geometry as the ordinary fixed-width route. The native class width remains a
floor and all authored rules remain intact.

```sh
kicad-route run --dir fixtures/pair_impedance --backend builtin \
  --diff-pairs auto --pair-layers F.Cu --pair-impedance 100 \
  --impedance-tolerance 10 --impedance-reference GND \
  --impedance-uncoupled 3 --grid-step .025 --budget 180
```

All four impedance controls are required. One explicit outer signal layer uses
the adjacent filled reference plane. One symmetric inner signal layer uses
both immediately adjacent filled reference planes, each on the declared net.
Copper and dielectric thickness and Er must be explicit. Inner dielectric
heights must agree within one native nanometre, and Er values must be equal.
Asymmetric, multilayer, arc, via and coplanar geometry cannot pass this model. These remain
separate engineering capabilities, not interchangeable analytic approximations.

`facts/impedance-plan.json` records the target, source board hash and solved
geometry. `validate/impedance.json` measures actual saved native segments and
filled reference copper. Each member's total coupled and terminal-escape lengths
are reported separately. Coupled trunks must match the declared gap within
0.001 mm and satisfy the requested calculated-impedance tolerance; the total
remaining length must fit `--impedance-uncoupled` and form pad-connected
terminal escapes; a middle-of-trunk discontinuity cannot use that allowance. All target tracks must respect
the native pair-width floor and lie over filled copper belonging to the named
reference. Terminal escapes may use a narrower width; their impedance is not
graded as a coupled trunk.
The native polygon comparison uses a conservative 0.001 mm approximation bound.
Revalidation repeats these checks against the current saved candidate.

These are nominal uncoated microstrip or symmetric homogeneous stripline calculations. Pad launches, solder mask,
fabrication tolerances, frequency-dependent material data, transitions and
physical measurements need separate evidence. The terminal-escape allowance
does not extend the impedance claim to that copper. The fixture assumptions,
including the rejected exploratory escape bound, are documented with the native
input in `fixtures/pair_impedance/README.md`.


The inner-layer software article is `fixtures/pair_stripline` (`--pair-layers
In1.Cu --impedance-uncoupled 4`, with the other controls above). Both native
reference fills are checked. A PTH launch may use only its own pad plus the
smallest explicit zone clearance and a reported numerical allowance of the
native polygon error plus 0.001 mm. This is not permission for a plane split
or an oversized terminal opening. Outer-layer coverage has no such exception.
The input fixture retains its footprint-library dependency; PCB-only snapshots
use the same library capture operation as projects with schematics.

For inner coupled trunks, coverage includes the complete corridor between the
outer edges of both traces. A split only between the pair members is rejected
as well as a split beneath either trace. Terminal antipad permission does not
apply to this coupled corridor.

## Single-ended buses alongside pairs

`--bus` enables the maintained geometric bus detector, middle-out ordering and
shared-corridor/neighbor attraction for the single-ended routing pass. It can
be combined with `--diff-pairs auto`: pair members are routed first, protected,
and excluded from bus scope. Omission keeps the previous routing behavior.

Optional `--bus-detection-radius MM` and `--bus-attraction-radius MM` default to
5 mm; `--bus-attraction-bonus N` defaults to 5000 and `--bus-min-nets N` to 2.
The existing geometric filter can require at least three coherent members even
when the raw clustering minimum is two. These are search preferences; no native
rule, net identity or authored placement changes. Invalid/nonfinite bounds,
controls without `--bus`, conflicting raw backend flags and other backends
refuse before routing.

The actual single-ended process retains its native net-story diagnostic and
reports observed groups under `process.bus_routing`, including the diagnostic
and raw-board hashes. Missing diagnostics cannot establish a successful bus
operation. Geometric membership is not electrical bus identity, length/time
matching or signal-integrity acceptance. Final saved connectivity, geometry and
preservation are still graded independently by native checks.

`fixtures/bus_routing` and `tests/test_bus_routing.py` exercise eight lanes, a
third terminal, an unrelated parallel connection, a native obstacle, paired
scope separation, invalid controls and rejection of a missing saved lane.
