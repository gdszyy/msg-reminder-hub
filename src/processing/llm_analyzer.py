"""
LLM 意图分析器
==============
调用大语言模型分析消息，判断是否需要目标用户回复/处理。

核心能力：
  1. 批量分析一组消息，识别需要目标用户回复的
  2. 特别关注 @提及目标用户 的消息（直接标记为 high）
  3. 支持上下文窗口：分析时带入前后消息，提高判断准确度
  4. 评估紧急程度 (high / medium / low)
  5. 生成简洁摘要

设计原则：
  - 使用 OpenAI 兼容接口，支持任何兼容的 LLM 服务
  - 低温度保证输出稳定性
  - JSON 格式化输出便于解析
  - 容错处理：LLM 调用失败不影响主流程
  - Lark 场景优化：识别 [TARGET_USER] 标记、@提及、问号结尾等模式
"""

import json
import logging
import re
from typing import Dict, List, Optional

from openai import OpenAI

from config import settings

logger = logging.getLogger("msg_reminder.llm_analyzer")

# ---------------------------------------------------------------------------
# LLM 客户端
# ---------------------------------------------------------------------------

_client: Optional[OpenAI] = None


def _get_client() -> OpenAI:
    """获取 LLM 客户端单例"""
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=settings.LLM_API_KEY,
            base_url=settings.LLM_BASE_URL,
        )
    return _client


# ---------------------------------------------------------------------------
# Prompt 模板（Lark 场景优化）
# ---------------------------------------------------------------------------

REPLY_DETECTION_SYSTEM_PROMPT = """你是一个智能消息分析助手。你的任务是分析群聊/私聊消息，判断哪些消息需要目标用户回复或处理。

【目标用户标识】
- 消息中出现 [TARGET_USER] 标记表示该消息直接 @了目标用户
- 消息中出现 [提及: ...TARGET_USER...] 表示提到了目标用户

【判定规则 — 需要回复】
满足以下任一条件，判定为"需要回复"：
1. 消息中有 [TARGET_USER] 标记（直接@了目标用户）→ 几乎一定需要回复
2. 消息包含对目标用户的明确提问（问号结尾、"请问"、"能否"、"帮忙"等）
3. 消息请求目标用户做决策（"你看看"、"你定"、"等你确认"等）
4. 消息是对目标用户之前发言的追问或反驳
5. 消息涉及目标用户负责的事项，且包含需要回应的问题
6. 有人在等目标用户的回复才能继续推进工作

【判定规则 — 不需要回复】
满足以下条件，判定为"不需要回复"：
1. 纯闲聊、表情包、打招呼、"好的"、"收到"
2. 已经有其他人回复解决了的问题
3. 通知类/公告类消息（不需要回应）
4. 与目标用户完全无关的讨论
5. 目标用户自己发的消息
6. 机器人/系统消息

【紧急程度】
- high: 直接@目标用户 + 明确问题/请求；线上故障/阻塞；有截止时间的事项
- medium: 间接相关，需要关注但不紧急；讨论中提到了目标用户的领域
- low: 可能相关，建议了解但不回复也没关系

请严格按 JSON 格式输出分析结果。"""

BATCH_ANALYSIS_USER_PROMPT = """以下是「{chat_name}」的最近消息记录（共 {msg_count} 条）：

{messages_text}

请分析以上消息，找出所有需要目标用户回复或处理的消息。

输出格式（严格 JSON）：
{{
  "needs_reply": [
    {{
      "message_index": 消息序号(从1开始),
      "reason": "需要回复的原因（一句话，简洁明了）",
      "summary": "消息核心内容摘要（让目标用户快速了解是什么事）",
      "urgency": "high/medium/low",
      "confidence": 0.0到1.0之间的数字
    }}
  ],
  "total_analyzed": {msg_count},
  "relevant_count": 与目标用户相关的消息数
}}

注意：
- 只输出确实需要回复的消息，宁可漏掉也不要误报
- confidence 低于 0.6 的不要输出
- 如果没有需要回复的消息，needs_reply 为空数组
- 必须输出合法 JSON，不要有注释"""


# ---------------------------------------------------------------------------
# 规则预筛选（减少 LLM 调用）
# ---------------------------------------------------------------------------

def _rule_based_precheck(messages: List[Dict]) -> List[Dict]:
    """
    规则预筛选：直接 @目标用户 的消息无需 LLM 即可判定为 high。
    返回确定需要回复的消息列表。
    """
    results = []
    for msg in messages:
        content = msg.get("content", "")
        index = msg.get("index", 0)

        # 规则1: 直接 @了目标用户
        if "[TARGET_USER]" in content:
            # 排除纯 @没有内容的情况
            clean = content.replace("[TARGET_USER]", "").replace("[提及:", "").strip()
            clean = re.sub(r'\[提及:[^\]]*\]', '', clean).strip()
            if len(clean) > 5:  # 有实质内容
                results.append({
                    "message_index": index,
                    "reason": "直接@了你",
                    "summary": clean[:100],
                    "urgency": "high",
                    "confidence": 0.95,
                    "source": "rule",
                })

    return results


# ---------------------------------------------------------------------------
# 核心分析函数
# ---------------------------------------------------------------------------

def analyze_messages(
    messages: List[Dict],
    chat_name: str = "",
    target_user_id: str = "",
    target_user_name: str = "",
) -> List[Dict]:
    """
    分析一批消息，识别需要目标用户回复的消息。

    参数:
      messages: 标准化消息列表，每条包含 {index, sender_name, content, created_at}
      chat_name: 群组名称
      target_user_id: 目标用户ID
      target_user_name: 目标用户名称

    返回:
      需要回复的消息列表，每条包含 {message_index, reason, summary, urgency, confidence}
    """
    if not messages:
        return []

    # Step 1: 规则预筛选
    rule_results = _rule_based_precheck(messages)

    # Step 2: LLM 分析（如果配置了 API Key）
    llm_results = []
    if settings.LLM_API_KEY:
        llm_results = _llm_analyze(messages, chat_name, target_user_id, target_user_name)
    else:
        logger.warning("LLM_API_KEY 未配置，仅使用规则判定")

    # Step 3: 合并结果（去重，以 message_index 为 key）
    seen_indices = set()
    final_results = []

    # 规则结果优先（置信度更高）
    for r in rule_results:
        idx = r["message_index"]
        if idx not in seen_indices:
            seen_indices.add(idx)
            final_results.append(r)

    # LLM 结果补充
    for r in llm_results:
        idx = r.get("message_index", 0)
        if idx not in seen_indices:
            seen_indices.add(idx)
            final_results.append(r)

    # 按紧急程度排序
    urgency_order = {"high": 0, "medium": 1, "low": 2}
    final_results.sort(key=lambda x: urgency_order.get(x.get("urgency", "low"), 2))

    logger.info(
        "分析完成: chat=%s, 总消息=%d, 规则命中=%d, LLM命中=%d, 最终=%d",
        chat_name, len(messages), len(rule_results), len(llm_results), len(final_results),
    )
    return final_results


def _llm_analyze(
    messages: List[Dict],
    chat_name: str,
    target_user_id: str,
    target_user_name: str,
) -> List[Dict]:
    """调用 LLM 进行深度分析"""
    # 构建消息文本
    lines = []
    for msg in messages:
        i = msg.get("index", 0)
        sender = msg.get("sender_name", "unknown")
        content = msg.get("content", "")
        time_str = msg.get("created_at", "")
        lines.append(f"[{i}] [{time_str}] {sender}: {content}")

    messages_text = "\n".join(lines)

    # 截断过长的消息文本
    if len(messages_text) > 8000:
        messages_text = messages_text[:8000] + "\n...(消息过多，已截断)"

    user_prompt = BATCH_ANALYSIS_USER_PROMPT.format(
        chat_name=chat_name or "未知群组",
        messages_text=messages_text,
        msg_count=len(messages),
    )

    try:
        client = _get_client()
        response = client.chat.completions.create(
            model=settings.LLM_MODEL,
            messages=[
                {"role": "system", "content": REPLY_DETECTION_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=2000,
        )
        raw_content = response.choices[0].message.content.strip()
        result = _parse_llm_json(raw_content)
        return result.get("needs_reply", [])

    except Exception as e:
        logger.error("LLM 分析失败: %s", str(e))
        return []


def _parse_llm_json(raw: str) -> dict:
    """解析 LLM 返回的 JSON，处理 markdown 代码块包裹的情况"""
    if "```" in raw:
        match = re.search(r"```(?:json)?\s*([\s\S]+?)```", raw)
        if match:
            raw = match.group(1)
    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError as e:
        logger.warning("JSON 解析失败: %s, raw=%s", e, raw[:200])
        return {"needs_reply": [], "total_analyzed": 0, "relevant_count": 0}


# ---------------------------------------------------------------------------
# 上下文窗口分析（用于 Webhook 实时场景）
# ---------------------------------------------------------------------------

CONTEXT_ANALYSIS_PROMPT = """分析以下对话上下文，判断最后一条消息（标记为 >>>）是否需要目标用户回复。

对话上下文（群「{chat_name}」）：
{context_text}

>>> 最新消息: [{sender}] {content}

目标用户标识: 消息中的 [TARGET_USER] 表示 @了目标用户。

仅输出 JSON（不要其他内容）:
{{"needs_reply": true或false, "reason": "原因", "summary": "摘要", "urgency": "high/medium/low", "confidence": 0.0-1.0}}"""


def analyze_with_context(
    target_message: Dict,
    context_messages: List[Dict],
    chat_name: str = "",
) -> Optional[Dict]:
    """
    带上下文的单条消息分析。
    用于 Webhook 实时接收场景，将最新消息连同上下文一起分析。

    参数:
      target_message: 目标消息 {sender_name, content}
      context_messages: 上下文消息列表（时间正序，最新的在最后）
      chat_name: 群组名称

    返回:
      需要回复时返回 {reason, summary, urgency, confidence}，否则返回 None
    """
    content = target_message.get("content", "")

    # 快速规则判定
    if "[TARGET_USER]" in content:
        clean = re.sub(r'\[提及:[^\]]*\]', '', content).strip()
        if len(clean) > 5:
            return {
                "reason": "直接@了你",
                "summary": clean[:100],
                "urgency": "high",
                "confidence": 0.95,
            }

    # LLM 分析
    if not settings.LLM_API_KEY:
        return None

    # 构建上下文文本
    context_lines = []
    for msg in context_messages[-10:]:  # 最多取最近 10 条上下文
        sender = msg.get("sender_name", "?")
        text = msg.get("content", "")
        context_lines.append(f"[{sender}] {text}")

    context_text = "\n".join(context_lines) if context_lines else "(无上下文)"

    prompt = CONTEXT_ANALYSIS_PROMPT.format(
        chat_name=chat_name,
        context_text=context_text,
        sender=target_message.get("sender_name", "unknown"),
        content=content[:500],
    )

    try:
        client = _get_client()
        response = client.chat.completions.create(
            model=settings.LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=300,
        )
        result = _parse_llm_json(response.choices[0].message.content.strip())
        if result.get("needs_reply") and result.get("confidence", 0) >= 0.6:
            return {
                "reason": result.get("reason", ""),
                "summary": result.get("summary", ""),
                "urgency": result.get("urgency", "medium"),
                "confidence": result.get("confidence", 0.0),
            }
        return None
    except Exception as e:
        logger.error("上下文分析失败: %s", str(e))
        return None
