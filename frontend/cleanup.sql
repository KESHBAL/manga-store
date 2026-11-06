-- Привести старые жанры в порядок (выполнять в Neon: SQL Editor). Копируй отсюда, а не из чата.
UPDATE products SET genre = 'Сёнэн' WHERE genre IN ('Сёнен', 'сёнен', 'Сёнэн ');
UPDATE products SET genre = 'Сэйнэн' WHERE genre IN ('Сэйнен', 'сэйнен');
UPDATE products SET genre = 'Фэнтези' WHERE genre IN ('Фэнтэзи', 'фэнтэзи', 'фэнтези');
UPDATE products SET genre = 'Меха' WHERE genre IN ('меха');
UPDATE products SET genre = 'Спорт' WHERE genre IN ('спокон', 'спорт');

-- Проверка: покажет жанры, которых нет в списке (если пусто, всё в порядке)
SELECT DISTINCT genre FROM products WHERE genre NOT IN ('Сёнэн', 'Сэйнэн', 'Сёдзё', 'Дзёсэй', 'Экшен', 'Приключения', 'Фэнтези', 'Фантастика', 'Комедия', 'Драма', 'Романтика', 'Триллер', 'Детектив', 'Ужасы', 'Спорт', 'Меха', 'Повседневность', 'Другое');
