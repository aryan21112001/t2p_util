#!/usr/bin/env python3
"""
Extract raw contents from Tableau .twbx files for inspection.

Usage:
    python extract_twbx_contents.py
    
    The script will prompt for:
    - Input folder (contains .twbx files)
    - Output folder (where to save extracted contents)
"""

import os
import sys
import zipfile
from pathlib import Path


def get_user_input(prompt: str, must_exist: bool = False) -> str:
    """Get and validate user input for a folder path."""
    while True:
        path = input(prompt).strip()
        if not path:
            print("  Error: Path cannot be empty.")
            continue
        
        path = os.path.expanduser(path)
        
        if must_exist and not os.path.exists(path):
            print(f"  Error: Path does not exist: {path}")
            continue
        
        if must_exist and not os.path.isdir(path):
            print(f"  Error: Path is not a directory: {path}")
            continue
        
        return path


def discover_twbx_files(folder: str) -> list:
    """Discover all .twbx files in a folder (recursive)."""
    twbx_files = []
    folder_path = Path(folder)
    
    if not folder_path.exists():
        print(f"Error: Input folder does not exist: {folder}")
        return []
    
    # Handle .twbx.twbx duplicates and normal .twbx files
    for pattern in ["**/*.twbx", "**/*.twbx.twbx"]:
        twbx_files.extend(folder_path.glob(pattern))
    
    # Remove duplicates and convert to sorted list
    twbx_files = sorted(set(twbx_files))
    return twbx_files


def extract_twbx(twbx_path: str, output_folder: str) -> bool:
    """Extract a single .twbx file to output folder."""
    try:
        twbx_path_obj = Path(twbx_path)
        
        # Create workbook subfolder using filename (without .twbx)
        workbook_name = twbx_path_obj.stem
        # Handle doubled suffix (.twbx.twbx)
        if workbook_name.endswith('.twbx'):
            workbook_name = workbook_name[:-5]
        
        extract_dir = Path(output_folder) / workbook_name
        extract_dir.mkdir(parents=True, exist_ok=True)
        
        # Extract all files
        with zipfile.ZipFile(twbx_path, 'r') as zip_ref:
            zip_ref.extractall(extract_dir)
        
        print(f"  ✓ Extracted to: {extract_dir}")
        return True
    
    except zipfile.BadZipFile:
        print(f"  ✗ Bad ZIP file (may be corrupted): {twbx_path}")
        return False
    except Exception as e:
        print(f"  ✗ Error extracting {twbx_path}: {e}")
        return False


def main():
    """Main extraction workflow."""
    print("\n" + "=" * 70)
    print("EXTRACT TWBX CONTENTS FOR INSPECTION")
    print("=" * 70)
    
    # Get input folder
    print("\n[1] Input Folder")
    print("    Enter the path to a folder containing .twbx files.")
    input_folder = get_user_input("    Path: ", must_exist=True)
    
    # Get output folder
    print("\n[2] Output Folder")
    print("    Enter the path where extracted contents will be saved.")
    print("    (Will be created if it doesn't exist)")
    output_folder = get_user_input("    Path: ", must_exist=False)
    
    # Create output folder
    try:
        Path(output_folder).mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print(f"Error: Cannot create output folder: {e}")
        sys.exit(1)
    
    # Discover .twbx files
    print("\n[3] Discovering .twbx files...")
    twbx_files = discover_twbx_files(input_folder)
    
    if not twbx_files:
        print("  No .twbx files found in the input folder.")
        sys.exit(1)
    
    print(f"  Found {len(twbx_files)} workbook(s)")
    
    # Extract each file
    print("\n[4] Extracting workbooks...")
    success_count = 0
    fail_count = 0
    
    for i, twbx_file in enumerate(twbx_files, 1):
        print(f"  [{i}/{len(twbx_files)}] {twbx_file.name}")
        if extract_twbx(str(twbx_file), output_folder):
            success_count += 1
        else:
            fail_count += 1
    
    # Summary
    print("\n" + "=" * 70)
    print(f"EXTRACTION COMPLETE")
    print(f"  Successful: {success_count}")
    if fail_count > 0:
        print(f"  Failed: {fail_count}")
    print(f"  Output folder: {output_folder}")
    print("=" * 70)
    print("\nNext steps:")
    print("  1. Browse the extracted contents in:", output_folder)
    print("  2. Inspect workbook.xml to see dashboard structure")
    print("  3. Check Datasources/ folder for data source definitions")
    print("  4. Review Worksheets/ and Dashboards/ for field definitions")
    print("\n")


if __name__ == "__main__":
    main()
