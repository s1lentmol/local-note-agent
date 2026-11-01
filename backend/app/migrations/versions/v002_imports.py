from alembic import op
import sqlalchemy as sa

revision = '002'
down_revision = '001'


def upgrade():
    with op.batch_alter_table('notes') as batch:
        batch.alter_column('extracted_text', existing_type=sa.Text(), nullable=True)
        for name, default in [('ingestion_status', 'ready'), ('enrichment_status', 'not_requested')]:
            batch.add_column(sa.Column(name, sa.String(20), nullable=False, server_default=default))
        batch.add_column(sa.Column('enrichment_version', sa.Integer()))
        batch.add_column(sa.Column('summary', sa.Text()))
        batch.add_column(sa.Column('category', sa.String(200)))
        for name in ['tags', 'entities']:
            batch.add_column(sa.Column(name, sa.JSON(), nullable=False, server_default='[]'))
        for name in ['ingestion_error', 'enrichment_error']:
            batch.add_column(sa.Column(name, sa.JSON()))
        batch.add_column(sa.Column('deleted_at', sa.DateTime(timezone=True)))
        batch.create_index('ix_notes_deleted_at', ['deleted_at'])
    with op.batch_alter_table('sources') as batch:
        batch.alter_column('original_text', existing_type=sa.Text(), nullable=True)
        batch.add_column(sa.Column('original_url', sa.Text()))
        batch.add_column(sa.Column('metadata_json', sa.JSON(), nullable=False, server_default='{}'))
    op.create_table('attachments',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('source_id', sa.String(36), sa.ForeignKey('sources.id', ondelete='CASCADE'), nullable=False, unique=True),
        sa.Column('original_name', sa.String(255), nullable=False),
        sa.Column('storage_key', sa.String(300), nullable=False, unique=True),
        sa.Column('mime_type', sa.String(100), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    op.create_table('jobs',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('note_id', sa.String(36), sa.ForeignKey('notes.id', ondelete='CASCADE'), nullable=False),
        sa.Column('kind', sa.String(20), nullable=False),
        sa.Column('input_version', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('error', sa.JSON()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True)),
        sa.Column('finished_at', sa.DateTime(timezone=True)))
    op.create_index('ix_jobs_note_id', 'jobs', ['note_id'])
    op.create_index('ix_jobs_queue', 'jobs', ['status', 'created_at'])
    op.create_index('uq_jobs_active', 'jobs', ['note_id', 'kind', 'input_version'], unique=True,
                    sqlite_where=sa.text("status IN ('queued', 'processing')"))
    op.create_table('worker_state',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('capabilities', sa.JSON(), nullable=False))


def downgrade():
    raise RuntimeError('Downgrade would discard imported data; restore a backup instead.')
