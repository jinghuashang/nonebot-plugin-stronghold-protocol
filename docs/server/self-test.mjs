// docs/server/self-test.mjs — 参考实现的自测（`node docs/server/self-test.mjs`）
//
// 它用一个假的 lobby/registry 跑真实的 recordResult + handleHttp，检查契约里最容易写错的地方：
// 「今日消耗资金 / 今日各称号」的聚合、训练局不计、同局重复上报不重复计数、权限与错误码。
// 服务端实现者可以把它当验收清单的可执行版本：自己的实现跑通同一组断言，就说明数据口径对上了。

import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { EventEmitter } from 'node:events';
import { createBotBridge, BRIDGE_PREFIX } from './stronghold-bot-bridge.mjs';

const TOKEN = 'test-token-0123456789';
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'bot-bridge-'));
const log = { info() {}, warn() {}, debug() {}, error: console.error };

// ---- 假的会话表 / 房间表（形状与上游 net.js / lobby.js 一致）------------------------------------

const session = (playerId, name, roomCode = null) => ({
  playerId, name, connected: true, disconnectedAt: null, roomCode, joinedAt: 1791800000000,
});

const registry = {
  size: 3,
  byPlayerId: new Map([
    ['a_0000000002', session('a_0000000002', '阿米娅', 'YFAF')],
    ['a_0000000003', session('a_0000000003', '玫兰莎', 'YFAF')],
    ['p_31c8f0a91b', session('p_31c8f0a91b', '游客', 'YFAF')],
  ]),
};

const room = {
  code: 'YFAF', mode: 'coop', difficulty: 'HARD', hostId: 'a_0000000002', matchCount: 5,
  match: { round: 7, bossRound: 14 }, round: 7, bossRound: 14, startedAt: 1791801700000,
  seats: [
    { seat: 0, playerId: 'a_0000000002', name: '阿米娅', isBot: false, alive: true, lp: 3 },
    { seat: 1, playerId: 'a_0000000003', name: '玫兰莎', isBot: false, alive: true, lp: 3 },
    { seat: 2, playerId: 'p_31c8f0a91b', name: '游客', isBot: false, alive: true, lp: 3 },
    { seat: 3, playerId: 'p_bot1', name: 'AI', isBot: true, alive: true, lp: 3 },
  ],
  spectators: [],
};

const lobby = {
  rooms: new Map([[room.code, room]]),
  stats: () => ({ rooms: 1, matches: 1, humans: 3, bots: 1, spectators: 0, hallMembers: 1 }),
};

// ---- 假的账号适配器（对应 §4.3/§4.4）------------------------------------------------------------

const CODES = new Map([['player@example.com', '123456']]);
const accounts = {
  count: () => 101,
  requestCode: async (email) => ({ ok: true, ttlSec: 600 }),
  verifyCode: async (email, code) => (CODES.get(email) === code ? { ok: true } : { ok: false, error: 'bad_code' }),
  lookup: async (email) => (email === 'player@example.com' ? { playerId: 'a_0000000001', name: '凯尔希' } : null),
};

const bridge = createBotBridge({ lobby, registry, dataDir: dir, token: TOKEN, accounts, log, now: () => 1791811511760 });

// ---- 假的 req / res -----------------------------------------------------------------------------

function fakeReq({ method = 'GET', url = BRIDGE_PREFIX, headers = {}, body = null } = {}) {
  const req = new EventEmitter();
  req.method = method;
  req.url = url;
  req.headers = headers;
  req.socket = { remoteAddress: '127.0.0.1' };
  req.destroy = () => {};
  setImmediate(() => {
    if (body != null) req.emit('data', Buffer.from(typeof body === 'string' ? body : JSON.stringify(body)));
    req.emit('end');
  });
  return req;
}

function fakeRes() {
  return {
    status: 0, headers: {}, setHeader(k, v) { this.headers[k] = v; },
    writeHead(status, headers) { this.status = status; Object.assign(this.headers, headers); },
    end(body) { this.body = body ? JSON.parse(body.toString('utf8')) : undefined; this.raw = body; },
  };
}

const call = async ({ method = 'GET', url = BRIDGE_PREFIX, headers = {}, body = null, auth = TOKEN } = {}) => {
  const sent = { ...headers };
  if (auth) sent.authorization = `Bearer ${auth}`;
  const req = fakeReq({ method, url, headers: sent, body });
  const res = fakeRes();
  const [rawPath, query = ''] = url.split('?');
  const handled = await bridge.handleHttp(req, res, { rawPath, query });
  return { handled, res, req };
};

// ---- 1) 开局前：空日报也算得出来 ----------------------------------------------------------------

assert.equal((await call({ url: `${BRIDGE_PREFIX}/daily` })).res.body.summary.matches, 0, '空日报');
assert.equal((await call({ url: `${BRIDGE_PREFIX}/daily`, auth: null })).res.status, 401, '没有令牌 → 401');
const meta = (await call({ url: `${BRIDGE_PREFIX}/meta`, auth: null })).res.body;
assert.equal(meta.ok, true, 'meta 免鉴权');
assert.ok(meta.capabilities.includes('bind.email'), 'meta 声明 bind.email');
assert.equal(meta.auth, 'on');

// ---- 2) 一局联机结束：聚合、称号、漏怪、消耗资金 ------------------------------------------------

const result = {
  t: 'm.result', victory: true, roundsPassed: 14, hiddenReached: true, hiddenCleared: false,
  reason: 'clear', modeId: 'mode_multi_hard', difficulty: 'HARD', durationMs: 1511829,
  players: [
    { playerId: 'a_0000000002', seat: 0, name: '阿米娅', isBot: false, alive: true, victory: true, roundsPassed: 15,
      stats: { gold: 320, leaks: 0, merges: 5, bossDamage: 9876, lpLost: 1 }, title: { id: 'comment_1', name: '卫戍之星' } },
    { playerId: 'a_0000000003', seat: 1, name: '玫兰莎', isBot: false, alive: true, victory: true, roundsPassed: 14,
      stats: { gold: 210, leaks: 12, merges: 2, bossDamage: 4000, lpLost: 5 }, title: { id: 'comment_6', name: '挥金如土' } },
    { playerId: 'p_31c8f0a91b', seat: 2, name: '游客', isBot: false, alive: true, victory: true, roundsPassed: 14,
      stats: { gold: 100, leaks: 3, merges: 1, bossDamage: 123, lpLost: 9 }, title: null },
    { playerId: 'p_bot1', seat: 3, name: 'AI', isBot: true, alive: true, victory: true, roundsPassed: 14,
      stats: { gold: 50, leaks: 0 }, title: null },
  ],
};

assert.equal(bridge.recordResult(room, result), true, 'recordResult 接受这一局');
bridge.recordResult(room, result); // 重连/重放导致的重复上报：必须不重复计数（靠 roomCode-matchCount 去重）

const training = { ...result, difficulty: 'TRAINING', modeId: 'mode_training_1' };
bridge.recordResult({ ...room, matchCount: 6 }, training); // 训练局：只进 trainingMatches（另一局，另一个 id）

const daily = (await call({ url: `${BRIDGE_PREFIX}/daily` })).res.body;
assert.equal(daily.summary.matches, 1, '同局重复上报只算一局');
assert.equal(daily.summary.victories, 1);
assert.equal(daily.summary.winRate, 1);
assert.equal(daily.summary.trainingMatches, 1, '训练局单独计');
assert.equal(daily.summary.humans, 3, '人类席位（不含 AI）');
assert.equal(daily.summary.players, 3, '去重人数：2 个账号 + 1 个游客');
assert.equal(daily.summary.goldSpent, 630, '今日消耗资金 = 320+210+100（AI 与游客的席位都算，但游客不进榜）');
assert.equal(daily.summary.leaks, 15, '今日漏怪 = 0+12+3');

assert.deepEqual(daily.titles.comment_1.holders.map((h) => [h.name, h.count]), [['阿米娅', 1]], '卫戍之星');
assert.deepEqual(daily.titles.comment_6.holders.map((h) => [h.name, h.count]), [['玫兰莎', 1]], '挥金如土');
assert.equal(daily.titles.comment_3.holders.length, 0, '当天没人拿坚若磐石 → 空榜也要在');
assert.equal(daily.titles.comment_3.name, '坚若磐石', '榜名由服务端补');
assert.deepEqual(daily.titles.worst.holders.map((h) => [h.name, h.leaks, h.best]), [['玫兰莎', 12, 12]], '卫戍之区按漏怪');
assert.equal(daily.titles.worst.kind, 'leaks');
assert.deepEqual(daily.players.map((p) => p.playerId).sort(), ['a_0000000002', 'a_0000000003'], '只有登录账号进 players');
assert.equal(daily.players.find((p) => p.playerId === 'a_0000000002').stats.goldSpent, 320);
assert.equal(daily.matches.length, 1);
assert.equal(daily.matches[0].players.length, 4, 'matches[].players 含 AI 与游客（展示用）');

// ---- 3) /status --------------------------------------------------------------------------------

const status = (await call({ url: `${BRIDGE_PREFIX}/status` })).res.body;
assert.equal(status.counts.online, 3);
assert.equal(status.counts.matches, 1);
assert.equal(status.counts.rooms, 1);
assert.equal(status.counts.spectators, 0);
assert.deepEqual(status.players.map((p) => [p.name, p.state]), [['阿米娅', 'match'], ['玫兰莎', 'match'], ['游客', 'match']]);
assert.equal(status.players[0].account, 'a_0000000002');
assert.equal(status.players.find((p) => p.name === '游客').account, null, '游客没有 account');
assert.equal(status.players[0].isHost, true);
assert.equal(status.matches[0].roomCode, 'YFAF');
assert.equal(status.matches[0].players.length, 4);
assert.equal(status.maintenance.mode, 'off');

// ---- 4) /player：累计 + 今日 + 最近对局 ----------------------------------------------------------

const me = (await call({ url: `${BRIDGE_PREFIX}/player?playerId=a_0000000002` })).res.body;
assert.equal(me.player.name, '阿米娅');
assert.equal(me.player.bound, true);
assert.equal(me.total.matches, 1);
assert.equal(me.total.goldSpent, 320);
assert.equal(me.total.titles.comment_1, 1);
assert.equal(me.today.matches, 1);
assert.equal(me.recent.length, 1);
assert.equal(me.recent[0].roomCode, 'YFAF');
assert.equal(me.recent[0].players.find((p) => p.playerId === 'a_0000000002').goldSpent, 320);

const byName = (await call({ url: `${BRIDGE_PREFIX}/player?name=玫兰莎` })).res.body;
assert.equal(byName.player.playerId, 'a_0000000003');

// 明确给了 playerId 就必须有答案（绑定了但没打过的新账号不能看起来像「查无此人」）
const fresh = await call({ url: `${BRIDGE_PREFIX}/player?playerId=a_0000000001` });
assert.equal(fresh.res.status, 200);
assert.equal(fresh.res.body.player.bound, true);
assert.equal(fresh.res.body.total.matches, 0);
assert.equal(fresh.res.body.recent.length, 0);

// ---- 5) 错误码 ---------------------------------------------------------------------------------

let r = await call({ url: `${BRIDGE_PREFIX}/player?name=查无此人` });
assert.equal(r.res.status, 404);
assert.equal(r.res.body.error, 'player_not_found');

r = await call({ url: `${BRIDGE_PREFIX}/daily?date=昨天` });
assert.equal(r.res.status, 400);
assert.equal(r.res.body.error, 'bad_request');

r = await call({ url: `${BRIDGE_PREFIX}/daily?date=2099-01-01` });
assert.equal(r.res.body.summary.matches, 0, '未来日期 → 空日报');

r = await call({ url: `${BRIDGE_PREFIX}/status`, auth: 'wrong-token-xxxxxxxx' });
assert.equal(r.res.status, 401);
assert.equal(r.res.body.error, 'unauthorized');

r = await call({ url: `${BRIDGE_PREFIX}/nope` });
assert.equal(r.res.status, 404);

r = await call({ url: '/healthz' });
assert.equal(r.handled, false, '不属于 Bridge 的路径必须交回上游');

// ---- 6) 绑定：发码 → 校验 -----------------------------------------------------------------------

r = await call({ method: 'POST', url: `${BRIDGE_PREFIX}/bind/request`, body: { email: 'player@example.com', qq: '123456789' } });
assert.equal(r.res.status, 200);
assert.equal(r.res.body.maskedEmail, 'p****@example.com', '邮箱必须打码');
const requestId = r.res.body.requestId;
assert.ok(JSON.stringify(r.res.body).includes('player@example.com') === false, '响应里不能出现邮箱明文');

r = await call({ method: 'POST', url: `${BRIDGE_PREFIX}/bind/verify`, body: { requestId, code: '000000' } });
assert.equal(r.res.body.error, 'code_invalid');

r = await call({ method: 'POST', url: `${BRIDGE_PREFIX}/bind/verify`, body: { requestId, code: '123456' } });
assert.equal(r.res.status, 200);
assert.equal(r.res.body.playerId, 'a_0000000001');
assert.equal(r.res.body.name, '凯尔希');

r = await call({ method: 'POST', url: `${BRIDGE_PREFIX}/bind/verify`, body: { requestId, code: '123456' } });
assert.equal(r.res.body.error, 'request_not_found', '同一个 requestId 只能成功一次');

r = await call({ method: 'DELETE', url: `${BRIDGE_PREFIX}/status` });
assert.equal(r.res.status, 405);

// ---- 7) 落盘 ------------------------------------------------------------------------------------

bridge.flush();
const dayFile = path.join(dir, 'bot-daily', `${daily.date}.json`);
assert.ok(fs.existsSync(dayFile), `写下了 ${dayFile}`);
const onDisk = JSON.parse(fs.readFileSync(dayFile, 'utf8'));
assert.equal(onDisk.summary.goldSpent, 630);
assert.deepEqual(onDisk.summary.people, ['a_0000000002', 'a_0000000003', 'p_31c8f0a91b'], '去重人数落盘用 people 数组');
assert.equal(onDisk.titles.comment_1.holders[0].name, '阿米娅');
assert.ok(fs.existsSync(path.join(dir, 'bot-players.json')), '累计战绩落盘');
assert.ok(!fs.existsSync(`${dayFile}.tmp`), '不留 .tmp 残渣');

// ---- 8) 概览 -----------------------------------------------------------------------------------

const counts = bridge.stats();
console.log(`ok · 参考实现自测通过（${counts.days} 天 / ${counts.players} 个账号聚合，落盘于 ${dir}）`);
