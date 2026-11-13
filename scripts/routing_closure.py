#!/usr/bin/env python3
"""Inventory the frozen router's source dependencies without importing its code.

This is a conservative static closure: imports inside optional branches count.
Nonliteral imports and sibling-script references remain explicit review items.
Routing and native KiCad comparisons must establish the runtime closure.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import subprocess


# The two routing entry points the adapters run, and the diagnostic tools the generated
# krt-* skills name. Their static import closure is what the engine carries from upstream.
ENTRYPOINTS = (
    "py_router/route.py", "py_router/route_diff.py",
    "py_router/check_connected.py", "py_router/check_drc.py", "py_router/check_weird.py",
    "py_router/list_nets.py", "py_tools/check_orphan_stubs.py",
    "py_tools/check_impedance.py", "py_tools/check_reachability.py", "py_tools/board_score.py",
)
SOURCE_DIRS = ("py_router", "py_tools", "py_placer", "rust_router", ".")
# Upstream files carried besides the import closure: licence and version, the numeric
# requirements, the Rust crate, the two skills kicad-route adapts, and the two upstream
# test files the engine's own tests use (scripts/kernel-tests.py, router/tests/).
ASSETS = (
    "LICENSE", "VERSION", "requirements.txt", "rust_router/Cargo.toml", "rust_router/Cargo.lock",
    ".claude/skills/diagnose-routing-failures/SKILL.md", ".claude/skills/review-routed-board/SKILL.md",
    "tests/synth.py", "tests/test_obstacle_addremove_parity.py",
)


def module_files(base, name):
    parts = name.split(".") if name else []
    target = base.joinpath(*parts)
    module = target.with_suffix(".py") if parts else None
    package = target / "__init__.py"
    # A package wins over a module of the same name beside it, as in Python's own import: upstream
    # has qfn_fanout.py (a command-line front) beside the package qfn_fanout/ that it runs, and
    # `from qfn_fanout import ...` means the package.
    if package.is_file():
        leaf = package
    elif module and module.is_file():
        leaf = module
    else:
        return []
    parents = [base.joinpath(*parts[:i], "__init__.py")
               for i in range(1, len(parts))]
    return [p for p in parents if p.is_file()] + [leaf]


def resolve(root, source, name, level=0):
    if level:
        base = source.parent
        for _ in range(level - 1):
            base = base.parent
        return module_files(base, name) if base.is_relative_to(root) else []
    for base in [source.parent, *(root / d for d in SOURCE_DIRS)]:
        found = module_files(base, name)
        if found:
            return found
    return []


def import_names(node):
    if isinstance(node, ast.Import):
        return [(n.name, 0, True) for n in node.names]
    if isinstance(node, ast.ImportFrom):
        base = node.module or ""
        return [(base, node.level, bool(base)), *[
            ((base + "." if base else "") + n.name, node.level, False)
            for n in node.names if n.name != "*"
        ]]
    return []


def dynamic_call(node):
    if not isinstance(node, ast.Call):
        return False
    return ((isinstance(node.func, ast.Name) and node.func.id == "__import__")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "import_module"))


def inspect_source(root, source):
    tree = ast.parse(source.read_bytes(), filename=str(source))
    imports, external, dynamic, scripts = set(), set(), [], []
    for node in ast.walk(tree):
        names = import_names(node)
        if dynamic_call(node):
            if (node.args and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                names = [(node.args[0].value, 0, True)]
            else:
                dynamic.append({"source": source.relative_to(root).as_posix(),
                                "line": node.lineno, "expression": ast.unparse(node)})
        for name, level, required in names:
            found = resolve(root, source, name, level)
            imports.update(found)
            if not found and required and not level:
                external.add(name.split(".")[0])
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.endswith(".py") and "\n" not in node.value and len(node.value) < 200:
                scripts.append({"source": source.relative_to(root).as_posix(),
                                "line": node.lineno, "literal": node.value})
    return imports, external, dynamic, scripts


def file_identity(path):
    body = path.read_bytes()
    return {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body),
            "lines": len(body.splitlines())}


def inventory(root, entrypoints=ENTRYPOINTS):
    root = Path(root).resolve()
    pending = [root / p for p in entrypoints]
    seen, edges, external, dynamic, scripts = set(), {}, set(), [], []
    while pending:
        source = pending.pop()
        if source in seen:
            continue
        seen.add(source)
        imports, other, calls, refs = inspect_source(root, source)
        edges[source.relative_to(root).as_posix()] = sorted(
            p.relative_to(root).as_posix() for p in imports)
        pending.extend(imports - seen)
        external.update(other)
        dynamic.extend(calls)
        scripts.extend(refs)
    files = {p.relative_to(root).as_posix(): file_identity(p) for p in sorted(seen)}
    assets = [*ASSETS, *[p.relative_to(root).as_posix()
                         for p in sorted((root / "rust_router" / "src").glob("*.rs"))]]
    return {
        "schema": "kicad-route/routing-source-closure/1",
        "entrypoints": list(entrypoints), "runtime_closure_proven": False,
        "files": files, "imports": dict(sorted(edges.items())),
        "totals": {"python_files": len(files),
                   "python_lines": sum(f["lines"] for f in files.values()),
                   "python_bytes": sum(f["bytes"] for f in files.values())},
        "external_modules": sorted(external),
        "dynamic_imports": sorted(dynamic, key=lambda r: (r["source"], r["line"])),
        "script_references": sorted(scripts, key=lambda r: (r["source"], r["line"])),
        "assets": {name: file_identity(root / name) for name in assets
                   if (root / name).is_file()},
    }


def git_value(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--entrypoint", action="append")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if git_value(args.source, "status", "--porcelain", "--untracked-files=normal"):
        parser.error("baseline source must be clean, including untracked files")
    report = inventory(args.source, args.entrypoint or ENTRYPOINTS)
    report["source"] = {"commit": git_value(args.source, "rev-parse", "HEAD"),
                        "tree": git_value(args.source, "rev-parse", "HEAD^{tree}"),
                        "origin": git_value(args.source, "remote", "get-url", "origin")}
    body = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(body)
    else:
        print(body, end="")


if __name__ == "__main__":
    main()
