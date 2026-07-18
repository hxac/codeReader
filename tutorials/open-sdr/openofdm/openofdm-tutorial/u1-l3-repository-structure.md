# 仓库目录结构全景

## 1. 本讲目标

学完本讲，你应当能够：

- 一眼看清 OpenOFDM 仓库根目录下每个目录的职责，并说清它们之间的依赖关系。
- 把仓库里的源码文件分成三类：**手写 RTL**、**Xilinx IP（coregen）**、**USRP 平台代码**，并能解释为什么会有这三类。
- 读懂 `verilog/dot11_modules.list` 这个编译清单，知道 iverilog 在编译时到底按什么顺序、从哪些目录去找文件。
- 说出 `scripts/` 下每个 Python 脚本干什么：生成查找表、转换样本、交叉验证。
- 区分 `testing_inputs/` 里 `conducted`（传导）和 `radiated`（辐射/空口）两类样本的采集方式与覆盖速率。

本讲是「地图课」：不深入任何一个算法模块，而是让你建立一张可以随时回查的全局导航图。后续每一讲都会落到这张图的某个具体目录或文件上。

## 2. 前置知识

在开始前，你需要先具备以下认知（来自前置讲义 u1-l1、u1-l2）：

- **OpenOFDM 是什么**：一个用 Verilog 写、可综合的 802.11 OFDM 物理层接收解码器，只收不发，最终目标是上板到 USRP N210（Xilinx Spartan 3A-DSP FPGA）。
- **它怎么跑**：用 iverilog 编译、vvp 运行、gtkwave 看波形；测试台 `dot11_tb.v` 用 `$readmemh` 把样本喂进 DUT（被测设计）。
- **一个关键约定**：`dot11_modules.list` 是 iverilog 的命令文件（`-c` 参数），`make compile` 时被喂给 iverilog。

如果你对「FPGA 上板」「Xilinx IP 核」「I/Q 采样」这些词完全陌生，不必担心，本讲会用最直白的方式解释。两个最常出现的概念先打底：

- **RTL（Register Transfer Level，寄存器传输级）**：指人手写的 Verilog 设计代码，描述电路逻辑。本讲里「手写 RTL」就是 OpenOFDM 作者自己写的 `.v` 文件，区别于工具自动生成的 IP。
- **IP 核（Intellectual Property core）**：可复用的、通常由厂商工具（这里是 Xilinx ISE 的 CORE Generator，简称 coregen）生成或配置好的电路模块，比如 FFT、除法器、Viterbi 译码器。你不必关心它的内部实现，把它当一个「黑盒」用即可。

## 3. 本讲源码地图

本讲涉及的关键文件和目录如下：

| 文件 / 目录 | 作用 |
| --- | --- |
| `docs/source/overview.rst` | 项目结构与解码流水线的官方说明，是本讲的「权威地图」。 |
| `verilog/dot11_modules.list` | iverilog 编译用的命令文件清单，列出全部要编译的源文件与库搜索路径。 |
| `verilog/` | 全部硬件设计源码，含手写 RTL、coregen IP、usrp2 平台模块、Xilinx 库。 |
| `scripts/` | 全部 Python 脚本：生成 LUT、转换样本、参考解码、交叉验证。 |
| `testing_inputs/conducted/readme.txt` | 传导样本的采集说明（同轴线直连）。 |

## 4. 核心概念与源码讲解

### 4.1 仓库根目录布局与三类源码的区分

#### 4.1.1 概念说明

一个真实的 FPGA 工程很少只有「纯手写代码」。OpenOFDM 的设计目标是上板到 **USRP N210**，而 N210 的主芯片是 **Xilinx Spartan 3A-DSP 3400** FPGA。这带来两个硬性依赖：

1. **厂商依赖**：FFT、Viterbi 译码、除法器这类复杂运算，作者没有自己手写，而是用了 Xilinx 提供的 IP 核（由 CORE Generator 生成），仿真和上板都要依赖 Xilinx 的库。
2. **平台依赖**：OpenOFDM 不是独立芯片，而是要嵌入 USRP 的接收链路。因此它用到了 USRP 代码库里的少数几个底层模块（如配置寄存器、双口 RAM）。

因此，仓库里的硬件源码天然分成三类：

- **手写 RTL**：作者自己写的解码逻辑，是项目的核心。
- **Xilinx IP（coregen）+ Xilinx 库**：工具生成的黑盒与厂商基础库。
- **USRP 平台代码**：与 USRP 硬件打交道的薄薄一层胶水代码。

区分这三类非常重要：仿真时缺一不可，但只有「手写 RTL」是你要逐行读懂的对象；后两类在阅读源码时可以当黑盒。

#### 4.1.2 核心流程

从仓库根目录自顶向下，目录的职责可以这样串起来：

```text
仓库根
├── verilog/          ← 硬件设计（手写 RTL + coregen + usrp2 + Xilinx 库）
├── scripts/          ← Python 工具链（生成 LUT、转换样本、交叉验证）
├── testing_inputs/   ← 样本数据集（conducted / radiated）
├── docs/             ← 文档（Sphinx，.rst 源 + 图片）
├── Readme.rst        ← 项目入口说明
├── requirements.txt  ← Python 依赖
└── LICENSE.txt       ← 许可证
```

数据的「生命周期」横跨这几个目录：

1. 离线阶段：`scripts/gen_*_lut.py` 生成查找表，喂给 `verilog/coregen/` 的 IP 与 `verilog/*.mif` 数据文件。
2. 采集阶段：USRP 采到的二进制 I/Q 存成 `testing_inputs/` 下的 `.dat`，再用 `scripts/bin_to_mem.py` 转成 `.txt`（readmemh 格式）。
3. 仿真阶段：`dot11_tb.v` 用 `$readmemh` 读 `.txt`，驱动 `dot11.v`；结果落盘到 `verilog/sim_out/`。
4. 验证阶段：`scripts/test.py` 用 `scripts/decode.py` 的 Python 参考结果，与 `sim_out/` 逐阶段比对。

#### 4.1.3 源码精读

项目结构的权威说明写在 `overview.rst` 的 **Project Structure** 一节，它明确点出了三个子目录的分工：

[overview.rst:74-84](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/overview.rst#L74-L84) —— 这一段先说明整个 `verilog` 目录最初是为 USRP N210 里的 Spartan 3A-DSP 3400 FPGA 设计的，因此带上了 Xilinx 库与 USRP 代码的依赖；随后用三个 bullet 列出：`verilog/Xilinx` 存 Xilinx 库、`verilog/coregen` 存 coregen 生成的 IP 核、`verilog/usrp2` 存 USRP 专用模块。这正是「三类源码」划分的官方出处。

紧接着它强调：**项目是自包含的（self-contained），用 iverilog（含 `iverilog` 和 `vvp`）即可仿真**。也就是说，虽然设计目标是 Xilinx FPGA，但你不需要装 Xilinx 工具就能跑仿真——因为仓库里已经带了仿真所需的行为模型（详见 4.2）。

文档目录 `docs/` 本身也是结构化的一组 `.rst` 文件，按解码流程分章节：

[overview.rst:74-110](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/overview.rst#L74-L110) —— 这段后半部分描述 `scripts` 与 `testing_inputs` 两个目录（4.3、4.4 会展开）。同时可对照同目录下的 `index.rst`、`detection.rst`、`sync_long.rst`、`freq_offset.rst`、`eq.rst`、`decode.rst`、`sig.rst`、`setting.rst`、`verilog.rst`、`usrp.rst`，它们一一对应后续讲义要讲的解码步骤与平台集成。

> 小贴士：`docs/` 用 Sphinx 构建（见 `docs/source/conf.py`），图片放在 `docs/source/images/`。本教程系列会频繁引用这些 `.rst` 作为算法层面的权威解释。

#### 4.1.4 代码实践

1. **实践目标**：亲手把根目录「摸」一遍，建立三类源码的直观印象。
2. **操作步骤**：
   - 在仓库根执行 `git ls-files | cut -d/ -f1 | sort | uniq -c`，统计每个顶层目录下的文件数量。
   - 再执行 `ls verilog/`，留意哪些是 `.v`、哪些是 `.coe/.mif`、哪些是子目录。
3. **需要观察的现象**：`verilog/` 下既有大量 `.v` 文件，也有 `atan_lut.coe`、`atan_lut.mif`、`deinter_lut.coe`、`deinter_lut.mif`、`rot_lut.coe`、`rot_lut.mif` 这类数据文件，以及 `Xilinx/`、`coregen/`、`usrp2/`、`sim_out/` 四个子目录。
4. **预期结果**：你能指认出 `verilog/*.v`（如 `dot11.v`、`sync_short.v`）属于手写 RTL，`coregen/` 与 `Xilinx/` 属于厂商依赖，`usrp2/` 属于平台胶水代码。
5. **待本地验证**：上述命令的精确文件计数依赖本地工作区状态，请以本机实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么说 OpenOFDM「既依赖 Xilinx、又能脱离 Xilinx 工具仿真」？这两句话矛盾吗？

> **答案**：不矛盾。「依赖 Xilinx」指它的综合/上板目标是 Xilinx FPGA、并使用了 coregen 生成的 IP 核；「能脱离 Xilinx 工具仿真」是因为仓库里带了这些 IP 的仿真行为模型（`.v`）和 Xilinx 仿真库，iverilog 直接拿这些模型就能跑，不需要 ISE/Vivado。

**练习 2**：把下列文件归入「手写 RTL / Xilinx IP / USRP 平台」三类：`dot11.v`、`verilog/coregen/xfft_v7_1.v`、`verilog/usrp2/setting_reg.v`、`verilog/coregen/viterbi_v7_0.v`、`sync_long.v`。

> **答案**：手写 RTL——`dot11.v`、`sync_long.v`；Xilinx IP——`xfft_v7_1.v`（FFT）、`viterbi_v7_0.v`（Viterbi）；USRP 平台——`setting_reg.v`。

### 4.2 dot11_modules.list：编译文件清单是怎么组织的

#### 4.2.1 概念说明

`dot11_modules.list` 是一个**编译清单**。iverilog 支持用命令文件（command file）一次性传入「库搜索路径 + 要编译的源文件」，避免在命令行写一长串参数。`make compile` 会把这个文件作为 `-c` 参数喂给 iverilog（参见前置讲义 u1-l2 对 Makefile 的分析）。

读懂这个清单，你就知道整个设计的「编译边界」在哪里：哪些文件是设计的真正组成，哪些只是厂商库。同时也能立刻看出一个有意思的细节——**测试台 `dot11_tb.v` 并不在清单里**，它由 Makefile 单独编译（因为测试台只在仿真时需要，不属于设计本身）。

#### 4.2.2 核心流程

清单自上而下分四段，逻辑非常清晰：

```text
① Xilinx 库搜索路径（-y，让 iverilog 按模块名自动找库）
② 手写 RTL 文件（设计核心）
③ ./usrp2/ 平台模块
④ ./coregen/ IP 核 + 一个显式的 Xilinx 原语
```

iverilog 读到第①段的两条 `-y` 路径后，遇到代码里实例化但未在清单中直接列出的模块时，会去这两个目录按文件名查找；第②③④段则是显式列出的、必须参与编译的源文件。

#### 4.2.3 源码精读

**第①段——Xilinx 库搜索路径**：

[dot11_modules.list:1-2](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_modules.list#L1-L2) —— 两条 `-y` 分别指向 `verilog/Xilinx/12.2/ISE_DS/ISE/verilog/src/unisims/`（Xilinx 统一原语库，如 `BUFG`、`FDRE` 等基础单元）和 `.../XilinxCoreLib/`（Xilinx 核心库）。这样代码里用到的 Xilinx 基础单元就能被自动找到。

**第②段——手写 RTL**：

[dot11_modules.list:4-31](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_modules.list#L4-L31) —— 从顶层 `dot11.v` 开始，列出全部手写模块，按功能大致分组：先 `dot11.v`，接着是前端检测/同步（`sync_short.v`、`power_trigger.v`、`moving_avg.v`、`delay_sample.v`、`complex_to_mag.v`、`divider.v`、`complex_to_mag_sq.v`），再到 `sync_long.v`、`stage_mult.v`，然后是后端解码链（`ofdm_decoder.v`、`phase.v`、`rotate.v`、`equalizer.v`、`complex_mult.v`、`calc_mean.v`、`deinterleave.v`、`demodulate.v`、`descramble.v`、`bits_to_bytes.v`），最后是若干通用原语与校验（`delayT.v`、`ht_sig_crc.v`、`rate_to_idx.v`、`crc32.v`）。

**第③段——USRP 平台模块**：

[dot11_modules.list:33-34](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_modules.list#L33-L34) —— `./usrp2/setting_reg.v`（配置寄存器原语，host 通过它写参数）和 `./usrp2/ram_2port.v`（双口 RAM，被 deinterleave 等模块复用）。这两个文件来自 USRP 代码库，是平台胶水代码。

**第④段——coregen IP + 显式 Xilinx 原语**：

[dot11_modules.list:36-44](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_modules.list#L36-L44) —— 列出 7 个 coregen IP 的仿真模型：`xfft_v7_1.v`（FFT）、`complex_multiplier.v`（复数乘法器）、`viterbi_v7_0.v`（Viterbi 译码）、`div_gen_v3_0.v`（除法器）、`deinter_lut.v`/`atan_lut.v`/`rot_lut.v`（三张查找表）；最后第 44 行显式拉入 `./Xilinx/12.2/.../unisims/MULT18X18S.v`（18×18 乘法器原语，因为 `complex_mult` 等模块要用到它，单独显式列出以保证被编译）。

> 小贴士：注意清单里引用的都是 coregen IP 的 `.v`（仿真行为模型），而不是 `.ngc`（网表）或 `.xco`（配置）。这正是「无需 Xilinx 工具也能仿真」的原因——iverilog 只认 Verilog 源码。

#### 4.2.4 代码实践

1. **实践目标**：用清单验证「三类源码」的划分，并确认测试台不在清单内。
2. **操作步骤**：
   - 在仓库根执行 `wc -l verilog/dot11_modules.list`，看清单总行数。
   - 执行 `grep -c '\.v$' verilog/dot11_modules.list` 统计显式列出的 `.v` 文件数（应为 26 个左右）。
   - 执行 `grep -n 'dot11_tb' verilog/dot11_modules.list`，确认测试台不在其中（无输出）。
3. **需要观察的现象**：清单里 `coregen/` 与 `usrp2/` 的文件都用相对路径前缀，而手写 RTL 只写文件名。
4. **预期结果**：测试台 `dot11_tb.v` 不在清单里；7 个 coregen IP 全部以 `./coregen/` 前缀出现；2 个 usrp2 模块以 `./usrp2/` 前缀出现。
5. **待本地验证**：具体行数与计数以本机实际输出为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `dot11_tb.v` 不在 `dot11_modules.list` 里？

> **答案**：因为清单定义的是「设计本身」的编译边界（要综合、要上板的部分），而测试台只用于仿真。Makefile 在 `make compile`/`make simulate` 时会把 `dot11_tb.v` 与清单里的设计文件一起编译，但清单本身只列设计模块，保持设计与验证分离。

**练习 2**：清单第 1、2 行的 `-y` 与第 44 行显式写出的 `MULT18X18S.v`，作用有什么不同？

> **答案**：`-y` 设置的是「按模块名自动查找」的搜索目录，iverilog 在实例化处缺定义时才去这里找；第 44 行则是**显式**强制编译 `MULT18X18S.v`，确保这个被多处用到的乘法器原语一定会被纳入编译，避免依赖自动查找时的不确定性。

### 4.3 scripts/：LUT 生成、样本转换与交叉验证

#### 4.3.1 概念说明

`scripts/` 是项目的「软件大脑」，全部用 Python 写成。它干三件事：

1. **离线生成查找表（LUT）**：相位查表、旋转因子表、解交织表，喂给 Verilog 的 coregen IP 与 `.mif` 数据。
2. **样本转换**：把 USRP 采的二进制 I/Q 转成仿真能读的文本格式。
3. **交叉验证**：提供一个**浮点 Python 参考解码器**，再把它的输出和 Verilog 仿真结果逐阶段比对。

为什么要有 Python 参考解码器？因为 Verilog 是定点、流水线的硬件实现，调试时很难直接判断「对不对」。先用一个易读的浮点 Python 版本算出「期望结果」，再让硬件去对齐，这是硬件验证的经典套路。

#### 4.3.2 核心流程

```text
┌─ 离线 LUT 生成 ─────────────────────────────────────┐
│ gen_atan_lut.py / gen_rot_lut.py / gen_deinter_lut.py │
│   → 产出 .mif / .coe，供 verilog/coregen/ 与 verilog/*.mif 使用
└──────────────────────────────────────────────────────┘

┌─ 样本流水线 ────────────────────────────────────────┐
│ bin_to_mem.py：.dat（二进制 int16 I/Q）→ .txt（readmemh 32 位 hex 行）
│ condense.py：去除前后静默段，缩短仿真时间
│   → 产出喂给 dot11_tb.v 的 $readmemh 输入
└──────────────────────────────────────────────────────┘

┌─ 交叉验证 ──────────────────────────────────────────┐
│ decode.py：浮点参考解码器，产出每一步「期望输出」
│ test.py：先跑 decode.py，再跑 vvp 仿真，逐文件比对 sim_out/ 与期望
│ commpy/：被 decode.py 复用的（修改版）信道编码/调制库
└──────────────────────────────────────────────────────┘
```

#### 4.3.3 源码精读

`overview.rst` 用一段话概括了 `scripts/` 的全部职责：

[overview.rst:90-106](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/overview.rst#L90-L106) —— 这一段先逐条列出脚本的分工（生成 LUT、把二进制 I/Q 转成 `$readmemh` 能读的文本、`condense.py` 去除静默段、`test.py` 逐步测试、`decode.py` 做交叉验证的 Python 解码器）；接着点明目录里还带了一份**修改过的 CommPy 库**；最后解释 `test.py` 的完整流程：先用 `decode.py` 解码样本并存下每一步期望输出，再用 `vvp` 跑 Verilog 仿真，把 Verilog 输出与期望输出**逐步比对**。

把这段与目录里的实际文件对照，可以确认 `scripts/` 下正好包含这些文件：`gen_atan_lut.py`、`gen_rot_lut.py`、`gen_deinter_lut.py`、`bin_to_mem.py`、`condense.py`、`decode.py`、`test.py`，以及一个子目录 `commpy/`（修改版 CommPy）。

> 小贴士：这些脚本将在第 5 单元（u5-l1～u5-l5）逐个精读。本讲你只需要知道它们的分工即可。注意前置讲义 u1-l2 提到：`scripts/test.py` 还隐式依赖 `scipy` 且为 Python 2 语法，仿真本身并不需要这些 Python 脚本。

#### 4.3.4 代码实践

1. **实践目标**：把 `scripts/` 下每个脚本对应到它的职责类别。
2. **操作步骤**：
   - 执行 `ls scripts/`，列出全部条目。
   - 对每个 `.py` 文件执行 `head -20 scripts/<文件名>`，阅读其顶部注释（多数脚本有 docstring 说明用途）。
3. **需要观察的现象**：`gen_atan_lut.py` 等三个 `gen_*` 脚本会引用 `SIZE`、`SCALE` 等参数；`decode.py` 顶部会说明它是一个 802.11 解码器；`commpy/` 是一个包目录（含 `__init__.py` 等子模块）。
4. **预期结果**：你能给 7 个 `.py` 文件各写一句职责说明，并把它们归入「LUT 生成 / 样本转换 / 交叉验证」三类。
5. **待本地验证**：脚本顶部是否有 docstring 以本机实际内容为准。

#### 4.3.5 小练习与答案

**练习 1**：如果只想验证 Verilog 解码是否正确，最少需要运行哪个脚本？

> **答案**：`test.py`。它会自动调用 `decode.py` 生成期望输出、调用 `vvp` 跑仿真、再做逐阶段比对，一条龙完成交叉验证。

**练习 2**：为什么 `scripts/` 里要带一份修改过的 `commpy/`，而不是直接 `pip install`？

> **答案**：作者对 CommPy 做了定制修改以匹配 OpenOFDM 的卷积码/调制约定，直接用官方版可能与参考解码器的行为不一致，从而让交叉验证失去意义。把修改版纳入仓库可以保证结果可复现。

### 4.4 testing_inputs/：conducted 与 radiated 样本集

#### 4.4.1 概念说明

`testing_inputs/` 存的是真实采集的 802.11 信号样本，是仿真的「粮食」。采样分两种采集方式：

- **conducted（传导）**：用**同轴电缆**把 USRP 的天线口和被测设备（一台 TP-LINK WDR3500 AP）直接连起来。信号在电缆里走，不受空气干扰，质量最干净，适合做算法验证。
- **radiated（辐射/空口）**：通过天线**隔空**收发，更接近真实使用场景，但带信道噪声与多径。

每个样本通常有三个伴生文件：

- `.dat`：原始二进制 I/Q，每个 I、Q 都是 `int16`。
- `.txt`：经 `bin_to_mem.py` 转成的 `readmemh` 文本（每行一个 32 位 hex，高 16 位 I、低 16 位 Q），这才是 `dot11_tb.v` 真正读的文件。
- `.pcap`：抓包文件（部分样本有），可以对照「期望解出的字节」。

#### 4.4.2 核心流程

样本从采集到喂进仿真：

```text
USRP 采集
   │  产出 .dat（int16 I/Q）
   ▼
bin_to_mem.py
   │  转成 .txt（readmemh hex）
   ▼
（可选）condense.py 裁剪静默段
   │
   ▼
dot11_tb.v 的 $readmemh 加载 .txt → 驱动 dot11.v
```

样本文件名本身就编码了关键信息，命名规则为 `<标准>_<速率>mbps_<...>.<ext>`：`dot11a` 表示 802.11a（legacy），`dot11n` 表示 802.11n（HT）；中间的数字就是速率。

#### 4.4.3 源码精读

**传导样本的采集说明**：

[conducted/readme.txt:1-3](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/testing_inputs/conducted/readme.txt#L1-L3) —— 这三行说明 conducted 样本是「从 USRP N210 在传导设置下 dump 出来的原始 I/Q 样本」，USRP 的天线口和被测设备（TP-LINK WDR3500 AP）用**同轴电缆直连**。注意 `radiated/` 目录下**没有** readme，但它的样本名同样是 `dot11n_*`，结合 `overview.rst` 对「conducted or over the air setup」的描述，可推断 radiated 即空口采集。

`overview.rst` 对样本集覆盖面的说明：

[overview.rst:108-110](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/overview.rst#L108-L110) —— 明确 `testing_inputs` 含 conducted 与 over-the-air（空口）两类样本，覆盖项目支持的全部 legacy 与 HT 速率。

结合实际文件清点（截至当前 HEAD）：

- `conducted/`：legacy `dot11a` 6/9/12/18/24/36/48 Mbps；HT `dot11n` 6.5/7.2/13/19.5/26/39/52/58.5/65 Mbps（即 MCS 0–7，外加一个 7.2 Mbps 的短 GI 样本）。每个样本都有 `.dat` 与 `.txt`，个别带 `.pcap`。
- `radiated/`：仅 HT 样本，6.5/19.5/26/65 Mbps，各带 `.dat`、`.txt`、`.pcap`。

> 小贴士：前置讲义 u1-l2 提到仿真默认用 24Mbps 的 `dot11a` 样本、`NUM_SAMPLE` 默认 3000，它指的就是 `conducted/dot11a_24mbps_*.txt`。

#### 4.4.4 代码实践

1. **实践目标**：清点样本集，确认它覆盖了你关心的速率，并理解三种文件后缀。
2. **操作步骤**：
   - 执行 `ls testing_inputs/conducted/*.dat` 与 `ls testing_inputs/radiated/*.dat`，列出所有二进制样本。
   - 选一个样本，例如 `dot11a_24mbps`，确认它同时有 `.dat` 和 `.txt` 两个文件。
   - 用 `head -3 testing_inputs/conducted/dot11a_24mbps_*.txt` 看前 3 行，确认是 hex 文本。
3. **需要观察的现象**：`.txt` 每行是一个 8 位 hex（32 bit），如 `xxxx xxxx` 形式的整数；`.dat` 是二进制，`head` 会显示乱码。
4. **预期结果**：conducted 同时含 legacy 与 HT 多档速率；radiated 只含少数 HT 速率；`dot11a` 样本名里没有 `54Mbps`（当前样本集未含该档）。
5. **待本地验证**：`.txt` 首行的具体 hex 值以本机实际文件为准。

#### 4.4.5 小练习与答案

**练习 1**：conducted 和 radiated 的根本区别是什么？做算法验证时优先用哪一种？

> **答案**：conducted 用同轴电缆直连，信号干净、可复现；radiated 走空口，带真实信道衰落与干扰。算法验证优先用 conducted，因为它的「正确答案」更确定，便于定位是算法问题还是信道问题。

**练习 2**：`dot11_tb.v` 里 `$readmemh` 读的是 `.dat` 还是 `.txt`？为什么？

> **答案**：读 `.txt`。因为 `$readmemh` 只能读「十六进制文本」，而 `.dat` 是原始二进制 int16，必须先用 `bin_to_mem.py` 转成 `.txt`（每行一个 32 位 hex 字）才能被 `$readmemh` 加载。

## 5. 综合实践

**任务**：遍历仓库目录，亲手制作一张「目录职责表」，并标注仿真必需 vs 综合上板专用。

请按下面的模板完成一张表（可填在笔记里）：

| 目录 | 包含内容（列举关键文件） | 在项目中的角色 | 仿真必需？ | 综合/上板专用？ |
| --- | --- | --- | --- | --- |
| `verilog/`（根） | `dot11.v`、`*.v`、`*.mif`/`*.coe`、`dot11_modules.list`、`dot11_tb.v`、`Makefile` | 手写 RTL 与编译/测试入口 | 是 | 是（不含 tb） |
| `verilog/coregen/` | `xfft_v7_1.v`、`viterbi_v7_0.v`、`div_gen_v3_0.v`、`*_lut.v` 等 + `.xco/.ngc` | Xilinx IP：仿真用 `.v`，上板用 `.ngc` | 是 | 是 |
| `verilog/Xilinx/` | `12.2/.../unisims/`、`XilinxCoreLib/` | Xilinx 仿真库与基础原语 | 是 | 是 |
| `verilog/usrp2/` | `setting_reg.v`、`ram_2port.v` | USRP 平台胶水模块 | 是 | 是 |
| `verilog/sim_out/` | 仿真输出（`.txt`、`.vcd`，git 忽略部分产物） | 存放仿真落盘结果 | 是 | 否 |
| `scripts/` | `gen_*_lut.py`、`bin_to_mem.py`、`condense.py`、`decode.py`、`test.py`、`commpy/` | 离线 LUT 生成、样本转换、交叉验证 | 否（仅验证时） | 否 |
| `testing_inputs/` | `conducted/`、`radiated/` 的 `.dat/.txt/.pcap` | 仿真输入样本 | 是 | 否 |
| `docs/` | `source/*.rst`、`images/`、`conf.py` | 文档 | 否 | 否 |

**完成标准**：

1. 表中每一行的「包含内容」必须是你用 `ls`/`git ls-files` 实际看到的，不能凭记忆编造。
2. 对「仿真必需 vs 上板专用」的判断，给出至少一句理由。例如：`verilog/coregen/` 仿真必需（提供行为模型 `.v`），上板也必需（提供网表 `.ngc`）；`scripts/` 仿真非必需（只有做交叉验证时才用），上板完全不需要。
3. 找出**只在仿真用、综合时不需要**的文件，至少列出 3 个（提示：`dot11_tb.v`、`verilog/sim_out/`、`coregen/*.v` 的行为模型与 `scripts/`）。

> 提示：判断「上板是否需要」的关键是——综合工具吃的是网表/RTL，不吃 Python、不吃测试台、不吃 `.txt` 样本。判断「仿真是否需要」的关键是——iverilog 能不能找到所有模块定义（这正是 `dot11_modules.list` 解决的事）。

## 6. 本讲小结

- 仓库硬件源码天然分三类：**手写 RTL**（作者核心代码）、**Xilinx IP / 库**（`coregen/` + `Xilinx/`）、**USRP 平台代码**（`usrp2/`），源于设计目标上板到 USRP N210 的 Spartan 3A-DSP FPGA。
- `verilog/dot11_modules.list` 是 iverilog 的编译清单，分四段：Xilinx 库搜索路径、手写 RTL、`usrp2/` 平台模块、`coregen/` IP 与显式原语；测试台 `dot11_tb.v` 不在其中。
- 项目自包含：coregen IP 用 `.v` 仿真行为模型，所以**无需 Xilinx 工具即可用 iverilog 仿真**。
- `scripts/` 是 Python 工具链，负责离线生成 LUT、样本格式转换、以及用浮点参考解码器 `decode.py` 做交叉验证。
- `testing_inputs/` 分 `conducted`（同轴直连，干净）和 `radiated`（空口，带信道）两类；仿真实际读的是经 `bin_to_mem.py` 转换的 `.txt`。
- `docs/source/overview.rst` 的 Project Structure 一节是全仓库目录分工的权威说明。

## 7. 下一步学习建议

你已经有了全局地图，下一步应该走到地图的「入口节点」——顶层模块 `dot11.v`：

- 下一讲 **u1-l4「顶层模块 dot11.v 的接口与时序约定」** 会逐组讲解 `dot11.v` 的端口（时钟/复位/使能、配置寄存器、I/Q 输入、字节输出）和 100MHz 时钟与 20MSPS 采样的 5:1 关系。
- 再下一讲 **u1-l5「OFDM 解码流水线总览」** 会把 8 步解码流水线映射到 `dot11.v` 里实例化的各个子模块，建立数据流地图。
- 在进入这两讲前，建议先回到本讲的「目录职责表」复习，确保你能随时定位 `dot11.v`、`dot11_modules.list`、`overview.rst` 这三个文件。
- 如果你更想先动起手来，可以回到前置讲义 u1-l2 跑一次 `make compile && make simulate`，把本讲描述的目录在仿真输出里「对号入座」。
