"""Public web vehicle marketplace connector.

The connector only visits URLs explicitly configured by the operator. Sources whose
published robots policy forbids automated search stay disabled unless the operator
supplies an authorised public search page and marks that page as allowed.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from logging import Logger
from typing import Any, Generator, Iterable, Type
from urllib.parse import quote_plus, urljoin, urlparse

from playwright.sync_api import Browser, Locator

from .listing import Listing
from .marketplace import ItemConfig, Marketplace, MarketplaceConfig
from .utils import KeyboardMonitor, hilight


@dataclass
class PublicWebMarketplaceConfig(MarketplaceConfig):
    """Configuration shared by public and authorised HTML sources."""

    search_url: str | None = None
    robots_allowed: bool = True
    blocked_reason: str | None = None
    max_results: int = 25
    result_selector: str | None = None
    title_selector: str = "[itemprop='name'], .title, h2, h3"
    price_selector: str = "[itemprop='price'], .price, .priceinfo"
    location_selector: str = "[itemprop='availableAtOrFrom'], .location, .meta"
    image_selector: str = "img"
    detail_selector: str = "[itemprop='description'], .postingbody, main"
    postal_code: str = "V3B0A3"

    def handle_search_url(self: "PublicWebMarketplaceConfig") -> None:
        if self.search_url is not None and not self.search_url.startswith("https://"):
            raise ValueError(f"Marketplace {hilight(self.name)} search_url must use HTTPS.")

    def handle_max_results(self: "PublicWebMarketplaceConfig") -> None:
        if not isinstance(self.max_results, int) or not 1 <= self.max_results <= 100:
            raise ValueError("max_results must be an integer between 1 and 100.")

    def handle_robots_allowed(self: "PublicWebMarketplaceConfig") -> None:
        if not isinstance(self.robots_allowed, bool):
            raise ValueError("robots_allowed must be a boolean.")


@dataclass
class PublicWebItemConfig(ItemConfig):
    """Item options for public web sources."""


def classify_seller(url: str, text: str, structured: dict[str, Any] | None = None) -> str:
    """Classify a listing as private, dealer, or unknown."""
    path = urlparse(url).path.lower()
    lowered = text.lower()
    seller = (structured or {}).get("seller", {})
    seller_type = seller.get("@type", "") if isinstance(seller, dict) else ""
    if "/cto/" in path or seller_type == "Person" or "private seller" in lowered:
        return "private"
    if (
        "/ctd/" in path
        or seller_type in {"Organization", "AutoDealer"}
        or re.search(r"\b(dealer|dealership|documentation fee)\b", lowered)
    ):
        return "dealer"
    return "unknown"


def classify_drivetrain(text: str) -> str:
    """Extract an explicit Tesla drivetrain without guessing."""
    lowered = text.lower()
    if re.search(r"\b(awd|all[- ]wheel drive|dual motor|long range)\b", lowered):
        return "AWD"
    if re.search(r"\b(rwd|rear[- ]wheel drive)\b", lowered):
        return "RWD"
    return "unknown"


def _iter_json_objects(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_json_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_json_objects(child)


def parse_json_ld(raw_documents: Iterable[str]) -> list[dict[str, Any]]:
    """Return vehicle/product/list-item objects from JSON-LD documents."""
    results: list[dict[str, Any]] = []
    for raw in raw_documents:
        try:
            document = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        for obj in _iter_json_objects(document):
            kind = obj.get("@type")
            if kind in {"Vehicle", "Car", "Product", "Offer"} and (
                obj.get("url") or obj.get("item")
            ):
                results.append(obj)
    return results


def _text(card: Locator, selector: str) -> str:
    try:
        match = card.locator(selector).first
        return match.inner_text(timeout=1_500).strip() if match.count() else ""
    except Exception:
        return ""


def _attribute(card: Locator, selector: str, name: str) -> str:
    try:
        match = card.locator(selector).first
        return (match.get_attribute(name, timeout=1_500) or "").strip() if match.count() else ""
    except Exception:
        return ""


class PublicWebMarketplace(Marketplace[PublicWebMarketplaceConfig, PublicWebItemConfig]):
    """Configurable connector for public and authorised HTML search pages."""

    def __init__(
        self,
        name: str,
        browser: Browser | None,
        keyboard_monitor: KeyboardMonitor | None = None,
        logger: Logger | None = None,
    ) -> None:
        super().__init__(name, browser, keyboard_monitor, logger)

    @classmethod
    def get_config(cls: Type["PublicWebMarketplace"], **kwargs: Any) -> PublicWebMarketplaceConfig:
        return PublicWebMarketplaceConfig(**kwargs)

    @classmethod
    def get_item_config(cls: Type["PublicWebMarketplace"], **kwargs: Any) -> PublicWebItemConfig:
        return PublicWebItemConfig(**kwargs)

    def _search_url(self, phrase: str, item: PublicWebItemConfig) -> str:
        assert self.config.search_url is not None
        radius = (item.radius or self.config.radius or [80])[0]
        return self.config.search_url.format(
            query=quote_plus(phrase),
            postal_code=quote_plus(self.config.postal_code),
            radius_km=radius,
        )

    def _cards(self) -> list[dict[str, str]]:
        assert self.page is not None
        selector = self.config.result_selector
        if not selector:
            return []
        try:
            self.page.wait_for_selector(selector, timeout=15_000)
        except Exception:
            return []
        cards = self.page.locator(selector)
        results: list[dict[str, str]] = []
        for index in range(min(cards.count(), self.config.max_results)):
            card = cards.nth(index)
            href = _attribute(card, "a[href]", "href")
            if not href:
                continue
            results.append(
                {
                    "url": urljoin(self.page.url, href),
                    "title": _text(card, self.config.title_selector) or _text(card, "a[href]"),
                    "price": _text(card, self.config.price_selector),
                    "location": _text(card, self.config.location_selector),
                    "image": _attribute(card, self.config.image_selector, "src"),
                }
            )
        return results

    def _details(self, url: str) -> tuple[str, dict[str, Any] | None]:
        assert self.page is not None
        detail_page = self.page.context.new_page()
        try:
            detail_page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            documents = detail_page.locator(
                "script[type='application/ld+json']"
            ).all_text_contents()
            structured = next(iter(parse_json_ld(documents)), None)
            locator = detail_page.locator(self.config.detail_selector).first
            text = locator.inner_text(timeout=5_000).strip() if locator.count() else ""
            return text[:20_000], structured
        except Exception as error:
            if self.logger:
                self.logger.warning(f"Unable to retrieve listing detail {url}: {error}")
            return "", None
        finally:
            detail_page.close()

    def search(self, item: PublicWebItemConfig) -> Generator[Listing, None, None]:
        if not self.config.robots_allowed:
            if self.logger:
                self.logger.warning(
                    f"{hilight('[Policy]', 'fail')} {self.name} is disabled: "
                    f"{self.config.blocked_reason or 'automated search is not authorised'}"
                )
            return
        if not self.config.search_url or not self.config.result_selector:
            if self.logger:
                self.logger.warning(f"{self.name} requires an authorised search_url and selector.")
            return

        self.create_page()
        seen: set[str] = set()
        for phrase in item.search_phrases:
            self.goto_url(self._search_url(phrase, item))
            for card in self._cards():
                url = card["url"].split("#", 1)[0]
                if url in seen:
                    continue
                seen.add(url)
                details, structured = self._details(url)
                combined = "\n".join((card["title"], details))
                seller_type = classify_seller(url, combined, structured)
                drivetrain = classify_drivetrain(combined)
                description = (
                    f"Source: {self.name}\nSeller type: {seller_type}\n"
                    f"Drivetrain: {drivetrain}\n{details}"
                ).strip()
                listing_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
                yield Listing(
                    marketplace=self.name,
                    name=item.name,
                    id=listing_id,
                    title=card["title"] or "**unspecified**",
                    image=card["image"],
                    price=card["price"] or "**unspecified**",
                    post_url=url,
                    location=card["location"] or "**unspecified**",
                    seller=seller_type,
                    condition=drivetrain,
                    description=description,
                )
