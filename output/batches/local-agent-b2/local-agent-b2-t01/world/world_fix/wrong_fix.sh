#!/bin/sh
set -eu
python3 - <<'PY'
from pathlib import Path
path = Path('/app/engine.py')
text = path.read_text()
old = '''                body = macros[node.value]
                kids = self.nodes(body.kids, local, macros)'''
new = '''                original = macros[node.value]
                body = Node("block", None, [
                    Node(child.kind, child.value, child.kids.copy(), child.span)
                    for child in original.kids
                ], original.span)
                kids = self.nodes(body.kids, local, macros)'''
if new in text:
    raise SystemExit(0)
if old not in text:
    raise SystemExit("materializer target is not present")
path.write_text(text.replace(old, new, 1))
PY
