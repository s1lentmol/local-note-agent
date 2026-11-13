"""Atomic metadata and provenance publication inside the T06 guarded transaction."""

from psycopg.types.json import Jsonb

from app.dto import Enrichment
from app.enrichment import EnrichmentResult


class EnrichmentRepository:
    @staticmethod
    def publish(conn, job, result: EnrichmentResult):
        fields = Enrichment.model_validate(result.fields.model_dump(), strict=True)
        conn.execute(
            """UPDATE notes SET summary=%s, tags=%s, category=%s, entities=%s,
               updated_at=clock_timestamp() WHERE id=%s""",
            (
                fields.summary,
                Jsonb(fields.tags),
                fields.category,
                Jsonb(fields.entities),
                job.note_id,
            ),
        )
        conn.execute(
            """INSERT INTO enrichments(note_id,job_id,model,prompt_version)
               VALUES (%s,%s,%s,%s) ON CONFLICT (note_id) DO UPDATE
               SET job_id=EXCLUDED.job_id, model=EXCLUDED.model,
                   prompt_version=EXCLUDED.prompt_version, created_at=clock_timestamp()""",
            (job.note_id, job.id, result.model, result.prompt_version),
        )
