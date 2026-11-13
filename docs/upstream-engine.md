# The routing engine and upstream KRT

The builtin router in `src/kicad_route/router/` is upstream
[KRT](https://github.com/drandyhaas/KiCadRoutingTools) at one recorded commit, plus a
small set of local changes. `src/kicad_route/router/UPSTREAM.json` records both:

- `upstream_commit`, `upstream_describe`, `upstream_commit_date`: the upstream commit the
  engine's upstream files come from;
- `local_changes`: every change to an upstream file, with its purpose and the files it
  touches;
- `local_files`: the engine files that are kicad-route's own (never merged with upstream).

## Moving to a newer upstream commit

One command, from a clean checkout:

```sh
scripts/sync-upstream-engine.py            # to upstream's origin/main
scripts/sync-upstream-engine.py --ref v0.24.0
```

It fetches upstream (a clone under `~/.cache/kicad-route/KiCadRoutingTools`, or
`--upstream CLONE` / `$KICAD_ROUTE_UPSTREAM_CLONE`; the clone is only fetched, never
checked out or edited), exports the recorded commit and the target commit under
`build/sync-upstream-engine/{old,new}`, and then, for every file the engine carries:

- merges it three ways in place (ours, old upstream, new upstream) when both sides changed;
- takes upstream's file when only upstream changed, keeps ours when only we did;
- adds files upstream added, and removes unchanged files upstream removed or the entry
  points no longer import;
- never touches a file listed in `local_files`.

It rewrites `py_router/effective_config_fields.json` from the merged `GridRouteConfig`,
records the new commit in `UPSTREAM.json`, and prints one `CONFLICT` line per file it
could not merge. A conflicted file holds `<<<<<<< local` / `||||||| upstream-old` /
`=======` / `>>>>>>> upstream-new` blocks. The exit status is 1 while a conflict is left.
Nothing is committed.

Then:

1. Resolve the conflicts. Take upstream's side when upstream now does what the local
   change did, and delete that entry from `local_changes`.
2. `scripts/sync-upstream-engine.py --status` lists how the engine differs from the
   recorded commit and fails when that list and `UPSTREAM.json` disagree, or when the
   config field list is stale.
3. Rebuild and check: `kicad-route update` (or `build_router.py --target-dir DIR` with the
   routing venv), `kicad-route doctor`, `make kernel`, `make smoke`.
4. Route real boards with the old and the new engine before landing. The engine's
   search changes with every upstream release; the tests do not measure that.

`--upstream-only` takes upstream's version of every upstream file, dropping the local
changes. It is for measuring what upstream alone does with this package.

## What is carried

The carried set is computed, not listed: the static import closure of the entry points
in `scripts/routing_closure.py` (`route.py`, `route_diff.py` and the diagnostic tools the
generated `krt-*` skills name) in the target tree, plus its `ASSETS` (licence, version,
requirements, the Rust crate, the two adapted skills, two upstream test files the
engine's own tests use). Upstream's GUI plugin, placer and plan tools, bus step (`awx/`),
tests, documentation and release files are not carried. A module upstream starts
importing arrives with the next sync; one it stops importing leaves.

`VERSION` is upstream's release (for example 0.23.9). The Rust crate has its own
version (`rust_router/Cargo.toml`), which upstream raises only when the crate changes;
`doctor` compares the built `grid_router` with the crate, not with `VERSION`.

## Keeping a local change small

A local change costs a merge at every upstream release, so:

- Put local code in a local file (`py_router/sexpr.py`, `native_delay.py`,
  `effective_config_evidence.py`) and leave a few inserted lines in the upstream file
  that call it. Inserted lines merge; rewritten upstream lines conflict.
- Mark the inserted lines `# kicad-route:` with the reason.
- Prefer doing the work outside the engine, in kicad-route's own steps before and after
  the engine runs, when that is possible.
- Add the change to `local_changes` in `UPSTREAM.json`; `--status` checks the file list.

## State on 2026-11-10 (AGENTEE-642)

The engine moved from the extraction base `295f9ee1` (v0.23.1) to `772003a6` (v0.23.9-1),
1,271 upstream commits. Before the move 31 upstream files carried 3,571 changed lines;
after it 12 files carry about 350, in nine listed changes.

Dropped because upstream now covers them:

| Local change (fork commits) | Upstream's own |
| --- | --- |
| Tap points on the grid, KICADROUTE-64 | #1291 |
| No weld bridge on a forbidden layer, KICADROUTE-63 | #1292 |
| Rule floors in every sub-pass, old-spelling pads (9d6f38e, f1b9e44) | #1290 |
| Layer and stub-swap safety (8fae31b) | upstream commits 8c3e8331, 29be549c, 20a8e1b8, 0e0029f4, ca96bc28 |
| Pair members clear of keep-outs (47ed7bd) | a0489b30, later #1729 |
| Tap map lifecycle, phase-3 retry on the current board (8b449c6) | 9ac916bc, ed17b29f |
| Rescued copper of a restored net is written (a4988cd) | 0b3b6a2d |
| One-layer joining via replaced by track (e0871b7 to 4ae1c4a) | #1372 weld |
| Exact T junctions, exact duplicates, terminal to centreline, singleton tap welds (526b167 to 5a3bd1a) | #1217 split, #162 dedup, #1291 |
| Quote-aware block scans in the writer (60e91bf) | #1437, #1440 |
| Pair cleanup before intra-pair matching (86f586c, order only) | #1622 re-match after cleanup |
| Group members already on the board set the target (a748a61, routing_common) | #1391 |

Dropped without an upstream equivalent, because they are not rule-obeying behaviour
and nothing in kicad-route selects them or depends on them:

- the rewrite of the optional `--ripup-blocker-select mincut` probe, with its Rust
  change (a71397d, 422b994). The crate is upstream's, byte for byte;
- parse-local parser caches in place of upstream's memo (176913f);
- roll-back of a phase-3 stub switch when the retry raises (4fb4fa1);
- removal by UUID and exact geometry in the writer (ec91e07), reduced to the
  locked-copper guard that is kept;
- the trims of a track running past its pad (be1f3a8, 5eae9c4 to 995891a).

Their tests went with them. What the kept changes are and why is in `UPSTREAM.json`.

## State on 2026-11-11

Three changes were added after routing real boards with the moved engine
(`lab/README.md`) and running the whole test suite on it; 16 upstream files now
carry about 500 changed lines in twelve listed changes.

| Change | Why upstream's behaviour was not enough |
| --- | --- |
| `class-via` | A net of more than two pads was searched with the Default via and delivered below its class's via size on a board with a power class (15 to 25 vias). Carried over from the fork (AGENTEE-650). |
| `stop-request` | The stop file was honoured only between the nets of the first pass. Three boards of 160 to 480 connections finished that pass inside a 300 s budget, ran on through the later passes, were killed, and no board came back. |

| `pad-anchored-dangle-trim` | A pair 2 mm long came back with each member running 0.5 to 0.7 mm past its far pad (two `track_dangling` findings). Upstream's dangle trim anchors a dead-end track on a via or another track's end, not on a pad. |
| `width-below-floor` | Tracks routed at a board's minimum width of 7.8 mil (0.19812 mm) came back 0.1981 mm wide, 20 nm under it, and KiCad reported each one. Upstream's smoothing and collinear merge group tracks by width rounded to 0.1 um and re-drew a group at the rounded number; a track necked at a pad was rounded the same way. Found when the run began to let a net that is still open go down to the board's minimums (`docs/routing.md`, the third closing round). |

The fork's own trim of a track that runs past its pad (92 lines) stays dropped. The
third change is 19 lines inside upstream's dangle trim and uses its connectivity
gate and its handling of input copper.
