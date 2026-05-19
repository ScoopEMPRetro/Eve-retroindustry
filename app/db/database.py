from sqlalchemy import create_engine, Column, Integer, String, Text, Float
from sqlalchemy.orm import DeclarativeBase, Session
import json
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "../../eve_cache.db")


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


engine = create_engine(f"sqlite:///{os.path.abspath(DB_PATH)}")
Base.metadata.create_all(engine)


def get_session() -> Session:
    return Session(engine)
