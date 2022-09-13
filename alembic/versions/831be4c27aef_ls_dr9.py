"""LS DR9 migration

Revision ID: 831be4c27aef
Revises: 5466bd5ce2e5
Create Date: 2022-09-12 19:28:19.491197

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = '831be4c27aef'
down_revision = '5466bd5ce2e5'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.execute(
        """
alter type "public"."thumbnail_types" rename to "thumbnail_types__old_version_to_be_dropped";

create type "public"."thumbnail_types" as enum ('new', 'ref', 'sub', 'sdss', 'dr8', 'ls', 'ps1', 'new_gz', 'ref_gz', 'sub_gz');

alter table "public"."thumbnails" alter column type type "public"."thumbnail_types" using type::text::"public"."thumbnail_types";

drop type "public"."thumbnail_types__old_version_to_be_dropped";
"""
    )
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    pass
    # ### end Alembic commands ###
