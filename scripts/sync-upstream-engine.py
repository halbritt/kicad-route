#!/usr/bin/env python3
"""Move the routing engine onto a newer upstream KRT commit, keeping the local changes.

    scripts/sync-upstream-engine.py [--upstream CLONE] [--ref REF] [--status] [--upstream-only] [--no-fetch]
    scripts/sync-upstream-engine.py --refresh-config-fields

The engine in src/kicad_route/router/ is upstream KRT at the commit recorded in its
UPSTREAM.json (`upstream_commit`) plus a small set of local changes. This command

1. fetches upstream (a clone at --upstream, default $KICAD_ROUTE_UPSTREAM_CLONE or
   ~/.cache/kicad-route/KiCadRoutingTools, made on first use),
2. exports the recorded commit (old) and the target commit (new, default origin/main),
3. takes the files the engine carries from the new tree: the static import closure of the
   entry points in scripts/routing_closure.py plus its fixed asset list,
4. merges every carried file three ways (ours, old, new) in place, adds files upstream
   added, removes unchanged files upstream no longer has or the entry points no longer
   import, and leaves local files alone,
5. rewrites the two files that restate GridRouteConfig's fields from the merged class:
   py_router/effective_config_fields.json and the field lists in
   schemas/effective-configuration-1.json (what a run's configuration record is checked against),
6. records the new commit in UPSTREAM.json and prints what conflicted.

A conflicted file holds standard `<<<<<<<`/`|||||||`/`=======`/`>>>>>>>` markers (ours, old
upstream, new upstream). The exit status is 1 while any conflict is left. Nothing is
committed; review `git status` and `git diff`, resolve, rebuild and test (docs/upstream-engine.md).

--status changes nothing: it lists how the engine differs from the recorded upstream commit,
which is the local change set UPSTREAM.json describes.

--upstream-only takes upstream's version of every upstream file and so drops the local
changes (local files stay). It is for measuring what upstream alone does with this package.
"""
import argparse
import ast
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "src" / "kicad_route" / "router"
RECORD = ENGINE / "UPSTREAM.json"


def closure_module():
    spec = importlib.util.spec_from_file_location("routing_closure", ROOT / "scripts" / "routing_closure.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(repo, *args, check=True, binary=False):
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=not binary)
    if check and done.returncode:
        raise SystemExit(f"git {' '.join(args)} failed in {repo}:\n{done.stderr if not binary else done.stderr.decode()}")
    return done.stdout if binary else done.stdout.strip()


def ensure_clone(path, url, fetch):
    path = Path(path).expanduser()
    if not (path / ".git").exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--quiet", url, str(path)], check=True)
    elif fetch:
        subprocess.run(["git", "-C", str(path), "fetch", "--quiet", "--tags", "origin"], check=True)
    return path


def export(clone, commit, destination):
    """The tree of `commit`, as plain files (no checkout: the clone's files are not touched)."""
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    archive = subprocess.Popen(["git", "-C", str(clone), "archive", commit], stdout=subprocess.PIPE)
    subprocess.run(["tar", "-x", "-C", str(destination)], stdin=archive.stdout, check=True)
    if archive.wait():
        raise SystemExit(f"git archive {commit} failed")
    return destination


def carried(tree, closure):
    """Paths the engine carries from an upstream tree: the import closure plus the assets."""
    entry = [e for e in closure.ENTRYPOINTS if (tree / e).is_file()]
    missing = sorted(set(closure.ENTRYPOINTS) - set(entry))
    report = closure.inventory(tree, entry)
    return set(report["files"]) | set(report["assets"]), missing


def ours_files():
    listed = git(ROOT, "ls-files", "--cached", "--others", "--exclude-standard", "--",
                 str(ENGINE.relative_to(ROOT)))
    prefix = ENGINE.relative_to(ROOT).as_posix() + "/"
    return {line[len(prefix):] for line in listed.splitlines() if (ROOT / line).is_file()}


def same(a, b):
    return a.read_bytes() == b.read_bytes()


def merge(ours, old, new):
    """Three-way merge into `ours`; the number of conflicts (0 when clean)."""
    done = subprocess.run(["git", "merge-file", "--diff3", "-L", "local", "-L", "upstream-old", "-L", "upstream-new",
                           str(ours), str(old), str(new)], capture_output=True, text=True)
    if done.returncode < 0 or done.returncode > 127:
        raise SystemExit(f"git merge-file failed on {ours}: {done.stderr}")
    return done.returncode


def config_fields():
    """GridRouteConfig's dataclass fields in declaration order, read from source (nothing is imported)."""
    tree = ast.parse((ENGINE / "py_router" / "routing_config.py").read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "GridRouteConfig")
    return [node.target.id for node in cls.body
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
            and "ClassVar" not in ast.unparse(node.annotation)]


CONFIG_SCHEMA = ROOT / "schemas" / "effective-configuration-1.json"


def config_field_files(fields):
    """{path: text} of the two files that restate GridRouteConfig's fields: the engine's
    inventory, and the configuration record schema (the field names of a complete record, and
    the required configuration keys: every field but `rules`, which the record carries
    beside the configuration)."""
    schema = json.loads(CONFIG_SCHEMA.read_text())
    schema["properties"]["configuration"]["required"] = [name for name in fields if name != "rules"]
    for clause in schema.get("allOf", ()):          # a complete record names exactly these fields
        names = clause.get("then", {}).get("properties", {}).get("field_names")
        if names and "const" in names:
            names["const"] = list(fields)
    return {ENGINE / "py_router" / "effective_config_fields.json": json.dumps(fields, indent=2) + "\n",
            CONFIG_SCHEMA: json.dumps(schema, indent=2) + "\n"}


def refresh_config_fields():
    """Rewrite both field lists when the merged config parses; True when either changed."""
    try:
        files = config_field_files(config_fields())
    except (SyntaxError, StopIteration):          # conflict markers: refresh after resolving
        return None
    changed = False
    for path, text in files.items():
        if not path.is_file() or path.read_text() != text:
            path.write_text(text)
            changed = True
    return changed


def plan(old_tree, new_tree, closure, local_files):
    """(action, path) rows; nothing is written. `local_files` are kicad-route's own
    (UPSTREAM.json): never merged, even where upstream has a file of the same name."""
    new_set, missing = carried(new_tree, closure)
    new_set -= set(local_files)
    rows = []
    for path in sorted(new_set | ours_files()):
        ours, old, new = ENGINE / path, old_tree / path, new_tree / path
        if path in local_files:
            rows.append(("local-file", path))
        elif path in new_set:
            if not ours.exists():
                rows.append(("add", path))
            elif not old.is_file():
                rows.append(("same" if same(ours, new) else "both-added", path))
            elif same(ours, old):
                rows.append(("same" if same(old, new) else "update", path))
            else:
                rows.append(("local" if same(old, new) else "merge", path))
        elif old.is_file():
            # upstream no longer has it, or the entry points no longer import it
            rows.append(("remove" if same(ours, old) else "local-orphan", path))
        else:
            rows.append(("unlisted", path))
    return rows, missing


def status(old_tree, closure, record):
    """How the engine differs from the recorded upstream commit, against what UPSTREAM.json says."""
    local_files = set(record.get("local_files", ()))
    rows, _ = plan(old_tree, old_tree, closure, local_files)
    by_action = {}
    for action, path in rows:
        by_action.setdefault(action, []).append(path)
    changed = by_action.get("local", []) + by_action.get("local-orphan", [])
    described = {path for change in record.get("local_changes", ()) for path in change.get("files", ())}
    print(f"upstream files changed locally ({len(changed)}):")
    for path in sorted(changed):
        diff = subprocess.run(["diff", str(old_tree / path), str(ENGINE / path)], capture_output=True, text=True)
        lines = sum(1 for line in diff.stdout.splitlines() if line[:1] in "<>")
        print(f"  {path}  ({lines} lines)" + ("" if path in described else "  NOT in local_changes"))
    print(f"local files ({len(by_action.get('local-file', []))})")
    problems = [f"local_changes names an unchanged or absent file: {path}" for path in sorted(described - set(changed))]
    problems += [f"local_files names a missing file: {path}" for path in sorted(local_files)
                 if not (ENGINE / path).is_file()]
    problems += [f"neither upstream's nor in local_files: {path}" for path in by_action.get("unlisted", [])]
    problems += [f"carried upstream file is missing: {path}" for path in by_action.get("add", [])]
    problems += [f"unchanged upstream file outside the carried set: {path}" for path in by_action.get("remove", [])]
    problems += [f"changed locally but not described in local_changes: {path}"
                 for path in sorted(set(changed) - described)]
    for path, text in config_field_files(config_fields()).items():
        if not path.is_file() or path.read_text() != text:
            problems.append(f"{path.relative_to(ROOT)} does not restate GridRouteConfig's fields "
                            "(run this command with --refresh-config-fields)")
    for problem in problems:
        print("PROBLEM " + problem)
    return 1 if problems else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--upstream", default=os.environ.get("KICAD_ROUTE_UPSTREAM_CLONE")
                        or Path.home() / ".cache" / "kicad-route" / "KiCadRoutingTools",
                        help="an upstream git clone (fetched, never checked out or edited)")
    parser.add_argument("--ref", default="origin/main", help="the upstream commit to move to (default origin/main)")
    parser.add_argument("--work", type=Path, default=ROOT / "build" / "sync-upstream-engine",
                        help="where the old and new upstream trees are exported")
    parser.add_argument("--status", action="store_true",
                        help="list the local differences from the recorded upstream commit and stop")
    parser.add_argument("--upstream-only", action="store_true",
                        help="take upstream's version of every upstream file, dropping the local changes")
    parser.add_argument("--no-fetch", action="store_true", help="use the clone as it is")
    parser.add_argument("--refresh-config-fields", action="store_true",
                        help="only rewrite the two files that restate GridRouteConfig's fields and stop")
    args = parser.parse_args()

    if args.refresh_config_fields:
        changed = refresh_config_fields()
        print("config field lists: " + {True: "rewritten", False: "already current",
                                        None: "routing_config.py does not parse"}[changed])
        return 1 if changed is None else 0
    record = json.loads(RECORD.read_text())
    clone = ensure_clone(args.upstream, record["upstream"], fetch=not args.no_fetch)
    closure = closure_module()
    old_commit = git(clone, "rev-parse", record["upstream_commit"] + "^{commit}")
    old_tree = export(clone, old_commit, args.work / "old")
    if args.status:
        return status(old_tree, closure, record)

    new_commit = git(clone, "rev-parse", args.ref + "^{commit}")
    new_tree = export(clone, new_commit, args.work / "new")
    rows, missing = plan(old_tree, new_tree, closure, set(record.get("local_files", ())))
    conflicts = []
    counts = {}
    for action, path in rows:
        if args.upstream_only:
            action = {"merge": "update", "local": "update", "both-added": "update",
                      "local-orphan": "remove"}.get(action, action)
        counts[action] = counts.get(action, 0) + 1
        ours, old, new = ENGINE / path, old_tree / path, new_tree / path
        if action in ("add", "update"):
            ours.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(new, ours)
        elif action == "remove":
            ours.unlink()
        elif action == "merge":
            found = merge(ours, old, new)
            if found:
                conflicts.append((path, f"{found} conflict(s)"))
        elif action == "both-added":
            conflicts.append((path, f"local file and a new upstream file differ: compare with {new}"))
        elif action == "local-orphan":
            conflicts.append((path, "changed locally; upstream removed it or the entry points no longer import it"))
        elif action == "unlisted":
            conflicts.append((path, "neither upstream's nor in UPSTREAM.json local_files"))
    for directory in sorted({p.parent for p in ENGINE.rglob("*") if p.is_dir()} | {ENGINE}, reverse=True):
        for child in directory.iterdir() if directory.is_dir() else ():
            if child.is_dir() and not any(child.iterdir()):
                child.rmdir()

    refreshed = refresh_config_fields()
    record["upstream_commit"] = new_commit
    record["upstream_describe"] = git(clone, "describe", "--tags", "--always", new_commit)
    record["upstream_commit_date"] = git(clone, "log", "-1", "--format=%cI", new_commit)
    RECORD.write_text(json.dumps(record, indent=2) + "\n")

    print(f"upstream {old_commit[:12]} -> {new_commit[:12]} ({record['upstream_describe']})")
    print("  " + ", ".join(f"{name} {count}" for name, count in sorted(counts.items())))
    if missing:
        print("  entry points upstream no longer has: " + " ".join(missing)
              + "  (fix ENTRYPOINTS in scripts/routing_closure.py)")
    if refreshed:
        print("  GridRouteConfig's fields changed: effective_config_fields.json and the "
              "effective-configuration schema rewritten")
    elif refreshed is None:
        print("  config field lists NOT refreshed: routing_config.py does not parse yet; "
              "run with --refresh-config-fields after resolving")
    for path, why in conflicts:
        print(f"CONFLICT {ENGINE.relative_to(ROOT) / path}: {why}")
    print(f"old tree: {old_tree}\nnew tree: {new_tree}")
    print("nothing is committed: resolve conflicts, update `local_changes` in UPSTREAM.json, rebuild, test"
          if conflicts else "no conflicts: rebuild and test before committing")
    return 1 if conflicts or missing else 0


if __name__ == "__main__":
    sys.exit(main())
