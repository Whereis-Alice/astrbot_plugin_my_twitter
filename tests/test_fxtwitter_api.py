import copy
import importlib.util
import json
import sys
import types
from pathlib import Path

import httpx
import pytest


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


class _Logger:
    def __init__(self):
        self.messages = []

    def __getattr__(self, name):
        def log(message, *_args, **_kwargs):
            self.messages.append((name, str(message)))

        return log


def _load_twitter_api_module():
    module_name = "twitter_api_fxtwitter_test"
    sys.modules.pop(module_name, None)

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = _Logger()
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api

    spec = importlib.util.spec_from_file_location(module_name, ROOT / "twitter_api.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def api_module():
    return _load_twitter_api_module()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body", [(503, "unavailable"), (200, "<html>challenge</html>")])
async def test_nitter_failure_is_not_a_successful_empty_timeline(api_module, status, body):
    api = api_module.TwitterAPI(nitter_url="https://nitter.example")
    api._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, text=body, request=request)
    ))
    with pytest.raises(api_module.FxTwitterTimelineError):
        await api.get_user_timeline_items("tester", "123")
    await api.close()


@pytest.mark.asyncio
async def test_nitter_protected_account_is_classified_separately(api_module):
    body = """
    <div class="timeline-header timeline-protected">
      <h2>This account's tweets are protected.</h2>
      <p>Only confirmed followers have access to @tester's tweets.</p>
    </div>
    """
    api = api_module.TwitterAPI(nitter_url="https://nitter.example")
    api._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text=body, request=request)
    ))
    with pytest.raises(api_module.NitterProtectedAccountError, match="受保护"):
        await api.get_user_timeline_items("tester")
    await api.close()


@pytest.mark.asyncio
async def test_nitter_health_check_requires_timeline_page(api_module):
    healthy_body = "<div class='timeline'><div class='timeline-item'></div></div>"

    def handler(request):
        if request.url.host == "challenge.example":
            return httpx.Response(
                200,
                text="<html><title>Just a moment...</title></html>",
                request=request,
            )
        return httpx.Response(200, text=healthy_body, request=request)

    api = api_module.TwitterAPI()
    api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await api.check_website_available(
        ["https://challenge.example", "https://healthy.example"]
    )

    assert result == "https://healthy.example"
    assert api.nitter_url == "https://healthy.example"
    assert api.provider_ready is True
    await api.close()


@pytest.mark.asyncio
async def test_nitter_health_check_rejects_http_200_without_timeline(api_module):
    api = api_module.TwitterAPI()
    api.provider_ready = True
    api._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            text="<html><title>Verify you are human</title></html>",
            request=request,
        )
    ))

    assert await api.check_website_available(["https://challenge.example"]) is None
    assert api.provider_ready is False
    await api.close()


def test_provider_defaults_and_base_normalization(api_module):
    default_api = api_module.TwitterAPI()
    assert default_api.provider == "nitter"
    assert not default_api.is_ready

    fx_api = api_module.TwitterAPI(
        provider="fxtwitter",
        fxtwitter_api_base="https://api.fxtwitter.com///",
    )
    assert fx_api.provider == "fxtwitter"
    assert fx_api.fxtwitter_api_base == "https://api.fxtwitter.com"


def test_adapts_plain_text_status(api_module):
    api = api_module.TwitterAPI(provider="fxtwitter")
    result = api._adapt_fxtwitter_status(_fixture("fxtwitter_text_status.json"))

    assert result["status"] is True
    assert result["tweet_id"] == "1005"
    assert result["username"] == "tester"
    assert result["screen_name"] == "Test User"
    assert result["verified"] is True
    assert result["text"] == "plain text"
    assert result["stats"] == {
        "comments": "1",
        "retweets": "2",
        "likes": "3",
        "views": "4",
    }
    assert result["date"].startswith("2026-07-")
    assert result["url"] == "https://x.com/tester/status/1005"


def test_adapts_images_and_selects_highest_bitrate_video(api_module):
    api = api_module.TwitterAPI(provider="fxtwitter", image_quality="large")
    image = api._adapt_fxtwitter_status(_fixture("fxtwitter_image_status.json"))
    video = api._adapt_fxtwitter_status(_fixture("fxtwitter_video_status.json"))

    assert image["images"] == ["https://pbs.twimg.com/media/photo.jpg?name=large"]
    assert video["videos"] == ["https://video.twimg.com/video/high.mp4"]
    assert video["video_previews"] == [
        {
            "poster": "https://pbs.twimg.com/video_thumb/v1.jpg",
            "duration": "01:05",
        }
    ]


def test_mixed_media_keeps_animated_gif_and_variants_without_ids(api_module):
    api = api_module.TwitterAPI(provider="fxtwitter")
    image = {"id": "photo", "type": "photo", "url": "https://pbs.twimg.com/photo.jpg"}
    video = {"id": "v1", "type": "video", "url": "https://video.twimg.com/normal.mp4"}
    gif = {"type": "animated_gif", "variants": [
        {"content_type": "video/mp4", "url": "https://video.twimg.com/gif.mp4", "bitrate": "unknown"}
    ]}
    images, videos, _ = api._extract_fxtwitter_media({"media": {
        "photos": [image], "videos": [video], "all": [image, video, gif],
    }})
    assert images == [image["url"]]
    assert videos == [video["url"], "https://video.twimg.com/gif.mp4"]


def test_video_prefers_transcode_over_hls_url(api_module):
    assert api_module.TwitterAPI._select_fxtwitter_video_url({
        "url": "https://video.twimg.com/master.m3u8",
        "transcode_url": "https://video.twimg.com/transcoded.mp4",
    }) == "https://video.twimg.com/transcoded.mp4"


def test_nitter_gif_selects_one_direct_encoding_and_excludes_quotes(api_module):
    api = api_module.TwitterAPI(nitter_url="https://nitter.example")
    container = api_module.BeautifulSoup('''<div class="main-tweet">
      <div class="gallery-gif"><video poster="/pic/gif.jpg" data-url="/v/master.m3u8">
        <source src="//video.twimg.com/gif.mp4" type="video/mp4">
        <source src="/v/alternate.mp4" type="video/mp4">
      </video></div>
      <div class="quote"><video src="/v/quote.mp4"></video></div>
    </div>''', "html.parser").select_one(".main-tweet")
    assert api._extract_videos(container) == ["https://video.twimg.com/gif.mp4"]
    assert api._extract_video_previews(container) == [{
        "poster": "https://nitter.example/pic/gif.jpg", "duration": "",
    }]


@pytest.mark.asyncio
@pytest.mark.parametrize("recovered", [True, False])
async def test_nitter_disabled_video_recovers_mp4_or_keeps_explicit_link(api_module, recovered):
    api = api_module.TwitterAPI(nitter_url="https://nitter.example")
    tweet = {"username": "tester", "tweet_id": "123", "video_previews": [{"poster": "thumb"}], "videos": []}

    async def request(path, retries=2):
        assert path == "2/status/123"
        assert retries == 1
        return {"status": {"media": {"videos": [{"url": "https://video.twimg.com/recovered.mp4"}]}}} if recovered else None

    api._request_fxtwitter_json = request
    await api._recover_nitter_videos(tweet)
    if recovered:
        assert tweet["videos"] == ["https://video.twimg.com/recovered.mp4"]
        assert "media_unavailable" not in tweet
    else:
        assert "https://x.com/tester/status/123" in tweet["media_unavailable"]


@pytest.mark.asyncio
async def test_media_without_length_header_still_has_download_limit(api_module, monkeypatch):
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"abc"
            yield b"def"

    api = api_module.TwitterAPI()
    api._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, stream=Body())
    ))
    monkeypatch.setattr(api_module, "MEDIA_DOWNLOAD_MAX_BYTES", 5)
    with pytest.raises(ValueError, match="大小限制"):
        await api.download_media("https://example.com/image.jpg")
    await api.close()


def test_adapts_retweet_quote_sensitive_and_missing_fields(api_module):
    api = api_module.TwitterAPI(provider="fxtwitter")
    retweet = api._adapt_fxtwitter_status(_fixture("fxtwitter_retweet_status.json"))
    quoted = api._adapt_fxtwitter_status(_fixture("fxtwitter_quote_status.json"))
    sensitive = api._adapt_fxtwitter_status(_fixture("fxtwitter_sensitive_status.json"))
    missing = api._adapt_fxtwitter_status(
        _fixture("fxtwitter_missing_fields_status.json")
    )

    assert retweet["username"] == "original"
    assert retweet["retweet"] == {
        "retweeter_username": "retweeter",
        "retweeter_screen_name": "Retweeter",
    }
    assert quoted["quote"]["username"] == "quoted"
    assert quoted["quote"]["text"] == "quoted text"
    assert quoted["quote"]["images"] == [
        "https://pbs.twimg.com/media/quoted.jpg?name=orig"
    ]
    assert sensitive["is_r18"] is True
    assert missing["status"] is True
    assert missing["screen_name"] == ""
    assert missing["images"] == []
    assert missing["quote"] is None


@pytest.mark.asyncio
async def test_timeline_cursor_since_id_order_dedup_and_cache(api_module):
    api = api_module.TwitterAPI(
        provider="fxtwitter", fxtwitter_max_pages=4, fxtwitter_max_items=100
    )
    page1 = _fixture("fxtwitter_timeline_page1.json")
    page2 = _fixture("fxtwitter_timeline_page2.json")
    calls = []

    async def request(_path, params=None, retries=2):
        calls.append(dict(params or {}))
        return page2 if params and params.get("cursor") == "page-2" else page1

    api._request_fxtwitter_json = request
    items = await api.get_user_timeline_items("tester", since_id="101")

    assert [item["tweet_id"] for item in items] == ["102", "103", "104", "105"]
    assert len(calls) == 2
    assert calls[1]["cursor"] == "page-2"
    assert items[2]["is_retweet"] is True
    assert items[2]["username"] == "original"
    assert items[2]["retweeter_username"] == "tester"
    assert "999" not in api._status_cache

    cached = await api.get_tweet("original", "104")
    assert cached["status"] is True
    assert cached["retweet"] is None
    assert "reposted_by" not in api._status_cache["104"]


@pytest.mark.asyncio
async def test_retweet_cache_does_not_pollute_direct_status_lookup(api_module):
    api = api_module.TwitterAPI(provider="fxtwitter")
    retweet_status = _fixture("fxtwitter_retweet_status.json")
    timeline = {
        "code": 200,
        "results": [retweet_status],
        "cursor": {"top": None, "bottom": None},
    }

    async def request(_path, params=None, retries=2):
        return timeline

    api._request_fxtwitter_json = request
    items = await api.get_user_timeline_items("retweeter")
    direct_status = await api.get_tweet("original", "1008")

    assert items[0]["is_retweet"] is True
    assert items[0]["retweeter_username"] == "retweeter"
    assert direct_status["status"] is True
    assert direct_status["retweet"] is None
    assert "reposted_by" not in api._status_cache["1008"]


@pytest.mark.asyncio
async def test_timeline_discards_results_when_first_page_fails(api_module):
    api = api_module.TwitterAPI(provider="fxtwitter")

    async def request(_path, params=None, retries=2):
        return None

    api._request_fxtwitter_json = request

    with pytest.raises(api_module.FxTwitterTimelineError, match="首页请求失败"):
        await api.get_user_timeline_items("tester", since_id="101")
    assert api._status_cache == {}


@pytest.mark.asyncio
async def test_timeline_discards_partial_results_when_later_page_fails(api_module):
    api = api_module.TwitterAPI(provider="fxtwitter")
    page1 = _fixture("fxtwitter_timeline_page1.json")

    async def request(_path, params=None, retries=2):
        return None if params and params.get("cursor") else page1

    api._request_fxtwitter_json = request

    with pytest.raises(api_module.FxTwitterTimelineError, match="第 2 页请求失败"):
        await api.get_user_timeline_items("tester", since_id="101")
    assert api._status_cache == {}


@pytest.mark.asyncio
async def test_timeline_page_limit_before_since_id_is_incomplete(api_module):
    api = api_module.TwitterAPI(provider="fxtwitter", fxtwitter_max_pages=1)
    page1 = _fixture("fxtwitter_timeline_page1.json")

    async def request(_path, params=None, retries=2):
        return page1

    api._request_fxtwitter_json = request

    with pytest.raises(api_module.FxTwitterTimelineError, match="尚未找到上次游标"):
        await api.get_user_timeline_items("tester", since_id="101")
    assert api._status_cache == {}


@pytest.mark.asyncio
async def test_timeline_deduplicates_ids_across_pages(api_module):
    api = api_module.TwitterAPI(provider="fxtwitter")
    page1 = _fixture("fxtwitter_timeline_page1.json")
    page2 = copy.deepcopy(_fixture("fxtwitter_timeline_page2.json"))
    page2["results"].insert(0, copy.deepcopy(page1["results"][0]))

    async def request(_path, params=None, retries=2):
        return page2 if params and params.get("cursor") == "page-2" else page1

    api._request_fxtwitter_json = request
    items = await api.get_user_timeline_items("tester", since_id="101")

    assert [item["tweet_id"] for item in items] == ["102", "103", "104", "105"]


@pytest.mark.asyncio
async def test_provider_clients_use_matching_request_headers(api_module):
    nitter_api = api_module.TwitterAPI(provider="nitter")
    fxtwitter_api = api_module.TwitterAPI(provider="fxtwitter")

    nitter_client = await nitter_api._get_client()
    fxtwitter_client = await fxtwitter_api._get_client()

    assert nitter_client.headers["user-agent"].startswith("Mozilla/5.0")
    assert "text/html" in nitter_client.headers["accept"]
    assert fxtwitter_client.headers["user-agent"].startswith(
        "AstrBot-My-Twitter/"
    )
    assert fxtwitter_client.headers["accept"] == "application/json"

    await nitter_api.close()
    await fxtwitter_api.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type",
    [httpx.ConnectError, httpx.ReadTimeout],
)
async def test_transport_failure_rebuilds_client_during_retry(
    api_module,
    monkeypatch,
    error_type,
):
    def broken_handler(request):
        raise error_type(
            "proxy tunnel unavailable",
            request=request,
        )

    def recovered_handler(_request):
        return httpx.Response(
            200,
            json={"code": 200, "results": []},
        )

    broken_client = httpx.AsyncClient(
        transport=httpx.MockTransport(broken_handler)
    )
    recovered_client = httpx.AsyncClient(
        transport=httpx.MockTransport(recovered_handler)
    )

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(api_module.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(
        api_module.httpx,
        "AsyncClient",
        lambda **_kwargs: recovered_client,
    )

    api = api_module.TwitterAPI(provider="fxtwitter")
    api._client = broken_client
    payload = await api._request_fxtwitter_json(
        "2/profile/tester/statuses",
        retries=2,
    )

    assert payload == {"code": 200, "results": []}
    assert broken_client.is_closed
    assert api._client is recovered_client
    assert api.is_ready is True

    await api.close()
    assert recovered_client.is_closed


@pytest.mark.asyncio
async def test_stale_client_invalidation_does_not_close_replacement(api_module):
    api = api_module.TwitterAPI(provider="fxtwitter")
    stale_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: None))
    api._client = stale_client

    assert await api._invalidate_client(stale_client) is True
    replacement = await api._get_client()
    assert await api._invalidate_client(stale_client) is False

    assert stale_client.is_closed
    assert api._client is replacement
    assert not replacement.is_closed
    await api.close()


@pytest.mark.asyncio
async def test_first_subscription_uses_only_latest_id(api_module):
    api = api_module.TwitterAPI(provider="fxtwitter")
    page = _fixture("fxtwitter_timeline_page1.json")

    async def request(_path, params=None, retries=2):
        return page

    api._request_fxtwitter_json = request
    latest_ids = await api.get_user_newtimeline("tester")

    assert latest_ids == ["105"]


@pytest.mark.asyncio
async def test_invalid_since_id_never_replays_history(api_module):
    api = api_module.TwitterAPI(provider="fxtwitter")
    called = False

    async def request(_path, params=None, retries=2):
        nonlocal called
        called = True
        return _fixture("fxtwitter_timeline_page1.json")

    api._request_fxtwitter_json = request
    items = await api.get_user_timeline_items("tester", since_id="corrupt")

    assert items == []
    assert called is False


@pytest.mark.asyncio
async def test_profile_health_check_and_client_close(api_module):
    profile = _fixture("fxtwitter_profile.json")
    timeline = _fixture("fxtwitter_timeline_page1.json")

    def handler(request):
        if request.url.path == "/2/profile/tester":
            return httpx.Response(200, json=profile)
        if request.url.path == "/2/profile/elonmusk/statuses":
            return httpx.Response(200, json=timeline)
        return httpx.Response(404, json={"code": 404})

    api = api_module.TwitterAPI(provider="fxtwitter")
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    )
    api._client = client

    user = await api.get_user_info("tester")
    available = await api.check_fxtwitter_available()

    assert user == {
        "status": True,
        "screen_name": "Test User",
        "bio": "profile bio",
        "user_name": "tester",
    }
    assert available is True
    assert api.is_ready is True
    assert await api._get_client() is client

    await api.close()
    assert client.is_closed
    assert api._client is None


@pytest.mark.asyncio
async def test_single_status_endpoint_supports_link_recognition(api_module):
    status = _fixture("fxtwitter_video_status.json")

    def handler(request):
        if request.url.path == "/2/status/1007":
            return httpx.Response(200, json={"code": 200, "status": status})
        return httpx.Response(
            404, json={"code": 404, "status": None, "message": "not found"}
        )

    api = api_module.TwitterAPI(provider="fxtwitter")
    api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    found = await api.get_tweet("ignored-handle", "1007")
    missing = await api.get_tweet("tester", "9999")

    assert found["status"] is True
    assert found["username"] == "tester"
    assert found["videos"] == ["https://video.twimg.com/video/high.mp4"]
    assert missing["status"] is False
    assert missing["tweet_id"] == "9999"
    await api.close()


@pytest.mark.asyncio
async def test_rate_limit_api_error_and_invalid_json_are_safe(api_module):
    rate_limit = _fixture("fxtwitter_rate_limit.json")
    api_error = _fixture("fxtwitter_api_error.json")

    def handler(request):
        if request.url.path.endswith("/rate"):
            return httpx.Response(429, json=rate_limit, headers={"Retry-After": "60"})
        if request.url.path.endswith("/missing"):
            return httpx.Response(404, json=api_error)
        return httpx.Response(200, text="not-json")

    api = api_module.TwitterAPI(provider="fxtwitter")
    api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    api.provider_ready = True
    assert await api._request_fxtwitter_json("rate") is None
    assert api.is_ready is False

    api.provider_ready = True
    assert await api._request_fxtwitter_json("missing") is None
    assert api.is_ready is True

    assert await api._request_fxtwitter_json("invalid") is None
    assert api.is_ready is False

    messages = [
        message for _level, message in sys.modules["astrbot.api"].logger.messages
    ]
    assert any("Retry-After=60" in message for message in messages)
    assert any("JSON 解码失败" in message for message in messages)
    await api.close()


@pytest.mark.asyncio
async def test_exhausted_server_errors_mark_provider_unavailable(api_module):
    def handler(_request):
        return httpx.Response(503, json={"code": 503})

    api = api_module.TwitterAPI(provider="fxtwitter")
    api.provider_ready = True
    api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    assert await api._request_fxtwitter_json("unavailable", retries=1) is None
    assert api.is_ready is False
    await api.close()


@pytest.mark.asyncio
async def test_exhausted_transport_errors_leave_fresh_start_for_next_poll(
    api_module,
    monkeypatch,
):
    def handler(request):
        raise httpx.ProxyError("proxy unavailable", request=request)

    first_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    retry_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(api_module.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(
        api_module.httpx,
        "AsyncClient",
        lambda **_kwargs: retry_client,
    )

    api = api_module.TwitterAPI(provider="fxtwitter")
    api.provider_ready = True
    api._client = first_client

    assert await api._request_fxtwitter_json("failed", retries=2) is None
    assert api.is_ready is False
    assert api._client is None
    assert first_client.is_closed
    assert retry_client.is_closed
