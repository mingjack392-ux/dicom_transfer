# Linux post-transfer selection by Excel UID list

Use this reference only when an external workbook selects DICOM from folders that have already been transferred. Do not alter V2/V3 transfer or infer the requested clinical period from DICOM metadata: the supplied workbook is the authority for the period-to-UID mapping.

Maintained entrypoint:

```text
src/filter_transferred_dicom_by_selection.py
```

It imports `src/dicom_transfer_by_screening.py` for header-only scanning, path validation, atomic copying, and SHA-256 duplicate/conflict handling. Linux needs `pydicom` and `openpyxl` from `requirements-linux.txt`.

## Selection contract

For the maintained workbook contract:

- `时期=术前`: select by `SeriesUID` and copy every DICOM instance in that Series to `术前3D影像`.
- `时期=术中`: select only the exact `SOPUID` and place it under `术后2DDSA`.
- Ignore other period values. Process every qualifying workbook row; do not assume a fixed number of preoperative or intraoperative rows per patient.

Match each immediate source patient folder using both workbook fields `住院号` and `患者`; never match or merge by name alone. Treat these two asymmetric cases differently:

- Workbook patient absent from the source: when the user has confirmed those patients are out of scope, ignore them without an error or empty output directory and retain only an audit status.
- Source patient folder absent from the qualifying workbook rows: do not copy it; require review because this may be a folder-name mismatch or a missing selection rule.

Preserve this destination layout unless the user explicitly requests another safe layout:

```text
destination_root/
└─ original_patient_folder/
   ├─ 术前3D影像/StudyInstanceUID/SeriesInstanceUID/SOPInstanceUID.dcm
   └─ 术后2DDSA/StudyInstanceUID/SeriesInstanceUID/SOPInstanceUID.dcm
```

The source and destination must be independent. Do not put the destination inside, equal to, or above the source. Do not modify DICOM headers or read Pixel Data for indexing.

## Preview, execution, and audit

Preview is the default. It reads DICOM headers and writes UTF-8 BOM audit CSVs but does not copy DICOM. Add `--execute` only after the preview and its audit have been reviewed.

```bash
python3 ./src/filter_transferred_dicom_by_selection.py \
  "<source_root>" "<selection.xlsx>" "<destination_root>" \
  --sheet "<sheet>" --workers 4 --copy-workers 2

python3 ./src/filter_transferred_dicom_by_selection.py \
  "<source_root>" "<selection.xlsx>" "<destination_root>" \
  --sheet "<sheet>" --workers 4 --copy-workers 2 --execute
```

Review both `筛选明细_*.csv` and `患者筛选汇总_*.csv`. Confirm patient-folder matches, UID hits, `ignored_not_in_selection`, unreadable DICOM, conflicts, and errors. Same target plus identical SHA-256 is `duplicate_same`; same UID plus different content is preserved as `.conflict.dcm`, never overwritten.

Synthetic tests and successful workbook parsing do not prove a real Linux dataset completed. Report real source counts and copied/duplicate/conflict/error counts only from the server audit and logs.

## Linux operational safeguards

For permission problems, first verify the literal path spelling, ownership, group membership, ACL, and effective Samba identity when access is through a Windows share. If the user authorizes a permission change, prefer a user-specific ACL such as `setfacl` over `chmod 777`; verify with a reversible create/delete test under the intended account. Do not change ownership or recursive mode broadly when a narrow ACL is sufficient.

For long runs through a jump host, prefer `tmux` when available. A `nohup bash -lc` wrapper is also suitable: run preview followed by execution with `&&`, redirect output to a log, save `$!` to a PID file, and write the final exit status to a status file. After reconnecting, check all three:

```bash
cat <status-file>
tail -n 100 <log-file>
ps -p "$(cat <pid-file>)" -o pid,etime,stat,cmd
```

`RUNNING` alone does not prove the process is still active; it can also mean the process stopped before writing its final status. `nohup` survives an SSH or jump-host disconnect, not a server reboot. Never claim the real selection completed until the process state, exit status, audit CSVs, and output counts agree.
