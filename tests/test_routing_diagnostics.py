"""Nested routing diagnostics through the production event and writer interfaces."""
import ast
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ENGINE = Path(__file__).resolve().parents[1] / 'src/kicad_route/router/py_router'
sys.path.insert(0, str(ENGINE))
from net_story import dump_net_story, story_path
from routing_run_history import diagnostic_run, bind_state
from routing_state import record_net_event


def state(name='GND', nid=3):
    return NS(pcb_data=NS(nets={nid: NS(name=name)}), net_history={}, routed_results={}, route_index=7)


def event(s, outcome, cpu=1.25):
    nid = next(iter(s.pcb_data.nets))
    record_net_event(s, nid, 'phase3_cascade', {'outcome': outcome, 'transaction_cpu_seconds': cpu})


def read(board):
    return json.loads(Path(story_path(str(board))).read_text())


def events(story):
    return sorted((e for n in story['nets'].values() for e in n['events']), key=lambda e: e['order'])


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setenv('KICAD_NET_STORY', '1')


def test_nested_skip_routing_empty_outer_does_not_overwrite_child(tmp_path):
    board = str(tmp_path / 'nested.kicad_pcb')

    @diagnostic_run
    def child(input_file, output_file):
        s = state()
        bind_state(s)
        event(s, 'accepted', .582422)
        dump_net_story(s, output_file or input_file)

    @diagnostic_run
    def outer(input_file, output_file):
        s = state()
        bind_state(s)
        child(output_file, '')  # repair_planes guaranteed-join call convention
        dump_net_story(s, output_file)
        assert not Path(story_path(output_file)).exists()  # one outer publication

    outer(board, board)
    story = read(board)
    assert story['board'] == board
    assert events(story)[0]['details'] == {'outcome': 'accepted', 'transaction_cpu_seconds': .582422}
    assert len(events(story)) == 1
    assert [(c['call_id'], c['parent_call_id'], c['children'], c['event_count'])
            for c in story['calls']] == [(0, None, [1], 0), (1, 0, [], 1)]
    assert story['diagnostics_complete'] is True


def test_multiple_children_and_nonempty_parent_keep_events_once_and_results_separate(tmp_path):
    board = str(tmp_path / 'multiple.kicad_pcb')

    @diagnostic_run
    def child(input_file, output_file, outcome):
        s = state(nid=99)  # parsed IDs can differ in a child board
        bind_state(s)
        event(s, outcome)
        s.routed_results[99] = {'new_segments': [], 'new_vias': [], 'iterations': 5}
        dump_net_story(s, input_file)
        dump_net_story(s, input_file)  # repeated dump requests do not duplicate events

    @diagnostic_run
    def outer(input_file, output_file):
        s = state()
        bind_state(s)
        event(s, 'outer-before')
        child(output_file, '', 'rejected')
        child(output_file, '', 'accepted')
        event(s, 'outer-after')
        s.routed_results[3] = {'new_segments': [], 'new_vias': [], 'iterations': 17}
        dump_net_story(s, output_file)

    outer(board, board)
    story = read(board)
    assert [e['details']['outcome'] for e in events(story)] == ['outer-before', 'rejected', 'accepted', 'outer-after']
    assert [e['call_id'] for e in events(story)] == [0, 1, 2, 0]
    assert sum(e['details']['transaction_cpu_seconds'] for e in events(story)) == 5
    assert [c['event_count'] for c in story['calls']] == [2, 1, 1]
    assert story['nets']['GND']['result']['iterations'] == 17
    assert story['calls'][1]['nets']['GND']['result']['iterations'] == 5
    assert all('events' not in n for c in story['calls'] for n in c['nets'].values())
    assert story['nets']['GND']['result_call_id'] == 0


def test_geometry_history_restore_and_mutated_details_do_not_erase_attempts(tmp_path):
    board = str(tmp_path / 'rollback.kicad_pcb')

    @diagnostic_run
    def route(input_file, output_file):
        s = state()
        bind_state(s)
        details = {'outcome': 'attempt', 'pads': ['original']}
        record_net_event(s, 3, 'proposal', details)
        s.net_history.clear()  # the relevant in-place action of geometry undo
        details['pads'].append('later')
        event(s, 'rejected')
        dump_net_story(s, output_file)

    route(board, board)
    story = read(board)
    assert len(events(story)) == 2
    assert events(story)[0]['details']['pads'] == ['original']
    assert events(story)[1]['details']['outcome'] == 'rejected'


@pytest.mark.parametrize('failure', [RuntimeError, KeyboardInterrupt])
def test_exceptions_keep_partial_child_history_and_release_owner(tmp_path, failure):
    board = str(tmp_path / 'exception.kicad_pcb')

    @diagnostic_run
    def child(input_file, output_file):
        s = state()
        bind_state(s)
        event(s, 'attempted')
        raise failure('after event')

    @diagnostic_run
    def outer(input_file, output_file):
        bind_state(state())
        child(output_file, '')

    with pytest.raises(failure, match='after event'):
        outer(board, board)
    story = read(board)
    assert len(events(story)) == 1
    assert [c['completion'] for c in story['calls']] == ['raised', 'raised']
    assert [c['exception'] for c in story['calls']] == [failure.__name__] * 2
    assert story['diagnostics_complete']

    @diagnostic_run
    def subsequent(input_file, output_file):
        return 'early return, no state'

    assert subsequent(board, board) == 'early return, no state'
    fresh = read(board)
    assert fresh['nets'] == {}
    assert len(fresh['calls']) == 1 and fresh['calls'][0]['call_id'] == 0
    assert not fresh['calls'][0]['state_available']


def test_caught_child_error_and_normal_cancellation_keep_parent_and_grandchild(tmp_path):
    board = str(tmp_path / 'cancel.kicad_pcb')

    @diagnostic_run
    def route(input_file, output_file, depth=0):
        s = state()
        bind_state(s)
        if depth == 2:
            event(s, 'cancelled')
            return 0  # normal budget-stop return, not an exception
        if depth == 1:
            route(input_file, '', depth=2)
            raise ValueError('child only')
        try:
            route(input_file, '', depth=1)
        except ValueError:
            event(s, 'parent-continues')
        dump_net_story(s, output_file)

    route(board, board)
    story = read(board)
    assert [c['parent_call_id'] for c in story['calls']] == [None, 0, 1]
    assert [c['completion'] for c in story['calls']] == ['returned', 'raised', 'returned']
    assert [e['details']['outcome'] for e in events(story)] == ['cancelled', 'parent-continues']


def test_route_and_main_copies_share_owner(tmp_path):
    board = str(tmp_path / 'copies.kicad_pcb')
    # Execute the same routing entry in two module namespaces, as CLI startup
    # and repair_planes' `from route import batch_route` do. The owner module
    # must be canonical, not a variable local to either routing module.
    source = '''
from routing_run_history import diagnostic_run, bind_state
from routing_state import record_net_event
from net_story import dump_net_story
@diagnostic_run
def batch_route(input_file, output_file, child=None):
    s = make_state()
    bind_state(s)
    if child:
        child(output_file, '')
    record_net_event(s, 3, __name__, {'outcome': 'accepted'})
    dump_net_story(s, output_file or input_file)
'''
    modules = [types.ModuleType(n) for n in ('__main__', 'route')]
    for module in modules:
        module.make_state = state
        exec(compile(source, str(ENGINE / 'route.py'), 'exec'), module.__dict__)
    modules[0].batch_route(board, board, modules[1].batch_route)
    story = read(board)
    assert [e['event'] for e in events(story)] == ['route', '__main__']
    assert len(story['calls']) == 2
    # Also require the real entry point to use this wrapper and bind its state.
    tree = ast.parse((ENGINE / 'route.py').read_text())
    batch = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'batch_route')
    assert any(isinstance(d, ast.Name) and d.id == 'diagnostic_run' for d in batch.decorator_list)
    assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'bind_state'
               for n in ast.walk(batch))


def test_disabled_run_keeps_legacy_writer_and_no_call_inventory(tmp_path, monkeypatch):
    monkeypatch.delenv('KICAD_NET_STORY')
    board = str(tmp_path / 'standalone.kicad_pcb')

    @diagnostic_run
    def route(input_file, output_file):
        s = state()
        bind_state(s)
        event(s, 'accepted')
        return dump_net_story(s, output_file)

    assert route(board, board) == story_path(board)
    story = read(board)
    assert set(story) == {'board', 'nets', 'bus_groups', 'phase3_order'}
    assert story['nets']['GND']['events'][0]['details']['outcome'] == 'accepted'


def test_child_snapshots_detach_and_release_geometry(tmp_path):
    import gc
    import weakref
    board = str(tmp_path / 'release.kicad_pcb')
    references = []
    class State:
        pass

    @diagnostic_run
    def child(input_file, output_file):
        s = State()
        s.__dict__.update(vars(state()))
        references.append(weakref.ref(s))
        bind_state(s)
        event(s, 'accepted')
        dump_net_story(s, input_file)

    @diagnostic_run
    def outer(input_file, output_file):
        bind_state(state())
        child(output_file, '')
        gc.collect()
        assert references[0]() is None  # no retained boards per completed child

    outer(board, board)
    assert len(events(read(board))) == 1


def test_writer_failure_does_not_leave_previous_run_or_mask_route_error(tmp_path, monkeypatch, capsys):
    import route_summary
    board = str(tmp_path / 'failed-write.kicad_pcb')
    Path(story_path(board)).write_text('{"old": true}')

    @diagnostic_run
    def route(input_file, output_file):
        bind_state(state())
        raise ValueError('original routing error')

    def fail_replace(*args):
        raise OSError('disk error')

    with monkeypatch.context() as m:
        m.setattr(route_summary.os, 'replace', fail_replace)
        with pytest.raises(ValueError, match='original routing error'):
            route(board, board)
    assert not Path(story_path(board)).exists()
    assert 'net story dump failed' in capsys.readouterr().out


def test_snapshot_failure_retains_journal_and_reports_incomplete(tmp_path, monkeypatch):
    import net_story
    board = str(tmp_path / 'bad-snapshot.kicad_pcb')

    @diagnostic_run
    def route(input_file, output_file):
        s = state()
        bind_state(s)
        event(s, 'accepted')
        return 42

    def fail_snapshot(*args):
        raise ValueError('bad result metadata')

    with monkeypatch.context() as m:
        m.setattr(net_story, 'build_net_story', fail_snapshot)
        assert route(board, board) == 42
    story = read(board)
    assert len(events(story)) == 1
    assert story['diagnostics_complete'] is False
    assert story['diagnostic_errors'] == [{'call_id': 0, 'error': 'bad result metadata'}]
