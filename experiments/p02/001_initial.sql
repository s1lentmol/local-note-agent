CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS p02_notes (
    id text PRIMARY KEY,
    language text NOT NULL CHECK (language IN ('ru', 'en')),
    title text NOT NULL,
    body text NOT NULL,
    model_digest text NOT NULL,
    embedding vector(1024) NOT NULL
);
