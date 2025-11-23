from contextlib import contextmanager

from sqlmodel import SQLModel, create_engine, Session

from app.config import settings

engine = create_engine(settings.database_url, echo=False, future=True)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)


@contextmanager
def get_db_session() -> Session:
    """
    Simple context manager to get a SQLModel Session.
    Use in non-request code (services).
    """
    with Session(engine) as session:
        yield session