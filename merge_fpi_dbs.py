import sqlite3
import sys
import glob

def merge_databases(start_year, end_year):
    final_db = f"fpi_data_{start_year}_{end_year}_FINAL.db"
    db_files = glob.glob("dbs/**/*.db", recursive=True)
    
    if not db_files:
        print("No database chunks found!")
        return
    
    print(f"Found {len(db_files)} chunks")
    
    with sqlite3.connect(final_db) as final_con:
        # Copy schema from first DB
        with sqlite3.connect(db_files[0]) as first_con:
            schema = first_con.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='fpi_wide_data'"
            ).fetchone()[0]
            final_con.execute(schema)
        
        total = 0
        for db_file in db_files:
            print(f"  Merging: {db_file}")
            with sqlite3.connect(db_file) as chunk_con:
                # Get columns (name is at index 1)
                cols = [r[1] for r in chunk_con.execute("PRAGMA table_info(fpi_wide_data)").fetchall()]
                
                # Ensure all columns exist in final DB
                existing = [r[1] for r in final_con.execute("PRAGMA table_info(fpi_wide_data)").fetchall()]
                for c in cols:
                    if c not in existing:
                        final_con.execute(f'ALTER TABLE fpi_wide_data ADD COLUMN "{c}" TEXT DEFAULT ""')
                
                # Insert rows
                for row in chunk_con.execute("SELECT * FROM fpi_wide_data").fetchall():
                    try:
                        placeholders = ", ".join("?" for _ in cols)
                        cols_sql = ", ".join(f'"{c}"' for c in cols)
                        final_con.execute(
                            f'INSERT OR REPLACE INTO fpi_wide_data ({cols_sql}) VALUES ({placeholders})',
                            row
                        )
                        total += 1
                    except Exception as e:
                        print(f"    Error: {e}")
        
        final_con.commit()
    
    print(f"\nDone! {total} rows in {final_db}")

if __name__ == "__main__":
    merge_databases(
        sys.argv[1] if len(sys.argv) > 1 else "2012",
        sys.argv[2] if len(sys.argv) > 2 else "2025"
    )