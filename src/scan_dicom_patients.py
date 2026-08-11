#!/usr/bin/env python3
"""Recursively scan DICOM headers and print the patients found."""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

try:
    import pydicom
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "\u7f3a\u5c11 pydicom\uff0c\u8bf7\u5148\u6267\u884c\uff1apip install pydicom"
    ) from exc


SCAN_TAGS = [
    "PatientID",
    "PatientName",
    "SOPClassUID",
    "SOPInstanceUID",
    "StudyInstanceUID",
    "SeriesInstanceUID",
]

PATIENT_ID_UUID_SUFFIX_PATTERN = re.compile(
    r"^(?P<patient_id>.+?)!"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FileScanResult:
    status: str
    patient_id: str = ""
    patient_name: str = ""
    error: str = ""


@dataclass
class PatientSummary:
    patient_id: str
    fallback_by_name: bool
    names: Counter[str] = field(default_factory=Counter)
    dicom_files: int = 0

    def add(self, patient_name: str) -> None:
        self.dicom_files += 1
        if patient_name:
            self.names[patient_name] += 1

    @property
    def preferred_name(self) -> str:
        if not self.names:
            return "<\u59d3\u540d\u7f3a\u5931>"
        return self.names.most_common(1)[0][0]

    @property
    def aliases(self) -> list[str]:
        preferred = self.preferred_name
        return sorted(name for name in self.names if name != preferred)


@dataclass
class ScanReport:
    total_files: int = 0
    dicom_files: int = 0
    non_dicom_files: int = 0
    read_errors: int = 0
    dicom_without_identity: int = 0
    patients: dict[str, PatientSummary] = field(default_factory=dict)


def normalized_text(value: object) -> str:
    if value is None:
        return ""
    return unicodedata.normalize("NFKC", str(value)).strip()


def normalize_patient_name(value: object) -> str:
    name = normalized_text(value).replace("^", " ")
    return re.sub(r"\s+", " ", name).strip()


def normalize_patient_id(value: object) -> str:
    patient_id = normalized_text(value)
    match = PATIENT_ID_UUID_SUFFIX_PATTERN.fullmatch(patient_id)
    if match:
        return match.group("patient_id").strip()
    return patient_id


def scan_one_file(path: Path) -> FileScanResult:
    """Read selected header tags only; Pixel Data is never read."""
    try:
        with path.open("rb") as stream:
            header = stream.read(132)
            has_prefix = len(header) >= 132 and header[128:132] == b"DICM"
            stream.seek(0)
            dataset = pydicom.dcmread(
                stream,
                force=True,
                stop_before_pixels=True,
                specific_tags=SCAN_TAGS,
            )
    except Exception as exc:
        return FileScanResult("error", error=str(exc))

    uid_values = (
        getattr(dataset, "SOPClassUID", None),
        getattr(dataset, "SOPInstanceUID", None),
        getattr(dataset, "StudyInstanceUID", None),
        getattr(dataset, "SeriesInstanceUID", None),
    )
    if not has_prefix and not any(normalized_text(value) for value in uid_values):
        return FileScanResult("not_dicom")

    return FileScanResult(
        "dicom",
        patient_id=normalize_patient_id(getattr(dataset, "PatientID", "")),
        patient_name=normalize_patient_name(getattr(dataset, "PatientName", "")),
    )


def iter_input_files(source: Path) -> Iterator[Path]:
    if source.is_file():
        yield source
        return

    def report_walk_error(error: OSError) -> None:
        print(
            f"\u65e0\u6cd5\u8bbf\u95ee\u76ee\u5f55\uff1a{error}",
            file=sys.stderr,
            flush=True,
        )

    for current, _dirnames, filenames in os.walk(source, onerror=report_walk_error):
        current_path = Path(current)
        for filename in filenames:
            yield current_path / filename


def batched(values: Iterable[Path], size: int) -> Iterator[list[Path]]:
    batch: list[Path] = []
    for value in values:
        batch.append(value)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def add_dicom_to_report(report: ScanReport, result: FileScanResult) -> None:
    patient_id = result.patient_id
    patient_name = result.patient_name

    if patient_id:
        key = f"ID:{patient_id}"
        fallback_by_name = False
    elif patient_name:
        key = f"NAME:{patient_name.casefold()}"
        fallback_by_name = True
    else:
        report.dicom_without_identity += 1
        return

    patient = report.patients.get(key)
    if patient is None:
        patient = PatientSummary(
            patient_id=patient_id,
            fallback_by_name=fallback_by_name,
        )
        report.patients[key] = patient
    patient.add(patient_name)


def scan_source(
    source: Path,
    *,
    workers: int = 8,
    batch_size: int = 2000,
    progress_every: int = 10000,
) -> ScanReport:
    report = ScanReport()
    next_progress = max(1, progress_every)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        for paths in batched(iter_input_files(source), max(1, batch_size)):
            for result in executor.map(scan_one_file, paths):
                report.total_files += 1
                if result.status == "dicom":
                    report.dicom_files += 1
                    add_dicom_to_report(report, result)
                elif result.status == "not_dicom":
                    report.non_dicom_files += 1
                else:
                    report.read_errors += 1

            if progress_every > 0 and report.total_files >= next_progress:
                print(
                    f"\u5df2\u626b\u63cf {report.total_files:,} \u4e2a\u6587\u4ef6\uff0c"
                    f"\u53d1\u73b0 {len(report.patients):,} \u4e2a\u60a3\u8005...",
                    flush=True,
                )
                while next_progress <= report.total_files:
                    next_progress += progress_every

    return report


def print_report(report: ScanReport, elapsed_seconds: float) -> None:
    print("\n\u626b\u63cf\u5b8c\u6210")
    print(f"\u6587\u4ef6\u603b\u6570\uff1a{report.total_files:,}")
    print(f"DICOM\u6587\u4ef6\uff1a{report.dicom_files:,}")
    print(f"\u975eDICOM\u6587\u4ef6\uff1a{report.non_dicom_files:,}")
    print(f"\u8bfb\u53d6\u5931\u8d25\uff1a{report.read_errors:,}")
    print(
        "\u7f3a\u5c11PatientID\u548cPatientName\u7684DICOM\uff1a"
        f"{report.dicom_without_identity:,}"
    )
    print(f"\u60a3\u8005\u6570\u91cf\uff1a{len(report.patients):,}")
    print(f"\u8017\u65f6\uff1a{elapsed_seconds:.1f}\u79d2")
    print("\n\u60a3\u8005\u59d3\u540d\uff1a")

    patients = sorted(
        report.patients.values(),
        key=lambda item: (item.preferred_name.casefold(), item.patient_id),
    )
    for index, patient in enumerate(patients, start=1):
        details: list[str] = []
        if patient.patient_id:
            details.append(f"PatientID={patient.patient_id}")
        else:
            details.append("PatientID\u7f3a\u5931\uff0c\u6309\u59d3\u540d\u7edf\u8ba1")
        details.append(f"DICOM={patient.dicom_files:,}")
        if patient.aliases:
            details.append(
                f"\u59d3\u540d\u522b\u540d={' / '.join(patient.aliases)}"
            )
        detail_text = "\uff1b".join(details)
        print(
            f"{index}. {patient.preferred_name}"
            f"\uff08{detail_text}\uff09"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan DICOM headers and print unique patients and PatientName values."
    )
    parser.add_argument("source", type=Path, help="DICOM directory or one DICOM file")
    parser.add_argument(
        "--workers", type=int, default=8, help="Reader threads (default: 8)"
    )
    parser.add_argument(
        "--batch-size", type=int, default=2000, help="Files per batch (default: 2000)"
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=10000,
        help="Progress interval; use 0 to disable (default: 10000)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = args.source.expanduser().resolve()
    if not source.exists():
        print(
            f"\u9519\u8bef\uff1a\u8f93\u5165\u8def\u5f84\u4e0d\u5b58\u5728\uff1a{source}",
            file=sys.stderr,
        )
        return 2
    if args.workers < 1 or args.batch_size < 1 or args.progress_every < 0:
        print(
            "\u9519\u8bef\uff1aworkers\u548cbatch-size\u5fc5\u987b\u5927\u4e8e0\uff0c"
            "progress-every\u4e0d\u80fd\u5c0f\u4e8e0",
            file=sys.stderr,
        )
        return 2

    print(f"\u626b\u63cf\u8def\u5f84\uff1a{source}")
    print(f"\u8bfb\u53d6\u7ebf\u7a0b\uff1a{args.workers}")
    started = time.monotonic()
    report = scan_source(
        source,
        workers=args.workers,
        batch_size=args.batch_size,
        progress_every=args.progress_every,
    )
    print_report(report, time.monotonic() - started)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
