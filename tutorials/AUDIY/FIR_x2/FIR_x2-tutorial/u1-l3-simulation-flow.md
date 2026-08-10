# 如何运行仿真：Questa 仿真流程与测试激励

## 1. 本讲目标

学完本讲后，你应该能够：

1. 读懂 `FIR_x2_TB.v` 测试激励，说清楚 MCLK/BCK/LRCK 三路时钟是怎样由一个计数器分频得到的。
2. 按照 `FIR_x2.bat` 与 `run.do` 的步骤，在 Questa 中独立完成「建库 → 编译 → 仿真 → 看波形 → 出覆盖率报告」的完整流程。
3. 区分项目中两类数据文件的作用：`.hex`（存储初始化，给 ROM/RAM 原语用）与 `.txt`（测试 PCM 信号，给测试激励逐行读取）。
4. 在波形中找到 `LRCK_I` 与过采样后的 `LRCKx2_O`，并验证二者频率比为 1∶2。

本讲只关心「怎么把仿真跑起来、怎么喂激励、怎么读结果」，不展开滤波器内部算法——那是后续 u2~u5 各讲的主题。

## 2. 前置知识

本讲需要你大致了解以下概念（不熟悉也没关系，下面会结合源码解释）：

- **测试激励（Testbench, TB）**：一段不会被综合成硬件的 Verilog 代码，专门用来给被测设计（Design Under Test, DUT）提供输入信号、观察输出。本项目的 DUT 就是顶层 `FIR_x2`。
- **Questa（原 ModelSim/Questa Sim）**：Siemens 出的 HDL 逻辑仿真器。它用 `vlib` 建工作库、`vlog` 编译、`vsim` 启动仿真。
- **I2S/PCM 时钟三件套**：`MCLK`（主时钟）、`BCK`（位时钟）、`LRCK`（声道/字时钟，其频率等于采样频率 fs）。在 u1-l1 已建立过：本设计的抽头数 = MCLK/fs，例如 `FIR512` 配置下 MCLK/fs = 512。
- **$readmemh / $fscanf**：Verilog 系统任务。`$readmemh` 把十六进制文件整体灌进一个存储数组；`$fscanf` 像 C 语言那样从文件按格式逐行读取。两者分别对应本项目的 `.hex` 与 `.txt` 文件。
- **覆盖率（Coverage）**：衡量仿真到底「跑到了多少代码分支/条件/语句」。本讲只要求看懂报告，深入内容留到 u6-l3。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|------|------|-----------|
| [07_FIR_x2/FIR_x2_TB.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2_TB.v) | 顶层测试激励（DUT 实例化 + 时钟 + 喂数据） | 时钟生成、数据读取、DUT 参数 |
| [07_FIR_x2/Questa/FIR_x2.bat](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/Questa/FIR_x2.bat) | Windows 批处理脚本（自动编译 + 启动仿真） | 编译顺序、`vsim` 选项 |
| [07_FIR_x2/Questa/run.do](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/Questa/run.do) | Questa 命令脚本（波形 + 覆盖率报告） | `add wave`、`run -all`、`coverage report` |
| [07_FIR_x2/Questa/PCM_1kHz_44100fs_32bit.txt](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/Questa/PCM_1kHz_44100fs_32bit.txt) | 测试 PCM 信号（1 kHz 正弦，44.1 kHz 采样，32 bit） | 文本格式、被 `$fscanf` 逐行读取 |
| `07_FIR_x2/Questa/FIR512_x2_48000.hex` 等 | 滤波器系数（十六进制，被 `$readmemh` 灌入 ROM） | 与 `.txt` 的格式差异 |
| `07_FIR_x2/Questa/report.txt` | 仿真后生成的覆盖率报告 | 读懂数字含义 |

> 提示：07 目录下每个核心模块（01~07）都自带一个 `Questa` 子目录和同名 `.bat`/`.do`，意味着**每个模块都能单独仿真验证**。本讲以顶层 07 为例，流程对其它模块完全通用。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：① 测试激励与时钟生成；② 批处理与 do 文件；③ 存储与信号数据文件。

### 4.1 测试激励与时钟生成

#### 4.1.1 概念说明

测试激励（TB）是一个「没有端口」的顶层模块，它的全部职责是：

1. 产生时钟与复位（`MCLK_I`、`BCK_I`、`LRCK_I`、`NRST_I`）；
2. 把测试数据（`DATA_I`）按节拍喂进 DUT；
3. 实例化被测设计 `FIR_x2`，把上面这些信号连到 DUT 端口上。

`FIR_x2_TB.v` 最大的特点是**只用一个自由运行的 `MCLK_I`，再用一个计数器分频出 `BCK` 和 `LRCK`**。这正好对应 u1-l1 讲过的「单时钟域设计」——片内所有逻辑共用 MCLK，BCK/LRCK 只是同步派生信号。

#### 4.1.2 核心流程

整个 TB 的运行过程可以用下面这段伪代码描述：

```
仿真开始
├─ initial: 打开 VCD 文件；打开 PCM_1kHz_..._32bit.txt；仿真跑 400000 ns 后 $finish
├─ always:  每 1 ns 翻转 MCLK_I  ──► 得到周期 2 ns 的主时钟
├─ always:  每个 MCLK 下降沿 MCLK_CNT + 1
├─ assign:  BCK_I  = MCLK_CNT[2]   (MCLK ÷ 8)
│           LRCK_I = MCLK_CNT[8]   (MCLK ÷ 512)
├─ always:  每个 LRCK 上升沿  → $fscanf 从 txt 读一行到 DATAREG
└─ always:  每个 LRCK 下降沿  → 把 DATAREG 打入 PCM_I（复位时清零）
```

时钟分频的数学关系：计数器第 n 位每 \(2^n\) 个计数翻转一次，一个完整周期需要两次翻转，即 \(2^{n+1}\) 个 MCLK 周期。所以

\[
f_{BCK} = \frac{f_{MCLK}}{2^{3}} = \frac{f_{MCLK}}{8}, \qquad
f_{LRCK} = \frac{f_{MCLK}}{2^{9}} = \frac{f_{MCLK}}{512}
\]

由于 `FIR512` 配置下抽头数 = MCLK/fs = 512，而 fs 就是 LRCK 频率，二者刚好吻合——这不是巧合，而是 README「FIR filter length must be equals to (MCLK_I frequency)/(Sampling frequency)」这条约束的直接体现。过采样后的 `LRCKx2` 由 DUT 内部产生，频率应是 LRCK 的 2 倍：

\[
f_{LRCKx2} = 2 \cdot f_{LRCK} = \frac{f_{MCLK}}{256}
\]

> 注意：TB 里的 MCLK「每 1 ns 翻转一次 → 周期 2 ns → 500 MHz」只是仿真时间尺度，不代表真实音频 MCLK。我们关心的是**比值**，不是绝对频率。

#### 4.1.3 源码精读

开头先声明无隐式连线，并设定时间尺度——仿真单位 1 ns、精度 1 ps：

[07_FIR_x2/FIR_x2_TB.v:31-33](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2_TB.v#L31-L33) — 关闭默认 wire 类型、设定时间尺度，确保任何漏声明的连线都会报错。

主时钟自由翻转，得到周期 2 ns 的 `MCLK_I`：

[07_FIR_x2/FIR_x2_TB.v:91-93](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2_TB.v#L91-L93) — `#1 MCLK_I <= ~MCLK_I;`，永远循环，是整个仿真的「心跳」。

用一个 9 位计数器在 MCLK 下降沿自增，再取不同 bit 分频：

[07_FIR_x2/FIR_x2_TB.v:54](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2_TB.v#L54) — `reg [8:0] MCLK_CNT`，9 位刚好够分频出 LRCK（bit 8）。

[07_FIR_x2/FIR_x2_TB.v:101-106](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2_TB.v#L101-L106) — 计数器自增，并用连续赋值把 `MCLK_CNT[2]`、`MCLK_CNT[8]` 直接接成 `BCK_I`、`LRCK_I`。注意 `BCK_I`/`LRCK_I` 声明为 `wire`（见第 40、41 行），由这里的 `assign` 驱动。

数据节拍：在 LRCK 上升沿从文件读一个 32 bit 样点，在下降沿打入 DUT 输入：

[07_FIR_x2/FIR_x2_TB.v:108-114](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2_TB.v#L108-L114) — `posedge LRCK_I` 时 `$fscanf` 读取一个十进制数到 `DATAREG`；`negedge LRCK_I` 时把 `DATAREG` 寄存到 `PCM_I`，复位（`NRST_I==0`）期间强制清零。也就是说**数据是按 LRCK 节拍并行喂入的并行 32 bit 字**，而非串行 I2S 比特流。

DUT 实例化：把 TB 信号连到顶层，并传入关键参数：

[07_FIR_x2/FIR_x2_TB.v:56-70](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2_TB.v#L56-L70) — 实例 `u_FIR_x2`。参数 `DATA_WIDTH=32`、`COEF_WIDTH=16`、`WADDR_WIDTH=8`、`COEF_INIT="FIR512_x2_48000.hex"`。其中 `COEF_INIT` 指定系数 ROM 的初始化文件名（见 4.3 节）。注意 `DATA_I` 接的是 `PCM_I` 而不是 `DATAREG`，多了一拍寄存。

仿真终止条件与文件打开：

[07_FIR_x2/FIR_x2_TB.v:72-89](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2_TB.v#L72-L89) — `$dumpfile/$dumpvars` 输出 VCD 波形；`$fopen` 打开 PCM 测试文件，文件不存在则报错并退出；`#400000 $finish` 表示仿真运行 400 000 ns（400 µs 仿真时间）后结束。第 80 行有一句被注释掉的 `Impulse_44100Hz_32bit.txt`，说明可以切换不同的测试信号。

#### 4.1.4 代码实践

**目标**：通过修改仿真时长，直观感受 400 000 ns 能跑过多少个 LRCK 周期。

**步骤**：

1. 打开 `FIR_x2_TB.v`，找到第 88 行 `#400000 $finish;`。
2. 临时改成 `#100000 $finish;`（缩短到 100 µs），保存。
3. 重新运行仿真（见 4.2.4 节的批处理）。
4. 在波形里数一下 `LRCK_I` 出现了多少个完整周期。

**需要观察的现象**：

- LRCK 周期为 512 个 MCLK 周期 = 512 × 2 ns = 1024 ns。
- 100 000 ns ÷ 1024 ns ≈ 97 个 LRCK 周期；400 000 ns ÷ 1024 ns ≈ 390 个周期。
- 缩短后波形明显变短，`LRCKx2_O` 的周期数约为 `LRCK_I` 的两倍。

**预期结果**：仿真时长与 LRCK 周期数成线性正比；`LRCKx2_O` 的翻转次数约为 `LRCK_I` 的两倍。

> 待本地验证：上述周期数取决于你本地是否真把 MCLK 设成周期 2 ns；若改过 `#1`，比例关系不变但绝对数字会变。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `BCK_I`/`LRCK_I` 声明为 `wire`，而 `MCLK_I` 声明为 `reg`？

**参考答案**：`MCLK_I` 在 `always` 块里用过程赋值翻转，必须是 `reg`；`BCK_I`/`LRCK_I` 由 `assign` 连续赋值表达式驱动，必须是 `wire`。

**练习 2**：如果想把采样频率 fs 改成 MCLK/256（对应 256 抽头滤波器），TB 里的 `LRCK_I` 赋值应改成取 `MCLK_CNT` 的哪一位？

**参考答案**：取第 7 位，`assign LRCK_I = MCLK_CNT[7];`。因为周期为 \(2^{7+1}=256\) 个 MCLK 周期，即 fs = MCLK/256。同时 `MCLK_CNT` 仍是 9 位、够用；DUT 端则要相应换成 `FIR256_x2_48000.hex` 系数。

---

### 4.2 批处理与 do 文件

#### 4.2.1 概念说明

Questa 仿真通常要敲一长串命令。本项目把这些命令固化成两类脚本：

- **`.bat`（Windows 批处理）**：负责**编译 + 启动仿真**。它依次调用 `vlib`（建库）→ `vmap`（映射）→ `vlog`（编译各模块）→ `vsim`（启动仿真并加载 do 文件）。
- **`.do`（Questa 命令脚本）**：负责**仿真启动后做的事**——加波形、跑完、出覆盖率报告。

二者是接力关系：`.bat` 用 `vsim ... -do "do run.do"` 把控制权交给 `run.do`。

#### 4.2.2 核心流程

```
FIR_x2.bat
├─ vlib work            建工作库
├─ vmap work work       把逻辑库名 work 映射到物理目录 work
├─ vlog +cover=bcs ../FIR_x2.v              编译顶层（启用 b分支/c条件/s语句 覆盖）
├─ vlog ../../01_DPRAM_CONT/DPRAM_CONT.v    编译各子模块（控制器/原语/封装）
├─ vlog ... 02 SDPRAM_SINGLECLK / DATA_BUFFER
├─ vlog ... 03 SPROM_CONT
├─ vlog ... 04 SPROM / FIR_COEF
├─ vlog ... 05 MULT
├─ vlog ... 06 ADD
├─ vlog ../FIR_x2_TB.v                      最后编译测试激励
└─ vsim -debugdb=+acc work.FIR_x2_TB -voptargs=+acc -coverage -do "do Run.do"
                                            启动仿真：保留全信号调试、开覆盖率、执行 run.do
```

`run.do` 内部：

```
add log -r *                          记录所有信号（含子模块）到日志
add wave ... MCLK_I BCK_I LRCK_I ...  把 9 个关键信号加进波形窗口
onfinish stop                         遇到 $finish 时停下来（不退出，便于看波形）
run -all                              一直跑到 $finish
coverage report -output report.txt -du=* -assert -directive -cvg -codeAll
                                      生成覆盖率报告
```

编译顺序的小约定：先编译顶层 `FIR_x2.v`，再编译它实例化的各子模块，最后编译 TB。Verilog 在编译期并不强制顺序（模块解析是独立的两阶段），但「自顶向下再 TB」的顺序便于人工核对依赖、也方便在出错时定位。

#### 4.2.3 源码精读

建库 + 映射：

[07_FIR_x2/Questa/FIR_x2.bat:1-3](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/Questa/FIR_x2.bat#L1-L3) — `vlib work` 在当前目录创建物理库文件夹 `work/`；`vmap work work` 把逻辑库名映射上去。这两步在全新环境下只需做一次。

编译列表（注意相对路径，脚本从 `07_FIR_x2/Questa/` 目录执行）：

[07_FIR_x2/Questa/FIR_x2.bat:5-14](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/Questa/FIR_x2.bat#L5-L14) — 顶层 `FIR_x2.v` 带 `+cover=bcs`（开分支/条件/语句覆盖）；子模块用 `../../` 跳回到仓库根再进各编号目录；最后编译 TB。这 10 条 `vlog` 正好覆盖 u1-l2 讲过的 7 个核心 RTL 目录。

启动仿真：

[07_FIR_x2/Questa/FIR_x2.bat:16](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/Questa/FIR_x2.bat#L16) — `-debugdb=+acc` 和 `-voptargs=+acc` 关闭部分优化、保留全部信号以便调试；`-coverage` 打开覆盖率收集；`-do "do Run.do"` 在仿真启动后执行 do 文件。

> ⚠️ 一个值得注意的细节：这行写的是 `do Run.do`（首字母大写 R），而仓库里实际的文件名是 `run.do`（全小写）。在 Windows Questa（文件系统不区分大小写）上能正常工作；但在 Linux/macOS Questa（区分大小写）上会报「找不到 Run.do」。如果你在 Linux 上跑，需要把这一行改成 `do run.do`，或把文件改名。这是移植时容易踩的坑。

do 文件中的波形与停止策略：

[07_FIR_x2/Questa/run.do:1-13](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/Questa/run.do#L1-L13) — `add log -r *` 递归记录所有层次信号；`add wave` 用绝对路径 `sim:/FIR_x2_TB/u_FIR_x2/...` 把 DUT 实例 `u_FIR_x2` 内部的 9 个信号加入波形；`onfinish stop` 保证 TB 执行到 `$finish` 时仿真器不退出，让你有时间看波形、量周期。

运行 + 覆盖率报告：

[07_FIR_x2/Questa/run.do:15-17](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/Questa/run.do#L15-L17) — `run -all` 跑到 `$finish`；`coverage report` 把结果写到 `report.txt`，`-du=*` 表示所有设计单元，`-assert`/`-directive` 收集断言、`-cvg` 收集功能覆盖率、`-codeAll` 收集全部代码覆盖类型。

读覆盖率报告（仿真产物，已随仓库提交一份样例）：

[07_FIR_x2/Questa/report.txt:1-22](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/Questa/report.txt#L1-L22) — `DPRAM_CONT` 有 3 条断言全部命中（100%）；`FIR_x2` 的 Branches 2/2、Conditions 1/1、Statements 7/7 全部 100%。这正是 `+cover=bcs`（b=branch、c=condition、s=statement）对应的三类指标。断言的来源、含义留到 u6-l3 详解。

#### 4.2.4 代码实践

**目标**：在本地 Questa 中跑通顶层仿真，并确认 `report.txt` 被生成。

**步骤**：

1. 打开 Questa，`cd` 到仓库的 `07_FIR_x2/Questa/` 目录（批处理里全是相对路径，必须在这里执行）。
2. 在控制台输入 `do FIR_x2.bat`（或在 Windows 双击它）。
3. 等待编译完成、`vsim` 自动启动并执行 `run.do`，波形窗口出现 9 路信号。
4. 仿真因 `$finish` 触发 `onfinish stop` 而暂停；用波形工具测量 `LRCK_I` 与 `LRCKx2_O` 的周期。
5. 用文本编辑器打开生成的 `report.txt`，核对断言与分支覆盖率。

**需要观察的现象**：

- 波形里 `MCLK_I` 周期 2 ns；`BCK_I` 周期 8 个 MCLK（16 ns）；`LRCK_I` 周期 512 个 MCLK（1024 ns）。
- `LRCKx2_O` 周期约 512 ns，正好是 `LRCK_I` 的一半，即频率为 2 倍。
- `report.txt` 中 `FIR_x2` 的 Statements 为 7/7 = 100%。

**预期结果**：`LRCKx2_O` 频率约为 `LRCK_I` 的 2 倍；覆盖率报告显示 100%。

> 待本地验证：`report.txt` 的具体数字以你本地这次仿真为准；仓库里这份是作者提交时的一次样例。

#### 4.2.5 小练习与答案

**练习 1**：为什么顶层 `FIR_x2.v` 编译时加了 `+cover=bcs`，而各子模块没有？

**参考答案**：`+cover=bcs` 指定在本次仿真里收集该单元的 branch/condition/statement 覆盖。顶层 `FIR_x2` 含饱和判断等关键分支（u5-l3 会讲），是验证重点；子模块也可以加，但这里作者只在顶层与 DPRAM_CONT 的断言上集中收集（report 里也只列了这两个 DU）。加上会略增编译/仿真开销。

**练习 2**：把 `-do "do Run.do"` 改成 `-do run.do` 在 Linux 上是否能运行？为什么？

**参考答案**：改成 `-do run.do` 能运行。`-do` 接受一条命令字符串，`do run.do` 里的 `do` 是 Questa 执行脚本的命令；`run.do` 必须与磁盘文件名大小写一致，Linux 区分大小写，所以小写才对。

---

### 4.3 存储与信号数据文件

#### 4.3.1 概念说明

本项目里有两类「数据文件」，长得都像一行一个数，但用途完全不同，初学者很容易混淆：

| 类型 | 扩展名 | 内容格式 | 谁来读 | 作用 |
|------|--------|----------|--------|------|
| 存储初始化文件 | `.hex` | 十六进制（补码） | 原语里的 `$readmemh` | 上电时灌入 ROM/RAM，决定**滤波器系数**或**RAM 初值** |
| 测试信号文件 | `.txt` | 十进制（有符号） | TB 里的 `$fscanf` | 仿真时逐行喂入 **PCM 输入样点** |

一句话区分：`.hex` 决定「滤波器长什么样」（系数），`.txt` 决定「这次仿真喂什么声音」（输入信号）。

#### 4.3.2 核心流程

**系数文件 `.hex` 的流动路径**（以 `FIR512_x2_48000.hex` 为例）：

```
FIR_x2_TB.v  参数 COEF_INIT = "FIR512_x2_48000.hex"
   └─► FIR_x2.v          把 COEF_INIT 传给 FIR_COEF.RAM_INIT_FILE
        └─► FIR_COEF.v    把 RAM_INIT_FILE 传给 SPROM.ROM_INIT_FILE
             └─► SPROM.v  $readmemh(ROM_INIT_FILE, ROM)   ← 真正读文件的地方
```

类似地，数据 RAM 也有初始化：`BUFF_INIT` 参数 → `DATA_BUFFER` → `SDPRAM` → `$readmemh(RAM_INIT_FILE, RAM)`，对应全 0 的 `BUFFER_INIT.hex`。

**测试信号 `.txt` 的流动路径**：

```
FIR_x2_TB.v  $fopen("./PCM_1kHz_44100fs_32bit.txt")
   └─► 每 posedge LRCK_I:  $fscanf(fp, "%d\n", DATAREG)   逐行读一个十进制样点
        └─► 每 negedge LRCK_I: PCM_I <= DATAREG           打入 DUT 输入
```

`PCM_1kHz_44100fs_32bit.txt` 的命名规则：1 kHz 正弦波、44.1 kHz 采样、32 bit 位宽；文件共 44 099 行，每行一个有符号十进制数（如 `2147470025`、`-890198924`），范围正好是 32 bit 有符号数的上下限。

#### 4.3.3 源码精读

TB 里指定系数文件名（作为参数传入 DUT）：

[07_FIR_x2/FIR_x2_TB.v:60](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2_TB.v#L60) — `.COEF_INIT("FIR512_x2_48000.hex")`。这个字符串会沿着 4.3.2 的路径一直传到 `SPROM.v` 的 `$readmemh`。

真正读取 `.hex` 的地方（在原语里，不在 TB）：

[04_FIR_COEF/SPROM.v:66](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/SPROM.v#L66) — `$readmemh(ROM_INIT_FILE, ROM);`，把整份十六进制系数灌进 ROM 数组 `ROM`。

[02_DATA_BUFFER/SDPRAM_SINGLECLK.v:75-76](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/SDPRAM_SINGLECLK.v#L75-L76) — 只有当 `RAM_INIT_FILE != ""` 时才 `$readmemh`，因此 RAM 初始化是可选的；本顶层默认用全 0 的 `BUFFER_INIT.hex`。

TB 里打开并逐行读 `.txt` 测试信号：

[07_FIR_x2/FIR_x2_TB.v:80-86](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2_TB.v#L80-L86) — `$fopen("./PCM_1kHz_44100fs_32bit.txt", "r")`，打不开就报错退出。第 80 行注释掉的是另一个信号 `Impulse_44100Hz_32bit.txt`（冲激响应测试），可按需切换。

测试信号文件本身的样子：

[07_FIR_x2/Questa/PCM_1kHz_44100fs_32bit.txt:1-10](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/Questa/PCM_1kHz_44100fs_32bit.txt#L1-L10) — 每行一个有符号十进制 32 bit 数；首行 `0`，随后是 1 kHz 正弦的样点序列（先上升到 `2147470025` 接近正满量程，再下降到负值）。`$fscanf(fp, "%d\n", DATAREG)` 正好按这个格式逐行解析。

#### 4.3.4 代码实践

**目标**：通过切换测试信号文件，观察 `DATA_I` 的不同输入，理解 `.txt` 与 `.hex` 各自独立的作用。

**步骤**：

1. 打开 `FIR_x2_TB.v`，定位第 80–81 行。
2. 注释掉第 81 行（PCM 正弦），取消第 80 行注释（改用冲激信号 `Impulse_44100Hz_32bit.txt`），保存。
3. 重新跑仿真，观察波形里 `DATA_I` 的形状：PCM 正弦是连续起伏的正弦波；冲激信号只在开头有一个尖峰、之后接近 0。
4. 再把 `COEF_INIT` 参数（第 60 行）从 `FIR512_x2_48000.hex` 换成 `FIR128_x2_48000.hex`（仓库里提供了 128/256/512 三种系数），重新仿真。

**需要观察的现象**：

- 换 `.txt` 只影响 `DATA_I`（输入），`LRCKx2_O/DATA_O` 的节拍不变，但输出波形形状随输入变化。
- 换 `.hex` 系数会改变滤波器抽头数与频率响应：`FIR128` 抽头少，过渡带更宽；`FIR512` 抽头多，选择性更好（同时 WADDR_WIDTH 等参数也可能需要配套调整，见 u2-l1）。

**预期结果**：`.txt` 决定输入信号形状，`.hex` 决定滤波特性，二者互不影响——验证了 4.3.1 的区分。

> 待本地验证：`FIR128/256/512` 切换后能否直接仿真通过，取决于顶层 `WADDR_WIDTH`/`RADDR_WIDTH` 等派生参数是否需要同步修改，这部分在 u2-l1、u6-l1 详述，本讲只观察输入侧现象。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `FIR512_x2_48000.hex` 这个文件从 `Questa/` 目录删掉，仿真会怎样？

**参考答案**：编译仍能通过（文件名只是字符串参数），但仿真启动后 `SPROM.v` 的 `$readmemh` 会因找不到文件而报 Warning，ROM 内容为默认值（通常是 `x`），`DATA_O` 会输出大量未知态 `x`。所以 `.hex` 必须和 `.bat`/`.do` 放在同一目录（或用正确相对路径）。

**练习 2**：为什么 `.hex` 用十六进制而 `.txt` 用十进制？

**参考答案**：因为读取它们的系统任务不同。`$readmemh`（h=hex）只认十六进制，适合直接灌存储器；`$fscanf("%d", ...)` 用格式化字符串读十进制，便于人直接阅读和用 MATLAB/Python 生成。两者是 Verilog 对存储初始化与文件 I/O 的两套不同机制。

## 5. 综合实践

把本讲三个模块串起来，完成一次「端到端」的顶层仿真验收：

1. **准备**：确认 `07_FIR_x2/Questa/` 下同时存在 `FIR_x2.bat`、`run.do`、`FIR512_x2_48000.hex`、`PCM_1kHz_44100fs_32bit.txt` 四个文件（缺一不可，分别对应编译/波形/系数/输入）。
2. **编译运行**：在该目录执行 `do FIR_x2.bat`（Linux 上先把 `run.do` 那处大小写改对）。
3. **验证时钟分频**：在波形里测量 `MCLK_I`、`BCK_I`、`LRCK_I` 的周期，确认三者比例为 1∶4∶256（周期），即频率 1∶(1/8)∶(1/512)。
4. **验证过采样**：测量 `LRCKx2_O` 周期，确认它 ≈ `LRCK_I` 周期的一半，从而 `f_LRCKx2 ≈ 2 × f_LRCK`，这正是「2 倍过采样」最直观的证据。
5. **看覆盖率**：打开 `report.txt`，确认 `FIR_x2` 的 Branches/Conditions/Statements 与 `DPRAM_CONT` 的 Assertions 均为 100%。
6. **换输入再跑一次**：把测试信号换成 `Impulse_44100Hz_32bit.txt`，观察 `DATA_O` 在冲激输入下的响应（这就是滤波器的冲激响应≈系数本身），加深「`.txt` 喂输入、`.hex` 定系数」的理解。

完成第 4 步即达成本讲核心目标。

## 6. 本讲小结

- `FIR_x2_TB.v` 用一个自由运行的 `MCLK_I` + 9 位计数器分频出 `BCK_I`（÷8）和 `LRCK_I`（÷512），并按 LRCK 节拍用 `$fscanf` 把 `.txt` 里的并行 PCM 样点喂入 DUT。
- `.bat` 负责「建库→编译→`vsim`」，`.do` 负责「加波形→`run -all`→`coverage report`」，二者通过 `vsim -do "do run.do"` 接力。
- 编译列表的 10 条 `vlog` 正好覆盖 7 个核心 RTL 目录；顶层带 `+cover=bcs` 收集分支/条件/语句覆盖。
- `.hex` 是给 `$readmemh` 灌 ROM/RAM 的存储初始化文件（决定系数），`.txt` 是给 `$fscanf` 逐行读的测试信号（决定输入），二者不可混淆。
- `report.txt` 是仿真产物：`FIR_x2` 三类代码覆盖与 `DPRAM_CONT` 三条断言在本样例中均为 100%。
- 移植注意：`FIR_x2.bat` 写的是 `do Run.do`（大写 R），在区分大小写的系统上需改成 `do run.do`；Vivado 下还需把 `.hex` 换成 `.data`（见 README Notes）。

## 7. 下一步学习建议

- 下一讲 **u2-l1（顶层 FIR_x2 模块：端口、参数与实例化图谱）** 会打开 `FIR_x2.v`，讲清 `DATA_WIDTH/COEF_WIDTH/WADDR_WIDTH/COEF_INIT/DATAO_WIDTH` 这几个参数如何派生出内部位宽，以及四个子模块如何连线——你已经在本讲见过它们的实例化，正好顺势深入内部。
- 若想先理解时钟内部如何产生 `LRCKx2/BCKx2`，可跳读 **u2-l2（音频时钟模型）** 与 **u4-l2（SPROM_CONT 过采样时钟生成）**。
- 想深入覆盖率与断言（`report.txt` 里那 3 条断言的来源），直接看 **u6-l3（PSL 断言与覆盖率报告）**。
