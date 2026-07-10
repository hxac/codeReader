# 浮点运算单元（fp17/fp32）

## 1. 本讲目标

本讲聚焦 `vmod/vlibs/` 里一组以 `HLS_fp` 开头的文件——它们是 NVDLA 的**浮点运算单元（FPU）**。学完后你应当能够：

- 说清 NVDLA 内部使用的三种浮点格式：标准 **fp16**（对外接口）、自研 **fp17**（内部运算）、标准 **fp32**（中间结果），以及它们各自的位宽与「符号 / 指数 / 尾数」划分。
- 解释**为什么 NVDLA 不直接用 fp32 做内部运算**，而是发明了一个 17 位的 fp17——这是精度、动态范围与硅片面积之间的权衡。
- 读懂 `HLS_fp17_mul` / `HLS_fp17_add` / `HLS_fp16_to_fp17` / `HLS_fp17_to_fp32` 这些模块的**流式（valid/ready）接口**与内部计算流程。
- 认识这些 FPU 单元在哪些引擎里被真正例化（CDP、PDP），以及**为什么 CMAC、CACC 不用它们**。
- 看懂「C++ 模板源 → Catapult HLS 工具 → Verilog 网表」这条生成链，并能把读源码的习惯从「啃网表」切换到「读 C++ 原始算法」。

本讲承接 [u6-l2 FIFO 与 vlibs 库原语](u6-l2-fifo-vlibs-primitives.md)：那一讲讲了 `vlibs` 里的同步器、FIFO 断言、MUX 等「数字积木」；本讲下钻到 `vlibs` 里另一类成体系的库单元——**浮点运算器**，它们同样是「库化的、可被各引擎复用的」标准件。

---

## 2. 前置知识

阅读本讲前，最好先具备以下直觉（不懂也没关系，本讲会顺带补）：

- **浮点数（floating point）**：用「符号位 + 指数 + 尾数」三段来表示一个实数，类似科学计数法 \( (-1)^s \times 1.m \times 2^{e} \)。它的好处是能同时表示很大和很小的数。
- **IEEE 754 半精度（fp16）**：16 位 = 1 符号 + 5 指数 + 10 尾数，是深度学习里最常用的「窄浮点」格式。
- **隐含的最高位 1（implied leading 1）**：规格化（normal）浮点数的尾数最高位永远是 1，所以这一位**不存储**、由硬件「脑补」出来，从而白赚 1 位精度。
- **偏置（bias）**：指数字段存的是无符号数 \( e \)，真实指数是 \( e - B \)，其中 \( B = 2^{E-1}-1 \)（\( E \) 是指数位宽）。这样用无符号比较就能比出浮点大小。
- **握手协议（valid/ready）**：数据从上游传到下游，需要上游声明「数据有效（valid）」、下游声明「我准备好接了（ready）」，只有两者同时为真，这一拍才真正完成一次传送。这正是 u6-l2 讲过的流式接口思想。
- **HLS（High-Level Synthesis，高层次综合）**：用 C/C++ 描述算法，由工具（NVDLA 用的是 Mentor 的 Catapult）自动生成 RTL。本讲的 Verilog 文件几乎全是机器生成的「网表」。

> **关键背景**：NVDLA 主力是**定点**（int8/int16）推理，但在后处理的 CDP（LRN 跨通道归一化）、PDP（池化）里，因为涉及「平方求和、除法、非线性激活」这类**动态范围大、难定点化**的运算，会在内部临时切到浮点。为了在「精度够用」和「面积最小」之间取平衡，NVDLA 自研了一个 17 位的内部浮点格式 **fp17**，并配套了 fp16↔fp17↔fp32 的转换单元和加减乘运算单元。

---

## 3. 本讲源码地图

本讲涉及两类文件：`vmod/vlibs/HLS_fp*.v`（FPU 单元本体，机器生成的网表）与 `cmod/hls/include/nvdla_float.h`（这些网表的**人类可读 C++ 原始源**），再加上 `vmod/nvdla/cdp`、`vmod/nvdla/pdp` 下的调用方。

| 文件 | 作用 |
|------|------|
| [cmod/hls/include/nvdla_float.h](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/hls/include/nvdla_float.h) | **C++ 模板源**：所有 FPU 单元的真正算法源头。定义格式编码、转换与加减乘算法。读这一份比读网表高效得多。 |
| [vmod/vlibs/HLS_fp16_to_fp17.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/HLS_fp16_to_fp17.v) | 转换单元：fp16（16 位）→ fp17（17 位），只拓宽指数。 |
| [vmod/vlibs/HLS_fp17_to_fp16.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/HLS_fp17_to_fp16.v) | 转换单元：fp17 → fp16，收窄指数（需舍入与 denorm 处理）。 |
| [vmod/vlibs/HLS_fp17_to_fp32.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/HLS_fp17_to_fp32.v) | 转换单元：fp17 → fp32（32 位），同时拓宽指数与尾数。 |
| [vmod/vlibs/HLS_fp17_mul.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/HLS_fp17_mul.v) | 运算单元：两个 fp17 相乘，输出 fp17。 |
| [vmod/vlibs/HLS_fp17_add.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/HLS_fp17_add.v) | 运算单元：两个 fp17 相加，输出 fp17（减法 = 加法 + 翻转符号）。 |
| [vmod/vlibs/HLS_fp32_mul.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/HLS_fp32_mul.v) | 运算单元：两个 fp32 相乘（C-model 参考用，RTL 中较少直接例化）。 |
| [vmod/vlibs/HLS_fp32_add.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/HLS_fp32_add.v) | 运算单元：两个 fp32 相加。 |
| [vmod/nvdla/cdp/NV_NVDLA_CDP_DP_MUL_unit.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_DP_MUL_unit.v) | 调用方：CDP 的乘法单元，例化 `HLS_fp17_mul`。 |
| [vmod/nvdla/cdp/NV_NVDLA_CDP_DP_sum.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_DP_sum.v) | 调用方：CDP 的求和块，例化多个 `HLS_fp17_to_fp32`。 |
| [vmod/nvdla/pdp/NV_NVDLA_PDP_CORE_cal2d.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/pdp/NV_NVDLA_PDP_CORE_cal2d.v) | 调用方：PDP 二维池化，例化 `HLS_fp17_mul` 与 `HLS_fp17_to_fp16`。 |
| [vmod/nvdla/pdp/fp16_4add.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/pdp/fp16_4add.v) | 调用方：PDP 的 4 路并行加法器，例化 `HLS_fp17_add`。 |

---

## 4. 核心概念与源码讲解

本讲按五个最小模块展开：**fp17 内部格式**、**HLS 流式接口与生成机制**、**加减乘运算单元**、**格式互转单元**、**FPU 在引擎中的使用场景**。

### 4.1 fp17 内部浮点格式（为什么是 17 位）

#### 4.1.1 概念说明

NVDLA 在 RTL 里一共出现三种浮点格式，它们的位段划分都遵循同一套规则：

\[
\text{总位宽} = 1\ (\text{符号}) + E\ (\text{指数}) + M\ (\text{尾数})
\]

| 格式 | 总宽 | 符号 | 指数位宽 \(E\) | 尾数位宽 \(M\) | 偏置 \(B=2^{E-1}-1\) | 用途 |
|------|------|------|----------------|----------------|----------------------|------|
| **fp16** | 16 | 1 | 5 | 10 | 15 | 对外接口、存储格式（标准 IEEE 半精度） |
| **fp17** | 17 | 1 | 6 | 10 | 31 | **内部运算**（NVDLA 自研） |
| **fp32** | 32 | 1 | 8 | 23 | 127 | 中间结果、C-model 参考（标准 IEEE 单精度） |

注意一个关键事实：**fp17 的尾数位宽和 fp16 完全一样（都是 10 位）**，只比 fp16 多了 1 位指数。也就是说：

- **精度不变**：fp17 和 fp16 的小数精度完全相同（10 位尾数）。
- **动态范围翻倍**：fp16 规格化数的真实指数范围是 \([-14, +15]\)，而 fp17 是 \([-30, +31]\)——上下两端各多出约 1 倍量级。

这正是 fp17 的设计意图：在**不增加乘法器面积**（尾数乘法仍是 11×11，因为多了个隐含 1）的前提下，给运算**留出更多指数余量**，避免累加、平方求和时溢出或下溢。

#### 4.1.2 核心流程：浮点数的编码规则

`nvdla_float.h` 开头的注释把整套编码规则讲得一清二楚：

```
//NAN:     ^E-1 & f!=0.
//INF:     ^E-1 & f==0. 
//Denorm:   e==0 & f!=0. X=(-1)^s * 2^(0-D) * (0.f)
//0:        e==0 & f==0. X=(-1)^s0
//Norm:     e=(0,2^E-1). X=(-1)^s * 2^(e-B) * (1.f)
```

参见 [cmod/hls/include/nvdla_float.h:16-26](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/hls/include/nvdla_float.h#L16-L26)。把这几行翻译成下表（\( s \)=符号，\( e \)=指数字段，\( f \)=尾数字段，\( B \)=偏置）：

| 类别 | 判定条件 | 数值 |
|------|----------|------|
| 零（Zero） | \( e=0 \) 且 \( f=0 \) | \( (-1)^s \times 0 \) |
| 非规格化（Denorm） | \( e=0 \) 且 \( f\neq 0 \) | \( (-1)^s \times 2^{1-B} \times (0.f)_2 \) |
| 规格化（Norm） | \( 0<e<2^E-1 \) | \( (-1)^s \times 2^{e-B} \times (1.f)_2 \) |
| 无穷（Inf） | \( e=2^E-1 \) 且 \( f=0 \) | \( \pm\infty \) |
| 非数（NaN） | \( e=2^E-1 \) 且 \( f\neq 0 \) | NaN |

规格化数里那个 \( 1.f \) 的「1」就是**隐含的最高位**，不占存储。位段在寄存器里的排列顺序是 **{符号, 指数, 尾数}**（符号在最高位），由 `FpSignedBitsToFloat` 这个模板函数负责拆解：

```cpp
o_mant = ubits.template slc<MantWidth>(0);            // 低位段 = 尾数
o_expo = ubits.template slc<ExpoWidth>(MantWidth);    // 中段 = 指数
o_sign = ubits.template slc<1>(MantWidth+ExpoWidth);  // 最高位 = 符号
```

参见 [cmod/hls/include/nvdla_float.h:115-129](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/hls/include/nvdla_float.h#L115-L129)。所以对一个 fp17 数，位 `bit[16]` 是符号、`bit[15:10]` 是指数、`bit[9:0]` 是尾数。

舍入方式统一采用 **RNE（Round to Nearest Even，就近舍入到偶数）**，注释里那句「4drop,6carry,5makeLSBeven」的意思是：被截掉的尾数若大于一半（…1x…）就进位、小于一半（…0…）就舍去、恰好一半（…10…0）则把保留位的最低位凑成偶数，避免统计偏差。

#### 4.1.3 源码精读：fp16→fp17 的指数偏置

fp16→fp17 只拓宽指数，所以核心就是「给指数字段加上偏置差」。在 C++ 源里：

```cpp
const unsigned int koExpoBias = (1ull<< oExpoWidth)/2 - 1;   // fp17 偏置 = 31
const unsigned int kiExpoBias = (1ull<< iExpoWidth)/2 - 1;   // fp16 偏置 = 15
const unsigned int kExpoBiasDelta = koExpoBias - kiExpoBias; // 差 = 16
...
o_expo = (oExpoType)(i_expo + kExpoBiasDelta);               // 规格化数：指数 +16
```

参见 [cmod/hls/include/nvdla_float.h:446-448](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/hls/include/nvdla_float.h#L446-L448) 与 [cmod/hls/include/nvdla_float.h:474-478](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/hls/include/nvdla_float.h#L474-L478)。也就是说，一个规格化 fp16 数转成 fp17，**数值完全不变**，只是指数字段从 \( e_{16} \) 变成 \( e_{16}+16 \)（因为偏置也大了 16，真实指数 \( e-B \) 抵消掉了）。这正是 fp17 能「无损容纳」fp17 范围内所有 fp16 数的根本原因。

而具体到生成的 RTL，在 `HLS_fp16_to_fp17_core` 里能直接看到同样的字段切分与判定（符号位 `bit[15]` 直接传递，指数段 `bit[14:10]` 判 Inf/Zero）：

```verilog
assign IsInf_5U_10U_IsInf_5U_10U_and_cse_sva = (chn_a_rsci_d_mxwt[14:10]==5'b11111); // fp16 指数全1 => Inf/NaN
assign IsZero_5U_10U_IsZero_5U_10U_nor_cse_sva = ~((chn_a_rsci_d_mxwt[14:10]!=5'b00000)); // 指数全0 => Zero/Denorm
...
chn_o_rsci_d_16 <= chn_a_rsci_d_mxwt[15];   // 符号位直接传过去（fp16 bit15 -> fp17 bit16）
```

参见 [vmod/vlibs/HLS_fp16_to_fp17.v:790-796](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/HLS_fp16_to_fp17.v#L790-L796) 与符号传递 [vmod/vlibs/HLS_fp16_to_fp17.v:851](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/HLS_fp16_to_fp17.v#L851)。命名里的 `5U_10U`、`6U_10U` 就是 Catapult 用来标记「指数 5 位/6 位、尾数 10 位」的模板实例化编号。

#### 4.1.4 代码实践：手算一次 fp16→fp17

1. **实践目标**：亲手验证「fp16 转 fp17 只是指数 +16、数值不变」。
2. **操作步骤**：
   - 取一个规格化 fp16，例如 `1.5`，其二进制为 \( 1.1_2 \times 2^0 \)。fp16 编码：符号 0、指数 \( e=0+15=15=\texttt{01111} \)、尾数 \( f=\texttt{1000000000} \)，完整 16 位 = `0 01111 1000000000`。
   - 按 fp16→fp17 规则，指数字段 +16：\( 15+16=31=\texttt{011111} \)（fp17 的 6 位指数），尾数不变 `1000000000`，符号 0。
   - 得到 fp17：`0 011111 1000000000`。
3. **需要观察的现象**：fp17 指数字段 `011111` 换算回真实指数 = \( 31 - 31(\text{fp17 偏置}) = 0 \)，与原 fp16 真实指数 \( 15-15=0 \) 一致；数值仍为 \( 1.1_2 \times 2^0 = 1.5 \)。
4. **预期结果**：转换前后数值完全相等，只是指数位宽从 5 变 6、字段值从 15 变 31。这印证了 fp17 是 fp16 的「无损扩展」。
5. 该结果为纯位运算推导，**待本地验证**（可用任意 fp16 转 fp17 的脚本对照）。

#### 4.1.5 小练习与答案

- **练习 1**：fp17 的最大规格化数大约比 fp16 大多少倍？
  - **答案**：fp17 指数最大真实值 \( 31 \)，fp16 为 \( 15 \)，相差 16，即约 \( 2^{16}\approx 65536 \) 倍的「最大值」提升（尾数精度不变）。
- **练习 2**：为什么 fp17 的尾数故意保持 10 位、不和指数一起加宽？
  - **答案**：尾数决定**精度**，乘法器面积约与 \( M^2 \) 成正比；保持 10 位就能复用与 fp16 同规模的 11×11 乘法器。指数只决定**动态范围**，多 1 位指数几乎不增加面积，却能消除累加溢出。这是「把面积花在刀刃上」。

---

### 4.2 HLS 流式运算单元的统一接口与生成机制

#### 4.2.1 概念说明

本讲的所有 `HLS_fp*` 模块都不是手写的 Verilog，而是 **Catapult HLS 工具**从 `nvdla_float.h` 的 C++ 模板自动生成的网表。每个文件顶部都留着生成记录，例如：

```
//  HLS HDL:        Verilog Netlister
//  HLS Version:    10.0/264918 Production Release
//  Generated by:   ezhang@...
```

参见 `HLS_fp17_mul.v` 的文件头 [vmod/vlibs/HLS_fp17_mul.v:77-85](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/HLS_fp17_mul.v#L77-L85)。**因此读这些文件的正确姿势是：先读 C++ 源（`nvdla_float.h`）理解算法，再把 RTL 当作「算法的电路实现」来对照，而不是逐行去啃那几千行布尔表达式。** 网表里甚至保留了指向 C++ 源的断言注释，例如：

```
// assert(iMantWidth > oMantWidth) - ../include/nvdla_float.h: line 386
```

参见 [vmod/vlibs/HLS_fp17_mul.v:1003-1004](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/HLS_fp17_mul.v#L1003-L1004)。这条注释直接把 RTL 与 `nvdla_float.h` 第 386 行的断言挂钩，是「网表←→源」的导航线索。

每个运算/转换单元对外都呈现**同一个流式接口模板**：一或两个输入「通道（channel）」、一个输出通道，每条通道都带 valid/ready 握手。

#### 4.2.2 核心流程：通道式握手

以乘法器 `HLS_fp17_mul` 的顶层端口为例：

```verilog
module HLS_fp17_mul (
  nvdla_core_clk, nvdla_core_rstn,
  chn_a_rsc_z, chn_a_rsc_vz, chn_a_rsc_lz,   // 输入通道 a：数据/有效/就绪
  chn_b_rsc_z, chn_b_rsc_vz, chn_b_rsc_lz,   // 输入通道 b：数据/有效/就绪
  chn_o_rsc_z, chn_o_rsc_vz, chn_o_rsc_lz    // 输出通道 o：数据/有效/就绪
);
  input  [16:0] chn_a_rsc_z;  input chn_a_rsc_vz;  output chn_a_rsc_lz;
  input  [16:0] chn_b_rsc_z;  input chn_b_rsc_vz;  output chn_b_rsc_lz;
  output [16:0] chn_o_rsc_z;  input chn_o_rsc_vz;  output chn_o_rsc_lz;
```

参见 [vmod/vlibs/HLS_fp17_mul.v:1648-1662](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/HLS_fp17_mul.v#L1648-L1662)。命名规律（`rsci` = resource interface，Catapult 的流式接口）：

| 后缀 | 含义 | 方向 |
|------|------|------|
| `_rsc_z` | 数据（data） | 输入通道=输入；输出通道=输出 |
| `_rsc_vz` | 与数据同向的「有效/就绪」之一 | 见下 |
| `_rsc_lz` | 与数据同向的「有效/就绪」之二 | 见下 |

`vz`/`lz` 哪个是 valid、哪个是 ready，要看通道朝向。从调用方的连接名能一眼看穿——CDP 里把它接成了标准的 `pvld`（producer valid）/`prdy`（producer ready）：

```verilog
HLS_fp17_mul u_fp_mul (
   .chn_a_rsc_z     (datin_pd[16:0])      // 输入数据 a
  ,.chn_a_rsc_vz    (fp_mul_a_vld)        // a 有效(valid)
  ,.chn_a_rsc_lz    (fp_mul_a_rdy)        // a 就绪(ready)
  ,.chn_b_rsc_z     (intp2mul_pd_0[16:0]) // 输入数据 b
  ,.chn_b_rsc_vz    (fp_mul_b_vld)
  ,.chn_b_rsc_lz    (fp_mul_b_rdy)
  ,.chn_o_rsc_z     (fp_mul_pd[16:0])     // 输出数据
  ,.chn_o_rsc_vz    (fp_mul_rdy)          // 下游就绪(ready)
  ,.chn_o_rsc_lz    (fp_mul_vld)          // 输出有效(valid)
);
```

参见 [vmod/nvdla/cdp/NV_NVDLA_CDP_DP_MUL_unit.v:158-170](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_DP_MUL_unit.v#L158-L170)。于是整套接口可以归纳为一句话：**输入通道是 `z`(数据)+`vz`(valid)+`lz`(ready)，输出通道是 `z`(数据)+`lz`(valid)+`vz`(ready)，一次传输发生在 valid 与 ready 同时为真的时钟沿。** 这与 u6-l2 讲的流式握手完全一致，只是信号名换成了 Catapult 风格。

每个单元内部还有三件标准的「控制壳」子模块，所有 `HLS_fp*` 都长得一样：

- `*_core_fsm`：一个两状态的小状态机（`core_rlp_C_0` 复位/释放态、`main_C_0` 主态），见 [vmod/vlibs/HLS_fp17_mul.v:145-189](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/HLS_fp17_mul.v#L145-L189)。
- `*_core_staller`：把所有通道的「写完成」信号 `wen_comp` 相与得到 `core_wen`，实现**反压停顿**——任意一路没就绪，整个运算就停一拍，见 [vmod/vlibs/HLS_fp17_mul.v:212](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/HLS_fp17_mul.v#L212)。
- `*_chn_*_wait_ctrl / wait_dp`：每个通道一对，做单拍数据缓存（skid buffer）以安全应付反压。

#### 4.2.3 代码实践：对照「C++ 源 ↔ RTL 网表」

1. **实践目标**：体会「读 C++ 源远比读网表高效」，并验证两者对应关系。
2. **操作步骤**：
   - 打开 [cmod/hls/include/nvdla_float.h:1650-1669](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/hls/include/nvdla_float.h#L1650-L1669)，看到 `Fp17Mul = FpMul<6,10>`、`Fp17Add = FpAdd<6,10>`、`Fp17Sub = FpSub<6,10>` 等一行行的 inline 包装。
   - 再打开 [vmod/vlibs/HLS_fp17_mul.v:1003-1004](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/HLS_fp17_mul.v#L1003-L1004) 的断言注释，看它如何指回 `nvdla_float.h`。
3. **需要观察的现象**：C++ 里一个 `FpMul<6,10>` 模板，在 RTL 里膨胀成 1700 多行的布尔逻辑；但算法语义（乘尾数、加指数减偏置、规格化、RNE 舍入、NaN 传播）完全来自那几十行 C++。
4. **预期结果**：建立「**算法读 .h，实现看 .v**」的阅读习惯。今后遇到任何 `HLS_*` 网表，先去找它对应的 C++ 模板。
5. 该实践为源码阅读型，无需运行，结论可直接从源码得出。

#### 4.2.4 小练习与答案

- **练习**：`HLS_fp17_mul` 内部 `staller` 为什么要把 `chn_a/chn_b/chn_o` 三个通道的 `wen_comp` 全部 AND 起来？
  - **答案**：乘法需要两个输入都到齐、且下游愿意接输出，才能在这一拍真正算并交付。任意一个条件不满足（某路输入未 valid、或下游反压），就必须停顿（stall），否则会丢数据或写冲掉未读结果。

---

### 4.3 加减乘运算单元（add/sub/mul）

#### 4.3.1 概念说明

三种运算在 C++ 源里都是同一组指数位宽（6）+ 尾数位宽（10）的模板实例，包装在文件末尾：

```cpp
inline ACINTT(17) Fp17Mul (ACINTT(17) a, ACINTT(17) b) { return FpMul<6,10>(a,b); }
inline ACINTT(17) Fp17Add (ACINTT(17) a, ACINTT(17) b) { return FpAdd<6,10>(a,b); }
inline ACINTT(17) Fp17Sub (ACINTT(17) a, ACINTT(17) b) { return FpSub<6,10>(a,b); }
```

参见 [cmod/hls/include/nvdla_float.h:1650-1669](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/hls/include/nvdla_float.h#L1650-L1669)。`Fp17Sub` 的实现极其巧妙——**减法就是「把 b 的符号位翻转，再调用加法」**：

```cpp
template<...> ACINTT(...) FpSub (a, b) {
    FpSignedBitsToFloat<...>(b, b_sign, b_expo, b_mant);
    b_sign = ~b_sign;                                   // 翻转符号
    b = FpFloatToSignedBits<...>(b_sign, b_expo, b_mant);
    return FpAdd<...>(a, b);                            // 复用加法器
}
```

参见 [cmod/hls/include/nvdla_float.h:1141-1161](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/hls/include/nvdla_float.h#L1141-L1161)。所以 RTL 里没有独立的 `HLS_fp17_sub` 减法器——要减就翻转符号再用 add。

#### 4.3.2 核心流程：乘法 FpMul

浮点乘法的标准三步：**尾数相乘、指数相加减偏置、规格化+舍入**。算法在 C++ 里清晰可见：

```cpp
pMantPlusOneType p_mant_p1 = a_mant_p1 * b_mant_p1;   // 1. 尾数相乘（含隐含1，11x11->22）
p_sign = a_sign ^ b_sign;                             //    符号 = 异或
...
p_expo = a_expo + b_expo - kExpoBias;                 // 2. 指数相加减偏置(31)
// 3. 规格化：若乘积最高位进位则指数+1、否则左移1位
if (p_mant_p1[pMantWidth]) { p_expo++; p_mant = p_mant_p1; }
else                       { p_mant = p_mant_p1<<1; }
```

参见 [cmod/hls/include/nvdla_float.h:1343-1370](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/hls/include/nvdla_float.h#L1343-L1370)。其中 \( a\_mant\_p1 \) 是「尾数前面补一个隐含 1」，所以是 11 位；两个 11 位相乘得 22 位（\( pMantWidth = 2M+1 = 21 \)，加隐含进位位共 22）。算完后还要用 `FpMantWidthDec` 把宽尾数**舍入回收**到 10 位（RNE），并处理上溢→Inf、下溢→Zero、NaN 传播。

RTL 网表里能精确定位到那一步「带隐含 1 的尾数乘法」和「符号异或」：

```verilog
// 11位(含隐含1) x 11位(含隐含1) -> 22位 尾数乘积
assign FpMul_6U_10U_p_mant_p1_mul_tmp =
    conv_u2u_22_22( ({1'b1, ua_sva[9:0]}) * ({1'b1, ub_sva[9:0]}) );
...
assign FpMul_6U_10U_xor_1_nl = (chn_a_rsci_d_mxwt[16]) ^ (chn_b_rsci_d_mxwt[16]); // 符号异或
```

参见尾数乘法 [vmod/vlibs/HLS_fp17_mul.v:1051-1052](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/HLS_fp17_mul.v#L1051-L1052) 与符号异或 [vmod/vlibs/HLS_fp17_mul.v:1398](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/HLS_fp17_mul.v#L1398)。`{1'b1, ...}` 就是 C++ 里那个「补隐含 1」的电路体现。运算主体在 [HLS_fp17_mul_core（vmod/vlibs/HLS_fp17_mul.v:727）](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/HLS_fp17_mul.v#L727)。

#### 4.3.3 核心流程：加法 FpAdd

浮点加法比乘法麻烦，要先**对阶**（把小指数的那个数右移，使两数指数相同），再加减尾数，最后规格化：

```cpp
bool is_a_greater = (a_expo > b_expo) || (a_expo==b_expo && a_mant>=b_mant);
o_expo = is_a_greater ? a_expo : b_expo;                 // 取大指数
ShiftType a_right_shift = is_a_greater ? 0 : (b_expo - a_expo); // 小的那个右移对阶
...
bool is_addition = (a_sign == b_sign);                   // 同号加、异号减
if (is_addition) int_mant_p1 = addend_larger + addend_smaller;
else             int_mant_p1 = addend_larger - addend_smaller;
// 之后 FpNormalize 规格化 + FpMantRNE 舍入
```

参见 [cmod/hls/include/nvdla_float.h:1031-1066](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/hls/include/nvdla_float.h#L1031-L1066)。注意对阶时为了不丢精度，C++ 把尾数先左移到一个更宽的 `InternalMantWidth`（\( = 2M+3 \)）再右移对阶，等价于在低位保留额外的 guard/round/stick 位，最后一次性 RNE 舍入。RTL 里对应的就是 [HLS_fp17_add_core（vmod/vlibs/HLS_fp17_add.v:955）](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/HLS_fp17_add.v#L955) 里那一大段 `FpAdd_6U_10U_int_mant_p1_sva`、`leading_sign`（前导 1/0 检测，用于规格化）和 `FpMantRNE` 的布尔逻辑。加法顶层端口见 [vmod/vlibs/HLS_fp17_add.v:1933-1947](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/HLS_fp17_add.v#L1933-L1947)（两个 `[16:0]` 输入、一个 `[16:0]` 输出）。

> fp32 的 `HLS_fp32_mul`（[顶层 vmod/vlibs/HLS_fp32_mul.v:1652](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/HLS_fp32_mul.v#L1652)）与 `HLS_fp32_add`（[顶层 vmod/vlibs/HLS_fp32_add.v:1950](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/HLS_fp32_add.v#L1950)）算法与 fp17 版完全同构，只是模板参数换成 `FpMul<8,23>` / `FpAdd<8,23>`（见 [cmod/hls/include/nvdla_float.h:1671-1690](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/hls/include/nvdla_float.h#L1671-L1690)），尾数乘法变成 24×24→48，面积大得多。

#### 4.3.4 代码实践：读测试理解 fp17 乘法行为

1. **实践目标**：通过阅读 CDP 乘法单元的连接，反推 fp17 乘法「何时算、结果给谁」。
2. **操作步骤**：
   - 阅读 [NV_NVDLA_CDP_DP_MUL_unit.v:149-181](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_MUL_unit.v#L149-L181)。
   - 注意 `mul_fp_vld = fp16_en_sync ? mul_vld : 1'b0;`——只有 fp16 模式启用时，这个 fp17 乘法器才真正被激活；int16 模式走另一条定点乘法路径。
   - 注意输出选择 `mul_unit_pd = fp16_en_sync ? {... fp_mul_pd} : ...`——fp17 乘积 `fp_mul_pd[16:0]` 被符号扩展成宽位后送出。
3. **需要观察的现象**：同一组端口 `chn_a/chn_b/chn_o`，在 fp16 模式下走 `HLS_fp17_mul`，在 int16 模式下走定点乘法器，由 `fp16_en_sync` 选择。
4. **预期结果**：理解 NVDLA 用「精度使能位」复用同一套数据通路——fp 与 int 共享周边选择逻辑，只在乘法核心处分流。
5. 该实践为源码阅读型，**待本地验证**（若要观测波形，可参考 [u1-l4](u1-l4-first-simulation.md) 跑一个 fp16 模式的 CDP trace）。

#### 4.3.5 小练习与答案

- **练习 1**：用 fp17 算 \( 1.5 \times 2.0 \)。\( 1.5=\texttt{0 011111 1000000000} \)（见 4.1.4），\( 2.0=1.0\times2^1 \)，fp17 指数 \( e=1+31=32=\texttt{100000} \)、尾数全 0。写出乘积。
  - **答案**：尾数 \( 1.1_2 \times 1.0_2 = 1.1_2 \)（即 1.5），指数 \( 0+1=1 \)，符号 0；所以结果 = `0 100000 1000000000`，数值 \( 3.0 \)。指数字段 32 对应真实指数 \( 32-31=1 \)，\( 1.5\times2^1=3.0 \) ✓。
- **练习 2**：为什么 `Fp17Sub` 不单独做硬件，而是「翻符号 + FpAdd」？
  - **答案**：减法与加法只差一个符号位的处理，复用加法器能把面积减半，且对阶/规格化/舍入逻辑完全通用。这是浮点单元设计里的经典复用。

---

### 4.4 格式互转单元（fp16↔fp17↔fp32）

#### 4.4.1 概念说明

转换决定「数据进/出 fp17 内部域」的边界。NVDLA 内部的浮点数据流典型路径是：**fp16（存储）→ fp17（运算）→ fp32（宽中间累加）→ fp17 → fp16（写回）**。各转换在 C++ 里都是 `FpExpoWidthInc/Dec`（只变指数）或 `FpWidthInc/Dec`（指数尾数一起变）的实例：

```cpp
inline ACINTT(1+6+10) Fp16ToFp17 (ACINTT(1+5+10) bits) { return FpExpoWidthInc<5,6,10,...>(bits); } // 只拓宽指数
inline ACINTT(1+5+10) Fp17ToFp16 (ACINTT(1+6+10) bits) { return FpExpoWidthDec<6,5,10,...>(bits); } // 只收窄指数
inline ACINTT(1+8+23) Fp17ToFp32 (ACINTT(1+6+10) bits) { return FpWidthInc<6,10,8,23,...>(bits);  } // 指数+尾数都拓宽
inline ACINTT(1+6+10) Fp32ToFp17 (ACINTT(1+8+23) bits) { return FpWidthDec<8,23,6,10,...>(bits);  } // 指数+尾数都收窄
```

参见 [cmod/hls/include/nvdla_float.h:914-935](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/hls/include/nvdla_float.h#L914-L935)。

#### 4.4.2 核心流程：拓宽 vs 收窄的难易

- **拓宽（Inc，小→大）**：几乎无损。指数加偏置差、尾数低位补 0，再处理 denorm（把非规格化数规格化）和 Inf/NaN 传播。fp16→fp17 只拓宽指数，最简单。
- **收窄（Dec，大→小）**：**有损**，必须舍入。可能发生：上溢→置 Inf、下溢→置 0 或 denorm、正常值→RNE 舍入。所以 `Fp17ToFp16`（指数 6→5）比 `Fp16ToFp17` 复杂得多，要处理指数超出 fp16 范围（\([-14,15]\)）的情况。

各转换顶层端口宽度直接暴露了「谁拓宽/收窄了什么」：

| 模块 | 输入宽度 | 输出宽度 | 变化 |
|------|----------|----------|------|
| [HLS_fp16_to_fp17 顶层](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/HLS_fp16_to_fp17.v#L984-L995) | `[15:0]` | `[16:0]` | +1 位指数 |
| [HLS_fp17_to_fp16 顶层](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/HLS_fp17_to_fp16.v#L1303-L1314) | `[16:0]` | `[15:0]` | −1 位指数（含舍入） |
| [HLS_fp17_to_fp32 顶层](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/HLS_fp17_to_fp32.v#L890-L901) | `[16:0]` | `[31:0]` | +2 指数 +13 尾数（无损拓宽） |

#### 4.4.3 源码精读：fp16→fp17 的 denorm 处理

拓宽时要特别照顾 denorm（非规格化数）。C++ 源里 `FpExpoWidthInc` 对 denorm 做了「前导零计数 + 左移」把它升格为规格化数：

```cpp
if (is_denorm) {
    MantType zero_count = IntLeadZero<MantWidth>(i_mant);   // 数尾数前导0
    o_expo = kExpoBiasDelta - zero_count;                   // 重算指数
    o_mant = ((MantPlusOneType)i_mant)<<(zero_count+1);     // 左移规格化
}
```

参见 [cmod/hls/include/nvdla_float.h:480-488](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/hls/include/nvdla_float.h#L480-L488)。对应的 RTL 网表里，`HLS_fp16_to_fp17_core` 例化了一个 `FP16_TO_FP17_leading_sign_10_0`（前导 1/0 检测器）和 `FP16_TO_FP17_mgc_shift_l_v4`（桶形左移器）来完成这件事，参见 [vmod/vlibs/HLS_fp16_to_fp17.v:723-734](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/HLS_fp16_to_fp17.v#L723-L734)。所以 fp16 的 denorm 进入 fp17 后会被「免费」规格化——这也是 fp17 指数余量带来的好处之一。

#### 4.4.4 代码实践：统计 CDP 里转换器的典型接法

1. **实践目标**：看清 fp16→fp17 在 CDP 入口、fp17→fp32 在求和出口的真实连线。
2. **操作步骤**：
   - 看 CDP 的格式转换单元 [cdp/fp_format_cvt.v:127-147](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/fp_format_cvt.v#L127-L147)：`HLS_fp16_to_fp17 u_X_fp16_to_fp17` 把输入特征 `fp16to17_in_X0[15:0]` 转成 `fp16to17_out_X0[16:0]`，注意它和 `HLS_fp16_to_fp32`、`HLS_uint16_to_fp17` 并列存在——按精度模式选不同转换入口。
   - 看 CDP 求和块 [NV_NVDLA_CDP_DP_sum.v:1096-1217](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_DP_sum.v#L1096-L1217)：一连串 12 个 `HLS_fp17_to_fp32 u_fp17_to_fp32_*`，把 fp17 的部分和拓宽成 fp32 再去累加（求和需要更宽的动态范围防溢出）。
3. **需要观察的现象**：入口用「只拓宽指数」的 fp16→fp17（省面积），出口求和用「拓宽指数+尾数」的 fp17→fp32（保精度防溢出）——两处选择背后的理由不同。
4. **预期结果**：理解「**运算用最窄够用的 fp17，累加用更宽的 fp32**」这条面积/精度折中主线。
5. 该实践为源码阅读型，结论可直接从连线得出。补充：CDP 的插值单元还注释了 `HLS_fp16_to_fp17 unit only 1 cycle latency`（见 [NV_NVDLA_CDP_DP_intp.v:528-529](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_DP_intp.v#L528-L529)），即转换器是单拍组合/单寄存器延迟，因此该处甚至省略了完整的 valid/ready 握手。

#### 4.4.5 小练习与答案

- **练习 1**：`Fp17ToFp32`（指数 6→8、尾数 10→23）会不会丢精度？
  - **答案**：不会。这是「拓宽」，尾数低位补 0、指数加偏置差（\( 127-31=96 \)），数值完全保留；它属于 `FpWidthInc`，是无损的。
- **练习 2**：`Fp17ToFp16`（指数 6→5）一定丢精度吗？
  - **答案**：不一定丢「数值范围内的精度」，但会丢「范围」。落在 fp16 范围 \([-65504, 65504]\) 内的数，尾数宽度相同（都 10 位）所以精度不变，但超出 fp16 指数范围的 fp17 数会被钳到 Inf 或 0；同时 denorm 处理与 RNE 舍入在边界值上会引入误差。

---

### 4.5 FPU 在引擎中的使用场景

#### 4.5.1 概念说明

这里要对本讲的「学习目标」做一个**重要修正**：经源码核实，`HLS_fp*` 这套浮点单元**只被 CDP 和 PDP 两个后处理引擎例化**，并不出现在 CMAC、CACC、SDP 里。下表是 `vmod/nvdla` 下所有例化点的真实分布：

| 引擎 | 例化的 FPU | 位置 |
|------|-----------|------|
| **CDP**（LRN 跨通道归一化） | `HLS_fp17_mul`（乘 \( f(\Sigma) \) 回原激活） | [NV_NVDLA_CDP_DP_MUL_unit.v:158](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_MUL_unit.v#L158) 所在文件 [NV_NVDLA_CDP_DP_MUL_unit.v:158-170](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_DP_MUL_unit.v#L158-L170) |
| **CDP** | `HLS_fp16_to_fp17`（入口定点/浮点→内部域） | [NV_NVDLA_CDP_DP_intp.v:529-565](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_DP_intp.v#L529-L565)、[fp_format_cvt.v:127-138](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/fp_format_cvt.v#L127-L138) |
| **CDP** | `HLS_fp17_to_fp32`（求和拓宽防溢出） | [NV_NVDLA_CDP_DP_sum.v:1096-1217](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_DP_sum.v#L1096-L1217) |
| **PDP**（空间池化） | `HLS_fp16_to_fp17`（输入转内部域） | [NV_NVDLA_PDP_CORE_cal1d.v:524-554](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/pdp/NV_NVDLA_PDP_CORE_cal1d.v#L524-L554) |
| **PDP** | `HLS_fp17_mul`（average 池化乘倒数，多处） | [NV_NVDLA_PDP_CORE_cal2d.v:915](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/pdp/NV_NVDLA_PDP_CORE_cal2d.v#L915)（首个实例，另见 9304–9421） |
| **PDP** | `HLS_fp17_add`（求和累加） | [pdp/fp16_4add.v:72-114](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/pdp/fp16_4add.v#L72-L114) |
| **PDP** | `HLS_fp17_to_fp16`（输出转回 fp16） | [NV_NVDLA_PDP_CORE_cal2d.v:9439-9469](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/pdp/NV_NVDLA_PDP_CORE_cal2d.v#L9439-L9469) |
| CMAC / CACC / SDP | **不例化** `HLS_fp*` | —— |

#### 4.5.2 核心流程：为什么是 CDP/PDP，而不是 CMAC/CACC

这背后的原因是**并行度与算术形态**的差异：

- **CMAC（卷积乘加阵列）**：单拍要做 1024～2048 个乘加，且输入是定点 int8/int16/fp16。在这种「极高吞吐、定点为主」的场景，NVDLA 用的是**自研的 Booth 基 4 编码乘法器 + CSA 压缩树 + exp/nan 单元**（见 [u3-l5 CMAC 讲义](u3-l5-cmac-mac-array.md)），它在 fp16 模式下自行做指数对齐与特殊值处理，**不复用**这套低吞吐的 `HLS_fp17_mul`。把一个流式、带反压停顿的 `HLS_fp17_mul` 复制 1024 份既慢又费面积。
- **CACC（累加器）**：累加用**定宽整数累加器**（int8 路径 34 位、int16/fp16 路径 48 位，见 [u3-l6 CACC 讲义](u3-l6-cacc-accumulator.md)），靠位宽裕量防溢出，不需要浮点加法器。
- **CDP / PDP**：做的是**逐通道 / 逐空间窗口的标量运算**（LRN 的平方和、池化的求和与乘倒数），并行度只有十几路，且涉及平方、除法倒数、非线性插值——**动态范围大、难定点化**，所以适合用一套小而全的流式 FPU。fp17 正是为这种「少量、需要指数余量」的场合量身定做。

一句话总结：**哪里算力密集且定点够用（CMAC/CACC），哪里就不用 FPU；哪里算力稀疏但数值范围棘手（CDP/PDP），哪里才上 fp17 FPU。**

#### 4.5.3 代码实践：跟踪一条完整的 fp16→fp17→fp32 数据链

1. **实践目标**：把本讲学的转换与运算单元，串成 CDP 里一条真实的浮点数据通路。
2. **操作步骤**：
   - 起点：CDP 输入特征图是 fp16。
   - 第 1 步（入口转换）：fp16 经 `HLS_fp16_to_fp17` 进入 fp17 内部域，见 [fp_format_cvt.v:127-136](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/fp_format_cvt.v#L127-L136)（`fp16to17_in_X0[15:0]` → `fp16to17_out_X0[16:0]`）。
   - 第 2 步（乘法）：fp17 激活与 fp17 的 \( f(\Sigma) \) 在 `HLS_fp17_mul` 相乘，见 [NV_NVDLA_CDP_DP_MUL_unit.v:158-170](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_MUL_unit.v#L158-L170)（`datin_pd[16:0]` × `intp2mul_pd_0[16:0]` → `fp_mul_pd[16:0]`）。
   - 第 3 步（求和拓宽）：邻域平方和需要宽动态范围，fp17 部分和经 `HLS_fp17_to_fp32` 拓宽成 fp32 再累加，见 [NV_NVDLA_CDP_DP_sum.v:1096-1129](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_DP_sum.v#L1096-L1129)。
   - 第 4 步（出口转换）：最终结果经定点化（cvtout）写回存储（CDP 对外仍是 fp16/int16）。
3. **需要观察的现象**：整条链路「**入口用最省的 fp16→fp17，运算用 fp17，求和用最宽的 fp32**」，每一处的位宽选择都对应一个明确的理由（省面积 / 够用 / 防溢出）。
4. **预期结果**：能画出 `fp16 →(to_fp17)→ fp17 →(mul)→ fp17 →(to_fp32)→ fp32 →(sum)→ ... → fp16` 的数据流图，并标注每段为何选这个精度。
5. 该实践为源码阅读与画图型，连线已在源码中给出；若要在波形上确认，**待本地验证**（跑一个 fp16 模式的 CDP sanity trace，参考 [u1-l4](u1-l4-first-simulation.md) 与 [u5-l3 CDP 讲义](u5-l3-cdp-lrn.md)）。

#### 4.5.4 小练习与答案

- **练习 1**：如果让 CMAC 也改用 `HLS_fp17_mul` 来做 fp16 卷积，主要会损失什么？
  - **答案**：吞吐和面积。CMAC 需要 1024+ 路同拍乘加，而 `HLS_fp17_mul` 是带反压停顿的流式单路运算；复制上千份既占巨大面积，又因流式握手无法像 Booth+CSA 压缩树那样做到单拍高吞吐。
- **练习 2**：CDP 求和为什么用 fp17→fp32，而不直接在 fp17 里累加？
  - **答案**：LRN 的平方和会把多个通道的平方值相加，动态范围会涨；fp17 的 6 位指数在多次累加后可能溢出。fp32 有 8 位指数 + 23 位尾数，专门用来吃下这种累加余量，算完再转回 fp17/fp16。

---

## 5. 综合实践

**任务：为 CDP 的 LRN 一条数据通路，写出「精度选择理由表」并对照源码核实。**

LRN（局部响应归一化）的核心是 \( b_x = a_x \cdot (k + \alpha \sum_{n} a_n^2)^{-\beta} \)。它涉及「平方求和（sum）」「查表插值（lut/intp）」「乘回原值（mul）」三段，正好覆盖本讲全部三类单元。

请按下列步骤完成：

1. **画出精度链路**：参考 4.5.3，标注数据在每一段的格式（fp16 / fp17 / fp32）。
2. **填精度选择理由表**：

   | 段 | 运算 | 输入格式 | 运算格式 | 输出格式 | 选择该精度的理由 |
   |----|------|----------|----------|----------|------------------|
   | 入口 | —— | fp16 | —— | fp17 | ？（提示：拓宽指数无损、省面积） |
   | sum | 平方求和 | fp17 | fp32 | fp32 | ？（提示：防累加溢出） |
   | mul | \( a_x \cdot f(\Sigma) \) | ? | ? | ? | ？（自行填写） |
   | 出口 | —— | ? | —— | fp16 | ？ |

3. **源码核实**：对表中每一格，给出一个本讲引用过的永久链接作为证据（如入口转换用 [fp_format_cvt.v:127](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/fp_format_cvt.v#L127)，mul 用 [CDP_DP_MUL_unit.v:158](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_DP_MUL_unit.v#L158)）。
4. **回答关键问题**：为什么 NVDLA 内部不直接全程用 fp32？请用「精度、动态范围、面积」三个维度，结合 fp17（11×11 乘法器）与 fp32（24×24 乘法器）的面积差异作答。

> 这道题把「格式定义、转换、运算、使用场景」四个最小模块串到了一起。完成后，你应当能脱稿讲清「一个 fp16 数在 NVDLA 内部走了一圈浮点运算，经历了哪些格式、为什么」。

---

## 6. 本讲小结

- NVDLA 内部有三种浮点格式：**fp16**（对外，5+10）、自研 **fp17**（内部运算，**6+10**，比 fp16 多 1 位指数、尾数相同）、**fp32**（中间累加，8+23）；均按 `{符号,指数,尾数}` 排列，统一用 RNE 舍入。
- **fp17 的本质是「fp16 的指数加宽版」**：精度不变、动态范围翻倍，用来给运算留溢出余量；fp16→fp17 对规格化数就是「指数 +16」、数值无损。
- 所有 `HLS_fp*` 单元都是 **Catapult 从 `cmod/hls/include/nvdla_float.h` 的 C++ 模板生成的网表**；读源码的正确姿势是「**算法读 .h，实现看 .v**」，网表里还留着指回 C++ 的断言注释。
- 每个单元对外是统一的**流式 valid/ready 接口**（`chn_x_rsc_z/vz/lz`），内部由 `core_fsm + staller + 通道 wait_ctrl/wait_dp` 组成，靠 staller 把各通道「写完成」相与来实现反压停顿。
- **乘法** = 尾数相乘（含隐含 1）+ 指数相加减偏置 + 规格化 + RNE；**加法** = 对阶 + 加/减尾数 + 规格化 + RNE；**减法** = 翻符号 + 加法（不单独做硬件）。
- 转换分**拓宽（Inc，无损）**与**收窄（Dec，需舍入、可能钳到 Inf/0）**；fp16→fp17 只拓宽指数，fp17→fp32 拓宽指数+尾数，二者都无损。
- **使用场景修正**：这套 FPU **只用在 CDP 与 PDP**（算力稀疏但数值范围棘手的标量运算）；**CMAC/CACC/SDP 不用**——CMAC 用自研 Booth 乘法树、CACC 用定宽整数累加器，因为那里算力密集且定点够用。

---

## 7. 下一步学习建议

- **顺着使用场景深入 CDP/PDP**：读 [u5-l3 CDP：通道数据处理器（LRN）](u5-l3-cdp-lrn.md) 与 [u5-l2 PDP：平面数据处理器（池化）](u5-l2-pdp-pooling.md)，看本讲的 FPU 单元是如何嵌进 LRN 的 `cvtin→sum→lut→intp→mul→cvtout` 与池化的 `cal1d→cal2d` 流水里的。
- **对照定点路径**：读 [u3-l5 CMAC：乘加阵列](u3-l5-cmac-mac-array.md)，对比 CMAC 的自研 Booth+CSA 乘法器与本讲的 `HLS_fp17_mul`，体会「高吞吐定点」与「低吞吐浮点」两套硬件的取舍。
- **读 C++ 源练内功**：把 [cmod/hls/include/nvdla_float.h](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/hls/include/nvdla_float.h) 从头读一遍，重点是 `FpMul`（[L1282](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/hls/include/nvdla_float.h#L1282)）、`FpAdd`（[L973](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/hls/include/nvdla_float.h#L973)）和 `FpMantRNE`（[L219](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/hls/include/nvdla_float.h#L219)），这是理解所有 `HLS_fp*` 网表的最短路径。
- **下一讲**：[u6-l5 重定时与 eperl 流水线插件](u6-l5-retiming-eperl-plugins.md) 将离开「库单元」，转去看 NVDLA 如何用模板插件在模块间自动插入流水寄存器（retiming），与本讲的 HLS 生成机制一样，都属于「用工具/模板批量生成 RTL」的工程化思路。
