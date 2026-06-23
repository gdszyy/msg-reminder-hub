"""
对话分离器 (Thread Splitter)
============================
将一个群内的大量消息拆分为独立的对话线程，再逐线程分析。

解决的问题：
  - 500 条消息里穿插了 20 几个独立对话，简单按数量切批会把对话切断
  - 某个话题刚好卡在两个批次中间，LLM 无法理解完整上下文
  - 横穿多个对话的消息需要被正确归属

方案（移植自 ai-secretary-architecture/scripts/thread_separator.py）：
  1. 按时间窗口切分 Session（超过 30 分钟无消息视为新 Session）
  2. 在每个 Session 内，基于 reply_to 关系 + @提及 + 发送者连续性做规则聚类
  3. 将聚类后的线程（而非原始消息列表）发给 LLM 分析

这样 LLM 每次收到的是一个完整的对话线程（通常 5~30 条），而不是被截断的片段。
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Tuple
from collections import defaultdict

logger = logging.getLogger("msg_reminder.thread_splitter")

# 时间窗口：超过此间隔（分钟）视为新 Session
SESSION_GAP_MINUTES = 30


# ---------------------------------------------------------------------------
# Step 1: 按时间窗口切分 Session
# ---------------------------------------------------------------------------

def split_by_time_window(messages: List[Dict], gap_minutes: int = SESSION_GAP_MINUTES) -> List[List[Dict]]:
    """
    按时间窗口将消息切分为多个 Session。
    如果两条消息之间超过 gap_minutes 分钟，视为新 Session。
    """
    if not messages:
        return []

    sessions = []
    current_session = [messages[0]]

    for msg in messages[1:]:
        prev_time = current_session[-1].get("created_at")
        curr_time = msg.get("created_at")

        if prev_time and curr_time:
            # 支持 datetime 对象和字符串
            if isinstance(prev_time, str):
                try:
                    prev_time = datetime.fromisoformat(prev_time)
                except ValueError:
                    prev_time = None
            if isinstance(curr_time, str):
                try:
                    curr_time = datetime.fromisoformat(curr_time)
                except ValueError:
                    curr_time = None

            if prev_time and curr_time:
                gap = (curr_time - prev_time).total_seconds() / 60
                if gap > gap_minutes:
                    sessions.append(current_session)
                    current_session = [msg]
                    continue

        current_session.append(msg)

    if current_session:
        sessions.append(current_session)

    return sessions


# ---------------------------------------------------------------------------
# Step 2: 在 Session 内基于规则聚类为对话线程
# ---------------------------------------------------------------------------

def cluster_into_threads(messages: List[Dict]) -> List[List[Dict]]:
    """
    在一个 Session 内，基于以下规则将消息聚类为对话线程：
      1. reply_to 关系：回复同一条消息的归为一组
      2. 发送者连续性：同一个人连续发的多条消息归为一组
      3. @提及关系：A @了 B，B 的回复归入同一线程
      4. 时间邻近 + 话题相关：5 分钟内的消息倾向于归为同一线程

    返回：线程列表，每个线程是一组消息（按时间正序）
    """
    if not messages:
        return []

    # 如果消息很少（≤15条），不拆分，整体作为一个线程
    if len(messages) <= 15:
        return [messages]

    # 构建 reply_to 图：找出所有对话链
    # reply_chains: {root_msg_index: [msg_indices]}
    msg_index_map = {}  # msg_id -> index
    for i, msg in enumerate(messages):
        msg_id = msg.get("platform_msg_id", "") or msg.get("id", str(i))
        msg_index_map[msg_id] = i

    # Union-Find 来合并对话
    parent = list(range(len(messages)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    # 规则 1: reply_to 关系
    for i, msg in enumerate(messages):
        reply_to = msg.get("reply_to_id", "")
        if reply_to and reply_to in msg_index_map:
            union(i, msg_index_map[reply_to])

    # 规则 2: 同一发送者 5 分钟内的连续消息
    for i in range(1, len(messages)):
        if messages[i].get("sender_id") == messages[i-1].get("sender_id"):
            time_a = messages[i-1].get("created_at")
            time_b = messages[i].get("created_at")
            if time_a and time_b and isinstance(time_a, datetime) and isinstance(time_b, datetime):
                if (time_b - time_a).total_seconds() < 300:  # 5 分钟
                    union(i, i-1)

    # 规则 3: @提及关系（A @了 B，B 紧接着回复）
    for i in range(1, len(messages)):
        content_prev = messages[i-1].get("content", "")
        sender_curr = messages[i].get("sender_name", "")
        if sender_curr and f"@{sender_curr}" in content_prev:
            union(i, i-1)
        # 反向：当前消息 @了上一条的发送者
        content_curr = messages[i].get("content", "")
        sender_prev = messages[i-1].get("sender_name", "")
        if sender_prev and f"@{sender_prev}" in content_curr:
            union(i, i-1)

    # 规则 4: 时间邻近（2 分钟内的相邻消息倾向合并，除非发送者完全不同）
    for i in range(1, len(messages)):
        time_a = messages[i-1].get("created_at")
        time_b = messages[i].get("created_at")
        if time_a and time_b and isinstance(time_a, datetime) and isinstance(time_b, datetime):
            gap = (time_b - time_a).total_seconds()
            if gap < 120:  # 2 分钟内
                # 检查是否有对话关联（同一组发送者）
                root_prev = find(i-1)
                participants_prev = set()
                for j in range(len(messages)):
                    if find(j) == root_prev:
                        participants_prev.add(messages[j].get("sender_id", ""))
                if messages[i].get("sender_id", "") in participants_prev:
                    union(i, i-1)

    # 收集线程
    thread_map = defaultdict(list)
    for i in range(len(messages)):
        root = find(i)
        thread_map[root].append(i)

    # 转换为消息列表
    threads = []
    for indices in thread_map.values():
        thread_msgs = [messages[i] for i in sorted(indices)]
        threads.append(thread_msgs)

    # 按第一条消息的时间排序
    threads.sort(key=lambda t: t[0].get("created_at", "") or "")

    logger.debug("Session 内聚类: %d 条消息 → %d 个线程", len(messages), len(threads))
    return threads


# ---------------------------------------------------------------------------
# 主入口：将消息列表拆分为对话线程
# ---------------------------------------------------------------------------

def split_messages_into_threads(messages: List[Dict]) -> List[List[Dict]]:
    """
    将一个群的消息列表拆分为独立的对话线程。

    流程：
      1. 按时间窗口切分 Session
      2. 在每个 Session 内基于规则聚类
      3. 返回所有线程（每个线程是一组按时间排序的消息）

    参数:
      messages: 消息列表，每条需包含:
        - content: 文本内容
        - sender_id: 发送者ID
        - sender_name: 发送者名称
        - created_at: datetime 对象
        - reply_to_id: 回复的消息ID（可选）
        - platform_msg_id: 消息ID（可选）

    返回:
      List[List[Dict]] - 线程列表，每个线程是一组消息
    """
    if not messages:
        return []

    # Step 1: 按时间窗口切分 Session
    sessions = split_by_time_window(messages)
    logger.info("消息切分为 %d 个 Session（总 %d 条）", len(sessions), len(messages))

    # Step 2: 在每个 Session 内聚类
    all_threads = []
    for session_msgs in sessions:
        threads = cluster_into_threads(session_msgs)
        all_threads.extend(threads)

    logger.info("聚类完成: %d 条消息 → %d 个对话线程", len(messages), len(all_threads))
    return all_threads
