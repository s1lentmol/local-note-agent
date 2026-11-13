-- Explicit configurations make stored vectors independent of session defaults.
ALTER TABLE notes ADD COLUMN search_ru tsvector GENERATED ALWAYS AS (
    setweight(to_tsvector('russian'::regconfig, title), 'A') ||
    setweight(to_tsvector('russian'::regconfig, coalesce(extracted_text, '')), 'B')
) STORED;
ALTER TABLE notes ADD COLUMN search_en tsvector GENERATED ALWAYS AS (
    setweight(to_tsvector('english'::regconfig, title), 'A') ||
    setweight(to_tsvector('english'::regconfig, coalesce(extracted_text, '')), 'B')
) STORED;
CREATE INDEX notes_search_ru ON notes USING gin(search_ru) WHERE deleted_at IS NULL;
CREATE INDEX notes_search_en ON notes USING gin(search_en) WHERE deleted_at IS NULL;
