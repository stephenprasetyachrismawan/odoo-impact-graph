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
    if build:
        graph['meta']['build'] = build  # lets `rebuild` and the MCP server redo the same build
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, 'w', encoding='utf-8') as fh:
        json.dump(graph, fh, separators=(',', ':'))
    print(f"graph: {len(graph['nodes'])} nodes, {len(graph['edges'])} edges -> {out}")
    if html:
        from .htmlexport import write_html
        print('html:', write_html(out, os.path.splitext(out)[0] + '.html'))


def cmd_build(args):
    from . import builder, static_facts
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
    for p in paths + (args.core or []):
        if not os.path.isdir(os.path.expanduser(p)):
            sys.exit(f'not a folder: {p}')
    wanted = [m.strip() for m in args.modules.split(',')] if args.modules else None
    facts = static_facts.build(paths, args.core or [], args.custom or [], wanted)
    if facts['unresolved_depends']:
        print(f"{len(facts['unresolved_depends'])} @api.depends paths could not be resolved "
              f"(module not scanned?), e.g. {facts['unresolved_depends'][0]}")
    build = {'cmd': 'build', 'paths': _abs(paths), 'core': _abs(args.core), 'custom': _abs(args.custom),
             'modules': args.modules, 'all': args.all}
    _save(builder.build_graph(facts, keep_all=args.all), args.output, not args.no_html, build)


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
    serve(args.graph)


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
    m.set_defaults(func=cmd_mcp)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == '__main__':
    main()
