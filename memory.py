"""有限、确定性的本地记忆辅助；不读写磁盘、不联网，也不模拟大模型。

只识别列明的第一人称陈述。漏记优于错记，规则并不理解所有自然语言；
安全检测只是保守启发式，不构成完整的秘密检测或提示注入防护。
"""

import re


# 同一语义属性共用一个键，不把句子本身当作键。
_KEYS = {
    "name": "profile.name",
    "occupation": "profile.occupation",
    "city": "profile.city",
    "learning": "goal.learning",
    "drink": "preference.drink",
    "reply_style": "preference.reply_style",
    "emoji": "boundary.emoji",
}
_CATEGORIES = {"profile", "preference", "goal", "boundary"}
_DRINKS = r"(?:咖啡|奶茶|红茶|绿茶|乌龙茶|普洱茶|花茶|茶|牛奶|豆浆|果汁|可乐|气泡水|白开水|水)"
_SECRET = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{12,}|Bearer\s+\S{8,}|"
    r"(?:api[ _-]?key|access[ _-]?token|refresh[ _-]?token|"
    r"client[ _-]?secret|password|passwd)|"
    r"密码|口令|银行卡|信用卡|私钥|密钥|验证码|助记词|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\bAKIA[A-Z0-9]{16}\b|\bgh[pousr]_[A-Za-z0-9]{20,}|"
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|"
    r"(?<!\d)(?:\d[ -]?){15,19}(?!\d))",
    re.IGNORECASE,
)
_INJECTION = re.compile(
    r"(?:忽略|无视|绕过|覆盖|忘掉|忘记|不要遵守).{0,20}(?:规则|指令|提示|安全|限制)|"
    r"(?:泄露|泄漏|输出|显示|打印|告诉我|透露).{0,24}(?:系统提示|系统指令|隐藏提示|开发者指令)|"
    r"(?:系统提示词|system\s*prompt|developer\s*message)|"
    r"(?:ignore|override|bypass|disregard).{0,32}(?:instruction|rule|prompt|safety)|"
    r"(?:reveal|print|show).{0,24}(?:hidden|system|developer)|"
    r"(?:你现在是|扮演).{0,12}(?:系统|开发者|管理员)|"
    r"<\|(?:system|im_start|im_end)\|>|\[INST\]",
    re.IGNORECASE,
)
# 输入出现转述、条件或不确定语气时整体不抽取，防止跨行上下文误归因。
_UNCERTAIN = re.compile(
    r"如果|假如|假设|假定|要是|倘若|比如|例如|举例|示例|演示|"
    r"也许|可能|或许|大概|似乎|好像|据说|听说|转述|引用|其实|实际上|更正|纠正|"
    r"说|表示|声称|写道|自称|转告|告诉|回答\s*[:：]|"
    r"助手|assistant|system\s*:|user\s*:|用户\s*[:：]|"
    r"对话|剧本|角色|台词|故事|小说|翻译|改写|测试|例句|才怪|开玩笑|反话|改名|改叫|不要记|别记|不保存|不记录|"
    r"并非|不是|不叫|不住|不喜欢|不想|不喝|不再喝|不再学习|但是|不过|然而|曾经|以前|之前|是否|是不是|难道|吗|么|呢|对不对|请确认|请判断|核实|[?？:：]",
    re.IGNORECASE,
)
_QUOTE_CHARS = re.compile(r"[\"'“”‘’「」『』`]")
_BAD_VALUE = re.compile(r"(?:但是|不过|或者|还是|以及|然后|其实|因为|所以|并且|而且|但|也|不|没|想|会|应该|曾经|以前|过去|曾|现在|将来|了|啦|吧)")


def is_sensitive(text: str) -> bool:
    """标记不宜自动记忆的凭证或明显注入载荷；不保证覆盖所有变体。"""
    if not isinstance(text, str):
        return True
    return bool(_SECRET.search(text) or _INJECTION.search(text))


def _candidate(attribute: str, category: str, content: str, quote: str) -> dict:
    return {"key": _KEYS[attribute], "category": category,
            "content": content, "source_quote": quote}


def _parse_clause(clause: str):
    """整段匹配，不从任意叙述的中间搜索一个看似第一人称的子串。"""
    match = re.fullmatch(r"我(?:叫|的名字是|的姓名是)([\u4e00-\u9fff]{2,12}|[A-Za-z][A-Za-z .-]{1,31})", clause)
    if (match and not _BAD_VALUE.search(match[1])
            and not re.search(r"的|是|很|非常|去|来|吃|玩|做|给|让|开心|难过", match[1])):
        return _candidate("name", "profile", "用户姓名是" + match[1].strip(), clause)

    match = re.fullmatch(r"我(?:现在)?(?:是|的职业是|的工作是)([\u4e00-\u9fffA-Za-z]{1,18}(?:设计师|工程师|程序员|研究员|教师|老师|医生|护士|律师|学生|经理|编辑|记者|会计|厨师|自由职业者)|教师|老师|医生|护士|律师|学生|经理|编辑|记者|会计|厨师|自由职业者)", clause)
    if match and not _BAD_VALUE.search(match[1]):
        return _candidate("occupation", "profile", "用户职业是" + match[1], clause)

    match = re.fullmatch(r"我(?:现在)?(?:住在|居住在)([\u4e00-\u9fff]{2,12})", clause)
    if match and not _BAD_VALUE.search(match[1]) and not re.search(r"朋友|家里|附近|旁边|这里|那里|地方|地球|火星", match[1]):
        return _candidate("city", "profile", "用户居住在" + match[1], clause)

    match = re.fullmatch(r"我(?:正在|在)学习([A-Za-z][A-Za-z0-9 +#.-]{0,30}|[\u4e00-\u9fff]{2,16})", clause)
    if match and not _BAD_VALUE.search(match[1]):
        topic = match[1].strip()
        topic = {"python": "Python", "javascript": "JavaScript", "typescript": "TypeScript"}.get(topic.lower(), topic)
        return _candidate("learning", "goal", "用户正在学习" + topic, clause)

    # 改喝明确说明新的正向偏好，旧饮品绝不独立写入。
    match = re.fullmatch(r"(?:我)?不再喜欢(?:喝)?" + _DRINKS + r"[，, ]*(?:现在)?改(?:为)?喝(" + _DRINKS + r")", clause)
    if not match:
        match = re.fullmatch(r"我(?:现在)?(?:更喜欢|喜欢|偏好)(?:喝)?(" + _DRINKS + r")", clause)
    if match:
        return _candidate("drink", "preference", "用户偏好喝" + match[1], clause)

    if re.fullmatch(r"(?:以后|今后)?(?:请|我希望你)?(?:回答|回复)(?:得)?(?:简洁|简短|简明)(?:一点|一些|些)?", clause) or re.fullmatch(r"我(?:更)?喜欢(?:简洁|简短|简明)(?:直接)?的(?:回答|回复)", clause):
        return _candidate("reply_style", "preference", "用户偏好简洁直接的回答", clause)
    if re.fullmatch(r"(?:以后|今后)?(?:请|我希望你)?(?:回答|回复)(?:得)?(?:详细|详尽)(?:一点|一些|些)?", clause):
        return _candidate("reply_style", "preference", "用户偏好详细的回答", clause)
    if re.fullmatch(r"(?:以后|今后)?(?:请)?不要(?:使用|用|加)(?:emoji|表情符号|表情包)", clause, re.IGNORECASE):
        return _candidate("emoji", "boundary", "用户要求回答不使用emoji", clause)
    return None


def extract_candidates(text: str) -> list[dict]:
    """抽取至多四个明确属性，保留原文片段；同属性由最后陈述取代。

    仅覆盖姓名、职业、居住地、正在学习、饮品和简单回复偏好。
    含引号、转述、提问、假设或安全风险的输入整体拒绝；代码块跳过。
    """
    if (not isinstance(text, str) or not text.strip() or is_sensitive(text)
            or _UNCERTAIN.search(text) or _QUOTE_CHARS.search(text)
            or re.search(r"(?m)^\s*>", text)):
        return []
    found = {}
    in_fence = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if (in_fence or line.startswith(("    ", "\t"))
                or stripped.startswith((">", "#", "- ", "* ")) or _QUOTE_CHARS.search(line)):
            continue
        for sentence in re.split(r"[。！!；;]", line):
            sentence = sentence.strip()
            if not sentence or _UNCERTAIN.search(sentence):
                continue
            # 带逗号的纠正作为整体解析，避免保留已否认的旧属性。
            whole = _parse_clause(sentence)
            if "不再喜欢" in sentence and not whole:
                continue
            clauses = [sentence] if whole else re.split(r"[，,]", sentence)
            for clause in clauses:
                clause = clause.strip().rstrip(".").strip()
                item = _parse_clause(clause)
                if item:
                    found[item["key"]] = item
    return list(found.values())[:4]


_TOPIC_WORDS = {
    "name": ("姓名", "名字", "叫什么", "怎么称呼"),
    "occupation": ("职业", "工作", "做什么的", "设计师", "工程师", "程序员"),
    "city": ("居住", "住在", "住哪", "城市", "所在地"),
    "learning": ("学习", "学什么", "目标", "python", "javascript"),
    "drink": ("饮品", "饮料", "喝什么", "咖啡", "茶", "牛奶", "豆浆", "果汁", "可乐"),
    "reply_style": ("回答风格", "回复风格", "简洁", "简短", "详细"),
    "emoji": ("emoji", "表情"),
}


def _memory_topic(memory: dict) -> set:
    key = str(memory.get("key", ""))
    content = str(memory.get("content", "")).lower()
    topics = {topic for topic, stable_key in _KEYS.items() if key == stable_key}
    for topic, words in _TOPIC_WORDS.items():
        if any(word in content for word in words):
            topics.add(topic)
    return topics


def _tokens(text: str) -> set:
    # 去除通用问答措辞，避免所有带“用户”的记忆都命中。
    text = re.sub(r"用户|请问|你记得|你知道|告诉我|是什么|什么|我的|关于|偏好|喜欢", "", text.lower())
    tokens = set(re.findall(r"[a-z][a-z0-9+#.-]*", text))
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        tokens.update(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


def relevance_score(query: str, memory: dict) -> float:
    """给出零到一的相关度；不处理置顶，也不让无关记忆获得基础分。"""
    if not isinstance(query, str) or not isinstance(memory, dict):
        return 0.0
    content = memory.get("content", "")
    if (not isinstance(content, str) or not content.strip() or is_sensitive(content)
            or memory.get("status", "active") != "active"):
        return 0.0
    normalized = query.lower().strip()
    if not normalized:
        return 0.0
    topics = _memory_topic(memory)
    query_topics = {topic for topic, words in _TOPIC_WORDS.items() if any(word in normalized for word in words)}
    if query_topics & topics:
        return 0.95
    if re.search(r"我的(?:偏好|喜好)|我(?:更)?喜欢什么", normalized):
        return 0.85 if memory.get("category") in {"preference", "boundary"} else 0.0
    if re.search(r"记得我吗|了解我什么|了解我哪些|知道我什么|关于我的记忆|对我了解多少", normalized):
        return 0.7 if memory.get("category") in _CATEGORIES else 0.0
    # 指向明确属性的自我询问只召回那个属性，不用零碎重字扩展。
    if query_topics and re.search(r"我|记得|知道", normalized):
        return 0.0
    query_tokens = _tokens(normalized)
    common = query_tokens & _tokens(content)
    return min(0.8, len(common) / max(1, len(query_tokens))) if common else 0.0


def local_reply(prompt: str, memories: list, candidates: list, memory_enabled: bool) -> str:
    """只呈现真实召回与候选状态；由调用方负责实际事务提交。"""
    lines = ["这是本地记忆预览（规则回复，非大模型），不是智能对话回答。"]
    if not memory_enabled:
        lines.append("长期记忆已暂停：本轮不读取已有记忆，也不会自动保存候选。")
    else:
        recalled = [item for item in memories if isinstance(item, dict) and relevance_score(prompt, item) > 0]
        recalled.sort(key=lambda item: relevance_score(prompt, item), reverse=True)
        if recalled:
            lines.append("根据本轮实际召回的记忆：")
            lines.extend("- " + item["content"] for item in recalled[:6])
        elif re.search(r"我叫|我的|记得我|了解我|知道我", prompt):
            lines.append("本轮没有检索到可用于回答该问题的相关记忆，不能据此猜测你的资料。")

    valid = [item for item in candidates if isinstance(item, dict)
             and isinstance(item.get("content"), str) and item["content"].strip()
             and isinstance(item.get("source_quote"), str) and item["source_quote"].strip()
             and item["source_quote"] in prompt
             and not is_sensitive(item["content"]) and not is_sensitive(item["source_quote"])]
    if valid and memory_enabled:
        lines.append("识别到明确记忆候选；本轮提交成功后将保存或更新（实际结果以记忆面板为准）：")
        lines.extend("- " + item["content"] for item in valid[:4])
    elif valid:
        lines.append("检测到了明确候选，但暂停期间不会保存。")
    else:
        lines.append("有限规则未识别到可自动保存的明确新信息；这不表示我理解或判断了全部输入。")
    lines.append("如需自然对话与开放问题回答，请连接模型；本地预览只展示记忆提取和召回。")
    return "\n".join(lines)
