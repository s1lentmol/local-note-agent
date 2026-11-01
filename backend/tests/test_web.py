import socket
from urllib.parse import urlsplit

import pytest

from app.config import Settings
from app.errors import ProcessingError
from app.extractors.web import download_html, extract_html, normalize_url, public_target


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/",
        "http://user:pass@example.com/",
        "http://example.com:8080/",
    ],
)
def test_invalid_urls(url):
    with pytest.raises(ProcessingError, match="HTTP"):
        normalize_url(url)


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1", "::ffff:127.0.0.1"]
)
def test_private_addresses_are_denied(monkeypatch, address):
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **k: [(None, None, None, None, (address, 80))]
    )
    with pytest.raises(ProcessingError) as error:
        public_target("http://example.com/")
    assert error.value.code == "url_not_allowed"


class Response:
    def __init__(self, status=200, headers=None, body=b"<html>hello</html>"):
        self.status = status
        self.headers = headers or {"Content-Type": "text/html"}
        self.body = body

    def read(self, *args, **kwargs):
        result, self.body = self.body, b""
        return result

    def close(self):
        pass


class Pool:
    def __init__(self, response):
        self.response = response
        self.options = None

    def urlopen(self, method, target, **options):
        self.options = options
        return self.response

    def close(self):
        pass


def test_validated_ip_is_used_with_original_tls_name(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **k: [(None, None, None, None, ("93.184.216.34", 443))]
    )
    pool = Pool(Response())
    connections = []

    def connect(ip, port, **kwargs):
        connections.append((ip, port, kwargs))
        return pool

    monkeypatch.setattr("app.extractors.web.urllib3.HTTPSConnectionPool", connect)
    body, final_url = download_html("https://example.com/path", Settings())
    assert body == b"<html>hello</html>"
    assert final_url == "https://example.com/path"
    assert connections[0][0] == "93.184.216.34"
    assert connections[0][2]["server_hostname"] == "example.com"
    assert connections[0][2]["assert_hostname"] == "example.com"
    assert pool.options["headers"]["Host"] == "example.com"
    assert pool.options["redirect"] is False
    assert pool.options["retries"] is False


def test_redirect_to_private_network_is_blocked(monkeypatch):
    def resolve(host, *args, **kwargs):
        return [
            (
                None,
                None,
                None,
                None,
                ("93.184.216.34" if host == "example.com" else "127.0.0.1", 80),
            )
        ]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    pool = Pool(Response(302, {"Location": "http://127.0.0.1/secret"}))
    monkeypatch.setattr("app.extractors.web.urllib3.HTTPConnectionPool", lambda *a, **k: pool)
    with pytest.raises(ProcessingError) as error:
        download_html("http://example.com/", Settings())
    assert error.value.code == "url_not_allowed"


@pytest.mark.parametrize(
    "response,code",
    [
        (Response(body=b"x" * 20), "page_too_large"),
        (Response(headers={"Content-Type": "application/pdf"}), "unsupported_content_type"),
        (
            Response(headers={"Content-Type": "text/html", "Content-Encoding": "gzip"}),
            "unsupported_encoding",
        ),
        (Response(status=403), "http_error"),
    ],
)
def test_web_limits(monkeypatch, response, code):
    monkeypatch.setattr(
        "app.extractors.web.public_target", lambda url: (urlsplit(url), "93.184.216.34", 80)
    )
    monkeypatch.setattr(
        "app.extractors.web.urllib3.HTTPConnectionPool", lambda *a, **k: Pool(response)
    )
    with pytest.raises(ProcessingError) as error:
        download_html("http://example.com/", Settings(html_max_bytes=10))
    assert error.value.code == code


def test_real_html_extraction():
    content = (
        "<html><body><article><h1>Local knowledge</h1><p>"
        + "Local notes retain original sources and help users find useful information. " * 15
        + "</p></article></body></html>"
    )
    result = extract_html(content.encode())
    assert "Local notes retain original sources" in result
    assert "<html>" not in result
