from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
import streamlit as st



USERNAME = "Rehemah"
PASSWORD = "Rehemah301996"
LOCATION_ID = "5"

st.title("OpenMRS Playwright Login")

ip_address = st.text_input("Write your IP address", placeholder="192.168.1.227")

if not ip_address or not ip_address.strip():
    st.warning("Please enter an IP address to continue.")
    st.stop()

ip_address = ip_address.strip()
base_url = f"http://{ip_address}:8081/openmrs"
login_url = f"{base_url}/login.htm"

st.markdown(f"**OpenMRS login URL:** {login_url}")

if st.button("Launch Playwright Login"):
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            page = browser.new_page()
            page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_selector("#username", timeout=30000)
            page.wait_for_selector("#password", timeout=30000)
            page.wait_for_selector("#sessionLocationInput", timeout=30000)
            page.wait_for_selector("#loginButton", timeout=30000)
            page.fill("#username", USERNAME)
            page.fill("#password", PASSWORD)
            page.select_option("#sessionLocationInput", LOCATION_ID)
            page.click("#loginButton")
            page.wait_for_selector("a#ugandaemr-findPatientLink-ugandaemr-findPatientLink-extension", timeout=30000)
            page.click("a#ugandaemr-findPatientLink-ugandaemr-findPatientLink-extension")
            page.wait_for_selector("#patient-search", timeout=30000)
            page.click("#patient-search")
            page.keyboard.press("Control+A")
            page.keyboard.type("MT623", delay=100)
            page.keyboard.press("Enter")
            page.wait_for_timeout(2000)
            page.wait_for_selector("i.icon-file-alt[title='Goto Patient Dashboard']", timeout=30000)
            page.click("i.icon-file-alt[title='Goto Patient Dashboard']")
            page.wait_for_selector("a:has-text('Add Past Visit')", timeout=30000)
            page.locator("a:has-text('Add Past Visit')").first.click()
            st.success("Login completed, patient dashboard opened, and Add Past Visit clicked.")
            st.write("The OpenMRS browser session was kept open for 10 seconds after navigation.")
            page.wait_for_timeout(10000)
            browser.close()
    except PlaywrightTimeoutError:
        st.error("Timed out while loading the OpenMRS login page or post-login page. Check the IP address and that the server is reachable.")
    except PlaywrightError as exc:
        st.error(f"Playwright error: {exc}")
    except Exception as exc:
        st.error(f"Unexpected error: {exc}")

