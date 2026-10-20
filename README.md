# kicad-route

Agent-driven PCB trace routing for KiCad projects, graded by KiCad itself.

`kicad-route` runs a routing backend — its owned engine, extracted from patched
[KRT (KiCadRoutingTools)](https://github.com/drandyhaas/KiCadRoutingTools),
or [Freerouting](https://github.com/freerouting/freerouting) — on an isolated copy of a
KiCad project and hands back a native KiCad candidate board. The candidate is then graded
with `kicad-cli` DRC (zones refilled, schematic parity) against the input's own,
unchanged rules, plus checks that the backend preserved what it must not touch: outline,
layers, stackup, net classes, footprints, zones, keepouts, existing copper, net-class
widths and pour integrity. Backend relaxations are switched off, and anything they still
slip through is caught by the grading rather than trusted.

The result is structured for an agent to read and act on: process, routing and
validation are reported separately, with exit codes 0 (complete and valid), 1 (process
failure, no candidate), 2 (valid but incomplete), 3 (fails validation) and 4 (refused
before routing: a usage or input error, the ledger, the `--canary` pre-flight, a board
without a `.kicad_pro` of its own name, or a writable state directory under
`--require-readonly-state`, with a refusal record). Programs calling it use the versioned
machine interface: `--json` prints one schema-carrying record on stdout ([`docs/interface.md`](docs/interface.md),
[`schemas/`](schemas/)).

## Install

Requirements: Linux, KiCad 10 with its Python bindings for `/usr/bin/python3`
(`kicad-cli`, `pcbnew`), `git`, [`uv`](https://docs.astral.sh/uv/), a Rust toolchain
with Cargo and C build tools (`build-essential` on Debian/Ubuntu) to build the
owned engine, and a Java runtime new
enough for the current Freerouting jar (Java 25 for 2.5.x:
`sudo apt install openjdk-25-jre-headless`). `pytest` and `jsonschema` for `smoke` (test-only).
The `planes` command also requires Bubblewrap (`bwrap`) on PATH.

```sh
git clone https://github.com/halbritt/kicad-route ~/git/kicad-route
~/git/kicad-route/scripts/install.sh      # CLI + skill links, then `kicad-route update`
kicad-route doctor
```

`install.sh` links `~/.local/bin/kicad-route` and the `kicad-route` agent skill into
`~/.agents/skills`, `~/.codex/skills` and `~/.claude/skills`. `kicad-route update`
builds the owned engine from this repository and installs Freerouting's current
release. The versions and source hashes used are recorded in every result.
It generates the `krt-*` skills from the included routing diagnostics,
and runs the integration tests. Re-run it to update.

## Use

From a KiCad project directory:

```sh
kicad-route run --backend krt                          # all nets, 300 s budget
kicad-route run --backend krt --diff-pairs auto        # native-class pairs first, then other nets
kicad-route run --backend freerouting --nets "/CAN*" --clearance-margin 0.01
kicad-route run --backend krt --rev HEAD --budget 1500 # route the committed state
kicad-route validate build/kicad-route/runs/<project>/<label>/latest
kicad-route compare <run_dir> <run_dir>
kicad-route run --backend krt --reserve-spokes         # keep new copper off thermal-spoke corridors
kicad-route run --backend krt --dir <run>/candidate/<project> --protect human
                                                       # continue a chain; earlier agent copper may be rerouted
kicad-route campaign --backend krt --rounds 2          # hard nets first, best chain graded vs the input
kicad-route blocked <run_dir>                          # what lies across each open connection
kicad-route blocked --board <B>.kicad_pcb --json      # the same for a placed board, before routing
kicad-route spokes <run_dir>                           # which new copper starved which thermal pad
kicad-route audit --old HEAD~1                         # grade the last commit's board change
kicad-route floor                                      # rules looser than their high-water mark / declared floor
kicad-route run --backend krt --json --run-dir /tmp/r1 # machine mode: one JSON record on stdout, logs on stderr
kicad-route run --backend krt --barrier-from <run>      # retry with a keepout where that run's new copper collided
kicad-route decoupling --board <B>.kicad_pcb          # read-only: bypass capacitors between IC pins and the plane?
kicad-route planes --board B.kicad_pcb --plan plane-plan.json --run-dir build/planes-1 --json
                                                       # explicit native planes with refill evidence
kicad-route doctor --json --offline                    # readiness, and what a sandbox must provide
```

Runs go under `./build/kicad-route/runs/<project>/<label>/<stamp>/` (`--out`, before or
after the subcommand, or `$KICAD_ROUTE_OUT` to change; `run --run-dir PATH` to choose
the directory); the candidate is `candidate/<project>/<board>.kicad_pcb`,
the verdict `result.json` (and `RESULT.md`). The project itself is never modified.

A project can wrap the commands in its own targets, for example:

```make
ROUTE := kicad-route --out build/route
route:
	$(ROUTE) run --backend $(BACKEND) --rev $(REV) --budget $(BUDGET) $(ARGS)
```

## Documentation

New here? Start with the **[guide](docs/guide/README.md)**: an overview with
diagrams, a walk-through on the bundled demo board (with before/after figures), how a
run works stage by stage, how to read results, real-board workflows, automation and
sandboxes, and a glossary. The pages below are the reference.

- [`docs/interface.md`](docs/interface.md) — the machine contract: record kinds and
  schema ids (`schemas/result-1.json`, `schemas/result-2.json`, `schemas/refusal-1.json`), what each exit code
  promises, refusal records, `--json`, `--run-dir`, stable fields and versioning.
- [`docs/routing.md`](docs/routing.md) — backends and what the adapters switch off,
  run layout and result semantics, native grading, preservation checks, `audit`,
  installation and updates, decisions, known limitations, fixture evidence.
- [`docs/routing-engine-migration.md`](docs/routing-engine-migration.md) — patched
  baseline, source provenance, owned build and migration acceptance requirements.
- [`docs/campaign.md`](docs/campaign.md) — `campaign`: automated hard-nets-first chains,
  backlog rounds, ranking, race orderings.
- [`docs/floor.md`](docs/floor.md) — rule high-water mark and the declared rule floor
  (`<board>.rules-floor.json`) used by `floor`, `audit` and the `run` pre-flight.
- [`docs/blocked.md`](docs/blocked.md) — blocked-by receipts for incomplete runs: what
  lies across each open connection, and a rip-up ranking (`kicad-route blocked`).
- [`docs/hygiene.md`](docs/hygiene.md) — run hygiene: `--stall` progress watchdog,
  the run ledger (exit 4 on an exact repeat of a failed attempt), the awkward-net-name
  canary, and the real-board `corpus` gate that `update` runs.
- [`docs/spokes.md`](docs/spokes.md) — `--reserve-spokes`: temporary keepouts on thermal-spoke
  corridors while routing (removed before grading), and `kicad-route spokes` attribution.
- [`docs/decoupling.md`](docs/decoupling.md) — `kicad-route decoupling`: read-only audit of bypass
  capacitor cells (pin → capacitor → plane, or a direct pin via that bypasses the capacitor).
- [`docs/barrier.md`](docs/barrier.md) — `--barrier-from RUN_DIR`: reroute with a small temporary keepout where a run's new copper collided (removed before grading).
- [`docs/provenance.md`](docs/provenance.md) — `--protect human`: in a `--dir` chain only the
  designer's copper is protected, and the `net_completion` ratchet.
- [`docs/planes.md`](docs/planes.md) — exact `agent-ee.plane-plan/1` zone production,
  native refill evidence and before/after pour integrity (`kicad-route planes`).
- [`docs/pour.md`](docs/pour.md) — the `pour_integrity` check: pad fill contact, thermal
  spokes and pour islands compared between the refilled input and candidate.
- [`docs/sandbox.md`](docs/sandbox.md) — running in a sandbox: what `doctor --json --offline`
  says a sandbox must provide, `--require-readonly-state`, `--ledger`, nothing under HOME.
- [`skills/kicad-route/SKILL.md`](skills/kicad-route/SKILL.md) — the agent-facing guide,
  including the retry playbook.
- `reports/fixture/` — the latest committed smoke evidence (`make evidence`);
  `reports/state/manifest.json` — the state manifest of the install that produced it;
  `reports/determinism/` — the KRT repeat check for that build (`make determinism`).

## Development

```sh
make install          # scripts/install.sh: CLI and skill links, then update
make doctor | update | smoke
make state-manifest   # record the installed backends in STATE/manifest.json (no downloads)
make determinism      # route the fixture twice with KRT, compare, record the verdict
make evidence     # smoke, then publish its structured evidence to reports/fixture/
make fixture      # regenerate fixtures/route_demo
```

## License

kicad-route itself is MIT-licensed ([`LICENSE`](LICENSE)).

The owned routing engine derives from MIT-licensed
[KRT](https://github.com/drandyhaas/KiCadRoutingTools), copyright drandyhaas.
Its license and source provenance are retained in
[`src/kicad_route/router/`](src/kicad_route/router/). It runs in a separate worker
process, using a Rust extension built from the included source.

[Freerouting](https://github.com/freerouting/freerouting) is GPL-3.0 and remains a
separate program, acquired into the git-ignored `STATE/vendor/freerouting` directory.
It is not redistributed, imported or linked by kicad-route.

The owned routing kernels have a separate fast gate: `make kernel`. See
[`docs/kernel-testing.md`](docs/kernel-testing.md) for its dependencies, mutation
proof, upstream test inventory and boundaries; native `make smoke` remains required.
