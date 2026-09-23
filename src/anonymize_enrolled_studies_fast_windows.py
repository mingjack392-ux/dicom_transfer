#!/usr/bin/env python3
"""Windows launcher for the separate high-throughput enrolled anonymizer."""

from multiprocessing import freeze_support

from anonymize_enrolled_studies_fast import main


if __name__ == "__main__":
    freeze_support()
    raise SystemExit(main())
