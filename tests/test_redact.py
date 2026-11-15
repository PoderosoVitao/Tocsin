from __future__ import annotations

import random
import string
import time

import pytest

from tocsin.redact import DEFAULT_MAX_CHARS, redact

# Fake credentials, built the way real ones are shaped. None is a real secret.
# The Slack and Stripe ones are joined from pieces so that the source never holds a
# whole token-shaped string for a secret scanner to mistake for a leaked one.
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
GITHUB = "ghp_16C7e42F292c6912E7710c838347Ae178B4a"
OPENAI = "sk-proj-Qk3vN8xT2mLp9RzYd4WcJf7HbUs1AeGo"
ANTHROPIC = "sk-ant-api03-Zr5TqLw8NxVb2KmHc9Yd4JfPs7UeAo1G"
SLACK = "xox" + "b-123456789012-1234567890123-" + "AbCdEfGhIjKlMnOpQrStUvWx"
GOOGLE = "AIzaSyD-9tSrke72PouQMnMX-a7eZSW0jkFMBWY"
STRIPE = "sk_" + "live_" + "51HqLyJ2eZvKYlo2C0ABCdefGhiJKLmn"
TELEGRAM = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw0"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"
TOCSIN_KEY = "tsn_4tQfx9mYb2LpZr7WcKd1HnSe8UvAo3Gj"
PING_UUID = "0b9e5bd4-77a4-4a2c-9a51-6b1f6b2d3c11"
PASSWORD = "hunter2-Xk9"
RANDOM_B64 = "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MEFCQ0RFRg"
RANDOM_HEX = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
PEM = """-----BEGIN OPENSSH PRIVATE KEY-----
b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW
QyNTUxOQAAACD0xA5fakeFAKEfakeFAKEfakeFAKEfakeFAKEfakeFAKEAAAAGZmFrZQ==
-----END OPENSSH PRIVATE KEY-----"""

# (secret, a line of job output that contains it). Each one is the sort of
# thing that ends up in a failing job's last lines.
PLANTED = [
    (AWS_KEY, f"botocore.exceptions.ClientError: key {AWS_KEY} is not authorized"),
    (AWS_SECRET, f"aws_secret_access_key = {AWS_SECRET}"),
    (GITHUB, f"remote: Invalid username or token. git clone https://{GITHUB}@github.com/o/r.git"),
    (OPENAI, f"openai.AuthenticationError: Incorrect API key provided: {OPENAI}."),
    (ANTHROPIC, f"401 invalid x-api-key: {ANTHROPIC}"),
    (SLACK, f"slack_sdk.errors.SlackApiError: token {SLACK} revoked"),
    (GOOGLE, f"GET https://maps.googleapis.com/maps/api/geocode/json?key={GOOGLE} 403"),
    (STRIPE, f"stripe.error.AuthenticationError: Invalid API Key provided: {STRIPE}"),
    (TELEGRAM, f"POST https://api.telegram.org/bot{TELEGRAM}/sendMessage failed"),
    (JWT, f"jwt expired: {JWT}"),
    (TOCSIN_KEY, f"curl -H 'Authorization: Bearer {TOCSIN_KEY}' http://tocsin/api/checks"),
    (PING_UUID, f"curl -fsS --retry 3 http://tocsin:8000/ping/{PING_UUID}/fail"),
    (PASSWORD, f"psycopg2.OperationalError: connection to postgresql://app:{PASSWORD}@db:5432/x"),
    (PASSWORD, f"mysql --user=root --password={PASSWORD} -h db"),
    (PASSWORD, f"mysqldump --host db --password {PASSWORD} app"),
    (PASSWORD, f"DB_PASSWORD={PASSWORD}"),
    (PASSWORD, f"export DB_PASSWORD='{PASSWORD}'"),
    (PASSWORD, f'{{"user": "app", "password": "{PASSWORD}", "host": "db"}}'),
    (PASSWORD, f"password: {PASSWORD}"),
    (PASSWORD, f'clientSecret = "{PASSWORD}"'),
    (PASSWORD, f"Authorization: Basic {PASSWORD}"),
    (PASSWORD, f"Cookie: theme=dark; session={PASSWORD}"),
    (PASSWORD, f"docker run -e API_TOKEN={PASSWORD} -e MODE=prod img"),
    (RANDOM_B64, f"unexpected value {RANDOM_B64} in response"),
    (RANDOM_HEX, f"sha256 mismatch: got {RANDOM_HEX}"),
    ("b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW", f"{PEM}\nexit 1"),
    ("QyNTUxOQAAACD0xA5fakeFAKEfakeFAKE", PEM),
]


@pytest.mark.parametrize(("secret", "line"), PLANTED)
def test_a_planted_secret_never_survives(secret: str, line: str) -> None:
    assert secret in line
    result = redact(f"step 3 of 5\n{line}\nbuild failed\n")
    assert secret not in result.text, result.text
    assert result.count >= 1
    # The text around it is kept, so a summary still has something to read.
    assert "step 3 of 5" in result.text and "build failed" in result.text


def test_a_secret_is_removed_wherever_it_sits_in_the_output() -> None:
    for secret, line in PLANTED:
        filler = "\n".join(f"line {n}: ok" for n in range(40))
        for text in (f"{line}\n{filler}", f"{filler}\n{line}", f"{filler}\n{line}\n{filler}"):
            assert secret not in redact(text, max_chars=100_000).text


# The secrets are put into random surroundings of ordinary words and numbers, in
# a fixed random order, to catch anything that depends on what is next to it.
def test_planted_secrets_survive_no_neighbourhood() -> None:
    rng = random.Random(7)
    words = ["error", "failed", "retrying", "12", "/tmp/build", "connection", "refused", "=", ":"]
    for secret, line in PLANTED * 3:
        before = " ".join(rng.choice(words) for _ in range(rng.randint(0, 8)))
        after = " ".join(rng.choice(words) for _ in range(rng.randint(0, 8)))
        assert secret not in redact(f"{before}\n{line}\n{after}").text


def test_what_was_removed_is_counted_by_kind() -> None:
    result = redact(f"key {AWS_KEY} mail ops@example.com password={PASSWORD} and {GITHUB}")
    assert dict(result.removed) == {
        "aws-key": 1,
        "email": 1,
        "secret-value": 1,
        "github-token": 1,
    }
    assert result.count == 4
    assert "[REDACTED:aws-key]" in result.text and "[REDACTED:email]" in result.text


def test_a_private_key_with_no_end_is_removed_to_the_end_of_the_output() -> None:
    result = redact("before\n-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\nmore base64\n")
    assert result.text == "before\n[REDACTED:private-key]"


def test_a_header_does_not_take_the_rest_of_the_command_with_it() -> None:
    result = redact(f'curl -H "Authorization: Bearer {TOCSIN_KEY}" https://t.example/api/checks')
    assert "https://t.example/api/checks" in result.text
    assert TOCSIN_KEY not in result.text


# Everything here is ordinary diagnostic text a summary needs. None of it may
# be touched, or the summaries would get worse for no gain.
ORDINARY = [
    "OSError: [Errno 28] No space left on device: '/mnt/backup/db-2026-11-02.sql.gz'",
    "psycopg2.OperationalError: connection refused (127.0.0.1:5432), is the server running?",
    "Killed\nexit status 137 (out of memory)",
    "PermissionError: [Errno 13] Permission denied: '/var/lib/app/state.json'",
    'File "/usr/local/lib/python3/dist-packages/requests/adapters.py", line 519, in send',
    'Traceback (most recent call last):\n  File "jobs/backup.py", line 88, in <module>',
    "tests passed: 12, failed: 2, tokens used: 812, secrets rotated: 3, author: bob",
    "bypass: yes\ncompass heading: 270\nauthenticated: true\ntoken expired, refreshing",
    "2026-11-02T03:00:07Z INFO copied 12 files (1.4 GiB) in 31m4s",
    "request 0b9e5bd4-77a4-4a2c-9a51-6b1f6b2d3c11 took 4.2s",
    "refund-agent-v12 nightly_eval_2026_11_02 failed",
    "rsync: connection unexpectedly closed (0 bytes received so far) [sender]",
    "pg_dump: error: query failed: ERROR: permission denied for table users",
    "ConnectionResetError: [Errno 104] Connection reset by peer",
    "git checkout 4f2c9a1 && make build-all-targets-for-release",
]


@pytest.mark.parametrize("text", ORDINARY)
def test_ordinary_diagnostic_text_is_left_alone(text: str) -> None:
    result = redact(text)
    assert result.text == text
    assert result.count == 0


def test_terminal_colour_codes_are_stripped() -> None:
    assert redact("\x1b[31mERROR\x1b[0m: \x1b[1;33mdisk full\x1b[0m").text == "ERROR: disk full"


def test_only_the_end_of_long_output_is_kept() -> None:
    text = "\n".join(f"line {n}" for n in range(2000)) + "\nthe real error"
    result = redact(text)
    assert result.truncated
    assert len(result.text) <= DEFAULT_MAX_CHARS
    assert result.text.endswith("the real error")
    assert result.text.startswith("line ")  # cut at the start of a line
    assert not redact("short").truncated


def test_a_secret_cut_by_the_size_limit_leaves_no_fragment() -> None:
    # Whatever the cut does, no piece of the secret may be left on its own:
    # redaction happens before the text is cut.
    for padding in range(0, 60):
        text = "x" * padding + f" password={PASSWORD} token {GITHUB} " + "y" * 30
        for limit in range(40, len(text), 7):
            kept = redact(text, max_chars=limit).text
            assert PASSWORD[:6] not in kept and GITHUB[:12] not in kept


def test_redacting_twice_changes_nothing_more() -> None:
    text = "\n".join(line for _, line in PLANTED)
    once = redact(text, max_chars=100_000)
    twice = redact(once.text, max_chars=100_000)
    assert twice.text == once.text
    assert twice.count == 0


def test_hostile_input_does_not_make_it_slow() -> None:
    rng = random.Random(1)
    nasty = [
        "a" * 10_000,
        "a-" * 5_000,
        "password" * 1_250,
        "token=" * 1_600,
        "/".join(["segment1"] * 1_100),
        "".join(rng.choice(string.ascii_letters + string.digits + "+/=_-") for _ in range(10_000)),
        "-----BEGIN PRIVATE KEY-----" * 300,
        "Bearer " + "x" * 9_990,
        " ".join(["--password"] * 900),
    ]
    for text in nasty:
        started = time.monotonic()
        redact(text, max_chars=100_000)
        assert time.monotonic() - started < 1.0, text[:30]


def test_a_long_random_string_is_removed_even_with_no_label_in_front_of_it() -> None:
    rng = random.Random(3)
    alphabet = string.ascii_letters + string.digits
    for _ in range(200):
        secret = "".join(rng.choice(alphabet) for _ in range(rng.randint(24, 64)))
        assert secret not in redact(f"got {secret} back").text


def test_sentences_with_digits_and_long_lowercase_words_are_not_mistaken_for_secrets() -> None:
    text = "pneumonoultramicroscopicsilicovolcanoconiosis was found in file_name_with_many_parts_v2"
    assert redact(text).text == text


def test_a_line_that_mentions_a_marker_is_still_scrubbed() -> None:
    text = (
        "Authorization: [REDACTED:auth-header] and then "
        + GITHUB
        + "\nAuthorization: Bearer abc12345678"
    )
    result = redact(text)
    assert GITHUB not in result.text and "abc12345678" not in result.text
