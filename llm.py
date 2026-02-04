import time
import os
from basereal import BaseReal
from logger import logger

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
    completion = client.chat.completions.create(
        model=model_name,
        messages=[{'role': 'system', 'content': 'You are a helpful assistant.'},
                  {'role': 'user', 'content': message}],
        stream=True,
        # 通过以下设置，在流式输出的最后一行展示token使用信息
        stream_options={"include_usage": True}
    )
    result=""
    first = True
    for chunk in completion:
        if len(chunk.choices)>0:
            #print(chunk.choices[0].delta.content)
            if first:
                end = time.perf_counter()
                logger.info(f"llm Time to first chunk: {end-start}s")
                first = False
            msg = chunk.choices[0].delta.content
            lastpos=0
            #msglist = re.split('[,.!;:，。！?]',msg)
            for i, char in enumerate(msg):
                if char in ",.!;:，。！？：；" :
                    result = result+msg[lastpos:i+1]
                    lastpos = i+1
                    if len(result)>10:
                        logger.info(result)
                        nerfreal.put_msg_txt(result)
                        result=""
            result = result+msg[lastpos:]
    end = time.perf_counter()
    logger.info(f"llm Time to last chunk: {end-start}s")
    nerfreal.put_msg_txt(result)    
