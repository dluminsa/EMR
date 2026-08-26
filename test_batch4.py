import pandas as pd
import unittest

import batch4


def reference(*rows):
    return pd.DataFrame(rows, columns=["Art", "ARVS"])


def upload(*rows):
    return pd.DataFrame(rows, columns=["ART", "LD", "ARVD"])


class PrepareBatchTests(unittest.TestCase):
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
