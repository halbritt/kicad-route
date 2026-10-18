"""KICADROUTE-59: --ledger and --require-readonly-state describe the environment of a
run, not its attempt (cli.add_environment_args). `corpus add` honours them for its
add-time run but never records them in entry.json; `corpus check` takes its own; an
entry recorded before the split that still carries them is replayed without them.
`campaign` still forwards both to every step (tests/test_doctor_sandbox.py,
test_campaign_passes_sandbox_flags_to_runs).
"""
import json

import pytest

from helpers import FIXTURE
from kicad_route import cli as route
from kicad_route import common, corpus, sandbox

ENV = {"ledger", "require_readonly_state"}


@pytest.fixture
def out(tmp_path, monkeypatch):
    """A private OUT, restored afterwards (common.out_dir is process-global)."""
    monkeypatch.setattr(common, "_out", None)
    common.set_out(tmp_path / "out")
    yield tmp_path / "out"


@pytest.fixture
def runs(monkeypatch):
    """Replace cli.run_attempt: record each Namespace, route nothing; a check of it passes."""
    seen = []

    def fake_run_attempt(ns):
        seen.append(ns)
        return {"refused": "test stub", "exit_code": 4}

    monkeypatch.setattr(route, "run_attempt", fake_run_attempt)
    monkeypatch.setattr(corpus, "compare", lambda base, now: ([], []))   # the stub is no regression
    return seen


def refusal(out):
    return json.loads((out / "refusals" / "refusal-latest.json").read_text())


def write_entry(root, name, attempt):
    """An entry as `corpus add` writes it (reference snapshot not needed: runs are stubbed)."""
    d = root / name
    (d / "reference" / "p").mkdir(parents=True)
    (d / "entry.json").write_text(json.dumps({
        "name": name, "board": "b.kicad_pcb", "schematic": None, "reference_dir": "reference/p",
        "attempt": attempt, "baseline": {"refused": "test stub", "exit_code": 4}}))
    return d


def test_environment_options_are_not_routing_options():
    assert set(route.ENVIRONMENT_DESTS) == ENV
    assert not ENV & set(route.route_option_dests())
    assert not ENV & set(corpus.RUN_KEYS)
    parser = route.build_parser()
    for argv in (["run", "--backend", "krt"], ["campaign", "--backend", "krt"],
                 ["corpus", "add", "--name", "x"], ["corpus", "check"]):
        a = parser.parse_args([*argv, "--ledger", "/l/ledger.jsonl", "--require-readonly-state"])
        assert a.ledger == "/l/ledger.jsonl" and a.require_readonly_state is True, argv


def test_corpus_add_records_neither_environment_option(out, tmp_path, monkeypatch, runs):
    monkeypatch.chdir(tmp_path)
    args = route.build_parser().parse_args(
        ["corpus", "add", "--dir", str(FIXTURE), "--name", "env", "--root", str(tmp_path / "corpus"),
         "--budget", "7", "--ledger", "add-ledger.jsonl", "--require-readonly-state"])
    assert corpus.cmd_corpus(args) == 0   # in-process: dispatch's writer refusal is not under test here
    attempt = json.loads((tmp_path / "corpus" / "env" / "entry.json").read_text())["attempt"]
    assert attempt["budget"] == 7 and attempt["backend"] == "krt"
    assert not ENV & set(attempt), attempt
    # the add-time run itself ran in the environment it was given
    assert runs[0].ledger == str(tmp_path / "add-ledger.jsonl") and runs[0].require_readonly_state is True


def test_old_entry_carrying_environment_keys_is_replayed_without_them(tmp_path):
    e = {"name": "old", "board": "b.kicad_pcb", "reference_dir": "reference/p", "_dir": tmp_path,
         "attempt": {"backend": "krt", "nets": ["/A"], "ledger": "/some/host/ledger.jsonl",
                     "require_readonly_state": True}}
    ns = corpus.attempt_args(e)
    assert ns.nets == ["/A"] and ns.ledger is None and ns.require_readonly_state is False
    ns = corpus.attempt_args(e, ledger="/here/ledger.jsonl", require_readonly_state=False)
    assert ns.ledger == "/here/ledger.jsonl" and ns.require_readonly_state is False
    with pytest.raises(TypeError):
        corpus.attempt_args(e, budget=5)   # only environment options come from the command


def test_corpus_check_uses_its_own_ledger_not_the_entrys(out, tmp_path, monkeypatch, runs):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(sandbox.ENV_FLAG, raising=False)
    root = tmp_path / "corpus"
    write_entry(root, "old", {"backend": "krt", "budget": 9, "ledger": "/some/host/ledger.jsonl",
                              "require_readonly_state": True})
    o = str(out)
    # an old entry recorded under the flag is checked without it: no refusal on a writable STATE
    assert route.main(["--out", o, "corpus", "check", "--root", str(root)]) == 0
    assert runs[-1].budget == 9 and runs[-1].ledger is None and runs[-1].require_readonly_state is False
    assert route.main(["--out", o, "corpus", "check", "--root", str(root), "--ledger", "mine.jsonl"]) == 0
    assert runs[-1].ledger == str(tmp_path / "mine.jsonl") and runs[-1].require_readonly_state is False
    assert len(runs) == 2


def test_corpus_check_honours_its_own_require_readonly_state(out, tmp_path, monkeypatch, runs):
    monkeypatch.delenv(sandbox.ENV_FLAG, raising=False)
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(common, "STATE", state)
    root = tmp_path / "corpus"
    write_entry(root, "e", {"backend": "krt"})
    o = str(out)
    # a writable STATE with no backend: refused before any entry runs
    assert route.main(["--out", o, "corpus", "check", "--root", str(root), "--require-readonly-state"]) == 4
    rec = refusal(out)
    assert rec["reason_class"] == "state" and "--require-readonly-state" in rec["message"], rec
    assert runs == [] and not (root / "e" / "checks").exists()
    # STATE acceptable (stubbed): a corpus inside it would need writing there, so that is refused too
    monkeypatch.setattr(sandbox, "state_refusal", lambda command="run", state=None: None)
    inside = state / "corpus"
    write_entry(inside, "e", {"backend": "krt"})
    assert route.main(["--out", o, "corpus", "check", "--root", str(inside), "--require-readonly-state"]) == 4
    assert refusal(out)["reason_class"] == "state" and "inside the read-only STATE" in refusal(out)["message"]
    assert runs == []
    # a corpus outside STATE runs, and every entry's run carries the flag
    assert route.main(["--out", o, "corpus", "check", "--root", str(root), "--require-readonly-state"]) == 0
    assert len(runs) == 1 and runs[0].require_readonly_state is True
