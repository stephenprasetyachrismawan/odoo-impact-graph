"""Traversals over the impact graph, shared by the CLI, the MCP server and tests."""
import json
import os
import re
import subprocess
from collections import defaultdict, deque

# a change flows along these edges (A changes -> B recomputes / runs)
CHANGE = {'compute', 'related', 'constrains', 'onchange', 'write_hook', 'create_hook', 'copy_hook',
          'unlink_hook', 'triggers', 'writes', 'calls', 'computes', 'runs'}
# what can change a field, walked backwards
WHO = {'compute', 'related', 'writes', 'computes', 'calls', 'runs', 'has_button', 'opens', 'triggers'}
# an entry point (menu, action, button) reaches code along these, then CHANGE
ENTRY = {'opens', 'uses_view', 'has_button', 'model'} | CHANGE
LEVELS = {0: 'L0 Field', 1: 'L1 Logic', 2: 'L2 Model/Wizard', 3: 'L3 View/Button/Report', 4: 'L4 Menu/Action/Cron'}


class Graph:
    def __init__(self, data):
        self.meta = data.get('meta', {})
        self.nodes = {n['id']: n for n in data['nodes']}
        self.edges = data['edges']
        self.out = defaultdict(list)
        self.inc = defaultdict(list)
        for e in self.edges:
            self.out[e['s']].append(e)
            self.inc[e['t']].append(e)

    @classmethod
    def load(cls, path):
        with open(path, encoding='utf-8') as fh:
            return cls(json.load(fh))

    # -- lookup ------------------------------------------------------------
    def find(self, text, limit=30, types=None):
        words = text.lower().split()
        hits = []
        for n in self.nodes.values():
            if types and n['type'] not in types:
                continue
            hay = f"{n['id']} {n.get('label', '')} {n.get('xmlid', '')}".lower()
            if all(w in hay for w in words):
                exact = n['id'].lower() == text.lower() or n.get('name', '').lower() == text.lower()
                hits.append((not exact, not n.get('custom'), len(hay), n['id']))
        return [self.nodes[h[3]] for h in sorted(hits)[:limit]]

    def resolve(self, ref):
        """Node id from an id, 'model.field', 'model.method', a label or an xmlid."""
        if ref in self.nodes:
            return ref
        if '.' in ref:
            model, name = ref.rsplit('.', 1)
            if f'{model}::{name}' in self.nodes:
                return f'{model}::{name}'
        for prefix in ('view:', 'action:', 'menu:', 'cron:'):
            if prefix + ref in self.nodes:
                return prefix + ref
        exact = [n['id'] for n in self.nodes.values() if ref in (n.get('label'), n.get('xmlid'))]
        if len(exact) == 1:
            return exact[0]
        hits = self.find(ref, limit=2)
        if len(hits) == 1:
            return hits[0]['id']
        return None

    # -- traversal ---------------------------------------------------------
    def keep(self, core_logic):
        return lambda n: core_logic or n['lvl'] != 1 or n.get('custom') or n['type'] in ('compute', 'onchange', 'constraint')

    def bfs(self, starts, kinds, depth, reverse=False, core_logic=False, only_at=None):
        """{node: (distance, edge from parent)}; only_at limits the first hop of a start node
        to edges read from given definitions ('file:a-b')."""
        keep = self.keep(core_logic)
        adj = self.inc if reverse else self.out
        seen = {s: (0, None) for s in starts}
        queue = deque(starts)
        while queue:
            cur = queue.popleft()
            dist = seen[cur][0]
            if dist >= depth:
                continue
            for e in adj[cur]:
                nxt = e['s'] if reverse else e['t']
                if e['k'] not in kinds or nxt in seen or nxt not in self.nodes or not keep(self.nodes[nxt]):
                    continue
                if only_at and dist == 0 and cur in only_at and e.get('at') and e['at'] not in only_at[cur]:
                    continue
                seen[nxt] = (dist + 1, e)
                queue.append(nxt)
        return seen

    def exposure(self, field_ids):
        """Views showing the fields, actions using those views, menus opening those actions."""
        views = {e['s'] for f in field_ids for e in self.inc[f] if e['k'] == 'shows'}
        actions = {e['s'] for v in views for e in self.inc[v] if e['k'] == 'uses_view'}
        menus = {e['s'] for a in actions for e in self.inc[a]
                 if e['k'] == 'opens' and self.nodes[e['s']]['type'] == 'menu'}
        return views, actions, menus

    def entry_path(self, ids):
        """For buttons: their view, the actions opening the view, the menus opening the actions."""
        views = {self.nodes[b]['view'] for b in ids if self.nodes[b]['type'] == 'button' and self.nodes[b].get('view') in self.nodes}
        actions = {e['s'] for v in views for e in self.inc[v] if e['k'] == 'uses_view'}
        menus = {e['s'] for a in actions for e in self.inc[a]
                 if e['k'] == 'opens' and self.nodes[e['s']]['type'] == 'menu'}
        return views | actions | menus

    # -- questions ---------------------------------------------------------
    def impact(self, ref, depth=6, core_logic=False):
        nid = self._need(ref)
        reached = self.bfs([nid], CHANGE, depth, core_logic=core_logic)
        fields = [n for n in reached if self.nodes[n]['type'] == 'field']
        views, actions, menus = self.exposure(fields)
        return {'root': nid, 'reached': reached, 'ui': views | actions | menus, 'reverse': False}

    def who(self, ref, depth=6, core_logic=False):
        nid = self._need(ref)
        reached = self.bfs([nid], WHO, depth, reverse=True, core_logic=core_logic)
        entries = {n for n in reached if self.nodes[n]['lvl'] >= 3}
        return {'root': nid, 'reached': reached, 'ui': entries | self.entry_path(entries), 'reverse': True}

    def reach(self, ref, depth=6, core_logic=False):
        nid = self._need(ref)
        reached = self.bfs([nid], ENTRY, depth + 4, core_logic=core_logic)
        return {'root': nid, 'reached': reached, 'ui': set(), 'reverse': False}

    def plan(self, refs, depth=6, core_logic=False):
        """Impact of touching several fields / methods at once (before writing code)."""
        ids = [self._need(r) for r in refs]
        reached = self.bfs(ids, CHANGE, depth, core_logic=core_logic)
        fields = [n for n in reached if self.nodes[n]['type'] == 'field']
        views, actions, menus = self.exposure(fields)
        writers = set()
        for f in ids:
            if self.nodes[f]['type'] == 'field':
                writers |= {n for n in self.bfs([f], WHO, 3, reverse=True, core_logic=core_logic) if n != f}
        return {'roots': ids, 'reached': reached, 'ui': views | actions | menus, 'writers': writers}

    def diff(self, repo, base='HEAD', depth=6, core_logic=False):
        """Fields / methods whose source lines changed between base and the working tree."""
        lines = changed_lines(repo, base)
        touched, only_at = set(), defaultdict(set)
        for nid, n in self.nodes.items():
            for loc in n.get('files') or ():
                m = re.match(r'(.+):(\d+)-(\d+)$', loc)
                if not m:
                    continue
                path = os.path.realpath(m.group(1))
                if path in lines and lines[path] & set(range(int(m.group(2)), int(m.group(3)) + 1)):
                    touched.add(nid)
                    only_at[nid].add(loc)
        reached = self.bfs(sorted(touched), CHANGE, depth, core_logic=core_logic, only_at=only_at)
        fields = [n for n in reached if self.nodes[n]['type'] == 'field']
        views, actions, menus = self.exposure(fields)
        return {'touched': touched, 'only_at': only_at, 'reached': reached, 'ui': views | actions | menus,
                'files': sorted(lines)}

    def _need(self, ref):
        nid = self.resolve(ref)
        if nid is None:
            hint = ', '.join(n['id'] for n in self.find(ref, limit=8))
            raise KeyError(f"'{ref}' not found." + (f' Did you mean: {hint}' if hint else ''))
        return nid


def changed_lines(repo, base):
    """{absolute file: {line numbers}} for .py files changed since base (committed or not)."""
    top = subprocess.run(['git', '-C', repo, 'rev-parse', '--show-toplevel'],
                         capture_output=True, text=True, check=True).stdout.strip()
    diff = subprocess.run(['git', '-C', top, 'diff', '--unified=0', base, '--', '*.py'],
                          capture_output=True, text=True, check=True).stdout
    lines = defaultdict(set)
    current = None
    for row in diff.splitlines():
        if row.startswith('+++ '):
            current = os.path.realpath(os.path.join(top, row[6:])) if row.startswith('+++ b/') else None
        m = re.match(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', row)
        if m and current:
            start, count = int(m.group(3)), int(m.group(4) if m.group(4) is not None else 1)
            # a pure deletion (count 0) still touches the definition around it
            lines[current].update(range(start, start + max(count, 1)))
    return lines


# -- text output ------------------------------------------------------------
def label(n, short=False):
    t = n['type']
    if t == 'field':
        bits = [n.get('ftype') or '', 'stored' if n.get('stored') else 'not stored']
        if n.get('compute'):
            bits.append(f"compute={n['compute']}")
        if n.get('related'):
            bits.append(f"related={n['related']}")
        where = '' if short or not n.get('files') else f"  [{_short(n['files'][-1])}]"
        return f"{n['id']} \"{n.get('label', '')}\" ({', '.join(b for b in bits if b)}){where}"
    if n['lvl'] == 1:
        where = '' if short else ('  [' + ', '.join(_short(f) for f in n['files'][:3]) + ']' if n.get('files') else '  [core]')
        return f"{n['model']}.{n['name']}() <{t}>{where}"
    if t == 'view':
        return f"view \"{n.get('label')}\" ({n.get('vtype')}) {n.get('xmlid') or n['id']}"
    if t == 'button':
        return f"button \"{n.get('label')}\" -> {n['model']}.{n['name']}()"
    if t == 'menu':
        return f"menu {n.get('label')}"
    return f"{t} \"{n.get('label') or n.get('model')}\" {n.get('xmlid') or n['id']}"


def _short(loc):
    parts = loc.split('/')
    return '/'.join(parts[-3:]) if len(parts) > 3 else loc


def edge_text(e):
    txt = e['k']
    if e.get('via'):
        txt += f" via {e['via']}"
    if e.get('fn'):
        txt += f" ({e['fn']})"
    if e['src'] == 'ast':
        txt = '~' + txt + (f" @{_short(e['at'])}" if e.get('at') else '')
    return txt


def tree_text(g, roots, reached, reverse=False, max_lines=200):
    children = defaultdict(list)
    for nid, (_, e) in reached.items():
        if e is not None:
            children[e['t'] if reverse else e['s']].append((nid, e))
    lines = []

    def rec(nid, indent):
        for child, e in sorted(children[nid], key=lambda x: (g.nodes[x[0]]['lvl'], x[0])):
            if len(lines) >= max_lines:
                return
            arrow = '<-' if reverse else '->'
            lines.append(f"{'  ' * indent}{arrow} {edge_text(e)}: {label(g.nodes[child])}")
            rec(child, indent + 1)

    for r in roots:
        lines.append(label(g.nodes[r]))
        rec(r, 1)
    if len(lines) >= max_lines:
        lines.append(f'... cut at {max_lines} lines, lower the depth to see less')
    return '\n'.join(lines)


def levels_text(g, title, ids, limit=40):
    by = defaultdict(list)
    for nid in ids:
        by[g.nodes[nid]['lvl']].append(nid)
    out = [f'== {title} ({len(ids)}) ==']
    for lvl in sorted(by, reverse=True):
        items = sorted(by[lvl], key=lambda x: (g.nodes[x]['type'], g.nodes[x].get('label') or x))
        out.append(f'{LEVELS[lvl]} ({len(items)})')
        out.extend(f'  {label(g.nodes[n], short=True)}' for n in items[:limit])
        if len(items) > limit:
            out.append(f'  ... {len(items) - limit} more')
    return '\n'.join(out)


def report(g, kind, result, limit=40):
    """Plain-text answer for one question (used by CLI and MCP)."""
    if kind == 'impact':
        fields = [n for n in result['reached'] if g.nodes[n]['type'] == 'field']
        return '\n\n'.join([
            f"If {result['root']} changes:\n" + tree_text(g, [result['root']], result['reached']),
            levels_text(g, f'Where the user sees the effect ({len(fields)} fields)', result['ui'], limit),
        ])
    if kind == 'who':
        return '\n\n'.join([
            f"What can change {result['root']}:\n" + tree_text(g, [result['root']], result['reached'], reverse=True),
            levels_text(g, 'Entry points', result['ui'], limit),
        ])
    if kind == 'reach':
        fields = {n for n in result['reached'] if g.nodes[n]['type'] == 'field'}
        return '\n\n'.join([
            f"What {result['root']} can change:\n" + tree_text(g, [result['root']], result['reached']),
            levels_text(g, 'Fields it can change', fields, limit * 2),
        ])
    if kind == 'plan':
        fields = {n for n in result['reached'] if g.nodes[n]['type'] == 'field'} - set(result['roots'])
        logic = {n for n in result['reached'] if g.nodes[n]['lvl'] == 1}
        return '\n\n'.join([
            levels_text(g, 'You plan to touch', set(result['roots']), limit),
            levels_text(g, 'Fields that follow (recompute / written by hooks)', fields, limit),
            levels_text(g, 'Logic that runs (constraints, onchanges, hooks, methods)', logic, limit),
            levels_text(g, 'Code that already writes these fields (keep consistent)', result['writers'], limit),
            levels_text(g, 'Screens to test', result['ui'], limit),
        ])
    if kind == 'diff':
        fields = {n for n in result['reached'] if g.nodes[n]['type'] == 'field'} - result['touched']
        parts = [f"{len(result['touched'])} fields/methods changed in {len(result['files'])} python files",
                 levels_text(g, 'Touched directly', result['touched'], limit)]
        multi = [f"  {n}: {', '.join(_short(x) for x in sorted(result['only_at'][n]))}"
                 for n in sorted(result['touched']) if len(g.nodes[n].get('files') or ()) > 1]
        if multi:
            parts.append('Only these definitions changed:\n' + '\n'.join(multi))
        parts += [levels_text(g, 'Fields that can change as a result', fields, limit),
                  levels_text(g, 'Screens to test', result['ui'], limit)]
        return '\n\n'.join(parts)
    raise ValueError(kind)


def node_text(g, ref, limit=40):
    nid = g._need(ref)
    n = g.nodes[nid]
    out = [label(n), f"type={n['type']} level=L{n['lvl']} id={nid}"]
    for k, v in n.items():
        if k in ('id', 'lvl', 'type', 'label', 'name') or v in ('', None, [], False):
            continue
        out.append(f'  {k}: {v}')
    out.append(f'Outgoing ({len(g.out[nid])}):')
    out.extend(f"  {edge_text(e)} -> {label(g.nodes[e['t']], short=True)}" for e in g.out[nid][:limit] if e['t'] in g.nodes)
    out.append(f'Incoming ({len(g.inc[nid])}):')
    out.extend(f"  {label(g.nodes[e['s']], short=True)} -> {edge_text(e)}" for e in g.inc[nid][:limit] if e['s'] in g.nodes)
    return '\n'.join(out)
