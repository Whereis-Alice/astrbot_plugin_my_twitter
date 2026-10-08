"""真实订阅服务 + 可控网络/投递，验证不补发的可观察行为。"""

import asyncio
import copy
import sys
import types

import pytest

from test_provider_initialization import _load_main_module


@pytest.fixture
def harness():
    module = _load_main_module()
    contract = sys.modules[f"{module.__package__}.services.tweet_delivery_service"]
    store = {"my_twitter_subs": {"tester": {
        "since_id": "100", "subscribers": {"session": {"status": True}},
    }}}
    sent, writes = [], []

    class API:
        ids = ["100"]
        timeline_failure = False
        detail_failure = False
        protected = False
        is_ready = True
        calls = []

        async def get_user_timeline_items(self, username, since_id, limit=0):
            self.calls.append((username, since_id, limit))
            assert since_id == ""  # Never paginate back to an obsolete cursor.
            if self.timeline_failure:
                raise module.FxTwitterTimelineError("temporarily unavailable")
            if self.protected:
                protected_error = sys.modules[
                    f"{module.__package__}.twitter_api"
                ].NitterProtectedAccountError
                raise protected_error("@tester 的账号受保护")
            return [{"tweet_id": value, "username": username} for value in reversed(self.ids)][:limit]

        async def get_tweet(self, username, tweet_id):
            return {"status": not self.detail_failure, "tweet_id": tweet_id, "username": username, "text": tweet_id}

    async def get_kv(key, default):
        return copy.deepcopy(store.get(key, default))

    async def put_kv(key, value):
        writes.append(copy.deepcopy(value))
        store[key] = copy.deepcopy(value)

    class Delivery:
        collective_enabled = False
        has_collected = False
        failure = False
        flush_failure = False
        begin_count = 0
        end_count = 0

        def begin_cycle(self):
            self.begin_count += 1

        def end_cycle(self):
            self.end_count += 1

        def clear_collected(self):
            self.has_collected = False

        async def push_to_subscribers(self, username, tweet, cycle=None):
            # Failure/cancellation after this point cannot replay consumed IDs.
            assert int(store["my_twitter_subs"][username]["since_id"]) >= int(tweet["tweet_id"])
            sent.append(tweet["tweet_id"])
            state = contract.DeliveryState.FAILED if self.failure else contract.DeliveryState.DELIVERED
            if self.collective_enabled:
                state = contract.DeliveryState.QUEUED
                self.has_collected = True
            return contract.DeliveryResult(state)

        async def flush_collected(self):
            self.has_collected = False
            return contract.CollectiveFlushResult(
                frozenset() if self.flush_failure else frozenset({"tester"}),
                frozenset({"tester"}) if self.flush_failure else frozenset(),
            )

    api, delivery = API(), Delivery()
    subscriptions = module.SubscriptionService(get_kv, put_kv, api, lambda: True)
    polling = module.PollingService(api, subscriptions, delivery, module.PollingSettings(True, "fxtwitter", "", ()))

    async def check():
        return await polling.check_user("tester", copy.deepcopy(store["my_twitter_subs"]["tester"]))

    return types.SimpleNamespace(**locals())


@pytest.mark.asyncio
async def test_startup_and_reload_only_sync_latest(harness):
    h = harness
    h.api.ids = [str(i) for i in range(101, 151)]
    assert await h.check()
    assert h.sent == []
    assert h.store["my_twitter_subs"]["tester"]["since_id"] == "150"
    h.api.ids += ["151"]
    assert await h.check()
    assert h.sent == ["151"]
    h.polling.require_resync()
    h.api.ids += ["152", "153"]
    assert await h.check()
    assert h.sent == ["151"]
    assert h.store["my_twitter_subs"]["tester"]["since_id"] == "153"


@pytest.mark.asyncio
async def test_protected_account_recovery_syncs_without_marking_mirror_failed(harness):
    h = harness
    assert await h.check()
    assert "tester" in h.polling._synchronized
    h.polling._synchronized.add("other")

    h.api.protected = True
    for tweet_id in ("101", "102", "103"):
        h.api.ids.append(tweet_id)
        assert await h.check()
        assert h.polling._synchronized == {"other"}
        assert h.store["my_twitter_subs"]["tester"]["since_id"] == "100"
        assert h.sent == []

    h.api.protected = False
    assert await h.check()
    assert h.store["my_twitter_subs"]["tester"]["since_id"] == "103"
    assert h.polling._synchronized == {"tester", "other"}
    assert h.sent == []

    h.api.ids.append("104")
    assert await h.check()
    assert h.sent == ["104"]
    assert h.store["my_twitter_subs"]["tester"]["since_id"] == "104"
    assert "other" in h.polling._synchronized


@pytest.mark.asyncio
async def test_excess_old_tweets_are_consumed_instead_of_queued(harness):
    h = harness
    await h.check()
    h.api.ids = [str(i) for i in range(101, 151)]
    assert await h.check()
    assert h.sent == ["150"]
    assert await h.check()
    assert h.sent == ["150"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeline_failure", "detail_failure", "delivery_failure"])
async def test_recovery_skips_failed_batch_and_interruption_backlog(harness, failure):
    h = harness
    await h.check()
    h.api.ids = ["101", "102"]
    if failure == "delivery_failure":
        h.delivery.failure = True
    else:
        setattr(h.api, failure, True)
    assert await h.check() is False
    previous = list(h.sent)
    h.delivery.failure = h.api.timeline_failure = h.api.detail_failure = False
    h.api.ids += ["103", "104"]
    assert await h.check()
    assert h.sent == previous
    assert h.store["my_twitter_subs"]["tester"]["since_id"] == "104"
    h.api.ids += ["105"]
    assert await h.check()
    assert h.sent == previous + ["105"]


@pytest.mark.asyncio
async def test_collective_failure_is_consumed_and_forces_sync(harness):
    h = harness
    h.delivery.collective_enabled = True
    await h.check()
    h.api.ids += ["101"]
    assert await h.check()
    assert h.store["my_twitter_subs"]["tester"]["since_id"] == "101"
    assert h.polling.has_pending_collective
    h.delivery.flush_failure = True
    await h.polling.flush_pending_collective()
    assert not h.polling.has_pending_collective
    h.api.ids += ["102", "103"]
    assert await h.check()
    assert h.sent == ["101"]


@pytest.mark.asyncio
async def test_kv_failure_prevents_any_send(harness):
    h = harness
    await h.check()
    h.api.ids += ["101"]

    async def fail(*_args):
        raise OSError("disk full")

    h.subscriptions._put_kv_data = fail
    assert await h.check() is False
    assert h.sent == []
    assert h.store["my_twitter_subs"]["tester"]["since_id"] == "100"


@pytest.mark.asyncio
async def test_cancellation_after_consumption_cannot_replay(harness):
    h = harness
    await h.check()
    h.api.ids += ["101"]

    async def cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError

    h.delivery.push_to_subscribers = cancelled
    with pytest.raises(asyncio.CancelledError):
        await h.check()
    assert h.store["my_twitter_subs"]["tester"]["since_id"] == "101"
    assert await h.check()


@pytest.mark.asyncio
async def test_retweets_disabled_and_known_ids_do_not_get_details(harness):
    h = harness
    await h.check()
    h.store["my_twitter_subs"]["tester"]["processed_tweet_ids"] = ["101"]

    async def timeline(*_args, **_kwargs):
        return [{"tweet_id": "102", "is_retweet": True}, {"tweet_id": "101"}]

    h.api.get_user_timeline_items = timeline
    h.polling.settings = h.module.PollingSettings(False, "fxtwitter", "", ())
    assert await h.check()
    assert h.sent == []
    assert h.store["my_twitter_subs"]["tester"]["since_id"] == "102"


@pytest.mark.asyncio
async def test_check_all_balances_cycle_hooks_and_global_failure_resync(harness, monkeypatch):
    h = harness
    h.store["my_twitter_subs"]["second"] = {"since_id": "99", "subscribers": {"session": {}}}
    polling_module = sys.modules[h.polling.__class__.__module__]

    async def no_delay(_seconds):
        pass

    monkeypatch.setattr(polling_module, "asyncio", types.SimpleNamespace(sleep=no_delay))
    await h.polling.check_all()
    assert h.delivery.begin_count == h.delivery.end_count == 1
    h.api.is_ready = False
    h.api.timeline_failure = True
    h.api.calls.clear()
    await h.polling.check_all()
    assert [entry[0] for entry in h.api.calls] == ["tester"]
    assert not h.polling._synchronized
    assert h.delivery.begin_count == h.delivery.end_count == 2
