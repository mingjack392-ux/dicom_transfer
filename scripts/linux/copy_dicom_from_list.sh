#!/usr/bin/env bash

# Usage:
#   bash copy_dicom_from_list.sh "path_list.txt" "destination_directory"
# If the destination requires elevated permissions:
#   sudo bash copy_dicom_from_list.sh "path_list.txt" "destination_directory"

set -u

if [[ $# -ne 2 ]]; then
    echo "Usage: $0 path_list.txt destination_directory"
    exit 1
fi

LIST_FILE=$1
DEST_DIR=$2

if [[ ! -f "$LIST_FILE" ]]; then
    echo "ERROR: list file does not exist: $LIST_FILE"
    exit 1
fi

if ! mkdir -p -- "$DEST_DIR"; then
    echo "ERROR: cannot create destination: $DEST_DIR"
    exit 1
fi

total=0
copied=0
existing=0
missing=0
failed=0

while IFS= read -r file || [[ -n "$file" ]]; do
    # Remove a UTF-8 BOM, Windows CR, and trailing spaces/tabs.
    file=${file#$'\xEF\xBB\xBF'}
    file=${file%$'\r'}
    while [[ "$file" == *[$' \t'] ]]; do
        file=${file%?}
    done
    [[ -z "$file" ]] && continue

    total=$((total + 1))

    if [[ ! -f "$file" ]]; then
        echo "MISSING: $file"
        missing=$((missing + 1))
        continue
    fi

    target="$DEST_DIR/${file##*/}"
    if [[ -e "$target" ]]; then
        echo "SKIP existing name: $target"
        existing=$((existing + 1))
        continue
    fi

    if cp -n -- "$file" "$target"; then
        echo "COPIED: $file"
        copied=$((copied + 1))
    else
        echo "FAILED: $file"
        failed=$((failed + 1))
    fi
done < "$LIST_FILE"

echo
echo "Copy finished"
echo "List entries: $total"
echo "Copied: $copied"
echo "Existing names skipped: $existing"
echo "Missing files: $missing"
echo "Copy failures: $failed"
echo "Destination: $DEST_DIR"

if (( failed > 0 )); then
    exit 1
fi
