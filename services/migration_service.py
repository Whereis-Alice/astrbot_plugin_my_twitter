"""一次性接管先前版本或复制原插件数据，只写入我的推特的独立空间。"""

import copy
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LEGACY_PLUGIN_ID = "ars1027/astrbot_plugin_twitter"
PREVIOUS_PLUGIN_ID = "whereis-alice/astrbot_plugin_x_sentinel"
PREVIOUS_SUBS_KEY = "x_sentinel_subs"
PREVIOUS_DEDUP_KEY = "x_sentinel_retweet_dedup_seen"
MIGRATION_KEY = "my_twitter_migration_v1"
BACKUP_KEY = "my_twitter_legacy_snapshot_v1"
SUBS_KEY = "my_twitter_subs"
DEDUP_KEY = "my_twitter_retweet_dedup_seen"
KVGetter = Callable[[str, Any], Awaitable[Any]]
KVSetter = Callable[[str, Any], Awaitable[None]]


def import_legacy_config(config: Any, source: Path, schema: dict) -> dict:
    """只在首次导入时复制旧配置；用户已修改的新配置优先。"""
    migration = config.get("migration", {}) or {}
    if (not migration.get("import_legacy", True) or migration.get("config_imported")
            or migration.get("previous_config_imported")):
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


def import_previous_config(config: Any, source: Path, schema: dict) -> dict:
    """改名升级保留先前版本配置；与是否导入原插件的开关、标记无关。"""
    migration = config.get("migration", {}) or {}
    if not isinstance(migration, dict):
        raise ValueError("新插件迁移配置格式错误")
    if migration.get("previous_config_imported"):
        return {"state": "unchanged", "source": PREVIOUS_PLUGIN_ID}
    if not source.is_file():
        return {"state": "not_found"}
    previous = json.loads(source.read_text(encoding="utf-8-sig"))
    if not isinstance(previous, dict):
        raise ValueError("先前版本配置不是 JSON 对象")

    before = copy.deepcopy(dict(config))
    copied = 0
    try:
        for group, group_schema in schema.items():
            fields = group_schema.get("items")
            if not isinstance(fields, dict):
                continue
            old_group = previous.get(group, {})
            if not isinstance(old_group, dict):
                raise ValueError(f"先前版本配置分组 {group} 格式错误")
            target = config.setdefault(group, {})
            if not isinstance(target, dict):
                raise ValueError(f"新插件配置分组 {group} 格式错误")
            for key, spec in fields.items():
                # 迁移完成标记属于目标安装，不沿用先前版本的旧标记。
                if group == "migration" and key != "import_legacy":
                    continue
                if key not in old_group and key not in previous:
                    continue
                current = target.get(key, config.get(key, spec.get("default")))
                if current != spec.get("default"):
                    continue
                target[key] = copy.deepcopy(old_group.get(key, previous.get(key)))
                copied += 1
        config.setdefault("migration", {})["previous_config_imported"] = True
        save = getattr(config, "save_config", None)
        if callable(save):
            save()
    except Exception:
        config.clear()
        config.update(before)
        raise
    return {"state": "imported", "source": PREVIOUS_PLUGIN_ID, "fields": copied}


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
    def __init__(
        self, get_new: KVGetter, put_new: KVSetter, get_old: KVGetter,
        get_previous: KVGetter | None = None,
    ):
        self.get_new = get_new
        self.put_new = put_new
        self.get_old = get_old
        self.get_previous = get_previous

    async def run(self, import_legacy: bool = True) -> dict:
        """保留旧订阅、开关、过滤项、游标与历史；重复执行不覆盖新数据。"""
        previous = await self.get_new(MIGRATION_KEY, None)
        if isinstance(previous, dict) and previous.get("state") == "completed":
            return previous
        snapshot = await self.get_new(BACKUP_KEY, None)
        if snapshot is not None and not isinstance(snapshot, dict):
            raise ValueError("迁移备份格式错误")
        if snapshot is None:
            old = (
                await self.get_previous(PREVIOUS_SUBS_KEY, None)
                if self.get_previous is not None else None
            )
            if old is not None:
                # 空订阅是用户已清空的有效状态；绝不能再合并更早的原插件。
                source = PREVIOUS_PLUGIN_ID
                dedup = await self.get_previous(PREVIOUS_DEDUP_KEY, {})
            else:
                if not import_legacy:
                    return {"state": "disabled", "authors": 0, "relations": 0}
                source = LEGACY_PLUGIN_ID
                old = await self.get_old("twitter_subs", None)
                dedup = await self.get_old("twitter_retweet_dedup_seen", {}) if old is not None else {}
            if old is None:
                return {"state": "not_found", "authors": 0, "relations": 0}
            subs = validate_subscriptions(old)
            if not isinstance(dedup, dict):
                raise ValueError("旧转帖去重记录格式错误")
            snapshot = {"source": source, "subscriptions": subs, "retweet_seen": copy.deepcopy(dedup)}
            # 先保存只读快照，再导入。写入中断后从同一个快照重试。
            await self.put_new(BACKUP_KEY, snapshot)
        source = snapshot.get("source", LEGACY_PLUGIN_ID)
        if source not in (PREVIOUS_PLUGIN_ID, LEGACY_PLUGIN_ID):
            raise ValueError("迁移备份来源无法识别")
        if source == LEGACY_PLUGIN_ID and not import_legacy:
            return {"state": "disabled", "authors": 0, "relations": 0}
        if not isinstance(snapshot.get("retweet_seen"), dict):
            raise ValueError("迁移备份的转帖去重记录格式错误")
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
            "state": "completed", "source": source,
            "authors": len(old_subs),
            "relations": sum(len(v["subscribers"]) for v in old_subs.values()),
            "added_relations": added,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        await self.put_new(MIGRATION_KEY, report)
        return report
