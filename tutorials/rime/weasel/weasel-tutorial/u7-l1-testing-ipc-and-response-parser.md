# 测试工程：IPC 与响应解析

> 所属单元：u7 工程实践与二次开发 ｜ 依赖：u2-l5 响应解析、反序列化与动作分发
> 阶段：advanced

## 1. 本讲目标

学完本讲后，你应该能够：

1. 读懂 `test/TestResponseParser` 如何把一段「服务端响应文本」喂给 `weasel::ResponseParser`，并用断言验证 `Context`/`Status`/`commit` 是否被正确填充。
2. 读懂 `test/TestWeaselIPC` 如何用一个进程当 Server、另一个进程当 Client，通过真正的命名管道做端到端 IPC 联调。
3. 说清这两个测试工程的构建方式（`weasel.sln` / MSBuild 与 `xmake.lua` 双轨，且**仅在 Debug 模式下编译**），以及它们各自依赖哪些静态库。
4. 仿照现有用例，独立编写一个新的响应解析断言用例，尤其是针对 `UIStyle` 颜色字段这类「整块 boost 序列化」动作的测试。

> ⚠️ 一个必须先澄清的事实：本系列大纲与本讲规格里把这两个工程称作「Google Test 工程」，但**真实源码并不是 Google Test**。
> - `TestResponseParser` 用的是 **Boost.LightweightTest**（`<boost/detail/lightweight_test.hpp>`，断言宏是 `BOOST_TEST` / `BOOST_TEST_EQ`，末尾 `boost::report_errors()` 汇总）。
> - `TestWeaselIPC` **没有用任何测试框架**，它就是一个带命令行参数的 Win32 控制台程序，靠人工启动两个进程来观察行为。
>
> 本讲一切以真实源码为准，不沿用「Google Test」这个不准确的表述。

## 2. 前置知识

- **响应行协议（来自 u2-l5）**：服务端 `_Respond` 把每次按键的产出编码成多行文本，首行形如 `action=commit,ctx`（声明本次响应激活哪些动作），随后是 `key=value` 正文，单独一行 `.` 表示结束。客户端 `ResponseParser` 逐行解析，按 `key` 的第一段（动作名）派发给对应的 `Deserializer`。
- **懒加载工厂（来自 u2-l5）**：`Deserializer::Initialize` 只默认激活 `action` 分发器；`ActionLoader` 解析 `action=` 行后，才按需 `Require`（实例化）`commit` / `ctx` / `status` / `config` / `style` 等分发器。未在 `action=` 头里声明的动作，其正文行会被静默丢弃。
- **两种正文风格（来自 u2-l4/u2-l5）**：
  - 逐字段文本：`commit=…`、`ctx.preedit=…`、`config.inline_preedit=1`，可读、可手写。
  - 整块 boost 序列化：`style=<archive>`、`ctx.cand=<archive>`，是一段 `boost::archive::text_woarchive` 产出的归档字符串，**不能手工拼写**，只能由程序序列化得到。这一区别是本讲综合实践的关键。
- **IPC 双方（来自 u2-l1～u2-l3）**：`weasel::Client`（前端侧）经命名管道把 `PipeMessage` 发给 `weasel::Server`（服务侧），`Server` 把命令派发给一个 `weasel::RequestHandler` 实现类（生产环境是 `RimeWithWeaselHandler`）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [test/TestResponseParser/TestResponseParser.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestResponseParser/TestResponseParser.cpp) | 响应解析的单元测试主体：4 个 `test_N()` 用例 + `_tmain` 串联 |
| [test/TestWeaselIPC/TestWeaselIPC.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestWeaselIPC/TestWeaselIPC.cpp) | IPC 端到端联调程序：按命令行参数分别扮演 Client / Server |
| [include/ResponseParser.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/ResponseParser.h) / [WeaselIPC/ResponseParser.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/ResponseParser.cpp) | 被测对象：逐行解析响应文本的函数对象 |
| [WeaselIPC/Deserializer.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/Deserializer.cpp) / [WeaselIPC/ActionLoader.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/ActionLoader.cpp) | 工厂注册表与 `action=` 行的懒加载机制（被测对象的依赖） |
| [WeaselIPC/Styler.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/Styler.cpp) | `style` 动作的分发器：用 `text_wiarchive` 反序列化整块 `UIStyle`（综合实践核心） |
| [RimeWithWeasel/RimeWithWeasel.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp) `_Respond` 段 | 服务端如何编码 `style=` / `ctx.cand=` 正文（用来反推测试输入） |
| [include/WeaselIPCData.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h) | `UIStyle` 颜色字段定义（综合实践的断言对象） |
| [xmake.lua](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/xmake.lua) / [test/TestResponseParser/xmake.lua](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestResponseParser/xmake.lua) / [test/TestWeaselIPC/xmake.lua](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestWeaselIPC/xmake.lua) | xmake 构建脚本（仅 Debug 编入测试） |
| [weasel.sln](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/weasel.sln) + 两个 `.vcxproj` | MSBuild 构建路径与工程引用关系 |

## 4. 核心概念与源码讲解

### 4.1 TestResponseParser：响应文本的纯单元测试

#### 4.1.1 概念说明

`TestResponseParser` 是一个**纯单元测试**：不需要启动 Server、不需要命名管道、不需要 librime，只检验「给定一段响应文本，`ResponseParser` 是否正确地把文本翻译成 `Context` / `Status` / `commit`」。它把 IPC 协议里最容易出错的「文本 ↔ 结构体」转换隔离出来单独验证，是回归响应协议时的第一道防线。

它选择的是 **Boost.LightweightTest**——一个只有单头文件、不依赖任何测试 main 入口的极简框架，适合这种「几个独立用例、最后汇总错误数」的场景。断言宏对照如下：

| Boost.LightweightTest 宏 | 含义 | Google Test 类比（仅供对照） |
| --- | --- | --- |
| `BOOST_TEST(cond)` | 条件为真则通过 | `EXPECT_TRUE(cond)` |
| `BOOST_TEST_EQ(a, b)` | 相等则通过，失败打印两值 | `EXPECT_EQ(a, b)` |
| `BOOST_ASSERT(cond)` | 断言（用于前置假设） | `ASSERT_TRUE(cond)` |
| `boost::report_errors()` | 汇总所有失败数，作为进程返回码 | gtest 自动汇总 |

#### 4.1.2 核心流程

每个用例都遵循同一个五步模板：

1. 准备一段宽字符响应文本 `WCHAR resp[] = L"action=…\n…\n"`，并算出长度 `len`。
2. 准备接收容器：`std::wstring commit; weasel::Context ctx; weasel::Status status;`（按需预填一些「旧值」，用来验证解析是否正确覆盖或保留）。
3. 构造解析器 `weasel::ResponseParser parser(&commit, &ctx, &status);`，把容器指针交给它。
4. 调用 `parser(resp, len);`（即 `operator()`）触发逐行解析。
5. 用 `BOOST_TEST*` 断言容器字段是否符合预期。

`_tmain` 只是把 `test_1` 到 `test_4` 顺序跑一遍，最后 `boost::report_errors()` 汇总。注意末尾的 `system("pause")` 让控制台窗口停住以便人工查看——这也说明它是面向「在 Windows 上双击运行」的小工具，而非 CI 里的无头测试。

#### 4.1.3 源码精读

框架头文件与汇总入口（注意是 Boost.LightweightTest，不是 gtest）：

[TestResponseParser.cpp:5](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestResponseParser/TestResponseParser.cpp#L5) 引入 `<boost/detail/lightweight_test.hpp>`；[TestResponseParser.cpp:88-96](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestResponseParser/TestResponseParser.cpp#L88-L96) 是 `_tmain`，依次调用四个用例并 `return boost::report_errors()`。

最简用例 `test_1`：验证「只有 `action=noop`、没有任何正文」时，容器保持空：

[TestResponseParser.cpp:9-19](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestResponseParser/TestResponseParser.cpp#L9-L19)。关键三步：构造 `ResponseParser(&commit, &ctx, &status)`（[L15](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestResponseParser/TestResponseParser.cpp#L15)）→ `parser(resp, len)`（[L16](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestResponseParser/TestResponseParser.cpp#L16)）→ `BOOST_TEST(commit.empty())` 与 `BOOST_TEST(ctx.empty())`（[L17-L18](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestResponseParser/TestResponseParser.cpp#L17-L18)）。

`test_2` 验证 `commit` 动作：响应里 `commit=教這句話上屏=3.14`（注意值里含 `=`，解析器只按**第一个** `=` 拆分，所以后续 `=` 原样保留），同时确认它**没有**误清掉预先设置的 `ctx.aux`：

[TestResponseParser.cpp:21-36](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestResponseParser/TestResponseParser.cpp#L21-L36)。其中 [L29](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestResponseParser/TestResponseParser.cpp#L29) 预填 `ctx.aux.str = L"從前的值"`，[L34](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestResponseParser/TestResponseParser.cpp#L34) 断言它仍被保留——这正是 `commit` 与 `ctx` 两条动作互不干扰的回归点。

`test_4` 是最完整的用例，一次激活两个动作 `action=commit,ctx`，并覆盖候选列表、光标区间、高亮属性：

[TestResponseParser.cpp:55-86](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestResponseParser/TestResponseParser.cpp#L55-L86)。注意其中 `ctx.preedit.cursor=0,3` 被还原成 `TextAttribute{type=HIGHLIGHTED, range=[0,3)}`（[L73-L77](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestResponseParser/TestResponseParser.cpp#L73-L77)），`ctx.cand.length=2` + `ctx.cand.0=…` / `ctx.cand.1=…` 还原成 `CandidateInfo.candies`（[L79-L85](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestResponseParser/TestResponseParser.cpp#L79-L85)）。这条用例几乎是一份「响应行协议」的活文档。

被测对象 `ResponseParser` 的核心：构造时调 `Deserializer::Initialize(this)` 注册全部工厂并默认激活 `action`；`operator()` 用 `wbufferstream` 逐行 `getline`，遇到单独一行 `.` 结束，每行交 `Feed`：

[ResponseParser.cpp:8-36](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/ResponseParser.cpp#L8-L36) 是构造与逐行循环（[L30-L31](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/ResponseParser.cpp#L30-L31) 处理 `.` 终止符）；[ResponseParser.cpp:38-69](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/ResponseParser.cpp#L38-L69) 是 `Feed`：按第一个 `=` 拆 key/value，按 `.` 拆 key 段，用 `key[0]` 在 `deserializers` 表里查分发器并 `Store`（[L59-L68](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/ResponseParser.cpp#L59-L68)）。未激活的动作在 [L61-L63](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/ResponseParser.cpp#L61-L63) 被静默丢弃——这就是为什么每条用例的响应都必须以正确的 `action=` 头开场。

工厂注册表与默认激活逻辑：

[Deserializer.cpp:13-28](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/Deserializer.cpp#L13-L28)。`Define` 登记六个工厂（`action/commit/ctx/status/config/style`，[L18-L23](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/Deserializer.cpp#L18-L23)），随后只 `Require(L"action", pTarget)`（[L27](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/Deserializer.cpp#L27)）。其余动作靠 `ActionLoader::Store` 在解析 `action=` 行时按需 `Require`：[ActionLoader.cpp:17-31](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/ActionLoader.cpp#L17-L31)。

#### 4.1.4 代码实践（源码阅读 + 用例改写）

**实践目标**：把 `test_4` 当作活文档，亲手改一行观察失败信息，从而理解断言是如何与协议绑定的。

**操作步骤**：

1. 打开 [TestResponseParser.cpp:55-86](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestResponseParser/TestResponseParser.cpp#L55-L86)。
2. 把响应文本里的 `ctx.cand.0=候選甲` 改成 `ctx.cand.0=被我篡改`。
3. 把对应断言 [L81](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestResponseParser/TestResponseParser.cpp#L81) 的 `BOOST_TEST(c.candies[0].str == L"候選甲")` 保持不变。
4. 用本讲 4.3 的方式重新编译并运行 `TestResponseParser.exe`。

**需要观察的现象**：控制台会打印一条形如 `test ... failed in ...: c.candies[0].str == L"候選甲" ...` 的失败信息，进程返回非 0（由 `boost::report_errors()` 决定）。

**预期结果**：断言失败 → `report_errors()` 汇总错误数为 1 → 进程退出码非 0。这说明每条 `BOOST_TEST*` 都直接绑死了一行响应文本与一个结构体字段。

> ⚠️ 待本地验证：本实践需要在 Windows + MSVC/xmake 环境按 4.3 节编译后运行；本讲不假装已执行。

#### 4.1.5 小练习与答案

**练习 1**：`test_2` 的响应里 `commit=教這句話上屏=3.14`，为什么值中间的 `=` 没有让解析出错？

**答案**：`Feed` 用 `line.find_first_of(L'=')` 只找**第一个** `=` 作为 key/value 分隔符（[ResponseParser.cpp:47](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/ResponseParser.cpp#L47)），value 取 `sep_pos + 1` 之后**整段**子串（[L53](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/ResponseParser.cpp#L53)），所以后续的 `=3.14` 原样进入 `commit`。

**练习 2**：若把 `test_3` 的首行从 `action=ctx` 改成 `action=commit`，但正文保留 `ctx.preedit=…`，`ctx.preedit.str` 最终会是什么？为什么？

**答案**：会是**构造时的默认值（空）**。因为 `action=commit` 不会激活 `ctx` 分发器（`ActionLoader` 只 `Require` 了 `commit`），`Feed` 在 `deserializers` 表里找不到 `ctx`，正文行在 [ResponseParser.cpp:61-63](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/ResponseParser.cpp#L61-L63) 被静默丢弃。

---

### 4.2 TestWeaselIPC：命名管道的端到端联调

#### 4.2.1 概念说明

`TestResponseParser` 只验证「文本 → 结构体」，但 IPC 的另一半——真正的命名管道往返、`Client` 与 `Server` 的握手、命令派发——它管不到。`TestWeaselIPC` 就是补这一半的：它**用同一个 `.exe` 扮演两个角色**，靠命令行参数切换，让你能在一台机器上手动把整条管道打通。

它不依赖 librime（注释里能看到原本想用 `RimeWithWeaselHandler`，但被换成了一个假处理器），所以它测的是**纯 IPC 骨架**：管道是否连通、`PipeMessage` 是否正确往返、`RequestHandler` 的回调是否被调用、响应正文是否原样回到客户端缓冲。

#### 4.2.2 核心流程

`_tmain` 是一个命令行分发器，按 `argv[1]` 决定身份：

| 调用方式 | 走的函数 | 角色 |
| --- | --- | --- |
| `TestWeaselIPC.exe`（无参） | `client_main` | 客户端：连服务器、发一个 `'a'`、读响应 |
| `TestWeaselIPC.exe /start` | `server_main` | 服务端：建管道、注入假处理器、跑消息循环 |
| `TestWeaselIPC.exe /stop` | 内联 | 客户端：连上后 `ShutdownServer()` 关掉服务端 |
| `TestWeaselIPC.exe /console` | `console_main` | 客户端：交互式，从 `std::cin` 逐字符发键 |

服务端的「假处理器」`TestRequestHandler` 继承 `weasel::RequestHandler`，只实现 `FindSession/AddSession/RemoveSession/ProcessKeyEvent` 四个核心虚函数，并在 `ProcessKeyEvent` 里通过 `eat(...)` 回写一行响应正文 `Greeting=Hello, 小狼毫.`，返回 `TRUE`（表示「吃键」）。客户端用 `GetResponseData(callback)` 把这行响应读进本地缓冲并打印。

典型联调链路：

```
启动：先开一个控制台跑 TestWeaselIPC.exe /start（服务端，阻塞在 server.Run()）
联调：再开一个控制台跑 TestWeaselIPC.exe（客户端 client_main）
  client.Connect()  ──► 命名管道 \\.\pipe\<用户名>\WeaselNamedPipe
  client.StartSession() / Echo()
  client.ProcessKeyEvent(KeyEvent('a', 0)) ──► 管道 ──► Server 派发
        ──► TestRequestHandler::ProcessKeyEvent ──► eat("Greeting=Hello, 小狼毫.\n") ──► 管道回写
  client.GetResponseData(read_buffer) ──► 打印 "Greeting=Hello, 小狼毫."
```

#### 4.2.3 源码精读

命令行分发与 `/stop` 内联逻辑：

[TestWeaselIPC.cpp:22-42](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestWeaselIPC/TestWeaselIPC.cpp#L22-L42)。无参走 `client_main`（[L23-L25](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestWeaselIPC/TestWeaselIPC.cpp#L23-L25)），`/start` 走 `server_main`（[L26-L27](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestWeaselIPC/TestWeaselIPC.cpp#L26-L27)），`/stop` 直接 `client.Connect()` + `client.ShutdownServer()`（[L28-L35](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestWeaselIPC/TestWeaselIPC.cpp#L28-L35)），`/console` 走 `console_main`（[L36-L39](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestWeaselIPC/TestWeaselIPC.cpp#L36-L39)，注意原代码 [L38](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestWeaselIPC/TestWeaselIPC.cpp#L38) 有一行不可达的 `return 0;`，照实说明即可）。

服务端的假处理器——重点看 `ProcessKeyEvent` 如何用 `eat` 闭包回写响应：

[TestWeaselIPC.cpp:131-163](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestWeaselIPC/TestWeaselIPC.cpp#L131-L163)。`eat(std::wstring(L"Greeting=Hello, 小狼毫.\n"))`（[L157](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestWeaselIPC/TestWeaselIPC.cpp#L157)）就是 u2-l3 里讲过的「`eat` 闭包把响应正文写回管道缓冲」；返回 `TRUE`（[L158](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestWeaselIPC/TestWeaselIPC.cpp#L158)）告诉客户端「这个键我吃了」。

服务端主体：初始化 `_Module`、建 `weasel::Server`、注入处理器、`Start()` + `Run()`：

[TestWeaselIPC.cpp:165-182](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestWeaselIPC/TestWeaselIPC.cpp#L165-L182)。注意 [L171-L173](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestWeaselIPC/TestWeaselIPC.cpp#L171-L173) 的注释——原本是用 `RimeWithWeaselHandler`（需要 librime），测试里替换成了轻量的 `TestRequestHandler`，这正是它能脱离 librime 独立编译的原因。

客户端读响应的关键：`GetResponseData` 接受一个回调，回调签名是 `bool(LPWSTR buffer, UINT length)`，测试里用 `std::bind` 把第三个参数 `dest` 绑成局部缓冲 `response`：

[TestWeaselIPC.cpp:54-58](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestWeaselIPC/TestWeaselIPC.cpp#L54-L58) 定义 `read_buffer`；[TestWeaselIPC.cpp:114-124](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestWeaselIPC/TestWeaselIPC.cpp#L114-L124) 是 `client_main` 的发键 + 取响应（[L118-L120](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestWeaselIPC/TestWeaselIPC.cpp#L118-L120) 用 `std::bind` 拼回调）。这段等价于 WeaselTSF 前端在真实流程里调用 `GetResponseData` 的姿势，是理解前端如何「拿到 Server 回写文本」的最小样本。

辅助：`launch_server` 用 `ShellExecute` 自我拉起 `/start`（[TestWeaselIPC.cpp:44-52](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestWeaselIPC/TestWeaselIPC.cpp#L44-L52)），`client_main` 里这行被注释掉了（[L102](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestWeaselIPC/TestWeaselIPC.cpp#L102)），所以默认要你手动开两个控制台。

#### 4.2.4 代码实践（端到端联调）

**实践目标**：在一台 Windows 机器上把 `TestWeaselIPC` 跑成一次真正的「双进程命名管道往返」，亲眼看到服务端假处理器写回的 `Greeting=Hello, 小狼毫.`。

**操作步骤**：

1. 按 4.3 节用 Debug 配置编译，得到 `TestWeaselIPC.exe`。
2. 控制台 A：`TestWeaselIPC.exe /start` → 看到 `handler ctor.` 与 `server running.`，进程阻塞（在 `server.Run()` 的消息循环里）。
3. 控制台 B：`TestWeaselIPC.exe`（无参，跑 `client_main`）。
4. 控制台 B 预期依次输出：`server replies: 1`（`ProcessKeyEvent` 返回 `TRUE` → 吃键）、`get response data: 1`、`buffer reads:` 以及 `Greeting=Hello, 小狼毫.`。
5. 同时控制台 A 会打印 `AddSession: 1`、`ProcessKeyEvent: 1 keycode: 97 mask: 0`（97 是 `'a'`）等诊断行。
6. 收尾：`TestWeaselIPC.exe /stop` 让客户端发 `ShutdownServer()`，控制台 A 打印 `handler dtor:` 后退出。

**需要观察的现象**：客户端的 `server replies` 为 1（非 0）说明键被吃；`buffer reads` 出现 `Greeting=…` 说明 `eat(...)` 写回的正文经管道原样回到客户端。

**预期结果**：两端诊断行与上一致。若 `server replies: 0`，多半是服务端没启动或管道名（含用户名）不匹配。

> ⚠️ 待本地验证：本实践依赖 Windows 与两个进程；若手头没有 Windows 环境，可退化为「源码阅读型实践」——按 4.2.3 的行号，画出 `_tmain` → `client_main` → `ProcessKeyEvent` → `GetResponseData` 的调用序列图，并标注每一步在哪个源码文件。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `TestWeaselIPC` 能在不链接 librime 的情况下编译运行？依据是哪几行？

**答案**：因为 `server_main` 注释掉了 `RimeWithWeaselHandler`，改用自带的 `TestRequestHandler`（[TestWeaselIPC.cpp:171-173](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestWeaselIPC/TestWeaselIPC.cpp#L171-L173)）。`TestRequestHandler` 自行实现了 `RequestHandler` 的虚函数，不调用任何 `rime_api`，所以只依赖 `WeaselIPC` / `WeaselIPCServer` 等静态库即可。

**练习 2**：客户端 `client_main` 里 `GetResponseData` 的回调为什么用 `std::bind` 把 `response` 绑成 `std::ref`，而不是按值传递？

**答案**：`read_buffer` 的第三个参数 `dest` 是输出位（[TestWeaselIPC.cpp:54-58](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestWeaselIPC/TestWeaselIPC.cpp#L54-L58)），需要把读到的内容写回调用方的局部数组 `response`。用 `std::ref` 传引用才能让回调真正写入该数组；若按值传，写入的是副本，调用方拿不到结果。

---

### 4.3 测试工程的构建与运行（vcxproj / xmake 双轨，仅 Debug）

#### 4.3.1 概念说明

Weasel 同时维护两套构建系统（见 u1-l3）：官方的 `weasel.sln` + MSBuild，以及替代的 `xmake.lua`。两个测试工程在两套系统里的接法不同，但有一个共同点：**它们默认只在 Debug 配置下参与构建**——因为测试程序不会随正式安装包发布，Release/LTCG 构建时把它们排除掉以缩短编译时间、避免链接进发布产物。

需要分清两个工程的依赖重量：

- `TestResponseParser` 很轻：只引用 `WeaselIPC` 一个工程（它只用到 `ResponseParser` / `Deserializer` 那一套）。
- `TestWeaselIPC` 很重：同时引用 `WeaselIPC`、`WeaselIPCServer`、`RimeWithWeasel`、`WeaselUI`（因为 `server_main` 用到 `weasel::Server`，且 `#include <RimeWithWeasel.h>`）。

#### 4.3.2 核心流程

**xmake 路径**：根 `xmake.lua` 用 `is_mode("debug")` 守卫，只在 Debug 下 `includes` 两个测试目标：

```lua
if is_mode("debug") then
  includes("test/TestWeaselIPC")
  includes("test/TestResponseParser")
else
  add_cxflags("/GL")
  add_ldflags("/LTCG /INCREMENTAL:NO", {force = true})
end
```

每个测试目标都是 `set_kind("binary")` + `add_rules("subcmd")`（`subcmd` 规则给链接器加 `/SUBSYSTEM:CONSOLE`），并用 `before_build` 把产物放进 `targetdir/<目标名>/` 子目录。

**MSBuild 路径**：`weasel.sln` 里登记了两个 `.vcxproj`，`ConfigurationType=Application`、`SubSystem=Console`，靠 `ProjectReference` 表达依赖，靠 `$(SolutionDir)\include`、`$(BOOST_ROOT)`、`librime\build\lib\Debug|Release` 提供头与库。

#### 4.3.3 源码精读

xmine 根脚本的 Debug 守卫与 `subcmd` 规则：

[xmake.lua:59-70](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/xmake.lua#L59-L70)。`includes("test/...")` 只在 `is_mode("debug")` 为真时执行（[L60-L61](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/xmake.lua#L60-L61)）；`subcmd` 规则在 [L67-L70](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/xmake.lua#L67-L70) 给目标追加 `/SUBSYSTEM:CONSOLE`。

两个测试目标的 xmake 定义（几乎对称）：

[test/TestResponseParser/xmake.lua:1-12](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestResponseParser/xmake.lua#L1-L12) 与 [test/TestWeaselIPC/xmake.lua:1-12](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestWeaselIPC/xmake.lua#L1-L12)。两者都 `add_deps("WeaselIPC", "WeaselIPCServer")`（[TestResponseParser/xmake.lua:4](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestResponseParser/xmake.lua#L4)、[TestWeaselIPC/xmake.lua:4](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestWeaselIPC/xmake.lua#L4)）。

MSBuild 解决方案里的登记：

[weasel.sln:32](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/weasel.sln#L32) 是 `TestWeaselIPC`（GUID `{9C1CC4BA-…}`），[weasel.sln:36](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/weasel.sln#L36) 是 `TestResponseParser`（GUID `{CC642427-…}`）。

两个 `.vcxproj` 的依赖差异（体现「轻 vs 重」）：

- 轻量级 [TestResponseParser.vcxproj:341-346](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestResponseParser/TestResponseParser.vcxproj#L341-L346)：只 `ProjectReference` 了 `WeaselIPC.vcxproj`。
- 重量级 [TestWeaselIPC.vcxproj:350-363](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestWeaselIPC/TestWeaselIPC.vcxproj#L350-L363)：引用了 `RimeWithWeasel`、`WeaselIPCServer`、`WeaselIPC`、`WeaselUI` 四个工程。

两者都 `import ..\..\weasel.props`（[TestResponseParser.vcxproj:42](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestResponseParser/TestResponseParser.vcxproj#L42)）拿版本号宏与公共设置，`SubSystem=Console`（[TestResponseParser.vcxproj:161](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestResponseParser/TestResponseParser.vcxproj#L161) 一类条目）。

#### 4.3.4 代码实践（编译并运行测试）

**实践目标**：用 xmake 的 Debug 配置把两个测试工程编出来，并运行 `TestResponseParser.exe` 看汇总结果。

**操作步骤**：

1. 按 u1-l3 设置 `BOOST_ROOT`、准备好 `librime` 与 `env.bat`。
2. 编译（Debug）：`xmake f -m debug && xmake`。注意必须是 `-m debug`，否则根 `xmake.lua` 的 [L59-L65](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/xmake.lua#L59-L65) 根本不会包含测试目标。
3. 运行：找到 `build/<plat>/arch/debug/TestResponseParser/TestResponseParser.exe`，直接双击或在控制台执行；控制台会因 `system("pause")` 停住。
4. （MSBuild 路线）在 VS 里用 `Debug | Win32`/`Debug | x64` 编译 `TestResponseParser` 工程，产物在 `msbuild\Debug\<Platform>\` 下（见 [TestResponseParser.vcxproj:116-134](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestResponseParser/TestResponseParser.vcxproj#L116-L134) 的 `OutDir`）。

**需要观察的现象**：`TestResponseParser.exe` 正常时控制台没有 `failed` 行，按任意键后退出码为 0；若故意改坏（见 4.1.4），退出码非 0。

**预期结果**：原始未改动的 4 条用例全部通过，`report_errors()` 返回 0。

> ⚠️ 待本地验证：具体输出目录名随 xmake 版本与平台而变，以本地 `xmake -v` 实际产物为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么用 `xmake f -m release` 编出来的产物里找不到 `TestResponseParser.exe`？

**答案**：因为根 `xmake.lua` 在 [L59-L65](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/xmake.lua#L59-L65) 用 `if is_mode("debug") then includes(...)` 守卫，非 Debug 模式根本不把测试目标纳入构建。

**练习 2**：`TestResponseParser.vcxproj` 与 `TestWeaselIPC.vcxproj` 的 `ProjectReference` 数量不同，背后的原因是什么？

**答案**：`TestResponseParser` 只用到 `ResponseParser`/`Deserializer`（属 `WeaselIPC`），所以只引用一个工程；`TestWeaselIPC` 要扮演 `weasel::Server`（属 `WeaselIPCServer`）并 `#include <RimeWithWeasel.h>`（属 `RimeWithWeasel`），还牵连 `WeaselUI`，故引用四个工程。

## 5. 综合实践：为 UIStyle 颜色字段写一个响应解析断言

**任务背景**：本讲规格要求的实践是「参考 `TestResponseParser`，为某一类 `UIStyle` 字段（如颜色）编写一个新的断言用例骨架，并说明需要构造的输入响应文本」。这里有一个**关键陷阱**必须先想清楚——它正是 u2-l4/u2-l5 强调过的两种正文风格的差异。

**先想清楚「输入响应文本」长什么样**：

- 像 `commit=`、`ctx.preedit=`、`config.inline_preedit=` 这类动作是**逐字段文本**，你可以直接手写 `config.inline_preedit=1`。
- 但 `style=` 不是！服务端在 `_Respond` 里是这样产生 `style=` 行的：

```cpp
// RimeWithWeasel.cpp:903-911  仅在 __synced 为假时整块序列化
if (!session_status.__synced) {
  std::wstringstream ss;
  boost::archive::text_woarchive oa(ss);
  oa << session_status.style;          // 整个 UIStyle 序列化进一个归档
  actions.push_back("style");
  body.append(L"style=").append(ss.str()).append(L"\n");
  session_status.__synced = true;
}
```

  客户端的 `Styler::Store` 则用 `text_wiarchive` 把这段归档**整体**反序列化回 `UIStyle`（[Styler.cpp:11-21](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/Styler.cpp#L11-L21)，`text_wiarchive` 见 [L18](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/Styler.cpp#L18)）。

  归档字符串形如 `22 serialization::archive 14 0 0 0 0 …`，**不可能手工拼写**。所以测颜色字段的正确姿势是「往返（round-trip）」：自己构造一个带颜色值的 `UIStyle`，序列化得到 `style=` 的正文，再喂给带 `UIStyle*` 的 `ResponseParser`，断言颜色被原样还原。

**`UIStyle` 颜色字段**（都是 `int`，存放 ABGR/COLORREF）来自 [WeaselIPCData.h:267-289](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L267-L289)，例如 `text_color`、`candidate_text_color`、`candidate_back_color`、`hilited_candidate_back_color`、`hilited_mark_color` 等，构造函数里默认全 0（[WeaselIPCData.h:340-356](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L340-L356)）。

**操作步骤**：

1. 在 `TestResponseParser.cpp` 里仿照 `test_4` 新增一个 `test_style_colors()`，并在 `_tmain` 里调用它。
2. 按下面骨架填写（**示例代码，非仓库原有**）：

```cpp
// 示例代码：新增到 TestResponseParser.cpp
#include <sstream>
#include <boost/archive/text_woarchive.hpp>  // 与服务端 _Respond 一致的写出归档
#include <boost/archive/text_wiarchive.hpp>  // ResponseParser 内部用到的读入归档

void test_style_colors() {
  // 1) 构造一个带颜色值的 UIStyle（ABGR 整数）
  weasel::UIStyle src;
  src.text_color = 0x000000;                     // 文本色：黑
  src.candidate_back_color = 0xFFFFFF;           // 候选背景：白
  src.hilited_candidate_back_color = 0x0000FF;   // 高亮候选背景：蓝

  // 2) 序列化成 style= 行的正文（与 RimeWithWeasel.cpp:903-909 同款姿势）
  std::wstringstream ss;
  {
    boost::archive::text_woarchive oa(ss);
    oa << src;
  }
  const std::wstring blob = ss.str();

  // 3) 拼出完整响应：首行 action=style 激活 Styler，次行 style=<归档>
  //    注意 ResponseParser::operator()(LPWSTR, UINT) 需要可写缓冲，故拷进数组
  std::wstring resp = L"action=style\nstyle=" + blob + L"\n";
  std::vector<WCHAR> buf(resp.begin(), resp.end());
  buf.push_back(L'\0');
  const DWORD len = (DWORD)resp.size();

  // 4) 用「带 UIStyle*」的 ResponseParser 解析（第 5 个参数传 &style）
  std::wstring commit;
  weasel::Context ctx;
  weasel::Status status;
  weasel::UIStyle style;      // 默认全 0
  weasel::ResponseParser parser(&commit, &ctx, &status, nullptr, &style);
  parser(buf.data(), len);

  // 5) 断言三个颜色字段被原样还原（往返一致）
  BOOST_TEST_EQ(src.text_color, style.text_color);
  BOOST_TEST_EQ(src.candidate_back_color, style.candidate_back_color);
  BOOST_TEST_EQ(src.hilited_candidate_back_color,
                style.hilited_candidate_back_color);
}
```

3. 用 4.3 的 Debug 方式编译运行。

**需要观察的现象**：三条 `BOOST_TEST_EQ` 全部通过，`report_errors()` 返回 0。

**预期结果**：颜色字段经 `text_woarchive` 序列化 → 经命名管道语义（这里直接用字符串模拟）→ 经 `Styler` 的 `text_wiarchive` 反序列化后，数值完全一致。

**延伸思考（可写入小练习）**：

- 若把响应首行写成 `action=ctx` 但保留 `style=<blob>` 正文，`style.text_color` 会是什么？为什么？（答：默认 0，因为 `style` 分发器未被激活，正文被静默丢弃。）
- 若故意把 `blob` 截断一半（破坏归档），会发生什么？（答：`Styler::Store` 里 `TryDeserialize` 会捕获 `archive_exception` 并弹 `MessageBox`——见 [Deserializer.h:8-16](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/Deserializer.h#L8-L16)，这正是 u2-l4 提到的「版本不兼容兜底弹框」。）

> ⚠️ 待本地验证：归档字符串的具体内容随 boost 版本与 `UIStyle` 字段集变化，无法在此给出定值；正确性以「序列化 → 反序列化往返相等」为准，而不是比对固定字符串。

## 6. 本讲小结

- `test/TestResponseParser` 是**纯单元测试**，用的是 **Boost.LightweightTest**（`BOOST_TEST*` + `boost::report_errors()`），不是 Google Test；它把响应行协议的「文本 ↔ 结构体」转换隔离出来单独验证。
- 每个用例都是「拼一段响应文本 → 喂给 `ResponseParser(&commit, &ctx, &status)` → 断言字段」的五步模板；`test_4` 几乎是一份响应协议活文档。
- `test/TestWeaselIPC` 是**端到端联调程序**，没有测试框架，靠命令行参数（无参 / `/start` / `/stop` / `/console`）让同一个 `.exe` 扮演 Client/Server，用自带的 `TestRequestHandler` 脱离 librime 验证命名管道往返。
- 两个工程都只在 **Debug 配置**下参与构建：xmake 用 `is_mode("debug")` 守卫，MSBuild 靠 `.vcxproj` 的 `ProjectReference`（`TestResponseParser` 只引 `WeaselIPC`，`TestWeaselIPC` 引四个工程）。
- 写新用例时要区分两种正文：`commit/ctx/config` 是可手写的逐字段文本；`style/ctx.cand` 是 boost 归档，**必须用「构造 → 序列化 → 反序列化」的往返方式**来测，不能手写其输入文本。

## 7. 下一步学习建议

- **继续 u7 单元**：接下来 u7-l2 会讲 `WeaselUtility`/`WeaselConstants`/`KeyEvent` 等公共工具与按键映射，可结合 `TestWeaselIPC` 里 `KeyEvent(ch, 0)` 的构造理解按键结构。
- **想做集成测试扩展**：参考 `TestWeaselIPC` 的 `TestRequestHandler` 写法，你可以注入更复杂的假处理器来回归某条 IPC 命令链；这正是 u7-l4「扩展点」要讨论的方向。
- **想做协议级回归**：把综合实践的 `test_style_colors` 思路推广到 `ctx.cand=`（同样走 boost 归档），为新加的 `UIStyle`/`CandidateInfo` 字段建立往返回归，避免 u2-l4 提到的「字段顺序即协议」破坏。
- **深入被测对象**：若要给 `ResponseParser` 加全新动作，回到 u2-l5 复习 `Define`/`Require`/`ActionLoader` 三步注册法，再回到本讲确认新用例的 `action=` 头是否正确激活了它。
