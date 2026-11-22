from types import SimpleNamespace

import pytest

from ai_marketplace_monitor.facebook import FacebookSearchResultPage


@pytest.mark.parametrize("verified_empty", [False, True])
def test_browse_marketplace_button_alone_is_not_a_verified_empty_batch(
    verified_empty: bool,
) -> None:
    page = SimpleNamespace(
        locator=lambda *args: SimpleNamespace(count=lambda: 1),
        get_by_text=lambda *args: SimpleNamespace(count=lambda: int(verified_empty)),
    )
    result = FacebookSearchResultPage(page, lambda text: text, None)
    result.strict_receipt = True
    if verified_empty:
        assert result.get_listings() == []
    else:
        with pytest.raises(ValueError, match="empty-result marker"):
            result.get_listings()
