CREATE TABLE IF NOT EXISTS resume_auto_updates (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    resume_id TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    next_run_at TIMESTAMP WITH TIME ZONE NOT NULL,
    last_success_at TIMESTAMP WITH TIME ZONE NULL,
    last_error TEXT NULL
);
