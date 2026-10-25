# Four single-ended length paths

This native software qualification article has four fixed two-terminal lanes
on a 70 × 50 mm two-layer board. The original direct distances are 50, 48, 46
and 44 mm. The authored rules require 0.25 mm minimum track width and 0.1 mm
group skew. Default-class clearance is 0.2 mm. These are agent-selected
qualification constraints, not a circuit or propagation-time specification.

```sh
bin/kicad-route run --backend builtin --dir fixtures/group_matching \
  --diff-pairs auto --length-match-group DQ0 DQ1 DQ2 DQ3 \
  --group-match-tolerance .1 --group-meander-amplitude 3 \
  --grid-step .1 --budget 180
```

The native project and its local stock footprint dependencies are captured.
`tests/test_length_groups.py` exercises ordinary routing, insufficient meander
room, ignored required checks, overlapping groups and mixed pair routing.
Its connected-loop negative passes the original whole-net DRC rule while
failing the independent terminal-to-terminal path rule. This is not a
manufacturing release or electrical signal-integrity qualification.
