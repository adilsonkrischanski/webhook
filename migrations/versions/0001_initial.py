"""Initial webhook schema."""
from alembic import op
import sqlalchemy as sa

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('endpoint',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(120), nullable=False),
        sa.Column('path', sa.String(240), nullable=False, unique=True),
        sa.Column('auth_type', sa.String(10), nullable=False),
        sa.Column('username', sa.String(100)),
        sa.Column('secret_hash', sa.String(256)),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('deleted', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False))
    op.create_table('webhook_request',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('endpoint_id', sa.Integer(), sa.ForeignKey('endpoint.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('method', sa.String(10), nullable=False),
        sa.Column('path', sa.Text(), nullable=False),
        sa.Column('query', sa.Text(), nullable=False),
        sa.Column('headers', sa.Text(), nullable=False),
        sa.Column('body', sa.LargeBinary(), nullable=False),
        sa.Column('content_type', sa.Text()),
        sa.Column('ip', sa.String(100)),
        sa.Column('authenticated', sa.Boolean(), nullable=False),
        sa.Column('status', sa.Integer(), nullable=False))
    op.create_index('ix_webhook_request_endpoint_id', 'webhook_request', ['endpoint_id'])
    op.create_index('ix_webhook_request_created_at', 'webhook_request', ['created_at'])
    op.create_table('login_attempt',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('ip', sa.String(100), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False))
    op.create_index('ix_login_attempt_ip', 'login_attempt', ['ip'])
    op.create_index('ix_login_attempt_created_at', 'login_attempt', ['created_at'])


def downgrade():
    op.drop_table('login_attempt')
    op.drop_table('webhook_request')
    op.drop_table('endpoint')
