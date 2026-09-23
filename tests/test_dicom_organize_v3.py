import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, XRayAngiographicImageStorage

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import dicom_organize_v3 as v3


def write_dicom(
    path: Path,
    *,
    patient_name: str,
    patient_id: str,
    uid_number: int,
    birth_date: str = "",
    sex: str = "",
    age: str = "",
    study_date: str = "20260804",
    institution: str = "",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    root = "1.2.826.0.1.3680043.10.300"
    study_uid = f"{root}.{uid_number}.1"
    series_uid = f"{root}.{uid_number}.2"
    sop_uid = f"{root}.{uid_number}.3"
    file_meta = FileMetaDataset()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.MediaStorageSOPClassUID = XRayAngiographicImageStorage
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.ImplementationClassUID = f"{root}.999"
    dataset = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    dataset.SpecificCharacterSet = "ISO_IR 192"
    dataset.PatientName = patient_name
    dataset.PatientID = patient_id
    dataset.PatientBirthDate = birth_date
    dataset.PatientSex = sex
    if age:
        dataset.PatientAge = age
    if institution:
        dataset.InstitutionName = institution
    dataset.StudyDate = study_date
    dataset.Modality = "XA"
    dataset.StudyInstanceUID = study_uid
    dataset.SeriesInstanceUID = series_uid
    dataset.SOPInstanceUID = sop_uid
    dataset.SOPClassUID = XRayAngiographicImageStorage
    dataset.SeriesDescription = "DSA"
    dataset.save_as(str(path), enforce_file_format=True)


def scan_all(source: Path) -> tuple[v3.IdentityPlanner, list[v3.V3ScanResult]]:
    planner = v3.IdentityPlanner()
    scans = []
    for path in sorted(item for item in source.rglob("*") if item.is_file()):
        scan = v3.scan_one_file(path, source)
        scans.append(scan)
        if scan.identity:
            planner.add(scan.identity)
    return planner, scans


class V3IdentityPlanTests(unittest.TestCase):
    def test_chinese_folder_name_uses_pinyin_match_not_dominant_file_count(self):
        planner = v3.IdentityPlanner()
        for index in range(3):
            planner.add(
                v3.IdentityObservation(
                    patient_id="SYNTH_OTHER_PID",
                    header_patient_name="WANG PINGCHANG",
                    source_subject="王长平",
                    source_subject_name="王长平",
                    study_uid=f"A{index}",
                )
            )
        planner.add(
            v3.IdentityObservation(
                patient_id="SYNTH_MATCH_PID",
                header_patient_name="WANG CHANGPING",
                source_subject="王长平",
                source_subject_name="王长平",
                study_uid="B1",
            )
        )

        profiles = planner.profiles()

        self.assertEqual(profiles["SYNTH_OTHER_PID"].canonical_name, "WANG PINGCHANG")
        self.assertEqual(profiles["SYNTH_MATCH_PID"].canonical_name, "王长平")
        self.assertTrue(
            v3.chinese_name_matches_header("王长平", "WANG CHANGPING M-65Y^^^^")
        )
        self.assertFalse(
            v3.chinese_name_matches_header("王长平", "WANG PINGCHANG")
        )

    def test_patient_name_demographic_suffix_is_parsed_conservatively(self):
        self.assertEqual(
            v3.parse_patient_name("WANG XIAOMING M-56Y^^^^"),
            ("WANG XIAOMING", "M", "056Y"),
        )
        self.assertEqual(
            v3.normalize_name("WANG XIAO MING"),
            v3.normalize_name("WANG XIAOMING M-56Y^^^^"),
        )
        self.assertEqual(
            v3.parse_patient_name("ZHANG MING"),
            ("ZHANG MING", "", ""),
        )

    def test_patient_name_suffix_conflict_is_not_used_for_auto_merge(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "data"
            write_dicom(
                source / "a.dcm",
                patient_name="WANG XIAOMING M-56Y^^^^",
                patient_id="A001",
                uid_number=30,
                sex="F",
                study_date="20250902",
            )
            planner, _ = scan_all(source)
            profile = planner.profiles()["A001"]

            self.assertIn("sex_vs_patient_name_suffix", profile.internal_conflicts)

    def test_single_patient_id_keeps_flat_patient_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "data"
            write_dicom(
                source / "王玉梅_A001" / "image.dcm",
                patient_name="WANG^YUMEI",
                patient_id="A001",
                uid_number=1,
                birth_date="19600102",
                sex="F",
            )
            planner, _ = scan_all(source)
            plan = v3.build_identity_plan(planner)

            route = plan.routes["A001"]
            self.assertEqual(route.canonical_name, "王玉梅")
            self.assertEqual(route.group_folder, "")
            self.assertEqual(route.destination_patient_path, "王玉梅_A001")

    def test_two_patient_ids_with_two_matching_demographics_are_nested(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "data"
            write_dicom(
                source / "王玉梅_A001" / "a.dcm",
                patient_name="WANG^YUMEI",
                patient_id="A001",
                uid_number=2,
                birth_date="19600102",
                sex="F",
            )
            write_dicom(
                source / "王玉梅_B002" / "b.dcm",
                patient_name="王玉梅",
                patient_id="B002",
                uid_number=3,
                birth_date="19600102",
                sex="F",
            )
            planner, _ = scan_all(source)
            plan = v3.build_identity_plan(planner)

            self.assertEqual(
                plan.routes["A001"].master_patient_key,
                plan.routes["B002"].master_patient_key,
            )
            self.assertEqual(plan.routes["A001"].group_folder, "王玉梅")
            self.assertEqual(
                plan.routes["B002"].destination_patient_path,
                str(Path("王玉梅") / "王玉梅_B002"),
            )

    def test_same_name_with_insufficient_evidence_is_not_merged(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "data"
            write_dicom(
                source / "王玉梅_A001" / "a.dcm",
                patient_name="王玉梅",
                patient_id="A001",
                uid_number=4,
            )
            write_dicom(
                source / "王玉梅_B002" / "b.dcm",
                patient_name="王玉梅",
                patient_id="B002",
                uid_number=5,
            )
            planner, _ = scan_all(source)
            plan = v3.build_identity_plan(planner)

            self.assertNotEqual(
                plan.routes["A001"].master_patient_key,
                plan.routes["B002"].master_patient_key,
            )
            self.assertEqual(plan.routes["A001"].group_folder, "")
            self.assertTrue(
                any(row["review_status"] == "needs_review" for row in plan.review_rows)
            )

    def test_conflicting_birth_dates_are_not_merged(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "data"
            write_dicom(
                source / "王玉梅_A001" / "a.dcm",
                patient_name="王玉梅",
                patient_id="A001",
                uid_number=6,
                birth_date="19600102",
                sex="F",
            )
            write_dicom(
                source / "王玉梅_B002" / "b.dcm",
                patient_name="王玉梅",
                patient_id="B002",
                uid_number=7,
                birth_date="19750304",
                sex="F",
            )
            planner, _ = scan_all(source)
            plan = v3.build_identity_plan(planner)

            self.assertNotEqual(
                plan.routes["A001"].master_patient_key,
                plan.routes["B002"].master_patient_key,
            )
            self.assertTrue(
                any(row["review_status"] == "conflict" for row in plan.review_rows)
            )

    def test_same_name_sex_and_hospital_groups_birth_date_conflict_for_review(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "王玉梅"
            write_dicom(
                source / "a.dcm",
                patient_name="Wang Yu Mei",
                patient_id="04553303",
                uid_number=60,
                birth_date="19740317",
                sex="F",
                age="051Y",
                study_date="20250417",
                institution="XiongAn Xuanwu Hospital_JJJHR",
            )
            write_dicom(
                source / "b.dcm",
                patient_name="Wang Yu Mei",
                patient_id="04594638",
                uid_number=61,
                birth_date="19740226",
                sex="F",
                age="052Y",
                study_date="20260225",
                institution="雄安宣武医院-JJJHR",
            )
            planner, _ = scan_all(source)

            plan = v3.build_identity_plan(planner)

            self.assertEqual(
                plan.routes["04553303"].master_patient_key,
                plan.routes["04594638"].master_patient_key,
            )
            self.assertEqual(plan.routes["04553303"].group_folder, "王玉梅")
            self.assertEqual(
                plan.routes["04594638"].match_status, "grouped_needs_review"
            )
            review = next(
                row
                for row in plan.review_rows
                if row["review_status"] == "grouped_needs_review"
            )
            self.assertIn("xiongan_xuanwu_hospital", review["left_institutions"])
            self.assertIn("birth_date", review["match_basis"])

    def test_historical_demographics_survive_missing_values_in_new_batch(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "data"
            write_dicom(
                source / "王玉梅_A001" / "a.dcm",
                patient_name="王玉梅",
                patient_id="A001",
                uid_number=8,
            )
            write_dicom(
                source / "王玉梅_B002" / "b.dcm",
                patient_name="王玉梅",
                patient_id="B002",
                uid_number=9,
                birth_date="19600102",
                sex="F",
            )
            planner, _ = scan_all(source)
            existing = [
                {
                    "master_patient_key": "G000010",
                    "canonical_name": "王玉梅",
                    "patient_id": "A001",
                    "header_patient_name": "王玉梅",
                    "birth_date": "19600102",
                    "sex": "F",
                    "estimated_birth_year": "",
                    "all_patient_ids": "A001",
                    "match_status": "new_identity",
                    "match_basis": "historical",
                    "source_subjects": "old_batch",
                }
            ]
            plan = v3.build_identity_plan(planner, existing)

            self.assertEqual(plan.routes["A001"].master_patient_key, "G000010")
            self.assertEqual(plan.routes["B002"].master_patient_key, "G000010")
            mapped_a = next(
                row for row in plan.mapping_rows if row["patient_id"] == "A001"
            )
            self.assertEqual(mapped_a["birth_date"], "19600102")
            self.assertEqual(mapped_a["sex"], "F")

    def test_matching_header_name_can_bridge_chinese_folder_and_pinyin(self):
        left = v3.IdentityProfile(
            patient_id="A001",
            canonical_name="王玉梅",
            header_patient_name="WANG YUMEI",
            birth_date="19600102",
            sex="F",
        )
        right = v3.IdentityProfile(
            patient_id="B002",
            canonical_name="WANG YUMEI",
            header_patient_name="WANG YUMEI",
            birth_date="19600102",
            sex="F",
        )

        relation, basis = v3.compare_profiles(left, right)

        self.assertEqual(relation, "confirmed")
        self.assertIn("header_patient_name", basis)

    def test_unknown_names_never_supply_name_evidence(self):
        left = v3.IdentityProfile(
            patient_id="A001", canonical_name="Unknown",
            birth_date="19600102", sex="F",
        )
        right = v3.IdentityProfile(
            patient_id="B002", canonical_name="UNKNOWN",
            birth_date="19600102", sex="F",
        )

        relation, _ = v3.compare_profiles(left, right)

        self.assertEqual(relation, "unrelated")

    def test_realistic_age_suffix_auto_merges_two_historical_groups(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "data"
            write_dicom(
                source / "2025-08-12 王小明 CTA" / "cta.dcm",
                patient_name="WANG XIAO MING",
                patient_id="SYNTH_PID_A",
                uid_number=17,
                birth_date="19690203",
                sex="M",
                age="056Y",
                study_date="20250812",
            )
            write_dicom(
                source / "2025-09-02 WXM" / "dsa.dcm",
                patient_name="WANG XIAOMING M-56Y^^^^",
                patient_id="SYNTH_PID_B",
                uid_number=18,
                sex="M",
                study_date="20250902",
            )
            planner, _ = scan_all(source)
            existing = [
                {
                    "master_patient_key": "G000001",
                    "canonical_name": "王小明",
                    "patient_id": "SYNTH_PID_A",
                    "header_patient_name": "WANG XIAO MING",
                    "birth_date": "19690203",
                    "sex": "M",
                    "estimated_birth_year": "1969",
                    "all_patient_ids": "SYNTH_PID_A",
                    "match_status": "new_identity",
                    "match_basis": "historical",
                    "source_subjects": "2025-08-12 王小明 CTA",
                },
                {
                    "master_patient_key": "G000002",
                    "canonical_name": "WANG XIAOMING M-56Y",
                    "patient_id": "SYNTH_PID_B",
                    "header_patient_name": "WANG XIAOMING M-56Y",
                    "birth_date": "",
                    "sex": "M",
                    "estimated_birth_year": "",
                    "all_patient_ids": "SYNTH_PID_B",
                    "match_status": "new_identity",
                    "match_basis": "historical",
                    "source_subjects": "2025-09-02 WXM",
                },
            ]

            plan = v3.build_identity_plan(planner, existing)

            left = plan.routes["SYNTH_PID_A"]
            right = plan.routes["SYNTH_PID_B"]
            self.assertEqual(left.master_patient_key, "G000001")
            self.assertEqual(right.master_patient_key, "G000001")
            self.assertEqual(left.canonical_name, "王小明")
            self.assertEqual(right.group_folder, "王小明")
            self.assertEqual(right.match_status, "auto_merged_historical_group")
            self.assertTrue(
                any(row["review_status"] == "auto_resolved" for row in plan.review_rows)
            )

            rerun = v3.build_identity_plan(planner, plan.mapping_rows)
            self.assertFalse(
                any(row["review_status"] == "mapping_drift" for row in rerun.review_rows)
            )


class V3MixedSourceRoutingTests(unittest.TestCase):
    def test_unmatched_patient_in_mixed_folder_uses_header_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "data"
            subject = source / "2024-11-11 王玉梅"
            write_dicom(
                subject / "main-a.dcm",
                patient_name="王玉梅",
                patient_id="A001",
                uid_number=10,
            )
            write_dicom(
                subject / "main-b.dcm",
                patient_name="王玉梅",
                patient_id="A001",
                uid_number=11,
            )
            write_dicom(
                subject / "stray.dcm",
                patient_name="ZHANG^SAN",
                patient_id="B002",
                uid_number=12,
            )
            planner, scans = scan_all(source)
            plan = v3.build_identity_plan(planner)

            self.assertEqual(plan.routes["A001"].canonical_name, "王玉梅")
            self.assertEqual(plan.routes["B002"].canonical_name, "ZHANG SAN")
            stray = next(scan for scan in scans if scan.scan.meta.patient_id == "B002")
            target, quarantined, _ = v3.destination_for(
                stray, Path(temp) / "output", plan.routes
            )
            self.assertFalse(quarantined)
            self.assertIn("ZHANG SAN_B002", target.parts)
            self.assertTrue(plan.source_audit_rows)

    def test_global_patient_id_route_recovers_chinese_name_for_stray_data(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "data"
            mixed = source / "2024-11-11 王玉梅"
            write_dicom(
                mixed / "main-a.dcm",
                patient_name="王玉梅",
                patient_id="A001",
                uid_number=13,
            )
            write_dicom(
                mixed / "main-b.dcm",
                patient_name="王玉梅",
                patient_id="A001",
                uid_number=14,
            )
            write_dicom(
                mixed / "stray.dcm",
                patient_name="ZHANG^SAN",
                patient_id="B002",
                uid_number=15,
            )
            write_dicom(
                source / "张三_B002" / "known.dcm",
                patient_name="ZHANG^SAN",
                patient_id="B002",
                uid_number=16,
            )
            planner, scans = scan_all(source)
            plan = v3.build_identity_plan(planner)

            self.assertEqual(plan.routes["B002"].canonical_name, "张三")
            stray = next(
                scan
                for scan in scans
                if scan.scan.path.name == "stray.dcm"
            )
            target, _, _ = v3.destination_for(
                stray, Path(temp) / "output", plan.routes
            )
            self.assertIn("张三_B002", target.parts)


class V3ProcessTests(unittest.TestCase):
    def test_process_directory_passes_cumulative_studies_to_second_workbook(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "output"
            first = root / "first"
            second = root / "second"
            write_dicom(
                first / "甲_A001" / "a.dcm",
                patient_name="甲",
                patient_id="A001",
                uid_number=70,
            )
            write_dicom(
                second / "乙_B002" / "b.dcm",
                patient_name="乙",
                patient_id="B002",
                uid_number=71,
            )
            excel = output / "影像检查明细_V3.xlsx"
            state = output / ".dicom_v3_state" / "identity_mapping.json"

            with patch.object(v3, "write_study_workbook") as workbook_writer:
                for source in (first, second):
                    v3.process_directory(
                        source,
                        output,
                        excel,
                        None,
                        state,
                        None,
                        None,
                        workers=1,
                        copy_workers=1,
                    )

            second_rows = workbook_writer.call_args_list[1].args[1]
            self.assertEqual(
                {row["study_uid"] for row in second_rows},
                {
                    "1.2.826.0.1.3680043.10.300.70.1",
                    "1.2.826.0.1.3680043.10.300.71.1",
                },
            )

    def test_study_inventory_appends_and_deduplicates_across_runs(self):
        previous = [
            {
                "patient_id": "A001",
                "patient_name": "甲",
                "exam_date": "2026-01-01",
                "modalities": "CT",
                "has_3d": "无",
                "display_type": "CT",
                "study_uid": "1.2.3",
                "series_count": 2,
                "file_count": 20,
                "confidence": 0.99,
                "evidence": "old",
                "source_root": "batch-a",
                "destination_dir": "out-a",
            }
        ]
        current = [
            {
                "patient_id": "B002",
                "patient_name": "乙",
                "exam_date": "2026-02-02",
                "modalities": "XA",
                "has_3d": "有",
                "display_type": "XA（含3D_DSA断层）",
                "study_uid": "4.5.6",
                "series_count": 3,
                "file_count": 30,
                "confidence": 0.88,
                "evidence": "new",
                "source_root": "batch-b",
                "destination_dir": "out-b",
            },
            {
                "patient_id": "A001",
                "patient_name": "甲",
                "exam_date": "2026-01-01",
                "modalities": "CT",
                "has_3d": "无",
                "display_type": "CT",
                "study_uid": "1.2.3",
                "series_count": 2,
                "file_count": 20,
                "confidence": 0.99,
                "evidence": "old",
                "source_root": "batch-a",
                "destination_dir": "out-a",
            },
        ]

        merged = v3.merge_study_inventory(previous, current)

        self.assertEqual([row["study_uid"] for row in merged], ["1.2.3", "4.5.6"])

    def test_study_inventory_groups_same_master_patient_and_sorts_by_date(self):
        studies = [
            {
                "patient_id": "A001",
                "patient_name": "Alpha Patient",
                "exam_date": "2026-07-12",
                "study_uid": "study-alpha-late",
            },
            {
                "patient_id": "B001",
                "patient_name": "Beta Patient",
                "exam_date": "2026-01-18",
                "study_uid": "study-beta",
            },
            {
                "patient_id": "C001",
                "patient_name": "Alpha Patient",
                "exam_date": "2025-04-17",
                "study_uid": "study-alpha-early",
            },
        ]
        mappings = [
            {
                "master_patient_key": "G000001",
                "canonical_name": "Alpha Patient",
                "patient_id": "A001",
            },
            {
                "master_patient_key": "G000002",
                "canonical_name": "Beta Patient",
                "patient_id": "B001",
            },
            {
                "master_patient_key": "G000001",
                "canonical_name": "Alpha Patient",
                "patient_id": "C001",
            },
        ]

        sorted_rows = v3.merge_study_inventory([], studies, mappings)

        self.assertEqual(
            [row["study_uid"] for row in sorted_rows],
            ["study-alpha-early", "study-alpha-late", "study-beta"],
        )

    def test_new_classification_rule_replaces_old_3d_result(self):
        previous = [
            {
                "study_uid": "1.2.3",
                "modalities": "XA",
                "has_3d": "有",
                "display_type": "XA（含3D_DSA断层）",
                "confidence": 0.88,
                "evidence": "旧规则：XA+DYNAMIC+多帧",
                "classification_rule_version": "2026.08-study-v1",
            }
        ]
        current = [
            {
                "study_uid": "1.2.3",
                "modalities": "XA",
                "has_3d": "无",
                "display_type": "XA",
                "confidence": 0.72,
                "evidence": "XA序列缺少SliceThickness，不判定为3D_DSA断层",
                "classification_rule_version": v3.v2.RULE_VERSION,
            }
        ]

        merged = v3.merge_study_inventory(previous, current)

        self.assertEqual(merged[0]["has_3d"], "无")
        self.assertEqual(merged[0]["display_type"], "XA")
        self.assertEqual(merged[0]["confidence"], 0.72)
        self.assertEqual(merged[0]["evidence"], current[0]["evidence"])
        self.assertEqual(
            merged[0]["classification_rule_version"], v3.v2.RULE_VERSION
        )

    def test_merged_study_attaches_3d_label_to_xa_before_ot(self):
        previous = [
            {
                "study_uid": "1.2.3",
                "modalities": "OT",
                "has_3d": "无",
                "display_type": "OT",
                "classification_rule_version": v3.v2.RULE_VERSION,
            }
        ]
        current = [
            {
                "study_uid": "1.2.3",
                "modalities": "XA",
                "has_3d": "有",
                "display_type": "XA（含3D_DSA断层）",
                "classification_rule_version": v3.v2.RULE_VERSION,
            }
        ]

        merged = v3.merge_study_inventory(previous, current)

        self.assertEqual(merged[0]["modalities"], "XA、OT")
        self.assertEqual(
            merged[0]["display_type"], "XA（含3D_DSA断层）、OT"
        )

    def test_process_directory_scans_plans_copies_and_writes_csv_reports(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "input"
            output = root / "output"
            write_dicom(
                source / "王玉梅_A001" / "image.dcm",
                patient_name="王玉梅",
                patient_id="A001",
                uid_number=20,
                birth_date="19600102",
                sex="F",
            )
            excel = output / "studies.xlsx"
            exceptions = output / "exceptions.csv"
            mapping = root / "identity.csv"
            review = output / "identity_review.csv"
            source_audit = output / "source_audit.csv"
            manifest = output / "manifest.csv"

            with patch.object(v3, "write_study_workbook") as workbook_writer:
                counters = v3.process_directory(
                    source,
                    output,
                    excel,
                    exceptions,
                    mapping,
                    review,
                    source_audit,
                    workers=2,
                    copy_workers=2,
                    batch_size=2,
                    manifest_path=manifest,
                )

            self.assertEqual(counters["dicom"], 1)
            self.assertEqual(counters["copied"], 1)
            self.assertEqual(counters["studies"], 1)
            self.assertTrue(mapping.is_file())
            self.assertTrue(review.is_file())
            self.assertTrue(source_audit.is_file())
            self.assertTrue(exceptions.is_file())
            self.assertTrue(manifest.is_file())
            copied = list(output.rglob("*.dcm"))
            self.assertEqual(len(copied), 1)
            self.assertIn("王玉梅_A001", copied[0].parts)
            workbook_writer.assert_called_once()

    def test_default_style_outputs_state_and_workbook_data_without_csvs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "input"
            output = root / "output"
            write_dicom(
                source / "王玉梅_A001" / "image.dcm",
                patient_name="王玉梅",
                patient_id="A001",
                uid_number=21,
                birth_date="19600102",
                sex="F",
            )
            state = output / ".dicom_v3_state" / "identity_mapping.json"

            with patch.object(v3, "write_study_workbook") as workbook_writer:
                v3.process_directory(
                    source,
                    output,
                    output / "report.xlsx",
                    None,
                    state,
                    None,
                    None,
                    workers=1,
                    copy_workers=1,
                    batch_size=10,
                )

            self.assertTrue(state.is_file())
            self.assertEqual(v3.load_identity_mapping(state)[0]["patient_id"], "A001")
            self.assertEqual(list(output.glob("*.csv")), [])
            call = workbook_writer.call_args
            self.assertEqual(call.args[2][0]["patient_id"], "A001")

    def test_v3_fast_copy_preserves_duplicate_and_conflict_semantics(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "input"
            output = root / "output"
            first = source / "first.dcm"
            second = source / "second.dcm"
            write_dicom(
                first,
                patient_name="王玉梅",
                patient_id="A001",
                uid_number=22,
                birth_date="19600102",
                sex="F",
            )
            write_dicom(
                second,
                patient_name="王玉梅",
                patient_id="A001",
                uid_number=22,
                birth_date="19610102",
                sex="F",
            )
            first_scan = v3.scan_one_file(first, source)
            second_scan = v3.scan_one_file(second, source)
            planner = v3.IdentityPlanner()
            planner.add(first_scan.identity)
            plan = v3.build_identity_plan(planner)

            copied = v3.transfer_one(first_scan, output, plan.routes)
            duplicate = v3.transfer_one(first_scan, output, plan.routes)
            conflict = v3.transfer_one(second_scan, output, plan.routes)

            self.assertEqual(copied.status, "copied")
            self.assertEqual(duplicate.status, "duplicate_same")
            self.assertEqual(conflict.status, "conflict")
            self.assertTrue(Path(conflict.destination_path).is_file())


if __name__ == "__main__":
    unittest.main()
