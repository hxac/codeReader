# 优先编码与仲裁器

## 1. 本讲目标

当多个「请求者」同时争用一个资源（一条总线、一个写端口、一个接收端）时，硬件必须每拍**只挑出一个**来服务。本讲围绕三个层层递进的模块，讲清楚「挑选」这件事在 FPGA 里是怎么做的：

- 学完 `Priority_Encoder`，你能把一串并发的请求位压缩成一个**索引号**。
- 学完 `Arbiter_Priority`，你能输出一个**独热授权（grant）** 并把它保持住，理解它为何**不公平、会饥饿**。
- 学完 `Arbiter_Round_Robin`，你能掌握「掩码法（mask method）」轮询仲裁，理解它如何**按活动比例公平**分配资源、避免饥饿。

核心收获：理解**公平性与饥饿的取舍**，并在面对「多个请求者抢一个资源」的场景时，能判断该用固定优先级还是轮询。

## 2. 前置知识

本讲建立在已学讲义之上，先复述两个关键基础：

- **独热（one-hot）编码**（u5-l2）：一个 N 位向量里只有一位是 1，其余是 0。例如 4 位的 `0010` 表示「第 1 号被选中」。本讲里，授权（grant）信号一律用独热表达。
- **Register 家族**（u6-l1）：一个带 `clock`/`clock_enable`/`clear`/`data_in`/`data_out` 的同步寄存器；`clock_enable` 为 0 时寄存器**冻结**（不采样），这个特性在本讲里会被巧妙利用。

再补两个位运算小技巧（来自 *Hacker's Delight*，本书多处复用）：

- **孤立最右边的 1**：`x & (-x)`。例如 `01011000 & (-01011000)` = `01011000 & 10101000` = `00001000`。它挑出最低位的那个 1，结果天然是独热。
- **温度计掩码到最右 1**：`x ^ (x - 1)`。例如 `001000 ^ 000111` = `001111`，把从第 0 位到最低那个 1 之间的所有位全置 1。

> 约定：本讲全文 **最低位（LSB，bit 0）优先级最高**。位号越大优先级越低。这是三个模块共同遵守的方向。

最后，什么是**仲裁（arbitration）**？就是「N 个请求者各拉一根 `requests` 请求线，仲裁器每拍根据某种策略只把一根 `grant` 授权线拉高」。请求者必须**拉住请求并等待授权**，办完事再放下请求——很像 ready/valid 握手，但**一旦授权就不能被打断**，否则这次访问就丢了。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [Priority_Encoder.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Priority_Encoder.v) | 把请求位掩码转成「最高优先级那一位」的**索引号**（二进制数），并给出是否有效。 |
| [Arbiter_Priority.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Arbiter_Priority.v) | 固定优先级仲裁器，输出独热 grant，**保持授权到请求释放**。 |
| [Arbiter_Round_Robin.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Arbiter_Round_Robin.v) | 轮询仲裁器，用掩码法按活动比例公平分配，避免饥饿。 |

支撑构件（被上面三个模块实例化复用）：

| 文件 | 作用 |
| --- | --- |
| [Bitmask_Isolate_Rightmost_1_Bit.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Bitmask_Isolate_Rightmost_1_Bit.v) | `x & (-x)`，孤立最低位 1。 |
| [Logarithm_of_Powers_of_Two.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Logarithm_of_Powers_of_Two.v) | 把独热位转成它的位索引（取对数）。 |
| [Bitmask_Thermometer_to_Rightmost_1_Bit.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Bitmask_Thermometer_to_Rightmost_1_Bit.v) | `x ^ (x-1)`，生成温度计掩码。 |
| [Pulse_Generator.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_Generator.v) | 把电平的上升沿转成一拍脉冲。 |
| [Register.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register.v) | 存放上一拍的 grant（状态记忆）。 |

---

## 4. 核心概念与源码讲解

### 4.1 优先编码器 Priority_Encoder

#### 4.1.1 概念说明

**优先编码器**解决的问题是：给定一个可能有多位同时为 1 的请求位掩码，输出「优先级最高的那一位的**索引号**」。

它和仲裁器有一处关键差别：

- **优先编码器**输出的是**二进制索引**（比如 `3`，即 `011`），把多个并发事件**过滤成一个编号**，方便后续用来索引表格或做数值处理。
- **仲裁器**输出的是**独热 grant**（比如 `001000`），直接驱动多路选择器选中某一路数据。

`Priority_Encoder` 的输入输出语义（注释里的真值表）：

| 输入 `word_in` | 输出 `word_out`（索引） | `word_out_valid` |
| --- | --- | --- |
| `11111` | `00000`（0） | 1 |
| `00010` | `00001`（1） | 1 |
| `01100` | `00010`（2） | 1 |
| `11000` | `00011`（3） | 1 |
| `10000` | `00011`（4） | 1 |
| `00000` | `00000`（0） | **0（无效）** |

注意最后一行：全零输入时输出索引也是 0，但用 `word_out_valid=0` 标明「这是无效的 0」，因为 0 本身是一个合法的索引。

#### 4.1.2 核心流程

`Priority_Encoder` 的实现思路极其优雅，分两步：

1. **孤立最低位的 1**：用 `Bitmask_Isolate_Rightmost_1_Bit`（`x & (-x)`）把多位请求压成独热，只留下优先级最高的那一位。
2. **取对数得到索引**：独热向量的「对数」就是它那一位的索引号——因为 \(\log_2(2^k) = k\)。用 `Logarithm_of_Powers_of_Two` 完成。
3. **处理全零**：全零时对数无定义，靠 `logarithm_undefined` 标志翻转成 `word_out_valid=0`。

用伪代码表示：

```
lsb_1          = word_in & (-word_in)        // 独热：最高优先级那一位
word_out       = log2(lsb_1)                  // 该位的索引号
word_out_valid = (word_in != 0)               // 全零则无效
```

#### 4.1.3 源码精读

模块端口声明：`word_in` 是请求位掩码，`word_out` 是索引号，`word_out_valid` 标明有效性。

[Priority_Encoder.v:26-34](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Priority_Encoder.v#L26-L34) —— 模块端口，注意 `word_out_valid` 是 `output reg`（来自本模块组合逻辑，见 u2-l1）。

第一步：孤立最低位 1，实例化 `Bitmask_Isolate_Rightmost_1_Bit`。

[Priority_Encoder.v:42-54](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Priority_Encoder.v#L42-L54) —— 把 `word_in` 喂给 isolate 块，得到独热的 `lsb_1`。

被实例化的 isolate 块核心只有一行：

[Bitmask_Isolate_Rightmost_1_Bit.v:28-30](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Bitmask_Isolate_Rightmost_1_Bit.v#L28-L30) —— `word_out = word_in & (-word_in);`，这就是 `x & (-x)`。

第二步：把独热转成索引。`Logarithm_of_Powers_of_Two` 的实现很有代表性——它**不能**用查表（要存 \(2^{WORD\_WIDTH}\) 项，综合极慢），而是**为每个输入位预算好它的对数，再按位门控、OR 归约**：

[Logarithm_of_Powers_of_Two.v:110-117](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Logarithm_of_Powers_of_Two.v#L110-L117) —— `generate for` 为每个输入位 `i` 算 `{PAD, i}`（即位索引），只有 `one_hot_in[i]==1` 时才放入对应字。

[Logarithm_of_Powers_of_Two.v:121-131](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Logarithm_of_Powers_of_Two.v#L121-L131) —— 用 `Word_Reducer`（OR 归约）把所有「可能的索引」合并成一个最终索引。

第三步：全零检测。

[Priority_Encoder.v:76-78](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Priority_Encoder.v#L76-L78) —— `word_out_valid = (logarithm_undefined == 1'b0)`，把「对数无定义」翻译成「输出无效」。

> 这一节最重要的体会：**`x & (-x)` 一行就能选出最高优先级**。后面两个仲裁器的全部技巧，本质都是在「隔离最低位 1」之上加状态、加掩码。

#### 4.1.4 代码实践

**实践目标**：手算 `Priority_Encoder`，建立对「位掩码→索引」的直觉。

**操作步骤**（纯纸笔，无需仿真器）：

1. 设 `WORD_WIDTH=8`，对下面每个 `word_in`，依次算出 `lsb_1 = x & (-x)`、`word_out`、`word_out_valid`：
   - `8'b0000_0000`
   - `8'b0000_0100`
   - `8'b1101_0110`
   - `8'b1000_0000`

**需要观察的现象**：无论输入有多少个 1，`lsb_1` 永远只有一位是 1（独热），且那一位就是 `word_in` 里**编号最小**的 1。

**预期结果**（待本地验证）：

| `word_in` | `lsb_1` | `word_out` | `valid` |
| --- | --- | --- | --- |
| `0000_0000` | `0000_0000` | `0` | 0 |
| `0000_0100` | `0000_0100` | `2` | 1 |
| `1101_0110` | `0000_0010` | `1` | 1 |
| `1000_0000` | `1000_0000` | `7` | 1 |

#### 4.1.5 小练习与答案

**练习 1**：为什么全零输入时，`word_out` 会是 0 而不是别的值？
**答案**：`Logarithm_of_Powers_of_Two` 在无任何位为 1 时，所有 `per_input_bit` 分支都填 `WORD_ZERO`，OR 归约后自然是 0；0 又恰好是 bit 0 的合法索引，所以必须额外用 `word_out_valid` 区分「真的选了第 0 位」和「根本没选」。

**练习 2**：注释说本模块与 `Number_of_Trailing_Zeros`（ntz）关系密切，为什么？
**答案**：ntz 数的是「最低位 1 右边有几个 0」，正好等于「最低位 1 的索引」——也就是 `Priority_Encoder` 的输出。两者只差一个 valid 标志的处理。

---

### 4.2 优先仲裁器 Arbiter_Priority

#### 4.2.1 概念说明

`Arbiter_Priority` 是**固定优先级**仲裁器：每拍从请求里选出**优先级最高（LSB）**的一路，输出独热 `grant`。它与 `Priority_Encoder` 的区别有二：

1. 输出是**独热 grant**而非二进制索引（直接驱动 mux）。
2. 有**状态**：授权会**保持到请求者释放请求为止**，期间不被更高优先级打断。

源码注释里有一句至关重要的**公平性警告**（[Arbiter_Priority.v:30-34](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Arbiter_Priority.v#L30-L34)）：

> **A Priority Arbiter is not fair.** 如果高优先级请求来得太频繁，会**饿死（starve）** 低优先级请求；如果低优先级请求把授权占太久，又会饿死高优先级请求（**优先级反转**）。

这就是「固定优先级」的代价：简单、快、面积小，但**不保证公平**。

模块还提供一个 `requests_mask` 输入，可动态屏蔽某些请求；不用时接全 1，屏蔽逻辑会被综合器优化掉。

#### 4.2.2 核心流程

`Arbiter_Priority` 的数据流（伪代码）：

```
requests_masked  = requests & requests_mask          // 先屏蔽被禁的请求
grant_candidate  = isolate(requests_masked)          // 选最高优先级候选 = 独热
grant = (上一拍grant的请求还在) ? grant_previous      // 还在就保持
                                 : grant_candidate    // 否则换新候选
// 用一个 Register 把 grant 存成下一拍的 grant_previous
```

关键设计点：

- **保持语义**：只要 `(requests & grant_previous) != 0`，即上一拍被授权的那一路还在请求，就**继续授权它**，不被别人抢走。这保证了一次事务不被打断。
- **授权是组合输出**：`grant` 当拍就由 `requests` 算出，注释反复提醒「so pipeline as necessary」——若你的下游时序紧，需要自己在前面加流水线寄存器。

#### 4.2.3 源码精读

端口声明，`grant` 是 `output reg`：

[Arbiter_Priority.v:58-70](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Arbiter_Priority.v#L58-L70) —— 注意有 `clock`/`clear`（因为有状态寄存器），还有 `requests_mask`（不用接全 1）。

第一步，屏蔽：

[Arbiter_Priority.v:81-85](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Arbiter_Priority.v#L81-L85) —— `requests_masked = requests & requests_mask`。

第二步，选最高优先级候选，复用 isolate 块：

[Arbiter_Priority.v:90-99](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Arbiter_Priority.v#L90-L99) —— 实例化 `Bitmask_Isolate_Rightmost_1_Bit` 得到独热 `grant_candidate`。

第三步，核心保持逻辑（一行三元，见 u3-l1「三元优于 if/else」）：

[Arbiter_Priority.v:104-106](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Arbiter_Priority.v#L104-L106) —— 若 `(requests & grant_previous) != INPUT_ZERO` 则保持 `grant_previous`，否则取 `grant_candidate`。

第四步，把当前 grant 存成下一拍的 `grant_previous`：

[Arbiter_Priority.v:112-124](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Arbiter_Priority.v#L112-L124) —— 一个 `Register`，`clock_enable=1'b1`（每拍都更新），`RESET_VALUE=INPUT_ZERO`，`clear` 直接接到模块的 `clear`。

#### 4.2.4 代码实践

**实践目标**：亲手构造一个**饥饿（starvation）场景**，体会「固定优先级不公平」。

**操作步骤**（纸笔推演，3 路请求 `INPUT_COUNT=3`，bit 0 优先级最高）：

1. 设 `requests = 3'b111`（三路一直在请求），`requests_mask = 3'b111`，初始 `grant_previous = 3'b000`。
2. 假设 bit 0 的请求者**永远不释放**请求（它一直 `requests[0]=1`）。
3. 逐拍写出 `grant` 的值。

**需要观察的现象**：因为 bit 0 优先级最高且永不释放，`(requests & grant_previous)` 一旦等于 bit 0 就永远是 bit 0，`grant` 锁死在 `001`。

**预期结果**（待本地验证）：从第一拍起 `grant = 3'b001`，之后**每拍都是 `001`**。bit 1、bit 2 **永远拿不到授权——被饿死**。这正是注释里警告的场景，也是需要轮询仲裁器的根本原因。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `requests_mask` 中 bit 0 设成 0（屏蔽最高优先级），会发生什么？
**答案**：`requests_masked = requests & ~001`，bit 0 被剔出候选；若 bit 1 在请求则授权给 bit 1。这就是 `requests_mask` 用来「自定义公平性调整」的入口。

**练习 2**：`grant` 是组合输出，为什么还需要 `clock` 和 `Register`？
**答案**：`grant` 的**保持**逻辑依赖 `grant_previous`（上一拍的 grant），而 `grant_previous` 是时序状态，必须靠寄存器存。所以 `clock` 不是为了产生 `grant`，而是为了记住「上一拍授权了谁」。

---

### 4.3 轮询仲裁器 Arbiter_Round_Robin

#### 4.3.1 概念说明

`Arbiter_Round_Robin` 用**掩码法（mask method）**实现轮询仲裁，源码注释指明出处（Weber 的 *Arbiters: Design Ideas and Coding Styles* 第 4.2.4 节图 12，[Arbiter_Round_Robin.v:31-33](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Arbiter_Round_Robin.v#L31-L33)）。

它的核心目标是：**按各请求者的活动比例公平分配资源**。轮询顺序从 LSB（最高优先级）走到 MSB（最低优先级）再绕回 LSB；**跳过没有请求的位**，不浪费时间；空闲的请求者不消耗任何被仲裁的资源；频繁的请求者也不会永远挡住别人。

与 `Arbiter_Priority` 的根本区别：轮询仲裁器**记住上一次授权了谁**，下一次优先尝试比它**优先级更低**的请求，从而轮转起来；当没有更低优先级请求时，再绕回最高优先级重新开始一轮。

> 它仍非「绝对公平」。注释指出（[Arbiter_Round_Robin.v:41-46](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Arbiter_Round_Robin.v#L41-L46)）：若某请求者总恰好在上一请求者结束前一刻提出请求，它等待的时间就比别人短。彻底解决需周期性快照，本模块不处理。

#### 4.3.2 核心流程

掩码法的精髓是：**每一拍计算两组候选，一组「从头开始的优先级候选」，一组「从上次授权处继续的轮询候选」，按规则二选一**。

数据流（伪代码）：

```
// (A) 检测是否有请求、是否刚脱离空闲
any_requests_active = (requests != 0)
out_from_idle       = 上升沿(any_requests_active)     // 刚恢复请求的那一拍

// (B) 优先级候选：从 LSB 开始选最高优先级（每一轮的起点）
grant_priority      = isolate(requests & requests_mask)

// (C) 轮询掩码：屏蔽掉「与上次授权同级及更高优先级」的请求
thermometer_mask    = thermometer_to_rightmost_1(grant_previous)  // 0..p 位置 1
round_robin_mask    = ~thermometer_mask                            // 只留 >p 的更低优先级

// (D) 把当前授权位 OR 回掩码（不打断当前事务），脱离空闲时则清掉它
grant_previous_gated = out_from_idle ? 0 : grant_previous

// (E) 轮询候选：在「当前授权位 + 更低优先级请求」里选最低位
masked_rr           = requests & (round_robin_mask | grant_previous_gated)
                                   & (requests_mask   | grant_previous_gated)
grant_round_robin   = isolate(masked_rr)

// (F) 没有更低优先级请求就回绕到优先级候选，否则用轮询候选
grant = (grant_round_robin == 0) ? grant_priority : grant_round_robin

// (G) 记住本次授权；空闲时冻结（clock_enable = any_requests_active）
```

四个关键机关：

1. **温度计掩码**（步骤 C）：`grant_previous` 是独热的某位 `p`。`Bitmask_Thermometer_to_Rightmost_1_Bit`（`x ^ (x-1)`）把第 `0..p` 位全置 1，取反后只剩 `>p` 的位——也就是**比上次授权优先级更低**的请求位。
2. **不打断当前事务**（步骤 D）：把 `grant_previous` OR 回掩码，让正在被服务的那一路仍可被选中（只要它还在请求），于是 `isolate` 会再次选中它——实现「保持到释放」。脱离空闲那一拍例外（见下条）。
3. **脱离空闲时复位**（步骤 D 的 `out_from_idle`）：刚恢复请求的那一拍，把 `grant_previous_gated` 清零，**不再立刻重授上次的那一路**，强制尝试更低优先级请求。否则空闲后总是从最高优先级重开，会让 lock-step 模式饿死低位。
4. **空闲时冻结状态**（步骤 G）：`Register` 的 `clock_enable = any_requests_active`，没有请求时 `grant_previous` **不更新**，保住「上次授权到哪了」，以便下次正确续上轮询。

回绕逻辑（步骤 F）：当所有更低优先级请求都不存在时（`grant_round_robin == 0`），用 `grant_priority` 从最高优先级重新开始一轮——这同时隐式复位了 `round_robin_mask`。

#### 4.3.3 源码精读

端口与 `Arbiter_Priority` 几乎一致：

[Arbiter_Round_Robin.v:70-82](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Arbiter_Round_Robin.v#L70-L82) —— 同样有 `clock`/`clear`/`requests`/`requests_mask`/`grant_previous`/`grant`。

(A) 检测是否有请求：

[Arbiter_Round_Robin.v:100-104](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Arbiter_Round_Robin.v#L100-L104) —— `any_requests_active = (requests != ZERO)`。

脱离空闲的脉冲，复用 `Pulse_Generator` 检测 `any_requests_active` 的上升沿：

[Arbiter_Round_Robin.v:111-123](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Arbiter_Round_Robin.v#L111-L123) —— `out_from_idle` 只在「无请求→有请求」的那一拍为 1。

(B) 优先级候选：

[Arbiter_Round_Robin.v:128-144](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Arbiter_Round_Robin.v#L128-L144) —— 先 `requests & requests_mask`，再 `Bitmask_Isolate` 得 `grant_priority`。

(C) 温度计掩码，复用 `Bitmask_Thermometer_to_Rightmost_1_Bit`：

[Arbiter_Round_Robin.v:152-168](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Arbiter_Round_Robin.v#L152-L168) —— `thermometer_mask` 经 `~` 取反成 `round_robin_mask`。

其底层公式：

[Bitmask_Thermometer_to_Rightmost_1_Bit.v:27-29](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Bitmask_Thermometer_to_Rightmost_1_Bit.v#L27-L29) —— `word_out = word_in ^ (word_in - ONE);`。

(D)+(E) 轮询候选，全模块最密集的两行：

[Arbiter_Round_Robin.v:187-189](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Arbiter_Round_Robin.v#L187-L189) —— `grant_previous_gated` 决定是否保持当前授权；`requests_masked_round_robin` 同时套上轮询掩码和外部 `requests_mask`，两处都 OR 回 `grant_previous_gated` 以保证正在服务的那路不被外部掩码夺走。

(F) 最终 grant 选择（回绕）：

[Arbiter_Round_Robin.v:213-215](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Arbiter_Round_Robin.v#L213-L215) —— `grant = (grant_round_robin == ZERO) ? grant_priority : grant_round_robin;`。

(G) 状态寄存器，注意 `clock_enable`：

[Arbiter_Round_Robin.v:223-235](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Arbiter_Round_Robin.v#L223-L235) —— `clock_enable` 接 `any_requests_active`，空闲时冻结 `grant_previous`；与 `Arbiter_Priority` 的 `clock_enable=1'b1` 形成鲜明对比，这是轮询能「续上」的关键。

#### 4.3.4 代码实践

**实践目标**：跟踪两路请求的轮询，验证它**轮流授权**而非锁死。

**操作步骤**（纸笔推演，`INPUT_COUNT=2`，bit 0 优先级最高，`requests_mask=2'b11`）：

1. 场景：两路**都持续请求**且各自**占用 1 拍**就释放、下一拍又请求（即 `requests` 在 `01`、`10` 间交替，模拟「办完就放」）。更简单的等价设定：让 bit 0 和 bit 1 都**始终请求**，但每路授权后**立即释放一拍**。
2. 为简化，采用最直接的设定：`requests = 2'b11` 始终为 1，但假设每个请求者拿到 grant 后**下一拍就放下请求一拍**再重新请求。逐拍记录 `grant`。
3. 对照：若换成 `Arbiter_Priority`，同样设定下 `grant` 会锁死在 bit 0。

**需要观察的现象**：轮询仲裁器的 `grant` 会在 `01` 与 `10` 之间**交替**，两路各拿一半带宽；而 `Arbiter_Priority` 会恒为 `01`，bit 1 被饿死。

**预期结果**（待本地验证）：在「两路持续请求、各自用完即放」的模式下，`Arbiter_Round_Robin` 的 grant 序列形如 `01, 10, 01, 10, ...`（轮流）；`Arbiter_Priority` 的 grant 序列形如 `01, 01, 01, ...`（锁死）。这正是「公平 vs 饥饿」的直观对照。

> 若想真跑仿真：本书 `tests/` 目录目前只有 `Counter_Gray` 的 cocotb 测试台，**没有**仲裁器的现成测试台。你可仿照 [tests/Counter_Gray_Tb.py](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/tests/Counter_Gray_Tb.py) 自行写一个（u18-l2 会讲 cocotb 写法），把上面的请求序列灌进去观察 grant 波形——此部分**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Register` 的 `clock_enable` 要接 `any_requests_active`，而不是像 `Arbiter_Priority` 那样接 `1'b1`？
**答案**：轮询必须记住「上次授权到哪一位」才能续上。若空闲时也更新 `grant_previous`，则空闲期间 `grant` 会变成 0（无请求），`grant_previous` 被刷成 0，下次请求一来就从最高优先级重开，破坏轮询连续性、可能饿死低位。冻结它能保住进度。

**练习 2**：`out_from_idle` 那一拍把 `grant_previous_gated` 清零，解决的是什么病态？
**答案**：解决「空闲后总是重授上次那一路」导致的 lock-step 饥饿——若多个请求者总在空闲后同步地交替起停，且总是先重授同一高位，低位会反复被插队。清零后强制先试更低优先级，让轮询真正前进。

**练习 3**：当 `grant_round_robin == 0` 时回绕到 `grant_priority`，这一步如何隐式「复位」了 `round_robin_mask`？
**答案**：回绕后 `grant` 取最高优先级位，下一拍 `grant_previous` 变成该高位，对应的 `thermometer_mask` 覆盖 `0..p`（几乎全 1），`round_robin_mask` 变得几乎全 0——等价于重新开始一轮，掩码自然重置。

---

## 5. 综合实践

把本讲三件事串起来：**用 `Arbiter_Round_Robin` + `Multiplexer_One_Hot` 搭一个两路公平合并器，并对比 `Arbiter_Priority` 的饥饿**。

**任务**：两路发送者（`din0_valid/din0_data`、`din1_valid/din1_data`）抢一个接收端，每拍只能放行一路。

**示例代码**（非项目原有代码，仅作讲解，待本地验证综合）：

```verilog
// 示例代码：两路公平合并（轮询）
wire [1:0] requests = {din1_valid, din0_valid};   // bit0=din0 优先级高
wire [1:0] grant_rr;
wire [1:0] grant_rr_prev;

Arbiter_Round_Robin
#(
    .INPUT_COUNT (2)
)
fair_arbiter
(
    .clock           (clock),
    .clear           (clear),
    .requests        (requests),
    .requests_mask   (2'b11),        // 不屏蔽
    .grant_previous  (grant_rr_prev),
    .grant           (grant_rr)
);

// 用独热 grant 驱动 one-hot mux 选出被授权那路的数据（参见 u5-l2）
// dout_data = grant_rr[0] ? din0_data : din1_data;
// 当两路都持续 valid 且各自用完即放时，dout 会轮流出现 din0/din1 的数据。
```

**对比实验**：把上面的 `Arbiter_Round_Robin` 换成 `Arbiter_Priority`，其余不变。在「两路持续 valid」下：

- 轮询版：`grant` 在 `01`/`10` 间交替，两路**各得一半带宽**。
- 优先版：`grant` 锁死 `01`，din1 的数据**永远出不来**——饥饿。

**需要观察与记录**：在一张表里写清楚两种仲裁器在前 8 拍各自的 `grant` 序列，并据此说明「何时该选轮询、何时固定优先级就够了」。结论应落到：**有公平性要求、请求者地位对等 → 轮询；请求者天然有优先级、且高优先级不会长期独占 → 固定优先级**（更小更快）。

## 6. 本讲小结

- **优先编码器**把请求位掩码压成「最高优先级那一位的索引号」，核心是 `x & (-x)` 孤立最低位 1，再取对数；全零用 valid 标志区分。
- **优先仲裁器**输出独热 grant 并**保持到请求释放**，逻辑简单（屏蔽→隔离→保持→寄存），但**固定优先级、不公平、会饥饿/优先级反转**。
- **轮询仲裁器**用**掩码法**：用温度计掩码屏蔽「同级及更高优先级」，强迫轮询向低位前进；没有低位请求时回绕到最高优先级。
- 轮询的两个关键状态机关：**空闲时冻结 `grant_previous`**（`clock_enable=any_requests_active`）保住进度；**脱离空闲那一拍清零**避免重授同一高位。
- **公平性取舍**：固定优先级面积小、延迟低，适合请求者天然有先后；轮询按活动比例公平分配，适合地位对等的多路。轮询也不是绝对公平（lock-step 等病态需快照法）。
- 三个模块都站在 `Bitmask_Isolate_Rightmost_1_Bit`（`x & (-x)`）这块基石之上——**隔离最低位 1 是所有「挑一个」逻辑的共同祖先**。

## 7. 下一步学习建议

- 本讲是 u11「仲裁与同步原语」单元的上半。下一讲 **u11-l2（Muller C 元素与流水线同步器）** 会从「挑一个」转向「等齐了再走」的**会合（rendez-vous）**同步，并把 `Synchronous_Muller_C_Element` 与 u9-l2 的 OK_IN/OK_OUT 会合联系起来。
- 想看仲裁器被**真实复用**的样子，直接读 [Pipeline_Merge_Round_Robin.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Merge_Round_Robin.v) 与 [Pipeline_Merge_Priority.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Merge_Priority.v)：它们把本讲的仲裁器嵌入 ready/valid 握手，实现多路弹性流的合流，是 u12-l3 的前奏。
- 若对掩码位运算感兴趣，可跳读 u16（位操作库），那里系统讲解 `Bitmask_Isolate_Rightmost_1_Bit`、`Bitmask_Thermometer_to_Rightmost_1_Bit` 等「Hacker's Delight」技巧的电路实现。
