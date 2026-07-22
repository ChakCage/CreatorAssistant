"""initial licensing schema"""
from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    # Models are the single schema source; create_all here keeps the initial migration readable and complete.
    from app.db import Base
    from app import models  # noqa
    Base.metadata.create_all(bind=op.get_bind())

def downgrade():
    from app.db import Base
    from app import models  # noqa
    Base.metadata.drop_all(bind=op.get_bind())
