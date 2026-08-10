# 数据流总览：fft_top 顶层模块如何串起整条流水线

## 1. 本讲目标

本讲是「入门单元」的收官篇。前面三讲你已经知道了：fpga-fft 是什么（u1-l1）、仓库怎么组织（u1-l2）、FFT 算法的数学原理（u1-l3）。本讲要把这些拼起来，回答一个关键问题：

> 这 14 级硬件模块，到底是怎么被「串」成一条能算 16384 点 FFT 的流水线的？

读完本讲，你应该能够：

1. 看懂顶层模块 `fft_top` 的全部对外端口（`clk / rst / start / data_real / data_img / out_real / out_img / out_first` 等），并说出每个端口的作用。
2. 理解流水线的连接方式：上一级的 `data_out` 喂给下一级的 `data_in`，`start_next` 触发下一级启动。
3. 解释为什么大点数层（`fft_16k`）排在流水线最前、`fft_2` 排在最后，并由此推出**输出为什么是 bit-reverse（位倒序）的**。

本讲只精读一个核心文件 `src/fft_top.v`，并用 `src/fft_16.v`、`src/fft_2.v` 作为「某一级内部长什么样」的样例来辅助理解。

---

## 2. 前置知识

在进入源码前，先用三段话把基础概念理顺（这些词在后面会反复出现）：

### 2.1 流水线（pipeline）

想象一条汽车装配线：原料（车架）从一端进入，经过「装发动机 → 装车门 → 喷漆」若干工位，每个工位只做一件事，成品从另一端出来。同一时刻，线上有半成品在流动。FFT 流水线也是这个思路：一串采样数据从第一级进入，每一级只做「一次蝶形 + 一次旋转因子相乘」，数据像水流一样逐级往后传。**只要前面数据还在流，后面就能持续输出**，不需要等整批算完——这就是「实时」的来源。

### 2.2 级（stage / layer）与点数 N 的关系

16384 点 FFT，因为 \( 16384 = 2^{14} \)，所以 Cooley-Tukey 分治需要 \(\log_2 N = 14\) 级。本项目的 `fft_top` 里恰好有 14 个模块实例（`fft_16k` 到 `fft_2`），每一级对应分治的一层。本项目用 `layer` 这个参数标记层级：`fft_16k` 的 `layer=14`，`fft_2` 的 `layer=1`，逐级递减。

### 2.3 DIF 与 bit-reverse 倒序（承接 u1-l3）

在 u1-l3 我们学过两条等价的 FFT 路线：

- **DIF（Decimation In Frequency，频率抽取）**：输入自然顺序，**先做蝶形、再做旋转因子相乘**，输出是**位倒序（bit-reversed）**的。
- **DIT（Decimation In Time，时间抽取）**：先倒序、再做蝶形。

本项目的蝶形单元是「先加减（A+C / C-A）、后乘旋转因子」，这正是 **DIF 路线**，因此 `fft_top` 最终输出的频谱序列是 bit-reverse 顺序，需要外部再重排一次（这一步硬件尚未实现，见 u1-l1）。本讲末尾会把这个结论和「大点数层排在最前」联系起来。

> 位倒序的含义：对索引 \( n = (b_{L-1}\dots b_1 b_0)_2 \)，其倒序索引为 \( \text{rev}(n) = (b_0 b_1 \dots b_{L-1})_2 \)。例如 8 点 FFT 的输出索引顺序是 0,4,2,6,1,5,3,7，而不是 0,1,2,3,4,5,6,7。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲中的角色 |
| --- | --- | --- |
| `src/fft_top.v` | 顶层模块，把 14 级首尾相连 | **唯一核心精读对象** |
| `src/fft_16.v` | 第 11 级（layer=4）的代表实现 | 用来展示「高层级内部的标准四件套结构」，帮助你理解 `data_in/data_out/start_next` 这些端口在级内部是怎么产生的 |
| `src/fft_2.v` | 第 14 级、也就是最后一级（layer=1） | 用来展示「最末级」如何省略乘法器和 RAM，并产生整条流水线的 `out_first` 脉冲 |

一句话总览：`fft_top` 本身**不含任何运算逻辑**，它只做「连线」——声明若干 `wire`，把上一级的输出端口接到下一级的输入端口。

---

## 4. 核心概念与源码讲解

本讲把 `fft_top.v` 拆成 4 个最小模块来读：

- 4.1 `fft_top` 的对外接口
- 4.2 14 级流水线的级联方式（数据流）
- 4.3 级间握手（`start_next → start`，以及 `over` / `end` 链）
- 4.4 大点数层在前、fft_2 在后 → 输出为何是 bit-reverse

---

### 4.1 fft_top 的对外接口

#### 4.1.1 概念说明

顶层模块是整个 IP 对外的「门面」。使用者（比如另一个 SoC 模块或 testbench）不需要知道内部有 14 级，只需要按 `fft_top` 的端口表接线：给时钟和复位、给 `start`、喂入实部/虚部数据，就能从 `out_real/out_img` 拿到频谱结果。

#### 4.1.2 核心流程

使用 `fft_top` 的典型流程（伪代码）：

```text
上电 → 拉高 rst 复位 → 拉低 rst
       ↓
持续从外部把 N=16384 个采样点按节拍送到 data_real / data_img
同时给一个 start 脉冲启动 fft_16k
       ↓
数据逐级流过 14 级流水线
       ↓
fft_2 的 out_start 拉高 → 顶层的 out_first 输出第一个脉冲
之后每个时钟从 out_real / out_img 读一个频谱点（注意：是 bit-reverse 顺序）
```

#### 4.1.3 源码精读

端口声明集中在文件开头：

[src/fft_top.v:8-20](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L8-L20) —— `fft_top` 模块的完整端口表，逐行定义了 clk/rst/start/over/data_config/data_real/data_img/out_real/out_img/out_first/out_last。

整理成表：

| 端口 | 方向 | 位宽 | 含义 |
| --- | --- | --- | --- |
| `clk` | input | 1 | 时钟 |
| `rst` | input | 1 | 复位（高电平有效） |
| `start` | input | 1 | 启动整条流水线（直接喂给 `fft_16k`） |
| `over` | input | 1 | 输入数据结束标志（喂给 `fft_16k`） |
| `data_config` | input | 4 | **声明了，但模块体内未被使用**（见下方说明） |
| `data_real` | input | 32 | 输入复数的实部 |
| `data_img` | input | 32 | 输入复数的虚部 |
| `out_real` | output | 32 | 输出频谱的实部（bit-reverse 顺序） |
| `out_img` | output | 32 | 输出频谱的虚部（bit-reverse 顺序） |
| `out_first` | output | 1 | 第一个有效输出脉冲（来自末级 `fft_2.out_start`） |
| `out_last` | output | 1 | **声明了，但模块体内未见赋值** |

> **两个需要注意的端口（诚实说明）：**
> - `data_config`（[src/fft_top.v:13](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L13)）本意是「配置 FFT 点数」（例如选 1024/4096/16384），但在整个 `fft_top` 里没有任何代码读取它，点数被固定为 16384。这与 u1-l1 提到的「`data_config` 仅声明未接线」一致。
> - `out_last`（[src/fft_top.v:19](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L19)）在端口表里出现，但在模块结尾的 `assign` 处（[src/fft_top.v:263-265](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L263-L265)）只给 `out_real`、`out_img`、`out_first` 做了赋值，**没有给 `out_last` 赋值**，它当前悬空（待确认是否为遗留的未完成功能）。

末尾的三条赋值语句确认了输出来源：

[src/fft_top.v:263-265](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L263-L265) —— `out_real / out_img` 直接取自末级 `fft_2` 的输出 `w_out_real2 / w_out_img2`；`out_first` 取自末级 `fft_2` 的 `w_out_start`。也就是说，**整条流水线的最终结果，就是最后一级 fft_2 的输出**。

#### 4.1.4 代码实践

**实践目标：** 通过静态阅读，确认 `fft_top` 的对外契约，避免接线时踩坑。

**操作步骤：**
1. 打开 `src/fft_top.v`，定位到第 8–20 行的端口表。
2. 在你的笔记里画一张「外部 ↔ fft_top」的接线图：左侧列出 clk/rst/start/data_real/data_img 进入，右侧列出 out_real/out_img/out_first 流出。
3. 搜索整个文件里是否出现 `data_config`（用编辑器查找）。你会发现除了端口声明，模块体内没有任何引用。

**需要观察的现象 / 预期结果：**
- 你会确认 `data_config` 在模块体内 0 次使用。
- 你会确认 `out_last` 在第 263–265 行的赋值列表中**缺席**。

> 本实践为「源码阅读型」，无需运行仿真；结论待本地用编辑器查找复核。

#### 4.1.5 小练习与答案

**练习 1：** `fft_top` 的复位是高有效还是低有效？依据是什么？

> **参考答案：** 高有效。端口声明是 `input rst`（[src/fft_top.v:10](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L10)），而内部各级（如 `fft_16.v` 中 `if(rst == 1)` 走复位分支，[src/fft_16.v:39](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16.v#L39)）也以 `rst==1` 作为复位条件。注意有些子模块（如 `multiplier`）用的是 `rstn = ~rst`，那是子模块内部把高有效反转成低有效来用。

**练习 2：** 如果使用者误以为 `out_last` 会输出「最后一个结果」的脉冲来计数，会发生什么？

> **参考答案：** 会一直拿到 0（或综合后的不定值），因为 `out_last` 在 `fft_top` 里没有被赋值。使用者不能依赖它来判断一帧结束，必须自己用点数 N=16384 来计数。

---

### 4.2 14 级流水线的级联方式（数据流）

#### 4.2.1 概念说明

`fft_top` 的主体就是「14 个模块实例 + 一堆 wire」。它的核心动作只有两个：

1. **数据流**：上一级的 `data_out`（实部+虚部）接到下一级的 `data_in`。
2. **握手流**：上一级的 `start_next` 接到下一级的 `start`（4.3 节详讲）。

本节只看数据流。你会看到数据像水管一样，从 `fft_16k` 一路流到 `fft_2`。

#### 4.2.2 核心流程

```
外部 data_real/data_img
        │  (实部+虚部, 各 32 位)
        ▼
     fft_16k ──w_out_real/img_16k──▶ fft_8k ──w_out_..._8k──▶ fft_4k ──▶ fft_2k
        ┊                                                                           
        ┊   （中间依次经过 1k → 512 → 256 → 128 → 64 → 32）                          
        ┊                                                                           
        ▼                                                                           
     fft_16 ──▶ fft_8 ──▶ fft_4 ──▶ fft_2 ──▶ out_real/out_img (顶层输出)
```

要点：
- **数据方向**永远是从大点数层流向小点数层（16k → 2）。
- 每一级之间用一对 `wire`（实部一条、虚部一条）连接。
- 最末级 `fft_2` 的输出直接赋给顶层 `out_real/out_img`。

#### 4.2.3 源码精读

**入口：外部数据喂给第一级 fft_16k。**

[src/fft_top.v:26-37](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L26-L37) —— `fft_16k` 实例。注意 `.data_in_real(data_real)`、`.data_in_img(data_img)`，即外部输入直接进了第一级；它的输出是 `.data_out_real(w_out_real_16k)`、`.data_out_img(w_out_img_16k)`，用一对 wire 引出。

**中间：上一级输出 → 下一级输入。**

以 `fft_8k` 为例：

[src/fft_top.v:43-54](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L43-L54) —— `fft_8k` 实例。关键两行：
- `.data_in_real(w_out_real_16k)`：输入取自上一级 `fft_16k` 的实部输出；
- `.data_out_real(w_out_real_8k)`：输出用新的一对 wire 引出，供下下级使用。

这个「上一级 out → 下一级 in」的模式在 `fft_8k`、`fft_4k`、`fft_2k`、`fft_1k`、`fft_512`、`fft_256`、`fft_128`、`fft_64`、`fft_32` 上一模一样地重复了 9 次。

**端口的「命名风格」在 fft_16 处发生了切换——这是一个容易看漏的细节：**

从 `fft_16k` 到 `fft_32`，这 10 个高层模块用的是**统一命名**：`data_in_real / data_in_img / data_out_real / data_out_img`。

但从 `fft_16` 开始的 4 个低层模块用的是**另一套命名**：`A_real / A_img`（输入）、`out_real_16 / out_img_16`（输出，名字里带点数）。切换点就在 fft_16 的实例化处：

[src/fft_top.v:199-210](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L199-L210) —— `fft_16` 实例。注意它接的是 `.A_real(w_out_real_32)`、`.out_real_16(w_out_real_16)`，端口名和前面 10 个高层模块完全不同。这是因为高层模块是「参数化批量生成」的（用 `butterfly_general`，见 u4-l3），而 `fft_16/8/4/2` 是早期手写的，沿用了自己的命名习惯。

**出口：末级 fft_2 的输出送给顶层。**

[src/fft_top.v:251-261](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L251-L261) —— `fft_2` 实例。它的输入 `.A_real(w_out_real4)` 来自上一级 `fft_4`，输出 `out_real2 / out_img2` 即整条流水线的最终结果。

#### 4.2.4 代码实践

**实践目标：** 把 14 级的数据流连线亲手梳理一遍，形成一张可查的表。

**操作步骤：**
1. 打开 `src/fft_top.v`，按实例出现顺序，把每一级的「输入 wire 名 → 模块名 → 输出 wire 名」抄成一张表。
2. 重点标注从 `fft_32` 到 `fft_16` 这一处端口命名风格的变化。

**预期结果（节选，供你对照）：**

| 序号 | 模块 | 输入 wire（实部） | 输出 wire（实部） | 端口名风格 |
| --- | --- | --- | --- | --- |
| 1 | fft_16k | data_real | w_out_real_16k | 统一风格 |
| 2 | fft_8k | w_out_real_16k | w_out_real_8k | 统一风格 |
| … | … | … | … | … |
| 10 | fft_32 | w_out_real_64 | w_out_real_32 | 统一风格 |
| 11 | fft_16 | w_out_real_32 | w_out_real_16 | **A_real / out_real_16** |
| 12 | fft_8 | w_out_real_16 | w_out_real8 | **A_real / out_real8** |
| 13 | fft_4 | w_out_real8 | w_out_real4 | **A_real / out_real4** |
| 14 | fft_2 | w_out_real4 | w_out_real2 | **A_real / out_real2** |

> 本实践为「源码阅读型」，无需运行；建议你先自己填，再与上表对照。

#### 4.2.5 小练习与答案

**练习 1：** 整条流水线一共有几级？这个数字和「16384 点 FFT」有什么关系？

> **参考答案：** 14 级。因为 \( 16384 = 2^{14} \)，Cooley-Tukey 分治需要 \(\log_2 16384 = 14\) 层，所以硬件也对应 14 个模块实例。

**练习 2：** 如果只看端口命名，你能从 `fft_top.v` 里一眼分辨出「这个实例是高层模块还是低层手写模块」吗？依据是什么？

> **参考答案：** 能。用统一风格（`data_in_real / data_out_real / start / start_next`）的是高层模块（16k~32）；用 `A_real / out_real_N / startN` 的是低层手写模块（16/8/4/2）。

---

### 4.3 级间握手：start_next → start，以及 over / end 链

#### 4.3.1 概念说明

光有数据线还不够。每一级内部都有状态机（IDLE/START/PROCESSING/END），必须有人「喊它开始」，它才会从 IDLE 进入工作状态。这个「喊开始」的信号，就是由**上一级的 `start_next`** 提供的——上一级算到一定程度、确认有有效数据要往下传时，就拉一下 `start_next`，启动下一级。

这样就形成了一条**启动链**：外部 `start` 启动 `fft_16k`，`fft_16k` 算好后用 `start_next` 启动 `fft_8k`，依次类推，直到 `fft_2`。

#### 4.3.2 核心流程

```
外部 start ──▶ fft_16k.start
fft_16k.start_next (w_start_8k) ──▶ fft_8k.start
fft_8k.start_next  (w_start_4k) ──▶ fft_4k.start
        …（逐级传递）…
fft_32.start_next  (w_start16)  ──▶ fft_16.start16
fft_16.start8      (w_start8)   ──▶ fft_8.start8
fft_8.start4       (w_start4)   ──▶ fft_4.start4
fft_4.start2       (w_start2)   ──▶ fft_2.start2
fft_2.out_start    (w_out_start)──▶ 顶层 out_first
```

> 为什么需要 `start_next` 而不是让所有级同时启动？因为每一级的延时（RAM 延时）深度不同：大点数层要攒很久才有第一个有效输出。如果同时启动，下游会空转很久、状态机时序也对不齐。所以用 `start_next` 让下一级「在该启动的时候才启动」，保证数据与状态对齐。这个时序细节在 u3-l3 会深入讲。

**关于 `over` / `end` 链（需要诚实说明的细节）：**

除了 `start`，端口里还有一组「结束」信号（`over`、`end_next`/`endN`）。在 `fft_top` 里，这组信号的连接是**不完整**的：

- 外部 `over` 只接到了 `fft_16k.over`（[src/fft_top.v:30](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L30)）。
- `fft_8k` 到 `fft_32` 这 9 个高层模块的 `over` 全部固定接 `0`（例如 [src/fft_top.v:47](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L47)），它们各自的 `end_next` 输出（`w_end_*`）虽然声明了 wire，但**没有接到下一级的 over**。
- 低层模块（fft_16/8/4/2）之间有一串 `end8/end4/end2` 信号在传，但源头 `fft_16.end16` 被固定接 `0`（[src/fft_top.v:203](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L203)）。

也就是说：**`start_next → start` 是这条流水线里真正完整、真正在用的握手机制；`over`/`end` 链基本没有真正贯通**。这与项目「部分功能未完成」的现状一致。本讲以 `start_next` 为主线讲解。

#### 4.3.3 源码精读

**启动链的源头：**

[src/fft_top.v:28-29](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L28-L29) —— `fft_16k` 的 `.start(start)`，外部 `start` 直接启动第一级。

**逐级传递（高层段）：**

[src/fft_top.v:45-46](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L45-L46) —— `fft_8k` 的 `.start(w_start_8k)`，而 `w_start_8k` 正是 `fft_16k` 的 `start_next` 输出（[src/fft_top.v:35](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L35)）。这种「上一行声明 wire、下一级 `.start()` 引用」的模式贯穿高层段。

**传递（低层段，命名变为 startN）：**

[src/fft_top.v:202](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L202) —— `fft_16` 的 `.start16(w_start16)`，`w_start16` 来自 `fft_32` 的 `start_next`（[src/fft_top.v:191](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L191)）。

[src/fft_top.v:219-225](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L219-L225) —— `fft_8` 的 `.start8(w_start8)`（来自 fft_16）并产出 `.start4(w_start4)` 给 fft_4。

**启动链的末端：**

[src/fft_top.v:256-260](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L256-L260) —— `fft_2` 的 `.start2(w_start2)` 接收启动，`.out_start(w_out_start)` 产出整条流水线的首个有效输出脉冲。

**`start_next` 在级内部是怎么产生的（用 fft_16 做样例）：**

[src/fft_16.v:112-142](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16.v#L112-L142) —— `fft_16` 内部用一个计数器 `start8_counter`，当它数到 `HALT_FOR_NEXT_LAYER-3` 时，把 `r_start8` 拉高一拍，作为下一级（fft_8）的 `start8`。这里的 `HALT_FOR_NEXT_LAYER = 6 + PERIOD/2`（[src/fft_16.v:22-23](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16.v#L22-L23)），是一个精心调好的「等待时长」，保证下一级启动时，本级正好有有效数据要往下传。（时序细节留到 u3-l3。）

> 旁注：`fft_16.v` 第 118–121 行有一条重要注释（[src/fft_16.v:118-121](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16.v#L118-L121)），说明 `-2` 用于 anlogic 版、`-3` 用于 vivado 版，原因是两个平台的 ROM 读取延迟不同。这是后续移植讲义（u5-l3）的伏笔。

#### 4.3.4 代码实践

**实践目标：** 在源码里亲手追踪一次启动信号，验证「逐级传递」的说法。

**操作步骤：**
1. 在 `fft_top.v` 里定位 `w_start_8k` 的声明（约第 24 行）。
2. 找到它被赋值的地方（fft_16k 的 `.start_next`，第 35 行）。
3. 找到它被使用的地方（fft_8k 的 `.start`，第 46 行）。
4. 对 `w_start16`（声明约第 180 行）重复同样三步追踪。

**需要观察的现象 / 预期结果：**
- 每个启动 wire 都恰好出现「声明 1 次 + 被上一级驱动 1 次 + 驱动下一级 1 次」三处。
- 你会直观看到信号是「自上而下」单向流动的，没有反馈。

> 本实践为「源码阅读型」，无需运行。

#### 4.3.5 小练习与答案

**练习 1：** 为什么不能用「一根全局 start 同时启动所有 14 级」来代替 `start_next` 链？

> **参考答案：** 因为各级的 RAM 延时深度不同（大点数层要攒几千拍才有第一个有效输出）。如果同时启动，下游级会在很长一段时间内收不到有效数据，状态机和数据无法对齐，输出时序会错乱。`start_next` 让每一级「在上一级开始有有效输出时」才启动，从而保证数据与状态对齐。

**练习 2：** `over` 信号在 `fft_top` 里真正接通的是哪一级？其余高层模块的 `over` 接的是什么？

> **参考答案：** 真正接通外部 `over` 的只有 `fft_16k`（[src/fft_top.v:30](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L30)）。其余高层模块（fft_8k~fft_32）的 `over` 都固定接 `0`（如 [src/fft_top.v:47](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L47)），即 `over`/`end` 链在顶层并未真正贯通。

---

### 4.4 大点数层在前、fft_2 在后 → 输出为何是 bit-reverse

#### 4.4.1 概念说明

到目前为止你可能有个疑问：为什么数据要先经过最大的 `fft_16k`（延时最深），最后才到最小的 `fft_2`？能不能反过来排？

答案是：**不能，这是由 DIF 算法结构决定的，也直接决定了输出是 bit-reverse 顺序。**

回忆 u1-l3：DIF 的第一级把 N 个点按「前一半 / 后一半」分开，做的是跨度为 \(N/2\) 的蝶形——也就是说，第一级需要把相距 \(N/2\) 个采样点的两个数据配对。要做到这点，硬件必须先攒够 \(N/2 = 8192\) 个点（放在延时 RAM 里），才能开始做第一个蝶形。**这个「最大的延时」天然属于第一级。**

#### 4.4.2 核心流程

每一级的延时深度（即 RAM 要攒多少个点）由 `layer` 决定（`delay #(.layer(...))`，详见 u3-l2）：

| 级（从输入起） | 模块 | layer | 延时深度（点数） |
| --- | --- | --- | --- |
| 第 1 级 | fft_16k | 14 | \(2^{13} = 8192\) |
| 第 2 级 | fft_8k | 13 | \(2^{12} = 4096\) |
| … | … | … | … |
| 第 11 级 | fft_16 | 4 | \(2^3 = 8\) |
| 第 12 级 | fft_8 | 3 | \(2^2 = 4\) |
| 第 13 级 | fft_4 | 2 | \(2^1 = 2\) |
| 第 14 级 | fft_2 | 1 | （末级，几乎无延时） |

可以看到，**延时深度从 8192 一路减半到 2**，呈严格的「前大后小」结构。这正对应 DIF 分治：第一级跨度最大（\(N/2\)），逐级减半。

**为什么大延时必须在前？两个理由：**

1. **算法正确性**：DIF 第一级要求把相距 \(N/2\) 的样本配对，只有第一级物理位置上「先收满 N/2 个点」才成立。把小延时级放前面，它配对的是相距很近的样本，对应的是 DIF 的最后一级，数学上就错了。
2. **总延迟最短（流水线效率）**：大延时级要「空等」很久才能开始（必须等够 N/2 个输入）。把它放最前，它的「填充时间」与上游无依赖、可以最早开始；它一旦开始流出，后面每一级（延时更小、填充更快）都能在前一级流出期间并行填充，整体延迟最短。如果反过来把大延时放最后，前面所有小级会先算完、再被迫停下来等最后一级慢慢攒满，效率最差。

#### 4.4.3 源码精读

**末级 fft_2：没有乘法器、没有 RAM 延时，且产生 out_first。**

[src/fft_2.v:13-14](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_2.v#L13-L14) —— 注释明说「rotator，只有一个 / 其实不需要」，因为最后一级 \(N=2\)，旋转因子 \(W_2^0 = 1\)，所以 fft_2 里**没有 multiplier、也没有 Rotator**，只剩一个蝶形。

[src/fft_2.v:128-140](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_2.v#L128-L140) —— 注释「延时单元，比较简单，不用 ram」，fft_2 用一个寄存器 `r_C_real/r_C_img` 打一拍，代替了大级里的双口 RAM 延时（[src/fft_2.v:155](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_2.v#L155) 注释「加减运算实质上充当了 delay 模块」）。

[src/fft_2.v:115-125](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_2.v#L115-L125) —— `r_out_start` 的产生逻辑，数到 `out_start_cnt==1` 时拉高一拍，经 [src/fft_2.v:143](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_2.v#L143) `assign out_start` 送出，最终成为顶层 `out_first`（[src/fft_top.v:265](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L265)）。也就是说，**整条流水线「第一个有效输出」的标志，是由最末级 fft_2 产生的**。

**对照 fft_16：一个完整的「标准级」四件套。**

[src/fft_16.v:169-180](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16.v#L169-L180) —— `delay #(.layer(4))`：RAM 延时单元，layer=4 对应延时 \(2^3=8\) 点。
[src/fft_16.v:230-236](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16.v#L230-L236) —— `Rotator16`：旋转因子 ROM。
[src/fft_16.v:239-252](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16.v#L239-L252) —— `butterfly16`：蝶形运算（先加减）。
[src/fft_16.v:255-267](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16.v#L255-L267) —— `multiplier16`：复数乘法（后乘旋转因子）。

注意这四件套的**顺序**是「蝶形 →（延时配对）→ 旋转因子 → 复数乘法」，即**先蝶形后乘旋转因子**，这正是 DIF 蝶形的特征。这也从源码侧印证了：本项目是 DIF 流水线。

**把「DIF + 大延时在前」合起来，就得到了 bit-reverse 输出：**

DIF 的数学性质决定了「自然顺序输入 → 位倒序输出」。本项目的输入从 `fft_16k`（第 1 级、自然顺序）进入，输出从 `fft_2`（第 14 级）流出，因此 `out_real/out_img` 的点序是 bit-reversed 的，**必须由使用者外部重排**才能得到自然顺序的频谱（重排硬件尚未实现，见 u1-l1）。

#### 4.4.4 代码实践

**实践目标：** 亲手算一个小的 bit-reverse 例子，建立对「输出乱序」的直觉。

**操作步骤：**
1. 取 N=8（即 3 位索引），列出 0~7 的二进制：000,001,010,011,100,101,110,111。
2. 把每个二进制串**反过来写**（位倒序），得到新值。
3. 写出倒序后的索引序列。

**预期结果：**

| 原索引 n | 二进制 | 倒序二进制 | 倒序索引 rev(n) |
| --- | --- | --- | --- |
| 0 | 000 | 000 | 0 |
| 1 | 001 | 100 | 4 |
| 2 | 010 | 010 | 2 |
| 3 | 011 | 110 | 6 |
| 4 | 100 | 001 | 1 |
| 5 | 101 | 101 | 5 |
| 6 | 110 | 011 | 3 |
| 7 | 111 | 111 | 7 |

所以 8 点 DIF 流水线输出的频谱顺序是 `0,4,2,6,1,5,3,7`。本项目 N=16384 同理会输出位倒序的点序。

> 本实践为「手算型」，结论可直接用于解释仿真波形中「输出点序看起来是乱的」现象。完整仿真验证见 u5-l2。

#### 4.4.5 小练习与答案

**练习 1：** 本项目是 DIF 还是 DIT？请从源码给出判断依据。

> **参考答案：** DIF。依据是每一级内部的顺序为「蝶形（先加减 A+C / C-A）→ 复数乘旋转因子」，例如 `fft_16.v` 里 `butterfly16`（[src/fft_16.v:239-252](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16.v#L239-L252)）在前、`multiplier16`（[src/fft_16.v:255-267](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16.v#L255-L267)）在后。DIF 的特征正是「先蝶形后乘旋转因子」。

**练习 2：** 假设有人把 `fft_2` 放到流水线最前、`fft_16k` 放到最后，结果会怎样？

> **参考答案：** 算法上会完全错误：第一级（现在是小延时）会配对相距很近的样本，这对应 DIF 的最后一级而不是第一级，蝶形跨度与 DIF 分治要求的顺序相反，输出的频谱将是错的。此外，大延时级放最后会导致前面各级算完后被迫空等，流水线效率极差。

---

## 5. 综合实践

把本讲内容串起来，完成下面这个贯穿性任务。

### 任务：画出 fft_top 的完整级联框图，并解释「大延时在前」

**步骤 1 — 画框图。** 在纸上（或任意画图工具）画出从 `fft_16k` 到 `fft_2` 的 14 个方框，按从左到右排列。在每个方框之间画两组箭头：
- 一对粗箭头表示**数据流**：上一级 `data_out_real/img` → 下一级 `data_in_real/img`（低层段改为 `out_real_N → A_real`）。
- 一根细箭头表示**启动握手**：上一级 `start_next` → 下一级 `start`（低层段是 `startN → startN`）。

在图上标注：
- 最左侧 `data_real/img`、`start` 进入 `fft_16k`；
- 最右侧 `fft_2` 输出 `out_real/img`、`out_first`；
- `fft_32 → fft_16` 那一处端口命名风格的切换。

**步骤 2 — 回答问题。** 用你自己的话（3–5 句）回答：**为什么最大的延时层（fft_16k）必须放在流水线最前面？**

参考答题要点（先自己写，再对照）：
- 算法层：DIF 第一级需要配对相距 \(N/2\) 的样本，只有先攒满 \(N/2\) 个点的「大延时」级放在最前，才满足 DIF 分治的层级顺序。
- 效率层：大延时级填充最慢，放最前能让它的填充时间与上游无依赖地最早开始，后续各级（延时更小）在前面流出期间并行填充，整体延迟最短。

**步骤 3（选做，衔接后续讲义）。** 在你画的框图上，用红笔标出「bit-reverse 发生的地方」——即在 `fft_2` 输出处加一个气泡注明「输出为位倒序，需外部重排」，并写一句：如果使用者忘了重排，会看到什么现象（提示：频谱的频率轴顺序被打乱，但幅度正确）。

> 本综合实践为「源码阅读 + 手绘」型，无需运行仿真。画完后，你可以把框图与 4.2.4 的连线表互相校验。

---

## 6. 本讲小结

- `fft_top` 是一个**纯连线**的顶层模块：它不含任何运算逻辑，只用 `wire` 把 14 级模块首尾相连。
- 它的对外端口中，`data_config` 声明了却未使用（点数固定 16384），`out_last` 声明了却未赋值——这两处反映了项目「部分功能未完成」的现状。
- **数据流**：上一级 `data_out` → 下一级 `data_in`（低层段为 `out_real_N → A_real`），方向永远从大点数层（16k）流向小点数层（2）。
- **握手流**：上一级 `start_next` → 下一级 `start`，是这条流水线真正完整在用的握手机制；`over`/`end` 链基本未贯通（只 `fft_16k` 接了外部 `over`，其余高层 `over=0`）。
- **大延时在前、小延时在后**，是 DIF 算法结构的要求（第一级跨度 \(N/2\) 最大），也使总延迟最短。
- 本项目是 **DIF 流水线**（先蝶形后乘旋转因子），因此输出是 **bit-reverse 位倒序**，需外部重排（硬件尚未实现）。

---

## 7. 下一步学习建议

本讲只看了「顶层怎么连线」，还没有真正进入任何一级的**内部**。建议接下来：

1. **进入第 2 单元（核心算子）**：先读 [src/butterfly.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly.v)（u2-l1）理解蝶形加减，再读 [src/multiplier.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/multiplier.v)（u2-l2）理解复数乘法——这是每一级内部的两个基本运算。
2. **进入第 3 单元（存储与时序）**：重点读 [src/delay.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v)（u3-l2）理解「RAM 延时」如何实现本讲提到的「攒满 N/2 个点」，以及 u3-l3 理解 `start_next` 那个 `HALT_FOR_NEXT_LAYER` 时序常量到底是怎么算出来的。
3. **如果想先看波形**：可以跳到 u5-l2，跑一个 `fft_8_tb` 的仿真，直观感受数据在各级之间流动、`out_first` 脉冲出现的那一刻。

掌握本讲后，你已经具备「俯瞰全局」的视角；后续讲义会带你逐层放大，看清每一级的内部构造。
