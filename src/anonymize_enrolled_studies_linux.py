#!/usr/bin/env python3
"""Linux entrypoint for reviewed enrolled-patient DICOM anonymization.

Keep this file in the same directory as ``anonymize_enrolled_studies.py`` and
``enrolled_anonymization_common.py``.  Preview remains the default; permanent
output still requires the explicit ``--execute`` option.
"""

from anonymize_enrolled_studies import main


if __name__ == "__main__":
    raise SystemExit(main())
