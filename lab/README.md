# Router lab

One job: find what the router gets wrong on real boards, and show whether a
change fixed it without breaking something else.

```sh
lab/lab.py add stickhub ~/boards/StickHub/StickHub.kicad_pcb      # once per board
lab/lab.py run before                                            # route every board with this checkout
lab/lab.py issues before stickhub                                # every issue, one line each
lab/lab.py opens before                                          # what is open: plane nets, pour nets, others
lab/lab.py passes before stickhub                                # each engine pass: how far the budget let it get
lab/lab.py why before --board stickhub --list                    # each open connection: the router's doing or the board's
# ... change the engine ...
lab/lab.py run after
lab/lab.py diff before after                                     # issues gone, issues new
lab/lab.py show before after upstream                            # the table
lab/lab.py run upstream --stock ~/src/KiCadRoutingTools -- --escalation board --no-fix-drc-settings
```

Start a round with `mise run lab:run -- TAG ...` (the same arguments as
`lab/lab.py run`): the round then asks the host for one unit of capacity per
job and is stopped, with everything it started, at its deadline. Rounds
started directly ran beside other agents' tests at a load of 20 to 30 on 12
processors, which slows every run on the host and theirs most.

## What a board is

A designer's board with its own rule files. `add` keeps two copies under
`$KICAD_ROUTE_LAB/corpus/NAME` (default `~/.local/share/kicad-route-lab`):

- `reference/`: the board as its designer routed it. Its native DRC is the
  yardstick: a board the designer routed with no error is one the router should
  route with no error.
- `input/`: the same board with tracks, vias and teardrops removed. Pours,
  parts and rules stay. This is what the router gets.

The corpus is never committed. The boards are other people's designs.

## What an issue is

`run` routes each input through `kicad-route run --backend builtin --protect
existing`, as a caller does, and writes one record per thing that went wrong
(`runs/TAG/NAME/issues.json`):

| Kind | Meaning |
|---|---|
| `open` | a connection the router left unrouted, with both ends |
| `drc` | a native DRC finding the unrouted input did not have, with its items and positions |
| `grading` | a kicad-route check on the candidate that failed (a class size, a moved part, a starved pour) |
| `quality` | the board passes every rule and is still not what its designer would accept: `plane_layer_tracks`, tracks on an inner layer the designer kept for planes (under 2 % of their track length there, more than 2 % of the router's) |
| `process` | the backend failed, or reported that it delivered something below a requirement |
| `advisory` | listed and never counted against a board: `below_class`, new copper below its net class size. KiCad's DRC enforces the board's minimums and rules; six of ten designers' boards of the corpus have such tracks |

A board is clean when it has none of the first four and the process ran.

The designer's own board is graded by the same rule in the "As designed" column: the
connections it leaves open and the DRC errors the unrouted input does not have. Four of
the ten designer-routed boards of the corpus are not clean by it (12 clearance errors
on one, 2 starved thermals on another, 199 vias below the board's own minimum drill on
a third, 4 clearance errors, 3 shorts and an open connection on the fourth). The line
under the table says on how many boards a run is clean among those the designer got
clean, among those the designer did not, and among those that came unrouted: a board
its designer did not get clean under its own rules is weaker evidence against a router
than one the designer did.
A table cell is `open / new errors`, plus the failed grading checks. After the
copper and the time it says where the budget cut the engine off, and what the wrapper
cleared up after the engine, each a thing the engine left: `N dangling removed` (new
tracks and vias ending in the air), `N pour stitched` (a piece of pour that new tracks
ringed off around one pad, joined to its net again with a via), `N pour left cut off`
(such a piece with no place for a via; KiCad's finding for its pad stands) and
`N starved thermal repaired` (pads the run's own copper had left short of spokes, whose
nets were routed once more round them; `docs/spokes.md`). `N closed in the time left`
is not a thing the engine left but what the run did with the budget the engine did not
use: connections closed by the rounds after it (`docs/routing.md`).

A run is of one commit. `run` copies the checkout's committed files to
`$KICAD_ROUTE_LAB/snap/frozen-COMMIT` and routes with the copy, so the checkout
can be edited while a run of an hour is going; uncommitted changes to tracked
files are refused. (kicad-route binds its grading to the hashes of its own
source files. An edit during a run once made three boards fail a completion
check the router had no part in.) The engine's built environment
(`.venv-krt`) is not in git; the copy links to the checkout's, and `run.json`
records the hash of the built extension.

## The loop

1. `run` a tag for the checkout as it is.
2. Pick the issue class that costs the most boards. Read its lines with `issues`.
   The run directory printed with each board holds the routed candidate
   (`kr/runs/NAME/lab/STAMP/candidate/`), the engine's log (`work/`), the
   findings (`validate/new-findings.json`); `kicad-route blocked RUN_DIR` names
   what lies across a connection left open.
   Two views say why a board is not done before any log is read. `opens`
   sorts the open connections by the kind of net: on a four-layer board 50 of
   56 were pads of the two plane nets, boxed in by signals routed before them,
   and the count alone had looked like a board too dense for the engine.
   `passes` lists each engine pass of a cell with its nets, its budget, the
   processor time it used and the step of the engine's procedure the budget
   stopped it in. A run stopped in its taps has rescued no net and repaired no
   plane, and a few seconds of budget move that point: two runs of one board
   12 s apart came out at 83 and 122 open connections.
   For each pass it also counts the path searches that found no route and says
   how many of them gave up at the engine's search allowance (200 000 steps a
   direction, about 0.3 s) instead of running out of cells to try. The engine
   prints "No route found" for both, and only the second kind says the pad is
   closed in. In a run of a four-layer board that used 760 of its 1800 s, 19
   of 142 failed searches had given up, on six nets, and three of those nets
   were among the seven left open; one search gave up straight after the
   engine had ripped the net that caged its pad, and the rip was undone.
   `why` says whose doing each open connection is. It runs the engine's own
   reachability check (`py_tools/check_reachability.py`: the widest track that
   reaches the rest of the net from a pad, on the placement alone and with the
   routing in place) from one pad of every group of pads the routed board
   leaves off its net, and sorts the answers. "Caged by copper this run laid":
   a track of the net's class reaches the rest of the net on the board as
   placed, and the routing closed the way; `closed off by` counts the nets and
   pads at the narrowest place. "Still reachable on the routed board": the way
   is there with all the routing in place, and the router did not take it; the
   line below says how much wider than the net's track that way is, and how
   many have under 100 um, less than the engine's grid step. "With nothing in
   the way inside the check's view": no answer. The check met no foreign
   copper within 8 mm, which is what a through-hole pad gives (it reaches its
   own net on the other layer at once), so a through-hole board gets few
   answers from this view.
   "Caged by the placement": the check finds no way at the class sizes whatever
   is routed. That one grades the engine's model at class sizes: on one corpus
   board it says so of two pads the designer reached with a 0.15 mm track where
   the class asks 0.25 mm, so read it as "not at class sizes", not as
   "impossible". The answers are kept in the cell's `why.json`; a connection
   takes some ten seconds. On a two-layer hub board all three open connections
   are the first kind, pins of a 0.5 mm pitch package behind tracks of their
   neighbours.
3. Fix the engine for the class, not for the board. Put the smallest board
   that shows the defect into `src/kicad_route/router/tests/` as a regression
   script (see `synth.py`).
4. `run` a new tag and `diff` it against the last one. A change lands when
   its issues are gone and no board has a new one.

## The opponent

`run --stock CLONE` routes the same inputs with an unmodified
KiCadRoutingTools clone and scores its boards by the same native DRC. Keep
that tag beside ours in `show`: the router is worth its upkeep only while it
routes these boards better than upstream does.

Both get the same time: `--budget` seconds, then the grace kicad-route
allows before it kills a backend. Stock `route.py` cannot be asked to stop and
write what it has, so where it needs longer its cell says so and has no board.
Compare columns only at the same budget.

A budget counts the engine's processor seconds (`kicad-route run
--budget-clock cpu`; `--clock wall` for wall seconds), because the host is
shared: the engine is deterministic, and two runs of one commit on the largest
board of the corpus came out at 88 and 122 open connections only because a
wall-time stop landed at different points of the same pass. Each cell gives
the processor seconds the engine used, and the table's footer the host's load
range during the run.

A cell that says `cut off in` is the budget's count, not the engine's: the same
commit and board give another count when the stop lands in another step, and a
change that makes the engine a little faster or slower moves it. Three boards
of the corpus are cut off at 300 s, and one of them came out at 20, 21, 26 and
37 open connections from four engine variants that differ in nothing that
touches it. Judge a change to the engine on those boards with a budget at which
the engine ends on its own (1800 s for two of them, 3600 s for the largest);
there two runs of one commit give the same board.
