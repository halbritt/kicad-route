# Routing attempt: freerouting on route_demo

- Backend: freerouting v2.5.0 (freerouting-2.5.0.jar b647a2f824cfbc54bc4dcaee71b03b7cd945eb76)
- Input: `route_demo.kicad_pcb` from working tree @ c45a2db29e2dacfcdc64513fd4f79e5c5257f6d9 (fingerprint 4f765361ce671bde)
- Scope: selected, 4 nets; protect: human; budget 180 s
- Candidate: `runs/route_demo/prov-step2-freerouting/20261018T103820.596247Z/candidate/route_demo/route_demo.kicad_pcb`

| Result | Status | Detail |
|---|---|---|
| process | PASS | exit 0, 19.518 s |
| routing | INCOMPLETE | 1 unconnected in scope, 1 total (input: 4) |
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
| existing_unlocked_copper | PASS |
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
- --protect human: agent copper is exported unlocked ((type route)) and rebuilt from the session, but Freerouting returns it as (type protect) and never rips it up; use KRT to reroute agent copper
- route_demo.kicad_dru: net-class-to-net-class clearance rules are translated to Specctra class_class rules; any other custom rule is listed as untranslated below

Blocked by (heuristic straight/L corridors, not a proof; details `runs/route_demo/prov-step2-freerouting/20261018T103820.596247Z/validate/blocked-by.json`):

| Blocker | Category | Blocks (best layer) | Blocks (any layer) |
|---|---|---|---|
| keepout 'route_keepout' (no tracks, no vias, no zone fills) on B.Cu, F.Cu | keepout | 0 | 1 |
| track /SIG_A F.Cu (input, unlocked) ba1f6156 | input_copper | 0 | 1 |
| track /SIG_A_OUT F.Cu (input, unlocked) e5f15cca | input_copper | 0 | 1 |

- `/VIN`: 1 open; best layer B.Cu with 0 hard blockers
- 1 connection(s) have an unobstructed corridor: the obstacle is elsewhere (congestion, detours, the backend's search or budget)

Route statistics:

```json
{
 "new_tracks": 6,
 "new_vias": 1,
 "new_track_length_mm": 32.549,
 "new_track_length_mm_by_layer": {
  "F.Cu": 32.549
 },
 "new_copper_nets": 3,
 "removed_existing_tracks": 0,
 "removed_existing_vias": 0
}
```
