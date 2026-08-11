import tempfile
import sys
import unittest
from pathlib import Path

import pydicom
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import scan_dicom_patients as scanner


def write_test_dicom(path: Path, patient_id: str, patient_name: str, uid: int) -> None:
    file_meta = FileMetaDataset()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    file_meta.MediaStorageSOPInstanceUID = f"1.2.826.0.1.3680043.10.999.{uid}"
    dataset = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    dataset.SpecificCharacterSet = "ISO_IR 192"
    dataset.PatientID = patient_id
    dataset.PatientName = patient_name
    dataset.SOPClassUID = SecondaryCaptureImageStorage
    dataset.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
    dataset.StudyInstanceUID = "1.2.826.0.1.3680043.10.999.100"
    dataset.SeriesInstanceUID = "1.2.826.0.1.3680043.10.999.200"
    pydicom.dcmwrite(str(path), dataset, enforce_file_format=True)


class ScanDicomPatientsTests(unittest.TestCase):
    def test_counts_by_patient_id_and_falls_back_to_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_test_dicom(root / "one.dcm", "P001", "ZHANG^SAN", 1)
            write_test_dicom(root / "two.dcm", "P001", "\u5f20\u4e09", 2)
            write_test_dicom(
                root / "uuid.dcm",
                "P001!12345678-1234-1234-1234-123456789abc",
                "\u5f20\u4e09",
                3,
            )
            write_test_dicom(root / "no_id.dcm", "", "\u674e\u56db", 4)
            (root / "notes.txt").write_text("not dicom", encoding="utf-8")

            report = scanner.scan_source(
                root,
                workers=2,
                batch_size=2,
                progress_every=0,
            )

            self.assertEqual(report.total_files, 5)
            self.assertEqual(report.dicom_files, 4)
            self.assertEqual(report.non_dicom_files, 1)
            self.assertEqual(report.read_errors, 0)
            self.assertEqual(len(report.patients), 2)
            self.assertEqual(report.patients["ID:P001"].dicom_files, 3)
            self.assertEqual(
                report.patients["NAME:\u674e\u56db"].preferred_name,
                "\u674e\u56db",
            )


if __name__ == "__main__":
    unittest.main()
