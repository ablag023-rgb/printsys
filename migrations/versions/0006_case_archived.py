"""архивные копии документов в деле и признак «требует внимания»

Из одноимённых копий документа в дело идёт только актуальная — самая свежая
по дате изменения в хранилище. Отброшенные копии складываются в `archived`:
они не печатаются, но остаются видимыми, чтобы оператор мог проверить, что
именно система сочла неактуальным.

`needs_attention` поднимается, когда выбрать версию нельзя: даты копий
совпадают, а содержимое различается. Молча взять любую нельзя — не та
редакция справки означает неверную сумму иска.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-04
"""
from alembic import op
import sqlalchemy as sa


revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    # server_default нужен: у существующих дел значения нет, а колонки NOT NULL.
    # После заливки дефолт снимаем — значение проставляет код при пересборке.
    op.add_column("cases", sa.Column("archived", sa.JSON(), nullable=False,
                                     server_default="[]"))
    op.add_column("cases", sa.Column("needs_attention", sa.Boolean(), nullable=False,
                                     server_default=sa.false()))
    op.create_index("ix_cases_needs_attention", "cases", ["needs_attention"])
    op.alter_column("cases", "archived", server_default=None)
    op.alter_column("cases", "needs_attention", server_default=None)


def downgrade():
    op.drop_index("ix_cases_needs_attention", table_name="cases")
    op.drop_column("cases", "needs_attention")
    op.drop_column("cases", "archived")
