# Running kicad-route in a sandbox

agent-ee runs kicad-route as a sandboxed writer (agent-ee decision 3): routing gets no
network, reads the installed backends and the project, and writes only its `--out`.
This page lists what such a sandbox has to provide, how kicad-route reports it, and
what kicad-route checks and records when it runs inside one. Plane: KICADROUTE-5 and
KICADROUTE-6. Code: `src/kicad_route/sandbox.py`. Tests: `tests/test_doctor_sandbox.py`.

## What a sandbox must provide: `doctor --json --offline`

```sh
kicad-route --out OUT doctor --json --offline [--no-canary] [--require-readonly-state]
```

- `--json` (the global machine-mode flag, docs/interface.md) prints the one
  `kicad-route/doctor/1` record on stdout. Logs and child-process output go to stderr.
  The same record is written to `OUT/doctor/doctor.json`.
- `--offline` skips the latest-release lookups. They are reported `NOT_RUN` with
  `required: false`. The doctor opens no network socket.
- `--no-canary` skips the awkward-net-name canary (reported `NOT_RUN`, not required).
  The canary runs by default.
- `--require-readonly-state` (or `KICAD_ROUTE_REQUIRE_READONLY_STATE=1`) makes the
  "state read-only" check required. Without it the check is informational.

The record holds `status`, `required_failed`, `checks`, `versions`, `capabilities`,
`state` (STATE's path, installed parts and writability) and a `sandbox` block:

| Key | Contents |
|---|---|
| `ro_paths` | each read-only path with its purpose: `/usr`, `/etc`, the checkout (TOOL_ROOT), its git common dir when it is a worktree, STATE, the KRT venv's interpreter target, the pcbnew Python and module, `/usr/share/kicad`, `kicad-cli`, `git`, and the JVM directory `find_java` chose. Paths are resolved and absolute. Bind them at the same path: the venv stores absolute paths |
| `ro_binds` | the smallest subset of `ro_paths` whose binds cover all of them |
| `rw_paths` | OUT, the only writable path a run needs |
| `env` | variables to set, with values: `KICAD_ROUTE_STATE`, `PATH` (`kicad-cli`, `git`), and the optional `HOME` (`required: false`; it need not be writable, see [HOME](#home) below), `KICAD_ROUTE_PCBNEW_PYTHON`, `KICAD_ROUTE_OUT`, `KICAD_ROUTE_REQUIRE_READONLY_STATE` |
| `network` | hosts per command. `run`, `campaign`, `validate`, `blocked` and `doctor --offline` need none. `doctor` needs `api.github.com`. `update` needs GitHub (API, clone, release assets) and PyPI |
| `writes_state` | the commands that write STATE: `update` and `corpus add` |
| `observed` | what the doctor process itself saw (the same facts a run records, below) |

On this host the binds beyond agent-ee's own `/usr` and `/etc` are the checkout and
STATE. Everything else (KiCad, Python, the JVM under `/usr/lib/jvm`) lies in `/usr`.

## The agent-ee launch

agent-ee's `executor.sandbox_argv` clears the environment, hides `/home` and `/run`,
makes `/tmp` a tmpfs, binds `/usr`, `/etc`, the project read-only at `/candidate`,
and one writable `/work`, and unshares the network. kicad-route then needs:

```sh
# extra_ro: every doctor ro_binds entry outside /usr and /etc, at its host path
bwrap ... --unshare-net \
  --ro-bind STATE STATE --ro-bind TOOL_ROOT TOOL_ROOT \
  --ro-bind PROJECT /candidate --bind WORK /work --chdir /work \
  --clearenv --setenv HOME /work/home --setenv PATH /usr/local/bin:/usr/bin:/bin -- \
  /usr/bin/env KICAD_ROUTE_STATE=STATE \
  /usr/bin/python3 TOOL_ROOT/bin/kicad-route --out /work/out \
    run --backend krt --dir /candidate --name PROJECT_NAME \
    --require-readonly-state --ledger /work/ledger.jsonl
```

Pass `--name`: the project directory is mounted as `/candidate`, and the name
otherwise comes from the directory. With these mounts the fixture's KRT and
Freerouting round trips give the same verdicts, input fingerprint and ledger
fingerprint as the same runs outside the sandbox, and the same candidate: identical
copper and an identical rest of the file. The raw file hashes differ, because KiCad
gives every new track and via a random UUID, so no two runs (sandboxed or not) are
byte-identical (`tests/test_doctor_sandbox.py::test_sandboxed_round_trip_matches_unsandboxed`).

## HOME

kicad-route writes nothing under `HOME`, so `HOME` may be read-only (agent-ee's
`--setenv HOME /work/home` above is harmless but not needed). KiCad's configuration
goes to `OUT/.kicad-config`; KiCad's cache and user data, fontconfig's cache and the
other XDG directories go to `OUT/.home` (`KICAD_CACHE_HOME`, `KICAD_DOCUMENTS_HOME`,
`XDG_CACHE_HOME`, `XDG_DATA_HOME`, `XDG_CONFIG_HOME`, `XDG_STATE_HOME`, set by
`common.tool_env()` for every tool subprocess). The Freerouting JVM reads `user.home`
from the password database, not `$HOME`, so each launch gets `-Duser.home` and
`-Djava.util.prefs.userRoot` under `OUT/.home/java`. The full table is in
[hygiene.md](hygiene.md#nothing-under-home). `/tmp` still receives KiCad's instance
lock, the JVM's `hsperfdata` and KRT's temporary directories.
`tests/test_home_containment.py` runs the fixture's round trips and `doctor --json
--offline` with an empty read-only `HOME` under `strace` and fails on any write
attempt there, under the real home, or outside OUT and `/tmp` (KICADROUTE-60).

## `--require-readonly-state`

`run` and `campaign` accept `--require-readonly-state`. It and `--ledger` are
environment options (`cli.add_environment_args`), not routing options: `campaign`
passes both to every step, and a corpus entry never records them (`corpus check`
takes its own). The environment variable
`KICAD_ROUTE_REQUIRE_READONLY_STATE=1` (also `true`, `yes`, `on`) sets it for every
command. As the very first step, before anything is written, a run refuses with exit 4
and a refusal record (`reason_class: state`, `OUT/refusals/`, docs/interface.md) when
STATE:

- does not exist;
- holds no installed backend (`vendor/krt` or `vendor/freerouting`), so it is not a
  kicad-route state directory;
- is not the directory its state manifest (`STATE/manifest.json`, written by `update`
  or `kicad-route state-manifest`) describes: the manifest is missing or malformed,
  its `state_dir` is another directory, or the files on disk have drifted from the
  hashes it records (statemanifest.verify);
- is writable by the process: STATE itself or one of its installed parts
  (`access(2)`, which reports a read-only mount as well as permissions).

A refused run leaves no run directory. `campaign` makes the same check before its first
run and passes the flag to each run. `corpus check` makes the same check before its
first entry and passes the flag to each entry's run; it also refuses when the corpus
root lies inside STATE (the default `STATE/corpus/`), because it writes each entry's
`checks/` there, so a sandboxed check needs `--root` (or `KICAD_ROUTE_CORPUS`) on a
writable copy of the corpus. `update` and `corpus add` write STATE, so they refuse
with exit 4 whatever STATE's state: `update` under the variable, `corpus add` under the
flag or the variable. Without the flag nothing changes.

kicad-route never writes under STATE during `run`, `validate`, `campaign`, `blocked`
or `doctor`. Python runs with bytecode writing off (`bin/kicad-route` and every backend
or KiCad-script subprocess), so no `__pycache__` appears in the checkout,
`vendor/krt` or the venv, and git runs with `--no-optional-locks` so `describe
--dirty` does not rewrite the checkout's index. The tests snapshot every entry's type,
size and mtime under STATE around each fixture run, sandboxed and not, and fail on any
difference. When STATE is also a source checkout (the default), only its installed
parts (`vendor/`, `.venv-krt/`, `corpus/`) are snapshotted, because merges and commits
change the sources and git metadata while tests run. Python bytecode for kicad-route
itself is not written either, so a checkout used as STATE stays clean too.

## Sandbox facts in result.json

Every run records `sandbox`:

| Key | Meaning |
|---|---|
| `require_readonly_state` | the flag or variable was set |
| `state` | `path`, `exists`, `installed`, `writable`, `writable_parts`, `readonly_mount` (statvfs `ST_RDONLY`) |
| `tool_root` | the checkout's path and whether it is writable |
| `out` | OUT |
| `home` | `HOME` as the process saw it, `required: false`, `tool_home` (`OUT/.home`), the variables that redirect KiCad's and the XDG writers' files (`env`) and the JVM options (`jvm`); see [HOME](#home) |
| `network` | interfaces and routes of the process's network namespace (from `/proc/net`), the namespace id, and `connect_probe`: a UDP `connect()` to `192.0.2.1:9` (TEST-NET-1), which asks the kernel for a route and sends no packet. `isolated` is true when only loopback exists and the probe finds no route |

`doctor --offline` reports the same facts without the probe.

## The ledger in a sandbox

The choice is stated in the contract ([interface.md](interface.md), "The ledger in a
sandbox"). The run ledger refuses an exact repeat of a failed attempt (exit 4,
[hygiene.md](hygiene.md)). It lives in `OUT/ledger.jsonl` by default, so with a fresh
OUT per stage it never sees an earlier attempt. Two choices:

- pass `--ledger PATH` (on `run`, `campaign` or `corpus check`) with a path that persists across
  stages and is writable inside the sandbox. The result records the path in
  `ledger.path`. This keeps repeat refusal;
- or accept that repeats are not refused inside the sandbox, and keep the accounting in
  agent-ee.

agent-ee's choice is recorded in its own documentation; kicad-route supports both.

## Open points

- kicad-route's version still comes from `git describe` on the checkout, so the
  checkout's git metadata must be bound (a worktree's common dir is listed in
  `ro_paths`).
