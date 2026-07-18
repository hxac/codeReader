# cxxrtl：C++ 仿真后端

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 cxxrtl 后端与 u7-l1 三个「文本后端」的本质区别——它输出的不是给人看的网表，而是一段**可编译、可运行**的 C++ 程序。
- 掌握 `write_cxxrtl` 命令的调用方式、关键选项（`-header`、`-namespace`、`-O`、`-g`、`-nohierarchy`/`-noflatten`/`-noproc`）以及它产出的文件结构。
- 理解 cxxrtl 把一个 RTLIL 模块翻译成一个 C++ 类、把组合/时序逻辑翻译成 `eval()` 方法的大流程，并知道 `Scheduler`、`FlowGraph`、`WireType` 各自的作用。
- 看懂 cxxrtl runtime 提供的 `value<>`/`wire<>`/`module` 三大原语，以及 `eval()`/`commit()`/`step()` 构成的仿真契约。
- 动手把 `counter` 综合成 C++，写一个 `main` 翻转时钟、调用 `step()` 并打印计数值，完成一次端到端仿真。

## 2. 前置知识

本讲默认你已经掌握 u7-l1（三个文本后端与 `Backend` 基类）。在继续之前，请确认以下概念：

- **Backend 基类**：u7-l1 讲过，`Backend("x")` 会自动拼出命令名 `write_x`，并用模板方法把「开文件、`extra_args` 处理选择/文件名」收进基类，子类只实现面向输出流 `std::ostream *&f` 的 `execute()`。cxxrtl 后端正是这种结构。
- **RTLIL 门级单元**：`$and`/`$mux`/`$dff`/`$adff` 等内部 `$` 单元，以及它们的端口约定（二元运算 `A/B→Y`、多路器 `A/B/S→Y`、触发器 `CLK/D→Q`）。详见 u3-l4。
- **RTLIL::Process**：行为级 `always` 块在前端的中间形态，由 `root_case`（决策树）与 `syncs`（敏感事件）组成；`proc` 把它翻译成门级。详见 u6-l2。
- **边沿敏感事件**：`STp`（posedge）、`STn`（negedge）、`STe`（both）、`STa`（combinational/`always @*`）。这些在 cxxrtl 里会被翻译成 `posedge_*`/`negedge_*` 检测。
- **仿真（simulation）与综合（synthesis）的区别**：Yosys 的本职是综合，而 cxxrtl 是「综合出一个仿真器」。它先在 RTLIL 上做轻量整理（hierarchy/flatten/proc），再把整理后的网表**翻译**成 C++，最终由你用普通 C++ 编译器编出仿真程序。

一句话定位：**cxxrtl 是一个把 RTLIL 设计「编译」成 C++ 源码的代码生成后端，C++ 编译器再把这些源码编成一个可执行仿真模型。** 它不是解释器，而是代码生成器。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `backends/cxxrtl/cxxrtl_backend.cc` | 后端主体，近 3900 行。包含 `CxxrtlBackend`（命令注册/帮助/选项解析）、`CxxrtlWorker`（代码生成的全部逻辑）、`Scheduler`（反馈弧排序）、`FlowGraph`（数据流图与节点分类）。 |
| `backends/cxxrtl/CMakeLists.txt` | 声明该后端编入 yosys，并标注它依赖 `hierarchy`/`flatten`/`proc` 三个 pass，同时把 `runtime/` 下的运行时头文件作为数据文件安装到 include 目录。 |
| `backends/cxxrtl/runtime/README.txt` | 说明 runtime 目录的用途：放在仿真程序的 include 路径上，**不**进入 yosys 二进制。 |
| `backends/cxxrtl/runtime/cxxrtl/cxxrtl.h` | runtime 核心头文件。定义 `value<Bits>`、`wire<Bits>`、`memory<Width>`、`module` 基类及其 `eval()/commit()/step()` 仿真契约，以及 `cxxrtl_yosys` 命名空间下的内部单元运算函数。 |
| `backends/cxxrtl/runtime/cxxrtl/capi/cxxrtl_capi.h` | 稳定的 C API，供 C/Python 等通过 C ABI 驱动仿真（`cxxrtl_create`/`cxxrtl_step` 等）。 |
| `examples/cmos/counter.v` | 本讲代码实践使用的小设计：3 位计数器，端口 `clk/rst/en/count[2:0]`。 |

## 4. 核心概念与源码讲解

### 4.1 cxxrtl 的定位与端到端流程

#### 4.1.1 概念说明

u7-l1 的三个后端都是「序列化器」：把内存里的 RTLIL 原样写成文本（Verilog/RTLIL/JSON），读回来还是同一个设计。它们是给人或下游工具**看**的。

cxxrtl 走的是完全不同的路线——它是**代码生成器（code generator）**。它的输出 `.cc` 文件本身不是设计，而是一段 C++ 程序；这段程序被 C++ 编译器编成可执行文件后，才是一个「软件实现的硬件仿真模型」。

这种「把硬件翻译成等价 C++」的思路有几个直接好处：

1. **性能**：cxxrtl 大量使用 C++ 模板做任意位宽运算，把「展开循环、内联函数」的活交给 C++ 编译器的指令选择器。runtime 注释里直言「CXXRTL essentially uses the C++ compiler as a hygienic macro engine that feeds an instruction selector」（CXXRTL 本质上把 C++ 编译器当成一个喂给指令选择器的卫生宏引擎）。
2. **可调试性**：生成的代码可读、可下断点，且可配合 VCD/C API 做波形与内省。
3. **可嵌入**：仿真模型是个普通 C++ 对象，能嵌进更大的软件（测试平台、协同仿真、形式验证驱动）。

正因为输出是「要被编译运行的程序」，cxxrtl 不能像文本后端那样只做翻译，它必须额外解决一个文本后端从不关心的问题：**求值顺序（evaluation order）**——硬件里信号是并发流动的，而 C++ 是顺序执行的，必须把网表排成一个让组合逻辑在一次扫描内收敛的顺序。

#### 4.1.2 核心流程

从敲下 `write_cxxrtl` 到拿到可运行仿真程序，端到端分两大阶段：

**阶段 A：Yosys 内（后端做的事）**

```
write_cxxrtl [options] file.cc
        │
        ▼
CxxrtlBackend::execute()      解析选项，构造 CxxrtlWorker
        │
        ▼
worker.prepare_design(design)   ① 轻量整理：hierarchy / flatten / proc
        │                       ② analyze_design：建 SigMap、FlowGraph、调度、WireType
        ▼
worker.dump_design(design)      ③ 拓扑排序模块 → 生成 namespace + 每模块一个类
                                ④ 每个类里生成 reset()/eval() 方法
        │
        ▼
写出 file.cc（+可选 file.h）
```

**阶段 B：用户侧（你做的事）**

```
写一个 main.cc：实例化 top 类，循环翻转时钟、调 step()、读输出
        │
        ▼
c++ -std=c++20 -I<runtime> main.cc top.cc -o sim
        │
        ▼
./sim   ← 这才是真正的「仿真器」
```

关键直觉：阶段 A 输出的是「设计专用的 C++ 源码」，它必须 `#include` runtime 头文件（阶段 B 的 `-I<runtime>`）才能编译。换句话说，**cxxrtl = 生成代码 + 公共 runtime**，缺一不可。

#### 4.1.3 源码精读

后端的注册与文本后端完全同构，只是这次 `execute` 不写文本而生成 C++。命令注册与帮助文本入口在 `CxxrtlBackend`：

[cxxrtl_backend.cc:3470-3474](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/cxxrtl_backend.cc#L3470-L3474) — `CxxrtlBackend` 继承 `Backend`，构造函数 `Backend("cxxrtl", ...)` 自动拼出命令名 `write_cxxrtl`。

`execute()` 先解析选项、设好 worker 的优化/调试开关，然后调用 `extra_args` 处理文件名与选择，最后把活全交给 worker 的两个方法：

[cxxrtl_backend.cc:3867-3868](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/cxxrtl_backend.cc#L3867-L3868) — `prepare_design` 整理与分析设计，`dump_design` 真正写 C++ 代码。

`prepare_design` 揭示了 cxxrtl 与纯文本后端的关键差异：它会**主动调用其它 pass** 把设计整理成适合代码生成的形态：

[cxxrtl_backend.cc:3434-3466](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/cxxrtl_backend.cc#L3434-L3466) — 依次按需调用 `hierarchy -auto-top`、`flatten`、`proc`，然后跑 `analyze_design`。注意默认 `run_hierarchy/run_flatten/run_proc` 都为真，对应 `-nohierarchy/-noflatten/-noproc` 三个开关。

为什么必须展平 + proc？因为代码生成要求「每个设计状态是一段顺序 C++ 代码」，而 RTLIL 的模块层次与 Process 是「并发/层次化」的抽象，必须先拍平。这也解释了 CMakeLists 里的依赖声明：

[CMakeLists.txt:15-18](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/CMakeLists.txt#L15-L18) — `REQUIRES hierarchy flatten proc`，声明本后端依赖这三个 pass，构建系统据此保证它们先被编入。

#### 4.1.4 代码实践

**实践目标**：亲手跑一遍「Yosys 内」那半截流程，看清 cxxrtl 到底吐出了什么。

**操作步骤**：

1. 在 yosys shell 里读入计数器并生成 C++：

   ```
   yosys -p "read_verilog examples/cmos/counter.v; write_cxxrtl counter.cc"
   ```

2. 用编辑器打开生成的 `counter.cc`，定位到 `namespace cxxrtl_design`，找到顶层类（名为 `p_counter`，`p_` 是公有名的 mangle 前缀）。

3. 在该类里找到 `bool eval(performer *performer)` 方法，观察它如何把 `posedge clk`、`if (rst)`、`count + 1` 翻译成 `if (posedge_…)`、`==` 比较与 `add_uu` 调用。

**需要观察的现象**：`counter.cc` 是合法的、可读的 C++；顶部 `#include <cxxrtl/cxxrtl.h>`；模块被翻译成一个派生自 `cxxrtl::module` 的类，端口变成成员变量（`p_clk` 等），`always` 逻辑变成 `eval()` 里的一段顺序代码。

**预期结果**：文件头是 include 与命名空间，主体是 `p_counter` 类。**完整编译运行留到 4.4 综合实践**。若本地尚未构建 yosys，这一步的精确输出标注为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 cxxrtl 后端必须在生成代码前先跑 `flatten` 和 `proc`，而 `write_verilog` 不需要？

**参考答案**：`write_verilog` 只是序列化当前 RTLIL，模块层次与 Process 都能原样表达成 Verilog 的 `module` 与 `always`；而 cxxrtl 要生成一段「顺序执行的 C++」，必须把并发语义（Process 的敏感事件）和层次化例化都拍平成单一平面网表，才能求出一个确定性的求值顺序。

**练习 2**：`prepare_design` 里 `run_proc` 为假时，为什么还要在 `has_sync_init` 时单独调用 `proc_init`？

**参考答案**：见 [cxxrtl_backend.cc:3451-3458](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/cxxrtl_backend.cc#L3451-L3458) 的注释——即便用户用 `-noproc` 跳过完整 proc，初值（`initial` 块里的同步初始化）仍必须被处理，否则 `check_design` 后面的 `log_assert(!has_sync_init)` 会失败，所以补跑 `proc_prune`/`proc_clean`/`proc_init`。这也让 `yosys foo.v -o foo.cc` 这种一行命令能直接工作。

### 4.2 write_cxxrtl 用法：命令、选项与输出文件结构

#### 4.2.1 概念说明

`write_cxxrtl` 是一条普通的 `Backend` 命令（u7-l1 讲过 `Backend` 基类），用法骨架是：

```
write_cxxrtl [options] [filename]
```

它的选项分为三组：**优化等级 `-O0..-O6`**、**调试等级 `-g0..-g4`**、以及一组**流程/输出开关**。优化等级控制「多少连线被消除」（让生成的代码更小更快但更难调试），调试等级控制「多少被优化掉的信号还能通过 debug/C API 看到」。两者正交：你可以同时 `-O6 -g4`，既最大化性能又保留完整可见性。

理解这两组等级的关键是 cxxrtl 的**连线分类（wire classification）**思想：每根 wire 会被归入不同种类（见 4.3），优化等级决定「能不能把内部/公有 wire 进一步去缓冲化、本地化、内联」，调试等级决定「要不要为被优化掉的 wire 额外生成按需计算的 debug 代码」。

#### 4.2.2 核心流程

`execute()` 把命令行选项翻译成 `CxxrtlWorker` 上的一组布尔开关，核心是两个 fall-through 的 `switch`：

- **`-O` 等级**：用 `YS_FALLTHROUGH` 串联，高等级**包含**低等级的全部优化。`-O1` 去缓冲化内部 wire，`-O2` 本地化内部 wire，`-O3` 内联内部 wire，`-O4/-O5/-O6` 把同样的三步施加到未被 `(*keep*)` 标记的**公有** wire。默认 `-O6`（最高）。
- **`-g` 等级**：`-g0` 关闭 debug（同时禁用 C API），`-g1` 开 C API、保留访问所有设计状态所需的最小 debug，`-g2` 加上可直接经 C++ 接口访问的公有 wire 的 debug，`-g3` 再加上接常数/别名 wire，`-g4` 为所有被优化掉的公有 wire 按需计算 debug。默认 `-g4`。

流程/输出开关：
- `-header`：把接口（`.h`）与实现（`.cc`）拆成两个文件（见 4.3.3）。
- `-namespace <ns>`：把生成代码放进指定命名空间，默认 `cxxrtl_design`。
- `-print-output std::cout|std::cerr`：`$print` 单元的输出流。
- `-nohierarchy / -noflatten / -noproc`：跳过 prepare_design 里的对应整理步骤。

#### 4.2.3 源码精读

帮助文本里给出的「单时钟驱动器」范例，就是理解用法最快的一扇窗：

[cxxrtl_backend.cc:3487-3499](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/cxxrtl_backend.cc#L3487-L3499) — `top.step()`、`top.p_clk.set(false)` / `set(true)` 的经典时钟翻转模板。注意帮助里特意警告：CXXRTL 仿真和真实硬件一样存在竞争，用户逻辑必须与上升沿错开（先拉低 step、再拉高 step）。

选项解析与两个等级 switch：

[cxxrtl_backend.cc:3807-3850](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/cxxrtl_backend.cc#L3807-L3850) — 优化与调试等级都从高位 fall-through 到低位，所以「最高等级必须等于 `DEFAULT_OPT_LEVEL`/`DEFAULT_DEBUG_LEVEL`」。这两个默认值定义在类开头：

[cxxrtl_backend.cc:3471-3472](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/cxxrtl_backend.cc#L3471-L3472) — `DEFAULT_OPT_LEVEL = 6`、`DEFAULT_DEBUG_LEVEL = 4`。

`-header` 的实际效果——派生出 `.h` 文件名并打开：

[cxxrtl_backend.cc:3852-3865](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/cxxrtl_backend.cc#L3852-L3865) — 取实现文件名 `.` 之前的部分拼上 `.h`，接口写进 `intf_f`，实现写进 `impl_f`。

#### 4.2.4 代码实践

**实践目标**：对比不同 `-O`/`-g` 等级与 `-header` 开关对输出文件的影响，建立对「优化=删线、调试=补可见性」的直觉。

**操作步骤**：

1. 默认参数生成单文件：

   ```
   yosys -p "read_verilog examples/cmos/counter.v; write_cxxrtl a.cc"
   ```

2. 加 `-header` 生成头文件：

   ```
   yosys -p "read_verilog examples/cmos/counter.v; write_cxxrtl -header b.cc"
   ls b.*
   ```

3. 对比 `-O0 -g0` 与默认（`-O6 -g4`）：

   ```
   yosys -p "read_verilog examples/cmos/counter.v; write_cxxrtl -O0 -g0 min.cc"
   wc -l a.cc min.cc
   ```

**需要观察的现象**：
- 步骤 2 产生 `b.cc` 和 `b.h` 两个文件；`b.cc` 顶部会有一行 `#include "b.h"`。
- 步骤 3 中 `min.cc`（无优化、无 debug）应明显比默认的 `a.cc` 行数多还是少？直觉上 `-O0` 不消除任何 wire，内部临时线更多，文件往往**更长**；`-g0` 则少了 debug 相关的 `debug_eval` 代码与 C API 工厂函数。

**预期结果**：`-header` 拆分成功；高优化等级去掉大量中间 wire、生成的类成员更少。精确行数对比「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `-O` 与 `-g` 是正交的、可以同时拉满？

**参考答案**：见 [cxxrtl_backend.cc:3694-3696](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/cxxrtl_backend.cc#L3694-L3696) 的说明：更高的 debug 等级「provide more visibility and generate more code, but do not pessimize evaluation」——debug 代码走单独的 `debug_eval` 路径，不影响主 `eval()` 的性能，所以可与最高优化并存。

**练习 2**：`-noflatten` 会在什么场景下让你「吃亏」？

**参考答案**：帮助文本 [cxxrtl_backend.cc:3657-3661](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/cxxrtl_backend.cc#L3657-L3661) 指出：完全展平且无组合反馈的设计能在一个 delta cycle 内收敛，求值最快；`-noflatten` 保留了层次，每层模块各自 `eval()`，可能需要更多 delta cycle 才能收敛，仿真更慢（但 debug 信息仍用全层次名）。

### 4.3 代码生成核心：从 RTLIL 到 C++ 模块类与 eval 方法

#### 4.3.1 概念说明

这是 cxxrtl 最核心、也最精巧的部分。它要回答三个问题：

1. **一个 RTLIL 模块 → 一个 C++ 什么？** → 一个派生自 `cxxrtl::module` 的类，端口和状态变成成员变量，行为逻辑变成 `eval()` 方法。
2. **硬件的并发信号流 → C++ 的顺序代码，顺序怎么定？** → 用 `FlowGraph` 建数据流依赖图，用 `Scheduler` 做反馈弧最小的拓扑排序，再按这个顺序在 `eval()` 里逐节点「翻译」。
3. **那么多种 RTLIL 对象（`$and`/`$dff`/Process/记忆体/assign）→ C++ 各翻译成什么？** → 按节点类型 `Node::Type` 分发到一组 `dump_*` 函数。

支撑这三件事的还有两个关键概念：

- **WireType（连线分类）**：每根 wire 被归为 `BUFFERED`（双缓冲成员，持有状态，如触发器输出）、`MEMBER`/`OUTLINE`（单缓冲成员）、`LOCAL`/`INLINE`（局部/内联临时）、`ALIAS`/`CONST`（别名/常数）、`UNUSED`。分类决定了它在 C++ 里是一个 `wire<>` 成员、一个 `value<>` 成员、一个局部变量、还是直接被替换掉。
- **mangle（名字改写）**：RTLIL 标识符可含任意字符（仅排除空白），但 C++ 标识符只能字母数字下划线。cxxrtl 用一套可读的改写方案：公有名 `\foo` → `p_foo`，内部名 `$bar` → `i_bar`，下划线转义为 `__`，其他字符用 `_hex_` 包围。

#### 4.3.2 核心流程

```
analyze_design(design)
    │
    ├── 对每个模块建 SigMap（信号归一化，承接 u3-l2）
    ├── 用 Mem 辅助类载入记忆体（承接 u6-l4）
    ├── 建 FlowGraph：为每条 connect / 每个 cell / 每个 process / 每个记忆端口
    │     生成一个 Node，并记录它定义/使用了哪些 wire
    ├── 用 Scheduler 对节点排序（最小化反馈弧）
    └── 据排序与依赖，给每根 wire 定 WireType

dump_design(design)
    │
    ├── 拓扑排序模块（被例化者先于例化者）
    ├── 写 namespace cxxrtl_design { ... }
    └── 对每个模块：
          dump_module_intf() → 类声明（成员变量 + reset()/eval() 声明）
          dump_module_impl() → reset()/eval() 的方法体
                dump_eval_method() → 遍历 schedule[module]，按 Node::Type
                                      调对应 dump_connect/dump_cell_eval/...
```

`eval()` 方法体里最外层是一个 `converged` 标志和按调度顺序排好的节点序列：

[cxxrtl_backend.cc:2164-2216](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/cxxrtl_backend.cc#L2164-L2216) — `dump_eval_method`：先算所有 `posedge_*/negedge_*` 边沿检测，再声明局部 wire，最后按 `schedule[module]` 的顺序遍历节点，按类型分发，结尾 `return converged;`。`eval_converges[module]` 决定初始 `converged` 值（无组合反馈的设计从一开始就为真，可省 delta cycle）。

节点类型的分发是整个翻译的「大 switch」：

[cxxrtl_backend.cc:2187-2213](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/cxxrtl_backend.cc#L2187-L2213) — 八种 `Node::Type`：`CONNECT`（assign）、`CELL_SYNC`/`CELL_EVAL`（单元，含黑盒与子模块）、`EFFECT_SYNC`（`$print`/`$check`）、`PROCESS_CASE`/`PROCESS_SYNC`（未跑 proc 时的 process）、`MEM_RDPORT`/`MEM_WRPORTS`（记忆体端口）。这些类型定义在 FlowGraph 开头：

[cxxrtl_backend.cc:282-291](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/cxxrtl_backend.cc#L282-L291) — `FlowGraph::Node::Type` 枚举。

#### 4.3.3 源码精读

**(a) 模块排序与命名空间布局**。`dump_design` 先对模块做拓扑排序（被例化者排前），再按「黑盒在前、其余按拓扑序」输出：

[cxxrtl_backend.cc:2775-2802](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/cxxrtl_backend.cc#L2775-L2802) — `dump_design`：用 `TopoSort` 按「cell 引用关系」给模块排序，保证父类（被例化的子模块）先于子类（例化它的模块）生成，这样 C++ 里父类完整定义在前。

生成代码的整体骨架（命名空间、include、可选 C API 工厂）：

[cxxrtl_backend.cc:2861-2870](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/cxxrtl_backend.cc#L2861-L2870) — `using namespace cxxrtl_yosys;` 之后进入 `namespace cxxrtl_design`，逐模块 `dump_module_intf` + `dump_module_impl`。

**(b) 反馈弧排序 Scheduler**。组合逻辑可能有 benign 的强连通分量（如进程间的相互依赖），直接拓扑排序会失败。cxxrtl 用 Eades 等人的反馈弧集启发式，把图排成「反馈弧尽量少」的线性序：

[cxxrtl_backend.cc:45-175](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/cxxrtl_backend.cc#L45-L175) — `Scheduler` 模板，文件头 [cxxrtl_backend.cc:33-44](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/cxxrtl_backend.cc#L33-L44) 注释说明动机：反馈弧为零时 `eval()` 能一次收敛，否则需要多个 delta cycle。`schedule()` 方法在 [cxxrtl_backend.cc:148-174](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/cxxrtl_backend.cc#L148-L174)。

**(c) WireType 连线分类**。这是性能的关键——把尽量多的 wire 从「双缓冲成员」降级为「局部变量」甚至「内联表达式」：

[cxxrtl_backend.cc:649-693](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/cxxrtl_backend.cc#L649-L693) — `WireType` 的八种类型与判定辅助函数。`is_buffered()` 决定是否生成 `wire<>`（双缓冲、持状态），其余生成 `value<>` 或被替换。

**(d) 名字改写**。

[cxxrtl_backend.cc:759-787](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/cxxrtl_backend.cc#L759-L787) — `mangle_name`：`\` → `p_`、`$` → `i_`、`_` → `__`、其他字符 → `_XX_`（小写 hex）。配合 `mangle_module_name`（黑盒加 `bb_` 前缀）、`mangle_memory_name`（`memory_` 前缀）、`mangle_cell_name`（`cell_` 前缀）。

**(e) 单元翻译**。组合单元被翻译成内联的 C++ 表达式（依赖 runtime 的 `cxxrtl_yosys` 运算函数），触发器被翻译成边沿/电平敏感的 `if` 块：

[cxxrtl_backend.cc:1134-1220](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/cxxrtl_backend.cc#L1134-L1220) — `dump_cell_expr`：一元/二元单元翻译成 `and_us<8>(...)` 之类的调用（`_us` 后缀编码有/无符号），`$mux` 翻译成三目 `?:`，`$pmux` 翻译成嵌套三目，`$concat`/`$slice` 翻译成 `.concat()`/`.slice<>()`。

[cxxrtl_backend.cc:1406-1467](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/cxxrtl_backend.cc#L1406-L1467) — `is_ff_cell` 分支：触发器按 `CLK_POLARITY` 生成 `if (posedge_… / negedge_…)`，内部再嵌 `EN`/`SRST`/`ARST` 等子条件，把 `Q.next = D`。边沿判定函数 `posedge_*`/`negedge_*` 由边沿敏感的 wire 生成（见 dump_wire 逻辑）。

哪些单元算「可内联」「触发器」由顶部的分类函数决定：

[cxxrtl_backend.cc:200-213](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/cxxrtl_backend.cc#L200-L213) — `is_inlinable_cell`（组合单元）与 `is_ff_cell`（全部时序单元 `$dff/$dffe/$adff/.../$dlatch`）。

#### 4.3.4 代码实践

**实践目标**：跟踪一个 `$dff` 从 RTLIL 到 C++ 的完整翻译，验证你对 4.3.3 的理解。

**操作步骤**：

1. 生成带行号的 C++ 源码并保留 RTLIL 中间结果：

   ```
   yosys -p "read_verilog examples/cmos/counter.v; proc; write_cxxrtl -O0 c.cc; write_rtlil r.txt"
   ```

2. 在 `r.txt` 里找到 counter 的 `$dff`（或 `$adff`）单元，记下它的 `CLK`/`D`/`Q`/`ARST` 端口连接与 `CLK_POLARITY` 参数。

3. 在 `c.cc` 的 `p_counter::eval()` 里找到对应的 `if (posedge_…)` 块，对照端口连接确认 `Q.next = D` 的赋值。

**需要观察的现象**：RTLIL 文本里的 `$dff` cell 与 C++ 里的 `if (posedge_p_clk) { … }` 块一一对应；复位逻辑（`rst`）被翻译成额外的条件分支或异步复位块。

**预期结果**：counter 是 `always @(posedge clk) if(rst) …`，综合后通常含一个带同步复位的 `$dff`（或 `$dffe`），C++ 里表现为 `if (posedge_p_clk) { if (p_rst == value<1>{1u}) { … } else { … } }`。精确形式「待本地验证」（取决于 yosys 版本的 proc/opt 结果）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 cxxrtl 要专门为「同步输出的黑盒单元」单独搞一个 `CELL_SYNC` 节点类型，而不是和普通单元一样处理？

**参考答案**：见 [cxxrtl_backend.cc:398-432](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/cxxrtl_backend.cc#L398-L432) 的详细注释：一个输入连到自己同步输出的黑盒，不会形成组合反馈弧，但若按「赋输入、求值、取输出」的朴素顺序排，连接线无法本地化、需要一个多余 delta cycle 才能收敛。把单元拆成 `CELL_SYNC`（专管同步输出，产出组合 def）与 `CELL_EVAL`（输入+其余输出）两个节点，就能重排代码、消除该 delta cycle。

**练习 2**：`eval_converges[module]` 何时为真？它对仿真性能有什么影响？

**参考答案**：见 [cxxrtl_backend.cc:3246](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/cxxrtl_backend.cc#L3246)：当模块没有「反馈 wire」也没有「缓冲化的组合 wire」时为真。它被写进 `eval()` 开头的 `bool converged = …`（[cxxrtl_backend.cc:2167](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/cxxrtl_backend.cc#L2167)），若为真，`step()` 里 `eval` 一次就能让 `commit()` 后跳出循环，省掉多余 delta cycle，这正是「完全展平 + 无组合反馈」设计最快的原因。

### 4.4 cxxrtl runtime：value/wire/module 与仿真契约

#### 4.4.1 概念说明

生成的 C++ 代码不能独立编译，它 `#include <cxxrtl/cxxrtl.h>`，依赖一套公共 **runtime**。runtime 提供「硬件在 C++ 里如何被表示」的原语：

- `value<Bits>`：任意位宽的**右值**，内部用 `uint32_t` 数组（chunk）存放。它是 cxxrtl 一切运算的基本类型，编译期已知位宽，靠模板让编译器充分展开。
- `wire<Bits>`：任意位宽的**双缓冲左值**，含 `curr`（当前值）和 `next`（下一值）两个 `value<>`。组合/时序逻辑在 `eval()` 里写 `next`，`commit()` 时把 `next` 拷给 `curr`。
- `memory<Width>`：记忆体，`data` 是 `value<Width>[]`。
- `module`：所有生成模块类的抽象基类，定义仿真契约 `reset()`/`eval()`/`commit()`/`step()`。

runtime 还分两层稳定性（见 `runtime/README.txt`）：`cxxrtl_capi*.h`（C API）是**稳定**接口，ABI 不会随意破坏；`cxxrtl*.h`（不含 capi）是**不稳定**接口，可能随版本变化。C API 让你能用 C 或 Python 之类语言经 C ABI 驱动仿真。

#### 4.4.2 核心流程

仿真的核心是 `module::step()` 的「求值—提交」循环（delta cycle）：

```
step():
    deltas = 0
    do:
        converged = eval(performer)   // 据 curr/输入 计算 next
        deltas++
    while (commit() && !converged)    // commit: next→curr，返回是否有变化
    return deltas
```

直觉解释：硬件里组合逻辑是「瞬间传播」的，但 C++ 必须一个节点一个节点算。若调度顺序里存在反馈弧，一趟 `eval()` 不一定能算到最终值——需要多趟：每趟 `eval()` 用当前的 `curr` 算 `next`，`commit()` 把 `next` 落回 `curr`，若还有变化就再 `eval()`，直到不再变化（`commit()` 返回假）或已知会收敛（`converged` 为真）。这就是 [cxxrtl_backend.cc:33-44](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/cxxrtl_backend.cc#L33-L44) 说的「反馈弧为零时可一次收敛」的运行时体现。

时序元件（触发器）靠**边沿检测**工作：runtime 为每个边沿敏感 wire 生成 `posedge_*`/`negedge_*`，它们比较「上一周期值」与「当前值」来判定边沿；边沿只在 `commit()` 把 next 落成 curr 之后才成立，于是触发器在 `eval()` 里写 `next`、`commit()` 后才真正「采样」，天然避免了 Verilog 仿真里的竞争。

#### 4.4.3 源码精读

`module` 基类与仿真契约（本节最重要的运行时代码）：

[cxxrtl.h:1551-1596](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/runtime/cxxrtl/cxxrtl.h#L1551-L1596)（位于 runtime 目录） — `module` 基类：纯虚 `reset()`/`eval(performer)`/`commit()`，`step()` 在 [cxxrtl.h:1575-1583](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/runtime/cxxrtl/cxxrtl.h#L1575-L1583) 实现 delta-cycle 循环。注意 `eval` 把 `performer` 放进虚函数签名（性能），而 `commit` 的 `observer` 重载是非虚的（避免拖慢热路径）。

`wire` 双缓冲：

[cxxrtl.h:773-818](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/runtime/cxxrtl/cxxrtl.h#L773-L818) — `wire<Bits>` 持有 `curr`/`next` 两个 `value<>`；`commit(observer)` 在 `curr != next` 时通知 observer 并把 `next` 拷给 `curr`。`wire` 刻意禁用拷贝（防止把 `value` 升级成 `wire` 时的隐蔽 bug）。

`value` 任意位宽表示：

[cxxrtl.h:112-130](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/runtime/cxxrtl/cxxrtl.h#L112-L130) — `value<Bits>` 用 `chunk_t`（`uint32_t`）数组存位，位宽与 chunk 数都是编译期常量，配合 `CXXRTL_ALWAYS_INLINE`（[cxxrtl.h:55-59](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/runtime/cxxrtl/cxxrtl.h#L55-L59)）让运算被编译器充分内联展开。

C API 入口（稳定接口）：

[cxxrtl_capi.h:53-87](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/runtime/cxxrtl/capi/cxxrtl_capi.h#L53-L87) — `cxxrtl_create`（从 toplevel 造 handle）、`cxxrtl_eval`/`cxxrtl_commit`/`cxxrtl_step`、`cxxrtl_reset`，与 C++ 的 `module` 接口一一对应，但走 C ABI，便于 Python 等绑定。

runtime 的部署方式与稳定性约定：

[README.txt:1-19](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/runtime/README.txt#L1-L19) — runtime 目录放在仿真程序的 `-I` include 路径上，**不**进 yosys 二进制；`cxxrtl_capi*.h` 稳定、`cxxrtl*.h`（不含 capi）不稳定。

CMakeLists 把这些 runtime 文件作为 DATA_FILES 安装：

[CMakeLists.txt:6-14](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/CMakeLists.txt#L6-L14) — `DATA_DIR include/backends/cxxrtl` 下的运行时头/源文件，安装后供仿真程序 `-I` 引用。

#### 4.4.4 代码实践

**实践目标**：完整跑通「Yosys 生成 → 用户写 main → 编译运行」全链路，得到计数器的软件仿真输出。这是本讲的主实践。

**操作步骤**：

1. 生成 C++（默认 `-O6 -g4`，单文件）：

   ```
   yosys -p "read_verilog examples/cmos/counter.v; write_cxxrtl counter.cc"
   ```

2. 编写驱动 `main.cc`（**示例代码**，仿照帮助文本 [cxxrtl_backend.cc:3487-3499](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/cxxrtl_backend.cc#L3487-L3499) 的模板）：

   ```cpp
   // 示例代码：cxxrtl 计数器驱动
   #include "counter.cc"            // 由 write_cxxrtl 生成
   #include <cstdint>
   #include <iostream>

   int main() {
       cxxrtl_design::p_counter top;   // 实例化顶层模块类
       top.reset();                    // 上电复位到初值
       top.p_rst.set(true);            // 先复位
       for (int i = 0; i < 4; ++i) {   // 复位几个周期
           top.p_clk.set(false); top.step();
           top.p_clk.set(true);  top.step();
       }
       top.p_rst.set(false);           // 释放复位
       top.p_en.set(true);             // 使能计数
       for (int i = 0; i < 8; ++i) {
           top.p_clk.set(false); top.step();
           top.p_clk.set(true);  top.step();
           // count 是 output，默认经 C++ 接口可读其 .curr
           std::cout << "count = " << (int)top.p_count.get<uint32_t>() << "\n";
       }
       return 0;
   }
   ```

   > 说明：`p_clk`/`p_rst`/`p_en`/`p_count` 是端口成员名（`p_` 来自 mangle 的公有名前缀）；输入用 `.set()` 写 `next`，输出用 `.curr`（或 `.get<T>()`）读当前值。端口是否为 `wire<>`（带 `.curr`）还是 `value<>` 取决于优化等级与是否在顶层，若编译报错可改用 debug_items/C API 读取，详见 runtime 文档。

3. 编译（需把 runtime 加入 include 路径，C++20）：

   ```
   c++ -std=c++20 -O2 -Ibackends/cxxrtl/runtime main.cc -o sim
   ```

   > runtime 路径相对仓库根为 `backends/cxxrtl/runtime`；若已 `make install`，则用安装前缀下的 `include/backends/cxxrtl`。`README.txt` 明确推荐用 `-I${YOSYS}/backends/cxxrtl/runtime`。

4. 运行：

   ```
   ./sim
   ```

**需要观察的现象**：每翻转一次时钟，`count` 在复位释放后按 `0,1,2,3,4,5,6,7,0,...`（3 位，模 8）递增，验证了 `step()` → `eval()`/`commit()` 的 delta-cycle 机制正确传播了边沿。

**预期结果**：打印 `count = 1` 起逐行递增，到 7 后回绕到 0。由于本讲未在本环境实跑，具体行与端口访问形式标注「待本地验证」——若 `top.p_count` 因优化被去掉 `.curr`，请改用 `-g2` 及以上并在生成时保留 debug，或经 `debug_items`/C API 读取。

#### 4.4.5 小练习与答案

**练习 1**：`eval()` 与 `commit()` 为什么要拆成两步，而不是直接在 `eval()` 里把结果写回 `curr`？

**参考答案**：因为一个 delta cycle 内所有节点应基于**同一组** `curr` 值求值，才能得到确定的组合逻辑结果。若边算边写回 `curr`，求值顺序就会影响结果（破坏「并发语义」）。拆成两步后：`eval()` 只写 `next`，所有节点都看一致的 `curr`；`commit()` 统一把 `next` 落成 `curr`。`wire::commit`（[cxxrtl.h:810-817](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/runtime/cxxrtl/cxxrtl.h#L810-L817)）正是这个 `curr = next` 的动作。

**练习 2**：C API（`cxxrtl_capi.h`）与 C++ 接口（`cxxrtl.h`）的稳定性承诺不同，这对使用者意味着什么？

**参考答案**：见 [README.txt:5-16](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/runtime/README.txt#L5-L16)：C API 接口与 ABI 都稳定，破坏性变更会让下游「可见地失败」，适合需要长期维护、跨语言（如 Python ctypes）的场景；C++ 接口不稳定，可能随版本变，适合与生成代码同源、同版本一起编译的场景。简言之：跨进程/跨语言用 C API，单 C++ 程序内嵌用 C++ 接口。

## 5. 综合实践

把本讲三块知识（代码生成、用法、runtime）串成一个完整任务：**给计数器加一个 VCD 波形导出**。

1. 用 `write_cxxrtl` 生成 `counter.cc`（默认 `-g4`，保留 debug）。
2. 写一个 `main.cc`：实例化 `cxxrtl_design::p_counter`，构造一个 `cxxrtl::vcd_writer`（见 runtime 的 `cxxrtl_vcd.h`），在每次 `step()` 前后调用 `vcd_writer` 记录 `p_clk`、`p_rst`、`p_en`、`p_count`。
3. 翻转若干个时钟周期（含复位与使能），运行程序生成 `waveform.vcd`。
4. 用 GTKWave 打开 `waveform.vcd`，确认 `count` 在每个 `clk` 上升沿按预期变化。

这个任务要求你：调用 `write_cxxrtl` 选对选项（4.2）、理解生成的类与端口成员（4.3）、并正确使用 runtime 的 VCD 工具与 step 契约（4.4）。VCD writer 的具体 API 细节请阅读 `backends/cxxrtl/runtime/cxxrtl/cxxrtl_vcd.h` 头文件注释；精确调用形式「待本地验证」。

## 6. 本讲小结

- cxxrtl 是**代码生成后端**：输出可编译的 C++ 源码，而非给人看的网表，本质区别于 u7-l1 的三个文本后端。
- `write_cxxrtl` 在生成前会主动跑 `hierarchy`/`flatten`/`proc` 把设计拍平成可顺序求值的平面网表（`prepare_design`），再由 `dump_design` 写 C++。
- 代码生成的核心是**求值顺序**：`FlowGraph` 建数据流图，`Scheduler`（Eades 反馈弧启发式）排序，`WireType` 把 wire 分成缓冲成员/局部/内联等类，逐节点翻译进 `eval()`。
- RTLIL 对象按 `Node::Type` 分发：组合单元→内联表达式，触发器→`posedge_`/`negedge_` 守卫的赋值，Process/记忆体各有对应 `dump_*`。
- runtime 提供 `value<>`（右值）/`wire<>`（curr+next 双缓冲）/`module` 基类；仿真契约是 `eval()`（算 next）→`commit()`（next 落 curr）→`step()` 的 delta-cycle 循环。
- 选项 `-O`（默认 6，删线提速）与 `-g`（默认 4，补可见性）正交；C API 稳定、C++ 接口不稳定；runtime 经 `-I` 进入仿真程序，不进 yosys 二进制。

## 7. 下一步学习建议

- **深入 runtime**：通读 `backends/cxxrtl/runtime/cxxrtl/cxxrtl.h`，尤其是 `cxxrtl_yosys` 命名空间下内部单元的运算函数（如 `and_us`/`add_uu`），看它们如何与 4.3 的 `dump_cell_expr` 输出对接。
- **黑盒扩展**：阅读帮助文本 [cxxrtl_backend.cc:3505-3601](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/cxxrtl/cxxrtl_backend.cc#L3505-L3601) 关于 `cxxrtl_blackbox`/`cxxrtl_edge`/`cxxrtl_template`/`cxxrtl_comb`/`cxxrtl_sync` 属性的说明，尝试用 C++ 实现一个黑盒（如虚拟 UART）并接入仿真。
- **横向对比**：把 cxxrtl 与 u7-l3 的形式验证后端（smt2/aiger/btor）对比——两者都「把 RTLIL 翻译成另一种语言」，但目标一个是可执行 C++、一个是可判定的逻辑公式，体会后端的「目标驱动」设计。
- **进到专家层**：学完 u7 三讲后，可进入 u8（工艺库与厂商综合流程），或回到 u10 看形式验证机制如何复用本讲提到的边沿/delta-cycle 思想。
