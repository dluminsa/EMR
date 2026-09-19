import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pandas as pd

import batch5
import test_batch4


class ValidationTests(test_batch4.PrepareBatchTests):
    """Run the existing CSV validation cases against the backup implementation."""

    def setUp(self):
        patcher = patch.object(test_batch4, "batch4", batch5)
        patcher.start()
        self.addCleanup(patcher.stop)


class BrowserTests(unittest.TestCase):
    def test_fresh_browser_and_cleanup_for_success_and_failure(self):
        with ExitStack() as stack:
            runtime = stack.enter_context(patch.object(batch5, "sync_playwright"))
            browsers = [MagicMock(), MagicMock(), MagicMock()]
            runtime.return_value.__enter__.return_value.chromium.launch.side_effect = browsers
            for name in (
                "open_login_page", "login_and_validate_access", "open_exact_art_dashboard",
                "open_start_date_picker", "select_calendar_date", "confirm_past_visit",
                "open_hmis_date_picker", "select_hmis_return_date",
                "select_first_hmis_provider", "open_hmis_medication_tab",
                "select_hmis_regimen", "fill_hmis_dispensing_and_save",
            ):
                stack.enter_context(patch.object(batch5, name, return_value=True))
            confirm = stack.enter_context(patch.object(
                batch5, "require_saved", side_effect=[None, RuntimeError("rejected"), None]
            ))
            for index in range(3):
                args = ("http://facility/openmrs", "user", "password", str(index),
                        "01/08/2026", "31/08/2026", 30, "TDF-3TC-DTG")
                if index == 1:
                    with self.assertRaisesRegex(RuntimeError, "rejected"):
                        batch5.update_client(*args)
                else:
                    batch5.update_client(*args)
                browsers[index].close.assert_called_once()
            self.assertEqual(confirm.call_count, 3)
            self.assertEqual(runtime.call_count, 3)

    def test_does_not_count_failed_or_unacknowledged_saves(self):
        response = MagicMock(status=200)
        response.json.return_value = {"success": True}
        batch5.require_saved(response)
        for status, payload in (
            (500, {"success": True}), (200, {"success": False}),
            (200, {}), (200, {"success": True, "errors": {"date": "invalid"}}),
        ):
            response.status = status
            response.json.return_value = payload
            with self.subTest(status=status, payload=payload), self.assertRaises(RuntimeError):
                batch5.require_saved(response)
        response.json.side_effect = ValueError("HTML")
        with self.assertRaises(RuntimeError):
            batch5.require_saved(response)

    def test_failed_row_does_not_stop_remaining_rows(self):
        ready, rejected = batch5.prepare_batch(
            pd.DataFrame({"ART": [1, 2, 3], "LD": ["01/08/2026"] * 3, "ARVD": [30] * 3}),
            pd.DataFrame({"Art": ["ART 001", "ART 002", "ART 003"],
                          "ARVS": ["TDF/3TC/DTG"] * 3}),
        )
        with patch.object(batch5, "st"), patch.object(
            batch5, "update_client", side_effect=[None, RuntimeError("failure"), None]
        ) as update:
            count, failed = batch5.run_updates(ready, rejected, "url", "user", "password")
        self.assertEqual(count, 2)
        self.assertEqual(update.call_count, 3)
        self.assertEqual(failed["Art"].tolist(), ["ART 002"])
        self.assertIn("failure", failed.iloc[0]["REASON_REJECTED"])
        self.assertEqual(update.call_args_list[0].args[4:7], ("01/08/2026", "31/08/2026", 30))

    def test_upload_review_does_not_launch_browser_before_button(self):
        uploaded = pd.DataFrame({"ART": [1, 999], "LD": ["01/08/2026"] * 2, "ARVD": [30] * 2})
        reference = pd.DataFrame({"Art": ["ART 001"], "ARVS": ["TDF/3TC/DTG"]})
        credentials = pd.DataFrame([dict(DISTRICT="District", FACILITY="Facility",
                                         ip="facility", user="user", password="password")])
        with patch.object(batch5, "st") as ui, patch.object(
            batch5, "load_credentials", return_value=credentials
        ), patch.object(batch5.Path, "is_file", return_value=True), patch.object(
            batch5.pd, "read_csv", side_effect=[reference, uploaded]
        ), patch.object(batch5, "run_updates") as run:
            ui.radio.side_effect = ["District", "Facility"]
            ui.file_uploader.return_value.getvalue.return_value = b"csv"
            ui.button.return_value = False
            batch5.main()
            run.assert_not_called()
            ui.title.assert_called_once_with("BACKUP")
            shown_rejected = ui.dataframe.call_args_list[0].args[0]
            self.assertEqual(len(shown_rejected), 1)
            self.assertIn("REASON_REJECTED", shown_rejected.columns)
            self.assertEqual(ui.button.call_args.args[0], "BATCH UPLOAD")


if __name__ == "__main__":
    unittest.main()
