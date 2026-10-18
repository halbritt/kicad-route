# Routing attempt: krt on route_demo_fzone

- Backend: krt 0.23.1 (v0.23.1)
- Input: `route_demo.kicad_pcb` from working tree @ None (fingerprint 2ef2b80912cdee6d)
- Scope: all, 6 nets; protect: existing; budget 180 s
- Candidate: `runs/route_demo_fzone/smoke-spokes-fzone-krt/20261018T051727.837981Z/candidate/route_demo/route_demo.kicad_pcb`

| Result | Status | Detail |
|---|---|---|
| process | PASS | exit 3, 131.378 s; KRT --strict-sizes: it reports delivering geometry below a requirement (candidate kept for grading) |
| routing | COMPLETE | 0 unconnected in scope, 0 total (input: 5) |
| validation | FAIL | preservation new_copper_netclass_geometry: 16 new tracks and 2 new vias checked against their net class width / via (microvia) diameter / drill (1 um quantisation tolerance); 1 below |

| Preservation check | Status |
|---|---|
| project_files | PASS |
| board_outline | PASS |
| layers | PASS |
| stackup | PASS |
| board_setup | PASS |
| nets_and_netclasses | PASS |
| pad_connectivity | PASS |
| footprints | PASS |
| zones | PASS |
| keepouts | PASS |
| copper_graphics | PASS |
| protected_copper | PASS |
| new_copper_netclass_geometry | FAIL |
| routing_scope | PASS |
| net_completion | PASS |
| pour_integrity | PASS |

Native DRC (original rules and severities):

```json
{
 "violations_total": 0,
 "parity_total": 0,
 "new_violations": {
  "count": 0,
  "by_type": {},
  "by_severity": {}
 },
 "preexisting_violations": 0,
 "resolved_violations": 0,
 "new_parity": {
  "count": 0,
  "by_type": {},
  "by_severity": {}
 },
 "resolved_parity": 0
}
```

Unsupported or approximated by this backend:

- KRT reads only a subset of .kicad_dru custom rules; every rule is still graded by KiCad DRC
- KRT moves copper gr_text and net-less copper graphics to silkscreen (not switchable on the CLI); the copper_graphics preservation check reports it
- KRT's cleanup may neck wide new segments near foreign pads without a flag; the new_copper_netclass_geometry check reports any new copper below its net class
- backend reported: .kicad_dru: rule 'power_to_signal_clearance': clearance 0.4mm has a non-layer scope (netclass/area/... condition) -- skipped, KiCad will still enforce it

Route statistics:

```json
{
 "new_tracks": 16,
 "new_vias": 2,
 "new_track_length_mm": 115.773,
 "new_track_length_mm_by_layer": {
  "B.Cu": 24.307,
  "F.Cu": 91.466
 },
 "new_copper_nets": 4,
 "removed_existing_tracks": 0,
 "removed_existing_vias": 0
}
```
