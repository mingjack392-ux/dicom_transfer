from __future__ import annotations

import csv
import hashlib
import shutil
import sys
import unittest
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid


ROOT = Path(__file__).resolve().parents[1]
WEB_REGISTRY = ROOT / "web_registry"
if str(WEB_REGISTRY) not in sys.path:
    sys.path.insert(0, str(WEB_REGISTRY))

import dicom_transfer_screening_packages_core as core


def write_dicom(
    path: Path,
    *,
    name: str,
    patient_id: str,
    birth_date: str = "19800102",
    sex: str = "F",
    patient_age: str = "045Y",
    study_uid: str | None = None,
    series_uid: str | None = None,
    sop_uid: str | None = None,
    payload: str = "",
) -> tuple[str, str, str]:
    study_uid = study_uid or generate_uid()
    series_uid = series_uid or generate_uid()
    sop_uid = sop_uid or generate_uid()
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    meta.MediaStorageSOPInstanceUID = sop_uid
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    dataset = FileDataset(str(path), {}, file_meta=meta, preamble=b"\0" * 128)
    dataset.SOPClassUID = SecondaryCaptureImageStorage
    dataset.SOPInstanceUID = sop_uid
    dataset.StudyInstanceUID = study_uid
    dataset.SeriesInstanceUID = series_uid
    dataset.PatientName = name
    dataset.SpecificCharacterSet = "ISO_IR 192"
    dataset.PatientID = patient_id
    dataset.PatientBirthDate = birth_date
    dataset.PatientSex = sex
    dataset.StudyDate = "20250102"
    dataset.PatientAge = patient_age
    dataset.ImageComments = payload
    path.parent.mkdir(parents=True, exist_ok=True)
    dataset.save_as(path, enforce_file_format=True)
    return study_uid, series_uid, sop_uid


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextmanager
def test_workspace():
    path = ROOT / "_bench_out" / f"screening_packages_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class ScreeningPackageTransferTests(unittest.TestCase):
    def test_cli_path_removes_invisible_boundary_characters(self) -> None:
        expected = (ROOT / "web_registry" / "河南省人民").resolve()
        actual = core.normalize_cli_path(f"\u200b{expected}\ufeff", "目标路径")
        self.assertEqual(actual, expected)

    def test_batch_rejects_single_subject_directory(self) -> None:
        with test_workspace() as work:
            source = work / "04-H001 单例数据"
            source.mkdir()
            with self.assertRaisesRegex(ValueError, "请改用single"):
                core.collect_batch_packages(source)

    def test_dicomdir_is_excluded_from_scan_identity_and_audit(self) -> None:
        with test_workspace() as work:
            source = work / "04-H001 单例数据"
            write_dicom(source / "DICOM" / "IM000001", name="测试者", patient_id="P1")
            dicomdir = source / "DICOMDIR"
            dicomdir.write_bytes((b"\0" * 128) + b"DICM")
            destination = work / "destination"

            exit_code = core.main([
                "single", str(source), str(destination), "--subject-id", "04-H001",
                "--workers", "1", "--temp-dir", str(work / "temp"),
            ], platform_label="test")

            self.assertEqual(exit_code, 0)
            audit_path = next(destination.joinpath("_transfer_audit").glob("预检清单_*.csv"))
            with audit_path.open("r", newline="", encoding="utf-8-sig") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 1)
            self.assertNotIn("DICOMDIR", rows[0]["源文件"].upper())

    def test_folders_only_ignores_top_level_and_nested_archives(self) -> None:
        with test_workspace() as work:
            source = work / "source"
            folder = source / "04-H001 已解压"
            folder_uids = write_dicom(folder / "folder.dcm", name="测试者", patient_id="P1")

            archived_file = work / "archived.dcm"
            archived_uids = write_dicom(archived_file, name="测试者", patient_id="P1")
            top_zip = source / "04-H001 顶层.zip"
            top_zip.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(top_zip, "w", zipfile.ZIP_DEFLATED) as output:
                output.write(archived_file, "top.dcm")
            with zipfile.ZipFile(folder / "nested.zip", "w", zipfile.ZIP_DEFLATED) as output:
                output.write(archived_file, "nested.dcm")

            destination = work / "destination"
            exit_code = core.main([
                "single", str(folder), str(destination), "--subject-id", "04-H001",
                "--folders-only", "--execute", "--workers", "1", "--copy-workers", "1",
            ], platform_label="test")

            self.assertEqual(exit_code, 0)
            self.assertTrue((
                destination / "04-H001" / folder_uids[0] / folder_uids[1]
                / f"{folder_uids[2]}.dcm"
            ).is_file())
            self.assertFalse((
                destination / "04-H001" / archived_uids[0] / archived_uids[1]
                / f"{archived_uids[2]}.dcm"
            ).exists())

    def test_folders_only_reuses_scan_checkpoint_and_rescans_changed_file(self) -> None:
        with test_workspace() as work:
            source = work / "04-H001 已解压"
            dicom_path = source / "one.dcm"
            write_dicom(dicom_path, name="测试者", patient_id="P1")
            destination = work / "destination"
            command = [
                "single", str(source), str(destination), "--subject-id", "04-H001",
                "--folders-only", "--workers", "1",
            ]

            self.assertEqual(core.main(command, platform_label="test"), 0)
            cache = destination / "_transfer_state" / "scan_cache" / "04-H001.json"
            self.assertTrue(cache.is_file())

            with patch.object(core, "scan_one_file", wraps=core.scan_one_file) as scanner:
                self.assertEqual(core.main(command, platform_label="test"), 0)
                self.assertEqual(scanner.call_count, 0)

            with dicom_path.open("ab") as stream:
                stream.write(b"changed")
            with patch.object(core, "scan_one_file", wraps=core.scan_one_file) as scanner:
                self.assertEqual(core.main(command, platform_label="test"), 0)
                self.assertEqual(scanner.call_count, 1)

            with patch.object(core, "scan_one_file", wraps=core.scan_one_file) as scanner:
                self.assertEqual(core.main([*command, "--rescan"], platform_label="test"), 0)
                self.assertEqual(scanner.call_count, 1)

    def test_screening_id_normalization(self) -> None:
        self.assertEqual(core.screening_id_from_name("04-H001 张三.zip"), "04-H001")
        self.assertEqual(core.screening_id_from_name("04_H002李四"), "04-H002")
        self.assertEqual(core.screening_id_from_name("说明.txt"), "")

    def test_multiple_patient_ids_with_insufficient_evidence_use_screening_default(self) -> None:
        records = [
            core.DicomRecord(
                Path("a"), "a", "a", True, patient_name="张三", patient_id="ID1",
                birth_date="19800102", sex="M", patient_age="045Y", study_date="20250102",
            ),
            core.DicomRecord(
                Path("b"), "b", "b", True, patient_name="张 三", patient_id="ID2",
                birth_date="19800102", sex="M", patient_age="045Y", study_date="20250102",
            ),
        ]
        profiles = core.profiles_from_records(records)
        decision = core.decide_profiles(profiles, records, [])
        self.assertEqual(decision.status, "confirmed")

        records[1].birth_date = ""
        records[1].patient_age = ""
        profiles = core.profiles_from_records(records)
        decision = core.decide_profiles(profiles, records, [])
        self.assertEqual(decision.status, "screening_default")
        self.assertTrue(decision.confirmed)

    def test_insufficient_identity_evidence_is_transferred_and_audited(self) -> None:
        with test_workspace() as work:
            source = work / "04-H004 mixed IDs"
            first_uids = write_dicom(
                source / "first.dcm", name="同一患者", patient_id="P1",
                birth_date="", sex="F", patient_age="",
            )
            second_uids = write_dicom(
                source / "second.dcm", name="同一患者", patient_id="P2",
                birth_date="", sex="F", patient_age="",
            )
            destination = work / "destination"

            exit_code = core.main([
                "single", str(source), str(destination), "--subject-id", "04-H004",
                "--execute", "--workers", "1", "--copy-workers", "1",
            ], platform_label="test")

            self.assertEqual(exit_code, 0)
            for uids in (first_uids, second_uids):
                self.assertTrue((
                    destination / "04-H004" / uids[0] / uids[1] / f"{uids[2]}.dcm"
                ).is_file())
            audit_path = next(destination.joinpath("_transfer_audit").glob("转存清单_*.csv"))
            with audit_path.open("r", newline="", encoding="utf-8-sig") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual({row["身份处理"] for row in rows}, {"screening_default_warning"})
            self.assertTrue(all(row["目标筛选号"] == "04-H004" for row in rows))

    def test_batch_reroutes_only_unique_strongly_matched_outlier(self) -> None:
        with test_workspace() as work:
            source = work / "source"
            destination = work / "destination"
            own_uids = write_dicom(
                source / "04-H001 主体" / "own.dcm", name="患者甲", patient_id="A1",
                birth_date="19800102", sex="M",
            )
            misplaced_uids = write_dicom(
                source / "04-H001 混入" / "misplaced.dcm", name="患者乙", patient_id="B1",
                birth_date="19900304", sex="F",
            )
            target_uids = write_dicom(
                source / "04-H002 主体" / "target.dcm", name="患者乙", patient_id="B1",
                birth_date="19900304", sex="F",
            )

            exit_code = core.main([
                "batch", str(source), str(destination), "--folders-only", "--execute",
                "--workers", "1", "--copy-workers", "1",
            ], platform_label="test")

            self.assertEqual(exit_code, 0)
            self.assertTrue((
                destination / "04-H001" / own_uids[0] / own_uids[1] / f"{own_uids[2]}.dcm"
            ).is_file())
            self.assertFalse((
                destination / "04-H001" / misplaced_uids[0] / misplaced_uids[1]
                / f"{misplaced_uids[2]}.dcm"
            ).exists())
            self.assertTrue((
                destination / "04-H002" / misplaced_uids[0] / misplaced_uids[1]
                / f"{misplaced_uids[2]}.dcm"
            ).is_file())
            self.assertTrue((
                destination / "04-H002" / target_uids[0] / target_uids[1]
                / f"{target_uids[2]}.dcm"
            ).is_file())
            h001_audit = next(
                destination.joinpath("_transfer_audit").glob("转存清单_04-H001_*.csv")
            )
            with h001_audit.open("r", newline="", encoding="utf-8-sig") as stream:
                rows = list(csv.DictReader(stream))
            rerouted = [row for row in rows if row["身份处理"] == "rerouted"]
            self.assertEqual(len(rerouted), 1)
            self.assertEqual(rerouted[0]["来源筛选号"], "04-H001")
            self.assertEqual(rerouted[0]["目标筛选号"], "04-H002")

    def test_confirmed_conflict_without_unique_target_is_not_guessed(self) -> None:
        with test_workspace() as work:
            source = work / "04-H001 mixed people"
            first_uids = write_dicom(
                source / "first.dcm", name="患者甲", patient_id="A1",
                birth_date="19800102", sex="M",
            )
            second_uids = write_dicom(
                source / "second.dcm", name="患者乙", patient_id="B1",
                birth_date="19900304", sex="F",
            )
            destination = work / "destination"

            exit_code = core.main([
                "single", str(source), str(destination), "--subject-id", "04-H001",
                "--execute", "--workers", "1", "--copy-workers", "1",
            ], platform_label="test")

            self.assertEqual(exit_code, 1)
            for uids in (first_uids, second_uids):
                self.assertFalse((
                    destination / "04-H001" / uids[0] / uids[1] / f"{uids[2]}.dcm"
                ).exists())

    def test_manual_same_subject_confirmation_transfers_conflicting_profiles(self) -> None:
        with test_workspace() as work:
            source = work / "04-H018 source-confirmed same person"
            first_uids = write_dicom(
                source / "first.dcm", name="同一患者", patient_id="P1",
                birth_date="19750310", sex="M", patient_age="051Y",
            )
            second_uids = write_dicom(
                source / "second.dcm", name="同一患者", patient_id="P2",
                birth_date="19520310", sex="M", patient_age="074Y",
            )
            destination = work / "destination"

            exit_code = core.main([
                "single", str(source), str(destination), "--subject-id", "04-H018",
                "--confirm-same-subject", "04-H018", "--execute",
                "--workers", "1", "--copy-workers", "1",
            ], platform_label="test")

            self.assertEqual(exit_code, 0)
            for uids in (first_uids, second_uids):
                self.assertTrue((
                    destination / "04-H018" / uids[0] / uids[1] / f"{uids[2]}.dcm"
                ).is_file())
            audit_path = next(
                destination.joinpath("_transfer_audit").glob("转存清单_04-H018_*.csv")
            )
            with audit_path.open("r", newline="", encoding="utf-8-sig") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual({row["身份处理"] for row in rows}, {"manual_confirmed_same"})
            self.assertTrue(all(row["目标筛选号"] == "04-H018" for row in rows))
            self.assertTrue(all("数据来源方人工确认" in row["身份说明"] for row in rows))

    def test_batch_merges_directory_and_zip_without_changing_source(self) -> None:
        with test_workspace() as work:
            source = work / "source"
            destination = work / "destination"
            temp_root = work / "temp"
            folder = source / "04-H001 张三 术前"
            first = folder / "first.dcm"
            first_uids = write_dicom(first, name="张三", patient_id="PID-A")
            first_hash = sha256(first)

            zip_source = work / "second.dcm"
            second_uids = write_dicom(zip_source, name="张 三", patient_id="PID-B")
            archive = source / "04-H001张三术中.zip"
            archive.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
                output.write(zip_source, "nested/second.dcm")
            archive_hash = sha256(archive)

            exit_code = core.main([
                "batch", str(source), str(destination), "--execute",
                "--workers", "1", "--copy-workers", "1", "--temp-dir", str(temp_root),
            ], platform_label="test")

            self.assertEqual(exit_code, 0)
            self.assertTrue((
                destination / "04-H001" / first_uids[0] / first_uids[1] / f"{first_uids[2]}.dcm"
            ).is_file())
            self.assertTrue((
                destination / "04-H001" / second_uids[0] / second_uids[1] / f"{second_uids[2]}.dcm"
            ).is_file())
            self.assertEqual(sha256(first), first_hash)
            self.assertEqual(sha256(archive), archive_hash)
            self.assertTrue(destination.joinpath("_transfer_state", "identity", "04-H001.json").is_file())
            self.assertTrue(any(destination.joinpath("_transfer_audit").glob("批量转存汇总_*.csv")))

    def test_single_supplement_checks_saved_identity(self) -> None:
        with test_workspace() as work:
            destination = work / "destination"
            temp_root = work / "temp"
            first_source = work / "04-H001 first"
            first_uids = write_dicom(
                first_source / "first.dcm", name="王五", patient_id="P1",
            )
            first_exit = core.main([
                "single", str(first_source), str(destination), "--subject-id", "04-H001",
                "--execute", "--workers", "1", "--copy-workers", "1",
                "--temp-dir", str(temp_root),
            ], platform_label="test")
            self.assertEqual(first_exit, 0)

            supplement_file = work / "supplement.dcm"
            supplement_uids = write_dicom(
                supplement_file, name="王五", patient_id="P2",
            )
            supplement_zip = work / "04-H001 supplement.zip"
            with zipfile.ZipFile(supplement_zip, "w", zipfile.ZIP_DEFLATED) as output:
                output.write(supplement_file, "supplement.dcm")
            second_exit = core.main([
                "single", str(supplement_zip), str(destination), "--subject-id", "04-H001",
                "--execute", "--workers", "1", "--copy-workers", "1",
                "--temp-dir", str(temp_root),
            ], platform_label="test")
            self.assertEqual(second_exit, 0)
            self.assertTrue((
                destination / "04-H001" / supplement_uids[0] / supplement_uids[1]
                / f"{supplement_uids[2]}.dcm"
            ).is_file())
            self.assertTrue((
                destination / "04-H001" / first_uids[0] / first_uids[1] / f"{first_uids[2]}.dcm"
            ).is_file())

    def test_single_different_person_is_blocked(self) -> None:
        with test_workspace() as work:
            destination = work / "destination"
            temp_root = work / "temp"
            first_source = work / "04-H001 first"
            write_dicom(first_source / "first.dcm", name="王五", patient_id="P1")
            self.assertEqual(core.main([
                "single", str(first_source), str(destination), "--subject-id", "04-H001",
                "--execute", "--workers", "1", "--copy-workers", "1",
                "--temp-dir", str(temp_root),
            ], platform_label="test"), 0)

            other_source = work / "other"
            other_uids = write_dicom(
                other_source / "other.dcm", name="赵六", patient_id="P9",
                birth_date="19900203", sex="M",
            )
            exit_code = core.main([
                "single", str(other_source), str(destination), "--subject-id", "04-H001",
                "--execute", "--workers", "1", "--copy-workers", "1",
                "--temp-dir", str(temp_root),
            ], platform_label="test")
            self.assertEqual(exit_code, 1)
            self.assertFalse((
                destination / "04-H001" / other_uids[0] / other_uids[1] / f"{other_uids[2]}.dcm"
            ).is_file())

    def test_single_outlier_uses_unique_saved_target_identity(self) -> None:
        with test_workspace() as work:
            destination = work / "destination"
            own_source = work / "04-H001 own"
            target_source = work / "04-H002 own"
            write_dicom(
                own_source / "own.dcm", name="患者甲", patient_id="A1",
                birth_date="19800102", sex="M",
            )
            write_dicom(
                target_source / "target.dcm", name="患者乙", patient_id="B1",
                birth_date="19900304", sex="F",
            )
            for source, screening_id in (
                (own_source, "04-H001"), (target_source, "04-H002"),
            ):
                self.assertEqual(core.main([
                    "single", str(source), str(destination), "--subject-id", screening_id,
                    "--execute", "--workers", "1", "--copy-workers", "1",
                ], platform_label="test"), 0)

            supplement = work / "supplement"
            supplement_uids = write_dicom(
                supplement / "misplaced.dcm", name="患者乙", patient_id="B1",
                birth_date="19900304", sex="F",
            )
            exit_code = core.main([
                "single", str(supplement), str(destination), "--subject-id", "04-H001",
                "--execute", "--workers", "1", "--copy-workers", "1",
            ], platform_label="test")

            self.assertEqual(exit_code, 0)
            self.assertFalse((
                destination / "04-H001" / supplement_uids[0] / supplement_uids[1]
                / f"{supplement_uids[2]}.dcm"
            ).exists())
            self.assertTrue((
                destination / "04-H002" / supplement_uids[0] / supplement_uids[1]
                / f"{supplement_uids[2]}.dcm"
            ).is_file())

    def test_preview_writes_audit_but_not_dicom(self) -> None:
        with test_workspace() as work:
            source = work / "04-H001 preview"
            uids = write_dicom(source / "one.dcm", name="测试者", patient_id="P1")
            destination = work / "chosen-output"
            exit_code = core.main([
                "single", str(source), str(destination), "--subject-id", "04-H001",
                "--workers", "1", "--temp-dir", str(work / "temp"),
            ], platform_label="test")
            self.assertEqual(exit_code, 0)
            self.assertFalse((
                destination / "04-H001" / uids[0] / uids[1] / f"{uids[2]}.dcm"
            ).exists())
            self.assertTrue(any(destination.joinpath("_transfer_audit").glob("预检清单_*.csv")))

    def test_uid_conflict_does_not_replace_formal_file(self) -> None:
        with test_workspace() as work:
            destination = work / "destination"
            temp_root = work / "temp"
            first_source = work / "04-H001 first"
            uids = write_dicom(
                first_source / "one.dcm", name="测试者", patient_id="P1", payload="FIRST",
            )
            self.assertEqual(core.main([
                "single", str(first_source), str(destination), "--subject-id", "04-H001",
                "--execute", "--workers", "1", "--copy-workers", "1",
                "--temp-dir", str(temp_root),
            ], platform_label="test"), 0)
            formal = destination / "04-H001" / uids[0] / uids[1] / f"{uids[2]}.dcm"
            first_hash = sha256(formal)

            second_source = work / "04-H001 second"
            write_dicom(
                second_source / "two.dcm", name="测试者", patient_id="P1",
                study_uid=uids[0], series_uid=uids[1], sop_uid=uids[2], payload="SECOND",
            )
            exit_code = core.main([
                "single", str(second_source), str(destination), "--subject-id", "04-H001",
                "--execute", "--workers", "1", "--copy-workers", "1",
                "--temp-dir", str(temp_root),
            ], platform_label="test")
            self.assertEqual(exit_code, 1)
            self.assertEqual(sha256(formal), first_hash)
            self.assertEqual(len(list(destination.joinpath("_conflicts").rglob("*.conflict.dcm"))), 1)


if __name__ == "__main__":
    unittest.main()
