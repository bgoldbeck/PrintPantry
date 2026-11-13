"""Login hardening for exposure to the internet through the HTTPS proxy."""

from fastapi.testclient import TestClient

from app.auth import LoginLimiter, client_ip, is_https
from app.config import Settings
from app.main import create_app


def make_client(tmp_path, client=("44.1.2.9", 5000)):
    lib = tmp_path / "lib"
    lib.mkdir()
    settings = Settings(library=lib, data=tmp_path / "d", password="correct horse", scan_interval=0)
    return TestClient(create_app(settings, background_scan=False), client=client)


def test_every_route_needs_login_but_the_open_few(tmp_path):
    with make_client(tmp_path) as c:
        for path in ["/", "/api/library", "/api/printers", "/docs", "/openapi.json",
                     "/loginx", "/login/../api/library", "/healthzz", "/static/app.js"]:
            r = c.get(path, follow_redirects=False)
            assert r.status_code in (303, 401, 404), path
            assert r.status_code != 200, path
        assert c.get("/login").status_code == 200
        assert c.get("/healthz").status_code == 200


def test_lockout_after_wrong_passwords(tmp_path):
    with make_client(tmp_path) as c:
        for _ in range(LoginLimiter.PER_IP):
            r = c.post("/login", data={"password": "nope"}, follow_redirects=False)
            assert r.headers["location"] == "/login?error=1"
        # even the right password is refused while locked out
        r = c.post("/login", data={"password": "correct horse"}, follow_redirects=False)
        assert r.headers["location"] == "/login?error=wait"
        assert int(r.headers["retry-after"]) > 0
        assert "pp_session" not in r.cookies


def test_lockout_is_per_address(tmp_path):
    with make_client(tmp_path) as c:
        for _ in range(LoginLimiter.PER_IP):
            c.post("/login", data={"password": "nope"}, follow_redirects=False)
    with TestClient(c.app, client=("45.3.4.7", 5000)) as other:
        r = other.post("/login", data={"password": "correct horse"}, follow_redirects=False)
        assert r.headers["location"] == "/"


def test_global_limit_spares_home_network():
    lim = LoginLimiter()
    for i in range(LoginLimiter.GLOBAL):
        lim.failed(f"44.1.2.{i % 250}", now=1000.0)
    assert lim.retry_after("45.3.4.1", now=1001.0) > 0
    assert lim.retry_after("192.168.1.20", now=1001.0) == 0
    assert lim.retry_after("45.3.4.1", now=1000.0 + LoginLimiter.WINDOW + 1) == 0


def test_forwarded_headers_only_trusted_from_proxy():
    h = {"x-real-ip": "8.8.8.8", "x-forwarded-proto": "https"}
    assert client_ip("172.18.0.1", h) == "8.8.8.8"
    assert client_ip("192.168.1.20", h) == "192.168.1.20"  # LAN can't spoof
    assert is_https("http", "172.18.0.1", h)
    assert not is_https("http", "192.168.1.20", h)


def test_cookie_is_secure_behind_https_proxy(tmp_path):
    with make_client(tmp_path, client=("172.18.0.1", 5000)) as c:
        r = c.post("/login", data={"password": "correct horse"}, follow_redirects=False,
                   headers={"x-forwarded-proto": "https", "x-real-ip": "8.8.8.8"})
        sc = r.headers["set-cookie"].lower()
        assert "secure" in sc and "httponly" in sc and "samesite=lax" in sc


def test_cross_site_post_refused(tmp_path):
    with make_client(tmp_path) as c:
        c.post("/login", data={"password": "correct horse"}, follow_redirects=False)
        r = c.post("/api/rescan", headers={"origin": "https://evil.example"})
        assert r.status_code == 403
        r = c.post("/api/rescan", headers={"origin": "http://testserver"})
        assert r.status_code != 403
