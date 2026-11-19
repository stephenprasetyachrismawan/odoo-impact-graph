"""Read views, actions, menus, reports and crons from a module's XML data files."""
import os
import re
import xml.etree.ElementTree as ET

REF_IN_EVAL = re.compile(r"ref\(\s*['\"]([\w.]+)['\"]\s*\)")
ACTION_REF = re.compile(r'^%\(([\w.]+)\)d$')


def _xmlid(module, xid):
    if not xid:
        return None
    return xid if '.' in xid else f'{module}.{xid}'


def _record_values(rec, module):
    """{field: value} of a <record>; value is the text, the resolved ref, or the arch element."""
    values = {}
    for fld in rec.findall('field'):
        name = fld.get('name')
        if not name:
            continue
        if name == 'arch' or fld.get('type') == 'xml':
            values[name] = fld
        elif fld.get('ref'):
            values[name] = _xmlid(module, fld.get('ref'))
        elif fld.get('eval') is not None:
            ev = fld.get('eval')
            refs = [_xmlid(module, r) for r in REF_IN_EVAL.findall(ev)]
            values[name] = refs if refs else ev.strip().strip('\'"')
            values[name + '__eval'] = ev
        else:
            values[name] = (fld.text or '').strip()
    return values


def data_files(module_path, manifest):
    files = []
    for key in ('data', 'init_xml', 'update_xml'):
        for rel in manifest.get(key, []) or []:
            if rel.endswith('.xml'):
                files.append(rel)
    return files


def scan_module(module_name, module_path, manifest, repo_root=None):
    """Raw XML records of the module, in manifest order."""
    out = {'views': [], 'actions': [], 'action_views': [], 'menus': [], 'reports': [], 'crons': [], 'server_actions': []}
    for rel in data_files(module_path, manifest):
        path = os.path.join(module_path, rel)
        if not os.path.isfile(path):
            continue
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            continue
        loc = os.path.relpath(path, repo_root) if repo_root else path
        _walk(root, module_name, loc, out, parent_menu=None)
    return out


def _walk(element, module, loc, out, parent_menu):
    for el in element:
        if not isinstance(el.tag, str):
            continue
        tag = el.tag
        if tag in ('data', 'odoo', 'openerp'):
            _walk(el, module, loc, out, parent_menu)
        elif tag == 'record':
            _record(el, module, loc, out)
        elif tag == 'menuitem':
            xid = _xmlid(module, el.get('id'))
            out['menus'].append({
                'xmlid': xid, 'name': el.get('name'), 'loc': loc,
                'parent': _xmlid(module, el.get('parent')) if el.get('parent') else parent_menu,
                'action': _xmlid(module, el.get('action')) if el.get('action') else None,
            })
            _walk(el, module, loc, out, xid)  # nested menuitems
        elif tag == 'act_window':
            out['actions'].append({
                'xmlid': _xmlid(module, el.get('id')), 'type': 'act_window', 'loc': loc,
                'name': el.get('name'), 'res_model': el.get('res_model'),
                'view_mode': el.get('view_mode') or 'tree,form', 'target': el.get('target'),
                'view_id': _xmlid(module, el.get('view_id')) if el.get('view_id') else None,
                'binding_model': el.get('binding_model') or el.get('src_model'),
            })
        elif tag == 'report':
            out['reports'].append({'xmlid': _xmlid(module, el.get('id')), 'name': el.get('string') or el.get('name'),
                                   'model': el.get('model'), 'loc': loc})


def _first(value):
    return value[0] if isinstance(value, list) and value else (value if isinstance(value, str) else None)


def _record(rec, module, loc, out):
    model = rec.get('model')
    xid = _xmlid(module, rec.get('id'))
    v = _record_values(rec, module)
    if model == 'ir.ui.view':
        arch = v.get('arch')
        if arch is None or v.get('type') == 'qweb':
            return
        root = next((c for c in arch if isinstance(c.tag, str)), None)
        if root is None or root.tag in ('t', 'template'):
            return
        out['views'].append({
            'xmlid': xid, 'name': v.get('name') or xid, 'model': v.get('model'),
            'inherit_id': _first(v.get('inherit_id')), 'mode': v.get('mode'),
            'priority': int(v['priority']) if str(v.get('priority', '')).isdigit() else 16,
            'arch': arch, 'loc': loc,
        })
    elif model == 'ir.actions.act_window':
        out['actions'].append({
            'xmlid': xid, 'type': 'act_window', 'loc': loc, 'name': v.get('name'),
            'res_model': v.get('res_model'), 'view_mode': v.get('view_mode') or 'tree,form',
            'target': v.get('target'), 'view_id': _first(v.get('view_id')),
            'search_view_id': _first(v.get('search_view_id')),
            'binding_model': _first(v.get('binding_model_id')),
            'view_ids_eval': v.get('view_ids__eval') or '',
        })
    elif model == 'ir.actions.act_window.view':
        out['action_views'].append({'action': _first(v.get('act_window_id')), 'view': _first(v.get('view_id'))})
    elif model in ('ir.actions.server', 'ir.cron'):
        item = {
            'xmlid': xid, 'loc': loc, 'name': v.get('name') or v.get('cron_name'),
            'model_ref': _first(v.get('model_id')), 'model': v.get('model'),
            'code': v.get('code') or '', 'function': v.get('function') or '',
            'binding_model': _first(v.get('binding_model_id')),
        }
        out['crons' if model == 'ir.cron' else 'server_actions'].append(item)
    elif model == 'ir.actions.report':
        out['reports'].append({'xmlid': xid, 'name': v.get('name'), 'model': v.get('model'), 'loc': loc})
    elif model == 'ir.ui.menu':
        action = v.get('action')
        out['menus'].append({
            'xmlid': xid, 'name': v.get('name'), 'loc': loc,
            'parent': _first(v.get('parent_id')),
            'action': _first(action) if isinstance(action, list) else None,
        })


def arch_items(arch_root, model, fields_of, comodel_of):
    """Fields and buttons used in an arch, with the model each belongs to.

    fields_of(model) -> set of field names, comodel_of(model, field) -> comodel or None.
    Inside an inherited arch (xpath) we do not know the target node, so a field missing
    on the view model is looked up on the comodels of that model's x2many fields.
    """
    items = []

    def lookup(name, ctx):
        if name in fields_of(ctx):
            return ctx
        hits = []
        for f in fields_of(ctx):
            co = comodel_of(ctx, f)
            if co and co != ctx and name in fields_of(co):
                hits.append(co)
        return hits[0] if len(set(hits)) == 1 else None

    def walk(el, ctx):
        for child in el:
            if not isinstance(child.tag, str):
                continue
            if child.get('position'):  # xpath / locator: its children are the new content
                if child.get('position') != 'attributes':
                    walk(child, ctx)
                continue
            if child.tag == 'field' and child.get('name'):
                owner = lookup(child.get('name'), ctx)
                if owner:
                    items.append(('field', owner, child.get('name'), child.get('string') or ''))
                    co = comodel_of(owner, child.get('name'))
                    if co and len(child):
                        walk(child, co)
                        continue
            elif child.tag == 'button' and child.get('name') and child.get('type') in ('object', 'action'):
                name = child.get('name')
                m = ACTION_REF.match(name)
                items.append(('button', ctx, name, child.get('type'), m.group(1) if m else None,
                              child.get('string') or child.get('title') or name))
            walk(child, ctx)

    walk(arch_root, model)
    return items


def view_type(arch):
    root = next((c for c in arch if isinstance(c.tag, str)), None)
    if root is None:
        return None
    if root.tag in ('data', 'xpath') or root.get('position'):
        return None  # extension
    return 'tree' if root.tag == 'list' else root.tag
