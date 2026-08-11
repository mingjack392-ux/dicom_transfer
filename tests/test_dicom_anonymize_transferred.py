import argparse
import csv
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

import pydicom
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dicom_anonymize_transferred import (
    PatientProfile,
    WORKBOOK_DELETE_TAGS,
    anonymize_dataset,
    assign_code,
    load_mapping,
    run,
)


def make_dicom(
    path: Path,
    *,
    patient_name: str = "张三",
    patient_id: str = "PID-0001",
    birth_date: str = "19800102",
    sex: str = "M",
    age: str = "046Y",
    study_date: str = "20260807",
) -> bytes:
    sop_uid = generate_uid()
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = CTImageStorage
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    dataset = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    dataset.SOPClassUID = CTImageStorage
    dataset.SOPInstanceUID = sop_uid
    dataset.SpecificCharacterSet = "ISO_IR 192"
    dataset.StudyInstanceUID = generate_uid()
    dataset.SeriesInstanceUID = generate_uid()
    dataset.PatientName = patient_name
    dataset.PatientID = patient_id
    dataset.PatientBirthDate = birth_date
    dataset.PatientSex = sex
    dataset.PatientAge = age
    dataset.InstitutionName = "测试医院"
    dataset.Modality = "CT"
    dataset.StudyDate = study_date
    dataset.StudyTime = "101112.345600"
    dataset.SeriesTime = "101113.99"
    dataset.AcquisitionTime = "101114.1"
    dataset.ContentTime = "101115.000001"
    dataset.InstitutionAddress = "测试路 1 号"
    dataset.InstitutionalDepartmentName = "影像科"
    dataset.ReferringPhysicianName = "李医生"
    request_item = Dataset()
    request_item.PatientName = "嵌套姓名"
    request_item.PatientID = "NESTED-ID"
    dataset.RequestAttributesSequence = Sequence([request_item])
    nested_item = Dataset()
    nested_item.PatientName = "嵌套姓名"
    nested_item.PatientID = "NESTED-ID"
    dataset.ReferencedStudySequence = Sequence([nested_item])
    dataset.add_new((0x0018, 0x2042), "UI", generate_uid())
    dataset.add_new((0x0062, 0x0021), "UI", generate_uid())
    dataset.add_new((0x2030, 0x0020), "LO", "free text")
    dataset.add_new((0x7005, 0x1063), "LO", "delete private")
    dataset.add_new((0x7005, 0x1070), "LO", "retain private")
    dataset.Rows = 2
    dataset.Columns = 2
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    dataset.BitsAllocated = 8
    dataset.BitsStored = 8
    dataset.HighBit = 7
    dataset.PixelRepresentation = 0
    dataset.PixelData = b"\x01\x02\x03\x04"
    path.parent.mkdir(parents=True, exist_ok=True)
    dataset.save_as(path, enforce_file_format=True)
    return dataset.PixelData


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RuleTests(unittest.TestCase):
    def test_workbook_delete_tag_count(self):
        self.assertEqual(212, len(WORKBOOK_DELETE_TAGS))

    def test_recursive_rules_and_explicit_retention(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "source.dcm"
            pixel_data = make_dicom(path)
            dataset = pydicom.dcmread(path)
            original_uids = (
                dataset.StudyInstanceUID,
                dataset.SeriesInstanceUID,
                dataset.SOPInstanceUID,
            )

            counts = anonymize_dataset(dataset, "E000123")

            self.assertEqual("E000123", str(dataset.PatientName))
            self.assertEqual("PID-0001", dataset.PatientID)
            self.assertEqual("测试医院", dataset.InstitutionName)
            self.assertEqual("CT", dataset.Modality)
            self.assertEqual("20260807", dataset.StudyDate)
            self.assertEqual(original_uids, (
                dataset.StudyInstanceUID,
                dataset.SeriesInstanceUID,
                dataset.SOPInstanceUID,
            ))
            self.assertEqual("101112", dataset.StudyTime)
            self.assertEqual("101113", dataset.SeriesTime)
            self.assertEqual("101114", dataset.AcquisitionTime)
            self.assertEqual("101115", dataset.ContentTime)
            self.assertEqual("", dataset.InstitutionAddress)
            self.assertEqual("", dataset.InstitutionalDepartmentName)
            self.assertNotIn((0x0008, 0x0090), dataset)
            self.assertNotIn((0x0040, 0x0275), dataset)
            self.assertNotIn((0x0018, 0x2042), dataset)
            self.assertNotIn((0x0062, 0x0021), dataset)
            self.assertNotIn((0x2030, 0x0020), dataset)
            self.assertNotIn((0x7005, 0x1063), dataset)
            self.assertEqual("retain private", dataset[(0x7005, 0x1070)].value)
            self.assertEqual("E000123", str(dataset.ReferencedStudySequence[0].PatientName))
            self.assertEqual("NESTED-ID", dataset.ReferencedStudySequence[0].PatientID)
            self.assertEqual(pixel_data, dataset.PixelData)
            self.assertGreaterEqual(counts.deleted_elements, 6)


class IdentityResolutionTests(unittest.TestCase):
    def profile(
        self,
        patient_id: str,
        name: str,
        birth_date: str = "19800102",
        sex: str = "M",
        age: str = "046Y",
        study_date: str = "20260807",
    ) -> PatientProfile:
        return PatientProfile(patient_id, name, birth_date, sex, age, study_date)

    def test_same_name_and_patient_id_is_same_person(self):
        rows = []
        first = assign_code(rows, "患者A_100", self.profile("100", "患者A"))
        second = assign_code(
            rows,
            "患者A_100_复查",
            self.profile("100", "患者A", birth_date="19700101", sex="F"),
        )
        self.assertEqual(first.code, second.code)
        self.assertEqual("exact_confirmed", second.status)

    def test_same_name_different_ids_merge_with_demographic_evidence(self):
        rows = []
        first = assign_code(rows, "李玄六_1007437", self.profile("1007437", "李玄六"))
        second = assign_code(
            rows,
            "李玄六_04568338",
            self.profile("04568338", "李玄六", age="047Y", study_date="20270807"),
        )
        self.assertEqual(first.code, second.code)
        self.assertEqual("demographic_confirmed", second.status)
        self.assertEqual("04568338|1007437", rows[0]["all_patient_ids"])
        self.assertEqual("04568338|1007437", rows[1]["all_patient_ids"])

    def test_same_name_different_ids_do_not_merge_when_birth_date_conflicts(self):
        rows = []
        first = assign_code(rows, "同名_100", self.profile("100", "同名"))
        second = assign_code(
            rows,
            "同名_200",
            self.profile("200", "同名", birth_date="19990101", age="027Y"),
        )
        self.assertNotEqual(first.code, second.code)
        self.assertEqual("new_identity", second.status)
        self.assertIn("demographic_conflict", second.basis)

    def test_different_names_same_id_merge_with_demographic_evidence(self):
        rows = []
        first = assign_code(rows, "旧姓名_100", self.profile("100", "旧姓名"))
        second = assign_code(rows, "新姓名_100", self.profile("100", "新姓名"))
        self.assertEqual(first.code, second.code)
        self.assertEqual("demographic_confirmed", second.status)

    def test_insufficient_demographics_are_kept_separate_and_marked_for_review(self):
        rows = []
        first = assign_code(
            rows,
            "同名_100",
            self.profile("100", "同名", birth_date="", age="", study_date=""),
        )
        second = assign_code(
            rows,
            "同名_200",
            self.profile("200", "同名", birth_date="", age="", study_date=""),
        )
        self.assertNotEqual(first.code, second.code)
        self.assertEqual("needs_review", second.status)

    def test_old_four_column_mapping_is_upgraded(self):
        with tempfile.TemporaryDirectory() as temp:
            mapping = Path(temp) / "old.csv"
            mapping.write_text(
                "source_patient_folder,PatientID,original_patient_name,anonymous_code\n"
                "患者A_100,100,患者A,E000001\n",
                encoding="utf-8-sig",
            )
            rows = load_mapping(mapping)
            self.assertEqual("100", rows[0]["PatientID"])
            self.assertIn("PatientBirthDate", rows[0])
            self.assertEqual("", rows[0]["PatientBirthDate"])


class DirectoryRunTests(unittest.TestCase):
    def test_dry_run_then_execute_preserves_source_and_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_root = root / "transferred"
            patient_root = input_root / "张三_PID-0001"
            relative = Path("1.2.3") / "1.2.3.4" / "1.2.3.4.5.dcm"
            source = patient_root / relative
            pixel_data = make_dicom(source)
            (patient_root / "note.txt").write_text("not dicom", encoding="utf-8")
            source_hash = file_hash(source)
            output_root = root / "anonymous"
            mapping = root / "mapping_sensitive.csv"
            audit = root / "audit_sensitive.csv"

            args = argparse.Namespace(
                input=input_root,
                output=output_root,
                mapping=mapping,
                audit=audit,
                single_patient=False,
                execute=False,
                overwrite=False,
            )
            preview = run(args)
            self.assertEqual(1, preview.planned)
            self.assertFalse(output_root.exists())
            self.assertFalse(mapping.exists())
            self.assertFalse(audit.exists())

            args.execute = True
            result = run(args)
            target = output_root / "E000001" / relative
            self.assertEqual(1, result.written)
            self.assertTrue(target.exists())
            self.assertEqual(source_hash, file_hash(source))
            output = pydicom.dcmread(target)
            self.assertEqual("E000001", str(output.PatientName))
            self.assertEqual("PID-0001", output.PatientID)
            self.assertEqual("测试医院", output.InstitutionName)
            self.assertEqual("CT", output.Modality)
            self.assertEqual(pixel_data, output.PixelData)
            self.assertFalse((output_root / "E000001" / "note.txt").exists())

            with mapping.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(1, len(rows))
            self.assertEqual("E000001", rows[0]["anonymous_code"])
            self.assertEqual("PID-0001", rows[0]["PatientID"])

            second = run(args)
            self.assertEqual(1, second.skipped_existing)
            self.assertEqual(0, second.conflicts)

    def test_batch_same_person_with_two_patient_ids_uses_one_e_code(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_root = root / "batch"
            first_relative = Path("1.2.3.1") / "1.2.3.1.1" / "a.dcm"
            second_relative = Path("1.2.3.2") / "1.2.3.2.1" / "b.dcm"
            make_dicom(
                input_root / "李玄六_1007437" / first_relative,
                patient_name="李玄六",
                patient_id="1007437",
            )
            make_dicom(
                input_root / "李玄六_04568338" / second_relative,
                patient_name="李玄六",
                patient_id="04568338",
                age="047Y",
                study_date="20270807",
            )
            output_root = root / "anonymous"
            mapping = root / "mapping.csv"
            audit = root / "audit.csv"
            args = argparse.Namespace(
                input=input_root,
                output=output_root,
                mapping=mapping,
                audit=audit,
                single_patient=False,
                execute=True,
                overwrite=False,
            )

            result = run(args)

            self.assertEqual(2, result.written)
            self.assertEqual(1, result.demographic_merges)
            self.assertTrue((output_root / "E000001" / first_relative).exists())
            self.assertTrue((output_root / "E000001" / second_relative).exists())
            with mapping.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual({"1007437", "04568338"}, {row["PatientID"] for row in rows})
            self.assertEqual({"E000001"}, {row["anonymous_code"] for row in rows})
            self.assertEqual({"04568338|1007437"}, {row["all_patient_ids"] for row in rows})


if __name__ == "__main__":
    unittest.main()
