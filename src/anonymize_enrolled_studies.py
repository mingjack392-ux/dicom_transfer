#!/usr/bin/env python3
"""Anonymize only reviewed enrolled-patient Studies into a delivery tree.

The source center directory is read-only.  Preview and full verification are
the default; permanent files are written only with ``--execute``.

Customer-specific rule:
* delete PatientName, PatientBirthDate and center/institution names;
* retain PatientID exactly as supplied;
* retain all DICOM date/time values and all UIDs exactly;
* retain PixelData bytes exactly.

This is controlled pseudonymization, not proof that burned-in pixel text or
recognizable anatomy has been removed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import sys
import tempfile
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Iterable, Iterator

try:
    import pydicom
    from pydicom.dataset import Dataset
    from pydicom.tag import Tag
except ImportError as exc:  # pragma: no cover - environment-specific
    raise SystemExit("缺少pydicom，请执行: python -m pip install pydicom") from exc

from enrolled_anonymization_common import (
    is_yes,
    normalized_text,
    read_table,
    RULE_VERSION,
    text,
)


PATIENT_NAME = Tag(0x0010, 0x0010)
PATIENT_ID = Tag(0x0010, 0x0020)
PATIENT_BIRTH_DATE = Tag(0x0010, 0x0030)
INSTITUTION_NAME = Tag(0x0008, 0x0080)
DELETE_TAGS = {PATIENT_NAME, PATIENT_BIRTH_DATE, INSTITUTION_NAME}
DATE_TIME_VRS = {"DA", "DT", "TM"}
TEXT_VRS = {"AE", "AS", "CS", "LO", "LT", "PN", "SH", "ST", "UC", "UR", "UT"}
UID_PATTERN = re.compile(r"\d+(?:\.\d+)+")
LEGACY_TARGET_PATTERN = re.compile(r"\d{3,}-(?:术前|术中|术前与术中|6M|12M)")
PROGRESS_LOCK = Lock()

LIST_REQUIRED = (
    "中心编号",
    "中心名称",
    "匿名编号",
    "StudyInstanceUID",
    "标准访视期",
    "目标二级目录",
    "是否进入匿名化",
)

AUDIT_FIELDS = (
    "规则版本",
    "状态",
    "中心编号",
    "中心名称",
    "匿名编号",
    "标准访视期",
    "StudyInstanceUID",
    "SeriesInstanceUID",
    "SOPInstanceUID",
    "源文件",
    "目标文件",
    "删除姓名元素数",
    "删除出生日期元素数",
    "删除机构名称元素数",
    "删除中心文字元素数",
    "验证结果",
    "说明",
)


@dataclass(frozen=True)
class StudyPlan:
    center_code: str
    center_name: str
    anonymous_code: str
    period: str
    target_folder: str
    study_uid: str


@dataclass(frozen=True)
class SourceFile:
    path: Path
    study_uid: str
    series_uid: str
    sop_uid: str
    patient_id: str


@dataclass
class ChangeCounts:
    patient_name: int = 0
    birth_date: int = 0
    institution_name: int = 0
    center_text: int = 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="按已审核的入组Study清单匿名化指定中心的转存数据。默认仅预览验证。"
    )
    parser.add_argument("--source-center", required=True, help="一个中心的转存目录；不会扫描其他中心")
    parser.add_argument("--study-list", required=True, help="第一步生成并已人工审核的.xlsx")
    parser.add_argument("--output-root", required=True, help="交付影像根目录，例如01.交付影像")
    parser.add_argument(
        "--control-dir",
        help="内部控制文件目录；执行时默认是输出根目录同级的02.内部控制文件",
    )
    parser.add_argument("--sheet", default="入组Study清单", help="Study清单工作表")
    parser.add_argument(
        "--center-alias",
        action="append",
        default=[],
        help="需要从文本元数据中删除的中心别名；可重复传入",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=min(4, os.cpu_count() or 1),
        help="并行处理Study数量，默认最多4；共享盘较慢时可设为2或1",
    )
    parser.add_argument("--execute", action="store_true", help="实际写入独立输出目录")
    return parser


def _progress(message: str) -> None:
    with PROGRESS_LOCK:
        print(message, flush=True)


def _fold(value: Any) -> str:
    raw = unicodedata.normalize("NFKC", text(value)).casefold()
    return re.sub(r"\s+", "", raw)


def _valid_uid(value: str) -> bool:
    return bool(value and len(value) <= 64 and UID_PATTERN.fullmatch(value))


def load_plans(path: str, sheet_name: str) -> tuple[list[StudyPlan], list[dict[str, str]]]:
    table = read_table(path, LIST_REQUIRED, sheet_name=sheet_name)
    plans_by_uid: dict[str, StudyPlan] = {}
    conflicted_uids: set[str] = set()
    rejected: list[dict[str, str]] = []
    for row in table.rows:
        if not is_yes(row.get("是否进入匿名化")):
            continue
        study_uid = text(row.get("StudyInstanceUID"))
        code = text(row.get("匿名编号"))
        period = text(row.get("标准访视期"))
        target_folder = text(row.get("目标二级目录"))
        center_code = text(row.get("中心编号"))
        center_name = text(row.get("中心名称"))
        expected_folder = f"{code}-{period}"
        reasons: list[str] = []
        if not _valid_uid(study_uid):
            reasons.append("StudyInstanceUID为空或格式无效")
        if not re.fullmatch(r"\d{3,}", code):
            reasons.append("匿名编号必须是至少3位数字")
        if period not in {"术前", "术中", "术前与术中", "6M", "12M"}:
            reasons.append("标准访视期不是允许值")
        if target_folder != expected_folder:
            reasons.append(f"目标二级目录应为{expected_folder}")
        if not center_code or not center_name:
            reasons.append("中心编号或中心名称为空")
        if reasons:
            rejected.append(
                {
                    "row": text(row.get("__row__")),
                    "study_uid": study_uid,
                    "message": "；".join(reasons),
                }
            )
            continue
        plan = StudyPlan(center_code, center_name, code, period, target_folder, study_uid)
        if study_uid in conflicted_uids:
            continue
        previous = plans_by_uid.get(study_uid)
        if previous and previous != plan:
            rejected.append(
                {
                    "row": text(row.get("__row__")),
                    "study_uid": study_uid,
                    "message": "同一StudyInstanceUID在清单中对应不同中心、患者或时期",
                }
            )
            plans_by_uid.pop(study_uid, None)
            conflicted_uids.add(study_uid)
            continue
        plans_by_uid[study_uid] = plan

    twelve_month_by_patient: dict[tuple[str, str], list[StudyPlan]] = {}
    for plan in plans_by_uid.values():
        if plan.period == "12M":
            twelve_month_by_patient.setdefault(
                (plan.center_code, plan.anonymous_code), []
            ).append(plan)
    for (_center_code, anonymous_code), candidates in twelve_month_by_patient.items():
        if len(candidates) <= 1:
            continue
        study_display = "、".join(plan.study_uid for plan in candidates)
        for plan in candidates:
            plans_by_uid.pop(plan.study_uid, None)
        rejected.append(
            {
                "row": "",
                "study_uid": study_display,
                "message": (
                    f"匿名编号{anonymous_code}选择了多个12M Study；"
                    "请在筛选表中只保留一个“是否进入匿名化=是”"
                ),
            }
        )

    plans = sorted(
        plans_by_uid.values(),
        key=lambda item: (int(item.anonymous_code), item.period, item.study_uid),
    )
    centers = {(item.center_code, item.center_name) for item in plans}
    if len(centers) > 1:
        raise ValueError("一个Study清单包含多个中心；请按中心分别执行")
    return plans, rejected


def locate_study_directories(source_center: Path, study_uids: set[str]) -> dict[str, list[Path]]:
    found: dict[str, list[Path]] = {uid: [] for uid in study_uids}
    visited = 0
    for root, directories, _files in os.walk(source_center):
        visited += 1
        if visited % 5000 == 0:
            matched = sum(bool(paths) for paths in found.values())
            _progress(f"[扫描目录] 已检查{visited}个目录，已找到{matched}/{len(study_uids)}个Study")
        current = Path(root)
        if current.name in study_uids:
            found[current.name].append(current)
            directories[:] = []
    matched = sum(bool(paths) for paths in found.values())
    _progress(f"[扫描完成] 共检查{visited}个目录，找到{matched}/{len(study_uids)}个Study")
    return found


def _audit_base(plan: StudyPlan) -> dict[str, Any]:
    return {
        "规则版本": RULE_VERSION,
        "中心编号": plan.center_code,
        "中心名称": plan.center_name,
        "匿名编号": plan.anonymous_code,
        "标准访视期": plan.period,
        "StudyInstanceUID": plan.study_uid,
    }


def inspect_study(
    plan: StudyPlan,
    study_directory: Path,
    *,
    allow_multiple_patient_ids: bool = False,
) -> tuple[list[SourceFile], list[dict[str, Any]]]:
    files: list[SourceFile] = []
    audits: list[dict[str, Any]] = []
    blocking = False
    candidate_paths = sorted(path for path in study_directory.rglob("*") if path.is_file())
    if not candidate_paths:
        audits.append(
            {
                **_audit_base(plan),
                "状态": "Study为空",
                "源文件": str(study_directory),
                "验证结果": "失败",
                "说明": "Study目录内没有文件",
            }
        )
        return [], audits

    for source_path in candidate_paths:
        try:
            dataset = pydicom.dcmread(source_path, stop_before_pixels=True, force=False)
        except Exception as exc:
            status = "DICOM读取失败" if source_path.suffix.lower() == ".dcm" else "跳过非DICOM"
            audits.append(
                {
                    **_audit_base(plan),
                    "状态": status,
                    "源文件": str(source_path),
                    "验证结果": "失败" if status == "DICOM读取失败" else "不适用",
                    "说明": str(exc),
                }
            )
            blocking = blocking or status == "DICOM读取失败"
            continue
        study_uid = text(dataset.get("StudyInstanceUID"))
        series_uid = text(dataset.get("SeriesInstanceUID"))
        sop_uid = text(dataset.get("SOPInstanceUID"))
        patient_id = text(dataset.get("PatientID"))
        problems: list[str] = []
        if study_uid != plan.study_uid:
            problems.append(f"头信息StudyInstanceUID={study_uid or '<空>'}与清单不一致")
        if not _valid_uid(series_uid):
            problems.append("SeriesInstanceUID缺失或无效")
        if not _valid_uid(sop_uid):
            problems.append("SOPInstanceUID缺失或无效")
        if problems:
            blocking = True
            audits.append(
                {
                    **_audit_base(plan),
                    "状态": "UID校验失败",
                    "SeriesInstanceUID": series_uid,
                    "SOPInstanceUID": sop_uid,
                    "源文件": str(source_path),
                    "验证结果": "失败",
                    "说明": "；".join(problems),
                }
            )
            continue
        files.append(SourceFile(source_path, study_uid, series_uid, sop_uid, patient_id))

    patient_ids = {item.patient_id for item in files if item.patient_id}
    if len(patient_ids) > 1:
        if allow_multiple_patient_ids:
            audits.append(
                {
                    **_audit_base(plan),
                    "状态": "Study多PatientID人工确认",
                    "源文件": str(study_directory),
                    "验证结果": "通过",
                    "说明": (
                        f"检测到{len(patient_ids)}个不同PatientID；"
                        "已通过命令行指定StudyUID人工确认属于同一自然人。"
                        "各DICOM的PatientID仍原值保留并逐文件验证"
                    ),
                }
            )
        else:
            blocking = True
            audits.append(
                {
                    **_audit_base(plan),
                    "状态": "Study包含多个PatientID",
                    "源文件": str(study_directory),
                    "验证结果": "失败",
                    "说明": f"检测到{len(patient_ids)}个不同PatientID，未自动合并",
                }
            )
    if not files:
        blocking = True
        audits.append(
            {
                **_audit_base(plan),
                "状态": "Study无可用DICOM",
                "源文件": str(study_directory),
                "验证结果": "失败",
                "说明": "Study目录中没有通过头信息校验的DICOM文件",
            }
        )
    if blocking:
        return [], audits
    return files, audits


def _walk_datasets(dataset: Dataset, prefix: str = "root") -> Iterator[tuple[str, Dataset]]:
    yield prefix, dataset
    for element in list(dataset):
        if element.VR != "SQ" or not element.value:
            continue
        for index, item in enumerate(element.value):
            yield from _walk_datasets(item, f"{prefix}/{int(element.tag):08X}[{index}]")


def _snapshot(dataset: Dataset, predicate) -> tuple[tuple[str, int, str, str], ...]:
    output: list[tuple[str, int, str, str]] = []
    for prefix, item in _walk_datasets(dataset):
        for element in item:
            if element.VR == "SQ":
                continue
            if predicate(element):
                output.append((prefix, int(element.tag), element.VR, str(element.value)))
    if dataset.file_meta:
        for element in dataset.file_meta:
            if predicate(element):
                output.append(("file_meta", int(element.tag), element.VR, str(element.value)))
    return tuple(output)


def _pixel_hash(dataset: Dataset) -> str:
    if "PixelData" not in dataset:
        return ""
    return hashlib.sha256(bytes(dataset.PixelData)).hexdigest()


def _contains_alias(value: Any, aliases: tuple[str, ...]) -> bool:
    folded = _fold(value)
    return bool(folded and any(alias in folded for alias in aliases))


def anonymize_dataset(dataset: Dataset, center_aliases: Iterable[str]) -> tuple[ChangeCounts, list[str]]:
    aliases = tuple(
        dict.fromkeys(_fold(alias) for alias in center_aliases if len(_fold(alias)) >= 2)
    )
    counts = ChangeCounts()
    retained_alias_locations: list[str] = []

    for prefix, item in list(_walk_datasets(dataset)):
        for tag in list(item.keys()):
            element = item[tag]
            if tag == PATIENT_NAME:
                del item[tag]
                counts.patient_name += 1
                continue
            if tag == PATIENT_BIRTH_DATE:
                del item[tag]
                counts.birth_date += 1
                continue
            if tag == INSTITUTION_NAME:
                del item[tag]
                counts.institution_name += 1
                continue
            if element.VR == "SQ" or element.VR not in TEXT_VRS or not aliases:
                continue
            if not _contains_alias(element.value, aliases):
                continue
            if tag == PATIENT_ID or element.VR in DATE_TIME_VRS or element.VR == "UI":
                retained_alias_locations.append(f"{prefix}/{int(tag):08X}")
                continue
            del item[tag]
            counts.center_text += 1
    return counts, retained_alias_locations


def verify_dataset(
    before: dict[str, Any], dataset: Dataset, center_aliases: Iterable[str]
) -> tuple[bool, str, list[str]]:
    aliases = tuple(
        dict.fromkeys(_fold(alias) for alias in center_aliases if len(_fold(alias)) >= 2)
    )
    problems: list[str] = []
    protected_residue: list[str] = []
    for prefix, item in _walk_datasets(dataset):
        for tag in DELETE_TAGS:
            if tag in item:
                problems.append(f"应删除标签仍存在: {prefix}/{int(tag):08X}")
        for element in item:
            if element.VR == "SQ" or element.VR not in TEXT_VRS or not aliases:
                continue
            if not _contains_alias(element.value, aliases):
                continue
            location = f"{prefix}/{int(element.tag):08X}"
            if element.tag == PATIENT_ID or element.VR in DATE_TIME_VRS or element.VR == "UI":
                protected_residue.append(location)
            else:
                problems.append(f"中心名称文字仍存在: {location}")

    patient_ids = _snapshot(dataset, lambda element: element.tag == PATIENT_ID)
    dates = _snapshot(
        dataset,
        lambda element: element.VR in DATE_TIME_VRS and element.tag != PATIENT_BIRTH_DATE,
    )
    uids = _snapshot(dataset, lambda element: element.VR == "UI")
    pixel_hash = _pixel_hash(dataset)
    if patient_ids != before["patient_ids"]:
        problems.append("PatientID发生变化")
    if dates != before["dates"]:
        problems.append("日期或时间标签发生变化")
    if uids != before["uids"]:
        problems.append("UID标签发生变化")
    if pixel_hash != before["pixel_hash"]:
        problems.append("PixelData字节发生变化")
    return not problems, "；".join(problems) or "通过", protected_residue


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_dataset_temp(dataset: Dataset, directory: Path | None) -> Path:
    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=".anon.", suffix=".part", dir=directory)
    os.close(handle)
    path = Path(name)
    try:
        dataset.save_as(path, enforce_file_format=True)
    except TypeError:  # pydicom 2.x compatibility
        dataset.save_as(path, write_like_original=False)
    return path


def process_file(
    plan: StudyPlan,
    source: SourceFile,
    output_root: Path,
    control_dir: Path,
    center_aliases: tuple[str, ...],
    execute: bool,
) -> dict[str, Any]:
    target = (
        output_root
        / plan.anonymous_code
        / plan.target_folder
        / plan.study_uid
        / source.series_uid
        / f"{source.sop_uid}.dcm"
    )
    base = {
        **_audit_base(plan),
        "SeriesInstanceUID": source.series_uid,
        "SOPInstanceUID": source.sop_uid,
        "源文件": str(source.path),
        "目标文件": str(target),
    }
    temporary: Path | None = None
    try:
        dataset = pydicom.dcmread(source.path, force=False)
        before = {
            "patient_ids": _snapshot(dataset, lambda element: element.tag == PATIENT_ID),
            "dates": _snapshot(
                dataset,
                lambda element: element.VR in DATE_TIME_VRS
                and element.tag != PATIENT_BIRTH_DATE,
            ),
            "uids": _snapshot(dataset, lambda element: element.VR == "UI"),
            "pixel_hash": _pixel_hash(dataset),
        }
        counts, retained_during_edit = anonymize_dataset(dataset, center_aliases)
        temporary = _write_dataset_temp(dataset, target.parent if execute else None)
        reopened = pydicom.dcmread(temporary, force=False)
        valid, verification, protected_residue = verify_dataset(before, reopened, center_aliases)
        retained = sorted(set(retained_during_edit + protected_residue))
        if not valid:
            return {
                **base,
                "状态": "验证失败",
                "删除姓名元素数": counts.patient_name,
                "删除出生日期元素数": counts.birth_date,
                "删除机构名称元素数": counts.institution_name,
                "删除中心文字元素数": counts.center_text,
                "验证结果": "失败",
                "说明": verification,
            }

        retained_note = ""
        if retained:
            retained_note = (
                "；中心文字出现在按客户规则必须保留的PatientID/日期/UID位置: "
                + ",".join(retained)
            )
        if target.exists():
            same_target = _sha256(target) == _sha256(temporary)
            if not execute and same_target:
                status = "计划跳过重复相同"
                message = "目标已存在且匿名化文件SHA-256相同；预览模式未落盘" + retained_note
            elif not execute:
                status = "计划UID冲突"
                message = "目标UID路径已存在但文件内容不同；执行时不会覆盖正式交付文件" + retained_note
            elif same_target:
                status = "重复相同"
                message = "目标已存在且匿名化文件SHA-256相同，保留原文件" + retained_note
            else:
                conflict_dir = (
                    control_dir
                    / "_conflicts"
                    / plan.anonymous_code
                    / plan.target_folder
                    / plan.study_uid
                    / source.series_uid
                )
                conflict_dir.mkdir(parents=True, exist_ok=True)
                digest = _sha256(temporary)[:12]
                conflict = conflict_dir / f"{source.sop_uid}.{digest}.conflict.dcm"
                if not conflict.exists():
                    os.replace(temporary, conflict)
                    temporary = None
                status = "UID冲突"
                message = f"正式交付文件未覆盖；差异文件保存在内部控制目录: {conflict}" + retained_note
        elif not execute:
            status = "计划写入"
            message = "验证通过；预览模式未落盘" + retained_note
        else:
            os.replace(temporary, target)
            temporary = None
            status = "已写入"
            message = "匿名化及回读验证通过" + retained_note

        return {
            **base,
            "状态": status,
            "删除姓名元素数": counts.patient_name,
            "删除出生日期元素数": counts.birth_date,
            "删除机构名称元素数": counts.institution_name,
            "删除中心文字元素数": counts.center_text,
            "验证结果": "通过",
            "说明": message,
        }
    except Exception as exc:
        return {
            **base,
            "状态": "处理失败",
            "验证结果": "失败",
            "说明": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _process_plan(
    index: int,
    total: int,
    plan: StudyPlan,
    directories: list[Path],
    output_root: Path,
    control_dir: Path,
    aliases: tuple[str, ...],
    execute: bool,
) -> list[dict[str, Any]]:
    label = f"{plan.anonymous_code}-{plan.period}"
    if len(directories) != 1:
        _progress(
            f"[Study {index}/{total}] {label} "
            + ("未找到目录" if not directories else f"找到{len(directories)}个同名目录，已跳过")
        )
        return [
            {
                **_audit_base(plan),
                "状态": "Study目录缺失" if not directories else "Study目录重复",
                "源文件": "；".join(str(path) for path in directories),
                "验证结果": "失败",
                "说明": f"在当前中心目录内找到{len(directories)}个同名Study目录，未处理",
            }
        ]

    _progress(f"[Study {index}/{total}] {label} 开始检查")
    files, audits = inspect_study(plan, directories[0])
    if not files:
        _progress(f"[Study {index}/{total}] {label} 没有可处理文件，请查看最终异常汇总")
        return audits
    _progress(f"[Study {index}/{total}] {label} 发现{len(files)}个DICOM，开始匿名化验证")
    for file_index, source in enumerate(files, start=1):
        audits.append(
            process_file(
                plan,
                source,
                output_root,
                control_dir,
                aliases,
                execute,
            )
        )
        if file_index % 100 == 0 or file_index == len(files):
            _progress(
                f"[Study {index}/{total}] {label} 已处理{file_index}/{len(files)}个DICOM"
            )
    return audits


def _audit_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    code = text(row.get("匿名编号"))
    code_key = int(code) if code.isdigit() else 10**12
    return (
        code_key,
        text(row.get("标准访视期")),
        text(row.get("StudyInstanceUID")),
        text(row.get("SeriesInstanceUID")),
        text(row.get("SOPInstanceUID")),
        text(row.get("状态")),
    )


def _write_csv_atomic(path: Path, rows: list[dict[str, Any]], fields: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
    os.close(handle)
    temporary = Path(name)
    try:
        with temporary.open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _assert_separate_paths(source: Path, output: Path, control: Path) -> None:
    source = source.resolve()
    output = output.resolve()
    control = control.resolve()
    if source == output or source in output.parents or output in source.parents:
        raise ValueError("源目录与交付目录必须完全分离，且不能互相包含")
    if source == control or source in control.parents or control in source.parents:
        raise ValueError("源目录与内部控制目录必须完全分离，且不能互相包含")
    if output == control or output in control.parents or control in output.parents:
        raise ValueError("交付目录与内部控制目录必须分离，不能互相包含")


def run(args: argparse.Namespace) -> tuple[dict[str, int], list[dict[str, Any]], Path | None]:
    source_center = Path(args.source_center)
    output_root = Path(args.output_root)
    control_dir = (
        Path(args.control_dir)
        if args.control_dir
        else output_root.parent / "02.内部控制文件"
    )
    if not source_center.is_dir():
        raise FileNotFoundError(f"中心转存目录不存在: {source_center}")
    _assert_separate_paths(source_center, output_root, control_dir)

    workers = int(getattr(args, "workers", min(4, os.cpu_count() or 1)))
    if workers < 1 or workers > 32:
        raise ValueError("--workers必须在1到32之间")

    plans, rejected = load_plans(args.study_list, args.sheet)
    if not plans:
        raise ValueError("清单中没有可进入匿名化的Study")
    if output_root.is_dir():
        legacy_directories = sorted(
            path.name
            for path in output_root.iterdir()
            if path.is_dir() and LEGACY_TARGET_PATTERN.fullmatch(path.name)
        )
        if legacy_directories:
            display = "、".join(legacy_directories[:5])
            if len(legacy_directories) > 5:
                display += f"等{len(legacy_directories)}个"
            raise ValueError(
                "交付根目录存在v1.3旧层级目录，不能与v1.4混合: "
                f"{display}；请保留旧数据并改用新的空交付目录，或人工核对后再迁移"
            )
    aliases = tuple(
        dict.fromkeys(
            [plan.center_name for plan in plans]
            + [source_center.name]
            + list(args.center_alias)
        )
    )
    mode = "执行" if args.execute else "预览"
    _progress(f"[准备] 模式={mode}，Study={len(plans)}，并行Study数={workers}")
    _progress("[提示] VR SH长度警告来自原始DICOM字段，不会单独导致程序中止")
    _progress(f"[扫描目录] 正在当前中心目录查找{len(plans)}个Study")
    located = locate_study_directories(source_center, {plan.study_uid for plan in plans})
    audits: list[dict[str, Any]] = []
    for item in rejected:
        audits.append(
            {
                "状态": "清单行无效",
                "StudyInstanceUID": item["study_uid"],
                "验证结果": "失败",
                "说明": f"工作表第{item['row']}行: {item['message']}",
            }
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(
                _process_plan,
                index,
                len(plans),
                plan,
                located.get(plan.study_uid, []),
                output_root,
                control_dir,
                aliases,
                args.execute,
            )
            for index, plan in enumerate(plans, start=1)
        ]
        completed = 0
        for future in as_completed(futures):
            audits.extend(future.result())
            completed += 1
            _progress(f"[总体进度] 已完成{completed}/{len(plans)}个Study")

    audits.sort(key=_audit_sort_key)

    summary: dict[str, int] = {}
    for row in audits:
        status = text(row.get("状态"))
        summary[status] = summary.get(status, 0) + 1
    audit_path: Path | None = None
    if args.execute:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        audit_path = control_dir / f"匿名化处理清单_{timestamp}.csv"
        _write_csv_atomic(audit_path, audits, AUDIT_FIELDS)
        verification_rows = [
            row
            for row in audits
            if text(row.get("状态"))
            in {"验证失败", "处理失败", "UID校验失败", "Study包含多个PatientID", "Study无可用DICOM", "Study目录缺失", "Study目录重复", "DICOM读取失败", "清单行无效", "UID冲突"}
        ]
        verification_path = control_dir / f"匿名化验证报告_{timestamp}.csv"
        _write_csv_atomic(verification_path, verification_rows, AUDIT_FIELDS)
    return summary, audits, audit_path


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary, audits, audit_path = run(args)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    mode = "执行" if args.execute else "预览"
    print(f"规则版本: {RULE_VERSION}")
    print(f"模式: {mode}")
    for status, count in sorted(summary.items()):
        print(f"{status}: {count}")
    if audit_path:
        print(f"审计清单: {audit_path.resolve()}")
    else:
        print("预览模式未写入DICOM或控制文件；确认后增加--execute。")
    failed_statuses = {
        "验证失败",
        "处理失败",
        "UID校验失败",
        "Study包含多个PatientID",
        "Study无可用DICOM",
        "Study目录缺失",
        "Study目录重复",
        "DICOM读取失败",
        "清单行无效",
        "UID冲突",
        "计划UID冲突",
    }
    return 2 if any(text(row.get("状态")) in failed_statuses for row in audits) else 0


if __name__ == "__main__":
    raise SystemExit(main())
