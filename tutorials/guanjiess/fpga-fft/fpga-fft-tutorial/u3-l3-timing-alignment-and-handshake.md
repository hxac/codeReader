# 时序对齐与跨级握手：rotator_valid、start_next、HALT

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 SDF（单路延迟反馈）流水线里「为什么不能从第一拍就开始乘旋转因子」——理解 delay 反馈环造成的「半周期建立期」。
- 读懂 `butterfly_general.v` 里 `WAIT_FOR_ROTATOR` 计数器和 `r_rotator_valid` 的产生过程，解释旋转因子输出是如何与蝶形 D 输出对齐到同一拍的。
- 读懂 `HALT_FOR_NEXT_LAYER = 6 + PERIOD/2` 的含义，解释 `next_level_start`（即 `start_next`）脉冲在哪一拍触发、又如何变成下一级的 `start`。
- 解释源码注释里 `HALT_FOR_NEXT_LAYER-2`（anlogic）与 `HALT_FOR_NEXT_LAYER-3`（vivado）的差异，到底来自 ROM 读取延迟的几拍之差。

本讲是整个流水线里「最难、最依赖逐拍推演」的一块。前置讲义 u3-l1 讲了旋转因子怎么存、怎么寻址，u3-l2 讲了 delay 怎么用 RAM 做反馈延时；本讲把它们串起来，回答一个问题：**这些部件要在哪一拍同时就位，整条流水线才算对齐。**

## 2. 前置知识

在进入本讲前，请先回忆两个关键结论（来自 u3-l1、u3-l2）：

1. **delay 是 SDF 的反馈心脏**：蝶形下支 B 写进双口 RAM，延时半个周期（\(2^{\text{layer}-1}\) 拍）后当上支 C 读出来喂回蝶形。也就是说，C 不是一开始就有的，必须等 RAM「攒满」半周期数据后才有效。
2. **旋转因子默认是 W=1**：当 `rotator_valid` 没拉高时，`Rotator_address`/`Rotator16` 的地址停在 0、select 为 0，输出端会稳稳给出 \((1\ll16,\ 0)\)（即实数 1、虚数 0，相当于乘 1，直通）。只有 `rotator_valid` 拉高后，地址才开始递增，真正的旋转因子才一个接一个地流出来。

把这两条放在一起，本讲的核心矛盾就浮现了：

- 蝶形 D 输出 \(D=A+C\)，而 C 要等半周期才有效。所以 **D 的前半周期是「垃圾」（A 与无效 C 相加）**，后半周期才是真正可用的频域中间结果。
- 旋转因子必须 **只在 D 有效的那一段** 才作用，否则就会把垃圾数据也乘上旋转因子，破坏结果。
- 同理，**下一级必须在 D 开始稳定输出有效样本时才启动**，启动早了会吃进垃圾，启动晚了会丢数据。

「让旋转因子、下一级启动都精确落在 D 有效的那一刻」——这就是本讲的全部主题。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| `src/butterfly_general.v` | 参数化通用蝶形层，`fft_32` 及以上所有高层复用 | `WAIT_FOR_ROTATOR`、`r_rotator_valid`、`HALT_FOR_NEXT_LAYER`、`next_level_start` 的产生 |
| `src/fft_16.v` | 16 点层，手写版（结构与 `butterfly_general` 同构） | 对照看 `WAIT_FOR_ROTATOR = PERIOD-1` 的细微差异 |
| `src/fft_8.v` | 8 点层，手写版 | 对照看计数封顶条件的另一种写法 |
| `src/Rotator16.v` / `src/Rotator_address.v` | 旋转因子地址与 select 生成 | `select_1d`/`select_2d` 两拍打拍、ROM 读延迟 |
| `src/fft_32.v` | 32 点层，`butterfly_general` 的最简真实用例 | 看 `rotator_valid` 如何驱动 `Rotator_address`、select 如何选 W=1 |
| `src/butterfly.v` | 蝶形加减核 | D 输出的选择逻辑 `S_1d ? added : C_1d` |

## 4. 核心概念与源码讲解

本讲围绕 `butterfly_general.v` 这一个最小模块，拆成四节：先讲清「为什么必须等」（建立期），再讲旋转因子对齐（`rotator_valid`），再讲跨级握手（`start_next`/`HALT`），最后讲平台差异（`-2` vs `-3`）。每节都对照手写的 `fft_8.v`/`fft_16.v` 加深理解。

### 4.1 为什么必须对齐：SDF 的半周期建立期

#### 4.1.1 概念说明

SDF（Single-path Delay Feedback，单路延迟反馈）的核心思想是：**用一条延时线把数据「攒」起来，攒够半周期后再放出去配对运算**。

对一级处理 \(N = 2^{\text{layer}}\) 点的流水层，定义：

\[
\text{PERIOD} = N = 2^{\text{layer}}, \qquad \text{delay 容量} = \frac{\text{PERIOD}}{2} = 2^{\text{layer}-1}
\]

一个完整 PERIOD 被对半切成两段：

- **前半周期（建立期）**：蝶形下支 B 的输出持续写进 delay RAM，但 RAM 还没攒满，读出来的 C 是无效的（或旧值）。此时蝶形算出的 \(D = A + C\) 里 C 是垃圾，**D 不可用**。
- **后半周期（有效期）**：RAM 攒满，C 开始反馈真实的历史样本，蝶形算出的 D 才是真正的频域中间结果。

所以，凡是依赖 D 有效性的操作——乘旋转因子、启动下一级——都**必须跳过前半周期，从后半周期开始**。这就是「半周期建立期」带来的对齐约束。

#### 4.1.2 核心流程

```
start 拉高
   │
   ▼
[状态机进入 START/PROCESSING，A 开始持续输入]
   │
   ├─ 前 PERIOD/2 拍：B 写入 delay RAM，C 无效 → D 是垃圾
   │      （这段时间 rotator_valid=0、start_next=0）
   │
   ▼  RAM 攒满半周期
[后半周期：C 反馈有效，D = A+C 成为真实结果]
   │
   ├─ rotator_valid 在 D 首次有效处拉高 → 旋转因子开始作用
   └─ start_next 在 D 首次有效处发脉冲 → 下一级 start 被触发
```

#### 4.1.3 源码精读

`butterfly_general.v` 里 delay 的延时量由 `layer` 参数决定（[src/butterfly_general.v:208-219](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L208-L219)），承接 u3-l2 的结论，其名义延时就是 \(2^{\text{layer}-1} = \text{PERIOD}/2\) 拍——这正是「半周期建立期」的硬件来源。

蝶形 D 的选择逻辑在 [src/butterfly.v:112-113](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly.v#L112-L113)：

```verilog
assign D_real = (S_1d) ? r_x_added_real : r_C_real_1d;  // S_1d=1 时 D=A+C
```

当控制信号 `S` 在后半周期翻转为 1（`S_1d` 随后也为 1），蝶形才输出求和结果 \(A+C\)。而 `S` 的翻转点由计数器决定（[src/butterfly_general.v:97-111](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L97-L111)）：

```verilog
end else if (S_counter == PERIOD/2-1 | S_counter == PERIOD-1)
    S <= ~S;   // 在半周期边界翻转 S
```

`S_counter` 数到 `PERIOD/2-1` 时翻转一次——这恰好是建立期结束、有效期开始的时刻。所以 **D 的首次有效，发生在大约 `PERIOD/2` 拍之后**（再叠加 delay 读出、蝶形打拍等几拍流水开销）。

#### 4.1.4 代码实践

**实践目标**：用具体数字感受「半周期建立期」有多长。

**操作步骤**：

1. 打开 `src/butterfly_general.v`，找到 `PERIOD = 1<<layer`（[第 24 行](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L24)）。
2. 分别代入 `layer = 5`（`fft_32`）、`layer = 10`（`fft_1k`）、`layer = 14`（`fft_16k`），计算 `PERIOD` 与 `PERIOD/2`。
3. 在 `src/fft_32.v` 中确认 `butterfly_general` 的例化参数确实是 `layer(5)`（[第 30 行](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v#L30)）。

**需要观察的现象 / 预期结果**：

| layer | 模块 | PERIOD | 建立期 PERIOD/2（拍） |
|------|------|--------|----------------------|
| 5 | fft_32 | 32 | 16 |
| 10 | fft_1k | 1024 | 512 |
| 14 | fft_16k | 16384 | 8192 |

可以看到：**点数越大，建立期越长**。`fft_16k` 要等 8192 拍 delay 才反馈有效——这也是 u1-l4 里说「大延时层排在流水线最前面」的代价之一。

#### 4.1.5 小练习与答案

**练习 1**：如果某一级的 `layer=6`，建立期是多少拍？D 首次有效大约在第几拍附近？

> **参考答案**：PERIOD = 64，建立期 = 32 拍。D 首次有效大约在第 32 拍之后（再叠加 delay 读出与蝶形打拍的若干拍流水开销，精确拍数待本地仿真确认）。

**练习 2**：为什么不能让旋转因子从第 0 拍就开始乘？

> **参考答案**：第 0 拍到第 PERIOD/2 拍是建立期，C 无效、D 是垃圾。若这段时间就乘旋转因子，会把无效数据当作有效结果送进下一级，破坏整个频谱。

---

### 4.2 rotator_valid：让旋转因子与蝶形 D 输出对齐

#### 4.2.1 概念说明

`rotator_valid` 是旋转因子通路的「总开关」。回顾 u3-l1：`Rotator_address` 内部的地址计数器 `r_addra` 只在 `rotator_valid == 1` 时才递增，否则停在 0；相应地，select 也只在 `rotator_valid` 期间才按节拍切换。也就是说：

- `rotator_valid == 0` 时，旋转因子输出恒为默认的 W=1 \((1\ll16,\ 0)\)，相当于「不旋转、直通」。
- `rotator_valid == 1` 时，地址开始走，真实旋转因子才一个一个流出来，并按「真实因子（前半段）→ 补 W=1（后半段）」的节拍循环。

所以 `rotator_valid` 的拉高时刻，**必须正好对上蝶形 D 第一次输出有效样本的那一拍**。早一拍，旋转因子作用在了垃圾 D 上；晚一拍，第一个有效 D 被当成了直通（漏乘了本该乘的因子）。

#### 4.2.2 核心流程

`butterfly_general.v` 用一个计数器 `r_count_rotator` 来「等待建立期」，再用它的封顶值产生 `r_rotator_valid`：

```
状态机进入 START/PROCESSING 后：
   r_count_rotator 从 0 开始每拍 +1
        │
        ▼  数到 WAIT_FOR_ROTATOR - 1 时封顶（不再增加）
   [r_count_rotator == WAIT_FOR_ROTATOR - 1]
        │
        ▼  下一拍
   r_rotator_valid <= 1   ← 旋转因子总开关打开
        │
        ▼  rotator_valid 传给 Rotator_address
   地址 r_addra 开始递增，真实旋转因子流出，与 D 同拍进入 multiplier
```

其中关键参数：

\[
\text{WAIT\_FOR\_ROTATOR} = \text{PERIOD} - 2
\]

这个值「比一个完整周期少 2 拍」，配合计数和打拍，让 valid 的上升沿落在大约「建立期结束 + 流水补偿」的位置。

#### 4.2.3 源码精读

参数与寄存器声明在 [src/butterfly_general.v:147-151](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L147-L151)：

```verilog
parameter WAIT_FOR_ROTATOR = PERIOD - 2;
reg   [13:0] r_count_rotator;
reg          r_rotator_valid;
```

计数器在 `START/PROCESSING` 状态下递增，数到 `WAIT_FOR_ROTATOR - 1` 封顶（[第 153-167 行](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L153-L167)）：

```verilog
if (r_state == STATE_START | r_state == STATE_PROCESSING) begin
    if (r_count_rotator == WAIT_FOR_ROTATOR - 1)
        r_count_rotator <= r_count_rotator;   // 封顶
    else
        r_count_rotator <= r_count_rotator + 1;
end
```

`r_rotator_valid` 的产生（[第 169-179 行](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L169-L179)）：当计数器到达封顶值，下一拍 valid 拉高；否则为 0。

```verilog
if (r_count_rotator == WAIT_FOR_ROTATOR - 1)
    r_rotator_valid <= 1;
else
    r_rotator_valid <= 0;
```

最后 `assign rotator_valid = r_rotator_valid;`（[第 180 行](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L180)）把开关送给外层的 `Rotator_address`。

**对照手写版**：`fft_16.v` 里的逻辑结构几乎一样，但 `WAIT_FOR_ROTATOR` 取值不同（[src/fft_16.v:183](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16.v#L183)）：

```verilog
parameter WAIT_FOR_ROTATOR = PERIOD - 1;   // 注意：这里是 PERIOD-1，不是 PERIOD-2
```

而 `fft_8.v` 又是另一种写法（[src/fft_8.v:187](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_8.v#L187) 与 [第 200、216 行](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_8.v#L200-L216)）：常量 `WAIT_FOR_ROTATOR = 7`（= PERIOD-1），且计数封顶和 valid 判断都用 `== WAIT_FOR_ROTATOR`（不减 1）。

> 这三处差异（`PERIOD-2` vs `PERIOD-1`、是否 `-1`）说明：**不同层级、不同写法下，valid 的精确对齐拍数被各自微调过**。它们的共同目标是同一个——让 valid 落在 D 首次有效处；但具体补偿几拍，取决于该层 delay 是 RAM 还是寄存器、蝶形打几拍等本地细节。精确拍数以仿真波形为准。

#### 4.2.4 代码实践

**实践目标**：在 `fft_16.v` 中追踪 `r_count_rotator` 与 `r_rotator_valid` 的产生过程，说明旋转因子为什么要在蝶形开始后等待 `WAIT_FOR_ROTATOR-1` 拍才有效。

**操作步骤**：

1. 打开 `src/fft_16.v`，定位 `WAIT_FOR_ROTATOR = PERIOD - 1`（[第 183 行](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16.v#L183)）。对 16 点层，PERIOD=16，所以 `WAIT_FOR_ROTATOR = 15`，封顶值 `WAIT_FOR_ROTATOR - 1 = 14`。
2. 读计数块（[第 192-206 行](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16.v#L192-L206)）：状态机进入 `START/PROCESSING` 后，`r_count_rotator` 从 0 每拍加 1，加到 14 封顶。即它要经历 0→14 共 15 个计数值才到达封顶。
3. 读 valid 块（[第 218-228 行](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16.v#L218-L228)）：只有当 `r_count_rotator == 14` 时，下一拍 `r_rotator_valid` 才置 1。
4. 追踪 `r_rotator_valid` 如何驱动 `Rotator16`（[第 230-236 行](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16.v#L230-L236)）：valid 一拉高，`Rotator16` 内部的 `r_addra` 才开始递增（见 [src/Rotator16.v:16-26](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/Rotator16.v#L16-L26)），真实旋转因子才开始流出。

**需要观察的现象 / 预期结果**：

- `r_count_rotator` 在状态机开始处理后，先静默计数约 15 拍（等建立期），这段时间 `r_rotator_valid` 始终为 0，旋转因子通路输出默认 W=1。
- 计数到 14 后，`r_rotator_valid` 在下一拍跳变为 1，`Rotator16` 的地址开始走，旋转因子与蝶形 D 的有效输出在同一时段进入 `multiplier`。
- **为什么是 `WAIT_FOR_ROTATOR-1` 拍**：因为 delay 需要半周期（PERIOD/2=8 拍）建立 C，蝶形 D 才有意义；计数值 0..14 这段等待，正是用来跨过这个建立期并补偿若干拍流水开销，让 valid 上升沿精确落在 D 首次有效处。

> 注：上面给出的「约 15 拍」是按计数语义推得的概念值。由于状态机从 IDLE→START→PROCESSING 的转移、各 always 块非阻塞赋值的相对节拍都会引入 ±1~2 拍的偏移，**精确到第几拍需以仿真波形为准**（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `WAIT_FOR_ROTATOR` 设得过大（比如等于 PERIOD），`rotator_valid` 会怎样？对结果有何影响？

> **参考答案**：valid 拉得更晚，前几个有效 D 会被当成 W=1 直通（漏乘旋转因子），输出频谱的前若干点会出错。

**练习 2**：`butterfly_general.v` 用 `PERIOD-2`，`fft_16.v` 用 `PERIOD-1`，二者 valid 拉高时刻差几拍？这个差异可能由什么引起？

> **参考答案**：差 1 拍。可能由 delay 在 RAM 版（fft_16）与参数化版（butterfly_general 经 `fft_32` 例化）里读出延迟的细微不同、或状态机进入计数时刻的差 1 拍引起。这种「同构但补偿常数不同」正是手写层向参数化层迁移时需要逐层校准的地方。

---

### 4.3 start_next 与 HALT_FOR_NEXT_LAYER：跨级握手

#### 4.3.1 概念说明

回顾 u1-l4：`fft_top` 把各级首尾相连，**上一级的 `data_out` 喂给下一级的 `data_in`，上一级的 `start_next` 触发下一级的 `start`**。这条 `start_next → start` 链是流水线真正在用的启动链（`over/end` 链在多数层级并未贯通，例如 `fft_8.v` 里 `end4` 被 `assign end4 = 0;` 恒置 0，见 [src/fft_8.v:277](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_8.v#L277)）。

那么上一级应该在 **哪一拍** 发出 `start_next` 脉冲？答案是：**在本级 D 开始稳定输出有效样本的那一拍**——这和 `rotator_valid` 拉高的时机是同一类问题。下一级一旦收到 `start`，也开始它自己的「建立期」，时序上正好衔接。

`butterfly_general.v` 用 `HALT_FOR_NEXT_LAYER` 这个常量来量化「本级开始处理后，要等多少拍才发 `start_next`」。

#### 4.3.2 核心流程

```
本级状态机进入 START/PROCESSING，next_level_start_counter 开始计数
        │
        ▼  每拍 +1，封顶在 HALT_FOR_NEXT_LAYER
[counter == HALT_FOR_NEXT_LAYER - 3]   ← vivado 版触发点
        │
        ▼  下一拍
r_next_level_start <= 1   ← 产生一个单拍脉冲
        │
        ▼  assign next_level_start = r_next_level_start
脉冲送出 → 下一级 fft 的 start 端口 → 下一级状态机离开 IDLE
```

关键常量：

\[
\text{HALT\_FOR\_NEXT\_LAYER} = 6 + \frac{\text{PERIOD}}{2}
\]

其中 `PERIOD/2` 就是半周期建立期，`6` 是固定的流水开销（状态机转移、打拍等）。再用 `-3`（或 `-2`）做平台相关的微调。

#### 4.3.3 源码精读

参数定义（[src/butterfly_general.v:23-25](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L23-L25)）：

```verilog
parameter current_layer       = layer;
parameter PERIOD              = 1 << layer;
parameter HALT_FOR_NEXT_LAYER = 6 + (PERIOD)/2;
```

`next_level_start_counter` 在 `START/PROCESSING` 状态下递增，封顶在 `HALT_FOR_NEXT_LAYER`（[第 131-143 行](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L131-L143)）。脉冲的产生（[第 116-129 行](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L116-L129)）：

```verilog
//if(next_level_start_counter == HALT_FOR_NEXT_LAYER-2) begin
//HALT_FOR_NEXT_LAYER-2 is used for anlogic version
//HALT_FOR_NEXT_LAYER-3 is used for vivado version
if(next_level_start_counter == HALT_FOR_NEXT_LAYER-3) begin
    r_next_level_start <= 1;
end else begin
    r_next_level_start <= 0;
end
```

最后 `assign next_level_start = r_next_level_start;`（[第 144 行](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L144)）。在 `fft_32.v` 里，这个输出直接成了 `start_next`（[src/fft_32.v:45](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v#L45)），再由 `fft_top` 接到下一级的 `start`。

> **关于「6」**：这是一个经验性的固定补偿，吸收了「状态机从 IDLE 走到 PROCESSING 的几拍 + delay 读出延迟 + 蝶形打拍」等不随 layer 变化的流水开销。而 `PERIOD/2` 是随 layer 线性增长的建立期。两者相加，就得到了本级应等待的总拍数。

#### 4.3.4 代码实践

**实践目标**：代入 `fft_32`（layer=5），算出 `HALT_FOR_NEXT_LAYER` 与触发拍数，理解握手时机。

**操作步骤**：

1. 对 `layer=5`：PERIOD = 32，`HALT_FOR_NEXT_LAYER = 6 + 32/2 = 6 + 16 = 22`。
2. vivado 触发点：`counter == 22 - 3 = 19`，即计数器数到 19 那一拍的下一拍，`next_level_start` 发出一个单拍脉冲。
3. 在 `src/fft_32.v` 中确认这个 `next_level_start` 经 `assign start_next = w_next_level_start;`（[第 45 行](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v#L45)）送出，最终接到下一级（`fft_16`）的 `start16`。

**需要观察的现象 / 预期结果**：

| layer | 模块 | PERIOD | HALT = 6+PERIOD/2 | vivado 触发 (HALT-3) | anlogic 触发 (HALT-2) |
|------|------|--------|-------------------|----------------------|----------------------|
| 4 | fft_16 | 16 | 14 | 11 | 12 |
| 5 | fft_32 | 32 | 22 | 19 | 20 |
| 10 | fft_1k | 1024 | 518 | 515 | 516 |

可以看到：**点数越大，等待下一级启动的延迟越长**，但相对于 PERIOD 的占比约为一半，与「建立期 = 半周期」吻合。

> 精确的脉冲拍数同样以仿真波形为准（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `HALT_FOR_NEXT_LAYER` 里要加一个常数 `6`，而不是直接用 `PERIOD/2`？

> **参考答案**：`PERIOD/2` 只覆盖了 delay 的建立期，但状态机从 IDLE→START→PROCESSING 的转移、delay 的读出延迟、蝶形的打拍等都还要额外几拍。常数 `6` 就是用来吸收这些不随 layer 变化的固定流水开销。

**练习 2**：如果下一级的 `start` 来早了一拍（即 `HALT_FOR_NEXT_LAYER` 偏小），会出什么问题？

> **参考答案**：下一级会在本级 D 还未稳定有效时就启动，吃进的前几个样本是建立期的垃圾数据，导致整条流水线的频谱错位。

---

### 4.4 平台差异：-2（anlogic）vs -3（vivado）与 ROM 读延迟

#### 4.4.1 概念说明

源码注释里反复出现一行关键说明（[src/butterfly_general.v:120-122](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L120-L122)、[src/fft_16.v:119-121](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16.v#L119-L121)、[src/fft_8.v:119-120](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_8.v#L119-L120)）：

```verilog
//HALT_FOR_NEXT_LAYER-2 is used for anlogic version
//HALT_FOR_NEXT_LAYER-3 is used for vivado version
```

也就是说：同样的握手逻辑，在 **anlogic（安路）FPGA** 上要把触发点设为 `HALT-2`，在 **vivado（Xilinx）** 上要设为 `HALT-3`，两者差 1 拍。这个差异的根源是：**旋转因子 ROM 在两个平台上的读取延迟不同**。

回顾 u3-l1：旋转因子通路是 `地址 r_addra → ROM douta → select mux → 送 multiplier`。这条路径上的延迟拍数，决定了旋转因子真正到达 multiplier 的时刻；而 `start_next` 的时机必须与「旋转因子就位 + D 有效」整体对齐，所以 ROM 的读延迟变化，会反过来要求 `start_next` 的触发拍数跟着变。

#### 4.4.2 核心流程

旋转因子从「地址给出」到「数据到达 multiplier 输入」的延迟链：

```
r_addra 寄存器（1 拍）  ← rotator_valid 驱动递增
        │
        ▼
ROM douta（vivado blk_mem_gen 默认 latency=1，即地址给出后 1 拍 douta 有效）
        │
        ▼
select_1d ← r_addra[高位]（1 拍）
select_2d ← select_1d（再 1 拍）   ← 共补 2 拍，让 select 与 ROM 数据对齐
        │
        ▼
select mux：select_2d ? W=1 : ROM_data
        │
        ▼
r_rotator_real/img 寄存（1 拍）→ 送 multiplier 的 c/d
```

在 vivado 下，ROM 有 1 拍读延迟，所以 select 要打 2 拍（`select_1d`、`select_2d`）来对齐；整个握手链因此比「ROM 零延迟」多出几拍，对应触发点用 `-3`。anlogic 平台的 ROM/BRAM 读延迟特性不同（少 1 拍），所以用 `-2`。

#### 4.4.3 源码精读

`Rotator16.v` 里 select 的两级打拍（[src/Rotator16.v:28-36](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/Rotator16.v#L28-L36)）：

```verilog
select_1d <= r_addra[3];     // 第 1 拍
select_2d <= select_1d;      // 第 2 拍
```

最终用 `select_2d` 在 ROM 数据与 W=1 之间二选一（[第 39-40 行](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/Rotator16.v#L39-L40)）：

```verilog
assign rotator_real = select_2d ? 1<<16 : w_rotator_real_tmp;
assign rotator_img  = select_2d ? 0      : w_rotator_img_tmp;
```

参数化版 `Rotator_address.v` 完全同理，只是把位宽参数化（[src/Rotator_address.v:34-45](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/Rotator_address.v#L34-L45)）：`select_1d <= r_addra[layer-1]`，再打一拍得 `select_2d`。

ROM 本身是厂商 IP，vivado 下的例化见 `Rotator16.v`（[第 60-70 行](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/Rotator16.v#L60-L70)），`addra` 给出后 `douta` 下一拍有效（典型 latency=1）。在 `fft_32.v` 里，这个 select 信号再控制 `r_rotator_real/img` 的寄存选择（[src/fft_32.v:74-92](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v#L74-L92)）：

```verilog
if (w_rotator_valid) begin
    if (w_select) begin
        r_rotator_real <= 1 << 16;   // 后半段补 W=1
        r_rotator_img  <= 0;
    end else begin
        r_rotator_real <= w_rotator_real_tmp;  // 前半段读真实因子
        r_rotator_img  <= w_rotator_img_tmp;
    end
end else begin
    r_rotator_real <= 1 << 16;       // valid 未到，默认 W=1
    r_rotator_img  <= 0;
end
```

> 正是因为这条「地址→ROM→select 两拍→寄存」的延迟链在两个平台上拍数不同，才需要 `start_next` 的触发点在 `-2` 与 `-3` 之间切换。**这是一个强平台耦合的常量**：移植到新 FPGA 时必须重新校准。

#### 4.4.4 代码实践

**实践目标**：解释 `HALT_FOR_NEXT_LAYER-3` 中那个 `-3` 的来源。

**操作步骤**：

1. 跟踪旋转因子数据通路的总延迟：`r_addra`（1 拍寄存）→ ROM `douta`（vivado 1 拍）→ `select_2d`（累计 2 拍）→ `r_rotator_real/img`（1 拍寄存）。
2. 数一数从「地址确定」到「旋转因子进入 multiplier 的 c/d 端口」共经过几级寄存/读延迟。
3. 对比：若换到 ROM 读延迟少 1 拍的平台（anlogic），整条链少 1 拍，所以触发点从 `-3` 变 `-2`。

**需要观察的现象 / 预期结果**：

- `-3` 中的「3」大致对应旋转因子通路里「ROM 读 1 拍 + select 对齐 2 拍」这三拍的平台相关延迟（精确归属以综合后时序报告为准，待本地验证）。
- 因此 `-3`（vivado）vs `-2`（anlogic）的差 1 拍，本质是 **ROM 读取延迟在两个平台上相差 1 拍** 的体现。
- 移植结论：换用第三家 FPGA（如 Intel/Altera）时，必须先测出其 BRAM/ROM 的读延迟，再把 `-3` 改成相应值，否则 `start_next` 会对齐错位。

#### 4.4.5 小练习与答案

**练习 1**：如果把 vivado 工程直接拿到 anlogic 上跑、且忘了把 `-3` 改回 `-2`，会怎样？

> **参考答案**：`start_next` 会比正确时机晚 1 拍发出，下一级启动滞后，D 的首个有效样本与下一级建立期错位，输出频谱整体错位/出错。

**练习 2**：为什么 `select` 要打两拍（`select_1d`、`select_2d`）而不是一拍？

> **参考答案**：因为 ROM 的 `douta` 在地址给出后有 1 拍读延迟，而 `select` 是直接由地址高位组合产生的。为了让 `select` 与「经过 1 拍延迟才到达的 ROM 数据」在同一拍对齐，select 自己也要往后推拍；经过调试最终用了两级打拍，使 mux 的两个输入（W=1 常量、ROM 数据）在同一拍就位。

---

## 5. 综合实践

**任务**：以 `fft_32`（`butterfly_general` layer=5）为对象，画一张从「外部 `start`」到「下一级 `start_next`」的完整时序对齐图，把本讲三个关键量——建立期、`rotator_valid`、`start_next`——在时间轴上对齐。

**操作步骤**：

1. 计算：PERIOD = 32，建立期 = 16 拍，`HALT_FOR_NEXT_LAYER = 6 + 16 = 22`，vivado 触发点 = 22-3 = 19，`WAIT_FOR_ROTATOR = 32-2 = 30`。
2. 在时间轴（横轴为时钟拍数 0,1,2,…）上标出：
   - 状态机：IDLE → START → PROCESSING 的转移拍。
   - delay 建立期：0 ~ 16 拍（C 无效区间）。
   - `r_count_rotator`：0 递增到 29 封顶；`r_rotator_valid` 在封顶后拉高。
   - `next_level_start_counter`：0 递增到 19 时，下一拍发 `start_next` 脉冲。
   - 蝶形 D 首次有效的大致区间（建立期结束后）。
3. 用箭头标出对齐关系：`rotator_valid` 上升沿 ↔ D 首次有效；`start_next` 脉冲 ↔ D 首次有效。
4. 思考：如果改用 anlogic（触发点 22-2=20），`start_next` 会比 vivado 晚 1 拍，图上要怎么改？

**预期结果**：一张能直观说明「建立期一过，`rotator_valid` 和 `start_next` 几乎同时就位」的对齐图。如果手头有仿真工具，可用 `tb/fft_general_tb.v` 跑 `fft_32`，把 `w_rotator_valid`、`w_next_level_start`、`w_D_real` 信号拉进波形，验证你画的图与实际波形是否一致（待本地验证）。

## 6. 本讲小结

- SDF 每级都有 **半周期建立期**（= `PERIOD/2` 拍）：前半周期 delay 在填充、C 无效，蝶形 D 是垃圾；所有依赖 D 有效性的操作都必须跳过这段。
- `rotator_valid` 是旋转因子通路的 **总开关**：由 `r_count_rotator` 计数到 `WAIT_FOR_ROTATOR-1` 触发，拉高前输出默认 W=1，拉高后真实旋转因子才流出，与蝶形 D 有效输出对齐进 multiplier。
- `start_next`（即 `next_level_start`）由 `HALT_FOR_NEXT_LAYER = 6 + PERIOD/2` 控制：`6` 是固定流水开销，`PERIOD/2` 是建立期；计数到 `HALT-3` 发单拍脉冲，触发下一级 `start`。
- **`-2`（anlogic）与 `-3`（vivado）的差 1 拍**，根源是旋转因子 ROM 在两个平台上的读取延迟不同，反映在「地址→ROM→select 两拍→寄存」这条延迟链上。
- `butterfly_general.v` 的参数化版与手写的 `fft_8.v`/`fft_16.v` 在 `WAIT_FOR_ROTATOR` 取值（`PERIOD-2` vs `PERIOD-1`）和计数封顶条件上有细微差异，说明这些对齐常量是 **逐层、逐平台校准** 出来的，移植时须重新验证。
- 真正在用的是 `start_next → start` 启动链；`over/end` 链在多数层级未贯通（如 `fft_8` 的 `end4` 恒为 0）。

## 7. 下一步学习建议

- **向大点数层延伸**：进入 u4-l3、u4-l4，看 `butterfly_general` 如何被 `fft_32`/`fft_1k`/`fft_16k` 复用——你会发现高层模块只是改 `layer` 参数和 ROM 实例名，本讲的 `HALT_FOR_NEXT_LAYER`、`WAIT_FOR_ROTATOR` 会随 `layer` 自动放大。
- **用仿真验证对齐**：进入 u5-l2，用 `tb/fft_general_tb.v` 跑仿真，重点观察 `rotator_valid`、`next_level_start` 与 `D_real` 的相对时序，亲手确认本讲的拍数推导。
- **平台移植**：进入 u5-l3，系统梳理项目对厂商 IP（`mult2`、`Delay`、`rotator_*_real/img`）的依赖，以及本讲提到的 `-2`/`-3` 校准在移植清单中的位置。
- **继续阅读的源码**：建议精读 `src/fft_32.v`（`butterfly_general` 的最简完整用例）和 `src/fft_top.v`（看 14 个 `start_next` 如何首尾相接），把「级内对齐」和「级间握手」连成一条完整链路。
