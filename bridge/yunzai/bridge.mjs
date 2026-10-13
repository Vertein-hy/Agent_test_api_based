// Use upstream Yunzai Runtime and miao-plugin functions without starting QQ adapters.
import http from 'node:http'
import fs from 'node:fs/promises'
import { createHash } from 'node:crypto'
import { authorized, validate, collector } from './protocol.mjs'

// Upstream node-fetch is a native-fetch alias; its legacy timeout option is ignored.
// Enforce a real abort signal here without modifying upstream files.
const nativeFetch = global.fetch
global.fetch = async (address, options = {}) => {
  const url = new URL(address)
  if (url.protocol !== 'https:' || url.hostname !== 'enka.network' ||
      !/^\/api\/uid\/(?:[1-9][0-9]{8}|18[0-9]{8})\/?$/.test(url.pathname))
    throw new Error('unsupported_public_endpoint')
  url.pathname = url.pathname.replace(/\/$/, '') // Enka normalizes trailing slashes via 308.
  return nativeFetch(url, {...options, redirect: 'error', signal: AbortSignal.timeout(20000),
    headers: {...options.headers, 'User-Agent': 'QQ-Yunzai-Bridge/1.0 (miao-plugin public showcase)'}})
}

global.start_type = 'external'
global.Bot = new (await import('../lib/bot.js')).default()
await (await import('../lib/config/init.js')).default()
const Runtime = (await import('../lib/plugins/runtime.js')).default
const { Cfg } = await import('../plugins/miao-plugin/components/index.js')
const { Character, Player } = await import('../plugins/miao-plugin/models/index.js')
const ProfileDetail = (await import('../plugins/miao-plugin/apps/profile/ProfileDetail.js')).default
const ProfileList = (await import('../plugins/miao-plugin/apps/profile/ProfileList.js')).default
const { profileHelp } = await import('../plugins/miao-plugin/apps/profile/ProfileCommon.js')
Cfg.set('profileServer', 2) // Public Enka service; no cookies or private API tokens.
const token = (await fs.readFile(process.env.YUNZAI_TOKEN_FILE, 'utf8')).trim()
if (token.length < 32) throw new Error('Missing bridge token')
let active = false
const cache = new Map()

async function execute(input) {
  const out = collector('/app/plugins/miao-plugin/resources')
  const pendingReplies = []
  // No QQ account is connected. Even upstream e.reply calls only collect results.
  const e = {
    self_id: 'bridge', user_id: parseInt(input.actor.slice(0, 12), 16),
    game: 'gs', isSr: false, isGs: true, isPrivate: true, isGroup: false,
    isMaster: false, uid: input.uid, sender: {}, reply: message => {
      const pending = out.reply(message)
      pendingReplies.push(pending)
      return pending
    },
    msg: '', original_msg: '',
  }
  e.runtime = new Runtime(e)
  e.runtime.getUid = async () => e.uid
  e.runtime.getMysInfo = async () => false
  e.runtime.getMysApi = async () => false
  try {
    if (input.operation !== 'help') {
      const player = Player.create(e)
      const cooldownKey = 'addon:fetch:' + input.uid
      if (!player.hasProfile || Date.now() - (player._profile || 0) >= 300000) {
        if (await redis.set(cooldownKey, '1', {NX: true, EX: 300})) {
          await player.refreshProfile(2, false)
        } else if (!player.hasProfile) {
          await out.reply('公开展柜查询正在冷却，请5分钟后重试，并确认游戏展柜已开启显示详情。')
        }
        if (!player.hasProfile) {
          if (!out.texts.length) await out.reply('未获取到公开角色数据，请确认 UID 和展柜详情设置；5分钟后可重试。')
          return {ok: true, operation: input.operation, uid: input.uid, texts: out.texts,
            images: [], image_count: 0, source: 'https://enka.network/u/' + input.uid + '/'}
        }
      }
    }
    if (input.operation === 'help') {
      await out.reply('云崽面板查询：提供原神 UID 和角色名；只读取公开展柜，不需要 Cookie。角色需放入游戏展柜并开启显示详情。')
      await profileHelp(e)
    } else if (input.operation === 'list') {
      e.msg = `#面板列表 ${e.uid}`
      await ProfileList.render(e)
    } else {
      const char = Character.get(input.character, 'gs')
      if (!char || char.isSr || char.isCustom) {
        await out.reply('未识别到原神角色，请使用角色正式名称或常用别名。')
      } else {
        // Resolve aliases before constructing the command; never dispatch free text.
        e.msg = `#${char.name}面板 ${e.uid}`
        await ProfileDetail.detail(e)
      }
    }
    // Some upstream helpers call e.reply without awaiting the returned promise.
    await Promise.all(pendingReplies)
    if (!out.texts.length && !out.images.length) await out.reply('没有取得面板结果，请确认 UID、角色名称和公开展柜设置。')
    const player = input.uid ? Player.create(input.uid) : null
    const profiles = player?.getProfiles?.() || {}
    const selected = input.character ? Character.get(input.character, 'gs') : null
    const p = selected ? profiles[selected.id] : null
    const publicData = p ? {
      character: p.char?.name, level: p.level, constellation: p.cons,
      weapon: p.getWeaponDetail?.()?.name || p.weapon?.name,
      attributes: Object.fromEntries(['hp', 'atk', 'def', 'mastery', 'cpct', 'cdmg', 'recharge', 'dmg']
        .filter(k => Number.isFinite(p.attr?.[k])).map(k => [k, Math.round(p.attr[k] * 100) / 100])),
    } : null
    return { ok: true, operation: input.operation, uid: input.uid,
      character: input.character, texts: out.texts, images: out.images,
      source: input.uid ? 'https://enka.network/u/' + input.uid + '/' : 'miao-plugin',
      updated_at: player?.getUpdateTime?.() || null, image_count: out.images.length,
      profile: publicData,
      characters: Object.values(profiles).slice(0, 24).map(p => ({name: p.char?.name, level: p.level, constellation: p.cons})) }
  } finally { out.close() }
}

function send(res, status, body) {
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' })
  res.end(JSON.stringify(body))
}

const server = http.createServer(async (req, res) => {
  if (req.method === 'GET' && req.url === '/healthz') {
    try { await redis.ping() } catch { return send(res, 503, { ready: false }) }
    return send(res, 200, { ready: true, busy: active, qq_adapter: false,
      operations: ['help', 'profile', 'list'], yunzai_commit: process.env.YUNZAI_COMMIT,
      miao_commit: process.env.MIAO_COMMIT })
  }
  if (req.method !== 'POST' || req.url !== '/call') return send(res, 404, { error: 'not_found' })
  if (!authorized(req.headers.authorization, token)) return send(res, 401, { error: 'unauthorized' })
  let input
  try {
    let raw = ''
    for await (const part of req) {
      raw += part.toString('utf8')
      if (Buffer.byteLength(raw) > 4096) return send(res, 413, { error: 'body_too_large' })
    }
    input = validate(JSON.parse(raw))
  } catch { return send(res, 400, { error: 'invalid_request' }) }
  const key = input.actor + ':' + input.request_id
  const signature = createHash('sha256').update(JSON.stringify(input)).digest('hex')
  for (const [k, v] of cache) if (Date.now() - v.time > 120000) cache.delete(k)
  if (cache.has(key)) {
    const cached = cache.get(key)
    if (cached.signature !== signature) return send(res, 409, { error: 'request_id_conflict' })
    return send(res, cached.status, cached.body)
  }
  if (active) return send(res, 429, { error: 'busy_retry_later' })
  active = true
  let body, status = 200
  try { body = await execute(input) }
  catch (err) {
    console.error('Yunzai query failed:', err?.constructor?.name || 'Error')
    body = { error: '云崽查询或图片生成暂时失败，请稍后重试。' }; status = 502
  } finally { active = false }
  // Small bounded cache prevents retries from rendering or fetching twice.
  cache.set(key, { signature, body, status, time: Date.now() })
  while (cache.size > 8) cache.delete(cache.keys().next().value)
  send(res, status, body)
})
server.requestTimeout = 10000
server.headersTimeout = 5000
server.listen(8082, '0.0.0.0', () => console.log('Yunzai isolated bridge ready'))
