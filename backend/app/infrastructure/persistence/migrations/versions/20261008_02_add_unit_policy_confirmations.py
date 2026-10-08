"""Persist unit policy confirmation receipts in the append transaction.

Revision ID: 20261008_02
Revises: 20261008_01
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261008_02"
down_revision: str | None = "20261008_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE unit_policy_confirmations (
          idempotency_key uuid PRIMARY KEY,
          request_fingerprint bytea NOT NULL,
          confirmation_token_digest bytea NOT NULL,
          version bigint NOT NULL REFERENCES unit_policy_versions(version)
            ON DELETE RESTRICT,
          CONSTRAINT ck_unit_policy_confirmations_fingerprints
            CHECK (octet_length(request_fingerprint) = 32 AND
                   octet_length(confirmation_token_digest) = 32),
          CONSTRAINT ck_unit_policy_confirmations_key_v4
            CHECK (substring(idempotency_key::text from 15 for 1) = '4'),
          CONSTRAINT uq_unit_policy_confirmations_confirmation_token_digest
            UNIQUE (confirmation_token_digest)
        )
    """)


def downgrade() -> None:
    op.drop_table("unit_policy_confirmations")
