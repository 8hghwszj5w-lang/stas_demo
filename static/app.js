'use strict';

/* 所有服务端内容通过文本节点展示；不将用户内容解释为 HTML。 */
const $ = (id) => document.getElementById(id);
const categories = { profile: '关于你', preference: '偏好', goal: '目标', boundary: '边界' };
const presets = {
  deepseek: { base_url: 'https://api.deepseek.com/v1', model: 'deepseek-chat' },
  openai: { base_url: 'https://api.openai.com/v1', model: 'gpt-4o-mini' },
  ollama: { base_url: 'http://localhost:11434/v1', model: 'qwen2.5:7b' },
  custom: { base_url: '', model: '' }
};
const app = { state: null, sessionId: null, messages: [], pending: null, sending: false, connected: false, category: 'all', settingsDirty: false, detailReturnFocus: null, sessionEpoch: 0, remoteBusy: false };
const recoveryKey = 'guanlan.pending.v1';
const activeKey = 'guanlan.session.v1';
let pendingAssistant = null;
let lastFocus = null;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}
function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'icon'); svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', '#i-' + name); svg.append(use); return svg;
}
function button(text, className, action) {
  const node = element('button', className, text); node.type = 'button';
  if (action) node.addEventListener('click', action); return node;
}
function storageGet(key) { try { return sessionStorage.getItem(key); } catch (_) { return null; } }
function storageSet(key, value) { try { value === null ? sessionStorage.removeItem(key) : sessionStorage.setItem(key, value); } catch (_) { /* 隐私模式不影响当前页重试。 */ } }
function savePending() { storageSet(recoveryKey, app.pending ? JSON.stringify(app.pending) : null); }
function uuid() {
  if (crypto.randomUUID) return crypto.randomUUID();
  const a = crypto.getRandomValues(new Uint8Array(16)); a[6] = (a[6] & 15) | 64; a[8] = (a[8] & 63) | 128;
  const s = Array.from(a, (b) => b.toString(16).padStart(2, '0')).join('');
  return `${s.slice(0,8)}-${s.slice(8,12)}-${s.slice(12,16)}-${s.slice(16,20)}-${s.slice(20)}`;
}
function toast(message, error = false) {
  const node = element('div', 'toast' + (error ? ' error' : ''), message);
  $('toasts').append(node); setTimeout(() => node.remove(), error ? 6500 : 3800);
}
async function api(path, method = 'GET', data) {
  const options = { method, headers: { Accept: 'application/json' }, cache: 'no-store' };
  if (method !== 'GET') {
    options.headers['Content-Type'] = 'application/json'; options.headers['X-Local-App'] = 'guanlan';
    options.body = JSON.stringify(data === undefined ? {} : data);
  }
  const response = await fetch(path, options);
  let body;
  try { body = await response.json(); } catch (_) { throw new Error('服务返回了无法识别的内容，请检查连接后重试。'); }
  if (!response.ok || body.error) throw new Error(body.error?.message || `请求失败（${response.status}）`);
  return body;
}
function showConnection(message) {
  $('connection-banner').hidden = !message; $('connection-text').textContent = message || '';
}
function syncControls() {
  const busy = app.sending || app.remoteBusy;
  $('send-button').disabled = !app.connected || busy || !$('message-input').value.trim();
  $('message-input').disabled = app.sending;
  $('messages').setAttribute('aria-busy', String(app.sending));
  for (const id of ['new-chat', 'memory-switch', 'add-memory', 'open-settings', 'model-badge', 'connect-model']) $(id).disabled = busy || !app.connected;
  $('send-button').replaceChildren(app.sending ? element('span', 'busy-spinner') : icon('arrow'));
  $('send-button').setAttribute('aria-label', app.sending ? '正在回复' : app.pending ? '使用原请求 ID 重试发送' : '发送消息');
  $('input-count').textContent = $('message-input').value.length > 5000 ? `${$('message-input').value.length} / 6000` : '';
}
function applySettings() {
  const settings = app.state.settings;
  const local = settings.mode === 'local';
  $('model-name').textContent = local ? '本地预览 · 规则回复' : settings.model || '已连接模型';
  $('model-badge').classList.toggle('local', local);
  $('preview-notice').hidden = !local;
  const prompts = local ? ['我叫小林，我是产品设计师。以后请回答简洁一点。', '我正在学习Python。', '你还记得我吗？'] : ['我希望你记住我的沟通偏好：', '我最近的目标是：', '今天我想和你聊聊：'];
  const titles = local ? ['从了解我开始', '记住正在做的事', '试试跨会话记忆'] : ['从了解我开始', '一起理清目标', '随意聊聊今天'];
  const descriptions = local ? ['示例内容，点击后可修改', '学习近况，点击后可修改', '有记忆后，换个会话问问'] : ['告诉观澜你的沟通偏好', '把想做的事，变成下一步', '从一个念头，开启对话'];
  document.querySelectorAll('[data-prompt]').forEach((chip, index) => {
    chip.dataset.prompt = prompts[index]; chip.querySelector('strong').textContent = titles[index]; chip.querySelector('span').textContent = descriptions[index];
  });
  $('memory-switch').setAttribute('aria-checked', String(settings.memory_enabled));
  $('memory-switch-text').textContent = settings.memory_enabled ? '记忆已开启' : '记忆已暂停';
  $('composer-status').textContent = settings.memory_enabled ? '记忆可查看、修改与忘记。你始终拥有决定权。' : '记忆已暂停：不写入新记忆，也不向模型注入已有记忆。';
}
function renderSessions() {
  const sessions = app.state.sessions.filter((s) => !s.archived);
  $('session-count').textContent = sessions.length || '';
  $('session-list').replaceChildren();
  if (!sessions.length) $('session-list').append(element('p', 'quiet sidebar-empty', '还没有对话。\n从一个想法开始吧。'));
  for (const session of sessions) {
    const row = element('div', 'session-row' + (session.id === app.sessionId ? ' active' : ''));
    const open = button('', 'session-button', () => openSession(session.id));
    open.append(icon('chat'), element('span', '', session.title || '新对话')); open.title = session.title || '新对话';
    open.disabled = app.sending;
    if (session.id === app.sessionId) open.setAttribute('aria-current', 'page');
    const more = button('', 'session-more', () => sessionActions(session)); more.append(icon('more'));
    more.setAttribute('aria-label', `管理会话：${session.title || '新对话'}`); more.disabled = app.sending;
    row.append(open, more); $('session-list').append(row);
  }
}
async function refreshState() {
  const state = await api('/api/state');
  app.state = state; app.connected = true; app.remoteBusy = Boolean(state.busy);
  applySettings(); renderSessions(); renderMemories(); syncControls();
  showConnection(app.remoteBusy && !app.sending ? '服务端仍在处理上一条请求。稍后重新同步，再以原请求 ID 恢复结果。' : '');
  return state;
}
async function initialize() {
  try {
    const pending = JSON.parse(storageGet(recoveryKey) || 'null');
    if (pending && typeof pending.request_id === 'string' && typeof pending.content === 'string' && typeof pending.session_id === 'string') {
      app.pending = pending; app.sessionId = pending.session_id;
      $('message-input').value = pending.content; resizeInput();
    }
  } catch (_) { storageSet(recoveryKey, null); }
  try {
    await refreshState();
    const id = app.pending?.session_id || storageGet(activeKey);
    if (id && app.state.sessions.some((s) => s.id === id && !s.archived)) await openSession(id, true);
    else if (!app.pending) { renderMessages(); }
    else { showConnection('已恢复未完成消息，但原会话暂时不可用。请重新同步；修改输入将放弃原请求。'); }
    if (app.pending) {
      $('message-input').value = app.pending.content;
      showConnection('已恢复未确认完成的消息。可以重试：沿用原请求 ID，不会重复保存对话。');
      $('composer-status').textContent = '上次发送未确认完成，输入已恢复。点击发送可安全重试。';
    }
    resizeInput(); syncControls();
  } catch (error) { app.connected = false; showConnection('无法连接本地服务：' + error.message); syncControls(); }
}
async function openSession(id, restoring = false) {
  if (app.sending) return;
  if (!restoring && app.pending && app.pending.session_id !== id) {
    toast('请先重试当前消息，或修改/清空输入以放弃原请求。'); return;
  }
  const epoch = ++app.sessionEpoch;
  try {
    const result = await api('/api/sessions/' + encodeURIComponent(id));
    if (epoch !== app.sessionEpoch) return;
    app.sessionId = id; app.messages = result.messages; storageSet(activeKey, id);
    $('conversation-title').textContent = result.session.title || '新对话';
    if (app.pending && result.messages.some((m) => m.role === 'assistant' && m.request_id === app.pending.request_id)) {
      app.pending = null; savePending(); $('message-input').value = ''; toast('上一条回复已完成，已从服务端恢复。');
    }
    renderSessions(); renderMessages(); syncControls(); closeSidebar(); scrollBottom();
  } catch (error) { toast(error.message, true); if (restoring) throw error; }
}
function renderMessages() {
  $('messages').replaceChildren(); $('welcome').hidden = app.messages.length > 0;
  for (const message of app.messages) $('messages').append(messageNode(message));
}

/* 轻量 Markdown：代码块/标题/列表/粗体/链接。只生成可信 DOM 元素。 */
function inlineMarkdown(text, parent) {
  const expression = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\[[^\]\n]+\]\([^\s)]+\))/g;
  let position = 0;
  for (const match of text.matchAll(expression)) {
    parent.append(document.createTextNode(text.slice(position, match.index)));
    const part = match[0];
    if (part.startsWith('`')) parent.append(element('code', '', part.slice(1, -1)));
    else if (part.startsWith('**')) parent.append(element('strong', '', part.slice(2, -2)));
    else {
      const link = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(part);
      try {
        const url = new URL(link[2]);
        if (!['https:', 'http:'].includes(url.protocol)) throw new Error('不安全链接');
        const a = element('a', '', link[1]); a.href = url.href; a.target = '_blank'; a.rel = 'noopener noreferrer'; parent.append(a);
      } catch (_) { parent.append(document.createTextNode(part)); }
    }
    position = match.index + part.length;
  }
  parent.append(document.createTextNode(text.slice(position)));
}
function markdown(text) {
  const fragment = document.createDocumentFragment();
  const lines = String(text || '').replace(/\r\n/g, '\n').split('\n');
  let paragraph = []; let list = null;
  const flush = () => { if (paragraph.length) { const p = element('p'); inlineMarkdown(paragraph.join('\n'), p); fragment.append(p); paragraph = []; } list = null; };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const fence = /^\s*```(.*)$/.exec(line);
    if (fence) {
      flush(); const codeLines = [];
      while (++i < lines.length && !/^\s*```\s*$/.test(lines[i])) codeLines.push(lines[i]);
      const pre = element('pre');
      if (fence[1].trim()) pre.append(element('span', 'code-language', fence[1].trim().slice(0, 40)));
      pre.append(element('code', '', codeLines.join('\n'))); fragment.append(pre); continue;
    }
    if (!line.trim()) { flush(); continue; }
    const heading = /^(#{1,4})\s+(.+)$/.exec(line);
    if (heading) { flush(); const h = element('h' + heading[1].length); inlineMarkdown(heading[2], h); fragment.append(h); continue; }
    const item = /^\s*([-*+]\s+|\d+\.\s+)(.*)$/.exec(line);
    if (item) {
      if (paragraph.length) flush();
      const type = /^\d/.test(item[1]) ? 'ol' : 'ul';
      if (!list || list.tagName.toLowerCase() !== type) { list = element(type); fragment.append(list); }
      const li = element('li'); inlineMarkdown(item[2], li); list.append(li); continue;
    }
    list = null; paragraph.push(line);
  }
  flush(); return fragment;
}
function messageNode(message) {
  const node = element('article', 'message ' + (message.role === 'user' ? 'user' : 'assistant'));
  if (message.id) node.id = 'message-' + message.id;
  const header = element('div', 'message-header');
  header.append(element('span', 'message-avatar', message.role === 'user' ? '你' : '澜'), element('span', '', message.role === 'user' ? '你' : '观澜'));
  if (message.role !== 'user') header.append(element('span', 'message-tag', message.mode === 'local' ? '本地预览 · 规则回复，非大模型' : '模型回复'));
  const body = element('div', 'message-body');
  if (message.role === 'user') body.append(element('p', '', message.content)); else body.append(markdown(message.content));
  node.append(header, body);
  if (message.context_memory_ids?.length) addContext(node, message.context_memory_ids.map((id) => app.state.memories.find((m) => m.id === id) || { id, content: '此条记忆已修订或忘记，不再展示原文。' }));
  return node;
}
function addContext(node, memories) {
  node.querySelector('.context-details')?.remove();
  if (!memories.length) return;
  const details = element('details', 'context-details');
  details.append(element('summary', '', `参考了 ${memories.length} 条记忆`));
  const list = element('ul');
  for (const memory of memories) list.append(element('li', '', memory.status && memory.status !== 'active' ? '此条记忆已修订或忘记，不再展示原文。' : memory.content));
  details.append(list); node.append(details);
}
function scrollBottom() { const scroller = $('chat-scroll'); scroller.scrollTop = scroller.scrollHeight; }
function nearBottom() { const s = $('chat-scroll'); return s.scrollHeight - s.scrollTop - s.clientHeight < 130; }
function resizeInput() { const input = $('message-input'); input.style.height = 'auto'; input.style.height = Math.min(180, input.scrollHeight) + 'px'; }
function inputChanged() {
  if (app.pending && $('message-input').value.trim() !== app.pending.content) {
    app.pending = null; savePending(); showConnection('');
    if (app.state) applySettings();
  }
  resizeInput(); syncControls();
}
async function sendMessage(event) {
  event?.preventDefault();
  const content = $('message-input').value.trim();
  if (!content || app.sending || !app.connected || app.remoteBusy) return;
  if (content.length > 6000) { toast('单次消息最多 6000 字。', true); return; }
  app.sending = true; syncControls(); renderSessions(); renderMemories(); showConnection('');
  let streamCommitted = false;
  let streamed = ''; let displayNode = null;
  try {
    if (!app.sessionId) {
      const created = await api('/api/sessions', 'POST', {});
      app.sessionId = created.session.id; storageSet(activeKey, app.sessionId);
      app.state.sessions.unshift(created.session); renderSessions();
    }
    if (!app.pending || app.pending.content !== content || app.pending.session_id !== app.sessionId) app.pending = { session_id: app.sessionId, content, request_id: uuid() };
    savePending();
    renderMessages(); $('welcome').hidden = true;
    $('messages').append(messageNode({ role: 'user', content }));
    displayNode = messageNode({ role: 'assistant', content: '', mode: app.state.settings.mode }); pendingAssistant = displayNode;
    const dots = element('div', 'loading-dots'); dots.append(element('span'), element('span'), element('span'));
    displayNode.querySelector('.message-body').append(dots); $('messages').append(displayNode); scrollBottom();
    $('composer-status').textContent = '正在回复… 完整回复与记忆会一起保存。';
    const response = await fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Local-App': 'guanlan', Accept: 'application/x-ndjson' }, body: JSON.stringify(app.pending) });
    if (!response.ok) {
      const error = await response.json().catch(() => ({})); throw new Error(error.error?.message || `发送失败（${response.status}）`);
    }
    if (!response.body) throw new Error('浏览器不支持流式读取，请使用新版浏览器。');
    const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = '';
    const handle = (line) => {
      if (!line.trim()) return;
      let data; try { data = JSON.parse(line); } catch (_) { throw new Error('回复数据不完整，请使用原请求重试。'); }
      if (data.type === 'error') throw new Error(data.message || '回复失败，请重试。');
      if (data.type === 'start') displayNode.querySelector('.message-tag').textContent = data.mode === 'local' ? '本地预览 · 规则回复，非大模型' : '模型回复';
      if (data.type === 'context') addContext(displayNode, data.memories || []);
      if (data.type === 'delta') {
        const stick = nearBottom(); streamed += String(data.text || '');
        displayNode.querySelector('.message-body').replaceChildren(markdown(streamed)); if (stick) scrollBottom();
      }
      if (data.type === 'done') {
        if (!data.message || !data.user_message) throw new Error('服务端完成事件缺少消息，请重新同步。');
        streamCommitted = true;
        if (data.memory_notice) toast(String(data.memory_notice));
        app.messages.push(data.user_message, data.message);
        app.pending = null; savePending(); $('message-input').value = ''; resizeInput();
        const index = app.state.sessions.findIndex((s) => s.id === data.session.id);
        if (index >= 0) app.state.sessions[index] = data.session; else app.state.sessions.unshift(data.session);
        $('conversation-title').textContent = data.session.title;
        renderMessages(); renderSessions(); scrollBottom();
      }
    };
    try {
      while (true) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value, { stream: !done });
        let newline;
        while ((newline = buffer.indexOf('\n')) >= 0) { const line = buffer.slice(0, newline); buffer = buffer.slice(newline + 1); handle(line); }
        if (done) { if (buffer.trim()) handle(buffer); break; }
      }
    } finally { reader.cancel().catch(() => {}); reader.releaseLock(); }
    if (!streamCommitted) throw new Error('连接中断，尚未确认保存。原输入已保留，可安全重试。');
  } catch (error) {
    if (!streamCommitted) {
      if (displayNode) {
        displayNode.classList.add('error');
        if (!streamed) displayNode.querySelector('.message-body').replaceChildren();
        const note = element('div', 'message-error-note', `${error.message} 上方部分回复未确认保存。`);
        note.append(document.createTextNode(' '), button('重试原请求', '', () => sendMessage())); displayNode.append(note);
      }
      toast(error.message, true);
      showConnection('发送未确认完成，输入仍保留。重试使用同一请求 ID，避免重复保存。');
    } else toast('回复已保存，但后续连接中断。可重新同步。', true);
  } finally {
    app.sending = false; pendingAssistant = null;
    try { await refreshState(); if (streamCommitted) renderMessages(); } catch (_) { app.connected = false; showConnection('无法连接本地服务。输入和请求 ID 已保留，请重新同步后重试。'); }
    if (app.pending) {
      $('composer-status').textContent = '未确认完成 · 输入已保留，重试不会重复保存。';
      if (!app.remoteBusy && app.connected) showConnection('发送未确认完成，输入仍保留。重试使用同一请求 ID，避免重复保存。');
    }
    syncControls(); $('message-input').focus();
  }
}
function renderMemories() {
  if (!app.state) return;
  const query = $('memory-search').value.trim().toLocaleLowerCase();
  const all = (app.state.memories || []).filter((m) => m.status === 'active');
  const memories = all.filter((m) => (app.category === 'all' || m.category === app.category) && `${m.content} ${m.source_quote || ''}`.toLocaleLowerCase().includes(query));
  memories.sort((a, b) => Number(b.pinned) - Number(a.pinned));
  const container = $('memory-list'); container.replaceChildren();
  $('memory-summary').textContent = app.state.settings.mode === 'local' ? (all.length ? `${all.length} 条真实记忆 · 本机持久化` : '记忆仅保存在本机') : (app.state.settings.memory_enabled ? '本机持久化 · 相关记忆随请求发送至所选模型' : '本机持久化 · 已暂停发送长期记忆');
  if (!memories.length) {
    const empty = element('div', 'memory-empty'); empty.append(icon('leaf'), element('h3', '', all.length ? '暂时没有匹配的记忆' : '我们可以慢慢认识'), element('p', '', all.length ? '试试其他关键词或分类。' : '告诉我你的偏好、目标或边界，\n值得记住的事会在这里生根。\n也可以由你手动添加。')); container.append(empty); return;
  }
  for (const memory of memories) {
    const card = element('article', 'memory-card');
    const heading = element('div', 'memory-card-heading'); heading.append(element('span', '', categories[memory.category] || '记忆'));
    if (memory.pinned) { const pin = icon('pin'); pin.setAttribute('aria-label', '已置顶'); heading.append(pin); }
    const source = button(memory.source_session_id ? '来自对话 · 查看来源 ↗' : '你主动记录 · 查看详情 ↗', 'memory-source', () => memorySource(memory));
    const actions = element('div', 'memory-card-actions');
    const pin = button(memory.pinned ? '取消置顶' : '置顶', '', () => mutateMemory(memory, { pinned: !memory.pinned }));
    const edit = button('编辑', '', () => memoryEditor(memory));
    const history = button('修订', '', () => memoryHistory(memory));
    const forget = button('忘记', '', () => forgetMemory(memory));
    for (const action of [pin, edit, history, forget]) { action.disabled = app.sending || app.remoteBusy; action.setAttribute('aria-label', action.textContent + '记忆：' + memory.content.slice(0, 35)); }
    actions.append(pin, edit, history, forget); card.append(heading, element('p', '', memory.content), source, actions); container.append(card);
  }
}
async function mutateMemory(memory, change) {
  if (app.sending || app.remoteBusy) return;
  try { await api('/api/memories/' + encodeURIComponent(memory.id), 'PATCH', change); await refreshState(); renderMessages(); toast('记忆已更新。'); }
  catch (error) { toast(error.message, true); }
}
function showDialog(dialog) { lastFocus = document.activeElement; dialog.showModal(); }
function closeDialog(dialog) { dialog.close(); }
function detailDialog(title, body, actions = []) {
  const dialog = $('detail-dialog');
  $('detail-title').textContent = title; $('detail-body').replaceChildren(...body); $('detail-footer').replaceChildren(...actions);
  if (!dialog.open) showDialog(dialog); return dialog;
}
function memoryEditor(memory = null) {
  const label = element('label', 'field', memory ? '修改这条记忆' : '你希望观澜记住什么？');
  const input = element('textarea'); input.value = memory?.content || ''; input.maxLength = 500; input.rows = 4; label.append(input);
  const categoryLabel = element('label', 'field', '分类'); const category = element('select');
  for (const [value, name] of Object.entries(categories)) { const option = element('option', '', name); option.value = value; category.append(option); }
  category.value = memory?.category || 'profile'; category.disabled = Boolean(memory); categoryLabel.append(category);
  const help = element('p', 'field-help', memory ? '修改会建立新修订；旧内容不再注入，历史聊天原文仍保留。' : '只记录你愿意长期保留的信息。以后可以随时修改或忘记。');
  const save = button('保存记忆', 'primary-button', async () => {
    if (!input.value.trim()) { input.focus(); return; }
    save.disabled = true;
    try {
      if (memory) await api('/api/memories/' + encodeURIComponent(memory.id), 'PATCH', { content: input.value.trim() });
      else await api('/api/memories', 'POST', { content: input.value.trim(), category: category.value });
      closeDialog($('detail-dialog')); await refreshState(); renderMessages(); toast('已经记下了。');
    } catch (error) { toast(error.message, true); } finally { save.disabled = false; }
  });
  detailDialog(memory ? '编辑记忆' : '手动添加记忆', [label, categoryLabel, help], [button('取消', 'secondary-button', () => closeDialog($('detail-dialog'))), save]); input.focus();
}
function forgetMemory(memory) {
  const content = element('p', '', memory.content);
  const warning = element('p', 'warning-copy', '忘记后，这条记忆及包含其来源或引用的历史轮次将不再注入后续对话。历史聊天原文与修订记录仍然保存，可供查看；这不是物理擦除。');
  const confirm = button('确认忘记', 'danger-button', async () => {
    confirm.disabled = true;
    try { await api('/api/memories/' + encodeURIComponent(memory.id), 'PATCH', { status: 'forgotten' }); closeDialog($('detail-dialog')); await refreshState(); renderMessages(); toast('已忘记，不会再用于后续对话。'); }
    catch (error) { toast(error.message, true); } finally { confirm.disabled = false; }
  });
  detailDialog('忘记这条记忆？', [content, warning], [button('保留记忆', 'secondary-button', () => closeDialog($('detail-dialog'))), confirm]);
}
async function memoryHistory(memory) {
  detailDialog('记忆修订历史', [element('p', 'quiet', '正在加载修订…')]);
  try {
    const data = await api('/api/memories/' + encodeURIComponent(memory.id) + '/history');
    if (!$('detail-dialog').open) return;
    const revisions = data.revisions.map((revision) => {
      const item = element('article', 'revision');
      const status = { active: '当前版本', superseded: '已被取代', forgotten: '已忘记' }[revision.status] || revision.status;
      item.append(element('strong', '', `版本 ${revision.version} · ${status}`), element('p', '', revision.content), element('small', '', revision.source_quote ? '来源：' + revision.source_quote : '用户主动记录或修订')); return item;
    });
    $('detail-body').replaceChildren(...(revisions.length ? revisions : [element('p', 'quiet', '暂无修订记录。')]));
  } catch (error) { $('detail-body').replaceChildren(element('p', 'warning-copy', error.message)); }
}
function memorySource(memory) {
  const nodes = [element('p', '', memory.content), element('p', 'quiet', `分类：${categories[memory.category] || '记忆'} · 版本 ${memory.version}`)];
  if (memory.source_quote) nodes.push(element('blockquote', 'source-quote', memory.source_quote));
  else nodes.push(element('p', 'quiet', '这条记忆由你手动记录或修订。'));
  const actions = [button('查看修订', 'secondary-button', () => memoryHistory(memory))];
  if (memory.source_session_id) actions.push(button('查看来源对话', 'primary-button', async () => {
    closeDialog($('detail-dialog')); await openSession(memory.source_session_id);
    const target = $('message-' + memory.source_message_id);
    if (target) { target.classList.add('highlight-message'); target.scrollIntoView({ block: 'center', behavior: 'smooth' }); setTimeout(() => target.classList.remove('highlight-message'), 4000); }
    if (window.innerWidth < 1190) toggleMemory(false);
  }));
  detailDialog('记忆来源', nodes, actions);
}
function sessionActions(session) {
  const label = element('label', 'field', '会话名称'); const input = element('input'); input.value = session.title; input.maxLength = 80; label.append(input);
  const save = button('保存名称', 'primary-button', async () => {
    if (!input.value.trim()) return;
    save.disabled = true;
    try { await api('/api/sessions/' + encodeURIComponent(session.id), 'PATCH', { title: input.value.trim() }); closeDialog($('detail-dialog')); await refreshState(); if (app.sessionId === session.id) $('conversation-title').textContent = input.value.trim(); }
    catch (error) { toast(error.message, true); } finally { save.disabled = false; }
  });
  const archive = button('归档会话', 'secondary-button', () => {
    const confirm = button('确认归档', 'primary-button', async () => {
      if (app.pending?.session_id === session.id) { toast('请先处理当前未完成的消息。', true); return; }
      confirm.disabled = true;
      try { await api('/api/sessions/' + encodeURIComponent(session.id), 'DELETE'); closeDialog($('detail-dialog')); if (app.sessionId === session.id) resetConversation(); await refreshState(); toast('会话已归档，记忆来源仍保留。'); }
      catch (error) { toast(error.message, true); } finally { confirm.disabled = false; }
    });
    detailDialog('归档会话？', [element('p', '', '会话将从左侧列表移除，但不会删除聊天内容或其记忆来源。')], [button('取消', 'secondary-button', () => closeDialog($('detail-dialog'))), confirm]);
  });
  detailDialog('管理会话', [label], [archive, save]); input.focus();
}
function resetConversation() {
  app.sessionEpoch++; app.sessionId = null; app.messages = []; storageSet(activeKey, null);
  $('conversation-title').textContent = '新的开始'; $('message-input').value = ''; renderMessages(); renderSessions(); resizeInput(); syncControls(); closeSidebar(); $('message-input').focus();
}
function newConversation() {
  if (app.sending || app.remoteBusy) return;
  if (app.pending) { toast('当前有未完成消息。请先重试，或修改/清空输入后再新建对话。'); $('message-input').focus(); return; }
  resetConversation();
}
function openSettings() {
  if (!app.state || app.sending || app.remoteBusy) return;
  const settings = app.state.settings;
  $('setting-mode').value = settings.mode;
  $('setting-provider').value = presets[settings.provider] ? settings.provider : 'custom';
  $('setting-model').value = settings.model || '';
  $('setting-url').value = settings.base_url || '';
  $('setting-key').value = ''; $('clear-key').checked = false;
  $('key-status').textContent = settings.has_api_key ? '已保存 · ' + (settings.api_key_masked || '••••••••') : '尚未保存密钥';
  $('settings-result').hidden = true; app.settingsDirty = false; $('test-settings').disabled = false;
  showDialog($('settings-dialog'));
}
function settingsResult(message, error = false) { $('settings-result').hidden = false; $('settings-result').textContent = message; $('settings-result').classList.toggle('error', error); }
async function saveSettings(event) {
  event.preventDefault(); $('save-settings').disabled = true; $('test-settings').disabled = true;
  const body = { mode: $('setting-mode').value, provider: $('setting-provider').value, base_url: $('setting-url').value.trim(), model: $('setting-model').value.trim() };
  body.api_key = $('clear-key').checked ? null : $('setting-key').value.trim();
  try {
    const result = await api('/api/settings', 'POST', body); app.state.settings = result.settings; applySettings(); renderMemories();
    $('setting-key').value = ''; $('clear-key').checked = false;
    $('key-status').textContent = result.settings.has_api_key ? '已保存 · ' + (result.settings.api_key_masked || '••••••••') : '尚未保存密钥';
    app.settingsDirty = false; settingsResult('配置已保存。现在可以测试已保存的连接。'); toast('配置已保存。');
  } catch (error) { settingsResult(error.message, true); }
  finally { $('save-settings').disabled = false; $('test-settings').disabled = app.settingsDirty; }
}
async function testSettings() {
  if (app.settingsDirty) { settingsResult('请先保存配置，再测试连接。', true); return; }
  $('test-settings').disabled = true; $('save-settings').disabled = true;
  settingsResult('正在测试已保存的连接…');
  try { const result = await api('/api/settings/test', 'POST', {}); settingsResult(result.message + (result.model ? '\n模型：' + result.model : ''), !result.ok); }
  catch (error) { settingsResult(error.message, true); }
  finally { $('test-settings').disabled = app.settingsDirty; $('save-settings').disabled = false; }
}
function toggleMemory(force) {
  const open = force === undefined ? $('app-shell').classList.contains('memory-collapsed') : force;
  $('app-shell').classList.toggle('memory-collapsed', !open);
  $('toggle-memory').setAttribute('aria-expanded', String(open));
  $('toggle-memory').setAttribute('aria-label', open ? '收起记忆面板' : '打开记忆面板');
}
function closeSidebar() { $('app-shell').classList.remove('sidebar-open'); $('toggle-sidebar').setAttribute('aria-expanded', 'false'); }
async function reconnect() {
  if (app.sending) return;
  $('reconnect').disabled = true;
  try { await refreshState(); if (app.sessionId) await openSession(app.sessionId, true); if (app.pending) { $('message-input').value = app.pending.content; if (!app.remoteBusy) showConnection('消息输入已恢复，可使用原请求 ID 重试。'); } resizeInput(); syncControls(); }
  catch (error) { app.connected = false; showConnection('仍无法连接本地服务：' + error.message); syncControls(); }
  finally { $('reconnect').disabled = false; }
}

$('composer').addEventListener('submit', sendMessage);
$('message-input').addEventListener('input', inputChanged);
$('message-input').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) { event.preventDefault(); sendMessage(); }
});
$('new-chat').addEventListener('click', newConversation);
for (const chip of document.querySelectorAll('[data-prompt]')) chip.addEventListener('click', () => { if (app.sending) return; $('message-input').value = chip.dataset.prompt; inputChanged(); $('message-input').focus(); });
for (const id of ['open-settings', 'model-badge', 'connect-model']) $(id).addEventListener('click', openSettings);
$('settings-form').addEventListener('submit', saveSettings);
$('test-settings').addEventListener('click', testSettings);
$('settings-form').addEventListener('input', () => { app.settingsDirty = true; $('test-settings').disabled = true; settingsResult('配置有未保存的更改。请先保存，再测试连接。'); });
$('setting-provider').addEventListener('change', () => { const preset = presets[$('setting-provider').value]; $('setting-url').value = preset.base_url; $('setting-model').value = preset.model; $('setting-key').value = ''; $('key-status').textContent = '更换服务商不会沿用旧密钥'; });
$('memory-switch').addEventListener('click', async () => {
  $('memory-switch').disabled = true;
  try { const result = await api('/api/settings', 'POST', { memory_enabled: !app.state.settings.memory_enabled }); app.state.settings = result.settings; applySettings(); renderMemories(); toast(result.settings.memory_enabled ? '记忆已开启。' : '记忆已暂停：停止记忆写入与注入，已有记忆仍保留。'); }
  catch (error) { toast(error.message, true); } finally { syncControls(); }
});
$('memory-search').addEventListener('input', renderMemories);
for (const filter of document.querySelectorAll('[data-category]')) filter.addEventListener('click', () => { app.category = filter.dataset.category; for (const item of document.querySelectorAll('[data-category]')) { const active = item === filter; item.classList.toggle('active', active); item.setAttribute('aria-pressed', String(active)); } if (app.state) renderMemories(); });
$('add-memory').addEventListener('click', () => memoryEditor());
$('toggle-memory').addEventListener('click', () => toggleMemory()); $('close-memory').addEventListener('click', () => { toggleMemory(false); $('toggle-memory').focus(); });
$('toggle-sidebar').addEventListener('click', () => { const open = $('app-shell').classList.toggle('sidebar-open'); $('toggle-sidebar').setAttribute('aria-expanded', String(open)); });
$('mobile-scrim').addEventListener('click', closeSidebar);
$('reconnect').addEventListener('click', reconnect);
for (const close of document.querySelectorAll('[data-close-dialog]')) close.addEventListener('click', () => closeDialog(close.closest('dialog')));
for (const dialog of document.querySelectorAll('dialog')) {
  dialog.addEventListener('click', (event) => { if (event.target === dialog) { const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) closeDialog(dialog); } });
  dialog.addEventListener('close', () => { if (dialog.id === 'settings-dialog') $('setting-key').value = ''; if (lastFocus?.isConnected) lastFocus.focus(); });
}
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') closeSidebar();
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k' && !document.querySelector('dialog[open]')) { event.preventDefault(); newConversation(); }
});
window.addEventListener('online', () => { if (!app.sending) reconnect(); });
window.addEventListener('offline', () => showConnection('网络连接已断开。未完成输入与请求 ID 会保留，可在连接恢复后重试。'));
window.addEventListener('beforeunload', (event) => { if (app.sending) { event.preventDefault(); event.returnValue = ''; } });
if (window.innerWidth < 1190) toggleMemory(false);
syncControls();
initialize();
