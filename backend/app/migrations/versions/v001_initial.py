import sqlalchemy as sa
from alembic import op

revision = "001"
down_revision = None


def upgrade():
    op.create_table(
        "notes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("extracted_text", sa.Text(), nullable=False),
        sa.Column("content_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_notes_created_at", "notes", ["created_at"])
    op.create_table(
        "sources",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "note_id",
            sa.String(36),
            sa.ForeignKey("notes.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("original_text", sa.Text(), nullable=False),
    )


def downgrade():
    op.drop_table("sources")
    op.drop_table("notes")
