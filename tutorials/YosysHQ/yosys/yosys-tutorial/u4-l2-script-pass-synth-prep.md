# ScriptPass 与 synth / prep 合成脚本

## 1. 本讲目标

在 [u1-l4](u1-l4-first-synthesis.md) 里你已经用过 `synth` 这条命令：它把一段 Verilog 一路变成门级网表。在 [u4-l1](u4-l1-pass-registration.md) 里你又知道了 `synth` 和 `opt`、`read_verilog` 一样，背后都是一个 `Pass` 子类对象。但 `synth` 有个特别之处：**它自己并不实现任何综合算法，它只是「按顺序调用了一大堆别的 pass」**。本讲就来揭开这种「编排型 pass」的实现方式。

Yosys 把这类「用一个 pass 编排一长串子 pass」的需求抽象成了一个专门的基类 `ScriptPass`。`synth`（通用综合）和 `prep`（面向形式验证的保守综合）就是它的两个典型实例，也是你日常最常碰到的两条「默认综合脚本」。

读完本讲，你应当能够：

- 说清 `ScriptPass` 提供的 `script()` / `check_label()` / `run()` / `run_script()` / `help_script()` 五件套各干什么，以及它如何**用同一份 `script()` 代码既「真正执行」又「生成帮助文本」**。
- 把 `synth` 的执行过程按 `begin → coarse → fine → check` 四个阶段画成一张子 pass 表，并解释每个阶段的目标。
- 说清 `prep` 与 `synth` 的核心差别：为什么 `prep`「保守」、停在哪里、它为谁服务。
- 会用 `synth -run <from>:<to>` 只跑某一阶段，会用 `help synth` 直接看到完整命令清单。

## 2. 前置知识

本讲会用到前面几讲建立的几个概念，这里只做最简提醒：

- **Pass / pass_register / Pass::call**：每条命令都是一个 `Pass` 子类，调度器靠命令名查 `pass_register` 拿到对象再调 `execute()`；`Pass::call(design, "命令字符串")` 可以在 pass 内部嵌套调用别的命令（见 [u4-l1](u4-l1-pass-registration.md)）。**这条嵌套调用能力正是 `ScriptPass` 能「编排」的前提**——所谓脚本，本质就是一条父 pass 反复 `Pass::call` 一堆子 pass。
- **内部单元库**：综合过程就是把高层 `$and/$mux/$dff/$mem` 一路降到门级 `$_AND_` 之类（见 [u3-l4](u3-l4-internal-cell-library.md)）。本讲不展开单元定义，但你需要知道 `proc / opt / memory / techmap / abc` 这些子 pass 各自负责把设计「往下推一层」。
- **Design / Module / 选择栈**：`synth` 一开始会断言 `design->full_selection()`，即它只接受「整个设计被选中」的情形（见 [u2-l2](u2-l2-design-module.md)）。
- **`-run` 标签语法**：`synth` 与 `prep` 都接受 `-run <from>:<to>`，`from`/`to` 是本讲要讲的「阶段标签」（`begin`/`coarse`/`fine`/`check`），空 `from` 等价于 `begin`，空 `to` 等价于命令表末尾。

还需要一个直觉：真实综合不是「一个算法」，而是「一条流水线」。读入的设计先被翻译成行为级 `RTLIL::Process`（`proc`），再被优化（`opt`）、存储器被处理（`memory`）、高层算术单元被展开（`techmap`）、最后做布尔逻辑优化与映射（`abc`）。`synth` 的工作，就是把这些步骤**按正确顺序、带正确参数**串起来。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [kernel/register.h](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.h) | 声明 `ScriptPass` 基类：成员 `block_active / help_mode / active_design / active_run_from / active_run_to`，以及 `script() / check_label() / run() / run_script() / help_script()` 五个核心方法。 |
| [kernel/register.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc) | 实现 `ScriptPass` 的方法，是理解「双模式」机制的关键：`check_label` 与 `run` 都会根据 `active_design` 是否为空切换行为。 |
| [techlibs/common/synth.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/synth.cc) | `SynthPass`，命令名 `synth`，通用综合脚本。本讲的头号样本。 |
| [techlibs/common/prep.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/prep.cc) | `PrepPass`，命令名 `prep`，保守综合脚本，常用于形式验证前的「整型」。 |
| [docs/source/using_yosys/synthesis/synth.rst](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/using_yosys/synthesis/synth.rst) | 官方文档对「打包好的 `synth_*` 命令」与通用 `prep` 的总览说明。 |

## 4. 核心概念与源码讲解

### 4.1 ScriptPass 框架：用 C++ 写脚本

#### 4.1.1 概念说明

很多综合任务并不是「一个算法」，而是「一串现成 pass 的固定组合」。比如通用综合永远是「先 `proc`，再 `opt`，再 `memory`，再 `techmap`，再 `abc`」这个大模样。如果每条综合流程都让用户手写一长串 `.ys` 脚本，既啰嗦又容易写错顺序。

Yosys 的解决办法是：**把「脚本」本身也做成一条 pass**。这条 pass 的 `execute()` 几乎不写业务逻辑，而是描述「要依次调用哪些子 pass」，框架替你负责真正去调用、去计时、去生成帮助。这个框架就是 `ScriptPass`。

`ScriptPass` 继承自 `Pass`（所以它自己也是一条命令、也会进 `pass_register`，见 [u4-l1](u4-l1-pass-registration.md)），但它额外约定了一个最关键的虚函数：

```cpp
virtual void script() = 0;
```

子类（如 `SynthPass`）只要重写 `script()`，在里面用 `check_label("阶段名")` 划分阶段、用 `run("命令字符串")` 声明要执行的子 pass，就完成了一条综合脚本。**`script()` 是一份「声明式的命令清单」**，至于「真正执行」还是「打印成帮助文本」，由框架根据当前模式决定。

#### 4.1.2 核心流程

理解 `ScriptPass` 的关键是它的**双模式（dual-mode）设计**。同一份 `script()` 代码会被以两种完全不同的状态遍历：

- **执行模式（run）**：用户敲下 `synth` 时。框架把 `active_design` 指向真实设计，`help_mode=false`。于是 `run("opt_expr")` 真的去 `Pass::call(active_design, "opt_expr")` 把优化跑一遍。
- **帮助模式（help）**：用户敲下 `help synth` 时。框架把 `active_design=nullptr`，`help_mode=true`。于是同一个 `run("opt_expr")` 不再执行，而是把字符串 `opt_expr` 打印到帮助文本里——这就是为什么 `help synth` 能逐条列出它要跑的所有命令。

切换发生在两个入口：

```
用户敲 "synth"
     │ SynthPass::execute()
     ▼
run_script(design, run_from, run_to)
     │  help_mode=false; active_design=design; block_active=(run_from为空)
     └─► script()   ← 每个 run() 真的调用子 pass

用户敲 "help synth"
     │ help() → help_script()
     ▼
help_script()
     │  help_mode=true; active_design=nullptr; block_active=true
     └─► script()   ← 每个 run() 只打印命令字符串
```

`script()` 内部用 `check_label("coarse")` 这种调用划分阶段。`check_label` 在两种模式下都返回一个 bool：帮助模式下基本恒为 `true`（把所有命令都打印出来），执行模式下则依据 `-run` 给的起止标签决定当前这一段「要不要激活」。激活与否由成员 `block_active` 记录，`run()` 在执行模式下只有当 `block_active` 为真（且 `check_label` 控制的范围命中）时才真正跑——准确说，`run()` 本身总是被调用，但 `check_label` 通过给 `block_active` 赋值来「关掉」不在范围内的段，于是那些段里的 `run()` 在执行模式下虽然被调到，却因为外层 `if (check_label(...))` 为假而整段跳过。

> 小提醒：`run()` 在帮助模式下打印、在执行模式下调用，这个分支写在 `run()` 自己内部（见 4.1.3）。而「某一段是否参与」由 `check_label()` 的返回值 + `if` 包裹控制。两者职责分开。

#### 4.1.3 源码精读

先看基类声明，它把全部状态浓缩成几个成员：

[register.h:L127-L145](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.h#L127-L145) —— `ScriptPass` 继承 `Pass`，新增 `block_active / help_mode / active_design / active_run_from / active_run_to` 五个成员和五个方法。注意 `script()` 是纯虚的，强制子类实现。

接着是四个方法的实现，集中体现了双模式：

[register.cc:L357-L377](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L357-L377) —— `check_label(label, info)`。当 `active_design == nullptr`（帮助模式）：打印一行 `    label:    info` 并返回 `true`，于是帮助文本里每个阶段都带标题。当 `active_design` 非空（执行模式）：用 `active_run_from`/`active_run_to` 两道闸门控制 `block_active`——遇到 `from` 标签就开闸、遇到 `to` 标签就关闸，从而实现 `-run from:to` 的范围限定；特别地，当 `from == to` 时只激活那一个标签（单段执行）。

[register.cc:L379-L390](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L379-L390) —— `run(command, info)`。这是双模式最直观的体现：帮助模式下打印 `        command    info`（带缩进、带可选说明，比如 `(if -flatten)`）；执行模式下调用 `Pass::call(active_design, command)` 真正跑子 pass，并在跑完后 `active_design->check()` 做一次一致性自检。注意它把 `command` 当作**一整条命令字符串**丢给 `Pass::call`，这正是 [u4-l1](u4-l1-pass-registration.md) 讲的「词法层切词」入口。

[register.cc:L404-L412](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L404-L412) —— `run_script()`。执行模式入口：置 `help_mode=false`、`active_design=design`，`block_active` 初值取决于 `run_from` 是否为空（空则从头开始激活）。然后调用 `script()`。

[register.cc:L414-L423](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L414-L423) —— `help_script()`。帮助模式入口：先 `clear_flags()`（把选项重置为默认，避免上次执行残留）、置 `help_mode=true`、`active_design=nullptr`、`block_active=true`（全段激活，打印完整清单），然后调用同一个 `script()`。

再看子类如何「使用」这套框架。`SynthPass` 的 `execute()` 在解析完命令行选项后，只做一件事：

[synth.cc:L252-L257](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/synth.cc#L252-L257) —— 打印一条 `log_header`，`log_push()` 缩进日志，然后 `run_script(design, run_from, run_to)`，完事 `log_pop()`。所有真正的综合逻辑都不在 `execute()` 里，而在 `script()` 里。

而 `help()` 方法则这样收尾：

[synth.cc:L112-L114](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/synth.cc#L112-L114) —— 先用一堆 `log()` 手写选项说明，最后一句 `help_script();` 自动追加「The following commands are executed by this synthesis command:」后面那一长串命令清单。这份清单不是手维护的，而是框架跑一遍 `script()` 帮你生成的——这就保证了**帮助文本里写的命令和实际执行的命令永远一致**，这是 `ScriptPass` 设计上最漂亮的一点。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「同一份 `script()` 在两种模式下分别『执行』和『打印』」。

**操作步骤**：

1. 先用帮助模式看 `synth` 的完整命令清单：

   ```bash
   ./build/yosys -p "help synth"
   ```

2. 再用执行模式真的跑一遍，找一个简单设计（沿用 [u1-l4](u1-l4-first-synthesis.md) 的 cmos 计数器）：

   ```bash
   ./build/yosys examples/cmos/counter.v -p "synth; stat"
   ```

3. 对比两份输出里的命令序列。

**需要观察的现象**：

- `help synth` 的输出里会有一段形如 `begin:` / `coarse:` / `fine:` / `check:` 的分组标题，每组下面缩进列出若干命令（如 `proc`、`opt_expr`、`abc`）。
- 真正执行 `synth` 时，日志里会出现 `2.X. Executing ... pass.` 这样的行，命令种类与 `help` 列出的**完全对应**。
- 注意帮助文本里有些命令带 `(if -flatten)`、`(unless -nofsm)` 之类注释——这是 `run(cmd, info)` 第二个参数 `info` 在帮助模式下的产物。

**预期结果**：你会确信「帮助里写的命令清单 = 真正跑的命令清单」，二者同源同代码。如果本地尚未构建 yosys，可先按 [u1-l2](u1-l2-build-and-run.md) 完成 `cmake -B build . && cmake --build build`，再执行上述命令；若暂无法构建，本步骤标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ScriptPass` 要把 `script()` 设计成纯虚函数，而不是在基类里给一个默认实现？

**参考答案**：因为「要调用哪些子 pass」是每条综合脚本的核心差异所在，基类无从知晓。把它设为纯虚，强制每个具体脚本（`synth`/`prep`/`synth_xilinx`……）必须自己声明命令清单；同时框架把「怎么执行 / 怎么打印 / 怎么按 `-run` 裁剪」这些与具体命令无关的机制实现在 `run_script`/`help_script`/`check_label`/`run` 里，子类只管「列清单」，职责分离。

**练习 2**：假如用户执行 `synth -run coarse:fine`，`check_label("begin")` 这一段还会真正执行吗？为什么？

**参考答案**：不会真正执行其内部命令。`run_script` 把 `active_run_from="coarse"`、`active_run_to="fine"` 传入，`block_active` 初值为 `false`（因为 `run_from` 非空）。`check_label` 在遇到 `coarse` 标签时才把 `block_active` 置 `true`，遇到 `fine` 时再置回 `false`。所以 `begin` 段处于关闸状态，其内的 `run(...)` 虽被调用，但被 `if (check_label("begin"))` 整段跳过，不会 `Pass::call`。

---

### 4.2 synth：通用综合的完整阶段

#### 4.2.1 概念说明

`synth` 是 Yosys 的「默认通用综合脚本」。它的目标很明确：**把一份 RTL 设计一路推到「可被映射」的门级/标准单元形式**。它不绑定任何具体工艺（那是 `synth_xilinx`、`synth_ice40` 等厂商脚本的活，见 [u8-l2](u8-l2-vendor-synth-flows.md)），但它在结尾会调用 `techmap` 把高层 `$` 单元降到门级、并可选地调用 `abc` 做布尔逻辑优化与 LUT/标准单元映射。

`synth` 把整条流水线划分成四个标签段，顺序为 `begin → coarse → fine → check`。直觉上：

- **begin**：整理层次，确定顶层（`hierarchy -check`）。
- **coarse**：粗粒度综合。把行为级 `process` 翻成逻辑（`proc`），做几轮优化（`opt`），处理有限状态机（`fsm`）、算术单元（`alumacc`）、资源共享（`share`），收集但**暂不展开**存储器（`memory -nomap`）。这一阶段保留较大的可优化空间。
- **fine**：细粒度综合。把存储器真正展开成触发器与多路器（`memory_map`），用 `techmap` 把高层单元拍成门级，最后用 `abc` 做逻辑优化与映射。这一阶段产物接近最终网表。
- **check**：收尾自检（`hierarchy -check`、`stat`、`check`）。

#### 4.2.2 核心流程

下面这张表是 `synth` 的「骨架」，对照 [synth.cc 的 `script()`](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/synth.cc#L260-L367) 逐行整理。表中「条件」列说明该命令何时才会出现（帮助模式下列出、执行模式下按选项触发）。

| 阶段 | 子 pass | 条件 / 说明 |
|------|---------|-------------|
| **begin** | `hierarchy -check [-top X \| -auto-top]` | `-flatten`/`-auto-top` 或无 top 时用 `-auto-top` |
| **coarse** | `proc [+latches]` | 把 always/process 翻成逻辑 |
| | `check` / `flatten` | 仅 `-flatten` 时 |
| | `opt_expr` | 常量折叠 |
| | `opt_clean` | 删悬空线 |
| | `check` | |
| | `opt -nodffe -nosdff` | 不在此推断 dffe/sdff |
| | `fsm [+opts]` | 除非 `-nofsm` |
| | `opt [-hier]` | `-hier` 仅 `-hieropt` |
| | `wreduce` | 缩窄过宽的运算 |
| | `peepopt` | 窥孔优化 |
| | `opt_clean` | |
| | `techmap -map cmp2lut.v -map cmp2lcu.v` | 仅 `-lut` |
| | `booth` | 仅 `-booth` |
| | `alumacc` | 除非 `-noalumacc` |
| | `arith_tree` | 仅 `-arith_tree` |
| | `share` | 除非 `-noshare` |
| | `opt [-hier]` | |
| | `memory -nomap [+opts]` | 收集存储器，但**不展开** |
| | `opt_clean` | |
| **fine** | `opt -fast -full [-hier]` | |
| | `memory_map` | 把存储器展开为 FF + mux |
| | `opt -full` | |
| | `techmap [+opts]` | 高层 `$` 单元 → 门级；`-extra-map` 追加规则 |
| | `techmap -map gate2lut.v` | 仅 `-noabc` 且 `-lut` |
| | `clean; opt_lut` | 仅 `-noabc` 且 `-lut` |
| | `flowmap -maxlut K` | 仅 `-flowmap` |
| | `opt -fast [-hier]` | |
| | `abc` / `abc -lut k` | 除非 `-noabc`、除非 `-flowmap`；需编译进 ABC |
| | `opt -fast` | 除非 `-noabc` |
| **check** | `hierarchy -check` | |
| | `stat` | 打印单元统计 |
| | `check` | 一致性自检 |

有几个设计要点值得点出：

1. **`memory -nomap`（coarse）与 `memory_map`（fine）分两步**。coarse 阶段先 `memory -nomap` 把分散的读写端口「收集」成规整的 `$mem` 单元但保留之，等 fine 阶段再 `memory_map` 展开成触发器与多路器。这样 coarse 阶段优化（`share`、`opt`）还能在「存储器抽象」层面进行，而不是在一堆零散触发器上进行。
2. **`opt` 反复出现**。几乎每个「会改变网表形状」的步骤之后都跟一次 `opt`/`opt_clean`，因为每一步都可能引入新的可化简常量或死代码。这是综合脚本里非常典型的「化简—变换—再化简」循环。
3. **`abc` 只在 fine 末尾、且需编译支持**。`abc` 调用外部 ABC 工具做布尔网络优化与映射，因此整个调用被包在 `#ifdef YOSYS_ENABLE_ABC` 里（见 [synth.cc:L347-L358](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/synth.cc#L347-L358)）；若 yosys 构建时关闭了 ABC（`YOSYS_WITHOUT_ABC`，见 [u1-l2](u1-l2-build-and-run.md)），这段代码根本不存在。

#### 4.2.3 源码精读

看 `script()` 的实际写法，体会「带条件命令」如何用 `help_mode` 双写：

[synth.cc:L286-L317](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/synth.cc#L286-L317) —— coarse 段。注意 `fsm` 这一处：

```cpp
if (!nofsm || help_mode)
    run("fsm" + fsm_opts, "      (unless -nofsm)");
```

这是 `ScriptPass` 的经典写法：条件写成 `(!真实条件 || help_mode)`，意思是「实际执行时受选项控制，但帮助模式下永远显示」。第二个参数 `"      (unless -nofsm)"` 就是帮助文本里那行说明。于是 `help synth` 能把所有「可能执行」的命令都列全，哪怕当前没带那个选项。这种 `|| help_mode` 的「双写」贯穿整个 `script()`，是阅读此类代码的钥匙。

再看 `hierarchy` 那一段对 `help_mode` 的处理：

[synth.cc:L272-L284](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/synth.cc#L272-L284) —— begin 段。帮助模式下打印一个「占位」形式 `hierarchy -check [-top <top> | -auto-top]`（用方括号列出所有可能）；执行模式下才根据 `top_module`/`flatten`/`autotop` 真正拼出具体命令。这说明 `script()` 既是「清单」也是「文档」，占位串 `[...]` 专供人读。

最后看 `execute()` 里两个重要的前置校验：

[synth.cc:L244-L250](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/synth.cc#L244-L250) —— `synth` 拒绝在「部分选择」的设计上运行（`full_selection()` 必须为真），并对 `-abc9`/`-flowmap` 强制要求同时给 `-lut`（ABC9/FlowMap 只用于 FPGA 的 LUT 架构）。这解释了帮助文本里那句「This command does not operate on partly selected designs.」

#### 4.2.4 代码实践

**实践目标**：用 `synth -run` 把流水线「分段」运行，亲眼看到 coarse 与 fine 各自把设计推进到什么程度。

**操作步骤**：

1. 准备一个含行为级 `always` 和存储器的小设计 `tiny.v`（示例代码）：

   ```verilog
   module tiny(input clk, input [7:0] d, output reg [7:0] q);
       reg [7:0] mem [0:3];
       always @(posedge clk) begin
           mem[0] <= d;
           q <= mem[1];
       end
   endmodule
   ```

2. 只跑 `begin:coarse`，然后导出 RTLIL 观察：

   ```bash
   ./build/yosys tiny.v -p "synth -run begin:coarse; write_rtlil" > after_coarse.txt
   ```

3. 再完整跑一遍对比：

   ```bash
   ./build/yosys tiny.v -p "synth; write_rtlil" > after_full.txt
   ```

4. 在两份输出里分别查找 `$mem`、`$dff`、`$mux`、`$_AND_`（或 `$_DFF_`）出现的种类与数量。

**需要观察的现象**：

- `after_coarse.txt` 里应当还能看到 `$mem` 单元（因为 coarse 只 `memory -nomap`，没展开）；`process` 已被 `proc` 消失。
- `after_full.txt` 里 `$mem` 应被 `memory_map` 展开成 `$dff` + `$mux` 组合，高层算术/逻辑单元也被 `techmap` 进一步降低；若开了 `abc`，末尾还可能出现更底层的门级单元。

**预期结果**：你会直观看到「coarse 保留存储器抽象、fine 把它拍平成寄存器与多路器」这一核心差异。若本地未构建 yosys，本步骤标注「待本地验证」；也可退而用源码阅读型方式：对照 [synth.cc:L315](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/synth.cc#L315) 的 `memory -nomap` 与 [synth.cc:L321](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/synth.cc#L321) 的 `memory_map`，说明二者为何分置两段。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `fsm` 放在 coarse 段、`abc` 放在 fine 段，而不是反过来？

**参考答案**：`fsm` 识别的是「一段用状态寄存器+次态逻辑描述的有限状态机」，需要在设计还处于较高抽象（触发器逻辑清晰、未被反复展平）时才能有效识别与重编码；放到 fine 末尾网表已被 `techmap`/`abc` 拍得很碎，状态机的结构特征早已消失。`abc` 则需要输入是「布尔组合逻辑」，只有 fine 段把存储器展开、高层算术单元 `techmap` 成门之后，剩下的才是 `abc` 能处理的纯组合逻辑块。所以顺序是「先抽象层优化（fsm），后门级优化（abc）」。

**练习 2**：帮助文本里 `abc` 那一行写着 `(unless -noabc, unless -lut)` 和 `(unless -noabc, if -lut)` 两条。这两条对应代码里哪两处 `run()`？

**参考答案**：对应 [synth.cc:L349-L350](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/synth.cc#L349-L350) 帮助模式下的两次 `run(abc, ...)`：一次是 `run(abc, "       (unless -noabc, unless -lut)")`（不带 `-lut` 的标准单元映射），一次是 `run(abc + " -lut k", "(unless -noabc, if -lut)")`（LUT 映射）。执行模式下则由 [synth.cc:L352-L356](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/synth.cc#L352-L356) 的 `if (lut) ... else ...` 二选一真正执行。

---

### 4.3 prep：面向形式验证的保守综合

#### 4.3.1 概念说明

`prep` 的名字来自 **prep**are（准备）。官方文档 [synth.rst:L17-L20](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/using_yosys/synthesis/synth.rst#L17-L20) 一句话点明了它的定位：「`prep` is limited to coarse-grain synthesis, without getting into any architecture-specific mappings or optimizations. Among other things, this is useful for design verification.」

换句话说，`prep` 是一条**保守的、只到粗粒度**的综合脚本。它和 `synth` 的根本区别在于：

- **不展开存储器**：`prep` 默认保留 `$mem`（除非加 `-nomem` 之外的进一步处理），而 `synth` 会 `memory_map` 把它拆成触发器。
- **不做工艺映射**：`prep` **完全没有** `techmap`（高层→门级）、**没有** `abc`（逻辑优化/映射）。设计停留在 `$and/$dff/$mem` 这一层抽象。
- **保留 `don't-care`（`-keepdc`）**：`prep` 默认带着 `-keepdc` 跑优化，刻意不消除那些含 `x`/`z` 的逻辑，以忠实反映 Verilog 仿真语义——这对形式验证至关重要，因为形式验证要在「和仿真一致」的语义上做证明。

为什么形式验证（如 SMT2 有界模型检查，见 [u7-l3](u7-l3-formal-backends.md)）需要这样一条脚本？因为形式验证工具要把设计编码成可判定的数学公式，它**希望设计尽量贴近原始 RTL 语义、不要被激进优化改写**。`synth` 的激进优化（常量折叠、资源共享、abc 重写）可能改变设计的边界行为（至少在 `x` 语义上），破坏「验证的对象 == 用户写的 RTL」这一前提。`prep` 正是为这个场景量身定做的「轻度整形」：只做必须的（`proc` 把行为级翻成逻辑、`memory_collect` 规整存储器端口），其余原样保留。

#### 4.3.2 核心流程

`prep` 的阶段更短，只有 `begin → coarse → check` 三段（没有 `fine`）。对照 [prep.cc 的 `script()`](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/prep.cc#L166-L222)：

| 阶段 | 子 pass | 条件 / 说明 |
|------|---------|-------------|
| **begin** | `hierarchy -check [-top X \| -auto-top]` | 同 synth |
| **coarse** | `proc [-ifx]` | `-ifx` 用仿真式 undef 处理 |
| | `flatten` | 仅 `-flatten` |
| | `future` | 解析 future 采样值函数 |
| | `opt_expr [-keepdc]` | 默认 `-keepdc` |
| | `opt_clean` | |
| | `check` | |
| | `opt -noff [-keepdc]` | 默认带 `-keepdc`，不推断锁存器/FF |
| | `wreduce [-keepdc] [-memx]` | 除非 `-ifx` |
| | `memory_dff` | 仅 `-rdff` |
| | `memory_memx` | 仅 `-memx` |
| | `opt_clean` | |
| | `memory_collect` | 规整存储器端口（**不展开**） |
| | `opt -noff [-keepdc] -fast` | |
| | `sort` | 规范化单元顺序 |
| **check** | `stat` | |
| | `check` | |

和 `synth` 对照，几个关键差异一眼可见：

1. **`memory_collect` 替代了 `memory -nomap` + `memory_map`**。`prep` 只做「收集」不做「展开」——存储器以 `$mem` 形式保留，因为形式验证后端（如 SMT2）能直接理解字级存储器，没必要拆成触发器。
2. **没有任何 `techmap` / `abc` / `flowmap`**。设计始终停留在内部 `$` 单元层。
3. **`-keepdc` 默认开启**（`nokeepdc` 为 false 时）。注意 [prep.cc:L193](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/prep.cc#L193) 用了 `nokeepdc ? "opt_expr" : "opt_expr -keepdc"` 这种「三元选命令」的写法，而不是 synth 里的 `|| help_mode` 双写——因为这里的差别只是「加不加一个开关」，命令名稳定，所以直接二选一即可。
4. **多了 `future` 和 `sort`**。`future`（[passes/cmds/future.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/cmds/future.cc)）处理 SystemVerilog 的 future 采样值函数，是形式验证常用的；`sort` 把单元按规范顺序排列，便于后端生成确定性的输出。

#### 4.3.3 源码精读

看 prep 的类定义，确认它也是 `ScriptPass`：

[prep.cc:L28-L30](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/prep.cc#L28-L30) —— `struct PrepPass : public ScriptPass`，构造函数 `ScriptPass("prep", "generic synthesis script")` 把命令名注册为 `prep`。注意它的 `short_help` 字符串和 `synth` 一样都是 `"generic synthesis script"`（看起来像复制粘贴遗留），但命令名不同，二者互不冲突。

看 coarse 段对 `-keepdc` 的处理：

[prep.cc:L193-L196](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/prep.cc#L193-L196) —— `opt_expr` 与 `opt -noff` 都根据 `nokeepdc` 决定是否追加 `-keepdc`。这就是 `prep`「保守」语义在代码里的落点：除非用户显式 `-nokeepdc`，否则优化都保留 don't-care 信息。

看存储器段的「按需开关」：

[prep.cc:L205-L212](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/prep.cc#L205-L212) —— 整个存储器处理被 `if (!nomemmode)` 包住：`-nomem` 时跳过全部 `memory_*`。其中 `memory_dff`（仅 `-rdff`）和 `memory_memx`（仅 `-memx`）是可选的，而 `memory_collect` 是必跑的。注意这里**只有 `memory_collect`，没有 `memory_map`**——这是 prep 与 synth 最本质的差异之一。

#### 4.3.4 代码实践

**实践目标**：对同一个设计分别跑 `synth` 和 `prep`，对比输出，验证「prep 保留存储器与高层单元、synth 把它们降到门级」。

**操作步骤**：

1. 用 4.2.4 里的 `tiny.v`（含存储器的设计）跑两条流水线，分别统计：

   ```bash
   ./build/yosys tiny.v -p "prep; stat" 2>&1 | tee prep_stat.txt
   ./build/yosys tiny.v -p "synth; stat" 2>&1 | tee synth_stat.txt
   ```

2. 对比两份 `stat` 输出里的「Number of cells」及其分类。

3. 再分别 `write_rtlil` 看具体单元：

   ```bash
   ./build/yosys tiny.v -p "prep; write_rtlil"  > prep.il
   ./build/yosys tiny.v -p "synth; write_rtlil" > synth.il
   ```

**需要观察的现象**：

- `prep_stat.txt` 里大概率还能看到 `$mem` 单元，且不会有 `$_AND_`/`$_DFF_` 这类「带极性的门级单元」；单元种类少。
- `synth_stat.txt` 里 `$mem` 消失（被展开），代之以更多 `$dff`/`$mux`，若开了 abc 还会出现更底层的单元；单元总数通常更多。

**预期结果**：直观印证「prep = 字级/粗粒度、保留存储器；synth = 门级、展开并映射」。若本地未构建，标注「待本地验证」；源码阅读型替代：对照 [prep.cc:L211](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/prep.cc#L211)（`memory_collect`，无 map）与 [synth.cc:L315,L321](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/synth.cc#L315-L321)（`memory -nomap` + `memory_map`），说明存储器处理的不同。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `prep` 默认带 `-keepdc`，而 `synth` 不带？

**参考答案**：`prep` 面向形式验证，必须让综合结果在 `x`/`z`（don't-care）语义上与原始 RTL 仿真一致，否则验证工具证明的是「被改写过的设计」而非用户写的设计。`-keepdc` 让 `opt_expr` 等优化不去消除含 don't-care 的逻辑，保真。`synth` 面向最终实现，目标是面积/速度，激进消除 don't-care 反而有利，所以默认不带。

**练习 2**：`prep` 没有 `fine` 段，也没有 `techmap`/`abc`。这是「遗漏」还是「有意为之」？请结合它的用途说明。

**参考答案**：有意为之。`prep` 的定位是「verification 前的保守整形」，它要在尽量贴近 RTL 语义的抽象层次上停住。`techmap` 会把高层 `$` 单元拍成门、`abc` 会重写布尔逻辑，二者都会改变设计的表示甚至边界语义，破坏形式验证「验证对象 == RTL」的前提。所以 `prep` 刻意止步于 `memory_collect` 与 `opt -keepdc`，把设计整理成形式验证后端（SMT2/BTOR 等，见 [u7-l3](u7-l3-formal-backends.md)）能直接消化的字级形式。

---

## 5. 综合实践

把本讲三块知识串起来：用「分段执行 + 帮助对照」的方式，亲手「阅读」一遍 `synth` 脚本。

**任务**：对 `examples/cmos/counter.v`，完成下面三件事，并把结果整理成一张表。

1. **生成权威清单**：运行 `./build/yosys -p "help synth"`，把输出里 `begin/coarse/fine/check` 四段下的命令逐条抄进表格（这就是 `help_script()` 跑 `script()` 的产物）。
2. **分段执行验证**：依次运行下面四条命令，每次都用 `stat` 记录单元总数与种类，观察「每一段往下推进了什么」：

   ```bash
   ./build/yosys examples/cmos/counter.v -p "synth -run begin:begin;  stat"
   ./build/yosys examples/cmos/counter.v -p "synth -run begin:coarse; stat"
   ./build/yosys examples/cmos/counter.v -p "synth -run begin:fine;   stat"
   ./build/yosys examples/cmos/counter.v -p "synth;                  stat"
   ```

   > 注意：`-run a:a` 表示只跑标签 `a` 那一段（`from==to`，见 [register.cc:L367-L368](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L367-L368)）；`-run begin:coarse` 表示从开头跑到 `coarse` 段结束。

3. **横向对比 prep**：对同一设计再跑 `./build/yosys examples/cmos/counter.v -p "prep; stat"`，在表格里新增一列「prep 是否执行该命令」（参考 [prep.cc:L166-L222](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/prep.cc#L166-L222)）。

**预期得到的表（骨架）**：

| 阶段 | synth 子 pass | 该段后单元总数（待填） | prep 是否执行 |
|------|---------------|------------------------|---------------|
| begin | hierarchy -check | | 是 |
| coarse | proc / opt_expr / fsm / opt / alumacc / share / memory -nomap … | | 部分（无 fsm/alumacc/share） |
| fine | memory_map / techmap / abc … | | **否（prep 无 fine 段）** |
| check | stat / check | | 是 |

**思考题**（写进你的笔记）：从第 2 步的数字变化里，你能看出哪一段「砍掉的单元最多」吗？它对应 `script()` 里哪几条命令？如果本地暂未构建 yosys，第 2、3 步标注「待本地验证」，但第 1 步的清单和表格骨架可以纯靠阅读 `synth.cc` 与 `prep.cc` 完成。

## 6. 本讲小结

- `ScriptPass` 是「编排型 pass」的基类：它本身不实现算法，而是用一份声明式的 `script()` 列出「要依次调用哪些子 pass」，由框架的 `run()` 真正 `Pass::call` 它们。
- 它的核心是**双模式设计**：同一份 `script()`，在 `run_script()`（`active_design` 非空）下真正执行，在 `help_script()`（`active_design` 为空、`help_mode=true`）下打印成帮助文本——因此 `help synth` 列出的命令清单与实际执行永远一致。
- `check_label("阶段名")` 划分阶段并在执行模式下配合 `-run from:to` 控制范围（`block_active` 的开闸/关闸）；`run(cmd, info)` 在帮助模式打印命令+说明、在执行模式调用命令并自检。
- `synth` 把通用综合分成 `begin → coarse → fine → check` 四段：coarse 做抽象层优化（proc/opt/fsm/alumacc/share/memory -nomap），fine 做门级化与映射（memory_map/techmap/abc），最终降到接近物理门级。
- `prep` 是「保守、只到粗粒度」的三段脚本（`begin → coarse → check`）：保留 `$mem`（只 `memory_collect` 不 `memory_map`）、没有 `techmap`/`abc`、默认带 `-keepdc`，专为形式验证前的「保真整形」服务。
- 带条件命令的经典写法是 `if (!cond || help_mode) run(cmd, "(说明)")`：执行时受选项控制，帮助时永远显示——这是阅读所有 `synth_*` 脚本的钥匙。

## 7. 下一步学习建议

- **动手扩脚本**：厂商综合脚本 `synth_xilinx` / `synth_ice40` 等都是 `ScriptPass` 子类，在通用 `synth` 基础上加入平台相关的 LUT/BRAM/DSP 映射阶段。学完本讲后，建议进入 [u8-l2](u8-l2-vendor-synth-flows.md)，对照阅读 `techlibs/xilinx/synth_xilinx.cc`，你会发现它的 `script()` 与本讲的 `synth` 几乎同构，只是多了几个标签段。
- **深入子 pass**：本讲把 `proc / opt / memory / techmap / abc` 当作「黑盒子 pass」对待。它们各自的内部机制在第 6 单元逐讲展开：`proc`（[u6-l2](u6-l2-proc.md)）、`opt`（[u6-l3](u6-l3-opt.md)）、`memory`（[u6-l4](u6-l4-memory.md)）、`techmap`（[u6-l5](u6-l5-techmap-simplemap.md)）、`abc9`（[u6-l6](u6-l6-abc9-liberty.md)）。建议按此顺序读，因为 `synth` 的阶段顺序正是按它们的依赖关系排的。
- **自己写一个**：如果你已迫不及待想写 pass，可以先跳到 [u9-l1](u9-l1-write-custom-pass.md) 看普通 `Pass` 的写法；想写「自己的综合脚本」时，再回来照着 `prep.cc` 这个不到 200 行的模板派生一个 `ScriptPass`，这是进入 Yosys 二次开发最直接的路径之一。
