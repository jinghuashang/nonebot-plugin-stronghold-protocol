"""Bridge 契约的数据模型。

所有模型都由 ``xxx.from_json`` 从服务端 JSON 构造，解析一律「宽容」：缺字段给默认值，
类型不对当缺字段，绝不因为服务端多给/少给一个键就抛异常 —— 服务端可以先行实现一部分能力。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ---- 解析助手 -----------------------------------------------------------------------------------


def as_dict(v: Any) -> Dict[str, Any]:
    return v if isinstance(v, dict) else {}


def as_list(v: Any) -> List[Any]:
    return v if isinstance(v, list) else []


def as_str(v: Any, default: str = "") -> str:
    return v if isinstance(v, str) else default


def as_opt_str(v: Any) -> Optional[str]:
    return v if isinstance(v, str) and v else None


def as_int(v: Any, default: int = 0) -> int:
    if isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        return int(v)
    return default


def as_opt_int(v: Any) -> Optional[int]:
    if isinstance(v, bool):
        return None
    return int(v) if isinstance(v, (int, float)) else None


def as_float(v: Any, default: float = 0.0) -> float:
    if isinstance(v, bool):
        return default
    return float(v) if isinstance(v, (int, float)) else default


def as_bool(v: Any, default: bool = False) -> bool:
    return v if isinstance(v, bool) else default


# ---- 称号 ---------------------------------------------------------------------------------------

#: 日报固定统计的五个称号：board id → (中文名, 说明)。前四个是游戏本身的结算称号
#: （``data/config.json`` 的 ``titles[]``），``worst`` 是「最菜的」漏怪口径（见 docs/BRIDGE-API.md §3.4）。
TITLE_INFO: Dict[str, tuple] = {
    "comment_1": ("卫戍之星", "对敌方领袖造成伤害最高（仅胜利时）"),
    "comment_3": ("坚若磐石", "目标生命值损失最少"),
    "comment_6": ("挥金如土", "消耗资金最多"),
    "comment_4": ("精英云集", "晋升精锐次数最多"),
    "worst": ("卫戍之区", "漏怪最多"),
}

#: 日报按这个顺序列称号。
TITLE_ORDER: List[str] = ["comment_1", "comment_3", "comment_6", "comment_4", "worst"]

#: board id → 中文名（服务端没给 name 时兜底；服务的 ``worst`` 从来没有官方名）。
TITLE_NAMES: Dict[str, str] = {k: v[0] for k, v in TITLE_INFO.items()}
TITLE_NOTES: Dict[str, str] = {k: v[1] for k, v in TITLE_INFO.items()}

#: board id → 强调色（全部取自游戏调色板：金 / 冰蓝 / 琥珀 / 薄荷 / 红），菜单里给称号配色用。
TITLE_ACCENTS: Dict[str, str] = {
    "comment_1": "gold",
    "comment_3": "ice",
    "comment_6": "amber",
    "comment_4": "mint",
    "worst": "red",
}

#: 游戏全部六个结算称号（`data/config.json` 的 `titles[]`）：其中四个是榜，`comment_2` / `comment_5`
#: 不进榜但会出现在玩家的称号次数里（服务端的 `/player` 会报），所以渲染战绩时也要有中文名。
OFFICIAL_TITLE_NAMES: Dict[str, str] = {
    "comment_1": "卫戍之星",
    "comment_2": "不朽盟约",
    "comment_3": "坚若磐石",
    "comment_4": "精英云集",
    "comment_5": "万事俱备",
    "comment_6": "挥金如土",
}

#: 称号 id → 展示名（榜 + 官方称号；`worst` 不是官方称号，是我们自己的漏怪口径）。
ALL_TITLE_NAMES: Dict[str, str] = {
    **OFFICIAL_TITLE_NAMES,
    "worst": TITLE_INFO["worst"][0],
}


@dataclass
class TitleHolder:
    """一个称号榜上的一行（``/api/bot/v1/daily`` 与 ``/player`` 共用）。"""

    player_id: str = ""
    name: str = ""
    #: 称号榜：被评上的局数；漏怪榜（worst）：累计漏怪
    count: int = 0
    #: 称号榜：这些局里胜利的局数
    victories: int = 0
    #: 称号榜：这些局里最远的回合
    best_rounds: int = 0
    #: 漏怪榜：累计漏怪 / 单局最多漏怪
    leaks: int = 0
    best: int = 0
    #: 漏怪榜：被计入的局数
    matches: int = 0
    last_at: int = 0

    @staticmethod
    def from_json(v: Any) -> "TitleHolder":
        d = as_dict(v)
        return TitleHolder(
            player_id=as_str(d.get("playerId")),
            name=as_str(d.get("name")),
            count=as_int(d.get("count")),
            victories=as_int(d.get("victories")),
            best_rounds=as_int(d.get("bestRounds")),
            leaks=as_int(d.get("leaks")),
            best=as_int(d.get("best")),
            matches=as_int(d.get("matches")),
            last_at=as_int(d.get("lastAt")),
        )


@dataclass
class TitleBoard:
    """一个称号在一天/一个区间里的结果。"""

    id: str = ""
    name: str = ""
    note: str = ""
    #: ``title`` = 称号榜；``leaks`` = 漏怪榜
    kind: str = "title"
    total: int = 0
    holders: List[TitleHolder] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        return self.name or TITLE_NAMES.get(self.id, self.id)

    @property
    def display_note(self) -> str:
        return self.note or TITLE_NOTES.get(self.id, "")

    @staticmethod
    def from_json(v: Any) -> "TitleBoard":
        d = as_dict(v)
        board_id = as_str(d.get("id"))
        holders = [TitleHolder.from_json(x) for x in as_list(d.get("holders"))]
        # `worst`（卫戍之区）本来就是漏怪口径：服务端没给 kind 时按 id 推，免得渲染按称号行去排版
        default_kind = "leaks" if board_id == "worst" else "title"
        return TitleBoard(
            id=board_id,
            name=as_str(d.get("name")),
            note=as_str(d.get("note")),
            kind=as_str(d.get("kind"), default_kind),
            total=as_int(d.get("total"), len(holders)),
            holders=holders,
        )


# ---- 在线 ---------------------------------------------------------------------------------------

#: 玩家状态 → 中文。
PLAYER_STATES: Dict[str, str] = {
    "match": "对局中",
    "waiting": "待开局",
    "hall": "大厅",
    "idle": "空闲",
}


@dataclass
class OnlinePlayer:
    player_id: str = ""
    name: str = ""
    #: match / waiting / hall / idle
    state: str = "idle"
    #: 已登录账号的 playerId（``a_…``）；游客为 null
    account: Optional[str] = None
    room_code: Optional[str] = None
    seat: Optional[int] = None
    is_host: bool = False
    spectating: bool = False
    mode: Optional[str] = None
    difficulty: Optional[str] = None
    joined_at: int = 0

    @property
    def state_label(self) -> str:
        return PLAYER_STATES.get(self.state, self.state or "空闲")

    @staticmethod
    def from_json(v: Any) -> "OnlinePlayer":
        d = as_dict(v)
        return OnlinePlayer(
            player_id=as_str(d.get("playerId")),
            name=as_str(d.get("name"), "?"),
            state=as_str(d.get("state"), "idle"),
            account=as_opt_str(d.get("account")),
            room_code=as_opt_str(d.get("roomCode")),
            seat=as_opt_int(d.get("seat")),
            is_host=as_bool(d.get("isHost")),
            spectating=as_bool(d.get("spectating")),
            mode=as_opt_str(d.get("mode")),
            difficulty=as_opt_str(d.get("difficulty")),
            joined_at=as_int(d.get("joinedAt")),
        )


@dataclass
class MatchPlayer:
    player_id: str = ""
    name: str = ""
    seat: int = 0
    is_bot: bool = False
    alive: bool = True
    lp: int = 0
    spectating: bool = False

    @staticmethod
    def from_json(v: Any) -> "MatchPlayer":
        d = as_dict(v)
        return MatchPlayer(
            player_id=as_str(d.get("playerId")),
            name=as_str(d.get("name"), "?"),
            seat=as_int(d.get("seat")),
            is_bot=as_bool(d.get("isBot")),
            alive=as_bool(d.get("alive"), True),
            lp=as_int(d.get("lp")),
            spectating=as_bool(d.get("spectating")),
        )


@dataclass
class OnlineMatch:
    room_code: str = ""
    mode: Optional[str] = None
    difficulty: Optional[str] = None
    round: int = 0
    boss_round: int = 0
    started_at: int = 0
    players: List[MatchPlayer] = field(default_factory=list)

    @staticmethod
    def from_json(v: Any) -> "OnlineMatch":
        d = as_dict(v)
        return OnlineMatch(
            room_code=as_str(d.get("roomCode")),
            mode=as_opt_str(d.get("mode")),
            difficulty=as_opt_str(d.get("difficulty")),
            round=as_int(d.get("round")),
            boss_round=as_int(d.get("bossRound")),
            started_at=as_int(d.get("startedAt")),
            players=[MatchPlayer.from_json(x) for x in as_list(d.get("players"))],
        )


@dataclass
class Counts:
    online: int = 0
    in_match: int = 0
    waiting: int = 0
    in_hall: int = 0
    spectators: int = 0
    rooms: int = 0
    matches: int = 0
    accounts: int = 0

    @staticmethod
    def from_json(v: Any) -> "Counts":
        d = as_dict(v)
        return Counts(
            online=as_int(d.get("online")),
            in_match=as_int(d.get("inMatch")),
            waiting=as_int(d.get("waiting")),
            in_hall=as_int(d.get("inHall")),
            spectators=as_int(d.get("spectators")),
            rooms=as_int(d.get("rooms")),
            matches=as_int(d.get("matches")),
            accounts=as_int(d.get("accounts")),
        )


@dataclass
class Maintenance:
    mode: str = "off"
    reason: Optional[str] = None
    until: Optional[int] = None

    @property
    def active(self) -> bool:
        return self.mode in ("notice", "maintenance")

    @property
    def label(self) -> str:
        return {"notice": "公告中", "maintenance": "维护中"}.get(self.mode, "正常")

    @staticmethod
    def from_json(v: Any) -> "Maintenance":
        d = as_dict(v)
        return Maintenance(
            mode=as_str(d.get("mode"), "off"),
            reason=as_opt_str(d.get("reason")),
            until=as_opt_int(d.get("until")),
        )


@dataclass
class ServerStatus:
    """``GET /api/bot/v1/status``。"""

    ok: bool = False
    protocol: int = 0
    app: str = ""
    server_now: int = 0
    counts: Counts = field(default_factory=Counts)
    players: List[OnlinePlayer] = field(default_factory=list)
    matches: List[OnlineMatch] = field(default_factory=list)
    maintenance: Maintenance = field(default_factory=Maintenance)

    @staticmethod
    def from_json(v: Any) -> "ServerStatus":
        d = as_dict(v)
        return ServerStatus(
            ok=as_bool(d.get("ok"), True),
            protocol=as_int(d.get("protocol")),
            app=as_str(d.get("app")),
            server_now=as_int(d.get("serverNow")),
            counts=Counts.from_json(d.get("counts")),
            players=[OnlinePlayer.from_json(x) for x in as_list(d.get("players"))],
            matches=[OnlineMatch.from_json(x) for x in as_list(d.get("matches"))],
            maintenance=Maintenance.from_json(d.get("maintenance")),
        )


# ---- 战绩 / 日报 --------------------------------------------------------------------------------

#: ``mode`` → 中文。
MODE_LABELS: Dict[str, str] = {"solo": "独立", "coop": "同盟"}

#: ``difficulty`` → 中文。
DIFFICULTY_LABELS: Dict[str, str] = {
    "TRAINING": "训练",
    "FUNNY": "标准",
    "NORMAL": "标准",
    "HARD": "险境",
    "ABYSS": "绝境",
}


@dataclass
class PlayerStats:
    """一个玩家在某个区间（今日或累计）里的成绩。"""

    matches: int = 0
    victories: int = 0
    win_rate: float = 0.0
    gold_spent: int = 0
    leaks: int = 0
    best_rounds: int = 0
    #: 称号 id → 获得次数
    titles: Dict[str, int] = field(default_factory=dict)
    first_at: int = 0
    last_at: int = 0

    @staticmethod
    def from_json(v: Any) -> "PlayerStats":
        d = as_dict(v)
        raw_titles = as_dict(d.get("titles"))
        titles = {k: as_int(x) for k, x in raw_titles.items() if isinstance(k, str) and as_int(x) > 0}
        return PlayerStats(
            matches=as_int(d.get("matches")),
            victories=as_int(d.get("victories")),
            win_rate=as_float(d.get("winRate")),
            gold_spent=as_int(d.get("goldSpent")),
            leaks=as_int(d.get("leaks")),
            best_rounds=as_int(d.get("bestRounds")),
            titles=titles,
            first_at=as_int(d.get("firstAt")),
            last_at=as_int(d.get("lastAt")),
        )


@dataclass
class DailyMatchPlayer:
    player_id: str = ""
    name: str = ""
    is_bot: bool = False
    victory: bool = False
    rounds_passed: int = 0
    gold_spent: int = 0
    leaks: int = 0
    title_id: Optional[str] = None
    title_name: Optional[str] = None

    @staticmethod
    def from_json(v: Any) -> "DailyMatchPlayer":
        d = as_dict(v)
        title = as_dict(d.get("title"))
        return DailyMatchPlayer(
            player_id=as_str(d.get("playerId")),
            name=as_str(d.get("name"), "?"),
            is_bot=as_bool(d.get("isBot")),
            victory=as_bool(d.get("victory")),
            rounds_passed=as_int(d.get("roundsPassed")),
            gold_spent=as_int(d.get("goldSpent")),
            leaks=as_int(d.get("leaks")),
            title_id=as_opt_str(title.get("id")),
            title_name=as_opt_str(title.get("name")),
        )


@dataclass
class DailyMatch:
    id: str = ""
    at: int = 0
    room_code: str = ""
    mode: Optional[str] = None
    difficulty: Optional[str] = None
    victory: bool = False
    rounds_passed: int = 0
    duration_ms: int = 0
    players: List[DailyMatchPlayer] = field(default_factory=list)

    @property
    def difficulty_label(self) -> str:
        return DIFFICULTY_LABELS.get(self.difficulty or "", self.difficulty or "?")

    @property
    def mode_label(self) -> str:
        return MODE_LABELS.get(self.mode or "", self.mode or "?")

    @staticmethod
    def from_json(v: Any) -> "DailyMatch":
        d = as_dict(v)
        return DailyMatch(
            id=as_str(d.get("id")),
            at=as_int(d.get("at")),
            room_code=as_str(d.get("roomCode")),
            mode=as_opt_str(d.get("mode")),
            difficulty=as_opt_str(d.get("difficulty")),
            victory=as_bool(d.get("victory")),
            rounds_passed=as_int(d.get("roundsPassed")),
            duration_ms=as_int(d.get("durationMs")),
            players=[DailyMatchPlayer.from_json(x) for x in as_list(d.get("players"))],
        )


@dataclass
class DailySummary:
    matches: int = 0
    finished: int = 0
    victories: int = 0
    defeats: int = 0
    win_rate: float = 0.0
    players: int = 0
    humans: int = 0
    gold_spent: int = 0
    leaks: int = 0
    rounds: int = 0
    duration_ms: int = 0
    training_matches: int = 0
    by_difficulty: Dict[str, Any] = field(default_factory=dict)
    by_mode: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_json(v: Any) -> "DailySummary":
        d = as_dict(v)
        return DailySummary(
            matches=as_int(d.get("matches")),
            finished=as_int(d.get("finished")),
            victories=as_int(d.get("victories")),
            defeats=as_int(d.get("defeats")),
            win_rate=as_float(d.get("winRate")),
            players=as_int(d.get("players")),
            humans=as_int(d.get("humans")),
            gold_spent=as_int(d.get("goldSpent")),
            leaks=as_int(d.get("leaks")),
            rounds=as_int(d.get("rounds")),
            duration_ms=as_int(d.get("durationMs")),
            training_matches=as_int(d.get("trainingMatches")),
            by_difficulty=as_dict(d.get("byDifficulty")),
            by_mode=as_dict(d.get("byMode")),
        )


@dataclass
class DailyPlayer:
    player_id: str = ""
    name: str = ""
    stats: PlayerStats = field(default_factory=PlayerStats)

    @staticmethod
    def from_json(v: Any) -> "DailyPlayer":
        d = as_dict(v)
        return DailyPlayer(
            player_id=as_str(d.get("playerId")),
            name=as_str(d.get("name"), "?"),
            stats=PlayerStats.from_json(d.get("stats") if "stats" in d else d),
        )


@dataclass
class DailyReport:
    """``GET /api/bot/v1/daily``。"""

    ok: bool = False
    protocol: int = 0
    date: str = ""
    tz: str = ""
    server_now: int = 0
    generated_at: int = 0
    summary: DailySummary = field(default_factory=DailySummary)
    titles: Dict[str, TitleBoard] = field(default_factory=dict)
    players: List[DailyPlayer] = field(default_factory=list)
    matches: List[DailyMatch] = field(default_factory=list)

    def board(self, title_id: str) -> Optional[TitleBoard]:
        return self.titles.get(title_id)

    @staticmethod
    def from_json(v: Any) -> "DailyReport":
        d = as_dict(v)
        titles: Dict[str, TitleBoard] = {}
        for key, raw in as_dict(d.get("titles")).items():
            if not isinstance(key, str):
                continue
            board = TitleBoard.from_json(raw)
            if not board.id:
                board.id = key
            titles[key] = board
        return DailyReport(
            ok=as_bool(d.get("ok"), True),
            protocol=as_int(d.get("protocol")),
            date=as_str(d.get("date")),
            tz=as_str(d.get("tz")),
            server_now=as_int(d.get("serverNow")),
            generated_at=as_int(d.get("generatedAt")),
            summary=DailySummary.from_json(d.get("summary")),
            titles=titles,
            players=[DailyPlayer.from_json(x) for x in as_list(d.get("players"))],
            matches=[DailyMatch.from_json(x) for x in as_list(d.get("matches"))],
        )


@dataclass
class PlayerProfile:
    """``GET /api/bot/v1/player``。"""

    ok: bool = False
    protocol: int = 0
    server_now: int = 0
    player_id: str = ""
    name: str = ""
    bound: bool = False
    total: PlayerStats = field(default_factory=PlayerStats)
    today: PlayerStats = field(default_factory=PlayerStats)
    recent: List[DailyMatch] = field(default_factory=list)

    @staticmethod
    def from_json(v: Any) -> "PlayerProfile":
        d = as_dict(v)
        player = as_dict(d.get("player"))
        return PlayerProfile(
            ok=as_bool(d.get("ok"), True),
            protocol=as_int(d.get("protocol")),
            server_now=as_int(d.get("serverNow")),
            player_id=as_str(player.get("playerId")) or as_str(d.get("playerId")),
            name=as_str(player.get("name")) or as_str(d.get("name")),
            bound=as_bool(player.get("bound")),
            total=PlayerStats.from_json(d.get("total")),
            today=PlayerStats.from_json(d.get("today")),
            recent=[DailyMatch.from_json(x) for x in as_list(d.get("recent"))],
        )


# ---- 绑定 ---------------------------------------------------------------------------------------


@dataclass
class BindChallenge:
    """``POST /api/bot/v1/bind/request``。"""

    request_id: str = ""
    masked_email: str = ""
    expires_at: int = 0
    retry_after_ms: int = 0

    @staticmethod
    def from_json(v: Any) -> "BindChallenge":
        d = as_dict(v)
        return BindChallenge(
            request_id=as_str(d.get("requestId")),
            masked_email=as_str(d.get("maskedEmail")),
            expires_at=as_int(d.get("expiresAt")),
            retry_after_ms=as_int(d.get("retryAfterMs")),
        )


@dataclass
class BindResult:
    """``POST /api/bot/v1/bind/verify``。"""

    player_id: str = ""
    name: str = ""
    email: str = ""
    bound_at: int = 0

    @staticmethod
    def from_json(v: Any) -> "BindResult":
        d = as_dict(v)
        return BindResult(
            player_id=as_str(d.get("playerId")),
            name=as_str(d.get("name")),
            email=as_str(d.get("email")),
            bound_at=as_int(d.get("boundAt")),
        )


@dataclass
class ServerMeta:
    """``GET /api/bot/v1/meta``：服务端支持哪些能力。"""

    ok: bool = False
    protocol: int = 0
    app: str = ""
    tz: str = ""
    capabilities: List[str] = field(default_factory=list)
    accounts: bool = False

    def has(self, cap: str) -> bool:
        return cap in self.capabilities

    @staticmethod
    def from_json(v: Any) -> "ServerMeta":
        d = as_dict(v)
        caps = [x for x in as_list(d.get("capabilities")) if isinstance(x, str)]
        return ServerMeta(
            ok=as_bool(d.get("ok"), True),
            protocol=as_int(d.get("protocol")),
            app=as_str(d.get("app")),
            tz=as_str(d.get("tz")),
            capabilities=caps,
            accounts=as_bool(d.get("accounts")),
        )
