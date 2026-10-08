# Anonymized UID selection and equipment workbook fill

Use this reference when the source is **already anonymized** in `anonymous_code/target_folder/StudyUID/SeriesUID/SOPUID.dcm`. The pre-anonymization post-transfer selector has a different contract in [selection-filter-linux.md](selection-filter-linux.md). Check current source and CLI help before giving a server command. Both tools require pydicom and openpyxl; the equipment tool requires Python 3.10+ and `requirements-dicom-equipment.txt`.

## Select delivery DICOM by Series/SOP

`src/filter_anonymized_dicom_by_uid_selection.py` reads only rows with `是否进入匿名化=是`. Required routing columns include `匿名编号`, `目标二级目录`, `StudyInstanceUID`, `SeriesUID`/`SeriesInstanceUID`, and `SOPUID`/`SOPInstanceUID`; `modality` is audit-only. `SOPUID=NA`, `N/A`, `/`, or blank selects the **entire Series**. A concrete SOP UID selects exactly that instance and never falls back to the whole Series. Exact duplicate rules are deduplicated. A missing or invalid rule blocks itself, not every rule. Study/Series/SOP header UIDs must match the path; an unreadable or mismatching file blocks that whole Series rule.

The source and target DICOM roots and separate control directory must not overlap. Preview is the default: it scans headers and writes audit CSVs, but does not copy DICOM. After reviewing `匿名化后UID筛选明细_*.csv` and `匿名化后UID筛选汇总_*.csv`, add `--execute` only if copying is requested. The target preserves the anonymized relative path; existing identical SHA-256 targets are `duplicate_same`. Existing different targets are `conflict`: **no overwrite and no `.conflict.dcm` copy** in this workflow. Exit code `1` can mean a completed run with unresolved rows, not necessarily no output; inspect audits. Test: `tests/test_filter_anonymized_dicom_by_uid_selection.py`.

## Fill Manufacturer and model in a new workbook

`src/fill_dicom_equipment.py` takes the **filtered DICOM root**, original `.xlsx`, and a new output `.xlsx`. It inserts `Manufacturer (0008,0070)` and `ManufacturerModelName (0008,1090)` immediately after `modality`. It does not copy or change DICOM or overwrite the input workbook. Preview writes `设备信息回填明细_*.csv`, `设备信息文件审计_*.csv`, and `设备信息回填汇总_*.json`, but no result workbook; `--execute` writes a new, previously nonexistent workbook. There is no `--copy-workers` option.

It uses the same exact SOP versus whole-Series rule, but scans only files currently present in the filtered tree; a Series scan does not prove upstream selection completeness. It validates UIDs and leaves unsupported, missing, unreadable, or multi-valued results blank with audit status. A single nonempty value with some missing tags may be filled with a `partial_missing` warning. Never infer a model from vendor, substitute secondary-capture equipment tags, or claim these tags necessarily identify the original acquisition device. Test: `tests/test_fill_dicom_equipment.py`.

For long Linux runs, keep each preview and execute log and the Python status, e.g. `python3 ... 2>&1 | tee "$run_log"` followed immediately in Bash by `run_exit=${PIPESTATUS[0]}`. A `tee` success alone does not prove the Python program succeeded. Confirm audit counts and output file counts before calling a run complete.
