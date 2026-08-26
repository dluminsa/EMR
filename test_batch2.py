import ast
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path


class TimestampStub:
    def __init__(self, value):
        self.value = str(value)

    def tz_convert(self, _timezone):
        return self

    def date(self):
        from datetime import date

        return date.fromisoformat(self.value[:10])


class PandasStub:
    @staticmethod
    def to_datetime(value, **_kwargs):
        return TimestampStub(value)

    @staticmethod
    def isna(_value):
        return False

def load_form_helpers():
    """Load pure parsing helpers without executing the Streamlit page."""
    source_path = Path(__file__).with_name("batch2.py")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    wanted = {
        "EmrError",
        "VisitDateConflictError",
        "FormParser",
        "create_visit",
        "select_value_for_label",
        "art_medication_controls",
    }
    nodes = [
        node
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef))
        and node.name in wanted
    ]
    namespace = {"HTMLParser": HTMLParser, "pd": PandasStub, "re": re}
    module = ast.Module(body=nodes, type_ignores=[])
    exec(compile(module, str(source_path), "exec"), namespace)
    return namespace


HELPERS = load_form_helpers()
FormParser = HELPERS["FormParser"]
EmrError = HELPERS["EmrError"]
art_medication_controls = HELPERS["art_medication_controls"]
create_visit = HELPERS["create_visit"]
VisitDateConflictError = HELPERS["VisitDateConflictError"]


class ArtMedicationControlTests(unittest.TestCase):
    def parse(self, body):
        parser = FormParser()
        parser.feed(
            '<form action="/openmrs/htmlformentryui/htmlform/'
            f'enterHtmlForm/submit.action">{body}</form>'
        )
        return parser

    def test_resolves_lukaya_generated_fields_by_stable_container(self):
        parser = self.parse(
            '<span id="art-regimen"><select name="w463">'
            '<option value=""></option>'
            '<option value="164977">TDF-3TC-DTG</option>'
            '</select></span>'
            '<span id="no-of-art-pills"><input name="w467"></span>'
            '<span id="no-of-art-pills-days"><input name="w469"></span>'
        )

        self.assertEqual(
            art_medication_controls(parser, "TDF-3TC-DTG"),
            ("w463", "164977", "w467", "w469"),
        )

    def test_supports_legacy_generated_fields_without_container_ids(self):
        parser = self.parse(
            '<select name="w589">'
            '<option value="164977">TDF-3TC-DTG</option>'
            '</select>'
            '<input name="w593"><input name="w595">'
        )

        self.assertEqual(
            art_medication_controls(parser, "TDF-3TC-DTG"),
            ("w589", "164977", "w593", "w595"),
        )

    def test_rejects_form_when_quantity_fields_are_missing(self):
        parser = self.parse(
            '<span id="art-regimen"><select name="w463">'
            '<option value="164977">TDF-3TC-DTG</option>'
            '</select></span>'
        )

        with self.assertRaisesRegex(EmrError, "pill and day fields"):
            art_medication_controls(parser, "TDF-3TC-DTG")


class EmptyVisitRetryTests(unittest.TestCase):
    def setUp(self):
        self.original_request = HELPERS.get("request")

    def tearDown(self):
        if self.original_request is None:
            HELPERS.pop("request", None)
        else:
            HELPERS["request"] = self.original_request

    @staticmethod
    def response_with(visits):
        class Response:
            status_code = 200

            @staticmethod
            def json():
                return {"results": visits}

        return Response()

    def test_reuses_single_empty_visit_left_by_failed_submission(self):
        response = self.response_with(
            [
                {
                    "uuid": "empty-visit-uuid",
                    "startDatetime": "2026-08-21T00:00:00.000+0300",
                    "encounters": [],
                    "voided": False,
                }
            ]
        )
        HELPERS["request"] = lambda *args, **kwargs: response

        self.assertEqual(
            create_visit(
                None,
                "http://emr/openmrs",
                "patient-uuid",
                "59",
                "2026-08-21",
            ),
            ("empty-visit-uuid", ""),
        )

    def test_does_not_reuse_visit_that_already_has_an_encounter(self):
        response = self.response_with(
            [
                {
                    "uuid": "used-visit-uuid",
                    "startDatetime": "2026-08-21T00:00:00.000+0300",
                    "encounters": [{"uuid": "encounter-uuid"}],
                    "voided": False,
                }
            ]
        )
        HELPERS["request"] = lambda *args, **kwargs: response

        with self.assertRaises(VisitDateConflictError):
            create_visit(
                None,
                "http://emr/openmrs",
                "patient-uuid",
                "59",
                "2026-08-21",
            )


if __name__ == "__main__":
    unittest.main()
