import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request

import pytest

from odoo_impact_graph import builder, catalog, service, static_facts

HERE = os.path.dirname(__file__)
CORE = os.path.join(HERE, 'fixtures', 'core')
CUSTOM = os.path.join(HERE, 'fixtures', 'custom')


@pytest.fixture(scope='module')
def cat():
    facts = static_facts.build([CUSTOM], core_paths=[CORE], log=lambda *a: None)
    return builder.build_graph(facts)['catalog']


def test_class_diagram_data(cat):
    order = cat['models']['shop.order']
    assert order['own'] is False and order['custom'] is True  # core model extended by a custom module
    assert cat['models']['shop.discount.wizard']['own'] is True
    assert cat['models']['shop.discount.wizard']['kind'] == 'transient'
    assert order['fields']['line_ids']['type'] == 'one2many' and order['fields']['line_ids']['inverse_name'] == 'order_id'
    assert order['fields']['state']['selection'] == [['draft', 'Draft'], ['sent', 'Sent'], ['done', 'Done']]
    assert 'shop.order' in cat['modules']['shop']['defines'] and 'shop.order' in cat['modules']['shop_discount']['models']


def test_use_case_data(cat):
    assert cat['groups']['shop.group_shop_manager']['name'] == 'Sales / Shop Manager'  # category from the manifest
    menu = next(m for m in cat['menus'] if m['id'] == 'menu:shop.menu_orders')
    assert menu['model'] == 'shop.order' and menu['groups'] == ['shop.group_shop_user']
    confirm = next(b for b in cat['models']['shop.order']['buttons'] if b['name'] == 'action_confirm')
    assert confirm['groups'] == ['shop.group_shop_manager']
    assert {'group': 'shop.group_shop_user', 'perms': 'rwc'} in cat['models']['shop.order']['access']


def test_activity_data(cat):
    wf = next(w for w in cat['models']['shop.order']['workflows'] if w['field'] == 'state')
    assert wf['initial'] == 'draft'
    t = {(tuple(x['from'] or ()), x['to'], x['label'], x['kind']) for x in wf['transitions']}
    # button visible only in draft (invisible="state != 'draft'"), writes through a helper's returned vals
    assert (('draft',), 'done', 'Confirm', 'button') in t
    # selection_add 'sent' from the custom module, set by a button visible in draft (rec.state = 'sent')
    assert (('draft',), 'sent', 'Send', 'button') in t
    # a cron writes the state with rec.state = ...
    assert ((), 'draft', '_cron_reset_discount', 'cron') in t


@pytest.mark.parametrize('cond,expected', [
    ({'invisible': "state != 'draft'"}, ['draft']),
    ({'invisible': "state not in ('draft', 'sent')"}, ['draft', 'sent']),
    ({'invisible': "state in ['done']"}, ['draft', 'sent']),
    ({'invisible': "state == 'done' or partner_id"}, ['draft', 'sent']),
    ({'invisible': "state == 'done' and partner_id"}, None),
    ({'states': 'draft,sent'}, ['draft', 'sent']),
    ({'attrs': "{'invisible': [('state', 'not in', ['sent'])]}"}, ['sent']),
    ({}, None),
])
def test_visible_states(cond, expected):
    assert catalog.visible_states(cond, 'state', ['draft', 'sent', 'done']) == expected


def git(path, *args):
    subprocess.run(['git', '-C', str(path), '-c', 'user.name=t', '-c', 'user.email=t@example.com', *args],
                   check=True, capture_output=True)


def test_branch_build_from_git_and_diagram_api(tmp_path, monkeypatch):
    monkeypatch.setattr(service, 'HOME', str(tmp_path / 'home'))
    repo = tmp_path / 'addons'
    shutil.copytree(CUSTOM, repo)
    git(repo, 'init', '-q', '-b', 'main')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'init')
    git(repo, 'checkout', '-qb', 'feature')
    py = repo / 'shop_discount' / 'models' / 'shop_order.py'
    py.write_text(py.read_text().replace("'discount': 10", "'discount': 15"))
    git(repo, 'commit', '-qam', 'more discount')
    git(repo, 'checkout', '-q', 'main')
    pid = service.add_project(str(repo), core=[CORE])
    project = service.get_project(pid)
    refs = {b['ref']: b for b in service.branches(project)}
    assert set(refs) == {'main', 'feature'} and refs['feature']['graph'] == 'none'
    out = service.build_ref(project, 'feature', log=lambda *a: None)  # no checkout: the working copy stays on main
    assert 'shop.order.discount' in open(out).read()
    assert json.load(open(out))['meta']['git']['branch'] == 'feature'
    assert service.branches(project)[0]['graph'] in ('up to date', 'none')
    key = next(b for b in service.branches(project) if b['ref'] == 'feature')['key']

    from http.server import ThreadingHTTPServer
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), service.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{httpd.server_address[1]}'
    post = lambda path, body: json.loads(urllib.request.urlopen(urllib.request.Request(
        base + path, json.dumps(body).encode(), {'Content-Type': 'application/json', 'X-Odoo-Impact': '1'})).read())
    get = lambda path: json.loads(urllib.request.urlopen(base + path).read())
    try:
        assert 'Odoo Diagram' in urllib.request.urlopen(base + '/diagram').read().decode()
        cat = get(f'/api/catalog/{pid}/{key}')['catalog']
        assert 'shop.order' in cat['models']
        q = get(f'/api/query/{pid}/{key}?kind=impact&ref=shop.order.discount')
        assert 'shop.order.amount_net' in {n['id'] for n in q['nodes']}
        d = get(f'/api/diff/{pid}/{key}?base=main')
        assert d['touched'] == ['shop.order::action_apply_discount']
        slug = post(f'/api/diagrams/{pid}', {'name': 'Discount flow', 'lens': 'activity',
                                             'nodes': [{'key': 'model:shop.order', 'x': 1, 'y': 2, 'opts': {}}]})['slug']
        assert get(f'/api/diagrams/{pid}')['diagrams'][0]['name'] == 'Discount flow'
        assert get(f'/api/diagrams/{pid}/{slug}')['nodes'][0]['key'] == 'model:shop.order'
        post(f'/api/diagrams/{pid}/{slug}/delete', {})
        assert get(f'/api/diagrams/{pid}')['diagrams'] == []
    finally:
        httpd.shutdown()
