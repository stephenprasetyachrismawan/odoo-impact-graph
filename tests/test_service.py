import io
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

import pytest

from odoo_impact_graph import service
from odoo_impact_graph.mcp_server import Server

HERE = os.path.dirname(__file__)
CORE = os.path.join(HERE, 'fixtures', 'core')
CUSTOM = os.path.join(HERE, 'fixtures', 'custom')


def git(path, *args):
    subprocess.run(['git', '-C', str(path), '-c', 'user.name=t', '-c', 'user.email=t@example.com', *args],
                   check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(service, 'HOME', str(tmp_path / 'home'))
    main = tmp_path / 'addons'
    shutil.copytree(CUSTOM, main)
    git(main, 'init', '-q', '-b', 'main')
    git(main, 'add', '.')
    git(main, 'commit', '-qm', 'init')
    git(main, 'worktree', 'add', '-q', '-b', 'feature', str(tmp_path / 'addons-feature'))
    conf = tmp_path / 'odoo.conf'
    conf.write_text(f'[options]\naddons_path = {CORE},{main}\n')
    service.add_project(str(main), conf=str(conf))
    return main, tmp_path / 'addons-feature'


def wait(jid):
    for _ in range(100):
        if service._jobs[jid]['state'] in ('done', 'failed'):
            return service._jobs[jid]
        time.sleep(0.05)
    raise AssertionError('build did not finish')


def test_worktrees_build_and_staleness(repo):
    main, feature = repo
    p = service.overview()[0]
    assert [w['branch'] for w in p['worktrees']] == ['main', 'feature']
    assert not p['worktrees'][1]['graph']['exists']
    # the conf points at the main checkout: building the feature worktree must scan the feature folder
    args = service.build_args(service.load_projects()[0], str(feature))
    assert args['paths'] == [os.path.realpath(feature)] and args['core'] == [os.path.realpath(CORE)]
    job = wait(service.start_build(str(feature)))
    assert job['state'] == 'done', job['log']
    st = service.graph_status(str(feature))
    assert st['exists'] and not st['stale']
    py = feature / 'shop_discount' / 'models' / 'shop_order.py'
    py.write_text(py.read_text().replace("'discount': 10", "'discount': 20"))
    assert service.graph_status(str(feature))['reason'].startswith('uncommitted')
    git(feature, 'commit', '-qam', 'change')
    assert service.graph_status(str(feature))['reason'] == 'new commits since the build'


def test_http_api(repo):
    main, feature = repo
    from http.server import ThreadingHTTPServer
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), service.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{httpd.server_address[1]}'
    try:
        assert 'Odoo Impact' in urllib.request.urlopen(base + '/').read().decode()
        data = json.loads(urllib.request.urlopen(base + '/api/projects').read())
        gid = data['projects'][0]['worktrees'][1]['gid']
        body = json.dumps({'gid': gid}).encode()
        with pytest.raises(urllib.error.HTTPError) as err:  # no custom header = cross-site attempt
            urllib.request.urlopen(urllib.request.Request(base + '/api/build', body, {'Content-Type': 'application/json'}))
        assert err.value.code == 403
        req = urllib.request.Request(base + '/api/build', body, {'Content-Type': 'application/json', 'X-Odoo-Impact': '1'})
        jid = json.loads(urllib.request.urlopen(req).read())['job']
        assert wait(jid)['state'] == 'done'
        page = urllib.request.urlopen(f'{base}/explore/{gid}').read().decode()
        assert 'shop.order.discount' in page and '← Dashboard' in page
        diff = json.loads(urllib.request.urlopen(f'{base}/api/diff/{gid}?base=HEAD').read())
        assert '0 fields/methods changed' in diff['text']
        ask = json.loads(urllib.request.urlopen(f'{base}/api/ask/{gid}?kind=who&ref=shop.order.discount').read())
        assert 'action_apply_discount' in ask['text']
        with pytest.raises(urllib.error.HTTPError) as err:  # DNS rebinding: foreign Host header
            urllib.request.urlopen(urllib.request.Request(base + '/api/projects', headers={'Host': 'evil.example'}))
        assert err.value.code == 403
    finally:
        httpd.shutdown()


def test_mcp_auto_builds_the_current_worktree(repo):
    main, feature = repo
    srv = Server(service.graph_path(str(feature)), worktree=str(feature))
    first = srv.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                        'params': {'name': 'impact_find', 'arguments': {'query': 'discount'}}})
    assert first['result']['isError'] and 'impact_rebuild' in first['result']['content'][0]['text']
    built = srv.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                        'params': {'name': 'impact_rebuild', 'arguments': {}}})
    assert not built['result']['isError'], built
    found = srv.handle({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                        'params': {'name': 'impact_find', 'arguments': {'query': 'discount'}}})
    assert 'shop.order.discount' in found['result']['content'][0]['text']
