"use strict";
// Пульт: главная как в макете 2 (слева разделы и проекты, справа задачи), карточка задачи справа.
// Данные — /api/state целиком (без описаний), карточка — /api/task/<номер> с журналом и вложениями,
// участники — /api/people (владельцу все, участнику он сам и его модели). Доска — те же задачи
// колонками по статусам: карточку перетаскивают в другую колонку (на телефоне — после долгого нажатия).
(() => {
  const CLOSED = new Set(["done", "dropped"]);
  const STATUS = {inbox: "Входящие", next: "Следующие", doing: "В работе", wait: "Ждёт", done: "Готово", dropped: "Снято"};
  const STATUS_ORDER = ["inbox", "next", "doing", "wait", "done", "dropped"];
  const OPEN_GROUPS = [["late", "Просрочено"], ["doing", "В работе"], ["wait", "Ждёт"], ["next", "Следующие"], ["inbox", "Входящие"]];
  const BOARD_COLS = [["inbox", "Входящие"], ["next", "Следующие"], ["doing", "В работе"], ["wait", "Ждёт"], ["done", "Готово"]];
  const RANK = {late: 0, doing: 1, wait: 2, next: 3, inbox: 4};
  const COLORS = ["c1", "c2", "c3", "c4", "c5", "c6", "artem", "claude", "codex", "ext"];
  const COLOR_SET = new Set(COLORS);
  const LEVEL = {view: "читает", edit: "правит"};
  const CLOSED_DAYS = 14;

  let S = null, etag = null, lastFetch = 0, gate = false;
  let PROJ = {}, AREA = {}, AREA_OF = {}, ACT = {}, ACC = {}, OWNER = false, LIMIT_FILE = 25 << 20;
  let route = {view: "all"}, openKey = null, card = null, mode = "view", newDefaults = null;
  let query = "", showAllClosed = false, logActor = "", passOpen = false, lastFocus = null;
  let cardSeq = 0, logSeq = 0, peopleSeq = 0, toastTimer = 0;
  let P = null, pcard = null, pmode = "view", pid = null, secret = null;
  let boardWho = "", drag = null, suppressClick = 0, waitEdit = null, aiEdit = null;
  let uploads = [], upSeq = 0, pumping = false, dropDepth = 0;

  const $ = id => document.getElementById(id);
  const pad = n => String(n).padStart(2, "0");
  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})[c]);
  const isoOf = d => d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  const today = () => isoOf(new Date());
  const addDays = (iso, n) => { const [y, m, d] = iso.split("-").map(Number); return isoOf(new Date(y, m - 1, d + n)); };
  const ddmm = iso => { if (!iso) return ""; const [y, m, d] = iso.split("-"); return d + "." + m + (Number(y) !== new Date().getFullYear() ? "." + y : ""); };
  const stamp = iso => {
    const d = new Date(iso);
    if (isNaN(d)) return String(iso || "");
    return pad(d.getDate()) + "." + pad(d.getMonth() + 1) + (d.getFullYear() !== new Date().getFullYear() ? "." + d.getFullYear() : "") + " " + pad(d.getHours()) + ":" + pad(d.getMinutes());
  };
  const dayOf = iso => { const d = new Date(iso); return isNaN(d) ? "" : isoOf(d); };
  const dayLabel = d => d === today() ? "Сегодня" : d === addDays(today(), -1) ? "Вчера" : ddmm(d);
  const plural = (n, one, few, many) => { const a = n % 10, b = n % 100; return a === 1 && b !== 11 ? one : a >= 2 && a <= 4 && (b < 12 || b > 14) ? few : many; };
  const fsize = n => n < 1024 ? n + " Б" : n < 1048576 ? Math.round(n / 1024) + " КБ" : (n / 1048576).toFixed(1).replace(".", ",") + " МБ";

  // Поиск не различает «е» и «ё», а русские буквы-двойники латинских считает одинаковыми: «1С-7» найдёт 1C-7.
  const FOLD = {"а": "a", "в": "b", "с": "c", "е": "e", "н": "h", "к": "k", "м": "m", "о": "o", "р": "p", "т": "t", "х": "x", "у": "y", "ё": "e"};
  const norm = s => String(s || "").toLowerCase().replace(/[авсенкмортхуё]/g, c => FOLD[c]);
  const terms = q => norm(q).split(/\s+/).filter(Boolean);
  const LOOK = {"А": "A", "В": "B", "С": "C", "Е": "E", "Н": "H", "К": "K", "М": "M", "О": "O", "Р": "P", "Т": "T", "Х": "X", "У": "Y"};
  const normKey = k => String(k || "").trim().toUpperCase().replace(/[АВСЕНКМОРТХУ]/g, c => LOOK[c]);

  const isOpen = t => !CLOSED.has(t.status);
  const isLate = t => isOpen(t) && !!t.due && t.due < today();
  const isCheck = t => isOpen(t) && !!t.check && t.check <= today();
  const lampOf = t => isLate(t) ? "late" : t.status;
  const recent = t => !isOpen(t) && !!t.done_on && t.done_on >= addDays(today(), -CLOSED_DAYS);
  const weekDone = t => t.status === "done" && !!t.done_on && t.done_on >= addDays(today(), -7);
  const stepText = t => lampOf(t) === "wait" && t.wait ? "ждём: " + t.wait : (t.next || "");
  const dkey = t => t.due || t.check || "9999-12-31";
  const byOpen = (x, y) => dkey(x).localeCompare(dkey(y)) || String(y.updated_at).localeCompare(String(x.updated_at));
  // на доске внутри колонки: просроченное, потом то, что пора проверить, потом по ближайшей дате
  const boardRank = t => isLate(t) ? 0 : isCheck(t) ? 1 : 2;
  const byBoard = (x, y) => (boardRank(x) - boardRank(y)) || byOpen(x, y);
  const byClosed = (x, y) => String(y.done_on || "").localeCompare(String(x.done_on || "")) || String(y.updated_at).localeCompare(String(x.updated_at));
  const canEdit = projectId => ACC[projectId] === "edit";
  const writersOf = projectId => new Set((PROJ[projectId] && PROJ[projectId].writers) || []);

  const LOCK = '<svg class="ico" viewBox="0 0 16 16" role="img" aria-label="закрытый"><path d="M5 7V5.5a3 3 0 0 1 6 0V7" fill="none" stroke="currentColor" stroke-width="1.6"/><rect x="3.5" y="7" width="9" height="7" rx="1.6" fill="currentColor"/></svg>';
  const SHARED = '<svg class="ico" viewBox="0 0 16 16" role="img" aria-label="общий"><circle cx="6" cy="5.5" r="2.3" fill="none" stroke="currentColor" stroke-width="1.5"/><circle cx="11.2" cy="6.5" r="1.8" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M1.8 13.5c.5-2.2 2.1-3.4 4.2-3.4s3.7 1.2 4.2 3.4M10 10.3c.4-.2.8-.3 1.2-.3 1.5 0 2.6.9 3 2.6" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>';
  const ICON_GRID = '<svg class="vi" viewBox="0 0 16 16" aria-hidden="true"><rect x="2" y="2" width="5" height="5" rx="1.2" fill="none" stroke="currentColor" stroke-width="1.4"/><rect x="9" y="2" width="5" height="5" rx="1.2" fill="none" stroke="currentColor" stroke-width="1.4"/><rect x="2" y="9" width="5" height="5" rx="1.2" fill="none" stroke="currentColor" stroke-width="1.4"/><rect x="9" y="9" width="5" height="5" rx="1.2" fill="none" stroke="currentColor" stroke-width="1.4"/></svg>';
  const ICON_LIST = '<svg class="vi" viewBox="0 0 16 16" aria-hidden="true"><path d="M6 4h8M6 8h8M6 12h8" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/><circle cx="3" cy="4" r="1" fill="currentColor"/><circle cx="3" cy="8" r="1" fill="currentColor"/><circle cx="3" cy="12" r="1" fill="currentColor"/></svg>';
  const ICON_LOG = '<svg class="vi" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="5.8" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="M8 4.8V8l2.2 1.4" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>';
  const ICON_PEOPLE = '<svg class="vi" viewBox="0 0 16 16" aria-hidden="true"><circle cx="6" cy="5.5" r="2.3" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="M1.8 13.5c.5-2.2 2.1-3.4 4.2-3.4s3.7 1.2 4.2 3.4" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/><circle cx="11.4" cy="6.3" r="1.8" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="M11.1 10.2c1.6 0 2.8.9 3.2 2.7" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>';
  const ICON_BOARD = '<svg class="vi" viewBox="0 0 16 16" aria-hidden="true"><rect x="1.8" y="2.3" width="3.6" height="11.4" rx="1.1" fill="none" stroke="currentColor" stroke-width="1.4"/><rect x="6.2" y="2.3" width="3.6" height="7.6" rx="1.1" fill="none" stroke="currentColor" stroke-width="1.4"/><rect x="10.6" y="2.3" width="3.6" height="9.4" rx="1.1" fill="none" stroke="currentColor" stroke-width="1.4"/></svg>';
  const CLIP = '<svg class="ico" viewBox="0 0 16 16" aria-hidden="true"><path d="M10.6 5.4l-4.7 4.7a1.4 1.4 0 0 0 2 2l5-5a2.8 2.8 0 0 0-4-4l-5 5a4.2 4.2 0 0 0 6 6l4-4" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const MSG = '<svg class="ico" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 3h10a1.2 1.2 0 0 1 1.2 1.2v5.6A1.2 1.2 0 0 1 13 11H7.4L4.2 13.6V11H3a1.2 1.2 0 0 1-1.2-1.2V4.2A1.2 1.2 0 0 1 3 3z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>';
  const FILEICON = '<svg class="fico" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 1.8h5l3.2 3.2v9.2H4z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/><path d="M9 1.8V5h3.2" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>';
  const XICON = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 3.5l9 9M12.5 3.5l-9 9" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>';

  const QUICK = [
    ["mine", "Мои задачи", "Задачи, которые держите вы", t => !!S.me && t.holder === S.me.id],
    ["models", "На моделях", "Задачи, которые держат модели", t => !!t.holder && !!ACT[t.holder] && ACT[t.holder].kind === "model"],
    ["late", "Просрочено", "Срок прошёл, задача не закрыта", isLate],
    ["check", "Пора проверить", "Наступила дата проверки: пора посмотреть, пришло ли то, чего ждём", isCheck],
    ["wait", "Ждут ответа", "Ждём человека, конвейер или решение", t => isOpen(t) && t.status === "wait"]
  ];

  // ---------- связь с сервером ----------

  class ApiErr extends Error {
    constructor(status, message, data) { super(message); this.status = status; this.data = data; }
  }

  async function api(method, path, body) {
    const opt = {method, headers: {"X-Pult": "1"}, credentials: "same-origin", cache: "no-store"};
    if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
    let r;
    try { r = await fetch(path, opt); } catch (e) { throw new ApiErr(0, "Нет связи с Пультом. Проверьте сеть и повторите."); }
    let data = null;
    try { data = await r.json(); } catch (e) { data = null; }
    if (r.status === 401) { setGate(); throw new ApiErr(401, (data && data.error) || "Нужен вход", data); }
    if (!r.ok) throw new ApiErr(r.status, (data && data.error) || ("Пульт ответил ошибкой " + r.status), data);
    return data;
  }

  async function loadState(force) {
    const headers = {"X-Pult": "1"};
    if (etag && !force) headers["If-None-Match"] = etag;
    let r;
    try { r = await fetch("/api/state", {headers, credentials: "same-origin", cache: "no-store"}); }
    catch (e) { throw new ApiErr(0, "Нет связи с Пультом. Проверьте сеть и обновите страницу."); }
    lastFetch = Date.now();
    if (r.status === 304) return false;
    if (r.status === 401) { setGate(); return false; }
    if (!r.ok) throw new ApiErr(r.status, "Пульт ответил ошибкой " + r.status);
    S = await r.json();
    etag = r.headers.get("ETag");
    gate = false;
    index();
    return true;
  }

  function index() {
    PROJ = {}; AREA = {}; AREA_OF = {}; ACT = {};
    ACC = S.access || {};
    OWNER = !!S.owner;
    if (S.limits && S.limits.file) LIMIT_FILE = S.limits.file;
    S.actors.forEach(a => { ACT[a.id] = a; });
    S.areas.forEach(a => { AREA[a.id] = Object.assign({}, a, {projects: []}); });
    S.projects.forEach(p => {
      PROJ[p.id] = p;
      const a = AREA[p.area];
      if (a) { a.projects.push(p); AREA_OF[p.id] = a; }
    });
    $("new-task").hidden = gate || !Object.values(ACC).includes("edit");
  }
  const areas = () => S.areas.map(a => AREA[a.id]);
  const visibleProjects = a => a.projects.filter(p => !p.archived);
  // Журнал и участники грузятся отдельно — их не перерисовываем на каждое изменение задач;
  // доску не перерисовываем, пока по ней тащат карточку
  const liveMain = () => route.view !== "log" && route.view !== "people" && !(drag && drag.on) && !aiEdit;

  function applyTask(task) {
    const lite = Object.assign({}, task, {has_notes: !!task.notes});
    delete lite.notes;
    const i = S.tasks.findIndex(x => x.key === task.key);
    if (i >= 0) S.tasks[i] = lite; else S.tasks.push(lite);
    etag = null;
    renderSide();
    if (liveMain()) renderMain();
  }

  async function refresh(force) {
    try {
      if (!await loadState(force) || gate) return;
      renderTeam();
      renderSide();
      if (liveMain()) renderMain();
      if (card && mode === "view" && !$("drawer").hidden) {
        const t = S.tasks.find(x => x.key === card.task.key);
        const busy = [...$("drawer").querySelectorAll("textarea, input.inp")].some(x => x.value.trim() || x === document.activeElement);
        if (t && t.version !== card.task.version && !busy) reloadCard();
      }
    } catch (e) { /* тихо: следующая попытка через минуту */ }
  }

  // ---------- мелкие части разметки ----------

  const actorName = id => id ? (ACT[id] ? ACT[id].name : id) : "никто";
  const nameOf = id => { const u = P && P.people.find(x => x.id === id); return u ? u.name : actorName(id); };
  const projName = id => PROJ[id] ? PROJ[id].name : id;
  function abbr(a) { const n = a.name || a.id; return a.kind === "model" ? n.slice(0, 2) : n.slice(0, 1).toUpperCase(); }
  function avA(a, extra) {
    const cls = ["av", COLOR_SET.has(a.color) ? a.color : "c6", a.kind === "model" ? "model" : "", extra || ""].filter(Boolean).join(" ");
    return '<span class="' + cls + '" title="' + esc(a.name + (a.kind === "model" ? ", модель" : ", человек")) + '">' + esc(abbr(a)) + '</span>';
  }
  function av(id, extra) {
    if (!id) return '<span class="av none" title="Никто не держит">–</span>';
    return avA(ACT[id] || {id, name: id, kind: "person", color: "c6"}, extra);
  }
  const icons = (a, p) => (p.closed && !(a && a.closed) ? LOCK : "") + (p.shared ? SHARED : "");
  function dateChip(t) {
    if (!isOpen(t) && t.done_on) return '<span class="chipdate' + (t.status === "done" ? " done" : "") + '" title="Когда закрыта">' + (t.status === "dropped" ? "снято " : "готово ") + ddmm(t.done_on) + '</span>';
    if (isLate(t)) return '<span class="chipdate late" title="Срок прошёл">просрочено, ' + ddmm(t.due) + '</span>';
    if (isCheck(t)) return '<span class="chipdate check" title="Пора проверить, пришло ли то, чего ждём">проверить ' + ddmm(t.check) + '</span>';
    if (t.due) return '<span class="chipdate" title="Срок">срок ' + ddmm(t.due) + '</span>';
    if (t.check) return '<span class="chipdate" title="Когда проверить, пришло ли то, чего ждём">проверить ' + ddmm(t.check) + '</span>';
    return "";
  }
  function linkify(text) {
    return esc(text).replace(/https?:\/\/[^\s<>"']*[^\s<>"'.,;:!?)\]»]/g, u => '<a href="' + u + '" target="_blank" rel="noopener noreferrer">' + u + '</a>');
  }
  function hl(text, ts) {
    text = String(text || "");
    if (!ts.length) return esc(text);
    const n = norm(text), r = [];
    ts.forEach(w => { let i = 0, j; while ((j = n.indexOf(w, i)) !== -1) { r.push([j, j + w.length]); i = j + w.length; } });
    if (!r.length) return esc(text);
    r.sort((x, y) => x[0] - y[0]);
    const m = [r[0].slice()];
    r.slice(1).forEach(x => { const last = m[m.length - 1]; if (x[0] <= last[1]) last[1] = Math.max(last[1], x[1]); else m.push(x.slice()); });
    let out = "", i = 0;
    m.forEach(([s, e]) => { out += esc(text.slice(i, s)) + "<mark>" + esc(text.slice(s, e)) + "</mark>"; i = e; });
    return out + esc(text.slice(i));
  }
  function matches(t, ts) {
    const p = PROJ[t.project], a = AREA_OF[t.project];
    const hay = norm([t.key, t.title, a ? a.name : "", p ? p.name : "", t.next, t.wait, t.prompt, t.holder ? actorName(t.holder) : ""].join(" "));
    return ts.every(w => hay.includes(w));
  }

  // Модель и усилие для сессии ИИ по карточке: карточка → проект → раздел → весь Пульт, нижний уровень главнее.
  const AI_FROM = {task: "в карточке", project: "из проекта", area: "из раздела", pult: "для всего Пульта"};
  const AI_ABOVE = {task: "как у проекта", project: "как у раздела", area: "как у Пульта", pult: ""};
  const AI_LISTS = '<datalist id="ai-models">' + ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"].map(v => '<option value="' + v + '">').join("") +
    '</datalist><datalist id="ai-efforts">' + ["low", "medium", "high", "xhigh", "max"].map(v => '<option value="' + v + '">').join("") + '</datalist>';
  function aiChain(kind, x) {
    const pult = ["pult", S.ai || {}];
    if (kind === "pult") return [pult];
    if (kind === "area") return [["area", x], pult];
    if (kind === "project") return [["project", x]].concat(aiChain("area", x && AREA_OF[x.id]));
    return [["task", x]].concat(aiChain("project", PROJ[x.project] || null));
  }
  function aiPick(levels) {
    const out = {};
    ["model", "effort"].forEach(k => {
      const hit = levels.find(l => l[1] && l[1][k]);
      out[k] = hit ? hit[1][k] : "";
      out[k + "From"] = hit ? hit[0] : "";
    });
    return out;
  }
  function aiHTML(ai) {
    return ["model", "effort"].filter(k => ai[k]).map(k => '<span>' + (k === "model" ? "модель " : "усилие ") + '<span class="mono">' + esc(ai[k]) + '</span> <span class="muted">' + AI_FROM[ai[k + "From"]] + '</span></span>').join("");
  }
  // поля модели и усилия; в подсказке — что достанется с уровня выше, если оставить пустым
  function aiInputs(kind, own, above) {
    const ph = (k, none) => above[k] ? (AI_ABOVE[kind] + ": " + above[k]) : none;
    return '<div class="row2"><label class="f"><span>Модель ИИ</span><input class="inp mono" name="model" list="ai-models" maxlength="60" value="' + esc(own.model || "") + '" placeholder="' + esc(ph("model", "не задана")) + '" autocomplete="off" spellcheck="false"></label>' +
      '<label class="f"><span>Усилие ИИ</span><input class="inp mono" name="effort" list="ai-efforts" maxlength="60" value="' + esc(own.effort || "") + '" placeholder="' + esc(ph("effort", "не задано")) + '" autocomplete="off" spellcheck="false"></label></div>' + AI_LISTS;
  }
  const scopeKey = r => r.view === "all" ? "pult" : r.view === "a" || r.view === "p" ? r.view + ":" + r.id : null;
  // строка «ИИ: …» в шапке раздела, проекта или всех задач; владелец меняет её на месте
  function aiHead(kind, x) {
    const key = kind === "pult" ? "pult" : (kind === "area" ? "a:" : "p:") + x.id;
    const own = kind === "pult" ? (S.ai || {}) : x;
    if (aiEdit === key && OWNER) {
      return '<form class="aiform" data-form="ai" data-scope="' + esc(key) + '" novalidate>' + aiInputs(kind, own, aiPick(aiChain(kind, x).slice(1))) +
        '<div class="formbar"><button class="btn primary small" type="submit">Сохранить</button><button class="btn small" type="button" data-aicancel>Отмена</button></div></form>';
    }
    const ai = aiPick(aiChain(kind, x)), shown = aiHTML(ai);
    if (!shown && !OWNER) return "";
    return '<p class="ai-line"><span class="muted">ИИ для сессий по карточкам:</span>' + (shown || '<span class="muted">не задано</span>') +
      (OWNER ? '<button class="linkbtn" type="button" data-aiedit="' + esc(key) + '">изменить</button>' : "") + '</p>';
  }
  // «Кто держит»: те, кто правит проект; владелец может назначить и остальных — с пометкой
  function holderChoices(projectId, current) {
    const w = writersOf(projectId);
    return S.actors.filter(a => (a.active && (OWNER || w.has(a.id))) || a.id === current).map(a => ({a, noacc: !w.has(a.id)}));
  }

  // ---------- левая колонка ----------

  function renderTeam() {
    const team = S.actors.filter(a => a.active && a.id !== S.me.id).map(a => av(a.id)).join("");
    $("team").innerHTML = team + '<button class="mebtn" type="button" data-me aria-haspopup="true" title="Вы вошли как ' + esc(S.me.name) + '">' + av(S.me.id) + '<span class="nm">' + esc(S.me.name) + '</span></button>';
  }

  function listFor(r) {
    const T = S.tasks;
    if (r.view === "q") { const q = QUICK.find(x => x[0] === r.id); return q ? T.filter(q[3]) : []; }
    if (r.view === "a") return T.filter(t => AREA_OF[t.project] && AREA_OF[t.project].id === r.id);
    if (r.view === "p") return T.filter(t => t.project === r.id);
    return T.slice();
  }

  function renderSide() {
    if (!S || gate) { $("side").innerHTML = ""; return; }
    const cnt = l => l.filter(isOpen).length;
    const expanded = route.view === "a" ? route.id : route.view === "p" && AREA_OF[route.id] ? AREA_OF[route.id].id : null;
    const item = (view, id, label, n, cls) => '<button class="side-item' + (cls ? " " + cls : "") + '" type="button" data-nav="' + view + (id ? ":" + esc(id) : "") + '" aria-current="' + (route.view === view && (!id || route.id === id)) + '">' + label + '<span class="n">' + n + '</span></button>';
    let h = item("overview", "", ICON_GRID + "Обзор проектов", "") + item("all", "", ICON_LIST + "Все задачи", cnt(S.tasks)) + item("board", "", ICON_BOARD + "Доска", "") + item("log", "", ICON_LOG + "Журнал", "") + item("people", "", ICON_PEOPLE + "Участники", "");
    h += '<div class="side-group">Подборки</div>';
    QUICK.forEach(([id, label]) => { h += item("q", id, label, cnt(listFor({view: "q", id})) || ""); });
    h += '<div class="side-group">Проекты</div>';
    areas().forEach(a => {
      h += item("a", a.id, esc(a.name) + (a.closed ? LOCK : ""), cnt(listFor({view: "a", id: a.id})) || "");
      if (expanded === a.id) visibleProjects(a).forEach(p => {
        h += item("p", p.id, esc(p.name) + icons(a, p), cnt(listFor({view: "p", id: p.id})) || "", "sub" + (p.paused ? " paused" : ""));
      });
    });
    $("side").innerHTML = h;
  }

  // ---------- правая часть ----------

  function scopeName(r) {
    if (r.view === "q") { const q = QUICK.find(x => x[0] === r.id); return "в подборке «" + (q ? q[1] : r.id) + "»"; }
    if (r.view === "a") return "в разделе «" + (AREA[r.id] ? AREA[r.id].name : r.id) + "»";
    if (r.view === "p") return "в проекте «" + (PROJ[r.id] ? PROJ[r.id].name : r.id) + "»";
    return "во всех задачах";
  }

  function headFor(r, list) {
    const openN = list.filter(isOpen).length, closedN = list.length - openN;
    if (r.view === "q") {
      const q = QUICK.find(x => x[0] === r.id) || [r.id, r.id, ""];
      return '<header class="mhead"><div class="crumbs">Подборка</div><h1>' + esc(q[1]) + '</h1><p>' + esc(q[2]) + '</p></header>';
    }
    if (r.view === "a") {
      const a = AREA[r.id];
      if (!a) return '<header class="mhead"><h1>Нет такого раздела</h1></header>';
      const k = visibleProjects(a).length;
      return '<header class="mhead"><div class="crumbs">Раздел</div><h1>' + esc(a.name) + '</h1><p>' + k + ' ' + plural(k, "проект", "проекта", "проектов") + ' · открытых задач ' + openN + (a.closed ? " · закрытый раздел" : "") + '</p>' + aiHead("area", a) + '</header>';
    }
    if (r.view === "p") {
      const p = PROJ[r.id];
      if (!p) return '<header class="mhead"><h1>Нет такого проекта</h1></header>';
      const a = AREA_OF[p.id], bits = [p.paused ? "пауза с " + ddmm(p.paused) : p.note].filter(Boolean);
      if (p.closed || (a && a.closed)) bits.push("закрытый проект");
      if (p.shared) bits.push("общий: " + p.shared);
      if (!canEdit(p.id)) bits.push("у вас — только чтение");
      const refs = [p.mem ? "память: " + p.mem : "", p.path ? "папка: " + p.path : ""].filter(Boolean).join(" · ");
      return '<header class="mhead"><div class="mhead-row"><div class="mhead"><div class="crumbs">' + (a ? '<button class="linkbtn" type="button" data-nav="a:' + esc(a.id) + '">' + esc(a.name) + '</button>' : "") + '</div><h1>' + esc(p.name) + '</h1><p>' + esc(bits.join(" · ")) + '</p>' + (refs ? '<p class="mono">' + esc(refs) + '</p>' : "") + aiHead("project", p) + '</div>' +
        '<div class="actions"><button class="btn small" type="button" data-board="p:' + esc(p.id) + '">' + ICON_BOARD + 'Доской</button>' + (canEdit(p.id) ? '<button class="btn small" type="button" data-new="' + esc(p.id) + '">+ Задача в проект</button>' : "") + '</div></div></header>';
    }
    return '<header class="mhead"><div class="crumbs">Все проекты</div><h1>Все задачи</h1><p>Открытых ' + openN + ', закрытых ' + closedN + '. Сверху то, что требует внимания.</p>' + (r.view === "all" ? aiHead("pult", null) : "") + '</header>';
  }

  function trow(t, ts) {
    const p = PROJ[t.project], a = AREA_OF[t.project], st = stepText(t);
    const where = (a ? a.name + " › " : "") + (p ? p.name : t.project);
    const extra = (t.prompt ? " · сообщение для ИИ" : "") +
      (t.files ? " · " + t.files + " " + plural(t.files, "вложение", "вложения", "вложений") : "");
    return '<button class="trow' + (t.key === openKey ? " on" : "") + '" type="button" data-task="' + esc(t.key) + '"><span class="tid">' + hl(t.key, ts) + '</span>' +
      '<span class="tt"><span class="t">' + hl(t.title, ts) + '</span><span class="meta">' + hl(where + (st ? " · " + st : ""), ts) + esc(extra) + '</span></span>' +
      '<span class="dslot">' + dateChip(t) + '</span>' + av(t.holder) + '</button>';
  }
  const group = (k, label, items, ts) => '<section class="group"><div class="gtitle"><i class="lamp ' + k + '"></i>' + esc(label) + '<span class="n">' + items.length + '</span></div>' + items.map(t => trow(t, ts)).join("") + '</section>';
  const noAccess = () => '<p class="empty-main">Вам пока не выдан доступ ни к одному проекту. Доступ выдаёт владелец Пульта.</p>';

  function renderMain() {
    const main = $("main");
    main.classList.toggle("wide", !gate && route.view === "board");
    if (gate) { main.innerHTML = gateHTML(); return; }
    if (!S) return;
    if (route.view === "people") { renderPeople(); return; }
    if (route.view === "overview") { main.innerHTML = overviewHTML(); return; }
    if (route.view === "log") { renderLog(); return; }
    if (route.view === "board") { renderBoard(); return; }
    const list = listFor(route), ts = terms(query);
    const found = ts.length ? list.filter(t => matches(t, ts)) : list;
    let body = "", shown = 0;
    OPEN_GROUPS.forEach(([k, label]) => {
      const items = found.filter(t => isOpen(t) && lampOf(t) === k).sort(byOpen);
      if (!items.length) return;
      shown += items.length;
      body += group(k, label, items, ts);
    });
    const closedAll = found.filter(t => !isOpen(t)).sort(byClosed);
    const full = showAllClosed || ts.length > 0;
    const closedShown = full ? closedAll : closedAll.filter(recent);
    if (closedShown.length) {
      shown += closedShown.length;
      body += group("done", full ? "Закрыто" : "Закрыто за две недели", closedShown, ts);
    }
    const more = closedAll.length > closedShown.length ? '<button class="linkbtn more" type="button" data-more-closed>Показать все закрытые (' + closedAll.length + ')</button>' : "";
    let h = headFor(route, list);
    if (ts.length) h += '<div class="qbar"><span>Найдено ' + shown + ' ' + plural(shown, "задача", "задачи", "задач") + ' по запросу «' + esc(query.trim()) + '» ' + esc(scopeName(route)) + '</span><button class="linkbtn" type="button" data-reset>Сбросить поиск</button></div>';
    if (!shown) body = ts.length
      ? '<p class="empty-main">Ничего не нашлось. Поиск смотрит в номер задачи, название, раздел и проект, следующий шаг, поле «ждём» и имя того, кто держит задачу.</p>'
      : !S.projects.length ? noAccess() : '<p class="empty-main">Здесь пока нет открытых задач.</p>';
    main.innerHTML = h + body + more;
  }

  function overviewHTML() {
    const open = S.tasks.filter(isOpen);
    const cnt = k => open.filter(t => lampOf(t) === k).length;
    const stat = (k, n, label, nav) => '<button class="stat" type="button"' + (nav ? ' data-nav="' + nav + '"' : "") + '><i class="lamp ' + k + '"></i><b>' + n + '</b>' + label + '</button>';
    let h = '<header class="mhead"><div class="crumbs">Все проекты</div><h1>Обзор проектов</h1><p>По строке на проект: что в работе, что ждёт, ближайший шаг и кто его держит. Нажмите на строку, чтобы открыть проект.</p></header>';
    if (!S.projects.length) return h + noAccess();
    h += '<div class="summary">' + stat("late", cnt("late"), "просрочено", "q:late") + stat("check", open.filter(isCheck).length, "пора проверить", "q:check") + stat("doing", cnt("doing"), "в работе", "all") +
      stat("wait", cnt("wait"), "ждёт", "q:wait") + stat("next", cnt("next"), "следующие", "all") + stat("inbox", cnt("inbox"), "входящие", "all") + stat("done", S.tasks.filter(weekDone).length, "готово за неделю", "all") + '</div>';
    h += '<div class="ov">';
    areas().forEach(a => {
      const ps = visibleProjects(a);
      if (!ps.length) return;
      h += '<div class="areahead">' + esc(a.name) + (a.closed ? LOCK : "") + '</div>';
      ps.forEach(p => {
        const openP = S.tasks.filter(t => t.project === p.id && isOpen(t));
        const u = openP.slice().sort((x, y) => (RANK[lampOf(x)] - RANK[lampOf(y)]) || byOpen(x, y))[0];
        const c = k => openP.filter(t => lampOf(t) === k).length;
        const counts = [["late", "просрочено"], ["doing", "в работе"], ["wait", "ждёт"]].map(([k, l]) => c(k) ? '<span><i class="lamp ' + k + '"></i>' + l + ' ' + c(k) + '</span>' : "").join("") +
          (c("next") + c("inbox") ? '<span><i class="lamp next"></i>дальше ' + (c("next") + c("inbox")) + '</span>' : "");
        const name = '<span class="pname"><span class="pn">' + esc(p.name) + icons(a, p) + '</span><span class="sub">' + esc(p.paused ? "пауза с " + ddmm(p.paused) : p.note) + '</span></span>';
        const step = u
          ? '<span class="nstep"><span class="nt"><span class="tid">' + esc(u.key) + '</span> ' + esc(u.title) + '</span><span><span class="arr">→</span> ' + esc(stepText(u) || "следующий шаг не записан") + '</span></span>'
          : '<span class="nstep none">открытых задач нет</span>';
        h += '<button class="prow" type="button" data-nav="p:' + esc(p.id) + '">' + name + '<span class="counts">' + counts + '</span>' + step + '<span class="holder">' + (u ? dateChip(u) + av(u.holder) : "") + '</span></button>';
      });
    });
    return h + '</div>';
  }

  // ---------- доска ----------

  function splitScope(id) {
    const i = String(id || "").indexOf(":");
    return i < 0 ? null : {view: id.slice(0, i), id: id.slice(i + 1)};
  }

  function boardList() {
    const sc = splitScope(route.id), w = boardWho;
    let list = listFor(sc && (sc.view === "a" || sc.view === "p") ? sc : {view: "all"});
    if (w === "me") list = list.filter(t => !!S.me && t.holder === S.me.id);
    else if (w === "none") list = list.filter(t => !t.holder);
    else if (w === "models") list = list.filter(t => !!t.holder && !!ACT[t.holder] && ACT[t.holder].kind === "model");
    else if (w) list = list.filter(t => t.holder === w);
    return list;
  }

  function bcard(t, ts, oneProject) {
    const p = PROJ[t.project];
    const ask = t.prompt ? '<span title="Сообщение для ИИ">' + MSG + 'ИИ</span>' : "";
    const files = t.files ? '<span title="Вложения">' + CLIP + t.files + '</span>' : "";
    return '<button class="bcard' + (isLate(t) ? " late" : "") + (isOpen(t) ? "" : " closed") + (t.key === openKey ? " on" : "") + '" type="button" data-task="' + esc(t.key) + '"' + (canEdit(t.project) ? " data-drag" : "") + '>' +
      '<span class="bc-top"><span class="tid">' + hl(t.key, ts) + '</span>' + dateChip(t) + '</span>' +
      '<span class="bc-t">' + hl(t.title, ts) + '</span>' +
      (oneProject ? "" : '<span class="bc-p">' + hl((AREA_OF[t.project] ? AREA_OF[t.project].name + " › " : "") + (p ? p.name : t.project), ts) + '</span>') +
      '<span class="bc-foot">' + ask + files + av(t.holder) + '</span></button>';
  }

  function renderBoard() {
    const sc = splitScope(route.id), ts = terms(query), oneProject = !!sc && sc.view === "p";
    const opt = (v, label, cur) => '<option value="' + esc(v) + '"' + (v === cur ? " selected" : "") + '>' + esc(label) + '</option>';
    const scopes = opt("", "Все проекты", route.id || "") + areas().map(a => {
      const ps = visibleProjects(a);
      return ps.length ? '<optgroup label="' + esc(a.name) + '">' + opt("a:" + a.id, "Весь раздел «" + a.name + "»", route.id) + ps.map(p => opt("p:" + p.id, p.name, route.id)).join("") + '</optgroup>' : "";
    }).join("");
    const whos = [["", "Держит: все"], ["me", "Держу я"], ["none", "Никто не держит"], ["models", "Держат модели"]]
      .concat(S.actors.filter(a => a.active && a.id !== S.me.id).map(a => [a.id, "Держит: " + a.name]))
      .map(([v, l]) => opt(v, l, boardWho)).join("");
    let list = boardList();
    if (ts.length) list = list.filter(t => matches(t, ts));
    const anyEdit = Object.values(ACC).includes("edit");
    const scopeLabel = !sc ? "Все проекты" : sc.view === "a" ? (AREA[sc.id] ? AREA[sc.id].name : sc.id) : (PROJ[sc.id] ? PROJ[sc.id].name : sc.id);
    let h = '<header class="mhead"><div class="crumbs">' + esc(scopeLabel) + '</div><h1>Доска</h1><p>' + (anyEdit
      ? 'Задачи по статусам. Чтобы сменить статус, перетащите карточку в другую колонку<span class="touch-only"> — нажмите на неё и подержите</span>.'
      : "Задачи по статусам. У вас доступ только на чтение.") + '</p></header>';
    h += '<div class="bfilters"><select class="sel" data-bscope aria-label="Какие проекты">' + scopes + '</select><select class="sel" data-bwho aria-label="Кто держит">' + whos + '</select></div>';
    if (ts.length) h += '<div class="qbar"><span>Найдено ' + list.filter(t => isOpen(t) || recent(t)).length + ' по запросу «' + esc(query.trim()) + '»</span><button class="linkbtn" type="button" data-reset>Сбросить поиск</button></div>';
    if (!S.projects.length) { $("main").innerHTML = h + noAccess(); return; }
    h += '<div class="board" id="board">';
    BOARD_COLS.forEach(([k, label]) => {
      const items = k === "done" ? list.filter(recent).sort(byClosed) : list.filter(t => t.status === k).sort(byBoard);
      h += '<section class="bcol" data-col="' + k + '" aria-label="' + label + '"><div class="bhead"><i class="lamp ' + k + '"></i>' + label + (k === "done" ? '<span class="hint">за две недели</span>' : "") + '<span class="n">' + items.length + '</span></div>' +
        (items.length ? items.map(t => bcard(t, ts, oneProject)).join("") : '<p class="bempty">Пусто</p>') + '</section>';
    });
    $("main").innerHTML = h + '</div>';
  }

  async function moveTask(key, status) {
    const t = S.tasks.find(x => x.key === key);
    if (!t || t.status === status || !canEdit(t.project)) return;
    const needWait = status === "wait" && !t.wait, before = Object.assign({}, t);
    t.status = status;  // сразу показываем на доске; не принял сервер — вернём как было
    t.done_on = CLOSED.has(status) ? today() : null;
    renderMain();
    try {
      const r = await api("PATCH", "/api/task/" + encodeURIComponent(key), {status, version: before.version});
      applyTask(r.task);
      if (card && card.task.key === key && !$("drawer").hidden) { card.task = r.task; reloadCard(); }
      toast(key + ": " + STATUS[status]);
      if (needWait) { waitEdit = key; location.hash = hashFor(route, key); }
    } catch (e) {
      Object.assign(t, before);
      if (e.status === 409 && e.data && e.data.task) applyTask(e.data.task); else renderMain();
      toast(e.message, true);
    }
  }

  // Перетаскивание на Pointer Events: мышью — сдвиг на несколько пикселей, пальцем — долгое нажатие,
  // чтобы обычное движение пальцем оставалось прокруткой. Колонку под пальцем ищем elementFromPoint.
  function startDrag() {
    if (!drag || drag.on || !document.body.contains(drag.el)) { drag = null; return; }
    clearTimeout(drag.timer);
    drag.on = true;
    const r = drag.el.getBoundingClientRect();
    drag.dx = drag.x - r.left; drag.dy = drag.y - r.top;
    const g = drag.el.cloneNode(true);
    g.classList.add("bghost");
    g.removeAttribute("data-task");
    g.style.width = r.width + "px";
    document.body.appendChild(g);
    drag.ghost = g;
    drag.el.classList.add("dragging");
    document.documentElement.classList.add("dnd");
    if (drag.touch && navigator.vibrate) navigator.vibrate(12);
    moveGhost();
    drag.raf = requestAnimationFrame(autoScroll);
  }

  function moveGhost() {
    drag.ghost.style.transform = "translate(" + Math.round(drag.x - drag.dx) + "px," + Math.round(drag.y - drag.dy) + "px) rotate(1.5deg)";
    const under = document.elementFromPoint(drag.x, drag.y), col = under && under.closest(".bcol");
    if (col === drag.over) return;
    if (drag.over) drag.over.classList.remove("over");
    drag.over = col;
    if (col) col.classList.add("over");
  }

  function autoScroll() {
    if (!drag || !drag.on) return;
    const b = $("board"), edge = 48, step = 14;
    if (b) {
      const r = b.getBoundingClientRect();
      if (drag.x < r.left + edge) b.scrollLeft -= step; else if (drag.x > r.right - edge) b.scrollLeft += step;
    }
    if (drag.y < edge) window.scrollBy(0, -step); else if (drag.y > window.innerHeight - edge) window.scrollBy(0, step);
    moveGhost();
    drag.raf = requestAnimationFrame(autoScroll);
  }

  function endDrag(drop) {
    const d = drag;
    drag = null;
    if (!d) return;
    clearTimeout(d.timer);
    if (!d.on) return;  // не тащили — обычное нажатие откроет карточку
    cancelAnimationFrame(d.raf);
    d.ghost.remove();
    d.el.classList.remove("dragging");
    document.documentElement.classList.remove("dnd");
    if (d.over) d.over.classList.remove("over");
    suppressClick = Date.now() + 500;
    if (drop && d.over) moveTask(d.key, d.over.dataset.col);
    else if (route.view === "board") renderMain();
  }

  // ---------- вложения ----------

  const canAttach = () => !!card && mode === "view" && !$("drawer").hidden && canEdit(card.task.project);

  function filesHTML(can) {
    const fs = card.files || [], imgs = fs.filter(f => f.image), other = fs.filter(f => !f.image);
    const meta = f => esc(fsize(f.size) + " · " + actorName(f.created_by) + ", " + stamp(f.created_at));
    const del = f => can ? '<button class="btn small" type="button" data-fdel="' + f.id + '" title="Удалить вложение">Удалить</button>' : "";
    let h = '<section><h3 class="subhead">Вложения' + (fs.length ? " · " + fs.length : "") + '</h3>';
    if (imgs.length) h += '<div class="thumbs">' + imgs.map(f => '<figure class="thumb"><a href="/api/file/' + f.id + '" target="_blank" rel="noopener" title="Открыть: ' + esc(f.name) + '"><img src="/api/file/' + f.id + '" alt="' + esc(f.name) + '" loading="lazy" decoding="async"></a>' +
      '<figcaption><span class="fn">' + esc(f.name) + '</span><span class="hint">' + meta(f) + '</span>' + del(f) + '</figcaption></figure>').join("") + '</div>';
    if (other.length) h += '<ul class="plist flist">' + other.map(f => '<li>' + FILEICON + '<span class="grow"><a href="/api/file/' + f.id + '?dl=1" download>' + esc(f.name) + '</a><span class="hint">' + meta(f) + '</span></span>' + del(f) + '</li>').join("") + '</ul>';
    if (!fs.length && !can) h += '<p class="muted">Вложений нет.</p>';
    h += '<div class="upl" id="upl">' + uploadsHTML() + '</div>';
    if (can) h += '<div class="fadd"><button class="btn small" type="button" data-attach>Прикрепить файл</button><span class="hint">до ' + fsize(LIMIT_FILE) + '<span class="desk-only"> · можно перетащить файл сюда, скриншот — Ctrl+V</span></span></div>';
    return h + '</section>';
  }

  function uploadsHTML() {
    if (!card) return "";
    return uploads.filter(u => u.key === card.task.key).map(u => '<div class="uprow"><span class="grow">' + esc(u.file.name) + '</span><progress max="100" value="' + Math.round(u.pct * 100) + '"></progress><span class="mono">' + Math.round(u.pct * 100) + '%</span></div>').join("");
  }

  function drawUploads() {
    const el = $("upl");
    if (el) el.innerHTML = uploadsHTML();
  }

  function uploadFiles(files) {
    if (!canAttach()) return;
    const key = card.task.key;
    files.forEach(f => {
      if (!f.size) toast(f.name + ": пустой файл", true);
      else if (f.size > LIMIT_FILE) toast(f.name + ": " + fsize(f.size) + " — больше предела " + fsize(LIMIT_FILE), true);
      else uploads.push({id: ++upSeq, key, file: f, pct: 0});
    });
    drawUploads();
    pumpUploads();
  }

  // по одному файлу: XMLHttpRequest, потому что у fetch нет хода отправки
  function xhrUpload(u) {
    return new Promise((resolve, reject) => {
      const x = new XMLHttpRequest();
      x.open("POST", "/api/task/" + encodeURIComponent(u.key) + "/files");
      x.setRequestHeader("X-Pult", "1");
      x.setRequestHeader("X-File-Name", encodeURIComponent(u.file.name || "файл"));
      x.setRequestHeader("Content-Type", "application/octet-stream");
      x.upload.onprogress = e => { if (e.lengthComputable) { u.pct = e.loaded / e.total; drawUploads(); } };
      x.onload = () => {
        let data = null;
        try { data = JSON.parse(x.responseText); } catch (e) { data = null; }
        if (x.status === 401) { setGate(); reject(new ApiErr(401, "Нужен вход", data)); }
        else if (x.status < 200 || x.status >= 300) reject(new ApiErr(x.status, (data && data.error) || ("Пульт ответил ошибкой " + x.status), data));
        else resolve(data);
      };
      x.onerror = () => reject(new ApiErr(0, "Нет связи с Пультом. Проверьте сеть и повторите."));
      x.send(u.file);
    });
  }

  async function pumpUploads() {
    if (pumping) return;
    pumping = true;
    try {
      while (uploads.length) {
        const u = uploads[0];
        try {
          const r = await xhrUpload(u);
          applyTask(r.task);
          if (card && card.task.key === u.key) { card.task = r.task; await reloadCard(); }
          toast("Прикреплено: " + r.file.name);
        } catch (e) {
          toast(u.file.name + ": " + e.message, true);
          if (e.status === 401) { uploads = []; break; }
        }
        uploads.shift();
        drawUploads();
      }
    } finally { pumping = false; }
  }

  // Скриншот из буфера приходит файлом image.png — даём ему имя с датой
  function pastedName(f) {
    if (f.name && f.name !== "image.png") return f;
    const d = new Date(), ext = (f.type.split("/")[1] || "png").replace("jpeg", "jpg");
    return new File([f], "скриншот " + pad(d.getDate()) + "." + pad(d.getMonth() + 1) + " " + pad(d.getHours()) + "-" + pad(d.getMinutes()) + "-" + pad(d.getSeconds()) + "." + ext, {type: f.type});
  }

  function jentry(e, withTask) {
    const via = [e.via, e.session].filter(Boolean).join(" · ");
    const meta = '<span class="jm"><b>' + esc(actorName(e.actor)) + '</b><span class="mono">' + esc(stamp(e.at)) + '</span>' + (via ? '<span>' + esc(via) + '</span>' : "") + '</span>';
    let task = "";
    if (withTask && e.task) task = '<span class="jtask"><span class="tid">' + esc(e.task) + '</span>' + esc(e.task_title || "") + (e.gone ? ' <span class="muted">(удалена)</span>' : "") + '</span>';
    else if (withTask && e.project && PROJ[e.project]) task = '<span class="jtask">' + esc(PROJ[e.project].name) + '</span>';
    const text = e.text ? '<span class="jt">' + linkify(e.text) + '</span>' : "";
    const ch = e.changes && e.changes.length ? '<span class="jc">' + e.changes.map(esc).join(" · ") + '</span>' : "";
    return meta + task + text + ch;
  }

  async function renderLog() {
    const main = $("main"), seq = ++logSeq;
    const chip = (id, label) => '<button class="chipbtn" type="button" data-logactor="' + esc(id) + '" aria-pressed="' + (logActor === id) + '">' + esc(label) + '</button>';
    const head = '<header class="mhead"><div class="crumbs">Все проекты</div><h1>Журнал</h1><p>Последние изменения по всем задачам: кто, когда и что поменял.</p></header>' +
      '<div class="filters">' + chip("", "Все") + S.actors.map(a => chip(a.id, a.name)).join("") + '</div>';
    main.innerHTML = head + '<p class="empty-main">Загружаю…</p>';
    let j;
    try { j = await api("GET", "/api/journal?limit=200" + (logActor ? "&actor=" + encodeURIComponent(logActor) : "")); }
    catch (e) { if (seq === logSeq && route.view === "log") main.innerHTML = head + '<p class="empty-main">' + esc(e.message) + '</p>'; return; }
    if (seq !== logSeq || route.view !== "log") return;
    let h = head + '<div class="feed">', day = "";
    j.journal.forEach(e => {
      const d = dayOf(e.at);
      if (d !== day) { day = d; h += '<div class="dayhead">' + esc(dayLabel(d)) + '</div>'; }
      const click = e.task && !e.gone;
      h += click
        ? '<button class="jrow click" type="button" data-task="' + esc(e.task) + '">' + av(e.actor) + '<span>' + jentry(e, true) + '</span></button>'
        : '<div class="jrow">' + av(e.actor) + '<span>' + jentry(e, true) + '</span></div>';
    });
    if (!j.journal.length) h += '<p class="empty-main">Записей нет.</p>';
    main.innerHTML = h + '</div>';
  }

  function gateHTML() {
    return '<div class="gate"><h1>Нужен вход</h1><p>Пульт открывается по личной ссылке для входа или по одноразовой ссылке-приглашению. Откройте её на этом устройстве один раз — дальше вход запомнится на полгода.</p><p class="muted">Ссылку выдаёт владелец Пульта; если она потерялась или устарела, попросите новую.</p></div>';
  }

  function setGate() {
    gate = true;
    $("new-task").hidden = true;
    $("team").innerHTML = "";
    $("side").innerHTML = "";
    hideDrawer();
    $("main").innerHTML = gateHTML();
  }

  // ---------- участники ----------

  const liveKeys = u => u.keys.filter(k => !k.revoked_at);
  function lastSeen(k) { return [k.last_used_at].concat(k.sessions.map(s => s.last_seen_at)).filter(Boolean).sort().pop() || ""; }
  function uaShort(ua) {
    ua = String(ua || "");
    const os = /Android/.test(ua) ? "Android" : /iPhone|iPad/.test(ua) ? "iOS" : /Windows/.test(ua) ? "Windows" : /Mac OS X/.test(ua) ? "macOS" : /Linux/.test(ua) ? "Linux" : "";
    const br = /YaBrowser/.test(ua) ? "Яндекс Браузер" : /Edg\//.test(ua) ? "Edge" : /Firefox\//.test(ua) ? "Firefox" : /Chrome\//.test(ua) ? "Chrome" : /Safari\//.test(ua) ? "Safari" : /python|urllib|curl/i.test(ua) ? "программа" : "";
    return [br, os].filter(Boolean).join(", ");
  }
  function keyLine(k) {
    const s = k.sessions[0], when = lastSeen(k);
    return [s ? uaShort(s.agent) : "", when ? "последний раз " + stamp(when) : "ещё не использовался", "выдан " + ddmm(dayOf(k.created_at))].filter(Boolean).join(" · ");
  }
  function personSub(u) {
    const role = u.role === "owner" ? "владелец Пульта" : "участник";
    const bits = [u.kind === "model" ? "модель" + (u.owner ? ", хозяин — " + nameOf(u.owner) : "") : "человек", role];
    if (!u.active) bits.push("доступ отключён");
    if (u.note && u.note !== role) bits.push(u.note);
    return bits.join(" · ");
  }
  function accessLine(u) {
    if (u.role === "owner") return "все проекты, включая закрытые";
    const names = Object.entries(u.access || {}).map(([p, l]) => projName(p) + (l === "view" ? " (читает)" : ""));
    if (!names.length) return "доступа к проектам нет";
    return names.slice(0, 3).join(", ") + (names.length > 3 ? " и ещё " + (names.length - 3) : "");
  }
  function seenLine(u) {
    const keys = liveKeys(u), when = keys.map(lastSeen).filter(Boolean).sort().pop();
    if (when) return "последний раз " + stamp(when);
    if (keys.length) return u.kind === "model" ? "ключ выдан, ещё не использовался" : "вход выдан, ещё не использовался";
    const inv = u.invites.find(i => i.state === "active");
    if (inv) return "ссылка до " + stamp(inv.expires_at);
    return u.kind === "model" ? "ключа нет" : "входов нет";
  }
  function scopeLabel(g) {
    if (g.kind === "all") return "Все проекты, кроме закрытых";
    if (g.kind === "area") return "Раздел «" + (AREA[g.target] ? AREA[g.target].name : g.target) + "»";
    return "Проект «" + projName(g.target) + "»";
  }

  function peopleHead() {
    const btns = (OWNER ? '<button class="btn small" type="button" data-pnew="person">+ Человек</button>' : "") +
      (S.me.kind === "person" ? '<button class="btn small" type="button" data-pnew="model">+ Модель</button>' : "");
    const text = OWNER
      ? "Кто видит какие проекты и как входит в Пульт. Доступ ко всем проектам или к разделу не открывает закрытые разделы и проекты: к ним доступ выдаётся отдельно."
      : "Ваш доступ, входы и модели. Доступ к проектам выдаёт владелец Пульта; своей модели вы выдаёте не больше, чем есть у вас.";
    return '<header class="mhead"><div class="mhead-row"><div class="mhead"><div class="crumbs">Пульт</div><h1>Участники</h1><p>' + esc(text) + '</p></div><div class="actions">' + btns + '</div></div></header>';
  }

  function peopleListHTML() {
    const row = u => '<button class="urow' + (u.active ? "" : " off") + (u.id === pid ? " on" : "") + '" type="button" data-person="' + esc(u.id) + '">' + avA(u) +
      '<span class="uname"><span class="un">' + esc(u.name) + (u.id === S.me.id ? '<span class="muted">это вы</span>' : "") + '</span><span class="sub">' + esc(personSub(u)) + '</span></span>' +
      '<span class="uacc">' + esc(accessLine(u)) + '</span><span class="useen">' + esc(seenLine(u)) + '</span></button>';
    const people = P.people.filter(u => u.kind === "person"), models = P.people.filter(u => u.kind === "model");
    let h = "";
    if (people.length) h += '<section class="group"><div class="gtitle">Люди<span class="n">' + people.length + '</span></div>' + people.map(row).join("") + '</section>';
    if (models.length) h += '<section class="group"><div class="gtitle">Модели<span class="n">' + models.length + '</span></div>' + models.map(row).join("") + '</section>';
    return h || '<p class="empty-main">Участников пока нет.</p>';
  }

  async function renderPeople() {
    const main = $("main"), seq = ++peopleSeq;
    if (!P) main.innerHTML = peopleHead() + '<p class="empty-main">Загружаю…</p>';
    let r;
    try { r = await api("GET", "/api/people"); }
    catch (e) { if (seq === peopleSeq && route.view === "people") main.innerHTML = peopleHead() + '<p class="empty-main">' + esc(e.message) + '</p>'; return; }
    if (seq !== peopleSeq || route.view !== "people") return;
    P = r;
    main.innerHTML = peopleHead() + peopleListHTML();
  }

  async function openPerson(id) {
    const seq = ++cardSeq;
    openKey = null; card = null; mode = "view"; passOpen = false;
    pid = id; pcard = null;
    if (secret && secret.actor !== id) secret = null;
    if (id.charAt(0) === "+") {
      pmode = "new";
      showDrawer(newPersonHTML(id.slice(1)), "Новый участник");
      const name = $("drawer").querySelector('input[name="name"]');
      if (name) name.focus();
      return;
    }
    pmode = "view";
    showDrawer(drawerHead(id) + '<div class="task"><p class="muted">Загружаю…</p></div>', "Карточка участника");
    try {
      const r = await api("GET", "/api/people");
      if (seq !== cardSeq) return;
      P = r;
      pcard = r.people.find(u => u.id === id) || null;
      if (!pcard) { $("drawer").innerHTML = drawerHead(id) + '<div class="task"><p class="empty-main">Такого участника нет или его карточка вам не видна.</p></div>'; return; }
      renderPerson();
      const x = $("drawer").querySelector("[data-close]");
      if (x) x.focus();
    } catch (e) {
      if (seq === cardSeq) $("drawer").innerHTML = drawerHead(id) + '<div class="task"><p class="empty-main">' + esc(e.message) + '</p></div>';
    }
  }

  function renderPerson() {
    const d = $("drawer"), top = d.scrollTop;
    d.innerHTML = personHTML(pcard);
    d.scrollTop = top;
  }

  async function reloadPerson() {
    const id = pid;
    try {
      P = await api("GET", "/api/people");
      if (route.view === "people") $("main").innerHTML = peopleHead() + peopleListHTML();
      if (pid !== id || $("drawer").hidden) return;
      pcard = P.people.find(u => u.id === id) || null;
      if (pcard) renderPerson(); else closeTask();
    } catch (e) { if (e.status !== 401) toast(e.message, true); }
  }

  async function personAct(promise, okMsg) {
    try {
      const r = await promise;
      if (okMsg) toast(okMsg);
      return r;
    } catch (e) {
      toast(e.message, true);
      return null;
    } finally {
      await reloadPerson();
      refresh(true);
    }
  }

  function scopeOptions() {
    let h = OWNER ? '<option value="all:">Все проекты, кроме закрытых</option>' : "";
    areas().forEach(a => {
      const ps = visibleProjects(a).filter(p => OWNER || ACC[p.id]);
      if (!ps.length && !OWNER) return;
      h += '<optgroup label="' + esc(a.name + (a.closed ? " — закрытый раздел" : "")) + '">' +
        (OWNER ? '<option value="area:' + esc(a.id) + '">Весь раздел «' + esc(a.name) + '»</option>' : "") +
        ps.map(p => '<option value="project:' + esc(p.id) + '">' + esc(p.name + (p.closed ? " (закрытый)" : "")) + '</option>').join("") + '</optgroup>';
    });
    return h;
  }
  const levelSelect = name => '<select class="sel" name="' + name + '" aria-label="Доступ"><option value="edit">правит</option><option value="view">читает</option></select>';
  const grantRow = () => '<li><select class="sel" name="g_scope" aria-label="Что открыть"><option value="">— выберите —</option>' + scopeOptions() + '</select>' + levelSelect("g_level") + '<button class="iconbtn" type="button" data-rmrow aria-label="Убрать">' + XICON + '</button></li>';
  const parseScope = v => { const i = v.indexOf(":"); return {kind: v.slice(0, i), target: v.slice(i + 1)}; };

  function secretHTML() {
    const s = secret, invite = s.kind === "invite";
    const what = invite ? "Ссылка для входа" : "Ключ модели";
    const note = invite ? "действует до " + stamp(s.expires) + ", войти по ней можно один раз. Скопируйте и передайте — больше её не покажем."
      : "скопируйте и передайте модели — больше его не покажем. Потеряется — выдайте новый, а этот отзовите.";
    return '<div class="secret" role="status"><p><b>' + what + '</b> — ' + esc(note) + '</p><div class="secret-row"><input class="inp mono" readonly value="' + esc(s.value) + '" aria-label="' + what + '" data-secret><button class="btn small primary" type="button" data-copy-secret>Скопировать</button></div></div>';
  }

  function loginHTML(u, mine) {
    const can = OWNER || mine;
    let h = '<section><h3 class="subhead">Вход в Пульт</h3>';
    if (secret && secret.actor === u.id && secret.kind === "invite") h += secretHTML();
    if (can && u.active) {
      const days = [1, 3, 7, 14, 30].filter(d => OWNER || d <= 7);
      h += '<form class="inline" data-form="invite"><select class="sel short" name="days" aria-label="Срок ссылки">' + days.map(d => '<option value="' + d + '"' + (d === 7 ? " selected" : "") + '>на ' + d + ' ' + plural(d, "день", "дня", "дней") + '</option>').join("") + '</select>' +
        '<input class="inp" name="note" maxlength="60" placeholder="' + (mine ? "для какого устройства" : "для кого или для какого устройства") + '" autocomplete="off" aria-label="Заметка к ссылке">' +
        '<button class="btn small" type="submit">' + (mine ? "Ссылка для другого устройства" : "Выдать ссылку для входа") + '</button></form>' +
        '<p class="hint">Ссылка одноразовая: по ней входят один раз, на одном устройстве, дальше вход помнится полгода.</p>';
    }
    const inv = u.invites.filter(i => i.state === "active"), keys = liveKeys(u);
    let list = inv.map(i => '<li><span class="grow">Ссылка от ' + esc(stamp(i.created_at)) + (i.note ? " · " + esc(i.note) : "") + '<span class="hint">действует до ' + esc(stamp(i.expires_at)) + ', ещё не использована</span></span>' +
      (can ? '<button class="btn small" type="button" data-revoke-invite="' + i.id + '">Отозвать</button>' : "") + '</li>').join("");
    list += keys.map(k => '<li><span class="grow">' + esc(k.label) + (k.current ? ' <span class="pill doing">это устройство</span>' : "") + '<span class="hint">' + esc(keyLine(k)) + '</span></span>' +
      (can ? '<button class="btn small" type="button" data-revoke-key="' + esc(k.kid) + '"' + (k.current ? " data-current" : "") + '>Закрыть вход</button>' : "") + '</li>').join("");
    h += list ? '<ul class="plist">' + list + '</ul>' : '<p class="muted">Входов пока нет.</p>';
    const gone = u.keys.length - keys.length;
    if (gone) h += '<p class="hint">Закрытых входов: ' + gone + '</p>';
    return h + '</section>';
  }

  function keysHTML(u, manage) {
    let h = '<section><h3 class="subhead">Ключи модели</h3>';
    if (secret && secret.actor === u.id && secret.kind === "key") h += secretHTML();
    if (manage && u.active) h += '<form class="inline" data-form="key"><input class="inp" name="label" maxlength="40" placeholder="название ключа, например codex-ноутбук" autocomplete="off" aria-label="Название ключа"><button class="btn small" type="submit">Выдать ключ</button></form>' +
      '<p class="hint">Модель передаёт ключ в заголовке Authorization: Bearer. Как работать с Пультом — <a href="/static/api.txt" target="_blank" rel="noopener">описание API</a>.</p>';
    const keys = liveKeys(u);
    h += keys.length ? '<ul class="plist">' + keys.map(k => '<li><span class="grow">' + esc(k.label) + ' <span class="tid">' + esc(k.kid) + '</span><span class="hint">' + esc(keyLine(k)) + '</span></span>' +
      (manage ? '<button class="btn small" type="button" data-revoke-key="' + esc(k.kid) + '">Отозвать</button>' : "") + '</li>').join("") + '</ul>' : '<p class="muted">Действующих ключей нет.</p>';
    const gone = u.keys.length - keys.length;
    if (gone) h += '<p class="hint">Отозванных ключей: ' + gone + '</p>';
    return h + '</section>';
  }

  function personHTML(u) {
    const mine = u.id === S.me.id, manage = OWNER || u.owner === S.me.id;
    if (pmode === "edit") return drawerHead(u.id) + '<div class="task">' + personFormHTML(u) + '</div>';
    let h = '<div class="task"><div class="task-top"><div class="crumbs">' + (u.kind === "model" ? "Модель" : "Человек") + '</div><h2 class="who">' + avA(u) + '<span>' + esc(u.name) + '</span></h2><p class="muted">' + esc(personSub(u)) + '</p></div>';
    h += '<section><h3 class="subhead">Доступ</h3>';
    if (u.role === "owner") h += '<p class="notes">Владелец Пульта: видит и правит все проекты, включая закрытые, и управляет участниками.</p>';
    else {
      h += u.grants.length ? '<ul class="plist">' + u.grants.map(g => '<li><span class="grow">' + esc(scopeLabel(g)) + '</span><span class="pill ' + (g.level === "edit" ? "doing" : "next") + '">' + LEVEL[g.level] + '</span>' +
        (manage ? '<button class="iconbtn" type="button" data-ungrant="' + esc(g.kind + ":" + g.target) + '" aria-label="Снять доступ" title="Снять доступ">' + XICON + '</button>' : "") + '</li>').join("") + '</ul>' : '<p class="muted">Доступа к проектам нет.</p>';
      const eff = Object.entries(u.access || {});
      if (eff.length) h += '<p class="hint">Итого видит ' + eff.length + ' ' + plural(eff.length, "проект", "проекта", "проектов") + ': ' + esc(eff.map(([p, l]) => projName(p) + (l === "view" ? " (читает)" : "")).join(", ")) + '</p>';
      if (u.kind === "model" && u.owner) h += '<p class="hint">Модель получает не больше, чем у её хозяина (' + esc(nameOf(u.owner)) + ').</p>';
      if (manage && u.active) h += '<form class="inline" data-form="grant"><select class="sel" name="scope" aria-label="Что открыть"><option value="">— что открыть —</option>' + scopeOptions() + '</select>' + levelSelect("level") + '<button class="btn small" type="submit">Выдать доступ</button></form>';
    }
    h += '</section>';
    h += u.kind === "person" ? loginHTML(u, mine) : keysHTML(u, manage);
    if (u.kind === "person") {
      const ms = P.people.filter(x => x.owner === u.id);
      if (ms.length || (mine && S.me.kind === "person")) {
        h += '<section><h3 class="subhead">Модели</h3>' + (ms.length ? '<ul class="plist">' + ms.map(x => '<li><button class="linkbtn who" type="button" data-person="' + esc(x.id) + '">' + avA(x) + esc(x.name) + '</button><span class="hint">' + esc(accessLine(x)) + '</span></li>').join("") + '</ul>' : '<p class="muted">Своих моделей пока нет.</p>') +
          (mine ? '<div class="actions"><button class="btn small" type="button" data-pnew="model">+ Модель</button></div>' : "") + '</section>';
      }
    }
    const tools = [];
    if (manage) tools.push('<button class="btn" type="button" data-pedit>Изменить</button>');
    if (OWNER && u.kind === "person" && !mine) tools.push('<button class="btn" type="button" data-role="' + (u.role === "owner" ? "member" : "owner") + '">' + (u.role === "owner" ? "Сделать участником" : "Сделать владельцем Пульта") + '</button>');
    if (manage && !mine) tools.push('<span class="spacer"></span><button class="btn' + (u.active ? " danger" : "") + '" type="button" data-active="' + (u.active ? "0" : "1") + '">' + (u.active ? "Отключить доступ" : "Включить доступ") + '</button>');
    if (tools.length) h += '<div class="formbar">' + tools.join("") + '</div>';
    return drawerHead(u.id) + h + '</div>';
  }

  function personFormHTML(u) {
    return '<form class="form" data-form="pedit" novalidate><h2>Изменить участника</h2>' +
      '<label class="f"><span>Имя</span><input class="inp" name="name" required maxlength="120" value="' + esc(u.name) + '" autocomplete="off"></label>' +
      '<label class="f"><span>Заметка</span><input class="inp" name="note" maxlength="500" value="' + esc(u.note) + '" autocomplete="off" placeholder="кто это и зачем"></label>' +
      '<div class="f"><span>Цвет</span><div class="swatches" role="radiogroup" aria-label="Цвет">' + COLORS.map(c => '<label class="sw"><input type="radio" name="color" value="' + c + '"' + (u.color === c ? " checked" : "") + ' aria-label="' + c + '">' + avA(Object.assign({}, u, {color: c})) + '</label>').join("") + '</div></div>' +
      '<div class="formbar"><button class="btn primary" type="submit">Сохранить</button><button class="btn" type="button" data-cancel>Отмена</button></div></form>';
  }

  function newPersonHTML(kind) {
    const model = kind === "model";
    const hosts = (P ? P.people : S.actors).filter(a => a.kind === "person" && a.active);
    let h = '<form class="form" data-form="pnew" data-kind="' + (model ? "model" : "person") + '" novalidate><h2>' + (model ? "Новая модель" : "Новый человек") + '</h2>' +
      '<label class="f"><span>Имя</span><input class="inp" name="name" required maxlength="120" autocomplete="off" placeholder="' + (model ? "например, Codex" : "например, Таня") + '"></label>';
    if (OWNER) h += '<label class="f"><span>Короткое имя латиницей</span><input class="inp mono" name="id" maxlength="32" autocomplete="off" spellcheck="false" placeholder="придумаем из имени"><span class="hint">По нему участника назначают из командной строки; потом не меняется.</span></label>';
    if (model) h += OWNER
      ? '<label class="f"><span>Чья модель</span><select class="sel" name="owner">' + hosts.map(a => '<option value="' + esc(a.id) + '"' + (a.id === S.me.id ? " selected" : "") + '>' + esc(a.name) + '</option>').join("") + '</select><span class="hint">Модель получает не больше, чем у её хозяина.</span></label>'
      : '<p class="hint">Модель будет вашей и начнёт с вашего же доступа; урезать его можно в карточке модели.</p>';
    if (OWNER) {
      if (!model) h += '<label class="f"><span>Роль</span><select class="sel" name="role"><option value="member" selected>Участник — только выданные проекты</option><option value="owner">Владелец Пульта — всё, включая участников</option></select></label>';
      h += '<div class="f"><span>Доступ</span><ul class="grants-edit">' + grantRow() + '</ul><div><button class="btn small" type="button" data-addgrant>+ ещё доступ</button></div><span class="hint">Доступ ко всем проектам или к разделу не открывает закрытые разделы и проекты — их выбирайте отдельно.</span></div>';
    }
    h += '<label class="f"><span>Заметка</span><input class="inp" name="note" maxlength="500" autocomplete="off" placeholder="кто это и зачем"></label>' +
      '<div class="formbar"><button class="btn primary" type="submit">Добавить</button><button class="btn" type="button" data-cancel>Отмена</button></div></form>';
    return drawerHead(model ? "новая модель" : "новый человек") + '<div class="task">' + h + '</div>';
  }

  async function saveNewPerson(form) {
    const v = n => form.elements[n] ? form.elements[n].value.trim() : "";
    const kind = form.dataset.kind, body = {name: v("name"), kind};
    if (!body.name) { toast("Нужно имя", true); form.elements.name.focus(); return; }
    if (v("note")) body.note = v("note");
    if (OWNER) {
      if (v("id")) body.id = v("id");
      if (kind === "model") body.owner = v("owner"); else body.role = v("role") || "member";
      const scopes = [...form.querySelectorAll('select[name="g_scope"]')], levels = [...form.querySelectorAll('select[name="g_level"]')];
      body.grants = scopes.map((s, i) => s.value ? Object.assign(parseScope(s.value), {level: levels[i].value}) : null).filter(Boolean);
    }
    await busy(form, api("POST", "/api/actor", body).then(r => {
      P = null;
      toast((kind === "model" ? "Модель добавлена" : "Участник добавлен") + (kind === "person" ? ": выдайте ссылку для входа" : ": выдайте ключ"));
      refresh(true);
      location.hash = "#people&u=" + encodeURIComponent(r.id);
    }, e => toast(e.message, true)));
  }

  function confirmed(el, ask) {
    if (el.classList.contains("armed")) return true;
    const was = el.textContent;
    el.classList.add("armed");
    el.textContent = ask;
    setTimeout(() => { if (document.body.contains(el)) { el.classList.remove("armed"); el.textContent = was; } }, 5000);
    return false;
  }

  // ---------- карточка задачи ----------

  const drawerHead = key => '<div class="drawer-close"><span class="tid">' + esc(key) + '</span><button class="xbtn" type="button" data-close aria-label="Закрыть карточку">' + XICON + '</button></div>';

  function showDrawer(html, label) {
    const d = $("drawer");
    if (d.hidden) lastFocus = document.activeElement;
    d.setAttribute("aria-label", label || "Карточка задачи");
    d.innerHTML = html;
    d.hidden = false;
    $("scrim").hidden = false;
    document.documentElement.classList.add("locked");
  }

  function hideDrawer() {
    const d = $("drawer");
    const was = !d.hidden, wasPerson = pid !== null;
    d.hidden = true;
    $("scrim").hidden = true;
    document.documentElement.classList.remove("locked");
    openKey = null; card = null; mode = "view"; passOpen = false;
    pid = null; pcard = null; pmode = "view"; secret = null;
    if (was) {
      if (S && !gate && (liveMain() || (wasPerson && route.view === "people"))) renderMain();
      if (lastFocus && document.body.contains(lastFocus)) lastFocus.focus();
    }
  }

  function closeTask() {
    if (/(^#|&)(t|u)=/.test(location.hash)) location.hash = hashFor(route, null);
    else hideDrawer();
  }

  async function openTask(key) {
    const seq = ++cardSeq;
    openKey = key; mode = "view"; passOpen = false; card = null;
    pid = null; pcard = null; secret = null;
    showDrawer(drawerHead(key) + '<div class="task"><p class="muted">Загружаю…</p></div>');
    if (liveMain()) renderMain();
    try {
      const r = await api("GET", "/api/task/" + encodeURIComponent(key));
      if (seq !== cardSeq) return;
      card = r;
      // с доски бросили в «Ждёт», а чего ждём — не записано: сразу форма с этим полем
      if (waitEdit === key && canEdit(r.task.project)) mode = "edit";
      waitEdit = null;
      renderCard();
      const w = mode === "edit" && $("drawer").querySelector('input[name="wait"]');
      if (w) { w.focus(); toast("Запишите, чего ждём, и дату проверки"); }
      const x = !w && $("drawer").querySelector("[data-close]");
      if (x) x.focus();
    } catch (e) {
      if (seq !== cardSeq) return;
      $("drawer").innerHTML = drawerHead(key) + '<div class="task"><p class="empty-main">' + esc(e.message) + '</p></div>';
    }
  }

  async function reloadCard() {
    if (!card) return;
    const key = card.task.key, seq = ++cardSeq;
    try {
      const r = await api("GET", "/api/task/" + encodeURIComponent(key));
      if (seq !== cardSeq || !card || card.task.key !== key) return;
      card = r;
      renderCard();
    } catch (e) { toast(e.message, true); }
  }

  function renderCard() {
    const d = $("drawer"), top = d.scrollTop;
    d.innerHTML = cardHTML();
    d.scrollTop = top;
  }

  function cardHTML() {
    const t = card.task, p = PROJ[t.project], a = AREA_OF[t.project], can = canEdit(t.project);
    if (mode === "edit" && can) return drawerHead(t.key) + '<div class="task">' + formHTML(t, false) + '</div>';
    const statusbar = can
      ? '<span class="statusbar" role="group" aria-label="Статус">' + STATUS_ORDER.map(s => '<button class="sbtn ' + s + '" type="button" data-status="' + s + '" aria-pressed="' + (t.status === s) + '"><i class="lamp ' + s + '"></i>' + STATUS[s] + '</button>').join("") + '</span>'
      : '<span class="pill ' + t.status + '"><i class="lamp ' + t.status + '"></i>' + STATUS[t.status] + '</span>';
    const who = t.holder ? ACT[t.holder] : null;
    let f = '<dl class="fields">';
    f += '<dt>Статус</dt><dd>' + statusbar + (isLate(t) ? '<span class="pill late">просрочено</span>' : "") + '</dd>';
    f += '<dt>Держит</dt><dd>' + (t.holder ? av(t.holder) + '<span>' + esc(actorName(t.holder)) + '</span><span class="muted">' + (who && who.kind === "model" ? "модель" : "человек") + (who && who.note ? ", " + esc(who.note) : "") + '</span>' : '<span class="muted">никто, задача свободна</span>') + '</dd>';
    if (t.next) f += '<dt>' + (isOpen(t) ? "Следующий шаг" : "Итог") + '</dt><dd>' + linkify(t.next) + '</dd>';
    if (t.wait) f += '<dt>Ждём</dt><dd>' + linkify(t.wait) + (t.check ? '<span class="chipdate' + (isCheck(t) ? " check" : "") + '">проверить ' + ddmm(t.check) + '</span>' : "") + '</dd>';
    else if (t.check) f += '<dt>Проверить</dt><dd><span class="chipdate' + (isCheck(t) ? " check" : "") + '">' + ddmm(t.check) + '</span></dd>';
    if (t.due) f += '<dt>Срок</dt><dd><span class="chipdate' + (isLate(t) ? " late" : "") + '">' + ddmm(t.due) + '</span></dd>';
    if (t.done_on) f += '<dt>Закрыта</dt><dd>' + dateChip(t) + '</dd>';
    f += '<dt>Проект</dt><dd>' + (a ? esc(a.name) + ' › ' : "") + '<button class="linkbtn" type="button" data-nav="p:' + esc(t.project) + '">' + esc(p ? p.name : t.project) + '</button>' +
      ((a && a.closed) || (p && p.closed) ? '<span class="muted">' + LOCK + 'закрытый</span>' : "") + (p && p.shared ? '<span class="muted">' + SHARED + 'общий: ' + esc(p.shared) + '</span>' : "") + '</dd>';
    const mem = t.mem || (p && p.mem);
    if (mem) f += '<dt>Подробности</dt><dd><span class="mono">' + esc(mem) + '</span></dd>';
    const ai = aiHTML(aiPick(aiChain("task", t)));
    if (ai) f += '<dt>ИИ</dt><dd>' + ai + '</dd>';
    f += '<dt>Создана</dt><dd><span class="muted">' + esc(stamp(t.created_at)) + ', ' + esc(actorName(t.created_by)) + '</span></dd></dl>';

    const pass = passOpen && can ? '<div class="picker" role="group" aria-label="Кому передать">' + holderChoices(t.project, t.holder).map(({a: x, noacc}) => '<button class="chipbtn' + (noacc ? " noacc" : "") + '" type="button" data-holder="' + esc(x.id) + '" aria-pressed="' + (t.holder === x.id) + '"' + (noacc ? ' title="Не правит этот проект: задачу не увидит"' : "") + '>' + av(x.id) + esc(x.name) + (noacc ? " · нет доступа" : "") + '</button>').join("") +
      '<button class="chipbtn" type="button" data-holder="" aria-pressed="' + !t.holder + '">никто</button></div>' : "";
    const acts = can
      ? '<div class="actions">' + (t.holder !== S.me.id ? '<button class="btn primary" type="button" data-take>Взять себе</button>' : "") +
        '<button class="btn" type="button" data-pass aria-expanded="' + passOpen + '">Передать</button><button class="btn" type="button" data-edit>Изменить</button><button class="btn" type="button" data-copy>Ссылка</button></div>' + pass
      : '<div class="actions"><button class="btn" type="button" data-copy>Ссылка</button><span class="muted">Только чтение: права править задачи этого проекта у вас нет.</span></div>';
    const ask = t.prompt ? '<section class="ask"><h3 class="subhead">' + MSG + 'Сообщение для ИИ</h3><p class="notes">' + linkify(t.prompt) + '</p></section>' : "";
    const notes = t.notes ? '<section><h3 class="subhead">Описание</h3><p class="notes">' + linkify(t.notes) + '</p></section>' : "";
    const comment = can ? '<form class="comment" data-form="comment"><textarea class="ta short" name="text" maxlength="4000" placeholder="Запись в журнал: что сделано, что решили" aria-label="Запись в журнал"></textarea><div><button class="btn small" type="submit">Записать в журнал</button></div></form>' : "";
    const jr = card.journal.length ? '<ol class="journal">' + card.journal.map(e => '<li>' + av(e.actor) + '<div>' + jentry(e, false) + '</div></li>').join("") + '</ol>' : '<p class="muted">Записей пока нет.</p>';
    return drawerHead(t.key) + '<div class="task"><div class="task-top"><div class="crumbs">' + (a ? esc(a.name) + ' › ' : "") + esc(p ? p.name : t.project) + '</div><h2>' + esc(t.title) + '</h2></div>' +
      f + acts + ask + notes + filesHTML(can) + '<section><h3 class="subhead">Журнал</h3>' + comment + '</section>' + jr + '</div>';
  }

  function formHTML(t, isNew) {
    const projOpts = areas().map(a => {
      const ps = a.projects.filter(p => (!p.archived || p.id === t.project) && (canEdit(p.id) || p.id === t.project));
      return ps.length ? '<optgroup label="' + esc(a.name) + '">' + ps.map(p => '<option value="' + esc(p.id) + '"' + (p.id === t.project ? " selected" : "") + '>' + esc(p.name) + '</option>').join("") + '</optgroup>' : "";
    }).join("");
    const statusOpts = STATUS_ORDER.map(s => '<option value="' + s + '"' + (s === t.status ? " selected" : "") + '>' + STATUS[s] + '</option>').join("");
    // для новой задачи проект ещё может смениться: участнику — все, с кем у него общие проекты, сервер проверит
    const pool = t.project ? holderChoices(t.project, t.holder) : S.actors.filter(a => a.active).map(a => ({a, noacc: false}));
    const holderOpts = '<option value="">никто</option>' + pool.map(({a, noacc}) => '<option value="' + esc(a.id) + '"' + (a.id === t.holder ? " selected" : "") + '>' + esc(a.name) + (a.kind === "model" ? " (модель)" : "") + (noacc ? " — нет доступа к проекту" : "") + '</option>').join("");
    return '<form class="form" data-form="' + (isNew ? "new" : "edit") + '" novalidate>' +
      '<h2>' + (isNew ? "Новая задача" : "Изменить задачу") + '</h2>' +
      '<label class="f"><span>Название</span><input class="inp" name="title" required maxlength="200" value="' + esc(t.title) + '" autocomplete="off"></label>' +
      '<div class="row2"><label class="f"><span>Проект</span><select class="sel" name="project" required>' + (!t.project ? '<option value="">выберите проект</option>' : "") + projOpts + '</select></label>' +
      '<label class="f"><span>Статус</span><select class="sel" name="status">' + statusOpts + '</select></label></div>' +
      '<div class="row2"><label class="f"><span>Держит</span><select class="sel" name="holder">' + holderOpts + '</select></label>' +
      '<label class="f"><span>Срок</span><input class="inp" type="date" name="due" value="' + esc(t.due || "") + '"></label></div>' +
      '<label class="f"><span>Следующий шаг</span><textarea class="ta short" name="next" maxlength="500">' + esc(t.next || "") + '</textarea></label>' +
      '<label class="f"><span>Сообщение для ИИ</span><textarea class="ta short" name="prompt" maxlength="4000" placeholder="что сделать ИИ, который возьмёт карточку: прочтёт первым в начале сессии">' + esc(t.prompt || "") + '</textarea></label>' +
      '<div class="row2"><label class="f"><span>Ждём</span><input class="inp" name="wait" maxlength="500" value="' + esc(t.wait || "") + '" placeholder="кого или чего" autocomplete="off"></label>' +
      '<label class="f"><span>Проверить</span><input class="inp" type="date" name="check" value="' + esc(t.check || "") + '"></label></div>' +
      '<label class="f"><span>Описание</span><textarea class="ta" name="notes" maxlength="20000" rows="6">' + esc(t.notes || "") + '</textarea></label>' +
      aiInputs("task", t, aiPick(aiChain("project", PROJ[t.project] || null))) +
      '<label class="f"><span>Подробности</span><input class="inp mono" name="mem" maxlength="500" value="' + esc(t.mem || "") + '" placeholder="файл памяти или папка" autocomplete="off"></label>' +
      '<label class="f"><span>Запись в журнал</span><input class="inp" name="comment" maxlength="4000" placeholder="необязательно: почему меняем" autocomplete="off"></label>' +
      '<div class="formbar"><button class="btn primary" type="submit">' + (isNew ? "Создать" : "Сохранить") + '</button><button class="btn" type="button" data-cancel>Отмена</button><span class="spacer"></span>' +
      (isNew || !OWNER ? "" : '<button class="btn danger" type="button" data-del>Удалить</button>') + '</div></form>';
  }

  function readForm(form) {
    const v = n => form.elements[n] ? form.elements[n].value : "";
    return {
      title: v("title").trim(), project: v("project"), status: v("status"), holder: v("holder") || null,
      due: v("due") || null, next: v("next").trim(), wait: v("wait").trim(), check: v("check") || null,
      notes: v("notes").trim(), mem: v("mem").trim(), prompt: v("prompt").trim(), model: v("model").trim(),
      effort: v("effort").trim(), comment: v("comment").trim()
    };
  }

  async function patchTask(body, okMsg) {
    if (!card) return;
    try {
      const r = await api("PATCH", "/api/task/" + encodeURIComponent(card.task.key), Object.assign({version: card.task.version}, body));
      mode = "view"; passOpen = false;
      card.task = r.task;
      applyTask(r.task);
      await reloadCard();
      if (okMsg) toast(okMsg);
      return r.task;
    } catch (e) {
      if (e.status === 409 && e.data && e.data.task) {
        card.task = e.data.task;
        applyTask(e.data.task);
        mode = "view";
        await reloadCard();
      }
      toast(e.message, true);
      return null;
    }
  }

  async function saveEdit(form) {
    const f = readForm(form), t = card.task, body = {};
    if (!f.title) { toast("Нужно название", true); form.elements.title.focus(); return; }
    const same = (x, y) => (x == null ? "" : x) === (y == null ? "" : y);
    ["title", "project", "status", "holder", "due", "next", "wait", "check", "notes", "mem", "prompt", "model", "effort"].forEach(k => { if (!same(f[k], t[k])) body[k] = f[k]; });
    if (f.comment) body.comment = f.comment;
    if (!Object.keys(body).length) { mode = "view"; renderCard(); return; }
    await busy(form, patchTask(body, "Сохранено"));
  }

  async function saveNew(form) {
    const f = readForm(form);
    if (!f.title) { toast("Нужно название", true); form.elements.title.focus(); return; }
    if (!f.project) { toast("Выберите проект", true); form.elements.project.focus(); return; }
    const body = Object.assign({}, f);
    if (!body.comment) delete body.comment;
    await busy(form, api("POST", "/api/task", body).then(r => {
      applyTask(r.task);
      toast("Создана " + r.task.key);
      location.hash = hashFor(route.view === "p" && route.id !== r.task.project ? {view: "p", id: r.task.project} : route, r.task.key);
    }, e => toast(e.message, true)));
  }

  async function busy(form, promise) {
    const buttons = [...form.querySelectorAll("button")];
    buttons.forEach(b => { b.disabled = true; });
    try { await promise; } finally { buttons.forEach(b => { b.disabled = false; }); }
  }

  function openNew() {
    if (!newDefaults) {
      const ok = id => PROJ[id] && canEdit(id);
      const r = route.view === "board" ? (splitScope(route.id) || {view: "all"}) : route;
      const pid0 = r.view === "p" && ok(r.id) ? r.id : r.view === "a" && AREA[r.id] ? ((visibleProjects(AREA[r.id]).find(p => canEdit(p.id)) || {}).id || "") : "";
      newDefaults = {project: pid0, title: "", status: "inbox", holder: null, next: "", wait: "", check: null, due: null, notes: "", mem: "", prompt: "", model: "", effort: ""};
    }
    openKey = null; card = null; mode = "new"; ++cardSeq;
    pid = null; pcard = null; secret = null;
    showDrawer(drawerHead("новая задача") + '<div class="task">' + formHTML(newDefaults, true) + '</div>');
    newDefaults = null;
    const title = $("drawer").querySelector('input[name="title"]');
    if (title) title.focus();
  }

  function newTask(projectId) {
    newDefaults = null;
    if (projectId && PROJ[projectId]) newDefaults = {project: projectId, title: "", status: "inbox", holder: null, next: "", wait: "", check: null, due: null, notes: "", mem: "", prompt: "", model: "", effort: ""};
    location.hash = hashFor(route, "new");
  }

  // ---------- адрес страницы ----------

  function parseHash() {
    let raw = location.hash.replace(/^#/, "");
    try { raw = decodeURIComponent(raw); } catch (e) { /* оставим как есть */ }
    let r = null, t = null, u = null;
    raw.split("&").forEach(part => {
      const i = part.indexOf("="), k = i < 0 ? part : part.slice(0, i), v = i < 0 ? "" : part.slice(i + 1);
      if (k === "t" && v) t = v;
      else if (k === "u" && v) u = v;
      else if ((k === "q" || k === "a" || k === "p") && v) r = {view: k, id: v};
      else if (k === "board") r = {view: k, id: v};
      else if (k === "overview" || k === "all" || k === "log" || k === "people") r = {view: k};
    });
    if (u && !r) r = {view: "people"};
    return {r, t, u};
  }

  function hashFor(r, t) {
    let s = (r.view === "q" || r.view === "a" || r.view === "p") ? r.view + "=" + encodeURIComponent(r.id)
      : r.view === "board" && r.id ? "board=" + encodeURIComponent(r.id).replace("%3A", ":") : r.view;
    if (t) s += "&t=" + encodeURIComponent(t);
    return "#" + s;
  }

  function onHash() {
    if (!S || gate) return;
    const {r, t, u} = parseHash();
    if (r) route = r;
    else if (t && t !== "new") {
      const task = S.tasks.find(x => x.key === normKey(t));
      route = task ? {view: "p", id: task.project} : {view: "all"};
    } else route = {view: "all"};
    if (route.view !== "all" && route.view !== "q" && route.view !== "a" && route.view !== "p") showAllClosed = false;
    if (aiEdit && aiEdit !== scopeKey(route)) aiEdit = null;
    renderSide();
    if (u) { if (u !== pid || $("drawer").hidden) openPerson(u); }
    else if (t === "new") { if (mode !== "new" || $("drawer").hidden) openNew(); }
    else if (t) { const key = normKey(t); if (key !== openKey || $("drawer").hidden) openTask(key); }
    else if (!$("drawer").hidden) hideDrawer();
    renderMain();
  }

  // ---------- уведомления и меню ----------

  function toast(msg, err) {
    const t = $("toast");
    t.textContent = msg;
    t.className = "toast" + (err ? " err" : "");
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, err ? 6500 : 2600);
  }

  function showMenu(anchor) {
    const m = $("menu"), r = anchor.getBoundingClientRect();
    m.innerHTML = '<p>Вы вошли как ' + esc(S.me.name) + (S.me.kind === "model" ? " (модель)" : "") + ' — ' + (OWNER ? "владелец Пульта" : "участник") + '.</p>' +
      '<button type="button" data-nav="people">' + (OWNER ? "Участники и доступ" : "Мой доступ, входы и модели") + '</button><button type="button" data-logout>Выйти на этом устройстве</button>';
    m.hidden = false;
    m.style.top = Math.round(r.bottom + 6) + "px";
    m.style.right = Math.max(8, Math.round(window.innerWidth - r.right)) + "px";
  }
  const hideMenu = () => { $("menu").hidden = true; };

  function copyText(text, input) {
    const done = () => toast("Скопировано");
    if (navigator.clipboard) navigator.clipboard.writeText(text).then(done, () => { if (input) { input.select(); toast("Выделено: нажмите Ctrl+C"); } else toast(text); });
    else if (input) { input.select(); toast("Выделено: нажмите Ctrl+C"); }
    else toast(text);
  }

  // ---------- события ----------

  const CLICKS = "[data-nav],[data-task],[data-close],[data-reset],[data-more-closed],[data-status],[data-take],[data-pass],[data-holder],[data-edit],[data-cancel],[data-copy],[data-del],[data-rmrow],[data-aiedit],[data-aicancel],[data-logactor],[data-new],[data-me],[data-logout]," +
    "[data-person],[data-pnew],[data-ungrant],[data-role],[data-revoke-key],[data-revoke-invite],[data-copy-secret],[data-pedit],[data-active],[data-addgrant],[data-board],[data-attach],[data-fdel]";

  document.addEventListener("click", ev => {
    if (suppressClick > Date.now()) { suppressClick = 0; ev.preventDefault(); return; }  // щелчок в конце перетаскивания
    if (ev.target === $("scrim")) { closeTask(); return; }
    const el = ev.target.closest(CLICKS);
    if (!el || !el.hasAttribute("data-me")) hideMenu();
    if (!el) return;
    const d = el.dataset;
    if (d.board !== undefined) {
      location.hash = hashFor({view: "board", id: d.board}, null);
    } else if (d.attach !== undefined) {
      if (canAttach()) $("file-input").click();
    } else if (d.fdel !== undefined && card) {
      if (!confirmed(el, "Точно?")) return;
      el.disabled = true;
      api("DELETE", "/api/file/" + encodeURIComponent(d.fdel)).then(r => {
        applyTask(r.task);
        if (card && card.task.key === r.task.key) { card.task = r.task; reloadCard(); }
        toast("Вложение удалено");
      }, e => { el.disabled = false; toast(e.message, true); });
    } else if (d.nav !== undefined) {
      const [view, id] = d.nav.split(":");
      showAllClosed = false;
      location.hash = hashFor(id ? {view, id} : {view}, null);
    } else if (d.task !== undefined) {
      location.hash = hashFor(route, d.task);
    } else if (d.close !== undefined) {
      closeTask();
    } else if (d.reset !== undefined) {
      $("q").value = ""; query = ""; renderMain(); $("q").focus();
    } else if (d.moreClosed !== undefined) {
      showAllClosed = true; renderMain();
    } else if (d.status !== undefined && card) {
      if (card.task.status === d.status) return;
      const needWait = d.status === "wait" && !card.task.wait;
      patchTask({status: d.status}, "Статус: " + STATUS[d.status]).then(task => {
        if (task && needWait) {
          mode = "edit"; renderCard();
          const w = $("drawer").querySelector('input[name="wait"]');
          if (w) w.focus();
          toast("Запишите, чего ждём, и дату проверки");
        }
      });
    } else if (d.take !== undefined && card) {
      patchTask({holder: S.me.id}, "Задача у вас");
    } else if (d.pass !== undefined && card) {
      passOpen = !passOpen; renderCard();
    } else if (d.holder !== undefined && card) {
      const id = d.holder || null;
      if (id === card.task.holder) { passOpen = false; renderCard(); return; }
      patchTask({holder: id}, "Держит: " + actorName(id));
    } else if (d.edit !== undefined && card) {
      mode = "edit"; renderCard();
      const title = $("drawer").querySelector('input[name="title"]');
      if (title) title.focus();
    } else if (d.cancel !== undefined) {
      if (pid !== null) { if (pmode === "new") closeTask(); else { pmode = "view"; renderPerson(); } }
      else if (mode === "new") closeTask(); else { mode = "view"; renderCard(); }
    } else if (d.aiedit !== undefined) {
      aiEdit = d.aiedit; renderMain();
      const m = $("main").querySelector('form[data-form="ai"] input[name="model"]');
      if (m) m.focus();
    } else if (d.aicancel !== undefined) {
      aiEdit = null; renderMain();
    } else if (d.copy !== undefined && card) {
      copyText(location.origin + "/#t=" + encodeURIComponent(card.task.key));
    } else if (d.del !== undefined && card) {
      if (!confirmed(el, "Точно удалить?")) return;
      const key = card.task.key;
      api("DELETE", "/api/task/" + encodeURIComponent(key) + "?version=" + card.task.version).then(() => {
        S.tasks = S.tasks.filter(x => x.key !== key);
        etag = null;
        toast("Задача " + key + " удалена");
        closeTask();
        renderSide(); renderMain();
      }, e => toast(e.message, true));
    } else if (d.addgrant !== undefined) {
      el.closest(".f").querySelector(".grants-edit").insertAdjacentHTML("beforeend", grantRow());
    } else if (d.rmrow !== undefined) {
      el.closest("li").remove();
    } else if (d.logactor !== undefined) {
      logActor = d.logactor; renderLog();
    } else if (d.new !== undefined) {
      newTask(d.new);
    } else if (d.me !== undefined) {
      if ($("menu").hidden) showMenu(el); else hideMenu();
    } else if (d.logout !== undefined) {
      hideMenu();
      api("POST", "/api/logout", {}).then(() => setGate(), e => toast(e.message, true));
    } else if (d.person !== undefined) {
      location.hash = "#people&u=" + encodeURIComponent(d.person);
    } else if (d.pnew !== undefined) {
      location.hash = "#people&u=" + encodeURIComponent("+" + d.pnew);
    } else if (d.pedit !== undefined && pcard) {
      pmode = "edit"; renderPerson();
      const n = $("drawer").querySelector('input[name="name"]');
      if (n) n.focus();
    } else if (d.ungrant !== undefined && pcard) {
      personAct(api("POST", "/api/actor/" + encodeURIComponent(pcard.id) + "/grant", Object.assign(parseScope(d.ungrant), {level: null})), "Доступ снят");
    } else if (d.role !== undefined && pcard) {
      if (!confirmed(el, d.role === "owner" ? "Точно? Получит всё, включая участников" : "Точно? Останутся только выданные проекты")) return;
      personAct(api("PATCH", "/api/actor/" + encodeURIComponent(pcard.id), {role: d.role}), d.role === "owner" ? "Теперь владелец Пульта" : "Теперь участник");
    } else if (d.active !== undefined && pcard) {
      const on = d.active === "1";
      if (!on && !confirmed(el, "Точно? Все входы закроются")) return;
      personAct(api("PATCH", "/api/actor/" + encodeURIComponent(pcard.id), {active: on}), on ? "Доступ включён" : "Доступ отключён, входы закрыты");
    } else if (d.revokeKey !== undefined && pcard) {
      if (!confirmed(el, d.current !== undefined ? "Точно? Вы выйдете" : "Точно отозвать?")) return;
      personAct(api("POST", "/api/key/" + encodeURIComponent(d.revokeKey) + "/revoke", {}), "Отозвано, входы по нему закрыты");
    } else if (d.revokeInvite !== undefined && pcard) {
      personAct(api("POST", "/api/invite/" + encodeURIComponent(d.revokeInvite) + "/revoke", {}), "Ссылка отозвана");
    } else if (d.copySecret !== undefined && secret) {
      copyText(secret.value, $("drawer").querySelector("[data-secret]"));
    }
  });

  document.addEventListener("submit", ev => {
    const form = ev.target.closest("form[data-form]");
    if (!form) return;
    ev.preventDefault();
    const kind = form.dataset.form;
    if (kind === "comment" && card) {
      const text = form.elements.text.value.trim();
      if (!text) return;
      busy(form, api("POST", "/api/task/" + encodeURIComponent(card.task.key) + "/comment", {text})
        .then(r => { card.task = r.task; applyTask(r.task); return reloadCard(); })
        .then(() => toast("Записано в журнал"), e => toast(e.message, true)));
    } else if (kind === "ai") {
      const scope = form.dataset.scope, body = {model: form.elements.model.value.trim(), effort: form.elements.effort.value.trim()};
      const path = scope === "pult" ? "/api/settings" : "/api/" + (scope[0] === "a" ? "area/" : "project/") + encodeURIComponent(scope.slice(2));
      busy(form, api("PATCH", path, body).then(() => { aiEdit = null; etag = null; return refresh(true); })
        .then(() => { if (card && mode === "view" && !$("drawer").hidden) renderCard(); toast("Сохранено"); }, e => toast(e.message, true)));
    } else if (kind === "edit" && card) saveEdit(form);
    else if (kind === "new") saveNew(form);
    else if (kind === "pnew") saveNewPerson(form);
    else if (kind === "pedit" && pcard) {
      const name = form.elements.name.value.trim(), note = form.elements.note.value.trim();
      const color = (form.querySelector('input[name="color"]:checked') || {}).value;
      if (!name) { toast("Нужно имя", true); return; }
      const body = {};
      if (name !== pcard.name) body.name = name;
      if (note !== pcard.note) body.note = note;
      if (color && color !== pcard.color) body.color = color;
      pmode = "view";
      if (!Object.keys(body).length) { renderPerson(); return; }
      busy(form, personAct(api("PATCH", "/api/actor/" + encodeURIComponent(pcard.id), body), "Сохранено"));
    } else if (kind === "grant" && pcard) {
      if (!form.elements.scope.value) { toast("Выберите, что открыть", true); form.elements.scope.focus(); return; }
      busy(form, personAct(api("POST", "/api/actor/" + encodeURIComponent(pcard.id) + "/grant",
        Object.assign(parseScope(form.elements.scope.value), {level: form.elements.level.value})), "Доступ выдан"));
    } else if (kind === "invite" && pcard) {
      const id = pcard.id, body = {days: Number(form.elements.days.value)};
      if (form.elements.note.value.trim()) body.note = form.elements.note.value.trim();
      busy(form, personAct(api("POST", "/api/actor/" + encodeURIComponent(id) + "/invite", body).then(r => {
        secret = {actor: id, kind: "invite", value: location.origin + "/login#i=" + r.token, expires: r.invite.expires_at};
      })));
    } else if (kind === "key" && pcard) {
      const id = pcard.id, label = form.elements.label.value.trim();
      busy(form, personAct(api("POST", "/api/actor/" + encodeURIComponent(id) + "/key", label ? {label} : {}).then(r => {
        secret = {actor: id, kind: "key", value: r.token};
      })));
    }
  });

  document.addEventListener("change", ev => {
    const el = ev.target;
    if (el.matches("select[data-bscope]")) location.hash = hashFor({view: "board", id: el.value}, null);
    else if (el.matches("select[data-bwho]")) { boardWho = el.value; renderMain(); }
    else if (el.id === "file-input") { uploadFiles([...el.files]); el.value = ""; }
  });

  // ---------- доска: перетаскивание ----------

  document.addEventListener("pointerdown", e => {
    const el = e.target.closest(".bcard[data-drag]");
    if (!el || e.button !== 0 || !e.isPrimary || drag) return;
    drag = {el, key: el.dataset.task, id: e.pointerId, x: e.clientX, y: e.clientY, x0: e.clientX, y0: e.clientY,
            touch: e.pointerType !== "mouse", on: false, timer: 0, ghost: null, over: null, raf: 0};
    if (drag.touch) drag.timer = setTimeout(startDrag, 350);
  });
  document.addEventListener("pointermove", e => {
    if (!drag || e.pointerId !== drag.id) return;
    drag.x = e.clientX; drag.y = e.clientY;
    if (!drag.on) {
      const far = Math.abs(drag.x - drag.x0) + Math.abs(drag.y - drag.y0);
      if (drag.touch && far > 10) { clearTimeout(drag.timer); drag = null; }  // палец поехал раньше — это прокрутка
      else if (!drag.touch && far > 6) startDrag();
      return;
    }
    moveGhost();
  });
  document.addEventListener("pointerup", e => { if (drag && e.pointerId === drag.id) endDrag(true); });
  document.addEventListener("pointercancel", e => { if (drag && e.pointerId === drag.id) endDrag(false); });
  // после долгого нажатия палец двигает карточку, а не страницу
  document.addEventListener("touchmove", e => { if (drag && drag.on) e.preventDefault(); }, {passive: false});
  document.addEventListener("contextmenu", e => { if (drag && drag.touch) e.preventDefault(); });
  document.addEventListener("dragstart", e => { if (e.target.closest && e.target.closest(".board")) e.preventDefault(); });

  // ---------- вложения: перетащить файл на карточку, вставить скриншот ----------

  const hasFiles = e => !!e.dataTransfer && [...e.dataTransfer.types].includes("Files");
  const drawer = $("drawer");
  drawer.addEventListener("dragenter", e => {
    if (!hasFiles(e) || !canAttach()) return;
    e.preventDefault();
    dropDepth++;
    drawer.classList.add("dropping");
  });
  drawer.addEventListener("dragover", e => {
    if (!hasFiles(e) || !canAttach()) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  });
  drawer.addEventListener("dragleave", e => {
    if (!hasFiles(e) || --dropDepth > 0) return;
    dropDepth = 0;
    drawer.classList.remove("dropping");
  });
  drawer.addEventListener("drop", e => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    dropDepth = 0;
    drawer.classList.remove("dropping");
    uploadFiles([...e.dataTransfer.files]);
  });
  // файл, брошенный мимо карточки, не должен открыться в вкладке вместо Пульта
  window.addEventListener("dragover", e => { if (hasFiles(e) && !drawer.contains(e.target)) { e.preventDefault(); e.dataTransfer.dropEffect = "none"; } });
  window.addEventListener("drop", e => { if (hasFiles(e)) e.preventDefault(); });
  // файл с заголовком картинки, который браузер не смог показать, — подпись вместо значка битой картинки
  document.addEventListener("error", e => {
    const th = e.target && e.target.tagName === "IMG" && e.target.closest(".thumb");
    if (th) th.classList.add("broken");
  }, true);
  document.addEventListener("paste", e => {
    const files = e.clipboardData ? [...e.clipboardData.files] : [];
    if (!files.length || !canAttach()) return;
    e.preventDefault();
    uploadFiles(files.map(pastedName));
  });

  document.addEventListener("keydown", e => {
    const a = document.activeElement, tag = a ? a.tagName : "";
    const typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (a && a.isContentEditable);
    if (e.key === "Escape") {
      if (drag) { endDrag(false); return; }
      if (!$("menu").hidden) { hideMenu(); return; }
      if (!$("drawer").hidden) {
        e.preventDefault();
        if (pid !== null && pmode === "edit") { pmode = "view"; renderPerson(); }
        else if (pid === null && mode === "edit") { mode = "view"; renderCard(); }
        else closeTask();
      }
      return;
    }
    if (typing || e.ctrlKey || e.metaKey || e.altKey || !S || gate) return;
    if (e.key === "/") { e.preventDefault(); $("q").focus(); }
    else if ((e.key === "n" || e.key === "т") && $("drawer").hidden && !$("new-task").hidden) { e.preventDefault(); newTask(); }
  });

  $("q").addEventListener("input", () => {
    query = $("q").value;
    if (!S || gate) return;
    if (route.view === "overview" || route.view === "log" || route.view === "people") location.hash = hashFor({view: "all"}, openKey);
    else renderMain();
  });
  $("new-task").addEventListener("click", () => newTask());
  window.addEventListener("hashchange", onHash);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && S && !gate && Date.now() - lastFetch > 15000) refresh();
  });
  setInterval(() => { if (document.visibilityState === "visible" && S && !gate) refresh(); }, 60000);

  (async () => {
    try { await loadState(true); }
    catch (e) { $("main").innerHTML = '<p class="empty-main">' + esc(e.message) + '</p>'; return; }
    if (gate) return;
    renderTeam();
    if (!location.hash) history.replaceState(null, "", "#all");
    onHash();
  })();
})();
