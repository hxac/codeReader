# 16×16 乘法器与 T/P 寄存器

## 1. 本讲目标

本讲聚焦 IKA32010 的**乘法通路**：它如何在一个 DSP 机器周期内完成一次有符号 16×16→32 乘法，以及乘积如何参与累加。学完后你应当能够：

- 说清 `IKA32010_multiplier` 子模块的端口、内部锁存与「每个 EMUCLK 边沿都采样」的设计，并理解它为何会被综合工具映射为 FPGA 的 DSP 块。
- 说清 T 寄存器（`reg_t`）与 P 寄存器（`reg_p`）的物理位置、加载时序，以及 P 寄存器如何经 ALU 端口 B 进入累加器。
- 说清 `mul_op1_source_sel` 如何在「RAM 数据（MPY）」与「立即数（MPYK）」之间选择第二操作数，并指出立即数路径上值得在仿真里验证的一个细节。
- 把 LT / MPY / MPYK / APAC / PAC / SPAC / LTA / LTD 这些指令串成一条「加载 T → 乘入 P → 加/减进 ACC」的乘加流水线，并解释为什么 LT 与 MPY 必须分成两条指令。

本讲承接 [u2-l1 内部写总线 `reg_wrbus`](u2-l1-internal-write-bus.md) 与 [u2-l7 ALU/移位器/累加器](u2-l7-alu-shifters-accumulator.md)：乘法器的两个操作数都来自 `reg_wrbus` 汇流，乘积 `reg_p` 又经 ALU 端口 B 汇入累加器。如果你对 `reg_wrbus`、`cyc_ncen`、ALU 端口 B 的 `alu_pbsel` 还不熟悉，建议先读那两讲。

## 2. 前置知识

- **定点乘法与符号扩展**：两个 *n* 位有符号数相乘，结果是 2*n* 位有符号数。Verilog 里只要把操作数声明成 `signed`，用 `*` 就能直接得到正确的有符号乘积；若把无符号 wire 硬塞进乘法，正数结果可能恰好正确，但负数会出错。本讲子模块正是用 `reg signed` 来保证有符号语义。
- **寄存输出与流水线**：真实的硬件乘法器往往是「拍入操作数 → 下一拍出结果」的流水线结构。IKA32010 的乘法器内部就多了一级 `op0_latch/op1_latch`，使得「结果」比「操作数」晚一个锁存动作出现。
- **`cyc_ncen` 与「每沿采样」两种节拍**：前面几讲里，PC、栈、ALU 写回等状态都只在 `cyc_ncen`（`cyclecntr==3`，即一个机器周期的最后一个 EMUCLK）更新。但本讲的乘法器子模块**不带 `i_CEN`**，它在 `i_MUL_EN` 为高的每一个 `i_EMUCLK` 上升沿都采样。理解这两种节拍的差别是本讲的关键之一。
- **FPGA 的 DSP 块**：Altera/Intel 与 Xilinx 的 FPGA 内部都有专门的乘加硬核单元（Altera 叫 DSP block / 9 位乘法器元件，Xilinx 叫 DSP48）。综合工具会把 `signed` 乘法自动打包进这些硬核，比用查找表拼乘法器快得多、省资源得多。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/IKA32010.sv](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv) | 顶层模块。本讲关注三段：顶部「Multiplier registers」区（`reg_t`/`reg_p`/`mul_op1_source_sel` 与子模块例化）、ALU 例化里 `reg_p` 如何进入端口 B、以及文件末尾的 `IKA32010_multiplier` 子模块本体。微码块里还有 8 条乘法器类指令的译码。 |
| [src/IKA32010_mnemonics.sv](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_mnemonics.sv) | 常量字典。本讲用到 `MUL_OP1_SOURCE_IMM/RAM`（操作数来源）与 `ALU_SOURCE_MUL`（ALU 端口 B 选择乘法器）。 |

本讲不涉及 ALU 内部运算细节（见 u2-l7）与总线控制器（见 [u2-l3](u2-l3-bus-controller.md)），只把它们当成「P 寄存器的消费者」。

## 4. 核心概念与源码讲解

### 4.1 IKA32010_multiplier 子模块：有符号 16×16→32 乘法

#### 4.1.1 概念说明

原始 TMS32010 片内有一个 16×16 的硬件乘法器，配合 T、P 两个寄存器，专门服务于 FIR 滤波、卷积等密集乘加运算。IKA32010 用一个独立子模块 `IKA32010_multiplier` 复刻了它，目标是：

- 接收两个 16 位操作数 `i_OP0`、`i_OP1`，输出 32 位乘积 `o_P`。
- 做**有符号**乘法（两个操作数与结果都声明为 `signed`）。
- 用一个使能信号 `i_MUL_EN` 控制：只有执行乘法指令（MPY/MPYK）时才工作，其它周期保持上一次的乘积不变。
- 写法上让综合工具能把它映射成 FPGA 的 DSP 块（子模块上方有一行注释专门说明这一点）。

> 小知识：为什么强调「有符号」？TMS32010 的 T 寄存器、RAM 数据都可能存放负数（补码）。如果乘法器按无符号处理，比如 T=−1（0xFFFF）乘以 3，无符号会把 0xFFFF 当成 65535，得到 196605，完全错误。声明 `signed` 后，0xFFFF 被当作 −1，得到 −3，才是 DSP 程序期望的结果。

#### 4.1.2 核心流程

子模块只有一段 `always @(posedge i_EMUCLK)`，逻辑可以概括为：

```text
每个 i_EMUCLK 上升沿：
    若 i_RST_n == 0（复位）：三个寄存器清零
    否则若 i_MUL_EN == 1：
        op0_latch <= signed(i_OP0)     // 锁存操作数 0
        op1_latch <= signed(i_OP1)     // 锁存操作数 1
        result    <= op0_latch * op1_latch   // 注意：用的是「旧」latch
    否则：保持不动

输出：o_P = unsigned(result)
```

这里有一个初学者容易看漏的细节：`result <= op0_latch * op1_latch` 用的是**赋值之前**的 `op0_latch/op1_latch`（即「旧值」），而新的操作数在同一拍才被锁进 latch。也就是说，「结果」比「操作数」晚一个锁存动作。这正是经典流水线乘法器的写法。

那么为什么这样写还能得到正确结果？关键在于「操作数在整个机器周期内稳定」。一次 MPY 指令会把 `i_MUL_EN` 拉高整整一个机器周期（4 个 `i_EMUCLK`），而 `i_OP0`（= `reg_t`）、`i_OP1`（= `mul_op1`）在这 4 拍里不变。于是：

| 机器周期内的边沿 | op0_latch / op1_latch | result |
|------|------|------|
| 第 1 个边沿 | 拍入新操作数 | 旧操作数之积（陈旧，会被覆盖） |
| 第 2 个边沿 | 保持新操作数 | **新操作数之积（正确）** |
| 第 3、4 个边沿 | 保持 | 同上（幂等，重复写同一个值） |

经过 4 个边沿后，`result` 必然等于本周期的 `i_OP0 × i_OP1`。这就是子模块**不需要 `i_CEN`/`cyc_ncen`** 的原因——只要操作数稳定，重复采样是幂等的，和 [u2-l5 RAM 子模块](u2-l5-data-ram-and-addressing.md)「靠整周期信号稳定保证每拍写入幂等」是同一种套路。

#### 4.1.3 源码精读

子模块本体在这里（端口只有时钟、复位、使能与两个操作数、一个乘积输出）：

[src/IKA32010.sv:L1985-L2018](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1985-L2018) —— `IKA32010_multiplier` 子模块，做有符号 16×16→32 乘法。

关键几行：

- [src/IKA32010.sv:L1996](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1996) —— 注释说明 Quartus（DE10-nano）与 Vivado（Zybo-Z20）都能把它综合成 DSP 块。README 的资源表也印证了这一点：Altera EP4CE6 上用「两个 9 位乘法器元件」，MiSTer 5CSEBA6 上用「1 DSP block」。
- [src/IKA32010.sv:L1999-L2000](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1999-L2000) —— `op0_latch`、`op1_latch` 声明为 `reg signed [15:0]`，`result` 为 `reg signed [31:0]`，这是「有符号乘法」语义的根。
- [src/IKA32010.sv:L2008-L2012](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L2008-L2012) —— 使能有效时，锁存操作数并用**旧** latch 计算乘积（流水线一拍延迟）。
- [src/IKA32010.sv:L2016](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L2016) —— `o_P = unsigned'(result)`：内部按有符号计算，输出时把位型重新解释成无符号 32 位交给上层（上层 ALU 也是 32 位无符号 wire，靠符号位 `[31]` 自己判断正负）。

> 历史小注：git 提交 `0532c08 "fix multiplier bug"` 把这段复位的极性从 `if(i_RST_n)` 改成了 `if(!i_RST_n)`。改之前，复位逻辑只在「非复位」时执行，等于复位时不清零——是个极性写反的 bug。当前 HEAD 已修复，你读到的就是修正后的版本。

#### 4.1.4 代码实践（源码阅读型）

**目标**：亲手验证「4 个边沿后 `result` 必然正确」这件事，而不依赖仿真器。

**步骤**：

1. 假设某次 MPY 周期内，`reg_t = 16'sd-2`（即 0xFFFE），`mul_op1 = 16'sd3`，`i_MUL_EN` 全周期为高，进入本周期前 `op0_latch = 0`、`op1_latch = 0`、`result = 0`。
2. 仿照 4.1.2 的表格，列出 4 个 `i_EMUCLK` 上升沿之后 `op0_latch`、`op1_latch`、`result` 的取值。
3. 写出 `o_P` 的最终 32 位十六进制值。

**预期结果（待本地验证，以下按源码推导）**：

| 边沿 | op0_latch | op1_latch | result |
|------|-----------|-----------|--------|
| 起始 | 0 | 0 | 0 |
| 1 | −2 | 3 | 0×0 = 0 |
| 2 | −2 | 3 | (−2)×3 = −6 |
| 3 | −2 | 3 | −6 |
| 4 | −2 | 3 | −6 |

`result = −6 = 0xFFFF_FFFA`，故 `o_P = 0xFFFF_FFFA`。注意第 1 拍的 `result=0` 是陈旧值，但它会被后续拍覆盖，不影响最终结论。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `op0_latch`/`op1_latch`/`result` 的 `signed` 关键字全部去掉（改成普通 `reg`），`T = −2 × 3` 会得到什么？

**答案**：会得到 0x0005_FFFA（65534 × 3 = 196602 = 0x0002_FFFA，请以本地仿真为准），总之不再是 −6。这说明了 `signed` 对负数乘法不可或缺。

**练习 2**：子模块为什么没有 `i_CEN`（`cyc_ncen`）输入？

**答案**：因为它在每个 `i_MUL_EN` 为高的 EMUCLK 边沿都采样，而操作数在整周期内稳定，重复采样幂等。只要 `mul_en` 是「整周期有效」的电平而非窄脉冲，4 拍之后结果必正确，无需再用 `cyc_ncen` 选通。

---

### 4.2 T 寄存器与 P 寄存器（reg_t / reg_p）

#### 4.2.1 概念说明

原始 TMS32010 的乘法器有两个配套寄存器：

- **T 寄存器**：保存乘法的一个固定操作数（通常是滤波器抽头延迟线上取出的数据）。每次乘法都「乘以 T」。
- **P 寄存器**：保存上一次乘法的 32 位乘积，供后续 APAC/PAC/SPAC 取用。

在 IKA32010 里，这两个寄存器的**物理位置不对称**，这一点容易被忽略：

- **T 寄存器 `reg_t`** 写在**顶层模块**，是一个真正的 `reg [15:0]`，有独立的加载使能 `reg_t_ld`，在 `cyc_ncen` 拍从 `reg_wrbus` 载入。
- **P 寄存器 `reg_p`** 在顶层只是一根 `wire [31:0]`，它直接连到子模块的输出 `o_P`。也就是说，P 寄存器的「存储体」其实是子模块内部的 `result`，顶层只是给它取了个别名。

#### 4.2.2 核心流程

```text
T 寄存器（顶层）：
    每个 cyc_ncen 拍：
        若 i_RS_n==0：reg_t <= 0
        否则若 reg_t_ld：reg_t <= reg_wrbus      // 从写总线载入（通常来自 RAM）

P 寄存器：
    reg_p（顶层 wire）=== result（子模块内 reg）  // 由乘法器在 i_MUL_EN 周期写入
    reg_p → ALU 端口 B（当 alu_pbsel == ALU_SOURCE_MUL 时）
```

注意时序配合：

- `reg_t` 在 `cyc_ncen`（周期最后一个 EMUCLK）才更新。
- 乘法器在整周期采样。如果某条指令「这一拍改 T」同时又「这一拍启用乘法」，操作数就会在周期中途跳变，4 拍幂等的前提就不成立了。

源码里通过**指令分工**避免了这种竞争：**改 T 的指令（LT/LTA/LTD）不开乘法（`mul_en=NO`），开乘法的指令（MPY/MPYK）不改 T**。于是 T 的更新与乘法永远发生在不同机器周期，操作数始终稳定。这也是为什么「装载数据」和「做乘法」必须拆成 LT、MPY 两条指令——这不是 1983 年工艺的限制，而是这套时序的直接推论。

#### 4.2.3 源码精读

- [src/IKA32010.sv:L383-L385](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L383-L385) —— `reg_t_ld`（加载使能）、`reg_t`（16 位 T 寄存器）、`reg_p`（32 位 wire，注意它不是 `reg`）。
- [src/IKA32010.sv:L387-L392](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L387-L392) —— T 寄存器在 `cyc_ncen` 拍从 `reg_wrbus` 载入，复位清零。
- [src/IKA32010.sv:L398-L401](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L398-L401) —— 子模块例化：`i_OP0` 接 `reg_t`，`i_OP1` 接 `mul_op1`，`o_P` 接 `reg_p`。
- [src/IKA32010.sv:L441](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L441) 与 [src/IKA32010.sv:L451](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L451) —— `alu_pbsel` 选择 ALU 端口 B 的来源：`alu_pbsel ? reg_p : sha_output`。当微码把 `alu_pbsel` 设为 `ALU_SOURCE_MUL`（见 [src/IKA32010_mnemonics.sv:L56-L57](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_mnemonics.sv#L56-L57)），端口 B 就是 P 寄存器，于是 APAC/PAC/SPAC 才能把乘积送进累加器。端口 B 默认切片 `ALU_PBDATA_LONGWORD`（见 u2-l7），所以 32 位 P 会完整地参与 32 位 ALU 运算。

#### 4.2.4 代码实践（源码阅读型）

**目标**：确认「改 T」与「开乘法」确实由不同指令承担，时序上不冲突。

**步骤**：

1. 在 [src/IKA32010.sv:L537](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L537) 开始的微码块里找到微码给乘法相关信号设的默认值（`reg_t_ld = NO; mul_en = NO; mul_op1_source_sel = MUL_OP1_SOURCE_RAM;`）。
2. 分别翻开 LT（[L1479](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1479)）、MPY（[L1535](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1535)）、MPYK（[L1552](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1552)）三条指令的译码。
3. 核对：LT 是否设 `reg_t_ld=YES` 而 `mul_en` 沿用默认 `NO`？MPY/MPYK 是否设 `mul_en=YES` 而 `reg_t_ld` 沿用默认 `NO`？

**预期结果**：应当看到 LT 只动 `reg_t_ld`、MPY/MPYK 只动 `mul_en`（与 `mul_op1_source_sel`）。两边互不交叉，故 T 的更新与乘法采样不会落在同一周期。

#### 4.2.5 小练习与答案

**练习 1**：P 寄存器在顶层声明成 `wire` 而不是 `reg`，为什么？

**答案**：因为 P 的存储体是子模块内部的 `result`，顶层只是用一根 wire 把 `o_P` 引出来。把 `reg_p` 写成 `reg` 反而会与子模块输出冲突。

**练习 2**：为什么不能把 LT 和 MPY 合并成一条「装载并立即乘」的指令？

**答案**：LT 在 `cyc_ncen`（周期末）才更新 `reg_t`，而乘法器在整周期采样。若同周期既改 T 又开乘法，`i_OP0` 会在周期中途跳变，4 拍幂等失效，乘积就不可靠。拆成两条指令、让 T 在 LT 周期结束后稳定，下一周期 MPY 才能安全采样。

---

### 4.3 操作数来源选择 mul_op1_source_sel（MPY 与 MPYK）

#### 4.3.1 概念说明

乘法器的第一个操作数恒为 T 寄存器（`i_OP0 = reg_t`），第二个操作数 `i_OP1 = mul_op1` 则有两种来源，由 1 位选择器 `mul_op1_source_sel` 决定：

- **MPY 指令**：第二操作数来自**数据 RAM**（16 位），对应 `MUL_OP1_SOURCE_RAM`。
- **MPYK 指令**：第二操作数来自指令字里编码的**立即数**（符号扩展到 16 位），对应 `MUL_OP1_SOURCE_IMM`。

两种来源的数据都先汇入 `reg_wrbus`，再由 `mul_op1` 这根组合 wire 加工。也就是说，`reg_wrbus` 既是「RAM 数据通路」（见 u2-l1），也兼任「乘法第二操作数通路」。

#### 4.3.2 核心流程

`mul_op1` 的生成只有一行，但藏着一个符号扩展的细节：

```text
mul_op1 = mul_op1_source_sel ? reg_wrbus                       // RAM 路径（MPY）
                            : {{3{reg_wrbus[12]}}, reg_wrbus[12:0]}  // 立即数路径（MPYK），按 bit[12] 符号扩展
```

- **RAM 路径**：`mul_op1_source_sel == MUL_OP1_SOURCE_RAM`（=1），直接把 16 位 `reg_wrbus`（RAM 读出的数据）送上 `mul_op1`，位宽天然匹配。
- **立即数路径**：`mul_op1_source_sel == MUL_OP1_SOURCE_IMM`（=0），取 `reg_wrbus[12:0]` 并按 `reg_wrbus[12]` 符号扩展到 16 位。

这里有一个**值得在仿真里亲自验证**的点。立即数路径期望取的是「13 位有符号立即数」，但 `reg_wrbus` 在 MPYK 时由 `WRBUS_SOURCE_IMM` 提供，而该来源只把指令字的低 8 位拼上 8 个 0：

```text
WRBUS_SOURCE_IMM 分支：reg_wrbus = {8'h00, if_opcodereg[7:0]}    // 只用了指令字 bit[7:0]
```

于是 `reg_wrbus[12]` 恒为 0，`mul_op1` 实际等于 `{8'h00, if_opcodereg[7:0]}`——**只用了立即数的低 8 位，且按零扩展（恒为正）**，而不是反汇编器 `disasm_type3` 里 `signed'(opcodereg[12:0])` 所暗示的「13 位有符号」。这与 TMS32010 手册里「MPYK 带 13 位有符号立即数」的描述存在出入：当立即数落在 0~127 时两种解释一致；当立即数为负、或超过 8 位时，硬件实际行为会与手册/反汇编显示的不同。本讲不对此下「是 bug 还是特性」的结论，留给 4.3.4 的实践在仿真里确认。

#### 4.3.3 源码精读

- [src/IKA32010.sv:L394-L396](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L394-L396) —— `mul_op1_source_sel`（1 位）、`mul_op1`（含符号扩展的三目运算）、`mul_en` 的声明。注释明确写了 `//sign extended`。
- [src/IKA32010_mnemonics.sv:L32-L34](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_mnemonics.sv#L32-L34) —— `MUL_OP1_SOURCE_IMM = 1'b0`、`MUL_OP1_SOURCE_RAM = 1'b1`。注意 IMM 是 0、RAM 是 1，与 `mul_op1` 三目运算的真假分支对应。
- [src/IKA32010.sv:L139](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L139) —— `WRBUS_SOURCE_IMM` 分支：`reg_wrbus = {8'h00, if_opcodereg[7:0]}`，只透传指令字低 8 位。
- MPY 译码 [src/IKA32010.sv:L1535-L1536](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1535-L1536)：`mul_en = YES; mul_op1_source_sel = MUL_OP1_SOURCE_RAM`，沿用默认的 `register_wrbus_source_sel = WRBUS_SOURCE_RAM`，所以第二操作数 = RAM 数据。
- MPYK 译码 [src/IKA32010.sv:L1552-L1554](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1552-L1554)：`register_wrbus_source_sel = WRBUS_SOURCE_IMM; mul_en = YES; mul_op1_source_sel = MUL_OP1_SOURCE_IMM`。

#### 4.3.4 代码实践（源码阅读 + 仿真验证）

**目标**：核对 MPYK 在「正数立即数」与「负数立即数」两种情况下，`mul_op1` 与最终 `reg_p` 是否符合手册的「13 位有符号」描述。

**步骤**：

1. 取 T = 5（如何把 5 装进 T 见 4.4 的完整序列）。分别构造两条 MPYK：
   - `MPYK 3`：指令字 `1000_0000_0000_0011` = `0x8003`（13 位立即数 = +3）。
   - `MPYK -1`：指令字 `1001_1111_1111_1111` = `0x9FFF`（13 位立即数 = −1，bit[12]=1 为符号位）。
2. 对每条，按 4.3.2 的式子手算 `mul_op1` 与 `reg_p = reg_t × mul_op1`。
3. 若有仿真器，把这两条喂进 DUT（参考 4.4.4 的最小 testbench），观察 `reg_p` 的实际值。

**预期结果（待本地验证）**：

| 指令 | 手册/反汇编期望（13 位有符号） | 按源码硬件推导（低 8 位零扩展） |
|------|------|------|
| MPYK 3 | mul_op1=+3，P=5×3=15（0x0000_000F） | if_opcodereg[7:0]=0x03，mul_op1=0x0003，P=15（0x0000_000F）——一致 |
| MPYK −1 | mul_op1=−1，P=5×(−1)=−5（0xFFFF_FFFB） | if_opcodereg[7:0]=0xFF，mul_op1=0x00FF=255，P=5×255=1275（0x0000_04FB）——**不一致** |

如果仿真结果与「按源码硬件推导」一栏相符，就证实了 4.3.2 的观察：MPYK 的立即数在硬件上只取低 8 位且按零扩展。请以本地仿真为准。

#### 4.3.5 小练习与答案

**练习 1**：`mul_op1` 的立即数路径写成 `{{3{reg_wrbus[12]}}, reg_wrbus[12:0]}`，意图是做几位、按哪个位做符号扩展？

**答案**：把 13 位（`reg_wrbus[12:0]`）按 `reg_wrbus[12]` 符号扩展到 16 位（补 3 个符号拷贝）。这是为 MPYK 的 13 位有符号立即数准备的——前提是 `reg_wrbus[12:0]` 真的承载了 13 位立即数。

**练习 2**：在当前实现里，为什么这个符号扩展对负立即数「不起作用」？

**答案**：因为 `WRBUS_SOURCE_IMM` 只把 `if_opcodereg[7:0]` 放进 `reg_wrbus[7:0]`，高 8 位（含 bit[12]）恒为 0，所以 `reg_wrbus[12]` 永远是 0，符号扩展退化为零扩展，立即数被当作非负数。

---

### 4.4 乘加指令与乘法流水线（LT / MPY / MPYK / APAC / PAC / SPAC / LTA / LTD）

#### 4.4.1 概念说明

乘法器本身只做「T × 第二操作数 → P」。要把乘积真正用起来，还需要一组指令把数据搬进 T、把 P 累加进累加器。IKA32010 实现了 8 条相关指令，它们组成一条典型的**乘加（MAC）流水线**：

| 指令 | 作用 | 关键微码信号 |
|------|------|------|
| LT | 从 RAM 装载 T | `reg_t_ld=YES` |
| MPY | T × RAM → P | `mul_en=YES; mul_op1_source_sel=RAM` |
| MPYK | T × 立即数 → P | `mul_en=YES; mul_op1_source_sel=IMM; WRBUS=IMM` |
| APAC | ACC ← ACC + P | `alu_pbsel=MUL; alu_modesel=ADD; alu_acc_ld=YES` |
| SPAC | ACC ← ACC − P | `alu_pbsel=MUL; alu_modesel=SUB; alu_acc_ld=YES` |
| PAC | ACC ← P（清空旧 ACC） | 同 APAC，但 `alu_paz=YES` 屏蔽 ACC 反馈 |
| LTA | LT + APAC（装载新 T，同时累加上一次的 P） | `reg_t_ld=YES` + APAC 信号 |
| LTD | LTA + DMOV（再顺带把 RAM 数据搬到下一高地址） | LTA 信号 + `ram_dmov=YES` |

理解这套指令的关键，是抓住 P 寄存器的**流水线延迟**：MPY/MPYK 在周期 N 算出的 P，要到周期 N+1 才能被 APAC/PAC/SPAC 读到。这恰好契合 FIR 滤波的节奏——一边装载下一个抽头，一边累加上一个抽头的乘积。

最精巧的是 **LTA/LTD**：它们在**同一条指令里**既装载新 T（为下一次乘法做准备），又把**上一次**的 P 累加进 ACC。注意是「上一次」的 P——因为本次装载的 T 还没经过 MPY，本周期没有新乘积。配合 LTD 的 DMOV（见 u2-l5，把当前 RAM 单元内容搬到下一高地址），一条指令就能完成「取延迟线样本 + 累加旧乘积 + 推进延迟线」三件事，这正是 FIR 抽头循环里最内层的操作。

#### 4.4.2 核心流程

一次最小的乘加运算（ACC += a × k）通常写成：

```text
LACK a ; SACL DATx   // 把 a 放进 RAM[x]（准备数据）
LT   DATx            // T = RAM[x] = a
MPYK k               // P = T * k = a*k        （本周期产出 P）
APAC                 // ACC = ACC + P = a*k     （下一周期消费 P）
```

把 APAC 提前与 LT 合并，就是 LTA；再叠加 DMOV，就是 LTD。一个两抽头 FIR 的核心循环可以写成：

```text
LTD  DAT0    // T=RAM[0], ACC+=P(旧), RAM[1]<-RAM[0]
LTD  DAT0    // 继续（配合 MPY 链）
MPY  DAT1    // 产出新 P
...          // 详见 4.4.4 实践与 u2-l5 的 DMOV
```

时序要点（贯穿 4.1～4.3）：

1. **改 T 与开乘法不同周期**（4.2）：LT/LTA/LTD 不开 `mul_en`，MPY/MPYK 不开 `reg_t_ld`。
2. **P 比操作数晚一拍**（4.1）：MPY 周期产出的 P，下一条指令才能读到。
3. **第二操作数走 `reg_wrbus`**（4.3）：MPY 走 RAM 路径，MPYK 走立即数路径（注意 8 位零扩展的细节）。

#### 4.4.3 源码精读

- 默认值 [src/IKA32010.sv:L573](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L573)：`reg_t_ld = NO; mul_en = NO; mul_op1_source_sel = MUL_OP1_SOURCE_RAM;`——水平微码的默认状态，多数指令无需改写。
- APAC [src/IKA32010.sv:L1469-L1476](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1469-L1476)：`alu_pbsel = ALU_SOURCE_MUL; alu_modesel = ALU_ADD; alu_acc_ld = YES;`——把 P 经端口 B 加进 ACC。
- LT [src/IKA32010.sv:L1479-L1493](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1479-L1493)：仅 `reg_t_ld = YES`，第二操作数与乘法都走默认（不乘）。
- LTA [src/IKA32010.sv:L1496-L1512](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1496-L1512)：`alu_pbsel=MUL + alu_modesel=ADD + alu_acc_ld=YES`（即 APAC 部分）叠加 `reg_t_ld=YES`（即 LT 部分）。
- LTD [src/IKA32010.sv:L1515-L1532](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1515-L1532)：在 LTA 基础上再加 `ram_dmov=YES`（DMOV 见 u2-l5）。
- MPY [src/IKA32010.sv:L1535-L1549](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1535-L1549) 与 MPYK [src/IKA32010.sv:L1552-L1559](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1552-L1559)：见 4.3.3。
- PAC [src/IKA32010.sv:L1562-L1569](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1562-L1569)：比 APAC 多 `alu_paz=YES`，屏蔽端口 A（ACC 反馈），所以 ACC = 0 + P = P。
- SPAC [src/IKA32010.sv:L1572-L1579](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1572-L1579)：把 APAC 的 ADD 换成 SUB。

#### 4.4.4 代码实践（仿真型，本讲主实践）

**目标**：执行 `LACK 5 → SACL DAT0x05 → LT DAT0x05 → MPYK 3 → MPYK −1` 序列，观察 `reg_t` 与 `reg_p` 的值，核对有符号乘积是否正确。

**操作步骤**：

1. 仓库自带的 `IKA32010_tb.v` 用 `$readmemh` 从硬编码的 Windows 路径（`D:/PROCESSOR/...`）加载 ROM，直接跑多半会报错。我们另写一个**最小 testbench**（示例代码，只接指令 ROM，数据走片内 RAM，无需外部数据总线）：

   ```verilog
   `timescale 10ps/10ps
   module tb_mul;                                  // 示例代码
       reg EMUCLK = 1; reg RS_n = 1;
       reg [1:0] divider = 0;
       always #1 EMUCLK = ~EMUCLK;
       always @(posedge EMUCLK) divider <= divider + 2'd1;
       wire cen_n = ~(divider == 2'd3);            // 复刻 tb 的 1/4 占空比 PCEN

       initial begin #20 RS_n = 0; #40 RS_n = 1; end   // 复位后释放

       wire MEN_n, DEN_n, WE_n; wire [11:0] ADDR; wire [15:0] RDBUS;
       reg [15:0] prog [0:15];
       assign RDBUS = MEN_n ? 16'hzzzz : prog[ADDR[3:0]];  // 指令 ROM

       IKA32010 dut (
           .i_EMUCLK(EMUCLK), .i_CLKIN_PCEN(~cen_n),
           .o_CLKOUT(), .o_CLKOUT_PCEN(), .o_CLKOUT_NCEN(),
           .i_RS_n(RS_n),
           .o_MEN_n(MEN_n), .o_DEN_n(DEN_n), .o_WE_n(WE_n),
           .o_AOUT(ADDR), .i_DIN(RDBUS),
           .o_DOUT(), .o_DOUT_OE(),
           .i_BIO_n(1'b1), .i_INT_n(1'b1)
       );

       initial begin
           prog[0] = 16'h7E05;  // LACK 5        -> ACC = 5
           prog[1] = 16'h5005;  // SACL DAT0x05  -> RAM[5] = 5
           prog[2] = 16'h6A05;  // LT  DAT0x05   -> T = 5
           prog[3] = 16'h8003;  // MPYK 3        -> P = T*3
           prog[4] = 16'h9FFF;  // MPYK -1       -> P = T*(-1) ?
           prog[5] = 16'h7F80;  // NOP
           // 用层次名观察内部寄存器（多数仿真器支持）：
           // $display("T=%h P=%h", dut.reg_t, dut.reg_p);
       end
   endmodule
   ```

2. 用你的仿真器（Icarus / ModelSim / Vivado xelab 等）编译 `src/IKA32010.sv` 与上面这个 tb。注意把 `src/` 加入 include 路径，让 `` `include "IKA32010_mnemonics.sv" `` 与 `` `include "IKA32010_disasm.sv" `` 能找到。
3. 把波形里的 `dut.reg_t`、`dut.reg_p`（或用 `$display` 的层次名引用）加进波形窗；同时打开反汇编控制台输出（默认已 `define IKA32010_DISASSEMBLY`）。
4. 单步跑到每条指令的反汇编打印出现，记录对应的 `reg_t`、`reg_p`。

**需要观察的现象**：

- 反汇编应依次打印 `LACK 0x05`、`SACL DAT0x05`、`LT DAT0x05`、`MPYK 3`、`MPYK -1`。
- `reg_t` 在 LT 执行后应变成 `0x0005`，之后不再变（MPYK 不改 T）。
- `reg_p` 在两条 MPYK 之后取不同值。

**预期结果（待本地验证，按源码推导）**：

| 阶段 | reg_t | reg_p（推导） | 说明 |
|------|-------|------|------|
| LACK 5 / SACL DAT0x05 | 0 | 0 | ACC=5，RAM[5]=5 |
| LT DAT0x05 | 0x0005 | 0 | T 载入 5，本周期不开乘法 |
| MPYK 3 | 0x0005 | 0x0000_000F（=15） | 5×3，立即数为正，与手册一致 |
| MPYK −1 | 0x0005 | **0x0000_04FB（=1275）** | 硬件按低 8 位 0xFF 零扩展成 255，得 5×255；而手册「13 位有符号」期望 −5（0xFFFF_FFFB） |

若仿真中 `MPYK −1` 的 `reg_p` 确实等于 `0x0000_04FB` 而非 `0xFFFF_FFFB`，即印证了 4.3 关于 MPYK 立即数路径「只取低 8 位、零扩展」的观察。请以本地仿真结果为准；若与上述推导不符，优先信任仿真。

#### 4.4.5 小练习与答案

**练习 1**：为什么 APAC 把 P 加进 ACC 时不需要 `alu_paz`，而 PAC 需要？

**答案**：APAC 是 `ACC + P`，要保留旧 ACC，所以端口 A 取 ACC 反馈（`alu_paz=NO`，默认）。PAC 是 `ACC ← P`，要丢弃旧 ACC，所以用 `alu_paz=YES` 把端口 A 清零，于是 `0 + P = P`。

**练习 2**：LTA 在同一条指令里既 `reg_t_ld=YES` 又把 P 加进 ACC。它累加的是「本次装载的 T 对应的乘积」吗？

**答案**：不是。本次装载的 T 还没有经过 MPY，本周期没有新乘积。LTA 累加的是**上一条 MPY/MPYK 留下的旧 P**。这正是 FIR 节奏：装载新抽头的同时，结算上一个抽头的乘积。

**练习 3**：把 4.4.4 序列里的 `MPYK −1` 换成 `MPYK 7`（指令字 `0x8007`），`reg_p` 会是多少？

**答案**：立即数 7 落在 0~127，硬件路径（低 8 位零扩展）与手册（13 位有符号）一致，`mul_op1=7`，`reg_p = 5×7 = 35 = 0x0000_0023`。两种解释在此一致。

## 5. 综合实践

把本讲的知识串起来，实现一次「**装载 T → 乘入 P → 累加进 ACC**」的完整跟踪，并理解它如何演化为一条 FIR 抽头指令。

**任务 A（源码跟踪）**：给定 T 已经是 4，连续执行 `MPYK 6`（`0x8006`）→ `APAC`（`0x7F8F`）→ `PAC`（`0x7F8E`）。在纸上画出每条指令执行后 `reg_t`、`reg_p`、`alu_pbsel`、`alu_paz`、ACC 的取值，并解释为什么 APAC 之后 ACC=24（假设初值 0），而 PAC 之后 ACC 仍是 24（因为 P 没变）。

**任务 B（仿真验证）**：在 4.4.4 的最小 testbench 里，把程序改成下面的「两抽头 FIR 内核」并观察 ACC：

```verilog
prog[0] = 16'h7E03;  // LACK 3
prog[1] = 16'h5000;  // SACL DAT0x00  -> RAM[0] = 3
prog[2] = 16'h7E04;  // LACK 4
prog[3] = 16'h5001;  // SACL DAT0x01  -> RAM[1] = 4
prog[4] = 16'h6A00;  // LT  DAT0x00   -> T = 3
prog[5] = 16'h8002;  // MPYK 2        -> P = 3*2 = 6      （产出 P）
prog[6] = 16'h6C01;  // LTA  DAT0x01  -> ACC += P(=6); T = 4
prog[7] = 16'h8002;  // MPYK 2        -> P = 4*2 = 8
prog[8] = 16'h7F8F;  // APAC          -> ACC += P(=8) => ACC = 14
```

跟踪要点：`prog[6]` 的 LTA 累加的是 `prog[5]` 产出的旧 P（6），同时把 T 换成 4；`prog[8]` 的 APAC 累加的是 `prog[7]` 产出的新 P（8）。最终 ACC 应为 6+8=14（`0x0000_000E`，待本地验证）。

**任务 C（思考）**：把任务 B 中的 `LTA DAT0x01` 改成 `LTD DAT0x01`（`0x6B01`），RAM 内容会发生什么变化？提示：DMOV 会把 `RAM[0x01]` 的内容搬到 `RAM[0x02]`（见 u2-l5）。这正是一抽头 FIR 延迟线的推进动作。

## 6. 本讲小结

- `IKA32010_multiplier` 子模块做**有符号 16×16→32 乘法**，内部用 `op0_latch/op1_latch/result` 三级 `signed` 寄存器；`result` 比「操作数锁存」晚一拍，但因为 `mul_en` 整周期有效、操作数稳定，4 个 EMUCLK 边沿后结果必然正确——所以子模块**不需要 `i_CEN`/`cyc_ncen`**。
- 综合器会把它映射为 FPGA 硬核：Altera EP4CE6 用「两个 9 位乘法器元件」，MiSTer 5CSEBA6 用「1 DSP block」（README 资源表为证）。
- **T 寄存器 `reg_t`** 在顶层、`cyc_ncen` 拍从 `reg_wrbus` 载入；**P 寄存器 `reg_p`** 在顶层只是一根 wire，存储体其实是子模块内的 `result`，经 ALU 端口 B（`alu_pbsel = ALU_SOURCE_MUL`）汇入累加器。
- 改 T（LT/LTA/LTD）与开乘法（MPY/MPYK）严格由不同指令承担，保证操作数在乘法周期内稳定，这是「LT、MPY 必须分成两条指令」的根因。
- `mul_op1_source_sel` 在 RAM（MPY）与立即数（MPYK）间选第二操作数；MPYK 的立即数路径由于 `WRBUS_SOURCE_IMM` 只透传低 8 位，`reg_wrbus[12]` 恒为 0，使得符号扩展退化为零扩展——负立即数的实际乘积与手册/反汇编的「13 位有符号」描述可能不一致，需在仿真中确认。
- LTA/LTD 把「装载新 T + 累加旧 P（+ DMOV 推进延迟线）」合为一条指令，是 FIR 内循环的高效形态；P 寄存器的流水线延迟恰好让「上一拍的乘积」被本拍累加。

## 7. 下一步学习建议

- 阅读 [u3-l7 乘法器与 I/O/数据存储类指令译码](u3-l7-multiplier-and-io-instructions.md)，从微码层面逐条剖析 LT/LTA/LTD/MPY/MPYK/APAC/PAC/SPAC 的 `casez` 分支，并把本讲看到的「默认值 + 覆盖」模式与完整的乘法指令组对照。
- 回看 [u2-l5 数据 RAM 与 DMOV](u2-l5-data-ram-and-addressing.md)，把 LTD 的 `ram_dmov` 与 RAM 子模块的「写下一高地址」机制连起来，理解 FIR 延迟线如何在硬件里零开销推进。
- 若你对 P 寄存器如何参与 ALU 的加减、以及 `alu_paz`/`alu_pbdata` 的切片细节还想更深，可重读 [u2-l7 ALU/移位器/累加器](u2-l7-alu-shifters-accumulator.md) 中「端口 B 来源选择」一节。
- 想看乘法器在真实程序里的用法，可结合 `docs/` 下的 TMS32010 用户手册检索 MPY/LTD 的程序设计示例，再回到本仓库的 testbench 与街机游戏验证程序（Twin Cobra 等）中寻找真实调用片段。
