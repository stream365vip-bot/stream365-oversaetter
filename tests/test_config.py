from oversaetter.config import Config


def test_defaults_are_safe():
    cfg = Config.from_env({})
    assert cfg.dry_run is True
    assert cfg.poll_minutes == 15 and cfg.search_timeout_minutes == 10 and cfg.max_searches_per_round == 10
    assert cfg.ignored_keys == ()
    assert cfg.translate_timeout_minutes == 45 and cfg.max_line_chars == 42 and cfg.proofread_batch == 50
    assert cfg.monthly_budget_usd == 5 and cfg.proofread_model == "claude-opus-5-5"
    assert cfg.translate_tag == "oversaet"
    assert cfg.credit_text == (
        'Oversat til dig af\\n<font color="#58D9AE">≋ </font><b>Stream<font color="#58D9AE">365</font></b>'
    )


def test_empty_credit_text_turns_credit_off():
    assert Config.from_env({"CREDIT_TEXT": ""}).credit_text == ""


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
            "SEARCH_TIMEOUT_MINUTES": "",
        }
    )
    assert cfg.dry_run is False and cfg.poll_minutes == 5 and cfg.monthly_budget_usd == 2.5
    assert cfg.proofread_batch == 20 and cfg.seerr_url == "http://seerr:5055" and cfg.search_timeout_minutes == 10
    assert cfg.anthropic_api_key() == "sk-test"


def test_missing_key_file():
    assert Config.from_env({"ANTHROPIC_API_KEY_FILE": "/findes/ikke"}).anthropic_api_key() == ""


def test_price():
    cfg = Config()
    assert abs(cfg.price(1_000_000, 1_000_000) - 24.0) < 1e-9


def test_old_delay_keys_are_ignored():
    cfg = Config.from_env(
        {"SEERR_DELAY_MINUTES": "30", "TAG_DELAY_HOURS": "48", "FIRST_SEEN_DELAY_HOURS": "24", "POLL_MINUTES": "5"}
    )
    assert cfg.ignored_keys == ("SEERR_DELAY_MINUTES", "TAG_DELAY_HOURS", "FIRST_SEEN_DELAY_HOURS")
    assert cfg.poll_minutes == 5
    assert not hasattr(cfg, "seerr_delay_minutes") and not hasattr(cfg, "tag_delay_hours")
    assert Config.from_env({"TAG_DELAY_HOURS": ""}).ignored_keys == ()
