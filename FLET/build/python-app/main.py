"""Native Flet application for the three UgandaEMR update workflows."""

from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path

import flet as ft
import pandas as pd

import batch4 as backend
from data_store import CREDENTIAL_COLUMNS, REFERENCE_COLUMNS, DataStoreError, load_json_table
from processing import PreparedBatch, frame_to_csv_bytes, prepare_eregister


APP_DIR = Path(__file__).resolve().parent
ASSETS_DIR = Path(os.environ.get("FLET_ASSETS_DIR", APP_DIR / "assets")).resolve()
DATA_DIR = ASSETS_DIR / "data"
CREDENTIALS_FILE = DATA_DIR / "CREDENTIALS.json"
REFERENCE_DIR = DATA_DIR / "BATCH_REFERENCE"

BLUE = ft.Colors.BLUE_700
BLUE_DARK = ft.Colors.BLUE_900
SURFACE = ft.Colors.BLUE_50
SUCCESS = ft.Colors.GREEN_700
WARNING = ft.Colors.ORANGE_800
ERROR = ft.Colors.RED_700


class ReferenceDataError(ValueError):
    pass


def options(values):
    return [ft.DropdownOption(key=str(value), text=str(value)) for value in values]


def load_credentials():
    try:
        columns, records = load_json_table(CREDENTIALS_FILE, "credentials")
    except DataStoreError as exc:
        raise ValueError(str(exc)) from exc
    missing = CREDENTIAL_COLUMNS - set(columns)
    if missing:
        raise ValueError(
            "CREDENTIALS.json is missing: " + ", ".join(sorted(missing))
        )
    credentials = pd.DataFrame(records, columns=columns)
    credentials = credentials.loc[
        credentials["user"].astype("string").str.strip().fillna("").ne("")
    ].copy()
    for column in ("DISTRICT", "FACILITY"):
        credentials[column] = credentials[column].astype("string").str.strip()
    return credentials


def load_reference(facility):
    path = REFERENCE_DIR / f"{facility}.json"
    if not path.is_file():
        raise ReferenceDataError(
            f"{facility} was rejected: no reference JSON was found."
        )
    try:
        columns, records = load_json_table(path, "facility_reference")
    except DataStoreError as exc:
        raise ReferenceDataError(
            f"{facility} was rejected: its reference JSON could not be read. {exc}"
        ) from exc
    missing = REFERENCE_COLUMNS - set(columns)
    if missing:
        raise ReferenceDataError(
            f"{facility} was rejected: its reference data must contain both exact "
            f"column names Art and ARVS. Missing: {', '.join(sorted(missing))}."
        )
    reference = pd.DataFrame(records, columns=columns)
    reference["Art"] = reference["Art"].astype("string")
    return reference


def credential_context(credentials, district, facility):
    rows = credentials.loc[
        credentials["DISTRICT"].eq(district)
        & credentials["FACILITY"].eq(facility)
    ]
    if rows.empty:
        raise ValueError("No credentials matched the selected facility.")
    row = rows.iloc[0]
    if any(
        pd.isna(row.get(name)) or not str(row.get(name)).strip()
        for name in ("ip", "user", "password")
    ):
        raise ValueError("The selected facility has incomplete server credentials.")
    return {
        "base_url": f"http://{str(row['ip']).strip()}:8081/openmrs",
        # Credentials are exact data. Do not silently alter valid leading or
        # trailing characters while preparing the login request.
        "username": str(row["user"]),
        "password": str(row["password"]),
    }


def friendly_emr_error(exc):
    if isinstance(exc, backend.VisitDateConflictError):
        return "The last encounter date conflicts with an existing visit."
    if isinstance(exc, backend.EmrError):
        message = str(exc).strip()
        return message or "UgandaEMR returned an error without diagnostic details."
    return "An unexpected error occurred while updating the client."


class StatusCard(ft.Container):
    def __init__(self):
        self.message = ft.Text("Select a district and facility to begin.")
        super().__init__(
            content=self.message,
            bgcolor=ft.Colors.GREY_100,
            border_radius=10,
            padding=12,
        )

    def set(self, message, kind="info"):
        colors = {
            "info": (ft.Colors.BLUE_50, BLUE_DARK),
            "success": (ft.Colors.GREEN_50, SUCCESS),
            "warning": (ft.Colors.ORANGE_50, WARNING),
            "error": (ft.Colors.RED_50, ERROR),
        }
        self.bgcolor, self.message.color = colors[kind]
        self.message.value = message


class FacilitySelector(ft.Column):
    def __init__(self, credentials, on_facility_change):
        self.credentials = credentials
        self.on_facility_change = on_facility_change
        districts = credentials["DISTRICT"].dropna().unique().tolist()
        self.district = ft.Dropdown(
            label="DISTRICT",
            options=options(districts),
            on_select=self._district_selected,
            expand=True,
        )
        self.facility = ft.Dropdown(
            label="FACILITY",
            options=[],
            disabled=True,
            on_select=self._facility_selected,
            expand=True,
        )
        super().__init__(
            controls=[
                ft.ResponsiveRow(
                    controls=[
                        ft.Container(self.district, col={"xs": 12, "md": 6}),
                        ft.Container(self.facility, col={"xs": 12, "md": 6}),
                    ]
                )
            ]
        )

    def _district_selected(self, _):
        district_rows = self.credentials.loc[
            self.credentials["DISTRICT"].eq(self.district.value)
        ]
        facilities = district_rows["FACILITY"].dropna().unique().tolist()
        self.facility.options = options(facilities)
        self.facility.value = None
        self.facility.disabled = False
        self.on_facility_change(None)
        self.page.update()

    def _facility_selected(self, _):
        self.on_facility_change(self.facility.value)
        self.page.update()

    def context(self):
        if not self.district.value or not self.facility.value:
            raise ValueError("Select both a district and facility.")
        return credential_context(
            self.credentials, self.district.value, self.facility.value
        )


class DateInput(ft.Row):
    def __init__(self, label, *, last_date=None, on_change=None):
        self.selected_date = None
        self.changed = on_change
        self.field = ft.TextField(
            label=label,
            read_only=True,
            expand=True,
            hint_text="DD/MM/YYYY",
        )
        self.picker = ft.DatePicker(
            first_date=date(2000, 1, 1),
            last_date=last_date or date(2100, 12, 31),
            on_change=self._picked,
        )
        super().__init__(
            controls=[
                self.field,
                ft.IconButton(
                    icon=ft.Icons.CALENDAR_MONTH,
                    tooltip=f"Choose {label}",
                    on_click=self._open,
                ),
            ],
            col={"xs": 12, "md": 4},
        )

    def _open(self, _):
        self.page.show_dialog(self.picker)

    def _picked(self, _):
        value = self.picker.value
        if isinstance(value, datetime):
            value = value.date()
        self.selected_date = value
        self.field.value = value.strftime("%d/%m/%Y") if value else ""
        if self.changed:
            self.changed()
        self.page.update()

    def clear(self):
        self.selected_date = None
        self.field.value = ""
        self.picker.value = None


class OneClientPanel(ft.Column):
    def __init__(self, credentials):
        self.credentials = credentials
        self.reference = None
        self.status = StatusCard()
        self.selector = FacilitySelector(credentials, self._facility_changed)
        self.art = ft.TextField(
            label="ART number",
            hint_text="Digits only",
            keyboard_type=ft.KeyboardType.NUMBER,
            on_change=self._lookup_art,
            col={"xs": 12, "md": 5},
        )
        self.exact_art = ft.Dropdown(
            label="Matching EMR ART identifier",
            options=[],
            visible=False,
            on_select=self._exact_art_selected,
            col={"xs": 12, "md": 7},
        )
        self.regimen = ft.Text("Regimen: —", color=BLUE_DARK)
        self.last_encounter = DateInput(
            "Last Encounter Date", last_date=date.today()
        )
        self.return_date = DateInput("Return Visit Date")
        self.days_choice = ft.Dropdown(
            label="Days Dispensed",
            options=options(["30", "90", "180", "Other"]),
            on_select=self._days_changed,
            col={"xs": 12, "md": 4},
        )
        self.other_days = ft.TextField(
            label="Other number of days",
            keyboard_type=ft.KeyboardType.NUMBER,
            visible=False,
            col={"xs": 12, "md": 4},
        )
        self.override = ft.Checkbox(
            label="Proceed when the return date differs by 10 or more days",
            value=False,
        )
        self.busy = ft.ProgressRing(visible=False, width=24, height=24)
        self.update_button = ft.FilledButton(
            content="UPDATE EMR",
            icon=ft.Icons.CLOUD_UPLOAD,
            bgcolor=BLUE,
            color=ft.Colors.WHITE,
            on_click=self._update_client,
        )
        super().__init__(
            controls=[
                ft.Text("ONE_CLIENT", size=28, weight=ft.FontWeight.BOLD),
                ft.Text("Update one client directly from the facility reference."),
                self.selector,
                self.status,
                ft.ResponsiveRow(controls=[self.art, self.exact_art]),
                self.regimen,
                ft.Divider(),
                ft.ResponsiveRow(
                    controls=[
                        self.last_encounter,
                        self.days_choice,
                        self.other_days,
                        self.return_date,
                    ]
                ),
                self.override,
                ft.Row([self.update_button, self.busy]),
            ],
            scroll=ft.ScrollMode.AUTO,
            expand=True,
            spacing=16,
        )

    def _facility_changed(self, facility):
        self.reference = None
        self._clear_art_match()
        if not facility:
            self.status.set("Select a facility.")
            return
        try:
            self.reference = load_reference(facility)
            self.selector.context()
        except (ReferenceDataError, ValueError) as exc:
            self.status.set(str(exc), "warning")
            return
        self.status.set(
            f"{facility} reference loaded: {len(self.reference)} clients.",
            "success",
        )
        self._lookup_art()

    def _clear_art_match(self):
        self.exact_art.value = None
        self.exact_art.options = []
        self.exact_art.visible = False
        self.regimen.value = "Regimen: —"

    def _lookup_art(self, _=None):
        self._clear_art_match()
        if self.reference is None or not str(self.art.value or "").strip():
            return
        normalized = backend.normalize_art_numbers(
            pd.Series([self.art.value], dtype="string")
        ).iloc[0]
        if pd.isna(normalized):
            self.status.set("Enter a valid ART number containing digits.", "warning")
            if self.page:
                self.page.update()
            return
        reference = self.reference.copy()
        reference["ARTX"] = backend.normalize_art_numbers(reference["Art"])
        matches = reference.loc[reference["ARTX"].eq(normalized)].copy()
        art_numbers = (
            matches["Art"].dropna().astype("string").str.strip().drop_duplicates()
        )
        art_numbers = [value for value in art_numbers.tolist() if value]
        if not art_numbers:
            self.status.set(
                f"ART number {int(normalized)} was not found for this facility.",
                "warning",
            )
        elif len(art_numbers) == 1:
            self.exact_art.options = options(art_numbers)
            self.exact_art.value = art_numbers[0]
            self._exact_art_selected()
        else:
            self.exact_art.options = options(art_numbers)
            self.exact_art.visible = True
            self.status.set("Choose the matching full ART identifier.", "info")
        if self.page:
            self.page.update()

    def _exact_art_selected(self, _=None):
        if self.reference is None or not self.exact_art.value:
            return
        rows = self.reference.loc[
            self.reference["Art"].astype("string").str.strip().eq(self.exact_art.value)
        ]
        regimens = rows["ARVS"].dropna().astype("string").str.strip()
        regimens = regimens.loc[~regimens.isin(["", "nan", "<NA>"])]
        if regimens.empty:
            self.regimen.value = "Regimen: missing in reference"
            self.status.set("The selected client's ARVS value is blank.", "warning")
            return
        regimen = str(regimens.iloc[0]).replace("/", "-")
        self.regimen.value = f"Regimen: {regimen}"
        self.status.set(f"Ready: {self.exact_art.value}", "success")

    def _days_changed(self, _):
        self.other_days.visible = self.days_choice.value == "Other"
        self.other_days.value = "" if not self.other_days.visible else self.other_days.value
        self.page.update()

    def _quantity(self):
        raw = self.other_days.value if self.days_choice.value == "Other" else self.days_choice.value
        try:
            value = int(str(raw))
        except (TypeError, ValueError):
            raise ValueError("Enter a valid number of days dispensed.")
        if value < 1:
            raise ValueError("Days dispensed must be at least 1.")
        return value

    async def _update_client(self, _):
        try:
            context = self.selector.context()
            if self.reference is None:
                raise ValueError("The facility reference is not valid.")
            art_number = self.exact_art.value
            if not art_number:
                raise ValueError("Enter and select a valid ART number.")
            rows = self.reference.loc[
                self.reference["Art"].astype("string").str.strip().eq(art_number)
            ]
            regimens = rows["ARVS"].dropna().astype("string").str.strip()
            regimens = regimens.loc[~regimens.isin(["", "nan", "<NA>"])]
            if regimens.empty:
                raise ValueError("The selected client's ARVS value is blank.")
            regimen = str(regimens.iloc[0]).replace("/", "-")
            last_date = self.last_encounter.selected_date
            return_date = self.return_date.selected_date
            if not last_date or not return_date:
                raise ValueError("Select both encounter and return dates.")
            if last_date > date.today():
                raise ValueError("Last Encounter Date cannot be in the future.")
            if return_date <= last_date:
                raise ValueError("Return Visit Date must be after Last Encounter Date.")
            quantity = self._quantity()
            difference = (return_date - (last_date + timedelta(days=quantity))).days
            if abs(difference) >= 10 and not self.override.value:
                direction = "less" if difference < 0 else "more"
                raise ValueError(
                    f"Return Date is {direction} than expected by "
                    f"{abs(difference)} days. Tick the confirmation box to proceed."
                )
        except ValueError as exc:
            self.status.set(str(exc), "warning")
            self.page.update()
            return

        self.update_button.disabled = True
        self.busy.visible = True
        self.status.set(f"Updating {art_number}…")
        self.page.update()
        try:
            await asyncio.to_thread(
                backend.update_client,
                context["base_url"],
                context["username"],
                context["password"],
                art_number,
                last_date.isoformat(),
                return_date.isoformat(),
                quantity,
                regimen,
            )
        except Exception as exc:
            self.status.set(friendly_emr_error(exc), "error")
        else:
            self.status.set(f"SUCCESS: {art_number} was updated.", "success")
            self.art.value = ""
            self._clear_art_match()
            self.last_encounter.clear()
            self.return_date.clear()
            self.days_choice.value = None
            self.other_days.value = ""
            self.other_days.visible = False
            self.override.value = False
        finally:
            self.update_button.disabled = False
            self.busy.visible = False
            self.page.update()


class BatchPanel(ft.Column):
    def __init__(self, credentials, mode):
        self.credentials = credentials
        self.mode = mode
        self.reference = None
        self.uploaded_name = None
        self.ready = pd.DataFrame()
        self.rejected = pd.DataFrame()
        self.clean_data = pd.DataFrame()
        self.status = StatusCard()
        self.selector = FacilitySelector(credentials, self._facility_changed)
        self.file_name = ft.Text("No CSV selected", italic=True)
        self.ready_count = ft.Text("READY: 0", weight=ft.FontWeight.BOLD)
        self.rejected_count = ft.Text("REJECTED: 0", weight=ft.FontWeight.BOLD)
        self.issues = ft.Column(spacing=4)
        self.progress = ft.ProgressBar(value=0, visible=False, color=BLUE)
        self.progress_text = ft.Text(visible=False)
        self.pick_button = ft.Button(
            content="CHOOSE CSV",
            icon=ft.Icons.UPLOAD_FILE,
            on_click=self._pick_csv,
        )
        self.update_button = ft.FilledButton(
            content="BATCH UPDATE",
            icon=ft.Icons.CLOUD_UPLOAD,
            bgcolor=BLUE,
            color=ft.Colors.WHITE,
            disabled=True,
            on_click=self._run_updates,
        )
        self.save_rejected = ft.Button(
            content="SAVE REJECTED CSV",
            icon=ft.Icons.DOWNLOAD,
            visible=False,
            on_click=self._save_rejected,
        )
        self.save_clean = ft.Button(
            content="SAVE CLEANING CSV",
            icon=ft.Icons.CLEANING_SERVICES,
            visible=False,
            on_click=self._save_clean,
        )
        title = "EREGISTERS" if mode == "eregister" else "EMR_EXTRACT"
        description = (
            "Upload the full E-register export, clean it, then update valid clients."
            if mode == "eregister"
            else "Upload a compact EMR extract, validate it, then update valid clients."
        )
        required_columns = ft.Container(
            content=ft.Text(
                "REQUIRED CSV COLUMNS:  ART  |  LD  |  ARVD   •   RD = LD + ARVD",
                size=12,
                weight=ft.FontWeight.BOLD,
                color=BLUE_DARK,
            ),
            bgcolor=SURFACE,
            border_radius=8,
            padding=ft.Padding.symmetric(horizontal=10, vertical=6),
            visible=mode == "extract",
        )
        super().__init__(
            controls=[
                ft.Text(title, size=28, weight=ft.FontWeight.BOLD),
                ft.Text(description),
                required_columns,
                self.selector,
                self.status,
                ft.Row([self.pick_button, self.file_name], wrap=True),
                ft.Row([self.ready_count, self.rejected_count], wrap=True),
                self.issues,
                self.progress,
                self.progress_text,
                ft.Row(
                    [self.update_button, self.save_rejected, self.save_clean],
                    wrap=True,
                ),
            ],
            scroll=ft.ScrollMode.AUTO,
            expand=True,
            spacing=16,
        )

    def _facility_changed(self, facility):
        self.reference = None
        self._reset_file_state()
        if not facility:
            self.status.set("Select a facility.")
            return
        try:
            self.reference = load_reference(facility)
            self.selector.context()
        except (ReferenceDataError, ValueError) as exc:
            self.status.set(str(exc), "warning")
            return
        self.status.set(
            f"{facility} reference loaded: {len(self.reference)} clients.",
            "success",
        )

    def _reset_file_state(self):
        self.uploaded_name = None
        self.ready = pd.DataFrame()
        self.rejected = pd.DataFrame()
        self.clean_data = pd.DataFrame()
        self.file_name.value = "No CSV selected"
        self.ready_count.value = "READY: 0"
        self.rejected_count.value = "REJECTED: 0"
        self.issues.controls = []
        self.update_button.disabled = True
        self.save_rejected.visible = False
        self.save_clean.visible = False

    async def _pick_csv(self, _):
        try:
            self.selector.context()
            if self.reference is None:
                raise ValueError("The selected facility reference is not valid.")
        except ValueError as exc:
            self.status.set(str(exc), "warning")
            self.page.update()
            return
        files = await ft.FilePicker().pick_files(
            allow_multiple=False,
            with_data=True,
            file_type=ft.FilePickerFileType.CUSTOM,
            allowed_extensions=["csv"],
        )
        if not files:
            return
        selected = files[0]
        payload = selected.bytes
        if payload is None and selected.path:
            payload = Path(selected.path).read_bytes()
        if payload is None:
            self.status.set("The selected CSV could not be read.", "error")
            self.page.update()
            return

        self._reset_file_state()
        self.uploaded_name = selected.name
        self.file_name.value = selected.name
        self.status.set(f"Validating {selected.name}…")
        self.page.update()
        try:
            uploaded = pd.read_csv(
                BytesIO(payload),
                dtype={"ART": "string", "ART: Art Number": "string"},
            )
            if self.mode == "eregister":
                prepared = prepare_eregister(uploaded, self.reference)
            else:
                ready, rejected = backend.prepare_batch(uploaded, self.reference)
                prepared = PreparedBatch(
                    ready=ready,
                    rejected=rejected,
                    clean_data=pd.DataFrame(),
                )
        except (
            OSError,
            UnicodeError,
            TypeError,
            ValueError,
            pd.errors.EmptyDataError,
            pd.errors.ParserError,
        ) as exc:
            self.status.set(f"Upload rejected: {exc}", "error")
            self.page.update()
            return

        self.ready = prepared.ready
        self.rejected = prepared.rejected
        self.clean_data = prepared.clean_data
        self.ready_count.value = f"READY: {len(self.ready)}"
        self.rejected_count.value = f"REJECTED: {len(self.rejected)}"
        self.save_rejected.visible = not self.rejected.empty
        self.save_clean.visible = prepared.blocked and not self.clean_data.empty
        self.update_button.disabled = self.ready.empty or prepared.blocked
        self.issues.controls = [
            ft.Text(f"• {reason}: {count}", color=WARNING)
            for reason, count in prepared.issue_counts.items()
        ]
        if prepared.blocked:
            self.status.set(
                "E-register data issues must be corrected before updating EMR.",
                "warning",
            )
        elif self.ready.empty:
            self.status.set("There are no valid clients to update.", "warning")
        else:
            self.status.set(
                f"Validation complete. {len(self.ready)} clients are ready.",
                "success",
            )
        self.page.update()

    async def _save_frame(self, frame, file_name):
        if frame.empty:
            return
        path = await ft.FilePicker().save_file(
            file_name=file_name,
            file_type=ft.FilePickerFileType.CUSTOM,
            allowed_extensions=["csv"],
            src_bytes=frame_to_csv_bytes(frame),
        )
        self.status.set(
            f"Saved {file_name}" + (f" to {path}" if path else ""),
            "success",
        )
        self.page.update()

    async def _save_rejected(self, _):
        await self._save_frame(self.rejected, "rejected_data.csv")

    async def _save_clean(self, _):
        await self._save_frame(self.clean_data, "clean_data.csv")

    async def _run_updates(self, _):
        if self.ready.empty:
            return
        try:
            context = self.selector.context()
        except ValueError as exc:
            self.status.set(str(exc), "warning")
            self.page.update()
            return

        self.update_button.disabled = True
        self.pick_button.disabled = True
        self.progress.visible = True
        self.progress.value = 0
        self.progress_text.visible = True
        failed_rows = []
        successful = 0
        total = len(self.ready)
        for position, (_, client) in enumerate(self.ready.iterrows(), start=1):
            art_number = str(client["Art"]).strip()
            self.progress_text.value = f"Updating {position} of {total}: {art_number}"
            self.page.update()
            try:
                await asyncio.to_thread(
                    backend.update_client,
                    context["base_url"],
                    context["username"],
                    context["password"],
                    art_number,
                    client["LD_PARSED"].date().isoformat(),
                    client["RD_PARSED"].date().isoformat(),
                    int(client["ARVD_PARSED"]),
                    str(client["ARVS"]).strip(),
                )
                successful += 1
            except Exception as exc:
                failed = client.to_dict()
                failed["REASON_REJECTED"] = friendly_emr_error(exc)
                failed_rows.append(failed)
            self.progress.value = position / total
            self.page.update()

        frames = [self.rejected]
        if failed_rows:
            frames.append(pd.DataFrame(failed_rows))
        self.rejected = pd.concat(frames, ignore_index=True, sort=False)
        self.rejected_count.value = f"REJECTED: {len(self.rejected)}"
        self.save_rejected.visible = not self.rejected.empty
        self.progress_text.value = f"Completed {total} clients."
        self.status.set(
            f"UPDATED: {successful}; REJECTED: {len(self.rejected)}",
            "success" if not failed_rows else "warning",
        )
        self.pick_button.disabled = False
        self.progress.visible = False
        self.update_button.disabled = True
        self.page.update()


def main(page: ft.Page):
    page.title = "UgandaEMR Update Tool"
    page.theme_mode = ft.ThemeMode.LIGHT
    page.padding = 0
    page.theme = ft.Theme(color_scheme_seed=BLUE)
    page.window.min_width = 360
    page.window.min_height = 640

    try:
        credentials = load_credentials()
    except Exception as exc:
        page.add(
            ft.SafeArea(
                ft.Container(
                    ft.Column(
                        [
                            ft.Text("UgandaEMR Update Tool", size=28, weight=ft.FontWeight.BOLD),
                            ft.Text(str(exc), color=ERROR),
                        ]
                    ),
                    padding=24,
                )
            )
        )
        return

    panels = [
        OneClientPanel(credentials),
        BatchPanel(credentials, "eregister"),
        BatchPanel(credentials, "extract"),
    ]
    content = ft.Container(
        content=panels[0],
        expand=True,
        padding=24,
        bgcolor=ft.Colors.WHITE,
    )

    rail_state = {"expanded": True, "manually_set": False}
    brand_text = ft.Text(
        "EMR TOOL", weight=ft.FontWeight.BOLD, color=BLUE_DARK
    )
    toggle_rail_button = ft.IconButton()

    rail = ft.NavigationRail(
        selected_index=0,
        extended=True,
        min_extended_width=230,
        bgcolor=SURFACE,
        indicator_color=ft.Colors.BLUE_100,
        leading=ft.Column(
            [
                ft.Icon(ft.Icons.MEDICAL_SERVICES, color=BLUE, size=34),
                brand_text,
                toggle_rail_button,
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        destinations=[
            ft.NavigationRailDestination(
                icon=ft.Icons.PERSON_OUTLINE,
                selected_icon=ft.Icons.PERSON,
                label="ONE_CLIENT",
            ),
            ft.NavigationRailDestination(
                icon=ft.Icons.TABLE_VIEW_OUTLINED,
                selected_icon=ft.Icons.TABLE_VIEW,
                label="EREGISTERS",
            ),
            ft.NavigationRailDestination(
                icon=ft.Icons.DESCRIPTION_OUTLINED,
                selected_icon=ft.Icons.DESCRIPTION,
                label="EMR_EXTRACT",
            ),
        ],
    )

    def navigate(event):
        content.content = panels[event.control.selected_index]
        page.update()

    rail.on_change = navigate

    def set_rail_expanded(expanded):
        rail_state["expanded"] = expanded
        rail.extended = expanded
        rail.label_type = (
            None if expanded else ft.NavigationRailLabelType.SELECTED
        )
        rail.min_width = None if expanded else 72
        brand_text.visible = expanded
        toggle_rail_button.icon = (
            ft.Icons.KEYBOARD_DOUBLE_ARROW_LEFT
            if expanded
            else ft.Icons.KEYBOARD_DOUBLE_ARROW_RIGHT
        )
        toggle_rail_button.tooltip = (
            "Collapse sidebar" if expanded else "Expand sidebar"
        )

    def toggle_rail(_):
        rail_state["manually_set"] = True
        set_rail_expanded(not rail_state["expanded"])
        page.update()

    toggle_rail_button.on_click = toggle_rail

    def resize(_=None):
        if not rail_state["manually_set"]:
            set_rail_expanded((page.width or 1000) >= 760)
        page.update()

    page.on_resize = resize
    page.add(
        ft.SafeArea(
            ft.Row(
                [
                    rail,
                    ft.VerticalDivider(width=1),
                    content,
                ],
                expand=True,
                spacing=0,
            ),
            expand=True,
        )
    )
    resize()


if __name__ == "__main__":
    ft.run(main, assets_dir="assets")
