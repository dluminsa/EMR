"""JSON data loading and CSV-to-JSON conversion for the Flet app."""

from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path


SCHEMA_VERSION = 1
CREDENTIAL_COLUMNS = {"DISTRICT", "FACILITY", "ip", "user", "password"}
REFERENCE_COLUMNS = {"Art", "ARVS"}


class DataStoreError(ValueError):
    """Raised when source or packaged data is invalid."""


@dataclass
class ConversionReport:
    credentials_rows: int = 0
    reference_files: int = 0
    reference_rows: int = 0
    warnings: list[str] = field(default_factory=list)


def _read_csv_table(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise DataStoreError(f"Could not read {path}: {exc}") from exc

    text = None
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise DataStoreError(f"{path.name} is not a supported text CSV file.")

    rows = list(csv.reader(StringIO(text, newline="")))
    if not rows:
        raise DataStoreError(f"{path.name} is empty.")

    columns = rows[0]
    if not columns or any(not column for column in columns):
        raise DataStoreError(f"{path.name} contains a blank column name.")
    duplicates = sorted({column for column in columns if columns.count(column) > 1})
    if duplicates:
        raise DataStoreError(
            f"{path.name} contains duplicate column names: {', '.join(duplicates)}."
        )

    records: list[dict[str, str]] = []
    for row_number, row in enumerate(rows[1:], start=2):
        if not row or all(value == "" for value in row):
            continue
        if len(row) != len(columns):
            raise DataStoreError(
                f"{path.name} row {row_number} has {len(row)} values; "
                f"expected {len(columns)}."
            )
        records.append(dict(zip(columns, row)))
    return columns, records


def _payload(path: Path, kind: str) -> dict:
    columns, records = _read_csv_table(path)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "source_file": path.name,
        "columns": columns,
        "records": records,
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_json_table(path: Path, expected_kind: str) -> tuple[list[str], list[dict]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DataStoreError(f"Missing packaged data file: {path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DataStoreError(f"Could not read {path.name}: {exc}") from exc

    if not isinstance(payload, dict):
        raise DataStoreError(f"{path.name} must contain a JSON object.")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise DataStoreError(f"{path.name} uses an unsupported schema version.")
    if payload.get("kind") != expected_kind:
        raise DataStoreError(f"{path.name} is not {expected_kind} data.")

    columns = payload.get("columns")
    records = payload.get("records")
    if not isinstance(columns, list) or not all(isinstance(item, str) for item in columns):
        raise DataStoreError(f"{path.name} has invalid column metadata.")
    if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
        raise DataStoreError(f"{path.name} has invalid records.")
    if any(set(record) != set(columns) for record in records):
        raise DataStoreError(f"{path.name} contains records that do not match its columns.")
    return columns, records


def convert_all(source_root: Path, output_data_dir: Path) -> ConversionReport:
    """Convert editable source CSVs into packaged JSON without changing the CSVs."""

    source_root = Path(source_root)
    output_data_dir = Path(output_data_dir)
    credentials_csv = source_root / "CREDENTIALS.csv"
    reference_source = source_root / "BATCH_REFERENCE"
    reference_output = output_data_dir / "BATCH_REFERENCE"

    credentials_payload = _payload(credentials_csv, "credentials")
    missing_credentials = CREDENTIAL_COLUMNS - set(credentials_payload["columns"])
    if missing_credentials:
        raise DataStoreError(
            "CREDENTIALS.csv is missing: " + ", ".join(sorted(missing_credentials))
        )
    if not reference_source.is_dir():
        raise DataStoreError(f"Missing reference folder: {reference_source}")

    report = ConversionReport(credentials_rows=len(credentials_payload["records"]))
    reference_payloads: list[tuple[Path, dict]] = []
    for source_path in sorted(reference_source.glob("*.csv")):
        payload = _payload(source_path, "facility_reference")
        missing = REFERENCE_COLUMNS - set(payload["columns"])
        if missing:
            report.warnings.append(
                f"{source_path.stem}: missing {', '.join(sorted(missing))}; "
                "the app will reject this facility when selected."
            )
        reference_payloads.append((source_path, payload))
        report.reference_files += 1
        report.reference_rows += len(payload["records"])

    if not reference_payloads:
        raise DataStoreError("BATCH_REFERENCE contains no CSV files.")

    _write_json(output_data_dir / "CREDENTIALS.json", credentials_payload)
    for source_path, payload in reference_payloads:
        _write_json(reference_output / f"{source_path.stem}.json", payload)

    expected_names = {f"{source_path.stem}.json" for source_path, _ in reference_payloads}
    if reference_output.is_dir():
        for old_json in reference_output.glob("*.json"):
            if old_json.name not in expected_names:
                old_json.unlink()

    return report
