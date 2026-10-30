import re

from backend.app import protocol


def test_signing_vector_in_the_deployment_guide_is_correct():
    doc = open("docs/DEPLOYMENT.md").read()
    h = protocol.sign("x" * 32, "POST", "/ea/heartbeat", b'{"a":1}', 1700000000, "abc123")
    assert h[protocol.H_SIG] in doc
    assert re.search(r"[0-9a-f]{64}", doc)


def test_ea_sample_config_header_is_up_to_date():
    import subprocess
    import sys
    assert subprocess.run([sys.executable, "tools/gen_sample_config.py", "--check"]).returncode == 0, "run: py tools/gen_sample_config.py"


def test_ea_self_test_expectations_match_the_backend_payload():
    """The MQL5 self-test hard-codes values from the default config: keep them in sync with the schema."""
    import json
    from backend.app.config_schema import BrokerConfig, SystemConfig
    p = SystemConfig(broker=BrokerConfig(allowed_logins=[555001])).ea_payload()
    assert p["mode"] == "SIGNAL_ONLY" and p["risk"]["risk_percent"] == 1.0 and p["scalp"]["target_multiplier"] == 3.0
    assert p["scalp"]["timeout_action"] == "CLOSE_ALL" and p["entry"]["entry_mode"] == "market_confirmed"
    assert p["confirmation"]["minimum_confirmations"] == 3 and len(p["confirmation"]["enabled_indicators"]) == 4
    assert p["derived"]["reset_utc_offset_seconds"] == 3600 and p["safety"]["heartbeat_seconds"] == 10
    assert p["schema_version"] == 3 and len(p["config_hash"]) == 64
    for needle in ('"risk_percent":1.0', '"target_multiplier":3.0', '"schema_version":3', '"timeout_action":"CLOSE_ALL"',
                   '"entry_mode":"market_confirmed"', '"minimum_confirmations":3', '"mode":"SIGNAL_ONLY"'):
        assert needle in json.dumps(p, separators=(",", ":"), sort_keys=True), needle
