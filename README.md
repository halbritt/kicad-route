# kicad-route

Agent-driven PCB trace routing for KiCad projects, graded by KiCad itself.

`kicad-route` runs a routing backend — [KRT (KiCadRoutingTools)](https://github.com/drandyhaas/KiCadRoutingTools)
or [Freerouting](https://github.com/freerouting/freerouting) — on an isolated copy of a
KiCad project and hands back a native KiCad candidate board. The candidate is then graded
with `kicad-cli` DRC (zones refilled, schematic parity) against the input's own,
unchanged rules, plus checks that the backend preserved what it must not touch: outline,
layers, stackup, net classes, footprints, zones, keepouts, existing copper, net-class
widths and pour integrity. Backend relaxations are switched off, and anything they still
slip through is caught by the grading rather than trusted.

The result is structured for an agent to read and act on: process, routing and
validation are reported separately, with exit codes 0 (complete and valid), 1 (backend
failed), 2 (valid but incomplete), 3 (fails validation) and 4 (refused before routing).

## Install

Requirements: Linux, KiCad 10 with its Python bindings for `/usr/bin/python3`
(`kicad-cli`, `pcbnew`), `git`, [`uv`](https://docs.astral.sh/uv/), and a Java runtime new
enough for the current Freerouting jar (Java 25 for 2.5.x:
`sudo apt install openjdk-25-jre-headless`). `pytest` for `smoke`.

```sh
git clone https://github.com/halbritt/kicad-route ~/git/kicad-route
~/git/kicad-route/scripts/install.sh      # CLI + skill links, then `kicad-route update`
kicad-route doctor
```

`install.sh` links `~/.local/bin/kicad-route` and the `kicad-route` agent skill into
`~/.agents/skills`, `~/.codex/skills` and `~/.claude/skills`. `kicad-route update`
installs both backends at their latest upstream releases (nothing is pinned; the versions
used are recorded in every result), generates the `krt-*` skills from the installed KRT,
and runs the integration tests. Re-run it to update.

## Use

From a KiCad project directory:

```sh
kicad-route run --backend krt                          # all nets, 300 s budget
kicad-route run --backend freerouting --nets "/CAN*" --clearance-margin 0.01
kicad-route run --backend krt --rev HEAD --budget 1500 # route the committed state
kicad-route validate build/kicad-route/runs/<project>/<label>/latest
kicad-route compare <run_dir> <run_dir>
kicad-route run --backend krt --reserve-spokes         # keep new copper off thermal-spoke corridors
kicad-route run --backend krt --dir <run>/candidate/<project> --protect human
                                                       # continue a chain; earlier agent copper may be rerouted
kicad-route campaign --backend krt --rounds 2          # hard nets first, best chain graded vs the input
kicad-route blocked <run_dir>                          # what lies across each open connection
kicad-route spokes <run_dir>                           # which new copper starved which thermal pad
kicad-route audit --old HEAD~1                         # grade the last commit's board change
kicad-route floor                                      # rules looser than their high-water mark / declared floor
```

Runs go under `./build/kicad-route/runs/<project>/<label>/<stamp>/` (`--out` or
`$KICAD_ROUTE_OUT` to change); the candidate is `candidate/<project>/<board>.kicad_pcb`,
the verdict `result.json` (and `RESULT.md`). The project itself is never modified.

A project can wrap the commands in its own targets, for example:

```make
ROUTE := kicad-route --out build/route
route:
	$(ROUTE) run --backend $(BACKEND) --rev $(REV) --budget $(BUDGET) $(ARGS)
```

## Documentation

- [`docs/routing.md`](docs/routing.md) — backends and what the adapters switch off,
  run layout and result semantics, native grading, preservation checks, `audit`,
  installation and updates, decisions, known limitations, fixture evidence.
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
- [`docs/provenance.md`](docs/provenance.md) — `--protect human`: in a `--dir` chain only the
  designer's copper is protected, and the `net_completion` ratchet.
- [`docs/pour.md`](docs/pour.md) — the `pour_integrity` check: pad fill contact, thermal
  spokes and pour islands compared between the refilled input and candidate.
- [`skills/kicad-route/SKILL.md`](skills/kicad-route/SKILL.md) — the agent-facing guide,
  including the retry playbook.
- `reports/fixture/` — the latest committed smoke evidence (`make evidence`).

## Development

```sh
make doctor | update | smoke
make state-manifest   # record the installed backends in STATE/manifest.json (no downloads)
make determinism      # route the fixture twice with KRT, compare, record the verdict
make evidence     # smoke, then publish its structured evidence to reports/fixture/
make fixture      # regenerate fixtures/route_demo
```

## License

kicad-route itself is MIT-licensed ([`LICENSE`](LICENSE)).

The two backends are separate upstream programs under their own licenses:
[KRT](https://github.com/drandyhaas/KiCadRoutingTools) is MIT (copyright drandyhaas) and
[Freerouting](https://github.com/freerouting/freerouting) is GPL-3.0. Neither is part of
this repository or redistributed with it. `kicad-route update` installs them from their
upstream releases into the git-ignored state directory (`vendor/krt`,
`vendor/freerouting`, `.venv-krt` under the checkout or `$KICAD_ROUTE_STATE`), and
kicad-route runs them only as separate processes; it does not import or link either one.
