import psycopg2
from psycopg2 import sql

# === Supabase/PostgreSQL connection
host = "aws-0-ap-southeast-1.pooler.supabase.com"
port = 6543
database = "postgres"
user = "postgres.dhnjamnukkaxqtlwutiv"
password = "MischiefManaged!"
schema = "CALAUAN_LAGUNA"  # Must stay UPPERCASE if created that way

# === Connect to DB
conn = psycopg2.connect(
    host=host, port=port, dbname=database, user=user, password=password
)
cur = conn.cursor()

# === Get all tables in the schema
cur.execute("""
    SELECT table_name
    FROM information_schema.tables
    WHERE table_schema = %s AND table_type = 'BASE TABLE'
""", (schema,))
tables = [row[0] for row in cur.fetchall()]

# === Create the trigger function (case-preserved)
cur.execute(sql.SQL("""
    CREATE OR REPLACE FUNCTION {}.update_timestamp()
    RETURNS TRIGGER AS $$
    BEGIN
        NEW.updated_at = now();
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
""").format(sql.Identifier(schema)))

# === Loop through tables
for table in tables:
    print(f"🔄 Processing table: {table}")

    # Add updated_at column if missing
    cur.execute(sql.SQL("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = %s
                AND table_name = %s
                AND column_name = 'updated_at'
            ) THEN
                EXECUTE format(
                    'ALTER TABLE "%I"."%I" ADD COLUMN updated_at TIMESTAMP DEFAULT now()',
                    %s, %s
                );
            END IF;
        END$$;
    """), (schema, table, schema, table))

    # Add BEFORE UPDATE trigger
    trigger_name = f"trg_update_timestamp_{table}"
    cur.execute(sql.SQL("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_trigger WHERE tgname = %s
            ) THEN
                EXECUTE format(
                    'CREATE TRIGGER {} BEFORE UPDATE ON "{}"."{}"
                     FOR EACH ROW EXECUTE FUNCTION "{}".update_timestamp()',
                    %s, %s, %s, %s
                );
            END IF;
        END$$;
    """), (trigger_name, trigger_name, schema, table, schema))

# === Done
conn.commit()
cur.close()
conn.close()

print("✅ All tables updated with `updated_at` tracking.")
