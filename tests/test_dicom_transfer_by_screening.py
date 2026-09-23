from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import dicom_transfer_by_screening as transfer


def write_dicom(
    path: Path,
    *,
    patient_name: str = "CHEN^SU^FANG",
    patient_id: str = "PID-1",
    birth_date: str = "19490114",
    sex: str = "F",
    patient_age: str = "075Y",
    study_date: str = "20240205",
    study_uid: str | None = None,
    series_uid: str | None = None,
    sop_uid: str | None = None,
    payload: str = "A",
) -> tuple[str, str, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    study_uid = study_uid or generate_uid()
    series_uid = series_uid or generate_uid()
    sop_uid = sop_uid or generate_uid()
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
    if birth_date:
        dataset.PatientBirthDate = birth_date
    if sex:
        dataset.PatientSex = sex
    if patient_age:
        dataset.PatientAge = patient_age
    dataset.StudyDate = study_date
    dataset.InstitutionName = "Sichuan Provincial Peoples Hospital"
    dataset.ImageComments = payload
    dataset.save_as(path, enforce_file_format=True)
    return study_uid, series_uid, sop_uid


class IdentityDecisionTests(unittest.TestCase):
    def test_same_person_with_different_patient_ids_is_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "23_H005"
            destination = root / "target"
            write_dicom(source / "术中" / "a.dcm", patient_id="PID-A")
            write_dicom(
                source / "术后6个月" / "b.dcm",
                patient_id="PID-B",
                study_date="20240809",
            )

            records = transfer.scan_directory(source, destination, workers=1, progress_every=0)
            decision = transfer.decide_identity(records)

            self.assertTrue(decision.confirmed)
            self.assertIn("姓名", decision.evidence)
            self.assertIn("出生日期", decision.evidence)

    def test_conflicting_birth_date_blocks_transfer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "23_H005"
            destination = root / "target"
            write_dicom(source / "术中" / "a.dcm", birth_date="19490114")
            write_dicom(
                source / "术后6个月" / "b.dcm",
                patient_id="PID-B",
                birth_date="19500114",
            )

            records = transfer.scan_directory(source, destination, workers=1, progress_every=0)
            decision = transfer.decide_identity(records)

            self.assertEqual("conflict", decision.status)
            self.assertTrue(any("出生日期" in reason for reason in decision.reasons))

    def test_name_without_other_demographic_evidence_is_insufficient(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "23_H005"
            destination = root / "target"
            write_dicom(
                source / "only.dcm",
                birth_date="",
                sex="",
                patient_age="",
            )

            records = transfer.scan_directory(source, destination, workers=1, progress_every=0)
            decision = transfer.decide_identity(records)

            self.assertEqual("insufficient", decision.status)


class TransferTests(unittest.TestCase):
    def test_repeated_dot_uid_is_transferred_with_warning(self) -> None:
        record = transfer.DicomRecord(
            path=Path("source.dcm"),
            relative_path="source.dcm",
            relative_hash="abc",
            is_dicom=True,
            study_uid="1.2.3",
            series_uid="1.2..3.4",
            sop_uid="1.2.3.4.5",
            warnings=transfer._uid_warnings("SeriesInstanceUID", "1.2..3.4"),
        )

        target, message = transfer.destination_for(
            record, Path("target"), "23_H005"
        )

        self.assertNotIn("_quarantine", target.parts)
        self.assertEqual("1.2..3.4", target.parent.name)
        self.assertIn("SeriesInstanceUID格式异常", message)

    def test_uid_with_path_unsafe_character_is_quarantined(self) -> None:
        record = transfer.DicomRecord(
            path=Path("source.dcm"),
            relative_path="source.dcm",
            relative_hash="abc",
            is_dicom=True,
            study_uid="1.2.3",
            series_uid="1.2/3.4",
            sop_uid="1.2.3.4.5",
        )

        target, message = transfer.destination_for(
            record, Path("target"), "23_H005"
        )

        self.assertIn("_quarantine", target.parts)
        self.assertIn("UID无法安全作为路径", message)

    def test_destination_parent_of_source_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            center_root = Path(temporary) / "四川省人民"
            source = center_root / "23_H005"
            source.mkdir(parents=True)

            with self.assertRaisesRegex(ValueError, "独立转存目录"):
                transfer.validate_paths(source, center_root)

    def test_dry_run_does_not_create_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "23_H005"
            destination = root / "target"
            write_dicom(source / "a.dcm")

            exit_code = transfer.main(
                [str(source), str(destination), "--workers", "1", "--progress-every", "0"]
            )

            self.assertEqual(0, exit_code)
            self.assertFalse(destination.exists())

    def test_execute_creates_screening_uid_hierarchy_and_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "23_H005"
            destination = root / "target"
            study_uid, series_uid, sop_uid = write_dicom(source / "a.dcm")

            exit_code = transfer.main(
                [
                    str(source),
                    str(destination),
                    "--workers", "1",
                    "--copy-workers", "1",
                    "--progress-every", "0",
                    "--execute",
                ]
            )

            self.assertEqual(0, exit_code)
            self.assertTrue(
                (destination / "23_H005" / study_uid / series_uid / f"{sop_uid}.dcm").is_file()
            )
            self.assertEqual(1, len(list((destination / "_transfer_audit").glob("*.csv"))))

    def test_same_uid_different_content_is_preserved_as_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "23_H005"
            destination = root / "target"
            study_uid, series_uid, sop_uid = write_dicom(source / "a.dcm", payload="FIRST")
            records = transfer.scan_directory(source, destination, workers=1, progress_every=0)
            first = transfer.execute_transfer(
                records, destination, "23_H005", copy_workers=1, progress_every=0
            )
            self.assertEqual("copied", first[0].status)

            write_dicom(
                source / "a.dcm",
                study_uid=study_uid,
                series_uid=series_uid,
                sop_uid=sop_uid,
                payload="SECOND",
            )
            records = transfer.scan_directory(source, destination, workers=1, progress_every=0)
            second = transfer.execute_transfer(
                records, destination, "23_H005", copy_workers=1, progress_every=0
            )

            self.assertEqual("conflict", second[0].status)
            target_dir = destination / "23_H005" / study_uid / series_uid
            self.assertEqual(2, len(list(target_dir.glob("*.dcm"))))

    def test_batch_execute_processes_each_subject_independently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            center = root / "四川省人民"
            destination = root / "target"
            first_uids = write_dicom(
                center / "23_H001" / "术中" / "a.dcm",
                patient_name="PATIENT^ONE",
                patient_id="PID-1",
            )
            second_uids = write_dicom(
                center / "23_H002" / "术中" / "b.dcm",
                patient_name="PATIENT^TWO",
                patient_id="PID-2",
            )

            exit_code = transfer.main(
                [
                    str(center),
                    str(destination),
                    "--batch",
                    "--workers", "1",
                    "--copy-workers", "1",
                    "--progress-every", "0",
                    "--execute",
                ]
            )

            self.assertEqual(0, exit_code)
            for subject_id, (study_uid, series_uid, sop_uid) in (
                ("23_H001", first_uids),
                ("23_H002", second_uids),
            ):
                self.assertTrue(
                    (destination / subject_id / study_uid / series_uid / f"{sop_uid}.dcm").is_file()
                )
            audit_dir = destination / "_transfer_audit"
            self.assertEqual(2, len(list(audit_dir.glob("转存清单_*.csv"))))
            self.assertEqual(1, len(list(audit_dir.glob("批量转存汇总_*.csv"))))

    def test_batch_identity_failure_does_not_block_other_subjects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            center = root / "四川省人民"
            destination = root / "target"
            valid_uids = write_dicom(
                center / "23_H001" / "a.dcm",
                patient_name="PATIENT^ONE",
                patient_id="PID-1",
            )
            write_dicom(
                center / "23_H002" / "a.dcm",
                patient_name="PATIENT^TWO",
                patient_id="PID-2",
                birth_date="",
                sex="",
                patient_age="",
            )

            exit_code = transfer.main(
                [
                    str(center),
                    str(destination),
                    "--batch",
                    "--workers", "1",
                    "--copy-workers", "1",
                    "--progress-every", "0",
                    "--execute",
                ]
            )

            self.assertEqual(1, exit_code)
            study_uid, series_uid, sop_uid = valid_uids
            self.assertTrue(
                (destination / "23_H001" / study_uid / series_uid / f"{sop_uid}.dcm").is_file()
            )
            self.assertFalse((destination / "23_H002").exists())
            summary_path = next((destination / "_transfer_audit").glob("批量转存汇总_*.csv"))
            summary_text = summary_path.read_text(encoding="utf-8-sig")
            self.assertIn("23_H001", summary_text)
            self.assertIn("23_H002", summary_text)
            self.assertIn("identity_failed", summary_text)


if __name__ == "__main__":
    unittest.main()
