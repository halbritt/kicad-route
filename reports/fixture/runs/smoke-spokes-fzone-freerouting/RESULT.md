# Routing attempt: freerouting on route_demo_fzone

- Backend: freerouting v2.5.0 (freerouting-2.5.0.jar b647a2f824cfbc54bc4dcaee71b03b7cd945eb76)
- Input: `route_demo.kicad_pcb` from working tree @ None (fingerprint 69730685befab820)
- Scope: all, 6 nets; protect: existing; budget 180 s
- Candidate: `runs/route_demo_fzone/smoke-spokes-fzone-freerouting/20261017T165035.629197Z/candidate/route_demo/route_demo.kicad_pcb`

| Result | Status | Detail |
|---|---|---|
| process | PASS | exit 0, 7.24 s |
| routing | INCOMPLETE | 1 unconnected in scope, 1 total (input: 5) |
| validation | FAIL | preservation zones: 2 copper zone definitions (outline, net, layers, priority, clearance, thermal and fill settings; fill polygons are regenerated) |

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
| zones | FAIL |
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

- Freerouting never rips up pre-existing copper (KiCad exports it as fixed/protected); --protect locked behaves like --protect existing
- Specctra carries per-class width/clearance/via only: custom .kicad_dru rules, diff pairs and length tuning are not exported; KiCad DRC grades them
- rule areas export only their no-tracks/no-vias restrictions; zones export as plane outlines
- route_demo.kicad_dru: net-class-to-net-class clearance rules are translated to Specctra class_class rules; any other custom rule is listed as untranslated below

Blocked by (heuristic straight/L corridors, not a proof; details `runs/route_demo_fzone/smoke-spokes-fzone-freerouting/20261017T165035.629197Z/validate/blocked-by.json`):

| Blocker | Category | Blocks (best layer) | Blocks (any layer) |
|---|---|---|---|
| keepout 'route_keepout' (no tracks, no vias, no zone fills) on B.Cu, F.Cu | keepout | 1 | 1 |
| track /SIG_A B.Cu (new, unlocked) e3b4a3ce | new_copper | 0 | 1 |
| track /SIG_A_OUT B.Cu (new, unlocked) ba2fd630 | new_copper | 0 | 1 |

- `/VIN`: 1 open; best layer F.Cu with 1 hard blockers (keepout 'route_keepout')

Route statistics:

```json
{
 "new_tracks": 14,
 "new_vias": 3,
 "new_track_length_mm": 77.963,
 "new_track_length_mm_by_layer": {
  "B.Cu": 54.546,
  "F.Cu": 23.417
 },
 "new_copper_nets": 4,
 "removed_existing_tracks": 0,
 "removed_existing_vias": 0
}
```
