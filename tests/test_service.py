import json
import os
import shutil
import subprocess
import threading
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
    pid = service.add_project(str(main), conf=str(conf))
    return service.get_project(pid), main, tmp_path / 'addons-feature'


def test_working_copy_build_and_staleness(repo):
    project, main, feature = repo
    assert service.working_copy(project)['graph'] == 'none'
    # the conf points at the main checkout: another folder of the repo is built from its own files
    args = service.build_args(project, str(feature))
    assert args['paths'] == [os.path.realpath(feature)] and args['core'] == [os.path.realpath(CORE)]
    service.build_worktree(str(main), log=lambda *a: None)
    assert service.working_copy(project)['graph'] == 'up to date'
    py = main / 'shop_discount' / 'models' / 'shop_order.py'
    py.write_text(py.read_text().replace("'discount': 10", "'discount': 20"))
    assert service.working_copy(project)['graph'] == 'outdated'  # uncommitted edit


def test_http_guards_and_pages(repo):
    project, main, feature = repo
    from http.server import ThreadingHTTPServer
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), service.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{httpd.server_address[1]}'
    try:
        assert 'Odoo Impact' in urllib.request.urlopen(base + '/').read().decode()
        data = json.loads(urllib.request.urlopen(base + '/api/projects').read())
        assert data['projects'][0]['working_copy']['branch'] == 'main'
        body = json.dumps({'project': project['id'], 'ref': 'main'}).encode()
        with pytest.raises(urllib.error.HTTPError) as err:  # no custom header = cross-site attempt
            urllib.request.urlopen(urllib.request.Request(base + '/api/build', body, {'Content-Type': 'application/json'}))
        assert err.value.code == 403
        with pytest.raises(urllib.error.HTTPError) as err:  # DNS rebinding: foreign Host header
            urllib.request.urlopen(urllib.request.Request(base + '/api/projects', headers={'Host': 'evil.example'}))
        assert err.value.code == 403
        with pytest.raises(urllib.error.HTTPError) as err:  # no path tricks in diagram names
            urllib.request.urlopen(base + f"/api/diagrams/{project['id']}/..%2F..%2Fprojects")
        assert err.value.code == 404
    finally:
        httpd.shutdown()


def test_mcp_auto_builds_the_current_worktree(repo):
    project, main, feature = repo
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
