import io
import json
import os
import shutil
import subprocess

import pytest

from odoo_impact_graph import builder, static_facts
from odoo_impact_graph.htmlexport import write_html
from odoo_impact_graph.mcp_server import serve
from odoo_impact_graph.query import Graph, report

HERE = os.path.dirname(__file__)
CORE = os.path.join(HERE, 'fixtures', 'core')
CUSTOM = os.path.join(HERE, 'fixtures', 'custom')


def build(custom=CUSTOM, keep_all=False):
    facts = static_facts.build([custom], core_paths=[CORE], log=lambda *a: None)
    return facts, Graph(builder.build_graph(facts, keep_all=keep_all))


@pytest.fixture(scope='module')
def graph():
    return build()[1]


def edges(g, kind=None):
    return {(e['s'], e['t'], e.get('via', '')) for e in g.edges if kind is None or e['k'] == kind}


def test_core_and_custom_modules():
    facts, _ = build()
    assert facts['modules']['shop']['custom'] is False
    assert facts['modules']['shop_discount']['custom'] is True


def test_compute_dependencies_like_odoo_trigger_tree(graph):
    compute = edges(graph, 'compute')
    # direct and through a one2many (path walked back via line_ids)
    assert ('shop.order.line.subtotal', 'shop.order.amount_total', 'line_ids') in compute
    assert ('shop.order.line_ids', 'shop.order.amount_total', '') in compute
    # the inverse many2one of the one2many also triggers (a line moved to another order)
    assert ('shop.order.line.order_id', 'shop.order.amount_total', 'line_ids') in compute
    # @api.depends(*CONSTANT) from the custom module
    assert ('shop.order.discount', 'shop.order.amount_net', '') in compute
    assert ('shop.order.amount_total', 'shop.order.amount_net', '') in compute


def test_related_field_triggers_each_hop():
    related = edges(build(keep_all=True)[1], 'related')
    assert ('shop.order.partner_id', 'shop.order.partner_city', '') in related
    assert ('shop.partner.city', 'shop.order.partner_city', 'partner_id') in related


def test_constraint_hook_and_writes(graph):
    assert ('shop.order.discount', 'shop.order::_check_discount', '') in edges(graph, 'constrains')
    assert ('shop.order.discount', 'shop.order::write', '') in edges(graph, 'write_hook')
    writes = edges(graph, 'writes')
    assert ('shop.order::action_apply_discount', 'shop.order.discount', '') in writes
    assert ('shop.discount.wizard::action_apply', 'shop.order.discount', '') in writes  # self.env['shop.order']
    assert ('shop.order::_cron_reset_discount', 'shop.order.discount', '') in writes  # order.discount = 0


def test_combined_view_and_ui_chain(graph):
    shows = edges(graph, 'shows')
    form = 'view:shop.view_order_form'
    assert (form, 'shop.order.discount', '') in shows  # added by the extension view
    assert (form, 'shop.order.line.subtotal', '') in shows  # inside the one2many subview
    assert (form, 'shop.order.line.order_id', '') not in shows
    assert ('menu:shop.menu_orders', 'action:shop.action_orders', '') in edges(graph, 'opens')
    assert ('action:shop.action_orders', form, '') in edges(graph, 'uses_view')
    buttons = {n['name'] for n in graph.nodes.values() if n['type'] == 'button'}
    assert {'action_confirm', 'action_apply_discount', 'shop_discount.action_discount_wizard'} <= buttons
    opens = edges(graph, 'opens')
    assert any(t == 'action:shop_discount.action_discount_wizard' for _, t, _ in opens)


def test_impact_of_a_field(graph):
    r = graph.impact('shop.order.line.price')
    assert {'shop.order.line.subtotal', 'shop.order.amount_total', 'shop.order.amount_net'} <= set(r['reached'])
    assert 'menu:shop.menu_orders' in r['ui']
    text = report(graph, 'impact', r)
    assert 'Where the user sees the effect' in text and 'menu Shop/Orders' in text


def test_who_changes_a_field(graph):
    r = graph.who('shop.order.discount')
    reached = set(r['reached'])
    assert {'shop.order::action_apply_discount', 'shop.discount.wizard::action_apply',
            'shop.order::_cron_reset_discount'} <= reached
    assert any(graph.nodes[n]['type'] == 'button' and graph.nodes[n]['name'] == 'action_apply_discount' for n in reached)
    assert 'cron:shop_discount.cron_reset_discount' in reached
    assert 'menu:shop.menu_orders' in r['ui']


def test_from_a_menu(graph):
    r = graph.reach('Shop/Orders')
    assert 'shop.order.discount' in r['reached']  # menu -> action -> form -> button -> method -> field


def test_plan_lists_writers_and_screens(graph):
    r = graph.plan(['shop.order.discount'])
    assert 'shop.order.amount_net' in r['reached']
    assert 'shop.order::_check_discount' in r['reached']
    assert 'shop.order::action_apply_discount' in r['writers']
    assert 'view:shop.view_order_form' in r['ui']


def test_unknown_reference_suggests(graph):
    with pytest.raises(KeyError) as err:
        graph.impact('shop.order.discont')
    assert 'not found' in err.value.args[0]


def test_diff_maps_changed_lines(tmp_path):
    work = tmp_path / 'addons'
    shutil.copytree(CUSTOM, work)
    git = ['git', '-C', str(work), '-c', 'user.name=t', '-c', 'user.email=t@example.com']
    subprocess.run(git[:3] + ['init', '-q'], check=True)
    subprocess.run(git + ['add', '.'], check=True)
    subprocess.run(git + ['commit', '-qm', 'init'], check=True)
    path = work / 'shop_discount' / 'models' / 'shop_order.py'
    path.write_text(path.read_text().replace("self.write({'discount': 10})", "self.write({'discount': 15})"))
    _, g = build(str(work))
    r = g.diff(str(work), 'HEAD')
    assert r['touched'] == {'shop.order::action_apply_discount'}
    assert 'shop.order.amount_net' in r['reached']
    assert 'menu:shop.menu_orders' in r['ui']


def test_html_and_mcp(tmp_path, graph):
    data = builder.build_graph(static_facts.build([CUSTOM], core_paths=[CORE], log=lambda *a: None))
    gpath = tmp_path / 'graph.json'
    gpath.write_text(json.dumps(data))
    html = write_html(str(gpath), str(tmp_path / 'graph.html'))
    assert 'shop.order.discount' in open(html).read()

    msgs = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}},
        {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
        {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
         'params': {'name': 'impact_plan_change', 'arguments': {'targets': ['shop.order.discount']}}},
        {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
         'params': {'name': 'impact_of_change', 'arguments': {'target': 'nope.nothing'}}},
    ]
    out = io.StringIO()
    serve(str(gpath), io.StringIO('\n'.join(json.dumps(m) for m in msgs) + '\n'), out)
    replies = {r['id']: r for r in map(json.loads, out.getvalue().splitlines())}
    assert set(replies) == {1, 2, 3, 4}
    assert replies[1]['result']['serverInfo']['name'] == 'odoo-impact-graph'
    assert 'impact_plan_change' in {t['name'] for t in replies[2]['result']['tools']}
    assert replies[3]['result']['isError'] is False
    assert 'shop.order.amount_net' in replies[3]['result']['content'][0]['text']
    assert replies[4]['result']['isError'] is True


def test_from_conf_rebuild_and_mcp_rebuild(tmp_path):
    import sys
    from odoo_impact_graph import cli
    conf = tmp_path / 'odoo.conf'
    conf.write_text(f'[options]\naddons_path = {CORE}, {CUSTOM}\n')
    out = tmp_path / 'g' / 'graph.json'
    # both folders come from the conf: mark custom explicitly since the fixture core has no odoo-bin
    cli.main(['build', '--from-conf', str(conf), '--custom', CUSTOM, '-o', str(out), '--no-html'])
    meta = json.loads(out.read_text())['meta']
    assert meta['build']['paths'] == [CORE, CUSTOM]
    assert meta['custom_modules'] == ['shop_discount']
    before = out.stat().st_mtime_ns
    cli.main(['rebuild', '-g', str(out), '--no-html'])
    assert out.stat().st_mtime_ns >= before
    reply = io.StringIO()
    msg = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'impact_rebuild', 'arguments': {}}}
    serve(str(out), io.StringIO(json.dumps(msg) + '\n'), reply)
    result = json.loads(reply.getvalue())['result']
    assert result['isError'] is False and 'graph:' in result['content'][0]['text']
    assert reply.getvalue().count('\n') == 1  # build logs did not leak into the JSON-RPC stream


def test_web_serves_the_explorer(tmp_path):
    import sys
    import time
    import urllib.request
    data = builder.build_graph(static_facts.build([CUSTOM], core_paths=[CORE], log=lambda *a: None))
    gpath = tmp_path / 'graph.json'
    gpath.write_text(json.dumps(data))
    proc = subprocess.Popen([sys.executable, '-m', 'odoo_impact_graph.cli', 'web', '-g', str(gpath),
                             '--port', '0', '--no-open'], stdout=subprocess.PIPE, text=True)
    try:
        line = proc.stdout.readline()
        url = line.split()[3]
        page = urllib.request.urlopen(url, timeout=5).read().decode()
        assert 'Odoo Impact' in page and 'shop.order.discount' in page
        with pytest.raises(Exception):
            urllib.request.urlopen(url + 'graph.json', timeout=5)  # only the page is served
    finally:
        proc.terminate()
        proc.wait(5)
