KICADROUTE-90 repairs campaign selection. A candidate must pass the existing
native grade, successful refill and DRC command checks, preservation checks and
connectivity evidence before it can become `best` or stop a campaign as complete.
Invalid attempts remain in the diagnostic leaderboard. When none is eligible,
`kicad-route/route-campaign/2` returns exit 1, `best: null` and the unchanged
captured input. Route and validate schema versions remain unchanged.

The implementation candidate is `683b270ca1a5d19c7422098a29893d55230d0af4`.
`krt90-red.txt` retains the failing regression against `557ae57`;
`krt90-focused.txt` and `krt90-review.json` retain focused verification and the
independent review. The integration receipt records the composed native gate.
The repair changes no routing geometry, electrical policy or solver costs.

KICADROUTE-91 is unfinished and has no released pin. Its preparatory candidate
`10ff94b874e840293c0ac3e73a703cee46aaae10` is retained locally under
`build/routability-m1/prep91/identity-prep.bundle` and `identity-prep.patch`.
It is not integrated: real strict comparisons require complete native board and
footprint rule-area coverage, actual policy verification and native terminal
evidence. A synthetic positive comparison is insufficient. No corrected-policy
target route, broad routing sweep or optimization measurement was performed.

The historical audit reproduces 55 connections on 32 nets and 51 on 30 from raw
native DRC. The historical input hit KiCad 10.0.7's 499-item unconnected-report
limit. Native pad-UUID component checks on the exact saved/refilled input show
that all ten reported lost nets were already incomplete. None of the 53 truly
complete origin nets becomes incomplete in either historical result. The old
completion ratchet inferred 155 complete nets from absence in the capped report;
that inference needs a separate correction before origin-valid promotion.

This does not qualify the historical routes for adoption. They use all four
trace layers, retain a new dangling-via finding and lack a captured schematic
identity. Current-policy capture and adapter coordination remain with agent-449.
Native inspection finds six footprint-owned keepouts and zero board-owned rule
areas; the old board-only facts count does not establish absent keepouts.

`historical-audit.json`, `historical-evidence.tar.gz` and its index retain the
raw count reconciliation, exact refill identities, native component UUIDs and
reproduction scripts. PCB/project files remain in the local ignored audit
directory. KICADROUTE-92–97 retain their ticket dependencies and conditional
release gates; no downstream gate is released by this report.
