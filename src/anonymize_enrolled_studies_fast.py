#!/usr/bin/env python3
"""High-throughput engine for reviewed enrolled-Study anonymization.

This is a separate entrypoint.  It deliberately reuses the established
``anonymize_enrolled_studies`` DICOM edit and verification functions, while
changing only orchestration:

* Study headers are inspected concurrently;
* validated DICOM files are dynamically scheduled across parallel workers;
* files sharing one target UID path are serialized as one group;
* verified completions are checkpointed for safe restart.

Preview remains the default.  Permanent output still requires ``--execute``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import warnings
from collections import Counter
from concurrent.futures import (
    FIRST_COMPLETED,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    as_completed,
    wait,
)
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

import anonymize_enrolled_studies as stable
from enrolled_anonymization_common import text
from pydicom.dataset import FileMetaDataset
from pydicom.errors import InvalidDicomError
from pydicom.uid import ImplicitVRLittleEndian, PYDICOM_IMPLEMENTATION_UID


ENGINE_VERSION = "2026.09.21-enrolled-anon-fast-v1.2.1"
PREVIOUS_ENGINE_VERSION = "2026.09.21-enrolled-anon-fast-v1.2.0"
OLDER_ENGINE_VERSION = "2026.09.10-enrolled-anon-fast-v1.1.0"
LEGACY_ENGINE_VERSION = "2026.09.10-enrolled-anon-fast-v1.0.1"
UID_POLICIES = {"strict", "preserve"}
HEADERLESS_POLICIES = {"strict", "allow"}
PATH_SAFE_UID_PATTERN = re.compile(r"[0-9.]+")
MAX_UID_PATH_CHARS = 240
FAST_AUDIT_FIELDS = (
    stable.AUDIT_FIELDS[0],
    "执行引擎",
    "UID处理策略",
    "无文件头策略",
    "多PatientID人工确认",
    *stable.AUDIT_FIELDS[1:9],
    "源UID问题",
    "源文件格式",
    "源文件格式问题",
    *stable.AUDIT_FIELDS[9:],
)
FAILED_STATUSES = {
    "验证失败",
    "处理失败",
    "UID校验失败",
    "UID路径不可用",
    "Study包含多个PatientID",
    "Study无可用DICOM",
    "Study目录缺失",
    "Study目录重复",
    "DICOM读取失败",
    "清单行无效",
    "UID冲突",
    "计划UID冲突",
    "高速扫描失败",
    "高速处理失败",
    "无文件头兼容校验失败",
}
CHECKPOINT_SUCCESS_STATUSES = {"已写入", "重复相同"}
STANDARD_SOURCE_FORMAT = "standard"
HEADERLESS_SOURCE_FORMAT = "headerless-implicit-vr-little-endian"
HEADERLESS_SOURCE_ISSUE = "源文件缺少DICM前导与File Meta，按兼容策略读取并规范化输出"


@dataclass
class FastRunResult:
    summary: dict[str, int]
    audits: list[dict[str, Any]]
    audit_path: Path | None
    verification_path: Path | None
    uid_issue_path: Path | None
    source_format_issue_path: Path | None
    checkpoint_path: Path | None
    resumed_files: int
    processed_files: int


@dataclass(frozen=True)
class FastSourceFile:
    path: Path
    study_uid: str
    series_uid: str
    sop_uid: str
    patient_id: str
    source_format: str = STANDARD_SOURCE_FORMAT


def build_parser(
    *, default_headerless_policy: str = "strict"
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "高速版：按已审核的入组Study清单匿名化指定中心。"
            "默认仅预览验证，旧版入口保持不变。"
        )
    )
    parser.add_argument("--source-center", required=True, help="一个中心的转存目录")
    parser.add_argument("--study-list", required=True, help="已人工审核的入组Study清单.xlsx")
    parser.add_argument("--output-root", required=True, help="交付影像根目录，例如01.交付影像")
    parser.add_argument(
        "--control-dir",
        help="内部控制目录；默认是输出根目录同级的02.内部控制文件",
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
        help="并行预检Study头信息的线程数，默认最多4",
    )
    parser.add_argument(
        "--dicom-workers",
        "--processes",
        dest="processes",
        metavar="N",
        type=int,
        default=min(4, os.cpu_count() or 1),
        help="并发匿名化DICOM的工作数，默认最多4；--processes是兼容别名",
    )
    parser.add_argument(
        "--backend",
        choices=("thread", "process"),
        default="thread",
        help="DICOM并发后端；默认thread适合磁盘/共享盘，process用于现场对比",
    )
    parser.add_argument(
        "--max-in-flight",
        type=int,
        default=0,
        help="同时排队的批任务数；0表示进程数的2倍",
    )
    parser.add_argument(
        "--batch-targets",
        type=int,
        default=0,
        help="每个进程任务包含的目标UID数；0表示自动计算，通常无需设置",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=100,
        help="每处理多少个DICOM显示一次总体进度，默认100；0表示只显示Study完成",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="不使用高速版断点状态，强制重新匿名化并回读验证全部文件",
    )
    parser.add_argument(
        "--uid-policy",
        choices=("strict", "preserve"),
        default="strict",
        help=(
            "strict按DICOM标准阻止非法UID；preserve接受可安全用于路径的非标准UID，"
            "原样保存全部UID并只在审计中记录问题"
        ),
    )
    parser.add_argument(
        "--headerless-policy",
        choices=("strict", "allow"),
        default=default_headerless_policy,
        help=(
            "strict拒绝缺少DICM/File Meta的文件；allow仅接受可验证的隐式VR小端"
            "无文件头数据集并规范化为标准DICOM输出"
        ),
    )
    parser.add_argument(
        "--confirmed-multi-patient-study",
        action="append",
        default=[],
        metavar="STUDY_UID",
        help=(
            "仅对指定StudyUID放行多个PatientID；可重复传入。"
            "必须已有人工身份确认，其他Study仍保持阻断"
        ),
    )
    parser.add_argument("--execute", action="store_true", help="实际写入独立交付目录")
    return parser


def _with_engine(row: dict[str, Any]) -> dict[str, Any]:
    output = dict(row)
    output["执行引擎"] = ENGINE_VERSION
    return output


def _configure_uid_warnings(uid_policy: str) -> None:
    if uid_policy != "preserve":
        return
    warnings.filterwarnings(
        "ignore",
        message=r"Invalid value for VR UI:.*",
        category=UserWarning,
    )
    warnings.filterwarnings(
        "ignore",
        message=(
            r"The value length \(\d+\) exceeds the maximum length of 64 "
            r"allowed for VR UI\."
        ),
        category=UserWarning,
    )


def _uid_standard_reasons(value: str) -> list[str]:
    reasons: list[str] = []
    if not value:
        return ["缺失"]
    if len(value) > 64:
        reasons.append(f"长度{len(value)}超过64")
    if not stable.UID_PATTERN.fullmatch(value):
        if ".." in value:
            reasons.append("包含连续小数点")
        elif value.startswith(".") or value.endswith("."):
            reasons.append("首尾包含小数点")
        else:
            reasons.append("格式不符合数字点分段")
    return reasons


def _uid_path_problem(value: str) -> str:
    if not value:
        return "缺失"
    if len(value) > MAX_UID_PATH_CHARS:
        return f"长度{len(value)}超过文件系统安全上限{MAX_UID_PATH_CHARS}"
    if not PATH_SAFE_UID_PATTERN.fullmatch(value):
        return "包含数字和小数点以外的路径不安全字符"
    if value.startswith(".") or value.endswith("."):
        return "首尾小数点无法跨Windows/Linux安全生成目录"
    return ""


def _source_uid_issues(dataset: Any) -> str:
    issues: list[str] = []
    for label in ("StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID"):
        value = text(dataset.get(label))
        reasons = _uid_standard_reasons(value)
        if reasons:
            issues.append(f"{label}：{'、'.join(reasons)}")

    nonstandard_count = 0
    for _prefix, item in stable._walk_datasets(dataset):
        for element in item:
            if element.VR == "UI" and not stable._valid_uid(text(element.value)):
                nonstandard_count += 1
    if dataset.file_meta:
        for element in dataset.file_meta:
            if element.VR == "UI" and not stable._valid_uid(text(element.value)):
                nonstandard_count += 1
    if nonstandard_count:
        issues.append(f"文件内非标准UI元素总数={nonstandard_count}")

    file_meta = dataset.file_meta
    meta_sop = text(file_meta.get("MediaStorageSOPInstanceUID")) if file_meta else ""
    data_sop = text(dataset.get("SOPInstanceUID"))
    if meta_sop != data_sop:
        issues.append("MediaStorageSOPInstanceUID与SOPInstanceUID不一致")
    meta_class = text(file_meta.get("MediaStorageSOPClassUID")) if file_meta else ""
    data_class = text(dataset.get("SOPClassUID"))
    if meta_class != data_class:
        issues.append("MediaStorageSOPClassUID与SOPClassUID不一致")
    for label in (
        "MediaStorageSOPClassUID",
        "MediaStorageSOPInstanceUID",
        "TransferSyntaxUID",
        "ImplementationClassUID",
    ):
        value = text(file_meta.get(label)) if file_meta else ""
        if not value:
            issues.append(f"文件元信息{label}缺失")
    return "；".join(dict.fromkeys(issues))


def _target_for(
    plan: stable.StudyPlan,
    source: stable.SourceFile,
    output_root: Path,
) -> Path:
    return (
        output_root
        / plan.anonymous_code
        / plan.target_folder
        / plan.study_uid
        / source.series_uid
        / f"{source.sop_uid}.dcm"
    )


def _checkpoint_context(
    aliases: Sequence[str],
    uid_policy: str,
    headerless_policy: str = "strict",
    confirmed_multi_patient_studies: Sequence[str] = (),
    *,
    engine_version: str = ENGINE_VERSION,
    include_uid_policy: bool = True,
    include_headerless_policy: bool = True,
    include_confirmed_multi_patient_studies: bool = True,
) -> str:
    normalized_aliases = sorted(
        {stable._fold(alias) for alias in aliases if stable._fold(alias)}
    )
    context_payload: dict[str, Any] = {
        "engine_version": engine_version,
        "rule_version": stable.RULE_VERSION,
        "aliases": normalized_aliases,
    }
    if include_uid_policy:
        context_payload["uid_policy"] = uid_policy
    if include_headerless_policy:
        context_payload["headerless_policy"] = headerless_policy
    if include_confirmed_multi_patient_studies:
        context_payload["confirmed_multi_patient_studies"] = sorted(
            set(confirmed_multi_patient_studies)
        )
    payload = json.dumps(
        context_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _previous_checkpoint_context(aliases: Sequence[str]) -> str:
    return _checkpoint_context(
        aliases,
        "strict",
        "strict",
        engine_version=LEGACY_ENGINE_VERSION,
        include_uid_policy=False,
        include_headerless_policy=False,
        include_confirmed_multi_patient_studies=False,
    )


def _v1_2_checkpoint_context(
    aliases: Sequence[str], uid_policy: str, headerless_policy: str
) -> str:
    return _checkpoint_context(
        aliases,
        uid_policy,
        headerless_policy,
        engine_version=PREVIOUS_ENGINE_VERSION,
        include_confirmed_multi_patient_studies=False,
    )


def _v1_1_checkpoint_context(
    aliases: Sequence[str], uid_policy: str
) -> str:
    return _checkpoint_context(
        aliases,
        uid_policy,
        "strict",
        engine_version=OLDER_ENGINE_VERSION,
        include_headerless_policy=False,
        include_confirmed_multi_patient_studies=False,
    )


def _checkpoint_path(source_center: Path, output_root: Path, control_dir: Path) -> Path:
    identity = f"{source_center.resolve()}\0{output_root.resolve()}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return control_dir / "_fast_state" / f"checkpoint_{digest}.jsonl"


def _checkpoint_key(source: Path, target: Path) -> str:
    return f"{source.resolve()}\0{target.resolve()}"


def _load_checkpoint(
    path: Path, contexts: Iterable[str]
) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    accepted_contexts = set(contexts)
    if not path.is_file():
        return entries
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                # A killed process can leave only the final append incomplete.
                # Earlier complete records remain valid; the damaged line is
                # conservatively ignored and will be processed again.
                continue
            if row.get("context") not in accepted_contexts:
                continue
            key = text(row.get("key"))
            if key:
                entries[key] = row
    return entries


def _stat_matches(path: Path, size: Any, mtime_ns: Any) -> bool:
    try:
        stat = path.stat()
        return stat.st_size == int(size) and stat.st_mtime_ns == int(mtime_ns)
    except (OSError, TypeError, ValueError):
        return False


def _can_resume(
    source: stable.SourceFile | FastSourceFile,
    target: Path,
    entry: dict[str, Any] | None,
    *,
    requires_multi_patient_confirmation: bool = False,
) -> bool:
    if not entry:
        return False
    source_format = getattr(source, "source_format", STANDARD_SOURCE_FORMAT)
    if source_format == HEADERLESS_SOURCE_FORMAT:
        if text(entry.get("source_format")) != HEADERLESS_SOURCE_FORMAT:
            return False
    if requires_multi_patient_confirmation and not text(
        entry.get("multi_patient_confirmation")
    ):
        return False
        if not text(entry.get("source_format_issue")):
            return False
    return (
        text(entry.get("source")) == str(source.path.resolve())
        and text(entry.get("target")) == str(target.resolve())
        and _stat_matches(source.path, entry.get("source_size"), entry.get("source_mtime_ns"))
        and _stat_matches(target, entry.get("target_size"), entry.get("target_mtime_ns"))
    )


class CheckpointWriter:
    def __init__(self, path: Path, context: str) -> None:
        self.path = path
        self.context = context
        self.stream = None

    def __enter__(self) -> "CheckpointWriter":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a", encoding="utf-8", newline="\n")
        return self

    def append(self, row: dict[str, Any]) -> None:
        if text(row.get("状态")) not in CHECKPOINT_SUCCESS_STATUSES:
            return
        source = Path(text(row.get("源文件")))
        target = Path(text(row.get("目标文件")))
        try:
            source_stat = source.stat()
            target_stat = target.stat()
        except OSError:
            return
        payload = {
            "context": self.context,
            "key": _checkpoint_key(source, target),
            "source": str(source.resolve()),
            "source_size": source_stat.st_size,
            "source_mtime_ns": source_stat.st_mtime_ns,
            "target": str(target.resolve()),
            "target_size": target_stat.st_size,
            "target_mtime_ns": target_stat.st_mtime_ns,
            "source_uid_issues": text(row.get("源UID问题")),
            "source_format": text(row.get("源文件格式")) or STANDARD_SOURCE_FORMAT,
            "source_format_issue": text(row.get("源文件格式问题")),
            "multi_patient_confirmation": text(row.get("多PatientID人工确认")),
            "verified_at": datetime.now().isoformat(timespec="seconds"),
        }
        assert self.stream is not None
        self.stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.stream.flush()

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        if self.stream is not None:
            self.stream.close()


def _resume_audit(
    plan: stable.StudyPlan,
    source: stable.SourceFile | FastSourceFile,
    target: Path,
    entry: dict[str, Any],
) -> dict[str, Any]:
    return _with_engine(
        {
            **stable._audit_base(plan),
            "状态": "断点跳过",
            "SeriesInstanceUID": source.series_uid,
            "SOPInstanceUID": source.sop_uid,
            "源文件": str(source.path),
            "目标文件": str(target),
            "源UID问题": text(entry.get("source_uid_issues")),
            "源文件格式": (
                text(entry.get("source_format")) or STANDARD_SOURCE_FORMAT
            ),
            "源文件格式问题": text(entry.get("source_format_issue")),
            "多PatientID人工确认": text(entry.get("multi_patient_confirmation")),
            "验证结果": "通过",
            "说明": (
                "高速版断点记录显示该源文件与已验证目标均未变化；"
                "本次未重复读取PixelData。使用--no-resume可强制重新验证"
            ),
        }
    )


def _study_directory_error(
    plan: stable.StudyPlan,
    directories: Sequence[Path],
) -> dict[str, Any]:
    return _with_engine(
        {
            **stable._audit_base(plan),
            "状态": "Study目录缺失" if not directories else "Study目录重复",
            "源文件": "；".join(str(path) for path in directories),
            "验证结果": "失败",
            "说明": f"在当前中心目录内找到{len(directories)}个同名Study目录，未处理",
        }
    )


def _multiple_patient_id_audit(
    plan: stable.StudyPlan,
    study_directory: Path,
    patient_id_count: int,
    allow_multiple_patient_ids: bool,
) -> dict[str, Any]:
    if allow_multiple_patient_ids:
        return {
            **stable._audit_base(plan),
            "状态": "Study多PatientID人工确认",
            "源文件": str(study_directory),
            "多PatientID人工确认": "是（命令行按StudyUID显式确认同一自然人）",
            "验证结果": "通过",
            "说明": (
                f"检测到{patient_id_count}个不同PatientID；"
                "已通过--confirmed-multi-patient-study显式放行。"
                "各DICOM的PatientID仍原值保留并逐文件验证"
            ),
        }
    return {
        **stable._audit_base(plan),
        "状态": "Study包含多个PatientID",
        "源文件": str(study_directory),
        "验证结果": "失败",
        "说明": f"检测到{patient_id_count}个不同PatientID，未自动合并",
    }


def _source_format(source: stable.SourceFile | FastSourceFile) -> str:
    return text(getattr(source, "source_format", "")) or STANDARD_SOURCE_FORMAT


def _source_format_issue(source: stable.SourceFile | FastSourceFile) -> str:
    return (
        HEADERLESS_SOURCE_ISSUE
        if _source_format(source) == HEADERLESS_SOURCE_FORMAT
        else ""
    )


def _has_dicm_prefix(path: Path) -> bool:
    with path.open("rb") as stream:
        header = stream.read(132)
    return len(header) >= 132 and header[128:132] == b"DICM"


def _read_headerless_dataset(
    path: Path, *, stop_before_pixels: bool
) -> Any:
    if _has_dicm_prefix(path):
        raise ValueError("文件存在DICM前导，不能按无文件头兼容模式降级读取")
    dataset = stable.pydicom.dcmread(
        path,
        stop_before_pixels=stop_before_pixels,
        force=True,
    )
    if dataset.file_meta and len(dataset.file_meta):
        raise ValueError("强制读取后检测到File Meta，不属于允许的无文件头数据集")
    if dataset.is_implicit_VR is not True or dataset.is_little_endian is not True:
        raise ValueError("只允许已识别为隐式VR小端的无文件头数据集")
    return dataset


def _headerless_path_problems(
    source_path: Path,
    study_directory: Path,
    study_uid: str,
    series_uid: str,
    sop_uid: str,
) -> list[str]:
    try:
        relative = source_path.relative_to(study_directory)
    except ValueError:
        return ["源文件不在当前Study目录内"]
    if len(relative.parts) != 2:
        return ["无文件头文件必须位于StudyUID/SeriesUID/SOPUID.dcm标准转存层级"]
    problems: list[str] = []
    if study_directory.name != study_uid:
        problems.append("StudyInstanceUID与Study目录名不一致")
    if relative.parts[0] != series_uid:
        problems.append("SeriesInstanceUID与Series目录名不一致")
    if relative.suffix.lower() != ".dcm" or relative.stem != sop_uid:
        problems.append("SOPInstanceUID与DICOM文件名不一致")
    return problems


def _inspect_study_allow_headerless(
    plan: stable.StudyPlan,
    study_directory: Path,
    *,
    allow_multiple_patient_ids: bool = False,
) -> tuple[list[FastSourceFile], list[dict[str, Any]]]:
    files: list[FastSourceFile] = []
    audits: list[dict[str, Any]] = []
    blocking = False
    candidate_paths = sorted(
        path for path in study_directory.rglob("*") if path.is_file()
    )
    if not candidate_paths:
        return [], [
            {
                **stable._audit_base(plan),
                "状态": "Study为空",
                "源文件": str(study_directory),
                "验证结果": "失败",
                "说明": "Study目录内没有文件",
            }
        ]

    for source_path in candidate_paths:
        source_format = STANDARD_SOURCE_FORMAT
        strict_error: Exception | None = None
        try:
            dataset = stable.pydicom.dcmread(
                source_path, stop_before_pixels=True, force=False
            )
        except Exception as exc:
            strict_error = exc
            if (
                source_path.suffix.lower() != ".dcm"
                or not isinstance(exc, InvalidDicomError)
            ):
                status = (
                    "DICOM读取失败"
                    if source_path.suffix.lower() == ".dcm"
                    else "跳过非DICOM"
                )
                audits.append(
                    {
                        **stable._audit_base(plan),
                        "状态": status,
                        "源文件": str(source_path),
                        "验证结果": "失败" if status == "DICOM读取失败" else "不适用",
                        "说明": str(exc),
                    }
                )
                blocking = blocking or status == "DICOM读取失败"
                continue
            try:
                dataset = _read_headerless_dataset(
                    source_path, stop_before_pixels=True
                )
                source_format = HEADERLESS_SOURCE_FORMAT
            except Exception as compatibility_error:
                blocking = True
                audits.append(
                    {
                        **stable._audit_base(plan),
                        "状态": "无文件头兼容校验失败",
                        "源文件": str(source_path),
                        "验证结果": "失败",
                        "说明": (
                            f"严格读取失败: {type(strict_error).__name__}: {strict_error}；"
                            f"兼容读取失败: {type(compatibility_error).__name__}: "
                            f"{compatibility_error}"
                        ),
                    }
                )
                continue

        study_uid = text(dataset.get("StudyInstanceUID"))
        series_uid = text(dataset.get("SeriesInstanceUID"))
        sop_uid = text(dataset.get("SOPInstanceUID"))
        sop_class_uid = text(dataset.get("SOPClassUID"))
        patient_id = text(dataset.get("PatientID"))
        problems: list[str] = []
        if study_uid != plan.study_uid:
            problems.append(
                f"头信息StudyInstanceUID={study_uid or '<空>'}与清单不一致"
            )
        if not stable._valid_uid(series_uid):
            problems.append("SeriesInstanceUID缺失或无效")
        if not stable._valid_uid(sop_uid):
            problems.append("SOPInstanceUID缺失或无效")
        if source_format == HEADERLESS_SOURCE_FORMAT:
            if not stable._valid_uid(study_uid):
                problems.append("StudyInstanceUID缺失或无效")
            if not stable._valid_uid(sop_class_uid):
                problems.append("SOPClassUID缺失或无效，无法生成标准File Meta")
            problems.extend(
                _headerless_path_problems(
                    source_path,
                    study_directory,
                    study_uid,
                    series_uid,
                    sop_uid,
                )
            )
        if problems:
            blocking = True
            audits.append(
                {
                    **stable._audit_base(plan),
                    "状态": (
                        "无文件头兼容校验失败"
                        if source_format == HEADERLESS_SOURCE_FORMAT
                        else "UID校验失败"
                    ),
                    "SeriesInstanceUID": series_uid,
                    "SOPInstanceUID": sop_uid,
                    "源文件": str(source_path),
                    "源文件格式": source_format,
                    "源文件格式问题": _source_format_issue(
                        FastSourceFile(
                            source_path,
                            study_uid,
                            series_uid,
                            sop_uid,
                            patient_id,
                            source_format,
                        )
                    ),
                    "验证结果": "失败",
                    "说明": "；".join(problems),
                }
            )
            continue
        files.append(
            FastSourceFile(
                source_path,
                study_uid,
                series_uid,
                sop_uid,
                patient_id,
                source_format,
            )
        )

    patient_ids = {item.patient_id for item in files if item.patient_id}
    if len(patient_ids) > 1:
        audits.append(
            _multiple_patient_id_audit(
                plan,
                study_directory,
                len(patient_ids),
                allow_multiple_patient_ids,
            )
        )
        blocking = blocking or not allow_multiple_patient_ids
    if not files:
        blocking = True
        audits.append(
            {
                **stable._audit_base(plan),
                "状态": "Study无可用DICOM",
                "源文件": str(study_directory),
                "验证结果": "失败",
                "说明": "Study目录中没有通过严格或无文件头兼容校验的DICOM文件",
            }
        )
    if blocking:
        return [], audits
    return files, audits


def _inspect_study_preserve(
    plan: stable.StudyPlan,
    study_directory: Path,
    *,
    allow_multiple_patient_ids: bool = False,
) -> tuple[list[stable.SourceFile], list[dict[str, Any]]]:
    files: list[stable.SourceFile] = []
    audits: list[dict[str, Any]] = []
    blocking = False
    candidate_paths = sorted(
        path for path in study_directory.rglob("*") if path.is_file()
    )
    if not candidate_paths:
        return [], [
            {
                **stable._audit_base(plan),
                "状态": "Study为空",
                "源文件": str(study_directory),
                "验证结果": "失败",
                "说明": "Study目录内没有文件",
            }
        ]

    for source_path in candidate_paths:
        try:
            dataset = stable.pydicom.dcmread(
                source_path, stop_before_pixels=True, force=False
            )
        except Exception as exc:
            status = (
                "DICOM读取失败"
                if source_path.suffix.lower() == ".dcm"
                else "跳过非DICOM"
            )
            audits.append(
                {
                    **stable._audit_base(plan),
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
            problems.append(
                f"头信息StudyInstanceUID={study_uid or '<空>'}与清单不一致"
            )
        study_problem = _uid_path_problem(study_uid)
        series_problem = _uid_path_problem(series_uid)
        sop_problem = _uid_path_problem(sop_uid)
        if study_problem:
            problems.append(f"StudyInstanceUID无法安全生成路径：{study_problem}")
        if series_problem:
            problems.append(f"SeriesInstanceUID无法安全生成路径：{series_problem}")
        if sop_problem:
            problems.append(f"SOPInstanceUID无法安全生成路径：{sop_problem}")
        if problems:
            blocking = True
            audits.append(
                {
                    **stable._audit_base(plan),
                    "状态": "UID路径不可用",
                    "SeriesInstanceUID": series_uid,
                    "SOPInstanceUID": sop_uid,
                    "源文件": str(source_path),
                    "验证结果": "失败",
                    "说明": "；".join(problems),
                }
            )
            continue
        files.append(
            stable.SourceFile(
                source_path, study_uid, series_uid, sop_uid, patient_id
            )
        )

    patient_ids = {item.patient_id for item in files if item.patient_id}
    if len(patient_ids) > 1:
        audits.append(
            _multiple_patient_id_audit(
                plan,
                study_directory,
                len(patient_ids),
                allow_multiple_patient_ids,
            )
        )
        blocking = blocking or not allow_multiple_patient_ids
    if not files:
        blocking = True
        audits.append(
            {
                **stable._audit_base(plan),
                "状态": "Study无可用DICOM",
                "源文件": str(study_directory),
                "验证结果": "失败",
                "说明": "Study目录中没有可安全路由的DICOM文件",
            }
        )
    if blocking:
        return [], audits
    return files, audits


def _inspect_one_study(
    plan: stable.StudyPlan,
    directories: Sequence[Path],
    uid_policy: str,
    headerless_policy: str,
    confirmed_multi_patient_studies: frozenset[str] = frozenset(),
) -> tuple[
    stable.StudyPlan,
    list[stable.SourceFile | FastSourceFile],
    list[dict[str, Any]],
]:
    if len(directories) != 1:
        return plan, [], [_study_directory_error(plan, directories)]
    allow_multiple_patient_ids = (
        plan.study_uid in confirmed_multi_patient_studies
    )
    if headerless_policy == "allow":
        files, audits = _inspect_study_allow_headerless(
            plan,
            directories[0],
            allow_multiple_patient_ids=allow_multiple_patient_ids,
        )
    elif uid_policy == "preserve":
        files, audits = _inspect_study_preserve(
            plan,
            directories[0],
            allow_multiple_patient_ids=allow_multiple_patient_ids,
        )
    else:
        files, audits = stable.inspect_study(
            plan,
            directories[0],
            allow_multiple_patient_ids=allow_multiple_patient_ids,
        )
        if allow_multiple_patient_ids:
            for row in audits:
                if text(row.get("状态")) == "Study多PatientID人工确认":
                    row["多PatientID人工确认"] = (
                        "是（命令行按StudyUID显式确认同一自然人）"
                    )
                    row["说明"] = (
                        text(row.get("说明"))
                        + "；参数=--confirmed-multi-patient-study"
                    )
    return plan, files, [_with_engine(row) for row in audits]


def _write_dataset_temp_preserving_uids(
    dataset: Any,
    directory: Path | None,
) -> Path:
    """Use the old writer when UID-neutral, otherwise preserve file-meta UIDs.

    ``enforce_file_format=True`` repairs MediaStorageSOPInstanceUID from
    SOPInstanceUID when they differ.  Compatibility mode switches writers only
    where enforcement could add or change a UID, keeping normal output bytes
    compatible with v1.0.1 while retaining all original UID logical values.
    """
    file_meta = dataset.file_meta
    meta_class = text(file_meta.get("MediaStorageSOPClassUID")) if file_meta else ""
    meta_instance = (
        text(file_meta.get("MediaStorageSOPInstanceUID")) if file_meta else ""
    )
    data_class = text(dataset.get("SOPClassUID"))
    data_instance = text(dataset.get("SOPInstanceUID"))
    transfer_syntax = text(file_meta.get("TransferSyntaxUID")) if file_meta else ""
    implementation_class = (
        text(file_meta.get("ImplementationClassUID")) if file_meta else ""
    )
    enforced_write_is_uid_neutral = bool(
        meta_class
        and meta_instance
        and transfer_syntax
        and implementation_class
        and (not data_class or meta_class == data_class)
        and (not data_instance or meta_instance == data_instance)
    )
    if enforced_write_is_uid_neutral:
        # Keep the exact v1.0.1 serialization path for normal files so an
        # existing valid target remains SHA-identical instead of becoming a
        # false conflict merely because compatibility mode was enabled.
        return stable._write_dataset_temp(dataset, directory)

    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=".anon.", suffix=".part", dir=directory)
    os.close(handle)
    preserved = Path(name)
    try:
        try:
            dataset.save_as(preserved, enforce_file_format=False)
        except TypeError:  # pydicom 2.x compatibility
            dataset.save_as(preserved, write_like_original=True)
    except Exception:
        preserved.unlink(missing_ok=True)
        raise
    return preserved


def _normalize_headerless_file_meta(dataset: Any) -> None:
    sop_class_uid = text(dataset.get("SOPClassUID"))
    sop_instance_uid = text(dataset.get("SOPInstanceUID"))
    if not stable._valid_uid(sop_class_uid):
        raise ValueError("SOPClassUID缺失或无效，不能生成标准File Meta")
    if not stable._valid_uid(sop_instance_uid):
        raise ValueError("SOPInstanceUID缺失或无效，不能生成标准File Meta")
    file_meta = FileMetaDataset()
    file_meta.FileMetaInformationVersion = b"\x00\x01"
    file_meta.MediaStorageSOPClassUID = sop_class_uid
    file_meta.MediaStorageSOPInstanceUID = sop_instance_uid
    file_meta.TransferSyntaxUID = ImplicitVRLittleEndian
    file_meta.ImplementationClassUID = PYDICOM_IMPLEMENTATION_UID
    dataset.file_meta = file_meta
    dataset.preamble = b"\x00" * 128
    dataset.is_implicit_VR = True
    dataset.is_little_endian = True


def _process_file_headerless(
    plan: stable.StudyPlan,
    source: FastSourceFile,
    output_root: Path,
    control_dir: Path,
    center_aliases: tuple[str, ...],
    execute: bool,
) -> dict[str, Any]:
    target = _target_for(plan, source, output_root)
    base = {
        **stable._audit_base(plan),
        "SeriesInstanceUID": source.series_uid,
        "SOPInstanceUID": source.sop_uid,
        "源文件": str(source.path),
        "目标文件": str(target),
        "源UID问题": "",
        "源文件格式": HEADERLESS_SOURCE_FORMAT,
        "源文件格式问题": HEADERLESS_SOURCE_ISSUE,
    }
    temporary: Path | None = None
    try:
        dataset = _read_headerless_dataset(
            source.path, stop_before_pixels=False
        )
        if text(dataset.get("StudyInstanceUID")) != source.study_uid:
            raise ValueError("处理阶段StudyInstanceUID与预检结果不一致")
        if text(dataset.get("SeriesInstanceUID")) != source.series_uid:
            raise ValueError("处理阶段SeriesInstanceUID与预检结果不一致")
        if text(dataset.get("SOPInstanceUID")) != source.sop_uid:
            raise ValueError("处理阶段SOPInstanceUID与预检结果不一致")

        _normalize_headerless_file_meta(dataset)
        before = {
            "patient_ids": stable._snapshot(
                dataset, lambda element: element.tag == stable.PATIENT_ID
            ),
            "dates": stable._snapshot(
                dataset,
                lambda element: element.VR in stable.DATE_TIME_VRS
                and element.tag != stable.PATIENT_BIRTH_DATE,
            ),
            "uids": stable._snapshot(dataset, lambda element: element.VR == "UI"),
            "pixel_hash": stable._pixel_hash(dataset),
        }
        counts, retained_during_edit = stable.anonymize_dataset(
            dataset, center_aliases
        )
        temporary = stable._write_dataset_temp(
            dataset, target.parent if execute else None
        )
        reopened = stable.pydicom.dcmread(temporary, force=False)
        valid, verification, protected_residue = stable.verify_dataset(
            before, reopened, center_aliases
        )
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
        format_note = (
            "；源文件缺少DICM/File Meta，已按隐式VR小端读取并规范化为标准DICOM"
        )
        if target.exists():
            same_target = stable._sha256(target) == stable._sha256(temporary)
            if not execute and same_target:
                status = "计划跳过重复相同"
                message = (
                    "目标已存在且匿名化文件SHA-256相同；预览模式未落盘"
                    + retained_note
                    + format_note
                )
            elif not execute:
                status = "计划UID冲突"
                message = (
                    "目标UID路径已存在但文件内容不同；执行时不会覆盖正式交付文件"
                    + retained_note
                    + format_note
                )
            elif same_target:
                status = "重复相同"
                message = (
                    "目标已存在且匿名化文件SHA-256相同，保留原文件"
                    + retained_note
                    + format_note
                )
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
                digest = stable._sha256(temporary)[:12]
                conflict = conflict_dir / f"{source.sop_uid}.{digest}.conflict.dcm"
                if not conflict.exists():
                    os.replace(temporary, conflict)
                    temporary = None
                status = "UID冲突"
                message = (
                    f"正式交付文件未覆盖；差异文件保存在内部控制目录: {conflict}"
                    + retained_note
                    + format_note
                )
        elif not execute:
            status = "计划写入"
            message = "验证通过；预览模式未落盘" + retained_note + format_note
        else:
            os.replace(temporary, target)
            temporary = None
            status = "已写入"
            message = "匿名化及回读验证通过" + retained_note + format_note

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


def _process_file_preserving_uids(
    plan: stable.StudyPlan,
    source: stable.SourceFile,
    output_root: Path,
    control_dir: Path,
    center_aliases: tuple[str, ...],
    execute: bool,
) -> dict[str, Any]:
    """Apply the established edits while preserving every original UI value."""
    target = _target_for(plan, source, output_root)
    base = {
        **stable._audit_base(plan),
        "SeriesInstanceUID": source.series_uid,
        "SOPInstanceUID": source.sop_uid,
        "源文件": str(source.path),
        "目标文件": str(target),
    }
    temporary: Path | None = None
    source_uid_issues = ""
    try:
        dataset = stable.pydicom.dcmread(source.path, force=False)
        source_uid_issues = _source_uid_issues(dataset)
        before = {
            "patient_ids": stable._snapshot(
                dataset, lambda element: element.tag == stable.PATIENT_ID
            ),
            "dates": stable._snapshot(
                dataset,
                lambda element: element.VR in stable.DATE_TIME_VRS
                and element.tag != stable.PATIENT_BIRTH_DATE,
            ),
            "uids": stable._snapshot(dataset, lambda element: element.VR == "UI"),
            "pixel_hash": stable._pixel_hash(dataset),
        }
        counts, retained_during_edit = stable.anonymize_dataset(
            dataset, center_aliases
        )
        temporary = _write_dataset_temp_preserving_uids(
            dataset, target.parent if execute else None
        )
        reopened = stable.pydicom.dcmread(temporary, force=False)
        valid, verification, protected_residue = stable.verify_dataset(
            before, reopened, center_aliases
        )
        retained = sorted(set(retained_during_edit + protected_residue))
        if not valid:
            return {
                **base,
                "源UID问题": source_uid_issues,
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
        uid_note = (
            "；源UID问题已原值保留并记录" if source_uid_issues else ""
        )
        if target.exists():
            same_target = stable._sha256(target) == stable._sha256(temporary)
            if not execute and same_target:
                status = "计划跳过重复相同"
                message = (
                    "目标已存在且匿名化文件SHA-256相同；预览模式未落盘"
                    + retained_note
                    + uid_note
                )
            elif not execute:
                status = "计划UID冲突"
                message = (
                    "目标UID路径已存在但文件内容不同；执行时不会覆盖正式交付文件"
                    + retained_note
                    + uid_note
                )
            elif same_target:
                status = "重复相同"
                message = (
                    "目标已存在且匿名化文件SHA-256相同，保留原文件"
                    + retained_note
                    + uid_note
                )
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
                digest = stable._sha256(temporary)[:12]
                conflict = conflict_dir / f"{source.sop_uid}.{digest}.conflict.dcm"
                if not conflict.exists():
                    os.replace(temporary, conflict)
                    temporary = None
                status = "UID冲突"
                message = (
                    f"正式交付文件未覆盖；差异文件保存在内部控制目录: {conflict}"
                    + retained_note
                    + uid_note
                )
        elif not execute:
            status = "计划写入"
            message = "验证通过；预览模式未落盘" + retained_note + uid_note
        else:
            os.replace(temporary, target)
            temporary = None
            status = "已写入"
            message = "匿名化及回读验证通过" + retained_note + uid_note

        return {
            **base,
            "源UID问题": source_uid_issues,
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
            "源UID问题": source_uid_issues,
            "状态": "处理失败",
            "验证结果": "失败",
            "说明": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _process_target_group(
    plan: stable.StudyPlan,
    sources: Sequence[stable.SourceFile | FastSourceFile],
    output_root: Path,
    control_dir: Path,
    aliases: tuple[str, ...],
    execute: bool,
    uid_policy: str,
) -> list[dict[str, Any]]:
    # Sources with one destination UID are deliberately sequential inside a
    # worker.  This preserves the stable engine's duplicate/conflict behavior
    # and avoids concurrent replacement of the same target.
    rows: list[dict[str, Any]] = []
    for source in sources:
        if _source_format(source) == HEADERLESS_SOURCE_FORMAT:
            row = _process_file_headerless(
                plan,
                source,
                output_root,
                control_dir,
                aliases,
                execute,
            )
        elif uid_policy == "preserve":
            row = _process_file_preserving_uids(
                plan,
                source,
                output_root,
                control_dir,
                aliases,
                execute,
            )
        else:
            row = stable.process_file(
                plan,
                source,
                output_root,
                control_dir,
                aliases,
                execute,
            )
        row["源文件格式"] = _source_format(source)
        row["源文件格式问题"] = _source_format_issue(source)
        rows.append(_with_engine(row))
    return rows


def _process_target_batch(
    batch: Sequence[
        tuple[stable.StudyPlan, list[stable.SourceFile | FastSourceFile]]
    ],
    output_root: Path,
    control_dir: Path,
    aliases: tuple[str, ...],
    execute: bool,
    uid_policy: str,
) -> list[dict[str, Any]]:
    _configure_uid_warnings(uid_policy)
    rows: list[dict[str, Any]] = []
    for plan, sources in batch:
        rows.extend(
            _process_target_group(
                plan,
                sources,
                output_root,
                control_dir,
                aliases,
                execute,
                uid_policy,
            )
        )
    return rows


def _unexpected_group_failure(
    plan: stable.StudyPlan,
    sources: Sequence[stable.SourceFile],
    output_root: Path,
    exc: Exception,
) -> list[dict[str, Any]]:
    return [
        _with_engine(
            {
                **stable._audit_base(plan),
                "状态": "高速处理失败",
                "SeriesInstanceUID": source.series_uid,
                "SOPInstanceUID": source.sop_uid,
                "源文件": str(source.path),
                "目标文件": str(_target_for(plan, source, output_root)),
                "验证结果": "失败",
                "说明": f"{type(exc).__name__}: {exc}",
            }
        )
        for source in sources
    ]


def _iter_group_results(
    groups: Sequence[tuple[stable.StudyPlan, list[stable.SourceFile]]],
    output_root: Path,
    control_dir: Path,
    aliases: tuple[str, ...],
    execute: bool,
    processes: int,
    max_in_flight: int,
    batch_targets: int,
    backend: str,
    uid_policy: str,
) -> Iterator[list[dict[str, Any]]]:
    batches = [
        groups[start : start + batch_targets]
        for start in range(0, len(groups), batch_targets)
    ]
    if processes == 1:
        for batch in batches:
            yield _process_target_batch(
                batch, output_root, control_dir, aliases, execute, uid_policy
            )
        return

    batch_iterator = iter(batches)
    executor_class = ThreadPoolExecutor if backend == "thread" else ProcessPoolExecutor
    with executor_class(max_workers=processes) as executor:
        pending: dict[
            Any, Sequence[tuple[stable.StudyPlan, list[stable.SourceFile]]]
        ] = {}

        def fill() -> None:
            while len(pending) < max_in_flight:
                try:
                    batch = next(batch_iterator)
                except StopIteration:
                    break
                future = executor.submit(
                    _process_target_batch,
                    batch,
                    output_root,
                    control_dir,
                    aliases,
                    execute,
                    uid_policy,
                )
                pending[future] = batch

        fill()
        while pending:
            completed, _not_done = wait(tuple(pending), return_when=FIRST_COMPLETED)
            for future in completed:
                batch = pending.pop(future)
                try:
                    yield future.result()
                except Exception as exc:  # includes a failed worker process
                    rows: list[dict[str, Any]] = []
                    for plan, sources in batch:
                        rows.extend(
                            _unexpected_group_failure(
                                plan, sources, output_root, exc
                            )
                        )
                    yield rows
            fill()


def _legacy_layout_guard(output_root: Path) -> None:
    if not output_root.is_dir():
        return
    legacy_directories = sorted(
        path.name
        for path in output_root.iterdir()
        if path.is_dir() and stable.LEGACY_TARGET_PATTERN.fullmatch(path.name)
    )
    if legacy_directories:
        display = "、".join(legacy_directories[:5])
        if len(legacy_directories) > 5:
            display += f"等{len(legacy_directories)}个"
        raise ValueError(
            "交付根目录存在v1.3旧层级目录，不能与v1.4混合: "
            f"{display}；请保留旧数据并改用新的空交付目录，或人工核对后再迁移"
        )


def run(args: argparse.Namespace) -> FastRunResult:
    source_center = Path(args.source_center).resolve()
    output_root = Path(args.output_root).resolve()
    control_dir = (
        Path(args.control_dir).resolve()
        if args.control_dir
        else output_root.parent / "02.内部控制文件"
    )
    if not source_center.is_dir():
        raise FileNotFoundError(f"中心转存目录不存在: {source_center}")
    stable._assert_separate_paths(source_center, output_root, control_dir)

    scan_workers = int(getattr(args, "workers", min(4, os.cpu_count() or 1)))
    processes = int(getattr(args, "processes", min(4, os.cpu_count() or 1)))
    backend = str(getattr(args, "backend", "thread"))
    max_in_flight = int(getattr(args, "max_in_flight", 0))
    requested_batch_targets = int(getattr(args, "batch_targets", 0))
    progress_every = int(getattr(args, "progress_every", 100))
    uid_policy = str(getattr(args, "uid_policy", "strict"))
    headerless_policy = str(
        getattr(args, "headerless_policy", "strict")
    )
    confirmed_multi_patient_studies = tuple(
        dict.fromkeys(
            text(value)
            for value in getattr(
                args, "confirmed_multi_patient_study", []
            )
            if text(value)
        )
    )
    if scan_workers < 1 or scan_workers > 32:
        raise ValueError("--workers必须在1到32之间")
    if processes < 1 or processes > 16:
        raise ValueError("--processes必须在1到16之间")
    if backend not in {"thread", "process"}:
        raise ValueError("--backend必须是thread或process")
    if max_in_flight < 0:
        raise ValueError("--max-in-flight不能小于0")
    if requested_batch_targets < 0 or requested_batch_targets > 512:
        raise ValueError("--batch-targets必须在0到512之间")
    if progress_every < 0:
        raise ValueError("--progress-every不能小于0")
    if uid_policy not in UID_POLICIES:
        raise ValueError("--uid-policy必须是strict或preserve")
    if headerless_policy not in HEADERLESS_POLICIES:
        raise ValueError("--headerless-policy必须是strict或allow")
    if uid_policy == "preserve" and headerless_policy == "allow":
        raise ValueError(
            "无文件头兼容模式当前只支持--uid-policy strict；"
            "不能同时使用--uid-policy preserve"
        )
    invalid_confirmed_studies = [
        value
        for value in confirmed_multi_patient_studies
        if not stable._valid_uid(value)
    ]
    if invalid_confirmed_studies:
        raise ValueError(
            "--confirmed-multi-patient-study必须是有效StudyInstanceUID: "
            + "、".join(invalid_confirmed_studies)
        )
    if max_in_flight == 0:
        max_in_flight = max(2, processes * 2)
    _configure_uid_warnings(uid_policy)

    plans, rejected = stable.load_plans(args.study_list, args.sheet)
    if not plans:
        raise ValueError("清单中没有可进入匿名化的Study")
    planned_study_uids = {plan.study_uid for plan in plans}
    unknown_confirmed_studies = sorted(
        set(confirmed_multi_patient_studies) - planned_study_uids
    )
    if unknown_confirmed_studies:
        raise ValueError(
            "以下人工确认StudyUID不在本次入组清单中，未执行: "
            + "、".join(unknown_confirmed_studies)
        )
    _legacy_layout_guard(output_root)
    aliases = tuple(
        dict.fromkeys(
            [plan.center_name for plan in plans]
            + [source_center.name]
            + list(args.center_alias)
        )
    )
    execute = bool(args.execute)
    use_resume = execute and not bool(getattr(args, "no_resume", False))
    context = _checkpoint_context(
        aliases,
        uid_policy,
        headerless_policy,
        confirmed_multi_patient_studies,
    )
    accepted_contexts = {context}
    accepted_contexts.add(
        _v1_2_checkpoint_context(aliases, uid_policy, headerless_policy)
    )
    accepted_contexts.add(_v1_1_checkpoint_context(aliases, uid_policy))
    if uid_policy == "strict":
        accepted_contexts.add(_previous_checkpoint_context(aliases))
    checkpoint_path = _checkpoint_path(source_center, output_root, control_dir)
    checkpoint_entries = (
        _load_checkpoint(checkpoint_path, accepted_contexts) if use_resume else {}
    )

    mode = "执行" if execute else "预览"
    stable._progress(
        f"[高速准备] 模式={mode}，Study={len(plans)}，"
        f"预检线程={scan_workers}，DICOM并发={backend}:{processes}，"
        f"UID策略={uid_policy}，无文件头策略={headerless_policy}，"
        f"多PatientID人工确认Study={len(confirmed_multi_patient_studies)}"
    )
    if confirmed_multi_patient_studies:
        stable._progress(
            "[身份确认例外] 仅放行命令中精确指定且实际检测到多个PatientID的Study；"
            "每个DICOM继续保留并验证原PatientID"
        )
    if uid_policy == "preserve":
        stable._progress(
            "[UID兼容] 所有UID原值保留；超长、连续点及文件头/数据集UID不一致"
            "只记入问题清单，不阻断。缺失或路径不安全UID仍阻断"
        )
    if headerless_policy == "allow":
        stable._progress(
            "[无文件头兼容] 仅接受缺少DICM/File Meta、UID与转存路径一致的"
            "隐式VR小端数据集；输出补齐标准File Meta并完整回读验证"
        )
    if use_resume:
        stable._progress(
            f"[断点] 已载入{len(checkpoint_entries)}条同版本完成记录；"
            "源或目标有变化时会自动重新处理"
        )
    elif execute:
        stable._progress("[断点] 已关闭，本次强制处理并验证全部文件")
    else:
        stable._progress("[断点] 预览模式不使用或写入断点状态")
    stable._progress("[提示] VR SH长度警告来自原始DICOM字段，不会单独导致程序中止")
    stable._progress(f"[扫描目录] 正在当前中心目录查找{len(plans)}个Study")
    located = stable.locate_study_directories(
        source_center, {plan.study_uid for plan in plans}
    )

    audits: list[dict[str, Any]] = [
        _with_engine(
            {
                "状态": "清单行无效",
                "StudyInstanceUID": item["study_uid"],
                "验证结果": "失败",
                "说明": f"工作表第{item['row']}行: {item['message']}",
            }
        )
        for item in rejected
    ]
    valid_files_by_plan: list[
        tuple[
            stable.StudyPlan,
            list[stable.SourceFile | FastSourceFile],
        ]
    ] = []
    confirmed_multi_patient_set = frozenset(
        confirmed_multi_patient_studies
    )
    confirmed_multi_patient_hits: set[str] = set()

    with ThreadPoolExecutor(max_workers=scan_workers) as executor:
        futures = {
            executor.submit(
                _inspect_one_study,
                plan,
                located.get(plan.study_uid, []),
                uid_policy,
                headerless_policy,
                confirmed_multi_patient_set,
            ): plan
            for plan in plans
        }
        inspected = 0
        for future in as_completed(futures):
            plan = futures[future]
            try:
                returned_plan, files, study_audits = future.result()
            except Exception as exc:
                returned_plan, files = plan, []
                study_audits = [
                    _with_engine(
                        {
                            **stable._audit_base(plan),
                            "状态": "高速扫描失败",
                            "验证结果": "失败",
                            "说明": f"{type(exc).__name__}: {exc}",
                        }
                    )
                ]
            audits.extend(study_audits)
            if any(
                text(row.get("状态")) == "Study多PatientID人工确认"
                for row in study_audits
            ):
                confirmed_multi_patient_hits.add(returned_plan.study_uid)
            if files:
                valid_files_by_plan.append((returned_plan, files))
            inspected += 1
            stable._progress(
                f"[Study预检] 已完成{inspected}/{len(plans)}；"
                f"{returned_plan.anonymous_code}-{returned_plan.period} "
                f"可处理DICOM={len(files)}"
            )

    unused_confirmations = sorted(
        confirmed_multi_patient_set - confirmed_multi_patient_hits
    )
    if unused_confirmations:
        raise ValueError(
            "以下StudyUID未实际检测到多个PatientID，人工确认参数未生效，"
            "为避免错误放行已停止: "
            + "、".join(unused_confirmations)
        )

    valid_files_by_plan.sort(
        key=lambda item: (
            int(item[0].anonymous_code)
            if item[0].anonymous_code.isdigit()
            else 10**12,
            item[0].period,
            item[0].study_uid,
        )
    )
    total_files = sum(len(files) for _plan, files in valid_files_by_plan)
    study_totals = Counter(
        {
            plan.study_uid: len(files)
            for plan, files in valid_files_by_plan
        }
    )
    study_done: Counter[str] = Counter()
    completed_studies: set[str] = set()
    resumed_files = 0
    grouped: dict[
        str,
        tuple[
            stable.StudyPlan,
            list[stable.SourceFile | FastSourceFile],
        ],
    ] = {}

    for plan, files in valid_files_by_plan:
        for source in files:
            target = _target_for(plan, source, output_root)
            key = _checkpoint_key(source.path, target)
            if use_resume and _can_resume(
                source,
                target,
                checkpoint_entries.get(key),
                requires_multi_patient_confirmation=(
                    plan.study_uid in confirmed_multi_patient_hits
                ),
            ):
                audits.append(
                    _resume_audit(
                        plan, source, target, checkpoint_entries[key]
                    )
                )
                resumed_files += 1
                study_done[plan.study_uid] += 1
                continue
            target_key = str(target.resolve())
            if target_key not in grouped:
                grouped[target_key] = (plan, [])
            grouped[target_key][1].append(source)

    groups = list(grouped.values())
    batch_targets = requested_batch_targets or min(
        64,
        max(1, math.ceil(len(groups) / max(1, processes * 8))),
    )
    pending_files = sum(len(sources) for _plan, sources in groups)
    stable._progress(
        f"[处理计划] DICOM={total_files}，断点跳过={resumed_files}，"
        f"本次需处理={pending_files}，唯一目标UID={len(groups)}，"
        f"每批最多={batch_targets}个目标UID"
    )

    def note_progress(rows: Iterable[dict[str, Any]]) -> None:
        for row in rows:
            study_uid = text(row.get("StudyInstanceUID"))
            if study_uid in study_totals:
                study_done[study_uid] += 1
                if (
                    study_done[study_uid] >= study_totals[study_uid]
                    and study_uid not in completed_studies
                ):
                    completed_studies.add(study_uid)
                    stable._progress(
                        f"[Study完成] {len(completed_studies)}/{len(study_totals)} "
                        f"StudyUID={study_uid} DICOM={study_totals[study_uid]}"
                    )

    for plan, _files in valid_files_by_plan:
        if (
            study_done[plan.study_uid] >= study_totals[plan.study_uid]
            and plan.study_uid not in completed_studies
        ):
            completed_studies.add(plan.study_uid)
            stable._progress(
                f"[Study完成] {len(completed_studies)}/{len(study_totals)} "
                f"StudyUID={plan.study_uid} DICOM={study_totals[plan.study_uid]}（全部断点跳过）"
            )

    processed_files = 0
    checkpoint_writer = (
        CheckpointWriter(checkpoint_path, context) if execute else None
    )
    if checkpoint_writer is not None:
        checkpoint_writer.__enter__()
    try:
        for rows in _iter_group_results(
            groups,
            output_root,
            control_dir,
            aliases,
            execute,
            processes,
            max_in_flight,
            batch_targets,
            backend,
            uid_policy,
        ):
            for row in rows:
                if text(row.get("StudyInstanceUID")) in confirmed_multi_patient_hits:
                    row["多PatientID人工确认"] = (
                        "是（命令行按StudyUID显式确认同一自然人）"
                    )
            audits.extend(rows)
            processed_files += len(rows)
            note_progress(rows)
            if checkpoint_writer is not None:
                for row in rows:
                    checkpoint_writer.append(row)
            current = resumed_files + processed_files
            if (
                progress_every
                and (current % progress_every < len(rows) or current == total_files)
            ):
                stable._progress(
                    f"[总体进度] 已完成{current}/{total_files}个DICOM，"
                    f"断点跳过={resumed_files}，实际处理={processed_files}"
                )
    finally:
        if checkpoint_writer is not None:
            checkpoint_writer.__exit__(None, None, None)

    for row in audits:
        row.setdefault("执行引擎", ENGINE_VERSION)
        row["UID处理策略"] = uid_policy
        row["无文件头策略"] = headerless_policy
        if text(row.get("StudyInstanceUID")) in confirmed_multi_patient_hits:
            row["多PatientID人工确认"] = (
                "是（命令行按StudyUID显式确认同一自然人）"
            )
        else:
            row.setdefault("多PatientID人工确认", "")
        row.setdefault("源UID问题", "")
        row.setdefault("源文件格式", "")
        row.setdefault("源文件格式问题", "")
    audits.sort(key=stable._audit_sort_key)
    summary: dict[str, int] = {}
    for row in audits:
        status = text(row.get("状态"))
        summary[status] = summary.get(status, 0) + 1

    audit_path: Path | None = None
    verification_path: Path | None = None
    uid_issue_path: Path | None = None
    source_format_issue_path: Path | None = None
    if execute:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        audit_path = control_dir / f"匿名化高速版处理清单_{timestamp}.csv"
        stable._write_csv_atomic(audit_path, audits, FAST_AUDIT_FIELDS)
        verification_rows = [
            row for row in audits if text(row.get("状态")) in FAILED_STATUSES
        ]
        verification_path = control_dir / f"匿名化高速版验证报告_{timestamp}.csv"
        stable._write_csv_atomic(
            verification_path, verification_rows, FAST_AUDIT_FIELDS
        )
        uid_issue_rows = [row for row in audits if text(row.get("源UID问题"))]
        uid_issue_path = control_dir / f"匿名化高速版源UID问题清单_{timestamp}.csv"
        stable._write_csv_atomic(uid_issue_path, uid_issue_rows, FAST_AUDIT_FIELDS)
        source_format_issue_rows = [
            row for row in audits if text(row.get("源文件格式问题"))
        ]
        source_format_issue_path = (
            control_dir / f"匿名化高速版源文件格式问题清单_{timestamp}.csv"
        )
        stable._write_csv_atomic(
            source_format_issue_path,
            source_format_issue_rows,
            FAST_AUDIT_FIELDS,
        )

    return FastRunResult(
        summary=summary,
        audits=audits,
        audit_path=audit_path,
        verification_path=verification_path,
        uid_issue_path=uid_issue_path,
        source_format_issue_path=source_format_issue_path,
        checkpoint_path=checkpoint_path if execute else None,
        resumed_files=resumed_files,
        processed_files=processed_files,
    )


def main(
    argv: list[str] | None = None,
    *,
    default_headerless_policy: str = "strict",
) -> int:
    args = build_parser(
        default_headerless_policy=default_headerless_policy
    ).parse_args(argv)
    try:
        result = run(args)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    print(f"业务规则版本: {stable.RULE_VERSION}")
    print(f"执行引擎: {ENGINE_VERSION}")
    print(f"模式: {'执行' if args.execute else '预览'}")
    print(f"无文件头策略: {args.headerless_policy}")
    print(
        "多PatientID人工确认Study: "
        f"{len(set(args.confirmed_multi_patient_study))}"
    )
    for status, count in sorted(result.summary.items()):
        print(f"{status}: {count}")
    print(f"本次实际处理DICOM: {result.processed_files}")
    print(f"断点跳过DICOM: {result.resumed_files}")
    if result.audit_path:
        print(f"审计清单: {result.audit_path.resolve()}")
        print(f"验证报告: {result.verification_path.resolve()}")
        print(f"源UID问题清单: {result.uid_issue_path.resolve()}")
        print(f"源文件格式问题清单: {result.source_format_issue_path.resolve()}")
        print(f"断点状态: {result.checkpoint_path.resolve()}")
    else:
        print("预览模式未写入DICOM、控制文件或断点状态；确认后增加--execute。")
    return 2 if any(
        text(row.get("状态")) in FAILED_STATUSES for row in result.audits
    ) else 0


if __name__ == "__main__":
    raise SystemExit(main())
