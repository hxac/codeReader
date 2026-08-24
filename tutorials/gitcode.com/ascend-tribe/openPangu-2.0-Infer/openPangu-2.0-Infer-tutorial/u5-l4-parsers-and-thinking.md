# 推理解析器与思考输出控制

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 openPangu-2.0 的推理输出长什么样：`<think>...</think>` 包裹思考、`<|tool_call_start|>...<|tool_call_end|>` 包裹工具调用，以及旧词表里的 `[unused16]/[unused17]/[unused11]/[unused12]` 兜底写法。
2. 读懂 `PanguReasoningParser` 如何把一段模型输出切成 `reasoning`（思考）与 `content`（正文），并理解流式（streaming）与非流式（non-streaming）两条切分路径的差异。
3. 读懂 `PanguToolParser` 如何从标记对中提取 JSON 工具调用，并在流式下用「部分 JSON 解析」逐步吐出 `tool_calls` 增量。
4. 解释 `_streaming_relay` 为什么存在：它是为绕过 vLLM v0.14.0 流式输出丢思考文本的上游 bug 而写的「跨解析器传令兵」。
5. 掌握思考输出控制的两把旋钮：`thinking_token_budget`（预算耗尽强制写 `</think>`）与 `ban_tool_call_in_thinking`（思考中禁发工具调用起始符），以及请求级关闭思考的 `think=False` 通道。

本讲属于「输出侧」：前面 u3 系列讲的是模型怎么把 token 算出来，本讲讲的是 token 变成文本之后、返回给客户端之前，服务层做的最后一道加工。

## 2. 前置知识

### 2.1 什么是推理模型的两段式输出

openPangu-2.0 是「思考型」模型：正式回答之前，它会先在一段特殊标记里自言自语。典型输出形态是：

```
<think>用户问北京天气，我需要调用天气工具……先确认城市名。</think>我来帮你查询北京天气。
<|tool_call_start|>[{"name": "get_weather", "arguments": {"city": "Beijing"}}]<|tool_call_end|>
```

- `<think>` / `</think>`：思考段的开始与结束（旧词表用 `[unused16]` / `[unused17]`）。
- `<|tool_call_start|>` / `<|tool_call_end|>`：工具调用 JSON 的包裹标记（旧词表用 `[unused11]` / `[unused12]`）。

客户端（聊天前端、Agent 框架）通常只想把 `content` 展示给用户、把 `reasoning` 折叠成「思考过程」、把 `tool_calls` 交给工具执行器。所以服务端必须把一整段原始文本**切分**成这三个字段——这就是解析器（parser）的工作。

### 2.2 OpenAI 兼容协议里的流式增量

回顾 u1-l5：客户端向 proxy 的 7000 端口发 OpenAI 兼容请求。流式模式下，服务端不是一次性返回全文，而是每个（或每几个）token 发一个增量块（delta），客户端拼接还原全文。vLLM 中增量的载体是 `DeltaMessage`，它同时可以携带：

- `content`：正文文本增量；
- `reasoning`（旧别名 `reasoning_content`）：思考文本增量；
- `tool_calls`：工具调用增量（函数名一次性发出，参数字符串分片累加）。

**切分难点在流式**：非流式拿到全文再切，一次搞定；流式每个 delta 只有一小段文本，解析器必须维护跨步状态（「思考是否已结束」「当前解析到第几个工具」「参数已发出多少字符」），这就是为什么源码里流式方法远比非流式方法长。

### 2.3 三个实现层面的机制

本讲会碰到三个通用工程机制，先给直觉：

1. **monkey patch（运行时补丁）**：不改 vLLM 源码，在运行时把它的类/函数替换成自己的版本。u2-l4 已详细讲过 PatchManager，本讲只引用它的一个补丁 `patch_thinking_limit.py`。
2. **contextvars.ContextVar**：Python 的「协程局部变量」。同一个名字，在不同的 asyncio 任务里各自持有一份互不干扰的值——比线程局部变量更适合 vLLM 这种 asyncio 服务器。
3. **logits 掩码（mask）与强制（force）**：在采样前修改每个词表位置的打分。把某 token 的 logit 置 \(-\infty\) 等于「禁止生成」；置一个巨大正值（如 \(10^9\)）等于「强制生成」。这是 thinking 控制的物理层手段。

### 2.4 部署侧如何开启这套东西

505B 的 ansible 模板里有现成接线（92B 模板未开启）：

```yaml
EXTRA_ARGS="${EXTRA_ARGS} --reasoning-parser pangu --enable-auto-tool-choice --tool-call-parser pangu"
REASONING_CONFIG='{"reasoning_start_str":"<think>","reasoning_end_str":"</think>"}'
EXTRA_ARGS="${EXTRA_ARGS} --reasoning-config ${REASONING_CONFIG}"
```

见 [tools/ansible/505B/omni_infer_server_template_performance2P1D_505B_int8_open.yml:L90-L93](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance2P1D_505B_int8_open.yml#L90-L93)（prefill 侧；decode 侧同样写法在 [L201-L204](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance2P1D_505B_int8_open.yml#L201-L204)）。`--reasoning-parser pangu` 与 `--tool-call-parser pangu` 里的名字 `pangu`，正是本讲要讲的两个解析器注册进 vLLM 工厂时用的键。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲角色 |
| --- | --- | --- |
| [components/omni-npu/src/omni_npu/v1/parsers/__init__.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/parsers/__init__.py) | 以名字 `pangu` 把两个解析器懒注册进 vLLM 工厂 | 入口 |
| [components/omni-npu/src/omni_npu/v1/parsers/pangu_reasoning_parser.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/parsers/pangu_reasoning_parser.py) | `<think>` 切分（流式 + 非流式） | 模块一 |
| [components/omni-npu/src/omni_npu/v1/parsers/pangu_tool_parser.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/parsers/pangu_tool_parser.py) | 工具调用标记解析（流式 + 非流式） | 模块二 |
| [components/omni-npu/src/omni_npu/v1/parsers/_streaming_relay.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/parsers/_streaming_relay.py) | 跨解析器传递思考文本的中继（bug 规避层） | 模块三 |
| [components/omni-npu/src/omni_npu/v1/config/reasoning.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/config/reasoning.py) | `ReasoningConfig`：思考起止串、预算、禁发开关 | 模块四 |
| [components/omni-npu/src/omni_npu/v1/sample/thinking_ban_state.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/sample/thinking_ban_state.py) | 三态状态机：思考中禁工具符、思考后禁重复 `</think>` | 模块四 |
| [components/omni-npu/src/omni_npu/v1/sample/thinking_budget_state.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni_npu/src/omni_npu/v1/sample/thinking_budget_state.py) | 预算耗尽时强制生成 `</think>` | 模块四 |
| [components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_thinking_limit.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_thinking_limit.py) | 把上述机制以补丁形式接进 vLLM 全链路 | 模块四 |
| [components/omni-npu/tests/unit/parsers/](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/parsers/test_pangu_reasoning_parser.py) | 解析器单测（mock tokenizer，无需 NPU） | 实践靶场 |

## 4. 核心概念与源码讲解

### 4.1 PanguReasoningParser：`<think>` 输出的切分（流式解析·上）

#### 4.1.1 概念说明

vLLM 自带一套 reasoning parser 框架（DeepSeek-R1 风格：按 `<think>`/`</think>` 切分），为什么 Pangu 还要自己写一个？因为 Pangu 有四个「方言差异」：

1. **双标记体系**：新词表用 `<think>`，旧词表用 `[unused16]`，解析器必须在初始化时探测词表里有哪个。
2. **默认开启思考**：Pangu 默认就是思考模型，只有用户**显式**传 `think=False`/`thinking=False` 时才把输出当纯正文。
3. **隐式结束**：模型可能不写 `</think>` 就直接发 `<|tool_call_start|>`，此时工具起始符要被当作思考结束的信号——但这个行为默认关闭，需环境变量 `PANGU_TOOL_CALL_ENDS_THINKING=1` 显式打开。
4. **多 token 边界块**：在 MTP 投机解码（u3-l5）下，一个 delta 可能同时含 `<think>` 和后续正文（如 `"<think>你好"`），父类处理不了这种「起始符和首批文本挤在同一块」的情况。

#### 4.1.2 核心流程

**非流式 `extract_reasoning(model_output) -> (reasoning, content)`** 是一棵判定树：

```text
输入全文
 ├─ 开头有 <think>？ → 剥掉（partition 取后半）
 ├─ thinking_enabled == False？ → 全文都算 content，reasoning=None
 ├─ 文中有 </think>？ → 按 </think> partition：前段=reasoning，后段=content（空则 None）
 ├─ （开关打开时）文中有 <|tool_call_start|>？ → 前段=reasoning，从标记起=content（标记保留！）
 └─ 都没有 → 全文=reasoning，content=None
```

注意隐式结束分支的注释：工具起始符**必须保留在 content 开头**，因为下游 `PanguToolParser` 靠搜索这个字面标记定位工具调用 payload——两个解析器之间有一个「约定接口」。

**流式 `extract_reasoning_streaming(...)`** 每个 delta 被调用一次，判定顺序是：

1. 思考被关闭 → `DeltaMessage(content=delta)`，直接返回；
2. 上一步已隐式结束（起始符在 previous_token_ids 里且 `</think>` 不在）→ 全部当 content；
3. 本 delta 刚出现工具起始符（且无 `</think>`）→ 在 delta 内按标记位置一分为二：前半 reasoning、后半（含标记）content；
4. 否则走父类逻辑（思考中归 reasoning，`</think>` 所在块一分为二），随后补救「`<think>` 与正文同块」的多 token 情形；
5. 每条返回路径都经 `stash_reasoning_from(ret)` 把 reasoning 文本暂存给中继（见 4.3）。

#### 4.1.3 源码精读

先看构造函数——三个关键状态都在这里建立：

> [components/omni-npu/src/omni_npu/v1/parsers/pangu_reasoning_parser.py:L41-L66](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/parsers/pangu_reasoning_parser.py#L41-L66)：构造函数缓存工具起始符的 token id（是否解析取决于环境变量属性是否返回 None），并从 `chat_template_kwargs` 读取 `think`/`thinking`，只要任一为假即置 `thinking_enabled=False`。

标记探测是「词表优先级」模式：

> [components/omni-npu/src/omni_npu/v1/parsers/pangu_reasoning_parser.py:L68-L76](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/parsers/pangu_reasoning_parser.py#L68-L76)：`start_token` / `end_token` 属性优先返回 `<think>` / `</think>`，词表没有时回退 `[unused16]` / `[unused17]`。

> [components/omni-npu/src/omni_npu/v1/parsers/pangu_reasoning_parser.py:L78-L89](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/parsers/pangu_reasoning_parser.py#L78-L89)：`tool_call_start_token` 属性——环境变量 `PANGU_TOOL_CALL_ENDS_THINKING` 不等于 `1` 时恒返回 `None`（隐式结束整体关闭）；打开时依次尝试 `<|tool_call_start|>`、`[unused11]`。

非流式主方法：

> [components/omni-npu/src/omni_npu/v1/parsers/pangu_reasoning_parser.py:L192-L230](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/parsers/pangu_reasoning_parser.py#L192-L230)：`extract_reasoning`。L200-L205 先剥掉起始符；L209-L211 思考关闭分支；L213-L216 显式 `</think>` 优先；L221-L228 隐式工具起始符结束（marker 保留在 content 头部）。

流式主方法的关键三段：

> [components/omni-npu/src/omni_npu/v1/parsers/pangu_reasoning_parser.py:L123-L141](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/parsers/pangu_reasoning_parser.py#L123-L141)：思考关闭、以及「隐式结束已发生在之前的 delta」两个早退分支，都直接产 content。

> [components/omni-npu/src/omni_npu/v1/parsers/pangu_reasoning_parser.py:L146-L168](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/parsers/pangu_reasoning_parser.py#L146-L168)：隐式结束恰好发生在本 delta——在 delta 文本内 `find` 标记位置切两半，若起始符也挤在本块则再剥一次，最后返回 `DeltaMessage(reasoning=前半, content=后半含标记)`。

> [components/omni-npu/src/omni_npu/v1/parsers/pangu_reasoning_parser.py:L170-L190](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/parsers/pangu_reasoning_parser.py#L170-L190)：调父类 `super().extract_reasoning_streaming(...)` 后的补救——若本 delta 同时含 `<think>` 和正文（多 token 块），把起始符之前的部分丢掉、只留正文作 reasoning；L189 无论走哪条路都调用 `stash_reasoning_from(ret)` 喂中继。

流式还有个配套方法 `is_reasoning_end`，vLLM serving 层每步调用它判断「边界事件是否发生在这一步」：

> [components/omni-npu/src/omni_npu/v1/parsers/pangu_reasoning_parser.py:L91-L109](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/parsers/pangu_reasoning_parser.py#L91-L109)：结束符（或工具起始符）首次出现在 delta 时计数加一并仅在计数为 1 时返回 True（边界事件只上报一次），另设 `input_ids[-1] == 结束符` 的兜底分支。

#### 4.1.4 代码实践

**实践目标**：跑通 reasoning parser 的既有单测，直观感受判定树的每个分支。

**操作步骤**：

1. 进入 omni-npu 组件目录并安装测试依赖（需在带 vllm 依赖的环境，如 omniinfer 容器内；见 [components/omni-npu/tests/QUICKSTART.md:L7-L19](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/QUICKSTART.md#L7-L19)）：

   ```bash
   cd components/omni-npu
   pip install -e ".[test]"
   pytest tests/unit/parsers/test_pangu_reasoning_parser.py -v
   ```

2. 精读 [tests/unit/parsers/test_pangu_reasoning_parser.py:L30-L39](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/parsers/test_pangu_reasoning_parser.py#L30-L39)：测试用 `MagicMock` 伪造 tokenizer，词表只有 4 个特殊 token——**解析器单测不需要真实模型与 NPU**。
3. 观察 `test_extract_reasoning_tool_call_start_ends_reasoning`（L96-L112）如何用 `@patch.dict(os.environ, {"PANGU_TOOL_CALL_ENDS_THINKING": "1"})` 打开隐式结束开关。

**需要观察的现象**：全部用例绿；其中「只有思考无正文」「正文为空」两类用例断言 `content is None`，验证判定树的第 4、3 分支。

**预期结果**：约 8 个用例通过，无 NPU 依赖。若本机没有 vllm 依赖则 `import` 失败，此实践需在容器内执行——**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：非流式输入 `"<think>思路A</think>答案B"` 与输入 `"思路A</think>答案B"`（少了起始符），`extract_reasoning` 的返回分别是什么？

**答案**：两者都返回 `("思路A", "答案B")`。前者走「剥起始符 + 按 `</think>` partition」；后者起始符不存在，`partition` 取 `[0]` 段即原文，再按 `</think>` 切分，结果相同——起始符只影响「剥不剥」，不影响 `</think>` 切分。

**练习 2**：为什么隐式结束分支要把 `<|tool_call_start|>` 保留在 content 开头，而不是像 `</think>` 那样直接丢掉？

**答案**：`</think>` 是纯分隔符，丢弃无害；而 `<|tool_call_start|>` 是下游 `PanguToolParser` 定位工具 payload 的搜索锚点（它在 `current_text` 里 `find`/`split` 这个字面标记）。若在这里丢掉，工具解析器将找不到标记，整个工具调用会被当普通文本返回。这体现了两个解析器间「marker 即接口」的约定。

**练习 3**：`PANGU_TOOL_CALL_ENDS_THINKING` 默认（未设置）时，模型输出 `"<think>需要查天气<|tool_call_start|>[...]"` 会被切成什么？

**答案**：属性 `tool_call_start_token` 返回 None → `tool_call_start_token_id` 为 None → 隐式结束分支全部失效；文中又没有 `</think>`，于是走兜底分支：整段（剥掉 `<think>` 后的 `"需要查天气<|tool_call_start|>[...]"`）全算 reasoning，content 为 None。这正是该环境变量存在的原因——默认保守，显式开启才启用新语义。

### 4.2 PanguToolParser：工具调用协议解析（工具调用协议）

#### 4.2.1 概念说明

工具调用（function calling）是 Agent 的基础协议：模型不直接回答，而是输出「请帮我调用某函数、参数是什么」，服务端把它翻译成 OpenAI 协议的 `tool_calls` 字段交给客户端执行。Pangu 的线上的格式是：

```
<|tool_call_start|>[{"name": "get_weather", "arguments": {"city": "Beijing"}},
                     {"name": "get_time",    "arguments": {}}]<|tool_call_end|>
```

要点：

- payload 是 **JSON 数组**，数组里每个对象是一个调用（`name` + `arguments`，兼容旧字段名 `parameters`）；一次可以并行调用多个工具。
- 请求侧需带 `--enable-auto-tool-choice --tool-call-parser pangu`（见 2.4 模板），vLLM 才会在流式分支里调用这个解析器。
- 流式的难点：参数 JSON 是逐 token 长出来的，`{"ci` → `{"city` → `{"city": "Bei`……解析器必须能对**残缺 JSON** 做增量解析（用 `partial_json_parser` 库），并且只把「新增的那部分参数字符串」作为 delta 发给客户端。

#### 4.2.2 核心流程

**非流式 `extract_tool_calls`**：

```text
全文中两个标记都不在 → tools_called=False，全文当 content
正则 <|tool_call_start|>(.*?)<|tool_call_end|> 全局抓取（DOTALL 跨行）
每个捕获段 json.loads（是数组则展开多个对象）
组装 ToolCall(type="function", name=..., arguments=json.dumps(...))
content = 第一个起始符之前的文本
任一步异常 → 降级：tools_called=False，全文当 content（绝不因格式错误 5xx）
```

**流式 `_extract_tool_calls_streaming`** 是一个以三个实例变量为状态机的循环：

- `current_tool_id`：正在流式输出的工具下标（从 -1 起）；
- `current_tool_name_sent`：当前工具的函数名是否已发出（OpenAI 协议要求名字一次性发全）；
- `streamed_args_for_tool[i]`：第 i 个工具的参数字符串**已发送**的前缀长度基准。

每个 delta 的处理顺序：

1. 三个早退：结束符单独成块 → None；结束符已在历史且不在本块（工具段已收尾）→ None；全文尚无起始符 → 纯 content；起始符刚出现在本块 → 只发它前面的文本；
2. 截取「最后一个起始符之后、结束符之前」的窗口，剥掉外层 `[`/`]`，对窗口反复 `partial_json_loads` 逐个解析出完整或残缺的 JSON 对象数组；
3. 数组比游标长 → 进入新工具：先补发上一工具漏发的参数 diff，游标 +1，函数名标记复位；
4. 函数名未发且已可解析 → 发 `DeltaToolCall(name=..., id=...)`（若参数恰好完整则连同参数一起发，避免空参调用）；
5. 否则处于参数流式段：把当前完整参数 JSON 重新序列化，与已发送前缀做差，只发 diff。

#### 4.2.3 源码精读

构造函数建立标记与正则：

> [components/omni-npu/src/omni_npu/v1/parsers/pangu_tool_parser.py:L48-L74](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/parsers/pangu_tool_parser.py#L48-L74)：按词表选择 `<|tool_call_start|>`/`[unused11]` 与 `<|tool_call_end|>`/`[unused12]`，组装 `re.escape(起始)(.*? )re.escape(结束)` 的 DOTALL 正则；任一标记 id 查不到直接 `RuntimeError`——工具解析器对词表的要求是**强约束**（对比 reasoning parser 的宽容回退）。

非流式提取与降级：

> [components/omni-npu/src/omni_npu/v1/parsers/pangu_tool_parser.py:L85-L88](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/parsers/pangu_tool_parser.py#L85-L88)：无标记 → 纯文本响应。

> [components/omni-npu/src/omni_npu/v1/parsers/pangu_tool_parser.py:L93-L111](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/parsers/pangu_tool_parser.py#L93-L111)：正则抓取、`json.loads`、数组展开，字段名兼容 `arguments`/`parameters`，参数重新 `json.dumps` 成字符串（OpenAI 协议里 arguments 是字符串而非对象）。

> [components/omni-npu/src/omni_npu/v1/parsers/pangu_tool_parser.py:L122-L127](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/parsers/pangu_tool_parser.py#L122-L127)：任何异常都降级为纯文本——工具调用格式错不该让整个请求失败。

流式状态机的三段核心：

> [components/omni-npu/src/omni_npu/v1/parsers/pangu_tool_parser.py:L165-L187](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/parsers/pangu_tool_parser.py#L165-L187)：四个早退分支（结束符单块、工具段已收尾、无起始符、起始符前置文本）。

> [components/omni-npu/src/omni_npu/v1/parsers/pangu_tool_parser.py:L193-L231](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/parsers/pangu_tool_parser.py#L193-L231)：窗口截取与部分 JSON 解析循环。注意 L193-L194 的 `Allow.ALL & ~Allow.STR` 掩码：函数名未发出前不允许把残缺字符串当完整值，保证名字永远整发；`MalformedJSON` 时返回 None 等下一批 token。

> [components/omni-npu/src/omni_npu/v1/parsers/pangu_tool_parser.py:L239-L301](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/parsers/pangu_tool_parser.py#L239-L301)：新工具分支（补发上一工具参数 diff、游标推进、复位名字标记）与函数名发送分支（`make_tool_call_id()` 生成 id；参数恰好已完整时随名字同发，避免空参）。

> [components/omni-npu/src/omni_npu/v1/parsers/pangu_tool_parser.py:L303-L339](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/parsers/pangu_tool_parser.py#L303-L339)：参数 diff 分支——当前参数 JSON 序列化后，要么取完整尾部（`is_complete`），要么与上一轮 `prev_tool_call_arr` 序列化结果求最长公共前缀再裁掉已发送长度。

#### 4.2.4 代码实践

**实践目标**：用既有单测驱动流式状态机，逐 delta 观察三个状态变量的变化。

**操作步骤**：

1. 运行工具解析器单测：

   ```bash
   cd components/omni-npu
   pytest tests/unit/parsers/test_pangu_tool_parser.py -v
   ```

2. 精读 [tests/unit/parsers/test_pangu_tool_parser.py:L117-L129](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/parsers/test_pangu_tool_parser.py#L117-L129)：该用例手工把状态机拨到「名字已发、正在流参数」（`current_tool_id=0`、`current_tool_name_sent=True`），然后喂一个只含参数增量的 delta——这就是流式解析器的调试套路：**状态自备、delta 喂食**。
3. 自己再构造一个两工具数组的流式序列（示例代码，加到该测试文件末尾即可）：

   ```python
   def test_two_tools_streaming(self):
       curr = ('<|tool_call_start|>'
               '[{"name": "f1", "arguments": {"a": 1}},'
               '{"name": "f2", "arguments": {"b": 2}}]')
       self.parser.current_tool_id = 0
       self.parser.current_tool_name_sent = True
       self.parser.streamed_args_for_tool = ['{"a": 1}']
       self.parser.prev_tool_call_arr = [{"name": "f1", "arguments": {"a": 1}}]
       res = self.parser.extract_tool_calls_streaming(
           "", curr, '{"b": 2}}]', [], [], [1], self.request)
       # 数组长度 2 > current_tool_id+1 → 切换到新工具 f2：
       # 先对 f1 补发参数差分。f1 已发 7 字符、完整序列化也是 7 字符，
       # 差分为空串——但仍会构造 DeltaMessage 返回（非 None）
       self.assertEqual(res.tool_calls[0].function.arguments, "")
       # 游标推进到 f2，名字标记复位 → 下一个 delta 将发出 f2 的名字
       self.assertEqual(self.parser.current_tool_id, 1)
       self.assertFalse(self.parser.current_tool_name_sent)
   ```

**需要观察的现象**：第 3 步返回的 DeltaMessage 携带 f1 的**空参数差分**（`index=0`、`arguments=""`），同时状态机推进：`current_tool_id` 从 0 变 1、`current_tool_name_sent` 复位为 False；再喂下一个 delta 就会发出 f2 的名字。

**预期结果**：断言通过，直观验证「数组变长 → 切新工具分支 + 状态推进」的状态机行为。此实践依赖 vllm 环境，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：payload 写成单个对象（不是数组）`<|tool_call_start|>{"name": ...}<|tool_call_end|>`，非流式会怎样？

**答案**：降级为纯文本（`tools_called=False`，原样返回）。`json.loads` 得到 dict 后 L99 的 `raw_function_calls.extend(function_call)` 会把 dict **逐键展开**成字符串（如 `"name"`），随后构造 `FunctionCall(name=..., ...)` 取不到合法字段抛异常，落入 L122-L127 的兜底。这正是既有用例 `test_extract_tool_calls_with_single_object_tool_call` 覆盖的场景——协议必须是数组。

**练习 2**：流式解析中 `streamed_args_for_tool[i]` 记的是字符串还是长度？为什么参数 diff 要用「重新序列化后裁前缀」而不是直接发 delta_text？

**答案**：它累积的是**已发送的参数 JSON 字符串**，diff 时用 `cur_args_json[sent:]` 按长度裁剪。不能直接发 delta_text 的原因：模型输出的 token 文本与规范化 JSON 之间可能有空白、转义差异（`partial_json_loads` 解析出的对象重新 `json.dumps` 后才是协议规定的形态），按「当前完整序列化 − 已发送前缀」求差能保证客户端拼出的参数字符串始终是合法 JSON。

**练习 3**：为什么 `current_tool_name_sent` 未置位时解析掩码要 `Allow.ALL & ~Allow.STR`？

**答案**：`partial_json_parser` 的 `Allow.STR` 控制是否接受「字符串值残缺」。函数名在残缺时可能被解析成部分字符串（如 `get_wea`），OpenAI 协议要求函数名一次完整发出；屏蔽 `Allow.STR` 后，名字不完整时解析直接抛 `MalformedJSON`，本步返回 None 等待，名字完整后才进入发送分支。

### 4.3 _streaming_relay：两个解析器之间的传令兵（流式解析·下）

#### 4.3.1 概念说明

这是本讲最有工程味的一块：一段约 90 行（不含注释）的代码，专门用来绕过 **vLLM v0.14.0 的一个流式 bug**。

问题场景：vLLM 的流式 serving 循环里，当某个 delta 使「思考结束」标志翻转（即 `</think>` 所在的那一块，称为**边界块**），它会先调 reasoning parser、再调 tool parser。对 MTP-K（K≥2）投机解码，`</think>` 和后续文本经常挤在同一 delta 里，reasoning parser 会返回 `DeltaMessage(reasoning="边界前的思考", content="边界后的正文")`；但 vLLM v0.14.0 在同一轮迭代里用**普通赋值**把 tool parser 的返回值覆盖上去——边界块里的思考文本被无声丢弃，客户端永远看不到最后一段思考。

K=1 时 `</think>` 通常单独成块，父类返回 None，覆盖打在空值上无副作用——所以这个 bug 只在多 token 投机解码下暴露，非常隐蔽。

修复思路（上游 vLLM PR #42691 用 snapshot+restore，omni-npu 等不到发版就本地实现）：reasoning parser 每次流式返回后，把 `.reasoning` **暂存**到一个中继里；tool parser 每次流式返回后，检查中继有无存货，有就**贴回**到自己的 DeltaMessage 上（哪怕自己本来返回 None，也要造一个只装 reasoning 的 DeltaMessage）。

#### 4.3.2 核心流程

```text
每个流式迭代（同一 asyncio 任务内、两次同步调用之间无 await）：
  PanguReasoningParser.extract_reasoning_streaming(...)
      └─ 返回前: stash_reasoning_from(ret)   # 先清空中继，ret.reasoning 非空则存入
  vLLM serving 层（可能覆盖 delta_message —— bug 所在）
  PanguToolParser.extract_tool_calls_streaming(...)
      └─ 返回前: reattach_reasoning_to(ret)  # 中继有货则贴回（None→新建），随后清空中继
```

并发隔离靠 `contextvars.ContextVar`：vLLM 每个请求的流式生成器跑在独立 asyncio 任务里，ContextVar 天然按任务隔离；同一迭代内两次调用之间没有 `await`，不存在被别的请求插队改写的窗口。

#### 4.3.3 源码精读

模块头部注释就是最好的文档，值得整段读：

> [components/omni-npu/src/omni_npu/v1/parsers/_streaming_relay.py:L11-L31](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/parsers/_streaming_relay.py#L11-L31)：完整复现 bug 成因——serving 层第 1067 行的普通赋值如何覆盖携带 reasoning 的 DeltaMessage，以及为何 MTP-K≥2 才容易触发。

> [components/omni-npu/src/omni_npu/v1/parsers/_streaming_relay.py:L33-L42](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/parsers/_streaming_relay.py#L33-L42)：上游修复是 PR #42691；本模块是其本地化镜像，并留了 TODO——vLLM 升级到含该 PR 的版本后可整体删除（约 570 行、4 个文件）。

存与取的两个函数：

> [components/omni-npu/src/omni_npu/v1/parsers/_streaming_relay.py:L71-L73](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/parsers/_streaming_relay.py#L71-L73)：中继本体——一个默认 None 的 ContextVar。

> [components/omni-npu/src/omni_npu/v1/parsers/_streaming_relay.py:L93-L105](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/parsers/_streaming_relay.py#L93-L105)：`stash_reasoning_from` ——**先无条件清空**再按需写入，保证非边界迭代不会把上一轮的存货错留给本轮（防止思考文本重复发送）。

> [components/omni-npu/src/omni_npu/v1/parsers/_streaming_relay.py:L108-L147](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/parsers/_streaming_relay.py#L108-L147)：`reattach_reasoning_to` ——取货即清空；tool parser 返回 None 时新建 `DeltaMessage(reasoning=...)`；已带 reasoning 则不动。L118-L132 的注释解释了一个 Pydantic 陷阱：`DeltaMessage` 的构造期校验器会把 `reasoning` 镜像到弃用字段 `reasoning_content`，但**属性赋值不会重新触发校验器**，所以给已存在的对象贴回时必须同时手工写两个字段，否则还在读旧字段的客户端恰好在被抢救的那一块看到 null。

两侧的接线点：

> [components/omni-npu/src/omni_npu/v1/parsers/pangu_reasoning_parser.py:L120-L122](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/parsers/pangu_reasoning_parser.py#L120-L122) 与 [components/omni-npu/src/omni_npu/v1/parsers/pangu_tool_parser.py:L139-L152](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/parsers/pangu_tool_parser.py#L139-L152)：reasoning 侧每条返回路径都调 `stash_reasoning_from`；tool 侧把真正的实现改名为 `_extract_tool_calls_streaming`，公开方法变成包着 `reattach_reasoning_to` 的薄壳。

测试隔离专用钩子：

> [components/omni-npu/src/omni_npu/v1/parsers/_streaming_relay.py:L76-L90](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/parsers/_streaming_relay.py#L76-L90)：`reset_for_tests`。生产不需要它（每请求一个 asyncio 任务，天然隔离），但 `unittest.TestCase` 全部同步跑在同一个 Context 里——上一个测试存下的思考会被下一个工具解析器测试**继承**。配套的 autouse fixture 在 [tests/unit/parsers/conftest.py:L25-L30](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/parsers/conftest.py#L25-L30)，每个测试前后各清一次。

#### 4.3.4 代码实践

**实践目标**：亲手复现「stash → 覆盖 → reattach 抢救」全过程，理解并发隔离。

**操作步骤**：

1. 跑中继自己的单测：

   ```bash
   cd components/omni-npu
   pytest tests/unit/parsers/test_streaming_relay.py -v
   ```

2. 写一个最小复现脚本（示例代码，`relay_demo.py`，放任意可 import omni_npu 的位置，不要提交进仓库源码目录）：

   ```python
   from vllm.entrypoints.openai.protocol import DeltaMessage
   from omni_npu.v1.parsers._streaming_relay import (
       stash_reasoning_from, reattach_reasoning_to)

   # 第 1 步：reasoning parser 在边界块的返回
   ret = DeltaMessage(reasoning="最后一段思考", content="<|tool_call_start|>[")
   stash_reasoning_from(ret)

   # 第 2 步：模拟 vLLM bug —— tool parser 的返回直接覆盖了上面的 ret
   overwritten = None   # tool parser 本步没有增量，返回 None

   # 第 3 步：tool parser 出口处的抢救
   rescued = reattach_reasoning_to(overwritten)
   print(rescued.reasoning, '|', rescued.reasoning_content)
   ```

3. 再验证「重复发送防线」：紧接着再调一次 `reattach_reasoning_to(DeltaMessage(content="x"))`，观察结果。

**需要观察的现象**：第 3 步打印 `最后一段思考 | 最后一段思考`（两个字段都被填上）；第 4 步返回的对象 `reasoning` 为 None——中继取货即清空，同一段思考不会被发两次。

**预期结果**：输出与上述一致。依赖 vllm 环境，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么中继用 `ContextVar` 而不是模块级全局变量或线程局部变量（threading.local）？

**答案**：vLLM 的流式接口是 asyncio 协程，一个线程里交错跑着成百上千个请求的生成器。全局变量会被并发请求互相覆盖；threading.local 在单线程 asyncio 下所有请求共享同一份，同样串数据。`ContextVar` 按「执行上下文」隔离，vLLM 每个请求跑在独立任务（独立 Context）中，天然一人一份；且同一迭代内 stash 与 reattach 之间没有 `await`，不会被同任务外的代码读到。

**练习 2**：`stash_reasoning_from` 为什么必须「先清空、再按需写入」，而不是只在有 reasoning 时写入？

**答案**：若只在有货时写，非边界的普通迭代（reasoning 为空）会留下**上一轮**的存货；下一轮 tool parser 调用时把它贴到毫不相关的增量上，同一段思考被发给客户端两次。先清空保证中继的语义严格是「最近一次 reasoning parser 调用所产生的思考增量」。

**练习 3**：如果未来 vLLM 升级到包含 PR #42691 的版本，这个模块可以删吗？删的时候要动哪几处？

**答案**：模块注释 L39-L42 给出了条件：vLLM 升级到含 PR #42691 的版本**并且** `PanguReasoningParser` 的父类链经过 `DelegatingParser.parse_delta`。要动的四处：删除 `_streaming_relay.py` 本体；reasoning parser 中所有 `stash_reasoning_from` 调用；tool parser 的公开包装方法（把 `_extract_tool_calls_streaming` 改回原名）；测试目录的 `conftest.py` autouse fixture。这也是读 TODO 注释学习「技术债如何管理」的好例子。

### 4.4 ReasoningConfig 与 thinking 预算/禁用控制（思考预算控制）

#### 4.4.1 概念说明

前三个模块都在**事后切分**已生成的文本；本模块反过来，在**生成过程中**干预模型，让思考「按需收场」。两个正交的旋钮都挂在 `ReasoningConfig` 上：

1. **`thinking_token_budget`（思考预算）**：给每个请求的思考段设 token 上限。预算耗尽而模型仍没写 `</think>` 时，采样前把 `</think>` 首token 的 logit 强制抬到 \(10^9\)——模型「不得不」结束思考。这防止模型陷入无限深思，是控制尾延迟（思考失控直接拉长响应时间）的手段。
2. **`ban_tool_call_in_thinking`（思考中禁发工具符）**：一个三态状态机。请求在 `<think>` 内部时，把 `<|tool_call_start|>` 的 logit 置 \(-\infty\)（想调工具？先闭合思考）；`</think>` 已出现后，再把 `</think>` 本身禁掉（防止输出被多段思考打碎）。它治理的是「模型在思考里突然开工具调用」导致 4.1 的解析器切分混乱的失败模式。

另有一个**请求级开关**：4.1 已见 `chat_template_kwargs` 里 `think=False` 可让解析器把输出全当正文；而预算/禁用是引擎级（logits 层）控制，两者一个管「生成」，一个管「展示」，层次不同。

配置数据流：

```text
服务启动:  --reasoning-config '{"reasoning_start_str":"<think>","reasoning_end_str":"</think>", ...}'
             │  (patch_thinking_limit 的 EngineArgsPatch 注册该 CLI 参数)
             ▼
        ReasoningConfig.initialize_token_ids()   # 用 tokenizer 把串变成 token id
             ▼
        GPUModelRunner.__init__ → InputBatch 挂上两个 holder（工厂函数按开关决定建不建）
每个请求:  ChatCompletionRequest 可带 thinking_token_budget → 塞进 SamplingParams.extra_args
每步采样:  Sampler.apply_logits_processors
             ├─ holder.update_state(...)      # 从已提交 token 历史推导状态/剩余预算
             └─ holder.apply_to_logits(...)   # ban: -inf 掩码；budget: +1e9 强制
```

#### 4.4.2 核心流程

**预算状态机**（`ThinkingBudgetStateHolder`）：只跟踪带预算的请求（`sync_batch` 时从 `params.extra_args` 取 `thinking_token_budget`，取不到不入表）。每步从「已提交输出历史」（投机解码下要剥掉尾部未验收的 draft 后缀）数出当前思考段长度；一旦

\[
\text{已思考 token 数} \geq \text{thinking\_token\_budget}
\]

就在对应 logits 行上把结束符 token 的打分设为 \(10^9\)，等价于强制采样出 `</think>`。

**禁发状态机**（`ThinkingBanStateHolder`）：每个请求按「历史中 `<think>` 与 `</think>` 最后出现位置」三选一：

| 条件（last_start = `<think>` 最后位置，last_end = `</think>` 最后位置） | 状态 | 动作 |
| --- | --- | --- |
| 两者都是 -1（都没出现） | PRE_THINK | 不干预 |
| last_start > last_end | IN_THINK | 禁 `<|tool_call_start|>` |
| last_end > last_start | POST_THINK | 禁 `</think>` 的最后一个 token |

状态迁移是**基于最后一次出现位置**而非严格事件序列——输出里就算混入多个 `<think>`/`</think>`，以最后谁在后为准，鲁棒且实现简单。

投机解码兼容：MTP-K 下一个请求有 K 个 draft 行 + 1 个 bonus 行，`apply_to_logits` 逐行推进状态（第 r 行采样时假定历史 + drafts[0..r-1] 已生成），保证「draft 中间夹着 `</think>`」时禁令在正确的行生效。

#### 4.4.3 源码精读

配置类本体：

> [components/omni-npu/src/omni_npu/v1/config/reasoning.py:L22-L40](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/config/reasoning.py#L22-L40)：全部配置项——解析器名、起止串（含弃用别名 think_start_str）、`thinking_token_budget`、`ban_tool_call_in_thinking`。这是 out-of-tree 配置：vLLM 原生没有这些字段。

> [components/omni-npu/src/omni_npu/v1/config/reasoning.py:L84-L125](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/config/reasoning.py#L84-L125)：`initialize_token_ids` ——用 tokenizer 把起止串 encode 成 token id（幂等，重复调用直接置 enabled 返回）；L118-L123 在禁发开关打开时顺带解析 `<|tool_call_start|>`/`[unused11]` 的 id，查不到就静默留 None（后续工厂见状降级）。

补丁如何把配置注入 vLLM：

> [components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_thinking_limit.py:L141-L187](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_thinking_limit.py#L141-L187)：`EngineArgsPatch` 给 `vllm serve` 增加 `--reasoning-config` 参数（L158-L162），并在 `create_engine_config` 末尾调用 `initialize_token_ids`、把配置挂到 `vllm_config.reasoning_config`（L184-L186）。配合 [L114-L120](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_thinking_limit.py#L114-L120) 把 `ReasoningConfig` 类本身暴露进 `vllm.config` 命名空间。

> [components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_thinking_limit.py:L200-L244](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_thinking_limit.py#L200-L244)：请求级预算的两条入口——`SamplingParams.from_optional` 与 `ChatCompletionRequest.to_sampling_params` 被改写，把请求里的 `thinking_token_budget` 写进 `extra_args` 字典随身携带。

> [components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_thinking_limit.py:L275-L300](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_thinking_limit.py#L275-L300)：`InputProcessor` 补丁——请求没带预算时用引擎级默认值兜底（L275-L287）；请求带了预算但引擎没配 `--reasoning-config` 则直接报错（L290-L300），避免「以为限了其实没限」的静默失效。

状态机挂载与采样应用：

> [components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_thinking_limit.py:L303-L325](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_thinking_limit.py#L303-L325)：`_attach_reasoning_state_holder` 在 ModelRunner 初始化（及 InputBatch 重建）时调用两个工厂，把 holder 挂到 `input_batch` 上。

> [components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_thinking_limit.py:L502-L523](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_thinking_limit.py#L502-L523)：`Sampler.apply_logits_processors` 补丁在常规惩罚处理之后依次调 `holder.update_state`（刷新状态）与 `holder.apply_to_logits`（施加掩码/强制）。拒绝采样路径（MTP 验证）在 [L590-L602](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_thinking_limit.py#L590-L602) 有对应调用。

禁发状态机本体：

> [components/omni-npu/src/omni_npu/v1/sample/thinking_ban_state.py:L43-L78](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/sample/thinking_ban_state.py#L43-L78)：工厂函数的三道门——配置缺失、开关关闭、token id 未解析，任一命中返回 None（禁用特性全程静默无操作）。

> [components/omni-npu/src/omni_npu/v1/sample/thinking_ban_state.py:L37-L40](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/sample/thinking_ban_state.py#L37-L40) 与 [L178-L184](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/sample/thinking_ban_state.py#L178-L184)：三态常量与 `_derive_state`——「最后出现位置」比较法的全部实现就这几行。

> [components/omni-npu/src/omni_npu/v1/sample/thinking_ban_state.py:L271-L337](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/sample/thinking_ban_state.py#L271-L337)：`update_state` ——先剥掉投机解码尾部 draft 后缀恢复「真实已提交历史」，再增量扫描新 token 更新两个最后位置（多 token 标记时对窗口尾部做有界重扫）。

> [components/omni-npu/src/omni_npu/v1/sample/thinking_ban_state.py:L411-L433](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/sample/thinking_ban_state.py#L411-L433)：`apply_to_logits` 的施法处——`IN_THINK` 行把 `logits[row, tool_call_start_tid]` 置 \(-\infty\)，`POST_THINK` 行把 `</think>` 末 token 置 \(-\infty\)；MTP-K 下按 draft 前缀逐行推进状态。

预算状态机本体（对照着看）：

> [components/omni-npu/src/omni_npu/v1/sample/thinking_budget_state.py:L118-L134](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/sample/thinking_budget_state.py#L118-L134)：`sync_batch` 只为**带预算**的请求建状态条目（对比 ban 的「全员跟踪」）。

> [components/omni-npu/src/omni_npu/v1/sample/thinking_budget_state.py:L522-L551](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni_npu/src/omni_npu/v1/sample/thinking_budget_state.py#L522-L551)：`_apply_forcing_to_logits` 尾部——把命中行的 `force_token_ids` 位置打上 \(10^9\)，softmax 后概率近乎 1，等效强制采样 `</think>`。

#### 4.4.4 代码实践

**实践目标**：不依赖 NPU，用单测观察禁发/预算两个状态机对 logits 的实际改动。

**操作步骤**：

1. 运行预算状态机单测：

   ```bash
   cd components/omni-npu
   pytest tests/unit/sample/test_thinking_budget_state.py -v
   ```

2. 打开 [components/omni-npu/tests/unit/sample/test_thinking_budget_state.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/sample/test_thinking_budget_state.py)，找到一个调用 `apply_to_logits` 的用例，记下它构造的 logits 形状与断言（哪些位置被改成 \(-\infty\) 或 \(10^9\)）。
3. 写一个 ban 状态机的最小驱动（示例代码，仿照该测试文件风格）：

   ```python
   import torch
   from omni_npu.v1.config import ReasoningConfig
   from omni_npu.v1.sample.thinking_ban_state import (
       maybe_create_thinking_ban_state_holder)

   cfg = ReasoningConfig(
       reasoning_start_str="", reasoning_end_str="",
       ban_tool_call_in_thinking=True)
   # 直接注入 token id，绕过 tokenizer（单测常用手法）
   cfg._reasoning_start_token_ids = [100]   # <think>
   cfg._reasoning_end_token_ids = [101]     # </think>
   cfg._tool_call_start_token_id = 104      # <|tool_call_start|>
   cfg._enabled = True

   holder = maybe_create_thinking_ban_state_holder(
       cfg, max_num_seqs=8, num_spec_tokens=0,
       device=torch.device("cpu"), is_pin_memory=False)
   ```

   随后参考源码 L207-L245 的 `sync_batch`（用 `BatchUpdate.added` 注入 prompt token）与 L247 起 `update_state`，先喂含 `<think>`（id=100）的输出历史，再对全零 logits（形状 `[1, vocab]`）调 `apply_to_logits`，打印 `logits[0, 104]`。

**需要观察的现象**：历史只含 `<think>` 时，`logits[0, 104]` 变成 `-inf`（工具符被禁）；把 `</think>`（id=101）追加进历史再跑一遍，`logits[0, 104]` 恢复 0、`logits[0, 101]` 变成 `-inf`（改为禁重复思考结束符）。

**预期结果**：两次输出与上述一致，状态从 IN_THINK 迁移到 POST_THINK。`BatchUpdate` 的构造方式请对照测试文件里的现成用法；若直接构造报错，改为手工填 `holder._state` 字典亦可验证 `apply_to_logits`。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`ban_tool_call_in_thinking=True` 但词表里既没有 `<|tool_call_start|>` 也没有 `[unused11]`，会发生什么？

**答案**：`initialize_token_ids` 的 L118-L123 查不到 id，`tool_call_start_token_id` 保持 None；工厂函数（thinking_ban_state.py L63-L70）据此打一条 warning 并返回 None，整个禁发特性静默变成无操作。设计取向：**配置缺料的降级优先于启动失败**——不用的部署不该被一个无关开关卡住。

**练习 2**：预算控制为什么把 logit 设成 \(10^9\) 而不是像禁发那样设 \(-\infty\)？两者的 softmax 效果有何不同？

**答案**：设 \(10^9\) 是「让该 token 的 softmax 概率趋于 1」，用于**必须选中**的目标（强制写 `</think>`）；设 \(-\infty\) 是「概率严格为 0」，用于**绝不许出现**的目标（禁工具符/禁重复结束符）。方向相反、目的一致：都是绕过模型自由采样，直接决定下一个 token。

**练习 3**：MTP-K 投机解码下，`update_state` 为什么要先从输出历史里剥掉尾部 `spec_token_ids` 长度的后缀？

**答案**：draft token 是「投机产物」，要等拒绝采样验收后才算真正提交（见 u3-l5）。`output_token_ids` 在 combine 之后可能已经混入未验收的 draft 尾巴；不剥掉就会把「可能被拒掉的 token」也计入思考长度/状态推导，导致预算提前触发或状态误判。剥后缀恢复的是「确定已提交」的历史，这也是两个 holder 共用的口径（ban 的 L289-L292 有同一处理与注释）。

## 5. 综合实践

**任务**：构造三段典型模型输出样例，写一个 pytest 文件同时验证两个解析器在**非流式与流式**下的切分结果，并顺带验证 `PANGU_TOOL_CALL_ENDS_THINKING` 开关与中继行为。这是本讲所有模块的串联。

**三段样例**：

1. 纯思考：`<think>仅思考没有正文</think>`
2. 思考 + 回答：`<think>先分析</think>这是回答`
3. 思考 + 工具调用（无显式 `</think>`）：`<think>需要查天气<|tool_call_start|>[{"name": "get_weather", "arguments": {"city": "Beijing"}}]<|tool_call_end|>`

**操作步骤**：

1. 在 `components/omni-npu/tests/unit/parsers/` 下新建 `test_pangu_tri_samples.py`（示例代码）：

   ```python
   import unittest
   from unittest.mock import MagicMock, patch

   from omni_npu.v1.parsers import PanguReasoningParser, PanguToolParser
   from vllm.entrypoints.openai.protocol import ChatCompletionRequest


   def make_tokenizer():
       tok = MagicMock()
       tok.get_vocab.return_value = {
           "<think>": 100, "</think>": 101,
           "<|tool_call_start|>": 104, "<|tool_call_end|>": 105,
       }
       tok.tokenizer = tok
       return tok


   def reasoning_of(delta):
       """兼容 reasoning / reasoning_content 两种字段名的读取器"""
       return getattr(delta, "reasoning", None) or getattr(
           delta, "reasoning_content", None)


   class TestTriSamples(unittest.TestCase):
       def setUp(self):
           self.request = MagicMock(spec=ChatCompletionRequest)

       # ---------- 非流式 ----------

       def test_s1_pure_reasoning(self):
           parser = PanguReasoningParser(make_tokenizer())
           r, c = parser.extract_reasoning(
               "<think>仅思考没有正文</think>", self.request)
           self.assertEqual(r, "仅思考没有正文")
           self.assertIsNone(c)

       def test_s2_reasoning_then_answer(self):
           parser = PanguReasoningParser(make_tokenizer())
           r, c = parser.extract_reasoning(
               "<think>先分析</think>这是回答", self.request)
           self.assertEqual((r, c), ("先分析", "这是回答"))

       def test_s3_tool_call_nonstreaming_default_off(self):
           # 默认开关关闭：无 </think> → 全文算 reasoning
           parser = PanguReasoningParser(make_tokenizer())
           out = ('<think>需要查天气<|tool_call_start|>'
                  '[{"name": "get_weather", "arguments": {"city": "Beijing"}}]'
                  '<|tool_call_end|>')
           r, c = parser.extract_reasoning(out, self.request)
           self.assertIn("需要查天气", r)
           self.assertIsNone(c)

       @patch.dict("os.environ", {"PANGU_TOOL_CALL_ENDS_THINKING": "1"})
       def test_s3_tool_call_implicit_end(self):
           # 开关打开：工具起始符隐式结束思考，marker 保留在 content 头部
           parser = PanguReasoningParser(make_tokenizer())
           out = ('<think>需要查天气<|tool_call_start|>'
                  '[{"name": "get_weather", "arguments": {"city": "Beijing"}}]'
                  '<|tool_call_end|>')
           r, c = parser.extract_reasoning(out, self.request)
           self.assertEqual(r, "需要查天气")
           self.assertTrue(c.startswith("<|tool_call_start|>"))
           # 下游工具解析器接着从 content 里提取工具调用
           tool_parser = PanguToolParser(make_tokenizer())
           res = tool_parser.extract_tool_calls(c, self.request)
           self.assertTrue(res.tools_called)
           self.assertEqual(res.tool_calls[0].function.name, "get_weather")

       # ---------- 流式 ----------

       def test_s2_streaming(self):
           parser = PanguReasoningParser(make_tokenizer())
           # delta 序列：<think> / 先 / 分析 / </think> / 这是回答
           seq = [
               ("<think>", [100]),
               ("先", [1]),
               ("分析", [1]),
               ("</think>", [101]),
               ("这是回答", [1]),
           ]
           prev_text, prev_ids = "", []
           reasoning_buf, content_buf = [], []
           for text, ids in seq:
               cur_text, cur_ids = prev_text + text, prev_ids + ids
               delta = parser.extract_reasoning_streaming(
                   prev_text, cur_text, text, prev_ids, cur_ids, ids)
               if delta is not None:
                   if reasoning_of(delta):
                       reasoning_buf.append(reasoning_of(delta))
                   if delta.content:
                       content_buf.append(delta.content)
               prev_text, prev_ids = cur_text, cur_ids
           self.assertEqual("".join(reasoning_buf), "先分析")
           self.assertIn("这是回答", "".join(content_buf))

       def test_s3_streaming_relay(self):
           # 边界块：思考尾巴与 </think> 挤在同一 delta → 思考文本必须
           # 出现在 delta 本身或经中继抢救的对象上，不能两边都丢
           from omni_npu.v1.parsers._streaming_relay import (
               reattach_reasoning_to)
           rparser = PanguReasoningParser(make_tokenizer())
           delta = rparser.extract_reasoning_streaming(
               "", "最后思考</think>", "最后思考</think>",
               [], [1, 101], [1, 101])
           rescued = reattach_reasoning_to(None)  # tool parser 本步无增量
           got_direct = reasoning_of(delta) if delta is not None else None
           got_relay = reasoning_of(rescued) if rescued is not None else None
           self.assertTrue(got_direct or got_relay)
   ```

   > 注意：本文件放在 `tests/unit/parsers/` 下会自动获得 [conftest.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/parsers/conftest.py#L25-L30) 的中继隔离 fixture，这正是 4.3 讲的测试坑位防护。

2. 运行：

   ```bash
   cd components/omni-npu
   pytest tests/unit/parsers/test_pangu_tri_samples.py -v
   ```

3. 对照源码核对每条断言的出处：样例 1/2 对应 4.1 判定树；样例 3 的两态行为对应 `tool_call_start_token` 属性（L78-L89）；流式样例验证父类路径与 `stash_reasoning_from`；中继样例验证 stash→reattach 链路。

**需要观察的现象**：6 条用例全绿；把 `test_s3_tool_call_implicit_end` 上的装饰器去掉重跑会失败（`r` 不再是 `"需要查天气"`），直观看到环境变量开关的语义。

**预期结果**：全部通过。断言写得偏宽松（如 `assertIn`）是为了兼容 vLLM 版本间 `reasoning`/`reasoning_content` 字段的差异；若某条失败，先打印 delta 实际内容再收紧断言。本实践需在带 vllm 依赖的环境中执行——**待本地验证**。

## 6. 本讲小结

- openPangu-2.0 的输出是「思考段 + 正文 + 工具调用」三合一，服务端用 `PanguReasoningParser`（`<think>`/`[unused16]` 切分）与 `PanguToolParser`（`<|tool_call_start|>`/`[unused11]` 标记对 + JSON 数组）两件工具在返回前完成拆包，两者都以 `pangu` 为名懒注册进 vLLM 工厂。
- 非流式切分是「全文判定树」，一次到位；流式切分是跨 delta 的状态机（思考是否已结束 / 第几个工具 / 参数已发多少），难点全在增量与残缺 JSON（`partial_json_loads` + 前缀差分）。
- `PANGU_TOOL_CALL_ENDS_THINKING=1` 打开「工具起始符隐式结束思考」语义，且标记必须保留在 content 头部给工具解析器当锚点——这是两个解析器之间的接口约定。
- `_streaming_relay` 用 ContextVar 中继抢救 vLLM v0.14.0 流式 bug 丢掉的边界块思考文本，只在 MTP-K≥2 多 token 边界块下暴露；上游 PR #42691 合入后可整体删除。
- thinking 控制走 logits 层：`thinking_token_budget` 超限时把 `</think>` 的 logit 抬到 \(10^9\) 强制收场；`ban_tool_call_in_thinking` 用三态状态机（最后出现位置法）在思考中禁工具符、思考后禁重复结束符（\(-\infty\)）。
- 整套机制由 `patch_thinking_limit.py` 以运行时补丁接入 vLLM（CLI 参数、请求参数透传、InputBatch 挂 holder、Sampler/RejectionSampler 施加），配置入口是 `--reasoning-config` JSON，解析器入口是 `--reasoning-parser pangu --tool-call-parser pangu`。

## 7. 下一步学习建议

- **补齐部署视角**：对照 [tools/ansible/505B/omni_infer_server_template_performance2P1D_505B_int8_open.yml](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance2P1D_505B_int8_open.yml#L90-L93) 实际拉起一个带 reasoning-parser 的服务，用 `stream=true` 发请求观察 `reasoning` 增量块，把本讲的解析结果在真实链路上验证一遍。
- **深入测试体系**：本讲大量实践都落在 `tests/unit/`，下一单元的 u10-l2（omni-npu 测试体系与本地跑测）会系统讲 pytest 组织、`run_tests.sh` 与容器内跑测。
- **回看补丁机制**：如果 `patch_thinking_limit.py` 的多个 `@register_patch` 让你眼花，回到 u2-l4（PatchManager）复习「注册名、目标、符号、动机」四要素分析法。
- **衔接投机解码**：中继只在 MTP-K≥2 下暴露、状态机要剥 draft 后缀，这些都以 u3-l5（MTP 投机解码与采样器）的验收模型为前提，建议交叉重读。
- **源码延伸阅读**：`thinking_budget_state.py` 的 `_update_think_state`（预算→force_index 的完整推导）本讲只讲了主干，值得一读；vLLM 侧 `vllm/entrypoints/openai/serving_chat.py` 的流式分支（解析器被调用的真实现场）也建议对照镜像内的 vLLM 源码看一遍。
