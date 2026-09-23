#!/usr/bin/env python3
"""按筛选号处理目录、ZIP、RAR 混合来源的 DICOM 转存核心。

源目录和目标目录均由命令行传入。默认仅预检；只有 ``--execute`` 才写入
DICOM。批量模式按一级来源名称开头的 ``NN-HNNN`` 归组，单例模式用于后续
补充一个受试者。原始数据始终只读。
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
import unicodedata
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence

import pydicom
from pydicom.config import IGNORE


pydicom.config.settings.reading_validation_mode = IGNORE

RULE_VERSION = "2026.09.08-screening-packages-v1.2.1"
SCAN_CACHE_VERSION = "2026.09.08-dicom-header-v1"
COPY_CHUNK_SIZE = 1024 * 1024
SCREENING_PATTERN = re.compile(r"^(?P<center>\d{2})[-_]H(?P<number>\d{3})(?!\d)", re.I)
ARCHIVE_SUFFIXES = {".zip", ".rar"}
UID_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]+)*")
UID_PATH_PATTERN = re.compile(r"[0-9.]+")
AGE_PATTERN = re.compile(r"(?P<value>\d{1,3})Y", re.I)
SCAN_TAGS = [
    "PatientName",
    "PatientID",
    "PatientBirthDate",
    "PatientSex",
    "PatientAge",
    "StudyDate",
    "StudyInstanceUID",
    "SeriesInstanceUID",
    "SOPInstanceUID",
]
_TRANSFER_LOCKS = tuple(threading.Lock() for _ in range(257))


@dataclass(frozen=True)
class SourcePackage:
    path: Path
    screening_id: str


@dataclass
class DicomRecord:
    path: Path
    source_display: str
    source_hash: str
    is_dicom: bool
    unreadable: bool = False
    error: str = ""
    patient_name: str = ""
    patient_id: str = ""
    birth_date: str = ""
    sex: str = ""
    patient_age: str = ""
    study_date: str = ""
    study_uid: str = ""
    series_uid: str = ""
    sop_uid: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass
class IdentityProfile:
    key: str
    files: int = 0
    patient_ids: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)
    birth_dates: set[str] = field(default_factory=set)
    sexes: set[str] = field(default_factory=set)
    birth_years: set[int] = field(default_factory=set)


@dataclass
class IdentityDecision:
    status: str
    reasons: list[str]
    evidence: list[str]
    profiles: list[IdentityProfile]

    @property
    def confirmed(self) -> bool:
        return self.status in {"confirmed", "screening_default"}


@dataclass
class ProfileRoute:
    profile_key: str
    source_screening_id: str
    target_screening_id: Optional[str]
    action: str
    reason: str


@dataclass
class RecordRoute:
    record: DicomRecord
    source_screening_id: str
    target_screening_id: Optional[str]
    action: str
    reason: str


@dataclass
class SubjectScan:
    screening_id: str
    packages: list[SourcePackage]
    records: list[DicomRecord]
    package_errors: list[str]
    new_profiles: list[IdentityProfile]
    combined_profiles: list[IdentityProfile]
    cache_path: Optional[Path] = None


@dataclass
class SubjectPlan:
    scan: SubjectScan
    routes: list[RecordRoute]
    profile_routes: dict[str, ProfileRoute]
    identity_status: str
    reasons: list[str]


@dataclass
class TransferResult:
    record: DicomRecord
    target: str
    status: str
    size: int = 0
    sha256: str = ""
    message: str = ""


@dataclass
class SubjectSummary:
    screening_id: str
    packages: int
    status: str
    identity_status: str
    dicom_files: int = 0
    copied: int = 0
    duplicate_same: int = 0
    rerouted: int = 0
    identity_held: int = 0
    conflicts: int = 0
    quarantined: int = 0
    errors: int = 0
    audit_path: str = ""
    message: str = ""


def text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def normalized_token(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").replace("^", " ")
    return "".join(ch for ch in value.casefold() if ch.isalnum())


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
    value = text(value).upper()
    return value if value in {"M", "F", "O"} else ""


def normalize_age(value: str) -> str:
    match = AGE_PATTERN.fullmatch(text(value))
    return f"{int(match.group('value')):03d}Y" if match else ""


def estimated_birth_year(age: str, study_date: str) -> Optional[int]:
    age = normalize_age(age)
    study_date = normalize_date(study_date)
    if not age or not study_date:
        return None
    return int(study_date[:4]) - int(age[:3])


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


def screening_id_from_name(name: str) -> str:
    match = SCREENING_PATTERN.match(name.strip())
    if not match:
        return ""
    return f"{match.group('center')}-H{match.group('number')}".upper()


def validate_screening_id(value: str) -> str:
    normalized = screening_id_from_name(value)
    if not normalized or len(normalized) != len(value.strip()):
        raise ValueError(f"筛选号格式无效: {value}；应类似04-H001")
    return normalized


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def normalize_cli_path(value: str, label: str) -> Path:
    """Normalize a copied CLI path without changing characters inside it.

    Commands copied from rich text occasionally contain a BOM, zero-width space,
    or word-joiner next to the path.  On Windows that makes ``D:\\...`` look
    correct in the terminal while ``abspath`` treats it as a relative path and
    prepends the current directory.
    """

    candidate = unicodedata.normalize("NFKC", value or "")

    def trim_boundary(text_value: str) -> str:
        start = 0
        end = len(text_value)
        while start < end and (
            text_value[start].isspace() or unicodedata.category(text_value[start]) == "Cf"
        ):
            start += 1
        while end > start and (
            text_value[end - 1].isspace()
            or unicodedata.category(text_value[end - 1]) == "Cf"
        ):
            end -= 1
        return text_value[start:end]

    candidate = trim_boundary(candidate)
    if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in {'"', "'"}:
        candidate = trim_boundary(candidate[1:-1])
    if not candidate:
        raise ValueError(f"{label}不能为空")

    # A second drive prefix can never be part of a valid Windows path.  Report
    # it here instead of letting pathlib fail later with an opaque WinError 123.
    if os.name == "nt":
        drive_prefixes = list(re.finditer(r"[A-Za-z]:[\\/]", candidate))
        if len(drive_prefixes) > 1 or (drive_prefixes and drive_prefixes[0].start() != 0):
            raise ValueError(
                f"{label}格式异常，检测到路径被重复拼接或含隐藏字符: {candidate!r}"
            )

    return Path(os.path.abspath(os.path.expandvars(os.path.expanduser(candidate))))


def validate_paths(source: Path, destination: Path) -> None:
    source = source.resolve()
    destination = destination.resolve()
    if not source.exists():
        raise FileNotFoundError(f"源路径不存在: {source}")
    if source == destination:
        raise ValueError("源路径与目标目录不能相同")
    if source.is_dir() and is_relative_to(destination, source):
        raise ValueError("目标目录不能位于源目录内部")
    if is_relative_to(source, destination):
        raise ValueError("源路径不能位于目标目录内部")


def collect_batch_packages(
    source_root: Path, *, folders_only: bool = False
) -> tuple[dict[str, list[SourcePackage]], list[str]]:
    if not source_root.is_dir():
        raise ValueError("批量模式的源路径必须是中心目录")
    if screening_id_from_name(source_root.name):
        raise ValueError(
            "batch模式应传入包含多个04-Hxxx来源包的中心根目录；"
            "当前路径像单个筛选号目录，请改用single并增加--subject-id"
        )
    grouped: dict[str, list[SourcePackage]] = {}
    ignored: list[str] = []
    for path in sorted(source_root.iterdir(), key=lambda item: item.name.casefold()):
        if folders_only and path.is_file() and path.suffix.casefold() in ARCHIVE_SUFFIXES:
            continue
        if not (path.is_dir() or (path.is_file() and path.suffix.casefold() in ARCHIVE_SUFFIXES)):
            ignored.append(f"不支持的一级来源: {path.name}")
            continue
        screening_id = screening_id_from_name(path.name)
        if not screening_id:
            ignored.append(f"名称没有筛选号前缀: {path.name}")
            continue
        grouped.setdefault(screening_id, []).append(SourcePackage(path, screening_id))
    return dict(sorted(grouped.items())), ignored


def collect_single_packages(
    source: Path, screening_id: str, *, folders_only: bool = False
) -> list[SourcePackage]:
    if source.is_file():
        if folders_only:
            raise ValueError("--folders-only模式不能把ZIP/RAR作为单例源，请传入已解压目录")
        if source.suffix.casefold() not in ARCHIVE_SUFFIXES:
            raise ValueError("单例文件输入仅支持ZIP或RAR；普通DICOM请放入一个目录")
        named_id = screening_id_from_name(source.name)
        if named_id and named_id != screening_id:
            raise ValueError(f"来源名称筛选号{name_id}与--subject-id {screening_id}不一致")
        return [SourcePackage(source, screening_id)]
    if not source.is_dir():
        raise ValueError("单例源路径必须是ZIP、RAR或目录")

    # 如果传入的是中心根目录，则只选择名称属于该筛选号的一级来源。
    matched: list[SourcePackage] = []
    for child in sorted(source.iterdir(), key=lambda item: item.name.casefold()):
        supported = child.is_dir() or (
            not folders_only and child.is_file() and child.suffix.casefold() in ARCHIVE_SUFFIXES
        )
        if supported:
            if screening_id_from_name(child.name) == screening_id:
                matched.append(SourcePackage(child, screening_id))
    if matched:
        return matched

    named_id = screening_id_from_name(source.name)
    if named_id and named_id != screening_id:
        raise ValueError(f"来源目录筛选号{named_id}与--subject-id {screening_id}不一致")
    return [SourcePackage(source, screening_id)]


def _safe_zip_members(archive: zipfile.ZipFile, max_bytes: int) -> list[zipfile.ZipInfo]:
    members: list[zipfile.ZipInfo] = []
    total = 0
    for info in archive.infolist():
        if info.is_dir():
            continue
        member_path = Path(info.filename.replace("\\", "/"))
        if member_path.is_absolute() or ".." in member_path.parts:
            raise ValueError(f"ZIP包含不安全路径: {info.filename}")
        unix_mode = (info.external_attr >> 16) & 0xF000
        if unix_mode == 0xA000:
            raise ValueError(f"ZIP包含符号链接，拒绝解压: {info.filename}")
        total += int(info.file_size)
        if total > max_bytes:
            raise ValueError("ZIP解压后大小超过--max-uncompressed-gb限制")
        members.append(info)
    return members


def extract_zip(path: Path, destination: Path, max_bytes: int) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "r") as archive:
        members = _safe_zip_members(archive, max_bytes)
        for info in members:
            target = (destination / Path(info.filename.replace("\\", "/"))).resolve()
            if not is_relative_to(target, destination.resolve()):
                raise ValueError(f"ZIP成员越界: {info.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as source_stream, target.open("wb") as target_stream:
                shutil.copyfileobj(source_stream, target_stream, COPY_CHUNK_SIZE)


def find_7zip(explicit: str = "") -> str:
    if explicit:
        candidate = Path(explicit)
        if candidate.is_file():
            return str(candidate)
        found = shutil.which(explicit)
        if found:
            return found
        raise FileNotFoundError(f"找不到指定的7-Zip程序: {explicit}")
    for name in ("7zz", "7z", "7za"):
        found = shutil.which(name)
        if found:
            return found
    if os.name == "nt":
        for candidate in (
            Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "7-Zip/7z.exe",
            Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "7-Zip/7z.exe",
        ):
            if candidate.is_file():
                return str(candidate)
    raise FileNotFoundError("处理RAR需要7-Zip；请安装7z/7zz，或使用--seven-zip指定程序路径")


def extract_rar(path: Path, destination: Path, seven_zip: str, max_bytes: int) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [seven_zip, "x", "-y", f"-o{destination}", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        tail = "\n".join(completed.stdout.splitlines()[-8:])
        raise RuntimeError(f"RAR解压失败({completed.returncode}): {path.name}\n{tail}")
    resolved_root = destination.resolve()
    total = 0
    for extracted in destination.rglob("*"):
        if extracted.is_symlink() or not is_relative_to(extracted.resolve(), resolved_root):
            raise ValueError(f"RAR解压结果包含不安全路径: {extracted}")
        if extracted.is_file():
            total += extracted.stat().st_size
            if total > max_bytes:
                raise ValueError("RAR解压后大小超过--max-uncompressed-gb限制")


def iter_disk_files(root: Path) -> Iterator[Path]:
    for current, _directories, filenames in os.walk(root):
        current_path = Path(current)
        for filename in filenames:
            # DICOMDIR is a media-directory index object, not an image instance.
            # It normally has no Study/Series/SOP instance route and must not
            # participate in patient identity checks or transfer auditing.
            if filename.casefold() == "dicomdir":
                continue
            path = current_path / filename
            if path.suffix.casefold() in ARCHIVE_SUFFIXES:
                continue
            if path.is_file():
                yield path


def nested_archives(root: Path) -> list[Path]:
    return sorted(
        (path for path in root.rglob("*") if path.is_file() and path.suffix.casefold() in ARCHIVE_SUFFIXES),
        key=lambda path: str(path).casefold(),
    )


def prepare_package_roots(
    package: SourcePackage,
    staging_root: Path,
    *,
    max_bytes: int,
    seven_zip_option: str,
    expand_archives: bool = True,
) -> list[tuple[Path, str]]:
    roots: list[tuple[Path, str]] = []
    archives: list[Path]
    if package.path.is_dir():
        roots.append((package.path, str(package.path)))
        archives = nested_archives(package.path) if expand_archives else []
    else:
        archives = [package.path]

    seven_zip = ""
    for index, archive_path in enumerate(archives, start=1):
        archive_root = staging_root / f"archive_{stable_hash(str(archive_path))[:12]}_{index}"
        if archive_path.suffix.casefold() == ".zip":
            extract_zip(archive_path, archive_root, max_bytes)
        else:
            seven_zip = seven_zip or find_7zip(seven_zip_option)
            extract_rar(archive_path, archive_root, seven_zip, max_bytes)
        roots.append((archive_root, str(archive_path)))
    return roots


def _has_dicom_prefix(stream: Any) -> bool:
    prefix = stream.read(132)
    stream.seek(0)
    return len(prefix) >= 132 and prefix[128:132] == b"DICM"


def _uid_warnings(name: str, value: str) -> list[str]:
    warnings: list[str] = []
    if value and not UID_PATTERN.fullmatch(value):
        warnings.append(f"{name}格式异常")
    if len(value) > 64:
        warnings.append(f"{name}长度{len(value)}超过64")
    return warnings


def scan_one_file(path: Path, root: Path, label: str) -> DicomRecord:
    relative = str(path.relative_to(root)).replace("\\", "/")
    source_display = f"{label}!{relative}"
    source_hash = stable_hash(source_display)
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
        likely = prefix or path.suffix.casefold() in {".dcm", ".dicom"}
        return DicomRecord(path, source_display, source_hash, likely, likely, str(exc))

    study_uid = text(getattr(dataset, "StudyInstanceUID", ""))
    series_uid = text(getattr(dataset, "SeriesInstanceUID", ""))
    sop_uid = text(getattr(dataset, "SOPInstanceUID", ""))
    identity_values = [
        getattr(dataset, "PatientName", None),
        getattr(dataset, "PatientID", None),
        getattr(dataset, "PatientBirthDate", None),
        getattr(dataset, "PatientSex", None),
    ]
    is_dicom = prefix or bool(study_uid or series_uid or sop_uid) or sum(
        bool(text(value)) for value in identity_values
    ) >= 2
    if not is_dicom:
        return DicomRecord(path, source_display, source_hash, False)
    return DicomRecord(
        path=path,
        source_display=source_display,
        source_hash=source_hash,
        is_dicom=True,
        patient_name=text(getattr(dataset, "PatientName", "")),
        patient_id=text(getattr(dataset, "PatientID", "")),
        birth_date=normalize_date(text(getattr(dataset, "PatientBirthDate", ""))),
        sex=normalize_sex(text(getattr(dataset, "PatientSex", ""))),
        patient_age=normalize_age(text(getattr(dataset, "PatientAge", ""))),
        study_date=normalize_date(text(getattr(dataset, "StudyDate", ""))),
        study_uid=study_uid,
        series_uid=series_uid,
        sop_uid=sop_uid,
        warnings=[
            *_uid_warnings("StudyInstanceUID", study_uid),
            *_uid_warnings("SeriesInstanceUID", series_uid),
            *_uid_warnings("SOPInstanceUID", sop_uid),
        ],
    )


def scan_roots(
    roots: Sequence[tuple[Path, str]],
    *,
    workers: int,
    progress_every: int,
    cache_path: Optional[Path] = None,
    ignore_cache: bool = False,
) -> list[DicomRecord]:
    jobs: list[tuple[Path, Path, str]] = []
    for root, label in roots:
        jobs.extend((path, root, label) for path in iter_disk_files(root))
    cached = load_scan_cache(cache_path) if cache_path and not ignore_cache else {}
    records: list[Optional[DicomRecord]] = [None] * len(jobs)
    pending: list[tuple[int, tuple[Path, Path, str]]] = []
    reused = 0
    for index, job in enumerate(jobs):
        path, root, label = job
        source_display = f"{label}!{str(path.relative_to(root)).replace(chr(92), '/')}"
        row = cached.get(source_display)
        try:
            stat = path.stat()
        except OSError:
            pending.append((index, job))
            continue
        if row and row.get("size") == stat.st_size and row.get("mtime_ns") == stat.st_mtime_ns:
            records[index] = record_from_cache(path, row)
            reused += 1
        else:
            pending.append((index, job))

    completed = reused
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        scanned = executor.map(lambda item: scan_one_file(*item[1]), pending)
        for (index, _job), record in zip(pending, scanned):
            records[index] = record
            completed += 1
            if progress_every and completed % progress_every == 0:
                dicom_count = sum(bool(item and item.is_dicom) for item in records)
                print(
                    f"[身份预检] 已检查{completed}/{len(jobs)}个文件，DICOM={dicom_count}"
                )
    final_records = [record for record in records if record is not None]
    dicom_count = sum(record.is_dicom for record in final_records)
    if cache_path:
        write_scan_cache(cache_path, final_records)
    print(
        f"[身份预检] 扫描完成：文件={len(final_records)}，DICOM={dicom_count}，"
        f"复用缓存={reused}，重新读取={len(pending)}"
    )
    return final_records


def scan_cache_path(destination: Path, screening_id: str) -> Path:
    return destination / "_transfer_state" / "scan_cache" / f"{screening_id}.json"


def record_to_cache(record: DicomRecord) -> dict[str, Any]:
    stat = record.path.stat()
    return {
        "path": str(record.path),
        "source_display": record.source_display,
        "source_hash": record.source_hash,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "is_dicom": record.is_dicom,
        "unreadable": record.unreadable,
        "error": record.error,
        "patient_name": record.patient_name,
        "patient_id": record.patient_id,
        "birth_date": record.birth_date,
        "sex": record.sex,
        "patient_age": record.patient_age,
        "study_date": record.study_date,
        "study_uid": record.study_uid,
        "series_uid": record.series_uid,
        "sop_uid": record.sop_uid,
        "warnings": record.warnings,
    }


def record_from_cache(path: Path, row: dict[str, Any]) -> DicomRecord:
    return DicomRecord(
        path=path,
        source_display=text(row.get("source_display")),
        source_hash=text(row.get("source_hash")),
        is_dicom=bool(row.get("is_dicom")),
        unreadable=bool(row.get("unreadable")),
        error=text(row.get("error")),
        patient_name=text(row.get("patient_name")),
        patient_id=text(row.get("patient_id")),
        birth_date=text(row.get("birth_date")),
        sex=text(row.get("sex")),
        patient_age=text(row.get("patient_age")),
        study_date=text(row.get("study_date")),
        study_uid=text(row.get("study_uid")),
        series_uid=text(row.get("series_uid")),
        sop_uid=text(row.get("sop_uid")),
        warnings=list(map(text, row.get("warnings", []))),
    )


def load_scan_cache(path: Optional[Path]) -> dict[str, dict[str, Any]]:
    if not path or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if payload.get("cache_version") != SCAN_CACHE_VERSION:
        return {}
    return {
        text(row.get("source_display")): row
        for row in payload.get("records", [])
        if text(row.get("source_display"))
    }


def records_from_scan_cache(path: Path) -> list[DicomRecord]:
    cached = load_scan_cache(path)
    if not cached:
        raise ValueError(f"扫描缓存为空或版本不兼容: {path}")
    records: list[DicomRecord] = []
    for row in cached.values():
        source_path = Path(text(row.get("path")))
        if not source_path.is_file():
            raise FileNotFoundError(f"扫描后源文件已不存在: {source_path}")
        records.append(record_from_cache(source_path, row))
    return records


def write_scan_cache(path: Path, records: Sequence[DicomRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    payload = {
        "cache_version": SCAN_CACHE_VERSION,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "records": [record_to_cache(record) for record in records],
    }
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def profile_key_for_record(record: DicomRecord) -> str:
    patient_id = normalized_token(record.patient_id)
    name = normalized_token(record.patient_name)
    return f"ID:{patient_id}" if patient_id else f"NOID:{name or record.study_uid}"


def profiles_from_records(records: Sequence[DicomRecord]) -> list[IdentityProfile]:
    profiles: dict[str, IdentityProfile] = {}
    for record in records:
        if not record.is_dicom or record.unreadable:
            continue
        name = normalized_token(record.patient_name)
        key = profile_key_for_record(record)
        profile = profiles.setdefault(key, IdentityProfile(key))
        profile.files += 1
        if record.patient_id:
            profile.patient_ids.add(record.patient_id)
        if name:
            profile.names.add(name)
        if record.birth_date:
            profile.birth_dates.add(record.birth_date)
        if record.sex:
            profile.sexes.add(record.sex)
        birth_year = estimated_birth_year(record.patient_age, record.study_date)
        if birth_year is not None:
            profile.birth_years.add(birth_year)
    return sorted(profiles.values(), key=lambda profile: profile.key)


def profile_to_json(profile: IdentityProfile) -> dict[str, Any]:
    return {
        "key": profile.key,
        "files": profile.files,
        "patient_ids": sorted(profile.patient_ids),
        "names": sorted(profile.names),
        "birth_dates": sorted(profile.birth_dates),
        "sexes": sorted(profile.sexes),
        "birth_years": sorted(profile.birth_years),
    }


def profile_from_json(row: dict[str, Any]) -> IdentityProfile:
    return IdentityProfile(
        key=text(row.get("key")),
        files=int(row.get("files", 0)),
        patient_ids=set(map(text, row.get("patient_ids", []))),
        names=set(map(text, row.get("names", []))),
        birth_dates=set(map(text, row.get("birth_dates", []))),
        sexes=set(map(text, row.get("sexes", []))),
        birth_years={int(value) for value in row.get("birth_years", [])},
    )


def merge_profiles(*collections: Sequence[IdentityProfile]) -> list[IdentityProfile]:
    merged: dict[str, IdentityProfile] = {}
    for collection in collections:
        for incoming in collection:
            target = merged.setdefault(incoming.key, IdentityProfile(incoming.key))
            target.files += incoming.files
            target.patient_ids.update(incoming.patient_ids)
            target.names.update(incoming.names)
            target.birth_dates.update(incoming.birth_dates)
            target.sexes.update(incoming.sexes)
            target.birth_years.update(incoming.birth_years)
    return sorted(merged.values(), key=lambda profile: profile.key)


def decide_profiles(
    profiles: Sequence[IdentityProfile], records: Sequence[DicomRecord], package_errors: Sequence[str]
) -> IdentityDecision:
    conflicts: list[str] = []
    insufficient: list[str] = list(package_errors)
    evidence: set[str] = set()
    dicom_records = [record for record in records if record.is_dicom]
    unreadable = [record for record in dicom_records if record.unreadable]
    if not dicom_records:
        insufficient.append("没有识别到DICOM文件")
    if unreadable:
        insufficient.append(f"存在{len(unreadable)}个不可读DICOM")
    if not profiles:
        insufficient.append("没有可用于身份判断的患者资料")

    for profile in profiles:
        if not profile.patient_ids:
            insufficient.append(f"{profile.key}缺少PatientID")
        if not profile.names:
            insufficient.append(f"{profile.key}缺少PatientName")
        if len(profile.names) > 1:
            insufficient.append(f"{profile.key}内部存在多个姓名，按筛选号默认归并")
        internal_conflicts = []
        if len(profile.birth_dates) > 1:
            internal_conflicts.append("出生日期")
        if len(profile.sexes) > 1:
            internal_conflicts.append("性别")
        if profile.birth_years and max(profile.birth_years) - min(profile.birth_years) > 1:
            internal_conflicts.append("年龄推算出生年")
        if len(internal_conflicts) >= 2:
            conflicts.append(f"{profile.key}内部{'、'.join(internal_conflicts)}同时冲突")
        elif internal_conflicts:
            insufficient.append(
                f"{profile.key}内部{internal_conflicts[0]}不一致，单项冲突不足以判定异人"
            )

    for index, left in enumerate(profiles):
        for right in profiles[index + 1 :]:
            pair_evidence, pair_conflicts = profile_demographic_comparison(left, right)
            names_match = bool(left.names and right.names and left.names & right.names)
            names_differ = bool(left.names and right.names and not names_match)
            hard_conflict = (
                names_differ and bool(pair_conflicts)
            ) or (
                names_match and len(pair_conflicts) >= 2
            )
            if hard_conflict:
                conflicts.append(
                    f"{left.key}与{right.key}明确不一致："
                    f"{'姓名不同；' if names_differ else ''}{'、'.join(pair_conflicts)}冲突"
                )
            elif not names_match or len(pair_evidence) < 2:
                insufficient.append(
                    f"{left.key}与{right.key}身份佐证不足"
                    f"（姓名{'一致' if names_match else '不一致或缺失'}，"
                    f"人口学一致{len(pair_evidence)}项、冲突{len(pair_conflicts)}项）"
                )
            else:
                evidence.update(pair_evidence)

    if conflicts:
        return IdentityDecision("conflict", sorted(set(conflicts)), sorted(evidence), list(profiles))
    if insufficient:
        return IdentityDecision(
            "screening_default", sorted(set(insufficient)), sorted(evidence), list(profiles)
        )
    evidence.add("姓名")
    if len(profiles) == 1:
        evidence.add("PatientID")
    return IdentityDecision("confirmed", [], sorted(evidence), list(profiles))


def _normalized_patient_ids(profile: IdentityProfile) -> set[str]:
    return {normalized_token(value) for value in profile.patient_ids if normalized_token(value)}


def profile_demographic_comparison(
    left: IdentityProfile, right: IdentityProfile
) -> tuple[set[str], list[str]]:
    matches: set[str] = set()
    conflicts: list[str] = []
    for label, left_values, right_values in (
        ("出生日期", left.birth_dates, right.birth_dates),
        ("性别", left.sexes, right.sexes),
    ):
        if left_values and right_values:
            if left_values & right_values:
                matches.add(label)
            else:
                conflicts.append(label)
    if left.birth_years and right.birth_years:
        if any(abs(a - b) <= 1 for a in left.birth_years for b in right.birth_years):
            matches.add("年龄推算出生年")
        else:
            conflicts.append("年龄推算出生年")
    return matches, conflicts


def profiles_name_mismatch(left: IdentityProfile, right: IdentityProfile) -> bool:
    return bool(left.names and right.names and not (left.names & right.names))


def profiles_hard_conflict(left: IdentityProfile, right: IdentityProfile) -> bool:
    matches, conflicts = profile_demographic_comparison(left, right)
    names_match = bool(left.names and right.names and left.names & right.names)
    names_differ = profiles_name_mismatch(left, right)
    del matches
    return (names_differ and bool(conflicts)) or (names_match and len(conflicts) >= 2)


def profile_internal_hard_conflict(profile: IdentityProfile) -> bool:
    conflict_categories = 0
    if len(profile.birth_dates) > 1:
        conflict_categories += 1
    if len(profile.sexes) > 1:
        conflict_categories += 1
    if profile.birth_years and max(profile.birth_years) - min(profile.birth_years) > 1:
        conflict_categories += 1
    return conflict_categories >= 2


def profiles_strong_match(
    left: IdentityProfile, right: IdentityProfile
) -> tuple[bool, list[str]]:
    matches, conflicts = profile_demographic_comparison(left, right)
    names_match = bool(left.names and right.names and left.names & right.names)
    patient_id_match = bool(_normalized_patient_ids(left) & _normalized_patient_ids(right))
    if conflicts or not names_match:
        return False, []
    evidence = ["姓名"]
    if patient_id_match:
        evidence.append("PatientID")
    evidence.extend(sorted(matches))
    return patient_id_match or len(matches) >= 2, evidence


def build_subject_plan(
    scan: SubjectScan,
    profiles_by_screening: dict[str, list[IdentityProfile]],
    *,
    manual_confirm_same: bool = False,
) -> SubjectPlan:
    decision = decide_profiles(scan.combined_profiles, scan.records, scan.package_errors)
    profiles = {profile.key: profile for profile in scan.combined_profiles}
    new_keys = {profile.key for profile in scan.new_profiles}
    if manual_confirm_same:
        reason = (
            "经数据来源方人工确认：本筛选号内多个PatientID属于同一患者；"
            "按当前筛选号归并并转存"
        )
        profile_routes = {
            key: ProfileRoute(
                key,
                scan.screening_id,
                scan.screening_id,
                "manual_confirmed_same",
                reason,
            )
            for key in sorted(new_keys)
        }
        routes: list[RecordRoute] = []
        for record in scan.records:
            if not record.is_dicom:
                continue
            if record.unreadable:
                routes.append(RecordRoute(
                    record,
                    scan.screening_id,
                    None,
                    "unreadable",
                    record.error or "DICOM不可读",
                ))
            else:
                routes.append(RecordRoute(
                    record,
                    scan.screening_id,
                    scan.screening_id,
                    "manual_confirmed_same",
                    reason,
                ))
        return SubjectPlan(
            scan,
            routes,
            profile_routes,
            "manual_confirmed_same",
            [reason],
        )

    hard_peers: dict[str, set[str]] = {key: set() for key in profiles}
    name_mismatch_peers: dict[str, set[str]] = {key: set() for key in profiles}
    profile_list = list(profiles.values())
    for index, left in enumerate(profile_list):
        for right in profile_list[index + 1:]:
            if profiles_hard_conflict(left, right):
                hard_peers[left.key].add(right.key)
                hard_peers[right.key].add(left.key)
            if profiles_name_mismatch(left, right):
                name_mismatch_peers[left.key].add(right.key)
                name_mismatch_peers[right.key].add(left.key)

    external_targets: dict[str, dict[str, set[str]]] = {}
    for key in new_keys:
        profile = profiles[key]
        candidates: dict[str, set[str]] = {}
        for target_screening_id, target_profiles in profiles_by_screening.items():
            if target_screening_id == scan.screening_id:
                continue
            for target_profile in target_profiles:
                matched, evidence = profiles_strong_match(profile, target_profile)
                if matched:
                    candidates.setdefault(target_screening_id, set()).update(evidence)
        external_targets[key] = candidates

    reroute_candidates: dict[str, tuple[str, set[str]]] = {}
    for key in new_keys:
        candidates = external_targets.get(key, {})
        if len(candidates) != 1:
            continue
        if hard_peers.get(key) or name_mismatch_peers.get(key):
            target_screening_id, evidence = next(iter(candidates.items()))
            reroute_candidates[key] = (target_screening_id, evidence)

    # 自动改放必须保留一个能够代表当前筛选号的本地身份锚点；如果所有身份
    # 都准备改放，无法证明哪个包才是错放，故全部留待人工确认。
    if reroute_candidates and not (set(profiles) - set(reroute_candidates)):
        reroute_candidates.clear()

    remaining_keys = set(profiles) - set(reroute_candidates)
    held_keys: set[str] = {
        key for key in new_keys if profile_internal_hard_conflict(profiles[key])
    }
    for key in remaining_keys:
        if any(peer in remaining_keys for peer in hard_peers.get(key, set())):
            if key in new_keys:
                held_keys.add(key)

    profile_routes: dict[str, ProfileRoute] = {}
    plan_reasons = list(decision.reasons)
    for key in sorted(new_keys):
        if key in reroute_candidates:
            target_screening_id, evidence = reroute_candidates[key]
            reason = (
                f"当前筛选号内存在身份不一致，且与{target_screening_id}唯一强匹配："
                f"{','.join(sorted(evidence))}"
            )
            profile_routes[key] = ProfileRoute(
                key, scan.screening_id, target_screening_id, "rerouted", reason
            )
            plan_reasons.append(f"{key}已重定向至{target_screening_id}")
        elif key in held_keys:
            candidates = external_targets.get(key, {})
            if not candidates:
                detail = "未在其他筛选号找到强匹配"
            elif len(candidates) > 1:
                detail = "在多个筛选号找到强匹配：" + ",".join(sorted(candidates))
            else:
                detail = "缺少可确认当前筛选号主体的本地身份锚点"
            reason = f"确认存在不同身份，但归属无法唯一确定；{detail}"
            profile_routes[key] = ProfileRoute(
                key, scan.screening_id, None, "different_person_unresolved", reason
            )
            plan_reasons.append(f"{key}异人归属待确认")
        else:
            warnings = []
            if decision.status == "screening_default":
                warnings.append("身份佐证不足")
            candidates = external_targets.get(key, {})
            if candidates and not hard_peers.get(key):
                warnings.append(
                    "其他筛选号存在相似身份，但当前筛选号内没有明确异人反证"
                )
            if warnings:
                action = "screening_default_warning"
                reason = "；".join(warnings) + "；按筛选号默认归并并转存"
            else:
                action = "confirmed_same"
                reason = "身份证据一致"
            profile_routes[key] = ProfileRoute(
                key, scan.screening_id, scan.screening_id, action, reason
            )

    routes: list[RecordRoute] = []
    for record in scan.records:
        if not record.is_dicom:
            continue
        if record.unreadable:
            routes.append(RecordRoute(
                record, scan.screening_id, None, "unreadable", record.error or "DICOM不可读"
            ))
            continue
        route = profile_routes.get(profile_key_for_record(record))
        if route is None:
            routes.append(RecordRoute(
                record, scan.screening_id, None, "identity_unavailable", "缺少可用身份档案"
            ))
            continue
        routes.append(RecordRoute(
            record, scan.screening_id, route.target_screening_id, route.action, route.reason
        ))

    if held_keys:
        identity_status = "different_person_unresolved"
    elif reroute_candidates:
        identity_status = "rerouted"
    elif decision.status == "screening_default":
        identity_status = "screening_default"
    else:
        identity_status = "confirmed"
    return SubjectPlan(
        scan, routes, profile_routes, identity_status, sorted(set(plan_reasons))
    )


def _uid_path_problem(name: str, value: str) -> str:
    if not UID_PATH_PATTERN.fullmatch(value):
        return f"{name}包含非数字点号字符"
    if value.startswith(".") or value.endswith("."):
        return f"{name}以点开头或结尾"
    if len(value) > 240:
        return f"{name}超过安全路径长度"
    return ""


def destination_for(record: DicomRecord, destination: Path, screening_id: str) -> tuple[Path, str, bool]:
    missing = [
        name for name, value in (
            ("StudyInstanceUID", record.study_uid),
            ("SeriesInstanceUID", record.series_uid),
            ("SOPInstanceUID", record.sop_uid),
        ) if not value
    ]
    path_problems = [
        problem for name, value in (
            ("StudyInstanceUID", record.study_uid),
            ("SeriesInstanceUID", record.series_uid),
            ("SOPInstanceUID", record.sop_uid),
        ) if value and (problem := _uid_path_problem(name, value))
    ]
    if missing or path_problems:
        notes = []
        if missing:
            notes.append("缺少必要标签: " + ",".join(missing))
        if path_problems:
            notes.extend(path_problems)
        return (
            destination / "_quarantine" / screening_id / "invalid_or_missing_uids" / f"{record.source_hash}.dcm",
            "；".join(notes),
            True,
        )
    return (
        destination / screening_id / record.study_uid / record.series_uid / f"{record.sop_uid}.dcm",
        "；".join(record.warnings),
        False,
    )


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(COPY_CHUNK_SIZE):
            digest.update(block)
    return digest.hexdigest()


def copy_atomic(record: DicomRecord, target: Path, destination: Path) -> tuple[str, Path, int, str]:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.part"
    digest = hashlib.sha256()
    size = 0
    try:
        with record.path.open("rb") as source, temporary.open("xb") as output:
            while block := source.read(COPY_CHUNK_SIZE):
                output.write(block)
                digest.update(block)
                size += len(block)
        sha256 = digest.hexdigest()
        if target.exists():
            if target.stat().st_size == size and hash_file(target) == sha256:
                temporary.unlink()
                return "duplicate_same", target, size, sha256
            relative = target.relative_to(destination)
            conflict = (
                destination / "_conflicts" / relative.parent
                / f"{target.stem}.{sha256[:12]}.conflict.dcm"
            )
            conflict.parent.mkdir(parents=True, exist_ok=True)
            if conflict.exists() and hash_file(conflict) == sha256:
                temporary.unlink()
            else:
                os.replace(temporary, conflict)
            return "conflict", conflict, size, sha256
        os.replace(temporary, target)
        return "copied", target, size, sha256
    finally:
        temporary.unlink(missing_ok=True)


def transfer_record(record: DicomRecord, destination: Path, screening_id: str) -> TransferResult:
    target, message, quarantined = destination_for(record, destination, screening_id)
    try:
        lock = _TRANSFER_LOCKS[hash(str(target).casefold()) % len(_TRANSFER_LOCKS)]
        with lock:
            status, actual, size, sha256 = copy_atomic(record, target, destination)
        if quarantined and status != "conflict":
            status = "quarantined"
        return TransferResult(record, str(actual), status, size, sha256, message)
    except Exception as exc:
        return TransferResult(record, str(target), "error", message=f"{type(exc).__name__}: {exc}")


def execute_transfer(
    records: Sequence[DicomRecord], destination: Path, screening_id: str, *, copy_workers: int, progress_every: int
) -> list[TransferResult]:
    transferable = [record for record in records if record.is_dicom and not record.unreadable]
    results: list[TransferResult] = []
    with ThreadPoolExecutor(max_workers=max(1, copy_workers)) as executor:
        for result in executor.map(lambda record: transfer_record(record, destination, screening_id), transferable):
            results.append(result)
            if progress_every and len(results) % progress_every == 0:
                print(f"[转存] {screening_id} 已处理{len(results)}/{len(transferable)}个DICOM")
    return results


AUDIT_FIELDS = [
    "筛选号", "来源筛选号", "目标筛选号", "身份处理", "身份说明", "源文件", "目标文件",
    "PatientName", "PatientID", "PatientBirthDate", "PatientSex", "StudyInstanceUID",
    "SeriesInstanceUID", "SOPInstanceUID", "状态", "文件大小", "SHA256", "说明",
]
SUMMARY_FIELDS = [
    "筛选号", "来源包数", "处理状态", "身份结论", "DICOM数量", "成功复制",
    "内容重复", "改放DICOM", "异人待确认", "UID冲突", "隔离文件", "错误",
    "审计文件", "说明",
]


def atomic_csv(path: Path, fields: Sequence[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def unique_csv(directory: Path, prefix: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S_%f")
    return directory / f"{prefix}_{stamp}.csv"


@contextmanager
def staging_directory(parent: Optional[Path], screening_id: str) -> Iterator[Path]:
    base = parent or Path(tempfile.gettempdir())
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"dicom_{screening_id}_{uuid.uuid4().hex}"
    path.mkdir(parents=False, exist_ok=False)
    try:
        yield path
    finally:
        try:
            shutil.rmtree(path)
        except OSError as exc:
            print(f"[临时目录警告] 未能完全清理{path}: {exc}", file=sys.stderr)


def state_path(destination: Path, screening_id: str) -> Path:
    return destination / "_transfer_state" / "identity" / f"{screening_id}.json"


def load_state(destination: Path, screening_id: str) -> list[IdentityProfile]:
    path = state_path(destination, screening_id)
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("screening_id") != screening_id:
        raise ValueError(f"身份状态筛选号不一致: {path}")
    return [profile_from_json(row) for row in payload.get("profiles", [])]


def write_state(destination: Path, screening_id: str, profiles: Sequence[IdentityProfile]) -> None:
    path = state_path(destination, screening_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    payload = {
        "rule_version": RULE_VERSION,
        "screening_id": screening_id,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "profiles": [profile_to_json(profile) for profile in profiles],
    }
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_all_states(destination: Path) -> dict[str, list[IdentityProfile]]:
    identity_dir = destination / "_transfer_state" / "identity"
    states: dict[str, list[IdentityProfile]] = {}
    if not identity_dir.is_dir():
        return states
    for path in sorted(identity_dir.glob("*.json"), key=lambda item: item.name.casefold()):
        screening_id = screening_id_from_name(path.stem)
        if screening_id:
            states[screening_id] = load_state(destination, screening_id)
    return states


def scan_subject(
    screening_id: str,
    packages: Sequence[SourcePackage],
    destination: Path,
    args: argparse.Namespace,
    staging: Path,
) -> SubjectScan:
    print(f"\n========== {screening_id}：来源包{len(packages)}个 ==========")
    staging.mkdir(parents=True, exist_ok=True)
    package_errors: list[str] = []
    roots: list[tuple[Path, str]] = []
    for package in packages:
        print(f"[来源包] {package.path}")
        try:
            roots.extend(prepare_package_roots(
                package,
                staging,
                max_bytes=int(args.max_uncompressed_gb * 1024**3),
                seven_zip_option=args.seven_zip,
                expand_archives=not args.folders_only,
            ))
        except Exception as exc:
            package_errors.append(f"{package.path.name}: {type(exc).__name__}: {exc}")
    cache = (
        scan_cache_path(destination, screening_id)
        if args.folders_only and all(package.path.is_dir() for package in packages)
        else None
    )
    records = scan_roots(
        roots,
        workers=max(1, args.workers),
        progress_every=max(0, args.progress_every),
        cache_path=cache,
        ignore_cache=args.rescan,
    ) if roots else []
    new_profiles = profiles_from_records(records)
    combined_profiles = merge_profiles(load_state(destination, screening_id), new_profiles)
    return SubjectScan(
        screening_id,
        list(packages),
        records,
        package_errors,
        new_profiles,
        combined_profiles,
        cache,
    )


def execute_routed_transfer(
    routes: Sequence[RecordRoute],
    destination: Path,
    source_screening_id: str,
    *,
    copy_workers: int,
    progress_every: int,
) -> list[TransferResult]:
    transferable = [
        route for route in routes
        if route.target_screening_id and not route.record.unreadable
    ]
    results: list[TransferResult] = []

    def transfer(route: RecordRoute) -> TransferResult:
        assert route.target_screening_id is not None
        return transfer_record(route.record, destination, route.target_screening_id)

    with ThreadPoolExecutor(max_workers=max(1, copy_workers)) as executor:
        for result in executor.map(transfer, transferable):
            results.append(result)
            if progress_every and len(results) % progress_every == 0:
                print(
                    f"[转存] {source_screening_id} 已处理"
                    f"{len(results)}/{len(transferable)}个DICOM"
                )
    return results


def write_subject_audit(
    path: Path,
    destination: Path,
    plan: SubjectPlan,
    results: Sequence[TransferResult],
    execute: bool,
) -> None:
    by_source = {result.record.source_display: result for result in results}
    rows = []
    for route in plan.routes:
        record = route.record
        result = by_source.get(record.source_display)
        if result:
            status, target, size, sha256 = (
                result.status, result.target, result.size, result.sha256
            )
            notes = [route.reason, result.message]
        elif route.target_screening_id:
            planned, note, _quarantined = destination_for(
                record, destination, route.target_screening_id
            )
            status = "预检通过待执行" if not execute else "未执行"
            target, size, sha256 = str(planned), 0, ""
            notes = [route.reason, record.error, note]
        else:
            status = {
                "unreadable": "DICOM不可读",
                "different_person_unresolved": "异人归属待确认未转存",
                "identity_unavailable": "身份资料不可用未转存",
            }.get(route.action, "未转存")
            target, size, sha256 = "", 0, ""
            notes = [route.reason, record.error]
        rows.append({
            "筛选号": route.source_screening_id,
            "来源筛选号": route.source_screening_id,
            "目标筛选号": route.target_screening_id or "",
            "身份处理": route.action,
            "身份说明": route.reason,
            "源文件": record.source_display,
            "目标文件": target,
            "PatientName": record.patient_name,
            "PatientID": record.patient_id,
            "PatientBirthDate": record.birth_date,
            "PatientSex": record.sex,
            "StudyInstanceUID": record.study_uid,
            "SeriesInstanceUID": record.series_uid,
            "SOPInstanceUID": record.sop_uid,
            "状态": status,
            "文件大小": size,
            "SHA256": sha256,
            "说明": "；".join(note for note in notes if note),
        })
    atomic_csv(path, AUDIT_FIELDS, rows)


def persist_plan_identity_state(destination: Path, plan: SubjectPlan) -> None:
    profiles = {profile.key: profile for profile in plan.scan.new_profiles}
    additions: dict[str, list[IdentityProfile]] = {}
    for key, route in plan.profile_routes.items():
        if route.target_screening_id and key in profiles:
            additions.setdefault(route.target_screening_id, []).append(profiles[key])
    for target_screening_id, incoming in additions.items():
        write_state(
            destination,
            target_screening_id,
            merge_profiles(load_state(destination, target_screening_id), incoming),
        )


def process_plan(
    plan: SubjectPlan,
    destination: Path,
    args: argparse.Namespace,
) -> SubjectSummary:
    screening_id = plan.scan.screening_id
    label = {
        "confirmed": "身份证据确认一致",
        "manual_confirmed_same": "数据来源方人工确认同一患者",
        "screening_default": "按筛选号默认归并（证据不足）",
        "rerouted": "发现异人并已确定唯一归属",
        "different_person_unresolved": "确认存在异人但归属待确认",
    }[plan.identity_status]
    print(f"[身份] {screening_id}: {label}")
    for reason in plan.reasons:
        print(f"[身份记录] {reason}")

    results = execute_routed_transfer(
        plan.routes,
        destination,
        screening_id,
        copy_workers=max(1, args.copy_workers),
        progress_every=max(0, args.progress_every),
    ) if args.execute else []
    if args.execute:
        persist_plan_identity_state(destination, plan)

    audit_dir = Path(args.audit_dir).resolve() if args.audit_dir else destination / "_transfer_audit"
    prefix = "转存清单" if args.execute else "预检清单"
    audit_path = unique_csv(audit_dir, f"{prefix}_{screening_id}")
    write_subject_audit(audit_path, destination, plan, results, args.execute)

    counters: dict[str, int] = {}
    for result in results:
        counters[result.status] = counters.get(result.status, 0) + 1
    rerouted = sum(route.action == "rerouted" for route in plan.routes)
    identity_held = sum(
        route.action in {"different_person_unresolved", "identity_unavailable", "unreadable"}
        for route in plan.routes
    )
    issue_count = (
        counters.get("error", 0) + counters.get("conflict", 0)
        + counters.get("quarantined", 0) + len(plan.scan.package_errors) + identity_held
    )
    warning_only = plan.identity_status in {
        "manual_confirmed_same", "screening_default", "rerouted"
    }
    if identity_held:
        status = "identity_unresolved"
    elif not args.execute:
        status = "preview_passed_with_warning" if warning_only else "preview_passed"
    elif issue_count:
        status = "completed_with_issues"
    elif warning_only:
        status = "completed_with_warning"
    else:
        status = "completed"
    summary = SubjectSummary(
        screening_id=screening_id,
        packages=len(plan.scan.packages),
        status=status,
        identity_status=plan.identity_status,
        dicom_files=sum(record.is_dicom for record in plan.scan.records),
        copied=counters.get("copied", 0),
        duplicate_same=counters.get("duplicate_same", 0),
        rerouted=rerouted,
        identity_held=identity_held,
        conflicts=counters.get("conflict", 0),
        quarantined=counters.get("quarantined", 0),
        errors=counters.get("error", 0) + len(plan.scan.package_errors),
        audit_path=str(audit_path),
        message="；".join(plan.reasons),
    )
    print(
        f"[结果] {screening_id}: {summary.status}，DICOM={summary.dicom_files}，"
        f"复制={summary.copied}，重复={summary.duplicate_same}，改放={summary.rerouted}，"
        f"异人待确认={summary.identity_held}，冲突={summary.conflicts}，"
        f"隔离={summary.quarantined}，错误={summary.errors}"
    )
    print(f"[审计] {audit_path}")
    return summary


def write_batch_summary(destination: Path, args: argparse.Namespace, summaries: Sequence[SubjectSummary]) -> Path:
    audit_dir = Path(args.audit_dir).resolve() if args.audit_dir else destination / "_transfer_audit"
    path = unique_csv(audit_dir, "批量转存汇总" if args.execute else "批量预检汇总")
    rows = [{
        "筛选号": item.screening_id,
        "来源包数": item.packages,
        "处理状态": item.status,
        "身份结论": item.identity_status,
        "DICOM数量": item.dicom_files,
        "成功复制": item.copied,
        "内容重复": item.duplicate_same,
        "改放DICOM": item.rerouted,
        "异人待确认": item.identity_held,
        "UID冲突": item.conflicts,
        "隔离文件": item.quarantined,
        "错误": item.errors,
        "审计文件": item.audit_path,
        "说明": item.message,
    } for item in summaries]
    atomic_csv(path, SUMMARY_FIELDS, rows)
    return path


def build_parser(platform_label: str = "通用") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=f"按筛选号安全转存目录/ZIP/RAR混合DICOM（{platform_label}入口）"
    )
    parser.add_argument("mode", choices=("batch", "single"), help="batch=中心批量；single=单患者补充")
    parser.add_argument("source", help="批量中心目录，或单例ZIP/RAR/目录")
    parser.add_argument("destination", help="目标中心根目录；运行时指定，不写死")
    parser.add_argument("--subject-id", help="single模式必填，例如04-H001")
    parser.add_argument(
        "--confirm-same-subject",
        action="append",
        default=[],
        metavar="04-HXXX",
        help=(
            "数据来源方已人工确认该筛选号内的多个PatientID属于同一患者；"
            "可重复指定，结论会写入审计"
        ),
    )
    parser.add_argument("--execute", action="store_true", help="正式转存；默认仅预检")
    parser.add_argument(
        "--folders-only", action="store_true",
        help="只读取已解压文件夹，忽略顶层及文件夹内的ZIP/RAR",
    )
    parser.add_argument(
        "--rescan", action="store_true",
        help="忽略既有扫描缓存并重新读取全部DICOM头；仍会更新缓存",
    )
    parser.add_argument("--workers", type=int, default=4, help="DICOM头读取线程数，默认4")
    parser.add_argument("--copy-workers", type=int, default=2, help="文件复制线程数，默认2")
    parser.add_argument("--progress-every", type=int, default=500, help="进度输出间隔，默认500")
    parser.add_argument("--audit-dir", help="审计目录；默认目标目录/_transfer_audit")
    parser.add_argument("--temp-dir", help="压缩包临时解压根目录；默认系统临时目录")
    parser.add_argument("--seven-zip", default="", help="7z/7zz程序路径；处理RAR时使用")
    parser.add_argument(
        "--max-uncompressed-gb", type=float, default=50.0,
        help="单个ZIP允许的最大解压大小，默认50GB",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None, platform_label: str = "通用") -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser(platform_label).parse_args(argv)
    if args.workers < 1 or args.copy_workers < 1 or args.max_uncompressed_gb <= 0:
        raise ValueError("workers、copy-workers和max-uncompressed-gb必须大于0")
    source = normalize_cli_path(args.source, "源路径")
    destination = normalize_cli_path(args.destination, "目标路径")
    validate_paths(source, destination)
    if args.mode == "batch" and args.subject_id:
        raise ValueError("batch模式不能使用--subject-id")
    if args.mode == "single" and not args.subject_id:
        raise ValueError("single模式必须提供--subject-id")
    manual_confirmed_subjects = {
        validate_screening_id(value) for value in args.confirm_same_subject
    }
    if args.mode == "single":
        single_screening_id = validate_screening_id(args.subject_id)
        unrelated = manual_confirmed_subjects - {single_screening_id}
        if unrelated:
            raise ValueError(
                "single模式的--confirm-same-subject必须与--subject-id一致: "
                + ",".join(sorted(unrelated))
            )

    print(f"规则版本: {RULE_VERSION}")
    print(f"模式: {'正式转存' if args.execute else '预检'} / {args.mode}")
    print(f"源路径: {source}")
    print(f"目标根目录: {destination}")
    if manual_confirmed_subjects:
        print(
            "人工确认同一患者: "
            + ", ".join(sorted(manual_confirmed_subjects))
            + "（将写入审计）"
        )
    if args.folders_only:
        print("来源策略: 仅文件夹（忽略所有ZIP/RAR，不执行解压）")
        print("扫描策略: 强制重新读取全部DICOM头" if args.rescan else "扫描策略: 复用未变化文件的断点缓存")
    elif args.rescan:
        print("[提示] 非--folders-only模式不使用扫描缓存，--rescan无需额外处理")
    summaries: list[SubjectSummary] = []
    if args.mode == "batch":
        missing_extracted: list[Path] = []
        if args.folders_only:
            top_archives = sorted(
                (
                    path for path in source.iterdir()
                    if path.is_file() and path.suffix.casefold() in ARCHIVE_SUFFIXES
                ),
                key=lambda path: path.name.casefold(),
            )
            missing_extracted = [
                path for path in top_archives
                if not (source / path.stem).is_dir()
            ]
            print(f"已忽略压缩包: {len(top_archives)}")
            for path in missing_extracted:
                print(f"[解压目录缺失] {path.name}")
        grouped, ignored = collect_batch_packages(source, folders_only=args.folders_only)
        print(f"识别筛选号: {len(grouped)}")
        print(f"支持的来源包: {sum(len(items) for items in grouped.values())}")
        for problem in ignored:
            print(f"[一级来源待确认] {problem}")
        temp_parent = Path(args.temp_dir).resolve() if args.temp_dir else None
        if temp_parent:
            temp_parent.mkdir(parents=True, exist_ok=True)
        scans: list[SubjectScan] = []
        with staging_directory(temp_parent, "BATCH") as batch_staging:
            print("[全中心预检] 先扫描全部筛选号并建立身份索引，再执行转存")
            for screening_id, packages in grouped.items():
                try:
                    scan = scan_subject(
                        screening_id,
                        packages,
                        destination,
                        args,
                        batch_staging / sanitize_component(screening_id),
                    )
                    if scan.cache_path and scan.records:
                        # 全中心匹配只需要患者档案。文件级明细已原子写入缓存，
                        # 转存该筛选号时再加载，避免全部明细同时占用内存。
                        scan.records.clear()
                    elif scan.cache_path:
                        scan.cache_path = None
                    scans.append(scan)
                except Exception as exc:
                    print(
                        f"[{screening_id}] 扫描异常: {type(exc).__name__}: {exc}",
                        file=sys.stderr,
                    )
                    summaries.append(SubjectSummary(
                        screening_id, len(packages), "error", "unknown", errors=1,
                        message=f"{type(exc).__name__}: {exc}",
                    ))

            profiles_by_screening = load_all_states(destination)
            for scan in scans:
                profiles_by_screening[scan.screening_id] = scan.combined_profiles
            for scan in scans:
                try:
                    if not scan.records and scan.cache_path:
                        scan.records = records_from_scan_cache(scan.cache_path)
                    plan = build_subject_plan(
                        scan,
                        profiles_by_screening,
                        manual_confirm_same=(scan.screening_id in manual_confirmed_subjects),
                    )
                    summaries.append(process_plan(plan, destination, args))
                    if scan.cache_path:
                        scan.records.clear()
                except Exception as exc:
                    screening_id = scan.screening_id
                    print(
                        f"[{screening_id}] 转存异常: {type(exc).__name__}: {exc}",
                        file=sys.stderr,
                    )
                    summaries.append(SubjectSummary(
                        screening_id, len(scan.packages), "error", "unknown", errors=1,
                        message=f"{type(exc).__name__}: {exc}",
                    ))
        if ignored:
            summaries.append(SubjectSummary(
                "UNMATCHED", len(ignored), "source_items_ignored", "unknown",
                errors=len(ignored), message="；".join(ignored),
            ))
        if missing_extracted:
            summaries.append(SubjectSummary(
                "ARCHIVE_WITHOUT_FOLDER", len(missing_extracted), "source_items_missing", "unknown",
                errors=len(missing_extracted),
                message="以下压缩包没有完全同名的解压目录：" + "；".join(
                    path.name for path in missing_extracted
                ),
            ))
        summary_path = write_batch_summary(destination, args, summaries)
        print(f"批量汇总: {summary_path}")
    else:
        screening_id = validate_screening_id(args.subject_id)
        packages = collect_single_packages(source, screening_id, folders_only=args.folders_only)
        temp_parent = Path(args.temp_dir).resolve() if args.temp_dir else None
        if temp_parent:
            temp_parent.mkdir(parents=True, exist_ok=True)
        with staging_directory(temp_parent, screening_id) as staging:
            scan = scan_subject(screening_id, packages, destination, args, staging)
            profiles_by_screening = load_all_states(destination)
            profiles_by_screening[screening_id] = scan.combined_profiles
            plan = build_subject_plan(
                scan,
                profiles_by_screening,
                manual_confirm_same=(screening_id in manual_confirmed_subjects),
            )
            summaries.append(process_plan(plan, destination, args))

    successful = {
        "preview_passed", "preview_passed_with_warning",
        "completed", "completed_with_warning",
    }
    return 0 if summaries and all(item.status in successful for item in summaries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
