# Nested routing diagnostics

With `KICAD_NET_STORY=1`, the outer `batch_route` call writes one
`<output_basename>_netstory.json` when it exits. Nested plane-repair calls join
that run, including calls loaded through a separate `route` module from the
CLI's `__main__`. They do not publish competing files. A standalone
`dump_net_story(state, path)` still writes the legacy per-state document.

The existing `board`, `nets`, `bus_groups` and `phase3_order` fields remain.
Events keep `seq`, `event` and `details` (including existing outcome and CPU
fields). Each event adds `call_id` and a run-wide `order`; `seq` is local to its
routing state and can repeat. Count attempted work from `nets[*].events` once.
Events are copied at recording time, so restoring geometry or its local
history cannot erase an attempted, rejected or cancelled operation.

`calls` lists every invocation, including one that returns before creating a
state. Its IDs are unique within this document, starting with `root_call_id: 0`.
Each entry includes `parent_call_id`, `children`, input/output paths,
`event_count`, `state_available`, and its own final net/bus/order snapshot.
Snapshots omit events to avoid counting them twice. Top-level net results and
bus groups prefer the outer call; child-only nets/groups are included, and
`result_call_id` identifies the chosen net snapshot. Child result snapshots
remain separately accessible. Results describe that call's state at exit, not
all the attempted copper in the event history.

`completion: returned` describes control flow, including a normal budget-stop
return; it is not routing success. `completion: raised` includes the exception
type. Both paths publish recorded history when Python unwinds. Snapshot errors
set `diagnostics_complete: false` and appear in `diagnostic_errors`; recorded
events remain. This flag measures diagnostic collection, not board correctness.
Write errors print a diagnostic failure and remove a stale destination when
possible; they do not replace an existing routing exception. Publication uses
the existing atomic JSON writer.

Ownership covers synchronous nested calls in one process. Separate processes
write separate stories, as before. A hard kill cannot run the exit writer, so
this is not a crash journal. Completed children retain only detached diagnostic
data until the outer call exits, not their mutable board states. No benchmark
or fabrication claim follows from a complete diagnostic history.
