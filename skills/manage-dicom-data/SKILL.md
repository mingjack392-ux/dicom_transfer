---
name: manage-dicom-data
description: Choose and maintain this repository's no-database DICOM workflows for Windows/Linux transfer, identity review, enrollment Study selection, retrospective or prospective anonymization, Excel UID filtering, image-web reports, and equipment workbook fill. Use for operating or changing this DICOM Transfer project; verify current source and keep patient data out of version control.
metadata:
  short-description: 安全执行DICOM扫描、转存、筛选、分类、报表与匿名化
---

# Manage DICOM Data

Use a checkout of the DICOM Transfer repository containing `src/`, `tests/`, and `docs/PROJECT_BASELINE.md`. All project paths below are relative to that checkout, even when this skill has been installed elsewhere. Locate the checkout from the user's workspace; if it is unavailable, ask for its location before constructing executable project commands. Read current source and `--help` when parameters or behavior matter. This skill includes its operational references; local center-specific deployment guides are not required.

## Route the request

- Count patients or inspect names and PatientIDs without copying: use `src/scan_dicom_patients.py`.
- Perform a formal new transfer, handle mixed source folders, or group one natural person across PatientIDs: prefer V3. Use `src/dicom_organize_v3.py` on Windows and `src/dicom_organize_v3_linux.py` on Linux.
- Transfer a screening-number patient or center folder after identity preflight: use `src/dicom_transfer_by_screening.py` (`--batch` for a center). For mixed directory/ZIP/RAR packages, use `web_registry/dicom_transfer_screening_packages_windows.py` or `_linux.py`. Read [references/center-and-screening.md](references/center-and-screening.md).
- Perform a simple one-batch transfer where cross-PatientID identity grouping is not required: use `src/dicom_organize_v2.py`.
- Build an image-web report from transferred data plus an external center workbook: use `src/analyze_transferred_dicom_by_center.py` on Windows or `_linux.py` on Linux. Preview first; `--write --output` creates the workbook. Read [references/center-and-screening.md](references/center-and-screening.md).
- Filter already transferred patient folders from an external Excel list of Series/SOP UIDs: use `src/filter_transferred_dicom_by_selection.py` and read [references/selection-filter-linux.md](references/selection-filter-linux.md). Keep this downstream selection separate from V2/V3 transfer and DICOM-derived classification.
- Rebuild a Linux V3 workbook only after a completed scan has valid `.dicom_v3_state`: use `--report-only`. It cannot read DICOM again or apply a changed classification rule.
- Anonymize already organized data: use `src/dicom_anonymize_transferred.py`, which previews by default. Add `--execute` only when the user explicitly asks to write the anonymized output after reviewing the plan.
- Build the reviewed, center-specific enrollment Study list first with `src/build_enrolled_study_selection.py` (Windows) or `_linux.py` (Linux). The shared anonymous-number ledger is sensitive and must not be split per center or concurrently updated. This list selects whole Studies, not only its displayed Series/SOP rows. Read [references/enrollment-variants.md](references/enrollment-variants.md).
- Anonymize approved retrospective Studies with `src/anonymize_enrolled_studies_fast_windows.py` or `_linux.py`. Strict UID and single-PatientID gates are defaults. Route proven headerless files or a specifically human-confirmed multi-PatientID Study to the exceptional entrypoints only after reading [references/enrollment-variants.md](references/enrollment-variants.md).
- For prospective `NN-QNNN` enrollment, use the *separate* prospective list builder and anonymizer, ledger, output, control, and checkpoint roots; read [references/enrollment-variants.md](references/enrollment-variants.md). Never point these at retrospective roots.
- Filter already anonymized delivery DICOM by approved Excel Series/SOP rules: use `src/filter_anonymized_dicom_by_uid_selection.py`, not the post-transfer selector. To add equipment fields to the selected workbook without touching DICOM, use `src/fill_dicom_equipment.py`. Read [references/anonymized-selection-equipment.md](references/anonymized-selection-equipment.md).
- Anonymize a raw directory or ZIP and classify it by source package: use `src/dicom_anonymize_classify.py`; treat this as a separate workflow from V3 organization.
- Treat directory merging, migrations, deletion from a path list, and overwrite options as separate higher-risk operations. Inspect the exact script and targets, preview first, and obtain clear authorization before execution.

Read [references/workflows.md](references/workflows.md) for maintained entrypoints, commands, dependencies, and output artifacts. Read [references/rules-and-safety.md](references/rules-and-safety.md) when evaluating paths, duplicates, patient identity, Study aggregation, modality classification, anonymization, or a rule change.

## Preserve these invariants

- Keep the source dataset unchanged. Transfer and anonymization write to a separate target.
- Never silently overwrite a UID collision. Same target plus identical content is a duplicate. Different content requires review; whether a conflict copy is retained depends on the exact workflow (the anonymized UID filter does **not** create one).
- Do not decide two people are the same from a name or a single PatientID alone. Preserve V3's conservative identity evidence and surface uncertain or conflicting relationships for human review.
- Preserve `.dicom_v3_state/identity_mapping.json` and `study_inventory.json`; they provide cross-batch continuity and are not disposable cache files.
- Keep identifiable workbooks, mappings, logs, and paths on controlled storage. Do not expose patient data in examples, commits, or external services.
- Keep enrollment Study scope distinct from subsequent Series/SOP delivery selection. `SOPUID=NA` means an entire Series rule, not proof of 3D-DSA modality.
- In UID-preservation mode, preserve every parsed UI element independently, including file-meta and dataset values that disagree. Never disable the full before/after UID snapshot check merely to make a legacy file pass. Missing routing UIDs, path-unsafe UID components, Study mismatch, or unreadable DICOM remain blocking conditions.
- Do not infer surgery time or label intraoperative, postoperative 6-month, or postoperative 12-month columns from DICOM alone. Those require an external surgery-time list; center image-web derives periods only after joining that external table.
- Do not run `src/dicom_organize.py`, `src/dicom_organize_linux.py`, `src/dicom_organizer.py`, or `scripts/windows/run_all.ps1` for a new task; those are legacy database workflows.

## When changing the project

Inspect the current implementation before editing. Keep V3 reusing V2's transfer and Series classification logic, and keep the Linux entrypoint reusing V3. A semantic classification or identity change must update the applicable rule versions, Windows and Linux workbook field descriptions, user documentation, and regression tests.

After a classification rule change, explain that existing workbooks and unscanned historical state are not automatically reclassified. Re-scan the complete relevant original dataset; `--report-only` is insufficient.

Validate narrowly first, then run the relevant regression suite. For changes shared by V2, V3, and Linux, use:

```powershell
python -m unittest tests.test_dicom_organize_v2 tests.test_dicom_organize_v3 tests.test_dicom_organize_v3_linux -v
```

## Handoff

Report the exact entrypoint and command, source and target scope, whether the run was preview or execution, relevant selected/copied/duplicate/conflict/quarantine/error counts, generated workbook/audit/state paths, and any identity or DICOM exceptions requiring review. For Linux `tee` pipelines retain the Python exit status via `${PIPESTATUS[0]}`. Do not call a package build, local test, or command proposal a completed server run; check the server exit status, logs, audit files, and output counts.
