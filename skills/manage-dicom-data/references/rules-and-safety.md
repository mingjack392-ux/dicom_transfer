# DICOM rules and safety invariants

Use this reference when reviewing or changing classification, destination paths, duplicate handling, patient identity, or anonymization.

## Transfer paths

V2 normal path:

```text
PatientName_PatientID/
└─ StudyInstanceUID/
   └─ SeriesInstanceUID/
      └─ SOPInstanceUID.dcm
```

Unreadable DICOM files go under `_quarantine/unreadable`. Files missing Study, Series, or SOP UID go under `_quarantine/missing_required_uids`. They are retained for review rather than discarded.

V3 uses the same Study/Series/SOP layout and adds identity routing:

- A single-PatientID person normally uses `PatientName_PatientID/...`.
- A confirmed or grouped multi-PatientID person uses an outer natural-person directory with one `PatientName_PatientID` child per ID.
- Separate same-name identity groups receive stable group suffixes when needed to avoid directory collisions.
- Missing PatientID or insufficient identity evidence is isolated or reported for review instead of being silently merged.

## Duplicate and UID-conflict handling

For V2/V3 and screening-number transfer, the destination key is the patient route plus StudyInstanceUID, SeriesInstanceUID, and SOPInstanceUID.

- If the target does not exist, copy through a temporary `.part` file and atomically replace the final target.
- If target size and SHA-256 match the source, record `duplicate_same` and keep the existing target.
- If the same target UID path contains different bytes, preserve the incoming file with a name containing the SHA-256 prefix and `.conflict.dcm`; never overwrite the existing file.
- Record conflicts, quarantined files, and errors in the exception output. A file name, timestamp, size alone, or PatientID alone is not a sufficiently rigorous duplicate test.

Other workflows have different conflict artifacts. The enrolled-Study anonymizer saves different-content output under internal-control `_conflicts`; the already-anonymized UID filter only reports `conflict` and writes **no** conflict copy. Do not generalize the transfer `.conflict.dcm` rule to either workflow.

## Patient identity

Do not merge natural persons from name alone. V3 compares normalized name plus demographic evidence such as birth date, sex, normalized institution, and a birth-year estimate derived from PatientAge and StudyDate. At least two comparable fields must support an automatic match, and a conflicting comparable field blocks automatic confirmation.

The project has a specific `grouped_needs_review` case for same normalized name, same sex, and same institution with conflicting birth dates. It may share an outer folder for operational grouping but remains explicitly unconfirmed. Do not describe this as a medically verified identity match.

When evidence is insufficient or conflicting, keep PatientIDs separate and surface the relationship in `患者身份待确认`.

## Study aggregation and dates

Classification is performed per Series and aggregated by StudyInstanceUID. One workbook row represents one Study, and multiple modalities are deduplicated and ordered. Use StudyDate and then AcquisitionDate according to the maintained implementation. If a Study contains conflicting dates, the report uses the earliest available date and creates a warning.

DICOM alone does not provide the external surgical timeline needed for `术中`, `术后6个月`, or `术后12个月`. Join Study dates to a separately supplied patient surgery-time table; do not guess those columns.

## Modality and 3D_DSA断层

Base types are derived from DICOM `Modality`. The maintained display order starts with XA, CT, MR, followed by other values such as OT.

`3D_DSA` and `3D_DSA断层` are different concepts. A Series is classified as `3D_DSA断层` only when all three conditions hold:

1. `Modality = XA`;
2. `SliceThickness` has a value;
3. `SeriesDescription` matches a 3D or reconstruction keyword such as `3D`, `MIP`, `MPR`, `VR`, `VRT`, `RECON`, `VOLUME`, `DYNACT`, `CBCT`, `XPERCT`, `断层`, or `重建`.

`PositionerMotion=DYNAMIC`, `NumberOfFrames>1`, and X-Ray 3D SOP Class do not independently trigger `3D_DSA断层`.

The 3D annotation belongs to XA, not to the entire Study string:

```text
XA（含3D_DSA断层）、OT
XA（含3D_DSA断层）、CT、OT
```

Do not output `XA、OT（含3D_DSA断层）`.

When a classification rule changes, bump the V2 classification rule version and the V3 rule version, update both workbook builders and both user documents, add regression examples, and re-scan affected original DICOM data. Historical Studies that are not rescanned retain their earlier stored result.

## Anonymization boundary

Organization and anonymization are separate workflows. V3 does not modify DICOM headers. Anonymization must write to a different destination, maintain an auditable mapping when reversibility is required, and default to preview where supported.

The current raw anonymization workflow does not prove that pixel data is free of burned-in names, identifiers, or recognizable faces. Treat such pixel review as a separate requirement.

## Preserving nonstandard source UIDs

Use this section for the maintained enrolled-Study high-speed anonymizer when a readable legacy DICOM batch contains nonstandard UID values. This is an explicit compatibility policy, not the global default.

- Keep strict UID validation as the default. Enable `--uid-policy preserve` only after confirming that the source files are readable and that the user wants source UID values retained instead of corrected.
- Warn and continue for path-safe UID defects such as values longer than the DICOM 64-character limit, internal consecutive dots, and file-meta Media Storage SOP Class/Instance values that differ from the corresponding dataset values. Record each affected file in the processing audit and the separate source-UID issue report; these warnings alone must not make the verification report fail or the command return an error.
- Still block the complete Study when a required Study/Series/SOP UID is missing, the source Study UID differs from the reviewed plan, a routing UID contains anything other than ASCII digits and dots, begins or ends with a dot, exceeds the maintained safe path-component limit, the DICOM is unreadable, or the Study contains multiple PatientIDs. The only multi-PatientID exception is an exact, human-confirmed Study UID on a supporting versioned entrypoint; see [enrollment-variants.md](enrollment-variants.md). Never truncate, clean, invent, or substitute a routing UID to make a path.
- “Preserve” means equality of parsed DICOM UI element values, not byte-for-byte equality of the whole file. Snapshot all UI elements before editing, including nested datasets and file-meta, then compare the complete snapshot after writing and reopening. Continue to verify PatientID, non-birth date/time values, and PixelData bytes, while confirming the required privacy tags and center text were removed.
- Do not bypass a post-write `UID标签发生变化` result. `pydicom` with `enforce_file_format=True` can synchronize `MediaStorageSOPInstanceUID`/`MediaStorageSOPClassUID` to dataset values or add required file-meta UIDs. In the maintained compatibility implementation, keep the established enforced writer only when it is UID-neutral; otherwise use `enforce_file_format=False` (`write_like_original=True` for pydicom 2.x), reopen the temporary file, and require the full snapshot comparison to pass.
- Keep normal files on the established serialization path so already validated output remains SHA-256 identical. An existing same target with the same SHA-256 is `重复相同`; different bytes remain a UID conflict in the internal control area and never overwrite formal delivery data.
- Include the UID policy and engine version in checkpoint context. A checkpoint row may be reused only when source and target still match its recorded state and the row contains the audit information required by the current policy. Recheck older successful rows once when their checkpoint schema lacks source-UID issue details; do not require the user to delete output or control state.

When changing this mode, test strict default behavior, path-safe nonstandard UID preservation, file-meta/dataset mismatch preservation, path-unsafe blocking, thread and process backends, same-target conflict handling, interruption/resume, prior-checkpoint handling, and package entrypoints. Validate a small read-only real-data sample through temporary output before recommending a full run; do not start the full anonymization merely to prove the code change.

## Sensitive-data handling

PatientName, PatientID, dates, identity mappings, source paths, and workbooks may contain protected health information. Keep them on approved storage, minimize copied logs and samples, and redact examples before sharing. Do not upload original DICOM or re-identification mappings to external services without explicit authorization and an approved data-handling arrangement.
