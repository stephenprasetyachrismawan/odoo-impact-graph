"""Catalog for the interactive diagram: modules, models (class diagram), menus and security
groups (use case diagram), state workflows (activity diagram). Everything is read from code."""
import ast
import re
from collections import defaultdict

CLAUSE = re.compile(r'^\s*(\w+)\s*(==|!=|not\s+in|in)\s*(.+?)\s*$')


def _values(text):
    try:
        v = ast.literal_eval(text.strip())
    except (ValueError, SyntaxError):
        return None
    if isinstance(v, str):
        return {v}
    if isinstance(v, (list, tuple, set)):
        return {x for x in v if isinstance(x, str)}
    return None


def visible_states(cond, field, values):
    """States in which a button is visible, from Odoo 17 `invisible`, older `states` / `attrs`.
    None = no rule about `field` (visible in every state, as far as the view tells)."""
    if not cond:
        return None
    every = set(values)
    if cond.get('states') and field == 'state':
        return sorted(s.strip() for s in cond['states'].split(',') if s.strip())
    expr = cond.get('invisible')
    if expr and expr not in ('0', '1', 'True', 'False'):
        if ' and ' in expr:
            return None  # combined conditions: do not guess
        hidden, found = set(), False
        for clause in re.split(r'\s+or\s+', expr):
            clause = clause.strip()
            while clause.startswith('(') and clause.endswith(')') and _values(clause) is None:
                clause = clause[1:-1].strip()  # (state == 'done') -> state == 'done'
            m = CLAUSE.match(clause)
            if not m or m.group(1) != field:
                continue
            vals = _values(m.group(3))
            if vals is None:
                continue
            found = True
            op = ' '.join(m.group(2).split())
            hidden |= {'==': vals, 'in': vals, '!=': every - vals, 'not in': every - vals}[op]
        visible = sorted(every - hidden)
        return visible if found and visible else None  # hidden everywhere = a rule we do not read right
    attrs = cond.get('attrs')
    if attrs:
        m = re.search(r"['\"]invisible['\"]\s*:\s*(\[.*?\])\s*[,}]", attrs)
        try:
            domain = ast.literal_eval(m.group(1)) if m else None
        except (ValueError, SyntaxError):
            domain = None
        for leaf in domain or []:
            if isinstance(leaf, (list, tuple)) and len(leaf) == 3 and leaf[0] == field:
                vals = {leaf[2]} if isinstance(leaf[2], str) else {x for x in leaf[2] if isinstance(x, str)}
                op = leaf[1]
                hidden = vals if op in ('=', '==', 'in') else every - vals
                return sorted(every - hidden)
    return None


def build(facts, mods, order, classes, kind, parents, inherits, all_methods, raw, model_ref, custom_mods):
    models = facts['models']

    # -- security groups ---------------------------------------------------
    # a group / category is often completed by several records with the same xmlid: merge them
    categories = {}
    # Odoo creates base.module_category_<a_b> named "B" from each manifest `category: 'A/B'`
    for mod in mods.values():
        parts = [x.strip() for x in str(mod.manifest.get('category') or '').split('/') if x.strip()]
        for i in range(len(parts)):
            slug = '_'.join(x.replace('&', 'and').replace(' ', '_').replace('.', '_').lower() for x in parts[:i + 1])
            categories.setdefault(f'base.module_category_{slug}', parts[i])
    for c in raw['categories']:
        if c.get('xmlid') and c.get('name'):
            categories[c['xmlid']] = c['name']
    merged = {}
    for g in raw['groups']:
        if g.get('xmlid'):
            cur = merged.setdefault(g['xmlid'], {'module': g['module']})
            for k in ('name', 'category'):
                if g.get(k):
                    cur[k] = g[k]
    groups = {}
    for xid, g in merged.items():
        cat, name = categories.get(g.get('category')), g.get('name') or xid.split('.')[-1]
        groups[xid] = {'name': f'{cat} / {name}' if cat else name, 'custom': g['module'] in custom_mods}

    def group_list(text_or_list):
        items = text_or_list.split(',') if isinstance(text_or_list, str) else (text_or_list or [])
        return [g.strip() for g in items if g and g.strip() and not g.strip().startswith('-')]

    access = defaultdict(list)
    for row in raw['access']:
        model = model_ref.get(row['model_ref'])
        if model in models:
            access[model].append({'group': row['group'], 'perms': row['perms']})

    # -- actions -> model, menus --------------------------------------------
    action_model = {a['id']: a['model'] for a in facts['actions']}
    action_name = {a['id']: a['name'] for a in facts['actions']}
    action_views = {a['id']: a.get('views', []) for a in facts['actions']}
    menus = {}
    for m in raw['menus']:
        if m.get('xmlid'):
            cur = menus.setdefault(m['xmlid'], {'groups': []})
            for k, v in m.items():
                if v not in (None, '', []):
                    cur[k] = v
    menu_out = []
    for xid, m in menus.items():
        aid = f"action:{m['action']}" if m.get('action') else None
        menu_out.append({
            'id': f'menu:{xid}', 'name': m.get('name') or action_name.get(aid) or xid.split('.')[-1],
            'parent': f"menu:{m['parent']}" if m.get('parent') in menus else None,
            'action': aid, 'model': action_model.get(aid), 'sequence': m.get('sequence', 10),
            'groups': group_list(m.get('groups')), 'custom': m.get('module') in custom_mods,
        })

    # -- buttons per model (use cases) -------------------------------------
    view_actions = defaultdict(set)
    for aid, views in action_views.items():
        for v in views:
            view_actions[v].add(aid)
    buttons = defaultdict(dict)
    for v in facts['views']:
        for item in v['items']:
            if item[0] != 'button':
                continue
            model, name, btype, action_xmlid, label = item[1], item[2], item[3], item[4], item[5]
            grp = item[6] if len(item) > 6 else ''
            cond = item[7] if len(item) > 7 else {}
            key = (name, label)
            b = buttons[model].setdefault(key, {
                'name': action_xmlid if btype == 'action' and action_xmlid else name, 'label': label, 'type': btype,
                'groups': group_list(grp), 'views': [], 'cond': cond, 'custom': False,
            })
            b['views'].append(v['id'])
            b['custom'] = b['custom'] or bool(v.get('custom'))

    # -- models: class diagram + workflows -----------------------------------
    module_models = defaultdict(set)
    module_defines = defaultdict(set)
    for model, infos in classes.items():
        for cls in infos:
            module_models[cls['module']].add(model)
            if cls['name'] == model and model not in cls['inherit']:
                module_defines[cls['module']].add(model)

    cron_calls = defaultdict(set)
    for c in facts['crons']:
        cron_calls[c['model']].update(c.get('calls', []))

    out_models = {}
    for model in models:
        fields = facts['fields'].get(model, {})
        methods = facts['methods'].get(model, {})
        callgraph = {name: {c for d in info['defs'] for c in d.get('calls', []) if c in methods and c != name}
                     for name, info in methods.items()}

        def reach(start, depth=3):
            """Methods a button runs: itself and what it calls, 3 calls deep (deeper = guessing)."""
            seen, frontier = {start}, {start}
            for _ in range(depth):
                frontier = {n for cur in frontier for n in callgraph.get(cur, ()) if n not in seen}
                seen |= frontier
            return seen

        returned = defaultdict(set)
        for name, info in methods.items():
            for d in info['defs']:
                returned[name].update(map(tuple, d.get('returned_values', [])))
        writes = defaultdict(set)  # (field, value) -> methods writing it
        for name, info in methods.items():
            for d in info['defs']:
                for f, v in d.get('value_writes', []):
                    writes[(f, v)].add(name)
                for helper in d.get('writes_result_of', []):
                    for f, v in returned.get(helper, ()):
                        writes[(f, v)].add(name)

        workflows = []
        for fname, f in fields.items():
            values = [v for v, _ in f.get('selection') or []]
            if f['type'] != 'selection' or not values or f.get('related') or (f.get('compute') and not f.get('stored')):
                continue
            if len(values) < 3 and not fname.endswith(('state', 'status', 'stage')):
                continue  # small flags (priority 0/1) are not workflows
            targets = {(fn, v): ms for (fn, v), ms in writes.items() if fn == fname and v in values}
            if not targets:
                continue
            transitions, used = [], set()
            for b in buttons.get(model, {}).values():
                if b['type'] != 'object':
                    continue
                reached = reach(b['name'])
                for (_, value), writers in targets.items():
                    hit = reached & writers
                    if hit:
                        used |= hit
                        transitions.append({
                            'from': visible_states(b['cond'], fname, values), 'to': value, 'kind': 'button',
                            'label': b['label'], 'method': b['name'], 'writer': sorted(hit)[0],
                            'groups': b['groups'],
                        })
            for (_, value), writers in targets.items():
                for w in sorted(writers - used):
                    by_cron = any(w in reach(c) for c in cron_calls.get(model, ()) if c in methods)
                    transitions.append({'from': None, 'to': value, 'kind': 'cron' if by_cron else 'code',
                                        'label': w, 'method': w, 'writer': w, 'groups': []})
            seen, unique = set(), []
            for t in transitions:
                key = (tuple(t['from'] or ()), t['to'], t['label'], t['method'])
                if key not in seen:
                    seen.add(key)
                    unique.append(t)
            workflows.append({'field': fname, 'label': f.get('string') or fname, 'states': f['selection'],
                              'computed_by': f.get('compute') or '',
                              'initial': f.get('default') if f.get('default') in values else values[0],
                              'transitions': unique})

        defined_in = [m for m, ms in module_defines.items() if model in ms]
        out_models[model] = {
            'kind': models[model]['kind'], 'description': models[model]['description'],
            'modules': models[model]['modules'], 'custom': models[model]['custom'],
            'own': any(m in custom_mods for m in defined_in),  # created by a custom module (not only extended)
            'defined_in': defined_in,
            'parents': [p for p in parents.get(model, []) if p != model],
            'inherits': inherits.get(model, {}),
            'fields': {n: {k: f.get(k) for k in ('type', 'string', 'comodel', 'inverse_name', 'compute', 'related',
                                                   'stored', 'required', 'custom', 'selection', 'groups')}
                       for n, f in fields.items()},
            'methods': {n: {'custom': info['custom'], 'loc': [d['loc'] for d in info['defs']][-1:],
                            'decorated': sorted({k for d in info['defs'] for k in ('depends', 'constrains', 'onchange') if d.get(k)})}
                        for n, info in methods.items()},
            'buttons': [{k: v for k, v in b.items() if k != 'cond'} for b in buttons.get(model, {}).values()],
            'access': access.get(model, []),
            'workflows': workflows,
        }

    return {
        'modules': {m: {'custom': mods[m].custom, 'depends': mods[m].depends,
                        'models': sorted(module_models[m] & set(models)),
                        'defines': sorted(module_defines[m] & set(models)),
                        'summary': str(mods[m].manifest.get('summary') or mods[m].manifest.get('name') or '')}
                    for m in order},
        'models': out_models,
        'menus': menu_out,
        'groups': groups,
    }
