from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

# Job output is about to be sent to a third party's model, and it can hold
# anything the job saw: tokens in a failing request, a password in a connection
# string, a private key in a stack trace. This scrubs it first.
#
# It errs towards removing too much. A summary that loses a hash or an email is
# a little worse; a leaked credential can't be taken back. What it can't catch
# is a secret that looks like ordinary text, such as a short or dictionary-word
# password with nothing around it to say it is one; the README says so.

DEFAULT_MAX_CHARS = 4000


@dataclass
class Redacted:
    text: str
    # How many things were removed, by kind, for the log of what was sent.
    removed: Counter[str] = field(default_factory=Counter)
    # True when the start of the output was dropped to fit the size cap.
    truncated: bool = False

    @property
    def count(self) -> int:
        return sum(self.removed.values())


def _mark(kind: str) -> str:
    return f"[REDACTED:{kind}]"


# Patterns for credentials with a recognisable shape: (kind, pattern). The whole
# match is replaced.
_TOKEN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "private-key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY"
            r"(?: BLOCK)?-----|\Z)",
            re.DOTALL,
        ),
    ),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("aws-key", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA|AIPA)[A-Z0-9]{16}\b")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("api-key", re.compile(r"\bsk-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_-]{16,}")),
    ("slack-token", re.compile(r"\bxox[abeoprs]-[A-Za-z0-9-]{10,}")),
    ("google-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("stripe-key", re.compile(r"\b[rs]k_(?:live|test)_[0-9A-Za-z]{16,}")),
    ("telegram-token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("tocsin-key", re.compile(r"\btsn_[A-Za-z0-9_-]{16,}")),
    # A ping URL is itself the secret that lets anyone report for that check.
    (
        "ping-url",
        re.compile(
            r"(?<=/ping/)[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
        ),
    ),
]

# user:password@ inside a URL.
_URL_CREDENTIALS = re.compile(r"(?P<scheme>\b[a-zA-Z][a-zA-Z0-9+.-]*://)[^\s/@:]*:[^\s/@]+@")

# Headers that carry credentials: everything after the colon up to the end of the
# line or the closing quote, since their values contain spaces (Authorization:
# Bearer abc, Cookie: a=1; b=2).
_CREDENTIAL_HEADER = re.compile(
    r"(?P<name>\b(?:proxy-)?authorization|\b(?:set-)?cookie|\bx-api-key|\bx-auth-token)"
    r"(?P<sep>\s*:[ \t]*)(?P<value>[^\n\"']+)",
    re.IGNORECASE,
)
# What a header's value is once it has been redacted, so a second pass over the
# same text leaves it alone. Only this exact shape is skipped: anything else,
# even a line that happens to mention a marker, is still scrubbed.
_ALREADY_REDACTED = re.compile(r"(?:(?:bearer|basic|digest) )?\[REDACTED:auth-header\]\s*", re.I)
# "Bearer abc" wherever it turns up (a header echoed inside a JSON dump, say).
_SCHEME_TOKEN = re.compile(
    r"(?P<label>\b(?:bearer|basic|digest)\s+)(?P<value>[A-Za-z0-9._~+/=-]{8,})", re.IGNORECASE
)

# A name that says its value is a secret, followed by =, : or a space and the
# value: PASSWORD=hunter2, "api_key": "abc", --token abc, aws_secret_access_key = abc.
#
# The keyword has to be a word of its own in the name (DB_PASSWORD, clientSecret,
# api-key) and not the start or middle of another one: "passed: 5" and
# "tokens: 812" are not secrets. It also may not be followed by a lowercase
# letter, so "secrets" and "author" don't count, while "passwordHash" does.
_KEYWORD = (
    r"(?:(?<![A-Za-z])|(?=[A-Z]))(?i:pass(?:word|wd|phrase)?|pwd|secret|token|api[_-]?key"
    r"|apikey|access[_-]?key|private[_-]?key|credentials?|auth|signature|session[_-]?id)(?![a-z])"
)
_SECRET_NAME = rf"[\w.-]{{0,40}}?{_KEYWORD}[\w.-]{{0,40}}"
_SECRET_ASSIGNMENT = re.compile(
    rf"(?P<name>{_SECRET_NAME})(?P<quote>[\"']?)(?P<sep>\s*[:=]\s*)"
    r"(?P<value>\"[^\"\n]*\"|'[^'\n]*'|[^\s,;&\"')\]}]+)",
)
_SECRET_FLAG = re.compile(
    rf"(?P<name>--{_SECRET_NAME})(?P<sep>[ =])(?P<value>\"[^\"\n]*\"|'[^'\n]*'|[^\s\"']+)",
)

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

# Long runs of characters that base64, hex and URL-safe tokens are made of.
_CANDIDATE = re.compile(r"[A-Za-z0-9+/=_-]{20,}")
MIN_ENTROPY = 3.6


def _entropy(text: str) -> float:
    counts = Counter(text)
    return -sum(n / len(text) * math.log2(n / len(text)) for n in counts.values())


# Whether a long run of token characters looks like a random secret and not a
# word, a path or a slug. It needs letters and digits and enough entropy. If it
# also contains separators (/ - _), as paths, slugs and UUIDs do, it must mix
# upper and lower case, as base64 does and ordinary lowercase names don't.
def _looks_random(candidate: str) -> bool:
    if not (any(c.isdigit() for c in candidate) and any(c.isalpha() for c in candidate)):
        return False
    if _entropy(candidate) < MIN_ENTROPY:
        return False
    if any(c in "/-_" for c in candidate):
        return any(c.isupper() for c in candidate) and any(c.islower() for c in candidate)
    return True


# Removes secrets, e-mail addresses and terminal colour codes from job output,
# then keeps its last `max_chars` characters (the end is where errors are).
#
# Redaction is done on the whole text before it is cut, so a secret that
# straddles the cut can't survive as a fragment.
def redact(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> Redacted:
    removed: Counter[str] = Counter()
    text = _ANSI.sub("", text)

    def replace(kind: str, pattern: re.Pattern[str]) -> None:
        nonlocal text

        def swap(match: re.Match[str]) -> str:
            removed[kind] += 1
            return _mark(kind)

        text = pattern.sub(swap, text)

    for kind, pattern in _TOKEN_PATTERNS:
        replace(kind, pattern)

    def credentials(match: re.Match[str]) -> str:
        if match[0] == f"{match['scheme']}{_mark('url-credentials')}@":
            return match[0]  # redacted already, on an earlier pass
        removed["url-credentials"] += 1
        return f"{match['scheme']}{_mark('url-credentials')}@"

    text = _URL_CREDENTIALS.sub(credentials, text)

    def header(match: re.Match[str]) -> str:
        if _ALREADY_REDACTED.fullmatch(match["value"]):
            return match[0]
        removed["auth-header"] += 1
        return f"{match['name']}{match['sep']}{_mark('auth-header')}"

    text = _CREDENTIAL_HEADER.sub(header, text)

    def scheme(match: re.Match[str]) -> str:
        if match["value"].startswith("[REDACTED"):
            return match[0]
        removed["auth-header"] += 1
        return f"{match['label']}{_mark('auth-header')}"

    text = _SCHEME_TOKEN.sub(scheme, text)

    def assignment(match: re.Match[str]) -> str:
        value = match["value"]
        # Already removed above, or a word that only names something ("token: expired").
        if "[REDACTED" in value or not value.strip("\"'"):
            return match[0]
        removed["secret-value"] += 1
        quote = match.groupdict().get("quote") or ""
        return f"{match['name']}{quote}{match['sep']}{_mark('secret-value')}"

    text = _SECRET_ASSIGNMENT.sub(assignment, text)

    def flag(match: re.Match[str]) -> str:
        if "[REDACTED" in match["value"]:
            return match[0]
        removed["secret-value"] += 1
        return f"{match['name']}{match['sep']}{_mark('secret-value')}"

    text = _SECRET_FLAG.sub(flag, text)
    replace("email", _EMAIL)

    def random_string(match: re.Match[str]) -> str:
        if not _looks_random(match[0]):
            return match[0]
        removed["high-entropy"] += 1
        return _mark("high-entropy")

    text = _CANDIDATE.sub(random_string, text)

    truncated = len(text) > max_chars
    if truncated:
        text = text[-max_chars:]
        # Start on a whole line when there is one near the cut.
        newline = text.find("\n")
        if 0 <= newline < max_chars // 4:
            text = text[newline + 1 :]
    return Redacted(text, removed, truncated)
