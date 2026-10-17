"""Canary pre-flight: the bundled fixture with awkward net names, through each
backend's round trip, checked for nets and pads that do not survive.

Net names on real boards carry parentheses (`Net-(BT1-+)`), spaces and slashes
(`/a b/c`), quotes, unicode and leading `+`/`-`. Each has broken a bridge or a
command line somewhere; the parenthesis case once produced an unparsable DSN on
a real board. The canary renames the fixture's nets (KiCad's own loader), keeps
the net classes on the renamed nets, scopes two of them out so the Specctra
class split runs on quoted names, and checks:

    split       specctra.split_classes on a synthetic KiCad-style network
                (pure Python, milliseconds; no KiCad needed)
    bridge      KiCad's DSN export of the renamed board, then the class split in
                process and through specctra.py --skip-nets: the DSN parses, every
                net stays in exactly one class, scoped-out nets move to the
                `__skip` copies with identical rules (seconds; no routing)
    <backend>   a short run of the adapter itself (krt_route / freerouting_route)
                on the renamed board: nets, net classes and pad-to-net
                assignments of the output equal the input's, new copper lies only
                on in-scope nets (none on the scoped-out ones, no new nets), and
                every in-scope net that needs a track got one

`doctor` runs split and bridge; `kicad-route canary` and `run --canary` add the
backend round trips. Results go to OUT/canary/<stamp>/canary.json.
"""
import json
import re
import shutil
from pathlib import Path

from . import common
from . import cli as route
from .kicad import specctra

PASS, FAIL = common.PASS, common.FAIL

AWKWARD = {"/SIG_A": "Net-(BT1-+)", "/SIG_A_OUT": '"q"', "/SIG_B": "/a b/c",
           "/SIG_B_OUT": "+3V3/ü µ", "/VIN": "-VIN(1)", "GND": "GND 'x' (y)"}
SCOPED_OUT = ["Net-(BT1-+)", '"q"']      # moved to kicad_default__skip by the bridge
POWER = ["-VIN(1)", "GND 'x' (y)"]       # the fixture's Power class, by pattern


# ----------------------------------------------------------- DSN structure

TOKEN = re.compile(r'"[^"]*"|\(|\)|[^\s()]+')


def parse(dsn):
    """Whole-file s-expression parse (DSN string_quote is `"`, no escapes).
    Returns the single top-level list; raises ValueError when unbalanced."""
    stack, cur = [], []
    dsn = re.sub(r'\(string_quote\s+"\s*\)', "(string_quote QUOTE)", dsn)  # the one bare quote char
    for t in TOKEN.findall(dsn):
        if t == "(":
            stack.append(cur)
            cur = []
        elif t == ")":
            if not stack:
                raise ValueError("unbalanced: ')' without '('")
            done, cur = cur, stack.pop()
            cur.append(done)
        else:
            cur.append(t)
    if stack:
        raise ValueError(f"unbalanced: {len(stack)} '(' never closed")
    if len(cur) != 1 or not isinstance(cur[0], list):
        raise ValueError(f"{len(cur)} top-level items, expected one (pcb ...)")
    return cur[0]


def classes(tree):
    """{class name: (members, sub-lists)} of the (network ...) section."""
    net = next((x for x in tree if isinstance(x, list) and x and x[0] == "network"), None)
    if net is None:
        raise ValueError("no (network ...) section")
    out = {}
    for c in net:
        if isinstance(c, list) and c and c[0] == "class" and len(c) > 1:
            members = [a for a in c[2:] if not isinstance(a, list)]
            subs = [a for a in c[2:] if isinstance(a, list)]
            out[specctra.unquote(c[1])] = (members, subs)
    return out


def dsn_forms(name):
    """Every spelling KiCad's exporter uses for a net name (class list, definition)."""
    return {name, specctra.unquote(specctra.dsn_net_token(name)), specctra.dsn_net_token(name)}


def kicad_name(atom):
    """The KiCad net name behind a DSN class-list atom (either spelling)."""
    if len(atom) >= 2 and atom[0] == atom[-1] == '"' and not re.search(r"[\s()]", atom[1:-1]):
        return atom  # KiCad quotes only for whitespace/parentheses: these quotes are the name's
    return specctra.unquote(atom).replace("''", '"')


def split_check(before, after, skip, ignored):
    """Problems (list of str) with `after`, the class split of `before` for `skip`."""
    problems = []
    try:
        cb, ca = classes(parse(before)), classes(parse(after))
    except ValueError as e:
        return [f"split DSN does not parse: {e}"]
    skip_forms = {f for n in skip for f in dsn_forms(n)}

    def is_skipped(atom):
        return atom in skip_forms or specctra.unquote(atom) in skip_forms

    members_b = sorted(kicad_name(m) for ms, _ in cb.values() for m in ms)
    members_a = sorted(kicad_name(m) for ms, _ in ca.values() for m in ms)
    if members_b != members_a:
        problems.append(f"class membership changed: {len(members_b)} members before, {len(members_a)} after")
    for name, (members, subs) in ca.items():
        for m in members:
            if name.endswith("__skip") and not is_skipped(m):
                problems.append(f"in-scope net {m} moved to {name}")
            if not name.endswith("__skip") and is_skipped(m):
                problems.append(f"scoped-out net {m} left in routable class {name}")
        if name.endswith("__skip"):
            base = name[:-len("__skip")]
            if base not in cb or cb[base][1] != subs:
                problems.append(f"{name}: rules differ from class {base}")
    for name in skip:
        if not any(is_skipped(m) for c, (ms, _) in ca.items() if c.endswith("__skip") for m in ms):
            problems.append(f"scoped-out net {name!r} is in no __skip class")
    want = sorted(c for c in ca if c.endswith("__skip"))
    if sorted(ignored) != want:
        problems.append(f"ignored classes {sorted(ignored)} != __skip classes {want}")
    return problems


def kicad_class_token(name):
    """How KiCad writes a net in a class list: raw, quoted for whitespace/parentheses."""
    return f'"{name}"' if re.search(r"[\s()]", name) else name


def synthetic_dsn():
    nets = list(AWKWARD.values())
    defs = "\n".join(f"    (net {specctra.dsn_net_token(n)}\n      (pins P{i}-1 P{i}-2)\n    )"
                     for i, n in enumerate(nets))
    default = [n for n in nets if n not in POWER]
    return ("(pcb canary.dsn\n  (parser\n    (string_quote \")\n    (space_in_quoted_tokens on)\n  )\n"
            f"  (network\n{defs}\n"
            f"    (class kicad_default {' '.join(kicad_class_token(n) for n in default)}\n"
            "      (circuit\n        (use_via \"Via[0-1]_600:300_um\")\n      )\n"
            "      (rule\n        (width 200)\n        (clearance 200)\n      )\n    )\n"
            f"    (class Power {' '.join(kicad_class_token(n) for n in POWER)}\n"
            "      (circuit\n        (use_via \"Via[0-1]_800:400_um\")\n      )\n"
            "      (rule\n        (width 500)\n        (clearance 300)\n      )\n    )\n  )\n)\n")


def check_split_unit():
    """specctra.split_classes on the synthetic network, scoping out a Default and a Power net."""
    dsn = synthetic_dsn()
    skip = SCOPED_OUT + POWER[:1]
    try:
        out, ignored = specctra.split_classes(dsn, set(skip))
    except Exception as e:  # a crash is a canary failure, not a tool crash
        return {"check": "split", "status": FAIL, "problems": [f"split_classes raised {type(e).__name__}: {e}"]}
    problems = split_check(dsn, out, skip, ignored)
    return {"check": "split", "status": FAIL if problems else PASS, "problems": problems,
            "scoped_out": skip, "ignored_classes": ignored}


# ----------------------------------------------------------- canary board

def prepare(dest):
    """dest/route_demo: the fixture with AWKWARD net names, Power kept on its nets."""
    proj = dest / route.FIXTURE.name
    shutil.copytree(route.FIXTURE, proj, ignore=shutil.ignore_patterns("generate.py", "__pycache__"))
    board = proj / "route_demo.kicad_pcb"
    (dest / "rename.json").write_text(json.dumps(AWKWARD, ensure_ascii=False))
    rec = common.run_cmd([route.pcbnew_python(), route.KICAD_SCRIPTS / "rename_nets.py", board,
                          dest / "rename.json"], dest, dest / "rename.log", timeout=300)
    if rec["exit_code"] != 0:
        raise RuntimeError(f"renaming nets failed (exit {rec['exit_code']}); see {rec['log']}")
    pro = proj / "route_demo.kicad_pro"
    data = json.loads(pro.read_text())
    data["net_settings"]["netclass_patterns"] = [{"netclass": "Power", "pattern": n} for n in POWER]
    pro.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    facts, frec = route.board_facts(board, dest / "input-facts.json")
    if facts is None:
        raise RuntimeError(f"facts of the canary board failed; see {frec['log']}")
    missing = sorted(set(AWKWARD.values()) - set(facts["nets"]))
    if missing:
        raise RuntimeError(f"renamed nets missing from the canary board: {missing}")
    return board, facts


def check_bridge(dest, board):
    """KiCad's DSN export of the canary board, split in process and by specctra.py."""
    out = {"check": "bridge", "status": PASS, "problems": []}
    raw = dest / "raw.dsn"
    rec = common.run_cmd([route.pcbnew_python(), route.KICAD_SCRIPTS / "specctra.py", "export", board, raw],
                         dest, dest / "export-raw.log", timeout=300)
    if rec["exit_code"] != 0 or not raw.exists():
        return dict(out, status=FAIL, problems=[f"DSN export failed (exit {rec['exit_code']}); see {rec['log']}"])
    text = raw.read_text()
    try:
        parse(text)
    except ValueError as e:
        out["problems"].append(f"KiCad's DSN of the canary board does not parse: {e}")
    try:
        split, ignored = specctra.split_classes(text, set(SCOPED_OUT))
        out["problems"] += [f"in process: {p}" for p in split_check(text, split, SCOPED_OUT, ignored)]
    except Exception as e:
        out["problems"].append(f"in process: split_classes raised {type(e).__name__}: {e}")
    skip = dest / "skip-nets.json"
    skip.write_text(json.dumps(SCOPED_OUT))
    scoped = dest / "scoped.dsn"
    rec = common.run_cmd([route.pcbnew_python(), route.KICAD_SCRIPTS / "specctra.py", "export", board, scoped,
                          "--skip-nets", skip], dest, dest / "export-scoped.log", timeout=300)
    bridge = route.tagged_json(rec["output"], "BRIDGE_JSON") if rec["exit_code"] == 0 else None
    if not bridge or not scoped.exists():
        out["problems"].append(f"scoped DSN export failed (exit {rec['exit_code']}); see {rec['log']}")
    else:
        out["problems"] += [f"specctra.py --skip-nets: {p}" for p in
                            split_check(text, scoped.read_text(), SCOPED_OUT, bridge["ignored_classes"])]
    out["status"] = FAIL if out["problems"] else PASS
    return out


def facts_check(ref, cand, scope):
    """Nets, classes and pads survive; new copper only in scope; routable nets routed."""
    problems = []
    nr = {n: v["netclass"] for n, v in ref["nets"].items()}
    nc = {n: v["netclass"] for n, v in cand["nets"].items()}
    if nr != nc:
        problems.append(f"nets/net classes changed: {route.dict_diff(nr, nc, limit=8)}")
    pr = {f"{r}.{p}": v["net"] for r, fp in ref["footprints"].items() for p, v in fp["pads"].items()}
    pc = {f"{r}.{p}": v["net"] for r, fp in cand["footprints"].items() for p, v in fp["pads"].items()}
    if pr != pc:
        problems.append(f"pad-to-net assignments changed: {route.dict_diff(pr, pc, limit=8)}")
    new = []
    for kind in ("tracks", "vias"):
        _, _, extra = route.match_copper([route.strip(t, "uuid", "locked") for t in ref[kind]],
                                         [route.strip(t, "uuid", "locked") for t in cand[kind]])
        new += extra
    new_nets = {t["net"] for t in new}
    outside = sorted(new_nets - set(scope))
    if outside:
        problems.append(f"new copper outside the scope: {outside}")
    copper = {t["net"] for t in ref["tracks"] + ref["vias"]}
    zoned = {z.get("net") for z in ref["zones"]}
    pads = {}
    for v in pr.values():
        pads[v] = pads.get(v, 0) + 1
    needed = sorted(n for n in scope if pads.get(n, 0) >= 2 and n not in copper and n not in zoned)
    unrouted = [n for n in needed if n not in new_nets]
    if unrouted:
        problems.append(f"in-scope nets that needed copper got none: {unrouted}")
    return problems, {"new_copper_nets": sorted(new_nets), "needed_copper": needed}


def check_backend(name, dest, board, facts):
    """A short run of the real adapter on the canary board, then the facts compare."""
    work = dest / name
    copy = work / "input" / board.parent.name
    route.copy_project(board.parent, copy)
    scope = sorted(set(facts["nets"]) - set(SCOPED_OUT) - {""})
    settings = {"backend": name, "budget_s": 90, "protect": "existing", "max_iterations": None,
                "passes": 10, "default_clearance_mm": 0.4 if name == "krt" else None,
                "clearance_margin_mm": None, "backend_args": [],
                "backend_env": {"KICAD_NET_RESCUE": "0"} if name == "krt" else {},
                "mode": "selected", "net_patterns": scope, "netclasses": []}
    out = {"check": name, "scope": scope, "scoped_out": SCOPED_OUT}
    try:
        proc = route.BACKENDS[name]["route"](work, copy / board.name, scope, facts, settings)
    except Exception as e:
        return dict(out, status=FAIL, problems=[f"adapter raised {type(e).__name__}: {e}"])
    out["process"] = {k: proc.get(k) for k in ("status", "reasons", "exit_code")}
    if proc["status"] != PASS:
        return dict(out, status=FAIL, problems=[f"backend process {proc['status']}: {'; '.join(proc['reasons'])}"])
    cand, rec = route.board_facts(Path(proc["output_board"]), work / "output-facts.json")
    if cand is None:
        return dict(out, status=FAIL, problems=[f"facts of the output failed; see {rec['log']}"])
    problems, detail = facts_check(facts, cand, scope)
    return dict(out, status=FAIL if problems else PASS, problems=problems, **detail)


def available(name):
    if name == "krt":
        return route.route_python().exists() and route.KRT_DIR.exists()
    return route.freerouting_installed() is not None


def run_canary(backend="all", quick=False, dest=None):
    """split + bridge, then (unless quick) each backend's round trip. Returns the
    record written to OUT/canary/<stamp>/canary.json."""
    common.ensure_kicad_config()
    dest = Path(dest or route.out_path("canary", common.now_utc().strftime("%Y%m%dT%H%M%S.%fZ")))
    dest.mkdir(parents=True, exist_ok=True)
    checks = [check_split_unit()]
    names = [] if quick else (sorted(route.BACKENDS) if backend == "all" else [backend])
    try:
        board, facts = prepare(dest)
        checks.append(check_bridge(dest, board))
        for name in names:
            if not available(name):
                checks.append({"check": name, "status": FAIL, "problems": [f"{name} is not installed"]})
                continue
            route.log(f"[canary] {name}: short round trip on awkward net names")
            checks.append(check_backend(name, dest, board, facts))
    except RuntimeError as e:
        checks.append({"check": "prepare", "status": FAIL, "problems": [str(e)]})
    reasons = [f"{c['check']}: {p}" for c in checks for p in c.get("problems", [])]
    rec = {"kind": "canary", "status": FAIL if reasons else PASS, "reasons": reasons,
           "nets": AWKWARD, "scoped_out": SCOPED_OUT, "checks": checks, "dir": common.rel(dest)}
    common.write_json(dest / "canary.json", rec)
    for c in checks:
        route.log(f"[canary] {c['status']:<5} {c['check']}" + (f": {'; '.join(c['problems'][:3])}"
                                                               if c.get("problems") else ""))
    route.log(f"[canary] {rec['status']} -> {common.rel(dest)}/canary.json")
    return rec


def cmd_canary(args):
    return 0 if run_canary(args.backend)["status"] == PASS else 1
