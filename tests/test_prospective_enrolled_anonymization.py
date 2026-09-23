import sys
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import build_enrolled_study_selection as selection
from enrolled_anonymization_common import TableData
from prospective_enrolled_anonymization import (
    PROSPECTIVE_MAP_FILENAME,
    is_prospective_screening,
    validate_fast_args,
    validate_prospective_screening_rows,
    validate_selection_args,
)


class ProspectiveNumberingTests(unittest.TestCase):
    def test_q_screening_normalization_and_h_rejection(self):
        self.assertTrue(is_prospective_screening("01-Q001"))
        self.assertTrue(is_prospective_screening("1_q001"))
        self.assertFalse(is_prospective_screening("01-H001"))

        with self.assertRaisesRegex(ValueError, "只允许NN-QNNN"):
            validate_prospective_screening_rows(
                [{"__row__": 2, "受试者筛选号": "01-H001"}],
                label="测试表",
            )

    def test_shared_prospective_ledger_starts_at_001_and_continues_by_center(self):
        shared_map = []
        first_patients = {
            "1124550": {"住院号": "1124550", "患者": "AAAA"},
            "1125699": {"住院号": "1125699", "患者": "BBBB"},
        }
        first_matches = {
            "1125699": {
                "#": 2,
                "中心编号": 1,
                "中心名称": "宣武",
                "受试者筛选号": "01-Q008",
                "受试者编号": 8,
            },
            "1124550": {
                "#": 1,
                "中心编号": 1,
                "中心名称": "宣武",
                "受试者筛选号": "01-Q001",
                "受试者编号": 1,
            },
        }

        selection.assign_anonymous_codes(
            first_matches, first_patients, "1", "宣武", shared_map
        )
        self.assertEqual(first_matches["1124550"]["匿名编号"], "001")
        self.assertEqual(first_matches["1125699"]["匿名编号"], "002")

        next_patients = {"2001": {"住院号": "2001", "患者": "CCCC"}}
        next_matches = {
            "2001": {
                "#": 3,
                "中心编号": 10,
                "中心名称": "朝阳",
                "受试者筛选号": "10-Q001",
                "受试者编号": 1,
            }
        }
        selection.assign_anonymous_codes(
            next_matches, next_patients, "10", "朝阳", shared_map
        )
        self.assertEqual(next_matches["2001"]["匿名编号"], "003")

        rerun = {key: dict(value) for key, value in first_matches.items()}
        selection.assign_anonymous_codes(
            rerun, first_patients, "1", "宣武", shared_map
        )
        self.assertEqual(rerun["1124550"]["匿名编号"], "001")
        self.assertEqual(rerun["1125699"]["匿名编号"], "002")
        self.assertEqual(len(shared_map), 3)


class ProspectivePathGuardTests(unittest.TestCase):
    @staticmethod
    def _selection_args() -> Namespace:
        control_root = Path("C:/workflow/前瞻性/control").resolve()
        return Namespace(
            prospective_control_root=str(control_root),
            center="1",
            sequence_workbook="C:/workflow/前瞻性/input/sequence.xlsx",
            enrolled_workbook="C:/workflow/前瞻性/input/enrolled.xlsx",
            registry_workbook="C:/workflow/前瞻性/input/registry.xlsx",
            registry_sheet=None,
            manual_match_workbook=None,
            anonymous_map=str(control_root / PROSPECTIVE_MAP_FILENAME),
            output=str(control_root / "宣武_入组Study筛选表.xlsx"),
        )

    @staticmethod
    def _fast_args() -> Namespace:
        delivery_root = Path("C:/data/前瞻性").resolve()
        control_root = Path("C:/workflow/前瞻性/control").resolve()
        return Namespace(
            prospective_root=str(delivery_root),
            prospective_control_root=str(control_root),
            source_center="C:/source/前瞻性/宣武",
            output_root=str(delivery_root / "01.交付影像"),
            control_dir=str(delivery_root / "02.内部控制文件"),
            anonymous_map=str(control_root / PROSPECTIVE_MAP_FILENAME),
            study_list=str(control_root / "宣武_入组Study筛选表.xlsx"),
            sheet="入组Study清单",
        )

    @patch("prospective_enrolled_anonymization.load_and_validate_prospective_map")
    @patch("prospective_enrolled_anonymization.read_table")
    def test_selection_guard_accepts_q_registry_and_unique_ledger(
        self, mock_read_table, mock_load_map
    ):
        mock_load_map.return_value = []
        mock_read_table.return_value = TableData(
            Path("registry.xlsx"),
            "registry",
            (),
            (
                {
                    "中心编号": 1,
                    "中心名称": "宣武",
                    "受试者筛选号": "01-Q001",
                },
            ),
        )

        guard = validate_selection_args(self._selection_args())

        self.assertEqual(guard["center_code"], "1")
        self.assertEqual(guard["center_name"], "宣武")

    def test_selection_guard_rejects_an_alternate_ledger(self):
        args = self._selection_args()
        args.anonymous_map = str(
            Path(args.prospective_control_root) / "患者匿名编号映射2.xlsx"
        )
        with self.assertRaisesRegex(ValueError, "唯一编号账本"):
            validate_selection_args(args)

    @patch("prospective_enrolled_anonymization.load_and_validate_prospective_map")
    @patch("prospective_enrolled_anonymization.read_table")
    def test_fast_guard_accepts_matching_q_study_list(
        self, mock_read_table, mock_load_map
    ):
        mapping = {
            "中心编号": "1",
            "受试者筛选号": "01-Q001",
            "匿名编号": "001",
        }
        mock_load_map.return_value = [mapping]
        mock_read_table.return_value = TableData(
            Path("study-list.xlsx"),
            "入组Study清单",
            (),
            (
                {
                    "__row__": 2,
                    "中心编号": "1",
                    "受试者筛选号": "01-Q001",
                    "匿名编号": "001",
                    "是否进入匿名化": "是",
                },
            ),
        )

        guard = validate_fast_args(self._fast_args())

        self.assertEqual(guard["approved_studies"], 1)

    def test_fast_guard_rejects_a_retrospective_source_directory(self):
        args = self._fast_args()
        args.source_center = "C:/source/回顾性/宣武"
        with self.assertRaisesRegex(ValueError, "前瞻性源中心目录"):
            validate_fast_args(args)


if __name__ == "__main__":
    unittest.main()
