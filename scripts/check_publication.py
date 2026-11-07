"""Check publishable Git files without printing secret values. Not a security audit."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DIRS = {".local", "data", "secrets", "evidence", "node_modules", "__pycache__"}
PRIVATE_SUFFIXES = {".key", ".pem", ".p12", ".enc", ".db", ".sqlite", ".sqlite3", ".log", ".jsonl"}
PATTERNS = {
    "provider key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}"),
    "gateway key": re.compile(r"\bgw_[A-Za-z0-9_-]{32,}"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}


def check():
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, capture_output=True, check=True,
    )
    paths = sorted(set(result.stdout.decode("utf-8").strip("\0").split("\0")))
    errors = []
    for name in paths:
        if not name:
            continue
        path = ROOT / name
        relative = Path(name)
        if path.is_symlink():
            errors.append(f"{name}: symlinks require manual review")
            continue
        if (
            PRIVATE_DIRS.intersection(relative.parts)
            or any(part.startswith(".venv") for part in relative.parts)
            or relative.suffix in PRIVATE_SUFFIXES
            or relative.name in {".env", ".runtime.local.json", "client-key.txt"}
            or (relative.name.startswith(".env.") and relative.name != ".env.example")
        ):
            errors.append(f"{name}: runtime/private file")
        if not path.is_file():
            errors.append(f"{name}: missing file")
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            errors.append(f"{name}: binary requires manual review")
            continue
        for label, pattern in PATTERNS.items():
            if pattern.search(content):
                errors.append(f"{name}: possible {label}")
        if path.suffix == ".md":
            for link in re.findall(r"\]\(([^)\s]+)\)", content):
                if ":" in link or link.startswith("#"):
                    continue
                target = link.split("#", 1)[0]
                if target and not (path.parent / target).exists():
                    errors.append(f"{name}: broken local link")
    if errors:
        raise SystemExit("Publication check failed:\n" + "\n".join(errors))
    print(f"Publication check passed: {len(paths)} files; no detected credential or private runtime file.")


if __name__ == "__main__":
    check()
