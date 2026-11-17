# Engine requirements and acceptance

The current default backend is controlled by Python and uses Rust search kernels.
The standalone `rust-engine/` candidate is an incomplete replacement. A successful
upstream import or equivalent board output does not establish the requested Rust
ownership boundary or a complete routing outcome.

[`routing-requirements.json`](routing-requirements.json) records the current
requirements, owner-relayed source records, independent findings, withdrawn
claims, action owners and remaining gaps. The original earlier all-Rust message
has not been recovered; the current explicit instruction is recorded separately.
Do not silently turn an agent summary into a historical user instruction.

[`engine-ownership.json`](engine-ownership.json) maps state, geometry/legality,
search, orchestration, rip/restore and output to actual source paths and symbols.
It includes the specifically allowed Python native-I/O adapter. These are
reviewable responsibility declarations, not a call graph inferred from filenames.
Review changes to the declarations together with the affected implementation.

## Separate the three conclusions

1. **Equivalence:** the retained comparison behavior matches the selected baseline.
   Identical incomplete results can satisfy this claim.
2. **Outcome fitness:** all candidate runs, including warmup, must be complete with
   zero opens and pass the existing native, preservation and resource gates. Use
   `scripts/compare_router.py --claim outcome-fitness` for this claim. The default
   comparison retains its historical meaning; its result also exposes fitness
   separately. Neither mode authorizes a new benchmark campaign.
3. **Architecture:** ownership meets the source requirement and permitted seam.
   Board quality and equivalent outputs do not establish this. Source inventory,
   a guarded runtime probe and independent exact-revision review are separate
   pieces of evidence; none alone proves complete architecture acceptance.

## Existing host checks

Run `mise run test:architecture`. It uses the existing focused pytest and host
process boundary, preserving the normal home, credentials, services and tools.
It checks the actual ownership inventory, positive/negative fitness cases, and
the native Rust fixture through the real export, engine and apply CLI commands.

The runtime fixture installs a disposable Python audit hook through `PYTHONPATH`.
It allows the exact `tools/rust_engine_kicad.py` exception and refuses execution
of other Python files in the selected repository. Both legacy routing entry
points are executed with `--help` as negative controls: the hook must reject them
before they can run. The positive path must execute the hook, export a native
board, run the actual Rust executable, apply its edits, preserve input/copper,
close the native open and introduce no DRC violations. A missing hook, legacy
execution, process failure or missing result fails the test. Rust/native failure
and refusal cases remain in the same suite.

Native application also enforces its result outside tests: it reloads the staged
board, requires zero native opens, compares native DRC against the input, and
rejects added widths below the native minimum. Only then does it publish a new
destination. False-complete, shorting, undersized and unavailable-native-tool
controls must leave no published candidate. This does not turn the incomplete
Rust engine into a production backend.
The operation pins one private source snapshot and requests all native track
errors. Source mutation, input-directory preservation, destination races and DRC
timeout controls complement the ordinary positive and negative routing cases.

The resource-limit fixture passes an optional `limits` JSON object through the
native export CLI with `--limits`. Rust owns its field validation, defaults and
ceilings. Existing inputs without it retain defaults. Engine results now require
both `expanded_states` and `work_used` as unsigned 64-bit integers; old complete
plans missing either counter must be regenerated. These counters describe the
engine result and do not replace native validation.

Actual executable controls exhaust work, item/search admission, undo history
and nesting depth separately. Each must return its typed failure, empty edits
and a work counter, and the native apply command must refuse publication while
preserving the source. Expansion exhaustion remains a separate outcome. Rust
tests exercise shared outer-transaction work, nested errors and panic recovery,
exact budget boundaries, and journal/allocator/index restoration after failure.
The [accounting inventory](../rust-engine/README.md#accounting-inventory) states
what is charged and what is separately bounded or excluded. Entry limits are
not a process-memory cap; this contract does not interrupt arbitrary caller
closures, native tools or the process at a wall-clock deadline. Full cancellation
and complete-engine acceptance remain open.

Inspect an inventory directly with:

```sh
python3 scripts/check_engine_ownership.py --implementation current-production
python3 scripts/check_engine_ownership.py --implementation rust-candidate
```

The first command exits 1 because five lifecycle responsibilities are Python.
The second can pass the source inventory while reporting runtime boundary,
architecture acceptance and outcome fitness as `NOT_EVALUATED`. Reports include
source hashes so the next reviewer can identify changed inputs. Missing files,
symbols, responsibility rows or source references fail instead of being omitted.

The runtime check is bounded regression evidence, not a hostile-code sandbox or
a proof that all possible paths were exercised. Python policy hidden inside the
allowed adapter, code relocated outside the repository, interpreter flags that
disable startup hooks, and changed unexecuted branches require source review and
additional controls. The fixture checks its hook is active; it does not certify
every invocation a future caller could choose. The declaration checker cannot
prove that a named function performs the declared responsibility. Independent
review must trace the actual entry points and affected failure paths.

## Review and handoff

An owner rearchitecture request or two attempts leaving the same required outcome
unmet without new supporting evidence triggers independent end-to-end review
before accepting that outcome. Keep implementation running within authorization.
Record delivery, review acceptance and implemented correction as distinct states.
For each unresolved finding retain an owner, next action, observation deadline,
exact revision and evidence. At a missed deadline, inspect actual delivery/work
state once and report the stalled item; do not silently extend or substitute an
acknowledgment for completion.

Carry source requirements, explicit exceptions, disputes, failed approaches,
unverified claims and pending actions through handoffs. Imports and future
language changes must rerun affected checks and receive fresh review. The current
Fable/Carbon assignments and milestone gates are in the requirement catalog.
No check here changes production routing defaults, deployment permissions or
existing benchmark and project holds.
