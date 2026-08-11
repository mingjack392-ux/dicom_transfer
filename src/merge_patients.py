#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# 识别“2026-05-18 王玉梅 复查”这样的一级目录
DATE_NAME_PATTERN = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<name>.+?)\s*$"
)

# 只移除末尾的“复查”
FOLLOWUP_PATTERN = re.compile(r"\s+复查\s*$")


class AuditLog:
    """实时写入CSV日志，每条记录立即刷新。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open(
            "w",
            newline="",
            encoding="utf-8-sig",
        )
        self.writer = csv.writer(self.stream)
        self.writer.writerow([
            "时间",
            "操作",
            "来源",
            "目标",
            "说明",
        ])
        self.stream.flush()

    def write(
        self,
        action: str,
        source: Path,
        destination: Path,
        detail: str = "",
    ) -> None:
        self.writer.writerow([
            datetime.now().isoformat(timespec="seconds"),
            action,
            str(source),
            str(destination),
            detail,
        ])
        self.stream.flush()

    def close(self) -> None:
        self.stream.close()

        try:
            os.chmod(str(self.path), 0o644)
        except OSError:
            pass


def path_exists(path: Path) -> bool:
    """包括失效符号链接在内的路径存在判断。"""
    return os.path.lexists(str(path))


def patient_name_from_folder(folder_name: str) -> Optional[str]:
    """
    从一级目录名提取患者姓名。

    示例：
    2025-11-25 王玉梅       -> 王玉梅
    2026-05-18 王玉梅 复查  -> 王玉梅
    """
    match = DATE_NAME_PATTERN.fullmatch(folder_name)

    if not match:
        return None

    patient_name = match.group("name").strip()
    patient_name = FOLLOWUP_PATTERN.sub("", patient_name).strip()

    return patient_name or None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as stream:
        while True:
            block = stream.read(4 * 1024 * 1024)

            if not block:
                break

            digest.update(block)

    return digest.hexdigest()


def compare_files(
    first: Path,
    second: Path,
) -> Tuple[bool, str, str]:
    """比较两个文件是否完全相同，并返回双方SHA256。"""

    if first.stat().st_size != second.stat().st_size:
        return False, "", ""

    first_hash = sha256_file(first)
    second_hash = sha256_file(second)

    return first_hash == second_hash, first_hash, second_hash


def safe_tag(value: str) -> str:
    cleaned = re.sub(
        r'[<>:"/\\|?*\s]+',
        "_",
        value,
    )
    return cleaned.strip("_") or "unknown_source"


def conflict_path(
    destination: Path,
    incoming: Path,
    source_folder: str,
    digest: str = "",
) -> Path:
    """
    为同名但不同内容的数据生成不覆盖的保留路径。
    """

    tag = safe_tag(source_folder)

    if incoming.is_dir() and not incoming.is_symlink():
        base_name = (
            destination.name
            + "__conflict_from_"
            + tag
        )
        candidate = destination.parent / base_name
    else:
        suffix = incoming.suffix
        stem = incoming.stem

        if not digest and incoming.is_file():
            digest = sha256_file(incoming)[:12]

        digest_part = "_" + digest[:12] if digest else ""

        candidate = destination.parent / (
            stem
            + "__conflict_from_"
            + tag
            + digest_part
            + suffix
        )

    original_candidate = candidate
    number = 1

    while path_exists(candidate):
        if incoming.is_dir() and not incoming.is_symlink():
            candidate = original_candidate.with_name(
                original_candidate.name + "_" + str(number)
            )
        else:
            candidate = original_candidate.with_name(
                original_candidate.stem
                + "_"
                + str(number)
                + original_candidate.suffix
            )

        number += 1

    return candidate


def increase_counter(
    counters: Dict[str, int],
    name: str,
) -> None:
    counters[name] += 1
    counters["processed_items"] += 1

    # 合并大量文件时，每处理500项显示一次进度
    if counters["processed_items"] % 500 == 0:
        print(
            "    已处理文件/目录：{0}，移动：{1}，"
            "去重：{2}，冲突保留：{3}".format(
                counters["processed_items"],
                counters["moved"],
                counters["duplicates"],
                counters["conflicts"],
            ),
            flush=True,
        )


def preserve_conflict(
    incoming: Path,
    target: Path,
    source_folder: str,
    logger: AuditLog,
    counters: Dict[str, int],
    reason: str,
    digest: str = "",
) -> None:
    preserved = conflict_path(
        target,
        incoming,
        source_folder,
        digest,
    )

    shutil.move(str(incoming), str(preserved))
    increase_counter(counters, "conflicts")

    logger.write(
        reason,
        incoming,
        preserved,
        "原目标={0}".format(target),
    )


def merge_directory(
    source: Path,
    destination: Path,
    source_folder: str,
    logger: AuditLog,
    counters: Dict[str, int],
) -> None:
    """
    递归合并目录。

    规则：
    1. 目标不存在：直接移动；
    2. 两边都是目录：递归合并；
    3. 同名文件内容相同：SHA256确认后删除重复副本；
    4. 同名文件内容不同：改名保留，绝不覆盖；
    5. 文件与目录冲突：改名保留。
    """

    entries = sorted(
        list(source.iterdir()),
        key=lambda item: item.name,
    )

    for incoming in entries:
        target = destination / incoming.name

        if not path_exists(target):
            shutil.move(str(incoming), str(target))
            increase_counter(counters, "moved")

            logger.write(
                "移动",
                incoming,
                target,
            )
            continue

        # 避免递归进入符号链接
        if incoming.is_symlink() or target.is_symlink():
            preserve_conflict(
                incoming,
                target,
                source_folder,
                logger,
                counters,
                "符号链接冲突，改名保留",
            )
            continue

        if incoming.is_dir() and target.is_dir():
            merge_directory(
                incoming,
                target,
                source_folder,
                logger,
                counters,
            )

            if incoming.exists() and not any(incoming.iterdir()):
                incoming.rmdir()

                logger.write(
                    "删除已合并空目录",
                    incoming,
                    target,
                )

            continue

        if incoming.is_file() and target.is_file():
            identical, incoming_hash, target_hash = compare_files(
                incoming,
                target,
            )

            if identical:
                source_path_before_delete = Path(str(incoming))
                incoming.unlink()
                increase_counter(counters, "duplicates")

                logger.write(
                    "相同文件去重",
                    source_path_before_delete,
                    target,
                    "SHA256={0}".format(incoming_hash),
                )
            else:
                if not incoming_hash:
                    incoming_hash = sha256_file(incoming)

                preserve_conflict(
                    incoming,
                    target,
                    source_folder,
                    logger,
                    counters,
                    "同名不同内容，改名保留",
                    incoming_hash,
                )

            continue

        # 一个是文件、另一个是目录
        preserve_conflict(
            incoming,
            target,
            source_folder,
            logger,
            counters,
            "文件与目录冲突，改名保留",
        )

    if source.exists() and not any(source.iterdir()):
        source.rmdir()

        logger.write(
            "删除已合并空目录",
            source,
            destination,
        )


def build_groups(root: Path) -> Dict[str, List[Path]]:
    """
    扫描根目录的一级子目录，根据姓名分组。
    """
    groups: Dict[str, List[Path]] = {}

    for folder in sorted(
        root.iterdir(),
        key=lambda item: item.name,
    ):
        if not folder.is_dir():
            continue

        patient_name = patient_name_from_folder(folder.name)

        # “王玉梅”这种已经整理好的目录不作为改名来源
        if patient_name is None:
            continue

        groups.setdefault(patient_name, []).append(folder)

    return groups


def show_plan(
    root: Path,
    groups: Dict[str, List[Path]],
) -> None:
    print("=" * 72)
    print("患者目录归类预览")
    print("当前阶段不会修改任何文件或目录")
    print("=" * 72)

    for patient_name in sorted(groups):
        sources = groups[patient_name]
        target = root / patient_name

        print()
        print("患者：{0}".format(patient_name))
        print("目标：{0}".format(target))

        if target.exists():
            print("状态：姓名目录已存在，将进行安全合并")
        else:
            print("状态：将创建姓名目录")

        for source in sources:
            print(
                "  {0}  ->  {1}".format(
                    source.name,
                    patient_name,
                )
            )

    print()
    print("患者数量：{0}".format(len(groups)))
    print(
        "待处理一级目录：{0}".format(
            sum(len(items) for items in groups.values())
        )
    )

    print()
    print("重要提醒：当前按照姓名合并。")
    print("如存在同名但并非同一人的患者，应在执行前停止并人工区分。")


def execute_merge(
    root: Path,
    groups: Dict[str, List[Path]],
    log_path: Path,
) -> Dict[str, int]:
    counters = {
        "renamed": 0,
        "merged": 0,
        "moved": 0,
        "duplicates": 0,
        "conflicts": 0,
        "processed_items": 0,
    }

    total_sources = sum(
        len(sources) for sources in groups.values()
    )
    current_source = 0

    logger = AuditLog(log_path)

    try:
        for patient_name in sorted(groups):
            sources = groups[patient_name]
            target = root / patient_name

            for source in sources:
                current_source += 1

                print()
                print(
                    "[{0}/{1}] 正在归类：{2}  ->  {3}".format(
                        current_source,
                        total_sources,
                        source.name,
                        patient_name,
                    ),
                    flush=True,
                )

                if not source.exists():
                    print(
                        "    跳过：来源目录已经不存在",
                        flush=True,
                    )
                    continue

                # 目标不存在时直接把第一个目录改名
                if not target.exists():
                    source_before_rename = Path(str(source))
                    source.rename(target)
                    counters["renamed"] += 1

                    logger.write(
                        "一级目录改名",
                        source_before_rename,
                        target,
                    )

                    print(
                        "    完成：一级目录已改名",
                        flush=True,
                    )
                    continue

                before_moved = counters["moved"]
                before_duplicates = counters["duplicates"]
                before_conflicts = counters["conflicts"]

                merge_directory(
                    source,
                    target,
                    source.name,
                    logger,
                    counters,
                )

                counters["merged"] += 1

                print(
                    "    完成：移动 {0}，去重 {1}，"
                    "冲突保留 {2}".format(
                        counters["moved"] - before_moved,
                        counters["duplicates"] - before_duplicates,
                        counters["conflicts"] - before_conflicts,
                    ),
                    flush=True,
                )

    finally:
        logger.close()

    return counters


def main() -> int:
    parser = argparse.ArgumentParser(
        description="按患者姓名归类并安全合并一级目录"
    )

    parser.add_argument(
        "root",
        help="患者数据根目录",
    )

    parser.add_argument(
        "--execute",
        action="store_true",
        help="实际执行；不添加时只显示预览",
    )

    parser.add_argument(
        "--log",
        help="CSV明细日志路径",
    )

    args = parser.parse_args()

    root = Path(args.root).resolve()

    if not root.is_dir():
        print(
            "错误：目录不存在：{0}".format(root),
            file=sys.stderr,
        )
        return 1

    if root == Path("/"):
        print(
            "错误：禁止处理系统根目录",
            file=sys.stderr,
        )
        return 1

    groups = build_groups(root)
    show_plan(root, groups)

    if not groups:
        print()
        print("没有找到需要整理的日期患者目录。")
        return 0

    if not args.execute:
        print()
        print("当前仅为预览，没有修改任何目录。")
        return 0

    print()
    answer = input(
        "即将实际改名和合并，确认无误请输入 MERGE："
    ).strip()

    if answer != "MERGE":
        print("输入不匹配，操作已取消。")
        print("没有修改任何目录。")
        return 1

    if args.log:
        log_path = Path(args.log).resolve()
    else:
        timestamp = datetime.now().strftime(
            "%Y%m%d_%H%M%S"
        )
        log_path = Path(
            "/tmp/patient_merge_{0}.csv".format(timestamp)
        )

    print()
    print("开始实际执行")
    print("CSV明细日志：{0}".format(log_path))
    print()

    try:
        counters = execute_merge(
            root,
            groups,
            log_path,
        )
    except KeyboardInterrupt:
        print()
        print("收到Ctrl+C，程序已经中止。")
        print("已经完成的操作不会自动回滚，请检查CSV日志。")
        print("未处理的源目录仍然保留。")
        return 130
    except Exception as exc:
        print()
        print(
            "执行失败：{0}".format(exc),
            file=sys.stderr,
        )
        print("请检查CSV日志和现有目录后再继续。")
        return 1

    print()
    print("=" * 72)
    print("归类完成")
    print("直接改名：{0}".format(counters["renamed"]))
    print("合并目录：{0}".format(counters["merged"]))
    print("移动内容：{0}".format(counters["moved"]))
    print("相同文件去重：{0}".format(counters["duplicates"]))
    print("同名不同内容保留：{0}".format(counters["conflicts"]))
    print("CSV明细日志：{0}".format(log_path))
    print("=" * 72)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())