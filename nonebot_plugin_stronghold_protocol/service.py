"""把配置、Bridge 客户端、绑定表拼成一个服务对象：命令与日报都只跟它打交道。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

from .bridge import BridgeClient, BridgeError
from .config import Config
from .models import BindChallenge, BindResult, DailyReport, PlayerProfile, ServerStatus
from .period import period_report
from .storage import Binding, BindingStore, ReportGroupStore, default_data_dir


def resolve_tz(name: str) -> tzinfo:
    """把配置里的时区名解析成 tzinfo；解析不了就退回系统本地时区。"""
    try:
        return ZoneInfo(name) if name else datetime.now().astimezone().tzinfo or ZoneInfo("UTC")
    except Exception:
        return datetime.now().astimezone().tzinfo or ZoneInfo("UTC")


class StrongholdService:
    """一进程一个实例，由插件启动时创建。"""

    def __init__(self, config: Config, store: Optional[BindingStore] = None,
                 report_store: Optional[ReportGroupStore] = None):
        self.config = config
        self.client = BridgeClient(
            base_url=config.stronghold_api_base,
            token=config.stronghold_api_token,
            prefix=config.stronghold_api_prefix,
            timeout=config.stronghold_timeout,
            verify_tls=config.stronghold_verify_tls,
            use_env_proxy=config.stronghold_use_env_proxy,
        )
        data_dir = default_data_dir(config.stronghold_data_dir)
        self.store = store or BindingStore(data_dir / "bindings.json")
        # 订阅表跟绑定表放同一个目录：测试传自定义绑定表时，订阅表也自动跟着走。
        base_dir = getattr(self.store, "path", None)
        self.report_store = report_store or ReportGroupStore(
            (Path(base_dir).parent if base_dir else data_dir) / "report_groups.json"
        )
        self.tz = resolve_tz(config.stronghold_report_tz)

    # ---- 在线 ------------------------------------------------------------------------------------

    async def status(self) -> ServerStatus:
        return await self.client.status()

    # ---- 战绩 ------------------------------------------------------------------------------------

    async def profile(self, *, player_id: Optional[str] = None, name: Optional[str] = None) -> PlayerProfile:
        return await self.client.player(player_id=player_id, name=name)

    async def profile_of(self, qq: object) -> PlayerProfile:
        """按绑定查战绩；未绑定时抛 ``BridgeError('bind_required')``。"""
        binding = self.store.get(qq)
        if not binding:
            raise BridgeError("bind_required", "还没绑定游戏账号")
        return await self.profile(player_id=binding.player_id)

    # ---- 日报 ------------------------------------------------------------------------------------

    async def daily(self, day: Optional[date] = None) -> DailyReport:
        return await self.client.daily(day)

    async def period_daily(self, days: int, end: Optional[date] = None) -> DailyReport:
        """区间战报：把最近 ``days`` 天（含今天）的日报聚合在一起。"""
        return await period_report(self.daily, days, end or self.report_date())

    # ---- 群日报订阅（「日报 开启 / 日报 关闭」）--------------------------------------------------

    def report_groups(self) -> List[int]:
        """这次实际要推送的群：配置默认值 + 群里显式开启的 − 显式关闭的。"""
        return self.report_store.effective(self.config.stronghold_report_groups)

    def report_group_enabled(self, group_id: object) -> bool:
        return self.report_store.enabled(group_id, self.config.stronghold_report_groups)

    def set_report_group(self, group_id: object, enabled: bool, by: object = "") -> bool:
        """记下某个群的开关；返回「和之前相比是否变了」。"""
        return self.report_store.set_enabled(group_id, enabled, by)

    def report_date(self, day: Optional[date] = None) -> date:
        """服务端「今天」的本地日期（日报的日界 = 配置时区的当日）。"""
        return day or datetime.now(self.tz).date()

    @staticmethod
    def parse_date(text: str, today: date) -> Optional[date]:
        """``/日报`` 的日期参数：``2026-10-15`` / ``昨天`` / 空。"""
        s = (text or "").strip()
        if not s:
            return None
        if s in ("昨天", "yesterday"):
            return today - timedelta(days=1)
        if s in ("今天", "today"):
            return today
        try:
            return date.fromisoformat(s)
        except ValueError:
            try:
                return datetime.strptime(s, "%m-%d").date().replace(year=today.year)
            except ValueError:
                return None

    # ---- 绑定 ------------------------------------------------------------------------------------

    async def start_bind(self, qq: object, email: str) -> BindChallenge:
        """发码：让服务端把验证码发到这个游戏账号的邮箱。"""
        challenge = await self.client.bind_request(email, qq=str(qq))
        ttl_ms = max(60_000, int(self.config.stronghold_bind_ttl_min) * 60_000)
        if challenge.expires_at:
            ttl_ms = max(60_000, challenge.expires_at - int(datetime.now().timestamp() * 1000))
        self.store.put_pending(qq, email, challenge.request_id, ttl_ms)
        return challenge

    async def finish_bind(self, qq: object, code: str) -> BindResult:
        """回码：校验通过后把 QQ ↔ playerId 记在本地。

        打错一次码**不销毁**本地的待验证请求（服务端自己限错误次数，玩家可以接着试）；
        只有服务端明确作废了这次请求（``code_expired`` / ``request_not_found``）才丢掉，要求重新发码。
        """
        pending = self.store.peek_pending(qq)
        if not pending:
            raise BridgeError("request_not_found", "没有待验证的绑定请求")
        try:
            result = await self.client.bind_verify(pending.request_id, code, qq=str(qq))
        except BridgeError as e:
            if e.code in ("code_expired", "request_not_found"):
                self.store.pop_pending(qq)
            raise
        self.store.pop_pending(qq)
        if not result.player_id:
            raise BridgeError("bad_response", "服务端没有返回 playerId")
        await self.store.bind(qq, result.player_id, result.name, result.email or pending.email)
        return result

    async def unbind(self, qq: object) -> Optional[Binding]:
        return await self.store.unbind(qq)

    def binding(self, qq: object) -> Optional[Binding]:
        return self.store.get(qq)
