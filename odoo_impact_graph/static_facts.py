"""Build the facts of an Odoo code base from source only (no database, no Odoo import).

Mirrors what Odoo's registry does at runtime:
  - a model is the merge of every class with that _name / _inherit, in module load order
  - _inherit of another model copies its fields (mixins), _inherits delegates them
  - a computed field depends on the union of @api.depends of every override of its method
  - each prefix of a dependency path triggers the field (a.b.c -> a, a.b, a.b.c)
  - a view is its arch plus every extension view that inherits it
"""
import re
from collections import defaultdict

from . import modules as modmod
from . import pyscan, xmlscan

MAGIC = {
    'id': {'type': 'Id', 'string': 'ID', 'store': True},
    'display_name': {'type': 'Char', 'string': 'Display Name', 'compute': '_compute_display_name', 'store': False},
    'create_uid': {'type': 'Many2one', 'comodel': 'res.users', 'string': 'Created by', 'store': True},
    'create_date': {'type': 'Datetime', 'string': 'Created on', 'store': True},
    'write_uid': {'type': 'Many2one', 'comodel': 'res.users', 'string': 'Last Updated by', 'store': True},
    'write_date': {'type': 'Datetime', 'string': 'Last Updated on', 'store': True},
}
CODE_CALL = re.compile(r'\.(\w+)\(')


def build(paths, core_paths=(), custom_paths=(), wanted=None, log=print):
    mods, sources = modmod.load_modules(paths, core_paths, custom_paths)
    chosen = modmod.select(mods, wanted)
    order = modmod.load_order(mods, chosen)
    log(f'{len(mods)} modules found, {len(order)} selected '
        f'({sum(1 for m in order if mods[m].custom)} custom)')
    custom_mods = {m for m in order if mods[m].custom}

    # -- python -------------------------------------------------------------
    classes = defaultdict(list)  # model -> class infos in load order
    for name in order:
        for cls in pyscan.scan_module(name, mods[name].path):
            classes[cls['model']].append(cls)

    kind, description, rec_name, parents, inherits, model_modules = {}, {}, {}, {}, {}, {}
    for model, infos in classes.items():
        parents[model] = []
        inherits[model] = {}
        model_modules[model] = []
        for cls in infos:
            if cls['name'] == model and model not in cls['inherit'] and model not in kind:
                kind[model] = cls['kind'] or 'model'
            for p in cls['inherit']:
                if p != model and p not in parents[model]:
                    parents[model].append(p)
            inherits[model].update(cls['inherits'])
            description.setdefault(model, cls['description'])
            if cls['rec_name']:
                rec_name[model] = cls['rec_name']
            if cls['module'] not in model_modules[model]:
                model_modules[model].append(cls['module'])
        kind.setdefault(model, next((c['kind'] for c in infos if c['kind']), 'model'))

    field_cache = {}

    def all_fields(model, stack=()):
        if model in field_cache:
            return field_cache[model]
        if model in stack:
            return {}
        result = {}
        for parent in parents.get(model, []):
            for fname, f in all_fields(parent, stack + (model,)).items():
                result[fname] = dict(f)
        for comodel, fk in inherits.get(model, {}).items():
            for fname, f in all_fields(comodel, stack + (model,)).items():
                if fname not in result and fname not in MAGIC:
                    result[fname] = {'type': f['type'], 'string': f.get('string'), 'related': f'{fk}.{fname}',
                                     'comodel': f.get('comodel'), 'modules': [], 'loc': [], 'delegated': True}
        for cls in classes.get(model, []):
            for fname, attrs in cls['fields'].items():
                base = result.get(fname, {'modules': [], 'loc': []})
                if base.get('delegated'):  # own definition replaces the _inherits one
                    base = {'modules': [], 'loc': []}
                merged = {**base, **{k: v for k, v in attrs.items() if k != 'loc'}}
                if attrs['type'] != base.get('type') and base.get('type'):
                    for k in ('compute', 'related', 'store', 'comodel'):  # redefinition, not an extension
                        if k not in attrs:
                            merged.pop(k, None)
                merged['modules'] = base['modules'] + [cls['module']]
                merged['loc'] = base['loc'] + [attrs['loc']]
                result[fname] = merged
        if kind.get(model) != 'abstract':
            for fname, attrs in MAGIC.items():
                result.setdefault(fname, {**attrs, 'modules': [], 'loc': []})
        field_cache[model] = result
        return result

    method_cache = {}

    def all_methods(model, stack=()):
        """{name: [defs]} over the whole MRO (parents first)."""
        if model in method_cache:
            return method_cache[model]
        if model in stack:
            return {}
        result = defaultdict(list)
        for parent in parents.get(model, []):
            for name, defs in all_methods(parent, stack + (model,)).items():
                result[name].extend(defs)
        for cls in classes.get(model, []):
            for name, meth in cls['methods'].items():
                result[name].append({**meth, 'module': cls['module'], 'model': model})
        method_cache[model] = result
        return result

    concrete = sorted(m for m in classes if kind.get(m) != 'abstract' and m in kind)

    def comodel_of(model, fname):
        return all_fields(model).get(fname, {}).get('comodel')

    def fields_of(model):
        return all_fields(model).keys()

    facts = {
        'source': 'static', 'db': None,
        'modules': {m: {'path': mods[m].path, 'custom': mods[m].custom} for m in order},
        'models': {}, 'fields': {}, 'field_edges': [], 'constraints': [], 'onchanges': [],
        'methods': {}, 'views': [], 'actions': [], 'menus': [], 'crons': [], 'automations': [],
        'unresolved_depends': [],
    }

    for model in concrete:
        facts['models'][model] = {
            'kind': kind[model], 'description': description.get(model) or model,
            'modules': model_modules[model], 'custom': any(m in custom_mods for m in model_modules[model]),
        }
        out = {}
        for fname, f in all_fields(model).items():
            stored = f.get('store')
            if stored is None:
                stored = not (f.get('compute') or f.get('related'))
            out[fname] = {
                'type': f['type'].lower(), 'string': f.get('string') or fname.replace('_', ' ').title(),
                'stored': bool(stored), 'compute': f.get('compute') if isinstance(f.get('compute'), str) else '',
                'related': f.get('related') or '', 'comodel': f.get('comodel') or '',
                'modules': sorted(set(f.get('modules', []))), 'loc': f.get('loc', []),
                'custom': any(m in custom_mods for m in f.get('modules', [])),
            }
        facts['fields'][model] = out

    # -- field dependencies (what Odoo puts in its trigger tree) -----------
    def triggers(model, fname, path):
        """[(trigger field id, via)] like Odoo's Field.resolve_depends + trigger tree:
        every prefix of the path, plus the inverse many2one of each one2many on the way.
        A transient model is never triggered by a regular model."""
        out, names, cur = [], [], model
        transient = kind.get(model) == 'transient'
        for index, seg in enumerate(path.split('.')):
            if not cur or seg not in fields_of(cur):
                return out, False
            if transient and kind.get(cur) != 'transient':
                break
            via = '.'.join(reversed(names))
            if not (index == 0 and seg == fname):
                out.append((f'{cur}.{seg}', via))
            names.append(seg)
            fdef = all_fields(cur)[seg]
            co = fdef.get('comodel')
            if fdef['type'] == 'One2many' and fdef.get('inverse_name') and co:
                out.append((f"{co}.{fdef['inverse_name']}", '.'.join(reversed(names))))
            cur = co
        return out, True

    for model in concrete:
        methods = all_methods(model)
        rn = rec_name.get(model) or ('name' if 'name' in fields_of(model) else None)
        for fname, f in all_fields(model).items():
            deps, how, fn = [], None, ''
            if f.get('related'):
                deps, how = [f['related']], 'related'
            elif isinstance(f.get('compute'), str):
                how, fn = 'compute', f['compute']
                for d in methods.get(fn, []):
                    deps.extend(d.get('depends', []))
                deps.extend(f.get('depends') or [])
                if fname == 'display_name' and rn:
                    deps.append(rn)
            for dep in dict.fromkeys(deps):
                found, ok = triggers(model, fname, dep)
                if not ok:
                    facts['unresolved_depends'].append(f'{model}.{fname}: {dep}')
                for trigger, via in found:
                    facts['field_edges'].append([trigger, f'{model}.{fname}', how, via, fn])
        for name, defs in methods.items():
            for d in defs:
                for kind_key, bucket in (('constrains', 'constraints'), ('onchange', 'onchanges')):
                    for fld in d.get(kind_key, []):
                        first = fld.split('.')[0]
                        if first in fields_of(model):
                            facts[bucket].append([f'{model}.{first}', model, name])

    # -- methods: own defs + defs from custom parents ----------------------
    for model in concrete:
        own = {}
        for name, defs in all_methods(model).items():
            keep = [d for d in defs if d['model'] == model or d['module'] in custom_mods]
            if keep:
                own[name] = {'custom': any(d['module'] in custom_mods for d in keep), 'defs': keep}
        facts['methods'][model] = own

    # -- xml ------------------------------------------------------------
    raw = defaultdict(list)
    for name in order:
        rec = xmlscan.scan_module(name, mods[name].path, mods[name].manifest)
        for key, items in rec.items():
            for item in items:
                item['module'] = name
                raw[key].append(item)

    views = {}
    for v in raw['views']:
        if v['xmlid'] in views:  # a later module redefines the record
            old = views[v['xmlid']]
            old.update({k: val for k, val in v.items() if val is not None and k != 'modules'})
            old['modules'].append(v['module'])
        else:
            views[v['xmlid']] = {**v, 'modules': [v['module']]}
    for v in views.values():
        if not v.get('mode'):
            v['mode'] = 'extension' if v.get('inherit_id') else 'primary'
    for _ in range(5):  # model / type come from the parent when an inherited view omits them
        for v in views.values():
            parent = views.get(v.get('inherit_id'))
            if parent:
                v['model'] = v.get('model') or parent.get('model')
                v.setdefault('vtype', None)
                v['vtype'] = v['vtype'] or xmlscan.view_type(v['arch']) or parent.get('vtype')
            else:
                v['vtype'] = v.get('vtype') or xmlscan.view_type(v['arch'])
    extensions = defaultdict(list)
    for v in views.values():
        if v['mode'] == 'extension' and v.get('inherit_id') in views:
            extensions[v['inherit_id']].append(v)

    def combined(xid, stack=()):
        v = views[xid]
        parts = [v]
        todo = list(extensions[xid])
        while todo:
            ext = todo.pop(0)
            parts.append(ext)
            todo.extend(extensions[ext['xmlid']])
        if v['mode'] == 'primary' and v.get('inherit_id') in views and v['inherit_id'] not in stack:
            parts = combined(v['inherit_id'], stack + (xid,)) + parts
        return parts

    primary_by_model = defaultdict(list)
    for xid, v in views.items():
        if v['mode'] != 'primary' or v.get('model') not in facts['models']:
            continue
        parts = combined(xid)
        items = []
        for part in parts:
            for item in xmlscan.arch_items(part['arch'], v['model'], fields_of, comodel_of):
                if item[0] == 'button' and item[4] and '.' not in item[4]:
                    item = item[:4] + (f"{part['modules'][-1]}.{item[4]}",) + item[5:]
                items.append(item)
        mods_touching = sorted({m for p in parts for m in p['modules']})
        facts['views'].append({
            'id': f'view:{xid}', 'xmlid': xid, 'model': v['model'], 'type': v.get('vtype') or '',
            'name': v.get('name') or xid, 'priority': v['priority'], 'loc': v['loc'],
            'custom': any(m in custom_mods for m in mods_touching),
            'items': [list(i) for i in items],
        })
        primary_by_model[(v['model'], v.get('vtype'))].append((v['priority'], len(facts['views']), f'view:{xid}'))

    model_ref = {f"model_{m.replace('.', '_')}": m for m in classes}

    def ref_model(ref, fallback=None):
        if fallback:
            return fallback
        if not ref:
            return None
        return model_ref.get(ref.split('.')[-1])

    action_views = defaultdict(list)
    for av in raw['action_views']:
        if av['action'] and av['view']:
            action_views[av['action']].append(f"view:{av['view']}")

    view_types = {v['id']: v['type'] for v in facts['views']}
    actions = {}
    for a in raw['actions']:
        if not a.get('xmlid'):
            continue
        actions.setdefault(a['xmlid'], {}).update({k: v for k, v in a.items() if v not in (None, '')})
    for xid, a in actions.items():
        if not a.get('res_model'):
            continue
        view_ids = list(action_views.get(xid, []))
        for key in ('view_id', 'search_view_id'):
            if a.get(key):
                view_ids.append(f'view:{a[key]}')
        for ref in xmlscan.REF_IN_EVAL.findall(a.get('view_ids_eval', '')):
            view_ids.append(f"view:{ref if '.' in ref else a['module'] + '.' + ref}")
        modes = [m.strip() for m in (a.get('view_mode') or '').split(',') if m.strip()] + ['search']
        have = {view_types.get(vid) for vid in view_ids}
        for mode in modes:
            mode = 'tree' if mode == 'list' else mode
            if mode in have:
                continue
            candidates = sorted(primary_by_model.get((a['res_model'], mode), []))
            if candidates:
                view_ids.append(candidates[0][2])
        facts['actions'].append({
            'id': f'action:{xid}', 'xmlid': xid, 'type': 'act_window', 'name': a.get('name') or xid,
            'model': a['res_model'], 'target': a.get('target') or '', 'binding': ref_model(a.get('binding_model')) or '',
            'views': list(dict.fromkeys(view_ids)), 'calls': [], 'custom': a['module'] in custom_mods,
        })
    for s in raw['server_actions']:
        model = ref_model(s.get('model_ref'), s.get('model'))
        if s.get('xmlid') and model:
            facts['actions'].append({
                'id': f"action:{s['xmlid']}", 'xmlid': s['xmlid'], 'type': 'server_action', 'name': s.get('name') or s['xmlid'],
                'model': model, 'binding': ref_model(s.get('binding_model')) or '', 'views': [],
                'calls': sorted(set(CODE_CALL.findall(s.get('code') or ''))), 'custom': s['module'] in custom_mods,
            })
    for r in raw['reports']:
        if r.get('xmlid') and r.get('model'):
            facts['actions'].append({
                'id': f"action:{r['xmlid']}", 'xmlid': r['xmlid'], 'type': 'report', 'name': r.get('name') or r['xmlid'],
                'model': r['model'], 'views': [], 'calls': [], 'custom': r['module'] in custom_mods,
            })
    for c in raw['crons']:
        model = ref_model(c.get('model_ref'), c.get('model'))
        if c.get('xmlid') and model:
            calls = set(CODE_CALL.findall(c.get('code') or ''))
            if c.get('function'):
                calls.add(c['function'])
            facts['crons'].append({'id': f"cron:{c['xmlid']}", 'xmlid': c['xmlid'], 'name': c.get('name') or c['xmlid'],
                                   'model': model, 'calls': sorted(calls), 'custom': c['module'] in custom_mods})

    menus = {}
    for m in raw['menus']:
        if m.get('xmlid'):
            menus.setdefault(m['xmlid'], {}).update({k: v for k, v in m.items() if v})
    action_names = {a['xmlid']: a['name'] for a in facts['actions']}

    def menu_name(xid, depth=0):
        m = menus.get(xid, {})
        own = m.get('name') or action_names.get(m.get('action')) or xid.split('.')[-1]
        if m.get('parent') in menus and depth < 8:
            return menu_name(m['parent'], depth + 1) + '/' + own
        return own

    for xid, m in menus.items():
        if m.get('action'):
            facts['menus'].append({'id': f'menu:{xid}', 'xmlid': xid, 'name': menu_name(xid),
                                   'action': f"action:{m['action']}", 'custom': m.get('module') in custom_mods})

    log(f"{len(facts['models'])} models, {sum(len(f) for f in facts['fields'].values())} fields, "
        f"{len(facts['views'])} views, {len(facts['actions'])} actions, {len(facts['menus'])} menus")
    return facts


def overlay_source(facts, log=print):
    """Add what only the source knows to registry facts: method bodies (writes, calls, hooks)
    and field definition locations, for the custom modules."""
    count = 0
    for name, info in facts['modules'].items():
        if not info['custom'] or not info.get('path'):
            continue
        for cls in pyscan.scan_module(name, info['path']):
            model = cls['model']
            if model not in facts['models']:
                continue
            methods = facts['methods'].setdefault(model, {})
            for mname, meth in cls['methods'].items():
                entry = methods.setdefault(mname, {'custom': True, 'defs': []})
                entry['defs'].append({**meth, 'module': name, 'model': model})
                count += 1
            for fname, attrs in cls['fields'].items():
                fd = facts['fields'][model].get(fname)
                if fd is not None:
                    fd['loc'] = fd.get('loc', []) + [attrs['loc']]
    log(f'{count} custom method definitions read from source')
    return facts
