# WebUI 聊天界面与工具调用评测

## 1. 本讲目标

学完本讲，你应当能够：

- 理解 MiniMind 的 **WebUI 聊天界面**（`scripts/web_demo.py`）是如何用 Streamlit 把一个 Transformer 模型包装成「带流式输出、思考折叠、多轮工具调用」的可交互页面的。
- 掌握 `scripts/eval_toolcall.py` 这条 **工具调用评测链路**：它如何用同一套代码同时驱动 `local`（本地 torch / transformers 模型）和 `api`（OpenAI 兼容接口）两种后端，并自动跑完一组 `TEST_CASES`。
- 看懂把 `<think>` 思考块和 `<tool_call>` 工具调用标签渲染成 HTML 的可视化逻辑，以及多轮工具调用的「生成→解析→执行→拼回→续写」循环。
- 能亲手把 WebUI 跑起来体验一次多轮工具对话，并用 `eval_toolcall.py` 统计工具调用成功率，对比 `full_sft` 与 `agent` 权重的差异。

本讲是整个学习路线的「出口」之一：前面 u3-l6 讲过的 `model.generate`、u7-l6 讲过的 Agentic RL 多轮 rollout、u8-l2 讲过的 OpenAI 兼容 API，在这里被组装成两类面向真实使用场景的入口——一个给人看（WebUI），一个给程序跑（评测脚本）。

## 2. 前置知识

在进入源码之前，先用通俗语言铺三个概念。

### 2.1 什么是「工具调用（Tool Call / Function Calling）」

大模型本身只会「接龙」——预测下一个 token。它不会真的去查时间、算数学、查天气。**工具调用**就是给模型一套「外部函数清单」，让它在回答里输出一段结构化的调用指令（MiniMind 用 `<tool_call>{"name": ..., "arguments": ...}</tool_call>` 这种格式），由外层程序解析、执行这个函数，再把结果拼回对话上下文，让模型基于真实结果继续回答。

一个完整的工具调用回合通常是：

```
用户提问
  → 模型输出 <tool_call>{...}</tool_call>
  → 程序解析 + 执行工具，得到结果
  → 程序把结果以 <tool_response>...</tool_response> 拼回上下文
  → 模型再次生成，给出最终回答（或继续发起下一个 tool_call）
```

这和 u7-l6 讲的 Agentic RL 的 `rollout_single` 是同构的——训练时这个循环用来采集轨迹算奖励，推理时这个循环用来真正完成任务。

### 2.2 Streamlit 与 `session_state`

**Streamlit** 是一个用纯 Python 写 Web 页面的框架：你写一段脚本，它帮你渲染成一个网页，并且**每次用户交互（发消息、点按钮）都会把整个脚本从头到尾重新跑一遍**。

这就带来一个问题：重新跑一遍，之前的对话历史不就丢了吗？解决办法是 `st.session_state`——一个跨重跑依然存活的「字典」，专门用来记住对话历史、当前选中的模型、温度等状态。理解「脚本会被反复重跑、状态靠 `session_state` 保活」是读懂 `web_demo.py` 的关键。

### 2.3 流式输出（Streaming）

「流式」指模型一边生成一边把已经吐出的 token 显示出来，而不是等整段回答生成完才一次性显示。在 MiniMind 里这靠 `TextIteratorStreamer` / `TextIteratorStreamer`（一个可迭代对象）实现：主线程开一个后台线程去跑 `model.generate`，主线程不断从 streamer 里 `for new_text in streamer` 取新 token、刷新页面。这和 u8-l2 讲的 `CustomStreamer` 是同一类「生产者-消费者」桥。

## 3. 本讲源码地图

本讲涉及三个脚本，都在 `scripts/` 目录下：

| 文件 | 作用 | 角色 |
| --- | --- | --- |
| [scripts/web_demo.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/web_demo.py) | Streamlit WebUI：模型加载、流式聊天、思考折叠、多轮工具调用 | 给人用的可视化界面 |
| [scripts/eval_toolcall.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/eval_toolcall.py) | 工具调用评测：`local`/`api` 双后端，自动跑 `TEST_CASES` | 给程序用的批量评测 |
| [scripts/chat_api.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/chat_api.py) | OpenAI 兼容接口的最小调用示例（含 `reasoning_content`） | `api` 后端的参考客户端 |

四个最小模块与它们的落点：

- **`load_model_tokenizer`**：WebUI 的模型加载（含动态扫描目录）。
- **`process_assistant_content`**：把 `<think>` / `<tool_call>` 渲染成 HTML。
- **`execute_tool`**：工具的本地执行沙盒（两个文件各有一份）。
- **`run_case`**：评测脚本里多轮工具调用的主循环。

## 4. 核心概念与源码讲解

### 4.1 模型加载：`load_model_tokenizer` 与目录动态扫描

#### 4.1.1 概念说明

WebUI 要做到「拷进来一个模型文件夹就能用」，就不能把模型路径写死。`web_demo.py` 的做法是：**启动时扫描脚本所在目录**，把所有「看起来像模型文件夹」的子目录列出来让用户在侧边栏选。模型本身的加载则用 `transformers` 的 `AutoModelForCausalLM.from_pretrained`（即 u8-l1 讲的 transformers 格式），并用 Streamlit 的 `@st.cache_resource` 装饰器做缓存，保证脚本被反复重跑时模型只加载一次。

注意一个重要区别：WebUI（`web_demo.py`）**只加载 transformers 格式**的模型文件夹；而评测脚本（`eval_toolcall.py`）的 `init_model` 同时支持原生 torch `.pth` 和 transformers 两种格式（靠 `'model' in load_from` 判分支，见 u1-l3、u8-l1）。所以体验 WebUI 前必须先把模型转成 transformers 文件夹并放进 `scripts/`。

#### 4.1.2 核心流程

加载一个模型分两步：

1. **目录扫描**（脚本顶层执行，每次重跑都跑，但很轻）：
   - 取脚本所在目录 → 遍历子目录 → 跳过 `.` / `_` 开头的 → 判断子目录里有没有 `.bin` / `.safetensors` / `.pt` 或 `model.safetensors.index.json` → 命中就收进 `MODEL_PATHS` 字典。
   - 一个都没扫到时填一个占位项 `"No models found"`，避免下拉框空掉。
2. **模型加载**（`main` 里调用，被 `@st.cache_resource` 缓存）：
   - `AutoModelForCausalLM.from_pretrained(model_path, trust_remote_code=True)` 加载模型；
   - `AutoTokenizer.from_pretrained(...)` 加载分词器；
   - `.half().eval().to(device)` 转半精度、切推理模式、搬到 GPU/CPU。

#### 4.1.3 源码精读

目录动态扫描：

[scripts/web_demo.py:239-248](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/web_demo.py#L239-L248) — 这段在脚本顶层执行，`sorted(..., reverse=True)` 让较新的（字典序靠后的）模型排前面。第 245 行的 `any(...)` 是判定条件：只要目录里存在任一权重文件或分片索引，就认为它是模型文件夹。

模型加载函数：

[scripts/web_demo.py:198-209](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/web_demo.py#L198-L209) — 第 198 行 `@st.cache_resource` 是关键：它以 `model_path` 为 key 缓存返回值 `(model, tokenizer)`，**切换模型时**才会重新加载，避免每次发消息都重载模型（否则对话会卡死）。`trust_remote_code=True` 允许加载仓库自带的 `model_minimind.py`（u3 系列讲过的模型定义）。

对比评测脚本的加载，它多支持 torch `.pth`：

[scripts/eval_toolcall.py:57-67](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/eval_toolcall.py#L57-L67) — 第 59 行 `if 'model' in args.load_from:` 判分支（和 `eval_llm.py` 同一套字符串约定），torch 分支按 `{weight}_{hidden_size}{_moe?}.pth` 拼文件名并 `load_state_dict(strict=True)`，transformers 分支走 `from_pretrained`，最后统一 `.half().eval().to(device)`。

#### 4.1.4 代码实践

**实践目标**：理解目录扫描的判定条件，能预测哪些文件夹会被识别为模型。

**操作步骤**（源码阅读型，不需要真跑模型）：

1. 读 [scripts/web_demo.py:242-246](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/web_demo.py#L242-L246)。
2. 假设 `scripts/` 下有这些子目录：`minimind-3/`（内含 `model.safetensors`）、`__pycache__/`、`.git/`、`my_notes/`（只有 `.txt`）、`minimind-3-moe/`（内含 `model.safetensors.index.json`）。
3. 用纸笔推断 `MODEL_PATHS` 字典的最终内容。

**需要观察的现象 / 预期结果**：

- `__pycache__/` 以 `_` 开头 → 被第 244 行过滤。
- `.git/` 以 `.` 开头 → 被过滤。
- `my_notes/` 里没有权重文件 → 第 245 行 `any(...)` 为 False → 不收。
- `minimind-3/`（有 `.safetensors`）和 `minimind-3-moe/`（有 `model.safetensors.index.json`）→ 都被收进 `MODEL_PATHS`。

最终：`MODEL_PATHS = {"minimind-3-moe": [...], "minimind-3": [...]}`（`reverse=True` 让 moe 排前）。**待本地验证**：可临时在脚本里 `print(MODEL_PATHS)` 后用 `streamlit run web_demo.py` 启动确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `load_model_tokenizer` 必须加 `@st.cache_resource`？去掉会怎样？

**参考答案**：Streamlit 每次用户交互都会重跑整个脚本。不加缓存意味着每次发消息都会重新 `from_pretrained` 加载一遍模型（几秒到几十秒），对话根本没法用。`@st.cache_resource` 让相同 `model_path` 的返回值被缓存，只在切换模型时才重新加载。

**练习 2**：如果想让 WebUI 也能加载原生 torch `.pth` 权重，需要改哪里？

**参考答案**：把 `load_model_tokenizer` 里的 `AutoModelForCausalLM.from_pretrained(...)` 换成 `eval_toolcall.py` 的 `init_model` 那套逻辑——先用 `MiniMindConfig(...)` 实例化 `MiniMindForCausalLM`，再 `load_state_dict(torch.load(...))`。但目录扫描的判定条件（找 `.safetensors`）也得相应改成识别 `out/` 下的 `.pth`，改动较大，所以仓库选择让 WebUI 只吃 transformers 格式。

---

### 4.2 思考与工具调用的可视化：`process_assistant_content`

#### 4.2.1 概念说明

模型生成的回答里可能混着两类「不是给人直接看」的内容：

- **`<tool_call>...</tool_call>`**：模型发起的工具调用指令（一段 JSON）。
- **`<think>...</think>`**：模型的显式思考过程（u2-l1、u8-l2 讲过的「思考」标签）。

直接把这两段原文显示给用户会很丑也很难读。`process_assistant_content` 的职责就是**用正则把这两类标签替换成好看的 HTML 片段**：工具调用变成一张蓝色「ToolCalling」卡片，思考过程变成一个可折叠的「已思考 / 思考中...」灰色块。

难点在于**流式渲染**：思考标签是逐 token 到达的，刚生成时只有 `<think>` 还没有 `</think>`，函数必须能处理这种「半截标签」的中间状态，并随着内容增长不断更新 UI。

#### 4.2.2 核心流程

函数对输入 `content` 依次做四件事（顺序很重要）：

```
1. 若含 <tool_call>：
     用正则 <tool_call>(.*?)</tool_call> 抓出每段 JSON，
     替换成「ToolCalling」蓝色卡片 HTML（name + arguments）。

2.【仅流式 + 开启思考 + 当前还没出现任何 think 标签】
     启发式判断：在内容里找 \n\n我是 / 您好 / 你好 这种「答案开头」，
     找到就把前半段当思考、后半段当答案，分别套折叠/正文；
     找不到且内容已有长度，就把整段塞进「思考中...」折叠块。
     （这是给「模型把思考写成纯散文、没用标签」的兜底分支。）

3. 同时有 <think> 和 </think>：
     完整的思考块 → 替换成「已思考」折叠块；空思考则直接删掉。

4. 只有 <think> 没有 </think>：
     思考还在生成中 → 套「思考中...」折叠块（column-reverse 让最新内容在底部）。

5. 没有 <think> 但有 </think>：
     起始标签被模板/解码吃掉了 → 把残段也包成「已思考」。
```

之所以要分这么多情况，是因为：

- 训练时模板可能注入**空的** `<think>\n\n</think>`（u2-l1 的 `open_thinking=0` 直答模式），渲染时要把它删干净。
- 流式时 `<think>` 先到、`</think>` 后到，中间状态要显示「思考中...」。
- `<think>` / `</think>` 在 `tokenizer_config.json` 里是 `special: false`（[model/tokenizer_config.json:206-221](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/tokenizer_config.json#L206-L221)），所以 `skip_special_tokens=True` 不会删它们，会以原文形式进入 `content`，正则才能匹配到。

#### 4.2.3 源码精读

工具调用卡片渲染：

[scripts/web_demo.py:151-160](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/web_demo.py#L151-L160) — `format_tool_call` 把抓到的 JSON 解析出 `name` 和 `arguments`，拼成一张带样式的 `<div>` 卡片；`re.DOTALL` 让 `.` 能匹配换行（工具参数可能跨行）。解析失败（`except`）时原样返回，保证页面不崩。

完整思考块与进行中思考块：

[scripts/web_demo.py:173-185](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/web_demo.py#L173-L185) — 第 173 行处理「完整块」（`<think>`+`</think>` 都在），第 176 行 `if think_content.replace('\n', '').strip()` 判断思考是否为空，空则返回 `''`（删掉空思考）。第 181 行处理「只有头没尾」的进行中状态，用 `display: flex; flex-direction: column-reverse;` 让滚动条停在最新生成的内容上。

调用入口（流式 vs 历史）：

[scripts/web_demo.py:381](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/web_demo.py#L381) — 流式生成时传 `is_streaming=True`；[scripts/web_demo.py:323](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/web_demo.py#L323) — 回显历史消息时不传（默认 `False`），跳过那个依赖 `st.session_state['enable_thinking']` 的启发式分支。

#### 4.2.4 代码实践

**实践目标**：验证 `process_assistant_content` 对不同输入的渲染行为。

**操作步骤**（隔离测试，避开 Streamlit 启动）：

`web_demo.py` 顶层有 `st.set_page_config(...)`，import 时就会触发 Streamlit 上下文，不能直接 `import`。所以把函数体复制到一个独立脚本里测试（**下面是示例代码，非项目原有代码**）：

```python
# 示例代码：从 web_demo.py 摘出 process_assistant_content 的 tool_call 分支做隔离测试
import re, json

def fmt_tool_call(content):
    def format_tool_call(match):
        tc = json.loads(match.group(1))
        return f'[ToolCalling] {tc.get("name")}: {json.dumps(tc.get("arguments"), ensure_ascii=False)}'
    return re.sub(r'<tool_call>(.*?)</tool_call>', format_tool_call, content, flags=re.DOTALL)

s = '<tool_call>{"name": "get_current_time", "arguments": {"timezone": "Asia/Shanghai"}}</tool_call>'
print(fmt_tool_call(s))
```

**需要观察的现象 / 预期结果**：输出形如 `[ToolCalling] get_current_time: {"timezone": "Asia/Shanghai"}`，说明正则确实把整段 `<tool_call>` 替换成了格式化文本。

**进阶**：把 `<think>\n让我想想\n</think>\n\n答案是 42` 喂给完整版 `process_assistant_content`（需在 Streamlit 上下文里跑，或临时把依赖 `st.session_state` 的第 163 行注释掉），观察思考部分被收进「已思考」折叠块、答案留在正文。

#### 4.2.5 小练习与答案

**练习 1**：为什么流式渲染时会出现「只有 `<think>` 没有 `</think>`」的情况？非流式（回显历史）时会出现吗？

**参考答案**：流式是逐 token 输出，`<think>` 标签先到、思考正文正在生成、`</think>` 还没生成出来，所以中间快照里只有头没尾。非流式回显历史时，整段回答已经生成完毕，要么头尾都有、要么都没有，不会出现「半截」状态，所以历史回显不需要第 181 行那个进行中分支。

**练习 2**：第 176 行为什么要 `if think_content.replace('\n', '').strip(): ... return ''`？

**参考答案**：这是为了处理 `open_thinking=0` 时模板注入的空思考块 `<think>\n\n</think>`。如果思考内容全是换行/空白，就直接返回空串把它从界面上删掉，不显示一个空的「已思考」折叠块干扰用户。

---

### 4.3 工具执行沙盒：`execute_tool`

#### 4.3.1 概念说明

模型只负责「喊一声我要调哪个工具、传什么参数」，真正执行工具的是外层程序。MiniMind 的两个脚本各自实现了一份 `execute_tool`，且工具的「真实逻辑」都是**模拟（mock）**的——比如天气永远返回「晴, 7~10°C」、汇率永远返回 `7.2`。这是因为项目重点是验证「模型能不能正确发起工具调用」，而不是真去对接天气 API。

两份实现的差异：

- `web_demo.py` 的 `execute_tool`：用一长串 `if/elif` 分发，返回 `{"result": ...}`，适合在页面里显示。
- `eval_toolcall.py` 的 `execute_tool` + `MOCK_RESULTS`：用字典把工具名映射到 lambda，返回结构化字典（如 `{"characters": ..., "words": ...}`），适合程序化校验。它还接受两种入参形态（字典 `call` 或裸 `name`+`arguments`），因为 `local` 和 `api` 后端传进来的结构不同。

#### 4.3.2 核心流程

以 `eval_toolcall.py` 的 `execute_tool` 为例：

```
入参 call（可能是 dict，也可能是裸字符串 name）和 arguments
  ↓
第 1 步：从 call 里取出 name（兼容 dict 和 str 两种形态）
第 2 步：从 call/arguments 里取出原始参数 raw_args
         若是字符串（API 后端的 arguments 是 JSON 字符串）→ json.loads
         若已是 dict（local 后端）→ 直接用
         解析失败 → args = {}
第 3 步：fn = MOCK_RESULTS.get(name)
         没有这个工具 → 返回 {"error": "未知工具"}
第 4 步：return fn(args)，执行失败 → 返回 {"error": "工具执行失败: ..."}
```

注意 `MOCK_RESULTS` 里 `calculate_math` 的 lambda 用了 `eval`（[scripts/eval_toolcall.py:30](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/eval_toolcall.py#L30)）：执行前会把 `^→**`、`×→*`、`÷→/` 等常见写法归一化，再交给 Python 的 `eval` 算出数值。这正是 README 那 20 道数学题能自动判对错（`gt` vs `pred`）的底层机制。`eval` 执行表达式有注入风险，但这里输入来自受控的数学表达式、且是本地演示，可接受；u7-l6 的 `train_agent.py` 里类似沙盒额外加了 `SIGALRM` 1 秒超时。

#### 4.3.3 源码精读

评测版的工具映射表与执行：

[scripts/eval_toolcall.py:29-38](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/eval_toolcall.py#L29-L38) — `MOCK_RESULTS` 把每个工具名映射到一个 lambda，注意返回的字段名因工具而异（`result` / `datetime` / `characters+words` / `rate` 等），由各自的 lambda 决定。

[scripts/eval_toolcall.py:99-112](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/eval_toolcall.py#L99-L112) — 第 100-102 行兼容两种入参（`call` 是 dict 还是 str）；第 102-105 行兼容 `arguments` 是字符串还是字典；第 106-112 行查表执行并兜底异常。

WebUI 版的 `execute_tool`：

[scripts/web_demo.py:124-146](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/web_demo.py#L124-L146) — 用 `if/elif` 链分发 8 个工具，每个返回 `{"result": ...}`（统一字段名，方便 [web_demo.py:395](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/web_demo.py#L395) 渲染成「ToolCalled」绿色卡片）。

#### 4.3.4 代码实践

**实践目标**：亲手调用 `execute_tool`，理解两种入参形态与 mock 返回。

**操作步骤**：

1. 在项目根目录启动 Python，导入评测脚本的 `execute_tool`（评测脚本顶层 `sys.path.append` 了项目根，可直接 import）：

   ```python
   # 示例代码
   import sys; sys.path.append('scripts')
   from eval_toolcall import execute_tool

   # 形态 A：local 后端，call 是 dict、arguments 已是 dict
   print(execute_tool({"name": "calculate_math", "arguments": {"expression": "256*37"}}))
   # 形态 B：api 后端，name 是字符串、arguments 是 JSON 字符串
   print(execute_tool("get_current_time", '{"timezone": "Asia/Shanghai"}'))
   # 形态 C：未知工具
   print(execute_tool("nonexistent", '{}'))
   ```

2. 运行并观察返回字典的字段差异。

**需要观察的现象 / 预期结果**：

- 形态 A → `{'result': '9472'}`（注意是字符串，因为 lambda 里 `str(eval(...))`）。
- 形态 B → `{'datetime': '...', 'timezone': 'Asia/Shanghai'}`（字段名是 `datetime` 不是 `result`）。
- 形态 C → `{'error': '未知工具: nonexistent'}`。

**待本地验证**：具体 `datetime` 取值取决于运行时刻。

#### 4.3.5 小练习与答案

**练习 1**：为什么评测版的 `execute_tool` 要同时支持「`call` 是 dict」和「`call` 是 str」两种入参？

**参考答案**：因为 `run_case` 里 `local` 后端传进来的是解析后的 dict（`tc`，由 `parse_tool_calls` 产出），而 `api` 后端在第 197 行 `execute_tool(tc if args.backend == 'local' else name, arguments)` 只传了工具名字符串 `name`。一份 `execute_tool` 要服务两种后端，就得在第 100-102 行做形态兼容。

**练习 2**：`calculate_math` 的 lambda 为什么先把 `^` 换成 `**`、`×` 换成 `*`？

**参考答案**：模型生成的表达式可能用人类习惯的写法（`^` 表示乘方、`×` 表示乘），而 Python 的 `eval` 不认识 `^`（它是按位异或）和 `×`。先做一轮符号归一化，才能让 `eval` 正确求值。这是把「自然语言里的数学符号」翻译成「Python 表达式」的必要适配。

---

### 4.4 多轮工具调用评测主循环：`run_case`

#### 4.4.1 概念说明

`run_case` 是 `eval_toolcall.py` 的心脏：它把「发问 → 生成 → 解析工具调用 → 执行 → 把结果塞回上下文 → 再生成」这条 Agentic 循环跑起来，直到模型不再发起工具调用为止。它的精髓在于**一套循环、两种后端**：

- `local` 后端：调本地模型的 `generate`，用正则 `parse_tool_calls` 从生成文本里抠 `<tool_call>`。
- `api` 后端：调 `chat_api` 走 OpenAI 兼容接口，工具调用由服务端（u8-l2 的 `parse_response`）解析成结构化 `tool_calls` 字段返回。

两种后端的消息拼装格式也不同：`api` 后端遵循 OpenAI 协议（`assistant` 消息要带 `tool_calls`、`tool` 消息要带 `tool_call_id`），`local` 后端则只用 `role`+`content` 的朴素结构（工具结果作为 `role: tool` 的 content，由本地 `apply_chat_template` 展开成 `<tool_response>`）。这套循环和 u7-l6 训练侧的 `rollout_single` 完全同构——训练时是采集轨迹算奖励，推理时是真正解题。

#### 4.4.2 核心流程

```
run_case(prompt, tools, ...)
  ├─ messages = [{role:user, content:prompt}]
  └─ while True:                              # 多轮工具循环
       ├─ if backend == 'local':
       │     content = generate(model, tokenizer, messages, tools, args)
       │     tool_calls = parse_tool_calls(content)      # 正则抠 <tool_call>
       │   else:  # 'api'
       │     content, tool_calls = chat_api(client, messages, tools, args, stream)
       │
       ├─ if not tool_calls: break            # 模型不再调工具 → 结束
       │
       ├─ （api 后端把 OpenAI 对象规整成 dict）
       ├─ messages.append({role:assistant, content, [tool_calls]?})  # 记下模型回答
       │
       └─ for tc in tool_calls:               # 逐个执行工具
            ├─ result = execute_tool(...)
            ├─ messages.append({role:tool, content: result, [tool_call_id]?})
            └─ （回到 while 顶部，让模型看到工具结果后继续生成）
```

循环何时退出？靠 `if not tool_calls: break`——当某轮生成里没有任何 `<tool_call>`（或 API 没返回 `tool_calls`），说明模型决定直接作答，循环结束。这和 WebUI 里 [web_demo.py:385-387](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/web_demo.py#L385-L387) 的 `if not tool_calls: break` 完全一致，只是 WebUI 多套了一层 `for _ in range(16)` 防止无限循环把页面卡死。

#### 4.4.3 源码精读

主循环骨架：

[scripts/eval_toolcall.py:177-199](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/eval_toolcall.py#L177-L199) — 第 178 行初始化 messages；第 179 行 `while True` 开循环；第 180-184 行按后端分流取 `content` 与 `tool_calls`；第 185-186 行是唯一的退出点；第 187-191 行把 `api` 后端的 OpenAI 对象规整成普通 dict（`hasattr(tc, 'id')` 用来区分对象 vs dict）；第 192 行按后端拼装 assistant 消息（`api` 要额外带 `tool_calls` 字段）；第 193-199 行逐个执行工具并 append `tool` 消息（`api` 要带 `tool_call_id`）。

local 后端的生成函数：

[scripts/eval_toolcall.py:115-130](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/eval_toolcall.py#L115-L130) — 第 117 行 `apply_chat_template(..., tools=tools, open_thinking=False)` 把工具清单和对话渲染成模型输入（注意评测时**关闭思考**，专注测工具调用）；第 121-126 行调 `model.generate`（u3-l6）；第 128-129 行统计并打印 `tokens/s`。

local 后端的工具解析：

[scripts/eval_toolcall.py:70-78](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/eval_toolcall.py#L70-L78) — `parse_tool_calls` 用 `re.findall(r'<tool_call>(.*?)</tool_call>', text, re.DOTALL)` 抠出每段调用并 `json.loads`，解析失败的静默跳过（`except: pass`）。这正是 u7-l6 `train_agent.py` 里 `parse_tool_calls` 的推理侧对应物。

WebUI 里对应的多轮循环（结构同构，但带流式刷新）：

[scripts/web_demo.py:384-413](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/web_demo.py#L384-L413) — 第 384 行 `for _ in range(16)` 限最多 16 轮工具调用；第 385 行正则抠 tool_call；第 388 行 append assistant、第 394 行 append tool 结果；第 400 行重新 `apply_chat_template` 拼出新 prompt；第 407 行再起后台线程生成；第 409-411 行边生成边 `placeholder.markdown` 刷新页面。

#### 4.4.4 代码实践

**实践目标**：用 `local` 后端真实跑一次多轮工具调用，肉眼观察 `run_case` 的循环行为。

**操作步骤**：

1. 确保已有一份 `full_sft` 权重（u5-l2 产物）或下载的 `minimind-3`。
2. 进入 scripts 目录运行（README 同款命令）：

   ```bash
   cd scripts
   python eval_toolcall.py --weight full_sft
   ```

3. 出现 `[0] 自动测试 / [1] 手动输入` 时选 `1`，输入一句需要调工具的提问，例如「现在几点了？」。
4. 观察终端输出。

**需要观察的现象 / 预期结果**（README 给出的样例，[README.md:850-857](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L850-L857)）：

```text
💬: 现在几点了？
🧠: <tool_call>{"name": "get_current_time", "arguments": {"timezone": "Asia/Shanghai"}}</tool_call>
📞 [Tool Calling]: get_current_time
✅ [Tool Called]: {"datetime": "...", "timezone": "Asia/Shanghai"}
🧠: 现在是...
```

能看到两段 `🧠`（第一段发起调用、第二段基于结果作答）和中间的 `📞`/`✅`，就说明 `while True` 循环跑了两轮生成、中间夹了一次工具执行，第二轮因没有新的 `<tool_call>` 而退出。**待本地验证**：具体时间与措辞取决于模型。

#### 4.4.5 小练习与答案

**练习 1**：`run_case` 里 `api` 后端的 `assistant` 消息为什么要带 `tool_calls` 字段，而 `local` 后端不用？

**参考答案**：OpenAI 协议要求 assistant 发起工具调用时，必须把调用信息放在结构化的 `tool_calls` 字段里，且后续 `tool` 消息要用 `tool_call_id` 回指，服务端才认。`local` 后端不走协议，工具调用就是 `<tool_call>` 原文混在 `content` 里，由本地 `apply_chat_template` 直接展开成模板片段，所以只需要 `role`+`content`。

**练习 2**：如果模型陷入「一直发起 `<tool_call>`、永不直接作答」的死循环，`run_case` 会怎样？WebUI 又会怎样？

**参考答案**：`run_case` 没有轮数上限，理论上会无限循环（实际上 `messages` 越来越长最终会触发 `truncation` 或超 `max_new_tokens` 而崩）。WebUI 的 [web_demo.py:384](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/web_demo.py#L384) 用 `for _ in range(16)` 兜底，最多 16 轮后强制结束，避免页面卡死。这是工程上对「Agentic 循环可能不收敛」的必要防护。

---

## 5. 综合实践

把本讲四个模块串成一个完整任务：**先用 WebUI 体验一次多轮工具对话，再用评测脚本量化 `agent` 权重的工具调用成功率。**

### 步骤 1：跑起 WebUI 体验多轮工具调用

1. 把 transformers 格式的模型文件夹复制到 `scripts/` 下（README 明确要求，[README.md:260](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L260)）：

   ```bash
   cp -r minimind-3 ./scripts/minimind-3
   cd scripts && streamlit run web_demo.py
   ```

2. 在侧边栏勾选 **2~3 个工具**（如「数学」「时间」），把「思考」开关打开。
3. 提一个需要多步的问题，例如「先生成一个 1 到 100 的随机数，再算它的平方」。
4. 观察：界面应出现「ToolCalling」蓝卡 → 「ToolCalled」绿卡 → 最终答案，且整个过程流式刷新。这对应 [web_demo.py:384-413](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/web_demo.py#L384-L413) 的多轮循环。

### 步骤 2：用评测脚本量化成功率

1. 如果跑过 u7-l6 的 Agentic RL，会有 `agent` 权重；否则用 `full_sft`。
2. 自动跑完 `TEST_CASES`（选 `0` 自动测试）：

   ```bash
   cd scripts
   python eval_toolcall.py --backend local --weight agent
   # 选 [0] 自动测试
   ```

3. 逐条记录每个 case 是否正确发起工具调用、最终答案是否正确。
4. 换 `--weight full_sft` 再跑一遍，对比两者成功率。

### 步骤 3（进阶）：用 `api` 后端接 WebUI 之外的服务

若已按 u8-l2 起了 `serve_openai_api.py`（或 ollama/vllm），可改用 api 后端，无需本地 GPU：

```bash
python eval_toolcall.py --backend api --api_base_url http://localhost:8000/v1
```

观察 [eval_toolcall.py:133-174](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/eval_toolcall.py#L133-L174) 的 `chat_api` 如何从流式 chunk 里累积 `delta.tool_calls`（[L154-L170](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/eval_toolcall.py#L154-L170)），并对照 [chat_api.py:37-43](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/chat_api.py#L37-L43) 里 `reasoning_content` 与 `content` 分流打印的写法。

> **关于成功率**：仓库 README 用一组 20 道数学 ToolUse 任务对比过 `full_sft`（12/20 = 60%）与 `agent`（17/20 = 85%），结论是 RL 后的 `agent` 在「可验证求解」类任务上更强，但通用问答的事实性会下降（[README.md:1394-1404](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L1394-L1404)）。注意这是基于 eval_toolcall 改出的脚本，与仓库内的 8 条 `TEST_CASES` 不是同一份，**待本地验证**你自己的数字。

## 6. 本讲小结

- WebUI（`web_demo.py`）只加载 **transformers 格式**模型，靠启动时**动态扫描 `scripts/` 目录**填充模型下拉框，并用 `@st.cache_resource` 保证模型只加载一次。
- `process_assistant_content` 用正则把 `<tool_call>` 渲染成蓝色工具卡、把 `<think>` 渲染成可折叠思考块；它要额外处理流式时「只有头标签没有尾标签」的中间状态和空思考块。
- 工具执行是**模拟的**：`web_demo.py` 用 `if/elif` 链返回 `{"result": ...}`，`eval_toolcall.py` 用 `MOCK_RESULTS` 字典 + lambda 返回结构化字典，`calculate_math` 靠 `eval` 求值以支持自动判分。
- `run_case` 是评测心脏：`while True` 循环跑「生成→解析→执行→拼回→续写」，靠「某轮没有 tool_call」退出；同一套循环用 `if backend` 分支兼容 `local`（正则抠 `<tool_call>`）和 `api`（OpenAI 结构化 `tool_calls`）两种后端。
- WebUI 的 [web_demo.py:384](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/web_demo.py#L384) 多套一层 `for _ in range(16)` 防止 Agentic 循环不收敛卡死页面，是工程上的必要兜底。
- 这套「生成→工具→续写」循环与 u7-l6 训练侧的 `rollout_single` 完全同构：训练时采集轨迹算奖励，推理时完成任务——理解了一侧就理解了另一侧。

## 7. 下一步学习建议

- **回顾训练侧**：回到 u7-l6（Agentic RL）对照阅读 `trainer/train_agent.py` 的 `rollout_single` 与 `calculate_rewards`，体会「评测时的多轮循环」与「训练时的多轮 rollout」如何共享同一套工具调用协议与 `response_mask` 设计。
- **深入服务端**：若想自己搭 OpenAI 兼容服务给 `eval_toolcall.py --backend api` 用，精读 u8-l2 的 `serve_openai_api.py`，重点看 `parse_response` 如何把 `<tool_call>` 解析成标准 `tool_calls`、`CustomStreamer` 如何做流式。
- **扩展工具集**：尝试在 `TOOLS` 和 `execute_tool`/`MOCK_RESULTS` 里新增一个真实工具（如调用一个免费天气 API），观察模型对新工具的泛化能力——README 已提示当前工具调用训练数据主要来自约 10 个模拟工具、泛化有限（[README.md:824](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L824)）。
- **格式与部署**：若 WebUI 扫不到模型，回到 u8-l1 复习 `convert_model.py` 如何把 torch `.pth` 转成 transformers 文件夹，以及向 ollama/vllm/SGLang 对接的要点。
