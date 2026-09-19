"""Pure CSV preparation helpers used by the Flet user interface."""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from batch4 import prepare_batch


EREGISTER_REQUIRED_COLUMNS = {
    "MR - First name",
    "MR - Surname",
    "MR - Sex",
    "HIV/ART-Next Appointment date",
    "Last updated on",
    "ART: Art Number",
    "HIV-ART Regimen - No. of days dispensed",
    "Service Type",
}


@dataclass
class PreparedBatch:
    ready: pd.DataFrame
    rejected: pd.DataFrame
    clean_data: pd.DataFrame
    issue_counts: dict[str, int] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return any(count > 0 for count in self.issue_counts.values())


def parse_dates(values):
    """Parse E-register dates without swapping ISO months and days."""
    text = values.astype("string").str.strip()
    parsed = pd.Series(pd.NaT, index=values.index, dtype="datetime64[ns]")

    # Excel date/time serials can appear when users upload an untouched export.
    numeric = pd.to_numeric(text, errors="coerce")
    excel_serial = numeric.between(20_000, 80_000)
    parsed.loc[excel_serial] = pd.to_datetime(
        numeric.loc[excel_serial],
        unit="D",
        origin="1899-12-30",
        errors="coerce",
    )

    # `dayfirst=True` corrupts ambiguous ISO values; e.g. 2026-08-07 can
    # become 2026-07-08. Parse year-first values separately.
    iso_date = (
        text.str.match(
            r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:[T\s]|$)",
            na=False,
        )
        & parsed.isna()
    )
    parsed.loc[iso_date] = pd.to_datetime(
        text.loc[iso_date],
        format="mixed",
        yearfirst=True,
        errors="coerce",
    )

    # Values containing only a time have already lost their calendar date and
    # must not be silently converted to today's date.
    time_only = text.str.match(
        r"^\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?$",
        na=False,
    )
    day_first_date = (
        parsed.isna()
        & text.notna()
        & text.ne("")
        & ~time_only
    )
    parsed.loc[day_first_date] = pd.to_datetime(
        text.loc[day_first_date],
        format="mixed",
        dayfirst=True,
        errors="coerce",
    )
    return parsed


def normalize_art_numbers(values):
    digits = values.astype("string").str.replace(r"[^0-9]", "", regex=True)
    digits = digits.mask(digits.eq(""))
    numbers = pd.to_numeric(digits, errors="coerce")
    numbers = numbers.where(numbers > 0)
    return numbers.astype("Int64")


def _empty_result(columns):
    empty = pd.DataFrame(columns=columns)
    return PreparedBatch(empty, empty.copy(), empty.copy())


def prepare_eregister(uploaded, reference):
    """Reproduce batch3's E-register cleaning and reference matching flow."""
    missing = EREGISTER_REQUIRED_COLUMNS - set(uploaded.columns)
    if missing:
        raise ValueError(
            "Uploaded E-register CSV is missing required columns: "
            + ", ".join(sorted(missing))
        )
    if uploaded.empty:
        return _empty_result(uploaded.columns)

    data = uploaded.copy()
    data.insert(0, "EREG_SOURCE_ROW", range(2, len(data) + 2))
    data["Service Type"] = data["Service Type"].astype("string")
    data = data.loc[
        data["Service Type"].str.contains("ART", case=False, na=False)
    ].copy()
    if data.empty:
        empty = pd.DataFrame(columns=[*uploaded.columns, "REASON_REJECTED"])
        return PreparedBatch(
            ready=empty.copy(),
            rejected=empty.copy(),
            clean_data=empty.copy(),
            issue_counts={"NO ART SERVICE ROWS": 1},
        )

    data["ARTX"] = normalize_art_numbers(data["ART: Art Number"])
    data["LD_PARSED"] = parse_dates(data["Last updated on"])
    data["RD_PARSED"] = parse_dates(data["HIV/ART-Next Appointment date"])
    data["ARVD_PARSED"] = pd.to_numeric(
        data["HIV-ART Regimen - No. of days dispensed"], errors="coerce"
    )

    raw_next = data["HIV/ART-Next Appointment date"].astype("string").str.strip()
    invalid_next = raw_next.notna() & raw_next.ne("") & data["RD_PARSED"].isna()

    # Derive days from the appointment when days are absent, exactly as batch3.
    derive_days = (
        data["ARVD_PARSED"].isna()
        & data["LD_PARSED"].notna()
        & data["RD_PARSED"].notna()
    )
    data.loc[derive_days, "ARVD_PARSED"] = (
        data.loc[derive_days, "RD_PARSED"]
        - data.loc[derive_days, "LD_PARSED"]
    ).dt.days

    # Derive the return date when days are present but the appointment is absent.
    derive_return = (
        data["RD_PARSED"].isna()
        & ~invalid_next
        & data["LD_PARSED"].notna()
        & data["ARVD_PARSED"].notna()
    )
    data.loc[derive_return, "RD_PARSED"] = data.loc[
        derive_return, "LD_PARSED"
    ] + pd.to_timedelta(data.loc[derive_return, "ARVD_PARSED"], unit="D")

    data["REASON_REJECTED"] = pd.NA

    def mark(mask, reason):
        available = data["REASON_REJECTED"].isna()
        data.loc[mask & available, "REASON_REJECTED"] = reason

    mark(data["ARTX"].isna(), "NO ART NUMBER")
    duplicate_art = data["ARTX"].notna() & data["ARTX"].duplicated(keep=False)
    mark(duplicate_art, "ROW-COUNT CHECK FAILED: DUPLICATED ART NUMBER")
    mark(data["LD_PARSED"].isna(), "BLANK OR INVALID LAST ENCOUNTER DATE")
    mark(invalid_next, "INVALID NEXT APPOINTMENT DATE")
    mark(data["ARVD_PARSED"].isna(), "MISSING DAYS DISPENSED AND NEXT APPOINTMENT DATE")
    mark(data["RD_PARSED"].isna(), "BLANK OR INVALID NEXT APPOINTMENT DATE")
    mark(data["ARVD_PARSED"] < 0, "NEXT APPOINTMENT BEFORE LAST ENCOUNTER")
    mark(
        (data["ARVD_PARSED"] >= 0) & (data["ARVD_PARSED"] < 30),
        "FEW DAYS DISPENSED, CHECK",
    )
    mark(data["ARVD_PARSED"] > 185, "MANY DAYS DISPENSED, CHECK")
    mark(
        data["ARVD_PARSED"].notna() & (data["ARVD_PARSED"] % 1 != 0),
        "DAYS DISPENSED MUST BE A WHOLE NUMBER",
    )

    issue_rows = data.loc[data["REASON_REJECTED"].notna()].copy()
    issue_counts = {
        str(reason): int(count)
        for reason, count in issue_rows["REASON_REJECTED"].value_counts().items()
    }

    clean_data = data.copy()
    clean_data["ART"] = clean_data["ARTX"].astype("string")
    clean_data["Last updated on"] = clean_data["LD_PARSED"].dt.strftime(
        "%d/%m/%Y"
    )
    clean_data["HIV/ART-Next Appointment date"] = clean_data[
        "RD_PARSED"
    ].dt.strftime("%d/%m/%Y")
    clean_data["HIV-ART Regimen - No. of days dispensed"] = clean_data[
        "ARVD_PARSED"
    ]
    internal = ["LD_PARSED", "RD_PARSED", "ARVD_PARSED", "ARTX"]
    clean_data = clean_data.drop(columns=internal, errors="ignore")

    # batch3 blocks the entire upload until E-register data issues are corrected.
    if issue_counts:
        return PreparedBatch(
            ready=pd.DataFrame(),
            rejected=issue_rows.drop(columns=internal, errors="ignore"),
            clean_data=clean_data,
            issue_counts=issue_counts,
        )

    compact = data.copy()
    compact["ART"] = compact["ART: Art Number"]
    compact["LD"] = compact["LD_PARSED"]
    compact["ARVD"] = compact["ARVD_PARSED"]
    ready, rejected = prepare_batch(compact, reference)
    # E-register appointments are authoritative when both dates were supplied.
    if not ready.empty:
        appointments = data.set_index("EREG_SOURCE_ROW")["RD_PARSED"]
        ready["RD_PARSED"] = ready["EREG_SOURCE_ROW"].map(appointments)
        ready["RD"] = ready["RD_PARSED"].dt.strftime("%d/%m/%Y")
    return PreparedBatch(
        ready=ready,
        rejected=rejected,
        clean_data=clean_data,
        issue_counts={},
    )


def frame_to_csv_bytes(frame):
    return frame.to_csv(index=False).encode("utf-8")
