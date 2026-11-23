"use strict";
// Scratchpad notes: #notes shows the list, #notes/<id> (or #notes/new) one note.
// A note is plain text with a few Markdown-style shortcuts, shown formatted:
//   # Heading / ## Smaller / ### Smallest     - item / * item / 1. item     - [ ] task / - [x] done     **bold**
// Loaded after app.js; uses its $, $$, api, act, state, flash, confirmDialog, setState, showTab.

const SAVE_DELAY = 800;          // ms of quiet typing before a save
const LIST_RE = /^(\s*)([-*+]|\d+[.)])\s+(?:\[([ xX])\]\s+)?(.*)$/;
const HEADING_RE = /^(#{1,3})\s+(.*)$/;

let note = null;                 // the open note: {id, text, dirty, mode: "view" | "edit"}
let saveTimer = null;
let saveChain = Promise.resolve();
let openedFromList = false;      // whether "‹ Notes" can simply go back in the browser history

const noteText = $("#note-text");

// ---------- reading the text ----------

/** A line without its Markdown markers, for titles and previews. */
function plainLine(line) {
  return line.replace(HEADING_RE, "$2").replace(LIST_RE, "$4").replace(/\*\*(.+?)\*\*/g, "$1").trim();
}

function noteTitle(text) {
  const first = text.split("\n").find(l => plainLine(l));
  return first ? plainLine(first) : "Empty note";
}

function checkboxCount(text) {
  let total = 0, done = 0;
  for (const line of text.split("\n")) {
    const m = LIST_RE.exec(line);
    if (m && m[3] !== undefined) { total++; if (m[3] !== " ") done++; }
  }
  return { total, done };
}

/** Text with **bold** parts, as DOM nodes (never HTML, so a note can't inject markup). */
function inline(text) {
  const out = [];
  text.split(/(\*\*.+?\*\*)/).forEach((part, i) => {
    if (!part) return;
    if (i % 2) {
      const b = document.createElement("strong");
      b.textContent = part.slice(2, -2);
      out.push(b);
    } else {
      out.push(document.createTextNode(part));
    }
  });
  return out;
}

/** Render the note's text as formatted blocks; every block remembers its source line. */
function renderMarkdown(text, onToggle) {
  const out = [];
  let para = null;
  text.split("\n").forEach((line, i) => {
    const h = HEADING_RE.exec(line);
    const li = !h && LIST_RE.exec(line);
    if (!h && !li && line.trim()) {                       // plain text: consecutive lines form a paragraph
      if (!para) { para = document.createElement("p"); para.dataset.line = i; out.push(para); }
      else para.append(document.createElement("br"));
      para.append(...inline(line));
      return;
    }
    para = null;
    if (h) {
      const el = document.createElement("h" + (h[1].length + 1));   // # → h2, ## → h3, ### → h4
      el.dataset.line = i;
      el.append(...inline(h[2]));
      out.push(el);
    } else if (li) {
      const [, indent, marker, box, rest] = li;
      const el = document.createElement("div");
      el.className = "li";
      el.dataset.line = i;
      el.style.setProperty("--level", Math.min(6, Math.floor(indent.replace(/\t/g, "  ").length / 2)));
      if (box !== undefined) {
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.checked = box !== " ";
        cb.addEventListener("change", () => onToggle(i));
        cb.addEventListener("click", ev => ev.stopPropagation());
        el.classList.add("task");
        if (cb.checked) el.classList.add("checked");
        el.append(cb);
      } else {
        const m = document.createElement("span");
        m.className = "marker";
        m.textContent = /\d/.test(marker) ? marker : "•";
        el.append(m);
      }
      const span = document.createElement("span");
      span.append(...inline(rest));
      el.append(span);
      out.push(el);
    } else {
      out.push(Object.assign(document.createElement("div"), { className: "gap" }));
    }
  });
  return out;
}

// ---------- the list ----------

const fold = s => s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();

function updatedLabel(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const day = iso.slice(0, 10) === state.today ? "Today"
    : d.toLocaleDateString(DATE_LOCALE, { day: "numeric", month: "numeric",
        year: iso.slice(0, 4) === state.today.slice(0, 4) ? undefined : "numeric" });
  return `${day} ${iso.slice(11, 16)}`;
}

function renderNoteList() {
  if (!state) return;
  const q = fold($("#notes-search").value.trim());
  const notes = state.scratchpad.filter(n => !q || fold(n.text).includes(q));
  const items = notes.map(n => {
    const li = document.createElement("li");
    const a = document.createElement("a");
    a.href = `#notes/${n.id}`;
    a.className = "note-card";
    a.addEventListener("click", () => { openedFromList = true; });
    const title = document.createElement("strong");
    title.textContent = noteTitle(n.text);
    const preview = document.createElement("span");
    preview.className = "preview";
    const lines = n.text.split("\n").map(plainLine).filter(Boolean);
    preview.textContent = lines.slice(1, 6).join(" · ");
    const meta = document.createElement("span");
    meta.className = "meta";
    const { total, done } = checkboxCount(n.text);
    meta.textContent = updatedLabel(n.updated) + (total ? `  ·  ☑ ${done}/${total}` : "");
    a.append(title, preview, meta);
    li.append(a);
    return li;
  });
  if (!items.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = q ? "No note matches." : "No notes yet. Start one with “+ New note”.";
    items.push(li);
  }
  $("#notes-list").replaceChildren(...items);
}

$("#notes-search").addEventListener("input", renderNoteList);
$("#note-new").addEventListener("click", () => { openedFromList = true; location.hash = "#notes/new"; });

// ---------- one note ----------

function setStatus(text) { $("#note-status").textContent = text; }

function renderNote() {
  const editing = note.mode === "edit";
  $("#note-mode").textContent = editing ? "Done" : "Edit";
  $("#note-tools").hidden = !editing;
  noteText.hidden = !editing;
  $("#note-render").hidden = editing;
  if (editing) {
    if (noteText.value !== note.text) noteText.value = note.text;
  } else {
    const blocks = renderMarkdown(note.text, toggleCheckbox);
    if (!note.text.trim()) {
      blocks.splice(0, blocks.length, Object.assign(document.createElement("p"),
        { className: "empty", textContent: "Empty note. Tap to write." }));
    }
    $("#note-render").replaceChildren(...blocks);
  }
}

function setMode(mode, caretLine) {
  note.mode = mode;
  renderNote();
  if (mode === "edit") {
    noteText.focus();
    if (caretLine !== undefined) {
      const lines = note.text.split("\n");
      const pos = lines.slice(0, caretLine + 1).join("\n").length;
      noteText.setSelectionRange(pos, pos);
    }
  } else {
    save();
  }
}

/** Open a note (id), a new one ("new"), or close the open one (null). */
function openNote(id) {
  if (note && note.id === id) return;
  if (note) save();
  if (id === null) { note = null; return; }
  if (id === "new") {
    note = { id: null, text: "", dirty: false, mode: "edit" };
  } else {
    const n = state.scratchpad.find(x => x.id === id);
    if (!n) { flash("That note doesn't exist (anymore)."); history.replaceState(null, "", "#notes"); routeNotes(); return; }
    note = { id: n.id, text: n.text, dirty: false, mode: n.text.trim() ? "view" : "edit" };
  }
  setStatus("");
  renderNote();
  if (note.mode === "edit") noteText.focus();
  window.scrollTo(0, 0);
}

function toggleCheckbox(lineNo) {
  const lines = note.text.split("\n");
  lines[lineNo] = lines[lineNo].replace(/\[([ xX])\]/, (_, c) => (c === " " ? "[x]" : "[ ]"));
  note.text = lines.join("\n");
  note.dirty = true;
  renderNote();
  save();
}

// Tapping the formatted text switches to editing, with the caret on that line.
$("#note-render").addEventListener("click", ev => {
  const block = ev.target.closest("[data-line]");
  setMode("edit", block ? Number(block.dataset.line) : undefined);
});
$("#note-mode").addEventListener("click", () => setMode(note.mode === "edit" ? "view" : "edit"));

noteText.addEventListener("input", () => {
  note.text = noteText.value;
  note.dirty = true;
  setStatus("Editing…");
  clearTimeout(saveTimer);
  saveTimer = setTimeout(save, SAVE_DELAY);
});

// ---------- saving ----------

/** Save the given note (default: the open one) if it changed. Saves run one at a time, in order. */
function save(n = note) {
  clearTimeout(saveTimer);
  if (!n) return saveChain;
  saveChain = saveChain.then(() => doSave(n));
  return saveChain;
}

async function doSave(n) {
  if (!n.dirty) return;
  if (!n.id && !n.text.trim()) { n.dirty = false; return; }    // don't create empty notes
  const text = n.text;
  n.dirty = false;
  if (n === note) setStatus("Saving…");
  try {
    const data = n.id ? await api("PUT", `/api/notes/${n.id}`, { text })
                      : await api("POST", "/api/notes", { text });
    if (!n.id) {
      n.id = data.note.id;
      if (n === note && location.hash === "#notes/new") history.replaceState(null, "", `#notes/${n.id}`);
    }
    setState(data);
    if (n === note && !n.dirty) setStatus("Saved");
  } catch (e) {
    n.dirty = true;                                   // keep the text; the next edit or leave retries
    if (n === note) setStatus("Not saved!");
    if (e.status !== 401) flash(e.message);
  }
}

// Last chance when the tab is closed or the phone switches apps.
document.addEventListener("visibilitychange", () => { if (document.hidden) save(); });
window.addEventListener("pagehide", () => {
  if (!note || !note.dirty || (!note.id && !note.text.trim())) return;
  fetch(note.id ? `/api/notes/${note.id}` : "/api/notes", {
    method: note.id ? "PUT" : "POST", keepalive: true,
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: note.text }),
  });
  note.dirty = false;
});

/** New data from the server: refresh the list, and the open note if it changed elsewhere. */
function notesOnState() {
  renderNoteList();
  if (!note && currentTab() === "notes" && location.hash.startsWith("#notes/")) { routeNotes(); return; }
  if (!note || !note.id || note.dirty) return;
  const n = state.scratchpad.find(x => x.id === note.id);
  if (!n) {
    flash("This note was deleted on another device.");
    note = null;
    history.replaceState(null, "", "#notes");
    routeNotes();
  } else if (n.text !== note.text && document.activeElement !== noteText) {
    note.text = n.text;
    renderNote();
  }
}

// ---------- leaving and deleting ----------

$("#note-back").addEventListener("click", () => {
  if (openedFromList) history.back();
  else { history.replaceState(null, "", "#notes"); routeNotes(); }
});

$("#note-delete").addEventListener("click", async () => {
  const n = note;
  if (!n.id && !n.text.trim()) { $("#note-back").click(); return; }
  const ok = await confirmDialog("Delete note?", `“${noteTitle(n.text)}” will be deleted for good.`);
  if (!ok) return;
  await saveChain;                                  // a create in flight must finish so there's an id
  n.dirty = false;
  if (n.id && !(await act("DELETE", `/api/notes/${n.id}`))) return;
  note = null;
  $("#note-back").click();
});

// ---------- formatting toolbar and list continuation ----------

/** Replace the lines the selection touches with fn(lines), keeping the browser's undo history. */
function editLines(fn) {
  const v = noteText.value;
  const start = v.lastIndexOf("\n", noteText.selectionStart - 1) + 1;
  let end = v.indexOf("\n", noteText.selectionEnd);
  if (end < 0) end = v.length;
  const replaced = fn(v.slice(start, end).split("\n")).join("\n");
  noteText.focus();
  noteText.setSelectionRange(start, end);
  if (!document.execCommand("insertText", false, replaced)) {
    noteText.setRangeText(replaced, start, end, "end");
    noteText.dispatchEvent(new Event("input"));
  }
}

/** A line without its heading or list prefix: [indent, content]. */
function stripPrefix(line) {
  const h = HEADING_RE.exec(line);
  if (h) return ["", h[2]];
  const li = LIST_RE.exec(line);
  if (li) return [li[1], li[4]];
  return [line.match(/^\s*/)[0], line.trimStart()];
}

const FORMATS = {
  h: lines => lines.map(l => {
    const h = HEADING_RE.exec(l);
    const level = h ? h[1].length : 0;
    const body = stripPrefix(l)[1];
    return level === 3 ? body : "#".repeat(level + 1) + " " + body;    // none → # → ## → ### → none
  }),
  ul: lines => {
    const all = lines.every(l => { const m = LIST_RE.exec(l); return m && !/\d/.test(m[2]) && m[3] === undefined; });
    return lines.map(l => { const [ind, body] = stripPrefix(l); return all ? ind + body : `${ind}- ${body}`; });
  },
  ol: lines => {
    const all = lines.every(l => { const m = LIST_RE.exec(l); return m && /\d/.test(m[2]); });
    return lines.map((l, i) => { const [ind, body] = stripPrefix(l); return all ? ind + body : `${ind}${i + 1}. ${body}`; });
  },
  cb: lines => {
    const all = lines.every(l => { const m = LIST_RE.exec(l); return m && m[3] !== undefined; });
    return lines.map(l => { const [ind, body] = stripPrefix(l); return all ? ind + body : `${ind}- [ ] ${body}`; });
  },
};

$$("#note-tools button").forEach(b => {
  b.addEventListener("pointerdown", ev => ev.preventDefault());     // keep the textarea focused (phone keyboard stays up)
  b.addEventListener("click", () => {
    const fmt = b.dataset.fmt;
    if (fmt !== "b") { editLines(FORMATS[fmt]); return; }
    const { selectionStart: s, selectionEnd: e, value: v } = noteText;
    const sel = v.slice(s, e);
    noteText.focus();
    if (!document.execCommand("insertText", false, `**${sel}**`)) {
      noteText.setRangeText(`**${sel}**`, s, e, "end");
      noteText.dispatchEvent(new Event("input"));
    }
    if (!sel) noteText.setSelectionRange(s + 2, s + 2);
  });
});

// Enter inside a list item starts the next item; Enter on an empty item ends the list.
noteText.addEventListener("beforeinput", ev => {
  if (ev.inputType !== "insertLineBreak" && ev.inputType !== "insertParagraph") return;
  const { selectionStart: pos, selectionEnd, value: v } = noteText;
  if (pos !== selectionEnd) return;
  const lineStart = v.lastIndexOf("\n", pos - 1) + 1;
  const m = LIST_RE.exec(v.slice(lineStart, pos));
  if (!m) return;
  ev.preventDefault();
  const [, indent, marker, box, rest] = m;
  if (!rest.trim()) {                                   // empty item: drop the marker, stop the list
    noteText.setSelectionRange(lineStart, pos);
    document.execCommand("insertText", false, "") || (noteText.setRangeText("", lineStart, pos, "end"),
      noteText.dispatchEvent(new Event("input")));
    noteText.setSelectionRange(lineStart, lineStart);   // Chrome may leave the caret before the line break
    return;
  }
  const next = /\d/.test(marker) ? `${parseInt(marker, 10) + 1}${marker.slice(-1)}` : marker;
  const insert = `\n${indent}${next} ${box !== undefined ? "[ ] " : ""}`;
  if (!document.execCommand("insertText", false, insert)) {
    noteText.setRangeText(insert, pos, pos, "end");
    noteText.dispatchEvent(new Event("input"));
  }
});

// Tab / Shift+Tab indent and outdent list lines (on a keyboard).
noteText.addEventListener("keydown", ev => {
  if (ev.key !== "Tab") return;
  ev.preventDefault();
  editLines(lines => lines.map(l => (ev.shiftKey ? l.replace(/^ {1,2}/, "") : "  " + l)));
});

// ---------- routing ----------

/** Show the list or one note, from the URL (#notes, #notes/new, #notes/<id>). */
function routeNotes() {
  const m = /^#notes\/(.+)$/.exec(location.hash);
  const id = m ? decodeURIComponent(m[1]) : null;
  if (id && !state) return;                         // wait for the data; notesOnState comes back here
  $("#notes-index").hidden = Boolean(id);
  $("#note").hidden = !id;
  openNote(id);
  if (!id) renderNoteList();
}

/** Called by app.js when the Notes tab is shown (true) or left (false). */
function notesTab(active) {
  if (active) routeNotes();
  else save();
}

window.addEventListener("hashchange", () => showTab(currentTab()));
if (currentTab() === "notes") routeNotes();
