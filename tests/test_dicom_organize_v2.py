import tempfile
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pydicom
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.uid import (
    ExplicitVRLittleEndian,
    XRay3DAngiographicImageStorage,
    XRayAngiographicImageStorage,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import dicom_organize_v2 as v2


def write_dicom(
    path: Path,
    *,
    study_uid: str = "1.2.826.0.1.3680043.10.100.1",
    series_uid: str = "1.2.826.0.1.3680043.10.100.2",
    sop_uid: str = "1.2.826.0.1.3680043.10.100.3",
    modality: str = "XA",
    study_date: str = "20260804",
    description: str = "DSA",
    motion: str = "STATIC",
    frames: int = 20,
    slice_thickness: str = "",
    with_preamble: bool = True,
    missing_uids: bool = False,
    patient_name: str = "Test^Patient",
    patient_id: str = "P001",
) -> None:
    values = {
        "PatientName": patient_name,
        "PatientID": patient_id,
        "Modality": modality,
        "StudyDate": study_date,
        "SeriesDescription": description,
        "NumberOfFrames": str(frames),
        "PositionerMotion": motion,
    }
    if slice_thickness:
        values["SliceThickness"] = slice_thickness
    if not missing_uids:
        values.update(
            {
                "StudyInstanceUID": study_uid,
                "SeriesInstanceUID": series_uid,
                "SOPInstanceUID": sop_uid,
                "SOPClassUID": XRayAngiographicImageStorage,
            }
        )

    if with_preamble:
        file_meta = FileMetaDataset()
        file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        file_meta.MediaStorageSOPClassUID = XRayAngiographicImageStorage
        file_meta.MediaStorageSOPInstanceUID = sop_uid
        file_meta.ImplementationClassUID = "1.2.826.0.1.3680043.10.100.999"
        dataset = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
        for keyword, value in values.items():
            setattr(dataset, keyword, value)
        dataset.save_as(str(path), enforce_file_format=True)
    else:
        dataset = Dataset()
        for keyword, value in values.items():
            setattr(dataset, keyword, value)
        pydicom.dcmwrite(
            str(path), dataset, implicit_vr=True, little_endian=True,
            enforce_file_format=False,
        )


class V2TransferTests(unittest.TestCase):
    def test_patient_id_uuid_suffix_is_removed_only_for_full_uuid(self):
        self.assertEqual(
            v2.normalize_patient_id(
                "9000001!4e38099c-9e4a-4204-bade-7c252448cc06"
            ),
            "9000001",
        )
        self.assertEqual(v2.normalize_patient_id("A001!manual"), "A001!manual")

    def test_chinese_source_root_overrides_pinyin_dicom_name(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "张小明"
            source.mkdir()
            path = source / "image.dcm"
            write_dicom(
                path, patient_name="ZHANG XIAOMING", patient_id="SYNTH_PID_001"
            )

            scan = v2.scan_one_file(path, source)

            self.assertEqual(scan.meta.patient_name, "张小明")
            self.assertEqual(v2.patient_folder(scan.meta), "张小明_SYNTH_PID_001")

    def test_batch_root_uses_patient_subdirectory_with_patient_id(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "data"
            patient_dir = source / "张小明_SYNTH_PID_001_1"
            patient_dir.mkdir(parents=True)
            path = patient_dir / "image.dcm"
            write_dicom(
                path, patient_name="ZHANG XIAOMING", patient_id="SYNTH_PID_001"
            )

            scan = v2.scan_one_file(path, source)

            self.assertEqual(scan.meta.patient_name, "张小明")
            self.assertEqual(v2.patient_folder(scan.meta), "张小明_SYNTH_PID_001")

    def test_scans_dataset_without_dicom_prefix(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "no_preamble"
            write_dicom(path, with_preamble=False)
            self.assertFalse(v2.has_dicom_prefix(path))
            scan = v2.scan_one_file(path, root)
            self.assertTrue(scan.is_dicom)
            self.assertEqual(scan.meta.patient_id, "P001")
            self.assertEqual(scan.meta.exam_date, "2026-08-04")

    def test_duplicate_is_not_overwritten_and_conflict_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            output.mkdir()
            first = source / "first.dcm"
            second = source / "second.dcm"
            write_dicom(first, description="DSA A")
            write_dicom(second, description="DSA B")

            first_result = v2.transfer_one(v2.scan_one_file(first, source), output)
            duplicate_result = v2.transfer_one(v2.scan_one_file(first, source), output)
            conflict_result = v2.transfer_one(v2.scan_one_file(second, source), output)

            self.assertEqual(first_result.status, "copied")
            self.assertEqual(duplicate_result.status, "duplicate_same")
            self.assertEqual(conflict_result.status, "conflict")
            self.assertTrue(Path(first_result.destination_path).exists())
            self.assertTrue(Path(conflict_result.destination_path).exists())
            self.assertNotEqual(first_result.sha256, conflict_result.sha256)

    def test_parallel_copy_preserves_same_uid_conflict(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            output.mkdir()
            first = source / "first.dcm"
            second = source / "second.dcm"
            write_dicom(first, description="DSA A")
            write_dicom(second, description="DSA B")
            scans = [
                v2.scan_one_file(first, source),
                v2.scan_one_file(second, source),
            ]

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(
                    executor.map(lambda scan: v2.transfer_one(scan, output), scans)
                )

            self.assertEqual({result.status for result in results}, {"copied", "conflict"})
            self.assertTrue(all(Path(result.destination_path).exists() for result in results))
            self.assertEqual(len({result.sha256 for result in results}), 2)

    def test_missing_uids_go_to_quarantine(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            output.mkdir()
            path = source / "missing.dcm"
            write_dicom(path, missing_uids=True)
            result = v2.transfer_one(v2.scan_one_file(path, source), output)
            self.assertEqual(result.status, "quarantined")
            self.assertIn("_quarantine", result.destination_path)
            self.assertTrue(Path(result.destination_path).exists())


class V2ClassificationTests(unittest.TestCase):
    def test_dynamic_multiframe_xa_without_slice_thickness_is_not_3d(self):
        series = v2.SeriesAccumulator(series_uid="series-1")
        series.add(
            v2.DicomMeta(
                modality="XA",
                positioner_motion="DYNAMIC",
                number_of_frames=120,
                sop_uid="sop-1",
            )
        )
        result = v2.classify_series(series)
        self.assertFalse(result["is_3d"])
        self.assertIn("缺少SliceThickness", " | ".join(result["evidence"]))

    def test_xray_3d_sop_without_hard_conditions_is_not_3d(self):
        series = v2.SeriesAccumulator(series_uid="series-xray-3d")
        series.add(
            v2.DicomMeta(
                modality="XA",
                sop_class_uid=str(XRay3DAngiographicImageStorage),
                series_description="DSA",
                positioner_motion="DYNAMIC",
                number_of_frames=120,
                sop_uid="sop-xray-3d",
            )
        )
        result = v2.classify_series(series)
        self.assertFalse(result["is_3d"])

    def test_xa_slice_without_recon_description_is_not_3d(self):
        series = v2.SeriesAccumulator(series_uid="series-slice-only")
        series.add(
            v2.DicomMeta(
                modality="XA",
                series_description="DSA",
                slice_thickness="0.5",
                sop_uid="sop-slice-only",
            )
        )
        result = v2.classify_series(series)
        self.assertFalse(result["is_3d"])
        self.assertIn("未命中3D/重建关键词", " | ".join(result["evidence"]))

    def test_xa_slice_and_recon_description_is_3d(self):
        series = v2.SeriesAccumulator(series_uid="series-2")
        series.add(
            v2.DicomMeta(
                modality="XA",
                series_description="3D MIP RECON",
                slice_thickness="0.5",
                sop_uid="sop-2",
            )
        )
        result = v2.classify_series(series)
        self.assertTrue(result["is_3d"])
        self.assertEqual(result["confidence"], 0.82)

    def test_static_xa_without_recon_evidence_is_not_3d(self):
        series = v2.SeriesAccumulator(series_uid="series-3")
        series.add(
            v2.DicomMeta(
                modality="XA",
                positioner_motion="STATIC",
                number_of_frames=30,
                series_description="DSA",
                sop_uid="sop-3",
            )
        )
        result = v2.classify_series(series)
        self.assertFalse(result["is_3d"])

    def test_one_study_merges_multiple_modalities_and_3d_flag(self):
        study = v2.StudyAccumulator(study_uid="study-1")
        study.add(
            v2.DicomMeta(
                patient_id="P001", patient_name="Test Patient",
                study_uid="study-1", series_uid="series-xa", sop_uid="sop-xa",
                study_date="20260804", modality="XA",
                series_description="3D DSA RECON", slice_thickness="0.5",
            ),
            "D:/out/Test_P001/study-1/series-xa/sop-xa.dcm",
        )
        study.add(
            v2.DicomMeta(
                patient_id="P001", patient_name="Test Patient",
                study_uid="study-1", series_uid="series-ct", sop_uid="sop-ct",
                study_date="20260804", modality="CT",
            ),
            "D:/out/Test_P001/study-1/series-ct/sop-ct.dcm",
        )
        study.add(
            v2.DicomMeta(
                patient_id="P001", patient_name="Test Patient",
                study_uid="study-1", series_uid="series-ot", sop_uid="sop-ot",
                study_date="20260804", modality="OT",
            ),
            "D:/out/Test_P001/study-1/series-ot/sop-ot.dcm",
        )
        row, warnings = v2.summarize_study(study, Path("D:/source"))
        self.assertEqual(row["modalities"], "XA、CT、OT")
        self.assertEqual(row["has_3d"], "有")
        self.assertEqual(
            row["display_type"], "XA（含3D_DSA断层）、CT、OT"
        )
        self.assertEqual(row["series_count"], 3)
        self.assertEqual(row["file_count"], 3)
        self.assertEqual(warnings, [])

    def test_study_date_falls_back_to_acquisition_date(self):
        meta = v2.DicomMeta(acquisition_date="20250121")
        self.assertEqual(meta.exam_date, "2025-01-21")


if __name__ == "__main__":
    unittest.main()
