# 一个命令的实现全过程

## 1. 本讲目标

在前两讲里，我们已经知道 `CommandInterpreter` 负责把一行文本「翻译」成某个 `CommandObject`，并调用它的 `Execute`（u3-l1）；也知道 `CommandObject` 用模板方法模式把生命周期固定为 `CheckRequirements → ParseOptions → DoExecute → Cleanup`，并配合 `OptionGroup`/`OptionValue` 选项体系（u3-l2）。

本讲要回答的是更具体的问题：**当我在终端敲下 `breakpoint set -n main` 或 `expression x + 1` 时，这两行文字究竟是怎么一步步变成一次真实的断点设置 / 一次表达式求值的？**

读完本讲，你应当能够：

1. 在源码里**定位任意一条 LLDB 命令对应的 `CommandObject` 源文件**（这是后续阅读任何命令的通用钥匙）。
2. 看懂 `CommandObjectBreakpoint` 这类**多词命令（multiword）容器**如何把 `breakpoint` 拆成 `set / enable / list ...` 等子命令，并理解 `breakpoint set` 如何用一个 switch 把「选项组合」翻译成「断点类型」再调用 `Target`。
3. 看懂 `CommandObjectExpression` 这类**原始命令（raw）**如何拿到整段表达式、组装 `EvaluateExpressionOptions`，最终调用 `Target::EvaluateExpression`。
4. 理解一个关键事实：**命令实现并不调用 SBAPI，而是直接操作 `lldb_private` 内部对象**（`Target` 等），命令路径与 SB API 路径最终汇聚到同一批内部方法上——这正是 u2-l3 所说「C++ 与 Python 走同一套 SB API」的镜像结论。

---

## 2. 前置知识

本讲默认你已经掌握以下概念（均来自前置讲义），这里只做一句话复习：

- **`CommandObject` 三种形态**（u3-l2）：`CommandObjectParsed`（选项 + 位置参数）、`CommandObjectRaw`（取原始字符串，如 `expression`）、`CommandObjectMultiword`（容器，转发给子命令，如 `breakpoint`）。本讲会同时见到这三种。
- **选项系统的四层结构**（u3-l2）：`OptionDefinition` 描述开关 → `Options` 用 getopt 解析 → `OptionGroup`/`OptionGroupOptions` 把多个可复用积木拼起来 → `OptionValue` 类型化存值。`breakpoint set` 就是「一堆 `OptionGroup` 拼装」的典型例子。
- **`DoExecute` 钩子**（u3-l2）：子类只填 `DoExecute`，框架负责前置检查、选项解析与清理。所以本讲我们几乎只盯 `DoExecute`。
- **`CommandReturnObject` 信封**（u3-l2）：命令与外界的唯一输出通道，封装输出流 / 错误流与 `ReturnStatus` 状态码；`AppendError` 会自动把状态置为失败。
- **执行上下文与 `Target`**（u2-l3 / u4）：被调试程序在 LLDB 内部被表示为 `Target` 对象，断点挂在 `Target` 上，表达式也在 `Target` 上求值。`CommandObject` 基类提供 `GetTarget()` 取当前目标。

> 一个贯穿全讲的直觉：**命令层是「翻译层」**。它的职责只有三件事——(1) 解析选项；(2) 根据选项判断用户「想要什么」；(3) 调用 `Target` / `Process` 等内部对象去真正执行。真正的调试逻辑（断点如何解析、表达式如何 JIT）都不在命令层，而在被调用的模块里。

---

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [source/Interpreter/CommandInterpreter.cpp](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Interpreter/CommandInterpreter.cpp) | 命令注册表。`LoadCommandDictionary()` 把每条命令名映射到一个 `CommandObject` 类，是「定位命令源文件」的起点。 |
| [source/Commands/CommandObjectBreakpoint.cpp](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectBreakpoint.cpp) | `breakpoint` 命令的全部实现：多词容器 `CommandObjectMultiwordBreakpoint` 及其十几个子命令，重点是 `breakpoint set`。 |
| [source/Commands/CommandObjectExpression.cpp](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectExpression.cpp) | `expression`（别名 `expr` / `p`）命令的实现：原始命令形态，驱动表达式求值。 |
| [source/Commands/CommandObjectExpression.h](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectExpression.h) | `CommandObjectExpression` 的类声明，展示它继承 `CommandObjectRaw` + `IOHandlerDelegate`，以及内部聚合的各 `OptionGroup`。 |
| [source/Commands/CommandObjectDisassemble.cpp](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectDisassemble.cpp) | `disassemble` 命令的实现：作为第三种形态（`CommandObjectParsed`）的对照，并演示命令如何与**插件**（`Disassembler`）协作。 |
| [include/lldb/Target/Target.h](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Target/Target.h) | `Target` 的公共接口。命令层最终调用的 `CreateBreakpoint` / `EvaluateExpression` 都声明在这里，证明「命令与 SB API 殊途同归」。 |

---

## 4. 核心概念与源码讲解

### 4.1 定位任意一条命令的 CommandObject

#### 4.1.1 概念说明

LLDB 有几十条命令，每条都是一个独立的 `CommandObject` 子类，散落在 `source/Commands/CommandObject*.cpp` 里。面对一条陌生命令，你不需要记住每个类名——只要知道**一张注册表**：解释器在构造时，用一个名为 `LoadCommandDictionary()` 的函数把「命令名 → CommandObject 类」的映射一次性建好。找到这张表，就能顺着命令名反向定位到源文件。

#### 4.1.2 核心流程

定位一条命令的通用步骤：

1. 在 `CommandInterpreter.cpp` 的 `LoadCommandDictionary()` 里搜索命令名，找到它对应的 `CommandObject` 类名。
2. 该类名通常就是源文件名（如 `CommandObjectDisassemble` → `CommandObjectDisassemble.cpp`）。
3. 若映射到的是一个 `CommandObjectMultiword`（多词命令，如 `breakpoint`），则在对应 `.cpp` 文件的「容器构造函数」里看它 `LoadSubCommand("set", ...)` 注册了哪些子命令，再跳到子命令的类。
4. 进入该类的 `DoExecute` 方法，就是命令真正的执行体。

#### 4.1.3 源码精读

注册靠一个极简的宏完成——它本质就是把一个新建的对象塞进字典 `m_command_dict`：

[CommandInterpreter.cpp:572-573](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Interpreter/CommandInterpreter.cpp#L572-L573) —— `REGISTER_COMMAND_OBJECT` 宏：把命令名 `NAME` 映射到一个新建的 `CLASS` 实例（`*this` 是解释器自身）。

```cpp
#define REGISTER_COMMAND_OBJECT(NAME, CLASS)                                   \
  m_command_dict[NAME] = std::make_shared<CLASS>(*this);
```

`LoadCommandDictionary()` 在解释器构造时被调用一次，集中登记所有内置命令。本讲涉及的三条命令正好分别对应三种 `CommandObject` 形态：

[CommandInterpreter.cpp:579](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Interpreter/CommandInterpreter.cpp#L579) —— `breakpoint` 映射到 `CommandObjectMultiwordBreakpoint`（多词容器）。

[CommandInterpreter.cpp:582](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Interpreter/CommandInterpreter.cpp#L582) —— `disassemble` 映射到 `CommandObjectDisassemble`（解析型单命令）。

[CommandInterpreter.cpp:584](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Interpreter/CommandInterpreter.cpp#L584) —— `expression` 映射到 `CommandObjectExpression`（原始型单命令）。

> 顺带一提，`history` 不是单独的类，而是别名。在 [CommandInterpreter.cpp:546-548](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Interpreter/CommandInterpreter.cpp#L546-L548) 里，`history` 被别名为 `session history`。本讲实践的「命令历史」就要用到它。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：掌握「命令名 → 源文件」的反查能力。

**操作步骤**：

1. 打开 `source/Interpreter/CommandInterpreter.cpp`，定位到 `LoadCommandDictionary()`（约 575 行起）。
2. 任选三条你感兴趣的命令（如 `memory`、`frame`、`register`），记下它们各自映射到哪个 `CommandObject` 类。
3. 用编辑器跳转到对应的 `source/Commands/CommandObject*.cpp`，确认类名与文件名一致。
4. 在该文件里搜索 `DoExecute`，那就是命令的执行体。

**需要观察的现象**：你会发现几乎所有内置命令都遵循 `命令名 → CommandObject<X> 类 → CommandObject<X>.cpp → DoExecute` 这条固定路径。

**预期结果**：你能不依赖 `help` 源码注释，仅凭字典反查，在 30 秒内找到任意一条命令的执行函数。

#### 4.1.5 小练习与答案

**练习 1**：`process` 命令映射到哪个类？它属于三种形态中的哪一种？

**参考答案**：从 [CommandInterpreter.cpp:592](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Interpreter/CommandInterpreter.cpp#L592) 可见 `process` 映射到 `CommandObjectMultiwordProcess`，类名含 `Multiword`，说明是多词命令（容器），真正的逻辑在 `process launch / continue / ...` 等子命令里。

**练习 2**：为什么 `breakpoint` 在字典里只占一行，却有 `set / enable / list` 等十几个子命令？

**参考答案**：因为 `breakpoint` 注册的是一个 `CommandObjectMultiword` 容器。解释器在 Phase 1 解析时会逐词下钻（u3-l1 讲过的 `ResolveCommandImpl`），把 `breakpoint set` 里的 `set` 这个子词在容器内部再查一次子命令字典，最终分发到 `CommandObjectBreakpointSet`。

---

### 4.2 CommandObjectBreakpoint：多词命令与 breakpoint set

#### 4.2.1 概念说明

`breakpoint` 是 LLDB 里最「重」的命令之一。它的复杂性来自两点：

1. **它是多词命令**：`breakpoint` 自己不做事，只是一个壳，下面挂着 `set / enable / disable / list / clear / delete / command / modify / name / write / read / override / add` 等子命令。这种「容器 + 子命令」的结构用 `CommandObjectMultiword` 实现（u3-l2 已介绍），由容器构造函数负责把每个子命令注册进来。
2. **`breakpoint set` 的输入是「选项组合」，输出是「一种断点类型」**：用户可以通过 `-f file -l line`（文件行）、`-a addr`（地址）、`-n func`（函数名）、`-r regex`（函数名正则）、`-p regex`（源码正则）、`-E`（异常）、`-P`（脚本）等多种组合来设置断点。命令层的核心工作就是**根据「用户填了哪些选项」推断出「用户想要哪种断点」**，再调用 `Target` 上对应的创建方法。

#### 4.2.2 核心流程

`breakpoint set` 的执行可以拆成三步：

```text
1. [框架层] CheckRequirements → ParseOptions
     选项被解析进三个 OptionGroup：
       m_options         —— 命令自己的选项（-f/-l/-n/-a/-r ...）
       m_bp_opts         —— 断点修饰选项（-c 条件 / -i 忽略计数 ...）
       m_dummy_options   —— 是否建在 dummy target 上
     这三者由 m_all_options 聚合（OptionGroupOptions 组合模式）。

2. [命令层] DoExecute：根据已解析的选项「推断断点类型」
     if 行号存在       -> eSetTypeFileAndLine
     else if 地址存在   -> eSetTypeAddress
     else if 函数名存在 -> eSetTypeFunctionName
     else if 正则存在   -> eSetTypeFunctionRegexp
     ... （优先级从上到下，先命中先定）

3. [命令层 → Target] switch(类型) 调用 Target 的对应创建方法
     eSetTypeFileAndLine -> target->CreateBreakpoint(modules, file, line, ...)
     eSetTypeAddress     -> target->CreateBreakpoint(addr, ...)
     eSetTypeFunctionName-> target->CreateBreakpoint(modules, files, names, ...)
     ... 拿到 BreakpointSP bp_sp

4. [收尾] 把修饰选项（条件/计数）CopyOverSetOptions 到断点上，
         调 bp_sp->GetDescription() 打印结果，置 ReturnStatus。
```

断点类型的判定是一个**优先级瀑布（priority cascade）**，可以写成一组从上到下短路的选择：

\[ \text{type} = \begin{cases} \text{FileAndLine} & \text{if } \text{line\_num} \neq 0 \\ \text{Address} & \text{else if } \text{load\_addr} \neq \text{INVALID} \\ \text{FunctionName} & \text{else if } \text{func\_names} \neq \emptyset \\ \text{FunctionRegexp} & \text{else if } \text{func\_regexp} \neq \emptyset \\ \text{SourceRegexp} & \text{else if } \text{source\_regexp} \neq \emptyset \\ \text{Exception} & \text{else if } \text{exception\_lang} \neq \text{Unknown} \\ \text{Invalid} & \text{otherwise} \end{cases} \]

注意：填了 `-l` 但没填 `-f` 是合法的（会回退到「当前默认文件」，见下文）；但完全不填任何定位选项，`type` 就是 `Invalid`，不会创建任何断点。

#### 4.2.3 源码精读

先看多词容器如何注册子命令。`CommandObjectMultiwordBreakpoint` 的构造函数把每个子命令 `new` 出来、起好全名（`breakpoint set`）、再用 `LoadSubCommand` 挂到 `set` 这个子词下：

[CommandObjectBreakpoint.cpp:3858-3918](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectBreakpoint.cpp#L3858-L3918) —— 这里贴出关键片段：

```cpp
CommandObjectMultiwordBreakpoint::CommandObjectMultiwordBreakpoint(
    CommandInterpreter &interpreter)
    : CommandObjectMultiword(interpreter, "breakpoint", ...) {
  CommandObjectSP set_command_object(new CommandObjectBreakpointSet(interpreter));
  ...
  set_command_object->SetCommandName("breakpoint set");
  ...
  LoadSubCommand("set", set_command_object);
  // 同样的方式注册 list / enable / disable / clear / delete / ...
}
```

> 准确链接：[CommandObjectBreakpoint.cpp:3874-3910](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectBreakpoint.cpp#L3874-L3910)（构造 `set` 子命令对象并 `LoadSubCommand`）。这就是「多词命令如何把子命令组织起来」的全部秘密。

再看 `CommandObjectBreakpointSet` 本身。它继承 `CommandObjectParsed`，声明了断点类型的枚举，构造时声明 `eCommandAllowsDummyTarget` 标志（允许在没有真实 target 时也建断点，即「dummy target」），并把四个 `OptionGroup` 聚合进 `m_all_options`：

[CommandObjectBreakpoint.cpp:1512-1540](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectBreakpoint.cpp#L1512-L1540) —— 类声明与构造函数。注意 `m_all_options.Append(&m_options)` 等几行就是 u3-l2 讲过的「OptionGroup 积木拼装」。

```cpp
class CommandObjectBreakpointSet : public CommandObjectParsed {
public:
  enum BreakpointSetType {
    eSetTypeInvalid, eSetTypeFileAndLine, eSetTypeAddress,
    eSetTypeFunctionName, eSetTypeFunctionRegexp, eSetTypeSourceRegexp,
    eSetTypeException, eSetTypeScripted,
  };
  CommandObjectBreakpointSet(CommandInterpreter &interpreter)
      : CommandObjectParsed(interpreter, "breakpoint set", ...,
                            eCommandAllowsDummyTarget), ... {
    m_all_options.Append(&m_python_class_options, ...);
    m_all_options.Append(&m_bp_opts, ...);
    m_all_options.Append(&m_dummy_options, ...);
    m_all_options.Append(&m_options);
    m_all_options.Finalize();
  }
```

核心执行体 `DoExecute` 首先决定 target（真实 or dummy），然后做断点类型的优先级判定：

[CommandObjectBreakpoint.cpp:1800-1831](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectBreakpoint.cpp#L1800-L1831) —— `DoExecute` 开头：选 target + 断点类型判定。

```cpp
void DoExecute(Args &command, CommandReturnObject &result) override {
  Target *target =
      m_dummy_options.m_use_dummy ? &GetDummyTarget() : GetTarget();
  ...
  BreakpointSetType break_type = eSetTypeInvalid;
  if (!m_python_class_options.GetName().empty())
    break_type = eSetTypeScripted;
  else if (m_options.m_line_num != 0)
    break_type = eSetTypeFileAndLine;
  else if (m_options.m_load_addr != LLDB_INVALID_ADDRESS)
    break_type = eSetTypeAddress;
  else if (!m_options.m_func_names.empty())
    break_type = eSetTypeFunctionName;
  ...
```

随后是一个大 `switch`，按类型调用 `Target` 上不同的创建方法。以最常见的「文件行」断点为例：

[CommandObjectBreakpoint.cpp:1843-1867](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectBreakpoint.cpp#L1843-L1867) —— `eSetTypeFileAndLine` 分支：处理默认文件，调用 `target->CreateBreakpoint(...)`。

```cpp
case eSetTypeFileAndLine: {
  FileSpec file;
  const size_t num_files = m_options.m_filenames.GetSize();
  if (num_files == 0) {
    if (!GetDefaultFile(*target, m_exe_ctx.GetFramePtr(), file, result)) {
      result.AppendError("no file supplied and no default file available.");
      return;
    }
  } ... else
    file = m_options.m_filenames.GetFileSpecAtIndex(0);

  bp_sp = target->CreateBreakpoint(
      &(m_options.m_modules), file, m_options.m_line_num, m_options.m_column,
      m_options.m_offset_addr, check_inlines, m_options.m_skip_prologue,
      internal, m_options.m_hardware, m_options.m_move_to_nearest_code);
} break;
```

> 这里体现了命令层的「翻译」本质：它把 `-f / -l / -c / --hardware` 等一堆选项，原样转交给 `Target::CreateBreakpoint` 的形参。断点到底怎么解析、怎么插桩，全部由 `Target` 与 `Breakpoint` 模块负责（那是 u9-l1 的主题）。

最后是收尾：把修饰选项（条件、忽略计数等）合并到新建的断点上，打印断点描述，并设置返回状态：

[CommandObjectBreakpoint.cpp:1988-2022](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectBreakpoint.cpp#L1988-L2022) —— 把 `m_bp_opts` 合并进断点、打印描述、置成功状态。

```cpp
if (bp_sp) {
  bp_sp->GetOptions().CopyOverSetOptions(m_bp_opts.GetBreakpointOptions());
  ...
}
if (bp_sp) {
  Stream &output_stream = result.GetOutputStream();
  bp_sp->GetDescription(&output_stream, lldb::eDescriptionLevelInitial, false);
  ...
  result.SetStatus(eReturnStatusSuccessFinishResult);
} else if (!bp_sp) {
  result.AppendError("breakpoint creation failed: no breakpoint created");
}
```

注意 `result.AppendError(...)` 会自动把状态置为失败（u3-l2 讲过的 `CommandReturnObject` 约定），所以命令失败时只需 `AppendError` + `return`，无需手动改状态码。

#### 4.2.4 代码实践（源码阅读型 + 可选实操）

**实践目标**：验证「选项组合 → 断点类型 → Target 调用」这条翻译链。

**操作步骤**：

1. 在 `CommandObjectBreakpoint.cpp` 的 `DoExecute`（约 1800 行）旁，对照断点类型枚举与 switch 分支，画一张表：`类型 → 调用的 Target 方法`。
2. （可选，待本地验证）启动一个带调试信息的小程序，分别执行下面四条命令，观察每条命令断点描述里的 `locations` 数量：
   - `breakpoint set -n main`
   - `breakpoint set -f main.c -l 10`
   - `breakpoint set -a 0x400000`（地址填一个实际存在的）
   - `breakpoint set -r '^print.*'`
3. 对照源码，确认这四条命令分别走了 `eSetTypeFunctionName / eSetTypeFileAndLine / eSetTypeAddress / eSetTypeFunctionRegexp` 四个 switch 分支。

**需要观察的现象**：不同选项组合产生「不同类型的断点」，但命令行输出格式一致（都是 `Breakpoint N: ...`），这是因为收尾都走同一段 `GetDescription`。

**预期结果**：你能把任意 `breakpoint set ...` 命令映射到 switch 的某一个 case，并说出它调用了 `Target` 的哪个方法。如果暂无本地环境，标注「待本地验证」即可，源码阅读部分不受影响。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `breakpoint set` 既不要求 `eCommandRequiresTarget`，反而用 `eCommandAllowsDummyTarget`？

**参考答案**：因为断点可以在「还没有真实被调试程序」时就设置（例如想在程序启动前预先设好断点）。`eCommandAllowsDummyTarget` 表示该命令允许在没有真实 target 时操作 dummy target；`GetTarget()` 在无真实 target 时返回 dummy target，断点建在 dummy target 上，等真实 target 创建时再复制过去（见源码 `target == &GetDummyTarget()` 分支的提示信息）。

**练习 2**：如果用户同时传了 `-l 10` 和 `-n main`，会发生什么？

**参考答案**：根据优先级瀑布，`m_line_num != 0` 先命中，类型被定为 `eSetTypeFileAndLine`，`-n main` 这个函数名选项**被忽略**。命令层只认第一个命中的类型，不会「混合」两种断点。这也提醒读者：选项解析成功不等于选项都被使用。

**练习 3**：断点的「条件（`-c`）」是在 `CreateBreakpoint` 时传进去的，还是创建后再合并的？

**参考答案**：是创建后再合并的。见 [CommandObjectBreakpoint.cpp:1989](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectBreakpoint.cpp#L1989)：`bp_sp->GetOptions().CopyOverSetOptions(m_bp_opts.GetBreakpointOptions())`。条件属于修饰选项 `m_bp_opts`，在断点对象创建完毕后整体拷贝覆盖上去。

---

### 4.3 CommandObjectExpression：原始命令与表达式求值

#### 4.3.1 概念说明

`expression`（别名 `expr`、`p`）和 `breakpoint set` 形态不同——它是 **`CommandObjectRaw`**。原因很简单：表达式里什么字符都可能出现（空格、引号、运算符、甚至 `--`），如果用 getopt 解析会把表达式本身误当成选项。所以原始命令把**选项之后剩下的整段文本**当作「原始表达式」原样取走。

表达式求值是一条更长的流水线（u7 会专门讲），命令层只负责两件事：

1. 把命令行选项翻译成一个 `EvaluateExpressionOptions` 对象（求值策略：是否允许 JIT、是否忽略断点、超时多久……）。
2. 调用 `Target::EvaluateExpression(expr, frame, result_valobj_sp, options)`，把得到的 `ValueObjectSP` 按格式打印进 `CommandReturnObject`。

> 关键直觉：`expression` 命令本身**不解析 C++ 语法、不做 JIT**。它只是「组装选项 + 调用 `Target`」。真正的编译、JIT、回退解释都在 `Target` → `ClangUserExpression` → `IRExecutionUnit` 这条链里（u7-l2）。

#### 4.3.2 核心流程

```text
1. [框架层] CheckRequirements（要求进程处于暂停态 eCommandProcessMustBePaused）
            注意：expression 是 Raw 命令，选项解析方式略不同。

2. [DoExecute] 用 OptionsWithRaw 把 "选项部分" 与 "原始表达式部分" 切开
     OptionsWithRaw args(command);
     expr = args.GetRawPart();          // 真正的表达式串
     if (args.HasArgs()) ParseOptionsAndNotify(...);  // 解析 -i/-u/-j... 等

3. [特殊情况] 若命令行为空 -> GetMultilineExpression() 进入多行编辑器(IOHandler)
              若带 --repl    -> 拉起 REPL

4. [正常路径] 调用本类的 EvaluateExpression(expr, out, err, result)
     4a. GetEvaluateExpressionOptions() 把 CommandOptions 折算成 EvaluateExpressionOptions
     4b. target.EvaluateExpression(expr, frame, result_valobj_sp, options, &fixed)
     4c. 对 result_valobj_sp 做 Dump（按 -f 格式 / 摘要级别）写入 result
     4d. 根据求值结果 ExpressionResults 置 ReturnStatus

5. [Fix-It] 若求值器应用了 Fix-It，且 target 开启了通知，
            把修正后的命令追加进 CommandHistory（这正好是本讲实践的观察点）。
```

`CommandOptions`（求值相关字段）与 `EvaluateExpressionOptions`（传给 `Target` 的策略对象）之间的折算，有几个值得注意的映射，例如「执行策略」取决于是否允许 JIT：

\[ \text{policy} = \begin{cases} \text{eExecutionPolicyNever} & \text{if } \neg\,\text{allow\_jit} \\ \text{default\_execution\_policy} & \text{otherwise} \end{cases} \]

也就是说，`expr -j false` 会把策略强制设为「永不执行（只静态求值）」，这正是「能 JIT 就 JIT，否则解释 IR」回退策略（u1-l1）在命令层的入口开关。

#### 4.3.3 源码精读

先看类声明，确认它是 `CommandObjectRaw` 且额外实现 `IOHandlerDelegate`（为了支持多行表达式编辑）：

[CommandObjectExpression.h:22-23](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectExpression.h#L22-L23) —— 继承关系。

```cpp
class CommandObjectExpression : public CommandObjectRaw,
                                public IOHandlerDelegate {
```

[CommandObjectExpression.h:96-100](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectExpression.h#L96-L100) —— 内部聚合的四个 `OptionGroup`：格式、值对象展示、REPL 开关、以及命令自己的 `CommandOptions`。又是「积木拼装」。

构造函数声明命令标志 `eCommandProcessMustBePaused | eCommandTryTargetAPILock | eCommandAllowsDummyTarget`，并把四个 `OptionGroup` `Append` 进 `m_option_group` 后 `Finalize()`：

[CommandObjectExpression.cpp:257-333](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectExpression.cpp#L257-L333) —— 构造函数（节选）：

```cpp
CommandObjectExpression::CommandObjectExpression(CommandInterpreter &interpreter)
    : CommandObjectRaw(interpreter, "expression", "...", "",
                       eCommandProcessMustBePaused | eCommandTryTargetAPILock |
                           eCommandAllowsDummyTarget),
      ...
{
  ...
  m_option_group.Append(&m_format_options, ...);
  m_option_group.Append(&m_command_options);
  m_option_group.Append(&m_varobj_options, ...);
  m_option_group.Append(&m_repl_option, ...);
  m_option_group.Finalize();
}
```

`CommandOptions` 到 `EvaluateExpressionOptions` 的折算在 `GetEvaluateExpressionOptions` 里完成。注意执行策略与「不忽略断点时强制生成调试信息」这两处细节：

[CommandObjectExpression.cpp:205-243](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectExpression.cpp#L205-L243) —— 折算求值选项（节选）：

```cpp
EvaluateExpressionOptions
CommandObjectExpression::CommandOptions::GetEvaluateExpressionOptions(
    const Target &target, const OptionGroupValueObjectDisplay &display_opts) {
  EvaluateExpressionOptions options;
  options.SetUnwindOnError(unwind_on_error);
  options.SetIgnoreBreakpoints(ignore_breakpoints);
  ...
  options.SetExecutionPolicy(
      allow_jit ? EvaluateExpressionOptions::default_execution_policy
                : lldb_private::eExecutionPolicyNever);
  ...
  // 若可能停下来（撞断点 / 出错回溯），就生成调试信息便于诊断
  if (!ignore_breakpoints || !unwind_on_error)
    options.SetGenerateDebugInfo(true);
  ...
}
```

真正的求值与结果处理在私有方法 `EvaluateExpression` 中。它先组装选项，再调用 `Target::EvaluateExpression`，最后把 `ValueObjectSP` Dump 出来：

[CommandObjectExpression.cpp:428-435](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectExpression.cpp#L428-L435) —— 组装选项并调用 `Target` 求值。

```cpp
EvaluateExpressionOptions eval_options =
    m_command_options.GetEvaluateExpressionOptions(target, m_varobj_options);
eval_options.SetSuppressPersistentResult(false);

ExpressionResults success = target.EvaluateExpression(
    expr, frame, result_valobj_sp, eval_options, &m_fixed_expression);
```

`Target::EvaluateExpression` 的签名（声明在 `Target.h`）证实了它就是 SB API `SBTarget::EvaluateExpression` 背后同一个内部方法：

[Target.h:1539-1543](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Target/Target.h#L1539-L1543) —— `Target::EvaluateExpression` 声明：

```cpp
lldb::ExpressionResults EvaluateExpression(
    llvm::StringRef expression, ExecutionContextScope *exe_scope,
    lldb::ValueObjectSP &result_valobj_sp,
    const EvaluateExpressionOptions &options = EvaluateExpressionOptions(),
    std::string *fixed_expression = nullptr, ValueObject *ctx_obj = nullptr);
```

拿到结果后，命令层按格式把值 Dump 进 `result` 的输出流，并根据 `ExpressionResults` 置状态码：

[CommandObjectExpression.cpp:444-510](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectExpression.cpp#L444-L510) —— 结果处理（节选）：

```cpp
if (result_valobj_sp) {
  result.GetValueObjectList().Append(result_valobj_sp);
  if (result_valobj_sp->GetError().Success()) {
    ... result_valobj_sp->Dump(output_stream, options); ...
    result.SetStatus(eReturnStatusSuccessFinishResult);
  } else {
    ...
    result.SetStatus(eReturnStatusFailed);
    result.SetError(result_valobj_sp->GetError().ToError());
  }
}
...
return (success != eExpressionSetupError &&
        success != eExpressionParseError);
```

入口 `DoExecute` 用 `OptionsWithRaw` 切分「选项」与「原始表达式」，处理空命令（进多行编辑器）和 `--repl`，最终调用上面的 `EvaluateExpression`：

[CommandObjectExpression.cpp:589-603](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectExpression.cpp#L589-L603) —— `DoExecute` 切分选项与表达式。

```cpp
void CommandObjectExpression::DoExecute(llvm::StringRef command,
                                        CommandReturnObject &result) {
  m_fixed_expression.clear();
  ...
  if (command.empty()) { GetMultilineExpression(); ... return; }

  OptionsWithRaw args(command);
  llvm::StringRef expr = args.GetRawPart();
  if (args.HasArgs()) {
    if (!ParseOptionsAndNotify(args.GetArgs(), result, m_option_group, exe_ctx))
      return;
    ...
  }
  ...
```

[CommandObjectExpression.cpp:683-699](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectExpression.cpp#L683-L699) —— 调用求值；若应用了 Fix-It，把修正后的完整命令追加进 `CommandHistory`（本讲实践的观察点）：

```cpp
if (EvaluateExpression(expr, result.GetOutputStream(),
                       result.GetErrorStream(), result)) {
  if (!m_fixed_expression.empty() && target->GetEnableNotifyAboutFixIts()) {
    CommandHistory &history = m_interpreter.GetCommandHistory();
    std::string fixed_command("expression ");
    ... fixed_command.append(m_fixed_expression);
    history.AppendString(fixed_command);
  }
  return;
}
```

#### 4.3.4 代码实践（可运行，本讲主实践）

**实践目标**：跟踪 `expression` 从「选项构建」到「调用 `Target::EvaluateExpression`」的完整路径，并用命令历史验证一次实际调用。

**操作步骤**：

1. 编译一个带调试信息的小程序，例如：

   ```c
   // demo.c —— 示例代码，非项目源码
   #include <stdio.h>
   int main(void) {
     int x = 40, y = 2;
     printf("%d\n", x + y);   // 在这行设断点
     return 0;
   }
   ```

   用 `cc -g demo.c -o demo` 编译（待本地验证具体工具链）。

2. 在源码 `CommandObjectExpression.cpp` 的 `EvaluateExpression`（约 408 行）入口与 `target.EvaluateExpression(...)`（约 434 行）调用处各打一个「心智断点」——记住这两个函数名。

3. 启动调试：`lldb demo`，然后：

   ```text
   (lldb) breakpoint set -f demo.c -l 5
   (lldb) run
   (lldb) expression x + y
   (lldb) expression int $z = x * y    ; 带用户变量
   (lldb) history
   ```

4. 观察第 3 步 `history` 的输出（它是 `session history` 的别名）。记录你看到的关键命令。

**需要观察的现象**：

- `expression x + y` 应直接打印 `42`（`int` 的和）。
- `history` 列出本会话执行过的命令，其中应能看到 `expression x + y` 这一行。若该表达式触发了 Fix-It（例如你故意写一个可被自动修正的小错误），`history` 里还会出现一条以 `expression ` 开头、带修正后文本的条目——这正是源码 [CommandObjectExpression.cpp:686-698](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectExpression.cpp#L686-L698) 里 `history.AppendString(fixed_command)` 的运行时体现。

**预期结果**：你能对照源码说出——`expression x + y` 这条文本，先被 `DoExecute` 用 `OptionsWithRaw` 切出 `expr = "x + y"`，再由 `EvaluateExpression` 组装 `EvaluateExpressionOptions`，最终调用 `Target::EvaluateExpression`，返回的 `ValueObjectSP` 被 `Dump` 成屏幕上的 `42`。记录的关键函数名应至少包括：`DoExecute`、`EvaluateExpression`（命令层私有方法）、`GetEvaluateExpressionOptions`、`Target::EvaluateExpression`。

**待本地验证**：具体编译命令、行号、Fix-It 是否触发取决于你的编译器与程序，请以本地实际输出为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `expression` 是 `CommandObjectRaw` 而不是 `CommandObjectParsed`？

**参考答案**：因为表达式体可能包含 `-`、空格、`--`、引号等会被 getopt 误判为选项的字符（例如 `expr -- my_var - 1` 里的 `- 1`）。原始命令用 `OptionsWithRaw` 把「`--` 之前的选项」和「`--` 之后（或第一个非选项起）的原始表达式」切开，确保表达式串原样到达求值器。注意 `DoExecute` 的签名是 `llvm::StringRef command`（整串），而 `CommandObjectParsed` 拿到的是已被切词的 `Args &command`。

**练习 2**：`expr -i false`（不忽略断点）会改变求值选项里的什么？

**参考答案**：`SetOptionValue` 的 `'i'` 分支把 `ignore_breakpoints` 置为 false；`GetEvaluateExpressionOptions` 据此 `options.SetIgnoreBreakpoints(false)`。此外，由于「可能停下来」，还会触发 `options.SetGenerateDebugInfo(true)`（见 [CommandObjectExpression.cpp:236-237](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectExpression.cpp#L236-L237)），以便表达式执行撞到断点时能看清栈。

**练习 3**：命令层 `EvaluateExpression` 与 `Target::EvaluateExpression` 是同一个东西吗？

**参考答案**：不是。前者是 `CommandObjectExpression` 的私有成员（[CommandObjectExpression.cpp:408](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectExpression.cpp#L408)），负责「组装选项 + 打印结果」；后者是 `Target` 的方法（[Target.h:1539](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/Target/Target.h#L1539)），负责真正的「解析 + 编译 + JIT/解释」。命令层的方法在内部调用 `Target` 的方法。

---

### 4.4 CommandObjectDisassemble：第三种形态与命令—插件协作

#### 4.4.1 概念说明

`disassemble` 用来反汇编指定地址范围的指令。把它放进本讲有两个作用：

1. **补齐三种命令形态**：它是标准的 `CommandObjectParsed`（选项 + 无位置参数），与 `breakpoint set`（parsed）、`expression`（raw）放在一起，正好凑齐「parsed / raw / multiword」三种形态。
2. **演示命令如何与「插件」协作**：`breakpoint` 命令调用 `Target`（`Target` 内部再去用断点插件），而 `disassemble` 命令更直接——它直接调用 `Disassembler` 这个插件体系的静态入口 `Disassembler::FindPlugin` / `Disassembler::Disassemble`。这呼应了 u1-l2 讲过的「LLDB 能力以插件形式组织」。

#### 4.4.2 核心流程

```text
1. [框架层] eCommandRequiresTarget —— 必须有真实 target 才能反汇编。

2. [DoExecute] 确定架构（用 -arch 或 target 架构）
     -> Disassembler::FindPlugin(arch, flavor, cpu, features, plugin_name)
        返回一个具体架构的反汇编器插件实例（如 ARM/x86）

3. 根据 -b/-m/-k/-r 等选项组装 Disassembler 输出标志位 options

4. GetRangesForSelectedMode(result) 算出要反汇编的 AddressRange 列表
   （当前函数 / 起止地址 / 指定指令数 / 某符号 等）

5. 对每个 range 调用 Disassembler::Disassemble(...)，
   反汇编结果直接写入 result.GetOutputStream()
```

#### 4.4.3 源码精读

构造函数声明它是 `CommandObjectParsed` 且要求真实 target：

[CommandObjectDisassemble.cpp:234-241](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectDisassemble.cpp#L234-L241) —— 构造函数：

```cpp
CommandObjectDisassemble::CommandObjectDisassemble(CommandInterpreter &interpreter)
    : CommandObjectParsed(interpreter, "disassemble", "...",
                          "disassemble [<cmd-options>]", eCommandRequiresTarget) {}
```

`DoExecute` 先确定架构并选择反汇编器插件，若找不到支持该架构的插件就直接报错：

[CommandObjectDisassemble.cpp:486-487](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectDisassemble.cpp#L486-L487) —— 选择插件：

```cpp
DisassemblerSP disassembler = Disassembler::FindPlugin(
    m_options.arch, flavor_string, cpu_string, features_string, plugin_name);
```

随后把各布尔选项拼成一组位标志 `options`，算出地址范围，最后调用静态分发函数 `Disassembler::Disassemble` 把结果写进 `result`：

[CommandObjectDisassemble.cpp:522-563](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectDisassemble.cpp#L522-L563) —— 组装标志、循环反汇编（节选）：

```cpp
uint32_t options = Disassembler::eOptionMarkPCAddress;
if (m_options.show_mixed)  options |= Disassembler::eOptionMarkPCSourceLine;
if (m_options.show_bytes)  options |= Disassembler::eOptionShowBytes;
...
llvm::Expected<std::vector<AddressRange>> ranges =
    GetRangesForSelectedMode(result);
...
for (AddressRange cur_range : *ranges) {
  Disassembler::Limit limit;
  if (m_options.num_instructions == 0) {
    limit = {Disassembler::Limit::Bytes, cur_range.GetByteSize()};
    if (limit.value == 0) limit.value = default_disasm_byte_size; // 32
  } else {
    limit = {Disassembler::Limit::Instructions, m_options.num_instructions};
  }
  if (Disassembler::Disassemble(GetDebugger(), m_options.arch, plugin_name,
          ..., cur_range.GetBaseAddress(), limit, ..., options,
          result.GetOutputStream()))
    result.SetStatus(eReturnStatusSuccessFinishResult);
  ...
}
```

> 注意第 554 行的 `default_disasm_byte_size = 32`（定义在文件顶部 [CommandObjectDisassemble.cpp:26](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectDisassemble.cpp#L26)）：这就是「不给定范围时默认反汇编 32 字节」这一行为的来源。读源码能直接定位到这种「魔法数字」。

#### 4.4.4 代码实践（源码阅读型）

**实践目标**：理解命令如何直接驱动插件，并定位一个默认行为的来源。

**操作步骤**：

1. 在 `CommandObjectDisassemble.cpp:468` 的 `DoExecute` 旁，标注它调用了哪些 `Disassembler::` 静态方法（`FindPlugin`、`Disassemble`）。
2. 找到 `default_disasm_byte_size` 与 `default_disasm_num_ins`（文件顶部约 26-27 行），理解它们分别是「按字节」和「按指令数」两种模式下的默认上限。
3. （可选，待本地验证）在 lldb 里对一个停止的程序执行 `disassemble`（不带任何选项），数一下默认输出了多少字节 / 多少条指令，与源码默认值对照。

**需要观察的现象**：`disassemble` 命令几乎不包含反汇编「算法」——它只负责「选插件 + 算范围 + 调静态函数 + 写输出流」。

**预期结果**：你能解释「为什么默认反汇编字节数是 32」——因为命令层把这个常量作为 `Limit::Bytes` 的回退值。

#### 4.4.5 小练习与答案

**练习 1**：`disassemble` 与 `breakpoint set` 都属于 `CommandObjectParsed`，它们处理「位置参数」的方式有何不同？

**参考答案**：`disassemble` **不接受位置参数**——它的 `DoExecute` 在 `command` 非空时直接报错 `"disassemble" arguments are specified as options`（见 [CommandObjectDisassemble.cpp:507-516](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectDisassemble.cpp#L507-L516)），所有输入都必须以选项形式给出。而 `breakpoint set` 同样主要靠选项，但二者都属于 parsed 形态；差异在业务约束而非框架。

**练习 2**：为什么说 `disassemble` 比 `breakpoint set` 更「贴近插件」？

**参考答案**：`breakpoint set` 调用的是 `Target` 的方法，由 `Target` 内部再去用断点 / 解析器插件；而 `disassemble` 在命令层就直接调用 `Disassembler::FindPlugin` 和 `Disassembler::Disassemble` 这两个插件体系的总入口。换句话说，`disassemble` 命令层自己就完成了「选插件」这一步。

---

## 5. 综合实践：命令路径与 SB API 路径殊途同归

本讲最重要的结论是：**命令层直接操作 `lldb_private` 内部对象（`Target` 等），而 SB API（`SBTarget` 等）是这些内部方法的公共封装——两条路径最终汇聚到同一批内部方法上。** 下面用一个综合任务把这条结论亲手验证一遍。

**任务**：用「命令」和「Python SB API」两种方式各设一个断点，证明它们落在同一个 `Target` 上。

**操作步骤**（待本地验证）：

1. 准备 `demo.c`（同 4.3.4），`cc -g demo.c -o demo`。

2. 启动 `lldb demo`，执行：

   ```text
   (lldb) breakpoint set -n main        ; 命令路径：CommandObjectBreakpointSet
   ```

   这会走本讲 4.2 讲的链路：选项 `-n main` → `eSetTypeFunctionName` → `Target::CreateBreakpoint(...)`。

3. 接着用 Python SB API 再设一个：

   ```text
   (lldb) script
   >>> import lldb
   >>> tgt = lldb.debugger.GetSelectedTarget()
   >>> bp = tgt.BreakpointCreateByName('printf', 'demo')   ; SB API 路径
   >>> print(bp.GetNumLocations())
   >>> quit()
   (lldb) breakpoint list
   ```

4. `breakpoint list` 应同时列出步骤 2 和步骤 3 设置的两个断点。

**需要观察的现象**：命令设置的断点（`breakpoint set -n main`）和 Python SB API 设置的断点（`BreakpointCreateByName`）在同一个 `breakpoint list` 里和平共处，编号连续。

**这说明什么**：`CommandObjectBreakpointSet::DoExecute` 调用的 `target->CreateBreakpoint(...)`，与 `SBTarget::BreakpointCreateByName` 背后调用的 `Target::CreateBreakpoint(...)`，本质是同一个 `Target` 对象上的同一族方法。命令层和脚本层只是两个不同的「前端」，共享同一个调试引擎后端。这正是 u2（SBAPI）与本讲（命令系统）的交汇点。

**进阶（可选）**：把 `breakpoint set -n main` 换成等价的 `expression`（例如 `expr` 一个有副作用的调用），对照本讲 4.3 的源码，指出命令层的 `Target::EvaluateExpression` 与 `SBTarget::EvaluateExpression` 同样是同一方法。

---

## 6. 本讲小结

- **定位命令**：任意一条命令都能通过 `CommandInterpreter::LoadCommandDictionary()`（[CommandInterpreter.cpp:575](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Interpreter/CommandInterpreter.cpp#L575)）反查到它的 `CommandObject` 类，进而找到 `DoExecute`。
- **三种形态齐全**：`breakpoint`（multiword 容器）、`expression`（raw 原始命令）、`disassemble`（parsed 解析命令）正好覆盖 u3-l2 讲过的三种 `CommandObject` 形态。
- **命令层 = 翻译层**：命令实现只做三件事——解析选项、判断用户意图、调用 `Target`/`Process`/插件。真正的调试逻辑（断点解析、表达式 JIT、反汇编算法）都在被调用的模块里。
- **`breakpoint set` 用优先级瀑布把「选项组合」翻译成「断点类型」**，再 switch 调用 `Target` 的对应 `CreateBreakpoint` 重载（[CommandObjectBreakpoint.cpp:1800-2026](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectBreakpoint.cpp#L1800-L2026)）。
- **`expression` 用 `OptionsWithRaw` 切出原始表达式**，组装 `EvaluateExpressionOptions` 后调用 `Target::EvaluateExpression`（[CommandObjectExpression.cpp:428-435](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Commands/CommandObjectExpression.cpp#L428-L435)）。
- **命令与 SB API 殊途同归**：命令层直接调 `lldb_private::Target` 的方法，SB API 是同一批方法的公共封装；二者共享同一个调试引擎后端。

---

## 7. 下一步学习建议

本讲把「命令文本 → 内部对象」这条链打通了，但被命令调用的内部模块我们还只看到了「入口签名」。建议按以下顺序继续：

1. **`Target` 内部到底如何创建断点**：阅读 `source/Breakpoint/Breakpoint.cpp` 与 `source/Target/Target.cpp` 里的 `CreateBreakpoint` 系列，这是 u9-l1（断点模型与解析）的主题。你会看到断点如何交给 `BreakpointResolver` 在共享库里持续解析新位置。
2. **`Target::EvaluateExpression` 内部的求值流水线**：进入 `source/Expression/` 与 `source/Plugins/ExpressionParser/Clang/`，看表达式如何被 Clang 编译、生成 IR、JIT 或解释执行——这是 u7（表达式求值）的主题。
3. **自己写一条命令**：等学到 u11-l2（编写 LLDB 命令插件）时，你会用 `CommandObject` 基类亲手实现一条新命令；届时回看本讲，你会发现「选项解析 + DoExecute + ReturnObject」这套骨架正是你要复用的。
4. **如果想横向对照**：随手挑一条命令（如 `source/Commands/CommandObjectFrame.cpp` 里的 `frame`），用本讲 4.1 的方法定位它的 `DoExecute`，检验你是否已能独立阅读任意命令。
