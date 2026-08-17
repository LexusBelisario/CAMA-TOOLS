import psycopg2
from psycopg2 import sql
from datetime import datetime

# === DB Connection Settings
host = "aws-0-ap-southeast-1.pooler.supabase.com"
port = 6543
database = "postgres"
user = "postgres.dhnjamnukkaxqtlwutiv"
password = "MischiefManaged!"
schema = "CALAUAN_LAGUNA"

# === Output file path
output_file = "edit_history.txt"

# === Connect to PostgreSQL
conn = psycopg2.connect(
    host=host, port=port, dbname=database, user=user, password=password
)
cur = conn.cursor()

# === Step 1: Get all tables with 'updated_at' column in the schema
cur.execute("""
    SELECT table_name
    FROM information_schema.columns
    WHERE table_schema = %s
      AND column_name = 'updated_at'
""", (schema,))
tables = cur.fetchall()

# === Step 2: Query max(updated_at) for each table
edit_records = []

for (table_name,) in tables:
    try:
        cur.execute(
            sql.SQL("SELECT max(updated_at) FROM {}.{}").format(
                sql.Identifier(schema),
                sql.Identifier(table_name)
            )
        )
        result = cur.fetchone()[0]
        edit_time = result.strftime("%Y-%m-%d %H:%M:%S") if result else "No edits yet"
        edit_records.append((table_name, edit_time))
    except Exception as e:
        edit_records.append((table_name, f"Error: {e}"))

# === Step 3: Write results to file
with open(output_file, "w") as f:
    for table, edit in edit_records:
        f.write(f"Table: {table:<20} | Last Edit: {edit}\n")

# === Cleanup
cur.close()
conn.close()

print(f"Edit history written to {output_file}")
