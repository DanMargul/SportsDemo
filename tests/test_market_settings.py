from sports_markets.discover import (DEFAULT_MARKET_SETTINGS,
                                     LEGACY_MARKET_SETTING_NAMES,
                                     canonical_market_settings)


def test_emitted_settings_match_the_command_line_flags():
    import re
    import pathlib
    source = pathlib.Path(
        "src/sports_markets/market_maker.py").read_text()
    flags = {name.replace("-", "_")
             for name in re.findall(r'add_argument\("--([a-z0-9-]+)"', source)}
    for name in DEFAULT_MARKET_SETTINGS:
        assert name in flags, (
            f"config key {name!r} has no matching command line flag")
    print("PASS every emitted config key matches a command line flag")


def test_legacy_setting_names_map_forward():
    legacy = {"size": 5, "max_inventory": 20, "gamma": 0.1, "k": 50,
              "sgo_poll": 10.0}
    canonical = canonical_market_settings(legacy)
    assert canonical == {"quote_size": 5, "max_inventory": 20,
                         "risk_aversion_gamma": 0.1,
                         "fill_intensity_decay_k": 50,
                         "sgo_refresh_seconds": 10.0}
    assert set(canonical) == set(DEFAULT_MARKET_SETTINGS), (
        "a legacy config must map onto exactly the current settings")
    assert canonical_market_settings(None) == {}
    unknown = canonical_market_settings({"ticker": "T", "quote_size": 3})
    assert unknown == {"ticker": "T", "quote_size": 3}, (
        "unrecognised keys must pass through untouched")
    for old_name, new_name in LEGACY_MARKET_SETTING_NAMES.items():
        assert new_name in DEFAULT_MARKET_SETTINGS
        assert old_name not in DEFAULT_MARKET_SETTINGS
    print("PASS legacy config keys map forward onto current settings")


TESTS = [
    test_emitted_settings_match_the_command_line_flags,
    test_legacy_setting_names_map_forward,
]


def main():
    for test in TESTS:
        test()
    print(f"\n{len(TESTS)} tests passed in test_market_settings.py")


if __name__ == "__main__":
    main()
