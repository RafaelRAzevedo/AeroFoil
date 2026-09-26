"""Add retro ROM library and retro request tables.

Revision ID: f1a2b3c4d5e6
Revises: d8e7f6a5b4c3
"""

from alembic import op
import sqlalchemy as sa


revision = 'f1a2b3c4d5e6'
down_revision = 'd8e7f6a5b4c3'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'roms',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('platform', sa.String(length=32), nullable=False),
        sa.Column('name', sa.String(length=512), nullable=False),
        sa.Column('relpath', sa.String(length=1024), nullable=False),
        sa.Column('size', sa.BigInteger(), nullable=False),
        sa.Column('added_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('relpath'),
    )
    op.create_index('ix_roms_platform', 'roms', ['platform'])

    op.create_table(
        'retro_requests',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('platform', sa.String(length=32), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('admin_note', sa.String(length=500), nullable=True),
        sa.Column('rom_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_retro_requests_platform', 'retro_requests', ['platform'])
    op.create_index('ix_retro_requests_status', 'retro_requests', ['status'])

    op.create_table(
        'retro_request_users',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('request_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('note', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['request_id'], ['retro_requests.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('request_id', 'user_id', name='uq_retro_request_users_request_user'),
    )
    op.create_index('ix_retro_request_users_request_id', 'retro_request_users', ['request_id'])
    op.create_index('ix_retro_request_users_user_id', 'retro_request_users', ['user_id'])

    op.create_table(
        'retro_request_views',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('request_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('viewed_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['request_id'], ['retro_requests.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('request_id', 'user_id', name='uq_retro_request_views_request_user'),
    )
    op.create_index('ix_retro_request_views_request_id', 'retro_request_views', ['request_id'])
    op.create_index('ix_retro_request_views_user_id', 'retro_request_views', ['user_id'])


def downgrade():
    op.drop_index('ix_retro_request_views_user_id', table_name='retro_request_views')
    op.drop_index('ix_retro_request_views_request_id', table_name='retro_request_views')
    op.drop_table('retro_request_views')
    op.drop_index('ix_retro_request_users_user_id', table_name='retro_request_users')
    op.drop_index('ix_retro_request_users_request_id', table_name='retro_request_users')
    op.drop_table('retro_request_users')
    op.drop_index('ix_retro_requests_status', table_name='retro_requests')
    op.drop_index('ix_retro_requests_platform', table_name='retro_requests')
    op.drop_table('retro_requests')
    op.drop_index('ix_roms_platform', table_name='roms')
    op.drop_table('roms')
