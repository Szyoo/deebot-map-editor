import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from deebot_map_editor.server import App, make_handler


@pytest.fixture
def server(tmp_path):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(App(tmp_path, demo=True)))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def request(port, method, path, host=None, origin=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Host": host or f"127.0.0.1:{port}"}
    if origin:
        headers["Origin"] = origin
    data = json.dumps(body).encode() if body is not None else None
    if data:
        headers["Content-Type"] = "application/json"
    c.request(method, path, body=data, headers=headers)
    r = c.getresponse()
    r.read()
    c.close()
    return r.status


def test_local_hosts_allowed(server):
    assert request(server, "GET", "/api/status") == 200
    assert request(server, "GET", "/api/status", host=f"localhost:{server}") == 200
    assert request(server, "POST", "/api/stop", origin=f"http://127.0.0.1:{server}", body={}) == 200


def test_dns_rebinding_rejected(server):
    # 恶意域名解析到 127.0.0.1：Origin 和 Host 一致，但不是本机地址
    evil = f"evil.example.com:{server}"
    assert request(server, "GET", "/", host=evil) == 403
    assert request(server, "GET", "/api/status", host=evil) == 403
    assert request(server, "POST", "/api/stop", host=evil, origin=f"http://{evil}", body={}) == 403


def test_cross_site_post_rejected(server):
    assert request(server, "POST", "/api/stop", origin="http://evil.example.com", body={}) == 403
