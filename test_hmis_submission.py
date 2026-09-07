import ast
import json
import re
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

from hmis_submission import (
    check_submission, refill_controls, submission_problem, submission_url,
)


def response(payload=None, status=200, text=None):
    def read_json():
        if payload is None:
            raise ValueError("Not JSON")
        return payload
    return SimpleNamespace(
        status_code=status, json=read_json,
        text=json.dumps(payload) if text is None else text,
        url="http://facility/openmrs/htmlformentryui/htmlform/form.page",
    )


class SubmissionTests(unittest.TestCase):
    def test_excludes_only_controls_inside_cddp_category(self):
        html = '''<form><div id="cddp_category"><label>Category</label>
          <span><select name="w674"><option selected value="state">State</option>
          </select><input name="_w674" value="" /></span></div>
          <input name="w595" value="90"><input name="csrf" value="token">
          <div id="cddp_category"><select name="w900"></select></div></form>'''
        controls = [("w674", "state"), ("_w674", ""), ("w595", "90"),
                    ("csrf", "token"), ("w900", "state2")]
        self.assertEqual(refill_controls(html, controls),
                         [("w595", "90"), ("csrf", "token")])

    def test_handles_quote_wrappers_in_generated_attributes(self):
        html = r'<div id=\"cddp_category\"><select name=\"w674\"><option value="state">State</option></select></div><input name="w595">'
        self.assertEqual(refill_controls(html, [("w674", "state"), ("w595", "30")]),
                         [("w595", "30")])

    def test_uses_facility_endpoint_and_preserves_query(self):
        for action in (
            "/openmrs/htmlformentryui/htmlform/enterHtmlForm/submit.action",
            "openmrs/htmlformentryui/htmlform/enterHtmlForm/submit.action",
            "/openmrs/htmlformentryui/htmlform/openmrs/htmlformentryui/htmlform/enterHtmlForm/submit.action",
        ):
            with self.subTest(action=action):
                self.assertEqual(
                    submission_url("http://facility:8081/openmrs/", action + "?successUrl=%2Fdashboard"),
                    "http://facility:8081/openmrs/htmlformentryui/htmlform/enterHtmlForm/submit.action?successUrl=%2Fdashboard",
                )

    def test_requires_explicit_success(self):
        self.assertIsNone(submission_problem(response({"success": True, "encounterId": 123})))
        for payload in ({}, [], {"success": "true"}, {"success": False},
                        {"success": True, "errors": {"w3": "Invalid date"}}):
            with self.subTest(payload=payload):
                self.assertIsNotNone(submission_problem(response(payload)))
        self.assertIsNotNone(submission_problem(response(text="<html>Login</html>")))
        self.assertIsNotNone(submission_problem(response({"success": True}, status=500)))

    def test_returns_field_validation_error_even_with_http_200(self):
        self.assertEqual(
            submission_problem(response({"success": False, "errors": {"w3": "Invalid date"}})),
            "w3: Invalid date",
        )

    def test_saves_error_response_and_explains_state_conflict(self):
        failed = response(status=500, text="<pre>A patient can&#39;t be in multiple patient states for the same program work flow</pre>")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "error.html"
            with self.assertRaisesRegex(RuntimeError, "overlapping patient program states"):
                check_submission(failed, path, RuntimeError)
            self.assertEqual(path.read_text(), failed.text)

    def test_does_not_claim_error_was_saved_when_write_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing" / "error.html"
            with self.assertRaisesRegex(RuntimeError, "could not be saved"):
                check_submission(response({"success": False}), path, RuntimeError)


class BatchIntegrationTests(unittest.TestCase):
    def helpers(self, filename):
        # Load functions without running either Streamlit page or contacting EMR.
        source = Path(filename).read_text(encoding="utf-8")
        wanted = {"EmrError", "VisitDateConflictError", "create_visit", "FormParser", "first_provider", "set_control",
                  "select_value_for_label", "art_medication_controls",
                  "control_value", "submit_hmis_form"}
        nodes = [n for n in ast.parse(source).body
                 if isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name in wanted]
        namespace = dict(HTMLParser=HTMLParser, re=re, LOCATION_ID="5", FORM_UUID="form",
                         refill_controls=refill_controls, submission_url=submission_url,
                         check_submission=check_submission)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), filename, "exec"), namespace)
        return namespace

    def test_all_batches_submit_refill_without_program_transition(self):
        html = '''<form action="openmrs/htmlformentryui/htmlform/enterHtmlForm/submit.action">
          <input name="personId" value="1"><input name="visitId" value="22">
          <input name="htmlFormId" value="16"><input name="csrf" value="token">
          <select name="w9"><option value="">Provider</option><option value="21">Provider A</option></select>
          <span id="art-regimen"><select name="w463"><option value=""></option>
          <option value="164977">TDF-3TC-DTG</option></select></span>
          <span id="no-of-art-pills"><input name="w467"></span>
          <span id="no-of-art-pills-days"><input name="w469"></span>
          <div id="cddp_category"><select name="w674">
          <option selected value="existing-state">General CDDP</option></select></div>
          <input name="disabled_input" value="unwanted" disabled>
          </form>'''
        for filename in ("batch2.py", "batch3.py", "batch4.py"):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as directory:
                helpers = self.helpers(filename)
                helpers["LAST_SUBMISSION_ERROR_FILE"] = Path(directory) / "error.html"
                calls = []
                def request(session, method, url, **kwargs):
                    calls.append((method, url, kwargs))
                    if method == "GET":
                        return response(text=html)
                    return response({"success": True, "encounterId": 123})
                helpers["request"] = request
                helpers["submit_hmis_form"](
                    object(), "http://facility/openmrs", "patient-uuid", "1",
                    "visit-uuid", "", "2026-08-01", "2026-08-31", "30", "TDF-3TC-DTG",
                )
                method, url, kwargs = calls[-1]
                self.assertEqual(method, "POST")
                self.assertEqual(url, "http://facility/openmrs/htmlformentryui/htmlform/enterHtmlForm/submit.action")
                posted = {name: value for name, (_, value) in kwargs["files"]}
                self.assertNotIn("w674", posted)
                self.assertNotIn("disabled_input", posted)
                self.assertEqual(posted["visitId"], "22")
                self.assertEqual(posted["csrf"], "token")
                self.assertEqual(posted["w463"], "164977")
                self.assertEqual(posted["w467"], "30")
                self.assertEqual(posted["w469"], "30")
                self.assertEqual(posted["w16"], "164972")

    def test_all_batches_reuse_only_a_single_empty_visit(self):
        import pandas as pd
        for filename in ("batch2.py", "batch3.py", "batch4.py"):
            for encounters in ([], [{"uuid": "existing-encounter"}]):
                with self.subTest(filename=filename, encounters=encounters):
                    helpers = self.helpers(filename)
                    helpers["pd"] = pd
                    visit = {"uuid": "existing-visit", "visitId": 22,
                             "startDatetime": "2026-08-01T00:00:00+03:00",
                             "encounters": encounters}
                    calls = []
                    def request(session, method, url, **kwargs):
                        self.assertEqual(method, "GET")
                        self.assertTrue(url.endswith("/ws/rest/v1/visit"))
                        calls.append(url)
                        return response({"results": [visit]})
                    helpers["request"] = request
                    if encounters:
                        with self.assertRaises(helpers["VisitDateConflictError"]):
                            helpers["create_visit"](None, "http://facility/openmrs", "patient-uuid", "1", "2026-08-01")
                    else:
                        self.assertEqual(
                            helpers["create_visit"](None, "http://facility/openmrs", "patient-uuid", "1", "2026-08-01"),
                            ("existing-visit", "22"),
                        )
                    self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
