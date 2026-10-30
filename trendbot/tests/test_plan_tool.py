from tools import plan_basket as tool

SPEC = """symbol,XAUUSDm
digits,2
point,0.01
tick_size,0.01
tick_value_profit,1.0
tick_value_loss,1.0
volume_min,0.01
volume_max,100
volume_step,0.01
stops_level_points,10
freeze_level_points,0
contract_size,100
margin_per_lot,2000
commission_per_lot,0
trade_allowed,1
account_currency,USD
equity,100
free_margin,100
"""


def test_load_spec_and_plan(tmp_path, capsys):
    f = tmp_path / "spec.csv"
    f.write_text(SPEC)
    spec, raw = tool.load_spec(str(f))
    assert spec.symbol == "XAUUSDm" and spec.volume_step == 0.01 and spec.problems() == []
    rc = tool.main([str(f), "--risk-pct", "10", "--entry", "2000", "--sl", "1995", "--dir", "buy", "--positions", "3"])
    out = capsys.readouterr().out
    assert rc == 0 and "positions 2" in out and "planned risk 10.00" in out and "2015.00" in out


def test_no_trade_when_min_lot_exceeds_risk(tmp_path, capsys):
    f = tmp_path / "spec.csv"
    f.write_text(SPEC)
    rc = tool.main([str(f), "--risk-pct", "1", "--entry", "2000", "--sl", "1995", "--dir", "buy"])
    assert rc == 1 and "min_lot_exceeds_risk" in capsys.readouterr().out
