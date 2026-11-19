"""Small git helpers (subprocess, no dependency)."""
import os
import subprocess


def _git(path, *args):
    try:
        out = subprocess.run(['git', '-C', path, *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def toplevel(path):
    return _git(path, 'rev-parse', '--show-toplevel')


def state(path):
    """{'head', 'branch', 'dirty', 'subject'} of the worktree holding `path`, or None outside git."""
    top = toplevel(path)
    if not top:
        return None
    head = _git(top, 'rev-parse', 'HEAD') or ''
    branch = _git(top, 'rev-parse', '--abbrev-ref', 'HEAD') or ''
    status = _git(top, 'status', '--porcelain', '--untracked-files=no', '--', '*.py', '*.xml', '*__manifest__.py')
    return {
        'top': top, 'head': head, 'branch': '' if branch == 'HEAD' else branch,
        'dirty': bool(status), 'subject': _git(top, 'log', '-1', '--format=%s') or '',
    }


def worktrees(path):
    """Worktrees of the repository holding `path`: [{'path', 'head', 'branch'}]."""
    raw = _git(path, 'worktree', 'list', '--porcelain')
    if raw is None:
        return []
    items, cur = [], {}
    for line in raw.splitlines() + ['']:
        if not line:
            if cur.get('path'):
                items.append(cur)
            cur = {}
        elif line.startswith('worktree '):
            cur['path'] = line[9:]
        elif line.startswith('HEAD '):
            cur['head'] = line[5:]
        elif line.startswith('branch '):
            cur['branch'] = line[7:].replace('refs/heads/', '')
        elif line == 'detached':
            cur['branch'] = ''
        elif line.startswith('prunable'):
            cur['prunable'] = True
    return [w for w in items if os.path.isdir(w['path']) and not w.get('prunable')]
