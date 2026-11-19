"""One build entry point shared by the CLI, the service and the MCP server."""
import json
import os
import time

from . import builder, gitinfo, static_facts


def _abs(paths):
    return [os.path.abspath(os.path.expanduser(p)) for p in paths or []]


def save(graph, out, html=True, build=None, log=print):
    meta = graph['meta']
    if build:
        meta['build'] = build  # lets `rebuild`, the service and the MCP server redo the same build
        anchor = (build.get('custom') or build.get('paths') or [None])[0]
        meta['git'] = gitinfo.state(anchor) if anchor else None
    meta['built_at'] = time.time()
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    tmp = out + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(graph, fh, separators=(',', ':'))
    os.replace(tmp, out)  # readers (MCP, web) never see a half-written file
    log(f"graph: {len(graph['nodes'])} nodes, {len(graph['edges'])} edges -> {out}")
    if html:
        from .htmlexport import write_html
        log('html: ' + write_html(out, os.path.splitext(out)[0] + '.html'))
    return out


def build_static(paths, core=(), custom=(), modules=None, keep_all=False, output=None, html=True, log=print):
    paths, core, custom = _abs(paths), _abs(core), _abs(custom)
    for p in paths + core:
        if not os.path.isdir(p):
            raise ValueError(f'not a folder: {p}')
    wanted = [m.strip() for m in modules.split(',')] if isinstance(modules, str) and modules else modules
    facts = static_facts.build(paths, core, custom, wanted, log=log)
    if facts['unresolved_depends']:
        log(f"{len(facts['unresolved_depends'])} @api.depends paths could not be resolved "
            f"(module not scanned?), e.g. {facts['unresolved_depends'][0]}")
    build = {'cmd': 'build', 'paths': paths, 'core': core, 'custom': custom,
             'modules': ','.join(wanted) if wanted else None, 'all': keep_all}
    return save(builder.build_graph(facts, keep_all=keep_all), output, html, build, log)
