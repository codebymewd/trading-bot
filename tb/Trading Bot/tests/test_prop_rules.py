import pytest

from backend.app.config_schema import SystemConfig
from backend.app.prop_rules import OcrUnavailable, ProfileDraft, extract_text, parse_rules
from tests.test_service import make, say, confirm_id

TEXT = """Account size: $10,000. Profit target 8%. Maximum daily loss 5% of the initial balance.
Maximum overall loss 10% (trailing drawdown, equity-based, including open positions). Daily loss resets at 00:00 UTC.
Maximum lot size 5 lots. No trading 5 minutes before or after high-impact news. Minimum 4 trading days. A consistency rule applies.
Restricted instruments: BTCUSD, ETHUSD."""


def test_extracts_the_documented_fields():
    ex = parse_rules(TEXT)
    f = ex.fields
    assert f["starting_balance"] == 10000.0 and f["profit_target_percent"] == 8.0
    assert f["daily_loss_limit_percent"] == 5.0 and f["max_drawdown_percent"] == 10.0
    assert f["drawdown_type"] == "trailing" and f["drawdown_basis"] == "equity" and f["count_floating_pl"] is True
    assert f["daily_reset_time"] == "00:00" and f["daily_reset_timezone"] == "UTC"
    assert f["max_lot_size"] == 5.0 and f["news_restricted"] is True and f["news_window_min"] == 5
    assert f["restricted_symbols"] == ["BTCUSD", "ETHUSD"] and f["min_trading_days"] == 4
    assert ex.unresolved == [] and ex.conflicts == []
    assert any("consistency" in n.lower() and "NOT enforced" in n for n in ex.notes)


def test_missing_values_are_flagged_not_guessed():
    ex = parse_rules("Profit target 10%. Daily loss limit 4%.")
    assert "max_drawdown_percent" in ex.unresolved and "starting_balance" in ex.unresolved
    assert "drawdown_type" not in ex.fields and "daily_reset_time" not in ex.fields
    with pytest.raises(ValueError):
        ProfileDraft("X", ex).to_profile()


def test_ambiguous_timezone_is_not_silently_converted():
    ex = parse_rules("The daily loss resets at 17:00 EST. Daily loss 5%. Max drawdown 10% static. Account size $5,000. Balance-based.")
    assert ex.fields["daily_reset_time"] == "17:00" and "daily_reset_timezone" not in ex.fields
    assert any("ambiguous" in n for n in ex.notes) and "daily_reset_timezone" in ex.unresolved


def test_conflicting_statements_block_confirmation():
    ex = parse_rules("Max drawdown is trailing. The max drawdown is static. Daily loss 5%. Max drawdown 10%. Account size $5,000.")
    assert any("drawdown_type" in c for c in ex.conflicts)
    d = ProfileDraft("X", ex)
    assert any("conflict" in u for u in d.unresolved)
    assert d.set_field("drawdown_type", "static") == ""
    assert not any("conflict" in u for u in d.unresolved)


def test_edit_validation_and_confirmed_profile_is_valid_for_the_config():
    d = ProfileDraft("MyFirm", parse_rules("Daily loss 5%. Max drawdown 10%. Account size $10,000. Static. Equity based."))
    assert d.set_field("daily_reset_timezone", "Mars/Base").startswith("unknown timezone")
    assert "invalid value" in d.set_field("daily_loss_limit_percent", "-3")
    assert "unknown field" in d.set_field("bogus", "1")
    assert d.set_field("daily_reset_timezone", "Africa/Lagos") == "" and d.set_field("daily_reset_time", "00:00") == ""
    prof = d.to_profile()
    assert prof.confirmed and prof.unresolved_fields == []
    cfg = SystemConfig(prop_profiles={"MyFirm": prof}, active_prop_profile="MyFirm")      # usable by the guard
    assert cfg.prop_profiles["MyFirm"].daily_reset_timezone == "Africa/Lagos"


def test_ocr_and_pdf_are_optional_dependencies(tmp_path, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "pytesseract", None)        # simulate: OCR libraries not installed
    monkeypatch.setitem(sys.modules, "pypdf", None)
    with pytest.raises(OcrUnavailable):
        extract_text(str(tmp_path / "rules.png"))
    with pytest.raises(OcrUnavailable):
        extract_text(str(tmp_path / "rules.pdf"))
    f = tmp_path / "rules.txt"
    f.write_text("Daily loss 5%")
    assert extract_text(str(f)) == "Daily loss 5%"


def test_telegram_import_flow_end_to_end():
    svc, c, notes, clock = make()
    assert "Paste the prop firm's rules" in say(svc, "/importrules")[0].text
    out = say(svc, TEXT.replace("00:00 UTC", "17:00 EST"))[0].text
    assert "MISSING / UNCLEAR" in out and "daily_reset_timezone" in out and "Review everything" in out
    assert "Cannot confirm yet" in say(svc, "/ruleconfirm Firm A")[0].text
    assert "Set." in say(svc, "/ruleset daily_reset_timezone Africa/Lagos")[0].text
    assert "Set." in say(svc, "/ruleset daily_reset_time 00:00")[0].text
    r = say(svc, "/ruleconfirm Firm A")
    assert "Confirm?" in r[0].text and "Firm A" not in svc.cfg.current.prop_profiles       # nothing saved before the button
    say(svc, data="ok:" + confirm_id(r))
    assert svc.cfg.current.prop_profiles["Firm A"].confirmed and svc.cfg.current.active_prop_profile is None
    r = say(svc, "/setrisk 2")                         # profile saved but not active: no prop conflict yet
    assert "Confirm" in r[0].text
    act = say(svc, data="set:profile:Firm A")
    assert "Confirm?" in act[0].text
    say(svc, data="ok:" + confirm_id(act))
    assert svc.cfg.current.active_prop_profile == "Firm A"
