"""Custom SQLAlchemy column types."""

from decimal import Decimal
from typing import Any

from sqlalchemy import Numeric, Text
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator, TypeEngine


class ExactDecimal(TypeDecorator[Decimal]):
    """A Decimal column that round-trips exactly on every database.

    SQLite has no decimal type: a NUMERIC column stores REAL (a binary float), so
    `64999.123456789012` comes back as `...013` and PnL sums drift. On SQLite the value is
    stored as TEXT in plain (non-exponent) notation instead; PostgreSQL keeps a real NUMERIC.

    Ordering or arithmetic on these columns inside SQL is not meaningful on SQLite (text
    compares lexicographically), so do such calculations in Python.
    """

    impl = Numeric
    cache_ok = True

    def __init__(self, precision: int = 28, scale: int = 12) -> None:
        super().__init__(precision=precision, scale=scale, asdecimal=True)
        self.precision = precision
        self.scale = scale

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "sqlite":
            return dialect.type_descriptor(Text())
        return dialect.type_descriptor(Numeric(self.precision, self.scale, asdecimal=True))

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        dec = value if isinstance(value, Decimal) else Decimal(str(value))
        if dialect.name == "sqlite":
            return format(dec, "f")
        return dec

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        if isinstance(value, Decimal):
            return value
        # Rows written before the TEXT migration may still come back as float.
        return Decimal(str(value))
