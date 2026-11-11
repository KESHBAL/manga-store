import os
import re
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

load_dotenv()


def clean_url(raw: str) -> str:
    """Достаёт адрес базы, даже если при копировании вокруг него попал лишний текст:
    DATABASE_URL=..., кавычки, пробелы, psql '...' и т.п."""
    raw = (raw or "").strip()
    m = re.search(r"postgres(?:ql)?://[^\s'\"]+", raw)
    url = m.group(0) if m else raw
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url or "sqlite:///./manga.db"


URL = clean_url(os.getenv("DATABASE_URL"))

kwargs = {"check_same_thread": False} if URL.startswith("sqlite") else {}
# pool_pre_ping нужен, т.к. бесплатная облачная БД "засыпает" и соединение может оборваться
engine = create_engine(URL, pool_pre_ping=True, connect_args=kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()