"""Isolated snapshot of a KiCad project: the board, project and rule files, every
schematic sheet reachable from the root, library tables and the local libraries
they point at. The source tree is never modified; `--rev` snapshots a committed
revision with `git archive` instead of the working tree.
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .common import git, sha256, write_json


def expand(path):
    return Path(os.path.expanduser(str(path)))


KICAD_SUFFIXES = {".kicad_sch", ".kicad_pcb", ".kicad_pro", ".kicad_dru", ".kicad_wks",
                  ".kicad_sym", ".lib", ".dcm", ".mod"}
LIB_DIR_SUFFIXES = {".pretty", ".3dshapes"}
URI_RE = re.compile(r'\(uri\s+"?([^")]+?)"?\)')
SHEETFILE_RE = re.compile(r'\(property\s+"Sheet ?file"\s+"([^"]+)"')


def resolve_sheet(project_dir, name, by=None):
    """Exact sheet file, or a unique case-insensitive match (what KiCad sees on
    the case-insensitive filesystems it is often authored on)."""
    candidates = [Path(name)]
    if by and Path(by).parent != Path("."):
        candidates.insert(0, Path(by).parent / name)
    for cand in candidates:
        if (project_dir / cand).exists():
            return str(cand), None
        parent = (project_dir / cand).parent
        if parent.is_dir():
            hits = [p.name for p in parent.iterdir() if p.name.lower() == cand.name.lower()]
            if len(hits) == 1:
                rel_hit = str(cand.parent / hits[0]) if cand.parent != Path(".") else hits[0]
                return rel_hit, hits[0]
    return None, None


def sheet_files(project_dir, root):
    """Schematic files reachable from the root, the sheet-instance count, and
    references whose exact filename does not exist."""
    files, instances, problems = [], 1, []
    seen = {}

    def walk(path, multiplicity):
        nonlocal instances
        if path not in seen:
            files.append(path)
            seen[path] = SHEETFILE_RE.findall((project_dir / path).read_text(errors="replace"))
        for child in seen[path]:
            instances += multiplicity
            actual, case_match = resolve_sheet(project_dir, child, by=path)
            if actual is None:
                problems.append({"referenced": child, "by": path, "resolved": None})
                continue
            if case_match:
                problems.append({"referenced": child, "by": path, "resolved": actual})
            walk(actual, multiplicity)

    walk(root, 1)
    return files, instances, problems


def sibling_names(table_texts, project_name):
    """Directories that library tables reach as ${KIPRJMOD}/../<dir>/... (one level up)."""
    names = set()
    for text in table_texts:
        for uri in URI_RE.findall(text):
            if uri.startswith("${KIPRJMOD}/"):
                rel = Path(uri[len("${KIPRJMOD}/"):]).parts
                if len(rel) >= 2 and rel[0] == ".." and rel[1] not in ("..", ".", project_name):
                    names.add(rel[1])
    return sorted(names)


def _untar(archive, dest, strip=0):
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.run(["tar", "-x", "-C", str(dest), *(["--strip-components", str(strip)] if strip else [])],
                   input=archive, check=True)


def _export_submodules(repo, commit, pathspecs, place, records, rel=""):
    """git archive leaves submodules empty: fill each one inside pathspecs from its
    own object store at the commit the superproject pins (never its working tree)."""
    listing = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", "-z", commit, "--", *pathspecs],
                             capture_output=True, text=True).stdout
    for item in filter(None, listing.split("\0")):
        meta, path = item.split("\t", 1)
        mode, kind, pin = meta.split()
        if kind != "commit":
            continue
        sub, full = repo / path, f"{rel}{path}"
        rec = {"path": full, "pin": pin}
        top = git(sub, "rev-parse", "--show-toplevel")
        if not top or Path(top).resolve() != sub.resolve():
            rec.update(exported=False, reason="submodule not checked out (map it with --library-dir NAME=PATH)")
        elif git(sub, "cat-file", "-e", f"{pin}^{{commit}}") is None:
            rec.update(exported=False, reason="the pinned commit is not in the checked-out submodule; fetch it")
        else:
            _untar(subprocess.run(["git", "-C", str(sub), "archive", "--format=tar", pin],
                                  capture_output=True, check=True).stdout, place(full))
            rec.update(exported=True, checked_out_head=git(sub, "rev-parse", "HEAD"))
            records.append(rec)
            _export_submodules(sub, pin, ["."], place, records, f"{full}/")
            continue
        records.append(rec)


def export_revision(cfg, run_dir):
    """Materialise the project as committed at cfg['rev'] under run_dir/source: the
    project directory, the sibling directories its committed library tables reach as
    ${KIPRJMOD}/../<dir> when they are in the same repository (only those, not the
    whole repository), and the content of submodules inside them at their pinned
    commits. Nothing comes from the working tree. A sibling outside the repository is
    not versioned by it; the record says so and make_snapshot reads it as it is.
    Returns (project dir, commit, record)."""
    src = cfg["path"]
    top = Path(git(src, "rev-parse", "--show-toplevel"))
    prefix = src.relative_to(top)
    commit = git(top, "rev-parse", "--verify", f"{cfg['rev']}^{{commit}}")
    if not commit:
        sys.exit(f"{cfg['rev']!r} is not a commit in {top}")
    if prefix.parts and git(top, "cat-file", "-t", f"{commit}:{prefix.as_posix()}") != "tree":
        sys.exit(f"{prefix} is not a directory at {cfg['rev']} ({commit})")
    base = run_dir / "source"
    out = base / src.name
    tables = [git(top, "show", f"{commit}:{(prefix / t).as_posix()}") or "" for t in ("sym-lib-table", "fp-lib-table")]
    siblings, paths = {}, [prefix.as_posix()]
    for name in sibling_names(tables, src.name):
        if not prefix.parts:  # the project is the repository's top: ../<name> is outside it
            siblings[name] = {"in_repo": False}
            continue
        rel = (prefix.parent / name).as_posix()
        kind = git(top, "cat-file", "-t", f"{commit}:{rel}")
        if kind in ("tree", "commit"):  # a directory or a submodule at that revision
            siblings[name] = {"in_repo": True, "exported": True, "repo_path": rel,
                              "tree": git(top, "rev-parse", f"{commit}:{rel}")}
            paths.append(rel)
            continue
        wt = top / rel
        outside = wt.exists() and not Path(os.path.realpath(wt)).is_relative_to(top.resolve())
        siblings[name] = ({"in_repo": False, "via_symlink": True} if outside else
                          {"in_repo": True, "exported": False, "repo_path": rel})
    archive = subprocess.run(["git", "-C", str(top), "archive", "--format=tar", commit, "--", *paths],
                             capture_output=True, check=True).stdout
    if prefix.parts:
        _untar(archive, base, strip=len(prefix.parts) - 1)  # base/<project>, base/<sibling>

        def place(p):
            return base / Path(p).relative_to(prefix.parent)
    else:
        _untar(archive, out)

        def place(p):
            return out / p
    submodules = []
    _export_submodules(top, commit, paths, place, submodules)
    return out, commit, {"commit": commit, "repository": str(top), "archived_paths": paths,
                         "siblings": siblings, "submodules": submodules}


def sibling_provenance(target, source_top):
    """How a ${KIPRJMOD}/../<dir> library read from disk is versioned."""
    real = Path(os.path.realpath(target))
    if source_top and real.is_relative_to(Path(source_top).resolve()):
        rel = real.relative_to(Path(source_top).resolve())
        dirty = git(source_top, "--no-optional-locks", "status", "--porcelain", "--", str(rel))
        return {"in_source_repo": True, "pinned": False, "source": "working tree",
                "dirty_files": dirty.splitlines() if dirty else []}
    rec = {"in_source_repo": False, "pinned": False,
           "note": "outside the project's repository: the project's commit does not pin it"}
    lib_top = git(real, "rev-parse", "--show-toplevel")
    if lib_top:
        dirty = git(real, "--no-optional-locks", "status", "--porcelain", "--", ".")
        rec.update(library_repo=lib_top, library_head=git(real, "rev-parse", "HEAD"),
                   library_dirty_files=dirty.splitlines() if dirty else [])
    else:
        rec["library_head"] = None
    return rec


def make_snapshot(cfg, run_dir):
    """Copy the KiCad-relevant project files; never touch the source tree."""
    src = cfg["path"]
    origin_parent = src.parent       # where ${KIPRJMOD}/../<dir> lives in the working tree
    source_top = git(src, "rev-parse", "--show-toplevel")
    rev_commit, export = None, None
    if cfg.get("rev"):
        src, rev_commit, export = export_revision(cfg, run_dir)
    snap_root = run_dir / "snapshot"
    dst = snap_root / src.name
    dst.mkdir(parents=True)
    issues = []

    def copy(relpath):
        s, d = src / relpath, dst / relpath
        if d.exists() or d.is_symlink():
            return
        d.parent.mkdir(parents=True, exist_ok=True)
        if s.is_dir():
            shutil.copytree(s, d, symlinks=True)
        elif s.exists():
            shutil.copy2(s, d)

    for entry in sorted(src.iterdir()):
        if entry.is_file() and (entry.suffix in KICAD_SUFFIXES or entry.name in ("sym-lib-table", "fp-lib-table")):
            copy(entry.name)
        elif entry.is_dir() and entry.suffix in LIB_DIR_SUFFIXES:
            copy(entry.name)
    sch_files, sheet_instances, sheet_problems = sheet_files(src, cfg["schematic"])
    for f in sch_files:
        copy(f)
    case_aliases = []
    for prob in sheet_problems:
        if prob["resolved"] is None:
            issues.append(f"sheet file {prob['referenced']!r} (referenced by {prob['by']}) does not exist")
            continue
        alias = dst / prob["referenced"]
        if not alias.exists():
            alias.parent.mkdir(parents=True, exist_ok=True)
            alias.symlink_to(Path(prob["resolved"]).name)
        case_aliases.append(prob)
        issues.append(f"sheet file {prob['referenced']!r} (referenced by {prob['by']}) exists only as "
                      f"{prob['resolved']!r}; a case-sensitive filesystem cannot open it. The snapshot "
                      "adds a case alias so the checks cover that sheet; the source repo is unchanged.")

    # Library directories mapped to the shared FOME library repository.
    library_links = []
    subs = {r["path"]: r for r in (export or {}).get("submodules", [])}
    for dirname, lib in cfg.get("library_dirs", {}).items():
        target = expand(lib)
        here = src / dirname
        pinned = None  # from the committed tree under --rev, else from HEAD
        tree = git(cfg["path"], "ls-tree", rev_commit or "HEAD", dirname)
        if tree and " commit " in tree:
            pinned = tree.split()[2]
        # A link to the mapped library is the mapping itself (a candidate of a run that
        # had it): link it again rather than copy the library into the snapshot.
        inherited = here.is_symlink() and here.resolve() == target.resolve()
        if here.is_dir() and any(here.iterdir()) and not inherited:
            copy(dirname)
            if pinned:  # a checked-out submodule: under --rev its content at the pin
                sub = subs.get(Path(os.path.relpath(cfg["path"] / dirname, export["repository"])).as_posix()) if export else None
                head = sub["checked_out_head"] if sub else git(here, "rev-parse", "HEAD")
                library_links.append({"path": dirname, "target": None, "submodule_pin": pinned,
                                      "source": f"submodule content at its pinned commit {pinned}" if sub
                                      else "submodule working tree", "library_head": head})
                if not sub and head and head != pinned:
                    issues.append(f"{dirname}: submodule pins {pinned} but its checkout is at {head}")
            continue
        link = dst / dirname
        if link.exists():
            shutil.rmtree(link)
        link.symlink_to(target)
        actual = git(target, "rev-parse", "HEAD")
        entry = {"path": dirname, "target": str(target), "submodule_pin": pinned, "library_head": actual}
        if pinned and actual and pinned != actual:
            issues.append(f"{dirname}: submodule pins {pinned} but {target} is at {actual}")
        library_links.append(entry)

    mapped = {Path(os.path.relpath(cfg["path"] / d, export["repository"])).as_posix()
              for d in cfg.get("library_dirs", {})} if export else set()
    for r in subs.values():
        if not r["exported"] and r["path"] not in mapped:
            issues.append(f"submodule {r['path']} (pinned {r['pin']}) is empty in the snapshot: {r['reason']}")

    # Resolve ${KIPRJMOD} references from the library tables.
    for table in ("sym-lib-table", "fp-lib-table"):
        tpath = src / table
        if not tpath.exists():
            continue
        for uri in URI_RE.findall(tpath.read_text()):
            if not uri.startswith("${KIPRJMOD}/"):
                if "${KIPRJMOD}" not in uri and not uri.startswith("${KICAD"):
                    if not expand(uri).exists():
                        issues.append(f"{table}: {uri} does not exist")
                continue
            relpath = Path(uri[len("${KIPRJMOD}/"):])
            original = Path(os.path.normpath(src / relpath))
            if not original.is_relative_to(src):
                original = Path(os.path.normpath(origin_parent / src.name / relpath))
            if original.is_relative_to(src):
                inner = original.relative_to(src)
                if not (src / inner).exists() and not (dst / inner).exists():
                    issues.append(f"{table}: {uri} does not exist")
                elif inner.parts and not (dst / inner.parts[0]).exists():
                    copy(inner.parts[0])
                continue
            up = 0
            for part in relpath.parts:
                if part != "..":
                    break
                up += 1
            if up != 1 or len(relpath.parts) < 2:
                issues.append(f"{table}: {uri} leaves the project more than one level; unsupported")
                continue
            first = relpath.parts[1]
            link, target = snap_root / first, origin_parent / first
            sib = (export or {}).get("siblings", {}).get(first)
            if sib and sib["in_repo"]:
                if not sib["exported"]:  # never fall back to the working tree under --rev
                    issues.append(f"{table}: {uri}: {sib['repo_path']} is not in revision {rev_commit}")
                    continue
                target = src.parent / first
                prov = {"in_source_repo": True, "pinned": True, "source": f"git revision {rev_commit}",
                        "repo_path": sib["repo_path"], "tree": sib["tree"]}
            else:
                prov = None
            if not target.exists():
                issues.append(f"{table}: {uri} does not exist")
                continue
            if not link.exists() and not link.is_symlink():
                link.symlink_to(target)
                prov = prov or sibling_provenance(target, source_top)
                library_links.append({"path": f"../{first}", "target": str(target), **prov})
                if rev_commit and not prov["in_source_repo"]:
                    issues.append(f"../{first} is outside the repository, so --rev {cfg['rev']} does not pin "
                                  f"it: read as it is now (git HEAD {prov.get('library_head')}, "
                                  f"{len(prov.get('library_dirty_files') or [])} uncommitted change(s))")
            if not (target / Path(*relpath.parts[2:])).exists():
                issues.append(f"{table}: {uri} does not exist")

    files = {}
    for root, dirs, names in os.walk(dst):  # does not descend into symlinked dirs
        for n in names:
            p = Path(root) / n
            if not p.is_symlink():
                files[str(p.relative_to(dst))] = sha256(p)
    files = dict(sorted(files.items()))
    if rev_commit:
        head, dirty = rev_commit, None
    else:
        head = git(src, "rev-parse", "HEAD")
        dirty = git(src, "status", "--porcelain", "--", *[f for f in files if (src / f).exists()]) if head else None
    manifest = {
        "project": cfg["name"],
        "source_dir": str(cfg["path"]),
        "source": f"git revision {cfg['rev']} ({rev_commit})" if rev_commit else "working tree",
        "source_git_head": head,
        "source_dirty_files": dirty.splitlines() if dirty else [],
        "schematic": cfg["schematic"],
        "pcb": cfg.get("pcb"),
        "schematic_files": sch_files,
        "sheet_instances": sheet_instances,
        "sheet_case_aliases": case_aliases,
        "missing_sheets": [p for p in sheet_problems if p["resolved"] is None],
        "library_links": library_links,
        "revision_export": export,
        "library_issues": issues,
        "files": files,
    }
    write_json(run_dir / "snapshot.json", manifest)
    return dst, manifest
