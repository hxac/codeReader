# 乘法器 vmul 与 array_multiplier

## 1. 本讲目标

本讲聚焦 Ventus SM 流水线中的**整数乘法执行单元**。学完后你应当能够：

- 说清为什么整数乘法要从通用 ALU 中独立出来，成为一个单独的 `vmul` 执行单元；
- 读懂单 lane 乘加核 `array_multiplier`，并解释它如何用「同一套硬件」同时支持 `MUL/MULH/MULHU/MULHSU` 四类乘法与 `MACC/NMSAC/MADD/NMSUB` 四类乘加；
- 解释底层 `mult_32` 如何用 **radix-4 Booth 编码 + Wallace 树**完成 32×32→64 位乘法，以及为何采用「先取绝对值相乘、最后修正符号」的策略；
- 理解 `vmul` 如何用 `generate for` 把乘法核复制成 lane 阵列，以及 `vmul_top` 里「折叠 / 不折叠」这一面积换吞吐的旋钮。

本讲承接 [u4-l2 向量 ALU valu](u4-l2-vector-alu.md)：你已知道一条向量指令如何在 warp 内被拆到 `NUM_THREAD` 个 lane 并行执行。乘法器是同一思想的又一个落地，只是把「单拍组合的 `alu`」换成了「两拍流水的 `array_multiplier`」。

## 2. 前置知识

阅读本讲前，请先具备以下概念（不熟悉的术语下面会就地解释）：

- **SIMT 与 lane**：一条向量指令广播给整个 warp，每个 lane 各自处理一条线程的数据。`NUM_THREAD` 既是每 warp 的线程数，也是 lane 数（参见 u1-l3、u4-l2）。
- **执行单元与 issue 路由**：`issue` 单元按指令类型把握手接通到不同执行单元（valu/vmul/fpu/sfu/lsu…），详见 u3-l4。本讲的 `vmul` 就是其中之一。
- **`FN_*` 功能码**：`define.v` 里用 6 位宽的宏给每类运算编号，作为译码后驱动执行单元的控制信号。本讲会大量用到 `FN_MUL` 等。
- **有符号 / 无符号乘法的低 32 位不变性**：这是理解 `MUL` 为何能对两操作数都做零扩展的关键，下文会展开。

如果你还想了解乘法结果如何写回寄存器堆，可回顾 u4-l1（操作数采集与寄存器堆）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [vmul/array_multiplier.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/vmul/array_multiplier.v) | **单 lane 乘加核**，本讲主角。两拍流水，统一处理 4 类乘法 + 4 类乘加。 |
| [vmul/mult_32.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/vmul/mult_32.v) | 32×32→64 位乘法器，用 Booth 编码 + Wallace 树实现。被 `array_multiplier` 例化。 |
| [vmul/booth.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/vmul/booth.v) | radix-4 Booth 编码单元，生成一个部分积。 |
| [vmul/wallace_adder_18.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/vmul/wallace_adder_18.v) | 18 输入 Wallace 压缩树，把 17 个 Booth 部分积（含符号扩展）累加成最终乘积。 |
| [vmul/vmul.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/vmul/vmul.v) | 向量乘法器，用 `generate for` 把 `array_multiplier` 复制成 lane 阵列（不折叠版）。 |
| [vmul/vmul_top.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/vmul/vmul_top.v) | 顶层外壳，用 `MUL_NOT_FOLD` 宏在「不折叠 `vmul`」与「折叠 `vmul_v2`」间切换。 |
| [valu/alu.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/alu.v) | 标量/向量整数 ALU 内核。本讲用它说明「乘法为何不在 alu 里做」。 |
| [define/define.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v) | 配置总开关，定义 `FN_MUL` 系列功能码、`XLEN`、`NUMBER_MUL` 等。 |
| [pipeline/pipe.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v) | SM 流水线顶层，例化 `vmul_top`（约 L1945）。 |

## 4. 核心概念与源码讲解

本讲的三个最小模块为：**乘法功能函数（`FN_*` 家族）**、**`array_multiplier`（单 lane 乘加核）**、**`vmul`（lane 阵列与折叠）**。我们按「先看指令语义、再看单核、最后看向量包装」的顺序展开。

### 4.1 乘法功能函数：FN_* 与乘法 / 乘加指令家族

#### 4.1.1 概念说明

在 Ventus 的指令系统里，整数乘法相关运算被归成一组**功能码**，集中在 `define.v` 中。它们沿用 RISC-V M 扩展的语义，共 8 个：

| 功能码 | 值 | 语义 | 取结果位段 |
| --- | --- | --- | --- |
| `FN_MUL` | 20 | 有符号×有符号 | 低 32 位 |
| `FN_MULH` | 21 | 有符号×有符号 | 高 32 位 |
| `FN_MULHU` | 22 | 无符号×无符号 | 高 32 位 |
| `FN_MULHSU` | 23 | 有符号×无符号 | 高 32 位 |
| `FN_MACC` | 24 | \(c - a\cdot b\)（负累加） | 低 32 位 |
| `FN_NMSAC` | 25 | 见下文 | 低 32 位 |
| `FN_MADD` | 26 | 见下文 | 低 32 位 |
| `FN_NMSUB` | 27 | 见下文 | 低 32 位 |

> 说明：上表中 `MACC/NMSAC/MADD/NMSUB` 的精确符号归属由 `array_multiplier` 内部的数据选择决定，下文 4.2 会逐一对应；这里先建立「8 个功能码 = 4 个纯乘 + 4 个乘加」的整体印象。

一个关键设计判断：**这 8 个功能码虽然写在 ALU 的命名空间里，却不在 `alu` 内核里实现**。`alu.v` 只负责加减、移位、逻辑、比较、min/max；乘法类被 `issue` 路由到了独立的 `vmul` 单元。原因有二：乘法是**多周期 / 流水**操作（`array_multiplier` 是两拍流水），与 `alu` 的纯组合单拍语义不同；且 Booth+Wallace 的面积代价远大于普通 ALU，独立成单元便于复用与替换。

#### 4.1.2 核心流程

从指令到乘法执行的链路如下：

1. `decodeUnit` 把 32 位指令译码出 6 位 `alu_fn`（即 `FN_*`），连同操作数、写回信息一起送入操作数采集器；
2. `issue` 看到 `alu_fn` 属于乘法类，把握手接通到 `vmul_top`（在 `pipe.v` 中名为 `mul` 实例）；
3. `vmul_top` → `vmul` → 每 lane 一个 `array_multiplier`，按 `alu_fn` 选择符号扩展方式与结果位段；
4. 结果经写回端口回到寄存器堆。

#### 4.1.3 源码精读

功能码定义在 [define.v:491-498](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L491-L498)，注意它们是 **6 位宽**：

```verilog
`define FN_MUL      6'd20
`define FN_MULH     6'd21
`define FN_MULHU    6'd22
`define FN_MULHSU   6'd23
`define FN_MACC     6'd24
`define FN_NMSAC    6'd25
`define FN_MADD     6'd26
`define FN_NMSUB    6'd27
```

再看 [alu.v:51-58](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/alu.v#L51-L58)，`alu` 内部也声明了同名的 5 位 localparam：

```verilog
localparam  FN_MUL    = 5'd20;
localparam  FN_MULH   = 5'd21;
...
localparam  FN_NMSUB  = 5'd27;
```

但若追踪 `alu` 的输出选择（[alu.v:145-160](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/alu.v#L145-L160)），会发现 `out_o` 只处理 `ADD/SUB/SEQ/SNE` 与 `shift/logic/cmp/minmax`，对 `op_i >= 20`（乘法类）**没有任何计算分支**。这就证实了：乘法类功能码在 `alu` 里「有名无实」，真正的运算在 `vmul` 里。`alu` 里保留这些 localparam，只是为了与 6 位 `alu_fn` 的低 5 位对齐命名、便于阅读。

`vmul` 单元在 `pipe.v` 中的例化见 [pipe.v:1945-1982](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1945-L1982)：输入接 `issue_out_MUL_*`，输出标量写回收 `writeback_in_x_ready[5]`、向量写回收 `writeback_in_v_ready[4]`。规模参数 `SOFT_THREAD=NUM_THREAD`、`HARD_THREAD=NUMBER_MUL`。

#### 4.1.4 代码实践

**实践目标**：确认「乘法类指令不在 `alu` 中执行」这一结论。

**操作步骤**：
1. 打开 [alu.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/alu.v)，找到 `out_o` 的赋值（L158 附近）。
2. 逐分支列出 `op_i` 取哪些值时 `out_o` 有定义（`FN_A1ZERO/FN_A2ZERO/isMIN/out`）。
3. 验证 `isMIN` 判据 `op_i[4:2]==3'b100`（即 16~19）是否覆盖不到 20~27。

**预期现象**：`op_i=20`（`FN_MUL`）既不满足 `isMIN`，也不进 `out` 的 ADD/SUB/SEQ/SNE 分支，会落到 `shift_logic_cmp` 的兜底逻辑——`alu` 对它没有有效输出。

**预期结果**：结论成立——整数乘法必须由 `vmul` 完成。本步骤为源码阅读型，无需运行仿真。

#### 4.1.5 小练习与答案

**练习 1**：`define.v` 用 6 位定义 `FN_MUL=6'd20`，`alu.v` 用 5 位定义 `FN_MUL=5'd20`，二者会冲突吗？为什么 `array_multiplier` 用的是 6 位版本？

> **答案**：不冲突。`alu.v` 的 5 位 localparam 是模块内局部可见，仅用于该模块内部比较；`array_multiplier` 的端口 `ctrl_alu_fn_i` 声明为 `[5:0]`（6 位，见下文），比较对象是 `define.v` 的全局 6 位宏。数值 20 在 5 位和 6 位下低 5 位相同，因此 `alu` 的 5 位比较与全链路的 6 位 `alu_fn` 天然对齐。

**练习 2**：`FN_MACC/NMSAC/MADD/NMSUB` 的十进制值是 24/25/26/27，它们的二进制 `[4:2]` 位段是什么？这会引出 4.2 的一个关键判据。

> **答案**：24=011000、25=011001、26=011010、27=011011，`[4:2]` 位段都是 `110`。所以只需 `func[4:2]==3'b110` 一句就能识别「这是乘加类指令」——这正是 `array_multiplier` 里 `ismac` 的判据。

### 4.2 array_multiplier：单 lane 的乘加流水核

#### 4.2.1 概念说明

`array_multiplier` 是乘法器的**单 lane 内核**：给它一对 32 位操作数（乘加时再加第三个），它在一个 lane 上算出 32 位结果。它是「阵列化」的——这里的「阵列」指底层 `mult_32` 用 Booth 编码生成多个部分积、再用 Wallace 树压缩的乘法阵列（参考香山南湖的 Array Multiplier 设计）。

它最精妙之处是**用同一套乘法硬件服务 8 种功能码**，靠三个手段区分：
1. **符号扩展方式**（决定有符号 / 无符号）；
2. **结果位段选择**（取高 32 还是低 32）；
3. **是否叠加第三个操作数 c**（决定是纯乘还是乘加）。

#### 4.2.2 核心流程

单条指令进入 `array_multiplier` 后：

1. **输入选择**（纯乘 vs 乘加的差别）：根据 `ctrl_alu_fn_i` 决定三个物理输入 `mul_in1/2/3` 分别承载 a/b/c 中的哪一个；
2. **符号扩展**：按功能码给 `mul_in1`、`mul_in2` 各自做符号扩展或零扩展，得到 33 位的 `ai`、`bi`；
3. **绝对值乘法**：`mult_32` 取两数的绝对值相乘，得到 64 位 `mul_result`，再按两数符号异或决定是否取负；
4. **乘加合成**：若 `ismac`，把 `mul_result` 与第三操作数 `cvec_mul_in3` 相加或相减，得到 `mac_out`；
5. **结果选择**：`ismac` → 取 `mac_out` 低 32 位；`MULH/MULHU/MULHSU` → 取 `mul_result` 高 32 位；其余（`MUL`）→ 取低 32 位；
6. **两拍流水**：控制信号与第三操作数经两级流水寄存器跟随，valid/ready 用 skid 反压逻辑保证不丢指令。

#### 4.2.3 源码精读

**(a) 输入选择：乘加时把累加数与乘数换位**

[array_multiplier.v:90-92](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/vmul/array_multiplier.v#L90-L92)：

```verilog
assign mul_in1 = (ctrl_alu_fn_i==`FN_MADD | ctrl_alu_fn_i==`FN_NMSUB) ? c_i : a_i;
assign mul_in2 = b_i;
assign mul_in3 = (ctrl_alu_fn_i==`FN_MADD | ctrl_alu_fn_i==`FN_NMSUB) ? a_i : c_i;
```

注意乘数恒为 `b_i`（即 `mul_in2`）。对 `MADD/NMSUB`，把累加数 `c_i` 接到 `mul_in1`、把 `a_i` 接到 `mul_in3`；其它情况 `mul_in1=a_i`、`mul_in3=c_i`。这样下游「乘 `mul_in1`×`mul_in2`、再叠加 `mul_in3`」的统一公式就能覆盖所有乘加变体。

**(b) 符号扩展：4 类乘法的核心区分**

[array_multiplier.v:94-101](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/vmul/array_multiplier.v#L94-L101)：

```verilog
assign signext_mul_in1 = {mul_in1[`XLEN-1], mul_in1};   // 符号扩展 1 位
assign zeroext_mul_in1 = {1'b0, mul_in1};               // 零扩展 1 位
...
assign ai = (fn==`FN_MULH | fn==`FN_MULHSU) ? signext_mul_in1 : zeroext_mul_in1;
assign bi = (fn==`FN_MULH) ? signext_mul_in2 : zeroext_mul_in2;
```

把规则列表化（与文件头注释一致）：

| 功能码 | `ai`（操作数1） | `bi`（操作数2） |
| --- | --- | --- |
| `FN_MUL` | 零扩展 | 零扩展 |
| `FN_MULH` | 符号扩展 | 符号扩展 |
| `FN_MULHU` | 零扩展 | 零扩展 |
| `FN_MULHSU` | 符号扩展 | 零扩展 |

为什么 `MUL`（有符号×有符号）能用零扩展？因为 **`MUL` 只取低 32 位，而乘积的低 32 位与操作数的符号解释无关**（模 \(2^{32}\) 运算下，有符号乘与无符号乘的低字相同）。只有取高 32 位的 `MULH*` 才必须严格区分符号。这是 RISC-V M 扩展的经典性质，硬件正好利用它简化了 `MUL` 的处理。

**(c) 底层 mult_32：先取绝对值，再修符号**

`array_multiplier` 在 [L104-115](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/vmul/array_multiplier.v#L104-L115) 例化 `mult_32`，得到 64 位 `mul_result`。`mult_32` 的策略是「**幅度乘法器**」——始终算绝对值之积，最后按符号修正：

[mult_32.v:98-99](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/vmul/mult_32.v#L98-L99) 取绝对值（用扩展位 `Asign[32]` 判断符号）：

```verilog
assign A = Asign[WORDLEN] ? ~(Asign[WORDLEN-1:0])+1 : Asign[WORDLEN-1:0];
assign B = Bsign[WORDLEN] ? ~(Bsign[WORDLEN-1:0])+1 : Bsign[WORDLEN-1:0];
```

[mult_32.v:256](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/vmul/mult_32.v#L256) 修符号（两扩展位异或）：

```verilog
assign Result = (Asign[WORDLEN] ^ Bsign[WORDLEN]) ? ~Result_abs+1 : Result_abs;
```

最终符号为：

\[
\text{sign} = s_a \oplus s_b,\qquad \text{Result} = \text{sign}\,?\, -(|a|\cdot|b|) : |a|\cdot|b|
\]

其中 \(s_a, s_b\) 就是 (b) 中选择的扩展位。这一套机制让一个无符号乘法器同时满足有符号、无符号、混合符号三种需求——**符号信息完全由输入端的扩展位携带**。

> 补充：对乘加类（`MACC/NMSAC/MADD/NMSUB`），(b) 中两操作数都走零扩展，文件头注释明确写道「乘加中都是无符号数乘法」([array_multiplier.v:16](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/vmul/array_multiplier.v#L16))。

**radix-4 Booth 编码**：32 位数需 16+1=17 个 Booth 编码器（每次处理 2 位、相邻重叠 1 位，外加 1 个处理最高符号位），见 [mult_32.v:102-118](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/vmul/mult_32.v#L102-L118)。单个 Booth 单元（[booth.v:14-33](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/vmul/booth.v#L14-L33)）把 3 位重叠窗口 \((a_{2i+1},a_{2i},a_{2i-1})\) 重编码成部分积 \(\in\{-2B,-B,0,B,2B\}\)。17 个部分积再经 18 输入 Wallace 树（[mult_32.v:231-254](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/vmul/mult_32.v#L231-L254)，第 18 路是符号扩展进位）压缩成 `Result_abs`。Booth 编码把部分积个数减半，Wallace 树把加法深度压到 \(O(\log)\)，二者合起来控制了乘法器的面积与延迟。

**(d) 乘加合成与结果选择：一条三目链覆盖 8 种功能码**

[array_multiplier.v:205-208](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/vmul/array_multiplier.v#L205-L208) 是全模块最关键的几句：

```verilog
assign func    = ctrlvec_alu_fn;
assign mac_out = (func==`FN_MACC | func==`FN_MADD) ? (mul_result + {{`XLEN{1'b0}},cvec_mul_in3})
                                                   : ({{`XLEN{1'b0}},cvec_mul_in3} - mul_result);
assign ismac   = func[4:2] == 3'b110;
assign res     = ismac ? mac_out[`XLEN-1:0] :
                 ((func==`FN_MULH | func==`FN_MULHU | func==`FN_MULHSU) ? mul_result[2*`XLEN-1:`XLEN]
                                                                         : mul_result[`XLEN-1:0]);
```

解读：
- `mac_out`：乘加时把 64 位 `mul_result` 与第三操作数 `cvec_mul_in3`（零扩展到 64 位）相加（`MACC/MADD`）或相减（`NMSAC/NMSUB`）。
- `ismac`：用练习 2 的位段判据一次性识别 4 个乘加码。
- `res`：三档优先级——乘加取 `mac_out` 低 32 位；高位乘 `MULH/MULHU/MULHSU` 取 `mul_result[63:32]`；`MUL`（及其它）取 `mul_result[31:0]`。

**这就是「MULH 取高位、MUL 取低位，从同一 64 位 `mul_result` 中选取」的直接答案**：二者共用同一个 `mult_32` 乘积，仅最终位段选择不同。

**(e) 两拍流水与握手**

[array_multiplier.v:117-143](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/vmul/array_multiplier.v#L117-L143) 用 `in_valid_reg1/reg2` 维护两级流水 valid，并通过 `!out_ready_i` 实现 skid（下游不收时上游保持）。`in_ready_o` 与 `out_valid_o` 满足 valid/ready 握手协议。控制信号（`alu_fn/wid/reg_idxw/wvd/wxd/mask`）与第三操作数 `cvec_mul_in3` 经 [L146-174](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/vmul/array_multiplier.v#L146-L174)、[L177-202](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/vmul/array_multiplier.v#L177-L202) 两级寄存器与数据对齐，最终 `result_o` 在 [L210-220](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/vmul/array_multiplier.v#L210-L220) 由 `reg_en2` 打拍输出。

#### 4.2.4 代码实践

**实践目标**：亲手验证 4 类乘法如何从同一 64 位乘积中取位，并用符号扩展 + 绝对值乘法的模型预测结果。

**操作步骤**：

1. 取 \(a=\texttt{0xFFFFFFFE}\)（按有符号解释为 \(-2\)，按无符号为 \(4294967294\)），\(b=\texttt{0x00000002}\)。
2. 用「绝对值相乘 + 符号修正」手算 64 位 `mul_result`：
   - \(|a|=2,\ |b|=2\)，\(|a|\cdot|b|=4=\texttt{0x00000004}\)。
3. 对 4 种功能码，分别确定 (b) 表中的扩展位、修正后的 64 位乘积、`res` 取哪一段：

| 功能码 | \(s_a\) | \(s_b\) | 修正后 64 位乘积 | 取段 | `res` |
| --- | --- | --- | --- | --- | --- |
| `MUL` | 0（零扩） | 0 | `0x00000004`×... → 见下 | 低 32 | `0xFFFFFFFC` |
| `MULH` | 1（符扩） | 0 | `0xFFFFFFFF_FFFFFFFC` | 高 32 | `0xFFFFFFFF` |
| `MULHU` | 0 | 0 | `0x00000001_FFFFFFFC` | 高 32 | `0x00000001` |
| `MULHSU` | 1 | 0 | `0xFFFFFFFF_FFFFFFFC` | 高 32 | `0xFFFFFFFF` |

   > 提示：`MUL` 走零扩展，\(|a|=4294967294\)，乘积 \(=8589934588=\texttt{0x1\_FFFFFFFC}\)，低 32 位为 `0xFFFFFFFC`；这与「有符号 \(-2\times2=-4\)」的低字一致，正体现了低 32 位的符号无关性。

4. （选做）按 u1-l4 的流程跑一个含乘法的测试用例，在 Verdi 中观察 `array_multiplier` 的 `a_i/b_i/ctrl_alu_fn_i/result_o`，比对上表。

**预期结果**：上表 `res` 列即预期硬件输出。注意 `MULH` 与 `MULHSU` 在本例下同为 `0xFFFFFFFF`，而 `MULHU` 为 `0x00000001`——这正是符号解释影响高位结果的直观体现。

> 若无法运行仿真，步骤 1~3 的手算已构成完整结论；步骤 4 标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：把 `array_multiplier` 的 `res` 选择改写成「先判 `MUL*` 高位、再判 `ismac`」会有什么问题？

> **答案**：会出错。`MACC/NMSAC/MADD/NMSUB` 的功能码（24~27）落在本设计的 `ismac` 判据内，但若先判高位乘法分支，需要写明排除乘加码，否则可能把乘加误当成高位乘。当前代码用 `ismac ? ... : (...)` 把乘加放在最高优先级，逻辑最清晰、最不易错。

**练习 2**：`mult_32` 为什么对两操作数都取绝对值再相乘，而不是直接做有符号乘法？

> **答案**：这样核心乘法阵列（Booth+Wallace）只需处理无符号幅度，结构单一、易于复用与综合；符号差异被压缩成「输入端扩展位 + 末端一个异或取负」，大幅简化了同时支持有符号/无符号/混合符号的设计。代价是末端一次取负（加法器），但远比维护两套乘法阵列划算。

### 4.3 vmul：lane 阵列、握手与折叠旋钮

#### 4.3.1 概念说明

`array_multiplier` 只算一个 lane。一条向量乘法指令要在 warp 内对 `NUM_THREAD` 个线程并行计算，就需要把核复制 `NUM_THREAD` 份——这正是 `vmul` 的工作。它与 `valu` 的结构如出一辙：用 `generate for` 把同一个内核铺成 lane 阵列，控制信号（功能码、wid、写回寄存器号）全广播，数据各 lane 独立。外层 `vmul_top` 还提供一个「折叠」选项：当 lane 资源紧张时，可用更少的物理核、分多拍迭代完成一条向量指令——面积换吞吐。

#### 4.3.2 核心流程

`vmul`（不折叠版）的数据流：

1. 输入 `in1/in2/in3` 各是 `SOFT_THREAD×32` 位的大向量，按 lane 切片喂给对应的 `array_multiplier`；
2. `ctrl_reverse_i` 控制是否交换 `in1/in2`（对应 reverse 类伪指令，与 valu 同思路）；
3. 每个 lane 独立算出 32 位结果，再拼回 `SOFT_THREAD×32` 位的向量结果；
4. 标量结果（`wxd`）与向量结果（`wvd`）分别走两个 `stream_fifo_pipe_true`（深度 1）做一拍缓冲，再向 writeback 输出；
5. `in_ready_o` 取自 lane[0] 的 `mul_in_ready`（所有 lane 同步握手）。

#### 4.3.3 源码精读

**(a) lane 阵列例化**

[vmul.v:91-128](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/vmul/vmul.v#L91-L128)：

```verilog
generate
  for(i=0;i<HARD_THREAD;i=i+1) begin : A1
    array_multiplier U_mul( ... );   // 每 lane 一个核
    assign mul_in1[i] = ctrl_reverse_i ? in2_i[...] : in1_i[...];
    assign mul_in2[i] = ctrl_reverse_i ? in1_i[...] : in2_i[...];
    assign mul_in3[i] = in3_i[...];
    assign mul_out_ready[i] = mul_out_wxd[i] ? result_x_in_ready : result_v_in_ready;
    assign wb_wvd_rd_comb[(i+1)*`XLEN-1-:`XLEN] = mul_result[i];
  end
endgenerate
```

要点：
- 循环上限是 `HARD_THREAD`（物理核数）；不折叠时 `HARD_THREAD=SOFT_THREAD=NUM_THREAD`，一拍处理完整个 warp。
- `ctrl_reverse_i` 在输入侧交换 `in1/in2`，让 reverse 类指令复用同一套乘法核。
- 每个 lane 按自己写回类型（`wxd` 标量 / `wvd` 向量）选择反压来源。

**(b) 标量 / 向量双出口的一拍缓冲**

[vmul.v:130-156](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/vmul/vmul.v#L130-L156) 例化两个 `stream_fifo_pipe_true`（`U_result_x`、`U_result_v`，深度 1），[L159-166](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/vmul/vmul.v#L159-L166) 把 lane[0] 的控制信号与结果打包进 FIFO。这与 `valu` 里用 `stream_fifo_pipe_true` 切断组合长路径、提供反压的用法完全一致（回顾 u4-l2）。

**(c) 折叠旋钮：vmul_top 的 ifdef**

[vmul_top.v:17](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/vmul/vmul_top.v#L17) 有一行硬编码宏：

```verilog
`define MUL_NOT_FOLD
```

它使得 [vmul_top.v:61-138](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/vmul/vmul_top.v#L61-L138) 的 `ifdef` 始终走「不折叠」的 `vmul` 分支。`vmul_v2`（折叠版）只有在注释掉这行、改走 `else` 时才会启用，其 `MAX_ITER = SOFT_THREAD/HARD_THREAD` 表示用 `HARD_THREAD` 个物理核分 `MAX_ITER` 拍完成一条向量乘法（[vmul_v2.v:17-21](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/vmul/vmul_v2.v#L17-L21)）。这与 `valu_top` 的 `ALU_NOT_FOLD` 是同一套面积换吞吐的设计模式（回顾 u4-l2）。

> 默认 `NUMBER_MUL = NUM_THREAD`（[define.v:266](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L266)），且 `MUL_NOT_FOLD` 硬编码开启，所以默认配置下乘法器是「全并行、不折叠」的。

#### 4.3.4 代码实践

**实践目标**：弄清「物理核数 × 迭代拍数 = 一个 warp 的线程数」这一关系，并定位折叠开关。

**操作步骤**：
1. 在 [vmul.v:92](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/vmul/vmul.v#L92) 确认 `generate` 上限为 `HARD_THREAD`。
2. 在 [pipe.v:1945-1948](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1945-L1948) 读出 `SOFT_THREAD=NUM_THREAD`、`HARD_THREAD=NUMBER_MUL`、`MAX_ITER=NUM_THREAD/NUMBER_MUL`。
3. 做一个纸面实验：假设把 `define.v` 中 `NUMBER_MUL` 改为 `NUM_THREAD/2`，并注释掉 `vmul_top.v` 的 `\`define MUL_NOT_FOLD` 改走 `vmul_v2`，说明此时一条向量乘法需要几拍、物理核省了一半。

**预期现象**：`MAX_ITER` 变为 2，一条向量乘法由 2 拍迭代完成；面积约为原来一半，吞吐减半。

**预期结果**：能复述「物理核数 `HARD_THREAD` × 迭代拍数 `MAX_ITER` = `SOFT_THREAD`（= `NUM_THREAD`）」。本步骤为源码阅读型；实际改宏重编综合属于进阶操作，结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `vmul` 的 `in_ready_o` 直接取 `mul_in_ready[0]`（lane 0 的 ready），而不是对所有 lane 做「与」？

> **答案**：因为所有 lane 共享同一组控制信号与同一拍 `in_valid_i`，且 `array_multiplier` 的握手逻辑对每个 lane 完全相同，下游 `result_x/result_v` 的 ready 也统一分发。因此各 lane 的 `in_ready` 在同一拍取值一致，取 lane[0] 即可代表全体，简化了连线。

**练习 2**：`stream_fifo_pipe_true`（深度 1）在这里起什么作用？去掉它会怎样？

> **答案**：它是一拍流水缓冲，作用有二：切断「`array_multiplier` 组合/流水结果 → writeback」之间的长组合路径，改善时序；并提供标准 valid/ready 反压接口。去掉后，乘法结果到写回的路径会变长，且反压逻辑需要重写，时序与正确性都可能受影响。

## 5. 综合实践

把本讲三个模块串起来，完成一次「**从指令到 bit 级结果**」的完整推演。

**任务**：给定向量指令对 \(a=\texttt{0xFFFFFFFE}\)、\(b=\texttt{0x00000002}\) 做 4 种乘法，完整还原硬件数据通路并预测结果。

**要求**：
1. **指令路由**：说明这条指令从 `issue` 经 `vmul_top`→`vmul`→`array_multiplier`→`mult_32` 的例化路径（引用 4.3.3(a) 与 4.2.3(c) 的源码位置）。
2. **符号扩展**：对照 4.2.3(b) 的表，写出 `MUL/MULH/MULHU/MULHSU` 各自的 `ai/bi` 扩展位。
3. **绝对值乘法**：写出 `mult_32` 内 `A/B/Result_abs/Result` 的值（引用 [mult_32.v:98-99](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/vmul/mult_32.v#L98-L99) 与 [L256](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/vmul/mult_32.v#L256)）。
4. **结果选择**：用 4.2.3(d) 的 `res` 三目链，给出 4 种功能码的最终 32 位 `result_o`。
5. **lane 维度**：说明当 `NUM_THREAD=4`、不折叠时，`vmul` 会同时例化几个 `array_multiplier`、一拍产出几组结果（引用 [vmul.v:91-128](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/vmul/vmul.v#L91-L128)）。

**参考答案要点**：4 种结果为 `MUL=0xFFFFFFFC`、`MULH=0xFFFFFFFF`、`MULHU=0x00000001`、`MULHSU=0xFFFFFFFF`（推导见 4.2.4）；`NUM_THREAD=4` 时 `vmul` 例化 4 个 `array_multiplier`，一拍产出 4 组 32 位结果。

## 6. 本讲小结

- 整数乘法是独立执行单元 `vmul`：`alu.v` 只声明 `FN_MUL*` 名字却不实现，真正运算在 `vmul`→`array_multiplier`，原因是乘法为多拍流水、面积大。
- `array_multiplier` 用**同一套硬件**覆盖 8 个功能码：靠符号扩展（有/无符号）、结果位段（高/低 32 位）、是否叠加第三操作数（纯乘/乘加）三轴区分。
- 底层 `mult_32` 是「幅度乘法器」：始终算 \(|a|\cdot|b|\)，用两扩展位异或修正符号，一套无符号阵列即可服务有符号/无符号/混合符号。
- `MUL` 取低 32 位、`MULH*` 取高 32 位，二者共用同一 64 位 `mul_result`，仅 `res` 位段选择不同；`MUL` 之所以能零扩展，源于乘积低 32 位的符号无关性。
- 乘加复用乘法核：`ismac = func[4:2]==3'b110` 识别 4 个乘加码，`MACC/MADD` 做 `+`、`NMSAC/NMSUB` 做 `−`。
- `vmul` 用 `generate for` 把核铺成 lane 阵列（SIMT 并行），`vmul_top` 用 `MUL_NOT_FOLD` 宏在「全并行」与「折叠省面积」间切换——与 `valu_top` 的 `ALU_NOT_FOLD` 同构。

## 7. 下一步学习建议

- **横向对照**：回到 [u4-l2](u4-l2-vector-alu.md)，把 `valu/alu` 与本讲的 `vmul/array_multiplier` 做一张对比表（单拍组合 vs 两拍流水、是否 lane 复制、`NOT_FOLD` 旋钮），体会「执行单元」的统一设计范式。
- **进入浮点**：下一讲 [u4-l4 FPU 与 vFPU](u4-l4-fpu-and-vfpu.md) 会讲解浮点执行通路，其 `fma`（乘加）与本讲整数乘加在「乘 + 加复用」思想上相通，可对照阅读。
- **深入底层**：若对综合面积/时序感兴趣，可阅读 `wallace_adder_18.v` 与 `booth.v`，结合 `mult_32.v` 的 FORMAL 断言（`assert property(A*B==Result)`，[mult_32.v:258-260](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/vmul/mult_32.v#L258-L260)）理解形式化验证乘法器正确性的方法。
- **写回视角**：想看乘法结果如何回到寄存器堆，可重温 u4-l1 的 operand_collector 写回路径，以及 `pipe.v` 中 `writeback` 对 `mul_out_x_*`/`mul_out_v_*` 的仲裁。
