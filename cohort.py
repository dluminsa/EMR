from playwright.sync_api import sync_playwright


with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    page = browser.new_page()

    page.goto("http://192.168.1.227:8081/openmrs/login.htm")

    page.fill("#username", "Rehemah")
    page.fill("#password", "Rehemah301996")
    page.select_option("#sessionLocationInput", "5")
    page.click("#loginButton")

    page.wait_for_timeout(2000)

    input("Logged in. Press Enter to close the browser...")

    browser.close()
