from __future__ import annotations

import csv
import shutil
import sys
import unittest
import uuid
import warnings
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import filter_anonymized_dicom_by_uid_selection as anonymous_filter


def write_dicom(
    path: Path,
    *,
    study_uid: str,
    series_uid: str,
    sop_uid: str,
    payload: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    dataset = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    dataset.SOPClassUID = SecondaryCaptureImageStorage
    dataset.SOPInstanceUID = sop_uid
    dataset.StudyInstanceUID = study_uid
    dataset.SeriesInstanceUID = series_uid
    dataset.PatientName = "ANON"
    dataset.PatientID = "001"
    dataset.ImageComments = payload
    dataset.save_as(path, enforce_file_format=True)


def write_selection(path: Path, rows: list[list[object]]) -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "筛选序列明细"
    worksheet.append(
        [
            "匿名编号",
            "目标二级目录",
            "StudyInstanceUID",
            "SeriesUID",
            "SOPUID",
            "modality",
            "是否进入匿名化",
        ]
    )
    for row in rows:
        worksheet.append(row)
    workbook.save(path)


def read_statuses(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return [row["状态"] for row in csv.DictReader(stream)]


class AnonymizedUidFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        output_root = Path(__file__).resolve().parents[1] / "outputs"
        output_root.mkdir(parents=True, exist_ok=True)
        self.root = output_root / f"uid_filter_test_{uuid.uuid4().hex}"
        self.root.mkdir(parents=True, exist_ok=False)
        self.source = self.root / "anonymous_source"
        self.destination = self.root / "selected_output"
        self.control = self.root / "selection_control"
        self.xlsx = self.root / "selection.xlsx"
        self.anonymous_code = "001"
        self.target_folder = "001-术前与术中"
        self.study_uid = generate_uid()
        self.series_whole = generate_uid()
        self.series_exact = generate_uid()
        self.sop_whole_1 = generate_uid()
        self.sop_whole_2 = generate_uid()
        self.sop_exact_selected = generate_uid()
        self.sop_exact_unselected = generate_uid()

        self.series_whole_dir = (
            self.source
            / self.anonymous_code
            / self.target_folder
            / self.study_uid
            / self.series_whole
        )
        self.series_exact_dir = (
            self.source
            / self.anonymous_code
            / self.target_folder
            / self.study_uid
            / self.series_exact
        )
        write_dicom(
            self.series_whole_dir / f"{self.sop_whole_1}.dcm",
            study_uid=self.study_uid,
            series_uid=self.series_whole,
            sop_uid=self.sop_whole_1,
            payload="WHOLE-1",
        )
        write_dicom(
            self.series_whole_dir / f"{self.sop_whole_2}.dcm",
            study_uid=self.study_uid,
            series_uid=self.series_whole,
            sop_uid=self.sop_whole_2,
            payload="WHOLE-2",
        )
        write_dicom(
            self.series_exact_dir / f"{self.sop_exact_selected}.dcm",
            study_uid=self.study_uid,
            series_uid=self.series_exact,
            sop_uid=self.sop_exact_selected,
            payload="EXACT-SELECTED",
        )
        write_dicom(
            self.series_exact_dir / f"{self.sop_exact_unselected}.dcm",
            study_uid=self.study_uid,
            series_uid=self.series_exact,
            sop_uid=self.sop_exact_unselected,
            payload="EXACT-NOT-SELECTED",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def standard_rows(self) -> list[list[object]]:
        return [
            [
                self.anonymous_code,
                self.target_folder,
                self.study_uid,
                self.series_whole,
                "NA",
                "3D-DSA",
                "是",
            ],
            [
                self.anonymous_code,
                self.target_folder,
                self.study_uid,
                self.series_exact,
                self.sop_exact_selected,
                "2D-DSA",
                "是",
            ],
            [
                self.anonymous_code,
                self.target_folder,
                self.study_uid,
                self.series_exact,
                self.sop_exact_unselected,
                "2D-DSA",
                "否",
            ],
        ]

    def run_filter(self, *, execute: bool) -> anonymous_filter.RunSummary:
        return anonymous_filter.run_filter(
            self.source,
            self.xlsx,
            self.destination,
            execute=execute,
            workers=2,
            copy_workers=2,
            control_dir=self.control,
            progress_every=0,
        )

    def target_for(self, series_uid: str, sop_uid: str) -> Path:
        return (
            self.destination
            / self.anonymous_code
            / self.target_folder
            / self.study_uid
            / series_uid
            / f"{sop_uid}.dcm"
        )

    def test_preview_plans_whole_series_and_exact_sop_only(self) -> None:
        write_selection(self.xlsx, self.standard_rows())

        summary = self.run_filter(execute=False)

        self.assertEqual(3, summary.workbook_rows)
        self.assertEqual(2, summary.included_rules)
        self.assertEqual(1, summary.ignored_rules)
        self.assertEqual(1, summary.series_rules)
        self.assertEqual(1, summary.sop_rules)
        self.assertEqual(2, summary.matched_rules)
        self.assertEqual(3, summary.selected_files)
        self.assertEqual(3, summary.header_validated)
        self.assertFalse(summary.needs_attention)
        self.assertFalse(self.destination.exists())
        self.assertTrue(Path(summary.detail_audit_path).is_file())
        self.assertTrue(Path(summary.group_summary_path).is_file())
        statuses = read_statuses(Path(summary.detail_audit_path))
        self.assertEqual(3, statuses.count("planned"))
        self.assertEqual(1, statuses.count("ignored_not_selected"))

    def test_execute_preserves_relative_layout_and_file_bytes(self) -> None:
        write_selection(self.xlsx, self.standard_rows())

        summary = self.run_filter(execute=True)

        expected = [
            (self.series_whole, self.sop_whole_1),
            (self.series_whole, self.sop_whole_2),
            (self.series_exact, self.sop_exact_selected),
        ]
        for series_uid, sop_uid in expected:
            source = (
                self.source
                / self.anonymous_code
                / self.target_folder
                / self.study_uid
                / series_uid
                / f"{sop_uid}.dcm"
            )
            target = self.target_for(series_uid, sop_uid)
            self.assertTrue(target.is_file())
            self.assertEqual(source.read_bytes(), target.read_bytes())
        self.assertFalse(
            self.target_for(self.series_exact, self.sop_exact_unselected).exists()
        )
        self.assertEqual(3, summary.copied)
        self.assertEqual(0, summary.conflicts)
        self.assertEqual(0, summary.errors)
        self.assertFalse((self.destination / "_selection_audit").exists())

    def test_rerun_keeps_identical_files_without_recopy(self) -> None:
        write_selection(self.xlsx, self.standard_rows()[:2])
        first = self.run_filter(execute=True)
        second = self.run_filter(execute=True)

        self.assertEqual(3, first.copied)
        self.assertEqual(0, second.copied)
        self.assertEqual(3, second.duplicate_same)
        self.assertFalse(second.needs_attention)

    def test_atomic_publish_failure_is_audited_without_final_or_partial_files(self) -> None:
        write_selection(self.xlsx, self.standard_rows()[:2])
        with patch.object(anonymous_filter.os, "link", side_effect=OSError("unsupported")):
            summary = self.run_filter(execute=True)

        self.assertEqual(3, summary.errors)
        self.assertEqual(0, summary.copied)
        self.assertTrue(summary.needs_attention)
        self.assertEqual([], list(self.destination.rglob("*.dcm")))
        self.assertEqual([], list(self.destination.rglob("*.part")))
        with Path(summary.detail_audit_path).open("r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(3, len(rows))
        for row in rows:
            self.assertEqual("error", row["状态"])
            self.assertIn("unsupported", row["说明"])
            self.assertEqual("", row["目标SHA256"])
            self.assertEqual(64, len(row["源SHA256"]))

    def test_target_conflict_blocks_that_rule_and_does_not_write_conflict_copy(self) -> None:
        write_selection(self.xlsx, self.standard_rows()[:2])
        conflict_target = self.target_for(self.series_exact, self.sop_exact_selected)
        write_dicom(
            conflict_target,
            study_uid=self.study_uid,
            series_uid=self.series_exact,
            sop_uid=self.sop_exact_selected,
            payload="DIFFERENT-TARGET-CONTENT",
        )
        original_target_bytes = conflict_target.read_bytes()

        summary = self.run_filter(execute=True)

        self.assertEqual(2, summary.copied)
        self.assertEqual(1, summary.conflicts)
        self.assertTrue(summary.needs_attention)
        self.assertEqual(original_target_bytes, conflict_target.read_bytes())
        self.assertEqual([], list(conflict_target.parent.glob("*.conflict.dcm")))

    def test_uid_mismatch_blocks_whole_series_but_other_rule_continues(self) -> None:
        mismatched_file = self.series_whole_dir / f"{self.sop_whole_2}.dcm"
        write_dicom(
            mismatched_file,
            study_uid=self.study_uid,
            series_uid=generate_uid(),
            sop_uid=self.sop_whole_2,
            payload="WRONG-SERIES-HEADER",
        )
        write_selection(self.xlsx, self.standard_rows()[:2])

        summary = self.run_filter(execute=True)

        self.assertEqual(1, summary.uid_mismatches)
        self.assertEqual(1, summary.blocked_rules)
        self.assertEqual(1, summary.copied)
        self.assertFalse(self.target_for(self.series_whole, self.sop_whole_1).exists())
        self.assertFalse(self.target_for(self.series_whole, self.sop_whole_2).exists())
        self.assertTrue(
            self.target_for(self.series_exact, self.sop_exact_selected).is_file()
        )

    def test_missing_exact_sop_does_not_fall_back_to_series(self) -> None:
        missing_sop = generate_uid()
        write_selection(
            self.xlsx,
            [
                [
                    self.anonymous_code,
                    self.target_folder,
                    self.study_uid,
                    self.series_exact,
                    missing_sop,
                    "2D-DSA",
                    "是",
                ]
            ],
        )

        summary = self.run_filter(execute=False)

        self.assertEqual(1, summary.missing_rules)
        self.assertEqual(0, summary.selected_files)
        self.assertTrue(summary.needs_attention)
        self.assertIn("sop_file_not_found", read_statuses(Path(summary.detail_audit_path)))

    def test_invalid_component_is_audited_without_path_escape(self) -> None:
        write_selection(
            self.xlsx,
            [
                [
                    self.anonymous_code,
                    "../escape",
                    self.study_uid,
                    self.series_whole,
                    "NA",
                    "3D-DSA",
                    "是",
                ]
            ],
        )

        summary = self.run_filter(execute=False)

        self.assertEqual(1, summary.invalid_rules)
        self.assertTrue(summary.needs_attention)
        self.assertFalse((self.root / "escape").exists())

    def test_path_safe_nonstandard_uid_is_warn_only(self) -> None:
        nonstandard_study = "1.2..840.123"
        series_uid = generate_uid()
        sop_uid = generate_uid()
        source_file = (
            self.source
            / self.anonymous_code
            / "001-6M"
            / nonstandard_study
            / series_uid
            / f"{sop_uid}.dcm"
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            write_dicom(
                source_file,
                study_uid=nonstandard_study,
                series_uid=series_uid,
                sop_uid=sop_uid,
                payload="NONSTANDARD-UID",
            )
        write_selection(
            self.xlsx,
            [
                [
                    self.anonymous_code,
                    "001-6M",
                    nonstandard_study,
                    series_uid,
                    "NA",
                    "MRA",
                    "是",
                ]
            ],
        )

        summary = self.run_filter(execute=False)

        self.assertEqual(1, summary.matched_rules)
        self.assertEqual(1, summary.selected_files)
        self.assertEqual(1, summary.header_validated)
        self.assertFalse(summary.needs_attention)

    def test_destination_and_control_must_be_separate_from_source(self) -> None:
        write_selection(self.xlsx, self.standard_rows()[:1])
        with self.assertRaisesRegex(ValueError, "目标根目录不能位于源目录内部"):
            anonymous_filter.run_filter(
                self.source,
                self.xlsx,
                self.source / "selected",
                control_dir=self.control,
                progress_every=0,
            )
        with self.assertRaisesRegex(ValueError, "控制目录必须与匿名化源目录相互独立"):
            anonymous_filter.run_filter(
                self.source,
                self.xlsx,
                self.destination,
                control_dir=self.source / "audit",
                progress_every=0,
            )


if __name__ == "__main__":
    unittest.main()
