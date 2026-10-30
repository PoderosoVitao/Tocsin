import {
  ALERT_FILTERS,
  STATE_LABELS,
  STATE_ORDER,
  checkToForm,
  countByState,
  describeChannel,
  describeDelivery,
  describeRuns,
  describeTransition,
  durationBars,
  errorMessage,
  formToCheckBody,
  formatDuration,
  formatSpan,
  mergeNewest,
  nextExpected,
  parseRoute,
  parseSse,
  relativeTime,
  shouldRefresh,
  sortChecks,
} from "/static/logic.js";

const KEY_STORAGE = "tocsin-key";
const THEME_STORAGE = "tocsin-theme";
const REFRESH_DELAY_MS = 250;
// Relative times and the derived "late" state change with the clock, not only
// with events, so the page also refreshes on a timer.
const POLL_MS = 30000;
const PAGE_SIZE = 30;
const RUN_MARKS = { success: "●", fail: "✕", running: "◌", unfinished: "–" };
const STATE_ICONS = { down: "✕", late: "!", new: "…", up: "✓", paused: "❚❚" };

const $ = (id) => document.getElementById(id);

let apiKey = readStorage(KEY_STORAGE);
let route = parseRoute(location.hash);
let checks = [];
let detail = null; // { check, runs, events, moreRuns, moreEvents } for the open check page
let alertLog = { status: "", items: [], more: false }; // the alert log page
let settings = { checks: [], channels: [], keys: [] }; // the settings page
let editing = null; // the check being edited in the dialog, or null for a new one
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

function when(iso) {
  return iso ? new Date(iso).toLocaleString() : "never";
}

// Everything that comes from the server is put on the page as text, never as
// HTML: check names and job output are written by other people and programs.

function renderSummary() {
  const counts = countByState(checks);
  $("summary").replaceChildren(
    ...STATE_ORDER.map((state) => {
      const item = el("li", `summary-item state-${state}${counts[state] ? "" : " zero"}`);
      item.append(el("span", "summary-count", String(counts[state])), el("span", "summary-label", STATE_LABELS[state]));
      return item;
    }),
  );
}

function stateBadge(state) {
  const badge = el("span", `badge state-${state}`);
  const icon = el("span", "badge-icon", STATE_ICONS[state] ?? "?");
  icon.setAttribute("aria-hidden", "true");
  badge.append(icon, el("span", "", STATE_LABELS[state] ?? state));
  return badge;
}

function runStrip(runs) {
  const strip = el("span", "runs");
  strip.setAttribute("role", "img");
  strip.setAttribute("aria-label", describeRuns(runs));
  for (const run of runs) {
    const mark = el("span", `run run-${run.status}`, RUN_MARKS[run.status] ?? "?");
    const detailText = run.exit_code === null ? "" : `, exit ${run.exit_code}`;
    mark.title = `${run.status}${detailText}, ${formatDuration(run.duration_ms)}, ${relativeTime(run.finished_at ?? run.started_at)}`;
    strip.append(mark);
  }
  return strip;
}

function checkRow(check) {
  const row = el("li", `check state-${check.state}`);
  const name = el("div", "check-name");
  const title = el("span", "check-title");
  const link = el("a", "", check.name);
  link.href = `#/checks/${check.id}`;
  title.append(link);
  name.append(title, el("span", "check-schedule", `${check.schedule} · ${check.timezone}`));

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

function renderOverview() {
  renderSummary();
  $("checks").replaceChildren(...sortChecks(checks).map(checkRow));
  $("empty").hidden = checks.length > 0;
}

function fact(label, value) {
  const item = el("div", "fact");
  item.append(el("dt", "", label), el("dd", "", value));
  return item;
}

function runRow(run) {
  const row = el("li", "row");
  const main = el("div", "row-main");
  main.append(
    el("span", `run run-${run.status}`, RUN_MARKS[run.status] ?? "?"),
    el("strong", "", run.status),
    el("span", "", formatDuration(run.duration_ms)),
  );
  if (run.exit_code !== null) main.append(el("span", "tag", `exit ${run.exit_code}`));
  const time = el("span", "row-time", when(run.finished_at ?? run.started_at));
  time.title = relativeTime(run.finished_at ?? run.started_at);
  main.append(time);
  row.append(main);
  if (run.output_tail) {
    const output = el("details");
    output.append(el("summary", "", "Output"), el("pre", "output", run.output_tail));
    row.append(output);
  }
  return row;
}

function eventRow(event) {
  const row = el("li", `row${event.suppressed ? " suppressed" : ""}`);
  const main = el("div", "row-main");
  main.append(el("strong", "", describeTransition(event)));
  if (event.suppressed) main.append(el("span", "tag", "held back by the cooldown"));
  const time = el("span", "row-time", when(event.created_at));
  time.title = relativeTime(event.created_at);
  main.append(time);
  row.append(main, el("div", "row-sub", event.reason));
  if (event.deliveries.length) {
    const list = el("ul", "deliveries");
    for (const delivery of event.deliveries) {
      list.append(el("li", `delivery ${delivery.status}`, describeDelivery(delivery)));
    }
    row.append(list);
  }
  return row;
}

function renderDetail() {
  if (!detail) return;
  const { check, runs, events } = detail;
  $("detail-title").textContent = check.name;
  $("detail-state").replaceChildren(stateBadge(check.state));
  $("detail-facts").replaceChildren(
    fact("Schedule", `${check.schedule} (${check.timezone})`),
    fact("Next expected", nextExpected(check)),
    fact("Last ping", `${relativeTime(check.last_ping_at)}${check.last_ping_at ? ` · ${when(check.last_ping_at)}` : ""}`),
    fact("Grace period", formatSpan(check.grace_seconds)),
    fact("Longest run allowed", check.max_duration_seconds ? formatSpan(check.max_duration_seconds) : "no limit"),
    fact("Cooldown", check.cooldown_seconds ? formatSpan(check.cooldown_seconds) : "none"),
    fact("Reminders while down", check.reminder_seconds ? `every ${formatSpan(check.reminder_seconds)}` : "off"),
    fact("Alerts go to", `${check.channel_ids.length} channel${check.channel_ids.length === 1 ? "" : "s"}`),
    fact("Ping URL", check.ping_url),
  );

  // Oldest first, like the overview's strip: the newest run is on the right.
  const oldestFirst = [...runs].reverse();
  const bars = $("run-bars");
  bars.hidden = runs.length === 0;
  bars.setAttribute("aria-label", `Run durations. ${describeRuns(oldestFirst.slice(-20))}`);
  bars.replaceChildren(
    ...durationBars(oldestFirst).map(({ run, height }) => {
      const bar = el("span", `bar bar-${run.status}`);
      bar.style.height = `${height}%`;
      bar.title = `${run.status}, ${formatDuration(run.duration_ms)}, ${relativeTime(run.finished_at ?? run.started_at)}`;
      return bar;
    }),
  );
  $("run-list").replaceChildren(...runs.map(runRow));
  $("runs-empty").hidden = runs.length > 0;
  $("more-runs").hidden = !detail.moreRuns;

  $("event-list").replaceChildren(...events.map(eventRow));
  $("events-empty").hidden = events.length > 0;
  $("more-events").hidden = !detail.moreEvents;
}

function show(view) {
  $("login").hidden = view !== "login";
  $("overview").hidden = view !== "overview";
  $("check-page").hidden = view !== "check";
  $("alerts-page").hidden = view !== "alerts";
  $("settings-page").hidden = view !== "settings";
  const signedIn = view !== "login";
  $("sign-out").hidden = !signedIn;
  $("live").hidden = !signedIn;
  $("nav").hidden = !signedIn;
  // A check's page belongs to the "Checks" section of the navigation.
  const section = view === "check" ? "overview" : view;
  for (const link of $("nav").querySelectorAll("a")) {
    if (link.dataset.route === section) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
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
  detail = null;
  $("new-key-notice").hidden = true;
  $("new-key-value").value = "";
  const error = $("login-error");
  error.hidden = !message;
  error.textContent = message ?? "";
  show("login");
  $("key").focus();
}

async function api(path, key = apiKey) {
  const response = await fetch(path, { headers: { Authorization: `Bearer ${key}` } });
  if (response.status === 401) throw new Error("unauthorized");
  if (response.status === 404) throw new Error("not found");
  if (!response.ok) throw new Error(`the server answered ${response.status}`);
  return response;
}

async function refreshOverview() {
  try {
    checks = await (await api("/api/overview")).json();
    $("load-error").hidden = true;
    renderOverview();
  } catch (error) {
    if (error.message === "unauthorized") return signOut("That key was not accepted.");
    const box = $("load-error");
    box.hidden = false;
    box.textContent = `Could not load checks: ${error.message}`;
  }
}

// Loads one check with its newest runs and events, and merges them into what
// is already on screen so a refresh never undoes "show older".
async function refreshDetail() {
  const id = route.id;
  try {
    const [check, runs, events] = await Promise.all([
      api(`/api/checks/${id}`).then((r) => r.json()),
      api(`/api/checks/${id}/runs?limit=${PAGE_SIZE}`).then((r) => r.json()),
      api(`/api/checks/${id}/events?limit=${PAGE_SIZE}`).then((r) => r.json()),
    ]);
    if (route.id !== id) return; // the person moved on while this was loading
    const same = detail?.check.id === id;
    const mergedRuns = mergeNewest(same ? detail.runs : [], runs, PAGE_SIZE);
    const mergedEvents = mergeNewest(same ? detail.events : [], events, PAGE_SIZE);
    detail = {
      check,
      runs: mergedRuns.items,
      events: mergedEvents.items,
      // null means the older items already loaded keep the answer they had.
      moreRuns: mergedRuns.more ?? detail.moreRuns,
      moreEvents: mergedEvents.more ?? detail.moreEvents,
    };
    $("detail-error").hidden = true;
    renderDetail();
  } catch (error) {
    if (error.message === "unauthorized") return signOut("That key was not accepted.");
    const box = $("detail-error");
    box.hidden = false;
    box.textContent =
      error.message === "not found" ? "This check no longer exists." : `Could not load this check: ${error.message}`;
  }
}

function alertRow(alert) {
  const row = el("li", "row");
  const main = el("div", "row-main");
  const link = el("a", "", alert.check_name);
  link.href = `#/checks/${alert.check_id}`;
  const time = el("span", "row-time", when(alert.created_at));
  time.title = relativeTime(alert.created_at);
  main.append(link, el("span", `delivery ${alert.status}`, describeDelivery(alert)), time);
  row.append(main, el("div", "row-sub", `${describeTransition(alert)}: ${alert.reason}`));
  return row;
}

function renderAlerts() {
  $("alert-list").replaceChildren(...alertLog.items.map(alertRow));
  const empty = $("alerts-empty");
  empty.hidden = alertLog.items.length > 0;
  empty.textContent = alertLog.status
    ? "No alerts match this filter."
    : "No alerts yet. They appear when a check goes down or recovers.";
  $("more-alerts").hidden = !alertLog.more;
}

async function refreshAlerts() {
  const status = alertLog.status;
  const filter = status ? `&status=${status}` : "";
  try {
    const fresh = await (await api(`/api/alerts?limit=${PAGE_SIZE}${filter}`)).json();
    if (route.page !== "alerts" || alertLog.status !== status) return; // moved on meanwhile
    const merged = mergeNewest(alertLog.items, fresh, PAGE_SIZE);
    alertLog = { status, items: merged.items, more: merged.more ?? alertLog.more };
    $("alerts-error").hidden = true;
    renderAlerts();
  } catch (error) {
    if (error.message === "unauthorized") return signOut("That key was not accepted.");
    const box = $("alerts-error");
    box.hidden = false;
    box.textContent = `Could not load the alert log: ${error.message}`;
  }
}

async function loadMoreAlerts() {
  const last = alertLog.items.at(-1);
  if (!last) return;
  const status = alertLog.status;
  const filter = status ? `&status=${status}` : "";
  try {
    const more = await (await api(`/api/alerts?limit=${PAGE_SIZE}&before=${last.id}${filter}`)).json();
    if (alertLog.status !== status) return;
    alertLog = { status, items: [...alertLog.items, ...more], more: more.length === PAGE_SIZE };
    renderAlerts();
  } catch (error) {
    if (error.message === "unauthorized") signOut("That key was not accepted.");
  }
}

async function loadMore(kind) {
  const items = detail[kind];
  const last = items.at(-1);
  if (!last) return;
  try {
    const more = await (await api(`/api/checks/${detail.check.id}/${kind}?limit=${PAGE_SIZE}&before=${last.id}`)).json();
    detail[kind] = [...items, ...more];
    detail[kind === "runs" ? "moreRuns" : "moreEvents"] = more.length === PAGE_SIZE;
    renderDetail();
  } catch (error) {
    if (error.message === "unauthorized") signOut("That key was not accepted.");
  }
}

const refreshers = {
  check: refreshDetail,
  alerts: refreshAlerts,
  overview: refreshOverview,
  settings: refreshSettings,
};
const refresh = () => refreshers[route.page]();

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
        for (const { event, data } of parsed.events) {
          if (shouldRefresh(route, event, data)) scheduleRefresh();
        }
      }
    } catch (error) {
      if (abort.signal.aborted) return;
    }
    setLive(false);
    await new Promise((resolve) => setTimeout(resolve, delay));
    delay = Math.min(delay * 2, 30000);
  }
}

// Sends a change to the API. Resolves to the response body (null for 204) or
// throws an Error with a message fit to show to a person.
async function send(method, path, body) {
  const response = await fetch(path, {
    method,
    headers: { Authorization: `Bearer ${apiKey}`, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (response.status === 401) throw new Error("unauthorized");
  if (response.status === 204) return null;
  const payload = await response.json().catch(() => null);
  if (!response.ok) throw new Error(errorMessage(response.status, payload));
  return payload;
}

function showFormError(id, error) {
  if (error.message === "unauthorized") return signOut("That key was not accepted.");
  const box = $(id);
  box.hidden = false;
  box.textContent = error.message;
}

function actionButton(label, onClick, className = "") {
  const button = el("button", `btn ghost small ${className}`.trim(), label);
  button.type = "button";
  button.addEventListener("click", onClick);
  return button;
}

function settingsCheckRow(check) {
  const row = el("li", "row");
  const main = el("div", "row-main");
  const link = el("a", "", check.name);
  link.href = `#/checks/${check.id}`;
  main.append(link, stateBadge(check.state), el("span", "check-schedule", `${check.schedule} · ${check.timezone}`));
  const actions = el("div", "row-actions");
  const paused = check.state === "paused";
  actions.append(
    actionButton("Edit", () => openCheckDialog(check)),
    actionButton(paused ? "Resume" : "Pause", () => changeCheck(check, { paused: !paused })),
    actionButton("Delete", () => deleteCheck(check), "danger"),
  );
  row.append(main, actions);
  return row;
}

function settingsChannelRow(channel) {
  const row = el("li", "row");
  const main = el("div", "row-main");
  main.append(el("strong", "", channel.name), el("span", "check-schedule", describeChannel(channel)));
  const status = el("span", "row-status");
  status.setAttribute("aria-live", "polite");
  const actions = el("div", "row-actions");
  actions.append(
    actionButton("Send test message", () => testChannel(channel, status)),
    actionButton("Delete", () => deleteChannel(channel), "danger"),
    status,
  );
  row.append(main, actions);
  return row;
}

function settingsKeyRow(key) {
  const row = el("li", "row");
  const main = el("div", "row-main");
  const mine = apiKey && apiKey.startsWith(key.prefix);
  main.append(el("strong", "", key.name), el("span", "check-schedule", `${key.prefix}…`));
  if (mine) main.append(el("span", "tag", "this browser"));
  if (key.revoked_at) main.append(el("span", "tag", `revoked ${relativeTime(key.revoked_at)}`));
  const detail = `Created ${when(key.created_at)} · ${key.last_used_at ? `last used ${relativeTime(key.last_used_at)}` : "never used"}`;
  row.append(main, el("div", "row-sub", detail));
  if (!key.revoked_at) {
    const revoke = actionButton("Revoke", () => revokeKey(key), "danger");
    if (mine) {
      revoke.disabled = true;
      revoke.title = "This is the key you are signed in with";
    }
    const actions = el("div", "row-actions");
    actions.append(revoke);
    row.append(actions);
  }
  return row;
}

function renderSettings() {
  const { checks: list, channels, keys } = settings;
  $("settings-checks").replaceChildren(...sortChecks(list).map(settingsCheckRow));
  $("settings-checks-empty").hidden = list.length > 0;
  $("settings-channels").replaceChildren(...channels.map(settingsChannelRow));
  $("settings-channels-empty").hidden = channels.length > 0;
  $("settings-keys").replaceChildren(...keys.map(settingsKeyRow));
}

async function refreshSettings() {
  try {
    const [checkList, channels, keys] = await Promise.all([
      api("/api/checks").then((r) => r.json()),
      api("/api/channels").then((r) => r.json()),
      api("/api/keys").then((r) => r.json()),
    ]);
    if (route.page !== "settings") return;
    settings = { checks: checkList, channels, keys };
    $("settings-error").hidden = true;
    renderSettings();
  } catch (error) {
    if (error.message === "unauthorized") return signOut("That key was not accepted.");
    const box = $("settings-error");
    box.hidden = false;
    box.textContent = `Could not load settings: ${error.message}`;
  }
}

// Runs a change from a button, then reloads; a failure is shown on the page.
async function act(work) {
  try {
    await work();
    $("settings-error").hidden = true;
  } catch (error) {
    if (error.message === "unauthorized") return signOut("That key was not accepted.");
    const box = $("settings-error");
    box.hidden = false;
    box.textContent = error.message;
  }
  await refreshSettings();
}

const changeCheck = (check, patch) => act(() => send("PATCH", `/api/checks/${check.id}`, patch));

function deleteCheck(check) {
  if (!confirm(`Delete "${check.name}"? Its run history and alerts are deleted with it.`)) return;
  return act(() => send("DELETE", `/api/checks/${check.id}`));
}

function deleteChannel(channel) {
  if (!confirm(`Delete the channel "${channel.name}"? Checks stop alerting it.`)) return;
  return act(() => send("DELETE", `/api/channels/${channel.id}`));
}

function revokeKey(key) {
  if (!confirm(`Revoke the key "${key.name}"? Anything using it stops working immediately.`)) return;
  return act(() => send("DELETE", `/api/keys/${key.id}`));
}

async function testChannel(channel, status) {
  status.className = "row-status";
  status.textContent = "Sending…";
  try {
    await send("POST", `/api/channels/${channel.id}/test`);
    status.className = "row-status good";
    status.textContent = "Sent. Check the chat.";
  } catch (error) {
    if (error.message === "unauthorized") return signOut("That key was not accepted.");
    status.className = "row-status bad";
    status.textContent = error.message;
  }
}

function openCheckDialog(check = null) {
  editing = check;
  const values = check
    ? checkToForm(check)
    : {
        name: "",
        schedule: "",
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
        grace: "5",
        maxRun: "",
        cooldown: "5",
        reminder: "",
      };
  $("check-dialog-title").textContent = check ? `Edit ${check.name}` : "New check";
  $("f-name").value = values.name;
  $("f-schedule").value = values.schedule;
  $("f-timezone").value = values.timezone;
  $("f-grace").value = values.grace;
  $("f-maxrun").value = values.maxRun;
  $("f-cooldown").value = values.cooldown;
  $("f-reminder").value = values.reminder;
  const chosen = new Set(check ? check.channel_ids : settings.channels.map((c) => c.id));
  $("f-channels").replaceChildren(
    ...(settings.channels.length
      ? settings.channels.map((channel) => {
          const label = el("label", "check-label");
          const box = el("input");
          box.type = "checkbox";
          box.value = channel.id;
          box.checked = chosen.has(channel.id);
          label.append(box, document.createTextNode(` ${channel.name}`));
          return label;
        })
      : [el("p", "hint", "No channels yet. Add one below to be alerted.")]),
  );
  $("check-form-error").hidden = true;
  $("check-dialog").showModal();
  $("f-name").focus();
}

$("check-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const body = formToCheckBody({
    name: $("f-name").value,
    schedule: $("f-schedule").value,
    timezone: $("f-timezone").value,
    grace: $("f-grace").value,
    maxRun: $("f-maxrun").value,
    cooldown: $("f-cooldown").value,
    reminder: $("f-reminder").value,
  });
  if (!body.name || !body.schedule || body.grace_seconds === null || body.cooldown_seconds === null) {
    return showFormError("check-form-error", new Error("Name, schedule, grace period and cooldown are required."));
  }
  if (settings.channels.length) {
    body.channel_ids = [...$("f-channels").querySelectorAll("input:checked")].map((box) => box.value);
  }
  try {
    if (editing) await send("PATCH", `/api/checks/${editing.id}`, body);
    else await send("POST", "/api/checks", body);
  } catch (error) {
    return showFormError("check-form-error", error);
  }
  $("check-dialog").close();
  refreshSettings();
});

$("channel-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const body = {
    kind: "telegram",
    name: $("c-name").value.trim(),
    config: { bot_token: $("c-token").value.trim(), chat_id: $("c-chat").value.trim() },
    attach_to_existing_checks: $("c-attach").checked,
  };
  if (!body.name) return showFormError("channel-form-error", new Error("Give the channel a name."));
  try {
    await send("POST", "/api/channels", body);
  } catch (error) {
    return showFormError("channel-form-error", error);
  }
  $("channel-form").reset();
  $("channel-dialog").close();
  refreshSettings();
});

$("key-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const name = $("k-name").value.trim();
  if (!name) return showFormError("key-form-error", new Error("Give the key a name."));
  try {
    const created = await send("POST", "/api/keys", { name });
    $("new-key-value").value = created.key;
    $("new-key-notice").hidden = false;
  } catch (error) {
    return showFormError("key-form-error", error);
  }
  $("key-form").reset();
  $("key-dialog").close();
  refreshSettings();
  $("new-key-value").select();
});

$("new-check").addEventListener("click", () => openCheckDialog());
$("new-channel").addEventListener("click", () => {
  $("channel-form-error").hidden = true;
  $("channel-dialog").showModal();
  $("c-name").focus();
});
$("new-key").addEventListener("click", () => {
  $("key-form-error").hidden = true;
  $("key-dialog").showModal();
  $("k-name").focus();
});
for (const button of document.querySelectorAll("dialog [data-close]")) {
  button.addEventListener("click", () => button.closest("dialog").close());
}

function showRoute() {
  route = parseRoute(location.hash);
  if (!apiKey) return show("login");
  detail = null;
  $("new-key-notice").hidden = true;
  $("new-key-value").value = "";
  alertLog = { status: alertLog.status, items: [], more: false };
  show(route.page);
  window.scrollTo(0, 0);
  refresh();
}

async function start() {
  setLive(false);
  showRoute();
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
$("more-runs").addEventListener("click", () => loadMore("runs"));
$("more-events").addEventListener("click", () => loadMore("events"));
$("more-alerts").addEventListener("click", loadMoreAlerts);
$("alert-filter").replaceChildren(
  ...ALERT_FILTERS.map(([value, label]) => {
    const option = el("option", "", label);
    option.value = value;
    return option;
  }),
);
$("alert-filter").addEventListener("change", (event) => {
  alertLog = { status: event.target.value, items: [], more: false };
  renderAlerts();
  refreshAlerts();
});
window.addEventListener("hashchange", () => {
  if (apiKey) showRoute();
});

setupTheme();
if (apiKey) start();
else show("login");
