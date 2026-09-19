"""BACKUP: CSV validation from batch4 and browser workflow from batch.py."""
import re
import time
import hashlib
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
import streamlit as st
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

CREDENTIALS_FILE = Path(__file__).resolve().parent / "CREDENTIALS.csv"
REFERENCE_DIR = Path(__file__).resolve().parent / "BATCH_REFERENCE"
LOCATION_ID = "5"
REQUIRED_UPLOAD_COLUMNS = {"ART", "LD", "ARVD"}
REQUIRED_REFERENCE_COLUMNS = {"Art", "ARVS"}

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
    "AZT-3TC-DTG",
    "TDF-3TC-EFV",
    "TDF-3TC-DTG",
    "ABC-3TC-DTG",
    "TDF-3TC-DRV/r",
    "Other",
]

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


def select_hmis_regimen(page, regimen):
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


def require_saved(response):
    """Wait for an explicit acknowledgement before counting a successful update."""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if (response.status < 400 and isinstance(payload, dict)
            and payload.get("success") is True and not payload.get("errors")):
        return
    detail = (
        payload.get("errors") or payload.get("message") or "Save was not confirmed."
        if isinstance(payload, dict)
        else f"HTTP {response.status}; no successful save acknowledgement."
    )
    raise RuntimeError(f"OpenMRS: {detail} Check the encounter before retrying.")


def update_client(base_url, username, password, art_number, visit_date,
                  return_date, quantity, regimen):
    """Each ART number gets a fresh browser, closed on success or failure."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        try:
            page = browser.new_page()
            open_login_page(page, base_url.rstrip("/") + "/login.htm")
            page.fill("#username", username)
            page.fill("#password", password)
            page.select_option("#sessionLocationInput", LOCATION_ID)
            login_and_validate_access(page)
            page.click("a#ugandaemr-findPatientLink-ugandaemr-findPatientLink-extension")
            page.wait_for_selector("#patient-search", timeout=30000)
            page.click("#patient-search")
            page.keyboard.press("Control+A")
            page.keyboard.type(art_number, delay=100)
            page.keyboard.press("Enter")
            page.wait_for_timeout(2000)
            open_exact_art_dashboard(page, art_number)
            page.wait_for_selector("a:has-text('Add Past Visit')", timeout=30000)
            page.locator("a:has-text('Add Past Visit')").first.click()
            page.wait_for_selector("text=Start Date", timeout=30000)
            if not open_start_date_picker(page):
                raise RuntimeError("Could not open the Start Date picker.")
            select_calendar_date(page, *parse_date(visit_date))
            if not confirm_past_visit(page):
                raise RuntimeError(
                    "Visit date conflicts with an existing visit. Review that visit "
                    "in OpenMRS before retrying; no refill was submitted."
                )
            link = page.locator(
                "a[id='patientDashboard.visitActions.form.24'], "
                "a:has-text('HMIS 003 HIV Care ART Card - Clinical Assessment')"
            ).first
            link.wait_for(state="visible", timeout=30000)
            link.click()
            open_hmis_date_picker(page)
            select_hmis_return_date(page, return_date)
            select_first_hmis_provider(page)
            open_hmis_medication_tab(page)
            select_hmis_regimen(page, regimen)
            try:
                with page.expect_response(
                    lambda response: (
                        "enterHtmlForm/submit.action" in response.url
                        and response.request.method == "POST"
                    ), timeout=45000,
                ) as saved:
                    fill_hmis_dispensing_and_save(page, quantity)
                require_saved(saved.value)
            except PlaywrightTimeoutError as exc:
                raise RuntimeError(
                    "Save was not confirmed before timeout. Check the patient's "
                    "encounter before retrying."
                ) from exc
        except LoginUrlError as exc:
            raise RuntimeError("The facility login address is incorrect or unreachable.") from exc
        except InvalidCredentialsError as exc:
            raise RuntimeError("Invalid username/password.") from exc
        except FacilityAccessError as exc:
            raise RuntimeError("The account cannot access Find Patient.") from exc
        finally:
            browser.close()


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
            with ThreadPoolExecutor(max_workers=1) as worker:
                worker.submit(update_client,
                base_url,
                username,
                password,
                art_number,
                client["LD_PARSED"].strftime("%d/%m/%Y"),
                client["RD_PARSED"].strftime("%d/%m/%Y"),
                int(client["ARVD_PARSED"]),
                str(client["ARVS"]).strip(),
                ).result()
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
    st.set_page_config(page_title="BACKUP", layout="wide")
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
    st.title("BACKUP")
    st.caption("A fresh browser opens for each ART number and closes after each attempt.")
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
    st.dataframe(rejected, use_container_width=True, hide_index=True)
    show_rejected_download(rejected, key="validation_rejected")
    with st.expander("Review rows ready to update"):
        st.dataframe(ready, use_container_width=True, hide_index=True)

    if ready.empty:
        st.warning("There are no valid rows to update.")
        st.stop()

    base_url = f"http://{str(credential['ip']).strip()}:8081/openmrs"
    username = str(credential["user"]).strip()
    password = str(credential["password"]).strip()
    batch_key = hashlib.sha256(
        uploaded_file.getvalue() + csv_bytes(reference)
        + f"{district}|{facility}|{base_url}|{username}".encode("utf-8")
    ).hexdigest()
    result_key = f"backup_result_{batch_key}"
    if st.button("BATCH UPLOAD", type="primary", disabled=result_key in st.session_state):
        successful, final_rejected = run_updates(
            ready, rejected, base_url, username, password
        )
        st.session_state[result_key] = (successful, final_rejected)
        st.rerun()
    if result_key in st.session_state:
        successful, final_rejected = st.session_state[result_key]
        st.success(f"UPDATED: {successful}; REJECTED: {len(final_rejected)}")
        st.dataframe(final_rejected, use_container_width=True, hide_index=True)
        show_rejected_download(final_rejected, key="final_rejected")


if __name__ == "__main__":
    main()

