import asyncio

import pandas as pd
import streamlit as st
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


CSV_PATH = r"C:\Users\Desire Luminsa\Desktop\PROJECT CENTCOM\PLAYWRIGHT\MIRAMBI.csv"


@st.cache_data
def load_patients():
    return pd.read_csv(CSV_PATH, dtype=str).fillna("")


def normalize_name(value):
    return " ".join(str(value).upper().split())


async def update_emr(art_number, first_name, family_name):
    expected_names = {
        normalize_name(f"{first_name} {family_name}"),
        normalize_name(f"{family_name} {first_name}"),
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()

        try:
            await page.goto("http://192.168.1.40:8081/openmrs/login.htm")
            await page.fill("#username", "nbetty")
            await page.fill("#password", "User@2018")
            await page.select_option("#sessionLocationInput", "5")
            await page.click("#loginButton")

            await page.wait_for_timeout(2000)
            await page.click("#ugandaemr-findPatientLink-ugandaemr-findPatientLink-extension")

            await page.locator("#patient-search").wait_for()
            await page.click("#patient-search")
            await page.keyboard.press("Control+A")
            await page.keyboard.type(art_number, delay=100)
            await page.get_by_text("Name", exact=True).wait_for(timeout=30000)
            await page.wait_for_timeout(1000)

            rows = page.locator("tr")
            row_count = await rows.count()

            for index in range(row_count):
                row = rows.nth(index)
                row_text = normalize_name(await row.inner_text())

                if any(expected_name in row_text for expected_name in expected_names):
                    dashboard_icon = row.locator(
                        "i.icon-file-alt[title='Goto Patient Dashboard']"
                    )
                    if await dashboard_icon.count() == 0:
                        await browser.close()
                        return False, "Patient found, but dashboard icon was not found."

                    await dashboard_icon.first.click()
                    await page.wait_for_timeout(2000)
                    await browser.close()
                    return True, f"Opened dashboard for ART {art_number}."

            await browser.close()
            return False, "Patient search result not found or name did not match."
        except PlaywrightTimeoutError as error:
            await browser.close()
            return False, f"Timed out while updating EMR: {error}"


patients = load_patients()

cluster = st.radio(
    "Cluster",
    sorted(patients["CLUSTER"].dropna().unique()),
    index=None,
    horizontal=True,
)

if not cluster:
    st.stop()

cluster_patients = patients[patients["CLUSTER"] == cluster]

district = st.radio(
    "District",
    sorted(cluster_patients["DISTRICT"].dropna().unique()),
    index=None,
)

if not district:
    st.stop()

district_patients = cluster_patients[cluster_patients["DISTRICT"] == district]

facility_col, art_col = st.columns(2)

with facility_col:
    facility = st.selectbox(
        "Facility",
        sorted(district_patients["FACILITY"].dropna().unique()),
        index=None,
        placeholder="Choose a facility",
    )

if not facility:
    st.stop()

facility_patients = district_patients[district_patients["FACILITY"] == facility]

with art_col:
    art_search = st.text_input("ART Number")

if not art_search:
    st.stop()

matches = facility_patients[
    facility_patients["ART"].str.contains(art_search, case=False, na=False)
]

if matches.empty:
    st.warning("No matching ART number found.")
    st.stop()

art_options = {}
for _, row in matches.iterrows():
    label = f"{row['ART']} - {row['NAME']} {row['Family Name']}".strip()
    art_options[label] = {
        "art": row["ART"],
        "first_name": row["NAME"],
        "family_name": row["Family Name"],
    }

selected_patient = st.radio(
    "Select Patient",
    list(art_options.keys()),
    index=None,
)

if not selected_patient:
    st.stop()

selected_patient_details = art_options[selected_patient]

if st.button("UPDATE EMR"):
    success, message = asyncio.run(
        update_emr(
            selected_patient_details["art"],
            selected_patient_details["first_name"],
            selected_patient_details["family_name"],
        )
    )

    if success:
        st.success(message)
    else:
        st.error(message)
