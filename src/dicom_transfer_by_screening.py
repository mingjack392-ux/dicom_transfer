#!/usr/bin/env python3
"""按受试者筛选号安全转存 DICOM（Windows/Linux 通用）。

本脚本只负责两件事：

1. 在复制前检查每个受试者目录内的 DICOM 是否具有一致的患者身份；
2. 身份确认后转存为 ``筛选号/StudyUID/SeriesUID/SOPUID.dcm``；
3. 批量模式下逐个处理中心目录中的一级受试者目录，绝不跨目录合并身份。

默认仅预检。只有显式传入 ``--execute`` 才会复制文件。脚本不读取分中心表、
不生成影像-web、不计算时期，也不修改原始 DICOM。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import shutil
import sys
import threading
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence

import pydicom
from pydicom.config import IGNORE


# 部分设备会写出超过64字符但仍为数字点号格式的UID。pydicom默认把同一警告
# 打到终端；本脚本自行在转存清单中逐文件记录，因此关闭库级重复输出。
pydicom.config.settings.reading_validation_mode = IGNORE


RULE_VERSION = "2026.09.02-screening-transfer-v1.2"
COPY_CHUNK_SIZE = 1024 * 1024
SCAN_TAGS = [
    "PatientName",
    "PatientID",
    "PatientBirthDate",
    "PatientSex",
    "PatientAge",
    "InstitutionName",
    "StudyDate",
    "StudyInstanceUID",
    "SeriesInstanceUID",
    "SOPInstanceUID",
]
UID_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]+)*")
UID_PATH_CHAR_PATTERN = re.compile(r"[0-9.]+")
AGE_PATTERN = re.compile(r"(?P<value>\d{1,3})Y", re.IGNORECASE)
_TRANSFER_LOCKS = tuple(threading.Lock() for _ in range(257))


@dataclass
class DicomRecord:
    path: Path
    relative_path: str
    relative_hash: str
    is_dicom: bool
    unreadable: bool = False
    error: str = ""
    patient_name: str = ""
    patient_id: str = ""
    birth_date: str = ""
    sex: str = ""
    patient_age: str = ""
    institution: str = ""
    study_date: str = ""
    study_uid: str = ""
    series_uid: str = ""
    sop_uid: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass
class IdentityProfile:
    group_key: str
    files: int = 0
    patient_ids: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)
    birth_dates: set[str] = field(default_factory=set)
    sexes: set[str] = field(default_factory=set)
    birth_years: set[int] = field(default_factory=set)
    institutions: set[str] = field(default_factory=set)


@dataclass
class IdentityDecision:
    status: str
    reasons: list[str]
    evidence: list[str]
    profiles: list[IdentityProfile]

    @property
    def confirmed(self) -> bool:
        return self.status == "confirmed"


@dataclass
class TransferResult:
    record: DicomRecord
    destination_path: str
    status: str
    file_size: int = 0
    sha256: str = ""
    message: str = ""


@dataclass
class SubjectRunSummary:
    subject_id: str
    source_dir: str
    status: str
    identity_status: str
    dicom_files: int = 0
    copied: int = 0
    duplicate_same: int = 0
    conflicts: int = 0
    quarantined: int = 0
    errors: int = 0
    audit_path: str = ""
    message: str = ""


def text_value(value: Any) -> str:
    return "" if value is None else str(value).strip()


def normalized_token(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").replace("^", " ")
    return "".join(character for character in normalized.casefold() if character.isalnum())


def normalize_name(value: str) -> str:
    return normalized_token(value)


def normalize_date(value: str) -> str:
    digits = re.sub(r"[^0-9]", "", value or "")
    if len(digits) < 8:
        return ""
    candidate = digits[:8]
    try:
        datetime.strptime(candidate, "%Y%m%d")
    except ValueError:
        return ""
    return candidate


def normalize_sex(value: str) -> str:
    sex = text_value(value).upper()
    return sex if sex in {"M", "F", "O"} else ""


def normalize_age(value: str) -> str:
    match = AGE_PATTERN.fullmatch(text_value(value))
    return f"{int(match.group('value')):03d}Y" if match else ""


def estimate_birth_year(patient_age: str, study_date: str) -> Optional[int]:
    age = normalize_age(patient_age)
    date = normalize_date(study_date)
    if not age or not date:
        return None
    return int(date[:4]) - int(age[:3])


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


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def iter_source_files(source_root: Path, destination_root: Path) -> Iterator[Path]:
    # Windows 的 ``Path.resolve()`` 可能把一侧变成 8.3 短路径、另一侧仍由
    # ``os.walk`` 返回长路径，进而破坏 relative_to。这里保留绝对词法路径，
    # 真正的包含关系校验仍由 validate_paths 使用解析后的路径完成。
    source_root = Path(os.path.abspath(source_root))
    destination_root = Path(os.path.abspath(destination_root))
    for current, dirnames, filenames in os.walk(source_root, topdown=True):
        current_path = Path(current)
        retained: list[str] = []
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


def _has_dicom_prefix(stream: Any) -> bool:
    header = stream.read(132)
    stream.seek(0)
    return len(header) >= 132 and header[128:132] == b"DICM"


def _uid_warnings(name: str, value: str) -> list[str]:
    if not value:
        return []
    warnings: list[str] = []
    if not UID_PATTERN.fullmatch(value):
        warnings.append(f"{name}格式异常")
    if len(value) > 64:
        warnings.append(f"{name}长度{len(value)}超过64")
    return warnings


def _uid_path_problem(name: str, value: str) -> str:
    """返回UID无法原样作为安全路径组件的原因。

    DICOM标准UID不允许连续点，但部分设备确实会产生这类可读文件。连续点位于
    组件内部时不会改变Windows/Linux路径层级，因此保留原值转存并由
    ``_uid_warnings`` 记录规范警告。这里仅阻止会改变路径语义或超出常见文件系统
    组件限制的值。
    """
    if not UID_PATH_CHAR_PATTERN.fullmatch(value):
        return f"{name}包含非数字点号字符"
    if value.startswith(".") or value.endswith("."):
        return f"{name}以点开头或结尾"
    if len(value) > 240:
        return f"{name}长度{len(value)}超过安全路径限制240"
    return ""


def scan_one_file(path: Path, source_root: Path) -> DicomRecord:
    relative_path = str(path.relative_to(source_root))
    relative_hash = stable_hash(relative_path.replace("\\", "/"))
    prefix = False
    try:
        with path.open("rb") as stream:
            prefix = _has_dicom_prefix(stream)
            dataset = pydicom.dcmread(
                stream,
                force=True,
                stop_before_pixels=True,
                specific_tags=SCAN_TAGS,
            )
    except Exception as exc:
        likely_dicom = prefix or path.suffix.casefold() in {".dcm", ".dicom"}
        return DicomRecord(
            path=path,
            relative_path=relative_path,
            relative_hash=relative_hash,
            is_dicom=likely_dicom,
            unreadable=likely_dicom,
            error=str(exc),
        )

    study_uid = text_value(getattr(dataset, "StudyInstanceUID", ""))
    series_uid = text_value(getattr(dataset, "SeriesInstanceUID", ""))
    sop_uid = text_value(getattr(dataset, "SOPInstanceUID", ""))
    identity_values = [
        getattr(dataset, "PatientName", None),
        getattr(dataset, "PatientID", None),
        getattr(dataset, "PatientBirthDate", None),
        getattr(dataset, "PatientSex", None),
    ]
    is_dicom = prefix or bool(study_uid or series_uid or sop_uid) or sum(
        bool(text_value(value)) for value in identity_values
    ) >= 2
    if not is_dicom:
        return DicomRecord(path, relative_path, relative_hash, False)

    record_warnings = [
        *_uid_warnings("StudyInstanceUID", study_uid),
        *_uid_warnings("SeriesInstanceUID", series_uid),
        *_uid_warnings("SOPInstanceUID", sop_uid),
    ]
    return DicomRecord(
        path=path,
        relative_path=relative_path,
        relative_hash=relative_hash,
        is_dicom=True,
        patient_name=text_value(getattr(dataset, "PatientName", "")),
        patient_id=text_value(getattr(dataset, "PatientID", "")),
        birth_date=normalize_date(text_value(getattr(dataset, "PatientBirthDate", ""))),
        sex=normalize_sex(text_value(getattr(dataset, "PatientSex", ""))),
        patient_age=normalize_age(text_value(getattr(dataset, "PatientAge", ""))),
        institution=text_value(getattr(dataset, "InstitutionName", "")),
        study_date=normalize_date(text_value(getattr(dataset, "StudyDate", ""))),
        study_uid=study_uid,
        series_uid=series_uid,
        sop_uid=sop_uid,
        warnings=record_warnings,
    )


def chunked(values: Iterable[Path], size: int) -> Iterator[list[Path]]:
    batch: list[Path] = []
    for value in values:
        batch.append(value)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def scan_directory(
    source_root: Path,
    destination_root: Path,
    *,
    workers: int = 4,
    progress_every: int = 500,
) -> list[DicomRecord]:
    records: list[DicomRecord] = []
    scanned = 0
    dicom_count = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        files = iter_source_files(source_root, destination_root)
        for batch in chunked(files, 500):
            for record in executor.map(
                lambda path: scan_one_file(path, source_root), batch
            ):
                records.append(record)
                scanned += 1
                if record.is_dicom:
                    dicom_count += 1
                if progress_every > 0 and scanned % progress_every == 0:
                    print(f"[身份预检] 已检查文件 {scanned}，识别DICOM {dicom_count}")
    print(f"[身份预检] 扫描完成：文件 {scanned}，DICOM {dicom_count}")
    return records


def identity_profile_key(record: DicomRecord) -> str:
    if record.study_uid:
        return f"Study:{record.study_uid}"
    return f"MissingStudy:{str(Path(record.relative_path).parent)}"


def build_identity_profiles(records: Sequence[DicomRecord]) -> list[IdentityProfile]:
    profiles: dict[str, IdentityProfile] = {}
    for record in records:
        if not record.is_dicom or record.unreadable:
            continue
        key = identity_profile_key(record)
        profile = profiles.setdefault(key, IdentityProfile(group_key=key))
        profile.files += 1
        if record.patient_id:
            profile.patient_ids.add(record.patient_id)
        name = normalize_name(record.patient_name)
        if name:
            profile.names.add(name)
        if record.birth_date:
            profile.birth_dates.add(record.birth_date)
        if record.sex:
            profile.sexes.add(record.sex)
        birth_year = estimate_birth_year(record.patient_age, record.study_date)
        if birth_year is not None:
            profile.birth_years.add(birth_year)
        institution = normalized_token(record.institution)
        if institution:
            profile.institutions.add(institution)
    return sorted(profiles.values(), key=lambda item: item.group_key)


def _profile_internal_conflicts(profile: IdentityProfile) -> list[str]:
    conflicts: list[str] = []
    for label, values in (
        ("姓名", profile.names),
        ("出生日期", profile.birth_dates),
        ("性别", profile.sexes),
    ):
        if len(values) > 1:
            conflicts.append(f"{profile.group_key}内部{label}存在多个值")
    return conflicts


def _profile_has_minimum_identity(profile: IdentityProfile) -> bool:
    return bool(profile.names) and bool(
        profile.birth_dates or profile.sexes or profile.birth_years
    )


def compare_profiles(
    left: IdentityProfile, right: IdentityProfile
) -> tuple[list[str], list[str], list[str]]:
    conflicts: list[str] = []
    evidence: list[str] = []
    insufficient: list[str] = []
    comparable = (
        ("姓名", left.names, right.names, True),
        ("出生日期", left.birth_dates, right.birth_dates, True),
        ("性别", left.sexes, right.sexes, True),
        ("出生年估算", left.birth_years, right.birth_years, False),
        ("机构", left.institutions, right.institutions, False),
    )
    for label, left_values, right_values, hard_conflict in comparable:
        if not left_values or not right_values:
            continue
        if left_values & right_values:
            evidence.append(label)
        elif hard_conflict:
            conflicts.append(
                f"{left.group_key} 与 {right.group_key} 的{label}冲突"
            )
    if not left.names or not right.names:
        insufficient.append(
            f"{left.group_key} 与 {right.group_key}缺少可比较姓名"
        )
    if len(set(evidence)) < 2:
        insufficient.append(
            f"{left.group_key} 与 {right.group_key}仅有{len(set(evidence))}项一致证据"
        )
    return conflicts, evidence, insufficient


def decide_identity(records: Sequence[DicomRecord]) -> IdentityDecision:
    dicom_records = [record for record in records if record.is_dicom]
    unreadable = [record for record in dicom_records if record.unreadable]
    profiles = build_identity_profiles(records)
    conflicts: list[str] = []
    insufficient: list[str] = []
    evidence: set[str] = set()

    if not dicom_records:
        insufficient.append("目录中没有识别到DICOM文件")
    if unreadable:
        insufficient.append(f"存在{len(unreadable)}个不可读DICOM，无法完成身份核验")
    if not profiles:
        insufficient.append("没有可用于身份核验的Study")

    for profile in profiles:
        conflicts.extend(_profile_internal_conflicts(profile))
        if not _profile_has_minimum_identity(profile):
            insufficient.append(
                f"{profile.group_key}缺少姓名及至少一项出生日期/性别/出生年证据"
            )

    for index, left in enumerate(profiles):
        for right in profiles[index + 1 :]:
            pair_conflicts, pair_evidence, pair_insufficient = compare_profiles(
                left, right
            )
            conflicts.extend(pair_conflicts)
            evidence.update(pair_evidence)
            insufficient.extend(pair_insufficient)

    if conflicts:
        return IdentityDecision("conflict", conflicts, sorted(evidence), profiles)
    if insufficient:
        return IdentityDecision("insufficient", insufficient, sorted(evidence), profiles)

    if len(profiles) == 1:
        profile = profiles[0]
        evidence.update(["姓名"])
        if profile.birth_dates:
            evidence.add("出生日期")
        if profile.sexes:
            evidence.add("性别")
        if profile.birth_years:
            evidence.add("出生年估算")
    return IdentityDecision("confirmed", [], sorted(evidence), profiles)


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
    source: Path, target: Path, *, durable: bool = False
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
        if source.stat().st_size != size:
            raise IOError("复制前后文件大小不一致")
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


def destination_for(
    record: DicomRecord, destination_root: Path, subject_id: str
) -> tuple[Path, str]:
    missing = [
        name
        for name, value in (
            ("StudyInstanceUID", record.study_uid),
            ("SeriesInstanceUID", record.series_uid),
            ("SOPInstanceUID", record.sop_uid),
        )
        if not value
    ]
    path_problems = [
        problem
        for name, value in (
            ("StudyInstanceUID", record.study_uid),
            ("SeriesInstanceUID", record.series_uid),
            ("SOPInstanceUID", record.sop_uid),
        )
        if value and (problem := _uid_path_problem(name, value))
    ]
    if missing or path_problems:
        reason_parts: list[str] = []
        if missing:
            reason_parts.append("缺少必要标签: " + ", ".join(missing))
        if path_problems:
            reason_parts.append("UID无法安全作为路径: " + ", ".join(path_problems))
        target = (
            destination_root
            / "_quarantine"
            / sanitize_component(subject_id)
            / "invalid_or_missing_uids"
            / f"{record.relative_hash}.dcm"
        )
        return target, "；".join(reason_parts)
    target = (
        destination_root
        / sanitize_component(subject_id)
        / record.study_uid
        / record.series_uid
        / f"{record.sop_uid}.dcm"
    )
    return target, "；".join(record.warnings)


def transfer_one(
    record: DicomRecord,
    destination_root: Path,
    subject_id: str,
    *,
    durable: bool = False,
) -> TransferResult:
    target, message = destination_for(record, destination_root, subject_id)
    quarantined = "_quarantine" in target.parts
    try:
        lock = _TRANSFER_LOCKS[hash(str(target).casefold()) % len(_TRANSFER_LOCKS)]
        with lock:
            status, actual_target, size, digest = copy_atomic_with_hash(
                record.path, target, durable=durable
            )
        if quarantined and status != "conflict":
            status = "quarantined"
        return TransferResult(
            record,
            str(actual_target),
            status,
            file_size=size,
            sha256=digest,
            message=message,
        )
    except Exception as exc:
        return TransferResult(record, str(target), "error", message=str(exc))


def execute_transfer(
    records: Sequence[DicomRecord],
    destination_root: Path,
    subject_id: str,
    *,
    copy_workers: int = 2,
    durable: bool = False,
    progress_every: int = 500,
) -> list[TransferResult]:
    transferable = [
        record for record in records if record.is_dicom and not record.unreadable
    ]
    results: list[TransferResult] = []
    with ThreadPoolExecutor(max_workers=max(1, copy_workers)) as executor:
        futures = executor.map(
            lambda record: transfer_one(
                record, destination_root, subject_id, durable=durable
            ),
            transferable,
        )
        for result in futures:
            results.append(result)
            if progress_every > 0 and len(results) % progress_every == 0:
                print(f"[转存] 已处理DICOM {len(results)}/{len(transferable)}")
    print(f"[转存] 处理完成：DICOM {len(results)}")
    return results


AUDIT_HEADERS = [
    "源文件", "目标文件", "筛选号", "PatientID", "StudyInstanceUID",
    "SeriesInstanceUID", "SOPInstanceUID", "文件大小", "SHA256", "状态", "说明",
]


def write_audit_csv(
    path: Path, results: Sequence[TransferResult], subject_id: str
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=AUDIT_HEADERS)
        writer.writeheader()
        for result in results:
            record = result.record
            writer.writerow(
                {
                    "源文件": str(record.path),
                    "目标文件": result.destination_path,
                    "筛选号": subject_id,
                    "PatientID": record.patient_id,
                    "StudyInstanceUID": record.study_uid,
                    "SeriesInstanceUID": record.series_uid,
                    "SOPInstanceUID": record.sop_uid,
                    "文件大小": result.file_size,
                    "SHA256": result.sha256,
                    "状态": result.status,
                    "说明": result.message,
                }
            )


BATCH_SUMMARY_HEADERS = [
    "筛选号", "源目录", "处理状态", "身份结论", "DICOM数量", "成功复制",
    "内容重复", "UID冲突", "隔离文件", "错误", "转存清单", "说明",
]


def write_batch_summary_csv(
    path: Path, summaries: Sequence[SubjectRunSummary]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=BATCH_SUMMARY_HEADERS)
        writer.writeheader()
        for summary in summaries:
            writer.writerow(
                {
                    "筛选号": summary.subject_id,
                    "源目录": summary.source_dir,
                    "处理状态": summary.status,
                    "身份结论": summary.identity_status,
                    "DICOM数量": summary.dicom_files,
                    "成功复制": summary.copied,
                    "内容重复": summary.duplicate_same,
                    "UID冲突": summary.conflicts,
                    "隔离文件": summary.quarantined,
                    "错误": summary.errors,
                    "转存清单": summary.audit_path,
                    "说明": summary.message,
                }
            )


def print_identity(decision: IdentityDecision) -> None:
    status_label = {
        "confirmed": "已确认同一人",
        "conflict": "身份冲突",
        "insufficient": "证据不足",
    }[decision.status]
    patient_ids = sorted(
        {patient_id for profile in decision.profiles for patient_id in profile.patient_ids}
    )
    print(f"身份结论: {status_label}")
    print(f"身份分组: {len(decision.profiles)}")
    print(f"PatientID数量: {len(patient_ids)}")
    if patient_ids:
        print("PatientID: " + ", ".join(patient_ids))
    if decision.evidence:
        print("一致证据: " + ", ".join(decision.evidence))
    for reason in decision.reasons:
        print("身份问题: " + reason)


def validate_paths(source_root: Path, destination_root: Path) -> None:
    source = source_root.resolve()
    destination = destination_root.resolve()
    if not source.is_dir():
        raise ValueError(f"源目录不存在: {source}")
    if source == destination:
        raise ValueError("源目录和目标根目录不能相同")
    if is_relative_to(destination, source):
        raise ValueError("目标根目录不能位于源目录内部")
    if is_relative_to(source, destination):
        raise ValueError("目标根目录不能是源目录的上级目录；请使用独立转存目录")


def unique_timestamped_csv(directory: Path, prefix: str, timestamp: str) -> Path:
    candidate = directory / f"{prefix}_{timestamp}.csv"
    if not candidate.exists():
        return candidate
    return directory / f"{prefix}_{timestamp}_{uuid.uuid4().hex[:8]}.csv"


def audit_directory(args: argparse.Namespace, destination_root: Path) -> Path:
    return (
        Path(args.audit_dir).resolve()
        if args.audit_dir
        else destination_root / "_transfer_audit"
    )


def process_subject(
    source_root: Path,
    destination_root: Path,
    subject_id: str,
    args: argparse.Namespace,
) -> SubjectRunSummary:
    print(f"源目录: {source_root}")
    print(f"目标目录: {destination_root / sanitize_component(subject_id)}")
    records = scan_directory(
        source_root,
        destination_root,
        workers=max(1, args.workers),
        progress_every=max(0, args.progress_every),
    )
    dicom_count = sum(record.is_dicom for record in records)
    decision = decide_identity(records)
    print_identity(decision)
    if not decision.confirmed:
        print("身份预检未通过：该受试者未复制任何文件。", file=sys.stderr)
        return SubjectRunSummary(
            subject_id=subject_id,
            source_dir=str(source_root),
            status="identity_failed",
            identity_status=decision.status,
            dicom_files=dicom_count,
            message="；".join(decision.reasons),
        )
    if not args.execute:
        print("身份预检通过；当前未复制。确认后增加 --execute。")
        return SubjectRunSummary(
            subject_id=subject_id,
            source_dir=str(source_root),
            status="preview_passed",
            identity_status=decision.status,
            dicom_files=dicom_count,
        )

    results = execute_transfer(
        records,
        destination_root,
        subject_id,
        copy_workers=max(1, args.copy_workers),
        durable=args.durable,
        progress_every=max(0, args.progress_every),
    )
    counters: dict[str, int] = {}
    for result in results:
        counters[result.status] = counters.get(result.status, 0) + 1
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    audit_path = unique_timestamped_csv(
        audit_directory(args, destination_root),
        f"转存清单_{sanitize_component(subject_id)}",
        timestamp,
    )
    write_audit_csv(audit_path, results, subject_id)
    print(f"成功复制: {counters.get('copied', 0)}")
    print(f"内容重复: {counters.get('duplicate_same', 0)}")
    print(f"UID冲突: {counters.get('conflict', 0)}")
    print(f"隔离文件: {counters.get('quarantined', 0)}")
    print(f"错误: {counters.get('error', 0)}")
    print(f"转存清单: {audit_path}")
    return SubjectRunSummary(
        subject_id=subject_id,
        source_dir=str(source_root),
        status="completed_with_errors" if counters.get("error", 0) else "completed",
        identity_status=decision.status,
        dicom_files=dicom_count,
        copied=counters.get("copied", 0),
        duplicate_same=counters.get("duplicate_same", 0),
        conflicts=counters.get("conflict", 0),
        quarantined=counters.get("quarantined", 0),
        errors=counters.get("error", 0),
        audit_path=str(audit_path),
    )


def subject_directories(center_root: Path) -> list[Path]:
    return sorted(
        (path for path in center_root.iterdir() if path.is_dir()),
        key=lambda path: path.name.casefold(),
    )


def print_batch_summary(summaries: Sequence[SubjectRunSummary]) -> None:
    print("\n========== 批量处理汇总 ==========")
    for summary in summaries:
        print(
            f"[{summary.subject_id}] {summary.status}，身份={summary.identity_status}，"
            f"DICOM={summary.dicom_files}，复制={summary.copied}，"
            f"重复={summary.duplicate_same}，冲突={summary.conflicts}，"
            f"隔离={summary.quarantined}，错误={summary.errors}"
        )
    print(f"受试者总数: {len(summaries)}")
    print(f"处理成功: {sum(item.status in {'preview_passed', 'completed'} for item in summaries)}")
    print(f"处理失败: {sum(item.status not in {'preview_passed', 'completed'} for item in summaries)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="检查受试者身份并按筛选号/Study/Series/SOP安全转存DICOM，支持单个和中心批量"
    )
    parser.add_argument(
        "source_dir",
        help="单个受试者原始目录；使用--batch时填写包含多个受试者一级目录的中心目录",
    )
    parser.add_argument("destination_root", help="中心目标根目录，例如 /data/dicom/四川省人民")
    parser.add_argument(
        "--subject-id",
        help="输出筛选号目录；默认使用source_dir目录名",
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="批量处理source_dir下的每个一级受试者目录",
    )
    parser.add_argument("--workers", type=int, default=4, help="DICOM头读取线程数，默认4")
    parser.add_argument("--copy-workers", type=int, default=2, help="复制线程数，默认2")
    parser.add_argument("--execute", action="store_true", help="身份确认后执行复制；默认仅预检")
    parser.add_argument("--durable", action="store_true", help="逐文件fsync，安全性更高但更慢")
    parser.add_argument(
        "--audit-dir",
        help="转存清单目录；默认写入目标根目录下_transfer_audit",
    )
    parser.add_argument(
        "--progress-every", type=int, default=500, help="每多少文件打印进度，0表示关闭"
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    source_root = Path(os.path.abspath(args.source_dir))
    destination_root = Path(os.path.abspath(args.destination_root))
    validate_paths(source_root, destination_root)
    if args.batch and args.subject_id:
        raise ValueError("批量模式不能使用 --subject-id；筛选号取每个一级目录名称")

    print(f"规则版本: {RULE_VERSION}")
    if args.batch:
        subjects = subject_directories(source_root)
        if not subjects:
            print("批量源目录下没有一级受试者目录。", file=sys.stderr)
            return 2
        print(f"运行模式: 中心批量")
        print(f"中心源目录: {source_root}")
        print(f"目标根目录: {destination_root}")
        print(f"发现受试者目录: {len(subjects)}")
        summaries: list[SubjectRunSummary] = []
        for index, subject_source in enumerate(subjects, start=1):
            subject_id = subject_source.name
            print(f"\n========== [{index}/{len(subjects)}] {subject_id} ==========")
            try:
                summaries.append(
                    process_subject(subject_source, destination_root, subject_id, args)
                )
            except Exception as exc:
                print(f"受试者处理异常: {exc}", file=sys.stderr)
                summaries.append(
                    SubjectRunSummary(
                        subject_id=subject_id,
                        source_dir=str(subject_source),
                        status="error",
                        identity_status="unknown",
                        errors=1,
                        message=str(exc),
                    )
                )
        print_batch_summary(summaries)
        if args.execute:
            timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
            summary_path = unique_timestamped_csv(
                audit_directory(args, destination_root), "批量转存汇总", timestamp
            )
            write_batch_summary_csv(summary_path, summaries)
            print(f"批量汇总: {summary_path}")
        return 1 if any(
            item.status not in {"preview_passed", "completed"} for item in summaries
        ) else 0

    subject_id = text_value(args.subject_id) or source_root.name
    if not subject_id:
        raise ValueError("无法确定筛选号目录名")
    summary = process_subject(source_root, destination_root, subject_id, args)
    if summary.status == "identity_failed":
        return 2
    return 1 if summary.status in {"completed_with_errors", "error"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
