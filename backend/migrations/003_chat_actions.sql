-- Committed action receipts survive a failed model response or process restart.
CREATE TABLE chat_actions (
    id uuid PRIMARY KEY,
    conversation_id uuid NOT NULL REFERENCES conversations(id),
    delivery_id text,
    operations jsonb NOT NULL DEFAULT '[]'::jsonb,
    response jsonb,
    UNIQUE (conversation_id, delivery_id)
);
