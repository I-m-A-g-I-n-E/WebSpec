"""The guard key comes from outside the gateway (GD-5): WEBSPEC_GUARD_KEY, populated from a
password manager, or a key file named by WEBSPEC_GUARD_KEY_FILE. The gateway never generates
or writes one, and fails closed without it."""
import hashlib
import os
import sys

import pytest
import webspec.config as config
from webspec.config import GUARD_KEY_FILE_MAX_BYTES, GuardKeyError, get_session_key


@pytest.fixture(autouse=True)
def _reset_ephemeral(monkeypatch):
    # Ensure a clean env + reset the cached dev key between tests.
    monkeypatch.delenv("WEBSPEC_GUARD_KEY", raising=False)
    monkeypatch.delenv("WEBSPEC_GUARD_KEY_FILE", raising=False)
    monkeypatch.delenv("WEBSPEC_GUARD_KEY_DEV_EPHEMERAL", raising=False)
    config._dev_ephemeral_key = None
    yield
    config._dev_ephemeral_key = None


def test_hex_key_decoded(monkeypatch):
    monkeypatch.setenv("WEBSPEC_GUARD_KEY", "01" * 32)  # 64 hex chars
    assert get_session_key() == b"\x01" * 32


def test_passphrase_key_derived(monkeypatch):
    monkeypatch.setenv("WEBSPEC_GUARD_KEY", "hunter2")
    assert get_session_key() == hashlib.sha256(b"hunter2").digest()
    assert len(get_session_key()) == 32


def test_missing_key_fails_closed():
    with pytest.raises(GuardKeyError):
        get_session_key()


def test_dev_ephemeral_is_stable(monkeypatch):
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_DEV_EPHEMERAL", "1")
    k1 = get_session_key()
    k2 = get_session_key()
    assert k1 == k2 and len(k1) == 32


def test_no_session_key_file_created(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("WEBSPEC_GUARD_KEY", "02" * 32)
    get_session_key()
    assert not (tmp_path / ".webspec" / "session.key").exists()


def test_whitespace_only_key_fails_closed(monkeypatch):
    # A blank/whitespace-only key must not silently derive to sha256(b"") — a
    # publicly-known key. It should be treated the same as missing.
    monkeypatch.setenv("WEBSPEC_GUARD_KEY", "   ")
    with pytest.raises(GuardKeyError):
        get_session_key()


# ── WEBSPEC_GUARD_KEY_FILE (the macOS deployment's /etc/webspec/guard.key, Docker secrets) ──


def _key_file(tmp_path, content: bytes):
    path = tmp_path / "guard.key"
    path.write_bytes(content)
    path.chmod(0o400)
    return path


def test_key_file_hex_is_stripped_and_decoded(monkeypatch, tmp_path):
    path = _key_file(tmp_path, b"  " + b"ab" * 32 + b"\n")  # as `op read … > guard.key` leaves it
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_FILE", str(path))
    assert get_session_key() == b"\xab" * 32


def test_key_file_passphrase_uses_the_env_var_derivation(monkeypatch, tmp_path):
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_FILE", str(_key_file(tmp_path, "pässphrase wörds\n".encode())))
    from_file = get_session_key()
    monkeypatch.delenv("WEBSPEC_GUARD_KEY_FILE")
    monkeypatch.setenv("WEBSPEC_GUARD_KEY", "pässphrase wörds")
    assert from_file == get_session_key() == hashlib.sha256("pässphrase wörds".encode()).digest()


@pytest.mark.parametrize("blank_env", [None, "", "   ", "\u200b", "\ufeff\u2060 "])
def test_key_file_used_when_the_env_var_is_unset_or_blank(monkeypatch, tmp_path, blank_env):
    if blank_env is not None:
        monkeypatch.setenv("WEBSPEC_GUARD_KEY", blank_env)
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_FILE", str(_key_file(tmp_path, b"03" * 32)))
    assert get_session_key() == b"\x03" * 32


def test_env_var_wins_over_the_key_file(monkeypatch, tmp_path):
    monkeypatch.setenv("WEBSPEC_GUARD_KEY", "04" * 32)
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_FILE", str(_key_file(tmp_path, b"05" * 32)))
    assert get_session_key() == b"\x04" * 32


def test_key_file_is_read_on_every_call(monkeypatch, tmp_path):
    path = _key_file(tmp_path, b"06" * 32)
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_FILE", str(path))
    assert get_session_key() == b"\x06" * 32
    path.chmod(0o600)
    path.write_bytes(b"07" * 32)  # rotated in place: the next request uses the new key
    assert get_session_key() == b"\x07" * 32
    path.unlink()  # removed: fail closed, never fall back to the old key
    with pytest.raises(GuardKeyError, match="cannot be read"):
        get_session_key()


@pytest.mark.parametrize("content", [b"", b" \n\t\n"])
def test_empty_key_file_fails_closed(monkeypatch, tmp_path, content):
    # Even with the dev escape hatch on: a configured key file that is broken is an error.
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_DEV_EPHEMERAL", "1")
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_FILE", str(_key_file(tmp_path, content)))
    with pytest.raises(GuardKeyError, match="is empty"):
        get_session_key()


def test_missing_key_file_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_DEV_EPHEMERAL", "1")
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_FILE", str(tmp_path / "absent.key"))
    with pytest.raises(GuardKeyError, match="cannot be read"):
        get_session_key()


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="root reads any file")
def test_unreadable_key_file_fails_closed(monkeypatch, tmp_path):
    path = _key_file(tmp_path, b"08" * 32)
    path.chmod(0)
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_FILE", str(path))
    with pytest.raises(GuardKeyError, match="cannot be read"):
        get_session_key()


def test_key_file_that_is_a_directory_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_FILE", str(tmp_path))
    with pytest.raises(GuardKeyError):
        get_session_key()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs here")
def test_key_file_that_is_a_fifo_fails_closed_without_blocking(monkeypatch, tmp_path):
    fifo = tmp_path / "guard.fifo"
    os.mkfifo(fifo)  # nobody writes to it: a blocking open would hang the request forever
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_FILE", str(fifo))
    with pytest.raises(GuardKeyError, match="not a regular file"):
        get_session_key()


def test_oversized_key_file_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_FILE", str(_key_file(tmp_path, b"k" * (GUARD_KEY_FILE_MAX_BYTES + 1))))
    with pytest.raises(GuardKeyError, match="larger than"):
        get_session_key()


def test_key_file_that_is_not_utf8_fails_closed(monkeypatch, tmp_path):
    # A raw binary key is refused rather than guessed at: store the key as 64 hex digits.
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_FILE", str(_key_file(tmp_path, bytes(range(128, 160)))))
    with pytest.raises(GuardKeyError, match="not UTF-8"):
        get_session_key()


def test_key_file_errors_never_echo_the_key(monkeypatch, tmp_path):
    secret = "s3cret-" * 700  # oversized, so it is refused
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_FILE", str(_key_file(tmp_path, secret.encode())))
    with pytest.raises(GuardKeyError) as excinfo:
        get_session_key()
    assert "s3cret" not in str(excinfo.value)


# ── What a key file may hold: one line of at least 16 characters, invisibles around it removed ──

HEX = hashlib.sha256(b"key-file-content").hexdigest()  # 64 hex digits, the recommended form


@pytest.mark.parametrize("content", [
    "\ufeff" + HEX,  # a UTF-8 byte order mark, as some editors write one
    "\ufeff" + HEX + "\r\n",
    "\u200b" + HEX + "\u200b\n",  # zero-width spaces from a copy out of a web page
    "\u2060\u00a0" + HEX + "\u3000\u200d",  # word joiner, no-break space, ideographic space, ZWJ
], ids=["bom", "bom-crlf", "zero-width-space", "mixed"])
def test_invisible_characters_around_the_key_are_removed(monkeypatch, tmp_path, content):
    # Before, a BOM stayed in front of the hex digits and silently derived sha256(BOM + hex).
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_FILE", str(_key_file(tmp_path, content.encode())))
    assert get_session_key() == bytes.fromhex(HEX)


@pytest.mark.parametrize("content", [b"\xef\xbb\xbf", b"\xef\xbb\xbf\n", "\u200b".encode(),
                                     "\ufeff\u200b \u2060\n".encode()],
                         ids=["bom", "bom-newline", "zero-width-space", "mixed"])
def test_a_key_file_of_invisible_characters_only_is_empty(monkeypatch, tmp_path, content):
    # Before, these derived sha256(U+FEFF) or sha256(U+200B): a key anyone can compute.
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_DEV_EPHEMERAL", "1")  # never a fallback for a broken file
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_FILE", str(_key_file(tmp_path, content)))
    with pytest.raises(GuardKeyError, match="is empty"):
        get_session_key()


@pytest.mark.parametrize("content, error", [
    (HEX + "\n" + HEX, "more than one line"),
    (HEX + "\r\n" + HEX + "\r\n", "more than one line"),
    ("first line of a passphrase\u2028second line", "more than one line"),
    (HEX[:32] + "\u200b" + HEX[32:], r"control or format character \(U\+200B\)"),
    (HEX[:32] + "\t" + HEX[32:], r"control or format character \(U\+0009\)"),
    (HEX[:32] + "\x00" + HEX[32:], r"control or format character \(U\+0000\)"),
    ("k" * 15, "fewer than 16 characters"),
    ("\ufeff" + "k" * 15 + "\n", "fewer than 16 characters"),
], ids=["two-lines", "two-crlf-lines", "line-separator", "inner-zero-width-space", "inner-tab",
        "inner-nul", "short", "short-after-bom"])
def test_malformed_key_files_fail_closed_without_echoing_the_key(monkeypatch, tmp_path, content, error):
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_DEV_EPHEMERAL", "1")
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_FILE", str(_key_file(tmp_path, content.encode())))
    with pytest.raises(GuardKeyError, match=error) as excinfo:
        get_session_key()
    message = str(excinfo.value)
    for fragment in (HEX[:12], HEX[-12:], "kkkk", "passphrase", "second line"):
        assert fragment not in message


def test_a_key_file_of_sixteen_characters_is_accepted(monkeypatch, tmp_path):
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_FILE", str(_key_file(tmp_path, b"sixteen-chars-ok\n")))
    assert get_session_key() == hashlib.sha256(b"sixteen-chars-ok").digest()


def test_the_env_var_keeps_its_own_rules(monkeypatch):
    # The key-file rules are not applied to WEBSPEC_GUARD_KEY: a short passphrase still works.
    monkeypatch.setenv("WEBSPEC_GUARD_KEY", "short")
    assert get_session_key() == hashlib.sha256(b"short").digest()


INVISIBLE_ONLY = ["\u200b", "\ufeff", "\u2060", "\ufeff \u2060\u200b\u00a0\n"]
INVISIBLE_IDS = ["zero-width-space", "bom", "word-joiner", "mixed"]


@pytest.mark.parametrize("value", INVISIBLE_ONLY, ids=INVISIBLE_IDS)
def test_an_env_var_of_invisible_characters_only_counts_as_unset(monkeypatch, value):
    # Before, these derived sha256 of the characters, a key anyone can compute: str.strip()
    # keeps format characters (category Cf), so the value did not count as blank.
    monkeypatch.setenv("WEBSPEC_GUARD_KEY", value)
    with pytest.raises(GuardKeyError, match=r"^WEBSPEC_GUARD_KEY is not set \(it holds only whitespace or "
                                            r"invisible characters\)\. "):
        get_session_key()


@pytest.mark.parametrize("value", INVISIBLE_ONLY, ids=INVISIBLE_IDS)
def test_an_env_var_of_invisible_characters_only_never_derives_their_hash(monkeypatch, value):
    # As for an empty variable, the dev escape hatch mints a random key, never the public one.
    monkeypatch.setenv("WEBSPEC_GUARD_KEY", value)
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_DEV_EPHEMERAL", "1")
    key = get_session_key()
    assert key == config._dev_ephemeral_key
    assert key not in (hashlib.sha256(value.encode()).digest(), hashlib.sha256(value.strip().encode()).digest())


def test_an_unset_env_var_is_reported_as_unset(monkeypatch):
    with pytest.raises(GuardKeyError, match=r"^WEBSPEC_GUARD_KEY is not set\. Source it"):
        get_session_key()


@pytest.mark.parametrize("value", ["\u200bshort", "short\ufeff", "\u2060k\u2060"])
def test_a_visible_character_keeps_the_env_vars_derivation(monkeypatch, value):
    # Only a value with nothing visible changed: every key that worked still derives the same.
    monkeypatch.setenv("WEBSPEC_GUARD_KEY", value)
    assert get_session_key() == hashlib.sha256(value.encode()).digest()


@pytest.mark.parametrize("value", ["secret-key\udcffmaterial", "ab" * 31 + "a\udcff"],
                         ids=["passphrase", "64-characters"])
def test_an_env_var_key_that_is_not_utf8_fails_closed_without_echoing_it(monkeypatch, value):
    # A byte that is not UTF-8 reaches os.environ as a lone surrogate. It used to escape as a
    # UnicodeEncodeError, which stopped create_app() and skipped the audited 500 per request.
    monkeypatch.setenv("WEBSPEC_GUARD_KEY", value)
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_DEV_EPHEMERAL", "1")  # a key that is set but unusable still fails closed
    with pytest.raises(GuardKeyError, match="WEBSPEC_GUARD_KEY is not valid UTF-8") as excinfo:
        get_session_key()
    assert excinfo.value.__context__ is None and excinfo.value.__cause__ is None  # nothing holds the value
    for fragment in ("secret", "material", "abab"):
        assert fragment not in str(excinfo.value)


# ── WEBSPEC_GUARD_KEY alone: env_guard_key, what WEBSPEC_REQUIRE_GUARD_KEY=1 requires ──

# Values the variable may hold that do not make a key: get_session_key would fall back to the
# key file or the dev key, and env_guard_key never does.
NO_KEY = {
    "unset": None,
    "empty": "",
    "spaces": "   ",
    "tab-newline": "\t\n",
    "zero-width-space": "\u200b",
    "no-break-space": "\u00a0",
    "bom": "\ufeff",
    "word-joiner": "\u2060",
    "ideographic-space": "\u3000",
    "mixed": "\u200b\u00a0 \u2060\n",
}


@pytest.mark.parametrize("value", NO_KEY.values(), ids=NO_KEY.keys())
def test_env_guard_key_has_no_fallback(monkeypatch, tmp_path, value):
    if value is not None:
        monkeypatch.setenv("WEBSPEC_GUARD_KEY", value)
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_FILE", str(_key_file(tmp_path, b"05" * 32)))
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_DEV_EPHEMERAL", "1")
    blank = r" \(it holds only whitespace or invisible characters\)" if value else ""
    with pytest.raises(GuardKeyError, match=rf"^WEBSPEC_GUARD_KEY is not set{blank}$"):
        config.env_guard_key()
    assert get_session_key() == b"\x05" * 32  # where the gateway's other rule falls back to the file
    assert config._dev_ephemeral_key is None  # and env_guard_key minted nothing


@pytest.mark.parametrize("value", ["secret-key\udcffmaterial", "ab" * 31 + "a\udcff"],
                         ids=["passphrase", "64-characters"])
def test_env_guard_key_refuses_a_value_that_is_not_utf8_without_echoing_it(monkeypatch, value):
    monkeypatch.setenv("WEBSPEC_GUARD_KEY", value)
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_DEV_EPHEMERAL", "1")
    with pytest.raises(GuardKeyError, match="^WEBSPEC_GUARD_KEY is not valid UTF-8$") as excinfo:
        config.env_guard_key()
    assert excinfo.value.__context__ is None and excinfo.value.__cause__ is None


@pytest.mark.parametrize("value", ["01" * 32, "short", "\u200bshort", " pass phrase "])
def test_env_guard_key_derives_what_get_session_key_derives(monkeypatch, tmp_path, value):
    monkeypatch.setenv("WEBSPEC_GUARD_KEY", value)
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_FILE", str(_key_file(tmp_path, b"05" * 32)))
    assert config.env_guard_key() == get_session_key() != b"\x05" * 32
