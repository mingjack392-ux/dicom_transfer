#!/usr/bin/env python3
"""Linux entrypoint for the enrolled-patient Study selection workbook.

Keep this file in the same directory as ``build_enrolled_study_selection.py``
and ``enrolled_anonymization_common.py``.  Business rules live in the shared
core so Windows and Linux produce the same selection result.
"""

from build_enrolled_study_selection import main


if __name__ == "__main__":
    raise SystemExit(main())
