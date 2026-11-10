"""ORM models. Import each model module here so Alembic's autogenerate can see its tables."""

from app.models.base import Base

__all__ = ["Base"]
