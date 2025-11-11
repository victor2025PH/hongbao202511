# scripts/manifest.py
from pathlib import Path
import fnmatch

ROOT = Path(__file__).resolve().parents[1]

INCLUDE = [
    "app.py",
    "config/**/*.py",
    "core/**/*.py",
    "exports/**/*.py",
    "locales/**/*.py",
    "middlewares/**/*.py",
    "models/**/*.py",
    "routers/**/*.py",
    "scripts/**/*.py",
    "services/**/*.py",
    "templates/**/*.*",
    "static/**/*.*",
    "tests/**/*.py",
    "README.md",
    "docs/**/*.md",
    "project_tree.txt",
    "star-groups-spec-v2.md",
]

EXCLUDE = [
    ".venv/**", "venv/**", "__pycache__/**", "*.pyc", "*.pyo", "*.pyd",
    "*.egg-info/**", ".pytest_cache/**", ".mypy_cache/**", ".ruff_cache/**",
    ".cache/**", ".git/**", ".idea/**", ".vscode/**", "node_modules/**",
    "dist/**", "build/**", "coverage/**", "*.log", "*.sqlite", "*.xls*", "*.zip"
]

def match_any(path, patterns):
    p = str(path).replace("\\", "/")
    return any(fnmatch.fnmatch(p, pat) for pat in patterns)

files = []
for p in ROOT.rglob("*"):
    if not p.is_file():
        continue
    if match_any(p, EXCLUDE):
        continue
    if match_any(p, INCLUDE):
        files.append(p.relative_to(ROOT))

print("\n".join(str(f) for f in sorted(files)))
