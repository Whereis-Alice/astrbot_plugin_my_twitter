"""屏蔽词参与有界筛选，不消耗额度，不产生补发队列。"""

import pytest

from test_polling_no_backlog import harness as harness


def install_filter(h, texts, max_tweets=1):
    h.polling = h.module.PollingService(
        h.api,
        h.subscriptions,
        h.delivery,
        h.module.PollingSettings(True, "fxtwitter", "", (), max_tweets),
        content_filter=h.module.TweetContentFilter(["oc"]),
    )

    async def check():
        return await h.polling.check_user("tester", {})

    h.check = check
    calls = []

    async def get_tweet(username, tweet_id):
        calls.append(tweet_id)
        payload = texts.get(tweet_id, {})
        return {
            "status": True, "tweet_id": tweet_id, "username": username,
            "text": "artwork", **payload,
        }

    h.api.get_tweet = get_tweet
    return calls


@pytest.mark.asyncio
async def test_filter_fills_quota_with_latest_allowed_and_delivers_in_order(harness):
    h = harness
    calls = install_filter(h, {
        "104": {"text": "my OC"},
        "102": {"quote": {"text": "an oc drawing"}},
    }, max_tweets=2)
    await h.check()
    h.api.ids = ["100", "101", "102", "103", "104"]

    assert await h.check()
    assert calls == ["104", "103", "102", "101"]
    assert h.sent == ["101", "103"]
    assert h.store["my_twitter_subs"]["tester"]["since_id"] == "104"
    assert await h.check()
    assert calls == ["104", "103", "102", "101"]
    assert h.sent == ["101", "103"]


@pytest.mark.asyncio
async def test_filter_stops_details_once_quota_is_full(harness):
    h = harness
    calls = install_filter(h, {"104": {"text": "OC"}})
    await h.check()
    h.api.ids = ["100", "101", "102", "103", "104"]

    assert await h.check()
    assert calls == ["104", "103"]
    assert h.sent == ["103"]


@pytest.mark.asyncio
async def test_all_filtered_items_stay_consumed_after_filter_is_disabled(harness):
    h = harness
    calls = install_filter(h, {
        str(i): {"text": "OC"} for i in range(101, 131)
    })
    await h.check()
    h.api.ids = [str(i) for i in range(101, 131)]

    assert await h.check()
    assert calls == [str(i) for i in range(130, 110, -1)]
    assert h.sent == []
    assert h.store["my_twitter_subs"]["tester"]["since_id"] == "130"

    h.polling.content_filter = h.module.TweetContentFilter()
    assert await h.check()
    assert h.sent == []
    h.api.ids.append("131")
    assert await h.check()
    assert h.sent == ["131"]


@pytest.mark.asyncio
@pytest.mark.parametrize("with_empty_filter", [False, True])
async def test_disabled_filter_keeps_latest_only_detail_requests(harness, with_empty_filter):
    h = harness
    calls = install_filter(h, {"104": {"text": "OC"}})
    h.polling.content_filter = h.module.TweetContentFilter() if with_empty_filter else None
    await h.check()
    h.api.ids = ["100", "101", "102", "103", "104"]

    assert await h.check()
    assert calls == ["104"]
    assert h.sent == ["104"]


@pytest.mark.asyncio
async def test_filter_detail_failure_consumes_batch_and_recovers_without_backlog(harness):
    h = harness
    calls = install_filter(h, {"103": {"status": False}}, max_tweets=2)
    await h.check()
    h.api.ids = ["100", "101", "102", "103", "104"]

    assert await h.check() is False
    assert calls == ["104", "103"]
    assert h.sent == []
    assert h.store["my_twitter_subs"]["tester"]["since_id"] == "104"
    h.api.ids.append("105")
    assert await h.check()
    assert h.sent == []
    h.api.ids.append("106")
    assert await h.check()
    assert h.sent == ["106"]


@pytest.mark.asyncio
async def test_filter_does_not_fetch_details_before_consumption_is_saved(harness):
    h = harness
    calls = install_filter(h, {})
    await h.check()
    h.api.ids.append("101")

    async def fail(*_args):
        raise OSError("KV unavailable")

    h.subscriptions._put_kv_data = fail
    assert await h.check() is False
    assert calls == []
    assert h.sent == []
