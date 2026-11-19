"""Facts read from a running Odoo registry. Runs INSIDE `odoo-bin shell` (stdin), read-only.

Self-contained on purpose (only stdlib + odoo), because the Odoo virtualenv usually does not
have odoo-impact-graph installed. Parameters come from environment variables:
  OIG_OUT          path of the facts JSON to write
  OIG_CUSTOM       os.pathsep-separated folders whose modules count as custom (optional)
Tested on Odoo 14 - 18 APIs (falls back where names changed).
"""
import json
import os
import re

import odoo
from odoo.modules.module import get_module_path

env = globals()['env']
registry = env.registry
OUT = os.environ['OIG_OUT']
CUSTOM_DIRS = [os.path.realpath(p) for p in os.environ.get('OIG_CUSTOM', '').split(os.pathsep) if p]
ODOO_DIR = os.path.realpath(os.path.join(os.path.dirname(odoo.__file__), '..'))
CODE_CALL = re.compile(r'\.(\w+)\(')

installed = env['ir.module.module'].search([('state', '=', 'installed')])
modules = {}
for mod in installed:
    path = get_module_path(mod.name, display_warning=False) or ''
    real = os.path.realpath(path) if path else ''
    if CUSTOM_DIRS:
        custom = any(real.startswith(d + os.sep) or real == d for d in CUSTOM_DIRS)
    else:
        custom = bool(real) and not real.startswith(ODOO_DIR + os.sep) and not str(mod.license or '').startswith('OEEL')
    modules[mod.name] = {'path': real, 'custom': custom}
CUSTOM = {m for m, info in modules.items() if info['custom']}


def fname_of(func):
    while not hasattr(func, '__name__') and hasattr(func, 'func'):
        func = func.func
    return getattr(func, '__name__', str(func))


facts = {
    'source': 'registry', 'db': env.cr.dbname, 'odoo_version': odoo.release.version,
    'modules': modules, 'models': {}, 'fields': {}, 'field_edges': [], 'constraints': [], 'onchanges': [],
    'methods': {}, 'views': [], 'actions': [], 'menus': [], 'crons': [], 'automations': [],
}

for model_name, Model in registry.items():
    if Model._abstract:
        continue
    model_modules = sorted(set(m for f in Model._fields.values() for m in (f._modules or ())))
    facts['models'][model_name] = {
        'kind': 'transient' if Model._transient else 'model', 'description': Model._description or model_name,
        'modules': model_modules, 'custom': bool(set(model_modules) & CUSTOM),
    }
    fields = {}
    for name, f in Model._fields.items():
        mods = sorted(f._modules or ())
        compute = f.compute if isinstance(f.compute, str) else (fname_of(f.compute) if f.compute else '')
        related = f.related if isinstance(f.related, str) else '.'.join(f.related or ())
        fields[name] = {
            'type': f.type, 'string': f.string, 'stored': bool(f.store), 'compute': compute or '',
            'related': related or '', 'comodel': getattr(f, 'comodel_name', '') or '',
            'modules': mods, 'loc': [], 'custom': bool(set(mods) & CUSTOM),
        }
    facts['fields'][model_name] = fields

# field triggers: exactly what Odoo uses to mark fields to recompute
triggers = getattr(registry, '_field_triggers', None)
if triggers is None:
    triggers = registry.field_triggers
for dep_field, tree in triggers.items():
    for path, targets in tree.items():
        via = '.'.join(f.name for f in path)
        for target in targets:
            how = 'related' if target.related else 'compute'
            fn = target.compute if isinstance(target.compute, str) else ''
            facts['field_edges'].append([f'{dep_field.model_name}.{dep_field.name}',
                                         f'{target.model_name}.{target.name}', how, via, fn])

for model_name in facts['models']:
    records = env[model_name]
    try:
        for method in records._constraint_methods:
            names = method._constrains
            if callable(names):
                names = names(records)
            for name in names:
                if name.split('.')[0] in records._fields:
                    facts['constraints'].append([f"{model_name}.{name.split('.')[0]}", model_name, fname_of(method)])
    except Exception as exc:  # noqa: BLE001 -- keep dumping other models
        print('constraints', model_name, exc)
    try:
        for name, methods in records._onchange_methods.items():
            if name in records._fields:
                for method in methods:
                    facts['onchanges'].append([f'{model_name}.{name}', model_name, fname_of(method)])
    except Exception as exc:  # noqa: BLE001
        print('onchanges', model_name, exc)


def xmlid_map(records):
    try:
        return {k: (v[0] if isinstance(v, list) and v else v) for k, v in records._get_external_ids().items()}
    except Exception:  # noqa: BLE001
        return {}


def combined_arch(view):
    from lxml import etree
    if hasattr(view, '_get_combined_arch'):
        return view._get_combined_arch()
    return etree.fromstring(view.read_combined(['arch'])['arch'])


Views = env['ir.ui.view']
views = Views.search([('mode', '=', 'primary'), ('type', 'in', ('form', 'tree', 'list', 'kanban', 'search', 'pivot', 'graph',
                                                                  'calendar', 'activity', 'gantt', 'cohort', 'map')),
                      ('model', 'in', sorted(facts['models']))])
view_xmlids = xmlid_map(views)
ACTION_REF = re.compile(r'^\d+$')
view_ids_seen = {}

for view in views:
    try:
        arch = combined_arch(view)
    except Exception as exc:  # noqa: BLE001 -- broken inherited view in this database
        print('skip view', view.id, exc)
        continue
    items = []

    def walk(el, model_name):
        Model = registry.get(model_name)
        for child in el:
            if not isinstance(child.tag, str):
                continue
            if child.tag == 'field' and Model is not None and child.get('name') in Model._fields:
                field = Model._fields[child.get('name')]
                items.append(['field', model_name, field.name, child.get('string') or ''])
                if getattr(field, 'comodel_name', None) and len(child):
                    walk(child, field.comodel_name)
                    continue
            elif child.tag == 'button' and child.get('name') and child.get('type') in ('object', 'action'):
                name = child.get('name')
                action_xmlid = None
                if child.get('type') == 'action' and ACTION_REF.match(name):
                    action_xmlid = f'#{name}'
                items.append(['button', model_name, name, child.get('type'), action_xmlid,
                              child.get('string') or child.get('title') or name])
            walk(child, model_name)

    walk(arch, view.model)
    extension_modules = set()
    for child in view.inherit_children_ids:
        for xid in (xmlid_map(child).get(child.id) or '',):
            extension_modules.add(xid.split('.')[0])
    own = view_xmlids.get(view.id) or ''
    facts['views'].append({
        'id': f'view:{own or view.id}', 'xmlid': own, 'model': view.model,
        'type': 'tree' if view.type == 'list' else view.type, 'name': view.name, 'priority': view.priority,
        'custom': bool(({own.split('.')[0]} | extension_modules) & CUSTOM), 'items': items, 'loc': '',
    })
    view_ids_seen[("view", view.id)] = f'view:{own or view.id}'

view_id_map = {k[1]: v for k, v in view_ids_seen.items()}


def action_node_id(action):
    xid = xmlid_map(action).get(action.id)
    return f'action:{xid}' if xid else f'action:#{action.id}'


acts = env['ir.actions.act_window'].search([('res_model', 'in', sorted(facts['models']))])
for act in acts:
    vids = [view_id_map[i] for i in (act.view_ids.mapped('view_id').ids + act.view_id.ids + act.search_view_id.ids) if i in view_id_map]
    if not vids:
        for mode in (act.view_mode or '').split(','):
            mode = mode.strip()
            default = Views.search([('model', '=', act.res_model), ('type', 'in', (mode, 'list' if mode == 'tree' else mode)),
                                    ('mode', '=', 'primary')], order='priority, id', limit=1)
            if default.id in view_id_map:
                vids.append(view_id_map[default.id])
    xid = xmlid_map(act).get(act.id) or ''
    facts['actions'].append({
        'id': action_node_id(act), 'xmlid': xid, 'type': 'act_window', 'name': act.name, 'model': act.res_model,
        'target': act.target or '', 'binding': act.binding_model_id.model or '', 'views': vids, 'calls': [],
        'custom': xid.split('.')[0] in CUSTOM,
    })

server_model = 'ir.actions.server'
for act in env[server_model].search([('model_id.model', 'in', sorted(facts['models']))]):
    xid = xmlid_map(act).get(act.id) or ''
    facts['actions'].append({
        'id': action_node_id(act), 'xmlid': xid, 'type': 'server_action', 'name': act.name, 'model': act.model_id.model,
        'binding': act.binding_model_id.model or '', 'views': [], 'calls': sorted(set(CODE_CALL.findall(act.code or ''))),
        'custom': xid.split('.')[0] in CUSTOM,
    })

for rep in env['ir.actions.report'].search([('model', 'in', sorted(facts['models']))]):
    xid = xmlid_map(rep).get(rep.id) or ''
    facts['actions'].append({'id': action_node_id(rep), 'xmlid': xid, 'type': 'report', 'name': rep.name,
                             'model': rep.model, 'views': [], 'calls': [], 'custom': xid.split('.')[0] in CUSTOM})

crons = env['ir.cron'].with_context(active_test=False).search([])
for cron in crons:
    model = cron.model_id.model if 'model_id' in cron._fields else cron.model
    if model not in facts['models']:
        continue
    code = (cron.code if 'code' in cron._fields else '') or ''
    calls = set(CODE_CALL.findall(code))
    if 'function' in cron._fields and cron.function:
        calls.add(cron.function)
    xid = xmlid_map(cron).get(cron.id) or ''
    facts['crons'].append({'id': f'cron:{xid or cron.id}', 'xmlid': xid, 'name': cron.name, 'model': model,
                           'calls': sorted(calls), 'custom': xid.split('.')[0] in CUSTOM})

if 'base.automation' in env:
    for auto in env['base.automation'].with_context(active_test=False).search([]):
        trigger_fields = [f'{f.model}.{f.name}' for f in auto.trigger_field_ids] if 'trigger_field_ids' in auto._fields else []
        if 'action_server_ids' in auto._fields:
            acts = [action_node_id(a) for a in auto.action_server_ids]
        else:
            acts = [action_node_id(auto.action_server_id)] if 'action_server_id' in auto._fields else []
        facts['automations'].append({'id': f'automation:{auto.id}', 'name': auto.name, 'model': auto.model_id.model,
                                     'trigger_fields': trigger_fields, 'actions': acts})

for menu in env['ir.ui.menu'].with_context(active_test=False).search([('action', '!=', False)]):
    xid = xmlid_map(menu).get(menu.id) or ''
    facts['menus'].append({'id': f'menu:{xid or menu.id}', 'xmlid': xid, 'name': menu.complete_name,
                           'action': action_node_id(menu.action), 'custom': xid.split('.')[0] in CUSTOM})

# buttons `type="action"` carry a database id: map it to the action node id
all_actions = env['ir.actions.actions'].search([])
typed = {a.id: env[a.type].browse(a.id) for a in all_actions if a.type in env}
for v in facts['views']:
    for item in v['items']:
        if item[0] == 'button' and item[4] and item[4].startswith('#'):
            rec = typed.get(int(item[4][1:]))
            if rec is not None:
                xid = xmlid_map(rec).get(rec.id)
                item[4] = xid or item[4]

with open(OUT, 'w', encoding='utf-8') as fh:
    json.dump(facts, fh)
print(f"facts: {len(facts['models'])} models, {len(facts['field_edges'])} field edges, {len(facts['views'])} views -> {OUT}")
