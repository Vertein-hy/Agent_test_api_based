import { createHash, timingSafeEqual } from 'node:crypto'
import fs from 'node:fs/promises'
import path from 'node:path'

export const MAX_IMAGE = 3 * 1024 * 1024
export function authorized(header, token) {
  const got = Buffer.from(header || '')
  const want = Buffer.from('Bearer ' + token)
  return got.length === want.length && timingSafeEqual(got, want)
}

export function validate(body) {
  if (!body || typeof body !== 'object' || Array.isArray(body) ||
      Object.keys(body).some(k => !['name', 'arguments', 'context'].includes(k)) || body.name !== 'yunzai_query')
    throw new Error('invalid_request')
  const a = body.arguments
  const c = body.context
  if (!a || typeof a !== 'object' || Array.isArray(a) ||
      Object.keys(a).some(k => !['operation', 'uid', 'character'].includes(k)) ||
      !['help', 'profile', 'list'].includes(a.operation)) throw new Error('invalid_arguments')
  if (!c || typeof c !== 'object' || Array.isArray(c) ||
      Object.keys(c).some(k => !['actor', 'request_id'].includes(k)) ||
      !/^[a-f0-9]{64}$/.test(c.actor || '') || !/^[a-f0-9-]{36}$/.test(c.request_id || ''))
    throw new Error('invalid_context')
  if (a.operation !== 'help' && (typeof a.uid !== 'string' || !/^(?:[1-9][0-9]{8}|18[0-9]{8})$/.test(a.uid)))
    throw new Error('invalid_uid')
  if ('uid' in a && typeof a.uid !== 'string') throw new Error('invalid_uid')
  if ('character' in a && (typeof a.character !== 'string' ||
      !/^[\p{L}\p{N}· ._-]{1,24}$/u.test(a.character))) throw new Error('invalid_character')
  if (a.operation === 'profile' && !a.character) throw new Error('missing_character')
  return { operation: a.operation, uid: a.uid || '', character: a.character || '', ...c }
}

export function imageType(buf) {
  if (buf.length >= 16 && buf.subarray(0, 8).equals(Buffer.from([137,80,78,71,13,10,26,10]))) return 'image/png'
  if (buf.length >= 16 && buf[0] === 255 && buf[1] === 216 && buf[2] === 255) return 'image/jpeg'
  throw new Error('invalid_image')
}

export function collector(resourceRoot) {
  const texts = [], images = []
  let alive = true
  return {
    texts, images,
    close() { alive = false },
    async reply(message) {
      if (!alive) return false
      const parts = Array.isArray(message) ? message.flat(2) : [message]
      for (const part of parts) {
        if (typeof part === 'string') {
          if (texts.length < 8 && part.trim()) texts.push(part.slice(0, 1500))
        } else if (part?.type === 'image' && images.length < 2) {
          let buf = part.file
          if (typeof buf === 'string' && buf.startsWith('base64://')) {
            if (buf.length > MAX_IMAGE * 4 / 3 + 20) throw new Error('image_too_large')
            buf = Buffer.from(buf.slice(9), 'base64')
          } else if (typeof buf === 'string' && buf.startsWith('file://')) {
            const file = await fs.realpath(buf.slice(7))
            const root = await fs.realpath(resourceRoot)
            if (!file.startsWith(root + path.sep) || (await fs.stat(file)).size > MAX_IMAGE)
              throw new Error('image_path_rejected')
            buf = await fs.readFile(file)
          }
          if (!Buffer.isBuffer(buf) || buf.length > MAX_IMAGE) throw new Error('invalid_image')
          const mime = imageType(buf)
          const sha256 = createHash('sha256').update(buf).digest('hex')
          if (!images.some(i => i.sha256 === sha256)) images.push({ mime, sha256, base64: buf.toString('base64') })
        }
        // Buttons, forwards, files and arbitrary network URLs are not delivered.
      }
      return { message_id: 'captured', captured: true }
    }
  }
}
