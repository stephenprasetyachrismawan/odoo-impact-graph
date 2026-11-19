"""Turn facts (static or registry) into the layered impact graph.

Levels
  0 field     model.field
  1 logic     compute / constraint / onchange / write-create hook / method
  2 model     model or wizard (TransientModel)
  3 ui        view, button, report
  4 entry     menu, action, server action, cron, automation

Edge direction is always "A changes or opens -> B is affected or runs".
Edge `src`:
  registry  read from a running Odoo registry (certain)
  static    read from declarations: fields, @api.*, views, actions, menus (reliable)
  ast       read from method bodies: vals keys, .write({...}), rec.x = ..., calls (heuristic)
"""
from collections import defaultdict

HOOKS = {'write', 'create', 'unlink', 'copy'}


class _Builder:
    def __init__(self, facts):
        self.f = facts
        self.nodes = {}
        self.edges = []
        self.seen = set()
        self.decl = 'registry' if facts.get('source') == 'registry' else 'static'

    def node(self, nid, **attrs):
        n = self.nodes.get(nid)
        if n is None:
            self.nodes[nid] = n = {'id': nid, **attrs}
        else:
            for k, v in attrs.items():
                if v not in (None, '', [], ()) and not n.get(k):
                    n[k] = v
        return nid

    def edge(self, a, b, kind, src=None, **attrs):
        src = src or self.decl
        key = (a, b, kind, attrs.get('via', ''))
        if a == b or key in self.seen:
            return
        self.seen.add(key)
        e = {'s': a, 't': b, 'k': kind, 'src': src}
        e.update({k: v for k, v in attrs.items() if v not in (None, '')})
        self.edges.append(e)

    def has_field(self, model, name):
        return name in self.f['fields'].get(model, {})

    def field(self, fid):
        model, name = fid.rsplit('.', 1)
        f = self.f['fields'].get(model, {}).get(name)
        if f is None:
            return None
        return self.node(fid, lvl=0, type='field', model=model, name=name, label=f.get('string') or name,
                         ftype=f.get('type'), stored=f.get('stored'), compute=f.get('compute') or '',
                         related=f.get('related') or '', comodel=f.get('comodel') or '',
                         modules=f.get('modules') or [], files=f.get('loc') or [], custom=bool(f.get('custom')))

    def method(self, model, name, mtype=None, **attrs):
        mid = f'{model}::{name}'
        if mid not in self.nodes:
            info = self.f['methods'].get(model, {}).get(name)
            files = [d['loc'] for d in info['defs']] if info else []
            self.node(mid, lvl=1, type=mtype or self.method_type(model, name), model=model, name=name,
                      files=files, custom=bool(info and info['custom']),
                      modules=sorted({d['module'] for d in info['defs']}) if info else [])
        return mid

    def method_type(self, model, name):
        if name in HOOKS:
            return 'hook'
        if name in self.computes.get(model, set()):
            return 'compute'
        if name.startswith(('_onchange', 'onchange')):
            return 'onchange'
        if name.startswith(('_check', '_constrain')):
            return 'constraint'
        if 'cron' in name:
            return 'cron_method'
        return 'public' if not name.startswith('_') else 'method'

    def model(self, model):
        m = self.f['models'].get(model)
        if m is None:
            return None
        return self.node(f'model:{model}', lvl=2, type='wizard' if m['kind'] == 'transient' else 'model',
                         model=model, label=m.get('description') or model, custom=bool(m.get('custom')))

    def build(self):
        f = self.f
        self.computes = defaultdict(set)
        for model, fields in f['fields'].items():
            for name, fd in fields.items():
                if fd.get('compute'):
                    self.computes[model].add(fd['compute'])
            if any(fd.get('custom') for fd in fields.values()):
                for name in fields:
                    self.field(f'{model}.{name}')

        for dep, target, how, via, fn in f['field_edges']:
            a, b = self.field(dep), self.field(target)
            if a and b:
                self.edge(a, b, how, via=via, fn=fn)
        for kind, bucket, mtype in (('constrains', 'constraints', 'constraint'), ('onchange', 'onchanges', 'onchange')):
            for fid, model, name in f[bucket]:
                a = self.field(fid)
                if a:
                    self.edge(a, self.method(model, name, mtype), kind)

        for model, methods in f['methods'].items():
            for name, info in methods.items():
                mid = self.method(model, name)
                for d in info['defs']:
                    where = d['loc']
                    if name in HOOKS:
                        for key in d.get('vals_keys', []):
                            if self.has_field(model, key):
                                self.edge(self.field(f'{model}.{key}'), mid, f'{name}_hook', 'ast', at=where)
                    for key in d.get('own_writes', []):
                        if self.has_field(model, key):
                            self.edge(mid, self.field(f'{model}.{key}'), 'writes', 'ast', at=where)
                    for other, key in d.get('other_writes', []):
                        if self.has_field(other, key):
                            self.edge(mid, self.field(f'{other}.{key}'), 'writes', 'ast', at=where)
                    for called in d.get('calls', []):
                        # write()/create() calls are generic: what they write is captured as `writes`
                        if called != name and called in methods and called not in HOOKS:
                            self.edge(mid, self.method(model, called), 'calls', 'ast', at=where)
                    for target in d.get('opens_models', []):
                        mn = self.model(target)
                        if mn:
                            self.edge(mid, mn, 'opens', 'ast', at=where)
                    for xid in d.get('opens_refs', []):
                        self.edge(mid, f'action:{xid}', 'opens', 'ast', at=where)
                if name in self.computes.get(model, set()):
                    for fname, fd in f['fields'][model].items():
                        if fd.get('compute') == name:
                            self.edge(mid, self.field(f'{model}.{fname}'), 'computes')

        for v in f['views']:
            if v['model'] not in f['models']:
                continue
            vid = self.node(v['id'], lvl=3, type='view', model=v['model'], label=v['name'], vtype=v['type'],
                            xmlid=v.get('xmlid') or '', custom=bool(v.get('custom')), files=[v['loc']] if v.get('loc') else [])
            for item in v['items']:
                if item[0] == 'field':
                    _, model, name, label = item
                    fid = self.field(f'{model}.{name}')
                    if fid:
                        self.edge(vid, fid, 'shows', label=label)
                else:
                    _, model, name, btype, action_xmlid, label = item
                    if btype == 'action' and action_xmlid:
                        name = action_xmlid  # instead of the raw %(xmlid)d / database id
                    bid = self.node(f'button:{v["id"]}:{model}:{name}', lvl=3, type='button', model=model,
                                    name=name, label=label, view=vid, custom=bool(v.get('custom')))
                    self.edge(vid, bid, 'has_button')
                    if btype == 'object':
                        self.edge(bid, self.method(model, name, 'public'), 'calls')
                    elif action_xmlid:
                        self.edge(bid, f'action:{action_xmlid}', 'opens')
                    elif name.isdigit():
                        self.edge(bid, f'action:#{name}', 'opens')

        for a in f['actions']:
            if a['model'] not in f['models']:
                continue
            lvl = 3 if a['type'] == 'report' else 4
            aid = self.node(a['id'], lvl=lvl, type={'act_window': 'action'}.get(a['type'], a['type']),
                            model=a['model'], label=a['name'], xmlid=a.get('xmlid') or '',
                            target=a.get('target') or '', binding=a.get('binding') or '', custom=bool(a.get('custom')))
            mn = self.model(a['model'])
            if mn:
                self.edge(aid, mn, 'model')
            for view in a.get('views', []):
                self.edge(aid, view, 'uses_view')
            for called in a.get('calls', []):
                if called in f['methods'].get(a['model'], {}):
                    self.edge(aid, self.method(a['model'], called), 'runs', 'ast')
        for c in f['crons']:
            if c['model'] not in f['models']:
                continue
            cid = self.node(c['id'], lvl=4, type='cron', model=c['model'], label=c['name'],
                            xmlid=c.get('xmlid') or '', custom=bool(c.get('custom')))
            for called in c.get('calls', []):
                if called in f['methods'].get(c['model'], {}):
                    self.edge(cid, self.method(c['model'], called), 'runs', 'ast')
        for au in f.get('automations', []):
            auid = self.node(au['id'], lvl=4, type='automation', model=au['model'], label=au['name'])
            for fid in au.get('trigger_fields', []):
                if self.field(fid):
                    self.edge(fid, auid, 'triggers')
            for act in au.get('actions', []):
                self.edge(auid, act, 'runs')
        for m in f['menus']:
            self.node(m['id'], lvl=4, type='menu', label=m['name'], xmlid=m.get('xmlid') or '',
                      custom=bool(m.get('custom')))
            self.edge(m['id'], m['action'], 'opens')
        return self


def scope(nodes, edges, keep_all=False):
    """Keep what is connected to custom code: custom nodes, everything their changes reach,
    everything that reaches them, and the views/actions/menus of the models involved."""
    edges = [e for e in edges if e['s'] in nodes and e['t'] in nodes]
    if keep_all:
        return nodes, edges
    logic = [e for e in edges if nodes[e['s']]['lvl'] <= 2 and nodes[e['t']]['lvl'] <= 2]
    fwd, bwd = defaultdict(set), defaultdict(set)
    for e in logic:
        fwd[e['s']].add(e['t'])
        bwd[e['t']].add(e['s'])
    seeds = {nid for nid, n in nodes.items() if n.get('custom') and n['lvl'] <= 2}

    def closure(graph):
        seen, stack = set(seeds), list(seeds)
        while stack:
            for nxt in graph[stack.pop()]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    keep = closure(fwd) | closure(bwd)
    models = {nodes[k].get('model') for k in keep}
    keep |= {nid for nid, n in nodes.items() if n['lvl'] >= 2 and (n.get('model') in models or n.get('custom'))}
    # buttons / views of kept models pull in the methods they call, menus pull in their actions
    for e in edges:
        if e['s'] in keep and e['k'] in ('calls', 'has_button', 'runs', 'uses_view', 'computes') and nodes[e['t']]['lvl'] <= 3:
            keep.add(e['t'])
    for e in edges:
        if nodes[e['s']]['type'] == 'menu' and e['t'] in keep:
            keep.add(e['s'])
    kept_edges = [e for e in edges if e['s'] in keep and e['t'] in keep]
    used = {e['s'] for e in kept_edges} | {e['t'] for e in kept_edges} | {k for k in keep if nodes[k].get('custom')}
    return {k: nodes[k] for k in used}, kept_edges


def build_graph(facts, keep_all=False):
    b = _Builder(facts).build()
    nodes, edges = scope(b.nodes, b.edges, keep_all)
    return {
        'meta': {
            'source': facts.get('source'), 'db': facts.get('db'), 'odoo_version': facts.get('odoo_version'),
            'custom_modules': sorted(m for m, info in facts['modules'].items() if info['custom']),
            'module_paths': {m: info['path'] for m, info in facts['modules'].items()},
            'unresolved_depends': facts.get('unresolved_depends', [])[:500],
        },
        'nodes': sorted(nodes.values(), key=lambda n: n['id']),
        'edges': edges,
    }
