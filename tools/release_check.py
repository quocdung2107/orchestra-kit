"""Pre-release scan: fail if the tree contains private names, local paths, e-mail addresses or non-English text.

    python tools/release_check.py                     # uses tools/denylist.txt if present
    python tools/release_check.py --denylist FILE     # extra patterns from FILE

Patterns: one Python regex per line (case-insensitive), `#` starts a comment. The denylist is personal and is
listed in .gitignore; without one, only the generic checks run. Exit code 1 when anything is found.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_DENYLIST = REPO / "tools" / "denylist.txt"

GENERIC = [
    ("windows user path", r"\b[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s\"'`]+"),
    ("posix home path", r"(?<![\w.])/(?:home|Users)/[A-Za-z0-9._-]+"),
    ("git-bash user path", r"(?<![\w.])/[a-z]/Users/[A-Za-z0-9._-]+"),
    ("e-mail address", r"\b[A-Za-z0-9._%+-]+@(?!example\.(?:com|org)\b)(?!users\.noreply\.github\.com\b)"
                       r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"),
]
# Vietnamese letters with diacritics (the kit is English only)
VIETNAMESE = re.compile("[\u00e0\u00e1\u1ea1\u1ea3\u00e3\u00e2\u1ea7\u1ea5\u1ead\u1ea9\u1eab\u0103\u1eb1\u1eaf\u1eb7\u1eb3\u1eb5\u00e8\u00e9\u1eb9\u1ebb\u1ebd\u00ea\u1ec1\u1ebf\u1ec7\u1ec3\u1ec5\u00ec\u00ed\u1ecb\u1ec9\u0129\u00f2\u00f3\u1ecd\u1ecf\u00f5\u00f4\u1ed3\u1ed1\u1ed9\u1ed5\u1ed7\u01a1\u1edd\u1edb\u1ee3\u1edf\u1ee1\u00f9\u00fa\u1ee5\u1ee7\u0169\u01b0\u1eeb\u1ee9\u1ef1\u1eed\u1eef\u1ef3\u00fd\u1ef5\u1ef7\u1ef9\u0111"
                        "\u00c0\u00c1\u1ea0\u1ea2\u00c3\u00c2\u1ea6\u1ea4\u1eac\u1ea8\u1eaa\u0102\u1eb0\u1eae\u1eb6\u1eb2\u1eb4\u00c8\u00c9\u1eb8\u1eba\u1ebc\u00ca\u1ec0\u1ebe\u1ec6\u1ec2\u1ec4\u00cc\u00cd\u1eca\u1ec8\u0128\u00d2\u00d3\u1ecc\u1ece\u00d5\u00d4\u1ed2\u1ed0\u1ed8\u1ed4\u1ed6\u01a0\u1edc\u1eda\u1ee2\u1ede\u1ee0\u00d9\u00da\u1ee4\u1ee6\u0168\u01af\u1eea\u1ee8\u1ef0\u1eec\u1eee\u1ef2\u00dd\u1ef4\u1ef6\u1ef8\u0110]")
SKIP = {"tools/denylist.txt"}


def files() -> list:
    """Files that would be published: tracked + untracked, minus .gitignore'd ones (falls back to a tree walk)."""
    try:
        out = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                             capture_output=True, check=True).stdout
        names = [n for n in out.decode("utf-8").split("\0") if n]
    except (OSError, subprocess.CalledProcessError):
        names = [p.relative_to(REPO).as_posix() for p in REPO.rglob("*")
                 if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts]
    return sorted(n for n in names if n not in SKIP and (REPO / n).is_file())


def load_patterns(path: Path) -> list:
    rules = []
    if path.is_file():
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if line and not line.startswith("#"):
                rules.append((f"denylist:{n}", line))
    return rules


def scan(rules: list) -> list:
    compiled = [(name, re.compile(rx, re.I)) for name, rx in rules]
    hits = []
    for rel in files():
        try:
            text = (REPO / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary file
        for name, rx in compiled:
            if rx.search(rel):
                hits.append(f"{rel}: file name matches {name}")
        for i, line in enumerate(text.splitlines(), 1):
            for name, rx in compiled:
                m = rx.search(line)
                if m:
                    hits.append(f"{rel}:{i}: {name}: {m.group(0)[:60]}")
            m = VIETNAMESE.search(line)
            if m:
                hits.append(f"{rel}:{i}: non-English text: {line.strip()[:60]}")
    return hits


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--denylist", type=Path, default=DEFAULT_DENYLIST)
    a = ap.parse_args(argv)
    rules = GENERIC + load_patterns(a.denylist)
    hits = scan(rules)
    for h in hits:
        print(h)
    extra = len(rules) - len(GENERIC)
    print(f"release_check: {len(files())} files, {len(GENERIC)} generic + {extra} denylist patterns, "
          f"{len(hits)} findings")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
