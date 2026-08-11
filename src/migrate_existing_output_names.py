#!/usr/bin/env python3
"""Safely migrate existing anonymized study folders to standardized names."""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List

from dicom_anonymize_classify import (
    chinese_person_name,
    date_from_dicom_values,
    date_from_text,
    sanitize_path_part,
)


def parse_date_overrides(values: Iterable[str]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"日期覆盖格式应为 originalStudyUID=YYYYMMDD: {value}")
        uid, raw_date = value.split("=", 1)
        date = date_from_dicom_values(raw_date)
        if not uid.strip() or not date:
            raise ValueError(f"无效日期覆盖: {value}")
        result[uid.strip()] = date
    return result


def parse_subject_overrides(values: Iterable[str]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for value in values:
        if "=" not in value or "|" not in value.split("=", 1)[0]:
            raise ValueError(f"姓名覆盖格式应为 imageType|oldFolder=姓名: {value}")
        key, person = value.split("=", 1)
        if not key.strip() or not chinese_person_name(person):
            raise ValueError(f"无效姓名覆盖: {value}")
        result[key.strip()] = chinese_person_name(person)
    return result


def load_rows(mapping_path: Path) -> Dict[str, List[dict]]:
    by_study: Dict[str, List[dict]] = defaultdict(list)
    with mapping_path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("record_type") != "SERIES":
                continue
            uid = row.get("anonymous_study_uid", "").strip()
            if uid:
                by_study[uid].append(row)
    return by_study


def one_value(rows: List[dict], field: str) -> str:
    values = {row.get(field, "").strip() for row in rows if row.get(field, "").strip()}
    if len(values) != 1:
        raise ValueError(f"同一 Study 的 {field} 不唯一: {sorted(values)}")
    return next(iter(values))


def build_plan(
    output_root: Path,
    rows_by_study: Dict[str, List[dict]],
    date_overrides: Dict[str, str],
    subject_overrides: Dict[str, str],
) -> tuple[List[dict], List[str]]:
    plan: List[dict] = []
    errors: List[str] = []
    for type_dir in sorted(p for p in output_root.iterdir() if p.is_dir()):
        for old_folder in sorted(p for p in type_dir.iterdir() if p.is_dir()):
            for study_dir in sorted(p for p in old_folder.iterdir() if p.is_dir()):
                rows = rows_by_study.get(study_dir.name, [])
                if not rows:
                    image_type = type_dir.name
                    source_folder = old_folder.name
                    source_subject = subject_overrides.get(
                        f"{image_type}|{source_folder}", ""
                    )
                    original_study_uid = ""
                    if not source_subject:
                        errors.append(f"映射表找不到 Study: {study_dir}")
                        continue
                else:
                    try:
                        image_type = one_value(rows, "source_image_type")
                        source_folder = one_value(rows, "source_folder_name")
                        source_subject = one_value(rows, "source_subject")
                        original_study_uid = one_value(rows, "original_study_uid")
                    except ValueError as exc:
                        errors.append(f"{study_dir}: {exc}")
                        continue

                date = date_from_text(source_folder) or date_overrides.get(original_study_uid, "")
                person = chinese_person_name(source_folder, source_subject)
                if not date:
                    errors.append(f"缺少原始 DICOM 日期: {study_dir} ({original_study_uid})")
                    continue
                if not person:
                    errors.append(f"找不到中文姓名: {study_dir}")
                    continue

                new_folder_name = sanitize_path_part(f"{date} {person} {image_type}")
                destination = output_root / sanitize_path_part(image_type) / new_folder_name / study_dir.name
                if study_dir == destination:
                    continue
                if destination.exists():
                    errors.append(f"目标已存在，拒绝覆盖: {destination}")
                    continue
                plan.append(
                    {
                        "source": study_dir,
                        "destination": destination,
                        "old_folder": old_folder,
                    }
                )
    return plan, errors


def write_report(path: Path, plan: List[dict], errors: List[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["status", "source", "destination"])
        for item in plan:
            writer.writerow(["READY", str(item["source"]), str(item["destination"])])
        for error in errors:
            writer.writerow(["ERROR", error, ""])


def main() -> int:
    parser = argparse.ArgumentParser(description="迁移已有匿名化输出目录名称")
    parser.add_argument("output", help="匿名化输出根目录")
    parser.add_argument("mapping", help="映射表 CSV")
    parser.add_argument(
        "--date-override",
        action="append",
        default=[],
        help="仅用于旧目录缺日期：originalStudyUID=YYYYMMDD（可重复）",
    )
    parser.add_argument(
        "--subject-override",
        action="append",
        default=[],
        help="仅用于缺映射行：imageType|oldFolder=姓名（可重复）",
    )
    parser.add_argument("--report", help="迁移清单 CSV")
    parser.add_argument("--apply", action="store_true", help="实际执行；默认只预览")
    args = parser.parse_args()

    output_root = Path(args.output).resolve()
    mapping_path = Path(args.mapping).resolve()
    overrides = parse_date_overrides(args.date_override)
    subject_overrides = parse_subject_overrides(args.subject_override)
    plan, errors = build_plan(
        output_root,
        load_rows(mapping_path),
        overrides,
        subject_overrides,
    )

    if args.report:
        write_report(Path(args.report).resolve(), plan, errors)
    for item in plan:
        print(f"[移动] {item['source']} -> {item['destination']}")
    for error in errors:
        print(f"[错误] {error}")
    print(f"计划移动 Study: {len(plan)}，错误: {len(errors)}")

    if errors:
        print("存在错误，未执行任何移动。")
        return 1
    if not args.apply:
        print("当前为预览模式；确认后加 --apply 执行。")
        return 0

    old_folders = set()
    for item in plan:
        source: Path = item["source"]
        destination: Path = item["destination"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)
        old_folders.add(item["old_folder"])
    for old_folder in sorted(old_folders, key=lambda p: len(p.parts), reverse=True):
        if old_folder.exists() and not any(old_folder.iterdir()):
            old_folder.rmdir()
    print(f"迁移完成: {len(plan)} 个 Study。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
