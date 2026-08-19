import pandas as pd

import os

import numpy as np

import re

from datetime import date

from html.parser import HTMLParser

import streamlit as st

from pathlib import Path

from urllib.parse import urljoin

import requests

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
        self.controls = []
        self.select_options = {}
        self.select = None
        self.option = None
        self.textarea = None

    def finish_option(self):
        if self.option is None or self.select is None:
            return
        if self.option["value"] is None:
            self.option["value"] = self.option["text"].strip()
        self.select["options"].append(self.option)
        self.option = None

    @staticmethod
    def clean_attribute(value):
        if value is None:
            return None
        cleaned = str(value).strip()
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
    print(f"[REQUEST] {method.upper()} {url}", flush=True)
    try:
        response = session.request(
            method, url, timeout=REQUEST_TIMEOUT, **kwargs
        )
    except requests.RequestException as exc:
        raise connection_error("The facility EMR request failed.") from exc
    print(f"[RESPONSE] {response.status_code} {response.url}", flush=True)
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
        found_identifiers = list(dict.fromkeys(returned_identifiers))
        print(
            f"[PATIENT MATCH FAILED] requested={art_number}; "
            f"returned={found_identifiers}",
            flush=True,
        )
        if not found_identifiers:
            print(
                f"[PATIENT SEARCH RAW RESULTS] {results}",
                flush=True,
            )
        found_text = ", ".join(found_identifiers) or "no identifier value"
        raise ArtNumberMismatchError(
            f"Expected exact ART number {art_number}, but no exact result "
            f"was found. OpenMRS returned: {found_text}. "
            "No patient was updated."
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
            return (
                parsed_date.tz_convert("Africa/Kampala").date().isoformat()
                == visit_date
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
    if (
        VISIT_DATE_CONFLICT_MESSAGE in response.text
        or "conflicting with other visit" in response.text
    ):
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
            return value, label
    raise EmrError("The HMIS form has no provider names in w9.")


def select_value_for_label(parser, control_name, selected_label):
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
    provider_value, _ = first_provider(parser)
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

    multipart = [(name, (None, value)) for name, value in controls]
    submitted = request(
        session,
        "POST",
        urljoin(form_url, parser.action),
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
        raise EmrError(
            "OpenMRS rejected the HMIS form submission. Server response was "
            "saved to batch2_last_submission_error.html."
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
    patient_uuid, patient_id = find_exact_patient(
        session, base_url, art_number
    )
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

st.markdown(
    """
    <style>
    .stApp, .stApp * {
        font-weight: 700 !important;
    }
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

st.title("EMR BATCH UPDATE TOOL")

if not CREDENTIALS_FILE.is_file():
    st.error(f"Missing credentials file: {CREDENTIALS_FILE}")
    st.stop()

dfcred = pd.read_csv(CREDENTIALS_FILE)
dfcred = dfcred[dfcred["user"].notna()].copy()
for column in ("DISTRICT", "FACILITY"):
    dfcred[column] = dfcred[column].astype(str).str.strip()

district = st.radio(
    "DISTRICT",
    dfcred["DISTRICT"].unique(),
    index=None,
    horizontal=True,
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

row = facility_credentials.iloc[0]
credential_fields = ("ip", "user", "password")
if any(
    pd.isna(row.get(name)) or not str(row.get(name)).strip()
    for name in credential_fields
):
    st.error("The selected facility has incomplete server credentials.")
    st.stop()

base_url = f"http://{str(row['ip']).strip()}:8081/openmrs"
username = str(row["user"]).strip()
password = str(row["password"]).strip()

reference_file = REFERENCE_DIR / f"{facility}.csv"
if not reference_file.is_file():
    st.error(
        f"No reference dataset was found for {facility}: {reference_file}"
    )
    st.stop()

dfref = pd.read_csv(reference_file)
missing_reference_columns = {"Art", "ARVS"} - set(dfref.columns)
if missing_reference_columns:
    st.error(
        f"{reference_file} is missing: "
        f"{', '.join(sorted(missing_reference_columns))}."
    )
    st.stop()

uploaded_file = st.file_uploader("Upload enrollments CSV", type="csv")
if uploaded_file is None:
    st.stop()

df = pd.read_csv(uploaded_file)

df['Service Type'] = df['Service Type'].astype(str)

df = df[df['Service Type'].str.contains('ART')].copy()

a = df.shape[0]

df = df[['MR - First name', 'MR - Surname', 'MR - Sex' ,'HIV/ART-Next Appointment date', 'Last updated on','ART: Art Number','HIV-ART Regimen - No. of days dispensed','Service Type']]

df[['HIV/ART-Next Appointment date', 'Last updated on']] = (df[['HIV/ART-Next Appointment date', 'Last updated on']]
                                                            .apply(lambda col: pd.to_datetime(col,format='mixed',dayfirst=True).dt.date))

dfart = df[df['ART: Art Number'].isnull()].copy()

dfartn = df[df['ART: Art Number'].notnull()].copy()

dfartn['ART'] = dfartn['ART: Art Number'].astype(str).str.replace('[^0-9]', '', regex=True)

dfartn['ART'] = dfartn['ART'].fillna(0)

dfartn['ART'] = pd.to_numeric(dfartn['ART'], errors = 'coerce')

dfartna = dfartn[dfartn['ART']<1].copy()

if dfartna.shape[0]>0:
    dfart = pd.concat([dfartna, dfart])
    dfartn = dfartn[dfartn['ART']>0].copy()
    

dfartn['ART'] = dfartn['ART: Art Number'].astype(str).str.replace('[^0-9]', '', regex=True)

dfart['ART_STATUS'] = 'NO ART NUMBER'

no_art = dfart.shape[0]

dfdup = dfartn[dfartn['ART'].duplicated()].copy()

dfdup['DUP STATUS'] = 'DUPLICATED IN E-REGISTER'

dup_ereg = dfdup.shape[0]

dfnodup = dfartn[~dfartn['ART'].duplicated()].copy()

if dfdup.shape[0]>0:
    dfartn = pd.concat([dfdup, dfnodup])
else:
    dfartn = dfnodup

if dfart.shape[0]>0:
    df = pd.concat([dfart, dfartn])
else:
    df = dfartn

df['HIV-ART Regimen - No. of days dispensed'] = pd.to_numeric(df['HIV-ART Regimen - No. of days dispensed'], errors = 'coerce')

dfnopills = df[df['HIV-ART Regimen - No. of days dispensed'].isnull()].copy()#NO PILLS 

dfpills = df[df['HIV-ART Regimen - No. of days dispensed'].notnull()].copy() #HAS PILLS

dfnoday = dfnopills[((dfnopills['HIV-ART Regimen - No. of days dispensed'].isnull()) & (dfnopills['HIV/ART-Next Appointment date'].isnull()))].copy()

dfday = dfnopills[((dfnopills['HIV-ART Regimen - No. of days dispensed'].isnull()) & (dfnopills['HIV/ART-Next Appointment date'].notnull()))].copy()

dfday = dfday.drop(columns=['HIV-ART Regimen - No. of days dispensed'])

dfday[['HIV/ART-Next Appointment date', 'Last updated on']] = (dfday[['HIV/ART-Next Appointment date', 'Last updated on']]
                                                            .apply(lambda col: pd.to_datetime(col,format='mixed',dayfirst=True)))

dfday['HIV-ART Regimen - No. of days dispensed'] = (
    pd.to_datetime(
        dfday['HIV/ART-Next Appointment date'],
        errors='coerce',
    )
    - pd.to_datetime(
        dfday['Last updated on'],
        errors='coerce',
    )
) / pd.Timedelta(days=1)

dfnoday['DAYS_STATUS'] = 'MISSING DAYS DISPENSED'

dfnoday = dfnoday.drop(columns =['HIV/ART-Next Appointment date','HIV-ART Regimen - No. of days dispensed'])

#dfa = pd.concat([dfday, dfnoday])

dfs = [dfx for dfx in [dfday, dfnoday] if not df.empty]


dfa = pd.concat(dfs, ignore_index=True)

dfnodate = dfpills[dfpills['HIV/ART-Next Appointment date'].isnull()].copy()

dfdate = dfpills[dfpills['HIV/ART-Next Appointment date'].notna()].copy()

dfnodate = dfnodate.drop(columns =['HIV/ART-Next Appointment date'])

dfnodate['Last updated on'] = pd.to_datetime(dfnodate['Last updated on'],format='mixed',dayfirst=True)

dfnodate['HIV-ART Regimen - No. of days dispensed'] = pd.to_numeric(dfnodate['HIV-ART Regimen - No. of days dispensed'], errors='coerce')

dfnodate['days'] = pd.to_timedelta(dfnodate['HIV-ART Regimen - No. of days dispensed'],unit='D')

dfnodate['HIV/ART-Next Appointment date'] = dfnodate['Last updated on'] + dfnodate['days']

#dfb = pd.concat([dfdate, dfnodate])

dfs = [dfx for dfx in [dfdate, dfnodate] if not df.empty]


dfb = pd.concat(dfs, ignore_index=True)

#df = pd.concat([dfa, dfb])

dfs = [dfx for dfx in [dfa, dfb] if not df.empty]


df = pd.concat(dfs, ignore_index=True)

def pillcheck(days):
    if pd.isna(days):
        return None
    if days < 0:
        return 'NEXT APPT < LAST ENCOUNTER, CHECK'
    if 0 <= days < 30:
        return 'FEW DAYS DISPENSED, CHECK'
    if 30 <= days <= 185:
        return None
    if days > 185:
        return 'MANY DAYS DISPENSED, CHECK'
    return None

df['HIV-ART Regimen - No. of days dispensed'] = pd.to_numeric(df['HIV-ART Regimen - No. of days dispensed'], errors='coerce').copy()

df['DAYS ERROR']  = df['HIV-ART Regimen - No. of days dispensed'].apply(pillcheck)

dfmany = df[df['DAYS ERROR']=='MANY DAYS DISPENSED, CHECK'].copy()

dfew = df[df['DAYS ERROR']== 'FEW DAYS DISPENSED, CHECK'].copy()
dfqn = df[df['DAYS ERROR']== 'NEXT APPT < LAST ENCOUNTER, CHECK'].copy()
dfcorrect = df[~df['DAYS ERROR'].isin(['MANY DAYS DISPENSED, CHECK','FEW DAYS DISPENSED, CHECK', 'NEXT APPT < LAST ENCOUNTER, CHECK'])].copy()

df = pd.concat(
    [dfew, dfmany, dfqn, dfcorrect],
    ignore_index=True,
)
cols = ['MR - First name', 'MR - Surname', 'MR - Sex','Service Type','ART: Art Number','ART',
         'Last updated on','HIV-ART Regimen - No. of days dispensed', 'HIV/ART-Next Appointment date','ART_STATUS',  
         'DUP STATUS','DAYS_STATUS', 'days','DAYS ERROR']
seta = set(df.columns)
setb = set(cols)
setc = setb-seta
for cl in setc:
    df[cl] = np.nan
df = df[cols].copy()
for date_column in (
    'Last updated on',
    'HIV/ART-Next Appointment date',
):
    df[date_column] = pd.to_datetime(
        df[date_column],
        format='mixed',
        dayfirst=True,
        errors='coerce',
    ).dt.strftime('%d/%m/%Y')

checkd = {'NO ART NOs': dfart.shape[0],
          'DUPLICATED IN E-REG': dfdup.shape[0],
          'NEXT APPT < LAST ENCOUNTER, CHECK': dfqn.shape[0],
          'NO DAYS DISPENSED' : dfnoday.shape[0],
          'FEW DAYS DISPENSED' : dfew.shape[0],
          'TOO MANY DAYS DISPENSED': dfmany.shape[0]
}

has_data_issues = False
for key,value in checkd.items():
    if value>0:
        st.write(f'{key}: {value}')
        has_data_issues = True

if has_data_issues:
    st.download_button(
        "CLEAN DATA",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name="clean_data.csv",
        mime="text/csv",
    )
    st.stop()

b =df.shape[0] + dfmany.shape[0] + dfew.shape[0]+dfqn.shape[0] 



df.shape

if a-b !=0:
    st.warning('warning')
    st.stop()

st.info('NO ERRORS FOUND')
expected = df.shape[0]

df = df[['MR - First name', 'MR - Surname', 'MR - Sex' ,'HIV/ART-Next Appointment date', 'Last updated on','ART: Art Number','HIV-ART Regimen - No. of days dispensed']]

df = df.rename(columns = {'HIV/ART-Next Appointment date':'Return Visit Date', 'Last updated on':'Last Encounter Date',
                          'HIV-ART Regimen - No. of days dispensed': 'Days Dispensed'})

for date_column in ('Return Visit Date', 'Last Encounter Date'):
    df[date_column] = pd.to_datetime(
        df[date_column],
        format='mixed',
        dayfirst=True,
        errors='coerce',
    )

df['Rday'] = df['Return Visit Date'].dt.day

df['Rmonth'] = df['Return Visit Date'].dt.month

df['Ryear'] = df['Return Visit Date'].dt.year

df['Lday'] = df['Last Encounter Date'].dt.day

df['Lmonth'] = df['Last Encounter Date'].dt.month

df['Lyear'] = df['Last Encounter Date'].dt.year

df['ART'] = df['ART: Art Number'].astype(str).str.replace('[^0-9]', "", regex= True)

df2 = dfref.copy()

df2['ART'] = df2['Art'].astype(str).str.replace('[^0-9]', "", regex= True)

df2['ARVS'] = df2['ARVS'].astype(str).str.replace('/', '-')

df2['ART'] = pd.to_numeric(df2['ART'], errors = 'coerce')

df['ART'] = pd.to_numeric(df['ART'], errors = 'coerce')

df2 = df2[df2['ART'].notna()].copy()

df = df[df['ART'].notna()].copy()

df['ART'] = pd.to_numeric(df['ART'], errors = 'coerce')

df = df.drop_duplicates(subset= ['ART'], keep='first') ####DUPS WON'T PASS ANYWAY, REMOVE LATER

df['ART'] = pd.to_numeric(df['ART'], errors = 'coerce')

df2['ART'] = pd.to_numeric(df2['ART'], errors = 'coerce')

df = pd.merge(df, df2, on ='ART', how = 'left')

dfdupe =  df[df['ART'].duplicated()].copy()

df =  df[~df['ART'].duplicated()].copy()

dfdupe['REASON_REJECTED'] = 'DUPLICATED IN EMR, UPDATE ONE BY ONE'

dfnoart = df[df['Art'].isnull()].copy()

dfemr = df[df['Art'].notnull()].copy()

dfnoart['REASON_REJECTED'] = 'NOT IN EMR, MAY BE TX_NEWS/VISITORS'

matched_total = dfemr.shape[0] + dfnoart.shape[0]
if expected != matched_total:
    st.error(
        f"Row-count check failed: expected {expected}, but matched "
        f"{matched_total} rows. No clients were updated."
    )
    st.stop()

dfemr = dfemr[
    [
        'Art',
        'Days Dispensed',
        'Return Visit Date',
        'Last Encounter Date',
        'Rday',
        'Rmonth',
        'Ryear',
        'Lday',
        'Lmonth',
        'Lyear',
        'ART',
        'ARVS',
    ]
].copy()

st.write(f"READY TO UPDATE: {dfemr.shape[0]}")

if st.button("BATCH UPLOAD", type="primary"):
    failed_rows = []
    successful_updates = 0
    total_updates = dfemr.shape[0]
    progress = st.progress(0.0)
    status = st.empty()

    for position, (_, client) in enumerate(dfemr.iterrows(), start=1):
        art_number = str(client['Art']).strip()
        status.write(
            f"Updating {position} of {total_updates}: {art_number}"
        )
        try:
            update_client(
                base_url,
                username,
                password,
                art_number,
                date(
                    int(client['Lyear']),
                    int(client['Lmonth']),
                    int(client['Lday']),
                ).isoformat(),
                date(
                    int(client['Ryear']),
                    int(client['Rmonth']),
                    int(client['Rday']),
                ).isoformat(),
                int(client['Days Dispensed']),
                str(client['ARVS']).strip(),
            )
            successful_updates += 1
            print(
                f"[BATCH UPDATE SUCCESS] ART={art_number}",
                flush=True,
            )
        except Exception as exc:
            print(
                f"[BATCH UPDATE FAILED] ART={art_number}: {exc}",
                flush=True,
            )
            rejected = client.to_dict()
            rejected['REASON_REJECTED'] = 'FAILED TO UPDATE'
            failed_rows.append(rejected)

        if total_updates:
            progress.progress(position / total_updates)

    status.empty()
    dfrej = pd.DataFrame(failed_rows)
    rejected_frames = [
        rejected_frame
        for rejected_frame in (dfrej, dfnoart, dfdupe)
        if not rejected_frame.empty
    ]
    if rejected_frames:
        dfrej = pd.concat(
            rejected_frames,
            ignore_index=True,
            sort=False,
        )
    else:
        dfrej = pd.DataFrame(columns=['Art', 'REASON_REJECTED'])

    st.success(
        f"UPDATED: {successful_updates}; REJECTED: {dfrej.shape[0]}"
    )
    if not dfrej.empty:
        st.download_button(
            "DOWNLOAD REJECTED",
            data=dfrej.to_csv(index=False).encode('utf-8'),
            file_name='rejected_data.csv',
            mime='text/csv',
        )
