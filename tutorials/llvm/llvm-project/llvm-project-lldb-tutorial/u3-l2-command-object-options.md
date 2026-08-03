# CommandObject 与选项系统

## 1. 本讲目标

上一讲（u3-l1）我们把视线停在「前台调度员」`CommandInterpreter`：它把一行文本解析、查找，最终找到那个承办命令的 `CommandObject`，然后调用它的 `Execute`。当时我们刻意把 `Execute` 内部当作一个黑盒，留到本讲拆开。

本讲就从 `Execute` 这个入口向内走，回答两个核心问题：

1. **一条命令本身是如何被组织的？** `CommandObject` 这个基类提供了哪些统一约定？单命令（`breakpoint set` 里的 `set`）、原始命令（`expression`）、多词命令（`breakpoint`）在类层次上有什么区别？
2. **命令的「选项」（`-n main`、`--repl` 这类参数）是怎么被解析并存下来的？** `Options`、`OptionGroup`、`OptionGroupOptions`、`OptionValue` 这一整套类是如何分工协作的？

学完本讲，你应当能够：

- 说清 `CommandObject::Execute` 的回调模型，以及 `CheckRequirements → ParseOptions → DoExecute → Cleanup` 这条固定生命周期；
- 区分 `CommandObjectParsed`、`CommandObjectRaw`、`CommandObjectMultiword` 三种命令形态，并知道何时用哪一种；
- 解释 `CommandReturnObject` 如何承载命令的输出、错误与状态；
- 描述「`OptionDefinition` 描述一个开关 → `Options` 用 getopt 解析 → `OptionGroup` 可复用捆绑 → `OptionValue` 存储解析后的值」这条数据流；
- 看懂一个真实命令（如 `expression`）的选项定义与 `Execute` 流程。

> 本讲全部内容基于真实源码，行号与永久链接对应仓库当前 HEAD `e7dd336e0f7`。术语首次出现时都会给出解释。

## 2. 前置知识

阅读本讲前，建议先建立以下概念（u3-l1 已讲过的此处简要承接）：

- **命令对象 `CommandObject`**：LLDB 里「一条命令」在 C++ 层面的对应物。`breakpoint`、`apropos`、`expression` 各自是一个 `CommandObject`（或其子类）的实例。解释器通过命令字典把命令名映射到这个对象。
- **多词命令（multiword command）**：像 `breakpoint set`、`thread step-over` 这种「动词 + 名词」结构，顶层 `breakpoint`/`thread` 是**容器**，本身不干活，只负责把第一个词（`set`/`step-over`）转发给对应的子命令对象。
- **参数（argument）与选项（option）**：
  - **参数**是位置性的「裸」输入，例如 `apropos breakpoint` 里的 `breakpoint`、`frame variable x y` 里的 `x y`。
  - **选项**是带短横线的「开关」或「键值」，例如 `breakpoint set -n main` 里的 `-n main`、`expression --repl` 里的 `--repl`。选项可以出现在任意位置、可有可无，因此需要专门的解析器（基于 `getopt_long` 的思想）。
- **getopt_long**：C 标准库里解析命令行选项的经典函数，支持 `-n`（短选项）和 `--name`（长选项）。LLDB 的 `Options` 类就是在这套机制之上封装的。理解「一个选项有一个短字符（如 `n`）和一个长名字（如 `name`），可能带参数」即可。
- **执行上下文 `ExecutionContext`**：u2-l3、u5-l1 会详讲。这里只需知道它打包了「当前调试器/目标/进程/线程/栈帧」，命令执行前会被冻结进 `CommandObject`，供 `DoExecute` 使用。

承接 u3-l1：解释器的 `HandleCommand` 在 Phase 2 调用的就是命令对象的 `Execute`；本讲就从这里钻进去。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/lldb/Interpreter/CommandObject.h](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Interpreter/CommandObject.h) | `CommandObject` 基类与 `CommandObjectParsed`/`CommandObjectRaw` 两个中间子类的声明。本讲的「骨架」定义在这里。 |
| [source/Interpreter/CommandObject.cpp](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Interpreter/CommandObject.cpp) | **本讲核心模块之一**。`Execute` 的模板方法实现、`ParseOptions`、`CheckRequirements`、帮助文本生成都在这里。 |
| [include/lldb/Interpreter/CommandObjectMultiword.h](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Interpreter/CommandObjectMultiword.h) | `CommandObjectMultiword`（容器命令）与 `CommandObjectProxy`（代理命令）声明。 |
| [source/Commands/CommandObjectMultiword.cpp](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectMultiword.cpp) | 多词命令的 `Execute`：取第一个词、查子命令、转发。 |
| [include/lldb/Interpreter/CommandReturnObject.h](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Interpreter/CommandReturnObject.h) / [source/Interpreter/CommandReturnObject.cpp](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Interpreter/CommandReturnObject.cpp) | 命令的「返回值对象」：输出流、错误流、状态码。 |
| [include/lldb/Interpreter/Options.h](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Interpreter/Options.h) | 选项体系的三层声明：`Options`（解析器）、`OptionGroup`（可复用捆绑）、`OptionGroupOptions`（聚合器）。 |
| [include/lldb/Utility/OptionDefinition.h](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Utility/OptionDefinition.h) | 描述「一个开关」的静态结构体：长短名、是否带参数、帮助文本。 |
| [include/lldb/Interpreter/OptionValue.h](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Interpreter/OptionValue.h) / [source/Interpreter/OptionValue.cpp](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Interpreter/OptionValue.cpp) | **本讲核心模块之二**。类型化的「值」多态体系，存储解析后的选项值与设置项。 |
| [include/lldb/Interpreter/OptionValueBoolean.h](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Interpreter/OptionValueBoolean.h) | `OptionValue` 的一个具体子类，作为「值系统」最小样例。 |
| [source/Commands/CommandObjectApropos.cpp](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectApropos.cpp) | 最简单的 `CommandObjectParsed` 实例，4.1 节用作「裸骨架」样例。 |
| [source/Commands/CommandObjectExpression.h](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectExpression.h) / [.cpp](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectExpression.cpp) | `expression` 命令，选项体系「组合式」用法最完整的范例。 |

## 4. 核心概念与源码讲解

### 4.1 CommandObject 基类：命令的统一抽象与 Execute 回调模型

#### 4.1.1 概念说明

`CommandObject` 是**所有命令的基类**。它定义了一条命令与命令解释器之间的「契约」：解释器只认准一个入口——`Execute(const char *args_string, CommandReturnObject &result)`，至于这条命令具体做什么，由子类决定。

这种「基类固定骨架、子类填一个钩子」的设计叫**模板方法模式（Template Method）**。它的好处是：所有命令共享同一套前置检查、选项解析、清理逻辑，子类作者只需关心「业务逻辑」这一个 `DoExecute` 钩子，不会漏掉锁、上下文清理等容易出错的细节。

需要先建立两个心智锚点：

- **`Execute` 是公共非虚的「外壳」**，由基类实现，负责流程编排；它最终会调用一个**纯虚的 `DoExecute`「内核」**，子类必须实现这个内核。这种「公共 `Execute` + 私有 `DoExecute`」的成对设计在 LLDB 里非常普遍（`Process::Launch`/`DoLaunch`、`Process::Resume`/`DoResume` 也是同一套路，u5-l2 会见到）。
- **`CommandReturnObject`（简称 result）** 是命令与外界通信的唯一通道：成功/失败状态、给用户看的输出、报错信息，全部写进 `result`。命令本身不直接 `printf`，而是 `result.GetOutputStream() << ...`。

#### 4.1.2 核心流程

`CommandObject` 基类并不直接实现 `Execute`，而是把它交给两个中间子类之一（`CommandObjectParsed` 或 `CommandObjectRaw`，见 4.2）。以最常用的 `CommandObjectParsed::Execute` 为例，一次命令执行的生命周期是固定的五步：

```
CommandInterpreter::HandleCommand  (u3-l1)
        │  找到命令对象，调用其 Execute(args_string, result)
        ▼
┌───────────────────────────────────────────────────────────┐
│ ① InvokeOverrideCallback  (若设了覆盖回调，可能直接返回)    │
│ ② CheckRequirements       (冻结执行上下文；校验 target/     │
│                            process/thread/frame 是否齐备)   │
│ ③ ParseOptions            (解析 -x/--xxx 选项，剩余留给参数)│
│ ④ DoExecuteStatusCheck    (RAII：强制子类必须设置状态)      │
│ ⑤ DoExecute(Args&, result)  ← 子类真正干活的钩子            │
│   finally: Cleanup()       (清空上下文、释放 API 锁)         │
└───────────────────────────────────────────────────────────┘
```

要点：

1. **上下文是「借」来的，不是「存」下来的**。第②步 `CheckRequirements` 把解释器当前的执行上下文拷进成员 `m_exe_ctx`，第⑤步之后 `Cleanup()` 立刻清空它。这意味着命令对象**两次调用之间不持有** target/process 指针，避免悬挂引用（见源码里那段长长的 assert 注释）。
2. **状态码必须被设置**。第④步的 `DoExecuteStatusCheck` 是个 RAII 对象：进入时把状态强行置为 `eReturnStatusInvalid`，析构时 `assert` 状态已被改变。这就强制子类的 `DoExecute` **必须**调用 `result.SetStatus(...)`（或间接通过 `AppendError` 触发），不能「忘记设状态」。
3. **选项解析在 `DoExecute` 之前**。第③步 `ParseOptions` 把命令行里的选项吃掉，剩下的「裸参数」才传给 `DoExecute`。因此子类在 `DoExecute` 里拿到的 `Args` 已经是「干净的参数」，无需再处理 `-n` 之类。

#### 4.1.3 源码精读

先看基类的「入口契约」——纯虚 `Execute`，以及两个最关键的虚拟钩子：

[include/lldb/Interpreter/CommandObject.h:351-352](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Interpreter/CommandObject.h#L351-L352) —— 基类把 `Execute` 声明为**纯虚**，强制每个命令必须提供。注意它只收到「原始命令串」和「结果对象」，不含解析后的结构。

```cpp
virtual void Execute(const char *args_string,
                     CommandReturnObject &result) = 0;
```

[include/lldb/Interpreter/CommandObject.h:197](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Interpreter/CommandObject.h#L197) —— 另一个纯虚 `WantsRawCommandString()`，决定本命令想要「已解析的参数」还是「未触碰的原始串」（详见 4.2）。

[include/lldb/Interpreter/CommandObject.h:119-123](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Interpreter/CommandObject.h#L119-L123) —— `GetOptions()` 默认返回 `nullptr`，意为「本命令没有选项」。**有选项的命令必须覆盖它**，返回一个 `Options*`：

```cpp
Options *CommandObject::GetOptions() {
  // By default commands don't have options unless this virtual function is
  // overridden by base classes.
  return nullptr;
}
```

接下来看 `CommandObjectParsed::Execute` 的完整五步编排——这是本节最重要的一段代码：

[source/Interpreter/CommandObject.cpp:826-867](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Interpreter/CommandObject.cpp#L826-L867) —— `CommandObjectParsed::Execute`：依次执行覆盖回调、反引号预处理、`CheckRequirements`、`ParseOptions`、参数数量校验、`DoExecuteStatusCheck`、`DoExecute`，最后 `Cleanup()`。下面只摘关键骨架（省略反引号预处理细节）：

```cpp
void CommandObjectParsed::Execute(const char *args_string,
                                  CommandReturnObject &result) {
  ...
  if (CheckRequirements(result)) {           // ② 冻结上下文 + 校验
    if (ParseOptions(cmd_args, result)) {    // ③ 解析选项
      if (cmd_args.GetArgumentCount() != 0 && m_arguments.empty()) {
        result.AppendErrorWithFormatv("'{0}' doesn't take any arguments.",
                                      GetCommandName());
        Cleanup();
        return;
      }
      m_interpreter.IncreaseCommandUsage(*this);
      DoExecuteStatusCheck check(result);     // ④ RAII 强制设状态
      DoExecute(cmd_args, result);           // ⑤ 子类干活
    }
  }
  Cleanup();                                 // 释放上下文与锁
}
```

注意第 ③ 步如果失败（如用户给了未知选项），`ParseOptions` 已经把错误写进 `result` 并返回 `false`，于是 `DoExecute` 根本不会被调用——错误在「解析阶段」就被拦下。

`DoExecuteStatusCheck` 这个 RAII 检查器本身很简洁：

[source/Interpreter/CommandObject.cpp:44-53](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Interpreter/CommandObject.cpp#L44-L53) —— 构造时把状态置为 `Invalid`，析构时断言状态已变。

`CheckRequirements` 的内容较长，但逻辑很直白——它把命令的「需求标志位」逐项核对。这些标志位定义在：

[include/lldb/lldb-enumerations.h:1302-1361](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/lldb-enumerations.h#L1302-L1361) —— `CommandFlags` 枚举，常用位如下表：

| 标志位 | 含义（命令执行前必须满足） |
| --- | --- |
| `eCommandRequiresTarget` | 必须有有效目标（`target create` 过） |
| `eCommandRequiresProcess` | 必须有进程 |
| `eCommandRequiresThread` | 必须有线程 |
| `eCommandRequiresFrame` | 必须有栈帧（进程须处于停止态） |
| `eCommandProcessMustBeLaunched` | 进程必须已启动 |
| `eCommandProcessMustBePaused` | 进程必须处于停止态（不能在跑） |
| `eCommandTryTargetAPILock` | 取目标 API 锁，串行化访问 |
| `eCommandAllowsDummyTarget` | 允许在没有真实目标时使用「虚拟目标」 |

这些标志位在命令**构造时**通过 `flags` 参数传入（见 4.2 各子类构造函数）。`CheckRequirements` 就是用它们来决定一条命令在「当前没有进程」「进程正在运行」等场景下该不该被拦下。

#### 4.1.4 代码实践

**实践目标**：用一个最简单的命令 `apropos` 验证「裸骨架」——它没有选项、不要求进程，是理解 `CommandObjectParsed` 的最佳起点。

**操作步骤**：

1. 阅读 [source/Commands/CommandObjectApropos.cpp:23-29](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectApropos.cpp#L23-L29)，确认它的构造函数只做了两件事：把基类设为 `CommandObjectParsed`、用 `AddSimpleArgumentList(eArgTypeSearchWord)` 声明「一个搜索词参数」：

   ```cpp
   CommandObjectApropos::CommandObjectApropos(CommandInterpreter &interpreter)
       : CommandObjectParsed(
             interpreter, "apropos",
             "List debugger commands and settings related to a word or subject.",
             nullptr) {
     AddSimpleArgumentList(eArgTypeSearchWord);
   }
   ```

2. 注意它**没有覆盖 `GetOptions()`**（默认返回 `nullptr`），也**没有传任何 `flags`**（不要求 target/process）。所以第 ③ 步 `ParseOptions` 什么也不做，第 ② 步 `CheckRequirements` 也一路放行。
3. 进入 `DoExecute`（[CommandObjectApropos.cpp:33](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectApropos.cpp#L33)）：它从 `args[0]` 取搜索词，调用 `m_interpreter.FindCommandsForApropos(...)` 查匹配命令，把结果写进 `result.GetOutputStream()`，最后 `result.SetStatus(...)`。

**需要观察的现象**：在已构建好的 `lldb` 里运行：

```
(lldb) apropos breakpoint
```

**预期结果**：看到一串与 `breakpoint` 相关的命令列表，并附带 `settings` 里的相关设置项。这串文本就是 `DoExecute` 通过 `result.GetOutputStream()` 写出的；命令能成功返回，说明 `DoExecute` 里调用了 `result.SetStatus(eReturnStatusSuccessFinishResult)`（见源码 [CommandObjectApropos.cpp:71](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectApropos.cpp#L71) 与 [:128](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectApropos.cpp#L128)），否则 `DoExecuteStatusCheck` 的 `assert` 会触发。

> 若手头没有可运行的构建产物，以上为「源码阅读型实践」——可仅通过阅读源码完成流程绘制。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `CommandObject` 要在 `Execute` 之后立刻 `Cleanup()` 清空 `m_exe_ctx`，而不是等下一次命令执行时再覆盖？

**参考答案**：`m_exe_ctx` 里持有 target/process/thread/frame 的**共享指针**。如果不及时清空，命令对象（通常长期存活在命令字典里）会让这些对象「多活一条命令的时间」，造成资源泄漏，更危险的是在多线程（如 lldb-dap、脚本回调）下会持有已失效的上下文。源码 [CommandObject.cpp:164-178](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Interpreter/CommandObject.cpp#L164-L178) 的注释与 assert 正是为此而设。

**练习 2**：如果一个命令的 `DoExecute` 忘记调用 `result.SetStatus(...)`，会发生什么？

**参考答案**：`DoExecuteStatusCheck` 的析构函数会 `assert(m_result.GetStatus() != eReturnStatusInvalid)`（[CommandObject.cpp:50-53](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Interpreter/CommandObject.cpp#L50-L53)），在调试构建里直接中止程序；`AppendError` 等方法内部会调 `SetStatus(eReturnStatusFailed)`，所以只要走了报错路径也算「设过状态」。

### 4.2 单命令 vs 多词命令：三种命令形态

#### 4.2.1 概念说明

基类 `CommandObject` 本身是抽象的（`Execute` 和 `WantsRawCommandString` 都是纯虚）。LLDB 提供了三个可直接使用的「形态」，对应三种命令风格：

| 类 | 适用于 | `WantsRawCommandString` | 钩子签名 | 典型命令 |
| --- | --- | --- | --- | --- |
| `CommandObjectParsed` | 「标准」命令：选项 + 位置参数 | `false` | `DoExecute(Args &command, ...)` | `apropos`、`breakpoint set` |
| `CommandObjectRaw` | 需要拿到**未经触碰的原始串**的命令 | `true` | `DoExecute(StringRef command, ...)` | `expression`、`command alias` |
| `CommandObjectMultiword` | 容器命令：只转发给子命令 | `false` | （重写 `Execute` 自身） | `breakpoint`、`thread`、`frame` |

**为什么需要 `CommandObjectRaw`？** 像 `expression a < b && c > d` 这样的表达式里，`<`、`>`、`&&` 都会被参数解析器误判成 shell 元字符或重定向。如果走 `CommandObjectParsed`，表达式会被提前切碎。因此 `expression` 选择拿「原始串」，自己用 `OptionsWithRaw` 在「选项」与「表达式正文」之间画一条 ` -- ` 分界线（详见 u3-l3）。

**多词命令的层级**：`breakpoint` 是一个 `CommandObjectMultiword`，它内部用一张字典 `m_subcommand_dict` 把 `set`/`list`/`delete`/`enable` 等子命令对象存起来。`breakpoint set` 的执行路径是：解释器先解析到 `breakpoint`（容器），调用它的 `Execute`；容器的 `Execute` 取出第一个词 `set`，查到对应的 `CommandObjectBreakpointSet`，再调用**它**的 `Execute`。

#### 4.2.2 核心流程

多词命令的转发逻辑：

```
"breakpoint set -n main"
   │
   ▼ CommandObjectMultiword::Execute
   │  1) Args args(args_string); 取 args[0] = "set"
   │  2) 若 argc==0：打印自身帮助并返回
   │  3) GetSubcommandObject("set")  → 在 m_subcommand_dict 里查
   │     （支持唯一前缀缩写，如 "brea s"）
   │  4) args.Shift();  去掉第一个词
   │  5) sub_cmd_obj->Execute(args_string, result);  ← 递归进子命令
   ▼
CommandObjectBreakpointSet::Execute  (走 4.1 的五步流程)
```

注意第 5 步传给子命令的仍是**原始的 `args_string`**（包含 `set -n main`），而不是 `Shift` 之后的。这是因为子命令自己的 `CommandObjectParsed::Execute` 会重新 `Args(args_string)` 解析——它会从「跳过第一个词」的位置开始读。这一点在源码注释里也写明了。

#### 4.2.3 源码精读

两个中间子类，体量极小，但定义了整棵命令树的两种基本节点：

[include/lldb/Interpreter/CommandObject.h:427-442](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Interpreter/CommandObject.h#L427-L442) —— `CommandObjectParsed`：覆盖 `Execute`（实现在 .cpp 里，即 4.1 看到的那段五步流程），把 `WantsRawCommandString` 钉死为 `false`，并要求子类实现 `DoExecute(Args&, ...)`：

```cpp
class CommandObjectParsed : public CommandObject {
  ...
  void Execute(const char *args_string, CommandReturnObject &result) override;
protected:
  virtual void DoExecute(Args &command, CommandReturnObject &result) = 0;
  bool WantsRawCommandString() override { return false; }
};
```

[include/lldb/Interpreter/CommandObject.h:444-460](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Interpreter/CommandObject.h#L444-L460) —— `CommandObjectRaw`：对称地，`WantsRawCommandString` 返回 `true`，钩子是 `DoExecute(StringRef, ...)`。它的 `Execute` 比 Parsed 版更简短，因为**不调用 `ParseOptions`**（选项解析推迟到 `DoExecute` 内部用 `OptionsWithRaw` 处理）。

多词命令本身则**绕过** `CommandObjectParsed/Raw`，直接继承 `CommandObject` 并重写 `Execute`：

[include/lldb/Interpreter/CommandObjectMultiword.h:20-32](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Interpreter/CommandObjectMultiword.h#L20-L32) —— `CommandObjectMultiword` 声明。关键覆盖：`IsMultiwordObject() override { return true; }` 与 `GetAsMultiwordCommand() override { return this; }`，让外界能识别「这是个容器」：

```cpp
class CommandObjectMultiword : public CommandObject {
  ...
  bool IsMultiwordObject() override { return true; }
  CommandObjectMultiword *GetAsMultiwordCommand() override { return this; }
  bool LoadSubCommand(llvm::StringRef cmd_name,
                      const lldb::CommandObjectSP &command_obj) override;
  ...
  CommandObject::CommandMap m_subcommand_dict;   // 子命令字典
};
```

[source/Commands/CommandObjectMultiword.cpp:151-181](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectMultiword.cpp#L151-L181) —— `Execute` 的转发实现，核心就几行：

```cpp
void CommandObjectMultiword::Execute(const char *args_string,
                                     CommandReturnObject &result) {
  Args args(args_string);
  ...
  auto sub_command = args[0].ref();                 // 取第一个词
  ...
  CommandObject *sub_cmd_obj = GetSubcommandObject(sub_command, &matches);
  if (sub_cmd_obj != nullptr) {
    args.Shift();                                   // 去掉第一个词
    sub_cmd_obj->Execute(args_string, result);      // 递归进子命令
    return;
  }
  ...
}
```

`GetSubcommandObject` 内部支持「唯一前缀缩写」：`breakpoint s` 若只有 `set` 一个子命令以 `s` 开头，就解析成 `set`；若有多个则报歧义（u3-l1 已讲过这条规则）。

#### 4.2.4 代码实践

**实践目标**：在真实 `lldb` 会话里观察「容器命令的转发」与「缩写解析」。

**操作步骤**：

1. 启动 `lldb`，执行 `help breakpoint`。注意输出是 `breakpoint` **所有子命令**的列表——这正是 `CommandObjectMultiword::Execute` 在 `argc==0` 时调用的 `GenerateHelpText`（[CommandObjectMultiword.cpp:155-157](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectMultiword.cpp#L155-L157)）。
2. 执行 `breakpoint li`（缩写）。若能成功列出断点，说明 `GetSubcommandObject("li")` 唯一匹配到了 `list`。
3. 执行 `breakpoint s`。若此时已存在多个以 `s` 开头的子命令（`set`/`show`...），应得到歧义错误。

**需要观察的现象**：步骤 2 能正常工作；步骤 3 是否报错取决于当前 LLDB 版本里 `breakpoint` 下以 `s` 开头的子命令数量。

**预期结果**：容器命令对「空参数」回以子命令清单，对「唯一前缀」自动补全，对「歧义前缀」报错并列出候选。

> 待本地验证：步骤 3 的具体歧义候选列表会随版本变化。

#### 4.2.5 小练习与答案

**练习 1**：`expression` 命令继承自 `CommandObjectRaw`（见 [CommandObjectExpression.h:22](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectExpression.h#L22)）。如果把它改成 `CommandObjectParsed`，会出什么问题？

**参考答案**：表达式正文里的 `<`、`>`、`&&`、`--` 等会被 `Args` 解析器和选项解析器误判（比如 `expr a > b` 里的 `>` 可能被当成重定向，`expr --foo` 里的 `--foo` 会被当选项）。`CommandObjectRaw` 让命令拿到原始串，再用 `OptionsWithRaw` 自行在选项与正文之间切分，才能保留表达式原貌。

**练习 2**：`CommandObjectMultiword` 的 `IsMultiwordObject()` 默认在基类返回 `false`（[CommandObject.h:147](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Interpreter/CommandObject.h#L147)），为什么需要这个「自报家门」的方法？

**参考答案**：解释器和帮助系统在很多地方需要「判定一个命令对象是不是容器」（例如决定是否继续下钻子命令、是否展示子命令列表）。用虚函数 + `GetAsMultiwordCommand()` 的安全向下转型，比 C++ 的 `dynamic_cast` 更可控，也便于在公共 `SBCommand` API 层透明地加载子命令（见基类 `LoadSubCommand` 的注释）。

### 4.3 CommandReturnObject：命令的结果与状态

#### 4.3.1 概念说明

`CommandReturnObject`（下称 **result**）是命令与外界沟通的唯一信封。它封装了三类东西：

- **两个流**：`GetOutputStream()`（正常输出）与 `GetErrorStream()`（错误/警告）。每个流底层是一个 `StreamTee`，可以同时把内容写进「内存字符串流」（供程序读取）和「即时文件流」（直接打到终端）。这种「双写」让命令的输出既能被脚本捕获，又能即时显示给用户。
- **一个状态码** `ReturnStatus`：标识命令的成败与是否产生了「结果」。
- **诊断信息** `m_diagnostics`：携带带行列定位的精细诊断（用于把错误指向用户输入的具体位置）。

状态码的定义：

[include/lldb/lldb-enumerations.h:322-331](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/lldb-enumerations.h#L322-L331) —— 七种返回状态：

```cpp
enum ReturnStatus {
  eReturnStatusInvalid,
  eReturnStatusSuccessFinishNoResult,   // 成功，无输出结果
  eReturnStatusSuccessFinishResult,     // 成功，有结果输出
  eReturnStatusSuccessContinuingNoResult,// 成功，但进程还在继续（如 continue）
  eReturnStatusSuccessContinuingResult,
  eReturnStatusStarted,                 // 启动了某事（如多行表达式输入）
  eReturnStatusFailed,                  // 失败
  eReturnStatusQuit                     // 触发了退出
};
```

理解这几个状态很关键：`continue` 命令成功后进程仍在跑，所以是 `...Continuing...`；`apropos` 找到了结果就是 `SuccessFinishResult`，没找到就是 `SuccessFinishNoResult`。

#### 4.3.2 核心流程

命令写结果的典型用法：

```
成功路径：
  result.GetOutputStream() << "1 process running"
  result.SetStatus(eReturnStatusSuccessFinishResult)

失败路径：
  result.AppendError("process must be launched")   // 内部自动 SetStatus(Failed)
  // 或
  result.SetError(Status/llvm::Error)              // 从错误对象搬运

辅助：
  result.AppendMessage(...)    // 普通信息（不带 error: 前缀）
  result.AppendWarning(...)    // 带 "warning:" 前缀，写错误流
  result.AppendNote(...)       // 带 "note:" 前缀
```

解释器在 `HandleCommand` 执行完命令后，会读取 `result.Succeeded()` 决定后续行为（比如是否继续执行 `;` 分隔的下一条命令）。两个判定方法：

- `Succeeded()`：状态码 `<= eReturnStatusSuccessContinuingResult`（即所有 Success 状态）。
- `HasResult()`：状态为 `SuccessFinishResult` 或 `SuccessContinuingResult`（用于判断是否有可打印的结果）。

#### 4.3.3 源码精读

[include/lldb/Interpreter/CommandReturnObject.h:29-31](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Interpreter/CommandReturnObject.h#L29-L31) —— 类声明。成员里两个 `StreamTee`（输出/错误）与 `m_status` 是核心。

[include/lldb/Interpreter/CommandReturnObject.h:59-77](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Interpreter/CommandReturnObject.h#L59-L77) —— `GetOutputStream()` / `GetErrorStream()`。注意它们会**惰性创建**一个 `StreamString` 放在索引 0 的位置，保证总有可读的字符串缓冲：

```cpp
Stream &GetOutputStream() {
  lldb::StreamSP stream_sp(m_out_stream.GetStreamAtIndex(eStreamStringIndex));
  if (!stream_sp) {
    stream_sp = std::make_shared<StreamString>();
    m_out_stream.SetStreamAtIndex(eStreamStringIndex, stream_sp);
  }
  return m_out_stream;
}
```

`AppendError` 是最常用的失败上报入口：

[source/Interpreter/CommandReturnObject.cpp:109-119](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Interpreter/CommandReturnObject.cpp#L109-L119) —— 它先把状态置为 `Failed`，再用带颜色的 `error:` 前缀把消息写进错误流：

```cpp
void CommandReturnObject::AppendError(llvm::StringRef in_string) {
  SetStatus(eReturnStatusFailed);          // ← 关键：自动置失败
  if (in_string.empty()) return;
  llvm::StringRef msg(in_string.rtrim());
  msg.consume_front("error: ");            // 容错：去掉重复前缀
  error(GetErrorStream()) << msg << '\n';   // error() 会打印红色 "error: "
}
```

`error()`/`warning()`/`note()` 三个辅助函数（[CommandReturnObject.cpp:18-34](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Interpreter/CommandReturnObject.cpp#L18-L34)）用 `llvm::WithColor` 给前缀上色。`validate_diagnostic`（[:36-52](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Interpreter/CommandReturnObject.cpp#L36-L52)）还会断言「诊断信息不要自带 `error:` 前缀、不要以句号/换行结尾」，强制遵循 LLVM 的诊断书写规范。

成功/有结果的判定：

[source/Interpreter/CommandReturnObject.cpp:166-173](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Interpreter/CommandReturnObject.cpp#L166-L173) —— `Succeeded()` 与 `HasResult()` 的实现：

```cpp
bool CommandReturnObject::Succeeded() const {
  return m_status <= eReturnStatusSuccessContinuingResult;
}
bool CommandReturnObject::HasResult() const {
  return (m_status == eReturnStatusSuccessFinishResult ||
          m_status == eReturnStatusSuccessContinuingResult);
}
```

#### 4.3.4 代码实践

**实践目标**：观察 result 的「双写」机制——同一条命令的输出既能即时打到终端，又能被脚本捕获。

**操作步骤**：

1. 在 `lldb` 里执行 `help`，终端会即时打印帮助文本（这走的是 `eImmediateStreamIndex` 那一路）。
2. 改用 Python 脚本捕获命令输出：

   ```python
   (lldb) script
   >>> import lldb
   >>> r = lldb.SBCommandReturnObject()
   >>> dbg.HandleCommand("help apropos", r)
   >>> print("SUCCEEDED:", r.Succeeded())
   >>> print("OUTPUT:", r.GetOutput())
   ```

**需要观察的现象**：步骤 2 里 `r.GetOutput()` 能拿到 `help apropos` 的完整输出字符串，而步骤 1 里同样的内容是直接打到屏幕的——这正是 `StreamTee` 把同一份内容「分叉」写到即时流与字符串流的结果。

**预期结果**：`SUCCEEDED: True`，`OUTPUT` 含 `apropos` 的帮助正文。

> 待本地验证：若 LLDB 未启用 Python 绑定，步骤 2 无法执行，可退化为纯阅读型实践——对照 [CommandReturnObject.h:79-103](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Interpreter/CommandReturnObject.h#L79-L103) 的 `SetImmediateOutputFile` 系列方法理解「双写」。

#### 4.3.5 小练习与答案

**练习 1**：`continue` 命令成功后，进程仍在运行。它应该把状态设成 `SuccessFinishResult` 还是 `SuccessContinuingNoResult`？为什么？

**参考答案**：`SuccessContinuingNoResult`。`Continuing` 表示「命令成功但进程没有停在某个可读状态」，提示解释器不要期待立刻能读变量；`NoResult` 表示没有要打印给用户的结果。这两个后缀组合精确描述了 `continue` 的语义。

**练习 2**：为什么 `AppendError` 要调用 `msg.consume_front("error: ")`？

**参考答案**：很多调用方（尤其是透传编译器诊断的路径）传进来的字符串可能已经带了 `error: ` 前缀。而 `error(GetErrorStream())` 自己会再打一个带颜色的 `error: `。`consume_front` 去掉重复前缀，避免出现 `error: error: ...` 的双重前缀。

### 4.4 选项体系：OptionDefinition / Options / OptionGroup / OptionGroupOptions

#### 4.4.1 概念说明

这是本讲的「重头戏」。LLDB 的选项系统是一个**四层结构**，初学者很容易被一堆 `Option*` 类名绕晕。抓住一条主线就好：**从「描述一个开关」到「解析开关」到「组合开关」再到「存储开关的值」**，每层各司其职。

| 层 | 类 | 职责 | 类比 |
| --- | --- | --- | --- |
| ① 描述 | `OptionDefinition` | 一个开关的**静态元数据**：长短名、是否带参数、帮助文本 | 一张「选项规格表」的一行 |
| ② 解析 | `Options` | getopt 解析器，吃进命令行与规格表，逐个回调 `SetOptionValue` | 解析引擎 |
| ③ 组合 | `OptionGroup` / `OptionGroupOptions` | 把若干开关**捆成可复用的组件**，再聚合到一起 | 乐高积木块 + 拼好的底板 |
| ④ 存储 | `OptionValue` 及其子类 | 解析后的**值**（布尔/字符串/...），统一类型擦除接口 | 选项的「当前值」 |

**两种用法**。历史上 LLDB 有两种给命令接选项的方式：

1. **直接子类化 `Options`**：命令自己写一个 `class FooCommandOptions : public Options`，实现 `GetDefinitions()`、`SetOptionValue()`、`OptionParsingStarting()`，并在命令的 `GetOptions()` 里返回它。较老、较啰嗦。
2. **组合式 `OptionGroupOptions`（推荐）**：命令持有一个 `OptionGroupOptions m_option_group`（它**本身是** `Options` 的子类），把若干现成的 `OptionGroup`（如 `OptionGroupFormat`、`OptionGroupBoolean`）`Append` 进去再 `Finalize()`，在 `GetOptions()` 里返回 `&m_option_group`。新命令几乎都用这种，因为它能复用通用选项组。

`OptionGroup` 的精髓是**组合优于继承**：`OptionGroupFormat`（控制 `-f` 输出格式）、`OptionGroupBoolean`（通用布尔开关）等是「预制积木」，多个命令共享同一份实现，避免每个命令各写一遍 `-f` 解析。

#### 4.4.2 核心流程

一条带选项的命令，从文本到值的数据流：

```
"expression -i false -- a + b"
        │
        │  CommandObjectRaw::Execute → DoExecute
        │  用 OptionsWithRaw 切出选项部分 "-i false" 与正文 "a + b"
        ▼
CommandObject::ParseOptionsAndNotify(args, result, m_option_group, exe_ctx)
        │
        ▼  m_option_group.Parse(args, ...)        ← Options::Parse
        │   内部用 getopt_long 遍历 argv：
        │     对每个识别到的选项 → 调 SetOptionValue(idx, value)
        │                              │
        │                              ▼  OptionGroupOptions::SetOptionValue
        │                                 根据 idx 找到来源 OptionGroup，
        │                                 转发给它的 SetOptionValue
        │                                          │
        │                                          ▼  例如 OptionGroupBoolean
        │                                             m_value.SetValueFromString(value)
        │                                                  │
        │                                                  ▼  改写 OptionValueBoolean
        │                                                     m_current_value = false
        │
        ▼  解析完成后，DoExecute 里读 m_command_options.ignore_breakpoints 等
```

其中 `Options::Parse` 的核心是一个标准 getopt 循环：

[source/Interpreter/Options.cpp:1256-1311](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Interpreter/Options.cpp#L1256-L1311) —— 不断调用 `OptionParser::Parse`（对 `getopt_long` 的跨平台封装），拿到选项字符后查 `long_options` 表、回调 `SetOptionValue`，遇到 `?` 报「未知选项」、遇到 `:` 报「缺参数」。

#### 4.4.3 源码精读

**第①层：描述一个开关**。

[include/lldb/Utility/OptionDefinition.h:20-55](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Utility/OptionDefinition.h#L20-L55) —— `OptionDefinition` 结构体的字段就是「一个开关的全部身份信息」：

```cpp
struct OptionDefinition {
  uint32_t usage_mask;        // 属于哪些「选项集」（option set）
  bool required;              // 是否必填
  const char *long_option;    // 长名，如 "repl"
  int short_option;           // 短字符，如 'r'
  int option_has_arg;         // no_argument / required_argument / optional_argument
  OptionValidator *validator; // 可选校验器
  OptionEnumValues enum_values;// 枚举取值
  uint32_t completion_type;   // 补全类型
  lldb::CommandArgumentType argument_type;
  const char *usage_text;     // 帮助文本
};
```

关于 **option set（选项集）**：`usage_mask` 是位掩码。一个命令可以定义多组互斥的选项组合，例如「`-r` 进 REPL 模式」与「正常求值」是不同的 option set。`LLDB_OPT_SET_ALL`（[lldb-defines.h:115](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/lldb-defines.h#L115) 的 `0xFFFFFFFFU`）表示「对所有选项集生效」，`LLDB_OPT_SET_1`（[:116](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/lldb-defines.h#L116) 的 `1U<<0`）表示「只在第 1 组生效」。

**第②③层：解析器与组合**。

[include/lldb/Interpreter/Options.h:58-232](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Interpreter/Options.h#L58-L232) —— `Options` 抽象基类。两个必须由子类实现的纯虚：`SetOptionValue`（[:156-157](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Interpreter/Options.h#L156-L157)）与 `OptionParsingStarting`（[:224](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Interpreter/Options.h#L224)）。后者在每次解析**开始前**被调用，负责把所有选项值重置为默认——这正是命令对象能跨多次调用复用的原因。

[include/lldb/Interpreter/Options.h:234-254](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Interpreter/Options.h#L234-L254) —— `OptionGroup` 抽象。它的接口与 `Options` 的两个纯虚**几乎一模一样**（`GetDefinitions`/`SetOptionValue`/`OptionParsingStarting`），差别在于 `OptionGroup` 不是解析器、只是「可被聚合的组件」：

```cpp
class OptionGroup {
public:
  virtual llvm::ArrayRef<OptionDefinition> GetDefinitions() = 0;
  virtual Status SetOptionValue(uint32_t option_idx, llvm::StringRef option_value,
                                ExecutionContext *execution_context) = 0;
  virtual void OptionParsingStarting(ExecutionContext *execution_context) = 0;
  ...
};
```

[include/lldb/Interpreter/Options.h:256-337](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Interpreter/Options.h#L256-L337) —— `OptionGroupOptions`：它**既是 `Options`（能被 `Parse` 调用），又内部持有一组 `OptionGroup`**。关键方法 `Append`（[:270](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Interpreter/Options.h#L270)）把一个 `OptionGroup` 的 `OptionDefinition` 拷进自己的 `m_option_defs`，并记录「这个选项来自哪个 group」（`m_option_infos`）；`Finalize`（[:309](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Interpreter/Options.h#L309)）标记「装配完成」，之后 `GetDefinitions` 才允许被调用（[:320-323](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Interpreter/Options.h#L320-L323) 有 `assert(m_did_finalize)`）。

`OptionGroupOptions::SetOptionValue` 的职责是**分发**：根据被命中的选项索引，找到它所属的 `OptionGroup`，再把 `SetOptionValue` 转发过去。这样解析器（`Options`）只管「遍历 + 回调」，至于「这个值最终存到哪个对象的哪个字段」由各 `OptionGroup` 自己决定。

**一个通用积木：`OptionGroupBoolean`**。

[include/lldb/Interpreter/OptionGroupBoolean.h:18-46](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Interpreter/OptionGroupBoolean.h#L18-L46) —— 它内部只持有一个 `OptionValueBoolean m_value` 和一个 `OptionDefinition m_option_definition`（即「一个开关的描述 + 它的值」）：

```cpp
class OptionGroupBoolean : public OptionGroup {
  ...
  OptionValueBoolean m_value;
  OptionDefinition m_option_definition;
};
```

[source/Interpreter/OptionGroupBoolean.cpp:16-34](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Interpreter/OptionGroupBoolean.cpp#L16-L34) —— 构造函数把传入的长短名、帮助文本填进 `m_option_definition`。注意一个精巧细节：当 `no_argument_toggle_default=true` 时（即「无参数开关」，如 `--repl` 出现就翻转默认值），它把 `option_has_arg` 设为 `eNoArgument`：

```cpp
m_option_definition.option_has_arg = no_argument_toggle_default
                                         ? OptionParser::eNoArgument
                                         : OptionParser::eRequiredArgument;
```

[source/Interpreter/OptionGroupBoolean.cpp:36-49](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Interpreter/OptionGroupBoolean.cpp#L36-L49) —— `SetOptionValue` 体现了两种布尔开关的行为差异：无参数时翻转默认值并标记「被设过」，有参数时把字符串（`"true"`/`"false"`/`"1"`/`"0"`）交给 `OptionValueBoolean::SetValueFromString` 解析：

```cpp
if (m_option_definition.option_has_arg == OptionParser::eNoArgument) {
  m_value.SetCurrentValue(!m_value.GetDefaultValue());  // 翻转
  m_value.SetOptionWasSet();
} else {
  error = m_value.SetValueFromString(option_value);     // 解析字符串
}
```

**最佳范例：`expression` 命令如何装配选项**。

[source/Commands/CommandObjectExpression.h:96-100](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectExpression.h#L96-L100) —— 它持有一个聚合器 + 四个选项源：

```cpp
OptionGroupOptions m_option_group;        // 聚合底板
OptionGroupFormat m_format_options;        // 预制积木：-f/-F 格式
OptionGroupValueObjectDisplay m_varobj_options; // 预制积木：值展示
OptionGroupBoolean m_repl_option;          // 预制积木：--repl
CommandOptions m_command_options;          // 命令专属选项（自写的 OptionGroup）
```

其中 `CommandOptions` 是命令**自己定义**的 `OptionGroup` 子类（[CommandObjectExpression.h:25-61](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectExpression.h#L25-L61)），承载 `-i`（忽略断点）、`-t`（超时）、`-l`（语言）等 `expression` 独有的开关。

[source/Commands/CommandObjectExpression.cpp:324-332](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectExpression.cpp#L324-L332) —— 构造函数末尾把四块积木拼到底板上，再 `Finalize`：

```cpp
m_option_group.Append(&m_format_options, ...OPTION_GROUP_FORMAT..., LLDB_OPT_SET_1);
m_option_group.Append(&m_command_options);                       // 默认全选项集
m_option_group.Append(&m_varobj_options, LLDB_OPT_SET_ALL, LLDB_OPT_SET_1 | LLDB_OPT_SET_2);
m_option_group.Append(&m_repl_option, LLDB_OPT_SET_ALL, LLDB_OPT_SET_3); // 只在第3组
m_option_group.Finalize();
```

注意 `Append` 的第二、三个参数是「源掩码 / 目标掩码」，用来把同一块积木映射到不同 option set。这里 `m_repl_option` 被映射到 `LLDB_OPT_SET_3`，意味着「只有走 REPL 模式时才允许 `--repl`」。

[source/Commands/CommandObjectExpression.cpp:337](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectExpression.cpp#L337) —— 命令覆盖 `GetOptions()` 把这块拼好的底板交出去：

```cpp
Options *CommandObjectExpression::GetOptions() { return &m_option_group; }
```

而命令专属选项如何把短字符映射到字段，看 `CommandOptions::SetOptionValue` 的 switch：

[source/Commands/CommandObjectExpression.cpp:39-130](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectExpression.cpp#L39-L130) —— 一个大 `switch (short_option)`，把 `'i'` 映射到 `ignore_breakpoints`、`'t'` 映射到 `timeout`、`'l'` 映射到 `language`，依此类推。

> **关于 `CommandOptions.inc`**：在 [CommandObjectExpression.cpp:36-37](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectExpression.cpp#L36-L37) 有一段 `#define LLDB_OPTIONS_expression` + `#include "CommandOptions.inc"`。这个 `.inc` 文件是构建时由 TableGen 从 `.td` 描述生成的 `OptionDefinition[]` 数组，作用与 u1-l4 讲过的 `Options.td` 同源——用声明式描述生成选项表，避免手写冗长的结构体初始化。本讲不展开 TableGen，只需知道「`.inc` 里就是 `GetDefinitions()` 返回的那个数组」。

#### 4.4.4 代码实践

**实践目标**：手写一组示例命令行参数，预测 `OptionValue` 的解析结果，然后用真实 `lldb` 验证。

**操作步骤**：

1. 先阅读 [CommandObjectExpression.cpp:75-85](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectExpression.cpp#L75-L85) 的 `-i` 分支，确认它用 `OptionArgParser::ToBoolean` 把字符串转成 `bool` 赋给 `ignore_breakpoints`。
2. 对下面三组命令，**先在纸上**预测各字段值：

   | 命令 | 预测 `ignore_breakpoints` | 预测 `m_repl_option` 的布尔值 |
   | --- | --- | --- |
   | `expression 1+1` | （默认值，查 `OptionParsingStarting`）| false |
   | `expression -i false 1+1` | false | false |
   | `expression -i 0 1+1` | false | false |
   | `expression --repl` | （默认值）| true（翻转默认 false）|

3. 在 `lldb` 里实际运行验证。`ignore_breakpoints` 不易直接观察，可改用 `-l`（语言）这类有可见效果的选项：`expression -l c -- 1+1`，对比不加 `-l` 时的行为差异。

**需要观察的现象**：`-i false` 与 `-i 0` 应被等价识别（都得到 `false`）；`--repl` 会进入交互式 REPL（输入表达式回车求值），证明它是「无参数翻转」型开关。

**预期结果**：纸面预测与实际行为一致。`--repl` 进入 REPL 后可用 `:quit` 退出。

> 待本地验证：`ignore_breakpoints` 的默认值需查 `CommandOptions::OptionParsingStarting`（同文件内），初学时可只验证 `-l` 与 `--repl` 这两个有可见现象的开关。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `OptionGroupOptions` 在 `GetDefinitions()` 里要 `assert(m_did_finalize)`，而 `Append` 之后必须显式调 `Finalize()`？

**参考答案**：`Append` 是「逐步往底板上加积木」的过程，加完之前选项表是不完整的、不应被外界读到。`Finalize` 标记装配结束，之后 `GetDefinitions` 才返回定型的 `m_option_defs`。这个 assert 是一道防错闸，防止命令在没 `Finalize` 就被解析时拿到半成品选项表。

**练习 2**：`OptionGroupBoolean` 既能做「`--repl` 无参数翻转」，又能做「`-i false` 带参数赋值」。这两种模式由什么决定？

**参考答案**：由构造参数 `no_argument_toggle_default` 决定（[OptionGroupBoolean.cpp:27-29](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Interpreter/OptionGroupBoolean.cpp#L27-L29)）。它为 `true` 时 `option_has_arg=eNoArgument`，`SetOptionValue` 走「翻转默认值」分支；为 `false` 时 `option_has_arg=eRequiredArgument`，走「`SetValueFromString` 解析参数」分支。

### 4.5 OptionValue：类型化的值存储与设置

#### 4.5.1 概念说明

`OptionValue` 是一个**多态的「值」基类**。它的角色是：用一个统一的接口，承载十几种不同类型的值——布尔、字符串、整数、文件路径、架构、正则、格式串……无论具体类型是什么，外界都能用同一套 `GetType()` / `SetValueFromString()` / `GetValueAs<T>()` 来操作。

这套设计服务于两个场景：

1. **选项的「当前值」存储**：如 `OptionGroupBoolean` 内部的 `OptionValueBoolean m_value`。
2. **`settings` 子系统**：`settings set target.arg0 ...`、`settings show` 背后是一棵由 `OptionValueProperties` 组成的「设置树」，每个叶子都是一个具体 `OptionValue` 子类。这也是为什么 `OptionValue` 拥有 `GetSubValue`/`SetSubValue`/`DumpQualifiedName` 这类「树形」接口——它不只是单个值，还能组成一棵带限定名的属性树。

初学者常问：**`OptionDefinition` 和 `OptionValue` 有什么区别？** 一句话：`OptionDefinition` 是「这个开关**长什么样**」（静态描述，用于解析），`OptionValue` 是「这个开关**当前的值是什么**」（动态状态，用于存取）。一个开关通常同时拥有两者：描述告诉解析器怎么识别它，值对象记录它被设成了什么。

#### 4.5.2 核心流程

`OptionValue` 的核心交互是「字符串进、类型化值出」：

```
命令行 / settings 命令
   │  传进来的是字符串，如 "false"、"x86_64"、"/tmp/a"
   ▼
OptionValue::SetValueFromString(value, op)
   │  op 是 VarSetOperationType：assign/append/remove/clear/...
   │  具体子类把字符串解析成自己的类型并存下
   ▼
命令 / 设置读取
   │  需要类型化值
   ▼
OptionValue::GetValueAs<bool>() / GetValueAs<uint64_t>() / ...
   │  模板按目标类型分发到对应的 GetXxxValue
   ▼
拿到 bool / uint64_t / FileSpec / ...
```

`OptionValue` 还维护一个 `m_value_was_set` 标志位：只要 `SetValueFromString` 被调用过（即「用户真的设过它」），就置为 `true`。这样命令可以区分「用户显式给了 `-i false`」与「根本没出现 `-i`，用的是默认值」——很多逻辑分支依赖这个区分。

#### 4.5.3 源码精读

[include/lldb/Interpreter/OptionValue.h:32-56](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Interpreter/OptionValue.h#L32-L56) —— `Type` 枚举列出全部支持的值类型，每个类型对应一个子类（`OptionValueBoolean`、`OptionValueString`、`OptionValueFileSpec`、`OptionValueArch`…）：

```cpp
enum Type {
  eTypeInvalid = 0, eTypeArch, eTypeArgs, eTypeArray, eTypeBoolean,
  eTypeChar, eTypeDictionary, eTypeEnum, eTypeFileLineColumn,
  eTypeFileSpec, eTypeFileSpecList, eTypeFormat, eTypeLanguage,
  eTypePathMap, eTypeProperties, eTypeRegex, eTypeSInt64, eTypeString,
  eTypeUInt64, eTypeUUID, eTypeFormatEntity
};
```

[include/lldb/Interpreter/OptionValue.h:101-103](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Interpreter/OptionValue.h#L101-L103) —— 统一的「从字符串设值」入口，`op` 控制是赋值/追加/清除等：

```cpp
virtual Status
SetValueFromString(llvm::StringRef value,
                   VarSetOperationType op = eVarSetOperationAssign);
```

[source/Interpreter/OptionValue.cpp:610-651](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Interpreter/OptionValue.cpp#L610-L651) —— 基类的默认实现对**所有**操作都返回「不支持」错误。这看似无用，实则重要：它强制每个具体子类**只覆盖自己支持的操作**，其余操作自动得到统一的、格式化的错误信息（如 `"boolean objects do not support the 'append' operation"`）。这是一种「默认拒绝」的安全基线。

类型化的读取靠一组模板与 `GetAsXxx` 转型：

[include/lldb/Interpreter/OptionValue.h:280-308](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Interpreter/OptionValue.h#L280-L308) —— `GetValueAs<T>()` 用 `if constexpr` 按 `T` 的类型分发到 `GetBooleanValue`/`GetUInt64Value`/`GetStringValue` 等私有方法，返回 `std::optional<T>`（类型不符时返回空）：

```cpp
template <typename T, ...>
std::optional<T> GetValueAs() const {
  if constexpr (std::is_same_v<T, bool>)    return GetBooleanValue();
  if constexpr (std::is_same_v<T, uint64_t>) return GetUInt64Value();
  if constexpr (std::is_same_v<T, llvm::StringRef>) return GetStringValue();
  ...
}
```

[source/Interpreter/OptionValue.cpp:43-47](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Interpreter/OptionValue.cpp#L43-L47) —— `GetAsBoolean()` 代表了一类「安全向下转型」方法：先 `GetType()` 判定，再 `static_cast`。所有 `GetAsXxx` 都遵循「类型不符返回 `nullptr`」的约定，避免外部 `dynamic_cast`：

```cpp
OptionValueBoolean *OptionValue::GetAsBoolean() {
  if (GetType() == OptionValue::eTypeBoolean)
    return static_cast<OptionValueBoolean *>(this);
  return nullptr;
}
```

具体子类 `OptionValueBoolean` 是最小样例：

[include/lldb/Interpreter/OptionValueBoolean.h:16-83](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Interpreter/OptionValueBoolean.h#L16-L83) —— 它只存两个 `bool`：当前值与默认值（[:81-82](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Interpreter/OptionValueBoolean.h#L81-L82)），`ClearImpl`（[:76-79](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Interpreter/OptionValueBoolean.h#L76-L79)）把当前值重置回默认值并清掉「被设过」标志——这正是 `OptionParsingStarting` 能让命令对象跨调用复用的落点：

```cpp
void ClearImpl() override {
  m_current_value = m_default_value;
  m_value_was_set = false;
}
```

它还继承了 `Cloneable<OptionValueBoolean, OptionValue>`（[:16](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Interpreter/OptionValueBoolean.h#L16)），这用 CRTP（奇异递归模板）自动实现 `Clone()`，让 `OptionValue::DeepCopy`（[OptionValue.cpp:601-605](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Interpreter/OptionValue.cpp#L601-L605)）能正确克隆出子类类型，用于设置树的复制。

#### 4.5.4 代码实践

**实践目标**：在 `settings` 子系统里直接观察 `OptionValue`，因为它对用户最可见。

**操作步骤**：

1. 在 `lldb` 里执行 `settings show target.skip-prologue`，观察其值（一个布尔 `OptionValue`）。
2. 执行 `settings set target.skip-progression true`（故意拼错键名）→ 应得到错误，说明设置树按限定名查找 `OptionValue` 节点。
3. 执行 `settings set target.skip-prologue true`，再 `settings show target.skip-prologue` → 值变为 `true`，这正是 `OptionValueBoolean::SetValueFromString("true")` 后 `m_value_was_set=true`、`m_current_value=true`。
4. 执行 `settings clear target.skip-prologue`，再 show → 回到默认值，对应 `ClearImpl`。

**需要观察的现象**：步骤 3 设值成功、步骤 4 恢复默认，完整演示了 `OptionValue` 的「设值 / 标记被设 / 清除回默认」三态。

**预期结果**：与上述一致。这证明 `settings` 命令背后操作的正是 `OptionValue` 这套多态值对象（其容器 `OptionValueProperties` 会在后续讲义涉及）。

> 待本地验证：不同 LLDB 版本下 `target.skip-prologue` 的默认值可能不同，但「设值→显示新值→清除→恢复默认」的行为是稳定的。

#### 4.5.5 小练习与答案

**练习 1**：`OptionValue` 基类的 `SetValueFromString` 对所有操作都返回错误（[OptionValue.cpp:610-651](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Interpreter/OptionValue.cpp#L610-L651)）。这种「默认拒绝」设计有什么好处？

**参考答案**：每个具体子类只需覆盖它**支持**的操作（如布尔支持 `assign`/`clear`，不支持 `append`/`insert-before`），未覆盖的操作自动继承基类的统一错误信息（如 `"boolean objects do not support the 'append' operation"`）。好处是：错误格式一致、不会「悄悄什么也不做」、新增子类时不容易遗漏对不支持操作的拒绝。

**练习 2**：`OptionValueBoolean` 为什么要同时存「当前值」和「默认值」两个字段？

**参考答案**：因为 `Clear`/`ClearImpl` 要能把值「恢复默认」，而默认值在构造时确定后不应被 `Clear` 改变；同时 `no_argument_toggle_default` 模式需要「翻转**默认**值」（不是翻转当前值），`IsDefault()` 也需要拿当前值与默认值比对（[OptionValueBoolean.h:43](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Interpreter/OptionValueBoolean.h#L43)）。所以两者都得留着。

## 5. 综合实践

把本讲四块知识串起来，完成一个「**读懂一条真实命令的完整生命周期**」任务。对象选 `breakpoint set -n main -i false`（涉及多词命令、选项解析、result）。

**任务步骤**：

1. **定位类**：在 `source/Commands/` 下找到 `breakpoint set` 对应的命令对象（提示：`breakpoint` 是 `CommandObjectMultiword`，`set` 是它的一个子命令对象）。读其构造函数，记录它继承自 `CommandObjectParsed` 还是 `CommandObjectRaw`，以及在构造时传了哪些 `CommandFlags`（如 `eCommandRequiresTarget`）。

2. **画选项装配图**：找到它的 `GetOptions()` 与选项成员。判断它用的是「直接子类化 `Options`」还是「`OptionGroupOptions` 组合式」。若是后者，列出它 `Append` 了哪些 `OptionGroup`，以及 `-n`（名字）这个开关最终落在哪个 `OptionValue` 子类上。

3. **跟踪一次执行的五步**：对照 4.1.2 的流程图，在源码里标注这次 `breakpoint set -n main -i false` 调用依次经过：`CheckRequirements`（这里会要求有 target）→ `ParseOptions`（把 `-n main`、`-i false` 解析进对应 OptionValue）→ `DoExecute`（用解析到的名字去 Target 里查符号、设断点）→ 写 `result`。

4. **预测并验证**：在没有 target 时直接执行 `breakpoint set -n main`，预测会得到哪条错误信息（提示：对应 `eCommandRequiresTarget` 与 `GetInvalidTargetDescription()`，见 [CommandObject.cpp:194-198](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Interpreter/CommandObject.cpp#L194-L198)）。然后在真实 `lldb` 里验证。

**交付物**：一张「类层次 + 选项装配 + 五步执行」的标注图（手绘或文字均可），以及步骤 4 的预测与实际对照。

> 本任务为「源码阅读 + 少量运行验证」混合型，u3-l3 会专门以 `breakpoint`/`expression` 端到端走通这条链，本讲作为热身。

## 6. 本讲小结

- `CommandObject` 是所有命令的基类，采用**模板方法模式**：公共非虚的 `Execute` 编排「`CheckRequirements` → `ParseOptions` → `DoExecuteStatusCheck` → `DoExecute` → `Cleanup`」五步，子类只填 `DoExecute` 一个钩子。
- 命令有三种形态：`CommandObjectParsed`（标准选项+参数）、`CommandObjectRaw`（要原始串，如 `expression`）、`CommandObjectMultiword`（容器，转发给子命令，如 `breakpoint`）。
- `CommandReturnObject` 是命令与外界沟通的唯一信封，封装输出流/错误流（双写机制）、`ReturnStatus` 状态码与诊断信息；`AppendError` 会自动置失败状态。
- 选项系统是四层结构：`OptionDefinition`（描述一个开关）→ `Options`（getopt 解析器）→ `OptionGroup`/`OptionGroupOptions`（可复用积木与聚合底板）→ `OptionValue`（类型化的值存储）。新命令推荐用组合式 `OptionGroupOptions`。
- `OptionValue` 是多态值基类，统一支撑「选项当前值」与 `settings` 设置树；基类对未覆盖操作「默认拒绝」，具体子类（如 `OptionValueBoolean`）只实现自己支持的操作，并通过 `m_value_was_set` 区分「用户设过」与「用默认」。
- 命令的 `flags`（`CommandFlags`，如 `eCommandRequiresTarget`/`eCommandProcessMustBePaused`）在构造时声明需求，由 `CheckRequirements` 在执行前统一校验，避免每条命令各写一遍前置检查。

## 7. 下一步学习建议

下一讲 **u3-l3「一个命令的实现全过程」** 会以 `breakpoint` 与 `expression` 为例，把本讲的「类层次 + 选项装配」与 u3-l1 的「解释器分发」端到端打通，走完「命令文本 → 选项解析 → 调用 Target/Expression → 输出」的完整链路。建议：

- 先做本讲第 5 节的综合实践，对 `breakpoint set` 有第一手认识；
- 进阶可阅读 [source/Commands/CommandObjectBreakpoint.cpp](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectBreakpoint.cpp) 与 [CommandObjectDisassemble.cpp](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectDisassemble.cpp)，对比「组合式选项」与「直接子类化 Options」两种写法；
- `OptionValue` 组成的设置树（`OptionValueProperties`）会在后续 Core/Utility 单元（u4）与 settings 主题里再次出现，届时可回看本讲 4.5 节建立的多态值模型。
