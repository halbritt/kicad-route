# Routing attempt: krt on route_demo

- Backend: krt 0.23.1 (v0.23.1)
- Input: `route_demo.kicad_pcb` from working tree @ None (fingerprint ba2f343c49e2f971)
- Scope: selected, 2 nets; protect: existing; budget 180 s
- Candidate: `runs/route_demo/floor-preflight/20261017T163959.072371Z/candidate/route_demo/route_demo.kicad_pcb`

| Result | Status | Detail |
|---|---|---|
| process | PASS | exit 0, 3.781 s |
| routing | COMPLETE | 0 unconnected in scope, 4 total (input: 6) |
| validation | FAIL | input rules below the declared rule floor (1): rules.min_clearance 0.15 is below the declared floor 0.2 (owner hw) |

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
| new_copper_netclass_geometry | PASS |
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
 "new_tracks": 5,
 "new_vias": 0,
 "new_track_length_mm": 46.91,
 "new_track_length_mm_by_layer": {
  "F.Cu": 46.91
 },
 "new_copper_nets": 2,
 "removed_existing_tracks": 0,
 "removed_existing_vias": 0
}
```
