# Maintained workflows

Locate the project checkout as described in `SKILL.md`. Use current code, CLI help, and `docs/PROJECT_BASELINE.md` when the checkout has changed. Paths below are relative to the project root unless a deployed package is explicitly named. Example data roots are placeholders.

## Patient inventory

Read DICOM headers and print patient counts, PatientIDs, and names without reading Pixel Data or copying files:

```powershell
python .\src\scan_dicom_patients.py "D:\input" --workers 8
```

Linux uses the same script with `python3`.

## V3 formal transfer

V3 is the default for new production batches because it scans the full batch first, routes mixed-source patients, maintains identity state across batches, and records uncertain identity relationships.

Windows:

```powershell
python .\src\dicom_organize_v3.py `
  "D:\input" `
  "D:\output" `
  --workers 1 `
  --copy-workers 2
```

Add `--manifest` only when a file-level transfer list is needed; it increases workbook size and generation time. Add `--durable` only when per-file `fsync` is required, because it is slower.

Linux:

```bash
python3 -m pip install --user -r requirements-linux.txt

python3 ./src/dicom_organize_v3_linux.py "/data/input" "/data/output" \
  --workers 1 --copy-workers 2
```

Linux report recovery after DICOM copying and state generation have already completed:

```bash
python3 ./src/dicom_organize_v3_linux.py "/data/input" "/data/output" \
  --report-only
```

`--report-only` requires `.dicom_v3_state/identity_mapping.json` and `study_inventory.json`. It rebuilds the workbook without rescanning or copying, so it cannot apply a new DICOM classification rule.

V3 normally produces:

- `影像检查明细_V3.xlsx`;
- `.dicom_v3_state/identity_mapping.json`;
- `.dicom_v3_state/study_inventory.json`;
- optional CSV compatibility outputs with `--keep-csv`;
- optional file-level manifest with `--manifest`.

The workbook includes Study details, patient identity mapping, identity review, source-folder audit, transfer exceptions, field definitions, and optionally a manifest.

## V2 simple transfer

Use V2 only when simple `PatientName_PatientID/Study/Series/SOP.dcm` routing is desired and cross-PatientID natural-person grouping is not needed:

```powershell
python .\src\dicom_organize_v2.py "D:\input" "D:\output"
```

Outputs are `影像检查明细.xlsx`, `待确认记录.csv`, and optionally `转存清单.csv` with `--manifest`.

## Screening-number transfer and center image-web

For an individual screening folder or a center of such folders, use `src/dicom_transfer_by_screening.py` (the latter with `--batch`). It previews identity and UID/path issues by default; `--execute` copies only after its identity gate passes. See [center-and-screening.md](center-and-screening.md) for package inputs and audits. Do not equate this with V3's cross-batch identity state.

For transferred data plus an external center workbook containing surgery dates, `src/analyze_transferred_dicom_by_center.py` (Windows) or `src/analyze_transferred_dicom_by_center_linux.py` builds image-web analysis. The default is a scan/statistics preview; `--write --output <new.xlsx>` creates the report. A single-center input needs `--single-center`; multiple immediate center children omit it. An existing output needs reviewed `--overwrite`. See [center-and-screening.md](center-and-screening.md). Periods are joined to the external surgery-date table, not inferred from DICOM alone.

## Anonymize organized data

Preview first:

```powershell
python .\src\dicom_anonymize_transferred.py `
  "D:\organized_input" `
  "D:\anonymous_output" `
  --single-patient
```

For a root containing multiple patient directories, omit `--single-patient`. After the user reviews the plan and explicitly authorizes the write, rerun with `--execute`. Do not add `--overwrite` unless replacement of different existing targets is explicitly intended and reviewed.

The mapping and audit files are sensitive because they permit re-identification. Store them separately on controlled storage.

## Anonymize reviewed enrolled Studies

Use this workflow when an approved enrollment workbook selects exact StudyInstanceUID values and assigns anonymous patient/visit directories. First build and review the center's Study list and shared anonymous-number ledger with `src/build_enrolled_study_selection.py` or `_linux.py`; list generation is a write, not an anonymizer preview. Read [enrollment-variants.md](enrollment-variants.md). The anonymizer processes the entire approved Study, not only its listed Series/SOP instances. Inspect current source and CLI help before constructing a command.

- Stable entrypoints: `src/anonymize_enrolled_studies.py` and `src/anonymize_enrolled_studies_linux.py`.
- High-speed shared core: `src/anonymize_enrolled_studies_fast.py`.
- High-speed launchers: `src/anonymize_enrolled_studies_fast_windows.py` and `src/anonymize_enrolled_studies_fast_linux.py`.
- `.xlsx`/`.xlsm` lists need `openpyxl`; `xlrd` is needed only for a legacy `.xls` list.

Linux high-speed example, preview by default:

```bash
python3 ./src/anonymize_enrolled_studies_fast_linux.py \
  --source-center "/data/source/center" \
  --study-list "/data/control/center_入组Study筛选表.xlsx" \
  --output-root "/data/delivery/01.交付影像" \
  --control-dir "/data/delivery/02.内部控制文件" \
  --center-alias "中心简称" \
  --workers 4 \
  --dicom-workers 4 \
  --backend thread
```

Add `--uid-policy preserve` only for the explicit nonstandard-UID policy described in [rules-and-safety.md](rules-and-safety.md). Add `--execute` only after the user authorizes the real write. Long jobs should run in `tmux` or an equivalent detached session; direct jump-host terminals may terminate the process when the web session closes.

The fast workflow produces:

- `匿名化高速版处理清单_*.csv` for all file outcomes;
- `匿名化高速版验证报告_*.csv` for blocking or actual verification failures;
- `匿名化高速版源UID问题清单_*.csv` for warn-only UID defects in preserve mode;
- `02.内部控制文件/_fast_state/checkpoint_*.jsonl` for verified resume state.

Do not delete a partial delivery tree or its checkpoint simply because a run was interrupted. Re-run the same version and policy; changed sources or targets are reprocessed, unchanged current-version successes are skipped, and existing identical output is retained. A first preserve-mode run may deliberately recheck successes from an older checkpoint schema to reconstruct complete UID issue auditing.

Deploy a new engine in a versioned directory instead of overwriting the previous package. After upload, verify the archive SHA-256, extract it into a new directory, and run `--help` to confirm the expected `--uid-policy` values before starting. In deployed release packages the launcher can be at the package root rather than `src/`; check the actual extracted layout. Validate both the package core hash and launcher import, not only the repository source. For prospective data or source-specific exceptions, use [enrollment-variants.md](enrollment-variants.md), never a generic flag added to every run.

## Select anonymized delivery and fill equipment workbook

After Study-level anonymization, `src/filter_anonymized_dicom_by_uid_selection.py` uses an approved Excel list to copy whole Series (`SOPUID=NA`) or exact SOP instances from the anonymized delivery tree into a separate filtered tree. This is distinct from `src/filter_transferred_dicom_by_selection.py`. Its default preview writes control audits; `--execute` copies. An existing different target is reported as `conflict` and not overwritten or copied under a conflict filename. See [anonymized-selection-equipment.md](anonymized-selection-equipment.md).

To add `Manufacturer (0008,0070)` and `ManufacturerModelName (0008,1090)` after `modality` in a *new* workbook, use `src/fill_dicom_equipment.py` against the already filtered DICOM tree. Preview writes audits only; `--execute` writes the workbook, never DICOM. Missing/multiple values need review and must not be guessed. See the same reference.

## Anonymize and classify a raw directory or ZIP

```powershell
python .\src\dicom_anonymize_classify.py "D:\input_or_zip" "D:\anonymous_output"
```

This workflow can classify by source package and supports stable UID remapping. It does not inspect burned-in pixel text or faces. Default behavior does not overwrite existing anonymized DICOM files. Review ZIP expansion limits before raising `--max-uncompressed-gb`.

## One-time utilities

- `src/merge_patients.py`: merge first-level `日期 姓名` folders; preview is default, execution requires `--execute` and confirmation. Use an explicit CSV log.
- `src/migrate_existing_output_names.py`: preview an old anonymous-output naming migration; use `--apply` only after reviewing the report.
- `scripts/linux/copy_dicom_from_list.sh`: copy absolute paths from a text list without overwriting same-name targets.
- Environment-specific deletion scripts are not distributed with the repository skill. A historical script name or local guide does not establish an available or authorized deletion workflow.

Run Bash scripts with `bash script.sh`, not `sh script.sh`.

## Verification

Targeted tests:

```powershell
python -m unittest tests.test_scan_dicom_patients -v
python -m unittest tests.test_dicom_organize_v2 -v
python -m unittest tests.test_dicom_organize_v3 -v
python -m unittest tests.test_dicom_organize_v3_linux -v
python -m unittest tests.test_dicom_anonymize_transferred -v
python -m unittest tests.test_dicom_anonymize_classify -v
python -m unittest tests.test_enrolled_anonymization_fast tests.test_enrolled_anonymization_workflow -v
python -m unittest tests.test_filter_anonymized_dicom_by_uid_selection tests.test_fill_dicom_equipment tests.test_prospective_enrolled_anonymization -v
```

Full suite:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```
