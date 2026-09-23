#!/usr/bin/env python3
"""Reproducible synthetic benchmark for the enrolled anonymization engines.

No real patient data is read.  The faster engine runs first so the stable
engine receives any benefit from the operating-system file cache.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import anonymize_enrolled_studies as stable  # noqa: E402
import anonymize_enrolled_studies_fast as fast  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="匿名化旧版/高速版合成数据性能对比")
    parser.add_argument("--files", type=int, default=96, help="合成DICOM数量，默认96")
    parser.add_argument(
        "--pixel-kb", type=int, default=512, help="每个文件PixelData大小，默认512KB"
    )
    parser.add_argument("--processes", type=int, default=4, help="高速版进程数，默认4")
    parser.add_argument(
        "--backend",
        choices=("thread", "process"),
        default="thread",
        help="高速版并发后端，默认thread",
    )
    return parser


def make_dicom(
    path: Path,
    study_uid: str,
    series_uid: str,
    sop_uid: str,
    pixel_bytes: int,
) -> None:
    import pydicom
    from pydicom.dataset import FileDataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage

    columns = 1024
    rows = max(1, (pixel_bytes + columns - 1) // columns)
    if rows > 65535:
        raise ValueError("--pixel-kb过大，合成图像行数超过DICOM US范围")
    payload_size = rows * columns
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    dataset = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    dataset.SOPClassUID = SecondaryCaptureImageStorage
    dataset.SOPInstanceUID = sop_uid
    dataset.SpecificCharacterSet = "ISO_IR 192"
    dataset.StudyInstanceUID = study_uid
    dataset.SeriesInstanceUID = series_uid
    dataset.PatientName = "性能测试患者"
    dataset.PatientID = "PERF-KEEP"
    dataset.PatientBirthDate = "19800102"
    dataset.InstitutionName = "性能测试中心"
    dataset.StudyDate = "20250901"
    dataset.StudyTime = "101112.123"
    dataset.Rows = rows
    dataset.Columns = columns
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    dataset.BitsAllocated = 8
    dataset.BitsStored = 8
    dataset.HighBit = 7
    dataset.PixelRepresentation = 0
    dataset.PixelData = bytes((index % 251 for index in range(payload_size)))
    path.parent.mkdir(parents=True, exist_ok=True)
    pydicom.dcmwrite(path, dataset, enforce_file_format=True)


def make_study_list(path: Path, study_uid: str) -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "入组Study清单"
    sheet.append(
        [
            "中心编号",
            "中心名称",
            "匿名编号",
            "StudyInstanceUID",
            "标准访视期",
            "目标二级目录",
            "是否进入匿名化",
        ]
    )
    sheet.append([1, "性能测试中心", "001", study_uid, "6M", "001-6M", "是"])
    workbook.save(path)
    workbook.close()


def timed(function, arguments: argparse.Namespace) -> tuple[float, object]:
    hidden_output = io.StringIO()
    started = time.perf_counter()
    with contextlib.redirect_stdout(hidden_output):
        result = function(arguments)
    return time.perf_counter() - started, result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.files < 1 or args.files > 10000:
        raise ValueError("--files必须在1到10000之间")
    if args.pixel_kb < 1:
        raise ValueError("--pixel-kb必须大于0")
    if args.processes < 1 or args.processes > 16:
        raise ValueError("--processes必须在1到16之间")

    with tempfile.TemporaryDirectory(prefix="dicom-anon-fast-benchmark-") as temporary:
        root = Path(temporary)
        study_uid = "1.2.826.0.1.3680043.8.498.6101"
        series_uid = "1.2.826.0.1.3680043.8.498.6201"
        for index in range(args.files):
            sop_uid = f"1.2.826.0.1.3680043.8.498.63{index + 1:05d}"
            make_dicom(
                root / "source" / "01-H001" / study_uid / series_uid / f"{sop_uid}.dcm",
                study_uid,
                series_uid,
                sop_uid,
                args.pixel_kb * 1024,
            )
        make_study_list(root / "study-list.xlsx", study_uid)

        common = {
            "source_center": str(root / "source"),
            "study_list": str(root / "study-list.xlsx"),
            "sheet": "入组Study清单",
            "center_alias": ["性能测试"],
            "execute": True,
        }
        fast_arguments = argparse.Namespace(
            **common,
            output_root=str(root / "fast-output"),
            control_dir=str(root / "fast-control"),
            workers=1,
            processes=args.processes,
            backend=args.backend,
            max_in_flight=args.processes * 2,
            progress_every=0,
            no_resume=False,
        )
        stable_arguments = argparse.Namespace(
            **common,
            output_root=str(root / "stable-output"),
            control_dir=str(root / "stable-control"),
            workers=1,
        )

        fast_seconds, fast_result = timed(fast.run, fast_arguments)
        resume_seconds, resume_result = timed(fast.run, fast_arguments)
        stable_seconds, stable_result = timed(stable.run, stable_arguments)
        fast_written = fast_result.summary.get("已写入", 0)
        stable_written = stable_result[0].get("已写入", 0)
        if fast_written != args.files or stable_written != args.files:
            raise RuntimeError(
                f"基准验证失败: fast={fast_written}, stable={stable_written}, expected={args.files}"
            )
        if resume_result.resumed_files != args.files:
            raise RuntimeError(
                f"断点验证失败: resumed={resume_result.resumed_files}, expected={args.files}"
            )

        speedup = stable_seconds / fast_seconds if fast_seconds else float("inf")
        print(f"合成DICOM: {args.files}个，每个PixelData约{args.pixel_kb}KB")
        print(f"高速版({args.backend}:{args.processes}): {fast_seconds:.3f}秒")
        print(f"高速版断点续跑: {resume_seconds:.3f}秒")
        print(f"旧版(单Study工作线程): {stable_seconds:.3f}秒")
        print(f"本机合成测试加速比: {speedup:.2f}x")
        print(f"相对旧版完整重跑的断点加速比: {stable_seconds / resume_seconds:.2f}x")
        print("说明: 高速版先运行，旧版反而更可能受益于文件缓存；真实共享盘结果需现场验证。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
