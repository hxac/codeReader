# proc：行为级 always 到门级网表

## 1. 本讲目标

读完本讲，你应当能够：

- 说清楚 `RTLIL::Process` 在内存里长什么样，以及它为什么存在；
- 说出 `proc` 这条「宏命令」按什么顺序调用哪些子 pass，以及每个子 pass 的大致职责；
- 解释 `proc_mux` 如何把 `if`/`case` 的「决策树」翻译成 `$mux`/`$pmux` 网络；
- 解释 `proc_dff` 与 `proc_dlatch` 如何根据敏感事件推断出触发器与锁存器；
- 亲自动手观察一段 `always @(posedge …)` 设计在执行 `proc` 前后，RTLIL 文本里到底发生了什么变化。

## 2. 前置知识

本讲是「核心综合流程」的第二讲，直接承接 [u6-l1 层次管理与展平](u6-l1-hierarchy-flatten.md)。在继续之前，你需要先具备以下认知（前面讲义已建立）：

- **RTLIL 是统一中间表示**：前端产出 RTLIL、后端消费 RTLIL，所有 pass 都在 RTLIL 上工作（见 u2-l1）。
- **Module / Wire / Cell / SigSpec 的关系**：Module 拥有 Wire 与 Cell，Cell 用 SigSpec 经端口连接 Wire（见 u2-l3、u3-l1）。
- **Pass 的注册与调用**：每条命令是一条 `Pass`，`Pass::call(design, "命令名")` 可在 pass 内部再触发另一条 pass（见 u4-l1）。
- **内部单元库**：Yosys 用 `$and`/`$mux`/`$dff`/`$adff` 等以 `$` 开头的单元作为统一目标（见 u3-l4）。本讲会反复出现 `$mux`、`$pmux`、`$eq`、`$reduce_or`、`$dff`、`$adff`、`$ff`、`$dlatch` 等单元。
- **综合主流程**：`synth` 的 coarse 阶段会调用 `proc`，把行为级描述「拍平」成门级网表（见 u4-l2）。

一个关键的直觉先建立起来：你写的 `always` 块，在 `read_verilog` 之后**并不是** `$dff` 或 `$mux`，而是一个叫 **Process（过程）** 的、更接近源代码语义的对象。`proc` 的工作，就是把这些 Process「编译」成纯门级的 `$mux`/`$dff`/`$dlatch`。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `passes/proc/` 与 `kernel/` 下：

| 文件 | 作用 |
|------|------|
| `kernel/rtlil.h` | 定义 `RTLIL::Process`、`CaseRule`、`SwitchRule`、`SyncRule` 与 `SyncType`，是 `proc` 操作的对象 |
| `passes/proc/proc.cc` | `proc` 宏命令本身，按固定顺序调用各 `proc_*` 子 pass |
| `passes/proc/proc_clean.cc` | 清理决策树中的空分支、删除空 Process |
| `passes/proc/proc_mux.cc` | 把决策树（`if`/`case`）翻译成 `$mux`/`$pmux` 网络 |
| `passes/proc/proc_dff.cc` | 从敏感事件中推断触发器，生成 `$dff`/`$adff`/`$ff`/`$aldff`/`$dffsr` |
| `passes/proc/proc_dlatch.cc` | 从组合反馈中识别锁存器，生成 `$dlatch`/`$adlatch` |

此外目录下还有 `proc_rmdead`/`proc_prune`/`proc_init`/`proc_arst`/`proc_rom`/`proc_memwr` 等，它们是流程中的「配角」，本讲只在总流程里点名带过，不展开。

## 4. 核心概念与源码讲解

### 4.1 proc 总流程与 RTLIL::Process

#### 4.1.1 概念说明

当你写下：

```verilog
always @(posedge clk) begin
    if (load) q <= din;
    q <= q + 1;   // 这里只是举例，说明结构
end
```

这段代码里同时包含两类信息：

1. **「在什么条件下，信号取什么值」**——这是数据/控制逻辑，对应 `if`/`case`。
2. **「这些值在什么时候被提交（commit）」**——这是时序语义，对应敏感列表 `posedge clk`、复位等。

Yosys 的做法是：`read_verilog` 把这两类信息一起装进一个 `RTLIL::Process` 对象，**暂时不**拆成 `$mux` 与 `$dff`。原因是行为级语义里「条件赋值」和「时钟提交」是耦合在一起的，需要专门的 pass 来仔细地、按语义地把它们分开。这个专门的 pass 就是 `proc`。

> 为什么不在前端直接生成 `$dff`？因为 Verilog 的 `always` 语义非常灵活（异步复位、电平敏感锁存、`always @*` 纯组合、多重驱动等），前端做这种推断会非常臃肿。Yosys 选择「先忠实记录语义（Process），再由独立的 pass 链逐步规范化」，这是关注点分离的典型设计。

Process 的内存结构定义在 `kernel/rtlil.h`：

- [`kernel/rtlil.h:2614-2634`](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L2614-L2634) —— `RTLIL::Process` 只有两个核心成员：`root_case`（决策树）和 `syncs`（敏感事件列表）。

决策树由两种互相嵌套的结构组成：

- [`kernel/rtlil.h:2564-2577`](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L2564-L2577) —— `CaseRule`（一个分支）：含 `compare`（匹配的模式，即 case 的取值）、`actions`（本分支内的赋值 `lhs=rhs`）、`switches`（嵌套的子 `if`/`case`）。
- [`kernel/rtlil.h:2579-2591`](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L2579-L2591) —— `SwitchRule`（一个 switch/if）：含 `signal`（被判断的信号）和 `cases`（若干分支 `CaseRule`）。`if` 在内部也是一种 `SwitchRule`，条件被包成一个一位信号。

敏感事件则用 `SyncRule` 描述：

- [`kernel/rtlil.h:2602-2612`](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L2602-L2612) —— `SyncRule`：含 `type`（事件类型）、`signal`（触发信号，如时钟/复位）、`actions`（该事件发生时要提交的赋值）。
- [`kernel/rtlil.h:42-51`](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L42-L51) —— `SyncType` 枚举，是理解 `proc_dff` 的钥匙：

| 枚举 | 含义 | 典型来源 |
|------|------|----------|
| `ST0` / `ST1` | 电平敏感：信号为 0 / 为 1 时有效 | 异步复位（低/高有效） |
| `STp` / `STn` | 边沿敏感：上升沿 / 下降沿 | `posedge clk` / `negedge clk` |
| `STe` | 双沿敏感 | `always @(clk)` |
| `STa` | 恒有效 | `always @*` / `always @(*)`（组合或锁存） |
| `STg` | 全局时钟 | `always @($global_clock)` |
| `STi` | 初始化 | `initial` 中的初值 |

> 一句话心智模型：`root_case` 描述「值」，`syncs` 描述「时机」。`proc_mux` 翻译 `root_case`，`proc_dff`/`proc_dlatch` 翻译 `syncs`。

#### 4.1.2 核心流程

`proc` 本身不做综合算法，它是一条「编排型」命令：直接继承自 `Pass`（注意：它不是 u4-l2 讲的 `ScriptPass`，而是更朴素地用一串 `Pass::call(design, "…")` 手动串联子 pass），按以下固定顺序调用：

```
proc_clean        # 清理空分支，规范化决策树
proc_rmdead       # 删除不可达分支（-ifx 模式下跳过）
proc_prune        # 裁剪冗余分支
proc_init         # 处理 initial 块（STi）
proc_arst         # 识别「边沿 + 赋常数」的异步复位模式，转成电平敏感 ST0/ST1
proc_rom          # 把常量查找行为识别为 ROM
proc_mux          # 决策树 → $mux/$pmux 网络
proc_dlatch       # 组合反馈 → 锁存器；纯组合 → 直连
proc_dff          # 边沿事件 → 触发器
proc_memwr        # 把存储器写动作归整为 $memrd/$memwr
proc_clean        # 再清理一遍（此时 Process 应已空，会被删除）
opt_expr -keepdc  # 常量传播等局部优化，保留 don't-care
```

注意三个要点：

1. **顺序敏感**：必须先 `proc_arst`（把异步复位规范成电平敏感），再 `proc_mux`（生成数据多路器），最后 `proc_dff`（才能正确识别复位端口）；`proc_dlatch` 必须在 `proc_dff` 之前，先把组合反馈吃掉变成锁存器，剩下的边沿事件才交给 `proc_dff`。
2. **可裁剪**：`proc` 提供 `-nomux`/`-norom`/`-noopt` 等开关，以及 `-global_arst`、`-ifx`、`-latches` 等透传选项。
3. **收尾**：理论上经过 `proc_dff` 后，所有 `syncs` 都被消费完，`root_case` 也已变成 mux，因此最后的 `proc_clean` 会发现 Process 已空并将其从模块中删除——这正是「process 消失」的来源。

#### 4.1.3 源码精读

先看 `proc` 命令的定义与帮助文本，它把上面那张顺序表原样写进了 `help()`：

- [`passes/proc/proc.cc:28-29`](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc.cc#L28-L29) —— `ProcPass` 构造，命令名注册为 `"proc"`。
- [`passes/proc/proc.cc:36-49`](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc.cc#L36-L49) —— `help()` 里印出的子 pass 顺序清单（与官方文档 `docs/source/code_examples/macro_commands/proc.ys` 完全一致）。

真正的编排逻辑在 `execute()` 里，逐条 `Pass::call`：

```cpp
Pass::call(design, "proc_clean");
if (!ifxmode)
    Pass::call(design, "proc_rmdead");
Pass::call(design, "proc_prune");
Pass::call(design, "proc_init");
if (global_arst.empty())
    Pass::call(design, "proc_arst");
else
    Pass::call(design, "proc_arst -global_arst " + global_arst);
if (!norom)
    Pass::call(design, "proc_rom");
if (!nomux)
    Pass::call(design, ifxmode ? "proc_mux -ifx" : "proc_mux");
if (latches.empty())
    Pass::call(design, "proc_dlatch");
else
    Pass::call(design, "proc_dlatch -latches " + latches);
Pass::call(design, "proc_dff");
Pass::call(design, "proc_memwr");
Pass::call(design, "proc_clean");
if (!noopt)
    Pass::call(design, "opt_expr -keepdc");
```

- [`passes/proc/proc.cc:119-140`](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc.cc#L119-L140) —— 上面这段编排的源码位置。可以清楚看到 `-nomux`/`-norom`/`-noopt`/`-ifx`/`-latches`/`-global_arst` 各自只影响其中一两行，整体骨架不变。

再看 `proc_clean` 如何删掉「已变空」的 Process——这是「process 消失」的最终落点：

- [`passes/proc/proc_clean.cc:212-225`](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc_clean.cc#L212-L225) —— 当一个 Process 的 `syncs`、`root_case.switches`、`root_case.actions` 三者皆空时，调用 `mod->remove(proc)` 把它从模块里删掉。

#### 4.1.4 代码实践

**实践目标**：在执行 `proc` 之前，亲眼看一眼 RTLIL 文本里的 Process 长什么样。

**操作步骤**：

1. 准备一个最小设计 `dff_arst.v`（复用官方示例 `docs/source/code_examples/synth_flow/proc_01.v`）：

   ```verilog
   module test(input D, C, R, output reg Q);
       always @(posedge C, posedge R)
           if (R) Q <= 0;
           else   Q <= D;
   endmodule
   ```

2. 用脚本 `before.ys` 只读入、**不**执行 `proc`，直接导出 RTLIL：

   ```
   read_verilog dff_arst.v
   hierarchy -check -top test
   write_rtlil before.il
   ```

3. 运行 `yosys -s before.ys`，然后用任意文本工具查看 `before.il`。

**需要观察的现象**：在 `before.il` 里你会看到一个 `process $proc$test…` 块，内部含：

- 一个嵌套的 `switch`（对应 `if (R)`），其下两个分支分别给出 `Q` 的取值（`0` 与 `D`）；
- 两条 `sync posedge C` 与 `sync posedge R`，各自带一条对 `Q` 的 `update` 动作。

**预期结果**：此时设计中**没有** `$dff`/`$adff`/`$mux`，只有 `process`。这正是 `proc` 待处理的输入形态。具体文本细节可能随版本略有差异（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：`proc` 与 `synth`（u4-l2）在结构上有何相似与不同？

> 答案：两者都是「编排型」命令，自身不实现算法、只负责按顺序调用别的 pass。不同的是 `synth` 用 `ScriptPass` 框架（带阶段标签、双模式 help、`-run` 范围控制），而 `proc` 是更朴素的 `Pass`，直接在 `execute()` 里一串 `Pass::call` 硬编码顺序。

**练习 2**：为什么 `proc_dlatch` 必须排在 `proc_dff` 之前？

> 答案：`proc_dlatch` 负责处理 `STa`（恒有效）类敏感事件——其中构成组合反馈的会变成锁存器，纯组合的会被改写成普通连线。只有先把这些「不是触发器」的情况分流掉，剩下到 `proc_dff` 眼里的边沿事件（`STp`/`STn`）才是干净、明确的触发器候选，否则会把本应是锁存器的信号误判。

### 4.2 proc_mux：决策树转多路选择器

#### 4.2.1 概念说明

`proc_mux` 的任务是把 `Process::root_case` 这棵「决策树」翻译成纯组合的 `$mux`/`$pmux` 网络。它的输入是树状的 `CaseRule`/`SwitchRule`，输出是一堆「给定条件，选出某个值」的多路选择器单元。

`$mux` 的语义是：

\[
Y = S \,?\, B : A
\]

即选择信号 `S` 为 1 时输出 `B`，否则输出 `A`（见 u3-l4 内部单元库）。`$pmux` 是 `$mux` 的「宽选择」版本：一个 `S` 是多位、`B` 是多组并行数据的优先多路器，常用来在一条语句里表达一整组 `case` 分支，比把每个分支都嵌套成一个 `$mux` 更省。

#### 4.2.2 核心流程

`proc_mux` 对每个 Process 做三件事（伪代码）：

```
proc_mux(mod, proc):
    1. SigSnippets: 扫描整棵决策树，找出所有「被赋值的信号位」，
       把总是连在一起被赋值的位归并成若干 snippet（连续片段）。
    2. SnippetSwCache: 对每个 snippet，记录它「穿过」了哪些 SwitchRule。
    3. 对每个 snippet，调用 signal_to_mux_tree，递归地把决策树翻译成一棵 mux 树，
       最后用 mod->connect(sig, value) 把这棵 mux 树的输出接到目标信号上。
```

`signal_to_mux_tree` 是核心递归。它对当前 `CaseRule`：

1. 先把本层 `actions`（无条件赋值）盖到结果上；
2. 对每个相关的 `SwitchRule`，**逆序**遍历各分支（保证源代码里写在前面的分支优先级最高），对每个分支：
   - 递归求出该分支下的值；
   - 用 `gen_cmp` 生成「信号 == 分支模式」的比较逻辑，得到一位选择信号；
   - 若该分支与前一个分支属于「同一并行组」，用 `append_pmux` 把它追加进一个 `$pmux`；否则用 `gen_mux` 新建一个 `$mux`。

「并行组」的判定（`pgroups`）很巧妙：如果一组 case 的取值互不重叠（`is_simple_parallel_case`），它们就可以并入同一个 `$pmux`，而不是串成多层 `$mux`。`parallel_case`/`full_case` 属性（来自 Verilog 的 `(* ... *)`）会影响这一步。

`gen_cmp` 生成的比较网络：

\[
\text{ctrl} = \bigvee_{i}\;(\text{signal} == \text{compare}_i)
\]

即「信号等于任意一个匹配模式」时该分支命中。当匹配模式只有「信号本身为 1」这一种时，会省去 `$eq`，直接用信号本身作为选择位（一条短路优化）。

#### 4.2.3 源码精读

先看顶层的 `proc_mux` 函数，对应上面三步：

- [`passes/proc/proc_mux.cc:413-437`](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc_mux.cc#L413-L437) —— 构造 `SigSnippets` 与 `SnippetSwCache`，对每个 snippet 调 `signal_to_mux_tree`，最后 `mod->connect(sig, value)` 接线。

再看比较逻辑 `gen_cmp`：

- [`passes/proc/proc_mux.cc:153-219`](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc_mux.cc#L153-L219) —— 对每个匹配模式生成一个比较单元；当模式是「信号为 1」时直接连线（[L174-177](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc_mux.cc#L174-L177)），否则生成 `$eq`/`$eqx`（`-ifx` 模式用 `$eqx`，严格按 Verilog 仿真语义处理 x）（[L181](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc_mux.cc#L181)）；多个比较位再用 `$reduce_or` 归约成一位选择信号（[L207-216](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc_mux.cc#L207-L216)）。

接着是建 `$mux` 的 `gen_mux`：

- [`passes/proc/proc_mux.cc:221-253`](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc_mux.cc#L221-L253) —— 新建一个 `$mux`，端口约定为 `A=else_signal`（默认值/低优先级）、`B=when_signal`（命中值/高优先级）、`S=ctrl`、`Y=新线`（[L242-249](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc_mux.cc#L242-L249)）。这正对应公式 \(Y=S?B:A\)。

把多个并行分支并入 `$pmux` 的 `append_pmux`：

- [`passes/proc/proc_mux.cc:255-276`](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc_mux.cc#L255-L276) —— 把已建好的 `$mux` 单元**就地升级**成 `$pmux`（`last_mux_cell->type = ID($pmux)`，[L265](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc_mux.cc#L265)），再向其 `S` 追加一位选择信号、向 `B` 追加一组数据。

最后是主递归 `signal_to_mux_tree`：

- [`passes/proc/proc_mux.cc:321-411`](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc_mux.cc#L321-L411) —— 处理 actions、判定并行组、逆序遍历分支；关键的是 [L399-407](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc_mux.cc#L399-L407)：`case_idx = sw->cases.size() - i - 1` 实现「逆序」，并在同一并行组内调 `append_pmux`、否则调 `gen_mux`。

#### 4.2.4 代码实践

**实践目标**：单独运行 `proc_mux`，观察 `if` 是如何变成 `$mux` 的。

**操作步骤**：

1. 准备 `mux_demo.v`：

   ```verilog
   module demo(input [1:0] sel, input [3:0] a, b, c, d, output reg [3:0] y);
       always @(*)
           case (sel)
               2'd0: y = a;
               2'd1: y = b;
               2'd2: y = c;
               default: y = d;
           endcase
   endmodule
   ```

2. 脚本 `mux.ys`：

   ```
   read_verilog mux_demo.v
   hierarchy -check -top demo
   proc_clean          # 先把决策树规范化
   write_rtlil step1.il
   proc_mux            # 只跑 mux 这一步
   write_rtlil step2.il
   ```

3. 运行 `yosys -s mux.ys`，对比 `step1.il`（含 `process` + `switch`/`case`）与 `step2.il`。

**需要观察的现象**：

- `step1.il` 里有一个四分支的 `switch`，判断信号是 `sel`；
- `step2.il` 里 `process` 的决策树被替换成了 `$eq`（比较 `sel==0/1/2`）、`$reduce_or`，以及一个 **`$pmux`**（四路选择）或若干 `$mux`，输出驱动 `y`。

**预期结果**：`case` 被编译成 `sel` 上的优先多路器。四个取值互不重叠，故应归并成一个 `$pmux`（待本地验证具体是 `$pmux` 还是 `$mux` 链，取决于 `pgroups` 判定与是否已有 `parallel_case` 属性）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `signal_to_mux_tree` 要「逆序」遍历分支？

> 答案：Verilog 里写在前面的分支优先级更高。逆序遍历，把后面的（低优先级）分支先作为 `$mux` 的 `A`（默认值），再用前面的（高优先级）分支不断「盖」上去作为 `B`，最终结果就是「前面命中则取前面」，自然实现了优先级。

**练习 2**：`gen_cmp` 里对「信号为 1」的特殊处理省掉了什么单元？为什么这样做是正确的？

> 答案：省掉了一个 `signal == 1` 的 `$eq`，直接把信号本身当作选择位。因为 `signal == 1` 当且仅当 `signal` 本身为真，二者等价，去掉比较器既正确又省一个单元——这是 yosys 里典型的「短路优化」。

### 4.3 proc_dff / proc_dlatch：时序元件推断

#### 4.3.1 概念说明

`proc_mux` 解决了「值」的问题，剩下的问题是「时机」：这些值何时被存进寄存器？答案藏在 `Process::syncs` 里。`proc` 用两个 pass 分工：

- **`proc_dff`**：处理**边沿敏感**事件（`STp`/`STn`），推断出触发器；
- **`proc_dlatch`**：处理**恒有效**事件（`STa`），识别出锁存器（或纯组合逻辑）。

两者会消费 `syncs` 里的 `actions`：每推断出一个寄存器/锁存器，就把对应信号从所有 `syncs` 的动作里「摘掉」（`action.first.remove2(sig, …)`），直到没有剩余 lvalue，Process 自然变空。

触发器有多种变体，`proc_dff` 会根据「有没有时钟」「有没有异步复位」「复位值是否常数」分别选择不同单元：

| 条件 | 生成的单元 |
|------|-----------|
| 无时钟（全局时钟 `STg`） | `$ff` |
| 有时钟、无异步复位 | `$dff` |
| 有时钟、有异步复位、复位值常数 | `$adff` |
| 有时钟、异步复位值非常数 | `$aldff`（异步加载） |
| 有时钟、多条不同复位值的异步规则 | `$dffsr`（带 set/reset） |

锁存器则相对简单：`$dlatch`（带使能的透明锁存器），以及带异步置位/复位的 `$adlatch`。

#### 4.3.2 核心流程

**proc_dff** 的主循环（每个 Process 反复执行，直到找不到 lvalue）：

```
while (sig = find_any_lvalue(proc)) is not empty:
    遍历 proc->syncs 的每个 action：
        按 sync->type 分类：
          ST0/ST1  → 异步复位规则（记录复位值 + 触发），加入 async_rules
          STp/STn  → 时钟边沿（记 sync_edge，把赋值填入 D 端 insig）
          STa      → 恒有效（组合，记 sync_always）
          STg      → 全局时钟
        把 sig 从该 action 中摘除
    决策生成哪种单元：
      - sync_always 且无其它事件 → 直接 connect（纯组合，不建寄存器）
      - 既无 edge 也非 global_clock → 报错（缺少时钟）
      - async_rules 多于 1 且复位值不同 → gen_dffsr_complex ($dffsr)
      - 复位值非常数 → gen_aldff ($aldff)
      - 否则 → gen_dff ($ff / $adff / $dff)
```

`find_any_lvalue` 找一个「在多个 sync 里都被赋值的公共信号」作为本轮处理对象，保证一个寄存器被一次性完整推断。

**proc_dlatch** 的核心是「组合反馈检测」：

```
对每个 STa（恒有效）sync：
    对其每个赋值 lhs = rhs：
        quickcheck(rhs, lhs)：粗筛 rhs 是否经过 mux 网络最终反馈回 lhs
        若无反馈  → 纯组合，改写成普通 connect
        若有反馈  → 这是锁存器
对每个锁存器位：
    find_mux_feedback：精确分析 mux 树，求出「保持条件」hold
    find_mux_constant(..., S0/S1)：求出异步复位/置位条件
    生成 en = NOT(hold) 作为锁存器使能
    建单元：有复位→$adlatch，否则→$dlatch
```

直觉上：一个 `always @*` 块里，如果某信号在某些输入组合下「没有被赋新值」，那它就必须「保持原值」——而「保持原值」在纯组合电路里只能靠**反馈环**实现，这就是锁存器。`proc_dlatch` 正是通过在 `proc_mux` 已生成的 mux 网络里找反馈环来识别锁存器的。

#### 4.3.3 源码精读

先看 `proc_dff` 的主循环与事件分类：

- [`passes/proc/proc_dff.cc:147-289`](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc_dff.cc#L147-L289) —— `proc_dff` 主函数。
- [`passes/proc/proc_dff.cc:172-204`](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc_dff.cc#L172-L204) —— 按 `sync->type` 分类：`ST0`/`ST1` 进 `async_rules`（[L178-182](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc_dff.cc#L178-L182)），`STp`/`STn` 设 `sync_edge` 与 `insig`（[L183-188](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc_dff.cc#L183-L188)），`STa` 设 `sync_always`（[L189-194](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc_dff.cc#L189-L194)）；并在 [L203](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc_dff.cc#L203) 把信号从动作里摘除。
- [`passes/proc/proc_dff.cc:257-265`](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc_dff.cc#L257-L265) —— 缺时钟报错；多条异步规则 → `gen_dffsr_complex`。
- [`passes/proc/proc_dff.cc:283-287`](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc_dff.cc#L283-L287) —— 默认走 `gen_dff`。

`gen_dff` 用一个三元表达式选单元类型，是本讲最值得记的一行：

```cpp
RTLIL::Cell *cell = mod->addCell(sstr.str(),
    clk.empty() ? ID($ff) : arst ? ID($adff) : ID($dff));
```

- [`passes/proc/proc_dff.cc:113-145`](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc_dff.cc#L113-L145) —— `gen_dff` 全文，[L119](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc_dff.cc#L119) 选类型，随后设 `WIDTH`/`ARST_VALUE`/`CLK_POLARITY` 等参数并接 `D`/`Q`/`CLK`/`ARST` 端口。

非常数复位值的情况：

- [`passes/proc/proc_dff.cc:273-281`](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc_dff.cc#L273-L281) —— 复位值无法常量求值时走 `gen_aldff`，对应 [`passes/proc/proc_dff.cc:91-111`](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc_dff.cc#L91-L111) 的 `$aldff`（异步加载端口 `AD`/`ALOAD`）。

`find_any_lvalue`：

- [`passes/proc/proc_dff.cc:31-54`](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc_dff.cc#L31-L54) —— 在所有 sync 的动作里求「公共被赋值信号」，作为本轮要推断的寄存器。

再看 `proc_dlatch` 的核心：

- [`passes/proc/proc_dlatch.cc:425-567`](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc_dlatch.cc#L425-L567) —— `proc_dlatch` 主函数。
- [`passes/proc/proc_dlatch.cc:432-457`](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc_dlatch.cc#L432-L457) —— 遍历 `STa` sync 的动作，用 `quickcheck` 粗筛反馈，无反馈的转普通 connect，有反馈的收集为锁存器候选。
- [`passes/proc/proc_dlatch.cc:543-547`](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc_dlatch.cc#L543-L547) —— 真正建单元：有异步复位/置位 → `addAdlatch`（`$adlatch`），否则 → `addDlatch`（`$dlatch`）；使能端 `en = NOT(make_hold(n))`（[L537](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc_dlatch.cc#L537)），即「不满足保持条件时锁存器透明」。

`make_hold` 把反馈分析得到的「保持条件树」还原成一棵 `And`/`Or`/`Eq` 逻辑（[`passes/proc/proc_dlatch.cc:302-338`](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc_dlatch.cc#L302-L338)），它就是锁存器使能的反逻辑。

最后看 pass 注册与调度入口：

- [`passes/proc/proc_dff.cc:291-315`](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc_dff.cc#L291-L315) —— `ProcDffPass`，对每个模块构造一个 `ConstEval`（用于常量求值复位值），再对每个选中 process 调 `proc_dff`。
- [`passes/proc/proc_dlatch.cc:569-621`](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc_dlatch.cc#L569-L621) —— `ProcDlatchPass`，`-latches info|warn|error` 控制锁存器推断的报告级别（也可用 scratchpad 变量 `proc.latches`）。

#### 4.3.4 代码实践

**实践目标**：完整跑一遍 `proc`，验证 4.1.4 里看到的 Process 最终被替换成 `$mux` + `$adff`。

**操作步骤**：

1. 沿用 `dff_arst.v`（4.1.4 那个异步复位 DFF）。
2. 脚本 `after.ys`：

   ```
   read_verilog dff_arst.v
   hierarchy -check -top test
   proc
   write_rtlil after.il
   stat
   ```

3. 运行 `yosys -s after.ys`，查看 `after.il` 与 `stat` 输出。

**需要观察的现象**：

- `after.il` 里**不再有** `process` 块；
- 取而代之的是一个 `$mux`（`S=R`，`B=1'd0`，`A=D`，即「R 为真时取 0，否则取 D」）和一个 `$adff`（`CLK=C`、`ARST=R`、`ARST_VALUE=0`，`D` 接 `$mux` 的输出，`Q` 接 `Q`）；
- `proc_arst` 已经把 `posedge R` 识别为「高有效异步复位」，所以你看到的是 `ST1` 复位而非第二个时钟边沿。

**预期结果**：逻辑等价于 \(Q_{\text{next}} = R\,?\,0 : D\)，在 `C` 上升沿采样，`R` 为高时立即清零。`stat` 会显示约 1 个 `$adff`、1 个 `$mux`（具体单元计数待本地验证）。

> 拓展：把脚本里的 `proc` 换成 `proc -latches error`，再写一个**会推断出锁存器**的设计（如 `always @* if (en) q <= d;` 但不写 else），观察 yosys 是否报错——这正是 `-latches error` 的用途：在 ASIC 流程里把意外锁存器当成硬错误。

#### 4.3.5 小练习与答案

**练习 1**：对于 `always @(posedge clk)` 且 `if (!rst_n) q <= 0; else q <= d;`（低有效同步复位），`proc_dff` 会生成什么单元？

> 答案：注意是「同步复位」——复位发生在 `posedge clk` 的同一个边沿事件里（条件包在决策树中），`syncs` 里只有一条 `STp`（posedge clk），没有独立的复位事件。因此 `async_rules` 为空，生成的是普通 `$dff`（而非 `$adff`）；复位逻辑体现在驱动 `D` 端的 `$mux` 里（`S=!rst_n`）。

**练习 2**：`proc_dff` 里那句三元选单元类型 `clk.empty() ? $ff : arst ? $adff : $dff` 分别对应什么场景？

> 答案：`clk.empty()` 为真表示没有边沿时钟、只有全局时钟（`STg`），生成 `$ff`（无时钟端）；否则若 `arst` 非空（存在异步复位事件），生成 `$adff`；否则（普通同步触发器）生成 `$dff`。

## 5. 综合实践

把本讲三块知识串起来：从一段「既有组合又有多个寄存器」的 Verilog 出发，逐 pass 观察它如何被「编译」成门级网表。

设计 `traffic.v`（一个带异步复位、使能与模式选择的 4 位计数器）：

```verilog
module traffic(input clk, rst, en, mode, output reg [3:0] cnt);
    always @(posedge clk, posedge rst) begin
        if (rst)        cnt <= 4'd0;
        else if (!en)   cnt <= cnt;          // 保持：体现选择器的「默认值」
        else if (mode)  cnt <= cnt + 4'd3;   // 步进 3
        else            cnt <= cnt + 4'd1;   // 步进 1
    end
endmodule
```

任务：

1. 写脚本：`read_verilog` → `hierarchy -top traffic` → `write_rtlil s0.il`（仅读入）。
2. 接着 `proc_clean` → `write_rtlil s1.il`，观察决策树是否被规范化。
3. 接着 `proc_mux` → `write_rtlil s2.il`，找到为 `cnt` 生成的 `$mux`/`$pmux` 树，确认嵌套 `if-else` 被翻译成「`rst`/`en`/`mode` 作为选择信号」的多路器链。
4. 接着 `proc_dff` → `write_rtlil s3.il`，确认出现一个 4 位 `$adff`（时钟 `clk`、异步复位 `rst`、复位值 0），其 `D` 端接第 3 步的多路器输出。
5. 最后 `proc_clean` → 确认 `process` 块彻底消失；再 `stat` 统计单元，看是否大致符合「若干 `$mux`/`$pmux` + 算术单元 + 1 个 `$adff`」。

记录每个阶段 `cnt` 相关逻辑的形态变化，画一张「Process 决策树 → mux 树 → mux+adff」的演进图。这就是 `proc` 在 `synth` coarse 阶段为后续 `opt`/`techmap` 准备门级网表的全过程。

> 说明：各阶段确切单元数量与命名（`$procmux$…`、`$procdff$…`）由 `autoidx` 自动分配，会随运行变化，属正常现象，不必强求与本文一字不差。

## 6. 本讲小结

- `read_verilog` 把 `always` 块装进 `RTLIL::Process`（= 决策树 `root_case` + 敏感事件 `syncs`），**不**直接生成 `$dff`/`$mux`。
- `proc` 是一条「编排型」`Pass`，按 `proc_clean → rmdead → prune → init → arst → rom → mux → dlatch → dff → memwr → clean → opt_expr` 固定顺序串联子 pass。
- `proc_mux` 用 `SigSnippets` 切分被赋值位、逆序遍历决策树，把 `if`/`case` 翻译成 `$mux`/`$pmux`（并行分支并入 `$pmux`），比较逻辑用 `$eq`/`$reduce_or`。
- `proc_dff` 按 `SyncType` 分类事件，据「有无时钟/异步复位/复位值是否常数」分别生成 `$ff`/`$dff`/`$adff`/`$aldff`/`$dffsr`；选单元类型的核心是一句三元表达式。
- `proc_dlatch` 处理 `STa` 恒有效事件，通过在 mux 网络里**检测组合反馈环**识别锁存器，生成 `$dlatch`/`$adlatch`，纯组合部分则改写成普通连线。
- 经过整条 `proc` 链，Process 被 `proc_clean` 删除，设计变成纯门级 `$mux`/`$dff`/`$dlatch` 网表——这正是「process 消失、被 `$mux`/`$dff` 取代」的含义。

## 7. 下一步学习建议

- **下一步进入优化**：本讲产出的网表还很「毛糙」，下一讲 [u6-l3 opt：网表优化大流程](u6-l3-opt.md) 会讲 `opt_expr`/`opt_merge`/`opt_clean` 如何做常量传播、合并等价单元、删除死逻辑，把 `proc` 的产物进一步精简。
- **存储器如何处理**：本讲提到 `proc_memwr` 把存储器写动作归整，但真正的存储器推断与映射在 [u6-l4 memory：存储器推断与映射](u6-l4-memory.md)，可对照阅读以理解 `$mem` 的来历。
- **想自定义 pass 练手**：`proc_*` 是一组「读取 Process、改写 RTLIL」的典型 pass，结构清晰，适合作为 [u9-l1 编写你的第一个自定义 Pass](u9-l1-write-custom-pass.md) 的阅读范例。
- **延伸阅读源码**：若对异步复位识别感兴趣，可读 [`passes/proc/proc_arst.cc`](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc_arst.cc)，看它如何把 `posedge R + 赋常数` 改写成电平敏感的 `ST0`/`ST1`，为 `proc_dff` 的 `$adff` 推断铺路。
