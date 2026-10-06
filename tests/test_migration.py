import copy
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "sentinel_migration_test", Path(__file__).resolve().parents[1] / "services/migration_service.py"
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def legacy_data():
    return {"Maker": {"screen_name": "Maker", "since_id": "123", "processed_tweet_ids": ["122", "123"],
                      "subscribers": {"default:GroupMessage:123": {
                          "status": False, "r18": True, "media": True,
                          "recent_deliveries": [{"tweet_id": "123", "text": "hello"}],
                      }}}}


def service(store, old, fail_key=None):
    async def get_new(key, default):
        return copy.deepcopy(store.get(key, default))

    async def put_new(key, value):
        if key == fail_key:
            raise OSError("disk full")
        store[key] = copy.deepcopy(value)

    async def get_old(key, default):
        return copy.deepcopy(old.get(key, default))

    return m.MigrationService(get_new, put_new, get_old)


@pytest.mark.asyncio
async def test_migration_preserves_flags_and_history_without_touching_source():
    old = {"twitter_subs": legacy_data(), "twitter_retweet_dedup_seen": {"umo": ["123"]}}
    original = copy.deepcopy(old)
    store = {}
    report = await service(store, old).run()
    assert report["authors"] == report["relations"] == 1
    assert store[m.SUBS_KEY] == old["twitter_subs"]
    assert store[m.BACKUP_KEY]["subscriptions"] == old["twitter_subs"]
    assert store[m.DEDUP_KEY] == {"umo": ["123"]}
    assert old == original
    store[m.SUBS_KEY] = {}
    await service(store, old).run()
    assert store[m.SUBS_KEY] == {}  # 用户清空后不重新复活旧数据。


@pytest.mark.asyncio
async def test_migration_can_resume_after_failed_write_and_does_not_override_new_choices():
    old = {"twitter_subs": legacy_data()}
    store = {}
    with pytest.raises(OSError):
        await service(store, old, m.DEDUP_KEY).run()
    assert m.MIGRATION_KEY not in store
    store[m.SUBS_KEY]["Maker"]["subscribers"]["default:GroupMessage:123"]["status"] = True
    report = await service(store, old).run()
    assert report["state"] == "completed"
    assert store[m.SUBS_KEY]["Maker"]["subscribers"]["default:GroupMessage:123"]["status"] is True


@pytest.mark.asyncio
async def test_absent_or_corrupt_old_data_never_marks_completed():
    store = {}
    assert (await service(store, {}).run())["state"] == "not_found"
    with pytest.raises(ValueError):
        await service(store, {"twitter_subs": {"bad": {}}}).run()
    assert m.MIGRATION_KEY not in store


def test_config_import_flat_and_grouped_is_idempotent_and_keeps_user_overrides(tmp_path):
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
    source = tmp_path / "legacy.json"
    source.write_text(json.dumps({"basic": {"twitter_proxy": "http://local:123", "twitter_poll_interval": 8},
                                  "twitter_translate_enabled": True, "twitter_poll_max_tweets_per_user": 99}),
                      encoding="utf-8-sig")
    config = {"basic": {"twitter_poll_interval": 7}}
    report = m.import_legacy_config(config, source, schema)
    assert report["state"] == "imported"
    assert config["basic"]["twitter_poll_interval"] == 7
    assert config["basic"]["twitter_proxy"] == "http://local:123"
    assert config["translation"]["twitter_translate_enabled"] is True
    assert "twitter_poll_max_tweets_per_user" not in config["basic"]
    config["basic"]["twitter_proxy"] = ""
    assert m.import_legacy_config(config, source, schema)["state"] == "unchanged"
    assert config["basic"]["twitter_proxy"] == ""
