from sqlalchemy import select, update

from app.models import Job, utc_now


def enqueue(session, note, kind):
    session.flush()
    existing = session.scalar(
        select(Job).where(
            Job.note_id == note.id,
            Job.kind == kind,
            Job.input_version == note.content_version,
            Job.status.in_(["queued", "processing"]),
        )
    )
    if existing:
        return existing
    job = Job(note_id=note.id, kind=kind, input_version=note.content_version)
    session.add(job)
    return job


def cancel_pending(session, note_id, kind=None):
    query = update(Job).where(Job.note_id == note_id, Job.status == "queued")
    if kind:
        query = query.where(Job.kind == kind)
    session.execute(query.values(status="cancelled", finished_at=utc_now()))


def queue_enrichment(session, note, settings):
    note.summary = note.category = note.enrichment_version = None
    note.tags = []
    note.entities = []
    note.enrichment_error = None
    cancel_pending(session, note.id, "enrich")
    note.enrichment_status = "queued" if settings.knowledge_adapter else "not_requested"
    if settings.knowledge_adapter:
        enqueue(session, note, "enrich")
