"""
Run migration: add monitoring columns to saved_filters.
Execute: python run_migration.py
"""
import os

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy import create_engine

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise SystemExit("DATABASE_URL not set in .env")

engine = create_engine(DATABASE_URL)
statements = [
    "ALTER TABLE saved_filters ADD COLUMN IF NOT EXISTS monitor_interval_minutes INTEGER NULL",
    "ALTER TABLE saved_filters ADD COLUMN IF NOT EXISTS monitoring_enabled BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE saved_filters ADD COLUMN IF NOT EXISTS last_monitoring_at TIMESTAMP WITH TIME ZONE NULL",
    "ALTER TABLE saved_filters ADD COLUMN IF NOT EXISTS monitoring_started_at TIMESTAMP WITH TIME ZONE NULL",
]

with engine.connect() as conn:
    for sql in statements:
        conn.execute(text(sql))
    conn.commit()

print("Migration completed: monitoring columns added to saved_filters")
