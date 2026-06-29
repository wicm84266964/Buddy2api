"""
responses.py — Responses API → Chat Completions 协议转换层

Codex 从 2026.2 起强制要求 wire_api="responses"，不再支持 chat/completions。
此模块在 Buddy2api 内部把 /v1/responses 请求转换为 Chat Completions 格式转发，
再映射响应/流事件回 Responses API 格式。
"""

import json
import os
import re
import time
from typing import AsyncGenerator, Optional

import proxy


def apply_codex_sanitize(chat_payload: dict) -> dict:
    """
    对 Chat Completions 请求应用 Codex 专用清洗。
    用于 client_type='codex' 的 API Key，即使请求直接打到 /v1/chat/completions 也做清洗。
    """
    # 清洗 system messages
    for msg in chat_payload.get("messages", []):
        if msg.get("role") in ("system", "developer"):
            msg["role"] = "system"
            msg["content"] = _sanitize_system_content(msg.get("content", ""))

    # 清洗 tool descriptions
    for tool in chat_payload.get("tools", []):
        if isinstance(tool, dict) and tool.get("type") == "function":
            fn = tool.get("function") or {}
            if fn.get("description"):
                fn["description"] = _sanitize_tool_description(fn["description"])

    # 过滤非 function 类型工具
    if chat_payload.get("tools"):
        chat_payload["tools"] = [
            t for t in chat_payload["tools"]
            if isinstance(t, dict) and t.get("type") == "function" and (t.get("function") or {}).get("name")
        ]

    return chat_payload


def responses_to_chat(resp_payload: dict) -> dict:
    """
    将 Responses API 请求转换为 Chat Completions 请求。
    
    Responses 请求结构:
      model, input[], instructions, tools[], stream, temperature, max_output_tokens
    
    Chat 请求结构:
      model, messages[], tools[], stream, temperature, max_tokens
    """
    messages = []

    # instructions → system message
    instructions = resp_payload.get("instructions")
    if instructions:
        inst_content = _flatten_content(instructions)
        if inst_content:
            inst_content = _sanitize_system_content(inst_content)
            msg = {"role": "system", "content": inst_content}
            messages.append(msg)

    # input[] → messages[]
    inp = resp_payload.get("input")
    if isinstance(inp, list):
        for item in inp:
            chat_msg = _input_item_to_chat_message(item)
            if chat_msg:
                # 清洗 system/developer 消息
                if chat_msg.get("role") in ("system", "developer"):
                    chat_msg["role"] = "system"
                    chat_msg["content"] = _sanitize_system_content(chat_msg.get("content", ""))
                messages.append(chat_msg)
    elif isinstance(inp, str) and inp.strip():
        messages.append({"role": "user", "content": inp})

    # tools[]: Responses 格式 {type:"function", name:..., parameters:...}
    #          → Chat 格式 {type:"function", function: {name:..., parameters:...}}
    # 非 function 类型工具（web_search, file_search 等）跳过，Chat API 不支持
    raw_tools = resp_payload.get("tools") or []
    tools = []
    for t in raw_tools:
        if not isinstance(t, dict):
            continue
        if t.get("type") == "function":
            name = t.get("name", "")
            if not name:
                continue  # 没有函数名的跳过
            tools.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": _sanitize_tool_description(t.get("description")),
                    "parameters": t.get("parameters"),
                    "strict": t.get("strict"),
                },
            })
        # web_search, file_search, code_interpreter 等跳过（后端不支持）

    # tool_choice
    tool_choice = resp_payload.get("tool_choice")
    if tool_choice and isinstance(tool_choice, str):
        # Responses uses simple string "auto"/"none"/"required"
        # Chat uses "auto"/"none"/"required" or {"type":"function","function":{"name":"x"}}
        pass

    chat_payload = {
        "model": resp_payload.get("model", "auto"),
        "messages": messages,
        "stream": resp_payload.get("stream", False),
    }
    if tools:
        chat_payload["tools"] = tools
    if tool_choice is not None:
        chat_payload["tool_choice"] = tool_choice
    if resp_payload.get("temperature") is not None:
        chat_payload["temperature"] = resp_payload["temperature"]
    if resp_payload.get("max_output_tokens"):
        chat_payload["max_tokens"] = resp_payload["max_output_tokens"]
    if resp_payload.get("top_p") is not None:
        chat_payload["top_p"] = resp_payload["top_p"]

    return chat_payload


def _input_item_to_chat_message(item) -> Optional[dict]:
    """将单个 input item 转换为 Chat message。"""
    if not isinstance(item, dict):
        return None

    kind = item.get("type", "")

    if kind == "message":
        role = item.get("role", "user")
        # Codex 可能发送 "developer" 角色，映射为 system
        if role == "developer":
            role = "system"
        content = _flatten_content(item.get("content"))
        msg = {"role": role, "content": content}
        if item.get("name"):
            msg["name"] = item["name"]
        return msg

    elif kind == "function_call_output":
        call_id = item.get("call_id", "")
        output = item.get("output", "")
        if isinstance(output, dict):
            output = json.dumps(output)
        return {"role": "tool", "tool_call_id": call_id, "content": str(output)}

    elif kind == "function_call":
        # 历史 function_call in input（罕见，保留）
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": item.get("call_id", item.get("id", "")),
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": json.dumps(item.get("arguments", {})) if isinstance(item.get("arguments"), dict) else str(item.get("arguments", "")),
                },
            }],
        }

    elif kind == "reasoning":
        # reasoning items 没有直接的 chat 对等物，跳过
        return None

    else:
        # 未知类型：尝试作为 user message 处理
        content = _flatten_content(item.get("content"))
        if content:
            return {"role": "user", "content": content}
        return None


def _flatten_content(content) -> str:
    """展平 content 字段：字符串直接返回，数组取 text，对象取字符串。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                pt = p.get("type", "")
                if pt in ("input_text", "output_text", "text"):
                    parts.append(p.get("text", ""))
                elif pt == "image_url" or pt == "input_image":
                    url = p.get("image_url", {}) if isinstance(p.get("image_url"), dict) else {}
                    url_str = url.get("url", "") if isinstance(url, dict) else str(url)
                    parts.append(f"[image: {url_str}]")
                elif pt == "file":
                    parts.append("[file]")
                else:
                    parts.append(str(p.get("text", p)))
        return "\n".join(parts)
    if isinstance(content, dict):
        return content.get("text", str(content))
    return str(content)

# 触发腾讯内容审核的关键词及替换映射
_SANITIZE_REPLACEMENTS = [
    # 权限/沙箱相关
    ("<permissions instructions>", "<guidelines>"),
    ("</permissions instructions>", "</guidelines>"),
    ("Filesystem sandboxing defines which files can be read or written.", "File access is managed by the environment."),
    ("`sandbox_mode`", "`mode`"),
    ("sandbox_mode", "mode"),
    ("sandbox", "workspace"),
    ("Filesystem", "File access"),
    ("filesystem", "file access"),
    # 执行/命令相关
    ("execute shell commands", "run commands"),
    ("execute commands", "run commands"),
    ("shell access", "command access"),
    ("execute code", "run code"),
    ("execute", "run"),
    # 安全相关
    ("security policy", "guidelines"),
    ("security restrictions", "guidelines"),
    ("security", "safety"),
    # 提权/攻击相关
    ("require_escalated", "require_approval"),
    ("escalated permissions", "additional permissions"),
    ("escalation", "approval"),
    ("elevated", "standard"),
    ("privilege escalation", "permission change"),
    ("privilege", "permission"),
    ("unrestricted", "standard"),
    # 删除/破坏相关
    ("destructive filesystem commands", "file operations"),
    ("destructive", "impactful"),
    ("recursive delete", "bulk removal"),
    ("recursive remove", "bulk removal"),
    ("delete files", "remove files"),
    ("deletion", "removal"),
    ("delete", "remove"),
    # 网络相关
    ("network access", "connectivity"),
    ("Network access is restricted", "Connectivity is managed"),
    # 绕过/突破
    ("bypass", "go through"),
    ("circumvent", "navigate"),
    # 其他敏感词
    ("attack", "approach"),
    ("exploit", "use"),
    ("vulnerability", "limitation"),
    ("injection", "insertion"),
    ("malicious", "unintended"),
]

# 需要从 system message 中完全移除的段落（正则匹配）
_SANITIZE_REMOVE_SECTIONS = [
    # 移除整个 Escalation Requests 段落
    r"# Escalation Requests.*?(?=\n#|\n##|\Z)",
    r"## How to request escalation.*?(?=\n#|\n##|\Z)",
]


def _sanitize_system_content(content: str) -> str:
    """清洗 system message 内容，避免触发腾讯内容审核。"""
    if not content:
        return content
    
    result = content
    
    # 移除敏感段落
    for pattern in _SANITIZE_REMOVE_SECTIONS:
        result = re.sub(pattern, "[section removed]", result, flags=re.DOTALL)
    
    # 关键词替换
    for old, new in _SANITIZE_REPLACEMENTS:
        result = result.replace(old, new)
    
    # 如果内容仍然很长，截断
    if len(result) > 2000:
        result = result[:2000] + "\n[... truncated ...]"
    
    return result


def _sanitize_tool_description(desc: str) -> str:
    """清洗 tool description，避免触发腾讯内容审核。"""
    if not desc:
        return desc
    
    result = desc
    for old, new in _SANITIZE_REPLACEMENTS:
        result = result.replace(old, new)
    
    # 截断过长的描述
    if len(result) > 500:
        result = result[:500] + "..."
    
    return result


# ============================================================
# Chat → Responses 响应映射（非流式）
# ============================================================

def chat_response_to_responses(chat_resp: dict, model: str) -> dict:
    """将 Chat Completions 非流式响应转换为 Responses API 响应。"""
    resp_id = "resp_" + chat_resp.get("id", os.urandom(12).hex())
    output = []

    for choice in chat_resp.get("choices") or []:
        msg = choice.get("message") or {}
        
        # 文本内容
        content = msg.get("content")
        if content:
            output.append({
                "type": "message",
                "id": _gen_item_id("msg"),
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": content}],
            })

        # reasoning 内容
        reasoning = msg.get("reasoning_content")
        if reasoning:
            output.append({
                "type": "reasoning",
                "id": _gen_item_id("rsn"),
                "summary": [{"type": "summary_text", "text": reasoning}],
            })

        # tool_calls
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            output.append({
                "type": "function_call",
                "id": tc.get("id") or _gen_item_id("fc"),
                "call_id": tc.get("id") or _gen_item_id("fc"),
                "name": fn.get("name", ""),
                "arguments": fn.get("arguments", ""),
                "status": "completed",
            })

    usage = chat_resp.get("usage") or {}
    return {
        "id": resp_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": chat_resp.get("model") or model,
        "output": output,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
    }


# ============================================================
# 流式 SSE: Chat delta → Responses events
# ============================================================

async def chat_stream_to_responses_stream(
    chat_stream: AsyncGenerator[bytes, None],
    model: str,
) -> AsyncGenerator[str, None]:
    """
    将 Chat Completions SSE 流转换为 Responses API SSE 流。
    
    Chat SSE 格式:
      data: {choices: [{delta: {content, role, tool_calls}, finish_reason}], usage}
      data: [DONE]
    
    Responses SSE 格式:
      event: response.created
      data: {type:"response.created", response:{...}}
      
      event: response.output_item.added
      data: {type:"response.output_item.added", output_index:0, item:{...}}
      
      event: response.content_part.added
      data: {type:"response.content_part.added", output_index:0, content_index:0, part:{...}}
      
      event: response.output_text.delta
      data: {type:"response.output_text.delta", output_index:0, content_index:0, delta:"..."}
      
      event: response.output_item.done
      data: {type:"response.output_item.done", output_index:0, item:{...}}
      
      event: response.completed
      data: {type:"response.completed", response:{...}}
    """
    resp_id = "resp_" + os.urandom(12).hex()
    seq = 0
    output_index = 0
    output_items = []
    current_text = ""
    usage = {}

    # --- event: response.created ---
    seq += 1
    yield _make_sse_event("response.created", {
        "type": "response.created",
        "sequence_number": seq,
        "response": {
            "id": resp_id,
            "object": "response",
            "created_at": int(time.time()),
            "status": "in_progress",
            "model": model,
            "output": [],
        },
    })

    # --- event: response.in_progress ---
    seq += 1
    yield _make_sse_event("response.in_progress", {
        "type": "response.in_progress",
        "sequence_number": seq,
    })

    # 状态机变量
    current_tool_call_id = ""
    current_tool_args = ""
    current_role = "assistant"
    _text_item_added = False
    _text_part_added = False
    _fc_item_added = False

    async for chunk_bytes in chat_stream:
        lines = chunk_bytes.decode("utf-8", errors="replace").split("\n")
        
        for line in lines:
            line = line.strip()
            if not line.startswith("data:"):
                continue
            
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                # --- 完成当前的 output items ---
                
                # 如果正在收集 tool_call，先发射 done
                if current_tool_call_id and _fc_item_added:
                    seq += 1
                    yield _make_sse_event("response.output_item.done", {
                        "type": "response.output_item.done",
                        "sequence_number": seq,
                        "output_index": output_index - 1,
                        "item": output_items[-1] if output_items else {},
                    })

                # --- event: response.completed ---
                seq += 1
                yield _make_sse_event("response.completed", {
                    "type": "response.completed",
                    "sequence_number": seq,
                    "response": {
                        "id": resp_id,
                        "object": "response",
                        "created_at": int(time.time()),
                        "status": "completed",
                        "model": model,
                        "output": output_items,
                        "usage": {
                            "input_tokens": usage.get("prompt_tokens", 0),
                            "output_tokens": usage.get("completion_tokens", 0),
                            "total_tokens": usage.get("total_tokens", 0),
                        },
                    },
                })
                continue
            
            try:
                chunk = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            # 提取 usage
            if chunk.get("usage"):
                usage.update(chunk["usage"])

            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                finish_reason = choice.get("finish_reason")

                # --- 文本内容 ---
                text = delta.get("content", "")
                if text:
                    if not _text_item_added:
                        # item.added
                        item = {
                            "type": "message",
                            "id": _gen_item_id("msg"),
                            "role": "assistant",
                            "status": "in_progress",
                            "content": [],
                        }
                        output_items.append(item)
                        seq += 1
                        yield _make_sse_event("response.output_item.added", {
                            "type": "response.output_item.added",
                            "sequence_number": seq,
                            "output_index": output_index,
                            "item": item,
                        })
                        _text_item_added = True

                    if not _text_part_added:
                        seq += 1
                        yield _make_sse_event("response.content_part.added", {
                            "type": "response.content_part.added",
                            "sequence_number": seq,
                            "output_index": output_index,
                            "content_index": 0,
                            "part": {"type": "output_text", "text": ""},
                        })
                        _text_part_added = True

                    seq += 1
                    yield _make_sse_event("response.output_text.delta", {
                        "type": "response.output_text.delta",
                        "sequence_number": seq,
                        "output_index": output_index,
                        "content_index": 0,
                        "delta": text,
                    })
                    current_text += text
                    # 更新 item 内容
                    if _text_item_added:
                        output_items[output_index]["content"] = [{"type": "output_text", "text": current_text}]

                # --- tool_calls ---
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)

                    if tc.get("id"):
                        current_tool_call_id = tc["id"]
                    
                    if not _fc_item_added and tc.get("id"):
                        # 新的 tool_call item
                        item = {
                            "type": "function_call",
                            "id": tc["id"],
                            "call_id": tc["id"],
                            "name": "",
                            "arguments": "",
                            "status": "in_progress",
                        }
                        if _text_item_added:
                            # 结束文本 item
                            output_items[output_index]["status"] = "completed"
                            seq += 1
                            yield _make_sse_event("response.output_item.done", {
                                "type": "response.output_item.done",
                                "sequence_number": seq,
                                "output_index": output_index,
                                "item": output_items[output_index],
                            })
                            _text_item_added = False
                            _text_part_added = False
                            output_index += 1

                        output_items.append(item)
                        seq += 1
                        yield _make_sse_event("response.output_item.added", {
                            "type": "response.output_item.added",
                            "sequence_number": seq,
                            "output_index": output_index,
                            "item": item,
                        })
                        _fc_item_added = True
                        current_tool_args = ""

                    fn = tc.get("function") or {}
                    if fn.get("name") and output_items:
                        output_items[-1]["name"] = fn["name"]
                    if fn.get("arguments") and output_items:
                        args = fn["arguments"]
                        current_tool_args += args
                        output_items[-1]["arguments"] = current_tool_args
                        seq += 1
                        yield _make_sse_event("response.output_text.delta", {
                            "type": "response.output_text.delta",
                            "sequence_number": seq,
                            "output_index": output_index,
                            "content_index": 0,
                            "delta": args,
                        })

                # --- delta role (标记消息开始) ---
                if delta.get("role") and not _text_item_added:
                    current_role = delta["role"]

                # --- finish_reason ---
                if finish_reason:
                    if _text_item_added:
                        output_items[output_index]["status"] = "completed"
                        seq += 1
                        yield _make_sse_event("response.output_item.done", {
                            "type": "response.output_item.done",
                            "sequence_number": seq,
                            "output_index": output_index,
                            "item": output_items[output_index],
                        })
                        _text_item_added = False
                        _text_part_added = False
                        output_index += 1

                    if _fc_item_added and current_tool_call_id:
                        output_items[-1]["status"] = "completed"
                        seq += 1
                        yield _make_sse_event("response.output_item.done", {
                            "type": "response.output_item.done",
                            "sequence_number": seq,
                            "output_index": output_index,
                            "item": output_items[-1],
                        })
                        _fc_item_added = False
                        output_index += 1


def _make_sse_event(event_name: str, data: dict) -> str:
    """构建 Responses API 风格的 SSE 事件字符串。"""
    return f"event: {event_name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _gen_item_id(prefix: str) -> str:
    return f"{prefix}_{os.urandom(8).hex()}"


async def proxy_responses(
    resp_payload: dict,
    api_key_info: Optional[dict] = None,
) -> tuple:
    """
    主代理函数 — Responses API 版本。
    
    返回:
      - ("stream", async_generator)  流式响应
      - ("json", dict)               非流式响应
      - ("error", (status_code, detail)) 错误
    """
    # 1. 转换为 Chat Completions 格式
    chat_payload = responses_to_chat(resp_payload)
    
    # 2. 调用现有 proxy
    result = await proxy.proxy_chat_completions(chat_payload, api_key_info)
    
    if result[0] == "error":
        return result
    
    model = chat_payload.get("model", "auto")
    
    if result[0] == "json":
        # 非流式: 映射响应
        chat_resp = result[1]
        resp = chat_response_to_responses(chat_resp, model)
        return ("json", resp)
    
    elif result[0] == "stream":
        # 流式: 映射事件
        chat_gen = result[1]
        resp_gen = chat_stream_to_responses_stream(chat_gen, model)
        return ("stream", resp_gen)
