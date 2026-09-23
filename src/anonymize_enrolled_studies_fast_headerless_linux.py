#!/usr/bin/env python3
"""Linux launcher for the opt-in headerless-DICOM compatibility engine."""

from multiprocessing import freeze_support

from anonymize_enrolled_studies_fast import main


if __name__ == "__main__":
    freeze_support()
    raise SystemExit(main(default_headerless_policy="allow"))
