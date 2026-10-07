import json
import webspec.config_writer as cw
from webspec.config import ServiceRegistry


def test_default_config_path_env(monkeypatch, tmp_path):
    p = tmp_path / "alt.json"
    monkeypatch.setenv("WEBSPEC_CONFIG", str(p))
    assert cw.default_config_path() == p


def test_registry_reads_env_config(monkeypatch, tmp_path):
    p = tmp_path / "alt.json"
    p.write_text(json.dumps({"mcpServers": {"svc-x": {"type": "http", "url": "http://x"}}}))
    monkeypatch.setenv("WEBSPEC_CONFIG", str(p))
    reg = ServiceRegistry()
    assert "svc-x" in reg.names()


# ── production defaults and permission-preserving writes ──

import os
import stat

import pytest


@pytest.fixture
def etc_webspec(monkeypatch, tmp_path):
    """/etc/webspec under tmp_path, not yet created; HOME and the WEBSPEC_* paths unset."""
    monkeypatch.delenv("WEBSPEC_CONFIG", raising=False)
    monkeypatch.delenv("WEBSPEC_ENV_FILE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    etc = tmp_path / "etc-webspec"
    monkeypatch.setattr(cw, "PRODUCTION_CONFIG", etc / "config.json")
    monkeypatch.setattr(cw, "PRODUCTION_ENV", etc / "gateway.env")
    return etc


def test_defaults_follow_the_production_install(etc_webspec, monkeypatch, tmp_path):
    assert cw.default_config_path() == tmp_path / "home" / ".claude.json"  # a dev host
    assert cw.default_env_path() == tmp_path / "home" / ".env"
    etc_webspec.mkdir()  # /etc/webspec alone (allowed_signers, say): still a dev host
    (etc_webspec / "gateway.env").write_text("WEBSPEC_DOMAIN=example.com\n")
    assert not cw.is_production_host()
    assert cw.default_config_path() == tmp_path / "home" / ".claude.json"
    assert cw.default_env_path() == tmp_path / "home" / ".env"
    (etc_webspec / "config.json").write_text("{}")  # the production install: sudo must edit it
    assert cw.is_production_host()
    assert cw.default_config_path() == etc_webspec / "config.json"
    assert cw.default_env_path() == etc_webspec / "gateway.env"
    monkeypatch.setenv("WEBSPEC_CONFIG", str(tmp_path / "explicit.json"))
    monkeypatch.setenv("WEBSPEC_ENV_FILE", str(tmp_path / "explicit.env"))
    assert cw.default_config_path() == tmp_path / "explicit.json"  # explicit always wins
    assert cw.default_env_path() == tmp_path / "explicit.env"


def test_a_config_json_that_is_not_a_regular_file_is_no_production_install(etc_webspec):
    (etc_webspec / "config.json").mkdir(parents=True)
    assert not cw.is_production_host()


@pytest.mark.skipif(os.geteuid() == 0, reason="root sees into every directory")
def test_a_closed_etc_webspec_counts_as_production(etc_webspec):
    # install.sh makes /etc/webspec 0750 root:webspec: a user who cannot look inside must be
    # sent to sudo, not quietly given ~/.claude.json.
    etc_webspec.mkdir()
    (etc_webspec / "config.json").write_text("{}")
    etc_webspec.chmod(0)
    try:
        assert cw.is_production_host()
        assert cw.default_config_path() == etc_webspec / "config.json"
    finally:
        etc_webspec.chmod(0o755)


def _mode(p):
    return stat.S_IMODE(p.stat().st_mode)


def test_config_rewrite_keeps_mode_and_backup_is_no_wider(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"mcpServers": {}}))
    os.chmod(cfg, 0o640)
    cw.add_service("svc", {"command": "x"}, path=cfg)
    assert _mode(cfg) == 0o640
    assert _mode(cfg.with_suffix(".json.bak")) == 0o640
    assert json.loads(cfg.read_text())["mcpServers"] == {"svc": {"command": "x"}}
    assert not list(tmp_path.glob(".*.tmp"))  # no temp file left behind


def test_a_new_config_or_env_file_is_private(tmp_path):
    cfg = tmp_path / "new.json"
    cw._write_claude_config({"mcpServers": {}}, path=cfg)
    assert _mode(cfg) == 0o600
    env = tmp_path / "gateway.env"
    cw.add_env_var("SOME_TOKEN", path=env)
    assert _mode(env) == 0o600 and env.read_text() == "SOME_TOKEN=\n"


def test_env_rewrites_keep_mode(tmp_path):
    env = tmp_path / "gateway.env"
    env.write_text("A=1\n")
    os.chmod(env, 0o600)
    cw.add_env_var("B", "2", path=env)
    cw.remove_env_var("A", path=env)
    assert env.read_text() == "B=2\n" and _mode(env) == 0o600


def test_a_symlink_at_the_temp_path_is_not_followed(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text("{}")
    target = tmp_path / "elsewhere"
    target.write_text("untouched")
    (tmp_path / ".config.json.tmp").symlink_to(target)
    cw._write_claude_config({"mcpServers": {}}, path=cfg)
    assert target.read_text() == "untouched"
    assert json.loads(cfg.read_text()) == {"mcpServers": {}}


@pytest.mark.skipif(os.geteuid() != 0, reason="restoring another owner needs root")
def test_as_root_owner_and_group_are_kept(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text("{}")
    os.chown(cfg, 1, 1)
    cw._write_claude_config({"mcpServers": {}}, path=cfg)
    st = cfg.stat()
    assert (st.st_uid, st.st_gid) == (1, 1)


# ── the env file: names, the export form (macOS), honest results ──


@pytest.fixture
def sourced(monkeypatch):
    """The macOS gateway.env, which the daemon's sh sources: `export KEY=` sets KEY too."""
    monkeypatch.setattr(cw, "env_file_sourced", lambda path=None: True)


@pytest.fixture
def systemd_env(monkeypatch):
    """The Linux gateway.env, a systemd EnvironmentFile=: only `KEY=` sets KEY (P11)."""
    monkeypatch.setattr(cw, "env_file_sourced", lambda path=None: False)


def test_the_env_files_consumer_follows_the_platform_and_the_file(etc_webspec, monkeypatch, tmp_path):
    # P11: systemd reads the production gateway.env and the development unit's
    # ~/.webspec/gateway.env (EnvironmentFile=), where `export KEY=` sets nothing. No unit reads
    # a development host's ~/.env, a shell's file, nor any other WEBSPEC_ENV_FILE.
    home = tmp_path / "home"
    (home / ".webspec").mkdir(parents=True)
    link = tmp_path / "link.env"
    link.symlink_to(etc_webspec / "gateway.env")
    systemd = [etc_webspec / "gateway.env", home / ".webspec" / "gateway.env", link]
    others = [home / ".env", tmp_path / "explicit.env"]
    monkeypatch.setattr(cw.sys, "platform", "darwin")
    assert all(cw.env_file_sourced(p) is True for p in systemd + others)
    assert cw.env_file_sourced() is True
    monkeypatch.setattr(cw.sys, "platform", "linux")
    assert [cw.env_file_sourced(p) for p in systemd] == [False, False, False]
    assert [cw.env_file_sourced(p) for p in others] == [True, True]
    assert cw.env_file_sourced() is True  # a development host: ~/.env
    (etc_webspec / "config.json").parent.mkdir(exist_ok=True)
    (etc_webspec / "config.json").write_text("{}")
    assert cw.env_file_sourced() is False  # the production install: /etc/webspec/gateway.env
    monkeypatch.setenv("WEBSPEC_ENV_FILE", str(tmp_path / "explicit.env"))
    assert cw.env_file_sourced() is True


def test_on_a_linux_development_host_the_shells_env_file_keeps_both_forms(etc_webspec, monkeypatch, tmp_path):
    # P11's rule is systemd's: a development host's ~/.env is a shell's file, where `export TOKEN=`
    # sets TOKEN. An empty TOKEN= after it would blank the secret for every shell that sources it,
    # and no warning about systemd applies.
    monkeypatch.setattr(cw.sys, "platform", "linux")
    env = tmp_path / "home" / ".env"
    env.parent.mkdir()
    env.write_text("export TOKEN='s3cr3t'\n")
    assert cw.default_env_path() == env
    assert cw.add_env_var("TOKEN") is False and env.read_text() == "export TOKEN='s3cr3t'\n"
    assert cw.ignored_exports("TOKEN") == [] and cw.ignored_export_warning("TOKEN") is None


@pytest.mark.parametrize("line", ["export TOKEN='s3cr3t'", "TOKEN=s3cr3t", "  export\tTOKEN = s3cr3t", "TOKEN =x"])
def test_add_env_var_sees_every_form_of_an_existing_assignment(tmp_path, sourced, line):
    # The macOS gateway.env is sourced by sh: `export TOKEN=` is TOKEN too. A second, empty
    # TOKEN= after it would blank the secret at the next start.
    env = tmp_path / "gateway.env"
    env.write_text(line + "\n")
    assert cw.add_env_var("TOKEN", path=env) is False
    assert env.read_text() == line + "\n"
    assert cw.ignored_export_warning("TOKEN", env) is None


@pytest.mark.parametrize("line", ["TOKEN=s3cr3t", "TOKEN =x", "  TOKEN=x"])
def test_on_linux_add_env_var_sees_the_lines_systemd_reads(tmp_path, systemd_env, line):
    env = tmp_path / "gateway.env"
    env.write_text(line + "\n")
    assert cw.add_env_var("TOKEN", path=env) is False
    assert env.read_text() == line + "\n"


@pytest.mark.parametrize("line", ["export TOKEN='s3cr3t'", "  export\tTOKEN = s3cr3t"])
def test_on_linux_an_export_line_gets_a_warning_to_fix_it_in_place(tmp_path, systemd_env, line):
    # P11: systemd ignores `export TOKEN=…`, so the gateway does not get TOKEN from it, and the
    # operator hears which line to change, never its value. No placeholder goes in after it: once
    # that line is fixed in place, an empty TOKEN= after it would be the last assignment, which
    # systemd keeps, and the gateway would get an empty secret.
    env = tmp_path / "gateway.env"
    env.write_text("KEEP=1\n" + line + "\n")
    assert cw.add_env_var("TOKEN", path=env) is False
    assert env.read_text() == f"KEEP=1\n{line}\n"
    warning = cw.ignored_export_warning("TOKEN", env)
    assert warning == (f"{env} sets TOKEN only with `export TOKEN=…` (line 2), which systemd ignores: it reads "
                       "KEY=value lines only, and logs that line, value included, to the journal at every start. "
                       "The gateway does not get TOKEN from this file. Change line 2 to TOKEN=value")
    assert "s3cr3t" not in warning
    assert cw.ignored_exports("TOKEN", env) == [2] and cw.ignored_exports("TOKEN", env, sourced=True) == []
    assert cw.ignored_exports("TOKEN", tmp_path / "missing.env") == []
    # Fixed as the warning says: the one line systemd reads holds the secret.
    env.write_text(env.read_text().replace(line, "TOKEN='s3cr3t'"))
    assert cw.ignored_export_warning("TOKEN", env) is None
    assert [ln for ln in env.read_text().splitlines() if "TOKEN" in ln] == ["TOKEN='s3cr3t'"]


def test_on_linux_the_warning_names_the_line_systemd_reads_where_there_is_one(tmp_path, systemd_env):
    # A placeholder (or install.sh's empty default) and the value on an export line after it:
    # systemd gives the gateway the empty one. The value belongs on the line systemd reads.
    env = tmp_path / "gateway.env"
    env.write_text("TOKEN=\nKEEP=1\nexport TOKEN='s3cr3t'\n")
    assert cw.add_env_var("TOKEN", path=env) is False
    assert cw.ignored_export_warning("TOKEN", env) == (
        f"{env} sets TOKEN with `export TOKEN=…` too (line 3), which systemd ignores: it reads KEY=value lines "
        "only, and logs that line, value included, to the journal at every start. The gateway gets TOKEN from "
        "line 1. Give line 1 the value (TOKEN=value), and delete line 3")
    env.write_text("export TOKEN=a\nKEEP=1\nexport TOKEN=b\n")
    assert cw.ignored_export_warning("TOKEN", env) == (
        f"{env} sets TOKEN only with `export TOKEN=…` (lines 1, 3), which systemd ignores: it reads KEY=value lines "
        "only, and logs those lines, values included, to the journal at every start. The gateway does not get "
        "TOKEN from this file. Keep one of lines 1, 3, as TOKEN=value, and delete the others")


@pytest.mark.parametrize("platform", ["sourced", "systemd_env"])
def test_remove_env_var_removes_the_export_form_and_says_so(tmp_path, request, platform):
    # Everywhere: where systemd ignores an export line, it still holds the secret.
    request.getfixturevalue(platform)
    env = tmp_path / "gateway.env"
    env.write_text("KEEP=1\nexport TOKEN='s3cr3t'\nTOKEN=again\nTOKEN_OTHER=x\n")
    assert cw.remove_env_var("TOKEN", path=env) is True
    assert env.read_text() == "KEEP=1\nTOKEN_OTHER=x\n"
    assert cw.remove_env_var("TOKEN", path=env) is False
    assert cw.remove_env_var("TOKEN", path=tmp_path / "missing.env") is False
    env.write_text("export TOKEN=s3cr3t\n")
    assert cw.remove_env_var("TOKEN", path=env) is True and env.read_text() == "\n"


@pytest.mark.parametrize("name", ["MY-TOKEN", "1TOKEN", "", "A B", "TOKEN=x", "TÖKEN", "x\ny"])
def test_names_that_are_not_shell_identifiers_are_refused(tmp_path, name):
    # `MY-TOKEN=` makes the macOS daemon's `set -e; . gateway.env` abort at every start.
    env = tmp_path / "gateway.env"
    env.write_text("KEEP=1\n")
    with pytest.raises(ValueError, match="not an environment variable name"):
        cw.add_env_var(name, path=env)
    with pytest.raises(ValueError):
        cw.remove_env_var(name, path=env)
    assert env.read_text() == "KEEP=1\n"


@pytest.mark.parametrize("name", ["WEBSPEC_HOST", "WEBSPEC_AUDIT_LOG", "WEBSPEC_GUARD_KEY", "WEBSPEC_DOMAIN"])
def test_gateway_settings_are_not_mcp_secrets(tmp_path, name):
    # They configure the gateway: `WEBSPEC_AUDIT_LOG=` would turn the audit log off, and removing
    # WEBSPEC_GUARD_KEY would stop the gateway. WEBSPEC_HOST decides which interfaces it listens
    # on (DP-8); an empty one means loopback, as unset does (__main__.resolve_bind_host).
    env = tmp_path / "gateway.env"
    env.write_text(f"{name}=keep\n")
    with pytest.raises(ValueError, match="gateway setting"):
        cw.add_env_var(name, path=env)
    with pytest.raises(ValueError, match="gateway setting"):
        cw.remove_env_var(name, path=env)
    assert env.read_text() == f"{name}=keep\n"


def test_remove_service_says_whether_it_removed_anything(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"mcpServers": {"svc": {"command": "x"}}}))
    assert cw.remove_service("other", path=cfg) is False
    assert cw.remove_service("svc", path=cfg) is True
    assert json.loads(cfg.read_text())["mcpServers"] == {}
