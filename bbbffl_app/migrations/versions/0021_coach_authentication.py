"""Coach authentication credentials and server-side sessions (roadmap
package 19, issue #74).

Two new tables, both referencing the existing persistent `coach` identity
from migration 0005 -- this package deliberately does not introduce a
second coach/user model (see docs/coach-authentication.md):

- `coach_credential` -- one managed password hash per coach. `coach_id` is
  the primary key (a coach has at most one active credential; there is no
  separate credential identity), so it can never be attached to anything
  but the real persistent coach row.
- `coach_session` -- server-authoritative session state. The bearer token a
  browser holds in its cookie is never stored directly: `token_hash` is a
  SHA-256 digest of it (the same defence-in-depth reasoning as storing a
  password hash rather than the password), so a database read alone cannot
  produce a usable session token. `session_id` is a separate, non-secret
  identifier safe to reference in audit events/logs. `expires_at` bounds
  lifetime; `revoked_at` supports logout/revocation without deleting the
  row (so a reused revoked/expired token can still be told apart from one
  that never existed, for diagnostics). `rotated_from_session_id` is an
  optional, unenforced lineage pointer -- authentication success always
  rotates to a brand-new `session_id`/token rather than mutating a
  pre-existing (e.g. pre-login) session in place.

Also adds a case-insensitive uniqueness constraint on `coach.email` where
set: `app.identity.IdentityRepository.get_coach_by_email` (the login lookup)
depends on email being unique among coaches that have one, which migration
0005 did not itself require (a coach with no email/no login access is still
valid). Implemented as a raw partial expression index rather than
`op.create_index` because both supported dialects accept the identical
`CREATE UNIQUE INDEX ... (lower(email)) WHERE email IS NOT NULL` syntax.
"""

import sqlalchemy as sa
from alembic import op

revision = "0021_coach_authentication"
down_revision = "0020_opening_round_deferral"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "coach_credential",
        sa.Column("coach_id", sa.Text(), sa.ForeignKey("coach.coach_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
    )
    op.create_table(
        "coach_session",
        sa.Column("session_id", sa.Text(), primary_key=True),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("coach_id", sa.Text(), sa.ForeignKey("coach.coach_id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("last_seen_at", sa.Text(), nullable=False),
        sa.Column("revoked_at", sa.Text()),
        sa.Column("rotated_from_session_id", sa.Text()),
        sa.UniqueConstraint("token_hash", name="uq_coach_session_token_hash"),
    )
    op.create_index("ix_coach_session_coach", "coach_session", ["coach_id"])

    op.execute("CREATE UNIQUE INDEX ix_coach_email_ci ON coach (lower(email)) WHERE email IS NOT NULL")


def downgrade():
    bind = op.get_bind()
    counts = sum(
        bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        for table in ("coach_credential", "coach_session")
    )
    if counts:
        raise RuntimeError(
            "0021 downgrade refused: coach_credential/coach_session hold data the prior schema cannot represent"
        )
    op.execute("DROP INDEX IF EXISTS ix_coach_email_ci")
    op.drop_index("ix_coach_session_coach", table_name="coach_session")
    op.drop_table("coach_session")
    op.drop_table("coach_credential")
