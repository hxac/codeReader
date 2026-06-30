# 面向 ASIC 的设计取舍

## 1. 本讲目标

学完本讲后，你应该能够：

- 用「面积 / 时序 / 功耗」三角的视角，重新审视前面几讲已经读过的 AES 核源码。
- 说清楚本工程里至少三处「用时间换面积」的取舍点各自牺牲了什么、换回了什么。
- 看懂贯穿全工程的「统一写使能寄存器 + 异步低有效复位」时钟风格为何有利于低功耗综合。
- 估算一次 AES 加密的时钟周期数与吞吐率，并定位设计中的关键路径（critical path）。
- 评估若要把本核改造成「流水线高吞吐」版本，需要动哪些模块、会破坏哪些共享关系。

本讲不再引入新的源码文件，而是把 u2、u3 前几讲读过的模块（`aes_core`、`aes_encipher_block`、`aes_key_mem`）当作素材，从 **ASIC 综合的工程视角** 重新解读它们「为什么这样写」。这是从「读懂算法」走向「能改硬件」的关键一步。

## 2. 前置知识

在进入源码分析前，先用最朴素的语言建立三个 ASIC 设计的基本观念。

### 2.1 面积、时序、功耗的三角取舍

芯片设计里有一句老话：**面积（Area）、速度（Speed/时序）、功耗（Power）三者不能同时占全**。

- **面积**：芯片上逻辑门和寄存器的总量，直接决定成本。复制一份硬件能把一件事做得更快，但面积翻倍。
- **时序**：一个时钟周期能完成多少运算，决定最高时钟频率 \(f_{\text{clk}}\)。组合逻辑链越长，关键路径越深，频率越低。
- **功耗**：动态功耗主要来自信号翻转（toggle）。寄存器越多、翻转越频繁，功耗越高。

本工程的 README 开篇就宣称「低功耗、高吞吐、短关键路径」三件好事，本讲的任务就是到源码里逐一找出**为这三件事付出的代价**。

> 参考：[README.md:1-1](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/README.md#L1-L1) —— README 第一句给出项目定位与「0.06 Gbps」吞吐数字（该数字的测量条件 README 未说明，后文会专门讨论）。

### 2.2 「用时间换面积」是什么意思

一块硬件（例如一个 S-box 查表电路），如果只在 1 个时钟周期里用 1 次，其余周期闲置，那就是浪费。反过来，**让多个使用者轮流（分时）复用同一块硬件**，可以把硬件数量压到 1 份——但代价是使用者必须排队，总耗时变长。

这就是「用时间换面积」：**用更多的时钟周期，换取更少的硬件副本**。本工程把这一思想用到了极致。

### 2.3 动态功耗与「写使能」

CMOS 电路的动态功耗近似为：

\[ P_{\text{dyn}} \approx \alpha \cdot C \cdot V_{DD}^2 \cdot f_{\text{clk}} \]

其中 \(\alpha\) 是信号翻转率。如果某个寄存器这一拍并不需要更新，却仍被写入新值（哪怕新值与旧值相同），仍可能引起内部节点翻转、增加 \(\alpha\)。**给每个寄存器配一个写使能（write enable）**，让它在「不需要变」的周期保持原值，是降低动态功耗的标准手法。现代综合工具还会把数据写使能进一步编译成**时钟门控（clock gating）**，连时钟翻转都省掉。本工程的 `_we` 信号正是为此而设。

## 3. 本讲源码地图

本讲复用前几讲已精读的文件，不引入新文件：

| 文件 | 本讲用它做什么 |
| --- | --- |
| [rtl/aes_core.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v) | 看「共享单个 S-box」与「加解密通路二选一」两处资源共享。 |
| [rtl/aes_encipher_block.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v) | 看「逐字 SubBytes 拆 4 拍」「单套轮逻辑迭代复用」「关键路径 mixcolumns」。 |
| [rtl/aes_key_mem.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_key_mem.v) | 看「轮密钥在 init 阶段一次性算好存表」的存储/时间取舍。 |
| [README.md](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/README.md) | 取吞吐率（0.06 Gbps）与「低功耗/短关键路径」的设计目标。 |

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **4.1 S-box 资源共享** —— 全核只例化 1 个正向 S-box。
2. **4.2 逐字 SubBytes 的面积/时序权衡** —— 128 位 SubBytes 拆成 4 拍。
3. **4.3 寄存器写使能与时钟风格** —— 统一 `_we` + 异步低有效复位。
4. **4.4 吞吐与关键路径** —— 周期数、吞吐率公式与最长组合路径。

### 4.1 S-box 资源共享

#### 4.1.1 概念说明

AES 算法里有两个「消费者」会用到正向 S-box（即 SubWord / SubBytes 的查表）：

- **密钥扩展**（`aes_key_mem`）：每生成一把新轮密钥，都要对前一把密钥的末字做一次 SubWord。
- **加密主循环**（`aes_encipher_block`）：每一轮对状态做 SubBytes。

朴素做法是给每个消费者各配一个 S-box 实例。本工程的取舍是：**全核只例化 1 个正向 S-box**（`sbox_inst`），让两个消费者分时共享。换回的好处是面积省一半以上（S-box 是 256 项常量表，占不少门）；付出的代价是两个消费者**不能同时工作**——这正是 u3-l1 总结的「init 与 next 必须分成两个主机触发阶段」的根本原因。

注意：解密用的**逆向** S-box 不参与这套共享，它被私挂在 `aes_decipher_block` 内部（`inv_sbox_inst`）。这是因为逆向 S-box 只有解密一个消费者，没有共享的必要（详见 u2-l2）。

#### 4.1.2 核心流程

共享靠 `aes_core` 里的两个组合逻辑块实现：

1. **`sbox_mux`**（多路选择）：根据当前是否处于 `init`（密钥扩展）阶段，决定把唯一的 S-box 的输入端 `muxed_sboxw` 接到 `keymem_sboxw` 还是 `enc_sboxw`。
2. S-box 的输出 `new_sboxw` 是一根总线，**同时**接回 `key_mem` 和 `enc_block` 的 `new_sboxw` 端口——消费者自己决定在当拍是否采纳。

由于密钥扩展（init）与加密（next）由 `aes_core_ctrl` 的 FSM 互斥调度（同一时刻只可能处在 `CTRL_INIT` 或 `CTRL_NEXT` 之一），两个消费者天然不会抢同一拍的 S-box，分时复用安全。

#### 4.1.3 源码精读

唯一的 S-box 例化——全工程就这一处正向 S-box：

[rtl/aes_core.v:138-138](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L138-L138) —— 例化唯一的 `aes_sbox sbox_inst`，输入是经多路选择后的 `muxed_sboxw`，输出 `new_sboxw` 回馈所有消费者。

选择「谁先用 S-box」的组合块：

[rtl/aes_core.v:184-194](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L184-L194) —— `sbox_mux`：`init_state` 为真（密钥扩展阶段）时把 S-box 输入接 `keymem_sboxw`，否则接 `enc_sboxw`（加密阶段）。这就是「分时复用」的总开关。

`init_state` 这个选择信号由总控 FSM 产生：

[rtl/aes_core.v:244-280](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L244-L280) —— `aes_core_ctrl` 在 `CTRL_INIT` 态里把 `init_state=1`，在 `CTRL_NEXT` 态里把 `init_state=0`，保证两个消费者按阶段互斥，不会在 S-box 上冲突。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：亲手验证「两个消费者共享一个 S-box、且靠 `init_state` 互斥」这条链路闭合。

**操作步骤**：

1. 在 `rtl/aes_core.v` 里找到 `sbox_inst`（L138），记下它的输入 `muxed_sboxw` 与输出 `new_sboxw`。
2. 用编辑器搜索 `new_sboxw`，确认它被同时连到了 `enc_block`（L97）和 `keymem`（L134）两处 `.new_sboxw(...)`。
3. 再搜索 `muxed_sboxw`，确认它的赋值只在 `sbox_mux`（L184-194）里出现一次，且分支条件就是 `init_state`。

**需要观察的现象**：你会看到「1 个 S-box 输出 → 2 个消费者输入」的扇出，但「2 个消费者输出 → 1 个 S-box 输入」却经过 `sbox_mux` 二选一收敛。这种**输出广播、输入多路选择**的拓扑，正是分时共享的硬件特征。

**预期结果**：能画出 `keymem_sboxw` 与 `enc_sboxw` 经 `sbox_mux` → `sbox_inst` → `new_sboxw`（同时回喂两者）的单线图，并标注 `init_state` 控制着多路选择器。

#### 4.1.5 小练习与答案

**练习 1**：如果把这套共享拆掉，给 `key_mem` 和 `enc_block` 各配一个独立的正向 S-box，能省掉什么、又会增加什么？

> **参考答案**：能省掉 `sbox_mux` 这个多路选择器，并且 `init` 与 `next` 不再因为抢 S-box 而互斥——理论上可以让密钥扩展和加密在某些流水线设计里交叠。代价是 S-box 硬件实例数从 1 变 2，面积增加约一个 S-box（256 项常量表）。

**练习 2**：为什么逆向 S-box（`inv_sbox_inst`）没有被设计成同样共享？

> **参考答案**：因为逆向 S-box 只有 `aes_decipher_block` 这一个消费者，不存在「第二个消费者」可以和它分时复用；强行提到 `aes_core` 层共享反而徒增布线，没有面积收益。

---

### 4.2 逐字 SubBytes 的面积/时序权衡

#### 4.2.1 概念说明

AES 的 SubBytes 要对一个 128 位状态（16 字节）逐字节替换。`aes_sbox` 模块本身的端口是 32 位的——它内部用 4 路并行 `assign` 一次替换完一个 32 位字（4 字节），详见 u2-l2。

本工程在 `aes_encipher_block` 里**没有**例化 4 个 S-box 把 128 位一次替换完，而是：

- 每轮用 **4 个时钟周期**，每个周期送一个 32 位字进共享 S-box；
- 用一个 2 位的字计数器 `sword_ctr` 在 0/1/2/3 之间循环，决定这一拍替换第几个字。

这就是「逐字 SubBytes」。它换回了什么？**S-box 的硬件宽度只需要 32 位（1 份）**，而不是 4 份 32 位 S-box 并排。代价是每轮多花 3 拍，总周期数显著上升。

#### 4.2.2 核心流程

加密 FSM `encipher_ctrl` 的每个完整轮（除初始轮）形如：

```
CTRL_SBOX（4 拍）：sword_ctr = 0,1,2,3
    每拍把 block_w{ctr}_reg 送进 S-box，把 new_sboxw 写回同一个字寄存器
CTRL_MAIN（1 拍）：shiftrows → mixcolumns → addroundkey 一次性算完写回
```

所以每轮 = 4（SubBytes）+ 1（其余三变换）= 5 拍。`sword_ctr` 是 2 位计数器，正好数到 4 个字。

关键细节：S-box 的输出 `new_sboxw` 被**广播**成 4 份拼接 `{new_sboxw, new_sboxw, new_sboxw, new_sboxw}`，但**只有当前 `sword_ctr` 指向的那一个字寄存器**的写使能被拉高（`block_w{ctr}_we = 1`）。换句话说，4 个字寄存器轮流接收，靠「广播数据 + 选择性写使能」实现 4 拍分时写入——这是非常典型的「数据广播 + 控制收窄」低面积手法。

#### 4.2.3 源码精读

字计数器寄存器声明：

[rtl/aes_encipher_block.v:131-135](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L131-L135) —— `sword_ctr_reg/new/we/inc/rst`，2 位计数器，就是「逐字」的灵魂。

`SBOX_UPDATE` 分支里的「广播 + 选择性写使能」：

[rtl/aes_encipher_block.v:261-290](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L261-L290) —— `block_new` 被拼成 4 份 `new_sboxw`，再由 `case (sword_ctr_reg)` 决定本拍只置 `block_w0_we`/`w1_we`/`w2_we`/`w3_we` 中的一个为 1，实现 4 拍逐字替换。

驱动 `sword_ctr` 走 4 拍的 FSM 片段：

[rtl/aes_encipher_block.v:414-423](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L414-L423) —— `CTRL_SBOX` 态每拍 `sword_ctr_inc`，直到 `sword_ctr_reg == 2'h3` 才切到 `CTRL_MAIN`。这正是「每轮 SubBytes 耗 4 拍」的源头。

#### 4.2.4 代码实践（源码阅读 + 估算型）

**实践目标**：量化「逐字 SubBytes」带来的周期代价。

**操作步骤**：

1. 在 `encipher_ctrl` 的 FSM（L392-449）里数清楚 `CTRL_INIT`、`CTRL_SBOX`、`CTRL_MAIN` 三个状态的停留拍数：
   - `CTRL_INIT`：1 拍（初始轮，仅 AddRoundKey）。
   - `CTRL_SBOX`：4 拍（`sword_ctr` 从 0 走到 3）。
   - `CTRL_MAIN`：1 拍（中间轮走 `MAIN_UPDATE`，最终轮走 `FINAL_UPDATE`）。
2. 推导 AES-128（10 轮）的总周期数：
   - 1（INIT）+ 10 × (4 SBOX + 1 MAIN) = 1 + 10 × 5 = **51 拍**。
3. 同理 AES-256（14 轮）：1 + 14 × 5 = **71 拍**。

**需要观察的现象**：如果你**取消逐字**、改成「4 个 S-box 并排、1 拍做完 SubBytes」，则每轮从 5 拍降到 2 拍（1 SBOX + 1 MAIN），AES-128 总周期从 51 降到 1 + 10×2 = 21 拍——快了一倍多，但 S-box 实例数 ×4。

**预期结果**：得到一张「S-box 实例数 vs 总周期数」的取舍表，直观看到面积换时间的斜率。

> 注：周期数推导基于源码 FSM，可直接复核；「改成 4 个 S-box」是假想改造，未实际综合，面积结论为定性判断。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `block_new` 要写成 4 份 `new_sboxw` 的拼接，而不是只连当前字？

> **参考答案**：因为 `block_new` 是一个统一的 128 位总线，时序块（`reg_update`）根据写使能决定把 `block_new` 的哪个 32 位切片写进哪个字寄存器。把 4 份都拼上是「广播」，让每个字寄存器都能从同一段组合结果里取值；真正决定写谁的，是 `block_w{ctr}_we` 这一组写使能。这样数据通路只需一份，控制信号收窄到 2 位的 `sword_ctr`。

**练习 2**：`sword_ctr` 为什么是 2 位？

> **参考答案**：128 位状态 = 4 个 32 位字，2 位计数器恰好编码 0/1/2/3 四个值，4 拍走完一个字的完整 SubBytes。

---

### 4.3 寄存器写使能与时钟风格

#### 4.3.1 概念说明

本工程所有时序逻辑都遵循同一个模板（u1-l3 已建立，这里从**低功耗综合**角度再读一遍）：

1. **统一上升沿 + 异步低有效复位**：`always @(posedge clk or negedge reset_n)`，复位分支把寄存器置已知初值。
2. **每个寄存器配一个写使能 `_we`**：`if (_we) _reg <= _new;`。不需要更新的周期，`_we=0`，寄存器**保持原值不动**。

第 2 点是低功耗的关键。如前置知识 2.3 所述，写使能让寄存器在不需变化的周期不翻转（甚至可被综合工具编译成时钟门控），直接压低 \(\alpha\)。本工程的每一个状态寄存器、数据寄存器、计数器无一例外地遵循「`_reg` + `_new` + `_we`」三件套（或其简化变体），这是一种**纪律性极强**的低功耗编码风格。

#### 4.3.2 核心流程

每个模块的时序块 `reg_update` 都长得几乎一样：

```
always @(posedge clk or negedge reset_n) begin
    if (!reset_n) begin
        所有 _reg <= 复位初值;
    end
    else begin
        if (regA_we) regA <= regA_new;
        if (regB_we) regB <= regB_new;
        ...
    end
end
```

组合逻辑块（`always @*`）负责算出每个寄存器在本拍的 `_new` 和 `_we`；时序块只做「按写使能机械搬运」。这种**组合算、时序搬**的两段式，天然把「是否需要翻转」这个决定权交给了组合块——组合块可以在「不需要变」时把 `_we` 拉低，寄存器就静止不动，功耗就省下来了。

#### 4.3.3 源码精读

`aes_core` 的 `reg_update`——三个寄存器各有独立写使能：

[rtl/aes_core.v:156-175](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L156-L175) —— 复位分支（L158-163）给 `ready_reg` 置 1、状态机置 `CTRL_IDLE`；正常分支里 `result_valid_we`/`ready_we`/`aes_core_ctrl_we` 三个写使能各自独立，互不影响——哪个不需要变，对应的 `_we` 就为 0，该寄存器保持。

`aes_encipher_block` 的 `reg_update`——4 个字寄存器 + 计数器 + 状态寄存器，每个都有自己的写使能：

[rtl/aes_encipher_block.v:185-224](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L185-L224) —— 注意 `block_w0_we`/`w1_we`/`w2_we`/`w3_we` 是 4 个独立的写使能（L200-210），这正是 4.2 节「逐字写入」能成立的硬件基础，也是「不需要写的字这一拍不翻转」的省电点。

`aes_key_mem` 的 `reg_update`——复位时用 `for` 循环清零整个 `key_mem` 数组：

[rtl/aes_key_mem.v:101-140](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_key_mem.v#L101-L140) —— `key_mem` 是 15 项的 128 位数组（L45），复位分支用 `for` 循环（L107-108）把 15 把轮密钥槽全清零；正常分支（L128-129）里 `key_mem[round_ctr_reg]` 只在 `key_mem_we` 有效时写入，**一次只写一个槽**——既省写端口，又省翻转。

异步低有效复位是全工程统一的（注释里也写明）：

[rtl/aes_core.v:152-155](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L152-L155) —— 注释 "All registers are positive edge triggered with asynchronous active low reset. All registers have write enable." 是这套风格的官方自述，三个模块文件里都能找到几乎相同的注释。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：统计一个模块里「写使能」的密度，体会这套风格的纪律性。

**操作步骤**：

1. 打开 `rtl/aes_encipher_block.v`。
2. 在「Registers including update variables and write enable」段（L128-160）数一下有多少个 `_we` 信号（`sword_ctr_we`、`round_ctr_we`、`block_w0_we`..`block_w3_we`、`ready_we`、`enc_ctrl_we`）。
3. 到 `reg_update`（L185-224）确认每个 `_we` 都对应一个 `if (_we) _reg <= _new;`。

**需要观察的现象**：你会发现寄存器个数与写使能个数几乎一一对应。这意味着综合器可以**为每一个寄存器单独推断一个时钟门控**，把「保持原值」变成「这一拍根本不给它发时钟」，功耗收益最大化。

**预期结果**：列出一张「寄存器 → 写使能 → 是否可门控」的对照表，确认几乎 100% 的寄存器都能被门控。

#### 4.3.5 小练习与答案

**练习 1**：如果不给某个寄存器配写使能，而是每拍无条件 `reg <= new`，功能上常常也对，为什么本工程还是坚持配 `_we`？

> **参考答案**：功能上可能相同（当 `_new` 恰好等于旧值时），但无条件写入会让寄存器在「逻辑上不需要变」的周期也可能因综合后的中间节点翻转而耗电；配 `_we` 后，综合器可推断时钟门控，连时钟都不翻转，动态功耗更低。这是面向 ASIC/低功耗的有意取舍。

**练习 2**：`key_mem` 数组有 15 个槽，但 `reg_update` 里只有一句 `if (key_mem_we) key_mem[round_ctr_reg] <= key_mem_new;`（L128-129），这说明了什么？

> **参考答案**：说明 `key_mem` 只有**一个写端口**，每个时钟沿最多写一个槽（由 `round_ctr_reg` 寻址）。这是典型的「单口存储」结构，省端口面积、省功耗，代价是密钥扩展必须逐把串行生成（init 阶段耗多拍）——又是一处「用时间换面积」。

---

### 4.4 吞吐与关键路径

#### 4.4.1 概念说明

衡量一个密码核的「快慢」有两个相关但不同的指标：

- **吞吐率（Throughput）**：每秒能处理多少比特明文，单位 Gbps 或 Mbps。
- **关键路径（Critical Path）**：组合逻辑里最长的一条信号传播路径，它决定**最高可工作时钟频率** \(f_{\text{clk}}\)。

二者关系是：

\[ \text{Throughput} = \frac{\text{每块明文位数}}{\text{每块总耗时}} = \frac{128}{N_{\text{cyc}} \times T_{\text{clk}}} = \frac{128 \times f_{\text{clk}}}{N_{\text{cyc}}} \]

所以**吞吐率同时受制于周期数 \(N_{\text{cyc}}\)（架构决定）和时钟频率 \(f_{\text{clk}}\)（关键路径决定）**。本工程的两个「慢」来源都很清楚：周期数因「逐字 SubBytes + 迭代轮」而偏多（AES-128 = 51 拍），关键路径则因「一轮的三变换在一拍内组合算完」而偏深。

#### 4.4.2 核心流程

先定周期数 \(N_{\text{cyc}}\)（4.2 节已推导）：

| 模式 | next 阶段加密周期数 |
| --- | --- |
| AES-128 | \(1 + 10 \times 5 = 51\) |
| AES-256 | \(1 + 14 \times 5 = 71\) |

> 注意：这里只算 `next`（真正加/解密）阶段。`init`（密钥扩展）是一次性开销，换一次密钥才做一次，长流场景下可摊薄，故吞吐率公式里通常不计入 init。

再定关键路径。在 `CTRL_MAIN` 那一拍，组合块 `round_logic` 要一口气算完：

\[ \text{old\_block} \xrightarrow{\text{shiftrows}} \xrightarrow{\text{mixcolumns}} \xrightarrow{\text{addroundkey}} \text{block\_new} \]

其中 `mixcolumns` 是最深的一段：它对 4 个列各调用一次 `mixw`，每个 `mixw` 又由 `gm2`（左移 + 条件异或 0x1b）和 `gm3`（= `gm2` ^ op）组合而成。此外 `addroundkey` 用的 `round_key` 来自 `key_mem` 的**组合读**（`key_mem[round]`），也算在这条路径里。这条「寄存器 → shiftrows → mixcolumns → addroundkey（含 key_mem 组合读）→ 多路选择 → 寄存器」就是最可能的关键路径。

> 注意：SubBytes 不在这条路径里——它被特意拆到了单独的 `CTRL_SBOX` 拍，S-box 的查表延迟和 mixcolumns 的延迟被分到两个时钟周期，互不叠加。这是「拆拍」降低关键路径深度的典型手法。

#### 4.4.3 源码精读

一轮三变换的组合计算（关键路径核心）：

[rtl/aes_encipher_block.v:244-249](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L244-L249) —— `round_logic` 在每个组合求值里同时算出 `shiftrows_block`、`mixcolumns_block`、三个 `addkey_*_block`，这一串就是 `CTRL_MAIN` 拍的关键路径来源。

`mixcolumns` → `mixw` → `gm2/gm3` 的复合深度：

[rtl/aes_encipher_block.v:85-101](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L85-L101) —— `mixcolumns` 调 4 次 `mixw`；

[rtl/aes_encipher_block.v:67-83](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L67-L83) —— 每个 `mixw` 输出 4 字节，每字节是 `gm2`/`gm3` 的异或树；

[rtl/aes_encipher_block.v:55-65](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L55-L65) —— `gm2` 是最底层的 `{op[6:0],1'b0} ^ (8'h1b & {8{op[7]}})`，决定了路径里最深的「移位 + 掩码 + 异或」级数。

`round_key` 的组合读端口（也挂在关键路径上）：

[rtl/aes_key_mem.v:148-151](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_key_mem.v#L148-L151) —— `key_mem_read` 是纯组合 `tmp_round_key = key_mem[round]`，没有寄存器打断，所以从 `round` 地址到 `round_key` 输出的延迟与 mixcolumns 叠加在同一条路径里。

README 给出的吞吐数字：

[README.md:1-1](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/README.md#L1-L1) —— 宣称吞吐 0.06 Gbps（= 60 Mbps）。README 未说明该数字对应的密钥长度、时钟频率、是否含 init、以及在哪块 FPGA/ASIC 上测得。

#### 4.4.4 代码实践（估算型）

**实践目标**：用源码推导出的周期数，反推 README 的 0.06 Gbps 对应怎样的工作点。

**操作步骤**：

1. 取 AES-128、`next` 阶段、不计 init：\(N_{\text{cyc}} = 51\)。
2. 由吞吐公式反解时钟周期：

   \[ 0.06\,\text{Gbps} = 60 \times 10^6\,\text{bps} = \frac{128}{51 \times T_{\text{clk}}} \]

   \[ T_{\text{clk}} = \frac{128}{51 \times 60 \times 10^{6}} \approx 4.18 \times 10^{-8}\,\text{s} \approx 41.8\,\text{ns} \]

   \[ f_{\text{clk}} \approx \frac{1}{41.8\,\text{ns}} \approx 23.9\,\text{MHz} \]

3. 同理对 AES-256（\(N_{\text{cyc}}=71\)）反解：\(T_{\text{clk}} \approx 30.0\,\text{ns}\)，\(f_{\text{clk}} \approx 33.3\,\text{MHz}\)。

**需要观察的现象**：反推出的时钟频率并不高（数十 MHz 量级）。这与本讲的两个结论自洽——(a) 周期数偏多（逐字 SubBytes、迭代轮），(b) 关键路径偏深（一轮三变换组合算完），二者共同压低了可达吞吐。

**预期结果**：得到一张「密钥长度 → 周期数 → 反推时钟频率」的小表。

> **待本地验证**：上述反推**假设** 0.06 Gbps 是 AES-128（或 AES-256）`next` 阶段稳态吞吐、且不计 init。但 README 没有说明测量条件（密钥长度、时钟、是否含 init、目标器件），所以反推出的 \(f_{\text{clk}}\) 只是一个**说明性估算**，不是器件实测值。要做严谨核对，需要在真实仿真/综合环境里测出 \(f_{\text{clk}}\) 与每块耗时（参见 u1-l5 的仿真方法与 u3-l2 的 NIST 向量）。

#### 4.4.5 小练习与答案

**练习 1**：本设计里 SubBytes 与 MixColumns 是否落在同一条关键路径上？为什么？

> **参考答案**：不在同一条路径上。SubBytes 被拆到 `CTRL_SBOX` 的 4 拍里，结果先写回字寄存器；MixColumns 在随后的 `CTRL_MAIN` 拍里从这些寄存器读出来再算。两者之间隔着寄存器边界，时钟周期把它们的延迟隔开了，所以 S-box 查表延迟不与 mixcolumns 叠加。这正是把 SubBytes 拆拍的额外好处（虽然拆拍的主因是省 S-box 面积）。

**练习 2**：如果只想提高 \(f_{\text{clk}}\)（不改动周期数），最该动手的是 `round_logic` 的哪一段？

> **参考答案**：最该拆分的是 `shiftrows → mixcolumns → addroundkey` 这条组合链，尤其 `mixcolumns`（4 个 `mixw`，每个含 `gm2/gm3` 复合）。可以在 `mixcolumns` 之后插一拍寄存器，把这条长路径切成两段，缩短关键路径、抬高 \(f_{\text{clk}}\)；代价是每轮多 1 拍，\(N_{\text{cyc}}\) 上升，吞吐是否净增益取决于「频率提升比例」是否超过「周期增加比例」。

---

## 5. 综合实践

**综合任务**：把本讲四个模块串起来，做一份「ASIC 取舍审计报告」，并设计一条「流水线化」改造路径。

**要求**：

1. **列出至少 3 处「用时间换面积」的取舍点**。每处要写清：
   - 在哪个文件、哪些行能找到证据（给出永久链接）；
   - 「省下的硬件」是什么（面积收益）；
   - 「多花的周期/约束」是什么（时间代价）。

   建议候选（你可以从中选，也可以补充）：
   - 全核共享 1 个正向 S-box（4.1）；
   - 逐字 SubBytes 拆 4 拍（4.2）；
   - 单套轮逻辑迭代复用 10/14 轮（未展开，4.4 关键路径讨论）；
   - `key_mem` 单写端口、逐把串行扩展（4.3）。

2. **讨论「流水线化提升吞吐」要改哪些模块**。具体回答：
   - 若要让「不同块的轮」能重叠执行（流水线），`aes_encipher_block` 的 `encipher_ctrl` 与 `round_logic` 要怎么改？（提示：现在状态寄存器只有一份 `block_w*_reg`，流水线需要每级一份。）
   - 流水线化会不会破坏 4.1 的「共享 S-box」？为什么？（提示：多块同时在不同轮，S-box 会被多个在途块同时争用。）
   - `aes_core` 的 `sbox_mux` 和 `encdec_mux` 在流水线下还成立吗？
   - `aes_key_mem` 的 `key_mem` 在流水线下，是否需要多端口读？（提示：多个在途块可能同时要不同轮号 `round` 的轮密钥。）

3. **诚实标注**：哪些结论是从源码直接读出的（如周期数、写使能密度），哪些是定性推断（如面积增减、关键路径深浅，未经综合）、哪些**待本地验证**（如反推的时钟频率）。

**交付物**：一份 Markdown 表格 + 一段流水线改造讨论（不超过 400 字）。

**预期结果示例**（取舍点部分）：

| 取舍点 | 源码证据 | 面积收益 | 时间代价 |
| --- | --- | --- | --- |
| 共享 1 个正向 S-box | [aes_core.v:138](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L138-L138)、[:184-194](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L184-L194) | 省一个 S-box 实例 | init 与 next 必须分阶段、不能并发 |
| 逐字 SubBytes 4 拍 | [aes_encipher_block.v:261-290](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L261-L290)、[:414-423](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L414-L423) | S-box 只需 32 位宽，而非 128 位 | 每轮多 3 拍 |
| 单套轮逻辑迭代复用 | [aes_encipher_block.v:232-314](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L232-L314) | 不复制 10/14 份轮硬件 | 串行 10/14 轮，周期数高 |

> 完成后，建议回到 u3-l2 的 NIST 向量与 u1-l5 的仿真环境，验证你对周期数的推导（在波形里数 `enc_ctrl_reg` 走完一轮的拍数）。

## 6. 本讲小结

- **共享单个正向 S-box**：全核只例化 1 个 `sbox_inst`，由 `sbox_mux` 按 `init_state` 在密钥扩展与加密间分时复用；解密用私挂的逆向 S-box 不参与共享。代价是 init/next 必须分阶段互斥。
- **逐字 SubBytes 拆 4 拍**：用 2 位 `sword_ctr` 把 128 位 SubBytes 拆成 4 拍，配合「广播数据 + 选择性写使能」只写当前字。省 S-box 宽度，但每轮耗 5 拍。
- **统一写使能寄存器 + 异步低有效复位**：每个寄存器配 `_we`，不需更新时保持原值，便于综合器推断时钟门控、降低动态功耗；`key_mem` 单写端口逐把串行写入。
- **周期数与吞吐**：AES-128 加密 `next` 阶段 51 拍、AES-256 为 71 拍；README 宣称 0.06 Gbps，在「不计 init」假设下反推时钟约数十 MHz 量级（**待本地验证**，README 未给测量条件）。
- **关键路径**：最深路径在 `CTRL_MAIN` 拍的 `shiftrows → mixcolumns → addroundkey`（含 `key_mem` 组合读）；SubBytes 被拆到独立拍，不与 mixcolumns 叠加——拆拍既省面积又顺带缩短了关键路径。
- **贯穿主线**：本工程几乎每一处设计都倾向「面积/功耗优先、接受更长周期」，是一份典型的低功耗 ASIC 取向实现。

## 7. 下一步学习建议

- **下一讲 u3-l5（二次开发与扩展实践）**：把本讲分析的取舍点变成动手实验——尝试新增一组 NIST 测试向量、调整地址映射、或者写一份「流水线化」的设计草案，并用现有分层 testbench 做回归。
- **回看验证体系**：结合 u3-l2（NIST 向量）与 u3-l3（分层测试），体会「架构取舍」与「可验证性」的关系——共享/拆拍让设计更省，但也让单次加密耗时变长、需要在 testbench 里用更长的固定延时来等待（这正是 u3-l2 指出的「固定延时不够精确」的根因）。
- **延伸阅读建议**（项目仓库外、AES 标准范畴）：
  - NIST FIPS-197（AES 标准），对照本工程确认轮变换、密钥扩展的实现是否与规范一致。
  - 关于「面积 vs 吞吐」的 AES 硬件实现综述，比较「迭代型（本工程）」「展开型」「流水线型」三种架构的面积-吞吐曲线，理解本工程在频谱中的位置。
