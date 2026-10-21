import {
  STATE_LABELS,
  STATE_ORDER,
  countByState,
  describeRuns,
  formatDuration,
  nextExpected,
  parseSse,
  relativeTime,
  sortChecks,
} from "/static/logic.js";

const KEY_STORAGE = "tocsin-key";
const THEME_STORAGE = "tocsin-theme";
const REFRESH_DELAY_MS = 250;
// Relative times and the derived "late" state change with the clock, not only
// with events, so the page also refreshes on a timer.
const POLL_MS = 30000;
const RUN_MARKS = { success: "●", fail: "✕", running: "◌", unfinished: "–" };
const STATE_ICONS = { down: "✕", late: "!", new: "…", up: "✓", paused: "❚❚" };

const $ = (id) => document.getElementById(id);

let apiKey = readStorage(KEY_STORAGE);
let checks = [];
let streamAbort = null;
let refreshTimer = null;
let pollTimer = null;

function readStorage(name) {
  try {
    return localStorage.getItem(name);
  } catch {
    return null;
  }
}

function writeStorage(name, value) {
  try {
    if (value === null) localStorage.removeItem(name);
    else localStorage.setItem(name, value);
  } catch {
    // Private windows can refuse storage; the page then works for this visit only.
  }
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// Everything that comes from the server is put on the page as text, never as
// HTML: check names and job output are written by other people and programs.

function renderSummary() {
  const counts = countByState(checks);
  const summary = $("summary");
  summary.replaceChildren(
    ...STATE_ORDER.map((state) => {
      const item = el("li", `summary-item state-${state}${counts[state] ? "" : " zero"}`);
      item.append(el("span", "summary-count", String(counts[state])), el("span", "summary-label", STATE_LABELS[state]));
      return item;
    }),
  );
}

function stateBadge(state) {
  const badge = el("span", `badge state-${state}`);
  badge.append(el("span", "badge-icon", STATE_ICONS[state] ?? "?"), el("span", "", STATE_LABELS[state] ?? state));
  badge.firstChild.setAttribute("aria-hidden", "true");
  return badge;
}

function runStrip(runs) {
  const strip = el("span", "runs");
  strip.setAttribute("role", "img");
  strip.setAttribute("aria-label", describeRuns(runs));
  for (const run of runs) {
    const mark = el("span", `run run-${run.status}`, RUN_MARKS[run.status] ?? "?");
    const detail = run.exit_code === null ? "" : `, exit ${run.exit_code}`;
    mark.title = `${run.status}${detail}, ${formatDuration(run.duration_ms)}, ${relativeTime(run.finished_at ?? run.started_at)}`;
    strip.append(mark);
  }
  return strip;
}

function checkRow(check) {
  const row = el("li", `check state-${check.state}`);
  const name = el("div", "check-name");
  name.append(el("span", "check-title", check.name), el("span", "check-schedule", `${check.schedule} · ${check.timezone}`));

  const lastRun = check.recent_runs.at(-1);
  const last = el("div", "check-cell");
  last.append(el("span", "cell-label", "Last ping"), el("span", "", relativeTime(check.last_ping_at)));
  if (lastRun && lastRun.duration_ms !== null) {
    last.append(el("span", "cell-sub", `took ${formatDuration(lastRun.duration_ms)}`));
  }

  const next = el("div", "check-cell");
  next.append(el("span", "cell-label", "Next expected"), el("span", "", nextExpected(check)));

  const runs = el("div", "check-cell check-runs");
  runs.append(el("span", "cell-label", "Recent runs"), runStrip(check.recent_runs));

  row.append(name, stateBadge(check.state), last, next, runs);
  return row;
}

function render() {
  renderSummary();
  const sorted = sortChecks(checks);
  $("checks").replaceChildren(...sorted.map(checkRow));
  $("empty").hidden = checks.length > 0;
}

function show(view) {
  $("login").hidden = view !== "login";
  $("overview").hidden = view !== "overview";
  $("sign-out").hidden = view !== "overview";
  $("live").hidden = view !== "overview";
}

function setLive(isLive) {
  const pill = $("live");
  pill.className = `status-pill ${isLive ? "live" : "offline"}`;
  pill.querySelector(".status-text").textContent = isLive ? "Live" : "Reconnecting";
}

function signOut(message) {
  apiKey = null;
  writeStorage(KEY_STORAGE, null);
  streamAbort?.abort();
  clearInterval(pollTimer);
  clearTimeout(refreshTimer);
  checks = [];
  const error = $("login-error");
  error.hidden = !message;
  error.textContent = message ?? "";
  show("login");
  $("key").focus();
}

async function api(path, key = apiKey) {
  const response = await fetch(path, { headers: { Authorization: `Bearer ${key}` } });
  if (response.status === 401) throw new Error("unauthorized");
  if (!response.ok) throw new Error(`the server answered ${response.status}`);
  return response;
}

async function refresh() {
  try {
    checks = await (await api("/api/overview")).json();
    $("load-error").hidden = true;
    render();
  } catch (error) {
    if (error.message === "unauthorized") return signOut("That key was not accepted.");
    const box = $("load-error");
    box.hidden = false;
    box.textContent = `Could not load checks: ${error.message}`;
  }
}

function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(refresh, REFRESH_DELAY_MS);
}

// Reads /api/stream with fetch, since EventSource cannot send the API key
// header, and reconnects with a growing delay if it drops.
async function followStream() {
  streamAbort?.abort();
  const abort = new AbortController();
  streamAbort = abort;
  let delay = 1000;

  while (!abort.signal.aborted) {
    try {
      const response = await fetch("/api/stream", {
        headers: { Authorization: `Bearer ${apiKey}` },
        signal: abort.signal,
      });
      if (response.status === 401) return signOut("That key was not accepted.");
      if (!response.ok || !response.body) throw new Error(`status ${response.status}`);

      setLive(true);
      delay = 1000;
      // Anything missed while disconnected: read the current state again.
      scheduleRefresh();
      const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        const parsed = parseSse(buffer + value);
        buffer = parsed.rest;
        if (parsed.events.some(({ event }) => event.startsWith("check."))) scheduleRefresh();
      }
    } catch (error) {
      if (abort.signal.aborted) return;
    }
    setLive(false);
    await new Promise((resolve) => setTimeout(resolve, delay));
    delay = Math.min(delay * 2, 30000);
  }
}

async function start() {
  show("overview");
  setLive(false);
  await refresh();
  if (!apiKey) return;
  clearInterval(pollTimer);
  pollTimer = setInterval(refresh, POLL_MS);
  followStream();
}

function setupTheme() {
  const button = $("theme-toggle");
  const dark = window.matchMedia("(prefers-color-scheme: dark)");
  const isDark = () => {
    const chosen = document.documentElement.dataset.theme;
    return chosen ? chosen === "dark" : dark.matches;
  };
  const sync = () => {
    button.querySelector(".theme-icon").textContent = isDark() ? "☀" : "☾";
    button.querySelector(".theme-label").textContent = isDark() ? "Light" : "Dark";
    button.setAttribute("aria-label", isDark() ? "Switch to light mode" : "Switch to dark mode");
  };
  button.addEventListener("click", () => {
    const next = isDark() ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    writeStorage(THEME_STORAGE, next);
    sync();
  });
  dark.addEventListener("change", sync);
  sync();
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const key = $("key").value.trim();
  if (!key) return;
  try {
    await api("/api/overview", key);
  } catch (error) {
    const box = $("login-error");
    box.hidden = false;
    box.textContent =
      error.message === "unauthorized" ? "That key was not accepted." : `Could not reach tocsin: ${error.message}`;
    return;
  }
  apiKey = key;
  writeStorage(KEY_STORAGE, key);
  $("key").value = "";
  $("login-error").hidden = true;
  start();
});

$("sign-out").addEventListener("click", () => signOut());

setupTheme();
if (apiKey) start();
else show("login");
