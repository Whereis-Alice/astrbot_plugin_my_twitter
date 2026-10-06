"""一次性复制旧插件数据；所有写入只发生在 X 哨兵的独立空间。"""

import copy
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LEGACY_PLUGIN_ID = "ars1027/astrbot_plugin_twitter"
MIGRATION_KEY = "x_sentinel_migration_v1"
BACKUP_KEY = "x_sentinel_legacy_snapshot_v1"
SUBS_KEY = "x_sentinel_subs"
DEDUP_KEY = "x_sentinel_retweet_dedup_seen"
KVGetter = Callable[[str, Any], Awaitable[Any]]
KVSetter = Callable[[str, Any], Awaitable[None]]


def import_legacy_config(config: Any, source: Path, schema: dict) -> dict:
    """只在首次导入时复制旧配置；用户已修改的新配置优先。"""
    migration = config.get("migration", {}) or {}
    if not migration.get("import_legacy", True) or migration.get("config_imported"):
        return {"state": "unchanged"}
    if not source.is_file():
        return {"state": "not_found"}
    old = json.loads(source.read_text(encoding="utf-8-sig"))
    if not isinstance(old, dict):
        raise ValueError("旧插件配置不是 JSON 对象")
    before = copy.deepcopy(dict(config))
    copied = 0
    try:
        for group in ("basic", "message_format", "content_filter", "translation"):
            fields = schema[group]["items"]
            target = config.setdefault(group, {})
            if not isinstance(target, dict):
                raise ValueError(f"新插件配置分组 {group} 格式错误")
            old_group = old.get(group, {})
            if not isinstance(old_group, dict):
                old_group = {}
            for key, spec in fields.items():
                # 新版的流量上限不沿用旧版积压补发设置。
                if key == "twitter_poll_max_tweets_per_user":
                    continue
                old_key = key
                if key == "twitter_include_tweet_link" and key not in old:
                    old_key = "twitter_retweet_include_link"
                if key not in old_group and old_key not in old:
                    continue
                if key in target and target[key] != spec.get("default"):
                    continue
                target[key] = copy.deepcopy(old_group.get(key, old.get(old_key)))
                copied += 1
        config.setdefault("migration", {})["config_imported"] = True
        save = getattr(config, "save_config", None)
        if callable(save):
            save()
    except Exception:
        config.clear()
        config.update(before)
        raise
    return {"state": "imported", "fields": copied}


def validate_subscriptions(value: Any) -> dict:
    """拒绝损坏的数据，避免把失败误报成“迁移成功、零订阅”。"""
    if not isinstance(value, dict):
        raise ValueError("旧订阅数据不是对象")
    for username, author in value.items():
        if not isinstance(username, str) or not isinstance(author, dict):
            raise ValueError("旧订阅中存在无效推主")
        subscribers = author.get("subscribers")
        if not isinstance(subscribers, dict):
            raise ValueError(f"@{username} 缺少有效订阅关系")
        if any(not isinstance(umo, str) or not isinstance(sub, dict)
               for umo, sub in subscribers.items()):
            raise ValueError(f"@{username} 的订阅关系格式错误")
    return copy.deepcopy(value)


class MigrationService:
    def __init__(self, get_new: KVGetter, put_new: KVSetter, get_old: KVGetter):
        self.get_new = get_new
        self.put_new = put_new
        self.get_old = get_old

    async def run(self) -> dict:
        """保留旧订阅、开关、过滤项、游标与历史；重复执行不覆盖新数据。"""
        previous = await self.get_new(MIGRATION_KEY, None)
        if isinstance(previous, dict) and previous.get("state") == "completed":
            return previous
        snapshot = await self.get_new(BACKUP_KEY, None)
        if not isinstance(snapshot, dict):
            old = await self.get_old("twitter_subs", None)
            if old is None:
                return {"state": "not_found", "authors": 0, "relations": 0}
            subs = validate_subscriptions(old)
            dedup = await self.get_old("twitter_retweet_dedup_seen", {})
            if not isinstance(dedup, dict):
                raise ValueError("旧转帖去重记录格式错误")
            snapshot = {"subscriptions": subs, "retweet_seen": copy.deepcopy(dedup)}
            # 先保存只读快照，再导入。写入中断后从同一个快照重试。
            await self.put_new(BACKUP_KEY, snapshot)
        old_subs = validate_subscriptions(snapshot["subscriptions"])
        new_subs = validate_subscriptions(await self.get_new(SUBS_KEY, {}))
        added = 0
        for username, author in old_subs.items():
            key = next((k for k in new_subs if k.casefold() == username.casefold()), None)
            if key is None:
                new_subs[username] = copy.deepcopy(author)
                added += len(author["subscribers"])
            else:
                target = new_subs[key]["subscribers"]
                for umo, sub in author["subscribers"].items():
                    if umo not in target:
                        target[umo] = copy.deepcopy(sub)
                        added += 1
        dedup = copy.deepcopy(await self.get_new(DEDUP_KEY, {}))
        if not isinstance(dedup, dict):
            raise ValueError("新插件转帖去重记录格式错误")
        for umo, values in snapshot["retweet_seen"].items():
            if isinstance(values, list):
                existing = dedup.get(umo, [])
                if not isinstance(existing, list):
                    existing = []
                dedup[umo] = list(dict.fromkeys(str(x) for x in [*values, *existing]))[-500:]
        await self.put_new(SUBS_KEY, new_subs)
        await self.put_new(DEDUP_KEY, dedup)
        if await self.get_new(SUBS_KEY, None) != new_subs:
            raise RuntimeError("迁移写入校验失败")
        if await self.get_new(DEDUP_KEY, None) != dedup:
            raise RuntimeError("去重记录写入校验失败")
        report = {
            "state": "completed", "source": LEGACY_PLUGIN_ID,
            "authors": len(old_subs),
            "relations": sum(len(v["subscribers"]) for v in old_subs.values()),
            "added_relations": added,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        await self.put_new(MIGRATION_KEY, report)
        return report
