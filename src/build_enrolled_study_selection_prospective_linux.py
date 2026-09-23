#!/usr/bin/env python3
"""Linux entrypoint for the prospective enrolled-Study selection workbook."""

from __future__ import annotations

import argparse
import sys

import build_enrolled_study_selection as stable
from prospective_enrolled_anonymization import (
    COHORT_NAME,
    GUARD_VERSION,
    validate_selection_args,
)


def build_parser() -> argparse.ArgumentParser:
    parser = stable.build_parser()
    parser.description = (
        "前瞻性专用：复用既有Study筛选规则，使用独立编号账本并阻止与回顾性混用。"
    )
    parser.add_argument(
        "--prospective-control-root",
        required=True,
        help="前瞻性编排控制根目录；编号账本、输出清单和人工确认表必须位于其内",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        guard = validate_selection_args(args)
        summary = stable.run(args)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    print(f"队列: {COHORT_NAME}")
    print(f"前瞻性防混用规则: {GUARD_VERSION}")
    print(f"筛选规则版本: {stable.SELECTION_RULE_VERSION}")
    print(f"中心: {summary['center_code']} {summary['center_name']}")
    print(f"入组患者: {summary['patients']}")
    print(
        f"Study: {summary['studies']}（默认进入 {summary['approved_by_default']}，"
        f"待确认 {summary['pending']}）"
    )
    print(f"匹配异常: {summary['exceptions']}")
    if summary["manual_match_workbook"]:
        print(
            "缺号人工确认: "
            f"患者{summary['manual_patients']}，Study{summary['manual_studies']}"
        )
        print(f"人工确认表: {summary['manual_match_workbook']}")
    print(
        f"前瞻性共享匿名编号账本: 共{summary['mapping_total']}人，"
        f"本次新增{summary['mapping_new']}人"
    )
    print(f"前瞻性控制根目录: {guard['control_root']}")
    print(f"编号账本: {summary['anonymous_map']}")
    print(f"输出: {summary['output']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
