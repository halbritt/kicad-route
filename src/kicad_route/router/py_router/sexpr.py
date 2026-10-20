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


def copper_text_blocks(content):
    result = []
    for start, end, tag in expression_spans(content, {'gr_text', 'fp_text', 'property'}):
        block = content[start:end]
        for a, b, _ in expression_spans(block, {'layer'}):
            match = re.fullmatch(r'\(layer\s+"([^"]+)"\s*\)', block[a:b])
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



def unsupported_copper(content):
    supported = {'gr_line', 'gr_arc', 'gr_poly', 'gr_rect', 'gr_circle',
                 'fp_line', 'fp_arc', 'fp_poly', 'fp_rect', 'fp_circle',
                 'gr_text', 'fp_text', 'property'}
    tags = supported | {'gr_curve', 'fp_curve', 'gr_text_box', 'fp_text_box',
                        'dimension', 'gr_table', 'gr_barcode'}
    for start, end, tag in expression_spans(content, tags):
        block = content[start:end]
        for a, b, _ in expression_spans(block, {'layer', 'layers'}):
            layers = re.findall(r'"([^"]+)"', block[a:b])
            copper = [layer for layer in layers if layer.endswith('.Cu')]
            if copper and (tag not in supported or '*.Cu' in copper):
                return f'KRT cannot model {tag} on {", ".join(copper)}; retained copper artwork is never relocated'
    return None
