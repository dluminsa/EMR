"""Run batch.py with Playwright network discovery enabled.

Launch with:

    streamlit run batchcopy.py

The normal batch workflow is preserved. Relevant OpenMRS document, XHR,
fetch, and POST traffic is printed to the console and appended to
``batch_network_log.jsonl``. Login secrets and sensitive headers are redacted.
The captured calls will be used to build the requests-only ``batch2.py``.
"""

from __future__ import annotations

import json
import runpy
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

import streamlit as st
from playwright.sync_api import Browser


BASE_DIR = Path(__file__).resolve().parent
BATCH_SCRIPT = BASE_DIR / "batch.py"
NETWORK_LOG_FILE = BASE_DIR / "batch_network_log.jsonl"

LOGGED_RESOURCE_TYPES = {"document", "xhr", "fetch"}
SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "set-cookie",
    "x-csrf-token",
    "x-xsrf-token",
}
SENSITIVE_BODY_KEYS = {
    "access_token",
    "accesstoken",
    "authorization",
    "cookie",
    "csrf_token",
    "password",
    "passwd",
    "pwd",
    "refresh_token",
    "refreshtoken",
    "token",
}

_log_lock = threading.Lock()
_seen_endpoints: set[tuple[str, str]] = set()


def timestamp() -> str:
    """Return an ISO timestamp for a captured network event."""
    return datetime.now(timezone.utc).isoformat()


def should_log_request(request) -> bool:
    """Keep traffic useful for reproducing the workflow with requests."""
    parsed_url = urlsplit(request.url)
    if "/openmrs/" not in parsed_url.path.lower():
        return False

    return (
        request.resource_type in LOGGED_RESOURCE_TYPES
        or request.method.upper() != "GET"
    )


def cleaned_headers(headers: dict[str, str]) -> dict[str, str]:
    """Redact authentication material while retaining useful headers."""
    return {
        key: "[REDACTED]" if key.lower() in SENSITIVE_HEADERS else value
        for key, value in headers.items()
    }


def redact_nested(value):
    """Recursively redact sensitive keys in decoded JSON values."""
    if isinstance(value, dict):
        return {
            key: (
                "[REDACTED]"
                if str(key).lower() in SENSITIVE_BODY_KEYS
                else redact_nested(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_nested(item) for item in value]
    return value


def cleaned_post_data(url: str, post_data: str | None):
    """Retain request parameters needed for batch2 while hiding secrets."""
    if not post_data:
        return None

    if "login" in url.lower():
        return "[REDACTED LOGIN FORM]"

    try:
        decoded_json = json.loads(post_data)
    except (json.JSONDecodeError, TypeError):
        decoded_json = None

    if decoded_json is not None:
        return json.dumps(redact_nested(decoded_json), ensure_ascii=True)

    try:
        form_items = parse_qsl(post_data, keep_blank_values=True)
    except ValueError:
        return post_data

    if not form_items:
        return post_data

    cleaned_items = [
        (
            key,
            "[REDACTED]"
            if key.lower() in SENSITIVE_BODY_KEYS
            else value,
        )
        for key, value in form_items
    ]
    return urlencode(cleaned_items, doseq=True)


def append_network_log(event: dict) -> None:
    """Append one event as JSON without interleaving callback writes."""
    with _log_lock:
        with NETWORK_LOG_FILE.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(event, ensure_ascii=True) + "\n")


def print_endpoint_once(method: str, url: str) -> None:
    """Highlight each unique method/endpoint pair once per process."""
    endpoint = (method, url)
    if endpoint in _seen_endpoints:
        return
    _seen_endpoints.add(endpoint)
    print("\n[DISCOVERED OPENMRS ENDPOINT]", flush=True)
    print(f"{method} {url}", flush=True)


def install_network_logger(page) -> None:
    """Attach redacted OpenMRS request and response console logging."""
    if getattr(page, "_batch_network_logger_installed", False):
        return
    setattr(page, "_batch_network_logger_installed", True)

    def log_request(request) -> None:
        if not should_log_request(request):
            return

        method = request.method.upper()
        post_data = cleaned_post_data(request.url, request.post_data)
        event = {
            "timestamp": timestamp(),
            "event": "request",
            "method": method,
            "url": request.url,
            "host": urlsplit(request.url).netloc,
            "resource_type": request.resource_type,
            "headers": cleaned_headers(request.headers),
            "post_data": post_data,
        }
        print_endpoint_once(method, request.url)
        print("\n[NETWORK REQUEST]", flush=True)
        print(f"{method} {request.url}", flush=True)
        print(f"resource_type={request.resource_type}", flush=True)
        if post_data:
            print(f"post_data={post_data}", flush=True)
        append_network_log(event)

    def log_response(response) -> None:
        request = response.request
        if not should_log_request(request):
            return

        event = {
            "timestamp": timestamp(),
            "event": "response",
            "method": request.method.upper(),
            "status": response.status,
            "url": response.url,
            "host": urlsplit(response.url).netloc,
            "resource_type": request.resource_type,
            "headers": cleaned_headers(response.headers),
            "content_type": response.headers.get("content-type"),
        }
        print("\n[NETWORK RESPONSE]", flush=True)
        print(
            f"{response.status} {request.method.upper()} {response.url}",
            flush=True,
        )
        print(
            f"content_type={response.headers.get('content-type')}",
            flush=True,
        )
        append_network_log(event)

    def log_request_failed(request) -> None:
        if not should_log_request(request):
            return
        event = {
            "timestamp": timestamp(),
            "event": "request_failed",
            "method": request.method.upper(),
            "url": request.url,
            "resource_type": request.resource_type,
            "failure": request.failure,
        }
        print("\n[NETWORK REQUEST FAILED]", flush=True)
        print(
            f"{request.method.upper()} {request.url}: {request.failure}",
            flush=True,
        )
        append_network_log(event)

    page.on("request", log_request)
    page.on("response", log_response)
    page.on("requestfailed", log_request_failed)


def install_page_factory_patch() -> None:
    """Instrument every page created by batch.py's Browser.new_page call."""
    if getattr(Browser, "_batch_network_logger_patched", False):
        return

    original_new_page = Browser.new_page

    def instrumented_new_page(browser, *args, **kwargs):
        page = original_new_page(browser, *args, **kwargs)
        install_network_logger(page)
        print("\n[BATCH NETWORK LOGGER ATTACHED]", flush=True)
        return page

    Browser.new_page = instrumented_new_page
    Browser._batch_network_logger_patched = True


def initialize_log_for_session() -> None:
    """Start one fresh log per Streamlit session, not per widget rerun."""
    state_key = "_batch_network_log_initialized"
    if st.session_state.get(state_key):
        return

    try:
        NETWORK_LOG_FILE.unlink(missing_ok=True)
    except OSError as exc:
        print(f"Could not reset network log: {exc}", flush=True)
    st.session_state[state_key] = True


def main() -> None:
    if not BATCH_SCRIPT.is_file():
        raise FileNotFoundError(f"Missing batch workflow: {BATCH_SCRIPT}")

    initialize_log_for_session()
    install_page_factory_patch()
    print("\n========================================", flush=True)
    print("BATCH EMR - NETWORK DISCOVERY MODE", flush=True)
    print(f"Structured log: {NETWORK_LOG_FILE}", flush=True)
    print("========================================\n", flush=True)
    runpy.run_path(str(BATCH_SCRIPT), run_name="__main__")


if __name__ == "__main__":
    main()
