import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import pandas as pd
import requests

import batch4
import main
from data_store import DataStoreError, convert_all, load_json_table
from processing import prepare_eregister


def eregister_row(**changes):
    row = {
        "MR - First name": "A",
        "MR - Surname": "Client",
        "MR - Sex": "F",
        "HIV/ART-Next Appointment date": "30/08/2026",
        "Last updated on": "01/08/2026",
        "ART: Art Number": "LKY 002",
        "HIV-ART Regimen - No. of days dispensed": 30,
        "Service Type": "ART",
    }
    row.update(changes)
    return row


class FletAppDataTests(unittest.TestCase):
    def setUp(self):
        self.reference = pd.DataFrame(
            {"Art": ["LKY 002", "LKY 003"], "ARVS": ["TDF/3TC/DTG", "ABC/3TC/DTG"]}
        )

    def test_packaged_credentials_load(self):
        credentials = main.load_credentials()
        self.assertFalse(credentials.empty)
        self.assertTrue({"DISTRICT", "FACILITY", "ip", "user", "password"}.issubset(credentials.columns))

    def test_packaged_app_data_is_json_only(self):
        self.assertTrue(main.CREDENTIALS_FILE.is_file())
        self.assertFalse(any(main.DATA_DIR.rglob("*.csv")))

    def test_selected_reference_requires_art_and_arvs(self):
        with self.assertRaisesRegex(main.ReferenceDataError, "Art and ARVS"):
            main.load_reference("MAKOOLE")

    def test_valid_eregister_is_prepared_for_emr(self):
        result = prepare_eregister(
            pd.DataFrame([eregister_row()]),
            self.reference,
        )

        self.assertFalse(result.blocked)
        self.assertEqual(len(result.ready), 1)
        self.assertEqual(result.ready.loc[0, "Art"], "LKY 002")
        self.assertEqual(result.ready.loc[0, "ARVS"], "TDF-3TC-DTG")
        self.assertEqual(result.ready.loc[0, "ARVD_PARSED"], 30)
        self.assertEqual(
            result.ready.loc[0, "RD_PARSED"].date().isoformat(), "2026-08-30"
        )

    def test_eregister_data_issue_blocks_the_whole_upload(self):
        uploaded = pd.DataFrame(
            [
                eregister_row(),
                eregister_row(
                    **{
                        "ART: Art Number": "missing",
                        "MR - First name": "B",
                    }
                ),
            ]
        )
        result = prepare_eregister(uploaded, self.reference)

        self.assertTrue(result.blocked)
        self.assertTrue(result.ready.empty)
        self.assertEqual(result.issue_counts["NO ART NUMBER"], 1)
        self.assertEqual(len(result.clean_data), 2)

    def test_eregister_derives_missing_days_from_dates(self):
        result = prepare_eregister(
            pd.DataFrame(
                [
                    eregister_row(
                        **{
                            "HIV/ART-Next Appointment date": "31/08/2026",
                            "HIV-ART Regimen - No. of days dispensed": None,
                        }
                    )
                ]
            ),
            self.reference,
        )

        self.assertFalse(result.blocked)
        self.assertEqual(result.ready.loc[0, "ARVD_PARSED"], 30)

    def test_eregister_iso_and_excel_dates_keep_calendar_date(self):
        for raw in ("2026-08-07", 46241):
            with self.subTest(raw=raw):
                result = prepare_eregister(pd.DataFrame([eregister_row(**{
                    "Last updated on": raw,
                    "HIV/ART-Next Appointment date": None,
                })]), self.reference)
                self.assertFalse(result.blocked)
                self.assertEqual(str(result.ready.loc[0, "LD_PARSED"].date()), "2026-08-07")
                self.assertEqual(str(result.ready.loc[0, "RD_PARSED"].date()), "2026-09-06")

    def test_invalid_appointment_is_not_replaced_by_derived_date(self):
        result = prepare_eregister(pd.DataFrame([eregister_row(**{
            "HIV/ART-Next Appointment date": "49:19.6",
        })]), self.reference)
        self.assertTrue(result.blocked)
        self.assertIn("INVALID NEXT APPOINTMENT DATE", result.issue_counts)

    def test_converter_preserves_identifiers_and_reports_bad_reference(self):
        with TemporaryDirectory() as source_name, TemporaryDirectory() as output_name:
            source = Path(source_name)
            output = Path(output_name)
            (source / "BATCH_REFERENCE").mkdir()
            (source / "CREDENTIALS.csv").write_text(
                "DISTRICT,FACILITY,ip,user,password\nD,F,10.0.0.1,u,p\n",
                encoding="utf-8",
            )
            (source / "BATCH_REFERENCE" / "F.csv").write_text(
                "Art,ARVS\n000123,TDF/3TC/DTG\n", encoding="utf-8"
            )
            (source / "BATCH_REFERENCE" / "BAD.csv").write_text(
                "Art\n000456\n", encoding="utf-8"
            )

            report = convert_all(source, output)
            _, records = load_json_table(
                output / "BATCH_REFERENCE" / "F.json", "facility_reference"
            )

            self.assertEqual(records[0]["Art"], "000123")
            self.assertEqual(len(report.warnings), 1)
            self.assertIn("BAD", report.warnings[0])

    def test_invalid_credentials_do_not_replace_existing_json(self):
        with TemporaryDirectory() as source_name, TemporaryDirectory() as output_name:
            source = Path(source_name)
            output = Path(output_name)
            (source / "BATCH_REFERENCE").mkdir()
            (source / "BATCH_REFERENCE" / "F.csv").write_text(
                "Art,ARVS\n1,TDF/3TC/DTG\n", encoding="utf-8"
            )
            (source / "CREDENTIALS.csv").write_text(
                "DISTRICT,FACILITY\nD,F\n", encoding="utf-8"
            )
            existing = output / "CREDENTIALS.json"
            existing.write_text("working copy", encoding="utf-8")

            with self.assertRaises(DataStoreError):
                convert_all(source, output)

            self.assertEqual(existing.read_text(encoding="utf-8"), "working copy")

    def test_credential_context_preserves_exact_username_and_password(self):
        credentials = pd.DataFrame(
            [
                {
                    "DISTRICT": "D",
                    "FACILITY": "F",
                    "ip": "10.0.0.1",
                    "user": " user ",
                    "password": " pass ",
                }
            ]
        )

        context = main.credential_context(credentials, "D", "F")

        self.assertEqual(context["username"], " user ")
        self.assertEqual(context["password"], " pass ")

    def test_ui_preserves_detailed_backend_diagnostic(self):
        error = batch4.InvalidCredentialsError(
            "[LOGIN_PAGE_RETURNED] Connected, but the login page was returned."
        )

        self.assertEqual(main.friendly_emr_error(error), str(error))

    def test_connection_refused_has_specific_diagnostic_code(self):
        session = Mock()
        session.request.side_effect = requests.ConnectionError(
            "Connection actively refused by the target machine"
        )

        with self.assertRaises(batch4.LoginUrlError) as caught:
            batch4.request(
                session,
                "GET",
                "http://10.0.0.1:8081/openmrs/login.htm",
                connection_error=batch4.LoginUrlError,
                operation="opening the login page",
            )

        message = str(caught.exception)
        self.assertIn("[NETWORK_CONNECTION_REFUSED]", message)
        self.assertIn("opening the login page", message)

    @staticmethod
    def response(status, url, text, reason="OK", history=None):
        result = Mock()
        result.status_code = status
        result.url = url
        result.text = text
        result.reason = reason
        result.history = history or []
        return result

    def test_login_reports_explicit_emr_message_and_http_context(self):
        login_url = "http://10.0.0.1:8081/openmrs/login.htm"
        page = self.response(200, login_url, '<input name="username">')
        rejected = self.response(
            200,
            login_url,
            '<div class="alert">Invalid username/password</div>',
        )
        session = Mock()
        session.request.side_effect = [page, rejected]

        with patch.object(batch4.requests, "Session", return_value=session):
            with self.assertRaises(batch4.InvalidCredentialsError) as caught:
                batch4.login("http://10.0.0.1:8081/openmrs", "user", "secret")

        message = str(caught.exception)
        self.assertIn("[LOGIN_REJECTED_BY_EMR]", message)
        self.assertIn("Invalid username/password", message)
        self.assertIn("HTTP 200", message)
        self.assertIn("Session location sent: 5", message)
        self.assertNotIn("secret", message)

    def test_login_page_without_reason_is_not_called_bad_password(self):
        login_url = "http://10.0.0.1:8081/openmrs/login.htm"
        page = self.response(200, login_url, '<input name="username">')
        returned = self.response(
            200,
            login_url,
            '<form><input name="username"><button id="loginButton"></button></form>',
        )
        session = Mock()
        session.request.side_effect = [page, returned]

        with patch.object(batch4.requests, "Session", return_value=session):
            with self.assertRaises(batch4.InvalidCredentialsError) as caught:
                batch4.login("http://10.0.0.1:8081/openmrs", "user", "password")

        message = str(caught.exception)
        self.assertIn("[LOGIN_PAGE_RETURNED]", message)
        self.assertIn("without a recognized explanation", message)
        self.assertNotIn("username or password is invalid", message.lower())


if __name__ == "__main__":
    unittest.main()
