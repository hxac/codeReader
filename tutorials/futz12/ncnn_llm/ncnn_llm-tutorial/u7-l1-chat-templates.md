# 对话模板 ChatML / YouTu 与 thinking

## 1. 本讲目标

本讲深入 ncnn_llm 的 `prompt` 模块（`src/utils/prompt.h`、`src/utils/prompt.cpp`），回答一个关键问题：**用户的多轮对话消息（system / user / assistant / tool）是如何被「拼接」成一段模型看得懂的纯文本 prompt 的？**

在 [u2-l5](u2-l5-context-and-multiturn.md) 里我们已经看到，`run_cli` 的多轮循环每轮都会调用 `apply_chat_template(...)` 生成一段字符串，再喂给 `model.prefill(...)`。当时我们把模板拼接当黑盒处理，本讲把这个黑盒彻底打开。

学完后你应当掌握：

1. 知道 `TemplateType` 枚举有哪两种模板（ChatML / YouTu），各自长什么样。
2. 理解 `Message` 结构如何承载一条消息（含 `reasoning_content` 与 `tool_calls`）。
3. 能读懂 `apply_chatml_template` 与 `apply_youtu_template` 两个内部函数的拼接逻辑，并能手算出给定消息序列的输出。
4. 理解 `enable_thinking` 开关如何控制历史「思考内容」是否写进 prompt。
5. 知道 `detect_template_type` 如何根据 `model.json` 自动选择模板。

---

## 2. 前置知识

在开始前，建议你先具备以下认知（本手册前几讲已建立）：

- **对话模板（chat template）是什么**：大模型本质上是「续写文本」的机器，它并不天生理解「角色」。为了让一个 decoder-only 模型按对话方式工作，业界约定了一套**文本格式**——用特殊字符串标签（如 `<|im_start|>`、`<|User|>`）把每条消息包起来，拼成一段长文本。模型在被训练时就见过这种格式，于是续写时会自然地「扮演 assistant」。
- **特殊令牌（special token）**：这些标签（`<|im_start|>`、`<|im_end|>`、`<think>` 等）在分词器里通常是**整段原子**（见 [u3-l1](u3-l1-tokenizer-abstraction.md) 的 `additional_special_tokens`），分词时不会被切开。
- **prefill / generate**：模板拼出的字符串会先 `encode` 成 token id，再进入 [u2-l3](u2-l3-prefill-flow.md) 的 prefill。本讲只讲「字符串怎么拼」，不涉及分词与推理。
- **工具调用（tool calling）**：模板还要负责把「有哪些工具可用」「工具返回了什么」也写进 prompt，这部分细节的**运行时行为**留给 [u7-l2](u7-l2-tool-calling.md)，本讲只看模板里工具相关的**文本片段**长什么样。

一句话定位：`prompt` 模块是一个**纯字符串拼装器**——输入是结构化的消息列表，输出是一段符合某种对话格式的 `std::string`，没有任何推理、分词或 I/O。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/utils/prompt.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/prompt.h) | 暴露公共 API：`TemplateType` 枚举、`Message` 结构、三个 `apply_chat_template` / `apply_youtu_chat_template` 重载。 |
| [src/utils/prompt.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/prompt.cpp) | 两个模板的**内部实现**（`apply_chatml_template`、`apply_youtu_template`）与公共 API 的转发。 |
| [examples/llm_ncnn_run/cli_runner.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/cli_runner.cpp) | `detect_template_type` 的定义，以及 `run_cli` 里如何每轮调用 `apply_chat_template` 把用户输入拼成 prompt。 |
| [examples/llm_ncnn_run/main.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/main.cpp) | 在 `main` 中调用 `detect_template_type`，把选好的模板类型一路传给 `run_cli`。 |
| [tests/test_llm.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/tests/test_llm.cpp) | 模板的单元测试（`test_prompt_template_basic`、`test_thinking_mode` 等），是验证你理解是否正确的「标准答案」。 |

---

## 4. 核心概念与源码讲解

本讲按 4 个最小模块展开：模板分发（`TemplateType` + `detect_template_type`）、消息数据模型（`Message`）、两套模板实现（ChatML / YouTu）、以及 `enable_thinking` 开关。

### 4.1 TemplateType 枚举与模板分发

#### 4.1.1 概念说明

ncnn_llm 目前内置**两种**对话格式，用枚举 `TemplateType` 表示：

- `CHATML`：Qwen3 / MiniCPM4 风格，用 `<|im_start|>` / `<|im_end|>` 包裹每条消息。这是**默认模板**。
- `YOUTU`：YouTu 大模型风格，用 `<|User|>` / `<|Assistant|>` 作为角色边界。

为什么要分两种？因为不同模型族在**训练时**见过的对话格式不同。把 Qwen3 训练时用的 ChatML 格式喂给一个用 YouTu 格式训练的模型，效果会很差。所以模板必须和模型的训练格式对齐。

#### 4.1.2 核心流程

```
用户拿到一段消息 messages
        │
        ▼
选择 TemplateType（CHATML 或 YOUTU）
        │
        ▼
apply_chat_template(type, messages, ...)   ← 公共分发函数
        │
        ├── type == CHATML ──► apply_chatml_template(...)
        └── type == YOUTU  ──► apply_youtu_template(...)
        │
        ▼
返回拼好的 std::string
```

注意：还存在一个**不带 `type` 参数**的 `apply_chat_template` 重载，它**写死走 ChatML**——这是「默认模板」语义的体现。

#### 4.1.3 源码精读

枚举定义只有两个值，注释里点出了各自代表的模型风格：

[enum class TemplateType —— CHATML 与 YOUTU 两种格式](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/prompt.h#L9-L12)

公共分发函数用一个 `switch` 把请求路由到对应的内部实现，`default` 兜底也走 ChatML：

[apply_chat_template(type, ...) —— 按 TemplateType 路由到 chatml 或 youtu](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/prompt.cpp#L312-L326)

而那个不带 `type` 的重载，直接转发给 `apply_chatml_template`，所以「不指定模板 = ChatML」：

[apply_chat_template(无 type) —— 默认走 ChatML](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/prompt.cpp#L295-L302)

#### 4.1.4 代码实践

**实践目标**：理解「指定 type」与「不指定 type」两条路径的差异。

**操作步骤**：

1. 打开 `src/utils/prompt.cpp`，找到第 312–326 行的分发函数。
2. 把 `switch` 里的 `case TemplateType::YOUTU:` 临时改成 `case TemplateType::CHATML:`（在本地实验，**不要提交**）。
3. 重新构建并运行 `test_llm`，观察 YouTu 相关行为是否退化为 ChatML。

**需要观察的现象**：分发函数本质就是一个 switch；改完后所有调用都走 ChatML 路径。

**预期结果**：分发逻辑只决定「调用哪个内部函数」，不改变拼接算法本身。实验后请还原改动。

> 说明：本实践为「源码阅读 + 局部实验型」，目的是让你确认分发函数的中转角色，不依赖任何模型文件。

#### 4.1.5 小练习与答案

**练习 1**：如果调用的是不带 `TemplateType` 参数的 `apply_chat_template` 重载，实际会走哪个模板？
**答案**：ChatML。该重载直接转发给 `apply_chatml_template`（见 prompt.cpp:295-302）。

**练习 2**：分发函数里为什么 `default` 分支也返回 ChatML？
**答案**：作为兜底——即便将来枚举新增了值但分发函数没及时更新，也至少能返回一个可用的 ChatML 结果，而不是编译/运行错误。这是一种防御式编程。

---

### 4.2 Message 结构与多消息拼接

#### 4.2.1 概念说明

模板的输入不是一坨原始文本，而是一组**结构化消息** `std::vector<Message>`。每条 `Message` 描述「谁说了什么」，外加两个可选字段：

- `role`：角色，常用 `"system"` / `"user"` / `"assistant"` / `"tool"`。
- `content`：这条消息的正文文本。
- `reasoning_content`（可选）：assistant 的**思考过程**文本（reasoning / thinking）。
- `tool_calls`（可选）：assistant 发起的工具调用，是一个 `vector<json>`。

把「正文」「思考」「工具调用」拆成三个字段，是为了让模板能**按需重组**它们（比如 `enable_thinking=false` 时就不输出思考）。

#### 4.2.2 核心流程

```
Message{ role, content, reasoning_content, tool_calls }
     │
     │  由模板循环遍历每条 Message
     ▼
根据 role 进入不同分支：
  system    ──► 写成系统段
  user      ──► 写成用户段
  assistant ──► （可选写 <think>）写正文 + （可选写 tool_call）
  tool      ──► 写成工具返回段
```

注意 `Message` 提供了一个**非 explicit** 的多参构造函数，所以可以这样用花括号初始化：`{{"user", "Hello!"}}` —— 每个内层 `{"user", "Hello!"}` 都会隐式构造一个 `Message`。`cli_runner` 就大量依赖这一点。

#### 4.2.3 源码精读

`Message` 是一个朴素的聚合数据结构，字段全部 public，并提供了默认构造与带参构造：

[struct Message —— role/content/reasoning_content/tool_calls 四字段](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/prompt.h#L14-L23)

`cli_runner` 里典型的用法——用花括号隐式构造一组 `Message`，再交给模板：

[run_cli 里 {{\"user\", input}} 的花括号隐式构造 Message](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/cli_runner.cpp#L64-L66)

#### 4.2.4 代码实践

**实践目标**：验证 `Message` 的隐式构造与字段访问。

**操作步骤**：

1. 阅读 `tests/test_llm.cpp` 第 23–26 行 `test_prompt_template_basic`，看它如何用 `{"system", "...", "", {}}` 显式构造 Message。
2. 对比 `cli_runner.cpp` 第 64 行 `{{"user", input}}` 的简写形式。

**需要观察的现象**：两种写法构造的是同一种 `Message` 对象，只是省略了默认参数（`reasoning_content`、`tool_calls`）。

**预期结果**：`Message` 的带参构造给 `reasoning_content`/`tool_calls` 提供了默认空值，所以省略它们完全合法。

> 说明：本实践为源码阅读型，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Message` 的构造函数没有标 `explicit`？
**答案**：为了允许 `{{"user", "Hello!"}}` 这种花括号隐式构造，让调用方代码更简洁。如果标了 `explicit`，就必须逐个 `Message(...)` 显式构造。

**练习 2**：`tool_calls` 为什么用 `std::vector<json>` 而不是专门的结构体？
**答案**：为了灵活性——工具调用的结构（name / arguments）由 JSON 直接承载，省去再定义一个结构体并反复和 JSON 互转的成本；这也和 OpenAI 风格的工具调用 JSON 天然对齐（见 [u7-l2](u7-l2-tool-calling.md)）。

---

### 4.3 ChatML 与 YouTu 两套模板的实现对比

这是本讲最核心的一节。两个内部函数（`apply_chatml_template`、`apply_youtu_template`）都是 `static` 的，只通过公共 API 暴露。

#### 4.3.1 概念说明

**ChatML**（默认）：每条消息用「成对标签」包裹，标签自带角色名。一段典型 ChatML prompt 长这样：

```
<|im_start|>system
You are a helpful assistant.<|im_end|>
<|im_start|>user
Hello!<|im_end|>
<|im_start|>assistant
```

注意每个 `<|im_start|>` 后面跟一个换行，再写角色名和正文，最后 `<|im_end|>` 收尾。当 `add_generation_prompt=true` 时，结尾会追加一个**空的** `<|im_start|>assistant\n`，相当于「请模型续写 assistant 这一轮」。

**YouTu**：不用成对标签，而是用一个「角色起始符」`<|User|>` / `<|Assistant|>` 作为分隔，system 内容直接前置、不加角色标签：

```
You are a helpful assistant.<|User|>Hello!<|Assistant|>
```

风格上的核心差异：ChatML 是「块状 + 显式结束标签」，YouTu 是「流式 + 起始符分隔」。

#### 4.3.2 核心流程

**ChatML 拼接流程**（`apply_chatml_template`）：

```
1. 若提供了 tools：
     把 system 正文 + 工具描述 + 工具调用示例，拼成一个特殊的 system 块
   否则：
     若首条消息是 system，正常输出一个 system 块
2. 倒序扫描消息，找到「最后一条非工具返回的 user 消息」位置 last_query_index
   （用于决定 thinking 写在哪条 assistant 上，见 4.5）
3. 正序遍历消息，按 role 分支拼接：
     system(非首条) / user ──► <|im_start|>{role}\n{content}<|im_end|>\n
     assistant            ──► （可选 <think>）{content}（可选 tool_call）<|im_end|>\n
     tool                 ──► <|im_start|>user\n<tool_response>...</tool_response><|im_end|>\n
4. 若 add_generation_prompt ──► 追加 <|im_start|>assistant\n
```

**YouTu 拼接流程**（`apply_youtu_template`）：

```
1. 收集所有 system 消息内容，用 \n\n 连接成 system_prompt
2. 若提供了 tools：把工具描述段拼到 system_prompt 末尾
3. 直接输出 system_prompt（无角色标签；bos 由分词器处理）
4. 正序遍历消息（system 跳过）：
     user      ──► <|User|>{content}
     assistant ──► <|Assistant|>（可选 <think>）{content}（可选 tool_call）
     tool      ──► <|User|><tool_response>{content}</tool_response>
5. 若 add_generation_prompt ──► 追加 <|Assistant|>
```

两者的关键差异可总结成下表：

| 维度 | ChatML | YouTu |
| --- | --- | --- |
| 消息边界 | `<|im_start|>...<|im_end|>` 成对标签 | `<|User|>` / `<|Assistant|>` 起始符 |
| system 处理 | 独立的 `<|im_start|>system` 块 | 直接前置纯文本，无角色标签 |
| 角色名位置 | 标签内（`<|im_start|>user`） | 起始符本身（`<|User|>`） |
| generation 提示 | `<|im_start|>assistant\n` | `<|Assistant|>` |
| 工具描述块 | `<tools>...</tools>` XML 风格 | `<|begin_of_tool_description|>...<|end_of_tool_description|>` + 代码块 |
| thinking 支持 | 支持（有 `enable_thinking` 参数） | 仅原样回放历史 `<think>`（无开关） |

#### 4.3.3 源码精读

**ChatML —— 无工具时的 system 块**（首条消息若是 system，单独输出一个块）：

[apply_chatml_template 无工具分支：输出 system 块](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/prompt.cpp#L45-L49)

**ChatML —— user / system(非首条) 的拼接**，注意 `<|im_start|>` 后的换行与角色名：

[ChatML：user/system 消息拼接为 <|im_start|>{role}\n{content}<|im_end|>](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/prompt.cpp#L77-L79)

**ChatML —— generation 提示**，结尾追加一个空的 assistant 块：

[ChatML：add_generation_prompt 追加 <|im_start|>assistant\n](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/prompt.cpp#L133-L135)

**YouTu —— 收集 system 内容**，把多条 system 用 `\n\n` 连接：

[apply_youtu_template：收集 system 消息内容](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/prompt.cpp#L156-L165)

**YouTu —— user 消息拼接**，只有一个起始符 `<|User|>`，没有结束标签：

[YouTu：user 消息拼接为 <|User|>{content}](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/prompt.cpp#L218-L231)

**YouTu —— generation 提示**，追加一个起始符：

[YouTu：add_generation_prompt 追加 <|Assistant|>](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/prompt.cpp#L284-L286)

**带工具时的差异**（这是两者工具描述格式的分水岭）：

- ChatML 把工具塞进一个 `<tools>...</tools>` XML 块，并示范 `<tool_call>` 格式：
  [ChatML 工具描述块：<tools> XML + tool_call 示例](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/prompt.cpp#L30-L44)
- YouTu 把工具用 ```` ```json ```` 代码块逐个列出，外加 `<|begin_of_tool_description|>` / `<|end_of_tool_description|>` 包裹：
  [YouTu 工具描述块：代码块 + begin/end_of_tool_description](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/prompt.cpp#L183-L200)

> 工具描述只是「写进 prompt 的文本」，真正的工具调用运行时机制（`define_tools`、`tool_callback`）见 [u7-l2](u7-l2-tool-calling.md)。

#### 4.3.4 代码实践

**实践目标**：用 `apply_chat_template` 分别构造 ChatML 与 YouTu 模板，**手算**并验证二者输出差异。

**操作步骤**：

1. 准备一组消息：`[system: "You are a helpful assistant.", user: "Hello!"]`，`tools={}`，`add_generation_prompt=true`。
2. 分别调用：
   - `apply_chat_template(TemplateType::CHATML, messages, {}, true, false)`
   - `apply_chat_template(TemplateType::YOUTU, messages, {}, true, false)`
3. 把两次结果打印出来对比。

**需要观察的现象 / 预期结果**（由源码逻辑可确定性推出，无需模型）：

ChatML 输出：
```
<|im_start|>system
You are a helpful assistant.<|im_end|>
<|im_start|>user
Hello!<|im_end|>
<|im_start|>assistant
```

YouTu 输出：
```
You are a helpful assistant.<|User|>Hello!<|Assistant|>
```

差异一目了然：ChatML 每条消息都成对包裹、结尾有换行；YouTu 把 system 直接前置、整条 prompt 是一串紧凑的起始符分隔流。

> 这组结果可直接由阅读 prompt.cpp 推出，不依赖运行环境。若想运行验证，可参考 4.5.4 的测试方法把打印加进 `test_llm`。

#### 4.3.5 小练习与答案

**练习 1**：同样的消息序列，ChatML 输出比 YouTu 长很多，为什么？
**答案**：ChatML 每条消息都带 `<|im_start|>{role}\n` 前缀和 `<|im_end|>\n` 后缀，还有换行；YouTu 只用一个起始符、且 system 无标签，故更紧凑。这直接影响 token 数与上下文占用。

**练习 2**：ChatML 处理 `tool` 角色消息时，用的是哪个角色标签？
**答案**：用的是 `user` 标签——把 `<tool_response>...</tool_response>` 包在 `<|im_start|>user ... <|im_end|>` 里（见 prompt.cpp:126-130）。也就是说「工具返回」在 ChatML 里被伪装成一条 user 消息。

**练习 3**：YouTu 模板里连续多条 `tool` 消息会怎么处理？
**答案**：第一条 tool 会输出 `<|User|><tool_response>...</tool_response>`，紧随其后的 tool 消息只追加 `content`（不开新起始符），见 prompt.cpp:273-281，把多个工具返回合并进同一个 `<|User|>` 段。

---

### 4.4 detect_template_type 与主链路接线

#### 4.4.1 概念说明

前面讲了「给定一个 `TemplateType` 怎么拼模板」，但这个 type 是**从哪来**的？答案是 `detect_template_type`：它读取模型目录下的 `model.json`，根据其中的 `type` 字段决定用哪种模板。这样用户只要换一个模型目录，模板就能**自动跟着切换**，无需手动指定命令行参数。

#### 4.4.2 核心流程

```
main(opt.model_path)
   │
   ▼
detect_template_type(model_path)
   │  打开 {model_path}/model.json
   │  读取 config["type"]
   │
   ├── type == "youtu_llm" ──► TemplateType::YOUTU
   └── 其它 / 读不到 / 抛异常 ──► TemplateType::CHATML（默认）
   │
   ▼
run_cli(..., template_type, ...)
   │  每轮用 apply_chat_template(template_type, ...) 拼接
```

注意三个「默认回退到 ChatML」的时机：`model.json` 打不开、没有 `type` 字段、或解析时抛异常（`catch(...)` 静默吞掉）。

#### 4.4.3 源码精读

`detect_template_type` 的实现非常短，核心就是「打开文件 → 读 `type` 字段 → 比较 `youtu_llm`」，任何意外都回退默认：

[detect_template_type —— 读 model.json 的 type 字段选择模板](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/cli_runner.cpp#L9-L29)

在 `main` 里，模型路径经过 `normalize_model_path` 归一化（见 [u1-l4](u1-l4-cli-entry-and-options.md)）后，立刻调用它选模板：

[main 中 detect_template_type 的调用点](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/main.cpp#L41)

随后 `template_type` 一路传进 `run_cli`：

[main 调用 run_cli，把 template_type 传入](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/main.cpp#L56)

在 `run_cli` 里，模板类型被用在两个地方：一是启动时打印提示（让用户知道当前用的哪种模板），二是首轮 system 预填与每轮用户消息拼接：

[run_cli：打印当前模板 + 用 template_type 预填 system](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/cli_runner.cpp#L38-L42)

> 细节提醒：`detect_template_type` 读的是 `model.json` 顶层的 `type` 字段，**不是** [u1-l5](u1-l5-model-json-config.md) 里 `ncnn_llm_gpt` 构造函数读取的 `setting`/`params`/`tokenizer`。两者是不同的读取方（一个是 `cli_runner`，一个是构造函数），读的字段也不同。

#### 4.4.4 代码实践

**实践目标**：理解模板选择完全由 `model.json` 的 `type` 字段驱动。

**操作步骤**：

1. 打开任意一个模型目录的 `model.json`，查看顶层是否有 `type` 字段。
2. 若没有 `type` 字段，或值不是 `"youtu_llm"`，预测 `detect_template_type` 会返回什么。

**需要观察的现象**：只要 `type != "youtu_llm"`（含缺失、打不开、异常），都会走 ChatML。

**预期结果**：绝大多数 LLM 模型目录（Qwen3 等）没有顶层 `type` 字段或值非 `youtu_llm`，因此默认就是 ChatML；只有 YouTu 系模型目录写明 `"type": "youtu_llm"` 才会切到 YouTu 模板。

> 说明：源码阅读型实践，无需构建。

#### 4.4.5 小练习与答案

**练习 1**：如果 `model.json` 是损坏的非法 JSON，`detect_template_type` 会发生什么？
**答案**：`ifs >> config` 会抛异常，被外层 `catch(...)` 捕获，函数返回 `TemplateType::CHATML`。即「坏了就默认 ChatML」，不会让程序崩溃。

**练习 2**：为什么 `detect_template_type` 放在 `cli_runner.cpp` 而不是 `prompt.cpp`？
**答案**：因为它依赖文件 I/O 和 `model.json` 路径——这属于 CLI 运行时的关注点；而 `prompt.cpp` 是纯字符串拼装、无 I/O 依赖，保持纯粹便于复用与测试。

---

### 4.5 enable_thinking 开关与 thinking 模式

#### 4.5.1 概念说明

「thinking 模式」指像 Qwen3 这类模型，在正式回答前先输出一段 `<think>...</think>` 的思考过程。`enable_thinking` 开关控制的是：**当把历史 assistant 消息写进 prompt 时，要不要把它的「思考内容」也写进去**。

这是一个容易被误解的点：`enable_thinking` **不影响**当前轮生成时模型是否「自发」产生 `<think>`——那是由模型自身训练与推理决定的（见下方源码佐证）。它只影响「历史思考内容」在 prompt 里的可见性。

#### 4.5.2 核心流程

在 ChatML 的 assistant 分支里，决定是否输出 `<think>` 块的判断是：

```
show_thinking = enable_thinking
              ∧ 该 assistant 消息在「最后一条 user 查询」之后(is_after_last_query)
              ∧ (它是最后一条消息(is_last_message)  ∨  它确实带 reasoning_content)
```

只有 `show_thinking` 为真，才会在正文前插入：

```
<think>
{reasoning_content}
</think>

```

`reasoning_content` 有两个来源：显式字段，或从 `content` 里解析 `<think>...</think>` 自动提取。

#### 4.5.3 源码精读

ChatML assistant 分支里，先尝试从 `content` 里解析出 `<think>` 段（若没有显式 `reasoning_content`）：

[ChatML：从 content 解析 <think> 填充 reasoning_content](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/prompt.cpp#L84-L93)

关键的 `show_thinking` 判定——三个条件的合取：

[ChatML：show_thinking 三条件判定](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/prompt.cpp#L95-L99)

`show_thinking` 为真时才输出 `<think>` 块：

[ChatML：show_thinking 为真时输出 <think> 块](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/prompt.cpp#L103-L105)

**重要的运行时佐证**：在 `ncnn_llm_gpt.cpp` 的构造函数里，`<think>` 与 `</think>` 这两个 token 的 id（`think_id` / `think_end_id`）确实被加载了，但在整个 `generate` 主循环里**并没有被用来拦截或隐藏思考输出**（用 grep 确认它们只在构造时赋值、从未在循环里读取）。这与 [u2-l4](u2-l4-generate-loop.md) 的结论一致：当前主循环把 `<think>` 当作普通文本流式输出，不做特殊处理。

[ncnn_llm_gpt 构造函数：加载 think_id / think_end_id（仅记录，循环中未消费）](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L162-L172)

另一个佐证是 `cli_runner` 里调用 `apply_chat_template` 时，`enable_thinking` 这个位置**永远传 `false`**：

[run_cli 每轮调用都传 enable_thinking=false](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/cli_runner.cpp#L57-L67)

也就是说，开箱即用的 CLI 默认**不**把历史思考写进上下文；`enable_thinking=true` 是留给库使用者的能力（如自定义前端），需要主动传入。

#### 4.5.4 代码实践

**实践目标**：观察 `enable_thinking=true/false` 在**带 reasoning_content 的 assistant 历史消息**时的输出差异。

**操作步骤**：

1. 准备消息序列（含一条带思考的 assistant 回复）：
   ```
   messages = [
     {user, "Solve 2+3."},
     {assistant, content="5", reasoning_content="2+3=5"},
   ]
   ```
   `tools={}`，`add_generation_prompt=false`（只看历史拼接，不追加 generation 提示）。
2. 分别调用：
   - `apply_chat_template(messages, {}, false, /*enable_thinking=*/true)`
   - `apply_chat_template(messages, {}, false, /*enable_thinking=*/false)`
3. 用 `tests/test_llm.cpp` 的 `test_thinking_mode`（第 185–197 行）作为对照——它对**纯 user 消息**做 true/false 对比，注意它只断言「两者都非空」，**不**断言二者不同。
4.（可选验证）在 `test_llm.cpp` 里临时新增一个测试，打印上面两种结果（实验后请删除或保留为你自己的练习）。

**需要观察的现象 / 预期结果**（可由源码确定性推出）：

`enable_thinking=true` 时输出（assistant 在最后一条 user 之后、且是最后一条消息、且有 reasoning_content → `show_thinking=true`）：
```
<|im_start|>user
Solve 2+3.<|im_end|>
<|im_start|>assistant
<think>
2+3=5
</think>

5<|im_end|>
```

`enable_thinking=false` 时输出（`show_thinking=false`，思考被丢弃）：
```
<|im_start|>user
Solve 2+3.<|im_end|>
<|im_start|>assistant
5<|im_end|>
```

**关键观察**：对于「纯 user 单消息」的场景（如 `test_thinking_mode`），由于没有 assistant 历史消息，`enable_thinking` true/false 的输出**完全相同**——这就是该测试不断言「不同」的原因。开关只在「带思考的 assistant 历史消息」上才产生可见差异。

> 这组结果由阅读 prompt.cpp 的 `show_thinking` 三条件逻辑直接推出，无需模型即可确认。

#### 4.5.5 小练习与答案

**练习 1**：`enable_thinking` 是否能「关闭」当前轮模型自发产生 `<think>` 的能力？
**答案**：不能。它只控制**历史** assistant 消息的思考内容是否写进 prompt。当前轮是否产生 `<think>` 由模型自身决定；当前 generate 主循环也不拦截 `<think>`，会原样流出。

**练习 2**：`show_thinking` 的三个条件中，`is_after_last_query` 的作用是什么？
**答案**：只对「最后一条 user 查询之后」的 assistant 回复显示思考——避免把每一轮历史的思考都堆进 prompt、浪费上下文。定位「最后一条 user 查询」靠的是 prompt.cpp:51-68 的倒序扫描 `last_query_index`。

**练习 3**：为什么 `test_thinking_mode` 不敢断言 true/false 两种输出不同？
**答案**：因为它只喂了一条 user 消息、没有带思考的 assistant 历史，此时 `show_thinking` 恒为 false，两种调用输出相同。要看到差异，消息里必须有带 `reasoning_content`（或含 `<think>` 段）的 assistant 消息。

---

## 5. 综合实践

把本讲知识串起来，完成下面这个综合任务：

**任务**：为一个**新接入的 YouTu 系模型**，端到端验证模板选择与拼接是否正确。

1. **配置驱动**：假设你有一个 YouTu 模型目录 `assets/my_youtu_model/`。在该目录的 `model.json` 顶层加上 `"type": "youtu_llm"`。说明这一行会让 `detect_template_type` 返回什么、从而让 `run_cli` 走哪个模板分支。（参考 4.4.3）
2. **手算 prompt**：以 `run_cli` 启动时的首轮 system 预填为例（[cli_runner.cpp:41-42](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/cli_runner.cpp#L41-L42)），它调用的是 `apply_chat_template(template_type, {{"system", "You are a helpful assistant."}}, {}, false, false)`。请在 YouTu 分支下手算这段字符串，确认它**不包含** `<|im_start|>`、而是直接输出 system 纯文本。
3. **多轮拼接**：接着用户输入 `"你好"`，`run_cli` 会调 `apply_chat_template(template_type, {{"user", "你好"}}, {}, true, false)`（[cli_runner.cpp:64-66](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/cli_runner.cpp#L64-L66)）。手算 YouTu 输出，确认结尾有 `<|Assistant|>`（generation 提示）。
4. **对比 ChatML**：把同一组消息用 `TemplateType::CHATML` 再算一遍，列出二者在 system 处理、消息边界、generation 提示上的三处差异。
5. **测试验证**：参考 `tests/test_llm.cpp` 的 `test_prompt_template_basic`（第 22–39 行）与 `test_thinking_mode`（第 185–197 行）的写法，为你手算的两段输出各写一个 `TEST_ASSERT`，确认关键字（如 `<|User|>` 或 `<|im_start|>`）出现/不出现。

**预期产出**：一份手算结果表 + 两条新增断言。这个任务把「配置 → 模板选择 → 字符串拼接 → 测试验证」整条链路打通，是接入任何新模型时排查「模型答非所问」问题的基本功（prompt 格式不对是最常见的坑）。

---

## 6. 本讲小结

- `prompt` 模块是**纯字符串拼装器**：输入 `vector<Message>`，输出符合某种对话格式的 `std::string`，无推理、无分词、无 I/O。
- 两种内置模板：**ChatML**（默认，`<|im_start|>...<|im_end|>` 成对块状）与 **YouTu**（`<|User|>` / `<|Assistant|>` 起始符流式）。两者在 system 处理、消息边界、工具描述格式、generation 提示上都有差异。
- `Message` 用 `role` / `content` / `reasoning_content` / `tool_calls` 四字段描述一条消息，构造函数非 explicit 以支持花括号隐式构造。
- `enable_thinking` 只控制**历史** assistant 思考内容是否写进 prompt（三条件 `show_thinking`），**不影响**当前轮模型自发产生 `<think>`；`cli_runner` 开箱即用时恒传 `false`。
- `detect_template_type` 读 `model.json` 顶层 `type` 字段，仅 `"youtu_llm"` 切 YouTu，其余（含缺失/异常）一律默认 ChatML。
- 模板产出会交给 [u2-l3](u2-l3-prefill-flow.md) 的 prefill；模板里的工具描述文本则与 [u7-l2](u7-l2-tool-calling.md) 的工具运行时机制衔接。

---

## 7. 下一步学习建议

- 阅读 [u7-l2 工具调用机制](u7-l2-tool-calling.md)：本讲只讲了模板里「工具描述」与「tool_call 文本」长什么样，下一讲讲 `make_function_tool` / `define_tools` / `tool_callback` 如何在运行时驱动真正的工具调用。
- 回看 [u2-l5](u2-l5-context-and-multiturn.md) 的多轮循环，结合本讲重新理解「每轮 prefill 一段模板字符串」的完整数据流。
- 若要为新模型族接入**自定义模板**，可在 `prompt.cpp` 新增一个 `apply_xxx_template` 内部函数，并在分发 `switch`（prompt.cpp:312-326）与 `detect_template_type`（cli_runner.cpp:9-29）里各加一个分支——这是本讲思路的自然延伸。
- 建议同步阅读 `tests/test_llm.cpp` 的全部模板相关测试（第 22–217 行），它们是模板行为的「可执行规格」。
