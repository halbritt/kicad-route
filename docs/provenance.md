# Copper provenance: `--protect human` and the completion ratchet

Large boards are routed in steps. Each step routes from the previous step's candidate
(`run --dir <prev run>/candidate/<project>`). With `--protect existing` (the default)
every track and via on a step's input is locked, and that includes the copper earlier
kicad-route steps placed. A late step then has no room. On a real 448-net board,
finishing 16 leftover nets on such a candidate routed 0 of 16. Routing the same nets
first on the original board routed 11 of 16.

`--protect human` locks only the design's own copper. A step may then rip and reroute
copper an earlier step placed. A completion ratchet makes sure no net that was already
connected ends up open.

```sh
kicad-route run --backend krt --nets "/CAN*" --label step1               # on the original project
kicad-route run --backend krt --protect human --nets "!/CAN*" --label step2 \
    --dir build/kicad-route/runs/<project>/step1/latest/candidate/<project>
kicad-route validate <step2 run> --against <step1 run>                   # end of the chain vs the original
```

## Human and agent copper

kicad-route does not tag anything on the board. It derives provenance from the chain
of runs. The chain is the same walk that `validate --against` uses: each run's
`attempt.json` `source.source_dir` points to the candidate of the run before it. That
walk is the `resolve_origin()` helper in `cli.py`. The origin is the first run whose
input is not a kicad-route candidate.

| Class | Definition |
|---|---|
| human | every track and via in the origin run's input (`reference/`), plus every input item that cannot be traced to a step (below) |
| agent | input items that are not in the origin's input **and** that some step of the chain delivered as new copper (its graded `validate/candidate-facts.json` minus its own `facts/reference-facts.json`) |

Items are keyed exactly as `kicad/locks.py` keys them: net, layer ids, width or via
size and drill, and end points (plus the mid point for arcs). The keys are compared as
a multiset, so two identical segments count as two items. Unknown copper fails closed:

- A trace the designer edited between steps matches neither the origin nor any step's
  output. It counts as human and is locked.
- A step without `validate/candidate-facts.json` (no graded candidate) contributes no
  agent copper.
- If the input's board facts and its lock keys disagree, every input item is locked
  and the run falls back to `existing`. The same happens if the keys cannot be read.
  `provenance.json` records the reason.

When the input is not a candidate, all of its copper is human, and `--protect human`
behaves exactly like `--protect existing`. The backend flags, locks and grading are
the same. `settings.protect` records `existing` and `settings.protect_requested`
records `human`.

## What a `--protect human` run does

1. `provenance.prepare()` (in `src/kicad_route/provenance.py`) runs `locks.py keys` on
   the input and on the origin's reference board. It classifies the input copper and
   writes two files:
   - `provenance.json` in the run directory.
   - `facts/human-keys.json`, the lock allowlist (`settings.protect_only`).
2. **KRT:** the work copy goes through `locks.py lock BOARD RECORD --only
   facts/human-keys.json`, so only human items are locked. KRT runs without
   `--keep-input-copper --no-stub-layer-swap` and without `KICAD_RIP_PREEXISTING=0
   KICAD_FINALIZE_RIP=0`. That lets its rip-up and rescue paths rework agent copper.
   Afterwards the original lock flags are restored, as with `existing`.
3. **Freerouting:** the bridge (`specctra.py export|import … --only KEYS.json`) locks
   only human items for the round trip. Agent copper exports as `(type route)` and is
   rebuilt from the session. In practice Freerouting returns `route` wires as
   `(type protect)` and never rips them up. On Freerouting, `human` therefore gives no
   extra room, and the attempt's `unsupported` list says so. Use KRT for steps that
   need to reroute agent copper.

`provenance.json` holds `chain`, `origin_run`, `input_is_candidate`,
`effective_protect`, `layer_ids`, and the counts, nets and keys for `human`, `agent`
and `unattributed` (the part of `human` that no step produced). It also holds
`origin_copper_missing_from_input`, `steps_without_candidate_facts` and a one-line
`detail`.

## Grading

- **`protected_copper`:** the designated items are the locked items plus the human
  items (`provenance.designation()` maps the human keys onto the reference facts).
  Agent copper is not designated. `existing_unlocked_copper` reports how much of it was
  removed or changed. That check is informational only and always passes.
- **`routing_scope`:** new copper is also allowed on nets that carried agent copper,
  because KRT reroutes the agent nets it rips. All new copper is still checked against
  its net class (`new_copper_netclass_geometry`).
- **`validate <last> --against <first>`:** protection comes from the origin run,
  usually `existing` (all of the original input's copper). That is the human set by
  definition.

## Completion ratchet (`net_completion`)

This check applies to every protect mode and to every `validate`. Every net that was
fully connected in the graded input must still be connected in the candidate. A net
counts as connected when no unconnected item in the input's baseline DRC names it. The
check is added to the preservation checks (`validation.preservation.net_completion`,
`validate/preservation.json`). The summary goes to `validation.net_completion`:
`status`, `nets_connected_in_input` and `nets_lost`. A lost net fails validation.

The ratchet matters most for `human` and `locked` runs, where the backend may remove
copper from nets outside the scope. A step's routing status covers only its own scope,
so a net cut outside that scope would otherwise pass as COMPLETE. With `existing`, no
input copper can be removed, so the ratchet passes unless new copper breaks a zone
connection. For example, a new track can split a fill and isolate a pad, which is a
real regression.

Nets with fewer than two pads have nothing to connect and always count as connected.

## Limits

- Provenance trusts the run directories. Someone who edits `validate/candidate-facts.json`
  of an earlier step can make copper count as agent copper. Editing the board alone
  cannot.
- The fixture has room for every net, so the tests prove the locking, flags and
  grading. They do not prove that `human` routes more nets than `existing` on a
  congested board. That evidence comes from real boards.
- KRT decides on its own what to rip. Its rip-up guards still apply: small nets only,
  no plane-backed nets, nothing locked, and nothing `!`-negated in `--nets`. A net it
  rips and cannot reroute is restored by KRT, or else caught by the ratchet.
- With Freerouting, agent copper makes a Specctra round trip and may come back
  re-normalised. On the fixture it came back with 0 changed items.

## Tests

`tests/test_provenance.py` covers these cases on the fixture, using `unlocked_dir`, a
variant with `/SIG_B` unlocked so that only provenance can protect it:

- `--protect human` on a non-candidate input is the same as `existing`: settings, KRT
  argv and env, locks and designation.
- A two-step chain locks exactly the 7 `/SIG_B` items, classes step 1's `/SIG_A*`
  copper as agent, drops KRT's keep-input switches, designates 5 tracks and 2 vias,
  and passes `validate --against` the origin.
- The Freerouting bridge exports agent copper as `(type route)`.
- A track edited between steps counts as human (fail closed).
- Cutting an agent-routed net outside the step's scope fails `net_completion`, while
  `protected_copper` passes and routing stays COMPLETE.
