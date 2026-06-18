"""
LLM 意图分析器
==============
调用大语言模型分析消息，判断是否需要目标用户回复/处理。

核心能力：
  1. 分析单条消息是否与目标用户相关
  2. 判断消息是否包含明确的问题/请求/待办
  3. 评估紧急程度
  4. 生成简洁摘要

设计原则：
  - 使用 OpenAI 兼容接口，支持任何兼容的 LLM 服务
  - 低温度保证输出稳定性
  - JSON 格式化输出便于解析
  - 容错处理：LLM 调用失败不影响主流程
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
# Prompt 模板
# ---------------------------------------------------------------------------

REPLY_DETECTION_SYSTEM_PROMPT = """你是一个智能消息分析助手。你的任务是分析群聊消息，判断这些消息中是否有需要目标用户回复或处理的内容。

目标用户信息：
- 用户ID: {target_user_id}
- 用户名: {target_user_name}

【判定规则】
一条消息"需要目标用户回复"的条件（满足任一即可）：
1. 消息中直接 @了目标用户 或明确提到了目标用户的名字
2. 消息包含对目标用户的明确提问（问号结尾、请求确认等）
3. 消息请求目标用户做某事（审批、决策、提供信息等）
4. 消息是对目标用户之前发言的追问或质疑
5. 消息涉及目标用户负责的领域，且包含需要决策的问题

一条消息"不需要回复"的条件：
1. 纯闲聊、表情包、打招呼
2. 已经有其他人回复解决了的问题
3. 通知类消息（不需要回应）
4. 与目标用户完全无关的讨论

【紧急程度判定】
- high: 直接@目标用户 + 明确问题/请求，或涉及紧急事项（线上问题、阻塞等）
- medium: 间接相关，需要关注但不紧急
- low: 可能相关，建议了解

请对每条消息输出 JSON 格式的分析结果。"""

BATCH_ANALYSIS_USER_PROMPT = """以下是群聊「{chat_name}」的最近消息记录：

{messages_text}

请分析以上消息，找出所有需要目标用户回复或处理的消息。

输出格式（JSON）：
{{
  "needs_reply": [
    {{
      "message_index": 消息序号(从1开始),
      "reason": "需要回复的原因（一句话）",
      "summary": "消息摘要（便于快速了解上下文）",
      "urgency": "high/medium/low",
      "confidence": 0.0-1.0
    }}
  ],
  "total_analyzed": 消息总数,
  "relevant_count": 与目标用户相关的消息数
}}

注意：
- 只输出确实需要回复的消息，不要过度判定
- confidence 低于 0.6 的不要输出
- 必须输出合法 JSON"""


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

    if not settings.LLM_API_KEY:
        logger.warning("LLM_API_KEY 未配置，跳过 AI 分析")
        return []

    # 构建消息文本
    lines = []
    for i, msg in enumerate(messages, 1):
        sender = msg.get("sender_name", "unknown")
        content = msg.get("content", "")
        time_str = msg.get("created_at", "")
        lines.append(f"[{i}] [{time_str}] {sender}: {content}")

    messages_text = "\n".join(lines)

    # 截断过长的消息文本（避免超出 token 限制）
    if len(messages_text) > 8000:
        messages_text = messages_text[:8000] + "\n...(消息过多，已截断)"

    system_prompt = REPLY_DETECTION_SYSTEM_PROMPT.format(
        target_user_id=target_user_id or settings.LARK_TARGET_USER_ID,
        target_user_name=target_user_name or "目标用户",
    )

    user_prompt = BATCH_ANALYSIS_USER_PROMPT.format(
        chat_name=chat_name or "未知群组",
        messages_text=messages_text,
    )

    try:
        client = _get_client()
        response = client.chat.completions.create(
            model=settings.LLM_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=2000,
        )
        raw_content = response.choices[0].message.content.strip()
        result = _parse_llm_json(raw_content)

        needs_reply = result.get("needs_reply", [])
        logger.info(
            "LLM 分析完成: chat=%s, 总消息=%d, 需回复=%d",
            chat_name, len(messages), len(needs_reply),
        )
        return needs_reply

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
# 单条消息快速判定（用于 Webhook 实时场景）
# ---------------------------------------------------------------------------

QUICK_CHECK_PROMPT = """判断以下消息是否需要用户「{target_user_name}」回复。

消息来自群「{chat_name}」，发送者: {sender_name}
消息内容: {content}

仅输出 JSON:
{{"needs_reply": true/false, "reason": "原因", "urgency": "high/medium/low", "confidence": 0.0-1.0}}"""


def quick_check_single(
    content: str,
    sender_name: str = "",
    chat_name: str = "",
    target_user_name: str = "",
) -> Optional[Dict]:
    """
    快速判定单条消息是否需要回复。
    用于 Webhook 实时接收场景。
    返回 None 表示不需要回复。
    """
    if not settings.LLM_API_KEY:
        return None

    prompt = QUICK_CHECK_PROMPT.format(
        target_user_name=target_user_name or "目标用户",
        chat_name=chat_name,
        sender_name=sender_name,
        content=content[:500],
    )

    try:
        client = _get_client()
        response = client.chat.completions.create(
            model=settings.LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=200,
        )
        result = _parse_llm_json(response.choices[0].message.content.strip())
        if result.get("needs_reply") and result.get("confidence", 0) >= 0.6:
            return result
        return None
    except Exception as e:
        logger.error("快速判定失败: %s", str(e))
        return None
