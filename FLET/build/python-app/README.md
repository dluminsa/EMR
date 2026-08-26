# UgandaEMR Update Tool

Native Flet application containing three workflows:

- `ONE_CLIENT` — update one client using the selected facility reference.
- `EREGISTERS` — validate and process a full E-register CSV.
- `EMR_EXTRACT` — process a compact CSV with `ART`, `LD`, `RD`, and `ARVD`.

The application connects directly to the selected facility's local UgandaEMR
server. An Android device must be connected to the same facility network as the
EMR server. A server address such as `127.0.0.1` refers to the phone itself on
Android and should not be used for a different computer.

## Run on Windows during development

```powershell
.\.venv\Scripts\flet.exe run main.py
```

## Build the Android APK

```powershell
.\.venv\Scripts\flet.exe build apk .
```

The APK is written to `build\apk`.

## Updating credentials or facility references

The app reads packaged JSON from `assets\data`. The CSV files in the project
root remain the editable source files, so original healthcare data is not
changed or deleted.

1. Replace or edit `CREDENTIALS.csv` and/or add facility CSVs to
   `BATCH_REFERENCE`.
2. Double-click `UPDATE_DATA.cmd`.
3. Review the conversion summary and warnings.
4. Rebuild the Windows app or Android APK so the new JSON is packaged.

Every selected facility reference must contain the exact headers `Art` and
`ARVS`. A file missing either header is still reported during conversion, and
the app rejects that facility with a warning when it is selected.

You can also run the converter from PowerShell:

```powershell
.\.venv\Scripts\python.exe convert_data.py
```
