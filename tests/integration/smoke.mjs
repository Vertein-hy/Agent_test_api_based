import fs from 'node:fs/promises'
import { createHash, randomUUID } from 'node:crypto'
import assert from 'node:assert/strict'
const uid = process.env.SMOKE_PANEL_UID
const character = process.env.SMOKE_PANEL_CHARACTER || '雷神'
assert.match(uid || '', /^(?:[1-9][0-9]{8}|18[0-9]{8})$/)
const token = (await fs.readFile('/run/secrets/yunzai_token', 'utf8')).trim()
const headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
const context = {actor: 'a'.repeat(64), request_id: randomUUID()}
async function call(args, ctx = {...context, request_id: randomUUID()}) {
  const r = await fetch('http://127.0.0.1:8082/call', {method: 'POST', headers,
    body: JSON.stringify({name: 'yunzai_query', arguments: args, context: ctx}), signal: AbortSignal.timeout(70000)})
  const result = await r.json()
  assert.equal(r.status, 200, JSON.stringify(result))
  return result
}
const noAuth = await fetch('http://127.0.0.1:8082/call', {method:'POST', body:'{}'})
assert.equal(noAuth.status, 401)
const invalid = await fetch('http://127.0.0.1:8082/call', {method:'POST', headers,
  body:JSON.stringify({name:'yunzai_query', arguments:{operation:'shell',command:'id'}, context})})
assert.equal(invalid.status, 400)
const help = await call({operation:'help'}, context)
assert.equal(help.images.length, 1)
const repeat = await call({operation:'help'}, context)
assert.equal(repeat.images[0].sha256, help.images[0].sha256)
console.log('Auth, command allowlist, help image and retry checks passed')
const list = await call({operation:'list', uid})
assert.ok(list.images?.length, JSON.stringify({...list, images: undefined}))
console.log('Configured showcase list rendered')
const profile = await call({operation:'profile', uid, character})
assert.ok(profile.images?.length, JSON.stringify({...profile, images: undefined}))
const image = profile.images[0]
const bytes = Buffer.from(image.base64, 'base64')
assert.equal(createHash('sha256').update(bytes).digest('hex'), image.sha256)
await fs.mkdir('/app/temp/addon-verification', {recursive:true})
await fs.writeFile('/app/temp/addon-verification/configured-profile.' + (image.mime === 'image/png' ? 'png' : 'jpg'), bytes)
console.log('Real miao-plugin profile rendered:', {bytes:bytes.length, mime:image.mime, images:profile.images.length})
