from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
import os

EXPORT_NAME = "MONTHLY"
BASE_URL = "http://10.3.1.5:8081/openmrs"
LOGIN_URL = f"{BASE_URL}/login.htm"
DATA_EXPORT_LIST_URL = f"{BASE_URL}/admin/reports/dataExport.list"


def get_export_row(page, export_name):
    export_link = page.get_by_role("link", name=export_name, exact=True)
    export_link.wait_for(state="visible", timeout=30000)
    row = export_link.locator("xpath=ancestor::tr[1]")
    row.wait_for(state="visible", timeout=30000)
    return row


def stop_loading(page):
    try:
        page.evaluate("window.stop()")
    except (PlaywrightError, PlaywrightTimeoutError):
        pass


def open_data_export_list(page):
    try:
        page.goto(DATA_EXPORT_LIST_URL, wait_until="domcontentloaded", timeout=30000)
    except PlaywrightTimeoutError:
        page.goto(DATA_EXPORT_LIST_URL, wait_until="commit", timeout=30000)
    page.locator("input[name='dataExportId']").first.wait_for(state="visible", timeout=30000)


with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    page = browser.new_page()
    
    # Create lshe download directory if it doesn't exist
    download_dir = r"C:\Users\Desire Luminsa\Desktop\PROJECT CENTCOM\EXTRACTS"
    os.makedirs(download_dir, exist_ok=True)
    
    #page.goto("http://192.168.1.232:8081/openmrs/login.htm") #LWEBITAKULI
    #page.goto("http://192.168.1.40:8081/openmrs/login.htm") #MIRAMBI
    page.goto(LOGIN_URL) #BUTENGA
    
    # Fill in username
    page.fill("#username", "admin")
    #page.fill("#username", "nbetty")
    
    # Fill in password
    page.fill("#password", "Admin123")
    #page.fill("#password", "User@123")
    
    # Select ART Clinic from dropdown
    page.select_option("#sessionLocationInput", "5")
    
    # Click login button
    page.click("#loginButton")
    
    # Wait a moment for login to process
    page.wait_for_timeout(2000)
    
    # Click on Legacy System Administration
    page.click("#ugandaemr-referenceapplication-legacyAdmin-homepageLink-ugandaemr-referenceapplication-legacyAdmin-homepageLink-extension")
    
    # Wait for page to load
    page.wait_for_timeout(2000)
    
    # Click on Manage Data Exports
    page.click("a[href*='/openmrs/admin/reports/dataExport.list']")
    
    # Wait for page to load
    page.wait_for_timeout(2000)
    
    # Select the data export by name instead of a hard-coded dataExportId.
    export_row = get_export_row(page, EXPORT_NAME)
    export_checkbox = export_row.locator("input[name='dataExportId']").first
    export_checkbox.click()
    
    # Wait for checkbox to be selected
    page.wait_for_timeout(1000)
    
    # Click the Generate Exports button. OpenMRS may not complete a clean
    # navigation signal here, so do not let Playwright wait on navigation.
    page.locator("input[type='submit'][value='Generate Exports']").click(no_wait_after=True)
    
    # Wait for exports to be generated
    page.wait_for_timeout(10000)
    stop_loading(page)
    open_data_export_list(page)
    page.wait_for_timeout(1000)
    
    # Listen for download and save it
    with page.expect_download() as download_info:
        # Click the Download link
        export_row = get_export_row(page, EXPORT_NAME)
        download_link = export_row.get_by_role("link", name="Download", exact=True)
        download_link.wait_for(state="visible", timeout=30000)
        download_link.click()
    
    download = download_info.value
    # Save the file to the specified directory
    download_path = os.path.join(download_dir, download.suggested_filename)
    download.save_as(download_path)
    print(f"Download saved to: {download_path}")
    
    # Wait a moment before closing
    page.wait_for_timeout(1000)
    
    browser.close()
