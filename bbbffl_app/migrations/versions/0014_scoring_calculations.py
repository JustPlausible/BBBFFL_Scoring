"""Bind season rules and durable calculation provenance to the scoring core."""

import sqlalchemy as sa
from alembic import op

revision = "0014_scoring"
down_revision = "0013_lockout_triggers"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("season_rules_version", sa.Column("scoring_rules", sa.Text(), nullable=True))
    for name, type_ in (
        ("input_fingerprint", sa.Text()),
        ("season_id", sa.Text()),
        ("rules_version_id", sa.Text()),
        ("bbbffl_round_id", sa.Text()),
        ("home_season_entry_id", sa.Text()),
        ("away_season_entry_id", sa.Text()),
        ("home_lineup_id", sa.Text()),
        ("home_lineup_version", sa.Integer()),
        ("away_lineup_id", sa.Text()),
        ("away_lineup_version", sa.Integer()),
        ("upstream_revision", sa.Text()),
        ("upstream_observed_at", sa.Text()),
        ("engine_version", sa.Text()),
    ):
        op.add_column("bbbffl_matchup_calculation", sa.Column(name, type_, nullable=True))


def downgrade():
    if op.get_bind().execute(sa.text("SELECT COUNT(*) FROM bbbffl_matchup_calculation")).scalar_one():
        raise RuntimeError("0014 downgrade refused: scoring calculation provenance would be lost")
    for name in (
        "engine_version",
        "upstream_observed_at",
        "upstream_revision",
        "away_lineup_version",
        "away_lineup_id",
        "home_lineup_version",
        "home_lineup_id",
        "away_season_entry_id",
        "home_season_entry_id",
        "bbbffl_round_id",
        "rules_version_id",
        "season_id",
        "input_fingerprint",
    ):
        op.drop_column("bbbffl_matchup_calculation", name)
    op.drop_column("season_rules_version", "scoring_rules")
