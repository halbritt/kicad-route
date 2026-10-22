# Effective configuration evidence before transactional repair

2026-10-21, agent-510 verification with a Sol builder and independent Sol reviewer.
Base: `422b9944e934e5757b45a0c0acd3703b2c052e8e`. This is a K91 prerequisite
identified during the K93/K94 state audit, not completion of those tickets.

## Result and boundary

The owned backend captures its actual resolved outer `GridRouteConfig` in
`router/py_router/effective_config_evidence.py::capture`. The wrapper requests,
qualifies and retains it through `configuration_evidence.py::{request,inspect}`,
`cli.py::krt_route` and `evidence.py::block`. The manifest contains typed values,
rule context, selected environment controls, runtime policy, scope and net IDs.
It binds the input/rules, invocation, source and request/settings hashes.

The `route.py` CLI entry point seals the output hash after its output postpasses and before
strict-size exit handling. The wrapper checks that hash before lock restoration;
lock-restored and native-refilled hashes remain separate. Source binding compares
the producer's capture-time source hash with the wrapper's pre-invocation hash.
Named nets are checked against native input facts; numeric net IDs are captured
without independent allocation validation.

The additive result field reports `bound`, `missing`, `unsupported` or `mismatch`.
Legacy and early-return paths can remain `missing`. Unknown configuration fields
and objects cannot silently become complete evidence. No routing default,
electrical rule, native acceptance or comparison gate changes. The versioned
manifest and result schemas are documented in `docs/interface.md`; the field
inventory is packaged and tested in an installed wheel.

`replay_authority` is false. This captures one stage, excluding later per-net
clones/subruns, derived maps and native validation. It is not a worker checkpoint,
primitive ownership manifest, repair transaction or proof of routing improvement.

## Why repair requires a stronger boundary

A synthetic occupied-corridor case at the base revision exercised production
`rip_up_net`, route insertion and `restore_net` with the native occupancy map.
After the victim's horizontal route was removed and the root's vertical route
inserted, restoration refused the occupied corridor. Native KiCad changed from
victim-connected/root-open to victim-open/root-connected. Complete-net count and
native unconnected-item count both stayed at one; cache reconstruction still
passed. Aggregate counts and cache consistency therefore do not prove preserved
connections. There were no introduced short/clearance findings in this fixture.

The root path was supplied, not found by A*. Independently authored before/after
boards required mapping terminals by deterministic footprint UUID plus pad
number; this does not test preservation of original pad UUIDs. A future accepted
repair must derive its candidate from the original board and retain those IDs.

`RoutingState` alone does not contain every live alias: batch locals, input-copper
object IDs, write ledgers, history and map-lift registries also matter. The next
implementation must provide persisted removable-primitive ownership and a
same-geometry isolated worker, then native-grade a whole cohort before atomic
promotion. Reuse `native_components` and `provenance` terminal equivalence checks.
Retain the incumbent unchanged on failure/cancellation. K93 and K94 need that
combined behavior before repair is usable; this evidence addition does not supply it.

## Retained evidence

Local review directory:
`/home/halbritt/.local/share/agent-ee/reviews/2026-10-21-krt93-agent510/`.
It retains `occupied_corridor.py`, `occupied-corridor/` boards/native partitions,
DRC and receipts, `state-audit.md`, `integration-seam.md`, and
`config-evidence-independent-review.md`. The independent source review cleared
the evidence-only boundary. Configuration binding, malformed evidence, legacy
compatibility, real producer/consumer, wheel packaging and the owned kernel are
the relevant validation gates. No target-board campaign or installed runtime
update is included.

Validation completed:

- Configuration tests: 11 independent unit/contract checks passed; the real
  owned-backend fixture passed separately in 2.79 seconds with a 20-second route
  budget and 5,000-iteration setting. It emitted all 102 configuration fields and
  the wrapper reported `bound`. Root independently recomputed manifest and board
  hashes. Output SHA-256:
  `b215c18041621478f06c364c5ab65da55adc281f02eff3ebb472dec304892af0`.
- The installed-wheel resource test passed. `make kernel` passed using the
  selected numeric interpreter, including blocker-resource and occupancy checks.
- Interface, evidence integrity/manifest, comparison acceptance and tool-environment
  tests: all 253 cases passed across the initial run and targeted retries. Initial
  failures came from a missing isolated routing venv, then a missing custom test
  output directory; retained retry logs identify both setup errors. The builder's
  separate test-only JSON receipt serialization error was corrected and its real
  backend test rerun. No production fallback was added for these test failures.

Commands (from the checkout, with the selected interpreter in `KERNEL_PYTHON`):

```sh
make kernel KERNEL_PYTHON="$KERNEL_PYTHON"
/usr/bin/python3 -m pytest -q tests/test_configuration_evidence.py
/usr/bin/python3 -m pytest -q tests/test_installed_wheel.py::test_configuration_evidence_inventory_and_schema_ship
/usr/bin/python3 -m pytest -q tests/test_interface_contract.py tests/test_evidence_integrity.py tests/test_evidence_manifest.py tests/test_engine_acceptance.py tests/test_tool_environment_scope.py
```

Integration tests require an existing writable test output directory and isolated
`KICAD_ROUTE_STATE` with the selected routing venv and installed backend assets.
Exact retry commands/environment are retained in `root-*-command.json` above.
Builder logs and the actual producer/consumer manifest are retained in sibling
`2026-10-21-krt91-config-evidence-agent510/`, including `kernel-tests.log`,
`wheel-test.log`, `focused-tests.log` and `real-backend-retry/`.

Doctrine packet `pkt-f48da27f17a9db19` records the narrower state-audit conclusion;
its retained disposition explicitly leaves full transaction ownership,
cancellation, preservation and promotion obligations unmet. None is inferred
from the configuration tests.
