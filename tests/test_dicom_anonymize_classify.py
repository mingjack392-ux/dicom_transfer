import sys
import unittest
from pathlib import Path

from pydicom.dataset import Dataset

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import dicom_anonymize_classify as anonymizer


def make_meta(patient_id: str = "TRACE-001") -> anonymizer.DicomMeta:
    return anonymizer.DicomMeta(
        path=Path("input.dcm"),
        source_subject="subject",
        source_container="container",
        source_folder_name="CT",
        source_image_type="CT",
        patient_name="Original Name",
        patient_id=patient_id,
        issuer="Hospital",
        birth_date="19800101",
        study_uid="1.2.826.0.1.3680043.10.100.1",
        series_uid="1.2.826.0.1.3680043.10.100.2",
        sop_uid="1.2.826.0.1.3680043.10.100.3",
        study_date="20260101",
        series_date="20260101",
        acquisition_date="20260101",
        content_date="20260101",
    )


class DicomAnonymizationTests(unittest.TestCase):
    def test_patient_id_is_retained_while_patient_name_is_replaced(self):
        dataset = Dataset()
        dataset.PatientName = "Original^Name"
        dataset.PatientID = "TRACE-001"
        dataset.IssuerOfPatientID = "Hospital"
        dataset.InstitutionName = "Original Name Hospital TRACE-001"
        dataset.StudyDate = "20260101"
        dataset.StudyInstanceUID = "1.2.826.0.1.3680043.10.100.1"
        dataset.TargetUID = "1.2.826.0.1.3680043.10.100.4"
        dataset.TrackingUID = "2.25.1234"
        dataset.TextString = "identity text"
        dataset.ManufacturerModelName = "TRACE-001 scanner"
        dataset.add_new((0x0011, 0x1010), "LO", "private patient data")

        item = Dataset()
        item.PatientName = "Original^Name"
        item.PatientID = "NESTED-TRACE-002"
        item.TargetUID = "1.2.826.0.1.3680043.10.100.5"
        item.TrackingUID = "2.25.5678"
        item.TextString = "nested identity text"
        dataset.ReferencedPatientSequence = [item]

        anonymizer.anonymize_dataset(
            dataset,
            make_meta(),
            anonymous_id="ANON000001",
            salt="test-salt",
            shift_days=10,
        )

        self.assertEqual(str(dataset.PatientName), "ANON000001")
        self.assertEqual(dataset.PatientID, "TRACE-001")
        self.assertEqual(
            dataset.ReferencedPatientSequence[0].PatientID,
            "NESTED-TRACE-002",
        )
        self.assertEqual(
            str(dataset.ReferencedPatientSequence[0].PatientName),
            "ANON000001",
        )
        self.assertEqual(dataset.IssuerOfPatientID, "")
        self.assertEqual(
            dataset.InstitutionName,
            "Original Name Hospital TRACE-001",
        )
        for keyword in ("TargetUID", "TrackingUID", "TextString"):
            self.assertNotIn(keyword, dataset)
            self.assertNotIn(keyword, dataset.ReferencedPatientSequence[0])
        self.assertEqual(dataset.ManufacturerModelName, "ANON000001 scanner")
        self.assertNotIn((0x0011, 0x1010), dataset)
        self.assertEqual(dataset.PatientIdentityRemoved, "YES")
        self.assertIn("PatientID retained", dataset.DeidentificationMethod)
        self.assertLessEqual(len(dataset.DeidentificationMethod), 64)

    def test_missing_patient_id_is_not_added(self):
        dataset = Dataset()
        dataset.PatientName = "Original^Name"

        anonymizer.anonymize_dataset(
            dataset,
            make_meta(patient_id=""),
            anonymous_id="ANON000001",
            salt="test-salt",
            shift_days=0,
        )

        self.assertNotIn("PatientID", dataset)
        self.assertEqual(str(dataset.PatientName), "ANON000001")


if __name__ == "__main__":
    unittest.main()
