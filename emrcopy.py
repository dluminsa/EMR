import asyncio
import json
import os
import re
import sys
import tempfile
import traceback
import warnings
from pathlib import Path

import time
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PWTimeoutError
from playwright.async_api import async_playwright

EXPORT_NAME = "VITALS ONLY"
BASE_URL = "http://10.3.1.5:8081/openmrs"
LOGIN_URL = f"{BASE_URL}/login.htm"
DATA_EXPORT_LIST_URL = f"{BASE_URL}/admin/reports/dataExport.list"
FINAL_OUTPUT_DIR = Path(r"C:\Users\Desire Luminsa\Desktop\PROJECT CENTCOM\EXTRACTS")
NETWORK_LOG_FILE = Path(r"C:\Users\Desire Luminsa\Desktop\PROJECT CENTCOM\emr_network_log.jsonl")
DOWNLOAD_PROGRESS: dict[str, dict[str, int | str]] = {}

# Hardcoded credentials (from emr.py)
USERNAME = "admin"
PASSWORD = "Admin123"
LOCATION_ID = "5"  # ART Clinic

NETWORK_KEYWORDS = [
    "/openmrs/",
    "/openmrs/admin/",
    "dataExport",
    "csv",
    "download",
    "export",
    "servlet",
]

SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "set-cookie",
    "x-csrf-token",
    "x-xsrf-token",
}

SENSITIVE_BODY_KEYS = {
    "password",
    "pwd",
    "token",
    "accessToken",
    "refreshToken",
}

if sys.platform.startswith("win"):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        proactor_policy = getattr(asyncio, "WindowsProactorEventLoopPolicy", None)
        if proactor_policy is not None:
            asyncio.set_event_loop_policy(proactor_policy())


def should_log_url(url: str) -> bool:
    low_url = url.lower()
    if BASE_URL.lower() not in low_url:
        return False
    return any(keyword in low_url for keyword in NETWORK_KEYWORDS)


def cleaned_headers(headers: dict) -> dict:
    cleaned = {}
    for key, value in headers.items():
        if key.lower() in SENSITIVE_HEADERS:
            cleaned[key] = "[REDACTED]"
        else:
            cleaned[key] = value
    return cleaned


def cleaned_post_data(url: str, post_data: str | None):
    if not post_data:
        return None

    if "login" in url.lower():
        return "[REDACTED]"

    try:
        data = json.loads(post_data)
    except json.JSONDecodeError:
        return post_data

    def redact(value):
        if isinstance(value, dict):
            return {
                key: "[REDACTED]" if key in SENSITIVE_BODY_KEYS else redact(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [redact(item) for item in value]
        return value

    return json.dumps(redact(data), ensure_ascii=True)


def append_network_log(event: dict) -> None:
    NETWORK_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with NETWORK_LOG_FILE.open("a", encoding="utf-8") as log_file:
        log_file.write(json.dumps(event, ensure_ascii=True) + "\n")


def install_network_logger(page, facility: str | None = None):
    label = facility or "EMR_EXTRACT"

    def log_request(request):
        if not should_log_url(request.url):
            return

        event = {
            "facility": label,
            "event": "request",
            "method": request.method,
            "url": request.url,
            "resource_type": request.resource_type,
            "headers": cleaned_headers(request.headers),
            "post_data": cleaned_post_data(request.url, request.post_data),
        }
        print("\n[NETWORK REQUEST]")
        print(f"{request.method} {request.url}")
        print(f"resource_type={request.resource_type}")
        if event["post_data"]:
            print(f"post_data={event['post_data']}")
        append_network_log(event)

    def log_response(response):
        if not should_log_url(response.url):
            return

        event = {
            "facility": label,
            "event": "response",
            "status": response.status,
            "url": response.url,
            "headers": cleaned_headers(response.headers),
            "content_type": response.headers.get("content-type"),
        }
        print("\n[NETWORK RESPONSE]")
        print(f"{response.status} {response.url}")
        print(f"content_type={response.headers.get('content-type')}")
        append_network_log(event)

    def log_download(download):
        event = {
            "facility": label,
            "event": "download",
            "url": download.url,
            "suggested_filename": download.suggested_filename,
        }
        print("\n[DOWNLOAD]")
        print(f"url={download.url}")
        print(f"suggested_filename={download.suggested_filename}")
        append_network_log(event)

    page.on("request", log_request)
    page.on("response", log_response)
    page.on("download", log_download)


# --------------------------------------------------
# Helpers
# --------------------------------------------------
async def safe_click(locator, timeout=20000):
    await locator.wait_for(state="visible", timeout=timeout)
    await locator.click()


async def wait_a_bit(page, ms=300):
    await page.wait_for_timeout(ms)


async def get_export_row(page, export_name: str):
    export_link = page.get_by_role("link", name=export_name, exact=True)
    await export_link.wait_for(state="visible", timeout=30000)
    row = export_link.locator("xpath=ancestor::tr[1]")
    await row.wait_for(state="visible", timeout=30000)
    return row


async def stop_loading(page):
    try:
        await page.evaluate("window.stop()")
    except (PlaywrightError, PWTimeoutError):
        pass


async def open_data_export_list(page):
    try:
        await page.goto(DATA_EXPORT_LIST_URL, wait_until="domcontentloaded", timeout=30000)
    except PWTimeoutError:
        await page.goto(DATA_EXPORT_LIST_URL, wait_until="commit", timeout=30000)
    await page.locator("input[name='dataExportId']").first.wait_for(state="visible", timeout=30000)


# --------------------------------------------------
# Main
# --------------------------------------------------
async def main(username: str, password: str, location_id: str, final_output: Path, facility: str | None = None):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=60)
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()
        install_network_logger(page, facility)

        try:
            # ============ LOGIN ============
            print("\n======== LOGGING IN ========")
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            print(f"✓ Navigated to {LOGIN_URL}")
            
            await page.wait_for_selector("#username", state="visible", timeout=60000)
            await page.fill("#username", username)
            print(f"✓ Filled username: {username}")
            
            await page.fill("#password", password)
            print("✓ Filled password")
            
            await page.select_option("#sessionLocationInput", location_id)
            print(f"✓ Selected location: {location_id}")
            
            await page.click("#loginButton")
            print("✓ Clicked login button")
            
            await page.wait_for_load_state("networkidle", timeout=30000)
            await wait_a_bit(page, 2000)
            print("✓ Login completed and page loaded")

            # ============ NAVIGATE TO ADMIN ============
            print("\n======== NAVIGATING TO LEGACY ADMIN ========")
            await page.wait_for_selector(
                "#ugandaemr-referenceapplication-legacyAdmin-homepageLink-ugandaemr-referenceapplication-legacyAdmin-homepageLink-extension",
                state="visible",
                timeout=30000
            )
            await page.click("#ugandaemr-referenceapplication-legacyAdmin-homepageLink-ugandaemr-referenceapplication-legacyAdmin-homepageLink-extension")
            print("✓ Clicked Legacy System Administration")
            
            await page.wait_for_load_state("networkidle", timeout=30000)
            await wait_a_bit(page, 2000)

            # ============ NAVIGATE TO DATA EXPORTS ============
            print("\n======== NAVIGATING TO MANAGE DATA EXPORTS ========")
            await page.wait_for_selector("a[href*='/openmrs/admin/reports/dataExport.list']", state="visible", timeout=30000)
            await page.click("a[href*='/openmrs/admin/reports/dataExport.list']")
            print("✓ Clicked Manage Data Exports")
            
            await page.wait_for_load_state("networkidle", timeout=30000)
            await wait_a_bit(page, 2000)

            # ============ SELECT EXPORT ============
            print(f"\n======== SELECTING {EXPORT_NAME} ========")
            export_row = await get_export_row(page, EXPORT_NAME)
            export_checkbox = export_row.locator("input[name='dataExportId']").first
            await export_checkbox.click()
            print("✓ Clicked EMR EXTRACT checkbox")
            
            await wait_a_bit(page, 1000)

            # ============ GENERATE EXPORTS ============
            print("\n======== GENERATING EXPORTS ========")
            await page.wait_for_selector("input[type='submit'][value='Generate Exports']", state="visible", timeout=30000)
            await page.locator("input[type='submit'][value='Generate Exports']").click(no_wait_after=True)
            print("✓ Clicked Generate Exports button")
            
            # Wait longer for export generation
            await wait_a_bit(page, 10000)
            await stop_loading(page)
            await open_data_export_list(page)
            await wait_a_bit(page, 1000)
            print("✓ Export generation completed")

            # ============ DOWNLOAD EXPORT ============
            print("\n======== DOWNLOADING EXPORT ========")
            export_row = await get_export_row(page, EXPORT_NAME)
            download_link = export_row.get_by_role("link", name="Download", exact=True)
            await download_link.wait_for(state="visible", timeout=30000)
            
            async with page.expect_download() as download_info:
                await download_link.click()
                print("✓ Clicked Download link")

            download = await download_info.value
            suggested = download.suggested_filename or "EMR_EXTRACT.csv"
            save_path = final_output / suggested
            
            final_output.mkdir(parents=True, exist_ok=True)
            await download.save_as(str(save_path))
            print(f"✓ Download saved to: {save_path}")
            
            # Verify file size
            file_size = os.path.getsize(save_path)
            print(f"✓ File size: {file_size} bytes")

            await wait_a_bit(page, 1000)

        except Exception as e:
            print(f"\n❌ ERROR: {e}")
            print(traceback.format_exc())
            raise
        finally:
            await browser.close()
            print("\n✓ Browser closed")


async def run_extraction(username: str, password: str, location_id: str) -> None:
    FINAL_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_file = FINAL_OUTPUT_DIR
    print("\n==============================")
    print(f"EMR EXTRACT - Network Logger")
    print("==============================")
    await main(username, password, location_id, out_file, facility="EMR_EXTRACT")
    print(f"✓ Extraction completed")


# ============ MAIN EXECUTION ============
if __name__ == "__main__":
    print(f"\n📝 Using hardcoded credentials (admin flow)")
    print(f"📝 Network log will be saved to: {NETWORK_LOG_FILE}")
    print(f"📝 Extracts will be saved to: {FINAL_OUTPUT_DIR}")
    
    # Run extraction with hardcoded credentials
    asyncio.run(run_extraction(USERNAME, PASSWORD, LOCATION_ID))
    
    print("\n" + "="*50)
    print("🎯 NETWORK LOG ANALYSIS")
    print("="*50)
    print(f"Check the network log at: {NETWORK_LOG_FILE}")
    print("This file contains all API calls that can be replicated with requests library")
