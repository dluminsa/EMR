"""Upload a compact EMR extract and batch-update UgandaEMR with requests."""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import pandas as pd
import requests

from hmis_submission import check_submission, refill_controls, submission_url

try:
    import streamlit as st
except ModuleNotFoundError:  # Flet builds use this module only as an EMR backend.
    st = None


CREDENTIALS_FILE = Path("CREDENTIALS.csv")
REFERENCE_DIR = Path("BATCH_REFERENCE")
LOCATION_ID = "5"
FORM_UUID = "12de5bc5-352e-4faf-9961-a2125085a75c"
REQUEST_TIMEOUT = 45
LAST_SUBMISSION_ERROR_FILE = Path("batch4_last_submission_error.html")
REQUIRED_UPLOAD_COLUMNS = {"ART", "LD", "ARVD"}
REQUIRED_REFERENCE_COLUMNS = {"Art", "ARVS"}
VISIT_DATE_CONFLICT_MESSAGE = (
    "The date you selected is conflicting with other visit(s). "
    "Click to navigate to a visit:"
)


class EmrError(RuntimeError):
    pass


class LoginUrlError(EmrError):
    pass


class InvalidCredentialsError(EmrError):
    pass


class FacilityAccessError(EmrError):
    pass


class ArtNumberNotFoundInEmrError(EmrError):
    pass


class VisitDateConflictError(EmrError):
    pass


def safe_endpoint(url):
    """Return a diagnostic URL without query parameters or fragments."""
    try:
        parts = urlsplit(str(url))
    except (TypeError, ValueError):
        return "the configured EMR address"
    if not parts.scheme or not parts.netloc:
        return parts.path or "the configured EMR address"
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def response_context(response):
    """Summarize an HTTP response without exposing submitted form values."""
    status = getattr(response, "status_code", "unknown")
    reason = str(getattr(response, "reason", "") or "").strip()
    status_text = f"HTTP {status}" + (f" {reason}" if reason else "")
    final_url = safe_endpoint(getattr(response, "url", ""))
    history = list(getattr(response, "history", []) or [])
    if history:
        redirects = " -> ".join(
            str(getattr(item, "status_code", "?")) for item in history
        )
        return f"{status_text}; final URL {final_url}; redirects {redirects}"
    return f"{status_text}; final URL {final_url}; no redirects"


def request_failure_message(exc, operation, url):
    """Convert Requests exceptions into actionable, safe diagnostics."""
    endpoint = safe_endpoint(url)
    detail = re.sub(r"\s+", " ", str(exc)).strip()
    if len(detail) > 220:
        detail = detail[:217] + "..."

    if isinstance(exc, requests.exceptions.ConnectTimeout):
        code = "NETWORK_CONNECT_TIMEOUT"
        reason = "The server did not accept a connection before the timeout."
    elif isinstance(exc, requests.exceptions.ReadTimeout):
        code = "NETWORK_READ_TIMEOUT"
        reason = "The server accepted the connection but did not respond in time."
    elif isinstance(exc, requests.exceptions.SSLError):
        code = "NETWORK_TLS_ERROR"
        reason = "The HTTPS/TLS handshake or certificate validation failed."
    elif isinstance(exc, requests.exceptions.TooManyRedirects):
        code = "NETWORK_REDIRECT_LOOP"
        reason = "The server redirected the request too many times."
    elif isinstance(exc, requests.exceptions.ConnectionError):
        lowered = detail.lower()
        if any(value in lowered for value in ("name resolution", "getaddrinfo", "nodename nor servname")):
            code = "NETWORK_DNS_ERROR"
            reason = "The device could not resolve the EMR server name."
        elif any(value in lowered for value in ("connection refused", "actively refused")):
            code = "NETWORK_CONNECTION_REFUSED"
            reason = "The device reached the host, but nothing accepted the EMR port."
        elif any(value in lowered for value in ("no route to host", "network is unreachable", "host is unreachable")):
            code = "NETWORK_NO_ROUTE"
            reason = "The device has no working network route to the EMR host."
        else:
            code = "NETWORK_CONNECTION_ERROR"
            reason = "The device could not establish or keep the EMR connection."
    elif isinstance(exc, requests.exceptions.Timeout):
        code = "NETWORK_TIMEOUT"
        reason = "The EMR request timed out."
    else:
        code = "NETWORK_REQUEST_ERROR"
        reason = "The EMR request failed before a usable HTTP response arrived."

    message = f"[{code}] {reason} Operation: {operation}; endpoint: {endpoint}."
    if detail:
        message += f" Technical detail: {detail}"
    return message


def extract_login_error(page_text):
    """Extract only known, safe authentication messages from a login page."""
    text = str(page_text or "")
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    text = unescape(re.sub(r"(?s)<[^>]+>", " ", text))
    text = re.sub(r"\s+", " ", text).strip()
    known_messages = (
        (r"invalid username\s*(?:/|or)\s*password", "Invalid username/password"),
        (r"unable to log in at (?:the|this) location", "Unable to log in at this location"),
        (r"not (?:allowed|authorized) to log in at (?:the|this) location", "Account is not allowed to log in at this location"),
        (r"please (?:choose|select) (?:a|your) (?:session )?location", "A session location must be selected"),
        (r"(?:session )?location (?:is )?(?:invalid|required)", "The session location is invalid or required"),
        (r"account (?:is |has been )?(?:locked|disabled)", "The account is locked or disabled"),
        (r"too many (?:failed )?login attempts", "Too many failed login attempts"),
    )
    for pattern, message in known_messages:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return message
    return None


def is_login_page(response):
    text = str(getattr(response, "text", "") or "").lower()
    path = urlsplit(str(getattr(response, "url", "") or "")).path.lower()
    markers = (
        "loginbutton",
        'name="username"',
        "name='username'",
        'name="sessionlocation"',
        "name='sessionlocation'",
    )
    return path.endswith("/login.htm") or any(marker in text for marker in markers)


class FormParser(HTMLParser):
    """Collect successful controls from the HMIS HTML form."""

    SEMANTIC_CONTROL_IDS = {
        "art-regimen",
        "no-of-art-pills",
        "no-of-art-pills-days",
    }
    BROWSER_MANAGED_CONTROL_MARKER = "encounterdiagnos"

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_target_form = False
        self.depth = 0
        self.action = ""
        self.controls: list[tuple[str, str]] = []
        self.select_options: dict[str, list[dict[str, object]]] = {}
        self.semantic_controls: dict[str, str] = {}
        self.pending_semantic_control = None
        self.pending_browser_managed_control = False
        self.browser_managed_controls: set[str] = set()
        self.select = None
        self.option = None
        self.textarea = None

    @staticmethod
    def clean_attribute(value):
        if value is None:
            return None
        cleaned = str(value).strip()
        return cleaned.lstrip("\\\"'").rstrip("\\\"'/").strip()

    def remember_semantic_control(self, name):
        if self.pending_semantic_control:
            self.semantic_controls.setdefault(self.pending_semantic_control, name)
            self.pending_semantic_control = None

    def finish_option(self):
        if self.option is None or self.select is None:
            return
        if self.option["value"] is None:
            self.option["value"] = self.option["text"].strip()
        self.select["options"].append(self.option)
        self.option = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            action = self.clean_attribute(attrs.get("action", "")) or ""
            if self.in_target_form:
                self.depth += 1
            elif "enterHtmlForm/submit.action" in action:
                self.in_target_form = True
                self.depth = 1
                self.action = action
            return
        if not self.in_target_form:
            return

        element_id = self.clean_attribute(attrs.get("id"))
        if element_id in self.SEMANTIC_CONTROL_IDS:
            self.pending_semantic_control = element_id
        if (
            tag.lower() == "encounterdiagnosis"
            or self.BROWSER_MANAGED_CONTROL_MARKER in str(element_id or "").lower()
        ):
            self.pending_browser_managed_control = True

        if tag == "input":
            name = self.clean_attribute(attrs.get("name"))
            input_type = (
                self.clean_attribute(attrs.get("type", "text")) or "text"
            ).lower()
            if not name or "disabled" in attrs:
                return
            if input_type in {"button", "submit", "reset", "file", "image"}:
                return
            if input_type in {"checkbox", "radio"} and "checked" not in attrs:
                return
            value = self.clean_attribute(attrs.get("value", "")) or ""
            self.remember_semantic_control(name)
            if self.pending_browser_managed_control:
                self.browser_managed_controls.add(name)
                self.pending_browser_managed_control = False
            self.controls.append((name, value))
        elif tag == "select" and attrs.get("name"):
            name = self.clean_attribute(attrs["name"])
            self.remember_semantic_control(name)
            if self.pending_browser_managed_control:
                self.browser_managed_controls.add(name)
                self.pending_browser_managed_control = False
            self.select = {
                "name": name,
                "disabled": "disabled" in attrs,
                "options": [],
            }
        elif tag == "option" and self.select is not None:
            self.finish_option()
            self.option = {
                "value": self.clean_attribute(attrs.get("value")),
                "selected": "selected" in attrs,
                "text": "",
            }
        elif tag == "textarea" and attrs.get("name"):
            self.textarea = {
                "name": self.clean_attribute(attrs["name"]),
                "disabled": "disabled" in attrs,
                "text": "",
            }

    def handle_data(self, data):
        if self.option is not None:
            self.option["text"] += data
        if self.textarea is not None:
            self.textarea["text"] += data

    def handle_endtag(self, tag):
        if not self.in_target_form:
            return
        if tag == "option" and self.option is not None and self.select is not None:
            self.finish_option()
        elif tag == "select" and self.select is not None:
            self.finish_option()
            self.select_options[self.select["name"]] = list(self.select["options"])
            if not self.select["disabled"] and self.select["options"]:
                selected = next(
                    (item for item in self.select["options"] if item["selected"]),
                    self.select["options"][0],
                )
                self.controls.append((self.select["name"], selected["value"]))
            self.select = None
        elif tag == "textarea" and self.textarea is not None:
            if not self.textarea["disabled"]:
                self.controls.append(
                    (self.textarea["name"], self.textarea["text"])
                )
            self.textarea = None
        elif tag == "form":
            self.depth -= 1
            if self.depth <= 0:
                self.in_target_form = False


def request(
    session,
    method,
    url,
    *,
    connection_error=EmrError,
    operation="contacting the EMR",
    **kwargs,
):
    """Send an EMR request with a consistent timeout and safe diagnostics."""
    print(f"[REQUEST] {method.upper()} {url}", flush=True)
    try:
        response = session.request(
            method, url, timeout=REQUEST_TIMEOUT, **kwargs
        )
    except requests.RequestException as exc:
        raise connection_error(
            request_failure_message(exc, operation, url)
        ) from exc
    print(f"[RESPONSE] {response.status_code} {response.url}", flush=True)
    return response


def login(base_url, username, password):
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 Batch4 UgandaEMR Updater",
            "Accept-Language": "en-GB,en;q=0.9",
        }
    )
    login_url = f"{base_url}/login.htm"
    page = request(
        session,
        "GET",
        login_url,
        connection_error=LoginUrlError,
        operation="opening the EMR login page",
    )
    if page.status_code >= 400:
        raise LoginUrlError(
            "[LOGIN_PAGE_HTTP_ERROR] The device connected to the server, but "
            f"the login page failed: {response_context(page)}."
        )

    response = request(
        session,
        "POST",
        login_url,
        data={
            "username": username,
            "password": password,
            "sessionLocation": LOCATION_ID,
            "redirectUrl": "",
        },
        allow_redirects=True,
        connection_error=LoginUrlError,
        operation="submitting the EMR login form",
    )
    login_error = extract_login_error(response.text)
    if login_error:
        raise InvalidCredentialsError(
            "[LOGIN_REJECTED_BY_EMR] The device connected to UgandaEMR, but "
            f'the login page explicitly reported: "{login_error}". '
            f"Session location sent: {LOCATION_ID}; {response_context(response)}."
        )
    if response.status_code in {401, 403}:
        raise InvalidCredentialsError(
            "[LOGIN_HTTP_REJECTED] The device connected to UgandaEMR, but the "
            f"server rejected authentication. Session location sent: {LOCATION_ID}; "
            f"{response_context(response)}."
        )
    if response.status_code >= 400:
        raise LoginUrlError(
            "[LOGIN_POST_HTTP_ERROR] The device connected to UgandaEMR, but "
            f"the login submission failed: {response_context(response)}."
        )
    if is_login_page(response):
        raise InvalidCredentialsError(
            "[LOGIN_PAGE_RETURNED] The device connected to UgandaEMR and submitted "
            "the login form, but the server returned the login page without a "
            "recognized explanation. This can be caused by an account restriction, "
            "an incorrect session location, an expired/blocked session, or credentials "
            f"being rejected. Session location sent: {LOCATION_ID}; "
            f"{response_context(response)}."
        )

    home = response
    if "ugandaemr-findPatientLink-ugandaemr-findPatientLink-extension" not in home.text:
        home = request(
            session,
            "GET",
            f"{base_url}/referenceapplication/home.page",
            operation="opening the UgandaEMR home page after login",
        )
    if "ugandaemr-findPatientLink-ugandaemr-findPatientLink-extension" not in home.text:
        if is_login_page(home):
            raise InvalidCredentialsError(
                "[LOGIN_SESSION_NOT_ESTABLISHED] The login submission did not create "
                "a usable authenticated session; UgandaEMR sent the app back to its "
                f"login page. Session location sent: {LOCATION_ID}; "
                f"{response_context(home)}."
            )
        if home.status_code >= 400:
            raise FacilityAccessError(
                "[HOME_PAGE_HTTP_ERROR] Authentication was submitted, but the "
                f"UgandaEMR home page failed: {response_context(home)}."
            )
        raise FacilityAccessError(
            "[HOME_PAGE_NOT_RECOGNIZED] Authentication was submitted, but the "
            "returned page did not contain the expected UgandaEMR home-page marker. "
            f"The server may use a different UgandaEMR version or the account may lack "
            f"the required application privilege; {response_context(home)}."
        )
    return session


def find_exact_patient(session, base_url, art_number):
    response = request(
        session,
        "GET",
        f"{base_url}/ws/rest/v1/patient",
        params={
            "identifier": art_number,
            "v": "custom:(patientId,uuid,patientIdentifier:(uuid,identifier))",
        },
        headers={"Accept": "application/json"},
    )
    if response.status_code != 200:
        raise EmrError("The patient search request failed.")
    try:
        results = response.json().get("results", [])
    except ValueError as exc:
        raise EmrError("The patient search did not return JSON.") from exc
    if not results:
        raise ArtNumberNotFoundInEmrError(
            f"ART number {art_number} was not found in EMR."
        )

    patient = results[0]
    if not patient.get("uuid") or patient.get("patientId") is None:
        raise EmrError("The selected patient result did not contain its IDs.")
    return str(patient["uuid"]), str(patient["patientId"])


def create_visit(session, base_url, patient_uuid, patient_id, visit_date):
    existing_visits = request(
        session,
        "GET",
        f"{base_url}/ws/rest/v1/visit",
        params={
            "patient": patient_uuid,
            "v": "full",
            "limit": 100,
            "includeInactive": "true",
        },
        headers={"Accept": "application/json"},
    )
    if existing_visits.status_code == 200:
        try:
            existing_results = existing_visits.json().get("results", [])
        except ValueError:
            existing_results = []

        def is_same_visit_date(item):
            if item.get("voided", False):
                return False
            raw_date = item.get("startDatetime") or item.get("startDate")
            if isinstance(raw_date, (int, float)) or (
                isinstance(raw_date, str) and raw_date.strip().isdigit()
            ):
                numeric_date = int(raw_date)
                unit = "ms" if numeric_date > 10_000_000_000 else "s"
                parsed_date = pd.to_datetime(
                    numeric_date, unit=unit, errors="coerce", utc=True
                )
            else:
                parsed_date = pd.to_datetime(raw_date, errors="coerce", utc=True)
            if pd.isna(parsed_date):
                return False
            return (
                parsed_date.tz_convert("Africa/Kampala").date().isoformat()
                == visit_date
            )

        same_date_visits = [
            item for item in existing_results if is_same_visit_date(item)
        ]
        if same_date_visits:
            reusable = [
                item for item in same_date_visits if not (item.get("encounters") or [])
            ]
            if len(same_date_visits) != 1 or len(reusable) != 1:
                raise VisitDateConflictError(
                    "The last encounter date conflicts with an existing visit."
                )
            visit_uuid = reusable[0].get("uuid")
            if not visit_uuid:
                raise EmrError("The existing empty visit has no UUID.")
            visit_id = reusable[0].get("visitId") or reusable[0].get("id")
            return str(visit_uuid), str(visit_id or "")

    response = request(
        session,
        "GET",
        f"{base_url}/coreapps/visit/retrospectiveVisit/create.action",
        params={
            "patientId": patient_id,
            "locationId": LOCATION_ID,
            "startDate": visit_date,
            "stopDate": visit_date,
        },
        headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
    )
    if (
        VISIT_DATE_CONFLICT_MESSAGE in response.text
        or "conflicting with other visit" in response.text
    ):
        raise VisitDateConflictError(
            "The last encounter date conflicts with an existing visit."
        )
    if response.status_code >= 400:
        raise EmrError("OpenMRS could not create the retrospective visit.")

    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    visit_uuid = payload.get("uuid") or payload.get("visitUuid")
    visit_id = payload.get("visitId") or payload.get("id")

    visits = request(
        session,
        "GET",
        f"{base_url}/ws/rest/v1/visit",
        params={
            "patient": patient_uuid,
            "v": "full",
            "limit": 100,
            "includeInactive": "true",
        },
        headers={"Accept": "application/json"},
    )
    if visits.status_code == 200:
        try:
            candidates = visits.json().get("results", [])
        except ValueError:
            candidates = []
        dated = [
            item
            for item in candidates
            if str(item.get("startDatetime", ""))[:10] == visit_date
        ]
        if dated:
            chosen = dated[-1]
            visit_uuid = visit_uuid or chosen.get("uuid")
            visit_id = visit_id or chosen.get("visitId") or chosen.get("id")

    if not visit_uuid:
        raise EmrError("The new visit UUID could not be determined.")
    dashboard = request(
        session,
        "GET",
        f"{base_url}/coreapps/patientdashboard/patientDashboard.page",
        params={"patientId": patient_id, "visitId": visit_id or visit_uuid},
    )
    numeric_match = re.search(r"[?&]visitId=(\d+)", dashboard.text)
    if numeric_match:
        visit_id = numeric_match.group(1)
    if not visit_id:
        raise EmrError("The new visit numeric ID could not be determined.")
    return str(visit_uuid), str(visit_id)


def set_control(controls, name, value):
    controls[:] = [(key, old) for key, old in controls if key != name]
    controls.append((name, str(value)))


def first_provider(parser):
    for option in parser.select_options.get("w9", []):
        value = FormParser.clean_attribute(option.get("value")) or ""
        label = str(option.get("text") or "").strip()
        if value and label:
            return value
    raise EmrError("The HMIS form has no available provider name.")


def select_value_for_label(parser, control_name, selected_label):
    for option in parser.select_options.get(control_name, []):
        value = FormParser.clean_attribute(option.get("value")) or ""
        label = str(option.get("text") or "").strip()
        if value and label == selected_label:
            return value
    raise EmrError(f"The HMIS form does not contain ART regimen {selected_label}.")


def art_medication_controls(parser, selected_label):
    regimen_name = parser.semantic_controls.get("art-regimen")
    if not regimen_name:
        matching_selects = []
        for name, options in parser.select_options.items():
            if any(
                (FormParser.clean_attribute(option.get("value")) or "")
                and str(option.get("text") or "").strip() == selected_label
                for option in options
            ):
                matching_selects.append(name)
        if len(matching_selects) != 1:
            raise EmrError(
                f"The HMIS form does not contain ART regimen {selected_label}."
            )
        regimen_name = matching_selects[0]

    regimen_value = select_value_for_label(parser, regimen_name, selected_label)
    pills_name = parser.semantic_controls.get("no-of-art-pills")
    days_name = parser.semantic_controls.get("no-of-art-pills-days")
    generated_match = re.fullmatch(r"w(\d+)", regimen_name)
    if generated_match:
        generated_number = int(generated_match.group(1))
        pills_name = pills_name or f"w{generated_number + 4}"
        days_name = days_name or f"w{generated_number + 6}"

    available_controls = {name for name, _ in parser.controls}
    if (
        not pills_name
        or not days_name
        or pills_name not in available_controls
        or days_name not in available_controls
    ):
        raise EmrError("The HMIS ART pill and day fields could not be identified.")
    return regimen_name, regimen_value, pills_name, days_name


def control_value(controls, name):
    return next((value for key, value in reversed(controls) if key == name), "")


def submit_hmis_form(
    session,
    base_url,
    patient_uuid,
    patient_id,
    visit_uuid,
    visit_id,
    visit_date,
    return_date,
    quantity,
    regimen,
):
    visit_reference = visit_id or visit_uuid
    return_url = (
        "/openmrs/coreapps/patientdashboard/patientDashboard.page?"
        f"patientId={patient_id}&visitId={visit_reference}"
    )
    form_url = f"{base_url}/htmlformentryui/htmlform/enterHtmlFormWithStandardUi.page"
    response = request(
        session,
        "GET",
        form_url,
        params={
            "patientId": patient_uuid,
            "visitId": visit_uuid,
            "formUuid": FORM_UUID,
            "returnUrl": return_url,
        },
    )
    if response.status_code != 200:
        raise EmrError("The HMIS clinical assessment form could not be opened.")

    parser = FormParser()
    parser.feed(response.text)
    if not parser.action:
        raise EmrError("The HMIS submission form was not found in the page.")
    provider_value = first_provider(parser)
    regimen_name, regimen_value, pills_name, days_name = art_medication_controls(
        parser, regimen
    )

    controls = [
        (name, value)
        for name, value in refill_controls(response.text, parser.controls)
        if name not in parser.browser_managed_controls
    ]
    form_visit_id = control_value(controls, "visitId") or visit_id
    if not form_visit_id:
        raise EmrError("The HMIS form did not contain its numeric visit ID.")
    return_url = (
        "/openmrs/coreapps/patientdashboard/patientDashboard.page?"
        f"patientId={patient_id}&visitId={form_visit_id}"
    )
    updates = {
        "personId": patient_id,
        "createVisit": "false",
        "visitId": form_visit_id,
        "returnUrl": return_url,
        "w1": LOCATION_ID,
        "w3": visit_date,
        "w6": return_date,
        "w9": provider_value,
        "w16": "164972",
        # The manual browser submit replaces the generated diagnosis w-field
        # with this JSON list.  Sending the generated field produces OpenMRS'
        # "invalid json list submitted" error instead.
        "encounterDiagnoses": "[]",
        regimen_name: regimen_value,
        pills_name: quantity,
        days_name: quantity,
    }
    for name, value in updates.items():
        set_control(controls, name, value)

    submitted = request(
        session,
        "POST",
        submission_url(base_url, parser.action),
        files=[(name, (None, value)) for name, value in controls],
        headers={
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": response.url,
        },
        allow_redirects=True,
    )
    check_submission(submitted, LAST_SUBMISSION_ERROR_FILE, EmrError)


def update_client(
    base_url,
    username,
    password,
    art_number,
    visit_date,
    return_date,
    quantity,
    regimen,
):
    """Update one client using the requests flow used by batch3.py."""
    session = login(base_url, username, password)
    patient_uuid, patient_id = find_exact_patient(session, base_url, art_number)
    visit_uuid, visit_id = create_visit(
        session, base_url, patient_uuid, patient_id, visit_date
    )
    submit_hmis_form(
        session,
        base_url,
        patient_uuid,
        patient_id,
        visit_uuid,
        visit_id,
        visit_date,
        return_date,
        quantity,
        regimen,
    )


def normalize_art_numbers(values):
    """Return ART numbers as nullable integers after removing non-digits."""
    digits = values.astype("string").str.replace(r"[^0-9]", "", regex=True)
    digits = digits.mask(digits.eq(""))
    numbers = pd.to_numeric(digits, errors="coerce")
    numbers = numbers.where(numbers > 0)
    return numbers.astype("Int64")


def parse_dates(values):
    """Parse common EMR extract dates, preferring day-first CSV values."""
    try:
        return pd.to_datetime(values, format="mixed", dayfirst=True, errors="coerce")
    except ValueError:
        return pd.to_datetime(values, dayfirst=True, errors="coerce")


def rejected_copy(frame, reason):
    rejected = frame.copy()
    rejected["REASON_REJECTED"] = reason
    return rejected


def prepare_batch(uploaded, reference):
    """Normalize, match, and divide uploaded rows into ready and rejected data."""
    missing_upload = REQUIRED_UPLOAD_COLUMNS - set(uploaded.columns)
    if missing_upload:
        raise ValueError(
            "Uploaded CSV is missing required columns: "
            + ", ".join(sorted(missing_upload))
        )
    missing_reference = REQUIRED_REFERENCE_COLUMNS - set(reference.columns)
    if missing_reference:
        raise ValueError(
            "Facility reference CSV is missing required columns: "
            + ", ".join(sorted(missing_reference))
        )

    data = uploaded.copy()
    data.insert(0, "SOURCE_ROW", range(2, len(data) + 2))
    data["SOURCE_ART"] = data["ART"]
    data["ARTX"] = normalize_art_numbers(data["ART"])
    # ART is deliberately digits-only in every generated CSV.
    data["ART"] = data["ARTX"].astype("string")

    ref = reference.copy()
    ref["ARTX"] = normalize_art_numbers(ref["Art"])
    ref["ARVS"] = (
        ref["ARVS"].astype("string").str.strip().str.replace("/", "-", regex=False)
    )

    rejected_frames = []
    invalid_art = data["ARTX"].isna()
    if invalid_art.any():
        rejected_frames.append(
            rejected_copy(data.loc[invalid_art], "BLANK OR INVALID ART NUMBER")
        )
    candidates = data.loc[~invalid_art].copy()

    duplicate_upload = candidates["ARTX"].duplicated(keep=False)
    if duplicate_upload.any():
        rejected_frames.append(
            rejected_copy(
                candidates.loc[duplicate_upload],
                "DUPLICATED ART NUMBER IN UPLOADED CSV",
            )
        )
    candidates = candidates.loc[~duplicate_upload].copy()

    valid_ref = ref.loc[ref["ARTX"].notna()].copy()
    duplicate_reference_keys = set(
        valid_ref.loc[valid_ref["ARTX"].duplicated(keep=False), "ARTX"].tolist()
    )
    reference_duplicate = candidates["ARTX"].isin(duplicate_reference_keys)
    if reference_duplicate.any():
        rejected_frames.append(
            rejected_copy(
                candidates.loc[reference_duplicate],
                "DUPLICATED ART NUMBER IN BATCH_REFERENCE",
            )
        )
    candidates = candidates.loc[~reference_duplicate].copy()
    valid_ref = valid_ref.loc[
        ~valid_ref["ARTX"].isin(duplicate_reference_keys), ["ARTX", "Art", "ARVS"]
    ]

    merged = candidates.merge(
        valid_ref,
        on="ARTX",
        how="left",
        validate="one_to_one",
        indicator=True,
    )
    not_found = merged["_merge"].eq("left_only")
    if not_found.any():
        rejected_frames.append(
            rejected_copy(
                merged.loc[not_found].drop(columns="_merge"),
                "ART NUMBER NOT FOUND IN BATCH_REFERENCE",
            )
        )
    ready = merged.loc[~not_found].drop(columns="_merge").copy()

    ready["LD_PARSED"] = parse_dates(ready["LD"])
    ready["ARVD_PARSED"] = pd.to_numeric(ready["ARVD"], errors="coerce")

    validation_rules = (
        (ready["LD_PARSED"].isna(), "BLANK OR INVALID LD"),
        (
            ready["ARVD_PARSED"].isna()
            | (ready["ARVD_PARSED"] <= 0)
            | (ready["ARVD_PARSED"] % 1 != 0),
            "BLANK OR INVALID ARVD",
        ),
        (
            ready["ARVS"].isna()
            | ready["ARVS"].astype("string").str.strip().isin(["", "nan", "<NA>"]),
            "BLANK ARVS IN BATCH_REFERENCE",
        ),
    )
    for invalid, reason in validation_rules:
        if invalid.any():
            rejected_frames.append(rejected_copy(ready.loc[invalid], reason))
            ready = ready.loc[~invalid].copy()

    ready["ARVD_PARSED"] = ready["ARVD_PARSED"].astype(int)
    ready["RD_PARSED"] = ready["LD_PARSED"] + pd.to_timedelta(
        ready["ARVD_PARSED"], unit="D"
    )
    ready["RD"] = ready["RD_PARSED"].dt.strftime("%d/%m/%Y")
    ready = ready.sort_values("SOURCE_ROW").reset_index(drop=True)
    if rejected_frames:
        rejected = pd.concat(rejected_frames, ignore_index=True, sort=False)
        rejected = rejected.sort_values("SOURCE_ROW", na_position="last").reset_index(
            drop=True
        )
    else:
        rejected = pd.DataFrame(columns=[*data.columns, "REASON_REJECTED"])
    return ready, rejected


def csv_bytes(frame):
    return frame.to_csv(index=False).encode("utf-8")


def show_rejected_download(rejected, *, key):
    if rejected.empty:
        return
    st.download_button(
        "DOWNLOAD REJECTED",
        data=csv_bytes(rejected),
        file_name="rejected_data.csv",
        mime="text/csv",
        key=key,
    )


def load_credentials():
    if not CREDENTIALS_FILE.is_file():
        raise FileNotFoundError(f"Missing credentials file: {CREDENTIALS_FILE}")
    credentials = pd.read_csv(CREDENTIALS_FILE)
    missing = {"DISTRICT", "FACILITY", "ip", "user", "password"} - set(
        credentials.columns
    )
    if missing:
        raise ValueError(
            "Credentials CSV is missing required columns: "
            + ", ".join(sorted(missing))
        )
    credentials = credentials.loc[credentials["user"].notna()].copy()
    for column in ("DISTRICT", "FACILITY"):
        credentials[column] = credentials[column].astype("string").str.strip()
    return credentials


def run_updates(ready, existing_rejected, base_url, username, password):
    failed_rows = []
    successful_updates = 0
    total_updates = len(ready)
    progress = st.progress(0.0)
    status = st.empty()

    for position, (_, client) in enumerate(ready.iterrows(), start=1):
        art_number = str(client["Art"]).strip()
        status.write(f"Updating {position} of {total_updates}: {art_number}")
        try:
            update_client(
                base_url,
                username,
                password,
                art_number,
                client["LD_PARSED"].date().isoformat(),
                client["RD_PARSED"].date().isoformat(),
                int(client["ARVD_PARSED"]),
                str(client["ARVS"]).strip(),
            )
            successful_updates += 1
            print(f"[BATCH UPDATE SUCCESS] ART={art_number}", flush=True)
        except Exception as exc:
            print(f"[BATCH UPDATE FAILED] ART={art_number}: {exc}", flush=True)
            failed = client.to_dict()
            failed["REASON_REJECTED"] = f"FAILED TO UPDATE: {exc}"
            failed_rows.append(failed)
        progress.progress(position / total_updates)

    status.empty()
    frames = [existing_rejected]
    if failed_rows:
        frames.append(pd.DataFrame(failed_rows))
    rejected = pd.concat(frames, ignore_index=True, sort=False)
    return successful_updates, rejected


def main():
    st.set_page_config(page_title="EMR Extract Batch Update", layout="wide")
    st.markdown(
        """
        <style>
        .stApp, .stApp * { font-weight: 700 !important; }
        div.stButton > button {
            width: 100%; min-height: 3rem; border-radius: 8px;
            border: 1px solid #0d47a1; background: #1565c0;
            color: white !important; font-weight: 800 !important;
        }
        div.stButton > button:hover {
            background: #0d47a1; color: white !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.title("EMR EXTRACT BATCH UPDATE TOOL")
    st.caption("Required upload columns: ART, LD, ARVD. RD is calculated as LD + ARVD.")

    try:
        credentials = load_credentials()
    except (OSError, ValueError, pd.errors.ParserError) as exc:
        st.error(str(exc))
        st.stop()

    district = st.radio(
        "DISTRICT",
        credentials["DISTRICT"].dropna().unique(),
        index=None,
        horizontal=True,
    )
    if district is None:
        st.stop()
    district_credentials = credentials.loc[
        credentials["DISTRICT"].eq(district)
    ].copy()

    facility = st.radio(
        "FACILITY",
        district_credentials["FACILITY"].dropna().unique(),
        index=None,
        horizontal=True,
        key=f"facility_{district}",
    )
    if facility is None:
        st.stop()

    facility_credentials = district_credentials.loc[
        district_credentials["FACILITY"].eq(facility)
    ]
    if facility_credentials.empty:
        st.error("No credentials matched the selected facility.")
        st.stop()
    credential = facility_credentials.iloc[0]
    if any(
        pd.isna(credential.get(name)) or not str(credential.get(name)).strip()
        for name in ("ip", "user", "password")
    ):
        st.error("The selected facility has incomplete server credentials.")
        st.stop()

    reference_file = REFERENCE_DIR / f"{facility}.csv"
    if not reference_file.is_file():
        st.warning(
            f"{facility} was rejected: no reference CSV was found at "
            f"{reference_file}."
        )
        st.stop()
    try:
        reference = pd.read_csv(reference_file, dtype={"Art": "string"})
    except (OSError, UnicodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as exc:
        st.warning(
            f"{facility} was rejected: its reference CSV could not be read. {exc}"
        )
        st.stop()
    missing_reference = REQUIRED_REFERENCE_COLUMNS - set(reference.columns)
    if missing_reference:
        st.warning(
            f"{facility} was rejected: {reference_file} must contain both exact "
            f"column names Art and ARVS. Missing: "
            + ", ".join(sorted(missing_reference))
            + "."
        )
        st.stop()

    uploaded_file = st.file_uploader("Upload EMR extract CSV", type=["csv"])
    if uploaded_file is None:
        st.stop()
    try:
        uploaded = pd.read_csv(uploaded_file, dtype={"ART": "string"})
    except (UnicodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as exc:
        st.error(f"The uploaded file is not a readable CSV: {exc}")
        st.stop()

    missing_upload = REQUIRED_UPLOAD_COLUMNS - set(uploaded.columns)
    if missing_upload:
        st.error(
            "Upload rejected. Missing required columns: "
            + ", ".join(sorted(missing_upload))
        )
        st.stop()
    try:
        ready, rejected = prepare_batch(uploaded, reference)
    except (TypeError, ValueError) as exc:
        st.error(f"Upload rejected: {exc}")
        st.stop()

    st.write(f"READY TO UPDATE: {len(ready)}")
    st.write(f"REJECTED DURING VALIDATION: {len(rejected)}")
    show_rejected_download(rejected, key="validation_rejected")

    if ready.empty:
        st.warning("There are no valid rows to update.")
        st.stop()

    base_url = f"http://{str(credential['ip']).strip()}:8081/openmrs"
    username = str(credential["user"]).strip()
    password = str(credential["password"]).strip()
    if st.button("BATCH UPLOAD", type="primary"):
        successful, final_rejected = run_updates(
            ready, rejected, base_url, username, password
        )
        st.success(f"UPDATED: {successful}; REJECTED: {len(final_rejected)}")
        show_rejected_download(final_rejected, key="final_rejected")


if __name__ == "__main__":
    main()
