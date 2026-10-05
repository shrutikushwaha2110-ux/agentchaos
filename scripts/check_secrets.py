"""Pre-commit guard: block a commit that would publish an API key.

It checks the staged version of each file being committed (what the commit
would actually contain, not the working copy):
- .env.example: every KEY=value line must have an empty value;
- any file: nothing that looks like a Gemini key (starts AQ.) or a Groq key
  (starts gsk_).

Findings give the file and line number only. The matching text is never
printed, so the output can't leak the key it found.

Run by .githooks/pre-commit. Install the hook once per clone with
scripts/install_hooks.py.
"""
import re
import subprocess
import sys
from pathlib import Path

PATTERNS = {
    "Gemini key": re.compile(r"AQ\.[A-Za-z0-9_\-]{20,}"),
    "Groq key": re.compile(r"gsk_[A-Za-z0-9]{20,}"),
}
ENV_EXAMPLE = ".env.example"


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def staged_paths() -> list[str]:
    """Files this commit adds or changes. -z keeps unusual file names intact."""
    out = git("diff", "--cached", "--name-only", "--diff-filter=ACM", "-z")
    return [p for p in out.decode("utf-8", errors="replace").split("\0") if p]


def staged_text(path: str) -> str:
    """The version of a file that's in the index (what gets committed)."""
    return git("show", f":{path}").decode("utf-8", errors="replace")


def find_problems(path: str, text: str) -> list[str]:
    """Return one message per problem. Never includes the matched value."""
    problems = []
    is_env_example = Path(path).name == ENV_EXAMPLE
    for number, line in enumerate(text.splitlines(), start=1):
        for label, pattern in PATTERNS.items():
            if pattern.search(line):
                problems.append(f"{path}:{number}: looks like a {label}")
        if is_env_example and not line.lstrip().startswith("#"):
            name, sep, value = line.partition("=")
            if sep and value.strip():
                problems.append(f"{path}:{number}: {name.strip()} has a value (it must be empty)")
    return problems


def main() -> int:
    problems = []
    for path in staged_paths():
        problems += find_problems(path, staged_text(path))
    if problems:
        print("commit blocked: possible secrets in the staged files (values not shown):", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
