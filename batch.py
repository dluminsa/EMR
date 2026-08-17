import re
import time
from datetime import timedelta
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
import streamlit as st
import pandas as pd



#CREDENTIALS
filc = r'CREDENTIALS.csv'
datasets = r'BATCH_REFERENCE'


#CREDENTIALS_DF
dfcred = pd.read_csv(filc)
dfcred = dfcred[dfcred['user'].notna()].copy()
for credential_column in ("DISTRICT", "MICRO-CLUSTER", "FACILITY"):
    dfcred[credential_column] = (
        dfcred[credential_column].astype(str).str.strip()
    )



districts = dfcred['DISTRICT'].unique()

VISIT_DATE_CONFLICT_MESSAGE = (
    "The date you selected is conflicting with other visit(s). "
    "Click to navigate to a visit:"
)

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

# The HMIS form uses a jQuery UI datepicker whose month dropdown contains
# abbreviated labels instead of the full month names used by Add Past Visit.
HMIS_MONTH_MAP = {
    "1": "Jan",
    "2": "Feb",
    "3": "Mar",
    "4": "Apr",
    "5": "May",
    "6": "Jun",
    "7": "Jul",
    "8": "Aug",
    "9": "Sep",
    "10": "Oct",
    "11": "Nov",
    "12": "Dec",
}

ART_REGIMENS = [
    "d4T-3TC-NVP",
    "d4T-3TC-EFV",
    "AZT-3TC-NVP",
    "AZT-3TC-EFV",
    "AZT-3TC-DTG",
    "TDF-3TC-NVP",
    "TDF-3TC-EFV",
    "TDF-3TC-DTG",
    "TDF-FTC-NVP",
    "TDF-FTC-EFV",
    "ABC-ddI(250)-LPV/r",
    "ABC-ddI(400)-LPV/r",
    "TDF-3TC-LPV/r",
    "TDF-FTC-LPV/r",
    "ZDV-ddI(250)-LPV/r",
    "ZDV-ddI(400)-LPV/r",
    "AZT-3TC-LPV/r",
    "ABC-ddI-LPV/r",
    "ABC-ddI-NFV",
    "ABC-ddI-SQV/r",
    "ZDV-ddI-LPV/r",
    "AZT-ABC-LPV/r",
    "ABC-ddI-ATV/r",
    "AZT-3TC-ATV/r",
    "ABC-3TC-NVP",
    "ABC-3TC-EFV",
    "ABC-3TC-DTG",
    "ABC-3TC-ATV/r",
    "ABC-3TC-LPV/r",
    "TDF-3TC-ATV/r",
    "Other",
]

DEFAULT_ART_REGIMEN = "TDF-3TC-DTG"


class ArtNumberMismatchError(RuntimeError):
    """Raised when search results do not contain the exact requested ART."""


class ArtNumberNotFoundInEmrError(RuntimeError):
    """Raised when the EMR patient table has no matching records."""


class LoginUrlError(RuntimeError):
    """Raised when the selected facility login page cannot be opened."""


class InvalidCredentialsError(RuntimeError):
    """Raised when OpenMRS rejects the supplied username or password."""


class FacilityAccessError(RuntimeError):
    """Raised when the user cannot access the Find Patient application."""


def parse_date(date_string):
    day, month, year = date_string.split("/")
    return day.lstrip("0"), MONTH_MAP[month], int(year)


def parse_hmis_date(date_string):
    day, month, year = date_string.split("/")
    return str(int(day)), HMIS_MONTH_MAP[str(int(month))], int(year)


def open_login_page(page, login_url):
    """Open and validate the configured OpenMRS login page."""
    try:
        response = page.goto(
            login_url, wait_until="domcontentloaded", timeout=30000
        )
    except (PlaywrightTimeoutError, PlaywrightError) as exc:
        raise LoginUrlError from exc

    if response is not None and response.status >= 400:
        raise LoginUrlError

    try:
        page.locator("#username").wait_for(state="visible", timeout=10000)
        page.locator("#password").wait_for(state="visible", timeout=10000)
        page.locator("#sessionLocationInput").wait_for(
            state="visible", timeout=10000
        )
        page.locator("#loginButton").wait_for(
            state="visible", timeout=10000
        )
    except PlaywrightTimeoutError as exc:
        raise LoginUrlError from exc


def login_and_validate_access(page):
    """Submit login and require access to the Find Patient application."""
    page.click("#loginButton")

    invalid_message = page.locator("#error-message.alert-danger[role='alert']")
    find_patient_link = page.locator(
        "a#ugandaemr-findPatientLink-ugandaemr-findPatientLink-extension"
    )
    login_button = page.locator("#loginButton")
    deadline = time.monotonic() + 30
    access_missing_since = None

    while time.monotonic() < deadline:
        try:
            if invalid_message.count() and invalid_message.is_visible():
                message = (invalid_message.text_content() or "").strip()
                if "Invalid username/password" in message:
                    raise InvalidCredentialsError

            if find_patient_link.count() and find_patient_link.is_visible():
                return

            login_is_visible = login_button.is_visible()
            page_is_ready = page.evaluate("document.readyState") == "complete"
        except (InvalidCredentialsError, FacilityAccessError):
            raise
        except PlaywrightError:
            page.wait_for_timeout(250)
            continue

        if not login_is_visible and page_is_ready:
            if access_missing_since is None:
                access_missing_since = time.monotonic()
            elif time.monotonic() - access_missing_since >= 5:
                raise FacilityAccessError
        else:
            access_missing_since = None

        page.wait_for_timeout(250)

    if not login_button.is_visible():
        raise FacilityAccessError
    raise RuntimeError("OpenMRS login did not complete.")


def open_exact_art_dashboard(page, art_number):
    """Open the dashboard icon only from the exact ART-number result row."""
    dashboard_selector = "i.icon-file-alt[title='Goto Patient Dashboard']"
    no_records = page.locator("td.dataTables_empty:visible")
    visible_dashboards = page.locator(f"{dashboard_selector}:visible")
    search_deadline = time.monotonic() + 30

    while time.monotonic() < search_deadline:
        try:
            if no_records.count() > 0:
                empty_text = (no_records.first.text_content() or "").strip()
                if "No matching records found" in empty_text:
                    raise ArtNumberNotFoundInEmrError(
                        f"ART number {art_number} was not found in EMR."
                    )

            if visible_dashboards.count() > 0:
                break
        except ArtNumberNotFoundInEmrError:
            raise
        except PlaywrightError:
            pass

        page.wait_for_timeout(250)
    else:
        raise PlaywrightTimeoutError(
            "Timed out while waiting for patient search results."
        )

    exact_art = re.compile(
        rf"^\s*{re.escape(art_number)}\s*$", re.IGNORECASE
    )
    matching_cells = page.locator("td:visible").filter(has_text=exact_art)

    if matching_cells.count() == 0:
        raise ArtNumberMismatchError(
            f"Expected exact ART number {art_number}, but no exact result "
            "was found. No patient was updated."
        )

    for index in range(matching_cells.count()):
        result_row = matching_cells.nth(index).locator("xpath=ancestor::tr[1]")
        dashboard_icon = result_row.locator(dashboard_selector)
        if dashboard_icon.count() > 0 and dashboard_icon.first.is_visible():
            dashboard_icon.first.click()
            print(
                f"[patient-search] Opened exact ART result: {art_number}"
            )
            return

    raise RuntimeError(
        f"The exact ART row for {art_number} did not contain a visible "
        "patient-dashboard icon."
    )


def get_visible_calendar(page, timeout=3000):
    """Return the open date-picker popup.

    OpenMRS appends this Bootstrap widget to the document body, so it is not
    necessarily a child of the retrospective-visit dialog.
    """
    popup = page.locator(
        ".datetimepicker:visible, "
        ".datepicker:visible, "
        ".bootstrap-datetimepicker-widget:visible"
    ).last

    try:
        popup.wait_for(state="visible", timeout=timeout)
        popup.locator("th.switch:visible").first.wait_for(
            state="visible", timeout=timeout
        )
        return popup
    except PlaywrightTimeoutError:
        # Some OpenMRS builds render the picker inside the dialog without one
        # of the standard Bootstrap container classes.
        dialog = page.locator("#retrospective-visit-creation-dialog:visible")
        dialog.locator("th.switch:visible").first.wait_for(
            state="visible", timeout=timeout
        )
        return dialog


def open_start_date_picker(page):
    try:
        get_visible_calendar(page, timeout=500)
        return True
    except PlaywrightTimeoutError:
        pass

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
                        get_visible_calendar(page, timeout=2000)
                        print(
                            f"[open_picker] opened picker with '{sel}' "
                            f"element index={i}"
                        )
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
                    get_visible_calendar(page, timeout=2000)
                    print(
                        f"[open_picker] opened picker with fallback "
                        f"'{selector}' index={i}"
                    )
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
                get_visible_calendar(page, timeout=2000)
                print(f"[open_picker] opened picker with .icon-calendar index={i}")
                return True
        except Exception as e:
            print(f"[open_picker] .icon-calendar click failed index={i}: {e}")
            continue

    print("[open_picker] failed to open any start date picker element")
    return False


def select_calendar_date(page, day, month_name, year):
    target_month = MONTH_NAME_TO_NUMBER[month_name]
    calendar = get_visible_calendar(page, timeout=30000)
    header_locator = calendar.locator("th.switch:visible").first
    header_locator.wait_for(state="visible", timeout=30000)

    date_reached = False
    for _ in range(120):
        header = (header_locator.text_content() or "").strip()
        if not header:
            raise RuntimeError("The open calendar has no month/year heading.")
        parts = header.split()
        if len(parts) < 2:
            raise RuntimeError(f"Could not read the calendar heading: {header!r}")
        current_month = parts[0]
        try:
            current_year = int(parts[1])
        except ValueError as exc:
            raise RuntimeError(
                f"Could not read the year from calendar heading: {header!r}"
            ) from exc
        current_month_num = MONTH_NAME_TO_NUMBER.get(current_month)
        if current_month_num is None:
            raise RuntimeError(f"Unknown month in calendar heading: {header!r}")

        if current_year == year and current_month_num == target_month:
            date_reached = True
            break

        if current_year > year or (current_year == year and current_month_num > target_month):
            direction = "LEFT"
            arrow = calendar.locator(
                "th.prev:visible, i.icon-arrow-left:visible"
            ).first
        else:
            direction = "RIGHT"
            arrow = calendar.locator(
                "th.next:visible, i.icon-arrow-right:visible"
            ).first

        print(f"[calendar-debug] header={header} -> clicking {direction}")
        arrow.wait_for(state="visible", timeout=5000)
        try:
            arrow.click()
        except Exception as e:
            print(f"[calendar-debug] regular click failed: {e}")
            try:
                arrow.click(force=True)
                print("[calendar-debug] force click succeeded")
            except Exception as e2:
                print(f"[calendar-debug] force click failed: {e2}")
                arrow.evaluate("element => element.click()")
                print("[calendar-debug] DOM click succeeded")
        page.wait_for_timeout(200)

    if not date_reached:
        raise RuntimeError(
            f"Could not navigate the calendar to {month_name} {year}."
        )

    # Match the whole cell text so day 3 cannot accidentally match 13 or 23.
    exact_day = re.compile(rf"^\s*{re.escape(str(int(day)))}\s*$")
    day_cells = calendar.locator(
        "td.day:not(.old):not(.new):not(.disabled):visible"
    ).filter(has_text=exact_day)
    if day_cells.count() == 0:
        day_cells = calendar.locator("td.day:visible").filter(has_text=exact_day)

    if day_cells.count() == 0:
        raise RuntimeError(
            f"Day {day} is not selectable in {month_name} {year}."
        )

    day_el = day_cells.first
    print(f"[calendar-debug] exact day matches={day_cells.count()}")
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
            day_el.evaluate("element => element.click()")
            print("[calendar-debug] DOM day click succeeded")

    # Verify that the widget actually updated its linked hidden value.
    month_num = MONTH_NAME_TO_NUMBER[month_name]
    formatted = f"{year}-{month_num:02d}-{int(day):02d}"
    try:
        page.wait_for_function(
            """expected => {
                const field = document.querySelector(
                    '#retrospectiveVisitStartDate-field'
                );
                return field && field.value === expected;
            }""",
            arg=formatted,
            timeout=5000,
        )
    except PlaywrightTimeoutError as exc:
        field = page.locator("#retrospectiveVisitStartDate-field")
        actual = field.input_value() if field.count() else "<field missing>"
        raise RuntimeError(
            f"Calendar click did not set the start date. "
            f"Expected {formatted}, got {actual}."
        ) from exc


def confirm_past_visit(page):
    """Confirm the visit, returning False when its date already exists."""
    dialog = page.locator("#retrospective-visit-creation-dialog:visible")
    dialog.wait_for(state="visible", timeout=30000)

    confirm_button = dialog.locator("button.confirm.right").filter(
        has_text=re.compile(r"^\s*Confirm\s*$", re.IGNORECASE)
    )
    confirm_button.wait_for(state="visible", timeout=30000)

    if not confirm_button.is_enabled():
        raise RuntimeError(
            "The Add Past Visit Confirm button is disabled after selecting "
            "the date."
        )

    confirm_button.scroll_into_view_if_needed()
    confirm_button.click()
    print("[past-visit] Confirm clicked; waiting for the result")

    result = page.wait_for_function(
        """conflictMessage => {
            const isVisible = element => {
                if (!element) return false;
                const style = window.getComputedStyle(element);
                return style.display !== 'none' &&
                    style.visibility !== 'hidden' &&
                    element.getClientRects().length > 0;
            };

            const conflict = Array.from(document.querySelectorAll('span')).find(
                element =>
                    element.textContent.trim() === conflictMessage &&
                    isVisible(element)
            );
            if (conflict) return 'conflict';

            const dialog = document.querySelector(
                '#retrospective-visit-creation-dialog'
            );
            if (!isVisible(dialog)) return 'confirmed';

            return false;
        }""",
        arg=VISIT_DATE_CONFLICT_MESSAGE,
        timeout=30000,
    ).json_value()

    if result == "conflict":
        print("[past-visit] Date conflicts with an existing visit; skipping")
        return False

    print("[past-visit] Add Past Visit dialog closed")
    return True


def open_hmis_date_picker(page):
    """Open the date picker attached to the HMIS form's w6 input."""
    date_input = page.locator("input#w6-display.hasDatepicker")
    date_input.wait_for(state="visible", timeout=30000)
    date_input.scroll_into_view_if_needed()
    date_input.click()

    calendar = page.locator(
        "#ui-datepicker-div:visible, .ui-datepicker:visible"
    ).first
    calendar.wait_for(state="visible", timeout=10000)
    print("[hmis-form] Clicked #w6-display and opened its calendar")


def select_hmis_return_date(page, return_date):
    """Select a return date in the jQuery UI calendar for #w6-display."""
    day, month_label, year = parse_hmis_date(return_date)
    calendar = page.locator(
        "#ui-datepicker-div:visible, .ui-datepicker:visible"
    ).first
    calendar.wait_for(state="visible", timeout=10000)

    month_select = calendar.locator("select.ui-datepicker-month")
    month_select.wait_for(state="visible", timeout=10000)
    month_select.select_option(label=month_label)
    print(f"[hmis-form] Selected return month {month_label}")

    # Selecting a month can redraw the jQuery datepicker, so locate the year
    # dropdown again before interacting with it.
    year_select = calendar.locator("select.ui-datepicker-year")
    year_select.wait_for(state="visible", timeout=10000)
    year_select.select_option(value=str(year))
    print(f"[hmis-form] Selected return year {year}")

    exact_day = re.compile(rf"^\s*{re.escape(day)}\s*$")
    day_links = calendar.locator(
        "td[data-handler='selectDay']:not(.ui-datepicker-other-month) "
        "a.ui-state-default"
    ).filter(has_text=exact_day)
    if day_links.count() == 0:
        day_links = calendar.locator(
            "td:not(.ui-datepicker-other-month) a.ui-state-default"
        ).filter(has_text=exact_day)

    if day_links.count() == 0:
        raise RuntimeError(
            f"Day {day} is not selectable for {month_label} {year}."
        )

    day_links.first.click()
    print(f"[hmis-form] Selected return day {day}")

    date_input = page.locator("input#w6-display")
    try:
        page.wait_for_function(
            """expected => {
                const input = document.querySelector('input#w6-display');
                return input && input.value === expected;
            }""",
            arg=return_date,
            timeout=5000,
        )
    except PlaywrightTimeoutError as exc:
        actual = date_input.input_value()
        raise RuntimeError(
            f"Return date was not set correctly. Expected {return_date}, "
            f"got {actual or '<empty>'}."
        ) from exc


def select_first_hmis_provider(page):
    """Select the first provider after the empty placeholder option."""
    provider_select = page.locator("select[name='w9']").first
    provider_select.wait_for(state="visible", timeout=10000)

    options = provider_select.locator("option")
    for index in range(options.count()):
        option = options.nth(index)
        value = (option.get_attribute("value") or "").strip()
        provider_name = (option.text_content() or "").strip()
        if value and provider_name:
            provider_select.select_option(value=value)
            print(
                f"[hmis-form] Selected first provider: {provider_name} "
                f"(value={value})"
            )
            return provider_name

    raise RuntimeError("No provider names were available in select[name='w9'].")


def open_hmis_medication_tab(page):
    """Check w16 and open the HMIS Medication tab."""
    checkbox = page.locator("input#w16[name='w16'][value='164972']")
    checkbox.wait_for(state="visible", timeout=10000)
    checkbox.check()
    print("[hmis-form] Checked #w16")

    medication_tab = page.locator(
        "a.nav-link[data-toggle='tab'][href='#medication']"
    ).first
    medication_tab.wait_for(state="visible", timeout=10000)
    medication_tab.click()
    page.locator("#medication").wait_for(state="visible", timeout=10000)
    print("[hmis-form] Opened Medication tab")


def select_hmis_regimen(page, regimen=DEFAULT_ART_REGIMEN):
    """Select an ART regimen in the Medication tab."""
    if regimen not in ART_REGIMENS:
        raise ValueError(f"Unknown ART regimen: {regimen}")

    regimen_select = page.locator("select#w589[name='w589']")
    regimen_select.wait_for(state="visible", timeout=10000)
    regimen_select.select_option(label=regimen)

    selected_label = regimen_select.locator("option:checked").text_content()
    if (selected_label or "").strip() != regimen:
        raise RuntimeError(f"Could not select ART regimen {regimen}.")

    print(f"[hmis-form] Selected ART regimen: {regimen}")


def fill_hmis_dispensing_and_save(page, quantity):
    """Fill equal pill/day quantities and submit the HMIS form."""
    quantity_text = str(quantity)

    pills_input = page.locator("input#w593[name='w593']")
    pills_input.wait_for(state="visible", timeout=10000)
    pills_input.fill(quantity_text)

    days_input = page.locator("input#w595[name='w595']")
    days_input.wait_for(state="visible", timeout=10000)
    days_input.fill(quantity_text)

    if pills_input.input_value() != quantity_text:
        raise RuntimeError("The number of pills was not filled correctly.")
    if days_input.input_value() != quantity_text:
        raise RuntimeError("The number of days was not filled correctly.")

    print(
        f"[hmis-form] Filled {quantity_text} pills and {quantity_text} days"
    )

    save_button = page.locator(
        "input.submitButton.confirm[type='button'][value='Save']:visible"
    ).first
    save_button.wait_for(state="visible", timeout=10000)
    save_button.click()
    print("[hmis-form] Save clicked")


def refresh_client_inputs(success_message=None):
    """Reset client-specific widgets while preserving facility selections."""
    current_version = st.session_state.get("client_form_version", 0)
    st.session_state["client_form_version"] = current_version + 1
    if success_message:
        st.session_state["client_update_success"] = success_message
    st.rerun()


st.markdown(
    """
    <style>
    .block-container {
        max-width: 1100px;
        padding-top: 1.5rem;
        padding-bottom: 2.5rem;
    }
    h1 {
        color: #17324d;
        font-size: 2rem !important;
        letter-spacing: -0.02em;
    }
    .stApp,
    .stApp p,
    .stApp label,
    .stApp span,
    .stApp input,
    .stApp button,
    .stApp div[data-testid="stMarkdownContainer"] {
        font-weight: 700 !important;
    }
    div[data-testid="stNumberInput"],
    div[data-testid="stDateInput"] {
        background: #fbfcfe;
        border: 1px solid #dce4ed;
        border-radius: 12px;
        padding: 0.7rem 0.85rem 0.85rem;
    }
    div[data-testid="stNumberInput"]:has(
        input[aria-label="ART number"]
    ) button,
    div[data-testid="stNumberInput"]:has(
        input[aria-label="Days refilled"]
    ) button {
        display: none !important;
    }
    div[data-testid="stNumberInput"]:has(
        input[aria-label="ART number"]
    ) input,
    div[data-testid="stNumberInput"]:has(
        input[aria-label="Days refilled"]
    ) input {
        border-radius: 0.5rem !important;
    }
    div.stButton > button {
        width: 100%;
        min-height: 3rem;
        border-radius: 10px;
        border: 1px solid #0d47a1;
        background: #1565c0;
        color: #ffffff !important;
        font-weight: 800 !important;
        box-shadow: 0 4px 12px rgba(21, 101, 192, 0.25);
    }
    div.stButton > button:hover {
        border-color: #083b7a;
        background: #0d47a1;
        color: #ffffff !important;
    }
    div.stButton > button * {
        color: #ffffff !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("EMR ONE CLIENT UPDATE TOOL")

selected_district = st.radio(
    "DISTRICT",
    districts,
    index=None,
    horizontal=True,
    key="district_selection",
)
if selected_district is None:
    st.stop()

dfcred = dfcred[dfcred["DISTRICT"] == selected_district].copy()
micro_clusters = dfcred["MICRO-CLUSTER"].dropna().unique()

selected_micro_cluster = st.radio(
    "MICRO-CLUSTER",
    micro_clusters,
    index=None,
    horizontal=True,
    key=f"micro_cluster_{selected_district}",
)
if selected_micro_cluster is None:
    st.stop()

dfcred = dfcred[
    dfcred["MICRO-CLUSTER"] == selected_micro_cluster
].copy()
facilities = dfcred["FACILITY"].dropna().unique()

selected_facility = st.radio(
    "FACILITY",
    facilities,
    index=None,
    horizontal=True,
    key=f"facility_{selected_district}_{selected_micro_cluster}",
)
if selected_facility is None:
    st.stop()

facility_file = Path(datasets) / f"{selected_facility}.csv"
if not facility_file.is_file():
    st.error(
        f"No reference dataset was found for {selected_facility}: "
        f"{facility_file}"
    )
    st.stop()

dfref = pd.read_csv(facility_file)
missing_reference_columns = {"ART", "Art"} - set(dfref.columns)
if missing_reference_columns:
    st.error(
        f"{facility_file} is missing required column(s): "
        f"{', '.join(sorted(missing_reference_columns))}."
    )
    st.stop()

client_form_version = st.session_state.get("client_form_version", 0)
client_widget_prefix = (
    f"{selected_district}_{selected_micro_cluster}_"
    f"{selected_facility}_{client_form_version}"
)
success_message = st.session_state.pop("client_update_success", None)
if success_message:
    st.success(success_message)

input_columns = st.columns(4, gap="small")
with input_columns[0]:
    entered_art_number = st.number_input(
        "ART number",
        min_value=1,
        value=None,
        step=1,
        key=f"art_number_{client_widget_prefix}",
    )
if entered_art_number is None:
    st.stop()

dfref = dfref[dfref["ART"] == entered_art_number].copy()
if dfref.empty:
    st.info(
        f"ART NUMBER {entered_art_number} NOT FOUND for {selected_facility}."
    )
    st.stop()

matching_art_numbers = (
    dfref["Art"].dropna().astype(str).str.strip()
)
matching_art_numbers = matching_art_numbers[
    matching_art_numbers != ""
].unique().tolist()

if not matching_art_numbers:
    st.error(
        f"ART number {entered_art_number} has no full Art value in "
        f"{facility_file}."
    )
    st.stop()

if len(matching_art_numbers) > 1:
    st.info(
        f"{len(matching_art_numbers)} similar ART numbers exist. "
        "Select the one to update."
    )
    ART_NUMBER = st.radio(
        "ART number to update",
        matching_art_numbers,
        index=None,
        horizontal=True,
        key=f"matching_art_{client_widget_prefix}_{entered_art_number}",
    )
    if ART_NUMBER is None:
        st.stop()
else:
    ART_NUMBER = matching_art_numbers[0]

dfcred = dfcred[
    dfcred["FACILITY"] == selected_facility
].copy()
if dfcred.empty:
    st.error("No credentials matched the selected Facility.")
    st.stop()

ip_address = dfcred["ip"].iat[0]
USERNAME = dfcred["user"].iat[0]
PASSWORD = dfcred["password"].iat[0]
LOCATION_ID = "5"

if pd.isna(ip_address) or not str(ip_address).strip():
    st.error("The selected facility has no server address configured.")
    st.stop()
if pd.isna(USERNAME) or not str(USERNAME).strip():
    st.error("The selected facility has no username configured.")
    st.stop()
if pd.isna(PASSWORD) or not str(PASSWORD).strip():
    st.error("The selected facility has no password configured.")
    st.stop()

ip_address = str(ip_address).strip()
USERNAME = str(USERNAME).strip()
PASSWORD = str(PASSWORD).strip()

with input_columns[1]:
    last_encounter_date = st.date_input(
        "Last Encounter Date",
        value=None,
        format="DD/MM/YYYY",
        key=f"last_encounter_date_{client_widget_prefix}",
    )
if last_encounter_date is None:
    st.stop()

with input_columns[2]:
    refill_days = st.number_input(
        "Days refilled",
        min_value=1,
        value=None,
        step=1,
        key=f"refill_days_{client_widget_prefix}",
    )
if refill_days is None:
    st.stop()

with input_columns[3]:
    return_date_input = st.date_input(
        "Return Date",
        value=None,
        format="DD/MM/YYYY",
        key=f"return_date_{client_widget_prefix}",
    )
if return_date_input is None:
    st.stop()

if return_date_input <= last_encounter_date:
    st.warning("Return Date must be after Last Encounter Date.")
    st.stop()

expected_return_date = last_encounter_date + timedelta(days=int(refill_days))
return_date_difference = (return_date_input - expected_return_date).days

if abs(return_date_difference) >= 10:
    difference_direction = (
        "less" if return_date_difference < 0 else "more"
    )
    st.info(
        f"This client was given {refill_days} pills. Return Date entered "
        f"is {difference_direction} than expected by "
        f"{abs(return_date_difference)} days."
    )
    proceed_with_date_difference = st.radio(
        "Do you still want to update this client?",
        ["Yes", "No"],
        index=None,
        horizontal=True,
        key=f"date_override_{client_widget_prefix}",
    )
    if proceed_with_date_difference is None:
        st.stop()
    if proceed_with_date_difference == "No":
        refresh_client_inputs()

visit_date = last_encounter_date.strftime("%d/%m/%Y")
return_date = return_date_input.strftime("%d/%m/%Y")
base_url = f"http://{ip_address}:8081/openmrs"
login_url = f"{base_url}/login.htm"

if st.button("Update Emr", key=f"launch_{client_widget_prefix}"):
    try:
        update_succeeded = False
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            page = browser.new_page()
            open_login_page(page, login_url)
            page.fill("#username", USERNAME)
            page.fill("#password", PASSWORD)
            page.select_option("#sessionLocationInput", LOCATION_ID)
            login_and_validate_access(page)
            page.click("a#ugandaemr-findPatientLink-ugandaemr-findPatientLink-extension")
            page.wait_for_selector("#patient-search", timeout=30000)
            page.click("#patient-search")
            page.keyboard.press("Control+A")
            page.keyboard.type(ART_NUMBER, delay=100)
            page.keyboard.press("Enter")
            page.wait_for_timeout(2000)
            open_exact_art_dashboard(page, ART_NUMBER)
            page.wait_for_selector("a:has-text('Add Past Visit')", timeout=30000)
            page.locator("a:has-text('Add Past Visit')").first.click()
            page.wait_for_selector("text=Start Date", timeout=30000)
            opened = open_start_date_picker(page)
            if not opened:
                print("[open_picker] ERROR: Could not open the Start Date picker - aborting flow")
                raise Exception("Could not open the Start Date picker")

            visit_day, visit_month_name, visit_year = parse_date(visit_date)
            select_calendar_date(page, visit_day, visit_month_name, visit_year)

            visit_created = confirm_past_visit(page)

            if not visit_created:
                st.info("Date for this ART number was already added.")
            else:
                # Dots are literal characters in this element's id, so use an
                # attribute selector instead of treating them as CSS classes.
                hmis_form_link = page.locator(
                    "a[id='patientDashboard.visitActions.form.24'], "
                    "a:has-text('HMIS 003 HIV Care ART Card - Clinical Assessment')"
                ).first
                hmis_form_link.wait_for(state="visible", timeout=30000)
                hmis_form_link.click()
                open_hmis_date_picker(page)
                select_hmis_return_date(page, return_date)
                provider_name = select_first_hmis_provider(page)
                open_hmis_medication_tab(page)
                select_hmis_regimen(page)
                fill_hmis_dispensing_and_save(page, int(refill_days))
                print(
                    f"[hmis-form] Update completed for {ART_NUMBER}; "
                    f"provider={provider_name}, return_date={return_date}"
                )
                update_succeeded = True
            browser.close()
        if update_succeeded:
            refresh_client_inputs(
                f"SUCCESS: {ART_NUMBER} was updated successfully. "
                "Update another client."
            )
    except LoginUrlError:
        st.info("The facility EMR login address is incorrect or unreachable.")
    except InvalidCredentialsError:
        st.info("Invalid username/password. Please try again.")
    except FacilityAccessError:
        st.info(
            "Credentials provided do not have access to update facility EMR."
        )
    except ArtNumberNotFoundInEmrError as exc:
        st.info(str(exc))
    except ArtNumberMismatchError as exc:
        st.info(f"Warning: {exc}")
    except PlaywrightTimeoutError:
        st.error(
            "Timed out while connecting to the selected facility server."
        )
    except PlaywrightError as exc:
        print(f"[playwright-error] {exc}")
        st.error("Browser automation failed while updating the client.")
    except Exception as exc:
        print(f"[unexpected-error] {exc}")
        st.error("An unexpected error occurred while updating the client.")
