-- Add experience and employment columns to saved_filters for HH API params
-- Run: psql $DATABASE_URL -f migrations/add_experience_employment_to_saved_filters.sql

ALTER TABLE saved_filters ADD COLUMN IF NOT EXISTS experience TEXT;
ALTER TABLE saved_filters ADD COLUMN IF NOT EXISTS employment TEXT;
