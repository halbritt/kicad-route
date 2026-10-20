"""Publication refusal, replacement and self-contained receipt verification."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_route import cli, common, contract, publication


def test_published_receipts_resolve_without_source_and_refresh_drops_stale(tmp_path):
    run = tmp_path / 'run'
    (run / 'validate').mkdir(parents=True)
    receipt = run / 'validate' / 'candidate-drc.json'
    receipt.write_text('{}')
    original = {'evidence': {'records': {'validate/candidate-drc.json': common.sha256(receipt)},
                             'candidate_board_sha256': 'external-board'}}
    (run / 'result.json').write_text(json.dumps(original))
    before = (run / 'result.json').read_bytes()
    dest = tmp_path / 'published'
    cli.publish_run(run, dest)
    manifest = publication.verify(dest)
    assert manifest['evidence']['result.json']['records']['validate/candidate-drc.json']['path'] == 'candidate-drc.json'
    assert manifest['evidence']['result.json']['external']['candidate_board_sha256'] == 'external-board'
    assert (run / 'result.json').read_bytes() == before
    receipt.unlink()
    (run / 'result.json').write_text('{}')
    cli.publish_run(run, dest)
    assert not (dest / 'candidate-drc.json').exists()
    publication.verify(dest)


@pytest.mark.parametrize('alias', [False, True])
def test_unowned_destination_is_refused_before_smoke(tmp_path, monkeypatch, alias):
    dest = tmp_path / 'mine'
    dest.mkdir()
    sentinel = dest / 'important.txt'
    sentinel.write_bytes(b'owner data')
    if alias:
        link = tmp_path / 'alias'
        link.symlink_to(dest)
        dest = link
    monkeypatch.setattr(cli.subprocess, 'run', lambda *a, **kw: pytest.fail('pytest must not run'))
    with pytest.raises(contract.Refusal):
        cli.smoke(dest)
    assert sentinel.read_bytes() == b'owner data'


@pytest.mark.parametrize('which', ['root', 'home', 'checkout', 'out', 'out-parent', 'out-child', 'fixture'])
def test_protected_destinations_are_refused(tmp_path, monkeypatch, which):
    out = tmp_path / 'active' / 'out'
    fixture = tmp_path / 'project'
    fixture.mkdir()
    (fixture / 'input.kicad_pcb').write_text('board')
    monkeypatch.setattr(common, 'out_dir', lambda: out)
    monkeypatch.setattr(cli, 'FIXTURE', fixture)
    monkeypatch.setattr(cli.subprocess, 'run', lambda *a, **kw: pytest.fail('pytest must not run'))
    paths = {'root': Path('/'), 'home': Path.home(), 'checkout': common.TOOL_ROOT,
             'out': out, 'out-parent': out.parent, 'out-child': out / 'child', 'fixture': fixture}
    with pytest.raises(contract.Refusal):
        cli.smoke(paths[which])
    assert (fixture / 'input.kicad_pcb').read_text() == 'board'


def test_destination_symlinks_never_write_outside_bundle(tmp_path):
    outside = tmp_path / 'outside.json'
    outside.write_text('private')
    run = tmp_path / 'run'
    run.mkdir()
    (run / 'result.json').write_text('{}')
    dest = tmp_path / 'dest'
    dest.mkdir()
    (dest / 'result.json').symlink_to(outside)
    with pytest.raises(contract.Refusal):
        cli.publish_run(run, dest)
    assert outside.read_text() == 'private'


def test_interrupted_refresh_preserves_previous_success(tmp_path):
    dest = tmp_path / 'dest'
    with publication.staging(dest, 'run') as stage:
        (stage / 'result.json').write_text('old')
        publication.seal(stage, 'run')
    with pytest.raises(RuntimeError):
        with publication.staging(dest, 'run') as stage:
            (stage / 'result.json').write_text('new')
            raise RuntimeError('interrupted before activation')
    assert (dest / 'result.json').read_text() == 'old'
    publication.verify(dest)
    with publication.staging(dest, 'run') as stage:
        (stage / 'result.json').write_text('new')
        publication.seal(stage, 'run')
    assert (dest / 'result.json').read_text() == 'new'


def test_failed_smoke_preserves_successful_publication(tmp_path, monkeypatch):
    out = tmp_path / 'out'
    monkeypatch.setattr(common, 'out_dir', lambda: out)
    monkeypatch.setattr(cli, 'out_path', lambda *parts: out.joinpath(*parts))
    monkeypatch.setattr(cli, 'krt_version', lambda: {})
    monkeypatch.setattr(cli, 'freerouting_version', lambda: {})
    monkeypatch.setattr(common, 'tool_versions', lambda: {})
    monkeypatch.setattr(cli.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=1))
    dest = tmp_path / 'dest'
    with publication.staging(dest, 'smoke') as stage:
        (stage / 'summary.json').write_text('successful prior run')
        publication.seal(stage, 'smoke')
    assert cli.smoke(dest) == 1
    assert (dest / 'summary.json').read_text() == 'successful prior run'


def test_bundle_manifest_does_not_overwrite_same_named_source_receipt(tmp_path):
    run = tmp_path / 'run'
    run.mkdir()
    (run / 'publication.json').write_text('{"source_receipt": true}')
    (run / 'result.json').write_text('{}')
    dest = tmp_path / 'dest'
    cli.publish_run(run, dest)
    manifest = publication.verify(dest)
    assert (dest / manifest['locators']['publication.json']).read_bytes() == (run / 'publication.json').read_bytes()


def test_source_directory_aliases_cannot_publish_external_receipts(tmp_path):
    run = tmp_path / 'run'
    run.mkdir()
    outside = tmp_path / 'outside'
    (outside / '20261020').mkdir(parents=True)
    (outside / '20261020' / 'secret.json').write_text('private')
    (run / 'revalidate').symlink_to(outside)
    (run / 'result.json').write_text('{}')
    dest = tmp_path / 'dest'
    cli.publish_run(run, dest)
    assert not list(dest.rglob('secret.json'))


def test_smoke_workspaces_preserve_unrelated_previous_files(tmp_path, monkeypatch):
    out = tmp_path / 'out'
    root = out / 'smoke'
    root.mkdir(parents=True)
    sentinel = root / 'unrelated.json'
    sentinel.write_text('owner data')
    monkeypatch.setattr(common, 'out_dir', lambda: out)
    monkeypatch.setattr(cli, 'out_path', lambda *parts: out.joinpath(*parts))
    monkeypatch.setattr(cli, 'krt_version', lambda: {})
    monkeypatch.setattr(cli, 'freerouting_version', lambda: {})
    monkeypatch.setattr(common, 'tool_versions', lambda: {})
    monkeypatch.setattr(cli.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=1))
    assert cli.smoke() == 1
    assert sentinel.read_text() == 'owner data'
    assert len(list(root.glob('*/summary.json'))) == 1


@pytest.mark.parametrize("manifest", [["unowned"], "unowned", 1, True, None, [], 0, False, "", {"files": []}])
def test_nonobject_or_invalid_ownership_manifest_refuses_before_smoke(tmp_path, monkeypatch, manifest):
    dest = tmp_path / "dest"
    dest.mkdir()
    sentinel = dest / "owner-data.txt"
    sentinel.write_bytes(b"unrelated owner data")
    (dest / publication.MANIFEST).write_text(json.dumps(manifest))
    monkeypatch.setattr(cli.subprocess, "run", lambda *args, **kwargs: pytest.fail("pytest must not run"))
    with pytest.raises(contract.Refusal) as refusal:
        cli.smoke(dest)
    assert refusal.value.code == 4
    assert sentinel.read_bytes() == b"unrelated owner data"
