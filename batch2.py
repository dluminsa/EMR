"""Requests-only version of the one-client UgandaEMR batch updater."""

from __future__ import annotations

import re
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd
import requests
import streamlit as st


CREDENTIALS_FILE = Path("CREDENTIALS.csv")
REFERENCE_DIR = Path("BATCH_REFERENCE")
LOCATION_ID = "5"
FORM_UUID = "12de5bc5-352e-4faf-9961-a2125085a75c"
REQUEST_TIMEOUT = 45
LAST_SUBMISSION_ERROR_FILE = Path("batch2_last_submission_error.html")
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


class ArtNumberMismatchError(EmrError):
    pass


class VisitDateConflictError(EmrError):
    pass


class FormParser(HTMLParser):
    """Collect successful controls from the HMIS HTML form."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_target_form = False
        self.depth = 0
        self.action = ""
        self.controls: list[tuple[str, str]] = []
        self.select_options: dict[str, list[dict[str, object]]] = {}
        self.select = None
        self.option = None
        self.textarea = None

    def finish_option(self):
        """Save an option even when its optional closing tag is omitted."""
        if self.option is None or self.select is None:
            return
        if self.option["value"] is None:
            self.option["value"] = self.option["text"].strip()
        self.select["options"].append(self.option)
        self.option = None

    @staticmethod
    def clean_attribute(value):
        """Remove quote characters emitted inside generated HTML attributes."""
        if value is None:
            return None
        # Generated UgandaEMR markup contains sequences such as value=\"16\"/.
        # HTMLParser exposes the trailing quote/backslash/slash as part of the
        # value, so remove all of those wrapper characters at both ends.
        cleaned = str(value).strip()
        # A leading slash may be a legitimate absolute application path.
        # Remove only quote/backslash wrappers on the left, while the right
        # side may also contain the stray self-closing-tag slash.
        return cleaned.lstrip("\\\"'").rstrip("\\\"'/").strip()

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

        if tag == "input":
            name = self.clean_attribute(attrs.get("name"))
            input_type = (
                self.clean_attribute(attrs.get("type", "text")) or "text"
            ).lower()
            if not name or attrs.get("disabled") is not None:
                return
            if input_type in {"button", "submit", "reset", "file", "image"}:
                return
            if input_type in {"checkbox", "radio"} and "checked" not in attrs:
                return
            value = self.clean_attribute(attrs.get("value", "")) or ""
            self.controls.append((name, value))
        elif tag == "select" and attrs.get("name"):
            self.select = {
                "name": self.clean_attribute(attrs["name"]),
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
            self.select_options[self.select["name"]] = list(
                self.select["options"]
            )
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


def request(session, method, url, *, connection_error=EmrError, **kwargs):
    """Send an EMR request with consistent timeout and console diagnostics."""
    print(f"[REQUEST] {method.upper()} {url}", flush=True)
    try:
        response = session.request(
            method, url, timeout=REQUEST_TIMEOUT, **kwargs
        )
    except requests.RequestException as exc:
        raise connection_error("The facility EMR request failed.") from exc
    print(
        f"[RESPONSE] {response.status_code} {response.url}", flush=True
    )
    return response


def login(base_url, username, password):
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 Batch2 UgandaEMR Updater",
            "Accept-Language": "en-GB,en;q=0.9",
        }
    )
    login_url = f"{base_url}/login.htm"
    page = request(
        session, "GET", login_url, connection_error=LoginUrlError
    )
    # Some UgandaEMR builds render the login controls through fragments or use
    # unquoted attributes. A successful response from the configured
    # /openmrs/login.htm endpoint is enough to proceed with the known login
    # fields; credential and access checks below validate the resulting page.
    if page.status_code >= 400:
        raise LoginUrlError("The configured login page is invalid.")

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
    )
    if "Invalid username/password" in response.text:
        raise InvalidCredentialsError
    if response.status_code >= 400 or "loginButton" in response.text:
        raise InvalidCredentialsError

    home = response
    if "ugandaemr-findPatientLink-ugandaemr-findPatientLink-extension" not in home.text:
        home = request(
            session, "GET", f"{base_url}/referenceapplication/home.page"
        )
    if "ugandaemr-findPatientLink-ugandaemr-findPatientLink-extension" not in home.text:
        raise FacilityAccessError
    return session


def normalize_art_identifier(value):
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


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

    wanted = normalize_art_identifier(art_number)
    exact = []
    returned_identifiers = []
    for patient in results:
        identifiers = patient.get("patientIdentifier") or []
        if isinstance(identifiers, dict):
            identifiers = [identifiers]
        values = [
            str(item.get("identifier", "")).strip()
            for item in identifiers
        ]
        returned_identifiers.extend(value for value in values if value)
        if wanted in {
            normalize_art_identifier(value) for value in values
        }:
            exact.append(patient)
    if not exact:
        print(
            f"[PATIENT MATCH FAILED] requested={art_number}; "
            f"returned={returned_identifiers}",
            flush=True,
        )
        raise ArtNumberMismatchError(
            f"Expected exact ART number {art_number}, but no exact result "
            "was found. No patient was updated."
        )
    if len(exact) > 1:
        raise ArtNumberMismatchError(
            f"More than one patient matched ART number {art_number}. "
            "No patient was updated."
        )
    patient = exact[0]
    if not patient.get("uuid") or patient.get("patientId") is None:
        raise EmrError("The exact patient result did not contain its IDs.")
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
            kampala_date = parsed_date.tz_convert("Africa/Kampala").date()
            return kampala_date.isoformat() == visit_date

        visit_dates = [
            item.get("startDatetime") or item.get("startDate")
            for item in existing_results
            if not item.get("voided", False)
        ]
        print(
            f"[VISIT CHECK] target={visit_date}, returned={len(existing_results)}, "
            f"dates={visit_dates[:10]}",
            flush=True,
        )
        if any(is_same_visit_date(item) for item in existing_results):
            raise VisitDateConflictError

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
    if VISIT_DATE_CONFLICT_MESSAGE in response.text or "conflicting with other visit" in response.text:
        raise VisitDateConflictError
    if response.status_code >= 400:
        raise EmrError("OpenMRS could not create the retrospective visit.")

    visit_uuid = None
    visit_id = None
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if isinstance(payload, dict):
        visit_uuid = payload.get("uuid") or payload.get("visitUuid")
        visit_id = payload.get("visitId") or payload.get("id")

    # Resolve any ID omitted by the legacy create.action response.
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
            item for item in candidates
            if str(item.get("startDatetime", ""))[:10] == visit_date
        ]
        if dated:
            chosen = dated[-1]
            visit_uuid = visit_uuid or chosen.get("uuid")
            visit_id = visit_id or chosen.get("visitId") or chosen.get("id")

    if not visit_uuid:
        raise EmrError("The new visit UUID could not be determined.")

    # The dashboard HTML exposes the numeric visit ID required by the form.
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
    """Return the first non-placeholder option from the parsed w9 select."""
    for option in parser.select_options.get("w9", []):
        value = FormParser.clean_attribute(option.get("value")) or ""
        label = str(option.get("text") or "").strip()
        if value and label:
            return value, label

    available = ", ".join(sorted(parser.select_options)) or "none"
    raise EmrError(
        "The HMIS form has no provider names in w9 "
        f"(available selects: {available})."
    )


def select_value_for_label(parser, control_name, selected_label):
    """Return the form option value matching a displayed select label."""
    for option in parser.select_options.get(control_name, []):
        value = FormParser.clean_attribute(option.get("value")) or ""
        label = str(option.get("text") or "").strip()
        if value and label == selected_label:
            return value

    raise EmrError(
        f"The HMIS form does not contain the ART regimen {selected_label}."
    )


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
    return_url = (
        f"/openmrs/coreapps/patientdashboard/patientDashboard.page?"
        f"patientId={patient_id}&visitId={visit_id}"
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
    provider_value, provider_name = first_provider(parser)
    regimen_value = select_value_for_label(parser, "w589", regimen)

    controls = parser.controls
    updates = {
        "personId": patient_id,
        "createVisit": "false",
        "visitId": visit_id,
        "returnUrl": return_url,
        "w1": LOCATION_ID,
        "w3": visit_date,
        "w6": return_date,
        "w9": provider_value,
        "w16": "164972",
        "w589": regimen_value,
        "w593": quantity,
        "w595": quantity,
    }
    for name, value in updates.items():
        set_control(controls, name, value)

    # Passing (None, value) makes requests reproduce the captured multipart form.
    multipart = [(name, (None, value)) for name, value in controls]
    populated = [(name, value) for name, value in controls if str(value).strip()]
    print(
        f"[HMIS PAYLOAD] fields={len(controls)}, populated={populated}",
        flush=True,
    )
    submit_url = urljoin(form_url, parser.action)
    submitted = request(
        session,
        "POST",
        submit_url,
        files=multipart,
        headers={
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": response.url,
        },
        allow_redirects=True,
    )
    body = submitted.text.lower()
    if submitted.status_code >= 400 or any(
        marker in body
        for marker in ("error submitting", "validation error", "has errors")
    ):
        try:
            LAST_SUBMISSION_ERROR_FILE.write_text(
                submitted.text, encoding="utf-8", errors="replace"
            )
        except OSError:
            pass
        error_text = re.sub(r"\s+", " ", submitted.text).strip()
        print(
            f"[HMIS SERVER ERROR] {error_text[:4000]}",
            flush=True,
        )
        raise EmrError(
            "OpenMRS rejected the HMIS form submission. Server response was "
            "saved to batch2_last_submission_error.html."
        )
    print(
        f"[HMIS] Updated patient={patient_uuid}, provider={provider_name}",
        flush=True,
    )


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


def refresh_client_inputs(success_message=None):
    version = st.session_state.get("client_form_version", 0)
    st.session_state["client_form_version"] = version + 1
    if success_message:
        st.session_state["client_update_success"] = success_message
    st.rerun()


def log_update_failure(art_number, error):
    print(f"[EMR UPDATE FAILED] ART={art_number}: {error}", flush=True)


st.markdown(
    """
    <style>
    .stApp,
    [data-testid="stAppViewContainer"] {
        background-color: #f7fafc;
    }
    [data-testid="stHeader"] {
        background-color: transparent;
    }
    .block-container {
        max-width: 1180px;
        padding-top: 1.25rem;
        padding-bottom: 2rem;
    }
    h1 {
        color: #17324d;
        font-size: 2rem !important;
        letter-spacing: 0;
        margin-bottom: 1.25rem;
    }
    .stApp, .stApp * {
        font-weight: 700 !important;
    }
    div[data-testid="stNumberInput"]:has(
        input[aria-label="ART number"]
    ) {
        max-width: 24rem;
    }
    div[data-testid="stNumberInput"] button {display: none !important;}
    div[data-baseweb="input"] {border-radius: 8px !important;}
    div[data-testid="stRadio"] {padding-bottom: 0.35rem;}
    div[data-testid="stRadio"] div[role="radiogroup"] {
        column-gap: 1.15rem;
        row-gap: 0.35rem;
    }
    div[data-testid="stAlert"] {border-radius: 8px;}
    div.stButton > button {
        width: 100%; min-height: 3rem; border-radius: 8px;
        border: 1px solid #0d47a1; background: #1565c0;
        color: white !important; font-weight: 800 !important;
    }
    div.stButton > button:hover {background: #0d47a1; color: white !important;}
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("EMR ONE CLIENT UPDATE TOOL")

if not CREDENTIALS_FILE.is_file():
    st.error(f"Missing credentials file: {CREDENTIALS_FILE}")
    st.stop()
dfcred = pd.read_csv(CREDENTIALS_FILE)
dfcred = dfcred[dfcred["user"].notna()].copy()
for column in ("DISTRICT", "FACILITY"):
    dfcred[column] = dfcred[column].astype(str).str.strip()

district = st.radio(
    "DISTRICT", dfcred["DISTRICT"].unique(), index=None, horizontal=True
)
if district is None:
    st.stop()
district_credentials = dfcred[dfcred["DISTRICT"] == district].copy()

facility = st.radio(
    "FACILITY",
    district_credentials["FACILITY"].dropna().unique(),
    index=None,
    horizontal=True,
    key=f"facility_{district}",
)
if facility is None:
    st.stop()
facility_credentials = district_credentials[
    district_credentials["FACILITY"] == facility
].copy()
if facility_credentials.empty:
    st.error("No credentials matched the selected Facility.")
    st.stop()

reference_file = REFERENCE_DIR / f"{facility}.csv"
if not reference_file.is_file():
    st.error(f"No reference dataset was found for {facility}: {reference_file}")
    st.stop()
dfref = pd.read_csv(reference_file)
missing = {"Art", "ARVS"} - set(dfref.columns)
if missing:
    st.error(f"{reference_file} is missing: {', '.join(sorted(missing))}.")
    st.stop()
dfref["ART"] = (
    dfref["Art"].astype(str).str.replace("[^0-9]", "", regex=True)
)

version = st.session_state.get("client_form_version", 0)
prefix = f"{district}_{facility}_{version}"
success = st.session_state.pop("client_update_success", None)
if success:
    st.success(success)

art_columns = st.columns([1, 2], gap="medium")
with art_columns[0]:
    entered_art = st.number_input(
        "ART number", min_value=1, value=None, step=1, key=f"art_{prefix}"
    )
if entered_art is None:
    st.stop()

matches = dfref[dfref["ART"] == str(int(entered_art))].copy()
if matches.empty:
    st.info(f"ART NUMBER {entered_art} NOT FOUND for {facility}.")
    st.stop()
art_numbers = matches["Art"].dropna().astype(str).str.strip()
art_numbers = art_numbers[art_numbers != ""].unique().tolist()
if not art_numbers:
    st.error(f"ART number {entered_art} has no full Art value in {reference_file}.")
    st.stop()
if len(art_numbers) > 1:
    with art_columns[1]:
        art_number = st.radio(
            "Which one?",
            art_numbers,
            index=None,
            horizontal=True,
            key=f"matching_{prefix}_{entered_art}",
        )
    if art_number is None:
        st.stop()
else:
    art_number = art_numbers[0]

selected_rows = matches[
    matches["Art"].astype(str).str.strip() == art_number
]
regimens = selected_rows["ARVS"].dropna().astype(str).str.strip()
regimens = regimens[regimens != ""]
regimen = (
    regimens.iloc[0].replace("/", "-")
    if not regimens.empty
    else "TDF-3TC-DTG"
)

row = facility_credentials.iloc[0]
if any(pd.isna(row.get(name)) or not str(row.get(name)).strip() for name in ("ip", "user", "password")):
    st.error("The selected facility has incomplete server credentials.")
    st.stop()
base_url = f"http://{str(row['ip']).strip()}:8081/openmrs"
username = str(row["user"]).strip()
password = str(row["password"]).strip()

visit_columns = st.columns([1, 1.45, 1], gap="medium")
with visit_columns[0]:
    last_encounter = st.date_input(
        "Last Encounter Date",
        value=None,
        max_value=date.today(),
        format="DD/MM/YYYY",
        key=f"last_{prefix}",
    )
if last_encounter is None:
    st.stop()
if last_encounter > date.today():
    st.warning("Last Encounter Date cannot be in the future.")
    st.stop()

with visit_columns[1]:
    refill_days_choice = st.radio(
        "Days Dispensed",
        ["30", "90", "180", "Other"],
        index=None,
        horizontal=True,
        key=f"days_choice_{prefix}",
    )
    if refill_days_choice == "Other":
        refill_days = st.number_input(
            "Other number of days",
            min_value=1,
            value=None,
            step=1,
            key=f"other_days_{prefix}",
        )
    elif refill_days_choice is not None:
        refill_days = int(refill_days_choice)
    else:
        refill_days = None
if refill_days is None:
    st.stop()

with visit_columns[2]:
    return_date = st.date_input(
        "Return Visit Date",
        value=None,
        format="DD/MM/YYYY",
        key=f"return_{prefix}",
    )
if return_date is None:
    st.stop()

if return_date <= last_encounter:
    st.warning("Return Visit Date must be after Last Encounter Date.")
    st.stop()

expected = last_encounter + timedelta(days=int(refill_days))
difference = (return_date - expected).days
if abs(difference) >= 10:
    direction = "less" if difference < 0 else "more"
    st.warning(
        f"This client was given {refill_days} pills. Return Date entered is "
        f"{direction} than expected by {abs(difference)} days."
    )
    proceed = st.radio(
        "Do you still want to update this client?",
        ["Yes", "No"],
        index=None,
        horizontal=True,
        key=f"override_{prefix}",
    )
    if proceed is None:
        st.stop()
    if proceed == "No":
        refresh_client_inputs()

if st.button("Update Emr", key=f"update_{prefix}"):
    try:
        update_client(
            base_url,
            username,
            password,
            art_number,
            last_encounter.isoformat(),
            return_date.isoformat(),
            int(refill_days),
            regimen,
        )
        print(f"[EMR UPDATE SUCCESS] ART={art_number}", flush=True)
        refresh_client_inputs(
            f"SUCCESS: {art_number} was updated successfully. Update another client."
        )
    except LoginUrlError as exc:
        log_update_failure(art_number, exc)
        st.info("The facility EMR login address is incorrect or unreachable.")
    except InvalidCredentialsError as exc:
        log_update_failure(art_number, exc)
        st.info("Invalid username/password. Please try again.")
    except FacilityAccessError as exc:
        log_update_failure(art_number, exc)
        st.info("Credentials provided do not have access to update facility EMR.")
    except VisitDateConflictError as exc:
        log_update_failure(art_number, exc)
        st.info(f"This date was already updated for client {art_number}.")
    except (ArtNumberNotFoundInEmrError, ArtNumberMismatchError) as exc:
        log_update_failure(art_number, exc)
        st.info(str(exc))
    except EmrError as exc:
        log_update_failure(art_number, exc)
        st.error(str(exc))
    except Exception as exc:
        log_update_failure(art_number, exc)
        st.error("An unexpected error occurred while updating the client.")
