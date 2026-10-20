# Routing attempt: krt on multipoint-tap

- Backend: krt 0.23.1 (owned-c6c09054f684)
- Input: `multipoint-tap.kicad_pcb` from working tree @ f0c09ac3b8d39a6674a734d3caaa9d7e3ae9633d (fingerprint ab0e09d48dc649a0)
- Scope: all, 1 nets; protect: existing; budget 180 s
- Candidate: `build/agent497-qualification/multipoint-cpu-rate/candidate-1/run/candidate/multipoint-tap/multipoint-tap.kicad_pcb`

| Result | Status | Detail |
|---|---|---|
| process | PASS | exit 0, 5.613 s |
| routing | COMPLETE | 0 unconnected in scope, 0 total (input: 3) |
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
 "violations_total": 5,
 "parity_total": 0,
 "new_violations": {
  "count": 0,
  "by_type": {},
  "by_severity": {}
 },
 "preexisting_violations": 5,
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

- KRT's cleanup may neck wide new segments near foreign pads without a flag; the new_copper_netclass_geometry check reports any new copper below its net class

Route statistics:

```json
{
 "new_tracks": 4,
 "new_vias": 0,
 "new_track_length_mm": 6.933,
 "new_track_length_mm_by_layer": {
  "F.Cu": 6.933
 },
 "new_copper_nets": 1,
 "removed_existing_tracks": 0,
 "removed_existing_vias": 0
}
```
