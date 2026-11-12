const API = "/api";
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = n => Number(n).toLocaleString("ru-RU") + " ₸";

let token = localStorage.getItem("token");
let me = null;
let favIds = new Set();
let page = 1;
let view = "catalog";

// ---------- запросы к бэкенду ----------
const FIELD_NAMES = { title: "Название", author: "Автор", genre: "Жанр", price: "Цена", stock: "Остаток",
  volume: "Том", cover_url: "Обложка", description: "Описание" };

// превращает ответ сервера об ошибке в понятную фразу
function errText(d) {
  if (typeof d === "string") return d;
  if (Array.isArray(d) && d[0]) {
    const loc = d[0].loc || [];
    const name = FIELD_NAMES[loc[loc.length - 1]];
    let m = String(d[0].msg || "").replace("Value error, ", "");
    if (/at most/.test(m)) m = "слишком длинное значение";
    else if (/at least/.test(m)) m = "слишком короткое или пустое значение";
    else if (/greater than or equal/.test(m)) m = "значение слишком маленькое";
    else if (/valid integer|valid number/.test(m)) m = "нужно целое число";
    return (name ? name + ": " : "") + m;
  }
  return "Проверьте введённые данные";
}

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json" };
  if (token) headers.Authorization = "Bearer " + token;
  const res = await fetch(API + path, { ...opts, headers, body: opts.body ? JSON.stringify(opts.body) : undefined });
  if (!res.ok) {
    const e = await res.json().catch(() => ({}));
    throw new Error(errText(e.detail));
  }
  return res.json();
}

// загрузка файла обложки (отдельный запрос: тут не JSON, а файл, поэтому Content-Type ставит сам браузер)
async function uploadCover(file) {
  const fd = new FormData();
  fd.append("file", file);
  const res = await fetch(API + "/upload-cover", {
    method: "POST", headers: token ? { Authorization: "Bearer " + token } : {}, body: fd,
  });
  if (!res.ok) {
    const e = await res.json().catch(() => ({}));
    throw new Error(errText(e.detail));
  }
  return res.json();
}

let toastTimer;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 2500);
}

// ---------- шапка и навигация ----------
function renderNav() {
  document.querySelectorAll(".need-auth").forEach(b => b.hidden = !me);
  document.querySelectorAll(".admin-only").forEach(b => b.hidden = !(me && me.role === "admin"));
  $("#loginBtn").hidden = !!me;
  $("#logoutBtn").hidden = !me;
  document.querySelectorAll("nav [data-view]").forEach(b => b.classList.toggle("active", b.dataset.view === view));

  // плашка с ролью: показывает, от чьего лица ты сейчас на сайте
  let badge = $("#roleBadge");
  if (!badge) {
    badge = document.createElement("span");
    badge.id = "roleBadge";
    $("nav").insertBefore(badge, $("#loginBtn"));
  }
  const role = me ? me.role : "guest";
  badge.className = "role " + role;
  badge.textContent = !me ? "Гость" : (me.role === "admin" ? "★ Админ · " : me.role === "mangaka" ? "✎ Мангака · " : "Пользователь · ") + me.email;
}

function showView(name) {
  view = name;
  ["catalog", "favs", "cart", "orders"].forEach(v => $("#v-" + v).hidden = v !== name);
  renderNav();
  if (name === "catalog") loadCatalog();
  if (name === "favs") loadFavs();
  if (name === "cart") loadCart();
  if (name === "orders") loadOrders();
}

async function refreshCartCount() {
  if (!me) return;
  try { $("#cartCount").textContent = (await api("/cart")).count; } catch { }
}

// ---------- карточка товара ----------
function coverHtml(p) {
  return p.cover_url
    ? `<img src="${esc(p.cover_url)}" alt="${esc(p.title)}" loading="lazy">`
    : `<div class="ph">${esc(p.title)}</div>`;
}

function cardHtml(p) {
  const admin = me && me.role === "admin";
  const mangaka = me && me.role === "mangaka";
  return `<article class="card">
    <div class="cover" data-act="open" data-id="${p.id}">${coverHtml(p)}</div>
    <div class="info">
      <h3>${esc(p.title)}</h3>
      <div class="author">${esc(p.author)} · том ${p.volume}<br>${esc(p.genre)}${p.rating_count ? `<br><span class="stars">★ ${p.rating.toFixed(1)}</span> <span class="muted2">(${p.rating_count})</span>` : ""}${p.preview_count ? `<br><span class="muted2">📖 есть предпросмотр</span>` : ""}</div>
      <div class="row"><span class="price">${money(p.price)}</span>
        <button class="fav ${favIds.has(p.id) ? "on" : ""}" data-act="fav" data-id="${p.id}" title="В избранное">♥</button></div>
      <button class="buy" data-act="add" data-id="${p.id}" ${p.stock < 1 ? "disabled" : ""}>${p.stock < 1 ? "Нет в наличии" : "В корзину"}</button>
      ${admin ? `<div class="adm"><button data-act="edit" data-id="${p.id}">Изменить</button><button data-act="del" data-id="${p.id}">Удалить</button></div>`
        : mangaka ? `<div class="adm"><button data-act="cover" data-id="${p.id}">Сменить обложку</button></div>` : ""}
    </div></article>`;
}

// ---------- каталог: фильтры и страницы ----------
async function loadCatalog() {
  const p = new URLSearchParams({ page, per_page: 12, sort: $("#sort").value });
  const set = (k, v) => { if (v !== "" && v != null) p.set(k, v); };
  set("q", $("#q").value.trim());
  set("genre", $("#genre").value);
  set("author", $("#author").value.trim());
  set("price_min", $("#pmin").value);
  set("price_max", $("#pmax").value);
  try {
    const d = await api("/products?" + p);
    page = d.page;
    $("#grid").innerHTML = d.items.length ? d.items.map(cardHtml).join("") : `<p class="empty">Ничего не найдено</p>`;
    $("#total").textContent = `Найдено товаров: ${d.total}`;
    renderPager(d.page, d.pages);
  } catch (e) { toast(e.message); }
}

function renderPager(cur, pages) {
  if (pages <= 1) { $("#pager").innerHTML = ""; return; }
  const nums = [];
  for (let i = 1; i <= pages; i++) if (i === 1 || i === pages || Math.abs(i - cur) <= 1) nums.push(i);
  let html = `<button data-page="${cur - 1}" ${cur === 1 ? "disabled" : ""}>‹</button>`;
  let prev = 0;
  for (const n of nums) {
    if (n - prev > 1) html += `<button disabled>…</button>`;
    html += `<button data-page="${n}" class="${n === cur ? "cur" : ""}">${n}</button>`;
    prev = n;
  }
  html += `<button data-page="${cur + 1}" ${cur === pages ? "disabled" : ""}>›</button>`;
  $("#pager").innerHTML = html;
}

async function loadGenres() {
  const list = await api("/genres");
  const cur = $("#genre").value;
  $("#genre").innerHTML = `<option value="">Все жанры</option>` + list.map(g => `<option>${esc(g)}</option>`).join("");
  $("#genre").value = cur;
}

function filtersChanged() { page = 1; loadCatalog(); }

let debounce;
$("#q").addEventListener("input", () => { clearTimeout(debounce); debounce = setTimeout(filtersChanged, 350); });
$("#author").addEventListener("input", () => { clearTimeout(debounce); debounce = setTimeout(filtersChanged, 350); });
["#genre", "#sort", "#pmin", "#pmax"].forEach(s => $(s).addEventListener("change", filtersChanged));
$("#reset").addEventListener("click", () => {
  ["#q", "#author", "#pmin", "#pmax", "#genre"].forEach(s => $(s).value = "");
  $("#sort").value = "new";
  filtersChanged();
});

// ---------- избранное ----------
async function loadFavIds() { favIds = new Set((await api("/favorites")).map(p => p.id)); }

async function loadFavs() {
  const list = await api("/favorites");
  favIds = new Set(list.map(p => p.id));
  $("#favGrid").innerHTML = list.length ? list.map(cardHtml).join("") : `<p class="empty">Пока пусто</p>`;
}

// ---------- корзина ----------
async function loadCart() {
  const c = await api("/cart");
  $("#cartCount").textContent = c.count;
  if (!c.items.length) { $("#cartBox").innerHTML = `<p class="empty">Корзина пуста</p>`; return; }
  $("#cartBox").innerHTML = c.items.map(i => `
    <div class="cartRow">
      <div class="cover">${coverHtml(i)}</div>
      <div><b>${esc(i.title)}</b><div class="author">${esc(i.author)} · том ${i.volume}<br>${money(i.price)} за шт.</div></div>
      <div class="qty"><button data-act="dec" data-id="${i.id}" data-q="${i.quantity}">−</button><b>${i.quantity}</b>
        <button data-act="inc" data-id="${i.id}" data-q="${i.quantity}">+</button></div>
      <b class="price">${money(i.subtotal)}</b>
      <button class="qty" data-act="rm" data-id="${i.id}" style="border:none;background:none;font-size:20px">✕</button>
    </div>`).join("") +
    `<div class="totalBox">Итого: ${money(c.total)} <button class="btn" data-act="order">Оформить заказ</button></div>`;
}

async function loadOrders() {
  const list = await api("/orders");
  $("#ordersBox").innerHTML = list.length ? list.map(o => `
    <div class="order"><h4>Заказ №${o.id} · ${o.date} · ${money(o.total)}</h4>
    ${o.items.map(i => `<div>${esc(i.title)} — ${i.quantity} × ${money(i.price)}</div>`).join("")}</div>`).join("")
    : `<p class="empty">Заказов пока нет</p>`;
}

// ---------- модальные окна ----------
function openModal(html, wide = false) {
  $("#modalBox").className = "box" + (wide ? " wide" : "");
  $("#modalBox").innerHTML = html;
  $("#modal").hidden = false;
}
function closeModal() {
  $("#modal").hidden = true;
  if (ratingsChanged) {  // оценку поменяли в окне товара: обновляем звёзды на карточках
    ratingsChanged = false;
    if (view === "catalog") loadCatalog(); else if (view === "favs") loadFavs();
  }
}
$("#modal").addEventListener("click", e => { if (e.target.id === "modal") closeModal(); });

function openAuth(mode = "login") {
  const reg = mode === "register";
  openModal(`<button class="close" data-act="close">×</button>
    <h3>${reg ? "Регистрация" : "Вход"}</h3>
    <div class="tabs"><button class="${reg ? "" : "on"}" data-act="tab-login">Вход</button><button class="${reg ? "on" : ""}" data-act="tab-reg">Регистрация</button></div>
    <label>Email<input id="aEmail" type="email"></label>
    <label>Пароль<input id="aPass" type="password" placeholder="минимум 6 символов"></label>
    <div class="err" id="aErr"></div>
    <button class="btn" data-act="${reg ? "do-reg" : "do-login"}">${reg ? "Создать аккаунт" : "Войти"}</button>`);
}

async function doAuth(kind) {
  try {
    const d = await api("/auth/" + kind, { method: "POST", body: { email: $("#aEmail").value, password: $("#aPass").value } });
    token = d.token; localStorage.setItem("token", token);
    me = { email: d.email, role: d.role };
    await loadFavIds();
    closeModal(); renderNav(); refreshCartCount(); showView("catalog");
    toast("Добро пожаловать, " + d.email);
  } catch (e) { $("#aErr").textContent = e.message; }
}

function logout() {
  token = null; me = null; favIds = new Set();
  localStorage.removeItem("token");
  $("#cartCount").textContent = 0;
  showView("catalog");
}

async function openProduct(id) {
  const p = await api("/products/" + id);  // полное описание грузится только здесь
  openModal(`<button class="close" data-act="close">×</button>
    <div class="detail"><div class="cover">${coverHtml(p)}</div>
    <div><h3>${esc(p.title)}</h3><p class="author">${esc(p.author)} · том ${p.volume} · ${esc(p.genre)}</p>
    <p class="price" style="margin:10px 0">${money(p.price)}</p>
    <p>${esc(p.description) || "Описания пока нет."}</p>
    <p class="muted" style="margin-top:10px">В наличии: ${p.stock} шт.</p></div></div>
    <button class="btn" data-act="add" data-id="${p.id}" ${p.stock < 1 ? "disabled" : ""} style="margin-top:14px">В корзину</button>
    <div id="previewBox" class="preview"></div>
    <div id="reviewsBox" class="reviews">Загрузка…</div>`, true);
  curPid = id; replyTo = null; loadReviews(); loadPreview();
}

async function openProductForm(id) {
  const p = id ? await api("/products/" + id) : { title: "", author: "", genre: "", price: 0, stock: 10, volume: 1, cover_url: "", description: "" };
  const genres = await api("/genres");
  const placeholder = genres.includes(p.genre) ? "" : `<option value="" disabled selected>Выберите жанр${p.genre ? " (сейчас: " + esc(p.genre) + ")" : ""}</option>`;
  const genreSelect = `<label>Жанр<select id="f_genre">${placeholder}${genres.map(g => `<option ${g === p.genre ? "selected" : ""}>${esc(g)}</option>`).join("")}</select></label>`;
  const f = (k, label, type = "text") => `<label>${label}<input id="f_${k}" type="${type}" value="${esc(p[k])}"></label>`;
  openModal(`<button class="close" data-act="close">×</button><h3>${id ? "Изменить товар" : "Новый товар"}</h3>
    ${f("title", "Название")}${f("author", "Автор")}${genreSelect}
    <div class="two">${f("price", "Цена, ₸", "number")}${f("stock", "Остаток, шт.", "number")}${f("volume", "Том", "number")}</div>
    <div class="coverField">
      <span class="lbl">Обложка</span>
      <div class="coverPick">
        <div class="coverPrev" id="coverPrev">${p.cover_url ? `<img src="${esc(p.cover_url)}" alt="">` : "нет"}</div>
        <div>
          <input id="f_cover_file" type="file" accept="image/jpeg,image/png,image/webp" hidden>
          <button type="button" class="ghost" data-act="pick-cover">Выбрать файл</button>
          <small id="coverHint">JPG, PNG или WEBP, до 5 МБ</small>
        </div>
      </div>
      <input id="f_cover_url" type="hidden" value="${esc(p.cover_url)}">
    </div>
    <label>Описание<textarea id="f_description" rows="4" maxlength="3000">${esc(p.description)}</textarea></label>
    <div class="err" id="aErr"></div>
    <button class="btn" data-act="save" data-id="${id || ""}">Сохранить</button>`);
}

async function saveProduct(id) {
  const v = k => $("#f_" + k).value;
  const body = { title: v("title"), author: v("author"), genre: v("genre"), price: +v("price"), stock: +v("stock"),
    volume: +v("volume") || 1, cover_url: v("cover_url"), description: v("description") };
  try {
    await api(id ? "/products/" + id : "/products", { method: id ? "PUT" : "POST", body });
    closeModal(); toast("Сохранено"); await loadGenres(); loadCatalog();
  } catch (e) { $("#aErr").textContent = e.message; }
}

// ---------- оценки и комментарии ----------
let curPid = null, replyTo = null, ratingsChanged = false;
const stars = n => "★".repeat(n) + "☆".repeat(5 - n);
const fmtDate = s => new Date(s).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" });
const roleTag = r => r === "admin" ? ` <span class="tag">Админ</span>` : r === "mangaka" ? ` <span class="tag">Мангака</span>` : "";

function commentHtml(c, isReply) {
  return `<div class="cm ${isReply ? "reply" : ""}">
    <div class="cmHead"><b>${esc(c.user)}</b>${roleTag(c.role)} <span class="muted2">${esc(fmtDate(c.created_at))}</span></div>
    <p class="cmText">${esc(c.text)}</p>
    <div class="cmAct">${me ? `<button data-act="reply" data-id="${c.id}" data-user="${esc(c.user)}">Ответить</button>` : ""}${c.can_delete ? `<button data-act="del-comment" data-id="${c.id}">Удалить</button>` : ""}</div></div>`;
}

async function loadReviews() {
  const box = $("#reviewsBox");
  if (!box) return;
  let d;
  try { d = await api("/products/" + curPid + "/reviews"); } catch (e) { box.textContent = e.message; return; }
  const keep = $("#cmText") ? $("#cmText").value : "";  // не теряем недописанный комментарий при обновлении блока
  const roots = d.comments.filter(c => !c.parent_id);
  const list = roots.map(r => commentHtml(r, false) + d.comments.filter(c => c.parent_id === r.id).map(c => commentHtml(c, true)).join("")).join("");
  const mine = me ? `<div class="rate">Ваша оценка: ${[1, 2, 3, 4, 5].map(i => `<button class="star ${d.my >= i ? "on" : ""}" data-act="rate" data-v="${i}" title="${i}">★</button>`).join("")}${d.my ? ` <button class="link" data-act="unrate">убрать</button>` : ""}</div>`
    : `<p class="muted2">Войдите, чтобы оценить мангу и написать комментарий</p>`;
  const form = me ? `<div class="cmForm">
      ${replyTo ? `<div class="muted2">Ответ для ${esc(replyTo.user)} · <button class="link" data-act="cancel-reply">отмена</button></div>` : ""}
      <textarea id="cmText" rows="3" maxlength="1000" placeholder="Ваш комментарий"></textarea>
      <button class="btn" data-act="send-comment">Отправить</button></div>`
    : `<button class="ghost" data-act="auth">Войти</button>`;
  box.innerHTML = `<div class="avg"><span class="big">${d.count ? d.avg.toFixed(1) : "—"}</span>
      <span class="starsRow">${d.count ? stars(Math.round(d.avg)) : ""}</span>
      <span class="muted2">${d.count ? "оценок: " + d.count : "Пока нет оценок"}</span></div>
    ${mine}
    <h4>Комментарии (${d.comments.length})</h4>
    ${list || `<p class="muted2">Комментариев пока нет. Будьте первым!</p>`}
    ${form}`;
  if ($("#cmText")) $("#cmText").value = keep;
}

async function sendComment() {
  const t = $("#cmText").value.trim();
  if (!t) { toast("Напишите комментарий"); return; }
  await api(`/products/${curPid}/comments`, { method: "POST", body: { text: t, parent_id: replyTo ? replyTo.id : null } });
  replyTo = null;
  $("#cmText").value = "";
  await loadReviews();
  toast("Комментарий добавлен");
}

// ---------- предпросмотр манги (пробные страницы) ----------
let previewPages = [], pvIndex = 0;
const isStaff = () => me && (me.role === "admin" || me.role === "mangaka");

async function loadPreview() {
  const box = $("#previewBox");
  if (!box) return;
  try { previewPages = await api("/products/" + curPid + "/preview"); } catch { return; }
  const read = previewPages.length
    ? `<button class="ghost" data-act="open-preview" data-i="0">📖 Читать предпросмотр (${previewPages.length} стр.)</button>` : "";
  const manage = isStaff() ? `<div class="pvManage">
      <div class="muted2">Страницы предпросмотра: JPG, PNG или WEBP до 5 МБ каждая, до 20 страниц. Можно выбрать сразу несколько файлов, они добавятся по порядку имён.</div>
      <div class="pvThumbs">${previewPages.map((p, i) => `<div class="pvThumb"><img src="${esc(p.url)}" alt=""><span>${i + 1}</span>
        <button data-act="del-preview" data-id="${p.id}" title="Удалить страницу">✕</button></div>`).join("")}</div>
      <input id="f_preview_files" type="file" accept="image/jpeg,image/png,image/webp" multiple hidden>
      <button class="ghost" data-act="pick-preview">+ Добавить страницы</button>
      <div class="err" id="pvErr"></div></div>` : "";
  box.innerHTML = read + manage;
}

function viewerHtml(i) {
  const n = previewPages.length;
  return `<button class="close" data-act="close">×</button><h3>Предпросмотр</h3>
    <div class="viewer"><img id="pvImg" src="${esc(previewPages[i].url)}" alt="Страница ${i + 1}"></div>
    <div class="pvNav"><button class="ghost" data-act="pv-prev" ${i === 0 ? "disabled" : ""}>‹ Назад</button>
      <b>${i + 1} / ${n}</b>
      <button class="ghost" data-act="pv-next" ${i === n - 1 ? "disabled" : ""}>Вперёд ›</button></div>
    <button class="btn" data-act="pv-back">К описанию манги</button>`;
}

function showPage(i) {
  pvIndex = Math.max(0, Math.min(previewPages.length - 1, i));
  $("#modalBox").innerHTML = viewerHtml(pvIndex);
  if (previewPages[pvIndex + 1]) new Image().src = previewPages[pvIndex + 1].url;  // следующая страница подгружается заранее
}

document.addEventListener("keydown", e => {
  if (!$("#pvImg") || $("#modal").hidden) return;  // стрелки работают только пока открыт просмотр
  if (e.key === "ArrowRight") showPage(pvIndex + 1);
  if (e.key === "ArrowLeft") showPage(pvIndex - 1);
});

// выбрали файлы страниц: загружаем по очереди и добавляем в предпросмотр
document.addEventListener("change", async e => {
  if (e.target.id !== "f_preview_files") return;
  const files = [...e.target.files].sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
  e.target.value = "";
  if (!files.length) return;
  const err = $("#pvErr");
  let done = 0, problem = "";
  for (const f of files) {
    if (f.size > 5 * 1024 * 1024) { problem = `«${f.name}» больше 5 МБ`; break; }
    err.textContent = `Загружаю ${done + 1} из ${files.length}…`;
    try {
      const up = await uploadCover(f);
      await api(`/products/${curPid}/preview`, { method: "POST", body: { cover_url: up.cover_url } });
      done++;
    } catch (ex) { problem = `«${f.name}»: ${ex.message}`; break; }
  }
  if (done) ratingsChanged = true;  // после закрытия окна каталог обновится, и на карточке появится пометка
  await loadPreview();
  if (problem && $("#pvErr")) $("#pvErr").textContent = problem;
  if (done) toast(`Добавлено страниц: ${done}`);
});

// окно мангаки: меняется только обложка, остальные поля товара ему недоступны
async function openCoverForm(id) {
  const p = await api("/products/" + id);
  openModal(`<button class="close" data-act="close">×</button><h3>Обложка</h3>
    <p class="author" style="margin-bottom:12px">${esc(p.title)} · том ${p.volume}</p>
    <div class="coverField">
      <div class="coverPick">
        <div class="coverPrev" id="coverPrev">${p.cover_url ? `<img src="${esc(p.cover_url)}" alt="">` : "нет"}</div>
        <div>
          <input id="f_cover_file" type="file" accept="image/jpeg,image/png,image/webp" hidden>
          <button type="button" class="ghost" data-act="pick-cover">Выбрать файл</button>
          <small id="coverHint">JPG, PNG или WEBP, до 5 МБ</small>
        </div>
      </div>
      <input id="f_cover_url" type="hidden" value="${esc(p.cover_url)}">
    </div>
    <div class="err" id="aErr"></div>
    <button class="btn" data-act="save-cover" data-id="${id}">Сохранить обложку</button>`);
}

async function saveCover(id) {
  try {
    await api("/products/" + id + "/cover", { method: "PATCH", body: { cover_url: $("#f_cover_url").value } });
    closeModal(); toast("Обложка обновлена");
    if (view === "favs") loadFavs(); else loadCatalog();
  } catch (e) { $("#aErr").textContent = e.message; }
}

// после выбора файла: загружаем его на сервер, путь подставляется в форму сам
document.addEventListener("change", async e => {
  if (e.target.id !== "f_cover_file") return;
  const input = e.target, file = input.files[0];
  if (!file) return;
  const urlField = $("#f_cover_url"), prev = $("#coverPrev"), hint = $("#coverHint"),
    err = $("#aErr"), saveBtn = document.querySelector('[data-act="save"], [data-act="save-cover"]');
  err.textContent = "";
  if (file.size > 5 * 1024 * 1024) { err.textContent = "Файл больше 5 МБ"; input.value = ""; return; }
  hint.textContent = "Загружаю…";
  saveBtn.disabled = true;  // пока файл грузится, сохранить нельзя: иначе обложка потеряется
  try {
    const d = await uploadCover(file);
    urlField.value = d.cover_url;
    prev.innerHTML = `<img src="${esc(d.cover_url)}" alt="">`;
    hint.textContent = "Загружено ✓ Теперь нажмите «Сохранить»";
  } catch (ex) {
    err.textContent = ex.message;
    hint.textContent = "JPG, PNG или WEBP, до 5 МБ";
    if (/войти|Сессия/.test(ex.message)) logout();
  } finally {
    saveBtn.disabled = false;
    input.value = "";
  }
});

// ---------- обработка всех кликов ----------
document.addEventListener("click", async e => {
  const pg = e.target.closest("[data-page]");
  if (pg && !pg.disabled) { page = +pg.dataset.page; loadCatalog(); window.scrollTo({ top: 0, behavior: "smooth" }); return; }
  const v = e.target.closest("[data-view]");
  if (v) { showView(v.dataset.view); return; }
  const el = e.target.closest("[data-act]");
  if (!el) return;
  const id = +el.dataset.id;
  const needLogin = () => { if (!me) { openAuth(); return true; } return false; };
  try {
    switch (el.dataset.act) {
      case "close": closeModal(); break;
      case "auth": case "tab-login": openAuth("login"); break;
      case "tab-reg": openAuth("register"); break;
      case "do-login": doAuth("login"); break;
      case "do-reg": doAuth("register"); break;
      case "logout": logout(); break;
      case "open": openProduct(id); break;
      case "new": openProductForm(); break;
      case "edit": openProductForm(id); break;
      case "save": saveProduct(el.dataset.id); break;
      case "pick-cover": $("#f_cover_file").click(); break;
      case "cover": openCoverForm(id); break;
      case "save-cover": saveCover(el.dataset.id); break;
      case "rate":
        await api(`/products/${curPid}/rating`, { method: "POST", body: { value: +el.dataset.v } });
        ratingsChanged = true; await loadReviews(); break;
      case "unrate":
        await api(`/products/${curPid}/rating`, { method: "DELETE" });
        ratingsChanged = true; await loadReviews(); break;
      case "reply": {
        replyTo = { id, user: el.dataset.user };
        await loadReviews();
        const t = $("#cmText");
        if (!t.value) t.value = "@" + replyTo.user + ", ";
        t.focus();
        break;
      }
      case "cancel-reply": replyTo = null; await loadReviews(); break;
      case "send-comment": await sendComment(); break;
      case "open-preview": openModal(viewerHtml(0), true); showPage(+el.dataset.i); break;
      case "pv-prev": showPage(pvIndex - 1); break;
      case "pv-next": showPage(pvIndex + 1); break;
      case "pv-back": openProduct(curPid); break;
      case "pick-preview": $("#f_preview_files").click(); break;
      case "del-preview":
        if (confirm("Удалить эту страницу предпросмотра?")) {
          await api(`/products/${curPid}/preview/${id}`, { method: "DELETE" });
          ratingsChanged = true;
          await loadPreview();
        }
        break;
      case "del-comment":
        if (confirm("Удалить комментарий?")) { await api("/comments/" + id, { method: "DELETE" }); await loadReviews(); }
        break;
      case "del":
        if (confirm("Удалить товар?")) { await api("/products/" + id, { method: "DELETE" }); toast("Удалено"); loadGenres(); loadCatalog(); }
        break;
      case "fav":
        if (needLogin()) break;
        if (favIds.has(id)) { await api("/favorites/" + id, { method: "DELETE" }); favIds.delete(id); }
        else { await api("/favorites/" + id, { method: "POST" }); favIds.add(id); }
        if (view === "favs") loadFavs(); else el.classList.toggle("on");
        break;
      case "add":
        if (needLogin()) break;
        $("#cartCount").textContent = (await api("/cart/" + id, { method: "POST" })).count;
        toast("Добавлено в корзину");
        break;
      case "inc": await api("/cart/" + id, { method: "PATCH", body: { quantity: +el.dataset.q + 1 } }); loadCart(); break;
      case "dec": await api("/cart/" + id, { method: "PATCH", body: { quantity: +el.dataset.q - 1 } }); loadCart(); break;
      case "rm": await api("/cart/" + id, { method: "DELETE" }); loadCart(); break;
      case "order": {
        const o = await api("/orders", { method: "POST" });
        toast(`Заказ №${o.id} оформлен на ${money(o.total)}`);
        showView("orders"); refreshCartCount();
        break;
      }
    }
  } catch (err) { toast(err.message); if (/войти|Сессия/.test(err.message)) logout(); }
});

// ---------- старт ----------
(async function init() {
  if (token) {
    try { me = await api("/me"); await loadFavIds(); refreshCartCount(); }
    catch { token = null; me = null; localStorage.removeItem("token"); }
  }
  await loadGenres();
  showView("catalog");
})();