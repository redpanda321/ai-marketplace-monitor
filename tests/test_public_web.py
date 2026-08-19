"""Tests for public and authorised-feed vehicle marketplace helpers."""

import pytest

from ai_marketplace_monitor.public_web import (
    PublicWebMarketplaceConfig,
    classify_drivetrain,
    classify_seller,
    parse_json_ld,
)


@pytest.mark.parametrize(
    "url,text,structured,expected",
    [
        ("https://vancouver.craigslist.org/cto/d/car/123.html", "", None, "private"),
        ("https://vancouver.craigslist.org/ctd/d/car/123.html", "", None, "dealer"),
        ("https://example.com/1", "Private seller", None, "private"),
        ("https://example.com/2", "Dealer documentation fee", None, "dealer"),
        ("https://example.com/3", "", {"seller": {"@type": "AutoDealer"}}, "dealer"),
        ("https://example.com/4", "", None, "unknown"),
    ],
)
def test_classify_seller(url: str, text: str, structured: dict | None, expected: str) -> None:
    assert classify_seller(url, text, structured) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Model Y Long Range Dual Motor", "AWD"),
        ("Tesla all-wheel drive", "AWD"),
        ("2024 Model Y RWD", "RWD"),
        ("rear wheel drive", "RWD"),
        ("2024 Model Y", "unknown"),
    ],
)
def test_classify_drivetrain(text: str, expected: str) -> None:
    assert classify_drivetrain(text) == expected


def test_parse_json_ld_finds_nested_vehicle_and_ignores_invalid_json() -> None:
    documents = [
        "not-json",
        '{"@context":"https://schema.org","@graph":['
        '{"@type":"Vehicle","name":"Model Y","url":"https://example.com/car"}]}',
    ]

    assert parse_json_ld(documents) == [
        {"@type": "Vehicle", "name": "Model Y", "url": "https://example.com/car"}
    ]


def test_public_web_config_rejects_insecure_url_and_invalid_limits() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        PublicWebMarketplaceConfig(name="bad", market_type="craigslist", search_url="http://x")
    with pytest.raises(ValueError, match="between 1 and 100"):
        PublicWebMarketplaceConfig(name="bad", market_type="craigslist", max_results=0)
