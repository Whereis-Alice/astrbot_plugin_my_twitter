"""
AstrBot 我的推特插件

支持 Nitter 与 FxTwitter API 数据源，以及订阅、定时推送、链接识别、
合并转发消息和推文翻译。
"""

import asyncio
import copy
import json
import re
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Node, Nodes, Plain
from astrbot.api.star import Context, Star, StarTools

from .services import (
    PollingService,
    PollingSettings,
    SubscriptionService,
    TweetDeliveryService,
    TweetDeliverySettings,
    TweetMessageService,
    TweetMessageSettings,
)
from .services.avatar_cache_service import AvatarCacheService
from .services.migration_service import (
    LEGACY_PLUGIN_ID, PREVIOUS_PLUGIN_ID, MIGRATION_KEY, MigrationService,
    import_legacy_config, import_previous_config,
)
from .twitter_api import (
    DATA_PROVIDER_FXTWITTER,
    DATA_PROVIDER_NITTER,
    DATA_PROVIDER_OPTIONS,
    DEFAULT_FXTWITTER_API_BASE,
    FxTwitterTimelineError,
    TwitterAPI,
    WEBSITE_LIST,
)

try:
    from .twitter_webui import MyTwitterWebUIController
except ModuleNotFoundError as exc:
    if exc.name != "astrbot.api.web":
        raise
    MyTwitterWebUIController = None


TWITTER_LINK_PATTERN = re.compile(
    r"(https?://(?:twitter\.com|x\.com)/([a-zA-Z0-9_]+)/status/(\d+))"
)
TWITTER_PARSE_COMMAND_PATTERN = re.compile(
    r"^\s*/(?:推特解析|twitter_parse)(?:\s|$)",
    re.IGNORECASE,
)

LINK_RECOGNITION_MODE_AUTO = "auto"
LINK_RECOGNITION_MODE_OFF = "off"
LINK_RECOGNITION_MODE_COMMAND = "command"
LINK_RECOGNITION_MODES = {
    LINK_RECOGNITION_MODE_AUTO,
    LINK_RECOGNITION_MODE_OFF,
    LINK_RECOGNITION_MODE_COMMAND,
}


def _normalize_link_recognition_mode(value: Any) -> str:
    """规范链接解析模式，并兼容旧版布尔配置。"""
    if isinstance(value, bool):
        return (
            LINK_RECOGNITION_MODE_AUTO
            if value
            else LINK_RECOGNITION_MODE_OFF
        )

    normalized = str(value or "").strip().lower()
    legacy_strings = {
        "true": LINK_RECOGNITION_MODE_AUTO,
        "false": LINK_RECOGNITION_MODE_OFF,
    }
    normalized = legacy_strings.get(normalized, normalized)
    if normalized in LINK_RECOGNITION_MODES:
        return normalized

    logger.warning(
        f"未知推文链接解析模式: {value!r}，已回退为 auto"
    )
    return LINK_RECOGNITION_MODE_AUTO


class MyTwitterPlugin(Star):
    """我的推特插件主类。"""

    @property
    def _provider_ready(self) -> bool:
        """返回与数据访问层同步的数据源状态。"""
        if (
            getattr(self, "data_provider", None) == DATA_PROVIDER_FXTWITTER
            and hasattr(self, "twitter_api")
        ):
            return bool(self.twitter_api.is_ready)
        return bool(getattr(self, "_provider_ready_state", False))

    @_provider_ready.setter
    def _provider_ready(self, value: bool) -> None:
        ready = bool(value)
        self._provider_ready_state = ready
        if (
            getattr(self, "data_provider", None) == DATA_PROVIDER_FXTWITTER
            and hasattr(self, "twitter_api")
        ):
            self.twitter_api.provider_ready = ready

    def _cfg(self, block: str, key: str, default, *legacy_keys: str):
        """读取分组配置，并兼容旧版顶层扁平配置。"""
        block_config = self.config.get(block, {}) or {}
        if isinstance(block_config, dict):
            value = block_config.get(key)
            if value is not None:
                return value

        for config_key in (key, *legacy_keys):
            value = self.config.get(config_key)
            if value is not None:
                return value

        return default

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self._migration_report: dict = {"state": "pending"}
        self._config_migration_error = ""
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_config_path
            source = Path(get_astrbot_config_path()) / "astrbot_plugin_twitter_config.json"
            schema = json.loads((Path(__file__).parent / "_conf_schema.json").read_text(encoding="utf-8"))
            previous_source = source.with_name("astrbot_plugin_x_sentinel_config.json")
            previous = import_previous_config(config, previous_source, schema)
            if previous["state"] == "not_found":
                import_legacy_config(config, source, schema)
        except ImportError:
            # 旧 SDK 没有路径辅助 API 时允许插件继续启动。
            pass
        except Exception as exc:
            self._config_migration_error = str(exc)
            logger.error(f"我的推特旧配置导入失败，自动推送将暂停: {exc}")

        self.proxy = str(self._cfg("basic", "twitter_proxy", "") or "") or None
        self.data_provider = str(
            self._cfg(
                "basic",
                "twitter_data_provider",
                DATA_PROVIDER_NITTER,
            )
            or DATA_PROVIDER_NITTER
        ).strip().lower()
        if self.data_provider not in DATA_PROVIDER_OPTIONS:
            logger.warning(
                f"未知 Twitter 数据源: {self.data_provider}，已回退为 nitter"
            )
            self.data_provider = DATA_PROVIDER_NITTER

        self.fxtwitter_api_base = str(
            self._cfg(
                "basic",
                "twitter_fxtwitter_api_base",
                DEFAULT_FXTWITTER_API_BASE,
            )
            or DEFAULT_FXTWITTER_API_BASE
        ).strip().rstrip("/") or DEFAULT_FXTWITTER_API_BASE
        self.use_node = bool(
            self._cfg("message_format", "twitter_use_node", True)
        )
        self.no_text = bool(
            self._cfg("message_format", "twitter_no_text", False)
        )
        self.send_media_separately = bool(
            self._cfg(
                "message_format",
                "twitter_send_media_separately",
                True,
            )
        )
        self.link_recognition_mode = _normalize_link_recognition_mode(
            self._cfg(
                "content_filter",
                "twitter_link_recognition_enabled",
                LINK_RECOGNITION_MODE_AUTO,
            )
        )
        self.poll_interval = max(
            1,
            int(self._cfg("basic", "twitter_poll_interval", 5)),
        )
        self.poll_max_tweets_per_user = max(
            1,
            int(
                self._cfg(
                    "basic",
                    "twitter_poll_max_tweets_per_user",
                    1,
                )
            ),
        )
        self.collective_forward = bool(
            self._cfg(
                "message_format",
                "twitter_collective_forward",
                False,
            )
        )
        self.include_retweets = bool(
            self._cfg(
                "content_filter",
                "twitter_include_retweets",
                True,
            )
        )
        self.deduplicate_retweets = bool(
            self._cfg(
                "content_filter",
                "twitter_deduplicate_retweets",
                False,
            )
        )
        self.include_tweet_link = bool(
            self._cfg(
                "message_format",
                "twitter_include_tweet_link",
                True,
                "twitter_retweet_include_link",
            )
        )
        self.text_render_mode = str(
            self._cfg(
                "message_format",
                "twitter_text_render_mode",
                "text",
            )
            or "text"
        ).strip().lower()
        if self.text_render_mode not in ("text", "screenshot"):
            logger.warning(
                f"未知推文文本渲染模式: {self.text_render_mode}，已回退为 text"
            )
            self.text_render_mode = "text"

        self.screenshot_theme = str(
            self._cfg(
                "message_format",
                "twitter_screenshot_theme",
                "dark",
            )
            or "dark"
        ).strip().lower()
        if self.screenshot_theme not in ("dark", "light"):
            logger.warning(
                f"未知截图主题: {self.screenshot_theme}，已回退为 dark"
            )
            self.screenshot_theme = "dark"

        self.video_max_size_mb = max(
            1,
            int(
                self._cfg(
                    "message_format",
                    "twitter_video_max_size_mb",
                    256,
                )
            ),
        )
        self.collective_max_authors = max(
            1,
            int(
                self._cfg(
                    "message_format",
                    "twitter_collective_max_authors",
                    5,
                )
            ),
        )
        self.translate_enabled = bool(
            self._cfg(
                "translation",
                "twitter_translate_enabled",
                False,
            )
        )
        self.translate_target_lang = str(
            self._cfg(
                "translation",
                "twitter_translate_target_lang",
                "简体中文",
            )
            or "简体中文"
        )
        self.translate_provider_id = str(
            self._cfg(
                "translation",
                "twitter_translate_provider_id",
                "",
            )
            or ""
        ).strip()
        self.translate_timeout_seconds = max(
            1,
            int(self._cfg("translation", "twitter_translate_timeout_seconds", 60)),
        )
        self.translate_custom_prompt_enabled = bool(
            self._cfg(
                "translation",
                "twitter_translate_custom_prompt_enabled",
                False,
            )
        )
        self.translate_custom_prompt = str(
            self._cfg(
                "translation",
                "twitter_translate_custom_prompt",
                "",
            )
            or ""
        ).strip()
        self.custom_nitter_url = str(
            self._cfg("basic", "twitter_nitter_url", "") or ""
        ).strip()
        self.image_quality = str(
            self._cfg(
                "message_format",
                "twitter_image_quality",
                "orig",
            )
            or "orig"
        ).strip()
        self.pre_download_media = bool(
            self._cfg("basic", "twitter_pre_download_media", False)
        )

        self.website_list: list[str] = []
        if self.data_provider == DATA_PROVIDER_NITTER:
            if self.custom_nitter_url:
                self.website_list.append(self.custom_nitter_url)
            self.website_list.extend(WEBSITE_LIST)

        self.twitter_api = TwitterAPI(
            proxy=self.proxy,
            nitter_url="",
            image_quality=self.image_quality,
            provider=self.data_provider,
            fxtwitter_api_base=self.fxtwitter_api_base,
        )
        self._provider_ready = False

        self._poll_task: asyncio.Task | None = None
        self._running = False
        self._poll_wakeup = asyncio.Event()
        self._config_lock = asyncio.Lock()

        self.subscription_service = SubscriptionService(
            self._get_kv_data,
            self._put_kv_data,
            self.twitter_api,
            lambda: self._provider_ready,
        )
        self.message_service = TweetMessageService(
            context,
            self.twitter_api,
            self._render_tweet_html,
            TweetMessageSettings(
                no_text=self.no_text,
                send_media_separately=self.send_media_separately,
                include_tweet_link=self.include_tweet_link,
                text_render_mode=self.text_render_mode,
                screenshot_theme=self.screenshot_theme,
                video_max_size_mb=self.video_max_size_mb,
                translate_enabled=self.translate_enabled,
                translate_target_lang=self.translate_target_lang,
                translate_provider_id=self.translate_provider_id,
                translate_timeout_seconds=self.translate_timeout_seconds,
                translate_custom_prompt_enabled=(
                    self.translate_custom_prompt_enabled
                ),
                translate_custom_prompt=self.translate_custom_prompt,
                pre_download_media=self.pre_download_media,
                proxy=self.proxy,
            ),
            avatar_cache=AvatarCacheService(
                self.twitter_api,
                lambda: StarTools.get_data_dir("astrbot_plugin_my_twitter"),
            ),
        )
        self.delivery_service = TweetDeliveryService(
            context,
            self.subscription_service,
            self.message_service,
            TweetDeliverySettings(
                use_node=self.use_node,
                collective_forward=self.collective_forward,
                collective_max_authors=self.collective_max_authors,
                deduplicate_retweets=self.deduplicate_retweets,
                max_tweets_per_session=max(1, int(self._cfg("delivery_guard", "max_tweets_per_session", 3))),
            ),
        )
        self.polling_service = PollingService(
            self.twitter_api,
            self.subscription_service,
            self.delivery_service,
            PollingSettings(
                include_retweets=self.include_retweets,
                data_provider=self.data_provider,
                custom_nitter_url=self.custom_nitter_url,
                website_list=tuple(self.website_list),
                max_tweets_per_user=self.poll_max_tweets_per_user,
            ),
        )

        self._webui_controller = None
        register_web_api = getattr(context, "register_web_api", None)
        if MyTwitterWebUIController is not None and callable(register_web_api):
            try:
                self._webui_controller = MyTwitterWebUIController(self, context)
            except Exception as exc:
                logger.warning(f"我的推特订阅管理 WebUI 注册失败: {exc}")
        else:
            logger.info("当前 AstrBot 版本不支持 Plugin Pages，跳过订阅管理 WebUI")

    async def _get_kv_data(self, key: str, default: Any) -> Any:
        """延迟调用 AstrBot KV 接口，便于服务独立测试。"""
        return await self.get_kv_data(key, default)

    async def _put_kv_data(self, key: str, value: Any) -> None:
        """延迟调用 AstrBot KV 写入接口。"""
        await self.put_kv_data(key, value)

    async def _render_tweet_html(self, *args, **kwargs):
        """延迟调用 AstrBot HTML 渲染接口。"""
        return await self.html_render(*args, **kwargs)

    async def _refresh_fxtwitter_availability(self) -> bool:
        """刷新 FxTwitter 健康状态，避免检查异常终止轮询任务。"""
        try:
            ready = await self.twitter_api.check_fxtwitter_available()
        except Exception as exc:
            logger.warning(f"FxTwitter API 健康检查异常: {exc}")
            ready = False
        self._provider_ready = bool(ready)
        return self._provider_ready

    async def _refresh_nitter_availability(self) -> bool:
        try:
            available = await self.twitter_api.check_website_available(self.website_list)
        except Exception as exc:
            logger.warning(f"Nitter 健康检查异常: {exc}")
            available = None
        self._provider_ready = bool(available)
        return self._provider_ready

    async def initialize(self):
        """初始化数据源并启动轮询任务。"""
        logger.info("我的推特插件初始化中...")

        if self._config_migration_error:
            self._migration_report = {"state": "failed", "error": "旧配置导入失败，请查看日志"}
            return
        try:
            self._migration_report = await MigrationService(
                self._get_kv_data, self._put_kv_data, self._read_legacy_kv,
                self._read_previous_kv,
            ).run(import_legacy=self._cfg("migration", "import_legacy", True))
            logger.info(f"我的推特数据继承: {self._migration_report}")
        except Exception as exc:
            self._migration_report = {"state": "failed", "error": "旧数据迁移失败，请查看日志"}
            logger.error(f"我的推特迁移失败，为保护数据暂停自动推送: {exc}")
            return

        if self.collective_forward and not self.use_node:
            logger.warning(
                "集体转发模式已开启但合并转发消息未开启，集体转发功能不会生效。"
                "请同时开启「使用合并转发消息」配置项。"
            )

        if self.data_provider == DATA_PROVIDER_FXTWITTER:
            logger.info("当前使用 Twitter 数据源: FxTwitter API")
            await self._refresh_fxtwitter_availability()
            if not self._provider_ready:
                logger.warning("FxTwitter API 健康检查失败，推文轮询功能暂不可用")
        else:
            logger.info("当前使用 Twitter 数据源: Nitter")
            available = await self._refresh_nitter_availability()
            if available:
                logger.info(f"当前使用 Nitter 镜像站: {self.twitter_api.nitter_url}")
            else:
                logger.warning("未找到可用 Nitter 镜像站，推文轮询功能暂不可用")

        should_start_polling = True
        if should_start_polling:
            self._running = True
            self._poll_task = asyncio.create_task(self._poll_tweets())
            if self._provider_ready:
                logger.info(
                    f"推文轮询已启动，间隔 {self.poll_interval} 分钟"
                )
            else:
                logger.warning(
                    "数据源暂不可用，已启动后台恢复检查，"
                    f"间隔 {self.poll_interval} 分钟"
                )

        logger.info("我的推特插件初始化完成")

    async def terminate(self):
        """停用立即停止发送，并丢弃缓存，避免停用后继续推送。"""
        self._running = False
        self._poll_wakeup.set()
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

        if (
            self.delivery_service.has_collected
            or self.polling_service.has_pending_collective
        ):
            self.delivery_service.clear_collected()
        if self.message_service.avatar_cache is not None:
            await self.message_service.avatar_cache.close()
        await self.twitter_api.close()
        logger.info("我的推特插件已停止")

    async def _read_legacy_kv(self, key: str, default: Any) -> Any:
        from astrbot.core import sp
        return await sp.get_async("plugin", LEGACY_PLUGIN_ID, key, default)

    async def _read_previous_kv(self, key: str, default: Any) -> Any:
        from astrbot.core import sp
        return await sp.get_async("plugin", PREVIOUS_PLUGIN_ID, key, default)

    def _legacy_plugin_active(self) -> bool:
        """原插件仍在运行时新插件待命，避免两份订阅重复推送。"""
        lookup = getattr(getattr(self, "context", None), "get_registered_star", None)
        if not callable(lookup):
            return False
        for name in ("astrbot_plugin_twitter", "astrbot_plugin_x_sentinel"):
            old = lookup(name)
            if old is not None and getattr(old, "activated", False):
                return True
        return False

    @filter.command("推特状态", alias={"twitter_status"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def twitter_status(self, event: AstrMessageEvent):
        """查看数据继承、数据源和后台轮询状态。"""
        report = await self.get_kv_data(MIGRATION_KEY, self._migration_report)
        subs = await self._get_subs()
        states = {"completed": "已完成", "not_found": "未发现旧数据", "pending": "待执行", "disabled": "未启用", "failed": "失败（查看日志）"}
        yield event.plain_result(
            f"我的推特 v1.0.2\n数据继承：{states.get(report.get('state'), report.get('state'))}\n"
            f"原订阅：{report.get('authors', 0)} 个推主 / {report.get('relations', 0)} 条关系\n"
            f"当前订阅：{len(subs)} 个推主\n数据源：{self.data_provider} / {'可用' if self._provider_ready else '待恢复'}\n"
            f"后台轮询：{'待命（原插件仍启用）' if self._legacy_plugin_active() else ('运行中' if self._running else '已暂停')}\n"
            "恢复策略：第一轮只同步，积压不补发；停用原插件后可避免重复推送。"
        )

    # WebUI 与现有调用方使用的兼容委托。
    async def _get_subs(self) -> dict:
        return await self.subscription_service.get_all()

    async def _save_subs(self, data: dict) -> None:
        await self.subscription_service.save_all(data)

    async def _get_subscriptions_snapshot(self) -> dict:
        return await self.subscription_service.get_snapshot()

    @staticmethod
    def _find_subscription_key(subs: dict, username: str) -> str | None:
        return SubscriptionService.find_key(subs, username)

    async def _add_subscription(
        self,
        umo: str,
        username: str,
        *,
        r18: bool = False,
        media_only: bool = False,
        reject_duplicate: bool = False,
    ) -> dict:
        return await self.subscription_service.add(
            umo,
            username,
            r18=r18,
            media_only=media_only,
            reject_duplicate=reject_duplicate,
        )

    async def _update_subscription(
        self,
        umo: str,
        username: str,
        changes: dict,
    ) -> dict:
        return await self.subscription_service.update(umo, username, changes)

    async def _remove_subscription(self, umo: str, username: str) -> dict:
        return await self.subscription_service.remove(umo, username)

    async def _set_session_subscriptions_status(
        self,
        umo: str,
        enabled: bool,
    ) -> int:
        return await self.subscription_service.set_session_status(umo, enabled)

    async def _clear_subscriptions(self) -> dict:
        return await self.subscription_service.clear()

    async def _update_subscription_cursor(
        self,
        username: str,
        since_id: str,
    ) -> bool:
        return await self.subscription_service.update_cursor(username, since_id)

    @staticmethod
    def _attach_timeline_item_metadata(tweet_info: dict, item: dict) -> None:
        PollingService.attach_timeline_item_metadata(tweet_info, item)

    async def _check_all_subscriptions(self) -> None:
        await self.polling_service.check_all()

    async def _check_user_tweets(self, username: str, info: dict) -> bool:
        return await self.polling_service.check_user(username, info)

    def _provider_unavailable_message(self) -> str:
        if self.data_provider == DATA_PROVIDER_FXTWITTER:
            return "FxTwitter API 不可用，请检查配置或网络"
        return "Nitter 镜像站不可用，请检查配置或网络"

    async def _set_poll_interval(self, minutes: int) -> None:
        """持久化全局轮询间隔，并重置下一次轮询倒计时。"""
        config_lock = getattr(self, "_config_lock", None)
        if config_lock is None:
            config_lock = asyncio.Lock()
            self._config_lock = config_lock

        async with config_lock:
            had_basic = "basic" in self.config
            previous_basic = copy.deepcopy(self.config.get("basic"))
            basic = self.config.get("basic")
            if not isinstance(basic, dict):
                basic = {}
                self.config["basic"] = basic
            basic["twitter_poll_interval"] = minutes

            try:
                save_config = getattr(self.config, "save_config", None)
                if not callable(save_config):
                    raise RuntimeError("当前配置对象不支持持久化")
                save_result = save_config()
                if asyncio.iscoroutine(save_result):
                    await save_result
            except Exception:
                if had_basic:
                    self.config["basic"] = previous_basic
                else:
                    self.config.pop("basic", None)
                raise

            self.poll_interval = minutes
            self._poll_wakeup.set()

    async def _wait_for_next_poll(self) -> None:
        """等待下一轮轮询；配置更新会重置完整倒计时。"""
        while self._running:
            self._poll_wakeup.clear()
            try:
                await asyncio.wait_for(
                    self._poll_wakeup.wait(),
                    timeout=self.poll_interval * 60,
                )
            except TimeoutError:
                return

    async def _poll_tweets(self) -> None:
        """按全局间隔执行轮询。"""
        wait_before_poll = (
            self.data_provider == DATA_PROVIDER_FXTWITTER
            and not self._provider_ready
        )
        while self._running:
            if wait_before_poll:
                await self._wait_for_next_poll()
                if not self._running:
                    break
            wait_before_poll = True

            if self._legacy_plugin_active():
                self.polling_service.require_resync()
                continue

            if self.data_provider == DATA_PROVIDER_NITTER and not self._provider_ready:
                self.polling_service.require_resync()
                if not await self._refresh_nitter_availability():
                    continue

            if (
                self.data_provider == DATA_PROVIDER_FXTWITTER
                and not self._provider_ready
            ):
                self.polling_service.require_resync()
                if not await self._refresh_fxtwitter_availability():
                    continue
                logger.info("FxTwitter API 已恢复，立即执行订阅轮询")

            try:
                await self._check_all_subscriptions()
            except Exception as exc:
                logger.error(f"推文轮询出错: {exc}")
            finally:
                if self.data_provider == DATA_PROVIDER_FXTWITTER:
                    self._provider_ready = bool(self.twitter_api.is_ready)

    @filter.command("推特关注", alias={"twitter_follow"})
    async def follow_twitter(
        self,
        event: AstrMessageEvent,
        username: str = "",
    ):
        """订阅推主，格式: /推特关注 <推主id> [r18] [媒体]。"""
        if self._legacy_plugin_active():
            return
        if not self._provider_ready:
            yield event.plain_result(self._provider_unavailable_message())
            return

        if not username:
            yield event.plain_result(
                "请提供推主ID，用法: /推特关注 <推主ID> [r18] [媒体]"
            )
            return

        username = username.strip("@").strip()
        extra_args = event.message_str.strip().split()[2:]
        r18 = "r18" in extra_args
        media_only = "媒体" in extra_args

        try:
            add_result = await self._add_subscription(
                event.unified_msg_origin,
                username,
                r18=r18,
                media_only=media_only,
            )
        except FxTwitterTimelineError as exc:
            logger.warning(f"订阅 @{username} 时获取时间线失败: {exc}")
            yield event.plain_result(f"获取 @{username} 时间线失败，请稍后重试")
            return
        except Exception as exc:
            logger.warning(f"订阅 @{username} 失败: {exc}")
            yield event.plain_result(f"订阅 @{username} 失败，请稍后重试")
            return

        reason = add_result.get("reason")
        if reason == "invalid_username":
            yield event.plain_result("推主ID须为 1–15 位英文字母、数字或下划线；请填写 @ 后的用户名。")
            return
        if reason == "not_found":
            yield event.plain_result(f"未找到用户: {username}")
            return
        if reason == "provider_unavailable":
            yield event.plain_result(self._provider_unavailable_message())
            return

        username = add_result["username"]
        screen_name = add_result["screen_name"]
        r18_str = " | R18" if r18 else ""
        media_str = " | 仅媒体" if media_only else ""
        raw_bio = add_result.get("bio", "")
        bio = raw_bio[:100] + ("..." if len(raw_bio) > 100 else "")
        yield event.plain_result(
            f"订阅成功!\n"
            f"ID: {username}\n"
            f"昵称: {screen_name}\n"
            f"简介: {bio}\n"
            f"选项: {r18_str}{media_str}"
        )

    @filter.command("推特批量关注", alias={"twitter_batch_follow"})
    async def batch_follow_twitter(self, event: AstrMessageEvent):
        """批量订阅推主。"""
        if self._legacy_plugin_active():
            return
        if not self._provider_ready:
            yield event.plain_result(self._provider_unavailable_message())
            return

        tokens = event.message_str.strip().split()[1:]
        if not tokens:
            yield event.plain_result(
                "请提供推主ID，用法: /推特批量关注 "
                "<推主ID1> <推主ID2> ... [r18] [媒体]"
            )
            return

        r18 = "r18" in tokens
        media_only = "媒体" in tokens
        usernames = [
            token.strip("@").strip()
            for token in tokens
            if token not in ("r18", "媒体")
        ]
        if not usernames:
            yield event.plain_result("请提供至少一个推主ID")
            return

        yield event.plain_result(
            f"正在批量订阅 {len(usernames)} 个推主，请稍候..."
        )

        umo = event.unified_msg_origin
        results: list[str] = []
        success_count = 0
        for username in usernames:
            try:
                add_result = await self._add_subscription(
                    umo,
                    username,
                    r18=r18,
                    media_only=media_only,
                )
                if add_result.get("reason") == "invalid_username":
                    results.append(f"❌ @{username} - 用户名格式不正确")
                    continue
                if add_result.get("reason") == "not_found":
                    results.append(f"❌ @{username} - 未找到用户")
                    continue
                if add_result.get("reason") == "provider_unavailable":
                    results.append(f"❌ @{username} - 数据源不可用")
                    continue

                success_count += 1
                r18_str = " | R18" if r18 else ""
                media_str = " | 仅媒体" if media_only else ""
                results.append(
                    f"✅ @{add_result['username']} "
                    f"({add_result['screen_name']}){r18_str}{media_str}"
                )
            except FxTwitterTimelineError as exc:
                logger.warning(
                    f"批量订阅 @{username} 时获取时间线失败: {exc}"
                )
                results.append(f"❌ @{username} - 获取时间线失败")
            except Exception as exc:
                results.append(f"❌ @{username} - 订阅失败: {exc}")

        yield event.plain_result(
            f"批量订阅完成: 成功 {success_count}/{len(usernames)}\n"
            + "\n".join(results)
        )

    @filter.command("推特取关", alias={"twitter_unfollow"})
    async def unfollow_twitter(
        self,
        event: AstrMessageEvent,
        username: str = "",
    ):
        """取关推主。"""
        if self._legacy_plugin_active():
            return
        if not username:
            yield event.plain_result("请提供推主ID，用法: /推特取关 <推主ID>")
            return

        username = username.strip("@").strip()
        remove_result = await self._remove_subscription(
            event.unified_msg_origin,
            username,
        )
        if not remove_result["ok"]:
            yield event.plain_result(f"当前会话未订阅 {username}")
            return
        yield event.plain_result(f"已取关 {remove_result['username']}")

    @filter.command("推特批量取关", alias={"twitter_batch_unfollow"})
    async def batch_unfollow_twitter(self, event: AstrMessageEvent):
        """批量取关推主。"""
        if self._legacy_plugin_active():
            return
        tokens = event.message_str.strip().split()[1:]
        if not tokens:
            yield event.plain_result(
                "请提供推主ID，用法: /推特批量取关 "
                "<推主ID1> <推主ID2> ..."
            )
            return

        usernames = [token.strip("@").strip() for token in tokens]
        umo = event.unified_msg_origin
        results: list[str] = []
        success_count = 0
        for username in usernames:
            remove_result = await self._remove_subscription(umo, username)
            if not remove_result["ok"]:
                results.append(f"❌ @{username} - 当前会话未订阅")
                continue
            success_count += 1
            results.append(f"✅ @{remove_result['username']} - 已取关")

        yield event.plain_result(
            f"批量取关完成: 成功 {success_count}/{len(usernames)}\n"
            + "\n".join(results)
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("推特清空订阅", alias={"twitter_clear_all"})
    async def clear_all_subscriptions(self, event: AstrMessageEvent, confirmation: str = ""):
        """清空所有推文订阅。"""
        if self._legacy_plugin_active():
            return
        if confirmation != "确认":
            yield event.plain_result("此操作会清空所有会话的订阅。如需继续，请发送 /推特清空订阅 确认")
            return
        cleared = await self._clear_subscriptions()
        if not cleared["authors"]:
            yield event.plain_result("当前没有任何订阅")
            return

        self.delivery_service.clear_collected()
        yield event.plain_result(
            f"已清空所有订阅: 共 {cleared['authors']} 个推主, "
            f"{cleared['relations']} 个订阅关系"
        )

    @staticmethod
    def _subscription_list_chunks(lines: list[str]) -> list[str]:
        """按完整条目分段，限制每个转发节点的条数与字符数。"""
        total = len(lines)
        header = "当前订阅列表（共 {total} 个，第 {index}/{chunks} 段）:\n"
        # 段数不会超过条目数，按最大可能段号为标题预留字符预算。
        budget = 1000 - len(header.format(total=total, index=total, chunks=total))
        chunks = []
        current = []
        size = 0
        for index, line in enumerate(lines, 1):
            row = f"{index}. {line}"
            if current and (len(current) >= 50 or size + 1 + len(row) > budget):
                chunks.append("\n".join(current))
                current = []
                size = 0
            size += len(row) + bool(current)
            current.append(row)
        if current:
            chunks.append("\n".join(current))
        return [
            header.format(total=total, index=index, chunks=len(chunks)) + body
            for index, body in enumerate(chunks, 1)
        ]

    @filter.command("推特列表", alias={"twitter_list"})
    async def list_follows(self, event: AstrMessageEvent):
        """以分段合并转发查看当前会话的完整订阅列表。"""
        if self._legacy_plugin_active():
            return
        umo = event.unified_msg_origin
        subs = await self._get_subs()
        lines = []
        for username, info in subs.items():
            subscribers = info.get("subscribers", {})
            if umo not in subscribers:
                continue
            sub_config = subscribers[umo]
            status_icon = "🟢" if sub_config.get("status", True) else "🔴"
            r18_str = " | R18" if sub_config.get("r18") else ""
            media_str = " | 仅媒体" if sub_config.get("media") else ""
            screen_name = " ".join(str(info.get("screen_name") or username).split())
            if len(screen_name) > 50:
                screen_name = screen_name[:49] + "…"
            lines.append(
                f"{status_icon} @{username} ({screen_name})"
                f"{r18_str}{media_str}"
            )

        if not lines:
            yield event.plain_result("当前没有订阅任何推主")
            return

        nodes = [
            Node(content=[Plain(text)], name="推特订阅列表", uin=event.get_self_id())
            for text in self._subscription_list_chunks(lines)
        ]
        yield event.chain_result([Nodes(nodes)])

    @filter.command("推特推送", alias={"twitter_push"})
    async def toggle_push(
        self,
        event: AstrMessageEvent,
        action: str = "",
    ):
        """开启或关闭当前会话的全部推文推送。"""
        if self._legacy_plugin_active():
            return
        if action not in ("开启", "关闭"):
            yield event.plain_result("用法: /推特推送 开启 或 /推特推送 关闭")
            return

        enabled = action == "开启"
        count = await self._set_session_subscriptions_status(
            event.unified_msg_origin,
            enabled,
        )
        if count > 0:
            status_text = "开启" if enabled else "关闭"
            yield event.plain_result(
                f"推文推送已{status_text} (影响 {count} 个订阅)"
            )
        else:
            yield event.plain_result("当前没有订阅任何推主")

    @filter.command("推特测试", alias={"twitter_test"})
    async def test_tweet(
        self,
        event: AstrMessageEvent,
        username: str = "",
    ):
        """立即获取并推送指定推主的最新一条推文。"""
        if self._legacy_plugin_active():
            return
        if not self._provider_ready:
            yield event.plain_result(self._provider_unavailable_message())
            return
        if not username:
            yield event.plain_result(
                "请提供推主ID，用法: /推特测试 <推主ID>"
            )
            return

        username = username.strip("@").strip()
        umo = event.unified_msg_origin
        yield event.plain_result(
            f"正在获取 @{username} 的最新推文，请稍候..."
        )

        try:
            timeline_items = await self.twitter_api.get_user_timeline_items(
                username
            )
        except FxTwitterTimelineError as exc:
            logger.warning(f"测试 @{username} 时获取时间线失败: {exc}")
            yield event.plain_result(
                f"获取 @{username} 时间线失败，请稍后重试"
            )
            return
        if not timeline_items:
            yield event.plain_result(f"未找到 @{username} 的推文")
            return

        selected_item = next(
            (
                item
                for item in timeline_items
                if self.include_retweets or not item.get("is_retweet")
            ),
            None,
        )
        if not selected_item:
            yield event.plain_result(f"未找到 @{username} 的非转贴推文")
            return

        tweet_id = str(selected_item.get("tweet_id") or "")
        tweet_username = str(selected_item.get("username") or username)
        tweet_info = await self.twitter_api.get_tweet(
            tweet_username,
            tweet_id,
        )
        if not tweet_info.get("status", True):
            yield event.plain_result(
                f"无法获取 @{username} 的推文，帖子可能已删除、受限或暂时不可用"
            )
            return
        self._attach_timeline_item_metadata(tweet_info, selected_item)

        translated_text, translate_model = (
            await self.message_service.maybe_translate(tweet_info, umo)
        )
        chain = await self.message_service.build_message_chain(
            username,
            tweet_info,
            translated_text=translated_text,
            translate_model=translate_model,
        )
        if not chain:
            yield event.plain_result(f"未找到 @{username} 的推文内容")
            return

        author_username = str(tweet_info.get("username") or username)
        screen_name = str(
            tweet_info.get("screen_name") or author_username
        )
        nickname = self.message_service.build_author_display(
            author_username,
            screen_name,
        )
        prepared = self.delivery_service.prepare_event_delivery(
            chain,
            nickname,
        )
        if prepared.primary_chain:
            yield event.chain_result(prepared.primary_chain)
        else:
            yield event.plain_result(f"未找到 @{username} 的推文内容")
        await self.delivery_service.send_prepared_videos(
            umo,
            prepared.videos,
        )

    async def _handle_tweet_link(
        self,
        event: AstrMessageEvent,
        match,
        *,
        report_errors: bool,
    ):
        """获取并发送链接对应的推文，供指令和自动识别共用。"""
        umo = event.unified_msg_origin
        link = match.group(1)
        username = match.group(2)
        tweet_id = match.group(3)
        logger.info(f"检测到推文链接: {link}")

        try:
            tweet_info = await self.twitter_api.get_tweet(username, tweet_id)
            if not tweet_info.get("status", True):
                yield event.plain_result(
                    "无法获取该推文，帖子可能已删除、受限或暂时不可用"
                )
                return

            translated_text, translate_model = (
                await self.message_service.maybe_translate(tweet_info, umo)
            )
            chain = await self.message_service.build_message_chain(
                username,
                tweet_info,
                {"r18": True, "media": False, "status": True},
                translated_text=translated_text,
                translate_model=translate_model,
            )
            if not chain:
                if report_errors:
                    yield event.plain_result("未找到可发送的推文内容")
                return

            author_username = str(tweet_info.get("username") or username)
            screen_name = str(
                tweet_info.get("screen_name") or author_username
            )
            nickname = self.message_service.build_author_display(
                author_username,
                screen_name,
            )
            prepared = self.delivery_service.prepare_event_delivery(
                chain,
                nickname,
            )
            if prepared.primary_chain:
                yield event.chain_result(prepared.primary_chain)
            elif report_errors and not prepared.videos:
                yield event.plain_result("未找到可发送的推文内容")
            await self.delivery_service.send_prepared_videos(
                umo,
                prepared.videos,
            )
        except Exception as exc:
            logger.error(f"解析推文链接失败: {exc}")
            if report_errors:
                yield event.plain_result("解析推文链接失败，请稍后重试")

    @filter.command("推特解析", alias={"twitter_parse"})
    async def parse_tweet_link(self, event: AstrMessageEvent):
        """解析指定的 Twitter/X 推文链接。"""
        if self._legacy_plugin_active():
            return
        event.stop_event()
        if self.link_recognition_mode == LINK_RECOGNITION_MODE_OFF:
            yield event.plain_result("推文链接解析已关闭")
            return

        match = TWITTER_LINK_PATTERN.search(event.message_str or "")
        if not match:
            yield event.plain_result(
                "请提供推文链接，用法: /推特解析 <Twitter/X 推文链接>"
            )
            return
        if not self._provider_ready:
            yield event.plain_result(self._provider_unavailable_message())
            return

        async for result in self._handle_tweet_link(
            event,
            match,
            report_errors=True,
        ):
            yield result

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """检测 Twitter/X 链接并解析推文。"""
        if self._legacy_plugin_active():
            return
        if self.link_recognition_mode != LINK_RECOGNITION_MODE_AUTO:
            return

        message = event.message_str or ""
        if TWITTER_PARSE_COMMAND_PATTERN.match(message):
            return

        match = TWITTER_LINK_PATTERN.search(message)
        if not match:
            return

        if not self._provider_ready:
            return

        async for result in self._handle_tweet_link(
            event,
            match,
            report_errors=False,
        ):
            yield result
