# OpenAI 兼容 API 服务：流式、思考与工具调用

> 对应源码：`scripts/serve_openai_api.py`（服务端）、`scripts/chat_api.py`（OpenAI SDK 客户端示例）、`eval_llm.py`（对照用的 CLI 推理入口）。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚为什么 MiniMind 要再包一层「兼容 OpenAI 协议」的 HTTP 服务，它解决了什么问题；
- 读懂 `serve_openai_api.py` 的四个核心部件：`ChatRequest`（请求模型）、`CustomStreamer`（线程队列流式）、`parse_response`（思考与工具调用解析）、`/v1/chat/completions`（FastAPI 端点 + SSE 增量切分）；
- 解释流式输出（streaming）如何用一个「后台线程生产 + 主线程消费」的 `Queue` 桥接阻塞式的 `model.generate` 与 HTTP 长连接；
- 动手启动服务，分别用 `open_thinking` 与 `tools` 两个字段调用，观察 `reasoning_content` 与 `tool_calls` 的返回结构。

## 2. 前置知识

本讲是「专家层」内容，但在算法上并不难——它的难点在**工程拼接**：把前面讲过的模型推理、`chat_template`、思考/工具标签，组装成一个对外的网络服务。开始前，请确认你已经理解以下概念（不熟悉的可先回看对应讲义）：

- **OpenAI Chat Completions 协议**：业界事实标准的对话接口。请求体里关键字段是 `model`、`messages`（多轮对话列表）、`temperature`/`top_p`/`max_tokens` 等采样参数、`stream`（是否流式）、`tools`（工具定义）。响应里关键字段是 `choices[0].message.content`（最终回答），流式时变成一连串 `choices[0].delta.content`（增量片段）。本讲的服务就是把 MiniMind 包装成这个协议的形状。
- **SSE（Server-Sent Events，服务器推送事件）**：一种在一条 HTTP 连接上「服务器持续向下推文本」的约定。每条消息形如 `data: <一段 JSON>\n\n`（两个换行结尾表示一条事件结束）。OpenAI 的流式接口就是基于 SSE。MiniMind 用 FastAPI 的 `StreamingResponse` 配合手写的 `data: ...\n\n` 前缀实现了它。
- **`reasoning_content` 与 `tool_calls`**：这是 OpenAI 协议在「思考型模型」和「函数调用」上的两处扩展。`reasoning_content` 承载模型的思考过程（对应 MiniMind 里的 `<think>...</think>`），`tool_calls` 承载结构化的函数调用请求（对应 `<tool_call>...</tool_call>`）。本讲的核心任务之一，就是把模型吐出的原始标签文本，**解析**成这两个结构化字段。
- **TextStreamer**（承接 [u3-l6 自定义 generate](./u3-l6-custom-generate.md)）：HuggingFace 提供的「逐 token 打印」回调，每解码出一段文本就回调 `on_finalized_text`。CLI 推理里它直接 `print`；本讲里它被改造成往队列里塞文本。
- **chat_template 与 open_thinking**（承接 [u2-l1](./u2-l1-tokenizer-and-chat-template.md)）：`open_thinking=1` 时模板只注入半个 `<think>\n` 起始标签，让模型自己续写思考内容直到 `</think>`；`open_thinking=0` 时模板注入一整个空的 `<think>\n\n</think>\n\n`，模型直接作答。本讲服务端要做的，就是把用户传来的 `open_thinking` 透传给 `apply_chat_template`，并在流式输出时按 `</think>` 把「思考」和「正文」切到不同字段。

一句话概括本讲定位：**把 MiniMind 的 `model.generate` 套进 OpenAI 协议的壳，重点解决「流式」「思考」「工具调用」这三件 OpenAI 协议有、但原生 generate 没有 的东西。**

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [scripts/serve_openai_api.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/serve_openai_api.py) | 兼容 OpenAI 协议的轻量服务端 | 全部四个最小模块都在这里 |
| [scripts/chat_api.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/chat_api.py) | 用官方 `openai` SDK 调用服务的客户端示例 | `extra_body` 如何开启思考；流式如何读 `reasoning_content` |
| [eval_llm.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py) | CLI 推理入口（对照） | 同样的 `apply_chat_template(..., open_thinking=...)` 在 CLI 里长什么样 |

> 小提示：服务端脚本里的权重路径写成了 `../{save_dir}/...`（见 [scripts/serve_openai_api.py:32](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/serve_openai_api.py#L32)），而 `eval_llm.py` 写成 `./{save_dir}/...`（[eval_llm.py:22](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py#L22)）。这是因为 README 要求 `cd scripts && python serve_openai_api.py` 在 `scripts/` 目录下运行，所以要多退一层到仓库根的 `out/`。后续做实践时请记住这个目录差异。

---

## 4. 核心概念与源码讲解

### 4.1 ChatRequest：请求模型与 open_thinking 的多入口兼容

#### 4.1.1 概念说明

OpenAI 协议把请求体定义成一段 JSON，服务端需要一段代码去「描述这个 JSON 长什么样、字段类型是什么、缺省值是多少」。在 Python/FastAPI 生态里，这件事由 **Pydantic** 的 `BaseModel` 完成：你声明一个类，把字段写成类属性并给默认值，Pydantic 就会自动帮你做类型校验、填默认值、把进来的 JSON 反序列化成这个类的实例。

MiniMind 定义了 `ChatRequest` 这个请求模型。它基本照搬 OpenAI 协议字段，又额外加了两个 MiniMind 特色字段：`open_thinking`（思考开关）和 `tools`（工具定义，协议里本来就有）。难点在于 **`open_thinking` 的传法不止一种**——有人直接在顶层传 `open_thinking: true`，有人（比如官方 `openai` SDK）只能通过 `extra_body={"chat_template_kwargs": {"open_thinking": True}}` 间接传。`ChatRequest` 用一个 `get_open_thinking()` 方法做了多入口兼容。

#### 4.1.2 核心流程

`ChatRequest` 的生命周期：

1. 客户端发来一段 JSON 请求体；
2. FastAPI 把它交给 Pydantic，按字段定义校验并构造 `ChatRequest` 实例（缺字段用默认值）；
3. 业务代码（端点函数）调用 `request.get_open_thinking()` 得到一个统一的 `bool`，决定要不要开思考。

判断 `open_thinking` 的优先级逻辑（伪代码）：

```
如果 顶层 open_thinking 为 True          → 开
否则 如果 chat_template_kwargs 存在：
        其中 open_thinking 或 enable_thinking 为 True → 开
否则 → 不开
```

#### 4.1.3 源码精读

请求模型定义与字段默认值：[scripts/serve_openai_api.py:50-59](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/serve_openai_api.py#L50-L59)

```python
class ChatRequest(BaseModel):
    model: str
    messages: list
    temperature: float = 0.7
    top_p: float = 0.92
    max_tokens: int = 8192
    stream: bool = True
    tools: list = Field(default_factory=list)
    open_thinking: bool = False
    chat_template_kwargs: dict = None
```

说明：
- `model`、`messages` 没有默认值，说明这两个字段**必填**（缺了 Pydantic 会直接报 422 错误）。
- `stream` 默认 `True`，即 MiniMind 这个服务**默认走流式**，和 OpenAI 官方默认（非流式）略有不同，调试时要留意。
- `tools` 用 `Field(default_factory=list)` 而不是 `= []`，这是 Pydantic 的规范写法——可变默认值必须用工厂函数，避免多个实例共享同一个列表。
- `chat_template_kwargs: dict = None`：专门用来接收 SDK 经 `extra_body` 传来的模板参数。

多入口兼容方法：[scripts/serve_openai_api.py:61-68](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/serve_openai_api.py#L61-L68)

```python
def get_open_thinking(self) -> bool:
    """兼容多种方式开启 thinking"""
    if self.open_thinking:
        return True
    if self.chat_template_kwargs:
        return self.chat_template_kwargs.get('open_thinking', False) or \
               self.chat_template_kwargs.get('enable_thinking', False)
    return False
```

说明：先看顶层 `open_thinking`；没有再看 `chat_template_kwargs` 里的 `open_thinking` 或 `enable_thinking`（后者是为了兼容 Qwen3 生态的命名习惯）。这样无论客户端用哪种姿势传，服务端都能拿到一个统一的布尔结果。

#### 4.1.4 代码实践

**目标**：不启动服务，直接验证 `ChatRequest` 的字段校验与 `get_open_thinking` 逻辑。

**步骤**：

1. 在 `scripts/` 目录下起一个 Python 交互环境（或在临时脚本里），把 `ChatRequest` 导入并实例化几种请求：
   ```python
   # 示例代码（非项目原有代码），请在 scripts/ 目录下运行
   from serve_openai_api import ChatRequest

   # (a) 顶层开思考
   r1 = ChatRequest(model="minimind", messages=[{"role": "user", "content": "你好"}], open_thinking=True)
   # (b) 通过 SDK 的 chat_template_kwargs 间接开
   r2 = ChatRequest(model="minimind", messages=[{"role": "user", "content": "你好"}],
                    chat_template_kwargs={"open_thinking": True})
   # (c) 啥都不传
   r3 = ChatRequest(model="minimind", messages=[{"role": "user", "content": "你好"}])
   print(r1.get_open_thinking(), r2.get_open_thinking(), r3.get_open_thinking())
   ```
2. 再试着故意漏掉必填字段，观察 Pydantic 报错：`ChatRequest(messages=[{"role":"user","content":"x"}])`（缺 `model`）。

**需要观察的现象**：
- 第 1 步应打印 `True True False`，说明两种入口都能正确打开思考、缺省时关闭。
- 第 2 步应抛出 `ValidationError`（提示 `model` 字段缺失），证明必填校验生效。

**预期结果**：`True True False`，且漏字段时报 422/ValidationError。**待本地验证**（取决于你机器上 `pydantic` 版本，报错文案略有差异，但行为一致）。

#### 4.1.5 小练习与答案

**练习 1**：如果客户端传的是 `chat_template_kwargs={"enable_thinking": True}`（注意是 `enable_thinking` 不是 `open_thinking`），`get_open_thinking()` 返回什么？为什么？

**答案**：返回 `True`。因为 `get_open_thinking` 在 `chat_template_kwargs` 分支里同时检查了 `open_thinking` 和 `enable_thinking` 两个键（[scripts/serve_openai_api.py:66-67](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/serve_openai_api.py#L66-L67)），这是为了兼容 Qwen3 模板家族的命名。

**练习 2**：`tools: list = Field(default_factory=list)` 能不能写成 `tools: list = []`？

**答案**：不建议。Pydantic（以及 Python 的普遍约定）要求可变默认值用 `default_factory` 工厂函数返回，写成 `= []` 会让所有「未传 tools 的请求」共享同一个列表对象，存在被意外修改的隐患。

---

### 4.2 CustomStreamer：用「线程 + 队列」把 streamer 接到 HTTP 流上

#### 4.2.1 概念说明

[u3-l6](./u3-l6-custom-generate.md) 讲过，`model.generate` 接受一个 `streamer` 回调，每解码出一段文本就回调一次。CLI 场景下，`TextStreamer` 的回调里直接 `print` 就完事了。

但 HTTP 流式场景有个矛盾：

- `model.generate` 是**阻塞**的同步函数，要跑几秒到几十秒；
- HTTP 流式要求服务端**边生成边把片段推给客户端**，不能等全部生成完再一次性返回。

如果让 `model.generate` 在主线程阻塞地跑，HTTP 响应就没法实时往外吐字。MiniMind 的解法是经典的生产者-消费者模式：**把 `model.generate` 丢到一个后台线程里跑（生产者），让它把文本片段塞进一个 `Queue`；HTTP 响应这边（消费者）不断从队列里取片段，包装成 SSE 往外发。** `CustomStreamer` 就是这两者之间的「桥」——它继承自 `TextStreamer`，但不打印，而是把文本 `put` 进队列。

#### 4.2.2 核心流程

```
后台线程 _generate:                      主线程 generate_stream_response (HTTP 侧)
  model.generate(..., streamer=...)        while True:
    每解码出一段文本                          text = queue.get()   # 阻塞等
      → streamer.on_finalized_text(text)     if text is None: break
      → queue.put(text)                       把 text 包成 SSE delta yield 出去
    生成结束:
      → streamer.on_finalized_text(..., stream_end=True)
      → queue.put(None)  ← 哨兵，表示「没了」
```

`None` 是约定好的「结束哨兵」：消费者拿到 `None` 就知道生成结束了，退出循环。异常情况下生产者会塞一个 `{"error": ...}` 字典再塞 `None`（见 [scripts/serve_openai_api.py:126-128](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/serve_openai_api.py#L126-L128)），消费者遇到字典就原样转发错误。

#### 4.2.3 源码精读

`CustomStreamer` 的全部实现只有 10 行：[scripts/serve_openai_api.py:71-80](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/serve_openai_api.py#L71-L80)

```python
class CustomStreamer(TextStreamer):
    def __init__(self, tokenizer, queue):
        super().__init__(tokenizer, skip_prompt=True, skip_special_tokens=True)
        self.queue = queue
        self.tokenizer = tokenizer

    def on_finalized_text(self, text: str, stream_end: bool = False):
        self.queue.put(text)
        if stream_end:
            self.queue.put(None)
```

说明：
- `super().__init__(..., skip_prompt=True, skip_special_tokens=True)`：`skip_prompt=True` 表示不回显用户输入的 prompt，只输出新生成的部分；`skip_special_tokens=True` 表示不把 `<|im_end|>` 这类特殊标记解码出来（否则客户端会看到一堆尖括号标签）。
- `on_finalized_text` 是 `TextStreamer` 留给子类实现的钩子。HF 内部解码完一段文本后会调用它，传进 `text` 和 `stream_end`。这里只做两件事：把 `text` 塞进队列；如果是最后一段（`stream_end=True`），再塞一个 `None` 哨兵。

后台线程的启动与异常兜底：[scripts/serve_openai_api.py:113-130](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/serve_openai_api.py#L113-L130)

```python
def _generate():
    try:
        model.generate(
            inputs.input_ids, max_new_tokens=max_tokens, do_sample=True,
            temperature=temperature, top_p=top_p,
            attention_mask=inputs.attention_mask,
            pad_token_id=tokenizer.pad_token_id,
            eos_token_id=tokenizer.eos_token_id,
            streamer=streamer)
    except Exception as e:
        queue.put({"error": str(e)})
        queue.put(None)

Thread(target=_generate).start()
```

说明：`model.generate` 在子线程跑，挂了也不会让整个服务崩——异常被捕获后转成 `{"error": ...}` 推给消费者，再补一个 `None` 收尾，客户端能看到错误而不是连接莫名断开。

#### 4.2.4 代码实践

**目标**：脱离 HTTP，单独理解「线程 + 队列 + CustomStreamer」是怎么协作的。

**步骤**（示例代码，非项目原有代码）：

```python
# 在 scripts/ 下运行；--load_from 按你的权重改
from queue import Queue
from threading import Thread
from transformers import AutoTokenizer
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM
from serve_openai_api import CustomStreamer
import torch

tok = AutoTokenizer.from_pretrained("../model")
model = MiniMindForCausalLM(MiniMindConfig()).half().eval()  # 随机权重，只为观察流式机制
model.load_state_dict(torch.load("../out/full_sft_768.pth", map_location="cpu"), strict=False)

q = Queue()
streamer = CustomStreamer(tok, q)

def _gen():
    ids = tok("你好", return_tensors="pt")["input_ids"]
    model.generate(ids, max_new_tokens=20, do_sample=True, streamer=streamer)

Thread(target=_gen).start()
while True:
    chunk = q.get()
    if chunk is None:
        print("\n[stream end]")
        break
    print(repr(chunk), end=" ")   # 观察每个片段长什么样
```

**需要观察的现象**：会看到一连串短字符串（往往一两个汉字或词）被逐个打印，最后打印 `[stream end]`。这印证了「片段是细粒度的、`None` 是结束哨兵」。

**预期结果**：终端逐字（或逐词）输出，最后以 `[stream end]` 收尾。**待本地验证**（片段粒度取决于分词器与生成内容）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `on_finalized_text` 里要在 `stream_end=True` 时再 `put(None)`？只用 `put(text)` 行不行？

**答案**：不行。`text` 是「还有更多内容要来」的普通片段，`None` 是「全部结束」的哨兵。消费者靠 `None` 才知道何时退出 `while True` 循环并发送 `finish_reason`，没有哨兵消费者会永远阻塞在 `queue.get()` 上。

**练习 2**：如果 `skip_special_tokens=False`，客户端流式输出会多出什么？

**答案**：会把 `<|im_end|>`、`<|im_start|>` 等特殊标记也解码成文本发给客户端，污染回答内容。所以默认开了 `skip_special_tokens=True`。

---

### 4.3 parse_response：把 `<think>` / `<tool_call>` 解析为 reasoning_content 与 tool_calls

#### 4.3.1 概念说明

模型生成出来的原始文本是一整条字符串，里面可能混着两种「结构化片段」：

- 思考过程：被 `<think> ... </think>` 包住，要提取成 OpenAI 协议的 `reasoning_content` 字段；
- 工具调用：被 `<tool_call> {json} </tool_call>` 包住，要提取成 `tool_calls` 字段（结构化的 `name` + `arguments`）。

`parse_response` 就是干这件事的**正则解析器**。它吃进原始文本，吐出三元组 `(content, reasoning_content, tool_calls)`：`content` 是去掉思考与工具标签后的「干净正文」，`reasoning_content` 是思考内容（没有就 `None`），`tool_calls` 是工具调用列表（没有就 `None`）。

> 注意它和非流式/流式两条路径的关系：**非流式**路径在拿到完整回答后调一次 `parse_response`，得到干净的三元组直接返回；**流式**路径在生成结束后也调一次 `parse_response`，但只是为了抽出 `tool_calls` 作为最后一个 delta 发出去（思考与正文已在生成过程中增量发过了）。这个差异在 4.4 节会详述。

#### 4.3.2 核心流程

```
输入: text（模型完整输出）
1. 提取思考:
   - 优先匹配 <think>...</think>（DOTALL 跨行），取中间内容为 reasoning_content，并从原文删掉这段
   - 若没有完整 <think> 开标签但有 </think>（半截思考，对应 open_thinking=1 模板只注入半个 <think> 的情况）：
     以 </think> 切分，前半为 reasoning_content，后半为正文
2. 提取工具调用:
   - 正则找所有 <tool_call>...</tool_call>，逐个 json.loads 成 {name, arguments}
   - 拼成 OpenAI 格式: {"id": "call_<时间戳>_<序号>", "type": "function",
                        "function": {"name": ..., "arguments": <json 字符串>}}
   - 从正文删掉这些 <tool_call> 段
3. 返回 (正文.strip(), reasoning_content, tool_calls 或 None)
```

#### 4.3.3 源码精读

完整函数：[scripts/serve_openai_api.py:83-102](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/serve_openai_api.py#L83-L102)

```python
def parse_response(text):
    reasoning_content = None
    think_match = re.search(r'<think>(.*?)</think>', text, re.DOTALL)
    if think_match:
        reasoning_content = think_match.group(1).strip()
        text = re.sub(r'<think>.*?</think>\s*', '', text, flags=re.DOTALL)
    elif '</think>' in text:
        parts = text.split('</think>', 1)
        reasoning_content = parts[0].strip()
        text = parts[1].strip() if len(parts) > 1 else ''
    tool_calls = []
    for i, m in enumerate(re.findall(r'<tool_call>(.*?)</tool_call>', text, re.DOTALL)):
        try:
            call = json.loads(m.strip())
            tool_calls.append({"id": f"call_{int(time.time())}_{i}",
                               "type": "function",
                               "function": {"name": call.get("name", ""),
                                            "arguments": json.dumps(call.get("arguments", {}),
                                                                    ensure_ascii=False)}})
        except Exception:
            pass
    if tool_calls:
        text = re.sub(r'<tool_call>.*?</tool_call>', '', text, flags=re.DOTALL)
    return text.strip(), reasoning_content, tool_calls or None
```

逐段说明：

- **思考的两条分支**（[L85-92](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/serve_openai_api.py#L85-L92)）：
  - `if think_match`：标准的「完整 think 块」情况，闭标签齐全。
  - `elif '</think>' in text`：这是给 `open_thinking=1` 的「半标签」情况兜底的。回忆 [u2-l1](./u2-l1-tokenizer-and-chat-template.md)：`open_thinking=1` 时模板只注入起始 `<think>`，没有起始闭合成对，所以模型输出里往往只有结尾的 `</think>` 而没有 `<think>` 开标签。这时用 `</think>` 把文本切成两段：前面是思考、后面是正文。这是一个很务实的容错设计。
- **工具调用解析**（[L94-99](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/serve_openai_api.py#L94-L99)）：用 `re.findall` 找出所有 `<tool_call>` 片段，逐个 `json.loads`。注意几个细节：
  - 包了 `try/except`：模型生成的 JSON 可能不合法（比如缺引号、带注释），解析失败就静默跳过（`pass`），不会让整次请求挂掉。
  - `id` 用 `call_{时间戳}_{序号}` 生成，保证同一批多个工具调用 id 不重复（OpenAI 协议要求每个 tool_call 有唯一 id）。
  - `arguments` 被 `json.dumps` 成**字符串**：这是 OpenAI 协议的规定——`function.arguments` 是 JSON 字符串而不是对象，客户端拿到后再自行 `json.loads`。
  - `ensure_ascii=False`：保留中文等非 ASCII 字符，避免被转成 `\uXXXX`。
- **正文清洗与返回**（[L100-102](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/serve_openai_api.py#L100-L102)）：如果有工具调用，就把 `<tool_call>` 段从正文删掉，让 `content` 干净。返回时 `tool_calls or None`——空列表 `[]` 会被转成 `None`，符合「没有就不返回这个字段」的约定。

#### 4.3.4 代码实践

**目标**：用几条手造文本，验证 `parse_response` 的三种解析行为，完全不需要加载模型。

**步骤**（示例代码）：

```python
from serve_openai_api import parse_response

# (a) 完整 think 块
print(parse_response("<think>用户在问海拔，我要查一下</think>珠穆朗玛峰海拔 8848 米。"))
# (b) 半截 think（只有 </think>，模拟 open_thinking=1 输出）
print(parse_response("先算一下 1+1=2</think>结果是 2。"))
# (c) 工具调用
print(parse_response('<tool_call>{"name":"get_time","arguments":{"tz":"Asia/Shanghai"}}</tool_call>'))
```

**需要观察的现象**：
- (a) 返回 `('珠穆朗玛峰海拔 8848 米。', '用户在问海拔，我要查一下', None)`——思考进了 `reasoning_content`，正文干净。
- (b) 返回 `('结果是 2。', '先算一下 1+1=2', None)`——半标签也能正确切分。
- (c) 返回 `('', None, [{...}])`——`tool_calls` 是带 `id/type/function` 的标准结构，`arguments` 是 JSON 字符串，正文被清空。

**预期结果**：同上。`待本地验证`（`id` 里的时间戳会随运行时刻变化）。

#### 4.3.5 小练习与答案

**练习 1**：如果模型输出的工具调用 JSON 不合法（比如 `{"name": get_time}` 缺引号），`parse_response` 会怎样？

**答案**：这个 `<tool_call>` 会被 `try/except` 捕获并跳过（`pass`），不进入 `tool_calls` 列表，但它的原文也不会被从 `text` 里删掉（因为 `if tool_calls:` 为 False 时不动正文）。所以客户端可能在 `content` 里看到原始的非法标签。这是「宁可不解析也不崩溃」的取舍。

**练习 2**：为什么 `arguments` 要再 `json.dumps` 一次变成字符串，而不是直接放字典？

**答案**：因为 OpenAI Chat Completions 协议规定 `function.arguments` 是一个 **JSON 字符串**而非对象。这样不同语言的客户端都能用统一的「再解析一次 JSON」方式拿到参数，避免协议层把对象结构写死。

---

### 4.4 /v1/chat/completions：FastAPI 端点与 SSE 增量切分

#### 4.4.1 概念说明

有了 `ChatRequest`（收请求）、`CustomStreamer`（线程队列桥）和 `parse_response`（解析），最后把它们串起来的就是 FastAPI 的端点函数 `chat_completions`，以及它调用的流式生成器 `generate_stream_response`。

这个端点要处理两种模式：

- **非流式（stream=False）**：一次性生成完整回答，调 `parse_response` 拆出三元组，组装成一个标准 `chat.completion` JSON 返回。逻辑简单，和 `eval_llm.py` 的 CLI 推理几乎一样。
- **流式（stream=True，默认）**：边生成边返回。这是本讲的真正难点——要把一条连续的文本流，**切成一连串 SSE 事件**，并且要正确地把「思考片段」路由到 `delta.reasoning_content`、「正文片段」路由到 `delta.content`、把工具调用留到最后作为 `delta.tool_calls`。

流式切分的核心难点在于：**模型吐字时，思考与正文是粘在一起的一段字符串，服务端要在 `</think>` 出现的那一刻，把后续输出从「思考字段」切换到「正文字段」。**

#### 4.4.2 核心流程

**端点分支**（[scripts/serve_openai_api.py:178-192](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/serve_openai_api.py#L178-L192)）：

```
POST /v1/chat/completions(request: ChatRequest):
  if request.stream:
      return StreamingResponse(
          ("data: " + chunk + "\n\n" for chunk in generate_stream_response(...)),
          media_type="text/event-stream")   # SSE
  else:
      一次性 model.generate → parse_response → 组装 chat.completion JSON 返回
```

**流式生成器 `generate_stream_response` 的增量切分算法**（关键）：

它维护两个变量：`full_text`（累计的全部文本）和 `emitted`（已经发出去的字符数）。每次从队列拿到新文本后，追加到 `full_text`，然后根据「是否已经跨过 `</think>`」决定把新增部分发到哪个字段：

```
thinking_ended = (not open_thinking)   # 开局：没开思考就直接是正文阶段
loop:
    text = queue.get()
    if text is None: break              # 生成结束
    full_text += text
    if 还没到 </think>（即 thinking_ended == False）:
        if full_text 里出现了 </think>:
            把 </think> 之前还没发的部分 → delta.reasoning_content
            把 </think> 之后的部分      → delta.content
            thinking_ended = True
        else:
            把还没发的部分              → delta.reasoning_content
    else（已经在正文阶段）:
        把还没发的部分                  → delta.content
# 循环结束后：
parse_response(full_text) 抽 tool_calls → 若有，发一个 delta.tool_calls
最后发一个 finish_reason: "tool_calls" 或 "stop"
```

`emitted` 这个「已发指针」是理解整段代码的钥匙：它保证每个字符**只发一次**，既不漏发也不重发。

#### 4.4.3 源码精读

**端点函数与流式分支**：[scripts/serve_openai_api.py:178-192](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/serve_openai_api.py#L178-L192)

```python
@app.post("/v1/chat/completions")
async def chat_completions(request: ChatRequest):
    try:
        if request.stream:
            return StreamingResponse(
                (f"data: {chunk}\n\n" for chunk in generate_stream_response(
                    messages=request.messages,
                    temperature=request.temperature,
                    top_p=request.top_p,
                    max_tokens=request.max_tokens,
                    tools=request.tools,
                    open_thinking=request.get_open_thinking()
                )),
                media_type="text/event-stream"
            )
```

说明：
- `@app.post("/v1/chat/completions")` 是 OpenAI 协议的标准路径，客户端 SDK 默认就往这里发。
- 关键是那个生成器表达式 `(f"data: {chunk}\n\n" for chunk in generate_stream_response(...))`：它把 `generate_stream_response` 吐出的每个 JSON 字符串前面加 `data: `、后面加 `\n\n`，正好是 SSE 的一条事件。`StreamingResponse` 会把这条流逐条写给客户端。
- `media_type="text/event-stream"` 是 SSE 的标准 MIME 类型，客户端（含官方 `openai` SDK、curl）据此识别这是一条事件流。

**流式生成器的初始化与线程启动**：[scripts/serve_openai_api.py:105-134](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/serve_openai_api.py#L105-L134)

```python
def generate_stream_response(messages, temperature, top_p, max_tokens, tools=None, open_thinking=False):
    try:
        new_prompt = tokenizer.apply_chat_template(messages, tokenize=False,
                add_generation_prompt=True, tools=tools or None, open_thinking=open_thinking)
        inputs = tokenizer(new_prompt, return_tensors="pt", truncation=True).to(device)

        queue = Queue()
        streamer = CustomStreamer(tokenizer, queue)
        ...
        Thread(target=_generate).start()

        full_text = ""
        emitted = 0
        thinking_ended = not bool(open_thinking)
```

说明：
- `apply_chat_template(..., add_generation_prompt=True, tools=..., open_thinking=...)`：和 `eval_llm.py`（[eval_llm.py:76](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py#L76)）用的是同一个模板调用，只是这里多传了 `tools`。回忆 [u2-l1](./u2-l1-tokenizer-and-chat-template.md)：`tools` 非空时模板会把工具定义渲染进 system 段，引导模型生成 `<tool_call>`。
- `thinking_ended = not bool(open_thinking)`：这是切分逻辑的「初始状态」。如果**没开**思考，开局就直接当作正文阶段（因为模板已经注入了空的 `<think></think>`，模型一上来就是正文）；如果**开了**思考，开局处于「思考阶段」，要等 `</think>` 出现才切换。

**增量切分主循环（思考分支）**：[scripts/serve_openai_api.py:136-162](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/serve_openai_api.py#L136-L162)

```python
while True:
    text = queue.get()
    if text is None:
        break
    if isinstance(text, dict):
        yield json.dumps(text, ensure_ascii=False)
        continue
    full_text += text

    if not thinking_ended:
        pos = full_text.find('</think>')
        if pos >= 0:
            thinking_ended = True
            new_r = full_text[emitted:pos]
            if new_r:
                yield json.dumps({"choices": [{"delta": {"reasoning_content": new_r}}]}, ensure_ascii=False)
            emitted = pos + len('</think>')
            after = full_text[emitted:].lstrip('\n')
            emitted = len(full_text) - len(after)
            if after:
                yield json.dumps({"choices": [{"delta": {"content": after}}]}, ensure_ascii=False)
                emitted = len(full_text)
        else:
            new_r = full_text[emitted:]
            if new_r:
                yield json.dumps({"choices": [{"delta": {"reasoning_content": new_r}}]}, ensure_ascii=False)
                emitted = len(full_text)
```

说明（这是全讲最绕的一段，慢慢看）：
- 拿到 `text` 后先判类型：字典是错误（原样转发）、`None` 是结束哨兵。
- `if not thinking_ended:`（还在思考阶段）：去 `full_text` 里找 `</think>`：
  - 找到（`pos >= 0`）：把 `[emitted, pos)` 之间没发的部分作为 `reasoning_content` 发出；然后把 `emitted` 推到 `</think>` 之后；紧接着把 `</think>` 之后的正文（去掉前导换行）作为 `content` 发出，并更新 `emitted`。这一步完成了「思考 → 正文」的字段切换。
  - 没找到：说明 `</think>` 还没生出来，把新增部分继续作为 `reasoning_content` 发出。
- `lstrip('\n')` + `emitted = len(full_text) - len(after)` 这一手是为了跳过 `</think>` 后面的空行，让正文不以下一堆换行开头——同时把 `emitted` 对齐到去换行之后的位置，保证不漏发。

**正文分支与收尾**：[scripts/serve_openai_api.py:163-172](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/serve_openai_api.py#L163-L172)

```python
            else:
                new_c = full_text[emitted:]
                if new_c:
                    yield json.dumps({"choices": [{"delta": {"content": new_c}}]}, ensure_ascii=False)
                    emitted = len(full_text)

        _, _, tool_calls = parse_response(full_text)
        if tool_calls:
            yield json.dumps({"choices": [{"delta": {"tool_calls": tool_calls}}]}, ensure_ascii=False)
        yield json.dumps({"choices": [{"delta": {}, "finish_reason": "tool_calls" if tool_calls else "stop"}]}, ensure_ascii=False)
```

说明：
- 进入正文阶段后，逻辑很简单：把 `[emitted:]` 的新增部分作为 `content` 发出，推进 `emitted`。
- 生成结束后，调一次 `parse_response(full_text)` **只为抽 `tool_calls`**（所以用 `_ , _` 丢弃前两个返回值）。如果有工具调用，作为一个 `delta.tool_calls` 整体发出；最后发一个带 `finish_reason` 的空 delta 收尾（`"tool_calls"` 或 `"stop"`）。

> ⚠️ **一个忠实于代码的观察**：在流式路径里，`<tool_call>...</tool_call>` 的**原始文本**其实在正文阶段已经被当作 `content` 增量发出去了（上面正文分支不会识别 tool_call 标签）；生成结束后又额外发了一个结构化的 `delta.tool_calls`。也就是说，流式模式下客户端会**同时**在 `content` 里看到原始 `<tool_call>` 标签、在 `tool_calls` 里看到结构化对象。相比之下，**非流式**路径（下面这段）调 `parse_response` 会把标签从 `content` 里删干净，`content` 是清爽的。如果你做客户端集成，需要留意这个流式/非流式的细微差异。

**非流式分支**：[scripts/serve_openai_api.py:193-232](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/serve_openai_api.py#L193-L232)

```python
        else:
            new_prompt = tokenizer.apply_chat_template(request.messages, tokenize=False,
                    add_generation_prompt=True, tools=request.tools or None,
                    open_thinking=request.get_open_thinking())
            inputs = tokenizer(new_prompt, return_tensors="pt", truncation=True).to(device)
            with torch.no_grad():
                generated_ids = model.generate(inputs["input_ids"],
                        max_length=inputs["input_ids"].shape[1] + request.max_tokens,
                        do_sample=True, ...)
                answer = tokenizer.decode(generated_ids[0][inputs["input_ids"].shape[1]:],
                                          skip_special_tokens=True)
            content, reasoning_content, tool_calls = parse_response(answer)
            message = {"role": "assistant", "content": content}
            if reasoning_content:
                message["reasoning_content"] = reasoning_content
            if tool_calls:
                message["tool_calls"] = tool_calls
            return { ...标准 chat.completion 结构, choices[0].message = message ... }
```

说明：非流式就是「生成完整回答 → `parse_response` 拆三元组 → 按需挂上 `reasoning_content`/`tool_calls` → 套进标准返回结构」。注意它用 `max_length=输入长度+max_tokens`（而流式用 `max_new_tokens`），这是两套参数写法的细微差异，效果近似。

**服务启动**：[scripts/serve_openai_api.py:237-252](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/serve_openai_api.py#L237-L252) 用 `argparse` 解析 `--load_from/--weight/--hidden_size` 等参数，调 `init_model` 加载模型，最后 `uvicorn.run(app, host="0.0.0.0", port=8998)` 监听 8998 端口。注意 `init_model`（[scripts/serve_openai_api.py:28-47](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/serve_openai_api.py#L28-L47)）和 `eval_llm.py` 的 `init_model` 几乎一样：靠 `'model' in load_from` 判定走原生 torch 权重还是 transformers 目录，并支持叠加 LoRA。

#### 4.4.4 代码实践

**目标**：把服务跑起来，用 `curl` 直接观察 SSE 原始字节流，亲眼看见 `data: {...}\n\n` 长什么样。

**步骤**：

1. 准备权重（任选其一）：把 `full_sft_768.pth` 放到仓库根的 `out/` 目录；或下载 `minimind-3` transformers 文件夹。
2. 启动服务（注意要在 `scripts/` 下跑）：
   ```bash
   cd scripts && python serve_openai_api.py --load_from ../minimind-3
   # 或用原生 torch 权重：
   # cd scripts && python serve_openai_api.py --load_from model --weight full_sft --hidden_size 768
   ```
3. 另开一个终端，用 `curl` 发一个**非流式**请求，先确认通：
   ```bash
   curl http://localhost:8998/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{"model":"minimind","messages":[{"role":"user","content":"你好"}],"stream":false}'
   ```
4. 再发一个**流式 + 开思考**的请求，用 `-N` 关闭 curl 缓冲，实时看流：
   ```bash
   curl -N http://localhost:8998/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{"model":"minimind","messages":[{"role":"user","content":"解释什么是机器学习"}],"stream":true,"open_thinking":true}'
   ```

**需要观察的现象**：
- 第 3 步返回一个完整 JSON，`choices[0].message` 里有 `content`（必要时还有 `reasoning_content`）。
- 第 4 步会看到一连串 `data: {"choices":[{"delta":{"reasoning_content":"..."}}]}`（思考阶段），在某个时刻切换成 `data: {"choices":[{"delta":{"content":"..."}}]}`（正文阶段），最后以 `data: {"choices":[{"delta":{},"finish_reason":"stop"}]}` 收尾。这就是 SSE 的真面目。

**预期结果**：流式输出能清楚看到「先 `reasoning_content` 后 `content`」的字段切换，对应模型先输出 `</think>` 前后的内容。**待本地验证**（具体文案取决于模型与权重）。

#### 4.4.5 小练习与答案

**练习 1**：`generate_stream_response` 里 `emitted` 这个变量如果删掉（每次都把整个 `full_text` 当 delta 发），会出现什么问题？

**答案**：会**重复发送**。因为 `full_text` 是累加的，每次都发 `full_text` 等于把前面发过的内容又发一遍，客户端拼起来会得到指数级膨胀的重复文本。`emitted` 的作用就是记录「已发到第几个字符」，每次只发 `[emitted:]` 的新增部分，保证每个字符只发一次。

**练习 2**：为什么流式路径在循环结束后还要再调一次 `parse_response`？它和循环内的增量发送不重复吗？

**答案**：循环内的增量只发 `reasoning_content` 和 `content`，**没有**发 `tool_calls`。因为工具调用需要完整的 `<tool_call>{json}</tool_call>` 才能解析出结构，而增量片段是不完整的。所以必须等生成全部结束、`full_text` 拼完整后，再调一次 `parse_response` 抽出 `tool_calls` 作为最后一个 delta 补发。它补的是循环里没发的东西，不算重复。

---

## 5. 综合实践

把本讲的四个模块串起来，完成一次「思考 + 工具调用」的完整端到端调用，并解释你看到的每一个字段。

**准备**：

1. 启动服务（权重自选，假设用 `minimind-3`）：
   ```bash
   cd scripts && python serve_openai_api.py --load_from ../minimind-3
   ```
2. 改造客户端 `scripts/chat_api.py`。**注意一个坑**：仓库自带的 `chat_api.py` 默认连的是 ollama（[scripts/chat_api.py:5](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/chat_api.py#L5) `base_url="http://localhost:11434/v1"`、[L16](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/chat_api.py#L16) `model="minimind-local:latest"`）。要连本讲的服务，请把这两处改成：
   ```python
   base_url="http://localhost:8998/v1"   # serve_openai_api.py 的端口
   model="minimind"
   ```
   它已经用 `extra_body={"chat_template_kwargs": {"open_thinking": True}}`（[scripts/chat_api.py:21](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/chat_api.py#L21)）来开思考，这正好走 4.1 讲的 `chat_template_kwargs` 入口。

**任务 A（思考开关对比）**：

1. 跑一次 `chat_api.py`（开思考），再临时把 [L21](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/chat_api.py#L21) 的 `open_thinking` 改成 `False` 再跑一次，问同一个问题（比如「为什么天空是蓝色的」）。
2. 观察客户端代码 [scripts/chat_api.py:37-42](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/chat_api.py#L37-L42)：它把 `delta.reasoning_content` 用灰色（`\033[90m`）打印、`delta.content` 正常颜色打印。说明你能从 `delta` 里同时拿到这两个字段——这正是 4.4 节字段切分的结果。

**任务 B（工具调用）**：

1. 用 `curl` 发一个带 `tools` 字段的请求（注意：服务端不执行工具，只让模型生成 `<tool_call>`，由 4.3 的 `parse_response` 抽成 `tool_calls` 返回）：
   ```bash
   curl -N http://localhost:8998/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{"model":"minimind","stream":true,"messages":[{"role":"user","content":"现在几点了？"}],
          "tools":[{"type":"function","function":{"name":"get_time","description":"获取当前时间","parameters":{"type":"object","properties":{}}}}]}'
   ```
2. 在返回里找到 `delta.tool_calls`（流式）或 `message.tool_calls`（非流式），确认它含 `id`、`function.name=get_time`、`function.arguments`（JSON 字符串）。

**需要观察并解释的现象**：

- 任务 A：开思考时先收到若干 `reasoning_content` 再收到 `content`；关思考时基本只有 `content`。请用 4.4 的 `</think>` 切分逻辑解释这一现象。
- 任务 B：返回里出现结构化 `tool_calls`。请说明它是 4.3 的正则 + `json.loads` 解析出来的，而不是模型直接吐了 JSON 对象。

> 提示：README 也指出，**同时**开 Tool Call 与显式思考时模型可能不稳定（见 README 关于「reasoning 与 tool call 联合蒸馏样本不足」的说明）。所以任务 B 建议关掉 `open_thinking` 单独测工具调用，成功率更高。

**预期结果**：任务 A 能看到思考与正文的字段切换；任务 B 能拿到结构化 `tool_calls`。其余取决于权重质量。**待本地验证**。

---

## 6. 本讲小结

- `serve_openai_api.py` 把 MiniMind 的 `model.generate` 包装成兼容 OpenAI Chat Completions 协议的 HTTP 服务，重点补齐了协议里**流式（SSE）**、**思考（`reasoning_content`）**、**工具调用（`tool_calls`）** 三件原生 generate 没有的东西。
- `ChatRequest`（Pydantic 模型）定义请求字段，并用 `get_open_thinking()` 兼容「顶层 `open_thinking`」与「`chat_template_kwargs.open_thinking/enable_thinking`」等多种开启思考的姿势。
- `CustomStreamer` 继承 `TextStreamer`，用「后台线程跑 `model.generate` + `Queue` 传片段」的生产者-消费者模式，把阻塞式生成桥接到 HTTP 流式响应；`None` 是结束哨兵。
- `parse_response` 用正则把原始文本里的 `<think>...</think>` 解析成 `reasoning_content`、把 `<tool_call>{json}</tool_call>` 解析成标准 `tool_calls`（`arguments` 是 JSON 字符串），并对半截 `</think>` 和非法 JSON 做了容错。
- `/v1/chat/completions` 端点分流式/非流式两条路径：流式靠 `generate_stream_response` 用 `full_text` + `emitted` 双指针在 `</think>` 处把输出切到 `reasoning_content`/`content`，末尾补发 `tool_calls` 与 `finish_reason`；非流式则一次性生成后调 `parse_response` 返回完整 `message`。
- 注意两个工程细节：服务须在 `scripts/` 下运行（权重路径是 `../out/`）；流式路径会把 `<tool_call>` 原文随 `content` 发出、非流式则会被 `parse_response` 清理干净，集成客户端时要留意。

## 7. 下一步学习建议

- **横向对照三个推理入口**：把本讲的 HTTP 服务、[u1-l3](./u1-l3-inference-and-generate.md) 的 `eval_llm.py`（CLI）、以及下一讲 [u8-l3](./u8-l3-webui-and-toolcall-eval.md) 的 `web_demo.py`（Streamlit WebUI）放在一起看，体会「同一个 `apply_chat_template + model.generate`，套上不同的外壳（CLI / HTTP / WebUI）」的工程复用思路。
- **深入工具调用的训练侧**：本讲只讲了**推理/服务侧**如何解析 `<tool_call>`。如果想理解模型是**怎么学会**生成这些标签的，去看 [u7-l6 Agentic RL](./u7-l6-agentic-rl.md) 里 `train_agent.py` 的 `parse_tool_calls`/`execute_tool` 多轮 rollout，以及 `eval_toolcall.py` 的多轮工具评测（下一讲也会涉及）。
- **性能与并发**：本讲服务是单进程、用 Python 线程做流式桥接，适合学习和轻量接入。若要追求高吞吐，可对照 README 里 SGLang / vllm 的 `OpenAI-compatible API server` 启动方式（同样监听 8998），体会「协议相同、推理引擎可换」的解耦价值——这正好呼应 [u7-l2 训推分离](./u7-l2-rollout-engine.md) 里 Rollout 引擎的可插拔设计。
- **协议细节**：如果想做得更「像 OpenAI」，可以尝试补全本讲服务缺失的部分，例如流式首块返回 `role: assistant`、返回 `usage`（token 计数）、支持 `n>1` 多候选——这些是很好的二次开发练手题。
