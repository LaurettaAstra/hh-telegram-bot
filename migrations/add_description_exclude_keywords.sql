-- Add description_exclude_keywords to saved_filters
-- Run: psql $DATABASE_URL -f migrations/add_description_exclude_keywords.sql

ALTER TABLE saved_filters ADD COLUMN IF NOT EXISTS description_exclude_keywords TEXT;
