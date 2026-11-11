"""Quote-aware boundaries shared by routing input and output checks."""
import hashlib
import json
import re


def expression_spans(content: str, tags):
    """Find real expression tokens, validating nesting and quoted strings.

    One lexical pass avoids interpreting text such as "(gr_text" as syntax.
    Yielded spans include the closing parenthesis; malformed input fails before
    a writer can emit a partial board.
    """
    stack = []
    spans = []
    quoted = escaped = False
    token = re.compile(r'\s*([^\s()]+)')
    for i, ch in enumerate(content):
        if quoted:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                quoted = False
        elif ch == '"':
            quoted = True
        elif ch == '(':
            match = token.match(content, i + 1)
            stack.append((i, match.group(1) if match else ''))
        elif ch == ')':
            if not stack:
                raise ValueError(f"Unexpected closing parenthesis at offset {i}")
            start, tag = stack.pop()
            if tags is None or tag in tags:
                spans.append((start, i + 1, tag))
    if quoted or stack:
        raise ValueError("Truncated PCB S-expression: unterminated " +
                         ("quoted string" if quoted else f"block at offset {stack[-1][0]}"))
    return sorted(spans)


# Copper artwork whose outline only KiCad can compute. The wrapper supplies each
# item's native bounding box; the parser never approximates glyphs, table grids,
# dimension arrows or barcode modules. Tag spellings cover KiCad 6 to 10 files.
COPPER_ARTWORK_TAGS = frozenset({'gr_text', 'fp_text', 'property', 'gr_text_box', 'fp_text_box',
                                 'dimension', 'table', 'gr_table', 'barcode', 'gr_barcode'})


def copper_text_blocks(content):
    result = []
    for start, end, tag in expression_spans(content, COPPER_ARTWORK_TAGS):
        block = content[start:end]
        for a, b, _ in expression_spans(block, {'layer'}):
            match = re.fullmatch(r'\(layer\s+"([^"]+)"(?:\s+knockout)?\s*\)', block[a:b])
            if match and match.group(1).endswith('.Cu'):
                result.append(block)
                break
    return result


def copper_text_signature(content):
    # Tokenize while retaining quoted strings exactly. Native lock/save and the
    # writer may change indentation, which does not change the text geometry.
    blocks = copper_text_blocks(content)
    # Footprint text uses local coordinates. Moving its owner invalidates the
    # native bounds even when the text's own expression is byte-identical.
    for start, end, _ in expression_spans(content, {'footprint', 'module'}):
        footprint = content[start:end]
        if not copper_text_blocks(footprint):
            continue
        ancestors = []
        for a, b, tag in expression_spans(footprint, None):
            while ancestors and a >= ancestors[-1]:
                ancestors.pop()
            if len(ancestors) == 1 and tag in {'at', 'layer'}:
                blocks.append(footprint[a:b])
            ancestors.append(b)
    tokens = [re.findall(r'"(?:\\.|[^"\\])*"|[^\s()]+|[()]', block) for block in blocks]
    return hashlib.sha256(json.dumps(tokens, ensure_ascii=True).encode()).hexdigest()


def copper_text_obstacles(content):
    """Copper text as router obstacles, at KiCad's own conservative bounds.

    Fonts, justification, rotation and knockout belong to KiCad. The wrapper
    supplies native bounds from its isolated snapshot (KICAD_ROUTE_COPPER_TEXT)
    instead of approximating glyph geometry here; they are bound to the exact
    input text. A direct engine caller must supply the same evidence or gets
    a limitation before search begins. Returns kicad_parser.Segment rows.
    """
    import os
    from pathlib import Path
    from kicad_parser import Segment, IU_PER_MM
    if not copper_text_blocks(content):
        return []
    evidence_path = os.environ.get('KICAD_ROUTE_COPPER_TEXT')
    if not evidence_path:
        raise ValueError('Copper text requires native bounding-box evidence; run through kicad-route')
    evidence = json.loads(Path(evidence_path).read_text())
    if evidence.get('signature') != copper_text_signature(content):
        raise ValueError('Copper text changed since native bounding-box preflight')
    # The native side lists what KiCad plots on copper: a hidden field is a text
    # block here and no entry there, and a dimension is one entry for several
    # blocks. The signature above binds the list to this exact text, so the two
    # are not counted against each other.
    segments = []
    for text in evidence.get('texts', []):
        x, y, width, height = [float(n) / IU_PER_MM for n in text['bbox']]
        # `copper_layer` is KiCad's canonical name. `layer` is the board's own name
        # for it, which a design may have renamed ("top_copper").
        layer = text.get('copper_layer') or text['layer']
        if not layer.endswith('.Cu') or width < 0 or height < 0:
            raise ValueError(f'Invalid native copper-text bounds for {text.get("uuid")} on {layer!r}')
        if width == 0 or height == 0:
            continue
        ring = ((x, y), (x+width, y), (x+width, y+height), (x, y+height))
        # The bounding rectangle is intentionally conservative: its interior
        # stays unavailable even between letters and in enclosed glyph holes.
        for a, b in zip(ring, ring[1:] + ring[:1]):
            segments.append(Segment(*a, *b, 0.0, layer, 0,
                uuid=text['uuid'], graphic=True, drawn_width=0.0,
                graphic_kind='rect', graphic_filled=True, graphic_ring=ring))
    return segments


def unsupported_copper(content):
    supported = {'gr_line', 'gr_arc', 'gr_poly', 'gr_rect', 'gr_circle',
                 'fp_line', 'fp_arc', 'fp_poly', 'fp_rect', 'fp_circle'} | COPPER_ARTWORK_TAGS
    tags = supported | {'gr_curve', 'fp_curve'}
    for start, end, tag in expression_spans(content, tags):
        block = content[start:end]
        for a, b, _ in expression_spans(block, {'layer', 'layers'}):
            layers = re.findall(r'"([^"]+)"', block[a:b])
            copper = [layer for layer in layers if layer.endswith('.Cu')]
            if copper and (tag not in supported or '*.Cu' in copper):
                return f'KRT cannot model {tag} on {", ".join(copper)}; retained copper artwork is never relocated'
    return None


def locked_block(block):
    """Whether a track or via block carries KiCad's lock at its own level.

    Only a depth-1 `(locked yes)`, `(locked)` or bare `locked` word counts:
    the same text inside a quoted net name is not a lock, and does not hide a
    real one that follows it.
    """
    if 'locked' not in block:
        return False
    from kicad_parser import _sexpr_children
    children, bare = _sexpr_children(block)
    return 'locked' in bare or any(
        not args or args[0] in ('yes', 'true') for args in children.get('locked', ()))


def keeps_locked_targets(remove):
    """Wrap a writer removal function so a locked target is never searched for.

    `remove(content, targets, net_id_to_name=None, unmatched_out=None, ...)`
    matches by geometry and net, so a locked target could take an unlocked twin
    with it. A locked target is handed back in `unmatched_out` instead.
    """
    import functools

    @functools.wraps(remove)
    def guarded(content, targets, *args, **kwargs):
        held = [t for t in targets or () if getattr(t, 'locked', False)]
        if not held:
            return remove(content, targets, *args, **kwargs)
        unmatched = kwargs.get('unmatched_out')
        if unmatched is None and len(args) >= 2:
            unmatched = args[1]
        if unmatched is not None:
            unmatched.extend(held)
        return remove(content, [t for t in targets if not getattr(t, 'locked', False)], *args, **kwargs)
    return guarded
