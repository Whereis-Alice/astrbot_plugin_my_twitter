import copy
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "my_twitter_migration_test", Path(__file__).resolve().parents[1] / "services/migration_service.py"
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def legacy_data():
    return {"Maker": {"screen_name": "Maker", "since_id": "123", "processed_tweet_ids": ["122", "123"],
                      "subscribers": {"default:GroupMessage:123": {
                          "status": False, "r18": True, "media": True,
                          "recent_deliveries": [{"tweet_id": "123", "text": "hello"}],
                      }}}}


def service(store, old, fail_key=None, previous=None):
    async def get_new(key, default):
        return copy.deepcopy(store.get(key, default))

    async def put_new(key, value):
        if key == fail_key:
            raise OSError("disk full")
        store[key] = copy.deepcopy(value)

    async def get_old(key, default):
        return copy.deepcopy(old.get(key, default))

    async def get_previous(key, default):
        return copy.deepcopy((previous or {}).get(key, default))

    return m.MigrationService(get_new, put_new, get_old, get_previous)


@pytest.mark.asyncio
async def test_migration_preserves_flags_and_history_without_touching_source():
    old = {"twitter_subs": legacy_data(), "twitter_retweet_dedup_seen": {"umo": ["123"]}}
    original = copy.deepcopy(old)
    store = {}
    report = await service(store, old).run()
    assert report["authors"] == report["relations"] == 1
    assert store[m.SUBS_KEY] == old["twitter_subs"]
    assert store[m.BACKUP_KEY]["subscriptions"] == old["twitter_subs"]
    assert store[m.BACKUP_KEY]["source"] == m.LEGACY_PLUGIN_ID
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


@pytest.mark.asyncio
@pytest.mark.parametrize("subscriptions", [{}, legacy_data()])
async def test_previous_namespace_has_priority_including_empty_subscriptions(subscriptions):
    old = {"twitter_subs": {"Deleted": legacy_data()["Maker"]}}
    previous = {m.PREVIOUS_SUBS_KEY: subscriptions, m.PREVIOUS_DEDUP_KEY: {"room": ["9"]},
                "x_sentinel_migration_v1": {"state": "completed", "source": m.LEGACY_PLUGIN_ID}}
    original = copy.deepcopy(previous)
    store = {}
    report = await service(store, old, previous=previous).run()
    assert report["state"] == "completed"
    assert report["source"] == m.PREVIOUS_PLUGIN_ID
    assert store[m.SUBS_KEY] == subscriptions
    assert store[m.DEDUP_KEY] == {"room": ["9"]}
    assert store[m.BACKUP_KEY]["source"] == m.PREVIOUS_PLUGIN_ID
    assert previous == original
    store[m.SUBS_KEY] = {}
    await service(store, old, previous=previous).run()
    assert store[m.SUBS_KEY] == {}


@pytest.mark.asyncio
async def test_disabled_legacy_import_does_not_block_previous_import():
    previous = {m.PREVIOUS_SUBS_KEY: legacy_data()}
    store = {}
    report = await service(store, {}, previous=previous).run(import_legacy=False)
    assert report["source"] == m.PREVIOUS_PLUGIN_ID
    assert store[m.SUBS_KEY] == legacy_data()


@pytest.mark.asyncio
async def test_disabled_legacy_import_does_not_read_legacy_namespace():
    instance = service({}, {"twitter_subs": legacy_data()})

    async def forbidden(*_args):
        raise AssertionError("关闭原插件导入后不应读取原插件命名空间")

    instance.get_old = forbidden
    assert (await instance.run(import_legacy=False))["state"] == "disabled"


@pytest.mark.asyncio
async def test_corrupt_previous_data_does_not_fall_back_to_older_source():
    store = {}
    with pytest.raises(ValueError):
        await service(store, {"twitter_subs": legacy_data()}, previous={
            m.PREVIOUS_SUBS_KEY: {"broken": {}},
        }).run()
    assert m.MIGRATION_KEY not in store
    assert m.SUBS_KEY not in store


@pytest.mark.asyncio
async def test_previous_snapshot_retries_without_original_namespace():
    previous = {m.PREVIOUS_SUBS_KEY: legacy_data(), m.PREVIOUS_DEDUP_KEY: {"room": ["123"]}}
    store = {}
    with pytest.raises(OSError):
        await service(store, {}, fail_key=m.DEDUP_KEY, previous=previous).run(import_legacy=False)
    assert m.MIGRATION_KEY not in store
    assert store[m.BACKUP_KEY]["source"] == m.PREVIOUS_PLUGIN_ID
    # 移除旧 namespace 后，仍从第一次保存的同一快照恢复。
    report = await service(store, {}).run(import_legacy=False)
    assert report["source"] == m.PREVIOUS_PLUGIN_ID
    assert store[m.SUBS_KEY] == previous[m.PREVIOUS_SUBS_KEY]
    assert store[m.DEDUP_KEY] == previous[m.PREVIOUS_DEDUP_KEY]


def _schema():
    root = Path(__file__).resolve().parents[1]
    return json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))


def test_previous_config_copies_all_groups_and_uses_separate_marker(tmp_path):
    source = tmp_path / "sentinel.json"
    source.write_text(json.dumps({
        "basic": {"twitter_proxy": "http://sentinel:7890", "twitter_poll_max_tweets_per_user": 2},
        "message_format": {"twitter_video_max_size_mb": 64},
        "content_filter": {"twitter_link_recognition_enabled": "command"},
        "delivery_guard": {"max_tweets_per_session": 2, "recovery_gap_minutes": 42},
        "migration": {"config_imported": True, "previous_config_imported": True},
    }), encoding="utf-8-sig")
    config = {"basic": {"twitter_proxy": ""},
              "migration": {"import_legacy": False, "config_imported": True}}
    report = m.import_previous_config(config, source, _schema())
    assert report["state"] == "imported"
    assert config["basic"]["twitter_proxy"] == "http://sentinel:7890"
    assert config["basic"]["twitter_poll_max_tweets_per_user"] == 2
    assert config["message_format"]["twitter_video_max_size_mb"] == 64
    assert config["content_filter"]["twitter_link_recognition_enabled"] == "command"
    assert config["delivery_guard"] == {"max_tweets_per_session": 2}
    assert config["migration"]["import_legacy"] is False
    assert config["migration"]["previous_config_imported"] is True
    config["basic"]["twitter_proxy"] = ""
    source.unlink()
    assert m.import_previous_config(config, source, _schema())["state"] == "unchanged"
    assert config["basic"]["twitter_proxy"] == ""


def test_previous_config_keeps_new_user_values_and_old_import_preference(tmp_path):
    source = tmp_path / "sentinel.json"
    source.write_text(json.dumps({
        "basic": {"twitter_poll_interval": 9, "twitter_proxy": "http://old:7890"},
        "migration": {"import_legacy": False, "config_imported": True},
    }), encoding="utf-8")
    config = {"basic": {"twitter_poll_interval": 7}, "twitter_proxy": "http://new:7890"}
    m.import_previous_config(config, source, _schema())
    assert config["basic"]["twitter_poll_interval"] == 7
    assert "twitter_proxy" not in config["basic"]
    assert config["twitter_proxy"] == "http://new:7890"
    assert config["migration"]["import_legacy"] is False
    assert "config_imported" not in config["migration"]


def test_previous_config_empty_is_authoritative_and_blocks_legacy_import(tmp_path):
    previous = tmp_path / "sentinel.json"
    previous.write_text("{}", encoding="utf-8")
    older = tmp_path / "legacy.json"
    older.write_text(json.dumps({"twitter_proxy": "http://old:7890"}), encoding="utf-8")
    config = {}
    assert m.import_previous_config(config, previous, _schema())["state"] == "imported"
    assert m.import_legacy_config(config, older, _schema())["state"] == "unchanged"
    assert config["basic"].get("twitter_proxy") is None


def test_previous_config_missing_or_corrupt_file_does_not_mark_imported(tmp_path):
    source = tmp_path / "sentinel.json"
    config = {}
    assert m.import_previous_config(config, source, _schema())["state"] == "not_found"
    source.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        m.import_previous_config(config, source, _schema())
    assert config == {}


def test_previous_config_save_failure_rolls_back_all_in_memory_changes(tmp_path):
    class FailedConfig(dict):
        def save_config(self):
            raise OSError("disk unavailable")

    source = tmp_path / "sentinel.json"
    source.write_text(json.dumps({"basic": {"twitter_proxy": "http://old:7890"}}), encoding="utf-8")
    config = FailedConfig({"basic": {"twitter_poll_interval": 7}})
    original = copy.deepcopy(config)
    with pytest.raises(OSError):
        m.import_previous_config(config, source, _schema())
    assert config == original
