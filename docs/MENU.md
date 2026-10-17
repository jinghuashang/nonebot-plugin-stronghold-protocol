# `帮助 卫戍` 菜单（可选，基于 nonebot-plugin-picmenu-next）

本插件只负责**自己那一项**的菜单：发 `/帮助 卫戍`（插件详情）与 `/帮助 卫戍 序号`（功能详情）。
顶层 `/帮助` 首页**不归我管** —— 你们可以继续用现有的全局模板（例如 `archive`），本插件不会去改
`PMN_INDEX_TEMPLATE`。

| `/帮助 卫戍`（插件详情） | `/帮助 卫戍 2`（功能详情） |
|---|---|
| ![插件详情](img/menu-detail.jpg) | ![功能详情](img/menu-func.jpg) |

## 设计语言

配色与质感**照抄游戏本体**（`public/css/theme.css` 的令牌，名字都一样，方便对照）：

| 令牌 | 值 | 用途 |
|---|---|---|
| `--bg-0…4` | `#0c0f0e` → `#2d3632` | 近黑墨绿的面（页 / 面板 / 卡片 / 表头） |
| `--line…3` | `#2f3a35` / `#3e4b45` / `#58675f` | 细描边 |
| `--mint-400/500/600` | `#59f4ca` / `#4ed8af` / `#35d8b4` | 主色（标题竖条、数字、微标、角框） |
| `--gold` | `#ffc600` | **消耗资金**（游戏里价格就是金色） |
| `--amber` / `--red` / `--ice` | `#f6a329` / `#e73118` / `#9fd4ff` | 称号配色（挥金如土 / 卫戍之区 / 坚若磐石） |
| `--text-hi/md/lo/dim` | `#f2f2f2` / `#c3cbc7` / `#8a948f` / `#5d6863` | 四级文字 |

还复刻了游戏里三个签名做法：

- **括号角框**（`.brackets`）：四个 L 形角用渐变画在 `::after` 上，和 `theme.css:209` 完全同一手法；
  外层面板用薄荷角框，我们自己的卡片用更小的角框。
- **技术微标**（`.micro`）：全大写、宽字距、暗色小字，如 `LIVE` / `INTRO` / `TRIGGER` / `COMMANDS`。
- **扫描线**（`.fx-scan`）：1px 白线每 3px 一道、`mix-blend-mode: screen`，压得比游戏里更淡。

内容上：`服务器状态` 一块是 HUD（在线/对局中/组队中/大厅/观战/今日局数/胜率/**金色消耗资金**/漏怪），
下面按称号配色列出**今日战报**（卫戍之星=金、坚若磐石=冰蓝、挥金如土=琥珀、精英云集=薄荷、卫戍之区=红），
再往下是简介 / 用法（Markdown）、**功能表**（`# / 功能 / 触发方式 / 触发条件` + 一行简介）、**快速上手**命令清单。
页脚只写「服务器名 · 生成时间 · `nonebot-plugin-stronghold-protocol vX.Y.Z`」，不显示 picmenu 的版本与模板名。

> 这五个称号的配色是本插件自己按游戏调色板挑的，不是官方指定色；想换就改
> `nonebot_plugin_stronghold_protocol/models.py` 的 `TITLE_ACCENTS`。

## 安装与配置

```bash
uv pip install "nonebot-plugin-stronghold-protocol[picmenu]"   # 或直接装 nonebot-plugin-picmenu-next
```

```dotenv
# 让「卫戍协议」这一项用本模板（只写这一处就够，首页模板保持你们原来的）
# 本插件已在 PluginMetadata.extra 里声明 pmn.template=stronghold，通常无需再配
# PMN_DETAIL_TEMPLATE=stronghold          # 可选：全局插件详情页也想用它
# PMN_FUNC_DETAIL_TEMPLATE=stronghold     # 可选：全局功能详情页也想用它

# 模板自己的开关（pmn_stronghold_* 前缀）
PMN_STRONGHOLD_LIGHT=false                # true = 浅色变体（默认就是设计语言本身的近黑墨绿）
```

```dotenv
# 本插件侧（详见 README）
stronghold_menu_server_name=卫戍协议：盟约
stronghold_menu_live=true        # 关掉则不请求游戏服，菜单里不出现「服务器状态」
stronghold_menu_live_ttl=30      # 实时数据缓存秒数
stronghold_menu_timeout=2        # 取数超时（秒）
```

### 前置要求（picmenu-next 带来的）

1. **Python ≥ 3.10**（本插件本体支持 3.9，3.9 的部署会自动跳过菜单）。
2. **localstore 必须显式指定路径**：picmenu-next 的依赖 `cookit` 会拒绝「localstore 用系统默认目录」，
   报「因此，本插件将 拒绝加载」。二选一：`LOCALSTORE_USE_CWD=True`（推荐）或
   `I_AM_SURE_I_WANT_TO_USE_DEFAULT_LOCALSTORE_FILE_PATH=True`。
3. **出图需要浏览器**：`RENDER_BACKEND=playwright`，首次用之前 `playwright install chromium`（或开 `RENDER_STARTUP_MODE=probe`）。

## 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| `/帮助 卫戍` 还是老样式 | 详情模板没命中：确认 `extra["pmn"]["template"]="stronghold"` 生效（本插件已声明），或显式配 `PMN_DETAIL_TEMPLATE=stronghold`。 |
| 没有「服务器状态」一块 | `stronghold_menu_live=false`，或游戏服还没实现 `docs/BRIDGE-API.md` 的接口（那种情况下这整块不渲染，不会报错）。 |
| 菜单把机器人搞成重启循环 | 旧版本在**插件导入期** `require` picmenu-next，和 `pyproject.toml` 的加载撞车（`Plugin already exists`）。现在注册在 `on_startup`，已修。 |
| 出图很慢/超时 | 第一次会拉起 Chromium；开 `RENDER_STARTUP_MODE=probe` 或预装浏览器。 |

## 实现要点（给改代码的人）

- **注册延迟到 `on_startup`**（`menu/__init__.py` 的 `register_menu_templates()`）：导入期注册会让
  bot 随后从 `pyproject.toml` 再加载一次 picmenu-next → `RuntimeError: Plugin already exists` → 容器重启循环。
  同时**必须用 `nonebot.require`**，不能 `import`（否则后续谁都 `require` 不动它）。
- 模板注册名 `stronghold`，三件套用 picmenu-next 的 `index_templates` / `detail_templates` /
  `func_detail_templates`；重名只 warning 不抛（`template.py` 的 `_register`）。
- 渲染管线照抄 pmn 内置 `default`（MIT）：Jinja + `build_base_render_kwargs` + `base_routers` +
  `get_new_page(viewport={"width": 810, "height": 2430})` + `screenshot_html(..., selector="main", type="jpeg")`；
  KaTeX 资源按 `/katex/<file>` 映射（上游 `default` 的 `/markdown/katex/...` 会多算一层路径）。
- 实时数据在 `menu/live.py`：TTL + 并发去重 + 超时 + 失败返回 `None`（菜单绝不因取数失败而报错）。
- 「今日战报」注入用**全局** `plugin_detail_mixins`：`self_*` 收集器靠「当前正在加载的插件」定位自己，
  在 `on_startup` 里那个上下文已经没了，会被登记到 picmenu-next 名下而永不生效。
  另外：当这一项用的就是我们的模板时（`info.pmn.template == "stronghold"`）**跳过注入** ——
  自己的模板已经把 HUD 画出来了，避免同一组数字出现两次；用别人的模板时才注入 Markdown 版本。
  注入时必须 `model_copy()`，否则污染 picmenu-next 的全局菜单数据。
