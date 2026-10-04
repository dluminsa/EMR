import pandas as pd
import unittest
from unittest.mock import patch

import batch4


def reference(*rows):
    return pd.DataFrame(rows, columns=["Art", "ARVS"])


def upload(*rows):
    return pd.DataFrame(rows, columns=["ART", "LD", "ARVD"])


class LoadCredentialsTests(unittest.TestCase):
    def test_facility_with_username_is_selectable_and_missing_username_is_dropped(self):
        rows = pd.DataFrame(
            {
                "DISTRICT": [" SEMBABULE ", "SEMBABULE"],
                "FACILITY": [" NTUUSI ", "OTHER"],
                "ip": ["192.0.2.1", None],
                "user": ["test-user", None],
                "password": [None, None],
            }
        )
        with patch.object(batch4.Path, "is_file", return_value=True), patch.object(
            batch4.pd, "read_csv", return_value=rows
        ):
            credentials = batch4.load_credentials()

        facilities = credentials.loc[
            credentials["DISTRICT"].eq("SEMBABULE"), "FACILITY"
        ].tolist()
        self.assertIn("NTUUSI", facilities)
        self.assertNotIn("OTHER", facilities)


class PrepareBatchTests(unittest.TestCase):
    def test_decimal_padding_does_not_change_uploaded_art_number(self):
        for art in (233, 233.0, "233", "233.0", " 00233.00 "):
            with self.subTest(art=art):
                ready, rejected = batch4.prepare_batch(
                    upload((art, "01/08/2026", 30)),
                    reference(("LKY 233", "TDF/3TC/DTG")),
                )
                self.assertTrue(rejected.empty)
                self.assertEqual(ready.loc[0, "ART"], "233")
                self.assertEqual(ready.loc[0, "Art"], "LKY 233")

    def test_decimal_padding_in_reference_matches_integer_upload(self):
        ready, rejected = batch4.prepare_batch(
            upload(("233", "01/08/2026", 30)),
            reference(("233.0", "TDF/3TC/DTG")),
        )
        self.assertTrue(rejected.empty)
        self.assertEqual(ready.loc[0, "ART"], "233")

    def test_fractional_art_is_rejected_instead_of_matching_another_patient(self):
        ready, rejected = batch4.prepare_batch(
            upload(("233.5", "01/08/2026", 30)),
            reference(("LKY 2335", "TDF/3TC/DTG")),
        )
        self.assertTrue(ready.empty)
        self.assertEqual(
            rejected.loc[0, "REASON_REJECTED"], "BLANK OR INVALID ART NUMBER"
        )

    def test_matches_on_digits_and_keeps_reference_identifier(self):
        ready, rejected = batch4.prepare_batch(
            upload(("002", "01/08/2026", "30")),
            reference(("LKY 002", "TDF/3TC/DTG")),
        )

        self.assertTrue(rejected.empty)
        self.assertEqual(ready.loc[0, "ART"], "2")
        self.assertEqual(ready.loc[0, "ARTX"], 2)
        self.assertEqual(ready.loc[0, "Art"], "LKY 002")
        self.assertEqual(ready.loc[0, "ARVS"], "TDF-3TC-DTG")
        self.assertEqual(
            ready.loc[0, "LD_PARSED"].date().isoformat(), "2026-08-01"
        )
        self.assertEqual(
            ready.loc[0, "RD_PARSED"].date().isoformat(), "2026-08-31"
        )
        self.assertEqual(ready.loc[0, "RD"], "31/08/2026")
        self.assertEqual(ready.loc[0, "ARVD_PARSED"], 30)

    def test_ignores_optional_uploaded_rd_and_recalculates_it(self):
        uploaded = upload(("002", "01/08/2026", 30))
        uploaded["RD"] = "01/01/2000"

        ready, rejected = batch4.prepare_batch(
            uploaded,
            reference(("LKY 002", "TDF/3TC/DTG")),
        )

        self.assertTrue(rejected.empty)
        self.assertEqual(ready.loc[0, "RD"], "31/08/2026")

    def test_rejects_all_uploaded_duplicate_rows(self):
        ready, rejected = batch4.prepare_batch(
            upload(
                ("LKY 002", "01/08/2026", 30),
                ("2", "02/08/2026", 30),
                ("LKY 003", "03/08/2026", 30),
            ),
            reference(
                ("LKY 002", "TDF/3TC/DTG"),
                ("LKY 003", "ABC/3TC/DTG"),
            ),
        )

        self.assertEqual(ready["Art"].tolist(), ["LKY 003"])
        duplicate_rows = rejected.loc[
            rejected["REASON_REJECTED"].eq(
                "DUPLICATED ART NUMBER IN UPLOADED CSV"
            )
        ]
        self.assertEqual(duplicate_rows["SOURCE_ROW"].tolist(), [2, 3])
    def test_combines_blank_unmatched_and_invalid_rejections(self):
        ready, rejected = batch4.prepare_batch(
            upload(
                ("no number", "01/08/2026", 30),
                ("999", "01/08/2026", 30),
                ("003", "not-a-date", 30),
                ("004", "01/08/2026", 30.5),
            ),
            reference(
                ("LKY 003", "TDF/3TC/DTG"),
                ("LKY 004", "ABC/3TC/DTG"),
            ),
        )

        self.assertTrue(ready.empty)
        self.assertEqual(rejected["SOURCE_ROW"].tolist(), [2, 3, 4, 5])
        self.assertEqual(
            rejected["REASON_REJECTED"].tolist(),
            [
                "BLANK OR INVALID ART NUMBER",
                "ART NUMBER NOT FOUND IN BATCH_REFERENCE",
                "BLANK OR INVALID LD",
                "BLANK OR INVALID ARVD",
            ],
        )
    def test_rejects_a_reference_duplicate(self):
        ready, rejected = batch4.prepare_batch(
            upload(("2", "01/08/2026", 30)),
            reference(
                ("LKY 002", "TDF/3TC/DTG"),
                ("LKY-2", "ABC/3TC/DTG"),
            ),
        )

        self.assertTrue(ready.empty)
        self.assertEqual(
            rejected.loc[0, "REASON_REJECTED"],
            "DUPLICATED ART NUMBER IN BATCH_REFERENCE",
        )
    def test_rejects_missing_required_upload_column(self):
        with self.assertRaisesRegex(ValueError, "ARVD"):
            batch4.prepare_batch(
                pd.DataFrame(
                    {"ART": [2], "LD": ["01/08/2026"]}
                ),
                reference(("LKY 002", "TDF/3TC/DTG")),
            )


if __name__ == "__main__":
    unittest.main()
