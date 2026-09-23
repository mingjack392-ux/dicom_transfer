#!/usr/bin/env python3
"""Shared rules for the enrolled-patient DICOM anonymization workflow.

This module intentionally has no dependency on the existing anonymization
entrypoints.  The customer-specific rule keeps PatientID and every date/time
value, while removing PatientName, PatientBirthDate and institution names.
"""

from __future__ import annotations

import math
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence


YES_VALUES = {"是", "y", "yes", "true", "1"}
NO_VALUES = {"否", "n", "no", "false", "0"}
RULE_VERSION = "2026.09.07-enrolled-anon-v1.4"


@dataclass(frozen=True)
class TableData:
    path: Path
    sheet_name: str
    headers: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class PeriodResult:
    category: str
    target_suffix: str
    valid: bool
    note: str
    followup_month: float | None = None
    is_12m_candidate: bool = False


def text(value: Any) -> str:
    """Return a stable string while preserving true identifiers as text."""

    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return ""
        if value.is_integer():
            return str(int(value))
        return format(value, ".15g")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value).strip()


def normalized_text(value: Any) -> str:
    value_text = unicodedata.normalize("NFKC", text(value))
    return re.sub(r"\s+", "", value_text).casefold()


def normalized_name(value: Any) -> str:
    return normalized_text(value).replace("^", "")


def normalized_identifier(value: Any) -> str:
    value_text = unicodedata.normalize("NFKC", text(value)).strip()
    if re.fullmatch(r"[+-]?\d+\.0+", value_text):
        value_text = value_text.split(".", 1)[0]
    return re.sub(r"\s+", "", value_text).casefold()


def normalized_screening(value: Any) -> str:
    value_text = unicodedata.normalize("NFKC", text(value)).strip().upper()
    value_text = re.sub(r"[＿_－—–]", "-", value_text)
    value_text = re.sub(r"\s+", "", value_text)
    match = re.fullmatch(r"0*(\d+)-(.+)", value_text)
    if match:
        return f"{int(match.group(1))}-{match.group(2)}"
    return value_text


def normalized_header(value: Any) -> str:
    return re.sub(r"[\s_\-/（）()]+", "", normalized_text(value))


def anonymous_code(global_number: Any) -> str:
    raw = text(global_number)
    if not re.fullmatch(r"\d+(?:\.0+)?", raw):
        raise ValueError(f"编号不是正整数: {raw or '<空>'}")
    number = int(float(raw))
    if number <= 0:
        raise ValueError(f"编号不是正整数: {raw}")
    return str(number).zfill(3)


def is_yes(value: Any) -> bool:
    return normalized_text(value) in YES_VALUES


def _row_values_xlsx(path: Path, sheet_name: str | None, max_columns: int | None):
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise RuntimeError(
            "读取或生成.xlsx需要openpyxl，请执行: python -m pip install openpyxl"
        ) from exc

    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        worksheets = (
            [workbook[sheet_name]] if sheet_name else list(workbook.worksheets)
        )
        for worksheet in worksheets:
            rows = []
            for row in worksheet.iter_rows(values_only=True):
                values = list(row[:max_columns] if max_columns else row)
                rows.append(values)
            yield worksheet.title, rows
    finally:
        workbook.close()


def _row_values_xls(path: Path, sheet_name: str | None, max_columns: int | None):
    try:
        import xlrd
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise RuntimeError(
            "读取旧版.xls需要xlrd，请执行: python -m pip install xlrd"
        ) from exc

    workbook = xlrd.open_workbook(path, on_demand=True)
    try:
        sheets = (
            [workbook.sheet_by_name(sheet_name)]
            if sheet_name
            else [workbook.sheet_by_index(i) for i in range(workbook.nsheets)]
        )
        for sheet in sheets:
            width = min(sheet.ncols, max_columns) if max_columns else sheet.ncols
            rows: list[list[Any]] = []
            for row_index in range(sheet.nrows):
                row: list[Any] = []
                for column_index in range(width):
                    cell = sheet.cell(row_index, column_index)
                    value: Any = cell.value
                    if cell.ctype == xlrd.XL_CELL_DATE:
                        value = xlrd.xldate.xldate_as_datetime(value, workbook.datemode)
                    row.append(value)
                rows.append(row)
            yield sheet.name, rows
    finally:
        workbook.release_resources()


def read_table(
    path: str | os.PathLike[str],
    required_headers: Sequence[str],
    *,
    sheet_name: str | None = None,
    max_columns: int | None = None,
    header_aliases: dict[str, Sequence[str]] | None = None,
) -> TableData:
    """Read the first sheet whose first non-empty row contains all headers."""

    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"表格不存在: {source}")
    suffix = source.suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        candidates = _row_values_xlsx(source, sheet_name, max_columns)
    elif suffix == ".xls":
        candidates = _row_values_xls(source, sheet_name, max_columns)
    else:
        raise ValueError(f"不支持的表格格式: {source.suffix}，仅支持.xls/.xlsx/.xlsm")

    aliases = header_aliases or {}
    required_lookup: dict[str, list[str]] = {}
    for canonical in required_headers:
        names = [canonical, *aliases.get(canonical, ())]
        required_lookup[canonical] = list(
            dict.fromkeys(normalized_header(name) for name in names)
        )

    examined: list[str] = []
    for candidate_sheet, matrix in candidates:
        examined.append(candidate_sheet)
        for header_index, raw_headers in enumerate(matrix[:20]):
            normalized = [normalized_header(value) for value in raw_headers]
            if not any(normalized):
                continue
            canonical_by_column: dict[int, str] = {}
            used_columns: set[int] = set()
            for canonical, accepted_in_priority_order in required_lookup.items():
                for accepted in accepted_in_priority_order:
                    matches = [
                        i
                        for i, value in enumerate(normalized)
                        if value == accepted and i not in used_columns
                    ]
                    if len(matches) == 1:
                        canonical_by_column[matches[0]] = canonical
                        used_columns.add(matches[0])
                        break
            if len(canonical_by_column) != len(required_headers):
                continue

            final_headers: list[str] = []
            used: dict[str, int] = {}
            for index, raw in enumerate(raw_headers):
                header = canonical_by_column.get(index, text(raw) or f"未命名列{index + 1}")
                used[header] = used.get(header, 0) + 1
                if used[header] > 1:
                    header = f"{header}_{used[header]}"
                final_headers.append(header)

            output_rows: list[dict[str, Any]] = []
            for excel_row, values in enumerate(matrix[header_index + 1 :], header_index + 2):
                padded = list(values) + [None] * (len(final_headers) - len(values))
                row = {header: padded[i] for i, header in enumerate(final_headers)}
                if not any(text(value) for value in padded):
                    continue
                row["__row__"] = excel_row
                output_rows.append(row)
            return TableData(
                path=source,
                sheet_name=candidate_sheet,
                headers=tuple(final_headers),
                rows=tuple(output_rows),
            )

    required_display = "、".join(required_headers)
    sheets_display = "、".join(examined) or "<无>"
    raise ValueError(
        f"在表格 {source} 的工作表 {sheets_display} 中未找到完整表头: {required_display}"
    )


def choose_center(registry_rows: Iterable[dict[str, Any]], query: str) -> tuple[str, str]:
    def center_code_key(value: Any) -> str:
        key = normalized_identifier(value)
        return str(int(key)) if re.fullmatch(r"\d+", key) else key

    centers: dict[tuple[str, str], None] = {}
    for row in registry_rows:
        code = text(row.get("中心编号"))
        name = text(row.get("中心名称"))
        if code and name:
            centers[(code, name)] = None

    normalized_query = normalized_text(query)
    exact = [
        center
        for center in centers
        if normalized_query
        in {center_code_key(center[0]), normalized_text(center[1])}
    ]
    if not exact and center_code_key(query) != normalized_query:
        exact = [center for center in centers if center_code_key(query) == center_code_key(center[0])]
    matches = exact or [
        center
        for center in centers
        if normalized_query and normalized_query in normalized_text(center[1])
    ]
    if len(matches) != 1:
        available = "；".join(f"{code}:{name}" for code, name in centers)
        raise ValueError(
            f"中心“{query}”无法唯一对应分中心影像表。匹配数={len(matches)}；可选中心: {available}"
        )
    return matches[0]


def _parse_period_token(value: Any) -> tuple[str, str, float | None]:
    raw = text(value)
    compact = normalized_text(value)
    if not compact:
        return "unknown", "空时期", None
    if compact == "术前":
        return "pre", raw, None
    if compact == "术中":
        return "intra", raw, None

    number: float | None = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
    else:
        month_match = re.fullmatch(r"术后([+-]?\d+(?:\.\d+)?)个?月", compact)
        plain_match = re.fullmatch(r"[+-]?\d+(?:\.\d+)?", compact)
        if month_match:
            number = float(month_match.group(1))
        elif plain_match:
            number = float(compact)

    if number is None or not math.isfinite(number):
        return "unknown", f"无法识别时期“{raw}”", None
    if 3 <= number < 9:
        return "6m", raw, number
    if number >= 9:
        return "12m", raw, number
    return "out", f"{raw}小于3个月，无法归入6M或12M", number


def classify_period(values: Iterable[Any]) -> PeriodResult:
    parsed = [_parse_period_token(value) for value in values]
    kinds = {kind for kind, _, _ in parsed}
    notes = [note for _, note, _ in parsed]

    if not parsed or "unknown" in kinds or "out" in kinds:
        reason = "；".join(dict.fromkeys(notes)) or "时期为空"
        return PeriodResult("时期待确认", "", False, reason)
    if kinds == {"pre"}:
        return PeriodResult("术前", "术前", True, "同一Study全部为术前")
    if kinds == {"intra"}:
        return PeriodResult("术中", "术中", True, "同一Study全部为术中")
    if kinds == {"pre", "intra"}:
        return PeriodResult("术前与术中", "术前与术中", True, "同一Study同时含术前和术中")
    if kinds == {"6m"}:
        return PeriodResult("6M", "6M", True, "术后月数满足3≤月数<9")
    if kinds == {"12m"}:
        months = [month for _, _, month in parsed if month is not None]
        representative = min(months, key=lambda month: (abs(month - 12), month))
        if max(months) - min(months) > 0.05:
            values_display = "、".join(format(month, ".6g") for month in sorted(set(months)))
            return PeriodResult(
                "时期待确认",
                "",
                False,
                f"同一Study存在不一致的术后月数: {values_display}",
            )
        if representative <= 15:
            note = f"12M候选月份={representative:g}，位于9～15个月优先窗口"
        else:
            note = f"12M候选月份={representative:g}，超出优先窗口，等待患者级最近值选择"
        return PeriodResult(
            "12M",
            "12M",
            True,
            note,
            followup_month=representative,
            is_12m_candidate=True,
        )

    display = "、".join(sorted(kinds))
    return PeriodResult("时期待确认", "", False, f"同一Study存在跨类别时期: {display}")


def atomic_save_workbook(workbook: Any, output_path: Path, overwrite: bool) -> None:
    output_path = output_path.resolve()
    if output_path.exists() and not overwrite:
        raise FileExistsError(f"输出文件已存在，未覆盖: {output_path}；确认后使用--overwrite")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.stem}.", suffix=".tmp.xlsx", dir=output_path.parent
    )
    os.close(handle)
    temporary_path = Path(temporary_name)
    try:
        workbook.save(temporary_path)
        os.replace(temporary_path, output_path)
    finally:
        temporary_path.unlink(missing_ok=True)
