"""Source and build identity for the routing implementation owned by this package."""
import hashlib
import json
from pathlib import Path

from . import common


ROOT = Path(__file__).resolve().parent / "router"


def source_sha256():
    digest = hashlib.sha256()
    for path in sorted(ROOT.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            digest.update(path.relative_to(ROOT).as_posix().encode() + b"\0")
            digest.update(path.read_bytes())
    return digest.hexdigest()


def identity():
    checksum = source_sha256()
    return {
        "version": (ROOT / "VERSION").read_text().strip(),
        "build": "owned-" + checksum[:12],
        "commit": common.git(common.TOOL_ROOT, "rev-parse", "HEAD"),
        "source": "https://github.com/halbritt/kicad-route",
        "source_sha256": checksum,
        "upstream": json.loads((ROOT / "UPSTREAM.json").read_text()),
    }
