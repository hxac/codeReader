# CACC：累加器、偏置与精度输出

## 1. 本讲目标

CACC（Convolution ACCumulator，卷积累加器）是卷积主流水线的第五级、也是最后一级「计算」单元。学完本讲，你应当能够：

- 说清楚 CACC 在卷积五级流水（CDMA→CBUF→CSC→CMAC→CACC）中的职责：把 CMAC 阵列送来的「冗余部分和」逐拍累加成完整卷积结果，并按精度做舍入与饱和，最终交付给后处理引擎 SDP。
- 理解「部分和累加」的本质——为什么需要一张可读改写（read-modify-write）的工作 SRAM 来保存跨输入通道的累加中间值。
- 掌握 INT8、INT16、FP16 三种精度在累加位宽与计算路径上的差异，并能对照源码说出各自的累加器位宽。
- 认识 assembly_buffer（装配缓冲）与 delivery_buffer（交付缓冲）这两级缓冲的分工，以及它们如何配合 producer/consumer 影偶配置与 GLB done 中断完成一次卷积层。

## 2. 前置知识

在进入 CACC 之前，先建立三个直觉。

**第一，什么是「部分和（partial sum）」。** 卷积是一个大量乘加的求和。对于某个输出通道的一个像素，其值为：

\[
y = \sum_{c=0}^{C-1}\sum_{i=0}^{K_h-1}\sum_{j=0}^{K_w-1} x_{c,i,j}\cdot w_{c,i,j}
\]

其中 \(C\) 是输入通道数（reduction/累加维），\(K_h,K_w\) 是卷积核高宽。CMAC 阵列每个节拍只能算出这个巨大求和的「一小撮」乘积之和，称为部分和。要把完整结果算出来，必须把许多节拍、许多输入通道的部分和不断累加。CACC 就是那个「负责把部分和累成最终结果」的单元。本仓库中，输入通道维由 `MAC_ATOMIC_C_SIZE_64` 锁定，输出通道维由 `MAC_ATOMIC_K_SIZE_32` 锁定（见 [spec/defs/nv_full.spec:16-17](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/defs/nv_full.spec#L16-L17)）。

**第二，什么是「饱和（saturation）」与「舍入（rounding）/截断（truncation）」。** 累加过程中数值会不断长大，需要更宽的位宽才不溢出；但最终交付给 SDP 的结果只有 32 位（甚至更窄的目标精度）。因此 CACC 必须做两件事：累加时用宽位宽并做饱和（超出范围就钳位到最大/最小值），交付前做移位截断（`cfg_truncate`）并做舍入（round-to-nearest）。这两步在源码里都有清晰的实现。

**第三，为什么需要「两级缓冲」。** CACC 既要持续累加（慢，跨整个 reduction 维），又要在结果就绪后按 SDP 的节拍平稳交付（快，不能让 SDP 等也不能让 SDP 丢数据）。用一张 SRAM 边算边写、另一张 SRAM 缓存已就绪结果等待交付，是经典的「计算与解耦」做法。本讲会把这两张 SRAM（assembly_buffer、delivery_buffer）讲透。

> 名词速查：reduction 维 = 需要被累加消除的维度（输入通道 C）；partial sum = 中间累加值；shadow/影偶 = 两套寄存器轮换以实现无缝切换（见 u2-l3）；CSB = 配置总线（见 u2-l1）；SDP = 单点数据后处理引擎（下一站）。

## 3. 本讲源码地图

CACC 全部源码位于 `vmod/nvdla/cacc/`，本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [NV_NVDLA_cacc.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_cacc.v) | CACC 顶层，例化六个子模块并连线，是看「整体结构」的入口。 |
| [NV_NVDLA_CACC_calculator.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_calculator.v) | 计算核心，例化 128 个精度可配置的累加单元，完成「读旧部分和 + 加新部分和 → 写新部分和 / 产出最终结果」。 |
| [NV_NVDLA_CACC_CALC_int16.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_CALC_int16.v) | INT16/INT8 通用整数累加单元（含高位优化与饱和）。 |
| [NV_NVDLA_CACC_CALC_int8.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_CALC_int8.v) | INT8 窄累加单元，结构最简单，适合作为入门样例。 |
| [NV_NVDLA_CACC_CALC_fp_48b.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_CALC_fp_48b.v) | FP16 浮点累加单元（48 位尾数累加）。 |
| [NV_NVDLA_CACC_assembly_buffer.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_assembly_buffer.v) | 装配缓冲 SRAM：保存累加过程中的部分和。 |
| [NV_NVDLA_CACC_delivery_buffer.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_delivery_buffer.v) | 交付缓冲 SRAM：保存最终结果，按节拍交付给 SDP 并产生 done 中断。 |
| NV_NVDLA_CACC_assembly_ctrl.v / delivery_ctrl.v | 两个缓冲的控制状态机（本讲按需引用，不深入状态机细节）。 |
| NV_NVDLA_CACC_regfile.v / dual_reg.v | 配置寄存器与影偶（shadow）切换，承接 u2-l3。 |

## 4. 核心概念与源码讲解

### 4.1 部分和累加：CACC 的核心使命

#### 4.1.1 概念说明

CMAC（上一级）是一个纯乘加阵列：它每个节拍吐出一堆「部分和」——也就是一组乘积的压缩和，但**不负责把跨输入通道、跨卷积窗口的所有部分和加完**。这件事交给 CACC。

可以把 CACC 想象成一个带「暂存抽屉」的加法器：

- 每来一个节拍的部分和，CACC 从抽屉里取出该输出位置之前累加到的中间值；
- 把新部分和加上去；
- 再写回抽屉；
- 当这个输出位置的所有输入通道都加完（reduction 结束），就把累加结果做精度处理，送出给 SDP。

这里的「抽屉」就是 assembly_buffer（装配缓冲）。之所以要一张 SRAM 而不是一排寄存器，是因为卷积有很多输出位置（不同输出通道、不同空间像素）的部分和在同时累加，必须用带地址的存储把它们各自记下来。

CACC 在结构上分为三段，对应三个职责，从顶层例化看得最清楚：

1. **assembly（装配）段**：控制对装配缓冲的读写，决定「现在该取哪个位置的部分和、算完写回哪里」。
2. **calculator（计算）段**：真正的加法 + 饱和 + 舍入硬件，按精度分三套。
3. **delivery（交付）段**：把最终结果缓存起来，按 SDP 的握手节拍平稳送出，并在层结束时上报 done 中断。

#### 4.1.2 核心流程

一个输出通道位置的一次完整累加，在 CACC 内部大致经历：

```text
CMAC 部分和 (mac_a2accu/mac_b2accu, 每路 8×176-bit)
        │
        ▼
 ┌─────────────── assembly_ctrl ───────────────┐
 │ 给出 abuf_rd_addr：读出「旧部分和」位置      │
 └──────────────────────────────────────────────┘
        │  abuf_rd_data（旧部分和）
        ▼
 ┌─────────────── calculator ──────────────────┐
 │  新部分和 = 旧部分和(in_op) + CMAC部分和(in_data) │
 │  饱和到累加位宽 → out_partial（写回 abuf）    │
 │  若本拍是该位置最后一拍(in_sel) → 舍入/截断   │
 │                          → out_final（送交付） │
 └──────────────────────────────────────────────┘
        │ abuf_wr_data(新部分和)        │ dlv_data(最终结果)
        ▼                              ▼
   assembly_buffer                delivery_ctrl
   （读改写循环）                      │
                                   delivery_buffer
                                      │ cacc2sdp_pd[513:0]
                                      ▼
                                     SDP
```

关键不变式：

- **in_data** = 本节拍 CMAC 送来的新部分和；
- **in_op** = 从 assembly_buffer 读出的、该位置此前累加到的旧部分和；
- **out_partial** = 累加后的新部分和，写回 assembly_buffer（供下一拍继续加）；
- **out_final** = 当 reduction 完成（`in_sel` 有效）时，把累加结果做移位 + 舍入 + 饱和后的最终值，送入交付通路。

#### 4.1.3 源码精读

先看顶层 [NV_NVDLA_cacc.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_cacc.v) 如何例化这六个子模块。它的输入是两组来自 CMAC 的部分和（mac_a2accu_* 与 mac_b2accu_*，对应阵列的两半 cmac_a / cmac_b），输出是送往 SDP 的 `cacc2sdp_pd` 与送往 GLB 的 done 中断 `cacc2glb_done_intr_pd`：

[NV_NVDLA_cacc.v:96-103](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_cacc.v#L96-L103) 给出了 CACC 的两个核心对外产出：514 位的 `cacc2sdp_pd`（交付给 SDP 的数据包）和 2 位的 `cacc2glb_done_intr_pd`（上报给 GLB 的完成中断，2 位对应两组影偶）。还有一条反向的 `accu2sc_credit`（回送给 CSC 的信用反压，防止累加器溢出）。

[NV_NVDLA_cacc.v:202-451](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_cacc.v#L202-L451) 是六个子模块的例化块，依次是 `u_regfile`、`u_assembly_ctrl`、`u_assembly_buffer`、`u_calculator`、`u_delivery_ctrl`、`u_delivery_buffer`。注意 calculator 同时连着两边的 SRAM：

- 它接收 `abuf_rd_data_*`（旧部分和）、产生 `abuf_wr_data_*`（新部分和）给 [assembly_buffer](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_cacc.v#L262-L286)；
- 它产生 `dlv_data_*`（最终结果）给 [delivery_ctrl](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_cacc.v#L364-L417)。

进入计算核心 [NV_NVDLA_CACC_calculator.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_calculator.v)：它例化了 **128 个**整数累加单元（`u_cell_int_0` ~ `u_cell_int_127`），每个单元负责一条输出通道的累加。看第一个单元的端口：

[NV_NVDLA_CACC_CALC_int16 u_cell_int_0 … calculator.v:7876-7890](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_calculator.v#L7876-L7890) 中：
- `in_data` = `calc_op0_int_0_d1[37:0]`：本节拍 CMAC 送来的新部分和；
- `in_op` = `calc_op1_int_0_d1[47:0]`：从装配缓冲读出的旧部分和；
- `out_partial_data` = `calc_pout_int_0_sum[47:0]`：累加后的新部分和（写回装配缓冲）；
- `out_final_data` = `calc_fout_int_0_sum[31:0]`：交付给 SDP 的最终 32 位结果；
- `in_sel` = `calc_dlv_en_int_d1[0]`：本拍是否为该位置 reduction 的最后一拍（决定走 partial 还是 final）。

CMAC 部分和进入 calculator 后先被打散成 `calc_data_*`。例如 [calculator.v:2290-2300](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_calculator.v#L2290-L2300) 把 `mac_a2accu_data0` 寄存一拍送入 `calc_data_0`（cmac_a 半阵列的第一组），`mac_b2accu_data0` 同理送入 `calc_data_8`（cmac_b 半阵列）。这印证了「同数据广播到 a/b 两半、各自累加不同输出通道」的设计。

128 个单元由 4 个独立门控时钟 `nvdla_cell_clk_0..3` 分别驱动（每时钟带 32 个单元），便于按需关钟省电（SLCG，见 u6-l1）。每个单元的 `cfg_truncate`（5 位截断量）从配置字中拆出，见 [calculator.v:7868](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_calculator.v#L7868)，共 128 个 5 位截断参数。

#### 4.1.4 代码实践

**实践目标**：在源码层面走通「部分和从 CMAC 进入、在 calculator 里与旧部分和相加、再写回装配缓冲」的连接关系。

**操作步骤**：
1. 打开 [NV_NVDLA_cacc.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_cacc.v)，在 `u_calculator` 例化块（约 291 行起）里找到 `abuf_rd_data_0` 和 `abuf_wr_data_0` 两个端口。
2. 确认 `abuf_rd_data_*` 来自 `u_assembly_buffer`，`abuf_wr_data_*` 也送往 `u_assembly_buffer`——这构成 calculator 与装配缓冲之间的「读改写」环路。
3. 打开 [NV_NVDLA_CACC_calculator.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_calculator.v)，跳到 7876 行 `u_cell_int_0`，确认其 `in_op`（旧部分和）与 `out_partial_data`（新部分和）正是这条环路的两端。

**需要观察的现象**：calculator 对装配缓冲是「先读后写同一地址」的关系——旧值进来、新值出去。

**预期结果**：你能用一句话描述这条环路：「calculator 每拍读出某个输出位置的旧部分和，加上 CMAC 的新部分和，饱和后写回同一区域；当该位置累加完毕，另产出一份 final 结果送交付。」（注：具体读写时序与地址仲裁由 assembly_ctrl 控制，本实践只确认数据通路连接，运行级时序「待本地验证」。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 CACC 不能像 CMAC 那样只做组合乘加、而不需要一张 SRAM 来记中间值？

> **参考答案**：因为一个输出位置的完整结果是跨「整个输入通道维 + 卷积窗口」的大量部分和之和，单拍算不完。必须用一个可寻址的存储（assembly_buffer）把每个输出位置累加到一半的中间值存下来，下一拍接着加，直到 reduction 维走完。

**练习 2**：`in_data`、`in_op`、`out_partial`、`out_final` 四个信号分别对应什么？哪个写回装配缓冲，哪个送往交付缓冲？

> **参考答案**：`in_data`=本拍 CMAC 新部分和；`in_op`=装配缓冲读出的旧部分和；`out_partial`=累加并饱和后的新部分和，**写回装配缓冲**；`out_final`=reduction 完成时经舍入/截断/饱和的最终值，**送往交付通路**。`in_sel` 决定本拍是否产出 `out_final`。

---

### 4.2 INT8 / INT16 / FP16 三条精度路径

#### 4.2.1 概念说明

NVDLA 的 nvdlav1 全精度版要同时支持 INT8、INT16、FP16 三种计算精度。CACC 的做法不是做三套完全独立的大模块，而是为「每个累加通道」提供一个精度可配置的小单元：同一时刻根据配置（`cfg_is_int8` / `cfg_is_int` / `cfg_is_fp`）只激活其中一条路径。

三条路径最本质的差异是**累加器位宽**——累加是个不断「长大」的值，精度越高、reduction 越深，所需位宽越宽：

| 精度 | `in_data`（新部分和） | `in_op`（旧部分和/累加器） | `out_partial`（写回） | `out_final`（交付） |
| --- | --- | --- | --- | --- |
| INT8 | 22 位 | 34 位 | 34 位 | 32 位 |
| INT16 | 38 位 | 48 位 | 48 位 | 32 位 |
| FP16 | —（浮点） | 48 位（尾数） | 48 位 | 32 位 |

注意一个关键设计点：**无论哪种精度，最终交付给 SDP 的都是 32 位**。差异只在「累加过程中用多宽的中间值来避免溢出」。INT8 累加器最窄（34 位），INT16/FP16 最宽（48 位）。

每条路径内部都做两件事：① 把 `in_data + in_op` 饱和到累加位宽（得到 `out_partial`）；② 当 `in_sel` 有效时，对累加值做移位截断（`>>> cfg_truncate`）、round-to-nearest 舍入、再饱和到 32 位（得到 `out_final`）。

#### 4.2.2 核心流程

以 INT8 为例（最简单），单拍计算可概括为：

\[
\text{sum} = \text{in\_data} + \text{in\_op}
\]

\[
\text{partial} = \text{sat}_{34}(\text{sum}) \quad \text{（写回装配缓冲）}
\]

\[
\text{final} = \text{sat}_{32}\big(\text{round}(\text{partial} \gg \text{cfg\_truncate})\big) \quad \text{（仅 in\_sel 时，送交付）}
\]

其中 \(\text{sat}_N(\cdot)\) 表示饱和到 N 位有符号范围，\(\text{round}(\cdot)\) 表示四舍五入到整数。

INT16 路径公式形式相同，只是位宽换成 48 位累加，并多了一步**高位符号优化**：当两个操作数的高位都只是单纯的符号扩展（全 0 或全 1）时，跳过完整的高位加法，用更省的逻辑推导高位——这是面积与时序的优化，功能等价。

INT8 饱和判定的核心是检查「符号位与次高符号位是否一致」：

\[
\text{overflow} = \text{sign} \oplus \text{msb}
\]

若 overflow 为真，说明相加后超出 34 位有符号范围，需钳位到 `±(2^{33}-1)`。

#### 4.2.3 源码精读

**INT8 路径**（[NV_NVDLA_CACC_CALC_int8.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_CALC_int8.v)）：

端口位宽见 [CALC_int8.v:26-36](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_CALC_int8.v#L26-L36)：`in_data[21:0]`、`in_op[33:0]`、`out_partial_data[33:0]`、`out_final_data[31:0]`。累加在 [CALC_int8.v:172](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_CALC_int8.v#L172) 完成：

```verilog
assign i_sum_pd_nxt[34:0] = $signed(di_pd) + $signed(oi_pd);
```

22 位 + 34 位扩展为 35 位求和，随后 [CALC_int8.v:191-204](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_CALC_int8.v#L191-L204) 把它饱和到 34 位（`out_partial`）。当 `in_sel` 有效时，[CALC_int8.v:206-221](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_CALC_int8.v#L206-L221) 做移位（`>>> cfg_truncate`）、`i_point5` 舍入与 32 位饱和，得到 `out_final`。

**INT16 路径**（[NV_NVDLA_CACC_CALC_int16.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_CALC_int16.v)）：

端口位宽见 [CALC_int16.v:26-36](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_CALC_int16.v#L26-L36)：`in_data[37:0]`、`in_op[47:0]`、`out_partial_data[47:0]`、`out_final_data[31:0]`。INT16 把 48 位加法拆成「低 32 位正常加 + 高位条件加」两部分。低位加法在 [CALC_int16.v:380-381](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_CALC_int16.v#L380-L381)：

```verilog
assign i_lsum_pd_nxt[32:0] = di_lsb_pd + oi_lsb_pd;   // 低32位加，带1位进位
```

高位加法带符号优化，见 [CALC_int16.v:396-400](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_CALC_int16.v#L396-L400)：当 `in_hsb_same`（两数高位都是纯符号扩展）为真时高位清零，跳过完整高位加法。最终在 [CALC_int16.v:427-444](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_CALC_int16.v#L427-L444) 饱和到 48 位得到 `out_partial`，并在 [CALC_int16.v:446-462](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_CALC_int16.v#L446-L462) 做与 INT8 同形式的移位/舍入/32 位饱和得到 `out_final`。

**精度选择（多路输出合并）**：calculator 把每条通道的 int 路径与 fp 路径的 `out_partial` 用 validity 掩码合并，见 [calculator.v:12305-12306](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_calculator.v#L12305-L12306)：

```verilog
assign calc_pout_0 = ({48{calc_pout_int_vld[0]}} & calc_pout_int_0_sum) |
                     ({48{calc_pout_fp_vld[0]}}  & calc_pout_fp_0_sum_ext);
```

即「整数路径有效时取整数结果，浮点路径有效时取浮点结果」；而 INT8 与 INT16 的区分则在 cell 内部按 `cfg_is_int8` 完成。配置来自寄存器 `reg2dp_proc_precision[1:0]` 与 `reg2dp_clip_truncate[4:0]`（见 [NV_NVDLA_cacc.v:213-223](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_cacc.v#L213-L223)）。

#### 4.2.4 代码实践

**实践目标**：对比 INT8 与 INT16 两条累加路径的位宽差异，并理解「累加完成后结果如何在 delivery_buffer 中等待交付给 SDP」。

**操作步骤**：
1. 打开 [CALC_int8.v:26-36](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_CALC_int8.v#L26-L36) 与 [CALC_int16.v:26-36](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_CALC_int16.v#L26-L36)，并排记录两表的端口位宽。
2. 在两个文件里分别找到「饱和到累加位宽」的代码段：INT8 是 [CALC_int8.v:191-204](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_CALC_int8.v#L191-L204)（饱和到 34 位），INT16 是 [CALC_int16.v:427-444](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_CALC_int16.v#L427-L444)（饱和到 48 位）。
3. 注意两者的 `out_final_data` 都是 `[31:0]`——交付位宽一致。
4. 跳到 [NV_NVDLA_CACC_delivery_buffer.v:1487-1504](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_delivery_buffer.v#L1487-L1504)，看 `out_final` 如何被打包进 `cacc2sdp_pd`。

**需要观察的现象**：INT8 用 34 位累加、INT16 用 48 位累加，但二者都把结果压缩到同样的 32 位交付。

**预期结果**：你能填写并解释下表——

| 路径 | 累加位宽 | 交付位宽 | 饱和代码位置 |
| --- | --- | --- | --- |
| INT8 | 34 | 32 | CALC_int8.v:191-204 |
| INT16 | 48 | 32 | CALC_int16.v:427-444 |

并解释：「累加完成后，每个通道的 32 位 `out_final` 被 calculator 作为 `dlv_data` 送出，经 delivery_ctrl 写入 delivery_buffer；delivery_buffer 把 16 个通道 × 32 位拼成 512 位数据，再加 2 位状态，组成 514 位 `cacc2sdp_pd`，在 SDP 握手就绪（`cacc2sdp_ready`）时平稳交付。」（握手时序「待本地验证」。）

#### 4.2.5 小练习与答案

**练习 1**：为什么 INT16 的累加器（48 位）比 INT8（34 位）宽这么多？

> **参考答案**：INT16 单个乘积项就是 16×16 的结果，本身就比 INT8（8×8）宽；且 INT16 模式下阵列并行度折半（1024 MAC vs 2048 MAC），但 reduction 深度与卷积核尺寸不变，累加值增长更快，需要更宽的累加器避免溢出。

**练习 2**：INT16 路径里的 `in_hsb_same` 优化在什么条件下生效？为什么这样做是安全的？

> **参考答案**：当两个操作数的高位段都只是符号扩展（全 0 或全 1）时生效。因为此时高位不携带有效数值信息，最终和的高位完全可以由低位进位与符号位推导出来，跳过完整高位加法在功能上等价，却省下了宽加法器的面积与延迟。

**练习 3**：三种精度的 `out_final_data` 位宽是否相同？为什么？

> **参考答案**：相同，都是 32 位。因为 CACC 下游 SDP 的接口宽度是固定的（每通道 32 位），不同精度只在累加中间值宽度上区别对待，交付时统一收敛到 32 位。

---

### 4.3 assembly_buffer 与 delivery_buffer：两级缓冲

#### 4.3.1 概念说明

CACC 用两张 SRAM 把「累加」和「交付」解耦：

- **assembly_buffer（装配缓冲，abuf）**：累加过程的「工作内存」。它保存所有正在累加中的部分和。calculator 每拍从它读出旧值、加完后写回新值——这是一个**读改写（read-modify-write）**循环。它的位宽很宽（最高 768 位），好让一拍之内并行处理大量输出通道。
- **delivery_buffer（交付缓冲，dbuf）**：最终结果的「待发送队列」。某个输出位置累加完毕后，其 32 位结果写入 dbuf，随后按 SDP 的握手节拍（`cacc2sdp_valid` / `cacc2sdp_ready`）一拍一拍送出。dbuf 还负责在层结束时产生 done 中断。

之所以要分两张，是因为两个节奏不同：累加跨整个 reduction 维（慢、要存中间值），交付是按 SDP 带宽匀速流出（快、不丢数据）。分开后，calculator 可以全速累加而不被 SDP 反压卡住，dbuf 则吸收节奏差。

两张 SRAM 都是「32 深度、8 个 bank」的结构，且都走 ECC 数据通路（`*_rd_data_ecc`），并带「同一地址同拍又读又写」的断言保护。

#### 4.3.2 核心流程

两级缓冲协同的总流程：

```text
assembly_ctrl  ──发 abuf_rd_addr──▶  assembly_buffer（读旧部分和，3拍延迟）
                                          │ abuf_rd_data（旧部分和）
                                          ▼
                                      calculator（加+饱和+舍入）
                                          │ abuf_wr_data（新部分和，1拍后写）
                                          ▼
                                    assembly_buffer（写回，构成读改写环路）

calculator ──发 dlv_data（最终结果）──▶ delivery_ctrl
                                          │ dbuf_wr_*
                                          ▼
                                    delivery_buffer（存最终结果）
                                          │ cacc2sdp_pd[513:0]（与 SDP 握手送出）
                                          ▼
                                         SDP
                                    （层末拍 → cacc_done → GLB done 中断）
```

几个关键参数：

- **assembly_buffer**：4 片 `nv_ram_rws_32x768`（768 位宽）+ 4 片 `nv_ram_rws_32x544`（544 位宽），共 8 个 bank、32 深；写延迟 1 拍、读延迟 3 拍。
- **delivery_buffer**：8 片 `nv_ram_rws_32x512`（512 位宽），8 个 bank、32 深；写延迟 2 拍、读延迟 3 拍。
- **交付包**：`cacc2sdp_pd[513:0]` = 16 × 32 位数据 + `batch_end`（bit 512）+ `layer_end`（bit 513）。
- **完成中断**：在层最后一拍数据成功交付 SDP 时拉起，2 位对应两组影偶。

#### 4.3.3 源码精读

**assembly_buffer**（[NV_NVDLA_CACC_assembly_buffer.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_assembly_buffer.v)）：

文件注释明确给出延迟：写 1 拍（[assembly_buffer.v:128-132](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_assembly_buffer.v#L128-L132)）、读 3 拍（[assembly_buffer.v:141-145](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_assembly_buffer.v#L141-L145)）。8 个 RAM 实例在 [assembly_buffer.v:275-368](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_assembly_buffer.v#L275-L368)：前 4 个是 `nv_ram_rws_32x768`（如 [u_accu_abuf_0](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_assembly_buffer.v#L275-L284)），后 4 个是 `nv_ram_rws_32x544`（如 [u_accu_abuf_4](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_assembly_buffer.v#L323-L332)）。每个 RAM 都有独立的读使能 `re`、写使能 `we`，共享 5 位地址。文件中还有 8 条 `nv_assert_never` 断言（如 [assembly_buffer.v:403-407](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_assembly_buffer.v#L403-L407)），禁止「同一 bank 同拍同地址又读又写」，这正是读改写环路必须由 assembly_ctrl 精心排程的原因。

> 关于 768 / 544 两种宽度：它们对应不同精度下「一拍并行处理的通道数 × 单通道部分和位宽」的不同打包方式。宽 bank 给高精度/多通道，窄 bank 给低精度场景，使 SRAM 利用率更高。具体位宽分配「待确认」。

**delivery_buffer**（[NV_NVDLA_CACC_delivery_buffer.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_delivery_buffer.v)）：

8 个 RAM 实例在 [delivery_buffer.v:806-890](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_delivery_buffer.v#L806-L890)，全部是 `nv_ram_rws_32x512`（512 位宽）。延迟注释：写 2 拍（[delivery_buffer.v:177-181](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_delivery_buffer.v#L177-L181)）、读 3 拍（[delivery_buffer.v:190-194](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_delivery_buffer.v#L190-L194)）。

交付包的拼装见 [delivery_buffer.v:1487-1504](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_delivery_buffer.v#L1487-L1504)：

```verilog
assign cacc2sdp_pd[31:0]    = cacc2sdp_data0[31:0];   // 通道 0
...                                                  // 共 16 个 32 位通道
assign cacc2sdp_pd[511:480]  = cacc2sdp_data15[31:0]; // 通道 15
assign cacc2sdp_pd[512]      = cacc2sdp_batch_end;    // 本批结束（本仓库恒 0）
assign cacc2sdp_pd[513]      = cacc2sdp_layer_end;    // 本层结束
```

即每拍交付 16 个输出通道的 32 位结果，`layer_end` 标记这是当前卷积层的最后一拍。

**done 中断生成**见 [delivery_buffer.v:1529-1549](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_delivery_buffer.v#L1529-L1549)：

```verilog
assign cacc_done            = dbuf_rd_valid_d3 & dbuf_rd_ready_d3 & dbuf_rd_layer_end_d3;
assign cacc_done_intr_w[0]  = cacc_done & ~intr_sel;   // 第 0 组影偶
assign cacc_done_intr_w[1]  = cacc_done &  intr_sel;   // 第 1 组影偶
assign intr_sel_w           = cacc_done ? ~intr_sel : intr_sel;  // 每次完成翻转
```

也就是说，当层末拍数据真正被 SDP 接走（valid 且 ready 且 layer_end）时，`cacc_done` 拉起；`intr_sel` 在每次完成后翻转，使 `cacc2glb_done_intr_pd[1:0]` 交替指向两组影偶中的某一组——这正是 u2-l4 GLB 中断聚合里「cacc 带 2 个影偶组」的来源。该信号经顶层 `NV_nvdla.v` 连到 GLB 的 `cacc done` 中断源。

#### 4.3.4 代码实践

**实践目标**：摸清两级缓冲的容量、延迟与对外接口，并确认交付握手与中断时序。

**操作步骤**：
1. 在 [assembly_buffer.v:275-368](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_assembly_buffer.v#L275-L368) 统计 RAM 的种类与数量；记下写 1 拍 / 读 3 拍的延迟。
2. 在 [delivery_buffer.v:806-890](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_delivery_buffer.v#L806-L890) 统计 RAM 的种类与数量；记下写 2 拍 / 读 3 拍的延迟。
3. 阅读 [delivery_buffer.v:1487-1504](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_delivery_buffer.v#L1487-L1504)，确认 `cacc2sdp_pd` 由 16×32 位数据加 2 位状态组成。
4. 阅读 [delivery_buffer.v:1529-1549](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_delivery_buffer.v#L1529-L1549)，追踪 `cacc_done` 如何在层末拍产生并交替点亮两组影偶中断。
5. （可选）在仿真里用 `+NVDLA_PRINT_CACC` 编译选项打开 CACC 打印（见 [delivery_buffer.v:1509-1522](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_delivery_buffer.v#L1509-L1522)），观察每拍交付的 512 位数据与 `layer end` 标记。

**需要观察的现象**：装配缓冲与交付缓冲深度都是 32，但位宽与 bank 组成不同；交付包每拍 16 通道；中断只在层末拍且 SDP 真正接收时产生。

**预期结果**：你能画一张「abuf（读改写环路，宽位）→ calculator → dbuf（交付队列，16 通道/拍）→ SDP」的框图，并标出各自延迟。中断时序的具体波形「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么装配缓冲用很宽的 bank（768/544 位），而交付缓冲统一用 512 位？

> **参考答案**：装配缓冲要在一拍内并行保存大量输出通道的部分和（中间值位宽随精度变化，宽 bank 提高并行度与带宽），所以宽度大且分两档；交付缓冲每拍固定交付 16 通道×32 位=512 位，宽度由下游 SDP 接口决定，所以统一 512 位。

**练习 2**：`cacc_done` 的产生条件是哪三个信号的与？为什么必须同时要求 `ready`？

> **参考答案**：`cacc_done = dbuf_rd_valid_d3 & dbuf_rd_ready_d3 & dbuf_rd_layer_end_d3`——层末拍、数据有效、且 SDP 真正接收（ready）。要求 ready 是为了确保最后一拍结果确实被下游取走，否则提前报 done 会导致「报完成但数据还卡在 CACC」的不一致。

**练习 3**：`cacc2glb_done_intr_pd` 为什么是 2 位？它与影偶配置什么关系？

> **参考答案**：2 位对应两组影偶（shadow）。CACC 每完成一层，`intr_sel` 翻转一次，使中断交替落在 bit0 / bit1，分别表示「第 0 组配置的那层完成」与「第 1 组配置的那层完成」。GLB 据此区分是哪一组配置运行结束（见 u2-l4），配合 producer/consumer 无缝切换下一层。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个「源码阅读 + 数据通路追踪」任务：

**任务**：为一个 INT16 卷积层，跟踪一个输出通道像素从 CMAC 进入到交付 SDP 的完整通路，并解释沿途的精度处理与缓冲作用。

**建议步骤**：

1. **配置侧**：在 [NV_NVDLA_cacc.v:212-226](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_cacc.v#L212-L226) 找到 `reg2dp_proc_precision`（选 INT16）、`reg2dp_clip_truncate`（截断量）、`reg2dp_batches`、`reg2dp_op_en`。说明 CPU 通过 CSB 写入这些寄存器、经 regfile 的 producer/consumer 影偶切换后送达数据通路（承接 u2-l3）。
2. **入侧**：从 `mac_a2accu_data0`（[cacc.v:17](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_cacc.v#L17)）出发，经 calculator 的 `calc_data_0`（[calculator.v:2290](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_calculator.v#L2290)）进入某个 `u_cell_int_*` 单元。
3. **累加侧**：在 `u_cell_int_*`（[calculator.v:7876-7890](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_calculator.v#L7876-L7890)）里说明 `in_op`（48 位旧部分和）来自装配缓冲、`out_partial`（48 位新部分和）写回装配缓冲，构成读改写循环；指出饱和发生在 [CALC_int16.v:427-444](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_CALC_int16.v#L427-L444)。
4. **交付侧**：当 reduction 完成（`in_sel`），说明 [CALC_int16.v:446-462](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_CALC_int16.v#L446-L462) 把 48 位累加值经移位/舍入/饱和成 32 位 `out_final`，再经 `dlv_data` → delivery_ctrl → delivery_buffer，最终拼成 [cacc2sdp_pd](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_delivery_buffer.v#L1487-L1504) 交付 SDP。
5. **中断侧**：在层末拍，[cacc_done](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_delivery_buffer.v#L1529-L1532) 经 `cacc2glb_done_intr_pd` 上报 GLB，CPU 据此知道本层结束并可切换下一层配置。

**交付物**：一张标注了「精度选择、48 位累加器、读改写环路、16 通道/拍交付、2 位影偶中断」的 CACC 数据通路框图，加上一段说明为什么这套设计能让 CACC 全速累加而不被 SDP 反压卡住。（运行级波形「待本地验证」。）

## 6. 本讲小结

- CACC 是卷积主流水线的收尾计算级，职责是把 CMAC 的部分和累加成完整卷积结果，并按精度做舍入与饱和后交付 SDP。
- 顶层 [NV_NVDLA_cacc.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_cacc.v) 例化六个子模块：regfile（配置）、assembly_ctrl/buffer（装配）、calculator（计算）、delivery_ctrl/buffer（交付）。
- calculator 例化 128 个精度可配置累加单元，每个单元做「旧部分和 + 新部分和 → 饱和写回 / 最终交付」。
- 三条精度路径差异在累加器位宽：INT8 用 34 位、INT16 用 48 位、FP16 用 48 位尾数；三者交付位宽统一为 32 位。INT16 还带高位符号扩展优化。
- 两级缓冲解耦了累加与交付：assembly_buffer（宽位、读改写环路、写1读3）保存中间部分和；delivery_buffer（512 位、16 通道/拍、写2读3）缓存最终结果并按 SDP 握手平稳送出。
- 完成中断在层末拍且 SDP 真正接收时产生，2 位交替点亮对应两组影偶，上报 GLB 后配合 producer/consumer 无缝切换下一层。

## 7. 下一步学习建议

- **向后看后处理**：CACC 的 `cacc2sdp_pd` 下一站就是 SDP。建议接着学 u5-l1（SDP 单点数据处理器），看 SDP 如何对 CACC 送来的逐点结果做 BN/EW/LUT 激活。
- **向深看缓冲控制**：本讲只点了 assembly_ctrl / delivery_ctrl 的作用，没展开状态机。若想理解「读改写地址如何排程、信用反压 `accu2sc_credit` 如何防溢出」，可精读 `NV_NVDLA_CACC_assembly_ctrl.v` 与 `NV_NVDLA_CACC_delivery_ctrl.v`，并对照 u3-l4 CSC 的 credit 反压回路。
- **向广看浮点单元**：FP16 路径依赖的浮点加法/格式转换来自 vlibs（见 u6-l4 浮点运算单元），可结合 `NV_NVDLA_CACC_CALC_fp_48b.v` 与 `HLS_fp*` 原语一起读。
- **向系统看集成**：本讲的 done 中断最终汇入 GLB（u2-l4），并与影偶配置（u2-l3）协作。学完 u8-l4 端到端集成后，可以回头用本讲的通路理解「编程一个卷积层」的完整链路。
