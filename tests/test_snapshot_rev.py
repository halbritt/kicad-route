"""Snapshot provenance for sibling libraries and submodules (no backend runs).

KICADROUTE-22: `--rev` takes the project, the in-repository sibling directories its
library tables reach as ${KIPRJMOD}/../<dir>, and checked-out submodules at their
pinned commits from git, never from the working tree; a sibling outside the
repository is recorded as unversioned. KICADROUTE-19: copy_project gives the work,
candidate and baseline copies the same sibling libraries as reference/<proj>.
"""
import json
import subprocess
from pathlib import Path

import pytest

from helpers import ROOT  # noqa: F401  (puts src on sys.path)
from kicad_route import cli, snapshot

GIT_ENV = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "protocol.file.allow=always",
           "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false"]


def git(repo, *args):
    return subprocess.run(["git", *GIT_ENV, "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def commit_all(repo, msg):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


def write_project(proj, sym_uris, fp_uris):
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "board.kicad_pcb").write_text("(kicad_pcb (version 1))\n")
    (proj / "board.kicad_sch").write_text("(kicad_sch (version 1))\n")
    (proj / "board.kicad_pro").write_text("{}\n")
    (proj / "sym-lib-table").write_text("(sym_lib_table\n" + "".join(
        f'  (lib (name "s{i}")(type "KiCad")(uri "{u}")(options "")(descr ""))\n' for i, u in enumerate(sym_uris)) + ")\n")
    (proj / "fp-lib-table").write_text("(fp_lib_table\n" + "".join(
        f'  (lib (name "f{i}")(type "KiCad")(uri "{u}")(options "")(descr ""))\n' for i, u in enumerate(fp_uris)) + ")\n")


def write_lib(d, tag):
    (d / "fp.pretty").mkdir(parents=True, exist_ok=True)
    (d / "lib.kicad_sym").write_text(f"(kicad_symbol_lib {tag})\n")
    (d / "fp.pretty" / "R.kicad_mod").write_text(f"(footprint R {tag})\n")


def cfg_for(proj, rev=None, library_dirs=None):
    c = {"name": proj.name, "path": proj.resolve(), "pcb": "board.kicad_pcb", "schematic": "board.kicad_sch",
         "library_dirs": library_dirs or {}}
    if rev:
        c["rev"] = rev
    return c


SIB = ["${KIPRJMOD}/../shared_lib/lib.kicad_sym"], ["${KIPRJMOD}/../shared_lib/fp.pretty"]


@pytest.fixture
def repo_with_sibling(tmp_path):
    """repo/proj (the project) + repo/shared_lib (its sibling library) + repo/unrelated:
    commit 1 (v1), commit 2 (v2), then uncommitted v3 edits to both."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    write_project(repo / "proj", *SIB)
    write_lib(repo / "shared_lib", "v1")
    (repo / "unrelated").mkdir()
    (repo / "unrelated" / "big.bin").write_text("not a library")
    c1 = commit_all(repo, "v1")
    write_lib(repo / "shared_lib", "v2")
    (repo / "proj" / "board.kicad_pcb").write_text("(kicad_pcb (version 2))\n")
    c2 = commit_all(repo, "v2")
    write_lib(repo / "shared_lib", "v3-uncommitted")
    (repo / "proj" / "board.kicad_pcb").write_text("(kicad_pcb (version 3-uncommitted))\n")
    return repo, c1, c2


def snap(cfg, run_dir):
    """cli.snapshot_input: snapshot/ renamed to reference/ as a real run does."""
    run_dir.mkdir(parents=True)
    ref, manifest = cli.snapshot_input(cfg, run_dir)
    assert json.loads((run_dir / "snapshot.json").read_text())["library_links"] == manifest["library_links"]
    return ref, manifest


@pytest.mark.parametrize("which,tag", [("c1", "v1"), ("c2", "v2")])
def test_rev_takes_sibling_from_the_revision_not_the_working_tree(repo_with_sibling, tmp_path, which, tag):
    repo, c1, c2 = repo_with_sibling
    rev = {"c1": c1, "c2": c2}[which]
    ref, m = snap(cfg_for(repo / "proj", rev=rev), tmp_path / "run")
    sib = ref.parent / "shared_lib"
    assert (sib / "lib.kicad_sym").read_text() == f"(kicad_symbol_lib {tag})\n"
    assert (sib / "fp.pretty" / "R.kicad_mod").read_text() == f"(footprint R {tag})\n"
    assert "uncommitted" not in (ref / "board.kicad_pcb").read_text()
    link = next(e for e in m["library_links"] if e["path"] == "../shared_lib")
    assert link["in_source_repo"] and link["pinned"]
    assert link["tree"] == git(repo, "rev-parse", f"{rev}:shared_lib")
    assert m["library_issues"] == []
    exp = m["revision_export"]
    assert exp["commit"] == rev and sorted(exp["archived_paths"]) == ["proj", "shared_lib"]
    assert not (tmp_path / "run" / "source" / "unrelated").exists()  # never the whole repository
    assert m["source_git_head"] == rev


def test_working_tree_snapshot_reads_the_working_tree(repo_with_sibling, tmp_path):
    repo, _, _ = repo_with_sibling
    ref, m = snap(cfg_for(repo / "proj"), tmp_path / "run")
    assert "v3-uncommitted" in (ref.parent / "shared_lib" / "lib.kicad_sym").read_text()
    link = next(e for e in m["library_links"] if e["path"] == "../shared_lib")
    assert link["in_source_repo"] and not link["pinned"]
    assert link["dirty_files"], "an edited in-repo sibling is reported as dirty"


def test_pcb_only_snapshot_keeps_local_and_revision_bound_sibling_libraries(repo_with_sibling, tmp_path):
    repo, first, _ = repo_with_sibling
    config = cfg_for(repo / 'proj', rev=first)
    config['schematic'] = None
    ref, manifest = snap(config, tmp_path / 'run')
    assert (ref.parent / 'shared_lib/fp.pretty/R.kicad_mod').read_text() == '(footprint R v1)\n'
    assert manifest['sheet_instances'] == 0
    assert manifest['schematic_files'] == []
    assert manifest['library_issues'] == []
    assert manifest['revision_export']['commit'] == first
    assert all(link['pinned'] for link in manifest['library_links'])


def test_rev_untracked_sibling_is_an_issue_not_a_working_tree_read(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    write_project(repo / "proj", *SIB)
    c1 = commit_all(repo, "project only")
    write_lib(repo / "shared_lib", "untracked")  # exists on disk, never committed
    ref, m = snap(cfg_for(repo / "proj", rev=c1), tmp_path / "run")
    assert not (ref.parent / "shared_lib").exists()
    assert m["library_links"] == []
    assert any("shared_lib is not in revision" in i for i in m["library_issues"])


def test_rev_sibling_outside_the_repository_is_recorded_unversioned(tmp_path):
    """The fome-mc shape: the project is the repository top and ../kicad-libraries is
    a separate repository beside it."""
    proj, libs = tmp_path / "board", tmp_path / "kicad-libraries"
    write_project(proj, ["${KIPRJMOD}/../kicad-libraries/lib.kicad_sym"], ["${KIPRJMOD}/../kicad-libraries/fp.pretty"])
    git(proj, "init", "-q")
    c1 = commit_all(proj, "board")
    libs.mkdir()
    git(libs, "init", "-q")
    write_lib(libs, "lib1")
    head = commit_all(libs, "libs")
    (libs / "lib.kicad_sym").write_text("(kicad_symbol_lib dirty)\n")
    ref, m = snap(cfg_for(proj, rev=c1), tmp_path / "run")
    link = next(e for e in m["library_links"] if e["path"] == "../kicad-libraries")
    assert link["in_source_repo"] is False and link["pinned"] is False
    assert link["library_head"] == head and link["library_dirty_files"]
    assert Path(link["library_repo"]).resolve() == libs.resolve()
    assert m["revision_export"]["siblings"] == {"kicad-libraries": {"in_repo": False}}
    assert any("outside the repository" in i and head in i for i in m["library_issues"])
    assert (ref.parent / "kicad-libraries" / "fp.pretty" / "R.kicad_mod").exists()


@pytest.fixture
def repo_with_submodule(tmp_path):
    """repo/proj with proj/libs a submodule pinned at commit A; the checkout is then
    moved to commit B and edited without committing."""
    lib = tmp_path / "libsrc"
    lib.mkdir()
    git(lib, "init", "-q")
    write_lib(lib, "A")
    a = commit_all(lib, "A")
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    write_project(repo / "proj", ["${KIPRJMOD}/libs/lib.kicad_sym"], ["${KIPRJMOD}/libs/fp.pretty"])
    git(repo / "proj", "submodule", "add", "-q", str(lib), "libs")
    c1 = commit_all(repo, "pin A")
    sub = repo / "proj" / "libs"
    write_lib(sub, "B")
    b = commit_all(sub, "B")
    (sub / "lib.kicad_sym").write_text("(kicad_symbol_lib B-dirty)\n")
    return repo, c1, a, b


def test_rev_snapshots_checked_out_submodule_at_its_pin(repo_with_submodule, tmp_path):
    repo, c1, a, b = repo_with_submodule
    ref, m = snap(cfg_for(repo / "proj", rev=c1, library_dirs={"libs": "/nonexistent"}), tmp_path / "run")
    assert (ref / "libs" / "lib.kicad_sym").read_text() == "(kicad_symbol_lib A)\n"
    assert (ref / "libs" / "fp.pretty" / "R.kicad_mod").read_text() == "(footprint R A)\n"
    assert m["revision_export"]["submodules"] == [
        {"path": "proj/libs", "pin": a, "exported": True, "checked_out_head": b}]
    entry = next(e for e in m["library_links"] if e["path"] == "libs")
    assert entry["submodule_pin"] == a and "pinned commit" in entry["source"]
    assert m["library_issues"] == []
    assert m["files"]["libs/lib.kicad_sym"] == snapshot.sha256(ref / "libs" / "lib.kicad_sym")


def test_rev_uninitialised_submodule_uses_library_dir_and_committed_pin(repo_with_submodule, tmp_path):
    repo, c1, a, b = repo_with_submodule
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(repo), str(clone))  # submodule not initialised
    mapped = tmp_path / "libsrc"  # the library repository, at A (its HEAD)
    ref, m = snap(cfg_for(clone / "proj", rev=c1, library_dirs={"libs": str(mapped)}), tmp_path / "run")
    sub = m["revision_export"]["submodules"]
    assert sub[0]["path"] == "proj/libs" and sub[0]["pin"] == a and sub[0]["exported"] is False
    entry = next(e for e in m["library_links"] if e["path"] == "libs")
    assert entry["submodule_pin"] == a and entry["library_head"] == a
    assert m["library_issues"] == []
    # the mapped library moves on: the committed pin catches it
    write_lib(mapped, "C")
    c = commit_all(mapped, "C")
    _, m2 = snap(cfg_for(clone / "proj", rev=c1, library_dirs={"libs": str(mapped)}), tmp_path / "run2")
    assert any(f"pins {a}" in i and c in i for i in m2["library_issues"])
    # without the mapping the empty submodule is reported
    _, m3 = snap(cfg_for(clone / "proj", rev=c1), tmp_path / "run3")
    assert any("submodule proj/libs" in i and "not checked out" in i for i in m3["library_issues"])


def test_copy_project_replicates_sibling_libraries(repo_with_sibling, tmp_path):
    """KICADROUTE-19: work/input, candidate and the baseline copy see ../shared_lib."""
    repo, c1, _ = repo_with_sibling
    run_dir = tmp_path / "run"
    ref, _ = snap(cfg_for(repo / "proj", rev=c1), run_dir)
    for dst in (run_dir / "work" / "input" / ref.name, run_dir / "candidate" / ref.name,
                tmp_path / "baseline-tmp" / ref.name):
        cli.copy_project(ref, dst)
        lib = dst / ".." / "shared_lib"
        assert (lib / "lib.kicad_sym").read_text() == "(kicad_symbol_lib v1)\n"
        assert (lib / "fp.pretty" / "R.kicad_mod").exists()
    # a --dir chain from the candidate resolves the same library (validate --against relies on it)
    cand = run_dir / "candidate" / ref.name
    _, m = snap(cfg_for(cand), tmp_path / "run2")
    assert m["library_issues"] == []
    chained = tmp_path / "run2" / "reference" / "shared_lib"
    assert (chained / "lib.kicad_sym").read_text() == "(kicad_symbol_lib v1)\n"


def test_copy_project_probe_shape(tmp_path):
    """The review probe: reference/<proj> with reference/<lib> a symlink outside the run."""
    ref_root = tmp_path / "run" / "reference"
    proj = ref_root / "my_board"
    write_project(proj, ["${KIPRJMOD}/../fome-common/syms.kicad_sym"], [])
    target = tmp_path / "external_fome_common"
    target.mkdir()
    (target / "syms.kicad_sym").write_text("(kicad_symbol_lib)")
    (ref_root / "fome-common").symlink_to(target)
    (ref_root / "unreferenced").mkdir()
    cand = tmp_path / "run" / "candidate" / "my_board"
    cli.copy_project(proj, cand)
    assert (cand.parent / "fome-common").resolve() == target.resolve()
    assert (cand.parent / "fome-common" / "syms.kicad_sym").exists()
    assert not (cand.parent / "unreferenced").exists()  # only what the tables reference
