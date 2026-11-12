// 本机 Chrome DevTools 验收：仅对独立测试数据实例运行，不使用用户模型密钥。
import fs from 'node:fs/promises';
import path from 'node:path';
import http from 'node:http';
import assert from 'node:assert/strict';

const base = process.argv[2] || 'http://127.0.0.1:8788';
const devtools = process.argv[3] || 'http://127.0.0.1:9224';
const artifacts = path.resolve('demo/artifacts');
await fs.mkdir(artifacts, { recursive: true });
const target = await (await fetch(devtools + '/json/new?about:blank', { method: 'PUT' })).json();
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
let nextId = 0;
const pending = new Map();
const errors = [];
ws.onmessage = (event) => {
  const message = JSON.parse(event.data);
  if (message.id && pending.has(message.id)) {
    const { resolve, reject, timer } = pending.get(message.id);
    clearTimeout(timer); pending.delete(message.id);
    message.error ? reject(new Error(JSON.stringify(message.error))) : resolve(message.result);
  } else if (message.method === 'Runtime.exceptionThrown') errors.push(message.params.exceptionDetails.text);
};
function cdp(method, params = {}) {
  const id = ++nextId;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error('CDP timeout: ' + method)); }, 20000);
    pending.set(id, { resolve, reject, timer }); ws.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression) {
  const result = await cdp('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
  if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
  return result.result.value;
}
async function wait(expression, name) {
  const until = Date.now() + 12000;
  while (Date.now() < until) {
    if (await evaluate(expression)) return;
    await new Promise((resolve) => setTimeout(resolve, 70));
  }
  throw new Error('Browser assertion timed out: ' + name);
}
async function click(selector) { await evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`); }
async function fill(selector, value) {
  await evaluate(`(() => {const n=document.querySelector(${JSON.stringify(selector)});n.value=${JSON.stringify(value)};n.dispatchEvent(new Event('input',{bubbles:true}));n.dispatchEvent(new Event('change',{bubbles:true}));})()`);
}
async function screenshot(name) {
  const shot = await cdp('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
  await fs.writeFile(path.join(artifacts, name), Buffer.from(shot.data, 'base64'));
}
async function state() { return (await fetch(base + '/api/state')).json(); }
async function send(text) {
  await fill('#message-input', text);
  await wait(`!document.querySelector('#send-button').disabled`, 'composer enabled');
  await click('#send-button');
  await wait(`document.querySelector('#message-input').value==='' && document.querySelectorAll('.message.assistant').length>0 && !document.querySelector('#message-input').disabled`, 'message committed');
}
async function buttonText(container, text) {
  await evaluate(`(() => {const b=[...document.querySelectorAll(${JSON.stringify(container+' button')})].find(x=>x.textContent.trim()===${JSON.stringify(text)});if(!b)throw Error('button not found');b.click()})()`);
}
async function memoryAction(content, action) {
  await evaluate(`(() => {const c=[...document.querySelectorAll('.memory-card')].find(x=>x.querySelector('p').textContent.includes(${JSON.stringify(content)}));if(!c)throw Error('memory not found');[...c.querySelectorAll('button')].find(x=>x.textContent.trim()===${JSON.stringify(action)}).click()})()`);
}

let mock;
const checks = [];
try {
  await cdp('Runtime.enable'); await cdp('Page.enable');
  await cdp('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
  await cdp('Page.navigate', { url: base });
  await wait(`document.querySelector('#model-name')?.textContent.includes('本地预览')`, 'app ready');
  assert.equal((await state()).stats.messages, 0, 'browser test must run on a fresh isolated data directory');
  await screenshot('workspace-empty.png');
  assert.match(await evaluate('document.body.innerText'), /规则回复，非真实大模型/);
  checks.push('首页三栏与本地预览明确标识');
  await send('我叫小林，我是产品设计师。我喜欢喝咖啡，以后请回答简洁一点。');
  await wait(`document.querySelectorAll('.memory-card').length===4`, 'four actual memories');
  assert.equal((await state()).stats.active_memories, 4);
  checks.push('对话真实提交与四条记忆来源');
  await memoryAction('姓名', '修订');
  await wait(`document.querySelector('#detail-body').innerText.includes('版本 1')`, 'revision display');
  await click('#detail-dialog [data-close-dialog]');
  await click('#new-chat');
  await send('你还记得我吗？');
  assert.match(await evaluate(`document.querySelector('.message.assistant .message-body').innerText`), /小林/);
  assert.equal((await state()).stats.sessions, 2);
  checks.push('新会话记忆召回与引用展示');
  await send('我不再喜欢咖啡，改喝茶。');
  await wait(`document.querySelector('#memory-list').innerText.includes('用户偏好喝茶')`, 'preference replacement');
  await memoryAction('喝茶', '修订');
  await wait(`document.querySelector('#detail-body').innerText.includes('已被取代')`, 'superseded history');
  assert.match(await evaluate(`document.querySelector('#detail-body').innerText`), /喝咖啡/);
  await click('#detail-dialog [data-close-dialog]');
  checks.push('偏好取代与两个历史修订');
  await screenshot('workspace-memory.png');
  await memoryAction('姓名', '编辑');
  await fill('#detail-body textarea', '用户姓名是小周');
  await buttonText('#detail-footer', '保存记忆');
  await wait(`!document.querySelector('#detail-dialog').open && document.querySelector('#memory-list').innerText.includes('小周')`, 'manual correction');
  checks.push('手动纠正与置顶保护');
  await memoryAction('小周', '忘记');
  assert.match(await evaluate(`document.querySelector('#detail-body').innerText`), /不是物理擦除/);
  await buttonText('#detail-footer', '确认忘记');
  await wait(`!document.querySelector('#detail-dialog').open && !document.querySelector('#memory-list').innerText.includes('小周')`, 'forget memory');
  await send('我叫什么名字？');
  const last = await evaluate(`[...document.querySelectorAll('.message.assistant .message-body')].at(-1).innerText`);
  assert.ok(!last.includes('小林') && !last.includes('小周'));
  checks.push('忘记后召回与关联历史排除');
  await click('#memory-switch');
  await wait(`document.querySelector('#memory-switch').getAttribute('aria-checked')==='false'`, 'pause enabled');
  await send('我住在杭州。');
  assert.ok(!(await state()).memories.some((m) => m.key === 'profile.city'));
  await click('#memory-switch');
  await wait(`document.querySelector('#memory-switch').getAttribute('aria-checked')==='true'`, 'resume memory');
  checks.push('暂停记忆不写入新信息');
  await click('#add-memory');
  await fill('#detail-body textarea', '用户喜欢 <img src=x onerror="window.__xss=1"> 这段测试文本');
  await buttonText('#detail-footer', '保存记忆');
  await wait(`!document.querySelector('#detail-dialog').open && document.querySelector('#memory-list').innerText.includes('<img')`, 'safe text rendering');
  assert.equal(await evaluate('window.__xss || 0'), 0);
  assert.equal(await evaluate(`document.querySelector('#memory-list').querySelectorAll('img').length`), 0);
  checks.push('用户内容作为文本展示，未执行 HTML');
  await cdp('Page.reload');
  await wait(`document.querySelector('#memory-list')?.innerText.includes('喝茶')`, 'reload persistence');
  assert.ok((await state()).stats.messages >= 10);
  checks.push('刷新恢复对话与记忆');

  // 模型端只为协议测试mock，不代表真实供应商验收。
  const modelRequests = [];
  mock = http.createServer(async (req, res) => {
    let body = ''; for await (const chunk of req) body += chunk;
    const request = JSON.parse(body); modelRequests.push(request);
    if (request.stream) {
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      for (const text of ['这是本机协议测试模型。', '你的回复偏好已作为用户级记忆传入。']) res.write('data: '+JSON.stringify({choices:[{delta:{content:text}}]})+'\n\n');
      res.end('data: [DONE]\n\n');
    } else {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({choices:[{message:{content:request.max_tokens===8?'OK':'{"memories":[]}'}}]}));
    }
  });
  await new Promise((resolve) => mock.listen(0, '127.0.0.1', resolve));
  await click('#open-settings');
  await fill('#setting-mode', 'openai'); await fill('#setting-provider', 'custom');
  await fill('#setting-url', 'http://127.0.0.1:'+mock.address().port+'/v1');
  await fill('#setting-model', 'local-protocol-test'); await fill('#setting-key', 'fake-browser-test-key');
  await click('#save-settings');
  await wait(`document.querySelector('#settings-result').innerText.includes('配置已保存')`, 'save model settings');
  assert.equal(await evaluate(`document.querySelector('#setting-key').value`), '');
  assert.ok(!JSON.stringify(await state()).includes('fake-browser-test-key'));
  await click('#test-settings');
  await wait(`document.querySelector('#settings-result').innerText.includes('连接成功')`, 'model connect test');
  await click('#settings-dialog [data-close-dialog]');
  await send('请解释一下递归。');
  assert.match(await evaluate(`[...document.querySelectorAll('.message.assistant .message-tag')].at(-1).innerText`), /模型回复/);
  const chatRequest = modelRequests.find((r) => r.stream);
  assert.ok(chatRequest.messages.some((m) => m.role === 'user' && m.content.includes('用户偏好简洁直接')));
  assert.ok(!chatRequest.messages[0].content.includes('用户偏好简洁直接'));
  checks.push('模型设置、密钥掩码、真实 HTTP SSE 与用户级记忆注入（本机 mock）');

  await cdp('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await cdp('Page.reload');
  await wait(`document.querySelector('#model-name')?.textContent.includes('local-protocol-test')`, 'mobile ready');
  assert.ok(await evaluate('document.documentElement.scrollWidth <= innerWidth + 1'));
  await click('#toggle-sidebar');
  assert.ok(await evaluate(`document.querySelector('#app-shell').classList.contains('sidebar-open')`));
  await click('#mobile-scrim'); await click('#toggle-memory');
  await screenshot('workspace-mobile.png');
  checks.push('移动端布局、会话导航与记忆抽屉');
  assert.deepEqual(errors, []);
  await fs.writeFile(path.join(artifacts, 'browser-report.json'), JSON.stringify({base, checks, browserExceptions:errors, model:'本机协议mock；未调用真实模型供应商', passed:true},null,2));
  console.log(JSON.stringify({passed:true,checks},null,2));
} finally {
  if (mock) await new Promise((resolve) => mock.close(resolve));
  ws.close();
  await fetch(devtools + '/json/close/' + target.id).catch(()=>{});
}
