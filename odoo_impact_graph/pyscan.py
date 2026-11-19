"""Read Odoo model classes from Python source with `ast` (no Odoo import needed)."""
import ast
import os

FIELD_TYPES = {
    'Char', 'Text', 'Html', 'Integer', 'Float', 'Monetary', 'Boolean', 'Date', 'Datetime', 'Selection',
    'Binary', 'Image', 'Many2one', 'One2many', 'Many2many', 'Many2oneReference', 'Reference', 'Json',
    'Properties', 'PropertiesDefinition', 'Id',
}
RELATIONAL = {'Many2one', 'One2many', 'Many2many'}
SKIP_DIRS = {'tests', 'migrations', 'upgrades', 'static', '__pycache__', 'node_modules'}
KIND_BY_BASE = {'Model': 'model', 'TransientModel': 'transient', 'AbstractModel': 'abstract'}


def _const(node):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple)):
        values = [_const(e) for e in node.elts]
        return values if all(v is not None for v in values) else None
    return None


def _name_of(node):
    """'x' for x / self.x / module.x."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _strings(node, consts):
    """List of strings of a constant expression: 'a', ('a', 'b'), NAME, NAME + ('c',)."""
    v = _const(node)
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [x for x in v if isinstance(x, str)]
    if isinstance(node, ast.Name):
        return list(consts.get(node.id, []))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _strings(node.left, consts) + _strings(node.right, consts)
    if isinstance(node, ast.Starred):
        return _strings(node.value, consts)
    return []


def _collect_consts(body, consts):
    """NAME = ('a', 'b') / NAME = OTHER + (...) at module or class level."""
    for stmt in body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            values = _strings(stmt.value, consts)
            if values and not isinstance(_const(stmt.value), str):
                consts[stmt.targets[0].id] = values


def _decorator(dec, consts):
    """('depends', ['a', 'b.c']) for @api.depends('a', 'b.c') or @api.depends(*NAMES)."""
    if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and isinstance(dec.func.value, ast.Name) \
            and dec.func.value.id == 'api':
        args = []
        for a in dec.args:
            args.extend(_strings(a, consts))
        return dec.func.attr, args
    return None, None


def _field_call(value, lits=None):
    """FieldType, positional constants, keyword values for fields.X(...).
    lits: module-level list constants (selection=SALE_ORDER_STATE)."""
    lits = lits or {}
    if not isinstance(value, ast.Call):
        return None
    func = value.func
    ftype = None
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == 'fields':
        ftype = func.attr
    elif isinstance(func, ast.Name) and func.id in FIELD_TYPES:
        ftype = func.id
    if ftype not in FIELD_TYPES:
        return None
    pos = [_const(a) if not isinstance(a, ast.Name) else lits.get(a.id) for a in value.args]
    kw = {}
    for k in value.keywords:
        if k.arg is None:
            continue
        v = _const(k.value)
        if v is None and isinstance(k.value, ast.Name) and k.value.id in lits:
            v = lits[k.value.id]
        if v is None and k.arg in ('compute', 'inverse', 'search', 'default', 'selection'):
            v = _name_of(k.value)  # compute=_compute_x (function object)
        kw[k.arg] = v
    attrs = {'type': ftype}
    if ftype in RELATIONAL:
        if pos and isinstance(pos[0], str):
            attrs['comodel'] = pos[0]
        if ftype == 'One2many' and len(pos) > 1 and isinstance(pos[1], str):
            attrs['inverse_name'] = pos[1]
        string_pos = {'Many2one': 1, 'One2many': 2, 'Many2many': 4}[ftype]
        if len(pos) > string_pos and isinstance(pos[string_pos], str):
            attrs['string'] = pos[string_pos]
    elif ftype in ('Selection', 'Reference'):
        if len(pos) > 1 and isinstance(pos[1], str):
            attrs['string'] = pos[1]
        if pos and isinstance(pos[0], list):
            attrs['selection'] = _pairs(pos[0])
    elif pos and isinstance(pos[0], str):
        attrs['string'] = pos[0]
    for key, target in (('comodel_name', 'comodel'), ('inverse_name', 'inverse_name'), ('string', 'string'),
                        ('compute', 'compute'), ('related', 'related'), ('store', 'store'),
                        ('inverse', 'inverse'), ('depends', 'depends'), ('readonly', 'readonly'),
                        ('required', 'required')):
        if key in kw and kw[key] is not None:
            attrs[target] = kw[key]
    if isinstance(attrs.get('depends'), str):
        attrs['depends'] = [attrs['depends']]
    if isinstance(kw.get('selection'), list):
        attrs['selection'] = _pairs(kw['selection'])
    if isinstance(kw.get('selection_add'), list):
        attrs['selection_add'] = _pairs(kw['selection_add'], allow_single=True)
    if isinstance(kw.get('default'), str):
        attrs['default'] = kw['default']  # initial state of a workflow selection
    for key in ('groups', 'help'):
        if isinstance(kw.get(key), str):
            attrs[key] = kw[key]
    return attrs


def _pairs(items, allow_single=False):
    """[['draft', 'Draft'], ...] from a selection literal (single-item tuples are position markers)."""
    out = []
    for item in items:
        if isinstance(item, list) and len(item) >= 2 and isinstance(item[0], str):
            out.append([item[0], item[1] if isinstance(item[1], str) else item[0]])
        elif allow_single and isinstance(item, list) and len(item) == 1 and isinstance(item[0], str):
            out.append([item[0], None])
    return out


def _env_model(expr):
    """'x.y' when expr is self.env['x.y'] (also behind .sudo() / .with_context(...) / .browse(...))."""
    while isinstance(expr, (ast.Call, ast.Attribute)) and not (isinstance(expr, ast.Attribute) and expr.attr == 'env'):
        expr = expr.func if isinstance(expr, ast.Call) else expr.value
        if isinstance(expr, ast.Subscript):
            break
    if isinstance(expr, ast.Subscript) and isinstance(expr.value, ast.Attribute) and expr.value.attr == 'env':
        v = _const(expr.slice)
        return v if isinstance(v, str) else None
    return None


def _dict_keys(node):
    return [k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]


def scan_body(fn):
    """What a method body touches, by name (resolved against fields later)."""
    vals_keys, own_writes, other_writes, calls, opens_models, opens_refs = set(), set(), set(), set(), set(), set()
    value_writes = set()  # (field, constant) written on own records: state transitions
    returned_values, writes_result_of = set(), set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Constant) and isinstance(node.left.value, str) \
                and any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
            vals_keys.add(node.left.value)  # 'x' in vals
        elif isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and 'val' in node.value.id:
            v = _const(node.slice)
            if isinstance(v, str):
                vals_keys.add(v)  # vals['x']
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            calls.add(attr)
            if attr == 'get' and node.args and isinstance(node.func.value, ast.Name) and 'val' in node.func.value.id:
                v = _const(node.args[0])
                if isinstance(v, str):
                    vals_keys.add(v)  # vals.get('x')
            elif attr in ('write', 'create', 'update') and node.args:
                arg = node.args[0]
                dicts = [arg] if isinstance(arg, ast.Dict) else \
                    [d for d in getattr(arg, 'elts', []) if isinstance(d, ast.Dict)]
                target = _env_model(node.func.value)
                for d in dicts:
                    if target:
                        other_writes.update((target, k) for k in _dict_keys(d))
                    else:
                        own_writes.update(_dict_keys(d))
                        for k, v in zip(d.keys, d.values):
                            if isinstance(k, ast.Constant) and isinstance(k.value, str) \
                                    and isinstance(v, ast.Constant) and isinstance(v.value, str):
                                value_writes.add((k.value, v.value))
            elif attr == 'ref' and node.args:
                v = _const(node.args[0])
                if isinstance(v, str) and '.' in v:
                    opens_refs.add(v)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                    own_writes.add(target.attr)  # rec.x = ...
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                        value_writes.add((target.attr, node.value.value))
        elif isinstance(node, ast.AugAssign):
            if isinstance(node.target, ast.Attribute) and isinstance(node.target.value, ast.Name):
                own_writes.add(node.target.attr)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            # vals prepared here and written by a caller: self.write(self._prepare_confirmation_values())
            for k, v in zip(node.value.keys, node.value.values):
                if isinstance(k, ast.Constant) and isinstance(k.value, str) \
                        and isinstance(v, ast.Constant) and isinstance(v.value, str):
                    returned_values.add((k.value, v.value))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ('write', 'update') \
                and node.args and isinstance(node.args[0], ast.Call) and isinstance(node.args[0].func, ast.Attribute) \
                and isinstance(node.args[0].func.value, ast.Name) and node.args[0].func.value.id == 'self':
            writes_result_of.add(node.args[0].func.attr)
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value == 'res_model':
                    model = _const(v)
                    if isinstance(model, str):
                        opens_models.add(model)
    return {
        'vals_keys': sorted(vals_keys), 'own_writes': sorted(own_writes),
        'other_writes': sorted(other_writes), 'calls': sorted(calls),
        'opens_models': sorted(opens_models), 'opens_refs': sorted(opens_refs),
        'value_writes': sorted(value_writes), 'returned_values': sorted(returned_values),
        'writes_result_of': sorted(writes_result_of),
    }


def _class_info(cls):
    info = {'name': None, 'inherit': [], 'inherits': {}, 'kind': None, 'description': None, 'rec_name': None}
    for base in cls.bases:
        kind = KIND_BY_BASE.get(_name_of(base))
        if kind:
            info['kind'] = kind
    for stmt in cls.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            key, value = stmt.targets[0].id, _const(stmt.value)
            if key == '_name' and isinstance(value, str):
                info['name'] = value
            elif key == '_inherit':
                info['inherit'] = [value] if isinstance(value, str) else [v for v in (value or []) if isinstance(v, str)]
            elif key == '_inherits' and isinstance(stmt.value, ast.Dict):
                info['inherits'] = {
                    k.value: v.value for k, v in zip(stmt.value.keys, stmt.value.values)
                    if isinstance(k, ast.Constant) and isinstance(v, ast.Constant)
                }
            elif key == '_description' and isinstance(value, str):
                info['description'] = value
            elif key == '_rec_name' and isinstance(value, str):
                info['rec_name'] = value
            elif key == '_transient' and value is True:
                info['kind'] = 'transient'
            elif key == '_abstract' and value is True:
                info['kind'] = 'abstract'
    info['model'] = info['name'] or (info['inherit'][0] if info['inherit'] else None)
    return info


def scan_module(module_name, module_path, repo_root=None, with_bodies=True):
    """List of model classes found in the module's Python files."""
    classes = []
    for dirpath, dirnames, filenames in os.walk(module_path):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for filename in sorted(filenames):
            if not filename.endswith('.py') or filename in ('__manifest__.py', '__openerp__.py'):
                continue
            path = os.path.join(dirpath, filename)
            try:
                with open(path, encoding='utf-8') as fh:
                    tree = ast.parse(fh.read(), filename=path)
            except (SyntaxError, UnicodeDecodeError, ValueError):
                continue
            rel = os.path.relpath(path, repo_root) if repo_root else path
            module_consts = {}
            _collect_consts(tree.body, module_consts)
            lits = {st.targets[0].id: _const(st.value) for st in tree.body
                    if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name)
                    and isinstance(st.value, (ast.List, ast.Tuple)) and _const(st.value) is not None}
            for cls in ast.walk(tree):
                if not isinstance(cls, ast.ClassDef):
                    continue
                info = _class_info(cls)
                if not info['model'] or (info['kind'] is None and not info['name'] and not info['inherit']):
                    continue
                info.update(module=module_name, file=rel, line=cls.lineno, fields={}, methods={})
                consts = dict(module_consts)
                _collect_consts(cls.body, consts)
                for stmt in cls.body:
                    target = value = None
                    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                        target, value = stmt.targets[0].id, stmt.value
                    elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                        target, value = stmt.target.id, stmt.value
                    if target and value is not None:
                        attrs = _field_call(value, lits)
                        if attrs:
                            attrs['loc'] = f'{rel}:{stmt.lineno}-{stmt.end_lineno}'
                            info['fields'][target] = attrs
                    elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        meth = {'loc': f'{rel}:{stmt.lineno}-{stmt.end_lineno}', 'depends': [],
                                'constrains': [], 'onchange': []}
                        for dec in stmt.decorator_list:
                            kind, args = _decorator(dec, consts)
                            if kind in ('depends', 'constrains', 'onchange'):
                                meth[kind].extend(args)
                        if with_bodies:
                            meth.update(scan_body(stmt))
                        info['methods'][stmt.name] = meth
                classes.append(info)
    return classes
