"use strict";
/* A small calendar date picker, styled like the rest of the app (the native <input type="date">
   looks different in every browser and is easy to overlook).

   DateField(el, { today }) turns an empty element into a "📅 Deadline" button that opens a month
   calendar. The element then has:
     el.value     "YYYY-MM-DD" or "" (settable)
     el.close()   close the calendar
   and fires a "change" event when the user picks or clears a date.
   An element with the class "inline" opens its calendar in place (used inside the edit dialog,
   where a floating popover would be clipped); otherwise the calendar floats below the button. */

function pad2(n) { return String(n).padStart(2, "0"); }
function isoDate(d) { return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`; }
function parseIso(s) { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); }
function addDays(d, n) { return new Date(d.getFullYear(), d.getMonth(), d.getDate() + n); }

// Dates are shown in Czech format: "pá 4. 12.", "prosinec 2026", "pátek 4. prosince".
const DATE_LOCALE = "cs-CZ";
const WEEKDAYS = ["Po", "Út", "St", "Čt", "Pá", "So", "Ne"];
const capitalize = s => s.charAt(0).toUpperCase() + s.slice(1);

function DateField(el, opts = {}) {
  const todayIso = () => (opts.today && opts.today()) || isoDate(new Date());
  let value = "";
  let shown = null;            // first day of the month on display

  el.innerHTML = `
    <span class="date-wrap">
      <button type="button" class="date-btn" aria-haspopup="dialog" aria-expanded="false">
        <span class="date-icon" aria-hidden="true">📅</span><span class="date-text"></span>
      </button>
      <button type="button" class="date-clear" title="Remove deadline" aria-label="Remove deadline" hidden>✕</button>
    </span>
    <div class="cal" role="dialog" aria-label="Choose a deadline" hidden>
      <div class="cal-head">
        <button type="button" class="cal-nav" data-step="-1" aria-label="Previous month">‹</button>
        <span class="cal-title"></span>
        <button type="button" class="cal-nav" data-step="1" aria-label="Next month">›</button>
      </div>
      <div class="cal-grid"></div>
      <div class="cal-foot">
        <button type="button" data-quick="0">Today</button>
        <button type="button" data-quick="1">Tomorrow</button>
        <button type="button" data-quick="7">In a week</button>
      </div>
    </div>`;
  const btn = el.querySelector(".date-btn");
  const clear = el.querySelector(".date-clear");
  const cal = el.querySelector(".cal");
  const grid = el.querySelector(".cal-grid");

  function label() {
    const text = el.querySelector(".date-text");
    if (!value) {
      text.textContent = "Deadline";
      btn.classList.remove("set");
    } else {
      const d = parseIso(value);
      const sameYear = value.slice(0, 4) === todayIso().slice(0, 4);
      text.textContent = d.toLocaleDateString(DATE_LOCALE, { weekday: "short", day: "numeric", month: "numeric",
        year: sameYear ? undefined : "numeric" });
      btn.classList.add("set");
    }
    clear.hidden = !value;
  }

  function render() {
    el.querySelector(".cal-title").textContent =
      capitalize(shown.toLocaleDateString(DATE_LOCALE, { month: "long", year: "numeric" }));
    const today = todayIso();
    const offset = (shown.getDay() + 6) % 7;          // Monday-first weeks
    const start = addDays(shown, -offset);
    const cells = WEEKDAYS.map(w => `<span class="cal-wd">${w}</span>`);
    for (let i = 0; i < 42; i++) {
      const d = addDays(start, i);
      const iso = isoDate(d);
      const cls = ["cal-day"];
      if (d.getMonth() !== shown.getMonth()) cls.push("other");
      if (iso < today) cls.push("past");
      if (iso === today) cls.push("today");
      if (iso === value) cls.push("selected");
      if (d.getDay() === 0 || d.getDay() === 6) cls.push("weekend");
      cells.push(`<button type="button" class="${cls.join(" ")}" data-date="${iso}"` +
        `${iso === value ? ' aria-pressed="true"' : ""}>${d.getDate()}</button>`);
    }
    grid.innerHTML = cells.join("");
  }

  function open() {
    const base = parseIso(value || todayIso());
    shown = new Date(base.getFullYear(), base.getMonth(), 1);
    render();
    cal.hidden = false;
    btn.setAttribute("aria-expanded", "true");
    (grid.querySelector(".selected") || grid.querySelector(".today") || grid.querySelector(".cal-day")).focus();
  }

  function close() {
    cal.hidden = true;
    btn.setAttribute("aria-expanded", "false");
  }

  function pick(iso) {
    value = iso;
    label();
    close();
    el.dispatchEvent(new Event("change", { bubbles: true }));
    btn.focus();
  }

  btn.addEventListener("click", () => (cal.hidden ? open() : close()));
  clear.addEventListener("click", () => pick(""));
  el.querySelectorAll(".cal-nav").forEach(b => b.addEventListener("click", () => {
    shown = new Date(shown.getFullYear(), shown.getMonth() + Number(b.dataset.step), 1);
    render();
  }));
  grid.addEventListener("click", ev => {
    const day = ev.target.closest(".cal-day");
    if (day) pick(day.dataset.date);
  });
  el.querySelectorAll("[data-quick]").forEach(b => b.addEventListener("click", () =>
    pick(isoDate(addDays(parseIso(todayIso()), Number(b.dataset.quick))))));

  // Arrow keys move between days; Escape closes the calendar (not the surrounding dialog).
  cal.addEventListener("keydown", ev => {
    if (ev.key === "Escape") {
      ev.preventDefault();
      ev.stopPropagation();
      close();
      btn.focus();
      return;
    }
    const step = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 }[ev.key];
    const cur = ev.target.closest(".cal-day");
    if (!step || !cur) return;
    ev.preventDefault();
    const next = addDays(parseIso(cur.dataset.date), step);
    if (next.getMonth() !== shown.getMonth()) {
      shown = new Date(next.getFullYear(), next.getMonth(), 1);
      render();
    }
    grid.querySelector(`[data-date="${isoDate(next)}"]`).focus();
  });

  // A click anywhere outside closes it.
  document.addEventListener("pointerdown", ev => {
    if (!cal.hidden && !el.contains(ev.target)) close();
  });

  Object.defineProperty(el, "value", {
    get: () => value,
    set: v => { value = v || ""; label(); if (!cal.hidden) render(); },
  });
  el.close = close;
  label();
  return el;
}
