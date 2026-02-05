import time
import os
from basereal import BaseReal
from logger import logger

SYSTEM_PROMPT = (
    "You are a helpful assistant for a live video avatar. "
    "Always reply in the same language as the user's last message. "
    "Keep responses concise and natural for spoken audio. "
    "Do not repeat or quote the user's message. "
    "Do not append the user's message at the end."
)
MAX_HISTORY = 10


def _get_history(nerfreal: BaseReal):
    history = getattr(nerfreal, "chat_history", None)
    if history is None:
        history = []
        nerfreal.chat_history = history
    return history


def _append_history(history, role, content):
    if not content:
        return
    history.append({"role": role, "content": content})
    if len(history) > MAX_HISTORY:
        del history[:-MAX_HISTORY]


def _strip_user_echo(text: str, user_msg: str) -> str:
    if not text or not user_msg:
        return text
    user_clean = user_msg.strip()
    if not user_clean:
        return text
    trimmed = text.strip()
    if trimmed == user_clean:
        return ""
    if trimmed.endswith(user_clean):
        trimmed = trimmed[: -len(user_clean)].rstrip()
    return trimmed


def llm_response(message,nerfreal:BaseReal):
    start = time.perf_counter()
    from openai import OpenAI
    openai_key = getattr(nerfreal, "openai_api_key", "") or os.getenv("OPENAI_API_KEY", "")
    openai_base = getattr(nerfreal, "openai_base_url", "") or os.getenv("OPENAI_BASE_URL", "")
    openai_model = getattr(nerfreal, "openai_model", "") or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    dashscope_key = os.getenv("DASHSCOPE_API_KEY", "")

    if openai_key:
        client = OpenAI(
            api_key=openai_key,
            base_url=openai_base if openai_base else None,
        )
        model_name = openai_model
    elif dashscope_key:
        client = OpenAI(
            # 如果您没有配置环境变量，请在此处用您的API Key进行替换
            api_key=dashscope_key,
            # 填写DashScope SDK的base_url
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        model_name = "qwen-plus"
    else:
        raise RuntimeError("No LLM API key configured.")
    end = time.perf_counter()
    logger.info(f"llm Time init: {end-start}s")
    history = _get_history(nerfreal)
    prompt_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history[-MAX_HISTORY:] + [
        {"role": "user", "content": message}
    ]
    _append_history(history, "user", message)

    completion = client.chat.completions.create(
        model=model_name,
        messages=prompt_messages,
        stream=True,
        stream_options={"include_usage": True}
    )
    result=""
    full_response=""
    first = True
    for chunk in completion:
        if len(chunk.choices)>0:
            #print(chunk.choices[0].delta.content)
            if first:
                end = time.perf_counter()
                logger.info(f"llm Time to first chunk: {end-start}s")
                first = False
            msg = chunk.choices[0].delta.content
            if msg:
                full_response += msg
            lastpos=0
            #msglist = re.split('[,.!;:，。！?]',msg)
            for i, char in enumerate(msg):
                if char in ",.!;:，。！？：；" :
                    result = result+msg[lastpos:i+1]
                    lastpos = i+1
                    if len(result)>10:
                        cleaned = _strip_user_echo(result, message)
                        if cleaned:
                            logger.info(cleaned)
                            nerfreal.put_msg_txt(cleaned)
                        result=""
            result = result+msg[lastpos:]
    end = time.perf_counter()
    logger.info(f"llm Time to last chunk: {end-start}s")
    cleaned_final = _strip_user_echo(result, message)
    if cleaned_final:
        nerfreal.put_msg_txt(cleaned_final)
    full_response = _strip_user_echo(full_response, message)
    _append_history(history, "assistant", full_response)
