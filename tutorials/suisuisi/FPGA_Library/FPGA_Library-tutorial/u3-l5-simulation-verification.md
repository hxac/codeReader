# 仿真验证与 SystemVerilog 验证环境

## 1. 本讲目标

本讲是 Unit 3（AXI-Lite IP 封装与软硬件协同）的收尾篇，主题从「设计」转到「验证」。

前面几讲我们读完了 AES 核心的数据通路（Unit 2）和它的 AXI 封装骨架（u3-l1 ~ u3-l4）。但 RTL 写出来只是「我认为它对」，要证明它对，必须用**仿真（simulation）**去跑、去比对。本仓库的作者为 AES 核心准备了**两套**风格完全不同的验证资产：

1. `hdl/tb/` 下的传统 Verilog 单元测试——短小、直白、只施加激励、靠人眼看波形。
2. `hdl/VE_sv/` 下的 SystemVerilog 验证环境（Verification Environment）——用「类 + 接口 + 参考模型 + 断言」的面向对象结构，自动驱动并自动比对。

学完本讲，你应当能够：

- 看懂 `tb_s_box.v`、`tb_gf_inv_8.v` 这类单元 testbench 如何施加激励、为什么它们没有自动比对。
- 理解 `VE_sv/` 中 `class` / `interface` / `program` / `env` 的面向对象验证结构，以及一条「激励→参考模型→断言」的完整比对链路。
- 说清 `AlreadyComputed`、`AesCore`、`AesEnvironment`、`agent_MixColumn` 各自扮演的角色，并诚实判断这个验证环境到底验证了什么、又有哪些缺口。
- 能动手用 Icarus Verilog（或 Vivado/Verilator）跑起一个 testbench，并用 GTKWave 看波形确认 S-Box 输出。

> 承接：u2-l5 已经打开过 S-Box 的复合域黑盒 `bSbox`，本讲正是给 `bSbox` 与 `gf_inv_8` 这类底层模块做「验证」的讲义。

## 2. 前置知识

本讲用到的概念都不复杂，先用大白话过一遍。

- **仿真（simulation）**：用一个软件（仿真器）模拟硬件电路在时钟驱动下的行为，不用真上 FPGA 板。Verilog/VHDL 仿真器常见的有 Icarus Verilog（开源、轻量）、Verilator（开源、极快）、Vivado Simulator（Xilinx 自带）、ModelSim/Questa 等。
- **testbench（测试平台）**：一段「不为综合、只为仿真」的代码。它产生输入信号（**激励 stimulus**），喂给被测模块，再观察输出。被测模块常被称为 **DUT（Design Under Test）**。
- **自检（self-checking）**：理想的 testbench 不只产生激励，还在内部用一句 `if (出错了) $display("ERROR")` 或 `assert` 自动判断对错。本仓库的 `tb/` 目录基本**没有**自检，只能靠波形人眼判断；`VE_sv/` 则有断言自检。
- **黄金参考模型（golden reference model）**：用另一种写法（这里是纯 SystemVerilog 软件、用查表实现 AES）算出「正确答案」，再和 DUT 的输出逐拍比对。一旦参考模型正确，DUT 的对错就由它说了算。
- **SystemVerilog（SV）相对 Verilog 多了什么**：本讲会用到 `class`（类，可面向对象）、`interface` + `modport`（把一堆信号打包并规定谁能驱动谁能读）、`program`（程序块，给测试用，带隐式时序）、`extern` 方法声明、`assert`（断言）、`$display`/$write 打印、动态数组/队列 `[$]`。这些都是验证专用、不会综合成硬件的特性。
- **断言（assertion）**：`assert (条件) else <出错处理>`。条件不成立时触发 else 分支，常用来打印错误。本仓库用的是「立即断言（immediate assertion）」，写在 `function` 里，调用那一刻立即判断。
- **覆盖率（coverage）**：衡量「测试到底覆盖了多少功能点」。完整做法是用 `covergroup`/`coverpoint` 或 `cover property`。**诚实地说，本仓库的 VE_sv 没有写任何覆盖率采集代码**，它的「验证」靠参考模型 + 断言完成；`AlreadyComputed` 的真正角色是给参考模型提供「黄金查表数据」，而不是覆盖率模块。这一点第 4 节会讲清楚。

## 3. 本讲源码地图

本讲涉及两套代码，分处两个目录：

| 目录 | 文件 | 角色 | 语言 |
|------|------|------|------|
| `hdl/tb/` | `tb_s_box.v` | 给单字节 S-Box（`bSbox`）施加激励的单元 testbench | Verilog |
| `hdl/tb/` | `tb_gf_inv_8.v` | 给 GF(2⁸) 求逆器（`gf_inv_8`）施加激励的单元 testbench | Verilog |
| `hdl/tb/` | `tb_gf_inv_2/4.v`、`tb_gf_mul_*.v`、`tb_gf_scl_*.v` | 同风格，覆盖复合域各子模块（本讲不逐个展开） | Verilog |
| `hdl/VE_sv/` | `ve_AES_Include.sv` | 整个 SV 验证环境的「总 include」，决定编译顺序 | SV |
| `hdl/VE_sv/` | `ve_AES_types.sv` | 参考模型用的类型与宏（`word8`、`MAXBC`…） | SV |
| `hdl/VE_sv/` | `ve_AES_BaseUnit.sv` | 所有验证组件的基类 `BaseUnit`（有名字、有 `run()`） | SV |
| `hdl/VE_sv/` | `ve_AES_AlreadyComputed.sv` | 预算好的「黄金表」：S 盒、逆 S 盒、对数表、Rcon… | SV |
| `hdl/VE_sv/` | `ve_AES_Core.sv` | **黄金参考模型** `AesCore`：纯软件实现的 AES | SV |
| `hdl/VE_sv/` | `ve_AES_interface.sv` | 接口 `mix_column_intf` + `drv/rcv` modport | SV |
| `hdl/VE_sv/` | `ve_AES_class_MixColumn.sv` | 验证代理 `agent_MixColumn`：驱动激励 + 断言比对 | SV |
| `hdl/VE_sv/` | `ve_AES_env.sv` | 环境 `AesEnvironment`：把参考模型和代理编排到一起 | SV |
| `hdl/VE_sv/` | `test_program.sv` | 程序块 `main_program`：创建 env 并启动 | SV |
| `hdl/VE_sv/` | `ve_AES_top_mix_column.sv` | 顶层 `top`：例化 DUT（`MixColumns`）+ 接口 + 程序块 | SV |

一句话定位：`tb/` 是「给底层 GF/S-Box 模块一个个点测」的散装小测试；`VE_sv/` 是「面向对象、带参考模型、目前聚焦 MixColumns 这一列」的完整验证环境。

## 4. 核心概念与源码讲解

### 4.1 两种验证范式：散装 tb 与 SV 环境

在动手读代码前，先把两种范式的差别讲透。看这张对比表：

| 维度 | `hdl/tb/*.v`（传统单元 tb） | `hdl/VE_sv/`（SV 验证环境） |
|------|------------------------------|------------------------------|
| 语言 | Verilog（可综合子集之外很少用） | SystemVerilog 验证特性（class/interface/program） |
| 规模 | 一个文件测一个模块，几十行 | 多个类协作，几百行 |
| 激励来源 | 写死在 `initial` 块里的几个常量 | 代理 `agent` 用循环自动生成多组激励 |
| 判对错 | **不判**，靠人眼看波形 | 参考模型算期望值 + `assert` 自动判 |
| 复用性 | 几乎不可复用 | 基类 + 继承，可扩展 |
| 适合阶段 | 开发初期快速点测单个模块 | 系统级、回归式验证 |

为什么需要两套？因为它们处于开发的不同阶段。底层模块（`gf_inv_8`、`bSbox`）刚写好时，作者用一个最小 tb 喂几个值、看波形对不对，这最快；等到要做「这一列 MixColumns 对不对」这类需要大量随机/遍历激励、并且要自动判对的验证时，就值得搭一个 SV 环境，把「怎么算对的」交给参考模型，把「怎么喂激励、怎么比对」交给代理。

> 结论先行（后面会用源码证明）：本仓库的 SV 环境**目前只验证了 `MixColumns` 这一个 DUT**，参考模型 `AesCore` 虽然是完整 AES，但其 `run()` 任务里只调用了 `MixColumn`（加密整轮 `Encrypt` 被注释掉了）。所以它是一个「框架已就位、用 MixColumns 打样」的半成品验证环境。

### 4.2 单元级 Verilog testbench（tb_s_box / tb_gf_inv_8）

#### 4.2.1 概念说明

传统单元 testbench 的套路固定：例化 DUT → 用 `initial` 块按时序改输入 → `$finish` 结束。它**不声明时钟**（因为这些 DUT 是纯组合逻辑，输出随输入立刻变化），也**不写自检**。验证靠仿真后用波形工具观察。

#### 4.2.2 核心流程

以 `tb_gf_inv_8.v` 为例，流程是：

1. 声明输入 `reg data_in0`、输出 `wire data_out`。
2. 例化 DUT：`gf_inv_8 DUT(data_in0, data_out)`。
3. `initial` 块里每隔 `#10`（10 ns）换一个输入值（0、0x10、0x06、0x01、0x74、0x10、0x47）。
4. `$finish` 退出仿真。
5. 仿真结束后，用 GTKWave 等工具看 `data_in0` 与 `data_out` 的波形，逐个手算验证 `data_out` 是否真的是 `data_in0` 在 GF(2⁸) 中的逆元。

#### 4.2.3 源码精读

先看 `tb_gf_inv_8.v` 的 DUT 例化与激励：

[_hdl/tb/tb_gf_inv_8.v:L24-L47](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/tb/tb_gf_inv_8.v#L24-L47) —— 例化 `gf_inv_8` 为 `DUT`，并用 `initial` 块逐拍改变 `data_in0`。注意它 `include "../aes_types.v"` 只为了拿到宏定义，并**不**包含 `gf_inv_8` 本体及其依赖（`gf_inv_4`、`gf_mul_4` 等），所以编译时必须把这些 `.v` 文件一起喂给仿真器（见第 5 节实践）。

对应的 DUT 本体在 `gf_s_box/gf_inv_8.v`：

[_hdl/gf_s_box/gf_inv_8.v:L22-L61](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/gf_s_box/gf_inv_8.v#L22-L61) —— 把 8 位输入拆成两个 4 位 `(a,b)`，算子域分母 `c`，调用 `gf_inv_4 dinv(c,d)` 求逆，再两次 `gf_mul_4` 重组出 8 位逆元 `Q={p,q}`。这是 u2-l5 讲过的 Canright 复合域求逆，这里只是被 testbench 当作黑盒来点测。

再看 `tb_s_box.v`：

[_hdl/tb/tb_s_box.v:L24-L47](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/tb/tb_s_box.v#L24-L47) —— 例化 `bSbox` 做正向（`ENCRIPT`）与逆向（`DECRIPT`）两次替换，喂入 0x01、0x10、0x02 三个值。

⚠️ **这里有一个真实的 bug 需要诚实指出**：第 31、32 行两次例化的实例名都叫 `sbox_e`：

```verilog
bSbox sbox_e(data_in, `ENCRIPT, data_out_e);   // 第 31 行
bSbox sbox_e(data_out_e, `DECRIPT, data_out_d); // 第 32 行 —— 实例名重复！
```

同一模块里两个实例同名，绝大多数仿真器（含 iverilog）会报错或警告，第二行本应是 `sbox_d`。也就是说，**`tb_s_box.v` 原样是无法直接编译通过的草稿**。第 5 节的实践会先修掉这个笔误再跑。这再次印证 u2-l5 的结论：本仓库是草稿级 RTL，须批判阅读。

`bSbox` 的 DUT 本体在 `src/aes_s_box.v`：

[_hdl/src/aes_s_box.v:L21-L85](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_s_box.v#L21-L85) —— `encrypt=1` 走正向 S-Box、`encrypt=0` 走逆 S-Box，由 `select_not_8` 切换；中间 `gf_inv_8 inv(Z,C)` 做求逆。输入 `A`、输出 `Q`。

#### 4.2.4 代码实践（点测 S-Box）

**实践目标**：把 `tb_s_box.v` 跑起来，用波形确认 S-Box 输出与 FIPS-197 标准一致。

**操作步骤**：

1. 修复实例重名：把第 32 行的 `sbox_e` 改成 `sbox_d`（仅在你的本地工作副本上改，**不要改动仓库源码**；可复制一份到临时目录再改）。
2. 用 iverilog 编译。`tb_s_box.v` 里 `include "../aes_include.v"` 会自动把整个复合域 S-Box 依赖链都拉进来，所以只需：

   ```bash
   cd HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/tb
   iverilog -g2012 -o tb_s_box.vvp tb_s_box.v
   vvp tb_s_box.vvp
   ```

   若 `include` 路径解析不到，加 `-I ../src -I ..` 指定搜索目录。

3. 加 dump 波形再跑：在 `initial` 开头加一行 `module` 内的 `$dumpfile("tb_s_box.vcd"); $dumpvars;`（改在你的临时副本里），重新编译运行后会得到 `tb_s_box.vcd`。
4. 用 GTKWave 打开：`gtkwave tb_s_box.vcd`，观察 `data_in`、`data_out_e`、`data_out_d`。

**需要观察的现象**：组合逻辑下，`data_out_e` 在 `data_in` 变化后几乎立即变化（仿真里是同一时刻）。

**预期结果**（对照 FIPS-197 标准 S-Box，可在 `VE_sv/ve_AES_AlreadyComputed.sv` 的 `S[]` 表里查到）：

| 时刻 | `data_in` | `data_out_e`（正向 S-Box） | `data_out_d`（把上一步结果再逆替换） |
|------|-----------|----------------------------|--------------------------------------|
| 0 ns | 0x01 | 0x7c | 0x01（回到原值） |
| 10 ns | 0x10 | 0xca | 0x10 |
| 20 ns | 0x02 | 0x77 | 0x02 |

> 说明：`S[0x01]=0x7c`、`S[0x10]=0xca`、`S[0x02]=0x77` 是标准值；第二个 `bSbox` 实例对正向结果再求逆，应还原成输入，这正是「加密→解密回到原文」的最小验证。若波形与上表一致，说明 S-Box 数据通路正确。

**若无法确定运行结果**：仿真器/路径差异可能导致编译失败，上述命令与预期值标注为「待本地验证」，以你本机实际波形为准。

#### 4.2.5 小练习与答案

**练习 1**：`tb_gf_inv_8.v` 里输入了 `8'h01`，GF(2⁸) 中 1 的逆元应是多少？为什么这个值不适合用来检验求逆器？
**答案**：1 的逆元还是 1（因为 1×1=1）。它对任何「看起来在算逆」的 buggy 实现都会蒙混过关，所以没有区分度；要检验应选非平凡值，如 0x74。

**练习 2**：为什么这些 tb 都没有时钟？
**答案**：被测的 `gf_inv_8`、`bSbox` 是纯组合逻辑，输出只取决于当前输入，没有寄存器，因此不需要时钟，只需用 `#延时` 改变输入即可观察输出。

### 4.3 SystemVerilog 验证环境总览（类层次 + 接口 + 程序块）

#### 4.3.1 概念说明

`VE_sv/` 是一个缩小版的面向对象验证平台（思路接近 UVM 的极简版）。它把职责拆给几个「类（class）」：

- **基类 `BaseUnit`**：所有组件的祖先，提供 `name`、`id` 和一个空的虚任务 `run()`。任何「能被环境启动」的组件都继承它。
- **参考模型 `AesCore`**：继承 `BaseUnit`，用纯软件（查表 + 函数）实现一整套 AES，是「黄金答案」的来源。
- **代理 `agent_MixColumn`**：继承 `BaseUnit`，负责往接口上**驱动激励**，并调用参考模型算期望值、用 `assert` 与 DUT 输出**比对**。
- **环境 `AesEnvironment`**：继承 `BaseUnit`，把参考模型和代理装进一个队列 `units[$]`，统一 `run()`。
- **接口 `mix_column_intf`**：用 `modport` 规定代理只能 `output`（驱动）b0~b3、只能 `input`（观察）a/c 系列，明确方向、防误驱动。
- **程序块 `main_program`**：测试入口，创建 `env`、调 `env.run()`。
- **顶层 `top`**：例化真正被测的硬件 DUT（`MixColumns`）、接口、程序块。

它们之间的层次关系（谁包含/持有谁）如下：

```
top（模块）
 ├── MixColumns DUT          ← 被测硬件（来自 hdl/src/aes_mix_columns.v）
 ├── mix_column_intf 接口实例 ← 连接 DUT 与程序块
 └── main_program 程序块
      └── AesEnvironment env
           ├── AesCore aes          （参考模型，持有 AlreadyComputed 黄金表）
           └── agent_MixColumn      （驱动激励 + 断言比对，持有 aes 引用）
```

整个编译顺序由总 include 文件规定：

[_hdl/VE_sv/ve_AES_Include.sv:L20-L34](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/VE_sv/ve_AES_Include.sv#L20-L34) —— 按类型→接口→基类→参考模型→代理→环境的顺序 include。注意第 33 行把 `ve_AES_top_mix_column.sv` 注释掉了，意味着默认编译并不把 `top` 算进去——你需要手动启用它或单独编译它，环境才能完整跑起来。

类型与宏定义在这：

[_hdl/VE_sv/ve_AES_types.sv:L21-L31](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/VE_sv/ve_AES_types.sv#L21-L31) —— 定义 `word8=bit[7:0]`、`word32`，以及 `MAXBC=8`、`MAXKC=8`、`MAXROUNDS=14`。⚠️ 注意：这里的 `MAXBC=8`、`MAXROUNDS=14` 是参考模型为支持到 AES-256（14 轮）设的上限，**与 RTL 里 AES-128 的 `NO_OF_ROUNDS=10` 不是一回事**；实际用几轮由 `numrounds` 表按 `BC/KC` 查表决定（见 4.4）。

#### 4.3.2 核心流程

1. `top` 例化 DUT `MixColumns`、接口 `mix_column_intf0`、程序块 `main_program`（把同一个接口同时按 `drv` 和 `rcv` 两个 modport 传进去）。
2. 程序块 `initial` 中 `env = new(...)`：构造环境。
3. 环境 `new()` 里构造参考模型 `aes` 和代理 `agent_MixColumn`，二者**共享同一个 `AlreadyComputed` 黄金表对象**，代理还持有 `aes` 的引用。
4. `env.run()` 用 `fork...join_any` 并发启动各组件的 `run()`，任一完成即继续。
5. 代理 `run()` 循环驱动 21 组激励，每组结束后调用 `check_mix_column_result()` 比对。
6. 比对函数用参考模型算期望值，再用 `assert` 与 DUT 实际输出比较，不符则打印 `**ERROR**`。

### 4.4 黄金参考模型与 AlreadyComputed

#### 4.4.1 概念说明

`AlreadyComputed`（直译「已预算好的」）是这个环境里最容易引起误解的名字。它**不是**功能覆盖率模块，而是一个**预先算好、存放黄金查表数据的类**：标准 AES 的 S-Box（`S`）、逆 S-Box（`Si`）、GF(2⁸) 乘法用的对数表/反对数表（`Logtable`/`Alogtable`）、轮常数（`RC`）、行移位量（`shifts`）、按密钥/数据块长度查轮数的表（`numrounds`）。参考模型 `AesCore` 的所有运算都查这些表，因此 `AlreadyComputed` 是「正确答案」的根。

> 诚实地回应学习目标里「AlreadyComputed 在功能覆盖率中的作用」：**本仓库没有写任何 `covergroup`/`cover property`，不存在功能覆盖率采集**。`AlreadyComputed` 的实际作用是给参考模型供表，与覆盖率无关。若将来要做覆盖率驱动验证，应在这个类或环境里另加 `covergroup`，但目前没有。

`AesCore` 则是「用软件实现 AES、给 DUT 当标尺」的参考模型。它和 RTL 用的是**完全不同的实现方式**（软件查表，而非复合域组合逻辑），这很重要——参考模型与 DUT 不能共享同一种 bug，否则比对就失去意义。

#### 4.4.2 核心流程

参考模型的核心运算是 GF(2⁸) 乘法 `mul`，用对数表把「乘法」变成「加法 + 反查」：

\[ a \cdot b = \mathrm{Alog}\big[(\mathrm{Log}[a] + \mathrm{Log}[b]) \bmod 255\big] \]

因为 \( g^{\,\mathrm{Log}[a]} = a \)（\(g\) 为生成元），所以 \( a\cdot b = g^{\,\mathrm{Log}[a]+\mathrm{Log}[b]} \)。有了 `mul`，MixColumns 的矩阵乘、InvMixColumn、KeyExpansion 的非线性注入都能逐字节算出来。

#### 4.4.3 源码精读

黄金表类（节选关键部分）：

[_hdl/VE_sv/ve_AES_AlreadyComputed.sv:L22-L124](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/VE_sv/ve_AES_AlreadyComputed.sv#L22-L124) —— 用 SV 数组字面量 `'{} ` 列出 256 项的 `S`（第 62 行起，第 1 项 99=0x63 即标准 S[0x00]）、`Si`（第 81 行起）、`Logtable`/`Alogtable`、`RC`（第 100 行起，正是 u2-l4 讲过的 01,02,04,08,10,20,40,80,1B,36… 序列）、`shifts`、`numrounds`（第 115 行起，`numrounds[0][0]=10` 即 AES-128 的 10 轮）。这些都是 AES 的「真理表」。

参考模型用对数表实现乘法：

[_hdl/VE_sv/ve_AES_Core.sv:L48-L60](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/VE_sv/ve_AES_Core.sv#L48-L60) —— `mul(a,b)`：若 a、b 都非 0，用 `Logtable` 把乘法转成加法再 `Alogtable` 反查。⚠️ 小瑕疵：`else` 分支只置 `temp=0` 却没给 `val` 赋值，乘 0 时返回值未定义——这是草稿代码的又一处不严谨，但不影响非零输入的主流程。

参考模型的 MixColumn（期望值就是这么算出来的）：

[_hdl/VE_sv/ve_AES_Core.sv:L111-L137](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/VE_sv/ve_AES_Core.sv#L111-L137) —— 对每列每行用 `2,3,1,1` 矩阵乘（注释里画出了矩阵），结果存 `res` 再写回 `a`。这正是 u2-l3 讲的 MixColumns 数学定义的纯软件版，作为 DUT 的「标准答案」。

参考模型的 `run()`（**关键：当前只跑 MixColumn**）：

[_hdl/VE_sv/ve_AES_Core.sv:L262-L318](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/VE_sv/ve_AES_Core.sv#L262-L318) —— 设 `BC=KC=4`、`ROUNDS=numrounds[0][0]=10`，把明文 `a` 初始化为列号，做一次 `KeyExpansion` 和一次 `MixColumn`，然后打印。注意第 292 行 `Encrypt(a,rk)` 被注释掉了，第 299~316 行整段加解密也被块注释。**这就是「环境目前只验证 MixColumns」的直接证据**。

基类很简单，给出 `run()` 契约：

[_hdl/VE_sv/ve_AES_BaseUnit.sv:L21-L38](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/VE_sv/ve_AES_BaseUnit.sv#L21-L38) —— `BaseUnit` 持有 `name/id`，提供空的 `virtual task run()`，让子类覆盖。环境只需遍历一个 `BaseUnit` 队列统一 `run()`，这就是「多态」带来的可扩展性。

### 4.5 接口 modport 与代理自检（ve_AES_interface / agent_MixColumn）

#### 4.5.1 概念说明

**接口（interface）** 把一组相关信号打包，并用 **modport** 声明每个使用者看到的信号方向。好处是：例化时不用再列一长串端口，且能在编译期阻止「不该驱动某个信号的人去驱动它」。

本环境的接口叫 `mix_column_intf`，信号对应 `MixColumns` DUT 的端口：4 个输入字节 `b0~b3`（待混的列）和 8 个输出字节 `a0~a3, c0~c3`（`a` 是加密 MixColumn 结果、`c` 是解密 InvMixColumn 结果）。

**代理（agent）** 是「干活的」组件：它驱动 `b0~b3` 一组组激励，等 DUT 算完，再把 DUT 的输入重新喂给参考模型算期望值，用 `assert` 比对 DUT 的 `a0~a3`。

#### 4.5.2 核心流程

代理 `run()`：

```
for i = 0..20:
    drv.b0,b1,b2,b3 ← i 的 4 个字节      // 驱动激励
    #10                                      // 等组合逻辑稳定
    check_mix_column_result()                // 自检
```

`check_mix_column_result()`：

```
test_matrix ← {rcv.b3, rcv.b2, rcv.b1, rcv.b0}   // 取回 DUT 当前输入的一列
aes.MixColumn(test_matrix)                        // 参考模型算期望值
assert(test_matrix[0][0] == rcv.a3) …              // 逐字节与 DUT 输出比对
```

#### 4.5.3 源码精读

接口与 modport：

[_hdl/VE_sv/ve_AES_interface.sv:L34-L53](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/VE_sv/ve_AES_interface.sv#L34-L53) —— 声明 12 个 8 位信号；`modport drv` 只允许 `output b0~b3`（只驱动输入），`modport rcv` 允许 `input` 全部信号（只观察）。文件顶部第 20~33 行还留了一段被注释掉的「带端口的方向声明」旧写法，可作为对照。⚠️ 注意：`drv` modport 没有列出 `a/c`，但代理类的 `check` 里又通过 `rcv` 去读 `a/c`——代理同时持有 `drv` 和 `rcv` 两个 modport 句柄，分工明确。

代理的断言自检（本环境真正的「判官」）：

[_hdl/VE_sv/ve_AES_class_MixColumn.sv:L31-L58](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/VE_sv/ve_AES_class_MixColumn.sv#L31-L58) —— 把 DUT 输入组成 `test_matrix`，调参考模型 `aes.MixColumn(test_matrix)`，然后用 4 条带标号的立即断言 `check_mix_c_byte0..3` 逐字节比对。失败时进入 `else` 打印 `**ERROR** ... found != expected`。这正是「参考模型 + 断言」式自检的精髓。

代理的驱动循环：

[_hdl/VE_sv/ve_AES_class_MixColumn.sv:L60-L76](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/VE_sv/ve_AES_class_MixColumn.sv#L60-L76) —— `for i=0..20`，把 32 位 `i` 拆成 4 字节驱动到 `drv.b0~b3`，`#10` 后调用自检。这相当于自动生成了 21 组激励，比 `tb/` 里写死 3 个值要系统得多。

> 细节提示：比对里 `test_matrix` 用的是 `rcv.b3/b2/b1/b0` 与 `rcv.a3/a2/a1/a0` 交叉比较（注意下标是反的），这对应 `MixColumns` DUT 端口到状态矩阵字节序的映射约定。读这段时要把「端口名 a/b」与「矩阵下标 [i][j]」的对应关系理清，否则容易误判为 bug。

### 4.6 环境编排与程序块驱动（ve_AES_env / test_program / top）

#### 4.6.1 概念说明

**环境（env）** 是「总指挥」：它持有参考模型和代理，构造时让二者共享同一份黄金表与同一对接口句柄，并提供统一的 `run()`。**程序块（program）** 是 SV 专用于测试的块，自带隐式时序调度，避免测试代码与 DUT 在同一时刻抢着驱动信号。**顶层 top** 把 DUT、接口、程序块物理连到一起。

#### 4.6.2 核心流程

1. 仿真从 `top` 开始，`top` 例化 DUT `MixColumns`、接口 `mix_column_intf0`、程序块 `main_program`。
2. 程序块 `initial` 执行：`env = new("Environment", 1, intf_drv, intf_rcv)`。
3. 环境 `new()`：创建 `AesCore aes`（共享 `computed_val`）和 `agent_MixColumn`（持有 `aes` 引用），都压入 `units[$]`。
4. 程序块调 `env.run()`。
5. `env.run()`：`fork` 每个单元的 `run()`，`join_any`（任一完成即继续）→ `end_of_simulation_mechanism()`（`#100; $finish`）。

#### 4.6.3 源码精读

环境类的构造与编排：

[_hdl/VE_sv/ve_AES_env.sv:L24-L46](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/VE_sv/ve_AES_env.sv#L24-L46) —— `AesEnvironment` 持有 `AlreadyComputed computed_val`、单元队列 `units[$]`、参考模型 `aes`、代理 `mix_column_inst`、两个 modport 接口句柄。`new()` 里先建黄金表，再用它建参考模型 `aes`，再把 `aes` 引用传给代理，使二者共用同一份「正确答案」。这就是面向对象验证平台的典型连线。

环境的并发启动与收尾：

[_hdl/VE_sv/ve_AES_env.sv:L48-L64](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/VE_sv/ve_AES_env.sv#L48-L64) —— `run()` 遍历 `units`，`fork`（用 `automatic int k=i` 捕获下标，避免经典并发陷阱）逐个 `units[k].run()`，`join_any` 后调 `end_of_simulation_mechanism()`。⚠️ 注意 `join_any` 的语义：**任一**单元结束就继续，再叠加固定的 `#100` 收尾，意味着仿真在「第一个单元完成 + 100 ns」时强制 `$finish`；参考模型的 `run()` 几乎瞬时完成（只有末尾 `#10`），所以仿真很可能在代理跑完 21 组之前就结束了。这是一个「能跑、但不严谨」的演示级调度，做正式回归时应当改成 `join_none`+显式等待或用事件同步。

程序块入口：

[_hdl/VE_sv/test_program.sv:L23-L34](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/VE_sv/test_program.sv#L23-L34) —— `program main_program(drv, rcv)`：创建 `env`，先做两次 `env.aes.mul(...)` 打印调试，再 `env.run()` 正式启动。`program` 块让测试代码在 Re-active 区调度，减少与 DUT 的竞争。

顶层模块（默认未参与编译）：

[_hdl/VE_sv/ve_AES_top_mix_column.sv:L22-L57](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/VE_sv/ve_AES_top_mix_column.sv#L22-L57) —— `top` 例化 `MixColumns`（DUT）、`mix_column_intf mix_column_intf0()`、`main_program test(...)`。⚠️ 如前所述，它在 `ve_AES_Include.sv` 第 33 行被注释掉；要真正跑 SV 环境需要手动放开注释或单独编译此文件。文件里还残留被注释的旧接口例化（第 39~52 行）与一处把 `c0` 重复注释的笔误，印证草稿状态。

#### 4.6.4 代码实践（读懂 test_program 如何驱动 env）

**实践目标**：不实际综合，只做「源码阅读型实践」——画一张从 `test_program` 到 `agent` 比对的完整时序/调用链，并回答「环境到底验了什么」。

**操作步骤**：

1. 打开 `test_program.sv`，确认入口是 `initial begin ... env = new(...); env.run(); end`。
2. 顺着 `env.run()`（`ve_AES_env.sv`）进入 `fork...join_any`，找到它启动了哪几个单元（`aes` 与 `mix_column_inst`）。
3. 进入 `agent_MixColumn::run()`（`ve_AES_class_MixColumn.sv`），看清驱动 21 组激励 → `#10` → `check_mix_column_result()` 的循环。
4. 进入 `check_mix_column_result()`，确认它调用了 `aes.MixColumn(test_matrix)`（参考模型），再用 4 条 `assert` 与 DUT 输出比对。
5. 反向确认：DUT 是谁？回到 `ve_AES_top_mix_column.sv`，看到 `MixColumns MixColumns_DUT(...)`——即 `hdl/src/aes_mix_columns.v` 里那个 MixColumns。

**需要观察的现象（源码层面）**：激励由代理循环生成，期望值由参考模型查表算出，判对错由断言完成——三者职责清晰分离。

**预期结果**：你能画出下面这条链路，并得出结论「该 SV 环境当前实际验证的是 **MixColumns DUT 的列混淆结果**，而非完整 AES 加解密」：

```
test_program.initial
   └─ env.run()  (ve_AES_env)
         ├─ aes.run()          → 仅 MixColumn（Encrypt 被注释）
         └─ agent.run()        → 驱动 21 组 b0~b3
               └─ check()      → aes.MixColumn() 算期望 + assert 比对 DUT 的 a0~a3
```

> 由于该环境依赖 SV 的 `class`/`interface`/`program`/`assert`，开源 iverilog 对这些支持有限（iverilog 对 SV 面向对象与 program 块支持不完整），实际运行通常需要 **Vivado Simulator、Questa/ModelSim 或 VCS** 等商业仿真器。若本机只有 iverilog，建议以「源码阅读 + 画链路」为主，运行结果标注「待本地验证」。

#### 4.6.5 小练习与答案

**练习 1**：如果想让这个 SV 环境去验证完整的 AES 加密（而不只是 MixColumn），至少要改动哪两处？
**答案**：① 放开 `ve_AES_Core.sv` 第 292 行 `Encrypt(a, rk)` 及相关块注释，让参考模型跑完整加密；② 把 DUT 从 `MixColumns` 换成顶层 `aes_top`（或包一层让接口接 128 位明文/密钥/密文），并改写代理的驱动与比对以覆盖完整数据通路。

**练习 2**：`env.run()` 用 `join_any` 而不是 `join_all`，会带来什么风险？
**答案**：`join_any` 在任一单元结束就继续，叠加固定 `#100; $finish`，可能在代理跑完所有激励前就结束仿真，导致部分激励未被比对。应改用同步事件或 `join_all`（配合单独的看门狗超时）来保证完整性。

## 5. 综合实践

**任务**：把本讲两套验证资产串起来，给 AES 的「字节替换」做一次从单元到集成的双层验证设计。

请按以下步骤完成（允许只做源码层面的设计与阅读，不强求全部上机）：

1. **单元层**：复制 `tb_s_box.v` 到临时目录，修复实例重名（第二个 `sbox_e` → `sbox_d`），加 `$dumpfile/$dumpvars`，用 iverilog 编译运行，用 GTKWave 确认 `S[0x01]=0x7c`、`S[0x10]=0xca`、`S[0x02]=0x77`，并确认逆替换能还原输入。记录你看到的波形值。
2. **参考模型层**：打开 `ve_AES_AlreadyComputed.sv`，从 `S[]` 表（第 62 行起）手工查出上述三个输入对应的期望值，与你波形的 `data_out_e` 对照，确认「黄金表」与「硬件」一致。
3. **集成层设计**：仿照 `agent_MixColumn` 的写法，在纸上设计一个 `agent_SBox`：它应通过一个新的 `sbox_intf` 接口（8 位 `data_in`、1 位 `encrypt`、8 位 `data_out`）驱动 256 个遍历激励，并在 `check` 里调用 `aes.SubBytes`（或直接查 `computed_val.S/Si`）算期望值，用 `assert` 与 DUT 输出比对。写出这个代理类的 `run()` 与 `check()` 伪代码。
4. **批判性总结**：列出本仓库验证资产至少 3 处「草稿痕迹」（例如 `tb_s_box` 实例重名、`ve_AES_top_mix_column.sv` 被注释、`mul` 的 `else` 未赋值、`join_any` 提前结束），并说明每一处对「能否信任验证结论」的影响。

**交付物**：① 修好并跑通的单元 tb 波形截图或记录；② `agent_SBox` 的伪代码；③ 一份「草稿痕迹—影响」对照表。

> 若本机缺少 GTKWave/iverilog 或 SV 商业仿真器，第 1 步可降级为「对照黄金表手算三个值」，第 3 步保持纸面设计，并在交付物中标注「待本地验证」。

## 6. 本讲小结

- AES 核心有两套验证资产：`tb/` 是只施加激励、不自动判对的散装单元 testbench；`VE_sv/` 是用 `class/interface/program/assert` 搭成的面向对象验证环境，带参考模型与自检。
- `tb_gf_inv_8.v`、`tb_s_box.v` 例化 DUT 后用 `initial` 逐拍改输入，靠波形人眼判断；`tb_s_box.v` 存在实例重名笔误（两个 `sbox_e`），原样无法编译，须先修复。
- `VE_sv/` 的核心是一条「代理驱动激励 → 参考模型算期望值 → 立即断言比对」的链路：`AesCore` 是查表实现的黄金参考模型，`AlreadyComputed` 是它依赖的黄金数据表（**不是**覆盖率模块，本仓库无覆盖率采集代码）。
- 环境由 `AesEnvironment` 用 `BaseUnit` 队列统一编排，`test_program` 创建并启动它，`top` 例化真正被测的 DUT（当前是 `MixColumns`）。
- 诚实结论：该 SV 环境**目前只验证 MixColumns 一列**（参考模型 `run()` 里 `Encrypt` 被注释、DUT 是 `MixColumns`），且存在 `join_any` 提前结束、顶层被注释掉等草稿痕迹——它是一个框架完备但内容待充实的半成品，运行通常需商业 SV 仿真器。

## 7. 下一步学习建议

- **横向扩展验证范围**：以本讲的 `agent_SBox` 设计为起点，逐步为 `ShiftRows`、`KeyExpansion`、完整 `aes_top` 各写一个代理，把 `VE_sv` 升级成真正的端到端 AES 验证环境，并补上 `covergroup` 做功能覆盖率（补齐本仓库缺失的一环）。
- **补齐 u3 的软件侧**：u3-l4（软件驱动与自检程序）目前缺讲义，建议接着阅读 `ip_repo/.../drivers/.../AesCryptoCore_selftest.c`，理解处理器侧的「软自检」与本讲「硬仿真」如何互补——前者验证寄存器读写链路，后者验证算法数据通路。
- **向 Unit 4 切换范式**：下一单元进入 HLS（高层综合）的 2D 中值滤波，建议从 u4-l1 开始，体会「C 仿真即验证」这一完全不同的验证范式——在 HLS 里，同一个 C 函数既是实现也是参考模型，C 仿真天然自检。
- **进阶阅读**：若对 SV 验证方法论感兴趣，可对照本讲的极简结构去读 UVM（Universal Verification Methodology）的 `uvm_component/uvm_agent/uvm_env/uvm_test`，本仓库的 `BaseUnit/agent/env` 正是 UVM 这些概念的「迷你前身」。
