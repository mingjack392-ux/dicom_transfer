from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import filter_transferred_dicom_by_selection as selection_filter


def write_dicom(
    path: Path,
    *,
    patient_name: str,
    patient_id: str,
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
    dataset.PatientName = patient_name
    dataset.PatientID = patient_id
    dataset.ImageComments = payload
    dataset.save_as(path, enforce_file_format=True)


def write_selection(path: Path, rows: list[list[object]]) -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "宣武"
    worksheet.append(
        ["住院号", "患者", "StudyInstanceUID", "SeriesUID", "SOPUID", "AcqusitionDate", "时期"]
    )
    for row in rows:
        worksheet.append(row)
    workbook.save(path)


class SelectionFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.destination = self.root / "filtered"
        self.audit = self.root / "audit"
        self.xlsx = self.root / "筛选序列.xlsx"
        self.patient = self.source / "10001 张三"
        self.study_uid = generate_uid()
        self.pre_series_uid = generate_uid()
        self.intra_series_uid = generate_uid()
        self.pre_sop_1 = generate_uid()
        self.pre_sop_2 = generate_uid()
        self.intra_sop_selected = generate_uid()
        self.intra_sop_unselected = generate_uid()

        write_dicom(
            self.patient / self.study_uid / self.pre_series_uid / f"{self.pre_sop_1}.dcm",
            patient_name="ZHANG^SAN",
            patient_id="PID-1",
            study_uid=self.study_uid,
            series_uid=self.pre_series_uid,
            sop_uid=self.pre_sop_1,
            payload="PRE-1",
        )
        write_dicom(
            self.patient / self.study_uid / self.pre_series_uid / f"{self.pre_sop_2}.dcm",
            patient_name="ZHANG^SAN",
            patient_id="PID-1",
            study_uid=self.study_uid,
            series_uid=self.pre_series_uid,
            sop_uid=self.pre_sop_2,
            payload="PRE-2",
        )
        write_dicom(
            self.patient / self.study_uid / self.intra_series_uid / f"{self.intra_sop_selected}.dcm",
            patient_name="ZHANG^SAN",
            patient_id="PID-1",
            study_uid=self.study_uid,
            series_uid=self.intra_series_uid,
            sop_uid=self.intra_sop_selected,
            payload="INTRA-SELECTED",
        )
        write_dicom(
            self.patient / self.study_uid / self.intra_series_uid / f"{self.intra_sop_unselected}.dcm",
            patient_name="ZHANG^SAN",
            patient_id="PID-1",
            study_uid=self.study_uid,
            series_uid=self.intra_series_uid,
            sop_uid=self.intra_sop_unselected,
            payload="INTRA-NOT-SELECTED",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def selection_rows(self) -> list[list[object]]:
        return [
            [10001, "张三", self.study_uid, self.pre_series_uid, "NA", 20240101, "术前"],
            [10001, "张三", self.study_uid, self.intra_series_uid, self.intra_sop_selected, 20240102, "术中"],
            [99999, "不处理", generate_uid(), generate_uid(), "NA", 20240101, "术前"],
            [10001, "张三", self.study_uid, generate_uid(), generate_uid(), 20240701, "术后6个月"],
        ]

    def test_preview_ignores_table_patient_missing_from_source(self) -> None:
        write_selection(self.xlsx, self.selection_rows())

        summary = selection_filter.run_filter(
            self.source,
            self.xlsx,
            self.destination,
            sheet_name="宣武",
            execute=False,
            workers=1,
            audit_dir=self.audit,
            progress_every=0,
        )

        self.assertEqual(3, summary.selection_rows)
        self.assertEqual(2, summary.selection_patients)
        self.assertEqual(1, summary.matched_patients)
        self.assertEqual(1, summary.ignored_missing_source_patients)
        self.assertEqual(2, summary.matched_rules)
        self.assertEqual(3, summary.planned_files)
        self.assertFalse(summary.needs_attention)
        self.assertFalse(self.destination.exists())
        self.assertTrue(Path(summary.detail_audit_path).is_file())
        self.assertTrue(Path(summary.patient_summary_path).is_file())

    def test_execute_copies_whole_preop_series_and_only_selected_intraop_sop(self) -> None:
        write_selection(self.xlsx, self.selection_rows()[:2])

        summary = selection_filter.run_filter(
            self.source,
            self.xlsx,
            self.destination,
            execute=True,
            workers=1,
            copy_workers=2,
            audit_dir=self.audit,
            progress_every=0,
        )

        pre_root = (
            self.destination
            / self.patient.name
            / selection_filter.PREOP_CATEGORY
            / self.study_uid
            / self.pre_series_uid
        )
        intra_root = (
            self.destination
            / self.patient.name
            / selection_filter.INTRAOP_CATEGORY
            / self.study_uid
            / self.intra_series_uid
        )
        self.assertTrue((pre_root / f"{self.pre_sop_1}.dcm").is_file())
        self.assertTrue((pre_root / f"{self.pre_sop_2}.dcm").is_file())
        self.assertTrue((intra_root / f"{self.intra_sop_selected}.dcm").is_file())
        self.assertFalse((intra_root / f"{self.intra_sop_unselected}.dcm").exists())
        self.assertEqual(3, summary.copied)
        self.assertEqual(0, summary.errors)

    def test_uid_not_found_requires_review(self) -> None:
        write_selection(
            self.xlsx,
            [[10001, "张三", self.study_uid, generate_uid(), "NA", 20240101, "术前"]],
        )

        summary = selection_filter.run_filter(
            self.source,
            self.xlsx,
            self.destination,
            execute=False,
            workers=1,
            audit_dir=self.audit,
            progress_every=0,
        )

        self.assertEqual(1, summary.uid_not_found)
        self.assertTrue(summary.needs_attention)
        self.assertEqual(0, summary.planned_files)

    def test_source_patient_without_selection_requires_review(self) -> None:
        extra_patient = self.source / "20002 李四"
        write_dicom(
            extra_patient / self.study_uid / self.pre_series_uid / "extra.dcm",
            patient_name="LI^SI",
            patient_id="PID-2",
            study_uid=self.study_uid,
            series_uid=self.pre_series_uid,
            sop_uid=generate_uid(),
            payload="EXTRA",
        )
        write_selection(self.xlsx, self.selection_rows()[:2])

        summary = selection_filter.run_filter(
            self.source,
            self.xlsx,
            self.destination,
            execute=False,
            workers=1,
            audit_dir=self.audit,
            progress_every=0,
        )

        self.assertEqual(1, summary.ignored_source_directories)
        self.assertTrue(summary.needs_attention)

    def test_same_sop_different_content_is_preserved_as_conflict(self) -> None:
        duplicate_path = self.patient / "duplicate" / "same-sop-different-content.dcm"
        write_dicom(
            duplicate_path,
            patient_name="ZHANG^SAN",
            patient_id="PID-1",
            study_uid=self.study_uid,
            series_uid=self.intra_series_uid,
            sop_uid=self.intra_sop_selected,
            payload="DIFFERENT-CONTENT",
        )
        write_selection(
            self.xlsx,
            [[10001, "张三", self.study_uid, self.intra_series_uid, self.intra_sop_selected, 20240102, "术中"]],
        )

        summary = selection_filter.run_filter(
            self.source,
            self.xlsx,
            self.destination,
            execute=True,
            workers=1,
            copy_workers=2,
            audit_dir=self.audit,
            progress_every=0,
        )

        output_dir = (
            self.destination
            / self.patient.name
            / selection_filter.INTRAOP_CATEGORY
            / self.study_uid
            / self.intra_series_uid
        )
        self.assertTrue((output_dir / f"{self.intra_sop_selected}.dcm").is_file())
        self.assertEqual(1, len(list(output_dir.glob("*.conflict.dcm"))))
        self.assertEqual(1, summary.conflicts)
        self.assertTrue(summary.needs_attention)

    def test_destination_cannot_be_inside_source(self) -> None:
        write_selection(self.xlsx, self.selection_rows()[:1])
        with self.assertRaisesRegex(ValueError, "目标根目录不能位于源目录内部"):
            selection_filter.run_filter(
                self.source,
                self.xlsx,
                self.source / "filtered",
                execute=False,
                workers=1,
                audit_dir=self.audit,
                progress_every=0,
            )


if __name__ == "__main__":
    unittest.main()
