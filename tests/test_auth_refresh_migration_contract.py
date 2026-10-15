"""Guard the source migration needed by real pairing against schema drift.

This test is an *offline* Alembic/model check, not proof that a PostgreSQL
migration ran. CI's PostgreSQL integration job runs Alembic separately.
"""
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.models import AuthSession


ROOT = Path(__file__).resolve().parents[1]


def test_refresh_token_columns_have_head_migration_and_nullable_model():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_current_head() == "0008"
    revision = scripts.get_revision("0008")
    assert revision.down_revision == "0007"

    for field in ("refresh_token_hash", "refresh_token_expires_at"):
        assert field in AuthSession.__table__.c
        assert AuthSession.__table__.c[field].nullable

    assert any(
        index.unique and tuple(c.name for c in index.columns) == ("refresh_token_hash",)
        for index in AuthSession.__table__.indexes
    )

    # The migration may not silently be replaced with a destructive full-table
    # rewrite or a non-null default that invalidates old sessions.
    source = (ROOT / "migrations/versions/0008_refresh_token_session_columns.py").read_text()
    assert source.count("op.add_column(") == 2
    assert source.count("nullable=True") == 2
    assert source.count("op.create_index(") == 1
    assert "op.execute(" not in source
    assert "op.drop_table(" not in source
