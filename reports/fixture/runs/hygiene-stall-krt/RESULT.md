# Routing attempt: krt on route_demo

- Backend: krt 0.23.1 (v0.23.1)
- Input: `route_demo.kicad_pcb` from working tree @ None (fingerprint ac02d10cd05dec4a)
- Scope: all, 6 nets; protect: existing; budget 240 s
- Candidate: `runs/route_demo/hygiene-stall-krt/20261018T035312.106302Z/candidate/route_demo/route_demo.kicad_pcb`

| Result | Status | Detail |
|---|---|---|
| process | PASS | exit 0, 15.586 s; KRT stalled in routing single-ended net(s) at nets 5 for 6 s: stopped early and wrote the board as it stood |
| routing | INCOMPLETE | 7 unconnected in scope, 7 total (input: 7) |
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

Blocked by (heuristic straight/L corridors, not a proof; details `runs/route_demo/hygiene-stall-krt/20261018T035312.106302Z/validate/blocked-by.json`):

| Blocker | Category | Blocks (best layer) | Blocks (any layer) |
|---|---|---|---|
| keepout 'wall' (no tracks, no vias, no zone fills) on B.Cu, F.Cu | keepout | 4 | 4 |
| keepout 'route_keepout' (no tracks, no vias, no zone fills) on B.Cu, F.Cu | keepout | 3 | 4 |

- `/SIG_A`: 1 open; best layer F.Cu with 1 hard blockers (keepout 'route_keepout')
- `/SIG_A_OUT`: 1 open; best layer F.Cu with 2 hard blockers (keepout 'route_keepout', keepout 'wall')
- `/SIG_B_OUT`: 1 open; best layer F.Cu with 1 hard blockers (keepout 'wall')
- `/VIN`: 2 open; best layer B.Cu with 1 hard blockers (keepout 'wall')
- `GND`: 2 open; best layer F.Cu with 2 hard blockers (keepout 'route_keepout', keepout 'wall')
- 2 connection(s) have an unobstructed corridor: the obstacle is elsewhere (congestion, detours, the backend's search or budget)

Route statistics:

```json
{
 "new_tracks": 0,
 "new_vias": 0,
 "new_track_length_mm": 0,
 "new_track_length_mm_by_layer": {},
 "new_copper_nets": 0,
 "removed_existing_tracks": 0,
 "removed_existing_vias": 0
}
```
