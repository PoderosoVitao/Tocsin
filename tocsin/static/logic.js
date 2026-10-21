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
