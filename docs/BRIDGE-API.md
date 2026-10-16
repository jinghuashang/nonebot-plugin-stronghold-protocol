# Stronghold Bot Bridge API v1 · 卫戍协议机器人接入契约

> 这份文档是**服务端要实现的接口规格**，也是**插件唯一依赖的取数通道**。
> 任何人（上游 `sganggs/Stronghold-Protocol` 或任何 fork）在自家卫戍协议服务端实现下面的
> 6 个 HTTP 接口，就能直接接入本插件（或任何按同一契约写的机器人 / 看板 / 群推送）。
>
> - 参考客户端：`nonebot-plugin-stronghold-protocol`（本仓库）
> - 参考服务端实现：`docs/server/stronghold-bot-bridge.mjs`（可直接放进 `server/`，见 §8 接线清单）
> - 契约版本：**1**（响应头 `X-Bridge-Protocol: 1`）

---

## 0. 设计取舍（先读这段，避免实现跑偏）

| 取舍 | 理由 |
|---|---|
| **独立命名空间** `/api/bot/v1`，不碰 `/api/admin/*` | 后台管理是服主的运维口子（玩家 IP、流量）；机器人要的是玩家视图。两者权限、限流、字段白名单都不同，混在一起早晚出事故。 |
| **纯拉取（pull），没有 webhook / 长连接** | 命令都是人手动触发；日报一天一次。拉取实现最简单，服务端崩了/重启了机器人不会积压消息。 |
| **只读 + 一个「邮箱归属证明」写操作** | 契约不碰对局、不碰经济。唯一的写是绑定：把验证码发到账号邮箱并校验，服务端**不留任何绑定状态**（绑定关系存在机器人侧）。 |
| **聚合落盘由服务端负责** | 「今日消耗资金」「今日各称号」这类数据只在每局结算的 `m.result` 帧里出现过，谁也不落盘（上游的 `/healthz`、房间、结果回放都不带）。契约要求服务端在结算处**顺手记一份按天聚合**，否则任何人都拿不到（详见 §6.3）。 |
| **字段可缺、客户端必须容错** | `capabilities` 协商：服务端可以先只做 `/status`，其余返回 `unsupported`；客户端必须忽略未知字段、缺字段按默认值。 |

---

## 1. 通用约定

### 1.1 挂载与版本

- 前缀默认 `/api/bot/v1`；可以整体挂到别的前缀（客户端用配置 `stronghold_api_prefix` 对齐）。
- `GET /api/bot/v1/meta` **无需鉴权**：能力发现用，也是「这台服到底支不支持机器人」的探针。
- 所有响应：`Content-Type: application/json`、`Cache-Control: no-store`、`X-Bridge-Protocol: 1`。
- 只接受 `GET` / `POST`；其它方法 → `405` + `{"ok":false,"error":"method_not_allowed"}`。
- 未知路径 → `404` + `{"ok":false,"error":"not_found"}`。

### 1.2 鉴权

```
Authorization: Bearer <BOT_TOKEN>
X-Bot-Token: <BOT_TOKEN>          # 二选一，两个都带时以 Authorization 为准
```

- 令牌来自服务端环境变量 `BOT_TOKEN`（建议同时支持 `BOT_TOKEN_FILE`：一个每次校验都重读的文件，方便轮换，参考 `ADMIN_TOKEN_FILE` 的做法）。
- 比较必须**常量时间**；失败 → `401` + `{"ok":false,"error":"unauthorized"}`。
- 没配 `BOT_TOKEN` 时：`/meta` 仍然可用（`"auth":"off"`），其余接口 → `403` + `{"ok":false,"error":"bot_disabled"}`。
- **绝对不要在响应里回显令牌、邮箱明文、玩家 IP、重连令牌、口令 hash。** 邮箱一律打码（`p****@example.com`）。

### 1.3 限流（建议值，服务端可调）

| 端点 | 建议 |
|---|---|
| `/status` `/player` `/daily` | 每来源 IP 120 次/分钟；超限 `429` + `retryAfterMs` |
| `/bind/request` | 每邮箱 1 次/分钟、每 IP 10 次/小时 |
| `/bind/verify` | 每个 `requestId` 5 次错误尝试后作废 |

带 `qq` 参数时，服务端应在日志里记下「哪个 QQ 请求了哪个邮箱」，便于线下核对。**不要把 QQ 号回显给别的玩家。**

### 1.4 错误格式

所有错误（含 4xx/5xx）统一：

```json
{ "ok": false, "error": "code", "message": "给玩家看的中文", "detail": "可选，给服主看的" }
```

| `error` | HTTP | 含义 |
|---|---|---|
| `unauthorized` | 401 | 令牌错/缺 |
| `bot_disabled` | 403 | 服务端没配置机器人令牌 |
| `not_found` | 404 | 路径不存在 |
| `unsupported` | 200/501 | 这台服没实现该能力（见 `capabilities`） |
| `bad_request` | 400 | 参数缺失或格式错（`detail` 说清哪个参数） |
| `player_not_found` | 404 | `/player` 查不到这个玩家 |
| `accounts_disabled` | 400 | 这台服没开账号系统（`bind.*` 不可用） |
| `smtp_failed` | 502 | 验证码发不出去（SMTP 没配好） |
| `account_not_found` | 404 | 该邮箱没有注册过账号（注意：只有服主明确要求时才暴露这个信息，否则统一作答，见 §4.5） |
| `code_invalid` | 400 | 验证码错 |
| `code_expired` | 400 | 验证码过期 / 错误次数用尽 |
| `request_not_found` | 404 | `requestId` 不认识（过期或伪造） |
| `too_many_requests` | 429 | 触发限流（带 `retryAfterMs`） |
| `internal` | 500 | 服务端自己的问题 |

### 1.5 时间与「日」

- **所有时间戳 = 毫秒 epoch 整数**（`Date.now()`）。
- 「今天」的日界 = **服务端本地时区**（部署示例 `TZ=Asia/Shanghai`），由 `meta.tz` 声明，`daily.date` 用它。
- 日期字符串格式 `YYYY-MM-DD`；`/daily?date=` 除 `date` 外不接受别的格式（`400 bad_request`）。
- 未来日期是合法的：返回一份空报告（`summary.matches = 0`）。

### 1.6 训练局

`difficulty === "TRAINING"` 或 `modeId` 以 `mode_training` 开头的对局 = 训练/入门协议：**无奖励、可无限刷**。

- 一律**不计**入称号、不计入 `players` 的战绩；
- 单独计入 `summary.trainingMatches`；
- **不计入胜率**（否则训练局会把胜率刷歪）。

### 1.7 统计口径（务必对齐，否则日报数字对不上）

| 字段 | 口径 |
|---|---|
| `matches` | 当天**结束**的非训练局数 |
| `victories` / `defeats` | 队伍胜 / 负的局数（一场只有队胜或队负） |
| `winRate` | `victories / matches`（`0`～`1` 的小数；`matches = 0` 时给 `0`） |
| `players` | 当天参与过的**去重人数**：有 `playerId`（登录账号 `a_…`）按账号算，游客按会话昵称算 |
| `humans` | 人类**席位**总和（同一人打两局算 2，是「人次」不是「人数」） |
| `goldSpent` | `Σ players[].stats.gold`（本局消耗资金，见 §5.2） |
| `leaks` | `Σ players[].stats.leaks` |
| `rounds` | `Σ` 队伍的 `roundsPassed` |
| `durationMs` | `Σ` 队伍的对局时长 |

### 1.8 玩家标识

| 标识 | 形态 | 说明 |
|---|---|---|
| `playerId`（登录） | `a_` + 10 位小写 hex，如 `a_0000000001` | 账号的稳定 id（`data/accounts.json` 的键）。跨设备不变，**是绑定与战绩的基准**。 |
| `playerId`（游客） | `p_` + … | 会话 id，重连就可能变。**游客不进战绩、不进榜、不能被绑定。** |
| 昵称 `name` | 任意 | 不唯一，只能用来「找找看」，不能作为身份。 |
| `account` | `"a_…"` 或 `null` | `/status` 里区分「登录玩家」与「游客」的字段。 |

---

## 2. 能力协商：`GET /meta`

无需鉴权。

```json
{
  "ok": true,
  "protocol": 1,
  "app": "0.2.6",
  "tz": "Asia/Shanghai",
  "accounts": true,
  "auth": "on",
  "capabilities": ["status", "player", "daily", "bind.email", "maintenance"]
}
```

| `capability` | 含义 | 没实现时客户端表现 |
|---|---|---|
| `status` | `/status` 可用 | `/在线`、`/对局` 报「这台服还没实现该接入能力」 |
| `player` | `/player` 可用 | `/战绩` 同上 |
| `daily` | `/daily` 可用 | `/日报` 与定时推送同上 |
| `bind.email` | `/bind/*` 可用（服务端有账号 + SMTP） | `/绑定` 提示「这台服务器没有开启账号系统」 |
| `bind.operator` | 支持人工代绑（`POST /bind/grant`，可选，见 §4.7） | 不显示 |
| `maintenance` | `/status.maintenance` 会如实反映维护/公告状态 | 不显示维护横幅 |

`auth`: `"on"` = 需要并已配置令牌；`"off"` = 只开放 `/meta`。`accounts`: 这台服是否开了账号系统。

---

## 3. 在线：`GET /status`

一次拿到「在线玩家 + 在线对局」。客户端 `/在线`、`/对局` 都打它。

### 3.1 响应

```json
{
  "ok": true,
  "protocol": 1,
  "app": "0.2.6",
  "serverNow": 1791811511760,
  "maintenance": { "mode": "off", "reason": null, "until": null },
  "counts": {
    "online": 12, "inMatch": 5, "waiting": 4, "inHall": 3, "spectators": 0,
    "rooms": 3, "matches": 2, "accounts": 101
  },
  "players": [
    {
      "playerId": "a_0000000002", "account": "a_0000000002", "name": "阿米娅",
      "state": "match", "roomCode": "YFAF", "seat": 0, "isHost": true, "spectating": false,
      "mode": "coop", "difficulty": "HARD", "joinedAt": 1791801610209
    },
    {
      "playerId": "p_31c8f0a91b", "account": null, "name": "路过",
      "state": "idle", "roomCode": null, "seat": null, "isHost": false, "spectating": false,
      "mode": null, "difficulty": null, "joinedAt": 1791811000000
    }
  ],
  "matches": [
    {
      "roomCode": "YFAF", "mode": "coop", "difficulty": "HARD",
      "round": 7, "bossRound": 14, "startedAt": 1791801700000,
      "players": [
        { "playerId": "a_0000000002", "name": "阿米娅", "seat": 0, "isBot": false, "alive": true, "lp": 3, "spectating": false }
      ]
    }
  ]
}
```

### 3.2 字段

`counts`：

| 字段 | 口径 |
|---|---|
| `online` | 当前**连接的会话**数（含正在对局 / 大厅 / 空闲；不含已断线等重连的） |
| `inMatch` | 坐在**已开局**同盟里的人类数 |
| `waiting` | 在同盟里但还没开局的人类数 |
| `inHall` | 在大厅里的人（大厅是可选加入的，只统计加入了的人） |
| `spectators` | 观战人数 |
| `rooms` | 房间数（含未开局） |
| `matches` | 已开局的房间数 |
| `accounts` | 注册账号总数（可选字段，没有就省略） |

> [!NOTE]
> `online` 与游戏标题页 / `GET /api/info` 的「在线」**不是一个量**：后者是 `lobby.stats().humans`，
> 只算**坐在同盟里**的人。同一时刻服务端会有三个数：`/healthz.sessions`（注册表里的会话，含等重连的）、
> `counts.online`（真正连着 socket 的）、`humans`（在同盟里的）。客户端展示时请把三者分开说
> （例如「在线 7 人 · 对局中 3 · 空闲 4」），不要拿 `online` 去和标题页的数字对齐。

`players[]`：

| 字段 | 类型 | 说明 |
|---|---|---|
| `playerId` | string | 会话 id（游客也会给）。**给机器人用的稳定标识，不要给 IP/重连令牌。** |
| `account` | string \| null | 登录账号 id（`a_…`），游客为 `null` |
| `name` | string | 当前昵称 |
| `state` | string | `match` 对局中 · `waiting` 待开局 · `hall` 在大厅 · `idle` 在线但没进任何地方 |
| `roomCode` | string \| null | 所在同盟（4 位字母） |
| `seat` | number \| null | 座位号 |
| `isHost` | bool | 是否房主 |
| `spectating` | bool | 是否观战席 |
| `mode` / `difficulty` | string \| null | 所在房间的模式 / 难度 |
| `joinedAt` | number | 上线（本会话建立）时刻，ms |

`matches[]`：`roomCode`、`mode`(`solo`/`coop`)、`difficulty`、`round`（第几回合）、`bossRound`（最终攻势回合，拿不到给 `0`）、`startedAt`、`players[]`（`playerId`、`name`、`seat`、`isBot`、`alive`、`lp`、`spectating`）。

### 3.3 约束

- 内存直读，**不要**在请求里扫盘或算重活；建议整个响应 < 20 KB。
- `players` 上限 200、`matches` 上限 50（超出截断，并给 `"truncated": true`）。
- 顺序：按 `state`（对局中 → 待开局 → 大厅 → 空闲）、再按 `joinedAt` 升序。

---

## 4. 战绩与日报

### 4.1 `GET /player?playerId=<a_…>` 或 `?name=<昵称>`

`playerId` 优先；两个都给时用 `playerId`。

- `?playerId=a_…`：**一定**返回 200 + 一份（可能全 0 的）战绩 —— 调用方已经断言了这个 id，没打过就是「暂无记录」。
- `?name=<昵称>`：找不到任何同名玩家才回 `404 player_not_found`（昵称不唯一，所以按昵称查是「找找看」）。

```json
{
  "ok": true,
  "protocol": 1,
  "serverNow": 1791811511760,
  "player": { "playerId": "a_0000000002", "name": "阿米娅", "bound": true },
  "total": {
    "matches": 42, "victories": 30, "winRate": 0.714,
    "goldSpent": 15230, "leaks": 18, "bestRounds": 15,
    "titles": { "comment_1": 3, "comment_3": 1, "comment_6": 2, "comment_4": 1, "worst": 1 },
    "firstAt": 1791000000000, "lastAt": 1791811130971
  },
  "today": { "matches": 3, "victories": 2, "winRate": 0.667, "goldSpent": 1200, "leaks": 4, "bestRounds": 15,
             "titles": { "comment_1": 1 }, "firstAt": 1791800000000, "lastAt": 1791811130971 },
  "recent": [
    {
      "id": "GJCR-60", "at": 1791811130971, "roomCode": "GJCR",
      "mode": "solo", "difficulty": "HARD", "victory": true,
      "roundsPassed": 14, "durationMs": 1511829,
      "players": [
        { "playerId": "a_0000000002", "name": "阿米娅", "isBot": false, "victory": true, "roundsPassed": 14,
          "goldSpent": 320, "leaks": 0, "title": { "id": "comment_1", "name": "卫戍之星" } }
      ]
    }
  ]
}
```

- `player.bound` = 服务端认它是「登录账号」（有聚合记录，或 id 形如 `a_…`）；游客 `false`。
  游客只应拿到 `today`/`recent` 里当前会话的记录，`total` 给空。
- `total` = 该账号**历史累计**，`today` = 服务端本地时区的今天。
- `recent`：最近 20 局（`?limit=` 可调，上限 50），时间倒序。
- `titles` 的键就是称号 id（见 §5.1），值为获得次数；没有的称号可以省略。

### 4.2 `GET /daily[?date=YYYY-MM-DD]`

日报的全部数据源。`date` 缺省 = 服务端今天。

```json
{
  "ok": true,
  "protocol": 1,
  "date": "2026-10-15",
  "tz": "Asia/Shanghai",
  "serverNow": 1791811511760,
  "generatedAt": 1791811511800,
  "summary": {
    "matches": 12, "finished": 12, "victories": 8, "defeats": 4, "winRate": 0.667,
    "players": 9, "humans": 14, "goldSpent": 12345, "leaks": 37, "rounds": 88,
    "durationMs": 12345678, "trainingMatches": 2,
    "byDifficulty": { "HARD": { "matches": 8, "victories": 6 }, "ABYSS": { "matches": 4, "victories": 2 } },
    "byMode": { "solo": { "matches": 4, "victories": 3 }, "coop": { "matches": 8, "victories": 5 } }
  },
  "titles": {
    "comment_1": {
      "id": "comment_1", "name": "卫戍之星", "note": "对敌方领袖造成伤害最高（仅胜利时）",
      "kind": "title", "total": 2,
      "holders": [
        { "playerId": "a_0000000002", "name": "阿米娅", "count": 2, "victories": 2, "bestRounds": 15, "lastAt": 1791811130971 },
        { "playerId": "a_3271091dc6", "name": "能天使", "count": 1, "victories": 1, "bestRounds": 14, "lastAt": 1791630417174 }
      ]
    },
    "worst": {
      "id": "worst", "name": "卫戍之区", "note": "漏怪最多", "kind": "leaks", "total": 3,
      "holders": [
        { "playerId": "a_0000000003", "name": "玫兰莎", "leaks": 321, "best": 101, "matches": 7, "lastAt": 1791648619728 }
      ]
    }
  },
  "players": [
    { "playerId": "a_0000000002", "name": "阿米娅",
      "stats": { "matches": 3, "victories": 2, "winRate": 0.667, "goldSpent": 1200, "leaks": 4, "bestRounds": 15,
                 "titles": { "comment_1": 1 }, "firstAt": 1791800000000, "lastAt": 1791811130971 } }
  ],
  "matches": [
    { "id": "GJCR-60", "at": 1791811130971, "roomCode": "GJCR", "mode": "solo", "difficulty": "HARD",
      "victory": true, "roundsPassed": 14, "durationMs": 1511829,
      "players": [
        { "playerId": "a_0000000002", "name": "阿米娅", "isBot": false, "victory": true, "roundsPassed": 14,
          "goldSpent": 320, "leaks": 0, "title": { "id": "comment_1", "name": "卫戍之星" } }
      ] }
  ],
  "matchesTruncated": false
}
```

约束：

- `titles` **必须包含** §5.1 里的 `comment_1 / comment_3 / comment_6 / comment_4`（当天无人获得时给空 `holders`）；`worst` 可选（没实现就不出现这个键）。`note` 可以省略，客户端有内置兜底文案。
- `holders` 按口径降序（称号：`count`；漏怪：`leaks`），并列按 `lastAt` 降序；每榜最多 20 行。
- `players` 上限 200（按 `matches` 降序）；`matches` 上限 500（时间倒序），截断时 `matchesTruncated = true`。
- `byDifficulty` / `byMode` 的键用服务端自己的难度/模式 id（`TRAINING`/`FUNNY`/`NORMAL`/`HARD`/`ABYSS`、`solo`/`coop`）。
- **只有登录账号进 `players` 与 `titles.holders`**（游客不计，否则没法绑定、也没法跨设备累计）。

### 4.3 `POST /bind/request`

请求：

```json
{ "email": "player@example.com", "qq": "123456789" }
```

响应：

```json
{ "ok": true, "protocol": 1, "requestId": "b_9f2c…", "maskedEmail": "p****@example.com",
  "expiresAt": 1791812111760, "retryAfterMs": 60000 }
```

- 服务端把验证码发到**这个游戏账号的邮箱**（复用注册/重置邮件通道即可）。验证码语义：证明「请求者能收到该账号的邮箱」。
- `requestId` 由服务端生成（不透明字符串，≥16 字符），有效期建议 10 分钟；同一 `requestId` 只能成功校验一次。
- `retryAfterMs`：同邮箱/同 QQ 再次请求时需要等待的毫秒数（客户端据此提示）。
- **隐私**：`maskedEmail` 只能打码；请求一个不存在的邮箱时，若服主不希望暴露「哪些邮箱注册过」，就照常返回 200（和存在的邮箱一样的形状），真正发码时才放弃 —— 与项目里 `auth.resetSent` 的既有做法一致。

### 4.4 `POST /bind/verify`

请求：

```json
{ "requestId": "b_9f2c…", "code": "123456", "qq": "123456789" }
```

响应：

```json
{ "ok": true, "protocol": 1, "playerId": "a_0000000001", "name": "凯尔希",
  "email": "p****@example.com", "boundAt": 1791811511800 }
```

- 校验通过后**服务端不需要保存绑定关系**：把 `playerId` 交给客户端即可（本插件把它写进自己的绑定表）。
- 错误：`code_invalid` / `code_expired` / `request_not_found` / `accounts_disabled`。
- 建议：同一 `requestId` 5 次错误后作废（和项目的验证码语义保持一致）。

### 4.5 `POST /bind/grant`（可选能力 `bind.operator`）

给服主「人工代绑」用：`{ "qq": "…", "playerId": "a_…" }` → `{ "ok": true, "playerId": "a_…", "name": "…" }`。
只应在服务端配置了额外开关（例如 `BOT_BIND_OPERATOR=on`）时启用，并且**要求同样的 BOT_TOKEN**。

---

## 5. 术语与口径对照（服务端实现时的取数位置）

### 5.1 称号

| `id` | 名称 | 达成条件 | 上游取数位置 |
|---|---|---|---|
| `comment_1` | 卫戍之星 | `stats.bossDamage` 最高，**仅胜利局** | `data/config.json` `titles[0]`（`server/match/results.js` 的 `STAT_OF`） |
| `comment_3` | 坚若磐石 | `stats.lpLost` 最少（只算结束时存活的人） | `data/config.json` `titles[2]`，**被 `data/tuning.json` 覆盖为 `lpLost` / `min`** |
| `comment_6` | 挥金如土 | `stats.gold`（本局消耗资金）最高 | `data/config.json` `titles[5]`；`results.js` 里 `fundsSpent = ps.stats.gold` |
| `comment_4` | 精英云集 | `stats.merges`（晋升精锐次数）最高 | `data/config.json` `titles[3]` |
| `worst` | 卫戍之区 | 漏怪最多（**不是官方称号，是社群口径**，可只按 `stats.leaks` 聚合） | 上游没有，等价于「`Σ leaks` 最高的玩家」 |

规则（上游 `assignTitles` 已保证）：每人每局最多 1 个称号；每个称号每局最多给 1 人。
所以「卫戍之星 count = 2」= 这个账号**有 2 局**被评上卫戍之星。

### 5.2 消耗资金

- 唯一来源：`m.result` → `players[].stats.gold`（**本局累计花费**，`economy.js` 每笔消费累加）。
- 上游**不落盘**这个数（后台统计的 `recent[]` 只留 `name/isBot/victory`，排行榜只留次数）。
  所以契约要求：**实现方在结算处抓一次 `m.result`，把 `stats.gold`、`stats.leaks`、`title.id` 等写进按天聚合**（§6.3）。

### 5.3 对局 `id` 与 `roomCode`

`m.result` 里没有房间码，也没有对局 id。实现方从房间/上下文取：

- `roomCode` ← 房间码（4 位字母）。
- `id` ← 建议 `<房间码>-<该房间第 n 局>`（上游 `server/lobby.js` 的 `room.matchCount`），保证同日唯一、可去重。

---

## 6. 服务端实现指南（上游 `sganggs/Stronghold-Protocol` v0.2.6）

> 上游只有 `/healthz` + 静态文件 + WebSocket `/ws`；账号、大厅、后台、排行榜都是各 fork 自己加的。
> 本节只依赖**上游一定有**的模块，任何 fork 照做都能接进来。

### 6.1 各端点怎么拼

| 端点 | 上游取数位置 |
|---|---|
| `counts`（在线/对局/房间/观战） | `server/lobby.js` 的 `stats()`（`{rooms, matches, humans, bots, spectators}`）+ `server/net.js` 的 `SessionRegistry.size` |
| `players[]` | 迭代 `SessionRegistry`（`server/net.js`：session 的 `playerId` / `name` / `connected`），再用 `lobby.rooms` 反查所在房间/座位/是否房主；`state` 由「在不在房间 + 房间开没开局」推出 |
| `matches[]` | `lobby.rooms`：`room.match` 非空即已开局；`room.seats[]` = `{seat, playerId, name, isBot, ready, connected, left}`、`room.spectators[]`；回合数从 `room.matchCtx` 或 `room.match` 上取（拿不到就给 0） |
| `maintenance` | fork 常见（公告/维护开关）；上游没有就固定给 `{"mode":"off","reason":null,"until":null}` |
| `player` / `daily` | **必须自己落一份聚合**（§6.3）：结算处抓 `m.result` → 按天 + 累计两份文件 |
| `bind.*` | 需要账号系统。fork 里 `server/accounts.js` 的 `requestResetCode(email)` + `verifyCode(email, code, 'reset')` + `accounts.file`（`playerId → email/name`）就能实现 `bind.email`。上游没有账号系统 → **不要**声明 `bind.email` 能力。 |

### 6.2 结算处抓 `m.result`

`m.result` 是**逐人单播**的（每名玩家收到一份自己的），所以：

- 抓**第一份**即可（各份的 `players[]` 内容一致，只有收件人视角不同）；
- 抓取点是 `server/lobby.js` 的 `matchSend` / `matchBroadcast`（判断 `msg.t === 'm.result'`），每局只记一次；
- 推荐做法：给 `onMatchEnd(room, ctx, summary)` 旁挂一个 `bridge.recordResult(room, msg)`，在 `matchSend`/`matchBroadcast` 里缓存该局的 `m.result` 到 `ctx`，`onMatchEnd` 时落盘。

### 6.3 落盘（按天 + 累计）

```
data/bot-daily/2026-10-15.json     # 当天聚合：summary/titles/players/matches（喂 /daily）
data/bot-players.json              # 账号累计：titles 次数、goldSpent、leaks、bestRounds…（喂 /player）
```

要求：

- **原子写**（`写 .tmp` → `rename`），和项目里 `accounts.json` / `leaderboard.json` 同样的风格；
- 只保留最近 30 天（够 30 天回溯日报）；
- 累计文件只记聚合数字，**不要**记邮箱/IP；
- 训练局只进 `trainingMatches` 计数，不进称号/玩家；
- 榜单行数有上限（建议 200 行/榜），超出淘汰最弱者，避免文件无限增长。

### 6.4 安全

- `/api/bot/v1` 是**玩家视图**：不返回 IP、不返回重连令牌、不返回邮箱明文、不返回口令/token hash。
- ⚠️ 上游把 `/data/` 当**静态目录**对外服务（`server/http/static.js` 的挂载表）。所以：
  **`BOT_TOKEN` 不能放在 `data/` 下**（会被匿名 GET 走），聚合文件也建议放到静态目录之外，或显式加白名单拒绝 `/data/bot-*`。
- 令牌建议支持 `BOT_TOKEN_FILE` 热读，轮换时不用重启。

---

## 7. 客户端必须遵守的兼容规则

1. 忽略未知字段、未知 `capability`；缺字段按 §3/§4 的默认值兜底。
2. 只用 §1.4 的 `error` 码做分支；未知码当通用错误显示。
3. 单次请求超时（本插件默认 8 s）；失败就报「稍后再试」，**不要重试风暴**。
4. 不缓存跨命令的响应（数字变化快，缓存只会让人困惑）。

---

## 8. 接线清单（上游 v0.2.6，约 20 行）

参考实现：[`docs/server/stronghold-bot-bridge.mjs`](server/stronghold-bot-bridge.mjs) —— 拷贝到 `server/bot-bridge.mjs`。

```js
// 1) server/index.js —— 建实例（lobby/network 之后）
import { createBotBridge } from './bot-bridge.mjs';
const bot = createBotBridge({ lobby, registry, dataDir, token: process.env.BOT_TOKEN, accounts });

// 2) server/http/routes.js —— 请求监听里，静态之前给它先手（createRequestHandler 拿得到 bot 时）
if (bot && await bot.handleHttp(req, res, parts)) return;

// 3) server/lobby.js —— 结算处喂一份 m.result
//    matchSend / matchBroadcast 里：if (msg?.t === 'm.result') this.bot?.recordResult(room, msg);
//    onMatchEnd 里：this.bot?.flush();
```

> 说明：本插件的参考实现**不依赖任何 fork 私有模块**；`accounts` 是可选的（不传就没有 `bind.email` 能力）。

---

## 9. 自测（复制即用）

```bash
BASE=http://127.0.0.1:3000
TOKEN=在服务端配的BOT_TOKEN

curl -s $BASE/api/bot/v1/meta | jq .                    # 无需令牌
curl -s -H "Authorization: Bearer $TOKEN" $BASE/api/bot/v1/status | jq '.counts,.players[0]'
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/api/bot/v1/daily" | jq '.summary,.titles.comment_1.holders[0]'
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/api/bot/v1/player?name=阿米娅" | jq '.player,.total'

# 绑定（会真发一封邮件）
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"email":"你的测试账号@qq.com","qq":"10001"}' $BASE/api/bot/v1/bind/request | jq .
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"requestId":"上一步的 requestId","code":"邮件里的验证码","qq":"10001"}' $BASE/api/bot/v1/bind/verify | jq .

# 反向用例
curl -s $BASE/api/bot/v1/status | jq .                   # 期待 401 unauthorized
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/api/bot/v1/player?name=不存在的人" | jq .   # 期待 404 player_not_found
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/api/bot/v1/daily?date=昨天" | jq .          # 期待 400 bad_request
```

验收标准（对齐本插件的用法）：

- [ ] `/meta` 无令牌可读，`capabilities` 与实际实现一致；
- [ ] `/status` 的 `counts.online` 与游戏里看到的人数一致；打完一局后 `matches` 减 1；
- [ ] `/daily` 的 `summary.matches` = 当天面板上的非训练局数；`goldSpent` 与当日实际消耗相符（**这是上游拿不到、必须由你记的那个数**）；
- [ ] 同一账号打一局后，`/player?playerId=…` 的 `today.goldSpent`、`titles` 有变化；
- [ ] 未知路径 404、错令牌 401、超限 429，且都带 `{"ok":false,"error":…}`。

---

## 10. 版本策略

- 破坏性变更（字段改名/删除、语义变化）→ 开 `/api/bot/v2`，v1 至少保留一个发布周期。
- 新增字段、新增 `capability`、新增端点 → **不算破坏**，直接加。
- `protocol` 字段与 `X-Bridge-Protocol` 响应头同步递增；客户端发现主版本不匹配时应当提示「服务端契约版本不兼容」，而不是崩。
