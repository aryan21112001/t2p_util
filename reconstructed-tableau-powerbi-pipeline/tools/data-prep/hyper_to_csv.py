#!/usr/bin/env python3
"""
Convert Tableau Hyper files to CSV format.

Writes the first table found in the extract's `Extract` schema. Values are written with the csv
module, so commas, quotes and line breaks inside values are quoted correctly, and NULL becomes an
empty field.
"""

import argparse
import csv
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
        from tableauhyperapi import Connection, HyperProcess, Telemetry
    except ImportError:
        print("Error: tableauhyperapi not installed. Install with: pip install tableauhyperapi",
              file=sys.stderr)
        sys.exit(1)

    try:
        with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hyper:
            with Connection(endpoint=hyper.endpoint, database=hyper_file) as connection:
                # Get table names
                tables = connection.catalog.get_table_names(schema='Extract')

                if not tables:
                    print("Error: No tables found in Hyper file", file=sys.stderr)
                    sys.exit(1)

                # Extract the first table
                table_name = tables[0]
                table_def = connection.catalog.get_table_definition(table_name)

                # Query the table and write to CSV
                with open(output_file, 'w', newline='', encoding='utf-8') as csv_file:
                    writer = csv.writer(csv_file)

                    # Write header
                    writer.writerow([str(col.name.unescaped) for col in table_def.columns])

                    # Write rows (NULL -> empty field)
                    query = f"SELECT * FROM {table_name}"
                    with connection.execute_query(query) as result:
                        for row in result:
                            writer.writerow(['' if val is None else str(val) for val in row])

                print(f"Successfully converted {hyper_file} to {output_file}")

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
        print(f"Error: File not found: {args.hyper_file}", file=sys.stderr)
        sys.exit(1)

    output_file = args.output or str(hyper_path.with_suffix('.csv'))
    convert_hyper_to_csv(str(hyper_path), output_file)


if __name__ == '__main__':
    main()
