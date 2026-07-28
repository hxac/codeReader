# 测试框架与单元测试

## 1. 本讲目标

学完本讲，你应该能够：

- 读懂 `tests/test_framework.h` 这个自研的极简测试框架：`TestRunner` 如何收集用例、如何运行、如何统计、如何返回退出码，以及 `TEST_ASSERT` 宏的工作方式。
- 读懂 `tests/test_llm.cpp` 里 12 个测试用例的写法，特别是**纯逻辑测试**与**依赖模型文件的测试**如何分流。
- 理解 `has_model` 模型存在性跳过机制：为什么没有模型权重时测试套件仍然能全绿退出。
- 能够仿照现有用例，为 `prompt.cpp` 里的某个函数（如 `apply_youtu_chat_template`）新增一个单元测试，加入 runner 并通过 `xmake build test_llm && xmake run test_llm`。

## 2. 前置知识

本讲是「高级篇」的第一类主题：不再讲推理算法本身，而是讲**如何验证推理之外、支撑整个项目的工程基础设施——测试**。阅读本讲前，你需要：

- 了解 C++ 的基本语法：`std::function`、`std::vector`、lambda、宏（`#define`）。
- 大致知道什么是「单元测试」：把一个函数的输入和预期输出写成断言，程序自动判定对错。
- 已经读过 [u2-l3](u2-l3-prefill-flow.md)（prefill 流程），知道 `ncnn_llm_gpt`、`apply_chat_template`、`Message`、`make_function_tool` 这些符号大致是什么；本讲的测试正是在验证这些纯函数。

几个关键术语先建立直觉：

- **断言（assertion）**：一条「条件应当成立」的检查。比如「prompt 不为空」「prompt 里包含 `<|im_start|>system`」。条件不成立就视为失败。
- **用例（test case）**：一组断言加上为它准备的输入数据。本项目中每个用例是一个返回 `bool` 的自由函数。
- **运行器（runner）**：负责把所有用例跑一遍、统计通过/失败数、最后给出总结的对象。
- **退出码（exit code）**：程序结束时返回给操作系统的整数。0 通常表示成功，非 0 表示有失败。这是把「测试结果」接进 CI（持续集成）的关键。

## 3. 本讲源码地图

本讲只涉及两个源码文件，加上作为被测对象的 prompt 模块和一个构建配置：

| 文件 | 作用 |
|------|------|
| [tests/test_framework.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/tests/test_framework.h) | 自研的极简单元测试框架：`TestRunner` 类 + `TEST_ASSERT` 宏。整个框架只有一个头文件。 |
| [tests/test_llm.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/tests/test_llm.cpp) | 实际的测试用例集合 + `main`。12 个用例（10 个纯逻辑 + 2 个模型级）在这里注册到 runner。 |
| [src/utils/prompt.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/prompt.h) / [src/utils/prompt.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/prompt.cpp) | 被测对象：`apply_chat_template`、`apply_youtu_chat_template`、`Message`、`make_function_tool` 都在这里。 |
| [xmake.lua](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua) | `test_llm` 这个 target 的定义，决定它编译什么、依赖什么、运行目录在哪。 |

## 4. 核心概念与源码讲解

### 4.1 TestRunner 测试运行器与 TEST_ASSERT 宏

#### 4.1.1 概念说明

很多 C++ 项目会引入 Google Test、Catch2 这类重型测试框架。ncnn_llm 没有这么做——它在 `tests/test_framework.h` 里**手写了一个极简框架**，只有约 60 行。这样做的好处是：零依赖、不需要额外安装、和项目的 xmake 构建天然兼容，初学者也能一眼看懂。

这个框架提供两样东西：

1. 一个 `TestRunner` 类，负责「注册用例 → 依次运行 → 统计 → 返回退出码」。
2. 一个 `TEST_ASSERT(cond, msg)` 宏，供用例内部写断言。

#### 4.1.2 核心流程

一个用例从注册到产生结果的过程如下：

```text
main()
  ├── runner.add_test("用例名", 函数指针)   // 把 {name, func} 存进 tests_ 向量
  ├── ... 多次 add_test ...
  └── runner.run_all()
        └── for 每个用例:
              ├── try:
              │     result = func()        // 调用用例函数
              │     result==true  → PASSED, passed++
              │     result==false → FAILED,  failed++
              ├── catch (std::exception):
              │     FAILED (exception), failed++
        └── 打印 Passed/Failed/Total
        └── return failed>0 ? 1 : 0        // 退出码
```

用例函数内部，`TEST_ASSERT` 的行为是：条件不满足时，打印一条错误信息到 `stderr`，然后**直接 `return false`**——这会立刻结束当前用例函数，把它标记为失败。

#### 4.1.3 源码精读

先看 `TestResult` 与 `TestRunner` 类的声明：

[TestRunner 类与 add_test 注册入口：tests/test_framework.h:14-18](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/tests/test_framework.h#L14-L18)

```cpp
void add_test(const std::string& name, std::function<bool()> test_func) {
    tests_.push_back({name, test_func});
}
```

`add_test` 接收「用例名 + 一个返回 `bool` 的可调用对象」。注意它用的是 `std::function<bool()>`，所以既能传**普通函数指针**（如本项目里的 `test_prompt_template_basic`），也能传 **lambda**。用例被存进私有向量 `tests_`。

> 顺带一提：头文件顶部还声明了一个 [`TestResult` 结构体（test_framework.h:8-12）](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/tests/test_framework.h#L8-L12)，含 `name/passed/message` 三字段。但翻遍实现会发现 `run_all` 并没有用到它（统计是用局部 `int passed/failed` 完成的）。这是一个**声明了但未被使用**的类型，属于历史遗留，读源码时不必纠结。

核心是 `run_all`：

[run_all：逐个运行用例并统计：tests/test_framework.h:20-49](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/tests/test_framework.h#L20-L49)

```cpp
int run_all() {
    int passed = 0;
    int failed = 0;
    std::cout << "=== Running Tests ===\n\n";
    for (const auto& test : tests_) {
        std::cout << "Running: " << test.name << "... ";
        try {
            bool result = test.func();
            if (result) { std::cout << "PASSED\n"; passed++; }
            else        { std::cout << "FAILED\n"; failed++; }
        } catch (const std::exception& e) {
            std::cout << "FAILED (exception: " << e.what() << ")\n";
            failed++;
        }
    }
    // ... 打印 Passed/Failed/Total ...
    return failed > 0 ? 1 : 0;
}
```

有三点设计值得注意：

1. **try/catch 兜底**：用例若抛出 `std::exception`（比如 `json::at` 越界、`token_to_id().at()` 找不到 token），不会让整个测试程序崩溃，而是被捕获、记为 `FAILED (exception: ...)`。这让一个用例的崩溃不会拖垮后续用例。
2. **返回值是退出码**：`return failed > 0 ? 1 : 0`。`main` 直接 `return runner.run_all();`，于是「全绿 → 进程退出 0」「有红 → 进程退出 1」。这是把测试结果接入 CI、shell 脚本（`&&` 串联）的标准做法。
3. **失败也继续跑**：某个用例失败后，循环不会中断，会继续跑剩下的用例，让你一次看到全部失败，而不是修一个跑一次。

最后是断言宏：

[TEST_ASSERT 宏：tests/test_framework.h:59-63](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/tests/test_framework.h#L59-L63)

```cpp
#define TEST_ASSERT(cond, msg) \
    if (!(cond)) { \
        std::cerr << "Assertion failed: " << msg << "\n"; \
        return false; \
    }
```

它的精髓在 `return false`：因为用例函数签名是 `bool f()`，`return false` 会**立刻结束当前用例**并把它判定为失败。这等价于「断言失败 = 本用例失败」，但**不会**影响其他用例。注意它没有用 `do { ... } while(0)` 包裹，所以在某些上下文（比如不带花括号的 `if` 后面）可能有意想不到的行为；在本项目里所有用例都在函数顶层调用它，所以没问题。

#### 4.1.4 代码实践

**实践目标**：亲手验证「退出码」这个机制——这是测试框架接入自动化的命脉。

**操作步骤**：

1. 阅读 `run_all` 的最后一行，确认它返回 `failed > 0 ? 1 : 0`。
2. 在 `tests/test_llm.cpp` 里临时把任意一个纯逻辑用例（例如 `test_prompt_template_basic`）的第一条断言改成必失败，比如把 `TEST_ASSERT(!prompt.empty(), ...)` 改成 `TEST_ASSERT(prompt.empty(), "故意失败")`。
3. 重新构建并运行，观察终端输出与退出码：

```bash
xmake build test_llm
xmake run test_llm
echo "退出码: $?"
```

**需要观察的现象**：终端会打印某个用例 `FAILED`，最后 `Failed: 1`，且 `echo $?` 输出 `1`。

**预期结果**：进程退出码为 `1`。改回原断言后重跑，退出码恢复为 `0`。这验证了「退出码 = 测试结论」，shell 里 `xmake run test_llm && echo OK` 只有全绿才会打印 `OK`。

> 注意：本实践要求你临时修改 `tests/test_llm.cpp`。这是练习用的本地改动，**练完后请改回原样**（本讲义禁止修改源码，这里只是供你本地实验）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `run_all` 要用 `try/catch` 包住每个用例的调用？如果不包会怎样？

> **答案**：为了防止某个用例抛异常导致整个测试进程崩溃、后续用例无法运行。不包的话，一个 `json::at` 越界就会让进程直接终止，你只能看到第一个崩溃点，无法一次获得全部用例的结果。

**练习 2**：`TEST_ASSERT` 失败时为什么是 `return false` 而不是 `exit(1)`？

> **答案**：`return false` 只结束当前用例函数，把它标记为失败，runner 会继续跑下一个用例；`exit(1)` 会直接终止整个进程，后续用例全部无法执行，也失去了「一次跑完看全貌」的意义。

---

### 4.2 纯逻辑测试：模板与工具定义

#### 4.2.1 概念说明

`test_llm.cpp` 里的 12 个用例分成泾渭分明的两类：

- **纯逻辑测试**（10 个）：只测不依赖模型权重的纯函数，主要是 prompt 模板拼装（`apply_chat_template`）和工具 JSON 生成（`make_function_tool`）。这些用例**不需要任何模型文件**，任何人 clone 仓库后都能立刻跑通。
- **模型级测试**（2 个）：需要真实模型权重（`qwen3_0.6b`）才能跑，测的是 prefill/generate/工具调用等端到端行为。

这种分流是本测试体系的核心理念：**能用纯函数验证的逻辑，绝不依赖沉重的模型推理**。prompt 拼装、工具 JSON 格式这些都是确定性字符串变换，用纯逻辑测试又快又稳；而真正的推理正确性才需要模型级测试兜底。

#### 4.2.2 核心流程

纯逻辑测试的套路高度一致，可以抽象成一个模板：

```text
1. 构造输入（一组 Message 或一组工具定义）
2. 调用被测函数（apply_chat_template / make_function_tool）
3. 用 TEST_ASSERT 检查输出字符串/JSON 的关键特征
   —— 用 find(...) != npos 检查"应当出现"的子串
   —— 用 find(...) == npos 检查"不应出现"的子串
4. 全部通过则 return true
```

#### 4.2.3 源码精读

先看一个最典型的模板测试：

[基础模板测试 test_prompt_template_basic：tests/test_llm.cpp:22-39](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/tests/test_llm.cpp#L22-L39)

```cpp
bool test_prompt_template_basic() {
    std::vector<Message> messages = {
        {"system", "You are a helpful assistant.", "", {}},
        {"user", "Hello!", "", {}}
    };
    std::string prompt = apply_chat_template(messages, {}, true, false);
    TEST_ASSERT(!prompt.empty(), "Prompt should not be empty");
    TEST_ASSERT(prompt.find("<|im_start|>system") != std::string::npos,
                "Prompt should contain system message start");
    // ...
    return true;
}
```

注意几个细节：

- `Message` 的构造用了**花括号聚合初始化** `{"system", "...", "", {}}`，对应 [prompt.h:21-22 的四参数构造函数](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/prompt.h#L21-L22)（role/content/reasoning_content/tool_calls）。该构造函数非 explicit，所以花括号能隐式构造。
- `apply_chat_template(messages, {}, true, false)` 的四个参数依次是：消息列表、工具列表（空）、`add_generation_prompt=true`（末尾加生成提示）、`enable_thinking=false`。
- 断言用 `find(...) != std::string::npos` 判断子串存在——这是 C++ 字符串测试最常见的写法，**不要求精确匹配整串**，只检查关键特征，避免测试因无关的空白/换行变化而脆弱。

再看工具定义测试，它验证的是 `make_function_tool` 生成的 JSON 结构：

[工具定义测试 test_tool_definition：tests/test_llm.cpp:68-84](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/tests/test_llm.cpp#L68-L84)

```cpp
auto tool = ncnn_llm_gpt::make_function_tool<int, int, int>(
    "random",
    "Generate a random number between two integers.",
    {"floor", "ceiling"}
);
TEST_ASSERT(tool.is_object(), "Tool should be a JSON object");
TEST_ASSERT(tool["type"] == "function", "Tool type should be function");
TEST_ASSERT(tool["function"]["name"] == "random", "Tool name should be random");
TEST_ASSERT(tool["function"]["parameters"]["properties"].is_object(),
            "Tool should have parameters properties");
```

这里 `tool` 是 `json` 对象（nlohmann_json），断言直接用 `tool["function"]["name"] == "random"` 这种**链式下标 + 比较**来检查 JSON 结构。`make_function_tool<int,int,int>` 的三个模板参数推断出两个参数 `floor`/`ceiling` 都是整数类型（详见 [u7-l2 工具调用机制](u7-l2-tool-calling.md)），这个测试就是在固化「函数工具 JSON 的 schema 契约」。

其余 8 个纯逻辑用例都遵循同样的套路，只是覆盖不同的边界：带工具的模板（`test_prompt_template_with_tools` 检查 `# Tools` 段是否出现）、空工具（`test_empty_tools` 反向检查 `# Tools` **不**出现）、长对话（`test_long_conversation` 塞 20 条消息）、thinking 开关（`test_thinking_mode`）等。其中 `test_empty_tools` 用 `find(...) == npos` 做**反向断言**，是值得学习的写法。

[test_empty_tools 反向断言：tests/test_llm.cpp:170-182](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/tests/test_llm.cpp#L170-L182)

#### 4.2.4 代码实践

**实践目标**：在 `main` 里跟踪一个纯逻辑用例的注册与运行，理解「无模型即可全绿」。

**操作步骤**：

1. 打开 `tests/test_llm.cpp`，找到 [`main` 函数（test_llm.cpp:285-307）](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/tests/test_llm.cpp#L285-L307)。
2. 确认 `assets/` 目录下**没有**任何模型（纯逻辑测试不依赖模型，所以有无模型都不影响这 10 个用例）。
3. 构建并运行：

```bash
xmake build test_llm
xmake run test_llm
```

**需要观察的现象**：终端先打印 `=== Unit Tests ===`，随后 10 个纯逻辑用例逐个 `PASSED`；接着打印 `=== Model Tests ===`，两个模型用例会因找不到模型而显示 `(skipped - model not found)` 后仍记为 `PASSED`（详见 4.3）。

**预期结果**：`Passed: 12 / Failed: 0 / Total: 12`，进程退出码为 `0`。这证明**即使没有任何模型权重，整个测试套件依然全绿**——这正是纯逻辑测试分流的威力。

#### 4.2.5 小练习与答案

**练习 1**：`test_empty_tools` 用 `find("# Tools") == std::string::npos` 断言，而 `test_prompt_template_with_tools` 用 `find("# Tools") != std::string::npos`。为什么要成对地写「正向 + 反向」两个用例？

> **答案**：正向用例证明「给了工具就应当出现 `# Tools` 段」，反向用例证明「没给工具就不应出现 `# Tools` 段」。两者合起来才能确认这段逻辑不是「永远出现」或「永远不出现」的退化实现，覆盖了 if 分支的两边。

**练习 2**：为什么这些纯逻辑断言都用 `find(...) != npos` 检查子串，而不是 `prompt == "某个精确字符串"`？

> **答案**：精确匹配极其脆弱——模板里任何一处空白、换行、标点的微调都会让测试失败，而这种变化往往不影响正确性。子串检查只锁定「语义关键特征」（如 `<|im_start|>system`），既验证了行为，又对无关细节宽容，是更健壮的测试写法。

---

### 4.3 has_model 模型存在性跳过机制

#### 4.3.1 概念说明

模型级测试面临一个现实问题：模型动辄几百 MB 到几 GB，**无法**放进 git 仓库，clone 后默认不存在。如果测试套件在「无模型」时直接报错失败，那 CI 在没配模型的机器上会永远红，纯逻辑测试的成果也被拖累。

本项目的解法是一个极简的「存在性跳过」机制：测试函数开头先问「模型在不在」，不在就打印一句 skip 提示并**返回 `true`**（注意不是 `false`），从而让该用例被记为 PASSED 而非 FAILED。这样测试套件在任何环境下都能全绿退出。

#### 4.3.2 核心流程

```text
test_model_xxx()
  ├── has_model("qwen3_0.6b")?
  │     ├── 否 → 打印 "(skipped - model not found) "，return true（算 PASSED）
  │     └── 是 → 继续：构造模型 → prefill → generate → 断言 → return true
```

`has_model` 的查找顺序是先 `./assets/<name>`，再 `./<name>`：

[has_model 与 get_model_path：tests/test_llm.cpp:9-19](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/tests/test_llm.cpp#L9-L19)

```cpp
static bool has_model(const std::string& model_name) {
    std::string path = "./assets/" + model_name;
    if (std::filesystem::exists(path)) return true;
    return std::filesystem::exists("./" + model_name);
}
static std::string get_model_path(const std::string& model_name) {
    std::string path = "./assets/" + model_name;
    if (std::filesystem::exists(path)) return path;
    return "./" + model_name;
}
```

两处 `./` 都是**相对于运行目录**的路径。而 `test_llm` 这个 target 在 [xmake.lua:103 把 rundir 设成了项目根 `$(projectdir)/`](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L103)，所以 `./assets/qwen3_0.6b` 实际指向仓库根下的 `assets/qwen3_0.6b`。这也是为什么 [u1-l3](u1-l3-directory-and-source-map.md) 强调过「`assets/` 是放模型权重的地方，权重需自行下载」。

#### 4.3.3 源码精读

跳过逻辑在每个模型级用例的开头都一样：

[模型存在性跳过：tests/test_llm.cpp:220-224](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/tests/test_llm.cpp#L220-L224)

```cpp
bool test_model_tool_calling() {
    if (!has_model("qwen3_0.6b")) {
        std::cout << "(skipped - model not found) ";
        return true;
    }
    ncnn_llm_gpt model(get_model_path("qwen3_0.6b"));
    // ... 真正的推理与断言 ...
}
```

这里有一个**值得审视的设计取舍**：跳过时返回的是 `true`，意味着「跳过」和「真正通过」在最终统计里无法区分——两者都算 PASSED。运行时唯一的区分线索是那一行 `(skipped - model not found)` 文本，它会被原样打印在 `PASSED` 前面，变成类似 `Running: model_tool_calling... (skipped - model not found) PASSED`。

这是一个务实但不精确的折中：好处是测试套件在无模型 CI 上不会误报失败；代价是你不能光看 `Passed: 12` 就断定「模型级测试真的跑过并验证了正确性」——必须翻日志确认有没有 `(skipped)` 字样。理解这一点很重要：**全绿 ≠ 模型行为被验证过**，只意味着「能跑的逻辑都跑过了」。

#### 4.3.4 代码实践

**实践目标**：亲手触发一次 skip，看清「跳过」在输出里的样子，并理解它对统计的影响。

**操作步骤**：

1. 确认仓库根下既没有 `assets/qwen3_0.6b` 也没有 `./qwen3_0.6b`（默认就是没有）。
2. 运行：

```bash
xmake run test_llm 2>&1 | grep -A1 "model_tool_calling\|model_context_memory"
```

**需要观察的现象**：两个模型级用例的输出行里都带 `(skipped - model not found)`，紧接着是 `PASSED`。

**预期结果**：即便没有任何模型，`Failed` 仍为 `0`。如果你之后真的把 `qwen3_0.6b` 放进 `assets/`，这两个用例才会真正加载模型、跑 prefill/generate——那时它们的输出里就不再有 `(skipped)` 了。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `has_model` 不满足时的 `return true` 改成 `return false`，会有什么后果？

> **答案**：那么在任何没有 `qwen3_0.6b` 的环境（包括大多数 CI、大多数新 clone）里，这两个模型级用例都会被记为 FAILED，测试套件永远报 `Failed: 2`、退出码永远为 1，纯逻辑测试的成果也被连累。这正是当前设计要避免的。

**练习 2**：为什么 `has_model` 要先查 `./assets/<name>` 再查 `./<name>` 两个位置？

> **答案**：为了兼容两种放置模型的方式——既支持「按约定放进 `assets/`」（rundir 是项目根时这是默认位置），也支持「直接放在项目根」。`get_model_path` 用同样的顺序返回第一个存在的路径，保证后续 `ncnn_llm_gpt model(path)` 能拿到有效路径。

---

### 4.4 模型级测试：tool_calling 与 context_memory

#### 4.4.1 概念说明

当模型确实存在时，`test_model_tool_calling` 和 `test_model_context_memory` 这两个用例会做**端到端**验证：真的构造一个 `ncnn_llm_gpt`，真的跑 prefill/generate，验证工具调用闭环和多轮上下文记忆。它们把 [u2-l3 ~ u2-l5](u2-l5-context-and-multiturn.md) 以及 [u7-l2](u7-l2-tool-calling.md) 讲的推理链路当成被测对象。

这两个用例是整个测试套件里**唯一**会触发真实推理的，因此也最慢、最依赖环境。它们的存在价值是：在「有模型」的开发机上，提供一道端到端的回归保险。

#### 4.4.2 核心流程

`test_model_tool_calling` 验证工具调用闭环：

```text
1. 构造模型 → prefill(system prompt) 得到基线 ctx
2. define_tools 把 add(a,b) 工具注入 ctx
3. prefill(user: "What is 2 + 3?", ctx)
4. generate(ctx, cfg 带 tool_callback)
     └── 模型若输出 <tool_call>...</tool_call>，回调被触发，tool_called=true
5. （本用例不强制断言 tool_called，宽松返回 true）
```

`test_model_context_memory` 验证多轮记忆：

```text
1. prefill(system) → ctx
2. prefill("My favorite color is blue.") → generate（让模型消化）→ ctx
3. prefill("What is my favorite color?") → generate（捕获 response）
4. TEST_ASSERT(!response.empty()) —— 至少确认能产出非空回复
```

#### 4.4.3 源码精读

先看工具调用用例：

[test_model_tool_calling：tests/test_llm.cpp:220-254](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/tests/test_llm.cpp#L220-L254)

```cpp
ncnn_llm_gpt model(get_model_path("qwen3_0.6b"));
std::string system_prompt = "You are a helpful assistant.";
std::string prompt = apply_chat_template({{"system", system_prompt}}, {}, false, false);
auto ctx = model.prefill(prompt);

auto tools = ncnn_llm_gpt::make_function_tool<int, int, int>(
    "add", "Add two integers.", {"a", "b"});
ctx = model.define_tools(ctx, {tools}, system_prompt);

std::string user_msg = apply_chat_template({{"user", "What is 2 + 3?"}}, {}, true, false);
ctx = model.prefill(user_msg, ctx);

GenerateConfig cfg;
cfg.max_new_tokens = 256;
cfg.tool_callback = [&](const json& call) {
    tool_called = true;
    return json{{"result", {{"value", 5}}}, {"call", call}};
};
ctx = model.generate(ctx, cfg, [](const std::string& token) {});
```

这段代码完整演示了 [u7-l2](u7-l2-tool-calling.md) 讲的工具调用闭环：`define_tools` 把工具 JSON 拼进 system 文本并 prefill 进 ctx；`generate` 主循环一旦解析到 `<tool_call>` 就会调用 `cfg.tool_callback`，回调返回的结果会被包成 `<tool_response>` 回填给模型。注意这里的链式 ctx 传递——每一步 `ctx = model.xxx(ctx, ...)` 都返回**新的** ctx（靠 [u2-l5](u2-l5-context-and-multiturn.md) 讲的 `clone_ctx` 机制），这正是多轮记忆的载体。

值得指出一个**测试严谨性问题**：该用例设置了 `tool_called` 标志并在回调里置 true，但函数末尾**并没有**对 `tool_called` 做断言（直接 `return true`）。也就是说，即便模型没真正触发工具调用，这个用例也算通过。这是一个「宽松」的端到端冒烟测试——它只保证「整个 prefill/define_tools/generate 链路不崩」，不保证「模型一定正确调用了工具」。真正严格的工具调用断言需要固定随机采样、固定模型版本，这在跨环境场景下很难稳定复现。

再看多轮记忆用例：

[test_model_context_memory：tests/test_llm.cpp:256-283](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/tests/test_llm.cpp#L256-L283)

```cpp
std::string msg1 = apply_chat_template({{"user", "My favorite color is blue."}}, {}, true, false);
ctx = model.prefill(msg1, ctx);
ctx = model.generate(ctx, GenerateConfig{}, [](const std::string& token) {});

std::string msg2 = apply_chat_template({{"user", "What is my favorite color?"}}, {}, true, false);
ctx = model.prefill(msg2, ctx);
std::string response;
ctx = model.generate(ctx, GenerateConfig{}, [&response](const std::string& token) {
    response += token;
});
TEST_ASSERT(!response.empty(), "Response should not be empty");
```

这里展示了**多轮对话的标准写法**：第一轮 user 消息 prefill 后 generate（消化陈述），第二轮 user 消息继续在**同一个 ctx** 上 prefill 再 generate。第二轮的 generate 通过 `lambda` 捕获 `response`，把流式输出的 token 逐个拼接成完整回复。唯一的断言是 `!response.empty()`——同样很宽松，只确认「能产出非空文本」，不检查内容是否真的回答了「蓝色」（那需要语义判断，超出单元测试范畴）。

这两个用例共同体现了一个原则：**模型级单元测试只适合做冒烟/不崩验证**，严格的输出正确性应交给离线评测脚本或人工抽检。

#### 4.4.4 代码实践

**实践目标**：在有模型时观察 tool_callback 是否被触发，理解工具调用回调的触发条件；在无模型时则做一次源码阅读型实践。

**操作步骤（无模型环境，源码阅读型）**：

1. 阅读 [test_model_tool_calling（test_llm.cpp:220-254）](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/tests/test_llm.cpp#L220-L254)。
2. 追踪 `tool_called` 这个布尔变量的生命周期：它在第 243 行声明为 false，唯一被置 true 的地方在第 247 行的 `tool_callback` lambda 里。
3. 回顾 [u7-l2](u7-l2-tool-calling.md)：`tool_callback` 只在 `generate` 主循环解析到 `tool_call_id` 与 `tool_call_end_id` 包裹的 JSON 时才被调用。如果模型配置里没有这两个特殊 token（`define_tools` 会降级为 no-op），回调永远不会触发。

**操作步骤（有模型环境，可选）**：

1. 把 `qwen3_0.6b` 放进 `assets/`。
2. 临时把用例末尾的 `return true;` 改成 `TEST_ASSERT(tool_called, "model should call the tool"); return true;`，重新构建运行。
3. 观察是否触发：能否通过，取决于该模型的 model.json 是否配置了 `tool_call_id`/`tool_call_end_id` 以及采样随机性。

**需要观察的现象**：

- 无模型：`(skipped - model not found)`，用例不实际执行。
- 有模型：若模型正确输出工具调用，回调触发；否则（断言被你加上后）用例 FAILED。

**预期结果**：无模型环境一定全绿（跳过）。有模型环境的工具触发行为**待本地验证**——它依赖具体模型配置与采样随机性，不能保证确定性触发。

> 注意：第 2 步的改动是本地实验，练完请还原。

#### 4.4.5 小练习与答案

**练习 1**：`test_model_context_memory` 里第一轮的 `generate` 调用，回调是 `[](const std::string& token) {}`（空操作），为什么还要调一次 generate？

> **答案**：模型在 prefill 完陈述后，需要 generate 一步来「消化」并产出回复、同时把回复对应的 KV cache 写进 ctx。即使回调丢弃输出，generate 仍推进了上下文状态（position_id、KV cache 行数），让第二轮能正确接续。跳过这一步直接 prefill 第二轮，上下文状态会不一致。

**练习 2**：为什么两个模型级用例的断言都这么宽松（一个不查 `tool_called`，另一个只查 `response` 非空）？

> **答案**：模型生成带随机性（采样）、且依赖具体权重版本，严格的输出匹配（如「必须回答 blue」）在不同环境、不同采样种子下很难稳定复现，会让测试时好时坏（flaky）。宽松断言只固化「链路不崩、能产出输出」这一稳定不变量，把语义正确性交给更专门的评测手段，是务实的取舍。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个完整任务：**为 `apply_youtu_chat_template` 新增一个单元测试，走完从写用例到构建通过的全流程**。这是本讲规格指定的实践，也是检验你是否真的掌握测试框架的最佳方式。

### 任务背景

现有 10 个纯逻辑用例**全部**针对 ChatML 模板（`apply_chat_template` 默认走 ChatML），**没有一个**针对 YouTu 模板（`apply_youtu_chat_template`）。这是一个真实的测试缺口——本实践就来补上它。

### 关键事实（已从源码核实）

- YouTu 模板用 `<|User|>`、`<|Assistant|>` 作为消息分隔符（[prompt.cpp:227-248](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/prompt.cpp#L227-L248)），**不使用** ChatML 的 `<|im_start|>`。
- system 消息的内容被**直接前置**到 prompt 开头，**不带任何标签**（[prompt.cpp:204-206](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/prompt.cpp#L204-L206)）。
- `apply_youtu_chat_template` 只有**三个参数**（messages, tools, add_generation_prompt），**没有** `enable_thinking` 参数（[prompt.h:34-38](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/prompt.h#L34-L38)），与 ChatML 版签名不同，调用时不要多传一个参数。

### 操作步骤

1. 打开 `tests/test_llm.cpp`，在任一纯逻辑用例（如 `test_system_prompt`）之后，新增一个用例函数（**示例代码**，非项目原有代码）：

```cpp
// Test: YouTu template (与 ChatML 对照)
bool test_youtu_template() {
    std::vector<Message> messages = {
        {"system", "You are a helpful assistant.", "", {}},
        {"user", "Hello!", "", {}}
    };

    // 注意：apply_youtu_chat_template 只有 3 个参数，没有 enable_thinking
    std::string prompt = apply_youtu_chat_template(messages, {}, true);

    TEST_ASSERT(!prompt.empty(), "YouTu prompt should not be empty");
    TEST_ASSERT(prompt.find("<|User|>") != std::string::npos,
                "YouTu prompt should contain <|User|> tag");
    TEST_ASSERT(prompt.find("<|Assistant|>") != std::string::npos,
                "YouTu prompt should contain <|Assistant|> tag (generation prompt)");
    TEST_ASSERT(prompt.find("You are a helpful assistant.") != std::string::npos,
                "YouTu prompt should contain system content");
    // 反向断言：YouTu 不应出现 ChatML 标签
    TEST_ASSERT(prompt.find("<|im_start|>") == std::string::npos,
                "YouTu prompt should NOT contain ChatML <|im_start|> tag");

    return true;
}
```

2. 在 [`main`（test_llm.cpp:285-307）](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/tests/test_llm.cpp#L285-L307) 的 Unit Tests 段里注册它：

```cpp
runner.add_test("youtu_template", test_youtu_template);
```

3. 构建并运行（这是纯逻辑测试，不需要任何模型）：

```bash
xmake build test_llm
xmake run test_llm
echo "退出码: $?"
```

### 需要观察的现象

- `youtu_template... PASSED`。
- 总数从 12 变成 13：`Total: 13`，`Failed: 0`，退出码 `0`。

### 预期结果

新用例通过，证明 YouTu 模板确实产出 `<|User|>`/`<|Assistant|>` 而非 `<|im_start|>`，system 内容被无标签前置。这同时验证了测试框架（`add_test`/`TEST_ASSERT`/`run_all`）的完整闭环，以及纯逻辑测试无需模型即可运行的设计。

> 说明：以上用例函数与注册行是为本实践撰写的**示例代码**，原仓库 `tests/test_llm.cpp` 中并不存在。如果你要真实运行，需手动加入这两处（这是练习，不在「禁止修改源码」的约束内——但完成后建议还原，以免污染仓库）。

## 6. 本讲小结

- ncnn_llm 没有引入 Google Test 等重型框架，而是在 `tests/test_framework.h` 里**手写了约 60 行的极简测试框架**：`TestRunner` 负责注册/运行/统计，`TEST_ASSERT` 宏负责断言。
- `run_all` 用 `try/catch` 兜底每个用例、失败也继续跑、最后返回退出码（`failed>0?1:0`），使测试结果可接入 CI 与 shell 串联。
- 测试用例分两类：**10 个纯逻辑测试**（prompt 模板、工具 JSON，无需模型，clone 即可全绿）与 **2 个模型级测试**（端到端推理，需 `qwen3_0.6b`）。
- `has_model` 实现「无模型则跳过」：找不到模型时打印 `(skipped)` 并 `return true`，使套件在任何环境都全绿——代价是「跳过」与「真通过」在统计上无法区分。
- 模型级用例的断言刻意宽松（只查不崩、产出非空），因为模型生成带随机性，严格匹配会变成 flaky 测试。
- 新增一个单元测试只需三步：写一个返回 `bool` 的用例函数（内部用 `TEST_ASSERT`）、在 `main` 里 `add_test` 注册、`xmake build test_llm && xmake run test_llm`。

## 7. 下一步学习建议

- 想把工具调用链路彻底搞懂，接着读 [u7-l2 工具调用机制](u7-l2-tool-calling.md) 和 [u7-l3 内置工具、路由与 JSON 工具](u7-l3-builtin-tools-json.md)，再回头看 `test_model_tool_calling` 里的 `tool_callback` 就一目了然。
- 想理解多轮 ctx 如何承载记忆，复习 [u2-l5 推理上下文 ctx 与多轮对话](u2-l5-context-and-multiturn.md)，对照 `test_model_context_memory` 的链式 `ctx = model.prefill(...)` / `ctx = model.generate(...)`。
- 下一讲 [u8-l4 模型导出流程](u8-l4-model-export.md) 会转向 `export/` 下的 Python 脚本，讲清楚「模型权重和 tokenizer 如何变成 `assets/` 下可被本讲测试加载的目录」——那是让模型级测试真正能跑起来的前置环节。
- 如果你要为项目贡献代码，建议把本讲的「纯逻辑优先、模型级宽松」原则应用到自己的测试里：能用纯函数验证的逻辑绝不依赖模型，这样你的测试在 CI 上才会又快又稳。
