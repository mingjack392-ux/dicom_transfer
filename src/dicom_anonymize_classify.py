#!/usr/bin/env python3
"""DICOM 匿名化、影像分类与转存工具（无数据库）。

功能：
1. 输入可以是目录或 ZIP 文件；目录中的 ZIP 也会被处理。
2. 为患者分配稳定的匿名编号，并在再次运行时复用已有映射。
3. 直接根据 ZIP/来源文件夹名确定整包影像类型，不再拆分包内 Series。
4. 匿名化 DICOM 标签、移除私有标签、稳定重映射身份相关 UID。
5. 输出一张可由 Excel 打开的 UTF-8 CSV 映射表，不依赖数据库。

示例：
    python dicom_anonymize_classify.py "D:\\原始数据" "D:\\匿名化结果"
    python dicom_anonymize_classify.py "D:\\2024-11-08 某患者 CT+CTA+CTP.zip" "D:\\匿名化结果"

重要限制：本工具不修改像素数据，仍需人工抽查是否有烧录姓名、编号或
可识别面部信息。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import secrets
import shutil
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

try:
    import pydicom
    from pydicom.dataset import Dataset
except ImportError:
    print("缺少依赖 pydicom，请先执行: pip install pydicom", file=sys.stderr)
    raise SystemExit(2)


MAPPING_FIELDS = [
    "record_type",
    "project_salt",
    # 患者与来源
    "source_subject",
    "source_folder_name",
    "source_image_type",
    "anonymous_patient_id",
    "original_patient_name",
    "original_patient_id",
    # 检查与序列 UID 映射
    "original_study_uid",
    "anonymous_study_uid",
    "original_series_uid",
    "anonymous_series_uid",
    # 该 Series 的文件数
    "file_count",
]


SENSITIVE_EMPTY_KEYWORDS = {
    "PatientBirthDate",
    "PatientBirthTime",
    "PatientSex",
    "PatientAge",
    "PatientSize",
    "PatientWeight",
    "PatientAddress",
    "PatientTelephoneNumbers",
    "PatientMotherBirthName",
    "MilitaryRank",
    "BranchOfService",
    "MedicalRecordLocator",
    "OtherPatientIDs",
    "OtherPatientNames",
    "OtherPatientIDsSequence",
    "EthnicGroup",
    "Occupation",
    "AdditionalPatientHistory",
    "PatientComments",
    "AdmissionID",
    "IssuerOfAdmissionID",
    "AccessionNumber",
    "StudyID",
    "InstitutionAddress",
    "InstitutionCodeSequence",
    "InstitutionalDepartmentName",
    "ReferringPhysicianName",
    "ReferringPhysicianAddress",
    "ReferringPhysicianTelephoneNumbers",
    "PerformingPhysicianName",
    "NameOfPhysiciansReadingStudy",
    "OperatorsName",
    "PhysicianOfRecord",
    "PhysiciansOfRecordIdentificationSequence",
    "DeviceSerialNumber",
    "StationName",
    "ProtocolName",
    "StudyDescription",
    "SeriesDescription",
    "ContentDescription",
    "AcquisitionComments",
    "ReasonForStudy",
    "DerivationDescription",
    "RequestingPhysician",
    "RequestingService",
    "RequestedProcedureDescription",
    "ScheduledPerformingPhysicianName",
    "ScheduledProcedureStepDescription",
    "PerformedProcedureStepDescription",
    "ContentCreatorName",
    "ImageComments",
    "StudyComments",
}


PRESERVE_ORIGINAL_KEYWORDS = {
    "PatientID",
    "InstitutionName",
}


DELETE_KEYWORDS = {
    "TargetUID",
    "TrackingUID",
    "TextString",
}


UID_EXCLUDE_KEYWORDS = {
    "SOPClassUID",
    "ReferencedSOPClassUID",
    "TransferSyntaxUID",
    "ImplementationClassUID",
    "CodingSchemeUID",
    "ContextGroupExtensionCreatorUID",
}


@dataclass
class DicomMeta:
    path: Path
    source_subject: str
    source_container: str
    source_folder_name: str
    source_image_type: str
    patient_name: str
    patient_id: str
    issuer: str
    birth_date: str
    study_uid: str
    series_uid: str
    sop_uid: str
    study_date: str
    series_date: str
    acquisition_date: str
    content_date: str


def text_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "\\".join(str(v) for v in value)
    return str(value).strip()


def sanitize_path_part(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value)).strip().rstrip(".")
    return cleaned or "_UNNAMED"


def normalized_person_name(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("^", " ")).strip()


def pending_mapping_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}_pending{path.suffix}")


def read_mapping(path: Path, allow_empty_reset: bool = False) -> Tuple[str, List[dict]]:
    pending = pending_mapping_path(path)
    source_path = path
    if pending.exists() and (
        not path.exists() or pending.stat().st_mtime >= path.stat().st_mtime
    ):
        source_path = pending
        print(f"[映射表] 使用较新的备用映射表继续: {pending}")
    if not source_path.exists():
        return secrets.token_hex(16), []

    with source_path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        if allow_empty_reset:
            return secrets.token_hex(16), []
        raise ValueError(
            "映射表已被清空，但输出目录仍包含旧 DICOM；原 project_salt 无法恢复。"
            "请改用新的空输出目录，或恢复原映射表。"
        )
    config = next((r for r in rows if r.get("record_type") == "CONFIG"), None)
    salt = (config or {}).get("project_salt", "").strip()
    if not salt:
        raise ValueError(f"映射表缺少 project_salt，无法保证 UID 一致性: {source_path}")
    return salt, [r for r in rows if r.get("record_type") == "SERIES"]


class MappingFileLockedError(RuntimeError):
    pass


def write_mapping(path: Path, salt: str, series_rows: Sequence[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.stem + "_", suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with tmp.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=MAPPING_FIELDS, extrasaction="ignore")
            writer.writeheader()
            config = {k: "" for k in MAPPING_FIELDS}
            config.update({"record_type": "CONFIG", "project_salt": salt})
            writer.writerow(config)
            for row in sorted(
                series_rows,
                key=lambda r: (
                    r.get("anonymous_patient_id", ""),
                    r.get("original_study_uid", ""),
                    r.get("original_series_uid", ""),
                ),
            ):
                writer.writerow({k: row.get(k, "") for k in MAPPING_FIELDS})
        try:
            os.replace(tmp, path)
        except PermissionError as exc:
            pending = pending_mapping_path(path)
            try:
                os.replace(tmp, pending)
            except PermissionError as pending_exc:
                raise MappingFileLockedError(
                    f"主映射表和备用映射表都被占用: {path}, {pending}。"
                    "请关闭 Excel/WPS 中的 CSV 文件和资源管理器预览窗格后重试。"
                ) from pending_exc
            print(f"[映射表] 主表被占用，已保存到备用表: {pending}")
            return pending
        pending = pending_mapping_path(path)
        if pending.exists():
            try:
                pending.unlink()
            except PermissionError:
                pass
        return path
    finally:
        if tmp.exists():
            tmp.unlink()


def patient_key(
    patient_id: str,
    issuer: str,
    patient_name: str,
    birth_date: str,
    source: str,
    source_subject: str = "",
) -> str:
    # 当前项目的外层目录已经按患者整理，优先使用目录身份，避免同一患者的
    # CTA/DSA 因设备端 PatientID 或 PatientName 不一致而被拆成多个匿名编号。
    if source_subject:
        return "SOURCE_SUBJECT|" + source_subject.strip().upper()
    if patient_id:
        return "PID|" + issuer.strip().upper() + "|" + patient_id.strip().upper()
    if patient_name or birth_date:
        return "DEMOGRAPHIC|" + patient_name.strip().upper() + "|" + birth_date.strip()
    return "SOURCE|" + source.strip().upper()


def next_anonymous_id(existing_ids: Iterable[str]) -> str:
    highest = 0
    for value in existing_ids:
        match = re.fullmatch(r"ANON(\d+)", value or "", re.I)
        if match:
            highest = max(highest, int(match.group(1)))
    return f"ANON{highest + 1:06d}"


def deterministic_uid(original_uid: str, salt: str) -> str:
    """生成符合 DICOM 长度限制的稳定 2.25 UID。"""
    digest = hashlib.sha256((salt + "|" + original_uid).encode("utf-8")).digest()[:16]
    return "2.25." + str(int.from_bytes(digest, byteorder="big", signed=False))


def date_shift_for(anonymous_id: str, salt: str) -> int:
    value = int.from_bytes(hashlib.sha256((salt + "|DATE|" + anonymous_id).encode()).digest()[:4], "big")
    days = value % 3651
    return days if value % 2 else -days


def shift_da(value: str, days: int) -> str:
    value = str(value or "")
    if not re.fullmatch(r"\d{8}", value):
        return ""
    try:
        return (datetime.strptime(value, "%Y%m%d") + timedelta(days=days)).strftime("%Y%m%d")
    except ValueError:
        return ""


def shift_dt(value: str, days: int) -> str:
    value = str(value or "")
    if len(value) < 8 or not value[:8].isdigit():
        return ""
    shifted = shift_da(value[:8], days)
    return shifted + value[8:] if shifted else ""


SOURCE_TYPE_TOKEN = re.compile(
    r"(?<![A-Z0-9])(NCCT|CTTP|CTP|CTA|DSA|CT)(?![A-Z0-9])", re.I
)

DATE_TOKEN = re.compile(
    r"(?<!\d)((?:19|20)\d{2})[-._/]?(0[1-9]|1[0-2])[-._/]?(0[1-9]|[12]\d|3[01])(?!\d)"
)

NAME_NOISE_WORDS = (
    "数字减影",
    "减影血管",
    "原始数据",
    "影像数据",
    "术前",
    "术后",
    "平扫",
    "增强",
    "灌注",
    "造影",
    "血管",
    "减影",
    "数字",
    "头颅",
    "颅脑",
    "胸部",
    "腹部",
    "动脉",
    "静脉",
    "复查",
    "重建",
    "序列",
    "检查",
    "影像",
    "数据",
    "病例",
)


def image_type_from_source_name(value: str) -> str:
    """按来源名称提取类型；组合名称按出现顺序保留，例如 CT+CTA+CTP。"""
    text = Path(str(value or "")).stem.upper()
    result: List[str] = []
    for match in SOURCE_TYPE_TOKEN.finditer(text):
        token = match.group(1).upper()
        if token == "NCCT":
            token = "CT"
        if token not in result:
            result.append(token)
    if not result:
        synonyms = [
            ("灌注", "CTP"),
            ("血管", "CTA"),
            ("数字减影", "DSA"),
            ("减影血管", "DSA"),
            ("平扫", "CT"),
        ]
        for keyword, category in synonyms:
            if keyword in text and category not in result:
                result.append(category)
    return "+".join(result) if result else "REVIEW"


def date_from_text(value: str) -> str:
    """从名称中提取并规范化日期为 YYYY-MM-DD。"""
    match = DATE_TOKEN.search(str(value or ""))
    if not match:
        return ""
    normalized = "-".join(match.groups())
    try:
        datetime.strptime(normalized, "%Y-%m-%d")
    except ValueError:
        return ""
    return normalized


def date_from_dicom_values(*values: str) -> str:
    """按传入顺序选择首个合法 DICOM DA 日期。"""
    for value in values:
        raw = re.sub(r"\D", "", str(value or ""))[:8]
        if len(raw) != 8:
            continue
        try:
            return datetime.strptime(raw, "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def chinese_person_name(*values: str) -> str:
    """从当前名称到上级目录依次寻找中文姓名，过滤常见影像描述词。"""
    for value in values:
        text = Path(str(value or "")).stem
        text = DATE_TOKEN.sub(" ", text)
        text = SOURCE_TYPE_TOKEN.sub(" ", text)
        for word in NAME_NOISE_WORDS:
            text = text.replace(word, " ")
        for candidate in re.findall(r"[\u3400-\u9fff]{2,6}", text):
            if 2 <= len(candidate) <= 4 and candidate not in NAME_NOISE_WORDS:
                return candidate
    return ""


def standardized_source_folder_name(meta: DicomMeta) -> str:
    """生成“时间 人名 影像类型”，名称已有日期时不参考上级目录日期。"""
    date = date_from_text(meta.source_folder_name)
    if not date:
        date = date_from_dicom_values(
            meta.study_date,
            meta.series_date,
            meta.acquisition_date,
            meta.content_date,
        )
    person_name = chinese_person_name(meta.source_folder_name, meta.source_subject)
    return " ".join(
        (
            date or "未知日期",
            person_name or "未知姓名",
            meta.source_image_type,
        )
    )


def looks_like_dicom(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            header = f.read(132)
        if len(header) >= 132 and header[128:132] == b"DICM":
            return True
    except OSError:
        return False
    return path.suffix.lower() in {".dcm", ".dicom", ".ima"}


SCAN_TAGS = [
    "PatientName",
    "PatientID",
    "IssuerOfPatientID",
    "PatientBirthDate",
    "StudyInstanceUID",
    "SeriesInstanceUID",
    "SOPInstanceUID",
    "StudyDate",
    "SeriesDate",
    "AcquisitionDate",
    "ContentDate",
]


def read_meta(
    path: Path,
    source_subject: str,
    source_container: str,
    source_folder_name: str,
    source_image_type: str,
) -> Optional[DicomMeta]:
    if not looks_like_dicom(path):
        return None
    try:
        ds = pydicom.dcmread(
            str(path), force=True, stop_before_pixels=True, specific_tags=SCAN_TAGS
        )
    except Exception:
        return None
    study_uid = text_value(getattr(ds, "StudyInstanceUID", ""))
    series_uid = text_value(getattr(ds, "SeriesInstanceUID", ""))
    sop_uid = text_value(getattr(ds, "SOPInstanceUID", ""))
    if not (study_uid and series_uid and sop_uid):
        return None
    return DicomMeta(
        path=path,
        source_subject=source_subject,
        source_container=source_container,
        source_folder_name=source_folder_name,
        source_image_type=source_image_type,
        patient_name=normalized_person_name(text_value(getattr(ds, "PatientName", ""))),
        patient_id=text_value(getattr(ds, "PatientID", "")),
        issuer=text_value(getattr(ds, "IssuerOfPatientID", "")),
        birth_date=text_value(getattr(ds, "PatientBirthDate", "")),
        study_uid=study_uid,
        series_uid=series_uid,
        sop_uid=sop_uid,
        study_date=text_value(getattr(ds, "StudyDate", "")),
        series_date=text_value(getattr(ds, "SeriesDate", "")),
        acquisition_date=text_value(getattr(ds, "AcquisitionDate", "")),
        content_date=text_value(getattr(ds, "ContentDate", "")),
    )


def should_remap_uid(keyword: str) -> bool:
    if not keyword or keyword in UID_EXCLUDE_KEYWORDS:
        return False
    return (
        keyword.endswith("InstanceUID")
        or keyword.endswith("FrameOfReferenceUID")
        or keyword in {"DimensionOrganizationUID", "TrackingUID"}
    )


def replace_text_identity(value, originals: Sequence[str], anonymous_id: str):
    if isinstance(value, str):
        result = value
        for original in originals:
            if original:
                result = re.sub(re.escape(original), anonymous_id, result, flags=re.I)
        return result
    if isinstance(value, (list, tuple)):
        return [replace_text_identity(v, originals, anonymous_id) for v in value]
    return value


def anonymize_dataset(ds: Dataset, meta: DicomMeta, anonymous_id: str, salt: str, shift_days: int) -> None:
    originals = [meta.patient_name, meta.patient_name.replace(" ", "^"), meta.patient_id]

    def visit(dataset: Dataset) -> None:
        for element in list(dataset):
            keyword = element.keyword or ""
            if element.tag.is_private:
                del dataset[element.tag]
                continue
            if keyword in DELETE_KEYWORDS:
                del dataset[element.tag]
                continue
            if keyword in PRESERVE_ORIGINAL_KEYWORDS:
                continue
            if keyword in SENSITIVE_EMPTY_KEYWORDS:
                # 敏感 Sequence 整体删除；普通属性保留空值以兼容类型要求。
                if element.VR == "SQ":
                    del dataset[element.tag]
                else:
                    element.value = ""
                continue
            if element.VR == "SQ":
                for item in element.value or []:
                    visit(item)
                continue

            if keyword == "PatientName":
                element.value = anonymous_id
            elif keyword == "IssuerOfPatientID":
                element.value = ""
            elif element.VR == "UI" and should_remap_uid(keyword):
                if isinstance(element.value, (list, tuple)):
                    element.value = [deterministic_uid(str(v), salt) for v in element.value if v]
                elif element.value:
                    element.value = deterministic_uid(str(element.value), salt)
            elif element.VR == "DA":
                element.value = shift_da(str(element.value), shift_days)
            elif element.VR == "DT":
                element.value = shift_dt(str(element.value), shift_days)
            elif element.VR in {"LO", "LT", "PN", "SH", "ST", "UC", "UT"}:
                element.value = replace_text_identity(element.value, originals, anonymous_id)

    visit(ds)
    ds.PatientName = anonymous_id
    ds.PatientIdentityRemoved = "YES"
    # LO 的最大长度为 64；保持说明简短，避免生成非标准长度值。
    ds.DeidentificationMethod = (
        "Name replaced; PatientID retained; private removed; UID/date map"
    )

    if getattr(ds, "file_meta", None):
        if getattr(ds, "SOPInstanceUID", None):
            ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID


def save_anonymized(meta: DicomMeta, target: Path, anonymous_id: str, salt: str, shift_days: int) -> None:
    ds = pydicom.dcmread(str(meta.path), force=True)
    anonymize_dataset(ds, meta, anonymous_id, salt, shift_days)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        ds.save_as(str(target), enforce_file_format=True)
    except TypeError:  # 兼容旧版 pydicom
        ds.save_as(str(target), write_like_original=False)


def safe_extract_zip(zip_path: Path, destination: Path, max_uncompressed_bytes: int) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    destination_root = destination.resolve()
    total_size = 0
    with zipfile.ZipFile(zip_path, "r") as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            total_size += info.file_size
            if total_size > max_uncompressed_bytes:
                raise ValueError(
                    f"ZIP 解压后大小超过限制 {max_uncompressed_bytes / (1024 ** 3):.1f} GB"
                )
            target = (destination / info.filename).resolve()
            try:
                target.relative_to(destination_root)
            except ValueError as exc:
                raise ValueError(f"ZIP 包含不安全路径: {info.filename}") from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def iter_directory_files(root: Path, excluded_root: Optional[Path] = None) -> Iterator[Path]:
    for base, directories, files in os.walk(root):
        base_path = Path(base)
        if excluded_root:
            directories[:] = [
                name for name in directories if not is_within(base_path / name, excluded_root)
            ]
        for name in files:
            path = base_path / name
            if path.suffix.lower() != ".zip":
                yield path


def discover_zip_files(root: Path, excluded_root: Optional[Path] = None) -> List[Path]:
    result = []
    for base, directories, files in os.walk(root):
        base_path = Path(base)
        if excluded_root:
            directories[:] = [
                name for name in directories if not is_within(base_path / name, excluded_root)
            ]
        for name in files:
            if name.lower().endswith(".zip"):
                result.append(base_path / name)
    return sorted(result)


def is_single_subject_directory(path: Path) -> bool:
    """判断输入目录本身是否就是一个患者目录。"""
    if re.match(r"^\d{4}[-_.]\d{1,2}[-_.]\d{1,2}\s+.+", path.name):
        return True
    try:
        children = list(path.iterdir())
    except OSError:
        return False
    return any(
        (child.is_dir() or child.suffix.lower() == ".zip")
        and image_type_from_source_name(child.name) != "REVIEW"
        for child in children
    )


def source_iterators(
    input_path: Path,
    output_path: Path,
    temp_root: Path,
    max_uncompressed_bytes: int,
) -> Iterator[Tuple[str, str, Path, Iterable[Path]]]:
    if input_path.is_file():
        if input_path.suffix.lower() != ".zip":
            raise ValueError("输入文件必须是 ZIP；或者传入一个目录")
        extracted = temp_root / "zip_0001"
        safe_extract_zip(input_path, extracted, max_uncompressed_bytes)
        yield input_path.stem, input_path.name, extracted, iter_directory_files(extracted)
        return

    single_subject = is_single_subject_directory(input_path)
    # 患者目录作为输入时，目录内的普通文件和 ZIP 必须使用同一个归组键。
    directory_subject = input_path.name if single_subject else ""
    yield directory_subject, input_path.name, input_path, iter_directory_files(input_path, output_path)
    for index, zip_path in enumerate(discover_zip_files(input_path, output_path), start=1):
        extracted = temp_root / f"zip_{index:04d}"
        print(f"[ZIP] 解压 {zip_path}")
        safe_extract_zip(zip_path, extracted, max_uncompressed_bytes)
        relative_zip = zip_path.relative_to(input_path)
        if single_subject:
            subject_hint = input_path.name
        elif len(relative_zip.parts) > 1:
            subject_hint = relative_zip.parts[0]
        else:
            subject_hint = zip_path.stem
        try:
            yield subject_hint, str(relative_zip), extracted, iter_directory_files(extracted)
        finally:
            # 大批量运行时不能把所有 ZIP 的解压内容一直留到任务结束，
            # 否则数百 GB 数据会迅速占满系统盘或临时盘。
            shutil.rmtree(extracted, ignore_errors=True)


def build_patient_lookup(rows: Sequence[dict]) -> Dict[str, str]:
    lookup: Dict[str, str] = {}
    for row in rows:
        key = patient_key(
            row.get("original_patient_id", ""),
            row.get("issuer_of_patient_id", ""),
            row.get("original_patient_name", ""),
            row.get("original_birth_date", ""),
            row.get("source_container", ""),
            row.get("source_subject", ""),
        )
        anonymous_id = row.get("anonymous_patient_id", "")
        if anonymous_id:
            lookup.setdefault(key, anonymous_id)
    return lookup


def run(args: argparse.Namespace) -> int:
    input_path = Path(args.input).resolve()
    output_path = Path(args.output).resolve()
    if not input_path.exists():
        print(f"错误：输入不存在: {input_path}", file=sys.stderr)
        return 1
    if input_path == output_path:
        print("错误：输入和输出不能是同一路径", file=sys.stderr)
        return 1

    mapping_path = (
        Path(args.mapping).resolve()
        if args.mapping
        else output_path.parent / f"{output_path.name}_mapping.csv"
    )
    output_path.mkdir(parents=True, exist_ok=True)

    output_has_dicom = any(output_path.rglob("*.dcm"))
    try:
        salt, previous_rows = read_mapping(
            mapping_path,
            allow_empty_reset=not output_has_dicom,
        )
    except Exception as exc:
        print(f"错误：读取映射表失败: {exc}", file=sys.stderr)
        return 1

    # 立即保存 CONFIG 行，确保即使中途退出，下一次运行仍使用同一 UID 盐值。
    try:
        write_mapping(mapping_path, salt, previous_rows)
    except MappingFileLockedError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    row_by_series: Dict[str, dict] = {
        row.get("original_series_uid", ""): dict(row)
        for row in previous_rows
        if row.get("original_series_uid")
    }
    patient_lookup = build_patient_lookup(previous_rows)
    anonymous_ids = {row.get("anonymous_patient_id", "") for row in previous_rows}

    scanned = dicom_count = success = duplicates = errors = 0
    last_save_count = 0
    series_seen_this_run: set[str] = set()
    max_bytes = int(args.max_uncompressed_gb * 1024 ** 3)

    # 临时解压目录放在输出目录所在磁盘。大型 ZIP 可能远超系统盘空间，
    # 并且同盘写入也避免跨盘复制临时文件。
    with tempfile.TemporaryDirectory(
        prefix="dicom_anon_",
        dir=str(output_path.parent),
    ) as temp_dir:
        try:
            sources = source_iterators(input_path, output_path, Path(temp_dir), max_bytes)
            for subject_hint, source_container, source_root, files in sources:
                for path in files:
                    scanned += 1
                    relative_path = path.relative_to(source_root)
                    relative_parent = relative_path.parent
                    if subject_hint:
                        source_subject = subject_hint
                    elif len(relative_path.parts) > 1:
                        source_subject = relative_path.parts[0]
                    else:
                        source_subject = input_path.name
                    if source_container.lower().endswith(".zip"):
                        source_folder_name = Path(source_container).stem
                    else:
                        name_candidates = [input_path.name, *relative_parent.parts]
                        typed_candidates = [
                            name
                            for name in name_candidates
                            if image_type_from_source_name(name) != "REVIEW"
                        ]
                        if typed_candidates:
                            source_folder_name = typed_candidates[-1]
                        elif len(relative_path.parts) > 1:
                            source_folder_name = relative_path.parts[0]
                        else:
                            source_folder_name = input_path.name
                    source_image_type = image_type_from_source_name(source_folder_name)
                    meta = read_meta(
                        path,
                        source_subject,
                        source_container,
                        source_folder_name,
                        source_image_type,
                    )
                    if meta is None:
                        continue
                    dicom_count += 1

                    pkey = patient_key(
                        meta.patient_id,
                        meta.issuer,
                        meta.patient_name,
                        meta.birth_date,
                        meta.source_container,
                        meta.source_subject,
                    )
                    anonymous_id = patient_lookup.get(pkey)
                    # 兼容旧映射表：旧版本只按 DICOM PatientID/姓名建键。
                    # 第一次升级运行时复用已存在的匿名编号，再绑定到外层患者目录。
                    legacy_pkey = patient_key(
                        meta.patient_id,
                        meta.issuer,
                        meta.patient_name,
                        meta.birth_date,
                        meta.source_container,
                    )
                    if not anonymous_id:
                        anonymous_id = patient_lookup.get(legacy_pkey)
                    if not anonymous_id:
                        anonymous_id = next_anonymous_id(anonymous_ids)
                        anonymous_ids.add(anonymous_id)
                        patient_lookup[pkey] = anonymous_id
                        print(f"[患者] 新建匿名编号 {anonymous_id}")
                    else:
                        patient_lookup[pkey] = anonymous_id
                    patient_lookup[legacy_pkey] = anonymous_id

                    shift_days = date_shift_for(anonymous_id, salt)
                    category = meta.source_image_type
                    anonymous_study_uid = deterministic_uid(meta.study_uid, salt)
                    anonymous_series_uid = deterministic_uid(meta.series_uid, salt)
                    anonymous_sop_uid = deterministic_uid(meta.sop_uid, salt)
                    target = (
                        output_path
                        / sanitize_path_part(category)
                        / sanitize_path_part(standardized_source_folder_name(meta))
                        / anonymous_study_uid
                        / anonymous_series_uid
                        / f"{anonymous_sop_uid}.dcm"
                    )

                    row = row_by_series.get(meta.series_uid)
                    if row is None:
                        row = {k: "" for k in MAPPING_FIELDS}
                        row.update(
                            {
                                "record_type": "SERIES",
                                "anonymous_patient_id": anonymous_id,
                                "original_patient_name": meta.patient_name,
                                "original_patient_id": meta.patient_id,
                                "issuer_of_patient_id": meta.issuer,
                                "original_birth_date": meta.birth_date,
                                "date_shift_days": str(shift_days),
                                "source_subject": meta.source_subject,
                                "source_folder_name": meta.source_folder_name,
                                "source_image_type": meta.source_image_type,
                                "original_study_uid": meta.study_uid,
                                "anonymous_study_uid": anonymous_study_uid,
                                "original_series_uid": meta.series_uid,
                                "anonymous_series_uid": anonymous_series_uid,
                                "file_count": "0",
                            }
                        )
                        row_by_series[meta.series_uid] = row

                    # 已有映射可能来自旧版本并指向另一个匿名目录；以当前外层
                    # 患者目录的归组结果为准，同时更新日期偏移和来源归组信息。
                    row["anonymous_patient_id"] = anonymous_id
                    row["source_subject"] = meta.source_subject
                    row["source_folder_name"] = meta.source_folder_name
                    row["source_image_type"] = meta.source_image_type

                    if meta.series_uid not in series_seen_this_run:
                        row["file_count"] = "0"
                        series_seen_this_run.add(meta.series_uid)
                    row["file_count"] = str(int(row.get("file_count") or 0) + 1)
                    if target.exists() and not args.overwrite:
                        duplicates += 1
                    else:
                        try:
                            save_anonymized(meta, target, anonymous_id, salt, shift_days)
                            success += 1
                        except Exception as exc:
                            errors += 1
                            print(f"[错误] {path}: {exc}", file=sys.stderr)

                    if dicom_count <= 3 or dicom_count % 100 == 0:
                        print(
                            f"[进度] DICOM={dicom_count} 成功={success} "
                            f"重复={duplicates} 错误={errors}"
                        )
                    if dicom_count - last_save_count >= 500:
                        write_mapping(mapping_path, salt, list(row_by_series.values()))
                        last_save_count = dicom_count
        except MappingFileLockedError as exc:
            errors += 1
            print(f"错误：{exc}", file=sys.stderr)
        except Exception as exc:
            errors += 1
            print(f"处理失败: {exc}", file=sys.stderr)
        finally:
            try:
                write_mapping(mapping_path, salt, list(row_by_series.values()))
            except MappingFileLockedError as exc:
                errors += 1
                print(f"错误：{exc}", file=sys.stderr)

    review_series = sum(
        1
        for row in row_by_series.values()
        if row.get("source_image_type") == "REVIEW"
    )
    print("=" * 64)
    print("处理完成")
    print(f"扫描文件: {scanned}")
    print(f"识别 DICOM: {dicom_count}")
    print(f"成功匿名化: {success}")
    print(f"已存在跳过: {duplicates}")
    print(f"错误: {errors}")
    print(f"待人工复核 Series: {review_series}")
    print(f"匿名化目录: {output_path}")
    print(f"私有映射表: {mapping_path}")
    print("注意：映射表包含原始身份信息，请与匿名化影像分开保管。")
    print("=" * 64)
    return 0 if errors == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DICOM 匿名化、按来源名称整包分类与转存工具")
    parser.add_argument("input", help="原始数据目录或 ZIP 文件")
    parser.add_argument("output", help="匿名化输出目录")
    parser.add_argument(
        "--mapping",
        help="映射表路径，默认在输出目录旁生成 <输出目录名>_mapping.csv",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="覆盖已经存在的匿名化 DICOM；默认跳过",
    )
    parser.add_argument(
        "--max-uncompressed-gb",
        type=float,
        default=200.0,
        help="单个 ZIP 允许的最大解压后大小，默认 200 GB",
    )
    return parser


def main() -> None:
    raise SystemExit(run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
