from oversaetter.config import Config


def test_defaults_are_safe():
    cfg = Config.from_env({})
    assert cfg.dry_run is True
    assert cfg.poll_minutes == 15 and cfg.seerr_delay_minutes == 60 and cfg.tag_delay_hours == 48
    assert cfg.translate_timeout_minutes == 45 and cfg.max_line_chars == 42 and cfg.proofread_batch == 50
    assert cfg.monthly_budget_usd == 5 and cfg.proofread_model == "claude-opus-5-5"
    assert cfg.translate_tag == "oversaet" and cfg.credit_text == "Oversat af Claude for ≋"


def test_env_values(tmp_path):
    key = tmp_path / "key.txt"
    key.write_text("  sk-test \n")
    cfg = Config.from_env(
        {
            "DRY_RUN": "false",
            "POLL_MINUTES": "5",
            "MONTHLY_BUDGET_USD": "2,5",
            "PROOFREAD_BATCH": "20",
            "SEERR_URL": "http://seerr:5055/",
            "ANTHROPIC_API_KEY_FILE": str(key),
            "TAG_DELAY_HOURS": "",
        }
    )
    assert cfg.dry_run is False and cfg.poll_minutes == 5 and cfg.monthly_budget_usd == 2.5
    assert cfg.proofread_batch == 20 and cfg.seerr_url == "http://seerr:5055" and cfg.tag_delay_hours == 48
    assert cfg.anthropic_api_key() == "sk-test"


def test_missing_key_file():
    assert Config.from_env({"ANTHROPIC_API_KEY_FILE": "/findes/ikke"}).anthropic_api_key() == ""


def test_price():
    cfg = Config()
    assert abs(cfg.price(1_000_000, 1_000_000) - 24.0) < 1e-9
