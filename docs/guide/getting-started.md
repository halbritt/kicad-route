# 2. Getting started

This page takes you from nothing to a graded routing attempt on the bundled demo
board, and shows what you see at each step.

## Install

Requirements: Linux, KiCad 10 with its Python bindings for `/usr/bin/python3`
(`kicad-cli` and `pcbnew`), `git`, [`uv`](https://docs.astral.sh/uv/), and a Java
runtime new enough for the current Freerouting jar (Java 25 for 2.5.x).

```sh
git clone https://github.com/halbritt/kicad-route ~/git/kicad-route
~/git/kicad-route/scripts/install.sh      # links the CLI and the agent skill, then runs `kicad-route update`
kicad-route doctor
```

`install.sh` links `~/.local/bin/kicad-route` and installs both backends at their
latest upstream releases. Nothing is pinned: the versions in use are recorded in
every result instead ([routing.md](../routing.md#installation-and-updates)).

```mermaid
flowchart LR
    A["scripts/install.sh"] --> B["link ~/.local/bin/kicad-route<br/>and the agent skill"]
    B --> C["kicad-route update"]
    C --> D["Owned routing source<br/>Cargo build + generation venv"]
    C --> E["Freerouting: latest jar<br/>(sha256 verified)"]
    C --> F["STATE/manifest.json<br/>what is installed"]
    C --> G["smoke: integration tests<br/>on the demo board"]
```

`kicad-route doctor` reports readiness: KiCad and backend versions, whether the
installed files still match the manifest, and whether the awkward-net-name canary
passes. Fix anything it marks as a required failure before routing.

## Route the demo board

Copy the demo project somewhere writable and route it. (The copy is only so that
the output directory lands beside a project you own; kicad-route never writes into
the project itself.)

```sh
cp -r ~/git/kicad-route/fixtures/route_demo /tmp/route_demo
cd /tmp/route_demo
kicad-route run --backend builtin --budget 120 --default-clearance 0.4
```

The console shows each stage as it happens. A new run starts with this backend
name and default directory label (`<stamp>` is the run's timestamp):

```text
[route] builtin on route_demo -> build/kicad-route/runs/route_demo/builtin/<stamp>
```

The captured output, verdicts and
comparison table below are from 2026-10-18, before the backend was renamed; their
`krt` names and paths are historical. New runs identify this backend as `builtin`
and use `builtin` as the default run label. `--backend krt` still works as a
deprecated alias, with a warning on stderr.

```
[route] krt on route_demo -> build/kicad-route/runs/route_demo/krt/20261018T145739.829486Z
[route] scope: all (6 of 6 nets)
[route] baseline: native DRC of the untouched input (refilled)
[route] routing with krt (budget 120s)
[route] process: PASS
[route] validating the candidate (refill, DRC + parity, preservation)
[route] routing: COMPLETE
[route] validation: PASS
[route] candidate: build/kicad-route/runs/route_demo/krt/20261018T145739.829486Z/candidate/route_demo/route_demo.kicad_pcb
[route] result: build/kicad-route/runs/route_demo/krt/20261018T145739.829486Z/result.json  (exit 0)
```

Exit 0 means **complete and valid**: every in-scope connection is routed, KiCad's
DRC found nothing new, and every preservation check passed.

## Look at the candidate

For the new run above, open the candidate in KiCad:

```sh
pcbnew build/kicad-route/runs/route_demo/builtin/latest/candidate/route_demo/route_demo.kicad_pcb
```

| Before | After |
|---|---|
| ![input](../figures/fixture-input.png) | ![historical KRT candidate](../figures/fixture-krt.png) |

The candidate directory is a complete copy of the project with the routed board in
it, zones refilled and saved. It is exactly the file KiCad graded. The original in
`/tmp/route_demo` is untouched: compare the two with `diff -r` if you like.

## Read the verdict

`RESULT.md` in the run directory is the human-readable verdict. The top of the historical example (use your new run's printed path to read its verdict):

```
# Routing attempt: krt on route_demo

- Backend: krt 0.23.1 (v0.23.1)
- Input: `route_demo.kicad_pcb` from working tree @ None (fingerprint ba2f343c49e2f971)
- Scope: all, 6 nets; protect: existing; budget 120 s
- Candidate: `build/kicad-route/runs/route_demo/krt/20261018T145739.829486Z/candidate/route_demo/route_demo.kicad_pcb`

| Result | Status | Detail |
|---|---|---|
| process | PASS | exit 0, 9.105 s |
| routing | COMPLETE | 0 unconnected in scope, 0 total (input: 6) |
| validation | PASS | no new findings |

| Preservation check | Status |
|---|---|
| project_files | PASS |
| board_outline | PASS |
| ...
| pour_integrity | PASS |
```

Three independent results, always in this order: did the backend run (**process**),
is everything connected (**routing**), and is the board acceptable (**validation**).
`result.json` beside it has the same content for programs. [Reading results](reading-results.md)
explains every field.

## A second attempt: a rule the router reads, and KiCad grades

The demo project has a custom rule in `route_demo.kicad_dru`: Power nets must keep
0.4 mm from everything that is not Power. The first attempt made the whole Default
class that strict with `--default-clearance 0.4`. The built-in router reads a
clearance rule scoped by net class itself, so the option is not needed. Run it
again without it:

```sh
kicad-route run --backend builtin --budget 120
```

```
[route] process: PASS
[route] routing: COMPLETE
[route] validation: PASS
```

The engine's log (`work/krt.log` in the run directory) says which rule it took:

```
Class/type-scoped clearance rule from the board's .kicad_dru (#1491): 'power_to_signal_clearance' 0.4mm when "A.NetClass == 'Power' && B.NetClass != 'Power'"
```

Reading the rule is the backend's business. The verdict is not: KiCad's DRC grades
every candidate under all of the project's rules, whether the backend read them or
not. `RESULT.md` says so under "Unsupported or approximated by this backend":

```
- KRT reads only a subset of .kicad_dru custom rules; every rule is still graded by KiCad DRC
```

When a backend misses a rule, the run ends with exit 3, **fails validation**: the
routing may be complete, but KiCad found a violation the backend could not see, and
`validate/new-findings.json` names the two items and where they are. kicad-route
does not relax the rule and does not hide the finding. Until the engine moved onto
KiCadRoutingTools 0.23.9 this board was that case: without the option the built-in
router passed a ground pad at 0.3748 mm with a `/SIG_A` track and the run failed
with one new clearance error. The remedy then, and for any rule a backend does not
read, is a tightening option such as `--default-clearance`. Tightening is always
allowed; relaxing never is.

## Compare attempts

```sh
kicad-route compare build/kicad-route/runs/route_demo/builtin/2026*Z
```

```
| fp | backend | exit | routing | unconnected | validation | new_violations | new_vias | new_length_mm | backend_s | run |
|---|---|---|---|---|---|---|---|---|---|---|
| d4d68e21a5b0 | krt 0.23.1 | 0 | COMPLETE | 0 | PASS | 0 | 1 | 117.987 | 9.105 | build/kicad-route/runs/route_demo/krt/20261018T145739.829486Z |
| a29dca227d25 | krt 0.23.1 | 3 | COMPLETE | 0 | FAIL | 1 | 1 | 117.87 | 10.463 | build/kicad-route/runs/route_demo/krt/20261018T145810.667194Z |
```

The second row is the historical run without the option, which failed on the custom
rule; with the present engine both attempts end with exit 0 and PASS.

`compare` refuses to put runs of different inputs side by side, so the table is
always apples to apples. The `fp` column is the attempt fingerprint the run ledger
uses (see below).

## Route only some nets

```sh
kicad-route run --backend builtin --nets '/SIG_A*' --default-clearance 0.4
```

![historical KRT candidate with only the /SIG_A nets routed](../figures/fixture-krt-nets.png)

Only `/SIG_A` and `/SIG_A_OUT` were routed; `/VIN` and the others are still open,
and the run is still exit 0, because the verdict is about the **scope** you asked
for. The `routing_scope` preservation check makes sure no copper appeared on nets
outside it.

## Try the other backend

```sh
kicad-route run --backend freerouting --clearance-margin 0.01
```

![Freerouting candidate](../figures/fixture-freerouting.png)

Freerouting takes a different path and uses the back layer for part of `/VIN`.
`--clearance-margin 0.01` adds 10 µm to every clearance it is given, because it
approximates pad shapes and can land a few micrometres inside a rule.

## Things to know on day one

- **Where runs go.** `./build/kicad-route/runs/<project>/<label>/<stamp>/`, with a
  `latest` link per label. Change the root with `--out DIR` or `$KICAD_ROUTE_OUT`;
  pick the directory yourself with `run --run-dir PATH`.
- **The run ledger.** An exact repeat of an attempt that already failed is refused
  with exit 4, because the built-in router would fail the same way again. Change something (scope,
  budget, an option) or pass `--force`. The ledger is `build/kicad-route/ledger.jsonl`.
- **Budget.** `--budget SECONDS` is wall clock (default 300). The built-in router is stopped
  cleanly at the budget when it can be, and killed after a grace period when it
  cannot.
- **The project file must match the board's name.** KiCad only loads
  `main.kicad_pro` and `main.kicad_dru` for `main.kicad_pcb`. A board without a
  same-name project file is refused (exit 4) rather than graded under KiCad's
  defaults.
- **The verdict is the exit code.** 0 complete and valid, 1 the backend failed,
  2 valid but incomplete, 3 fails validation, 4 refused before routing.

Next: [How a run works](how-a-run-works.md).
