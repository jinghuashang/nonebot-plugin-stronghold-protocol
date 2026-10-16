// server/bot-bridge.mjs — Stronghold Bot Bridge v1（契约见 docs/BRIDGE-API.md）
//
// 放到上游 sganggs/Stronghold-Protocol 的 server/ 目录里就能用：只依赖上游一定有的模块
// （server/net.js 的 SessionRegistry、server/lobby.js 的房间、server/match/results.js 结算出的 m.result），
// 账号系统是可选适配器（不传就没有 bind.email 能力）。
//
// 它在结算处顺手记下「按天」和「按账号累计」两份聚合 —— 这是「今日消耗资金」「今日各称号」
// 唯一的来源：上游既没有这些接口，也没有任何人落盘 m.result 里的 players[].stats。
//
// 接线（3 处，约 20 行）：
//   1) server/index.js
//        import { createBotBridge } from './bot-bridge.mjs';
//        const bot = createBotBridge({ lobby, registry, dataDir, token: process.env.BOT_TOKEN, accounts: accountsAdapter });
//   2) server/http/routes.js（静态之前）
//        if (bot && await bot.handleHttp(req, res, parts)) return;
//   3) server/lobby.js
//        matchSend / matchBroadcast 里:  if (msg?.t === 'm.result') this.bot?.recordResult(room, msg);
//        onMatchEnd 里:                 this.bot?.flush();
//
// 注意：本文件不是插件的一部分，只是给服务端实现者的**参考实现**（可直接拷走改写）。
// 许可建议与服务端一致（GPL-3.0-or-later）；把它当作规格的可执行版本读。

import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

export const BRIDGE_PROTOCOL = 1;
export const BRIDGE_PREFIX = '/api/bot/v1';

/**
 * JSON 应答。上游 `server/http/common.js` 里的 `sendJson` 做的是同一件事（多了安全响应头，那些由
 * 请求入口统一设）；这里自带一份，是为了这个文件**零本地依赖**：拷到任何地方都能单独跑、单独测。
 */
function sendJson(req, res, status, obj) {
  const body = Buffer.from(JSON.stringify(obj));
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Content-Length': body.length, 'Cache-Control': 'no-store' });
  res.end(req.method === 'HEAD' ? undefined : body);
}
/** 保留多少天的日报（data/bot-daily/*.json）。 */
export const KEEP_DAYS = 30;
/** /daily 里最多回放多少局（更早的截断，聚合数字不受影响）。 */
export const MAX_DAILY_MATCHES = 500;
/** 一个榜最多留多少行。 */
export const MAX_BOARD_ROWS = 200;
/** 聚合写盘的合并窗口。 */
const SAVE_MS = 1000;

/** 训练/入门协议：不计称号、不计胜率（上游 leaderboard 同规则）。 */
const isTraining = (msg) => msg?.difficulty === 'TRAINING'
  || (typeof msg?.modeId === 'string' && msg.modeId.startsWith('mode_training'));

/** 本机时区的 YYYY-MM-DD（与上游 trafficDays.js 的 dayKey 同口径）。 */
function dayKey(ms = Date.now()) {
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

const isDayKey = (s) => typeof s === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(s) && !Number.isNaN(Date.parse(s));

/** 邮箱打码：`player@example.com` → `p****@example.com`。 */
function maskEmail(email) {
  const at = String(email || '').indexOf('@');
  if (at <= 0) return '***';
  const head = String(email).slice(0, at);
  const keep = head.slice(0, 1);
  return `${keep}****${String(email).slice(at)}`;
}

/** 常量时间比较（长度不同直接不等，避免 timingSafeEqual 抛异常）。 */
function tokenEquals(a, b) {
  const x = Buffer.from(String(a || ''), 'utf8');
  const y = Buffer.from(String(b || ''), 'utf8');
  return x.length === y.length && x.length > 0 && crypto.timingSafeEqual(x, y);
}

/** 写文件：先写 .tmp 再 rename（和 accounts.json / leaderboard.json 一样的原子写）。 */
function writeJsonAtomic(file, doc) {
  const tmp = `${file}.tmp`;
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(tmp, JSON.stringify(doc, null, 2) + '\n');
  fs.renameSync(tmp, file);
}

function readJson(file) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return null; }
}

/** 一个称号榜的内存态：id → 每个 holder 的聚合。 */
const BOARD_IDS = ['comment_1', 'comment_3', 'comment_6', 'comment_4', 'worst'];
const BOARD_META = {
  comment_1: { name: '卫戍之星', note: '对敌方领袖造成伤害最高（仅胜利时）', kind: 'title' },
  comment_3: { name: '坚若磐石', note: '目标生命值损失最少', kind: 'title' },
  comment_6: { name: '挥金如土', note: '消耗资金最多', kind: 'title' },
  comment_4: { name: '精英云集', note: '晋升精锐次数最多', kind: 'title' },
  worst: { name: '卫戍之区', note: '漏怪最多', kind: 'leaks' },
};

/**
 * 建一个 Bridge 服务。
 *
 * @param {{
 *   lobby: { stats?: Function, rooms?: Map<string, any>, now?: Function },
 *   registry: { byPlayerId: Map<string, any>, size: number },
 *   dataDir: string,                             // 聚合落盘目录（**不要**放在被静态服务的 data/ 下）
 *   token?: string | null,                       // BOT_TOKEN
 *   tokenFile?: string | null,                   // 每次校验重读的令牌文件（轮换用）
 *   accounts?: object | null,                    // 可选：{ requestCode(email), verifyCode(email, code), lookup(email) }
 *   maintain?: (() => { mode?: string, reason?: string | null, until?: number | null }) | null,
 *   prefix?: string, tz?: string, log?: object, now?: () => number,
 * }} opts
 */
export function createBotBridge({
  lobby, registry, dataDir, token = process.env.BOT_TOKEN || null, tokenFile = process.env.BOT_TOKEN_FILE || null,
  accounts = null, maintain = null, prefix = BRIDGE_PREFIX, tz = process.env.TZ || 'Asia/Shanghai',
  log = console, now = Date.now,
} = {}) {
  if (!lobby || !registry) throw new Error('createBotBridge needs { lobby, registry }');
  if (!dataDir) throw new Error('createBotBridge needs { dataDir }');

  const dailyDir = path.join(dataDir, 'bot-daily');
  const playersFile = path.join(dataDir, 'bot-players.json');
  const base = prefix.endsWith('/') ? prefix.slice(0, -1) : prefix;

  /** @type {Map<string, {date: string, summary: any, boards: Map<string, Map<string, any>>, players: Map<string, any>, matches: any[], truncated: boolean}>} */
  const days = new Map();
  /** @type {Map<string, any>} playerId → 累计 */
  const totals = new Map();
  /** @type {Map<string, {email: string, expiresAt: number, tries: number}>} requestId → 待校验 */
  const challenges = new Map();
  /** 粗限流：`${ip}:${route}` → [windowStart, count] */
  const buckets = new Map();

  let dirtyDays = new Set();
  let playersDirty = false;
  let saveTimer = null;

  // ---- 落盘 --------------------------------------------------------------------------------------

  const emptySummary = () => ({
    matches: 0, finished: 0, victories: 0, defeats: 0, winRate: 0, players: 0, humans: 0,
    goldSpent: 0, leaks: 0, rounds: 0, durationMs: 0, trainingMatches: 0,
    byDifficulty: {}, byMode: {}, _people: new Set(),
  });

  const emptyDay = (date) => ({
    date, summary: emptySummary(), boards: new Map(), players: new Map(), matches: [], truncated: false,
    /** 当天已计入的对局 id（去重：重连/重放导致的重复上报不能重复计数） */
    seen: new Set(),
  });

  const dayOf = (date) => {
    let day = days.get(date);
    if (!day) { day = emptyDay(date); days.set(date, day); }
    return day;
  };

  /** 从文件恢复一天（没有就当天新建）。 */
  function loadDay(date) {
    const doc = readJson(path.join(dailyDir, `${date}.json`));
    if (!doc || doc.version !== 1 || !doc.summary) return dayOf(date);
    const day = emptyDay(date);
    Object.assign(day.summary, doc.summary);
    day.summary._people = new Set(Array.isArray(doc.summary.people) ? doc.summary.people : []);
    day.matches = Array.isArray(doc.matches) ? doc.matches : [];
    day.truncated = !!doc.matchesTruncated;
    day.seen = new Set(Array.isArray(doc.seenIds) && doc.seenIds.length ? doc.seenIds : day.matches.map((m) => m.id));
    for (const id of BOARD_IDS) {
      const board = new Map();
      const rows = doc.titles?.[id]?.holders;
      if (Array.isArray(rows)) for (const row of rows) if (row?.playerId) board.set(row.playerId, row);
      day.boards.set(id, board);
    }
    for (const p of Array.isArray(doc.players) ? doc.players : []) if (p?.playerId) day.players.set(p.playerId, p.stats || {});
    days.set(date, day);
    return day;
  }

  function loadTotals() {
    const doc = readJson(playersFile);
    if (!doc || doc.version !== 1 || !doc.players) return;
    for (const [playerId, row] of Object.entries(doc.players)) totals.set(playerId, row);
  }

  function pruneDays() {
    const cutoff = now() - KEEP_DAYS * 86400000;
    for (const date of days.keys()) if (Date.parse(`${date}T23:59:59`) < cutoff) days.delete(date);
    let files = [];
    try { files = fs.readdirSync(dailyDir); } catch { return; }
    for (const name of files) {
      const date = name.replace(/\.json$/, '');
      if (isDayKey(date) && Date.parse(`${date}T23:59:59`) < cutoff) {
        try { fs.unlinkSync(path.join(dailyDir, name)); } catch { /* 删不掉就算了 */ }
      }
    }
  }

  function persist() {
    saveTimer = null;
    for (const date of dirtyDays) {
      const day = days.get(date);
      if (!day) continue;
      const titles = {};
      for (const [id, board] of day.boards) {
        const rows = rank(board, id);
        titles[id] = { id, ...BOARD_META[id], total: rows.length, holders: rows.slice(0, MAX_BOARD_ROWS) };
      }
      const summary = { ...day.summary };
      summary.people = [...summary._people];
      summary.winRate = summary.matches ? summary.victories / summary.matches : 0;
      delete summary._people;
      try {
        writeJsonAtomic(path.join(dailyDir, `${date}.json`), {
          version: 1, date, tz, generatedAt: now(), summary, titles,
          players: [...day.players].map(([playerId, stats]) => ({ playerId, name: stats.name || playerId, stats })),
          matches: day.matches, matchesTruncated: day.truncated, seenIds: [...day.seen],
        });
      } catch (e) {
        log.warn?.(`[bot] 写 ${date} 日报失败: ${e?.message}`);
      }
    }
    dirtyDays = new Set();
    if (playersDirty) {
      playersDirty = false;
      try {
        writeJsonAtomic(playersFile, { version: 1, savedAt: now(), players: Object.fromEntries(totals) });
      } catch (e) {
        log.warn?.(`[bot] 写累计战绩失败: ${e?.message}`);
      }
    }
  }

  function scheduleSave(date) {
    dirtyDays.add(date);
    if (saveTimer) return;
    saveTimer = setTimeout(persist, SAVE_MS);
    saveTimer.unref?.();
  }

  // ---- 记账 --------------------------------------------------------------------------------------

  /** 一个 holder 行的空值（称号榜与漏怪榜共用字段并集，与上游 leaderboard.json 同风格）。 */
  const emptyHolder = (playerId, name) => ({
    playerId, name, count: 0, victories: 0, bestRounds: 0, leaks: 0, best: 0, matches: 0, lastAt: 0,
  });

  const holderOf = (board, playerId, name) => {
    let row = board.get(playerId);
    if (!row) { row = emptyHolder(playerId, name); board.set(playerId, row); }
    row.name = name || row.name;
    return row;
  };

  /** 一个榜的排序行（称号按 count，漏怪按 leaks；并列按 lastAt 新在前）。 */
  function rank(board, id) {
    const isLeaks = BOARD_META[id]?.kind === 'leaks';
    const rows = [...board.values()].filter((r) => (isLeaks ? r.leaks > 0 : r.count > 0));
    rows.sort((a, b) => (isLeaks ? b.leaks - a.leaks : b.count - a.count) || b.lastAt - a.lastAt
      || (a.playerId < b.playerId ? -1 : 1));
    return rows.map((r, i) => ({ rank: i + 1, ...r }));
  }

  /**
   * 记一局结束（契约 §6.2）。在 lobby 的 matchSend / matchBroadcast 里对 `msg.t === 'm.result'` 调一次即可
   * （同局重复调用只会覆盖当天已记的那局 —— 用 `roomCode + matchCount` 做 id 去重）。
   * @param {any} room @param {any} msg
   */
  function recordResult(room, msg) {
    if (!msg || !Array.isArray(msg.players)) return false;
    const roomCode = typeof room?.code === 'string' ? room.code : null;
    const id = roomCode && Number.isFinite(room?.matchCount) ? `${roomCode}-${room.matchCount}` : `${roomCode || 'X'}-${now()}`;
    const at = now();
    const date = dayKey(at);
    const day = loadDay(date);

    // 幂等：同一局只计一次（lobby 的重连重放、重复的 matchSend 都可能喂同一份 m.result）
    if (day.seen.has(id)) return false;
    day.seen.add(id);
    if (day.seen.size > 500) day.seen.delete(day.seen.values().next().value);

    if (isTraining(msg)) {
      day.summary.trainingMatches += 1;
      scheduleSave(date);
      return true;
    }

    const players = msg.players.filter((p) => p && typeof p === 'object');
    const humans = players.filter((p) => !p.isBot);
    const signedIn = humans.filter((p) => typeof p.playerId === 'string' && p.playerId.startsWith('a_'));
    const mode = room?.mode === 'solo' || room?.mode === 'coop' ? room.mode : (humans.length >= 2 ? 'coop' : 'solo');
    const difficulty = typeof msg.difficulty === 'string' ? msg.difficulty : 'UNKNOWN';
    const rounds = Number.isFinite(msg.roundsPassed) ? Math.max(0, Math.round(msg.roundsPassed)) : 0;
    const duration = Number.isFinite(msg.durationMs) ? Math.max(0, Math.round(msg.durationMs)) : 0;

    const summary = day.summary;
    summary.matches += 1;
    summary.finished += 1;
    if (msg.victory) summary.victories += 1; else summary.defeats += 1;
    summary.humans += humans.length;
    summary.rounds += rounds;
    summary.durationMs += duration;
    for (const p of humans) summary._people.add(typeof p.playerId === 'string' && p.playerId ? p.playerId : `name:${p.name ?? '?'}`);
    const diff = (summary.byDifficulty[difficulty] ??= { matches: 0, victories: 0 });
    diff.matches += 1;
    if (msg.victory) diff.victories += 1;
    const modeRow = (summary.byMode[mode] ??= { matches: 0, victories: 0 });
    modeRow.matches += 1;
    if (msg.victory) modeRow.victories += 1;

    for (const id2 of BOARD_IDS) if (!day.boards.has(id2)) day.boards.set(id2, new Map());
    const worst = day.boards.get('worst');

    const rows = [];
    for (const p of players) {
      const isAccount = typeof p.playerId === 'string' && p.playerId.startsWith('a_');
      const stats = p.stats || {};
      const goldSpent = Number.isFinite(stats.gold) ? Math.max(0, Math.round(stats.gold)) : 0;
      const leaks = Number.isFinite(stats.leaks) ? Math.max(0, Math.round(stats.leaks)) : 0;
      const titleId = typeof p.title?.id === 'string' ? p.title.id : null;
      rows.push({
        playerId: typeof p.playerId === 'string' ? p.playerId : null,
        name: typeof p.name === 'string' ? p.name : '?',
        isBot: !!p.isBot, victory: !!p.victory,
        roundsPassed: Number.isFinite(p.roundsPassed) ? Math.max(0, Math.round(p.roundsPassed)) : 0,
        goldSpent, leaks,
        title: titleId ? { id: titleId, name: typeof p.title?.name === 'string' ? p.title.name : titleId } : null,
      });
      if (p.isBot) continue;
      summary.goldSpent += goldSpent;
      summary.leaks += leaks;
      // 只有登录账号进榜与玩家聚合：游客的会话 id 不跨设备，记了也没法查（契约 §1.8 / §4.2）
      if (!isAccount) continue;
      const name = typeof p.name === 'string' && p.name ? p.name : p.playerId;

      const dp = day.players.get(p.playerId) ?? { name, matches: 0, victories: 0, goldSpent: 0, leaks: 0, bestRounds: 0, titles: {}, firstAt: at, lastAt: at };
      dp.name = name;
      dp.matches += 1;
      if (p.victory) dp.victories += 1;
      dp.goldSpent += goldSpent;
      dp.leaks += leaks;
      if (Number.isFinite(p.roundsPassed)) dp.bestRounds = Math.max(dp.bestRounds, Math.round(p.roundsPassed));
      dp.lastAt = at;
      if (titleId) dp.titles[titleId] = (dp.titles[titleId] || 0) + 1;
      day.players.set(p.playerId, dp);

      if (titleId && day.boards.has(titleId)) {
        const h = holderOf(day.boards.get(titleId), p.playerId, name);
        h.count += 1;
        if (p.victory) h.victories += 1;
        if (Number.isFinite(p.roundsPassed)) h.bestRounds = Math.max(h.bestRounds, Math.round(p.roundsPassed));
        h.lastAt = at;
      }
      const w = holderOf(worst, p.playerId, name);
      w.leaks += leaks;
      w.best = Math.max(w.best, leaks);
      w.matches += 1;
      w.lastAt = at;

      const tp = totals.get(p.playerId) ?? { name, matches: 0, victories: 0, goldSpent: 0, leaks: 0, bestRounds: 0, titles: {}, firstAt: at, lastAt: at };
      tp.name = name;
      tp.matches += 1;
      if (p.victory) tp.victories += 1;
      tp.goldSpent += goldSpent;
      tp.leaks += leaks;
      if (Number.isFinite(p.roundsPassed)) tp.bestRounds = Math.max(tp.bestRounds, Math.round(p.roundsPassed));
      tp.lastAt = at;
      if (titleId) tp.titles[titleId] = (tp.titles[titleId] || 0) + 1;
      totals.set(p.playerId, tp);
      playersDirty = true;
    }

    // 同一天同一局的重复上报已在入口按 id 拦掉；这里只负责把这一局放进回放列表
    const record = { id, at, roomCode, mode, difficulty, victory: !!msg.victory, roundsPassed: rounds, durationMs: duration, players: rows };
    day.matches.unshift(record);
    if (day.matches.length > MAX_DAILY_MATCHES) { day.matches.length = MAX_DAILY_MATCHES; day.truncated = true; }

    scheduleSave(date);
    return true;
  }

  // ---- 读模型 ------------------------------------------------------------------------------------

  /** /daily 的响应体（从内存/文件里的当天聚合拼出来）。 */
  function dailyPayload(date) {
    const day = loadDay(date);
    const titles = {};
    for (const id of BOARD_IDS) {
      const board = day.boards.get(id) ?? new Map();
      titles[id] = { id, ...BOARD_META[id], total: board.size, holders: rank(board, id).slice(0, 20) };
    }
    const s = day.summary;
    const summary = {
      matches: s.matches, finished: s.finished, victories: s.victories, defeats: s.defeats,
      winRate: s.matches ? s.victories / s.matches : 0,
      players: s._people instanceof Set ? s._people.size : (Array.isArray(s.people) ? s.people.length : 0),
      humans: s.humans, goldSpent: s.goldSpent, leaks: s.leaks, rounds: s.rounds, durationMs: s.durationMs,
      trainingMatches: s.trainingMatches, byDifficulty: s.byDifficulty, byMode: s.byMode,
    };
    const players = [...day.players].map(([playerId, p]) => ({
      playerId, name: p.name || playerId,
      stats: { ...p, winRate: p.matches ? p.victories / p.matches : 0 },
    })).sort((a, b) => b.stats.matches - a.stats.matches).slice(0, 200);
    return { ok: true, protocol: BRIDGE_PROTOCOL, date, tz, serverNow: now(), generatedAt: now(), summary, titles, players, matches: day.matches, matchesTruncated: day.truncated };
  }

  /** 一个玩家的累计/今日战绩 + 最近对局（最近 7 天的日报文件里找）。 */
  function playerPayload(playerId, name) {
    let id = playerId;
    let display = name || '';
    if (!id && name) {
      for (const [pid, row] of totals) if (row.name === name) { id = pid; break; }
      if (!id) {
        const today = loadDay(dayKey(now()));
        for (const [pid, row] of today.players) if (row.name === name) { id = pid; break; }
      }
      if (!id) return { error: 'player_not_found' };
    }
    const total = totals.get(id);
    const today = loadDay(dayKey(now())).players.get(id);
    // 明确给了 playerId 就一定有答案（哪怕没打过）：只有「按昵称找」找不到才算 404
    display = display || total?.name || today?.name || id;

    const recent = [];
    for (let i = 0; i < 7 && recent.length < 20; i += 1) {
      const date = dayKey(now() - i * 86400000);
      const day = days.get(date) ?? loadDay(date);
      for (const m of day.matches) {
        if (recent.length >= 20) break;
        if (m.players.some((p) => p.playerId === id)) recent.push(m);
      }
    }
    const shape = (row) => row ? { ...row, winRate: row.matches ? row.victories / row.matches : 0 } : {
      matches: 0, victories: 0, winRate: 0, goldSpent: 0, leaks: 0, bestRounds: 0, titles: {}, firstAt: 0, lastAt: 0,
    };
    return {
      ok: true, protocol: BRIDGE_PROTOCOL, serverNow: now(),
      player: { playerId: id, name: display, bound: !!total || id.startsWith('a_') },
      total: shape(total), today: shape(today), recent,
    };
  }

  /** /status 的响应体：会话表 + 房间表（都在内存里，不扫盘）。 */
  function statusPayload() {
    const lobbyStats = typeof lobby.stats === 'function' ? lobby.stats() : {};
    const rooms = lobby.rooms instanceof Map ? [...lobby.rooms.values()] : [];
    const seatOf = new Map();
    for (const room of rooms) {
      for (const seat of room.seats || []) {
        if (!seat) continue;
        seatOf.set(seat.playerId, { room, seat, spectating: false });
      }
      for (const sp of room.spectators || []) seatOf.set(sp.playerId, { room, seat: null, spectating: true });
    }

    const players = [];
    for (const session of registry.byPlayerId.values()) {
      if (!session.connected) continue; // 断线等重连的不算「在线」
      const hit = seatOf.get(session.playerId) || null;
      const room = hit?.room || (session.roomCode && lobby.rooms?.get?.(session.roomCode)) || null;
      const inMatch = !!room?.match;
      const state = room ? (inMatch ? 'match' : 'waiting') : 'idle';
      players.push({
        playerId: session.playerId,
        account: typeof session.playerId === 'string' && session.playerId.startsWith('a_') ? session.playerId : null,
        name: session.name,
        state,
        roomCode: room?.code ?? null,
        seat: hit?.seat ?? null,
        isHost: !!room && room.hostId === session.playerId,
        spectating: !!hit?.spectating,
        mode: room?.mode ?? null,
        difficulty: room?.difficulty ?? null,
        joinedAt: session.joinedAt ?? 0,
      });
    }
    players.sort((a, b) => a.joinedAt - b.joinedAt);

    const matches = [];
    for (const room of rooms) {
      if (!room.match) continue;
      matches.push({
        roomCode: room.code,
        mode: room.mode ?? null,
        difficulty: room.difficulty ?? null,
        round: room.round ?? room.match?.round ?? 0,
        bossRound: room.bossRound ?? 0,
        startedAt: room.startedAt ?? 0,
        players: (room.seats || []).filter(Boolean).map((s) => ({
          playerId: s.playerId, name: s.name, seat: s.seat, isBot: !!s.isBot,
          alive: s.alive !== false, lp: Number.isFinite(s.lp) ? s.lp : 0, spectating: false,
        })),
      });
    }

    const maintenance = maintain ? (maintain() || { mode: 'off' }) : { mode: 'off', reason: null, until: null };
    return {
      ok: true, protocol: BRIDGE_PROTOCOL, app: null, serverNow: now(),
      maintenance: { mode: maintenance.mode || 'off', reason: maintenance.reason ?? null, until: maintenance.until ?? null },
      counts: {
        online: players.length,
        inMatch: players.filter((p) => p.state === 'match').length,
        waiting: players.filter((p) => p.state === 'waiting').length,
        inHall: Number.isFinite(lobbyStats.hallMembers) ? lobbyStats.hallMembers : 0,
        spectators: players.filter((p) => p.spectating).length,
        rooms: Number.isFinite(lobbyStats.rooms) ? lobbyStats.rooms : rooms.length,
        matches: matches.length,
        accounts: accounts && typeof accounts.count === 'function' ? accounts.count() : undefined,
      },
      players: players.slice(0, 200),
      matches: matches.slice(0, 50),
      truncated: players.length > 200 || matches.length > 50,
    };
  }

  // ---- 鉴权 / 限流 -------------------------------------------------------------------------------

  function currentToken() {
    if (tokenFile) {
      try {
        const raw = fs.readFileSync(tokenFile, 'utf8').trim();
        if (raw) return raw;
      } catch { /* 读不到就用环境里的 */ }
    }
    return token;
  }

  function authorized(req) {
    const want = currentToken();
    if (!want) return false;
    const header = req.headers?.authorization;
    if (typeof header === 'string' && /^Bearer\s+/i.test(header)) return tokenEquals(header.replace(/^Bearer\s+/i, '').trim(), want);
    const alt = req.headers?.['x-bot-token'];
    return typeof alt === 'string' && tokenEquals(alt.trim(), want);
  }

  /** 粗限流：窗口内每 IP 的请求数。 */
  function throttled(req, route, limit, windowMs = 60000) {
    const ip = req.socket?.remoteAddress || '?';
    const key = `${ip}|${route}`;
    const bucket = buckets.get(key);
    const at = now();
    if (!bucket || at - bucket[0] > windowMs) { buckets.set(key, [at, 1]); return false; }
    bucket[1] += 1;
    return bucket[1] > limit;
  }

  const fail = (req, res, status, error, message, extra = {}) =>
    sendJson(req, res, status, { ok: false, error, message, ...extra });

  function readJsonBody(req, limit = 4096) {
    return new Promise((resolve) => {
      let size = 0;
      const chunks = [];
      req.on('data', (c) => {
        size += c.length;
        if (size > limit) { resolve(null); req.destroy(); return; }
        chunks.push(c);
      });
      req.on('end', () => {
        try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}')); } catch { resolve(null); }
      });
      req.on('error', () => resolve(null));
    });
  }

  // ---- 绑定 --------------------------------------------------------------------------------------

  function capabilities() {
    const caps = ['status'];
    if (totals.size || days.size) caps.push('player', 'daily');
    else caps.push('player', 'daily'); // 聚合从第一局就开始写，能力始终声明
    if (accounts && typeof accounts.requestCode === 'function' && typeof accounts.verifyCode === 'function') caps.push('bind.email');
    if (maintain) caps.push('maintenance');
    return caps;
  }

  async function bindRequest(req, res, body) {
    if (!capabilities().includes('bind.email')) return fail(req, res, 400, 'accounts_disabled', '这台服务器没有开启账号系统');
    const email = typeof body?.email === 'string' ? body.email.trim().toLowerCase() : '';
    if (!email.includes('@')) return fail(req, res, 400, 'bad_request', '邮箱格式不对', { detail: 'email' });
    if (throttled(req, 'bind', 10, 3600000)) return fail(req, res, 429, 'too_many_requests', '发码太频繁了，稍后再试', { retryAfterMs: 600000 });
    const sent = await accounts.requestCode(email, { qq: body?.qq ?? null });
    if (!sent?.ok) {
      const code = sent?.error === 'too_many' ? 'too_many_requests' : (sent?.error || 'smtp_failed');
      return fail(req, res, code === 'smtp_failed' ? 502 : 400, code, '验证码发送失败');
    }
    const requestId = 'b_' + crypto.randomBytes(16).toString('hex');
    challenges.set(requestId, { email, expiresAt: now() + 10 * 60000, tries: 0 });
    return sendJson(req, res, 200, {
      ok: true, protocol: BRIDGE_PROTOCOL, requestId, maskedEmail: maskEmail(email),
      expiresAt: challenges.get(requestId).expiresAt, retryAfterMs: 60000,
    });
  }

  async function bindVerify(req, res, body) {
    if (!capabilities().includes('bind.email')) return fail(req, res, 400, 'accounts_disabled', '这台服务器没有开启账号系统');
    const requestId = typeof body?.requestId === 'string' ? body.requestId : '';
    const code = typeof body?.code === 'string' ? body.code.trim() : '';
    const challenge = challenges.get(requestId);
    if (!challenge || challenge.expiresAt <= now()) {
      challenges.delete(requestId);
      return fail(req, res, 404, 'request_not_found', '验证码请求已过期，请重新获取');
    }
    if (challenge.tries >= 5) {
      challenges.delete(requestId);
      return fail(req, res, 400, 'code_expired', '错误次数过多，请重新获取验证码');
    }
    const checked = await accounts.verifyCode(challenge.email, code);
    if (!checked?.ok) {
      challenge.tries += 1;
      return fail(req, res, 400, checked?.error === 'code_expired' ? 'code_expired' : 'code_invalid', '验证码不对');
    }
    const account = await accounts.lookup(challenge.email);
    if (!account?.playerId) return fail(req, res, 404, 'account_not_found', '这个邮箱没有注册过游戏账号');
    challenges.delete(requestId);
    return sendJson(req, res, 200, {
      ok: true, protocol: BRIDGE_PROTOCOL, playerId: account.playerId, name: account.name || '',
      email: maskEmail(challenge.email), boundAt: now(),
    });
  }

  // ---- HTTP --------------------------------------------------------------------------------------

  /**
   * 接管 `/api/bot/v1/*`。放在静态处理之前调用；返回 true 表示这个请求已经答完。
   * @returns {Promise<boolean>}
   */
  async function handleHttp(req, res, parts) {
    const rawPath = parts?.rawPath || '';
    if (rawPath !== base && !rawPath.startsWith(`${base}/`)) return false;
    const route = rawPath.slice(base.length) || '/';
    const method = (req.method || 'GET').toUpperCase();
    const query = new URLSearchParams(parts?.query || '');
    res.setHeader('X-Bridge-Protocol', String(BRIDGE_PROTOCOL));

    if (route === '/meta' && (method === 'GET' || method === 'HEAD')) {
      return sendJson(req, res, 200, {
        ok: true, protocol: BRIDGE_PROTOCOL, app: null, tz,
        accounts: capabilities().includes('bind.email'), auth: currentToken() ? 'on' : 'off',
        capabilities: capabilities(),
      }), true;
    }
    if (!currentToken()) return fail(req, res, 403, 'bot_disabled', '服务端没有配置 BOT_TOKEN'), true;
    if (!authorized(req)) return fail(req, res, 401, 'unauthorized', '令牌无效'), true;
    if (throttled(req, 'read', 120, 60000)) return fail(req, res, 429, 'too_many_requests', '请求过于频繁', { retryAfterMs: 60000 }), true;

    if (method === 'GET' || method === 'HEAD') {
      if (route === '/status') return sendJson(req, res, 200, statusPayload()), true;
      if (route === '/daily') {
        const raw = query.get('date');
        if (raw && !isDayKey(raw)) return fail(req, res, 400, 'bad_request', 'date 必须是 YYYY-MM-DD', { detail: 'date' }), true;
        const date = raw || dayKey(now());
        if (Date.parse(`${date}T00:00:00`) > Date.parse(`${dayKey(now())}T23:59:59`)) {
          return sendJson(req, res, 200, { ok: true, protocol: BRIDGE_PROTOCOL, date, tz, serverNow: now(), generatedAt: now(), summary: emptySummary(), titles: {}, players: [], matches: [], matchesTruncated: false }), true;
        }
        return sendJson(req, res, 200, dailyPayload(date)), true;
      }
      if (route === '/player') {
        const playerId = query.get('playerId') || undefined;
        const name = query.get('name') || undefined;
        if (!playerId && !name) return fail(req, res, 400, 'bad_request', '需要 playerId 或 name'), true;
        const payload = playerPayload(playerId, name);
        if (payload.error) return fail(req, res, 404, 'player_not_found', '没找到这个玩家'), true;
        return sendJson(req, res, 200, payload), true;
      }
      return fail(req, res, 404, 'not_found', '没有这个接口'), true;
    }

    if (method === 'POST' && (route === '/bind/request' || route === '/bind/verify')) {
      const body = await readJsonBody(req);
      if (!body) return fail(req, res, 400, 'bad_request', '请求体不是 JSON'), true;
      if (route === '/bind/request') return await bindRequest(req, res, body), true;
      return await bindVerify(req, res, body), true;
    }

    res.setHeader('Allow', 'GET, HEAD, POST');
    return fail(req, res, 405, 'method_not_allowed', '不支持这个请求方法'), true;
  }

  // ---- 生命周期 ----------------------------------------------------------------------------------

  function flush() { if (dirtyDays.size || playersDirty) persist(); }
  function dispose() {
    if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; }
    flush();
  }
  function stats() {
    return { days: days.size, players: totals.size, challenges: challenges.size };
  }

  loadTotals();
  pruneDays();
  loadDay(dayKey(now()));
  setInterval(() => {
    const at = now();
    for (const [id, c] of challenges) if (c.expiresAt <= at) challenges.delete(id);
    pruneDays();
  }, 3600000).unref?.();

  return { handleHttp, recordResult, flush, dispose, stats, dailyPayload, statusPayload, playerPayload, prefix: base };
}
