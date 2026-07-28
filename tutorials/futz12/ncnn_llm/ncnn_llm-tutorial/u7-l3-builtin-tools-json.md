# 内置工具、路由与 JSON 工具

## 1. 本讲目标

学完本讲，你应当能够：

- 用 `make_builtin_tools()` / `make_builtin_router()` 两个工厂函数，分别生成「一批默认演示工具的描述」和「一个按工具名分发的路由表」，并说清二者为什么必须成对出现。
- 解释 `tool_name_from_openai_tool` 如何从 OpenAI 风格的工具 JSON 里取出名字，以及 `merge_tools_by_name` 如何按名字去重合并两份工具列表。
- 读懂 `parse_messages` / `extract_content` 如何把 OpenAI 风格的 `messages` 数组（`content` 既可能是字符串、也可能是多模态分片数组）翻译成本项目内部的 `Message` 结构。
- 描述 `sanitize_utf8` / `strip_image_payloads` 等 JSON 清洗工具的作用，特别是 base64 探测、UTF-8 修复、大字符串截断这几类「让 JSON 安全可打印」的处理。
- 把 [u7-l2 工具调用机制](u7-l2-tool-calling.md) 里讲过的 `tool_callback` 回调，落到 `cli_runner.cpp` 真正用 `builtin_router.find(name)` 分发的这一行代码上，串起「注册 → 模型调用 → 路由分发 → 回填」的完整闭环。

## 2. 前置知识

本讲是 [u7-l2 工具调用机制](u7-l2-tool-calling.md) 的直接续篇，请先确认你已经了解：

- `ncnn_llm_gpt::make_function_tool<Ret, Args...>`：用 C++ 模板把参数类型推断成 JSON Schema 类型（`int→integer`、`bool→boolean`、浮点→`number`、其余→`string`），一行生成 OpenAI 风格的函数工具描述。
- `define_tools(ctx, tools, system_prompt)`：把工具描述经 ChatML 模板拼进 system prompt，再 `prefill` 进推理上下文 ctx；若模型没配 `tool_call_id` / `tool_call_end_id` 特殊 token，整个注入会降级为 no-op。
- `generate` 主循环里的两态状态机：普通文本态 ↔ 工具调用态，靠这两个特殊 token id 驱动；调用态下偷偷累积 JSON、不输出给用户，遇到结束 token 就调 `GenerateConfig.tool_callback` 拿结果，再 `<tool_response>` 包裹回填 ctx。

此外用到一点 [u7-l1 对话模板](u7-l1-chat-templates.md) 里的 `Message` 结构，以及通用的 UTF-8 与 base64 常识。本讲不再重复 u7-l2 的机制本身，重点放在「工具描述与路由表的工厂」和「JSON 消息的解析与清洗」这两个工程化层次。

## 3. 本讲源码地图

本讲全部源码集中在示例目录 `examples/llm_ncnn_run/`，是 `llm_ncnn_run` 这个 target 的一部分（被 `xmake` 编进同一个可执行文件）：

| 文件 | 作用 |
| --- | --- |
| [tools.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/tools.h) / [tools.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/tools.cpp) | 内置工具与路由：`make_builtin_tools`、`make_builtin_router`、`tool_name_from_openai_tool`、`merge_tools_by_name`。 |
| [json_utils.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/json_utils.h) / [json_utils.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/json_utils.cpp) | JSON 消息解析与清洗：`parse_messages`、`extract_content`、`sanitize_utf8`、`strip_image_payloads`、`looks_like_base64`、`base64_fingerprint`、`truncate_large_strings`、`make_response_id`、`make_error`。 |
| [cli_runner.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/cli_runner.cpp) | 真正消费内置工具与路由的地方：`define_tools` 注入、`cfg.tool_callback` 里用路由分发。 |
| [main.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/main.cpp) | 构造 `builtin_tools` 与 `builtin_router` 并传入 `run_cli`。 |
| [src/ncnn_llm_gpt.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h) | `make_function_tool` / `define_tools` 的声明（u7-l2 已讲，本讲当作被调用的底层）。 |

> 一个重要事实先说在前面：`tools.cpp` 里的 `make_builtin_tools` / `make_builtin_router` 是**活代码**——它们在 `main.cpp` 构造、在 `cli_runner.cpp` 的 `tool_callback` 里被真正调用。而 `merge_tools_by_name` 和 `json_utils.*` 里的一整套函数目前是**已编译、但尚未被 CLI 主路径调用**的可复用工具集（`cli_runner.cpp` 虽然 `#include "json_utils.h"`，却没实际调用其中的函数）。从命名（`make_response_id` 生成 `chatcmpl-...`、`make_error` 带 `invalid_request_error`、`parse_messages` 解析 `messages` 数组）可以判断，它们是为**OpenAI 兼容的 HTTP 服务端**准备的积木。本讲会如实标注哪些是活路径、哪些是待接线的积木。

## 4. 核心概念与源码讲解

### 4.1 内置工具与路由表：make_builtin_tools / make_builtin_router

#### 4.1.1 概念说明

[u7-l2](u7-l2-tool-calling.md) 讲的是「一个工具怎么定义、怎么被模型调用」。那一讲的 `tool_callback` 是一个由调用方提供的回调函数，但回调里**到底怎么根据工具名找到对应的实现**，u7-l2 留了白。本模块补上这一块。

ncnn_llm 的做法是把「工具」拆成两层、用两个工厂函数分别生成：

- **工具描述层** `make_builtin_tools()`：返回一组 OpenAI 风格的 JSON 描述。这些 JSON 只告诉模型「有哪些工具、各自叫什么、参数是什么类型」，本身**不会执行任何计算**。它们最终经 `define_tools` 拼进 system prompt，让模型「看见」工具。
- **工具实现层** `make_builtin_router()`：返回一个 `unordered_map<string, function<json(const json&)>>`，即「工具名 → 可调用对象」的路由表。模型的工具调用进来后，靠这张表按名字找到真正的 C++ 实现。

为什么要拆成两层、而不是把描述和实现塞进一个对象？因为这两层的消费者不同、生命周期也不同：描述层是给**模型**看的（要序列化成文本喂进 prompt），实现层是给**宿主程序**调的（要保留 C++ 闭包）。把它们解耦后，同一份描述可以配不同的实现，反之亦然——这正是「路由」这个词的由来。

#### 4.1.2 核心流程

两个工厂函数 + `cli_runner` 消费它们，构成一条完整链路：

```
main.cpp
  ├─ builtin_tools  = make_builtin_tools()      // 描述层：[random, add]
  └─ builtin_router = make_builtin_router()     // 实现层：{"random": λ, "add": λ}
        │
        ▼
run_cli(...)
  ├─ model.define_tools(ctx, builtin_tools)     // 描述层拼进 system prompt，prefill 进 ctx
  └─ cfg.tool_callback = [&](call){            // 模型发起调用时触发
         fname = call["name"]
         if (builtin_router.find(fname))        // ★ 在路由表里按名字找实现
             result = builtin_router[fname](args)
         else
             result = {"error": "unknown function"}
     }
```

关键点：模型永远不会「直接执行」工具，它只输出一段表示「我要调用 add(a,b)」的 JSON；宿主程序拿到这段 JSON 后，用**工具名**去路由表里查实现、执行、把结果回填。路由表就是「名字 → 实现」的查表操作。

#### 4.1.3 源码精读

先看工具描述层 `make_builtin_tools`，它复用 u7-l2 的 `make_function_tool` 拼出两个演示工具：

```cpp
auto random_tool = ncnn_llm_gpt::make_function_tool<int, int, int>(
    "random", "Generate a random number between two integers.",
    {"floor", "ceiling"});
auto add_tool = ncnn_llm_gpt::make_function_tool<int, int, int>(
    "add", "Add two integers.", {"a", "b"});
return {random_tool, add_tool};
```

完整代码见 [examples/llm_ncnn_run/tools.cpp:34-46](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/tools.cpp#L34-L46)。这里 `make_function_tool<int,int,int>` 的三个 `int` 分别是返回类型和两个参数类型，模板会把它们推断成 JSON Schema 里的 `"integer"`（详见 [src/ncnn_llm_gpt.h:157-184](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L157-L184) 的 `json_type_name` 与折叠表达式）。产出的 `add` 工具描述大致长这样（示例，结构来自源码而非字面常量）：

```json
{"type":"function","function":{"name":"add","description":"Add two integers.",
 "parameters":{"type":"object",
   "properties":{"a":{"type":"integer","description":""},
                 "b":{"type":"integer","description":""}},
   "required":["a","b"]}}}
```

再看实现层 `make_builtin_router`，它为每个名字注册一个 lambda：

```cpp
tool_router["random"] = [](const json& args) {
    int lo = args.value("floor", 0);
    int hi = args.value("ceiling", 1);
    if (lo > hi) std::swap(lo, hi);
    int val = lo + (rand() % (hi - lo + 1));
    return json{{"value", val}};
};
tool_router["add"] = [](const json& args) {
    int a = args.value("a", 0);
    int b = args.value("b", 0);
    return json{{"value", a + b}};
};
```

完整代码见 [examples/llm_ncnn_run/tools.cpp:48-63](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/tools.cpp#L48-L63)。两个细节值得注意：

1. lambda 签名统一是 `json(const json&)`——**入参是模型传来的参数对象，返回值是结果对象**。这种「擦除类型、统一走 JSON」的设计让所有工具能塞进同一张 `unordered_map`，不必为每个工具写不同的函数指针类型。
2. `args.value("floor", 0)` 用默认值兜底：模型若漏传参数，不会崩，而是拿默认值。`random` 还顺手 `std::swap` 保证 `lo<=hi`，是典型的防御式编程。

最后看消费侧。`main.cpp` 用 `enable_builtin_tools`（默认 `true`，见 [options.h:9](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/options.h#L9)，可用 `--no-builtin-tools` 关闭，见 [options.cpp:70-71](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/options.cpp#L70-L71)）决定是否构造工具：

```cpp
std::vector<json> builtin_tools = opt.enable_builtin_tools ? make_builtin_tools() : std::vector<json>();
auto builtin_router = make_builtin_router();
```

见 [examples/llm_ncnn_run/main.cpp:44-45](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/main.cpp#L44-L45)。注意 `builtin_router` 无论开关与否都会构造（它很轻），只有 `builtin_tools` 描述层会随开关置空。

真正的分发发生在 `cli_runner.cpp` 的 `tool_callback` 里：

```cpp
cfg.tool_callback = [&](const json& call) {
    std::string fname = call.at("name").get<std::string>();
    json args = call.value("arguments", json::object());
    if (!builtin_tools.empty()) {
        if (auto it = builtin_router.find(fname); it != builtin_router.end()) {
            result = it->second(args);          // ★ 按名字查表，执行 lambda
            handled = true;
        }
    }
    if (!handled) result = json{{"error", "unknown function"}, {"name", fname}};
    ...
};
```

见 [examples/llm_ncnn_run/cli_runner.cpp:77-106](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/cli_runner.cpp#L77-L106)（路由查表在 [L85-88](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/cli_runner.cpp#L85-L88)）。这段就是把 u7-l2 的 `tool_callback` 黑盒打开：模型调用 → 取名字 → 路由表查实现 → 执行 → 未命中则返回 `unknown function`。`it->second(args)` 这一行就是「路由」的本质。

#### 4.1.4 代码实践

**实践目标**：仿照 `random` / `add`，新增一个「返回当前时间戳」的内置工具，完整走一遍「描述层 + 实现层」的成对注册。

**操作步骤**（这是给读者的练习，需要改动 `tools.cpp`；本讲义本身未改动任何源码）：

1. 在 `tools.cpp` 顶部补一个头文件 `#include <ctime>`（时间戳要用 `std::time`）。
2. 在 `make_builtin_tools()` 的 `return` 列表里追加一个工具描述（示例代码）：

   ```cpp
   // 示例代码：新增 current_time 工具描述
   auto time_tool = ncnn_llm_gpt::make_function_tool<int>(
       "current_time",
       "Return the current UNIX timestamp in seconds.",
       {"utc"});
   return {random_tool, add_tool, time_tool};
   ```

3. 在 `make_builtin_router()` 里注册同名路由（示例代码）：

   ```cpp
   // 示例代码：注册 current_time 的实现
   tool_router["current_time"] = [](const json& args) {
       int ts = static_cast<int>(std::time(nullptr));
       return json{{"value", ts}};
   };
   ```

4. 重新构建并运行：`xmake build llm_ncnn_run` 然后 `xmake run llm_ncnn_run <模型路径>`，在对话里诱导模型调用工具（例如「请用 current_time 工具告诉我现在的时间」）。

**需要观察的现象**：终端应打印形如 `[Tool Call]: {"name":"current_time", ...}` 与 `[Tool Result]: {"value": 17xxxxxx}` 的两行（这两行打印在 [cli_runner.cpp:98-99](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/cli_runner.cpp#L98-L99)），随后模型会基于回填的结果继续作答。

**预期结果**：路由表能命中 `current_time` 并返回一个整数时间戳，模型回复里出现该时间。**待本地验证**：能否真正触发取决于所用模型的 `model.json` 是否配置了 `tool_call_id` / `tool_call_end_id` 特殊 token，以及模型本身是否被训练过工具调用。若没有这些 token，按 u7-l2 的结论，`define_tools` 会降级为 no-op，模型根本看不见工具，自然不会发起调用——这时可以改用下面的源码阅读型实践。

**源码阅读型实践（无需模型）**：直接在 `make_builtin_router()` 末尾临时加一段 `std::cout << tool_router["add"](json{{"a",7},{"b",5}}) << std::endl;` 单独验证某个 lambda 的返回值是否为 `{"value":12}`（需额外 `#include <iostream>`），验证后删掉。这能确认实现层逻辑正确，与模型无关。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `make_builtin_tools` 返回的是 `vector<json>`，而 `make_builtin_router` 返回的是 `unordered_map<...>`？

**参考答案**：描述层要被序列化成文本拼进 prompt、且需要保持顺序（`vector` 保序），它面向「模型阅读」；实现层要按工具名快速查找对应的 C++ 闭包（`unordered_map` O(1) 查找），它面向「程序分发」。两者消费者和访问模式不同，所以容器不同。

**练习 2**：若把 `add` 工具从 `make_builtin_tools` 里删掉、但保留 `make_builtin_router` 里的 `"add"` 路由，会发生什么？

**参考答案**：模型看不见 `add` 的描述（`define_tools` 不会把它写进 prompt），所以基本不会主动调用 `add`；但路由表里仍有实现。结果是「实现存在但模型不知道」——这正说明两层必须成对出现，缺了描述层，实现层就是死代码。

---

### 4.2 OpenAI 风格工具的命名与合并：tool_name_from_openai_tool / merge_tools_by_name

#### 4.2.1 概念说明

OpenAI 的工具描述有一个固定嵌套结构：外层 `{"type":"function", "function": {...}}`，真正的名字、描述、参数藏在内层 `function` 对象里（见 4.1.3 的示例 JSON）。这意味着「取工具名」不是简单地 `tool["name"]`，而要先钻进 `function` 字段。

`tool_name_from_openai_tool` 就是这个「钻一层取名字」的小工具。它存在的意义是**统一抽取工具的身份标识**，供去重、查找、日志等场景复用。`merge_tools_by_name` 则建立在它之上：当你手里有两份工具列表（比如「内置工具」+「用户自定义工具」），需要合并成一份给模型看时，要按名字去重——否则同一个名字出现两次会让模型困惑。

#### 4.2.2 核心流程

```
tool_name_from_openai_tool(tool)
    ├─ 校验 tool 是 object            → 否则返回 ""
    ├─ 校验 tool["function"] 是 object → 否则返回 ""
    └─ 返回 tool["function"]["name"]   → 缺省返回 ""

merge_tools_by_name(base, extra)
    ├─ seen = {} （名字集合）
    ├─ 遍历 base：全部放入 out，名字记进 seen
    └─ 遍历 extra：
         ├─ 名字非空且 seen 里没有 → 放入 out，记进 seen
         ├─ 名字非空但已存在      → 跳过（去重）
         └─ 名字为空（解析失败）  → 照放（无法去重，宁滥毋缺）
```

去重策略是「**base 优先、extra 让位**」：名字冲突时保留 base 里的版本，丢掉 extra 里的同名工具。

#### 4.2.3 源码精读

取名字的函数只有 4 行，但层层校验体现了防御式编程：

```cpp
std::string tool_name_from_openai_tool(const json& tool) {
    if (!tool.is_object()) return {};
    if (!tool.contains("function") || !tool["function"].is_object()) return {};
    return tool["function"].value("name", "");
}
```

见 [examples/llm_ncnn_run/tools.cpp:8-12](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/tools.cpp#L8-L12)。任何一环不符都返回空串而不是抛异常——这样上游用空串判断「这是不是合法工具」，不会因为一个坏数据炸掉整条合并流程。

合并函数用 `unordered_set` 记录已见过的名字，靠 `seen.insert(name).second` 判断是否首次出现（`insert` 返回的 pair 的 second 为 `true` 表示插入成功、即之前没见过）：

```cpp
for (const auto& t : extra) {
    std::string name = tool_name_from_openai_tool(t);
    if (!name.empty()) {
        if (seen.insert(name).second) out.push_back(t);   // 首次见，保留
    } else {
        out.push_back(t);                                 // 无名字，无法判重，照放
    }
}
```

见 [examples/llm_ncnn_run/tools.cpp:14-32](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/tools.cpp#L14-L32)。注意 `else` 分支的设计取舍：解析不出名字的工具（结构不合规）**不参与去重、直接保留**，宁可让下游再处理，也不静默丢弃——这是「不丢数据」的保守策略。

> 现状提示：`merge_tools_by_name` 目前没有被 CLI 主路径调用，属「待接线的积木」。它的典型用途是把 `make_builtin_tools()` 与一份用户从外部（如配置文件或 HTTP 请求体）读进来的工具列表合并。

#### 4.2.4 代码实践

**实践目标**：通过构造几份工具 JSON，验证 `tool_name_from_openai_tool` 的容错与 `merge_tools_by_name` 的去重规则。

**操作步骤**（写一个临时小 main，或临时塞进 `make_builtin_router` 末尾打印后删除）：

```cpp
// 示例代码：验证命名抽取与合并
using json = nlohmann::json;
json good = {{"type","function"},{"function",{{"name","add"}}}};
json dup   = {{"type","function"},{"function",{{"name","add"}}}};
json fresh = {{"type","function"},{"function",{{"name","search"}}}};
json bad   = {{"type","function"}};                 // 缺 function.name

auto merged = merge_tools_by_name({good}, {dup, fresh, bad});
// 预期 merged 含 3 项：good(add)、fresh(search)、bad；dup 因同名被去重
```

**需要观察的现象**：`tool_name_from_openai_tool(bad)` 应返回 `""`；`merged` 的长度应为 3（同名 `add` 只保留 base 里的那份，`bad` 因无名字照放）。

**预期结果**：base 的 `add` 保留，extra 的 `dup` 被丢，`fresh` 与 `bad` 被保留。**待本地验证**（需自行编译运行上述片段）。

#### 4.2.5 小练习与答案

**练习 1**：`tool_name_from_openai_tool` 为什么对每一层都做 `is_object()` / `contains()` 检查，而不是直接写 `tool["function"]["name"]`？

**参考答案**：`nlohmann::json` 对不存在的键用 `operator[]` 会**插入**一个 null 而非报错，对类型不匹配用 `get<>()` 才会抛异常。逐层校验能在数据结构不规范时安全返回空串，避免污染原对象或抛异常打断流程，符合「单点容错、向上返回空」的设计。

**练习 2**：`merge_tools_by_name` 里 `seen.insert(name).second` 的 `.second` 是什么意思？

**参考答案**：`unordered_set::insert` 返回 `pair<iterator, bool>`，其中 `bool` 为 `true` 表示这次真的插入了（之前没有该元素），`false` 表示已存在。用 `.second` 就能一行判断「是否首次见到这个名字」，首次才把工具放进 `out`，从而实现去重。

---

### 4.3 OpenAI 消息解析：parse_messages / extract_content

#### 4.3.1 概念说明

OpenAI 的 Chat Completion 接口用 `messages` 数组描述一段对话，每条消息形如：

```json
{"role":"user", "content":"你好"}
```

但 `content` 有两种合法形态：**字符串**，或**分片数组**（多模态格式）：

```json
{"role":"user", "content":[{"type":"text","text":"这是图"}, {"type":"image_url","image_url":{...}}]}
```

本项目内部用的则是 [u7-l1](u7-l1-chat-templates.md) 里的 `Message` 结构（`role`/`content`/`reasoning_content`/`tool_calls`，见 [src/utils/prompt.h:14-23](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/prompt.h#L14-L23)），其 `content` 只是单个字符串。`parse_messages` + `extract_content` 的职责就是把 OpenAI 的「外部格式」翻译成项目内部的 `Message`，重点是把 `content` 的两种形态都压扁成一个字符串。

#### 4.3.2 核心流程

```
extract_content(content)
    ├─ content 是字符串 → 直接返回
    ├─ content 是数组   → 遍历每个分片：
    │      ├─ 分片是字符串        → 直接拼接
    │      └─ 分片是 {type:"text", text:...} → 把 text 拼上
    │      （type 为 image_url 等非文本分片被忽略）
    └─ 其它 → 返回 ""

parse_messages(messages_json)
    └─ 对每个元素 m：
         ├─ 无 role → 跳过
         ├─ msg.role = m["role"]
         ├─ msg.content = extract_content(m["content"])   // 压扁
         ├─ msg.tool_calls = m["tool_calls"]（若是数组）
         └─ msg.reasoning_content = m.value("reasoning_content","")
```

#### 4.3.3 源码精读

`extract_content` 是把多模态 content 降维成纯文本的核心：

```cpp
if (content.is_array()) {
    std::string merged;
    for (const auto& part : content) {
        if (part.is_string()) {
            merged += part.get<std::string>();
        } else if (part.is_object()) {
            if (part.value("type","") == "text" && part.contains("text"))
                merged += part["text"].get<std::string>();
        }
    }
    return merged;
}
```

见 [examples/llm_ncnn_run/json_utils.cpp:39-58](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/json_utils.cpp#L39-L58)。注意它**只认 `type:"text"` 分片**，`image_url` / `input_audio` 等非文本分片被静默跳过——因为内部的 `Message::content` 只能存字符串，图像等信息要走别的通道（见 4.4 的 `strip_image_payloads`，以及 u5 的图像注入）。

`parse_messages` 把每条消息装配成 `Message`，对 `tool_calls` 额外做了「是数组才取」的保护：

```cpp
if (m.contains("tool_calls") && m["tool_calls"].is_array()) {
    msg.tool_calls = m["tool_calls"].get<std::vector<json>>();
}
msg.reasoning_content = m.value("reasoning_content", "");
```

见 [examples/llm_ncnn_run/json_utils.cpp:60-76](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/json_utils.cpp#L60-L76)。`tool_calls` 被原样存为 `vector<json>`（内部不进一步解析），供后续模板渲染时回放进 prompt（与 u7-l1 的 `Message.tool_calls` 字段对接）。

> 现状提示：`parse_messages` / `extract_content` 目前未被 CLI 主路径调用，属为 HTTP 服务端准备的积木——它们处理的是「网络传进来的标准 OpenAI 请求体」。

#### 4.3.4 代码实践

**实践目标**：用一个混合形态的 `content` 验证 `extract_content` 的压扁行为。

**操作步骤**（示例代码，自行编译运行）：

```cpp
using json = nlohmann::json;
json content = json::array({
    "前缀",
    {{"type","text"}, {"text","中段"}},
    {{"type","image_url"}, {"image_url", {{"url","data:image/png;base64,...."}}}},
    {{"type","text"}, {"text","后缀"}}
});
std::cout << extract_content(content) << std::endl;
```

**需要观察的现象**：输出应为 `前缀中段后缀`，`image_url` 分片被丢弃。

**预期结果**：多模态 content 被压成纯文本，非文本分片不参与拼接。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果一条消息没有 `role` 字段，`parse_messages` 会怎么处理？

**参考答案**：直接 `continue` 跳过整条消息（见源码 `if (!m.contains("role")) continue;`）。因为没有 role 的消息无法被对话模板归类（system/user/assistant），属于无效消息，丢弃最合理。

**练习 2**：为什么 `extract_content` 要同时支持「分片本身是字符串」和「分片是 `{type:"text",...}` 对象」两种？

**参考答案**：OpenAI 规范允许 content 数组的元素既可以是裸字符串，也可以是带 `type` 的对象（便于混排文本与图片）。为了兼容这两种写法、不丢任何文本，函数对两种形态都做了拼接。

---

### 4.4 JSON 清洗工具：sanitize_utf8 / strip_image_payloads 及 base64 探测

#### 4.4.1 概念说明

当 JSON 数据来自网络、模型输出或第三方文件时，常常混入两类麻烦：**无效 UTF-8 字节**（会让下游显示乱码甚至崩溃）和**巨大的 base64 图片载荷**（一张图就是几十万字符，打印/日志/传输时极其浪费）。`json_utils` 提供一组清洗函数处理这些情况：

- `sanitize_utf8`：逐字节校验 UTF-8 编码，把非法字节替换成 `'?'`，保证输出是合法 UTF-8。
- `looks_like_base64`：用启发式规则判断一个字符串「看起来像不像 base64 编码」（典型用途：识别藏在 JSON 字段里的图片数据）。
- `base64_fingerprint`：给一个大 base64 串算一个轻量指纹（前缀+后缀+长度的哈希），便于去重或比对，而不必持有整串。
- `strip_image_payloads`：递归遍历 JSON，把 `{"type":"image",...}` 的 `data` 字段和任何「看起来像 base64」的字符串替换成 `<omitted N bytes>` 占位符。
- `truncate_large_strings`：递归遍历 JSON，把任何超过 `max_bytes` 的字符串替换成占位符。

这一组的共同思想是「**让 JSON 适合被人看、被日志记**」：原始数据不丢（清洗后仍是合法、可读的 JSON），但超大或有害的部分被压缩成占位符。

#### 4.4.2 核心流程与原理

**UTF-8 校验原理**。UTF-8 是变长编码，一个字符由 1～4 个字节组成，首字节的最高位标识了该字符的字节数，后续字节（continuation byte）都以 `10xxxxxx` 开头：

| 字节数 | 首字节范围 | 首字节特征 | 后续字节数 |
| --- | --- | --- | --- |
| 1 | 0x00–0x7F | `0xxxxxxx`（`c < 0x80`） | 0 |
| 2 | 0xC0–0xDF | `110xxxxx`（`(c>>5)==0x6`） | 1 |
| 3 | 0xE0–0xEF | `1110xxxx`（`(c>>4)==0xE`） | 2 |
| 4 | 0xF0–0xF7 | `11110xxx`（`(c>>3)==0x1E`） | 3 |

后续字节统一满足 `(c & 0xC0) == 0x80`。`sanitize_utf8` 就是按这张表逐个字符「对号入座」：能匹配某个合法模式就原样保留对应字节，匹配不上就把当前字节换成 `'?'`。

**base64 探测原理**。base64 只用 `A-Za-z0-9+/=` 这 65 个字符。`looks_like_base64` 统计一段字符串里「合法字符占比」，要求：

- 总长度（去空白后）不少于 1024；
- 合法字符占比 \( \frac{\text{valid}}{n} \geq 0.98 \)（98%）。

为避免对超长串做完整扫描，它在扫过 4096 个字符后就提前用同样的 98% 阈值剪枝。这是一个**启发式**判断——普通英文/代码很难同时满足「够长」和「字符集这么纯」，而 base64 图片恰好两者都满足。

**strip_image_payloads 流程**：

```
递归遍历 json v：
  ├─ v 是数组 → 对每个元素递归
  ├─ v 是对象 →
  │    ├─ 若 type=="image" 且有 data 字符串 → 把 data 换成 <omitted N bytes>
  │    └─ 对每个值：
  │         ├─ 值是字符串且 looks_like_base64 → 换成 <omitted N bytes>
  │         └─ 否则递归
  └─ 其它 → 原样返回
```

#### 4.4.3 源码精读

`sanitize_utf8` 的核心是四个 `if` 对应 1～4 字节序列，`is_cont` 判定后续字节：

```cpp
auto is_cont = [&](unsigned char c) { return (c & 0xC0) == 0x80; };
// 1 字节: c < 0x80
// 2 字节: (c>>5)==0x6 且后有 1 个续字节
// 3 字节: (c>>4)==0xE 且后有 2 个续字节
// 4 字节: (c>>3)==0x1E 且后有 3 个续字节
// 都不匹配 → push_back('?'), 前进 1 字节
```

见 [examples/llm_ncnn_run/json_utils.cpp:134-159](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/json_utils.cpp#L134-L159)。注意每个多字节分支都用 `i + k < s.size()` 防越界，且只有**全部续字节都合法**才整体保留——任一续字节非法就落到最后的 `'?'` 兜底，避免「半个字符」污染输出。

`looks_like_base64` 的 98% 阈值与提前剪枝：

```cpp
if (n > 4096 && valid < n * 98 / 100) return false;   // 提前剪枝
...
if (n < 1024) return false;                            // 太短不算
return valid >= n * 98 / 100;                          // 占比达标才算
```

见 [examples/llm_ncnn_run/json_utils.cpp:8-24](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/json_utils.cpp#L8-L24)。

`strip_image_payloads` 把 `type=="image"` 的 `data` 与任何 base64 串一并替换，复用了上面的探测函数：

```cpp
if (v.value("type","") == "image") {
    if (v.contains("data") && v["data"].is_string()) {
        std::string s = v["data"].get<std::string>();
        v["data"] = "<omitted " + std::to_string(s.size()) + " bytes>";
    }
}
// 再遍历所有值：字符串若 looks_like_base64 → 替换；否则递归
```

见 [examples/llm_ncnn_run/json_utils.cpp:107-132](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/json_utils.cpp#L107-L132)。这种「显式 image.data + 隐式 base64 串」双保险，能同时处理「标准图片字段」和「乱塞在任意字段里的 base64」两种情况。

`truncate_large_strings` 用同一个「`<omitted N bytes>`」占位风格，递归处理任意深度：

```cpp
if (v.is_string()) {
    if (s.size() <= max_bytes) return v;
    return "<omitted " + std::to_string(s.size()) + " bytes>";
}
// 数组/对象 → 递归每个元素/每个值
```

见 [examples/llm_ncnn_run/json_utils.cpp:86-105](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/json_utils.cpp#L86-L105)。`max_bytes==0` 时整体跳过（视为「不截断」）。

此外还有两个面向 HTTP 响应的小工具：`make_response_id` 生成形如 `chatcmpl-<毫秒时间戳>` 的响应 id（[json_utils.cpp:78-84](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/json_utils.cpp#L78-L84)），`make_error` 生成 `{"error":{"type":"invalid_request_error","message":...},"status":...}`（[json_utils.cpp:161-166](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/json_utils.cpp#L161-L166)）——这两个命名格式都是 OpenAI 接口的约定，进一步印证这套工具的服务端定位。

> 现状提示：以上函数目前均未被 CLI 主路径调用，是 `llm_ncnn_run` 已编译、待接线的服务端积木。

#### 4.4.4 代码实践

**实践目标**：用一段被破坏的 UTF-8 与一个塞了 base64 的 JSON，验证 `sanitize_utf8` 与 `strip_image_payloads` 的效果。

**操作步骤**（示例代码，自行编译运行；需 `#include "json_utils.h"`）：

```cpp
using json = nlohmann::json;

// 1) UTF-8 修复：0xFF 不是任何合法 UTF-8 字符的首字节
std::string bad = std::string("ab") + char(0xFF) + std::string("cd");
std::cout << sanitize_utf8(bad) << std::endl;          // 预期: ab?cd

// 2) base64 擦除：构造一个长 base64 串
std::string b64(2000, 'A');                             // 2000 个 'A'，符合 base64 字符集
json msg = {{"type","image"}, {"data", b64}, {"note", "hi"}};
std::cout << strip_image_payloads(msg).dump() << std::endl;
// 预期: data 变成 "<omitted 2000 bytes>"，note 保留 "hi"
```

**需要观察的现象**：第一行输出 `ab?cd`（非法字节被 `?` 替换）；第二行 JSON 里 `data` 字段变成 `<omitted 2000 bytes>`，其余字段不变。

**预期结果**：如上。**待本地验证**（需自行编译运行）。你也可以把 `b64` 改成 500 个字符，验证 `looks_like_base64` 因长度不足 1024 而「不擦除」——此时长 base64 串不会被替换。

#### 4.4.5 小练习与答案

**练习 1**：`sanitize_utf8` 为什么在每个多字节分支里都要检查 `i + k < s.size()`？

**参考答案**：UTF-8 多字节字符的续字节可能跨越字符串末尾——比如首字节声称是 3 字节字符，但后面只剩 1 个字节。不检查边界就会越界读取。加上长度检查后，长度不足时直接落到 `'?'` 兜底，安全且不越界。

**练习 2**：`strip_image_payloads` 已经专门处理了 `type=="image"` 的 `data` 字段，为什么还要对「任意 looks_like_base64 的字符串」都替换？

**参考答案**：现实数据不一定规整——base64 图片可能被塞进任意字段名（如 `url`、`content`、自定义字段），不一定叫 `data`、也不一定带 `type:"image"` 标记。用 `looks_like_base64` 做通用兜底，能覆盖所有「长且字符集纯净」的可疑字符串，确保任何位置的图片载荷都被压缩，避免日志/传输被巨型 base64 淹没。

---

## 5. 综合实践

把本讲四个模块串起来，设计一个「**带去重合并的内置工具扩展**」小任务：

1. **准备两份工具列表**：用 `make_builtin_tools()` 作为 `base`；再手写一份 `extra`，里面放一个与 `add` 同名的工具（参数不同），外加一个全新的 `current_time` 工具。
2. **合并**：调用 `merge_tools_by_name(base, extra)`，验证同名 `add` 按「base 优先」被去重，而 `current_time` 作为新名字被保留。
3. **补实现**：为 `current_time` 在 `make_builtin_router()` 里注册路由（参考 4.1.4 的示例代码）。
4. **清洗验证**：构造一条 OpenAI 风格的多模态 `messages`，用 `parse_messages` 解析，再对解析结果里可能出现的 base64 字段跑 `strip_image_payloads`，确认图片载荷被擦除、文本内容保留。
5. **（可选，需模型）**重新构建运行，诱导模型调用 `current_time`，观察 `[Tool Call]` / `[Tool Result]` 两行输出。

这个任务把「工具描述合并（4.2）→ 路由注册（4.1）→ 消息解析与清洗（4.3/4.4）」连成一条线，复现了「拿到外部 OpenAI 请求 → 合并工具 → 注入模型 → 模型调用 → 路由分发 → 结果回填」的服务端处理骨架。其中第 5 步能否真正触发模型调用，取决于模型的工具调用 token 配置，**待本地验证**。

## 6. 本讲小结

- `make_builtin_tools` 与 `make_builtin_router` 是成对的工厂：前者生成给模型看的「工具描述 JSON 列表」，后者生成给程序用的「名字→lambda 路由表」；二者解耦是因为消费者（模型 vs 程序）和访问模式（顺序读 vs 按名查）不同。
- 真正的分发发生在 `cli_runner.cpp` 的 `cfg.tool_callback` 里，核心一行是 `builtin_router.find(fname)->second(args)`——这就是「路由」的落地，把 u7-l2 留白的 `tool_callback` 内部实现补全。
- `tool_name_from_openai_tool` 层层校验地从 `function.name` 取工具身份；`merge_tools_by_name` 据此按名字去重，策略是「base 优先、extra 让位」，无名字的工具照放不丢。
- `parse_messages` / `extract_content` 把 OpenAI 的 `messages`（`content` 可为字符串或多模态分片数组）压扁成项目内部的 `Message`，只保留 `type:"text"` 分片。
- `sanitize_utf8` 按 UTF-8 编码规则逐字节校验、非法字节替换为 `'?'`；`strip_image_payloads` 借助 `looks_like_base64`（≥1024 字符且合法率 ≥98%）递归擦除图片载荷；两者共同保证 JSON 合法、可读、不臃肿。
- 工程现状：`make_builtin_tools` / `make_builtin_router` 是活路径；`merge_tools_by_name` 与整套 `json_utils` 是已编译、待接线的 OpenAI 兼容服务端积木。

## 7. 下一步学习建议

- 想看「自定义算子如何被模型在生成阶段触发、又如何与工具调用交织」，可进入 [u7-l4 自定义 ncnn 算子 GDR / ShortConv](u7-l4-custom-ncnn-ops-gdr.md)。
- 想从工程角度验证工具与模板逻辑（不依赖大模型），可阅读 [u8-l3 测试框架与单元测试](u8-l3-testing.md)，并尝试为 `merge_tools_by_name` 或 `sanitize_utf8` 补一个单元测试。
- 若你想把本讲的 `json_utils` 服务端积木真正跑起来，建议自行实现一个最小的 OpenAI 兼容 HTTP 服务端（解析 `messages` → `parse_messages` → 复用 `define_tools` / `generate` / 路由表），这会是理解整套工具链最好的练手项目（仓库目前未提供该服务端，属扩展实践）。
