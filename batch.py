from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
import streamlit as st



USERNAME = "Rehemah"
PASSWORD = "Rehemah301996"
LOCATION_ID = "5"

MONTH_MAP = {
    "1": "January",
    "01": "January",
    "2": "February",
    "02": "February",
    "3": "March",
    "03": "March",
    "4": "April",
    "04": "April",
    "5": "May",
    "05": "May",
    "6": "June",
    "06": "June",
    "7": "July",
    "07": "July",
    "8": "August",
    "08": "August",
    "9": "September",
    "09": "September",
    "10": "October",
    "11": "November",
    "12": "December",
}

MONTH_NAME_TO_NUMBER = {
    "January": 1,
    "February": 2,
    "March": 3,
    "April": 4,
    "May": 5,
    "June": 6,
    "July": 7,
    "August": 8,
    "September": 9,
    "October": 10,
    "November": 11,
    "December": 12,
}


def parse_date(date_string):
    day, month, year = date_string.split("/")
    return day.lstrip("0"), MONTH_MAP[month], int(year)


def open_start_date_picker(page):
    # First try the retrospective dialog wrapper add-on which opens the picker
    wrapper_selectors = [
        "#retrospectiveVisitStartDate-wrapper .add-on",
        "#retrospectiveVisitStartDate-wrapper .icon-calendar",
        "#retrospectiveVisitStartDate-display",
    ]
    print(f"[open_picker] checking wrapper selectors: {wrapper_selectors}")
    for sel in wrapper_selectors:
        count = page.locator(sel).count()
        print(f"[open_picker] selector='{sel}' count={count}")
        if count > 0:
            for i in range(count):
                el = page.locator(sel).nth(i)
                try:
                    visible = el.is_visible()
                except Exception:
                    visible = False
                print(f"[open_picker] - element index={i} visible={visible}")
                try:
                    if visible:
                        el.click()
                        print(f"[open_picker] clicked '{sel}' element index={i}")
                        return True
                except Exception as e:
                    print(f"[open_picker] click failed for '{sel}' index={i}: {e}")
                    continue

    # Fallback: try other visible start inputs on the page
    selector_candidates = [
        "input[placeholder='Start Date']",
        "input[placeholder*='Start']",
        "input[aria-label='Start Date']",
    ]
    print(f"[open_picker] checking fallback input selectors: {selector_candidates}")
    for selector in selector_candidates:
        locator = page.locator(selector)
        count = locator.count()
        print(f"[open_picker] fallback selector='{selector}' count={count}")
        for i in range(count):
            el = locator.nth(i)
            try:
                visible = el.is_visible()
            except Exception:
                visible = False
            print(f"[open_picker] - fallback element index={i} visible={visible}")
            try:
                if visible:
                    el.click()
                    print(f"[open_picker] clicked fallback '{selector}' index={i}")
                    return True
            except Exception as e:
                print(f"[open_picker] fallback click failed for '{selector}' index={i}: {e}")
                continue

    # Last resort: inspect calendar icons
    icons = page.locator(".icon-calendar")
    icon_count = icons.count()
    print(f"[open_picker] .icon-calendar count={icon_count}")
    for i in range(icon_count):
        try:
            el = icons.nth(i)
            visible = el.is_visible()
        except Exception:
            visible = False
        print(f"[open_picker] icon index={i} visible={visible}")
        try:
            if visible:
                el.click()
                print(f"[open_picker] clicked .icon-calendar index={i}")
                return True
        except Exception as e:
            print(f"[open_picker] .icon-calendar click failed index={i}: {e}")
            continue

    print("[open_picker] failed to open any start date picker element")
    return False


def select_calendar_date(page, day, month_name, year):
    # Scope interactions to the retrospective dialog to avoid other calendars
    dialog = "#retrospective-visit-creation-dialog"
    target_month = MONTH_NAME_TO_NUMBER[month_name]
    header_locator = page.locator(f"{dialog} th.switch")
    header_locator.first.wait_for(state="visible", timeout=30000)

    for _ in range(48):
        header = header_locator.first.text_content()
        if not header:
            break
        parts = header.split()
        if len(parts) < 2:
            break
        current_month = parts[0]
        current_year = int(parts[1])
        current_month_num = MONTH_NAME_TO_NUMBER.get(current_month)
        if current_month_num is None:
            break

        if current_year == year and current_month_num == target_month:
            break

        if current_year > year or (current_year == year and current_month_num > target_month):
            arrow_sel = f"{dialog} i.icon-arrow-left"
            arrows = page.locator(arrow_sel)
            print(f"[calendar-debug] header={header} -> clicking LEFT arrows.count={arrows.count()}")
            try:
                # try normal click first
                arrows.first.click()
            except Exception as e:
                print(f"[calendar-debug] regular click failed: {e}")
                try:
                    arrows.first.click(force=True)
                    print("[calendar-debug] click with force succeeded")
                except Exception as e2:
                    print(f"[calendar-debug] force click failed: {e2}")
                    # fallback to DOM click
                    page.evaluate("sel => document.querySelector(sel) && document.querySelector(sel).click()", arrow_sel)
                    print("[calendar-debug] evaluate DOM click attempted")
        else:
            arrow_sel = f"{dialog} i.icon-arrow-right"
            arrows = page.locator(arrow_sel)
            print(f"[calendar-debug] header={header} -> clicking RIGHT arrows.count={arrows.count()}")
            try:
                arrows.first.click()
            except Exception as e:
                print(f"[calendar-debug] regular click failed: {e}")
                try:
                    arrows.first.click(force=True)
                    print("[calendar-debug] click with force succeeded")
                except Exception as e2:
                    print(f"[calendar-debug] force click failed: {e2}")
                    page.evaluate("sel => document.querySelector(sel) && document.querySelector(sel).click()", arrow_sel)
                    print("[calendar-debug] evaluate DOM click attempted")
        page.wait_for_timeout(300)

    # Click the day inside the dialog calendar
    day_selector = f"{dialog} td.day:not(.old):not(.new):has-text('{day}')"
    if page.locator(day_selector).count() == 0:
        day_selector = f"{dialog} td.day:has-text('{day}')"
    day_el = page.locator(day_selector).first
    print(f"[calendar-debug] day_selector='{day_selector}', count={page.locator(day_selector).count()}")
    try:
        day_el.wait_for(state="visible", timeout=10000)
        day_el.click()
        print("[calendar-debug] day click succeeded")
    except Exception as e:
        print(f"[calendar-debug] day click failed: {e}")
        try:
            day_el.click(force=True)
            print("[calendar-debug] day click with force succeeded")
        except Exception as e2:
            print(f"[calendar-debug] day force click failed: {e2}")
            # fallback to DOM click via selector inside dialog
            page.evaluate("sel => { const el = document.querySelector(sel); if(el){ el.click(); return true } return false }", day_selector)
            print("[calendar-debug] evaluate DOM day click attempted")

    # wait for hidden linked field to update (yyyy-mm-dd)
    month_num = MONTH_NAME_TO_NUMBER[month_name]
    formatted = f"{year}-{month_num:02d}-{int(day):02d}"
    try:
        page.wait_for_selector(f"#retrospectiveVisitStartDate-field[value='{formatted}']", timeout=3000)
    except Exception:
        page.wait_for_timeout(500)


st.title("OpenMRS Playwright Login")

ip_address = st.text_input("Write your IP address", placeholder="192.168.1.227")
visit_date = st.text_input("Past visit date (dd/mm/yyyy)", value="13/07/2026")

if not ip_address or not ip_address.strip():
    st.warning("Please enter an IP address to continue.")
    st.stop()

if not visit_date or not visit_date.strip():
    st.warning("Please enter a past visit date in dd/mm/yyyy format.")
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
            page.wait_for_selector("text=Start Date", timeout=30000)
            opened = open_start_date_picker(page)
            if not opened:
                print("[open_picker] ERROR: Could not open the Start Date picker - aborting flow")
                raise Exception("Could not open the Start Date picker")

            visit_day, visit_month_name, visit_year = parse_date(visit_date)
            page.wait_for_selector("th.switch", timeout=30000)
            select_calendar_date(page, visit_day, visit_month_name, visit_year)

            page.wait_for_selector("button.confirm.right", timeout=30000)
            page.click("button.confirm.right")
            page.wait_for_timeout(1000)
            page.wait_for_selector("a#patientDashboard.visitActions.form.24, a:has-text('HMIS 003 HIV Care ART Card - Clinical Assessment')", timeout=30000)
            page.click("a#patientDashboard.visitActions.form.24")
            page.wait_for_timeout(2000)

            st.success("Login completed, patient dashboard opened, Add Past Visit clicked, date confirmed, and HMIS form opened.")
            st.write("The OpenMRS browser session was kept open for 10 seconds after navigation.")
            page.wait_for_timeout(10000)
            browser.close()
    except PlaywrightTimeoutError:
        st.error("Timed out while loading the OpenMRS login page or post-login page. Check the IP address and that the server is reachable.")
    except PlaywrightError as exc:
        st.error(f"Playwright error: {exc}")
    except Exception as exc:
        st.error(f"Unexpected error: {exc}")

