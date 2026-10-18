# Routing attempt: krt on route_demo

- Backend: krt 0.23.1 (v0.23.1)
- Input: `route_demo.kicad_pcb` from working tree @ None (fingerprint ac02d10cd05dec4a)
- Scope: all, 6 nets; protect: existing; budget 180 s
- Candidate: `runs/route_demo/smoke-blocked-krt/20261018T044326.034754Z/candidate/route_demo/route_demo.kicad_pcb`

| Result | Status | Detail |
|---|---|---|
| process | PASS | exit 0, 82.284 s |
| routing | INCOMPLETE | 4 unconnected in scope, 4 total (input: 7) |
| validation | PASS | no new findings |

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

Blocked by (heuristic straight/L corridors, not a proof; details `runs/route_demo/smoke-blocked-krt/20261018T044326.034754Z/validate/blocked-by.json`):

| Blocker | Category | Blocks (best layer) | Blocks (any layer) |
|---|---|---|---|
| keepout 'wall' (no tracks, no vias, no zone fills) on B.Cu, F.Cu | keepout | 4 | 4 |
| keepout 'route_keepout' (no tracks, no vias, no zone fills) on B.Cu, F.Cu | keepout | 3 | 3 |
| track /SIG_A F.Cu (new, unlocked) ab49f926 | new_copper | 0 | 2 |

- `/SIG_A_OUT`: 1 open; best layer F.Cu with 2 hard blockers (keepout 'route_keepout', keepout 'wall')
- `/SIG_B_OUT`: 1 open; best layer F.Cu with 1 hard blockers (keepout 'wall')
- `/VIN`: 1 open; best layer B.Cu with 2 hard blockers (keepout 'route_keepout', keepout 'wall')
- `GND`: 1 open; best layer B.Cu with 2 hard blockers (keepout 'route_keepout', keepout 'wall')

Route statistics:

```json
{
 "new_tracks": 7,
 "new_vias": 1,
 "new_track_length_mm": 29.412,
 "new_track_length_mm_by_layer": {
  "B.Cu": 0.028,
  "F.Cu": 29.384
 },
 "new_copper_nets": 3,
 "removed_existing_tracks": 0,
 "removed_existing_vias": 0
}
```
