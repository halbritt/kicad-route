# Owned routing kernel checks

Run `make kernel` after installing the routing environment. In a secondary
worktree, point `KICAD_ROUTE_STATE` at its installed state. The default Python
is selected by `cli.route_python()`, including an active staged generation.
An explicit `KERNEL_PYTHON=/path/to/routing/python` override is also supported. The gate builds this
checkout's locked Rust source offline into `build/kernel`, loads that exact
extension, and runs without KiCad, a board corpus, `vendor/krt`, or network
acquisition. The first build needs the crate versions from `Cargo.lock` already
in Cargo's cache; `update` normally supplies them. It never replaces the live
installed extension.

The fast gate covers:

- Refcounts under duplicate adds, overlap, remove, merge, clone and static
  obstacles, bitmap growth in both directions, and capped-window overflow.
  A separate multiset oracle checks blocked membership after each mutation.
- The retained upstream segment/via add/remove and base/cache stamping parity
  cases, including diagonal and off-grid copper, per-layer widths and raised
  track clearance.
- Small exhaustive lane/pair pose graphs: obstacle avoidance, deterministic
  equal-cost ordering, empty frontiers, impossible paths, required layer
  transitions, and heading/straight-run/via restrictions. The independent
  Dijkstra oracle deliberately covers the unit-cost fixtures; it does not
  establish equivalence for every weighted heuristic or crossover geometry.
- Both public board writers with literal parentheses, escaped quotes and
  backslashes, fake expression text, and malformed input.

Run the mutation proof with the same selected routing environment:

```
make kernel-mutants
```

It builds disposable source copies under `build/`, breaks bitmap clearing,
lane tie ordering and pair via clearance independently, and requires each
mutant to fail an invariant assertion. A build/import failure does not count as
a detected mutation. Logs remain under `build/kernel-mutant-*.log`.

`reports/routing-engine/kernel-test-inventory.json` inventories pinned upstream
entrypoints and records retention or exclusion, source hashes and mechanically
observed imports. The two previously retained fork regressions remain native
integration tests. The wider upstream churn, corpus, GUI, placement and tool
registry suites are excluded from this fast gate; exclusion is not a claim that
those behaviors are protected or unnecessary. In particular the fast refcount
sequence does not replace a whole-route churn audit.

Native integration remains a separate required gate: run `make smoke` with the
system KiCad Python installed. `tests/test_owned_artwork.py` routes a generated
narrow copper-artwork passage, keeps F.Cu/B.Cu text and graphics, and checks
native DRC, preservation and completion. Runtime and aggregate-memory
qualification remains the benchmark harness's job.
