from uuid import uuid4

import psycopg
import pytest


def note(db):
    return db.execute(
        "INSERT INTO notes (title, extracted_text, ingestion_status) VALUES (%s,%s,%s) RETURNING id",
        ("Заметка", "Текст", "ready"),
    ).fetchone()[0]


def job(db, note_id):
    return db.execute(
        "INSERT INTO jobs (note_id, kind) VALUES (%s, 'process') RETURNING id", (note_id,)
    ).fetchone()[0]


def test_jobs_and_note_constraints(db):
    note_id = note(db)
    job_id = job(db, note_id)
    with pytest.raises(psycopg.errors.UniqueViolation):
        job(db, note_id)
    for statement, params in [
        ("UPDATE jobs SET status='processing' WHERE id=%s", (job_id,)),
        ("UPDATE jobs SET attempts=-1 WHERE id=%s", (job_id,)),
        ("UPDATE jobs SET kind='enrich' WHERE id=%s", (job_id,)),
        ("UPDATE jobs SET status='done' WHERE id=%s", (job_id,)),
        ("UPDATE notes SET ingestion_status='unknown' WHERE id=%s", (note_id,)),
        ("UPDATE notes SET extracted_text=NULL WHERE id=%s", (note_id,)),
        ("UPDATE notes SET tags='{}' WHERE id=%s", (note_id,)),
        ("UPDATE notes SET processing_error='{}' WHERE id=%s", (note_id,)),
    ]:
        with pytest.raises(psycopg.errors.CheckViolation):
            db.execute(statement, params)
    token = uuid4()
    db.execute(
        """UPDATE jobs SET status='processing', started_at=now(),
        lease_until=now()+interval '1 minute', lease_token=%s WHERE id=%s""",
        (token, job_id),
    )
    assert db.execute("SELECT lease_token FROM jobs WHERE id=%s", (job_id,)).fetchone() == (token,)
    db.execute("UPDATE jobs SET status='cancelled', finished_at=now() WHERE id=%s", (job_id,))
    assert job(db, note_id) != job_id
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        job(db, uuid4())


def test_provenance_and_delivery_dedup(db):
    first, second = note(db), note(db)
    insert = """INSERT INTO sources (note_id,type,original_text,channel,delivery_scope,
        delivery_id,telegram_chat_id,telegram_message_id,metadata)
        VALUES (%s,'text','original','telegram','bot:1',%s,123,456,%s)"""
    from psycopg.types.json import Jsonb

    metadata = {"forward_origin": {"type": "user", "sender_user": {"id": 7}}}
    db.execute(insert, (first, "update:1", Jsonb(metadata)))
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute(insert, (second, "update:1", Jsonb(metadata)))
    db.execute(insert, (second, "update:2", Jsonb(metadata)))
    assert (
        db.execute("SELECT metadata FROM sources WHERE note_id=%s", (first,)).fetchone()[0]
        == metadata
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute(insert, (first, "update:3", Jsonb(metadata)))
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "INSERT INTO sources (note_id,type,channel) VALUES (%s,'audio','telegram')", (note(db),)
        )
    db.execute("UPDATE notes SET extracted_text='edited' WHERE id=%s", (first,))
    assert db.execute(
        "SELECT original_text FROM sources WHERE note_id=%s", (first,)
    ).fetchone() == ("original",)


def test_vectors_profile_and_job_ownership(db):
    note_id, other = note(db), note(db)
    job_id = job(db, note_id)
    vector = "[" + ",".join(["1"] + ["0"] * 1023) + "]"
    insert = """INSERT INTO chunks (note_id,job_id,position,text,embedding)
        VALUES (%s,%s,%s,'chunk',%s::vector)"""
    db.execute(insert, (note_id, job_id, 0, vector))
    assert db.execute("SELECT embedding <=> %s::vector FROM chunks", (vector,)).fetchone() == (0.0,)
    profile = db.execute(
        "SELECT model, revision, dimensions, metric FROM embedding_profiles"
    ).fetchone()
    assert profile == (
        "qwen3-embedding:0.6b",
        "ac6da0dfba84a81fdbfbaf330198c33cd77c4cdfc53e8bc50eb581914a15621d",
        1024,
        "cosine",
    )
    with pytest.raises(psycopg.errors.DataException, match="expected 1024 dimensions"):
        db.execute(insert, (note_id, job_id, 1, "[1,2]"))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute(insert, (other, job_id, 0, vector))
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute(insert, (note_id, job_id, 0, vector))
    with pytest.raises(psycopg.errors.RaiseException, match="Delete chunks"):
        db.execute("UPDATE embedding_profiles SET model='other'")
    db.execute("DELETE FROM chunks")
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE embedding_profiles SET dimensions=2")


def test_collections_imports_attachments_and_cascades(db):
    n = note(db)
    source = db.execute(
        "INSERT INTO sources (note_id,type) VALUES (%s,'audio') RETURNING id", (n,)
    ).fetchone()[0]
    collection = db.execute(
        "INSERT INTO collections (title) VALUES ('test') RETURNING id"
    ).fetchone()[0]
    db.execute("INSERT INTO collection_notes VALUES (%s,%s,now())", (collection, n))
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute("INSERT INTO collection_notes VALUES (%s,%s,now())", (collection, n))
    batch = db.execute(
        """INSERT INTO import_batches (collection_id,channel,total)
        VALUES (%s,'web',1) RETURNING id""",
        (collection,),
    ).fetchone()[0]
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE import_batches SET succeeded=2 WHERE id=%s", (batch,))
    db.execute(
        """INSERT INTO import_items (batch_id,position,original_name,note_id)
        VALUES (%s,0,'voice.ogg',%s)""",
        (batch, n),
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute(
            "INSERT INTO import_items (batch_id,position,original_name) VALUES (%s,0,'x')", (batch,)
        )
    insert = """INSERT INTO attachments (source_id,original_name,storage_key,mime_type,size_bytes)
        VALUES (%s,'voice.ogg',%s,'audio/ogg',%s)"""
    for path, size in [("../secret", 1), ("/absolute", 1), ("safe", 0)]:
        with pytest.raises(psycopg.errors.CheckViolation):
            db.execute(insert, (source, path, size))
    db.execute(insert, (source, "uploads/unique/original.ogg", 10))
    db.execute("DELETE FROM collections WHERE id=%s", (collection,))
    assert db.execute("SELECT count(*) FROM notes WHERE id=%s", (n,)).fetchone() == (1,)
    assert db.execute("SELECT collection_id FROM import_batches").fetchone() == (None,)
    db.execute("DELETE FROM notes WHERE id=%s", (n,))
    assert db.execute("SELECT count(*) FROM attachments").fetchone() == (0,)
    assert db.execute("SELECT note_id FROM import_items").fetchone() == (None,)


def test_links_and_conversation_delivery(db):
    first, second = sorted([note(db), note(db)])
    for a, b, score in [(first, first, 0.5), (second, first, 0.5), (first, second, 2)]:
        with pytest.raises(psycopg.errors.CheckViolation):
            db.execute(
                """INSERT INTO note_links (source_note_id,target_note_id,score,calculation_source)
                VALUES (%s,%s,%s,'cosine')""",
                (a, b, score),
            )
    conversation = db.execute("""INSERT INTO conversations (channel,external_id,delivery_scope)
        VALUES ('telegram','chat:1','bot:1') RETURNING id""").fetchone()[0]
    query = (
        "INSERT INTO messages (conversation_id,role,text,delivery_id) VALUES (%s,'user','hi','1')"
    )
    db.execute(query, (conversation,))
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute(query, (conversation,))
    db.execute("DELETE FROM conversations WHERE id=%s", (conversation,))
    assert db.execute("SELECT count(*) FROM messages").fetchone() == (0,)
