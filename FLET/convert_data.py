"""Convert CREDENTIALS.csv and BATCH_REFERENCE CSVs into app JSON assets."""

from pathlib import Path

from data_store import DataStoreError, convert_all


APP_DIR = Path(__file__).resolve().parent


def main() -> int:
    try:
        report = convert_all(APP_DIR, APP_DIR / "assets" / "data")
    except DataStoreError as exc:
        print(f"CONVERSION FAILED: {exc}")
        return 1

    print(f"Credentials: {report.credentials_rows} rows converted")
    print(
        f"References: {report.reference_files} files and "
        f"{report.reference_rows} rows converted"
    )
    if report.warnings:
        print("\nWARNINGS:")
        for warning in report.warnings:
            print(f"- {warning}")
    else:
        print("All facility reference files contain Art and ARVS.")
    print("\nJSON assets are ready. Rebuild the app to include the changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
