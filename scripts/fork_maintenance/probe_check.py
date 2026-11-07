#!/usr/bin/env python3
"""Check for the repair probe: inspects the resolved file syntactically; never executes it."""
import ast
from pathlib import Path
import sys

SOURCE = Path(sys.argv[1]) / 'agent' / 'curator.py'


def main() -> int:
    if SOURCE.is_symlink() or not SOURCE.is_file():
        raise SystemExit('probe file missing')
    text = SOURCE.read_text(encoding='utf-8')
    tree = ast.parse(text)
    values = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                values[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                pass
    functions = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
    if values.get('DESCRIPTION_LIMIT') != 60 or values.get('CONSERVATIVE_CURATION') is not True or functions != ['describe']:
        raise SystemExit('probe resolution lost upstream or fork content: %r %r' % (values, functions))
    print('probe resolution accepted')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
