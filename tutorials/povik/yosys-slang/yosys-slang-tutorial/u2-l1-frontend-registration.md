# Yosys 前端注册与 read_slang 命令

## 1. 本讲目标

学完本讲后，你应该能够：

- 理解 Yosys 的 **Frontend / Pass 插件机制**：一段 C++ 是如何变成一条可在脚本里调用的命令的。
- 说明 `read_slang` 这条命令名是怎么从 `SlangFrontend` 这个类「注册」出来的，以及它为何能覆盖 Yosys 内置版本。
- 在 [`SlangFrontend::execute`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3674) 这一个方法里，定位出「解析源码 → 创建 Compilation → 遍历 PopulateNetlist → 调用 proc」这四个阶段的真实代码行，并说清楚每个阶段失败时的行为。
- 认识两个配套命令 `slang_version` 与 `slang_defaults` 的作用。

本讲是单元 2（入口与整体流水线）的总纲。后面 u2-l2、u2-l3、u2-l4 会分别把 slang driver、`SynthesisSettings`、诊断系统拆开细讲；本讲先把它们串成一条主线，让你「先看到森林，再研究树木」。

## 2. 前置知识

在阅读本讲前，建议你已经建立以下认知（来自 u1-l1 及入门单元）：

- sv-elab（原名 yosys-slang）把 SystemVerilog **精化（elaborate）** 成字级网表。词法/语法/语义分析由内嵌的 slang 库完成，本仓库负责把 slang 产出的 AST 翻译成 Yosys 的 RTLIL 网表。
- 它对用户暴露的命令叫 `read_slang`；既可作为 Yosys（v0.67+）内置组件，也可作为 `slang.so` 插件加载（`yosys -m slang` 或 `plugin -i slang`），加载后会覆盖内置版本。

此外需要一点 Yosys 的背景直觉：

- **RTLIL** 是 Yosys 内部的中间表示（Register-Transfer-Level Intermediate Language）。所有前端（Verilog、RTLIL、BLIF……）的目标都是把设计翻译进 RTLIL 的 `Design`/`Module` 结构。
- **Pass** 是 Yosys 里「对当前设计做一次变换或分析」的命令，比如 `proc`、`opt`、`synth`。**Frontend** 是一种特殊的 Pass，专门负责「读入某种外部格式并生成 RTLIL」。
- slang 的 `driver::Driver` 是 slang 自己提供的命令行驱动，负责解析参数、读源文件、创建 `Compilation`。sv-elab 复用它而不是自己重写一遍。

> 名词速查：**AST**（Abstract Syntax Tree，抽象语法树）是源码解析后的树形结构；**Compilation** 是 slang 把所有编译单元组装起来、做完语义分析后的「设计对象」，里面挂着可遍历的符号树。

## 3. 本讲源码地图

本讲几乎全部内容集中在一个文件里：

| 文件 | 作用 |
|------|------|
| [src/slang_frontend.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc) | 主入口文件。定义了 `SlangFrontend`（前端）、`SlangVersionPass`、`SlangDefaultsPass`（两条配套命令），以及 `execute()` 这条端到端流水线。还包含 `PopulateNetlist`、`HierarchyQueue` 等核心结构（后续讲义细讲）。 |
| [src/slang_frontend.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h) | 头文件，声明 `SynthesisSettings`、`NetlistContext`、`RTLILBuilder` 等公共类型。本讲主要用到它来理解选项如何挂到 driver 上。 |
| [src/diag.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc) | 诊断系统的实现，`diag::setup_messages` 在这里。本讲只点一下它在哪里被调用。 |

只引用真实存在的文件；所有行号均对应当前 HEAD `3dddccd`。

## 4. 核心概念与源码讲解

本讲按三个最小模块推进：

1. **4.1 SlangFrontend**：类是如何变成 `read_slang` 命令的。
2. **4.2 execute()**：一次 `read_slang` 调用从入口到出口的全流程。
3. **4.3 SlangVersionPass / SlangDefaultsPass**：两个配套小命令。

### 4.1 SlangFrontend：注册为 Yosys 前端

#### 4.1.1 概念说明

Yosys 的扩展模型非常简洁：**用一个全局静态对象来「自我注册」**。你写一个继承自 `Yosys::Pass`（或 `Yosys::Frontend`）的类，在文件作用域里声明一个该类的全局实例，它的构造函数就会把这个命令登记进 Yosys 的命令注册表。当插件 `.so` 被 `dlopen` 加载（或代码被编译进 Yosys）时，这些全局对象的构造函数自动执行，命令就出现了。

`Frontend` 和普通 `Pass` 的区别在于：

- `Frontend` 注册的命令名前会被 Yosys 自动加上 `read_` 前缀。所以一个注册名为 `"slang"` 的 Frontend，用户命令就是 `read_slang`。
- `Frontend::execute` 的签名多了一个 `std::istream *&f` 和 `filename`，因为 Yosys 默认会帮前端打开文件、提供一个输入流。但 sv-elab 自己用 slang driver 读文件，所以它把这两个参数显式丢弃了（见 4.1.3）。

#### 4.1.2 核心流程

```
插件 .so 被 dlopen（或内置编译）
        │
        ▼
全局对象 SlangFrontend 的构造函数执行
        │  调用基类 Frontend("slang", "read SystemVerilog (slang)")
        ▼
Yosys 命令注册表新增一条：命令名 slang，描述 "read SystemVerilog (slang)"
        │
        ▼
用户在脚本里敲 read_slang
        │  Yosys 识别 read_ 前缀 → 找到 slang 前端
        ▼
调用 SlangFrontend::execute(f, filename, args, design)
```

关键点：`replace_existing_pass()` 返回 `true`，表示如果注册表里已有同名命令（比如 Yosys v0.67+ 内置的 sv-elab），就用插件版本**覆盖**它。这正是 README 里「插件不与内置版本冲突，而是覆盖它」的实现原理。

#### 4.1.3 源码精读

类的定义只有几行，但信息密度很高：

[ src/slang_frontend.cc:3549-3552](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3549-L3552)

```cpp
struct SlangFrontend : Frontend {
	SlangFrontend() : Frontend("slang", "read SystemVerilog (slang)") {}

	bool replace_existing_pass() const override { return true; }
```

- 第 3550 行：构造时把名字注册为 `"slang"`，于是用户命令是 `read_slang`；第二个字符串是命令描述。
- 第 3552 行：`replace_existing_pass()` 返回 `true`，使插件能覆盖内置实现。

紧接着是 `help()`，它决定 `help read_slang` 的输出：

[ src/slang_frontend.cc:3596-3597](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3596-L3597) 打印出命令用法 `read_slang [options] [filename]`。help 里还调用了 `driver.addStandardArgs()` 与 `settings.addOptions(...)`，把 slang 的标准参数和 sv-elab 自己的选项（如 `dump_ast`、`keep_hierarchy`）一起列出来——这部分由 u2-l3 详讲。

最关键的注册动作其实在文件末尾、类定义结束处：

[ src/slang_frontend.cc:3835](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3835) `} SlangFrontend;` —— 这一行在结构体定义结尾的同时**声明了一个全局实例**。正是这个实例的构造完成了注册。下面的 `SlangVersionPass`、`SlangDefaultsPass` 也都用了同样的 `} XXX;` 写法。

再看 `execute` 的签名与开头：

[ src/slang_frontend.cc:3674-3678](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3674-L3678)

```cpp
void execute(std::istream *&f, std::string filename, std::vector<std::string> args, RTLIL::Design *design) override
{
	(void) f;
	(void) filename;
	log_header(design, "Executing SLANG frontend.\n");
```

`(void) f; (void) filename;` 这两行很有意味：Yosys 想帮前端把文件打开好，但 sv-elab **不领情**——它要自己用 slang driver 读文件（这样才能支持 here-document、命令行直接列多个 `.v`、`-D` 宏定义等 slang 风格的用法）。所以它把 Yosys 传入的流和文件名显式丢弃，转而在 `args` 里自己解析。

> 补充：`read_heredoc` ([ src/slang_frontend.cc:3610-3648](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3610-L3648)) 就是用来支持 `read_slang <<EOF ... EOF` 这种「把源码内嵌在脚本里」的写法。在测试用例 `tests/unit/dff.ys` 里你会大量看到它。

#### 4.1.4 代码实践

**实践目标**：亲手验证「类 → 命令」这条注册链路确实存在，并理解覆盖行为。

**操作步骤**（源码阅读型，无需编译）：

1. 打开 [src/slang_frontend.cc:3549](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3549)，确认 `SlangFrontend` 继承自 `Frontend`，注册名为 `"slang"`。
2. 跳到 [src/slang_frontend.cc:3835](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3835)，确认末尾 `} SlangFrontend;` 创建了全局实例。
3. 在仓库里全局搜索 `read_slang` 这个**字符串字面量**（注意不是命令用法）。你会发现它只出现在 `help()` 的 `log(...)` 文本和文档/测试里，而**不在任何注册调用里**。

**需要观察的现象**：代码里没有一处形如 `register("read_slang", ...)` 的显式注册；命令名是由 `Frontend("slang", ...)` 的名字 + Yosys 的 `read_` 前缀约定共同产生的。

**预期结果**：你应当能向别人解释——「`read_slang` 这个命令名里，`read_` 是 Yosys 给所有 Frontend 加的前缀，`slang` 才是我们在构造函数里登记的名字」。

**待本地验证**（如果你装了带 sv-elab 的 yosys）：运行 `yosys -p "help read_slang"`，确认 help 文本与本讲引用的 [ src/slang_frontend.cc:3597](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3597) 输出一致。

#### 4.1.5 小练习与答案

**练习 1**：如果把构造函数改成 `Frontend("mysv", "...")`，用户命令会变成什么？  
**答案**：变成 `read_mysv`。命令名 = `read_` + Frontend 注册名。

**练习 2**：为什么 `SlangFrontend::execute` 要写 `(void) f; (void) filename;`？  
**答案**：因为 sv-elab 不使用 Yosys 预先打开的输入流，而是自己用 slang driver 读源码（支持 heredoc、多文件、`-D` 等）。显式 `(void)` 既表示「故意不用」，也消除「未使用参数」的编译警告。

---

### 4.2 execute()：read_slang 的端到端流水线

#### 4.2.1 概念说明

`SlangFrontend::execute` 是整个 sv-elab 的「主函数」。它接收一行命令的参数（`args`）和一个空的 RTLIL `Design`，要把 SystemVerilog 源码变成填好的 `Design`。

它的责任可以切成两大段：

1. **精化段（slang 主导）**：解析源码 → 建立 `Compilation` → 报告并检查诊断。这一段几乎全靠 slang driver 完成；sv-elab 只是在外围做参数修补（`fixup_options`）和黑盒导入。
2. **翻译段（sv-elab 主导）**：遍历 slang AST → 用 `PopulateNetlist` 把每个实例体翻译成 RTLIL 模块 → 收集诊断。这一段才是本仓库的核心代码。

翻译段之后，sv-elab 还会顺手调用一组 Yosys 内置的 `proc` 系列命令，把刚生成的 RTLIL `Process`（过程块表示）「降级」成纯组合/时序逻辑，方便下游继续处理。这一步可以通过 `no_proc` 选项关闭。

> 为什么要区分这两段？因为 slang 的产出（AST）是「语言层面」的，而 RTLIL 是「电路层面」的。两段之间通过 `Compilation` 和 `topInstances` 衔接：slang 给出符号树，sv-elab 从顶层实例开始往下访问。

#### 4.2.2 核心流程

下面是 `execute()` 的流程骨架（行号对应 4.2.3 的源码）：

```
[阶段0 参数] driver.addStandardArgs + SynthesisSettings.addOptions + diag::setup_messages
             read_heredoc + 注入 default_options + driver.parseCommandLine
             fixup_options → processOptions → catch_forbidden_options
                          │
[阶段1 解析源码]   driver.parseAllSources()       ──失败→ log_error "Parsing failed"
                          │
[阶段2 创建Compilation] driver.createCompilation()
             import_blackboxes_from_rtlil (可选)
             dump_ast (可选)
             reportCompilation + 检查诊断  ──有错→ log_error "Design elaboration failed" 并 return
                          │
[阶段3 遍历PopulateNetlist] 设置 global_compilation/global_sourcemgr
             建 HierarchyQueue，为每个 topInstance get_or_emplace 出一个 NetlistContext
             对队列里每个 netlist：
                 PopulateNetlist populate(hqueue, netlist);
                 netlist.realm.visit(populate);   ← 真正翻译 AST→RTLIL
             收集 populate/netlist 的诊断，再次 reportDiagnostics
                          │
[阶段4 调用proc]  if (!no_proc)：
             构造只含本次产出模块的 selection
             call("proc_clean"); call("tribuf"); ... ; call("opt_expr -keepdc")
```

整段翻译逻辑被包在 `try { ... } catch (const std::exception& e) { log_error(...); }` 里，任何未捕获的 C++ 异常都会被转成一条 Yosys 错误，而不是让进程直接崩溃。

#### 4.2.3 源码精读

**(a) 阶段 0：参数装配** [ src/slang_frontend.cc:3683-3714](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3683-L3714)

```cpp
slang::driver::Driver driver;
driver.addStandardArgs();
SynthesisSettings settings;
settings.addOptions(driver.cmdLine);
diag::setup_messages(driver.diagEngine);
```

这里同时挂载了「slang 标准参数」（如 `-D`、`--top`、`-I`）和「sv-elab 专属选项」（由 `SynthesisSettings::addOptions` 注册，详见 u2-l3），并初始化诊断引擎。随后：

[ src/slang_frontend.cc:3707-3714](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3707-L3714) 解析命令行，失败则 `log_cmd_error("Bad command")`；接着 `fixup_options` 给 driver 打补丁（比如默认加 `SYNTHESIS=1` 宏、强制关闭未知模块引用等），再 `processOptions`、`catch_forbidden_options`。`fixup_options` 的实现见 [ src/slang_frontend.cc:3477-3522](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3477-L3522)。

注意 [ src/slang_frontend.cc:3695](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3695) 把 `default_options`（由 `slang_defaults -add` 预设的默认参数）插到命令行参数前面——这是 4.3 节 `SlangDefaultsPass` 的落脚点。

**(b) 阶段 1：解析源码** [ src/slang_frontend.cc:3717-3718](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3717-L3718)

```cpp
if (!driver.parseAllSources())
    log_error("Parsing failed; see full log for details\n");
```

`parseAllSources()` 让 slang 完成词法/语法分析。失败时调用 `log_error`——这是 Yosys 的致命错误接口，会抛出异常终止本次命令（注意：它终止的是这次 `read_slang`，而不一定让整个 yosys 进程退出）。那句「see full log for details」提示用户去完整日志里找 slang 的详细报错，这是当前 HEAD 刚刚加上的提示（见 git 提交 `3dddccd "Add hint about full log to error message"`）。

**(c) 阶段 2：创建 Compilation 并校验** [ src/slang_frontend.cc:3720-3751](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3720-L3751)

```cpp
auto compilation = driver.createCompilation();

if (settings.extern_modules.value_or(true))
    import_blackboxes_from_rtlil(driver.sourceManager, *compilation, design);
```

`createCompilation()` 把已解析的编译单元组装成 `ast::Compilation`，完成参数求值、generate 展开、实例化等语义工作（精化的核心）。接着可选地把当前 RTLIL 设计里已有的模块作为黑盒「反向导入」回 slang，这样源码里引用到的未定义模块就能被解析（u7-l3 详讲）。

之后 [ src/slang_frontend.cc:3735-3746](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3735-L3746) 调用 `reportCompilation` 并检查诊断：

```cpp
if (driver.diagEngine.getNumErrors()) {
    // Stop here ... PopulateNetlist requires a well-formed AST without error nodes
    (void) driver.reportDiagnostics(/* quiet */ false);
    if (!in_succesful_failtest)
        log_error("Design elaboration failed; see full log for details\n");
    return;
}
```

注释点明了为什么要在这里硬停：**`PopulateNetlist` 要求一棵没有错误节点的、良构的 AST**。如果带着语法/语义错误继续往下翻译，行为不可预测，所以一旦有 error 就直接 `return`（除非是测试模式下「期望失败」的用例 `in_succesful_failtest`，相关机制见 u2-l4）。

**(d) 阶段 3：遍历 PopulateNetlist（本仓库核心）** [ src/slang_frontend.cc:3753-3795](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3753-L3795)

先缓存两个全局指针，供后续格式化诊断和源码文本使用：

```cpp
global_compilation = &(*compilation);
global_sourcemgr = compilation->getSourceManager();
```

然后构造层次队列，并为每个顶层实例创建一个 `NetlistContext`（RTLIL 模块画布）：

[ src/slang_frontend.cc:3756-3770](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3756-L3770)

```cpp
HierarchyQueue hqueue;
for (auto instance : compilation->getRoot().topInstances) {
    ...
    auto ref_body = &get_instance_body(settings, *instance);
    ...
    auto [netlist, new_] = hqueue.get_or_emplace(ref_body, design, settings,
                                                 *compilation, *ref_body->parentInstance);
    netlist.canvas->attributes[ID::top] = 1;   // 标记顶层模块
}
```

`topInstances` 是 slang 给出的顶层实例列表（可能不止一个，对应多个 `--top`）。`HierarchyQueue::get_or_emplace`（见 [ src/slang_frontend.cc:1696-1718](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1696-L1718)）负责按实例体去重地创建 `NetlistContext`，并把新模块推进队列。

接下来是一个**边遍历边扩张**的循环——这正是「层次队列」模式的精髓：

[ src/slang_frontend.cc:3772-3780](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3772-L3780)

```cpp
for (int i = 0; i < (int) hqueue.queue.size(); i++) {
    NetlistContext &netlist = *hqueue.queue[i];
    ...
    PopulateNetlist populate(hqueue, netlist);
    netlist.realm.visit(populate);
    ...
}
```

注意循环条件用的是 `hqueue.queue.size()`，而在循环体内 `PopulateNetlist` 在翻译子模块实例时还会往 `hqueue` 里追加新的 `NetlistContext`（u7-l2 详讲层次展平）。于是这个 `for` 会一直跑到所有递归发现的模块都被翻译完——用「队列长度当上界」实现了一个动态扩张的工作列表。

`PopulateNetlist` 本身是一个 slang AST 访问者（[ src/slang_frontend.cc:1766-1777](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1766-L1777)），`netlist.realm.visit(populate)` 触发对整个实例体符号树的访问，把端口、线网、过程块、实例等逐个翻译成 RTLIL。这是后续第 3~7 单元的主题。

循环结束后，[ src/slang_frontend.cc:3800-3807](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3800-L3807) 再次上报诊断；若仍有错误则 `log_error`。最外层的 `catch` 把任何逃逸的 `std::exception` 转成 `log_error("Exception: %s", ...)`。

**(e) 阶段 4：调用 proc** [ src/slang_frontend.cc:3809-3833](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3809-L3833)

```cpp
if (!settings.no_proc.value_or(false)) {
    ...
    design->selection_stack.push_back(emitted_modules);  // 只选本次产出的模块
    log_push();
    call(design, "proc_clean");
    call(design, "tribuf");
    call(design, "proc_rmdead");
    call(design, "proc_prune");
    call(design, "proc_init");
    call(design, "proc_rom");
    call(design, "proc_mux");
    call(design, "proc_clean");
    call(design, "opt_expr -keepdc");
    log_pop();
    design->selection_stack.pop_back();
}
```

`call(design, "xxx")` 是 Yosys 提供的「在 C++ 里调用另一条 pass」的接口。这里跑的是标准的 `proc` 降级流水线：把 sv-elab 生成的 RTLIL `Process`（case 树、`$dff` 使能等高层结构）化简成更基础的单元。`selection_stack` 的 push/pop 保证这一串变换**只作用于本次 `read_slang` 新增的模块**，不影响设计里已有的其它模块。`no_proc` 选项可跳过这一步，留给需要保留原始 Process 的场景。

#### 4.2.4 代码实践

**实践目标**（即本讲规格里的核心实践）：在 `execute()` 中精确标注四个阶段，并解释各阶段失败行为。

**操作步骤**：

1. 打开 [src/slang_frontend.cc:3674](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3674)，跟随下面的表格逐行对照。
2. 在本地副本里（不要改原文件）给这四行加注释，标注阶段归属。

**四阶段定位表**：

| 阶段 | 关键代码行 | 失败时的行为 |
|------|-----------|--------------|
| 解析源码 | [3717](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3717) `driver.parseAllSources()` | 返回 false → [3718](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3718) `log_error("Parsing failed; see full log for details")` |
| 创建 Compilation | [3720](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3720) `driver.createCompilation()`，含 [3735](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3735) 的诊断检查 | AST 有 error → [3744](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3744) `log_error("Design elaboration failed...")` 并 `return`（测试期望失败除外） |
| 遍历 PopulateNetlist | [3779-3780](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3779-L3780) `PopulateNetlist populate(...); netlist.realm.visit(populate);` | 翻译期抛 C++ 异常 → [3806](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3806) `log_error("Exception: %s")`；翻译后诊断有错 → [3802](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3802) `log_error` 并 `return` |
| 调用 proc | [3821-3829](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3821-L3829) 一串 `call(design, "proc_*")` | 该阶段不读用户输入；可被 `no_proc` 选项整体跳过 |

**需要观察的现象**：注意阶段 2 与阶段 3 对「失败」的处理差异——阶段 2 失败时**直接 `return`**（设计里什么都没生成），而阶段 3 的异常被 `try/catch` 包住后转成 `log_error`。两者都通过 Yosys 的异常机制终止命令，但路径不同。

**预期结果**：你能不查资料地说出「解析失败看 parseAllSources 的返回值，语义失败看 diagEngine 的错误计数，翻译失败靠 try/catch 兜底」。

#### 4.2.5 小练习与答案

**练习 1**：为什么阶段 3 的 `for` 循环用 `i < hqueue.queue.size()` 而不是先把 size 存下来？  
**答案**：因为循环体里的 `PopulateNetlist` 会在翻译到子模块实例时往 `hqueue` 追加新的 `NetlistContext`，队列长度会增长。用「实时 size」当上界，才能把递归发现的所有模块都翻译到。

**练习 2**：`getNumErrors()` 非零时为什么必须 `return`，不能「尽量继续翻译」？  
**答案**：代码注释写明 `PopulateNetlist requires a well-formed AST without error nodes`。带错误节点的 AST 结构不完整，继续翻译会触发断言失败或产生无意义网表，所以必须在这里硬停。

**练习 3**：`selection_stack` 的 push/pop 在阶段 4 起什么作用？  
**答案**：它把后续 `proc_*` 这一串 pass 的作用范围限定在「本次 `read_slang` 新产出的模块」上，避免改动设计里已有的其它模块；变换完成后 pop 恢复原选择状态。

---

### 4.3 SlangVersionPass / SlangDefaultsPass：两条配套命令

#### 4.3.1 概念说明

sv-elab 除了主命令 `read_slang`，还注册了两条辅助命令，它们都继承自普通 `Pass`（不是 Frontend，所以命令名前**不加** `read_`）：

- **`slang_version`**：打印 sv-elab 与内嵌 slang 的 git 版本号，用于排查「我到底跑的是哪个版本」。
- **`slang_defaults`**：维护一组「默认参数」，让后续每次 `read_slang` 都自动带上这些参数，免去重复书写。支持 `-add`/`-clear`/`-push`/`-pop` 四个子操作。

这两条命令展示了「普通 Pass 的注册方式」——和 Frontend 几乎一样，只是基类不同、命令名不加前缀。

#### 4.3.2 核心流程

**slang_version** 非常简单：

```
用户敲 slang_version
    → SlangVersionPass::execute
    → 校验参数个数
    → log 打印 sv-elab revision + slang revision
```

**slang_defaults** 维护两个文件级静态变量：

```
static std::vector<std::string> default_options;            // 当前默认参数
static std::vector<std::vector<std::string>> defaults_stack; // push/pop 用栈
```

```
slang_defaults -add <opts>   → opts 追加进 default_options
slang_defaults -clear        → default_options.clear()
slang_defaults -push         → 把当前 default_options 压栈（不清空）
slang_defaults -pop          → 栈顶出栈覆盖回 default_options
```

下次 `read_slang` 执行时，[ src/slang_frontend.cc:3695](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3695) 的 `args.insert(..., default_options...)` 会把这些默认参数插到命令行最前面，从而生效。

#### 4.3.3 源码精读

**SlangVersionPass** [ src/slang_frontend.cc:3449-3472](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3449-L3472)

```cpp
struct SlangVersionPass : Pass {
	SlangVersionPass() : Pass("slang_version", "display revision of slang frontend") {}
	bool replace_existing_pass() const override { return true; }
	...
	void execute(std::vector<std::string> args, [[maybe_unused]] RTLIL::Design *d) override
	{
		if (args.size() != 1)
			cmd_error(args, 1, "Extra argument");
		log("sv-elab revision %s\n", YOSYS_SLANG_REVISION);
		log("slang revision %s\n", SLANG_REVISION);
	}
} SlangVersionPass;
```

注意它的 `execute` 签名是 `(args, design)`——普通 Pass 的标准签名，比 Frontend 少了流和文件名。`YOSYS_SLANG_REVISION` 与 `SLANG_REVISION` 是构建期注入的宏（来自 `version.h.in`，u8-l3 详讲）。同样以 `} SlangVersionPass;` 完成注册，同样 `replace_existing_pass()` 返回 `true` 以覆盖内置同名命令。

**SlangDefaultsPass** [ src/slang_frontend.cc:3837-3890](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3837-L3890)，其 execute 的核心分支：

[ src/slang_frontend.cc:3868-3888](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3868-L3888)

```cpp
if (args[1] == "-add") {
    default_options.insert(default_options.end(), args.begin() + 2, args.end());
} else {
    ...
    if (args[1] == "-clear") {
        default_options.clear();
    } else if (args[1] == "-push") {
        defaults_stack.push_back(default_options);
    } else if (args[1] == "-pop") {
        if (!defaults_stack.empty()) {
            default_options.swap(defaults_stack.back());
            defaults_stack.pop_back();
        } else {
            default_options.clear();
        }
    } else {
        cmd_error(args, 1, "Unknown option");
    }
}
```

而它写入的 `default_options` 就是在 [ src/slang_frontend.cc:3474](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3474) 声明的文件级静态变量，由 `SlangFrontend::execute` 在 [ src/slang_frontend.cc:3695](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3695) 消费。两条命令通过这个全局变量形成「写入—读取」的耦合。

#### 4.3.4 代码实践

**实践目标**：用真实命令体会「普通 Pass 的注册 + 默认参数的传递」。

**操作步骤**（待本地验证，需装好带 sv-elab 的 yosys）：

1. 运行 `yosys -p "slang_version"`，对照 [ src/slang_frontend.cc:3469-3470](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3469-L3470)，确认输出两行版本号。
2. 写一个最小脚本验证 `slang_defaults` 的传递：
   ```
   slang_defaults -add --top mytop
   read_slang some_design.sv
   slang_defaults -clear
   ```
3.（源码阅读型，可不运行）追踪：`-add` 把 `--top mytop` 写进 `default_options` → 下一次 `read_slang` 在 [ src/slang_frontend.cc:3695](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3695) 把它插到 `args` 前面 → slang driver 解析时就像用户自己写了 `--top mytop` 一样。

**需要观察的现象**：`slang_defaults -push` 后再 `-add`，最后 `-pop` 应恢复成 push 之前的状态；如果对空栈 `-pop`，代码会走 `default_options.clear()` 分支（见 [ src/slang_frontend.cc:3879-3884](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3879-L3884)）。

**预期结果**：你能说清 `slang_defaults` 不是「立即生效的解析」，而是「往一个全局列表里存字符串，等下次 `read_slang` 时注入」。

#### 4.3.5 小练习与答案

**练习 1**：`slang_version` 是 Frontend 吗？它的命令名为什么没有 `read_` 前缀？  
**答案**：不是，它继承自 `Pass` 而非 `Frontend`。`read_` 前缀只对 Frontend 自动添加，普通 Pass 的命令名就是构造函数里登记的名字 `slang_version`。

**练习 2**：`slang_defaults -pop` 作用于空栈时会发生什么？  
**答案**：见 [ src/slang_frontend.cc:3879-3884](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3879-L3884)，因为 `defaults_stack` 为空，走 else 分支调用 `default_options.clear()`，即清空当前默认参数，而不是报错。

---

## 5. 综合实践

把本讲三块内容串起来，完成下面这个「读源码 + 画时序」的小任务：

**任务**：选取仓库里一个真实测试用例 `tests/unit/dff.ys`（本讲已引用），完成以下三件事：

1. **命令定位**：在 [src/slang_frontend.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc) 里找出 `read_slang` 是怎么从 `Frontend("slang", ...)` 注册出来的，并解释为何脚本里写成 `read_slang` 而不是 `read frontend`。
2. **流水线追踪**：`dff.ys` 第一段 `read_slang <<EOF ... EOF` 会触发 `SlangFrontend::execute`。请按本讲 4.2.4 的四阶段表，逐阶段说出：heredoc 里的源码在哪个阶段被解析？顶层模块 `dff_iff01_gate` 在哪个阶段被翻译成 RTLIL？翻译完后又走了哪一串 `proc_*`？
3. **失败路径推演**：假设你故意把 heredoc 里的 `module` 拼错成 `modul`，让 slang 报语法错。请指出：错误最先在哪个阶段被捕获（阶段 1 的 `parseAllSources` 还是阶段 2 的 `getNumErrors`）？最终用户看到的错误信息里为什么会有「see full log for details」？这条提示对应源码的哪一行？

**交付物**：一张包含上述三问答案的简表或简短文字。完成后，你应当能把「一行 `read_slang` 命令」与「`execute()` 里的几十行代码」一一对应起来，这正是后续 u2-l2~u2-l4 继续深入 slang driver、选项系统和诊断系统的起点。

> 提示：第 3 问里，词法/语法错误通常由 `parseAllSources` 阶段就上报；但 sv-elab 把「是否致命」的判断集中在阶段 2 的 `getNumErrors()` 检查处。请结合 [ src/slang_frontend.cc:3739-3745](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3739-L3745) 给出你的判断（具体在哪一步终止，可「待本地验证」）。

## 6. 本讲小结

- sv-elab 通过继承 `Yosys::Frontend` 并在文件作用域声明全局实例 `SlangFrontend` 来注册命令；`Frontend("slang", ...)` 的名字加上 Yosys 的 `read_` 前缀，得到用户命令 `read_slang`。
- `replace_existing_pass()` 返回 `true`，使 `slang.so` 插件能覆盖 Yosys 内置的同名实现。
- `SlangFrontend::execute` 是端到端流水线，分四大段：**参数装配 → 解析源码（slang）→ 创建 Compilation 并校验 → 遍历 PopulateNetlist 翻译成 RTLIL**，最后再跑一串 `proc_*` 把 `Process` 降级。
- 关键阶段都有明确的失败处理：`parseAllSources` 失败、`getNumErrors` 非零、翻译期异常分别走不同的 `log_error` 路径；阶段 3 的循环用「队列实时长度」当上界，支持层次展平时动态扩张。
- `execute` 显式丢弃 Yosys 提供的输入流 `(void) f; (void) filename;`，改由 slang driver 自行读源码，从而支持 heredoc、多文件、`-D` 等 slang 风格用法。
- `slang_version`（打印版本）和 `slang_defaults`（维护默认参数）是两条普通 `Pass` 命令；后者通过文件级静态变量 `default_options` 与 `read_slang` 耦合。

## 7. 下一步学习建议

本讲只画出了 `execute()` 的「骨架」，把其中每个零件都当成黑盒。接下来建议：

- **u2-l2 slang 驱动流水线**：深入 `driver.parseAllSources` / `createCompilation` / `getRoot().topInstances` 这条 slang 侧的数据流，理解 sv-elab 消费的那棵 AST 是怎么长出来的。
- **u2-l3 SynthesisSettings**：把本讲一带而过的 `settings.addOptions`、`fixup_options`、`dump_ast`/`no_proc`/`keep_hierarchy` 等选项逐一讲清。
- **u2-l4 诊断系统**：展开 `diag::setup_messages`、`check_diagnostics` 与 `in_succesful_failtest`，理解「期望失败」的测试模式是如何与本讲的错误终止逻辑配合的。

如果你更想先看「翻译段」内部，可以跳到单元 3（`NetlistContext`/`RTLILBuilder`/`Variable`）和单元 5（`ProceduralContext`/`StatementExecutor`），但建议先完成 u2-l2~u2-l4，把入口这条主线吃透。
