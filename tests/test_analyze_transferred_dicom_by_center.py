import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pydicom
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, XRayAngiographicImageStorage


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import analyze_transferred_dicom_by_center as center_web


def center_sheet_payload(*, second_hospital: str = "H001"):
    headers = [
        "#",
        "中心名称",
        "姓名",
        "住院号/门诊号/放射号",
        "手术日期",
        "",
        "#",
        "中心名称",
        "姓名",
        "住院号/门诊号/放射号",
        "手术日期",
    ]
    values = [
        headers,
        [
            1,
            "宜昌中心医院",
            "测试患者",
            "H001",
            "2025/1/1",
            "",
            1,
            "宜昌中心医院",
            "测试患者",
            second_hospital,
            "2025/1/1",
        ],
    ]
    return [{"name": "分中心", "values": values}]


def dicom_record(
    name: str,
    sop_uid: str,
    *,
    frames=None,
    acquisition=date(2025, 1, 31),
    study_date=None,
    series_date=None,
    series_uid="1.2.3.2",
):
    return center_web.DicomRecord(
        path=Path(f"{sop_uid}.dcm"),
        relative_path=f"患者/{sop_uid}.dcm",
        patient_id="P001",
        header_patient_name="TEST PATIENT",
        patient_name=name,
        study_uid="1.2.3.1",
        series_uid=series_uid,
        sop_uid=sop_uid,
        acquisition_date=acquisition,
        acquisition_date_raw=acquisition.strftime("%Y%m%d") if acquisition else "",
        study_date=study_date,
        series_date=series_date,
        image_type="['ORIGINAL', 'PRIMARY']",
        series_description="DSA",
        slice_thickness=center_web.NA_VALUE,
        number_of_frames=frames,
        modality="XA",
        primary_angle=10.0 if frames else center_web.NA_VALUE,
        secondary_angle=2.0 if frames else center_web.NA_VALUE,
    )


class CenterWorkbookRuleTests(unittest.TestCase):
    def test_period_uses_thirty_day_month_and_two_decimal_rounding(self):
        surgery = date(2025, 1, 1)
        self.assertEqual(
            center_web.calculate_period(date(2024, 12, 31), surgery), "术前"
        )
        self.assertEqual(center_web.calculate_period(surgery, surgery), "术中")
        self.assertEqual(center_web.calculate_period(date(2025, 1, 2), surgery), 0.03)
        self.assertEqual(center_web.calculate_period(date(2025, 7, 5), surgery), 6.17)

    def test_identical_horizontal_center_blocks_are_read_once(self):
        rows, exceptions = center_web.parse_center_sheets(center_sheet_payload())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].patient_name, "测试患者")
        self.assertEqual(rows[0].hospital_number, "H001")
        self.assertEqual(rows[0].surgery_date, date(2025, 1, 1))
        self.assertEqual(exceptions, [])

    def test_different_horizontal_center_blocks_stop_processing(self):
        with self.assertRaisesRegex(ValueError, "内容不一致"):
            center_web.parse_center_sheets(
                center_sheet_payload(second_hospital="H999")
            )

    def test_blank_center_name_is_filled_down_within_same_block(self):
        payload = center_sheet_payload()
        payload[0]["values"].append(
            [
                2,
                "",
                "第二患者",
                "H002",
                "2025/2/2",
                "",
                2,
                "",
                "第二患者",
                "H002",
                "2025/2/2",
            ]
        )
        rows, _ = center_web.parse_center_sheets(payload)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1].center_name, "宜昌中心医院")

    def test_screening_number_and_initials_are_read_for_missing_name_center(self):
        payload = [
            {
                "name": "分中心",
                "values": [
                    [
                        "#",
                        "中心名称",
                        "受试者筛选号",
                        "姓名缩写",
                        "姓名",
                        "住院号/门诊号/放射号",
                        "手术日期",
                    ],
                    [
                        1,
                        "首都医科大学附属北京安贞医院",
                        "15-H001",
                        "ZFYI",
                        "",
                        "",
                        "2025/1/1",
                    ],
                ],
            }
        ]

        rows, exceptions = center_web.parse_center_sheets(payload)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].screening_number, "15-H001")
        self.assertEqual(rows[0].patient_initials, "ZFYI")
        self.assertEqual(rows[0].hospital_number, "")
        self.assertTrue(
            any(
                item["异常类型"] == "分中心表缺少姓名"
                and "受试者筛选号" in item["说明"]
                for item in exceptions
            )
        )

    def test_screening_number_normalizes_folder_separator(self):
        self.assertEqual(
            center_web.normalize_screening_number("15_H001"), "15-H001"
        )
        self.assertEqual(
            center_web.normalize_screening_number("15-H001"), "15-H001"
        )
        self.assertEqual(
            center_web.normalize_screening_number("01_Q001"), "01-Q001"
        )
        self.assertEqual(
            center_web.normalize_screening_number("01-Q008"), "01-Q008"
        )
        self.assertEqual(center_web.normalize_screening_number("01-X001"), "")


class CenterSeriesRuleTests(unittest.TestCase):
    def setUp(self):
        self.center_record = center_web.CenterRecord(
            center_name="宜昌中心医院",
            patient_name="测试患者",
            hospital_number="H001",
            surgery_date=date(2025, 1, 1),
            source_sheet="分中心",
            source_row=2,
            source_order=1,
        )
        self.index = center_web.center_record_index(
            [self.center_record], "宜昌中心医院"
        )

    def test_single_frame_series_is_one_row_and_multiframe_is_one_row_per_sop(self):
        records = [
            dicom_record("测试患者", "1.2.3.3"),
            dicom_record("测试患者", "1.2.3.4"),
            dicom_record("测试患者", "1.2.3.5", frames=20),
        ]
        rows, exceptions = center_web.summarize_series_group(
            "宜昌", records, self.index
        )
        self.assertEqual(len(rows), 2)
        single = next(row for row in rows if row["SOPUID"] == "NA")
        multi = next(row for row in rows if row["SOPUID"] == "1.2.3.5")
        self.assertEqual(single["帧数"], 2)
        self.assertEqual(single["NumberOfFrames"], "NA")
        self.assertEqual(multi["帧数"], "NA")
        self.assertEqual(multi["NumberOfFrames"], 20)
        self.assertEqual(multi["住院号"], "H001")
        self.assertEqual(multi["时期"], 1.0)
        self.assertTrue(
            any(item["异常类型"] == "同一Series混合单帧与多帧" for item in exceptions)
        )

    def test_patient_matching_is_restricted_to_reference_center(self):
        other = center_web.CenterRecord(
            center_name="其他中心",
            patient_name="同名患者",
            hospital_number="OTHER001",
            surgery_date=date(2025, 1, 1),
            source_sheet="分中心",
            source_row=3,
            source_order=1,
        )
        index = center_web.center_record_index(
            [self.center_record, other], "宜昌中心医院"
        )
        match, reason = center_web.match_center_record(index, "同名患者")
        self.assertIsNone(match)
        self.assertIn("当前中心", reason)

    def test_missing_name_matches_by_screening_number_without_filling_hospital(self):
        anzhen_record = center_web.CenterRecord(
            center_name="首都医科大学附属北京安贞医院",
            patient_name="",
            hospital_number="",
            surgery_date=date(2025, 1, 31),
            source_sheet="分中心",
            source_row=18,
            source_order=17,
            screening_number="15-H001",
            patient_initials="ZFYI",
        )
        record = dicom_record(
            "ZHANG FENGYING",
            "1.2.3.3",
            acquisition=date(2025, 1, 31),
        )
        record = center_web.DicomRecord(
            **{
                **record.__dict__,
                "relative_path": "15_H001/1.2.3.1/1.2.3.2/1.2.3.3.dcm",
                "screening_number": "15-H001",
            }
        )

        result = center_web.process_center_records(
            "安贞",
            Path("安贞"),
            [record],
            [anzhen_record],
            "首都医科大学附属北京安贞医院",
            [],
            {"files_seen": 1, "dicom": 1, "non_dicom": 0, "scan_errors": 0},
        )

        self.assertEqual(result.rows[0]["住院号"], "")
        self.assertEqual(result.rows[0]["患者"], "ZFYI")
        self.assertEqual(result.rows[0]["时期"], "术中")
        self.assertEqual(result.counters["matched_rows"], 1)
        self.assertTrue(
            any(item["异常类型"] == "编号回退匹配" for item in result.exceptions)
        )
        self.assertTrue(
            any(
                item["异常类型"] == "匹配记录缺少住院号"
                for item in result.exceptions
            )
        )

    def test_pinyin_name_matches_prospective_record_by_q_screening_number(self):
        prospective_record = center_web.CenterRecord(
            center_name="首都医科大学宣武医院",
            patient_name="测试中文姓名",
            hospital_number="1124550",
            surgery_date=date(2026, 6, 9),
            source_sheet="分中心影像",
            source_row=2,
            source_order=1,
            screening_number="01-Q001",
            patient_initials="CSZWXM",
        )
        record = dicom_record(
            "PINYIN NAME",
            "1.2.3.3",
            acquisition=date(2026, 6, 9),
        )
        record = center_web.DicomRecord(
            **{
                **record.__dict__,
                "relative_path": "01-Q001/1.2.3.1/1.2.3.2/1.2.3.3.dcm",
                "screening_number": "01-Q001",
            }
        )

        result = center_web.process_center_records(
            "首都医科大学宣武医院",
            Path("宣武"),
            [record],
            [prospective_record],
            "首都医科大学宣武医院",
            [],
            {"files_seen": 1, "dicom": 1, "non_dicom": 0, "scan_errors": 0},
        )

        self.assertEqual(result.rows[0]["住院号"], "1124550")
        self.assertEqual(result.rows[0]["患者"], "测试中文姓名")
        self.assertEqual(result.rows[0]["时期"], "术中")
        self.assertEqual(result.counters["matched_rows"], 1)
        self.assertTrue(
            any(item["异常类型"] == "编号回退匹配" for item in result.exceptions)
        )

    def test_multiple_acquisition_dates_use_earliest_and_report_exception(self):
        records = [
            dicom_record("测试患者", "1.2.3.3", acquisition=date(2025, 2, 1)),
            dicom_record("测试患者", "1.2.3.4", acquisition=date(2025, 1, 31)),
        ]
        rows, exceptions = center_web.summarize_series_group(
            "宜昌", records, self.index
        )
        self.assertEqual(rows[0]["AcqusitionDate"], 20250131)
        self.assertTrue(
            any(
                item["异常类型"] == "同一Series存在多个AcquisitionDate"
                for item in exceptions
            )
        )

    def test_repeated_sop_uid_is_counted_once_and_reported(self):
        first = dicom_record("测试患者", "1.2.3.3")
        second = dicom_record("测试患者", "1.2.3.3")
        second = center_web.DicomRecord(
            **{**second.__dict__, "path": Path("duplicate.dcm"), "relative_path": "患者/duplicate.dcm"}
        )
        rows, exceptions = center_web.summarize_series_group(
            "宜昌", [first, second], self.index
        )
        self.assertEqual(rows[0]["帧数"], 1)
        self.assertTrue(
            any(item["异常类型"] == "同一SOPUID出现多个文件" for item in exceptions)
        )

    def test_missing_acquisition_date_falls_back_to_study_date_first(self):
        records = [
            dicom_record(
                "测试患者",
                "1.2.3.3",
                acquisition=None,
                study_date=date(2025, 1, 31),
                series_date=date(2025, 2, 2),
            )
        ]
        rows, exceptions = center_web.summarize_series_group(
            "宜昌", records, self.index
        )
        self.assertEqual(rows[0]["AcqusitionDate"], 20250131)
        self.assertEqual(rows[0]["时期"], 1.0)
        self.assertTrue(
            any(
                item["异常类型"] == "日期回退" and "StudyDate" in item["说明"]
                for item in exceptions
            )
        )

    def test_series_date_is_used_when_acquisition_and_study_dates_are_missing(self):
        records = [
            dicom_record(
                "测试患者",
                "1.2.3.3",
                acquisition=None,
                study_date=None,
                series_date=date(2025, 2, 2),
            )
        ]
        rows, exceptions = center_web.summarize_series_group(
            "宜昌", records, self.index
        )
        self.assertEqual(rows[0]["AcqusitionDate"], 20250202)
        self.assertTrue(
            any(
                item["异常类型"] == "日期回退" and "SeriesDate" in item["说明"]
                for item in exceptions
            )
        )


class DicomHeaderScanTests(unittest.TestCase):
    def test_unreadable_dcm_is_reported_by_center_scan(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "broken.dcm").write_bytes(b"not dicom")
            records, exceptions, counters = center_web.scan_center(
                root, root, workers=1, center_label="宜昌"
            )
            self.assertEqual(records, [])
            self.assertEqual(counters["scan_errors"], 1)
            self.assertEqual(exceptions[0]["中心"], "宜昌")
            self.assertEqual(exceptions[0]["异常类型"], "DICOM读取失败")

    def test_known_report_and_state_files_are_not_scanned_as_dicom(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "report.xlsx").write_bytes(b"not a workbook")
            (root / "state.json").write_text("{}", encoding="utf-8")
            (root / "image.dcm").write_bytes(b"DICOM candidate")
            self.assertEqual(
                [path.name for path in center_web.iter_center_files(root)],
                ["image.dcm"],
            )

    def test_header_scan_reads_fourth_batch_fields_without_pixels(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "image.dcm"
            file_meta = FileMetaDataset()
            file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
            file_meta.MediaStorageSOPClassUID = XRayAngiographicImageStorage
            file_meta.MediaStorageSOPInstanceUID = "1.2.826.0.1.3680043.10.400.3"
            file_meta.ImplementationClassUID = "1.2.826.0.1.3680043.10.400.999"
            dataset = FileDataset(
                str(path), {}, file_meta=file_meta, preamble=b"\0" * 128
            )
            dataset.PatientName = "TEST^PATIENT"
            dataset.PatientID = "P001"
            dataset.StudyInstanceUID = "1.2.826.0.1.3680043.10.400.1"
            dataset.SeriesInstanceUID = "1.2.826.0.1.3680043.10.400.2"
            dataset.SOPInstanceUID = "1.2.826.0.1.3680043.10.400.3"
            dataset.SOPClassUID = XRayAngiographicImageStorage
            dataset.AcquisitionDate = "20250131"
            dataset.StudyDate = "20250130"
            dataset.SeriesDate = "20250129"
            dataset.ImageType = ["ORIGINAL", "PRIMARY"]
            dataset.SeriesDescription = "DSA"
            dataset.SliceThickness = "0.5"
            dataset.NumberOfFrames = "20"
            dataset.Modality = "XA"
            dataset.PositionerPrimaryAngle = "10.5"
            dataset.PositionerSecondaryAngle = "-2.0"
            dataset.save_as(str(path), enforce_file_format=True)

            outcome = center_web.scan_dicom_file(
                path, root, {"P001": "测试患者"}
            )

            self.assertTrue(outcome.is_dicom)
            self.assertIsNotNone(outcome.record)
            self.assertEqual(outcome.record.patient_name, "测试患者")
            self.assertEqual(outcome.record.acquisition_date, date(2025, 1, 31))
            self.assertEqual(outcome.record.study_date, date(2025, 1, 30))
            self.assertEqual(outcome.record.series_date, date(2025, 1, 29))
            self.assertEqual(outcome.record.number_of_frames, 20)
            self.assertEqual(outcome.record.primary_angle, 10.5)
            self.assertIn("ORIGINAL", outcome.record.image_type)

    def test_header_scan_reads_screening_number_from_first_patient_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            patient_folder = root / "15_H001" / "study" / "series"
            patient_folder.mkdir(parents=True)
            path = patient_folder / "image.dcm"
            file_meta = FileMetaDataset()
            file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
            file_meta.MediaStorageSOPClassUID = XRayAngiographicImageStorage
            file_meta.MediaStorageSOPInstanceUID = "1.2.826.0.1.3680043.10.500.3"
            file_meta.ImplementationClassUID = "1.2.826.0.1.3680043.10.500.999"
            dataset = FileDataset(
                str(path), {}, file_meta=file_meta, preamble=b"\0" * 128
            )
            dataset.PatientName = "ZHANG^FENGYING"
            dataset.PatientID = "P001"
            dataset.StudyInstanceUID = "1.2.826.0.1.3680043.10.500.1"
            dataset.SeriesInstanceUID = "1.2.826.0.1.3680043.10.500.2"
            dataset.SOPInstanceUID = "1.2.826.0.1.3680043.10.500.3"
            dataset.SOPClassUID = XRayAngiographicImageStorage
            dataset.Modality = "XA"
            dataset.save_as(str(path), enforce_file_format=True)

            outcome = center_web.scan_dicom_file(path, root, {})

            self.assertTrue(outcome.is_dicom)
            self.assertEqual(outcome.record.screening_number, "15-H001")


if __name__ == "__main__":
    unittest.main()
