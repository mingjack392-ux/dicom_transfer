#!/usr/bin/env python3
"""Anonymize an already-transferred DICOM directory tree.

This is intentionally independent from ``dicom_anonymize_classify.py``.
The source tree is never modified.  Dry-run is the default; pass ``--execute``
to write a separate output tree.

Rule precedence:
1. Explicit project requirements (PatientID/InstitutionName/Modality retained)
2. The 212-tag medical-imaging deletion list supplied on 2026-08-07
3. Rules learned from the paired reference dataset
4. Everything else retained unchanged
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

import pydicom
from pydicom.dataset import Dataset
from pydicom.multival import MultiValue
from pydicom.tag import BaseTag, Tag


DELETE_TAG_TEXT = """
(0008,0090) (0008,0092) (0008,0094) (0008,0096) (0008,009C) (0008,009D)
(0008,1048) (0008,1049) (0008,1050) (0008,1052) (0008,1060) (0008,1062)
(0010,0050) (0010,0101) (0010,0102) (0010,1000) (0010,1001) (0010,1002)
(0010,1005) (0010,1040) (0010,1050) (0010,1060) (0010,1080) (0010,1081)
(0010,1090) (0010,1100) (0010,2154) (0010,2155) (0010,2297) (0010,2299)
(0012,0010) (0012,0020) (0012,0021) (0012,0030) (0012,0031) (0012,0040)
(0012,0042) (0012,0060) (0012,0071) (0012,0072) (0012,0081) (0012,0082)
(0016,002B) (0016,004B) (0016,004D) (0016,004E) (0016,004F) (0016,0050)
(0016,0051) (0016,0070) (0018,1004) (0018,1005) (0018,1007) (0018,1008)
(0018,1009) (0018,100A) (0018,5011) (0018,9185) (0018,9367) (0018,9371)
(0018,9373) (0018,937B) (0018,937F) (0018,9937) (0018,A003) (0020,3401)
(0020,3406) (0032,0012) (0032,1020) (0032,1021) (0032,1032) (0032,1067)
(0032,1070) (0034,0001) (0034,0002) (0034,0005) (0038,0004) (0038,0011)
(0038,0014) (0038,001E) (0038,0040) (0038,0050) (0038,0060) (0038,0061)
(0038,0062) (0038,0064) (0038,0300) (0038,0400) (0038,0500) (0040,0001)
(0040,0006) (0040,0007) (0040,0009) (0040,000B) (0040,0010) (0040,0011)
(0040,0012) (0040,0241) (0040,0242) (0040,0243) (0040,0253) (0040,0254)
(0040,0275) (0040,050A) (0040,0512) (0040,0513) (0040,051A) (0040,0551)
(0040,0562) (0040,0600) (0040,06FA) (0040,1001) (0040,1002) (0040,1004)
(0040,1005) (0040,100A) (0040,1010) (0040,1011) (0040,1101) (0040,1102)
(0040,1103) (0040,1104) (0040,2001) (0040,2008) (0040,2009) (0040,2010)
(0040,2011) (0040,3001) (0040,4025) (0040,4027) (0040,4028) (0040,4030)
(0040,4034) (0040,4036) (0040,4037) (0040,A075) (0040,A078) (0040,A07A)
(0040,A07C) (0040,A088) (0040,A123) (0040,A307) (0040,A352) (0040,A354)
(0040,A358) (0050,001B) (0050,0020) (0050,0021) (0070,0086) (0088,0200)
(0088,0904) (0088,0906) (0088,0910) (0088,0912) (0400,0402) (0400,0403)
(0400,0404) (0400,0550) (0400,0551) (0400,0552) (0400,0561) (0400,0600)
(3006,0004) (3006,0006) (3006,0026) (3006,0028) (3006,0038) (3006,0085)
(3006,0088) (300A,0003) (300A,0004) (300A,000E) (300A,0016) (300A,0072)
(300A,00C3) (300A,00DD) (300A,0196) (300A,01A6) (300A,01B2) (300A,0216)
(300A,02EB) (300A,0611) (300A,0615) (300A,0619) (300A,0676) (300A,078E)
(300A,0792) (300A,0794) (300A,079A) (300C,0113) (3010,001B) (3010,0036)
(3010,0037) (3010,0043) (3010,0061) (4008,0040) (4008,0042) (4008,0102)
(4008,010A) (4008,010B) (4008,010C) (4008,0111) (4008,0114) (4008,0115)
(4008,0118) (4008,0119) (4008,011A) (4008,0200) (4008,0202) (4008,0300)
(FFFA,FFFA) (FFFC,FFFC)
"""


def _parse_tag_set(text: str) -> frozenset[BaseTag]:
    return frozenset(
        Tag(int(group, 16), int(element, 16))
        for group, element in re.findall(r"\(([0-9A-Fa-f]{4}),([0-9A-Fa-f]{4})\)", text)
    )


WORKBOOK_DELETE_TAGS = _parse_tag_set(DELETE_TAG_TEXT)
if len(WORKBOOK_DELETE_TAGS) != 212:
    raise RuntimeError(f"Expected 212 workbook delete tags, got {len(WORKBOOK_DELETE_TAGS)}")

# User-requested additions, outside the uploaded 212-tag list.
EXTRA_DELETE_TAGS = frozenset(
    {
        Tag(0x0018, 0x2042),  # TargetUID
        Tag(0x0062, 0x0021),  # TrackingUID
        Tag(0x2030, 0x0020),  # TextString
        Tag(0x7005, 0x1063),
        Tag(0x7005, 0x1067),
        Tag(0x7005, 0x1068),
        Tag(0x7005, 0x1069),
        Tag(0x7005, 0x106C),
        Tag(0x7005, 0x106D),
    }
)
ALL_DELETE_TAGS = WORKBOOK_DELETE_TAGS | EXTRA_DELETE_TAGS

PATIENT_NAME_TAG = Tag(0x0010, 0x0010)
PROTECTED_RETAIN_TAGS = frozenset(
    {
        Tag(0x0010, 0x0020),  # PatientID
        Tag(0x0008, 0x0080),  # InstitutionName
        Tag(0x0008, 0x0060),  # Modality
    }
)
CLEAR_TAGS = frozenset(
    {
        Tag(0x0008, 0x0081),  # InstitutionAddress
        Tag(0x0008, 0x1040),  # InstitutionalDepartmentName
    }
)
STRIP_FRACTIONAL_TM_TAGS = frozenset(
    {
        Tag(0x0008, 0x0030),  # StudyTime
        Tag(0x0008, 0x0031),  # SeriesTime
        Tag(0x0008, 0x0032),  # AcquisitionTime
        Tag(0x0008, 0x0033),  # ContentTime
    }
)
CODE_PATTERN = re.compile(r"^E\d{6}$", re.IGNORECASE)
UID_FOLDER_PATTERN = re.compile(r"^\d+(?:\.\d+)+$")
LEGACY_MAPPING_FIELDS = (
    "source_patient_folder",
    "PatientID",
    "original_patient_name",
    "anonymous_code",
)
MAPPING_FIELDS = LEGACY_MAPPING_FIELDS + (
    "PatientBirthDate",
    "PatientSex",
    "PatientAge",
    "StudyDate",
    "all_patient_ids",
    "match_status",
    "match_basis",
)
AUDIT_FIELDS = (
    "status",
    "source_file",
    "output_file",
    "anonymous_code",
    "PatientID",
    "PatientBirthDate",
    "PatientSex",
    "PatientAge",
    "identity_match_status",
    "identity_match_basis",
    "deleted_elements",
    "cleared_elements",
    "renamed_patient_names",
    "trimmed_times",
    "message",
)


@dataclass
class ChangeCounts:
    deleted_elements: int = 0
    cleared_elements: int = 0
    renamed_patient_names: int = 0
    trimmed_times: int = 0


@dataclass
class RunSummary:
    mode: str
    patient_folders: int = 0
    candidate_files: int = 0
    dicom_files: int = 0
    written: int = 0
    planned: int = 0
    skipped_non_dicom: int = 0
    skipped_existing: int = 0
    conflicts: int = 0
    errors: int = 0
    identity_profiles: int = 0
    demographic_merges: int = 0
    identities_needing_review: int = 0
    changes: ChangeCounts = field(default_factory=ChangeCounts)

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "patient_folders": self.patient_folders,
            "candidate_files": self.candidate_files,
            "dicom_files": self.dicom_files,
            "written": self.written,
            "planned": self.planned,
            "skipped_non_dicom": self.skipped_non_dicom,
            "skipped_existing": self.skipped_existing,
            "conflicts": self.conflicts,
            "errors": self.errors,
            "identity_profiles": self.identity_profiles,
            "demographic_merges": self.demographic_merges,
            "identities_needing_review": self.identities_needing_review,
            "changes": vars(self.changes),
        }


@dataclass(frozen=True)
class PatientProfile:
    patient_id: str = ""
    patient_name: str = ""
    birth_date: str = ""
    sex: str = ""
    age: str = ""
    study_date: str = ""


@dataclass(frozen=True)
class IdentityDecision:
    code: str
    status: str
    basis: str


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _normalize_name(value: object) -> str:
    return re.sub(r"[\s^]+", "", _clean_text(value)).casefold()


def _normalize_id(value: object) -> str:
    return re.sub(r"\s+", "", _clean_text(value)).casefold()


def _normalize_date(value: object) -> str:
    digits = re.sub(r"\D", "", _clean_text(value))
    return digits[:8] if len(digits) >= 8 else ""


def _normalize_sex(value: object) -> str:
    text = _clean_text(value).upper()
    return text if text in {"M", "F"} else ""


def _age_in_years(value: object) -> float | None:
    match = re.fullmatch(r"\s*(\d{1,3})([DWMY])\s*", _clean_text(value).upper())
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2)
    return {"D": amount / 365.25, "W": amount / 52.18, "M": amount / 12, "Y": float(amount)}[unit]


def _estimated_birth_year(age: object, study_date: object) -> float | None:
    years = _age_in_years(age)
    normalized_study_date = _normalize_date(study_date)
    if years is None or not normalized_study_date:
        return None
    return int(normalized_study_date[:4]) - years


def profile_from_header(header: Dataset) -> PatientProfile:
    return PatientProfile(
        patient_id=_clean_text(header.get("PatientID", "")),
        patient_name=_clean_text(header.get("PatientName", "")),
        birth_date=_normalize_date(header.get("PatientBirthDate", "")),
        sex=_normalize_sex(header.get("PatientSex", "")),
        age=_clean_text(header.get("PatientAge", "")).upper(),
        study_date=_normalize_date(header.get("StudyDate", "")),
    )


def _modal_value(values: Iterable[str]) -> str:
    nonempty = [value for value in values if value]
    if not nonempty:
        return ""
    counts = Counter(nonempty)
    return sorted(counts, key=lambda value: (-counts[value], value))[0]


def combine_profiles(profiles: Iterable[PatientProfile]) -> PatientProfile:
    rows = list(profiles)
    return PatientProfile(
        patient_id=_modal_value(row.patient_id for row in rows),
        patient_name=_modal_value(row.patient_name for row in rows),
        birth_date=_modal_value(row.birth_date for row in rows),
        sex=_modal_value(row.sex for row in rows),
        age=_modal_value(row.age for row in rows),
        study_date=_modal_value(row.study_date for row in rows),
    )


def _strip_fraction(value: object) -> object:
    if isinstance(value, (list, tuple, MultiValue)):
        return [_strip_fraction(item) for item in value]
    text = str(value)
    return text.split(".", 1)[0]


def anonymize_dataset(dataset: Dataset, anonymous_code: str) -> ChangeCounts:
    """Apply the project rules recursively in place and return change counts."""
    counts = ChangeCounts()

    def walk(current: Dataset) -> None:
        for element in list(current):
            tag = element.tag
            if tag in PROTECTED_RETAIN_TAGS:
                continue
            if tag in ALL_DELETE_TAGS:
                del current[tag]
                counts.deleted_elements += 1
                continue
            if tag == PATIENT_NAME_TAG:
                if str(element.value) != anonymous_code:
                    element.value = anonymous_code
                    counts.renamed_patient_names += 1
                continue
            if tag in CLEAR_TAGS:
                if element.value not in (None, ""):
                    element.value = ""
                    counts.cleared_elements += 1
                continue
            if tag in STRIP_FRACTIONAL_TM_TAGS:
                new_value = _strip_fraction(element.value)
                if str(new_value) != str(element.value):
                    element.value = new_value
                    counts.trimmed_times += 1
                continue
            if element.VR == "SQ":
                for item in element.value:
                    walk(item)

    walk(dataset)
    return counts


def _read_header(path: Path) -> Dataset | None:
    try:
        return pydicom.dcmread(path, stop_before_pixels=True, force=False)
    except Exception:
        return None


def _iter_files(root: Path) -> Iterator[Path]:
    for path in root.rglob("*"):
        if path.is_file():
            yield path


def _contains_dicom(root: Path) -> bool:
    for path in _iter_files(root):
        if _read_header(path) is not None:
            return True
    return False


def discover_patient_roots(input_root: Path, single_patient: bool) -> list[Path]:
    if single_patient:
        return [input_root]
    children = sorted((p for p in input_root.iterdir() if p.is_dir()), key=lambda p: p.name)
    if not children:
        return [input_root]
    dicom_children = [child for child in children if _contains_dicom(child)]
    if not dicom_children:
        return [input_root]
    # A transferred single-patient root normally starts directly with Study UID folders.
    if all(UID_FOLDER_PATTERN.fullmatch(child.name) for child in dicom_children):
        return [input_root]
    return dicom_children


def load_mapping(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = set(LEGACY_MAPPING_FIELDS) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Mapping file is missing columns: {sorted(missing)}")
        return [{key: str(row.get(key, "")) for key in MAPPING_FIELDS} for row in reader]


def _next_code(used_codes: set[str]) -> str:
    highest = max(
        (int(code[1:]) for code in used_codes if CODE_PATTERN.fullmatch(code)),
        default=0,
    )
    while True:
        highest += 1
        candidate = f"E{highest:06d}"
        if candidate not in used_codes:
            return candidate


def _profile_from_mapping(row: dict[str, str]) -> PatientProfile:
    return PatientProfile(
        patient_id=_clean_text(row.get("PatientID", "")),
        patient_name=_clean_text(row.get("original_patient_name", "")),
        birth_date=_normalize_date(row.get("PatientBirthDate", "")),
        sex=_normalize_sex(row.get("PatientSex", "")),
        age=_clean_text(row.get("PatientAge", "")).upper(),
        study_date=_normalize_date(row.get("StudyDate", "")),
    )


def compare_identity(left: PatientProfile, right: PatientProfile) -> tuple[str, str]:
    """Return confirmed, possible, conflict, or unrelated with an audit basis."""
    same_name = bool(
        _normalize_name(left.patient_name)
        and _normalize_name(left.patient_name) == _normalize_name(right.patient_name)
    )
    same_id = bool(
        _normalize_id(left.patient_id)
        and _normalize_id(left.patient_id) == _normalize_id(right.patient_id)
    )
    if same_name and same_id:
        return "confirmed", "same_name_and_patient_id"
    if not (same_name or same_id):
        return "unrelated", "name_and_patient_id_both_different"

    conflicts: list[str] = []
    evidence: list[str] = []
    if left.birth_date and right.birth_date:
        if left.birth_date == right.birth_date:
            evidence.append("birth_date")
        else:
            conflicts.append("birth_date")
    if left.sex and right.sex:
        if left.sex == right.sex:
            evidence.append("sex")
        else:
            conflicts.append("sex")

    left_birth_year = _estimated_birth_year(left.age, left.study_date)
    right_birth_year = _estimated_birth_year(right.age, right.study_date)
    if left_birth_year is not None and right_birth_year is not None:
        if abs(left_birth_year - right_birth_year) <= 1.25:
            evidence.append("age_consistent_with_study_date")
        else:
            conflicts.append("age")
    elif left.age and right.age and left.study_date == right.study_date:
        if left.age == right.age:
            evidence.append("age")
        else:
            conflicts.append("age")

    relation = "same_name" if same_name else "same_patient_id"
    if conflicts:
        return "conflict", f"{relation};conflict={'+'.join(conflicts)}"
    if len(set(evidence)) >= 2:
        return "confirmed", f"{relation};demographics={'+'.join(evidence)}"
    return "possible", f"{relation};insufficient_demographics={'+'.join(evidence) or 'none'}"


def _mapping_row(
    source_folder: str,
    profile: PatientProfile,
    code: str,
    status: str,
    basis: str,
) -> dict[str, str]:
    return {
        "source_patient_folder": source_folder,
        "PatientID": profile.patient_id,
        "original_patient_name": profile.patient_name,
        "anonymous_code": code,
        "PatientBirthDate": profile.birth_date,
        "PatientSex": profile.sex,
        "PatientAge": profile.age,
        "StudyDate": profile.study_date,
        "all_patient_ids": profile.patient_id,
        "match_status": status,
        "match_basis": basis,
    }


def _refresh_all_patient_ids(mapping_rows: list[dict[str, str]]) -> None:
    ids_by_code: dict[str, set[str]] = {}
    for row in mapping_rows:
        patient_id = _clean_text(row.get("PatientID", ""))
        if patient_id:
            ids_by_code.setdefault(row["anonymous_code"], set()).add(patient_id)
    for row in mapping_rows:
        row["all_patient_ids"] = "|".join(sorted(ids_by_code.get(row["anonymous_code"], set())))


def assign_code(
    mapping_rows: list[dict[str, str]],
    source_folder: str,
    profile: PatientProfile,
) -> IdentityDecision:
    for row in mapping_rows:
        if (
            row["source_patient_folder"] == source_folder
            and _normalize_name(row["original_patient_name"]) == _normalize_name(profile.patient_name)
            and _normalize_id(row["PatientID"]) == _normalize_id(profile.patient_id)
        ):
            return IdentityDecision(row["anonymous_code"], "existing_mapping", "same_folder_name_and_patient_id")

    confirmed: dict[str, list[str]] = {}
    possible: dict[str, list[str]] = {}
    conflicts: list[str] = []
    for row in mapping_rows:
        code = row["anonymous_code"]
        relation, basis = compare_identity(profile, _profile_from_mapping(row))
        if relation == "confirmed":
            confirmed.setdefault(code, []).append(basis)
        elif relation == "possible":
            possible.setdefault(code, []).append(basis)
        elif relation == "conflict":
            conflicts.append(f"{code}:{basis}")

    if len(confirmed) == 1:
        code = next(iter(confirmed))
        basis = "|".join(sorted(set(confirmed[code])))
        status = "demographic_confirmed" if "demographics=" in basis else "exact_confirmed"
        mapping_rows.append(_mapping_row(source_folder, profile, code, status, basis))
        _refresh_all_patient_ids(mapping_rows)
        return IdentityDecision(code, status, basis)

    used = {row["anonymous_code"] for row in mapping_rows}
    proposed = source_folder.upper() if CODE_PATTERN.fullmatch(source_folder) else ""
    code = proposed if proposed and proposed not in used else _next_code(used)
    if len(confirmed) > 1:
        status = "needs_review"
        basis = "multiple_confirmed_codes=" + "|".join(sorted(confirmed))
    elif possible:
        status = "needs_review"
        basis = "possible_codes=" + "|".join(sorted(possible))
    elif conflicts:
        status = "new_identity"
        basis = "demographic_conflict=" + "|".join(sorted(conflicts))
    else:
        status = "new_identity"
        basis = "no_related_name_or_patient_id"
    mapping_rows.append(_mapping_row(source_folder, profile, code, status, basis))
    _refresh_all_patient_ids(mapping_rows)
    return IdentityDecision(code, status, basis)


def _atomic_write_csv(path: Path, fieldnames: Iterable[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _save_dataset_atomic(dataset: Dataset, output_path: Path, overwrite: bool) -> str:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent
    )
    os.close(descriptor)
    temp_path = Path(temp_name)
    try:
        dataset.save_as(temp_path, enforce_file_format=True)
        if output_path.exists():
            if _sha256(temp_path) == _sha256(output_path):
                return "duplicate"
            if not overwrite:
                return "conflict"
        os.replace(temp_path, output_path)
        return "written"
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _validate_paths(input_root: Path, output_root: Path) -> None:
    source = input_root.resolve()
    target = output_root.resolve()
    if source == target:
        raise ValueError("Output directory must be different from input directory")
    if source in target.parents:
        raise ValueError("Output directory must not be inside the input directory")


def run(args: argparse.Namespace) -> RunSummary:
    input_root = args.input.resolve()
    output_root = args.output.resolve()
    _validate_paths(input_root, output_root)
    if not input_root.is_dir():
        raise FileNotFoundError(f"Input directory does not exist: {input_root}")

    summary = RunSummary(mode="execute" if args.execute else "dry-run")
    mapping_rows = load_mapping(args.mapping)
    audit_rows: list[dict[str, object]] = []
    patient_roots = discover_patient_roots(input_root, args.single_patient)
    summary.patient_folders = len(patient_roots)

    for patient_root in patient_roots:
        files = list(_iter_files(patient_root))
        summary.candidate_files += len(files)
        headers: dict[Path, Dataset] = {}
        raw_profiles: dict[Path, PatientProfile] = {}
        for source_file in files:
            header = _read_header(source_file)
            if header is None:
                summary.skipped_non_dicom += 1
                continue
            headers[source_file] = header
            raw_profiles[source_file] = profile_from_header(header)
        if not headers:
            continue

        source_folder = patient_root.name
        default_name = _modal_value(profile.patient_name for profile in raw_profiles.values())
        default_id = _modal_value(profile.patient_id for profile in raw_profiles.values())
        grouped_files: dict[tuple[str, str], list[Path]] = {}
        for source_file, profile in raw_profiles.items():
            name = profile.patient_name or default_name
            patient_id = profile.patient_id or default_id
            key = (_normalize_name(name), _normalize_id(patient_id))
            grouped_files.setdefault(key, []).append(source_file)

        for key in sorted(grouped_files):
            group_files = grouped_files[key]
            group_profile = combine_profiles(raw_profiles[path] for path in group_files)
            group_profile = PatientProfile(
                patient_id=group_profile.patient_id or default_id,
                patient_name=group_profile.patient_name or default_name,
                birth_date=group_profile.birth_date,
                sex=group_profile.sex,
                age=group_profile.age,
                study_date=group_profile.study_date,
            )
            decision = assign_code(mapping_rows, source_folder, group_profile)
            summary.identity_profiles += 1
            if decision.status == "demographic_confirmed":
                summary.demographic_merges += 1
            if decision.status == "needs_review":
                summary.identities_needing_review += 1

            for source_file in group_files:
                header = headers[source_file]
                file_profile = raw_profiles[source_file]
                summary.dicom_files += 1
                relative = source_file.relative_to(patient_root)
                output_file = output_root / decision.code / relative
                audit: dict[str, object] = {
                    "status": "",
                    "source_file": str(source_file),
                    "output_file": str(output_file),
                    "anonymous_code": decision.code,
                    "PatientID": file_profile.patient_id,
                    "PatientBirthDate": file_profile.birth_date,
                    "PatientSex": file_profile.sex,
                    "PatientAge": file_profile.age,
                    "identity_match_status": decision.status,
                    "identity_match_basis": decision.basis,
                    "deleted_elements": 0,
                    "cleared_elements": 0,
                    "renamed_patient_names": 0,
                    "trimmed_times": 0,
                    "message": "",
                }
                try:
                    dataset = pydicom.dcmread(source_file, force=False)
                    counts = anonymize_dataset(dataset, decision.code)
                    for name, value in vars(counts).items():
                        setattr(summary.changes, name, getattr(summary.changes, name) + value)
                        audit[name] = value
                    if not args.execute:
                        audit["status"] = "planned"
                        summary.planned += 1
                    else:
                        save_status = _save_dataset_atomic(dataset, output_file, args.overwrite)
                        audit["status"] = save_status
                        if save_status == "written":
                            summary.written += 1
                        elif save_status == "duplicate":
                            summary.skipped_existing += 1
                        else:
                            summary.conflicts += 1
                            audit["message"] = "Target exists with different content; use --overwrite to replace"
                except Exception as exc:
                    summary.errors += 1
                    audit["status"] = "error"
                    audit["message"] = f"{type(exc).__name__}: {exc}"
                audit_rows.append(audit)

    if args.execute:
        _refresh_all_patient_ids(mapping_rows)
        _atomic_write_csv(args.mapping, MAPPING_FIELDS, mapping_rows)
        _atomic_write_csv(args.audit, AUDIT_FIELDS, audit_rows)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="对已转存的 DICOM 目录执行可回溯匿名化（默认仅预演）"
    )
    parser.add_argument("input", type=Path, help="已转存 DICOM 输入目录")
    parser.add_argument("output", type=Path, help="独立的匿名化输出目录")
    parser.add_argument(
        "--mapping",
        type=Path,
        help="敏感映射 CSV；默认位于输出目录同级，不放入匿名化目录",
    )
    parser.add_argument(
        "--audit",
        type=Path,
        help="敏感审计 CSV；默认位于输出目录同级，不放入匿名化目录",
    )
    parser.add_argument("--single-patient", action="store_true", help="将输入根目录视为一个患者目录")
    parser.add_argument("--execute", action="store_true", help="实际写入；不指定时仅预演")
    parser.add_argument("--overwrite", action="store_true", help="覆盖内容不同的既有目标文件")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.input = args.input.resolve()
    args.output = args.output.resolve()
    args.mapping = (args.mapping or args.output.parent / f"{args.output.name}_mapping_sensitive.csv").resolve()
    args.audit = (args.audit or args.output.parent / f"{args.output.name}_audit_sensitive.csv").resolve()
    try:
        summary = run(args)
    except Exception as exc:
        parser.error(str(exc))
    print(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2))
    if args.execute:
        print(f"映射表（敏感）: {args.mapping}")
        print(f"审计日志（敏感）: {args.audit}")
    else:
        print("当前为预演模式，未写入 DICOM、映射表或审计日志；确认后加 --execute。")
    return 1 if summary.errors or summary.conflicts else 0


if __name__ == "__main__":
    sys.exit(main())
