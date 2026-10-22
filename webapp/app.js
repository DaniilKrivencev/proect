// Кабинет салона. Чистый JavaScript, без сборки. Один и тот же код у всех салонов;
// подсказки в полях зависят от типа салона (маникюр / парикмахерская).
// Владелец видит весь салон и управляет мастерами; мастер — только свои записи и график.
const tg = window.Telegram?.WebApp;
tg?.ready();
tg?.expand();

// Тема: «auto» — как в Telegram, или выбранная вручную (запоминается на этом телефоне)
function themePref() { try { return localStorage.getItem("theme") || "auto"; } catch { return "auto"; } }
function setThemePref(v) { try { localStorage.setItem("theme", v); } catch {} applyTheme(); renderThemeControls(); }
function applyTheme() {
  const pref = themePref();
  const sys = tg?.colorScheme ? tg.colorScheme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  const dark = pref === "auto" ? sys : pref === "dark";
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  const bg = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
  try { tg?.setHeaderColor(bg); tg?.setBackgroundColor(bg); tg?.setBottomBarColor?.(bg); } catch {}
}
applyTheme();
tg?.onEvent?.("themeChanged", applyTheme);
function renderThemeControls() {
  const dark = document.documentElement.dataset.theme === "dark";
  const btn = document.getElementById("themeBtn");
  if (btn) {
    btn.innerHTML = dark
      ? '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.2 5.2l1.4 1.4M17.4 17.4l1.4 1.4M5.2 18.8l1.4-1.4M17.4 6.6l1.4-1.4"/></svg>'
      : '<svg viewBox="0 0 24 24"><path d="M19.5 14.5A8 8 0 0 1 9.5 4.5a8 8 0 1 0 10 10z"/></svg>';
    btn.setAttribute("aria-label", dark ? "Включить светлую тему" : "Включить тёмную тему");
  }
  document.querySelectorAll("#themeSeg button").forEach((b) => b.classList.toggle("active", b.dataset.theme === themePref()));
}

const DEV = !tg?.initData && new URLSearchParams(location.search).has("dev");
// Личная ссылка входа (?k=…): так кабинет открывают сотрудники из MAX — в обычном браузере
const LOGIN = (() => {
  const k = new URLSearchParams(location.search).get("k");
  try {
    if (k) { sessionStorage.setItem("k", k); history.replaceState(null, "", location.pathname); }
    return k || sessionStorage.getItem("k") || "";
  } catch { return k || ""; }
})();
const AUTH = tg?.initData || LOGIN || (DEV ? "dev" : "");
const AUTH_HEADERS = tg?.initData ? { "X-Init-Data": tg.initData } : LOGIN ? { "X-Login": LOGIN } : DEV ? { "X-Init-Data": "dev" } : {};
// Поделиться текстом: в Telegram — окно «переслать», в браузере — системное меню или копирование
async function shareText(text, url) {
  const full = url ? text + "\n" + url : text;
  if (tg?.initData) {
    tg.openTelegramLink(url ? `https://t.me/share/url?url=${encodeURIComponent(url)}&text=${encodeURIComponent(text)}`
      : `https://t.me/share/url?url=${encodeURIComponent(full)}`);
    return;
  }
  try { if (navigator.share) { await navigator.share({ text: full }); return; } } catch {}
  try { await navigator.clipboard.writeText(full); toast("Скопировано — вставьте в сообщение"); } catch { prompt("Скопируйте:", full); }
}

const WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];
const MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"];
const PASTELS = ["#EFE1D9", "#E5E0EC", "#DEE7DF", "#F1E5D3", "#DCE3EB", "#ECDFE2"];
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (v) => Number(v || 0).toLocaleString("ru-RU") + " ₽";
const dur = (m) => (m >= 60 ? `${Math.floor(m / 60)} ч${m % 60 ? " " + (m % 60) + " мин" : ""}` : `${m} мин`);
const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const wdMon = (d) => (d.getDay() + 6) % 7;
const nowStr = () => { const d = new Date(); return `${iso(d)} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`; };
const human = (d) => `${WD[wdMon(d)]}, ${d.getDate()} ${MONTHS[d.getMonth()]}`;
const fromIso = (s) => { const [y, m, d] = s.slice(0, 10).split("-").map(Number); return new Date(y, m - 1, d); };
const plural = (n, a, b, c) => (n % 10 === 1 && n % 100 !== 11 ? a : n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 10 || n % 100 >= 20) ? b : c);
const initials = (name) => name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("");
const avatar = (m, cls = "") => m ? `<span class="ava ${cls}" style="background:${PASTELS[(m.id - 1) % PASTELS.length]}">${esc(initials(m.name))}</span>` : "";

const PH_BY_NICHE = {
  nails: { master: "Мастер маникюра", spec: "Топ-мастер, наращивание и дизайн", newSpec: "Мастер маникюра и педикюра",
    service: "Маникюр + гель-лак", serviceEx: "«Маникюр + гель-лак», 2200 ₽, 2 часа",
    note: "Любит миндалевидную форму и нюд. Тонкие ногти — без агрессивного опила", about: "Уютная студия маникюра. Стерильные инструменты, кофе и пледы ☕" },
  hair: { master: "Мастер по волосам", spec: "Топ-стилист, сложное окрашивание", newSpec: "Парикмахер-универсал",
    service: "Женская стрижка", serviceEx: "«Женская стрижка», от 2000 ₽, 1 час",
    note: "Корни 6.0 + 7.1, оксид 6%. Не любит короткие виски", about: "Стрижки, окрашивание и уход. Работаем на профессиональной косметике" },
};
const PH = () => PH_BY_NICHE[state.me?.niche] || PH_BY_NICHE.nails;
const state = { me: null, masters: [], services: [], day: iso(new Date()), master: 0, clients: [], clientFilter: "all" };
const isAdmin = () => state.me?.role === "admin";
// Частный мастер: владелец и единственный мастер в одном лице — интерфейс без «Мастеров» и «Администраторов»
const isSolo = () => isAdmin() && state.me?.mode === "solo";
const myMasterId = () => (isAdmin() ? state.me?.own_master_id : state.me?.master_id);
const masterById = (id) => state.masters.find((m) => m.id === id);

// ---------- сеть ----------
async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", ...AUTH_HEADERS },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    let msg = data.detail;
    if (Array.isArray(msg)) msg = "Проверьте, что все поля заполнены правильно";
    throw new Error(msg || "Ошибка сервера");
  }
  return data;
}
function toast(text) {
  const t = $("toast");
  t.textContent = text;
  t.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("show"), 2200);
  tg?.HapticFeedback?.notificationOccurred("success");
}
function fail(e) {
  tg?.HapticFeedback?.notificationOccurred("error");
  if (tg?.showAlert) tg.showAlert(e.message); else alert(e.message);
}
const confirmBox = (text) => new Promise((ok) => (tg?.showConfirm ? tg.showConfirm(text, ok) : ok(confirm(text))));

function openSheet(html) {
  $("sheet").innerHTML = `<button class="sheet-close" aria-label="Закрыть"><svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6 6 18"/></svg></button>` + html;
  $("sheet").querySelector(".sheet-close").onclick = () => $("sheetBg").click();
  $("sheet").classList.add("open"); $("sheetBg").classList.add("open");
}
function closeSheet() { $("sheet").classList.remove("open"); $("sheetBg").classList.remove("open"); }
$("sheetBg").onclick = closeSheet;

// ---------- навигация ----------
const ICONS = {
  bookings: '<rect x="3.5" y="5" width="17" height="15" rx="4"/><path d="M8 3v4M16 3v4M3.5 10h17"/>',
  masters: '<path d="M7 11.5C7 7.4 9.2 4 12 4s5 3.4 5 7.5V18a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2z"/><path d="M9.2 17.5c.6-1.5 1.6-2.3 2.8-2.3s2.2.8 2.8 2.3"/>',
  services: '<path d="M5 6.5h14M5 12h14M5 17.5h9"/>',
  clients: '<circle cx="12" cy="8.5" r="3.5"/><path d="M5 20c1-3.6 3.8-5.5 7-5.5s6 1.9 7 5.5"/>',
  salon: '<path d="M4 10.5 12 4l8 6.5V19a1 1 0 0 1-1 1h-4.5v-5h-5v5H5a1 1 0 0 1-1-1z"/>',
  myschedule: '<circle cx="12" cy="12" r="8"/><path d="M12 7.5V12l3 2"/>',
  chats: '<path d="M4.5 6.5a2 2 0 0 1 2-2h11a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H10l-4 3.5v-3.5h0a1.5 1.5 0 0 1-1.5-1.5z"/>',
  reviews: '<path d="M12 4.5l2.3 4.7 5.2.8-3.8 3.6.9 5.2-4.6-2.4-4.6 2.4.9-5.2-3.8-3.6 5.2-.8z"/>',
};
const LABELS = { bookings: "Записи", chats: "Чаты", masters: "Мастера", clients: "Клиенты", salon: "Салон", myschedule: "График", reviews: "Отзывы" };
const LOADERS = { bookings: () => loadDay(), chats: () => loadChats(), masters: () => loadMasters(),
  clients: () => loadClients(), salon: () => loadSalon(), myschedule: () => loadMySchedule(), reviews: () => loadReviews() };

function buildTabs() {
  const tabs = isSolo() ? ["bookings", "chats", "clients", "reviews", "myschedule", "salon"]
    : isAdmin() ? ["bookings", "chats", "masters", "clients", "reviews", "salon"] : ["bookings", "clients", "reviews", "myschedule"];
  const label = (t) => (t === "salon" && isSolo() ? "Профиль" : LABELS[t]);
  $("tabs").innerHTML = tabs.map((t, i) => `<button data-tab="${t}" class="${i ? "" : "active"}"><svg viewBox="0 0 24 24">${ICONS[t]}</svg>${label(t)}${t === "chats" ? '<span class="count" id="chatCount" hidden></span>' : ""}</button>`).join("");
  $("tabs").querySelectorAll("button").forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));
  updateChatBadge();
}
function updateChatBadge() {
  const el = $("chatCount");
  if (!el) return;
  const n = state.me?.unread_chats || 0;
  el.hidden = !n; el.textContent = n;
}
function showTab(name) {
  document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("active", x.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + name));
  LOADERS[name]().catch(fail);
  window.scrollTo(0, 0);
}

// ---------- шапка ----------
async function loadMe() {
  state.me = await api("api/me");
  const { me } = state;
  $("salonName").textContent = me.salon.salon_name;
  document.title = me.salon.salon_name + " — кабинет";
  $("pAbout").placeholder = PH().about;
  $("whoAmI").textContent = isSolo() ? "Мой кабинет" : isAdmin() ? "Кабинет владельца" : `Мастер ${me.name}`;
  const n = me.next;
  if (n) {
    const d = fromIso(n.start), diff = (d - fromIso(me.today)) / 864e5;
    const when = diff === 0 ? "сегодня" : diff === 1 ? "завтра" : `${d.getDate()} ${MONTHS[d.getMonth()]}`;
    $("next").innerHTML = `<div class="next">
      <div class="next-label">Ближайшая запись</div>
      <div class="next-time">${n.start.slice(11)}<small>${when}</small></div>
      <div class="next-who">${esc(n.client_name)}</div>
      <div class="next-what">${esc(n.service_name)}${isAdmin() && !isSolo() ? ` · мастер ${esc(n.master_name)}` : ""}${n.client_phone ? ` · <a href="tel:${esc(n.client_phone)}">${esc(n.client_phone)}</a>` : ""}</div>
    </div>`;
  } else {
    $("next").innerHTML = `<div class="next empty-next">
      <div class="next-label">Ближайших записей нет</div>
      <div class="next-time">Окошки свободны — самое время позвать клиентов</div>
      <div class="next-what">${isSolo() ? "Ссылка для записи — во вкладке «Профиль»" : isAdmin() ? "Ссылка для записи — во вкладке «Салон»" : "Новые записи придут вам в Telegram"}</div>
    </div>`;
  }
  const s = me.stats;
  $("stats").innerHTML = `
    <div class="stat"><b>${money(s.month_revenue)}</b><span>за этот месяц</span></div>
    <div class="stat"><b>${s.upcoming}</b><span>${plural(s.upcoming, "запись впереди", "записи впереди", "записей впереди")}</span></div>
    ${isAdmin() && !isSolo() ? `<div class="stat"><b>${s.masters}</b><span>${plural(s.masters, "мастер", "мастера", "мастеров")}</span></div>` : ""}`;
}

// ---------- записи ----------
function renderMasterChips() {
  if (!isAdmin() || isSolo()) { $("masterChips").hidden = true; return; }
  const chips = [`<button class="chip ${state.master ? "" : "active"}" data-m="0">Все мастера</button>`]
    .concat(state.masters.map((m) => `<button class="chip ${state.master === m.id ? "active" : ""}" data-m="${m.id}">${avatar(m)}${esc(m.name)}</button>`));
  $("masterChips").innerHTML = chips.join("");
  $("masterChips").querySelectorAll(".chip").forEach((c) => (c.onclick = () => { state.master = Number(c.dataset.m); loadDay(); }));
}

async function renderDays() {
  const summary = await api(`api/days_summary?master_id=${state.master}`);
  const start = new Date();
  let html = "";
  for (let i = 0; i < 14; i++) {
    const d = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i), k = iso(d);
    html += `<button class="daybtn ${k === state.day ? "active" : ""}" data-day="${k}">${summary[k] ? '<i class="badge"></i>' : ""}
      <div class="wd">${i === 0 ? "сег" : WD[wdMon(d)]}</div><div class="dn">${d.getDate()}</div></button>`;
  }
  $("days").innerHTML = html;
  $("days").querySelectorAll(".daybtn").forEach((b) => (b.onclick = () => { state.day = b.dataset.day; loadDay(); }));
}

async function loadDay() {
  if (!isAdmin() || isSolo()) state.master = myMasterId();
  renderMasterChips();
  await renderDays();
  const data = await api(`api/bookings?day=${state.day}&master_id=${state.master}`);
  $("dayTitle").textContent = human(fromIso(state.day));
  if (state.master) {
    $("dayHours").textContent = data.day_off ? "Выходной — запись закрыта" : data.hours ? `Работает ${data.hours[0]}–${data.hours[1]}` : "Не рабочий день по графику";
    $("toggleOff").hidden = false;
    $("toggleOff").textContent = data.day_off ? "Открыть запись" : "Сделать выходным";
    $("toggleOff").onclick = async () => {
      try { const r = await api(`api/days_off/${state.master}/${state.day}`, { method: "POST" }); toast(r.day_off ? "День закрыт для записи" : "Запись открыта"); loadDay(); }
      catch (e) { fail(e); }
    };
  } else {
    $("dayHours").textContent = data.working.length ? `Работают: ${data.working.join(", ")}` : "Сегодня никто не работает";
    $("toggleOff").hidden = true;
  }
  const list = data.bookings, active = list.filter((b) => b.status === "active");
  if (!list.length) {
    $("bookingList").innerHTML = `<div class="empty"><b>Свободный день</b>${state.master ? "Все окошки открыты — новые записи появятся здесь" : "Записей пока нет ни у одного мастера"}</div>`;
    return;
  }
  $("bookingList").innerHTML = list.map((b) => {
    const m = masterById(b.master_id) || { id: b.master_id, name: b.master_name };
    return `<div class="bk ${b.status !== "active" ? "cancelled" : ""}">
      <div class="time">${b.start.slice(11)}</div>
      <div class="card-in">
        <div class="top-line"><div class="title">${esc(b.client_name)}</div>
          ${b.status === "active" ? `<button class="danger small" data-cancel="${b.id}">Отменить</button>` : `<span class="tag">отменена ${b.cancelled_by === "client" ? "клиентом" : "салоном"}</span>`}</div>
        <div class="sub">${esc(b.service_name)} · ${dur(b.duration)} · ${money(b.price)}</div>
        ${b.client_phone ? `<div class="sub"><a href="tel:${esc(b.client_phone)}">${esc(b.client_phone)}</a></div>` : ""}
        ${b.client_note ? `<div class="sub">${esc(b.client_note)}</div>` : ""}
        ${isAdmin() && !state.master ? `<div class="master-line">${avatar(m)}${esc(m.name)}</div>` : ""}
        ${b.status === "active" && b.start <= nowStr() ? `<button class="ghost small rv-ask" data-rv="${b.id}">⭐ Попросить отзыв</button>` : ""}
      </div></div>`;
  }).join("") + (active.length ? `<div class="total">${active.length} ${plural(active.length, "запись", "записи", "записей")} · <b>${money(active.reduce((s, b) => s + b.price, 0))}</b></div>` : "");
  $("bookingList").querySelectorAll("[data-rv]").forEach((btn) => (btn.onclick = async () => {
    try { const r = await api(`api/bookings/${btn.dataset.rv}/review_link`); shareText(r.text, r.link); }
    catch (e) { fail(e); }
  }));
  $("bookingList").querySelectorAll("[data-cancel]").forEach((btn) => (btn.onclick = async () => {
    if (!(await confirmBox("Отменить запись? Клиент получит уведомление в Telegram."))) return;
    try { await api(`api/bookings/${btn.dataset.cancel}/cancel`, { method: "POST" }); toast("Запись отменена"); loadDay(); loadMe(); }
    catch (e) { fail(e); }
  }));
}

async function openBookingForm(preClient = null) {
  const masters = isAdmin() ? state.masters : [masterById(state.me.master_id)].filter(Boolean);
  if (!masters.length) return fail(new Error(isAdmin() ? "Сначала добавьте мастера во вкладке «Мастера»" : "Мастер не найден"));
  let mid = state.master || masters[0].id;
  openSheet(`
    <h3>Новая запись</h3>
    <p class="muted small">Для клиентов, которые звонят или пишут в личку.</p>
    ${preClient ? `<div class="item"><div class="body"><div class="title">${esc(preClient.name)}</div><div class="sub">${esc(preClient.phone || "")}</div></div></div>`
      : `<label>Имя клиента<input id="bName" placeholder="Анна"></label>
         <label>Телефон<input id="bPhone" type="tel" placeholder="+7 900 123-45-67"></label>`}
    ${masters.length > 1 ? `<label>Мастер<select id="bMaster">${masters.map((m) => `<option value="${m.id}" ${m.id === mid ? "selected" : ""}>${esc(m.name)}</option>`).join("")}</select></label>` : ""}
    <label>Услуга<select id="bService"></select></label>
    <label>Дата<input id="bDay" type="date" value="${state.day}"></label>
    <label>Свободное время</label><div class="times" id="bTimes"></div>
    <label>или другое время<input id="bCustom" type="time"></label>
    <button class="primary wide" id="bSave">Записать</button>`);
  let chosen = null;
  const fillServices = () => {
    const ids = masterById(mid)?.service_ids || [];
    const list = state.services.filter((s) => ids.includes(s.id));
    $("bService").innerHTML = list.map((s) => `<option value="${s.id}">${esc(s.name)} · ${dur(s.duration)}</option>`).join("")
      || `<option value="">У мастера не отмечены услуги</option>`;
  };
  const loadTimes = async () => {
    chosen = null;
    if (!$("bService").value) { $("bTimes").innerHTML = ""; return; }
    const times = await api(`api/slots?service_id=${$("bService").value}&day=${$("bDay").value}&master_id=${mid}`);
    $("bTimes").innerHTML = times.length ? times.map((t) => `<button data-t="${t}">${t}</button>`).join("")
      : `<div class="muted small" style="grid-column:1/-1">Свободных окошек по графику нет — укажите время вручную</div>`;
    $("bTimes").querySelectorAll("button").forEach((b) => (b.onclick = () => {
      $("bTimes").querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
      chosen = b.dataset.t; $("bCustom").value = "";
    }));
  };
  if ($("bMaster")) $("bMaster").onchange = () => { mid = Number($("bMaster").value); fillServices(); loadTimes().catch(fail); };
  $("bService").onchange = () => loadTimes().catch(fail);
  $("bDay").onchange = () => loadTimes().catch(fail);
  fillServices();
  loadTimes().catch(fail);
  $("bSave").onclick = async () => {
    const time = $("bCustom").value || chosen;
    const name = preClient ? preClient.name : $("bName").value.trim();
    if (!name) return fail(new Error("Укажите имя клиента"));
    if (!$("bService").value) return fail(new Error("Выберите услугу"));
    if (!time) return fail(new Error("Выберите время"));
    try {
      await api("api/bookings", { method: "POST", body: {
        client_name: name, client_phone: preClient ? "" : $("bPhone").value.trim(), client_id: preClient?.id || null,
        service_id: Number($("bService").value), master_id: mid, day: $("bDay").value, time } });
      state.day = $("bDay").value;
      closeSheet(); toast("Запись добавлена");
      showTab("bookings"); loadMe();
    } catch (e) { fail(e); }
  };
}
$("addBooking").onclick = () => openBookingForm();

// ---------- мастера ----------
async function refreshMasters() { state.masters = await api("api/masters"); }

async function loadMasters() {
  await refreshMasters();
  $("masterList").innerHTML = state.masters.length ? state.masters.map((m) => `
    <div class="item click" data-id="${m.id}">
      ${avatar(m, "big")}
      <div class="body">
        <div class="title">${esc(m.name)}</div>
        <div class="sub">${esc(m.spec || PH().master)} · ${m.service_ids.length} ${plural(m.service_ids.length, "услуга", "услуги", "услуг")}</div>
        <span class="tag ${m.connected ? "" : "accent"}">${m.connected ? "Telegram подключён" : "Ждёт приглашения"}</span>
      </div>
      <div class="sub" style="text-align:right">${m.upcoming}<br>${plural(m.upcoming, "запись", "записи", "записей")}</div>
    </div>`).join("")
    : `<div class="empty"><b>Добавьте первого мастера</b>Клиенты смогут записаться, когда в салоне появится хотя бы один мастер</div>`;
  $("masterList").querySelectorAll(".item").forEach((el) => (el.onclick = () => openMaster(masterById(Number(el.dataset.id)))));
}

function scheduleRows(schedule) {
  return schedule.map((d) => `
    <div class="sched" data-wd="${d.weekday}">
      <label><input type="checkbox" ${d.works ? "checked" : ""}> ${WD[d.weekday]}</label>
      <input type="time" value="${d.start}" ${d.works ? "" : "disabled"}>
      <input type="time" value="${d.end}" ${d.works ? "" : "disabled"}>
    </div>`).join("");
}
function bindSchedule(root) {
  root.querySelectorAll(".sched").forEach((row) => {
    const [cb, s, e] = row.querySelectorAll("input");
    cb.onchange = () => { s.disabled = e.disabled = !cb.checked; };
  });
}
function readSchedule(root) {
  return [...root.querySelectorAll(".sched")].map((row) => {
    const [cb, s, e] = row.querySelectorAll("input");
    return { weekday: Number(row.dataset.wd), works: cb.checked, start: s.value, end: e.value };
  });
}

function shareInvite(m) {
  const text = `${m.name}, это приглашение в кабинет мастера салона «${state.me.salon.salon_name}». ` +
    `Откройте ссылку в мессенджере, которым пользуетесь, — и записи к вам будут приходить туда.` +
    (m.invite_link_max ? `\nTelegram: ${m.invite_link}\nMAX: ${m.invite_link_max}` : "");
  shareText(text, m.invite_link_max ? "" : m.invite_link);
}

function openMaster(m) {
  if (!state.services.length) return api("api/services").then((s) => { state.services = s; openMaster(m); }).catch(fail);
  openSheet(`
    <div class="sheet-head">${avatar(m, "big")}<div><h3>${esc(m.name)}</h3>
      <div class="sub">${m.connected ? "Мессенджер подключён — получает свои записи" : "Ещё не подключён к боту"}</div></div></div>
    <label>Имя<input id="mName" maxlength="60" value="${esc(m.name)}"></label>
    <label>Описание для клиентов<input id="mSpec" maxlength="100" value="${esc(m.spec)}" placeholder="${PH().spec}"></label>
    <div class="section-title">Услуги мастера</div>
    <div class="checks">${state.services.map((s) => `<label class="check"><input type="checkbox" value="${s.id}" ${m.service_ids.includes(s.id) ? "checked" : ""}>${esc(s.name)}</label>`).join("")}</div>
    <div class="section-title">График</div>
    <div id="mSchedule">${scheduleRows(m.schedule)}</div>
    <button class="primary wide" id="mSave">Сохранить</button>
    <button class="ghost wide" id="mInvite">${m.connected ? "Отправить приглашение заново" : "Отправить приглашение мастеру"}</button>
    ${m.connected ? `<button class="danger wide" id="mReset">Отключить Telegram и создать новую ссылку</button>` : ""}
    <button class="danger wide" id="mDelete">Удалить мастера</button>`);
  bindSchedule($("mSchedule"));
  $("mSave").onclick = async () => {
    try {
      await api(`api/masters/${m.id}`, { method: "PUT", body: {
        name: $("mName").value.trim(), spec: $("mSpec").value.trim(),
        service_ids: [...$("sheet").querySelectorAll(".checks input:checked")].map((i) => Number(i.value)),
        schedule: readSchedule($("mSchedule")) } });
      closeSheet(); toast("Сохранено"); loadMasters();
    } catch (e) { fail(e); }
  };
  $("mInvite").onclick = () => shareInvite(m);
  if ($("mReset")) $("mReset").onclick = async () => {
    if (!(await confirmBox(`Отключить Telegram мастера ${m.name}? Старая ссылка перестанет работать, уведомления перестанут приходить, пока мастер не откроет новую ссылку.`))) return;
    try { const nm = await api(`api/masters/${m.id}/new_invite`, { method: "POST" }); await refreshMasters(); toast("Создана новая ссылка"); openMaster(nm); }
    catch (e) { fail(e); }
  };
  $("mDelete").onclick = async () => {
    if (!(await confirmBox(`Удалить мастера ${m.name}? Клиенты больше не смогут к нему записаться. История записей сохранится.`))) return;
    try { await api(`api/masters/${m.id}`, { method: "DELETE" }); closeSheet(); toast("Мастер удалён"); if (state.master === m.id) state.master = 0; loadMasters(); loadMe(); }
    catch (e) { fail(e); }
  };
}

$("addMaster").onclick = () => {
  openSheet(`
    <h3>Новый мастер</h3>
    <label>Имя<input id="nmName" maxlength="60" placeholder="Анастасия"></label>
    <label>Описание для клиентов<input id="nmSpec" maxlength="100" placeholder="${PH().newSpec}"></label>
    <p class="muted small">По умолчанию мастер делает все услуги и работает пн–пт 10–20, сб 10–18. Это можно поменять на следующем шаге.</p>
    <button class="primary wide" id="nmSave">Добавить мастера</button>`);
  $("nmSave").onclick = async () => {
    const name = $("nmName").value.trim();
    if (!name) return fail(new Error("Введите имя мастера"));
    try {
      const m = await api("api/masters", { method: "POST", body: { name, spec: $("nmSpec").value.trim() } });
      await refreshMasters(); loadMasters(); loadMe(); toast("Мастер добавлен");
      openMaster(m);
    } catch (e) { fail(e); }
  };
};

// ---------- мой график (мастер) ----------
async function loadMySchedule() {
  await refreshMasters();
  const m = masterById(myMasterId());
  if (!m) { $("mySchedule").innerHTML = `<div class="empty">Мастер не найден</div>`; return; }
  $("mySchedule").innerHTML = scheduleRows(m.schedule);
  bindSchedule($("mySchedule"));
}
$("saveMySchedule").onclick = async () => {
  try { await api(`api/masters/${myMasterId()}`, { method: "PUT", body: { schedule: readSchedule($("mySchedule")) } }); toast("График сохранён"); }
  catch (e) { fail(e); }
};

// ---------- услуги ----------
async function loadServices() {
  state.services = await api("api/services");
  $("serviceList").innerHTML = state.services.length ? state.services.map((s) => `
    <div class="item click" data-id="${s.id}">
      <div class="body"><div class="title">${esc(s.name)}</div><div class="sub">${dur(s.duration)}</div></div>
      <div class="price">${s.price_from ? "от " : ""}${money(s.price)}</div>
    </div>`).join("")
    : `<div class="empty"><b>Добавьте первую услугу</b>Например, ${PH().serviceEx}</div>`;
  $("serviceList").querySelectorAll(".item").forEach((el) => (el.onclick = () => openService(state.services.find((s) => s.id == el.dataset.id))));
}

function openService(s = null) {
  const durations = [15, 30, 45, 60, 75, 90, 105, 120, 150, 180, 210, 240, 300];
  const cur = s?.duration || 60;
  if (!durations.includes(cur)) durations.push(cur);
  openSheet(`
    <h3>${s ? "Изменить услугу" : "Новая услуга"}</h3>
    <label>Название<input id="sName" maxlength="60" value="${esc(s?.name || "")}" placeholder="${PH().service}"></label>
    <label>Цена, ₽<input id="sPrice" type="number" inputmode="numeric" min="0" value="${s?.price ?? ""}" placeholder="2200"></label>
    <label class="check"><input type="checkbox" id="sFrom" ${s?.price_from ? "checked" : ""}>
      <div>Цена «от»<span>Итог зависит от длины и сложности дизайна</span></div></label>
    <label>Длительность<select id="sDur">${durations.sort((a, b) => a - b).map((d) => `<option value="${d}" ${d === cur ? "selected" : ""}>${dur(d)}</option>`).join("")}</select></label>
    <button class="primary wide" id="sSave">Сохранить</button>
    ${s ? `<button class="danger wide" id="sDel">Удалить услугу</button>` : `<p class="muted small">Новую услугу сразу делают все мастера — это можно поменять в карточке мастера.</p>`}`);
  $("sSave").onclick = async () => {
    const body = { name: $("sName").value.trim(), price: Number($("sPrice").value || 0), duration: Number($("sDur").value), price_from: $("sFrom").checked };
    if (!body.name) return fail(new Error("Введите название услуги"));
    try {
      await api(s ? `api/services/${s.id}` : "api/services", { method: s ? "PUT" : "POST", body });
      closeSheet(); toast("Услуга сохранена"); loadServices(); refreshMasters();
    } catch (e) { fail(e); }
  };
  if (s) $("sDel").onclick = async () => {
    if (!(await confirmBox(`Удалить «${s.name}»? Уже созданные записи останутся.`))) return;
    try { await api(`api/services/${s.id}`, { method: "DELETE" }); closeSheet(); toast("Удалено"); loadServices(); }
    catch (e) { fail(e); }
  };
}
$("addService").onclick = () => openService();

// ---------- клиенты ----------
async function loadClients() { state.clients = await api("api/clients"); renderClients(); }
function renderClients() {
  const q = $("clientSearch").value.trim().toLowerCase();
  let list = state.clients.filter((c) => !q || c.name.toLowerCase().includes(q) || (c.phone || "").includes(q));
  if (state.clientFilter === "sleeping") list = list.filter((c) => c.sleeping);
  if (!list.length) {
    $("clientList").innerHTML = `<div class="empty">${state.clients.length ? "Никого не нашлось" : "<b>Пока пусто</b>Клиенты появятся здесь после первых записей"}</div>`;
    return;
  }
  const short = (s) => s.slice(8, 10) + "." + s.slice(5, 7);
  $("clientList").innerHTML = list.map((c) => `
    <div class="item click" data-id="${c.id}">
      <div class="body">
        <div class="title">${esc(c.name)}</div>
        <div class="sub">${esc(c.phone || "без телефона")} · ${c.visits} ${plural(c.visits, "визит", "визита", "визитов")} · ${money(c.spent)}</div>
        <div class="sub">${c.next_visit ? `Следующий визит ${short(c.next_visit)} в ${c.next_visit.slice(11)}` : c.last_visit ? `Последний визит ${short(c.last_visit)}` : "Ещё не была"}${isAdmin() && c.fav_master ? ` · любимый мастер ${esc(c.fav_master)}` : ""}</div>
        ${c.sleeping ? `<span class="tag accent">давно не была</span>` : ""}
      </div>
    </div>`).join("");
  $("clientList").querySelectorAll(".item").forEach((el) => (el.onclick = () => openClient(state.clients.find((c) => c.id == el.dataset.id))));
}
$("clientSearch").oninput = renderClients;
document.querySelectorAll("#tab-clients .chip").forEach((ch) => (ch.onclick = () => {
  document.querySelectorAll("#tab-clients .chip").forEach((x) => x.classList.toggle("active", x === ch));
  state.clientFilter = ch.dataset.filter;
  renderClients();
}));

function openClient(c) {
  openSheet(`
    <h3>${esc(c.name)}</h3>
    <div class="muted small">${c.visits} ${plural(c.visits, "визит", "визита", "визитов")} · ${money(c.spent)}${c.fav_master ? ` · любимый мастер ${esc(c.fav_master)}` : ""}</div>
    <label>Имя<input id="cName" value="${esc(c.name)}"></label>
    <label>Телефон<input id="cPhone" type="tel" value="${esc(c.phone)}"></label>
    <label>Заметки (видят только сотрудники)<textarea id="cNote" rows="3" placeholder="${PH().note}">${esc(c.note)}</textarea></label>
    <button class="primary wide" id="cSave">Сохранить</button>
    <div class="row">
      ${c.phone ? `<a class="ghost" style="padding:13px" href="tel:${esc(c.phone)}">Позвонить</a>` : ""}
      ${c.username ? `<button class="ghost" id="cTg">Написать</button>` : ""}
      <button class="ghost" id="cBook">Записать</button>
    </div>
    ${c.tg_id ? `<button class="ghost wide" id="cInvite">Пригласить снова — сообщение от бота</button>` : ""}
    ${isAdmin() ? `<button class="danger wide" id="cDelete">Удалить клиента</button>` : ""}`);
  if ($("cDelete")) $("cDelete").onclick = async () => {
    if (!(await confirmBox(`Удалить ${c.name}? Имя, телефон, заметки и переписка будут стёрты навсегда. ` +
      `Прошлые визиты останутся в статистике как «Удалённый клиент».`))) return;
    try { await api(`api/clients/${c.id}`, { method: "DELETE" }); closeSheet(); toast("Клиент удалён"); loadClients(); loadMe(); }
    catch (e) { fail(e); }
  };
  $("cSave").onclick = async () => {
    try { await api(`api/clients/${c.id}`, { method: "PUT", body: { note: $("cNote").value, name: $("cName").value, phone: $("cPhone").value } }); closeSheet(); toast("Сохранено"); loadClients(); }
    catch (e) { fail(e); }
  };
  $("cBook").onclick = () => { closeSheet(); setTimeout(() => openBookingForm(c), 250); };
  if ($("cTg")) $("cTg").onclick = () => (tg ? tg.openTelegramLink(`https://t.me/${c.username}`) : window.open(`https://t.me/${c.username}`));
  if ($("cInvite")) $("cInvite").onclick = async () => {
    if (!(await confirmBox(`Отправить ${c.name} сообщение «Давно вас не видели» со ссылкой на запись?`))) return;
    try { await api(`api/clients/${c.id}/invite`, { method: "POST" }); closeSheet(); toast("Приглашение отправлено"); }
    catch (e) { fail(e); }
  };
}

// ---------- салон ----------
async function loadSalon() {
  await loadMe();
  loadServices().catch(fail);
  api("api/admin_invite").then((r) => { $("adminList").textContent = "Сейчас: " + r.admins.join(", "); state.adminInvite = r.link; }).catch(() => {});
  const s = state.me.salon;
  $("clientLink").textContent = state.me.link_max ? `Telegram: ${state.me.link}\nMAX: ${state.me.link_max}` : (state.me.link || "Ссылка появится, когда бот запущен");
  $("pName").value = s.salon_name; $("pAddress").value = s.address; $("pPhone").value = s.phone || ""; $("pAbout").value = s.about; $("pStep").value = String(s.slot_step);
  $("adminCard").hidden = isSolo();
  $("hAbout").textContent = isSolo() ? "О вас" : "О салоне";
  $("lPhone").textContent = isSolo() ? "Телефон для клиентов" : "Телефон салона";
  renderMode();
  const sc = $("supportCard");
  if (sc) {
    sc.style.display = state.me.support_access ? "" : "none";
    if (state.me.support_access) $("supportNote").textContent =
      "Разработчик сервиса может входить в ваш салон для настройки и помощи. Если вам это не нужно — напишите ему, доступ отключат.";
  }
}
$("copyLink").onclick = async () => {
  try { await navigator.clipboard.writeText($("clientLink").textContent); toast("Ссылка скопирована"); }
  catch { fail(new Error("Не получилось скопировать — нажмите и удерживайте ссылку")); }
};
$("shareLink").onclick = () => {
  const text = `Онлайн-запись в «${state.me.salon.salon_name}»` + (state.me.link_max ? `\nTelegram: ${state.me.link}\nMAX: ${state.me.link_max}` : "");
  shareText(text, state.me.link_max ? "" : state.me.link);
};
$("saveSalon").onclick = async () => {
  try {
    await api("api/salon", { method: "PUT", body: { salon_name: $("pName").value.trim(), address: $("pAddress").value.trim(), phone: $("pPhone").value.trim(),
      about: $("pAbout").value.trim(), slot_step: Number($("pStep").value) } });
    toast("Сохранено"); loadMe();
  } catch (e) { fail(e); }
};

$("inviteAdmin").onclick = async () => {
  try {
    const r = await api("api/admin_invite");
    const text = `Приглашение стать администратором салона «${state.me.salon.salon_name}» — откройте ссылку` +
      (r.link_max ? `\nTelegram: ${r.link}\nMAX: ${r.link_max}` : "");
    shareText(text, r.link_max ? "" : r.link);
  } catch (e) { fail(e); }
};

// ---------- чаты ----------
const timeShort = (s) => {
  const today = iso(new Date());
  return s.slice(0, 10) === today ? s.slice(11, 16) : `${s.slice(8, 10)}.${s.slice(5, 7)}`;
};
async function loadChats() {
  const chats = await api("api/chats");
  state.me.unread_chats = chats.filter((c) => c.unread).length;
  updateChatBadge();
  $("chatList").innerHTML = chats.length ? chats.map((c) => `
    <div class="item click chat-item ${c.unread ? "unread" : ""}" data-id="${c.id}">
      ${avatar({ id: c.id, name: c.name }, "big")}
      <div class="body">
        <div class="title">${esc(c.name)}</div>
        <div class="preview">${c.last_dir === "out" ? "Вы: " : ""}${c.last_photo ? "📸 Фото " : ""}${esc(c.last_text || "")}</div>
      </div>
      <div class="meta">${timeShort(c.last_at)}${c.unread ? `<br><span class="dot">${c.unread}</span>` : ""}</div>
    </div>`).join("")
    : `<div class="empty"><b>Пока тихо</b>Когда клиентка напишет боту вопрос или пришлёт фото, переписка появится здесь</div>`;
  $("chatList").querySelectorAll(".item").forEach((el) => (el.onclick = () => openChat(Number(el.dataset.id))));
}

let chatTimer = null;
async function openChat(clientId) {
  const render = async (first) => {
    const data = await api(`api/chats/${clientId}`);
    const c = data.client;
    if (first) {
      openSheet(`<div class="chat-sheet">
        <div class="sheet-head">${avatar({ id: c.id, name: c.name }, "big")}<div><h3>${esc(c.name)}</h3>
          <div class="sub">${c.phone ? `<a href="tel:${esc(c.phone)}">${esc(c.phone)}</a>` : "без телефона"}${c.username ? ` · @${esc(c.username)}` : ""}</div></div></div>
        ${data.next ? `<div class="chat-context">Ближайшая запись: ${data.next.start.slice(8, 10)}.${data.next.start.slice(5, 7)} в ${data.next.start.slice(11)} · ${esc(data.next.service)} · ${esc(data.next.master)}</div>` : ""}
        <button class="danger small chat-delete" id="chatDelete">Удалить переписку</button>
        <div class="thread" id="thread"></div>
        ${c.can_reply ? `<div class="composer"><textarea id="chatInput" rows="1" placeholder="Напишите ответ…"></textarea>
          <button id="chatSend" aria-label="Отправить"><svg viewBox="0 0 24 24"><path d="M5 12h13M13 6l6 6-6 6"/></svg></button></div>`
          : `<p class="muted small">Клиентка не подключена к боту — ответить можно только по телефону.</p>`}
      </div>`);
      $("chatDelete").onclick = async () => {
        if (!(await confirmBox(`Удалить всю переписку с ${c.name}? Её нельзя будет восстановить. Сам клиент и его записи останутся.`))) return;
        try {
          await api(`api/chats/${clientId}`, { method: "DELETE" });
          clearInterval(chatTimer); closeSheet(); toast("Переписка удалена"); loadChats();
        } catch (e) { fail(e); }
      };
      if (c.can_reply) {
        const input = $("chatInput");
        input.oninput = () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 120) + "px"; };
        $("chatSend").onclick = async () => {
          const text = input.value.trim();
          if (!text) return;
          $("chatSend").disabled = true;
          try { await api(`api/chats/${clientId}`, { method: "POST", body: { text } }); input.value = ""; input.style.height = "auto"; await render(false); }
          catch (e) { fail(e); }
          $("chatSend").disabled = false;
        };
      }
    }
    const thread = $("thread");
    if (!thread) return;
    const atBottom = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 40;
    let lastDay = "";
    thread.innerHTML = data.messages.map((m) => {
      const day = m.created_at.slice(0, 10);
      const sep = day !== lastDay ? `<div class="day-sep">${human(fromIso(day))}</div>` : "";
      lastDay = day;
      const body = (m.has_photo ? "📸 Фото" + (m.text ? "\n" : "") : "") + esc(m.text);
      return sep + `<div class="bubble ${m.direction}">${body}<span class="stamp">${m.direction === "out" && m.author ? esc(m.author) + " · " : ""}${m.created_at.slice(11, 16)}</span></div>`;
    }).join("");
    if (first || atBottom) thread.scrollTop = thread.scrollHeight;
  };
  try {
    await render(true);
    clearInterval(chatTimer);
    chatTimer = setInterval(() => { if ($("thread")) render(false).catch(() => {}); else clearInterval(chatTimer); }, 5000);
  } catch (e) { fail(e); }
}
$("sheetBg").addEventListener("click", () => { clearInterval(chatTimer); if (document.querySelector('#tabs button.active')?.dataset.tab === "chats") loadChats().catch(() => {}); });

// ---------- тема ----------
document.getElementById("themeBtn").onclick = () => {
  const dark = document.documentElement.dataset.theme === "dark";
  setThemePref(dark ? "light" : "dark");
  toast(dark ? "Светлая тема" : "Тёмная тема");
};
document.querySelectorAll("#themeSeg button").forEach((b) => (b.onclick = () => setThemePref(b.dataset.theme)));
renderThemeControls();

// ---------- старт ----------
(async () => {
  if (!AUTH) {
    document.body.innerHTML = `<div class="empty" style="margin:40px 16px"><b>Кабинет салона</b>Откройте кабинет из бота салона — кнопка «Открыть кабинет».</div>`;
    return;
  }
  try {
    await loadMe();
    buildTabs();
    [state.masters, state.services] = await Promise.all([api("api/masters"), api("api/services")]);
    if (isAdmin() && !state.masters.length) { showTab("masters"); toast("Начните с добавления мастеров"); }
    else await loadDay();
  } catch (e) {
    document.body.innerHTML = `<div class="empty" style="margin:40px 16px"><b>Нет доступа</b>${esc(e.message)}</div>`;
  }
})();


// ---------- отзывы ----------
const starLine = (r) => "★".repeat(r) + "☆".repeat(5 - r);
const fmtRating = (v) => String(v.avg).replace(".", ",");
async function loadReviews() {
  if (isAdmin() && !state.masters.length) await refreshMasters();
  const data = await api(`api/reviews?master_id=${isAdmin() ? (state.reviewMaster || 0) : 0}`);
  const ratings = data.ratings;
  const mine = !isAdmin() ? ratings[state.me.master_id] : null;
  const all = Object.values(ratings);
  const total = all.reduce((s, v) => s + v.n, 0);
  const avg = total ? (all.reduce((s, v) => s + v.avg * v.n, 0) / total).toFixed(1) : null;
  const head = isAdmin() ? (total ? { avg, n: total } : null) : mine;
  $("reviewSummary").innerHTML = head
    ? `<div class="card rv-head"><div class="rv-big">${String(head.avg).replace(".", ",")}</div><div><div class="rv-stars">${starLine(Math.round(head.avg))}</div>
       <div class="muted small">${head.n} ${plural(head.n, "отзыв", "отзыва", "отзывов")}${isAdmin() && !isSolo() ? " по салону" : ""}</div></div></div>`
    : "";
  if (isAdmin() && !isSolo()) {
    $("reviewChips").hidden = false;
    $("reviewChips").innerHTML = [`<button class="chip ${state.reviewMaster ? "" : "active"}" data-m="0">Все мастера</button>`]
      .concat(state.masters.map((m) => `<button class="chip ${state.reviewMaster === m.id ? "active" : ""}" data-m="${m.id}">${avatar(m)}${esc(m.name)}${ratings[m.id] ? ` · ★ ${fmtRating(ratings[m.id])}` : ""}</button>`)).join("");
    $("reviewChips").querySelectorAll(".chip").forEach((c) => (c.onclick = () => { state.reviewMaster = Number(c.dataset.m); loadReviews().catch(fail); }));
  } else $("reviewChips").hidden = true;
  const list = data.reviews;
  if (!list.length) {
    $("reviewList").innerHTML = `<div class="empty"><b>Отзывов пока нет</b>После визита бот сам попросит клиента оценить мастера — отзывы появятся здесь</div>`;
    return;
  }
  $("reviewList").innerHTML = list.map((r) => `
    <div class="card rv ${r.hidden ? "rv-hidden" : ""}">
      <div class="rv-top"><span class="rv-stars">${starLine(r.rating)}</span><span class="muted small">${r.date.slice(8, 10)}.${r.date.slice(5, 7)}</span></div>
      <div class="title">${esc(r.client)}${r.service ? ` <span class="muted small">· ${esc(r.service)}</span>` : ""}</div>
      ${isAdmin() && !isSolo() && !state.reviewMaster ? `<div class="muted small">Мастер ${esc(r.master)}</div>` : ""}
      ${r.text ? `<p class="rv-text">${esc(r.text)}</p>` : ""}
      ${r.photo ? `<a href="${esc(r.photo)}" target="_blank"><img class="rv-photo" src="${esc(r.photo)}" alt="Фото к отзыву" loading="lazy"></a>` : ""}
      ${r.reply ? `<div class="rv-reply"><b>${esc(r.reply_by || "Салон")}:</b> ${esc(r.reply)}</div>` : ""}
      ${r.hidden ? `<span class="tag accent">скрыт от клиентов</span>` : ""}
      <div class="row">
        <button class="ghost small" data-reply="${r.id}">${r.reply ? "Изменить ответ" : "Ответить"}</button>
        ${isAdmin() ? `<button class="ghost small" data-hide="${r.id}">${r.hidden ? "Показать" : "Скрыть"}</button>` : ""}
      </div>
    </div>`).join("");
  $("reviewList").querySelectorAll("[data-reply]").forEach((b) => (b.onclick = () => openReply(list.find((r) => r.id == b.dataset.reply))));
  $("reviewList").querySelectorAll("[data-hide]").forEach((b) => (b.onclick = async () => {
    try { const r = await api(`api/reviews/${b.dataset.hide}/hide`, { method: "POST" }); toast(r.hidden ? "Отзыв скрыт от клиентов" : "Отзыв снова виден"); loadReviews(); }
    catch (e) { fail(e); }
  }));
}
function openReply(r) {
  openSheet(`
    <h3>Ответ на отзыв</h3>
    <p class="muted small">${starLine(r.rating)} · ${esc(r.client)}${r.text ? `<br>«${esc(r.text)}»` : ""}</p>
    <label>Ваш ответ<textarea id="rvText" rows="4" maxlength="1000" placeholder="Спасибо, что выбрали нас! Ждём снова 🌸">${esc(r.reply || "")}</textarea></label>
    <p class="muted small">Ответ увидят все клиенты под отзывом, а автор получит его в мессенджер.</p>
    <button class="primary wide" id="rvSend">Опубликовать ответ</button>`);
  $("rvSend").onclick = async () => {
    const text = $("rvText").value.trim();
    if (!text) return fail(new Error("Напишите ответ"));
    try { await api(`api/reviews/${r.id}/reply`, { method: "POST", body: { text } }); closeSheet(); toast("Ответ опубликован"); loadReviews(); }
    catch (e) { fail(e); }
  };
}


// ---------- формат работы ----------
function renderMode() {
  const solo = state.me.mode === "solo";
  document.querySelectorAll("#modeSeg button").forEach((b) => b.classList.toggle("active", b.dataset.mode === state.me.mode));
  $("modeNote").textContent = solo
    ? "Вы принимаете клиентов сами: один график, без списка мастеров. Наняли мастеров — переключитесь на «Салон»."
    : "У вас салон: у каждого мастера свои услуги, график и записи.";
}
document.querySelectorAll("#modeSeg button").forEach((b) => (b.onclick = async () => {
  if (b.dataset.mode === state.me.mode) return;
  const toSolo = b.dataset.mode === "solo";
  if (!(await confirmBox(toSolo ? "Перейти на формат «Сам(а) на себя»? Вкладка «Мастера» скроется, график будет только ваш."
    : "Перейти на формат «Салон с мастерами»? Появится вкладка «Мастера» — там можно добавить сотрудников."))) return;
  try {
    await api("api/mode", { method: "POST", body: { mode: b.dataset.mode } });
    await loadMe(); await refreshMasters(); buildTabs(); showTab("salon"); toast("Готово");
  } catch (e) { fail(e); }
}));
