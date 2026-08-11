#!/usr/bin/env python3
"""DICOM 原始影像转存与 Study 级检查汇总（无数据库版）。

设计目标：

* 原始 ``dicom_organize.py`` 和原数据库保持不变；
* 只读取满足当前业务需求的 13 个 DICOM 标签；
* 原始文件按患者/Study/Series/SOP 层级安全转存；
* Series 仅在内存中临时分类，最终 Excel 每个 Study 一行；
* 同一 Study 的 XA、CT、MR 合并展示，3D_DSA断层单独标记；
* 不依赖 MySQL，不保存文件级或 Series 级数据库记录。

当前程序生成的是第一阶段 ``影像检查明细.xlsx``。以后取得住院号和
手术时间名单后，可以基于该明细计算术中、术后 6 个月和 12 个月结果。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence

import pydicom
from pydicom.uid import UID


RULE_VERSION = "2026.08-study-v1"
COPY_CHUNK_SIZE = 1024 * 1024

# 最终确认的 13 个字段。不要随意扩充；缺失值通过保守分类和待确认记录处理。
SCAN_TAGS = [
    "PatientName",
    "PatientID",
    "StudyInstanceUID",
    "SeriesInstanceUID",
    "SOPInstanceUID",
    "SOPClassUID",
    "StudyDate",
    "AcquisitionDate",
    "Modality",
    "SeriesDescription",
    "PositionerMotion",
    "NumberOfFrames",
    "SliceThickness",
]

RECON_PATTERN = re.compile(
    r"3D|MIP|MPR|VRT?|RECON|VOLUME|DYNACT|CBCT|XPERCT|断层|重建",
    re.IGNORECASE,
)
MODALITY_ORDER = {"XA": 0, "CT": 1, "MR": 2}
CHINESE_TEXT_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
PATIENT_ID_UUID_SUFFIX_PATTERN = re.compile(
    r"^(?P<patient_id>.+?)!"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
GENERIC_CHINESE_DIRECTORIES = {
    "数据", "原始数据", "影像", "影像数据", "患者", "患者数据", "病例", "病例数据",
    "检查", "检查数据", "资料", "导出", "备份",
}

# 并行复制时，同一个目标 SOP 路径仍必须串行判断重复/冲突，避免竞态覆盖。
_TRANSFER_LOCKS = tuple(threading.Lock() for _ in range(257))


@dataclass
class DicomMeta:
    patient_name: str = ""
    patient_id: str = ""
    study_uid: str = ""
    series_uid: str = ""
    sop_uid: str = ""
    sop_class_uid: str = ""
    study_date: str = ""
    acquisition_date: str = ""
    modality: str = ""
    series_description: str = ""
    positioner_motion: str = ""
    number_of_frames: Optional[int] = None
    slice_thickness: str = ""

    @property
    def exam_date(self) -> str:
        return normalize_dicom_date(self.study_date) or normalize_dicom_date(
            self.acquisition_date
        )


@dataclass
class ScanResult:
    path: Path
    relative_path: str
    relative_path_hash: str
    is_dicom: bool
    meta: Optional[DicomMeta] = None
    error: str = ""


@dataclass
class TransferResult:
    scan: ScanResult
    destination_path: str = ""
    file_size: int = 0
    sha256: str = ""
    status: str = ""
    message: str = ""


@dataclass
class SeriesAccumulator:
    series_uid: str
    modalities: set[str] = field(default_factory=set)
    sop_class_uids: set[str] = field(default_factory=set)
    descriptions: set[str] = field(default_factory=set)
    motions: set[str] = field(default_factory=set)
    max_frames: int = 1
    has_slice_thickness: bool = False
    sop_uids: set[str] = field(default_factory=set)

    def add(self, meta: DicomMeta) -> None:
        if meta.modality:
            self.modalities.add(normalize_modality(meta.modality))
        if meta.sop_class_uid:
            self.sop_class_uids.add(meta.sop_class_uid)
        if meta.series_description:
            self.descriptions.add(meta.series_description)
        if meta.positioner_motion:
            self.motions.add(meta.positioner_motion.upper())
        self.max_frames = max(self.max_frames, meta.number_of_frames or 1)
        self.has_slice_thickness = self.has_slice_thickness or bool(
            meta.slice_thickness
        )
        if meta.sop_uid:
            self.sop_uids.add(meta.sop_uid)


@dataclass
class StudyAccumulator:
    study_uid: str
    patient_ids: set[str] = field(default_factory=set)
    patient_names: set[str] = field(default_factory=set)
    exam_dates: set[str] = field(default_factory=set)
    series: dict[str, SeriesAccumulator] = field(default_factory=dict)
    destination_dir: str = ""

    def add(self, meta: DicomMeta, destination_path: str) -> None:
        if meta.patient_id:
            self.patient_ids.add(meta.patient_id)
        if meta.patient_name:
            self.patient_names.add(meta.patient_name)
        if meta.exam_date:
            self.exam_dates.add(meta.exam_date)
        series = self.series.setdefault(
            meta.series_uid, SeriesAccumulator(series_uid=meta.series_uid)
        )
        series.add(meta)
        if destination_path and not self.destination_dir:
            destination = Path(destination_path)
            try:
                self.destination_dir = str(destination.parents[1])
            except IndexError:
                self.destination_dir = str(destination.parent)


def text_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def int_value(value: Any) -> Optional[int]:
    try:
        return int(str(value)) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def normalize_modality(value: str) -> str:
    modality = (value or "").strip().upper()
    if modality == "MRI":
        return "MR"
    return modality


def normalize_dicom_date(value: str) -> str:
    digits = re.sub(r"[^0-9]", "", value or "")
    if len(digits) < 8:
        return ""
    try:
        parsed = datetime.strptime(digits[:8], "%Y%m%d")
    except ValueError:
        return ""
    return parsed.strftime("%Y-%m-%d")


def normalize_patient_id(value: Any) -> str:
    """移除设备偶发追加的 ``!UUID``，其他 PatientID 保持原样。"""
    patient_id = text_value(value)
    match = PATIENT_ID_UUID_SUFFIX_PATTERN.fullmatch(patient_id)
    if match:
        return match.group("patient_id").strip()
    return patient_id


def sanitize_component(value: str, fallback: str = "UNKNOWN") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value or "")
    cleaned = cleaned.strip().rstrip(". ")
    if cleaned.upper() in {
        "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4",
        "COM5", "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3",
        "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    }:
        cleaned = "_" + cleaned
    return (cleaned or fallback)[:180]


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()


def patient_folder(meta: DicomMeta) -> str:
    patient_name = meta.patient_name.replace("^", " ").strip() or "Unknown"
    patient_id = meta.patient_id or "NoID"
    return sanitize_component(f"{patient_name}_{patient_id}")


def chinese_name_from_component(value: str) -> str:
    """从 ``昌永杰_1033475_1`` 这类目录名中提取中文姓名。"""
    matches = CHINESE_TEXT_PATTERN.findall(value or "")
    if not matches:
        return ""
    candidate = matches[0].strip()
    if candidate in GENERIC_CHINESE_DIRECTORIES:
        return ""
    return candidate


def resolve_patient_name(
    path: Path,
    source_root: Path,
    patient_id: str,
    dicom_patient_name: str,
) -> str:
    """目录中文姓名优先，DICOM PatientName 仅作回退。

    批量处理时优先选择包含 PatientID 的上级目录；处理单个患者目录时，
    也会识别源目录本身（例如 ``.../昌永杰``）。
    """
    candidates: list[tuple[int, str]] = []
    try:
        parent_parts = path.relative_to(source_root).parent.parts
    except ValueError:
        parent_parts = path.parent.parts

    normalized_id = (patient_id or "").strip().casefold()
    for index, component in enumerate(parent_parts):
        chinese_name = chinese_name_from_component(component)
        if not chinese_name:
            continue
        score = 80 - min(index, 40)
        if normalized_id and normalized_id in component.casefold():
            score += 200
        if 2 <= len(chinese_name) <= 6:
            score += 20
        candidates.append((score, chinese_name))

    root_component = source_root.name
    root_name = chinese_name_from_component(root_component)
    if root_name:
        score = 100
        if normalized_id and normalized_id in root_component.casefold():
            score += 200
        if 2 <= len(root_name) <= 6:
            score += 20
        candidates.append((score, root_name))

    if candidates:
        return max(candidates, key=lambda item: item[0])[1]
    return (dicom_patient_name or "").replace("^", " ").strip()


def has_dicom_prefix(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            header = stream.read(132)
        return len(header) >= 132 and header[128:132] == b"DICM"
    except OSError:
        return False


def scan_one_file(path: Path, source_root: Path) -> ScanResult:
    relative_path = str(path.relative_to(source_root))
    relative_path_hash = stable_hash(relative_path.replace("\\", "/"))
    try:
        # 同一个文件只打开一次：先看 DICM 前缀，再从同一文件句柄读取标签。
        with path.open("rb") as stream:
            header = stream.read(132)
            prefix = len(header) >= 132 and header[128:132] == b"DICM"
            stream.seek(0)
            dataset = pydicom.dcmread(
                stream,
                force=True,
                stop_before_pixels=True,
                specific_tags=SCAN_TAGS,
            )
    except Exception as exc:
        return ScanResult(
            path,
            relative_path,
            relative_path_hash,
            locals().get("prefix", False),
            error=str(exc),
        )

    uid_values = [
        getattr(dataset, "SOPClassUID", None),
        getattr(dataset, "SOPInstanceUID", None),
        getattr(dataset, "StudyInstanceUID", None),
        getattr(dataset, "SeriesInstanceUID", None),
    ]
    supporting_values = [
        getattr(dataset, "PatientID", None),
        getattr(dataset, "PatientName", None),
        getattr(dataset, "Modality", None),
        getattr(dataset, "StudyDate", None),
    ]
    if (
        not prefix
        and not any(text_value(value) for value in uid_values)
        and sum(bool(text_value(value)) for value in supporting_values) < 3
    ):
        return ScanResult(path, relative_path, relative_path_hash, False)

    patient_id = normalize_patient_id(getattr(dataset, "PatientID", ""))
    dicom_patient_name = text_value(getattr(dataset, "PatientName", ""))
    meta = DicomMeta(
        patient_name=resolve_patient_name(
            path, source_root, patient_id, dicom_patient_name
        ),
        patient_id=patient_id,
        study_uid=text_value(getattr(dataset, "StudyInstanceUID", "")),
        series_uid=text_value(getattr(dataset, "SeriesInstanceUID", "")),
        sop_uid=text_value(getattr(dataset, "SOPInstanceUID", "")),
        sop_class_uid=text_value(getattr(dataset, "SOPClassUID", "")),
        study_date=text_value(getattr(dataset, "StudyDate", "")),
        acquisition_date=text_value(getattr(dataset, "AcquisitionDate", "")),
        modality=normalize_modality(text_value(getattr(dataset, "Modality", ""))),
        series_description=text_value(
            getattr(dataset, "SeriesDescription", "")
        ),
        positioner_motion=text_value(
            getattr(dataset, "PositionerMotion", "")
        ).upper(),
        number_of_frames=int_value(getattr(dataset, "NumberOfFrames", None)),
        slice_thickness=text_value(getattr(dataset, "SliceThickness", "")),
    )
    return ScanResult(
        path, relative_path, relative_path_hash, True, meta=meta
    )


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def iter_source_files(source_root: Path, destination_root: Path) -> Iterator[Path]:
    source_root = source_root.resolve()
    destination_root = destination_root.resolve()
    for current, dirnames, filenames in os.walk(source_root, topdown=True):
        current_path = Path(current)
        retained = []
        for dirname in dirnames:
            child = (current_path / dirname).resolve()
            if child == destination_root or is_relative_to(child, destination_root):
                continue
            retained.append(dirname)
        dirnames[:] = retained
        for filename in filenames:
            path = current_path / filename
            resolved = path.resolve()
            if resolved == destination_root or is_relative_to(resolved, destination_root):
                continue
            if path.is_file():
                yield path


def chunked(values: Iterable[Any], size: int) -> Iterator[list[Any]]:
    batch: list[Any] = []
    for value in values:
        batch.append(value)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(COPY_CHUNK_SIZE)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def unique_conflict_path(target: Path, digest: str) -> Path:
    candidate = target.with_name(
        f"{target.stem}.{digest[:12]}.conflict{target.suffix}"
    )
    if not candidate.exists():
        return candidate
    return target.with_name(
        f"{target.stem}.{digest[:12]}.{uuid.uuid4().hex[:8]}.conflict{target.suffix}"
    )


def copy_atomic_with_hash(
    source: Path,
    target: Path,
    *,
    durable: bool = False,
) -> tuple[str, Path, int, str]:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.part"
    digest = hashlib.sha256()
    size = 0
    try:
        with source.open("rb") as input_stream, temporary.open("xb") as output_stream:
            while True:
                block = input_stream.read(COPY_CHUNK_SIZE)
                if not block:
                    break
                output_stream.write(block)
                digest.update(block)
                size += len(block)
            output_stream.flush()
            if durable:
                os.fsync(output_stream.fileno())
        source_size = source.stat().st_size
        if source_size != size:
            raise IOError(f"复制大小不一致: source={source_size}, copied={size}")
        if durable:
            try:
                shutil.copystat(source, temporary)
            except OSError:
                pass

        sha256 = digest.hexdigest()
        if target.exists():
            if target.stat().st_size == size and hash_file(target) == sha256:
                temporary.unlink()
                return "duplicate_same", target, size, sha256
            conflict = unique_conflict_path(target, sha256)
            os.replace(temporary, conflict)
            return "conflict", conflict, size, sha256
        os.replace(temporary, target)
        return "copied", target, size, sha256
    finally:
        if temporary.exists():
            temporary.unlink()


def destination_for(scan: ScanResult, destination_root: Path) -> tuple[Path, bool, str]:
    meta = scan.meta
    if meta is None:
        target = (
            destination_root
            / "_quarantine"
            / "unreadable"
            / f"{scan.relative_path_hash}.dcm"
        )
        return target, True, scan.error or "DICOM 文件无法解析"
    missing = [
        name
        for name, value in (
            ("StudyInstanceUID", meta.study_uid),
            ("SeriesInstanceUID", meta.series_uid),
            ("SOPInstanceUID", meta.sop_uid),
        )
        if not value
    ]
    if missing:
        target = (
            destination_root
            / "_quarantine"
            / "missing_required_uids"
            / f"{scan.relative_path_hash}.dcm"
        )
        return target, True, "缺少必要标签: " + ", ".join(missing)
    target = (
        destination_root
        / patient_folder(meta)
        / sanitize_component(meta.study_uid)
        / sanitize_component(meta.series_uid)
        / f"{sanitize_component(meta.sop_uid)}.dcm"
    )
    return target, False, ""


def transfer_one(
    scan: ScanResult,
    destination_root: Path,
    *,
    durable: bool = False,
) -> TransferResult:
    target, quarantined, reason = destination_for(scan, destination_root)
    try:
        target_lock = _TRANSFER_LOCKS[hash(str(target).casefold()) % len(_TRANSFER_LOCKS)]
        with target_lock:
            status, actual_target, size, digest = copy_atomic_with_hash(
                scan.path, target, durable=durable
            )
        if quarantined and status != "conflict":
            status = "quarantined"
        return TransferResult(
            scan=scan,
            destination_path=str(actual_target),
            file_size=size,
            sha256=digest,
            status=status,
            message=reason,
        )
    except Exception as exc:
        return TransferResult(
            scan=scan,
            destination_path=str(target),
            status="error",
            message=str(exc),
        )


def sop_class_name(uid_value: str) -> str:
    if not uid_value:
        return ""
    try:
        return UID(uid_value).name
    except Exception:
        return ""


def classify_series(series: SeriesAccumulator) -> dict[str, Any]:
    modalities = sorted_modalities(series.modalities)
    descriptions = " | ".join(sorted(series.descriptions))
    sop_names = sorted(
        filter(None, (sop_class_name(uid) for uid in series.sop_class_uids))
    )
    evidence: list[str] = []
    is_3d = False
    confidence = 0.0

    if any("X-Ray 3D" in name for name in sop_names):
        is_3d = True
        confidence = 0.98
        evidence.append("SOPClass=X-Ray 3D")
    elif (
        "XA" in series.modalities
        and "DYNAMIC" in series.motions
        and series.max_frames > 1
    ):
        is_3d = True
        confidence = 0.88
        evidence.extend(
            [
                "Modality=XA",
                "PositionerMotion=DYNAMIC",
                f"NumberOfFrames={series.max_frames}",
            ]
        )
    elif (
        "XA" in series.modalities
        and series.has_slice_thickness
        and bool(RECON_PATTERN.search(descriptions))
    ):
        is_3d = True
        confidence = 0.82
        evidence.extend(
            [
                "Modality=XA",
                "SliceThickness存在",
                "SeriesDescription包含3D/重建关键词",
            ]
        )
    elif "XA" in series.modalities:
        confidence = 0.72
        evidence.append("XA序列未检出3D证据")
    else:
        confidence = 0.99 if set(modalities).issubset({"CT", "MR"}) else 0.60
        evidence.append("按Modality识别")

    return {
        "series_uid": series.series_uid,
        "modalities": modalities,
        "is_3d": is_3d,
        "confidence": round(confidence, 2),
        "evidence": evidence,
        "description": descriptions,
        "file_count": len(series.sop_uids),
    }


def sorted_modalities(modalities: Iterable[str]) -> list[str]:
    values = {value for value in modalities if value}
    return sorted(values, key=lambda value: (MODALITY_ORDER.get(value, 99), value))


def join_values(values: Iterable[str]) -> str:
    return "、".join(value for value in values if value)


def summarize_study(
    study: StudyAccumulator,
    source_root: Path,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    series_summaries = [
        classify_series(series)
        for _, series in sorted(study.series.items(), key=lambda item: item[0])
    ]
    modalities = sorted_modalities(
        modality
        for summary in series_summaries
        for modality in summary["modalities"]
    )
    has_3d = any(summary["is_3d"] for summary in series_summaries)
    modality_text = join_values(modalities) or "其他/未知"
    display_type = (
        f"{modality_text}（含3D_DSA断层）" if has_3d else modality_text
    )
    three_d_confidences = [
        summary["confidence"] for summary in series_summaries if summary["is_3d"]
    ]
    confidence = (
        max(three_d_confidences)
        if three_d_confidences
        else min(
            (summary["confidence"] for summary in series_summaries),
            default=0.0,
        )
    )
    evidence = []
    for summary in series_summaries:
        if summary["is_3d"]:
            evidence.append(
                f"Series {summary['series_uid']}: " + "；".join(summary["evidence"])
            )
    if not evidence:
        evidence.append("基础类型按Modality汇总；未检出3D证据")

    patient_id = join_values(sorted(study.patient_ids))
    patient_name = join_values(sorted(study.patient_names))
    exam_dates = sorted(study.exam_dates)
    warnings: list[dict[str, str]] = []
    if len(study.patient_ids) > 1:
        warnings.append(
            exception_record(
                "Study患者ID冲突", patient_id, patient_name, study.study_uid,
                "同一Study中出现多个PatientID", "人工核对患者归属",
            )
        )
    if len(study.exam_dates) > 1:
        warnings.append(
            exception_record(
                "Study日期不一致", patient_id, patient_name, study.study_uid,
                "同一Study中出现多个检查日期: " + join_values(exam_dates),
                "当前明细使用最早日期，请人工核对",
            )
        )
    if not exam_dates:
        warnings.append(
            exception_record(
                "缺少检查日期", patient_id, patient_name, study.study_uid,
                "StudyDate和AcquisitionDate均为空", "无法进行手术时间匹配",
            )
        )

    row = {
        "patient_id": patient_id,
        "patient_name": patient_name,
        "exam_date": exam_dates[0] if exam_dates else "",
        "modalities": modality_text,
        "has_3d": "有" if has_3d else "无",
        "display_type": display_type,
        "study_uid": study.study_uid,
        "series_count": len(study.series),
        "file_count": sum(summary["file_count"] for summary in series_summaries),
        "confidence": round(float(confidence), 2),
        "evidence": " | ".join(evidence),
        "source_root": str(source_root),
        "destination_dir": study.destination_dir,
        "series_summaries": series_summaries,
    }
    return row, warnings


def exception_record(
    category: str,
    patient_id: str,
    patient_name: str,
    study_uid: str,
    reason: str,
    suggestion: str,
    source_path: str = "",
) -> dict[str, str]:
    return {
        "异常类型": category,
        "PatientID": patient_id,
        "PatientName": patient_name,
        "StudyInstanceUID": study_uid,
        "源文件": source_path,
        "原因": reason,
        "建议处理": suggestion,
    }


def transfer_exception(result: TransferResult) -> dict[str, str]:
    meta = result.scan.meta or DicomMeta()
    categories = {
        "quarantined": "DICOM隔离",
        "conflict": "同UID内容冲突",
        "error": "转存失败",
    }
    suggestions = {
        "quarantined": "核对缺失标签后决定归档位置",
        "conflict": "保留两个文件并人工核对来源",
        "error": "检查源文件和目标目录权限后重试",
    }
    return exception_record(
        categories.get(result.status, result.status),
        meta.patient_id,
        meta.patient_name,
        meta.study_uid,
        result.message,
        suggestions.get(result.status, "人工核对"),
        result.scan.relative_path,
    )


EXCEPTION_COLUMNS = [
    "异常类型", "PatientID", "PatientName", "StudyInstanceUID",
    "源文件", "原因", "建议处理",
]
MANIFEST_COLUMNS = [
    "源文件", "目标文件", "PatientID", "StudyInstanceUID",
    "SeriesInstanceUID", "SOPInstanceUID", "文件大小", "SHA256", "状态", "说明",
]


def write_exception_csv(path: Path, rows: Sequence[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=EXCEPTION_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def write_manifest_header(stream) -> csv.DictWriter:
    writer = csv.DictWriter(stream, fieldnames=MANIFEST_COLUMNS)
    writer.writeheader()
    return writer


def manifest_row(result: TransferResult) -> dict[str, Any]:
    meta = result.scan.meta or DicomMeta()
    return {
        "源文件": result.scan.relative_path,
        "目标文件": result.destination_path,
        "PatientID": meta.patient_id,
        "StudyInstanceUID": meta.study_uid,
        "SeriesInstanceUID": meta.series_uid,
        "SOPInstanceUID": meta.sop_uid,
        "文件大小": result.file_size,
        "SHA256": result.sha256,
        "状态": result.status,
        "说明": result.message,
    }


def find_node_executable() -> str:
    configured = os.environ.get("DICOM_V2_NODE")
    candidates = [
        configured,
        shutil.which("node"),
        r"C:\Users\unionstrong\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    raise RuntimeError(
        "未找到Node.js运行时，无法生成Excel；可通过DICOM_V2_NODE指定node.exe"
    )


def write_study_workbook(
    output_path: Path,
    study_rows: Sequence[dict[str, Any]],
    source_root: Path,
    destination_root: Path,
    counters: dict[str, int],
    preview_dir: Optional[Path] = None,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_root": str(source_root),
        "destination_root": str(destination_root),
        "rule_version": RULE_VERSION,
        "counters": counters,
        "studies": [
            {key: value for key, value in row.items() if key != "series_summaries"}
            for row in study_rows
        ],
    }
    script_path = Path(__file__).with_name("build_dicom_study_workbook.mjs")
    if not script_path.is_file():
        raise RuntimeError(f"缺少Excel构建脚本: {script_path}")
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", encoding="utf-8", delete=False,
            dir=str(output_path.parent),
        ) as temporary:
            json.dump(payload, temporary, ensure_ascii=False)
            temporary_path = Path(temporary.name)
        command = [
            find_node_executable(), str(script_path), str(temporary_path),
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
            timeout=180,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f"Excel生成失败: {detail}")
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()


def process_directory(
    source_root: Path,
    destination_root: Path,
    excel_path: Path,
    exception_path: Path,
    workers: int = 8,
    copy_workers: int = 2,
    batch_size: int = 500,
    manifest_path: Optional[Path] = None,
    preview_dir: Optional[Path] = None,
    durable: bool = False,
) -> dict[str, int]:
    source_root = source_root.resolve()
    destination_root = destination_root.resolve()
    excel_path = excel_path.resolve()
    exception_path = exception_path.resolve()
    if not source_root.is_dir():
        raise ValueError(f"源目录不存在: {source_root}")
    if source_root == destination_root:
        raise ValueError("源目录与目标目录不能相同")
    destination_root.mkdir(parents=True, exist_ok=True)

    counters = {
        "total": 0,
        "dicom": 0,
        "copied": 0,
        "duplicate": 0,
        "conflict": 0,
        "quarantined": 0,
        "skipped": 0,
        "error": 0,
        "studies": 0,
    }
    studies: dict[str, StudyAccumulator] = {}
    exceptions: list[dict[str, str]] = []
    manifest_stream = None
    manifest_writer = None
    try:
        if manifest_path is not None:
            manifest_path = manifest_path.resolve()
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_stream = manifest_path.open(
                "w", newline="", encoding="utf-8-sig"
            )
            manifest_writer = write_manifest_header(manifest_stream)

        files = iter_source_files(source_root, destination_root)
        with (
            ThreadPoolExecutor(max_workers=max(1, workers)) as scan_executor,
            ThreadPoolExecutor(max_workers=max(1, copy_workers)) as copy_executor,
        ):
            for file_batch in chunked(files, max(1, batch_size)):
                scans = list(
                    scan_executor.map(
                        lambda file_path: scan_one_file(file_path, source_root),
                        file_batch,
                    )
                )
                dicom_scans = []
                for scan in scans:
                    counters["total"] += 1
                    if not scan.is_dicom:
                        counters["skipped"] += 1
                        continue
                    counters["dicom"] += 1
                    dicom_scans.append(scan)

                results = copy_executor.map(
                    lambda scan: transfer_one(
                        scan, destination_root, durable=durable
                    ),
                    dicom_scans,
                )
                for result in results:
                    scan = result.scan
                    if result.status == "copied":
                        counters["copied"] += 1
                    elif result.status == "duplicate_same":
                        counters["duplicate"] += 1
                    elif result.status == "conflict":
                        counters["conflict"] += 1
                    elif result.status == "quarantined":
                        counters["quarantined"] += 1
                    else:
                        counters["error"] += 1

                    if manifest_writer is not None:
                        manifest_writer.writerow(manifest_row(result))
                    if result.status in {"conflict", "quarantined", "error"}:
                        exceptions.append(transfer_exception(result))

                    meta = scan.meta
                    if (
                        meta is not None
                        and meta.study_uid
                        and meta.series_uid
                        and meta.sop_uid
                        and result.status in {"copied", "duplicate_same"}
                    ):
                        study = studies.setdefault(
                            meta.study_uid,
                            StudyAccumulator(study_uid=meta.study_uid),
                        )
                        study.add(meta, result.destination_path)

                print(
                    f"已扫描 {counters['total']}，DICOM {counters['dicom']}，"
                    f"复制 {counters['copied']}，重复 {counters['duplicate']}，"
                    f"隔离 {counters['quarantined']}，错误 {counters['error']}"
                )
    finally:
        if manifest_stream is not None:
            manifest_stream.close()

    study_rows = []
    for _, study in sorted(studies.items(), key=lambda item: item[0]):
        row, study_warnings = summarize_study(study, source_root)
        study_rows.append(row)
        exceptions.extend(study_warnings)
    study_rows.sort(
        key=lambda row: (
            row["patient_id"], row["exam_date"] or "9999-99-99", row["study_uid"]
        )
    )
    counters["studies"] = len(study_rows)

    write_exception_csv(exception_path, exceptions)
    write_study_workbook(
        excel_path,
        study_rows,
        source_root,
        destination_root,
        counters,
        preview_dir=preview_dir,
    )
    return counters


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="DICOM原始影像转存与Study级检查汇总（无数据库版）"
    )
    parser.add_argument("src_dir", help="DICOM源目录")
    parser.add_argument("dst_dir", help="转存目标目录")
    parser.add_argument(
        "--excel", help="检查明细Excel路径，默认写入目标目录/影像检查明细.xlsx"
    )
    parser.add_argument(
        "--exceptions", help="异常CSV路径，默认写入目标目录/待确认记录.csv"
    )
    parser.add_argument(
        "--manifest",
        nargs="?",
        const="AUTO",
        help="可选：输出完整文件级转存清单CSV",
    )
    parser.add_argument("--workers", type=int, default=8, help="DICOM解析线程数，默认8")
    parser.add_argument(
        "--copy-workers", type=int, default=2, help="文件复制线程数，默认2"
    )
    parser.add_argument("--batch-size", type=int, default=500, help="扫描批次大小，默认500")
    parser.add_argument(
        "--durable",
        action="store_true",
        help="每个文件复制后强制fsync并保留时间戳；更安全但明显更慢",
    )
    parser.add_argument(
        "--preview-dir",
        help=argparse.SUPPRESS,
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    source_root = Path(args.src_dir)
    destination_root = Path(args.dst_dir)
    excel_path = Path(args.excel) if args.excel else destination_root / "影像检查明细.xlsx"
    exception_path = (
        Path(args.exceptions)
        if args.exceptions
        else destination_root / "待确认记录.csv"
    )
    if args.manifest == "AUTO":
        manifest_path = destination_root / "转存清单.csv"
    elif args.manifest:
        manifest_path = Path(args.manifest)
    else:
        manifest_path = None
    preview_dir = Path(args.preview_dir) if args.preview_dir else None

    try:
        counters = process_directory(
            source_root,
            destination_root,
            excel_path,
            exception_path,
            workers=args.workers,
            copy_workers=args.copy_workers,
            batch_size=args.batch_size,
            manifest_path=manifest_path,
            preview_dir=preview_dir,
            durable=args.durable,
        )
    except Exception as exc:
        print(f"处理失败: {exc}", file=sys.stderr)
        return 1

    print("=" * 60)
    print("处理完成")
    print(f"扫描文件: {counters['total']}")
    print(f"DICOM文件: {counters['dicom']}")
    print(f"成功复制: {counters['copied']}")
    print(f"内容重复: {counters['duplicate']}")
    print(f"UID冲突: {counters['conflict']}")
    print(f"隔离文件: {counters['quarantined']}")
    print(f"非DICOM跳过: {counters['skipped']}")
    print(f"错误: {counters['error']}")
    print(f"Study汇总: {counters['studies']}")
    print(f"Excel: {excel_path.resolve()}")
    print(f"异常记录: {exception_path.resolve()}")
    if manifest_path:
        print(f"转存清单: {manifest_path.resolve()}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
