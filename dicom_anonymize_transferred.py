#!/usr/bin/env python3
"""Compatibility launcher for the relocated anonymization program."""

from pathlib import Path
import runpy


if __name__ == "__main__":
    target = Path(__file__).resolve().parent / "src" / "dicom_anonymize_transferred.py"
    runpy.run_path(str(target), run_name="__main__")
