import os
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

load_dotenv()

URL = os.getenv("DATABASE_URL") or "sqlite:///./manga.db"
if URL.startswith("postgres://"):
    URL = URL.replace("postgres://", "postgresql://", 1)

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
