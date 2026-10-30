import base64
import io
import json
import wave

import numpy as np
import requests
import torch
from PIL import Image

TIMEOUT = 300
VIDEO_FRAMES_PER_VIDEO = 4


TIMEOUT = 300
VIDEO_FRAMES_PER_VIDEO = 4
MAX_IMAGE_SIDE = 1568  # VLM 友好的长边上限，超过则等比缩小
JPEG_QUALITY = 88


def image_to_data_url(frame):
    array = (frame.clamp(0, 1).mul(255).round().cpu().numpy()).astype("uint8")
    image = Image.fromarray(array)
    w, h = image.size
    if max(w, h) > MAX_IMAGE_SIDE:
        scale = MAX_IMAGE_SIDE / max(w, h)
        image = image.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
        print("[llm_bridge] image downscaled {}x{} -> {}x{}".format(w, h, *image.size), flush=True)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY)
    size_kb = len(buffer.getvalue()) / 1024
    print("[llm_bridge] image payload {}x{}: {:.0f} KB".format(*image.size, size_kb), flush=True)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()


def frames_to_data_urls(frames, max_frames=VIDEO_FRAMES_PER_VIDEO):
    """frames: [F, H, W, C]; evenly picks at most max_frames and returns data URLs."""
    total = frames.shape[0]
    picks = torch.linspace(0, total - 1, min(total, max_frames)).round().long().tolist()
    return [image_to_data_url(frames[i]) for i in picks]


def audio_to_content_part(audio):
    """audio: {waveform: [B, C, N] float -1..1, sample_rate: int}; encodes mono WAV for OpenAI-style input_audio."""
    data = audio["waveform"][0].mean(dim=0).cpu().numpy()  # [N] mono
    pcm = (np.clip(data, -1.0, 1.0) * 32767).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(audio.get("sample_rate") or audio.get("sampler_rate")))
        w.writeframes(pcm.tobytes())
    return {"type": "input_audio", "input_audio": {"data": base64.b64encode(buffer.getvalue()).decode(), "format": "wav"}}


def chat(base_url, api_key, model, messages, temperature, status_cb=None):
    if status_cb:
        status_cb("请求大模型（{} 字符上下文）…".format(len(json.dumps(messages, ensure_ascii=False))))
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    response = requests.post(
        base_url.rstrip("/") + "/chat/completions",
        json={"model": model, "messages": messages, "temperature": temperature},
        headers=headers,
        timeout=TIMEOUT,
    )
    if response.status_code != 200:
        raise RuntimeError("LLM API error {}: {}（endpoint={}, model={}）".format(
            response.status_code, response.text[:500], base_url, model))
    try:
        data = response.json()
    except ValueError as e:
        raise RuntimeError("LLM API 返回了无法解析的 JSON（endpoint={}）: {} | 原文: {}".format(
            base_url, e, response.text[:300]))
    try:
        choice = data["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError) as e:
        raise RuntimeError("LLM API 响应缺少 choices/message 结构（endpoint={}）: {} | 原文: {}".format(
            base_url, e, json.dumps(data)[:300]))
    print("[llm_bridge] finish_reason={} content_len={} reasoning_len={} tool_calls={}".format(
        choice.get("finish_reason"), len(message.get("content") or ""),
        len(message.get("reasoning_content") or ""), bool(message.get("tool_calls"))), flush=True)
    content = message.get("content") or ""
    if not content.strip():
        hint = "（回复在服务端默认输出上限处被截断）" if choice.get("finish_reason") == "length" else ""
        raise RuntimeError("模型返回了空回复，finish_reason={} {}".format(choice.get("finish_reason"), hint))
    return content


def chat_with_tools(base_url, api_key, model, messages, temperature, tools, tool_executor, max_rounds=64, status_cb=None):
    """Agent Loop：模型可发起工具调用，tool_executor(name, arguments_dict) 执行后回传，
    直到模型给出不含工具调用的最终回复；超过 max_rounds 轮抛出错误。"""
    for round_no in range(1, max_rounds + 1):
        if status_cb:
            status_cb("第 {} 轮：请求大模型…".format(round_no))
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
        payload = {"model": model, "messages": messages, "temperature": temperature, "tools": tools}
        response = requests.post(base_url.rstrip("/") + "/chat/completions",
                                 json=payload, headers=headers, timeout=TIMEOUT)
        if response.status_code != 200:
            raise RuntimeError("LLM API error (round {}) {}: {}（endpoint={}, model={}）".format(
                round_no, response.status_code, response.text[:500], base_url, model))
        try:
            data = response.json()
        except ValueError as e:
            raise RuntimeError("LLM API 返回了无法解析的 JSON（round {}, endpoint={}）: {}".format(round_no, base_url, e))
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError) as e:
            raise RuntimeError("LLM API 响应缺少 choices/message 结构（round {}）: {}".format(round_no, e))
        tool_calls = message.get("tool_calls") or []
        print("[llm_bridge] agent round {}: finish_reason={} tool_calls={}".format(
            round_no, choice.get("finish_reason"), len(tool_calls)), flush=True)
        if not tool_calls:
            content = message.get("content") or ""
            if not content.strip():
                raise RuntimeError("模型在 Agent 第 {} 轮返回了空回复，finish_reason={}".format(round_no, choice.get("finish_reason")))
            return content
        messages.append({"role": "assistant", "content": message.get("content") or "",
                         "tool_calls": [{"id": t["id"], "type": "function",
                                         "function": {"name": t["function"]["name"],
                                                      "arguments": t["function"].get("arguments", "{}")}}
                                        for t in tool_calls]})
        for t in tool_calls:
            call_name = t["function"]["name"]
            try:
                args = json.loads(t["function"].get("arguments") or "{}")
            except json.JSONDecodeError as e:
                result = "工具参数 JSON 无法解析（{}），请修正后重试: {}".format(e, t["function"].get("arguments", "")[:200])
            else:
                if status_cb:
                    summary = args.get("command") or args.get("path") or call_name
                    status_cb("第 {} 轮：执行工具 {} — {}".format(round_no, call_name, str(summary)[:48]))
                try:
                    result = tool_executor(call_name, args)
                except Exception as e:
                    result = "工具执行失败: {}".format(e)
            if not isinstance(result, str):
                result = json.dumps(result, ensure_ascii=False)
            print("[llm_bridge] agent round {} tool {}: {}".format(round_no, call_name, str(result)[:120]), flush=True)
            messages.append({"role": "tool", "tool_call_id": t["id"], "content": result[:20000]})
    raise RuntimeError("Agent 超过最大工具轮数（{} 轮）仍未给出最终回复".format(max_rounds))
