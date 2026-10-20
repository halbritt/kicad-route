# Routing attempt: krt on diff-pair

- Backend: krt 0.23.1 (owned-c2ac657d5617)
- Input: `pair.kicad_pcb` from working tree @ f0c09ac3b8d39a6674a734d3caaa9d7e3ae9633d (fingerprint a7d83a96e7699b7c)
- Scope: all, 3 nets; protect: existing; budget 180 s
- Candidate: `build/agent497-qualification/diff-pair-env-reuse/reference-3/run/candidate/diff-pair/pair.kicad_pcb`

| Result | Status | Detail |
|---|---|---|
| process | PASS | exit 0, 8.433 s |
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

Differential pairs (lengths and skew in mm; native planar copper):

| P | N | Status | Width / gap | P length | N length | Skew | Gap violations |
|---|---|---|---|---|---|---|---|
| /CAN+ | /CAN- | ROUTED | 0.25 / 0.35 | 29.8125 | 29.8125 | 0.0 | 0 |

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

Route statistics:

```json
{
 "new_tracks": 3,
 "new_vias": 0,
 "new_track_length_mm": 89.625,
 "new_track_length_mm_by_layer": {
  "F.Cu": 89.625
 },
 "new_copper_nets": 3,
 "removed_existing_tracks": 0,
 "removed_existing_vias": 0
}
```
