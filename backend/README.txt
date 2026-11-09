ЗАПУСК (из папки backend)

1. python -m venv venv
2. venv\Scripts\activate
3. python -m pip install -r requirements.txt
4. Скопируй .env.example в .env (потом туда вставишь строку из Neon)
5. python seed.py            (создаст таблицы, админа и тестовые товары)
6. python -m uvicorn main:app --reload
7. Открой http://127.0.0.1:8000  (документация API: http://127.0.0.1:8000/docs)

Админ: admin@manga.kz / admin123 (поменяй пароль в seed.py до первого запуска)
Обложки: положи файлы в frontend/img/ и в админке укажи путь вида /img/berserk.jpg
