# 弹性缓冲与流水线解耦

## 1. 本讲目标

上一讲（u2-l1）我们看到 `vector_top` 在 vRRM→vIS、vIS→vEX 等流水级之间插入了若干 `eb_buff_generic`，并且它们统一使用 `BUFF_TYPE=1`。本讲就把这个「黑盒子」打开，学完后你应当能够：

- 说清楚 `BUFF_TYPE` 的 0/1/2/3 四种取值各自对应哪种缓冲后端，以及在**吞吐**和**组合回压路径**上的差异。
- 读懂 `eb_one_slot` 的 `ready/valid` 握手，并能解释 `FULL_THROUGHPUT` 为什么能换来 100% 吞吐、代价又是什么。
- 读懂 `eb_two_slot` 如何用「多一个槽」把组合回压路径切断，从而抬高可达到的主频。
- 读懂 `fifo_duth` 这个循环缓冲 FIFO 后端的指针、状态计数与输出选择逻辑。
- 能够回答本讲的核心实践问题：**为什么 `vector_top` 的 `vRR_vIS` 选 `BUFF_TYPE=1` 而不是 `2` 或 `3`**，并能给出 one-slot 与 two-slot 在时序（频率）上的取舍理由。

---

## 2. 前置知识

### 2.1 ready/valid 握手与反压

本仓库的流水线各级之间用一对握手信号通信，这是 AXI-Stream 风格的**valid/ready 握手**：

- 生产者（上游）给出 `valid` 和 `data`，表示「我有一份有效数据」。
- 消费者（下游）给出 `ready`，表示「我能接收一份数据」。
- 只有当 `valid & ready` 同时为 1 的那个时钟沿，一次**传输（transfer）**才真正发生。

关键在于**反压（backpressure）**：当下游来不及处理时，它把 `ready` 拉低，上游就必须把数据「顶住」，不能丢。这种「上游被下游卡住」的信息要沿着与数据相反的方向传回去，所以也叫**回压（back-notification）**。

### 2.2 为什么流水级之间要插缓冲

理想流水线里，每一级都只做一个时钟周期的事，数据像传送带一样往下走。但现实里：

- 某一级可能偶尔停一拍（例如 vIS 检测到冒险、vMU 等缓存命中）。
- 如果上游和下游**直接**用组合逻辑连起来，下游的 `ready` 会一路组合地传回上游，形成一条很长的**组合路径**，拖慢主频。
- 我们希望在两级之间插一个**弹性缓冲（Elastic Buffer, EB）**：它能存一两拍数据，既能**吸收局部的速率不匹配**，又能**切断或缩短组合回压路径**。

弹性缓冲的本质就是：一个带 ready/valid 接口的小存储 + 一套让上下游「谁快谁慢都不丢数据」的控制逻辑。本讲的四个文件就是在实现不同权衡下的弹性缓冲。

### 2.3 三个关键术语

- **吞吐（throughput）**：稳态下每周期完成几次传输。100% 吞吐 = 每周期 1 次 = 1 transfer/cycle。
- **延迟（latency）**：一个数据从进入缓冲到出现在输出端，跨了几个时钟周期。
- **组合回压路径（combinational backpressure path）**：从下游的 `ready_in` 组合地（不经过寄存器）影响上游的 `ready_out` 的那条逻辑路径。这条路径越长，主频越低。

---

## 3. 本讲源码地图

| 文件 | 角色 | 一句话作用 |
|------|------|-----------|
| [rtl/shared/eb_buff_generic.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/eb_buff_generic.sv) | 选择器/包装器 | 用 `BUFF_TYPE` 参数在 4 种后端里选一种，对外统一暴露 ready/valid 接口。 |
| [rtl/shared/eb_one_slot.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/eb_one_slot.sv) | 单槽后端 | 1 个寄存器槽，`FULL_THROUGHPUT` 决定是半带宽还是满吞吐（含组合回压）。 |
| [rtl/shared/eb_two_slot.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/eb_two_slot.sv) | 双槽后端 | 2 个槽，满吞吐且**无**组合回压路径。 |
| [rtl/shared/fifo_duth.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/fifo_duth.sv) | FIFO 后端 | 深度可调的循环缓冲 FIFO，由 `and_or_mux` 做输出选择。 |
| [rtl/vector/vector_top.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv) | 使用方 | 在四级流水之间例化 4 个 `eb_buff_generic`，全部 `BUFF_TYPE=1`。 |
| [rtl/shared/and_or_mux.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/and_or_mux.sv) | 辅助 | `fifo_duth` 的输出选择器，由 one-hot 信号选通。 |

这四个文件都属于 `rtl/shared/`（跨标量/向量复用的通用 IP），在 [vector_simulator/files_rtl.f](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/vector_simulator/files_rtl.f) 里被列为「积木」。

---

## 4. 核心概念与源码讲解

### 4.1 弹性缓冲类型与 eb_buff_generic 选择器

#### 4.1.1 概念说明

`eb_buff_generic` 自己**不做任何存储**，它只是一个「分发器」：根据参数 `BUFF_TYPE`，在 `generate` 块里选择例化哪一种真正的后端，再把对外统一的 ready/valid 接口连过去。这样做的好处是——调用方（比如 `vector_top`）只认 `eb_buff_generic` 这一个名字，将来想换缓冲策略时，**只改一个参数**就行，不必动连线。

文件顶部的注释把四种类型一锤定音：

> [rtl/shared/eb_buff_generic.sv:12-16](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/eb_buff_generic.sv#L12-L16) —— `BUFF_TYPE` 的取值表：0=单槽半带宽（50% 吞吐）；1=单槽满带宽（100% 吞吐，但有组合回压路径）；2=双槽满带宽（100% 吞吐）；3=FIFO（深度由 `DEPTH` 设定）。

把它整理成一张对比表（贯穿全讲，建议反复对照）：

| `BUFF_TYPE` | 后端模块 | 槽位数 | 满吞吐 | 组合回压路径 | 本仓库是否使用 |
|------|------|------|------|------|------|
| 0 | `eb_one_slot`（`FULL_THROUGHPUT=0`） | 1 | 否 | 否 | 否 |
| 1 | `eb_one_slot`（`FULL_THROUGHPUT=1`） | 1 | 是 | **是** | 是（`vector_top` 全部 4 处） |
| 2 | `eb_two_slot` | 2 | 是 | 否 | 否（作为升级备选） |
| 3 | `fifo_duth`（`DEPTH` 可调） | `DEPTH` | 是（`DEPTH≥2`） | 否 | 否 |

#### 4.1.2 核心流程

`eb_buff_generic` 的执行流程就是一条「参数 → generate 分支 → 例化后端」的静态派发：

```text
读入参数 BUFF_TYPE
  ├── BUFF_TYPE ∈ {0,1} ──► eb_one_slot, 令 FULL_THROUGHPUT = (BUFF_TYPE==1)
  ├── BUFF_TYPE == 2     ──► eb_two_slot
  └── 其它(即 3)         ──► fifo_duth（注意：FIFO 接口不是 ready/valid，要转换）
```

唯一的「坑」是：`fifo_duth` 的接口是 `push/pop`（不是 flow-controlled 的 ready/valid），所以 `BUFF_TYPE=3` 分支里要做一次接口转换。其余两个后端的接口与 `eb_buff_generic` 完全一致，直接对应相连即可。

#### 4.1.3 源码精读

**参数与端口**：三个参数 `DW`（数据位宽）、`BUFF_TYPE`、`DEPTH`（仅 FIFO 用），端口是标准的「输入通道 + 输出通道」两套 ready/valid：

- [rtl/shared/eb_buff_generic.sv:17-34](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/eb_buff_generic.sv#L17-L34) —— 模块声明。注意默认 `BUFF_TYPE=2`（双槽），但 `vector_top` 实际都覆写成 1。

**分支一：单槽（BUFF_TYPE 0 或 1）**：把 `BUFF_TYPE==1` 这个布尔值作为 `FULL_THROUGHPUT` 传给 `eb_one_slot`：

- [rtl/shared/eb_buff_generic.sv:37-54](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/eb_buff_generic.sv#L37-L54) —— `gen_one_slot_eb` 分支。`.FULL_THROUGHPUT(BUFF_TYPE == 1)` 这一句是 0/1 两种类型差异的全部来源。`GATING_FRIENDLY` 被硬编码为 1（省功耗，见 4.2.3）。

**分支二：双槽（BUFF_TYPE 2）**：例化 `eb_two_slot`，接口一一对应：

- [rtl/shared/eb_buff_generic.sv:55-70](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/eb_buff_generic.sv#L55-L70) —— `gen_two_slot_eb` 分支。

**分支三：FIFO（BUFF_TYPE 3）**：做接口转换。`push` 发生在「上游 valid 且本缓冲可收」时，`pop` 发生在「下游 ready 且本缓冲有数」时：

- [rtl/shared/eb_buff_generic.sv:71-94](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/eb_buff_generic.sv#L71-L94) —— `gen_fifo` 分支，关键两行 `fifo_push`/`fifo_pop` 在 [L76-77](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/eb_buff_generic.sv#L76-L77)：把 ready/valid 握手翻译成 push/pop。

#### 4.1.4 代码实践

> **实践目标**：建立「`BUFF_TYPE` 一改、后端就换」的直觉。

操作步骤：

1. 打开 [rtl/vector/vector_top.sv:84-101](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L84-L101)，找到 `vRR_vIS` 这个例化。
2. 把 `.BUFF_TYPE(1)` 改成 `.BUFF_TYPE(2)`，**其它连线一行都不动**。
3. 在脑子里追踪：`generate` 会从 `gen_one_slot_eb` 跳到 `gen_two_slot_eb`，对外端口（`data_i/valid_i/ready_o/data_o/valid_o/ready_i`）完全不变。

需要观察的现象：由于 `eb_buff_generic` 对外接口不变，`vector_top` 的连线不需要任何改动就能切换后端——这正是「选择器模式」的价值。

预期结果：功能（仿真行为）应当**完全不变**（两者都是满吞吐），变化只在面积（2 个槽 vs 1 个槽）和时序（组合回压路径是否被切断）上。

> 这一改动是否真的「行为不变」需要本地用 QuestaSim 跑一遍 `results.log` 对比确认——**待本地验证**。

#### 4.1.5 小练习与答案

**Q1**：`BUFF_TYPE` 默认值是多少？`vector_top` 实际用的是什么？

**答**：默认 `BUFF_TYPE=2`（见 [eb_buff_generic.sv:20](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/eb_buff_generic.sv#L20)）；`vector_top` 全部 4 处都覆写成 `BUFF_TYPE=1`。

**Q2**：为什么 `BUFF_TYPE=3` 分支需要额外写 `fifo_push/fifo_pop` 两行，而 0/1/2 都不需要？

**答**：因为 `fifo_duth` 的接口是 `push/pop`（非流控），不是 ready/valid；必须用 `valid_i & ready_o`、`valid_o & ready_i` 把握手「翻译」成 push/pop。其余后端本身就是 ready/valid 接口，直接相连即可。

---

### 4.2 单槽缓冲 eb_one_slot：握手、反压与满吞吐

#### 4.2.1 概念说明

`eb_one_slot` 是整个缓冲体系的**核心**——`eb_two_slot` 也是用它搭出来的。它只有**一个数据寄存器** `data_r` 和**一个满标志** `full_r`，靠一个叫 `write_en` 的写使能把 ready/valid 握手管起来。

它有一个关键参数 `FULL_THROUGHPUT`（文件头注释 [rtl/shared/eb_one_slot.sv:9-16](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/eb_one_slot.sv#L9-L16) 解释得很清楚）：

- `FULL_THROUGHPUT=0`：**半带宽 EB（HBEB）**，`ready_out` 在缓冲满时拉低，稳态吞吐 ≤50%。
- `FULL_THROUGHPUT=1`：**流水化 EB（PEB）**，支持 100% 吞吐，但 `ready_out` 会**组合地**依赖 `ready_in`，也就是存在组合回压路径。

`vector_top` 选的 `BUFF_TYPE=1` 对应的就是 `FULL_THROUGHPUT=1`（PEB）。

#### 4.2.2 核心流程

`eb_one_slot` 的全部输出都由 `write_en` 这一个信号驱动：

```text
valid_out = full_r            // 有数据可读 ⇔ 槽是满的
ready_out = write_en          // 能收新数据 ⇔ 本拍允许写
data_out  = data_r            // 输出永远反映寄存器内容

write_en 的两种实现（generate 二选一）：
  FULL_THROUGHPUT=1 (PEB): write_en = ready_in | ~full_r
  FULL_THROUGHPUT=0 (HBEB): write_en = ~full_r

每个时钟沿（write_en 有效时）：
  full_r <= valid_in          // 满标志跟随上游是否有数
  data_r <= data_in           // （受 GATING_FRIENDLY 调节）
```

**为什么 PEB 能做到 100% 吞吐？** 看那行 `write_en = ready_in | ~full_r`：只要「下游 ready」**或**「自己没满」，本拍就允许写。关键场景是**下游 ready 且自己已满**——此时 `write_en` 仍为 1：

- 同一拍里，旧的 `data_r` 通过 `valid_out & ready_in` 被**下游取走**；
- 同时 `data_r <= data_in`，新的数据**本拍就写入**；
- 也就是「出 1 个、进 1 个」发生在**同一个周期**，没有任何空拍。

用吞吐公式表达，稳态下（`valid_in=1, ready_in=1` 持续）：

\[
T_{\text{PEB}} = 1 \;\text{transfer/cycle} = 100\%
\]

**代价**就是组合回压路径：`ready_out = write_en = ready_in | ~full_r`，`ready_out` 直接含 `ready_in` 这一项。于是下游的 `ready` 信号会**组合地、不经过任何寄存器**地传到上游。在 `vector_top` 里，这意味着 vIS 的 `i_ready`（背后是计分板冒险检测）会一路组合地影响 vRRM 的 `ready`——如果这段逻辑太深，就会成为关键路径，压低主频。

> 关于 HBEB（`BUFF_TYPE=0`）的一个**诚实的细节**：注释说它是「50% 吞吐」，这是教科书里 HBEB 的经典说法。但就本文件 [L50](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/eb_one_slot.sv#L50) `assign write_en = ~full_r` 这一行的实现而言，`full_r` 仅在 `write_en` 有效时才更新（见 [L56-64](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/eb_one_slot.sv#L56-L64)），而 `write_en` 在「满」时恰好为 0，所以**一旦置满就无法通过正常消费回到空**。这也是本仓库从不实例化 `BUFF_TYPE=0` 的根本原因——它只作为类型学上的对照存在，真正干活的是 PEB。

#### 4.2.3 源码精读

**端口**：输入侧 `valid_in/ready_out/data_in`，输出侧 `valid_out/ready_in/data_out`：

- [rtl/shared/eb_one_slot.sv:18-36](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/eb_one_slot.sv#L18-L36) —— 模块声明与参数。

**三句核心赋值**：整个模块的「灵魂」就是这三行：

- [rtl/shared/eb_one_slot.sv:42-44](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/eb_one_slot.sv#L42-L44) —— `valid_out=full_r`、`ready_out=write_en`、`data_out=data_r`。

**`write_en` 的二选一**：`FULL_THROUGHPUT` 的全部差异：

- [rtl/shared/eb_one_slot.sv:47-53](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/eb_one_slot.sv#L47-L53) —— PEB 是 `ready_in | ~full_r`，HBEB 是 `~full_r`。多出来的那个 `ready_in` 就是「100% 吞吐」和「组合回压路径」的共同来源。

**满标志寄存器**：

- [rtl/shared/eb_one_slot.sv:56-64](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/eb_one_slot.sv#L56-L64) —— 异步复位（`posedge rst`，高有效），`write_en` 有效时 `full_r <= valid_in`。注意复位极性：`vector_top` 用 `.rst(~rst_n)` 把核里的低有效 `rst_n` 翻成这里要的高有效 `rst`。

**数据寄存器与门控**：

- [rtl/shared/eb_one_slot.sv:67-73](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/eb_one_slot.sv#L67-L73) —— `GATING_FRIENDLY=1`（`eb_buff_generic` 硬编码）时，只有 `valid_in` 才真正写 `data_r`，避免在无效拍上无谓翻转、省功耗；代价是无效时 `data_r` 保留旧值（但因为 `valid_out=full_r` 会同步为 0，下游不会误读）。

#### 4.2.4 代码实践

> **实践目标**：亲手追踪 PEB 在「满 + 下游 ready」这一拍为什么不丢数据。

操作步骤：

1. 假设初始 `full_r=1`（槽里有一个数据 A），`valid_in=1`（上游有新数据 B），`ready_in=1`（下游要取数）。
2. 代入 PEB 的 `write_en = ready_in | ~full_r = 1 | ~1 = 1`。
3. 本拍对外：`valid_out = full_r = 1`，`ready_in = 1` ⇒ **A 被下游取走**；`ready_out = write_en = 1`，`valid_in = 1` ⇒ **B 被上游写入**。
4. 下一拍沿：`full_r <= valid_in = 1`，`data_r <= data_in = B`。

需要观察的现象：在「满」状态下，同一拍里 A 出、B 进，没有任何空拍。

预期结果：稳态吞吐 = 1 transfer/cycle，即 100%。这正是 `vector_top` 选 PEB 的原因——它不能容忍流水线寄存器把发射带宽砍半。

> 用波形验证：可在 QuestaSim 里对 `vIS_fEX_data` 这个实例加 `valid_i/ready_o/valid_o/ready_i` 四个信号，观察连续多拍 `valid & ready` 同时成立——**待本地验证**。

#### 4.2.5 小练习与答案

**Q1**：把 `FULL_THROUGHPUT` 从 1 改成 0（即 HBEB），`write_en` 表达式少了哪一项？它同时改变了哪两个指标？

**答**：少了 `ready_in` 这一项，变成 `write_en = ~full_r`。它同时让「100% 吞吐」丢失（无法同拍进出）、并消除了 `ready_out` 对 `ready_in` 的组合依赖（组合回压路径消失）。换句话说，HBEB 用吞吐换时序，PEB 用时序换吞吐。

**Q2**：`ready_out = write_en` 而不是 `ready_out = ~full_r`，这两种写法在 PEB 下等价吗？

**答**：在 PEB 下等价。因为 PEB 的 `write_en = ready_in | ~full_r`，而 `ready_out` 本就要表达「能否收」语义，直接用 `write_en` 更简洁；HBEB 下 `write_en = ~full_r`，`ready_out = write_en` 与 `ready_out = ~full_r` 也等价。统一用 `write_en` 是为了两种模式共用一行赋值。

**Q3**：为什么 `vector_top` 把 `.rst` 接成 `~rst_n`？

**答**：核内部（如 `vector_top`）用低有效 `rst_n`，而 `eb_one_slot` 的 `full_r` 寄存器写的是 `posedge rst`（高有效），所以要在例化时取反映射，保证复位语义一致。

---

### 4.3 双槽缓冲 eb_two_slot：切断组合回压的满吞吐

#### 4.3.1 概念说明

PEB（`BUFF_TYPE=1`）的唯一毛病是那条组合回压路径。如果某条流水级的 `ready` 计算很重（比如 vIS 的逐元素冒险检测），这条组合路径就会拖垮主频。`eb_two_slot`（`BUFF_TYPE=2`）就是为了**两全其美**而生的：

- 像 PEB 一样保持 **100% 吞吐**；
- 又像 HBEB 一样**没有组合回压路径**（`ready_out` 不组合依赖 `ready_in`）；
- 代价是多用一个槽（2 个寄存器），且输入数据路径多一个 2:1 选择器。

文件头注释把这层意图说得非常直白：

> [rtl/shared/eb_two_slot.sv:6-16](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/eb_two_slot.sv#L6-L16) —— 「2-slot EB，100% 吞吐且无组合回压通知路径；兼具 HBEB 与 PEB 的优点，但需要 2 个槽」。

#### 4.3.2 核心流程

`eb_two_slot` 内部其实是**两个 `eb_one_slot`（都设 `FULL_THROUGHPUT=1`）串联**，但巧妙地在它们之间插了一个**寄存过的 ready 信号** `ready_main_buf`，把组合路径切断：

```text
上游 ──► [eb_aux (one_slot)] ──► [eb_main (one_slot)] ──► 下游
                  ▲                       ▲
                  │ ready_to_aux_eb       │ ready_from_main_eb
                  │ = ready_main_buf      │
                  │   ↑ 寄存器！           │
              ready_main_buf <= ready_from_main_eb （打一拍）

输入侧的 2:1 MUX：
  若 aux 能收 (ready_from_aux_eb=1)：数据直接进 main（绕过 aux）
  若 aux 不能收：用 aux 暂存的数据进 main
```

**为什么这样就能切断组合回压？** 上游看到的 `ready_out = ready_from_aux_eb`（[L55](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/eb_two_slot.sv#L55)），而 `eb_aux` 的 `ready_in` 接的是 `ready_main_buf`——这是一个**寄存器**（[L79-85](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/eb_two_slot.sv#L79-L85)），复位值为 1。于是下游的 `ready_in` 要先被打一拍才影响上游的 `ready_out`，组合路径就此打断。

**代价**：

- 多一个槽（aux）；
- 输入数据路径出现 2:1 MUX（[L88-89](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/eb_two_slot.sv#L88-L89)）；
- 注释也坦承它「不那么省电」（aux 会做冗余写）、停顿时会**搬移（shift）数据**。

#### 4.3.3 源码精读

**内部信号**：分 main 通道和 aux 通道两组：

- [rtl/shared/eb_two_slot.sv:40-52](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/eb_two_slot.sv#L40-L52) —— main/aux 的 valid/ready/data 互连信号声明。

**对外的 ready 来自 aux**：

- [rtl/shared/eb_two_slot.sv:55](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/eb_two_slot.sv#L55) —— `assign ready_out = ready_from_aux_eb;` 上游的 ready 由 aux 决定。

**aux 实例**（一个标准的 `eb_one_slot`，满吞吐）：

- [rtl/shared/eb_two_slot.sv:58-74](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/eb_two_slot.sv#L58-L74) —— `eb_aux`。注意它的 `ready_in` 接 `ready_to_aux_eb`。

**切断组合路径的关键寄存器**：

- [rtl/shared/eb_two_slot.sv:76](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/eb_two_slot.sv#L76) —— `assign ready_to_aux_eb = ready_main_buf;`
- [rtl/shared/eb_two_slot.sv:79-85](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/eb_two_slot.sv#L79-L85) —— `ready_main_buf` 是寄存器，复位值 1（保证上电时 aux 能收），每拍跟随 `ready_from_main_eb`。就是它把回压路径「打断一拍」。

**输入 2:1 MUX**：当 aux 这一拍能收，数据直接进 main（绕过 aux 的寄存器延迟）；否则用 aux 暂存的：

- [rtl/shared/eb_two_slot.sv:88-89](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/eb_two_slot.sv#L88-L89) —— `valid_to_main_eb`/`data_to_main_eb` 的 2:1 选择。

**main 实例**（另一个 `eb_one_slot`，输出即整个模块输出）：

- [rtl/shared/eb_two_slot.sv:91-107](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/eb_two_slot.sv#L91-L107) —— `eb_main`，`ready_in` 直接接下游 `ready_in`，`data_out`/`valid_out` 即对外输出。注释里说「输出数据不经过任何 MUX」正是指 main 的 `data_out = data_r` 这条干净通路（FIFO 方案则要过输出 MUX）。

#### 4.3.4 代码实践

> **实践目标**：用「断点」思维定位组合回压路径在哪里被切断。

操作步骤：

1. 在 [eb_two_slot.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/eb_two_slot.sv) 里，从下游 `ready_in` 出发往回追：`ready_in` → `eb_main.ready_in` → `eb_main.ready_out = ready_from_main_eb` → `ready_main_buf`（寄存器）→ `ready_to_aux_eb` → `eb_aux.ready_in` → `eb_aux.ready_out = ready_from_aux_eb` → `ready_out`（对外）。
2. 标出这条链上**唯一的寄存器**：`ready_main_buf`。
3. 对比 `eb_one_slot`（PEB）的回压链：`ready_in` → `write_en` → `ready_out`，**全程组合**，没有任何寄存器。

需要观察的现象：two-slot 的回压链被 `ready_main_buf` 切成两段，每段都只有一个 `eb_one_slot` 内部的组合深度；one-slot 则是一整条。

预期结果：two-slot 的回压路径组合深度更浅 → 对主频更友好；但多了一个槽、一个输入 MUX、一个寄存器，面积和功耗更大。

> 是否真能提升该路径的最高频率，需结合综合工具（如 Design Compiler / Genus）的时序报告确认——**待本地验证**。

#### 4.3.5 小练习与答案

**Q1**：`ready_main_buf` 复位值为什么是 1 而不是 0？

**答**：见 [L81](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/eb_two_slot.sv#L81)。复位时 main 还没满（`full_r=0`），`ready_from_main_eb` 本应为 1；让 `ready_main_buf` 复位为 1 与之一致，保证上电瞬间 aux 就能向上游报告「可收」，避免复位后第一个周期的吞吐损失。

**Q2**：`eb_two_slot` 内部为什么用「两个 `eb_one_slot`」而不是重新写一套逻辑？

**答**：复用已经验证过的 `eb_one_slot`，降低设计风险；two-slot 的全部「新意」只在于那个 `ready_main_buf` 寄存器和输入 2:1 MUX，其余行为（满吞吐、门控）直接继承 one-slot。

**Q3**：注释说 two-slot 在停顿时会「shift 数据」，结合 [L88-89](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/eb_two_slot.sv#L88-L89) 的 MUX，试着解释这个现象。

**答**：正常时 aux 能收，数据经 MUX 直接进 main，aux 空；一旦 main 暂时不能收（`ready_main_buf` 变 0），aux 这一拍仍能收（把上游数据存进 aux 的 `data_r`），之后当 `ready_from_aux_eb=0` 时 MUX 选 `data_from_aux_eb`，aux 里暂存的数据被「搬」进 main——这就是注释说的「shift」。

---

### 4.4 FIFO 后端 fifo_duth：深度可调的循环缓冲

#### 4.4.1 概念说明

当需要**深度大于 2** 的缓冲（例如吸收一次访存突发、平滑较大的速率抖动）时，单槽/双槽都不够，`BUFF_TYPE=3` 选用 `fifo_duth`——一个深度由 `DEPTH` 参数设定的**循环缓冲 FIFO**。

它的接口和前两者不同，是 `push/pop` 形式（`eb_buff_generic` 会做转换，见 4.1.3）。内部用：

- 一个存储数组 `mem[DEPTH][DW]`；
- 两个 **one-hot 编码**的指针 `push_pnt`、`pop_pnt`（用循环左移推进）；
- 一个 **one-hot 风格**的 `status_cnt[DEPTH+1]` 表示当前占用了几个槽。

文件头有一个重要警告：

> [rtl/shared/fifo_duth.sv:6-10](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/fifo_duth.sv#L6-L10) —— 「`DEPTH=1` 会导致 50% 吞吐，那种情况请改用 `eb_one_slot` 并断言 `FULL_THROUGHPUT`」。

#### 4.4.2 核心流程

```text
push（valid_i & ready_o）：把 push_data 写入 push_pnt 指向的槽，push_pnt 循环左移 1 位
pop （valid_o & ready_i）：从 pop_pnt 指向的槽读出（经 and_or_mux 选通），pop_pnt 循环左移 1 位

status_cnt（onehot 编码的占用计数）：
  push & ~pop  ⇒ 左移（占用 +1）
  ~push &  pop ⇒ 右移（占用 -1）

对外：
  valid = ~status_cnt[0]   // status_cnt[0]==1 表示「空」
  ready = ~status_cnt[DEPTH] // status_cnt[DEPTH]==1 表示「满」
```

为什么 one-hot 指针？因为指针每次只移一位，地址译码天然是 one-hot 的，写端口直接用 `push & push_pnt[i]` 选择目标槽（[L77-83](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/fifo_duth.sv#L77-L83)），读端口用 `and_or_mux` 做与或树选择（[L85-95](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/fifo_duth.sv#L85-L95)），避免了二进制指针的加法器与译码器。

`status_cnt` 用 onehot 编码也很巧妙：复位为 `1`（只有 bit0=1，表示空）；每 push 一次左移一位（bit0→bit1→…），bit k=1 就代表「恰好占用 k 个槽」；bit0 是「空」标志，bit DEPTH 是「满」标志。所以 `valid=~status_cnt[0]`、`ready=~status_cnt[DEPTH]`。

#### 4.4.3 源码精读

**存储与状态**：

- [rtl/shared/fifo_duth.sv:30-36](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/fifo_duth.sv#L30-L36) —— `mem`、`push_pnt`、`pop_pnt`、`status_cnt` 声明，以及 `valid`/`ready` 的 onehot 译码。

**指针推进**（one-hot 循环左移）：

- [rtl/shared/fifo_duth.sv:39-58](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/fifo_duth.sv#L39-L58) —— `push` 时 `push_pnt <= {push_pnt[DEPTH-2:0], push_pnt[DEPTH-1]}`，`pop` 同理。

**占用计数**：

- [rtl/shared/fifo_duth.sv:61-73](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/fifo_duth.sv#L61-L73) —— push-pop 决定左移/右移/保持；复位 `status_cnt<=1`（空）。

**写端口译码 + 读端口选择**：

- [rtl/shared/fifo_duth.sv:77-83](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/fifo_duth.sv#L77-L83) —— `for` 循环 + `push & push_pnt[i]` 选槽写入（one-hot 译码）。
- [rtl/shared/fifo_duth.sv:85-95](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/fifo_duth.sv#L85-L95) —— `and_or_mux` 用 `pop_pnt` 选通输出。`and_or_mux` 本体是一个 one-hot 与或树：[rtl/shared/and_or_mux.sv:31-39](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/and_or_mux.sv#L31-L39)。

**断言保护**：

- [rtl/shared/fifo_duth.sv:97-98](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/fifo_duth.sv#L97-L98) —— 「满时 push」或「空时 pop」直接 `$fatal`，防止误用。

#### 4.4.4 代码实践

> **实践目标**：理解 `fifo_duth` 的 `DEPTH` 如何影响「能吸收多大抖动」。

操作步骤：

1. 假设上游连续 push 4 个数据，而下游前 3 拍都不 ready（`pop=0`）。
2. 追踪 `status_cnt`：复位 `=1`（bit0），第 1 次 push 后 bit1=1，第 2 次 bit2=1，第 3 次 bit3=1，第 4 次 bit4=1。
3. 若 `DEPTH=4`，则 bit4 即 `status_cnt[DEPTH]`，`ready=~status_cnt[4]=0`——第 4 次恰好把 FIFO 写满，第 5 次上游必须被反压。
4. 比较若 `DEPTH=8`：同样 4 次 push 不会写满，上游可继续 push，缓冲吸收了这次「下游 3 拍停顿」的抖动。

需要观察的现象：`DEPTH` 越大，越能吸收下游的突发停顿，但面积（`mem` 大小、指针宽度）线性增长。

预期结果：FIFO 的吞吐在 `DEPTH≥2` 时可达 100%（一旦预热完成），延迟约为 `DEPTH` 级；它适合做「平滑突发」的深缓冲，而不适合做「切断组合路径」的流水线寄存器（输出要过 `and_or_mux`，反而比 two-slot 的直通输出慢）。

> 在 `vector_top` 里把某个流水线寄存器换成 `BUFF_TYPE=3, DEPTH=4` 是否合理，可结合面积/时序报告判断——**待本地验证**。

#### 4.4.5 小练习与答案

**Q1**：为什么 `status_cnt` 用 onehot 编码而不是普通二进制计数？

**答**：onehot 下，「空」「满」的判定简化为某一位是否为 1（`status_cnt[0]` 空、`status_cnt[DEPTH]` 满），无需比较器；且增减只是循环移位，无加法器，时序和面积都友好。

**Q2**：`fifo_duth` 的 `DEPTH=1` 为什么是 50% 吞吐？

**答**：`DEPTH=1` 时，存储只有 1 个槽，写入后必须等它被 pop 出去才能再写——「写」和「读」无法像 PEB 那样同拍重叠，于是至多每两拍完成一次传输，即 50%。注释因此建议 `DEPTH=1` 时改用 `eb_one_slot(FULL_THROUGHPUT=1)`。

**Q3**：`and_or_mux` 要求 `sel` 是 at-most-one-hot。`fifo_duth` 里是谁保证这一点的？

**答**：`pop_pnt` 本身就是 one-hot 编码（复位为 1，每次 pop 循环左移 1 位），任何时刻只有一位为 1，天然满足 `and_or_mux` 的 one-hot 选择要求。

---

## 5. 综合实践

把全讲内容串起来，完成下面这个**对比 + 决策**任务。

**任务背景**：[rtl/vector/vector_top.sv:84-101](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L84-L101) 的 `vRR_vIS` 缓冲当前是 `BUFF_TYPE=1`（单槽满吞吐）。请回答两个问题并做一次「纸面改造」。

**第一问（解释选型）**：为什么 `vRR_vIS` 选 `BUFF_TYPE=1`，而不是 0、2 或 3？请用本讲学过的「吞吐 / 组合回压路径 / 面积 / 延迟」四维分析，逐个排除：

- 为什么不是 0（HBEB）？——吞吐不足，且 `write_en=~full_r` 实现下置满后无法正常排空（见 4.2.2），不能做流水线寄存器。
- 为什么不是 3（FIFO）？——深度缓冲不必要（相邻级之间不需要吸收突发），且输出要过 `and_or_mux`，反而比直通输出更慢、更费面积。
- 为什么不是 2（two-slot）？——这是**最有力**的候选，同样满吞吐、且无组合回压路径。设计者选 1 而非 2，是一次**「赌这段组合路径不在关键路径上」**的取舍：用 1 个槽、最低延迟、最小面积，换取「接受 vIS 的 `i_ready` 组合地影响 vRRM」这一风险。

**第二问（纸面改造）**：把 `vRR_vIS` 的 `BUFF_TYPE` 从 `1` 改成 `2`，列出会发生变化的四个方面：

| 维度 | BUFF_TYPE=1（现状） | BUFF_TYPE=2（改造后） |
|------|------|------|
| 吞吐 | 100% | 100%（不变） |
| 组合回压路径 | 存在（`ready_in→ready_out`） | **被 `ready_main_buf` 切断** |
| 面积 | 1 个数据寄存器 | 2 个数据寄存器 + 输入 2:1 MUX + `ready_main_buf` |
| 延迟/功耗 | 最低延迟 | aux 在停顿时会 shift 数据，功耗略高 |

**结论（取舍理由）**：one-slot（PEB）赢在**最小面积、最小延迟**，输在**组合回压路径**；two-slot 赢在**无组合回压、主频更友好**，输在**面积/功耗**。两者吞吐相同，接口相同（`eb_buff_generic` 包装），所以这是一道纯粹的**时序 vs 面积**的取舍题。`vector_top` 当前全用 1，意味着设计者判断这些级间路径**不是时序瓶颈**；一旦综合发现某条 `ready` 路径成了关键路径，**把那一处单独改成 `BUFF_TYPE=2` 即可**，无需动其它连线——这正是「选择器模式」的最大价值。

> 若条件允许：在 QuestaSim 里分别用 `BUFF_TYPE=1` 和 `2` 跑同一个示例（如 vvadd），对比 `results.log` 的 `total_cycles` 应当一致（功能等价）；再查阅综合时序报告确认关键路径是否变化——**待本地验证**。

---

## 6. 本讲小结

- `eb_buff_generic` 是一个**选择器**，用 `BUFF_TYPE`（0/1/2/3）在 `eb_one_slot` / `eb_two_slot` / `fifo_duth` 三种后端里选一种，对外统一 ready/valid 接口；调用方改一个参数即可换策略。
- `eb_one_slot` 的全部行为由 `write_en` 驱动：`FULL_THROUGHPUT=1`（PEB）时 `write_en = ready_in | ~full_r`，靠那个 `ready_in` 实现「同拍进出」的 100% 吞吐，代价是 `ready_out` 组合依赖 `ready_in` 的**回压路径**。
- `BUFF_TYPE=0`（HBEB）在本仓库**从未使用**：它的 `write_en=~full_r` 实现导致置满后无法正常排空，只作为类型学对照存在。
- `eb_two_slot` 用「多一个槽 + 一个寄存的 `ready_main_buf` + 输入 2:1 MUX」，在保持 100% 吞吐的同时**切断组合回压路径**，是 PEB 的「时序升级版」。
- `fifo_duth` 是深度可调的 one-hot 循环缓冲 FIFO，靠 `status_cnt` 的 onehot 编码判空/判满，用 `and_or_mux` 选通输出；适合深缓冲/平滑突发，不适合做轻量流水线寄存器（`DEPTH=1` 还会退化到 50% 吞吐）。
- `vector_top` 的 4 处级间缓冲全部 `BUFF_TYPE=1`，是一次「最小面积/最低延迟」的取舍：赌这些 `ready` 组合路径不在关键路径上；真出问题时，把单处改成 `BUFF_TYPE=2` 即可，接口不变。

---

## 7. 下一步学习建议

- **横向承接**：回到 [rtl/vector/vector_top.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv)，重新审视那 4 个缓冲的 `ready_o/ready_i` 各自连到了哪个子模块的冒险/反压逻辑——你会发现 vIS 的 `i_ready` 正是计分板冒险检测的输出，这条组合回压路径的「深度」由 vIS 决定。
- **纵向深入**：下一讲 **u2-l3 vRRM 寄存器重映射** 将进入 vRRM 内部，看它如何在上游被这些缓冲反压的同时，完成物理寄存器分配与 ticket 发放。
- **后续呼应**：`fifo_duth` 与 `and_or_mux` 这些「积木」在 **u4-l3 存储子系统**（`ld_st_buffer` / `wait_buffer` / `data_cache`）里会再次出现，届时你会更理解为什么那里需要**深缓冲**（吸收 miss），而流水线寄存器只需要**单槽满吞吐**。
- **拓展阅读**：若对弹性缓冲的理论感兴趣，可搜索关键词 *skid buffer*、*pipelined valid-ready handshake*、*HBEB / PEB*，对照本讲源码理解工业界对「吞吐 vs 时序」的标准权衡手法。
