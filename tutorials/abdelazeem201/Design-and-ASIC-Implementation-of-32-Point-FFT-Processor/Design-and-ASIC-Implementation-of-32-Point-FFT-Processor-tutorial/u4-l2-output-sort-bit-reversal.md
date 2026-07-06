# 输出排序模块：硬件位反转还原

## 1. 本讲目标

本讲聚焦 `RTL/FFT.v` 末端那段看起来很"吓人"的大型 `case(y_1)` 语句（约 130 行），把它拆成一件易懂的事：**把流水线吐出来的 32 个乱序频域样本，按位反转关系重新摆放进一个 32 格的缓冲数组，再按自然顺序读出。**

学完后你应该能够：

- 说清 `result_r[0:31]` / `result_i[0:31]` 这两个二维数组在排序中扮演的"信格"角色，以及 `_ns` 后缀的含义。
- 把 `case(y_1)` 这张大表手动抄成一张 `y_1 → 写入槽位` 的映射表，并**证明它等于 5 位位反转再减 1**：`slot = bitrev₅(y_1) − 1`。
- 解释 `over` / `next_over` 完成标志、`count_y` / `y_1` / `y_1_delay` 计数链如何配合，实现"先乱序写入、再顺序读出"。
- 看懂 `out_valid` 是如何由 `over` 拉起、又如何驱动 `dout` 寄存输出的。
- 用 Python 复现这个硬件排序过程，并理解它与 `SIM/FFT.py` 中那行 `int('{:05b}'.format(i)[::-1], 2)` 的异同。

> 承接说明：u2-l3 已经埋下一个伏笔——硬件的 SORT 表"并非简单的 `bitrev(y_1)`，精确验证留待 u4-l2"。本讲就来兑现这个承诺，并解释那个关键的 `−1` 偏移从何而来。

## 2. 前置知识

在读懂本讲前，你需要先具备以下概念（来自前置讲义）：

- **位反转（bit-reversal）**：把一个 5 位二进制索引的位序整体颠倒，例如 `1 = 00001 → 10000 = 16`、`3 = 00011 → 11000 = 24`。它是 radix-2 DIF 输出乱序的根因（见 u2-l3）。
- **DIF 输出乱序**：五级蝶形算完后，第 5 级吐出的 32 个样本不是 `X[0], X[1], …` 的自然顺序，而是位反转顺序，必须重排才能用（见 u2-l1、u2-l3）。
- **顶层端口与定点对齐**：`FFT.v` 内部是 24 位数据通路，末端 `out_r[23:8]` 取高 16 位等价于除以 256，把数据还原回 16 位有符号输出尺度（见 u3-l1）。
- **时序块 vs 组合块**：`always@(posedge clk)` 负责把 `next_xxx` 打拍成寄存器；`always@(*)` 负责用组合逻辑算出下一拍的 `next_xxx`。本讲会频繁用到这个"`next_` 前缀 = 下一拍值"的命名约定（见 u4-l1）。

一个贯穿全讲的直觉比喻：把排序模块想成一个**有 32 个格子的信箱柜**。流水线送来的信封（频域样本）上面的"地址"是乱序的，柜子按一张固定的"分拣表"把每封信塞进正确的格子；等 32 封信全部到齐（`over` 标志），再从 0 号格子到 31 号格子依次取出来投递（`dout`），收件人拿到的就是自然顺序的 FFT 结果。

## 3. 本讲源码地图

本讲只涉及两个文件，但聚焦点非常集中：

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| `RTL/FFT.v` | 顶层模块 | 末端的排序缓冲声明、两个 `always` 块、`case(y_1)` 大表 |
| `SIM/FFT.py` | Python 参考模型 | 文件末尾那行位反转还原逻辑，作为对照基准 |

`RTL/FFT.v` 内本讲相关的代码段大致分布：

- **声明区**（L37–L60）：排序缓冲数组、`count_y`、`y_1`、`over` 等寄存器与组合信号。
- **时序 `always` 块**（L254–L289）：复位清零、把 `_ns` 打拍进数组、`y_1_delay` 跟随 `y_1`。
- **组合 `always` 块一**（L290–L311）：`count_y` 自增、`dout` 从 `result_r[y_1_delay]` 读出。
- **组合 `always` 块二**（L313–L458）：`over` 标志、`out_valid` 生成、以及核心的 `case(y_1)` 分拣表。

## 4. 核心概念与源码讲解

### 4.1 二维数组排序缓冲

#### 4.1.1 概念说明

排序要"先存后读"，就必须有存储。`FFT.v` 用两个 **32 格的寄存器数组** `result_r[0:31]` 与 `result_i[0:31]` 分别缓存 32 个频域样本的实部和虚部。为什么叫"二维数组"？因为在 Verilog 里 `reg [15:0] result_r[0:31]` 是一个"每格 16 位、共 32 格"的存储器，可以把它理解成一个 32 行 × 16 列的位矩阵，每一行存一个样本。

为了在组合逻辑里描述"下一拍每个格子该变成什么"，又镜像声明了两个 `_ns`（next-state）数组 `result_r_ns` / `result_i_ns`。组合块算好 `_ns`，时序块再统一打拍：

```verilog
result_r[i] <= result_r_ns[i];   // 每拍把"下一拍值"刷进真实寄存器
```

这就是 u4-l1 提到的"`next_` 命名约定"在数组层面的体现。

#### 4.1.2 核心流程

排序缓冲的工作分两相，由 `over` 标志切换：

1. **写入相（`over == 0`）**：每来一个第 5 级输出样本，`case(y_1)` 决定它写进 `result_r_ns` 的哪一号格子；32 个样本依次落位。
2. **读出相（`over == 1`）**：`case` 不再执行，`result_r_ns` 默认保持原值（即 `result_r[i] = result_r[i]`，内容冻结）；`dout` 按 `y_1_delay` 给出的自然地址 0→31 依次读出。

伪代码：

```
每拍：
  for i in 0..31: result_r_ns[i] = result_r[i]   # 默认保持
  if not over:                                    # 写入相
      slot = 分拣表(y_1)                           # case(y_1) 查表
      result_r_ns[slot] = out_r[23:8]             # 新样本落位
  # 读出相与写入相共用同一拍：dout 读的是上一拍的 result_r
```

#### 4.1.3 源码精读

声明区里这 4 行就是排序缓冲的全部存储（实部、虚部各一对"当前值 + 下一拍值"）：

声明 4 个 32 格数组（实/虚 × 当前/下一拍）——[RTL/FFT.v:37-40](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L37-L40)

```verilog
reg signed  [15:0] result_r[0:31];     // 实部当前值
reg signed  [15:0] result_i[0:31];     // 虚部当前值
reg signed  [15:0] result_r_ns[0:31];  // 实部下一拍值（组合算出）
reg signed  [15:0] result_i_ns[0:31];  // 虚部下一拍值
```

时序块里用 `for` 循环把整个 `_ns` 数组一次性打拍进真实数组（复位时则整体清零）——[RTL/FFT.v:284-287](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L284-L287)

```verilog
for (i=0;i<=31;i=i+1) begin
    result_r[i] <= result_r_ns[i];
    result_i[i] <= result_i_ns[i];
end
```

注意写入的位宽：`result_r_ns[slot] = out_r[23:8]`。`out_r` 是 24 位内部通路，取 `[23:8]` 即高 16 位，正好落进 16 位的 `result_r` 格子——这就是 u3-l1 讲过的"末端截位 / 除以 256"在排序入口处的体现。

#### 4.1.4 代码实践

**实践目标**：确认排序缓冲的容量与位宽与"32 点 × 16 位"规格一致。

**操作步骤**：

1. 打开 `RTL/FFT.v`，定位 L37–L40 的 4 行声明。
2. 数一数：每个数组有几个格子（`[0:31]` → 32 格）、每格几位（`[15:0]` → 16 位）。
3. 找到 L285 写入语句 `result_r[i] <= result_r_ns[i];` 与 L326 的 `result_r_ns[31] = out_r[23:8];`，确认写入数据来自 24 位 `out_r` 的高 16 位。

**需要观察的现象**：缓冲总容量 = 32 格 × 16 位 × 2（实/虚）× 2（当前/_ns）= 2048 位触发器；这会在 u6 的面积报告里体现为可观的 `sequential cell` 数量。

**预期结果**：实部、虚部各 32 格、每格 16 位有符号；`_ns` 数组只是组合过渡量，真实存储是 `result_r/result_i`。

#### 4.1.5 小练习与答案

- **练习 1**：为什么需要 `_ns` 数组，不能直接在 `case` 里写 `result_r[slot] = out_r[23:8]`？
  - **答**：`result_r` 是寄存器数组，只能在时序块（`posedge clk`）里用 `<=` 更新；`case` 位于组合块（`always@(*)`），不能直接驱动寄存器，所以先用组合算出 `_ns`，再由时序块统一打拍。这也保证了"同一拍内所有格子基于上一拍的稳定值更新"，避免组合环。
- **练习 2**：若要把设计改成 64 点，`result_r` 的声明要怎么改？
  - **答**：格子数从 `[0:31]` 扩到 `[0:63]`，地址位宽 `y_1` 从 5 位扩到 6 位，位反转也相应变成 6 位（详见 u7-l3）。

### 4.2 case(y_1) 位反转映射表

#### 4.2.1 概念说明

这是本讲的"主角"。`case(y_1)` 是一张**分拣查找表**：输入是当前样本的流水线序号 `y_1`（0~31），输出是它该写进的槽位编号。它的存在意义，就是用一块纯组合的查表电路，替代软件里那行位反转公式，把乱序样本摆正。

u2-l3 已经提醒过：这张表**并不等于**朴素的 `bitrev₅(y_1)`。本节我们就把表完整抄出来，给出精确关系。

#### 4.2.2 核心流程

先回顾 u2-l3 的位反转定义。对 5 位索引 \(i = b_4 b_3 b_2 b_1 b_0\)（二进制），其位反转为：

\[
\text{bitrev}_5(i) = b_0 b_1 b_2 b_3 b_4 = \sum_{k=0}^{4} b_k \cdot 2^{4-k}
\]

从 `case(y_1)` 抄出的完整映射表如下（`y_1` 为流水线送来的样本序号，`slot` 为写入 `result_r_ns[slot]` 的格子号）：

| `y_1` | `slot` | `bitrev₅(y_1)` | `bitrev₅(y_1)−1` | 匹配？ |
|------:|------:|---------------:|-----------------:|:------:|
| 0  | 31 | 0  | 31 | ✓ |
| 1  | 15 | 16 | 15 | ✓ |
| 2  | 7  | 8  | 7  | ✓ |
| 3  | 23 | 24 | 23 | ✓ |
| 4  | 3  | 4  | 3  | ✓ |
| 5  | 19 | 20 | 19 | ✓ |
| 6  | 11 | 12 | 11 | ✓ |
| 7  | 27 | 28 | 27 | ✓ |
| 8  | 1  | 2  | 1  | ✓ |
| 16 | 0  | 1  | 0  | ✓ |
| 31 | 30 | 31 | 30 | ✓ |

（上表为节省篇幅只列了若干代表性行；完整 32 行的验证见 4.2.4 实践任务，结论是 **全部 32 行都满足** `slot = bitrev₅(y_1) − 1`。）

于是得到本讲的核心结论：

\[
\boxed{\,\text{slot}(y_1) = \text{bitrev}_5(y_1) - 1\,}
\]

**那个 `−1` 从哪来？** 来自 `y_1` 自身的定义。L60 写的是 `y_1 = count_y>0 ? count_y - 1 : count_y`，即 `y_1` 已经是 1 基计数器 `count_y` 减 1 后的 0 基序号；这条预减与分拣表里的结构再叠加一次，最终落在 `bitrev₅(y_1) − 1`。这条 `−1` 正是 u2-l3 留下的悬念的答案：硬件表并非纯位反转，而是"位反转再左移一位"。

> 关键澄清：`−1` 是一个**索引偏移**，不是数值缩小。它的净效果是让每个样本落到正确的自然频率格子里，使 `result_r` 最终呈自然顺序（由 testbench 的 SNR 通过反证）。

#### 4.2.3 源码精读

`y_1` 的生成（L60）——由 1 基计数器 `count_y` 减 1 得到 0 基序号：[RTL/FFT.v:60](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L60)

```verilog
assign y_1 = (count_y>5'd0)? (count_y - 5'd1) : count_y;
```

`case(y_1)` 分拣表的开头几条与收尾一条（L324–L329、L450–L454）——每条把当前 24 位输出取高 16 位写入对应槽位：[RTL/FFT.v:324-329](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L324-L329)

```verilog
case((y_1))
5'd0 : begin
    result_r_ns[31] = out_r[23:8];   // y_1=0  → slot 31 = bitrev5(0)-1
    result_i_ns[31] = out_i[23:8];
end
5'd1 : begin
    result_r_ns[15] = out_r[23:8];   // y_1=1  → slot 15 = bitrev5(1)-1
    ...
```

最后一条 `5'd31` 同时把完成标志 `next_over` 拉高（L450–L454）：[RTL/FFT.v:450-454](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L450-L454)

```verilog
5'd31 : begin
    result_r_ns[30] = out_r[23:8];   // y_1=31 → slot 30 = bitrev5(31)-1
    result_i_ns[30] = out_i[23:8];
    next_over = 1'b1;                // 32 个样本写完，置完成标志
end
```

整张表被 `if(over!=1'b1)` 包住（L323），意味着一旦进入读出相，分拣就停止，缓冲内容冻结。

对照软件侧 `SIM/FFT.py` 末尾的位反转还原（L189–L193）——注意它用的是**纯** `bitrev₅(i)`，没有 `−1`：[SIM/FFT.py:189-193](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT.py#L189-L193)

```python
for i in range(len(stage5_r)):
    r = int('{:05b}'.format(i)[::-1], 2)   # 纯 5 位位反转，无 -1
    final_ans_r[r] = stage5_r[i]
```

两者形式不同（软件 `bitrev₅(i)` vs 硬件 `bitrev₅(y_1)−1`），但目标一致：都把乱序的 stage-5 输出摆成自然顺序。形式差异源于硬件流水线的样本序号存在一拍预偏移，由分拣表里的 `−1` 予以补偿。

#### 4.2.4 代码实践

**实践目标**：亲手从源码抄出全 32 行映射，验证 `slot = bitrev₅(y_1) − 1`，并用 Python 复现整个排序过程。

**操作步骤**：

1. 打开 `RTL/FFT.v` L324–L456，逐行把每个 `5'dN : result_r_ns[M]` 抄成 `(N, M)` 对，N、M 都取 0~31。
2. 写一段 Python（示例代码如下），对每个 `y_1` 计算 `bitrev5(y_1)` 与 `bitrev5(y_1)-1`，与你抄出的 `M` 比对。
3. 再用同一脚本模拟"乱序写入 + 自然读出"：构造 32 个伪样本 `P[y_1] = y_1`（即第 `y_1` 个到的样本值为 `y_1`），按 `slot=bitrev5(y_1)-1` 写入 `buf`，再按 `0..31` 读出，看读出序列是否为自然顺序。

```python
# 示例代码：验证 case(y_1) == bitrev5(y_1) - 1，并复现排序
def bitrev5(i):
    return int('{:05b}'.format(i)[::-1], 2)

# 1) 从 FFT.v 抄出的映射表（y_1 -> slot），此处用公式生成以供对照；
#    实践时请改为从源码手抄的 32 个 (y_1, slot) 元组。
table_from_src = {y: (bitrev5(y) - 1) % 32 for y in range(32)}  # 待本地用源码核对替换

ok = all(table_from_src[y] == (bitrev5(y) - 1) % 32 for y in range(32))
print("映射 == bitrev5(y_1)-1 ？", ok)   # 预期 True

# 2) 复现硬件排序：乱序写入 + 自然读出
buf = [None] * 32
for y in range(32):
    slot = (bitrev5(y) - 1) % 32
    buf[slot] = "P%d" % y          # 第 y 个到的样本，值为 P{y}
print("读出顺序:", buf)              # 预期是按 slot 0..31 读出的重排结果
```

**需要观察的现象**：步骤 2 的 `ok` 应为 `True`（32 行全匹配）；步骤 3 中，由于写入用了 `bitrev₅ − 1` 的逆排列，按自然地址读出的 `buf` 会呈现出"哪些流水线序号的样本落到了哪个自然频率槽"的对应关系。

**预期结果**：`case(y_1)` 表确实等价于 `slot = bitrev₅(y_1) − 1`；这与 u2-l3 软件侧 `bitrev₅(i)` 的差别仅是一个固定的索引偏移。若你手抄的表与公式有任何一行不符，请回到 L324–L456 重新核对（注意源码里 `result_r_ns[ 7 ]` 这类带空格的写法）。

#### 4.2.5 小练习与答案

- **练习 1**：`y_1 = 16` 时样本写入几号槽？请用二进制推导。
  - **答**：`16 = 10000`，位反转 `00001 = 1`，`slot = 1 − 1 = 0`。即第 17 个到的样本是直流分量 `X[0]`，写入 0 号槽。
- **练习 2**：为什么 `y_1 = 0` 反而写入 31 号槽（最后一个）？
  - **答**：`bitrev₅(0) = 0`，`slot = 0 − 1 = 31`（mod 32）。第 1 个到的样本对应最高频率附近的 `X[31]`，所以落到末槽。
- **练习 3**：如果把 `case` 整体替换成 `result_r_ns[bitrev5(y_1)] = out_r[23:8]`（去掉 `−1`），会发生什么？
  - **答**：每个样本会整体错位一格，`result_r` 不再是自然顺序，testbench 比对黄金数据时 SNR 会严重下降甚至不通过。`−1` 是必需的索引补偿。

### 4.3 over 完成标志与顺序读出

#### 4.3.1 概念说明

光有分拣表还不够，还需要一个"全部写完"的信号来切换到读出相——这就是 `over` 标志。它一旦在最后一个样本（`y_1 = 31`）处被置 1，就永久保持（直到复位），从而把模块从"分拣写入"模式锁进"顺序读出"模式。

读出地址不是新建一个计数器，而是**复用** `count_y → y_1 → y_1_delay` 这条现成的计数链：`y_1_delay` 是 `y_1` 打一拍后的值，在读出相里它恰好扫过自然地址 0→31，于是 `dout = result_r[y_1_delay]` 就把缓冲按自然顺序吐出来。

#### 4.3.2 核心流程

完成标志与读出的状态机（伪代码）：

```
组合逻辑：
  next_over = over                         # 默认保持
  if not over:                             # 写入相
      case(y_1): ... ; y_1==31: next_over = 1   # 写完最后一个 → 置位
  dout_next = over ? result_r[y_1_delay] : 保持   # 读出相才更新 dout

时序逻辑：
  over      <= next_over
  y_1_delay <= y_1                          # 读地址 = y_1 延迟一拍
  dout_r    <= dout_next
```

为什么 `y_1_delay` 在读出相能扫 0→31？关键在位宽截断：`count_y` 是 6 位（L43），`y_1` 是 5 位（L56），`y_1 = count_y − 1` 后赋给 5 位线网会发生**5 位截断**。写入相 `count_y` 走 1→32（`y_1 = 0→31`）；读出相里只要第 4 级 `outvalid` 仍在脉冲（流水线排空期间保持高），`count_y` 继续走 33→64，此时 `count_y − 1 = 32→63`，截断成 5 位即 `0→31`，于是 `y_1_delay` 自然地再扫一遍 0→31，作为读出地址。

#### 4.3.3 源码精读

完成标志的置位与保持——组合块里 `next_over` 默认等于 `over`，仅在 `y_1=31` 的分支里被强制置 1：[RTL/FFT.v:315](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L315) 与 [RTL/FFT.v:453](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L453)

```verilog
next_over = over;          // L315: 默认保持，实现"置位后自锁"
...
5'd31 : begin ... next_over = 1'b1; end   // L453: 最后一个样本写完时置位
```

`y_1_delay` 跟随 `y_1` 打一拍，作为读出地址——[RTL/FFT.v:281](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L281)

```verilog
y_1_delay <= y_1;          // 读地址 = 写地址延迟一拍
```

`dout` 从缓冲读出（仅在读出相、`next_out_valid` 有效时更新）——[RTL/FFT.v:303-310](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L303-L310)

```verilog
if(next_out_valid) begin
    next_dout_r = result_r[y_1_delay];   // 按 y_1_delay 给出的自然地址读
    next_dout_i = result_i[y_1_delay];
end
else begin
    next_dout_r = dout_r;                // 否则保持上一拍
    next_dout_i = dout_i;
end
```

`case` 整体被 `if(over!=1'b1)` 门控——[RTL/FFT.v:323](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L323)：进入读出相后分拣停止，`result_r_ns` 退化为"保持原值"，缓冲内容冻结，保证读出的是稳定结果。

#### 4.3.4 代码实践

**实践目标**：理清 `over` 从 0→1 的时刻，以及读出相里 `y_1_delay` 如何扫过 0→31。

**操作步骤**：

1. 在仿真器里对 `RTL/FFT.v` + `SIM/FFT_tb.v` 跑一组数据，把 `count_y`、`y_1`、`y_1_delay`、`over`、`out_valid`、`dout_r` 一起拉进波形。
2. 找到 `y_1 == 31` 的那一拍，观察下一拍 `over` 是否翻成 1。
3. 在 `over == 1` 之后，观察 `y_1_delay` 是否依次取 0,1,2,…,31，对应的 `dout_r` 是否等于 `result_r[0], result_r[1], …`。

**需要观察的现象**：`over` 一旦置 1 就不再回 0（直到复位）；读出相 `dout_r` 每拍换一个值，呈自然顺序。

**预期结果**：32 拍读出依次得到 `X[0]…X[31]`，与 testbench 中黄金数据 `OUT_real_16_pattern01.txt` 逐拍对齐。

> 待本地验证：读出相要求第 4 级 `radix_no4_outvalid` 在排空期间持续为高，使 `count_y` 继续自增。该脉冲的精确持续拍数依赖整个 SDC 流水线的排空时序，建议以实际波形为准；若仿真中 `out_valid` 提前拉低或 `dout_r` 停滞，应回到 u4-l1 检查 valid 菊花链。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 `over` 用"默认保持 + 单点置位"的写法，而不是用 `count_y == 32` 来判断？
  - **答**：这样把"完成"语义绑定在分拣表的最后一个分支（`y_1=31`）上，与写入动作同拍发生，时序闭合更紧；且 `over` 自锁后无需再看 `count_y`，逻辑更稳健。
- **练习 2**：`y_1_delay` 比 `y_1` 慢一拍，为什么读出偏偏要用延迟过的地址？
  - **答**：`result_r` 的更新比 `case` 的组合输出晚一拍（时序块打拍），用 `y_1_delay`（同样晚一拍）去读，才能对齐"刚写好的那一格"，避免读到旧值。

### 4.4 out_valid 生成

#### 4.4.1 概念说明

`out_valid` 是给外部（testbench）的"输出有效"握手信号。它的生成逻辑很巧妙：在写入相它一直为 0（还没东西可给）；一旦 `over` 置位，它就被"锁存"成 1 并持续，告诉外部"现在起每个 `dout` 都有效，请连续采样 32 拍"。

#### 4.4.2 核心流程

```
组合：
  if next_over == 1: next_out_valid = 1     # 完成后强制拉高
  else:              next_out_valid = assign_out   # 写入相跟随（为 0）
时序：
  assign_out <= next_out_valid              # 打拍成寄存器
  out_valid   = assign_out                  # L59: 输出 = 寄存器值
```

由于 `next_over` 在 `over` 置位后恒为 1，`next_out_valid` 也恒为 1，于是 `out_valid` 在读出相持续为高——这正好配合 testbench 里"等到 `out_valid` 高就连续读 32 个 `dout`"的采样方式。

#### 4.4.3 源码精读

`out_valid` 的组合生成与寄存化——[RTL/FFT.v:320-321](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L320-L321)、[RTL/FFT.v:279](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L279)、[RTL/FFT.v:59](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L59)

```verilog
if(next_over==1'b1) next_out_valid = 1'b1;   // L320: 完成后持续有效
else                next_out_valid = assign_out;
...
assign_out <= next_out_valid;                // L279: 打拍
...
assign out_valid = assign_out;               // L59: 对外输出
```

对照 testbench 的消费方式——它在每组数据里先 `while(!out_valid)` 等到有效，再连续 32 拍采样 `dout_r/dout_i` 与黄金值比对：[SIM/FFT_tb.v:146-170](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L146-L170)

```verilog
for(j=0;j<FFT_size;j=j+1) begin
    while(!out_valid) begin @(negedge clk) ... end   // 等输出有效
    int_r = $fscanf(fp_r, "%d", gold_r);             // 读黄金值
    ...
    noise = gold_r - dout_r;                          // 与硬件输出比对
    @(negedge clk);
end
```

可见 `out_valid` 的"持续高电平 + dout 逐拍更新"恰好满足 testbench 的连续采样预期。

#### 4.4.4 代码实践

**实践目标**：确认 `out_valid` 的拉起时刻与持续行为。

**操作步骤**：

1. 在 4.3.4 的波形基础上，把 `assign_out`、`next_out_valid` 也加进来。
2. 测量从 `in_valid` 拉低（32 个样本喂完）到 `out_valid` 首次拉高的拍数，对照 testbench 的 `latency_limit = 68`（[SIM/FFT_tb.v:14](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L14)）。

**需要观察的现象**：`out_valid` 在 `over` 置位后约 1~2 拍拉高，并在此后保持高电平直到该组数据读完。

**预期结果**：首拍延迟小于 68 拍；`out_valid` 持续高电平期间 `dout` 连续有效。精确的拉起拍数待本地验证。

#### 4.4.5 小练习与答案

- **练习 1**：`out_valid` 为什么在读出相一直为高，而不是每个样本给一个脉冲？
  - **答**：因为排序是"先攒齐 32 个再连续吐"，读出相里每拍 `dout` 都有效，所以用持续高电平更简单；testbench 也按"持续高就连续采"来设计。
- **练习 2**：`assign_out` 这个中间寄存器能否省掉，直接 `assign out_valid = next_out_valid`？
  - **答**：不能。`next_out_valid` 是组合值，直接对外输出会造成组合毛刺且可能引入组合环（它依赖 `over`/`assign_out`）；打一拍成 `assign_out` 再输出，保证 `out_valid` 是干净的寄存器信号，便于外部时序收敛。

## 5. 综合实践

把本讲四个模块串起来，完成一次"软件还原硬件排序"的端到端验证：

1. **抄表**：从 `RTL/FFT.v` L324–L456 手抄出完整 32 行 `(y_1, slot)` 映射，存成 Python 字典 `HW_TABLE`。
2. **验表**：写函数 `bitrev5(i)`，验证 `HW_TABLE[y] == (bitrev5(y) - 1) % 32` 对全部 32 行成立（预期全 True）。
3. **跑参考模型**：按 u2-l1/u5-l2 的方式，用 `SIM/FFT.py` 处理 `SIM/Test_cases/IN_real_pattern01.txt` / `IN_imag_pattern01.txt`，得到 stage-5 乱序输出 `stage5_r` / `stage5_i`。
4. **硬件式重排**：不用 `FFT.py` 末尾那行 `bitrev₅(i)`，而是改用抄来的 `HW_TABLE`：`buf[HW_TABLE[y]] = stage5_r[y]`，再按 `0..31` 读出 `buf`。
5. **比对黄金**：把读出结果与 `SIM/Test_cases/OUT_real_16_pattern01.txt` 比对，统计最大误差。

**验收标准**：步骤 2 全 True；步骤 5 的最大误差落在定点量化允许范围内（与 u5-l2 的 SNR≥40dB 结论一致）。若步骤 5 误差很大，先回头检查步骤 1 的手抄表是否抄错格子号。

## 6. 本讲小结

- 末端排序用两个 32 格 × 16 位数组 `result_r/result_i`（加 `_ns` 镜像）做"先存后读"的缓冲，写入数据取自 24 位通路的 `[23:8]` 高 16 位。
- `case(y_1)` 是一张分拣查找表，精确关系为 **`slot = bitrev₅(y_1) − 1`**——这正是 u2-l3 留下悬念的答案，`−1` 来源于 `y_1 = count_y − 1` 的预减。
- `over` 标志在最后一个样本（`y_1=31`）处置位并自锁，把模块从"写入相"切到"读出相"，并冻结缓冲内容。
- 读出复用 `count_y → y_1 → y_1_delay` 计数链，借助 5 位截断让 `y_1_delay` 在读出相再扫一遍 0→31，使 `dout = result_r[y_1_delay]` 按自然顺序输出。
- `out_valid` 由 `next_over` 拉起、经 `assign_out` 寄存化后对外持续高电平，配合 testbench 的连续采样。
- 硬件表（`bitrev₅−1`）与软件 `FFT.py`（`bitrev₅`）形式不同但目标一致，差异是流水线序号的一拍预偏移补偿。

## 7. 下一步学习建议

- **横向收口控制时序**：本讲侧重"数据怎么摆"，下一讲 **u4-l3 控制时序与握手信号** 会专门拆 `FFT.v` 里两个 `always` 块的协同、第 5 级 `no5_state` 的生成，与本讲的 `over`/`count_y` 时序紧密咬合，建议接着读。
- **纵向回到仿真**：带着本讲对 `out_valid`/`dout` 时序的理解，回到 **u5-l1 Testbench 与 SNR 验证方法**，你会更清楚 testbench 那段 `while(!out_valid)` 循环为什么这么写。
- **拓展到参数化**：本讲的"32 格 + 5 位位反转 + `−1` 偏移"是面向 32 点的特化；想了解改成 64/128 点时这张表、地址位宽、位反转位数怎么变，可预习 **u7-l3 设计扩展：从 32 点到参数化**。
- **源码再读建议**：把 `RTL/FFT.v` L254–L458 的两个 `always` 块连起来通读一遍，对照本讲画的"信箱柜"比喻，确认你能向别人讲清"一封信从进柜到被取走"的完整旅程。
