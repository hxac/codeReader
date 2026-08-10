# 通用蝶形 butterfly_general.v：参数化封装 layer

## 1. 本讲目标

本讲是「逐级解析」单元的第三篇，承接 u4-l2（fft_8 / fft_16 的过渡）与 u3-l3（时序对齐与跨级握手）。前两篇我们看到：fft_8、fft_16 各自手写了状态机、S 控制、延时反馈、握手计数——**每升一级就把这一大段逻辑照抄一遍**，只改几个参数。本讲要讲透的 `butterfly_general.v`，就是作者把这段「每级都要重复一遍」的逻辑**抽成一个参数化模块**的成果。

学完本讲，你应当能够：

- 说清 `butterfly_general` 的**设计动机**：它把 fft_8/fft_16 里反复出现的「状态机 + S 控制 + RAM 延时 + 下一级启动 + rotator_valid」五样东西收进一个 `layer` 参数控制的模块，从此 fft_32 及以上的所有高层都不再手写这段逻辑。
- 看懂它的 **`layer` 参数体系**：`PERIOD = 1<<layer`、`HALT_FOR_NEXT_LAYER = 6 + PERIOD/2`、`WAIT_FOR_ROTATOR = PERIOD-2`、以及延时实例 `delay #(.layer(current_layer))`，全部由 `layer` 一处导出。
- 画出它的**内部数据通路**：状态机驱动 `butterfly` 与 `delay`，构成 `B（下支）→delay→C（上支）` 的 SDF 反馈闭环，`D` 作为前向输出送出。
- 读懂它对外暴露的**两个门控信号** `next_level_start`（驱动下一级启动）和 `rotator_valid`（放行旋转因子），并理解它们已在 u3-l3 讲过的常量是如何被封装进来的。
- 对照 `fft_32.v`，看懂 **fft_32 起所有高层模块的同构模板**：`butterfly_general` + `Rotator_address` + ROM + `multiplier` 四块拼接，只差 `layer` 参数与 ROM 实例名。

> 本讲聚焦「封装结构」与「fft_32 的拼接方式」。时序对齐（`HALT`、`WAIT_FOR_ROTATOR` 的逐拍推导）已在 u3-l3 讲透，本讲只说明它们如何被搬进 `butterfly_general`，不再重复推导细节。

## 2. 前置知识

阅读本讲前，请确认你已理解以下概念（均来自前置讲义）：

- **SDF 单路延迟反馈**（u1-l4、u3-l2）：每一级蝶形的下支输出 B 先存入延时单元，攒满半周期后再当上支 C 喂回蝶形，使相隔半周期的样本配对。延时深度恒为半周期 \( \text{PERIOD}/2 = 2^{\text{layer}-1} \)。
- **蝶形 butterfly.v**（u2-l1）：D 是前向输出（与 C 寄存一拍对齐后选择输出），B 是反馈下支；模块自带 1 拍流水线延迟，靠 `S_1d` 在「直通」与「加减计算」两路间二选一。
- **复数乘法 multiplier.v**（u2-l2）：\((a+jb)(c+jd)\) 拆成 4 个实数乘法；`a/b` 接数据、`c/d` 接旋转因子，只用截断输出 `*_trunc`，`.rstn(~rst)` 把高有效复位翻转成低有效。
- **旋转因子寻址 Rotator_address.v**（u3-l1）：参数 `layer` 生成 ROM 地址与 `select`，前半周期读真实因子、后半周期补 \(W=1\)。
- **时序对齐与跨级握手**（u3-l3）：`rotator_valid` 是旋转因子通路的总开关（跳过半周期建立期）；`next_level_start` 由 `HALT_FOR_NEXT_LAYER = 6 + PERIOD/2` 控制，计数到位发单拍脉冲驱动下一级 `start`；`HALT-2`（Anlogic）与 `HALT-3`（Vivado）差一拍源于 ROM 读延迟不同。
- **RAM 延时 delay.v**（u3-l2）：双口 RAM「先写后读」，`DELAY_TIME = 1<<(layer-1)`，五状态机 `IDLE→DELAY→OUT→TAIL→END`，端到端净延时为半周期。

**一个贯穿全讲的数量关系**：

\[
\text{PERIOD}=2^{\text{layer}},\qquad \text{反馈延时深度}=\text{PERIOD}/2=2^{\text{layer}-1}.
\]

`butterfly_general` 的核心工作，就是用 `layer` 一个参数把这个关系**统一表达**出来。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| [src/butterfly_general.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v) | 参数化蝶形层（主角） | 一个 `layer` 参数封装「状态机+S 控制+延时+握手+rotator_valid」 |
| [src/fft_32.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v) | 32 点层（首个用例） | 用 `layer=5` 例化 butterfly_general，再拼旋转因子与乘法器 |
| [src/butterfly.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly.v) | 蝶形算子 | 被 butterfly_general 内部例化，提供 B/D 与 1 拍对齐 |
| [src/delay.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v) | RAM 延时单元 | 被 butterfly_general 以 `layer(current_layer)` 例化 |
| [src/Rotator_address.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/Rotator_address.v) | 旋转因子寻址 | 在 fft_32 中**外部**拼接，由 `rotator_valid` 门控 |

> 注意：`Rotator_address`、ROM、`multiplier` **都不在** `butterfly_general` 内部——它们由每个 `fft_*` 包装层各自提供。理解「哪些被封装进来了、哪些留在外面」是本讲的关键。

## 4. 核心概念与源码讲解

### 4.1 设计动机：从手写 fft_8/fft_16 到参数化封装

#### 4.1.1 概念说明

先回顾 u4-l1、u4-l2 看到的现象：fft_2、fft_4、fft_8、fft_16 每个文件里都**各自手写**了一遍几乎相同的东西——

1. 一个 `IDLE → START → PROCESSING → END` 状态机；
2. 一个 `S` 控制信号，在 `PERIOD/2-1` 与 `PERIOD-1` 处翻转；
3. 一个反馈延时（fft_8 用寄存器、fft_16 起用 RAM）；
4. 一个数到 `HALT_FOR_NEXT_LAYER-3` 发脉冲的「下一级启动」计数器；
5. 一个数到 `WAIT_FOR_ROTATOR-1` 拉高的 `rotator_valid` 计数器。

这些逻辑**与具体点数无关**，只随 `layer` 改变数值。fft_16 之后还有 fft_32、fft_64、…、fft_16k 共 11 级，若继续照抄，会产生 11 段几乎雷同的代码——既难维护（改一个常量要改 11 处），也容易抄错。

`butterfly_general` 的出现就是为了消除这种重复：**把这五样东西收进一个用 `layer` 参数控制的模块**，fft_32 及以上的高层只需例化它、再补上旋转因子与乘法器即可。这就是工程上典型的「**先抄几遍摸清共性，再把共性抽成参数化模块**」的演进路径。

#### 4.1.2 核心流程

`butterfly_general` 在整个流水线层级里的定位：

```text
   fft_2 / fft_4 / fft_8        fft_16          fft_32 ~ fft_16k
   (各自手写，layer=1/2/3)    (手写，layer=4)   (全部改用 butterfly_general)
        │                         │                    │
        └──────────── 手写阶段 ────┘                    │
                                                      ▼
                                          butterfly_general #(layer)
                                          + Rotator_address + ROM + multiplier
```

它把「每级都要重复」的部分包成黑盒，对外只留**数据输入**、**前向数据输出 D**、**两个门控信号**和**首末脉冲**：

```text
        ┌─────────────────── butterfly_general(layer) ───────────────────┐
        │                                                                  │
data ──►│ A_real/A_img                                          D_real/D_img │──► (前向，待乘旋转因子)
start──►│ data_in_start                                   data_out_first │──► (首样本脉冲)
over ──►│ data_in_end                                      data_out_last  │──► (末样本脉冲)
        │                                            next_level_start │──► (驱动下一级 start)
        │                                                rotator_valid │──► (放行旋转因子)
        │  [内部：状态机 + S 控制 + butterfly + delay 反馈环 + 两个计数器]   │
        └──────────────────────────────────────────────────────────────────┘
```

#### 4.1.3 源码精读

`butterfly_general` 的模块声明与端口见 [src/butterfly_general.v:7-20](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L7-L20)。这组端口就是上面黑盒的精确写照：

- 输入侧：`data_in_start` / `data_in_end`（上一级握手进来的开始/结束）、`A_real` / `A_img`（本级数据输入，32 位）。
- 输出侧：`next_level_start`（驱动下一级）、`D_real` / `D_img`（前向数据输出）、`data_out_first` / `data_out_last`（首末样本脉冲）、`rotator_valid`（旋转因子门控）。

注意端口里**没有**旋转因子输入、也没有乘法结果输出——这两件事被故意留给了外层的 `fft_*` 包装。原因会在 4.5 讲：旋转因子 ROM 的实例名因层而异（`rotator_32_real`、`rotator_1k_real`…），无法用一个参数统一例化，所以索性把整条旋转因子通路留在包装层。

#### 4.1.4 代码实践

**实践目标**：亲手感受「重复」与「封装」的差别。

1. 打开 [src/fft_8.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_8.v) 与 [src/butterfly_general.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v)。
2. 在 fft_8.v 中找到状态机、`S8` 生成、`start4_counter`、`r_rotator_valid` 四段逻辑。
3. 在 butterfly_general.v 中找到对应的四段：状态机、`S` 生成、`next_level_start_counter`、`r_rotator_valid`。

**需要观察的现象**：两边的逻辑骨架几乎一致，区别只在于 fft_8 把数值写死（`PERIOD=8`），而 butterfly_general 用 `layer` 推导（`PERIOD=1<<layer`）。

**预期结果**：你能列出一个「手写 fft_8 ↔ butterfly_general」的逐行对照表，证明后者就是前者的参数化版本。

#### 4.1.5 小练习与答案

**练习 1**：如果把 fft_8 里那段「下一级启动」计数器原样搬到 fft_16，需要改哪几个数值？

> **答案**：把 `PERIOD` 从 8 改成 16，`HALT_FOR_NEXT_LAYER = 6+PERIOD/2` 随之从 10 变 14，启动脉冲触发点 `HALT-3` 从 7 变 11。其它（计数器位宽、状态名）可不动。这正是「只差参数」的证据。

**练习 2**：`butterfly_general` 的端口里为什么没有 `rotator_real` / `rotator_img` 输入？

> **答案**：因为旋转因子 ROM 的实例名随层变化、无法参数化，作者把整条旋转因子通路（`Rotator_address` + ROM + select 选择的寄存器）留在了 `fft_*` 包装层。`butterfly_general` 只通过 `rotator_valid` 这一个信号告诉包装层「现在该输出真实因子了」。

---

### 4.2 layer 参数体系：PERIOD、HALT、WAIT 与延时深度的统一导出

#### 4.2.1 概念说明

`butterfly_general` 的全部行为由**一个参数** `layer` 决定。模块一开头就用 `layer` 推导出三个常量与一个延时实例参数。理解这四处推导，就理解了「为什么改一个 `layer` 就能换一级」。

`layer` 的含义与前置讲义一致：它就是 Cooley-Tukey 分治的**当前层级编号**，满足 \(N=2^{\text{layer}}\)。例如 fft_32 的 layer=5、fft_1k 的 layer=10、fft_16k 的 layer=14。

#### 4.2.2 核心流程

由 `layer` 一处导出的参数体系：

\[
\text{PERIOD} = 2^{\text{layer}} = 1 \ll \text{layer}
\]

\[
\text{HALT\_FOR\_NEXT\_LAYER} = 6 + \text{PERIOD}/2 = 6 + 2^{\text{layer}-1}
\]

\[
\text{WAIT\_FOR\_ROTATOR} = \text{PERIOD} - 2 = 2^{\text{layer}} - 2
\]

\[
\text{延时实例}=\texttt{delay}\ \#(\texttt{.layer}(\text{current\_layer}))
\]

四个导出量的物理含义（u3-l2、u3-l3 已分别讲过，这里只做归纳）：

| 导出量 | 公式 | 物理含义 |
|---|---|---|
| `PERIOD` | \(2^{\text{layer}}\) | 本级处理一个完整周期的样本数；也是 S 信号方波的周期 |
| `DELAY_TIME`（在 delay 内部） | \(2^{\text{layer}-1}\) | 反馈延时深度 = 半周期 |
| `HALT_FOR_NEXT_LAYER` | \(6+\text{PERIOD}/2\) | 本级启动后，经此拍数触发下一级 start；6=固定流水开销，PERIOD/2=建立期 |
| `WAIT_FOR_ROTATOR` | \(\text{PERIOD}-2\) | 本级启动后，经此拍数才让旋转因子有效 |

#### 4.2.3 源码精读

参数定义集中在 [src/butterfly_general.v:23-25](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L23-L25)，三行就把上面三个常量算出：

```verilog
parameter current_layer        = layer;
parameter PERIOD               = 1<<layer;
parameter HALT_FOR_NEXT_LAYER  = 6 + (PERIOD)/2;
```

`WAIT_FOR_ROTATOR` 单独写在旋转因子段 [src/butterfly_general.v:148](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L148)（`PERIOD - 2`）。

延时实例用 `current_layer` 作参数，见 [src/butterfly_general.v:208-209](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L208-L209)，`delay` 内部再用它算出 `DELAY_TIME = 1<<(layer-1)`（[src/delay.v:17](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v#L17)）。两层参数传递形成链路：`butterfly_general.layer → delay.layer → DELAY_TIME`。

#### 4.2.4 代码实践

**实践目标**：代入具体数值，验证参数链。

1. 假设例化 `butterfly_general #(.layer(5))`（即 fft_32）。
2. 手算 `PERIOD`、`HALT_FOR_NEXT_LAYER`、`WAIT_FOR_ROTATOR`、delay 内部 `DELAY_TIME`。
3. 再代入 `layer=10`（fft_1k）算一遍。

**需要观察的现象**：所有量都随 `layer` 单调增长；延时深度 `DELAY_TIME` 增长最快（\(2^{\text{layer}-1}\)）。

**预期结果**：

| layer | PERIOD | HALT | WAIT | DELAY_TIME |
|---|---|---|---|---|
| 5（fft_32） | 32 | 22 | 30 | 16 |
| 10（fft_1k） | 1024 | 518 | 1022 | 512 |

（HALT = 6 + PERIOD/2；fft_32 为 \(6+16=22\)，fft_1k 为 \(6+512=518\)。）

#### 4.2.5 小练习与答案

**练习 1**：fft_16k（layer=14）的 `HALT_FOR_NEXT_LAYER` 是多少？这意味着下一级要在本级启动后多久才启动？

> **答案**：\(\text{HALT}=6+2^{13}=6+8192=8198\)。即本级 `start` 后约 8198 个时钟，下一级才被触发——因为 fft_16k 的延时 RAM 要先攒满 8192 个样本（建立期），加上 6 拍固定开销。这正是「大点数层排在流水线最前、最先启动」的原因（u1-l4）。

**练习 2**：为什么 `WAIT_FOR_ROTATOR = PERIOD - 2` 而不是 `PERIOD/2`？

> **答案**：`WAIT_FOR_ROTATOR` 控制的是「旋转因子何时开始有效」，要让旋转因子覆盖蝶形 D 输出的有效区间，而不是仅在建立期之后立刻有效；`-2` 是为了抵消 ROM 读取与寄存打拍的延迟，使真实因子与 D 输出在同一拍对齐。详见 u3-l3 的逐拍推导。

---

### 4.3 数据通路与反馈环：状态机驱动 butterfly + delay 的 B↔C 闭环

#### 4.3.1 概念说明

`butterfly_general` 内部最核心的是一条 **SDF 反馈数据通路**：状态机控制全局节奏，`butterfly` 做加减，`delay` 做「存半周期再放行」，二者用 `B↔C` 连成一个闭环。这条通路把 u2-l1（蝶形）、u3-l2（延时）两个独立算子在**参数化层**里组装起来。

回顾两个算子的角色（来自前置讲义）：

- `butterfly`：两个输入 `A`（本级新数据）、`C`（反馈回来的旧数据）；两个输出 `D`（前向，送出本级）、`B`（下支，进延时反馈）。`S=1` 且 `enable` 时计算 `added=A+C`、`subtracted=C-A`，结果寄存一拍，靠 `S_1d` 选择直通或计算值（[src/butterfly.v:110-113](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly.v#L110-L113)）。
- `delay`：`din` 写入双口 RAM，延时 \(2^{\text{layer}-1}\) 拍后从 `dout` 读出，五状态机管理「建立→输出→排空」。

#### 4.3.2 核心流程

`butterfly_general` 内部数据通路（这是本讲最重要的一张图）：

```text
   A_real/A_img (上一级送来的本级数据)
         │
         ▼
      ┌─────────── butterfly ───────────┐
      │  A(新), C(旧=延时后)            │
      │   │                             │
      │   ├──► D = (S_1d? added : C_1d) ─┼──► D_real/D_img (前向输出端口)
      │   └──► B = (S_1d? subtracted: A_1d)┼──► B_real/B_img
      └──────────────────────────────────┘
                                          │ (下支，反馈)
                                          ▼
                                   ┌── delay(layer) ──┐
                                   │ din = B          │
                                   │ dout = C ────────┼──► C_real/C_img (回到 butterfly 的 C 输入)
                                   │ wea = r_wea      │
                                   └──────────────────┘
                                          │
                                          ▼
                              out_first/out_last ──► data_out_first/data_out_last
```

要点：

1. **状态机** `IDLE→START→PROCESSING→END` 把工作分四阶段；`data_in_start` 触发 `IDLE→START`，`data_in_end` 触发 `PROCESSING→END`。
2. **`butterfly_enable`** 在 `START/PROCESSING` 期间为 1，让蝶形真正做加减。
3. **`S` 控制信号**：`data_in_start` 当拍置 0，随后在 `S_counter` 数到 `PERIOD/2-1` 与 `PERIOD-1` 时翻转，形成占空比 50% 方波，驱动蝶形上下支按半周期切换。
4. **`r_wea`**（延时写使能）：在 `START/PROCESSING` 期间为 1，让 `delay` 在工作阶段持续把 B 写入 RAM。
5. **闭环**：`butterfly.B → delay.din → delay.dout(C) → butterfly.C`，B 经半周期延时后变成 C 回流，完成 SDF 单路反馈。

#### 4.3.3 源码精读

**(1) 状态机** —— [src/butterfly_general.v:36-64](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L36-L64)：四状态转移，`data_in_start` 启动、`data_in_end` 结束，结构与 fft_8/fft_16 完全一致，只是搬进模块里。

**(2) 蝶形使能与 S 计数** —— `butterfly_enable` 见 [src/butterfly_general.v:67-79](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L67-L79)；`S_counter` 见 [src/butterfly_general.v:81-95](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L81-L95)，在 `START/PROCESSING` 内 0 到 `PERIOD-1` 循环计数。

**(3) S 信号翻转点** —— [src/butterfly_general.v:97-111](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L97-L111)，关键一行：

```verilog
end else if (S_counter == PERIOD/2-1 | S_counter == PERIOD-1) begin
    S <= ~S;
```

这与 fft_4（u4-l1）、fft_8（u4-l2）的写法逐字相同，只是 `PERIOD` 由 `layer` 推导。

**(4) 延时写使能与延时实例** —— `r_wea` 见 [src/butterfly_general.v:195-205](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L195-L205)；`delay` 例化见 [src/butterfly_general.v:208-219](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L208-L219)，注意端口连接正好画出了闭环：`.din_real(w_B_real)`（蝶形 B 写入）、`.dout_real(w_C_real)`（延时后读出），而 `w_C_real` 又接到下面蝶形的 `.C_real`。

**(5) 蝶形实例** —— [src/butterfly_general.v:222-235](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L222-L235)：`.A_real(A_real)`（模块输入）、`.C_real(w_C_real)`（来自延时）、`.B_real(w_B_real)`（送入延时）、`.D_real(w_D_real_tmp)`（送至输出）。`.S(S)`、`.enable(butterfly_enable)` 把上面的控制接进来。

**(6) 最终输出** —— [src/butterfly_general.v:236-240](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L236-L240)：把蝶形 D 与延时的首末脉冲 `assign` 到模块端口。

#### 4.3.4 代码实践

**实践目标**：在源码里把 B↔C 闭环「指」出来。

1. 在 [src/butterfly_general.v:222-235](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L222-L235) 找到蝶形的 `.B_real(w_B_real)` 与 `.C_real(w_C_real)`。
2. 在 [src/butterfly_general.v:208-219](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L208-L219) 找到 delay 的 `.din_real(w_B_real)` 与 `.dout_real(w_C_real)`。

**需要观察的现象**：`w_B_real` 同时出现在蝶形输出与延时输入；`w_C_real` 同时出现在延时输出与蝶形输入——这就是 `B → delay → C` 闭环。

**预期结果**：你能用三个箭头画出闭环 `butterfly.B → delay.din → delay.dout(C) → butterfly.C`，并指出 `D` 是唯一的前向出口。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `butterfly_enable` 在 `STATE_END` 就清 0？若不清会怎样？

> **答案**：`END` 表示本级所有数据已处理完（`data_in_end` 已到），若继续使能蝶形，会对无效数据做加减，污染反馈 RAM 的内容，影响下一次启动。清 0 是为了「封存」状态、等待下一次 `data_in_start`。

**练习 2**：闭环里的 `delay` 写使能 `r_wea` 由谁驱动？为什么不能用 `butterfly_enable` 直接代替？

> **答案**：`r_wea` 由 [src/butterfly_general.v:195-205](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L195-L205) 单独产生，条件含 `data_in_start | STATE_START | STATE_PROCESSING`。它与 `butterfly_enable` 几乎同相但语义不同：延时需要在数据真正流入的整段（含 `data_in_start` 当拍）就开始写，以保证第一拍样本进入反馈；用 `butterfly_enable` 直接代替会漏掉 `data_in_start` 当拍的写入。

---

### 4.4 对外握手封装：next_level_start 与 rotator_valid 的产生

#### 4.4.1 概念说明

`butterfly_general` 对外暴露的两个门控信号 `next_level_start` 和 `rotator_valid`，是整条流水线级联与旋转因子对齐的关键。它们的逐拍推导已在 u3-l3 讲透，本节只关注「**它们如何被封装进这个模块**」——即在 fft_8/fft_16 里散落在文件各处的两段计数逻辑，现在被收进 `butterfly_general` 的固定段落，随 `layer` 自动调参。

两个信号的职责回顾：

- **`next_level_start`**：本级启动后，经过固定拍数向下一级发一个单拍 `start` 脉冲。它取代了 fft_4/fft_8 里的 `start4` / `start_counter`。
- **`rotator_valid`**：本级启动后，经过固定拍数拉高，告诉包装层「现在开始输出真实旋转因子」；拉高之前包装层默认输出 \(W=1\)（实部 `1<<16`、虚部 0），即「直通不旋转」。

#### 4.4.2 核心流程

两个计数器都在 `STATE_START | STATE_PROCESSING` 期间递增：

```text
本级 start
   │
   ▼
 next_level_start_counter: 0 → 1 → … → 数到 HALT_FOR_NEXT_LAYER
   │                                   │
   │  当计数 == HALT-3（vivado）        │
   ▼                                   ▼
 r_next_level_start 拉高 1 拍       (计数封顶保持)

 r_count_rotator:        0 → 1 → … → 数到 WAIT_FOR_ROTATOR-1
   │                                   │
   │  当计数 == WAIT_FOR_ROTATOR-1      │
   ▼                                   ▼
 r_rotator_valid 拉高（之后持续有效）  (计数封顶保持)
```

关键常量（来自 4.2）：`HALT_FOR_NEXT_LAYER = 6 + PERIOD/2`，`WAIT_FOR_ROTATOR = PERIOD - 2`。

#### 4.4.3 源码精读

**(1) next_level_start 的产生** —— [src/butterfly_general.v:115-144](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L115-L144)。其中触发判定见 [src/butterfly_general.v:123](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L123)：

```verilog
//HALT_FOR_NEXT_LAYER-2 is used for anlogic version
//HALT_FOR_NEXT_LAYER-3 is used for vivado version
if(next_level_start_counter == HALT_FOR_NEXT_LAYER-3) begin
    r_next_level_start <= 1;
```

注释里同时给出 `-2`（Anlogic）与 `-3`（Vivado）两个版本，差一拍源于两个平台的旋转因子 ROM 读延迟不同（u3-l3、u5-l3 详述）。计数器本身在 [src/butterfly_general.v:131-143](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L131-L143)，数到 `HALT_FOR_NEXT_LAYER` 后封顶。`assign next_level_start = r_next_level_start`（[L144](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L144)）。

**(2) rotator_valid 的产生** —— [src/butterfly_general.v:147-180](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L147-L180)。`WAIT_FOR_ROTATOR = PERIOD-2`（[L148](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L148)）；`r_count_rotator` 数到 `WAIT_FOR_ROTATOR-1` 后封顶（[L153-L167](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L153-L167)）；当计数到位，`r_rotator_valid` 置 1（[L169-L179](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L169-L179)），`assign rotator_valid = r_rotator_valid`（[L180](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L180)）。

这两个信号是 `butterfly_general` 与外部世界「对话」的咽喉：前者驱动流水线下游，后者驱动旋转因子通路。

#### 4.4.4 代码实践

**实践目标**：对比 fft_8 手写版与 butterfly_general 封装版的等价性。

1. 在 [src/fft_8.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_8.v) 中找到产生 `start4` 脉冲与 `r_rotator_valid` 的两段 `always`。
2. 在 [src/butterfly_general.v:115-180](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L115-L180) 找到对应的 `next_level_start_counter` 与 `r_count_rotator`。

**需要观察的现象**：两段逻辑的计数终点都写成 `HALT_FOR_NEXT_LAYER-3` 与 `WAIT_FOR_ROTATOR-1`，结构一致。

**预期结果**：你能下结论——fft_8 的握手逻辑是 butterfly_general 的一个「`layer=3` 特化实例」，封装没有改变行为，只改变了写法。

#### 4.4.5 小练习与答案

**练习 1**：`HALT_FOR_NEXT_LAYER-3` 中的 `-3` 若误写成 `-2`，在 Vivado 平台上会发生什么？

> **答案**：`-2` 会让下一级 `start` 提前一拍触发，下一级蝶形与延时会比本级数据早一拍开始工作，导致 B↔C 配对错位、最终频谱错位。这正是注释强调「`-2` 用于 Anlogic、`-3` 用于 Vivado」的原因——两个平台 ROM 读延迟不同，触发点必须相应调整（u5-l3）。

**练习 2**：`r_rotator_valid` 一旦拉高，会保持多久？

> **答案**：从计数到位起持续为 1，直到状态机离开 `PROCESSING`（`r_count_rotator` 在非 `START/PROCESSING` 时清 0，于是 `r_rotator_valid` 也跟着回 0）。即在整个有效输出区间内它都有效，包住所有需要真实旋转因子的拍。

---

### 4.5 fft_32 用例：butterfly_general × Rotator_address × ROM × multiplier 高层模板

#### 4.5.1 概念说明

`butterfly_general` 本身**不能独立工作**——它只管「蝶形 + 反馈延时 + 状态/握手」，不管「乘旋转因子」。要凑成完整的一级 FFT，还差三块：旋转因子寻址（`Rotator_address`）、旋转因子存储（ROM IP）、复数乘法（`multiplier`）。`fft_32.v` 就是把这三块与 `butterfly_general` 拼起来的**包装层**，也是 fft_32 起所有高层模块（fft_64、fft_128、…、fft_1k、fft_16k）的同构模板——它们之间的差别，通常只是 `layer` 参数与 ROM 实例名。

#### 4.5.2 核心流程

fft_32 内部四块拼接的数据通路（这是本讲的「组装图」）：

```text
start ──┐                                       ┌── start_next (→下一级)
over ──┐│                                       │
data ──┼┴─► ┌── butterfly_general #(layer=5) ──┐ ├─► D_real/D_img ──┐
       │   │                                    │ │                  │
       │   │  [状态机+butterfly+delay+两个计数]  │ ├─► rotator_valid ─┐│
       │   │                                    │ │                  ││
       │   └────────────────────────────────────┘ │                  ││
       │                                          │                  ││
       │   ┌── Rotator_address #(layer=5) ────────◄┘                  ││
       │   │  rotator_valid → 地址 w_rotator_addr, select w_select    ││
       │   └────┬──────────────────────────────────                   ││
       │        ▼ addr                                                 ││
       │   ┌── rotator_32_real / rotator_32_img (ROM) ──┐              ││
       │   │   addr → dout (实/虚, 18 位)              │              ││
       │   └────┬───────────────────────────────────────┘              ││
       │        ▼ ROM 原始值                                            ││
       │   ┌── select mux (r_rotator_real/img) ─────────────────────────┘│
       │   │  rotator_valid & ~select → 取 ROM; 否则 → W=1(1<<16, 0)     │
       │   └────┬───────────────────────────────────────────────────────┘
       │        │ c/d (旋转因子, 18 位)                                  │
       │        ▼                                                       ▼
       │       ┌─────────── multiplier ───────────┐   a/b = D(数据,32位)
       │       │ (a+jb)(c+jd), >>16, 截断到 32 位  │
       │       └────┬──────────────────────────────┘
       │            ▼
       └────────► data_out_real / data_out_img
```

数据流的「一句话总结」：`butterfly_general` 产出**待乘旋转因子的前向数据 D**与**门控信号 rotator_valid**；`Rotator_address` 据门控生成 ROM 地址与 select；ROM 读出旋转因子；select 在「真实因子」与「\(W=1\)」间二选一寄存；最后 `multiplier` 把 D × 旋转因子，截断输出即为 `data_out`。

#### 4.5.3 源码精读

**(1) butterfly_general 的例化** —— [src/fft_32.v:30-44](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v#L30-L44)，`#(.layer(5))`。注意端口连接：
- `.data_in_start(start)`、`.data_in_end(over)`：上一级握手进来；
- `.A_real(data_in_real)`：本级数据输入；
- `.D_real(w_D_real)`：前向数据输出（**待乘旋转因子**，正是下面 multiplier 的 a/b 来源）；
- `.rotator_valid(w_rotator_valid)`：门控信号（**正是下面 Rotator_address 的输入**）；
- `.next_level_start(w_next_level_start)`：驱动下一级，经 [L45](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v#L45) `assign start_next = w_next_level_start` 送出。

**(2) Rotator_address 的例化** —— [src/fft_32.v:54-61](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v#L54-L61)，`#(.layer(5))`，关键连接 `.rotator_valid(w_rotator_valid)`（来自 butterfly_general），输出 `w_rotator_addr` 与 `w_select`。其内部：`rotator_valid` 有效时地址计数器自增（[src/Rotator_address.v:22-32](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/Rotator_address.v#L22-L32)），地址高位作 select（[src/Rotator_address.v:34-45](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/Rotator_address.v#L34-L45)）。

**(3) ROM 实例** —— [src/fft_32.v:63-73](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v#L63-L73)，实/虚分存两块（`rotator_32_real` / `rotator_32_img`），共用地址 `w_rotator_addr`，各输出 18 位。**实例名带 `_32_`** 正是各层不同、无法参数化的原因。

**(4) select 二选一寄存器** —— [src/fft_32.v:74-92](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v#L74-L92)：默认（复位或 `rotator_valid` 无效）输出 \(W=1\)（`r_rotator_real <= 1<<16; r_rotator_img <= 0`）；`rotator_valid` 有效时，`w_select` 为真仍补 \(W=1\)、为假才取 ROM 真实值。这把 u3-l1 的「前半周期真实因子、后半周期 \(W=1\)」落到了寄存器上。

**(5) multiplier 的例化** —— [src/fft_32.v:97-109](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v#L97-L109)：
- `.a(w_D_real)`、`.b(w_D_img)`：数据端，接 butterfly_general 的 D 输出；
- `.c(r_rotator_real)`、`.d(r_rotator_img)`：旋转因子端，接上面的 select 寄存器；
- `.rstn(~rst)`：高有效复位翻转成低有效（u2-l2）；
- `.data_real_trunc(w_out_real_32)`：只取截断输出，全精度端口 `.data_real()`、`.data_img()` 悬空。

**(6) 模块输出** —— [src/fft_32.v:111-112](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v#L111-L112)：乘法器截断输出直接 `assign` 成 `data_out_real/img`。

> **模板同构性**：fft_1k、fft_16k 等高层模块的结构与 fft_32 完全一致，差别仅在 `#(.layer(...))` 的数值（5/10/14）与 ROM 实例名（`rotator_32_real` → `rotator_1k_real` → `rotator_16k_real`）。下一讲 u4-l4 会集中对比这些高层模块。

#### 4.5.4 代码实践

**实践目标**：本讲的指定实践——画出 fft_32 内部连接框图，回答三个接线问题。

操作步骤：

1. 打开 [src/fft_32.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v)。
2. 回答下面三个问题（参考 4.5.3 的行号）：
   - **问题 A**：`butterfly_general` 的 `D_real` / `D_img`（[L39-L40](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v#L39-L40)）接到哪里？
   - **问题 B**：`rotator_valid`（[L43](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v#L43)）接到 `Rotator_address` 的哪个端口（[L54-L61](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v#L54-L61)）？
   - **问题 C**：multiplier 的输出（[L107-L108](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v#L107-L108)）如何成为 `data_out`？

**参考答案**：
- **A**：`w_D_real` / `w_D_img` 接到 `multiplier` 的 `.a` / `.b`（[L98-L99](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v#L98-L99)），即复数乘法的数据端——D 是「待乘旋转因子的前向数据」。
- **B**：`w_rotator_valid` 接到 `Rotator_address` 的 `.rotator_valid`（[L58](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v#L58)），作为地址计数器的总开关——门控有效才开始扫地址。
- **C**：`multiplier` 的截断输出 `w_out_real_32` / `w_out_img_32` 经 [L111-L112](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v#L111-L112) 直接 `assign` 成 `data_out_real` / `data_out_img`，即乘法器输出 = 本级最终输出。

**需要观察的现象 / 预期结果**：把上述三条连线画进 4.5.2 的组装图，你会得到一张完整闭环——`butterfly_general` 既提供数据 D，又提供门控 rotator_valid，二者分别走向 multiplier 的 a/b 与 Rotator_address 的输入，最终在 multiplier 汇合。

> 本实践为「源码阅读型实践」：不需要运行仿真，只需在源码里把信号连线指出来。若需波形验证，可参考 tb/fft_8_tb.v 的结构（u5-l2）。

#### 4.5.5 小练习与答案

**练习 1**：如果把 fft_32 改造成 fft_64，需要改哪几处？

> **答案**：(1) `butterfly_general #(.layer(5))` 改为 `#(.layer(6))`；(2) `Rotator_address #(.layer(5))` 改为 `#(.layer(6))`；(3) ROM 实例从 `rotator_32_real/img` 换成 `rotator_64_real/img`（需重新生成对应 .coe 的 ROM IP）；(4) 模块名、内部 `PERIOD=32` 改为 64。主体逻辑无需改动——这正是封装的价值。

**练习 2**：为什么 `multiplier` 的全精度输出 `.data_real()` / `.data_img()` 要悬空？

> **答案**：因为后续级联只接受 32 位数据，而全精度输出是 50 位（u2-l2）。保留全精度端口只是 IP 提供的可选项，本设计用不到，故悬空；只用 `.data_real_trunc`（截断到 32 位，且已 `>>16` 抵消定点放大）作为本级输出。

---

## 5. 综合实践

**任务：把 fft_32 的四件套连接图补全为一张「信号级」完整数据通路图，并据此推断 fft_1k 的写法。**

1. 在一张纸上（或文本编辑器里）画出 fft_32 的完整框图，要求标出**每一条 signal 名**与**每一个实例的 `layer` 参数**，至少包含：
   - `butterfly_general #(.layer(5))` 的 9 个对外信号分别连到哪里；
   - `Rotator_address` 的 `rotator_valid` 输入与 `rotator_addr` / `select` 输出；
   - 两块 ROM 的地址与数据线；
   - select 二选一寄存器 `r_rotator_real/img` 的默认值与取值条件；
   - `multiplier` 的 a/b/c/d 来源与截断输出走向。
2. 据此推断 [src/fft_1k.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_1k.v)（layer=10）应有哪些改动，列出「相同部分」与「不同部分」两张清单。
3. （进阶）思考：既然 fft_32~fft_16k 结构如此同构，作者为什么没有把它们再封装成一个更高层的 `fft_layer #(layer)`，而是为每个点数各写一个文件？把你的猜想写下来（提示：从 ROM IP 实例名能否参数化、综合工具对 IP 例化的限制两方面考虑）。

**预期结果**：

- 你得到一张可复用的「fft 高层模块装配图」，4.5.2 的组装图是它的简版；
- 「不同部分」清单应至少包含：`layer` 参数值、ROM 实例名、`PERIOD` 常量、模块名与端口名后缀；
- 第 3 问的合理猜想：因为厂商 ROM IP 的实例名和 .coe 内容因层而异、Verilog 无法用参数「换 IP 名」，所以每个点数仍需单独一个文件来例化各自的 ROM；`butterfly_general` 已经吃掉了「可以参数化」的那部分共性，剩下的差异恰恰是「不可参数化」的部分。这将在下一讲 u4-l4 集中验证。

> 本综合实践为「源码阅读 + 设计推断」型，无需运行；若想动手，可在仿真器里把 fft_32 的 `w_D_real`、`w_rotator_valid`、`w_rotator_addr`、`r_rotator_real`、`w_out_real_32` 挂波形，对照本图观察一次完整 `start→over` 周期里这些信号的先后顺序（仿真框架见 u5-l2）。

## 6. 本讲小结

- `butterfly_general` 是把 fft_8/fft_16 里反复出现的「**状态机 + S 控制 + RAM 延时 + 下一级启动 + rotator_valid**」五样逻辑收进**一个 `layer` 参数控制**的模块，消除了 11 个高层模块的重复代码。
- 全部行为由 `layer` 一处导出：`PERIOD = 1<<layer`、`HALT_FOR_NEXT_LAYER = 6+PERIOD/2`、`WAIT_FOR_ROTATOR = PERIOD-2`、延时实例 `delay #(.layer(current_layer))`。
- 内部核心是一条 **SDF 反馈闭环**：状态机驱动 `butterfly` 与 `delay`，蝶形 B 输出经 RAM 延时半周期后作为 C 回流，`D` 作为唯一前向出口送出。
- 对外暴露两个门控信号：`next_level_start`（计数到 `HALT-3` 发单拍脉冲驱动下一级）与 `rotator_valid`（计数到 `WAIT_FOR_ROTATOR-1` 拉高、放行旋转因子）；二者在 u3-l3 已逐拍推导，本讲只展示其封装位置。
- `fft_32` 是高层模板：`butterfly_general`（提供 D 与 rotator_valid）+ `Rotator_address`（生成地址/select）+ ROM（存因子）+ `multiplier`（D × 因子）四块拼接；fft_1k、fft_16k 同构，只差 `layer` 与 ROM 实例名。
- 关键分工：**可参数化的共性**（蝶形核心）进 `butterfly_general`，**不可参数化的差异**（每层 ROM 实例名）留在各 `fft_*` 包装层——这是后续 u4-l4 对比高层模块、u5-l3 平台移植的认知基础。

## 7. 下一步学习建议

- **u4-l4 高层模块 fft_32~fft_16k 与参数化复用**：把本讲的「同构模板」放到放大镜下，集中对比 fft_32 / fft_1k / fft_16k 三者，验证「只差 `layer` 与 ROM 名」的结论，并看 `delay_1k_plus` 如何应对更大的延时深度。建议先做本讲综合实践第 2 步的「相同/不同清单」，再去 u4-l4 核对。
- **u5-l2 仿真与 testbench**：本讲的连线都是静态阅读，若想在波形里看 `butterfly_general` 的 `D` 与 `rotator_valid` 如何先后生效，可学完 u5-l2 后用 `tb/fft_general_tb.v` 跑一次仿真。
- **u5-l3 双平台移植**：本讲提到的 `HALT_FOR_NEXT_LAYER-3`（Vivado）与 `-2`（Anlogic）之差，在移植时必须重新校准，u5-l3 会给出完整 checklist。
- **延伸阅读**：可顺手翻 [src/fft_1k.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_1k.v)，对照本讲 4.5 的 fft_32 装配图，提前感受「同构」的震撼——你会发现 fft_1k 几乎就是 fft_32 改了几个数字。
