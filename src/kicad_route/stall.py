"""Progress observation for a running backend: tail its log, keep a heartbeat
ledger, and detect a stall (no progress for --stall seconds).

The backends print progress in their own words. A parser per backend turns
log lines into a (phase, metric) state; a heartbeat is recorded whenever that
state moves forward:

    KRT          phase banners (=== title ===), "Routing N single-ended net(s)",
                 per-net "[i/N (k failed)] Routing NET", SUCCESS/FAILED counts,
                 "Per-net fine-parameter rescue" / "Rescuing NET", plane repair,
                 KiCad-oracle rounds, "Final reconciliation", JSON_SUMMARY,
                 JSON_IMPROVEMENT_GATE, "Writing output", "CHECKPOINT STOP".
                 Search chatter ("Probe ...", "No route found after N
                 iterations") is not progress: a search that never ends is the
                 stall this detects.
    Freerouting  "<stage> stage started|completed", fanout and auto-routing
                 passes. A pass counts as progress only when it beats the best
                 (fewer unrouted, then higher score) seen in the current stage;
                 passes that repeat the same score are not.

Heartbeats go to work/heartbeat.jsonl beside the backend log, one JSON object
per line: {"t_s", "utc", "phase", "metric", "line"}. Observation is always on
(the result records the last phase even for a budget kill); stopping on a stall
only happens when the run was given --stall.
"""
import json
import re
import subprocess
import time
from pathlib import Path

from . import common

POLL_S = 0.25


def _short(text, n=100):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 3] + "..."


class KrtProgress:
    """KRT's py_router console output -> (phase, metric)."""
    NET = re.compile(r"^\s*\[(\d+)/(\d+)(?:\s*\((\d+) failed\))?\]\s+Routing\s+(.+?)(?:\s+\(id=\d+\))?\s*$")
    EDGE = re.compile(r"^\s*\[(\d+)/(\d+)\](?:\s*\((\d+) failed\))?\s+Routing MST edge")
    ROUTING_N = re.compile(r"^Routing (\d+) (.+?)\.\.\.\s*$")
    RESCUE = re.compile(r"^\s*Rescuing (.+?) \((\w+), (\d+) disconnected parts?\)")
    IMPROVED = re.compile(r"^\s*(improved|unchanged|no improvement|failed)\b.*?\((\d+) -> (\d+) parts?")
    PLANE_NET = re.compile(r"^\[([^\]]+)\] on (\S+)")
    ORACLE = re.compile(r"KiCad-oracle recheck round (\d+)")
    PHASES = [  # (regex on the line, phase name)
        (re.compile(r"^Loading .*\.kicad_pcb"), "loading board"),
        (re.compile(r"^Using MPS ordering"), "net ordering"),
        (re.compile(r"Per-net fine-parameter rescue"), "net rescue"),
        (re.compile(r"^Final reconciliation"), "final reconciliation"),
        (re.compile(r"^\s*Plane finalize"), "plane finalize"),
        (re.compile(r"^\s*KiCad-oracle recheck:"), "oracle recheck"),
        (re.compile(r"^JSON_REGRADE:"), "regrade"),
        (re.compile(r"^Writing output to"), "writing output"),
        (re.compile(r"^CHECKPOINT STOP"), "checkpoint stop"),
    ]

    def __init__(self):
        self.phase, self.metric = "startup", {}
        self.successes = self.failures = 0
        self._prev = ["", ""]

    def feed(self, line):
        """Returns True when the line moved the state forward."""
        before = (self.phase, json.dumps(self.metric, sort_keys=True))
        s = line.rstrip()
        prev2, prev1 = self._prev
        self._prev = [prev1, s]
        if re.fullmatch(r"=+", s) and len(s) >= 20 and re.fullmatch(r"=+", prev2) and prev1.strip():
            self._set_phase(prev1.strip())  # === title === banner
        elif s.startswith("JSON_SUMMARY:"):
            self._set_phase("summary")
        elif s.startswith("JSON_IMPROVEMENT_GATE:"):
            self._set_phase("improvement gate")
            try:
                gate = json.loads(s.split(":", 1)[1])
                self.metric = {k: gate[k] for k in list(gate)[:4] if not isinstance(gate[k], (dict, list))}
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass
        elif m := self.ROUTING_N.match(s):
            self._set_phase(f"routing {m.group(2)}")
            self.metric = {"nets": int(m.group(1))}
        elif m := self.NET.match(s):
            self.metric = {"net": f"{m.group(1)}/{m.group(2)}", "failed": int(m.group(3) or 0),
                           "current": _short(m.group(4), 60)}
        elif m := self.EDGE.match(s):
            self.metric = dict(self.metric, edge=f"{m.group(1)}/{m.group(2)}")
        elif m := self.RESCUE.match(s):
            self.metric = {"rescuing": _short(m.group(1), 60), "parts": int(m.group(3))}
        elif m := self.IMPROVED.match(s):
            self.metric = dict(self.metric, parts=int(m.group(3)), outcome=m.group(1))
        elif m := self.PLANE_NET.match(s):
            self.metric = {"plane_net": m.group(1), "layer": m.group(2)}
        elif m := self.ORACLE.search(s):
            self.metric = dict(self.metric, oracle_round=int(m.group(1)))
        elif re.match(r"^\s*SUCCESS:", s):
            self.successes += 1
            self.metric = dict(self.metric, routed=self.successes)
        elif s.lstrip().startswith("FAILED:"):
            self.failures += 1
            self.metric = dict(self.metric, failures=self.failures)
        else:
            for rx, name in self.PHASES:
                if rx.search(s):
                    self._set_phase(name)
                    break
        return (self.phase, json.dumps(self.metric, sort_keys=True)) != before

    def _set_phase(self, name):
        name = _short(name, 80)
        if name != self.phase:
            self.phase, self.metric = name, {}


class FreeroutingProgress:
    """Freerouting's log -> (phase, metric). Score: higher is better."""
    STAGE = re.compile(r"(?i)\b([\w-]+(?: [\w-]+)?) stage (started|completed)")
    PASS = re.compile(r"(?i)(auto-routing|optimi[sz]\w*|fanout|fan-out) pass #(\d+).*?score ([\d.]+) "
                      r"\((\d+) unrouted(?: and (\d+) violations)?")
    FAN = re.compile(r"(?i)\bfan(?:out)? ?#(\d+)\b.*?(\d+) not routed")
    BASE = re.compile(r"baseline score ([\d.]+) for (\d+) unrouted")
    PHASES = [(re.compile(r"Loading board file"), "loading board"),
              (re.compile(r"Saving output file"), "saving session"),
              (re.compile(r"Job '.*' finished"), "finished")]

    def __init__(self):
        self.phase, self.metric, self.best = "startup", {}, None

    def feed(self, line):
        before = (self.phase, json.dumps(self.metric, sort_keys=True))
        if m := self.PASS.search(line):
            unrouted, score = int(m.group(4)), float(m.group(3))
            key = (-unrouted, score)
            if self.best is None or key > self.best:
                self.best = key
                self.metric = {"pass": int(m.group(2)), "score": score, "unrouted": unrouted,
                               "violations": int(m.group(5) or 0)}
            else:  # a pass that did not improve: not progress, but keep the pass count visible
                self.metric = dict(self.metric, last_pass=int(m.group(2)))
                return False
        elif m := self.STAGE.search(line):
            self._set_phase(f"{m.group(1).lower()} {m.group(2).lower()}")
            if b := self.BASE.search(line):
                self.metric = {"score": float(b.group(1)), "unrouted": int(b.group(2))}
        elif m := self.FAN.search(line):
            self.metric = {"fanout_pass": int(m.group(1)), "not_routed": int(m.group(2))}
        else:
            for rx, name in self.PHASES:
                if rx.search(line):
                    self._set_phase(name)
                    break
        return (self.phase, json.dumps(self.metric, sort_keys=True)) != before

    def _set_phase(self, name):
        if name != self.phase:
            self.phase, self.metric, self.best = name, {}, None


PARSERS = {"krt": KrtProgress, "freerouting": FreeroutingProgress}


def describe(metric):
    if not metric:
        return "no progress metric"
    return ", ".join(f"{k} {v}" for k, v in metric.items() if k != "last_pass") + (
        f" (last pass #{metric['last_pass']})" if "last_pass" in metric else "")


class Monitor:
    """Tails a backend log while the process runs. wait() raises
    subprocess.TimeoutExpired at the budget, or at a stall when stall_s is set."""

    def __init__(self, backend, log_path, stall_s=None, heartbeat_path=None):
        self.parser = PARSERS[backend]()
        self.backend = backend
        self.log_path = Path(log_path)
        self.heartbeat_path = Path(heartbeat_path or self.log_path.parent / "heartbeat.jsonl")
        self.stall_s = stall_s or None
        self.stalled = False
        self.count = 0
        self.tail = []
        self._offset, self._partial = 0, b""
        self._start = self._last_progress = None
        self._stall = None

    # -- log tailing
    def _read(self):
        try:
            with open(self.log_path, "rb") as f:
                f.seek(self._offset)
                data = f.read()
        except FileNotFoundError:
            return
        self._offset += len(data)
        data = self._partial + data
        *lines, self._partial = data.split(b"\n")
        for raw in lines:
            line = raw.decode("utf-8", "replace")
            if self.parser.feed(line):
                self._beat(line)

    def _beat(self, line):
        now = time.monotonic()
        self._last_progress = now
        self.count += 1
        rec = {"t_s": round(now - self._start, 3), "utc": common.now_utc().isoformat(),
               "phase": self.parser.phase, "metric": self.parser.metric, "line": _short(line, 160)}
        self.tail = (self.tail + [rec])[-5:]
        with open(self.heartbeat_path, "a") as f:
            f.write(json.dumps(rec) + "\n")

    def wait(self, proc, budget):
        self._start = self._last_progress = time.monotonic()
        self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        self.heartbeat_path.write_text("")
        deadline = self._start + budget
        while True:
            try:
                proc.wait(timeout=POLL_S)
                self._read()
                if self._partial:  # last line without a newline
                    if self.parser.feed(self._partial.decode("utf-8", "replace")):
                        self._beat(self._partial.decode("utf-8", "replace"))
                    self._partial = b""
                return proc.returncode
            except subprocess.TimeoutExpired:
                pass
            self._read()
            now = time.monotonic()
            if self.stall_s and now - self._last_progress >= self.stall_s:
                self.stalled = True
                self._stall = {"phase": self.parser.phase, "metric": dict(self.parser.metric),
                               "no_progress_s": round(now - self._last_progress, 1)}
                raise subprocess.TimeoutExpired(proc.args, budget)
            if now >= deadline:
                raise subprocess.TimeoutExpired(proc.args, budget)

    def finish(self):
        """Read what the process wrote after wait() gave up (stop request, kill)."""
        if self._start is not None:
            self._read()

    def summary(self):
        now = time.monotonic()
        return {"last_phase": self.parser.phase, "last_metric": self.parser.metric,
                "heartbeats": {"count": self.count, "log": common.rel(self.heartbeat_path),
                               "stall_s": self.stall_s,
                               "since_last_progress_s": round(now - self._last_progress, 1)
                               if self._last_progress else None,
                               "tail": self.tail},
                "stalled": self.stalled,
                **({"stall": self._stall} if self.stalled else {})}


def stall_reason(rec):
    """`stalled in <phase> at <metric> for N s` for a run_budgeted record, else None."""
    st = rec.get("stall")
    if not st:
        return None
    return f"stalled in {st['phase']} at {describe(st['metric'])} for {st['no_progress_s']:g} s"


def annotate(proc):
    """Copy the monitored command's last phase and heartbeats into the process record."""
    for c in proc.get("commands") or []:
        if "last_phase" in c:
            proc["last_phase"] = c["last_phase"]
            proc["heartbeats"] = c["heartbeats"]
            proc["stalled"] = c.get("stalled", False)
            return proc
    return proc
