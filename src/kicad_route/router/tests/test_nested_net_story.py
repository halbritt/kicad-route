#!/usr/bin/env python3
"""Real batch_route calls in separate module copies retain every child's history."""
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile

TESTS = Path(__file__).resolve().parent
sys.path[:0] = [str(TESTS), str(TESTS.parent / 'py_router')]
from regression_utils import tool, evidence
from synth import RoutingBoard
from route import batch_route as child_route


def main():
    os.environ['KICAD_NET_STORY'] = '1'
    # CLI route.py and repair_planes' imported route.py have separate globals.
    # Load a second real copy without executing its command-line parser.
    outer_route = runpy.run_path(tool('route.py'), run_name='route_cli_copy')['batch_route']
    with tempfile.TemporaryDirectory() as tmp:
        board = RoutingBoard(w=30, h=20)
        for ref, x in [('A', 5), ('B', 15), ('C', 25)]:
            board.pad(ref, x, 10, 'N0')
        inp = Path(tmp) / 'input.kicad_pcb'
        out = Path(tmp) / 'output.kicad_pcb'
        inp.write_text(board.text())
        options = dict(grid_step=.1, final_reconcile=False, return_results=True)
        children = []

        def progress(*args):
            if children:
                return
            children.append('started')
            for _ in range(2):
                result = child_route(str(inp), '', ['N0'], **options)
                assert len(result) == 4
            # Children used an empty output, just as plane repair does. They
            # must not publish either their input-side story or the outer file.
            assert not (Path(tmp) / 'input_netstory.json').exists()
            assert not (Path(tmp) / 'output_netstory.json').exists()

        outer_route(str(inp), str(out), ['N0'], progress_callback=progress, **options)
        path = Path(tmp) / 'output_netstory.json'
        story = json.loads(path.read_text())
        calls = story['calls']
        assert children and len(calls) == 3
        assert [c['parent_call_id'] for c in calls] == [None, 0, 0]
        assert calls[0]['children'] == [1, 2]
        assert all(c['completion'] == 'returned' and c['event_count'] > 0 for c in calls)
        assert story['diagnostics_complete']
        events = [e for net in story['nets'].values() for e in net['events']]
        assert len(events) == sum(c['event_count'] for c in calls)
        assert sorted(e['order'] for e in events) == list(range(len(events)))
        assert {e['call_id'] for e in events} == {0, 1, 2}
        assert story['nets']['N0']['result_call_id'] == 0
        evidence(path, 'aggregate of real parent and two children')
        print(f"PASS: {len(calls)} calls, {len(events)} events retained exactly once")


if __name__ == '__main__':
    main()
