import os
import re
import json
import math
import urllib.parse
import urllib.request
import urllib.error
import datetime as dt
from urllib.parse import unquote
from fastapi import FastAPI, Depends, HTTPException, Query, UploadFile, File, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, func, delete, desc, or_
from sqlalchemy.orm import Session

from database import engine, get_db
from models import Base, User, Product, Favorite, CartItem, Order, OrderItem, Cover, Rating, Comment, PreviewPage, Payment, Profile
from genres import GENRES
from auth import hash_password, check_password, make_token, current_user, admin_only, bearer

Base.metadata.create_all(engine)  # создаёт таблицы в БД, если их ещё нет
app = FastAPI(title="Manga Store")


# ---------- схемы входных данных ----------
class AuthIn(BaseModel):
    email: str
    password: str


class ProductIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    author: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=3000)  # ограничение длины описания
    price: int = Field(ge=0)
    stock: int = Field(ge=0, default=0)
    cover_url: str = ""
    genre: str = "Другое"
    volume: int = Field(ge=1, default=1)

    @field_validator("genre")
    @classmethod
    def genre_must_be_in_list(cls, v):
        if v not in GENRES:  # проверка на сервере: подделать запрос и добавить свой жанр не выйдет
            raise ValueError("Выберите жанр из списка")
        return v


class QtyIn(BaseModel):
    quantity: int


# ---------- вспомогательное ----------
def card(p: Product) -> dict:
    """Лёгкая карточка для списков: БЕЗ длинного описания (так список грузится быстрее)."""
    return {"id": p.id, "title": p.title, "author": p.author, "price": p.price,
            "stock": p.stock, "cover_url": p.cover_url, "genre": p.genre, "volume": p.volume}


def rating_map(db: Session, ids: list[int]) -> dict:
    """Средняя оценка и число оценок для набора товаров одним запросом: {id: (средняя, количество)}."""
    if not ids:
        return {}
    rows = db.execute(select(Rating.product_id, func.avg(Rating.value), func.count())
                      .where(Rating.product_id.in_(ids)).group_by(Rating.product_id)).all()
    return {pid: (round(float(a), 1), n) for pid, a, n in rows}


def with_rating(items: list[dict], db: Session) -> list[dict]:
    """Добавляет к карточкам среднюю оценку и число страниц предпросмотра (двумя запросами на весь список)."""
    ids = [i["id"] for i in items]
    m = rating_map(db, ids)
    pc = dict(db.execute(select(PreviewPage.product_id, func.count())
                         .where(PreviewPage.product_id.in_(ids)).group_by(PreviewPage.product_id)).all()) if ids else {}
    for i in items:
        i["rating"], i["rating_count"] = m.get(i["id"], (0, 0))
        i["preview_count"] = pc.get(i["id"], 0)
    return items


def optional_user(cred=Depends(bearer), db: Session = Depends(get_db)) -> User | None:
    """Пользователь, если он вошёл, иначе None (читать комментарии можно и гостям)."""
    if not cred:
        return None
    try:
        return current_user(cred, db)
    except HTTPException:
        return None


def get_product(db: Session, pid: int) -> Product:
    p = db.get(Product, pid)
    if not p:
        raise HTTPException(404, "Товар не найден")
    return p


def cart_view(db: Session, user: User) -> dict:
    rows = db.execute(
        select(CartItem, Product).join(Product, Product.id == CartItem.product_id)
        .where(CartItem.user_id == user.id).order_by(Product.title)
    ).all()
    items = [{**card(p), "quantity": c.quantity, "subtotal": p.price * c.quantity} for c, p in rows]
    return {"items": items, "total": sum(i["subtotal"] for i in items),
            "count": sum(i["quantity"] for i in items)}


# ---------- регистрация и вход ----------
def auth_response(user: User) -> dict:
    return {"token": make_token(user), "email": user.email, "role": user.role}


@app.post("/api/auth/register")
def register(data: AuthIn, db: Session = Depends(get_db)):
    email = data.email.strip().lower()
    if "@" not in email or len(email) < 5:
        raise HTTPException(400, "Введите корректный email")
    if len(data.password) < 6:
        raise HTTPException(400, "Пароль минимум 6 символов")
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(400, "Этот email уже зарегистрирован")
    user = User(email=email, password_hash=hash_password(data.password), role="user")
    db.add(user)
    db.commit()
    return auth_response(user)


@app.post("/api/auth/login")
def login(data: AuthIn, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == data.email.strip().lower()))
    if not user or not check_password(data.password, user.password_hash):
        raise HTTPException(400, "Неверный email или пароль")
    return auth_response(user)


def avatar_url(pr: Profile | None) -> str | None:
    return f"/api/covers/{pr.avatar_id}" if pr and pr.avatar_id else None


@app.get("/api/me")
def me(db: Session = Depends(get_db), user: User = Depends(current_user)):
    pr = db.get(Profile, user.id)
    return {"email": user.email, "role": user.role, "nickname": pr.nickname if pr else None,
            "avatar_url": avatar_url(pr)}


# ---------- каталог: фильтрация + постраничный вывод ----------
SORTS = {
    "new": desc(Product.created_at),
    "price_asc": Product.price.asc(),
    "price_desc": Product.price.desc(),
    "title": Product.title.asc(),
}


@app.get("/api/genres")
def genres():
    return GENRES


@app.get("/api/products")
def products(
    q: str = "", genre: str = "", author: str = "",
    price_min: int | None = None, price_max: int | None = None,
    sort: str = "new",
    page: int = Query(1, ge=1), per_page: int = Query(12, ge=1, le=48),
    db: Session = Depends(get_db),
):
    query = select(Product)
    if q:
        like = f"%{q}%"
        query = query.where(or_(Product.title.ilike(like), Product.author.ilike(like)))
    if genre:
        query = query.where(Product.genre == genre)
    if author:
        query = query.where(Product.author.ilike(f"%{author}%"))
    if price_min is not None:
        query = query.where(Product.price >= price_min)
    if price_max is not None:
        query = query.where(Product.price <= price_max)

    total = db.scalar(select(func.count()).select_from(query.subquery()))
    # LIMIT/OFFSET: из базы берутся только товары нужной страницы, а не весь каталог
    rows = db.scalars(
        query.order_by(SORTS.get(sort, SORTS["new"]), Product.id)
        .offset((page - 1) * per_page).limit(per_page)
    ).all()
    pages = max(1, -(-total // per_page))
    return {"items": with_rating([card(p) for p in rows], db), "total": total, "page": page, "pages": pages}


@app.get("/api/products/{pid}")
def product_detail(pid: int, db: Session = Depends(get_db)):
    p = get_product(db, pid)
    avg, n = rating_map(db, [p.id]).get(p.id, (0, 0))
    return {**card(p), "description": p.description, "rating": avg, "rating_count": n}  # описание грузится только при открытии товара


# ---------- товары: только администратор ----------
@app.post("/api/products")
def create_product(data: ProductIn, db: Session = Depends(get_db), _: User = Depends(admin_only)):
    p = Product(**data.model_dump())
    db.add(p)
    db.commit()
    return card(p)


@app.put("/api/products/{pid}")
def update_product(pid: int, data: ProductIn, db: Session = Depends(get_db), _: User = Depends(admin_only)):
    p = get_product(db, pid)
    for k, v in data.model_dump().items():
        setattr(p, k, v)
    db.commit()
    return card(p)


@app.delete("/api/products/{pid}")
def delete_product(pid: int, db: Session = Depends(get_db), _: User = Depends(admin_only)):
    p = get_product(db, pid)
    db.execute(delete(Favorite).where(Favorite.product_id == pid))
    db.execute(delete(CartItem).where(CartItem.product_id == pid))
    db.execute(delete(Comment).where(Comment.product_id == pid))
    db.execute(delete(Rating).where(Rating.product_id == pid))
    page_covers = db.scalars(select(PreviewPage.cover_id).where(PreviewPage.product_id == pid)).all()
    db.execute(delete(PreviewPage).where(PreviewPage.product_id == pid))
    db.delete(p)
    db.flush()
    for cid in page_covers:
        drop_cover_if_unused(db, cid)  # страницы удалённой манги не копятся в базе
    db.commit()
    return {"ok": True}


# ---------- обложки: администратор и мангака ----------
def staff_only(user: User = Depends(current_user)) -> User:
    """Админ или мангака. Мангаке доступны ТОЛЬКО обложки: создание, правка и удаление товаров остаются за admin_only."""
    if user.role not in ("admin", "mangaka"):
        raise HTTPException(403, "Только для администратора или мангаки")
    return user


IMG_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend", "img")  # старые обложки, лежащие файлами
MAX_COVER_SIZE = 5 * 1024 * 1024  # 5 МБ
COVER_URL_RE = re.compile(r"^/api/covers/(\d+)$")


def detect_image_type(data: bytes) -> str | None:
    """Определяем формат по содержимому файла, а не по имени: переименованный .exe не пройдёт."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


@app.post("/api/upload-cover")
async def upload_cover(file: UploadFile = File(...), db: Session = Depends(get_db), _: User = Depends(staff_only)):
    data = await file.read(MAX_COVER_SIZE + 1)
    if len(data) > MAX_COVER_SIZE:
        raise HTTPException(400, "Файл слишком большой (максимум 5 МБ)")
    ctype = detect_image_type(data)
    if not ctype:
        raise HTTPException(400, "Нужна картинка JPG, PNG или WEBP")
    cover = Cover(content_type=ctype, data=data)  # картинка сохраняется прямо в базе
    db.add(cover)
    db.commit()
    return {"cover_url": f"/api/covers/{cover.id}"}


@app.get("/api/covers/{cid}")
def get_cover(cid: int, db: Session = Depends(get_db)):
    """Отдаёт картинку из базы. Номер никогда не получает другую картинку, поэтому браузер может хранить её долго."""
    c = db.get(Cover, cid)
    if not c:
        raise HTTPException(404, "Обложка не найдена")
    return Response(content=c.data, media_type=c.content_type,
                    headers={"Cache-Control": "public, max-age=31536000, immutable",
                             "X-Content-Type-Options": "nosniff"})


class CoverIn(BaseModel):
    cover_url: str = Field(min_length=1, max_length=500)

    @field_validator("cover_url")
    @classmethod
    def cover_must_be_ours(cls, v):
        if COVER_URL_RE.match(v):  # картинка из базы (существование проверяется в set_cover)
            return v
        # старые обложки-файлы из frontend/img: внешние ссылки и пути вида /img/../ не пройдут
        name = unquote(v[len("/img/"):]) if v.startswith("/img/") else ""
        if not name or "/" in name or "\\" in name or ".." in name or not os.path.isfile(os.path.join(IMG_DIR, name)):
            raise ValueError("Выберите картинку, загруженную через сайт")
        return v


@app.patch("/api/products/{pid}/cover")
def set_cover(pid: int, data: CoverIn, db: Session = Depends(get_db), _: User = Depends(staff_only)):
    """Меняет у товара только обложку, больше ничего."""
    p = get_product(db, pid)
    m = COVER_URL_RE.match(data.cover_url)
    if m and not db.get(Cover, int(m.group(1))):
        raise HTTPException(400, "Такой обложки нет, загрузите файл заново")
    p.cover_url = data.cover_url
    db.commit()
    return card(p)


# ---------- предпросмотр манги (пробные страницы) ----------
MAX_PREVIEW_PAGES = 20


def drop_cover_if_unused(db: Session, cid: int) -> None:
    """Удаляет картинку из базы, если на неё больше нет ссылок: ни обложки товара, ни страницы предпросмотра, ни аватара."""
    in_products = db.scalar(select(func.count()).select_from(Product).where(Product.cover_url == f"/api/covers/{cid}"))
    in_pages = db.scalar(select(func.count()).select_from(PreviewPage).where(PreviewPage.cover_id == cid))
    in_avatars = db.scalar(select(func.count()).select_from(Profile).where(Profile.avatar_id == cid))
    if not in_products and not in_pages and not in_avatars:
        db.execute(delete(Cover).where(Cover.id == cid))


def preview_list(db: Session, pid: int) -> list[dict]:
    rows = db.scalars(select(PreviewPage).where(PreviewPage.product_id == pid)
                      .order_by(PreviewPage.position, PreviewPage.id)).all()
    return [{"id": r.id, "url": f"/api/covers/{r.cover_id}"} for r in rows]


@app.get("/api/products/{pid}/preview")
def get_preview(pid: int, db: Session = Depends(get_db)):
    get_product(db, pid)
    return preview_list(db, pid)


@app.post("/api/products/{pid}/preview")
def add_preview(pid: int, data: CoverIn, db: Session = Depends(get_db), _: User = Depends(staff_only)):
    """Добавляет страницу в конец. Картинку сначала загружают через /api/upload-cover, сюда приходит её адрес."""
    get_product(db, pid)
    m = COVER_URL_RE.match(data.cover_url)
    if not m or not db.get(Cover, int(m.group(1))):
        raise HTTPException(400, "Сначала загрузите страницу через сайт")
    count = db.scalar(select(func.count()).select_from(PreviewPage).where(PreviewPage.product_id == pid))
    if count >= MAX_PREVIEW_PAGES:
        raise HTTPException(400, f"Максимум {MAX_PREVIEW_PAGES} страниц предпросмотра")
    last = db.scalar(select(func.max(PreviewPage.position)).where(PreviewPage.product_id == pid))
    db.add(PreviewPage(product_id=pid, cover_id=int(m.group(1)), position=(last or 0) + 1))
    db.commit()
    return preview_list(db, pid)


@app.delete("/api/products/{pid}/preview/{page_id}")
def delete_preview(pid: int, page_id: int, db: Session = Depends(get_db), _: User = Depends(staff_only)):
    page = db.get(PreviewPage, page_id)
    if not page or page.product_id != pid:
        raise HTTPException(404, "Страница не найдена")
    cid = page.cover_id
    db.delete(page)
    db.flush()
    drop_cover_if_unused(db, cid)
    db.commit()
    return preview_list(db, pid)


# ---------- оценки и комментарии ----------
class RatingIn(BaseModel):
    value: int = Field(ge=1, le=5)


class CommentIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    parent_id: int | None = None

    @field_validator("text")
    @classmethod
    def not_blank(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("Напишите комментарий")
        return v


def public_name(u: User, pr: Profile | None = None) -> str:
    """В комментариях показываем ник, а если его нет - только часть email до @, чтобы не раскрывать адреса читателей."""
    return pr.nickname if pr and pr.nickname else u.email.split("@")[0]


def review_info(db: Session, pid: int, me: User | None) -> dict:
    avg, n = rating_map(db, [pid]).get(pid, (0, 0))
    mine = db.get(Rating, (me.id, pid)) if me else None
    return {"avg": avg, "count": n, "my": mine.value if mine else None}


@app.get("/api/products/{pid}/reviews")
def reviews(pid: int, db: Session = Depends(get_db), me: User | None = Depends(optional_user)):
    get_product(db, pid)
    rows = db.execute(
        select(Comment, User, Profile).join(User, User.id == Comment.user_id)
        .outerjoin(Profile, Profile.user_id == User.id)
        .where(Comment.product_id == pid).order_by(Comment.created_at, Comment.id).limit(500)
    ).all()
    comments = [{"id": c.id, "parent_id": c.parent_id, "user": public_name(u, pr), "role": u.role, "text": c.text,
                 "avatar": avatar_url(pr), "created_at": c.created_at.isoformat() + "Z",
                 "can_delete": bool(me and (me.id == c.user_id or me.role == "admin"))} for c, u, pr in rows]
    return {**review_info(db, pid, me), "comments": comments}


@app.post("/api/products/{pid}/rating")
def set_rating(pid: int, data: RatingIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    get_product(db, pid)
    r = db.get(Rating, (user.id, pid))
    if r:
        r.value = data.value  # оценку можно изменить, но она всегда одна
    else:
        db.add(Rating(user_id=user.id, product_id=pid, value=data.value))
    db.commit()
    return review_info(db, pid, user)


@app.delete("/api/products/{pid}/rating")
def remove_rating(pid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    db.execute(delete(Rating).where(Rating.user_id == user.id, Rating.product_id == pid))
    db.commit()
    return review_info(db, pid, user)


@app.post("/api/products/{pid}/comments")
def add_comment(pid: int, data: CommentIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    get_product(db, pid)
    parent_id = None
    if data.parent_id:
        parent = db.get(Comment, data.parent_id)
        if not parent or parent.product_id != pid:
            raise HTTPException(400, "Комментарий, на который вы отвечаете, не найден")
        parent_id = parent.parent_id or parent.id  # ответ на ответ тоже попадает в ветку главного комментария
    # защита от спама: не больше 5 комментариев в минуту с одного аккаунта
    since = dt.datetime.utcnow() - dt.timedelta(seconds=60)
    recent = db.scalar(select(func.count()).select_from(Comment).where(Comment.user_id == user.id, Comment.created_at > since))
    if recent >= 5:
        raise HTTPException(429, "Слишком часто. Подождите минуту")
    db.add(Comment(product_id=pid, user_id=user.id, parent_id=parent_id, text=data.text))
    db.commit()
    return {"ok": True}


@app.delete("/api/comments/{cid}")
def delete_comment(cid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = db.get(Comment, cid)
    if not c:
        raise HTTPException(404, "Комментарий не найден")
    if c.user_id != user.id and user.role != "admin":
        raise HTTPException(403, "Удалить можно только свой комментарий")
    db.execute(delete(Comment).where(Comment.parent_id == cid))  # вместе с комментарием удаляются ответы на него
    db.delete(c)
    db.commit()
    return {"ok": True}


# ---------- избранное ----------
@app.get("/api/favorites")
def favorites(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.scalars(select(Product).join(Favorite, Favorite.product_id == Product.id)
                      .where(Favorite.user_id == user.id).order_by(Product.title)).all()
    return with_rating([card(p) for p in rows], db)


@app.post("/api/favorites/{pid}")
def add_favorite(pid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    get_product(db, pid)
    if not db.get(Favorite, (user.id, pid)):
        db.add(Favorite(user_id=user.id, product_id=pid))
        db.commit()
    return {"ok": True}


@app.delete("/api/favorites/{pid}")
def remove_favorite(pid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    db.execute(delete(Favorite).where(Favorite.user_id == user.id, Favorite.product_id == pid))
    db.commit()
    return {"ok": True}


# ---------- корзина (сумму считает сервер) ----------
@app.get("/api/cart")
def get_cart(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return cart_view(db, user)


@app.post("/api/cart/{pid}")
def cart_add(pid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = get_product(db, pid)
    item = db.get(CartItem, (user.id, pid))
    if item:
        if item.quantity >= p.stock:
            raise HTTPException(400, "Больше нет в наличии")
        item.quantity += 1
    else:
        if p.stock < 1:
            raise HTTPException(400, "Нет в наличии")
        db.add(CartItem(user_id=user.id, product_id=pid, quantity=1))
    db.commit()
    return cart_view(db, user)


@app.patch("/api/cart/{pid}")
def cart_set(pid: int, data: QtyIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = get_product(db, pid)
    item = db.get(CartItem, (user.id, pid))
    if item:
        if data.quantity <= 0:
            db.delete(item)
        else:
            item.quantity = min(data.quantity, p.stock)
        db.commit()
    return cart_view(db, user)


@app.delete("/api/cart/{pid}")
def cart_remove(pid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    db.execute(delete(CartItem).where(CartItem.user_id == user.id, CartItem.product_id == pid))
    db.commit()
    return cart_view(db, user)


# ---------- заказы ----------
def place_order(db: Session, user: User, items: list | None = None) -> Order:
    """Превращает корзину (или снимок корзины [[id, кол-во], ...]) в заказ: списывает остатки и очищает корзину.
    Всё в одной транзакции: либо выполнилось всё, либо ничего."""
    if items is None:
        items = [[c.product_id, c.quantity] for c in db.scalars(select(CartItem).where(CartItem.user_id == user.id))]
    if not items:
        raise HTTPException(400, "Корзина пуста")
    rows = []
    for pid, qty in items:
        p = db.get(Product, pid)
        if not p:
            raise HTTPException(400, "Один из товаров больше не продаётся")
        if qty > p.stock:
            raise HTTPException(400, f"«{p.title}»: в наличии только {p.stock} шт.")
        rows.append((p, qty))
    order = Order(user_id=user.id, total=sum(p.price * q for p, q in rows))
    db.add(order)
    db.flush()
    for p, q in rows:
        p.stock -= q
        db.add(OrderItem(order_id=order.id, product_id=p.id, title=p.title, price=p.price, quantity=q))
    db.execute(delete(CartItem).where(CartItem.user_id == user.id))
    return order


@app.post("/api/orders")
def make_order(db: Session = Depends(get_db), user: User = Depends(current_user)):
    if stripe_enabled():  # оплата подключена: заказ без оплаты оформить нельзя
        raise HTTPException(400, "Заказ оплачивается картой: нажмите «Оплатить картой» в корзине")
    order = place_order(db, user)
    db.commit()
    return {"id": order.id, "total": order.total}


# ---------- оплата картой через Stripe ----------
# Ключ берётся из переменной окружения (.env / Render). Секретный ключ живёт ТОЛЬКО на сервере.
STRIPE_KEY = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
try:
    KZT_PER_USD = float(os.getenv("KZT_PER_USD") or 500)  # Stripe-платёж идёт в долларах, переводим тенге по курсу
except ValueError:
    KZT_PER_USD = 500.0
if KZT_PER_USD <= 0:
    KZT_PER_USD = 500.0
STRIPE_MIN_CENTS = 50  # минимальный платёж Stripe - 0,50 $


class CheckoutIn(BaseModel):
    origin: str = ""  # адрес сайта в браузере: на него Stripe вернёт покупателя после оплаты


def stripe_enabled() -> bool:
    return STRIPE_KEY.startswith(("sk_test_", "sk_live_", "rk_test_", "rk_live_"))


def stripe_request(method: str, path: str, form: dict | None = None) -> dict:
    """Один запрос к Stripe API (данные отправляются как форма, авторизация - секретным ключом)."""
    req = urllib.request.Request(
        "https://api.stripe.com" + path, method=method,
        headers={"Authorization": "Bearer " + STRIPE_KEY},
        data=urllib.parse.urlencode(form).encode() if form is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        print("Stripe error:", e.code, e.read()[:500])
        raise HTTPException(502, "Stripe отклонил запрос. Проверьте ключ и попробуйте ещё раз")
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise HTTPException(502, "Не удалось связаться со Stripe. Попробуйте позже")


def to_cents(total_kzt: int) -> int:
    return math.ceil(total_kzt / KZT_PER_USD * 100)  # переводим в центы, округляя вверх


@app.get("/api/stripe/config")
def stripe_config():
    return {"enabled": stripe_enabled(), "rate": KZT_PER_USD, "test": STRIPE_KEY.startswith(("sk_test_", "rk_test_"))}


@app.post("/api/stripe/checkout")
def stripe_checkout(data: CheckoutIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if not stripe_enabled():
        raise HTTPException(503, "Оплата картой не настроена")
    if not re.fullmatch(r"https?://[A-Za-z0-9.\-]+(:\d+)?", data.origin):
        raise HTTPException(400, "Неверный адрес сайта")
    cart = db.execute(select(CartItem, Product).join(Product, Product.id == CartItem.product_id)
                      .where(CartItem.user_id == user.id)).all()
    if not cart:
        raise HTTPException(400, "Корзина пуста")
    for c, p in cart:
        if c.quantity > p.stock:
            raise HTTPException(400, f"«{p.title}»: в наличии только {p.stock} шт.")
    total = sum(p.price * c.quantity for c, p in cart)  # сумму считаем на сервере, а не берём из браузера
    cents = to_cents(total)
    if cents < STRIPE_MIN_CENTS:
        raise HTTPException(400, f"Stripe принимает платежи от 0,50 $ (≈ {math.ceil(STRIPE_MIN_CENTS / 100 * KZT_PER_USD)} ₸)")
    res = stripe_request("POST", "/v1/checkout/sessions", {
        "mode": "payment",
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": "usd",
        "line_items[0][price_data][unit_amount]": str(cents),
        "line_items[0][price_data][product_data][name]": f"Manga store: заказ на {total} ₸",
        "success_url": data.origin + "/?stripe_session={CHECKOUT_SESSION_ID}",
        "cancel_url": data.origin + "/?stripe_cancel=1",
    })
    db.add(Payment(session_id=res["id"], user_id=user.id, total=total, cents=cents,
                   items=json.dumps([[p.id, c.quantity] for c, p in cart])))
    db.commit()
    return {"url": res["url"]}


@app.post("/api/stripe/confirm/{session_id}")
def stripe_confirm(session_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    pay = db.get(Payment, session_id)
    if not pay or pay.user_id != user.id:
        raise HTTPException(404, "Платёж не найден")
    if pay.order_id:  # повторный вызов (обновили страницу): отдаём уже созданный заказ
        return {"id": pay.order_id, "total": pay.total}
    res = stripe_request("GET", "/v1/checkout/sessions/" + urllib.parse.quote(session_id, safe=""))
    if res.get("payment_status") != "paid" or res.get("amount_total") != pay.cents or res.get("currency") != "usd":
        raise HTTPException(400, "Оплата не подтверждена. Если деньги списались, подождите минуту и обновите страницу")
    order = place_order(db, user, json.loads(pay.items))
    pay.order_id = order.id
    db.commit()
    return {"id": order.id, "total": order.total}


@app.get("/api/orders")
def my_orders(db: Session = Depends(get_db), user: User = Depends(current_user)):
    orders = db.scalars(select(Order).where(Order.user_id == user.id).order_by(desc(Order.id)).limit(50)).all()
    paid = set(db.scalars(select(Payment.order_id).where(Payment.user_id == user.id, Payment.order_id.is_not(None))))
    return [{"id": o.id, "total": o.total, "paid": o.id in paid, "date": o.created_at.strftime("%d.%m.%Y %H:%M"),
             "items": [{"title": i.title, "price": i.price, "quantity": i.quantity} for i in o.items]}
            for o in orders]


# ---------- профиль пользователя ----------
MAX_AVATAR_SIZE = 2 * 1024 * 1024  # 2 МБ
NICK_RE = re.compile(r"^[A-Za-zА-Яа-яЁё0-9_. \-]{2,24}$")


class ProfileIn(BaseModel):
    nickname: str = Field(default="", max_length=24)
    bio: str = Field(default="", max_length=300)

    @field_validator("nickname")
    @classmethod
    def nick_ok(cls, v):
        v = " ".join(v.split())  # лишние пробелы убираем
        if v and not NICK_RE.match(v):
            raise ValueError("2–24 символа: буквы, цифры, пробел, _ . -")
        return v

    @field_validator("bio")
    @classmethod
    def bio_strip(cls, v):
        return v.strip()


class PasswordIn(BaseModel):
    current_password: str = Field(max_length=200)
    new_password: str = Field(min_length=6, max_length=72)


def get_or_create_profile(db: Session, user: User) -> Profile:
    pr = db.get(Profile, user.id)
    if not pr:
        pr = Profile(user_id=user.id, bio="")
        db.add(pr)
        db.flush()
    return pr


def profile_view(db: Session, user: User) -> dict:
    pr = db.get(Profile, user.id)
    n_orders, spent = db.execute(select(func.count(), func.coalesce(func.sum(Order.total), 0))
                                 .where(Order.user_id == user.id)).one()
    count = lambda col, tbl: db.scalar(select(func.count()).select_from(tbl).where(col == user.id))
    # любимый жанр: чего куплено больше всего, а если покупок нет - что чаще в избранном
    row = db.execute(select(Product.genre).join(OrderItem, OrderItem.product_id == Product.id)
                     .join(Order, Order.id == OrderItem.order_id).where(Order.user_id == user.id)
                     .group_by(Product.genre).order_by(desc(func.sum(OrderItem.quantity))).limit(1)).first()
    if not row:
        row = db.execute(select(Product.genre).join(Favorite, Favorite.product_id == Product.id)
                         .where(Favorite.user_id == user.id).group_by(Product.genre)
                         .order_by(desc(func.count())).limit(1)).first()
    return {"email": user.email, "role": user.role,
            "nickname": pr.nickname if pr else None, "bio": pr.bio if pr else "", "avatar_url": avatar_url(pr),
            "favorite_genre": row[0] if row else None,
            "stats": {"orders": n_orders, "spent": int(spent), "comments": count(Comment.user_id, Comment),
                      "ratings": count(Rating.user_id, Rating), "favorites": count(Favorite.user_id, Favorite)}}


@app.get("/api/profile")
def get_profile(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return profile_view(db, user)


@app.patch("/api/profile")
def update_profile(data: ProfileIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    pr = get_or_create_profile(db, user)
    key = data.nickname.lower() or None
    if key and db.scalar(select(Profile).where(Profile.nick_key == key, Profile.user_id != user.id)):
        raise HTTPException(400, "Этот ник уже занят")
    pr.nickname = data.nickname or None
    pr.nick_key = key
    pr.bio = data.bio
    db.commit()
    return profile_view(db, user)


@app.post("/api/profile/avatar")
async def upload_avatar(file: UploadFile = File(...), db: Session = Depends(get_db), user: User = Depends(current_user)):
    data = await file.read(MAX_AVATAR_SIZE + 1)
    if len(data) > MAX_AVATAR_SIZE:
        raise HTTPException(400, "Файл слишком большой (максимум 2 МБ)")
    ctype = detect_image_type(data)
    if not ctype:
        raise HTTPException(400, "Нужна картинка JPG, PNG или WEBP")
    pr = get_or_create_profile(db, user)
    old = pr.avatar_id
    cover = Cover(content_type=ctype, data=data)  # аватар лежит в той же таблице картинок, что и обложки
    db.add(cover)
    db.flush()
    pr.avatar_id = cover.id
    db.flush()
    if old:
        drop_cover_if_unused(db, old)  # у пользователя всегда одна картинка: старая удаляется
    db.commit()
    return profile_view(db, user)


@app.delete("/api/profile/avatar")
def delete_avatar(db: Session = Depends(get_db), user: User = Depends(current_user)):
    pr = db.get(Profile, user.id)
    if pr and pr.avatar_id:
        old = pr.avatar_id
        pr.avatar_id = None
        db.flush()
        drop_cover_if_unused(db, old)
        db.commit()
    return profile_view(db, user)


@app.post("/api/profile/password")
def change_password(data: PasswordIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if not check_password(data.current_password, user.password_hash):
        raise HTTPException(400, "Текущий пароль неверный")
    user.password_hash = hash_password(data.new_password)
    db.commit()
    return {"ok": True}


# ---------- сайт (фронтенд) отдаётся этим же сервером ----------
FRONT = os.path.join(os.path.dirname(__file__), "..", "frontend")


class NoCacheStaticFiles(StaticFiles):
    """Браузер каждый раз сверяет файлы сайта с сервером, поэтому после правки app.js или style.css
    не нужно бороться с кэшем."""
    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/", NoCacheStaticFiles(directory=FRONT, html=True), name="frontend")