"""Shared safeguards for requests-based HMIS refill submissions.

UgandaEMR 3.4.1's HMIS clinical assessment initializes cddp_category as
disabled. Its workflowState widget otherwise defaults to the current state
and submitting it can create a retrospective program transition. Refill
updates do not edit CDDP categories, so omit those controls as the browser does.

Reference: https://github.com/METS-Programme/openmrs-module-ugandaemr/blob/3.4.1/omod/src/main/webapp/resources/htmlforms/HMIS-HIV-003-HivCareArtCard-ClinicalAssessmentPage.xml
The workflowState submission handler skips a missing/blank state UUID.
"""

import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit


class _CddpControls(HTMLParser):
    VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.names = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        # Match FormParser's handling of quote wrappers in generated markup.
        for key in ("id", "name"):
            if attrs.get(key) is not None:
                attrs[key] = attrs[key].strip().lstrip("\\\"'").rstrip("\\\"'/").strip()
        inside = any(active for _, active in self.stack)
        inside = inside or attrs.get("id") == "cddp_category"
        if inside and tag in {"input", "select", "textarea"}:
            if attrs.get("name"):
                self.names.add(attrs["name"])
        if tag not in self.VOID_TAGS:
            self.stack.append((tag, inside))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


def refill_controls(html, controls):
    """Preserve the form's controls except the untouched CDDP workflow widget."""
    parser = _CddpControls()
    parser.feed(html)
    return [(name, value) for name, value in controls if name not in parser.names]


def submission_url(base_url, action):
    """Use the facility's known endpoint, preserving the form's query string."""
    base = urlsplit(base_url)
    return urlunsplit((
        base.scheme, base.netloc,
        base.path.rstrip("/") + "/htmlformentryui/htmlform/enterHtmlForm/submit.action",
        urlsplit(action).query, "",
    ))


def submission_problem(response):
    """Require the HTML Form Entry JSON acknowledgement, not just HTTP 200."""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if response.status_code < 400 and payload.get("success") is True and not errors:
            return None
        if errors:
            if isinstance(errors, dict):
                return "; ".join(f"{key}: {value}" for key, value in errors.items())
            return str(errors)
        if payload.get("success") is False:
            return str(payload.get("message") or "OpenMRS reported success=false.")

    text = unescape(re.sub(r"<[^>]+>", " ", response.text))
    if "multiple patient states for the same program work flow" in text:
        return (
            "OpenMRS found overlapping patient program states. The untouched "
            "CDDP category was excluded from this refill; if the conflict persists, "
            "the patient's program history needs review in OpenMRS."
        )
    if "Cannot find fragment controller" in text:
        return "OpenMRS could not resolve the HMIS submission endpoint."
    if response.status_code >= 400:
        return f"OpenMRS returned HTTP {response.status_code}."
    return (
        "OpenMRS did not return an explicit successful submission acknowledgement. "
        "Check the patient's encounter before retrying."
    )


def check_submission(response, error_path, error_type):
    problem = submission_problem(response)
    if problem is None:
        return
    try:
        error_path.write_text(response.text, encoding="utf-8", errors="replace")
        saved = f" Response saved to {error_path}."
    except OSError:
        saved = " The response could not be saved to disk."
    raise error_type(problem + saved)
