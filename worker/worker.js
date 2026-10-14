// Cloudflare Worker: общие отметки «проверено/отклонено» для страницы + Telegram-бот.
// Привязки: KV MARKS, секрет TELEGRAM_BOT_TOKEN.

const DATA_URL = "https://igorch2004.github.io/Updates_versions/data.json";
const PAGE_URL = "https://igorch2004.github.io/Updates_versions/";
const MAX_SHOWN = 10;      // сколько версий присылать за раз (лимит Telegram — не заваливать чат)
const NOTES_LEN = 600;     // обрезка «что нового» в сообщении бота

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "Content-Type",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
};

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    if (url.pathname.startsWith("/telegram/")) return telegram(req, env, url);

    if (req.method === "OPTIONS") return new Response(null, { headers: CORS });
    if (req.method === "GET") {
      const body = (await env.MARKS.get("marks")) || "{}";
      return new Response(body, { headers: { ...CORS, "Content-Type": "application/json" } });
    }
    if (req.method === "POST") {
      const { key, status } = await req.json();
      const marks = JSON.parse((await env.MARKS.get("marks")) || "{}");
      if (status) marks[key] = { status, at: new Date().toISOString() };
      else delete marks[key];
      await env.MARKS.put("marks", JSON.stringify(marks));
      return new Response("ok", { headers: CORS });
    }
    return new Response("not found", { status: 404, headers: CORS });
  },
};

// ---------- Telegram ----------

const BADGES = { pending: "⏳ Не проверено", checked: "✅ Проверено", rejected: "❌ Отклонено" };
const PERIODS = { day: "Сегодня", week: "Неделя", month: "Месяц", all: "За всё время" };
const PERIOD_DAYS = { day: 1, week: 7, month: 30, all: null };   // как на странице: сутки / 7 / 30 дней / без ограничения

async function telegram(req, env, url) {
  // адрес вебхука содержит токен, так что левые запросы сюда не пройдут
  if (url.pathname !== `/telegram/${env.TELEGRAM_BOT_TOKEN}` || req.method !== "POST")
    return new Response("not found", { status: 404 });
  const update = await req.json();
  const api = (method, body) => fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  try {
    if (update.callback_query) await onCallback(update.callback_query, api, env);
    else if (update.message && update.message.text) await onMessage(update.message, api, env);
  } catch (e) {
    console.error(e);
  }
  return new Response("ok");
}

const esc = s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const fmtDate = s => (s ? s.split("-").reverse().join(".") : "дата не указана");

async function loadData(env) {
  const res = await fetch(DATA_URL, { cf: { cacheTtl: 300 } });
  if (!res.ok) throw new Error("data.json " + res.status);
  return res.json();
}

async function botName(api, env) {
  const res = await (await api("getMe", {})).json();
  return res.result.username;
}

async function onMessage(msg, api, env) {
  const [cmd, arg] = msg.text.trim().split(/\s+/);
  if (cmd !== "/start" && cmd !== "/menu") return;
  const data = await loadData(env);
  // /start all или /start a3 — переход по ссылке из меню
  if (arg === "all") return askPeriod(msg.chat.id, "*", data, api);
  const m = /^a(\d+)$/.exec(arg || "");
  if (m && data.apps[+m[1]]) return askPeriod(msg.chat.id, data.apps[+m[1]].package, data, api);
  return sendMenu(msg.chat.id, data, api, env);
}

async function sendMenu(chatId, data, api, env) {
  const bot = await botName(api, env);
  const link = (arg, text) => `<a href="https://t.me/${bot}?start=${arg}">${esc(text)}</a>`;
  const text = "Привет! Какие версии ты хочешь посмотреть?\n\n"
    + link("all", "Все приложения") + "\n\n"
    + data.apps.map((a, i) => link("a" + i, a.name)).join("\n");
  await api("sendMessage", { chat_id: chatId, text, parse_mode: "HTML", disable_web_page_preview: true });
}

async function askPeriod(chatId, pkg, data, api) {
  const app = data.apps.find(a => a.package === pkg);
  const what = app ? app.name : "все приложения";
  await api("sendMessage", {
    chat_id: chatId,
    text: `${what}\n\nЗа какую дату хочешь посмотреть?`,
    reply_markup: { inline_keyboard: [
      ["day", "week", "month"].map(p => ({ text: PERIODS[p], callback_data: `r:${p}:${pkg}` })),
      [{ text: PERIODS.all, callback_data: `r:all:${pkg}` }],
    ] },
  });
}

async function onCallback(cb, api, env) {
  await api("answerCallbackQuery", { callback_query_id: cb.id });
  const chatId = cb.message.chat.id;
  if (cb.data === "m") return sendMenu(chatId, await loadData(env), api, env);
  const [kind, period, pkg] = cb.data.split(":");
  if (kind !== "r" || !(period in PERIOD_DAYS)) return;
  const data = await loadData(env);
  const marks = JSON.parse((await env.MARKS.get("marks")) || "{}");

  const dayStr = n => new Date(Date.now() + 3 * 3600e3 - n * 864e5).toISOString().slice(0, 10);   // по Москве
  const from = PERIOD_DAYS[period] ? dayStr(PERIOD_DAYS[period]) : "0000-01-01", to = dayStr(0);
  const list = data.releases
    .filter(r => (pkg === "*" || r.package === pkg) && (r.date || r.checked) >= from && (r.date || r.checked) <= to)
    .sort((a, b) => (b.date || b.checked).localeCompare(a.date || a.checked));
  const app = data.apps.find(a => a.package === pkg);
  const what = app ? app.name : "все приложения";

  if (!list.length) {
    await api("sendMessage", { chat_id: chatId, text: `${what}: за этот период (${PERIODS[period].toLowerCase()}) обновлений нет.`, reply_markup: menuButton() });
    return;
  }
  await api("sendMessage", { chat_id: chatId, text: `${what} — ${PERIODS[period].toLowerCase()}: найдено версий ${list.length}` });
  for (const r of list.slice(0, MAX_SHOWN)) {
    const key = `${r.package}|${r.version}`;
    const status = (marks[key] && marks[key].status) || "pending";   // у каждой версии виден статус; без отметки — «не проверено»
    const notes = (r.whats_new || "Описание не указано.").slice(0, NOTES_LEN) + ((r.whats_new || "").length > NOTES_LEN ? "…" : "");
    const text = `<b>${esc(r.name)}</b> — версия ${esc(r.version)} (${fmtDate(r.date)})\n`
      + `${BADGES[status]}\n\n${esc(notes)}\n\n`
      + `<a href="https://www.rustore.ru/catalog/app/${encodeURIComponent(r.package)}/versions">Страница в RuStore</a>`;
    await api("sendMessage", { chat_id: chatId, text, parse_mode: "HTML", disable_web_page_preview: true });
  }
  const more = list.length > MAX_SHOWN ? `Показано ${MAX_SHOWN} из ${list.length}. Все версии: ${PAGE_URL}` : "Это всё за выбранный период.";
  await api("sendMessage", { chat_id: chatId, text: more, disable_web_page_preview: true, reply_markup: menuButton() });
}

const menuButton = () => ({ inline_keyboard: [[{ text: "← В начало", callback_data: "m" }]] });
