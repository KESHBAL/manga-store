"""Создаёт аккаунт мангаки (права как у админа: добавляет и меняет товары, загружает обложки).

Запуск (из папки backend, с включённым venv):
    python add_mangaka.py mangaka@manga.kz krasava123
"""
import sys
from database import SessionLocal, engine
from models import Base, User
from auth import hash_password

if len(sys.argv) != 3:
    sys.exit("Использование: python add_mangaka.py email пароль")

email, password = sys.argv[1].strip().lower(), sys.argv[2]
if "@" not in email or len(password) < 6:
    sys.exit("Нужен корректный email и пароль минимум 6 символов")

Base.metadata.create_all(engine)
db = SessionLocal()
user = db.query(User).filter_by(email=email).first()
if user:
    user.role = "admin"  # аккаунт уже есть (например, мангака зарегистрировался сам): пароль не трогаем, даём права
    print(f"Аккаунт {email} уже существовал, теперь у него права мангаки. Пароль остался прежним.")
else:
    db.add(User(email=email, password_hash=hash_password(password), role="admin"))
    print(f"Создан аккаунт мангаки: {email}")
db.commit()
