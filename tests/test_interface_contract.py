"""The machine interface (docs/interface.md): versioned records checked against
schemas/, the refusal path (exit 4 with a record, no run directory left), --json
machine mode, --out after the subcommand, and one definition of the routing
options for run, campaign and corpus.

The refusal, parser and schema tests need no backend (a few read the fixture board
with KiCad's Python). The machine-mode tests at the end route the fixture.
"""
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

from helpers import FIXTURE, OUT, ROOT, record
from kicad_route import campaign, contract, corpus
from kicad_route import cli as route

CLI = [sys.executable, str(ROOT / "bin" / "kicad-route")]
RESULT_SCHEMA = json.loads((ROOT / "schemas" / "result-1.json").read_text())
REFUSAL_SCHEMA = json.loads((ROOT / "schemas" / "refusal-1.json").read_text())
RESULTS = jsonschema.Draft202012Validator(RESULT_SCHEMA)
REFUSALS = jsonschema.Draft202012Validator(REFUSAL_SCHEMA)
EVIDENCE = ROOT / "reports" / "fixture"


def kr(*args, cwd, env=None, timeout=900):
    return subprocess.run([*CLI, *map(str, args)], capture_output=True, text=True, cwd=cwd, timeout=timeout,
                          env=dict(os.environ, **(env or {})))


def one_json(stdout):
    """--json prints exactly one JSON object (one line) on stdout."""
    lines = [l for l in stdout.splitlines() if l.strip()]
    assert len(lines) == 1, f"expected one JSON line on stdout, got {len(lines)}:\n{stdout[-2000:]}"
    data = json.loads(lines[0])
    assert isinstance(data, dict)
    return data


def run_dirs_left(out):
    """Anything a refused attempt left under OUT/runs (empty project directories aside)."""
    runs = out / "runs"
    return [p for p in runs.rglob("*") if not p.is_dir() or p.is_symlink()] if runs.exists() else []


def project_copy(tmp_path, name="route_demo"):
    d = tmp_path / "proj" / name
    shutil.copytree(FIXTURE, d, ignore=shutil.ignore_patterns("generate.py", "__pycache__"))
    return d


def assert_refused(r, out, reason_class, json_mode=False, command=None):
    assert r.returncode == contract.REFUSED_EXIT, f"exit {r.returncode}\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}"
    latest = out / "refusals" / "refusal-latest.json"
    assert latest.is_symlink(), f"no refusal record under {out}/refusals:\n{r.stdout}\n{r.stderr}"
    rec = json.loads(latest.read_text())
    REFUSALS.validate(rec)
    assert rec["reason_class"] == reason_class, rec
    assert (out / "refusals" / os.readlink(latest)).exists()
    if command:
        assert rec["command"] == command
    if json_mode:
        assert one_json(r.stdout) == rec
        assert "refused" in r.stderr
    else:
        assert not r.stdout.lstrip().startswith("{")
    assert not run_dirs_left(out), run_dirs_left(out)
    assert not (out / "campaigns").exists() or not any((out / "campaigns").rglob("campaign.json"))
    return rec


# ------------------------------------------------------------------ refusals

def _cases(tmp_path):
    """name -> (argv after --out, reason_class)."""
    empty = tmp_path / "empty"
    empty.mkdir()
    two = tmp_path / "two"
    two.mkdir()
    for b in ("a.kicad_pcb", "b.kicad_pcb"):
        (two / b).write_text("(kicad_pcb)\n")
    garbage = tmp_path / "garbage" / "route_demo"
    garbage.mkdir(parents=True)
    (garbage / "route_demo.kicad_pcb").write_text("this is not a KiCad board\n")
    nogit = project_copy(tmp_path)
    taken = tmp_path / "taken"
    taken.mkdir()
    fx = str(FIXTURE)
    return {
        # usage: the command line itself (argparse errors included: never exit 2)
        "unknown-option": (["run", "--backend", "krt", "--bogus"], "usage"),
        "bad-choice": (["run", "--backend", "nosuch"], "usage"),
        "bad-int": (["run", "--backend", "krt", "--budget", "abc"], "usage"),
        "abbreviation": (["run", "--backend", "krt", "--dir", fx, "--budg", "5"], "usage"),
        "no-command": ([], "usage"),
        "backend-env": (["run", "--backend", "krt", "--dir", fx, "--backend-env", "NOEQ"], "usage"),
        "library-dir": (["run", "--backend", "krt", "--dir", fx, "--library-dir", "NOEQ"], "usage"),
        "campaign-rounds": (["campaign", "--backend", "krt", "--dir", fx, "--rounds", "-1"], "usage"),
        "campaign-library-dir": (["campaign", "--backend", "krt", "--dir", fx, "--library-dir", "NOEQ"], "usage"),
        "campaign-backend-env": (["campaign", "--backend", "krt", "--dir", fx, "--backend-env", "NOEQ"], "usage"),
        "corpus-no-name": (["corpus", "add", "--dir", fx], "usage"),
        "corpus-bad-name": (["corpus", "add", "--dir", fx, "--name", "a b"], "usage"),
        "corpus-library-dir": (["corpus", "add", "--dir", fx, "--name", "x", "--library-dir", "NOEQ"], "usage"),
        "audit-library-dir": (["audit", "--dir", fx, "--old", "HEAD", "--library-dir", "NOEQ"], "usage"),
        "spokes-nothing": (["spokes"], "usage"),
        "json-unsupported": (["compare", str(tmp_path), "--json"], "usage"),
        # input: the project, run or revision named
        "not-a-dir": (["run", "--backend", "krt", "--dir", tmp_path / "missing"], "input"),
        "no-board": (["run", "--backend", "krt", "--dir", empty], "input"),
        "two-boards": (["run", "--backend", "krt", "--dir", two], "input"),
        "rev-outside-git": (["run", "--backend", "krt", "--dir", nogit, "--rev", "HEAD"], "input"),
        "rev-not-a-commit": (["run", "--backend", "krt", "--dir", fx, "--rev", "no-such-rev-xyz"], "input"),
        "nets-match-nothing": (["run", "--backend", "krt", "--dir", fx, "--nets", "NO_SUCH_NET*"], "input"),
        "unreadable-board": (["run", "--backend", "krt", "--dir", garbage], "input"),
        "campaign-not-a-dir": (["campaign", "--backend", "krt", "--dir", tmp_path / "missing"], "input"),
        "validate-not-a-run": (["validate", tmp_path], "input"),
        "blocked-not-a-run": (["blocked", tmp_path], "input"),
        "spokes-not-a-run": (["spokes", tmp_path], "input"),
        "compare-no-result": (["compare", tmp_path], "input"),
        "publish-no-result": (["publish", tmp_path], "input"),
        "floor-outside-git": (["floor", "--dir", nogit], "input"),
        "floor-since-not-a-commit": (["floor", "--dir", fx, "--since", "no-such-rev-xyz"], "input"),
        "audit-not-a-dir": (["audit", "--dir", tmp_path / "missing", "--old", "HEAD"], "input"),
        # state: kicad-route's own state
        "run-dir-exists": (["run", "--backend", "krt", "--dir", fx, "--run-dir", taken], "state"),
    }


CASE_NAMES = sorted([  # listed here for pytest ids; test_pre_routing_failure_is_a_refusal checks it matches
    "unknown-option", "bad-choice", "bad-int", "abbreviation", "no-command", "backend-env", "library-dir",
    "campaign-rounds", "campaign-library-dir", "campaign-backend-env", "corpus-no-name", "corpus-bad-name",
    "corpus-library-dir", "audit-library-dir", "spokes-nothing", "json-unsupported", "not-a-dir", "no-board",
    "two-boards", "rev-outside-git", "rev-not-a-commit", "nets-match-nothing", "unreadable-board",
    "campaign-not-a-dir", "validate-not-a-run", "blocked-not-a-run", "spokes-not-a-run", "compare-no-result",
    "publish-no-result", "floor-outside-git", "floor-since-not-a-commit", "audit-not-a-dir", "run-dir-exists"])


@pytest.mark.parametrize("json_mode", [False, True], ids=["human", "json"])
@pytest.mark.parametrize("case", CASE_NAMES)
def test_pre_routing_failure_is_a_refusal(case, json_mode, tmp_path):
    cases = _cases(tmp_path)
    assert set(cases) == set(CASE_NAMES)
    argv, reason = cases[case]
    out = tmp_path / "out"
    env = {"KICAD_ROUTE_CORPUS": str(tmp_path / "corpus"), "KICAD_ROUTE_LEDGER": "off"}
    if json_mode and "--json" not in map(str, argv):
        argv = [*argv[:1], "--json", *argv[1:]] if argv else ["--json"]
        if argv[0] not in route.JSON_COMMANDS and argv[0] != "--json":
            reason = "usage"   # --json on a command without machine mode is itself a usage refusal
    json_mode = json_mode or "--json" in map(str, argv)
    r = kr("--out", out, *argv, cwd=tmp_path, env=env)
    rec = assert_refused(r, out, reason, json_mode=json_mode)
    if case == "run-dir-exists":
        assert (tmp_path / "taken").is_dir() and not any((tmp_path / "taken").iterdir())  # left untouched
    if case in ("rev-not-a-commit", "nets-match-nothing", "unreadable-board"):  # after the run dir existed
        assert rec["discarded_run_dir"] and not (tmp_path / rec["discarded_run_dir"]).exists()
    if case == "nets-match-nothing":  # the input was snapshotted, so its fingerprint is known
        assert re.fullmatch(r"[0-9a-f]{64}", rec["input_fingerprint"])


def test_corpus_entry_exists_is_a_state_refusal(tmp_path):
    (tmp_path / "corpus" / "taken").mkdir(parents=True)
    out = tmp_path / "out"
    r = kr("--out", out, "corpus", "add", "--dir", FIXTURE, "--name", "taken", cwd=tmp_path,
           env={"KICAD_ROUTE_CORPUS": str(tmp_path / "corpus")})
    assert_refused(r, out, "state", command="corpus")


def _gate_refusal(monkeypatch, tmp_path, cls, argv_extra):
    from kicad_route import canary, common, ledger
    monkeypatch.setattr(common, "_out", common._out)
    if cls == "ledger":
        real = ledger.check

        def refusing(*a, **kw):
            return dict(real(*a, **kw), refuse=True, message="identical attempt already failed (simulated)")
        monkeypatch.setattr(ledger, "check", refusing)
    else:
        monkeypatch.setattr(canary, "run_canary", lambda backend, **kw: {"status": common.FAIL,
                                                                        "reasons": ["bridge: simulated"]})
    out = tmp_path / "out"
    rc = route.main(["--out", str(out), "run", "--backend", "krt", "--dir", str(FIXTURE),
                     "--label", f"refusal-{cls}", *argv_extra])
    assert rc == contract.REFUSED_EXIT
    rec = json.loads((out / "refusals" / "refusal-latest.json").read_text())
    REFUSALS.validate(rec)
    assert rec["reason_class"] == cls and rec["command"] == "run"
    assert re.fullmatch(r"[0-9a-f]{64}", rec["input_fingerprint"])  # known by then
    assert not run_dirs_left(out) and not (out / ".cache" / "baseline").exists()
    return rec


def test_ledger_refusal_record(monkeypatch, tmp_path):
    rec = _gate_refusal(monkeypatch, tmp_path, "ledger", [])
    assert "simulated" in rec["message"]


def test_canary_refusal_record(monkeypatch, tmp_path):
    rec = _gate_refusal(monkeypatch, tmp_path, "canary", ["--canary"])
    assert rec["message"].startswith("canary pre-flight failed")


def test_refusal_is_a_system_exit_with_code_4():
    r = contract.Refusal("input", "nope")
    assert isinstance(r, SystemExit) and r.code == 4 and str(r) == "nope"
    with pytest.raises(ValueError):
        contract.Refusal("bogus", "x")


def test_no_string_exits_outside_the_refusal_helper():
    """sys.exit("message") exits 1 (backend failed); SystemExit("message") likewise.
    Command paths refuse through contract.refuse instead."""
    bad = re.compile(r"""\bsys\.exit\(\s*[rf]?["']|\bSystemExit\(\s*[rf]?["']""")
    hits = []
    for name in ("cli", "campaign", "corpus", "floor", "blocked", "spokes", "snapshot", "canary", "ledger",
                 "provenance", "pour", "stall", "common", "barrier", "decoupling", "determinism", "statemanifest",
                 "evidence", "procgroup"):
        for i, line in enumerate((ROOT / "src" / "kicad_route" / f"{name}.py").read_text().splitlines(), 1):
            if bad.search(line):
                hits.append(f"{name}.py:{i}: {line.strip()}")
    assert not hits, "\n".join(hits)


# ---------------------------------------------------------- --out position

@pytest.mark.parametrize("where", ["root", "subcommand", "both"])
def test_out_is_accepted_before_and_after_the_subcommand(where, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    base = ["run", "--backend", "krt", "--dir", tmp_path / "missing"]
    argv = {"root": ["--out", a, *base], "subcommand": [*base, "--out", b],
            "both": ["--out", a, *base, "--out", b]}[where]
    r = kr(*argv, cwd=tmp_path)
    assert "unrecognized arguments" not in r.stderr
    assert_refused(r, a if where == "root" else b, "input")
    if where == "both":
        assert not (a / "refusals").exists()  # the later --out wins


def test_out_after_nested_subcommand_and_help(tmp_path):
    r = kr("doctor", "--out", tmp_path / "o", "--help", cwd=tmp_path)
    assert r.returncode == 0 and "--out" in r.stdout and "--json" in r.stdout
    r = kr("corpus", "check", "--out", tmp_path / "o2", "--json", cwd=tmp_path)
    assert_refused(r, tmp_path / "o2", "usage", json_mode=True)  # corpus has no machine mode
    ns = route.build_parser().parse_args(["--out", "X", "run", "--backend", "krt"])
    assert ns.out == "X" and ns.json is False                     # SUPPRESS: the root value survives
    ns = route.build_parser().parse_args(["run", "--backend", "krt", "--json", "--out", "Y"])
    assert ns.out == "Y" and ns.json is True


# --------------------------------------------------- shared routing options

def subparser(name, *nested):
    ap = route.build_parser()
    p = next(a for a in ap._actions if hasattr(a, "choices") and isinstance(a.choices, dict)).choices[name]
    for n in nested:
        p = next(a for a in p._actions if hasattr(a, "choices") and isinstance(a.choices, dict)).choices[n]
    return p


def option_strings(p):
    return {s for a in p._actions for s in a.option_strings}


def test_run_campaign_and_corpus_take_the_same_routing_options():
    shared = {s for a in route._route_actions() for s in a.option_strings}
    assert {"--reserve-spokes", "--pour-area-loss", "--pour-min-spokes", "--canary", "--floor-history",
            "--stall", "--force", "--protect"} <= shared
    for p in (subparser("run"), subparser("campaign"), subparser("corpus", "add")):
        assert shared <= option_strings(p), (p.prog, sorted(shared - option_strings(p)))
    for p in (subparser("run"), subparser("campaign"), subparser("corpus", "add")):
        protect = next(a for a in p._actions if a.dest == "protect")
        assert protect.choices == ["existing", "locked", "human"] and protect.default == "existing"
    assert set(corpus.RUN_KEYS) == {"backend", *route.route_option_dests(), *corpus.REPLAYED_INPUT_KEYS}


ALL_ROUTE = ["--nets", "/SIG_A*", "!GND", "--netclass", "Power", "--budget", "77", "--max-iterations", "9",
             "--passes", "3", "--default-clearance", "0.4", "--clearance-margin", "0.01", "--protect", "human",
             "--stall", "30", "--force", "--canary", "--pour-area-loss", "5.5", "--pour-min-spokes", "2",
             "--backend-arg=--x=1", "--backend-arg=-y", "--backend-env", "K=V", "--floor-history"]


@pytest.mark.parametrize("spokes", [["--reserve-spokes", "2"], ["--reserve-spokes"], []])
def test_route_argv_reproduces_every_routing_option(spokes):
    argv = ALL_ROUTE + spokes
    a = route.build_parser().parse_args(["run", "--backend", "krt", *argv])
    again = route.build_parser().parse_args(["run", "--backend", "krt", *route.route_argv(a)])
    for dest in route.route_option_dests():
        if dest in route.SCOPE_DESTS:
            assert getattr(again, dest) is None   # a campaign sets each step's scope itself
        else:
            assert getattr(again, dest) == getattr(a, dest), dest
    # the campaign passes exactly that to every step
    c = campaign.Campaign.__new__(campaign.Campaign)
    c.cli, c.args = route, route.build_parser().parse_args(["campaign", "--backend", "krt", *argv])
    assert c.common_argv() == route.route_argv(a)


def test_corpus_entry_replays_every_routing_option(tmp_path):
    a = route.build_parser().parse_args(["corpus", "add", "--name", "x", "--backend", "freerouting", *ALL_ROUTE,
                                         "--reserve-spokes", "2"])
    attempt = {k: getattr(a, k) for k in corpus.RUN_KEYS if getattr(a, k, None) not in (None, [], False)}
    entry = {"name": "x", "board": "b.kicad_pcb", "reference_dir": "reference/p", "attempt": attempt,
             "_dir": tmp_path}
    ns = corpus.attempt_args(json.loads(json.dumps(entry, default=str)) | {"_dir": tmp_path})
    assert ns.backend == "freerouting" and ns.force is True and ns.label == "corpus-x"
    for dest in route.route_option_dests():
        if dest != "force":
            assert getattr(ns, dest) == getattr(a, dest), dest
    old = dict(entry, attempt={"backend": "krt", "nets": ["/A"]})   # an entry recorded before these options
    ns = corpus.attempt_args(old)
    assert ns.nets == ["/A"] and ns.budget == 300 and ns.protect == "existing" and ns.reserve_spokes is None


# ------------------------------------------------------------------ schemas

def evidence_results():
    runs = sorted((EVIDENCE / "runs").glob("*/result.json")) + sorted((EVIDENCE / "runs").glob("*/*/result.json"))
    assert runs, "no fixture evidence under reports/fixture/runs"
    return runs


def check_invariants(res, process=None):
    """docs/interface.md, Exit codes: what each exit code promises beyond the schema."""
    process = process or res.get("process") or {"status": route.PASS}
    assert res["exit_code"] == route.overall_exit(process, res["routing"], res["validation"])
    v, rt = res["validation"], res["routing"]
    if res["exit_code"] == 0:
        assert v["delivered_board_sha256"] == v["validated_board_sha256"]
        assert rt["unconnected_in_scope"] == 0 and rt["unrouted_nets"] == []
    if res["exit_code"] == 1:
        assert process["status"] == route.FAIL
        assert res.get("candidate") is None
    if res["exit_code"] == 2:
        assert rt["status"] == route.INCOMPLETE and "blocked_by" in rt and v["status"] == route.PASS
    if res["exit_code"] == 3:
        assert v["status"] != route.PASS and v["reasons"]


def test_schemas_are_valid_2020_12():
    for s in (RESULT_SCHEMA, REFUSAL_SCHEMA):
        jsonschema.Draft202012Validator.check_schema(s)
        assert s["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_fixture_evidence_meets_the_contract():
    """Every committed result.json (and against-origin/result.json) validates and keeps
    the exit-code promises. Evidence published before the contract has no `schema`
    field; it is checked with the id its kind implies (the strict check is below)."""
    checked = []
    for path in evidence_results():
        res = json.loads(path.read_text())
        legacy = "schema" not in res
        res.setdefault("schema", contract.schema_id(res["kind"]))
        errors = sorted(e.message for e in RESULTS.iter_errors(res))
        assert not errors, f"{path.relative_to(ROOT)}: {errors[:5]}"
        process = res.get("process")
        if res["kind"] == "route-validate" and process is None:   # records before validate carried it
            process = json.loads((path.parent.parent / "result.json").read_text())["process"]
        check_invariants(res, process)
        checked.append({"result": str(path.relative_to(EVIDENCE)), "kind": res["kind"],
                        "exit_code": res["exit_code"], "schema": res["schema"], "pre_contract": legacy})
    record("interface_contract", {"schema": "schemas/result-1.json", "validated": checked})


def test_fixture_evidence_carries_schema_ids():
    """Once evidence is published with this contract (`make evidence`), a result
    without its schema id fails."""
    results = [(p, json.loads(p.read_text())) for p in evidence_results()]
    if not any("schema" in r for _, r in results):
        pytest.skip("reports/fixture predates the result contract; `make evidence` republishes it with schema ids")
    missing = [str(p.relative_to(ROOT)) for p, r in results if r.get("schema") != contract.schema_id(r["kind"])]
    assert not missing, missing


def test_schema_rejects_broken_promises():
    base = json.loads((EVIDENCE / "runs" / "smoke-krt" / "result.json").read_text())
    base["schema"] = "kicad-route/route/1"
    assert not list(RESULTS.iter_errors(base))

    def broken(mutate):
        r = json.loads(json.dumps(base))
        mutate(r)
        return list(RESULTS.iter_errors(r))
    assert broken(lambda r: r.pop("schema"))
    assert broken(lambda r: r.update(schema="kicad-route/route/2"))
    assert broken(lambda r: r["validation"].update(status="FAIL", reasons=["x"]))   # exit 0 needs PASS
    assert broken(lambda r: r.update(exit_code=2))                                  # 2 needs INCOMPLETE
    assert broken(lambda r: r.update(exit_code=4))                                  # 4 is a refusal record
    assert broken(lambda r: r.update(candidate=None))
    assert broken(lambda r: r["validation"]["preservation"].update(footprints="FAIL"))
    assert broken(lambda r: r["input"].update(fingerprint="short"))
    assert broken(lambda r: r["routing"].update(status="INCOMPLETE"))              # 0 unconnected
    good = json.loads(json.dumps(base))
    good["new_informational_field"] = {"anything": 1}                              # additive: still /1
    assert not list(RESULTS.iter_errors(good))
    ref = contract.refusal_record(contract.Refusal("usage", "m"), ["x"], "run")
    assert not list(REFUSALS.iter_errors(ref))
    assert list(REFUSALS.iter_errors(dict(ref, exit_code=1)))
    assert list(REFUSALS.iter_errors(dict(ref, reason_class="other")))


def test_records_written_by_commands_carry_schema_ids():
    src = {n: (ROOT / "src" / "kicad_route" / f"{n}.py").read_text() for n in ("cli", "campaign", "floor")}
    for kind in ("route", "route-validate", "route-blocked", "audit", "doctor"):
        assert f'contract.schema_id("{kind}")' in src["cli"], kind
    assert 'contract.schema_id("route-campaign")' in src["campaign"]
    assert 'contract.schema_id("floor")' in src["floor"]
    assert "RESULT_LINE" not in src["campaign"] and "VALIDATE_LINE" not in src["campaign"]


# ---------------------------------------------------- machine mode (routes)

def test_json_runs_with_explicit_run_dirs_do_not_interfere():
    """Two concurrent `run --json --run-dir` attempts: each prints exactly its own
    result on stdout (progress on stderr), in the directory it was given, with no
    `latest` link. Then validate/blocked --json on one of them."""
    base = OUT / "runs" / "route_demo" / "iface-json"
    shutil.rmtree(base, ignore_errors=True)
    jobs = {"krt": ["--backend", "krt", "--default-clearance", "0.4"],
            "freerouting": ["--backend", "freerouting", "--clearance-margin", "0.01"]}

    def go(name):
        rd = base / name / "run"
        r = kr("--out", OUT, "run", "--json", "--dir", FIXTURE, "--nets", "/SIG_A*", "--budget", "180",
               "--run-dir", rd, *jobs[name], cwd=OUT)
        return name, rd, r

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        done = list(pool.map(go, jobs))
    transcripts = {}
    for name, rd, r in done:
        res = one_json(r.stdout)
        assert "[route]" in r.stderr and "[route]" not in r.stdout
        # exit code not pinned: this checks the --json contract (record on stdout, codes agree), not the fixture outcome (pinned in test_routing.py)
        assert r.returncode == res["exit_code"] in (0, 2, 3), r.stderr[-2000:]
        assert (OUT / res["run_dir"]).resolve() == rd.resolve()
        assert json.loads((rd / "result.json").read_text()) == res
        assert res["settings"]["backend"] == name and res["schema"] == "kicad-route/route/1"
        RESULTS.validate(res)
        check_invariants(res)
        assert not (rd.parent / "latest").exists()
        transcripts[name] = {"exit_code": r.returncode, "stdout_record": res["run_dir"],
                             "stderr_tail": r.stderr[-600:]}
    krt_dir = done[0][1]
    v = kr("--out", OUT, "validate", krt_dir, "--json", cwd=OUT)
    vres = one_json(v.stdout)
    assert v.returncode == vres["exit_code"] and vres["kind"] == "route-validate"
    RESULTS.validate(vres)
    assert json.loads((OUT / vres["dir"] / "result.json").read_text()) == vres
    b = kr("--out", OUT, "blocked", krt_dir, "--json", cwd=OUT)
    bres = one_json(b.stdout)
    assert b.returncode == bres["exit_code"] and bres["schema"] == "kicad-route/route-blocked/1"
    record("interface_json_runs", {"runs": transcripts, "validate": vres["schema"], "blocked": bres["schema"]})


def test_json_campaign_reads_child_records():
    r = kr("--out", OUT, "campaign", "--json", "--backend", "krt", "--budget", "120", "--default-clearance", "0.4",
           "--dir", FIXTURE, "--nets", "/SIG_A*", "--label", "iface-campaign", cwd=OUT, timeout=1800)
    rec = one_json(r.stdout)
    assert "[campaign]" in r.stderr and r.returncode == rec["exit_code"] == 0, r.stderr[-3000:]
    RESULTS.validate(rec)
    assert json.loads((OUT / rec["campaign_dir"] / "campaign.json").read_text()) == rec
    step = rec["chains"][0]["steps"][0]
    assert step["status"] == "ok" and "--json" in step["argv"]
    assert "[route]" in (OUT / step["log"]).read_text()      # the child's progress went to its log
    record("interface_json_campaign", {k: rec[k] for k in ("schema", "exit_code", "stop_reason", "best")})


@pytest.mark.parametrize("cmd", [
    ["floor", "--dir", str(FIXTURE)],
    ["audit", "--dir", str(FIXTURE), "--old", "HEAD~1", "--new", "HEAD"],
    ["doctor"],
])
def test_json_reporting_commands_print_one_record(cmd):
    r = kr("--out", OUT, *cmd, "--json", cwd=OUT, timeout=1800)
    rec = one_json(r.stdout)
    assert rec["schema"] == contract.schema_id(rec["kind"]) and r.returncode == rec["exit_code"], r.stderr[-2000:]
