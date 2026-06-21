import sqlite3
import sys
import os
import glob

def merge_databases(start_year, end_year):
    """Merge all chunk databases into one final database."""
    final_db = f"fpi_data_{start_year}_{end_year}_FINAL.db"
    
    # Find all chunk databases
    db_files = glob.glob("dbs/**/*.db", recursive=True)
    
    if not db_files:
        print("No database chunks found!")
        return
    
    print(f"Found {len(db_files)} database chunks to merge")
    for f in db_files:
        print(f"  - {f}")
    
    # Initialize final database with schema from first chunk
    with sqlite3.connect(final_db) as final_con:
        first_db = db_files[0]
        print(f"\nUsing schema from: {first_db}")
        
        with sqlite3.connect(first_db) as first_con:
            # Get CREATE TABLE statement
            schema = first_con.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='fpi_daily_data'"
            ).fetchone()
            
            if schema:
                print(f"Schema: {schema[0]}")
                final_con.execute(schema[0])
            else:
                print("ERROR: Could not find fpi_daily_data table schema!")
                return
        
        # Merge all chunks
        total_rows = 0
        for db_file in db_files:
            print(f"\n  Merging: {db_file}")
            
            with sqlite3.connect(db_file) as chunk_con:
                # Get column names (PRAGMA returns: cid, name, type, notnull, dflt_value, pk)
                pragma_result = chunk_con.execute("PRAGMA table_info(fpi_daily_data)").fetchall()
                # Column name is at index 1
                columns = [row[1] for row in pragma_result]
                print(f"    Columns: {columns}")
                
                # Get all rows
                rows = chunk_con.execute("SELECT * FROM fpi_daily_data").fetchall()
                print(f"    Rows in chunk: {len(rows)}")
                
                if not rows:
                    print(f"    No rows to merge, skipping")
                    continue
                
                # Build INSERT statement
                cols_sql = ", ".join(f'"{c}"' for c in columns)
                placeholders = ", ".join("?" for _ in columns)
                
                insert_sql = f'INSERT OR REPLACE INTO fpi_daily_data ({cols_sql}) VALUES ({placeholders})'
                
                # Insert rows one by one (with error handling)
                for row in rows:
                    try:
                        final_con.execute(insert_sql, row)
                        total_rows += 1
                    except Exception as e:
                        print(f"    Error inserting row: {e}")
                        print(f"    Row data: {row}")
        
        final_con.commit()
    
    # Final report
    print(f"\n{'='*60}")
    print(f"MERGE COMPLETE")
    print(f"{'='*60}")
    print(f"Final database: {final_db}")
    print(f"Total rows inserted: {total_rows}")
    
    with sqlite3.connect(final_db) as con:
        row_count = con.execute("SELECT COUNT(*) FROM fpi_daily_data").fetchone()[0]
        dates = con.execute("SELECT COUNT(DISTINCT reporting_date) FROM fpi_daily_data").fetchone()[0]
        columns = [row[1] for row in con.execute("PRAGMA table_info(fpi_daily_data)").fetchall()]
        
        print(f"Total rows in final DB: {row_count}")
        print(f"Unique dates: {dates}")
        print(f"Columns ({len(columns)}): {columns}")
        
        # Show sample data
        print(f"\nSample rows:")
        for row in con.execute("SELECT * FROM fpi_daily_data LIMIT 5").fetchall():
            print(f"  {row}")

if __name__ == "__main__":
    start_year = sys.argv[1] if len(sys.argv) > 1 else "2012"
    end_year = sys.argv[2] if len(sys.argv) > 2 else "2025"
    merge_databases(start_year, end_year)