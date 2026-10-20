"""Migration inventories must include package initialization and expose unknowns."""
import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "routing_closure.py"


def load_inventory():
    spec = importlib.util.spec_from_file_location("routing_closure", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_inventory_follows_packages_and_literal_dynamic_imports(tmp_path):
    source = tmp_path / "source"
    (source / "py_router" / "geometry").mkdir(parents=True)
    (source / "py_router" / "route.py").write_text(
        "from geometry.paths import route\n"
        "import importlib\n"
        "importlib.import_module('cleanup')\n"
        "importlib.import_module(selected_plugin)\n"
    )
    (source / "py_router" / "geometry" / "__init__.py").write_text("import bootstrap\n")
    (source / "py_router" / "geometry" / "paths.py").write_text("from . import costs\n")
    (source / "py_router" / "geometry" / "costs.py").write_text("import numpy\n")
    (source / "py_router" / "bootstrap.py").write_text("READY = True\n")
    (source / "py_router" / "cleanup.py").write_text("CLEAN = True\n")
    (source / "py_router" / "unused.py").write_text("raise RuntimeError('unused')\n")

    report = load_inventory().inventory(source, ["py_router/route.py"])

    assert set(report["files"]) == {
        "py_router/route.py", "py_router/bootstrap.py", "py_router/cleanup.py",
        "py_router/geometry/__init__.py", "py_router/geometry/paths.py",
        "py_router/geometry/costs.py",
    }
    assert report["external_modules"] == ["importlib", "numpy"]
    assert report["dynamic_imports"] == [{
        "source": "py_router/route.py", "line": 4,
        "expression": "importlib.import_module(selected_plugin)",
    }]
    assert report["files"]["py_router/cleanup.py"]["sha256"]
    assert not report["runtime_closure_proven"]
