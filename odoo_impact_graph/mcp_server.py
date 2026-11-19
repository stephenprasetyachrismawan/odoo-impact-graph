"""Model Context Protocol server (stdio, JSON-RPC 2.0, no dependencies).

Lets an AI agent ask the impact graph before it changes Odoo code:
    odoo-impact mcp -g .odoo-impact/graph.json
"""
import json
import os
import sys
import traceback

from . import __version__, query

INSTRUCTIONS = """Impact graph of an Odoo code base, in levels:
L0 field -> L1 logic (compute, constraint, onchange, write/create hook, method) -> L2 model/wizard
-> L3 view/button/report -> L4 menu/action/cron.
Before changing Odoo code: call impact_plan_change with every field and method you intend to touch,
read which fields recompute, which hooks/constraints run, which code already writes those fields,
and which screens must be tested. After editing, call impact_rebuild then impact_diff to check the real change.
Use impact_find to get exact ids (fields are 'model.field', methods 'model::method').
Edges marked ~ were read from Python method bodies and are heuristic: open the file:line to confirm."""

DEPTH = {'type': 'integer', 'description': 'how many hops to follow (default 6)', 'default': 6}
CORE = {'type': 'boolean', 'description': 'also follow methods of core (non-custom) modules', 'default': False}
TOOLS = [
    {'name': 'impact_find', 'description': 'Search fields, methods, views, buttons, actions, menus by words. Returns ids to use in other tools.',
     'inputSchema': {'type': 'object', 'properties': {
         'query': {'type': 'string', 'description': "words, e.g. 'sale.order amount' or 'Confirm'"},
         'limit': {'type': 'integer', 'default': 30}}, 'required': ['query']}},
    {'name': 'impact_of_change', 'description': 'If this field or method changes: which fields recompute, which constraints/onchanges/hooks run, and which views/actions/menus show the effect.',
     'inputSchema': {'type': 'object', 'properties': {
         'target': {'type': 'string', 'description': "field 'model.field' or method 'model::method'"},
         'depth': DEPTH, 'core_logic': CORE}, 'required': ['target']}},
    {'name': 'impact_who_changes', 'description': 'What can change this field: compute dependencies, methods writing it, buttons/menus/crons that lead there.',
     'inputSchema': {'type': 'object', 'properties': {
         'field': {'type': 'string', 'description': "'model.field'"}, 'depth': DEPTH, 'core_logic': CORE}, 'required': ['field']}},
    {'name': 'impact_from_entry', 'description': 'What a menu, action, button or method can change (all fields reachable from it).',
     'inputSchema': {'type': 'object', 'properties': {
         'target': {'type': 'string', 'description': 'node id, xmlid or label of a menu/action/button/method'},
         'depth': DEPTH, 'core_logic': CORE}, 'required': ['target']}},
    {'name': 'impact_plan_change', 'description': 'Use BEFORE writing code: combined impact of touching several fields/methods, existing writers to keep consistent, and screens to test.',
     'inputSchema': {'type': 'object', 'properties': {
         'targets': {'type': 'array', 'items': {'type': 'string'}, 'description': "ids like 'sale.order.state', 'sale.order::action_confirm'"},
         'depth': DEPTH, 'core_logic': CORE}, 'required': ['targets']}},
    {'name': 'impact_diff', 'description': 'Impact of the Python lines changed in a git repository (working tree vs base).',
     'inputSchema': {'type': 'object', 'properties': {
         'repo': {'type': 'string', 'description': 'path inside the git repository'},
         'base': {'type': 'string', 'description': "git base, e.g. 'HEAD' (uncommitted) or 'origin/main'", 'default': 'HEAD'},
         'depth': DEPTH, 'core_logic': CORE}, 'required': ['repo']}},
    {'name': 'impact_node', 'description': 'Details of one node (files, attributes) and its direct incoming/outgoing edges.',
     'inputSchema': {'type': 'object', 'properties': {'id': {'type': 'string'}}, 'required': ['id']}},
    {'name': 'impact_rebuild', 'description': 'Rebuild the graph from the code (same folders / database as the first build). Call after editing code or switching branch, before impact_diff.',
     'inputSchema': {'type': 'object', 'properties': {}}},
    {'name': 'impact_graph_info', 'description': 'Which code base / database the graph was built from, and its size.',
     'inputSchema': {'type': 'object', 'properties': {}}},
]


class Server:
    def __init__(self, graph_path):
        self.path = graph_path
        self.graph = None
        self.mtime = None

    def g(self):
        mtime = os.path.getmtime(self.path)  # reload when the graph is rebuilt
        if self.graph is None or mtime != self.mtime:
            self.graph = query.Graph.load(self.path)
            self.mtime = mtime
        return self.graph

    def call(self, name, a):
        g = self.g()
        depth, core = int(a.get('depth', 6)), bool(a.get('core_logic', False))
        if name == 'impact_find':
            hits = g.find(a['query'], limit=int(a.get('limit', 30)))
            return '\n'.join(f"{n['id']}  |  {query.label(n, short=True)}" for n in hits) or 'no match'
        if name == 'impact_of_change':
            return query.report(g, 'impact', g.impact(a['target'], depth, core))
        if name == 'impact_who_changes':
            return query.report(g, 'who', g.who(a['field'], depth, core))
        if name == 'impact_from_entry':
            return query.report(g, 'reach', g.reach(a['target'], depth, core))
        if name == 'impact_plan_change':
            return query.report(g, 'plan', g.plan(a['targets'], depth, core))
        if name == 'impact_diff':
            return query.report(g, 'diff', g.diff(a['repo'], a.get('base') or 'HEAD', depth, core))
        if name == 'impact_node':
            return query.node_text(g, a['id'])
        if name == 'impact_rebuild':
            import contextlib
            import io
            from .cli import rebuild
            log = io.StringIO()
            with contextlib.redirect_stdout(log):  # stdout is the JSON-RPC channel
                rebuild(self.path)
            self.graph = None
            return log.getvalue().strip()
        if name == 'impact_graph_info':
            m = g.meta
            return json.dumps({
                'graph': os.path.abspath(self.path), 'source': m.get('source'), 'db': m.get('db'),
                'odoo_version': m.get('odoo_version'), 'custom_modules': m.get('custom_modules'),
                'nodes': len(g.nodes), 'edges': len(g.edges), 'built_with': m.get('build'),
                'built_at': os.path.getmtime(self.path),
            }, indent=1)
        raise KeyError(f'unknown tool {name}')

    def handle(self, msg):
        method, mid = msg.get('method'), msg.get('id')
        if mid is None:
            return None  # notification
        if method == 'initialize':
            requested = (msg.get('params') or {}).get('protocolVersion') or '2025-06-18'
            result = {'protocolVersion': requested, 'capabilities': {'tools': {'listChanged': False}},
                      'serverInfo': {'name': 'odoo-impact-graph', 'version': __version__},
                      'instructions': INSTRUCTIONS}
        elif method == 'ping':
            result = {}
        elif method == 'tools/list':
            result = {'tools': TOOLS}
        elif method == 'tools/call':
            params = msg.get('params') or {}
            try:
                text, error = self.call(params.get('name'), params.get('arguments') or {}), False
            except KeyError as exc:
                text, error = str(exc.args[0]), True
            except Exception as exc:  # noqa: BLE001 -- report to the agent instead of dying
                traceback.print_exc(file=sys.stderr)
                text, error = f'{type(exc).__name__}: {exc}', True
            result = {'content': [{'type': 'text', 'text': text}], 'isError': error}
        else:
            return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32601, 'message': f'method not found: {method}'}}
        return {'jsonrpc': '2.0', 'id': mid, 'result': result}


def serve(graph_path, stdin=None, stdout=None):
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    if not os.path.exists(graph_path):
        print(f'odoo-impact mcp: {graph_path} not found, run `odoo-impact build` first', file=sys.stderr)
        sys.exit(1)
    server = Server(graph_path)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            reply = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'parse error'}}
        else:
            reply = server.handle(msg)
        if reply is not None:
            stdout.write(json.dumps(reply) + '\n')
            stdout.flush()
