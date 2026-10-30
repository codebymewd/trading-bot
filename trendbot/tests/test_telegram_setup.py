import json

import httpx

from backend.app.telegram_client import BOT_COMMANDS, TelegramClient
from backend.envfile import load_env_file, parse_env
from tools.telegram_check import check


def mock(ok_token=True, can_message=True, log=None):
    def handler(req: httpx.Request):
        if log is not None:
            log.append((req.url.path, req.read().decode()))
        if req.url.path.endswith("getMe"):
            return httpx.Response(200, json={"ok": ok_token, "result": {"username": "my_trend_bot"}} if ok_token else {"ok": False})
        if req.url.path.endswith("sendMessage"):
            return httpx.Response(200, json={"ok": True} if can_message else {"ok": False, "description": "chat not found"})
        return httpx.Response(200, json={"ok": True})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_env_parsing_and_no_override(tmp_path, monkeypatch):
    assert parse_env('# c\nA=1\nB = "two"\n\nbad line\nC=\n') == {"A": "1", "B": "two", "C": ""}
    f = tmp_path / ".env"
    f.write_text("TB_TEST_A=from_file\nTB_TEST_B=from_file\n")
    monkeypatch.setenv("TB_TEST_B", "from_env")
    monkeypatch.delenv("TB_TEST_A", raising=False)
    assert load_env_file(str(f)) == 1
    import os
    assert os.environ["TB_TEST_A"] == "from_file" and os.environ["TB_TEST_B"] == "from_env"
    assert load_env_file(str(tmp_path / "missing.env")) == 0


def test_command_menu_is_valid_for_telegram():
    names = [c for c, _ in BOT_COMMANDS]
    assert len(names) == len(set(names)) and all(n == n.lower() and n.isalnum() and len(n) <= 32 for n in names)
    assert all(1 <= len(d) <= 256 for _, d in BOT_COMMANDS) and len(names) <= 100
    log = []
    assert TelegramClient("T", mock(log=log)).set_commands(BOT_COMMANDS)
    body = json.loads(log[0][1])
    assert body["commands"][0] == {"command": "start", "description": "Authorize and open the dashboard"}


def test_check_reports_each_failure_in_plain_language():
    out = []
    assert check("T", [1], mock(), out.append) and any("Token OK" in l and "my_trend_bot" in l for l in out)
    assert any("delivered" in l for l in out)
    out.clear()
    assert not check("T", [1], mock(ok_token=False), out.append) and "rejected the token" in out[0]
    out.clear()
    assert not check("T", [1], mock(can_message=False), out.append) and any("press START" in l for l in out)
    out.clear()
    assert not check("", [1], mock(), out.append) and "TB_TELEGRAM_TOKEN" in out[0]
    out.clear()
    assert not check("T", [], mock(), out.append) and "TB_TELEGRAM_USERS" in out[0]
    assert all("T" != l for l in out)
