# 主控状态机 systolic_controll

## 1. 本讲目标

本讲精读 TPU 的「总指挥」—— [rtl/systolic_controll.v](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v)。

在 [u1-l3](u3-l1-controller-fsm.md) 里我们已经看到，`tpu_top` 把五个子模块连成一条流水线，而所有「何时读、何时算、何时写、何时结束」的命令都来自同一个模块：`systolic_controll`。学完本讲，你应当能够：

- 画出 `IDLE → LOAD_DATA → WAIT1 → ROLLING` 四状态机的转移图，并说出每个状态的职责与转移条件。
- 解释 `cycle_num`、`matrix_index`、`data_set` 三个计数器在 `ROLLING` 状态里的递增与回绕规则。
- 看懂「三段式」写法：一个时序 `always` 块负责寄存器更新，三个组合 `always@(*)` 块分别产出「状态/addr_sel/系统控制」三类输出。
- 说出 `sram_write_enable` 何时为 1、`tpu_done` 何时拉高，并能与 [u2-l2](u2-l2-mac-accumulate.md) 里 `FIRST_OUT = ARRAY_SIZE+1 = 9` 的结论互相印证。

本讲是 [u3 单元](u3-l2-addr-sel-skew.md) 的入口：控制器发出的 `addr_serial_num` 交给 [u3-l2 addr_sel](u3-l2-addr-sel-skew.md) 解码地址，发出的 `matrix_index` / `data_set` / `sram_write_enable` 交给 [u3-l3 write_out](u3-l3-write-out.md) 写回结果。

## 2. 前置知识

### 2.1 为什么要一个「控制器」

脉动阵列本体（`systolic`）只会机械地「移位 + 乘加」，它不知道：

- 第几个周期才开始把外部 SRAM 的数据喂进来；
- 算到第几个周期，第一组有效结果才出现、可以开始写回；
- 一批结果要写多少个 `matrix_index`，写完之后要不要换一组输出 SRAM；
- 全部算完后，如何告诉外部「我做完了」。

这些「节拍」问题必须由一个集中式状态机来统一下发命令，否则各个子模块各跑各的，数据对不齐。`systolic_controll` 就是这个节拍器。

### 2.2 三段式状态机写法回顾

数字电路里常见的 FSM 写法有「一段式 / 二段式 / 三段式」。本项目采用接近三段式的风格：

- **时序块**（`always @(posedge clk)`）：只做寄存器更新，把上一拍算好的 `_nx`（next）值打进寄存器。
- **组合块**（`always @(*)`）：根据当前状态和输入，算出下一拍的状态和输出。

本项目里更细一点：状态转移、`addr_serial_num`、系统控制信号被拆成了**三个独立的 `always @(*)` 块**，各自专注一类输出。这样做的好处是可读性强、便于分别理解和修改。

### 2.3 关键前置结论（来自前几讲）

- `ARRAY_SIZE = 8`，阵列是 8×8（见 [u1-l4](u1-l4-parameterization-fixedpoint.md)）。
- 阵列里一个 cell 的累加「首入节拍」由反对角线编号 `s = i + j` 决定，**第一个有效输出在 `cycle_num = ARRAY_SIZE+1 = 9` 出现**（见 [u2-l2 的 `FIRST_OUT`](u2-l2-mac-accumulate.md)）。本讲你会看到控制器恰好用 `cycle_num >= ARRAY_SIZE+1` 作为「开始写回」的门控，这正是同一件事的另一面。
- `matrix_index` 是「一生产者两消费者」信号：由控制器产生，同时驱动 `systolic`（挑结果）和 `write_out`（写回），保证「算出的」与「写出的」属于同一批（见 [u2-l3](u2-l3-output-gather.md)）。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件：

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [rtl/systolic_controll.v](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v) | TPU 主控状态机 | 全部内容 |

为了说清「控制器下发的命令分别给谁用」，还会引用两处连接关系：

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [rtl/tpu_top.v](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v) | 顶层例化 | `systolic_controll` 例化与端口连线（[L120-L137](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L120-L137)） |
| [rtl/addr_sel.v](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/addr_sel.v) | 地址解码 | 消费 `addr_serial_num` 产生四路 SRAM 读地址（[u3-l2](u3-l2-addr-sel-skew.md) 详讲） |

## 4. 核心概念与源码讲解

本讲把控制器拆成四个最小模块：

1. **4.1 端口与三段式骨架** —— 控制器对外发哪些命令、内部怎么组织。
2. **4.2 状态定义与状态转移** —— 四状态机骨架（最小模块①）。
3. **4.3 addr_serial_num 的递增与流水预启** ——（最小模块②）。
4. **4.4 ROLLING 中的计数器更新** —— `cycle_num` / `matrix_index` / `data_set` 的递增与回绕（最小模块③）。

### 4.1 端口与三段式骨架

#### 4.1.1 概念说明

控制器是一个**单输入、多命令输出**的模块：

- 输入只有三个：`clk`、`srstn`（同步复位低有效）、`tpu_start`（启动脉冲）。
- 输出是一组「命令总线」，分别下发给 `addr_sel`、`systolic`、`write_out`，外加一个 `tpu_done` 完成脉冲。

它内部用「时序块 + 三个组合块」的四块结构来组织这些输出。

#### 4.1.2 核心流程

```text
         clk, srstn, tpu_start
                 │
        ┌────────▼────────┐
        │  时序 always     │  把 _nx 打进寄存器（state/cycle_num/...）
        │  (posedge clk)   │
        └────────┬────────┘
                 │  当前寄存器值
       ┌─────────┼─────────┬──────────────┐
       ▼         ▼         ▼              ▼
  ┌─────────┐ ┌────────┐ ┌───────────┐ ┌──────────┐
  │状态转移 │ │addr_sel│ │系统控制    │ │(时序块)  │
  │always@* │ │always@*│ │always@*   │ │寄存更新  │
  └────┬────┘ └───┬────┘ └─────┬─────┘ └────┬─────┘
       │          │            │            │
   state_nx   addr_serial_  alu_start,     各寄存器
   tpu_done_  num_nx        cycle_num_nx,  _nx←组合值
   nx                       matrix_index_nx,
                            data_set_nx,
                            sram_write_enable
```

四个 `always` 块各司其职：

| 块 | 类型 | 产出 | 对应源码 |
|---|---|---|---|
| 初始化块 | 时序 `posedge clk` | 寄存器更新 + 复位 | [rtl/systolic_controll.v:43-60](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v#L43-L60) |
| 状态转移块 | 组合 `@(*)` | `state_nx`、`tpu_done_nx` | [rtl/systolic_controll.v:63-99](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v#L63-L99) |
| addr_sel 块 | 组合 `@(*)` | `addr_serial_num_nx` | [rtl/systolic_controll.v:102-127](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v#L102-L127) |
| 系统控制块 | 组合 `@(*)` | `alu_start`、`cycle_num_nx`、`matrix_index_nx`、`data_set_nx`、`sram_write_enable` | [rtl/systolic_controll.v:131-187](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v#L131-L187) |

#### 4.1.3 源码精读

端口声明——注意每个输出注释里写明它「发给谁」：

[rtl/systolic_controll.v:6-23](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v#L6-L23) 这段声明了控制器的全部端口：`tpu_start` 是唯一的启动输入；`sram_write_enable` 给 `write_out`；`addr_serial_num[6:0]` 给 `addr_sel`；`alu_start` / `cycle_num[8:0]` / `matrix_index[5:0]` 给 `systolic`；`data_set[1:0]` 和 `matrix_index` 一起给 `write_out`；`tpu_done` 是完成脉冲。注意位宽：`cycle_num` 是 9 位（实际只用到 ~41），`matrix_index` 是 6 位（用到 0..15），`data_set` 是 2 位（用到 0..1）。

时序块——只做寄存器更新：

[rtl/systolic_controll.v:43-60](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v#L43-L60) 复位（`~srstn`）时把所有寄存器清 0、状态置 `IDLE`；否则在每个上升沿把对应的 `_nx` 值打进去。`alu_start` 和 `sram_write_enable` 没有出现在这里——它们是纯组合输出（见 4.4.3）。

> **「一生产者多消费者」的实物对照**：在 [rtl/tpu_top.v:120-137](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L120-L137) 的例化里，`matrix_index` 同时连到 `systolic`（[L133](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L133)）和 `write_out`（[L150](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L150)），这就是 [u2-l3](u2-l3-output-gather.md) 强调的「算出的与写出的属同一批」在顶层连线上的体现。

#### 4.1.4 代码实践

**实践目标**：建立「命令总线 → 消费者」的映射表，确认控制器是唯一的命令源。

**操作步骤**：

1. 打开 [rtl/systolic_controll.v:6-23](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v#L6-L23)，逐行读注释。
2. 打开 [rtl/tpu_top.v:120-137](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L120-L137)，看每个输出连到了哪个子模块。

**需要观察的现象**：控制器的 7 个输出（`sram_write_enable`、`addr_serial_num`、`alu_start`、`cycle_num`、`matrix_index`、`data_set`、`tpu_done`）分别流向 `addr_sel` / `systolic` / `write_out` / 顶层 `tpu_done`。

**预期结果**：你会得到一张「命令 → 消费者」表，其中 `matrix_index` 是唯一连到两个消费者的信号。

#### 4.1.5 小练习与答案

**练习 1**：`cycle_num` 声明为 9 位，但实际一次运行最多跑到约 41。为什么留这么多冗余位？

> **答案**：留足余量是为了在不同 `ARRAY_SIZE` 或未来扩展（更多 `matrix_index` / `data_set`）下不至于溢出；同时也避免综合后高位被优化掉造成隐患。9 位可表示 0..511，远大于 41。

**练习 2**：`alu_start` 和 `sram_write_enable` 为什么不出现在时序块（L43-L60）里？

> **答案**：它们在组合块里被直接赋值（不经过 `_nx`），属于「取决于当前状态/计数器的组合输出」。由于它们依赖的 `state`、`cycle_num` 都是寄存器，在两个时钟沿之间是稳定的，所以效果上等同于寄存过的电平。

---

### 4.2 状态定义与状态转移

#### 4.2.1 概念说明

控制器用四个状态描述一次完整的矩阵乘任务的生命周期：

| 状态 | 编码 | 职责 |
|---|---|---|
| `IDLE` | `3'd0` | 空闲，等待 `tpu_start` |
| `LOAD_DATA` | `3'd1` | 启动后第 1 拍，开始预启地址流水 |
| `WAIT1` | `3'd2` | 启动后第 2 拍，继续预启（补偿 SRAM 读延迟） |
| `ROLLING` | `3'd3` | 主运算阶段：边读、边算、边写，直到全部写完 |

`LOAD_DATA` 和 `WAIT1` 合在一起是为地址流水「提前两拍」铺路（详见 4.3）。

#### 4.2.2 核心流程

状态转移图：

```text
              tpu_start==1
   ┌──────────────────────────┐
   │                          ▼
 ┌──────┐    tpu_start==1   ┌──────────┐      ┌───────┐      ┌─────────┐
 │ IDLE │ ───────────────▶ │ LOAD_DATA │ ───▶ │ WAIT1 │ ───▶ │ ROLLING │
 └──────┘                  └──────────┘      └───────┘      └─────────┘
   ▲                                                          │
   │            matrix_index==15 && data_set==1               │
   └──────────────────────────── tpu_done=1 ──────────────────┘
                                  (回 IDLE)
```

唯一的两条「条件边」：

- `IDLE → LOAD_DATA`：当且仅当 `tpu_start == 1`。
- `ROLLING → IDLE`：当且仅当 `matrix_index == 15 && data_set == 1`（此时同时置 `tpu_done = 1`）。

其余都是无条件单拍转移：`LOAD_DATA → WAIT1 → ROLLING`。

#### 4.2.3 源码精读

状态编码用 `localparam` 定义（注意只用了 0..3，第 4、5 个编码未定义）：

[rtl/systolic_controll.v:25](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v#L25) 定义了四个状态的 3 位编码。`state` 和 `state_nx` 都是 `[2:0]`（见 [L28-L29](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v#L28-L29)）。

状态转移 + `tpu_done` 的组合块：

[rtl/systolic_controll.v:63-99](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v#L63-L99) 这是状态机的核心。重点看两段：

- [L65-L71](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v#L65-L71) `IDLE`：`tpu_start` 为 1 才进 `LOAD_DATA`，否则保持 `IDLE`；任何情况下 `tpu_done_nx = 0`。
- [L83-L92](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v#L83-L92) `ROLLING`：只有当 `matrix_index==15 && data_set==1` 时才回 `IDLE` 并把 `tpu_done_nx = 1`，否则留在 `ROLLING`。

> **`tpu_done` 是单拍脉冲**：由于回 `IDLE` 后下一拍 `IDLE` 分支会把 `tpu_done_nx` 设回 0，`tpu_done` 只会高正好一个时钟周期。testbench 正是靠捕捉这个上升沿来判断「做完了」（见 [u4-l2](u4-l2-golden-verify.md) 中 `while(~tpu_finish)` 循环）。

#### 4.2.4 代码实践

**实践目标**：亲手把状态转移图与源码一一对应。

**操作步骤**：

1. 阅读 [rtl/systolic_controll.v:63-99](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v#L63-L99)。
2. 对每个状态，写下「下一状态」和「触发条件」两列。
3. 用工具（纸笔 / draw.io / Mermaid）画出转移图。

**需要观察的现象**：`LOAD_DATA` 和 `WAIT1` 是无条件直通的「过渡态」，它们存在的意义不在「等待某个条件」，而在「让地址流水提前两拍启动」（见 4.3）。

**预期结果**：得到一张四节点、五条边（含 `IDLE` 自环、`ROLLING` 自环）的状态图，且只有两条边带条件。

#### 4.2.5 小练习与答案

**练习 1**：如果删掉 `WAIT1` 状态（让 `LOAD_DATA` 直接到 `ROLLING`），地址流水会少几拍预启？对结果有什么影响？

> **答案**：会少 1 拍预启。地址提前量从 2 拍降到 1 拍，可能导致 `cycle_num=0` 时阵列还没收到对应的 SRAM 读数据，前几个 `matrix_index` 的结果错位。这正是 `WAIT1` 存在的理由。

**练习 2**：`default` 分支把 `state_nx` 设为 `IDLE`（[L94-L97](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v#L94-L97)），有什么作用？

> **答案**：`state` 是 3 位，但只定义了 0..3 四个值。理论上电路上电时 `state` 可能是任意值；`default` 保证一旦进入未定义状态会安全回到 `IDLE`，是一种防御性设计，也避免组合块推断锁存器。

---

### 4.3 addr_serial_num 的递增与流水预启

#### 4.3.1 概念说明

`addr_serial_num` 是控制器发给 `addr_sel` 的「地址串行号」——一个单调递增的 7 位计数器。`addr_sel` 再把它解码成四路 SRAM 读地址（详见 [u3-l2](u3-l2-addr-sel-skew.md)）。

这里的关键不是「怎么递增」，而是「**提前两拍开始递增**」：在 `LOAD_DATA` 和 `WAIT1` 这两个过渡态里，`addr_serial_num` 就已经被分别设成 1 和 2，等进入 `ROLLING` 时它已经是 2，比 `cycle_num`（此时为 0）领先两拍。这两拍的提前量，是用来补偿 SRAM 读数据固有的延迟——现在发出的地址，要过两拍数据才到达阵列。

#### 4.3.2 核心流程

`addr_serial_num` 在各状态的取值（组合块算 `_nx`，下一拍生效）：

| 当前状态 | `addr_serial_num_nx` | 说明 |
|---|---|---|
| `IDLE`（`tpu_start==1`） | `0` | 启动瞬间清零 |
| `IDLE`（其它） | 保持 | 空闲不动 |
| `LOAD_DATA` | `1` | 预启 +1 |
| `WAIT1` | `2` | 预启 +1 |
| `ROLLING`（< 127） | `当前 + 1` | 与 `cycle_num` 同步递增 |
| `ROLLING`（== 127） | 保持 | 到顶封顶，防止溢出 |

进入 `ROLLING` 后，`addr_serial_num` 与 `cycle_num` **每拍各加 1**，但 `addr_serial_num` 始终领先 `cycle_num` 正好 2（因为预启了 1、2 两拍）。数学上：

\[
\text{addr\_serial\_num} = \text{cycle\_num} + 2 \quad (\text{在 ROLLING 期间})
\]

#### 4.3.3 源码精读

[rtl/systolic_controll.v:102-127](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v#L102-L127) 是 `addr_serial_num` 的组合块。重点三处：

- [L104-L109](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v#L104-L109) `IDLE`：`tpu_start` 一来就清 0。
- [L111-L115](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v#L111-L115) `LOAD_DATA` / `WAIT1`：硬编码为 1、2，这就是「提前两拍预启」。
- [L117-L122](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v#L117-L122) `ROLLING`：未到 127 则 `+1`，到 127 则封顶保持。

> **为什么封顶在 127**：`addr_serial_num` 是 7 位，最大 127。`addr_sel` 里也把 `addr_serial_num > 98`（或对应区间外）的地址统一映射成 127（产生「空数据」，见 [rtl/addr_sel.v:33-37](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/addr_sel.v#L33-L37)）。实际一次运行 `addr_serial_num` 最高约 42，远不会到 127，这个封顶只是安全护栏。

#### 4.3.4 代码实践

**实践目标**：验证 `addr_serial_num` 比 `cycle_num` 领先 2 拍。

**操作步骤**：

1. 准备一张表，列出从 `tpu_start` 拉高后连续若干拍的 `state`、`cycle_num`、`addr_serial_num`（取寄存器更新后的值）。
2. 从 [L43-L60](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v#L43-L60)（时序块）和 [L102-L127](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v#L102-L127)、[L131-L187](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v#L131-L187) 推算每拍的下一值。

**需要观察的现象**：进入 `ROLLING` 的第一拍，`cycle_num=0` 而 `addr_serial_num=2`；此后每拍两者同步 +1，差值恒为 2。

**预期结果**：得到类似下表（节选）：

| 拍号 | state | cycle_num | addr_serial_num |
|---|---|---|---|
| 进入 ROLLING | ROLLING | 0 | 2 |
| +1 | ROLLING | 1 | 3 |
| +2 | ROLLING | 2 | 4 |

（差值恒为 2。）若仿真与此不符，请检查你是否把组合 `_nx` 与寄存后的值混淆了。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `addr_serial_num` 要比 `cycle_num` 提前，而不是同步？

> **答案**：因为 SRAM 从给出地址到返回数据有固有延迟（本项目里是 2 拍，由 `addr_sel` 输出端的 flip-flop 与 SRAM 模型共同决定）。要让 `cycle_num=k` 时阵列正好用到「第 k 组数据」，就必须提前 2 拍发出对应地址。

**练习 2**：把 `LOAD_DATA` 里的 `addr_serial_num_nx = 1` 改成 `= 0`，会发生什么？

> **答案**：预启量从 2 拍降到 1 拍，所有读数据整体晚 1 拍到达阵列，导致 `cycle_num` 与数据错位、`matrix_index` 对应的结果串位，最终 testbench 比对失败（待本地验证具体错位表现）。

---

### 4.4 ROLLING 中的计数器更新

#### 4.4.1 概念说明

`ROLLING` 是真正干活的状态，这里有三个相互配合的计数器：

- **`cycle_num`**：节拍计数器，每拍 +1，驱动 `systolic` 的乘加时序。它决定「现在该算第几拍」。
- **`matrix_index`**：输出批索引，决定「现在该写回第几组结果」。它同时喂给 `systolic`（挑哪 8 个结果）和 `write_out`（写到哪里）。
- **`data_set`**：数据集切换，决定「现在写哪一组输出 SRAM（a / b / c 中的哪一组）」。

三者不是独立递增，而是**嵌套**关系：`cycle_num` 每拍 +1；当 `cycle_num` 跨过 `ARRAY_SIZE+1 = 9` 后，每拍同时推进一个 `matrix_index`；`matrix_index` 计满 15 后回 0 并让 `data_set` +1；`data_set` 到 1 且 `matrix_index` 再到 15 时，整个任务结束。

#### 4.4.2 核心流程

`ROLLING` 内部的判断（见 [L157-L176](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v#L157-L176)）分两大分支，由 `cycle_num >= ARRAY_SIZE+1`（即 `cycle_num >= 9`）把关：

```text
进入 ROLLING，每拍：
  cycle_num_nx = cycle_num + 1            // 始终递增
  alu_start    = 1                        // 始终让阵列工作

  if (cycle_num >= ARRAY_SIZE+1)          // 即 >= 9：已有有效结果
      sram_write_enable = 1               //   → 开始写回
      if (matrix_index == 15)
          matrix_index_nx = 0             //   → 回绕
          data_set_nx     = data_set + 1  //   → 换一组输出 SRAM
      else
          matrix_index_nx = matrix_index + 1
          data_set_nx     = data_set      //   → 保持
  else                                    // cycle_num < 9：还在「灌满」阵列
      sram_write_enable = 0               //   → 不写
      matrix_index_nx   = 0
      data_set_nx       = data_set
```

把这段逻辑展开成时间轴（寄存器每拍更新后的值），就得到本讲最关键的一张表：

| `cycle_num` | `matrix_index` | `data_set` | `sram_write_enable` | 阶段 |
|---|---|---|---|---|
| 0 | 0 | 0 | 0 | 灌满阵列（灌满期） |
| 1..8 | 0 | 0 | 0 | 灌满阵列（灌满期） |
| 9 | 0 | 0 | **1** | 写 data_set=0 的 matrix_index=0 |
| 10 | 1 | 0 | **1** | 写 data_set=0 的 matrix_index=1 |
| … | … | 0 | **1** | … |
| 24 | 15 | 0 | **1** | 写 data_set=0 的 matrix_index=15（写完回绕） |
| 25 | 0 | 1 | **1** | 写 data_set=1 的 matrix_index=0 |
| … | … | 1 | **1** | … |
| 40 | 15 | 1 | **1** | 写 data_set=1 的 matrix_index=15 → `tpu_done` 拉高，回 `IDLE` |

由此可得几个数量关系：

- **灌满期**：`cycle_num` 从 0 到 8，共 9 拍，`sram_write_enable = 0`。
- **写回期**：`cycle_num` 从 9 到 40，共 \(40 - 9 + 1 = 32\) 拍，`sram_write_enable = 1`。
- 写回期恰好是 16（`data_set=0`）+ 16（`data_set=1`）= 32 个 `matrix_index`，每个 `matrix_index` 产出 `ARRAY_SIZE = 8` 个结果，共 \(32 \times 8 = 256\) 个输出元素。
- **`tpu_done` 恰在最后一拍（`cycle_num=40, matrix_index=15, data_set=1`）拉高**，且只高一拍。

> **与 [u2-l2](u2-l2-mac-accumulate.md) 互相印证**：那里推出首个有效输出在 `FIRST_OUT = ARRAY_SIZE+1 = 9` 出现；这里控制器恰好用 `cycle_num >= ARRAY_SIZE+1` 作为写回门控。两边用的是同一个 `ARRAY_SIZE+1`，说明「控制器何时开始写」与「阵列何时算出第一组有效结果」是严格对齐的——这是整个 TPU 能算对的命门之一。

#### 4.4.3 源码精读

`ROLLING` 分支的主体：

[rtl/systolic_controll.v:157-176](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v#L157-L176) 逐行看：

- [L158](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v#L158) `alu_start = 1`：在 `ROLLING` 里阵列始终被使能。
- [L159](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v#L159) `cycle_num_nx = cycle_num + 1`：每拍 +1，无条件。
- [L160](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v#L160) 门控 `if (cycle_num >= ARRAY_SIZE+1)`：注意用的是**当前寄存器值** `cycle_num`，不是 `_nx`。
- [L161-L168](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v#L161-L168) `matrix_index` 的回绕 + `data_set` 递增。
- [L169](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v#L169) `sram_write_enable = 1`：只要过了门控就写。
- [L171-L175](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v#L171-L175) 否则（灌满期）不写、`matrix_index` 保持 0。

> **`sram_write_enable` 与 `alu_start` 是组合输出**：它们在 [L131-L187](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v#L131-L187) 的 `always @(*)` 里被直接赋值，不经过 `_nx` / 时序块。因为它们依赖的 `state`、`cycle_num` 都是寄存器（沿间稳定），所以下游 `write_out` 在时钟沿采样它们时，看到的是稳定的电平。`cycle_num_nx`、`matrix_index_nx`、`data_set_nx` 则走 `_nx` → 时序块的常规寄存路径。

#### 4.4.4 代码实践（本讲核心实践）

**实践目标**：亲手填出上面那张 `cycle_num / matrix_index / data_set / sram_write_enable` 时间轴表，并标注 `tpu_done` 何时出现。

**操作步骤**：

1. 假设 `ARRAY_SIZE = 8`，从 `ROLLING` 第一拍（`cycle_num=0, matrix_index=0, data_set=0`）开始。
2. 对每一拍，套用 [L157-L176](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v#L157-L176) 的规则，算出下一拍的 `cycle_num`、`matrix_index`、`data_set`，以及本拍的 `sram_write_enable`。
3. 同时套用 [L83-L92](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v#L83-L92) 判断本拍是否拉 `tpu_done`。
4. 一直填到状态回到 `IDLE`。

**需要观察的现象**：

- `cycle_num = 0..8` 共 9 拍 `sram_write_enable = 0`（灌满期）。
- 从 `cycle_num = 9` 起 `sram_write_enable = 1`，`matrix_index` 从 0 逐拍 +1。
- `matrix_index` 到 15 后回 0，`data_set` 从 0 变 1。
- `data_set = 1` 且 `matrix_index` 再次到 15 那一拍，`tpu_done = 1`，状态回 `IDLE`。

**预期结果**：得到 4.4.2 的那张表。`tpu_done` 只在最后一拍出现一次。

**仿真对照（可选，待本地验证）**：testbench 在 [Pre-Synthesis_Simulation/test_tpu.v:277-281](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/Pre-Synthesis_Simulation/test_tpu.v#L277-L281) 用 `while(~tpu_finish)` 循环统计 `cycle_cnt`，并在 [L317](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/Pre-Synthesis_Simulation/test_tpu.v#L317) 打印总周期数。你可以运行仿真，把打印出的总周期数与本讲推出的「灌满 9 + 写回 32 = 41 拍 ROLLING」+ 两拍过渡态做对照（因 testbench 计的是 negedge 间周期、且起止点含 `tpu_start` 脉冲对齐，具体整数以仿真输出为准）。

#### 4.4.5 小练习与答案

**练习 1**：把 `ARRAY_SIZE` 改成 4（假设其它写死处也相应改对），灌满期是几拍？写回期总长（单个 `data_set`）是几拍？

> **答案**：门控阈值变成 `ARRAY_SIZE+1 = 5`，所以灌满期是 `cycle_num = 0..4` 共 5 拍；写回期单个 `data_set` 仍是 16 个 `matrix_index`（`matrix_index` 仍计到 15，与 `ARRAY_SIZE` 无关），所以单个 `data_set` 写回 16 拍。注意：`matrix_index` 的 0..15 范围在本模块里是写死的，不随 `ARRAY_SIZE` 变。

**练习 2**：`sram_write_enable` 第一次变 1 时，`matrix_index` 的值是多少？此时写回的是「第几组结果」？

> **答案**：第一次 `sram_write_enable = 1` 发生在 `cycle_num = 9` 那拍，此时寄存器里的 `matrix_index` 仍是 0（要等本拍结束的上升沿才变成 1）。所以写回的是 `data_set=0`、`matrix_index=0` 这组结果——即阵列算出的第一组 8 个有效元素。

**练习 3**：如果删掉 [L160](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v#L160) 的门控（让 `sram_write_enable` 一进 `ROLLING` 就为 1），前 9 拍会写出什么？

> **答案**：前 9 拍阵列还没产出有效结果（首个有效结果在 `cycle_num=9` 才出现），此时 `quantized_data` 是无意义的中间值，会把垃圾数据写进输出 SRAM，导致 testbench 比对失败。这就是门控存在的理由。

---

## 5. 综合实践

把本讲全部内容串起来，完成下面这个端到端的小任务。

### 任务：画出完整状态图 + 标注写回与完成时序

1. **画状态转移图**：画出 `IDLE / LOAD_DATA / WAIT1 / ROLLING` 四状态，标出全部转移边和条件（含两条自环：`IDLE` 的 `tpu_start==0`、`ROLLING` 的非结束条件）。

2. **画一张「ROLLING 时序总表」**：横轴是 `cycle_num`（0 到 40），纵轴有四行：`matrix_index`、`data_set`、`sram_write_enable`、`tpu_done`。把 4.4.2 的表完整填出来，并圈出三个关键时刻：
   - 第一次 `sram_write_enable = 1`（`cycle_num = 9`）；
   - `data_set` 从 0 跳到 1 的边（`cycle_num = 24 → 25`）；
   - `tpu_done` 脉冲（`cycle_num = 40`）。

3. **回答两个连线问题**：
   - 控制器发出的 `addr_serial_num` 在 `cycle_num = 0` 时等于几？为什么？（提示：4.3 的预启）
   - 控制器用 `cycle_num >= ARRAY_SIZE+1` 作为写回门控，这与 [u2-l2](u2-l2-mac-accumulate.md) 的哪个常量是一回事？为什么必须一致？

### 进阶（可选）

打开 [Pre-Synthesis_Simulation/test_tpu.v](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/Pre-Synthesis_Simulation/test_tpu.v)，找到 [L269-L281](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/Pre-Synthesis_Simulation/test_tpu.v#L269-L281) 的启动与计数段落，在你熟悉的仿真器（如 Icarus Verilog + GTKWave）里跑一次，把波形里 `state`、`cycle_num`、`matrix_index`、`data_set`、`sram_write_enable`、`tpu_done` 六条信号与你手画的表逐拍对照。**待本地验证**：仿真打印的总周期数（[L317](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/Pre-Synthesis_Simulation/test_tpu.v#L317)）与你推算的 ROLLING 长度是否吻合。

## 6. 本讲小结

- `systolic_controll` 是 TPU 的总指挥：输入只有 `clk / srstn / tpu_start`，输出是一组下发给 `addr_sel`、`systolic`、`write_out` 的命令总线 + 一个 `tpu_done` 脉冲。
- 它是四状态机 `IDLE → LOAD_DATA → WAIT1 → ROLLING`，两条条件边：`tpu_start` 启动、`matrix_index==15 && data_set==1` 结束；`LOAD_DATA` / `WAIT1` 是为地址流水提前两拍铺路。
- 采用「一个时序块 + 三个组合块」的四块结构：时序块只做寄存器更新，三个 `always @(*)` 分别管状态转移、`addr_serial_num`、系统控制信号；`alu_start` / `sram_write_enable` 是纯组合输出。
- `ROLLING` 里三个计数器嵌套递增：`cycle_num` 每拍 +1；过 `cycle_num >= ARRAY_SIZE+1 = 9` 后开始写回，`matrix_index` 每拍 +1；`matrix_index` 计满 15 回 0 并让 `data_set` +1。
- 灌满期 9 拍（`sram_write_enable=0`）+ 写回期 32 拍（`sram_write_enable=1`），共 41 拍 ROLLING；`tpu_done` 仅在最后一拍高一个周期。
- 控制器的写回门控 `cycle_num >= ARRAY_SIZE+1` 与 [u2-l2](u2-l2-mac-accumulate.md) 的 `FIRST_OUT = ARRAY_SIZE+1` 完全一致，保证「写回时机」与「结果就绪时机」严格对齐。

## 7. 下一步学习建议

- **紧接着读 [u3-l2 addr_sel](u3-l2-addr-sel-skew.md)**：看 `addr_serial_num` 如何被解码成四路 SRAM 读地址，以及「提前两拍」如何变成 weight / data 的时间歪斜（skew）。
- **然后读 [u3-l3 write_out](u3-l3-write-out.md)**：看 `sram_write_enable`、`matrix_index`、`data_set` 如何把量化结果按反对角线重排写进 a / b / c 三组输出 SRAM，理解为什么需要三组。
- **回顾 [u2-l2](u2-l2-mac-accumulate.md) / [u2-l3](u2-l3-output-gather.md)**：把「阵列何时算出结果」「控制器何时写回结果」「write_out 写到哪里」三者的时序对齐关系彻底打通。
- **跑一次仿真**：配合 [u4-l1](u4-l1-data2sram-loading.md) / [u4-l2](u4-l2-golden-verify.md)，用 testbench 验证本讲推算的状态时序与总周期数。
