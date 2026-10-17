# kicad-route: design and reference

Local, headless trace routing for KiCad projects with two backends behind one
command. An agent (or a person) asks for an attempt; kicad-route routes an isolated
copy of the project, hands back a native KiCad candidate, and grades it with KiCad's
own checks against the input's unchanged rules. Agents read the structured result
and plan the next attempt. Agent-facing summary: the `kicad-route` skill
(`skills/kicad-route/SKILL.md`).

Run from the project directory (or pass `--dir`); `--out` (default
`./build/kicad-route`) is where runs, caches and the hermetic KiCad configuration go.

```sh
kicad-route doctor                                   # readiness, versions, capabilities
kicad-route run --backend krt                        # route the project in . (a copy), 300 s budget
kicad-route run --backend freerouting --clearance-margin 0.01
kicad-route run --backend krt --rev HEAD --nets "/CAN*" --budget 600
kicad-route validate <run_dir>                       # re-grade a candidate (e.g. after hand edits)
kicad-route validate <last step> --against <first run>   # grade a --dir chain vs the original input
kicad-route compare <run_dir> <run_dir>
kicad-route blocked <run_dir>                        # what blocks the open connections (docs/blocked.md)
kicad-route audit --old HEAD~1 --new HEAD            # grade a committed board change against its parent
kicad-route floor                                    # rule high-water mark and declared floor (docs/floor.md)
kicad-route publish <run_dir> --to reports/routing   # structured evidence for committing
kicad-route update                                   # newest upstream releases, skills, smoke
kicad-route smoke                                    # integration tests on the bundled fixture
```

## Backends

| | KRT (KiCadRoutingTools) | Freerouting |
|---|---|---|
| Upstream | github.com/drandyhaas/KiCadRoutingTools (MIT) | github.com/freerouting/freerouting (GPL-3.0) |
| Channel used | latest GitHub release, git checkout `vendor/krt` | latest GitHub release, executable jar `vendor/freerouting/current/` |
| Runtime | `.venv-krt` (Python 3.12, numpy/scipy/shapely/Pillow from KRT's `requirements.txt` floors) + KRT's prebuilt Rust module | newest installed JVM that satisfies the jar (Java 25 for 2.5.x, apt `openjdk-25-jre-headless`) |
| KiCad interface | reads/writes `.kicad_pcb` directly | KiCad pcbnew SWIG: `ExportSpecctraDSN(board, dsn)` / `ImportSpecctraSES(board, ses)` (`src/kicad_route/kicad/specctra.py`) |
| Needs a running KiCad editor | no | no (the two-argument SWIG functions work headless) |
| Net-name scope | `--nets` passed through | DSN rewrite: out-of-scope nets move to `<class>__skip` copies (same rules), ignored with `--router.autorouter.ignore_net_classes` |
| Net-class scope | wrapper expands classes to names | same DSN mechanism |
| Budget | wall clock: `KICAD_STOP_FILE` at the budget, kill after a grace period; `--max-iterations` | `--router.job_timeout`, kill after budget + 60 s; `--passes` → `--router.autorouter.max_passes` |
| Existing copper | locked copper never touched; with `--protect existing` (default) the wrapper locks all input copper for the run (`kicad/locks.py`) and restores the flags | all pre-existing copper is frozen by Freerouting; the bridge locks it for the round trip and restores the flags |
| Custom `.kicad_dru` rules | reads only a subset (track-to-track class rules, layer clearances); logs the rest as skipped — surfaced in `unsupported` | not in Specctra; the bridge translates net-class-to-net-class `clearance` rules to `class_class` rules; others listed as untranslated |

MCP is not used: both backends have working local CLIs, and agents drive kicad-route
through its CLI (or a project's make targets). Neither backend contacts the network
during an attempt (below).

### What the adapters switch off

KRT relaxes by default. The adapter (`KRT_FLAGS` / `KRT_ENV` in `kicad_route/cli.py`) passes
`--escalation off --fab-tier standard --strict-sizes --no-power-tap-neckdown
--no-fix-drc-settings`, plus `--keep-input-copper --no-stub-layer-swap` for
`--protect existing`, and sets `KICAD_INRUN_FLOOR_SYNC=0 KICAD_TERMINAL_ESCALATION=0
KICAD_IMPEDANCE_NECKDOWN=0 KICAD_RESCUE_CAP_MOVE=0 KICAD_DYNAMIC_ITERATIONS=0`
(and `KICAD_RIP_PREEXISTING=0 KICAD_FINALIZE_RIP=0` for `--protect existing`). Those
flags were not enough on a real 4-layer board: KRT's net rescue still moved 138 existing
segments and vias ("STUB-SWAP RESCUE: GND stub B.Cu -> In1.Cu") and reworked copper on
nets it failed. KRT never touches locked copper, so for `--protect existing` the wrapper
locks every input track and via in the work copy before routing and restores the
original lock flags on KRT's output by exact geometry (`kicad/locks.py`). Without
these, KRT steps down fab tiers, necks wide nets into pads, moves passives to rescue a
net, and writes lowered DRC minimums into its output `.kicad_pro` (seen on the fixture:
`min_hole_clearance` 0.25 → 0.2 with a "FAB FLOOR RELAXED" warning). Two behaviours
cannot be switched off from the CLI in v0.23.x and are caught by the grader instead: a
cleanup pass that may neck new wide segments near foreign pads
(`new_copper_netclass_geometry`), and moving copper text to silkscreen
(`copper_graphics`).

Freerouting is run with `-da` (analytics off), `--api_server.enabled=false`,
`--router.automatic_neckdown=false` (no narrowing into pins),
`--router.fanout.enabled=false` (the fanout stage inserts a "micro-neckdown" at 3/4,
3/5 or 1/2 of the class width whether or not neckdown is enabled —
`FoundConnectionInserter.insertFanoutMicroNeckdown`; on a real-board trial it delivered
0.1904 mm tracks on 0.254 mm nets), a per-run
`--user_data_path`, and `HTTPS_PROXY/HTTP_PROXY=http://127.0.0.1:9`: Freerouting checks
GitHub for a newer version on every start and has no switch for it; the dead proxy
keeps attempts offline. The SES `(placement ...)` section is removed before import, so
a session can never move footprints. Pre-existing copper is locked during the round
trip and its lock flags restored afterwards (see Protected copper below).

Without fanout Freerouting may leave SMD-to-plane or pad-escape connections open;
`--backend-arg=--router.fanout.enabled=true` re-enables it for a retry, and the grader
still checks every new conductor against its net class.

## Attempts, candidates and results

`kicad-route run` creates `OUT/runs/<project>/<label>/<stamp>/` (`<project>` is the
directory name unless `--name` is given):

| Path | Contents |
|---|---|
| `reference/<project>/` | untouched copy of the input (board, `.kicad_pro`, `.kicad_dru`, every reachable schematic sheet, library tables, local libraries; `${KIPRJMOD}/../<dir>` libraries linked beside it as `reference/<dir>`, and the same links are made beside `work/input/` and `candidate/`; `--rev` takes the project, its in-repository `../<dir>` libraries and checked-out submodules (at their pinned commits) from that revision, never the working tree. A `../<dir>` outside the repository is not pinned by the commit: `snapshot.json` `library_links` records it as `pinned: false` with its own git HEAD and uncommitted files) |
| `work/` | backend scratch: its own output files and project writes (recorded, never used), `krt.log` / `freerouting.log`, `board.dsn` / `board.ses`, result JSON |
| `candidate/<project>/` | the reference project with the routed board; zones refilled and saved; open it in KiCad |
| `facts/reference-facts.json` | engineering facts of the input board (`kicad/facts.py`) |
| `validate/` | candidate DRC (`candidate-drc.json`), `new-findings.json`, `preservation.json`, `candidate-facts.json`, `candidate-pour.json` and `pour-integrity.json` |
| `attempt.json` | input identity, scope, settings, baseline location (used by `kicad-route validate`) |
| `result.json`, `RESULT.md` | the outcome |

`result.json` holds backend identity and observed version/build, the input fingerprint
(hash over every input file) and git revision, effective settings, every executed
command with exit code, duration and log path, the candidate location, and three
independent results:

- **process** — PASS when the backend ran and returned a board; FAIL with a reason
  (missing install, crash, budget exceeded, bridge failure) otherwise.
- **routing** — COMPLETE / INCOMPLETE from KiCad's unconnected items on in-scope nets in
  the refilled candidate, with `unrouted_nets` and route statistics (new tracks, vias,
  length per layer, removed copper). When INCOMPLETE, `blocked_by` summarises what lies
  across the open connections (`validate/blocked-by.json`, [blocked.md](blocked.md)).
  Backend self-reports are kept under `process.backend_report`, not used for the verdict.
- **validation** — PASS when native DRC has no new violations or parity issues relative
  to the input, parity actually ran, the graded file is the delivered file, and every
  preservation check passes.

Exit status: 0 complete and valid; 1 process failure (no candidate); 2 valid but
incomplete; 3 fails validation (regardless of completion); 4 refused before routing (the
ledger shows this exact attempt already failed, or the `--canary` pre-flight failed; no
candidate is made; [hygiene.md](hygiene.md)).

### Native grading

The candidate is graded with the installed `kicad-cli` (10.0.7):
`kicad-cli pcb drc --refill-zones --save-board` (fills are saved into the delivered
candidate), then `kicad-cli pcb drc --format json --severity-all --schematic-parity
--units mm` on that saved file. Its project files are the input's, byte for byte, so
KiCad applies the original net classes, custom rules and severities. If a candidate's
project files differ (for example after hand edits), `validate` restores them before
grading and fails the attempt, naming the files. The parity-skip message ("Failed to
fetch schematic netlist…") fails validation. KiCad runs with a hermetic configuration
(`OUT/.kicad-config`: stock global library tables, nothing from the GUI).

Pre-existing findings: the untouched input is graded the same way (refill, then DRC)
and cached under `OUT/.cache/baseline/<input fingerprint>/`. A candidate finding
is new unless the same type, severity and items (description and position) exist in
the baseline. Only new findings fail.

### Preservation checks

From `kicad/facts.py` (KiCad's own loader; serialisation differences do not count):

| Check | Compared |
|---|---|
| `project_files` | `.kicad_pro`, `.kicad_dru`, schematics, library tables, local libraries (bytes) |
| `board_outline` | every Edge.Cuts shape, board and footprint owned |
| `layers` | copper layer count and types, enabled layers |
| `stackup` | the `(setup (stackup …))` tree |
| `board_setup` | the rest of the board `(setup …)` tree (plot settings excluded) |
| `nets_and_netclasses` | net names and the effective net class KiCad resolves for each |
| `pad_connectivity` | every pad's net |
| `footprints` | library id, position, orientation, side, lock, pad geometry |
| `zones` | outline, net, layers, priority, clearance, thermal and fill settings (fill polygons are regenerated) |
| `keepouts` | rule-area outlines, layers and restrictions |
| `copper_graphics` | non-track copper shapes and text |
| `protected_copper` | designated tracks/vias still present with the same geometry (1 µm tolerance for Specctra quantisation); designated = locked items, plus all input copper with `--protect existing`, or the chain's human copper with `--protect human` ([`provenance.md`](provenance.md)) |
| `new_copper_netclass_geometry` | every new track at least its net class width, every new via at least its class diameter and drill |
| `routing_scope` | new copper only on in-scope nets |
| `net_completion` | every net fully connected in the input is still connected in the candidate (the completion ratchet; all protect modes) |
| `existing_unlocked_copper` | with `--protect locked`/`human`: how much unlocked input copper was removed or changed (informational, always PASS) |
| `pour_integrity` | the refilled candidate's pours against the refilled input's: no zone-connected pad loses fill contact, drops below `min_resolved_spokes` or lands on an island cut off from its net's main pour; area loss reported (fails only with `--pour-area-loss PCT`). See [`pour.md`](pour.md) |

Protected copper across formats: KiCad exports locked tracks/vias as Specctra
`(type fix)` and unlocked ones as `(type route)`; Freerouting freezes both (it reads
`fix` as system-fixed and never writes it back; `route` becomes user-fixed and returns as
`protect`). KiCad's SES import removes every unlocked track and via, keeps locked ones
untouched, and rebuilds the rest from the session. On a real-board trial that rebuild
altered 7 pre-existing segments (a 0.5 µm segment collapsed, short diagonals at
junctions re-normalised — upstream #958/#960/#987). The bridge therefore locks every
pre-existing track and via for export and import and then restores the original lock
flags, so input copper never passes through the session. Copper is still matched by
geometry with a 1 µm tolerance, and new copper sizes allow the same 1 µm for Specctra
quantisation (KiCad reads a 0.29972 mm via drill back as 0.299 mm).

### Tightening controls

Allowed because they only make routing stricter; grading is unchanged:
`--default-clearance MM` (Default class: KRT `--clearance`, DSN `kicad_default` class
and board rule), `--clearance-margin MM` (Freerouting: added to every DSN clearance),
plus any backend option through `--backend-arg` / `--backend-env` (recorded in the
result). A relaxing option passed through does not change the verdict; it can only
make the candidate fail.

## Installation and updates

Nothing here is pinned. The versions in use are recorded in every `result.json`,
`OUT/doctor/doctor.json` and `OUT/update/update.json` for diagnosis; they are not
install requirements. Installed state lives in this checkout (or `$KICAD_ROUTE_STATE`):
`vendor/krt`, `vendor/freerouting`, `.venv-krt` and the generated `vendor/skills`, all
git-ignored.

- `scripts/install.sh` links `~/.local/bin/kicad-route` and the `kicad-route` skill
  (`~/.agents/skills`, `~/.codex/skills`, `~/.claude/skills`), then runs `update`
  (`--no-update` skips it). A stale link (another checkout) is repointed; a real file
  or directory at a link path is never replaced.
- `kicad-route update`:
  - KRT: `git fetch --tags`, check out the latest GitHub release tag, create
    `.venv-krt` if needed (on the Python running kicad-route, or
    `$KICAD_ROUTE_KRT_PYTHON`; KRT does not use pcbnew), `uv pip install --upgrade -r requirements.txt`,
    `build_router.py --tag <tag>` (downloads the release's prebuilt Rust module; KRT
    builds from source with cargo only when no prebuilt matches), KRT's
    `startup_checks.py`.
  - Freerouting: download the latest stable release's `freerouting-<v>.jar` (a
    pre-release-suffixed jar such as `-rc1` only when the release has no plain one),
    verify it against GitHub's per-asset `sha256:` digest (a mismatch aborts; a missing
    digest installs it with `digest_verified: false`), record `release.json`, repoint
    `current`, remove the previous version.
  - GitHub API calls send `$GITHUB_TOKEN` (else `$GH_TOKEN`) when set, to avoid the
    unauthenticated rate limit.
  - Regenerate the `krt-*` skills from the new KRT (`vendor/skills/`, linked into the same
    skill directories), then run `kicad-route smoke` (round trips, constraint
    preservation, native validation, failure reporting). Compatibility problems surface
    there and in `kicad-route doctor` ("KRT controls used by the adapter" fails if a
    flag the adapter relies on disappears).
- Java: the jar states the Java release it was built for; the wrapper picks the newest
  JVM under `/usr/lib/jvm` that satisfies it. Install new majors with apt
  (`sudo apt install openjdk-<N>-jre-headless`); security updates arrive with normal
  `apt upgrade`. The system default `java` stays where it was (here 21:
  `sudo update-alternatives --set java /usr/lib/jvm/java-21-openjdk-amd64/bin/java`
  after installing 25, which apt otherwise makes the default).
- Following KRT `main` instead of releases: `git -C vendor/krt checkout origin/main &&
  .venv-krt/bin/python vendor/krt/build_router.py` (needs a Rust toolchain when `main`'s
  crate is ahead of the latest release); then `kicad-route smoke`.
- Fresh clone: `scripts/install.sh` installs everything except the JVM and KiCad.

### Observed at setup (2026-10-17)

| Component | Observed |
|---|---|
| KiCad / kicad-cli | 10.0.7 (`10.0.7~ubuntu24.04.1`) |
| KRT | v0.23.1 (`295f9ee`), grid_router 0.23.1 prebuilt |
| KRT Python deps | Python 3.12.3, numpy 2.5.4, scipy 1.18.1, shapely 2.2.0, Pillow 13.0.0 |
| Freerouting | v2.5.0 jar, sha256 `f6f51bb0…5de3c7`, build revision `b647a2f` |
| Java for Freerouting | OpenJDK 25.0.4.1 (`openjdk-25-jre-headless 25.0.4.1+1-1~24.04.4`) |

## Decisions

- **Freerouting jar, not the native CLI.** v2.5.0 introduced
  `freerouting-cli-<v>-linux-x64` (GraalVM native image). It crashes in
  `BoardSnapshotManager.serialize` as soon as routing starts and then hangs without
  writing a session (upstream issue #957, fixed on master by PR #966, not yet released).
  The jar has the same CLI and works. Revisit when a release contains the fix: the
  native binary needs no JVM.
- **Java 25 from apt.** The 2.5.0 jar is built for Java 25 (class file 69) and fails on
  the installed Java 21. apt's `openjdk-25-jre-headless` gets normal security updates;
  the system default `java` was left at 21.
- **KRT latest release, not `main`.** Releases carry the matching prebuilt Rust module
  (no Rust toolchain on this machine); `main` moves several times a day.
- **pcbnew SWIG for the Specctra bridge.** `kicad-cli` 10.0.7 has no DSN export or SES
  import (`kicad-cli pcb import` reads only foreign CAD formats). The one-argument SWIG
  functions need the PCB editor; the two-argument ones work headless. KiCad marks SWIG
  as deprecated in favour of the IPC API, which needs a running KiCad; when a KiCad
  release removes SWIG, the bridge and `kicad/facts.py` need an IPC-based replacement.
- **Grading is KiCad's, always.** Backend reports disagree with KiCad in both
  directions (below); only KiCad's DRC against the original project decides.
- **kicad-route is the routing stage of the agent-ee generator** (agent-ee decision 3,
  2026-10-17). The generator runs requirements, parts, schematic, placement, routing,
  verification and fab package as separately verified stages; routing is a
  `kicad-route run` or `campaign` that agent-ee executes as a sandboxed writer. It
  routes a copy, returns a candidate and a `result.json`, and agent-ee captures both
  as evidence. What that needs from kicad-route (a versioned result contract,
  hashes, read-only state, offline operation) is tracked in Plane KICADROUTE under
  the `generator` label.
- **KRT is the default backend; Freerouting runs are attempts, not measurements**
  (agent-ee decision 3). KRT is chosen because it is deterministic: the same input,
  settings and KRT build should give the same candidate, so a result can be
  reproduced and a repeat of a failed attempt changes nothing. No repeat-run
  comparison proves this yet; a Plane item tracks that check. A Freerouting run can
  be graded and adopted like any candidate, but its outcome is not evidence of what
  a board, a scope or a setting can achieve.
- **Placement is not kicad-route's job** (agent-ee decision 4). kicad-route preserves
  footprints, outline, zones, keepouts and rules and grades only the copper it adds;
  moving a footprint fails the `footprints` preservation check. The placement stage
  lives in agent-ee because (a) the agent-ee campaign loop is the generator's engine
  and placement is the same "writer proposes, checks grade" step; (b) moving
  footprints contradicts kicad-route's preservation invariants; (c) the placement
  checks (courtyard DRC with `kicad-cli`, ratsnest length and keepouts with the
  pcbnew probe, connector-on-edge geometry) are native-tool checks that agent-ee
  already runs in its sandbox. kicad-route contributes routability evidence: the
  placement stage consumes `kicad-route blocked` receipts ([blocked.md](blocked.md))
  as a check input.
- **Backends stay upstream; versions are recorded per run** (agent-ee decision 6). KRT
  and Freerouting are installed from their upstream releases into the git-ignored
  state directory (`vendor/`, `.venv-krt`), never committed or modified here, and
  every `result.json` records the backend version and build. Freerouting (GPL-3.0) and
  KRT run only as separate processes; kicad-route does not import or link either.
- **MIT license for kicad-route** (2026-10-17, [`LICENSE`](../LICENSE)). It covers this
  repository only; KRT (MIT) and Freerouting (GPL-3.0) keep their own licenses.

## Known limitations

- KRT honours only part of `.kicad_dru` (it logs skipped rules; they appear under
  `unsupported`). On the fixture, its default attempt violates a Power-to-signal 0.4 mm
  rule; `--default-clearance 0.4` produces a valid candidate.
- KRT checks the stop file only between nets of its main pass. Its later passes (net
  rescue, Phase 3, plane finalize, final reconciliation) ignore it. On a blocked board,
  the rescue keeps searching after the budget until the grace period ends, and then
  KRT is killed (process FAIL, no candidate). `--backend-env KICAD_NET_RESCUE=0`
  (or `KICAD_PLANE_FINALIZE=0`) returns a partial candidate. See docs/hygiene.md,
  "Budget overrun".
- KRT exits 0 even when nets fail; the wrapper ignores its exit code for completion.
- Freerouting approximates pad shapes and can land a few micrometres inside a clearance
  (fixture: 0.3928 mm against a 0.4 mm rule); `--clearance-margin 0.01` resolved it.
- Specctra export rounds pad centres to 1 µm, so Freerouting can report a locked track
  that ends on a pad centre (e.g. 124.0875 mm) as unconnected; KiCad sees it connected.
  Recorded in `process.backend_report`, not used for the verdict.
- Freerouting leaves fanout vias on nets it cannot finish (`via_dangling` findings), and
  stops when its score stops improving; more `--passes` or another scope may help.
- Freerouting only places through vias and cannot see custom rules other than
  class-to-class clearances, diff pairs, or length tuning; arcs export as chords.
- Attempts route one scope at a time; chain scopes by routing from a previous candidate
  (`--dir <run>/candidate/<project>`), which keeps the original rules (the candidate's
  project files are the input's). Each step is graded against its own input, so a step
  inherits the previous step's findings as pre-existing; grade the end of the chain with
  `kicad-route validate <last> --against <first>`, which follows the steps' recorded
  inputs back to the first run (refusing a board that does not descend from it) and
  grades against the original input, baseline and protection with the union of the
  steps' scopes (`revalidate-origin/<stamp>/result.json`). With `--protect existing` a
  later step cannot move copper an earlier step placed, so route the hardest nets first
  (on a real board, finishing 16 leftover nets on a fully locked candidate routed 0 of 16
  on both backends; routing them first on the original input routed 11).
- Board facts and the bridge depend on KiCad's SWIG module (see Decisions).

## Fixture and evidence

`fixtures/route_demo/` (regenerate with `make fixture`) is a 2-layer
50 × 30 mm board with KiCad's stock stackup, two net classes (Default 0.2/0.2 mm, Power
0.5/0.3 mm with 0.8/0.4 vias), a custom `.kicad_dru` rule (Power to non-Power ≥ 0.4 mm),
a keepout across the direct J1→J2 path, a GND zone on B.Cu, and a locked, pre-routed
`/SIG_B` (5 tracks, 2 vias). The input is ERC/DRC/parity clean with 6 unconnected items.

`kicad-route smoke` (`tests/test_routing.py`) runs under `OUT/smoke`; `make evidence`
also publishes it to `reports/fixture/`: per-test records, junit, and for each attempt
its structured receipts (as `kicad-route publish`: `result.json`, `RESULT.md`,
`attempt.json`, every `validate/*.json` such as `preservation.json`,
`new-findings.json`, `candidate-drc.json`, `pour-integrity.json`, `blocked-by.json`,
plus `spokes.json`, `provenance.json`, `facts/`, `spokes/`, and the latest
`revalidate*/` as `against-origin/` and `revalidate/`; never a board, project file, log
or `work/`). It proves:

- KRT and Freerouting round trips that are complete and valid, with every preservation
  check passing and the candidate's rules byte-identical to the input's;
- the KRT default attempt fails validation on the custom rule, and the failure is the
  exit code (3), with the skipped rule named under `unsupported`;
- net-name (`--nets '/SIG_A*'`) and net-class (`--netclass Power`) scopes, with no
  copper outside scope;
- unlocked pre-existing copper (a variant with `/SIG_B` unlocked) comes back from both
  backends bit-identical (0 nm deviation) and still unlocked;
- an unroutable variant (keepout wall) comes back INCOMPLETE from both backends, and a
  budget overrun is a process failure (exit 1);
- a tampered candidate (footprint moved 0.5 mm, a locked via deleted, a Power track
  narrowed to 0.3 mm, a project clearance lowered) fails `validate` on `footprints`,
  `protected_copper`, `new_copper_netclass_geometry` and `project_files`;
- `compare` refuses to call different inputs comparable;
- a `--dir` chain graded with `validate --against` is judged against the first input;
- `audit` of a commit that lowers a rule and moves a part flags the rule change, grades
  the new board under the parent's rules and lists the footprint change;
- the source fixture is byte-identical after the whole run.

## Extensions

Each has its own page; all are graded by the same native checks above and none
relaxes a rule.

| Feature | Entry point | Page |
|---|---|---|
| Hard-nets-first campaigns | `kicad-route campaign` | [`campaign.md`](campaign.md) |
| Thermal-spoke corridor reservation and spoke-cut attribution | `run --reserve-spokes [N]`, `kicad-route spokes` | [`spokes.md`](spokes.md) |
| Collision barriers (rewind with a keepout where new copper collided) | `run --barrier-from RUN_DIR`, `kicad-route barrier` | [`barrier.md`](barrier.md) |
| Copper provenance and the completion ratchet | `run --protect human`, check `net_completion` | [`provenance.md`](provenance.md) |
| Blocked-by receipts and rip-up ranking | `routing.blocked_by`, `kicad-route blocked` | [`blocked.md`](blocked.md) |
| Pour integrity | check `pour_integrity`, `--pour-area-loss`, `--pour-min-spokes` | [`pour.md`](pour.md) |
| Rule high-water mark and declared floor | `kicad-route floor`, `audit`, `run` pre-flight | [`floor.md`](floor.md) |
| Stall watchdog, run ledger, net-name canary, real-board corpus | `--stall`, `--force`, `--canary`, `kicad-route canary`, `kicad-route corpus` | [`hygiene.md`](hygiene.md) |

## Auditing a committed board change

`kicad-route audit --old REV --new REV` checks a design commit (hand routing, another
agent's work) the way an attempt is checked, without routing anything: both revisions
are snapshotted with `git archive`, refilled and graded with native DRC and parity;
findings new in `--new` are separated from the parent's; the preservation checks list
what changed (moved footprints, edited zones, removed copper, new copper below its net
class); and rule-relevant project changes (`rules`, `rule_severities`, `drc_exclusions`,
net classes, `.kicad_dru`) are listed. When rules changed, the new board is also graded
under the parent's project files, so findings a rule change hides are reported
(`hidden_by_rule_changes`). Design changes are reported, not failed; the exit status is
3 when a rule value changed (keys KiCad merely writes out with their defaults are
listed as `added`, not counted), a finding is hidden, or the new board has unconnected items, DRC
errors or parity issues, else 0. Output: `OUT/audit/<project>/<old>..<new>/audit.json`.

Audit also checks `--new` against the rule floor (`docs/floor.md`): the strictest value
each rule ever had in the history up to `--new`, and the declared floor
`<board>.rules-floor.json`. Lowering the declared floor (`floor_lowered`), a setting
below it (`below_declared`) and findings that appear only when the new board is graded
under the floor (`hidden_by_floor`) exit 3; a setting below only its high-water mark is
a note naming the commit that set it.
