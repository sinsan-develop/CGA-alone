"""Validate that the Git index is suitable for the public CGA repository."""

from __future__ import annotations

import argparse
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path, PurePosixPath


MANUAL_PREFIXES = (
    "docs/manual/",
    "apps/web/public/manuals/",
)
PUBLIC_READMES = frozenset(
    {
        "README.md",
        "README.en.md",
        "README.zh-CN.md",
        "README.ja.md",
        "README.vi.md",
        "README.fr.md",
        "README.de.md",
    }
)
FORBIDDEN_DOCUMENTS = {
    "AGENTS.md",
    "CODEx_WORK_LOG.md",
    "apps/api/README.md",
    "apps/vector-worker/README.md",
    "docs/manual/cga-manual-verification-matrix.md",
    "docs/manual/cga-nlu-guide/engine-comparison.md",
}
FORBIDDEN_CONTENT = {
    "personal-identifier": re.compile(r"(?i)cy" r"huh|허" r"철영"),
    "operations-domain": re.compile(r"(?i)(?:[a-z0-9-]+\.)*sinsan\.kr"),
    "server-deploy-path": re.compile(r"/home/(?:ubuntu|daon)/deploy/"),
    "windows-user-path": re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+"),
}
PUBLIC_PROTOCOL_EMAILS = frozenset({"chat@system.gserviceaccount.com"})

EMAIL_PATTERN = re.compile(
    r"(?i)\b[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z]{2,63}\b"
)


def _normalise_path(path: str) -> str:
    return PurePosixPath(path.replace("\\", "/")).as_posix().lstrip("./")


def _is_manual(path: str) -> bool:
    return path not in FORBIDDEN_DOCUMENTS and path.startswith(MANUAL_PREFIXES)


def collect_tracked_paths(repo: Path) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return tuple(
        item.decode("utf-8")
        for item in result.stdout.split(b"\0")
        if item
    )


def find_path_violations(paths: Iterable[str]) -> list[str]:
    violations: list[str] = []
    for raw_path in paths:
        path = _normalise_path(raw_path)
        is_root_readme = (
            "/" not in path
            and path.startswith("README")
            and path.endswith(".md")
        )
        forbidden = (
            path in FORBIDDEN_DOCUMENTS
            or path.startswith(".local/")
            or (path.startswith("docs/") and not _is_manual(path))
            or (is_root_readme and path not in PUBLIC_READMES)
        )
        if forbidden:
            violations.append(f"{path}:non-manual-document")
    return sorted(violations)


def _read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except (OSError, ValueError):
        return None
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def find_content_violations(repo: Path, paths: Iterable[str]) -> list[str]:
    violations: list[str] = []
    for raw_path in paths:
        path = _normalise_path(raw_path)
        text = _read_text(repo / Path(path))
        if text is None:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            for rule, pattern in FORBIDDEN_CONTENT.items():
                if rule == "operations-domain" and _is_manual(path):
                    continue
                if pattern.search(line):
                    violations.append(f"{path}:{line_number}:{rule}")
            for match in EMAIL_PATTERN.finditer(line):
                if match.group(0) in PUBLIC_PROTOCOL_EMAILS:
                    continue
                domain = match.group(0).rsplit("@", 1)[1].lower()
                if domain not in {"example.com", "example.org", "example.net"} and not domain.endswith(".invalid"):
                    violations.append(f"{path}:{line_number}:personal-email")
                    break
    return sorted(set(violations))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    paths = collect_tracked_paths(repo)
    violations = find_path_violations(paths) + find_content_violations(repo, paths)
    for violation in sorted(violations):
        print(violation)
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
