# GPT-3.5 调用工具 utils.py

## 1. 本讲目标

学完本讲，读者应该能够：

- 说清 `askGPT35` 是如何用「system + user」两条消息构造一次 GPT-3.5 请求的，以及 `is_response` 开关为什么能让同一个函数既「造题目」又「造代码」。
- 复述 `askGPT35` 的重试循环：最多容忍几次「maximum context」错误、每次失败 `max_gen_tokens` 如何衰减、彻底失败时返回什么。
- 解释 `load_json` / `save_json` 为什么用「一行一个 JSON」的 JSONL 格式，以及它与断点续跑的关系。
- 看懂 `utils.py` 与 `instruction_gen.py` 之间的契约：`askGPT35` 返回的列表结构如何被下游解析、`load_json/save_json` 如何被主循环复用。

## 2. 前置知识

进入源码前，先回顾四个基础概念：

- **ChatCompletion 的消息格式**：OpenAI 的对话接口接收一个「消息列表」，每条消息是 `{"role": ..., "content": ...}`。`role` 常见取值有 `system`（设定模型人设/角色）和 `user`（用户实际提问）。system 消息就像「开考前的角色分配」，user 消息才是「考题」。
- **OpenAI Python SDK 的 0.28 旧版 API**：RTL-Coder 的 [requirements.txt:L6](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/requirements.txt#L6) 锁定了 `openai==0.28.0`，这是 1.0 大改版之前的旧接口——调用入口是 `openai.ChatCompletion.create(engine=..., messages=...)`，异常基类是 `openai.error.OpenAIError`。如今 `openai>=1.0` 已改成 `openai.chat.completions.create(model=...)`，所以本讲的代码必须配 0.28 版才能直接跑。
- **上下文长度（context length）与 max_tokens**：一个模型单次请求能处理的 token 总量有上限，公式是「输入 prompt 的 token 数 + 允许生成的 `max_tokens`」不能超过该上限。超了就会报 `maximum context length ... exceeded` 这类错误。`max_tokens` 是「生成预算」，prompt 是「必交费用」，超限时唯一能砍的就是生成预算。
- **JSONL（逐行 JSON）**：即 Newline-Delimited JSON——文件里每一行是一个独立的、完整的 JSON 对象，行与行之间用换行分隔。它和「一个大 JSON 数组」的区别是：可以逐行流式读写、追加，而不必把整个文件解析进内存。

承接 [u2-l1](u2-l1-data-generation-overview.md)：数据生成的三个阶段共用同一个 GPT 调用工具 `askGPT35`，靠 `is_response` 开关在「造题目」和「造代码」之间切换；`instruction_gen.py` 只负责编排（加载、循环、解析、去重、落盘），真正的「调 GPT」和「读写数据」都封装在 `utils.py` 里。承接 [u2-l2](u2-l2-mutation-prompt-template.md)：`encode_prompt_uniq` 拼出的完整 prompt，最终正是交给本讲的 `askGPT35` 发送出去的。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [data_generation/utils.py](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/utils.py) | 工具箱。`askGPT35` 封装 GPT-3.5 调用（含 system/user 消息构造、重试、上下文超限降级），`load_json`/`save_json` 提供逐行 JSONL 读写。 |
| [data_generation/instruction_gen.py](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py) | `utils.py` 的消费者。主循环里调用 `utils.askGPT35`（L126）、`utils.load_json`（L98/L103）、`utils.save_json`（L159），并用 `post_process_gpt3_response_uniq` 解析返回结构。 |
| [requirements.txt](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/requirements.txt) | 锁定 `openai==0.28.0`，解释了 `askGPT35` 里旧式 API 写法的来源。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块拆解：先看 `askGPT35` 怎么用 system/user 消息和 `is_response` 开关复用于两个阶段（4.1）；再看它的重试循环和「上下文超限就砍 `max_tokens`」的降级策略（4.2）；最后看 `load_json`/`save_json` 的逐行 JSONL 设计（4.3）。

### 4.1 `askGPT35` 的 system/user 消息构造与 is_response 模式

#### 4.1.1 概念说明

`askGPT35` 是数据生成流程里**唯一**与 OpenAI 通信的函数。它的核心设计是：**用一个 `is_response` 布尔开关，让同一个函数服务两个完全不同的阶段**——

- `is_response=False`（默认）：阶段 2「造题目」。system 消息是**空字符串**，相当于不给模型任何角色限定，让它自由地按 prompt 模板（见 [u2-l2](u2-l2-mutation-prompt-template.md)）改写出新的 Verilog 需求指令。
- `is_response=True`：阶段 3「造参考代码」。system 消息是 `"I want you act as a Professional Verilog coder."`，把模型的角色钉死成「专业 Verilog 程序员」，让它为某条需求指令写出对应的 RTL 代码。

这样做的好处是**避免重复造轮子**：重试、超限降级、返回结构这些通用逻辑只写一遍，两个阶段只差一句 system 提示词。

#### 4.1.2 核心流程

消息构造逻辑的伪代码：

```
askGPT35(question, is_response):
  if is_response:                       # 造代码
    system = "I want you act as a Professional Verilog coder."
  else:                                 # 造题目
    system = ""                          # 空系统提示
  p_message = [
    {role: system, content: system},
    {role: user,   content: question},   # question 就是拼好的 prompt 模板
  ]
  ……（进入 4.2 的重试循环）
```

两个阶段的差异**只在 system 这一行**，user 消息统一就是传入的 `question` 字符串。

#### 4.1.3 源码精读

函数签名与消息构造：[data_generation/utils.py:L34-L45](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/utils.py#L34-L45)，逐段说明：

- **签名**（L34）：`def askGPT35(question, model='gpt-35-turbo', is_response=False, temperature=0.7)`。三个默认值要留意：`model='gpt-35-turbo'`（注意是连字符 `35`，不是 `3.5`，这是 Azure 部署名的常见风格）；`is_response=False`（默认造题目）；`temperature=0.7`（中等随机性）。
- **造代码分支**（L37-L40）：system 消息设为「Professional Verilog coder」。
- **造题目分支**（L42-L45）：system 消息是空字符串 `''`。

> 关于 `model='gpt-35-turbo'` 与 `engine=model`：函数里用的是 `openai.ChatCompletion.create(engine=model, ...)`（见 4.2.3 的 L55-L60），关键字是 `engine` 而不是 `model`。`engine` 是 **Azure OpenAI** 部署名的参数（本地部署名常把 `gpt-3.5-turbo` 写成 `gpt-35-turbo`）。配合 [requirements.txt:L6](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/requirements.txt#L6) 的 `openai==0.28.0`，可以判断这套代码当初是面向 Azure OpenAI 服务、用旧版 SDK 写的。若改用现在的 `openai>=1.0`，`ChatCompletion.create` 和 `openai.error.OpenAIError` 都已不存在，必须迁移。

**一个诚实的观察**：尽管 `is_response=True` 分支是为阶段 3（参考代码生成）准备的入口，仓库里 shipped 的驱动脚本 `instruction_gen.py` **只调用过 `is_response=False`** 这一路——见 [data_generation/instruction_gen.py:L126](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L126) 的 `results = utils.askGPT35(question=batch_inputs[0])`（未传 `is_response`，走默认 False）。所以 `is_response=True` 是「已就绪、但当前没有 shipped 批量脚本去驱动」的阶段 3 入口（与 [u2-l1](u2-l1-data-generation-overview.md) 的结论一致：阶段 3 的批量驱动待确认）。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是用肉眼确认「两阶段只差 system 一行」。

1. 实践目标：不运行代码，仅靠阅读，预测 `is_response=True` 与 `is_response=False` 两次调用发出去的 `p_message` 各是什么。
2. 操作步骤：
   - 打开 [data_generation/utils.py:L36-L45](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/utils.py#L36-L45)。
   - 假设 `question = "请把这道题改写成 4 级流水线"`，手写出两种 `is_response` 下的 `p_message`。
3. 需要观察的现象：两个 `p_message` 的 user 消息完全相同，只有 system 的 `content` 不同。
4. 预期结果：
   - `is_response=False`：`[{"role":"system","content":""}, {"role":"user","content":"请把这道题改写成 4 级流水线"}]`
   - `is_response=True`：`[{"role":"system","content":"I want you act as a Professional Verilog coder."}, {"role":"user","content":"请把这道题改写成 4 级流水线"}]`
5. 结论：`is_response` 是「单人设切换器」——同一份重试/降级逻辑，靠这一行 system 文本服务两个阶段。

#### 4.1.5 小练习与答案

**练习 1**：为什么「造代码」要给 system 提示，而「造题目」反而给空的 system 提示？

> **答案**：造代码需要模型进入「专业 Verilog 程序员」的人设，才能输出语法正确、可综合的 RTL；造题目则是让模型按 prompt 模板（`p_example.txt`，见 [u2-l2](u2-l2-mutation-prompt-template.md)）自由改写需求，模板本身已经把角色和格式约束写死了，不需要再用 system 重复限定，留空反而避免干扰。

**练习 2**：如果要把这套代码从 Azure OpenAI 迁到如今的 `openai>=1.0` SDK，`askGPT35` 里至少有哪两处必须改？

> **答案**：① 调用入口 `openai.ChatCompletion.create(engine=...)` 要改成 `openai.chat.completions.create(model=...)`；② 异常 `openai.error.OpenAIError` 在新版已移除，要改成 `openai.APIError` 之类的新异常类。

### 4.2 重试与「maximum context」超限降级

#### 4.2.1 概念说明

调用远程 GPT 接口有三类常见失败：网络抖动/限流、上下文超长（`maximum context`）、模型偶发错误。`askGPT35` 用一个 `while True` 循环 + `try/except` 兜住这些失败，并对「上下文超长」这一类做了**特殊处理**——自动缩小 `max_tokens`（生成预算）后再重试。直觉是：上下文超长意味着「prompt + 生成预算」之和撞上了模型的上下文上限；prompt 已经拼好改不动了，唯一能砍的就是生成预算，于是每失败一次就把 `max_tokens` 除以 1.3，期望下一次能塞进上限内。

#### 4.2.2 核心流程

重试循环的伪代码：

```
max_gen_tokens = 2048      # 初始生成预算
count = 0                  # 仅统计「maximum context」失败次数
while True:
    if count == 5:                          # 砍了 5 次还失败 → 放弃
        return [{finish_reason: 'length', text: ''}]
    try:
        resp = ChatCompletion.create(max_tokens=max_gen_tokens, ...)
        dic = {text: resp..., finish_reason: ...}
        break                               # 成功 → 跳出
    except OpenAIError as e:
        if 'maximum context' in str(e):     # 只有这一类才计数 + 降级
            count += 1
            max_gen_tokens = int(max_gen_tokens / 1.3)
        log("OpenAIError ...")
        log("Hit request rate limit; retrying...")   # ← 见 4.2.3 的诚实观察
        sleep(2)
return [dic]
```

`max_gen_tokens` 的衰减是一个递推过程（带向下取整）：

\[ a_0 = 2048,\qquad a_k = \left\lfloor \frac{a_{k-1}}{1.3} \right\rfloor \]

每砍一步保留约 \( 1/1.3 \approx 0.769 \) 的生成预算，即砍掉约 \( 23\% \)。忽略取整的闭式近似为：

\[ a_k \approx 2048 \cdot \left(\frac{1}{1.3}\right)^{k} \]

#### 4.2.3 源码精读

重试循环主体：[data_generation/utils.py:L46-L72](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/utils.py#L46-L72)，关键行解读：

- **初始预算与计数器**（L46-L47）：`max_gen_tokens = 2048`、`count = 0`。
- **放弃条件**（L49-L53）：`count == 5` 时返回 `[{'finish_reason': 'length', 'text': ''}]`——注意这个返回结构与正常成功时**形状一致**（都是「单元素列表、元素含 `text` 和 `finish_reason`」），只是 `finish_reason` 被标成 `'length'`、`text` 为空。这个统一的形状是 `utils` 与编排层之间的契约（见下方「下游消费」）。
- **真正的 API 调用**（L54-L63）：`openai.ChatCompletion.create(engine=model, messages=p_message, temperature=temperature, max_tokens=max_gen_tokens)`，成功则取出 `choices[0].message.content` 存进 `dic` 并 `break`。
- **异常处理与降级**（L65-L71）：`except openai.error.OpenAIError as e`；只有当错误信息里含 `'maximum context'` 时，才 `count += 1` 且 `max_gen_tokens = int(max_gen_tokens / 1.3)`；随后 `logging.warning` 打两条日志，`time.sleep(2)` 后回到循环顶部。

**这里有两个必须看懂的「诚实观察」**：

1. **`count` 的计数是不对称的**。只有 `'maximum context'` 这一类错误会让 `count += 1`；其他错误（真正的限流、超时、网络错误）**不会**累加 `count`，只会 `sleep(2)` 后无限重试，直到不再报错为止。也就是说，所谓「5 次重试」其实是**专门针对上下文超长的降级上限**，不是所有错误的总重试上限。读代码时若把 L49 的 `count == 5` 理解成「任何错误重试 5 次就放弃」，就错了。

2. **L70 的日志有误导性**。无论捕获到哪种 `OpenAIError`，它都打印 `"Hit request rate limit; retrying..."`。但真正触发 `count` 与降级的只有上下文超长那一类。所以日志说的是「限流」，实际可能在处理的是「上下文超长」——开发者把两种情况合并在同一条文案里了。读日志排错时要注意这一点。

**下游消费（契约验证）**：放弃时返回的 `finish_reason == 'length'`，正好被编排层的解析函数识别并优雅丢弃——见 [data_generation/instruction_gen.py:L49-L66](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L49-L66)，其中 L53-L54：

```python
if response["finish_reason"] == "length":
    return []          # 放弃的样本被解析成空列表，不进入落盘
```

所以一次「彻底放弃」不会让程序崩溃，而是悄悄变成「本轮没有产出新指令」——这就是 `utils` 与 `instruction_gen` 之间的默契。

#### 4.2.4 代码实践

这是本讲的核心**可运行实践**，对应任务规格：把 `askGPT35` 改写成一个本地 mock，让前 3 次抛出 `'maximum context'` 错误，验证重试逻辑会逐步减小 `max_gen_tokens`。整个脚本**不联网、也不依赖 `openai==0.28.0`**，用一个「假 openai 对象」顶替真实调用，因此可以确定性预测输出。

1. 实践目标：亲眼看到 `max_gen_tokens` 按 \( 2048 \to 1575 \to 1211 \to 931 \) 衰减，并在第 4 次成功返回固定文本。
2. 操作步骤：在 `data_generation/` 目录下新建 `mock_askgpt35.py`（**示例代码，非项目原有文件**）。它做了三件事：① 定义一个假的 `openai` 对象（含 `ChatCompletion.create` 与 `error.OpenAIError`）；② 把 `askGPT35` 的逻辑原样照搬，只把 `openai` 指向假对象；③ 在 `except` 里多打印一行，暴露每次衰减后的 `max_gen_tokens`。

   ```python
   # 示例代码：mock_askgpt35.py —— 离线复现 askGPT35 的重试与 max_tokens 降级
   import time

   # —— 1. 假的 openai 层（无需联网、无需安装 openai==0.28.0）——
   class FakeOpenAIError(Exception):
       """模拟 openai.error.OpenAIError"""

   class _FakeChatCompletion:
       call_log = []          # 记录每次调用收到的 max_tokens
       raise_n = 3            # 前 raise_n 次模拟「maximum context」错误

       @staticmethod
       def create(engine, messages, temperature, max_tokens):
           _FakeChatCompletion.call_log.append(max_tokens)
           if len(_FakeChatCompletion.call_log) <= _FakeChatCompletion.raise_n:
               raise FakeOpenAIError(
                   "This model's maximum context length is ... exceeded.")
           # 第 raise_n+1 次起：返回固定文本
           return {"choices": [{"message": {"content": "{Instruction}\nfake task\n{Input}\nmodule fake();"},
                                  "finish_reason": "stop"}]}

   class _FakeOpenAI:                          # 用最小接口顶替 import openai
       ChatCompletion = _FakeChatCompletion
       class error:
           OpenAIError = FakeOpenAIError
   openai = _FakeOpenAI()

   # —— 2. askGPT35 逻辑（结构照搬 utils.py L34-L72，仅 openai 换成假对象）——
   def askGPT35(question, model='gpt-35-turbo', is_response=False,
                temperature=0.7, sleep_time=0):   # sleep_time 默认 0 加速观察
       if is_response is True:
           p_message = [
               {'role': 'system', 'content': 'I want you act as a Professional Verilog coder.'},
               {'role': 'user', 'content': question}]
       else:
           p_message = [
               {'role': 'system', 'content': ''},
               {'role': 'user', 'content': question}]
       max_gen_tokens = 2048
       count = 0
       while True:
           if count == 5:
               return [{'finish_reason': 'length', 'text': ''}]
           try:
               response = openai.ChatCompletion.create(
                   engine=model, messages=p_message,
                   temperature=temperature, max_tokens=max_gen_tokens)
               ans = response['choices'][0]['message']['content']
               dic = {'text': ans,
                      'finish_reason': response['choices'][0]['finish_reason']}
               break
           except openai.error.OpenAIError as e:
               if 'maximum context' in str(e):
                   count += 1
                   max_gen_tokens = int(max_gen_tokens / 1.3)
               print(f"[retry] count={count}, next max_gen_tokens={max_gen_tokens}")
               time.sleep(sleep_time)
       return [dic]

   if __name__ == "__main__":
       result = askGPT35(question="模板内容……")
       print("最终返回：", result)
       print("每次调用收到的 max_tokens 序列：", _FakeChatCompletion.call_log)
   ```

   运行 `python mock_askgpt35.py`。

3. 需要观察的现象：`[retry]` 行会打印 3 次，`count` 从 1 涨到 3，`next max_gen_tokens` 依次为 1575、1211、931；第 4 次调用成功并返回固定文本。
4. 预期结果（确定性输出）：

   ```text
   [retry] count=1, next max_gen_tokens=1575
   [retry] count=2, next max_gen_tokens=1211
   [retry] count=3, next max_gen_tokens=931
   最终返回： [{'text': '{Instruction}\nfake task\n{Input}\nmodule fake();', 'finish_reason': 'stop'}]
   每次调用收到的 max_tokens 序列： [2048, 1575, 1211, 931]
   ```

5. 进阶观察：把 `_FakeChatCompletion.raise_n` 改成 `6`（大于 5），模拟「砍 5 次还失败」。此时第 6 次循环顶部 `count == 5` 命中放弃分支，返回 `[{'finish_reason': 'length', 'text': ''}]`，`call_log` 为 `[2048, 1575, 1211, 931, 716]`——注意 716 之后的 `550` 被算出来却**没有**真正发给 API（放弃分支在调用前就 `return` 了）。这一现象验证了 4.2.3 的「最多实际发起 5 次带衰减的请求」。

#### 4.2.5 小练习与答案

**练习 1**：把 `_FakeChatCompletion.raise_n` 设为 `6` 后，函数返回什么？这个返回值会被下游怎么处理？

> **答案**：返回 `[{'finish_reason': 'length', 'text': ''}]`。下游 `post_process_gpt3_response_uniq`（[instruction_gen.py:L53-L54](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L53-L54)）检测到 `finish_reason == 'length'` 就返回 `[]`，于是这一轮没有任何新指令落盘，程序继续下一轮而非崩溃。

**练习 2**：如果把 L66 的条件从 `'maximum context' in str(e)` 改成 `True`（即任何错误都计数 + 降级），行为会有什么变化？

> **答案**：那么「真正的限流/网络错误」也会消耗 `count` 并砍 `max_tokens`。砍 `max_tokens` 对限流毫无帮助（限流不是上下文超长），反而白白缩小生成预算；最多 5 次后就放弃。当前代码只对上下文超长降级，是把「降级」这个动作精准地用在了它唯一能起作用的场景上。

**练习 3**：为什么衰减用「除以 1.3」而不是「除以 2」？

> **答案**：除以 1.3 每次只砍约 23%，是「温和收缩」——既大概率能塞回上下文上限，又不至于一下子把生成预算砍得太狠导致输出被截断；除以 2 每次砍一半，过于激进，容易让本来能生成的较长代码被截断。1.3 是一个经验性的折中常数。

### 4.3 `load_json` / `save_json`：逐行 JSONL 读写

#### 4.3.1 概念说明

数据生成流程要在磁盘和内存之间反复搬运「指令-代码」样本——加载种子、读回中途产物（断点续跑）、每轮落盘新结果。`load_json`/`save_json` 就是这套搬运的统一接口，二者都采用 **JSONL（每行一个 JSON 对象）** 格式。这个格式不是随便选的：它和断点续跑、流式读写天然契合，并且让生成样本（`data_sample.json`）、训练集（`Resyn27k.json`）、评分样本（`scoring_data_sample.json`）共用同一套读写逻辑（见 [u1-l4](u1-l4-data-formats.md)）。

#### 4.3.2 核心流程

两个函数的伪代码：

```
load_json(filename):
    des_data = []
    逐行读取文件:
        des_data.append(json.loads(该行))   # 每行独立解析成一个 dict
    return des_data                          # 返回 dict 列表

save_json(dic_list, path):
    以 'w' 打开 path:
        for dic in dic_list:
            写入 json.dumps(dic) + '\n'      # 每个 dict 压成一行
```

落盘后的文件长这样（每行一个完整 JSON，字段就是 [u1-l4](u1-l4-data-formats.md) 讲过的 `Instruction`/`Input`/`Response`）：

```text
{"Instruction": "...", "Input": "...", "Response": ["..."]}
{"Instruction": "...", "Input": "...", "Response": ["..."]}
```

#### 4.3.3 源码精读

- **`load_json`**：[data_generation/utils.py:L18-L24](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/utils.py#L18-L24)。`for line in f` 逐行读，`json.loads(line)` 把每一行解析成一个 dict 并 append。因为是逐行解析，文件损坏时只会丢损坏的那一行，不会让整个文件不可读。
- **`save_json`**：[data_generation/utils.py:L26-L31](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/utils.py#L26-L31)。对每个 dict 调 `json.dumps(dic)` 压成一行，再补一个 `\n`。

**一个必须看懂的细节**：`save_json` 用 `'w'` 模式打开文件，意味着**每次调用都会用 `dic_list` 的完整内容覆盖整个文件**，而不是「追加」。主循环 [instruction_gen.py:L159](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L159) 在 `while` 循环**内部**每轮都调一次 `save_json`，于是每一轮都把「到目前为止的全部 `target_instruction_data`」整体重写一遍。这样做的代价是整体写入量是 \( O(N^2) \)（N 轮、每轮写 i 条），收益是：文件始终反映「完整的当前状态」，任何时候中断，下次 `load_json`（[instruction_gen.py:L102-L103](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L102-L103) 配合 `os.path.exists`）都能读回已生成部分，从断点续跑。对生成 50 条或 2.7 万条指令来说，这个 \( O(N^2) \) 完全可接受——磁盘 IO 远比调 GPT 便宜。

**为什么不用「一个大 JSON 数组」**：如果存成 `[{...}, {...}, ...]` 的大数组，落盘时必须把整个数组序列化、加载时必须把整个文件一次性 `json.loads` 进内存；而 JSONL 可以逐行读写、天然支持「写到哪算哪」。`save_json` 虽然每次整体重写，但「逐行写入」的格式让 `load_json` 保持了流式、低内存、抗局部损坏的优点。

> **顺带一提**：[benchmark_inference/test_on_verilog-eval.py:L20](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_verilog-eval.py#L20) 里也定义了一个同名的 `load_json`——那是评测脚本**自己复制的一份**，与 `utils.py` 的不是同一个对象。本讲只讲 `data_generation/utils.py` 里的版本。

#### 4.3.4 代码实践

这是一个**可运行实践**，验证 JSONL 的「写 → 读」往返一致，并体会「逐行」格式。

1. 实践目标：用 `save_json` 写出几条样本，再用 `load_json` 读回，确认数据无损，并肉眼看到文件是「一行一个 JSON」。
2. 操作步骤：新建 `test_jsonl.py`（**示例代码**），把 `utils.py` 的两个函数复制出来本地运行：

   ```python
   # 示例代码：test_jsonl.py
   import json

   def load_json(filename):                 # 复制自 utils.py L18-L24
       des_data = []
       with open(filename, 'r') as f:
           for line in f:
               des_data.append(json.loads(line))
       return des_data

   def save_json(dic_list, path):           # 复制自 utils.py L26-L31
       with open(path, 'w') as f:
           for dic in dic_list:
               f.write(json.dumps(dic))
               f.write('\n')

   data = [
       {"Instruction": "add a counter", "Input": "module cnt();",
        "Response": ["module cnt(input clk); ... endmodule"]},
       {"Instruction": "build a fifo",  "Input": "module fifo();",
        "Response": ["module fifo(...); ... endmodule"]},
   ]
   save_json(data, "tmp_test.jsonl")        # 注意落盘文件是 .jsonl 一行一个
   back = load_json("tmp_test.jsonl")
   print("读回条数：", len(back))
   print("第一条 Response：", back[0]["Response"])
   ```

   运行 `python test_jsonl.py`，然后用 `head` 看 `tmp_test.jsonl` 的前几行。

3. 需要观察的现象：`tmp_test.jsonl` 恰好两行，每行是一个完整的 JSON 对象；读回的条数与写入一致，字段无丢失。
4. 预期结果：

   ```text
   读回条数： 2
   第一条 Response： ['module cnt(input clk); ... endmodule']
   ```

   文件内容（两行）：

   ```text
   {"Instruction": "add a counter", "Input": "module cnt();", "Response": ["module cnt(input clk); ... endmodule"]}
   {"Instruction": "build a fifo", "Input": "module fifo();", "Response": ["module fifo(...); ... endmodule"]}
   ```

5. 结论：`Response` 统一存成列表（即便只有一条），正是 [u1-l4](u1-l4-data-formats.md) 强调的「三种格式共用一套读写」的体现——`load_json`/`save_json` 不关心字段含义，只负责逐行搬运 dict。

#### 4.3.5 小练习与答案

**练习 1**：`save_json` 每轮都整体重写文件，为什么主循环还要把它放在 `while` 内部每轮都调？

> **答案**：为了断点续跑。调 GPT 成本高、耗时长且可能失败，每轮把当前全部成果落盘一次，即使中途崩溃，下次重启时 `os.path.exists` + `load_json` 也能读回已生成部分继续。磁盘重写的代价相对调 GPT 可忽略。

**练习 2**：如果某一行因为编码问题损坏，`load_json` 会怎样？

> **答案**：`json.loads(坏行)` 会抛 `JSONDecodeError`，导致整个 `load_json` 中断。因为 JSONL 每行独立，理想做法是 `try/except` 跳过坏行只丢一条；但 shipped 的 `load_json` 没有这一层保护，所以实际使用中要保证落盘数据干净（这也是为什么 `askGPT35` 放弃时返回结构化空值、由下游优雅丢弃，而不是写进坏数据）。

**练习 3**：把同样的两条数据存成「一个大 JSON 数组」（`json.dumps(data)` 一次写整个列表），相比 JSONL，会丢失本讲提到的哪个优点？

> **答案**：丢失「逐行流式读写」的优点——加载时必须一次性把整个文件读进内存并整体解析，也无法只读回最后几行做断点续跑；而且一旦某处损坏，整个数组都解析失败，无法像 JSONL 那样只丢一行。

## 5. 综合实践

把本讲三个模块串成一个**离线、不联网**的迷你测试，验证 `utils.py` 与编排层之间的完整契约：放弃时的返回值能被解析层优雅丢弃，落盘数据能被读回无损。

任务步骤：

1. 把 4.2.4 的 `mock_askgpt35.py` 与 4.3.4 的 `test_jsonl.py` 合并成一个脚本 `mini_contract.py`（**示例代码**），其中：
   - 将 `_FakeChatCompletion.raise_n` 设为 `6`（模拟「砍 5 次还失败」），调用 `askGPT35` 得到放弃返回值 `[{'finish_reason':'length','text':''}]`。
   - 把 [instruction_gen.py:L49-L66](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L49-L66) 的 `post_process_gpt3_response_uniq` 函数体复制进来，对上面的放弃返回值调用一次，观察它返回 `[]`。
2. 再构造一个「成功」的返回值（`finish_reason='stop'`，`text` 里包含 `{Instruction}` 和 `{Input}` 标记），用同一个 `post_process_gpt3_response_uniq` 解析，确认能抽出 `{"Instruction": ..., "Input": ...}`。
3. 把第 2 步抽出的若干条用 `save_json` 写成 `mini_out.jsonl`，再用 `load_json` 读回，确认往返一致。
4. 用一句话总结：`utils` 层（返回统一形状的列表 + JSONL 读写）与编排层（按 `finish_reason` 分流 + 解析切分）是如何解耦的——为什么把 `askGPT35` 换成任何返回同形状结构的本地模型，主循环都能原样复用？

预期结果：你能独立解释「放弃 → `length` → 解析成空」「成功 → `stop` → 解析成指令 → JSONL 落盘 → 读回」两条路径，并说清「工具/编排分层」让 `askGPT35` 可被任意替换的价值。真实 GPT 行为需接 OpenAI key，但本实践的契约验证全在本地完成，结论可确定。

## 6. 本讲小结

- `askGPT35` 用 `is_response` 开关复用于两个阶段：`False`（默认，造题目）给空 system 提示，`True`（造代码）给 `"I want you act as a Professional Verilog coder."`；shipped 主循环目前只调用 `is_response=False`（[instruction_gen.py:L126](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L126)）。
- `engine=model` 与 [requirements.txt:L6](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/requirements.txt#L6) 的 `openai==0.28.0` 共同说明这套代码面向 Azure OpenAI、用旧版 SDK 写的；迁到 `openai>=1.0` 需改调用入口与异常类。
- 重试循环的 `count` **只对 `'maximum context'` 错误计数 + 降级**，每砍一次 `max_gen_tokens = int(max_gen_tokens / 1.3)`；5 次后放弃并返回 `[{finish_reason:'length', text:''}]`。衰减序列约为 \( 2048 \to 1575 \to 1211 \to 931 \to 716 \)。
- 「5 次重试」是上下文超长的降级上限，**不是**所有错误的总上限——其他错误只 `sleep(2)` 后无限重试；L70 的 "rate limit" 日志对所有 `OpenAIError` 都打印，有误导性。
- 放弃时的 `length` 返回值被下游 `post_process_gpt3_response_uniq` 识别成空列表优雅丢弃，是 `utils` 与编排层的契约。
- `load_json`/`save_json` 用 JSONL（一行一个 JSON）格式；`save_json` 每轮整体重写文件以支持断点续跑，`load_json` 逐行解析、流式低内存。

## 7. 下一步学习建议

- 下一讲 [u2-l4 指令生成主循环 instruction_gen.py](u2-l4-instruction-gen-loop.md)：把本讲的 `askGPT35`、`load_json`、`save_json` 放回 `generate_instruction_following_data` 主循环，看批量调用、解析、断点续跑、落盘是如何串成一条链的。
- 想理解落盘前的去重过滤，衔接 [u2-l5 基于 ROUGE-L 的指令去重](u2-l5-rouge-dedup.md)。
- 想看 `is_response=True` 那一路在训练数据格式上的对应，回顾 [u1-l4 数据格式详解](u1-l4-data-formats.md) 里的 `Response[N] + Score[N]` 多候选结构。
