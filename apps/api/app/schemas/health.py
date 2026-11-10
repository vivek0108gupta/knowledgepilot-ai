from pydantic import BaseModel


class Health(BaseModel):
    status: str


class Readiness(BaseModel):
    status: str
    database: str
    pgvector: str
