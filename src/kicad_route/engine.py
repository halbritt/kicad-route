"""Source and build identity for the routing implementation owned by this package."""
import hashlib
import json
from pathlib import Path

from . import common


SOURCE_ROOT = Path(__file__).resolve().parent / "router"
GENERATION = common.router_generation()
ROOT = GENERATION / "router" if GENERATION else SOURCE_ROOT


def source_sha256(root=None):
    root = Path(root or ROOT)
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            digest.update(path.relative_to(root).as_posix().encode() + b"\0")
            digest.update(path.read_bytes())
    return digest.hexdigest()


def identity(root=None):
    root = Path(root or ROOT)
    checksum = source_sha256(root)
    return {
        "version": (root / "VERSION").read_text().strip(),
        "build": "owned-" + checksum[:12],
        "commit": common.git(common.TOOL_ROOT, "rev-parse", "HEAD"),
        "source": "https://github.com/halbritt/kicad-route",
        "source_sha256": checksum,
        "upstream": json.loads((root / "UPSTREAM.json").read_text()),
    }
