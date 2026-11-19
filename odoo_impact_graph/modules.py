"""Find Odoo modules on disk, tell core from custom, and order them like Odoo loads them."""
import ast
import os
from dataclasses import dataclass, field

MANIFESTS = ('__manifest__.py', '__openerp__.py')
SKIP_DIRS = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', 'static', 'tests', 'migrations', 'upgrades'}


@dataclass
class Module:
    name: str
    path: str
    manifest: dict
    custom: bool = False
    depends: list = field(default_factory=list)


def read_manifest(path):
    for name in MANIFESTS:
        file = os.path.join(path, name)
        if os.path.isfile(file):
            try:
                with open(file, encoding='utf-8') as fh:
                    return ast.literal_eval(fh.read())
            except (ValueError, SyntaxError):
                return {}
    return None


def odoo_source_root(path):
    """Directory that holds odoo-bin above `path` (the Odoo source tree), or None."""
    cur = os.path.abspath(path)
    for _ in range(6):
        if os.path.isfile(os.path.join(cur, 'odoo-bin')):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def expand_roots(paths):
    """A path may be an Odoo source tree, an addons folder, a module, or a project folder.
    Returns (roots, odoo_source_roots)."""
    roots, sources = [], []
    for path in paths:
        path = os.path.abspath(os.path.expanduser(path))
        if os.path.isfile(os.path.join(path, 'odoo-bin')):
            sources.append(path)
            for sub in ('odoo/addons', 'addons'):
                if os.path.isdir(os.path.join(path, sub)):
                    roots.append(os.path.join(path, sub))
        else:
            roots.append(path)
    return roots, sources


def discover(roots, max_depth=4):
    """{name: path} of every module found below the roots; the first root wins on duplicates."""
    found = {}
    for root in roots:
        if read_manifest(root) is not None:  # the root itself is a module
            found.setdefault(os.path.basename(root.rstrip('/')), root)
            continue
        base_depth = root.rstrip('/').count(os.sep)
        for dirpath, dirnames, _ in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith('.'))
            if dirpath.count(os.sep) - base_depth >= max_depth:
                dirnames[:] = []
            for d in list(dirnames):
                full = os.path.join(dirpath, d)
                if read_manifest(full) is not None:
                    found.setdefault(d, full)
                    dirnames.remove(d)  # do not look for modules inside a module
    return found


def load_modules(paths, core_paths=(), custom_paths=()):
    """Build Module objects.

    paths        what the user picked; modules here are custom unless they sit in an Odoo source tree
                 or are Odoo Enterprise (license OEEL-1)
    core_paths   extra addons folders used only to resolve fields (odoo/addons, enterprise, OCA...)
    custom_paths force modules below these folders to count as custom
    """
    roots, sources = expand_roots(paths)
    core_roots, core_sources = expand_roots(core_paths)
    custom_paths = [os.path.abspath(p) for p in custom_paths]
    found = discover(roots + core_roots)
    core_root_set = [os.path.abspath(r) for r in core_roots]
    modules = {}
    for name, path in found.items():
        manifest = read_manifest(path) or {}
        apath = os.path.abspath(path)
        if custom_paths:
            custom = any(apath.startswith(p + os.sep) or apath == p for p in custom_paths)
        else:
            custom = not (
                odoo_source_root(apath)
                or str(manifest.get('license', '')).startswith('OEEL')
                or any(apath.startswith(r + os.sep) for r in core_root_set)
            )
        modules[name] = Module(name, path, manifest, custom, list(manifest.get('depends', [])))
    return modules, sources + core_sources


def select(modules, wanted=None):
    """Modules that would be installed: wanted (default: all custom) + their dependencies
    + auto_install modules whose dependencies are all there."""
    if wanted:
        todo = [m for m in wanted if m in modules]
    else:
        todo = [m.name for m in modules.values() if m.custom and m.manifest.get('installable', True)]
    chosen = set()
    while todo:
        name = todo.pop()
        if name in chosen or name not in modules:
            continue
        chosen.add(name)
        todo.extend(modules[name].depends)
    changed = True
    while changed:
        changed = False
        for mod in modules.values():
            auto = mod.manifest.get('auto_install')
            if mod.name in chosen or not auto or not mod.manifest.get('installable', True):
                continue
            needed = auto if isinstance(auto, list) else mod.depends
            if needed and all(d in chosen for d in needed) and all(d in modules for d in mod.depends):
                chosen.add(mod.name)
                for dep in mod.depends:
                    if dep not in chosen:
                        todo.append(dep)
                while todo:
                    name = todo.pop()
                    if name not in chosen and name in modules:
                        chosen.add(name)
                        todo.extend(modules[name].depends)
                changed = True
    return chosen


def load_order(modules, chosen):
    """Dependencies first, like Odoo's module graph (alphabetical among equals)."""
    order, done = [], set()

    def visit(name, stack=()):
        if name in done or name not in chosen or name in stack:
            return
        for dep in sorted(modules[name].depends):
            visit(dep, stack + (name,))
        done.add(name)
        order.append(name)

    if 'base' in chosen:
        visit('base')
    for name in sorted(chosen):
        visit(name)
    return order
