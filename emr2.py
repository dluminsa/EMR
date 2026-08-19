"""
EMR Data Extract downloader using requests library
Based on network analysis from emrcopy.py
"""

import requests
import json
import time
import re
from html.parser import HTMLParser
from pathlib import Path


# Configuration
EXPORT_NAME = "MONTHLY"
BASE_URL = "http://10.3.1.5:8081/openmrs"
LOGIN_URL = f"{BASE_URL}/login.htm"
ADMIN_URL = f"{BASE_URL}/admin/index.htm"
DATA_EXPORT_LIST_URL = f"{BASE_URL}/admin/reports/dataExport.list"
DATA_EXPORT_SERVLET_URL = f"{BASE_URL}/moduleServlet/reportingcompatibility/dataExportServlet"
GENERATE_TIMEOUT_SECONDS = 300
DOWNLOAD_TIMEOUT_SECONDS = 300
GENERATE_WAIT_SECONDS = 30

# Credentials
USERNAME = "admin"
PASSWORD = "Admin123"
LOCATION_ID = "5"  # ART Clinic

# Output
FINAL_OUTPUT_DIR = Path(r"C:\Users\Desire Luminsa\Desktop\PROJECT CENTCOM\EXTRACTS")
NETWORK_LOG_FILE = Path(r"C:\Users\Desire Luminsa\Desktop\PROJECT CENTCOM\emr2_network_log.jsonl")

# Session for maintaining cookies
session = requests.Session()


class DataExportListParser(HTMLParser):
    """Extract data export rows from the legacy OpenMRS table."""

    def __init__(self):
        super().__init__()
        self.rows = []
        self.current_row = None
        self.current_link = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)

        if tag == "tr":
            self.current_row = {
                "checkbox_values": [],
                "links": [],
            }
            return

        if self.current_row is None:
            return

        if tag == "input" and attrs.get("name") == "dataExportId":
            value = attrs.get("value")
            if value:
                self.current_row["checkbox_values"].append(value)
            return

        if tag == "a":
            self.current_link = {
                "href": attrs.get("href", ""),
                "text": "",
            }

    def handle_data(self, data):
        if self.current_link is not None:
            self.current_link["text"] += data

    def handle_endtag(self, tag):
        if tag == "a" and self.current_link is not None and self.current_row is not None:
            self.current_link["text"] = self.current_link["text"].strip()
            self.current_row["links"].append(self.current_link)
            self.current_link = None
            return

        if tag == "tr" and self.current_row is not None:
            self.rows.append(self.current_row)
            self.current_row = None


def log_network_event(event: dict) -> None:
    """Log network events to JSONL file"""
    NETWORK_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with NETWORK_LOG_FILE.open("a", encoding="utf-8") as log_file:
        log_file.write(json.dumps(event, ensure_ascii=True) + "\n")


def log_request(method: str, url: str, data: dict = None, headers: dict = None) -> None:
    """Log HTTP request"""
    event = {
        "event": "request",
        "method": method,
        "url": url,
        "data": str(data)[:200] if data else None,
        "headers": {k: v for k, v in (headers or {}).items() if k.lower() not in ["authorization", "cookie"]},
    }
    print(f"\n[REQUEST] {method} {url}")
    if data:
        print(f"Data: {data}")
    log_network_event(event)


def log_response(status_code: int, url: str, content_type: str = None) -> None:
    """Log HTTP response"""
    event = {
        "event": "response",
        "status": status_code,
        "url": url,
        "content_type": content_type,
    }
    print(f"[RESPONSE] {status_code} {url}")
    if content_type:
        print(f"Content-Type: {content_type}")
    log_network_event(event)


def get_csrf_token() -> str:
    """Extract CSRF token from login page"""
    print("\n======== GETTING CSRF TOKEN ========")
    try:
        response = session.get(LOGIN_URL, timeout=30)
        log_response(response.status_code, LOGIN_URL, response.headers.get("content-type"))
        
        # Extract CSRF token from HTML (if present)
        if "XSRF-TOKEN" in response.text or "_csrf" in response.text:
            print("✓ Found CSRF token in response")
            # Try to extract from meta tags or hidden inputs
            import re
            csrf_match = re.search(r'name=["\']_csrf["\'].*?value=["\']([^"\']+)["\']', response.text)
            if csrf_match:
                return csrf_match.group(1)
        
        return None
    except Exception as e:
        print(f"❌ Error getting CSRF token: {e}")
        return None


def login(username: str, password: str, location_id: str) -> bool:
    """Login to OpenMRS"""
    print("\n======== LOGGING IN ========")
    try:
        # First, get any CSRF tokens
        csrf_token = get_csrf_token()
        
        # Prepare login data
        login_data = {
            "username": username,
            "password": password,
            "sessionLocation": location_id,
            "redirectUrl": "",
        }
        
        # Add CSRF token if found
        if csrf_token:
            login_data["_csrf"] = csrf_token
        
        log_request("POST", LOGIN_URL, login_data)
        
        # Send login request
        response = session.post(LOGIN_URL, data=login_data, timeout=30, allow_redirects=True)
        log_response(response.status_code, response.url, response.headers.get("content-type"))
        
        # Check if login was successful (should redirect to home page or stay on login)
        if response.status_code == 200:
            if "login" not in response.url.lower() or "home" in response.url.lower():
                print("✓ Login successful")
                return True
            elif "sessionLocation" in response.text or username in response.text:
                # Sometimes the form is re-displayed on error
                print("❌ Login failed - credentials may be incorrect")
                return False
        
        print("✓ Login request completed")
        return True
        
    except Exception as e:
        print(f"❌ Login error: {e}")
        return False


def navigate_to_admin() -> bool:
    """Navigate to admin section"""
    print("\n======== NAVIGATING TO ADMIN ========")
    try:
        log_request("GET", ADMIN_URL)
        response = session.get(ADMIN_URL, timeout=30)
        log_response(response.status_code, response.url, response.headers.get("content-type"))
        
        if response.status_code == 200:
            print("✓ Admin page loaded")
            return True
        
        return False
        
    except Exception as e:
        print(f"❌ Error navigating to admin: {e}")
        return False


def get_data_export_list() -> str | None:
    """Get data export list page"""
    print("\n======== GETTING DATA EXPORT LIST ========")
    try:
        log_request("GET", DATA_EXPORT_LIST_URL)
        response = session.get(DATA_EXPORT_LIST_URL, timeout=30)
        log_response(response.status_code, response.url, response.headers.get("content-type"))
        
        if response.status_code == 200:
            print("✓ Data export list page loaded")
            return response.text
        
        return None
        
    except Exception as e:
        print(f"❌ Error getting data export list: {e}")
        return None


def find_export_id(export_name: str, data_export_html: str) -> str:
    """Find the checkbox value for an export by its exact linked name."""
    parser = DataExportListParser()
    parser.feed(data_export_html)

    for row in parser.rows:
        for link in row["links"]:
            if link["text"] != export_name:
                continue

            if row["checkbox_values"]:
                export_id = row["checkbox_values"][0]
                print(f"Found {export_name} dataExportId={export_id}")
                return export_id

            match = re.search(r"dataExportId=(\d+)", link["href"])
            if match:
                export_id = match.group(1)
                print(f"Found {export_name} dataExportId={export_id}")
                return export_id

    raise ValueError(f"Could not find export named {export_name!r} on data export list")


def generate_export(export_id: str) -> bool:
    """Generate data export"""
    print("\n======== GENERATING EXPORT ========")
    try:
        # This is the critical API call discovered from network log
        generate_data = {
            "dataExportId": export_id,
            "action": "Generate Exports",
        }
        
        log_request("POST", DATA_EXPORT_LIST_URL, generate_data)
        response = session.post(
            DATA_EXPORT_LIST_URL,
            data=generate_data,
            timeout=GENERATE_TIMEOUT_SECONDS,
            allow_redirects=True,
        )
        log_response(response.status_code, response.url, response.headers.get("content-type"))
        
        if response.status_code == 200 or response.status_code == 302:
            print("✓ Export generation request sent")
            # Wait for generation to complete
            time.sleep(GENERATE_WAIT_SECONDS)
            return True
        
        return False
        
    except Exception as e:
        print(f"❌ Error generating export: {e}")
        return False


def download_export(export_id: str) -> bool:
    """Download the generated export"""
    print("\n======== DOWNLOADING EXPORT ========")
    try:
        # This is the servlet endpoint discovered from network log
        download_url = f"{DATA_EXPORT_SERVLET_URL}?dataExportId={export_id}"
        
        log_request("GET", download_url)
        response = session.get(download_url, timeout=DOWNLOAD_TIMEOUT_SECONDS)
        log_response(response.status_code, response.url, response.headers.get("content-type"))
        
        if response.status_code == 200:
            # Determine filename from Content-Disposition header or use default
            content_disposition = response.headers.get("content-disposition", "")
            if "filename=" in content_disposition:
                filename = content_disposition.split("filename=")[-1].strip('"\'')
            else:
                filename = f"{EXPORT_NAME.replace(' ', '_')}_{int(time.time())}.xls"
            
            # Save the file
            FINAL_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            save_path = FINAL_OUTPUT_DIR / filename
            
            with open(save_path, "wb") as f:
                f.write(response.content)
            
            file_size = len(response.content)
            print(f"✓ Download saved to: {save_path}")
            print(f"✓ File size: {file_size} bytes")
            
            # Log download
            event = {
                "event": "download",
                "url": download_url,
                "filename": filename,
                "size": file_size,
            }
            log_network_event(event)
            
            return True
        
        print(f"❌ Download failed with status {response.status_code}")
        return False
        
    except Exception as e:
        print(f"❌ Error downloading export: {e}")
        return False


def main():
    """Main execution"""
    print("\n" + "="*60)
    print("EMR DATA EXTRACT DOWNLOADER (using requests library)")
    print("="*60)
    
    try:
        # Step 1: Login
        if not login(USERNAME, PASSWORD, LOCATION_ID):
            print("❌ Login failed")
            return False
        
        # Small delay between requests
        time.sleep(1)
        
        # Step 2: Navigate to admin
        if not navigate_to_admin():
            print("❌ Failed to navigate to admin")
            return False
        time.sleep(1)
        
        # Step 3: Get data export list and find the requested export by name
        data_export_html = get_data_export_list()
        if not data_export_html:
            print("❌ Failed to get data export list")
            return False
        
        export_id = find_export_id(EXPORT_NAME, data_export_html)
        
        time.sleep(1)
        
        # Step 4: Generate export
        if not generate_export(export_id):
            print("❌ Failed to generate export")
            return False
        # Step 5: Download export
        if not download_export(export_id):
            print("❌ Failed to download export")
            return False
        
        print("\n" + "="*60)
        print("✅ EMR EXTRACT DOWNLOAD COMPLETED SUCCESSFULLY")
        print("="*60)
        print(f"Network log saved to: {NETWORK_LOG_FILE}")
        
        return True
        
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)
