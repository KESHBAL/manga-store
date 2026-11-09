"""Заполняет БД случайными товарами через Faker (только товары, без пользователей).

Запуск (из папки backend, с включённым venv):
    python fake_data.py                  (30 товаров)
    python fake_data.py --products 50
"""
import argparse
import random
from faker import Faker

from database import SessionLocal, engine
from models import Base, Product
from genres import GENRES

# Из этих двух списков собираются названия: «Клинок» + «тумана» = «Клинок тумана»
HEADS = ["Клинок", "Дракон", "Город", "Тень", "Песня", "Школа", "Король", "Хроники", "Академия", "Охотники",
         "Печать", "Врата", "Страж", "Наследие", "Лес", "Башня", "Ученики", "Путь", "Эхо", "Легенда"]
ENDINGS = ["тумана", "алой луны", "забытых богов", "тёмной зари", "падающих звёзд", "ледяного ветра",
           "стальной воли", "спящих теней", "последней весны", "тысячи масок", "разбитого неба", "чёрного лотоса",
           "морских демонов", "багряных клинков", "утренней звезды", "древних клятв"]


def make_title(used: set) -> str:
    for _ in range(200):
        t = f"{random.choice(HEADS)} {random.choice(ENDINGS)}"
        if t not in used:
            used.add(t)
            return t
    t = f"Том без названия {len(used)}"
    used.add(t)
    return t


def main(n_products: int):
    Base.metadata.create_all(engine)
    db = SessionLocal()
    fk_ru = Faker("ru_RU")   # русский текст для описаний
    fk_jp = Faker("ja_JP")   # японские имена латиницей для авторов

    used_titles = {t for (t,) in db.query(Product.title).all()}  # чтобы не повторять уже существующие названия
    products = []
    for _ in range(n_products):
        author = f"{fk_jp.first_romanized_name().title()} {fk_jp.last_romanized_name().title()}"
        products.append(Product(
            title=make_title(used_titles),
            author=author,
            description=fk_ru.text(max_nb_chars=200),
            price=random.randrange(2500, 6100, 100),
            stock=random.randint(0, 30),
            cover_url="",                       # пока без обложки, покажется заглушка
            genre=random.choice(GENRES),
            volume=random.randint(1, 12),
        ))
    db.add_all(products)
    db.commit()
    print(f"Добавлено товаров: {len(products)}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--products", type=int, default=30)
    main(p.parse_args().products)