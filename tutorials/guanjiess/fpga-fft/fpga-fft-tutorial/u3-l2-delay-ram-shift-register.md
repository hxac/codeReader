# 延时单元 delay.v：基于 RAM 的移位寄存器反馈

## 1. 本讲目标

本讲精读流水线 FFT 里「最容易看懂功能、却最难看懂时序」的一个模块——基于双口 RAM 的延时单元 `delay.v`。学完本讲你应该能够：

- 说清楚 **SDF（单路延迟反馈）** 结构里「延时」到底在做什么：为什么要把蝶形的 B 输出存起来、过若干拍再当 C 输入喂回去。
- 看懂 `delay.v` 如何用一块**双口 RAM**（厂商 `Delay` IP）实现「先写后读」的可配置深度延时，写地址 `r_addra`、读地址 `r_addrb`、写使能 `wea` 各自如何产生。
- 理解延时量 `DELAY_TIME = 2^(layer-1)`（即蝶形半个 PERIOD），以及状态机里 `required_delay_in_state_machine = DELAY_TIME - 1 - 3 - 1` 这个「-5」补偿的是什么。
- 画出 `IDLE → DELAY → OUT → TAIL → END` 五状态机的转移条件，并区分「延时建立 / 正常输出 / 尾部排空」三个阶段。
- 解释 `out_first` / `out_last` 两个边界脉冲的产生与打拍延迟，以及 `delay.v` 与变体 `delay_1k_plus.v` 的差异。

本讲承接 [u2-l1 蝶形运算单元](u2-l1-butterfly-unit.md)：那里讲过蝶形有 A/C 两个输入、B/D 两个输出，其中 **B 是「待乘旋转因子的下支」**、**D 是「直送下一级的上支」**。本讲要回答：蝶形的 C 输入从哪来？答案就是——从 B 经过延时反馈回来。

## 2. 前置知识

### 2.1 什么是「延时（delay line）」

在数字电路里，延时线就是「数据进、原样出、但中间晚若干个时钟周期」。最朴素的实现是用一串寄存器（D 触发器）首尾相接：第一个寄存器存第 1 拍的数据，第二个存第 2 拍的……第 k 个存第 k 拍的。要延时 k 拍，就需要 k 个寄存器。

| 延时实现 | 资源消耗 | 适合的延时深度 |
| --- | --- | --- |
| 寄存器链（latch） | 每拍 1 个触发器，复数还要 ×2（实部+虚部） | 浅延时（几拍到十几拍） |
| 双口 RAM | 1 块 BRAM，深度可到几千~几万 | 深延时（几十拍以上） |

这正是本项目的分水岭：`fft_8` 用寄存器链做 4 拍延时，`fft_16` 起改用 RAM 做延时。原因很简单——16 点层要延时 8 拍、复数实虚部各一条线，寄存器还勉强够；但到了 `fft_16k`（layer=14），延时深度高达 \(2^{13}=8192 \) 拍，再用寄存器链会吃掉几万个触发器，只能用 RAM。

### 2.2 双口 RAM 的「先写后读」

`delay.v` 用的是 Xilinx 的双口 RAM IP（`Delay`），它有两个独立端口：

- **A 端口（写端口）**：`clka`（写时钟）、`wea`（写使能）、`addra`（写地址）、`dina`（写数据）。
- **B 端口（读端口）**：`clkb`（读时钟）、`enb`（读使能）、`addrb`（读地址）、`doutb`（读数据）。

「先写后读」的意思是：数据按地址 0,1,2,… 顺序写入 A 端口；当写指针已经领先读指针「延时深度」那么多拍后，B 端口再以同样的顺序读出来——这样读出的数据就比写入时晚了「延时深度」拍。读地址永远落后写地址一段距离，这段距离就是延时量。

### 2.3 SDF 为什么需要延时反馈

回顾 [u1-l1](u1-l1-project-overview.md) 的结论：本项目采用 **SDF（Single-path Delay Feedback，单路延迟反馈）** 流水线。radix-2 蝶形每次要把「相隔 PERIOD/2 的两个样本」配对做加减。在硬件里，当前样本是实时来的，而它要配对的「半个周期前的那个样本」早已流过——唯一的办法就是把那个样本**存起来**，等半个周期后再取出来配对。

SDF 的精妙之处在于：它**只存下支（B）、把它反馈成上支的 C**，数据通路始终只有一条（单路）。这就是 `delay.v` 存在的全部理由。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲定位 |
| --- | --- | --- |
| [src/delay.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v) | 本讲主角：参数化（`layer`）的 RAM 延时单元 + 五状态机 | 精读 |
| [src/delay_1k_plus.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay_1k_plus.v) | `delay.v` 的变体，计数器位宽与默认 `layer` 不同；**当前未被任何模块例化** | 对比 |
| [src/butterfly_general.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v) | `fft_32` 起所有高层模块的通用壳，内部例化 `delay` 形成反馈环 | 看用法 |
| [src/fft_16.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16.v) | 第一个用 RAM 延时的层级，例化 `delay #(.layer(4))` | 看用法 / 实践 |
| [src/fft_8.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_8.v) | 用寄存器链做延时，作为 RAM 延时的对照 | 对比 |
| [tb/delay_tb.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/tb/delay_tb.v) | `delay` 的仿真测试，例化 `delay #(.layer(5))` 并用 `data_gen` 喂激励 | 实践依据 |

## 4. 核心概念与源码讲解

本讲把 `delay.v` 拆成 4 个最小模块逐层剖析：先讲延时反馈的作用（4.1），再讲 RAM 如何实现它（4.2），再讲五状态机如何调度读写（4.3），最后讲边界脉冲与变体（4.4）。

### 4.1 延时反馈在 SDF 中的作用：把 B 存起来再当 C 用

#### 4.1.1 概念说明

回顾 [u2-l1](u2-l1-butterfly-unit.md) 的蝶形：A/C 是输入，B/D 是输出。在 SDF 流水线里，蝶形的 **C 输入并不是上一级直接送来的**，而是**本级自己产生的 B 输出，经过一段延时后反馈回来**。也就是说，本级形成一个闭环：

\[
\text{蝶形 } B \text{ 输出} \xrightarrow{\ \text{延时 } 2^{(\text{layer}-1)}\ \text{拍}\ } \text{蝶形 } C \text{ 输入}
\]

为什么要延时正好 \(2^{(\text{layer}-1)}\) 拍？因为这一级的 PERIOD = \(2^{\text{layer}}\)（见 [butterfly_general.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v) 的 `parameter PERIOD = 1<<layer`）。radix-2 DIF 蝶形要把「相隔 PERIOD/2」的样本配对，所以反馈延时 = PERIOD/2 = \(2^{(\text{layer}-1)}\)。延时太少，配对错位；延时太多，样本还没到。

> 名词解释：**反馈（feedback）** 指输出回头当输入；**单路（single-path）** 指数据只有一条通路（不像 MDF 多路并行）。合起来 SDF = 一条数据线 + 一段反馈延时。

#### 4.1.2 核心流程

这条反馈环在两个地方都能看到，逻辑完全一致：

```
                ┌────────── 延时 2^(layer-1) 拍 ──────────┐
                ▼                                          │
  A ──▶ 蝶形 ──┬──▶ D (上支，直送下一级)                    │
               │                                           │
               └──▶ B (下支) ──────────────────────────────┘
                          │
                       （存入 RAM）
```

- 蝶形上支 D 直接送去和旋转因子相乘，进下一级。
- 蝶形下支 B 进入 `delay`，存够半个 PERIOD 后从 `delay` 输出，成为蝶形的 C 输入，参与下一对样本的加减。

#### 4.1.3 源码精读

在通用层级 `butterfly_general.v` 里，这条反馈环清晰可见。蝶形例化时，B 输出接 `w_B`，C 输入接 `w_C`：

[butterfly_general.v:222-235](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L222-L235) —— 蝶形的 `.C_real(w_C_real)` 来自延时输出，`.B_real(w_B_real)` 去往延时输入。

而 `w_C` 正是 `delay` 的输出，`delay` 的输入正是 `w_B`：

[butterfly_general.v:208-219](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L208-L219) —— `delay` 例化：`.din_real(w_B_real)`（B 进延时）、`.dout_real(w_C_real)`（延时输出当 C）。注意它用 `delay #(.layer(current_layer))`，延时深度跟随本级的 layer。

`fft_16.v`（layer=4）里是完全相同的模式，只是手写而不是走通用壳：

[fft_16.v:169-180](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16.v#L169-L180) —— `delay #(.layer(4))`，`.din_real(w_B_real)` → `.dout_real(w_C_real)`，实例名叫 `delay8`。

对照一下 `fft_8`（layer=3）的寄存器版本，能更直观地看出「反馈」二字——它直接用三级寄存器把 B 延时 3 拍成 C：

[fft_8.v:160-184](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_8.v#L160-L184) —— `B_real_1d <= w_B_real; B_real_2d <= B_real_1d; C_real <= B_real_2d;` 三级寄存器把 B 延时成 C。注释「generate C, 4D latch of B」点明了「C 就是 B 的延时」。

#### 4.1.4 代码实践

**实践目标**：在一张图上确认「B → delay → C」反馈环是闭环，并理解每级的延时深度。

**操作步骤**：
1. 打开 [src/fft_16.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16.v)。
2. 找到 `delay8` 的例化（约 169 行），记下 `din_real`/`din_img` 接的信号（`w_B_real`/`w_B_img`）和 `dout_real`/`dout_img` 接的信号（`w_C_real`/`w_C_img`）。
3. 再找到 `butterfly16` 的例化（约 239 行），确认 `w_B_*` 来自蝶形 `.B_*`，`w_C_*` 喂给蝶形 `.C_*`。
4. 画出这条 `w_B → delay8 → w_C → butterfly16.C` 的连线，标成闭环。
5. 重复一次，但换成 [src/butterfly_general.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v) 里的 `delay` 与 `butterfly` 例化（约 208 行与 222 行）。

**需要观察的现象**：两处画出的反馈环结构是否同构？唯一差别是不是只在 `layer` 参数？

**预期结果**：是的，`fft_16`（layer=4，PERIOD=16，延时 8 拍）与 `butterfly_general`（layer=`current_layer`，PERIOD=`1<<layer`，延时 `1<<(layer-1)` 拍）是同构的，只是参数不同。

#### 4.1.5 小练习与答案

**练习 1**：如果 `fft_16` 把 `delay` 的 `layer` 从 4 改成 3（即延时 4 拍而不是 8 拍），蝶形会怎样？

**答案**：反馈延时不再等于 PERIOD/2（=8）。蝶形会把「相隔 4 拍」而非「相隔 8 拍」的样本配对，蝶形运算的配对关系错乱，FFT 结果会全部出错。这印证了 SDF 里「延时深度必须严格等于半周期」。

**练习 2**：为什么 `fft_8` 敢用寄存器延时，而 `fft_16` 不敢？

**答案**：`fft_8` 是 layer=3，延时深度 \(2^{3-1}=4\) 拍，复数实虚部各一条线共 8 个 32 位寄存器，资源很少。`fft_16` 是 layer=4，延时 8 拍、16 个寄存器，尚可；但越往上点数翻倍，到 `fft_16k` 要 8192 拍，寄存器方案不可行，必须用 RAM。项目选择在 layer=4 这一级就切换到 RAM，统一后续实现。

### 4.2 双口 RAM 的「先写后读」延时实现

#### 4.2.1 概念说明

`delay.v` 的存储核心是两块双口 RAM（`Delay` IP）——一块存实部、一块存虚部，逻辑完全对称。数据从 A 端口写入、B 端口读出，读地址落后写地址「延时深度」拍，从而实现延时。模块对外只暴露：

- 输入：`din_real/din_img`（要延时的复数数据）、`wea`（外部写使能，由上层 `r_wea` 驱动）。
- 输出：`dout_real/dout_img`（延时后的数据）、`out_first/out_last`（边界脉冲）。
- 参数：`layer`（决定延时深度）。

#### 4.2.2 核心流程

读写地址的产生规则（伪代码）：

```
每个时钟上升沿:
    if rst:           r_addra <= 0
    else if wea:      r_addra <= r_addra + 1   // 写指针：wea 高时逐拍推进
    else:             r_addra <= 0             // wea 低时归零

    if rst:           r_addrb <= 0
    else if 状态==OUT 或 TAIL:
                      r_addrb <= r_addrb + 1   // 读指针：只在输出阶段推进
    else:             r_addrb <= 0
```

要点：
- **写指针 `r_addra`** 由外部 `wea` 驱动——上层（`butterfly_general` 或 `fft_16`）在数据有效期间把 `wea` 拉高，写指针就 0→1→2→… 地推进，把数据顺序写入 RAM。
- **读指针 `r_addrb`** 只在 `STATE_OUT`（正常输出）和 `STATE_TAIL`（尾部排空）期间推进。在「延时建立」阶段（`STATE_DELAY`）读指针不动，让写指针先跑出一段距离，把延时填满。
- 读指针始终落后写指针一段，这段距离就是延时深度。

> 设计细节（重要）：在 Xilinx 版的 RAM 例化里，`.wea(1)` 被硬编码成常 1（见 [src/delay.v:224](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v#L224)）。也就是说 RAM **每拍都在写**，真正的「写与否」是靠 `r_addra` 是否推进来控制的：`wea=0` 时 `r_addra` 停在 0，RAM 只是一直重写地址 0（无害，因为真正数据来时仍从地址 0 开始覆盖）。

#### 4.2.3 源码精读

写地址产生（注意它独立于状态机，只看 `wea`）：

[src/delay.v:89-99](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v#L89-L99) —— `if(wea) r_addra <= r_addra + 1; else r_addra <= 0;` 写地址随 `wea` 推进。

读地址产生（只在输出阶段推进）：

[src/delay.v:141-151](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v#L141-L151) —— `if(r_delay_state == STATE_OUT | r_delay_state == STATE_TAIL) r_addrb <= r_addrb + 1; else r_addrb <= 0;`

两块 RAM 的 Xilinx 例化（实部 + 虚部对称）：

[src/delay.v:222-242](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v#L222-L242) —— `Delay delay_real` 与 `Delay delay_img`：A 端口写（`addra=r_addra`, `dina=din_*`, `wea=1`），B 端口读（`addrb=r_addrb`, `doutb=w_dout_*`, `enb=1`），最后 `assign dout_* = w_dout_*`。注意 IP 端口注释写的是 `[8:0] addra/addrb`，那是 IP 例化模板的默认位宽提示，实际接的是模块内 14 位的 `r_addra/r_addrb`（高位被 IP 按需截断/忽略）。

上面紧接着是被注释掉的 Anlogic 版本：

[src/delay.v:196-218](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v#L196-L218) —— Anlogic RAM 用 `dia/cea/ceb/dob` 端口名，与 Xilinx 的 `dina/wea/enb/doutb` 不同；两套 IP 故意取同名 `Delay`，靠注释切换平台。

#### 4.2.4 代码实践

**实践目标**：用现成的 `tb/delay_tb.v` 理解 `wea` 激励与 `dout` 出现时机的关系。

**操作步骤**：
1. 打开 [tb/delay_tb.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/tb/delay_tb.v)，看清激励时序：`rst=1` 持续 30ns 后拉低；再等 300ns 后 `r_wea=1` 持续 1000ns，然后 `r_wea=0`（[delay_tb.v:18-27](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/tb/delay_tb.v#L18-L27)）。
2. 确认被测对象是 `delay #(.layer(5))`（[delay_tb.v:31-42](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/tb/delay_tb.v#L31-L42)），激励数据由 `data_gen #(.layer(4))` 产生递增的 `data_real`/`data_img`。
3. 在仿真器（Vivado/iverilog）里跑这个 testbench，把 `data_real`（输入）、`r_wea`、`delay_real_16`（输出）、`w_out_first`/`w_out_last` 放进波形窗口。

**需要观察的现象**：
- `r_wea` 拉高后，`delay_real_16` 是否要等大约 `DELAY_TIME`（layer=5 时 \(2^4=16\) 拍）才出现第一个有效数据？
- `w_out_first` 脉冲是否正好对齐第一个有效输出数据？
- `r_wea` 拉低后，是否还能在 `delay_real_16` 上看到「尾部」数据继续输出，直到 `w_out_last` 脉冲？

**预期结果**：输入在 `wea` 高期间被顺序写入；约一个 `DELAY_TIME` 后 `out_first` 拉高、`dout` 开始吐出延时数据；`wea` 拉低后 RAM 里残留的数据被排空输出，排空结束于 `out_last` 脉冲。
**待本地验证**：因依赖厂商 `Delay`/`mult2` 等 IP，能否直接跑通取决于本地是否已生成这些 IP；若仅有 iverilog 而无 IP，可用行为级双口 RAM 模型替换 `Delay` 后再仿真。

#### 4.2.5 小练习与答案

**练习 1**：`wea` 接的是模块输入端口 `wea`，但 RAM 例化里 `.wea(1)` 写的是常量 1，这两者矛盾吗？

**答案**：不矛盾。模块输入 `wea` 控制的是**写地址 `r_addra` 是否推进**；RAM 的 `.wea(1)` 表示 RAM 每拍都在写。真正的「写第几个地址」由 `r_addra` 决定，而 `r_addra` 是否推进由模块输入 `wea` 决定。所以「写到哪」由 `wea` 间接控制，二者职责不同。

**练习 2**：为什么需要两块 RAM（`delay_real` 和 `delay_img`），不能合并成一块？

**答案**：复数数据有实部和虚部两部分，且位宽都是 32 位。用两块独立 RAM 各存一路，读地址 `r_addrb` 共享，逻辑清晰、综合工具也容易把它们映射到两块 BRAM。若要合并，需要把实虚部拼接成 64 位再存，会增加单块 RAM 位宽、降低通用性。当前做法是典型的「实虚分存」。

### 4.3 五状态机：建立 / 输出 / 排空三阶段

#### 4.3.1 概念说明

光有 RAM 还不够——还需要一个状态机来回答三个问题：(1) 什么时候 RAM 已经「存够」了，可以开始读？(2) 输入数据什么时候结束？(3) 输入结束后，RAM 里残留的数据怎么排空？`delay.v` 用 5 个状态回答：

| 状态 | 含义 | 对应阶段 |
| --- | --- | --- |
| `STATE_IDLE` | 空闲，等 `wea` 上升沿 | —— |
| `STATE_DELAY` | 写指针在跑，读指针还不动（填满延时） | 延时建立 |
| `STATE_OUT` | 边写边读，正常输出 | 正常输出 |
| `STATE_TAIL` | 输入已停，只读不写，排空残留数据 | 尾部排空 |
| `STATE_END` | 一拍过渡，回 `IDLE` | —— |

学习目标里说的「延时建立 / 正常输出 / 尾部排空三个阶段」就是 `DELAY` / `OUT` / `TAIL` 三个状态。

#### 4.3.2 核心流程

状态转移（伪代码）：

```
IDLE  : if r_write_trig==1                 → DELAY   // 检测到 wea 上升沿，开始填延时
DELAY : if r_delay_cnt == required_delay   → OUT     // 延时填满，开始读
OUT   : if r_write_trig==1                 → TAIL    // 检测到 wea 下降沿，输入结束
TAIL  : if r_tail_cnt == required_delay    → END     // 残留数据排空
END   : 无条件                              → IDLE
```

其中关键计数器与触发信号：

- `r_write_trig = r_wea_1d ^ wea`（异或）—— `wea` 的**任意跳变沿**都会让 `r_write_trig` 拉高 1 拍。`wea` 上升沿（0→1）触发 `IDLE→DELAY`；`wea` 下降沿（1→0）触发 `OUT→TAIL`。一个信号、两次复用。
- `required_delay_in_state_machine = DELAY_TIME - 1 - 3 - 1` = `DELAY_TIME - 5`——状态机**内部**等待的拍数。`DELAY_TIME` 是名义延时（半周期），但进入 `DELAY` 态之前已经花掉若干拍（边沿检测寄存器 `r_wea_1d`、`r_write_trig` 各 1 拍，以及读地址/RAM 读延迟），所以状态机内部只数 `DELAY_TIME - 5` 拍，扣掉这 5 拍的固定开销，使**端到端**净延时回到 `DELAY_TIME`。

> 名词解释：`-5` 里的「-1-3-1」可以理解为「1 拍 wea 边沿检测 + 3 拍状态/地址流水 + 1 拍读出对齐」。这是典型的「状态机内部计数 = 名义延时 − 流水开销」的补偿写法。

`DELAY_TIME` 与 layer 的关系：

\[
\text{DELAY\_TIME} = 2^{(\text{layer}-1)} = \frac{\text{PERIOD}}{2}
\]

对 layer=4（`fft_16` 用的延时）：\(\text{DELAY\_TIME}=2^3=8\)，\(\text{required\_delay}=8-5=3\)。

#### 4.3.3 源码精读

状态定义：

[src/delay.v:38-43](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v#L38-L43) —— 5 个状态 `STATE_IDLE/DELAY/OUT/TAIL/END`。

参数定义（延时量与补偿）：

[src/delay.v:17-18](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v#L17-L18) —— `DELAY_TIME = 1<<(layer-1)`；`required_delay_in_state_machine = DELAY_TIME - 1 - 3 - 1`。注意上方被注释掉的旧写法 `required_delay_in_machine = DELAY_TIME - 5`（[delay.v:15-16](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v#L15-L16)），等价但不如展开形式直观。

状态机主体：

[src/delay.v:45-86](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v#L45-L86) —— 五段 `case`：
- `IDLE → DELAY`：`if(r_write_trig==1)`（[delay.v:50-56](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v#L50-L56)）。
- `DELAY → OUT`：`if(r_delay_cnt == required_delay_in_state_machine)`（[delay.v:57-63](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v#L57-L63)）。
- `OUT → TAIL`：`if(r_write_trig==1)`（[delay.v:64-70](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v#L64-L70)）——同一个 `r_write_trig`，这次是 `wea` 下降沿。
- `TAIL → END`：`if(r_tail_cnt == required_delay_in_state_machine)`（[delay.v:72-78](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v#L72-L78)）。
- `END → IDLE`：无条件（[delay.v:80-82](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v#L80-L82)）。

触发信号 `r_write_trig`（边沿检测）：

[src/delay.v:101-115](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v#L101-L115) —— 先把 `wea` 打一拍得 `r_wea_1d`，再 `r_write_trig <= r_wea_1d ^ wea`。`wea` 跳变的那一拍两者不等，异或为 1。

两个阶段计数器：

[src/delay.v:117-127](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v#L117-L127) —— `r_delay_cnt` 仅在 `STATE_DELAY` 自增（控制建立阶段长度）。
[src/delay.v:129-139](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v#L129-L139) —— `r_tail_cnt` 仅在 `STATE_TAIL` 自增（控制排空阶段长度）。两者都数到 `required_delay_in_state_machine`。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：以 `fft_16` 用的 `delay #(.layer(4))` 为例，亲手算出关键常量，并画出状态机转移图。

**操作步骤**：
1. 代入 `layer=4` 计算（写在纸上）：
   - `DELAY_TIME = 1<<(4-1) = ?`
   - `required_delay_in_state_machine = DELAY_TIME - 1 - 3 - 1 = ?`
2. 画出 5 个状态节点，按 [src/delay.v:45-86](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v#L45-L86) 标出每条转移弧的条件。
3. 另算一遍 `layer=5`（即 `tb/delay_tb.v` 里用的值）的两个常量，对比。

**需要观察的现象**：`DELAY` 态和 `TAIL` 态用的是不是同一个 `required_delay` 常量？`layer` 每加 1，延时量翻几倍？

**预期结果**：
- `layer=4`：`DELAY_TIME = 8`，`required_delay_in_state_machine = 3`。
- `layer=5`：`DELAY_TIME = 16`，`required_delay_in_state_machine = 11`。
- `DELAY` 与 `TAIL` 复用同一个 `required_delay_in_state_machine`（建立多长、排空就多长，因为存了多少就要吐多少）。
- `layer` 每加 1，`DELAY_TIME` 翻倍（因为 \(2^{(\text{layer}-1)}\)）。

**状态机转移图（参考答案）**：

```
        r_write_trig==1                  r_delay_cnt==3
  IDLE ───────────────────▶ DELAY ──────────────────────▶ OUT
   ▲                           │                            │
   │                           │ (无转移，数到 3 才走)        │ r_write_trig==1
   │                           ▼                            ▼
  END ◀── r_tail_cnt==3 ─── TAIL ◀──────────────────────────┘
   │                      (排空残留)
   └────── 无条件 ─────────▶ (回 IDLE)
```

> **待本地验证**：`-5` 这个常数是针对当前 RTL 流水级数标定的。若你在移植时改动了边沿检测或读地址的打拍级数（例如把 `r_write_trig` 多打一拍），`required_delay` 里的 `5` 必须同步调整，否则端到端延时会偏离半周期、FFT 结果出错。建议用仿真核对 `out_first` 到来的绝对时刻是否等于 `DELAY_TIME`。

#### 4.3.5 小练习与答案

**练习 1**：`DELAY` 态和 `TAIL` 态的退出条件都用 `required_delay_in_state_machine`，为什么对称？

**答案**：`DELAY` 态填入多少拍数据，`TAIL` 态就要排空多少拍数据——写入的数据量必须等于读出的数据量，否则 RAM 里会残留或欠数。两个阶段共享同一个常量，正是为了保证「建立多少、排空多少」的对称。

**练习 2**：`OUT → TAIL` 的触发是 `r_write_trig==1`，但 `IDLE → DELAY` 也是 `r_write_trig==1`，状态机怎么区分这两次？

**答案**：靠**当前所处状态**区分。`r_write_trig` 在 `wea` 上升沿和下降沿都会各拉高 1 次：第一次（上升沿）发生在 `IDLE`，于是走 `IDLE→DELAY`；第二次（下降沿）发生在 `OUT`，于是走 `OUT→TAIL`。同一个信号、靠状态上下文赋予两次不同含义。

**练习 3**：如果把 `required_delay_in_state_machine` 的 `-5` 误改成 `-4`（少扣 1 拍），延时会偏多还是偏少？

**答案**：会偏多 1 拍。`required_delay` 越大，`DELAY` 态等得越久，读指针起跑得越晚，端到端延时越长。少扣 1 拍 → 多等 1 拍 → 延时比半周期多 1 拍 → 蝶形配对错位 1 拍 → FFT 出错。

### 4.4 边界脉冲 out_first/out_last 与延迟补偿（含 delay_1k_plus 对比）

#### 4.4.1 概念说明

`delay` 除了输出延时数据，还要告诉上层「第一个有效输出样本」和「最后一个有效输出样本」分别在哪一拍——这就是 `out_first` 和 `out_last` 两个单拍脉冲。上层（`butterfly_general`）把它们当 `data_out_first`/`data_out_last` 继续上传，供下一级或顶层判断数据起止。

产生时机很自然：
- `out_first`：`DELAY` 态数满（延时建立完成）的那一拍——因为再过若干拍的 RAM 读延迟，第一个有效数据就到 `dout` 了。
- `out_last`：`TAIL` 态数满（排空完成）的那一拍——最后一个残留样本即将输出。

但这里有个时序陷阱：状态机判断「数满」是在**状态寄存器**里，而真正的数据要经过 **RAM 同步读**才到 `dout`，二者差几拍。所以代码把 `out_first` 又往后打了两拍（`r_out_first_2d`）、`out_last` 打了一拍（`r_out_last_1d`），让脉冲与数据在 `dout` 上对齐。

#### 4.4.2 核心流程

```
DELAY 态数满 → r_out_first ← 1（1 拍）→ r_out_first_1d → r_out_first_2d → out_first
                                  （打 2 拍，补偿 RAM 读延迟 + 输出寄存）

TAIL 态数满  → r_out_last  ← 1（1 拍）→ r_out_last_1d → out_last
                                  （打 1 拍）
```

- `out_first` 比「数满」晚 2 拍出现，对齐 RAM 读出的第一个有效样本。
- `out_last` 比「数满」晚 1 拍出现，对齐 RAM 读出的最后一个样本。
- 两者打拍数不同，是因为建立阶段的 RAM 读路径和排空阶段的有效数据出现时机不同（**待本地验证**：精确到拍的差值建议在波形上确认）。

#### 4.4.3 源码精读

`out_first` 原始脉冲（`DELAY` 数满那一拍）：

[src/delay.v:154-164](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v#L154-L164) —— `if(r_delay_state==STATE_DELAY && r_delay_cnt==required_delay_in_state_machine) r_out_first<=1;`

`out_last` 原始脉冲（`TAIL` 数满那一拍）：

[src/delay.v:166-176](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v#L166-L176) —— `if(r_delay_state==STATE_TAIL && r_tail_cnt==required_delay_in_state_machine) r_out_last<=1;`

打拍对齐与对外输出：

[src/delay.v:177-189](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v#L177-L189) —— `r_out_first` 经 `r_out_first_1d`、`r_out_first_2d` 两级，`assign out_first = r_out_first_2d`；`r_out_last` 经 `r_out_last_1d` 一级，`assign out_last = r_out_last_1d`。

变体 `delay_1k_plus.v` 的差异（对比表）：

| 项 | `delay.v` | `delay_1k_plus.v` |
| --- | --- | --- |
| 默认 `layer` | 1 | 11 |
| `r_addra`/`r_addrb`/`r_halt` 位宽 | `[13:0]`（14 位） | `[12:0]`（13 位） |
| `r_tail_cnt` 位宽 | `[13:0]`（14 位，最大 16383） | `[8:0]`（9 位，最大 511） |
| 状态机 / 逻辑 | —— | 与 `delay.v` **完全相同** |
| 是否被例化 | 是（`butterfly_general`、`fft_16`） | **否**（仓库内无任何例化） |

参见 [delay_1k_plus.v:7](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay_1k_plus.v#L7)（默认 `layer=11`）、[delay_1k_plus.v:25-31](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay_1k_plus.v#L25-L31)（更窄的计数器）。逻辑主体与 `delay.v` 一致，可对照 [delay_1k_plus.v:50-91](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay_1k_plus.v#L50-L91)。

> **重要提醒**：`delay_1k_plus.v` 在当前仓库中**没有任何模块例化它**（所有 `fft_32`~`fft_16k` 都走 `butterfly_general` → `delay`）。它看起来是为大点数层预留的实验版本，但其 `r_tail_cnt` 只有 9 位，而 `layer≥10` 时 `required_delay = 2^(layer-1)−5` 已远超 511（如 `layer=11` 时为 1019），计数器会溢出。**若要启用它，必须先扩宽 `r_tail_cnt` 并在仿真中验证**（待本地验证）。

#### 4.4.4 代码实践

**实践目标**：核对 `out_first`/`out_last` 的打拍延迟，并评估 `delay_1k_plus` 的可用性。

**操作步骤**：
1. 在 [src/delay.v:177-189](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v#L177-L189) 数清楚 `r_out_first` 到对外 `out_first` 经过几个寄存器；`r_out_last` 到 `out_last` 经过几个。
2. 打开 [src/delay_1k_plus.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay_1k_plus.v)，逐行与 `delay.v` 对比，找出所有不同的行。
3. 在仓库内搜索 `delay_1k_plus` 的例化（预期找不到），确认它是死代码。

**需要观察的现象**：`out_first` 与 `out_last` 的打拍数差几个？`delay_1k_plus` 除了位宽和默认 `layer`，逻辑有没有变？

**预期结果**：`out_first` 打 2 拍、`out_last` 打 1 拍；`delay_1k_plus` 逻辑与 `delay.v` 完全一致，差别仅在计数器位宽与默认 `layer`，且当前未被使用。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `out_first` 要打 2 拍、`out_last` 只打 1 拍？

**答案**：`out_first` 标的是「第一个有效输出样本」，它需要在 `dout` 上与该样本同时出现。从「`DELAY` 数满」到「第一个样本真的从 RAM 读出到 `dout`」，要经过 RAM 的同步读延迟和输出寄存，路径较长，故打 2 拍补偿。`out_last` 标的是「最后一个样本」，此时 RAM 读路径已稳定，补偿拍数较少。精确差值需在波形上核对（待本地验证）。

**练习 2**：如果上层完全忽略 `out_first`/`out_last`，`delay` 还能正常延时吗？

**答案**：数据延时功能不受影响——`dout` 依旧按时输出延时数据。但上层会失去「数据起止时刻」的标记，无法知道哪些 `dout` 样本是有效的、哪一拍是第一个/最后一个，从而无法正确驱动下一级 start 或判断本级完成。所以这两个脉冲是「功能性握手」而非「数据通路」的一部分。

## 5. 综合实践

把本讲四个最小模块串起来，完成一个「延时单元时序手册」小任务：

**任务**：选定 `delay #(.layer(4))`（即 `fft_16` 的延时），整理出它从「`wea` 上升沿」到「`out_last` 脉冲」的完整时序故事。

**要求产出**（一份简短文档或注释）：
1. 列出两个关键常量：`DELAY_TIME`、`required_delay_in_state_machine` 的数值。
2. 画出 5 状态转移图，标注每条转移弧的条件信号（`r_write_trig` / `r_delay_cnt` / `r_tail_cnt`）。
3. 用一张时序示意图（文字版即可）说明：`wea` 上升沿后，`r_addra` 何时开始推进、`r_addrb` 何时开始推进、`dout` 上的第一个有效样本大约在第几拍出现、`out_first` 在第几拍拉高。
4. 指出「-5」补偿对应的是哪几级流水开销，并说明移植时若改动打拍级数要同步修改哪个常量。
5. 给 `delay_1k_plus.v` 写一句话结论：它当前是否可用、若要用于 `layer=11` 必须先改什么。

**自检方法**：把第 3 步的预测拿到 `tb/delay_tb.v`（注意它用的是 `layer=5`，需把你的预测也换算到 `layer=5`）的仿真波形上比对；若手头没有厂商 IP，可用一段行为级双口 RAM 替换 `Delay` 后再仿真（待本地验证）。

## 6. 本讲小结

- `delay.v` 是 SDF 流水线的「反馈心脏」：把蝶形下支 B 存起来、延时 \(2^{(\text{layer}-1)}\) 拍（半个 PERIOD）后当上支 C 喂回去，使「相隔半周期的样本」得以配对。
- 存储用两块双口 RAM（`Delay` IP，实/虚分存），写地址 `r_addra` 随外部 `wea` 推进、读地址 `r_addrb` 仅在输出阶段推进；`.wea(1)` 常写、用地址推进与否来控制实际写入。
- 五状态机 `IDLE→DELAY→OUT→TAIL→END` 把工作切成「延时建立 / 正常输出 / 尾部排空」三阶段；`r_write_trig = r_wea_1d ^ wea` 一个信号在上升沿触发建立、下降沿触发排空。
- 名义延时 `DELAY_TIME = 1<<(layer-1)`，状态机内部只数 `DELAY_TIME − 1 − 3 − 1`（即 −5），扣掉边沿检测/状态/读出流水开销，使端到端净延时回到半周期。
- `out_first`/`out_last` 单拍脉冲标记首末有效样本，分别打 2 拍/1 拍对齐 RAM 读延迟。
- `delay_1k_plus.v` 是逻辑相同、计数器位宽更窄、默认 `layer=11` 的变体，**当前未被例化**，且 `r_tail_cnt` 仅 9 位不足以支撑大 layer，启用前需扩宽并仿真验证。

## 7. 下一步学习建议

本讲搞定了「存储 + 状态机」这一半，下一讲 [u3-l3 时序对齐与跨级握手](u3-l3-timing-alignment-and-handshake.md) 会把 `delay` 放回 `butterfly_general` 的完整上下文，解决整条流水线最难的时序对齐：`rotator_valid` 何时拉高让旋转因子与蝶形 D 输出对齐、`HALT_FOR_NEXT_LAYER` 如何决定下一级 `start` 时机、以及 Anlogic 与 Vivado 两套版本因 ROM 读延迟不同导致的 `-2` vs `-3` 差异。

进阶阅读建议：
- 想看「延时最简形态」可先读 [src/fft_8.v:160-184](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_8.v#L160-L184) 的寄存器版本，再回头看 RAM 版本会更顺。
- 想理解「半周期延时」的算法根源可回看 [u1-l3 算法基础](u1-l3-fft-algorithm-foundation.md) 的 Cooley-Tukey 蝶形配对。
- 准备移植的同学可预习 [u5-l3 平台移植与 IP 依赖](u5-l3-platform-porting-and-ip.md)，那里会系统讲 `Delay`/`mult2`/ROM 等 IP 的替换清单和 `HALT` 常量校准。
