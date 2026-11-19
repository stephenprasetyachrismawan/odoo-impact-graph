"""Local web service: projects, their branches, one-click graph generation, interactive diagram.

    odoo-impact serve            -> http://127.0.0.1:8770/

A branch is built from git directly (`git archive` of its commit: .py, .xml, .csv), so no checkout
is needed; "Working copy" builds the files on disk, uncommitted edits included.

State lives in ODOO_IMPACT_HOME (default ~/.local/share/odoo-impact):
    projects.json                     registered projects
    graphs/<project>/<sha>.json       graph of a commit (shared by every branch pointing at it)
    graphs/<project>/refs.json        branch -> last built commit
    graphs/<folder id>/graph.json     graph of a folder on disk (working copy, MCP --auto)
    trees/<project>/<sha>/            extracted sources of the built commits
    diagrams/<project>/<slug>.json    saved diagrams
"""
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import threading
import time
import traceback
import uuid
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__, gitinfo

HOME = os.path.expanduser(os.environ.get('ODOO_IMPACT_HOME', '~/.local/share/odoo-impact'))
STATIC = os.path.dirname(__file__)
SAFE = re.compile(r'^[A-Za-z0-9._-]+$')


def home(*parts):
    return os.path.join(HOME, *parts)


def graph_id(folder):
    return hashlib.sha1(os.path.realpath(folder).encode()).hexdigest()[:12]


def graph_path(folder):
    """Graph of a folder on disk (working copy of a repository, or any worktree used by MCP --auto)."""
    return home('graphs', graph_id(folder), 'graph.json')


def _read_json(path, default):
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, path)


def _git(repo, *args, binary=False):
    out = subprocess.run(['git', '-C', repo, *args], capture_output=True, timeout=120)
    if out.returncode != 0:
        raise ValueError(out.stderr.decode(errors='replace').strip() or f'git {args[0]} failed')
    return out.stdout if binary else out.stdout.decode(errors='replace')


# -- projects -------------------------------------------------------------------
_lock = threading.Lock()


def load_projects():
    return _read_json(home('projects.json'), {}).get('projects', [])


def save_projects(projects):
    _write_json(home('projects.json'), {'projects': projects})


def get_project(pid):
    return next((p for p in load_projects() if p['id'] == pid), None)


def add_project(repo, conf=None, core=(), modules=None, name=None):
    """Register a git repository of custom addons. conf: odoo.conf whose addons_path gives the core
    folders (folders inside the repository are replaced by the code being built)."""
    from .cli import addons_path_from_conf
    top = gitinfo.toplevel(repo)
    if not top:
        raise ValueError(f'not inside a git repository: {repo}')
    trees = gitinfo.worktrees(top)
    main = trees[0]['path'] if trees else top
    conf = os.path.abspath(os.path.expanduser(conf)) if conf else None
    if conf and not addons_path_from_conf(conf):
        raise ValueError(f'no addons_path in {conf}')
    core = [os.path.abspath(os.path.expanduser(c)) for c in core if c.strip()]
    for c in core:
        if not os.path.isdir(c):
            raise ValueError(f'not a folder: {c}')
    with _lock:
        pid = graph_id(main)
        projects = [p for p in load_projects() if p['id'] != pid]
        projects.append({'id': pid, 'name': name or os.path.basename(main), 'repo': main, 'conf': conf,
                         'core': core, 'modules': modules or None})
        save_projects(projects)
    return pid


def remove_project(pid):
    with _lock:
        save_projects([p for p in load_projects() if p['id'] != pid])


def project_of(folder):
    """Registered project whose repository holds this folder (any worktree of it), or None."""
    real = os.path.realpath(folder)
    for p in load_projects():
        if any(os.path.realpath(w['path']) == real for w in gitinfo.worktrees(p['repo'])):
            return p
    return None


def build_args(project, source):
    """paths / core / custom to build `source` (a worktree or an extracted commit) of `project`."""
    from .cli import addons_path_from_conf
    repo = os.path.realpath(project['repo'])
    src = os.path.realpath(source)
    paths, core = [src], list(project.get('core') or [])
    for p in addons_path_from_conf(project['conf']) if project.get('conf') else []:
        real = os.path.realpath(p)
        if real == repo or real.startswith(repo + os.sep):
            mapped = src + real[len(repo):]  # same folder inside the code being built
            if os.path.isdir(mapped) and mapped not in paths:
                paths.append(mapped)
        elif real not in core:
            core.append(real)
    return {'paths': paths, 'core': core, 'custom': [src], 'modules': project.get('modules')}


# -- builds -------------------------------------------------------------------------
def build_worktree(folder, log=print):
    """Graph of the files on disk of a worktree (uncommitted edits included)."""
    project = project_of(folder)
    if project is None:
        raise ValueError(f'{folder} does not belong to a registered project: add its repository in the '
                         f'dashboard or with `odoo-impact project add <repo>`')
    from .buildrun import build_static
    args = build_args(project, folder)
    log(f"building working copy {folder} ({project['name']})")
    return build_static(args['paths'], args['core'], args['custom'], args['modules'],
                        output=graph_path(folder), html=False, log=log)


def resolve_ref(project, ref):
    return _git(project['repo'], 'rev-parse', '--verify', ref + '^{commit}').strip()


def extract(project, sha):
    """Sources of a commit (.py, .xml, .csv) in trees/<project>/<sha>/, extracted once."""
    dest = home('trees', project['id'], sha)
    if os.path.isdir(dest):
        return dest
    names = _git(project['repo'], 'ls-tree', '-r', '--name-only', sha).splitlines()
    specs = [f':(glob)**/*{ext}' for ext in ('.py', '.xml', '.csv') if any(n.endswith(ext) for n in names)]
    if not specs:
        raise ValueError(f'no Python / XML file in {sha[:8]}')
    data = _git(project['repo'], 'archive', '--format=tar', sha, '--', *specs, binary=True)
    tmp = dest + '.part'
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        members = [m for m in tar.getmembers()
                   if (m.isfile() or m.isdir()) and not m.name.startswith('/') and '..' not in m.name.split('/')]
        if hasattr(tarfile, 'data_filter'):
            tar.extractall(tmp, members=members, filter='data')  # refuses links / absolute paths
        else:
            tar.extractall(tmp, members=members)
    os.replace(tmp, dest)
    return dest


def ref_graph_path(project, sha):
    return home('graphs', project['id'], f'{sha}.json')


def build_ref(project, ref, log=print):
    from .buildrun import build_static
    sha = resolve_ref(project, ref)
    out = ref_graph_path(project, sha)
    if not os.path.exists(out):
        log(f"extracting {ref} ({sha[:8]}) from git")
        src = extract(project, sha)
        args = build_args(project, src)
        build_static(args['paths'], args['core'], args['custom'], args['modules'], output=out, html=False, log=log)
        meta_fix = _read_json(out, None)
        if meta_fix:  # the graph of a commit is not a folder: record the commit itself
            subject = _git(project['repo'], 'log', '-1', '--format=%s', sha).strip()
            meta_fix['meta']['git'] = {'head': sha, 'branch': ref, 'dirty': False, 'subject': subject}
            meta_fix['meta']['source_tree'] = src
            _write_json(out, meta_fix)
    else:
        log(f'{ref} ({sha[:8]}) already built')
    with _lock:
        refs = _read_json(home('graphs', project['id'], 'refs.json'), {})
        refs[ref] = sha
        _write_json(home('graphs', project['id'], 'refs.json'), refs)
    _cleanup(project)
    return out


def _cleanup(project):
    """Drop graphs / trees of commits no branch points at any more (keeps the disk small)."""
    keep = set(_read_json(home('graphs', project['id'], 'refs.json'), {}).values())
    for folder, suffix in ((home('graphs', project['id']), '.json'), (home('trees', project['id']), '')):
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            sha = name[:-len(suffix)] if suffix and name.endswith(suffix) else name
            if re.fullmatch(r'[0-9a-f]{40}', sha) and sha not in keep:
                path = os.path.join(folder, name)
                shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) else os.remove(path)


def graph_file(project, key):
    """Graph file of `key`: 'wc' (working copy) or a commit sha."""
    if key == 'wc':
        return graph_path(project['repo'])
    if re.fullmatch(r'[0-9a-f]{40}', key or ''):
        return ref_graph_path(project, key)
    raise ValueError('bad graph key')


# -- jobs -------------------------------------------------------------------------
_jobs = {}
_build_slot = threading.Semaphore(1)  # one build at a time: a big code base uses ~1 GB while building


def start_job(key, label, fn):
    for job in _jobs.values():
        if job['key'] == key and job['state'] in ('queued', 'running'):
            return job['id']
    jid = uuid.uuid4().hex[:10]
    job = {'id': jid, 'key': key, 'label': label, 'state': 'queued', 'log': [], 'started': time.time()}
    _jobs[jid] = job

    def run():
        with _build_slot:
            job['state'] = 'running'
            try:
                fn(job['log'].append)
                job['state'] = 'done'
            except Exception as exc:  # noqa: BLE001 -- shown in the dashboard
                job['log'].append(f'{type(exc).__name__}: {exc}')
                traceback.print_exc()
                job['state'] = 'failed'
            job['ended'] = time.time()

    threading.Thread(target=run, daemon=True).start()
    return jid


def start_build(folder):
    """Build a folder on disk (kept for MCP / CLI callers)."""
    return start_job(f'wt:{graph_id(folder)}', folder, lambda log: build_worktree(folder, log))


def _job_for(key):
    jobs = [j for j in _jobs.values() if j['key'] == key]
    job = max(jobs, key=lambda j: j['started']) if jobs else None
    return job and {'id': job['id'], 'state': job['state']}


# -- views of the data ---------------------------------------------------------------
def graph_meta(path):
    data = _read_json(path, None)
    return (data or {}).get('meta') if data else None


def branches(project, remote=True):
    fmt = '%(refname)|%(refname:short)|%(objectname)|%(committerdate:unix)|%(subject)'
    refs = ['refs/heads'] + (['refs/remotes'] if remote else [])
    raw = _git(project['repo'], 'for-each-ref', '--sort=-committerdate', f'--format={fmt}', *refs)
    built = _read_json(home('graphs', project['id'], 'refs.json'), {})
    out = []
    for line in raw.splitlines():
        full, short, sha, when, subject = (line.split('|', 4) + [''] * 5)[:5]
        if full.endswith('/HEAD'):
            continue
        has = os.path.exists(ref_graph_path(project, sha))
        last = built.get(short)
        out.append({
            'ref': short, 'remote': full.startswith('refs/remotes/'), 'sha': sha, 'date': int(when or 0),
            'subject': subject, 'graph': 'up to date' if has else ('outdated' if last else 'none'),
            'key': sha if has else (last if last and os.path.exists(ref_graph_path(project, last)) else None),
            'job': _job_for(f"ref:{project['id']}:{short}"),
        })
    return out


def working_copy(project):
    repo = project['repo']
    cur = gitinfo.state(repo) or {}
    meta = graph_meta(graph_path(repo))
    built = (meta or {}).get('git') or {}
    status = 'none'
    if meta:
        status = 'up to date'
        if built.get('head') != cur.get('head') or cur.get('dirty') or built.get('dirty'):
            status = 'outdated'
    return {'path': repo, 'branch': cur.get('branch') or '(detached)', 'head': cur.get('head', ''),
            'subject': cur.get('subject', ''), 'dirty': cur.get('dirty', False), 'graph': status,
            'built_at': (meta or {}).get('built_at'), 'key': 'wc' if meta else None,
            'job': _job_for(f"wc:{project['id']}")}


_graph_cache = OrderedDict()


def load_graph(project, key):
    """query.Graph of a build, cached (3 most recent)."""
    from .query import Graph
    path = graph_file(project, key)
    if not os.path.exists(path):
        raise KeyError('no graph: press Generate first')
    stamp = (path, os.path.getmtime(path))
    if stamp in _graph_cache:
        _graph_cache.move_to_end(stamp)
        return _graph_cache[stamp]
    g = Graph.load(path)
    with open(path, encoding='utf-8') as fh:
        g.catalog = json.load(fh).get('catalog') or {}
    _graph_cache[stamp] = g
    while len(_graph_cache) > 3:
        _graph_cache.popitem(last=False)
    return g


def diff_lines(project, key, base):
    """{absolute file in the built sources: {lines}} for the changes base..key."""
    if key == 'wc':
        from .query import changed_lines
        return changed_lines(project['repo'], base)
    meta = graph_meta(graph_file(project, key)) or {}
    root = meta.get('source_tree') or extract(project, key)
    raw = _git(project['repo'], 'diff', '--unified=0', f'{base}...{key}', '--', '*.py')
    lines, current = {}, None
    for row in raw.splitlines():
        if row.startswith('+++ '):
            current = os.path.realpath(os.path.join(root, row[6:])) if row.startswith('+++ b/') else None
        m = re.match(r'@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@', row)
        if m and current:
            start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
            lines.setdefault(current, set()).update(range(start, start + max(count, 1)))
    return lines


def subgraph(g, reached, extra=()):
    """Compact JSON of a traversal for the diagram: nodes + the edge that reached each."""
    nodes, edges = {}, []
    for nid, (dist, e) in reached.items():
        n = g.nodes[nid]
        nodes[nid] = {k: n.get(k) for k in ('id', 'type', 'lvl', 'model', 'name', 'label', 'ftype', 'files', 'custom')}
        nodes[nid]['dist'] = dist
        if e is not None:
            edges.append({k: e.get(k) for k in ('s', 't', 'k', 'src', 'via', 'fn', 'at')})
    for nid in extra:
        if nid in g.nodes and nid not in nodes:
            n = g.nodes[nid]
            nodes[nid] = {k: n.get(k) for k in ('id', 'type', 'lvl', 'model', 'name', 'label', 'files', 'custom')}
    return {'nodes': list(nodes.values()), 'edges': edges}


# -- diagrams ----------------------------------------------------------------------
def diagram_dir(pid):
    return home('diagrams', pid)


def list_diagrams(pid):
    folder = diagram_dir(pid)
    out = []
    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        if name.endswith('.json'):
            d = _read_json(os.path.join(folder, name), {})
            out.append({'slug': name[:-5], 'name': d.get('name') or name[:-5], 'updated': d.get('updated'),
                        'nodes': len(d.get('nodes') or [])})
    return sorted(out, key=lambda d: -(d['updated'] or 0))


def slugify(name):
    slug = re.sub(r'[^A-Za-z0-9]+', '-', name.strip()).strip('-').lower()[:60]
    return slug or 'diagram'


# -- http -----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = f'odoo-impact/{__version__}'

    def log_message(self, *a):
        pass

    def _host_ok(self):
        # refuse DNS rebinding: only answer to the loopback names we listen on
        host = (self.headers.get('Host') or '').rsplit(':', 1)[0].strip('[]')
        return host in ('127.0.0.1', 'localhost', '::1', self.server.server_address[0])

    def _send(self, code, body, ctype='application/json'):
        if isinstance(body, bytes):
            data = body
        else:
            data = (json.dumps(body, separators=(',', ':')) if ctype == 'application/json' else body).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype + '; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(data)

    def _page(self, name):
        with open(os.path.join(STATIC, name), encoding='utf-8') as fh:
            return self._send(200, fh.read(), 'text/html')

    def _project(self, pid):
        p = get_project(pid)
        if not p:
            raise KeyError('unknown project')
        return p

    def do_GET(self):  # noqa: C901 -- one flat router
        if not self._host_ok():
            return self._send(403, {'error': 'bad host'})
        url = urlparse(self.path)
        parts = [unquote(x) for x in url.path.strip('/').split('/') if x]
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if not parts:
                return self._page('dashboard.html')
            if parts == ['diagram']:
                return self._page('diagram.html')
            if parts == ['api', 'projects']:
                return self._send(200, {'projects': [{**p, 'working_copy': working_copy(p)} for p in load_projects()],
                                        'home': HOME})
            if parts[:2] == ['api', 'branches'] and len(parts) == 3:
                p = self._project(parts[2])
                return self._send(200, {'branches': branches(p, q.get('remote', '1') == '1')})
            if parts[:2] == ['api', 'jobs'] and len(parts) == 3:
                job = _jobs.get(parts[2])
                return self._send(200, job) if job else self._send(404, {'error': 'no such job'})
            if parts[:2] == ['api', 'catalog'] and len(parts) == 4:
                p = self._project(parts[2])
                g = load_graph(p, parts[3])
                return self._send(200, {'catalog': g.catalog, 'meta': g.meta, 'project': p})
            if parts[:2] == ['api', 'query'] and len(parts) == 4:
                p = self._project(parts[2])
                g = load_graph(p, parts[3])
                kind, ref, depth = q.get('kind', 'impact'), q.get('ref', ''), int(q.get('depth', 4))
                if kind == 'impact':
                    r = g.impact(ref, depth)
                elif kind == 'who':
                    r = g.who(ref, depth)
                else:
                    r = g.reach(ref, depth)
                from . import query
                return self._send(200, {**subgraph(g, r['reached'], r['ui']), 'root': r['root'],
                                        'text': query.report(g, {'impact': 'impact', 'who': 'who'}.get(kind, 'reach'), r)})
            if parts[:2] == ['api', 'node'] and len(parts) == 4:
                from . import query
                g = load_graph(self._project(parts[2]), parts[3])
                return self._send(200, {'text': query.node_text(g, q.get('id', ''))})
            if parts[:2] == ['api', 'diff'] and len(parts) == 4:
                from . import query
                p = self._project(parts[2])
                g = load_graph(p, parts[3])
                base = q.get('base') or 'HEAD'
                r = g.diff(p['repo'], base, lines=diff_lines(p, parts[3], base))
                return self._send(200, {'base': base, 'text': query.report(g, 'diff', r),
                                        **subgraph(g, r['reached'], r['ui']), 'touched': sorted(r['touched'])})
            if parts[:2] == ['api', 'diagrams'] and len(parts) == 3:
                return self._send(200, {'diagrams': list_diagrams(parts[2])})
            if parts[:2] == ['api', 'diagrams'] and len(parts) == 4 and SAFE.match(parts[2]) and SAFE.match(parts[3]):
                d = _read_json(os.path.join(diagram_dir(parts[2]), parts[3] + '.json'), None)
                return self._send(200, d) if d else self._send(404, {'error': 'no such diagram'})
        except KeyError as exc:
            return self._send(400, {'error': str(exc.args[0])})
        except ValueError as exc:
            return self._send(400, {'error': str(exc)})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._send(500, {'error': f'{type(exc).__name__}: {exc}'})
        return self._send(404, {'error': 'not found'})

    def do_POST(self):
        # a custom header cannot be sent cross-site without a CORS preflight we never allow
        if not self._host_ok() or self.headers.get('X-Odoo-Impact') != '1':
            return self._send(403, {'error': 'forbidden'})
        length = int(self.headers.get('Content-Length') or 0)
        if length > 20_000_000:
            return self._send(413, {'error': 'too large'})
        try:
            body = json.loads(self.rfile.read(length) or b'{}')
        except ValueError:
            return self._send(400, {'error': 'bad json'})
        parts = [unquote(x) for x in urlparse(self.path).path.strip('/').split('/') if x]
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
                p = self._project(body.get('project', ''))
                if body.get('ref') == '__wc__':
                    jid = start_job(f"wc:{p['id']}", 'working copy', lambda log: build_worktree(p['repo'], log))
                else:
                    ref = body.get('ref', '')
                    resolve_ref(p, ref)  # fail fast on an unknown branch
                    jid = start_job(f"ref:{p['id']}:{ref}", ref, lambda log: build_ref(p, ref, log))
                return self._send(200, {'job': jid})
            if parts[:2] == ['api', 'diagrams'] and len(parts) == 3 and SAFE.match(parts[2]):
                self._project(parts[2])
                name = str(body.get('name') or 'Untitled')[:120]
                slug = body.get('slug') if SAFE.match(str(body.get('slug') or '')) else slugify(name)
                doc = {'name': name, 'lens': body.get('lens'), 'nodes': body.get('nodes') or [],
                       'view': body.get('view'), 'updated': time.time()}
                _write_json(os.path.join(diagram_dir(parts[2]), slug + '.json'), doc)
                return self._send(200, {'slug': slug})
            if parts[:2] == ['api', 'diagrams'] and len(parts) == 5 and parts[4] == 'delete' \
                    and SAFE.match(parts[2]) and SAFE.match(parts[3]):
                path = os.path.join(diagram_dir(parts[2]), parts[3] + '.json')
                if os.path.exists(path):
                    os.remove(path)
                return self._send(200, {'ok': True})
        except KeyError as exc:
            return self._send(400, {'error': str(exc.args[0])})
        except ValueError as exc:
            return self._send(400, {'error': str(exc)})
        return self._send(404, {'error': 'not found'})


def overview():
    """CLI `project list`: projects, working copy and their built branches."""
    out = []
    for p in load_projects():
        built = _read_json(home('graphs', p['id'], 'refs.json'), {})
        out.append({**p, 'working_copy': working_copy(p), 'built_refs': built})
    return out


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
