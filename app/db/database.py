from sqlalchemy import create_engine, Column, Integer, String, Text, Float
from sqlalchemy.orm import DeclarativeBase, Session
from sqlalchemy.pool import NullPool
import json
import os

# launcher.py sets EVE_APP_DIR to a writable location (next to the .exe /
# .AppImage). Fall back to the project root for dev mode.
_APP_DIR = os.environ.get("EVE_APP_DIR") or os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
DB_PATH = os.path.join(_APP_DIR, "eve_cache.db")


class Base(DeclarativeBase):
    pass


class TypeCache(Base):
    __tablename__ = "type_cache"
    type_id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    group_id = Column(Integer)
    category_id = Column(Integer)


class BlueprintCache(Base):
    __tablename__ = "blueprint_cache"
    type_id = Column(Integer, primary_key=True)  # type_id produktu
    blueprint_type_id = Column(Integer)
    data_json = Column(Text, nullable=False)  # raw JSON z Fuzzwork API
    cached_at = Column(Float)  # unix timestamp


# NullPool: open a fresh sqlite3 connection per query. Avoids stale FDs after
# the SDE-download `shutil.move(...)` replaces eve_cache.db, which otherwise
# leaves pooled connections holding the old inode and raises
# "(sqlite3.OperationalError) attempt to write a readonly database"
# (SQLITE_READONLY_DBMOVED) on the next INSERT.
engine = create_engine(
    f"sqlite:///{os.path.abspath(DB_PATH)}",
    poolclass=NullPool,
)
Base.metadata.create_all(engine)


def get_session() -> Session:
    return Session(engine)


def ensure_user_tables() -> None:
    """Re-runs Base.metadata.create_all on the live engine.

    Necessary whenever eve_cache.db is replaced from outside (fresh-install
    copy of bundled sde_base.db, /setup SDE download, …) — the bundled file
    only has SDE tables, so the SQLAlchemy-managed user tables
    (type_cache, blueprint_cache) must be recreated.
    """
    Base.metadata.create_all(engine)
