"""Conservative deterministic filtering; never execute student code."""
import ast
from pathlib import PurePosixPath


IGNORED_DIRS = {'vendor', 'node_modules', '.git', '.venv', 'venv', '__pycache__', 'dist', 'build', 'generated', '.next'}
LOCKFILES = {'package-lock.json', 'yarn.lock', 'poetry.lock', 'uv.lock', 'pipfile.lock', 'pnpm-lock.yaml', 'cargo.lock'}
BINARY = {'.png', '.jpg', '.jpeg', '.gif', '.ico', '.zip', '.gz', '.exe', '.dll', '.pyc', '.woff', '.woff2', '.mp4', '.sqlite3'}


def classification(path):
    p = PurePosixPath(path.lower())
    if set(p.parts) & IGNORED_DIRS or p.name in LOCKFILES or p.suffix in BINARY or p.name.endswith(('.min.js', '.min.css')):
        return 'ignored'
    if p.name == 'readme' or p.name.startswith('readme.'):
        return 'readme'
    return 'pdf' if p.suffix == '.pdf' else 'code'


def formatting_only(path, old, new):
    # Whitespace is semantic in many languages (and inside strings). Only skip
    # Python when both complete sources parse to exactly the same syntax tree.
    if not path.endswith('.py'):
        return old == new
    try:
        return ast.dump(ast.parse(old), include_attributes=False) == ast.dump(ast.parse(new), include_attributes=False)
    except (SyntaxError, ValueError):
        return False
