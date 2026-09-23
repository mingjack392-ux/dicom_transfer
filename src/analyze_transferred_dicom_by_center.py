#!/usr/bin/env python3
"""按中心分析已转存 DICOM，并生成第四批结构的影像-web工作簿。

本入口与 V2/V3 转存流程独立，只读扫描已转存目录和分中心 Excel：

* 转存根目录的一级子目录视为中心，输出工作表使用中心目录名；
* 主表固定采用第四批的 15 个字段；
* 单帧 DICOM 按 Series 汇总，多帧 DICOM 按 SOP 保留；
* 住院号和手术日期在同一中心内按规范姓名匹配，姓名缺失时可按严格格式的受试者筛选号回退；
* 时期按 ``(AcquisitionDate - 手术日期).days`` 计算，正数除以 30；
* 默认只预览统计，显式 ``--write`` 才生成新的 Excel，且不覆盖现有文件。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence

import pydicom
from pydicom.config import IGNORE


# 部分设备会写出连续点或超过64字符的UID。这些值仍需原样进入影像-web，
# pydicom的逐文件重复警告会淹没运行统计，因此关闭库级读取校验输出。
pydicom.config.settings.reading_validation_mode = IGNORE


RULE_VERSION = "2026.09.21-center-web-v1.3"
NA_VALUE = "NA"

WEB_HEADERS = [
    "住院号",
    "患者",
    "StudyInstanceUID",
    "SeriesUID",
    "SOPUID",
    "AcqusitionDate",
    "时期",
    "影像类型",
    "序列描述",
    "SliceThickness",
    "NumberOfFrames",
    "帧数",
    "modality",
    "第一拍摄角度",
    "第二拍摄角度",
]

CENTER_REQUIRED_HEADERS = (
    "中心名称",
    "姓名",
    "住院号/门诊号/放射号",
    "手术日期",
)

CENTER_OPTIONAL_HEADERS = (
    "受试者筛选号",
    "姓名缩写",
)

DICOM_TAGS = [
    "PatientName",
    "PatientID",
    "StudyInstanceUID",
    "SeriesInstanceUID",
    "SOPInstanceUID",
    "SOPClassUID",
    "AcquisitionDate",
    "StudyDate",
    "SeriesDate",
    "ImageType",
    "SeriesDescription",
    "SliceThickness",
    "NumberOfFrames",
    "Modality",
    "PositionerPrimaryAngle",
    "PositionerSecondaryAngle",
]

EXCLUDED_DIRECTORY_NAMES = {
    ".dicom_v3_state",
    "_quarantine",
    "_identity_review",
    "logs",
    "outputs",
}

SKIPPED_FILE_SUFFIXES = {
    ".csv",
    ".json",
    ".log",
    ".png",
    ".jpg",
    ".jpeg",
    ".xlsx",
    ".xls",
}

GENERIC_PATIENT_DIRECTORIES = {
    "数据",
    "原始数据",
    "影像",
    "影像数据",
    "患者",
    "患者数据",
    "病例",
    "病例数据",
    "检查",
    "检查数据",
    "资料",
    "导出",
    "备份",
}

CHINESE_TEXT_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]{2,8}")
INVALID_SHEET_CHARACTER_PATTERN = re.compile(r"[\\/*?:\[\]]")
SCREENING_NUMBER_PATTERN = re.compile(r"^(\d+)[_-]([HQ]\d+)$", re.IGNORECASE)


@dataclass(frozen=True)
class CenterRecord:
    center_name: str
    patient_name: str
    hospital_number: str
    surgery_date: Optional[date]
    source_sheet: str
    source_row: int
    source_order: int
    screening_number: str = ""
    patient_initials: str = ""

    @property
    def signature(self) -> tuple[str, str, str, str, str, str]:
        return (
            normalize_center_name(self.center_name),
            normalize_patient_name(self.patient_name),
            normalize_identifier(self.hospital_number),
            self.surgery_date.isoformat() if self.surgery_date else "",
            normalize_screening_number(self.screening_number),
            normalize_patient_name(self.patient_initials),
        )


@dataclass(frozen=True)
class DicomRecord:
    path: Path
    relative_path: str
    patient_id: str
    header_patient_name: str
    patient_name: str
    study_uid: str
    series_uid: str
    sop_uid: str
    acquisition_date: Optional[date]
    acquisition_date_raw: str
    study_date: Optional[date]
    series_date: Optional[date]
    image_type: str
    series_description: str
    slice_thickness: Any
    number_of_frames: Optional[int]
    modality: str
    primary_angle: Any
    secondary_angle: Any
    screening_number: str = ""

    @property
    def is_multiframe(self) -> bool:
        return (self.number_of_frames or 0) > 1


@dataclass
class ScanOutcome:
    path: Path
    is_dicom: bool
    record: Optional[DicomRecord] = None
    error: str = ""


@dataclass
class CenterResult:
    sheet_name: str
    source_center_name: str
    reference_center_name: str
    source_path: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    exceptions: list[dict[str, Any]] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)


def text_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def normalize_patient_name(value: Any) -> str:
    return re.sub(r"[\s\u3000]+", "", text_value(value)).casefold()


def normalize_center_name(value: Any) -> str:
    return re.sub(r"[\s\u3000\-—_()（）]+", "", text_value(value)).casefold()


def normalize_identifier(value: Any) -> str:
    text = text_value(value)
    if re.fullmatch(r"[+-]?\d+\.0+", text):
        return text.split(".", 1)[0]
    return text


def normalize_screening_number(value: Any) -> str:
    text = re.sub(r"\s+", "", text_value(value)).upper()
    match = SCREENING_NUMBER_PATTERN.fullmatch(text)
    if not match:
        return ""
    return f"{match.group(1)}-{match.group(2).upper()}"


def parse_excel_date(value: Any) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return (datetime(1899, 12, 30) + timedelta(days=float(value))).date()
        except (OverflowError, ValueError):
            return None
    text = text_value(value)
    if not text:
        return None
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        try:
            return date(*(int(part) for part in match.groups()))
        except ValueError:
            return None
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 8:
        try:
            return datetime.strptime(digits[:8], "%Y%m%d").date()
        except ValueError:
            return None
    for pattern in ("%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def compact_date(value: Optional[date]) -> Any:
    if value is None:
        return NA_VALUE
    return int(value.strftime("%Y%m%d"))


def calculate_period(acquisition_date: date, surgery_date: date) -> str | float:
    difference = (acquisition_date - surgery_date).days
    if difference < 0:
        return "术前"
    if difference == 0:
        return "术中"
    return round(difference / 30, 2)


def numeric_or_text(value: Any) -> Any:
    text = text_value(value)
    if not text:
        return NA_VALUE
    try:
        number = float(text)
    except ValueError:
        return text
    return int(number) if number.is_integer() else number


def normalized_image_type(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return str([str(item) for item in value])
    except TypeError:
        return text_value(value)


def find_node_executable() -> str:
    configured = os.environ.get("DICOM_CENTER_WEB_NODE") or os.environ.get(
        "DICOM_V2_NODE"
    )
    candidates = [
        configured,
        shutil.which("node"),
        r"C:\Users\unionstrong\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    raise RuntimeError(
        "未找到Node.js运行时；可通过DICOM_CENTER_WEB_NODE指定node.exe"
    )


def read_center_workbook(workbook_path: Path) -> list[dict[str, Any]]:
    script_path = Path(__file__).with_name("read_center_workbook.mjs")
    if not script_path.is_file():
        raise RuntimeError(f"缺少分中心Excel读取脚本: {script_path}")
    completed = subprocess.run(
        [find_node_executable(), str(script_path), str(workbook_path)],
        cwd=str(script_path.parent),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=180,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"读取分中心Excel失败: {detail}")
    payload = json.loads(completed.stdout or "[]")
    if not isinstance(payload, list):
        raise RuntimeError("分中心Excel读取结果格式不正确")
    return payload


def header_positions(headers: Sequence[Any], name: str) -> list[int]:
    return [index for index, value in enumerate(headers) if text_value(value) == name]


def center_block_signature(
    records: Sequence[CenterRecord],
) -> list[tuple[str, str, str, str, str, str, str]]:
    return [(*record.signature, str(record.source_order)) for record in records]


def parse_center_sheets(
    sheets: Sequence[dict[str, Any]],
) -> tuple[list[CenterRecord], list[dict[str, Any]]]:
    parsed: list[CenterRecord] = []
    exceptions: list[dict[str, Any]] = []
    for sheet_payload in sheets:
        sheet_name = text_value(sheet_payload.get("name")) or "Sheet"
        values = sheet_payload.get("values") or []
        if not isinstance(values, list):
            continue
        for header_row_index, raw_headers in enumerate(values):
            if not isinstance(raw_headers, list):
                continue
            positions = {
                header: header_positions(raw_headers, header)
                for header in CENTER_REQUIRED_HEADERS
            }
            optional_positions = {
                header: header_positions(raw_headers, header)
                for header in CENTER_OPTIONAL_HEADERS
            }
            block_count = min((len(items) for items in positions.values()), default=0)
            if block_count == 0:
                continue
            order_positions = header_positions(raw_headers, "#")
            blocks: list[list[CenterRecord]] = []
            for block_index in range(block_count):
                center_column = positions["中心名称"][block_index]
                patient_column = positions["姓名"][block_index]
                hospital_column = positions["住院号/门诊号/放射号"][block_index]
                surgery_column = positions["手术日期"][block_index]
                screening_column = (
                    optional_positions["受试者筛选号"][block_index]
                    if block_index < len(optional_positions["受试者筛选号"])
                    else None
                )
                initials_column = (
                    optional_positions["姓名缩写"][block_index]
                    if block_index < len(optional_positions["姓名缩写"])
                    else None
                )
                order_column = (
                    order_positions[block_index]
                    if block_index < len(order_positions)
                    else None
                )
                last_center_name = ""
                block_records: list[CenterRecord] = []
                for row_index in range(header_row_index + 1, len(values)):
                    row = values[row_index]
                    if not isinstance(row, list):
                        continue

                    def cell(column: Optional[int]) -> Any:
                        if column is None or column >= len(row):
                            return ""
                        return row[column]

                    center_name = text_value(cell(center_column)) or last_center_name
                    if center_name:
                        last_center_name = center_name
                    patient_name = text_value(cell(patient_column))
                    hospital_number = normalize_identifier(cell(hospital_column))
                    screening_number = normalize_screening_number(
                        cell(screening_column)
                    )
                    patient_initials = text_value(cell(initials_column))
                    raw_surgery_date = cell(surgery_column)
                    surgery_date = parse_excel_date(raw_surgery_date)
                    raw_order = text_value(cell(order_column))
                    if not any(
                        (
                            center_name,
                            patient_name,
                            hospital_number,
                            screening_number,
                            patient_initials,
                            text_value(raw_surgery_date),
                        )
                    ):
                        continue
                    try:
                        source_order = (
                            int(float(raw_order))
                            if raw_order
                            else row_index - header_row_index
                        )
                    except ValueError:
                        source_order = row_index - header_row_index
                    record = CenterRecord(
                        center_name=center_name,
                        patient_name=patient_name,
                        hospital_number=hospital_number,
                        surgery_date=surgery_date,
                        source_sheet=sheet_name,
                        source_row=row_index + 1,
                        source_order=source_order,
                        screening_number=screening_number,
                        patient_initials=patient_initials,
                    )
                    block_records.append(record)
                blocks.append(block_records)

            if len(blocks) > 1:
                first_signature = center_block_signature(blocks[0])
                for block_number, block in enumerate(blocks[1:], start=2):
                    if center_block_signature(block) != first_signature:
                        raise ValueError(
                            f"{sheet_name} 第{header_row_index + 1}行存在重复字段区域，"
                            f"但第1块与第{block_number}块内容不一致，请先人工核对"
                        )
            parsed.extend(blocks[0])
            break

    unique: list[CenterRecord] = []
    seen: set[tuple[str, str, str, str, str, str]] = set()
    for record in parsed:
        if record.signature in seen:
            continue
        seen.add(record.signature)
        unique.append(record)
        if not record.patient_name:
            detail = (
                "将按受试者筛选号与患者目录编号回退匹配"
                if record.screening_number
                else "该记录无法与DICOM患者自动匹配"
            )
            exceptions.append(
                center_exception(
                    record,
                    "分中心表缺少姓名",
                    detail,
                )
            )
        if not record.hospital_number:
            exceptions.append(
                center_exception(
                    record,
                    "分中心表缺少住院号",
                    "匹配成功后主表住院号仍将留空",
                )
            )
        if record.surgery_date is None:
            exceptions.append(
                center_exception(
                    record,
                    "分中心表手术日期无效",
                    "无法计算时期",
                )
            )
    return unique, exceptions


def center_exception(
    record: CenterRecord, category: str, detail: str
) -> dict[str, Any]:
    return {
        "中心": record.center_name,
        "异常类型": category,
        "患者": record.patient_name or record.patient_initials,
        "住院号": record.hospital_number,
        "StudyInstanceUID": "",
        "SeriesUID": "",
        "SOPUID": "",
        "来源文件或行": f"{record.source_sheet}!{record.source_row}",
        "说明": detail,
    }


def load_center_aliases(path: Optional[Path]) -> dict[str, str]:
    if path is None:
        return {}
    with path.open("r", encoding="utf-8-sig") as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict):
        raise ValueError("中心映射JSON必须是对象，例如 {\"宜昌\": \"医院全称\"}")
    return {text_value(key): text_value(value) for key, value in payload.items()}


def identity_rows_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("rows", "mappings", "patients"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    return []


def load_identity_mapping(
    center_root: Path, transferred_root: Path
) -> dict[str, str]:
    mapping: dict[str, str] = {}
    candidates = [
        transferred_root / ".dicom_v3_state" / "identity_mapping.json",
        center_root / ".dicom_v3_state" / "identity_mapping.json",
    ]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        with candidate.open("r", encoding="utf-8-sig") as stream:
            payload = json.load(stream)
        for row in identity_rows_from_payload(payload):
            canonical_name = text_value(
                row.get("canonical_name")
                or row.get("patient_name")
                or row.get("header_patient_name")
            )
            patient_ids: set[str] = set()
            direct_id = normalize_identifier(row.get("patient_id"))
            if direct_id:
                patient_ids.add(direct_id)
            all_ids = row.get("all_patient_ids")
            if isinstance(all_ids, list):
                patient_ids.update(normalize_identifier(value) for value in all_ids)
            elif all_ids:
                patient_ids.update(
                    normalize_identifier(value)
                    for value in re.split(r"[、,;|]", text_value(all_ids))
                )
            if not canonical_name:
                continue
            for patient_id in patient_ids:
                if patient_id:
                    mapping[patient_id] = canonical_name
    return mapping


def chinese_name_from_relative_path(path: Path, center_root: Path) -> str:
    try:
        parts = path.relative_to(center_root).parts[:-1]
    except ValueError:
        parts = path.parts[:-1]
    for part in parts:
        for candidate in CHINESE_TEXT_PATTERN.findall(part):
            if candidate not in GENERIC_PATIENT_DIRECTORIES:
                return candidate
    return ""


def screening_number_from_relative_path(path: Path, center_root: Path) -> str:
    try:
        relative_parts = path.relative_to(center_root).parts
    except ValueError:
        return ""
    if not relative_parts:
        return ""
    return normalize_screening_number(relative_parts[0])


def iter_center_files(center_root: Path) -> Iterator[Path]:
    for current, dirnames, filenames in os.walk(center_root, topdown=True):
        dirnames[:] = [
            dirname
            for dirname in dirnames
            if dirname not in EXCLUDED_DIRECTORY_NAMES
            and not dirname.startswith(".")
        ]
        current_path = Path(current)
        for filename in filenames:
            path = current_path / filename
            if path.is_file() and path.suffix.casefold() not in SKIPPED_FILE_SUFFIXES:
                yield path


def scan_dicom_file(
    path: Path,
    center_root: Path,
    identity_mapping: dict[str, str],
) -> ScanOutcome:
    relative_path = str(path.relative_to(center_root))
    try:
        with path.open("rb") as stream:
            header = stream.read(132)
            has_prefix = len(header) >= 132 and header[128:132] == b"DICM"
            stream.seek(0)
            dataset = pydicom.dcmread(
                stream,
                force=True,
                stop_before_pixels=True,
                specific_tags=DICOM_TAGS,
            )
    except Exception as exc:
        return ScanOutcome(path=path, is_dicom=False, error=str(exc))

    study_uid = text_value(getattr(dataset, "StudyInstanceUID", ""))
    series_uid = text_value(getattr(dataset, "SeriesInstanceUID", ""))
    sop_uid = text_value(getattr(dataset, "SOPInstanceUID", ""))
    patient_id = normalize_identifier(getattr(dataset, "PatientID", ""))
    header_name = text_value(getattr(dataset, "PatientName", "")).replace("^", " ")
    modality = text_value(getattr(dataset, "Modality", ""))
    if not has_prefix and not any((study_uid, series_uid, sop_uid)):
        supporting = sum(bool(value) for value in (patient_id, header_name, modality))
        if supporting < 2:
            likely_dicom = path.suffix.casefold() in {"", ".dcm", ".dicom", ".ima"}
            return ScanOutcome(
                path=path,
                is_dicom=False,
                error=(
                    "未检测到DICOM前缀、必要UID或足够的患者/模态标签"
                    if likely_dicom
                    else ""
                ),
            )

    patient_name = identity_mapping.get(patient_id) or chinese_name_from_relative_path(
        path, center_root
    ) or header_name
    screening_number = screening_number_from_relative_path(path, center_root)
    acquisition_raw = text_value(getattr(dataset, "AcquisitionDate", ""))
    study_date_raw = text_value(getattr(dataset, "StudyDate", ""))
    series_date_raw = text_value(getattr(dataset, "SeriesDate", ""))
    frames = numeric_or_text(getattr(dataset, "NumberOfFrames", ""))
    number_of_frames = frames if isinstance(frames, int) and frames > 0 else None
    if isinstance(frames, float) and frames.is_integer() and frames > 0:
        number_of_frames = int(frames)
    record = DicomRecord(
        path=path,
        relative_path=relative_path,
        patient_id=patient_id,
        header_patient_name=header_name,
        patient_name=patient_name,
        study_uid=study_uid,
        series_uid=series_uid,
        sop_uid=sop_uid,
        acquisition_date=parse_excel_date(acquisition_raw),
        acquisition_date_raw=acquisition_raw,
        study_date=parse_excel_date(study_date_raw),
        series_date=parse_excel_date(series_date_raw),
        image_type=normalized_image_type(getattr(dataset, "ImageType", "")),
        series_description=text_value(getattr(dataset, "SeriesDescription", "")),
        slice_thickness=numeric_or_text(getattr(dataset, "SliceThickness", "")),
        number_of_frames=number_of_frames,
        modality=text_value(getattr(dataset, "Modality", "")).upper(),
        primary_angle=numeric_or_text(
            getattr(dataset, "PositionerPrimaryAngle", "")
        ),
        secondary_angle=numeric_or_text(
            getattr(dataset, "PositionerSecondaryAngle", "")
        ),
        screening_number=screening_number,
    )
    return ScanOutcome(path=path, is_dicom=True, record=record)


def scan_center(
    center_root: Path,
    transferred_root: Path,
    workers: int,
    center_label: str = "",
) -> tuple[list[DicomRecord], list[dict[str, Any]], dict[str, int]]:
    identity_mapping = load_identity_mapping(center_root, transferred_root)
    files = list(iter_center_files(center_root))
    outcomes: list[ScanOutcome] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        outcomes.extend(
            executor.map(
                lambda path: scan_dicom_file(path, center_root, identity_mapping),
                files,
            )
        )
    records: list[DicomRecord] = []
    exceptions: list[dict[str, Any]] = []
    counters = {
        "files_seen": len(files),
        "dicom": 0,
        "non_dicom": 0,
        "scan_errors": 0,
    }
    for outcome in outcomes:
        if outcome.record is not None:
            counters["dicom"] += 1
            records.append(outcome.record)
            continue
        counters["non_dicom"] += 1
        if outcome.error:
            counters["scan_errors"] += 1
            if outcome.path.suffix.casefold() in {"", ".dcm", ".dicom", ".ima"}:
                exceptions.append(
                    make_exception(
                        center_label or center_root.name,
                        "DICOM读取失败",
                        outcome.error,
                        source=str(outcome.path.relative_to(center_root)),
                    )
                )
    return records, exceptions, counters


def make_exception(
    center: str,
    category: str,
    detail: str,
    *,
    patient: str = "",
    hospital_number: str = "",
    study_uid: str = "",
    series_uid: str = "",
    sop_uid: str = "",
    source: str = "",
) -> dict[str, Any]:
    return {
        "中心": center,
        "异常类型": category,
        "患者": patient,
        "住院号": hospital_number,
        "StudyInstanceUID": study_uid,
        "SeriesUID": series_uid,
        "SOPUID": sop_uid,
        "来源文件或行": source,
        "说明": detail,
    }


def resolve_reference_center(
    folder_name: str,
    center_records: Sequence[CenterRecord],
    aliases: dict[str, str],
    patient_names: Iterable[str],
) -> tuple[str, str]:
    known = sorted(
        {record.center_name for record in center_records if record.center_name},
        key=normalize_center_name,
    )
    normalized_known = defaultdict(list)
    for center_name in known:
        normalized_known[normalize_center_name(center_name)].append(center_name)

    explicit = aliases.get(folder_name)
    if explicit:
        matches = normalized_known.get(normalize_center_name(explicit), [])
        if len(matches) != 1:
            raise ValueError(
                f"中心映射 {folder_name} -> {explicit} 在分中心表中不是唯一中心"
            )
        return matches[0], "显式中心映射"

    exact_matches = normalized_known.get(normalize_center_name(folder_name), [])
    if len(exact_matches) == 1:
        return exact_matches[0], "中心名称精确匹配"

    folder_key = normalize_center_name(folder_name)
    contains_matches = [
        center_name
        for center_name in known
        if len(folder_key) >= 2
        and folder_key in normalize_center_name(center_name)
    ]
    if len(contains_matches) == 1:
        return contains_matches[0], "中心目录简称唯一包含匹配"

    if len(known) == 1:
        return known[0], "分中心表仅有一个中心"

    normalized_patients = {
        normalize_patient_name(value) for value in patient_names if text_value(value)
    }
    overlap: list[tuple[int, str]] = []
    for center_name in known:
        reference_names = {
            normalize_patient_name(record.patient_name)
            for record in center_records
            if normalize_center_name(record.center_name)
            == normalize_center_name(center_name)
            and record.patient_name
        }
        overlap.append((len(normalized_patients & reference_names), center_name))
    overlap.sort(reverse=True)
    if overlap and overlap[0][0] >= 2 and (
        len(overlap) == 1 or overlap[0][0] > overlap[1][0]
    ):
        return overlap[0][1], f"患者姓名集合唯一匹配({overlap[0][0]}人)"
    return "", "无法安全确定中心全称"


def center_record_index(
    records: Sequence[CenterRecord], reference_center_name: str
) -> dict[str, list[CenterRecord]]:
    result: dict[str, list[CenterRecord]] = defaultdict(list)
    center_key = normalize_center_name(reference_center_name)
    for record in records:
        if normalize_center_name(record.center_name) != center_key:
            continue
        name_key = normalize_patient_name(record.patient_name)
        if name_key:
            result[name_key].append(record)
    return result


def center_screening_index(
    records: Sequence[CenterRecord], reference_center_name: str
) -> dict[str, list[CenterRecord]]:
    result: dict[str, list[CenterRecord]] = defaultdict(list)
    center_key = normalize_center_name(reference_center_name)
    for record in records:
        if normalize_center_name(record.center_name) != center_key:
            continue
        screening_key = normalize_screening_number(record.screening_number)
        if screening_key:
            result[screening_key].append(record)
    return result


def unique_center_match(
    candidates: Sequence[CenterRecord], missing_reason: str, ambiguous_reason: str
) -> tuple[Optional[CenterRecord], str]:
    if not candidates:
        return None, missing_reason
    signatures = {
        (
            record.hospital_number,
            record.surgery_date.isoformat() if record.surgery_date else "",
            record.patient_initials,
        )
        for record in candidates
    }
    if len(signatures) > 1:
        return None, ambiguous_reason
    return candidates[0], "唯一匹配"


def match_center_record(
    index: dict[str, list[CenterRecord]],
    patient_name: str,
    screening_index: Optional[dict[str, list[CenterRecord]]] = None,
    screening_number: str = "",
) -> tuple[Optional[CenterRecord], str]:
    candidates = index.get(normalize_patient_name(patient_name), [])
    match, reason = unique_center_match(
        candidates,
        "未在当前中心找到姓名",
        "当前中心存在同名但住院号、手术日期或姓名缩写不同",
    )
    if match is not None:
        return match, "姓名唯一匹配"
    if candidates:
        return None, reason

    screening_key = normalize_screening_number(screening_number)
    screening_candidates = (
        (screening_index or {}).get(screening_key, []) if screening_key else []
    )
    screening_match, screening_reason = unique_center_match(
        screening_candidates,
        "未在当前中心找到姓名或受试者筛选号",
        "当前中心存在相同受试者筛选号但记录内容不同",
    )
    if screening_match is not None:
        return screening_match, "受试者筛选号唯一匹配"
    return None, screening_reason


def distinct_non_na(values: Iterable[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        if value in (None, "", NA_VALUE):
            continue
        marker = text_value(value)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(value)
    return result


def stable_value(
    records: Sequence[DicomRecord],
    attribute: str,
    *,
    join_multiple: bool = True,
) -> Any:
    values = distinct_non_na(getattr(record, attribute) for record in records)
    if not values:
        return NA_VALUE
    if len(values) == 1:
        return values[0]
    if not join_multiple:
        return NA_VALUE
    return " | ".join(text_value(value) for value in values)


def choose_patient_name(records: Sequence[DicomRecord]) -> tuple[str, set[str]]:
    names = [text_value(record.patient_name) for record in records if record.patient_name]
    if not names:
        return "", set()
    counts = Counter(normalize_patient_name(value) for value in names)
    chosen_key = sorted(counts, key=lambda key: (-counts[key], key))[0]
    chosen = next(value for value in names if normalize_patient_name(value) == chosen_key)
    return chosen, set(counts)


def choose_screening_number(
    records: Sequence[DicomRecord],
) -> tuple[str, set[str]]:
    values = [
        normalize_screening_number(record.screening_number)
        for record in records
        if normalize_screening_number(record.screening_number)
    ]
    if not values:
        return "", set()
    counts = Counter(values)
    chosen = sorted(counts, key=lambda key: (-counts[key], key))[0]
    return chosen, set(counts)


def patient_display_value(
    patient_name: str,
    center_match: Optional[CenterRecord],
    screening_number: str,
) -> str:
    if center_match is not None:
        return (
            center_match.patient_name
            or center_match.patient_initials
            or patient_name
            or screening_number
        )
    return patient_name or screening_number


def imaging_date_for_records(
    records: Sequence[DicomRecord],
) -> tuple[Optional[date], str, set[date]]:
    for attribute, source_name in (
        ("acquisition_date", "AcquisitionDate"),
        ("study_date", "StudyDate"),
        ("series_date", "SeriesDate"),
    ):
        dates = {
            value
            for record in records
            if (value := getattr(record, attribute)) is not None
        }
        if dates:
            return min(dates), source_name, dates
    return None, "", set()


def base_web_row(
    records: Sequence[DicomRecord],
    patient_name: str,
    center_match: Optional[CenterRecord],
    imaging_date: Optional[date],
    screening_number: str = "",
) -> dict[str, Any]:
    hospital_number = center_match.hospital_number if center_match else ""
    period: Any = ""
    if imaging_date and center_match and center_match.surgery_date:
        period = calculate_period(imaging_date, center_match.surgery_date)
    return {
        "住院号": hospital_number,
        "患者": patient_display_value(patient_name, center_match, screening_number),
        "StudyInstanceUID": records[0].study_uid,
        "SeriesUID": records[0].series_uid,
        "SOPUID": NA_VALUE,
        "AcqusitionDate": compact_date(imaging_date),
        "时期": period,
        "影像类型": stable_value(records, "image_type"),
        "序列描述": stable_value(records, "series_description"),
        "SliceThickness": stable_value(records, "slice_thickness"),
        "NumberOfFrames": NA_VALUE,
        "帧数": NA_VALUE,
        "modality": stable_value(records, "modality"),
        "第一拍摄角度": stable_value(
            records, "primary_angle", join_multiple=False
        ),
        "第二拍摄角度": stable_value(
            records, "secondary_angle", join_multiple=False
        ),
        "_matched": center_match is not None,
    }


def summarize_series_group(
    center_sheet_name: str,
    records: Sequence[DicomRecord],
    reference_index: dict[str, list[CenterRecord]],
    reference_screening_index: Optional[dict[str, list[CenterRecord]]] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    exceptions: list[dict[str, Any]] = []
    patient_name, patient_name_keys = choose_patient_name(records)
    screening_number, screening_number_keys = choose_screening_number(records)
    study_uid = records[0].study_uid
    series_uid = records[0].series_uid
    ordered_records = sorted(records, key=lambda record: record.relative_path)
    source_paths = ordered_records[0].relative_path
    if len(ordered_records) > 1:
        source_paths += f" | ...共{len(ordered_records)}个文件"

    if len(patient_name_keys) > 1:
        exceptions.append(
            make_exception(
                center_sheet_name,
                "同一Series患者姓名不一致",
                "已按出现次数最多的规范姓名生成结果，请人工核对",
                patient=patient_name,
                study_uid=study_uid,
                series_uid=series_uid,
                source=source_paths,
            )
        )
    if len(screening_number_keys) > 1:
        exceptions.append(
            make_exception(
                center_sheet_name,
                "同一Series受试者筛选号不一致",
                "已按出现次数最多的目录筛选号尝试匹配，请人工核对",
                patient=patient_name,
                study_uid=study_uid,
                series_uid=series_uid,
                source=source_paths,
            )
        )
    patient_ids = {record.patient_id for record in records if record.patient_id}
    if len(patient_ids) > 1:
        exceptions.append(
            make_exception(
                center_sheet_name,
                "同一Series出现多个PatientID",
                "未自动合并患者身份，仅按当前Series生成结果",
                patient=patient_name,
                study_uid=study_uid,
                series_uid=series_uid,
                source=source_paths,
            )
        )

    sop_counts = Counter(record.sop_uid for record in records if record.sop_uid)
    repeated_sops = sorted(sop_uid for sop_uid, count in sop_counts.items() if count > 1)
    if repeated_sops:
        exceptions.append(
            make_exception(
                center_sheet_name,
                "同一SOPUID出现多个文件",
                "主表按一个逻辑SOP计数；请核对是否为重复文件或保留的UID内容冲突",
                patient=patient_name,
                study_uid=study_uid,
                series_uid=series_uid,
                sop_uid=" | ".join(repeated_sops[:3]),
                source=source_paths,
            )
        )

    center_match, match_reason = match_center_record(
        reference_index,
        patient_name,
        reference_screening_index,
        screening_number,
    )
    display_patient = patient_display_value(
        patient_name, center_match, screening_number
    )
    if center_match is None:
        exceptions.append(
            make_exception(
                center_sheet_name,
                "患者匹配失败",
                match_reason,
                patient=patient_name,
                study_uid=study_uid,
                series_uid=series_uid,
                source=source_paths,
            )
        )
    elif match_reason == "受试者筛选号唯一匹配":
        exceptions.append(
            make_exception(
                center_sheet_name,
                "编号回退匹配",
                f"目录筛选号{screening_number}与分中心表受试者筛选号唯一匹配；"
                "患者字段使用姓名或姓名缩写，住院号不回填筛选号",
                patient=display_patient,
                hospital_number=center_match.hospital_number,
                study_uid=study_uid,
                series_uid=series_uid,
                source=f"{center_match.source_sheet}!{center_match.source_row}",
            )
        )

    if center_match is not None and not center_match.hospital_number:
        exceptions.append(
            make_exception(
                center_sheet_name,
                "匹配记录缺少住院号",
                "主表住院号留空",
                patient=display_patient,
                study_uid=study_uid,
                series_uid=series_uid,
                source=f"{center_match.source_sheet}!{center_match.source_row}",
            )
        )
    if center_match is not None and center_match.surgery_date is None:
        exceptions.append(
            make_exception(
                center_sheet_name,
                "匹配记录缺少有效手术日期",
                "主表时期留空",
                patient=display_patient,
                hospital_number=center_match.hospital_number,
                study_uid=study_uid,
                series_uid=series_uid,
                source=f"{center_match.source_sheet}!{center_match.source_row}",
            )
        )

    multiframe = [record for record in records if record.is_multiframe]
    single_frame = [record for record in records if not record.is_multiframe]
    if multiframe and single_frame:
        exceptions.append(
            make_exception(
                center_sheet_name,
                "同一Series混合单帧与多帧",
                "单帧部分汇总一行，多帧部分按SOP分别输出",
                patient=patient_name,
                hospital_number=center_match.hospital_number if center_match else "",
                study_uid=study_uid,
                series_uid=series_uid,
                source=source_paths,
            )
        )

    output_rows: list[dict[str, Any]] = []
    if single_frame:
        imaging_date, date_source, all_dates = imaging_date_for_records(single_frame)
        if imaging_date is None:
            exceptions.append(
                make_exception(
                    center_sheet_name,
                    "缺少影像日期",
                    "AcquisitionDate、StudyDate和SeriesDate均缺失，时期留空",
                    patient=patient_name,
                    hospital_number=center_match.hospital_number if center_match else "",
                    study_uid=study_uid,
                    series_uid=series_uid,
                    source=source_paths,
                )
            )
        elif date_source != "AcquisitionDate":
            exceptions.append(
                make_exception(
                    center_sheet_name,
                    "日期回退",
                    f"AcquisitionDate缺失，使用{date_source}="
                    + imaging_date.strftime("%Y%m%d"),
                    patient=patient_name,
                    hospital_number=center_match.hospital_number if center_match else "",
                    study_uid=study_uid,
                    series_uid=series_uid,
                    source=source_paths,
                )
            )
        if imaging_date is not None and len(all_dates) > 1:
            exceptions.append(
                make_exception(
                    center_sheet_name,
                    f"同一Series存在多个{date_source}",
                    "主表使用最早日期；全部日期="
                    + ",".join(sorted(value.strftime("%Y%m%d") for value in all_dates)),
                    patient=patient_name,
                    hospital_number=center_match.hospital_number if center_match else "",
                    study_uid=study_uid,
                    series_uid=series_uid,
                    source=source_paths,
                )
            )
        unique_sops = {record.sop_uid for record in single_frame if record.sop_uid}
        row = base_web_row(
            single_frame,
            patient_name,
            center_match,
            imaging_date,
            screening_number,
        )
        row["帧数"] = len(unique_sops) if unique_sops else len(single_frame)
        output_rows.append(row)

    multiframe_by_sop: dict[str, list[DicomRecord]] = defaultdict(list)
    for record in multiframe:
        key = record.sop_uid or f"MISSING:{record.relative_path}"
        multiframe_by_sop[key].append(record)
    for sop_key in sorted(multiframe_by_sop):
        sop_records = multiframe_by_sop[sop_key]
        imaging_date, date_source, all_dates = imaging_date_for_records(sop_records)
        if imaging_date is None:
            exceptions.append(
                make_exception(
                    center_sheet_name,
                    "缺少影像日期",
                    "AcquisitionDate、StudyDate和SeriesDate均缺失，时期留空",
                    patient=patient_name,
                    hospital_number=center_match.hospital_number if center_match else "",
                    study_uid=study_uid,
                    series_uid=series_uid,
                    sop_uid=sop_records[0].sop_uid,
                    source=sop_records[0].relative_path,
                )
            )
        elif date_source != "AcquisitionDate":
            exceptions.append(
                make_exception(
                    center_sheet_name,
                    "日期回退",
                    f"AcquisitionDate缺失，使用{date_source}="
                    + imaging_date.strftime("%Y%m%d"),
                    patient=patient_name,
                    hospital_number=center_match.hospital_number if center_match else "",
                    study_uid=study_uid,
                    series_uid=series_uid,
                    sop_uid=sop_records[0].sop_uid,
                    source=sop_records[0].relative_path,
                )
            )
        row = base_web_row(
            sop_records,
            patient_name,
            center_match,
            imaging_date,
            screening_number,
        )
        row["SOPUID"] = sop_records[0].sop_uid or NA_VALUE
        row["NumberOfFrames"] = sop_records[0].number_of_frames or NA_VALUE
        row["帧数"] = NA_VALUE
        output_rows.append(row)
        if imaging_date is not None and len(all_dates) > 1:
            exceptions.append(
                make_exception(
                    center_sheet_name,
                    f"同一SOP存在多个{date_source}",
                    "主表使用最早日期；全部日期="
                    + ",".join(sorted(value.strftime("%Y%m%d") for value in all_dates)),
                    patient=patient_name,
                    hospital_number=center_match.hospital_number if center_match else "",
                    study_uid=study_uid,
                    series_uid=series_uid,
                    sop_uid=sop_records[0].sop_uid,
                    source=sop_records[0].relative_path,
                )
            )

    return output_rows, exceptions


def process_center_records(
    sheet_name: str,
    source_path: Path,
    records: Sequence[DicomRecord],
    center_records: Sequence[CenterRecord],
    reference_center_name: str,
    initial_exceptions: Sequence[dict[str, Any]],
    scan_counters: dict[str, int],
) -> CenterResult:
    result = CenterResult(
        sheet_name=sheet_name,
        source_center_name=sheet_name,
        reference_center_name=reference_center_name,
        source_path=str(source_path),
        exceptions=list(initial_exceptions),
        counters=dict(scan_counters),
    )
    valid: list[DicomRecord] = []
    for record in records:
        missing = [
            field_name
            for field_name, value in (
                ("StudyInstanceUID", record.study_uid),
                ("SeriesInstanceUID", record.series_uid),
                ("SOPInstanceUID", record.sop_uid),
            )
            if not value
        ]
        if missing:
            result.exceptions.append(
                make_exception(
                    sheet_name,
                    "DICOM缺少必要UID",
                    "缺少=" + ",".join(missing) + "；该文件不进入主表",
                    patient=record.patient_name,
                    study_uid=record.study_uid,
                    series_uid=record.series_uid,
                    sop_uid=record.sop_uid,
                    source=record.relative_path,
                )
            )
            continue
        valid.append(record)

    reference_index = center_record_index(center_records, reference_center_name)
    reference_screening_index = center_screening_index(
        center_records, reference_center_name
    )
    groups: dict[tuple[str, str], list[DicomRecord]] = defaultdict(list)
    for record in valid:
        groups[(record.study_uid, record.series_uid)].append(record)
    for group_key in sorted(groups):
        rows, exceptions = summarize_series_group(
            sheet_name,
            groups[group_key],
            reference_index,
            reference_screening_index,
        )
        result.rows.extend(rows)
        result.exceptions.extend(exceptions)

    reference_order = {
        normalize_patient_name(
            record.patient_name
            or record.patient_initials
            or record.screening_number
        ): record.source_order
        for record in center_records
        if normalize_center_name(record.center_name)
        == normalize_center_name(reference_center_name)
        and (record.patient_name or record.patient_initials or record.screening_number)
    }

    def row_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
        patient_key = normalize_patient_name(row.get("患者"))
        acquisition = row.get("AcqusitionDate")
        acquisition_key = acquisition if isinstance(acquisition, int) else 99999999
        return (
            reference_order.get(patient_key, 10**9),
            patient_key,
            acquisition_key,
            text_value(row.get("StudyInstanceUID")),
            text_value(row.get("SeriesUID")),
            text_value(row.get("SOPUID")),
        )

    result.rows.sort(key=row_sort_key)
    result.counters.update(
        {
            "valid_dicom": len(valid),
            "patients": len(
                {normalize_patient_name(record.patient_name) for record in valid}
            ),
            "studies": len({record.study_uid for record in valid}),
            "series": len(groups),
            "output_rows": len(result.rows),
            "exceptions": len(result.exceptions),
            "matched_rows": sum(1 for row in result.rows if row.get("_matched")),
        }
    )
    return result


def safe_sheet_name(value: str, used: set[str]) -> str:
    base = INVALID_SHEET_CHARACTER_PATTERN.sub("_", value).strip(" '") or "中心"
    base = base[:31]
    candidate = base
    suffix = 2
    while candidate.casefold() in used:
        suffix_text = f"_{suffix}"
        candidate = base[: 31 - len(suffix_text)] + suffix_text
        suffix += 1
    used.add(candidate.casefold())
    return candidate


def discover_centers(
    transferred_root: Path, single_center: Optional[str]
) -> list[tuple[str, Path]]:
    if single_center:
        return [(single_center, transferred_root)]
    centers = [
        path
        for path in transferred_root.iterdir()
        if path.is_dir()
        and not path.name.startswith(".")
        and path.name not in EXCLUDED_DIRECTORY_NAMES
    ]
    return [(path.name, path) for path in sorted(centers, key=lambda item: item.name)]


def write_workbook(
    output_path: Path,
    center_results: Sequence[CenterResult],
    center_table_exceptions: Sequence[dict[str, Any]],
    transferred_root: Path,
    center_workbook: Path,
    preview_dir: Optional[Path],
) -> None:
    script_path = Path(__file__).with_name("build_center_image_workbook.mjs")
    if not script_path.is_file():
        raise RuntimeError(f"缺少Excel构建脚本: {script_path}")
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "rule_version": RULE_VERSION,
        "transferred_root": str(transferred_root),
        "center_workbook": str(center_workbook),
        "headers": WEB_HEADERS,
        "centers": [
            {
                "sheet_name": result.sheet_name,
                "source_center_name": result.source_center_name,
                "reference_center_name": result.reference_center_name,
                "source_path": result.source_path,
                "rows": result.rows,
                "counters": result.counters,
            }
            for result in center_results
        ],
        "exceptions": [
            *center_table_exceptions,
            *(
                exception
                for result in center_results
                for exception in result.exceptions
            ),
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            encoding="utf-8",
            delete=False,
            dir=str(output_path.parent),
        ) as temporary:
            json.dump(payload, temporary, ensure_ascii=False)
            temporary_path = Path(temporary.name)
        command = [
            find_node_executable(),
            str(script_path),
            str(temporary_path),
            str(output_path),
        ]
        if preview_dir is not None:
            preview_dir.mkdir(parents=True, exist_ok=True)
            command.append(str(preview_dir))
        completed = subprocess.run(
            command,
            cwd=str(script_path.parent),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=300,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f"Excel生成失败: {detail}")
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()


def analyze(
    transferred_root: Path,
    center_workbook: Path,
    *,
    center_aliases: Optional[dict[str, str]] = None,
    single_center: Optional[str] = None,
    workers: int = 4,
) -> tuple[list[CenterResult], list[dict[str, Any]]]:
    center_sheets = read_center_workbook(center_workbook)
    center_records, center_table_exceptions = parse_center_sheets(center_sheets)
    if not center_records:
        raise ValueError("分中心Excel中没有找到中心名称、姓名、住院号和手术日期字段")
    centers = discover_centers(transferred_root, single_center)
    if not centers:
        raise ValueError("转存根目录下没有可处理的中心目录")

    aliases = center_aliases or {}
    used_sheet_names: set[str] = {"处理异常".casefold(), "运行摘要".casefold(), "字段说明".casefold()}
    results: list[CenterResult] = []
    for folder_name, center_path in centers:
        sheet_name = safe_sheet_name(folder_name, used_sheet_names)
        records, scan_exceptions, scan_counters = scan_center(
            center_path, transferred_root, workers, sheet_name
        )
        reference_center, center_basis = resolve_reference_center(
            folder_name,
            center_records,
            aliases,
            (record.patient_name for record in records),
        )
        initial_exceptions = list(scan_exceptions)
        if not reference_center:
            initial_exceptions.append(
                make_exception(
                    sheet_name,
                    "中心名称匹配失败",
                    center_basis + "；该中心患者住院号和时期将留空",
                    source=str(center_path),
                )
            )
        result = process_center_records(
            sheet_name,
            center_path,
            records,
            center_records,
            reference_center,
            initial_exceptions,
            scan_counters,
        )
        result.counters["center_match_basis"] = center_basis
        results.append(result)
    processed_center_keys = {
        normalize_center_name(result.reference_center_name)
        for result in results
        if result.reference_center_name
    }
    relevant_center_exceptions = [
        exception
        for exception in center_table_exceptions
        if normalize_center_name(exception.get("中心")) in processed_center_keys
    ]
    return results, relevant_center_exceptions


def print_summary(results: Sequence[CenterResult]) -> None:
    print(f"规则版本: {RULE_VERSION}")
    for result in results:
        counters = result.counters
        print(
            f"[{result.sheet_name}] 参考中心={result.reference_center_name or '未匹配'} "
            f"DICOM={counters.get('dicom', 0)} Study={counters.get('studies', 0)} "
            f"Series={counters.get('series', 0)} 输出行={counters.get('output_rows', 0)} "
            f"已匹配行={counters.get('matched_rows', 0)} 异常={counters.get('exceptions', 0)}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="按中心分析已转存DICOM并生成第四批15字段结构的影像-web表"
    )
    parser.add_argument("transferred_root", help="转存后数据根目录；一级目录为中心")
    parser.add_argument("center_workbook", help="分中心影像Excel路径")
    parser.add_argument("--output", help="输出Excel路径；--write时必填")
    parser.add_argument(
        "--center-map",
        help='中心简称映射JSON，例如 {"北医三":"北京大学第三医院"}',
    )
    parser.add_argument(
        "--single-center",
        help="把transferred_root本身作为一个中心处理，并指定输出工作表名",
    )
    parser.add_argument("--workers", type=int, default=4, help="Header读取线程数")
    parser.add_argument(
        "--write",
        action="store_true",
        help="生成Excel；默认仅扫描并打印预览统计",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="允许覆盖明确指定的已有输出Excel",
    )
    parser.add_argument("--preview-dir", help="可选的Excel视觉预览图片目录")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    transferred_root = Path(args.transferred_root).resolve()
    center_workbook = Path(args.center_workbook).resolve()
    if not transferred_root.is_dir():
        raise ValueError(f"转存根目录不存在: {transferred_root}")
    if not center_workbook.is_file():
        raise ValueError(f"分中心Excel不存在: {center_workbook}")
    aliases = load_center_aliases(
        Path(args.center_map).resolve() if args.center_map else None
    )
    results, center_exceptions = analyze(
        transferred_root,
        center_workbook,
        center_aliases=aliases,
        single_center=args.single_center,
        workers=max(1, args.workers),
    )
    print_summary(results)
    print(f"分中心表异常: {len(center_exceptions)}")
    if not args.write:
        print("当前为预览模式，未生成Excel；确认后加 --write --output <路径>")
        return 0
    if not args.output:
        raise ValueError("使用--write时必须指定--output")
    output_path = Path(args.output).resolve()
    if output_path.exists() and not args.overwrite:
        raise FileExistsError(
            f"输出文件已存在，未覆盖: {output_path}；确认后使用--overwrite"
        )
    write_workbook(
        output_path,
        results,
        center_exceptions,
        transferred_root,
        center_workbook,
        Path(args.preview_dir).resolve() if args.preview_dir else None,
    )
    print(f"已生成: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
