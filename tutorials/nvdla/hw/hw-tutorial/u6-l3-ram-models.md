# RAM 行为模型与综合模型

## 1. 本讲目标

NVDLA 是一个计算密集型加速器，但它的硅片面积大部分并不在计算逻辑上，而在**存储（SRAM）**上：卷积缓冲 CBUF 一个就有 512 KB，加上各引擎里的装配缓冲、命令队列、FIFO，全片要例化成百上千块小 RAM。这些 RAM 在 RTL 里既不能写成一行 `reg array`（综合工具无法把它映射成高效的 SRAM 宏单元），也不能直接写成某个具体工艺的 SRAM（那样就绑死代工厂了）。

本讲解决一个核心问题：**NVDLA 如何用「同一套源码」同时满足仿真、综合、不同代工厂这三种相互冲突的需求？**

读完本讲你应当能够：

1. 区分 `vmod/rams/model/`（行为 RAM 模型）与 `vmod/rams/synth/`（综合 RAM wrapper）的职责。
2. 读懂物理 RAM 宏单元名（如 `RAMDP_256X8_GL_M2_E2`、`RAMPDP_256X144_GL_M2_D2`）的命名规则。
3. 看懂一份 RAM 模型文件如何用 `RAM_INTERFACE` / `EMULATION` / `SYNTHESIS` 三个宏三刀切成「仿真全模型 / 仿真精简模型 / 综合黑盒接口」。
4. 读懂 `nv_ram_rws_*` 这类 wrapper 如何把一块「逻辑 RAM」拼装成若干「物理 RAM 宏单元」。
5. 追踪 CBUF 里的一个大缓冲到底落在哪些 RAM 模型上。

本讲承接 [u6-l2](u6-l2-fifo-vlibs-primitives.md) 关于 vlibs 库原语的讲解，并把视角从「标准单元原语」转向「SRAM 宏单元」这条更大的复用轴线。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 为什么 RAM 要单独建模

通用 RTL 仿真器（VCS、Verilator）和综合工具（Synopsys DC）对「存储」的期望完全不同：

| 角色 | 期望的 RAM 形态 |
|------|-----------------|
| 仿真器 | 一段可读可写的 `reg array`，最好还能注入故障、统计覆盖率、检查上电序列 |
| 综合工具 | 一个**黑盒**，端口签名固定，由代工厂提供的 SRAM 编译器生成实际电路 |
| 代工厂 | 一份工艺相关的 GDS，名字、端口、时序由 SRAM 编译器决定 |

如果直接把 `reg array` 写进 RTL，综合工具会把它综合成成千上万个触发器，面积和功耗都不可接受；如果直接写死某个代工厂的 SRAM，开源项目就无法独立存在。NVDLA 的做法是**把 RAM 抽象成两层**，让同一份 RTL 在三种语境下各取所需。

### 2.2 宏单元（Macro Cell）的概念

芯片里有两类基本电路实现方式：

- **标准单元（standard cell）**：NAND、D 触发器、MUX 等基本门，由标准单元库提供，综合工具像搭积木一样拼装（u6-l2 讲的 vlibs 多属此类）。
- **宏单元（macro cell）**：面积大、结构规则的电路块，如 SRAM、PLL、IO。它们不由标准单元拼出，而是由专门的**编译器**（如 SRAM 编译器）针对具体工艺生成一整块硬核（hard macro）。

SRAM 就是最典型的宏单元。本讲里的 `RAMDP_*`、`RAMPDP_*` 都是 SRAM 宏单元的「名字」，它们的真实电路由代工厂提供，NVDLA 仓库里只有**行为模型**（仿真用）和**端口接口**（综合用）。

### 2.3 行为模型与综合模型的分工

- **行为模型（behavioral model）**：用 Verilog 的 `reg array`、`always` 块描述存储行为，只为仿真服务。本讲在 `vmod/rams/model/`。
- **综合 wrapper**：对外暴露一个干净、稳定的端口（`clk/ra/re/dout/wa/we/di`），对内把这块逻辑 RAM 拼装成若干物理宏单元，并挂上 MBIST、扫描等 DFT（Design-for-Test）逻辑。本讲在 `vmod/rams/synth/`。

> 关键结论：**引擎 RTL（如 CBUF）只认识 wrapper；wrapper 认识物理宏单元；物理宏单元在仿真里由行为模型实现，在综合里由代工厂硬核替换。** 这就是「同一套源码，三种用途」的总架构。

## 3. 本讲源码地图

| 文件 | 目录 | 作用 |
|------|------|------|
| [RAMDP_256X8_GL_M2_E2.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/rams/model/RAMDP_256X8_GL_M2_E2.v) | rams/model | 物理宏单元 `RAMDP_256X8` 的行为模型：256 行 × 8 位双口 RAM，演示三刀切结构与仿真特性 |
| [RAMDP_16X256_GL_M1_E2.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/rams/model/RAMDP_16X256_GL_M1_E2.v) | rams/model | 物理宏单元 `RAMDP_16X256` 的行为模型：16 行 × 256 位双口 RAM，宽字浅深范例 |
| [nv_ram_rws_256x512.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/rams/synth/nv_ram_rws_256x512.v) | rams/synth | 256×512「逻辑 RAM」综合 wrapper，对外 1 读口 + 1 写口，对内拼 4 块物理宏单元 |
| [nv_ram_rws_16x256.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/rams/synth/nv_ram_rws_16x256.v) | rams/synth | 16×256「逻辑 RAM」wrapper，1:1 对应单块物理宏单元，并演示仿真预加载 |
| [NV_NVDLA_cbuf.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cbuf/NV_NVDLA_cbuf.v) | nvdla/cbuf | 卷积缓冲：例化 32 个 `nv_ram_rws_256x512`，是「RAM 实例映射」的活样本 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 行为 RAM 模型**：读懂 `rams/model/` 里的物理宏单元模型与三刀切结构。
- **4.2 综合 RAM wrapper**：读懂 `rams/synth/` 里的 `nv_ram_rws_*` 如何拼装宏单元。
- **4.3 RAM 实例映射**：以 CBUF 为例，把一块大缓冲从引擎一路追到物理宏单元。

### 4.1 行为 RAM 模型（vmod/rams/model）

#### 4.1.1 概念说明

`vmod/rams/model/` 存放的是各类 SRAM 物理宏单元的**行为模型**。命名遵循统一规则：

```
RAM[P]DP_<行数>X<位宽>_GL_M<mux>_E<n>   （或 _D<n>）
   │       │      │      │    │
   │       │      │      │    └─ 选项码（E/D，影响流水/读延迟，代工厂定义）
   │       │      │      └──── 多路复用比选项（M1/M2，面积与速度的权衡）
   │       │      └─────────── 通用/电平选项码
   │       └────────────────── 行数 × 每行位宽（直接对应存储容量）
   └──────────────────────── RAM 类型：DP=Dual Port 双口（独立读口+写口）
```

两个本讲范例：

- `RAMDP_256X8_GL_M2_E2`：256 行 × 8 位，双口（独立读时钟 CLK_R、写时钟 CLK_W）。
- `RAMDP_16X256_GL_M1_E2`：16 行 × 256 位，双口，浅深宽字。

`RAMDP` 与 `RAMPDP` 都是双口 SRAM 宏单元家族；`RAMPDP` 多见于被 wrapper 拼装的较大块（如 256×144、256×80），`RAMDP` 多见于独立使用的小块（如 16×256、256×8）。两者的**行为模型结构完全一致**，只是容量与选项不同。

> 术语：**双口（dual-port）**这里指「一个专用读口（RE/RADR/RD）+ 一个专用写口（WE/WADR/WD）」，两口可同时工作、各有自己的时钟（CLK_R/CLK_W）。它与「单口（single-port，读或写二选一）」相对。

#### 4.1.2 核心流程：一份文件，三种身份

行为模型最精巧的地方在于：**同一份 `.v` 文件，靠三个宏定义切成三种实现**。结构如下（伪代码）：

```
module RAMDP_256X8_GL_M2_E2 (CLK_R, CLK_W, RE, WE, RADR, WADR, WD, RD, SLEEP_EN, RET_EN, IDDQ, SVOP, ...);
  `ifndef RAM_INTERFACE            // (1) 没定义 RAM_INTERFACE 才有内容
    `ifndef EMULATION              // (2) 没定义 EMULATION：完整仿真模型
      `ifndef SYNTHESIS            // (3a) 没定义 SYNTHESIS：带断言/监控/故障注入的全模型
        ... clobber / power 断言 / MONITOR / FAULT_INJECTION ...
      `else                        // (3b) 定义了 SYNTHESIS：clobber 全置 0，避免 X 悲观
      `endif
      例化 RAM_BANK（含 vram 行为阵列）   // 仿真用行为存储
    `else                          // (2) 定义了 EMULATION：极简 reg 阵列模型
      ... reg array; 写时钟沿写入；读组合输出 ...
    `endif
  `endif                           // 定义了 RAM_INTERFACE：模块体为空，只剩端口壳
endmodule
```

三种身份对应三种使用场景：

| 宏定义状态 | 模块体内容 | 用途 |
|-----------|-----------|------|
| 全都不定义 | 完整行为模型（断言 + 监控 + 故障注入 + 行为阵列） | VCS 功能仿真（默认） |
| `+define+EMULATION` | 极简 `reg array` 模型 | Verilator / FPGA 仿真，追求速度 |
| `+define+RAM_INTERFACE` | **空壳**（只有端口声明） | 综合：交由代工厂 SRAM 宏替换；或换用外部 RAM 模型 |
| `+define+SYNTHESIS`（非 EMULATION） | 行为阵列 + clobber=0 | 门级仿真，避免 X 传播 |

#### 4.1.3 源码精读：RAMDP_256X8_GL_M2_E2

先看端口。双口 RAM 的端口是「读一组、写一组」对称的：

[RAMDP_256X8_GL_M2_E2.v:38-47](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/rams/model/RAMDP_256X8_GL_M2_E2.v#L38-L47) 声明了读口 `RE/RADR/RD`、写口 `WE/WADR/WD`，以及电源管理口 `SLEEP_EN/RET_EN/IDDQ` 和 `SVOP`。注意读、写各有独立时钟 `CLK_R`、`CLK_W`，这正是「双口」的体现。

接着是三刀切的守卫与物理尺寸声明：

[RAMDP_256X8_GL_M2_E2.v:27-36](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/rams/model/RAMDP_256X8_GL_M2_E2.v#L27-L36) 用三层 `ifndef` 包住仿真专属内容，并用 `localparam phy_rows=128, phy_cols=16` 记录物理阵列的物理排布（256×8 的逻辑容量对应 128 行 × 16 列的物理 bit 阵列，因为 8 位数据每位的物理列各占一列）。

由于模块端口是「位展平」的（`RADR_7..RADR_0` 每位一根线），模型先把它们拼回总线：

[RAMDP_256X8_GL_M2_E2.v:52-59](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/rams/model/RAMDP_256X8_GL_M2_E2.v#L52-L59) 把 `RADR_*`/`WADR_*` 拼成 8 位 `RA`/`WA`，`WD_*` 拼成 `WD`，`SLEEP_EN_*` 拼成总线，便于行为描述。

行为存储的核心在叶子模块 `vram_RAMDP_256X8_GL_M2_E2`，它是一个二维行为阵列：

[RAMDP_256X8_GL_M2_E2.v:904-917](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/rams/model/RAMDP_256X8_GL_M2_E2.v#L904-L917) 定义参数 `words=256, bits=8, addrs=8`（与文件名 256×8 对应），声明读写口。真正的存储体是：

[RAMDP_256X8_GL_M2_E2.v:1030](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/rams/model/RAMDP_256X8_GL_M2_E2.v#L1030) `reg [bits-1:0] array[0:words-1];` —— 即 256 个 8 位寄存器组成的阵列，这就是「行为 RAM」的真身。

写入与读出逻辑用 `generate` 按位展开，模拟位写使能（bit write enable）：

[RAMDP_256X8_GL_M2_E2:1045-1073](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/rams/model/RAMDP_256X8_GL_M2_E2.v#L1045-L1073) 按位 `always` 块在写时钟有效时把 `w0_din[idx]` 写进 `array[w0_addr][idx]`，支持位级掩码 `w0_bwe`。

这套「位展平端口 + 行为阵列 + 位级写使能」是所有 `RAM[P]DP_*` 模型的统一骨架，换容量只改 `words/bits/addrs` 三个参数。对照看宽字范例 `RAMDP_16X256`：

[RAMDP_16X256_GL_M1_E2.v:641-644](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/rams/model/RAMDP_16X256_GL_M1_E2.v#L641-L644) 参数为 `words=16, bits=256, addrs=4`，与文件名 16×256 对应；同一套骨架，容量由参数决定。

仿真专属的高级特性都集中在「全模型」分支里。例如电源上电序列断言：

[RAMDP_256X8_GL_M2_E2.v:194-227](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/rams/model/RAMDP_256X8_GL_M2_E2.v#L194-L227) 在 `ifdef NV_RAM_ASSERT` 下用 `nv_assert_never` 检查 SLEEP_EN/RET_EN 切换必须留出 2 个空拍，违反则报 `Power-S1.1` 等错误。这些只在仿真生效，综合时被 `RAM_INTERFACE`/`SYNTHESIS` 守卫整段剔除。

而 EMULATION 分支则是另一副面孔——极简：

[RAMDP_256X8_GL_M2_E2.v:714-759](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/rams/model/RAMDP_256X8_GL_M2_E2.v#L714-L759) 注释明写 "Simple emulation model without MBIST, SCAN or REDUNDANCY"：一个 `reg [7:0] array[0:255]`，写口在 `negedge CLK_W` 写入，读口组合输出，没有任何电源/断言/扫描逻辑，专门为 Verilator 等高速仿真服务。

#### 4.1.4 代码实践：观察三刀切

**实践目标**：亲眼确认同一份模型文件在不同宏下的形态差异。

**操作步骤**：

1. 打开 [RAMDP_256X8_GL_M2_E2.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/rams/model/RAMDP_256X8_GL_M2_E2.v)。
2. 搜索 `ifndef RAM_INTERFACE`、`ifndef EMULATION`、`ifndef SYNTHESIS` 三处守卫，分别记下它们的行号。
3. 对照本讲 4.1.2 的「三种身份」表格，确认每段逻辑落在哪个分支。
4. 找到 EMULATION 分支里的 `reg [7:0] array[0:255]`，对比非 EMULATION 分支里 `vram` 模块的 `reg [bits-1:0] array[0:words-1]`，体会「同一块存储，两种描述精度」。

**需要观察的现象**：守卫是嵌套的（`RAM_INTERFACE` 最外、`EMULATION` 次外、`SYNTHESIS` 最内），不是平铺的 `ifdef/elsif`。

**预期结果**：你能用一句话说清——「定义 `RAM_INTERFACE` 时整段模块体被掏空；定义 `EMULATION` 时只留极简 reg 阵列；都不定义时是完整仿真模型」。具体宏在构建时由谁传入属于构建系统细节，**待本地验证**（可在仿真命令行加 `+define+EMULATION` 对比波形行为）。

#### 4.1.5 小练习与答案

**练习 1**：`RAMDP_256X8_GL_M2_E2` 的逻辑容量是多少 bit？它的物理阵列 `phy_rows × phy_cols` 又是多少？两者为何不等？

> **答案**：逻辑容量 = 256×8 = 2048 bit。物理阵列 = 128×16 = 2048 bit。两者总 bit 数相同，只是排布不同：物理上每位数据各占一列，故 8 位数据展开成 16 列（含冗余/校验列），行数相应减半。

**练习 2**：为什么仿真要用「位级写使能（bit write enable, `w0_bwe`）」而不是整字写？

> **答案**：真实 SRAM 宏单元支持按字节/位掩码写入（读改写时只改部分位）。行为模型若只支持整字写，就无法精确复现代工厂宏的字节使能行为，掩盖潜在 bug；故模型用 `generate` 按位展开写逻辑，与硬件一致。

**练习 3**：`RAMDP_16X256_GL_M1_E2` 的 `M1` 与 `RAMDP_256X8_GL_M2_E2` 的 `M2` 有何不同含义？

> **答案**：`M1/M2` 是代工厂定义的多路复用（mux）选项码，反映地址译码的复用比，影响宏的面积与速度权衡（复用比越高面积越小、速度越慢）。具体数值语义由 SRAM 编译器文档规定，开源仓库只把名字当稳定接口使用。

### 4.2 综合 RAM wrapper（vmod/rams/synth）

#### 4.2.1 概念说明

引擎 RTL 不直接例化 `RAMDP_*` 这类物理宏单元，而是例化一层 **wrapper**（`nv_ram_rws_*`）。wrapper 做三件事：

1. **统一接口**：对外只暴露 `clk, ra, re, dout, wa, we, di, pwrbus_ram_pd` 这样一组干净端口，屏蔽物理宏的位展平端口与电源细节。
2. **容量拼装**：一块「逻辑 RAM」（如 256×512）往往没有同尺寸的单个物理宏，wrapper 用多块小宏拼出所需容量。
3. **挂接 DFT**：在 wrapper 里挂上 MBIST（内存内建自测）、扫描等可测性设计逻辑，让物理宏可被自动测试。

wrapper 命名规则：`nv_ram_<类型>_<深度>x<宽度>`。本讲两个范例都属 `rws` 类型：

- `nv_ram_rws_256x512`：256 行 × 512 位的逻辑 RAM，对外一个读口 + 一个写口（共用 `clk`）。
- `nv_ram_rws_16x256`：16 行 × 256 位的逻辑 RAM。

> 术语：`rws` 表示「1 个读口 + 1 个写口」（1R1W），两口可同时工作但共用同一时钟 `clk`，区别于双时钟双口的物理宏（CLK_R/CLK_W 分离）。其它前缀：`rwsp` 表示单口（1RW，读或写二选一），`rwst/rwsthp` 为带流水/保留的变体——具体语义以源码端口为准。

#### 4.2.2 核心流程：wrapper 的两层结构

每个 `nv_ram_rws_*` wrapper 实际由两个文件组成：

```
nv_ram_rws_<D>x<W>.v          // 外壳：声明干净端口，把 DFT 信号钳位到默认值
   └── nv_ram_rws_<D>x<W>_logic.v   // 内核：挂 MBIST/扫描，例化物理宏单元
```

外壳做的事很机械：为每个 DFT/MBIST 信号（如 `mbist_*`、`scan_en`、`shiftDR`）用 `NV_BLKBOX_SRC0`（拉 0）和 `AN2D4PO4`（与 DFT_clamp）钳到安全默认值，再把用户端口（`ra/re/wa/we/di/dout`）原样接到内核。这层存在的意义是**把测试逻辑与用户视角隔离**——引擎作者只看到 `clk/ra/re/dout/wa/we/di`，不必关心 MBIST。

内核 `_logic.v` 才是重点，它把逻辑位宽切成若干「片（piece）」，每片对应一块物理宏，最后逐片例化。容量关系满足：

\[
\text{逻辑位宽 } W \;=\; \sum_{i} \text{第 } i \text{ 块物理宏的位宽}
\]

例如 256×512 的逻辑 RAM，若只有 144 位和 80 位的物理宏可用，就切成：

\[
512 = 144 + 144 + 144 + 80
\]

即 3 块 `RAMPDP_256X144_GL_M2_D2` 加 1 块 `RAMPDP_256X80_GL_M2_D2`，所有块共享同一组地址（深度都是 256），只是各自负责不同的数据位段。

#### 4.2.3 源码精读：nv_ram_rws_256x512（拼装型）

先看 wrapper 外壳的干净端口——这正是引擎 RTL 看到的样子：

[nv_ram_rws_256x512.v:12-32](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/rams/synth/nv_ram_rws_256x512.v#L12-L32) 声明 `clk, ra[7:0], re, dout[511:0], wa[7:0], we, di[511:0], pwrbus_ram_pd[31:0]`。地址 8 位 → 256 深，数据 512 位，与文件名 256×512 完全对应。注意只有一个 `clk`，读口（ra/re/dout）与写口（wa/we/di）共用它。

外壳文件的头部注释直接写明了拼装方案：

[nv_ram_rws_256x512.v:36](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/rams/synth/nv_ram_rws_256x512.v#L36) 注释 "This wrapper consists of : 4 Ram cells: RAMPDP_256X144_GL_M2_D2 ×3 + RAMPDP_256X80_GL_M2_D2 ×1"，正好印证上面的 512=144×3+80 切分。

外壳把所有 DFT 信号钳位后，例化内核：

[nv_ram_rws_256x512.v:725-748](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/rams/synth/nv_ram_rws_256x512.v#L725-L748) 例化 `nv_ram_rws_256x512_logic`，把用户的 `ra/re/wa/we/di/dout/clk` 以及钳位后的 `mbist_*`/`scan_*` 信号一并接入。

进入内核，先看电源控制如何从 `pwrbus_ram_pd` 解出：

[nv_ram_rws_256x512_logic.v:129-130](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/rams/synth/nv_ram_rws_256x512_logic.v#L129-L130) `wire [7:0] sleep_en = pwrbus_ram_pd[7:0]; wire ret_en = pwrbus_ram_pd[8];` —— wrapper 把电源岛控制压缩进一个 32 位 `pwrbus_ram_pd` 总线，低 8 位是各 bank 的睡眠使能，bit8 是保持使能，再下发给各物理宏的 `SLEEP_EN`/`RET_EN`。

内核按位段切分逻辑数据，每段声明一组片内连线。第一片（数据位 [143:0]）：

[nv_ram_rws_256x512_logic.v:586-626](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/rams/synth/nv_ram_rws_256x512_logic.v#L586-L626) 注释 "START PIECE … RAMPDP_256X144_GL_M2_D2 … Data Bit range: [143:0] (144 bits)"，把 `muxed_Di_w0[143:0]` 接到该片写数据、`muxed_Ra_r0/muxed_Wa_w0` 接到地址、`we/re` 接到使能。

随后真正例化第一块物理宏：

[nv_ram_rws_256x512_logic.v:793-819](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/rams/synth/nv_ram_rws_256x512_logic.v#L793-L819) 例化 `RAMPDP_256X144_GL_M2_D2 ram_Inst_256X144_0_0`，把片内 `wa_0_0/ra_0_0/Wdata_0_0/ramDataOut_0_0` 以及电源 `SLEEP_EN/RET_EN/IDDQ/SVOP` 接到物理宏的位展平端口。这就是 wrapper 与行为模型的握手点：这里的 `RAMPDP_256X144_GL_M2_D2` 在仿真里解析为 `rams/model/RAMPDP_256X144_GL_M2_D2.v` 的行为模型，在综合里则被替换为代工厂硬核。

内核里同样的 piece 结构重复 4 次（`_0_0`/`_0_144`/`_0_288` 为 144 位片、`_0_432` 为 80 位片），合起来覆盖 512 位数据宽度。

#### 4.2.4 源码精读：nv_ram_rws_16x256（1:1 型）

并非所有 wrapper 都需要拼装。当逻辑尺寸恰好等于某个物理宏时，wrapper 退化为 1:1 直连：

[nv_ram_rws_16x256.v:12-32](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/rams/synth/nv_ram_rws_16x256.v#L12-L32) 端口为 `clk, ra[3:0], re, dout[255:0], wa[3:0], we, di[255:0], pwrbus_ram_pd[31:0]`（4 位地址→16 深，256 位数据）。

[nv_ram_rws_16x256.v:36](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/rams/synth/nv_ram_rws_16x256.v#L36) 注释 "This wrapper consists of : 1 Ram cells: RAMDP_16X256_GL_M1_E2"——只用一块物理宏，无需拼装。

这种 1:1 wrapper 还多带了一组仿真专用的**内存预加载** task，是上一类没有的便利：

[nv_ram_rws_16x256.v:741-760](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/rams/synth/nv_ram_rws_16x256.v#L741-L760) 在 `ifndef SYNTHESIS` 下提供 `init_mem_val` / `init_mem_commit` task，用影子数组 `shadow_mem` 暂存预加载值，再统一写入物理宏的行为阵列。测试平台可在仿真启动前把权重/特征图直接灌进 RAM，省去逐拍搬运。

#### 4.2.5 代码实践：读懂一个 wrapper 的拼装

**实践目标**：验证 `nv_ram_rws_256x512` 的「4 块拼 512 位」与 `nv_ram_rws_16x256` 的「1 块直连」两种形态。

**操作步骤**：

1. 在 [nv_ram_rws_256x512.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/rams/synth/nv_ram_rws_256x512.v) 顶部读第 36 行注释，记下它声明的 4 块物理宏。
2. 打开 [nv_ram_rws_256x512_logic.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/rams/synth/nv_ram_rws_256x512_logic.v)，搜索 `START PIECE`，统计共有几个 piece、各自覆盖的数据位段（注释里有 `Data Bit range:`）。
3. 核对：3 个 piece 标注 `RAMPDP_256X144_GL_M2_D2`（位段 144 位），1 个标注 `RAMPDP_256X80_GL_M2_D2`（位段 80 位），合计 512 位。
4. 对比 [nv_ram_rws_16x256.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/rams/synth/nv_ram_rws_16x256.v) 第 36 行注释，确认它只有 1 块 `RAMDP_16X256_GL_M1_E2`。

**需要观察的现象**：拼装型 wrapper 的 `_logic` 文件里，每个 piece 的地址线都来自同一组 `muxed_Wa_w0/muxed_Ra_r0`（深度一致），只有数据位段不同。

**预期结果**：你能画出 512 位数据的「切片条带图」——[143:0]、[287:144]、[431:288] 各占一条 144 位片，[511:432] 占一条 80 位片，四条并排共享地址。1:1 型则只有一条整宽条带。

#### 4.2.6 小练习与答案

**练习 1**：为什么 wrapper 要把读口、写口合到一个 `clk`，而底层物理宏却分 `CLK_R`/`CLK_W`？

> **答案**：wrapper 面向引擎设计者，提供「同域 1R1W」的简单视图；底层物理宏保留双时钟是为了在 wrapper 内核里能灵活接同域或跨域。本仓库 `rws` 类型在内核里把 `CLK_R`/`CLK_W` 都接到同一个用户 `clk`，从而对外呈现单时钟。

**练习 2**：`pwrbus_ram_pd[31:0]` 这个名字暗示了什么？为何要把电源控制塞进一个数据总线？

> **答案**：`pwrbus` = power bus，`ram_pd` = RAM power-down。把每个 RAM 的睡眠/保持使能统一收进一条总线，是为了让 SoC 顶层（电源管理）能批量、规整地控制全片成百上千块 RAM 的开关，便于综合时统一约束、降低布线拥塞。本 wrapper 用到 `[7:0]`=sleep_en、`[8]`=ret_en，其余位被 `NV_BLKBOX_SINK` 吸收。

**练习 3**：如果某天代工厂提供了 256×256 的新物理宏，`nv_ram_rws_256x512` 的拼装会怎样变化？

> **答案**：只需把内核里的 4 片改成 2 片 256×256（512=256×2），外壳端口与引擎 RTL 完全不动。这正是 wrapper 抽象的价值——**换工艺不改引擎**。

### 4.3 RAM 实例映射：从引擎到物理宏

#### 4.3.1 概念说明

前两节分别讲了「物理宏的行为模型」和「逻辑 RAM 的 wrapper」。本节把它们串起来：以卷积缓冲 CBUF 为活样本，看一块真实的大缓冲如何**层层下钻**到物理宏单元，并在仿真/综合两种语境下分别落在什么模型上。

回忆 [u3-l3](u3-l3-cbuf-convolution-buffer.md)：CBUF 是卷积核心里夹在 CDMA（写）与 CSC（读）之间的 512 KB 片上 SRAM，由 32 块小 RAM 组成（16 bank × 2 column）。这 32 块小 RAM 正是本节要追的实例。

#### 4.3.2 核心流程：三级下钻

CBUF 的 RAM 映射是一条三层调用链：

```
NV_NVDLA_cbuf.v                  （引擎层）
   └─ 例化 nv_ram_rws_256x512 ×32      （wrapper 层，rams/synth）
        └─ 例化 nv_ram_rws_256x512_logic
             └─ 例化 RAMPDP_256X144_GL_M2_D2 ×3 + RAMPDP_256X80_GL_M2_D2 ×1
                  （物理宏层）
                       ├─ 仿真：rams/model/RAMPDP_*.v 行为模型
                       └─ 综合：代工厂 SRAM 硬核
```

单块容量核算：

\[
\text{单块逻辑容量} = 256 \times 512 = 131\,072 \text{ bit}
\]

\[
\text{CBUF 总容量} = 32 \times 131\,072 = 4\,194\,304 \text{ bit} = 512 \text{ KB}
\]

与 u3-l3 给出的 512 KB 完全吻合——这是验证映射正确性的硬约束。

#### 4.3.3 源码精读：CBUF 的 RAM 例化

CBUF 在文件末尾集中例化全部 32 块 RAM，开头有醒目注释：

[NV_NVDLA_cbuf.v:3094-3107](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cbuf/NV_NVDLA_cbuf.v#L3094-L3107) 注释 "Instance RAMs"，随后例化第一块 `nv_ram_rws_256x512 u_cbuf_ram_bank0_column0`，端口连接 `clk=nvdla_core_clk`、`ra=cbuf_ra_b0c0[7:0]`、`re`、`dout=cbuf_rdat_b0c0[511:0]`、`wa`、`we`、`di`、`pwrbus_ram_pd[31:0]`。注意引擎只看到 wrapper 的 8 个干净端口，完全不接触物理宏的位展平信号。

实例命名规律 `u_cbuf_ram_bank<N>_column<C>` 清楚地反映了 16 bank × 2 column 的组织：bank0~bank15，每 bank 下 column0/column1，共 32 块。全树共 32 个同样形态的例化（见 [NV_NVDLA_cbuf.v:3098-3470](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cbuf/NV_NVDLA_cbuf.v#L3098-L3470)）。

把这条链补全：CBUF 例化的 `nv_ram_rws_256x512` 就是 4.2.3 讲的拼装 wrapper；它内部例化的 `RAMPDP_256X144_GL_M2_D2`、`RAMPDP_256X80_GL_M2_D2` 在仿真里由 `vmod/rams/model/` 下的同名行为模型实现（这两个文件确实存在），在综合里被代工厂 SRAM 硬核替换。

#### 4.3.4 代码实践：追踪一块 CBUF RAM 的两副面孔（本讲主任务）

**实践目标**：找到 CBUF 中使用的 RAM 类型，分别说明它在仿真与综合下被替换成什么模型。

**操作步骤**：

1. **定位引擎用到的 RAM 类型**。在 [NV_NVDLA_cbuf.v:3098-3107](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cbuf/NV_NVDLA_cbuf.v#L3098-L3107) 看到 CBUF 例化的是 `nv_ram_rws_256x512`（rams/synth 的 wrapper）。
2. **看 wrapper 拼了哪些物理宏**。在 [nv_ram_rws_256x512.v:36](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/rams/synth/nv_ram_rws_256x512.v#L36) 读到它由 `RAMPDP_256X144_GL_M2_D2 ×3 + RAMPDP_256X80_GL_M2_D2 ×1` 组成。
3. **确认物理宏的行为模型存在**。在 `vmod/rams/model/` 下应能找到 `RAMPDP_256X144_GL_M2_D2.v` 与 `RAMPDP_256X80_GL_M2_D2.v`（本仓库确实存在）。
4. **理解综合替换**。物理宏名在综合时对应代工厂 SRAM 编译器产出的硬核；行为模型文件靠 `RAM_INTERFACE` 宏在综合时退化为空壳（见 4.1.2）。

**需要观察的现象**：从引擎到物理宏是一条「`nv_ram_rws_256x512` → `_logic` → `RAMPDP_*`」的单向调用链，没有任何反向依赖；物理宏模型文件不引用任何 NVDLA 专属模块。

**预期结果（参考答案）**：

- CBUF 每块 RAM 的逻辑类型是 **`nv_ram_rws_256x512`**（256 深 × 512 位，1R1W 单时钟）。
- **仿真下**：`nv_ram_rws_256x512`（wrapper 外壳）→ `nv_ram_rws_256x512_logic`（内核，含 MBIST/扫描钳位）→ 4 块物理宏 `RAMPDP_256X144_GL_M2_D2 ×3` + `RAMPDP_256X80_GL_M2_D2 ×1`，每块物理宏由 `vmod/rams/model/` 下的同名 `.v` 行为模型实现（默认全模型，或 `+define+EMULATION` 走极简模型）。
- **综合下**：同一份 wrapper 经 `+define+RAM_INTERFACE`（或作为黑盒）处理后，物理宏 `RAMPDP_*` 不再展开成行为阵列，而是由综合工具（DC）映射到代工厂 SRAM 硬核；仿真专属的电源断言、监控、故障注入、影子预加载 task 全部被宏守卫剔除。

具体仿真/综合命令由 `verif/sim`、`syn` 等构建流程驱动，**待本地验证**（例如可在仿真里打开波形，观察 `u_cbuf_ram_bank0_column0` 内部是否展开出 `ram_Inst_256X144_0_0` 等子实例）。

#### 4.3.5 小练习与答案

**练习 1**：若把 CBUF 单块 RAM 从 `nv_ram_rws_256x512` 换成两块 `nv_ram_rws_256x256`，CBUF 总容量和引擎行为会变吗？

> **答案**：总容量不变（仍 512 KB），但引擎 RTL 必须改写例化与地址/数据连线（512 位数据要拆成两个 256 位口、地址译码也要改）。这正是 wrapper 存在的意义——保持 `nv_ram_rws_256x512` 这个逻辑接口稳定，让容量拼装细节对引擎透明。

**练习 2**：Verilator 路径（见 [u7-l4](u7-l4-verilator-path.md)）默认会用到 `rams/model` 还是 `rams/synth`？

> **答案**：两者都用。`verilator.f` 把 `vmod/rams/synth` 列为 include 目录（取 wrapper），又显式 `-v` 列出大量 `vmod/rams/model/RAMPDP_*.v`（取物理宏的行为模型）。Verilator 倾向于搭配 `+define+EMULATION` 走极简模型以提速，具体以构建配置为准。

**练习 3**：为什么 `rams/model` 和 `rams/synth` 在 `build.config` 里属于同一个 sandbox `vmod_rams`？

> **答案**：因为它们必须一起编译——wrapper（synth）引用物理宏（model），物理宏的模块名由 wrapper 例化解析。把两者放进同一 sandbox `vmod_rams`，让 tmake 把它们当成一个构建单元统一处理，保证模块定义在引擎（如 cbuf）编译前就绪。详见 [u1-l3](u1-l3-build-system-toolchain.md)。

## 5. 综合实践

把本讲三个模块串成一个端到端追踪任务。

**任务**：任选 CBUF 之外的一个引擎（如 CDMA 的 `shared_buffer`、CACC 的 `assembly_buffer`、或 MCIF 的命令队列 `cq`），完成它的「RAM 映射表」。

**步骤**：

1. 在该引擎的 `.v` 文件里搜索 `nv_ram_` 开头的实例，记下它用的是哪一类 wrapper（如 `nv_ram_rws_*`、`nv_ram_rwsp_*`、`nv_ram_rwst_*`），以及例化深度与位宽。
2. 打开对应的 `vmod/rams/synth/<wrapper>.v`，读第 36 行附近的 "consists of … Ram cells" 注释，列出它拼装了哪几块物理宏。
3. 在 `vmod/rams/model/` 下确认这些物理宏的行为模型文件是否存在；任选一个，记录它的 `words/bits/addrs` 参数与三刀切守卫位置。
4. 仿照 4.3.4，写一句话结论：「该引擎的 X 缓冲在仿真下 = wrapper → 物理宏行为模型；在综合下 = wrapper → 代工厂 SRAM 硬核」。

**自检**：你选的缓冲容量应当与该引擎的设计文档/讲义一致（例如若选 CACC assembly_buffer，其累加位宽应与 [u3-l6](u3-l6-cacc-accumulator.md) 讲的 int8/int16/fp 精度路径对应）。如对不上，说明你追踪的实例不是主数据缓冲，换一个再试。

## 6. 本讲小结

- NVDLA 把 RAM 抽象成两层：`vmod/rams/model/` 存物理宏单元（`RAM[P]DP_*`）的**行为模型**，`vmod/rams/synth/` 存 `nv_ram_*` **综合 wrapper**；引擎 RTL 只例化 wrapper。
- 一份行为模型文件靠 `RAM_INTERFACE` / `EMULATION` / `SYNTHESIS` 三个宏切成三种身份：完整仿真模型、极简仿真模型、综合空壳接口——同一份源码服务仿真与综合。
- wrapper 对外暴露干净端口（`clk/ra/re/dout/wa/we/di/pwrbus_ram_pd`），对内把逻辑 RAM 按位段拼成若干物理宏（如 256×512 = 144×3 + 80），并挂接 MBIST/扫描 DFT 逻辑。
- 物理宏名 `RAM[P]DP_<行>x<位>_GL_M<n>_E/D<n>`：行列乘积即容量，`M/E/D` 是代工厂选项码（复用比、流水/读延迟）。
- CBUF 的 512 KB 由 32 块 `nv_ram_rws_256x512` 组成，每块下钻到 3× `RAMPDP_256X144_GL_M2_D2` + 1× `RAMPDP_256X80_GL_M2_D2`，仿真用行为模型、综合用代工厂硬核——总容量 32×256×512=4 194 304 bit 与设计完全吻合。
- 这套「wrapper + 行为模型 + 宏守卫」的分层，是 NVDLA 能做到「换工艺不改引擎 RTL、同一源码跑仿真与综合」的根本原因。

## 7. 下一步学习建议

- **横向铺开**：用本讲 4.3 的方法，把 `vmod/rams/synth/` 里其它 wrapper（如 `nv_ram_rwsp_*` 单口系列、`nv_ram_rwst_*` 流水系列）扫一遍，归纳 NVDLA 全片共有多少种逻辑 RAM 形态、各自用在哪些引擎。
- **纵向深入综合**：进入 [u8-l3](u8-l3-synthesis-flow.md) 综合流程，看 Synopsys DC 如何通过 SDC 与工艺库把这里的 `RAMPDP_*` 物理宏替换为真实 SRAM 硬核、`pwrbus_ram_pd` 如何参与电源域约束。
- **接续横切基础设施**：本讲讲完「SRAM 宏单元」这条复用轴线后，[u6-l4](u6-l4-floating-point-units.md) 将转向另一类宏单元——浮点运算单元（HLS_fp17/fp32），它们同样是「可复用、跨引擎、有仿真与综合双重身份」的积木。
- **回到验证视角**：学完 [u7-l1](u7-l1-traceplayer-testbench.md) trace-player 测试平台后，可回头验证 4.3.4 的追踪——在波形里确认 CBUF 的 wrapper 确实展开成 4 块物理宏子实例。
