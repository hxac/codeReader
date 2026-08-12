# stdcells 晶体管级与 xilibs 仿真模型

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 OH! 库的四种「实现底座」之间的层次关系：`stdcells`（晶体管/开关级）→ `asiclib`（标准单元硬核）→ `stdlib`（RTL 软核）→ `xilibs`（FPGA 厂商原语），并理解本讲处在最底层。
- 读懂用 SystemVerilog（`.sv`）写成的开关级单元 `oh_nmos`/`oh_pmos`/`oh_nand2`，能用 CMOS 上拉/下拉网络解释与非门的导通逻辑。
- 理解 `xilibs` 里 IBUF/IDDR/ODDR/MMCME2_ADV 等「厂商原语行为模型」在 FPGA 仿真流程中的作用——它们让综合时才存在的黑盒在 iverilog 里「现形」。
- 掌握 iverilog 的 `-y` 库搜索与 `+incdir+` 头文件搜索机制，知道编译器如何自动找到分散在各目录里的模块定义。

## 2. 前置知识

本讲是第 9 单元（ASIC 实现、物理设计与工程规范）的第三讲，承接 [u9-l1 双实现策略：soft vs hard](u9-l1-soft-hard-duality.md)。那里讲过：同一功能在 `stdlib`（soft，可综合 RTL）与 `asiclib`（hard，绑定 PDK 的标准单元黄金模型）里各有一套实现。本讲往**更底层**走两步：

- **比标准单元更低**：标准单元（如 `asic_nand2`）本身是用晶体管搭出来的。`stdcells` 就把这一层揭开，用 Verilog 的开关级原语 `nmos`/`pmos` 直接描述 MOS 管的导通与关断，用于教学和原理理解。
- **换一种实现底座**：除了走 ASIC（标准单元），OH! 的链路（elink/mio）也能跑在 Xilinx FPGA 上。FPGA 综合时，工具会把你写的 `MMCME2_ADV`、`IDDR` 这类名字映射成芯片里真实存在的硬原语；但在 iverilog 仿真时，这些名字是「未定义模块」。`xilibs` 就是为它们提供的行为级替身。

需要的两个背景概念：

- **MOS 管（开关模型）**：把一个 MOSFET 想象成一个受栅极（gate）电压控制的开关。NMOS 在栅极为高电平时导通（连通源/漏），PMOS 在栅极为低电平时导通。这是 CMOS 逻辑的物理基础。
- **黑盒（black box）**：一段 RTL 里实例化了一个模块，却没有给出它的定义——对仿真器而言就是黑盒。综合工具往往「认识」某些黑盒（厂商原语），但仿真器不认识，需要额外喂行为模型。

> 提醒：本讲多处会发现「文档/脚本与实际目录漂移」的老问题（与 u9-l1、u9-l2 一致）。结论一律以源码文本为准，能跑通的部分标注「待本地验证」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `stdcells/hdl/oh_nmos.sv` | 用 SystemVerilog 包装 Verilog 内置 `nmos` 开关原语，带 W/L/M/NF 工艺参数（仅调试打印） |
| `stdcells/hdl/oh_pmos.sv` | 同上，包装 `pmos` 开关原语 |
| `stdcells/hdl/oh_nand2.sv` | 2 输入与非门，含两套实现：`SIM=="rtl"` 走 `assign`、否则走晶体管网络 |
| `stdcells/dv/oh_nand2_tb.sv` | 与非门开关级 testbench，用 `supply0/supply1` 当电源，遍历四种输入 |
| `stdcells/dv/run.sh` | 仿真脚本：用 `sv2v` 把 `.sv` 转 `.v`，再用 iverilog 编译 |
| `xilibs/README.md` | 一句话说明 xilibs 是「可仿真、可综合的 Xilinx 原语」 |
| `xilibs/dv/IDDR.v` | Xilinx IDDR（双沿输入寄存器）的行为模型 |
| `xilibs/dv/IBUFDS.v` | 差分输入缓冲器行为模型，`O = I & ~IB` |
| `xilibs/dv/MMCME2_ADV.v` | MMCM 时钟管理器行为模型（含自述缺陷） |
| `xilibs/dv/ODDR.v` | Xilinx ODDR（双沿输出寄存器）行为模型 |
| `stdlib/testbench/libs.cmd` | iverilog 库搜索配置，演示 `-y`/`+incdir+` 机制 |
| `scripts/build.sh` | 顶层编译脚本，调用 `libs.cmd` |
| `elink/hdl/etx_clocks.v` | elink 真实例化 `MMCME2_ADV` 的地方，演示「厂商原语黑盒」 |

## 4. 核心概念与源码讲解

### 4.1 晶体管级建模：从标准单元向下到开关

#### 4.1.1 概念说明

`asiclib` 里的标准单元（如 `asic_nand2`）是绑定某个工艺库（PDK）的「黄金模型」——它告诉你逻辑功能，但不告诉你晶体管怎么连。再往下走一层，就是**晶体管级（switch level）**：直接用 NMOS/PMOS 开关画出电路。

Verilog（含 2005 标准）内置了一组**开关级原语（switch primitives）**：`nmos`、`pmos`、`cmos`、`rnmos`、`rpmos` 等。它们不是用 `always` 或 `assign` 写的行为，而是仿真器内建的「理想开关」模型：

- `nmos n (d, s, g)` 表示一个 NMOS：栅极 `g` 为 1 时，源 `s` 与漏 `d` 导通；为 0 时呈高阻。
- `pmos p (d, s, g)` 表示一个 PMOS：栅极 `g` 为 0 时导通。

`stdcells` 就是这一层的薄封装，价值在于**教学**：让你看见一个与非门到底是怎么用 4 个晶体管搭出来的。它不参与正式的综合流片（那是 asiclib 标准单元的活），也不在 RTL 设计里被例化（那是 stdlib 的活）。

#### 4.1.2 核心流程

一个静态 CMOS 门由两个互补的晶体管网络组成：

- **下拉网络（PDN，NMOS）**：决定输出何时被拉到 `vss`（逻辑 0）。NMOS 串联实现「与」（都要导通才通路），并联实现「或」。
- **上拉网络（PUN，PMOS）**：决定输出何时被拉到 `vdd`（逻辑 1）。PMOS 网络与 NMOS 网络对偶（串联↔并联）。

对 2 输入与非门，功能为 \( z = \overline{a \cdot b} \)。其晶体管拓扑是教科书标准结构：

- 下拉：2 个 NMOS **串联**（`a` 与 `b` 同时为 1 才把 `z` 拉低）。
- 上拉：2 个 PMOS **并联**（`a` 或 `b` 任一为 0 就把 `z` 拉高）。

真值表：

| a | b | 下拉(PDN) | 上拉(PUN) | z |
|---|---|-----------|-----------|---|
| 0 | 0 | 断 | 通（两管都通）| 1 |
| 0 | 1 | 断 | 通（a 路 PMOS 通）| 1 |
| 1 | 0 | 断 | 通（b 路 PMOS 通）| 1 |
| 1 | 1 | 通（两 NMOS 串联导通）| 断 | 0 |

#### 4.1.3 源码精读

先看开关原语的最薄封装 `oh_nmos`：

[stdcells/hdl/oh_nmos.sv:2-16](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdcells/hdl/oh_nmos.sv#L2-L16) —— 模块端口 `bulk/g/s`（衬底/栅/源）与 `inout d`（漏，双向），核心就一行 `nmos n (d, s, g);` 实例化 Verilog 内建开关。`MODEL/W/L/M/NF` 都是工艺参数（沟道宽 W、长 L、并联数 M、手指数 NF），只在 `` `ifdef OH_DEBUG `` 下用 `$display` 打印调试，**不参与开关行为**——也就是说仿真层面它们是「占位」，真正流片时才由设计者填进网表。

`oh_pmos` 完全对称：

[stdcells/hdl/oh_pmos.sv:2-16](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdcells/hdl/oh_pmos.sv#L2-L16) —— 注释 `//out,in,ctrlr` 点明了开关原语的三端顺序：`pmos p (d, s, g)` 即输出 d、输入 s、控制 g，PMOS 在 g=0 时导通。

再看把它们组装成与非门的 `oh_nand2`：

[stdcells/hdl/oh_nand2.sv:7-23](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdcells/hdl/oh_nand2.sv#L7-L23) —— 端口除了 `a/b/z`，还有电源 `vdd/vss`（晶体管级必须有电源网络）。参数 `SIM` 用来切换两种实现；`W/L/M/NF` 是**数组参数**（每个晶体管一组尺寸），这是 SystemVerilog 语法。

[stdcells/hdl/oh_nand2.sv:25-29](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdcells/hdl/oh_nand2.sv#L25-L29) —— `SIM=="rtl"` 分支：退回普通 RTL，`assign z = ~(a & b);`，不需要电源、不实例化晶体管。这呼应了 u9-l1 的「双实现切换」范式——同一模块用字符串参数 + `generate if` 在两套实现间切换。

[stdcells/hdl/oh_nand2.sv:30-46](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdcells/hdl/oh_nand2.sv#L30-L46) —— 晶体管网络分支，即 4.1.2 描述的 CMOS 结构：
- `m0`（NMOS，g=a，s=vss，d=inet）与 `m1`（NMOS，g=b，s=inet，d=z）：**串联**的下拉链 `z → m1 → inet → m0 → vss`。
- `m2`（PMOS，g=a，s=vdd，d=z）与 `m3`（PMOS，g=b，s=vdd，d=z）：**并联**的上拉，任一栅极为 0 即把 z 顶到 vdd。
- 注意 NMOS 的 `bulk` 接 `vss`、PMOS 的 `bulk` 接 `vdd`——这是 MOS 管衬底的正确偏置（NMOS 衬底接最低电位、PMOS 衬底接最高电位）。

#### 4.1.4 代码实践

**目标**：亲眼看到开关级与非门的四种输入下输出符合 \( z=\overline{a\cdot b} \)。

**操作步骤**：

1. 打开 `stdcells/hdl/oh_nand2.sv`，对照 4.1.3 在纸上标出 `m0–m3` 各自的栅/源/漏/衬底。
2. 阅读 testbench：

   [stdcells/dv/oh_nand2_tb.sv:17-41](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdcells/dv/oh_nand2_tb.sv#L17-L41) —— 激励每 100ns 切换一次 `(a,b)`：`00→01→10→11`；DUT 用 `.SIM("switch")` 强制走晶体管分支；顶部 `` `define OH_DEBUG `` 会触发 `oh_nmos/oh_pmos` 里的 `$display` 打印实例路径与尺寸。

3. 尝试运行 `stdcells/dv/run.sh`（详见 4.2.4 的修正说明）。

**需要观察的现象**：`z` 在前三种输入下为 1，仅在 `a=b=1` 时被下拉到 0；OH_DEBUG 打印出每个晶体管的实例层级。

**预期结果**：`z` 序列为 `1,1,1,0`，即与非逻辑。

**待本地验证**：`run.sh` 依赖外部工具 `sv2v`，且脚本里有一处历史路径错误（见 4.2.4），需先修正才能跑通。若环境无 `sv2v`，可改用 `iverilog -g2012` 直接编译 `.sv`（iverilog 2012 支持 SystemVerilog 子集）。

#### 4.1.5 小练习与答案

**练习 1**：若把 `oh_nand2` 的两个 NMOS 改成**并联**、两个 PMOS 改成**串联**，得到的是什么门？

**参考答案**：或非门（NOR），\( z=\overline{a+b} \)。因为下拉并联→任一为 1 即拉低（或），上拉串联→两个都为 0 才拉高。仓库里的 `oh_nor2_tb.sv` 正是对应的 NOR 测试。

**练习 2**：为什么 `oh_nmos`/`oh_pmos` 的漏端 `d` 声明为 `inout` 而不是 `output`？

**参考答案**：理想开关是双向导通的，源和漏哪个当输入、哪个当输出取决于外电路怎么接（在上拉网络里漏朝 vdd、在下拉网络里漏朝 vss）。声明为 `inout` 才能让同一个开关单元在不同网络方向下正确仿真。

---

### 4.2 SystemVerilog 与开关级仿真流程

#### 4.2.1 概念说明

OH! 全库锁定 **Verilog 2005**（见 u1-l4），唯独 `stdcells` 用了 **SystemVerilog**（文件后缀 `.sv`）。原因在于开关级建模需要 Verilog 2005 没有的语法——**数组参数**：要给每个晶体管单独指定 W/L/M/NF，最自然的写法是 `parameter integer W[N-1:0] = '{0,0,0,0}`，这是 SystemVerilog 的「unpackged 数组参数」，Verilog 2005 不支持。

于是带来一个工程问题：OH! 主仿真器是 iverilog（默认 `-g2005`），而本讲的 `.sv` 用了 SV 语法。解决办法在 `run.sh` 里——先用第三方工具 `sv2v`（SystemVerilog to Verilog）把 `.sv` 翻译成等价的 `.v`，再用 iverilog 编译翻译结果。

`oh_nand2_tb.sv` 还展示了开关级 testbench 的两个特色：用 `supply0`/`supply1` 这两种 Verilog 网络类型充当电源与地（持续驱动 0/1），以及用 `` `define OH_DEBUG `` 打开晶体管的调试打印。

#### 4.2.2 核心流程

开关级仿真的编译流程：

1. `sv2v` 把 testbench 与设计的 `.sv` 分别翻译成 `.v.tmp`（临时纯 Verilog）。
2. `iverilog` 把翻译后的 testbench、设计，连同未翻译的开关封装 `oh_nmos.sv`/`oh_pmos.sv` 一起编译成可执行 `nand2.out`。
3. 运行 `nand2.out`，按 testbench 的 `initial` 块注入 `(a,b)` 序列，dump 出 `waveform.vcd`。
4. （可选）用 gtkwave 看波形，对照 4.1.2 的真值表。

#### 4.2.3 源码精读

testbench 骨架与电源网络：

[stdcells/dv/oh_nand2_tb.sv:3-14](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdcells/dv/oh_nand2_tb.sv#L3-L14) —— `supply0 vss;` 恒为逻辑 0、`supply1 vdd;` 恒为逻辑 1，二者就是被测电路的电源/地网络；`initial` 里 `$dumpfile/$dumpvars` 准备 VCD，1000ns 后 `$finish`。

DUT 的参数化例化：

[stdcells/dv/oh_nand2_tb.sv:27-41](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdcells/dv/oh_nand2_tb.sv#L27-L41) —— `.SIM("switch")` 选中晶体管分支；`.W({0,1,2,3})` 等把四个晶体管的尺寸塞进数组参数（这里只是占位数字，仿真不影响行为）。对照 `oh_nor2_tb.sv` 可以看到另一种写法——用 `defparam` 改参数（u1-l4 提醒过 OH! 设计规范禁用 `defparam`，`oh_nor2_tb.sv` 属于较旧的示例）。

仿真脚本本身：

[stdcells/dv/run.sh:1-4](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdcells/dv/run.sh#L1-L4) —— 第 2、3 行 `sv2v` 翻译，第 4 行 `iverilog` 编译。

#### 4.2.4 代码实践

**目标**：把 `run.sh` 跑通，至少看到编译产物或波形。

**操作步骤**：

1. 确认本机是否安装 `sv2v`（`sv2v --version`）与 `iverilog`（`iverilog -V`）。
2. 阅读 [stdcells/dv/run.sh:1-4](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdcells/dv/run.sh#L1-L4)，发现第 4 行引用 `../netlist/oh_pmos.sv`——但仓库里**没有 `netlist/` 目录**，`oh_pmos.sv` 实际在 `../hdl/`。
3. 修正该路径：把 `../netlist/oh_pmos.sv` 改为 `../hdl/oh_pmos.sv`。
4. 在 `stdcells/dv/` 下执行 `bash run.sh`，再 `./nand2.out`，最后 `gtkwave waveform.vcd`。

**需要观察的现象**：编译通过；运行时控制台因 `OH_DEBUG` 打印每个晶体管的实例与尺寸；波形里 `z` 在 `a=b=1` 段为 0、其余为 1。

**预期结果**：`z` 序列 `1,1,1,0`。

**待本地验证**：是否安装了 `sv2v`、修正后的脚本能否在你机器上通过，取决于本地环境。若没有 `sv2v`，可尝试 `iverilog -g2012 oh_nand2_tb.sv ../hdl/oh_nand2.sv ../hdl/oh_nmos.sv ../hdl/oh_pmos.sv -o nand2.out` 直接编译 SV（iverilog 的 `-g2012` 支持本讲用到的 SV 语法）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 OH! 全库坚持 Verilog 2005，却唯独 `stdcells` 破例用 SystemVerilog？

**参考答案**：开关级建模需要给每个晶体管单独的尺寸参数，SystemVerilog 的数组参数 `integer W[N-1:0]` 是最自然的表达，Verilog 2005 无此能力。`stdcells` 只用于教学/原理演示，不进入主仿真与综合流，因此可以破例。

**练习 2**：`supply0`/`supply1` 和普通 `wire` 拉成 0/1 有何不同？

**参考答案**：`supply0/supply1` 是 Verilog 内置的「电源/地」网络类型，驱动强度最高（supply strength），用来建模真实电源网络；普通 `wire` 赋 0/1 是普通驱动强度，且需要 `assign`。电源网络通常不应被信号驱动覆盖，所以用专门类型。

---

### 4.3 厂商仿真模型（xilibs）与 -y 库替换机制

#### 4.3.1 概念说明

OH! 的链路 elink/mio 设计成可在 Xilinx FPGA 上实现。当你写下 `MMCME2_ADV mmcm_cclk (...)`（时钟管理器）、`IDDR ...`（双沿输入寄存器）、`IBUFDS ...`（差分输入缓冲）时：

- **在 Xilinx 综合工具（Vivado）里**：这些名字是**厂商原语（primitive）**，工具认识它们，会映射到 FPGA 芯片里真实的硬资源（MMCM 硬核、IOB 里的 DDR 触发器、差分输入焊盘）。
- **在 iverilog 仿真里**：iverilog 不认识这些名字，它们是**未定义模块 = 黑盒**，仿真会报错或行为未知。

`xilibs`（Xilinx libs）就是这层黑盒的**行为级替身**——用可综合的纯 Verilog 把每个原语的「外部行为」模拟出来，让仿真器能跑。README 一句话点明定位：

[xilibs/README.md:1-2](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/xilibs/README.md#L1-L2) —— 这些原语「可被 Verilator 仿真，也能正确综合」。换言之，仿真用它当模型、上板时由 Vivado 替换成真硬件，二者行为一致。

注意 `xilibs` 与 `asiclib` 的对照：一个是 FPGA 厂商（Xilinx）原语的模型，一个是 ASIC 工艺库（PDK）标准单元的黄金模型——都是「绑定某一家底层实现」的库，分别服务 FPGA 流程与 ASIC 流程。

#### 4.3.2 核心流程

让黑盒在仿真中现形的机制分两半：

**（a）厂商原语黑盒怎么被填上**：iverilog 编译时，遇到一个未定义模块名（如 `IDDR`），不会立刻报错，而是去**库搜索目录**里找有没有同名文件。找到就用它当定义、填上黑盒；找不到才报「unknown module」。OH! 把所有 Xilinx 原语模型集中放在 `xilibs/dv/`，靠这个机制被自动拾取。

**（b）库搜索怎么配置**：`-y <目录>` 告诉 iverilog「去这里按模块名找 `.v` 文件」；`+incdir+ <目录>` 告诉它「`include` 头文件去这里找」。OH! 把这些搜索路径集中写在 `libs.cmd`，编译脚本用 `-f libs.cmd` 一次性读入。

真实例化链路：elink 的收发时钟/IO 模块在 `generate if(TARGET=="XILINX")` 分支里例化这些原语；综合时走真硬件，仿真时被 xilibs 模型替换。

#### 4.3.3 源码精读

先看几个典型 xilibs 模型有多「朴素」：

[xilibs/dv/IBUFDS.v:2-17](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/xilibs/dv/IBUFDS.v#L2-L17) —— 差分输入缓冲的全部实现就一句 `assign O = I & ~IB;`（正端为 1 且负端为 0 时输出 1）。真实 FPGA 里 IBUFDS 是模拟差分接收器，这里用最简布尔近似其数字行为——够仿真用即可。

[xilibs/dv/IDDR.v:35-58](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/xilibs/dv/IDDR.v#L35-L58) —— 双沿输入寄存器模型：`posedge C` 采到的 `D` 进 `Q1_pos`（再打一拍得 `Q1_reg`），`negedge C` 采到的 `D` 进 `Q2_neg`（再打一拍）。`Q1/Q2` 按 `DDR_CLK_EDGE` 模式选择源。一句 `localparam HOLDHACK = 0.1;` 加 `#(HOLDHACK)` 延时，是为了在仿真里模拟保持时间、避免零延时竞争。这正是 elink 接收通路 [erx_io.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_io.v#L440-L448) 里 `IDDR #(.DDR_CLK_EDGE("SAME_EDGE_PIPELINED"))` 把 LVDS 双沿数据解成 `Q1/Q2` 两比特的依据。

[xilibs/dv/ODDR.v:29-48](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/xilibs/dv/ODDR.v#L29-L48) —— 双沿输出寄存器模型：`D1` 在上升沿进 `Q1`、`D2` 在下降沿进 `Q2`，输出 `Q = C ? Q1 : Q2_reg`（时钟高输出上升沿数据、低输出下降沿数据），与 IDDR 互为逆过程。

再看复杂原语 MMCM，注意它**自带免责声明**：

[xilibs/dv/MMCME2_ADV.v:89-97](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/xilibs/dv/MMCME2_ADV.v#L89-L97) —— 用 `localparam` 由 `CLKIN1_PERIOD/CLKFBOUT_MULT_F/DIVCLK_DIVIDE` 推算 VCO 周期与各输出相位移，是行为级近似而非真实模拟锁相环。

[xilibs/dv/MMCME2_ADV.v:159-172](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/xilibs/dv/MMCME2_ADV.v#L159-L172) —— 注释 `BUG! This only supports divide by 2,4,8...` 明说这个分频器模型只支持 2 的幂次分频。这印证了 xilibs 的定位：**够用的行为模型**，不是精确的硬件副本，仿真读时序时要把这点记在心里。

[xilibs/dv/MMCME2_ADV.v:204-214](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/xilibs/dv/MMCME2_ADV.v#L204-L214) —— `LOCKED` 用一个减法计数器模拟「上电后若干周期才锁定」，是对真实 MMCM 锁定行为的粗略建模。

这些原语在 elink 里被真实例化：

[elink/hdl/etx_clocks.v:172-208](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_clocks.v#L172-L208) —— 在 `generate if(TARGET=="XILINX")` 分支里例化 `MMCME2_ADV mmcm_cclk`，用 `CLKFBOUT_MULT_F/CLKOUTx_DIVIDE` 等参数配出 cclk/tx_lclk/tx_lclk90/tx_lclk_div4 多路时钟。综合上板时这是真 MMCM；iverilog 仿真时，编译器找不到 `MMCME2_ADV` 的定义，就靠 `-y` 去 xilibs 里把它拾回来（机制见下）。

库搜索配置（`-y` 机制）：

[stdlib/testbench/libs.cmd:3-22](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/libs.cmd#L3-L22) —— 一长串 `-y ../../<模块>/hdl` 或 `-y ../../<模块>/dv`，告诉 iverilog 遇到未定义模块就按模块名去这些目录找同名 `.v`。第 21–22 行专门配了 xilibs：`-y ../../xilibs/hdl` 与 `-y ../../xilibs/ip`。

[stdlib/testbench/libs.cmd:23-32](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/libs.cmd#L23-L32) —— `+incdir+` 段告诉 iverilog `` `include `` 的 `.vh` 头文件（如各 `regmap.vh`、`constants.vh`）去这些目录找。

[scripts/build.sh:15-19](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/scripts/build.sh#L15-L19) —— 顶层编译命令：`iverilog -g2005 -DTARGET_SIM=1 -DCFG_ASIC=0 -f $OH_HOME/scripts/libs.cmd -o dut.bin $1`。`-f libs.cmd` 把上面所有 `-y`/`+incdir+` 一次性灌进来，于是「分散在全仓库的模块定义 + 头文件 + xilibs 原语模型」被自动拼成一个可仿真整体。

> **一处可验证的目录漂移**：`libs.cmd` 第 21–22 行写的是 `-y ../../xilibs/hdl` 与 `-y ../../xilibs/ip`，但仓库里 `xilibs/` 下**没有 `hdl/` 目录**，所有原语模型实际都在 `xilibs/dv/`，而 `xilibs/ip/` 只有一个 `.xci`（Xilinx IP 的 XML 配置，非 `.v`）。也就是说，按现配置 iverilog 在 `-y` 搜索时找不到这些原语。要让 xilibs 模型真正被拾取，需把搜索路径改成 `-y ../../xilibs/dv`。这与 u1-l2、u9-l1 指出的「文档/脚本与实际布局漂移」是同一类问题，结论以实际目录为准。

#### 4.3.4 代码实践

**目标**：亲手验证 `-y` 库替换如何让一个黑盒原语在仿真里「现形」。

**操作步骤**：

1. 写一个 3 行顶层 `tb_ibufds.v`：声明 `reg I, IB; wire O;`，例化 `IBUFDS dut (.I(I), .IB(IB), .O(O));`，`initial` 里给 `I/IB` 几组值后 `$finish`。
2. 先**不带**库搜索编译：`iverilog -g2005 tb_ibufds.v -o a.out`。预期报 `IBUFDS` 为 unknown module（黑盒未被解析）。
3. 再**带上**正确目录编译：`iverilog -g2005 -y xilibs/dv tb_ibufds.v -o a.out`。预期编译通过，因为 iverilog 在 `xilibs/dv` 里按名找到了 `IBUFDS.v`。
4. 运行 `./a.out`，对照 `O = I & ~IB` 验证输出。

**需要观察的现象**：第 2 步报未定义模块；第 3 步编译通过；运行结果符合 `O = I & ~IB`。

**预期结果**：`(I,IB)=(1,0)→O=1`；`(1,1)→O=0`；`(0,1)→O=0`。

**待本地验证**：取决于本机 iverilog 版本对未知模块是报错还是仅告警；旧版会静默把黑盒当 0，新版更严格。

#### 4.3.5 小练习与答案

**练习 1**：`-y` 与 `+incdir+` 分别解决什么问题？

**参考答案**：`-y <目录>` 解决「**模块定义**在哪里」——遇到未定义模块，按模块名在该目录找 `<模块名>.v`。`+incdir+ <目录>` 解决「`` `include `` 的**头文件**在哪里」——找到 `.vh` 并展开宏。前者补黑盒，后者补宏定义。

**练习 2**：为什么 `xilibs` 里的 MMCM 模型自称「只支持 2/4/8 分频」也不影响 elink 的主流程仿真？

**参考答案**：elink 的仿真关心的是事务级时序与协议握手（包的收发），而不是 MMCM 输出时钟的精确频率。只要模型能给出「若干周期后锁定 + 大致分频」的可用时钟，链路的功能行为就能被验证；精确的模拟锁相环行为由上板后的真硬件保证。

**练习 3**：`IDDR.v` 里的 `HOLDHACK = 0.1` 延时起什么作用？

**参考答案**：给采样寄存器加一点 `#(0.1)` 延时，模拟真实触发器的保持时间，避免仿真器在零延时下因「同一拍数据与时钟同时翻」而产生不确定（X）结果。

## 5. 综合实践

把本讲三个模块串起来：**用开关级单元搭一个或非门，并在 iverilog 里跑通它**。

1. **设计**：仿照 [oh_nand2.sv](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdcells/hdl/oh_nand2.sv#L7-L49) 的结构，新建一个示例文件 `oh_nor2_demo.sv`（示例代码，非仓库原有文件）：保留 `SIM` 双实现开关；`rtl` 分支写 `assign z = ~(a | b);`；`switch` 分支把 NMOS 改成**并联**（两管都 `s(vss)/d(z)`、栅极分别接 `a/b`）、PMOS 改成**串联**（`vdd→m2→mid→m3→z`）。
2. **测试台**：照 [oh_nand2_tb.sv:17-41](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdcells/dv/oh_nand2_tb.sv#L17-L41) 写一个遍历 `00/01/10/11` 的 testbench，`.SIM("switch")`、`supply0/supply1` 当电源。
3. **编译**：用 4.2.4 的修正流程（`sv2v` 或 `iverilog -g2012`），连同 `oh_nmos.sv`/`oh_pmos.sv` 一起编译。
4. **验证**：观察 `z` 序列应当是 `1,0,0,0`（仅 `a=b=0` 时为 1），即或非逻辑 \( z=\overline{a+b} \)。
5. **延伸**：把 `.SIM` 改成 `"rtl"` 再跑一次，确认两套实现结果一致——这正是 u9-l1「双实现」思想在晶体管级的最小体现。

若本地无法运行，至少完成「纸上作业」：画出 NOR 的 PDN/PUN 拓扑，并填写四行真值表。

## 6. 本讲小结

- `stdcells` 是 OH! 最底层的实现底座：用 Verilog 内建开关原语 `nmos`/`pmos` 描述 CMOS 晶体管，`oh_nand2` 用「NMOS 串联下拉 + PMOS 并联上拉」的标准结构实现与非门。
- 四种实现底座的层次：`stdcells`（晶体管/开关级，教学）→ `asiclib`（标准单元硬核，ASIC）→ `stdlib`（RTL 软核）→ `xilibs`（Xilinx 原语模型，FPGA）。
- `stdcells` 破例使用 SystemVerilog（`.sv`），是因为开关级建模需要数组参数（逐管给 W/L/M/NF）；`run.sh` 用 `sv2v` 把 SV 翻成 Verilog 再交给 iverilog。
- `xilibs` 为 Xilinx 原语（IBUFDS/IDDR/ODDR/MMCME2_ADV…）提供**行为级替身**，让综合时才存在的黑盒在 iverilog 仿真里现形；它们是「够用」的近似（如 MMCM 自述仅支持 2 的幂次分频）。
- iverilog 的 `-y <目录>` 按「模块名→同名 `.v`」补黑盒、`+incdir+ <目录>` 补 `` `include `` 头文件；OH! 用 `libs.cmd` 集中配置，`build.sh` 以 `-f` 一次读入。
- 仍有目录漂移：`libs.cmd` 把 xilibs 搜索路径写成 `xilibs/hdl`、`xilibs/ip`，但模型实际在 `xilibs/dv/`；`run.sh` 引用了不存在的 `netlist/` 目录。结论一律以源码实际布局为准。

## 7. 下一步学习建议

- 继续本单元：[u9-l4 padring 与芯片顶层集成](u9-l4-padring-chip-integration.md) 会从晶体管/单元层上升到芯片焊盘环与板级顶层，理解「IP → 单元 → padring → 板级」的收敛过程。
- 回顾对照：把本讲的 `xilibs`（FPGA 厂商原语）与 u9-l2 的 `asiclib`（ASIC 标准单元）并排看，体会 OH! 用「两套底层库」分别服务 ASIC 与 FPGA 的对称设计。
- 源码延伸阅读：打开 `xilibs/dv/` 目录浏览 `ISERDESE2.v`、`OSERDESE2.v`、`PLLE2_ADV.v`，它们是 elink/mio 高速串化/解串与时钟生成的同款行为模型；再回到 `elink/hdl/erx_io.v`、`etx_clocks.v` 看它们如何被真实例化，巩固「黑盒→-y 拾取→行为模型」这条链。
