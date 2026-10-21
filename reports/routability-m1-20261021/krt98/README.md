KICADROUTE-98 replaces completion inferred from a capped DRC list with native
terminal partitions on the exact saved/refilled geometry. Every originally joined
pad UUID group must remain joined, including subsets of incomplete nets. Missing,
stale or incompatible coverage fails validation. Cache schema 2 retains four sealed
records and invalidates legacy or changed-producer reuse. Historical receipts stay
unchanged.

The corrected causal regression checks native partitions [1,2]|[3] changing to
[1]|[2,3], with one raw open connection before and after. Replaying the original
function from 3ae2c03 returns PASS; the new ratchet returns FAIL with the split
UUID group. The initial RED had the wrong intended joined pair and is retained
only as nonaccepting fixture history. A separate 511-net native fixture keeps
both raw reports capped at 499 while its native terminal deficit rises from 510
to 511 and loses an originally complete connection.

Independent extraction reconciles all 235 terminal-net UUID partitions on each
archived input/result. The origin has 53 complete nets and deficit 686; the two
results have 203/205 complete nets and deficits 55/51. All 53 original complete
nets and all five multi-pad groups on otherwise incomplete nets remain joined.
The earlier inferred count of 155 complete origin nets came from absence in the
capped report. The historical routes still need qualified policy and adoption evidence.

Native module, binding and interpreter hashes, KiCad CLI identity, producer
hashes, PCB and project/custom-rule hashes bind the evidence. Jumper semantics
are read through native serialization into a temporary copy; disconnected zone
islands sharing a parent zone UUID remain separate. Printed pad numbers do not
replace terminal UUIDs. Malformed identity and inventory tests guard the unknown
path. Board/rule/netlist geometry is unchanged by this software repair.

Existing routing.unconnected_* fields retain raw DRC counts. Separate terminal
metrics expose the full native graph. A raw count insufficient for terminal
obligations makes routing unknown and ineligible. This does not establish that
other native violation or schematic-parity finding lists are uncapped.

KICADROUTE-91 remains unreleased. Native policy enforcement, board and footprint
rule-area coverage, policy adapter retention and exact experiment identity still
need their own evidence. No corrected-policy target route or optimization sweep
was performed for KICADROUTE-98.

The final implementation is 8d866f65ac9ee2e50c2a83b6303663c3d5c99dff, landed and
pushed through git-land. The configured composed gate passed all 157 cases in
383.98 seconds, with no failures, errors or skips. Landing checks passed 54 cases
with one native case deselected because the composed gate already covered it.
The installed CLI doctor passed with offline/no-canary settings. Independent
reviews found no remaining code blockers. Earlier failed attempts are recorded
in nonaccepting-attempts.txt; the final gate and source hashes are in integration.json.
