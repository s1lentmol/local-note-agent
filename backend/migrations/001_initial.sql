CREATE EXTENSION IF NOT EXISTS vector;

CREATE DOMAIN safe_error AS jsonb CHECK (
    VALUE IS NULL OR (
        jsonb_typeof(VALUE) = 'object'
        AND VALUE ?& ARRAY['code', 'message']
        AND jsonb_typeof(VALUE->'code') = 'string'
        AND jsonb_typeof(VALUE->'message') = 'string'
        AND length(btrim(VALUE->>'code')) > 0
        AND length(btrim(VALUE->>'message')) > 0
    )
);

CREATE TABLE notes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title text NOT NULL CHECK (length(btrim(title)) BETWEEN 1 AND 200),
    title_is_explicit boolean NOT NULL DEFAULT false,
    extracted_text text CHECK (length(btrim(extracted_text)) BETWEEN 1 AND 100000),
    ingestion_status text NOT NULL CHECK (ingestion_status IN ('queued','processing','ready','failed')),
    processing_status text NOT NULL DEFAULT 'not_requested'
        CHECK (processing_status IN ('not_requested','queued','processing','ready','failed')),
    summary text,
    category text,
    tags jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(tags) = 'array'),
    manual_tags jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(manual_tags) = 'array'),
    entities jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(entities) = 'array'),
    ingestion_error safe_error,
    processing_error safe_error,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    CHECK (ingestion_status <> 'ready' OR extracted_text IS NOT NULL),
    CHECK (processing_status = 'not_requested' OR ingestion_status = 'ready')
);
CREATE INDEX notes_listing ON notes (created_at DESC, id DESC) WHERE deleted_at IS NULL;

CREATE TABLE sources (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    note_id uuid NOT NULL UNIQUE REFERENCES notes ON DELETE CASCADE,
    type text NOT NULL CHECK (type IN ('text','text_file','url','image','audio','pdf')),
    original_text text,
    original_url text,
    channel text NOT NULL DEFAULT 'web' CHECK (channel IN ('web','telegram')),
    delivery_scope text,
    delivery_id text,
    telegram_chat_id bigint,
    telegram_message_id bigint,
    metadata jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(metadata) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (channel, delivery_scope, delivery_id),
    CHECK ((type = 'text') = (original_text IS NOT NULL)),
    CHECK ((type = 'url') = (original_url IS NOT NULL)),
    CHECK (original_text IS NULL OR length(btrim(original_text)) BETWEEN 1 AND 100000),
    CHECK (original_url IS NULL OR original_url ~ '^https?://'),
    CHECK ((delivery_id IS NULL) = (delivery_scope IS NULL)),
    CHECK (channel <> 'telegram' OR (delivery_id IS NOT NULL AND telegram_chat_id IS NOT NULL
           AND telegram_message_id IS NOT NULL))
);

CREATE TABLE attachments (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id uuid NOT NULL REFERENCES sources ON DELETE CASCADE,
    original_name text NOT NULL CHECK (length(original_name) > 0),
    storage_key text NOT NULL UNIQUE CHECK (storage_key <> '' AND storage_key !~ '(^/|(^|/)\.\.(/|$))'),
    mime_type text NOT NULL,
    size_bytes bigint NOT NULL CHECK (size_bytes > 0),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE collections (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title text NOT NULL CHECK (length(btrim(title)) BETWEEN 1 AND 200),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE collection_notes (
    collection_id uuid NOT NULL REFERENCES collections ON DELETE CASCADE,
    note_id uuid NOT NULL REFERENCES notes ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (collection_id, note_id)
);
CREATE INDEX collection_notes_note ON collection_notes (note_id);

CREATE TABLE import_batches (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    collection_id uuid REFERENCES collections ON DELETE SET NULL,
    channel text NOT NULL CHECK (channel IN ('web','telegram')),
    delivery_scope text,
    external_id text,
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','processing','done','failed')),
    total integer NOT NULL CHECK (total > 0),
    succeeded integer NOT NULL DEFAULT 0 CHECK (succeeded >= 0),
    failed integer NOT NULL DEFAULT 0 CHECK (failed >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (channel, delivery_scope, external_id),
    CHECK ((delivery_scope IS NULL) = (external_id IS NULL)),
    CHECK (channel <> 'telegram' OR external_id IS NOT NULL),
    CHECK (succeeded + failed <= total)
);
CREATE TABLE import_items (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id uuid NOT NULL REFERENCES import_batches ON DELETE CASCADE,
    position integer NOT NULL CHECK (position >= 0),
    original_name text NOT NULL,
    original_path text,
    delivery_id text,
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','processing','done','failed')),
    note_id uuid REFERENCES notes ON DELETE SET NULL,
    error safe_error,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (batch_id, position),
    UNIQUE (batch_id, delivery_id)
);

CREATE TABLE jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    note_id uuid NOT NULL REFERENCES notes ON DELETE CASCADE,
    kind text NOT NULL CHECK (kind IN ('extract','process','cleanup')),
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','processing','done','failed','cancelled')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    error safe_error,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    lease_until timestamptz,
    lease_token uuid,
    UNIQUE (id, note_id),
    CHECK (status <> 'processing' OR (lease_until IS NOT NULL AND lease_token IS NOT NULL
           AND started_at IS NOT NULL)),
    CHECK (status NOT IN ('done','failed','cancelled') OR finished_at IS NOT NULL)
);
CREATE UNIQUE INDEX jobs_one_active_kind ON jobs (note_id, kind) WHERE status IN ('queued','processing');
CREATE INDEX jobs_queue ON jobs (status, created_at);

-- A single embedding space. Replacing the profile requires deleting chunks first.
CREATE TABLE embedding_profiles (
    id boolean PRIMARY KEY DEFAULT true CHECK (id),
    model text NOT NULL,
    revision text NOT NULL,
    dimensions integer NOT NULL CHECK (dimensions = 1024),
    metric text NOT NULL CHECK (metric = 'cosine'),
    normalization text NOT NULL CHECK (normalization = 'l2'),
    query_instruction text NOT NULL
);
INSERT INTO embedding_profiles VALUES (
    true, 'qwen3-embedding:0.6b',
    'ac6da0dfba84a81fdbfbaf330198c33cd77c4cdfc53e8bc50eb581914a15621d',
    1024, 'cosine', 'l2',
    E'Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery: '
);
CREATE TABLE chunks (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    note_id uuid NOT NULL REFERENCES notes ON DELETE CASCADE,
    job_id uuid NOT NULL,
    position integer NOT NULL CHECK (position >= 0),
    text text NOT NULL CHECK (length(btrim(text)) > 0),
    embedding vector(1024) NOT NULL CHECK (vector_norm(embedding) > 0),
    embedding_profile_id boolean NOT NULL DEFAULT true REFERENCES embedding_profiles,
    UNIQUE (note_id, position),
    FOREIGN KEY (job_id, note_id) REFERENCES jobs (id, note_id) ON DELETE CASCADE
);
CREATE FUNCTION guard_embedding_profile() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM chunks) THEN
        RAISE EXCEPTION 'Delete chunks before changing embedding profile';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER embedding_profile_update BEFORE UPDATE ON embedding_profiles
    FOR EACH ROW EXECUTE FUNCTION guard_embedding_profile();

CREATE TABLE enrichments (
    note_id uuid PRIMARY KEY REFERENCES notes ON DELETE CASCADE,
    job_id uuid NOT NULL,
    model text NOT NULL,
    prompt_version text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (job_id, note_id) REFERENCES jobs (id, note_id) ON DELETE CASCADE
);
CREATE TABLE note_links (
    source_note_id uuid NOT NULL REFERENCES notes ON DELETE CASCADE,
    target_note_id uuid NOT NULL REFERENCES notes ON DELETE CASCADE,
    type text NOT NULL DEFAULT 'semantic' CHECK (type = 'semantic'),
    score double precision NOT NULL CHECK (score BETWEEN -1 AND 1),
    calculation_source text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_note_id, target_note_id, type),
    CHECK (source_note_id < target_note_id)
);
CREATE INDEX note_links_target ON note_links (target_note_id);

CREATE TABLE conversations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    channel text NOT NULL CHECK (channel IN ('web','telegram')),
    external_id text,
    delivery_scope text,
    collection_id uuid REFERENCES collections ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (channel, delivery_scope, external_id),
    CHECK ((external_id IS NULL) = (delivery_scope IS NULL))
);
CREATE TABLE messages (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL REFERENCES conversations ON DELETE CASCADE,
    role text NOT NULL CHECK (role IN ('user','assistant','tool','system')),
    text text NOT NULL,
    operations jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(operations) = 'array'),
    tool_calls jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(tool_calls) = 'array'),
    tool_call_id text,
    delivery_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (conversation_id, delivery_id)
);
CREATE INDEX messages_history ON messages (conversation_id, created_at, id);
