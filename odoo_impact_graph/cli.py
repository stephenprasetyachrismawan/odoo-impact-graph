"""odoo-impact command line."""
import argparse
import json
import os
import subprocess
import sys
import tempfile

from . import __version__

DEFAULT_GRAPH = os.environ.get('ODOO_IMPACT_GRAPH', os.path.join('.odoo-impact', 'graph.json'))


def _abs(paths):
    return [os.path.abspath(os.path.expanduser(p)) for p in paths or []]


def addons_path_from_conf(conf):
    """addons_path of an odoo.conf / odoo.cfg ([options] section)."""
    import configparser
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(conf, encoding='utf-8')
    raw = parser.get('options', 'addons_path', fallback='')
    base = os.path.dirname(os.path.abspath(conf))
    return [os.path.normpath(os.path.join(base, p.strip())) for p in raw.split(',') if p.strip()]


def _save(graph, out, html, build=None):
    from .buildrun import save
    save(graph, out, html, build)


def cmd_build(args):
    from .buildrun import build_static
    paths = list(args.paths or [])
    if getattr(args, 'from_conf', None):
        conf_paths = addons_path_from_conf(args.from_conf)
        if not conf_paths:
            sys.exit(f'no addons_path in {args.from_conf}')
        print('addons_path from', args.from_conf, ':', ', '.join(conf_paths))
        paths += conf_paths
    if not paths:
        answer = input('Odoo folder to analyse (custom addons, project or Odoo source): ').strip()
        if not answer:
            sys.exit('nothing to analyse')
        paths = [answer]
    try:
        build_static(paths, args.core, args.custom, args.modules, args.all, args.output, not args.no_html)
    except ValueError as exc:
        sys.exit(str(exc))


def cmd_build_registry(args):
    from . import builder, static_facts
    script = os.path.join(os.path.dirname(__file__), 'registry_dump.py')
    with tempfile.TemporaryDirectory() as tmp:
        facts_path = os.path.join(tmp, 'facts.json')
        cmd = [args.python, args.odoo_bin, 'shell', '--no-http', '--log-level=critical', '-d', args.database]
        if args.config:
            cmd.append(f'--config={args.config}')
        if args.addons_path:
            cmd.append(f'--addons-path={args.addons_path}')
        env = dict(os.environ, OIG_OUT=facts_path, OIG_CUSTOM=os.pathsep.join(os.path.realpath(p) for p in args.custom or []))
        print('running:', ' '.join(cmd), '< registry_dump.py')
        with open(script, encoding='utf-8') as stdin:
            proc = subprocess.run(cmd, stdin=stdin, env=env, capture_output=True, text=True)
        if not os.path.exists(facts_path):
            sys.exit('odoo shell failed:\n' + proc.stdout[-3000:] + proc.stderr[-3000:])
        with open(facts_path, encoding='utf-8') as fh:
            facts = json.load(fh)
    print(f"registry of {facts['db']} (Odoo {facts.get('odoo_version')}): {len(facts['models'])} models")
    static_facts.overlay_source(facts)
    build = {'cmd': 'build-registry', 'odoo_bin': os.path.abspath(args.odoo_bin), 'python': args.python,
             'config': args.config and os.path.abspath(args.config), 'database': args.database,
             'addons_path': args.addons_path, 'custom': _abs(args.custom), 'all': args.all}
    _save(builder.build_graph(facts, keep_all=args.all), args.output, not args.no_html, build)


def rebuild(graph_path, html=True):
    """Redo the build recorded in the graph (after code changes / a branch switch)."""
    with open(graph_path, encoding='utf-8') as fh:
        build = json.load(fh).get('meta', {}).get('build')
    if not build:
        raise ValueError(f'{graph_path} has no recorded build, run `odoo-impact build ...` once more')
    common = dict(output=os.path.abspath(graph_path), no_html=not html, all=build.get('all', False),
                  custom=build.get('custom'))
    if build['cmd'] == 'build':
        cmd_build(argparse.Namespace(paths=build['paths'], core=build.get('core'), modules=build.get('modules'),
                                     from_conf=None, **common))
    else:
        cmd_build_registry(argparse.Namespace(odoo_bin=build['odoo_bin'], python=build['python'],
                                              config=build.get('config'), database=build['database'],
                                              addons_path=build.get('addons_path'), **common))


def cmd_rebuild(args):
    try:
        rebuild(args.graph, html=not args.no_html)
    except (ValueError, FileNotFoundError) as exc:
        sys.exit(str(exc))


def cmd_web(args):
    """Serve the HTML explorer on localhost and open it in the browser."""
    import http.server
    import shutil
    import socketserver
    import webbrowser
    from .htmlexport import write_html
    if not os.path.exists(args.graph):
        sys.exit(f'{args.graph} not found: run `odoo-impact build <odoo folder>` first (or pass -g)')
    html = os.path.splitext(os.path.abspath(args.graph))[0] + '.html'
    if not os.path.exists(html) or os.path.getmtime(html) < os.path.getmtime(args.graph):
        write_html(args.graph, html)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=os.path.dirname(html), **kw)

        def do_GET(self):
            if self.path.split('?')[0] in ('/', '/index.html'):
                self.path = '/' + os.path.basename(html)
            if self.path.split('?')[0] != '/' + os.path.basename(html):
                self.send_error(404)
                return
            super().do_GET()

        def log_message(self, *a):
            pass

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer((args.host, args.port), Handler) as httpd:
        url = f'http://{args.host}:{httpd.server_address[1]}/'
        print(f'Odoo impact explorer: {url}   (Ctrl+C to stop)', flush=True)
        if not args.no_open:
            opened = False
            for opener in ('wslview', 'xdg-open', 'open'):
                if shutil.which(opener) and subprocess.run([opener, url], capture_output=True).returncode == 0:
                    opened = True
                    break
            if not opened:
                webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print('stopped')


def _graph(args):
    from .query import Graph
    if not os.path.exists(args.graph):
        sys.exit(f'{args.graph} not found: run `odoo-impact build <odoo folder>` first (or pass -g)')
    return Graph.load(args.graph)


def cmd_query(args):
    from . import query
    g = _graph(args)
    try:
        if args.cmd == 'find':
            for n in g.find(' '.join(args.text), limit=args.limit):
                print(f"{n['id']:<70} {query.label(n, short=True)}")
            return
        if args.cmd == 'node':
            print(query.node_text(g, args.ref))
            return
        if args.cmd == 'field':
            print(query.report(g, 'impact', g.impact(args.ref, args.depth, args.core_logic), args.limit))
        elif args.cmd == 'who':
            print(query.report(g, 'who', g.who(args.ref, args.depth, args.core_logic), args.limit))
        elif args.cmd == 'from':
            print(query.report(g, 'reach', g.reach(args.ref, args.depth, args.core_logic), args.limit))
        elif args.cmd == 'plan':
            print(query.report(g, 'plan', g.plan(args.refs, args.depth, args.core_logic), args.limit))
        elif args.cmd == 'diff':
            print(query.report(g, 'diff', g.diff(args.repo, args.base, args.depth, args.core_logic), args.limit))
    except KeyError as exc:
        sys.exit(str(exc.args[0]))
    print('\n(~ = read from Python method bodies, heuristic; other edges are declarations'
          f"{' / registry' if g.meta.get('source') == 'registry' else ''})")


def cmd_html(args):
    from .htmlexport import write_html
    print(write_html(args.graph, args.output or os.path.splitext(args.graph)[0] + '.html'))


def cmd_mcp(args):
    from .mcp_server import serve
    if args.auto:
        from . import gitinfo, service
        # outside git or an unregistered repo the server still starts; its tools explain what to do
        top = gitinfo.toplevel(os.getcwd()) or os.getcwd()
        serve(service.graph_path(top), worktree=top)
    else:
        serve(args.graph)


def cmd_serve(args):
    from . import service
    service.serve(args.host, args.port, args.open)


def cmd_project(args):
    from . import service
    if args.action == 'add':
        try:
            pid = service.add_project(args.repo, args.conf, args.core or [], args.modules, args.name)
        except ValueError as exc:
            sys.exit(str(exc))
        print('project', pid, 'saved in', service.home('projects.json'))
    elif args.action == 'list':
        for p in service.overview():
            wc = p['working_copy']
            print(f"{p['name']}  {p['repo']}  (id {p['id']})")
            print(f"   working copy [{wc['branch']}]: {wc['graph']}")
            for ref, sha in sorted(p['built_refs'].items()):
                print(f"   {ref:<55} built at {sha[:8]}")
    elif args.action == 'build':
        from . import gitinfo
        try:
            if args.ref:
                project = service.project_of(gitinfo.toplevel(args.repo) or args.repo)
                if not project:
                    sys.exit(f'{args.repo} is not in a registered project')
                service.build_ref(project, args.ref)
            else:
                service.build_worktree(gitinfo.toplevel(args.repo) or args.repo)
        except ValueError as exc:
            sys.exit(str(exc))


SYSTEMD_UNIT = """[Unit]
Description=odoo-impact service (impact graphs of Odoo worktrees)

[Service]
ExecStart={exe} serve --host 127.0.0.1 --port {port}
Restart=on-failure
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
"""


def cmd_service(args):
    """Install / remove a systemd --user unit running `odoo-impact serve`."""
    import shutil
    unit_dir = os.path.expanduser('~/.config/systemd/user')
    unit = os.path.join(unit_dir, 'odoo-impact.service')
    if args.action == 'install':
        exe = shutil.which('odoo-impact') or os.path.join(os.path.dirname(sys.executable), 'odoo-impact')
        os.makedirs(unit_dir, exist_ok=True)
        with open(unit, 'w', encoding='utf-8') as fh:
            fh.write(SYSTEMD_UNIT.format(exe=os.path.abspath(exe), port=args.port))
        for cmd in (['daemon-reload'], ['enable', '--now', 'odoo-impact.service'], ['restart', 'odoo-impact.service']):
            subprocess.run(['systemctl', '--user', *cmd], check=True)
        print(f'installed {unit}\nrunning on http://127.0.0.1:{args.port}/  (systemctl --user status odoo-impact)')
    elif args.action == 'uninstall':
        subprocess.run(['systemctl', '--user', 'disable', '--now', 'odoo-impact.service'])
        if os.path.exists(unit):
            os.remove(unit)
        subprocess.run(['systemctl', '--user', 'daemon-reload'])
        print('removed', unit)
    else:
        subprocess.run(['systemctl', '--user', 'status', 'odoo-impact.service', '--no-pager'])


def main(argv=None):
    p = argparse.ArgumentParser(prog='odoo-impact', description=__doc__)
    p.add_argument('--version', action='version', version=__version__)
    sub = p.add_subparsers(dest='cmd', required=True)

    b = sub.add_parser('build', help='analyse Odoo source folders (no database needed)')
    b.add_argument('paths', nargs='*', help='custom addons / project folder / Odoo source tree (asked if empty)')
    b.add_argument('--from-conf', metavar='ODOO_CONF', help='take the folders from the addons_path of an odoo.conf')
    b.add_argument('--core', action='append', help='extra addons folders used to resolve fields (odoo, enterprise, OCA); repeatable')
    b.add_argument('--custom', action='append', help='only modules below these folders count as custom; repeatable')
    b.add_argument('--modules', help='comma-separated modules to treat as installed (default: all custom ones)')
    b.add_argument('--all', action='store_true', help='keep the whole graph, not only what touches custom code')
    b.add_argument('-o', '--output', default=DEFAULT_GRAPH)
    b.add_argument('--no-html', action='store_true')
    b.set_defaults(func=cmd_build)

    r = sub.add_parser('build-registry', help='read the graph from a running database registry (most exact)')
    r.add_argument('--odoo-bin', required=True)
    r.add_argument('--python', default=sys.executable, help='python of the Odoo virtualenv')
    r.add_argument('-c', '--config')
    r.add_argument('-d', '--database', required=True)
    r.add_argument('--addons-path')
    r.add_argument('--custom', action='append', help='folders whose modules count as custom; repeatable')
    r.add_argument('--all', action='store_true')
    r.add_argument('-o', '--output', default=DEFAULT_GRAPH)
    r.add_argument('--no-html', action='store_true')
    r.set_defaults(func=cmd_build_registry)

    def q(name, help_, **extra):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument('-g', '--graph', default=DEFAULT_GRAPH)
        sp.add_argument('--depth', type=int, default=6)
        sp.add_argument('--limit', type=int, default=40)
        sp.add_argument('--core-logic', action='store_true', help='also show core (non-custom) methods')
        sp.set_defaults(func=cmd_query)
        return sp

    q('find', 'search nodes').add_argument('text', nargs='+')
    q('node', 'details and direct edges of one node').add_argument('ref')
    q('field', 'if this changes: what follows and where the user sees it').add_argument('ref')
    q('who', 'what can change this field (methods, buttons, menus, crons)').add_argument('ref')
    q('from', 'what a menu / action / button / method can change').add_argument('ref')
    q('plan', 'impact of touching several fields/methods (before coding)').add_argument('refs', nargs='+')
    d = q('diff', 'impact of the changed Python lines in a git repo')
    d.add_argument('base', nargs='?', default='HEAD', help='git base (default HEAD = uncommitted changes)')
    d.add_argument('--repo', default='.')

    rb = sub.add_parser('rebuild', help='redo the build recorded in the graph (after code changes)')
    rb.add_argument('-g', '--graph', default=DEFAULT_GRAPH)
    rb.add_argument('--no-html', action='store_true')
    rb.set_defaults(func=cmd_rebuild)

    w = sub.add_parser('web', help='open the HTML explorer in the browser (local web server)')
    w.add_argument('-g', '--graph', default=DEFAULT_GRAPH)
    w.add_argument('--host', default='127.0.0.1')
    w.add_argument('--port', type=int, default=8765, help='0 = any free port')
    w.add_argument('--no-open', action='store_true', help='only print the URL')
    w.set_defaults(func=cmd_web)

    h = sub.add_parser('html', help='write the interactive HTML explorer')
    h.add_argument('-g', '--graph', default=DEFAULT_GRAPH)
    h.add_argument('-o', '--output')
    h.set_defaults(func=cmd_html)

    m = sub.add_parser('mcp', help='serve the graph to AI agents over MCP (stdio)')
    m.add_argument('-g', '--graph', default=DEFAULT_GRAPH)
    m.add_argument('--auto', action='store_true',
                   help='use the service graph of the git worktree of the current folder (see `serve`)')
    m.set_defaults(func=cmd_mcp)

    sv = sub.add_parser('serve', help='local web service: projects, worktrees, Generate, explorer, changes')
    sv.add_argument('--host', default='127.0.0.1')
    sv.add_argument('--port', type=int, default=8770)
    sv.add_argument('--open', action='store_true', help='open the browser')
    sv.set_defaults(func=cmd_serve)

    pj = sub.add_parser('project', help='manage the projects of the service without the browser')
    pj.add_argument('action', choices=['add', 'list', 'build'])
    pj.add_argument('repo', nargs='?', default='.', help='git repository / worktree folder')
    pj.add_argument('--conf', help='odoo.conf whose addons_path gives the core folders')
    pj.add_argument('--core', action='append', help='core folder (Odoo, Enterprise, OCA); repeatable')
    pj.add_argument('--modules')
    pj.add_argument('--name')
    pj.add_argument('--ref', help='build: a branch / commit instead of the files on disk')
    pj.set_defaults(func=cmd_project)

    sy = sub.add_parser('service', help='run `serve` as a systemd --user service (Linux / WSL with systemd)')
    sy.add_argument('action', choices=['install', 'uninstall', 'status'])
    sy.add_argument('--port', type=int, default=8770)
    sy.set_defaults(func=cmd_service)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == '__main__':
    main()
