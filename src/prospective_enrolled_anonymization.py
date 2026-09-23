#!/usr/bin/env python3
"""Safety guards for the prospective enrolled-Study anonymization workflow.

The retrospective and prospective cohorts deliberately share the established
selection and DICOM anonymization business rules.  This module only prevents
the two numbering namespaces, control workbooks and delivery trees from being
mixed accidentally.
"""

from __future__ import annotations

import re
import unicodedata
from argparse import Namespace
from pathlib import Path
from typing import Any, Iterable

import build_enrolled_study_selection as selection
from enrolled_anonymization_common import (
    anonymous_code,
    choose_center,
    is_yes,
    normalized_identifier,
    normalized_screening,
    read_table,
    text,
)


COHORT_NAME = "前瞻性"
GUARD_VERSION = "2026.09.21-prospective-enrolled-anon-guard-v1.0"
PROSPECTIVE_SCREENING_PATTERN = re.compile(r"\d+-Q\d{3}", re.IGNORECASE)
PROSPECTIVE_PATH_TOKENS = ("前瞻性", "prospective")
PROSPECTIVE_MAP_FILENAME = "患者匿名编号映射.xlsx"
STUDY_LIST_GUARD_REQUIRED = (
    "中心编号",
    "受试者筛选号",
    "匿名编号",
    "是否进入匿名化",
)


def _normalized_path_token(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _contains_prospective_path_token(path: Path) -> bool:
    for component in path.resolve().parts:
        normalized = _normalized_path_token(component)
        if any(token in normalized for token in PROSPECTIVE_PATH_TOKENS):
            return True
    return False


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def require_within(path: Path, root: Path, label: str) -> None:
    if not _is_relative_to(path, root):
        raise ValueError(f"{label}必须位于前瞻性控制根目录内: {root.resolve()}")


def require_prospective_root(path: Path, label: str) -> None:
    if not _contains_prospective_path_token(path):
        raise ValueError(
            f"{label}路径必须明确包含“前瞻性”或“prospective”，防止误用回顾性目录: "
            f"{path.resolve()}"
        )


def is_prospective_screening(value: Any) -> bool:
    return bool(PROSPECTIVE_SCREENING_PATTERN.fullmatch(normalized_screening(value)))


def _format_bad_screenings(items: Iterable[tuple[Any, Any]]) -> str:
    displays = []
    for row_number, value in items:
        displays.append(f"第{row_number or '?'}行={text(value) or '<空>'}")
    return "、".join(displays[:8]) + ("等" if len(displays) > 8 else "")


def validate_prospective_screening_rows(
    rows: Iterable[dict[str, Any]], *, label: str
) -> None:
    bad: list[tuple[Any, Any]] = []
    seen = 0
    for row in rows:
        screening = row.get("受试者筛选号")
        if not text(screening):
            continue
        seen += 1
        if not is_prospective_screening(screening):
            bad.append((row.get("__row__"), screening))
    if not seen:
        raise ValueError(f"{label}没有受试者筛选号")
    if bad:
        raise ValueError(
            f"{label}含非前瞻性筛选号；只允许NN-QNNN: {_format_bad_screenings(bad)}"
        )


def load_and_validate_prospective_map(path: Path) -> list[dict[str, Any]]:
    path = path.resolve()
    require_prospective_root(path.parent, "前瞻性编号账本目录")
    rows = selection.load_anonymous_map(path)
    if rows:
        validate_prospective_screening_rows(rows, label="前瞻性匿名编号账本")
    return rows


def validate_selection_args(args: Namespace) -> dict[str, str]:
    control_root = Path(args.prospective_control_root).resolve()
    require_prospective_root(control_root, "前瞻性控制根目录")
    if not getattr(args, "anonymous_map", None):
        raise ValueError("前瞻性入口必须显式提供--anonymous-map，不能使用默认账本")

    output = Path(args.output).resolve()
    anonymous_map = Path(args.anonymous_map).resolve()
    require_within(output, control_root, "Study筛选表输出")
    require_within(anonymous_map, control_root, "前瞻性匿名编号账本")
    expected_map = control_root / PROSPECTIVE_MAP_FILENAME
    if anonymous_map != expected_map:
        raise ValueError(
            "前瞻性全项目必须共用唯一编号账本: "
            f"{expected_map}"
        )
    for value, label in (
        (args.sequence_workbook, "前瞻性筛选序列表"),
        (args.enrolled_workbook, "前瞻性入组患者表"),
        (args.registry_workbook, "前瞻性分中心影像表"),
    ):
        require_prospective_root(Path(value), label)
    manual_match = getattr(args, "manual_match_workbook", None)
    if manual_match:
        require_within(Path(manual_match), control_root, "前瞻性人工确认表")

    load_and_validate_prospective_map(anonymous_map)
    registry = read_table(
        args.registry_workbook,
        selection.REGISTRY_REQUIRED,
        sheet_name=args.registry_sheet,
        max_columns=17,
    )
    center_code, center_name = choose_center(registry.rows, args.center)
    center_rows = [
        row
        for row in registry.rows
        if normalized_identifier(row.get("中心编号"))
        == normalized_identifier(center_code)
    ]
    validate_prospective_screening_rows(
        center_rows, label=f"前瞻性分中心影像表（{center_name}）"
    )
    return {
        "control_root": str(control_root),
        "anonymous_map": str(anonymous_map),
        "output": str(output),
        "center_code": center_code,
        "center_name": center_name,
    }


def _mapping_key(center_code: Any, screening: Any) -> str:
    return f"{normalized_identifier(center_code)}|{normalized_screening(screening)}"


def validate_study_list_against_map(
    study_list: Path,
    sheet_name: str,
    mapping_rows: list[dict[str, Any]],
) -> int:
    table = read_table(study_list, STUDY_LIST_GUARD_REQUIRED, sheet_name=sheet_name)
    validate_prospective_screening_rows(table.rows, label="前瞻性Study清单")
    by_key = {
        _mapping_key(row.get("中心编号"), row.get("受试者筛选号")): anonymous_code(
            row.get("匿名编号")
        )
        for row in mapping_rows
    }
    approved = 0
    mismatches: list[str] = []
    for row in table.rows:
        if not is_yes(row.get("是否进入匿名化")):
            continue
        approved += 1
        key = _mapping_key(row.get("中心编号"), row.get("受试者筛选号"))
        try:
            code = anonymous_code(row.get("匿名编号"))
        except ValueError as exc:
            mismatches.append(f"第{row.get('__row__', '?')}行匿名编号无效: {exc}")
            continue
        mapped_code = by_key.get(key)
        if mapped_code != code:
            mismatches.append(
                f"第{row.get('__row__', '?')}行{key}的清单编号={code}，"
                f"前瞻性账本编号={mapped_code or '<不存在>'}"
            )
    if mismatches:
        raise ValueError("Study清单与前瞻性编号账本不一致: " + "；".join(mismatches[:8]))
    if not approved:
        raise ValueError("前瞻性Study清单中没有“是否进入匿名化=是”的Study")
    return approved


def validate_fast_args(args: Namespace) -> dict[str, str | int]:
    prospective_root = Path(args.prospective_root).resolve()
    require_prospective_root(prospective_root, "前瞻性匿名化根目录")
    control_root = Path(args.prospective_control_root).resolve()
    require_prospective_root(control_root, "前瞻性编排控制根目录")
    require_prospective_root(Path(args.source_center), "前瞻性源中心目录")
    output_root = Path(args.output_root).resolve()
    if not getattr(args, "control_dir", None):
        raise ValueError("前瞻性入口必须显式提供--control-dir")
    control_dir = Path(args.control_dir).resolve()
    expected_output = prospective_root / "01.交付影像"
    expected_control = prospective_root / "02.内部控制文件"
    if output_root != expected_output:
        raise ValueError(f"--output-root必须是: {expected_output}")
    if control_dir != expected_control:
        raise ValueError(f"--control-dir必须是: {expected_control}")

    anonymous_map = Path(args.anonymous_map).resolve()
    expected_map = control_root / PROSPECTIVE_MAP_FILENAME
    if anonymous_map != expected_map:
        raise ValueError(
            "前瞻性全项目必须共用唯一编号账本: "
            f"{expected_map}"
        )
    mapping_rows = load_and_validate_prospective_map(anonymous_map)
    study_list = Path(args.study_list).resolve()
    require_within(study_list, control_root, "前瞻性Study清单")
    approved = validate_study_list_against_map(study_list, args.sheet, mapping_rows)
    return {
        "prospective_root": str(prospective_root),
        "prospective_control_root": str(control_root),
        "output_root": str(output_root),
        "control_dir": str(control_dir),
        "anonymous_map": str(anonymous_map),
        "approved_studies": approved,
    }
