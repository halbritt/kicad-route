"""Source and build identity for the routing implementation owned by this package."""
import hashlib
import json
from pathlib import Path

from . import common


SOURCE_ROOT = Path(__file__).resolve().parent / "router"
SELECTION_ERROR = None
try:
    GENERATION = common.router_generation()
except common.RouterSelectionError as error:
    GENERATION, ROOT, SELECTION_ERROR = None, None, error
else:
    ROOT = GENERATION / "router" if GENERATION else SOURCE_ROOT


def require_runtime():
    if SELECTION_ERROR is not None:
        raise SELECTION_ERROR


def source_sha256(root=None):
    if root is None:
        require_runtime()
    root = Path(ROOT if root is None else root)
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            digest.update(path.relative_to(root).as_posix().encode() + b"\0")
            digest.update(path.read_bytes())
    return digest.hexdigest()


def identity(root=None):
    if root is None:
        require_runtime()
    root = Path(ROOT if root is None else root)
    checksum = source_sha256(root)
    return {
        "version": (root / "VERSION").read_text().strip(),
        "build": "owned-" + checksum[:12],
        "commit": common.git(common.TOOL_ROOT, "rev-parse", "HEAD"),
        "source": "https://github.com/halbritt/kicad-route",
        "source_sha256": checksum,
        "upstream": json.loads((root / "UPSTREAM.json").read_text()),
    }
