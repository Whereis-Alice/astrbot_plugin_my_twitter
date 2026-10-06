"""只推送当前新消息；启动、故障恢复与超额消息均不补发。"""

import asyncio
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

from ..twitter_api import DATA_PROVIDER_FXTWITTER, DATA_PROVIDER_NITTER, get_next_website
from .subscription_service import RecentDelivery, SubscriptionService
from .tweet_delivery_service import DeliveryResult, DeliveryState, TweetDeliveryService
from .tweet_message_service import TranslationCycleState


@dataclass(frozen=True, slots=True)
class PollingSettings:
    include_retweets: bool
    data_provider: str
    custom_nitter_url: str
    website_list: tuple[str, ...]
    max_tweets_per_user: int = 1


class PollingService:
    """有界实时通知，不维护失败重试或历史补发队列。"""

    def __init__(
        self,
        twitter_api: Any,
        subscriptions: SubscriptionService,
        delivery: TweetDeliveryService,
        settings: PollingSettings,
    ) -> None:
        self.twitter_api = twitter_api
        self.subscriptions = subscriptions
        self.delivery = delivery
        self.settings = settings
        self._synchronized: set[str] = set()
        self._pending_collective_cursors: dict[str, str] = {}
        self._pending_collective_tweet_ids: dict[str, list[str]] = {}

    @property
    def has_pending_collective(self) -> bool:
        return bool(self._pending_collective_cursors)

    def require_resync(self, username: str | None = None) -> None:
        """故障后下一次成功获取只同步，避免故障期间的推文集中出现。"""
        if username is None:
            self._synchronized.clear()
        else:
            self._synchronized.discard(username.casefold())

    async def _save_partial_history(self, deliveries: tuple[RecentDelivery, ...]) -> None:
        if not deliveries:
            return
        try:
            await self.subscriptions.save_recent_deliveries(deliveries)
        except Exception as exc:
            logger.warning(f"保存最近推送记录失败: {exc}")

    async def flush_pending_collective(self) -> None:
        """发送有界缓存；失败的批次已经消费，不会留到下轮补发。"""
        if not getattr(self.delivery, "collective_enabled", False):
            return
        if not bool(getattr(self.delivery, "has_collected", False)) and not self.has_pending_collective:
            return
        authors = tuple(self._pending_collective_cursors)
        self._pending_collective_cursors.clear()
        self._pending_collective_tweet_ids.clear()
        try:
            result = await self.delivery.flush_collected()
            for username in getattr(result, "failed_authors", ()):
                self.require_resync(username)
                logger.warning(f"@{username} 集体推送失败，本批次不重试，恢复后先同步")
            await self._save_partial_history(tuple(getattr(result, "recent_deliveries", ())))
        except Exception as exc:
            for username in authors:
                self.require_resync(username)
            clear_collected = getattr(self.delivery, "clear_collected", None)
            if callable(clear_collected):
                clear_collected()
            logger.error(f"集体推送失败，本批次不重试: {exc}")

    @staticmethod
    def attach_timeline_item_metadata(tweet_info: dict, item: dict) -> None:
        tweet_info["username"] = str(item.get("username") or tweet_info.get("username") or "")
        tweet_info["retweet"] = (
            {
                "retweeter_username": str(item.get("retweeter_username") or ""),
                "retweeter_screen_name": str(item.get("retweeter_screen_name") or ""),
            }
            if item.get("is_retweet") else None
        )

    async def check_all(self) -> None:
        """同一轮中的所有推主共用会话配额。"""
        subscribe_list = await self.subscriptions.get_snapshot()
        self._synchronized.intersection_update(str(name).casefold() for name in subscribe_list)
        if not subscribe_list:
            return
        results: list[bool] = []
        translation_cycle = TranslationCycleState()
        begin_cycle = getattr(self.delivery, "begin_cycle", None)
        if callable(begin_cycle):
            begin_cycle()
        try:
            for username, info in subscribe_list.items():
                results.append(await self.check_user(username, info, cycle=translation_cycle))
                if (
                    self.settings.data_provider == DATA_PROVIDER_FXTWITTER
                    and not bool(getattr(self.twitter_api, "is_ready", True))
                ):
                    self.require_resync()
                    logger.warning("数据源不可用，停止本轮检查；恢复后只同步游标")
                    break
                await asyncio.sleep(3)
            await self.flush_pending_collective()
        finally:
            end_cycle = getattr(self.delivery, "end_cycle", None)
            if callable(end_cycle):
                end_cycle()

        if translation_cycle.skipped:
            logger.info(f"本轮因翻译服务连续失败，{translation_cycle.skipped} 条推文使用原文")
        if (
            self.settings.data_provider == DATA_PROVIDER_NITTER
            and not self.settings.custom_nitter_url and results
            and sum(results) < len(results) / 2 and self.settings.website_list
        ):
            new_url = get_next_website(list(self.settings.website_list), self.twitter_api.nitter_url)
            if new_url and new_url != self.twitter_api.nitter_url:
                logger.info(f"当前镜像站出错过多，切换至: {new_url}")
                self.twitter_api.nitter_url = new_url
                self.require_resync()

    async def check_user(
        self, username: str, info: dict, cycle: TranslationCycleState | None = None
    ) -> bool:
        """先持久化本批次消费位置，再尝试发送；发送失败不自动重试。"""
        try:
            # 不使用旧游标翻页追历史，避免长期停机后分页失败或无限追赶。
            limit = max(20, min(100, int(self.settings.max_tweets_per_user)))
            raw_items = await self.twitter_api.get_user_timeline_items(username, "", limit=limit)
            items_by_id = {
                str(item.get("tweet_id")): item for item in raw_items
                if isinstance(item, dict) and str(item.get("tweet_id", "")).isascii()
                and str(item.get("tweet_id", "")).isdigit()
            }
            items = sorted(items_by_id.values(), key=lambda item: int(item["tweet_id"]))
            latest_subs = await self.subscriptions.get_all()
            key = self.subscriptions.find_key(latest_subs, username)
            if key is None:
                self.require_resync(username)
                return True
            current = latest_subs[key]
            since_id = str(current.get("since_id") or "")
            since_int = int(since_id) if since_id.isascii() and since_id.isdigit() else -1
            synchronized = key.casefold() in self._synchronized and since_int >= 0
            processed_ids = self.subscriptions.processed_tweet_ids(current)
            new_items = [item for item in items if int(item["tweet_id"]) > since_int
                         and str(item["tweet_id"]) not in processed_ids]

            # 写入失败时绝不开始发送；进程中断、关闭插件和部分投递均不重放本批次。
            if items:
                consumed_ids = [str(item["tweet_id"]) for item in items]
                if not await self.subscriptions.commit_processed_tweets(key, consumed_ids, consumed_ids[-1]):
                    return False
            self._synchronized.add(key.casefold())
            if not synchronized:
                logger.info(f"@{key} 已同步最新位置，本轮不补发历史推文")
                return True

            eligible = [item for item in new_items
                        if self.settings.include_retweets or not item.get("is_retweet")]
            max_tweets = max(1, int(self.settings.max_tweets_per_user))
            selected = eligible[-max_tweets:]
            skipped = len(eligible) - len(selected)
            if skipped:
                logger.info(f"@{key} 跳过 {skipped} 条过量推文，仅尝试最新 {len(selected)} 条")

            for item in selected:
                tweet_id = str(item["tweet_id"])
                tweet_info = await self.twitter_api.get_tweet(str(item.get("username") or key), tweet_id)
                if not isinstance(tweet_info, dict) or not tweet_info.get("status", True):
                    self.require_resync(key)
                    logger.warning(f"@{key} 推文 {tweet_id} 详情失败，本批次不补发")
                    return False
                self.attach_timeline_item_metadata(tweet_info, item)
                result = await self.delivery.push_to_subscribers(key, tweet_info, cycle=cycle)
                if not isinstance(result, DeliveryResult):
                    self.require_resync(key)
                    return False
                await self._save_partial_history(result.recent_deliveries)
                if result.state is DeliveryState.FAILED:
                    self.require_resync(key)
                    logger.warning(f"@{key} 推文 {tweet_id} 发送失败，本批次不补发")
                    return False
                if result.state is DeliveryState.QUEUED:
                    self._pending_collective_cursors[key] = tweet_id
            return True
        except Exception as exc:
            self.require_resync(username)
            logger.warning(f"检查 @{username} 失败，恢复后先同步且不补发: {exc}")
            return False
