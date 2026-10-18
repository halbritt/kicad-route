---
name: kicad-route
description: "Use for KiCad PCB trace routing with kicad-route: run KRT or Freerouting on an isolated copy of a project, read the structured result (process / routing / validation), plan the next attempt without relaxing the design's constraints, or audit a committed board change against its parent."
---

# kicad-route

`kicad-route` routes an isolated copy of a KiCad project with one of two backends
and grades the result with KiCad's own checks:
- **KRT** (KiCadRoutingTools) reads and writes the native KiCad files.
- **Freerouting** goes through KiCad's Specctra DSN/SES bridge.

The source checkout is `~/git/kicad-route`. Full reference, install and update notes,
and the decision record are in `docs/routing.md` there. A project may wrap these
commands in make targets and add its own project skill; when it does, use those
first.

## Ground rules

- **Never route the real project in place.**
  - Every attempt copies the project into `OUT/runs/<project>/<label>/<stamp>/` and
    routes the copy.
  - `OUT` defaults to `./build/kicad-route`; set it with `--out` (before or after the
    subcommand) or `$KICAD_ROUTE_OUT`.
  - `--run-dir PATH` on `run` picks the run directory yourself (it must not exist).
  - Native KiCad files stay authoritative. A candidate becomes design work only when a
    person adopts it.
- **The input's constraints are fixed.**
  - Never lower clearances, narrow net-class widths or vias, add layers, change
    connectivity, move footprints, edit zones or keepouts, or relax DRC severities to
    make an attempt pass.
  - Tightening is fine, for example `--default-clearance`.
- **The verdict is the run's `result.json`.**
  - It comes from KiCad's own DRC: zones refilled, schematic parity, the input's own
    `.kicad_pro`/`.kicad_dru`.
  - Preservation checks are added on top.
  - Backend self-reports (KRT `JSON_SUMMARY`, Freerouting's result JSON) are
    diagnostics only.

## Commands (run from the project directory, or pass `--dir`)

| Command | What it does |
|---|---|
| `kicad-route doctor` | Readiness, versions and capabilities of both backends (`OUT/doctor/doctor.json`) |
| `kicad-route doctor --json --offline [--no-canary]` | The same record on stdout (schema `kicad-route/doctor/1`, logs on stderr), with no network; its `sandbox` block lists the read-only mounts, writable OUT, environment and network a sandbox must provide (`docs/sandbox.md`) |
| `kicad-route run --backend krt\|freerouting [options]` | One attempt; prints the run directory and exits with its verdict |
| `kicad-route validate <run_dir>` | Re-grade a candidate (e.g. after hand edits) into `<run_dir>/revalidate/<stamp>/` |
| `kicad-route validate <last> --against <first>` | Grade the end of a `--dir` chain against the first run's input (`revalidate-origin/<stamp>/`) |
| `kicad-route campaign --backend krt\|freerouting [run options] [--rounds N] [--race K] [--total-budget S]` | Hard nets first, automated: round 0 over the scope, then the backlog of unrouted nets first on the original input and the rest from that candidate; every chain graded against the input; prints the best run (`OUT/campaigns/…/campaign.json`, `docs/campaign.md`). `--total-budget` cuts each run's `--budget` to the time left and stops a step still running past its grace (`timeouts` in campaign.json) |
| `kicad-route blocked <run_dir>` | (Re)compute the blocked-by receipts of an incomplete run or re-grade (`validate/blocked-by.json`) |
| `kicad-route blocked --board B.kicad_pcb [--json]` | The same receipts for a placed, unrouted or partly routed board, with no routing run (placement evidence; the board is not written) |
| `kicad-route compare <run_dir> …` | Side-by-side table; refuses to call different inputs comparable |
| `kicad-route audit --old REV [--new REV]` | Grade a committed board change against its parent: DRC, parity, rule changes and what they hide, design edits, the rule floor |
| `kicad-route floor [--write-declared]` | Rules looser than their git high-water mark or the declared floor `<board>.rules-floor.json` (`OUT/floor/<project>/floor.json`) |
| `kicad-route publish <run_dir> [--to DIR]` | Copy the attempt's structured receipts (every JSON receipt plus `RESULT.md`: validation, spokes, blocked-by, pour integrity, provenance, facts, latest re-validation; never a board, log or `work/`) to `<DIR>/<project>/<label>/` for committing |
| `kicad-route update` | Move both backends to their latest upstream release, write `STATE/manifest.json`, regenerate the `krt-*` skills, run `smoke`, the corpus gate and the KRT repeat check |
| `kicad-route state-manifest` | Record what is installed under STATE (versions, URLs, sha256 of the jar, the KRT extension module, the venv packages, kicad-cli) in `STATE/manifest.json`; downloads nothing. `doctor` fails when the files drift from it |
| `kicad-route determinism [--backend krt] [--runs N] [--record]` | Route the fixture (or `--dir`) N times, ledger off, and compare copper geometry, candidate bytes and outcome; `--record` stores the verdict in the state manifest (exit 0 reproducible, 3 not) |
| `kicad-route smoke` | Integration tests on the bundled fixture board |
| `kicad-route canary [--backend B]` | Awkward net names (`Net-(BT1-+)`, `/a b/c`, `-VIN(1)`, ...) through each backend's round trip on the fixture |
| `kicad-route corpus add\|check\|list` | Real-board regression gate: snapshots with baseline outcomes, re-run after backend updates (`update` runs `check`) |

`run` options (`campaign` passes every one of them to each step, and `corpus add`
records them; one shared definition):

- **Input:**
  - `--dir DIR` (default `.`), `--board B.kicad_pcb` (default: the only board),
    `--schematic`, `--name`.
  - `--rev REV` routes a committed revision through `git archive`. It's recommended
    while others edit the tree. It also takes the `${KIPRJMOD}/../<dir>` libraries
    that live in the same repository, and checked-out submodules at their pinned
    commits. A `../<dir>` outside the repository is read as it is now; check
    `snapshot.json` `library_links` (`pinned: false`, `library_head`) and
    `library_issues`.
  - `--library-dir NAME=PATH` maps an empty library submodule to a path.
- **Scope:**
  - `--nets PAT …` selects by **net name**: fnmatch patterns, `!PAT` excludes, and
    exclusions alone start from all nets. A name that starts with `!` takes an
    escape: `--nets '\!RESET'` routes the net `!RESET`, `'!\!RESET'` excludes it.
    A bare `--nets '!RESET'` on a board with the net `!RESET` (and no `RESET`) is
    refused (exit 4) rather than routing every other net.
  - `--netclass CLASS …` selects by **effective KiCad net class**.
  - Default: all nets.
- **Budget:**
  - `--budget SECONDS` is wall clock (default 300).
  - `--max-iterations N` (KRT); `--passes N` (Freerouting).
- **Protection:**
  - `--protect existing` (default): all input copper must survive.
  - `--protect locked`: only locked copper must survive, and KRT may rip up unlocked
    copper.
  - `--protect human`: in a `--dir` chain, only the designer's copper (the first run's
    input) is protected; copper earlier kicad-route steps placed may be ripped up and
    rerouted by KRT. On a non-candidate input it equals `existing` (docs/provenance.md).
- **Thermal spokes:** `--reserve-spokes [N]` keeps new tracks and vias out of N
  spoke corridors per zone-connected pad while routing; the corridors are removed
  before grading (docs/spokes.md). `kicad-route spokes <run>` names the new copper
  that starved each pad. `kicad-route spokes --board B --verify` checks, with KiCad's
  own zone filler, that the board's corridors really protect their spokes (exit 3 if
  one does not; then the reservation cannot be trusted on that board).
- **Decoupling cells (read-only):** `kicad-route decoupling --board B.kicad_pcb` or
  `kicad-route decoupling <run>` reports per IC power/ground pin pair and bypass
  capacitor whether the copper runs pin → capacitor → plane or the pin has its own
  plane via (`bypassed`); for a run it lists cells the candidate changed
  (docs/decoupling.md). Advisory: it never changes a verdict. Exit 1 means the
  analysis failed (`decoupling.json` holds `error`); a bad board or run is refused
  (exit 4).
- **Collision barriers:** `--barrier-from <run>` routes the same input again with a
  small no-track/no-via keepout where that run's new copper collided (its new
  `clearance`, `tracks_crossing`, `shorting_items`, `hole_clearance` or
  `copper_edge_clearance` findings); the keepouts are removed before grading
  (docs/barrier.md). `kicad-route barrier <run>` previews them (exit 4 when the run
  has nothing to barrier, 1 when no barrier could be placed).
- **Tightening:**
  - `--default-clearance MM` (Default net class, both backends).
  - `--clearance-margin MM` (Freerouting only).
  - `--pour-area-loss PCT` and `--pour-min-spokes N` make `pour_integrity` stricter
    (also on `validate`; see `docs/pour.md`).
- **Pass-through:**
  - `--backend-arg ARG` (repeat) and `--backend-env KEY=VALUE` (for example KRT
    `KICAD_*` knobs).
  - Both are recorded in the result; grading is unaffected.
- `--label NAME` names the run directory; `--run-dir PATH` uses exactly `PATH`
  (must not exist; no `latest` link).
- **Machine mode:** `--json` (on `run`, `validate`, `campaign`, `blocked`, `audit`,
  `floor`, `doctor`, `decoupling`, `barrier`) prints exactly one JSON record on
  stdout (the `result.json` content, or a refusal) and sends progress to stderr. Read that instead of the
  progress lines. Record kinds, schema ids and stable fields: `docs/interface.md`.
- Spell options in full: abbreviations such as `--budg` are refused.
- **Hygiene** (docs/hygiene.md):
  - `--stall SECONDS` stops the backend when its log shows no progress for that
    long. The reason then reads `stalled in <phase> at <metric> for N s`;
    `process.last_phase` and `work/heartbeat.jsonl` show where it was.
  - The run ledger (`OUT/ledger.jsonl`) refuses an exact repeat of an attempt
    that already failed (exit 4, naming the earlier run). Change something, or
    pass `--force`.
  - `--canary` runs the awkward-net-name canary first.
- **Sandbox** (docs/sandbox.md):
  - `--require-readonly-state` (or `KICAD_ROUTE_REQUIRE_READONLY_STATE=1`) refuses
    with exit 4 (`reason_class: state`) before routing when the state directory
    (`$KICAD_ROUTE_STATE`) is missing, holds no backend, is not the directory its
    `manifest.json` describes (or has drifted from it), or is writable. `update` and
    `corpus add` refuse under it.
  - `--ledger PATH` keeps the run ledger outside OUT, so repeat refusal survives a
    fresh OUT per stage.
  - Both are environment options: `campaign` passes them to every step, a corpus
    entry never records them, and `corpus check` takes its own.
  - `result.json` `sandbox` records the network namespace (isolated or not), the
    state directory's writability and the flag.

## Reading a result

`result.json` reports three independent statuses:

| Field | Values | Meaning |
|---|---|---|
| `process.status` | PASS / FAIL | backend ran and handed back a board (`process.reasons` says why not) |
| `routing.status` | COMPLETE / INCOMPLETE / NOT_RUN | KiCad's unconnected items on in-scope nets after refill |
| `validation.status` | PASS / FAIL / NOT_RUN | no new DRC violations or parity issues vs the input, and every preservation check PASS |

Exit codes:
- 0: complete and valid.
- 1: process failure (no candidate; `validate` of a run without a candidate board too).
- 2: valid but incomplete.
- 3: fails validation.
- 4: refused before routing. No run directory is left; the reason is in
  `OUT/refusals/refusal-latest.json` (`reason_class`: `usage` for a bad command line,
  `input` for a missing directory, no or several boards, a bad `--rev`, an unreadable
  board, a net selection matching nothing, or a board with no `.kicad_pro` of its own
  name (KiCad would load none of its rules; `audit` refuses the same way), `ledger`
  for an identical attempt that already failed, `canary` for a failed `--canary`,
  `state` for an existing `--run-dir`, or under `--require-readonly-state` a state
  directory that is writable, missing or not the one its manifest describes). Fix the command or the input; a refusal is
  never a routing verdict.

Provenance, for recording a run as evidence:

- `evidence`: sha256 of the input board, every reference file, the rules files, the
  declared rule floor, the backend's output board, the delivered candidate, and the
  grading records; `candidate_copper_signature` is a UUID- and order-free digest of
  the candidate's copper. Compare candidates by that signature, not by file hash:
  KiCad writes fresh UUIDs on every save.
- `tool_identity`: kicad-route, kicad-cli (version, sha256), pcbnew, Python, the
  backend build (KRT commit and extension-module sha256; Freerouting jar sha256 and
  Java), and the state manifest's sha256 with any drift from it.
- `backend.reproducible` and `measurement`: Freerouting results are attempts
  (`reproducible: false`, `measurement: false`); a KRT result is a measurement only
  when the state manifest holds a passing repeat check for the build on disk.

Also read:

- **New findings:** `validation.reasons`, `validation.drc.new_violations.by_type`,
  and `validate/new-findings.json` (each new violation with positions). Pre-existing
  input violations are counted separately and never fail an attempt.
- **Preservation:** `validation.preservation` covers `project_files`,
  `board_outline`, `layers`, `stackup`, `board_setup`, `nets_and_netclasses`,
  `pad_connectivity`, `footprints`, `zones`, `keepouts`, `copper_graphics`,
  `protected_copper`, `new_copper_netclass_geometry`, `routing_scope`,
  `net_completion` (no net that was connected in the input may be disconnected) and
  `pour_integrity` (pad fill contact, thermal spokes, islands; `validate/pour-integrity.json`). Details are
  in `validate/preservation.json`. `protected_copper` also fails when a locked track
  or via comes back unlocked; `board_setup` and `stackup` fail when a board's
  `(setup)` block cannot be read (fail closed); a `footprints` difference lists the
  changed attributes under `changed`.
- **Routing details:** `routing.unrouted_nets` and `routing.stats` (new tracks,
  vias, length).
- **What blocks the open connections:** `routing.blocked_by` (when INCOMPLETE). It
  lists the top blockers by how many open connections they block, by category:
  `keepout`, `input_copper`, `new_copper`, `footprint` and `board_edge`. It also gives
  each net's least obstructed layer. Details are in `validate/blocked-by.json`
  (schema `kicad-route/blocked/2`, `schemas/blocked-by.schema.json`: endpoints name
  footprint and pad, `footprints` says which connections each footprint blocks or
  terminates). It is a straight/L-corridor heuristic (`heuristic: true`): use it as a
  pointer, not a proof (`docs/blocked.md`).
- **`unsupported`:** constraints the backend could not see. KiCad still grades them.
- **Backend reports:** `process.backend_report` holds KRT's relaxations ledger and
  Freerouting's incomplete count.
- **The candidate:** `candidate/<project>/<board>.kicad_pcb` opens in KiCad. Its zones
  are refilled, and it is exactly the file that was graded.

## Planning the next attempt

| Evidence | Next attempt |
|---|---|
| new `clearance` violations naming a `.kicad_dru` rule, rule listed in `unsupported` | `--default-clearance <rule value>`. For Freerouting, class-to-class rules are already translated; add `--clearance-margin 0.01` for micrometre misses |
| `routing INCOMPLETE`, `unrouted_nets` few | larger `--budget`, or Freerouting `--passes 100`. Route the hard nets first with `--nets` **on the original input**, then continue from that candidate with `--dir <run>/candidate/<project>`. Finishing them *after* everything else rarely works because existing copper is locked. Grade the end with `validate <last> --against <first>`. `kicad-route campaign` does all of this |
| `blocked_by` top blockers are `new_copper` | the backend's own copper is in the way: route the blocked nets first (`--nets`) on the original input, then the rest |
| `blocked_by` top blockers are `input_copper` | existing copper is in the way. If it is unlocked, `--protect locked` lets KRT move it. If it is locked, ask the owner; never unlock it yourself |
| `blocked_by` top blocker is a `keepout` on every layer | the keepout separates the endpoints. Report it as a design issue; never edit the keepout |
| `process FAIL` "exceeded the … budget" (KRT) | larger `--budget`, or `--backend-env KICAD_NET_RESCUE=0` to skip the long rescue search. If plane nets are what runs long, use `--backend-env KICAD_PLANE_FINALIZE=0` and plan the planes separately |
| Freerouting INCOMPLETE at SMD pads or plane connections | `--backend-arg=--router.fanout.enabled=true`. Fanout is off by default because it can neck traces to 3/4–1/2 width; `new_copper_netclass_geometry` catches that if it happens |
| `via_dangling` / `track_dangling` new | Freerouting left fanout stubs on a net it could not finish. Treat it as incomplete routing, and retry as above or with the other backend |
| a few new `clearance` / `tracks_crossing` / `shorting_items` findings at single spots | when tightening the whole net class (rows above) is too broad: rewind with `run ... --barrier-from <run>` (same input and scope), which puts a keepout where the new copper ran. Check `process.barrier.outcome`; a barrier can move the problem elsewhere |
| `starved_thermal` new | new tracks crowd the thermal spokes of a zone-connected pad. Retry with `--reserve-spokes`; `kicad-route spokes <run>` names the cutting tracks. A stitching via is a design call for a person; never lower `min_resolved_spokes` |
| `net_completion` FAIL | the candidate disconnected a net that was connected in its input (with `--protect human`, KRT ripped agent copper and did not finish it). Do not adopt; route that net first or use `--protect existing` |
| finishing leftovers on a candidate routes nothing (all copper locked) | `--protect human` lets KRT reroute copper earlier steps placed, or let `kicad-route campaign` reorder the work |
| `pour_integrity` FAIL | new copper damaged a pour: `isolated_island` (a pad cut off from its net's main pour), `lost_fill_contact`, `lost_pad_reach` (the zone no longer reaches the pad at all), `spokes_below_minimum`, or `area_loss` (only with `--pour-area-loss`). Read `validate/pour-integrity.json` for the pads and islands. Retry with the cutting nets kept off that layer near the pads (another backend, or a `--nets` scope that routes them separately); a stitching via is a design call for a person. Never lower `min_resolved_spokes`. `--pour-min-spokes N` and `--pour-area-loss PCT` only tighten |
| `protected_copper` / `footprints` / `zones` FAIL | backend or bridge defect. Report it with the run directory; do not adopt the candidate |
| `new_copper_netclass_geometry` FAIL | the backend narrowed a conductor below its net class. Do not adopt; report the KRT/Freerouting version |
| `process FAIL` "stalled in <phase> at <metric>" | the backend stopped making progress there. A rescue search on walled-off nets: `--backend-env KICAD_NET_RESCUE=0` or a different scope. A silent KiCad leg (refill, oracle): a larger `--stall`. KRT honours the stop file only in its main pass; the kill reason names the knob for the pass that ignored it (docs/hygiene.md, "Budget overrun") |
| exit 4 "has no <board>.kicad_pro beside it" | KiCad reads a board's net classes, constraints, severities and `.kicad_dru` only from the project of the board's own name (`main.kicad_pcb` needs `main.kicad_pro`, `main.kicad_dru`). Ask the owner to rename or copy the project; never grade without it |
| `project_files` FAIL, `restored_project_files` | the backend (or an edit) changed the rules files; grading used the input's. The delivered versions are in `<validate dir>/delivered-project-files/` (`validation.delivered_project_files`) |
| exit 4 "identical attempt ... already finished" | the ledger: the same input, backend build and settings already failed. KRT: change the attempt; `--force` only to check for nondeterminism. Freerouting: its outcome may vary, so `--force` is a legitimate retry |
| exit 4, `reason_class` `usage` or `input` | nothing ran: read `message` in the refusal record and fix the command line or the input (`--board`, `--rev`, `--nets`) |
| same failure twice with no new evidence | change approach: the other backend, a different scope, or inspect the board (load `krt-diagnose-routing-failures`) |

Compare attempts from the same input with `kicad-route compare`. For deeper diagnosis
use `krt-diagnose-routing-failures` and `krt-review-routed-board`. Those are KRT's
own skills, adapted and generated by `kicad-route update`.

## Auditing someone else's board change

`kicad-route audit --old <parent> --new <commit>` checks a design commit, such as hand
routing or another agent's routing, without routing anything:

- **Exit code:** 3 when a rule value changed, a finding is hidden by a rule change,
  or the new board has unconnected items, DRC errors or parity issues.
- **Design edits:** moved parts, zones and removed copper are listed under
  `changes`, not failed.
- **Output:** read `OUT/audit/<project>/<old>..<new>/audit.json`:
  - `findings`
  - `rule_changes`: `kind` is changed, added or removed.
  - `hidden_by_rule_changes`: what the new board shows under the parent's rules.
  - `new_violations`
  - `floor`: `floor_lowered` (declared floor lowered: an owner decision, never
    yours), `below_declared`, `hidden_by_floor` (findings), and
    `below_high_water` notes naming the commit that set each mark. See
    `docs/floor.md`.
- **Rule floor in `run`:** an input below its declared floor still routes, but
  validation FAILs (`validation.floor_violated`). Do not edit or lower
  `<board>.rules-floor.json` to get past it; report it to the owner it names.

## Paths

- **CLI:** `~/git/kicad-route/bin/kicad-route`, linked as `~/.local/bin/kicad-route`.
- **Code:** `src/kicad_route/`. That's `cli.py`, plus `kicad/facts.py`,
  `kicad/specctra.py` and `kicad/locks.py`, which run under KiCad's Python.
- **Backends:** `vendor/krt` (latest release checkout) with `.venv-krt`, and
  `vendor/freerouting/current/freerouting-*.jar`. The jar runs with the newest
  installed JVM that satisfies it (Java 25 for 2.5.x).
- **Evidence:** the fixture is in `fixtures/route_demo/`, and its smoke evidence is
  in `reports/fixture/`.
