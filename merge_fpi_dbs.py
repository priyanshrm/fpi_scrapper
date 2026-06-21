import sqlite3
import sys
import glob

def merge_databases(start_year, end_year):
    final_db = f"fpi_data_{start_year}_{end_year}_FINAL.db"
    db_files = glob.glob("dbs/**/*.db", recursive=True)
    
    if not db_files:
        print("No database chunks found!")
        # Check alternate paths
        db_files = glob.glob("**/*.db", recursive=True)
        db_files = [f for f in db_files if "fpi_data_" in f and "FINAL" not in f]
    
    if not db_files:
        print("Still no database chunks found!")
        return
    
    print(f"Found {len(db_files)} chunks:")
    for f in db_files:
        print(f"  - {f}")
    
    with sqlite3.connect(final_db) as final_con:
        # Copy schema from first DB
        with sqlite3.connect(db_files[0]) as first_con:
            schema = first_con.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='fpi_daily_wide'"
            ).fetchone()
            
            if not schema:
                print("ERROR: Could not find fpi_daily_wide table!")
                return
            
            print(f"Schema: {schema[0]}")
            final_con.execute(schema[0])
        
        total = 0
        for db_file in db_files:
            print(f"\n  Merging: {db_file}")
            with sqlite3.connect(db_file) as chunk_con:
                # Check if table exists
                table_check = chunk_con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='fpi_daily_wide'"
                ).fetchone()
                
                if not table_check:
                    print(f"    Table fpi_daily_wide not found in {db_file}, skipping")
                    continue
                
                # Get columns (name is at index 1)
                cols = [r[1] for r in chunk_con.execute("PRAGMA table_info(fpi_daily_wide)").fetchall()]
                print(f"    Columns: {len(cols)}")
                
                # Get rows
                rows = chunk_con.execute("SELECT * FROM fpi_daily_wide").fetchall()
                print(f"    Rows: {len(rows)}")
                
                if not rows:
                    print(f"    No rows, skipping")
                    continue
                
                # Ensure all columns exist in final DB
                existing = [r[1] for r in final_con.execute("PRAGMA table_info(fpi_daily_wide)").fetchall()]
                for c in cols:
                    if c not in existing:
                        try:
                            final_con.execute(f'ALTER TABLE fpi_daily_wide ADD COLUMN "{c}" TEXT DEFAULT ""')
                        except Exception as e:
                            print(f"    Warning: Could not add column {c}: {e}")
                
                # Insert rows
                placeholders = ", ".join("?" for _ in cols)
                cols_sql = ", ".join(f'"{c}"' for c in cols)
                
                for row in rows:
                    try:
                        final_con.execute(
                            f'INSERT OR REPLACE INTO fpi_daily_wide ({cols_sql}) VALUES ({placeholders})',
                            row
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
    
    print(f"\n{'='*60}")
    print(f"MERGE COMPLETE")
    print(f"{'='*60}")
    print(f"Final database: {final_db}")
    print(f"Total rows (trading days): {row_count}")
    print(f"Unique dates: {dates}")
    print(f"Total columns: {len(cols)}")
    
    # Show sample
    print(f"\nSample rows:")
    with sqlite3.connect(final_db) as con:
        for row in con.execute("SELECT reporting_date FROM fpi_daily_wide LIMIT 5").fetchall():
            print(f"  {row[0]}")
        print(f"  ...")
        for row in con.execute("SELECT reporting_date FROM fpi_daily_wide ORDER BY reporting_date DESC LIMIT 3").fetchall():
            print(f"  {row[0]}")

if __name__ == "__main__":
    merge_databases(
        sys.argv[1] if len(sys.argv) > 1 else "2012",
        sys.argv[2] if len(sys.argv) > 2 else "2025"
    )