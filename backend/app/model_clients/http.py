"""Small injectable synchronous transport; no retries, redirects or payload logging."""

import http.client
import json
import socket
import urllib.error
import urllib.request

from .contracts import ModelClientError

# The native T07 endpoint groups its domain failures under HTTP 422.
ASR_ERROR_CODES = {
    "invalid_audio_format",
    "invalid_audio_data",
    "invalid_audio_duration",
    "empty_asr_result",
    "asr_runtime_timeout",
    "asr_runtime_unavailable",
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HTTPTransport:
    def __init__(self):
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, settings, path, payload=None, *, audio=None):
        headers = {
            "Content-Type": "application/octet-stream" if audio is not None else "application/json"
        }
        if settings.api_key.get_secret_value():
            headers["Authorization"] = "Bearer " + settings.api_key.get_secret_value()
        data = (
            audio
            if audio is not None
            else (json.dumps(payload, allow_nan=False).encode() if payload is not None else None)
        )
        request = urllib.request.Request(settings.base_url + path, data=data, headers=headers)
        try:
            with self.opener.open(request, timeout=settings.timeout) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
                if len(raw) > 16 * 1024 * 1024:
                    raise ModelClientError("response_too_large")
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            with exc:
                if audio is not None and exc.code == 422:
                    try:
                        code = json.loads(exc.read(1024)).get("error")
                    except (AttributeError, ValueError, OSError, http.client.HTTPException):
                        code = None
                    if isinstance(code, str) and code in ASR_ERROR_CODES:
                        raise ModelClientError(code) from None
            raise ModelClientError(f"http_{exc.code}") from None
        except (TimeoutError, socket.timeout):
            raise ModelClientError("timeout") from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                code = "timeout"
            elif isinstance(exc.reason, http.client.HTTPException):
                code = "http_protocol_error"
            else:
                code = "connection_failed"
            raise ModelClientError(code) from None
        except http.client.HTTPException:
            raise ModelClientError("http_protocol_error") from None
        except OSError:
            raise ModelClientError("connection_failed") from None
        except (ValueError, UnicodeError):
            raise ModelClientError("invalid_json") from None
