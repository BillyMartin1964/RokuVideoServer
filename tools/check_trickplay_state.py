#!/usr/bin/env python3
"""
Simple diagnostic: compare indexed file IDs with trick-play cache directories.
Run from repo root with `python tools/check_trickplay_state.py`.
"""
import json
import os
import sys

# Import config to get canonical cache paths
try:
    import config
except Exception:
    print("Could not import config.py from workspace. Ensure script runs from repo root.")
    sys.exit(1)

FILE_CACHE = getattr(config, "FILE_CACHE_FILE", None)
TRICKPLAY_ROOT = getattr(config, "TRICKPLAY_CACHE_DIR", None)
THUMB_ROOT = getattr(config, "THUMB_CACHE_DIR", None)

indexed_ids = set()

# Try to read cached file list if present
if FILE_CACHE and os.path.isfile(FILE_CACHE):
    try:
        with open(FILE_CACHE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for item in data:
            if isinstance(item, dict):
                fid = item.get("id") or item.get("fileId")
                if fid:
                    indexed_ids.add(str(fid))
    except Exception as ex:
        print(f"Warning: could not read FILE_CACHE_FILE: {ex}")

# Fallback to config.FILES_LIST if available in memory
if not indexed_ids:
    try:
        for item in getattr(config, "FILES_LIST", []) or []:
            if isinstance(item, dict):
                fid = item.get("id") or item.get("fileId")
                if fid:
                    indexed_ids.add(str(fid))
    except Exception:
        pass

# Heuristic: also check for trickplay under THUMB cache (misplaced)
misplaced_root = None
if THUMB_ROOT:
    cand = os.path.join(THUMB_ROOT, "trickplay")
    if os.path.isdir(cand):
        misplaced_root = cand

# Collect actual directories
actual_dirs = set()
if TRICKPLAY_ROOT and os.path.isdir(TRICKPLAY_ROOT):
    try:
        actual_dirs.update([name for name in os.listdir(TRICKPLAY_ROOT) if os.path.isdir(os.path.join(TRICKPLAY_ROOT, name))])
    except Exception:
        pass

misplaced_dirs = set()
if misplaced_root:
    try:
        misplaced_dirs.update([name for name in os.listdir(misplaced_root) if os.path.isdir(os.path.join(misplaced_root, name))])
    except Exception:
        pass

# Summarize
print("Trickplay diagnostic")
print("-------------------")
print(f"TRICKPLAY_CACHE_DIR: {TRICKPLAY_ROOT}")
print(f"THUMB_CACHE_DIR: {THUMB_ROOT}")
print(f"Indexed IDs: {len(indexed_ids)}")
print(f"Trickplay directories in TRICKPLAY_CACHE_DIR: {len(actual_dirs)}")
if misplaced_root:
    print(f"Potential misplaced trickplay under: {misplaced_root} ({len(misplaced_dirs)} dirs)")

missing = sorted([fid for fid in indexed_ids if fid not in actual_dirs])
orphaned = sorted([d for d in actual_dirs if d not in indexed_ids])
misplaced_orphaned = sorted([d for d in misplaced_dirs if d not in indexed_ids])

print(f"Indexed without trickplay dir: {len(missing)}")
print(f"Trickplay dirs without indexed id (orphaned): {len(orphaned)}")
if misplaced_root:
    print(f"Dirs under misplaced trickplay that are not indexed: {len(misplaced_orphaned)}")

# Show small samples
if missing:
    print("\nSample missing (first 10):")
    for x in missing[:10]:
        print(" - ", x)

if orphaned:
    print("\nSample orphaned in TRICKPLAY_CACHE_DIR (first 10):")
    for x in orphaned[:10]:
        print(" - ", x)

if misplaced_root and misplaced_orphaned:
    print("\nSample orphaned in misplaced trickplay (first 10):")
    for x in misplaced_orphaned[:10]:
        print(" - ", x)

# Quick checks
print("\nQuick checks:")
if not TRICKPLAY_ROOT or not os.path.isdir(TRICKPLAY_ROOT):
    print(" - TRICKPLAY_CACHE_DIR does not exist or is not accessible.")
else:
    print(" - TRICKPLAY_CACHE_DIR exists and is accessible.")

if shutil_wrong := (not hasattr(sys.modules.get('shutil'), 'move') if sys.modules.get('shutil') else False):
    # unlikely, placeholder
    pass

print('\nDone.')
