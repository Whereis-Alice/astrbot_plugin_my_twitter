"""全局屏蔽词的配置兼容、匹配边界及实际发送入口回归。"""

import copy
import importlib.util
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from test_media_lifecycle import (
    Plain,
    _delivery_contract,
    _delivery_settings,
    _load_main_module as load_delivery_module,
)
from test_provider_initialization import (
    Plain as CommandPlain,
    _load_main_module as load_plugin_module,
)


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "content_filter_under_test", ROOT / "services/content_filter_service.py"
)
filter_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(filter_module)
TweetContentFilter = filter_module.TweetContentFilter


@pytest.mark.parametrize("keywords", [None, [], (), "", " \n\t", ["", " ", None, 123, False], 42])
def test_empty_or_invalid_keywords_leave_filter_disabled(keywords):
    content_filter = TweetContentFilter(keywords)
    assert content_filter.enabled is False
    assert content_filter.match({"text": "OC", "quote": {"text": "OC"}}) is None


@pytest.mark.parametrize("text", ["my oc", "my OC", "my Oc", "＃ＯＣ", "我的oc角色", "social"])
def test_contains_is_case_insensitive_and_normalizes_full_width(text):
    content_filter = TweetContentFilter([" oc "])
    assert content_filter.enabled is True
    assert content_filter.match({"text": text}) == "oc"


@pytest.mark.parametrize("keywords", [["other", "OC"], "other\r\nOC\n", [None, " ", 12, "OC"]])
def test_any_valid_keyword_matches_body_or_quote(keywords):
    content_filter = TweetContentFilter(keywords)
    assert content_filter.match({"text": "my oc"}) == "OC"
    assert content_filter.match({"text": "new drawing", "quote": {"text": "my oc"}}) == "OC"
    assert content_filter.match({"text": "new drawing"}) is None


@pytest.mark.parametrize(
    "text,blocked",
    [
        ("OC", True), ("#OC", True), ("＃ｏｃ", True), ("my OC!", True),
        ("(oc)", True), ("oc\nart", True), ("social", False), ("oculus", False),
        ("my_oc", False), ("oc123", False), ("我的OC", False), ("OC角色", False),
    ],
)
def test_word_mode_observes_unicode_word_boundaries(text, blocked):
    content_filter = TweetContentFilter(["oc"], match_mode="word")
    assert (content_filter.match({"text": text}) is not None) is blocked


@pytest.mark.parametrize("mode", ["contains", "word"])
def test_keywords_are_literals_and_unicode_casefold_is_supported(mode):
    literal_filter = TweetContentFilter(["[oc]", "a.b", ".*"], match_mode=mode)
    assert literal_filter.match({"text": "plain o or c, axb, everything"}) is None
    assert literal_filter.match({"text": "art [OC] today"}) == "[oc]"
    assert literal_filter.match({"text": "art a.b today"}) == "a.b"
    assert TweetContentFilter(["straße"], match_mode=mode).match({"text": "STRASSE"}) == "straße"


def test_invalid_match_mode_preserves_default_contains_behavior():
    assert TweetContentFilter(["oc"], match_mode="unknown").match({"text": "social"}) == "oc"


@pytest.mark.parametrize("quote", [None, "OC", [], {"text": None}, {"text": ["OC"]}])
def test_malformed_optional_text_is_ignored(quote):
    assert TweetContentFilter(["oc"]).match({"text": None, "quote": quote}) is None


def test_author_media_and_translation_do_not_trigger_original_text_filter():
    tweet = {
        "username": "oc_artist", "screen_name": "OC", "text": "new art",
        "translated_text": "OC", "images": ["https://example.com/oc.jpg"],
        "videos": ["https://example.com/oc.mp4"],
        "retweet": {"retweeter_username": "OC"},
        "quote": {"username": "OC", "text": "a landscape", "translated_text": "OC"},
    }
    original = copy.deepcopy(tweet)
    assert TweetContentFilter(["oc"]).match(tweet) is None
    assert tweet == original


@pytest.fixture
def plugin_module():
    return load_plugin_module()


@pytest.mark.parametrize("provider", ["nitter", "fxtwitter"])
@pytest.mark.parametrize("grouped", [False, True])
def test_config_wires_same_global_filter_for_both_sources(plugin_module, provider, grouped):
    values = {"twitter_blocked_keywords": ["OC"], "twitter_keyword_match_mode": "word"}
    config = {"basic": {"twitter_data_provider": provider}}
    config.update({"content_filter": values} if grouped else values)
    plugin = plugin_module.MyTwitterPlugin(object(), config)

    assert plugin.content_filter.match({"text": "#oc"}) == "OC"
    assert plugin.content_filter.match({"text": "social"}) is None
    assert plugin.delivery_service.content_filter is plugin.content_filter
    assert plugin.polling_service.content_filter is plugin.content_filter


def test_grouped_filter_config_wins_over_legacy_flat_values(plugin_module):
    plugin = plugin_module.MyTwitterPlugin(object(), {
        "twitter_blocked_keywords": ["legacy"],
        "twitter_keyword_match_mode": "contains",
        "content_filter": {
            "twitter_blocked_keywords": ["OC"], "twitter_keyword_match_mode": "word",
        },
    })
    assert plugin.content_filter.match({"text": "#OC"}) == "OC"
    assert plugin.content_filter.match({"text": "legacy social"}) is None


def test_missing_filter_config_keeps_existing_behavior(plugin_module):
    plugin = plugin_module.MyTwitterPlugin(object(), {})
    assert plugin.content_filter.enabled is False
    assert plugin.content_filter.match({"text": "OC"}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["plain", "node", "collective"])
@pytest.mark.parametrize("blocked_part", ["text", "quote"])
async def test_blocked_tweet_does_no_work_and_preserves_session_quota(transport, blocked_part):
    module = load_delivery_module()
    data = {"artist": {"subscribers": {"group": {"status": True}, "private": {"status": True}}}}
    subscriptions = types.SimpleNamespace(
        get_all=AsyncMock(return_value=data),
        get_retweet_seen=AsyncMock(return_value={}),
        save_retweet_seen=AsyncMock(),
        record_delivery=AsyncMock(),
        retweet_seen_by_umo=module.SubscriptionService.retweet_seen_by_umo,
        mark_retweet_seen=module.SubscriptionService.mark_retweet_seen,
    )
    messages = types.SimpleNamespace(
        maybe_translate=AsyncMock(return_value=(None, None)),
        build_message_chain=AsyncMock(return_value=[Plain("allowed art")]),
        build_nickname=lambda *args: "Artist",
        build_author_display=lambda *args: "Artist",
    )
    context = types.SimpleNamespace(send_message=AsyncMock(return_value=True))
    delivery = module.TweetDeliveryService(
        context, subscriptions, messages,
        _delivery_settings(
            module, use_node=transport != "plain", collective_forward=transport == "collective",
            deduplicate_retweets=True, max_tweets_per_session=1,
        ),
        content_filter=TweetContentFilter(["oc"]),
    )
    delivery.begin_cycle()
    blocked = {"tweet_id": "123", "username": "artist", "text": "a drawing",
               "retweet": {"retweeter_username": "artist"}}
    if blocked_part == "text":
        blocked["text"] = "my OC"
    else:
        blocked["quote"] = {"text": "my OC"}
    original = copy.deepcopy(blocked)

    result = await delivery.push_to_subscribers("artist", blocked)
    contract = _delivery_contract(module)
    assert result.state is contract.DeliveryState.SKIPPED
    assert result.counts_toward_limit is False
    assert result.recent_deliveries == ()
    assert delivery.has_collected is False
    assert blocked == original
    messages.maybe_translate.assert_not_awaited()
    messages.build_message_chain.assert_not_awaited()
    context.send_message.assert_not_awaited()
    subscriptions.save_retweet_seen.assert_not_awaited()
    subscriptions.record_delivery.assert_not_awaited()
    assert (await delivery.flush_collected()).recent_deliveries == ()
    context.send_message.assert_not_awaited()

    # One allowed tweet must still reach both sessions with a one-tweet quota.
    allowed = {**blocked, "tweet_id": "124", "text": "landscape"}
    allowed.pop("quote", None)
    result = await delivery.push_to_subscribers("artist", allowed)
    if transport == "collective":
        assert result.state is contract.DeliveryState.QUEUED
        result = await delivery.flush_collected()
    else:
        assert result.state is contract.DeliveryState.DELIVERED
    assert {call.args[0] for call in context.send_message.await_args_list} == {"group", "private"}
    assert {(record.umo, record.record["tweet_id"]) for record in result.recent_deliveries} == {
        ("group", "124"), ("private", "124"),
    }
    seen = subscriptions.save_retweet_seen.await_args.args[0]
    assert seen == {"group": ["124"], "private": ["124"]}
    delivery.end_cycle()


@pytest.fixture
def command_harness(plugin_module):
    plugin = plugin_module.MyTwitterPlugin.__new__(plugin_module.MyTwitterPlugin)
    plugin._provider_ready = True
    plugin.include_retweets = True
    plugin.link_recognition_mode = plugin_module.LINK_RECOGNITION_MODE_AUTO
    plugin.content_filter = TweetContentFilter(["OC"])
    tweet = {"status": True, "tweet_id": "123", "username": "artist", "text": "my oc"}
    plugin.twitter_api = types.SimpleNamespace(
        get_user_timeline_items=AsyncMock(return_value=[
            {"tweet_id": "123", "username": "artist"},
            {"tweet_id": "122", "username": "artist"},
        ]),
        get_tweet=AsyncMock(return_value=tweet),
    )
    plugin.message_service = types.SimpleNamespace(
        maybe_translate=AsyncMock(return_value=(None, None)),
        build_message_chain=AsyncMock(return_value=[CommandPlain("my oc")]),
        build_author_display=lambda *args: "Artist",
    )
    plugin.delivery_service = types.SimpleNamespace(
        prepare_event_delivery=lambda chain, nickname: types.SimpleNamespace(primary_chain=chain, videos=[]),
        send_prepared_videos=AsyncMock(),
    )

    def event(text):
        return types.SimpleNamespace(
            unified_msg_origin="session", message_str=text,
            plain_result=lambda value: value, chain_result=lambda chain: chain,
            stop_event=lambda: None,
        )

    return types.SimpleNamespace(plugin=plugin, event=event, tweet=tweet)


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_part", ["text", "quote"])
async def test_latest_test_command_explains_filter_without_backfilling(command_harness, blocked_part):
    h = command_harness
    if blocked_part == "quote":
        h.tweet["text"] = "a drawing"
        h.tweet["quote"] = {"text": "OC"}
    results = [result async for result in h.plugin.test_tweet(h.event("/推特测试 artist"), "artist")]

    assert any(isinstance(result, str) and "屏蔽词" in result for result in results)
    assert all(isinstance(result, str) for result in results)
    h.plugin.twitter_api.get_tweet.assert_awaited_once_with("artist", "123")
    h.plugin.message_service.maybe_translate.assert_not_awaited()
    h.plugin.message_service.build_message_chain.assert_not_awaited()
    h.plugin.delivery_service.send_prepared_videos.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["auto", "command"])
async def test_direct_links_still_parse_requested_tweet(command_harness, entrypoint):
    h = command_harness
    if entrypoint == "auto":
        generator = h.plugin.on_message(h.event("https://x.com/artist/status/123"))
    else:
        generator = h.plugin.parse_tweet_link(h.event("/推特解析 https://x.com/artist/status/123"))
    results = [result async for result in generator]

    assert len(results) == 1
    assert results[0][0].text == "my oc"
    h.plugin.message_service.maybe_translate.assert_awaited_once()
    h.plugin.twitter_api.get_tweet.assert_awaited_once_with("artist", "123")
