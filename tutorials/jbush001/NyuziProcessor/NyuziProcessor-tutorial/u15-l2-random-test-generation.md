# 随机测试生成与约束

## 1. 本讲目标

本讲是「验证体系与测试方法」单元的第二讲。上一篇（u15-l1）讲了测试**框架**怎么把一个测试编译、运行、校验；上一篇的上游（u8-l3）讲了**协同仿真**怎么让周期精确的硬件模型与功能级模拟器逐条指令锁步比对。本讲则回答一个更上游的问题：

> 当人工写测试已经写不出新的 bug 时，**这些海量比对的程序从哪里来？**

答案是**随机测试**——让机器自动生成海量程序去「冲撞」硬件。但朴素的随机生成覆盖率很差，Nyuzi 采用的是**约束随机（constrained random）**生成：给随机数戴上精心设计的镣铐，让它专门往最容易暴露 bug 的角落钻。

学完本讲你应当掌握：

1. 理解为什么「无偏随机」覆盖率差，以及约束随机的**覆盖目标**与**约束策略**之间的关系。
2. 掌握三类核心约束的设计：**前向短分支**、**固定内存指针寄存器**、**小寄存器池**，并能解释它们如何分别提升「控制流覆盖率」「缓存命中率覆盖」和「RAW 数据冒险覆盖」。
3. 了解 **csmith** 整程序随机测试的作用，以及它在「跨字宽（32 位 vs 64 位）」上的已知限制。

## 2. 前置知识

在进入正题前，先用三段话补齐本讲需要的基础概念。

**约束随机（constrained random）。** 这是芯片验证领域的经典技术，README 里直接引用了 DEC Alpha 21264、HP PA 8000、PicoJava II 三篇工业级验证文档（见 [tests/cosimulation/README.md:7-12](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/README.md#L7-L12)）。其核心思想是：不直接均匀采样整个指令空间（那样大量采样会落在「无趣」的区域），而是加一组**约束**把采样导向「边界条件密集、冒险密集、缓存交互密集」的区域，再用海量种子反复跑，靠数量换覆盖。一句话：**随机是为了数量，约束是为了质量。**

**指令副作用（side effect）。** 这是 u8-l3 协同仿真的比对单位。一条指令的「副作用」指它对架构可见状态的改变：写一个标量/向量寄存器，或写一段内存。分支、`move` 到被掩码掉的通道等没有副作用的指令，在协同仿真里**不产生 trace 事件**。这个概念在本讲会反复出现，因为它决定了「为什么浮点和 store buffer 难以验证」。

**RAW 数据冒险。** 当一条指令的**源**寄存器恰是上一条指令的**目的**寄存器时（Read After Write），后一条必须等前一条写回才能拿到新值。硬件用记分牌检测并推迟发射（见 u4-l3）。RAW 冒险是流水线验证的「重灾区」——如果随机测试很少触发 RAW，那么记分牌里隐藏的 bug 就永远暴露不出来。

## 3. 本讲源码地图

本讲围绕两组「随机源」展开，一组在硬件验证层，一组在编译器验证层：

| 文件 | 作用 |
| --- | --- |
| [tests/cosimulation/generate_random.py](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py) | **本讲主角**。生成随机汇编程序，注入到协同仿真（Verilator RTL ↔ C 模拟器）里压测硬件。 |
| [tests/cosimulation/README.md](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/README.md) | 解释随机生成的「指令选择策略」与「已知限制」，是本讲约束设计的权威说明。 |
| [tests/cosimulation/runtest.py](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/runtest.py) | 驱动协同仿真：拉起 Verilator 与模拟器、做内存镜像比对，揭示随机测试的「最后兜底校验」。 |
| [tests/csmith/runtest.py](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/csmith/runtest.py) | 用 csmith 生成随机 C 程序，跨「宿主 gcc」与「Nyuzi 模拟器」比对 checksum，验证编译器。 |
| [tests/csmith/README.md](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/csmith/README.md) | 点明 csmith 在 64 位宿主上的「假阴性」限制。 |
| [tests/asm_macros.h](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/asm_macros.h) | 随机汇编程序 `#include` 的宏文件，提供 `start_all_threads` / `halt_current_thread` 等。 |

两条线最大的区别先记在脑子里：

- `generate_random.py` 比对的是 **RTL vs 模拟器**（验证**硬件**实现）。
- `csmith` 比对的是 **宿主 gcc vs Nyuzi 模拟器**（验证**编译器 + 模拟器**的语义正确性）。

两者都以模拟器为参照金标准，但压测的是不同抽象层。

## 4. 核心概念与源码讲解

### 4.1 约束随机：为什么「无偏随机」不够好

#### 4.1.1 概念说明

如果让生成器在全部指令、全部 32 个寄存器上做**均匀**随机采样，会发生什么？README 给出了两个反直觉的结论（[tests/cosimulation/README.md:91-101](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/README.md#L91-L101)）：

1. **分支会冲刷流水线。** 一旦分支 taken，取指级到执行级之间的指令全部作废。如果分支太密集，大部分指令在被比对前就被冲掉了，等于没测——「它会掩盖指令依赖类的问题」。
2. **寄存器池太大，RAW 就太少。** 如果在 32 个寄存器里随机挑源/目的，相邻两条指令撞上同一个寄存器的概率很低，于是记分牌的冒险检测逻辑长期得不到锻炼。

所以约束随机的本质，是**反向利用这两点**：压低分支频率、缩小寄存器池，把随机数逼进「冒险密集」的区域。与此同时，还要再加一组「保命约束」，让程序**不会崩溃**（不会访问非法地址、不会陷入死循环），否则程序根本跑不到能被比对的那一步。

#### 4.1.2 核心流程

`generate_random.py` 的主体是把若干个「指令生成函数」按概率混合，逐条吐出汇编。每种指令类型对应一个生成函数，配一个权重：

| 生成函数 | 权重 | 累积阈值 | 实际占比 |
| --- | --- | --- | --- |
| `generate_computed_pointer` | 0.10 | 0.10 | 10% |
| `generate_binary_arith` | 0.50 | 0.60 | 50% |
| `generate_unary_arith` | 0.05 | 0.65 | 5% |
| `generate_compare` | 0.10 | 0.75 | 10% |
| `generate_memory_access` | 0.20 | 0.95 | 20% |
| `generate_device_io` | 0.01 | 0.96 | 1% |
| `generate_cache_control` | 0.03 | 0.99 | 3% |
| `generate_branch` | 1.00 | 1.99 | **1%** |

注意最后一行 `generate_branch` 的权重写的是 `1.0`，看起来很大，其实它只是个「足够大的兜底值」——因为 `random.random()` 取值落在 \([0, 1)\)，只有当随机数 ≥ 0.99（前 7 项累积和）时才轮到分支，所以**分支的实际占比只有 1%**。这是初读时最容易看走眼的地方。

每条指令的派发逻辑是一个简单的累积概率遍历：

```text
inst_type ← random() ∈ [0, 1)
cumul ← 0
for (权重, 函数) in 生成函数表:
    cumul ← cumul + 权重
    if inst_type < cumul:
        调用 该函数 生成一条指令
        break
```

于是整张表被设计成：**二元算术占一半**（最有机会形成长依赖链），**访存占两成**（压缓存），**分支只占 1%**（避免冲刷掩盖冒险）。

#### 4.1.3 源码精读

先看权重表与派发循环本身：

- 权重表 [tests/cosimulation/generate_random.py:398-407](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L398-L407) 定义了 8 个 `(概率, 函数)` 二元组，`generate_branch` 的概率值故意写成 `1.0`。
- 派发循环 [tests/cosimulation/generate_random.py:532-538](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L532-L538) 正是上面伪代码的直译。

再看「保命约束」的基石——**寄存器约定**。生成器把 32 个寄存器划成几组，各组职责互不交叉，写死在文件开头的模块文档里（[tests/cosimulation/generate_random.py:18-33](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L18-L33)）：

```
v0, s0 - 共享数据段基址（只读）
v1, s1 - 计算地址寄存器，保证 64 字节对齐且落在私有段内
v2, s2 - 私有数据段基址（每线程可读可写）
v3-v8, s3-s8 - 运算寄存器
s9 - 指向 MMIO 空间（0xffff0000）
```

**运算指令只会用到 s3–s8 / v3–v8 这 6 个寄存器**，因为 `generate_arith_reg()` 把随机范围硬限制在 3 到 8（[tests/cosimulation/generate_random.py:40-43](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L40-L43)）。这就是「小寄存器池」约束的落点。而 s0/s1/s2/s9 这些「系统寄存器」被排除在运算之外，避免运算指令误改指针导致崩溃——这是「保命」的一面。

为什么缩小寄存器池能放大 RAW 覆盖？可以做一个最简单的概率估算：若相邻两条指令各自独立地在一个大小为 \(n\) 的寄存器池里挑寄存器，那么后一条的某个源恰好等于前一条目的的概率是

\[
P_{\text{RAW}}(n) = \frac{1}{n}
\]

代入两种池大小：

\[
P_{\text{RAW}}(6) = \frac{1}{6} \approx 16.7\%, \qquad
P_{\text{RAW}}(32) = \frac{1}{32} \approx 3.1\%
\]

也就是说，把寄存器池从 32 缩到 6，**每对相邻指令的 RAW 命中率提升约 5 倍**。这就是约束随机「用设计换覆盖」的最直接证据。

最后，算术/比较指令表里还能看到一类「主动回避」：浮点指令被整段注释掉了。`BINARY_OPS` 里 `add_f`/`sub_f`/`mul_f`（[tests/cosimulation/generate_random.py:79-83](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L79-L83)）和 `COMPARE_OPS` 里的 `_f` 比较（[tests/cosimulation/generate_random.py:201-205](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L201-L205)）都带注释「还有舍入 bug 导致不一致」。这对应 u5-l3 的结论：Nyuzi 浮点非完全 IEEE 754 兼容，于是干脆不进入随机覆盖范围（详见 4.4 与 README 的「限制」一节）。

#### 4.1.4 代码实践

**实践目标：** 不运行任何工具，纯靠读源码算出 `generate_random.py` 的「有效指令分布」，验证分支确实只占约 1%。

**操作步骤：**

1. 打开 [tests/cosimulation/generate_random.py:398-407](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L398-L407)，把 8 个权重从上到下累加，填出本讲 4.1.2 表格里的「累积阈值」列。
2. 对照派发循环 [tests/cosimulation/generate_random.py:532-538](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L532-L538)，确认「实际占比 = 本档累积阈值 − 上一档累积阈值」。
3. 检验：前 7 档累积到 0.99，分支档占 `1.0 − 0.99 = 0.01`，即 1%。

**需要观察的现象 / 预期结果：** 8 档占比之和应等于 1.0（因为 `random()` 永远 < 1.0，1.99 的兜底阈值只会贡献到 1.0 处）。二元算术应占 50%、访存 20%、分支 1%。

> 本实践为源码阅读型，无需运行；若想实证，可在装好工具链后执行 `./generate_random.py -o /tmp/r.S -n 100000 -t 1`，再用 `grep -c` 统计各助记符频次，应与上表大体一致（**待本地验证**）。

#### 4.1.5 小练习与答案

**练习 1.** 如果把 `generate_branch` 的权重从 `1.0` 改成 `0.05`（其余不变），分支的实际占比会变成多少？程序还能正常工作吗？

> **答：** 前 7 档累积到 0.99 不变；分支档累积阈值变成 1.04，仍 > 1.0，所以分支占比仍是 `1.0 − 0.99 = 0.01`，**不变**。只要兜底阈值 ≥ 1.0，单独改它不影响分布——这也说明作者写成 `1.0` 是为了让它「必然兜底」。程序逻辑依然成立。

**练习 2.** 为什么作者不干脆把权重都归一化成和为 1.0，而要用这种「最后一个写大数」的写法？

> **答：** 这是一种**容错的兜底写法**：调整前面任何一档的权重时，只要保证累积过程单调上升、最后一档 ≥ 1.0，派发逻辑就始终能覆盖整个 \([0,1)\) 区间，永远不会出现「随机数落空、没有函数被调用」的情况。代价是「最末档的实际占比」要靠减法推算，不能直读——这也是本讲特意把它单列成一张表的原因。

---

### 4.2 分支约束：只用前向短分支

#### 4.2.1 概念说明

分支是随机生成的「头号危险源」：一条向后的分支可能形成**死循环**，让程序永远跑不到 `halt`；一条跳得很远的分支会**跳过大段代码**，使被跳过的指令根本不参与比对。README 用两句话定下规矩（[tests/cosimulation/README.md:103-107](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/README.md#L103-L107)）：**只生成前向分支**（避免死循环），且**只往前跳少于 8 条指令**（避免跳过太多代码）。

「前向」还带来一个协同仿真的副作用好处：前向短分支让控制流大体保持「往下走」，于是指令的**程序序**与**退休序**比较接近，减轻了 u8-l3 里「硬件把乱序退休事件重排回程序序」的压力。

#### 4.2.2 核心流程

分支生成只允许四种形态：

```text
bz   s?, Nf     # 条件：寄存器最低位为 0 则跳
bnz  s?, Nf     # 条件：寄存器最低位为 1 则跳
call Nf         # 无条件调用
b    Nf         # 无条件跳转
```

其中 `Nf` 是汇编器的「前向匿名标号」语法（`N` 是 1–6 的数字，`f` 表示 forward）。关键设计有三：

1. **只用 `f`（前向）**，永不出现 `b`（后向），从语法上杜绝死循环。
2. **距离 1–6 条**：`random.randint(1, 6)`，落在 README 说的「少于 8 条」之内。
3. **标号循环复用**：代码里维护一个在 1–6 之间循环的 `label_idx`，每条指令前都贴一个递增标号；程序末尾再把 1–6 号标号统一定义为 `nop` 兜底，保证任何 `Nf` 引用都能解析到合法位置。

#### 4.2.3 源码精读

四种分支形态定义在 `BRANCH_TYPES`（[tests/cosimulation/generate_random.py:327-332](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L327-L332)），元组第二位标记是否条件分支。生成函数 [tests/cosimulation/generate_random.py:335-354](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L335-L354) 据此拼出 `bz s3, 2f` / `b 5f` 之类的指令，距离恒为 `random.randint(1, 6)`。

标号的「循环复用 + 末尾兜底」是让前向引用成立的精妙之处，见两个片段：

- 每条指令前的标号自增并在 1–6 间取模（[tests/cosimulation/generate_random.py:528-531](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L528-L531)）。于是序列里反复出现 `2:`、`3:`…`6:`、`1:`，而汇编器对 `2f` 的解释是「**往后找**的第一个 `2:`」。
- 每个线程的随机段末尾，固定写死 6 个 nop 标号（[tests/cosimulation/generate_random.py:540-547](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L540-L547)），再接 `halt_current_thread`。这样即使某条分支跳到的 `Nf` 在随机段里没贴上对应标号，也一定能在末尾的兜底 nop 处落地，然后顺流到 `halt` 干净退出——既保证「前向」又保证「必终止」。

把 4.1 与 4.2 合起来看，就能回答本讲实践任务的前半句：**为什么只用前向短分支**——前向杜绝死循环保证程序能跑到比对阶段；短距离避免跳过太多指令、让冒险链被冲刷掩盖；而 1% 的极低频次，则把分支对流水线的「冲刷噪声」压到最低，让冒险与缓存问题尽可能多地被比对到。

#### 4.2.4 代码实践

**实践目标：** 验证「前向短分支 + 标号兜底」确实能让任意随机种子下程序都不死循环、不跳飞。

**操作步骤：**

1. 读 [tests/cosimulation/generate_random.py:526-550](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L526-L550)，注意每个线程段的结构是「若干带标号的随机指令 → 6 个 nop 兜底 → halt」。
2. 假设某条指令生成了 `b 6f`，但后续 5 条指令前的标号依次是 `2,3,4,5,1`（因为取模循环），问：这条 `b 6f` 会落到哪里？
3. 追踪 `halt_current_thread` 宏（[tests/asm_macros.h:85-91](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/asm_macros.h#L85-L91)），确认它是通过写 `CR_SUSPEND_THREAD` 让本线程退出，从而保证程序终将结束。

**需要观察的现象 / 预期结果：** 第 2 步中，由于末尾固定有 `6: nop` 兜底，`b 6f` 会跳到末尾的 `6:`，随后顺次执行 nop 与 halt。无论随机种子如何，分支永远前向、永远在 6 条内、永远有合法落脚点——程序必然终止。

> 本实践为源码阅读型；运行实证需 Verilator/模拟器工具链（**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1.** 如果把分支距离上限从 6 改成 60，会带来什么坏处？

> **答：** 一次分支可能跳过 60 条指令，被跳过的指令全部不参与比对，等于浪费了 60 条「本可压测冒险/缓存」的指令；同时控制流被拉散，硬件重排队列压力增大。README 之所以限定「少于 8 条」，正是为了把「跳过」的代价控制在可接受范围内。

**练习 2.** 为什么要同时保留 `bz`/`bnz`（条件）和 `b`/`call`（无条件）四种？只用 `bz` 一种行不行？

> **答：** 条件分支的「taken/not-taken」本身就是流水线验证的重点（分支预测、回滚），必须覆盖；无条件分支和 `call` 则覆盖了「必然跳转 + 返回地址写回（s31）」这条路径。只用 `bz` 会让 `call` 的返回地址链路（见 u2-l4）完全得不到测试。

---

### 4.3 内存指针约束：固定指针寄存器与每线程私有写区

#### 4.3.1 概念说明

访存指令是另一类「危险源」：如果用随机值当地址，几乎必然访问未对齐或越界地址，立刻触发 `TT_UNALIGNED_ACCESS` / `TT_PAGE_FAULT` 陷阱而崩溃。约束随机的对策是**保留专用寄存器当指针，并保证它们始终指向合法地址**。

更进一步，为了让访存指令能**密集命中缓存交互**（L1 命中/缺失、L2 命中/缺失/写回、别名），生成器精心安排了三件事（[tests/cosimulation/README.md:109-128](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/README.md#L109-L128)）：

1. **固定指针寄存器**：s0/v0 指共享只读段，s1/v1 指私有可写「计算地址」，s2/v2 指私有段基址。
2. **随机偏移命中不同缓存行**：偏移按缓存行（64 字节）或访问宽度对齐来随机，刻意制造 L1/L2 命中与缺失的混合。
3. **各段对齐到 L2 容量的整数倍**，使不同地址映射到同一缓存组，**人为制造别名与 L2 写回**。

还有一条「保命 + 保正确」的关键约束：**每个线程写自己的私有段，互不重叠**。这是因为模拟器不建模 store buffer（见 u8-l3、u10-l1），无法准确模拟「同缓存行多线程读写」的可见性，于是干脆从源头上让各线程写不同区域。

#### 4.3.2 核心流程

访存生成的总体策略：

```text
ptr_reg ← 随机选 0 或 1
若 ptr_reg == 0（共享只读段）：只能是 load
若 ptr_reg == 1（私有计算地址）：load 或 store 均可

访问形态 ← 三选一：
  0: 向量块访存   偏移 = 随机×64        （整行搬运）
  1: scatter/gather 偏移 = 随机×4        （逐 lane 串行）
  2: 标量访存     偏移 = 随机×对齐宽度
```

此外，`generate_computed_pointer` 会以 10% 的概率（见 4.1 权重表）把 s1/v1 重新赋值为 `s2/v2 + 随机×64`，让「计算地址」在私有段内随机漂移——这同时验证了**访存指令的 RAW 冒险**（见 [tests/cosimulation/README.md:116-118](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/README.md#L116-L118)）：一条写 s1 的 `add_i` 紧跟一条用 s1 寻址的 load，正是记分牌要管的那种依赖。

#### 4.3.3 源码精读

私有段的「每线程独立」建立在启动时的地址计算上（[tests/cosimulation/generate_random.py:429-444](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L429-L444)）：

```asm
getcr s2, CR_CURRENT_THREAD     # 取线程号 0/1/2/3
add_i s2, s2, 8                 # +8
shl  s2, s2, 20                 # ×1MB：私有基址
```

于是线程 0 的私有段落在 `8<<20 = 0x800000`，线程 1 落在 `0x900000`，依此到 `0xb00000`，与文件头内存映射（[tests/cosimulation/generate_random.py:27-32](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L27-L32)）完全吻合。接着 `fill_loop`（[tests/cosimulation/generate_random.py:455-464](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L455-L464)）用一个以线程号为种子的线性同余发生器把每段填上已知伪随机图案——这样段内有确定内容可供 load 比对，又因每段种子不同而互不相同。

访存生成本体见 [tests/cosimulation/generate_random.py:248-305](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L248-L305)。要点逐条对应 README 的设计意图：

- `ptr_reg = random.randint(0, 1)` 选共享/私有指针；共享段（ptr_reg==0）强制只读，所以 `opstr = 'load'`（第 263 行的短路逻辑）。
- 三类访问形态分别用 `offset = random.randint(0,16)*64`（块，整行）、`*4`（scatter/gather）、`*align`（标量），偏移范围被刻意设计成能横跨多个缓存行，制造命中/缺失混合。
- 标量访存的宽度与对齐表 `LOAD_OPS`/`STORE_OPS`（[tests/cosimulation/generate_random.py:233-245](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L233-L245)）覆盖 8/16/32 位与有无符号扩展，连带符号扩展路径一起压测。

「计算地址」漂移见 [tests/cosimulation/generate_random.py:357-376](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L357-L376)，它只写 s1/v1，偏移恒为 64 的倍数，确保对齐且不出私有段。

最后是「兜底校验」如何与缓存覆盖配合。协同仿真驱动脚本 dump 出私有段内存镜像并比对（[tests/cosimulation/runtest.py:90-91](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/runtest.py#L90-L91) 的 `assert_files_equal`），比对范围正是 `0x800000` 起的私有区（Verilator 侧 `+memdumpbase=800000 +memdumplen=400000`、模拟器侧 `-d ...,0x800000,0x400000`，见 [tests/cosimulation/runtest.py:41-58](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/runtest.py#L41-L58)）。因为随机程序不会主动 flush 缓存、而 C 模型又不建模缓存，Verilator 侧必须靠 `+autoflushl2`（[tests/cosimulation/runtest.py:47](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/runtest.py#L47)）在结束时把脏 L2 行写回内存，两侧镜像才有可比性——这正是 README 第 124–128 行（[tests/cosimulation/README.md:124-128](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/README.md#L124-L128)）所说机制的脚本落点。

> 关于同步访存的一个细节：生成器里有一段「在 `_sync` load 前插 `membar`」的守卫（[tests/cosimulation/generate_random.py:296-299](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L296-L299)），其注释解释了动机——模拟器不建模 store queue，store 可能让随后的同步 load 看到旧值，故用 membar 强制排序。注意当前 `LOAD_OPS` 表里实际不含 `_sync` 项，所以这段守卫在现行代码里并不会被触发，可视作预留的防御性逻辑；读代码时不必纠结，理解其设计意图即可。

#### 4.3.4 代码实践

**实践目标：** 解释「固定指针寄存器 + 限制寄存器编号」如何同时提升 **缓存覆盖** 与 **RAW 覆盖**。

**操作步骤：**

1. 读 [tests/cosimulation/generate_random.py:248-305](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L248-L305)，列出访存指令可能用到的全部基址寄存器，确认它们只有 s0/s1（标量）与 v0/v1（向量），而**绝不**使用 s3–s8。
2. 读 [tests/cosimulation/generate_random.py:357-376](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L357-L376)，确认 `generate_computed_pointer` 写的是 s1/v1。
3. 把两条事实拼起来：一条 `add_i s1, s2, ...`（写 s1）之后，随机访存有 50% 概率选 `ptr_reg=1`（用 s1 寻址）——这就是一条「写后读」RAW。
4. 对照 README 第 120–123 行（[tests/cosimulation/README.md:120-123](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/README.md#L120-L123)），理解各段对齐到 L2 容量整数倍如何「故意制造别名与写回」。

**需要观察的现象 / 预期结果：**

- **缓存覆盖：** 因为基址寄存器内容受控、偏移随机且段对齐到 L2 容量，访存会自然散布到不同缓存组，并周期性触发别名（同一物理行映射到同组不同路）和 L2 脏行写回——L1/L2 的命中、缺失、替换、snoop 路径都能被压到。
- **RAW 覆盖：** 因为 `generate_computed_pointer`（写 s1/v1）与 `generate_memory_access`（读 s1/v1 寻址）共享同一组指针寄存器，二者相邻时就构成 RAW，专测访存指令的依赖检测。

> 本实践为源码阅读型；运行实证需工具链（**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1.** 为什么访存基址不也用 s3–s8 这些运算寄存器，而要单独保留 s0/s1/s2？

> **答：** 两点。其一，保命：运算指令会随机改写 s3–s8，若地址也放在这里，一条算术指令就可能把指针改成非法值导致崩溃。其二，可解释：把「指针寄存器」与「运算寄存器」物理隔离，地址来源永远可控、永远合法，测试才是可重复、可调试的。

**练习 2.** README 说「各段对齐到 L2 缓存容量的整数倍，以制造别名与写回」。结合 u6-l3，这是什么道理？

> **答：** L2 是组相联的物理索引缓存。若多个段基址相差恰为 L2 容量（组数×路数×行）的整数倍，它们会映射到**同一组**，于是访问不同段时会在同一组内互相驱逐、触发脏行写回，从而压测 L2 的替换与写回路径。这是一种「故意制造冲突」以提升覆盖的技巧。

---

### 4.4 csmith：整程序随机与跨字宽限制

#### 4.4.1 概念说明

`generate_random.py` 生成的是**汇编**，压测的是硬件 RTL。但编译器（NyuziToolchain，基于 LLVM 的 clang 后端）本身也可能有 bug——代码生成错了，硬件再正确也没用。**csmith** 专治这一层：它是犹他大学开发的工具，能生成「语法合法、语义可控」的**随机 C 程序**（[tests/csmith/README.md:1-3](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/csmith/README.md#L1-L3)）。

验证思路是**交叉执行 + checksum 比对**：

1. 用宿主机原生 `cc`（如 x86-64 gcc）编译并运行 csmith 程序，它会对自身数据结构算一个校验和（checksum）输出。
2. 用 Nyuzi 的 clang 把同一份 C 编译成 Nyuzi 机器码，在 Nyuzi 模拟器上运行，得到另一个 checksum。
3. 两个 checksum 相等，就说明「Nyuzi 编译器 + 模拟器」完整复现了程序的语义。

注意这又一次用到了「副作用比对」的思想：不逐条比指令，而是比程序**最终的可观测输出**（checksum）。

#### 4.4.2 核心流程

csmith 测试只跑在模拟器目标上（不经 Verilator），流程是 100 次循环：

```text
for x in 0..99:
    csmith --no-longlong --no-packed-struct  → 生成随机 C
    cc（宿主）编译运行  → host_checksum
    Nyuzi clang 编译 + 模拟器运行 → emulator_checksum
    if host_checksum != emulator_checksum: 报错
```

两个关键约束 `--no-longlong` 与 `--no-packed-struct` 是为规避 Nyuzi 与 64 位宿主的**字宽不匹配**而设。

#### 4.4.3 源码精读

整段驱动在 [tests/csmith/runtest.py:39-85](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/csmith/runtest.py#L39-L85)，只注册到 `emulator` 目标（第 39 行装饰器 `@test_harness.test(['emulator'])`）。核心循环 [tests/csmith/runtest.py:51-83](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/csmith/runtest.py#L51-L83) 每轮：

1. 调 csmith 生成源码，关键约束在第 58–59 行（[tests/csmith/runtest.py:55-59](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/csmith/runtest.py#L55-L59)）：`--no-longlong`、`--no-packed-struct`，注释直接点明原因——「禁用 long long 以避免 32 位 Nyuzi 与 64 位宿主的不兼容；禁用 packed struct 因为我们不支持非对齐访问」。
2. 宿主编译运行、正则 `checksum = ([0-9A-Fa-f]+)` 抓出 host_checksum（[tests/csmith/runtest.py:62-71](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/csmith/runtest.py#L62-L71)）。
3. 用 `test_harness.build_program` 走 Nyuzi 工具链编译、`run_program` 在模拟器跑、再抓 emulator_checksum（[tests/csmith/runtest.py:74-81](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/csmith/runtest.py#L74-L81)）。
4. 比对，不等则抛 `TestException`（[tests/csmith/runtest.py:82-83](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/csmith/runtest.py#L82-L83)）。

**跨字宽限制**是 csmith 测试最大的已知坑，README 用粗体郑重警告（[tests/csmith/README.md:22-23](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/csmith/README.md#L22-L23)）：在 64 位宿主上运行时，**某些运算会产生不同结果，从而造成假阴性（false negative）**。根源是 C 语言里 `long`、`long long` 等类型的位宽随宿主变化：Nyuzi 是 32 位机，宿主通常是 64 位，同一个 `long long` 运算（尤其是涉及 64 位乘法、移位、溢出回绕）在两边可能给出不同 bit 模式。`--no-longlong` 正是为此把 64 位类型挡在门外；`--no-packed-struct` 则是因为 packed 结构会强制非对齐访问，而 Nyuzi 不支持非对齐访存（会触发 `TT_UNALIGNED_ACCESS`），同样会导致两边行为分叉。

把 csmith 与 `generate_random.py` 放在一起，就能看清 Nyuzi 随机验证的「双层」结构：

| 维度 | generate_random.py | csmith |
| --- | --- | --- |
| 生成物 | 随机**汇编** | 随机 **C 程序** |
| 比对双方 | Verilator **RTL** ↔ C **模拟器** | **宿主 gcc** ↔ Nyuzi **模拟器** |
| 验证对象 | **硬件**微架构实现 | **编译器** + 模拟器语义 |
| 比对单位 | 逐条指令副作用 + 最终内存镜像 | 程序最终 checksum |
| 主要限制 | 不覆盖浮点/store buffer/虚拟内存翻译 | 64 位宿主字宽不一致致假阴性 |

两者互补：一个从下往上托住硬件，一个从上往下托住工具链。

#### 4.4.4 代码实践

**实践目标：** 理解 csmith 的「宿主 vs 模拟器」比对模型，以及两个禁用开关各自规避的失败模式。

**操作步骤：**

1. 读 [tests/csmith/runtest.py:55-59](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/csmith/runtest.py#L55-L59)，把 `--no-longlong` 和 `--no-packed-struct` 旁边的注释翻译成自己的话。
2. 追踪 checksum 的产生与比对路径：`CHECKSUM_RE` 正则（[tests/csmith/runtest.py:35](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/csmith/runtest.py#L35)）→ 宿主侧抓取（第 66–70 行）→ 模拟器侧抓取（第 76–80 行）→ 比对（第 82–83 行）。
3. 设问：若去掉 `--no-longlong`，在一个会生成 64 位乘法溢出的随机程序上，宿主（64 位）与 Nyuzi（32 位，无 64 位硬件乘法，靠软件实现）最可能在哪一步产生不同 checksum？

**需要观察的现象 / 预期结果：** 第 3 步——`long long` 的 64 位运算在 32 位 Nyuzi 上需软件多精度实现，一旦编译器的 64 位展开或模拟器的 64 位语义与 64 位宿主原生指令在**溢出回绕、符号、舍入**上不一致，checksum 就会不同，触发假阴性。这正是必须禁用 `long long` 的根本原因。

> 本实践为源码阅读型；运行 csmith 需先按 [tests/csmith/README.md:6-18](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/csmith/README.md#L6-L18) 安装 csmith（**待本地验证**）。

#### 4.4.5 小练习与答案

**练习 1.** csmith 测试为什么只注册到 `emulator` 目标，而不像 cosimulation 测试那样也跑 verilator？

> **答：** csmith 验证的是「编译器 + 模拟器」语义是否与宿主一致，参照系是宿主 gcc，跑模拟器即可。若改跑 Verilator，则 100 次随机程序每次都要走周期精确仿真，速度慢数个量级，而增益（额外验证 RTL）相对有限——RTL 已经由 `generate_random.py` 的大规模协同仿真覆盖。所以这里用模拟器这个「快但功能正确」的目标就够。

**练习 2.** 既然 csmith 会因字宽问题产生假阴性，为什么还要保留它？

> **答：** 即便存在假阴性，csmith 仍能暴露编译器在「指针运算、整数溢出、结构体布局、控制流」等大量**非字宽敏感**路径上的代码生成 bug，覆盖面是手写测试难以企及的。工程上的取舍是：接受偶发假阴性（用 `--no-longlong`/`--no-packed-struct` 尽量压低），换取对编译器海量路径的回归保护。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个**「约束设计反推」**任务，它正好对应本讲规格里的代码实践任务。

**任务：** 假设你是 Nyuzi 验证团队的新人，被要求评审一份「改进版」随机生成方案。该方案提议做三处改动——(A) 把分支权重从 1% 提到 25%，让控制流更丰富；(B) 把运算寄存器池从 s3–s8（6 个）扩到 s3–s30（28 个），「减少寄存器压力」；(C) 让所有线程共享同一个读写段，「简化内存布局」。请逐一判断这三处改动是否会损害覆盖目标，并用本讲师过的源码与概率推理说明理由。

**操作步骤：**

1. **针对 (A)**，回顾 4.1.1 与 [tests/cosimulation/README.md:91-96](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/README.md#L91-L96)：分支会冲刷流水线、掩盖冒险。判断 25% 分支会把多少比例的指令「冲掉」。
2. **针对 (B)**，用 4.1.3 的公式 \(P_{\text{RAW}}(n)=1/n\) 算出池从 6 扩到 28 后 RAW 命中率的变化（从 ≈16.7% 降到 ≈3.6%），说明记分牌覆盖会下降多少。
3. **针对 (C)**，回顾 4.3.1 与 README 的「限制」节（[tests/cosimulation/README.md:145-150](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/README.md#L145-L150)）：模拟器不建模 store buffer，无法正确模拟同缓存行多线程读写。判断共享写段会让协同仿真出现什么「假错/漏错」。

**需要观察的现象 / 预期结果：**

- (A) 损害：分支过密 → 大量指令被冲刷 → RAW/缓存 bug 被掩盖。应**拒绝**。
- (B) 损害：寄存器池扩大 → RAW 概率从 ~1/6 跌到 ~1/28 → 记分牌冒险检测覆盖骤降。应**拒绝**。
- (C) 损害：共享写段 → 多线程同缓存行读写 → 模拟器因不建模 store buffer 给出与 RTL 不一致的可见性 → 要么大量假阳性（误报 mismatch），要么被迫放宽比对（漏报真 bug）。应**拒绝**，维持每线程私有写段。

**预期产出：** 一份半页纸的评审意见，三处改动均建议驳回，每条引用本讲至少一处源码行号或概率推理作依据。

> 本综合实践为源码阅读与分析型，不依赖运行工具链；结论可在本地用 `grep`/计数脚本进一步实证（**待本地验证**）。

## 6. 本讲小结

- **约束随机 = 随机求数量 + 约束求质量。** 无偏随机覆盖率差（分支冲刷、寄存器池大），所以 Nyuzi 给生成器戴上三重镣铐：低频分支、小寄存器池、固定指针寄存器。
- **指令分布是精心调过的。** 8 档权重里二元算术占 50%、访存占 20%、分支仅占 1%（注意 `generate_branch` 的 `1.0` 是兜底阈值，不是占比），派发靠累积概率遍历（[generate_random.py:398-407](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L398-L407) 与 [532-538](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L532-L538)）。
- **分支只用前向、只跳 1–6 条**，靠「标号循环复用 + 末尾 nop 兜底」保证不死循环、不跳飞（[generate_random.py:335-354](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L335-L354) 与 [540-547](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L540-L547)）。
- **内存访问用固定指针寄存器 + 每线程私有写区**，偏移随机命中不同缓存行、段对齐到 L2 容量整数倍以制造别名与写回；私有写区是为了回避「模拟器不建模 store buffer」的盲区。
- **缩小寄存器池放大 RAW**：\(P_{\text{RAW}}(n)=1/n\)，池从 32 缩到 6 使相邻指令 RAW 命中率提升约 5 倍。
- **csmith 验证编译器而非硬件**：宿主 gcc 与 Nyuzi 模拟器比对 checksum；受 32/64 位字宽不匹配困扰，必须 `--no-longlong`、`--no-packed-struct`，且在 64 位宿主上仍有假阴性。

## 7. 下一步学习建议

- **横向补齐「限制」全景：** 本讲多处引用了协同仿真的已知盲区（浮点、store buffer、store_sync+中断、subcycle、虚拟内存翻译）。建议精读 [tests/cosimulation/README.md:144-167](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/README.md#L144-L167)，把这些「不覆盖」与对应的硬件讲义（u5-l3 浮点、u10-l1 同步访存、u7-l1 TLB）对上号，理解每个盲区的硬件根因。
- **纵向进入下一讲 u15-l3：** 下一篇「单元测试与整机测试策略」会把随机测试放到整个验证金字塔里，与 unit（周期精确、内部信号可见）、core/isa（定向功能）、whole-program/render（整机比对）四类测试对比，说明为何需要多层次互补。
- **动手延伸：** 若已装好工具链，可尝试给 `generate_random.py` 增加一种新的指令生成函数（如 `cr` 读写），按本讲学到的「约束」原则给它选一个合理的权重并加进 `GENERATE_FUNCS`，再用 `runtest.py random*` 跑一轮协同仿真，观察是否会暴露新的 mismatch——这是从「读懂约束」走向「设计约束」的最短路径。
