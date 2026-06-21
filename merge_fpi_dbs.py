import sqlite3
import sys
import os
import glob

def merge_databases(start_year, end_year):
    """Merge all chunk databases into one final database."""
    final_db = f"fpi_data_{start_year}_{end_year}_FINAL.db"
    
    db_files = glob.glob("dbs/**/*.db", recursive=True)
    
    if not db_files:
        print("No database chunks found!")
        return
    
    print(f"Found {len(db_files)} database chunks to merge")
    
    with sqlite3.connect(final_db) as final_con:
        first_db = db_files[0]
        with sqlite3.connect(first_db) as first_con:
            schema = first_con.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='fpi_daily_data'"
            ).fetchone()[0]
            final_con.execute(schema)
        
        total_rows = 0
        for db_file in db_files:
            print(f"  Merging: {db_file}")
            with sqlite3.connect(db_file) as chunk_con:
                rows = chunk_con.execute("SELECT * FROM fpi_daily_data").fetchall()
                columns = [desc[0] for desc in chunk_con.execute("PRAGMA table_info(fpi_daily_data)").fetchall()]
                
                existing_cols = [desc[0] for desc in final_con.execute("PRAGMA table_info(fpi_daily_data)").fetchall()]
                for col in columns:
                    if col not in existing_cols:
                        final_con.execute(f'ALTER TABLE fpi_daily_data ADD COLUMN "{col}" TEXT DEFAULT ""')
                
                placeholders = ", ".join("?" for _ in columns)
                cols_sql = ", ".join(f'"{c}"' for c in columns)
                
                for row in rows:
                    try:
                        final_con.execute(
                            f'INSERT OR REPLACE INTO fpi_daily_data ({cols_sql}) VALUES ({placeholders})',
                            row
                        )
                        total_rows += 1
                    except Exception as e:
                        print(f"    Error inserting row: {e}")
        
        final_con.commit()
    
    print(f"\nMerge complete!")
    print(f"Final database: {final_db}")
    print(f"Total rows: {total_rows}")
    
    with sqlite3.connect(final_db) as con:
        columns = [desc[0] for desc in con.execute("PRAGMA table_info(fpi_daily_data)").fetchall()]
        dates = con.execute("SELECT COUNT(DISTINCT reporting_date) FROM fpi_daily_data").fetchone()[0]
        print(f"Total columns: {len(columns)}")
        print(f"Unique dates: {dates}")

if __name__ == "__main__":
    start_year = sys.argv[1] if len(sys.argv) > 1 else "2012"
    end_year = sys.argv[2] if len(sys.argv) > 2 else "2025"
    merge_databases(start_year, end_year)