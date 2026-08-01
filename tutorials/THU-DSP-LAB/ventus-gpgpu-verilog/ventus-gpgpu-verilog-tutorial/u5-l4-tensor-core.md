# 张量核 tensor core

## 1. 本讲目标

张量核（Tensor Core）是现代 GPU 中专门用来加速「矩阵乘加」\(D=A\times B+C\) 的高吞吐单元。本讲聚焦 Ventus GPGPU（Verilog 版）中张量核的 RTL 实现，学完后你应当能够：

- 说清 `TC_DIM_M / TC_DIM_N / TC_DIM_K` 三个维度参数各自代表矩阵的哪一维，以及它们如何被「打包」进一个向量寄存器；
- 数出一次 `tc_dot_product` 点积到底需要多少次乘、多少次加，并算出全张量核一次运算的乘加（MAC）总数；
- 梳理 `tc_mul_pipe`（流水化乘法）与 `tc_add_pipe`（流水化加法）的流水级如何用一棵二叉归约树累加出最终结果；
- 理解 `VFTTA_VV` 这条张量指令是如何被译码、被 `issue` 路由到张量核、再把结果写回向量寄存器堆的。

本讲承接 u4-l3（乘法器）与 u4-l4（浮点单元）：张量核的底层乘/加核与浮点单元同源（都是 `fadd/fmul` 风格的流水段），但把它们组织成了面向矩阵的二维阵列。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（来自前置讲义）：

- **SIMT 与向量寄存器堆**：一条向量指令广播给整个 warp，`NUM_THREAD` 个 lane 各持有一份数据；一个向量寄存器是 `NUM_THREAD×32` 位的「一列数据」（u4-l1）。
- **浮点流水段**：浮点乘法通常拆成「分类/对阶 → 尾数相乘 → 规格化/舍入」多拍流水；浮点加法拆成「对阶/远近路径选择 → 尾数加减 → 舍入」（u4-l4）。
- **issue 路由**：`issue.v` 是一个按指令类型把握手接通到唯一执行单元的组合路由器（u3-l4）。
- **矩阵乘基础**：\(C_{ij}=\sum_{n} A_{in}\cdot B_{nj}\)，其中 \(n\) 是「归约维」（内积维），需要对 DIM_N 个乘积求和。

一个关键直觉：**张量核 = 把多个独立的浮点乘法器 + 一棵加法归约树，按矩阵的行列关系硬连线织成一张二维计算网**。它不是新指令集哲学，而是把已有的浮点乘加「阵列化、流水化」以获得吞吐。

## 3. 本讲源码地图

本讲涉及的关键文件全部位于 `src/gpgpu_top/sm/pipeline/tensor/`，外加译码、发射、顶层连线与配置几处：

| 文件 | 作用 |
|------|------|
| [tensor_core_exe.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/tensor/tensor_core_exe.v) | 张量核的**执行壳**：例化 `tensor_core_fp32`，并在输出端挂一个深度为 1 的 FIFO 削峰/切断组合路径，对接写回口。 |
| [tensor_core_fp32.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/tensor/tensor_core_fp32.v) | 张量核**阵列本体**：用 `generate` 把 `tc_dot_product` 例化 `DIM_M×DIM_K` 次，并把三个向量寄存器操作数按矩阵切片喂给每个点积。 |
| [tc_dot_product.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/tensor/tc_dot_product.v) | **单个输出元素的点积**：DIM_N 个乘法 + 一棵二叉加法归约树 + 一次偏置加 C。 |
| [tc_mul_pipe.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/tensor/tc_mul_pipe.v) | **流水化浮点乘法**：`fmul_s1 → fmul_s2 → fmul_s3`，LATENCY=2 拍。 |
| [tc_add_pipe.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/tensor/tc_add_pipe.v) | **流水化浮点加法**：`fadd_s1 → fadd_s2`，LATENCY=2 拍。 |
| [naivemultiplier.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/tensor/naivemultiplier.v) | 尾数乘法核：直接用 `*` 运算符实现的无符号整数乘法（区别于 vmul 的 Booth 阵列乘法器）。 |
| [fadd_s1.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/tensor/fadd_s1.v) / [fadd_s2.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/tensor/fadd_s2.v) | 加法的远近双路径（near/far path）与舍入/溢出处理。 |
| [fmul_s1.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/tensor/fmul_s1.v) | 乘法的分类/指数对齐/移位量计算。 |
| [define.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v) | `TC_DIM_M/N/K`、`VFTTA_VV` 指令位模式、`FN_TTF` 功能码。 |
| [pipe.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v) | 张量核在 SM 流水线中的例化点（写回向量口 `[5]`）。 |
| [issue.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/issue.v) | 把 `tc` 类指令路由到张量核（最高优先级分支）。 |
| [decodeUnit.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v) | `VFTTA_VV` 的译码表项。 |

## 4. 核心概念与源码讲解

### 4.1 张量核整体结构：从一条指令到一棵乘加阵列

#### 4.1.1 概念说明

张量核要算的是一次「矩阵乘加」：

\[
D_{M\times K}=A_{M\times N}\cdot B_{N\times K}+C_{M\times K}
\]

其中 \(M\) 是输出行数、\(K\) 是输出列数、\(N\) 是**归约维**（内积长度）。每一个输出元素：

\[
D_{ij}=\sum_{n=0}^{N-1} A_{in}\cdot B_{nj}+C_{ij}
\]

这正是 `tc_dot_product` 要算的「一次点积 + 一次偏置加」。整张 \(D\) 矩阵共 \(M\times K\) 个元素，所以张量核把 `tc_dot_product` 例化 \(M\times K\) 次。

Ventus 的设计取舍是：把 \(A\)、\(B\)、\(C\) 三个矩阵的元素**紧凑打包进三个向量寄存器**（即 `in1/in2/in3`，各 `NUM_THREAD×32` 位），每个 lane 装一个 FP32 元素。这就要求：

\[
M\cdot N\le \text{NUM\_THREAD},\quad N\cdot K\le \text{NUM\_THREAD},\quad M\cdot K\le \text{NUM\_THREAD}
\]

这也是 `pipe.v` 例化处的注释 `M*N/N*K/M*K < NUM_THREAD` 的含义。

#### 4.1.2 核心流程

一条 `VFTTA_VV` 指令在 SM 内的旅行：

1. **译码**：`decodeUnit` 用 `casex` 把 `VFTTA_VV` 译成功能码 `FN_TTF`，并置 `tc=1` 选择位；三个源操作数取自 `A1_VRS1/A2_VRS2/A3_VRS3`（三个向量寄存器）。
2. **操作数采集**：`operand_collector` 从向量寄存器堆读出 `in1/in2/in3` 三个向量。
3. **发射路由**：`issue` 见 `tc=1`，把握手接到张量核口 `issue_out_TC_*`（在 issue 路由中优先级最高，见 4.1.3）。
4. **阵列计算**：`tensor_core_exe → tensor_core_fp32` 把操作数切片，喂给 \(M\times K\) 个 `tc_dot_product` 并行计算。
5. **写回**：结果经一个深度为 1 的 FIFO，从向量写回口 `[5]` 写回目的向量寄存器。

整体是一棵三层嵌套的例化树：**执行壳 → 阵列 → 点积 → 乘/加流水段**。

#### 4.1.3 源码精读

先看 `VFTTA_VV` 的译码表项（位于 decodeUnit，两条指令各一条）：

[decodeUnit.v:592](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L592) — 把 `VFTTA_VV` 译为：三源 `A3_VRS3/A2_VRS2/A1_VRS1`、功能码 `FN_TTF`、`tc=1`、`wvd=1`（写向量）。

```verilog
`VFTTA_VV : ctrlSignals_0 = {`Y,`Y,`N,`B_N,`N,`N,`CSR_N,`N,`A3_VRS3,`A2_VRS2,`A1_VRS1,`IMM_X,`MEM_X,`FN_TTF, ...};
```

对应的 `tc` 选择位在第 [3] 位被抽出：

[decodeUnit.v:631](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L631) — `assign control_Signals_tc_0_o = ctrlSignals_0[3];` 抽出张量核选择位。

再看 issue 路由：`tc` 是最高优先级分支，一旦命中就把所有别的执行单元 valid 拉零：

[issue.v:452-467](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/issue.v#L452-L467) — `if(inputBuf_warps_control_Signals_tc)` 时把握手接到 `issue_out_TC_*`，其余执行单元全部置 0。

最后是 `pipe.v` 的例化，注意三个关键点：维度参数硬编码为 `2/2/2`、舍入模式取自 CSR 的 `frm`（`csrfile_rm[8:6]`）、写回口接 `writeback_in_v_ready[5]`：

[pipe.v:1984-2015](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1984-L2015) — 张量核例化，`.DIM_M(2)/.DIM_N(2)/.DIM_K(2)`、`.rm_i(csrfile_rm[8:6])`、`.out_ready_i(writeback_in_v_ready[5])`。

> 注意一个工程细节：虽然 `define.v` 里定义了 `TC_DIM_M/N/K` 宏（见 4.5），但 `pipe.v` 例化时**没有**直接引用这些宏，而是写死了 `2/2/2`（`tensor_core_exe` 内部的 parameter 默认值也是 `2/2/2`）。若要改维度，需同步修改 `pipe.v` 的例化参数。

#### 4.1.4 代码实践

**实践目标**：确认张量核在你当前配置下能否被正确喂饱。

**操作步骤**：
1. 打开 `src/define/define.v`，记录 `NUM_THREAD`（默认 4）与 `TC_DIM_M/N/K`（均 2）。
2. 打开 `pipe.v:1984` 附近的例化，确认 `.DIM_M/.DIM_N/.DIM_K` 实际传参。
3. 手算约束：`M*N`、`N*K`、`M*K` 是否都 `≤ NUM_THREAD`。

**需要观察的现象 / 预期结果**：默认 `NUM_THREAD=4`、维度 `2×2×2` 时，三个乘积均为 4，恰等于 `NUM_THREAD`，刚好装满一个向量寄存器；若把 `NUM_THREAD` 改为更小（如仿真极小配置）则约束被破坏，张量核的切片位宽会越界。

> 待本地验证：若你在 define.v 把 `NUM_THREAD` 设为 8 而维度仍为 `2×2×2`，则每个向量寄存器只有前 4 个 lane 被张量核使用，后 4 个 lane 的结果内容无定义——可在波形中观察 `tensorcore_out_v_wb_wvd_rd_o` 的高位 lane 是否为 0 或随机值。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `issue.v` 里 `tc` 分支要把 `issue_out_vFPU_valid_o`、`issue_out_MUL_valid_o` 等全部拉零？

**答案**：issue 是「单发射」路由器，一拍只允许一条指令进入一个执行单元。把别的 valid 拉零，是为了确保同一拍不会把同一条指令误送到多个单元，也避免别的 warp 指令抢同一写回口。

**练习 2**：张量核的舍入模式 `rm_i` 从哪里来？

**答案**：从 `pipe.v` 例化看，`.rm_i(csrfile_rm[8:6])`，即取自 CSR 文件中 `frm`（浮点舍入模式寄存器）的字段，与浮点单元共享同一舍入模式来源（详见 u5-l2 CSR 与 u4-l4 舍入模式三选一）。

---

### 4.2 tensor_core_exe：执行壳与结果 FIFO

#### 4.2.1 概念说明

`tensor_core_exe` 是张量核对外的「门面模块」。它本身不做任何矩阵运算，只做两件事：

1. 例化真正的阵列 `tensor_core_fp32`；
2. 在阵列输出与写回口之间，插一个**深度为 1、组合穿透（`pipe=true`）的 `stream_fifo_pipe_true`**。

这个 FIFO 的作用与 u4-l2 中 vALU 输出端的 `stream_fifo_pipe_true` 完全一致：**切断张量核内部那条很长的乘加组合/流水路径，避免它与写回口的组合逻辑串成一条致命的长路径**，同时提供标准的 `valid/ready` 反压握手。

#### 4.2.2 核心流程

- 输入：三个向量操作数 `in1/in2/in3`、目的寄存器号 `ctrl_reg_idxw_i`、warp 号 `ctrl_wid_i`、舍入模式 `rm_i`，以及 `in_valid_i / out_ready_i` 握手。
- 阵列算完后，把 `result + fflags + warp_id + reg_idxw + valid` 打包成一个宽位宽的总线写入 FIFO。
- FIFO 读出后切片还原成 `wb_wvd_rd_o`（结果数据）、`wvd_mask_o`（全 1 掩码）、`wvd_o`（写向量有效）、`reg_idxw_o`、`warp_id_o`。

注意 `wvd_mask_o` 被硬连为全 1（`{NUM_THREAD{1'b1}}`）：张量核的结果对所有 lane 都有效，不存在掩码写。

#### 4.2.3 源码精读

模块端口与参数：[tensor_core_exe.v:16-49](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/tensor/tensor_core_exe.v#L16-L49) — `VL=NUM_THREAD`，`DIM_M/N/K=2`，`EXPWIDTH=8/PRECISION=24`（FP32 单精度）。

例化阵列，注意 `rm_i` 被广播成 `VL` 份（每个 lane 共享同一舍入模式）：

[tensor_core_exe.v:68-98](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/tensor/tensor_core_exe.v#L68-L98) — `.rm_i({VL{rm_i}})`。

结果 FIFO（深度 1、pipe=true）与打包/切片逻辑：

[tensor_core_exe.v:101-126](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/tensor/tensor_core_exe.v#L101-L126) — 打包 `result_v_data_in`，再从 `result_v_data_out` 切片还原各输出字段；`in_ready_o = result_v_in_ready`（反压由 FIFO 驱动）。

#### 4.2.4 代码实践

**实践目标**：理解「打包—进 FIFO—切片」的位宽对齐。

**操作步骤**：阅读 [tensor_core_exe.v:116](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/tensor/tensor_core_exe.v#L116) 的打包式 `{tensor_result, tensor_ctrl_warpid, tensor_ctrl_reg_idxw, tensor_out_valid, {NUM_THREAD{1'b1}}}`，然后对照 L120-L124 的切片下标。

**预期结果**：打包总宽 = `NUM_THREAD*32 + DEPTH_WARP + 8 + 1 + NUM_THREAD`，与 FIFO 的 `DATA_WIDTH` 一致；`wvd_o` 取的是那一位 `valid`，`wvd_mask_o` 取的是末尾 `NUM_THREAD` 位全 1 掩码。

#### 4.2.5 小练习与答案

**练习**：为什么 `wvd_mask_o` 要硬连成全 1，而不是像 LSU 那样按字节使能？

**答案**：张量核的每个输出 lane 都是一个完整的、有效的 FP32 结果（\(D_{ij}\)），不存在「部分 lane 写、部分 lane 不写」的情况；而 LSU 的向量访存需要掩码是因为要支持掩码 load/store（如 `v0` 掩码寄存器）。所以这里全 1 即可。

---

### 4.3 tensor_core_fp32 与 TC_DIM_*：点积阵列与矩阵切片

#### 4.3.1 概念说明

`tensor_core_fp32` 是张量核的「阵列本体」。它用两层 `generate for` 把 `tc_dot_product` 例化 `DIM_M × DIM_K` 次，每个点积负责算输出矩阵 \(D\) 的一个元素 \(D_{ij}\)。

三个输入向量寄存器按如下方式被切片成矩阵元素（这是理解 `TC_DIM_*` 的核心）：

- `a_i`：被切成 `DIM_M` 段，每段 `DIM_N` 个元素 → 矩阵 \(A\) 是 `DIM_M × DIM_N`。
- `b_i`：被切成 `DIM_K` 段，每段 `DIM_N` 个元素 → 矩阵 \(B\) 是 `DIM_K × DIM_N`（按行存）。
- `c_i`：被切成 `DIM_M × DIM_K` 个标量 → 偏置矩阵 \(C\) 是 `DIM_M × DIM_K`。

于是第 \((i,j)\) 个点积算的是 \(A\) 的第 \(i\) 行与 \(B\) 的第 \(j\) 行的内积，再加 \(C_{ij}\)。换言之 \(D = A \cdot B^{T} + C\)，输出 `DIM_M × DIM_K`。`DIM_N` 就是内积归约长度。

#### 4.3.2 核心流程

对每个 \((i,j)\)（\(i\in[0,M), j\in[0,K)\)）：

```
a_slice = a_i 的第 i 行（DIM_N 个元素）
b_slice = b_i 的第 j 行（DIM_N 个元素）
c_elem  = c_i 的第 (i*K+j) 个标量
result[i*K+j] = dot_product(a_slice, b_slice, c_elem)
```

所有点积共享同一对 `in_valid_i / out_ready_i`（同步喂入、同步回收）；`in_ready_o / out_valid_o` 直接取第 `[0]` 个点积的信号作为代表（因为所有点积的流水深度相同，必然同步）。

#### 4.3.3 源码精读

切片逻辑是本模块的精髓，逐行看 [tensor_core_fp32.v:54-86](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/tensor/tensor_core_fp32.v#L54-L86)：

```verilog
.a_i (a_i[(i+1)*DIM_N*W-1 : i*DIM_N*W])           // A 的第 i 行
.b_i (b_i[(j+1)*DIM_N*W-1 : j*DIM_N*W])           // B 的第 j 行
.c_i (c_i[(i*DIM_K+j+1)*W-1 : (i*DIM_K+j)*W])     // C 的第 (i,j) 个元素
.result_o(result_o[(i*DIM_K+j+1)*W-1 : (i*DIM_K+j)*W])  // D 的第 (i,j) 个元素
```

（其中 `W = EXPWIDTH+PRECISION = 32`。）

代表信号的选取：[tensor_core_fp32.v:88-92](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/tensor/tensor_core_fp32.v#L88-L92) — `in_ready_o = tc_array_in_ready[0]`、`out_valid_o = tc_array_out_valid[0]`。

`TC_DIM_*` 宏的定义在 define.v，默认全为 2：

[define.v:219-223](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L219-L223) — `` `define TC_DIM_M 2 ``、`` `define TC_DIM_N 2 ``、`` `define TC_DIM_K 2 ``。

#### 4.3.4 代码实践

**实践目标**：手算默认配置下张量核一次运算处理多少个矩阵元素、需要多少次乘加。

**操作步骤**：
1. 默认 `DIM_M=DIM_N=DIM_K=2`，计算点积个数 = `M*K = 2*2 = 4`。
2. 每个点积的归约长度 = `DIM_N = 2`，即每个点积做 2 次乘法 + 归约加法。
3. 全阵列一次运算的总乘法数 = `M*K*N = 2*2*2 = 8` 次乘法。

**预期结果**：默认 `2×2×2` 配置下，一条 `VFTTA_VV` 触发 4 个点积，共 8 次 FP32 乘法与相应的加法，产出 4 个 \(D\) 元素（填满 `NUM_THREAD=4` 个 lane）。若把维度改为 `4×4×4`（需 `NUM_THREAD≥16`），则一次运算做 \(4\times4\times4=64\) 次乘法。

#### 4.3.5 小练习与答案

**练习**：若 `NUM_THREAD=8`，想让张量核吞吐最大化，`DIM_M/DIM_N/DIM_K` 可取到多少？

**答案**：约束是 `M*N≤8`、`N*K≤8`、`M*K≤8`（三者同时成立，因为 A/B/C/D 都要装进 8-lane 向量寄存器）。一组可行解是 `2×2×2`（占 4 lane，浪费一半）；要装满 8 lane 可选 `2×4×2`（M*N=8、N*K=8、M*K=4，但 C/D 只有 4 个元素用到 4 lane）或 `2×2×4` 等，取决于哪个矩阵最受限。完全装满三个矩阵需 `NUM_THREAD` 同时 ≥ `M*N`、`N*K`、`M*K`。

---

### 4.4 tc_dot_product：乘法阵列 + 二叉加法归约树

#### 4.4.1 概念说明

`tc_dot_product` 是张量核的「计算心脏」，负责算一个输出元素：

\[
\text{result}=\left(\sum_{n=0}^{N-1} a_n\cdot b_n\right)+c
\]

它由三类部件组成：

1. **DIM_N 个 `tc_mul_pipe`**：并行算出 \(a_n\cdot b_n\)（\(n=0..N-1\)）。
2. **一棵二叉归约树（由 `tc_add_pipe` 组成）**：把这 \(N\) 个乘积两两相加，归约成单个和。源码注释精确给出规模：「`adds` 共有 `DIM_N-1` 个元素，假设 `DIM_N=16`，则个数 = 8+4+2+1=15」。
3. **一个 `final_add`（偏置加）**：把归约和加上偏置 \(c\)。

注意 `DIM_N` 必须是 2 的幂（源码注释 `DIM_N需要定义为2的指数`），这样二叉树才能完美对齐。

#### 4.4.2 核心流程

以 `DIM_N=2`（默认）为例，结构最简单：

```
a[0],b[0] ─→ tc_mul_pipe ─→ muls[0] ─┐
                                       ├─ tc_add_pipe(adds[0]) ─→ adds[0] ─┐
a[1],b[1] ─→ tc_mul_pipe ─→ muls[1] ─┘                                   │
                                                                         ├─ final_add ─→ result
                                                                   c ────┘
```

- 2 个乘法器并行算 `muls[0]`、`muls[1]`；
- 1 个 `adds[0] = muls[0] + muls[1]`（归约）；
- 1 个 `finaladd = adds[0] + c`（偏置）；
- 末端一个深度 1 的 FIFO 切断路径。

对一般 `DIM_N`，归约树有 \(\lceil\log_2 N\rceil\) 层：第 0 层 `N/2` 个加法、第 1 层 `N/4` 个……直到 1 个，共 `N-1` 个加法器；再加 1 个偏置加。每个 `tc_add_pipe` 是 2 拍流水，所以归约树的**纵向关键路径**为 \((\log_2 N + 1)\) 级加法。

整条点积的握手反压靠 `out_ready` 链传递：每个加法器的 `in_ready` 喂回上游乘法器的 `out_ready`，末级 `final_add` 的 `in_ready` 接 FIFO 写就绪。

#### 4.4.3 源码精读

DIM_N 个乘法器例化（共享 `in_valid_i`）：

[tc_dot_product.v:91-129](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/tensor/tc_dot_product.v#L91-L129) — 例化 `DIM_N` 个 `tc_mul_pipe`，`a_i/b_i` 按 lane 切片，`out_ready_i` 接 `muls_out_ready[i]`。

第 0 层归约加法（把乘积前后半段两两相加）：

[tc_dot_product.v:131-171](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/tensor/tc_dot_product.v#L131-L171) — `for(j=0;j<DIM_N/2)` 例化 `tc_add_pipe`，`.a_i(muls_result[j])`、`.b_i(muls_result[j+DIM_N/2])`。

后续归约层（循环相加直到剩一个）：

[tc_dot_product.v:173-213](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/tensor/tc_dot_product.v#L173-L213) — `for(m=1;m<$clog2(DIM_N))` 嵌套 `for(n=0;n<DIM_N/(1<<(m+1)))` 逐层归约。

偏置加（加 \(c\)）与末端 FIFO：

[tc_dot_product.v:228-289](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/tensor/tc_dot_product.v#L228-L289) — `U_final_add` 的 `.b_i(adds_ctrl_c[DIM_N-2])` 即偏置 \(c\)；末端 `stream_fifo_pipe_true` 切断路径；`in_ready_o = muls_in_ready[DIM_N-1]`。

`out_ready` 反压链的显式连线：

[tc_dot_product.v:215-226](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/tensor/tc_dot_product.v#L215-L226) — 把后级 `in_ready` 回送给前级 `out_ready`，末级接 `finaladd_in_ready`。

#### 4.4.4 代码实践

**实践目标**：数清一次点积的乘加次数与流水级数（对应规格里的核心实践任务）。

**操作步骤**：
1. **乘加计数**：对默认 `DIM_N=2`，点积 = 2 乘（`muls[0/1]`）+ 1 归约加（`adds[0]`）+ 1 偏置加（`final_add`）。对一般 `DIM_N=N`：\(N\) 次乘 + \((N-1)\) 次归约加 + 1 次偏置加。
2. **流水级梳理**：跟踪 `tc_mul_pipe → adds(tc_add_pipe) → final_add(tc_add_pipe) → FIFO`：
   - `tc_mul_pipe` 内部 `fmul_s1`（组合）→ 寄存器 → `fmul_s2`（组合）→ 寄存器 → `fmul_s3`（组合出结果），即 **2 拍**（`LATENCY=2`）。
   - 每个 `tc_add_pipe` 内部 `fadd_s1`（组合）→ 寄存器 → `fadd_s2`（组合出结果），即 **2 拍**。
   - `DIM_N=2` 时纵向经过 1 级归约加 + 1 级偏置加 = 2 级加法。
   - 因此单点积结果约在 `2(乘) + 2(归约加) + 2(偏置加) = 6` 拍后有效（再经末级 FIFO）。

**需要观察的现象 / 预期结果**：手算结果应与上一节「4.3.4」的总乘法数自洽——4 个点积 × 每点积 2 乘 = 8 次乘法。归约深度随 \(\log_2(\text{DIM_N})\) 增长，所以把 `DIM_N` 加倍会让单点积延迟多 2 拍（多一级加法），但乘法器数也翻倍——典型的「面积换吞吐」权衡。

> 待本地验证：在波形中给 `tensor_core_exe` 喂一条 `VFTTA_VV`，数 `in_valid_i` 拉起到 `out_valid_o` 拉起的拍数，验证是否约为 6～7 拍。

#### 4.4.5 小练习与答案

**练习 1**：`DIM_N=4` 时，归约树有多少个 `tc_add_pipe`？分几层？

**答案**：共 `DIM_N-1 = 3` 个归约加法器 + 1 个偏置加 = 4 个 `tc_add_pipe`；归约树分 \(\log_2 4 = 2\) 层（第 0 层 2 个、第 1 层 1 个），再加 1 级偏置加。

**练习 2**：为什么归约树用「两两相加的二叉树」而不是「顺序累加」\(（\cdots((a_0b_0+a_1b_1)+a_2b_2)+\cdots)\)？

**答案**：二叉树的纵向深度是 \(\log_2 N\)，而顺序累加深度是 \(N-1\)。二叉树让关键路径短得多（延迟随 \(\log N\) 而非 \(N\) 增长），且每层可并行流水，吞吐更高；代价是同一层需要更多加法器（面积），但这是张量核愿意付出的换吞吐代价。

---

### 4.5 tc_mul_pipe 与 tc_add_pipe：流水化乘/加核

#### 4.5.1 概念说明

`tc_mul_pipe` 和 `tc_add_pipe` 是张量核的最底层运算核，分别封装一次 FP32 乘法与一次 FP32 加法，都用 `LATENCY=2` 的两拍流水实现。它们与 u4-l4 浮点单元里的 `fmul_pipe/fadd_pipe` 同源，但有几个张量核专属的工程特点：

- **控制信号伴随数据流水**：`ctrl_c`（偏置 \(c\)）、`ctrl_rm`（舍入模式）、`ctrl_reg_idxw`（目的寄存器号）、`ctrl_warpid` 都随数据一起被打拍寄存，确保 \(N\) 拍后输出时仍带着正确的 warp/寄存器身份。其中 `ctrl_c` 一路传递到 `final_add` 才被用作偏置加的操作数。
- **`naivemultiplier` 取代 Booth 阵列**：乘法的尾数相乘用的是 [naivemultiplier.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/tensor/naivemultiplier.v)，它直接用 Verilog `*` 运算符（`assign result = reg_a * reg_b;`，见 [naivemultiplier.v:47](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/tensor/naivemultiplier.v#L47)），交给综合工具映射成硬件乘法器，比 u4-l3 整数乘法里的 Booth + Wallace `array_multiplier` 简单得多。

#### 4.5.2 核心流程

**`tc_mul_pipe`**（计算 \(a\times b\)）：

```
拍0: fmul_s1 组合算(符号/指数/移位量/特例)  ┐
    naivemultiplier 寄存 a,b 的尾数          ┘──→ [reg_en1 寄存]
拍1: fmul_s2 组合(用 naivemultiplier 的乘积做规格化) ──→ [reg_en2 寄存]
拍2: fmul_s3 组合出最终 result_o（含舍入/特例）
```

**`tc_add_pipe`**（计算 \(a+b\)）：

```
拍0: fadd_s1 组合(far/near 双路径分类、对阶) ──→ [reg_en2 寄存]   (输入先经 reg_en1 打一拍 a_reg/b_reg)
拍1: fadd_s2 组合(舍入/溢出/输出 result_o)
```

两者都实现标准的 `valid/ready` 反压：当 `out_ready_i=0` 且两级寄存器都满时，停止打拍（`reg_en` 被门控），`in_ready_o` 拉低。

#### 4.5.3 源码精读

`tc_mul_pipe` 的 valid/ready 流水控制（两拍 LATENCY，反压时冻结）：

[tc_mul_pipe.v:132-158](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/tensor/tc_mul_pipe.v#L132-L158) — `in_valid_reg1/reg2` 两级，`in_ready_o = !(!out_ready_i && in_valid_reg1 && in_valid_reg2)`。

乘法三段例化：

[tc_mul_pipe.v:160-180](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/tensor/tc_mul_pipe.v#L160-L180) — `fmul_s1`（分类/指数对齐，组合）；
[tc_mul_pipe.v:237-256](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/tensor/tc_mul_pipe.v#L237-L256) — `naivemultiplier`（尾数相乘，内部自带一级寄存器，`regenable=reg_en1`）；
[tc_mul_pipe.v:258-288](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/tensor/tc_mul_pipe.v#L258-L288) — `fmul_s2`（用乘积做规格化，组合）；
[tc_mul_pipe.v:348-375](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/tensor/tc_mul_pipe.v#L348-L375) — `fmul_s3`（舍入/特例/最终结果，组合）。

`tc_add_pipe` 的加法两段例化：

[tc_add_pipe.v:165-195](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/tensor/tc_add_pipe.v#L165-L195) — `fadd_s1`（far/near 双路径分类，组合；注意 `PRECISION` 被实例化为 `2*PRECISION`、`OUTPC=PRECISION`，即内部用扩展精度做加法以保住乘积的低位）；
[tc_add_pipe.v:264-287](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/tensor/tc_add_pipe.v#L264-L287) — `fadd_s2`（舍入/溢出，组合出 `result_o`）。

`naivemultiplier` 全貌：

[naivemultiplier.v:16-49](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/tensor/naivemultiplier.v#L16-L49) — 输入尾数先打一拍寄存（`regenable`），再 `assign result = reg_a * reg_b;`。

#### 4.5.4 代码实践

**实践目标**：理解「控制信号伴随数据流水」的设计，特别是 `ctrl_c` 如何一路传到偏置加。

**操作步骤**：
1. 在 `tc_mul_pipe.v` 找 `ctrl_c_reg1 / ctrl_c_reg2`（如 [L49/L53](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/tensor/tc_mul_pipe.v#L49-L56)），观察 `ctrl_c_i` 如何在 `reg_en1/reg_en2` 下被打两拍，最终从 `ctrl_c_o` 输出。
2. 在 `tc_dot_product.v` 的 `U_final_add`（[L228](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/tensor/tc_dot_product.v#L228)）处，确认 `.b_i(adds_ctrl_c[DIM_N-2])`——即偏置 \(c\) 正是这一路伴随传来的 `ctrl_c`。

**预期结果**：`ctrl_c` 在乘法器里只是「搭便车」寄存两拍（乘法本身不用它），输出后进入归约树继续传递，最终在 `final_add` 被当作第二操作数消费。这是一种避免额外布线/寄存器堆端口的「数据伴随控制」技巧。

> 待本地验证：把 `tc_dot_product.v` 中 `U_final_add` 的 `.b_i` 改为一个常数（如 0），仿真应看到结果少了偏置 \(c\)，从而验证这条伴随通路的作用。

#### 4.5.5 小练习与答案

**练习 1**：`tc_add_pipe` 例化 `fadd_s1` 时为何把 `PRECISION` 翻倍（`.PRECISION(2*PRECISION)`）？

**答案**：因为乘法器 `tc_mul_pipe` 输出的是两个 24 位尾数相乘的乘积，位宽接近 48 位。归约加法必须用扩展精度（48 位尾数）来加这些乘积，才能保住低位不被过早丢弃，从而保证最终舍入只发生一次（与 u4-l4 FMA「传递未舍入乘积、只舍入一次」的精度原则一致）。

**练习 2**：`naivemultiplier` 用 `*` 运算符，而 `vmul`（u4-l3）用 Booth `array_multiplier`。张量核为何选「朴素」乘法？

**答案**：张量核已经通过「阵列化 + 流水化」获得吞吐，且 `naivemultiplier` 自带一级寄存器（正好对齐到 `tc_mul_pipe` 的第一拍流水寄存器）。综合工具会把 `*` 映射到目标工艺的高效乘法器/IP，实现简单、可读性好；而整数 `vmul` 需要精确控制有符号/无符号/高低位等多模式复用，故自建 Booth 阵列。两者服务于不同场景。

---

## 5. 综合实践

**任务：手算并绘制默认张量核的完整计算与流水图。**

请按以下步骤把本讲知识串起来：

1. **维度与打包**：默认 `NUM_THREAD=4`、`DIM_M=DIM_N=DIM_K=2`。画出三个向量寄存器 `in1(A)`、`in2(B)`、`in3(C)` 的 4 个 lane，按 4.3 节的切片规则标出 \(A_{2\times2}\)、\(B_{2\times2}\)、\(C_{2\times2}\) 各元素落在哪个 lane。
2. **阵列结构**：画出 `tensor_core_fp32` 里 4 个 `tc_dot_product`（对应 \(D_{00},D_{01},D_{10},D_{11}\)），每个点积的 \(a\) 行、\(b\) 行、\(c\) 元素分别来自哪里。
3. **点积内部**：任选一个点积，画出 2 个 `tc_mul_pipe` → 1 个归约 `tc_add_pipe` → 1 个偏置 `tc_add_pipe` → 末端 FIFO 的结构，并标注每一拍的流水占用（参考 4.4.2 的图）。
4. **乘加总数**：写出全阵列一次运算的乘法总数与加法总数（应为 8 乘、8 加：4 点积 ×（2 乘 + 1 归约加 + 1 偏置加））。
5. **延迟估算**：估算从 `in_valid_i` 到 `out_valid_o` 的拍数（约 6～7 拍），并指出延迟的「纵向关键路径」是 `乘(2) + 归约加(2×⌈log₂N⌉) + 偏置加(2)`。

**进阶（可选）**：阅读 [tc_dot_product.v:174-213](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/tensor/tc_dot_product.v#L174-L213) 的多层 `generate` 归约树，把 `DIM_N` 在脑中改为 4，重画归约树（应有 2 层加法 + 偏置加），验证 `adds` 实例数仍为 `DIM_N-1 = 3`。

> 这一步是纯源码阅读型实践，无需运行仿真即可完成；若要验证，可在 `tc_vecadd` 框架下编写一段调用 `VFTTA_VV` 的小 kernel，但请注意当前仓库的张量指令软件支持与可运行用例需「待本地验证」。

## 6. 本讲小结

- 张量核用三层例化树实现矩阵乘加 \(D=A\cdot B^{T}+C\)：**执行壳 → `DIM_M×DIM_K` 点积阵列 → 单点积（`DIM_N` 乘 + 二叉归约加树 + 偏置加）**。
- `TC_DIM_M/K` 是输出行列数、`TC_DIM_N` 是内积归约维；三个矩阵的元素被打包进三个向量寄存器，要求 `M*N`、`N*K`、`M*K` 均 `≤ NUM_THREAD`。
- 一次点积 = `DIM_N` 次乘 + `(DIM_N-1)` 次归约加 + 1 次偏置加；全阵列一次运算的乘法总数 = \(M\times K\times N\)（默认 `2×2×2` → 8 次乘）。
- 底层 `tc_mul_pipe`（`fmul_s1→fmul_s2→fmul_s3`，2 拍）与 `tc_add_pipe`（`fadd_s1→fadd_s2`，2 拍）是流水化的 FP32 乘/加核，尾数乘法用朴素的 `naivemultiplier`（`*` 运算符）。
- 归约用二叉树（深度 \(\log_2 N\)）而非顺序累加，用面积换更短的关键路径与更高吞吐；偏置 \(c\) 通过 `ctrl_c` 伴随数据一路寄存到 `final_add` 才消费。
- `VFTTA_VV` 经 `decodeUnit`（`FN_TTF` + `tc` 位）→ `issue` 最高优先级路由 → 张量核 → 结果 FIFO → 向量写回口 `[5]`，舍入模式取自 CSR `frm`。

## 7. 下一步学习建议

- **横向对比浮点单元**：回到 u4-l4，对比 `fma`（融合乘加）与张量核的归约加树——两者都遵循「传递未舍入乘积、只舍入一次」的精度原则，但 FPU 面向单条标量/向量 FMA 指令，张量核面向矩阵阵列。
- **纵向进入存储子系统**：张量核的高吞吐对数据供给要求极高，建议接着学 u6（L1 dcache、共享内存、`l1cache_arb`），理解 SM 如何把矩阵 tile 数据喂给张量核。
- **指令集扩展视角**：若你想新增/修改张量指令，可结合 u8-l4（指令集扩展），注意 `VFTTA_VV` 的位模式（[define.v:840](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L840)）与 `FN_TTF`（[define.v:553](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L553)），以及 `pipe.v` 例化处维度参数需同步修改的工程细节。
- **继续阅读源码**：精读 [fadd_s1.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/tensor/fadd_s1.v) 的 far/near 双路径与 [fadd_s2.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/tensor/fadd_s2.v) 的舍入/溢出处理，理解浮点加减的精度保证细节。
