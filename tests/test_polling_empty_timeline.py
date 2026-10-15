"""空账号完成同步后应能推送其首条新动态。"""

import pytest

from test_polling_no_backlog import harness as harness


@pytest.mark.asyncio
async def test_empty_initial_timeline_saves_baseline_and_delivers_first_new_post(harness):
    h = harness
    h.store["my_twitter_subs"]["tester"]["since_id"] = ""
    h.api.ids = []

    assert await h.check()
    assert h.store["my_twitter_subs"]["tester"]["since_id"] == "0"
    assert h.store["my_twitter_subs"]["tester"]["processed_tweet_ids"] == []
    h.api.ids = ["101"]
    assert await h.check()
    assert h.sent == ["101"]


@pytest.mark.asyncio
async def test_empty_initial_timeline_kv_failure_does_not_mark_synced(harness):
    h = harness
    h.store["my_twitter_subs"]["tester"]["since_id"] = ""
    h.api.ids = []
    original_put = h.subscriptions._put_kv_data

    async def fail(*_args):
        raise OSError("KV unavailable")

    h.subscriptions._put_kv_data = fail
    assert await h.check() is False
    assert h.polling._synchronized == set()
    assert h.store["my_twitter_subs"]["tester"]["since_id"] == ""

    h.subscriptions._put_kv_data = original_put
    h.api.ids = ["101"]
    assert await h.polling.check_user("tester", {})
    assert h.sent == []
    h.api.ids.append("102")
    assert await h.polling.check_user("tester", {})
    assert h.sent == ["102"]


@pytest.mark.asyncio
@pytest.mark.parametrize("post_while_stopped", [False, True])
async def test_empty_baseline_reload_preserves_no_backlog_rule(harness, post_while_stopped):
    h = harness
    h.store["my_twitter_subs"]["tester"]["since_id"] = ""
    h.api.ids = []
    assert await h.check()
    h.polling = h.module.PollingService(
        h.api, h.subscriptions, h.delivery,
        h.module.PollingSettings(True, "fxtwitter", "", ()),
    )
    if post_while_stopped:
        h.api.ids = ["101"]

    assert await h.polling.check_user("tester", {})
    assert h.sent == []
    h.api.ids.append("102")
    assert await h.polling.check_user("tester", {})
    assert h.sent == ["102"]
