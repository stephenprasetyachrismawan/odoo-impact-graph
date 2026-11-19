"""Local web service: projects, their git worktrees, one-click graph generation, explorer, diff.

    odoo-impact serve            -> http://127.0.0.1:8770/

State lives in ODOO_IMPACT_HOME (default ~/.local/share/odoo-impact):
    projects.json                registered projects
    graphs/<id>/graph.json       one graph per worktree (id = hash of the worktree path)
"""
import hashlib
import json
import os
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__, gitinfo

HOME = os.path.expanduser(os.environ.get('ODOO_IMPACT_HOME', '~/.local/share/odoo-impact'))
DASHBOARD = os.path.join(os.path.dirname(__file__), 'dashboard.html')
EXPLORER = os.path.join(os.path.dirname(__file__), 'explorer.html')


# -- storage ------------------------------------------------------------------
def home(*parts):
    return os.path.join(HOME, *parts)


def graph_id(worktree):
    return hashlib.sha1(os.path.realpath(worktree).encode()).hexdigest()[:12]


def graph_path(worktree):
    return home('graphs', graph_id(worktree), 'graph.json')


_lock = threading.Lock()


def load_projects():
    try:
        with open(home('projects.json'), encoding='utf-8') as fh:
            return json.load(fh).get('projects', [])
    except (OSError, ValueError):
        return []


def save_projects(projects):
    os.makedirs(HOME, exist_ok=True)
    tmp = home('projects.json.tmp')
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump({'projects': projects}, fh, indent=1)
    os.replace(tmp, home('projects.json'))


def add_project(repo, conf=None, core=(), modules=None, name=None):
    """Register a repository (any of its worktrees). conf: odoo.conf whose addons_path gives the
    core folders (folders inside the repository are replaced by the worktree being built)."""
    from .cli import addons_path_from_conf
    top = gitinfo.toplevel(repo)
    if not top:
        raise ValueError(f'not inside a git repository: {repo}')
    main = gitinfo.worktrees(top)[0]['path'] if gitinfo.worktrees(top) else top
    conf = os.path.abspath(os.path.expanduser(conf)) if conf else None
    if conf and not addons_path_from_conf(conf):
        raise ValueError(f'no addons_path in {conf}')
    core = [os.path.abspath(os.path.expanduser(c)) for c in core if c.strip()]
    for c in core:
        if not os.path.isdir(c):
            raise ValueError(f'not a folder: {c}')
    with _lock:
        projects = load_projects()
        pid = graph_id(main)
        projects = [p for p in projects if p['id'] != pid]
        projects.append({'id': pid, 'name': name or os.path.basename(main), 'repo': main, 'conf': conf,
                         'core': core, 'modules': modules or None})
        save_projects(projects)
    return pid


def remove_project(pid):
    with _lock:
        save_projects([p for p in load_projects() if p['id'] != pid])


def project_of(worktree):
    """Registered project that owns this worktree, or None."""
    real = os.path.realpath(worktree)
    for p in load_projects():
        if any(os.path.realpath(w['path']) == real for w in gitinfo.worktrees(p['repo'])):
            return p
    return None


def build_args(project, worktree):
    """paths / core / custom for building `worktree` of `project`."""
    from .cli import addons_path_from_conf
    repo = os.path.realpath(project['repo'])
    wt = os.path.realpath(worktree)
    paths, core = [wt], list(project.get('core') or [])
    for p in addons_path_from_conf(project['conf']) if project.get('conf') else []:
        real = os.path.realpath(p)
        if real == repo or real.startswith(repo + os.sep):
            mapped = wt + real[len(repo):]  # same folder inside the worktree being built
            if os.path.isdir(mapped) and mapped not in paths:
                paths.append(mapped)
        elif real not in core:
            core.append(real)
    return {'paths': paths, 'core': core, 'custom': [wt], 'modules': project.get('modules')}


def build_worktree(worktree, log=print):
    project = project_of(worktree)
    if project is None:
        raise ValueError(f'{worktree} does not belong to a registered project: add it in the dashboard '
                         f'or with `odoo-impact project add <repo>`')
    from .buildrun import build_static
    args = build_args(project, worktree)
    log(f"building {worktree} ({project['name']})")
    return build_static(args['paths'], args['core'], args['custom'], args['modules'],
                        output=graph_path(worktree), html=False, log=log)


def graph_status(worktree, current=None):
    path = graph_path(worktree)
    if not os.path.exists(path):
        return {'exists': False}
    try:
        with open(path, encoding='utf-8') as fh:
            meta = json.load(fh).get('meta', {})
    except (OSError, ValueError):
        return {'exists': False}
    built = meta.get('git') or {}
    current = current or gitinfo.state(worktree) or {}
    reason = None
    if current.get('head') and built.get('head') != current.get('head'):
        reason = 'new commits since the build'
    elif current.get('dirty'):
        reason = 'uncommitted changes (rebuild to include them)'
    return {'exists': True, 'built_at': meta.get('built_at'), 'built_head': (built.get('head') or '')[:8],
            'built_branch': built.get('branch'), 'stale': bool(reason), 'reason': reason,
            'custom_modules': len(meta.get('custom_modules') or [])}


# -- jobs -----------------------------------------------------------------------
_jobs = {}
_build_slot = threading.Semaphore(1)  # one build at a time: a build of a big code base uses ~1 GB


def start_build(worktree):
    gid = graph_id(worktree)
    for job in _jobs.values():
        if job['gid'] == gid and job['state'] in ('queued', 'running'):
            return job['id']
    jid = uuid.uuid4().hex[:10]
    job = {'id': jid, 'gid': gid, 'worktree': worktree, 'state': 'queued', 'log': [], 'started': time.time()}
    _jobs[jid] = job

    def run():
        with _build_slot:
            job['state'] = 'running'
            try:
                build_worktree(worktree, log=job['log'].append)
                job['state'] = 'done'
            except Exception as exc:  # noqa: BLE001 -- shown in the dashboard
                job['log'].append(f'{type(exc).__name__}: {exc}')
                traceback.print_exc()
                job['state'] = 'failed'
            job['ended'] = time.time()

    threading.Thread(target=run, daemon=True).start()
    return jid


def overview():
    out = []
    for p in load_projects():
        trees = []
        for w in gitinfo.worktrees(p['repo']):
            cur = gitinfo.state(w['path']) or {}
            job = next((j for j in sorted(_jobs.values(), key=lambda j: -j['started'])
                        if j['gid'] == graph_id(w['path'])), None)
            trees.append({
                'path': w['path'], 'gid': graph_id(w['path']), 'branch': w.get('branch') or '(detached)',
                'head': (w.get('head') or '')[:8], 'subject': cur.get('subject', ''), 'dirty': cur.get('dirty', False),
                'graph': graph_status(w['path'], cur),
                'job': job and {k: job[k] for k in ('id', 'state')},
            })
        out.append({**p, 'worktrees': trees})
    return out


# -- http -----------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = f'odoo-impact/{__version__}'

    def log_message(self, *a):
        pass

    def _host_ok(self):
        # refuse DNS-rebinding: only answer to the loopback names we listen on
        host = (self.headers.get('Host') or '').rsplit(':', 1)[0].strip('[]')
        return host in ('127.0.0.1', 'localhost', '::1', self.server.server_address[0])

    def _send(self, code, body, ctype='application/json'):
        data = body if isinstance(body, bytes) else (json.dumps(body) if ctype == 'application/json' else body).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype + '; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(data)

    def _gid_worktree(self, gid):
        for p in load_projects():
            for w in gitinfo.worktrees(p['repo']):
                if graph_id(w['path']) == gid:
                    return w['path']
        return None

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {'error': 'bad host'})
        url = urlparse(self.path)
        parts = [unquote(x) for x in url.path.strip('/').split('/') if x]
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if not parts:
                with open(DASHBOARD, encoding='utf-8') as fh:
                    return self._send(200, fh.read(), 'text/html')
            if parts == ['api', 'projects']:
                return self._send(200, {'projects': overview(), 'home': HOME})
            if parts[:2] == ['api', 'jobs'] and len(parts) == 3:
                job = _jobs.get(parts[2])
                return self._send(200, job) if job else self._send(404, {'error': 'no such job'})
            if parts[0] == 'explore' and len(parts) == 2:
                wt = self._gid_worktree(parts[1])
                if not wt or not os.path.exists(graph_path(wt)):
                    return self._send(404, 'No graph yet: press Generate in the dashboard.', 'text/plain')
                with open(graph_path(wt), encoding='utf-8') as fh:
                    graph = fh.read().replace('</', '<\\/')
                with open(EXPLORER, encoding='utf-8') as fh:
                    page = fh.read().replace('/*__GRAPH__*/null', graph, 1)
                back = ('<a href="/" style="position:fixed;right:16px;bottom:12px;z-index:20;font:13px system-ui;'
                        'background:var(--ink);color:var(--bg);padding:6px 10px;border-radius:6px;'
                        'text-decoration:none">← Dashboard</a></body>')
                return self._send(200, page.replace('</body>', back, 1), 'text/html')
            if parts[:2] == ['api', 'diff'] and len(parts) == 3:
                from . import query
                wt = self._gid_worktree(parts[2])
                if not wt or not os.path.exists(graph_path(wt)):
                    return self._send(404, {'error': 'no graph'})
                g = query.Graph.load(graph_path(wt))
                base = q.get('base') or 'HEAD'
                text = query.report(g, 'diff', g.diff(wt, base))
                return self._send(200, {'base': base, 'text': text})
            if parts[:2] == ['api', 'ask'] and len(parts) == 3:
                from . import query
                wt = self._gid_worktree(parts[2])
                g = query.Graph.load(graph_path(wt))
                kind, ref = q.get('kind', 'impact'), q.get('ref', '')
                fn = {'impact': g.impact, 'who': g.who, 'reach': g.reach}[kind]
                return self._send(200, {'text': query.report(g, kind, fn(ref))})
        except KeyError as exc:
            return self._send(400, {'error': str(exc.args[0])})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._send(500, {'error': f'{type(exc).__name__}: {exc}'})
        return self._send(404, {'error': 'not found'})

    def do_POST(self):
        # a custom header cannot be sent cross-site without a CORS preflight we never allow
        if not self._host_ok() or self.headers.get('X-Odoo-Impact') != '1':
            return self._send(403, {'error': 'forbidden'})
        length = int(self.headers.get('Content-Length') or 0)
        try:
            body = json.loads(self.rfile.read(length) or b'{}')
        except ValueError:
            return self._send(400, {'error': 'bad json'})
        parts = [x for x in urlparse(self.path).path.strip('/').split('/') if x]
        try:
            if parts == ['api', 'projects']:
                pid = add_project(body.get('repo', ''), body.get('conf') or None,
                                  [c for c in body.get('core', []) if c], body.get('modules') or None,
                                  body.get('name') or None)
                return self._send(200, {'id': pid})
            if parts[:2] == ['api', 'projects'] and len(parts) == 4 and parts[3] == 'delete':
                remove_project(parts[2])
                return self._send(200, {'ok': True})
            if parts == ['api', 'build']:
                wt = self._gid_worktree(body.get('gid', ''))
                if not wt:
                    return self._send(404, {'error': 'unknown worktree'})
                return self._send(200, {'job': start_build(wt)})
        except ValueError as exc:
            return self._send(400, {'error': str(exc)})
        return self._send(404, {'error': 'not found'})


def serve(host='127.0.0.1', port=8770, open_browser=False):
    os.makedirs(HOME, exist_ok=True)
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f'http://{host}:{httpd.server_address[1]}/'
    print(f'odoo-impact service: {url}   (data: {HOME})', flush=True)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('stopped')
    finally:
        httpd.server_close()
