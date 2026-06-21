import sqlite3
import sys
import glob
import os

def merge_databases(start_year, end_year):
    final_db = f"fpi_data_{start_year}_{end_year}_FINAL.db"
    
    # Search for chunk databases
    db_files = glob.glob("dbs/**/*.db", recursive=True)
    
    if not db_files:
        # Try current directory
        db_files = glob.glob("fpi_data_*.db")
        db_files = [f for f in db_files if "FINAL" not in f]
    
    if not db_files:
        print("No database chunks found!")
        return
    
    print(f"Found {len(db_files)} chunks:")
    for f in sorted(db_files):
        size_kb = os.path.getsize(f) / 1024
        print(f"  - {f} ({size_kb:.1f} KB)")
    
    with sqlite3.connect(final_db) as final_con:
        # Copy schema from first DB
        with sqlite3.connect(db_files[0]) as first_con:
            schema = first_con.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='fpi_daily_wide'"
            ).fetchone()
            
            if not schema:
                print("ERROR: Could not find fpi_daily_wide table!")
                return
            
            final_con.execute(schema[0])
            print(f"Created table from schema")
        
        total = 0
        
        for db_file in sorted(db_files):
            print(f"\n  Merging: {db_file}")
            
            with sqlite3.connect(db_file) as chunk_con:
                # Check if table exists
                table_check = chunk_con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='fpi_daily_wide'"
                ).fetchone()
                
                if not table_check:
                    print(f"    Table not found, skipping")
                    continue
                
                # Get columns from chunk
                chunk_cols = [r[1] for r in chunk_con.execute("PRAGMA table_info(fpi_daily_wide)").fetchall()]
                print(f"    Columns in chunk: {len(chunk_cols)}")
                
                # Get rows
                rows = chunk_con.execute("SELECT * FROM fpi_daily_wide").fetchall()
                print(f"    Rows in chunk: {len(rows)}")
                
                if not rows:
                    print(f"    No rows, skipping")
                    continue
                
                # Ensure all columns exist in final DB
                existing_cols = [r[1] for r in final_con.execute("PRAGMA table_info(fpi_daily_wide)").fetchall()]
                new_cols = [c for c in chunk_cols if c not in existing_cols]
                
                for c in new_cols:
                    try:
                        final_con.execute(f'ALTER TABLE fpi_daily_wide ADD COLUMN "{c}" TEXT DEFAULT ""')
                    except Exception as e:
                        print(f"    Warning: Could not add column {c}: {e}")
                
                if new_cols:
                    print(f"    Added {len(new_cols)} new columns")
                
                # Get updated final columns
                final_cols = [r[1] for r in final_con.execute("PRAGMA table_info(fpi_daily_wide)").fetchall()]
                
                # Build INSERT statement with final columns
                placeholders = ", ".join("?" for _ in final_cols)
                cols_sql = ", ".join(f'"{c}"' for c in final_cols)
                
                # For each row in chunk, map to final columns
                for row in rows:
                    # Create dict from chunk columns and row values
                    row_dict = dict(zip(chunk_cols, row))
                    
                    # Build values list matching final columns order
                    values = [row_dict.get(c, "") for c in final_cols]
                    
                    try:
                        final_con.execute(
                            f'INSERT OR REPLACE INTO fpi_daily_wide ({cols_sql}) VALUES ({placeholders})',
                            values
                        )
                        total += 1
                    except Exception as e:
                        print(f"    Error inserting row: {e}")
        
        final_con.commit()
    
    # Final stats
    with sqlite3.connect(final_db) as con:
        row_count = con.execute("SELECT COUNT(*) FROM fpi_daily_wide").fetchone()[0]
        dates = con.execute("SELECT COUNT(DISTINCT reporting_date) FROM fpi_daily_wide").fetchone()[0]
        cols = [r[1] for r in con.execute("PRAGMA table_info(fpi_daily_wide)").fetchall()]
        
        # Get date range
        first_date = con.execute("SELECT MIN(reporting_date) FROM fpi_daily_wide").fetchone()[0]
        last_date = con.execute("SELECT MAX(reporting_date) FROM fpi_daily_wide").fetchone()[0]
    
    file_size = os.path.getsize(final_db) / (1024 * 1024)
    
    print(f"\n{'='*60}")
    print(f"MERGE COMPLETE")
    print(f"{'='*60}")
    print(f"Final database: {final_db}")
    print(f"File size: {file_size:.1f} MB")
    print(f"Date range: {first_date} to {last_date}")
    print(f"Total rows (trading days): {row_count}")
    print(f"Unique dates: {dates}")
    print(f"Total columns: {len(cols)}")
    
    # Show sample dates
    print(f"\nFirst 5 dates:")
    with sqlite3.connect(final_db) as con:
        for row in con.execute("SELECT DISTINCT reporting_date FROM fpi_daily_wide ORDER BY reporting_date LIMIT 5").fetchall():
            print(f"  {row[0]}")
        print(f"  ...")
        for row in con.execute("SELECT DISTINCT reporting_date FROM fpi_daily_wide ORDER BY reporting_date DESC LIMIT 3").fetchall():
            print(f"  {row[0]}")

if __name__ == "__main__":
    start_year = sys.argv[1] if len(sys.argv) > 1 else "2012"
    end_year = sys.argv[2] if len(sys.argv) > 2 else "2025"
    
    print(f"Merging FPI data for {start_year}-{end_year}")
    print()
    
    merge_databases(start_year, end_year)