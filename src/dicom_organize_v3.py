#!/usr/bin/env python3
"""DICOM 原始影像转存与患者身份归组 V3（无数据库版）。

V3 在 V2 的安全复制、UID 路径、冲突保留和 Study 汇总基础上增加：

* 先扫描整批 DICOM Header，建立全局 ``PatientID -> 患者身份`` 路由；
* 来源患者目录中混入其他 PatientID 时，按全局路由分流；
* 未匹配 PatientID 使用 DICOM Header 的 ``PatientName_PatientID``；
* 姓名相同但 PatientID 不同时，用出生日期、性别以及年龄/检查日期一致性
  做保守复合判断；证据不足不合并，任务也不会暂停；
* 确认同一自然人具有多个 PatientID 时输出
  ``患者姓名/患者姓名_PatientID/Study/Series/SOP.dcm``；单 PatientID 仍保持
  ``患者姓名_PatientID/Study/Series/SOP.dcm``；
* 使用敏感 CSV 映射维持跨批次身份稳定，不连接数据库。

V2 文件不被修改；V3 复用 V2 中已经验证过的原子复制、SHA256 冲突保护、
Series 分类和 Study 汇总函数。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import pickle
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import unicodedata
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence

import pydicom

import dicom_organize_v2 as v2


RULE_VERSION = "2026.08.21-identity-v3.4"
IDENTITY_SCAN_TAGS = list(
    dict.fromkeys(
        [
            *v2.SCAN_TAGS,
            "PatientBirthDate",
            "PatientSex",
            "PatientAge",
            "InstitutionName",
        ]
    )
)

IDENTITY_MAPPING_COLUMNS = [
    "master_patient_key",
    "canonical_name",
    "patient_id",
    "raw_header_patient_name",
    "header_patient_name",
    "birth_date",
    "sex",
    "institutions",
    "estimated_birth_year",
    "all_patient_ids",
    "match_status",
    "match_basis",
    "source_subjects",
]

IDENTITY_REVIEW_COLUMNS = [
    "review_status",
    "left_patient_id",
    "left_name",
    "right_patient_id",
    "right_name",
    "left_birth_date",
    "right_birth_date",
    "left_sex",
    "right_sex",
    "left_institutions",
    "right_institutions",
    "left_estimated_birth_year",
    "right_estimated_birth_year",
    "match_basis",
    "suggested_action",
]

SOURCE_AUDIT_COLUMNS = [
    "source_subject",
    "source_subject_name",
    "dominant_patient_id",
    "detected_patient_id",
    "canonical_name",
    "study_count",
    "file_count",
    "action",
    "destination_patient_path",
]

_TRANSFER_LOCKS = tuple(threading.Lock() for _ in range(257))
_CREATED_DIRECTORY_LOCK = threading.Lock()
_CREATED_DIRECTORIES: set[str] = set()
COPY_CHUNK_SIZE = 8 * 1024 * 1024

# 只识别 PatientName 末尾结构明确的年龄/性别噪声，不做宽松编辑距离匹配。
NAME_DEMOGRAPHIC_SUFFIXES = (
    re.compile(
        r"^(?P<name>.+?)\s+(?P<sex>[MF])\s*[-_/ ]?\s*"
        r"(?P<age>\d{1,3})\s*(?:Y|YR|YRS|YEAR|YEARS)$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?P<name>.+?)\s+(?P<age>\d{1,3})\s*"
        r"(?:Y|YR|YRS|YEAR|YEARS)\s*[-_/ ]?\s*(?P<sex>[MF])$",
        re.IGNORECASE,
    ),
)


@dataclass
class IdentityObservation:
    patient_id: str = ""
    raw_header_patient_name: str = ""
    header_patient_name: str = ""
    strong_folder_name: str = ""
    source_subject: str = ""
    source_subject_name: str = ""
    birth_date: str = ""
    sex: str = ""
    institution_name: str = ""
    age: str = ""
    study_date: str = ""
    study_uid: str = ""
    demographic_conflicts: tuple[str, ...] = ()


@dataclass
class V3ScanResult:
    scan: v2.ScanResult
    identity: Optional[IdentityObservation] = None


@dataclass
class SubjectPatientStats:
    study_uids: set[str] = field(default_factory=set)
    file_count: int = 0


@dataclass
class IdentityAggregate:
    patient_id: str
    header_names: Counter[str] = field(default_factory=Counter)
    raw_header_names: Counter[str] = field(default_factory=Counter)
    trusted_names: Counter[str] = field(default_factory=Counter)
    subject_names: Counter[str] = field(default_factory=Counter)
    birth_dates: Counter[str] = field(default_factory=Counter)
    sexes: Counter[str] = field(default_factory=Counter)
    institutions: Counter[str] = field(default_factory=Counter)
    estimated_birth_years: list[float] = field(default_factory=list)
    source_subjects: set[str] = field(default_factory=set)
    study_uids: set[str] = field(default_factory=set)
    file_count: int = 0
    internal_conflicts: set[str] = field(default_factory=set)

    def add(self, observation: IdentityObservation) -> None:
        if observation.raw_header_patient_name:
            self.raw_header_names[observation.raw_header_patient_name] += 1
        if observation.header_patient_name:
            self.header_names[observation.header_patient_name] += 1
        if observation.strong_folder_name:
            self.trusted_names[observation.strong_folder_name] += 1
        if observation.birth_date:
            self.birth_dates[observation.birth_date] += 1
        if observation.sex:
            self.sexes[observation.sex] += 1
        if observation.institution_name:
            self.institutions[observation.institution_name] += 1
        birth_year = estimated_birth_year(observation.age, observation.study_date)
        if birth_year is not None:
            self.estimated_birth_years.append(birth_year)
        if observation.source_subject:
            self.source_subjects.add(observation.source_subject)
        if observation.study_uid:
            self.study_uids.add(observation.study_uid)
        self.internal_conflicts.update(observation.demographic_conflicts)
        self.file_count += 1


@dataclass(frozen=True)
class IdentityProfile:
    patient_id: str
    canonical_name: str
    raw_header_patient_name: str = ""
    header_patient_name: str = ""
    birth_date: str = ""
    sex: str = ""
    institutions: tuple[str, ...] = ()
    estimated_birth_year: Optional[float] = None
    source_subjects: tuple[str, ...] = ()
    internal_conflicts: tuple[str, ...] = ()


@dataclass(frozen=True)
class IdentityRoute:
    master_patient_key: str
    canonical_name: str
    patient_id: str
    all_patient_ids: tuple[str, ...]
    match_status: str
    match_basis: str
    group_folder: str = ""

    @property
    def patient_folder(self) -> str:
        return v2.sanitize_component(
            f"{self.canonical_name or 'Unknown'}_{self.patient_id or 'NoID'}"
        )

    @property
    def destination_patient_path(self) -> str:
        if self.group_folder:
            return str(Path(self.group_folder) / self.patient_folder)
        return self.patient_folder


@dataclass
class IdentityPlan:
    routes: dict[str, IdentityRoute]
    mapping_rows: list[dict[str, str]]
    review_rows: list[dict[str, str]]
    source_audit_rows: list[dict[str, str]]


class UnionFind:
    def __init__(self, values: Iterable[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root

    def groups(self) -> list[set[str]]:
        grouped: dict[str, set[str]] = defaultdict(set)
        for value in self.parent:
            grouped[self.find(value)].add(value)
        return list(grouped.values())


class IdentityPlanner:
    def __init__(self) -> None:
        self.aggregates: dict[str, IdentityAggregate] = {}
        self.subject_stats: dict[str, dict[str, SubjectPatientStats]] = defaultdict(dict)
        self.subject_names: dict[str, str] = {}

    def add(self, observation: IdentityObservation) -> None:
        patient_id = observation.patient_id
        if not patient_id:
            return
        aggregate = self.aggregates.setdefault(patient_id, IdentityAggregate(patient_id))
        aggregate.add(observation)
        subject = observation.source_subject
        if subject:
            stats = self.subject_stats[subject].setdefault(
                patient_id, SubjectPatientStats()
            )
            if observation.study_uid:
                stats.study_uids.add(observation.study_uid)
            stats.file_count += 1
            if observation.source_subject_name:
                self.subject_names[subject] = observation.source_subject_name

    def dominant_patient_ids(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for subject, patient_stats in self.subject_stats.items():
            ranked = sorted(
                patient_stats.items(),
                key=lambda item: (
                    len(item[1].study_uids), item[1].file_count, item[0]
                ),
                reverse=True,
            )
            if len(ranked) == 1:
                result[subject] = ranked[0][0]
                continue
            first_score = (len(ranked[0][1].study_uids), ranked[0][1].file_count)
            second_score = (len(ranked[1][1].study_uids), ranked[1][1].file_count)
            if first_score != second_score:
                result[subject] = ranked[0][0]
        return result

    def profiles(self) -> dict[str, IdentityProfile]:
        # 中文来源目录名不能按文件量分配。混合患者目录里，文件最多的人并不
        # 一定就是目录名所指的人；应由中文姓名拼音与 Header 姓名匹配。
        for aggregate in self.aggregates.values():
            aggregate.subject_names.clear()
        for subject, patient_stats in self.subject_stats.items():
            subject_name = self.subject_names.get(subject, "")
            if not subject_name:
                continue
            matched_ids = [
                patient_id
                for patient_id in patient_stats
                if patient_id in self.aggregates
                and chinese_name_matches_header(
                    subject_name,
                    modal(self.aggregates[patient_id].header_names),
                )
            ]
            for patient_id in matched_ids:
                self.aggregates[patient_id].subject_names[subject_name] += 1

            # 只有一个 PatientID 且 Header 没有可用姓名时，才允许用中文目录补名。
            if not matched_ids and len(patient_stats) == 1:
                patient_id = next(iter(patient_stats))
                aggregate = self.aggregates.get(patient_id)
                if aggregate is not None and not modal(aggregate.header_names):
                    aggregate.subject_names[subject_name] += 1

        return {
            patient_id: profile_from_aggregate(aggregate)
            for patient_id, aggregate in self.aggregates.items()
        }


def parse_patient_name(value: object) -> tuple[str, str, str]:
    """返回 ``(姓名主体, 尾部性别, 尾部DICOM年龄)``。

    例如 ``WANG XIAOMING M-56Y^^^^`` 返回
    ``("WANG XIAOMING", "M", "056Y")``。原始值仍另外保存用于审计。
    """
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"[\^\s]+", " ", text).strip(" ,;|/\\_-")
    for pattern in NAME_DEMOGRAPHIC_SUFFIXES:
        match = pattern.fullmatch(text)
        if not match:
            continue
        name = re.sub(r"\s+", " ", match.group("name")).strip(" ,;|/\\_-")
        sex = match.group("sex").upper()
        age = f"{int(match.group('age')):03d}Y"
        return name, sex, age
    return text, "", ""


def clean_patient_name(value: object) -> str:
    return parse_patient_name(value)[0]


def normalize_name(value: object) -> str:
    cleaned = clean_patient_name(value)
    normalized = "".join(
        character.casefold()
        for character in unicodedata.normalize("NFKC", cleaned)
        if character.isalnum()
    )
    if normalized in {
        "unknown", "anonymous", "anonymized", "patient", "noname", "无名氏",
    }:
        return ""
    return normalized


_GBK_INITIAL_RANGES = (
    (-20319, -20284, "A"), (-20283, -19776, "B"),
    (-19775, -19219, "C"), (-19218, -18711, "D"),
    (-18710, -18527, "E"), (-18526, -18240, "F"),
    (-18239, -17923, "G"), (-17922, -17418, "H"),
    (-17417, -16475, "J"), (-16474, -16213, "K"),
    (-16212, -15641, "L"), (-15640, -15166, "M"),
    (-15165, -14923, "N"), (-14922, -14915, "O"),
    (-14914, -14631, "P"), (-14630, -14150, "Q"),
    (-14149, -14091, "R"), (-14090, -13319, "S"),
    (-13318, -12839, "T"), (-12838, -12557, "W"),
    (-12556, -11848, "X"), (-11847, -11056, "Y"),
    (-11055, -10247, "Z"),
)


def chinese_pinyin_initials(value: object) -> str:
    """用 GB2312 拼音排序取得常用汉字首字母，无需额外安装拼音库。"""
    initials: list[str] = []
    for character in str(value or ""):
        if not v2.CHINESE_TEXT_PATTERN.search(character):
            continue
        try:
            encoded = character.encode("gbk")
        except UnicodeEncodeError:
            return ""
        if len(encoded) != 2:
            return ""
        code = encoded[0] * 256 + encoded[1] - 65536
        initial = next(
            (letter for start, end, letter in _GBK_INITIAL_RANGES if start <= code <= end),
            "",
        )
        if not initial:
            return ""
        initials.append(initial)
    return "".join(initials)


def latin_name_has_initial_sequence(value: object, initials: str) -> bool:
    compact = "".join(
        character.upper()
        for character in clean_patient_name(value)
        if "A" <= character.upper() <= "Z"
    )
    if not compact or len(initials) < 2 or compact[0] != initials[0]:
        return False

    positions = {0}
    for initial_index, initial in enumerate(initials[1:], start=1):
        remaining_initials = len(initials) - initial_index - 1
        next_positions: set[int] = set()
        for position in positions:
            for candidate in range(position + 1, len(compact)):
                if compact[candidate] != initial:
                    continue
                # 每个音节至少保留一个字母，避免越界式的伪匹配。
                if len(compact) - candidate - 1 < remaining_initials:
                    continue
                next_positions.add(candidate)
        positions = next_positions
        if not positions:
            return False
    return bool(positions)


def chinese_name_matches_header(chinese_name: object, header_name: object) -> bool:
    chinese = "".join(v2.CHINESE_TEXT_PATTERN.findall(str(chinese_name or "")))
    initials = chinese_pinyin_initials(chinese)
    return bool(initials and latin_name_has_initial_sequence(header_name, initials))


def normalize_institution(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    compact = "".join(character for character in text if character.isalnum())
    if not compact:
        return ""
    if (
        ("xiongan" in compact and "xuanwu" in compact)
        or ("雄安" in compact and "宣武" in compact)
    ):
        return "xiongan_xuanwu_hospital"
    return compact


def normalize_date(value: object) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) < 8:
        return ""
    try:
        return datetime.strptime(digits[:8], "%Y%m%d").strftime("%Y%m%d")
    except ValueError:
        return ""


def normalize_sex(value: object) -> str:
    text = str(value or "").strip().upper()
    return text if text in {"M", "F"} else ""


def age_in_years(value: object) -> Optional[float]:
    match = re.fullmatch(r"\s*(\d{1,3})([DWMY])\s*", str(value or "").upper())
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2)
    return {
        "D": amount / 365.25,
        "W": amount / 52.18,
        "M": amount / 12,
        "Y": float(amount),
    }[unit]


def estimated_birth_year(age: object, study_date: object) -> Optional[float]:
    years = age_in_years(age)
    date = normalize_date(study_date)
    if years is None or not date:
        return None
    return int(date[:4]) - years


def modal(counter: Counter[str]) -> str:
    if not counter:
        return ""
    return sorted(counter.items(), key=lambda item: (-item[1], item[0]))[0][0]


def choose_canonical_name(names: Iterable[str]) -> str:
    counter = Counter(name for name in names if name)
    if not counter:
        return "Unknown"
    return sorted(
        counter.items(),
        key=lambda item: (
            -item[1],
            -bool(v2.CHINESE_TEXT_PATTERN.search(item[0])),
            item[0],
        ),
    )[0][0]


def profile_from_aggregate(aggregate: IdentityAggregate) -> IdentityProfile:
    name_sources = (
        aggregate.trusted_names
        or aggregate.subject_names
        or aggregate.header_names
    )
    canonical_name = modal(name_sources) or "Unknown"
    internal_conflicts: list[str] = list(aggregate.internal_conflicts)
    birth_date = modal(aggregate.birth_dates)
    if len(aggregate.birth_dates) > 1:
        internal_conflicts.append("birth_date")
    if len(aggregate.sexes) > 1:
        internal_conflicts.append("sex")
    if aggregate.estimated_birth_years:
        if max(aggregate.estimated_birth_years) - min(aggregate.estimated_birth_years) > 1.25:
            internal_conflicts.append("age")
        birth_year = statistics.median(aggregate.estimated_birth_years)
        if birth_date and abs(int(birth_date[:4]) - birth_year) > 1.25:
            internal_conflicts.append("age_vs_birth_date")
    else:
        birth_year = None
    return IdentityProfile(
        patient_id=aggregate.patient_id,
        canonical_name=canonical_name,
        raw_header_patient_name=modal(aggregate.raw_header_names),
        header_patient_name=modal(aggregate.header_names),
        birth_date=birth_date,
        sex=modal(aggregate.sexes),
        institutions=tuple(sorted(aggregate.institutions)),
        estimated_birth_year=birth_year,
        source_subjects=tuple(sorted(aggregate.source_subjects)),
        internal_conflicts=tuple(sorted(set(internal_conflicts))),
    )


def compare_profiles(
    left: IdentityProfile,
    right: IdentityProfile,
) -> tuple[str, str]:
    """返回 confirmed、review_group、possible、conflict 或 unrelated。"""
    left_canonical = normalize_name(left.canonical_name)
    right_canonical = normalize_name(right.canonical_name)
    left_header = normalize_name(left.header_patient_name)
    right_header = normalize_name(right.header_patient_name)
    if left_canonical and left_canonical == right_canonical:
        name_basis = "canonical_name"
    elif left_header and left_header == right_header:
        name_basis = "header_patient_name"
    else:
        return "unrelated", "normalized_name_different"
    conflicts = set(left.internal_conflicts) | set(right.internal_conflicts)
    evidence: list[str] = []
    if left.birth_date and right.birth_date:
        if left.birth_date == right.birth_date:
            evidence.append("birth_date")
        else:
            conflicts.add("birth_date")
    if left.sex and right.sex:
        if left.sex == right.sex:
            evidence.append("sex")
        else:
            conflicts.add("sex")
    institution_overlap = sorted(set(left.institutions) & set(right.institutions))
    if institution_overlap:
        evidence.append("institution")
    if (
        left.estimated_birth_year is not None
        and right.estimated_birth_year is not None
    ):
        if abs(left.estimated_birth_year - right.estimated_birth_year) <= 1.25:
            evidence.append("age_consistent_with_study_date")
        else:
            conflicts.add("age")
    # 用户约定：姓名、性别、医院一致而出生日期不同，仍放入同一患者外层目录，
    # 但身份结论只能是“归组待确认”，不能伪装成自动确认。
    if conflicts == {"birth_date"} and {"sex", "institution"}.issubset(evidence):
        return (
            "review_group",
            f"same_name({name_basis});demographics="
            + "+".join(evidence)
            + ";conflict=birth_date",
        )
    if conflicts:
        return (
            "conflict",
            f"same_name({name_basis});conflict=" + "+".join(sorted(conflicts)),
        )
    if len(set(evidence)) >= 2:
        return (
            "confirmed",
            f"same_name({name_basis});demographics=" + "+".join(evidence),
        )
    return (
        "possible",
        f"same_name({name_basis});insufficient_demographics="
        + ("+".join(evidence) or "none"),
    )


def source_subject_for(path: Path, source_root: Path) -> str:
    relative = path.relative_to(source_root)
    return relative.parts[0] if len(relative.parts) > 1 else source_root.name


def strong_folder_name_for(path: Path, source_root: Path, patient_id: str) -> str:
    if not patient_id:
        return ""
    try:
        components = list(path.relative_to(source_root).parent.parts)
    except ValueError:
        components = list(path.parent.parts)
    normalized_id = patient_id.casefold()
    candidates: list[tuple[int, str]] = []
    for index, component in enumerate(components):
        if normalized_id not in component.casefold():
            continue
        name = v2.chinese_name_from_component(component)
        if name:
            score = 300 - min(index, 40)
            if 2 <= len(name) <= 6:
                score += 20
            candidates.append((score, name))

    # 单患者运行时，源目录本身通常就是最可靠的 ``姓名_PatientID`` 目录。
    root_component = source_root.name
    if normalized_id in root_component.casefold():
        root_name = v2.chinese_name_from_component(root_component)
        if root_name:
            score = 310 + (20 if 2 <= len(root_name) <= 6 else 0)
            candidates.append((score, root_name))
    return max(candidates, default=(0, ""), key=lambda item: item[0])[1]


def merge_mapping_and_observed_profile(
    mapped: IdentityProfile,
    observed: IdentityProfile,
) -> tuple[IdentityProfile, tuple[str, ...]]:
    """历史映射优先，同时补入本批新证据并标记人口学漂移。"""
    conflicts = set(mapped.internal_conflicts) | set(observed.internal_conflicts)
    drift: list[str] = []
    if mapped.birth_date and observed.birth_date and mapped.birth_date != observed.birth_date:
        conflicts.add("birth_date")
        drift.append("birth_date")
    if mapped.sex and observed.sex and mapped.sex != observed.sex:
        conflicts.add("sex")
        drift.append("sex")
    if (
        mapped.estimated_birth_year is not None
        and observed.estimated_birth_year is not None
        and abs(mapped.estimated_birth_year - observed.estimated_birth_year) > 1.25
    ):
        conflicts.add("age")
        drift.append("age")
    mapped_name = normalize_name(mapped.canonical_name)
    observed_name = normalize_name(observed.canonical_name)
    mapped_header_name = normalize_name(mapped.header_patient_name)
    observed_header_name = normalize_name(observed.header_patient_name)
    if (
        mapped_name
        and observed_name
        and mapped_name != observed_name
        and not (
            mapped_header_name
            and mapped_header_name == observed_header_name
        )
        and observed.canonical_name != "Unknown"
    ):
        drift.append("name")

    canonical_name = mapped.canonical_name
    mapped_is_chinese = bool(v2.CHINESE_TEXT_PATTERN.search(mapped.canonical_name))
    observed_is_chinese = bool(v2.CHINESE_TEXT_PATTERN.search(observed.canonical_name))
    mapped_name_matches_header = (
        mapped_is_chinese
        and chinese_name_matches_header(
            mapped.canonical_name, observed.header_patient_name
        )
    )
    if (
        not canonical_name
        or canonical_name == "Unknown"
        or (observed_is_chinese and not mapped_is_chinese)
        or (mapped_is_chinese and not mapped_name_matches_header and not observed_is_chinese)
    ):
        canonical_name = observed.canonical_name
    return (
        IdentityProfile(
            patient_id=observed.patient_id,
            canonical_name=canonical_name or "Unknown",
            raw_header_patient_name=(
                observed.raw_header_patient_name or mapped.raw_header_patient_name
            ),
            header_patient_name=(
                observed.header_patient_name or mapped.header_patient_name
            ),
            birth_date=mapped.birth_date or observed.birth_date,
            sex=mapped.sex or observed.sex,
            estimated_birth_year=(
                mapped.estimated_birth_year
                if mapped.estimated_birth_year is not None
                else observed.estimated_birth_year
            ),
            source_subjects=tuple(
                sorted(set(mapped.source_subjects) | set(observed.source_subjects))
            ),
            institutions=tuple(
                sorted(set(mapped.institutions) | set(observed.institutions))
            ),
            internal_conflicts=tuple(sorted(conflicts)),
        ),
        tuple(sorted(set(drift))),
    )


def scan_one_file(path: Path, source_root: Path) -> V3ScanResult:
    relative_path = str(path.relative_to(source_root))
    relative_hash = v2.stable_hash(relative_path.replace("\\", "/"))
    prefix = False
    try:
        with path.open("rb") as stream:
            header = stream.read(132)
            prefix = len(header) >= 132 and header[128:132] == b"DICM"
            stream.seek(0)
            dataset = pydicom.dcmread(
                stream,
                force=True,
                stop_before_pixels=True,
                specific_tags=IDENTITY_SCAN_TAGS,
            )
    except Exception as exc:
        return V3ScanResult(
            v2.ScanResult(
                path, relative_path, relative_hash, prefix, error=str(exc)
            )
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
        and not any(v2.text_value(value) for value in uid_values)
        and sum(bool(v2.text_value(value)) for value in supporting_values) < 3
    ):
        return V3ScanResult(
            v2.ScanResult(path, relative_path, relative_hash, False)
        )

    patient_id = v2.normalize_patient_id(getattr(dataset, "PatientID", ""))
    raw_header_name = v2.text_value(getattr(dataset, "PatientName", ""))
    header_name, embedded_sex, embedded_age = parse_patient_name(raw_header_name)
    study_date = v2.text_value(getattr(dataset, "StudyDate", ""))
    meta = v2.DicomMeta(
        patient_name=header_name,
        patient_id=patient_id,
        study_uid=v2.text_value(getattr(dataset, "StudyInstanceUID", "")),
        series_uid=v2.text_value(getattr(dataset, "SeriesInstanceUID", "")),
        sop_uid=v2.text_value(getattr(dataset, "SOPInstanceUID", "")),
        sop_class_uid=v2.text_value(getattr(dataset, "SOPClassUID", "")),
        study_date=study_date,
        acquisition_date=v2.text_value(getattr(dataset, "AcquisitionDate", "")),
        modality=v2.normalize_modality(v2.text_value(getattr(dataset, "Modality", ""))),
        series_description=v2.text_value(getattr(dataset, "SeriesDescription", "")),
        positioner_motion=v2.text_value(
            getattr(dataset, "PositionerMotion", "")
        ).upper(),
        number_of_frames=v2.int_value(getattr(dataset, "NumberOfFrames", None)),
        slice_thickness=v2.text_value(getattr(dataset, "SliceThickness", "")),
    )
    subject = source_subject_for(path, source_root)
    tag_sex = normalize_sex(getattr(dataset, "PatientSex", ""))
    tag_age = v2.text_value(getattr(dataset, "PatientAge", "")).upper()
    demographic_conflicts: list[str] = []
    if tag_sex and embedded_sex and tag_sex != embedded_sex:
        demographic_conflicts.append("sex_vs_patient_name_suffix")
    if tag_age and embedded_age:
        tag_age_years = age_in_years(tag_age)
        embedded_age_years = age_in_years(embedded_age)
        if (
            tag_age_years is not None
            and embedded_age_years is not None
            and abs(tag_age_years - embedded_age_years) > 1
        ):
            demographic_conflicts.append("age_vs_patient_name_suffix")
    observation = IdentityObservation(
        patient_id=patient_id,
        raw_header_patient_name=raw_header_name,
        header_patient_name=header_name,
        strong_folder_name=strong_folder_name_for(path, source_root, patient_id),
        source_subject=subject,
        source_subject_name=v2.chinese_name_from_component(subject),
        birth_date=normalize_date(getattr(dataset, "PatientBirthDate", "")),
        sex=tag_sex or embedded_sex,
        institution_name=normalize_institution(
            getattr(dataset, "InstitutionName", "")
        ),
        age=tag_age or embedded_age,
        study_date=study_date,
        study_uid=meta.study_uid,
        demographic_conflicts=tuple(demographic_conflicts),
    )
    return V3ScanResult(
        v2.ScanResult(path, relative_path, relative_hash, True, meta=meta),
        observation,
    )


def scan_to_json(scan: V3ScanResult) -> str:
    return json.dumps(
        {
            "path": str(scan.scan.path),
            "relative_path": scan.scan.relative_path,
            "relative_path_hash": scan.scan.relative_path_hash,
            "is_dicom": scan.scan.is_dicom,
            "error": scan.scan.error,
            "meta": asdict(scan.scan.meta) if scan.scan.meta else None,
            "identity": asdict(scan.identity) if scan.identity else None,
        },
        ensure_ascii=False,
    )


def scan_from_json(line: str) -> V3ScanResult:
    payload = json.loads(line)
    meta = v2.DicomMeta(**payload["meta"]) if payload.get("meta") else None
    identity = (
        IdentityObservation(**payload["identity"])
        if payload.get("identity")
        else None
    )
    return V3ScanResult(
        v2.ScanResult(
            Path(payload["path"]),
            payload["relative_path"],
            payload["relative_path_hash"],
            bool(payload["is_dicom"]),
            meta=meta,
            error=payload.get("error", ""),
        ),
        identity,
    )


def load_identity_mapping(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    if path.suffix.lower() == ".json":
        with path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
        source_rows = payload.get("rows", payload) if isinstance(payload, dict) else payload
        return [
            {
                column: str(row.get(column, "") or "").strip()
                for column in IDENTITY_MAPPING_COLUMNS
            }
            for row in source_rows
            if isinstance(row, dict) and str(row.get("patient_id", "") or "").strip()
        ]
    if path.suffix.lower() == ".xlsx":
        script_path = Path(__file__).with_name("read_dicom_identity_mapping.mjs")
        completed = subprocess.run(
            [v2.find_node_executable(), str(script_path), str(path)],
            cwd=str(script_path.parent),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=120,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f"读取综合Excel中的患者身份映射失败: {detail}")
        matrix = json.loads(completed.stdout or "[]")
        if not matrix:
            return []
        headers = [str(value or "").strip() for value in matrix[0]]
        required = {"master_patient_key", "canonical_name", "patient_id"}
        if not required.issubset(set(headers)):
            return []
        rows: list[dict[str, str]] = []
        for values in matrix[1:]:
            source = {
                header: str(
                    (values[index] if index < len(values) else "") or ""
                ).strip()
                for index, header in enumerate(headers)
            }
            if source.get("patient_id"):
                rows.append(
                    {
                        column: source.get(column, "")
                        for column in IDENTITY_MAPPING_COLUMNS
                    }
                )
        return rows
    with path.open("r", newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        required = {"master_patient_key", "canonical_name", "patient_id"}
        if not required.issubset(set(reader.fieldnames or [])):
            raise ValueError(f"患者身份映射表缺少必要列: {path}")
        return [
            {column: str(row.get(column, "") or "").strip() for column in IDENTITY_MAPPING_COLUMNS}
            for row in reader
            if str(row.get("patient_id", "") or "").strip()
        ]


def next_master_number(rows: Sequence[dict[str, str]]) -> int:
    highest = 0
    for row in rows:
        match = re.fullmatch(r"G(\d+)", row.get("master_patient_key", ""), re.I)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def mapping_profile(row: dict[str, str]) -> IdentityProfile:
    try:
        birth_year = float(row.get("estimated_birth_year", ""))
    except (TypeError, ValueError):
        birth_year = None
    return IdentityProfile(
        patient_id=row.get("patient_id", ""),
        canonical_name=row.get("canonical_name", "") or "Unknown",
        raw_header_patient_name=(
            row.get("raw_header_patient_name", "")
            or row.get("header_patient_name", "")
        ),
        header_patient_name=clean_patient_name(
            row.get("header_patient_name", "")
        ),
        birth_date=normalize_date(row.get("birth_date", "")),
        sex=normalize_sex(row.get("sex", "")),
        institutions=tuple(
            sorted(
                {
                    normalize_institution(value)
                    for value in row.get("institutions", "").split("|")
                    if normalize_institution(value)
                }
            )
        ),
        estimated_birth_year=birth_year,
        source_subjects=tuple(
            value for value in row.get("source_subjects", "").split("|") if value
        ),
    )


def review_row(
    status: str,
    left: IdentityProfile,
    right: IdentityProfile,
    basis: str,
    suggestion: str,
) -> dict[str, str]:
    def year(value: Optional[float]) -> str:
        return "" if value is None else f"{value:.2f}"

    return {
        "review_status": status,
        "left_patient_id": left.patient_id,
        "left_name": left.canonical_name,
        "right_patient_id": right.patient_id,
        "right_name": right.canonical_name,
        "left_birth_date": left.birth_date,
        "right_birth_date": right.birth_date,
        "left_sex": left.sex,
        "right_sex": right.sex,
        "left_institutions": "|".join(left.institutions),
        "right_institutions": "|".join(right.institutions),
        "left_estimated_birth_year": year(left.estimated_birth_year),
        "right_estimated_birth_year": year(right.estimated_birth_year),
        "match_basis": basis,
        "suggested_action": suggestion,
    }


def build_identity_plan(
    planner: IdentityPlanner,
    existing_rows: Sequence[dict[str, str]] = (),
) -> IdentityPlan:
    observed_profiles = planner.profiles()
    existing_by_pid = {
        row["patient_id"]: dict(row) for row in existing_rows if row.get("patient_id")
    }
    mapped_profiles = {
        row["patient_id"]: mapping_profile(row)
        for row in existing_rows
        if row.get("patient_id")
    }
    profiles = dict(mapped_profiles)
    profile_drifts: dict[str, tuple[str, ...]] = {}
    for patient_id, observed in observed_profiles.items():
        mapped = mapped_profiles.get(patient_id)
        if mapped is None:
            profiles[patient_id] = observed
            continue
        profiles[patient_id], drift = merge_mapping_and_observed_profile(
            mapped, observed
        )
        if drift:
            profile_drifts[patient_id] = drift
    all_patient_ids = sorted(profiles)
    union = UnionFind(all_patient_ids)

    existing_keys_by_pid: dict[str, str] = {}
    pids_by_existing_key: dict[str, list[str]] = defaultdict(list)
    for row in existing_rows:
        patient_id = row.get("patient_id", "")
        key = row.get("master_patient_key", "")
        if patient_id and key:
            existing_keys_by_pid[patient_id] = key
            pids_by_existing_key[key].append(patient_id)
    for patient_ids in pids_by_existing_key.values():
        for patient_id in patient_ids[1:]:
            union.union(patient_ids[0], patient_id)

    pair_relations: dict[tuple[str, str], tuple[str, str]] = {}
    merge_pairs: list[tuple[str, str, str, str]] = []
    review_rows: list[dict[str, str]] = []
    for patient_id, drift in sorted(profile_drifts.items()):
        review_rows.append(
            review_row(
                "mapping_drift",
                mapped_profiles[patient_id],
                observed_profiles[patient_id],
                "same_patient_id;changed=" + "+".join(drift),
                "同一 PatientID 的本批信息与历史映射不一致；本次沿用历史身份并请人工核对",
            )
        )
    for index, left_id in enumerate(all_patient_ids):
        for right_id in all_patient_ids[index + 1 :]:
            left = profiles[left_id]
            right = profiles[right_id]
            relation, basis = compare_profiles(left, right)
            pair_relations[(left_id, right_id)] = (relation, basis)
            if relation == "confirmed":
                merge_pairs.append((left_id, right_id, basis, relation))
            elif relation == "review_group":
                merge_pairs.append((left_id, right_id, basis, relation))
                review_rows.append(
                    review_row(
                        "grouped_needs_review", left, right, basis,
                        "已按同名、同性别、同医院归入同一患者目录；出生日期冲突，仍需人工确认自然人身份",
                    )
                )
            elif relation == "possible":
                review_rows.append(
                    review_row(
                        "needs_review", left, right, basis,
                        "证据不足，V3 本次保持两个 PatientID 分开；确认同一人后更新映射表",
                    )
                )
            elif relation == "conflict":
                review_rows.append(
                    review_row(
                        "conflict", left, right, basis,
                        "人口学信息冲突，V3 不自动合并",
                    )
                )

    def cross_group_has_conflict(left_root: str, right_root: str) -> bool:
        left_group = {pid for pid in all_patient_ids if union.find(pid) == left_root}
        right_group = {pid for pid in all_patient_ids if union.find(pid) == right_root}
        for left_id in left_group:
            for right_id in right_group:
                key = tuple(sorted((left_id, right_id)))
                if pair_relations.get(key, ("", ""))[0] == "conflict":
                    return True
        return False

    auto_merged_existing_pids: set[str] = set()
    provisionally_grouped_pids: set[str] = set()
    merge_basis_by_pid: dict[str, str] = {}
    for left_id, right_id, basis, relation in merge_pairs:
        left_root = union.find(left_id)
        right_root = union.find(right_id)
        if left_root == right_root:
            continue
        left_keys = {
            existing_keys_by_pid[pid]
            for pid in all_patient_ids
            if union.find(pid) == left_root and pid in existing_keys_by_pid
        }
        right_keys = {
            existing_keys_by_pid[pid]
            for pid in all_patient_ids
            if union.find(pid) == right_root and pid in existing_keys_by_pid
        }
        historical_groups_differ = bool(
            left_keys and right_keys and left_keys != right_keys
        )
        if relation == "confirmed" and cross_group_has_conflict(left_root, right_root):
            review_rows.append(
                review_row(
                    "mapping_conflict", profiles[left_id], profiles[right_id], basis,
                    "历史身份组之间存在人口学冲突，本次不自动合并",
                )
            )
            continue
        if relation == "review_group":
            affected = {
                patient_id
                for patient_id in all_patient_ids
                if union.find(patient_id) in {left_root, right_root}
            }
            provisionally_grouped_pids.update(affected)
            for patient_id in affected:
                merge_basis_by_pid[patient_id] = basis
        elif historical_groups_differ:
            affected = {
                patient_id
                for patient_id in all_patient_ids
                if union.find(patient_id) in {left_root, right_root}
            }
            auto_merged_existing_pids.update(affected)
            for patient_id in affected:
                merge_basis_by_pid[patient_id] = (
                    basis + ";historical_master_keys="
                    + "+".join(sorted(left_keys | right_keys))
                )
            review_rows.append(
                review_row(
                    "auto_resolved", profiles[left_id], profiles[right_id], basis,
                    "复合人口学证据充分，已将两个历史身份组自动归入较早的组号",
                )
            )
        union.union(left_id, right_id)

    components = union.groups()
    component_records: list[tuple[str, set[str], str, str]] = []
    number = next_master_number(existing_rows)
    for patient_ids in sorted(components, key=lambda values: sorted(values)):
        existing_keys = sorted(
            {
                existing_keys_by_pid[pid]
                for pid in patient_ids
                if pid in existing_keys_by_pid
            }
        )
        if existing_keys:
            master_key = existing_keys[0]
        else:
            master_key = f"G{number:06d}"
            number += 1
        names = [profiles[pid].canonical_name for pid in patient_ids]
        canonical_name = choose_canonical_name(names)
        component_records.append((master_key, patient_ids, canonical_name, ""))

    name_group_count = Counter(
        normalize_name(canonical_name)
        for _, _, canonical_name, _ in component_records
        if canonical_name
    )
    routes: dict[str, IdentityRoute] = {}
    mapping_rows: list[dict[str, str]] = []
    for master_key, patient_ids, canonical_name, _ in component_records:
        sorted_ids = tuple(sorted(patient_ids))
        multi_id = len(sorted_ids) > 1
        group_folder = ""
        if multi_id:
            group_folder = canonical_name
            if name_group_count[normalize_name(canonical_name)] > 1:
                group_folder = f"{canonical_name}__{master_key}"
            group_folder = v2.sanitize_component(group_folder)
        for patient_id in sorted_ids:
            profile = profiles[patient_id]
            existing = existing_by_pid.get(patient_id, {})
            status = (
                "grouped_needs_review"
                if patient_id in provisionally_grouped_pids
                else "auto_merged_historical_group"
                if patient_id in auto_merged_existing_pids
                else "existing_mapping"
                if existing
                else "auto_confirmed_multi_id"
                if multi_id
                else "new_identity"
            )
            basis = merge_basis_by_pid.get(patient_id, "")
            if not basis:
                basis = existing.get("match_basis", "")
            if not basis:
                basis = (
                    "confirmed_composite_demographics"
                    if multi_id
                    else "unique_patient_id;name_from_folder_or_header"
                )
            route = IdentityRoute(
                master_patient_key=master_key,
                canonical_name=canonical_name,
                patient_id=patient_id,
                all_patient_ids=sorted_ids,
                match_status=status,
                match_basis=basis,
                group_folder=group_folder,
            )
            routes[patient_id] = route
            mapping_rows.append(
                {
                    "master_patient_key": master_key,
                    "canonical_name": canonical_name,
                    "patient_id": patient_id,
                    "raw_header_patient_name": profile.raw_header_patient_name,
                    "header_patient_name": profile.header_patient_name,
                    "birth_date": profile.birth_date,
                    "sex": profile.sex,
                    "institutions": "|".join(profile.institutions),
                    "estimated_birth_year": (
                        ""
                        if profile.estimated_birth_year is None
                        else f"{profile.estimated_birth_year:.2f}"
                    ),
                    "all_patient_ids": "|".join(sorted_ids),
                    "match_status": status,
                    "match_basis": basis,
                    "source_subjects": "|".join(profile.source_subjects),
                }
            )

    dominant = planner.dominant_patient_ids()
    source_audit_rows: list[dict[str, str]] = []
    for subject, patient_stats in sorted(planner.subject_stats.items()):
        if len(patient_stats) <= 1:
            continue
        dominant_id = dominant.get(subject, "")
        subject_name = planner.subject_names.get(subject, "")
        for patient_id, stats in sorted(patient_stats.items()):
            route = routes.get(patient_id)
            action = (
                "source_name_pinyin_match"
                if subject_name
                and patient_id in planner.aggregates
                and chinese_name_matches_header(
                    subject_name,
                    modal(planner.aggregates[patient_id].header_names),
                )
                else "dominant_by_volume_only_not_used_for_naming"
                if patient_id == dominant_id
                else "routed_by_global_patient_id"
            )
            source_audit_rows.append(
                {
                    "source_subject": subject,
                    "source_subject_name": subject_name,
                    "dominant_patient_id": dominant_id,
                    "detected_patient_id": patient_id,
                    "canonical_name": route.canonical_name if route else "Unknown",
                    "study_count": str(len(stats.study_uids)),
                    "file_count": str(stats.file_count),
                    "action": action,
                    "destination_patient_path": (
                        route.destination_patient_path if route else "_identity_review"
                    ),
                }
            )

    mapping_rows.sort(key=lambda row: (row["master_patient_key"], row["patient_id"]))
    review_rows.sort(
        key=lambda row: (
            row["review_status"], row["left_name"],
            row["left_patient_id"], row["right_patient_id"],
        )
    )
    return IdentityPlan(routes, mapping_rows, review_rows, source_audit_rows)


def write_csv_atomic(
    path: Path,
    columns: Sequence[str],
    rows: Sequence[dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.stem + "_", suffix=".tmp", dir=str(path.parent)
    )
    os.close(file_descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_identity_state_atomic(
    path: Path,
    rows: Sequence[dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        try:
            import ctypes

            attributes = ctypes.windll.kernel32.GetFileAttributesW(str(path.parent))
            if attributes != -1:
                ctypes.windll.kernel32.SetFileAttributesW(
                    str(path.parent), attributes | 0x2
                )
        except (AttributeError, OSError):
            pass
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.stem + "_", suffix=".tmp", dir=str(path.parent)
    )
    os.close(file_descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(
                {
                    "rule_version": RULE_VERSION,
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                    "rows": list(rows),
                },
                stream,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def preflight_scan(
    source_root: Path,
    destination_root: Path,
    spool_path: Path,
    *,
    workers: int,
    batch_size: int,
) -> tuple[IdentityPlanner, dict[str, int]]:
    planner = IdentityPlanner()
    counters = {"total": 0, "dicom": 0, "skipped": 0}
    with (
        spool_path.open("wb") as spool,
        ThreadPoolExecutor(max_workers=max(1, workers)) as executor,
    ):
        files = v2.iter_source_files(source_root, destination_root)
        for file_batch in v2.chunked(files, max(1, batch_size)):
            scans = executor.map(
                lambda file_path: scan_one_file(file_path, source_root), file_batch
            )
            for scan in scans:
                counters["total"] += 1
                if not scan.scan.is_dicom:
                    counters["skipped"] += 1
                    continue
                counters["dicom"] += 1
                pickle.dump(scan, spool, protocol=pickle.HIGHEST_PROTOCOL)
                if scan.identity:
                    planner.add(scan.identity)
            print(
                f"[身份预扫描] 文件 {counters['total']}，DICOM {counters['dicom']}，"
                f"非DICOM {counters['skipped']}"
            )
    return planner, counters


def destination_for(
    scan: V3ScanResult,
    destination_root: Path,
    routes: dict[str, IdentityRoute],
) -> tuple[Path, bool, str]:
    meta = scan.scan.meta
    if meta is None:
        return (
            destination_root
            / "_quarantine"
            / "unreadable"
            / f"{scan.scan.relative_path_hash}.dcm",
            True,
            scan.scan.error or "DICOM 文件无法解析",
        )
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
        return (
            destination_root
            / "_quarantine"
            / "missing_required_uids"
            / f"{scan.scan.relative_path_hash}.dcm",
            True,
            "缺少必要标签: " + ", ".join(missing),
        )

    route = routes.get(meta.patient_id)
    if route:
        meta.patient_name = route.canonical_name
        patient_parts = [
            part for part in (route.group_folder, route.patient_folder) if part
        ]
        reason = ""
    else:
        header_name = clean_patient_name(
            scan.identity.header_patient_name if scan.identity else meta.patient_name
        ) or "Unknown"
        meta.patient_name = header_name
        patient_parts = [
            "_identity_review",
            v2.sanitize_component(f"{header_name}_{meta.patient_id or 'NoID'}"),
        ]
        reason = "PatientID缺失或未建立身份路由，按Header姓名放入待确认目录"

    target = destination_root
    for part in patient_parts:
        target = target / part
    target = (
        target
        / v2.sanitize_component(meta.study_uid)
        / v2.sanitize_component(meta.series_uid)
        / f"{v2.sanitize_component(meta.sop_uid)}.dcm"
    )
    return target, False, reason


def ensure_target_directory(path: Path) -> None:
    key = os.path.normcase(str(path))
    with _CREATED_DIRECTORY_LOCK:
        if key in _CREATED_DIRECTORIES:
            return
    path.mkdir(parents=True, exist_ok=True)
    with _CREATED_DIRECTORY_LOCK:
        _CREATED_DIRECTORIES.add(key)


def hash_file_fast(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(COPY_CHUNK_SIZE):
            digest.update(block)
    return digest.hexdigest()


def copy_atomic_with_hash_fast(
    source: Path,
    target: Path,
    *,
    durable: bool = False,
) -> tuple[str, Path, int, str]:
    """V3 优化复制：缓存目录、8 MiB 缓冲，并避免重跑时再次写临时副本。"""
    ensure_target_directory(target.parent)
    source_size = source.stat().st_size

    # 重跑时先只读比较。相同内容不再把源文件完整写入 .part 后又删除。
    if target.exists() and target.stat().st_size == source_size:
        source_digest = hash_file_fast(source)
        if hash_file_fast(target) == source_digest:
            return "duplicate_same", target, source_size, source_digest
    else:
        source_digest = ""

    temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.part"
    digest = hashlib.sha256()
    copied_size = 0
    try:
        with source.open("rb") as input_stream, temporary.open("xb") as output_stream:
            while block := input_stream.read(COPY_CHUNK_SIZE):
                output_stream.write(block)
                digest.update(block)
                copied_size += len(block)
            if durable:
                os.fsync(output_stream.fileno())
        if copied_size != source_size:
            raise IOError(
                f"复制大小不一致: source={source_size}, copied={copied_size}"
            )
        if durable:
            try:
                shutil.copystat(source, temporary)
            except OSError:
                pass

        sha256 = digest.hexdigest()
        if source_digest and source_digest != sha256:
            raise IOError("复制期间源文件内容发生变化")
        if target.exists():
            # 目标可能在并发等待期间出现，因此写入前仍做最终判定。
            if target.stat().st_size == copied_size and hash_file_fast(target) == sha256:
                temporary.unlink()
                return "duplicate_same", target, copied_size, sha256
            conflict = v2.unique_conflict_path(target, sha256)
            os.replace(temporary, conflict)
            return "conflict", conflict, copied_size, sha256
        os.replace(temporary, target)
        return "copied", target, copied_size, sha256
    finally:
        if temporary.exists():
            temporary.unlink()


def transfer_one(
    scan: V3ScanResult,
    destination_root: Path,
    routes: dict[str, IdentityRoute],
    *,
    durable: bool = False,
) -> v2.TransferResult:
    target, quarantined, reason = destination_for(scan, destination_root, routes)
    try:
        lock = _TRANSFER_LOCKS[hash(str(target).casefold()) % len(_TRANSFER_LOCKS)]
        with lock:
            status, actual_target, size, digest = copy_atomic_with_hash_fast(
                scan.scan.path, target, durable=durable
            )
        if quarantined and status != "conflict":
            status = "quarantined"
        return v2.TransferResult(
            scan=scan.scan,
            destination_path=str(actual_target),
            file_size=size,
            sha256=digest,
            status=status,
            message=reason,
        )
    except Exception as exc:
        return v2.TransferResult(
            scan=scan.scan,
            destination_path=str(target),
            status="error",
            message=str(exc),
        )


def iter_spool(path: Path) -> Iterator[V3ScanResult]:
    with path.open("rb") as stream:
        while True:
            try:
                yield pickle.load(stream)
            except EOFError:
                return


STUDY_WORKBOOK_COLUMN_MAP = {
    "PatientID": "patient_id",
    "PatientName": "patient_name",
    "检查日期": "exam_date",
    "基础影像类型": "modalities",
    "是否3D_DSA断层": "has_3d",
    "影像类型汇总": "display_type",
    "StudyInstanceUID": "study_uid",
    "序列数量": "series_count",
    "文件数量": "file_count",
    "判断置信度": "confidence",
    "判断依据": "evidence",
    "来源目录": "source_root",
    "转存目录": "destination_dir",
}


def _study_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _study_date(value: object) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return (datetime(1899, 12, 30) + timedelta(days=float(value))).strftime(
                "%Y-%m-%d"
            )
        except (OverflowError, ValueError):
            return ""
    text = _study_text(value)
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        return "-".join(match.groups())
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return text


def load_study_inventory(state_path: Path, excel_path: Path) -> list[dict[str, Any]]:
    if state_path.is_file():
        with state_path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
        rows = payload.get("rows", []) if isinstance(payload, dict) else payload
        state_classification_version = (
            _study_text(payload.get("classification_rule_version"))
            if isinstance(payload, dict)
            else ""
        )
        loaded_rows: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("study_uid"):
                continue
            loaded = dict(row)
            loaded.setdefault(
                "classification_rule_version",
                state_classification_version or "legacy-state",
            )
            loaded_rows.append(loaded)
        return loaded_rows
    if not excel_path.is_file():
        return []

    script_path = Path(__file__).with_name("read_dicom_study_inventory.mjs")
    completed = subprocess.run(
        [v2.find_node_executable(), str(script_path), str(excel_path)],
        cwd=str(script_path.parent),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=120,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"读取历史影像检查明细失败: {detail}")
    matrix = json.loads(completed.stdout or "[]")
    header_index = next(
        (
            index
            for index, values in enumerate(matrix)
            if "StudyInstanceUID" in {_study_text(value) for value in values}
        ),
        -1,
    )
    if header_index < 0:
        return []
    headers = [_study_text(value) for value in matrix[header_index]]
    rows: list[dict[str, Any]] = []
    for values in matrix[header_index + 1 :]:
        source = {
            STUDY_WORKBOOK_COLUMN_MAP[header]: (
                values[index] if index < len(values) else ""
            )
            for index, header in enumerate(headers)
            if header in STUDY_WORKBOOK_COLUMN_MAP
        }
        study_uid = _study_text(source.get("study_uid"))
        if not study_uid:
            continue
        source["study_uid"] = study_uid
        source["classification_rule_version"] = "legacy-excel"
        source["exam_date"] = _study_date(source.get("exam_date"))
        for key in ("patient_id", "patient_name", "modalities", "has_3d", "display_type", "evidence", "source_root", "destination_dir"):
            source[key] = _study_text(source.get(key))
        for key in ("series_count", "file_count"):
            try:
                source[key] = int(float(source.get(key) or 0))
            except (TypeError, ValueError):
                source[key] = 0
        confidence_text = _study_text(source.get("confidence")).rstrip("%")
        try:
            confidence = float(confidence_text)
            source["confidence"] = confidence / 100 if confidence > 1 else confidence
        except ValueError:
            source["confidence"] = 0.0
        rows.append(source)
    return rows


def _merge_joined(left: object, right: object, separators: str = "、|") -> str:
    pattern = "[" + re.escape(separators) + "]"
    values = {
        item.strip()
        for value in (left, right)
        for item in re.split(pattern, _study_text(value))
        if item.strip()
    }
    return "、".join(sorted(values))


def _split_study_patient_ids(value: object) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                item.strip()
                for item in re.split(r"[、|]", _study_text(value))
                if item.strip()
            }
        )
    )


def sort_study_rows(
    study_rows: Iterable[dict[str, Any]],
    mapping_rows: Sequence[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    """按自然患者身份组聚集Study，组内按检查日期升序排列。"""
    patient_to_master: dict[str, str] = {}
    master_to_names: dict[str, set[str]] = defaultdict(set)
    for mapping in mapping_rows:
        patient_id = _study_text(mapping.get("patient_id"))
        master_key = _study_text(mapping.get("master_patient_key"))
        canonical_name = _study_text(mapping.get("canonical_name"))
        if not patient_id or not master_key:
            continue
        patient_to_master[patient_id] = master_key
        if canonical_name:
            master_to_names[master_key].add(canonical_name)

    master_sort_names = {
        master_key: min(names, key=lambda value: (normalize_name(value), value))
        for master_key, names in master_to_names.items()
        if names
    }

    if not patient_to_master:
        return sorted(
            study_rows,
            key=lambda row: (
                _study_text(row.get("patient_id")),
                _study_text(row.get("exam_date")) or "9999-99-99",
                _study_text(row.get("study_uid")),
            ),
        )

    def sort_key(row: dict[str, Any]) -> tuple[str, int, str, str, str, str]:
        patient_ids = _split_study_patient_ids(row.get("patient_id"))
        master_keys = tuple(
            sorted(
                {
                    patient_to_master[patient_id]
                    for patient_id in patient_ids
                    if patient_id in patient_to_master
                }
            )
        )
        patient_name = _study_text(row.get("patient_name"))
        if master_keys:
            group_name = min(
                (
                    master_sort_names.get(master_key, patient_name)
                    for master_key in master_keys
                ),
                key=lambda value: (normalize_name(value), value),
            )
            group_key = "|".join(master_keys)
            unmapped = 0
        else:
            # 旧状态或异常记录没有身份映射时，仅按姓名+PatientID形成回退组；
            # 这只是显示排序，不改变身份判断或目录归组。
            group_name = patient_name
            group_key = "UNMAPPED:" + (
                "|".join(patient_ids)
                or _study_text(row.get("destination_dir"))
                or _study_text(row.get("study_uid"))
            )
            unmapped = 1
        return (
            normalize_name(group_name),
            unmapped,
            group_key,
            _study_text(row.get("exam_date")) or "9999-99-99",
            "|".join(patient_ids),
            _study_text(row.get("study_uid")),
        )

    return sorted(study_rows, key=sort_key)


def merge_study_inventory(
    previous_rows: Sequence[dict[str, Any]],
    current_rows: Sequence[dict[str, Any]],
    mapping_rows: Sequence[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    combined = {
        _study_text(row.get("study_uid")): dict(row)
        for row in previous_rows
        if _study_text(row.get("study_uid"))
    }
    for current in current_rows:
        study_uid = _study_text(current.get("study_uid"))
        if not study_uid:
            continue
        previous = combined.get(study_uid)
        if previous is None:
            combined[study_uid] = dict(current)
            continue
        previous_classification_version = _study_text(
            previous.get("classification_rule_version")
        ) or "legacy"
        current_classification_version = _study_text(
            current.get("classification_rule_version")
        ) or v2.RULE_VERSION
        classification_rule_changed = (
            previous_classification_version != current_classification_version
        )
        merged = dict(previous)
        for key in ("patient_id", "patient_name"):
            merged[key] = _merge_joined(previous.get(key), current.get(key))
        merged_modalities = {
            item.strip()
            for value in (previous.get("modalities"), current.get("modalities"))
            for item in re.split(r"[、|]", _study_text(value))
            if item.strip()
        }
        merged["modalities"] = v2.join_values(
            v2.sorted_modalities(merged_modalities)
        )
        if classification_rule_changed:
            # 同一Study已在新规则下重新扫描时，不能让旧规则的阳性结果继续通过OR保留。
            merged["has_3d"] = _study_text(current.get("has_3d")) or "无"
        else:
            merged["has_3d"] = (
                "有"
                if "有" in {previous.get("has_3d"), current.get("has_3d")}
                else "无"
            )
        merged["display_type"] = v2.format_display_type(
            merged_modalities,
            merged["has_3d"] == "有",
        )
        dates = [
            _study_date(value)
            for value in (previous.get("exam_date"), current.get("exam_date"))
            if _study_date(value)
        ]
        merged["exam_date"] = min(dates) if dates else ""
        merged["series_count"] = max(
            int(previous.get("series_count") or 0),
            int(current.get("series_count") or 0),
        )
        merged["file_count"] = max(
            int(previous.get("file_count") or 0),
            int(current.get("file_count") or 0),
        )
        if classification_rule_changed:
            merged["confidence"] = float(current.get("confidence") or 0)
            merged["evidence"] = _study_text(current.get("evidence"))
        else:
            merged["confidence"] = max(
                float(previous.get("confidence") or 0),
                float(current.get("confidence") or 0),
            )
            merged["evidence"] = " | ".join(
                dict.fromkeys(
                    value
                    for value in (
                        _study_text(previous.get("evidence")),
                        _study_text(current.get("evidence")),
                    )
                    if value
                )
            )
        merged["classification_rule_version"] = current_classification_version
        merged["source_root"] = " | ".join(
            sorted(
                {
                    value
                    for value in (
                        _study_text(previous.get("source_root")),
                        _study_text(current.get("source_root")),
                    )
                    if value
                }
            )
        )
        merged["destination_dir"] = (
            _study_text(current.get("destination_dir"))
            or _study_text(previous.get("destination_dir"))
        )
        merged["study_uid"] = study_uid
        combined[study_uid] = merged
    return sort_study_rows(combined.values(), mapping_rows)


def write_study_inventory_atomic(
    path: Path, rows: Sequence[dict[str, Any]]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.stem + "_", suffix=".tmp", dir=str(path.parent)
    )
    os.close(file_descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(
                {
                    "rule_version": RULE_VERSION,
                    "classification_rule_version": v2.RULE_VERSION,
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                    "rows": [
                        {
                            key: value
                            for key, value in row.items()
                            if key != "series_summaries"
                        }
                        for row in rows
                    ],
                },
                stream,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_study_workbook(
    output_path: Path,
    study_rows: Sequence[dict[str, Any]],
    mapping_rows: Sequence[dict[str, Any]],
    identity_review_rows: Sequence[dict[str, Any]],
    source_audit_rows: Sequence[dict[str, Any]],
    exception_rows: Sequence[dict[str, Any]],
    source_root: Path,
    destination_root: Path,
    counters: dict[str, int],
    manifest_csv_path: Optional[Path] = None,
    preview_dir: Optional[Path] = None,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    study_rows = sort_study_rows(study_rows, mapping_rows)
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
        "reports": [
            {
                "sheet_name": "患者身份映射",
                "table_name": "PatientIdentityMapTable",
                "columns": IDENTITY_MAPPING_COLUMNS,
                "rows": list(mapping_rows),
                "widths": [16, 18, 17, 28, 24, 14, 11, 28, 18, 28, 25, 42, 38],
            },
            {
                "sheet_name": "患者身份待确认",
                "table_name": "PatientIdentityReviewTable",
                "columns": IDENTITY_REVIEW_COLUMNS,
                "rows": list(identity_review_rows),
                "widths": [20, 17, 18, 17, 18, 16, 16, 11, 11, 28, 28, 18, 18, 48, 54],
            },
            {
                "sheet_name": "来源目录身份审计",
                "table_name": "SourceIdentityAuditTable",
                "columns": SOURCE_AUDIT_COLUMNS,
                "rows": list(source_audit_rows),
                "widths": [32, 18, 18, 18, 18, 12, 12, 28, 42],
            },
            {
                "sheet_name": "转存异常",
                "table_name": "TransferExceptionTable",
                "columns": v2.EXCEPTION_COLUMNS,
                "rows": list(exception_rows),
                "widths": [20, 17, 18, 38, 48, 46, 46],
            },
        ],
        "manifest_csv_path": str(manifest_csv_path) if manifest_csv_path else "",
    }
    script_path = Path(v2.__file__).with_name("build_dicom_study_workbook.mjs")
    if not script_path.is_file():
        raise RuntimeError(f"缺少Excel构建脚本: {script_path}")
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", encoding="utf-8", delete=False,
            dir=str(output_path.parent),
        ) as temporary:
            json.dump(payload, temporary, ensure_ascii=False)
            temporary_path = Path(temporary.name)
        command = [
            v2.find_node_executable(), str(script_path), str(temporary_path),
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
            combined_output = completed.stdout + "\n" + completed.stderr
            preview_export_succeeded = bool(
                preview_dir is not None
                and output_path.is_file()
                and (preview_dir / "影像检查明细.png").is_file()
                and (preview_dir / "字段说明.png").is_file()
                and '"output"' in combined_output
                and "Inspect result written to file:" in combined_output
            )
            if preview_export_succeeded:
                return
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f"Excel生成失败: {detail}")
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()


def process_directory(
    source_root: Path,
    destination_root: Path,
    excel_path: Path,
    exception_path: Optional[Path],
    identity_mapping_path: Path,
    identity_review_path: Optional[Path],
    source_audit_path: Optional[Path],
    *,
    workers: int = 8,
    copy_workers: int = 2,
    batch_size: int = 500,
    manifest_path: Optional[Path] = None,
    include_manifest: bool = False,
    preview_dir: Optional[Path] = None,
    durable: bool = False,
) -> dict[str, int]:
    source_root = source_root.resolve()
    destination_root = destination_root.resolve()
    if not source_root.is_dir():
        raise ValueError(f"源目录不存在: {source_root}")
    if source_root == destination_root:
        raise ValueError("源目录与目标目录不能相同")
    destination_root.mkdir(parents=True, exist_ok=True)
    identity_mapping_path = identity_mapping_path.resolve()
    existing_rows = load_identity_mapping(identity_mapping_path)
    if not existing_rows and identity_mapping_path.suffix.lower() in {".json", ".xlsx"}:
        legacy_candidates = (
            destination_root / "患者身份映射表.csv",
            destination_root.parent
            / f"{destination_root.name}_患者身份映射表.csv",
            excel_path.resolve(),
        )
        for legacy_path in legacy_candidates:
            if legacy_path.is_file() and legacy_path != identity_mapping_path:
                existing_rows = load_identity_mapping(legacy_path)
                if existing_rows:
                    print(f"[身份映射] 已迁移历史映射: {legacy_path}")
                    break

    file_descriptor, spool_name = tempfile.mkstemp(
        prefix="dicom_v3_scan_", suffix=".pkl", dir=str(destination_root.parent)
    )
    os.close(file_descriptor)
    spool_path = Path(spool_name)
    temporary_manifest_path: Optional[Path] = None
    actual_manifest_path = manifest_path.resolve() if manifest_path else None
    if include_manifest and actual_manifest_path is None:
        manifest_descriptor, manifest_name = tempfile.mkstemp(
            prefix="dicom_v3_manifest_",
            suffix=".csv",
            dir=str(destination_root.parent),
        )
        os.close(manifest_descriptor)
        temporary_manifest_path = Path(manifest_name)
        actual_manifest_path = temporary_manifest_path
    try:
        planner, scan_counters = preflight_scan(
            source_root,
            destination_root,
            spool_path,
            workers=workers,
            batch_size=batch_size,
        )
        plan = build_identity_plan(planner, existing_rows)
        if identity_mapping_path.suffix.lower() == ".json":
            write_identity_state_atomic(identity_mapping_path, plan.mapping_rows)
        elif identity_mapping_path.suffix.lower() != ".xlsx":
            write_csv_atomic(
                identity_mapping_path, IDENTITY_MAPPING_COLUMNS, plan.mapping_rows
            )
        if identity_review_path is not None:
            write_csv_atomic(
                identity_review_path.resolve(), IDENTITY_REVIEW_COLUMNS,
                plan.review_rows,
            )
        if source_audit_path is not None:
            write_csv_atomic(
                source_audit_path.resolve(), SOURCE_AUDIT_COLUMNS,
                plan.source_audit_rows,
            )
        print(
            f"[身份计划] 身份组 {len(set(route.master_patient_key for route in plan.routes.values()))}，"
            f"PatientID {len(plan.routes)}，待确认关系 {len(plan.review_rows)}，"
            f"混合来源记录 {len(plan.source_audit_rows)}"
        )

        counters = {
            **scan_counters,
            "copied": 0,
            "duplicate": 0,
            "conflict": 0,
            "quarantined": 0,
            "error": 0,
            "studies": 0,
            "identity_groups": len(
                set(route.master_patient_key for route in plan.routes.values())
            ),
            "identity_review_pairs": len(plan.review_rows),
            "mixed_source_records": len(plan.source_audit_rows),
        }
        studies: dict[str, v2.StudyAccumulator] = {}
        exceptions: list[dict[str, str]] = []
        manifest_stream = None
        manifest_writer = None
        try:
            if actual_manifest_path is not None:
                actual_manifest_path.parent.mkdir(parents=True, exist_ok=True)
                manifest_stream = actual_manifest_path.open(
                    "w", newline="", encoding="utf-8-sig"
                )
                manifest_writer = v2.write_manifest_header(manifest_stream)
            with ThreadPoolExecutor(max_workers=max(1, copy_workers)) as executor:
                transferred = 0
                for scan_batch in v2.chunked(iter_spool(spool_path), max(1, batch_size)):
                    results = executor.map(
                        lambda item: transfer_one(
                            item,
                            destination_root,
                            plan.routes,
                            durable=durable,
                        ),
                        scan_batch,
                    )
                    for result in results:
                        transferred += 1
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
                            manifest_writer.writerow(v2.manifest_row(result))
                        if result.status in {"conflict", "quarantined", "error"}:
                            exceptions.append(v2.transfer_exception(result))
                        elif result.message and "_identity_review" in result.destination_path:
                            meta = result.scan.meta or v2.DicomMeta()
                            exceptions.append(
                                v2.exception_record(
                                    "患者身份待确认",
                                    meta.patient_id,
                                    meta.patient_name,
                                    meta.study_uid,
                                    result.message,
                                    "补充或核对 PatientID 后更新患者身份映射表",
                                    result.scan.relative_path,
                                )
                            )
                        meta = result.scan.meta
                        if (
                            meta is not None
                            and meta.study_uid and meta.series_uid and meta.sop_uid
                            and result.status in {"copied", "duplicate_same"}
                        ):
                            study = studies.setdefault(
                                meta.study_uid,
                                v2.StudyAccumulator(study_uid=meta.study_uid),
                            )
                            study.add(meta, result.destination_path)
                    print(
                        f"[转存] 已处理DICOM {transferred}/{counters['dicom']}，"
                        f"复制 {counters['copied']}，重复 {counters['duplicate']}，"
                        f"冲突 {counters['conflict']}，隔离 {counters['quarantined']}，"
                        f"错误 {counters['error']}"
                    )
        finally:
            if manifest_stream is not None:
                manifest_stream.close()

        current_study_rows: list[dict[str, Any]] = []
        for _, study in sorted(studies.items(), key=lambda item: item[0]):
            row, warnings = v2.summarize_study(study, source_root)
            current_study_rows.append(row)
            exceptions.extend(warnings)
        study_state_path = destination_root / ".dicom_v3_state" / "study_inventory.json"
        previous_study_rows = load_study_inventory(
            study_state_path, excel_path.resolve()
        )
        study_rows = merge_study_inventory(
            previous_study_rows,
            current_study_rows,
            plan.mapping_rows,
        )
        write_study_inventory_atomic(study_state_path, study_rows)
        counters["studies_current_run"] = len(current_study_rows)
        counters["studies"] = len(study_rows)
        if exception_path is not None:
            v2.write_exception_csv(exception_path.resolve(), exceptions)
        write_study_workbook(
            excel_path.resolve(), study_rows, plan.mapping_rows, plan.review_rows,
            plan.source_audit_rows, exceptions, source_root, destination_root,
            counters, manifest_csv_path=actual_manifest_path,
            preview_dir=preview_dir,
        )
        return counters
    finally:
        if spool_path.exists():
            spool_path.unlink()
        if temporary_manifest_path and temporary_manifest_path.exists():
            temporary_manifest_path.unlink()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="DICOM原始影像转存、混入患者分流与多PatientID身份归组 V3"
    )
    parser.add_argument("src_dir", help="DICOM源目录")
    parser.add_argument("dst_dir", help="转存目标目录")
    parser.add_argument("--excel", help="V3综合Excel路径")
    parser.add_argument("--exceptions", help="兼容输出：另存转存异常CSV")
    parser.add_argument(
        "--identity-map",
        help="兼容输入/输出：外部敏感患者身份映射CSV或综合Excel",
    )
    parser.add_argument("--identity-review", help="兼容输出：另存患者身份待确认CSV")
    parser.add_argument("--source-audit", help="兼容输出：另存来源目录身份审计CSV")
    parser.add_argument(
        "--manifest", nargs="?", const="AUTO",
        help="可选：在综合Excel加入文件级转存清单；指定路径时同时另存CSV",
    )
    parser.add_argument(
        "--keep-csv", action="store_true",
        help="兼容模式：同时保留原来的各项CSV输出",
    )
    parser.add_argument(
        "--workers", type=int, default=1,
        help="Header解析线程数，默认1；本地单盘通常比8线程更快",
    )
    parser.add_argument("--copy-workers", type=int, default=2, help="复制线程数，默认2")
    parser.add_argument("--batch-size", type=int, default=500, help="处理批次大小，默认500")
    parser.add_argument(
        "--durable", action="store_true",
        help="每个文件复制后fsync并保留时间戳；更安全但明显更慢",
    )
    parser.add_argument("--preview-dir", help=argparse.SUPPRESS)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    source_root = Path(args.src_dir)
    destination_root = Path(args.dst_dir)
    excel_path = Path(args.excel) if args.excel else destination_root / "影像检查明细_V3.xlsx"
    exception_path = (
        Path(args.exceptions)
        if args.exceptions
        else destination_root / "待确认记录_V3.csv"
        if args.keep_csv
        else None
    )
    identity_mapping_path = (
        Path(args.identity_map)
        if args.identity_map
        else destination_root / "患者身份映射表.csv"
        if args.keep_csv
        else destination_root / ".dicom_v3_state" / "identity_mapping.json"
    )
    identity_review_path = (
        Path(args.identity_review)
        if args.identity_review
        else destination_root / "患者身份待确认.csv"
        if args.keep_csv
        else None
    )
    source_audit_path = (
        Path(args.source_audit)
        if args.source_audit
        else destination_root / "源目录身份审计.csv"
        if args.keep_csv
        else None
    )
    include_manifest = args.manifest is not None
    if args.manifest == "AUTO" and args.keep_csv:
        manifest_path = destination_root / "转存清单_V3.csv"
    elif args.manifest and args.manifest != "AUTO":
        manifest_path = Path(args.manifest)
    else:
        manifest_path = None
    preview_dir = Path(args.preview_dir).resolve() if args.preview_dir else None
    try:
        counters = process_directory(
            source_root,
            destination_root,
            excel_path,
            exception_path,
            identity_mapping_path,
            identity_review_path,
            source_audit_path,
            workers=args.workers,
            copy_workers=args.copy_workers,
            batch_size=args.batch_size,
            manifest_path=manifest_path,
            include_manifest=include_manifest,
            preview_dir=preview_dir,
            durable=args.durable,
        )
    except Exception as exc:
        print(f"V3处理失败: {exc}", file=sys.stderr)
        return 1

    print("=" * 60)
    print("V3处理完成")
    print(f"扫描文件: {counters['total']}")
    print(f"DICOM文件: {counters['dicom']}")
    print(f"身份组: {counters['identity_groups']}")
    print(f"待确认身份关系: {counters['identity_review_pairs']}")
    print(f"来源混合身份记录: {counters['mixed_source_records']}")
    print(f"成功复制: {counters['copied']}")
    print(f"内容重复: {counters['duplicate']}")
    print(f"UID冲突: {counters['conflict']}")
    print(f"隔离文件: {counters['quarantined']}")
    print(f"错误: {counters['error']}")
    print(f"Study汇总: {counters['studies']}")
    print(f"综合Excel: {excel_path.resolve()}")
    if args.identity_map or args.keep_csv:
        print(f"外部敏感身份映射: {identity_mapping_path.resolve()}")
    if args.keep_csv:
        print("兼容CSV: 已保留")
    return 0 if counters["error"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
