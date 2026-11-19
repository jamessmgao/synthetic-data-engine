#!/bin/sh
set -eu
python3 - <<'PY'
from pathlib import Path

parser = Path('/app/parser.py')
text = parser.read_text()
if 'def clone_node(' not in text:
    marker = '\ndef parse(source):\n'
    if marker not in text:
        raise SystemExit('parser insertion point is not present')
    clone = '''\ndef clone_node(node):
    return Node(node.kind, node.value, [clone_node(child) for child in node.kids], node.span)
'''
    text = text.replace(marker, clone + marker, 1)
    parser.write_text(text)

engine = Path('/app/engine.py')
text = engine.read_text()
if 'from parser import Node, Span, clone_node, parse' not in text:
    text = text.replace('from parser import Node, Span, parse', 'from parser import Node, Span, clone_node, parse', 1)

text = text.replace('                body = macros[node.value]', '                body = clone_node(macros[node.value])')

text = text.replace('''                original = macros[node.value]
                body = Node("block", None, [
                    Node(child.kind, child.value, child.kids.copy(), child.span)
                    for child in original.kids
                ], original.span)
                kids = self.nodes(body.kids, local, macros)''', '''                body = clone_node(macros[node.value])
                kids = self.nodes(body.kids, local, macros)''')

engine.write_text(text)
PY
