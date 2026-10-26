// Pure functions for the dashboard: no DOM, no network, so they can be run
// and checked on their own.

// Problems sort to the top: down first, then late, then checks still waiting
// for a first ping, then healthy ones, then paused ones.
export const STATE_ORDER = ["down", "late", "new", "up", "paused"];

export const STATE_LABELS = {
  down: "Down",
  late: "Late",
  new: "Waiting",
  up: "Up",
  paused: "Paused",
};

export function sortChecks(checks) {
  const rank = (check) => {
    const index = STATE_ORDER.indexOf(check.state);
    return index === -1 ? STATE_ORDER.length : index;
  };
  return [...checks].sort(
    (a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name, undefined, { sensitivity: "base" }),
  );
}

export function countByState(checks) {
  const counts = Object.fromEntries(STATE_ORDER.map((state) => [state, 0]));
  for (const check of checks) {
    counts[check.state] = (counts[check.state] ?? 0) + 1;
  }
  return counts;
}

const UNITS = [
  ["d", 86400],
  ["h", 3600],
  ["m", 60],
  ["s", 1],
];

// "45s", "31m", "2h 5m": the two largest units that matter.
export function formatSpan(seconds) {
  let left = Math.max(0, Math.round(seconds));
  const parts = [];
  for (const [unit, size] of UNITS) {
    const value = Math.floor(left / size);
    left -= value * size;
    if (value > 0) parts.push(`${value}${unit}`);
  }
  return parts.slice(0, 2).join(" ") || "0s";
}

// "5m ago" or "in 2h"; "never" when there is no time.
export function relativeTime(iso, now = Date.now()) {
  if (!iso) return "never";
  const seconds = (new Date(iso).getTime() - now) / 1000;
  if (Math.abs(seconds) < 5) return "just now";
  return seconds < 0 ? `${formatSpan(-seconds)} ago` : `in ${formatSpan(seconds)}`;
}

export function formatDuration(ms) {
  if (ms === null || ms === undefined) return "no duration";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return formatSpan(ms / 1000);
}

// What the "next expected" column shows, which depends on the state.
export function nextExpected(check, now = Date.now()) {
  if (check.state === "paused") return "paused";
  if (check.state === "new") return "waiting for the first ping";
  if (check.state === "down") return "—";
  if (!check.due_at) return "unknown";
  const overdue = (now - new Date(check.due_at).getTime()) / 1000;
  return overdue > 0 ? `overdue by ${formatSpan(overdue)}` : relativeTime(check.due_at, now);
}

// Splits a Server-Sent Events text stream into events. Returns the events in
// `buffer` that are complete, and the unfinished remainder to prepend to the
// next chunk. Comment lines (": keepalive") are dropped.
export function parseSse(buffer) {
  const blocks = buffer.split("\n\n");
  const rest = blocks.pop() ?? "";
  const events = [];
  for (const block of blocks) {
    let event = "message";
    const data = [];
    for (const line of block.split("\n")) {
      if (line.startsWith(":")) continue;
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
    }
    if (data.length === 0 && event === "message") continue;
    let parsed = null;
    try {
      parsed = data.length ? JSON.parse(data.join("\n")) : null;
    } catch {
      continue;
    }
    events.push({ event, data: parsed });
  }
  return { events, rest };
}

// How the run strip describes itself to a screen reader.
export function describeRuns(runs) {
  if (runs.length === 0) return "No runs yet";
  const failed = runs.filter((run) => run.status === "fail").length;
  const passed = runs.filter((run) => run.status === "success").length;
  return `Last ${runs.length} runs, oldest first: ${passed} passed, ${failed} failed`;
}

// "#/checks/<uuid>" is a check's page; anything else is the overview. Alert
// messages link to the first form.
export function parseRoute(hash) {
  const match = /^#\/checks\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/i.exec(hash ?? "");
  return match ? { page: "check", id: match[1].toLowerCase() } : { page: "overview" };
}

export function plural(count, word) {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

// One line for a state change: "Up → Down", or "Still down" for a reminder.
export function describeTransition(event) {
  if (event.from_state === "down" && event.to_state === "down") return "Still down";
  return `${STATE_LABELS[event.from_state] ?? event.from_state} → ${STATE_LABELS[event.to_state] ?? event.to_state}`;
}

// What happened to one alert on one channel, in words, including why it
// failed and when it will be tried again.
export function describeDelivery(delivery, now = Date.now()) {
  const where = delivery.channel_name;
  const tries = plural(delivery.attempts, "attempt");
  const error = delivery.last_error ? `: ${delivery.last_error}` : "";
  switch (delivery.status) {
    case "sent":
      return delivery.attempts > 1 ? `Sent to ${where} after ${tries}` : `Sent to ${where}`;
    case "failed":
      return `Could not send to ${where} after ${tries}${error}`;
    case "retrying":
      return `Retrying ${where}, ${tries} so far${error}; next try ${relativeTime(delivery.next_attempt_at, now)}`;
    default:
      return `Queued for ${where}`;
  }
}

// Heights (percent) for a bar chart of run durations. Runs that have no
// duration (a finish with no start, or one still running) get a stub so they
// still show.
export function durationBars(runs) {
  const longest = Math.max(0, ...runs.map((run) => run.duration_ms ?? 0));
  return runs.map((run) => ({
    run,
    height: run.duration_ms && longest ? Math.max(6, Math.round((run.duration_ms / longest) * 100)) : 6,
  }));
}

// Combines a fresh read of the newest `pageSize` items (newest first, ids
// descending) with the older ones already on screen, so a refresh adds what is
// new without dropping what "show older" loaded.
//
// If the fresh page came back short, it is everything there is. If it is full
// and no longer overlaps what is on screen (more than a page arrived since),
// the old items would leave a gap, so they are dropped and the person can load
// older ones again.
export function mergeNewest(existing, fresh, pageSize) {
  if (fresh.length < pageSize) return { items: fresh, more: false };
  const floor = fresh[fresh.length - 1].id;
  if (existing.length === 0 || existing[0].id < floor) return { items: fresh, more: true };
  const older = existing.filter((item) => item.id < floor);
  return { items: [...fresh, ...older], more: older.length > 0 ? null : true };
}
