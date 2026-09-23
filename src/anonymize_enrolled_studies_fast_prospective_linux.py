#!/usr/bin/env python3
"""Linux launcher for prospective high-throughput enrolled anonymization."""

from __future__ import annotations

import argparse
import sys
from multiprocessing import freeze_support

import anonymize_enrolled_studies_fast as fast
from enrolled_anonymization_common import text
from prospective_enrolled_anonymization import (
    COHORT_NAME,
    GUARD_VERSION,
    validate_fast_args,
)


def build_parser() -> argparse.ArgumentParser:
    parser = fast.build_parser()
    parser.description = (
        "前瞻性专用高速匿名化：复用既有匿名化核心，强制使用独立编号账本、"
        "交付目录、控制目录和断点状态。默认仅预览。"
    )
    parser.add_argument(
        "--prospective-root",
        required=True,
        help="前瞻性匿名化根目录；其下必须使用01.交付影像和02.内部控制文件",
    )
    parser.add_argument(
        "--prospective-control-root",
        required=True,
        help="前瞻性编排控制根目录；Study清单和共享编号账本必须位于其内",
    )
    parser.add_argument(
        "--anonymous-map",
        required=True,
        help="前瞻性所有中心共用的患者匿名编号映射.xlsx",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        guard = validate_fast_args(args)
        result = fast.run(args)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    print(f"队列: {COHORT_NAME}")
    print(f"前瞻性防混用规则: {GUARD_VERSION}")
    print(f"前瞻性已批准Study: {guard['approved_studies']}")
    print(f"业务规则版本: {fast.stable.RULE_VERSION}")
    print(f"执行引擎: {fast.ENGINE_VERSION}")
    print(f"模式: {'执行' if args.execute else '预览'}")
    for status, count in sorted(result.summary.items()):
        print(f"{status}: {count}")
    print(f"本次实际处理DICOM: {result.processed_files}")
    print(f"断点跳过DICOM: {result.resumed_files}")
    if result.audit_path:
        print(f"审计清单: {result.audit_path.resolve()}")
        print(f"验证报告: {result.verification_path.resolve()}")
        print(f"源UID问题清单: {result.uid_issue_path.resolve()}")
        print(f"断点状态: {result.checkpoint_path.resolve()}")
    else:
        print("预览模式未写入DICOM、控制文件或断点状态；确认后增加--execute。")
    return 2 if any(
        text(row.get("状态")) in fast.FAILED_STATUSES for row in result.audits
    ) else 0


if __name__ == "__main__":
    freeze_support()
    raise SystemExit(main())
