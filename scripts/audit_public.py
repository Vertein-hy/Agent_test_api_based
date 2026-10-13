"""Audit publishable text without printing secret or personal matched values.

Use --tracked before publication and in CI, so Git-ignored runtime files cannot
hide an accidentally staged secret. Default mode inspects the candidate tree.
This is a supplemental review gate, not a guarantee about arbitrary future data.
"""

import argparse
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".git", ".venv", ".ruff_cache", "__pycache__", "node_modules"}
FORBIDDEN = {"secrets", "state", "evidence", "backups", "private", "models"}
ALLOWED_SUFFIXES = {
    ".py",
    ".sh",
    ".mjs",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".txt",
    ".md",
    ".example",
    ".dockerignore",
}
FIXTURE_KEYS = {"sk-0123456789abcdef", "sk-1234567890abcdef", "sk-secretsecretsecret"}
RULES = {
    "personal_host_path": re.compile(r"/(?:Users|home)/[A-Za-z0-9_.-]+/"),
    "private_key": re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    "github_token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    "api_key": re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    "credential_url": re.compile(r"https?://[^\s/@:]+:[^\s/@]+@"),
    "private_provider": re.compile("discovery" + r"-api\.intern-ai\.org\.cn"),
}


def audit(paths):
    findings = []
    count = 0
    for rel in paths:
        path = ROOT / rel
        if not path.is_file() and not path.is_symlink():
            continue
        count += 1
        parts = rel.parts
        # tools/models contains authored source, not model artifacts.
        forbidden = set(parts[:-1]) & FORBIDDEN
        if parts[:2] == ("tools", "models"):
            forbidden.discard("models")
        if forbidden or path.is_symlink() or path.name in {".env", "persona.txt"}:
            findings.append({"path": str(rel), "rule": "private_path_or_symlink"})
            continue
        if path.suffix in {".db", ".sqlite", ".sqlite3", ".pem", ".key", ".pt", ".safetensors"}:
            findings.append({"path": str(rel), "rule": "runtime_artifact"})
            continue
        if path.name.startswith(".env.") or path.suffix == ".env":
            findings.append({"path": str(rel), "rule": "private_environment"})
        if path.suffix not in ALLOWED_SUFFIXES and path.name not in {
            "LICENSE",
            ".gitignore",
            ".dockerignore",
            "Dockerfile.bot",
            "Dockerfile.memory",
            "Dockerfile.panel",
        }:
            findings.append({"path": str(rel), "rule": "unexpected_file_type"})
            continue
        try:
            content = path.read_text()
        except UnicodeError:
            findings.append({"path": str(rel), "rule": "binary_content"})
            continue
        for name, rule in RULES.items():
            matches = list(rule.finditer(content))
            if name == "api_key" and (
                parts[0] == "tests" or rel.as_posix() == "scripts/audit_public.py"
            ):
                matches = [m for m in matches if m[0] not in FIXTURE_KEYS]
            if name == "credential_url" and (
                parts[0] == "tests" or rel.as_posix() == "scripts/audit_public.py"
            ):
                matches = [m for m in matches if m[0] not in {"https://user:pass@"}]
            if matches:
                findings.append({"path": str(rel), "rule": name, "count": len(matches)})
    return {"passed": not findings, "files_checked": count, "findings": findings}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tracked", action="store_true")
    args = parser.parse_args()
    if args.tracked:
        result = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
        )
        paths = [Path(p.decode()) for p in result.stdout.split(b"\0") if p]
        if not paths:
            raise SystemExit("No tracked files to audit")
    else:
        paths = [
            p.relative_to(ROOT)
            for p in ROOT.rglob("*")
            if not set(p.relative_to(ROOT).parts) & SKIP
        ]
    result = audit(paths)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
