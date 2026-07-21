# 响应解析、反序列化与动作分发

## 1. 本讲目标

本讲是 IPC 章节的收口。在前几讲里，我们已经知道：

- 服务端 `WeaselServer` 通过命名管道把一个 `DWORD` 返回值 + 一段文本「正文」回写给客户端（见 u2-l2、u2-l3）。
- 这段正文里装着 `Context`、`Status`、`Config`、`UIStyle` 等数据结构（见 u2-l4）。

但「正文」并不是一段二进制 blob，而是一行行人类可读的文本协议。客户端拿到这段文本后，**谁来把它翻译回 C++ 结构体？** 答案就是本讲的主角——`ResponseParser` 及其背后的一套「动作分发」机制。

学完本讲，你应当能够：

1. 读懂 `ResponseParser` 的「行协议」：每行 `key=value`、以 `.` 结束、以 `#` 注释。
2. 说清 `Deserializer` / `ActionLoader` 的注册与按需激活（lazy factory）机制，知道一行响应是如何被路由到某个 `Store` 方法的。
3. 区分三类更新动作——逐字段文本更新（`ContextUpdater`/`StatusUpdater`/`Configurator`）、单值上屏（`Committer`）、整块 boost 反序列化（`Styler`）——并理解它们各自的适用场景。
4. 仿照现有代码，独立编写一个新的 `Deserializer` 子类，并让它被服务端响应真正触发。

---

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（来自前置讲义）：

- **命名管道往返模型**（u2-l2）：客户端 `Transact(msg)` = `_Ensure` + `_Send` + `_ReceiveResponse`；响应区先是一个定长返回值，后面跟着变长正文，正文就放在共享缓冲区 `buffer` 里。
- **Client/Server/RequestHandler 三层抽象**（u2-l3）：服务端用 `EatLine` 回调（`std::function<bool(std::wstring&)>`）把正文逐块写回管道；客户端用 `ResponseHandler` 回调（`std::function<bool(LPWSTR, DWORD)>`）接收正文。
- **IPC 数据契约**（u2-l4）：`Context`（preedit/aux/cinfo）、`Status`、`Config`、`UIStyle` 是前后端共享的数据结构，其中 `UIStyle`、`CandidateInfo` 用 boost 序列化整体传输，其余字段走文本协议。

如果你对上面任意一项还不熟悉，建议先回看对应讲义。本讲默认你已经知道「正文是一段文本」，而要解决「这段文本如何被解析」。

补充一个 C++ 小知识：**函数对象（functor）**。一个重载了 `operator()` 的类对象，可以像函数一样「调用」。`ResponseParser` 就重载了 `operator()(LPWSTR, UINT)`，所以它的实例能直接作为 `ResponseHandler` 回调传给管道层。

---

## 3. 本讲源码地图

本讲涉及的源码集中在 `WeaselIPC` 子工程与它的公共头：

| 文件 | 作用 |
| --- | --- |
| [include/ResponseParser.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/ResponseParser.h) | `ResponseParser` 结构体声明：持有 5 个目标指针 + 一张 deserializer 表，重载 `operator()` 作为回调。 |
| [WeaselIPC/ResponseParser.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/ResponseParser.cpp) | 逐行读取缓冲、按 `.` 结束、`Feed` 拆分 `key=value` 并派发。 |
| [WeaselIPC/Deserializer.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/Deserializer.h) | 抽象基类 `Deserializer`：`Store` 虚函数、静态工厂表 `s_factories`、`Define/Require/Initialize`。 |
| [WeaselIPC/Deserializer.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/Deserializer.cpp) | 注册 6 个工厂，默认激活 `action`。 |
| [WeaselIPC/ActionLoader.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/ActionLoader.h) / [.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/ActionLoader.cpp) | 解析 `action=a,b,c` 头，按需 `Require` 出对应的 deserializer。 |
| [WeaselIPC/ContextUpdater.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/ContextUpdater.cpp) | `ContextUpdater`（ctx）与 `StatusUpdater`（status）两个动作，逐字段更新。 |
| [WeaselIPC/Committer.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/Committer.cpp) | `Committer`（commit）动作：单值写入上屏串。 |
| [WeaselIPC/Styler.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/Styler.cpp) | `Styler`（style）动作：整块 boost 反序列化 `UIStyle`。 |
| [WeaselIPC/Configurator.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/Configurator.cpp) | `Configurator`（config）动作：目前只更新 `inline_preedit`。 |
| [RimeWithWeasel/RimeWithWeasel.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp) | 服务端 `_Respond` 拼装这段文本的「源头」，理解它有助你反向验证协议。 |
| [test/TestResponseParser/TestResponseParser.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestResponseParser/TestResponseParser.cpp) | 协议的「活文档」：4 个用例展示了真实响应文本长什么样。 |

---

## 4. 核心概念与源码讲解

### 4.1 ResponseParser 行协议

#### 4.1.1 概念说明

`ResponseParser` 是客户端一侧的「翻译官」。管道层把整段响应正文（一个 `wchar_t` 缓冲区）交给它，它要做的只有一件事：**把文本变回结构体**。

它采用了一种极其朴素、人类可读的「行协议」：

- 整段响应由若干行组成，每行以 `\n` 结尾。
- 每行形如 `key=value`，其中 `key` 用 `.` 分段（例如 `ctx.preedit.cursor`）。
- 空行被忽略；以 `#` 开头的行是注释。
- 单独一行 `.` 表示「响应结束」。

这套协议最大的好处是**可增量、可调试**：服务端只发变化了的字段，客户端也只更新对应字段；出问题时把缓冲打印出来就能肉眼读。

#### 4.1.2 核心流程

`ResponseParser` 是一个函数对象，被管道层这样调用（伪代码）：

```
buffer = "...响应正文 wchar_t 缓冲..."
length = 缓冲区可容纳的 wchar_t 数
ResponseParser parser(&commit, &context, &status, &config, &style);
parser(buffer, length);     // 触发 operator()
```

`operator()` 的执行过程：

```
用 wbufferstream 包裹 buffer
while (还能读):
    getline 读一行 -> line
    若流已坏 -> return false
    若 line == "."  -> break（响应结束）
    Feed(line)                // 交给单行解析
return 流状态
```

`Feed(line)` 的执行过程：

```
若 line 为空 或 以 '#' 开头 -> 忽略
找到第一个 '=' 的位置 sep_pos
    没有 '=' -> 忽略
key   = line[0 .. sep_pos)        // '=' 左边
value = line[sep_pos+1 .. )       // '=' 右边
把 key 用 '.' 切成数组（KeyType）
    key[0] = action（动作类型）
在 deserializers 表里查 key[0]
    查不到 -> 忽略（该动作未激活）
    查到   -> 调用 p->Store(key, value)
```

一句话：**先按行切，再按 `=` 切成 key/value，key 的第一段决定派发给谁。**

#### 4.1.3 源码精读

构造函数把 5 个目标指针存下来，并立即调用 `Deserializer::Initialize(this)` 初始化分发器表（详见 4.2）：

[WeaselIPC/ResponseParser.cpp:8-19](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/ResponseParser.cpp#L8-L19) —— 构造函数：保存 `p_commit/p_context/p_status/p_config/p_style` 五个输出指针，并 `Deserializer::Initialize(this)`。

`operator()` 用 `wbufferstream` 在裸缓冲上做行读取，遇到单独的 `.` 即终止：

[WeaselIPC/ResponseParser.cpp:21-36](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/ResponseParser.cpp#L21-L36) —— 行循环主体：`getline` 逐行读，`line == L"."` 时 `break`，其余交给 `Feed`。

`Feed` 是协议解析的核心：跳过空行/注释、按 `=` 拆 key/value、按 `.` 拆 key、用 `key[0]` 查表派发：

[WeaselIPC/ResponseParser.cpp:38-69](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/ResponseParser.cpp#L38-L69) —— `Feed`：注意 `line.find_first_of(L'#') == 0` 只过滤「整行注释」，`split(key, ..., L".")` 把 key 切段，`key[0]` 作为动作名查 `deserializers`，命中后 `p->Store(key, value)`。

呼应服务端：这段文本是谁写出来的？是 [RimeWithWeasel/RimeWithWeasel.cpp:738-935](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L738-L935) 的 `_Respond`。它在最后拼出 `action=...\n` 头部，再追加 `body`，最后 `body.append(L".\n")` 作为结束符，并通过 `eat` 回调分两批写回管道：

[RimeWithWeasel/RimeWithWeasel.cpp:913-932](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L913-L932) —— 先 `eat(header)`（含 `action=` 摘要行），再 `body.append(L".\n")` 并 `eat(body)`。`eat` 即 u2-l3 讲过的 `EatLine` 回调，最终被写进客户端读取的那个缓冲。

#### 4.1.4 代码实践

**实践目标**：用测试用例反向验证你对行协议的理解。

**操作步骤**：

1. 打开 [test/TestResponseParser/TestResponseParser.cpp:55-86](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestResponseParser/TestResponseParser.cpp#L55-L86) 的 `test_4`。
2. 阅读它喂给 `parser` 的响应文本，**先不看断言**，自己用一张表写下每一行会被 `Feed` 拆成什么 `key`/`value`，以及会派发给哪个 deserializer。
3. 再对照文末断言，看你的推导是否一致。

**需要观察的现象**：例如 `ctx.preedit.cursor=0,3` 这一行，`key` 应被切成 `["ctx","preedit","cursor"]`、`value="0,3"`，派发给 `ContextUpdater`，并最终在 `ctx.preedit.attributes[0]` 生成一个 `HIGHLIGHTED` 属性。

**预期结果**：断言要求 `attr0.range.start == 0`、`attr0.range.end == 3`、`c.candies.size() == 2`、`c.highlighted == 1`，与你手工拆解一致。**待本地验证**：该测试工程依赖 Windows + boost，若本地无环境，可仅做纸面推导。

#### 4.1.5 小练习与答案

**练习 1**：若服务端某行写成 `ctx.preedit=`（value 为空），`Feed` 会怎么处理？

> **答案**：`Feed` 照常拆分，`key=["ctx","preedit"]`、`value=""`，仍派发给 `ContextUpdater::Store`；后者进入 `_StoreText`，由于 `k.size()==2`，会执行 `target.clear()` 并把 `str` 设为 `unescape_string("")`（即清空写作串）。也就是说「空 value」是合法的「清空」语义。

**练习 2**：为什么用 `.` 单独一行作为结束符，而不是靠缓冲长度？

> **答案**：因为同一个缓冲可能比一次响应的正文长（残留旧数据），也可能被分多次 `eat` 写入。一个显式的终止符让解析器以「逻辑边界」而非「物理边界」结束，更健壮、也便于人类阅读调试。

---

### 4.2 ActionLoader / Deserializer 注册与分发机制

#### 4.2.1 概念说明

`Feed` 用 `key[0]` 在 `deserializers` 表里查派发对象。这张表里装的是什么？是 `Deserializer` 的子类实例（`shared_ptr`）。但这里有一个巧妙设计——**这张表不是一开始就装满的，而是按需增长**。

原因：一次响应里可能只涉及 `commit`，也可能涉及 `ctx`+`status`+`style`。如果一开始就把所有 deserializer 都实例化，既浪费又会误更新没出现在响应里的字段。于是 Weasel 用了两层机制：

- **工厂表 `s_factories`**：静态的「动作名 → 工厂函数」映射，进程级单例，只注册不实例化。
- **实例表 `deserializers`**：每个 `ResponseParser` 自己的「动作名 → 实例」映射，按需填充。

负责「按需填充」的就是 `ActionLoader`：它专门处理响应的第一行 `action=a,b,c`，把逗号分隔的每个名字 `Require` 出来（实例化并塞进实例表）。之后真正的数据行 `ctx.preedit=...` 才能在表里找到 `ContextUpdater`。

这是一种**懒加载工厂（lazy factory）+ 自描述协议**的组合：响应自己声明「我这批里有哪些动作」，解析器据此激活对应的处理器。

#### 4.2.2 核心流程

整个注册与分发流程：

```
[进程启动一次]
Deserializer::Initialize(parser):
    若 s_factories 为空:
        Define("action",  ActionLoader::Create)
        Define("commit",  Committer::Create)
        Define("ctx",     ContextUpdater::Create)
        Define("status",  StatusUpdater::Create)
        Define("config",  Configurator::Create)
        Define("style",   Styler::Create)
    Require("action", parser)        # 默认只激活 ActionLoader

[每次响应]
Feed("action=ctx,commit"):
    -> ActionLoader::Store(key=["action"], value="ctx,commit")
        -> split value by ','  => ["ctx","commit"]
        -> 对每个名字调用 Require(name)
            -> Require 在 s_factories 找工厂 -> 调工厂 new 出实例
            -> parser.deserializers[name] = 实例

Feed("ctx.preedit=你好"):
    key[0]="ctx" -> 在 deserializers 命中 ContextUpdater
    -> ContextUpdater::Store(["ctx","preedit"], "你好")
```

关键点：

- `Define` 只登记工厂函数，`Require` 才真正 `new` 对象。
- 同一个 `ResponseParser` 内，同一个动作只 `Require` 一次（重复 `Require` 会覆盖实例，但 `action=` 头一般只出现一次）。
- `ActionLoader` 自己也是一个 `Deserializer`（处理 `action` 这个 key），它是「分发器的分发器」。

#### 4.2.3 源码精读

抽象基类 `Deserializer` 定义了三件套：`Store` 虚函数、`Factory` 类型、静态工厂表 `s_factories`：

[WeaselIPC/Deserializer.h:17-36](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/Deserializer.h#L17-L36) —— `Deserializer` 基类：`Store` 默认空实现（子类覆盖）；`Factory` 是 `std::function<Ptr(ResponseParser*)>`；`s_factories` 是私有的进程级静态表。

`Initialize` 注册 6 个工厂，并默认 `Require("action")`：

[WeaselIPC/Deserializer.cpp:13-28](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/Deserializer.cpp#L13-L28) —— 注意第 18–23 行的 6 个 `Define`，以及第 27 行 `Require(L"action", pTarget)`：这是唯一在构造时就激活的动作，所以 `action=` 头总能被处理。

`Require` 是「按名实例化」的核心：

[WeaselIPC/Deserializer.cpp:35-50](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/Deserializer.cpp#L35-L50) —— 在 `s_factories` 查工厂，调 `factory(pTarget)` 造实例，写入 `pTarget->deserializers[action]`。查不到返回 `false`（未知动作）。

`ActionLoader::Store` 解析 `action=` 头并触发一批 `Require`：

[WeaselIPC/ActionLoader.cpp:17-31](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/ActionLoader.cpp#L17-L31) —— 仅当 `key.size()==1`（即裸 `action=...`，没有 `action.xxx` 子键）时处理；把 value 按 `,` 切开，对每个动作名调 `Deserializer::Require`。

#### 4.2.4 代码实践

**实践目标**：跟踪一次响应里「分发器的分发器」是如何连锁激活其它分发器的。

**操作步骤**：

1. 想象客户端收到如下响应（取自服务端 `_Respond` 的真实输出格式）：

   ```
   action=ctx,status
   ctx.preedit=ni'hao
   status.composing=1
   .
   ```

2. 在纸上画两张表：`s_factories`（6 项，静态）和 `parser.deserializers`（动态，初始只有 `action`）。
3. 逐行模拟 `Feed`，记录 `parser.deserializers` 在每一步后多了哪些条目。

**需要观察的现象**：

- 处理完第 1 行 `action=ctx,status` 后，`deserializers` 应从 `{action}` 增长为 `{action, ctx, status}`。
- 第 2、3 行才能在表里分别命中 `ContextUpdater` 与 `StatusUpdater`。
- 若把第 1 行改成 `action=noop`（见 [RimeWithWeasel/RimeWithWeasel.cpp:916](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L916)），则 `ctx`/`status` 永不被激活，后续 `ctx.*`/`status.*` 行会被 `Feed` 静默丢弃。

**预期结果**：你会直观看到「没有在 `action=` 头里声明的动作，其数据行不会被解析」这一懒加载特性。这是一个纯纸面推导任务，**待本地验证**的部分仅在你想用 `TestResponseParser` 工程跑真实断言时才需要 Windows 环境。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Initialize` 里只 `Require("action")`，而不是把 6 个动作全 `Require` 一遍？

> **答案**：为了避免「过度激活」。若全激活，则即便响应里没出现 `style=`，`Styler` 实例也已存在——虽然它没被调用不会误改数据，但会无谓占用对象、且让「这批响应到底更新了什么」的语义变模糊。懒加载让激活集合与响应内容严格对应。

**练习 2**：如果服务端发来的 `action=` 头里有个拼写错误 `actoin=ctx`，会发生什么？

> **答案**：`key[0]="actoin"` 在 `deserializers` 表里查不到（表里只有 `action`），`Feed` 直接 `return` 忽略该行。更糟的是：真正的动作声明没被解析，`ctx` 不会被激活，后续所有 `ctx.*` 行也都被忽略——客户端这次按键拿不到任何上下文更新。这也是为什么 `action=` 头是整套协议的「总开关」。

---

### 4.3 ContextUpdater / Committer / Styler：三类更新动作

#### 4.3.1 概念说明

被激活的 deserializer 各司其职。按「如何把 value 写进结构体」来分，共有三类风格，理解这三类就掌握了全部动作的写法：

| 风格 | 代表动作 | 写入方式 | 适用场景 |
| --- | --- | --- | --- |
| **逐字段文本** | `ContextUpdater`(ctx)、`StatusUpdater`(status)、`Configurator`(config) | 用 `key` 的后续段（`key[1]`、`key[2]`）定位到某个字段，手动赋值 | 字段多、每次只变一两个、需要细粒度增量 |
| **单值上屏** | `Committer`(commit) | `key.size()==1` 时把 value（经 `unescape`）整体赋给 `p_commit` | 只有一个字符串：本次要上屏的最终文字 |
| **整块反序列化** | `Styler`(style) | 把 value 当成 boost 文本归档，一次反序列化整个 `UIStyle` | 字段极多（约 80 个）、整体同步、版本兼容由 boost 负责 |

之所以有三种风格并存，是因为「字段数量」与「更新频率」不同：`Status` 的几个布尔位适合逐字段；`commit` 只有一个串；而 `UIStyle` 字段太多，逐字段写协议行会爆炸，干脆复用 u2-l4 讲过的 boost 序列化整体传输。

#### 4.3.2 核心流程

三类动作的 `Store` 行为对比（伪代码）：

```
# Committer：单值
Store(key, value):
    if p_commit 且 key.size()==1:
        *p_commit = unescape_string(value)   # 例如 commit=你好

# ContextUpdater：逐字段，按 key[1] 分支
Store(key, value):
    if key[1]=="preedit": _StoreText(preedit, key, value)
    if key[1]=="aux":     _StoreText(aux,     key, value)
    if key[1]=="cand":    _StoreCand(key, value)   # boost 整块反序列化 CandidateInfo

_StoreText(target, key, value):
    if key.size()==2:               # ctx.preedit=...
        target.clear(); target.str = unescape(value)
    if key.size()==3 且 key[2]=="cursor":   # ctx.preedit.cursor=start,end,cursor
        追加一个 HIGHLIGHTED 的 TextAttribute

# StatusUpdater：逐字段布尔/字符串
Store(key, value):
    bool_value = (value 非空 且 != "0")
    按 key[1] ∈ {schema_id, ascii_mode, composing, disabled, full_shape} 赋值

# Styler：整块 boost 反序列化
Store(key, value):
    用 text_wiarchive 包裹 value
    TryDeserialize(ia, *p_style)     # 一次性还原整个 UIStyle
```

注意一个跨行状态细节：`ContextUpdater` 在同一次响应里会先收到 `ctx.preedit=...`（清空并设串），再收到 `ctx.preedit.cursor=...`（追加高亮属性）。因为 deserializer 实例在整个响应期间存活于 `deserializers` 表，这种「同一目标、多行累积」才成立。

另一个细节：`ctx.cand` 和 `style` 都用 boost，但来源不同——`ctx.cand` 的 value 是服务端用 `text_woarchive` 序列化 `CandidateInfo` 得到的（见 [RimeWithWeasel/RimeWithWeasel.cpp:884-892](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L884-L892)），`style` 同理（[L902-L909](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L902-L909)）。反序列化用 `text_wiarchive`，正好与 u2-l4 讲的「`ar & x` 双向运算符」对应。

#### 4.3.3 源码精读

`Committer` 最简单，是「单值上屏」的范本：

[WeaselIPC/Committer.cpp:16-23](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/Committer.cpp#L16-L23) —— 只有 `key.size()==1` 时处理，把 `unescape_string(value)` 赋给 `*p_commit`。`unescape_string` 与服务端的 `escape_string` 配对，处理换行、`=` 等特殊字符（这也是测试里 `commit=教這句話上屏=3.14` 能正确还原含 `=` 的串的原因）。

`ContextUpdater` 展示「逐字段 + 嵌套 boost」的混合风格：

[WeaselIPC/ContextUpdater.cpp:21-40](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/ContextUpdater.cpp#L21-L40) —— `Store` 按 `key[1]` 分派到 `preedit`/`aux`/`cand`。

[WeaselIPC/ContextUpdater.cpp:42-68](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/ContextUpdater.cpp#L42-L68) —— `_StoreText`：`k.size()==2` 时 `clear()` 再设 `str`；`k.size()==3` 且 `k[2]=="cursor"` 时解析 `start,end,cursor` 三段整数为一个 `HIGHLIGHTED` 属性并 `push_back`。

[WeaselIPC/ContextUpdater.cpp:70-84](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/ContextUpdater.cpp#L70-L84) —— `_StoreCand`：用 `text_wiarchive` 反序列化整个 `CandidateInfo`，再对 `candies/labels/comments` 三组字符串分别 `unescape_string`（因为序列化前服务端对它们做过 `escape_string`）。

`StatusUpdater`（与 `ContextUpdater` 同文件）是「逐字段布尔」的范本：

[WeaselIPC/ContextUpdater.cpp:96-127](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/ContextUpdater.cpp#L96-L127) —— 注意 `bool_value = (!value.empty() && value != L"0")` 这一行统一的「真值判定」；`schema_id` 例外，直接存字符串。它注册的工厂名是 `"status"`（见 [Deserializer.cpp:21](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/Deserializer.cpp#L21)），但实现类放在 `ContextUpdater.cpp` 里——读代码时别找错文件。

`Styler` 是「整块反序列化」的范本，最短却最「重」：

[WeaselIPC/Styler.cpp:11-21](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/Styler.cpp#L11-L21) —— 把 value 当 boost 文本归档，`TryDeserialize(ia, sty)` 一次还原整个 `UIStyle`。

`TryDeserialize` 是一个容错包装，反序列化失败时弹框而不是崩溃：

[WeaselIPC/Deserializer.h:7-16](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/Deserializer.h#L7-L16) —— 捕获 `boost::archive::archive_exception`，用 `MessageBoxA` 报错。这正是 u2-l4 提到的「版本不一致由 TryDeserialize 兜底为弹框」的落点。

`Configurator` 目前只有一个字段，是「逐字段」风格的极简版：

[WeaselIPC/Configurator.cpp:15-23](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/Configurator.cpp#L15-L23) —— 只识别 `config.inline_preedit`。

最后看一眼「真正调用 `ResponseParser`」的现场，把全链路闭合。前端在每次需要刷新时，构造一个 `ResponseParser` 并把它当作回调喂给 `GetResponseData`：

[WeaselTSF/EditSession.cpp:6-14](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/EditSession.cpp#L6-L14) —— `DoEditSession` 里构造 `ResponseParser(&commit, context, &_status, &config, &_cand->style())`，再 `m_client.GetResponseData(std::ref(parser))`。`std::ref` 是因为 `parser` 是函数对象，要按引用传递而非拷贝。

而 `GetResponseData` 最终走到管道层，把缓冲交给这个回调：

[WeaselIPC/WeaselClientImpl.cpp:177-183](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L177-L183) —— `GetResponseData` 转调 `channel.HandleResponseData(handler)`。

[include/PipeChannel.h:139-148](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/PipeChannel.h#L139-L148) —— `HandleResponseData` 把整个共享缓冲 `(LPWSTR)buffer.get()` 与可容纳的 wchar 数交给 handler，也就是 `ResponseParser::operator()`。至此，从「服务端 `_Respond` 写文本」到「客户端结构体被更新」的闭环完整呈现。

#### 4.3.4 代码实践

**实践目标**：用一张对照表吃透三类风格的差异，为第 5 节自己写新动作做准备。

**操作步骤**：

1. 在三份源码里分别找到「判定 key 形态」「读取 value」「写入目标」这三步的代码行。
2. 填写下面这张对照表（示例已给出第一行）：

   | 动作 | 类 | 文件 | 判定 key | value 处理 | 写入目标 |
   | --- | --- | --- | --- | --- | --- |
   | commit | Committer | Committer.cpp:20 | `key.size()==1` | `unescape_string` | `*p_commit` |
   | ctx.preedit | ContextUpdater | ContextUpdater.cpp:26 | `key[1]=="preedit"` | ? | ? |
   | status.composing | StatusUpdater | ContextUpdater.cpp:113 | ? | `bool_value` | ? |
   | style | Styler | Styler.cpp:11 | 无细分 | boost 反序列化 | ? |

3. 思考：如果要新增一个「只更新 `Status.composing`」的动作（见第 5 节），你会借鉴哪一类的写法？

**需要观察的现象**：你会发现「逐字段布尔」风格（`StatusUpdater`）最适合单字段更新，因为它的 `Store` 就是一串 `if (key[1]==...)` 分支，照抄即可。

**预期结果**：完成表格后，你应能说出「新增单字段动作 = 抄 `StatusUpdater` 的壳 + 在 `Initialize` 里 `Define` + 服务端在 `action=` 头和 body 里加上对应行」。**待本地验证**：若想在 `TestResponseParser` 里加真实验证用例，需要 Windows + boost 环境。

#### 4.3.5 小练习与答案

**练习 1**：`Styler::Store` 里没有对 `key` 做任何 `key[1]` 判断，为什么？

> **答案**：因为 `style` 的 value 是「整个 `UIStyle` 的 boost 归档」，没有子字段。服务端只会发 `style=<整块>`（[RimeWithWeasel.cpp:908-909](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L908-L909)），不会发 `style.color=...` 这种细分行，所以无需按 `key[1]` 分派。

**练习 2**：`ContextUpdater::_StoreCand` 反序列化后为什么还要对 `candies/labels/comments` 各跑一遍 `unescape_string`？

> **答案**：因为服务端在序列化前，对每个候选文本都做过 `escape_string`（见 [RimeWithWeasel.cpp:456-465](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L456-L465) 的 `_GetCandidateInfo`）。boost 只负责还原结构，不知道业务层的转义约定，所以要在反序列化之后再手动解转义，才能得到真正可显示的文本。

**练习 3**：`Committer` 里若 `p_commit == NULL` 会怎样？

> **答案**：`Store` 第一行 `if (!m_pTarget->p_commit) return;` 直接返回，什么都不做。这正是 `ResponseParser` 构造时允许各指针传 `NULL` 的意义——例如 [WeaselTSF/WeaselTSF.cpp:179](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.cpp#L179) 只关心 `status` 与 `style`，于是把 `commit/context/config` 都传 `NULL`，对应的 deserializer 即使被激活也只是空跑。

---

## 5. 综合实践

**任务**：仿照 `ContextUpdater`/`Styler` 的写法，编写一个新的 `Deserializer` 子类骨架，并让它能被服务端响应真正触发。

> 说明：规格里给出的示例目标是「只更新 `Status.composing`」。但 `Status.composing` 已经被现有的 `status` 动作（`StatusUpdater`）处理。为了让你的新动作能干净地插入而不与 `status` 冲突，我们用一个**新的动作关键字** `composing`，它只更新 `Status.composing` 这一个字段。这样既贴合规格意图（单字段更新），又演示了完整的新增流程。

### 第 1 步：新建头文件（示例代码，非项目原有文件）

仿照 [WeaselIPC/Styler.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/Styler.h) 与 [WeaselIPC/Committer.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/Committer.h) 的形态：

```cpp
// ComposingFlagUpdater.h —— 示例代码，非项目原有文件
#pragma once
#include "Deserializer.h"

class ComposingFlagUpdater : public weasel::Deserializer {
 public:
  ComposingFlagUpdater(weasel::ResponseParser* pTarget);
  virtual ~ComposingFlagUpdater();
  virtual void Store(weasel::Deserializer::KeyType const& key,
                     std::wstring const& value);
  static weasel::Deserializer::Ptr Create(weasel::ResponseParser* pTarget);
};
```

### 第 2 步：实现 `.cpp`（示例代码）

抄 `StatusUpdater` 的「布尔真值判定」与 `Committer` 的「单值」骨架：

```cpp
// ComposingFlagUpdater.cpp —— 示例代码，非项目原有文件
#include "stdafx.h"
#include "Deserializer.h"
#include "ComposingFlagUpdater.h"

using namespace weasel;

Deserializer::Ptr ComposingFlagUpdater::Create(ResponseParser* pTarget) {
  return Deserializer::Ptr(new ComposingFlagUpdater(pTarget));
}

ComposingFlagUpdater::ComposingFlagUpdater(ResponseParser* pTarget)
    : Deserializer(pTarget) {}
ComposingFlagUpdater::~ComposingFlagUpdater() {}

void ComposingFlagUpdater::Store(Deserializer::KeyType const& key,
                                 std::wstring const& value) {
  if (!m_pTarget->p_status)        // 调用方没要 status 就直接返回
    return;
  // 只认裸 composing=... 这一行（与 Committer 一样用 key.size()==1）
  if (key.size() != 1)
    return;
  bool bool_value = (!value.empty() && value != L"0");
  m_pTarget->p_status->composing = bool_value;
}
```

### 第 3 步：注册工厂（这是「被触发」的关键）

仿照 [WeaselIPC/Deserializer.cpp:13-28](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/Deserializer.cpp#L13-L28)，在 `Initialize` 的 `Define` 列表里加一行，并在头部 `#include "ComposingFlagUpdater.h"`：

```cpp
Define(L"composing", ComposingFlagUpdater::Create);   // 新增这一行
```

加完之后，`s_factories` 里就有了 `composing` 工厂。但**注意**：它不会自动激活——只有在响应的 `action=` 头里出现 `composing` 这个名字时，[ActionLoader.cpp:26-29](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/ActionLoader.cpp#L26-L29) 的 `Require` 才会把它实例化进 `deserializers` 表。

### 第 4 步：让服务端发出对应文本

要让客户端真正触发它，服务端 `_Respond` 里需要发出两行（仿照现有 `status.`/`commit=` 的写法）：

```
action=composing            # 1) 在 actions 摘要里加上 composing
composing=1                 # 2) 在 body 里发出数据行
```

第 1 行会被 `ActionLoader` 解析 → `Require("composing")` → 实例化 `ComposingFlagUpdater`；第 2 行的 `key[0]="composing"` 命中实例 → 调 `Store` → 更新 `p_status->composing`。

### 第 5 步：自检

回答以下问题（不写代码、只推理）：

1. 如果只做了第 2、3 步（注册了工厂、写了类），但服务端**没发** `action=composing`，会发生什么？
   > `composing` 工厂存在于 `s_factories`，但从未被 `Require`，`deserializers` 表里没有它。即便 body 里有 `composing=1`，`Feed` 也会因查表未命中而丢弃。**工厂登记 ≠ 运行时激活**，这是本机制最容易踩的坑。
2. 如果服务端发了 `action=composing` 但**没发** `composing=1` 这一行，会怎样？
   > `ComposingFlagUpdater` 被激活（实例存在于表），但 `Store` 从未被调用，`p_status->composing` 保持原值。无副作用。
3. 为什么我们的动作名不能复用 `status`？
   > 因为 `status` 已经被 `StatusUpdater` 注册（[Deserializer.cpp:21](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/Deserializer.cpp#L21)）。若用同名 `Define`，会**覆盖** `StatusUpdater` 的工厂，导致原本的 `status.ascii_mode`/`status.schema_id` 等字段全部失效。新增动作必须用一个尚未占用的新名字。

> 提示：本实践以「源码阅读 + 纸面编写」为主。若要在真实工程里编译验证，需要把头/源加入 `WeaselIPC.vcxproj`（或 `xmake.lua`），并在 Windows + boost 环境下重新构建 `WeaselIPC`，**待本地验证**。

---

## 6. 本讲小结

- `ResponseParser` 是一个**函数对象**，作为 `ResponseHandler` 回调被管道层调用；它把响应正文按**行协议**解析：`key=value`、`#` 注释、`.` 结束。
- `Feed` 按 `=` 拆 key/value，按 `.` 拆 key，用 **`key[0]` 作为动作名**在 `deserializers` 表里查派发对象。
- 分发采用**懒加载工厂**：`Define` 只登记静态工厂表 `s_factories`，`Require` 才实例化；`ActionLoader` 解析 `action=a,b,c` 头，按需把动作激活进实例表。
- 三类写入风格各管一摊：**逐字段文本**（`ContextUpdater`/`StatusUpdater`/`Configurator`）、**单值上屏**（`Committer`）、**整块 boost 反序列化**（`Styler`，以及 `ctx.cand`）。
- `TryDeserialize` 是反序列化的容错兜底，失败时弹框而非崩溃，承接 u2-l4 的版本兼容设计。
- 全链路闭环：服务端 `_Respond` 拼文本 → `EatLine` 写回管道 → 客户端 `HandleResponseData` 交缓冲 → `ResponseParser::operator()` 逐行 `Feed` → 各 `Deserializer::Store` 更新 `Context/Status/Config/UIStyle/commit`。

---

## 7. 下一步学习建议

本讲讲完了解析端。接下来建议：

1. **进入第 3 单元（TSF 前端）**：看 `WeaselTSF/EditSession.cpp`、`Composition.cpp` 如何在 `ResponseParser` 填好 `commit`/`context` 之后，把上屏串写回应用文档、把 preedit 画进候选窗口。本讲的 `commit` 字段就是它们 `DoEditSession` 的输入。
2. **进入第 4 单元（RimeWithWeasel）**：对照 [RimeWithWeasel.cpp 的 `_Respond`](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L738-L935)，从「生产端」反向理解每个协议行是怎么从 librime 的 `RimeContext`/`RimeStatus` 转译出来的，与本讲形成闭环。
3. **想做扩展练习**：参考第 5 节，真的在 `test/TestResponseParser` 里加一个用例，喂入 `action=composing\ncomposing=1\n.\n`，断言 `status.composing == true`——这是验证你是否真正掌握「注册 + 激活 + 派发」三步的最直接方式。
