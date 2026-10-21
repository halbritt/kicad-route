"""kicad-route campaign: route the hard nets first, automatically.

A single attempt routes its whole scope in the backend's own order, and with
`--protect existing` a later attempt cannot move copper an earlier one placed, so
nets left open by a full-board attempt rarely finish on that candidate. A campaign
re-orders the work instead (it never touches a rule):

    round 0   one ordinary attempt over the scope, on the input
              (--race K adds K two-pass orderings in parallel: a "first" subset
              chosen by pad count, pad span or net class, then the rest)
    round k   the backlog (every net left unrouted in any earlier round) routed
              FIRST on the original input, then the rest of the scope routed from
              that candidate (--dir), and the chain end graded against the
              original input (`validate --against`)

It stops when a round is complete, the backlog stops growing (the next round would
repeat this one), --rounds hardfirst rounds are done, or --total-budget has run
out. Every step is an ordinary `kicad-route run` subprocess with the campaign's own
options passed through (the input options, --library-dir included, on every step),
so each step has its own run directory and result. With --total-budget a step's
--budget is cut to the time left, and every subprocess has a timeout (see
step_limits), so a hung step cannot hold the campaign.

Each step runs in machine mode (`--json`): the campaign reads the step's result
record from its stdout, never its progress lines (docs/interface.md).

Output: OUT/campaigns/<project>/<label>/<stamp>/campaign.json and CAMPAIGN.md.
Exit status: the best chain's (0 complete and valid, 2 valid but incomplete,
3 fails validation), 1 when no chain produced a gradable candidate, or 4 when
the campaign was refused before any backend ran (its round-0 run was refused,
or a usage or input error); a refusal leaves no campaign directory.
"""
import collections
import concurrent.futures
import json
import math
import argparse
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import backend_names
from . import common
from . import contract

STRATEGIES = ("hardfirst",)
ORDERINGS = ("pads", "span", "netclass")  # --race lanes, in this order
DEFAULT_CLASS = "Default"
CLI = [sys.executable, "-m", "kicad_route"]
# Subprocess timeouts (step_limits). After its --budget a run's backend gets
# run_budgeted's stop grace; then the run grades its candidate (snapshot, refill,
# DRC, parity, preservation). GRADE_ALLOWANCE_S bounds that with --total-budget;
# without one only a hung step is stopped, after HANG_ALLOWANCE_S (the run's own
# limit for one KiCad DRC).
GRADE_ALLOWANCE_S = 600
HANG_ALLOWANCE_S = 1800

_print_lock = threading.Lock()


def log(msg):
    with _print_lock:
        print(msg, flush=True)


# ------------------------------------------------------------------ arguments

def add_parser(sub, cli):
    """The `campaign` subcommand: the routing and environment options of `run`
    (cli.add_route_args, cli.add_environment_args, passed to every step; --nets/--netclass
    are the campaign scope), plus campaign controls."""
    p = sub.add_parser("campaign", help="hard-nets-first routing campaign: several attempts, best chain graded "
                                        "against the original input")
    p.add_argument("--backend", type=backend_names.parse, default="builtin",
                   choices=backend_names.CHOICES, help=backend_names.HELP)
    cli.add_input_args(p)
    cli.add_route_args(p)
    cli.add_environment_args(p)   # forwarded to every step too (cli.route_argv)
    p.add_argument("--label", help="campaign name (default campaign-<backend>); runs are labelled <label>-<step>")
    p.add_argument("--strategy", choices=STRATEGIES, default="hardfirst",
                   help="hardfirst (default): route the backlog first on the original input, then the rest")
    p.add_argument("--rounds", type=int, default=2, metavar="N",
                   help="hardfirst rounds after round 0 (default 2)")
    p.add_argument("--race", type=int, default=0, metavar="K", choices=range(len(ORDERINGS) + 1),
                   help=f"round 0 also tries K two-pass orderings in parallel ({', '.join(ORDERINGS)}; default 0). "
                        "Parallel lanes share the CPU, so each wall-clock budget buys less search")
    p.add_argument("--total-budget", type=int, metavar="SECONDS",
                   help="wall clock for the whole campaign: no new run starts after it, each run's --budget "
                        "is cut to the time left, and a run still going a grace period after it is stopped")
    return p


# ---------------------------------------------------------------- net scopes

def net_pattern(name):
    """An fnmatch pattern (for --nets) that matches exactly this net name."""
    out = "".join(f"[{c}]" if c in "*?[" else c for c in name)
    if out.startswith("!"):
        out = "[\\!]" + out[1:]   # a leading ! would mean "exclude"
    elif out.startswith("-"):
        out = "[-]" + out[1:]     # a leading - would look like an option
    return out


def scope_args(args, mode, nets=()):
    """--nets/--netclass for one step. mode: full (the campaign scope), only (exactly
    `nets`), rest (the campaign scope minus `nets`; select_nets applies exclusions last)."""
    if mode == "only":
        return ["--nets", *[net_pattern(n) for n in sorted(nets)]]
    patterns = list(args.nets or [])
    if mode == "rest":
        patterns += ["!" + net_pattern(n) for n in sorted(nets)]
    out = ["--nets", *patterns] if patterns else []
    return out + (["--netclass", *args.netclass] if args.netclass else [])


def net_pads(facts, scope):
    pads = collections.defaultdict(list)
    for fp in facts["footprints"].values():
        for pad in fp["pads"].values():
            if pad["net"] in scope:
                pads[pad["net"]].append(pad["position"])
    return pads


def ordering_first(facts, scope, ordering):
    """The "first" subset of a race lane, or None when the ordering does not split the scope."""
    scope = set(scope)
    pads = net_pads(facts, scope)
    eligible = sorted(n for n in scope if len(pads[n]) >= 2)
    quarter = max(1, math.ceil(len(eligible) / 4))
    if ordering == "netclass":   # constrained (non-Default) classes first
        first = {n for n in eligible if facts["nets"][n]["netclass"] != DEFAULT_CLASS}
    elif ordering == "pads":     # most pads first
        first = set(sorted(eligible, key=lambda n: (-len(pads[n]), n))[:quarter])
    elif ordering == "span":     # widest pad bounding box (half perimeter) first
        def span(n):
            xs, ys = [p[0] for p in pads[n]], [p[1] for p in pads[n]]
            return (max(xs) - min(xs)) + (max(ys) - min(ys))
        first = set(sorted(eligible, key=lambda n: (-span(n), n))[:quarter])
    else:
        raise ValueError(ordering)
    return sorted(first) if first and first != scope else None


# ------------------------------------------------------------------- ranking

def rank_key(chain):
    """Best first: a graded candidate; complete and valid; no preservation failure (an
    unadoptable board never wins on counts); fewer unconnected in scope; fewer new DRC
    and parity findings; shorter runtime."""
    g = chain.get("grade")
    if not g:
        return (1, 1, 1, math.inf, math.inf, math.inf)
    v = g["validation"]
    return (0, g["exit_code"] != 0, len(v["preservation_failures"]) > 0,
            g["routing"]["unconnected_in_scope"] if g["routing"]["unconnected_in_scope"] is not None else math.inf,
            v["new_violations"] + v["new_parity"], chain["runtime_s"])


def leaderboard(chains):
    rows = []
    for i, c in enumerate(sorted(chains, key=rank_key), 1):
        g = c.get("grade") or {}
        rt, v = g.get("routing", {}), g.get("validation", {})
        rows.append({"rank": i, "chain": c["id"], "round": c["round"], "strategy": c["strategy"],
                     "status": c["status"], "exit_code": g.get("exit_code", 1),
                     "routing": rt.get("status"), "unconnected_in_scope": rt.get("unconnected_in_scope"),
                     "unconnected_total": rt.get("unconnected_total"),
                     "validation": v.get("status"), "new_violations": v.get("new_violations"),
                     "new_parity": v.get("new_parity"), "preservation_failures": v.get("preservation_failures"),
                     "runtime_s": c["runtime_s"], "end_run": c["steps"][-1].get("run_dir") if c["steps"] else None,
                     "candidate": g.get("candidate")})
    return rows


# --------------------------------------------------------------------- steps

def stop_grace(budget):
    """The most run_budgeted lets a backend run past `budget`: the clean-stop grace
    (max(30, budget/5) s), then terminate (10 s) and kill (5 s)."""
    return max(30, budget // 5) + 15


def kill_group(proc, wait=10):
    """Stop a step and everything it started (its own process group): SIGTERM,
    then SIGKILL; returns the exit code."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            break
        try:
            proc.wait(timeout=wait)
            break
        except subprocess.TimeoutExpired:
            continue
    try:
        os.killpg(proc.pid, signal.SIGKILL)   # stragglers the leader left behind
    except ProcessLookupError:
        pass
    return proc.wait()


class Campaign:
    def __init__(self, args, cli):
        self.args, self.cli = args, cli
        self.cfg = cli.resolve_input(args)        # validates --dir/--board/--rev like run does
        self.project = self.cfg["name"]
        self.label = args.label or f"campaign-{args.backend}"
        self.dir = common.out_dir() / "campaigns" / self.project / self.label / \
            common.now_utc().strftime("%Y%m%dT%H%M%S.%fZ")
        self.dir.mkdir(parents=True)
        common.point_latest(self.dir.parent / "latest", self.dir.name)
        self.started = time.monotonic()
        self.deadline = self.started + args.total_budget if args.total_budget else None
        self.cwd = Path.cwd()
        self.fingerprint = None   # the original input's, from round 0
        self.scope = None         # the campaign scope on the original input (net names)
        self.chains, self.rounds, self.backlog_history = [], [], []
        self.stop_reason = None
        self.timeouts = []            # every subprocess the campaign stopped at its timeout
        self.procs, self.procs_lock = set(), threading.Lock()

    # paths in records are relative to the working directory, like run results
    def path(self, p):
        p = Path(p)
        return p if p.is_absolute() else self.cwd / p

    def out_of_time(self):
        return self.deadline is not None and time.monotonic() >= self.deadline

    def time_left(self):
        return None if self.deadline is None else self.deadline - time.monotonic()

    def step_limits(self):
        """(--budget, subprocess timeout) for a run starting now. Without --total-budget:
        the campaign's --budget, and a timeout for a hung step only (budget + stop grace +
        HANG_ALLOWANCE_S). With it: the budget is cut to the time left (at least 1 s),
        and the run is stopped at the deadline plus its grace (stop grace +
        GRADE_ALLOWANCE_S), so no step overruns the total budget by more than that."""
        left = self.time_left()
        if left is None:
            budget = self.args.budget
            return budget, budget + stop_grace(budget) + HANG_ALLOWANCE_S
        budget = max(1, min(self.args.budget, int(left)))
        return budget, round(max(left, budget) + stop_grace(budget) + GRADE_ALLOWANCE_S, 3)

    def validate_timeout(self):
        """`validate --against` of a finished chain: the time left plus GRADE_ALLOWANCE_S
        (a chain whose last run ended at the deadline is still graded), or
        HANG_ALLOWANCE_S without --total-budget."""
        left = self.time_left()
        return HANG_ALLOWANCE_S if left is None else round(max(0.0, left) + GRADE_ALLOWANCE_S, 3)

    def common_argv(self, budget=None):
        """Every routing option of the campaign except its scope (cli.route_argv), for
        each step; `budget` replaces --budget (cut to the total budget left)."""
        a = self.args if budget is None else argparse.Namespace(**{**vars(self.args), "budget": budget})
        return self.cli.route_argv(a)

    def input_argv(self, prev=None):
        """The original input (as given to the campaign), or a previous step's candidate.
        The --library-dir mappings go to every step: a candidate keeps the mapped
        directory as a link to the library, and the step's snapshot re-links it
        (snapshot.make_snapshot) instead of copying the library into the input."""
        a = self.args
        libs = [x for e in a.library_dir or [] for x in ("--library-dir", e)]
        if prev is None:
            argv = ["--dir", str(self.cfg["path"]), "--board", self.cfg["pcb"], "--name", self.project]
            if a.schematic:
                argv += ["--schematic", a.schematic]
            return argv + libs + (["--rev", a.rev] if a.rev else [])
        argv = ["--dir", str(self.path(prev["candidate_dir"])), "--board", prev["board"], "--name", self.project]
        return argv + (["--schematic", prev["schematic"]] if prev.get("schematic") else []) + libs

    def invoke(self, argv, log_name, timeout=None):
        """One kicad-route subcommand in machine mode, in its own process group, stopped
        with everything it started (backend, kicad-cli) when `timeout` seconds pass. Its
        progress (stderr) goes to logs/<name>.log, its one JSON record (stdout) to
        logs/<name>.json. Returns (rec, log text); rec["_record"] holds that record parsed
        (None if it printed none), for the caller to pop: it is not kept in campaign.json."""
        logf = self.dir / "logs" / f"{log_name}.log"
        logf.parent.mkdir(parents=True, exist_ok=True)
        full = [*CLI, "--out", str(common.out_dir()), argv[0], "--json", *argv[1:]]
        t0 = time.monotonic()
        timed_out = False
        recf = logf.with_suffix(".json")
        with open(logf, "w") as err, open(recf, "w") as out:
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            # The checkout launcher adjusts sys.path in-process. Give children the
            # same package location; installed wheels resolve here as well.
            env["PYTHONPATH"] = str(common.PACKAGE_ROOT.parent) + os.pathsep + env.get("PYTHONPATH", "")
            proc = subprocess.Popen(full, cwd=self.cwd, env=env, stdout=out, stderr=err, start_new_session=True)
            with self.procs_lock:
                self.procs.add(proc)
            try:
                rc = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                rc = kill_group(proc)
            except BaseException:   # interrupted: its own session would not see the terminal's signal
                kill_group(proc, wait=5)
                raise
            finally:
                with self.procs_lock:
                    self.procs.discard(proc)
        try:
            data = json.loads(recf.read_text())
        except (OSError, ValueError):
            data = None
        if not isinstance(data, dict):
            data = None
        text = logf.read_text(errors="replace")
        if data is None:  # not a machine-mode record: keep whatever it printed with the log
            text += recf.read_text(errors="replace") if recf.exists() else ""
        rec = {"argv": full, "exit_code": rc, "log": common.rel(logf), "record": common.rel(recf),
               "wall_s": round(time.monotonic() - t0, 3), "timeout_s": timeout, "timed_out": timed_out,
               "_record": data}
        if timed_out:
            with self.procs_lock:
                self.timeouts.append({"log": rec["log"], "timeout_s": timeout, "wall_s": rec["wall_s"],
                                      "argv": full})
            log(f"[campaign] {log_name}: stopped at its timeout ({timeout} s)")
        return rec, text

    @staticmethod
    def failure(rec, data, text):
        """Why a step returned no usable record: its timeout, its refusal, else the log tail."""
        if rec.get("timed_out"):
            return f"stopped at the campaign's timeout ({rec['timeout_s']} s) before a result"
        if data and data.get("kind") == "refusal":
            return f"refused ({data.get('reason_class')}): {data.get('message')}"
        return "; ".join([l for l in text.splitlines() if l.strip()][-5:]) or f"exit {rec['exit_code']}"

    def stop_all(self):
        """Stop every running step (the campaign is being interrupted)."""
        with self.procs_lock:
            procs = list(self.procs)
        for proc in procs:
            kill_group(proc, wait=5)

    def step(self, chain_id, role, scope_mode, nets=(), prev=None, intended=None):
        """One `kicad-route run`; returns its record (never raises for a failed run)."""
        label = f"{self.label}-{chain_id}-{role}" if role != "full" else f"{self.label}-{chain_id}"
        budget, timeout = self.step_limits()
        argv = ["run", "--backend", self.args.backend, *self.input_argv(prev), *scope_args(self.args, scope_mode, nets),
                *self.common_argv(budget), "--label", label]
        log(f"[campaign] {chain_id}: {role} pass ({self.describe_scope(scope_mode, nets)}) on "
            + ("the original input" if prev is None else f"the candidate of {prev['run_dir']}")
            + (f"; budget cut to {budget} s (total budget left)" if budget < self.args.budget else ""))
        rec, text = self.invoke(argv, label, timeout)
        data = rec.pop("_record", None)
        rec.update(role=role, label=label, on="input" if prev is None else "previous candidate", budget_s=budget)
        if scope_mode == "only":
            rec["nets"] = sorted(nets)
        elif scope_mode == "rest" and nets:
            rec["excluded_nets"] = sorted(nets)
        result = data if data and data.get("kind") == "route" else None
        run_dir = self.path(result["run_dir"]) if result else None
        attempt = common.load_json(run_dir / "attempt.json") if run_dir else None
        rec["run_dir"] = common.rel(run_dir) if run_dir else None
        if attempt is None or result is None:
            rec.update(status="error", error=self.failure(rec, data, text))
            if data and data.get("kind") == "refusal":
                rec["refusal"] = data
            log(f"[campaign] {chain_id}: {role} pass did not produce a result: {rec['error']}")
            return rec
        rt, v = result["routing"], result["validation"]
        rec.update(status="ok", source_dir=attempt["source"].get("source_dir"),
                   input_fingerprint=attempt["input_fingerprint"], board=attempt["board"],
                   schematic=attempt.get("schematic"), scope_nets=len(attempt["scope"]["nets"]),
                   process=result["process"]["status"], routing=rt["status"],
                   unconnected_in_scope=rt.get("unconnected_in_scope"), unrouted_nets=rt.get("unrouted_nets"),
                   validation=v["status"], runtime_s=result["runtime_s"],
                   candidate_dir=result["candidate"]["dir"] if result.get("candidate") else None)
        rec["exit_code"] = result["exit_code"]
        if intended is not None and set(attempt["scope"]["nets"]) != set(intended):
            rec.update(status="error", error=f"scope mismatch: ran {len(attempt['scope']['nets'])} nets, "
                                             f"intended {len(intended)}")
        log(f"[campaign] {chain_id}: {role} -> exit {rec['exit_code']}, {rec['routing']}"
            + (f" ({rec['unconnected_in_scope']} unconnected in scope)" if rec["routing"] == "INCOMPLETE" else "")
            + f", validation {rec['validation']}  [{rec['run_dir']}]")
        return rec

    def describe_scope(self, mode, nets):
        if mode == "only":
            return f"{len(nets)} nets"
        if mode == "rest":
            return f"scope minus {len(nets)} nets"
        return "campaign scope"

    def grade(self, chain):
        """Grade the chain end against the chain's first run (the original input)."""
        steps = chain["steps"]
        first, end = steps[0], steps[-1]
        if len(steps) == 1:
            res_path = self.path(end["run_dir"]) / "result.json"
            by = "run"
        else:
            rec, text = self.invoke(["validate", str(self.path(end["run_dir"])), "--against",
                                     str(self.path(first["run_dir"]))], f"{self.label}-{chain['id']}-validate",
                                    self.validate_timeout())
            data = rec.pop("_record", None)
            chain["validate"] = rec
            if not data or data.get("kind") != "route-validate":
                chain.update(status="error", error=(
                    f"validate --against stopped at the campaign's timeout ({rec['timeout_s']} s)"
                    if rec["timed_out"] else "validate --against produced no result: "
                    + self.failure(rec, data, text)))
                return
            res_path = self.path(data["dir"]) / "result.json"
            by = "validate --against"
        res = common.load_json(res_path)
        v, rt = res["validation"], res["routing"]
        drc = v.get("drc") or {}
        end_result = common.load_json(self.path(end["run_dir"]) / "result.json")
        chain["grade"] = {
            "by": by, "result": common.rel(res_path), "against": first["run_dir"],
            "chain": v.get("chain") or [end["run_dir"]], "exit_code": res["exit_code"],
            "candidate": end_result["candidate"]["board"],
            "routing": {k: rt.get(k) for k in ("status", "unconnected_in_scope", "unconnected_total", "unrouted_nets")},
            "validation": {"status": v["status"], "reasons": v.get("reasons", [])[:10],
                           "new_violations": (drc.get("new_violations") or {}).get("count", 0),
                           "new_parity": (drc.get("new_parity") or {}).get("count", 0),
                           "preservation_failures": [k for k, s in (v.get("preservation") or {}).items()
                                                     if s != common.PASS]}}
        chain["status"] = "graded"
        g = chain["grade"]
        log(f"[campaign] {chain['id']}: graded against the original input -> exit {g['exit_code']}, "
            f"{g['routing']['unconnected_in_scope']} unconnected in scope, "
            f"{g['validation']['new_violations']} new violations")

    def run_chain(self, chain_id, rnd, strategy, passes):
        """passes: [(role, scope_mode, nets, intended)]; the first runs on the original
        input, each later one on the previous pass's candidate."""
        chain = {"id": chain_id, "round": rnd, "strategy": strategy, "steps": [], "status": None, "runtime_s": 0.0}
        prev = None
        for role, mode, nets, intended in passes:
            if self.out_of_time():
                chain.update(status="stopped", error="total budget reached before the " + role + " pass")
                log(f"[campaign] {chain_id}: total budget reached; {role} pass not started")
                break
            rec = self.step(chain_id, role, mode, nets, prev, intended)
            chain["steps"].append(rec)
            chain["runtime_s"] = round(chain["runtime_s"] + rec["wall_s"], 3)
            if rec["status"] != "ok":
                chain.update(status="error", error=rec["error"])
                break
            if prev is None and self.fingerprint and rec["input_fingerprint"] != self.fingerprint:
                chain.update(status="input_changed", error="the input changed during the campaign "
                             f"(fingerprint {rec['input_fingerprint'][:16]}, round 0 had {self.fingerprint[:16]})")
                break
            if rec["candidate_dir"] is None:
                chain.update(status="no_candidate", error=f"{role} pass produced no candidate")
                break
            prev = rec
        else:
            t0 = time.monotonic()
            self.grade(chain)
            chain["runtime_s"] = round(chain["runtime_s"] + time.monotonic() - t0, 3)
        return chain

    # ---------------------------------------------------------------- rounds

    def prepare_race(self):
        """Input facts for the race orderings, and the baseline warmed so parallel runs share it."""
        cli = self.cli
        common.ensure_kicad_config()
        ref_dir, manifest = cli.snapshot_input(self.cfg, self.dir / "input")
        facts, rec = cli.board_facts(ref_dir / self.cfg["pcb"], self.dir / "input" / "facts.json")
        if facts is None:
            contract.refuse("input", f"could not read the input board {self.cfg['pcb']} (facts.py exit "
                            f"{rec['exit_code']}): {self.cli.log_tail(self.dir / 'input' / 'facts.log')}")
        self.fingerprint = cli.input_fingerprint(manifest)
        try:
            self.scope = cli.select_nets(facts, self.args.nets, self.args.netclass)[0]
        except cli.NetSelectionError as e:
            contract.refuse("input", str(e))
        cli.baseline_drc(ref_dir, self.cfg["pcb"], self.fingerprint,
                         pcbnew_build=facts.get("kicad_version"))
        lanes = []
        for ordering in ORDERINGS:
            first = ordering_first(facts, self.scope, ordering) if self.scope else None
            if first is None:
                log(f"[campaign] race: ordering '{ordering}' does not split this scope; skipped")
                continue
            lanes.append((ordering, first))
        return lanes[:self.args.race]

    def round0(self):
        lanes = self.prepare_race() if self.args.race else []
        plans = [("r0", "full", [("full", "full", (), self.scope)])]
        for ordering, first in lanes:
            rest = sorted(set(self.scope) - set(first))
            plans.append((f"r0-{ordering}", f"race:{ordering}",
                          [("first", "only", first, first), ("rest", "rest", first, rest)]))
        if len(plans) == 1:
            chains = [self.run_chain(plans[0][0], 0, plans[0][1], plans[0][2])]
        else:
            log(f"[campaign] round 0: {len(plans)} chains in parallel")
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(plans)) as pool:
                try:
                    chains = list(pool.map(lambda p: self.run_chain(p[0], 0, p[1], p[2]), plans))
                except BaseException:   # interrupted: stop the lanes, or shutdown would wait for them
                    self.stop_all()
                    raise
        r0 = chains[0]["steps"][0] if chains[0]["steps"] else None
        refused = [s["refusal"] for c in chains for s in c["steps"] if s.get("refusal")]
        if refused and not any(s.get("status") == "ok" for c in chains for s in c["steps"]):
            # nothing was routed: the campaign is refused for the same reason as its round-0 run
            contract.refuse(refused[0]["reason_class"], f"round-0 run refused: {refused[0]['message']}",
                            input_fingerprint=refused[0].get("input_fingerprint"))
        if r0 and r0["status"] == "ok":
            self.fingerprint = self.fingerprint or r0["input_fingerprint"]
            if self.scope is None:
                attempt = common.load_json(self.path(r0["run_dir"]) / "attempt.json")
                self.scope = attempt["scope"]["nets"]
        return chains

    def finish_round(self, rnd, chains, backlog_used):
        self.chains += chains
        graded = [c for c in chains if c.get("grade")]
        unrouted = sorted({n for c in graded for n in c["grade"]["routing"]["unrouted_nets"] or []})
        backlog = sorted(set(backlog_used or ()) | set(unrouted))
        self.rounds.append({"round": rnd, "backlog_routed_first": backlog_used, "chains": [c["id"] for c in chains],
                            "unrouted_after": unrouted, "backlog_next": backlog})
        self.backlog_history.append({"round": rnd, "size": len(backlog), "nets": backlog})
        self.save()
        return graded, backlog

    def execute(self):
        a = self.args
        log(f"[campaign] {a.strategy} on {self.project} with {a.backend} -> {common.rel(self.dir)}")
        graded, backlog = self.finish_round(0, self.round0(), None)
        if any(c["status"] == "input_changed" for c in self.chains):
            self.stop_reason = "input changed during the campaign"
        elif not graded:
            self.stop_reason = "round 0 produced no gradable candidate" + (
                " (total budget reached)" if self.out_of_time() else "")
        elif any(c["grade"]["routing"]["status"] == "COMPLETE" for c in graded):
            self.stop_reason = "complete in round 0"
        rnd = 0
        while self.stop_reason is None:
            if rnd >= a.rounds:
                self.stop_reason = f"{a.rounds} hardfirst rounds done"
                break
            if self.out_of_time():
                self.stop_reason = "total budget reached"
                break
            rnd += 1
            rest = sorted(set(self.scope) - set(backlog))
            passes = [("backlog", "only", backlog, backlog)]
            if rest:
                passes.append(("rest", "rest", backlog, rest))
            log(f"[campaign] round {rnd}: backlog of {len(backlog)} nets first on the original input, "
                f"then {len(rest)} more nets from that candidate")
            chain = self.run_chain(f"r{rnd}", rnd, a.strategy, passes)
            graded, new_backlog = self.finish_round(rnd, [chain], backlog)
            if chain["status"] == "input_changed":
                self.stop_reason = "input changed during the campaign"
            elif graded and graded[0]["grade"]["routing"]["status"] == "COMPLETE":
                self.stop_reason = f"complete in round {rnd}"
            elif chain["status"] == "stopped":
                self.stop_reason = "total budget reached"
            elif new_backlog == backlog:
                self.stop_reason = "backlog did not grow (the next round would repeat this one)"
            backlog = new_backlog
        return self.conclude()

    # ---------------------------------------------------------------- output

    def best(self):
        ranked = sorted(self.chains, key=rank_key)
        return ranked[0] if ranked and ranked[0].get("grade") else None

    def record(self):
        best = self.best()
        a = self.args
        return {
            "schema": contract.schema_id("route-campaign"), "kind": "route-campaign", "project": self.project, "label": self.label, "strategy": a.strategy,
            "backend": a.backend, "campaign_dir": common.rel(self.dir),
            "input": {"dir": str(self.cfg["path"]), "board": self.cfg["pcb"], "rev": a.rev,
                      "fingerprint": self.fingerprint},
            "scope": {"net_patterns": a.nets or [], "netclasses": a.netclass or [],
                      "nets": len(self.scope) if self.scope is not None else None},
            "settings": {"rounds": a.rounds, "race": a.race, "total_budget_s": a.total_budget,
                         "per_run": self.common_argv(),
                         "timeouts": {"grade_allowance_s": GRADE_ALLOWANCE_S if a.total_budget else None,
                                      "hang_allowance_s": None if a.total_budget else HANG_ALLOWANCE_S}},
            "rounds": self.rounds, "backlog_evolution": self.backlog_history, "chains": self.chains,
            "timeouts": list(self.timeouts),
            "leaderboard": leaderboard(self.chains), "stop_reason": self.stop_reason,
            "best": None if best is None else {
                "chain": best["id"], "round": best["round"], "strategy": best["strategy"],
                "run_dir": best["steps"][-1]["run_dir"], "candidate": best["grade"]["candidate"],
                "graded_against": best["grade"]["against"], "grade": best["grade"]["result"],
                "exit_code": best["grade"]["exit_code"],
                # the graded record's hashes and tool identity (evidence.py), as run/validate carry them
                **{k: (common.load_json(best["grade"]["result"]) or {}).get(k) for k in ("evidence", "tool_identity")}},
            "exit_code": best["grade"]["exit_code"] if best else 1,
            "runtime_s": round(time.monotonic() - self.started, 3),
            "finished_utc": common.now_utc().isoformat() if self.stop_reason else None,
        }

    def save(self):
        rec = self.record()
        common.write_json(self.dir / "campaign.json", rec)
        (self.dir / "CAMPAIGN.md").write_text(render(rec))
        return rec

    def conclude(self):
        rec = self.save()
        log(f"[campaign] stopped: {rec['stop_reason']}")
        for row in rec["leaderboard"]:
            log(f"[campaign]   #{row['rank']} {row['chain']}: exit {row['exit_code']}, "
                f"{row['unconnected_in_scope']} unconnected, {row['new_violations']} new violations, "
                f"{row['runtime_s']} s ({row['status']})")
        if rec["best"]:
            log(f"[campaign] best: {rec['best']['run_dir']} (chain {rec['best']['chain']})")
            log(f"[campaign] candidate: {rec['best']['candidate']}")
        else:
            log("[campaign] best: none (no chain produced a gradable candidate)")
        log(f"[campaign] result: {rec['campaign_dir']}/campaign.json  (exit {rec['exit_code']})")
        return rec


def render(rec):
    lines = [f"# Routing campaign: {rec['strategy']} with {rec['backend']} on {rec['project']}", "",
             f"- Input: `{rec['input']['board']}` in `{rec['input']['dir']}`"
             + (f" @ {rec['input']['rev']}" if rec["input"]["rev"] else "")
             + (f" (fingerprint {rec['input']['fingerprint'][:16]})" if rec["input"]["fingerprint"] else ""),
             f"- Scope: {rec['scope']['nets']} nets; rounds {rec['settings']['rounds']}, race {rec['settings']['race']}"
             f", total budget {rec['settings']['total_budget_s'] or 'none'}",
             f"- Stop: {rec['stop_reason'] or 'running'}"]
    if rec.get("timeouts"):
        stopped = ", ".join(f"`{t['log']}` ({t['timeout_s']} s)" for t in rec["timeouts"])
        lines.append(f"- Stopped at a timeout: {stopped}")
    if rec["best"]:
        b = rec["best"]
        lines += [f"- Best: chain `{b['chain']}` (exit {b['exit_code']}), run `{b['run_dir']}`",
                  f"- Candidate: `{b['candidate']}`",
                  f"- Graded against the original input (`{b['graded_against']}`): `{b['grade']}`"]
    else:
        lines.append("- Best: none (no chain produced a gradable candidate)")
    lines += ["", "| # | Chain | Exit | Routing | Unconnected | Validation | New violations | Preservation failures"
                  " | Runtime s | End run |", "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rec["leaderboard"]:
        lines.append(f"| {r['rank']} | {r['chain']} ({r['status']}) | {r['exit_code']} | {r['routing'] or '-'} | "
                     f"{r['unconnected_in_scope'] if r['unconnected_in_scope'] is not None else '-'} | "
                     f"{r['validation'] or '-'} | {r['new_violations'] if r['new_violations'] is not None else '-'} | "
                     f"{', '.join(r['preservation_failures'] or []) or '-'} | {r['runtime_s']} | `{r['end_run']}` |")
    lines += ["", "Backlog (nets left unrouted in any round so far):", ""]
    lines += [f"- after round {b['round']}: {b['size']} nets" + (f": {', '.join(b['nets'][:20])}"
              + (" …" if b["size"] > 20 else "") if b["nets"] else "") for b in rec["backlog_evolution"]]
    lines += ["", "Chains:", ""]
    for c in rec["chains"]:
        lines.append(f"- `{c['id']}` ({c['strategy']}, {c['status']}"
                     + (f": {c['error']}" if c.get("error") else "") + ")")
        for s in c["steps"]:
            lines.append(f"  - {s['role']} on {s['on']}: exit {s['exit_code']}, "
                         f"{s.get('routing', '-')}, validation {s.get('validation', '-')}, `{s['run_dir']}`")
    return "\n".join(lines) + "\n"


def main(args, cli):
    """The campaign record (campaign.json). A refusal before any backend ran removes
    the campaign directory and propagates (exit 4, docs/interface.md)."""
    if args.rounds < 0:
        contract.refuse("usage", "--rounds must be 0 or more")
    c = Campaign(args, cli)
    previous = signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))  # steps run in their own sessions
    try:
        return c.execute()
    except contract.Refusal as r:   # nothing was routed: no campaign directory is left
        c.stop_all()
        shutil.rmtree(c.dir, ignore_errors=True)
        latest = c.dir.parent / "latest"
        if latest.is_symlink() and not latest.exists():
            latest.unlink()
        r.detail["discarded_run_dir"] = common.rel(c.dir)
        raise
    except BaseException:
        c.stop_all()
        raise
    finally:
        signal.signal(signal.SIGTERM, previous)
