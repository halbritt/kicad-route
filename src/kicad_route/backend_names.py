"""Public backend names and stable keys for pre-rename persisted identities."""
import sys


CHOICES = ("builtin", "freerouting")
HELP = ("builtin (default): engine built from kicad-route source; "
        "freerouting: external engine; krt is a deprecated alias for builtin")


def canonical(name):
    return "builtin" if name == "krt" else name


def storage_key(name):
    # State and fingerprint keys retain their original spelling across the rename.
    return "krt" if name == "builtin" else name


def parse(name):
    if name == "krt":
        print("[deprecated] --backend krt is an alias for builtin; use --backend builtin "
              "(the engine built from kicad-route source).", file=sys.stderr)
    return canonical(name)
