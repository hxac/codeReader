# opt：网表优化大流程

## 1. 本讲目标

上一讲（u6-l2）我们用 `proc` 把 `always` 行为级代码翻译成了由 `$and`、`$mux`、`$dff` 等「门级单元」组成的网表。但这个翻译是「直译」的，会产生大量冗余：常数输入的门、完全相同的重复单元、没有任何下游使用的悬空线。本讲就讲解 Yosys 如何用一个名为 `opt` 的「编排型 pass」把整张网表收拾干净。

学完本讲你应该能够：

- 说清 `opt` 这一条命令内部按什么顺序调用哪些子 pass，以及它「跑到不动点」的循环机制。
- 理解 `opt_expr` 的常量折叠（`a & 0 → 0`、`a | 0 → a`、时钟反相器消除等）。
- 理解 `opt_merge` 如何用哈希识别「类型相同、输入相同」的单元并把它们合并。
- 理解 `opt_clean` 如何删除悬空线与无用单元、`opt_dff` 如何把触发器变换成更省的形式。
- 能动手对设计反复执行 `opt`，记录每一轮各类单元数的变化并解释原因。

## 2. 前置知识

本讲建立在前面几讲已经建立的认知之上，这里只做最简提示，不展开：

- **RTLIL 网表**（u2 / u3）：设计在内存里是 `RTLIL::Design → RTLIL::Module → Wire/Cell`，Cell 用 `type` + `connections_`（端口名→SigSpec）+ `parameters` 描述。`$and/$or/$mux/$dff` 等是内部单元（见 u3-l4）。
- **Pass 系统**（u4-l1）：每条命令是一个 `Pass`，纯虚入口是 `execute(args, design)`；`Pass::call(design, "命令字符串")` 可以在一条 pass 内部再调用另一条 pass（嵌套调用）。
- **scratchpad**（u2-l2 / u4-l4）：`design->scratchpad` 是一张字符串键值表，用于 pass 之间传数据。本讲会反复用到键 `opt.did_something`。
- **proc 之后的状态**（u6-l2）：`proc` 跑完后，`RTLIL::Process` 被删除，设计变成「纯门级网表 + 一堆冗余」，这正是 `opt` 的输入。

一个核心直觉：**优化是迭代的，不是一次到位的。** 删掉一个常数输入的门，可能让它的下游也变成常数输入；合并掉两个重复单元，可能又制造出新的重复。所以 `opt` 必须「反复跑，直到网表不再变化为止」。这个「不再变化」的状态，在程序里叫**不动点（fixpoint）**。

## 3. 本讲源码地图

本讲全部围绕 `passes/opt/` 目录展开。下表列出关键文件及其职责：

| 文件 | 对应命令 | 作用 |
|------|----------|------|
| `passes/opt/opt.cc` | `opt` | 编排型 pass，按固定顺序反复调用下面这一串子 pass |
| `passes/opt/opt_expr.cc` | `opt_expr` | 常量折叠、表达式重写、时钟反相器消除 |
| `passes/opt/opt_merge.cc` | `opt_merge` | 识别并合并「类型相同、输入相同」的重复单元 |
| `passes/opt/opt_dff.cc` | `opt_dff` | 触发器优化：合并使能/同步复位 mux、删除无用控制端 |
| `passes/opt/opt_clean/opt_clean.cc` | `opt_clean` / `clean` | 删除悬空线与无用单元 |
| `passes/opt/opt_muxtree.cc` | `opt_muxtree` | 消除多路器树中的「死分支」 |
| `passes/opt/opt_reduce.cc` | `opt_reduce` | 简化大型 MUX 与 AND/OR 树 |

`opt` 编排里还会出现 `opt_hier`（`-hier` 时）和 `opt_share`（`-full` 时），本讲以四个最小模块为主线，对这两个仅作交代。

---

## 4. 核心概念与源码讲解

### 4.1 opt 编排：把一串子 pass 跑到不动点

#### 4.1.1 概念说明

`opt` 自己**不做任何算法**。它只做两件事：

1. **决定顺序**：把若干已有的 `opt_*` 子 pass 按一个「有用的顺序」串起来。
2. **决定何时停**：用 `while` 循环反复跑这一串，直到网表这一轮没有任何变化为止（即不动点）。

为什么要编排而不让用户自己手写脚本？因为各子 pass 之间有**协同效应**（synergy）：`opt_expr` 折叠常数后会产生新的重复单元交给 `opt_merge`；`opt_merge` 合并后又可能暴露新的常数输入交回 `opt_expr`；`opt_clean` 删掉死代码后又能让下一轮看得更清楚。让用户自己排顺序很容易排错，所以 Yosys 把这套「黄金顺序」固化进 `opt`。

#### 4.1.2 核心流程

`opt` 有两种模式，由 `-fast` 开关切换。默认（非 fast）模式的执行顺序是：

```
opt_expr [-选项...]
opt_merge -nomux [-选项...]          # 第一趟：先不合 mux

do                                    # 不动点循环开始
    opt_muxtree
    opt_reduce [-选项...]
    opt_merge [-选项...]              # 第二趟起：允许合 mux
    opt_share          (-full 时才跑)
    opt_dff [-选项...]  (-noff 时跳过)
    opt_hier           (-hier 时才跑)
    opt_clean [-选项...]
    opt_expr [-选项...]               # 循环里再跑一次 expr
while <changed design>                # 没变化就退出
```

判断「这一轮有没有变化」靠的是 scratchpad 里的 `opt.did_something` 标志：每个子 pass 只要真的改动了设计，就把这个布尔键置为 `true`。循环开头先 `unset`（清成未设置），跑完一轮再读取，为 `false` 就 `break`。

`-fast` 模式更轻量：循环体里只跑 `opt_expr → opt_merge → opt_dff → opt_clean`，并且只在 `opt_dff` 改动了设计时才继续重跑——因为它专门服务于「删寄存器后会暴露更多机会」的场景。

无论哪种模式，循环结束后都调用 `design->optimize()`（做索引整理）和 `design->check()`（一致性自检）。

#### 4.1.3 源码精读

`opt` 这条 pass 的 `help()` 直接把执行顺序写给了用户，这是「文档与实现同源」的好例子：

[passes/opt/opt.cc:L40-L52](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt.cc#L40-L52) — help 文本里列出的默认执行顺序，与下面 `execute` 真正调用的序列一一对应。

真正干活的 `execute` 先把命令行选项「分发」到各子 pass 的参数串里（比如 `-keepdc` 要同时透传给 `opt_expr`、`opt_dff`、`opt_merge`）：

[passes/opt/opt.cc:L85-L152](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt.cc#L85-L152) — 解析 `opt` 自己的选项，把它们累加到 `opt_expr_args` / `opt_merge_args` / `opt_dff_args` 等字符串里，后面拼到子命令后透传。

默认模式的核心不动点循环在：

[passes/opt/opt.cc:L172-L193](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt.cc#L172-L193) — 这是整条 pass 的心脏。注意三个细节：
- 第 174 行先跑一次 `opt_expr` 与带 `-nomux` 的 `opt_merge` 做冷启动；
- 第 177 行 `design->scratchpad_unset("opt.did_something")` 在每轮开头清标志；
- 第 178–188 行依次调用六个子 pass，最后第 189 行 `scratchpad_get_bool("opt.did_something")` 判断是否还要再来一轮。

子 pass 是怎么把自己的成果上报的？以 `opt_merge` 为例：

[passes/opt/opt_merge.cc:L569-L570](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt_merge.cc#L569-L570) — `opt_merge` 只要真的删了单元（`total_count > 0`），就把 `opt.did_something` 置 `true`，供 `opt` 循环判断。

循环收尾时的整理与自检：

[passes/opt/opt.cc:L195-L196](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt.cc#L195-L196) — `design->optimize()` 重建内部索引，`design->check()` 做一致性校验，保证交给下一条 pass 的是合法 RTLIL。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `opt` 的不动点循环确实在多轮后才收敛，并感受「协同效应」。

**操作步骤**（以下为示例脚本，假设你已按 u1-l2 构建出可执行的 `yosys`）：

```
# 文件：opt_demo.ys （示例脚本）
read_verilog <<EOT
module opt_demo(input [3:0] a, input [3:0] b, input sel, output [3:0] y);
    wire [3:0] c0 = a & 4'h0;       // opt_expr 会折叠成常数 0
    wire [3:0] c1 = b | 4'h0;       // opt_expr 会简化成缓冲
    wire [3:0] d1 = a ^ b;
    wire [3:0] d2 = a ^ b;          // opt_merge 会与 d1 合并
    assign y = sel ? (d1 & d2 & ~c0) : c1;
endmodule
EOT
hierarchy -top opt_demo
proc
stat                                 # 记录基线单元数
opt
stat                                 # 记录 opt 后单元数
```

在 `yosys` 里执行 `script opt_demo.ys`，或直接 `yosys -s opt_demo.ys`。

**需要观察的现象**：

1. `proc` 之后 `stat` 会报告若干 `$and`/`$or`/`$xor`/`$mux` 单元，其中包括「`a & 0`」「两个相同的 `a ^ b`」等冗余。
2. `opt` 运行时日志里会出现多段 `Executing OPT_EXPR pass`、`Executing OPT_MERGE pass`，并在某段出现 `Finished ... (There is nothing left to do.)`——这正是不动点收敛的信号。
3. 最终 `stat` 的单元数应明显少于基线。

**预期结果**：`a & 4'h0` 折叠为常数 `0`，两个 `a ^ b` 合并为一个，`b | 4'h0` 被简化为直连。具体减少多少个单元**待本地验证**（取决于 `proc` 产出的中间命名与位宽）。

> 提示：想看到「逐轮」变化，可在脚本里把 `opt` 拆成手动循环——多次重复 `opt_expr; opt_merge; opt_clean; stat`，直到 `stat` 数字不再变化。注意手动拆开会丢失 `opt` 的协同顺序，结果可能略有差异。

#### 4.1.5 小练习与答案

**练习 1**：`opt` 默认模式第一趟 `opt_merge` 带 `-nomux`，循环里的 `opt_merge` 不带。为什么第一趟不合 mux？

**答案**：第一趟是「冷启动」，目的是先把纯组合逻辑里的常数与重复单元收拾掉、降低规模；`$mux`/`$pmux` 的等价判定更复杂（选择端与数据端要按位配对排序），留到循环里再做既能减少开销，也配合 `opt_muxtree`/`opt_reduce` 先把 mux 树简化后再合并。源码上 `-nomux` 体现为 [opt_merge.cc:L552-L555](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt_merge.cc#L552-L555) 把 `$mux`/`$pmux` 从可合并类型表里删除。

**练习 2**：如果某个子 pass 忘了在改动设计后置 `opt.did_something`，`opt` 的循环会出什么问题？

**答案**：循环靠这个标志判断是否继续。若子 pass 改了网表却没置标志，`opt` 可能在仍有优化空间时提前退出（少收敛一轮），结果不是最优但仍然合法（因为 `design->check()` 会过）。反之标志被错误地常置 `true`，则循环会多跑一轮空转直到下一轮真的没变化才退出，最多浪费一次迭代。

---

### 4.2 opt_expr：常量折叠与表达式重写

#### 4.2.1 概念说明

`opt_expr` 是 `opt` 里「最先动刀」的子 pass，目标是**把输入里含常数的单元化简掉**。典型例子：

| 表达式 | 折叠结果 | 说明 |
|--------|----------|------|
| `a & 0` | `0` | 「与 0 恒为 0」（const_and） |
| `a \| 1` | `1` | 「或 1 恒为 1」（const_or） |
| `a & 1` | `a` | 退化为缓冲（and_or_buffer） |
| `a ^ 0` | `a` | 异或 0 等于自身（xor_buffer） |
| `a ^ a` | `0` | 自反（const_xor，需 `-keepdc` 关闭） |
| `$mux(S, A=1, B=0)` | `~S` | 选择端驱动的反相器 |

它还做两件额外的事：

- **`-undriven`**：把没有任何驱动的悬空网驱动成常数 `x`（`replace_undriven`）。
- **时钟反相器消除**：如果某个触发器的时钟是通过一个反相器接进来的，`opt_expr` 可以「吃掉」反相器、改用相反极性的触发器类型（`$_DFF_P_` ↔ `$_DFF_N_`），从而少一个非门。`-noclkinv` 可关闭这项。

#### 4.2.2 核心流程

`opt_expr::execute` 对每个模块按如下顺序处理：

```
若 -undriven：replace_undriven(module)          # 悬空网 → x

repeat                                          # 外层不动点
    repeat                                      # 内层不动点（consume_x=false）
        replace_const_cells(..., consume_x=false, ...)
        直到本轮没改动
    若未 -keepdc：
        replace_const_cells(..., consume_x=true, ...)   # 允许吃掉 x 位
    直到本轮没改动

replace_const_connections(module)               # 把常量连接传播开
```

关键是 `replace_const_cells` 内部：它先把网表里的单元按数据依赖做**拓扑排序**（因为折叠一个门会让它的输出变成常数，从而解锁下游），然后按拓扑序逐个单元检查并重写。`consume_x` 控制是否把含 `x`（不定值）的输入也参与折叠——这会改变电路对 don't-care 位的语义，所以 `-keepdc` 时禁止。

#### 4.2.3 源码精读

「替换为常数驱动」的公共动作封装在 `replace_cell`，它把单元的输出 `Y` 连到常数值、再删除该单元：

[passes/opt/opt_expr.cc:L120-L134](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt_expr.cc#L120-L134) — 注意它同时更新了 `assign_map`（局部 SigMap）和模块连接，并置 `did_something=true`，这样同一次扫描里下游单元立刻能看到新常数。

`replace_const_cells` 先收集「反相器映射」`invert_map`（哪根线等于另一根线的非），并（若非 `-noclkinv`）调用一系列 `handle_polarity_inv` / `handle_clkpol_celltype_swap` 做时钟极性优化：

[passes/opt/opt_expr.cc:L400-L409](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt_expr.cc#L400-L409) — 建立 `invert_map`：单个 `$not` 或「`$mux` 且 `A=1,B=0`」这样的单元会被记录成「它的输出 = 某输入的非」。

[passes/opt/opt_expr.cc:L411-L415](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt_expr.cc#L411-L415) — 时钟反相器消除的入口：对 `$dff/$adff/$sdff…` 这一大族时序单元，调用 `handle_polarity_inv` 处理 `CLK` 极性。

随后把所有「可静态求值」的单元拓扑排序：

[passes/opt/opt_expr.cc:L511-L517](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt_expr.cc#L511-L517) — 用 `cells.sort()` 做拓扑排序；若存在组合环排不出来，则打日志提示「可能耗时更长」但继续工作。

逐个单元按类型做常量折叠，以与/或为例：

[passes/opt/opt_expr.cc:L537-L578](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt_expr.cc#L537-L578) — 检测到「输入里有 0」就把 `$and` 折成 `0`（const_and）、有 1 就把 `$or` 折成 `1`（const_or）、若只有一个非常量输入则退化为缓冲（and_or_buffer）。这里也用 `invert_map` 发现 `a & ~a` 这类自反情况。

`opt_expr` 主入口对每个模块的两层不动点循环：

[passes/opt/opt_expr.cc:L2357-L2368](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt_expr.cc#L2357-L2368) — 内层 `do…while` 跑 `consume_x=false` 到不动点；外层再跑一次 `consume_x=true`（仅当未 `-keepdc`），直到整体不动。每轮有改动即写 `opt.did_something`。

#### 4.2.4 代码实践

**实践目标**：隔离观察 `opt_expr` 单独的折叠效果。

**操作步骤**：

```
read_verilog opt_demo.v        # 用 4.1.4 里那个 opt_demo
hierarchy -top opt_demo
proc
stat                            # 基线 A
opt_expr -full                  # 单独跑 opt_expr，-full = -mux_undef -mux_bool -undriven -fine
stat                            # 折叠后 B
```

**需要观察的现象**：

- `c0 = a & 4'h0` 对应的 `$and` 应被删掉，`c0` 变成常数 `0`。
- `c1 = b | 4'h0` 的 `$or` 退化为对 `b` 的直连。
- 日志里会出现形如 `Replacing $and cell '...' (const_and) ... with constant driver` 的行。

**预期结果**：`$and`/`$or` 中输入含常数者被替换为常数驱动或缓冲。**待本地验证**：`-full` 触发的 `mux_undef` 是否会进一步简化最后的 `$mux`（取决于 `proc` 给 mux 端口接了什么）。

#### 4.2.5 小练习与答案

**练习 1**：`a + 0` 在 `-keepdc` 开启时不会被替换成 `a`，为什么？

**答案**：因为 `a` 里若含 `x` 位，按四值逻辑 `x + 0` 的结果是全 `x`（一位 x 污染整个和），而直接替换成 `a` 则只保留原 x 位、其余位不变——两者对 don't-care 的语义不同。`-keepdc` 承诺不改变 don't-care 行为，故禁止此类折叠。`opt_expr` 的 help 文本（[opt_expr.cc:L2289-L2293](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt_expr.cc#L2289-L2293)）正是用这个例子解释 `-keepdc`。

**练习 2**：`replace_const_cells` 为什么要先对单元做拓扑排序？

**答案**：折叠一个门会让它的输出 `Y` 变成常数（经 `assign_map.add(Y, out_val)`），下游门就能在本轮扫描中立刻「看到」这个新常数并继续折叠。按拓扑序（从输入端往输出端）扫描，能在一轮里传播尽可能远的常数链，减少外层不动点轮数。

---

### 4.3 opt_merge：相同单元合并

#### 4.3.1 概念说明

`opt_merge` 解决「**重复劳动**」：如果两个单元类型相同、参数相同、对应的输入信号也相同，那它们的输出必然永远相等，留一个、删另一个、把删掉那个的输出接到保留者的输出上即可。这是经典的**公共子表达式消去**（CSE）在网表层面的对应。

例子：`d1 = a ^ b` 和 `d2 = a ^ b` 会被合并成一个 `$xor`，`d2` 的输出接到 `d1` 的输出。

难点在于「输入相同」的判定——同一信号在网表里有多种等价写法（`assign` 改名、切片、拼接），直接比 `SigSpec` 会误判。`opt_merge` 的办法是先用 `SigMap`（见 u3-l2）把所有信号**归一化**到规范代表位，再比较。

#### 4.3.2 核心流程

`opt_merge` 是一个**基于哈希的等价类划分**算法，并且支持多线程。整体步骤：

```
对每个被选中的 module：
    建 SigMap（归一化）+ FfInitVals（处理 init 属性）
    repeat
        1) 给每个相关单元算一个哈希（类型 + 归一化输入 + 参数）
        2) 把哈希相同的单元分到同一桶
        3) 桶内两两精确比较（compare_cell_parameters_and_connections）
        4) 真相同的，保留一个、删其余、重接输出、更新 SigMap
    until 本轮没有重复
```

对**交换律**运算（`$and/$or/$xor/$add/$mul` 等），哈希与比较都把两个输入当作无序集合处理（用 `commutative_hash`），这样 `a&b` 与 `b&a` 也能合并。对 `$pmux`，则按选择位排序后再比较。

#### 4.3.3 源码精读

哈希计算对交换律运算做「无序化」处理：

[passes/opt/opt_merge.cc:L144-L179](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt_merge.cc#L144-L179) — `hash_cell_inputs`：对 `$and/$or/$add/$mul…` 用 `hashlib::commutative_hash` 让 `A`、`B` 顺序不影响哈希；对 `$reduce_*` 先排序输入位；对 `$pmux` 用 `hash_pmux_in` 把 `(选择位, 数据)` 配对后交换律哈希。其余单元按「端口名 + 归一化信号」逐项哈希。

精确比较（桶内哈希冲突时再核实）：

[passes/opt/opt_merge.cc:L198-L257](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt_merge.cc#L198-L257) — `compare_cell_parameters_and_connections`：先比类型与参数，再比每个端口（输出端口置空比较、输入端口用 `assign_map` 归一化后比较），并对交换律运算规范化 `A/B` 顺序，对 `$pmux` 排序。注意对触发器 `Q` 输出特殊处理——用 `init` 属性值参与比较（[L217-L222](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt_merge.cc#L217-L222)），这样两个初值不同的寄存器不会被错误合并。

发现重复后真正执行合并与重接：

[passes/opt/opt_merge.cc:L456-L479](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt_merge.cc#L456-L479) — 对每个待删单元，遍历它的输出端口，用 `module->connect(SigSig(被删输出, 保留输出))` 把被删输出连到保留输出上，更新 `assign_map` 与 `initvals`，最后 `module->remove(remove_cell)`。

`opt_merge` 选项与可合并类型表：

[passes/opt/opt_merge.cc:L547-L562](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt_merge.cc#L547-L562) — `CellTypes ct` 先 `setup_internals/stdcells` 登记可合并类型；`-nomux` 时把 `$mux/$pmux` 移出；`$tribuf/$anyconst` 等始终不合并（它们有特殊语义）。`-share_all` 才会对非内置（如厂商黑盒）单元也尝试合并。

> 并发细节（选读）：`opt_merge` 用「按哈希分桶、桶再分 shard」的方式让多线程无锁地建哈希表，源码顶部有完整算法注释 [opt_merge.cc:L49-L61](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt_merge.cc#L49-L61)；单元数不足 2000 时不启线程（[L370-L380](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt_merge.cc#L370-L380)）。

#### 4.3.4 代码实践

**实践目标**：隔离观察 `opt_merge` 合并重复单元的效果。

**操作步骤**：

```
read_verilog opt_demo.v
hierarchy -top opt_demo
proc
opt_expr -full                  # 先把常数收拾掉，避免干扰
opt_clean                       # 清掉因折叠产生的死单元
stat                            # 基线
opt_merge                       # 单独跑合并
stat                            # 合并后
```

**需要观察的现象**：设计里两个 `a ^ b`（`d1`、`d2`）对应的两个 `$xor` 应合并为一个；日志（用 `-v` 或开 debug）会出现 `Cell '...' is identical to cell '...'` 与 `Removing $xor cell ...`。

**预期结果**：`$xor` 数量减少，`d2` 的输出被接到 `d1` 的输出。具体数量**待本地验证**。

> 进阶观察：交换律验证。把设计改成 `d1 = a & b; d2 = b & a;`，`opt_merge` 仍应合并它们——因为 [hash_cell_inputs](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt_merge.cc#L144-L179) 对 `$and` 用了 `commutative_hash`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `opt_merge` 比较输入信号前一定要过一遍 `SigMap`？

**答案**：因为网表里同一根信号常被 `assign` 改名、切片或拼接，例如 `wire n = a;` 之后 `n` 与 `a` 是同一信号但 `SigSpec` 字面不同。直接比字面会漏掉合并机会。`SigMap` 用并查集把所有连通位归并到唯一的「规范代表位」（见 u3-l2），归一化后相等的才是真同一信号。

**练习 2**：两个 `$dff`，输入 `D`、时钟、复位都相同，但 `init` 属性（初值）不同，能合并吗？

**答案**：不能。`opt_merge` 在比较触发器时把 `Q` 输出替换成 `init` 属性的值参与比较（[opt_merge.cc:L217-L222](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt_merge.cc#L217-L222)），初值不同则视为不等价。这是为了保住上电初值语义——合并会改变仿真与形式验证里的初始状态。

---

### 4.4 opt_clean 与 opt_dff：死代码清除与时序元件优化

#### 4.4.1 概念说明

这两个子 pass 分工明确：

- **`opt_clean`（别名 `clean`）**：删除**没人用的单元和线**。前面的 pass（`opt_expr` 折叠、`opt_merge` 合并）常常「删了单元却留下悬空的线」或「改了连接却留下旧单元」，`opt_clean` 负责扫尾。它还做 `design->optimize()` 与 `design->check()`，是 `opt` 循环里「保持网表整洁」的清道夫。
- **`opt_dff`**：专门优化**触发器**。它做三类事：
  1. 把驱动 `D` 端口的 `$mux`（时钟使能、同步复位产生的选择器）**吸收进触发器**，变成带使能的 `$dffe` 或带同步复位的 `$sdff`，省掉一个 mux。
  2. 删除触发器上**没用到的控制端**（如永不触发的复位）。
  3. 若一个触发器的输出其实是常数（或 `D == Q` 永不自更新），把它替换成常数驱动、整个删掉。

#### 4.4.2 核心流程

`opt_clean` 对每个模块做经典的「可达性分析」：

```
remove_temporary_cells(module)          # 删临时单元
rmunused_module_cells(module)           # 删输出不被任何下游使用的单元
while rmunused_module_signals(module):  # 反复删悬空线
    ;                                   # （删线可能让更多单元变无用，故循环）
rmunused_module_init(module)            # 清理 init 属性（-purge 时更激进）
```

`opt_dff` 对每个模块遍历所有 FF 单元，逐个尝试一组优化，只要命中一个就重写并继续下一个：

```
while 还有 dff_cells:
    取一个 ff
    若宽度为 0：直接删
    尝试 optimize_sr / optimize_aload / optimize_arst   # 异步控制端
    尝试 optimize_srst / optimize_ce / optimize_const_clk  # 同步控制端/常数时钟
    若 D == Q：optimize_d_equals_q                      # 永不更新 → 常数
    尝试 try_merge_srst（D 端 mux 是同步复位 → $sdff）
    尝试 try_merge_ce  （D 端 mux 是时钟使能 → $dffe）
    若有改动：ff.emit() 重写单元
```

`-nodffe` / `-nosdff` 分别禁用「吸收成使能 FF」「吸收成同步复位 FF」这两类变换；`-sat` 额外调用 SAT 求解器来证明某些 FF 输出恒为常数（与 `-keepdc` 互斥）。

#### 4.4.3 源码精读

`opt_clean` 的核心入口 `rmunused_module`：

[passes/opt/opt_clean/opt_clean.cc:L28-L43](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt_clean/opt_clean.cc#L28-L43) — 依次调用删临时单元、删无用单元、`while` 反复删悬空信号、（`rminit` 时）清理 init 属性。`-purge` 选项控制是否连「有公有名但无驱动」的内部网也删（[opt_clean.cc:L66-L68](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt_clean.cc#L66-L68) help 说明）。

`opt_clean` 还顺带做整设计的整理与自检，并触发垃圾回收：

[passes/opt/opt_clean/opt_clean.cc:L92-L104](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt_clean.cc#L92-L104) — `design->optimize()` + `design->check()` + `request_garbage_collection()`。注意它只处理「整模块被选中且无 process」的模块（`has_processes_warn`，[L89-L91](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt_clean.cc#L89-L91)）——这正是 `proc` 之后才适合跑 `opt` 的原因。

`opt_dff` 的 `OptDffWorker::run()` 是它所有优化的总调度：

[passes/opt/opt_dff.cc:L807-L886](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt_dff.cc#L807-L886) — 每弹出一个 FF，依次尝试异步控制端优化（[L825-L838](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt_dff.cc#L825-L838)）、同步控制端优化（[L841-L848](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt_dff.cc#L841-L848)）、`D==Q` 退化（[L851-L852](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt_dff.cc#L851-L852)），最后是「吸收 D 端 mux」的两种变换：

[passes/opt/opt_dff.cc:L862-L877](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt_dff.cc#L862-L877) — `can_merge_srst` 决定能否吸收成 `$sdff`（受 `-nosdff` 控制），`can_merge_ce` 决定能否吸收成 `$dffe`（受 `-nodffe` 控制）。命中即 `ff.emit()` 重写单元类型。

`opt_dff` 把成果上报给 `opt` 循环：

[passes/opt/opt_dff.cc:L1468-L1480](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt_dff.cc#L1468-L1480) — 任一模块的 `run()`/`run_constbits()`/`run_eqbits()` 返回 `true` 即置 `opt.did_something`。注意 [L1465-L1466](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt_dff.cc#L1465-L1466) 明确拒绝 `-sat` 与 `-keepdc` 同时使用（SAT 用二值逻辑会消解 don't-care）。

#### 4.4.4 代码实践

**实践目标**：观察 `opt_dff` 把「D 端带使能 mux 的触发器」吸收成 `$dffe`，以及 `opt_clean` 清扫死单元。

**操作步骤**（示例代码）：

```verilog
// 示例代码：cen 是时钟使能
module dff_demo(input clk, input cen, input [3:0] d, output reg [3:0] q);
    always @(posedge clk)
        if (cen) q <= d;
endmodule
```

```
read_verilog dff_demo.v
hierarchy -top dff_demo
proc
stat                            # 看 proc 直译产物：$dff + $mux（实现 cen 选择）
opt_dff
stat                            # 应出现 $dffe，$mux 消失
opt_clean
stat                            # 清扫后最终形态
```

**需要观察的现象**：

- `proc` 之后，`cen` 的 `if` 被翻成一个 `$mux`（选择 `q` 还是 `d`）驱动 `$dff` 的 `D`。
- `opt_dff` 之后，`$mux` 被「吸收」进触发器，`stat` 报告里 `$dff` 变成 `$dffe`（带使能），`$mux` 数减少。
- `opt_clean` 之后，任何因上述改写而悬空的中间线被删除。

**预期结果**：`$mux` 数下降、出现 `$dffe`。**待本地验证**：`stat` 的确切数字与单元命名。

> 反例验证：给 `opt_dff` 加 `-nodffe`，再跑一遍，`$mux` 应当**不被吸收**——这印证了 [opt_dff.cc:L871](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt_dff.cc#L871) 的 `can_merge_ce` 受 `-nodffe` 控制。

#### 4.4.5 小练习与答案

**练习 1**：`opt_clean` 为什么删完信号后要用 `while` 反复删，而不是一遍过？

**答案**：因为删掉一根悬空信号后，原本驱动它的单元可能失去所有下游使用者，从而变成新的「无用单元」；删除这些单元又可能让更多信号变悬空。这是连锁反应，必须迭代到不再有变化（一个局部不动点），单遍扫描会漏删。

**练习 2**：`opt_dff` 里 `D == Q` 的触发器会被怎样处理？

**答案**：若一个有时钟的触发器其 `D` 端连的就是自己的 `Q`，那它每个时钟沿都把当前值写回自己——值永不改变，等价于一个常数驱动。`optimize_d_equals_q`（[opt_dff.cc:L851-L852](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt_dff.cc#L851-L852)）会把它替换成其初值的常数驱动并删除该 FF。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成一次「带记录的端到端优化」。

**任务**：对一个含常数、重复逻辑、使能寄存器的小设计，手动拆解 `opt` 的不动点循环，逐轮记录 `opt_expr`/`opt_merge`/`opt_dff`/`opt_clean` 各自让单元数变化了多少，直到收敛。

**示例设计**（合在一个文件里）：

```verilog
// 示例代码：综合实践用
module top(input clk, input cen, input [3:0] a, input [3:0] b,
           output reg [3:0] q, output [3:0] z);
    wire [3:0] k0 = a & 4'h0;        // opt_expr: 与 0
    wire [3:0] x1 = a ^ b;
    wire [3:0] x2 = a ^ b;           // opt_merge: 与 x1 重复
    always @(posedge clk)
        if (cen) q <= x1 | x2;       // opt_dff: cen 使能可吸收
    assign z = q & ~k0;              // opt_expr: 折叠后简化
endmodule
```

**操作步骤**：

```
read_verilog top.v
hierarchy -top top
proc
stat                                 # 第 0 轮：基线
setvar iter 0

# 手动模拟 opt 的不动点循环（示例脚本，逐轮打印）
opt_expr -full;  log opt_expr done;  stat
opt_merge;       log opt_merge done; stat
opt_dff;         log opt_dff done;   stat
opt_clean;       log opt_clean done; stat
opt_expr -full;  stat                # 循环里的第二趟 expr
# 重复上面 5 行，直到 stat 数字不再变化
```

**需要记录与解释的内容**：

1. 画一张表：每一轮里 `$and/$or/$xor/$mux/$dff` 的数量变化，标注是哪个子 pass 造成的（如「第 1 轮 opt_expr 把 `k0` 折叠为常数，`$and -1`」）。
2. 指出一次「协同效应」：例如 `opt_merge` 合并 `x1/x2` 后，`opt_expr` 才能进一步简化 `x1 | x2` 为 `x1`——单独跑某一个子 pass 看不到这个效果。
3. 确认收敛：当连续两轮 `stat` 完全相同时即达到不动点，对比 `opt`（整条命令）跑出来的最终 `stat`，两者单元数应一致（或非常接近）。

**预期结果**：设计应收敛到「无 `$mux`、`$dff` 变 `$dffe`、重复 `$xor` 合并、含常数输入的门被折叠」的形态。各轮确切数字**待本地验证**；重点是能讲清「哪一步砍了哪些单元、为什么」。

## 6. 本讲小结

- `opt` 是一条**编排型 pass**：自身不做算法，只按「黄金顺序」反复调用 `opt_expr → opt_muxtree → opt_reduce → opt_merge → opt_share → opt_dff → opt_hier → opt_clean → opt_expr`，直到 scratchpad 标志 `opt.did_something` 为 `false`（不动点）。
- 不动点循环是必须的：各子 pass 之间存在协同效应（折叠暴露重复、合并暴露常数、删寄存器暴露新机会），单趟跑不够。
- `opt_expr` 做**常量折叠与表达式重写**：`a&0→0`、`a|1→1`、`a^0→a`、时钟反相器消除；`-keepdc` 禁止任何会改变 don't-care 语义的折叠。
- `opt_merge` 做**相同单元合并**（网表版 CSE）：用 `SigMap` 归一化信号后，按「类型+输入+参数」哈希分桶、桶内精确比较；对交换律运算无序化处理，对触发器用 `init` 属性参与比较以防误合并。
- `opt_clean` 是清道夫：反复删除悬空线与无用单元（连锁删除到不动点），并做 `optimize`/`check`；`opt_dff` 优化时序元件，最常见的是把 D 端使能/同步复位 mux 吸收成 `$dffe`/`$sdff`。
- 所有子 pass 都通过 `design->scratchpad_set_bool("opt.did_something", true)` 上报「我改过设计了」，这正是 `opt` 循环判断收敛的唯一信号。

## 7. 下一步学习建议

- **继续核心综合流程**：下一讲 u6-l4 进入 `memory`——Yosys 如何把 `reg [..] mem [..]` 数组式存储收集、共享、分块，最终映射成地址译码 `$mux` 与触发器阵列。建议先回顾本讲的 `opt_clean`，因为 `memory_map` 之后通常要跑一轮 `opt` 收拾碎片。
- **更深的窥孔优化**：本讲只提了一句 `opt_share`（`-full` 时启用）和 `peepopt`。u10-l3 会专门讲 `pmgen` 模式匹配生成器与 `peepopt` 如何做子图替换式窥孔优化，是 `opt` 之外的另一类优化手段。
- **动手扩展**：学完 u9-l1「编写自定义 pass」后，可以尝试写一个统计每种子 pass 贡献的小 pass，把本讲综合实践里手工记录的过程自动化。
- **建议阅读的源码**：`passes/opt/opt_reduce.cc`（简化大型 MUX/AND-OR 树）与 `passes/opt/opt_muxtree.cc`（消除 mux 死分支），它们在 `opt` 循环里紧跟 `opt_expr`，补全了本讲未细讲的两块拼图。
