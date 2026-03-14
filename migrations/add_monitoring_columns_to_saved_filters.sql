-- Add monitoring-related columns to saved_filters (schema fix)
-- Run: psql $DATABASE_URL -f migrations/add_monitoring_columns_to_saved_filters.sql
-- Safe: uses IF NOT EXISTS, existing rows get NULL/DEFAULT values

ALTER TABLE saved_filters ADD COLUMN IF NOT EXISTS monitor_interval_minutes INTEGER NULL;
ALTER TABLE saved_filters ADD COLUMN IF NOT EXISTS monitoring_enabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE saved_filters ADD COLUMN IF NOT EXISTS last_monitoring_at TIMESTAMP WITH TIME ZONE NULL;
ALTER TABLE saved_filters ADD COLUMN IF NOT EXISTS monitoring_started_at TIMESTAMP WITH TIME ZONE NULL;
