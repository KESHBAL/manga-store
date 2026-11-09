import os
import uuid
from urllib.parse import unquote
from fastapi import FastAPI, Depends, HTTPException, Query, UploadFile, File
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, func, delete, desc, or_
from sqlalchemy.orm import Session

from database import engine, get_db
from models import Base, User, Product, Favorite, CartItem, Order, OrderItem
from genres import GENRES
from auth import hash_password, check_password, make_token, current_user, admin_only

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


@app.get("/api/me")
def me(user: User = Depends(current_user)):
    return {"email": user.email, "role": user.role}


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
    return {"items": [card(p) for p in rows], "total": total, "page": page, "pages": pages}


@app.get("/api/products/{pid}")
def product_detail(pid: int, db: Session = Depends(get_db)):
    p = get_product(db, pid)
    return {**card(p), "description": p.description}  # полное описание грузится только при открытии товара


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
    db.delete(p)
    db.commit()
    return {"ok": True}


# ---------- обложки: администратор и мангака ----------
def staff_only(user: User = Depends(current_user)) -> User:
    """Админ или мангака. Мангаке доступны ТОЛЬКО обложки: создание, правка и удаление товаров остаются за admin_only."""
    if user.role not in ("admin", "mangaka"):
        raise HTTPException(403, "Только для администратора или мангаки")
    return user


IMG_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend", "img")
os.makedirs(IMG_DIR, exist_ok=True)
MAX_COVER_SIZE = 5 * 1024 * 1024  # 5 МБ


def detect_image_ext(data: bytes) -> str | None:
    """Определяем формат по содержимому файла, а не по имени: переименованный .exe не пройдёт."""
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


@app.post("/api/upload-cover")
async def upload_cover(file: UploadFile = File(...), _: User = Depends(staff_only)):
    data = await file.read(MAX_COVER_SIZE + 1)
    if len(data) > MAX_COVER_SIZE:
        raise HTTPException(400, "Файл слишком большой (максимум 5 МБ)")
    ext = detect_image_ext(data)
    if not ext:
        raise HTTPException(400, "Нужна картинка JPG, PNG или WEBP")
    name = f"{uuid.uuid4().hex}{ext}"  # случайное имя: файлы не перезапишут друг друга
    with open(os.path.join(IMG_DIR, name), "wb") as f:
        f.write(data)
    return {"cover_url": f"/img/{name}"}


class CoverIn(BaseModel):
    cover_url: str = Field(min_length=1, max_length=500)

    @field_validator("cover_url")
    @classmethod
    def cover_must_be_our_image(cls, v):
        # принимаем только картинки, которые уже лежат в frontend/img: внешние ссылки и пути вида /img/../ не пройдут
        name = unquote(v[len("/img/"):]) if v.startswith("/img/") else ""
        if not name or "/" in name or "\\" in name or ".." in name or not os.path.isfile(os.path.join(IMG_DIR, name)):
            raise ValueError("Выберите картинку, загруженную через сайт")
        return v


@app.patch("/api/products/{pid}/cover")
def set_cover(pid: int, data: CoverIn, db: Session = Depends(get_db), _: User = Depends(staff_only)):
    """Меняет у товара только обложку, больше ничего."""
    p = get_product(db, pid)
    p.cover_url = data.cover_url
    db.commit()
    return card(p)


# ---------- избранное ----------
@app.get("/api/favorites")
def favorites(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.scalars(select(Product).join(Favorite, Favorite.product_id == Product.id)
                      .where(Favorite.user_id == user.id).order_by(Product.title)).all()
    return [card(p) for p in rows]


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
@app.post("/api/orders")
def make_order(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.execute(select(CartItem, Product).join(Product, Product.id == CartItem.product_id)
                      .where(CartItem.user_id == user.id)).all()
    if not rows:
        raise HTTPException(400, "Корзина пуста")
    for c, p in rows:
        if c.quantity > p.stock:
            raise HTTPException(400, f"«{p.title}»: в наличии только {p.stock} шт.")
    order = Order(user_id=user.id, total=sum(p.price * c.quantity for c, p in rows))
    db.add(order)
    db.flush()
    for c, p in rows:
        p.stock -= c.quantity
        db.add(OrderItem(order_id=order.id, product_id=p.id, title=p.title, price=p.price, quantity=c.quantity))
    db.execute(delete(CartItem).where(CartItem.user_id == user.id))
    db.commit()  # всё выше - одна транзакция: либо всё выполнилось, либо ничего
    return {"id": order.id, "total": order.total}


@app.get("/api/orders")
def my_orders(db: Session = Depends(get_db), user: User = Depends(current_user)):
    orders = db.scalars(select(Order).where(Order.user_id == user.id).order_by(desc(Order.id)).limit(50)).all()
    return [{"id": o.id, "total": o.total, "date": o.created_at.strftime("%d.%m.%Y %H:%M"),
             "items": [{"title": i.title, "price": i.price, "quantity": i.quantity} for i in o.items]}
            for o in orders]


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