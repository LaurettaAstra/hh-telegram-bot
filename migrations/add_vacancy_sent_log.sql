-- Deduplication table: track vacancies sent per user per filter by HH vacancy_id.
-- Ensures each vacancy is sent only once, even if HH re-indexes or updates it.
-- Run: psql $DATABASE_URL -f migrations/add_vacancy_sent_log.sql

CREATE TABLE IF NOT EXISTS vacancy_sent_log (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    filter_id INTEGER NOT NULL REFERENCES saved_filters(id),
    vacancy_id TEXT NOT NULL,
    first_seen_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    sent_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_vacancy_sent_user_filter
    ON vacancy_sent_log (user_id, filter_id, vacancy_id);
