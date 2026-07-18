# 开发环境搭建与仿真运行

## 1. 本讲目标

上一讲我们认识了 OpenOFDM 是什么。本讲解决一个更紧迫的问题：**怎么把它跑起来**。

读完本讲，你应该能够：

- 安装并验证 Icarus Verilog 工具链（`iverilog` / `vvp` / `gtkwave`）。
- 看懂 `verilog/Makefile` 里的编译与仿真目标，并知道哪些命令是「真实存在」的、哪些只是注释里的设想。
- 理解 `verilog/dot11_modules.list` 作为 iverilog「命令文件」的作用。
- 成功运行 `dot11_tb` 测试台，在 `sim_out/` 下看到各阶段输出文件。
- 用 gtkwave 打开 `dot11.vcd` 波形，定位到 `short_preamble_detected` 跳变的时刻。

本讲是后续所有源码精读的前置条件——只有先能跑通仿真、看懂波形，后面定位信号才有意义。

## 2. 前置知识

- **硬件描述语言（HDL）与仿真**：Verilog 源码本身只是「文本」，要验证它是否正确，需要先「编译」成可执行的仿真模型，再「运行」得到各信号随时间变化的波形。这与编译 C 程序、再运行可执行文件类似，只是这里的「程序」是一组随时钟节拍翻转的信号。
- **波形（waveform）/ VCD**：仿真过程会把每个信号在每个时刻的值记录下来，存成 VCD（Value Change Dump）文件。用 gtkwave 打开它，就能像看示波器一样观察信号跳变。
- **时钟与采样率**：OpenOFDM 的 FPGA 时钟是 100 MHz，而输入基带 I/Q 样本的采样率是 20 MSPS（每秒 2000 万个样本）。两者之比是 \(100/20 = 5 \)，所以硬件每 5 个时钟周期才收到一个新样本。这个「5:1」关系是本讲的一个重要细节。
- **hex 内存文件**：测试台用 `$readmemh` 把一个文本文件按十六进制逐行读进一个数组。OpenOFDM 仓库 `testing_inputs/` 下的 `.txt` 文件就是这种格式，每行是一个 32 位的 I/Q 样本（高 16 位 I、低 16 位 Q）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `verilog/Makefile` | 构建脚本，定义 iverilog/vvp 的调用方式与编译、仿真目标 |
| `verilog/dot11_modules.list` | iverilog 的命令文件（`-c` 参数），列出所有要参与编译的源文件与库搜索路径 |
| `verilog/dot11_tb.v` | 测试台，加载样本、喂时钟、把各阶段信号落盘并生成波形 |
| `requirements.txt` | Python 交叉验证工具链的依赖声明 |
| `Readme.rst` | 顶层说明，写明了依赖工具 |

## 4. 核心概念与源码讲解

### 4.1 Makefile 构建脚本与工具链

#### 4.1.1 概念说明

仿真一条 Verilog 设计需要三件套：

1. **编译器 `iverilog`**（Icarus Verilog）：把 `.v` 源码与测试台一起编译成一个仿真可执行文件（OpenOFDM 里叫 `dot11.out`）。
2. **仿真器 `vvp`**：运行 `dot11.out`，按测试台里写好的时钟与激励推进时间，产生波形与文本输出。
3. **波形查看器 `gtkwave`**：打开 VCD 文件，交互式查看信号。

`Makefile` 就是把这三步封装成 `make xxx` 命令，免得每次手敲一长串参数。

#### 4.1.2 核心流程

```text
make compile  →  iverilog 编译 dot11_tb.v + 全部模块  →  dot11.out
make simulate →  vvp 运行 dot11.out                   →  dot11.vcd + sim_out/*.txt
（看波形）     →  gtkwave dot11.vcd
```

#### 4.1.3 源码精读

`Readme.rst` 明确声明了依赖只有两个工具：

[Readme.rst:15-22](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/Readme.rst#L15-L22) 说明环境依赖是 Icarus Verilog 与 GtkWave，前者负责编译与仿真，后者负责波形可视化。

`Makefile` 顶部用注释列出了「设想中的三条命令」：

[verilog/Makefile:7-11](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/Makefile#L7-L11) 注释里写道：`make check` 用来编译以检查代码，`make simulate` 编译并仿真，`make display` 编译、仿真并显示波形。

> ⚠️ **重要：要区分「注释里写的」和「真实存在的目标」**。继续往下看真实的 target 定义：

[verilog/Makefile:15-20](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/Makefile#L15-L20) 定义了文件清单变量：`SRC` 是所有 `.v`/`.mif`/`.coe` 文件，`MODULE_LIST = dot11_modules.list`，测试台是 `dot11_tb.v`，编译输出是 `dot11.out`，仿真波形输出是 `dot11.vcd`。

[verilog/Makefile:26-32](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/Makefile#L26-L32) 指定三个工具与选项：`COMPILER = iverilog`、`SIMULATOR = vvp`、`VIEWER = gtkwave`；编译标志 `-v -c $(MODULE_LIST) -o $(COMPILER_OUT) -DDEBUG_PRINT`，仿真标志 `-v -n`。

真实可用的 target 在这里：

[verilog/Makefile:35-46](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/Makefile#L35-L46) 定义了 `all`（= compile + simulate）、`compile`、`simulate`，以及两条文件规则：`dot11.out` 由 `iverilog $(COMPILER_FLAGS) dot11_tb.v` 生成，`dot11.vcd` 由 `vvp $(SIMULATOR_FLAGS) dot11.out` 生成。

[verilog/Makefile:48-51](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/Makefile#L48-L51) 定义了 `clean`，删除 `dot11.out` 与 `dot11.vcd`。

把注释和真实 target 对照后，结论很明确：

| 注释里的设想命令 | 真实情况 | 你该怎么做 |
|------------------|----------|------------|
| `make check` | **未定义为 target**，直接敲会报错 | 用 `make compile` 代替（只编译、检查代码） |
| `make simulate` | ✅ 真实 target | 直接用，会自动先编译再运行 vvp |
| `make display` | **未定义为 target** | 用 `make simulate` 后，再手动 `gtkwave dot11.vcd` |

这就是为什么本讲不会让你敲 `make check` 或 `make display`——它们在本仓库里并不能直接工作。

#### 4.1.4 代码实践

1. **实践目标**：安装工具链并验证 `make compile` 能成功生成 `dot11.out`。
2. **操作步骤**（以 Ubuntu/Debian 为例）：
   ```bash
   sudo apt-get install -y iverilog gtkwave
   iverilog -V     # 看到版本号说明安装成功
   cd verilog
   make compile
   ```
3. **需要观察的现象**：`make compile` 会打印一行很长的 `iverilog -v -c dot11_modules.list -o dot11.out -DDEBUG_PRINT dot11_tb.v`，然后报告编译了多少个模块。
4. **预期结果**：`verilog/` 目录下生成 `dot11.out` 文件，且终端无 error。
5. 若系统提示找不到 `iverilog`，说明 Icarus Verilog 未安装成功，需回到第一步排查。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `make simulate` 不需要先手动 `make compile`？

> **答案**：`simulate` target 依赖 `$(COMPILER_OUT)`（即 `dot11.out`），而 `dot11.out` 的生成规则里又依赖测试台与全部源码。make 会自动按依赖关系先触发编译，所以一条 `make simulate` 就够了。

**练习 2**：`make display` 敲下去会发生什么？该如何真正「显示波形」？

> **答案**：因为 `display` 不是真实 target，make 会报 `No rule to make target 'display'`。正确做法是先 `make simulate` 生成 `dot11.vcd`，再手动运行 `gtkwave dot11.vcd`。

---

### 4.2 dot11_modules.list：iverilog 的编译清单

#### 4.2.1 概念说明

OpenOFDM 由几十个 `.v` 文件组成，还依赖 Xilinx 的库与 coregen 生成的 IP。iverilog 需要知道「编译哪些文件、去哪里找库」。把这些信息写成一个**命令文件**（command file），再用 `-c` 传给 iverilog，比在命令行上罗列几十个路径要清爽得多。这个命令文件就是 `dot11_modules.list`。

#### 4.2.2 核心流程

```text
iverilog -c dot11_modules.list ...
            │
            ├─ 读取以 -y 开头的库搜索路径
            ├─ 顺序读入下面列出的每一个 .v 文件
            └─ 顶层 dot11_tb.v（由 Makefile 单独追加）例化 dot11 → 形成完整设计
```

#### 4.2.3 源码精读

[verilog/dot11_modules.list:1-2](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_modules.list#L1-L2) 前两行是 `-y` 开头的库搜索路径，分别指向 Xilinx 的 `unisims`（基本原语）与 `XilinxCoreLib`（IP 行为模型）目录。这些路径是相对仓库根的，仿真时要从 `verilog/` 目录运行。

[verilog/dot11_modules.list:4-31](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_modules.list#L4-L31) 列出手写的 RTL 文件：顶层 `dot11.v`，前端同步 `sync_short.v`/`power_trigger.v`/`sync_long.v`/`stage_mult.v`，复数与统计原语 `complex_to_mag.v`/`complex_mult.v`/`moving_avg.v` 等，以及解码链 `ofdm_decoder.v`/`equalizer.v`/`demodulate.v`/`deinterleave.v`/`descramble.v` 等。

[verilog/dot11_modules.list:33-34](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_modules.list#L33-L34) 是平台依赖：`./usrp2/setting_reg.v`（配置寄存器原语）、`./usrp2/ram_2port.v`（双口 RAM）。

[verilog/dot11_modules.list:36-44](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_modules.list#L36-L44) 是 coregen IP 的**仿真行为模型**：`xfft_v7_1.v`（FFT）、`viterbi_v7_0.v`（卷积解码）、`div_gen_v3_0.v`（除法器）、`complex_multiplier.v`、以及三个查找表 `deinter_lut.v`/`atan_lut.v`/`rot_lut.v`，外加一个 Xilinx 乘法器原语 `MULT18X18S.v`。

> 关键认知：这里引用的是 coregen IP 的 `.v` **行为模型**，所以**即使没有 Xilinx 综合工具链，也能用 iverilog 跑仿真**。综合（上板）才需要 `.ngc` 网表，那是第 6 单元的话题。

#### 4.2.4 代码实践

1. **实践目标**：搞清楚「这个清单里谁是 RTL、谁是 IP 模型、谁是库」。
2. **操作步骤**：打开 `verilog/dot11_modules.list`，按三类给每一行打标签：① 手写 RTL（无路径前缀的 `xxx.v`）② 平台模块（`./usrp2/...`）③ coregen IP（`./coregen/...`）④ Xilinx 库原语（`./Xilinx/...`）。
3. **需要观察的现象**：你会发现 RTL 占绝大多数，IP 只有 7 个。
4. **预期结果**：得到一张「文件 → 类别」对照表，记住 FFT、Viterbi、除法器都被 coregen IP 承担。
5. 若某行对应的文件在仓库里不存在，说明该 IP 的行为模型缺失，仿真会报无法解析模块——本仓库这些 `.v` 都已提供。

#### 4.2.5 小练习与答案

**练习 1**：为什么清单第一行的 `unisims` 路径以 `-y` 标注，而 `dot11.v` 没有？

> **答案**：`-y` 声明的是**库搜索目录**，iverilog 在例化某模块但找不到时，会去这些目录里按文件名找；而直接列出文件名（如 `dot11.v`）则是显式地把该文件作为参与编译的源文件。

**练习 2**：如果仿真报 `Unknown module: xfft_v7_1`，最可能的原因是什么？

> **答案**：最可能是 `dot11_modules.list` 第 36 行的 `./coregen/xfft_v7_1.v` 没被正确加载，或当前工作目录不是 `verilog/`（相对路径解析失败）。

---

### 4.3 dot11_tb 测试台：样本加载与 5:1 采样节拍

#### 4.3.1 概念说明

测试台（testbench）`dot11_tb.v` 不属于硬件设计本身，它是「用来驱动设计的激励 + 观察输出的外壳」。它做四件事：

1. 把一个 `.txt` 样本文件读进内存数组；
2. 产生 100 MHz 时钟；
3. 每 5 个时钟把一个样本喂给 `dot11` 设计（模拟 20 MSPS 采样）；
4. 把设计各阶段的输出信号写进 `sim_out/` 下的文本文件，并 dump 成 `dot11.vcd` 波形。

理解它，你才能解释「样本怎么进去、波形怎么出来」。

#### 4.3.2 核心流程

```text
$readmemh(SAMPLE_FILE, ram)        # 把样本文件读进 ram[]
   │
   ▼
always #5 clock = !clock           # 产生 10ns 周期 = 100MHz 时钟
   │
   ▼  posedge clock
clk_count 从 0 数到 4              # 数满 5 拍
   │  clk_count == 4 时：
   ├─ sample_in_strobe <= 1        #   告诉设计「来样本了」
   ├─ sample_in      <= ram[addr]  #   喂一个样本
   └─ addr <= addr + 1             #   下一个样本
   │
   ▼  各阶段 strobe 触发
$fwrite(... sim_out/*.txt)         # 落盘，供交叉验证
$dumpvars                          # 同时记录 dot11.vcd
```

时钟频率的计算：

\[
T_{\text{clk}} = 2 \times 5\,\text{ns} = 10\,\text{ns} \;\Rightarrow\; f_{\text{clk}} = \frac{1}{10\,\text{ns}} = 100\,\text{MHz}
\]

采样率与时钟的关系：

\[
\frac{f_{\text{clk}}}{f_{\text{sample}}} = \frac{100\,\text{MHz}}{20\,\text{MSPS}} = 5
\]

所以每 5 个时钟来一个样本，这正是 `clk_count == 4`（0、1、2、3、4 共 5 拍）才置 strobe 的原因。

#### 4.3.3 源码精读

[verilog/dot11_tb.v:82-88](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L82-L88) 用宏定义了默认样本与样本数：`SAMPLE_FILE` 默认指向 `../testing_inputs/conducted/dot11a_24mbps_qos_data_..._42.txt`（即上一讲提到的 24Mbps 样本），`NUM_SAMPLE` 默认 3000。这意味着「直接 `make simulate`」用的就是 24Mbps 样本。

[verilog/dot11_tb.v:90-98](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L90-L98) `initial` 块开头 `$dumpfile("dot11.vcd")` 与 `$dumpvars` 负责生成波形；`$readmemh(\`SAMPLE_FILE, ram)` 把样本读进数组 `ram`。

[verilog/dot11_tb.v:107-114](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L107-L114) 通过设置寄存器把 `SR_SKIP_SAMPLE` 写成 0，即「不要跳过样本」。`SR_SKIP_SAMPLE` 的地址定义在 [verilog/common_params.v:19](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v#L19)（地址值 5）。

[verilog/dot11_tb.v:116-133](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L116-L133) 在 `./sim_out/` 下打开一批输出文件（`sample_in.txt`、`power_trigger.txt`、`short_preamble_detected.txt`、`byte_out.txt` 等）。注意：这个 `sim_out/` 目录在仓库里通过一个 `.gitignore` 占位文件保留，所以克隆下来就存在，`$fopen` 才能成功。

[verilog/dot11_tb.v:137-139](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L137-L139) `always #5 clock = !clock` 产生时钟，半周期 5ns。

[verilog/dot11_tb.v:147-156](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L147-L156) 是 5:1 喂样的核心：每个 `posedge clock` 让 `clk_count` 递增，当 `clk_count == 4` 时置 `sample_in_strobe=1`、送出 `ram[addr]`、`addr+1` 并把 `clk_count` 归零；否则保持 `sample_in_strobe=0`。

[verilog/dot11_tb.v:161-164](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L161-L164) 在样本有效且 `power_trigger` 置位时，把时间戳与 `power_trigger`、`short_preamble_detected` 等信号写进对应文件——这些正是你要在波形里观察的关键信号。

[verilog/dot11_tb.v:233-281](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L233-L281) 例化被测设计 `dot11`，把上面的激励（clock/sample_in/配置寄存器）接进输入端口，把各阶段输出接到 `wire` 上供观察。

#### 4.3.4 代码实践（本讲主实践）

1. **实践目标**：用 24Mbps 样本跑通一次完整仿真，并在波形里定位 `short_preamble_detected` 的跳变。
2. **操作步骤**：
   ```bash
   cd verilog
   make compile          # 生成 dot11.out
   make simulate         # 运行 vvp，生成 dot11.vcd 并写满 sim_out/
   ls sim_out/           # 应看到 sample_in.txt、short_preamble_detected.txt、byte_out.txt 等
   gtkwave dot11.vcd     # 打开波形
   ```
   在 gtkwave 里：左侧信号树找到 `dot11_tb`，把 `clock`、`sample_in_strobe`、`short_preamble_detected`、`power_trigger` 拖到右侧信号区；用放大镜缩小到能看到 `short_preamble_detected` 从 0 跳到 1 的那一段。
3. **需要观察的现象**：
   - `clock` 每 10ns 翻转一次（验证 100MHz）。
   - `sample_in_strobe` 每 5 个时钟拉高一个节拍（验证 5:1）。
   - 样本持续输入若干拍后，`power_trigger` 先跳变为 1，再过一段 `short_preamble_detected` 跳变为 1。
4. **预期结果**：`sim_out/short_preamble_detected.txt` 里能找到一行从 `0` 变 `1` 的记录，对应波形上 `short_preamble_detected` 的上升沿；`dot11.vcd` 成功生成且大小非 0。
5. 如果 `sim_out/` 为空或 `dot11.vcd` 没生成，多半是 `make compile` 失败（看终端 error）或当前不在 `verilog/` 目录——**待本地验证**实际样本下的具体时间戳。

#### 4.3.5 小练习与答案

**练习 1**：把 `clk_count == 4` 改成 `clk_count == 9`，采样率会变成多少？设计的 5:1 约定还能满足吗？

> **答案**：那样每 10 个时钟才送一个样本，等效采样率 \(100\,\text{MHz}/10 = 10\,\text{MSPS}\)，与设计要求的 20 MSPS 不符，后续同步/FFT 全部会错位。所以这个 5 是和设计内部参数绑死的，不能随意改。

**练习 2**：仿真什么时候结束？

> **答案**：见 [verilog/dot11_tb.v:180-182](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L180-L182)，当 `addr == NUM_SAMPLE`（默认 3000）时调用 `$finish`。即读完 3000 个样本就停。

---

### 4.4 requirements.txt：Python 依赖与交叉验证

#### 4.4.1 概念说明

OpenOFDM 的主线是 Verilog 硬件解码器，但仓库还带了一套 **Python 浮点参考解码器**，用来与硬件做逐阶段交叉验证（这是第 5 单元的主题）。`requirements.txt` 声明的是这套 Python 工具链的依赖。

需要注意的是：本讲的「跑仿真」**完全不需要 Python**——只要 iverilog 就行。Python 依赖是为后续的交叉验证（`scripts/test.py`）准备的。

#### 4.4.2 核心流程

```text
pip install -r requirements.txt
   ├─ numpy      → 参考解码器做复数/数组运算
   └─ wltrace    → 解析 802.11 帧/pcap 辅助
（test.py 还隐式依赖 scipy，且是 Python 2 语法）
```

#### 4.4.3 源码精读

[requirements.txt:1-2](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/requirements.txt#L1-L2) 固定了两个版本：`numpy==1.11.2` 与 `wltrace==1.1.1`。

两点「读源码才能发现」的诚实提醒：

- `scripts/test.py` 里出现 `print "..."`（无括号）这种 **Python 2 语法**，且 `import scipy`，但 `scipy` 并未写进 `requirements.txt`。也就是说，要跑交叉验证，除了装这两个包，还需要 Python 2 环境与额外的 `scipy`。这是后续（第 5 单元）才会用到的，本讲可暂不处理。
- 因此本讲的实践只依赖 iverilog/gtkwave，**不必**先把 Python 环境弄好。

#### 4.4.4 代码实践

1. **实践目标**：分清「本讲必需」与「后续才需要」的依赖，避免在 Python 环境上卡住。
2. **操作步骤**：阅读 `requirements.txt` 与 `scripts/test.py` 开头的 `import` 语句；列出两个清单——「仿真必需」与「交叉验证必需」。
3. **需要观察的现象**：仿真必需清单里只有 iverilog/gtkwave；交叉验证清单里有 numpy、wltrace、scipy，且需要 Python 2。
4. **预期结果**：你能清楚地告诉同伴「我现在只想看波形，所以只装 iverilog 就够了」。
5. 如果你现在就要尝试交叉验证，需要自行解决 Python 2 + scipy，**版本较旧，待本地验证**是否能在你的系统装上。

#### 4.4.5 小练习与答案

**练习 1**：本讲的实践任务里，哪一步会用到 `requirements.txt` 中的包？

> **答案**：一步都不会。`make compile`/`make simulate`/`gtkwave` 全是 Verilog 工具链，与 Python 无关。`requirements.txt` 是给 `scripts/` 下的交叉验证脚本准备的。

**练习 2**：为什么 `requirements.txt` 里没有 `scipy`，但 `test.py` 又 import 了它？

> **答案**：这更像是项目维护的一个疏漏/历史遗留——`requirements.txt` 没有完整覆盖 `test.py` 的运行时依赖。实际跑交叉验证时必须额外安装 `scipy`。

---

## 5. 综合实践

把本讲内容串起来，完成一次「从样本到波形」的完整体验：

1. 确认 `verilog/sim_out/` 目录存在（仓库已用 `.gitignore` 占位保留）。
2. 在 `verilog/` 下依次执行 `make compile` → `make simulate`。
3. 打开 `sim_out/signal_out.txt`，确认 SIGNAL 字段已解析出来（说明前面同步与解码链路跑通了）；再打开 `sim_out/byte_out.txt`，确认有解码出的字节。
4. 用 `gtkwave dot11.vcd` 打开波形，把 `clock`、`sample_in_strobe`、`power_trigger`、`short_preamble_detected`、`dot11_state` 一起拖进来。沿着时间轴走一遍，亲眼看到状态机如何从等待 `power_trigger`，到检出短前导，再到进入后续解码状态。
5. 写一句话总结：在 100MHz 时钟下，`short_preamble_detected` 大约在第几个**样本**（不是第几个时钟）跳变？把样本序号换算成时钟拍数，验证它确实是 5 的倍数级别的时间尺度。

这个任务把「工具链安装 → 编译 → 仿真 → 看波形 → 用 5:1 关系做 sanity check」全部覆盖，是后续源码精读的实操基础。

## 6. 本讲小结

- OpenOFDM 仿真三件套是 `iverilog`（编译）、`vvp`（运行）、`gtkwave`（看波形），依赖声明在 `Readme.rst`。
- `Makefile` 真实可用的 target 是 `compile`、`simulate`、`all`、`clean`；注释里的 `make check`/`make display` 并未真实实现，要分别用 `make compile` 和手动 `gtkwave` 替代。
- `dot11_modules.list` 是 iverilog 的 `-c` 命令文件，含 Xilinx 库搜索路径、手写 RTL、`usrp2/` 平台模块与 `coregen/` IP 的仿真行为模型——后者让无 Xilinx 工具链也能仿真。
- `dot11_tb.v` 用 `$readmemh` 加载样本、用 `#5` 翻转产生 100MHz 时钟、用 `clk_count==4` 实现 5:1 喂样（对应 20 MSPS 采样），并把各阶段信号落盘到 `sim_out/`。
- 默认样本就是 24Mbps 的 dot11a 样本，`make simulate` 直接就能跑。
- `requirements.txt`（numpy/wltrace）是 Python 交叉验证的依赖，本讲仿真无需安装；注意 `test.py` 还隐式依赖 scipy 且为 Python 2 语法。

## 7. 下一步学习建议

跑通仿真、会看波形之后，建议按数据流进入第 1 单元后续讲义：

- **u1-l3 仓库目录结构全景**：搞清楚 `verilog/`、`scripts/`、`coregen/`、`usrp2/`、`testing_inputs/` 各自的职责，为阅读源码建好导航。
- **u1-l4 顶层模块 dot11.v 的接口与时序约定**：结合本讲看到的 `sample_in`/`byte_out` 端口，系统读懂顶层端口表与 5:1 约定。
- **u1-l5 OFDM 解码流水线总览**：把本讲在波形里看到的 `power_trigger → short_preamble_detected → 解码` 映射到 8 步流水线的具体子模块。

随后第 2 单元将逐个深入 `power_trigger.v`、`sync_short.v` 等前端模块——那时你就能用本讲学会的方法，对这些模块单独加探针、看波形来辅助理解了。
