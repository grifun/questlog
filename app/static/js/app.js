"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

let state = null;          // last /api/state response
let addKind = "once";
let editing = null;        // quest being edited
let busy = false;

// ---------- API ----------

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (res.status === 401) { showLogin(); throw new ApiError(401, "Log in first."); }
  if (!res.ok) {
    const msg = data && typeof data.detail === "string" ? data.detail : `Request failed (${res.status})`;
    throw new ApiError(res.status, msg);
  }
  return data;
}

/** Run a change; on success apply the returned state, on failure show the error and resync. */
async function act(method, path, body) {
  try {
    const data = await api(method, path, body);
    if (data && data.quests) setState(data);
    return data;
  } catch (e) {
    if (e.status !== 401) { flash(e.message); load(); }
    return null;
  }
}

/** A styled yes/no dialog. Resolves to true only if the confirm button was pressed. */
function confirmDialog(title, text, okLabel = "Delete") {
  const dlg = $("#confirm");
  $("#confirm-title").textContent = title;
  $("#confirm-text").textContent = text;
  $("#confirm-ok").textContent = okLabel;
  dlg.returnValue = "";
  dlg.showModal();
  return new Promise(resolve => dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true }));
}

async function deleteQuest(q) {
  const ok = await confirmDialog("Delete quest?",
    `“${q.title}” will be removed from your quests. What you've already checked off stays in the history.`);
  if (!ok) return false;
  return Boolean(await act("DELETE", `/api/quests/${q.id}`));
}

function flash(msg) {
  const b = $("#banner");
  b.textContent = msg;
  b.hidden = !msg;
}

// ---------- login ----------

function showLogin() {
  $("#login").hidden = false;
  $("#quests").hidden = $("#history").hidden = true;
  $(".tabs").hidden = true;
  $("#password").focus();
}

$("#login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("#login-error").textContent = "";
  try {
    await api("POST", "/api/login", { password: $("#password").value });
    $("#password").value = "";
    $("#login").hidden = true;
    $(".tabs").hidden = false;
    showTab(currentTab());
    load();
  } catch (e) {
    $("#login-error").textContent = e.message;
  }
});

$("#logout").addEventListener("click", async () => {
  await api("POST", "/api/logout");
  location.reload();
});

// ---------- tabs ----------

function currentTab() { return location.hash === "#history" ? "history" : "quests"; }

function showTab(tab) {
  $$(".tabs button").forEach(b => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
  $("#quests").hidden = tab !== "quests";
  $("#history").hidden = tab !== "history";
  if (tab === "history") resetHistory();
}

$$(".tabs button").forEach(b => b.addEventListener("click", () => {
  history.replaceState(null, "", b.dataset.tab === "history" ? "#history" : "#");
  showTab(b.dataset.tab);
}));

// ---------- quests ----------

async function load() {
  try {
    setState(await api("GET", "/api/state"));
  } catch (e) {
    if (e.status === 403) flash(e.message);
    else if (e.status !== 401) flash("Can't reach the server: " + e.message);
  }
}

function setState(s) {
  state = s;
  $("#logout").hidden = !s.login;
  flash(s.error || "");
  if ($("#login").hidden === false) { $("#login").hidden = true; $(".tabs").hidden = false; showTab(currentTab()); }
  render();
}

function dueInfo(due) {
  if (!due) return null;
  const today = state.today;
  const d = new Date(due + "T00:00:00");
  const label = d.toLocaleDateString(DATE_LOCALE, { weekday: "short", day: "numeric", month: "numeric",
    year: due.slice(0, 4) === today.slice(0, 4) ? undefined : "numeric" });
  const tomorrow = isoDate(addDays(parseIso(today), 1));
  if (due < today) return { cls: "overdue", text: `⏳ overdue · deadline was ${label}` };
  if (due === today) return { cls: "today", text: "⏳ deadline today" };
  if (due === tomorrow) return { cls: "soon", text: "⏳ deadline tomorrow" };
  return { cls: "", text: `⏳ deadline ${label}` };
}

function render() {
  if (!state) return;
  for (const group of $$(".group")) {
    const kind = group.dataset.kind;
    let quests = state.quests.filter(q => q.kind === kind);
    if (kind === "once") {
      // Dated quests first (soonest first), then the rest in their own order.
      quests = [...quests].sort((a, b) =>
        (a.due ? 0 : 1) - (b.due ? 0 : 1) || (a.due || "").localeCompare(b.due || "") || a.order - b.order);
    } else {
      quests = [...quests].sort((a, b) => Number(a.done) - Number(b.done) || a.order - b.order);
    }
    const list = $(".list", group);
    list.replaceChildren(...quests.map(questRow));
    if (!quests.length) {
      const li = document.createElement("li");
      li.className = "empty";
      li.textContent = kind === "once" ? "No open quests." : `No ${kind} quests yet.`;
      list.append(li);
    }
    const doneCount = quests.filter(q => q.done).length;
    $(".count", group).textContent = kind === "once" ? (quests.length || "") : quests.length ? `${doneCount}/${quests.length}` : "";
  }
}

function questRow(q) {
  const li = document.createElement("li");
  li.className = "quest" + (q.done ? " done" : "");
  li.dataset.id = q.id;

  const check = document.createElement("button");
  check.className = "check";
  check.textContent = "✓";
  check.title = q.done ? "Undo" : "Done";
  check.setAttribute("aria-label", (q.done ? "Uncheck " : "Check off ") + q.title);
  check.addEventListener("click", () => toggle(q));

  const body = document.createElement("div");
  body.className = "body";
  body.title = "Edit";
  const title = document.createElement("div");
  title.className = "title";
  title.textContent = q.title;
  body.append(title);
  if (q.notes) {
    const notes = document.createElement("div");
    notes.className = "notes";
    notes.textContent = q.notes;
    body.append(notes);
  }
  const due = q.kind === "once" && dueInfo(q.due);
  if (due) {
    const d = document.createElement("div");
    d.className = "due " + due.cls;
    d.textContent = due.text;
    body.append(d);
  }
  body.addEventListener("click", () => openEdit(q));

  const actions = document.createElement("div");
  actions.className = "actions";
  const edit = document.createElement("button");
  edit.className = "icon";
  edit.textContent = "✎";
  edit.title = "Edit";
  edit.setAttribute("aria-label", "Edit " + q.title);
  edit.addEventListener("click", () => openEdit(q));
  const del = document.createElement("button");
  del.className = "icon del";
  del.textContent = "🗑";
  del.title = "Delete";
  del.setAttribute("aria-label", "Delete " + q.title);
  del.addEventListener("click", () => deleteQuest(q));
  actions.append(edit, del);

  const handle = document.createElement("span");
  handle.className = "handle";
  handle.textContent = "⋮⋮";
  handle.title = "Drag to reorder";
  handle.addEventListener("pointerdown", ev => startDrag(ev, li));

  li.append(check, body, actions, handle);
  return li;
}

async function toggle(q) {
  if (busy) return;
  busy = true;
  try {
    if (q.kind === "once") {
      const data = await act("POST", `/api/quests/${q.id}/done`);
      if (data) toast(`Done: ${q.title}`, () => act("POST", `/api/history/${encodeURIComponent(data.entry.id)}/undo`));
    } else {
      await act("POST", `/api/quests/${q.id}/${q.done ? "undone" : "done"}`);
    }
  } finally {
    busy = false;
  }
}

// ---------- add ----------

function setAddKind(kind) {
  addKind = kind;
  $$("#add .seg button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.kind === kind)));
  $("#add-due").hidden = kind !== "once";
  if (kind !== "once") $("#add-due").close();
}
$$("#add .seg button").forEach(b => b.addEventListener("click", () => { setAddKind(b.dataset.kind); $("#add-title").focus(); }));

$("#add").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const title = $("#add-title").value.trim();
  if (!title) return;
  const body = { title, kind: addKind };
  if (addKind === "once" && $("#add-due").value) body.due = $("#add-due").value;
  if (await act("POST", "/api/quests", body)) {
    $("#add-title").value = "";
    $("#add-due").value = "";
  }
  $("#add-title").focus();
});

// ---------- edit ----------

function openEdit(q) {
  editing = q;
  $("#edit-title").value = q.title;
  $("#edit-kind").value = q.kind;
  $("#edit-notes").value = q.notes || "";
  $("#edit-due").value = q.due || "";
  $("#edit-due").close();
  $("#edit-due-wrap").hidden = q.kind !== "once";
  $("#edit").showModal();
}

// Calendar widgets for the deadline (see datefield.js).
DateField($("#add-due"), { today: () => state && state.today });
DateField($("#edit-due"), { today: () => state && state.today });

$("#edit-kind").addEventListener("change", () => { $("#edit-due-wrap").hidden = $("#edit-kind").value !== "once"; });
$("#edit-cancel").addEventListener("click", () => $("#edit").close());

$("#edit-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const kind = $("#edit-kind").value;
  const body = { title: $("#edit-title").value, kind, notes: $("#edit-notes").value };
  if (kind === "once") body.due = $("#edit-due").value || null;
  if (await act("PATCH", `/api/quests/${editing.id}`, body)) $("#edit").close();
});

$("#edit-delete").addEventListener("click", async () => {
  $("#edit").close();
  if (!(await deleteQuest(editing))) openEdit(editing);
});

// ---------- drag to reorder (pointer events, so it works with touch too) ----------

function startDrag(ev, li) {
  ev.preventDefault();
  const list = li.parentElement;
  li.classList.add("dragging");
  li.setPointerCapture?.(ev.pointerId);
  const move = (e) => {
    const over = document.elementFromPoint(e.clientX, e.clientY)?.closest(".quest");
    if (!over || over === li || over.parentElement !== list) return;
    const r = over.getBoundingClientRect();
    list.insertBefore(li, e.clientY < r.top + r.height / 2 ? over : over.nextSibling);
  };
  const up = async () => {
    li.classList.remove("dragging");
    li.removeEventListener("pointermove", move);
    li.removeEventListener("pointerup", up);
    li.removeEventListener("pointercancel", up);
    const ids = $$(".quest", $("#quests")).map(el => el.dataset.id);
    const before = [...state.quests].sort((a, b) => a.order - b.order).map(q => q.id);
    if (ids.join() !== before.join()) await act("PUT", "/api/order", { ids });
  };
  li.addEventListener("pointermove", move);
  li.addEventListener("pointerup", up);
  li.addEventListener("pointercancel", up);
}

// ---------- toast with undo ----------

let toastTimer = null;
function toast(text, onUndo) {
  const t = $("#toast");
  $("span", t).textContent = text;
  const btn = $("button", t);
  btn.onclick = () => { t.hidden = true; onUndo(); };
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 6000);
}

// ---------- history ----------

let hist = { entries: [], more: null, loading: false, done: false };

function resetHistory() {
  hist = { entries: [], more: null, loading: false, done: false };
  $("#history-list").replaceChildren();
  loadHistory();
}

async function loadHistory() {
  if (hist.loading || hist.done) return;
  hist.loading = true;
  $("#history-more").textContent = "Loading…";
  try {
    const q = hist.more ? `?before=${hist.more}` : "";
    const data = await api("GET", "/api/history" + q);
    hist.entries.push(...data.entries);
    hist.more = data.more;
    hist.done = !data.more;
    renderHistory();
  } catch (e) {
    $("#history-more").textContent = e.message;
    hist.loading = false;
    return;
  }
  hist.loading = false;
  $("#history-more").textContent = hist.done ? (hist.entries.length ? "That's everything." : "Nothing done yet.") : "";
  // Keep loading while the sentinel is still on screen (e.g. a short month).
  if (!hist.done && isVisible($("#history-more"))) loadHistory();
}

function isVisible(el) {
  const r = el.getBoundingClientRect();
  return r.top < innerHeight && r.bottom >= 0 && !$("#history").hidden;
}

new IntersectionObserver((items) => {
  if (items.some(i => i.isIntersecting) && !$("#history").hidden) loadHistory();
}).observe($("#history-more"));

function dayLabel(day) {
  const today = state ? state.today : new Date().toISOString().slice(0, 10);
  const d = new Date(day + "T00:00:00");
  const t = new Date(today + "T00:00:00");
  const diff = Math.round((t - d) / 86400000);
  if (diff === 0) return "Today";
  if (diff === 1) return "Yesterday";
  return capitalize(d.toLocaleDateString(DATE_LOCALE, { weekday: "long", day: "numeric", month: "long",
    year: day.slice(0, 4) === today.slice(0, 4) ? undefined : "numeric" }));
}

const KIND_LABEL = { daily: "daily", weekly: "weekly", once: "quest" };

function renderHistory() {
  const text = $("#history-search").value.trim().toLowerCase();
  const kind = $("#history-kind").value;
  const shown = hist.entries.filter(e => (!kind || e.kind === kind) && (!text || e.title.toLowerCase().includes(text)));
  const days = new Map();
  for (const e of shown) {
    const day = e.at.slice(0, 10);
    if (!days.has(day)) days.set(day, []);
    days.get(day).push(e);
  }
  const out = [];
  for (const [day, entries] of days) {
    const sec = document.createElement("div");
    sec.className = "day";
    const h = document.createElement("h3");
    h.textContent = dayLabel(day);
    const n = document.createElement("span");
    n.className = "n";
    n.textContent = entries.length;
    h.append(n);
    sec.append(h);
    for (const e of entries) {
      const row = document.createElement("div");
      row.className = "entry";
      const time = document.createElement("time");
      time.textContent = e.at.slice(11, 16);
      const badge = document.createElement("span");
      badge.className = "badge " + e.kind;
      badge.textContent = KIND_LABEL[e.kind] || e.kind;
      const title = document.createElement("span");
      title.className = "title";
      title.textContent = e.title;
      const undo = document.createElement("button");
      undo.className = "undo";
      undo.textContent = "↶";
      undo.title = e.kind === "once" ? "Undo: remove from history and reopen the quest" : "Remove from history";
      undo.addEventListener("click", async () => {
        const what = e.kind === "once" ? " The quest goes back to your open quests." : "";
        if (!(await confirmDialog("Remove from history?",
          `“${e.title}” (${e.at.slice(0, 16).replace("T", " ")}) will be removed from the history.${what}`, "Remove"))) return;
        if (await act("POST", `/api/history/${encodeURIComponent(e.id)}/undo`)) {
          hist.entries = hist.entries.filter(x => x.id !== e.id);
          renderHistory();
        }
      });
      row.append(time, badge, title, undo);
      sec.append(row);
    }
    out.push(sec);
  }
  $("#history-list").replaceChildren(...out);
}

$("#history-search").addEventListener("input", renderHistory);
$("#history-kind").addEventListener("change", renderHistory);

// ---------- start, and stay in sync ----------

setAddKind("once");
showTab(currentTab());
load();
// Pick up changes from other devices and the day/week rollover.
document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });
setInterval(() => { if (!document.hidden) load(); }, 60000);
