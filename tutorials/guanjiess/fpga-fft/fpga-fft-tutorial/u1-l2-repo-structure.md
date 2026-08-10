# 仓库结构与文件组织：src / tb / matlab / scheme

## 1. 本讲目标

学完本讲后，你应该能够：

- 看懂 `fpga-fft` 仓库的整体目录划分，知道每一类文件被放在哪里、为什么放在那里。
- 理解 `src` 目录里 `fft_2`~`fft_16k` 各级模块、`butterfly` / `delay` / `multiplier` / `Rotator` 等基础模块的**命名规律**，看到文件名就能猜出它的职责。
- 区分四类目录的不同职责：**设计源码 `src`**、**仿真测试 `tb`**、**算法参考 `matlab`**、**设计文档 `scheme`**。
- 学会借助 `scheme` 里的文档（`README.md`、`FFT.md`、`参数和问题.md`）快速建立对项目的整体认知。

承接上一讲（u1-l1）：你已经知道本项目是一条「SDF 单路延迟反馈」的实时流水线 FFT，顶层 `fft_top` 把 14 级模块首尾级联。本讲不再讲算法和动机，而是**把这条流水线在磁盘上「摊开」**，让你拿到代码就知道该从哪个文件读起。

## 2. 前置知识

- **Verilog HDL**：本项目用 Verilog 写硬件。`.v` 文件就是一个 Verilog 模块（`module ... endmodule`）。
- **模块例化（instantiation）**：在一个 `.v` 文件里写 `fft_16k fft_16k(...)` 表示把 `fft_16k` 这个模块「拿过来用一次」，括号里是把端口连到当前模块的线网上。顶层 `fft_top.v` 就是用这种方式把 14 个子模块串起来的。
- **testbench（测试平台）**：一段不会真正综合成硬件的 Verilog 代码，作用是给被测模块「喂激励、看波形」。习惯上文件名带 `_tb` 后缀。
- **参数化模块**：用 `#(parameter layer = 5)` 给模块传一个可配置参数。本项目大量用 `layer` 表示「这是第几级」，点数 \(N = 2^{\text{layer}}\)。
- **厂商 IP**：FPGA 厂商（Xilinx/Anlogic 等）提供的现成电路块，如乘法器、双口 RAM、ROM。它们在源码里以「黑盒模块名」出现（如 `mult2`、`Delay`），实际电路由厂商工具生成。

如果你对某一项完全陌生也没关系，本讲只要求你「看懂文件组织」，不要求理解每个模块内部细节——那是后续讲义的任务。

## 3. 本讲源码地图

下表列出本讲涉及的关键文件。记住一句话：**`src` 是「造出来的机器」，`tb` 是「测试台」，`matlab` 是「标准答案」，`scheme` 是「设计说明书」。**

| 路径 | 目录职责 | 本讲用来做什么 |
| --- | --- | --- |
| `README.md` | 根目录 | 作者亲写的总说明书，列出顶层接口、子模块需求、学习提示 |
| `src/fft_top.v` | `src`（设计源码） | 顶层模块，把 14 级流水线级联起来，是理解全貌的入口 |
| `scheme/FFT.md` | `scheme`（设计文档） | DFT/Cooley-Tukey/bit-reverse 的算法推导 |
| `scheme/参数和问题.md` | `scheme`（设计文档） | 参数约定与已知问题（如倒序未实现） |
| `tb/fft_top_tb.v` | `tb`（仿真测试） | 整条流水线的全链路仿真 |
| `matlab/FFT_iterative_DIF.m` | `matlab`（算法参考） | 迭代版 FFT，作为硬件输出的「黄金参考」 |

---

## 4. 核心概念与源码讲解

本讲按四个目录拆成四个最小模块：`src` → `tb` → `matlab` → `scheme`。其中 `src` 是核心，篇幅最重；其余三个相对简短但结构同样完整。

### 4.1 src 目录：Verilog 设计源码（项目核心）

#### 4.1.1 概念说明

`src`（source）存放项目的**设计源码**——也就是真正会被综合成 FPGA 电路的 Verilog 文件。整个 FFT 处理器的所有硬件逻辑都在这里。

`src` 里的文件命名非常有规律，作者用了两套命名前缀：

- **`fft_<点数>.v`**：表示流水线的「某一级」。例如 `fft_2.v` 是 2 点层、`fft_1k.v` 是 1024 点层（`1k` = 1024）、`fft_16k.v` 是 16384 点层。`fft_top.v` 是唯一的「非某一级」文件，它是把所有级串起来的顶层。
- **小写动词/功能名 `.v`**：表示被各级反复复用的「基础算子」，如 `butterfly`（蝶形）、`multiplier`（乘法）、`delay`（延时）、`data_gen`（数据生成）。
- **大写开头 `Rotator*.v`**：表示与「旋转因子」相关的模块（Rotator = 旋转器）。

掌握这套命名规律后，看到一个文件名就能大致猜出它的角色。

#### 4.1.2 核心流程：src 文件的四类划分

按职责，`src` 下的文件可以归为四类。先看整体结构（伪代码树）：

```
src/
├── fft_top.v              ← 顶层：级联全部 14 级
├── fft_2.v ~ fft_16k.v    ← 各级流水线层（14 个，对应 2¹ ~ 2¹⁴ 点）
│
├── butterfly.v            ┐
├── butterfly_general.v    │
├── multiplier.v           ├← 基础算子模块（被各级复用）
├── delay.v                │
├── delay_1k_plus.v        ┘
│
├── Rotator16.v            ┐
├── RotatorMemory8.v       ├← 旋转因子模块
├── Rotator_address.v      │
├── rom.v                  ┘
│
├── data_gen.v             ┐← 数据生成模块（仿真激励）
├── data_gen2.v            ┘
│
├── *_tb.v  (4 个)         ← 注意：少量 testbench 也混在 src 里
└── dsa.txt                ← 空文件（历史遗留）
```

各级 `fft_*` 模块都遵循同一个「四件套」组合：**蝶形运算 → RAM 延时 → 旋转因子 → 复数乘法**。区别只在于「点数」不同（即 `layer` 参数不同），所以文件名直接用点数来命名。

点数与 `layer` 参数的对应关系是 \(N = 2^{\text{layer}}\)：

| 文件 | `current_layer` | 点数 \(N = 2^{\text{layer}}\) | 在流水线中的位置 |
| --- | --- | --- | --- |
| `fft_16k.v` | 14 | 16384 | **最前级**（最先收到外部输入） |
| `fft_8k.v` | 13 | 8192 | 第 2 级 |
| `fft_4k.v` | 12 | 4096 | 第 3 级 |
| `fft_2k.v` | 11 | 2048 | 第 4 级 |
| `fft_1k.v` | 10 | 1024 | 第 5 级 |
| `fft_512.v` | 9 | 512 | 第 6 级 |
| `fft_256.v` | 8 | 256 | 第 7 级 |
| `fft_128.v` | 7 | 128 | 第 8 级 |
| `fft_64.v` | 6 | 64 | 第 9 级 |
| `fft_32.v` | — | 32 | 第 10 级（首个用 `butterfly_general`） |
| `fft_16.v` | — | 16 | 第 11 级（首个用 RAM delay + ROM） |
| `fft_8.v` | — | 8 | 第 12 级（寄存器延时） |
| `fft_4.v` | — | 4 | 第 13 级 |
| `fft_2.v` | — | 2 | **最末级**（直接输出） |

> 说明：`fft_2/4/8/16/32` 这几层实现方式与高层略有不同（有些直接写死 `PERIOD` 常量，没有显式的 `current_layer` 参数），它们是「简单层」。从 `fft_32` 起的模块高度同构，统一用 `current_layer` 参数。这些细节会在第 4 单元逐级精读，本讲只需记住「点数 → 文件名」的映射即可。

#### 4.1.3 源码精读：用 fft_top.v 串起整条流水线

`src` 里最能体现「整体结构」的文件就是 `fft_top.v`。它的端口（对外接口）如下：

[src/fft_top.v:8-20](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L8-L20) —— 这是顶层模块的端口声明：`clk`/`rst`/`start`/`over` 是控制信号，`data_real`/`data_img` 是输入（分实部、虚部两路），`out_real`/`out_img` 是结果输出，`out_first`/`out_last` 标记首尾。注意 `data_config`（配置级数）端口虽然声明了，但后面并未真正使用——这与 `参数和问题.md` 里「点数固定 16384」的现状一致。

进入 `fft_top` 内部，第一段就是流水线的**最前级** `fft_16k`：

[src/fft_top.v:26-37](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L26-L37) —— 例化 `fft_16k`。它的输入直接接顶层端口（`data_real`/`data_img`），输出 `w_out_real_16k`/`w_out_img_16k` 用 `wire` 声明后，喂给下一级 `fft_8k`；同时输出 `start_next`（`w_start_8k`）去触发下一级启动。

这种「上一级 `data_out` 喂下一级 `data_in`，`start_next` 触发下一级 `start`」的连接模式，从 `fft_16k` 一路重复到 `fft_2`。看最后一级就能闭环：

[src/fft_top.v:251-265](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L251-L265) —— 例化最末级 `fft_2`，它的输出 `out_real2`/`out_img2` 直接 `assign` 给顶层输出 `out_real`/`out_img`。至此，外部数据从 `fft_16k` 进、从 `fft_2` 出，走完整条 14 级流水线。

因此，在 `src` 里阅读源码的推荐顺序是：**先读 `fft_top.v` 建立全局**，再挑一级（如 `fft_16k.v`）看它如何组合「四件套」，最后逐个阅读四件套本身。

#### 4.1.4 代码实践：给 src 文件分类

**实践目标**：亲手把 `src` 目录下的 30 个 `.v` 文件按四类归类，建立「文件名 ↔ 职责」的快速映射，为后续精读打下基础。

**操作步骤**：

1. 在仓库根目录执行 `ls src/`，确认文件列表与本讲表格一致。
2. 按下表把每个文件归入对应类别，并补全「作用」一栏（可参考 4.1.2 节）。

| 类别 | 文件 | 大致作用 |
| --- | --- | --- |
| 各级 fft 模块 | `fft_top.v` | 顶层，级联全部 14 级 |
| 各级 fft 模块 | `fft_16k.v` | 16384 点级（流水线最前级，current_layer=14） |
| 各级 fft 模块 | `fft_8k.v` … `fft_2.v` | 8192 ~ 2 点的各级（current_layer=13 …，逐级递减） |
| 基础算子模块 | `butterfly.v` | radix-2 蝶形：做一次加、一次减 |
| 基础算子模块 | `butterfly_general.v` | 参数化蝶形封装（带状态机、延时、握手），供 fft_32 及以上复用 |
| 基础算子模块 | `multiplier.v` | 复数乘法 (a+jb)(c+jd)，调用 Xilinx 乘法器 IP |
| 基础算子模块 | `delay.v` | 基于双口 RAM 的移位延时（SDF 的核心反馈存储） |
| 基础算子模块 | `delay_1k_plus.v` | 面向更大延时的扩展版（default layer=11；当前 HEAD 已定义但未例化，待确认） |
| 旋转因子模块 | `Rotator16.v` | 用 ROM IP 存 16 点旋转因子（实部/虚部两块 ROM） |
| 旋转因子模块 | `RotatorMemory8.v` | 用硬编码常量存 8 点旋转因子 |
| 旋转因子模块 | `Rotator_address.v` | 按 layer 生成旋转因子 ROM 的读地址与 select 信号 |
| 旋转因子模块 | `rom.v` | ROM IP 封装（内部带 addr_ctrl 地址控制器） |
| 数据生成模块 | `data_gen.v` | 参数化测试激励生成器（带 layer 参数，仿真用） |
| 数据生成模块 | `data_gen2.v` | 另一个测试激励生成器（模块名 `FFT_test2`） |
| 混入的测试文件 | `data_gen_tb.v` / `fft_general_tb.v` / `fft_tb.v` / `top_tb.v` | 这 4 个 `_tb` 文件其实放在了 `src` 里（见 4.2 节讨论） |
| 其他 | `dsa.txt` | 空文件，历史遗留 |

**需要观察的现象**：

- 归类后你会发现，`src` 里真正的「设计文件」约有 26 个，另有 4 个 `_tb` 文件「混」了进来。这是一个值得注意的组织特点（见 4.2 节）。
- 各级 `fft_*` 模块数量恰好是 14 个（`fft_2` 到 `fft_16k`），加上 `fft_top` 共 15 个，与「16384 点 = log₂16384 = 14 级」完全对应。

**预期结果**：你得到一张「文件名 → 类别 → 作用」的速查表。今后看到任何 `fft_*` 文件，都能立刻定位它是第几级、点数是多少。

**待本地验证**：`delay_1k_plus.v` 是否在某处被例化——目前 grep 全 `src` 未发现 `delay_1k_plus` 的例化语句，仅有 `delay` 被例化（如 `butterfly_general.v:208`）。请自行用 `grep -rn "delay_1k_plus" src/` 复核。

#### 4.1.5 小练习与答案

**练习 1**：`fft_4k.v` 处理的是多少点的数据？它在流水线中大概排第几级？

**参考答案**：`4k` = 4096 = \(2^{12}\)，对应 `current_layer = 12`。在 14 级流水线中，layer 越大越靠前，所以 `fft_4k` 是第 3 级（仅次于最前级 `fft_16k` 和第二级 `fft_8k`）。

**练习 2**：为什么 `Rotator16.v` 和 `RotatorMemory8.v` 一个用 ROM、一个用「硬编码常量」？

**参考答案**：8 点 FFT 需要的旋转因子极少（只有 1 和 \(-j\) 等），用硬编码常量直接写在代码里更简单；16 点起旋转因子数量变多，更适合用 `.coe` 文件初始化的 ROM 来存。这也解释了为什么 `fft_8` 用 `RotatorMemory8`，而 `fft_16` 起改用 `Rotator16`（ROM）。

---

### 4.2 tb 目录：仿真测试平台

#### 4.2.1 概念说明

`tb`（testbench）目录存放仿真测试平台。`src` 里的模块是「造好的机器」，`tb` 里的文件则是「测试台」：它们负责给机器**喂输入激励、产生时钟和复位、观察输出波形**，本身不会被综合成真实电路。

#### 4.2.2 核心流程：tb 文件的分层验证策略

`tb` 目录按被测对象分了三类测试：

```
tb/
├── fft_2_tb.v / fft_4_tb.v / fft_8_tb.v / fft_16_tb.v   ← 单级小点数层验证
├── fft_general_tb.v / butterfly_general_tb.v            ← 高层通用模块验证
├── fft_top_tb.v                                          ← 全链路（整条流水线）验证
├── multiplier_tb.v / delay_tb.v / rom_tb.v               ← 单个基础算子验证
├── Rotators_tb.v / RotatorMemory8_tb.v                   ← 旋转因子模块验证
└── data_gen_tb.v                                         ← 激励生成器验证
```

这是一种**分级验证策略**：从「单个算子」→「单级层」→「整条流水线」逐层向上验证，定位问题时可以层层缩小范围。

#### 4.2.3 源码精读：注意 src 与 tb 的「混合」现象

值得特别指出的是：本项目的 testbench **没有全部放在 `tb/` 里**。`grep` 显示 `src/` 中也有 4 个 `_tb` 文件——`data_gen_tb.v`、`fft_general_tb.v`、`fft_tb.v`、`top_tb.v`。而 `tb/` 里同样存在 `fft_general_tb.v`、`data_gen_tb.v`、`fft_top_tb.v` 等同名/近似文件。

这意味着 `src` 与 `tb` 之间存在**重复和交叉**：有些测试文件同时出现在两个目录，有些则只在一处。阅读时要注意区分你打开的是哪一个，避免被「同名不同内容」误导。一个稳妥的做法是：以 `tb/fft_top_tb.v` 为整链路验证的入口，以 `src/` 里带 `_tb` 的文件为早期/历史版本看待，必要时用 `diff` 对比两者差异。

#### 4.2.4 代码实践：找到全链路仿真入口

**实践目标**：定位整条流水线的仿真入口，理解它如何驱动 `fft_top`。

**操作步骤**：

1. 打开 [tb/fft_top_tb.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/tb/fft_top_tb.v)，找到 `module fft_top_tb` 的声明。
2. 观察它是否例化了 `fft_top` 和 `data_gen`，以及 `clk`/`rst` 是如何用 `initial` 块产生的。
3. 对照 4.1.3 节里 `fft_top` 的端口，确认 testbench 把哪些信号连了进去。

**需要观察的现象**：testbench 里通常有一段 `initial begin clk=0; forever #5 clk=~clk; end` 之类产生时钟的代码，以及 `rst` 的复位时序。

**预期结果**：你能画出「testbench → data_gen → fft_top」的激励与观测关系图。

**待本地验证**：`tb/fft_top_tb.v` 是否能直接在你的仿真器（如 Vivado/iverilog）中跑通——它依赖 `data_gen` 和 `fft_top`，而后者又依赖厂商 IP（乘法器/RAM/ROM），所以可能需要先准备好 IP 才能仿真。

#### 4.2.5 小练习与答案

**练习 1**：要单独验证「蝶形运算」的正确性，应该读哪个 tb 文件？

**参考答案**：`tb/butterfly_general_tb.v`（验证封装后的参数化蝶形）。如果想看更底层的蝶形单元，则需自行参考它的结构写一个针对 `butterfly.v` 的 testbench（仓库未单独提供 `butterfly_tb.v`）。

**练习 2**：为什么项目需要 `fft_8_tb.v`（单级）和 `fft_top_tb.v`（全链路）两套测试？

**参考答案**：单级测试便于定位「某一层」的时序或数值错误，全链路测试验证各级级联后整条流水线的端到端正确性。两者是「局部」与「整体」的互补关系。

---

### 4.3 matlab 目录：算法黄金参考

#### 4.3.1 概念说明

`matlab` 目录存放 MATLAB 脚本，作用是充当硬件的**「黄金参考」（golden reference）**。硬件 FFT 的输出是否正确？最简单的办法就是拿 MATLAB 算一遍同样的输入，再逐点比对。MATLAB 是「标准答案」，硬件是「待测实现」。

#### 4.3.2 核心流程：matlab 文件的分工

```
matlab/
├── DFT_original.m         ← 按定义直接算 DFT（最慢，但最直观，作为基准）
├── FFT_iterative_DIF.m    ← 迭代版 DIF（频率抽取）FFT，逐级保存中间结果
├── FFT_iterative_DIT.m    ← 迭代版 DIT（时间抽取）FFT
├── fft_official_demo.m    ← 调用 MATLAB 内置 fft 的演示
├── fft_testing.m          ← 测试/比对脚本
├── FFT_figures.m          ← 画图（频谱、镜像对称等）
└── rotators/              ← 存放旋转因子数据的子目录（当前为空）
```

其中 `FFT_iterative_DIF.m` / `FFT_iterative_DIT.m` 是核心：它们的「双层 for 循环」恰好对应硬件的「逐级流水」，并且会把每一级的中间结果保存下来（变量 `X_FFT_middle_result`），方便与硬件**各级输出逐拍比对**——这是比只比对最终结果更强大的验证手段。

#### 4.3.3 源码精读：DFT 定义与迭代 FFT 的对应

`matlab` 脚本背后的数学定义在 `scheme/FFT.md` 里有完整推导，其核心是 DFT 定义：

\[ X(k) = \sum_{n=0}^{N-1} x(n)\, W_{N}^{\,nk}, \quad W_{N} = e^{-j\frac{2\pi}{N}} \]

`DFT_original.m` 就是把这个求和原样翻译成代码（\(O(N^2)\)）；`FFT_iterative_DIF.m` 则用 Cooley-Tukey 分治把它降到 \(O(N\log_2 N)\)。两者对同一输入应给出相同结果，这就是「黄金参考」的可信来源。

> 注意：`matlab/rotators/` 子目录当前为空，原本可能计划存放预计算的旋转因子数据（`.coe` 或 `.mat`），目前未填充。

#### 4.3.4 代码实践：用 MATLAB 生成黄金参考

**实践目标**：跑通一个 MATLAB 脚本，为后续硬件仿真准备「标准答案」。

**操作步骤**：

1. 打开 [matlab/FFT_iterative_DIF.m](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/matlab/FFT_iterative_DIF.m)，阅读它的输入向量 `x` 和逐级循环结构。
2. 在 MATLAB（或 Octave）中运行它，记录最终输出。
3. 用 `DFT_original.m` 对同一 `x` 再算一次，确认两者一致。

**需要观察的现象**：迭代 FFT 与按定义的 DFT 结果应当几乎完全相等（仅有浮点误差）。

**预期结果**：你得到一个可信的「输入 x → 输出 X」对照表，可作为硬件 `fft_top` 仿真输出的判定依据。

**待本地验证**：具体能否运行取决于你的 MATLAB/Octave 环境；脚本里若硬编码了输入向量，可改成你自己的测试数据。

#### 4.3.5 小练习与答案

**练习 1**：为什么要把每一级的中间结果 `X_FFT_middle_result` 也保存下来？

**参考答案**：硬件是逐级流水的，每一级（如 `fft_8`）都有自己的输出。保存中间结果后，可以把硬件**某一级的输出**和 MATLAB **对应那一级**的中间结果逐拍比对，从而精确定位是哪一级出了错，而不是只能看到最终结果的「对/错」。

**练习 2**：`fft_official_demo.m` 调用 MATLAB 内置 `fft`，它和 `FFT_iterative_DIF.m` 的关系是什么？

**参考答案**：内置 `fft` 是 MATLAB 高度优化、绝对可信的实现，可作为 `FFT_iterative_DIF.m`（作者手写）的**二次校验**。三者（定义 DFT、手写迭代 FFT、内置 fft）结果一致，才能确认算法实现无误。

---

### 4.4 scheme 目录：设计文档

#### 4.4.1 概念说明

`scheme` 目录存放**设计文档**：算法推导、参数约定、已知问题、研究背景资料。它不参与综合，但是理解整个项目「为什么这么设计」的钥匙。**拿到一个新项目，先读文档往往比先读代码高效得多。**

#### 4.4.2 核心流程：scheme 文件一览

```
scheme/
├── FFT.md                       ← DFT/Cooley-Tukey/bit-reverse 的算法推导（Markdown）
├── 参数和问题.md                  ← 参数约定 + 已知问题（如倒序未实现、点数固定）
├── real-time-fft.pdf            ← 实时 FFT 的研究资料
├── 基于FPGA的实时FFT分析方法研究.pdf  ← 完整研究论文（项目学术背景）
└── 项目管理规范.jpg              ← 项目管理规范图片
```

此外，根目录还有两类「文档资源」：`README.md`（作者亲写的总说明书）和两个 `.assets` 图片目录（`design-document.assets/`、`fft-design-document.assets/`，存放文档引用的时序图/结构图 PNG）。

#### 4.4.3 源码精读：用文档快速建立全局认知

阅读顺序建议（这也是 README「提示」一节的建议）：

1. **先读 [README.md](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/README.md)**：它列出了顶层接口表、子模块需求表（`butterfly_general` / `butterfly` / `Rotator` / `Delay` / `Multiplier`），以及作者亲授的学习步骤——「看文档 → 推算法 → 画 4/8 点时序 → 跑仿真 → coding debug」。
2. **再读 [scheme/FFT.md](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/scheme/FFT.md)**：把 DFT 到 FFT 的算法推导过一遍，理解旋转因子的对称性/周期性、Cooley-Tukey 分治、bit-reverse 倒序。
3. **读 [scheme/参数和问题.md](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/scheme/参数和问题.md)**：掌握参数约定（如延时量「输出较输入延时 \(2^{n-1}-1\) 个时钟」）和已知缺陷（输出倒序 Reverse 尚未实现）。

特别留意 README 中这段对高层模块组成的描述：

> fft32 之后的模块组成：butterfly_general + Rotator_address 及两个存储旋转因子的 ROM + multiplier。

这句话几乎就是 `src` 里 `fft_32.v` ~ `fft_16k.v` 全部高层模块的**结构公式**。文档与代码高度对应，是本项目文档质量较高的体现。

#### 4.4.4 代码实践：文档 ↔ 代码交叉验证

**实践目标**：验证文档描述与真实代码是否一致，建立「读文档即懂代码结构」的信心。

**操作步骤**：

1. 读 README 里「功能子模块需求」表格，记下五个子模块名：`butterfly_general`、`butterfly`、`Rotator`、`Delay`、`Multiplier`。
2. 在 `src/` 中逐一确认这五个名字对应的真实文件：`butterfly_general.v`、`butterfly.v`、`Rotator*.v`、`delay.v`、`multiplier.v`。
3. 读 `scheme/参数和问题.md`，把里面提到的「已知问题」（如倒序未实现）在 `src/fft_top.v` 里求证——你会发现 `fft_top` 的输出确实没有倒序环节。

**需要观察的现象**：文档里提到的模块名，在 `src` 里都能找到对应文件；文档提到的「未完成项」，代码里确实缺失。

**预期结果**：你建立起「文档 ↔ 代码」的双向索引，今后读到一个设计点，既能查文档找原理，也能查代码找实现。

**待本地验证**：`参数和问题.md` 的具体内容请以本地打开为准（文件名为中文）。

#### 4.4.5 小练习与答案

**练习 1**：`scheme/基于FPGA的实时FFT分析方法研究.pdf` 这类 PDF 在项目里起什么作用？

**参考答案**：它是项目的学术研究背景，提供了 SDF 流水线 FFT 的理论基础、结构选型依据和参考文献。当你想搞清楚「为什么作者选 SDF 而不是别的结构」「延时反馈的原理是什么」时，应该回头读它。日常读代码时不必通读，按需查阅即可。

**练习 2**：如果 README 和实际代码出现不一致（例如 README 说某接口存在，代码里却没有），该以谁为准？

**参考答案**：**以代码为准**。文档可能滞后于代码（README 末尾的接口表里 `config` 标注「条件不成熟」，代码里 `data_config` 也确实未接线，这正是一致的例子；但若发现矛盾，代码才是最终真相）。发现不一致时，应把它当作「文档待更新」的信号记录下来。

---

## 5. 综合实践

把本讲四个目录的知识串起来，完成下面这个「仓库导览」小任务：

**任务**：为 `fpga-fft` 绘制一张**一页纸的仓库导览图**，要求包含：

1. **四个目录的职责**：用一句话标注 `src` / `tb` / `matlab` / `scheme` 各自放什么。
2. **一条数据路径**：画出从「`tb` 里的激励」→「`src/fft_top` 的 `fft_16k` 输入」→ 逐级到「`fft_2` 输出」→「`matlab` 黄金参考比对」的完整链路。
3. **四类文件清单**：把 `src` 下的文件按「各级 fft / 基础算子 / 旋转因子 / 数据生成」四类列出（可直接引用 4.1.4 的表格）。
4. **一个文档锚点**：在图上标注「想懂算法读 `scheme/FFT.md`，想懂结构读 `README.md`」。

完成后，你应该能向一个新同事在 5 分钟内讲清楚「这个仓库怎么读」。

> 提示：你可以用 Markdown 的列表/表格、Mermaid 流程图，或手画后拍照。重点是**分类清晰、链路完整**，而不是画得漂亮。

## 6. 本讲小结

- `src` 是设计源码核心，文件命名遵循规律：`fft_<点数>.v` 是各级流水线层（14 级，\(N = 2^{\text{layer}}\)），`fft_top.v` 是级联顶层；小写功能名是基础算子，大写 `Rotator*` 是旋转因子模块。
- 各级 `fft_*` 模块都由「蝶形 → RAM 延时 → 旋转因子 → 复数乘法」四件套组成，差别只在 `layer` 参数（即点数）。
- `fft_top.v` 用「上一级 `data_out` 喂下一级 `data_in`、`start_next` 触发下一级」的模式，把 `fft_16k` → … → `fft_2` 共 14 级串成一条流水线。
- `tb` 存仿真测试，采用「单算子 → 单级 → 全链路」的分级验证策略；注意部分 `_tb` 文件混在 `src` 里，与 `tb` 存在重复。
- `matlab` 存黄金参考脚本，其中 `FFT_iterative_DIF/DIT.m` 会逐级保存中间结果，便于和硬件各级输出逐拍比对。
- `scheme` 存设计文档，`README.md` + `FFT.md` + `参数和问题.md` 是理解项目「为什么这么设计」的钥匙，文档与代码高度对应。

## 7. 下一步学习建议

本讲让你看清了「仓库长什么样」。接下来建议：

- **若想先夯实算法基础**：学 **u1-l3（算法基础：DFT、Cooley-Tukey 与旋转因子）**，结合 `scheme/FFT.md` 和 `matlab/` 脚本，把 DFT → FFT 的推导走一遍。
- **若想直接进入硬件全貌**：学 **u1-l4（数据流总览：fft_top 顶层模块如何串起整条流水线）**，在 4.1.3 的基础上精读 `fft_top.v` 的级联与握手。
- **后续深挖顺序**：第 2 单元讲核心算子（`butterfly` / `multiplier`），第 3 单元讲存储与时序（`Rotator` / `delay`），第 4 单元逐级精读 `fft_2` ~ `fft_16k`，第 5 单元讲仿真验证与平台移植。

无论选哪条路，本讲建立的「文件名 ↔ 职责」速查表都会是你随时翻阅的基础。
