#!/usr/bin/env python3
"""Internal native exact removal on an owned disposable board; never routes/refills."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from kicad_route import common, copper_ownership as custody
from facts import main as facts_main, sexpr


def nontrack_tree(board):
    tree = sexpr(board.read_text())
    if len(tree) != 1 or not tree[0] or tree[0][0] != 'kicad_pcb':
        raise custody.OwnershipError('invalid canonical board tree')
    children = tree[0][1:]
    # Preserve nested sequence ordering (pad IDs, polygon vertices, etc.). Only
    # top-level object order is immaterial when pcbnew saves the owned copy.
    return sorted(json.dumps(c, separators=(',', ':')) for c in children
                  if not (isinstance(c, list) and c and c[0] in ('segment', 'arc', 'via')))


def copper_trees(board):
    out = {}
    for node in sexpr(board.read_text())[0][1:]:
        if isinstance(node, list) and node and node[0] in ('segment', 'arc', 'via'):
            u = next((c[1] for c in node if isinstance(c, list) and c and c[0] == 'uuid'), None)
            if not u or u in out:
                raise custody.OwnershipError('unknown or duplicate structural copper identity')
            out[u] = node
    return out


def remove(board_path, request_path, receipt_path):
    import pcbnew
    board, request_path, receipt = map(Path, (board_path, request_path, receipt_path))
    request = custody._json(request_path)
    expected = custody.inventory(request['facts'])
    ids = request['removed_uuids']
    if not isinstance(ids, list) or not ids or len(set(ids)) != len(ids) or any(u not in expected for u in ids):
        raise custody.OwnershipError('invalid exact removal allowlist')
    if common.sha256(board) != request['board_sha256']:
        raise custody.OwnershipError('disposable source hash mismatch')
    facts_main(board, board.parent / 'before-facts.json')
    before = custody._json(board.parent / 'before-facts.json')
    if before != request['facts']:
        raise custody.OwnershipError('native re-extraction differs from bound facts')
    structure = nontrack_tree(board)
    copper = copper_trees(board)
    native = pcbnew.LoadBoard(str(board))
    objects = {}
    for obj in native.GetTracks():
        u = obj.m_Uuid.AsString()
        if u in objects:
            raise custody.OwnershipError('duplicate live native UUID')
        objects[u] = obj
    if set(objects) != set(expected):
        raise custody.OwnershipError('native object inventory mismatch')
    # Refuse unsupported primitive and special via modes before any mutation.
    tree = sexpr(board.read_text())[0]
    blocks = {}
    for node in tree[1:]:
        if isinstance(node, list) and node and node[0] in ('segment', 'arc', 'via'):
            u = next((c[1] for c in node if isinstance(c, list) and c and c[0] == 'uuid'), None)
            if u:
                blocks[u] = node
    for u in ids:
        rec = expected[u]
        if rec['locked'] or rec['net'] not in request['selected_nets']:
            raise custody.OwnershipError('protected or unselected primitive')
        if rec['collection'] == 'tracks' and rec['kind'] != 'segment':
            raise custody.OwnershipError('unsupported selected arc')
        if rec['collection'] == 'vias':
            if rec['via_type'] != 'through' or rec['layers'] != ['F.Cu', 'B.Cu']:
                raise custody.OwnershipError('unsupported selected via type/span')
            allowed = {'at', 'size', 'drill', 'layers', 'net', 'uuid'}
            node = blocks.get(u)
            if node is None or any(not isinstance(c, list) or not c or c[0] not in allowed for c in node[1:]):
                raise custody.OwnershipError('unsupported selected via attributes')
    for u in ids:
        native.Remove(objects[u])
    saved = native.Save(str(board))
    if saved is False:
        raise custody.OwnershipError('native save failed')
    facts_main(board, board.parent / 'survivor-facts.json')
    after = custody._json(board.parent / 'survivor-facts.json')
    survivors = {u: x for u, x in expected.items() if u not in ids}
    if custody.inventory(after) != survivors:
        raise custody.OwnershipError('saved survivor inventory mismatch')
    if copper_trees(board) != {u: node for u, node in copper.items() if u not in ids}:
        raise custody.OwnershipError('saved survivor copper attributes changed')
    ignored = {'sha256', 'tracks', 'vias'}
    if {k: v for k, v in before.items() if k not in ignored} != {k: v for k, v in after.items() if k not in ignored}:
        raise custody.OwnershipError('native non-track facts changed')
    if nontrack_tree(board) != structure:
        raise custody.OwnershipError('canonical non-track board structure changed')
    common.write_json(receipt, {'status': 'prepared', 'input_board_sha256': request['board_sha256'],
                               'output_board_sha256': after['sha256'], 'removed_uuids': sorted(ids),
                               'survivor_facts_sha256': common.sha256(board.parent / 'survivor-facts.json'),
                               'survivors_verified': True, 'nontrack_verified': True})


if __name__ == '__main__':
    remove(*sys.argv[1:])
