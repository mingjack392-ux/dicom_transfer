# Screening transfer and center image-web reports

Paths here are relative to the project checkout. Data paths in commands are placeholders; use the user's actual source and independent target roots.

## Screening-number folder transfer

`src/dicom_transfer_by_screening.py` accepts a single subject directory, or a center root with `--batch` whose immediate children are subjects. It scans all DICOM headers in each subject independently. Name plus additional demographic evidence is required; conflicting or insufficient identity evidence blocks that subject even with `--execute`. PatientIDs may differ only when the identity gate passes. Output is `screening_id/StudyUID/SeriesUID/SOPUID.dcm`; source visit-folder names are retained in the audit rather than destination routing.

```bash
python3 src/dicom_transfer_by_screening.py "/data/source-center" "/data/organized-center" --batch
```

Default mode previews. After review and authorization, add `--execute`. Inspect subject outcomes in the transfer audit and batch summary; one blocked subject does not prevent other subjects from being assessed. Missing/path-unsafe UIDs are quarantined; path-safe nonstandard UIDs are retained with warnings. Same-target SHA-256 matches are duplicates; differing bytes produce `.conflict.dcm` files without overwriting the established target.

## Mixed folder/ZIP/RAR packages

Use `web_registry/dicom_transfer_screening_packages_windows.py` or `_linux.py` for packages and supplemental inputs. They share `web_registry/dicom_transfer_screening_packages_core.py`; inspect its current parser and identity decisions. RAR requires 7z/7zz. Do not treat a package filename as conclusive patient identity evidence.

```bash
python3 web_registry/dicom_transfer_screening_packages_linux.py batch "/data/packages" "/data/organized-center"
```

`single` mode additionally requires `--subject-id`. Preview is default; review identity/routing audits before `--execute`. Preserve scan checkpoints and use current CLI help for resume options. `--confirm-same-subject` records a human identity confirmation for an exact screening number and must be grounded in that confirmation, not automatically added to clear a failed gate.

## Center image-web report

Use `src/analyze_transferred_dicom_by_center.py` on Windows or `_linux.py` on Linux. The latter uses openpyxl; the original Windows reporting path uses Node/MJS helpers. The DICOM source and external center registry workbook are read-only inputs. A single center requires `--single-center`; with multiple immediate center children, omit it. Unknown aliases can be supplied through `--center-map` JSON after inspecting the registry.

```bash
python3 src/analyze_transferred_dicom_by_center_linux.py "/data/organized-center" "/data/control/registry.xlsx" --single-center "示例中心" --workers 4
```

The default scans and prints statistics. `--write --output "/data/reports/image-web.xlsx"` creates a workbook; replacing an existing workbook requires explicit `--overwrite`. Check matching and exception counts, then the center sheets, `处理异常`, `运行摘要`, and `字段说明`.

Important matching/reporting semantics:

- Image dates fall back from AcquisitionDate to StudyDate to SeriesDate. Period uses image date minus the externally supplied surgery date; positive differences are divided by 30. DICOM alone supplies no surgery timeline.
- The maintained screening fallback recognizes retrospective H and prospective Q folder identifiers and normalizes underscores to hyphens for a unique match within the current center. Missing hospital numbers remain blank; the registry can supply the display name or initials.
- Single-frame data is aggregated by Series; multi-frame data is reported by SOP. These are report granularities, not clinical modality decisions.
- `SOPUID=NA` indicates whole-Series granularity. Preserve the separate DICOM classification rule in [rules-and-safety.md](rules-and-safety.md); it does not establish 3D-DSA by itself.

Relevant regressions: `tests/test_dicom_transfer_by_screening.py`, `tests/test_dicom_transfer_screening_packages.py`, and `tests/test_analyze_transferred_dicom_by_center_linux.py`.
