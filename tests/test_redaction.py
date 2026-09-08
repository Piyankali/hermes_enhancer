"""v0.23 redaction unit tests: sanitizer behavior in isolation."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from redaction import sanitize, is_sensitive_key, assert_no_plaintext, REDACTED


def test_exact_keys_redacted():
    out = sanitize({"password": "hunter2", "username": "alice"})
    assert out["password"] == REDACTED
    assert out["username"] == "alice"


def test_case_insensitive_and_variants():
    out = sanitize({"Password": "a", "X-Api-Key": "b", "AUTHORIZATION": "c",
                    "db_passwd": "d", "Session_ID": "e"})
    for k in out:
        assert out[k] == REDACTED, k


def test_nested_structures():
    payload = {"headers": {"Authorization": "Bearer abc", "ok": 1},
               "items": [{"token": "t"}, ("a", {"secret": "s"})],
               "tags": {"x", "y"}}
    out = sanitize(payload)
    assert out["headers"]["Authorization"] == REDACTED
    assert out["headers"]["ok"] == 1
    assert out["items"][0]["token"] == REDACTED
    assert out["items"][1][1]["secret"] == REDACTED
    assert out["tags"] == {"x", "y"}


def test_does_not_mutate_input():
    src = {"auth": "s3cr3t", "nested": {"key": "v"}}
    sanitize(src)
    assert src == {"auth": "s3cr3t", "nested": {"key": "v"}}


def test_cycle_safe_and_depth_capped():
    a = {}
    a["self"] = a
    out = sanitize(a)
    assert out["self"] == REDACTED
    deep = cur = {}
    for _ in range(100):
        cur["n"] = {}
        cur = cur["n"]
    assert sanitize(deep) is not None


def test_non_string_keys_and_exotics():
    out = sanitize({1: "one", "when": __import__("datetime").datetime(2020, 1, 1)})
    assert out[1] == "one"
    assert "2020" in out["when"]


def test_long_strings_truncated():
    out = sanitize({"blob": "x" * 9000})
    assert len(out["blob"]) < 9000 and out["blob"].endswith("[truncated]")


def test_assert_no_plaintext_helper():
    assert assert_no_plaintext({"a": REDACTED}, ["secret"]) == []
    assert assert_no_plaintext({"a": "secret"}, ["secret"]) == ["secret"]


def test_all_mission_tokens_covered():
    tokens = ["password", "passwd", "token", "api_key", "secret",
              "client_secret", "private_key", "credential", "authorization",
              "cookie", "session", "auth", "bearer", "jwt"]
    for t in tokens:
        assert is_sensitive_key(t), t
        assert is_sensitive_key(t.upper()), t
