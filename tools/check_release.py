"""Fail closed on common credential patterns and accidentally tracked runtime data.

Print locations/categories only, never matched values. This is a source guard,
not proof of no secret; review the exact release tree and artifact as well.
"""
import argparse
from pathlib import Path
import re
import subprocess


PATTERNS = {
    'telegram-token': r'(?<![\w-])\d{6,12}:AA[A-Za-z0-9_-]{25,}(?![\w-])',
    'github-token': r'\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{30,})\b',
    'provider-key': r'\bsk-(?:proj-|svcacct-|ant-)?[A-Za-z0-9_-]{20,}\b',
    'aws-access-id': r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b',
    'google-api-key': r'\bAIza[A-Za-z0-9_-]{35}\b',
    'slack-token': r'\bxox[baprs]-[A-Za-z0-9-]{16,}\b',
    'private-key': r'-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----',
    'credential-url': r'(?:https?|postgres(?:ql)?|mysql|redis)://[^\s/:@]+:[^\s/@]{3,}@',
    'personal-home-path': r'/Users/(?!example(?:/|[\s\"\']))[A-Za-z0-9_.-]+/',
}
FORBIDDEN_PARTS = {'private', '.venv', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache'}
FORBIDDEN_NAMES = {'installation.json', 'setup-target.json', 'bridges.json', 'bot-token'}
EXCLUDE_UNTRACKED = {'.git', '.venv', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache'}


def files(root):
    if (root / '.git').exists():
        result = subprocess.run(['git', '-C', str(root), 'ls-files', '-z'],
                                capture_output=True, check=True)
        return [root / name for name in result.stdout.decode().split('\0') if name]
    return [p for p in root.rglob('*') if p.is_file()
            and not any(part in EXCLUDE_UNTRACKED or part.endswith('.egg-info')
                        for part in p.relative_to(root).parts)]


def scan(root):
    problems = []
    candidates = files(root)
    for path in candidates:
        name = path.relative_to(root)
        if (any(p in FORBIDDEN_PARTS for p in name.parts) or path.name in FORBIDDEN_NAMES
                or path.name == '.env' or path.name.startswith('.env.')
                or path.suffix in {'.log', '.pid', '.pem', '.key', '.p12', '.pfx'}):
            problems.append((str(name), 0, 'runtime-or-credential-path'))
        if path.is_symlink():
            problems.append((str(name), 0, 'release-symlink-needs-review'))
            continue
        data = path.read_text(errors='replace')
        for kind, expression in PATTERNS.items():
            for match in re.finditer(expression, data):
                problems.append((str(name), data.count('\n', 0, match.start()) + 1, kind))
        if 'src/codex_telegram_bridge' in str(name):
            for match in re.finditer(r'\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b', data):
                problems.append((str(name), data.count('\n', 0, match.start()) + 1, 'hardcoded-runtime-task-id'))
    return len(candidates), problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    count, problems = scan(args.root.resolve())
    for path, line, kind in problems:
        print(f'{path}:{line}: {kind}')
    print(f'Checked {count} source files; {len(problems)} blocking findings. No matched values printed.')
    return bool(problems)


if __name__ == '__main__':
    raise SystemExit(main())
