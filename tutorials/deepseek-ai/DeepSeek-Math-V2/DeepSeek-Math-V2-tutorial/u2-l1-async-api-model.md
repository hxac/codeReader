# APIModel 与异步生成：generate.py 的核心抽象

## 1. 本讲目标

学完本讲，你应该能够：

1. 画出 `APIModel` 三个方法（`generate_one` / `generate_all` / `generate`）的分层调用关系，说清「谁负责单次请求、谁负责并发、谁负责对接外部调用方」。
2. 逐行解释 `generate_one` 如何从 OpenAI 兼容接口的返回中取出 `reasoning_content` 与 `content`，并拼接成带 `</think>` 标记的输出字符串——包括其中一个容易被忽略的细节：**当 `content` 非空时，开头的 `<think>` 标签实际上会被丢弃**。
3. 说明 `generate_all` 如何用 `asyncio.gather` 让一批请求真正并发执行、且结果顺序与输入顺序一一对应。
4. 理解 `generate` 这个同步方法如何用 `asyncio.run` 桥接异步世界，以及它对输入 `item` 的 `messages` / `prompt` 两种兼容处理和 `{**item, ...}` 输出合并方式。

本讲只聚焦 [inference/generate.py](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py) 中 `APIModel` 类这一个最小模块（第 15-75 行）；多进程队列与断点续跑属于下一讲 u2-l2 的内容。

## 2. 前置知识

### 2.1 同步、异步与事件循环

- **同步调用**：代码一行执行完才执行下一行。如果一行是「等网络返回」，整个线程就干等着。
- **异步（async）调用**：遇到「等网络返回」时，程序可以把 CPU 让出去处理别的任务，等返回就绪后再回来继续。
- **协程（coroutine）**：用 `async def` 定义的函数。调用它不会立刻执行，而是返回一个「协程对象」，必须被事件循环调度才会真正运行。
- **事件循环（event loop）**：一个调度器，不断在「就绪的协程」之间切换，让所有等待中的 I/O 任务看起来在同时推进。
- **`await`**：在协程内部表示「这里可能要等，事件循环可以先去忙别的」。`await foo()` 会挂起当前协程，直到 `foo()` 完成。
- **`asyncio.run(coro)`**：创建一个全新的事件循环，运行传入的协程直到结束，然后关闭循环。它是「从同步代码进入异步世界」的大门。
- **`asyncio.gather(*aws)`**：把多个协程打包成一组并发执行，等全部完成后，**按传入顺序**（而非完成顺序）返回结果列表。

一句话直觉：把每个 API 请求想象成「烧水」，同步做法是守着第一壶开了再烧第二壶；`gather` 的做法是同时把十壶水放上灶，谁先开不管，最后按编号把水壶排好交给你。

### 2.2 OpenAI Chat Completions 接口与 AsyncOpenAI

本项目不加载本地权重，而是通过 HTTP 调用一个「兼容 OpenAI Chat Completions 协议」的推理服务（可以是 DeepSeek 官方 API，也可以是自建的 vLLM/SGLang 等服务）。接口要点：

- 请求体核心是 `messages`，一个按时间排列的对话列表，每条形如 `{"role": "user", "content": "..."}`。
- 常用采样参数：`temperature`、`top_p`、`max_tokens` 等，作为关键字参数随请求发出。
- 返回体中 `res.choices[0].message.content` 是模型正文，`res.choices[0].finish_reason` 是结束原因（`"stop"` 表示正常收尾，`"length"` 表示因长度截断）。
- `openai` 官方 Python SDK 提供 `AsyncOpenAI` 客户端，其 `client.chat.completions.create(...)` 是一个协程，必须 `await`。

### 2.3 reasoning_content 与 `<think>` 标记

DeepSeek 的推理模型在输出时会区分两段内容：

- `reasoning_content`：思维链（思考过程）；
- `content`：正式回答。

API 以两个独立字段返回它们。但很多下游处理习惯使用「单字符串 + 标记」的老格式：

```
<think>
思考过程...
</think>
正式回答...
```

`APIModel.generate_one` 的职责之一就是把两个字段拼回这种单字符串格式——下游的 `main.py` 全靠 `</think>` 这个标记来切分「思考」与「正文」（后面 4.2.3 会看到证据）。

### 2.4 与前几讲的衔接

u1-l3 已经建立：`main.py` 通过 `os.system` 拼命令行调用 `generate.py`，`generate.py` 是整条流水线里**唯一真正发 HTTP 请求**的文件；`--api_url` 参数因为 `parse_known_args` 的宽容解析而被静默忽略，真正生效的 API 配置是 `APIModel.__init__` 里硬编码的 `api_key` 与 `base_url`。本讲就深入这个类的内部。

## 3. 本讲源码地图

| 文件 | 本讲关注范围 | 作用 |
| --- | --- | --- |
| [inference/generate.py](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py) | 第 15-75 行（`APIModel` 类） | 本讲主角：异步 API 调用的核心抽象 |
| [inference/generate.py](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L116-L121) | 第 116-121 行 | `__main__` 中构造 `sampling_params` 字典，是 `**sampling_params` 的上游来源 |
| [inference/main.py](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L450-L462) | 第 450-462 行 | `main.py` 用 `os.system` 调用 `generate.py` 的位置，说明 CLI 参数如何传进来 |
| [inference/main.py](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L71-L76) | 第 71-76 行 | 下游消费 `finish_reason` 与 `</think>` 的证据，解释拼接格式为何这样设计 |

## 4. 核心概念与源码讲解

### 4.1 分层设计与客户端构造：`__init__` 与三级方法

#### 4.1.1 概念说明

`APIModel` 是一个很小的类，只有四个方法，却把职责切得非常干净：

| 方法 | 异步？ | 职责 |
| --- | --- | --- |
| `__init__` | 否 | 构造 `AsyncOpenAI` 客户端，持有 API 配置 |
| `generate_one` | 是（`async def`） | 发出**一次**请求并整理返回 |
| `generate_all` | 是（`async def`） | 把一批请求并发发出并收集结果 |
| `generate` | 否（普通 `def`） | 对外总入口：预处理输入 → 驱动事件循环 → 合并输出 |
| `mp_generate` | 否 | 多进程 worker 的批处理循环（下一讲 u2-l2 精读） |

这种「单请求 → 并发 → 同步门面」的三层结构是异步封装的经典写法：底层保持全异步获得吞吐，顶层提供同步接口方便外部（不熟悉 asyncio 的调用方）使用。

#### 4.1.2 核心流程

```text
外部调用方（mp_generate / 你的测试脚本）
        │  同步调用
        ▼
   generate(input_data, sampling_params)      ← 同步门面
        │  整理 messages / prompt 两种输入
        │  asyncio.run(...)                    ← 进入异步世界
        ▼
   generate_all(data)                          ← 并发层
        │  为每条数据创建 generate_one 协程
        │  await asyncio.gather(*tasks)        ← 同时在飞
        ▼
   generate_one(prompt, sampling_params) × N   ← 单请求层
        │  await client.chat.completions.create(...)
        │  拼接 reasoning_content / content
        ▼
   [(output_string, finish_reason), ...]       ← 按输入顺序返回
```

#### 4.1.3 源码精读

类的定义与构造函数：

[inference/generate.py:L15-L21](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L15-L21)

```python
class APIModel:
    def __init__(self):
        self.client = AsyncOpenAI(
            api_key="xxx",
            timeout=300000,
            base_url="yyy"
        )
```

这段代码做了三件事：

1. 实例化 `AsyncOpenAI` 客户端并保存为 `self.client`，后续所有请求都通过它发出。
2. `api_key="xxx"` 与 `base_url="yyy"` 是**占位符**——这就是 u1-l3 说的「API 配置唯一真实入口」。想跑通流水线，必须把这两个字符串改成真实值。`base_url` 指向任何兼容 OpenAI 协议的服务地址即可。
3. `timeout=300000`：`openai` SDK 的超时单位是**秒**，300000 秒约 83 小时，等于实际上不设超时。考虑到证明生成动辄上万 token 的长思维链，这样设置可以理解为「宁等不杀」，但也意味着挂死的连接不会被自动掐断。

再看这批请求的采样参数从哪来。[inference/generate.py:L116-L121](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L116-L121) 在 `__main__` 中把命令行参数打包成字典：

```python
sampling_params = dict(
    temperature=temperature,
    top_p=top_p,
    max_tokens=max_tokens,
    max_total_tokens=max_tokens
)
```

这个字典随后整体传给 `generate`，最终被 `generate_one` 里的 `**sampling_params` 解包进请求。注意 `max_total_tokens` **不是** OpenAI 官方 Chat Completions 参数，属于某些推理后端的扩展参数；能否被接受取决于你对接的服务（官方 OpenAI 端点遇到未知参数可能直接报错）。这一点在对接自建服务时需要留意（待确认：你所用后端是否支持该参数）。

上游调用链的证据在 [inference/main.py:L450-L462](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L450-L462)：`main.py` 用 f-string 拼出 `python generate.py --temperature ... --top_p 0.95 --max_tokens ... --n ...` 这样的命令并 `os.system(proof_gen_cmd)` 执行，命令行参数由此进入 `generate.py` 的 argparse，再变成上面的 `sampling_params`。

#### 4.1.4 代码实践

实践目标：确认 API 配置入口与参数流向。

操作步骤：

1. 打开 `inference/generate.py`，定位第 17-21 行，找到 `api_key` 与 `base_url`。
2. 顺着 [inference/run.sh](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/run.sh) 阅读：注意 `run.sh` 里**没有**任何 `--api_url` 传参，印证「API 配置只认 `__init__` 里的硬编码」。
3. 在 [inference/generate.py:L84-L93](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L84-L93) 的 argparse 段数一数：`--temperature`、`--top_p`、`--max_tokens`、`--n` 都是 `required=True`，而 `--num_processes`、`--batch_size` 有默认值 16。

需要观察的现象：argparse 定义里**没有** `--api_url`，但 [inference/main.py:L453](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L453) 确实传了 `--api_url {proof_gen_url}`；配合第 93 行的 `parse_known_args`，这个参数被静默丢弃。

预期结果：你能口头复述「命令行采样参数 → `sampling_params` 字典 → `**sampling_params` 解包」这条链路，以及为什么改 `run.sh` 里的 URL 无效。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `APIModel` 不把 `api_key` / `base_url` 做成命令行参数？

**参考答案**：从代码现状看这是发布时的简化处理——仓库是模型发布仓库，作者只需自己改一处硬编码即可。副作用是 `main.py` 传的 `--api_url` 成了死参数（argparse 未注册，`parse_known_args` 静默忽略），多进程场景下每个 worker 各自实例化 `APIModel` 时也无法按进程区分配置。

**练习 2**：`timeout=300000` 在 `openai` SDK 中的单位是什么？这个值意味着什么？

**参考答案**：单位是秒，300000 秒 ≈ 83 小时，实际上禁用了超时。对超长思维链生成来说避免了「想太久被掐断」，但也失去了对挂死连接的保护。

**练习 3**：`sampling_params` 里哪个键不是 OpenAI 官方参数？

**参考答案**：`max_total_tokens`。它是部分推理后端的扩展参数，且在 [inference/generate.py:L116-L121](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L116-L121) 中被设置成与 `max_tokens` 相同的值。

### 4.2 `generate_one`：单次请求生命周期与 `</think>` 拼接

#### 4.2.1 概念说明

`generate_one` 是整个流水线的「最小网络单元」：它发出一次请求、拿到一次返回、产出 `(output_string, finish_reason)` 二元组。它要解决两个问题：

1. **字段抽取**：从 SDK 返回对象里取出 `reasoning_content`（思维链）、`content`（正文）、`finish_reason`（结束原因）。
2. **格式还原**：把分开的两个字段拼回 `<think>...</think>` 单字符串格式，供下游用字符串切分处理。

#### 4.2.2 核心流程

```text
await client.chat.completions.create(messages, stream=False, **sampling_params)
        │
        ▼
res.choices[0].message.reasoning_content ──.strip()──► reasoning_content
res.choices[0].message.content            ──.strip()──► content
        │
        ▼
output_string = f"<think>\n{reasoning_content}"          # 先默认无正文
if content:
    output_string = reasoning_content + f"\n</think>\n{content}"   # 重新赋值！
        │
        ▼
return output_string, finish_reason
```

拼接结果对照表（务必手推一遍）：

| 情形 | 最终 `output_string` |
| --- | --- |
| `content` 为空字符串 | `<think>\n{reasoning_content}`（有开头标签，**没有** `</think>`） |
| `content` 非空 | `{reasoning_content}\n</think>\n{content}`（**没有**开头 `<think>` 标签） |

注意第二行：`if content:` 分支里是**重新赋值**而不是在原字符串后追加，所以第 31 行拼好的 `<think>\n` 前缀被整个丢掉了。最终正常样本的输出形如：

```
（思维链正文）
</think>
（正式回答）
```

为什么丢了开头标签却无伤大雅？因为下游从来不找 `<think>`，只找 `</think>`。

#### 4.2.3 源码精读

[inference/generate.py:L23-L35](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L23-L35)

```python
async def generate_one(self, prompt, sampling_params):
    res = await self.client.chat.completions.create(
        messages=prompt,
        stream=False,
        **sampling_params
    )
    reasoning_content = res.choices[0].message.reasoning_content.strip()
    content = res.choices[0].message.content.strip()
    output_string = f"<think>\n{reasoning_content}"
    if content:
        output_string = reasoning_content + f"\n</think>\n{content}"
    finish_reason = res.choices[0].finish_reason
    return output_string, finish_reason
```

逐段说明：

- **L24-L28**：`await` 发起请求。参数 `prompt` 的名字有误导性——它传入的其实是 **messages 列表**（见 4.3.3 中 `generate` 的组装逻辑）。`stream=False` 表示非流式，一次性拿到完整返回；`**sampling_params` 把 4.1 提到的字典解包成关键字参数。
- **L29-L30**：两个字段各 `.strip()` 去掉首尾空白。注意这里**无条件**访问 `reasoning_content` 属性——如果你对接的后端不返回该字段（非 DeepSeek 风格的服务），这里会直接 `AttributeError`。这是一个兼容性前提。
- **L31-L33**：拼接逻辑，如上表所示。当 `content` 非空时，`<think>\n` 前缀被覆盖丢失。
- **L34-L35**：`finish_reason` 原样返回（可能是 `"stop"`、`"length"` 等），小写化发生在上一层 `generate` 里。

下游消费 `</think>` 的铁证在 [inference/main.py:L71-L76](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L71-L76)（`prepare_proof_verification` 函数内）：

```python
item['proof_finish_reason'] = item.pop('finish_reason').lower()
...
if item['proof_finish_reason'] == 'stop':
    assert '</think>' in prover_output
    proof = prover_output.split("</think>")[-1].strip()
```

可以看到下游只做两件事：判断 `finish_reason` 是否为 `stop`，以及用 `split("</think>")[-1]` 取正文。开头 `<think>` 标签是否存在完全不影响。同样的模式在 [inference/main.py:L124-L127](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L124-L127)、[inference/main.py:L303-L306](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L303-L306) 反复出现——`</think>` 是整条流水线的通用切分锚点。

另一个隐含约定：`finish_reason == "stop"` 被下游当作「思维链完整走完」的信号；被 `length` 截断的样本会在验证准备阶段被过滤掉（u4-l2 会专门讲）。

#### 4.2.4 代码实践

实践目标：在不发任何网络请求的前提下，手工复现并验证拼接逻辑。

操作步骤（示例代码，非仓库原有文件）：

```bash
cd /tmp && python3 -c '
reasoning_content = " let me think... ".strip()
content = "## Solution\n...".strip()

# 逐行复现 generate.py L31-L33
output_string = f"<think>\n{reasoning_content}"
if content:
    output_string = reasoning_content + f"\n</think>\n{content}"
print(repr(output_string))

# 再试 content 为空的分支
content = ""
output_string = f"<think>\n{reasoning_content}"
if content:
    output_string = reasoning_content + f"\n</think>\n{content}"
print(repr(output_string))
'
```

需要观察的现象：第一段输出以 `let me think...` 开头（没有 `<think>`），中间含 `\n</think>\n`；第二段输出以 `<think>\n` 开头且不含 `</think>`。

预期结果：与 4.2.2 的对照表完全一致。（本实践只依赖纯 Python 字符串操作，可本地直接验证。）

#### 4.2.5 小练习与答案

**练习 1**：如果一个样本被 `max_tokens` 截断（`finish_reason == "length"`），它的 `output_string` 里一定有 `</think>` 吗？

**参考答案**：不一定。截断可能发生在思维链中间，此时服务端返回的 `content` 为空，代码走 `content` 为假分支，输出是 `<think>\n{reasoning_content}`，没有 `</think>` 闭合标记。即使 `content` 非空，也可能是不完整的正文。所以下游要同时检查 `finish_reason == 'stop'` 和 `'</think>' in output` 两个条件（见 [inference/main.py:L124](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L124)）。

**练习 2**：若把 L33 改成 `output_string = output_string + f"\n</think>\n{content}"`，输出会变成什么样？对下游有影响吗？

**参考答案**：输出会变成 `<think>\n{reasoning_content}\n</think>\n{content}`，即带完整开闭标签的标准格式。对下游**没有**影响，因为 `main.py` 只用 `split("</think>")[-1]` 取 `</think>` 之后的内容，开头标签在不在、是什么，都不会改变切分结果。

**练习 3**：为什么 `generate_one` 要返回二元组而不是只返回字符串？

**参考答案**：`finish_reason` 承载了「样本是否完整」的元信息，下游需要它过滤被截断的样本；把它和文本一起返回，可以让每个样本自带质量标记，避免下游再发请求探测。

### 4.3 `generate_all` 与 `generate`：并发调度与同步桥接

#### 4.3.1 概念说明

- `generate_all` 是**并发层**：接收已整理好的任务列表，用 `asyncio.gather` 同时发出所有请求，等待全部完成。
- `generate` 是**同步门面**：外部（多进程 worker 或测试脚本）直接调用它，不需要懂 asyncio。它负责三件事：输入预处理（`prompt`/`messages` 兼容）、驱动事件循环（`asyncio.run`）、输出合并（`{**item, ...}`）。

#### 4.3.2 核心流程

```text
generate(input_data, sampling_params)
  ├─ 1. 预处理：for item in input_data
  │      ├─ 无 "messages" 键 → 把 item["prompt"] 包成 [{"role":"user","content":prompt}]
  │      └─ 有 "messages" 键 → 原样使用
  │      组装 data = [{'prompt': messages, 'sampling_params': sampling_params}, ...]
  │
  ├─ 2. outputs = asyncio.run(self.generate_all(data))
  │      └─ generate_all 内部：
  │           tasks = [generate_one(t['prompt'], t['sampling_params']) for t in data]
  │           results = await asyncio.gather(*tasks)   # 并发，结果按输入顺序
  │
  └─ 3. 合并：zip(input_data, outputs)
         output_data.append({**item, "output": ..., "finish_reason": ....lower()})
```

并发收益的直观公式（设第 \( i \) 个请求耗时 \( t_i \) 秒）：

\[ T_{\text{串行}} \approx \sum_{i=1}^{B} t_i \qquad T_{\text{gather 并发}} \approx \max_{i} t_i \]

对一批 \( B \) 个平均耗时几十秒的证明生成请求，差异是「几十分钟」对「几十秒」量级——这是整条流水线可行的前提。

#### 4.3.3 源码精读

并发层非常短，[inference/generate.py:L37-L40](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L37-L40)：

```python
async def generate_all(self, data):
    tasks = [self.generate_one(task['prompt'], task['sampling_params']) for i, task in enumerate(data)]
    results = await asyncio.gather(*tasks)
    return results
```

- 列表推导式为每条数据创建一个 `generate_one` **协程对象**（此刻尚未执行）；`enumerate` 的下标 `i` 没有被使用，属于残留写法。
- `asyncio.gather(*tasks)` 把所有协程交给事件循环并发调度。关键性质：**返回列表的顺序与传入协程的顺序一致**，与各请求实际完成先后无关。这使得第 3 步可以直接 `zip(input_data, outputs)` 而不会张冠李戴。
- 注意 `gather` 默认 `return_exceptions=False`：任何一个请求抛异常，整个 `gather` 立即向上抛，**该批全部结果作废**，代码里没有重试与异常兜底。

同步门面 [inference/generate.py:L42-L66](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L42-L66)：

```python
def generate(self, input_data, sampling_params):
    data = []
    for item in input_data:
        if "messages" not in item:
            messages = [{
                "role": "user",
                "content": item["prompt"],
            }]
        else:
            messages = item['messages']
        data.append({
            'prompt': messages,
            'sampling_params': sampling_params
        })

    outputs = asyncio.run(self.generate_all(data))
    output_data = []
    assert len(input_data) == len(outputs)
    for item, (output_string, finish_reason) in zip(input_data, outputs):
        output_data.append({
            **item,
            "output": output_string,
            "finish_reason": finish_reason.lower(),
        })
    return output_data
```

四个要点：

1. **两种输入兼容**（L44-L51）：`item` 若带 `messages` 键（多轮对话或已渲染好的消息列表）则直接使用；若只有 `prompt` 键（纯文本），就包一层 `role: user` 的单条消息。`main.py` 渲染模板后写入 input.jsonl 的记录带 `messages`，走前者的分支。
2. **进入异步世界**（L57）：`asyncio.run` 每次调用都会新建并随后销毁一个事件循环。也就是说每个批（batch）处理一次，循环就重建一次；连接的复用由 `AsyncOpenAI` 客户端内部的 HTTP 连接池负责，而不是事件循环。
3. **顺序配对**（L59-L60）：`assert` 保证输出条数与输入一致，`zip` 依赖 `gather` 的保序性质完成一一配对。
4. **输出合并**（L61-L65）：`{**item, "output": ..., "finish_reason": ...}` 先展开原 item 的全部字段再覆盖写入两个新字段——原题面、`problem_idx`、`messages` 等都原样保留，下游因此能在同一条记录里同时看到「输入了什么」和「生成了什么」。若 item 恰好自带 `output` 字段，会被新值覆盖（字典解包后者优先）。`finish_reason.lower()` 在这里完成小写化，所以 [inference/main.py:L71](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L71) 的再次 `.lower()` 属于防御性冗余。

结合 `__main__` 的默认值（[inference/generate.py:L87-L88](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L87-L88)：`num_processes=16`、`batch_size=16`），单个 `generate` 调用会让最多 16 个请求同时在飞，16 个 worker 进程各自持有独立 `APIModel` 与事件循环（见 [inference/generate.py:L78-L81](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L78-L81)），理论峰值并发约 256——这正是 u1-l3 所说「输出条数 = 输入条数 × n」得以高效落地的机制。多进程细节留到 u2-l2。

#### 4.3.4 代码实践

实践目标：用纯本地实验验证「gather 保序」与「`{**item}` 合并语义」两个性质。

操作步骤（示例代码，非仓库原有文件；保存为 `/tmp/check_gather.py` 运行）：

```python
import asyncio, random

async def fake_one(idx, delay):
    await asyncio.sleep(delay)
    return f"result-{idx}"

async def main():
    random.seed(0)
    delays = [random.uniform(0.01, 0.05) for _ in range(10)]
    tasks = [fake_one(i, d) for i, d in enumerate(delays)]
    results = await asyncio.gather(*tasks)
    print(results == [f"result-{i}" for i in range(10)])  # 期望 True：顺序不变
    print(sum(delays), ">", max(delays))                  # 串行总和 vs 并发最慢者

asyncio.run(main())

# {**item} 覆盖语义
item = {"problem_idx": "CMO2024-1", "output": "旧值"}
merged = {**item, "output": "新值", "finish_reason": "stop"}
print(merged)  # 期望 output 被覆盖为 新值，其余字段保留
```

需要观察的现象：第一个打印为 `True`（尽管各任务完成顺序随机）；第二个打印显示总耗时按 `max` 而非 `sum` 计；第三个打印中 `output` 为 `新值`。

预期结果：三条全部符合即验证了 `zip` 配对与输出合并的正确性基础。（纯本地可验证；`random` 完成顺序的随机性本身不影响结果顺序。）

#### 4.3.5 小练习与答案

**练习 1**：`generate_all` 中如果第 3 个请求抛出网络异常，会发生什么？

**参考答案**：`asyncio.gather` 默认 `return_exceptions=False`，异常向上传播，`asyncio.run` 在 `generate` 里直接抛出，整个批次没有返回值；代码没有 try/except 与重试。在多进程框架下这会导致该 worker 进程崩溃，相关批次不会被记入 `complete_batches`，下次重跑会重新处理（这是 u2-l2 的内容）。

**练习 2**：为什么 `generate` 不直接做成 `async def`，而要包一层 `asyncio.run`？

**参考答案**：`generate` 的调用方（`mp_generate` 的批处理循环，乃至未来的任何脚本）是普通同步代码；提供同步门面后，调用方无需理解事件循环即可使用。同时 `asyncio.run` 自带「新建循环→运行→关闭循环」的完整生命周期管理，避免调用方手工管理循环。

**练习 3**：一批 16 条输入经过 `generate` 后，输出记录里一定**新增**了哪两个键？原有的 `messages` 键还在吗？

**参考答案**：新增 `output` 与 `finish_reason`（小写）。`messages` 仍在——`{**item, ...}` 先展开原字段再覆盖，`main.py` 后续还依赖这一点（例如 [inference/main.py:L339](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L339) 在组装精炼输入时明确把 `messages` 等键剔除，反证它们此前一直存在）。

## 5. 综合实践

**任务**：编写 `mini_api.py`，用 `MockAPIModel` 在**完全不发网络请求**的情况下复刻 `APIModel` 的行为，验证 `</think>` 拼接逻辑与 `finish_reason` 透传。

1. **实践目标**：证明你已经理解 `APIModel` 三层结构——只要替换 `client.chat.completions.create` 这一个「网络边界」，其余逻辑可以原封不动地复用与测试。

2. **操作步骤**：

   a. 把 `inference/generate.py` 的 `APIModel` 类复制到一个新文件 `mini_api.py`（示例代码，建议放在你自己的实验目录，不要改动仓库源文件），并按下述方式替换 `__init__` 与请求边界，`generate_one` / `generate_all` / `generate` **三个方法保持逐字不变**：

   ```python
   # mini_api.py（示例代码，非仓库原有文件）
   import asyncio

   class FakeMessage:
       def __init__(self, reasoning_content, content):
           self.reasoning_content = reasoning_content
           self.content = content

   class FakeChoice:
       def __init__(self, message, finish_reason):
           self.message = message
           self.finish_reason = finish_reason

   class FakeResponse:
       def __init__(self, choices):
           self.choices = choices

   class FakeCompletions:
       async def create(self, messages, stream=False, **sampling_params):
           await asyncio.sleep(0.05)                       # 模拟网络延迟
           n = sum(len(m["content"]) for m in messages)    # 用输入长度制造差异
           return FakeResponse([FakeChoice(
               FakeMessage(
                   f"思考第 {n} 号输入的过程",               # reasoning_content
                   "## Solution\n证明略\n\n## Self Evaluation\n\\boxed{1}",  # content
               ),
               "Stop",                                      # 故意大写，测试 .lower()
           )])

   class MockAPIModel:
       def __init__(self):
           class C: pass
           self.client = C()
           self.client.chat = C()
           self.client.chat.completions = FakeCompletions()

       # ↓↓↓ 从 generate.py L23-L66 原样复制 generate_one / generate_all / generate ↓↓↓

   if __name__ == "__main__":
       import time
       model = MockAPIModel()
       input_data = [{"messages": [{"role": "user", "content": f"题目 {i}"}]} for i in range(10)]
       t0 = time.time()
       out = model.generate(input_data, sampling_params=dict(temperature=1.0, max_tokens=1024))
       elapsed = time.time() - t0

       assert len(out) == 10
       for i, item in enumerate(out):
           assert item["finish_reason"] == "stop"          # 大写 Stop 被 .lower() 透传
           assert "\n</think>\n" in item["output"]          # 拼接标记存在
           assert not item["output"].startswith("<think>")  # 复现「开头标签被丢弃」
           assert item["output"].endswith("\\boxed{1}")     # 正文在 </think> 之后
           assert item["messages"][0]["content"] == f"题目 {i}"  # 原字段保留且顺序不乱
       print(f"10 条并发完成，耗时 {elapsed:.2f}s（串行预期 >0.50s）")
   ```

   b. 运行 `python mini_api.py`。

3. **需要观察的现象**：断言全部通过；总耗时明显小于 10 × 0.05 = 0.5 秒（接近单次 0.05 秒多一点），证明 10 条请求确实并发执行而非顺序执行。

4. **预期结果**：输出末行打印的耗时约在 0.05-0.10 秒量级；`finish_reason` 全部为小写 `stop`；每条 `output` 形如 `思考第 N 号输入的过程\n</think>\n## Solution...`。若把 `FakeCompletions.create` 里的 `content` 改成空字符串 `""`，断言 `"\n</think>\n" in item["output"]` 应当失败（走无正文分支）——这正好反向验证了 4.2 的对照表。

5. 本实践只依赖标准库与仓库 `generate.py` 的代码副本，不联网、不需要 API Key；具体耗时数字因机器而异，**待本地验证**。

## 6. 本讲小结

- `APIModel` 采用三层结构：`generate_one`（单次异步请求）→ `generate_all`（`asyncio.gather` 并发）→ `generate`（`asyncio.run` 同步门面），职责切分干净。
- API 配置唯一入口是 `__init__` 中硬编码的 `api_key` 与 `base_url`；`timeout=300000` 秒实际上禁用了超时。
- 拼接逻辑有个真实细节：`content` 非空时输出是 `{reasoning_content}\n</think>\n{content}`，开头的 `<think>` 标签因重新赋值而丢失；下游只依赖 `</think>` 做切分，因此无伤大雅。
- `finish_reason` 在 `generate` 里被 `.lower()`，与 `</think>` 存在性一起构成下游「样本完整」的双重判据。
- `asyncio.gather` 保证结果顺序与输入顺序一致，使 `zip(input_data, outputs)` 的一一配对成立；但单请求异常会让整批失败，代码没有重试机制。
- 输出记录用 `{**item, "output": ..., "finish_reason": ...}` 合并，原输入字段全部保留，这是下游各阶段能在同一记录里同时读到输入与输出的基础。

## 7. 下一步学习建议

下一讲 **u2-l2（多进程队列与断点续跑）** 将沿着 [inference/generate.py:L68-L181](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L68-L181) 继续向下读：`mp_generate` 的批处理循环、`mp_generate_loop` 为何每个进程 `sleep(5)` 后各建一个 `APIModel`、`__main__` 如何按 `batch_size` 切批并通过 `multiprocessing.Queue` 分发、以及 `.meta` pickle 中 `complete_batches` 如何实现幂等续跑。建议阅读前先弄清 `Queue.put/get` 的阻塞语义与 `(None, None)` 哨兵约定，本讲的 `generate` 正是 worker 进程处理每个批次的最终落点。
