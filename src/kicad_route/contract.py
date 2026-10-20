"""The machine interface of kicad-route (docs/interface.md): schema ids, the one
refusal path (exit 4 with a record), the parser every command uses, and --json
output (one JSON object on stdout, everything else on stderr).

Schema ids are "kicad-route/<kind>/<major>". Opt-in pair evidence uses major 2;
unchanged records keep major 1 (docs/interface.md lists the stable fields).
The JSON Schemas live in schemas/ at the checkout root.
"""
import argparse
import contextlib
import io
import json
import os
import sys
from pathlib import Path

from . import common

SCHEMA_MAJOR = 1
REFUSED_EXIT = 4
# usage: the command line itself; input: the project, run or revision named;
# ledger / canary: the pre-flight gates of `run`; state: kicad-route's own state
# (an existing --run-dir, an existing corpus entry).
REASON_CLASSES = ("usage", "input", "ledger", "canary", "state")
SCHEMAS_DIR = common.RESOURCE_ROOT / "schemas"


def schema_id(kind, major=None):
    return f"kicad-route/{kind}/{SCHEMA_MAJOR if major is None else major}"


class Refusal(SystemExit):
    """A failure before any backend ran. main() turns it into exit 4 and a refusal
    record (OUT/refusals/). It is a SystemExit (code 4), so a caller that already
    catches SystemExit keeps working, and str() is the message."""

    def __init__(self, reason_class, message, input_fingerprint=None, usage=None, **detail):
        if reason_class not in REASON_CLASSES:
            raise ValueError(f"unknown refusal class {reason_class!r}")
        super().__init__(message)
        self.code = REFUSED_EXIT
        self.reason_class = reason_class
        self.message = message
        self.input_fingerprint = input_fingerprint
        self.usage = usage
        self.detail = detail

    def __str__(self):
        return self.message


def refuse(reason_class, message, **kw):
    raise Refusal(reason_class, message, **kw)


# ------------------------------------------------------------------ the parser

def add_global_args(p, root=False):
    """--out and --json, accepted before or after the subcommand. On subparsers the
    default is SUPPRESS, so a value given before the subcommand is not reset."""
    d = {} if root else {"default": argparse.SUPPRESS}
    p.add_argument("--out", metavar="DIR", **d,
                   help="where runs, caches and reports go (default ./build/kicad-route or $KICAD_ROUTE_OUT)")
    p.add_argument("--json", action="store_true", **({"default": False} if root else d),
                   help="machine mode: exactly one JSON object (the command's record or a refusal) on "
                        "stdout; progress on stderr (docs/interface.md)")


class Parser(argparse.ArgumentParser):
    """Every kicad-route parser and subparser: a usage error is a refusal (exit 4),
    never argparse's exit 2 (which means "valid but incomplete" here). Option
    abbreviations are off so adding an option never changes what a caller's
    command line means."""

    def __init__(self, *args, root=False, **kw):
        kw.setdefault("allow_abbrev", False)
        super().__init__(*args, **kw)
        add_global_args(self, root=root)

    def error(self, message):
        raise Refusal("usage", message, usage=self.format_usage(), prog=self.prog)


def prescan(argv):
    """(json, out) from a raw argv, for refusals raised while parsing it."""
    want_json, out = False, None
    it = iter(argv)
    for tok in it:
        if tok == "--":
            break
        if tok == "--json":
            want_json = True
        elif tok == "--out":
            out = next(it, None)
        elif tok.startswith("--out="):
            out = tok.split("=", 1)[1]
    return want_json, out


# -------------------------------------------------------------------- refusals

def _link(target_name, link):
    tmp = link.with_name(f".{link.name}.{os.getpid()}")
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(target_name)
    os.replace(tmp, link)  # atomic: concurrent refusals never see a missing link


def refusal_record(r, argv, command=None):
    return {"schema": schema_id("refusal"), "kind": "refusal", "exit_code": REFUSED_EXIT,
            "command": command, "reason_class": r.reason_class, "message": r.message,
            "argv": [str(a) for a in argv], "cwd": str(Path.cwd()),
            "input_fingerprint": r.input_fingerprint,
            "discarded_run_dir": r.detail.get("discarded_run_dir"),
            "refused_utc": common.now_utc().isoformat(), "record": None}


def write_refusal(r, argv, command=None):
    """OUT/refusals/<stamp>-<pid>.json and the refusal-latest.json link. The record
    is returned even when OUT cannot be written (its `record` is then null)."""
    rec = refusal_record(r, argv, command)
    d = common.out_dir() / "refusals"
    name = f"{common.now_utc().strftime('%Y%m%dT%H%M%S.%fZ')}-{os.getpid()}.json"
    try:
        d.mkdir(parents=True, exist_ok=True)
        rec["record"] = common.rel(d / name)
        common.write_json(d / name, rec)
        _link(name, d / "refusal-latest.json")
    except OSError as e:
        rec["record"] = None
        rec["record_error"] = str(e)
    return rec


def report_refusal(r, rec, json_mode):
    """The human lines (machine mode prints the record instead)."""
    if json_mode:
        print(f"kicad-route: refused ({r.reason_class}): {r.message}", file=sys.stderr)
        return
    if r.reason_class == "usage":
        if r.usage:
            sys.stderr.write(r.usage)
        print(f"{r.detail.get('prog') or 'kicad-route'}: error: {r.message}", file=sys.stderr)
        print(f"kicad-route: refusal record {rec['record']} (exit {REFUSED_EXIT})", file=sys.stderr)
        return
    print(f"[kicad-route] refused ({r.reason_class}): {r.message}", flush=True)
    print(f"[kicad-route] refusal record: {rec['record']}  (exit {REFUSED_EXIT})", flush=True)


# ---------------------------------------------------------------- machine mode

@contextlib.contextmanager
def machine_output(enabled):
    """In machine mode everything written to stdout (by this process and by any
    child that inherits it) goes to stderr; emit(record) writes the one JSON object
    to the real stdout. Outside machine mode emit does nothing."""
    if not enabled:
        yield lambda rec: None
        return
    sys.stdout.flush()
    real = sys.stdout
    fd = saved = None
    try:
        fd = real.fileno()
        saved = os.dup(fd)
        os.dup2(sys.stderr.fileno(), fd)
    except (AttributeError, OSError, ValueError, io.UnsupportedOperation):
        fd = saved = None
    emitted = []

    def emit(rec):
        if emitted or rec is None:
            return
        emitted.append(True)
        text = json.dumps(rec, default=str) + "\n"
        if saved is not None:
            os.write(saved, text.encode())
        else:
            real.write(text)
            real.flush()

    try:
        with contextlib.redirect_stdout(sys.stderr):
            yield emit
    finally:
        real.flush()
        sys.stdout.flush()
        sys.stderr.flush()
        if saved is not None:
            os.dup2(saved, fd)
            os.close(saved)
