# Routing attempt: krt on route_demo

- Backend: krt 0.23.1 (v0.23.1)
- Input: `route_demo.kicad_pcb` from working tree @ None (fingerprint 58a7a678f1b3b03c)
- Scope: all, 6 nets; protect: existing; budget 240 s
- Candidate: none

| Result | Status | Detail |
|---|---|---|
| process | FAIL | exit -15, 34.233 s; KRT stalled in net rescue at rescuing /VIN, parts 2 for 6 s (--stall 6), did not stop at the stop file, and was killed |
| routing | NOT_RUN | - unconnected in scope, - total (input: -) |
| validation | NOT_RUN | backend produced no candidate |

Unsupported or approximated by this backend:

- KRT reads only a subset of .kicad_dru custom rules; every rule is still graded by KiCad DRC
- KRT moves copper gr_text and net-less copper graphics to silkscreen (not switchable on the CLI); the copper_graphics preservation check reports it
- KRT's cleanup may neck wide new segments near foreign pads without a flag; the new_copper_netclass_geometry check reports any new copper below its net class
- backend reported: .kicad_dru: rule 'power_to_signal_clearance': clearance 0.4mm has a non-layer scope (netclass/area/... condition) -- skipped, KiCad will still enforce it

