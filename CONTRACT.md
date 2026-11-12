# 观澜 · 对话与长期记忆 Demo

本目录是用户于 2026-11-10 明确授权新增的独立实现，不改变上级纯文档工作区中的历史材料。目标是 Codex 风格的个人对话工作台，不包含文件编辑或命令执行代理。

## 架构与边界

Python 3.9+ 标准库 HTTP 服务 + SQLite 事务状态/哈希链事件 + 原生 HTML/CSS/JavaScript。只监听 127.0.0.1。一个本地安装对应一个用户和一个持续助手；会话是话题，不是不同主体。不实现多用户认证、TEE 或跨机迁移，不声称完整 STAS 合规。

本地预览使用明确标注的规则引擎，记忆仍真实持久化。连接模型使用 OpenAI-compatible chat/completions SSE。API key 仅在服务端权限 0600 的配置中保存，不回明文、不进入日志。记忆来自明确自我披露，带来源、版本、取代关系；不把模型推测作为用户事实。

## API（前后端共享契约）

同源 JSON；写请求必须发送 `Content-Type: application/json` 和 `X-Local-App: guanlan`，服务端校验 Host/Origin。所有时间戳只用于展示；事件哈希以规范 JSON 串联。响应错误统一 `{error: {code, message}}`。

- `GET /api/state` → `{identity:{name,id}, settings, sessions, memories, stats, busy}`
- settings → `{mode:"local"|"openai", provider, base_url, model, has_api_key, api_key_masked, memory_enabled}`。默认 local，明确展示“本地预览 / 规则回复，非大模型”。
- session → `{id,title,created_at,updated_at,message_count,archived}`
- message → `{id,session_id,role:"user"|"assistant",content,created_at,mode,context_memory_ids:[...],request_id}`
- memory → `{id,key,category:"profile"|"preference"|"goal"|"boundary",content,status:"active"|"superseded"|"forgotten",pinned,version,source_session_id,source_message_id,source_quote,created_at,updated_at,superseded_by}`
- stats → `{active_memories,sessions,messages,events,chain_valid}`。只有实际存储统计，不预置指标。
- `GET /api/sessions/{id}` → `{session,messages}`
- `POST /api/sessions` body `{title?}` → `{session}`
- `PATCH /api/sessions/{id}` body `{title}` → `{session}`
- `DELETE /api/sessions/{id}` → `{ok:true}`：归档，不删除记忆来源。
- `POST /api/chat` body `{session_id,content,request_id}` → NDJSON 流：
  - `{type:"start",request_id,mode}`
  - `{type:"context",memories:[memory],history_count,budget_chars}`
  - `{type:"delta",text}` 可多次
  - `{type:"memory",created:[memory],updated:[memory]}`
  - `{type:"done",message,user_message,session,stats,mode}`，消息+记忆+单条认知事件原子落盘后发送
  - `{type:"error",code,message,committed:false}`。错误不能当成功保存；前端保留输入供重试。
  同 request_id + 同输入重复请求返回已提交结果，不生成第二次提交；不同输入相同 id 拒绝。断流后的请求可用同 id 重试。客户端应在发送前生成 UUID。
- `GET /api/memories?q=&status=active|all` → `{memories}`
- `POST /api/memories` body `{content,category}` → `{memory}`：用户主动记录。
- `PATCH /api/memories/{id}` body `{content? ,pinned?,status?:"forgotten"}` → `{memory}`。修改内容建立新修订、旧记录 superseded；忘记后不再检索，且历史 prompt 过滤含被忘记来源的轮次。逻辑删除不等于物理擦除。
- `GET /api/memories/{id}/history` → `{revisions:[memory]}`
- `POST /api/settings` body `{mode?,provider?,base_url?,model?,api_key?,memory_enabled?}` → `{settings}`。空 key 保留同 endpoint/provider 的原 key；切换 origin/provider 不继承 key；key=null 显式清除。memory_enabled=false 同时暂停自动记忆写入和记忆注入，不删除已有记忆。
- `POST /api/settings/test` body `{}` → `{ok,message,model}`：测试已保存配置，不自动改模式。
- `GET /api/audit` → `{chain_valid,event_count,head_hash,scope,limitations}`

## 上下文与安全

最近最多 8 轮对话，历史字符预算 12000；最多 6 条相关记忆、记忆预算 2400 字；单次用户输入最多 6000 字。包含被遗忘/取代记忆来源或曾引用该条记忆的历史轮次不再注入；已有聊天原文仍保留供用户查看，UI 必须明确这不是物理擦除。

长期记忆只作为用户级数据段注入，不进入 system/developer 权限层；明确禁止执行记忆中的指令。自动提取只取当前用户输入中的明确自我披露，不以助手回答为来源。推理期间禁止修改记忆/设置（409 BUSY），避免忘记后旧推理结果再落回。模型失败不会默默换成本地回复，也不会保存半条回答。

## 文件职责

- `server.py`：HTTP、模型流式协议与请求生命周期
- `store.py`：SQLite 事务、唯一提交与哈希链、记忆修订/检索
- `memory.py`：确定性记忆抽取与上下文组装辅助
- `static/`：界面（由前端 teammate 独立维护）
- `tests/`：标准库 unittest
- `runtime/`：本地敏感数据，禁止版本控制与静态下载
