import test from 'node:test'
import assert from 'node:assert/strict'
import { authorized, validate, collector } from '../../bridge/yunzai/protocol.mjs'

const body = () => ({name: 'yunzai_query', arguments: {operation: 'profile', uid: '700000001', character: '雷神'},
  context: {actor: 'a'.repeat(64), request_id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'}})
test('only declared operations and server-side identity are accepted', () => {
  assert.equal(validate(body()).character, '雷神')
  for (const change of [
    b => b.arguments.operation = 'shell', b => b.arguments.command = '#更新',
    b => b.arguments.character = '#删除面板\n', b => b.arguments.uid = '../../data',
    b => b.arguments.uid = '13812345678', b => b.context.actor = '123456',
    b => b.context.group_id = '456', b => b.name = 'send_group_msg',
  ]) { const b = body(); change(b); assert.throws(() => validate(b)) }
  assert.equal(validate({...body(), arguments: {operation: 'help'}}).operation, 'help')
})
test('bearer authentication compares the entire secret', () => {
  assert.equal(authorized('Bearer correct', 'correct'), true)
  assert.equal(authorized('Bearer wrong', 'correct'), false)
  assert.equal(authorized(undefined, 'correct'), false)
})
test('collector captures supported results and cannot send messages or fetch URLs', async () => {
  const c = collector('/unused')
  const png = Buffer.from('89504e470d0a1a0a0000000000000000', 'hex')
  await c.reply(['hello', {type: 'image', file: png}, {type: 'button', data: ['admin']}, {type: 'reply', id: 42}])
  await c.reply({type: 'image', file: png})
  assert.equal(c.images.length, 1)
  assert.deepEqual(c.texts, ['hello'])
  await assert.rejects(c.reply({type: 'image', file: 'https://example.com/image.png'}))
  await assert.rejects(c.reply({type: 'image', file: 'file:///etc/passwd'}))
  c.close(); assert.equal(await c.reply('late callback'), false)
  assert.deepEqual(c.texts, ['hello'])
})
