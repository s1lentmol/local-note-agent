import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import certifi
import trafilatura
import urllib3
from pydantic import AnyHttpUrl, TypeAdapter, ValidationError

from app.errors import ProcessingError


def normalize_url(url: str) -> str:
    try:
        normalized = str(TypeAdapter(AnyHttpUrl).validate_python(url))
        parts = urlsplit(normalized)
        if (
            parts.username is not None
            or parts.password is not None
            or parts.port not in (None, 80, 443)
        ):
            raise ValueError("Credentials or port")
        if len(normalized) > 2048:
            raise ValueError("URL too long")
        return normalized
    except (ValueError, ValidationError) as exc:
        raise ProcessingError(
            "invalid_url", "Нужна HTTP(S)-ссылка без логина и пароля, с портом 80 или 443."
        ) from exc


def public_target(url: str):
    url = normalize_url(url)
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        addresses = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ProcessingError("url_unreachable", "Не удалось определить адрес сайта.") from exc
    ips = list(dict.fromkeys(item[4][0] for item in addresses))
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise ProcessingError(
            "url_not_allowed", "Загрузка локальных и служебных сетевых адресов запрещена."
        )
    return parts, ips[0], port


def download_html(url: str, settings) -> tuple[bytes, str]:
    for _ in range(6):
        parts, ip, port = public_target(url)
        options = {
            "timeout": urllib3.Timeout(
                connect=settings.web_timeout_seconds, read=settings.web_timeout_seconds
            )
        }
        if parts.scheme == "https":
            pool = urllib3.HTTPSConnectionPool(
                ip,
                port,
                server_hostname=parts.hostname,
                assert_hostname=parts.hostname,
                cert_reqs="CERT_REQUIRED",
                ca_certs=certifi.where(),
                **options,
            )
        else:
            pool = urllib3.HTTPConnectionPool(ip, port, **options)
        response = None
        try:
            # Connect to the validated IP, retaining the original TLS name and Host header.
            target = parts.path or "/"
            if parts.query:
                target += "?" + parts.query
            response = pool.urlopen(
                "GET",
                target,
                headers={
                    "Host": parts.netloc,
                    "User-Agent": "LocalNoteAgent/0.2",
                    "Accept": "text/html, application/xhtml+xml",
                    "Accept-Encoding": "identity",
                },
                redirect=False,
                retries=False,
                preload_content=False,
                assert_same_host=False,
            )
            if response.status in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not location:
                    raise ProcessingError(
                        "invalid_redirect", "Сайт вернул перенаправление без адреса."
                    )
                url = normalize_url(urljoin(url, location))
                continue
            if response.status != 200:
                raise ProcessingError("http_error", f"Сайт вернул HTTP {response.status}.")
            mime = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if mime not in ("text/html", "application/xhtml+xml"):
                raise ProcessingError(
                    "unsupported_content_type", "Ссылка должна вести на HTML-страницу."
                )
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise ProcessingError(
                    "unsupported_encoding", "Сайт не предоставил несжатую HTML-страницу."
                )
            output = bytearray()
            while chunk := response.read(64 * 1024, decode_content=False):
                output.extend(chunk)
                if len(output) > settings.html_max_bytes:
                    raise ProcessingError(
                        "page_too_large", "HTML-страница превышает допустимый размер."
                    )
            return bytes(output), url
        except urllib3.exceptions.HTTPError as exc:
            raise ProcessingError(
                "url_unreachable", "Не удалось загрузить страницу за отведённое время."
            ) from exc
        finally:
            if response:
                response.close()
            pool.close()
    raise ProcessingError("too_many_redirects", "Слишком много перенаправлений.")


def extract_html(html: bytes) -> str:
    return trafilatura.extract(html, include_comments=False, include_tables=True) or ""
