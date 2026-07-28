# 工具调用机制

## 1. 本讲目标

学完本讲，你应当能够：

- 用 `make_function_tool<...>` 借助 C++ 模板自动推断参数类型，生成一份 OpenAI 风格的「函数工具」JSON 描述。
- 说清 `define_tools` 如何把工具描述拼进 system prompt、并 `prefill` 进推理上下文（KV cache）。
- 解释 `generate` 自回归循环里，如何只凭 `tool_call_id` / `tool_call_end_id` 两个特殊 token 把模型输出「分流」到工具调用通道。
- 描述 `GenerateConfig.tool_callback` 回调的触发时机、回调返回值如何被 `<tool_response>` 包裹后回填进上下文，让模型「看到」工具结果并继续作答。
- 串联从「注册工具 → 模型决定调用 → 执行回调 → 结果回填」的完整闭环。

## 2. 前置知识

本讲是 [u2-l4 generate 自回归解码主循环](u2-l4-generate-loop.md) 的延伸，也是 [u7-l1 对话模板 ChatML / YouTu 与 thinking](u7-l1-chat-templates.md) 的姊妹篇。阅读前请确认你已了解：

- **自回归解码**：模型每步吐一个 token，`generate` 在一个 `for` 循环里不断预测下一个 token，直到遇到 `eos` 或步数耗尽。本讲的工具调用正是「嵌入」在这个循环里的一个分流分支。
- **特殊 token（special token）**：像 `<|im_start|>`、`<|im_end|>`、`<eos>` 这类 token 在分词器里是「原子」，不会被 BPE 切碎，每个都对应一个固定整数 id（参见 u3-l1）。工具调用依赖两个新的特殊 token：`<tool_call>` 与 `</tool_call>`。
- **推理上下文 ctx**：保存 KV cache、`cur_token`、`position_id` 的「会话状态」对象；`prefill` 把一段文本写进 ctx，`generate` 在 ctx 上续写（参见 u2-l5）。
- **ChatML 模板**：用 `<|im_start|>role\n...<|im_end|>` 包裹每条消息；工具描述与工具返回都借道 system / user 角色塞进这个模板（参见 u7-l1）。
- **nlohmann::json**：项目使用的单头文件 JSON 库，本讲大量出现 `json::parse`、`json::dump`、`json::object()` 等操作。

什么是「工具调用（tool calling / function calling）」？一句话：**让模型在回答时，可以选择「调用一个外部函数」而不是直接说话**。例如用户问「2 + 3 等于几」，模型不自己算（容易算错），而是输出一段结构化的「我要调用 add 函数，参数 a=2, b=3」；宿主程序接到后真正执行 `add(2,3)` 得到 5，再把 5 喂回给模型，模型最后说出「等于 5」。要实现这套流程，需要解决三件事：**怎么告诉模型有哪些工具可用**、**怎么识别模型正在发起调用**、**怎么执行并把结果送回去**——分别对应本讲的 `define_tools`、token 解析、`tool_callback`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/ncnn_llm_gpt.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h) | 声明 `GenerateConfig`（含 `tool_callback`）、`tool_call_id`/`tool_call_end_id` 成员、`define_tools`、以及模板函数 `make_function_tool` / `json_type_name`。 |
| [src/ncnn_llm_gpt.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp) | `define_tools` 的实现、构造函数里读取 `functions` 配置解析两个特殊 token id、`generate` 里的工具调用状态机与 `handle_tool` 回填逻辑。 |
| [examples/llm_ncnn_run/cli_runner.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/cli_runner.cpp) | 端到端示例：先 `define_tools` 注入工具，再在 `generate` 前挂上 `tool_callback`，回调里查路由表执行真实函数。 |
| [examples/llm_ncnn_run/tools.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/tools.cpp) | 内置工具的样板：用 `make_function_tool` 造 `random`/`add` 两个工具，并配一张「名字 → 可调用对象」的路由表。 |
| [src/utils/prompt.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/prompt.cpp) | ChatML 模板里「工具描述段」「`<tool_call>` 段」「`<tool_response>` 段」的字符串拼装（u7-l1 已详述，本讲只取相关片段）。 |
| [tests/test_llm.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/tests/test_llm.cpp) | `test_model_tool_calling` 给出最小可运行的工具调用端到端用例。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，按「造工具 → 注入工具 → 识别调用 → 执行回填」的数据流顺序展开。

### 4.1 用 make_function_tool 生成工具描述 JSON

#### 4.1.1 概念说明

要让模型「知道」有哪些函数可调，业界通行做法（OpenAI Function Calling、Qwen 等都沿用）是给模型看一段 **JSON Schema 风格的函数签名**，大致长这样：

```json
{
  "type": "function",
  "function": {
    "name": "add",
    "description": "Add two integers.",
    "parameters": {
      "type": "object",
      "properties": {
        "a": { "type": "integer", "description": "" },
        "b": { "type": "integer", "description": "" }
      },
      "required": ["a", "b"]
    }
  }
}
```

手写这种 JSON 既繁琐又容易把参数类型写错（比如把 `int` 写成 `"number"`）。`ncnn_llm_gpt` 提供了一个模板函数 `make_function_tool<Ret, Args...>`，**用 C++ 的类型实参直接推断 JSON Schema 的类型字符串**，让你写一行就能拿到正确的工具描述。

#### 4.1.2 核心流程

- **类型 → JSON 类型名**：`json_type_name<T>()` 是一个 `constexpr` 函数模板，用 `if constexpr` 把 C++ 类型映射到 JSON Schema 的类型字符串：
  - `int` / `long` / `long long` → `"integer"`
  - `bool` → `"boolean"`
  - 浮点（`float`/`double`）→ `"number"`
  - 其它（含 `std::string`）→ `"string"`
- **展开参数包**：`make_function_tool<Ret, Args...>` 接收一个 `std::array<std::string, sizeof...(Args)>` 作为参数名列表，用 C++17 折叠表达式（fold expression）逐个把 `arg_names[idx]` 配上 `json_type_name<Args>()` 写进 `properties`。
- **拼装外壳**：套上 `type`/`function`/`parameters`/`required` 这层 OpenAI 约定的结构返回。

注意返回类型 `Ret` 当前只参与模板签名、并不进入生成的 JSON（工具描述里没有「返回值类型」字段），这是与 OpenAI 规范一致的——函数的返回值由宿主程序决定，模型不需要知道。

#### 4.1.3 源码精读

类型推断的核心——[src/ncnn_llm_gpt.h:157-163](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L157-L163) 用一串 `if constexpr` 把 C++ 类型翻译成 JSON 类型名：

```cpp
template<typename T>
static constexpr const char* json_type_name() {
    if constexpr (std::is_same_v<T, int> || std::is_same_v<T, long> || std::is_same_v<T, long long>) return "integer";
    else if constexpr (std::is_same_v<T, bool>) return "boolean";
    else if constexpr (std::is_floating_point_v<T>) return "number";
    else return "string";
}
```

工具构造函数本体——[src/ncnn_llm_gpt.h:165-188](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L165-L188)。关键是那一行折叠表达式：

```cpp
template<typename Ret, typename... Args>
static nlohmann::json make_function_tool(const std::string& name, const std::string& description,
                                         const std::array<std::string, sizeof...(Args)>& arg_names) {
    assert(arg_names.size() == sizeof...(Args));
    nlohmann::json properties = nlohmann::json::object();
    size_t idx = 0;
    ((
        properties[arg_names[idx]] = nlohmann::json{{"type", json_type_name<Args>()}, {"description", ""}},
        ++idx
    ), ...);
    // ...套上 type/function/parameters/required 外壳返回
}
```

`(( ... , ++idx), ...)` 是 C++17 的折叠表达式：对参数包 `Args...` 里的每个类型，按顺序执行「在 `properties` 里写入一个键、再让 `idx` 自增」。`Args` 的推导顺序与 `arg_names` 的下标一一对应，所以 `make_function_tool<int,int,int>("add", ..., {"a","b"})` 会产出 `a`、`b` 两个 `"integer"` 参数（`Ret=int` 不计入参数包，故 3 个 `int` 实参对应 2 个参数）。

#### 4.1.4 代码实践

**实践目标**：验证 `make_function_tool` 的类型推断与 JSON 结构。

**操作步骤**（源码阅读 + 可选编译）：

1. 打开 [tests/test_llm.cpp:68-84](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/tests/test_llm.cpp#L68-L84) 的 `test_tool_definition`，它调用 `make_function_tool<int,int,int>("random", ..., {"floor","ceiling"})` 并断言结构。
2. 在该测试末尾临时加一行打印（**示例代码**，非项目原有）：
   ```cpp
   std::cout << tool.dump(2) << std::endl;
   ```
3. 执行 `xmake build test_llm && xmake run test_llm`（参见 u1-l2 与 u8-l3）。

**需要观察的现象**：打印出的 JSON 里，`floor`、`ceiling` 两个参数的 `type` 都是 `"integer"`；外层有 `type=="function"`、`function.name=="random"`、`function.parameters.required` 含两个名字。

**预期结果**：与 4.1.1 里给出的 `add` 例子结构完全一致，只是名字换成 `random`/`floor`/`ceiling`。若把模板实参改成 `make_function_tool<int,double,std::string>`，对应参数类型应分别变成 `"integer"`、`"number"`、`"string"`。

#### 4.1.5 小练习与答案

**练习 1**：`make_function_tool<int,int,int>("add", ..., {"a","b"})` 有 3 个 `int` 模板实参，却只生成 2 个参数，为什么？
**答案**：第一个模板实参是返回类型 `Ret`，不进入 `Args...` 参数包；后两个 `int` 才是参数类型，分别绑定到 `a`、`b`。因此 `sizeof...(Args)==2`，`arg_names` 也必须是 2 元素数组。

**练习 2**：若想描述一个参数为「是否启用 verbose」的布尔开关，模板实参应该怎么写？
**答案**：用 `bool`，例如 `make_function_tool<void,bool>("set_verbose", ..., {"verbose"})`，`json_type_name<bool>()` 会产出 `"boolean"`。

---

### 4.2 define_tools：把工具写入推理上下文

#### 4.2.1 概念说明

光有一个 JSON 描述还不够——模型是个「只读 KV cache」的状态机，**任何信息都必须先经过 `prefill` 写进它的上下文**，它才能在后续 `generate` 时「看见」这些信息。`define_tools` 就是负责「把工具描述塞进上下文」的那一步：它把工具 JSON 拼成一段 ChatML 格式的 system 文本，再调用 `prefill` 把这段文本「读」进 KV cache。

#### 4.2.2 核心流程

1. **闸门检查**：若 `tool_call_id < 0 || tool_call_end_id < 0`，直接原样返回 ctx——说明这个模型根本没配置工具调用能力（见 4.3），注入工具描述也毫无意义，等于 no-op。
2. **暂存工具**：`this->tools = tools;` 把工具列表存到成员里（供外部可能的内省用途）。
3. **拼装工具 prompt**：调用 `apply_chat_template({{"system", system_prompt}}, tools, false, false)`，由 ChatML 模板把工具 JSON 包进 `<tools>...</tools>` 段（详见 4.2.3 与 u7-l1）。
4. **prefill 进上下文**：若传入的 ctx 非空，就在它上面续接（多轮场景）；否则从空开始首发 `prefill`。两种情况都返回更新后的新 ctx。

#### 4.2.3 源码精读

`define_tools` 的实现非常短，[src/ncnn_llm_gpt.cpp:987-995](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L987-L995)：

```cpp
std::shared_ptr<ncnn_llm_gpt_ctx> ncnn_llm_gpt::define_tools(
    const std::shared_ptr<ncnn_llm_gpt_ctx>& ctx,
    const std::vector<nlohmann::json>& tools,
    const std::string& system_prompt) {
    if (tool_call_id < 0 || tool_call_end_id < 0) return ctx;   // 闸门
    this->tools = tools;
    std::string tool_prompt = apply_chat_template({{"system", system_prompt}}, tools, false, false);
    if (ctx) return prefill(tool_prompt, ctx);
    return prefill(tool_prompt);
}
```

「工具描述长什么样」由 ChatML 模板决定，见 [src/utils/prompt.cpp:30-44](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/prompt.cpp#L30-L44)。当 `tools` 非空时，模板会在 system 消息里追加一段固定措辞：

```
<|im_start|>system
<system_prompt>

# Tools
... <tools>
{每个工具的 JSON}
</tools>
For each function call, return a json object ... within <tool_call></tool_call> XML tags:
<tool_call>
{"name": <function-name>, "arguments": <args-json-object>}
</tool_call><|im_end|>
```

这段文本不仅告诉模型「有哪些工具」，还**规定了模型发起调用时必须使用的格式**——即用 `<tool_call>` ... `</tool_call>` 包裹一段 `{"name":..., "arguments":...}` 的 JSON。这个格式约定是 4.3 里 token 解析能成立的前提：模型被训练成在决定调用工具时，先吐 `<tool_call>` 这个特殊 token。

`tool_call_id` / `tool_call_end_id` 这两个整数 id 来自构造函数，[src/ncnn_llm_gpt.cpp:148-160](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L148-L160) 从 `model.json` 的 `setting.functions` 段读取字符串、再用分词器查表得到：

```cpp
if (config["setting"].contains("functions")) {
    auto func_cfg = config["setting"]["functions"];
    if (func_cfg["type"].get<std::string>() == "tool_call") {
        if (func_cfg.contains("tool_call_id"))
            tool_call_id = bpe->token_to_id().at(func_cfg["tool_call_id"].get<std::string>());
        if (func_cfg.contains("tool_call_end_id"))
            tool_call_end_id = bpe->token_to_id().at(func_cfg["tool_call_end_id"].get<std::string>());
    }
}
```

也就是说，模型目录的 `model.json` 里需要有这样一段（**示例配置**，键名以源码为准）：

```json
"setting": {
  "functions": {
    "type": "tool_call",
    "tool_call_id": "<tool_call>",
    "tool_call_end_id": "</tool_call>"
  }
}
```

两个字符串（如 `<tool_call>`、`</tool_call>`）必须是分词器里已注册的特殊 token，`.at()` 在找不到时会抛异常、被构造函数的 `catch` 统一转成 `load model failed`（与 u1-l5、u3-l1 的「必需令牌用 `.at()` 尽早失败」设计一致）。默认值 `-1` 则意味着「未配置」，于是 `define_tools` 的闸门会把它降级为 no-op。

#### 4.2.4 代码实践

**实践目标**：理解「无 functions 配置时 define_tools 是 no-op」。

**操作步骤**（源码阅读型）：

1. 在 [src/ncnn_llm_gpt.cpp:988](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L988) 的闸门行处，确认 `tool_call_id`、`tool_call_end_id` 的默认初值（见头文件 [src/ncnn_llm_gpt.h:105-106](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L105-L106)，均为 `-1`）。
2. 跟踪：若一个模型的 `model.json` 没有 `setting.functions` 段，构造完后这两个成员保持 `-1`。
3. 推理：此时调用 `define_tools(ctx, tools, ...)` 会发生什么？

**需要观察的现象 / 预期结果**：函数在第 988 行直接 `return ctx;`，**既不修改 ctx，也不 prefill 任何工具描述**。这意味着对一个未训练/未配置工具调用的模型，即使你传入了工具列表，模型也「看不见」它们——这是刻意的安全降级，避免往上下文里塞入模型无法理解的文本。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `define_tools` 要在 `ctx` 非空和空两种情况下分别调用不同的 `prefill` 重载？
**答案**：多轮对话里，system prompt 已经 prefill 过、ctx 已存在，工具描述应**续接**在已有 KV cache 之后（用 `prefill(text, ctx)` 重载，它会克隆 ctx 再续写）；首轮则还没有 ctx，用无 ctx 的 `prefill(text)` 首发。两者都返回「包含工具描述」的新 ctx。

**练习 2**：`this->tools = tools;` 把工具存到成员里，但翻遍 `generate` 并没有用到 `this->tools`。这个赋值目前的作用是什么？
**答案**：当前实现里，工具描述已通过 `prefill` 烤进 KV cache，`generate` 阶段不需要再读 `this->tools`；该成员主要供外部内省（查询「这个对象注册过哪些工具」）或未来扩展使用。它**不参与**推理主链路，工具的「可见性」完全由 prefill 进上下文的文本决定。

---

### 4.3 generate 中的 tool_call_id / tool_call_end_id 解析

#### 4.3.1 概念说明

工具描述进上下文后，剩下的核心问题就是：**`generate` 怎么知道模型「正在发起一次工具调用」？** 答案出奇地简单——全靠两个特殊 token：

- 当模型吐出 `tool_call_id`（即 `<tool_call>`）→ 表示「接下来我要写一段调用 JSON」。
- 在这之后、直到吐出 `tool_call_end_id`（即 `</tool_call>`）之前 → 所有 token 都是 JSON 的一部分，**不应输出给用户**，而是偷偷累积起来。
- 遇到 `tool_call_end_id` → 这段 JSON 收集完毕，交给回调执行，再把结果回填进上下文。

本质上这是一个嵌在自回归循环里的**两态状态机**：普通文本态 ↔ 工具调用态。

#### 4.3.2 核心流程

`generate` 主循环每步先处理「上一个 token 是什么角色」，再算下一个 token。工具调用的分流逻辑可画成：

```
                 ┌─ cur_token == eos ──────────────► break 退出
                 │
   上一个 token ─┼─ cur_token == tool_call_id ────► 进入工具调用态(flag=true)
                 │
                 ├─ cur_token == tool_call_end_id ─► 退出工具调用态(flag=false)
                 │                                   → handle_tool(累积的JSON, ctx)
                 │                                   → 清空累积、清空 history、continue
                 │
                 ├─ flag==true(在工具调用态) ──────► 把 decode 后的文本追加到累积串
                 │
                 └─ 否则(普通文本态) ──────────────► callback(token) 输出给用户
```

之后才是「embed → decoder → lm_head → 采样」算下一个 token 的常规流程（u2-l4 已详述）。关键在于：**工具调用的检测发生在「消费上一个 token」阶段，完全基于整数 id 比较，不需要任何字符串匹配**，所以又快又稳。

#### 4.3.3 源码精读

状态变量在循环前初始化，[src/ncnn_llm_gpt.cpp:871-872](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L871-L872)：

```cpp
bool flag_in_tool_call = false;
std::string tool_call_content;
```

四路分流在循环体开头，[src/ncnn_llm_gpt.cpp:874-890](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L874-L890)：

```cpp
for (int step = 0; step < cfg.max_new_tokens; ++step) {
    if (ctx->cur_token == eos) break;

    if (ctx->cur_token == tool_call_id) {
        flag_in_tool_call = true;                       // 进入工具调用态
    } else if (ctx->cur_token == tool_call_end_id) {
        flag_in_tool_call = false;                      // 退出工具调用态
        handle_tool(tool_call_content, ctx);            // 执行+回填（见 4.4）
        tool_call_content.clear();
        history.clear();                                // 重复惩罚重置
        history.insert(ctx->cur_token);
        continue;                                       // 跳过本步的 decoder 计算
    } else if (flag_in_tool_call) {
        tool_call_content += bpe->decode({ctx->cur_token}, false);  // 偷偷累积 JSON
    } else {
        callback(bpe->decode({ctx->cur_token}, false));            // 普通文本输出给用户
    }
    // ……下面才是 embed/decoder/lm_head/采样算 next token……
}
```

几个关键细节：

- **偷偷累积**：工具调用态下，token 经 `bpe->decode` 还原成文本片段拼进 `tool_call_content`，但**不调用 `callback`**，所以用户屏幕上不会闪现原始 JSON。
- **`continue` 跳过 decoder**：遇到 `tool_call_end_id` 时，`handle_tool` 内部已经做了一次 `prefill`（回填工具结果，见 4.4），`ctx` 的 `cur_token` 已被 prefill 更新为「工具结果之后的下一个 token」；因此本步**不应再跑一次 decoder**，直接 `continue` 进入下一轮循环去消费那个新 token。
- **`history.clear()`**：重复惩罚（repetition penalty，见 u3-l4）基于 `history` 这个「已出现 token 集合」。工具调用回填后语境已切换，把历史清空、只保留当前 token，避免上一段文本的惩罚干扰后续生成。
- **`tool_call_id` 与 `tool_call_end_id` 为 `-1` 时**：由于真实 token id 都 ≥ 0，`cur_token` 永远不会等于 `-1`，这两个分支自动失效——与 `define_tools` 的闸门呼应，**未配置工具的模型会完全忽略这两个分支**，行为退化为普通文本生成。

#### 4.3.4 代码实践

**实践目标**：用一个微小的「纸面执行」验证状态机。

**操作步骤**（源码跟踪型，无需模型）：

1. 假设某模型 `tool_call_id=151665`（`<tool_call>`）、`tool_call_end_id=151666`（`</tool_call>`）、`eos=151643`。
2. 设想 `generate` 依次产出的 token id 序列为：
   ```
   151665, 9126, ..., 13, 151666, 151643
   ```
   （即 `<tool_call>` + 一段 JSON 的 token 们 + `</tool_call>` + `<eos>`）
3. 逐 token 跟踪 [src/ncnn_llm_gpt.cpp:874-890](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L874-L890) 的四路分流，记录 `flag_in_tool_call`、`tool_call_content`、是否调用 `callback`、是否 `continue`。

**需要观察的现象 / 预期结果**（待本地验证具体 id）：第一个 `151665` 置 `flag=true`，**不输出**；中间的 JSON token 全部被 decode 后追加到 `tool_call_content`，**不输出给 callback**；`151666` 触发 `handle_tool` 并 `continue`；下一轮若拿到 `151643` 则 `break`。整个过程中 `callback`（用户可见输出）应当**始终没有被调用**——一次纯粹的工具调用，用户屏幕上是空的，直到模型在回填结果后重新开始生成自然语言。

#### 4.3.5 小练习与答案

**练习 1**：为什么工具调用态下用 `bpe->decode({cur_token}, false)` 累积文本，而不是直接保存 token id？
**答案**：因为最终要把累积的内容当成 JSON 字符串解析（`handle_tool` 里 `json::parse`）。decode 把 token id 还原成可见字符（含 `{`、`"`、数字等），拼成完整的 JSON 文本后才好解析。注意这里逐 token decode 再拼接，依赖分词器对 JSON 字符的可逆还原（u3-l2）。

**练习 2**：如果模型的 `tool_call_end_id == -1`（未配置），但 `tool_call_id` 被正确配置成某个正数，会发生什么？
**答案**：`tool_call_id` 分支仍会触发，`flag_in_tool_call` 被置 true，之后所有 token 都进入「偷偷累积」分支，`callback` 永远不被调用——模型会一直「沉默」地累积，直到 `eos` 或步数耗尽。也就是说两个 id 必须**同时**配置才有效，这也是 `define_tools` 闸门用 `||`（任一为 -1 即 no-op）的原因。

---

### 4.4 GenerateConfig.tool_callback 与结果回填

#### 4.4.1 概念说明

`handle_tool` 在拿到完整的调用 JSON 后，需要做两件事：**执行真正的函数**、**把结果送回模型**。前者由用户通过 `GenerateConfig.tool_callback` 提供（因为「加法怎么算」「天气怎么查」只有宿主程序知道），后者由 `handle_tool` 自动完成——它把回调返回值包进一段 `<tool_response>` 模板，再 `prefill` 进 ctx，模型就能「读到」结果并继续作答。

`tool_callback` 的类型定义在 [src/ncnn_llm_gpt.h:40](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L40)：

```cpp
std::function<nlohmann::json(const nlohmann::json&)> tool_callback = nullptr;
```

输入是模型产出的调用 JSON（含 `name`、`arguments`），输出是「工具结果」JSON，由 `handle_tool` 拿去 dump 成字符串塞进上下文。若 `tool_callback` 为 `nullptr`，`handle_tool` 会用一个默认占位响应。

#### 4.4.2 核心流程

`handle_tool` 是 `generate` 内部的一个 lambda（按引用捕获 `cfg` 与 `ctx_ref`），流程为：

1. **解析 JSON**：`json::parse(tool_call_text)`，解析失败则退化成空对象（容错，不崩）。
2. **调用回调**：若 `cfg.tool_callback` 非空，调用它拿到 `tool_resp`；否则 `tool_resp = {{"tool_call", tool_call_json}}`。
3. **拼装工具响应模板**：用固定的 ChatML 措辞把结果包成一段「user 角色的 `<tool_response>` + assistant 重启」的文本。
4. **prefill 回填**：`ctx_ref = prefill(tool_response_pre + tool_resp.dump() + tool_response_post, ctx_ref);`——这一步把工具结果写进 KV cache，并让 `ctx->cur_token` 指向「模型看到结果后应当续写的位置」。

#### 4.4.3 源码精读

`handle_tool` lambda 本体，[src/ncnn_llm_gpt.cpp:846-865](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L846-L865)：

```cpp
auto handle_tool = [&](const std::string& tool_call_text, std::shared_ptr<ncnn_llm_gpt_ctx>& ctx_ref) {
    nlohmann::json tool_call_json;
    try {
        tool_call_json = nlohmann::json::parse(tool_call_text);
    } catch (const std::exception& e) {
        tool_call_json = nlohmann::json::object();          // 解析失败容错
    }

    nlohmann::json tool_resp;
    if (cfg.tool_callback) {
        tool_resp = cfg.tool_callback(tool_call_json);      // 用户回调
    } else {
        tool_resp = nlohmann::json{{"tool_call", tool_call_json}};
    }

    std::string tool_response_pre  = "<|im_end|>\n<|im_start|>user\n<tool_response>\n\n";
    std::string tool_response_post = "\n\n</tool_response><|im_end|>\n<|im_start|>assistant\n<think>\n</think>\n\n";

    ctx_ref = prefill(tool_response_pre + tool_resp.dump() + tool_response_post, ctx_ref);
};
```

注意回填模板的几个讲究：

- 它先 `<|im_end|>` 关掉 assistant 当前那段（模型刚吐完 `</tool_call>`），再以 **user 角色** 给一段 `<tool_response>...结果...</tool_response>`——这正是 ChatML 模板里 tool 结果的标准位置（参见 [src/utils/prompt.cpp:126-130](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/prompt.cpp#L126-L130) 对 `role=="tool"` 的处理）。
- 末尾用 `<|im_start|>assistant\n<think>\n</think>\n\n` 重启一个 assistant 段，并预置一对空的 `<think></think>`——这是 Qwen 系列的约定，告诉模型「直接给最终答案，不必再思考」。
- `tool_resp.dump()` 把回调返回的 JSON 序列化成文本，模型读到的是结构化的结果（如 `{"result":{"value":5}}`）。
- 这一步 `prefill` 同时更新了 `ctx_ref->cur_token`（指向重启 assistant 段之后模型应当续写的首 token），所以 4.3 里 `handle_tool` 返回后紧接着 `continue`，下一轮循环直接消费这个新 token——**工具结果就这样无感地接上了生成流**。

端到端的真实用法在 cli_runner，先注入工具、再挂回调，[examples/llm_ncnn_run/cli_runner.cpp:44-46](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/cli_runner.cpp#L44-L46)：

```cpp
if (!builtin_tools.empty()) {
    ctx = model.define_tools(ctx, builtin_tools, system_prompt);
}
```

回调里查路由表执行真实函数，[examples/llm_ncnn_run/cli_runner.cpp:77-106](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/cli_runner.cpp#L77-L106)（节选）：

```cpp
cfg.tool_callback = [&](const json& call) {
    json result;
    std::string fname = call.at("name").get<std::string>();
    json args = call.value("arguments", json::object());
    if (auto it = builtin_router.find(fname); it != builtin_router.end()) {
        result = it->second(args);          // 真正执行 add/random 等
        handled = true;
    }
    // ……打印 [Tool Call] / [Tool Result]……
    return json{{"result", result}, {"call", call}};
};
```

「路由表」`builtin_router` 是 `名字 → std::function<json(const json&)>` 的映射，在 [examples/llm_ncnn_run/tools.cpp:48-63](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/tools.cpp#L48-L63) 里注册了 `add`、`random` 两个可调用对象：

```cpp
tool_router["add"] = [](const json& args) {
    int a = args.value("a", 0);
    int b = args.value("b", 0);
    return json{{"value", a + b}};
};
```

于是整条链路是：模型吐 `<tool_call>{"name":"add","arguments":{"a":2,"b":3}}</tool_call>` → `handle_tool` 解析 → `tool_callback` 查到 `add` → 执行 `2+3=5` → 返回 `{"result":{"value":5},"call":{...}}` → `handle_tool` 把它 dump 进 `<tool_response>` → prefill 回填 → 模型读到 5，续写「等于 5」。

#### 4.4.4 代码实践

**实践目标**：跑通「注册 add 工具 → 模型调用 → 回调计算 → 结果回填」的端到端闭环。

**操作步骤**：

1. 先确认 `./assets/qwen3_0.6b`（或任意配置了 `setting.functions` 的 Qwen 模型目录）存在；不存在则跳过实际运行，改为源码阅读。
2. 阅读 [tests/test_llm.cpp:220-254](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/tests/test_llm.cpp#L220-L254) 的 `test_model_tool_calling`，它正是本实践任务的样板：构造模型 → prefill system → `make_function_tool<int,int,int>("add",...)` 造工具 → `define_tools` 注入 → prefill 用户问题 `What is 2 + 3?` → 在 `cfg.tool_callback` 里把 `tool_called` 置 true 并返回 `{{"result",{{"value",5}}},{"call",call}}` → `generate`。
3. 在回调里加一行打印（**示例代码**）：
   ```cpp
   std::cout << "model called: " << call.dump() << std::endl;
   ```
4. 执行 `xmake build test_llm && xmake run test_llm`（模型存在时该测试会真实跑；不存在时 `has_model` 返回 false，测试打印 `(skipped - model not found)` 直接通过，见 u8-l3）。

**需要观察的现象**：模型存在且正常时，回调被触发、打印出 `model called: {"name":"add","arguments":{"a":2,"b":3}}`（具体 arguments 取决于模型生成），`tool_called` 为 true；控制台看不到原始 JSON 串（因为工具调用态不调用用户 callback），但在 `tool_response` 回填后模型会继续输出自然语言结论。

**预期结果**：一次完整的工具调用闭环——`tool_callback` 被调用一次、返回的结果经 `handle_tool` 回填进 ctx、`generate` 最终正常返回。若模型未配置 `functions`，则 `define_tools` no-op、回调永不触发（这是验证 4.2 闸门的好时机）。

#### 4.4.5 小练习与答案

**练习 1**：`handle_tool` 里若 `cfg.tool_callback` 为 `nullptr`，会怎样？
**答案**：走 else 分支，`tool_resp = {{"tool_call", tool_call_json}}`，即把模型自己的调用 JSON 原样包进 `<tool_response>` 回填。模型会「看到自己刚才的调用被当作结果返回」，通常没有实际意义，但保证流程不崩——这是给未提供回调的调用方的安全兜底。

**练习 2**：为什么 `handle_tool` 要按引用捕获 `ctx_ref`（`std::shared_ptr<...>&`）而不是按值？
**答案**：因为它要在 lambda 内部把 `ctx_ref` 重新赋值为 `prefill` 返回的新 ctx（`ctx_ref = prefill(...)`）。按引用捕获才能让这个新 ctx 反映到外层 `generate` 的 `ctx` 变量上，使后续循环用到的是「已回填工具结果」的上下文。若按值捕获，赋值只改到 lambda 内的副本，外层 ctx 不变，工具结果就丢失了。

---

## 5. 综合实践

把四个模块串起来，实现一个「自带 add 工具的最小问答程序」。本任务即本讲的 `practice_task`。

**任务**：用 `make_function_tool<int,int,int>` 定义一个 `add(a,b)` 工具，调用 `define_tools` 注入，再用 `generate` 让模型触发 `tool_callback`，在回调里真正计算 `a+b` 并返回。

**步骤**：

1. 复制 `examples/llm_ncnn_run/` 的结构，新建一个最小 main（或在现有 cli_runner 上改）。核心代码（**示例代码**）：

   ```cpp
   #include "ncnn_llm_gpt.h"
   #include "utils/prompt.h"
   using json = nlohmann::json;

   int main() {
       ncnn_llm_gpt model("./assets/qwen3_0.6b");   // 需配置 setting.functions

       // (1) 造工具
       auto add_tool = ncnn_llm_gpt::make_function_tool<int, int, int>(
           "add", "Add two integers.", {"a", "b"});

       // (2) 首发 system + 注入工具
       std::string sys = apply_chat_template({{"system","You are a helpful assistant."}}, {}, false, false);
       auto ctx = model.prefill(sys);
       ctx = model.define_tools(ctx, {add_tool}, "You are a helpful assistant.");

       // (3) 用户提问
       std::string user = apply_chat_template({{"user","What is 2 + 3?"}}, {}, true, false);
       ctx = model.prefill(user, ctx);

       // (4) 生成，挂回调
       GenerateConfig cfg;
       cfg.max_new_tokens = 256;
       cfg.tool_callback = [&](const json& call) {
           int a = call.value("arguments", json::object()).value("a", 0);
           int b = call.value("arguments", json::object()).value("b", 0);
           std::cerr << "[add] " << a << " + " << b << " = " << (a+b) << "\n";
           return json{{"result", {{"value", a + b}}}, {"call", call}};
       };
       ctx = model.generate(ctx, cfg, [](const std::string& tok){ std::cout << tok << std::flush; });
       std::cout << std::endl;
       return 0;
   }
   ```

2. 在 `xmake.lua` 里加一个 target 依赖 `ncnn_llm`（仿照 `llm_ncnn_run`，见 u1-l2），或直接把它写进 `tests/test_llm.cpp` 的 `test_model_tool_calling` 里复用现有构建。

3. 运行，对照 `tests/test_llm.cpp:220-254` 的写法核对你的流程是否一致。

**需要观察的现象 / 预期结果**（待本地验证）：

- 若模型配置了 `functions`：stderr 打印 `[add] 2 + 3 = 5`（或模型给出的其它参数），说明回调被触发；stdout 最终出现类似「2 + 3 等于 5」的自然语言结论——这是工具结果回填后模型续写的。
- 若模型**未**配置 `functions`：`define_tools` 闸门返回原 ctx，模型看不到工具，会尝试直接回答（可能算错），回调永不触发——这正印证了 4.2 的安全降级。

**进阶**：仿照 `examples/llm_ncnn_run/tools.cpp` 的 `make_builtin_router`，把回调里的 `a+b` 改成查一张路由表，再新增一个 `random` 工具（用 `make_function_tool<int,int,int>` + 一个生成随机数的 lambda），让模型能在「加法」和「随机数」之间选择。

## 6. 本讲小结

- **工具描述靠模板生成**：`make_function_tool<Ret,Args...>` 用 `json_type_name` 把 C++ 类型推断成 JSON Schema 类型，折叠表达式展开参数包，一行生成 OpenAI 风格的函数工具 JSON。
- **注入靠 prefill**：`define_tools` 把工具 JSON 经 ChatML 模板拼成 system 文本，再 `prefill` 进 ctx；若模型未配置 `tool_call_id`/`tool_call_end_id`（均为 -1），整个注入是 no-op。
- **两个特殊 token id 来自 model.json**：构造函数从 `setting.functions`（`type=="tool_call"`）读取 `<tool_call>`/`</tool_call>` 字符串、用分词器查表得整数 id；缺省 -1 即「未启用」。
- **解析是两态状态机**：`generate` 循环凭 `tool_call_id`/`tool_call_end_id` 在「普通文本态」与「工具调用态」间切换，调用态下偷偷累积 JSON、不输出给用户，结束 token 触发 `handle_tool` 并 `continue` 跳过本步 decoder。
- **回填是自动的**：`handle_tool` 调用用户的 `tool_callback` 拿结果，包进 `<tool_response>` 模板后 `prefill` 回 ctx，模型因此「看到」结果并续写；`ctx_ref` 按引用捕获使新 ctx 生效。
- **闭环靠路由表**：cli_runner 用 `tool_callback` 查 `builtin_router`（名字→可调用对象）执行真实函数，把结构与 `make_function_tool` 的产物对应起来。

## 7. 下一步学习建议

- 阅读 [u7-l3 内置工具、路由与 JSON 工具](u7-l3-builtin-tools-json.md)（下一篇），深入了解 `make_builtin_tools` / `make_builtin_router` / `merge_tools_by_name` 以及 `json_utils` 的消息解析与清洗工具，把本讲的路由表机制工程化。
- 回顾 [u2-l4 generate 自回归解码主循环](u2-l4-generate-loop.md)，把本讲的工具调用分流视为「嵌入在主循环里的一个分支」，加深对 `clone_ctx`、`history`、`continue` 等控制流的整体理解。
- 若对工具结果如何进入多轮记忆感兴趣，结合 [u2-l5 推理上下文 ctx 与多轮对话](u2-l5-context-and-multiturn.md) 思考：`handle_tool` 的 `prefill` 回填本质上就是「以 user 角色追加一轮」，与普通多轮对话的 ctx 推进是同一套机制。
- 动手做本讲第 5 节的综合实践，并尝试为一个真实需求（如「查时间」「读文件」）定义新工具、注册到 router，体验从工具描述到模型自主调用的完整开发链路。
