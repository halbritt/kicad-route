# Explicit short-pair coupling and pad-tail cleanup

The normal `run --backend builtin --diff-pairs auto` command now attempts a
coupled route for short pairs. The backend's default short-leg heuristic stays
available to direct callers; the wrapper explicitly disables that deferral.
Hybrid, partial and single-ended results remain rejected.

The new native 2 mm terminal-span case initially connected both nets but failed
verification with two `track_dangling` findings. Cleanup only recognized vias
and track junctions as anchors along a track body. It now also recognizes actual
same-layer pad copper, projects the pad centre onto the existing track, and
trims beyond that anchor if connectivity is preserved. Protected input and
locked copper remain immutable. The saved short-pair result has zero native
violations, zero opens, 1.90625 mm per member, zero skew and zero gap violations
under its authored 0.34–0.36 mm gap rule. No fabrication qualification follows.

Observed environment: KiCad 10.0.7, Python 3.12.3, pytest 9.1.0, owned router
0.23.1. Tests select the checkout source through a private state directory and
reuse the installed numeric virtualenv. They do not replace installed state.

Fresh checks:

| Exercise | Result |
| --- | --- |
| Differential native routing, incomplete/hybrid rejection, revalidation, short-pair controls and pad-tail controls | 30 passed |
| Owned entrypoint, native preservation, full route_demo round trip, unlocked input copper and forced/cooperative interruption | 37 passed |
| Configuration binding/schema and native short-pair/pad-tail controls | 43 passed |
| Concurrent JSON entrypoint contract, built-in and Freerouting | 1 passed |

All four invocations ran without skips. These are separate invocations with
some overlapping controls, not a count of unique capability requirements.
The initial failed native board, missing test output-directory setup, and
configuration field-inventory/schema mismatch are retained alongside the
corrected runs. The optional copper-ownership result key is now reflected in
the interface tests; the emitted product contract was already additive.

Reproduction from this checkout, with its numeric virtualenv installed:

```sh
mkdir -p build/short-pair-state build/short-pair-tests
ln -s "$PWD/.venv-krt" build/short-pair-state/.venv-krt
KICAD_ROUTE_STATE="$PWD/build/short-pair-state" \
KICAD_ROUTE_TEST_OUT="$PWD/build/short-pair-tests" \
PYTHONPATH=src:tests /usr/bin/python3 -m pytest -v \
  tests/test_diff_pairs.py tests/test_owned_coupling.py
```

Working evidence is `build/agent545-required-coupling/`. Its durable copy is
`~/.cache/agent-ee-qualification/agent545/short-pair-policy-20261024/`, with
native input/output artifacts, failed and passing logs, exact source identity
and a SHA-256 manifest. Full esp_prog still fails terminal coupling; an isolated
probe allowing the natural no-pad-swap candidate also fails the unchanged
clearance guard. That diagnostic source is not integrated. This change does
not establish whole-board acceptance or KRT parity.
