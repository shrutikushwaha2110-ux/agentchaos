"""Install this repo's git hooks. Run once after cloning:

    python scripts/install_hooks.py

Git hooks aren't copied with the repo, so this points git at the .githooks/
folder that IS tracked. Afterwards, .githooks/pre-commit runs on every commit.
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

subprocess.run(["git", "config", "core.hooksPath", ".githooks"], cwd=ROOT, check=True)
print("Git hooks installed. Each commit now runs scripts/check_secrets.py first.")
