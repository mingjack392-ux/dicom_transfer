# Enrollment Study anonymization variants

Use current source, CLI help, and the repository's `docs/PROJECT_BASELINE.md` before selecting a variant. The reviewed workbook chooses **whole StudyInstanceUIDs**; later Series/SOP filtering is a different step. The builder may write a sensitive shared anonymous-number ledger, so it is not a read-only preview. Never create a second retrospective ledger for one center, concurrently allocate numbers, or use a screening number as a missing hospital number.

## Standard retrospective path

- Build a per-center reviewed list with `src/build_enrolled_study_selection.py` (Windows) or `_linux.py` (Linux), using sequence, enrolled-patient, and registry workbooks plus one shared retrospective `患者匿名编号映射.xlsx`. Inspect `匹配异常`, time-period review, and `是否进入匿名化`; an existing output needs an explicit `--overwrite` only after review.
- Preview approved Studies with `src/anonymize_enrolled_studies_fast_windows.py` or `_linux.py` (or maintained non-fast counterparts). Default UID policy is strict. `--uid-policy preserve` is an explicit compatibility decision for readable legacy UIDs: record defects, keep all parsed UIDs, and continue to block missing/path-unsafe UIDs and Study mismatches. See [rules-and-safety.md](rules-and-safety.md).
- Execute only on request after preview review. Keep the existing delivery, `_fast_state` checkpoint, verification reports, and `_conflicts`; resume with the same version/policy rather than deleting partial outputs. A multi-PatientID Study blocks by default.

Example list generation from the repository root (this writes control workbooks):

```bash
python3 src/build_enrolled_study_selection_linux.py \
  --center "示例中心" \
  --sequence-workbook "/data/control/sequences.xlsx" \
  --enrolled-workbook "/data/control/enrolled.xlsx" \
  --registry-workbook "/data/control/registry.xlsx" \
  --anonymous-map "/data/control/患者匿名编号映射.xlsx" \
  --output "/data/control/reviewed-studies.xlsx"
```

The ledger reuses assignments by center and subject screening number. Normal patient matching uses center, hospital number, and name; missing-name fallback requires a unique hospital number within the center. Missing hospital numbers require `--manual-match-workbook` with explicit `受试者筛选号`, `患者`, `StudyInstanceUID`, `是否确认同一人`, and `确认依据`, rather than a name-only guess. Retain this evidence in internal control storage.

Visit labels come from aggregating the supplied sequence periods by Study: all preoperative, all intraoperative, or both map to their corresponding folders. Follow-up `3 <= months < 9` maps to 6M. For each patient, only the Study closest to 12 months among candidates with `months >= 9` is selected as 12M; equal-distance ties and mixed/unrecognized periods need review. Check the current implementation before changing this business rule.

This project's enrolled anonymizer preserves PatientID, non-birth date/time values, all UID values, and PixelData. It removes PatientName, PatientBirthDate, InstitutionName, and matching center text recursively. This is reversible pseudonymization under the project rules, with separate pixel-privacy review. Source DICOM remains read-only; delivery paths are `anonymous_code/target_folder/StudyUID/SeriesUID/SOPUID.dcm`. Identifying mappings stay in the independent control directory.

## Exceptional retrospective sources

- Verified missing preamble/`DICM`/File Meta in implicit-VR little-endian `.dcm` files: use the separate `src/anonymize_enrolled_studies_fast_headerless_windows.py` or `_linux.py` launcher, which defaults to `--headerless-policy allow`. The compatibility gate validates format, required UIDs, and the exact Study/Series/SOP path; output gets standard File Meta and is reopened for UID, PatientID, date, and PixelData checks. An arbitrary unreadable file is **not** eligible. This mode requires strict UID policy; it cannot be combined with `--uid-policy preserve`.
- An independently and explicitly human-confirmed multi-PatientID Study: on a package that supports it, `--confirmed-multi-patient-study STUDY_UID` scopes the exception to that exact Study. Verify the deployed `--help`. It does not merge people, alter PatientID, or disable the gate for other Studies. Preserve its audit and checkpoint context. A stale argument that no longer matches an approved multi-PatientID Study stops before processing; inspect the data rather than broadening the exception.

## Prospective path

`src/build_enrolled_study_selection_prospective_linux.py` and `src/anonymize_enrolled_studies_fast_prospective_linux.py` are dedicated to `NN-QNNN` subjects. Use one shared **prospective** ledger across its centers, distinct from the retrospective ledger. The list builder requires `--prospective-control-root`; the anonymizer also requires `--prospective-root` and `--anonymous-map`. Relevant input/root paths must explicitly contain `前瞻性` or `prospective`; the ledger must be named `患者匿名编号映射.xlsx` within the prospective control root, and every approved assignment must agree with it. Output and audit roots must be `01.交付影像` and `02.内部控制文件` under the prospective anonymization root. Read `src/prospective_enrolled_anonymization.py` for the full path checks. The builder writes control workbooks; the anonymizer previews before `--execute`. Do not mix these with retrospective `NN-HNNN` identifiers or delivery/checkpoint roots. Strict UID is the default; preserve mode requires the same explicit decision and auditing as retrospective.

For any variant, report local tests/preflight separately from a real Linux run. Verify deployed package hashes and `--help`, then inspect server exit status, per-file audit, verification and UID/format issue reports, checkpoint, and output counts. Do not turn historical case totals into general hard-coded expectations. Relevant regressions are `tests/test_enrolled_anonymization_workflow.py`, `tests/test_enrolled_anonymization_fast.py`, and `tests/test_prospective_enrolled_anonymization.py`.
