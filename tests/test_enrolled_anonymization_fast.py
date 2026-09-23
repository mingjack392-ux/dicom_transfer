import argparse
import csv
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
import warnings
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


OPENPYXL_AVAILABLE = importlib.util.find_spec("openpyxl") is not None
PYDICOM_AVAILABLE = importlib.util.find_spec("pydicom") is not None


@unittest.skipUnless(OPENPYXL_AVAILABLE and PYDICOM_AVAILABLE, "需要openpyxl和pydicom")
class FastAnonymizationTests(unittest.TestCase):
    @staticmethod
    def make_dicom(
        path: Path,
        study_uid: str,
        series_uid: str,
        sop_uid: str,
        *,
        pixel_data: bytes = b"\x01\x02\x03\x04",
        file_meta_sop_uid: str | None = None,
        patient_id: str = "PID-KEEP-FAST",
    ) -> None:
        import pydicom
        from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
        from pydicom.sequence import Sequence
        from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage

        file_meta = FileMetaDataset()
        file_meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
        file_meta.MediaStorageSOPInstanceUID = file_meta_sop_uid or sop_uid
        file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        dataset = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
        dataset.is_little_endian = True
        dataset.is_implicit_VR = False
        dataset.SOPClassUID = SecondaryCaptureImageStorage
        dataset.SOPInstanceUID = sop_uid
        dataset.SpecificCharacterSet = "ISO_IR 192"
        dataset.StudyInstanceUID = study_uid
        dataset.SeriesInstanceUID = series_uid
        dataset.PatientName = "测试患者"
        dataset.PatientID = patient_id
        dataset.PatientBirthDate = "19800102"
        dataset.InstitutionName = "测试中心医院"
        dataset.StudyDate = "20250901"
        dataset.StudyTime = "101112.123"
        dataset.StationName = "测试中心医院DSA"
        nested = Dataset()
        nested.PatientName = "嵌套姓名"
        nested.PatientBirthDate = "19700101"
        nested.InstitutionName = "测试中心医院"
        dataset.ReferencedStudySequence = Sequence([nested])
        dataset.Rows = 1
        dataset.Columns = len(pixel_data)
        dataset.SamplesPerPixel = 1
        dataset.PhotometricInterpretation = "MONOCHROME2"
        dataset.BitsAllocated = 8
        dataset.BitsStored = 8
        dataset.HighBit = 7
        dataset.PixelRepresentation = 0
        dataset.PixelData = pixel_data
        path.parent.mkdir(parents=True, exist_ok=True)
        pydicom.dcmwrite(
            path,
            dataset,
            enforce_file_format=file_meta_sop_uid is None,
        )

    @staticmethod
    def make_headerless_dicom(
        path: Path,
        study_uid: str,
        series_uid: str,
        sop_uid: str,
        *,
        pixel_data: bytes = b"\x01\x02\x03\x04",
        patient_id: str = "PID-KEEP-HEADERLESS",
    ) -> None:
        import pydicom
        from pydicom.dataset import Dataset
        from pydicom.sequence import Sequence
        from pydicom.uid import SecondaryCaptureImageStorage

        dataset = Dataset()
        dataset.is_little_endian = True
        dataset.is_implicit_VR = True
        dataset.SOPClassUID = SecondaryCaptureImageStorage
        dataset.SOPInstanceUID = sop_uid
        dataset.SpecificCharacterSet = "ISO_IR 192"
        dataset.StudyInstanceUID = study_uid
        dataset.SeriesInstanceUID = series_uid
        dataset.PatientName = "测试患者"
        dataset.PatientID = patient_id
        dataset.PatientBirthDate = "19800102"
        dataset.InstitutionName = "测试中心医院"
        dataset.StudyDate = "20250902"
        dataset.StudyTime = "111213.456"
        dataset.StationName = "测试中心医院CT"
        nested = Dataset()
        nested.PatientName = "嵌套姓名"
        nested.PatientBirthDate = "19700101"
        nested.InstitutionName = "测试中心医院"
        dataset.ReferencedStudySequence = Sequence([nested])
        dataset.Rows = 1
        dataset.Columns = len(pixel_data)
        dataset.SamplesPerPixel = 1
        dataset.PhotometricInterpretation = "MONOCHROME2"
        dataset.BitsAllocated = 8
        dataset.BitsStored = 8
        dataset.HighBit = 7
        dataset.PixelRepresentation = 0
        dataset.PixelData = pixel_data
        path.parent.mkdir(parents=True, exist_ok=True)
        pydicom.dcmwrite(path, dataset, enforce_file_format=False)

    @staticmethod
    def uid_snapshot(dataset) -> tuple[tuple[str, int, str], ...]:
        values: list[tuple[str, int, str]] = []

        def walk(item, prefix: str) -> None:
            for element in item:
                location = f"{prefix}/{int(element.tag):08X}"
                if element.VR == "SQ":
                    for index, child in enumerate(element.value or []):
                        walk(child, f"{location}[{index}]")
                elif element.VR == "UI":
                    values.append((location, int(element.tag), str(element.value)))

        walk(dataset, "root")
        if dataset.file_meta:
            for element in dataset.file_meta:
                if element.VR == "UI":
                    values.append(
                        (f"file_meta/{int(element.tag):08X}", int(element.tag), str(element.value))
                    )
        return tuple(values)

    @staticmethod
    def make_list(path: Path, study_uid: str) -> None:
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "入组Study清单"
        sheet.append(
            [
                "中心编号",
                "中心名称",
                "匿名编号",
                "StudyInstanceUID",
                "标准访视期",
                "目标二级目录",
                "是否进入匿名化",
            ]
        )
        sheet.append([1, "测试中心医院", "001", study_uid, "6M", "001-6M", "是"])
        workbook.save(path)
        workbook.close()

    @staticmethod
    def arguments(
        root: Path,
        output_name: str,
        *,
        processes: int = 2,
        backend: str = "thread",
        uid_policy: str = "strict",
        headerless_policy: str = "strict",
        confirmed_multi_patient_studies: list[str] | None = None,
    ) -> argparse.Namespace:
        return argparse.Namespace(
            source_center=str(root / "source"),
            study_list=str(root / "study-list.xlsx"),
            output_root=str(root / output_name),
            control_dir=str(root / f"{output_name}-control"),
            sheet="入组Study清单",
            center_alias=["测试中心"],
            workers=1,
            processes=processes,
            backend=backend,
            max_in_flight=2,
            batch_targets=0,
            progress_every=0,
            no_resume=False,
            uid_policy=uid_policy,
            headerless_policy=headerless_policy,
            confirmed_multi_patient_study=list(
                confirmed_multi_patient_studies or []
            ),
            execute=True,
        )

    def test_fast_output_matches_stable_engine_and_resumes(self) -> None:
        import pydicom
        import anonymize_enrolled_studies as stable
        import anonymize_enrolled_studies_fast as fast

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary_directory:
            root = Path(temporary_directory)
            study_uid = "1.2.826.0.1.3680043.8.498.4101"
            series_uid = "1.2.826.0.1.3680043.8.498.4201"
            sop_uids = [
                f"1.2.826.0.1.3680043.8.498.43{index:02d}" for index in range(1, 5)
            ]
            for index, sop_uid in enumerate(sop_uids, start=1):
                self.make_dicom(
                    root / "source" / "01_H001" / study_uid / series_uid / f"{sop_uid}.dcm",
                    study_uid,
                    series_uid,
                    sop_uid,
                    pixel_data=bytes([index, index + 1, index + 2, index + 3]),
                )
            self.make_list(root / "study-list.xlsx", study_uid)

            stable_args = argparse.Namespace(
                source_center=str(root / "source"),
                study_list=str(root / "study-list.xlsx"),
                output_root=str(root / "stable-output"),
                control_dir=str(root / "stable-control"),
                sheet="入组Study清单",
                center_alias=["测试中心"],
                workers=1,
                execute=True,
            )
            stable_summary, _rows, _audit = stable.run(stable_args)
            self.assertEqual(stable_summary.get("已写入"), 4)

            fast_args = self.arguments(root, "fast-output", processes=2)
            first = fast.run(fast_args)
            self.assertEqual(first.summary.get("已写入"), 4)
            self.assertEqual(first.processed_files, 4)
            self.assertEqual(first.resumed_files, 0)
            self.assertTrue(first.checkpoint_path and first.checkpoint_path.is_file())
            self.assertTrue(first.audit_path and first.audit_path.is_file())

            target_mtimes = {}
            for sop_uid in sop_uids:
                relative = Path("001") / "001-6M" / study_uid / series_uid / f"{sop_uid}.dcm"
                stable_target = root / "stable-output" / relative
                fast_target = root / "fast-output" / relative
                self.assertEqual(
                    hashlib.sha256(stable_target.read_bytes()).hexdigest(),
                    hashlib.sha256(fast_target.read_bytes()).hexdigest(),
                )
                result = pydicom.dcmread(fast_target)
                self.assertNotIn("PatientName", result)
                self.assertNotIn("PatientBirthDate", result)
                self.assertNotIn("InstitutionName", result)
                self.assertNotIn("StationName", result)
                self.assertEqual(result.PatientID, "PID-KEEP-FAST")
                self.assertEqual(result.StudyDate, "20250901")
                self.assertEqual(result.StudyInstanceUID, study_uid)
                target_mtimes[str(fast_target)] = fast_target.stat().st_mtime_ns

            preserve_normal_args = self.arguments(
                root,
                "preserve-normal-output",
                processes=1,
                uid_policy="preserve",
            )
            preserve_normal = fast.run(preserve_normal_args)
            self.assertEqual(preserve_normal.summary.get("已写入"), 4)
            self.assertFalse(any(row.get("源UID问题") for row in preserve_normal.audits))
            for sop_uid in sop_uids:
                relative = Path("001") / "001-6M" / study_uid / series_uid / f"{sop_uid}.dcm"
                self.assertEqual(
                    hashlib.sha256((root / "stable-output" / relative).read_bytes()).hexdigest(),
                    hashlib.sha256(
                        (root / "preserve-normal-output" / relative).read_bytes()
                    ).hexdigest(),
                )

            old_context = fast._previous_checkpoint_context(
                ("测试中心医院", "source", "测试中心")
            )
            checkpoint_rows = [
                json.loads(line)
                for line in preserve_normal.checkpoint_path.read_text(
                    encoding="utf-8"
                ).splitlines()
                if line.strip()
            ]
            for checkpoint_row in checkpoint_rows:
                checkpoint_row["context"] = old_context
                checkpoint_row.pop("source_uid_issues", None)
            preserve_normal.checkpoint_path.write_text(
                "".join(
                    json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                    for row in checkpoint_rows
                ),
                encoding="utf-8",
            )
            rechecked_old_checkpoint = fast.run(preserve_normal_args)
            self.assertEqual(rechecked_old_checkpoint.resumed_files, 0)
            self.assertEqual(rechecked_old_checkpoint.processed_files, 4)
            self.assertEqual(rechecked_old_checkpoint.summary.get("重复相同"), 4)

            second = fast.run(fast_args)
            self.assertEqual(second.summary.get("断点跳过"), 4)
            self.assertEqual(second.processed_files, 0)
            self.assertEqual(second.resumed_files, 4)
            for path, mtime_ns in target_mtimes.items():
                self.assertEqual(Path(path).stat().st_mtime_ns, mtime_ns)

            with second.audit_path.open("r", newline="", encoding="utf-8-sig") as stream:
                audit_rows = list(csv.DictReader(stream))
            self.assertEqual({row["执行引擎"] for row in audit_rows}, {fast.ENGINE_VERSION})
            self.assertEqual({row["状态"] for row in audit_rows}, {"断点跳过"})

            changed_source = (
                root / "source" / "01_H001" / study_uid / series_uid
                / f"{sop_uids[0]}.dcm"
            )
            source_stat = changed_source.stat()
            os.utime(
                changed_source,
                ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns + 2_000_000_000),
            )
            third = fast.run(fast_args)
            self.assertEqual(third.processed_files, 1)
            self.assertEqual(third.resumed_files, 3)
            self.assertEqual(third.summary.get("重复相同"), 1)

            changed_target = Path(next(iter(target_mtimes)))
            target_stat = changed_target.stat()
            os.utime(
                changed_target,
                ns=(target_stat.st_atime_ns, target_stat.st_mtime_ns + 2_000_000_000),
            )
            fourth = fast.run(fast_args)
            self.assertEqual(fourth.processed_files, 1)
            self.assertEqual(fourth.resumed_files, 3)
            self.assertEqual(fourth.summary.get("重复相同"), 1)

    def test_same_target_uid_sources_are_serialized(self) -> None:
        import anonymize_enrolled_studies_fast as fast

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary_directory:
            root = Path(temporary_directory)
            study_uid = "1.2.826.0.1.3680043.8.498.5101"
            series_uid = "1.2.826.0.1.3680043.8.498.5201"
            sop_uid = "1.2.826.0.1.3680043.8.498.5301"
            first = root / "source" / "01_H001" / study_uid / series_uid / "first.dcm"
            second = root / "source" / "01_H001" / study_uid / series_uid / "second.dcm"
            self.make_dicom(first, study_uid, series_uid, sop_uid)
            shutil.copy2(first, second)
            self.make_list(root / "study-list.xlsx", study_uid)

            result = fast.run(
                self.arguments(
                    root, "fast-output", processes=2, backend="process"
                )
            )

            self.assertEqual(result.summary.get("已写入"), 1)
            self.assertEqual(result.summary.get("重复相同"), 1)
            self.assertNotIn("UID冲突", result.summary)
            target = (
                root / "fast-output" / "001" / "001-6M" / study_uid
                / series_uid / f"{sop_uid}.dcm"
            )
            self.assertTrue(target.is_file())
            self.assertFalse((root / "fast-output-control" / "_conflicts").exists())

    def test_confirmed_multi_patient_study_is_exact_audited_and_resumable(self) -> None:
        import pydicom
        import anonymize_enrolled_studies_fast as fast

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary_directory:
            root = Path(temporary_directory)
            study_uid = "1.2.826.0.1.3680043.8.498.5351"
            sources = [
                (
                    "1.2.826.0.1.3680043.8.498.5352",
                    "1.2.826.0.1.3680043.8.498.5353",
                    "PATIENT-A",
                ),
                (
                    "1.2.826.0.1.3680043.8.498.5354",
                    "1.2.826.0.1.3680043.8.498.5355",
                    "PATIENT-B",
                ),
            ]
            for series_uid, sop_uid, patient_id in sources:
                self.make_dicom(
                    root
                    / "source"
                    / "01_H001"
                    / study_uid
                    / series_uid
                    / f"{sop_uid}.dcm",
                    study_uid,
                    series_uid,
                    sop_uid,
                    patient_id=patient_id,
                )
            self.make_list(root / "study-list.xlsx", study_uid)

            blocked = fast.run(
                self.arguments(root, "blocked-multi-patient", processes=1)
            )
            self.assertEqual(blocked.summary.get("Study包含多个PatientID"), 1)
            self.assertEqual(blocked.processed_files, 0)
            self.assertFalse(
                any((root / "blocked-multi-patient").rglob("*.dcm"))
            )

            confirmed_args = self.arguments(
                root,
                "confirmed-multi-patient",
                processes=2,
                confirmed_multi_patient_studies=[study_uid],
            )
            confirmed = fast.run(confirmed_args)
            self.assertEqual(confirmed.summary.get("Study多PatientID人工确认"), 1)
            self.assertEqual(confirmed.summary.get("已写入"), 2)
            self.assertFalse(set(confirmed.summary) & fast.FAILED_STATUSES)
            self.assertEqual(confirmed.processed_files, 2)

            confirmed_rows = [
                row
                for row in confirmed.audits
                if row.get("StudyInstanceUID") == study_uid
            ]
            self.assertEqual(len(confirmed_rows), 3)
            self.assertTrue(
                all(row.get("多PatientID人工确认") for row in confirmed_rows)
            )
            with confirmed.audit_path.open(
                "r", newline="", encoding="utf-8-sig"
            ) as stream:
                persisted_rows = list(csv.DictReader(stream))
            persisted_for_study = [
                row
                for row in persisted_rows
                if row.get("StudyInstanceUID") == study_uid
            ]
            self.assertEqual(len(persisted_for_study), 3)
            self.assertTrue(
                all(row.get("多PatientID人工确认") for row in persisted_for_study)
            )
            with confirmed.verification_path.open(
                "r", newline="", encoding="utf-8-sig"
            ) as stream:
                self.assertEqual(list(csv.DictReader(stream)), [])

            output_patient_ids = {
                str(pydicom.dcmread(path, stop_before_pixels=True).PatientID)
                for path in (root / "confirmed-multi-patient").rglob("*.dcm")
            }
            self.assertEqual(output_patient_ids, {"PATIENT-A", "PATIENT-B"})

            resumed = fast.run(confirmed_args)
            self.assertEqual(resumed.resumed_files, 2)
            self.assertEqual(resumed.processed_files, 0)
            self.assertEqual(resumed.summary.get("断点跳过"), 2)
            resumed_file_rows = [
                row for row in resumed.audits if row.get("状态") == "断点跳过"
            ]
            self.assertEqual(len(resumed_file_rows), 2)
            self.assertTrue(
                all(row.get("多PatientID人工确认") for row in resumed_file_rows)
            )

    def test_multi_patient_confirmation_rejects_unknown_or_unused_study(self) -> None:
        import anonymize_enrolled_studies_fast as fast

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary_directory:
            root = Path(temporary_directory)
            study_uid = "1.2.826.0.1.3680043.8.498.5361"
            series_uid = "1.2.826.0.1.3680043.8.498.5362"
            sop_uid = "1.2.826.0.1.3680043.8.498.5363"
            self.make_dicom(
                root
                / "source"
                / "01_H001"
                / study_uid
                / series_uid
                / f"{sop_uid}.dcm",
                study_uid,
                series_uid,
                sop_uid,
            )
            self.make_list(root / "study-list.xlsx", study_uid)

            with self.assertRaisesRegex(ValueError, "不在本次入组清单"):
                fast.run(
                    self.arguments(
                        root,
                        "unknown-confirmation",
                        processes=1,
                        confirmed_multi_patient_studies=[
                            "1.2.826.0.1.3680043.8.498.9999"
                        ],
                    )
                )
            with self.assertRaisesRegex(ValueError, "未实际检测到多个PatientID"):
                fast.run(
                    self.arguments(
                        root,
                        "unused-confirmation",
                        processes=1,
                        confirmed_multi_patient_studies=[study_uid],
                    )
                )

    def test_headerless_policy_is_opt_in_normalizes_and_deduplicates(self) -> None:
        import pydicom
        from pydicom.errors import InvalidDicomError
        import anonymize_enrolled_studies_fast as fast

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary_directory:
            root = Path(temporary_directory)
            study_uid = "1.2.826.0.1.3680043.8.498.5401"
            standard_series_uid = "1.2.826.0.1.3680043.8.498.5402"
            standard_sop_uid = "1.2.826.0.1.3680043.8.498.5403"
            headerless_series_uid = "1.2.826.0.1.3680043.8.498.5404"
            headerless_sop_uid = "1.2.826.0.1.3680043.8.498.5405"
            source_root = root / "source" / "01_H001" / study_uid
            self.make_dicom(
                source_root / standard_series_uid / f"{standard_sop_uid}.dcm",
                study_uid,
                standard_series_uid,
                standard_sop_uid,
                pixel_data=b"\x01\x02\x03\x04",
            )
            headerless_source = (
                source_root / headerless_series_uid / f"{headerless_sop_uid}.dcm"
            )
            headerless_pixels = b"\x05\x06\x07\x08"
            self.make_headerless_dicom(
                headerless_source,
                study_uid,
                headerless_series_uid,
                headerless_sop_uid,
                pixel_data=headerless_pixels,
                patient_id="PID-KEEP-FAST",
            )
            self.make_list(root / "study-list.xlsx", study_uid)

            with self.assertRaises(InvalidDicomError):
                pydicom.dcmread(headerless_source, force=False)

            strict_result = fast.run(
                self.arguments(root, "strict-headerless-output", processes=1)
            )
            self.assertEqual(strict_result.summary.get("DICOM读取失败"), 1)
            self.assertEqual(strict_result.processed_files, 0)
            self.assertFalse(any((root / "strict-headerless-output").rglob("*.dcm")))

            compatible_args = self.arguments(
                root,
                "compatible-output",
                processes=2,
                backend="process",
                headerless_policy="allow",
            )
            compatible_result = fast.run(compatible_args)
            self.assertEqual(compatible_result.summary.get("已写入"), 2)
            self.assertFalse(set(compatible_result.summary) & fast.FAILED_STATUSES)
            self.assertEqual(compatible_result.processed_files, 2)
            issue_rows = [
                row
                for row in compatible_result.audits
                if row.get("源文件格式问题")
            ]
            self.assertEqual(len(issue_rows), 1)
            self.assertEqual(
                issue_rows[0]["源文件格式"], fast.HEADERLESS_SOURCE_FORMAT
            )
            self.assertEqual(issue_rows[0]["状态"], "已写入")
            self.assertTrue(
                compatible_result.source_format_issue_path
                and compatible_result.source_format_issue_path.is_file()
            )
            with compatible_result.source_format_issue_path.open(
                "r", newline="", encoding="utf-8-sig"
            ) as stream:
                persisted_issues = list(csv.DictReader(stream))
            self.assertEqual(len(persisted_issues), 1)
            self.assertEqual(persisted_issues[0]["无文件头策略"], "allow")
            with compatible_result.verification_path.open(
                "r", newline="", encoding="utf-8-sig"
            ) as stream:
                self.assertEqual(list(csv.DictReader(stream)), [])

            headerless_target = (
                root
                / "compatible-output"
                / "001"
                / "001-6M"
                / study_uid
                / headerless_series_uid
                / f"{headerless_sop_uid}.dcm"
            )
            anonymized = pydicom.dcmread(headerless_target, force=False)
            self.assertTrue(anonymized.preamble == b"\x00" * 128)
            self.assertEqual(
                str(anonymized.file_meta.MediaStorageSOPInstanceUID),
                headerless_sop_uid,
            )
            self.assertEqual(str(anonymized.SOPInstanceUID), headerless_sop_uid)
            self.assertEqual(str(anonymized.PatientID), "PID-KEEP-FAST")
            self.assertEqual(bytes(anonymized.PixelData), headerless_pixels)
            self.assertNotIn("PatientName", anonymized)
            self.assertNotIn("PatientBirthDate", anonymized)
            self.assertNotIn("InstitutionName", anonymized)
            self.assertNotIn("StationName", anonymized)

            target_hashes = {
                str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (root / "compatible-output").rglob("*.dcm")
            }
            compatible_result.checkpoint_path.unlink()
            duplicate_result = fast.run(compatible_args)
            self.assertEqual(duplicate_result.summary.get("重复相同"), 2)
            self.assertNotIn("UID冲突", duplicate_result.summary)
            for path, digest in target_hashes.items():
                self.assertEqual(
                    hashlib.sha256(Path(path).read_bytes()).hexdigest(), digest
                )

    def test_allow_headerless_reuses_v1_0_1_checkpoint_for_standard_file(self) -> None:
        import anonymize_enrolled_studies_fast as fast

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary_directory:
            root = Path(temporary_directory)
            study_uid = "1.2.826.0.1.3680043.8.498.5501"
            series_uid = "1.2.826.0.1.3680043.8.498.5502"
            sop_uid = "1.2.826.0.1.3680043.8.498.5503"
            self.make_dicom(
                root / "source" / "01_H001" / study_uid / series_uid / f"{sop_uid}.dcm",
                study_uid,
                series_uid,
                sop_uid,
            )
            self.make_list(root / "study-list.xlsx", study_uid)
            args = self.arguments(
                root,
                "allow-old-checkpoint-output",
                processes=1,
                headerless_policy="allow",
            )
            first = fast.run(args)
            self.assertEqual(first.summary.get("已写入"), 1)
            checkpoint_rows = [
                json.loads(line)
                for line in first.checkpoint_path.read_text(
                    encoding="utf-8"
                ).splitlines()
                if line.strip()
            ]
            legacy_context = fast._previous_checkpoint_context(
                ("测试中心医院", "source", "测试中心")
            )
            for row in checkpoint_rows:
                row["context"] = legacy_context
                row.pop("source_format", None)
                row.pop("source_format_issue", None)
                row.pop("source_uid_issues", None)
            first.checkpoint_path.write_text(
                "".join(
                    json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                    for row in checkpoint_rows
                ),
                encoding="utf-8",
            )

            resumed = fast.run(args)
            self.assertEqual(resumed.summary.get("断点跳过"), 1)
            self.assertEqual(resumed.resumed_files, 1)
            self.assertEqual(resumed.processed_files, 0)

    def test_headerless_path_mismatch_blocks_complete_study(self) -> None:
        import anonymize_enrolled_studies_fast as fast

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary_directory:
            root = Path(temporary_directory)
            study_uid = "1.2.826.0.1.3680043.8.498.5601"
            series_uid = "1.2.826.0.1.3680043.8.498.5602"
            sop_uid = "1.2.826.0.1.3680043.8.498.5603"
            self.make_headerless_dicom(
                root
                / "source"
                / "01_H001"
                / study_uid
                / "wrong-series"
                / "wrong-sop.dcm",
                study_uid,
                series_uid,
                sop_uid,
            )
            self.make_list(root / "study-list.xlsx", study_uid)

            result = fast.run(
                self.arguments(
                    root,
                    "headerless-path-mismatch-output",
                    processes=1,
                    headerless_policy="allow",
                )
            )
            self.assertEqual(result.summary.get("无文件头兼容校验失败"), 1)
            self.assertNotIn("已写入", result.summary)
            self.assertEqual(result.processed_files, 0)
            self.assertFalse(
                any((root / "headerless-path-mismatch-output").rglob("*.dcm"))
            )

    def test_preserve_policy_keeps_path_safe_nonstandard_uids_and_reports_them(self) -> None:
        import pydicom
        import anonymize_enrolled_studies_fast as fast

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary_directory:
            root = Path(temporary_directory)
            study_uid = "1.2.826.0.1.3680043.8.498.6101"
            series_uid = "1.2.826..6201"
            sop_uid = "1." + "2" * 63
            file_meta_sop_uid = "1.2.826.0.1.3680043.8.498.6301"
            source = (
                root / "source" / "01_H001" / study_uid / "source-series" / "input.dcm"
            )
            normal_meta_series_uid = "1.2.826..6202"
            normal_meta_sop_uid = "1.2.826.0.1.3680043.8.498.6302"
            normal_meta_source = (
                root
                / "source"
                / "01_H001"
                / study_uid
                / "source-series-2"
                / "input.dcm"
            )
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                self.make_dicom(
                    source,
                    study_uid,
                    series_uid,
                    sop_uid,
                    file_meta_sop_uid=file_meta_sop_uid,
                )
                self.make_dicom(
                    normal_meta_source,
                    study_uid,
                    normal_meta_series_uid,
                    normal_meta_sop_uid,
                )
            self.make_list(root / "study-list.xlsx", study_uid)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                source_dataset = pydicom.dcmread(source)
            source_uids = self.uid_snapshot(source_dataset)
            source_pixel_hash = hashlib.sha256(source_dataset.PixelData).hexdigest()

            preserve_args = self.arguments(
                root,
                "preserve-output",
                processes=2,
                backend="process",
                uid_policy="preserve",
            )
            result = fast.run(preserve_args)

            self.assertEqual(result.summary.get("已写入"), 2)
            self.assertFalse(set(result.summary) & fast.FAILED_STATUSES)
            thread_result = fast.run(
                self.arguments(
                    root,
                    "preserve-thread-output",
                    processes=2,
                    backend="thread",
                    uid_policy="preserve",
                )
            )
            self.assertEqual(thread_result.summary.get("已写入"), 2)
            self.assertFalse(set(thread_result.summary) & fast.FAILED_STATUSES)
            target = (
                root
                / "preserve-output"
                / "001"
                / "001-6M"
                / study_uid
                / series_uid
                / f"{sop_uid}.dcm"
            )
            self.assertTrue(target.is_file())
            normal_meta_target = (
                root
                / "preserve-output"
                / "001"
                / "001-6M"
                / study_uid
                / normal_meta_series_uid
                / f"{normal_meta_sop_uid}.dcm"
            )
            self.assertTrue(normal_meta_target.is_file())
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                normal_meta_result = pydicom.dcmread(normal_meta_target)
            self.assertEqual(normal_meta_result.SeriesInstanceUID, normal_meta_series_uid)
            self.assertEqual(
                normal_meta_result.file_meta.MediaStorageSOPInstanceUID,
                normal_meta_result.SOPInstanceUID,
            )
            thread_normal_meta_target = (
                root
                / "preserve-thread-output"
                / "001"
                / "001-6M"
                / study_uid
                / normal_meta_series_uid
                / f"{normal_meta_sop_uid}.dcm"
            )
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                thread_normal_meta_result = pydicom.dcmread(
                    thread_normal_meta_target
                )
            self.assertEqual(
                self.uid_snapshot(thread_normal_meta_result),
                self.uid_snapshot(normal_meta_result),
            )
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                anonymized = pydicom.dcmread(target)
            self.assertEqual(self.uid_snapshot(anonymized), source_uids)
            self.assertEqual(anonymized.StudyInstanceUID, study_uid)
            self.assertEqual(anonymized.SeriesInstanceUID, series_uid)
            self.assertEqual(anonymized.SOPInstanceUID, sop_uid)
            self.assertEqual(
                anonymized.file_meta.MediaStorageSOPInstanceUID,
                file_meta_sop_uid,
            )
            self.assertNotEqual(
                anonymized.file_meta.MediaStorageSOPInstanceUID,
                anonymized.SOPInstanceUID,
            )
            self.assertEqual(
                hashlib.sha256(anonymized.PixelData).hexdigest(),
                source_pixel_hash,
            )
            self.assertNotIn("PatientName", anonymized)
            issue_rows = [row for row in result.audits if row.get("源UID问题")]
            self.assertEqual(len(issue_rows), 2)
            mismatch_row = next(
                row for row in issue_rows if row["SOPInstanceUID"] == sop_uid
            )
            self.assertEqual(mismatch_row["状态"], "已写入")
            self.assertEqual(mismatch_row["验证结果"], "通过")
            self.assertEqual(mismatch_row["UID处理策略"], "preserve")
            self.assertIn("长度65超过64", mismatch_row["源UID问题"])
            self.assertIn("包含连续小数点", mismatch_row["源UID问题"])
            self.assertIn(
                "MediaStorageSOPInstanceUID与SOPInstanceUID不一致",
                mismatch_row["源UID问题"],
            )
            self.assertTrue(result.uid_issue_path and result.uid_issue_path.is_file())
            with result.uid_issue_path.open(
                "r", newline="", encoding="utf-8-sig"
            ) as stream:
                persisted_issue_rows = list(csv.DictReader(stream))
            self.assertEqual(len(persisted_issue_rows), 2)
            self.assertEqual({row["状态"] for row in persisted_issue_rows}, {"已写入"})
            self.assertEqual(
                {row["UID处理策略"] for row in persisted_issue_rows},
                {"preserve"},
            )
            with result.verification_path.open(
                "r", newline="", encoding="utf-8-sig"
            ) as stream:
                self.assertEqual(list(csv.DictReader(stream)), [])

            resumed = fast.run(preserve_args)
            self.assertEqual(resumed.summary.get("断点跳过"), 2)
            self.assertEqual(resumed.resumed_files, 2)
            self.assertEqual(resumed.processed_files, 0)
            resumed_issues = [
                row for row in resumed.audits if row.get("源UID问题")
            ]
            self.assertEqual(len(resumed_issues), 2)
            self.assertTrue(
                any(
                    "MediaStorageSOPInstanceUID与SOPInstanceUID不一致"
                    in row["源UID问题"]
                    for row in resumed_issues
                )
            )

    def test_preserve_policy_still_blocks_path_unsafe_uid(self) -> None:
        import anonymize_enrolled_studies_fast as fast

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary_directory:
            root = Path(temporary_directory)
            study_uid = "1.2.826.0.1.3680043.8.498.7101"
            series_uid = "1.2.826/unsafe"
            sop_uid = "1.2.826.0.1.3680043.8.498.7301"
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                self.make_dicom(
                    root / "source" / "01_H001" / study_uid / "source-series" / "input.dcm",
                    study_uid,
                    series_uid,
                    sop_uid,
                )
            self.make_list(root / "study-list.xlsx", study_uid)

            result = fast.run(
                self.arguments(
                    root,
                    "unsafe-output",
                    processes=1,
                    uid_policy="preserve",
                )
            )

            self.assertEqual(result.summary.get("UID路径不可用"), 1)
            self.assertNotIn("已写入", result.summary)
            self.assertEqual(result.processed_files, 0)
            self.assertFalse(any((root / "unsafe-output").rglob("*.dcm")))

    def test_uid_policy_defaults_to_strict_and_old_namespace_is_compatible(self) -> None:
        import anonymize_enrolled_studies_fast as fast

        parser_args = fast.build_parser().parse_args(
            [
                "--source-center",
                "source",
                "--study-list",
                "study-list.xlsx",
                "--output-root",
                "output",
            ]
        )
        self.assertEqual(parser_args.uid_policy, "strict")
        self.assertEqual(parser_args.headerless_policy, "strict")
        self.assertEqual(parser_args.confirmed_multi_patient_study, [])
        confirmed_parser_args = fast.build_parser().parse_args(
            [
                "--source-center",
                "source",
                "--study-list",
                "study-list.xlsx",
                "--output-root",
                "output",
                "--confirmed-multi-patient-study",
                "1.2.3.4",
                "--confirmed-multi-patient-study",
                "1.2.3.5",
            ]
        )
        self.assertEqual(
            confirmed_parser_args.confirmed_multi_patient_study,
            ["1.2.3.4", "1.2.3.5"],
        )
        compatible_parser_args = fast.build_parser(
            default_headerless_policy="allow"
        ).parse_args(
            [
                "--source-center",
                "source",
                "--study-list",
                "study-list.xlsx",
                "--output-root",
                "output",
            ]
        )
        self.assertEqual(compatible_parser_args.headerless_policy, "allow")

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary_directory:
            root = Path(temporary_directory)
            study_uid = "1.2.826.0.1.3680043.8.498.8101"
            series_uid = "1.2.826..8201"
            sop_uid = "1.2.826.0.1.3680043.8.498.8301"
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                self.make_dicom(
                    root / "source" / "01_H001" / study_uid / "source-series" / "input.dcm",
                    study_uid,
                    series_uid,
                    sop_uid,
                )
            self.make_list(root / "study-list.xlsx", study_uid)
            legacy_args = self.arguments(root, "strict-output", processes=1)
            delattr(legacy_args, "uid_policy")

            result = fast.run(legacy_args)

            self.assertEqual(result.summary.get("UID校验失败"), 1)
            self.assertNotIn("已写入", result.summary)
            self.assertEqual(result.processed_files, 0)
            self.assertFalse(any((root / "strict-output").rglob("*.dcm")))

    def test_linux_and_windows_launchers_reuse_fast_main(self) -> None:
        import anonymize_enrolled_studies_fast as shared
        import anonymize_enrolled_studies_fast_headerless_linux as headerless_linux
        import anonymize_enrolled_studies_fast_headerless_windows as headerless_windows
        import anonymize_enrolled_studies_fast_linux as linux
        import anonymize_enrolled_studies_fast_windows as windows

        self.assertIs(linux.main, shared.main)
        self.assertIs(windows.main, shared.main)
        self.assertIs(headerless_linux.main, shared.main)
        self.assertIs(headerless_windows.main, shared.main)


if __name__ == "__main__":
    unittest.main()
