-- Add monitoring_started_at column to saved_filters for tracking when monitoring was enabled
-- Run: psql $DATABASE_URL -f migrations/add_monitoring_started_at.sql

ALTER TABLE saved_filters ADD COLUMN IF NOT EXISTS monitoring_started_at TIMESTAMP WITH TIME ZONE NULL;
