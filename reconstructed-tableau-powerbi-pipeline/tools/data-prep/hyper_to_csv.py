#!/usr/bin/env python3
"""
Convert Tableau Hyper files to CSV format.
"""

import argparse
import sys
from pathlib import Path


def convert_hyper_to_csv(hyper_file: str, output_file: str) -> None:
    """
    Convert a Hyper file to CSV format.
    
    Args:
        hyper_file: Path to the .hyper file
        output_file: Path to the output CSV file
    """
    try:
        from tableauhyperapi import HyperProcess, SqlQueryContext, escape_name
        
        with HyperProcess() as hyper:
            with hyper.connect() as connection:
                # Get table names
                tables = connection.catalog.get_table_names(schema='Extract')
                
                if not tables:
                    print("Error: No tables found in Hyper file", file=sys.stderr)
                    sys.exit(1)
                
                # Extract the first table
                table_name = tables[0]
                table_def = connection.catalog.get_table(table_name)
                
                # Query the table and write to CSV
                with open(output_file, 'w', newline='', encoding='utf-8') as csv_file:
                    # Write header
                    columns = [col.name for col in table_def.columns]
                    csv_file.write(','.join(columns) + '\n')
                    
                    # Write rows
                    query = f"SELECT * FROM {escape_name('Extract', table_name)}"
                    for row in connection.execute_list_query(query):
                        csv_file.write(','.join(str(val) for val in row) + '\n')
                
                print(f"Successfully converted {hyper_file} to {output_file}")
    
    except ImportError:
        print("Error: tableauhyperapi not installed. Install with: pip install tableauhyperapi", 
              file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error converting file: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Convert Tableau Hyper files to CSV")
    parser.add_argument('hyper_file', help='Path to the .hyper file')
    parser.add_argument('-o', '--output', help='Output CSV file path (default: <input>.csv)')
    
    args = parser.parse_args()
    
    hyper_path = Path(args.hyper_file)
    if not hyper_path.exists():
        print(f"Error: File not found: {hyper_file}", file=sys.stderr)
        sys.exit(1)
    
    output_file = args.output or str(hyper_path.with_suffix('.csv'))
    convert_hyper_to_csv(str(hyper_path), output_file)


if __name__ == '__main__':
    main()
