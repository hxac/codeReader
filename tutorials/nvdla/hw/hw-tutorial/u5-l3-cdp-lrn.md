# CDP：通道数据处理器（LRN）

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 **CDP（Channel Data Processor，通道数据处理器）** 在 NVDLA 后处理流水线里的位置与职责——它对卷积输出做**跨通道**运算，核心算子是 **LRN（Local Response Normalization，局部响应归一化）**。
- 把 CDP 数据通路拆成 `cvtin → (bufferin+sum) / lut → intp → mul → cvtout` 这一串计算单元，并说明每个单元解决什么问题。
- 解释一次 LRN 是如何由 **sum（求平方和）** 与 **mul（乘法）** 两个单元协作完成的：sum 算出邻域平方和 Σ，LUT+intp 逼近归一化函数 f(Σ)，mul 把 f(Σ) 乘回到原始激活上。
- 说清楚为什么输入要先经 **cvtin（定点→浮点）** 转成内部浮点格式参与 sum/LUT 计算，再经 **cvtout（浮点→定点）** 转回 int8/int16/fp16 输出。
- 读懂 CDP 的 RDMA/WDMA 读写通路与寄存器配置入口（CDP 核心寄存器页基址 0xf000、CDP RDMA 寄存器页基址 0xe000）。

## 2. 前置知识

在进入 CDP 之前，先建立三个直觉。

**（1）为什么需要“跨通道”的运算？**

卷积输出是一个三维张量：宽 W × 高 H × 通道 C。前面讲过的 **SDP（u5-l1）** 和 **PDP（u5-l2）** 都是“逐点”或“逐平面”运算——SDP 对每个激活点独立处理，PDP 在 W-H 平面内做池化，二者都**不跨通道**。但有些算子需要同时看同空间位置、相邻若干通道的数据，典型代表就是 **LRN**：它抑制那些在多个通道上都强烈响应的特征，让响应在不同通道间“互相竞争”，从而提升泛化能力。这类“沿通道方向滑动一个窗口”的运算就是 CDP 的职责。

**（2）LRN 的数学含义。**

跨通道 LRN（ACROSS channels）的经典公式为：

\[
b_x(i) = a_x(i)\cdot\left( k + \frac{\alpha}{n}\sum_{j} a_y(j)^2 \right)^{-\beta}
\]

其中 \(a_x(i)\) 是当前位置第 \(i\) 个通道的输入，求和下标 \(j\) 遍历以 \(i\) 为中心、宽度为 \(n\) 的**通道邻域**，\(k,\alpha,\beta\) 是超参数。直观地：先对邻域内各通道的激活**平方求和**得到 Σ，再用一个非线性函数 \(f(\Sigma)=(k+\alpha\Sigma)^{-\beta}\) 把它变成一个缩放系数，最后把该系数**乘回到**原始激活 \(a_x(i)\) 上。

关键观察：Σ 是若干个平方数的和，数值范围跨度很大；而 \(f(\Sigma)\) 是带实数幂的非线性函数。这两件事用定点整数做都会很难（平方和容易溢出、实数幂无法直接实现），所以 CDP 内部用**浮点 + 查表（LUT）+ 线性插值**来实现。这正是本讲“定点↔浮点转换”主题的由来。

**（3）NVDLA 引擎的通用骨架。**

CDP 和 SDP/PDP 一样遵循后处理引擎的通用结构：一个**读 DMA（RDMA）**把输入特征图从存储（MCIF/CVIF）搬进来 → 一段**计算数据通路（DP）** → 一个**写 DMA（WDMA）**把结果写回存储；两侧都配 CSB 寄存器口供 CPU 配置。理解了 u5-l1/u5-l2 的 RDMA→core→WDMA 骨架，CDP 的读写通路可以直接类推，本讲重点放在它**特有**的 DP 计算流水上。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [vmod/nvdla/cdp/NV_NVDLA_cdp.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_cdp.v) | CDP 顶层：例化 RDMA、DP、WDMA、寄存器、NaN 预处理与 4 个时钟门控单元，连接对外端口 |
| [vmod/nvdla/cdp/NV_NVDLA_CDP_dp.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_dp.v) | **计算数据通路核心**：把 cvtin/bufferin/sum/lut/intp/mul/cvtout 串成一条 LRN 流水线 |
| [vmod/nvdla/cdp/NV_NVDLA_CDP_DP_cvtin.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_DP_cvtin.v) | 输入转换器：int8/int16/fp16 → 内部格式，用 datin_offset/scale/shifter 做缩放 |
| [vmod/nvdla/cdp/NV_NVDLA_CDP_DP_sum.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_DP_sum.v) | 平方求和单元：对通道邻域内的激活做平方累加，得到 Σ |
| [vmod/nvdla/cdp/NV_NVDLA_CDP_DP_lut.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_DP_lut.v) | 查找表：用软件在线写入的表内容逼近非线性函数 f |
| [vmod/nvdla/cdp/NV_NVDLA_CDP_DP_intp.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_DP_intp.v) | 插值器：在 LUT 相邻表项之间做线性插值，提高精度 |
| [vmod/nvdla/cdp/NV_NVDLA_CDP_DP_mul.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_DP_mul.v) | 乘法器：把原始激活 × LUT/插值得到的缩放系数（即 LRN 的最后一步乘法） |
| [vmod/nvdla/cdp/NV_NVDLA_CDP_DP_MUL_unit.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_DP_MUL_unit.v) | 单个乘法叶子单元：定点乘用 `$signed` 乘法器、浮点乘复用 `HLS_fp17_mul` |
| [vmod/nvdla/cdp/NV_NVDLA_CDP_DP_cvtout.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_DP_cvtout.v) | 输出转换器：内部格式 → int8/int16/fp16，含饱和计数 |
| [vmod/nvdla/cdp/NV_NVDLA_CDP_rdma.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_rdma.v) | 读 DMA 顶层：IG→cq→EG 三级，配独立 CSB 寄存器口 |
| [vmod/nvdla/cdp/NV_NVDLA_CDP_REG_dual.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_REG_dual.v) | 影偶寄存器文件：LRN_CFG/OP_ENABLE 等逐层参数（d0/d1 两组轮换） |

---

## 4. 核心概念与源码讲解

### 4.1 CDP 跨通道 LRN：总体架构

#### 4.1.1 概念说明

CDP 是 NVDLA 后处理四件套（SDP/PDP/CDP/Rubik）中的“通道维”引擎。它接收上游（通常是 SDP，或直接来自 CACC）的特征图，在**通道方向**上完成 LRN 归一化，再把结果交还存储或送给下一级。它与 SDP/PDP 的根本区别在“窗口方向”：

| 引擎 | 运算方向 | 典型算子 |
|------|----------|----------|
| SDP（u5-l1） | 逐点（无窗口） | BN、逐元素、激活 |
| PDP（u5-l2） | W-H 空间平面内 | max/average pooling |
| **CDP（本讲）** | **通道 C 方向滑动窗口** | **LRN** |
| Rubik（u5-l4） | 布局重排 | reshape/contract |

CDP 内部并不直接实现 \((k+\alpha\Sigma)^{-\beta}\) 这个实数幂——硬件做任意实数幂代价太高。它的做法是：用一个**可被软件在线编程的查找表（LUT）+ 线性插值**来逼近任意非线性函数 f。于是 LRN 在 CDP 里被拆成三步可硬件实现的运算：

1. **sum**：对通道邻域内激活求平方和 Σ；
2. **lut + intp**：用查表 + 插值算出 f(Σ)；
3. **mul**：把 f(Σ) 乘到原始激活上，得到归一化输出。

这也解释了为什么本讲把 sum 与 mul 称作“LRN 的左右手”——sum 提供 Σ，mul 应用 f(Σ)，中间由 LUT 桥接。

#### 4.1.2 核心流程

CDP 的整体数据流如下（自左向右）：

```
        ┌───────── RDMA ─────────┐
存储 ──>│ ig → cq → eg (读返回)   │──> cdp_rdma2dp (86 位/拍)
        └─────────────────────────┘
                                          ┌──→ cvtin ──→ bufferin ──→ sum(Σ) ──┐
cdp_rdma2dp ──> DP_nan(预处理) ──┐        │                                  ├──> LUT_ctrl ──> lut ──> intp ──┐
                                 └────────┤                                  │                                  │
                                          └──→ (syncfifo 把同一份输入分发) ───┘                                  ├──> mul(a×f) ──> cvtout ──> cdp_dp2wdma
                                                                                sync2mul(原始输入 a) ──────────┘
        ┌───────── WDMA ─────────┐
存储 <──│ 写请求 + done 中断       │<── cdp_dp2wdma (79 位/拍)
        └─────────────────────────┘
```

要点：
- **输入只有一份**，由 `syncfifo` 扇出成三路：一路去 sum 算 Σ，一路（`sync2mul`）作为“原始激活 a”留到 mul 用，一路旁路到输出转换。
- **两条并行子通路在 mul 处汇合**：sum→lut→intp 这条算出系数 f(Σ)，sync2mul 这条保留原始 a，mul 把两者相乘。
- **cvtin/cvtout 是格式边界**：进入 sum/LUT 用浮点，输出回到 int8/int16/fp16 用定点。

#### 4.1.3 源码精读

CDP 顶层 [NV_NVDLA_cdp.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_cdp.v) 例化了五个核心子模块：`u_rdma` / `u_dp` / `u_wdma` / `u_reg` / `u_DP_nan`，外加 4 个时钟门控。

[顶层例化 RDMA/DP/WDMA/reg（NV_NVDLA_cdp.v:L228-L509）](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_cdp.v#L228-L509)：这一段把 RDMA 读回的数据 `cdp_rdma2dp_pd`（86 位）接到 DP，把 DP 算完的 `cdp_dp2wdma_pd`（79 位）接到 WDMA；寄存器 `u_reg` 把所有 `reg2dp_*` 配置（数据类型、bypass 开关、LUT 参数等）扇出给 DP 与 WDMA。

[精度选择信号 fp16_en（NV_NVDLA_cdp.v:L224）](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_cdp.v#L224)：`fp16_en = (reg2dp_input_data_type[1:0] == 2'h2)`。数据类型编码在后文 dp.v 里完整：`0=int8, 1=int16, 2=fp16`。`fp16_en` 直接驱动一个 SLCG 门控时钟 `nvdla_op_gated_clk_fp16`。

[四个 SLCG 二级时钟门控（NV_NVDLA_cdp.v:L263-L301）](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_cdp.v#L263-L301)：core/wdma 各一个；fp16 与 int 各一个，且**互斥**——`u_slcg_fp16` 用 `slcg_op_en[2] & fp16_en`，`u_slcg_int` 用 `slcg_op_en[3] & (~fp16_en)`。也就是说 int8/int16 走 int 门控时钟、fp16 走 fp16 门控时钟，同一时刻只开一套，空闲时整套门控关钟省电（SLCG 概念见 u6-l1）。

[NaN 预处理 u_DP_nan（NV_NVDLA_cdp.v:L306-L321）](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_cdp.v#L306-L321)：在 RDMA 与 DP 之间插入一级 NaN 检测。它根据 `reg2dp_nan_to_zero` 决定是否把 fp16 的 NaN/Inf 输入“冲零”（flush-to-zero），并统计 `dp2reg_nan_input_num` / `dp2reg_inf_input_num`。这是 fp16 路径特有的清理动作，定点路径上基本透传。

> 寄存器确认：在自动生成的影偶寄存器文件里，能看到 [LRN_CFG 的 normalz_len 字段（NV_NVDLA_CDP_REG_dual.v:L461-L464）](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_REG_dual.v#L461-L464) 和 [nan_to_zero 字段（NV_NVDLA_CDP_REG_dual.v:L466-L469）](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_REG_dual.v#L466-L469)。寄存器名 `NVDLA_CDP_D_LRN_CFG_0` 直接证实了 CDP 就是 LRN 引擎；`D_` 前缀代表 dual_reg（d0/d1 影偶两组，机制见 u2-l3）。

#### 4.1.4 代码实践

**目标**：在顶层确认 CDP 的对外接线，建立“读端口→分区归属”的直觉。

1. 打开 [NV_NVDLA_cdp.v:L11-L51](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_cdp.v#L11-L51)，把端口分成四类：CSB 配置口（`csb2cdp_*` / `cdp2csb_*`）、存储读口（`cdp2mcif_rd_*` / `cdp2cvif_rd_*`）、存储写口（`cdp2mcif_wr_*` / `cdp2cvif_wr_*`）、中断（`cdp2glb_done_intr_pd[1:0]`）。
2. 注意 `cdp2glb_done_intr_pd` 是 **2 位**——对应 producer/consumer 两个影偶组，交替点亮（与 u5-l2 的 PDP、u3-l6 的 CACC 同构，见 4.4 节）。
3. 注意 CDP 有**两套** CSB 端口：`csb2cdp_req_*`（核心配置）与 `csb2cdp_rdma_req_*`（RDMA 独立配置）。说明 RDMA 寄存器与核心寄存器是分开的两个 4KB 页。

**预期结果**：你能说出 CDP 同时向 MCIF 和 CVIF 各发读/写请求（二选一由寄存器 `*_ram_type` 决定，见 u4-l1），且中断分两组上报 GLB。**待本地验证**：若环境允许，可在波形上观察一次 CDP 完成时 `cdp2glb_done_intr_pd` 的两拍脉冲。

#### 4.1.5 小练习与答案

**练习 1**：CDP 与 PDP 都是后处理引擎，二者最大的区别是什么？
**答**：窗口方向不同。PDP 在 W-H **空间平面**内做池化（不跨通道）；CDP 在 **通道 C 方向**滑动窗口做 LRN。

**练习 2**：CDP 为什么不直接用硬件计算 \((k+\alpha\Sigma)^{-\beta}\)？
**答**：因为它是含实数幂的非线性函数，硬件直接实现代价高且不灵活。CDP 改用“软件在线编程的 LUT + 线性插值”来逼近任意 f，既省硬件又能支持多种激活/归一化曲线。

---

### 4.2 intp / lut / mul / sum：LRN 计算单元流水线

#### 4.2.1 概念说明

CDP 的计算核心 [NV_NVDLA_CDP_dp.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_dp.v) 是一条流水线，把 LRN 拆成若干级：

| 级 | 模块 | 作用 |
|----|------|------|
| 0 | `cvtin` | 输入格式转换 + 缩放（datin_offset/scale/shifter） |
| 1 | `syncfifo` | 把同一份转换结果扇出给 itp/mul/ocvt 三路 |
| 2 | `bufferin` | 把邻域内多通道数据缓存对齐，喂给 sum |
| 3 | `sum` | 通道邻域内**平方求和**，得 Σ |
| 4 | `LUT_ctrl` + `lut` | 用 Σ 查表，得到 f 的表项 |
| 5 | `intp` | 在表项间**线性插值**，得平滑的 f(Σ) |
| 6 | `mul` | **原始激活 a × f(Σ)**，完成 LRN |
| 7 | `cvtout` | 输出格式转换 + 饱和（datout_offset/scale/shifter） |

四个“计算单元” intp/lut/mul/sum 在本节聚焦——它们对应 LRN 公式的三段：sum 算 Σ、（lut+intp）算 f、mul 算 a·f。

#### 4.2.2 核心流程

把 LRN 公式对照到硬件：

\[
\underbrace{b_x = a_x}_{\text{sync2mul 保留原始 }a} \cdot \underbrace{f\Big(\underbrace{\textstyle\sum_j a_y(j)^2}_{\text{sum 单元}}\Big)}_{\text{lut+intp 单元}} \quad\text{最终乘法在 mul 单元}
\]

- **sum**：先把每个通道激活平方（`int16_sq_*`、`int8_*_sq`），再沿通道窗口累加。窗口宽度由 `reg2dp_normalz_len[1:0]` 选择。
- **lut + intp**：以 Σ 为地址查一张软件预先写好的表，得到 f 在若干采样点上的值；若 Σ 落在两个采样点之间，用 `intp` 做线性插值得到中间值。这样一条折线就能逼近 \((\cdot)^{-\beta}\) 这类光滑曲线。
- **mul**：`sync2mul` 一直保留着原始激活 a（未经 sum/lut 处理的那一路），与 intp 算出的系数在 mul 处相乘。这正是“sum 与 mul 协作完成 LRN”的含义。

旁路开关：寄存器 `reg2dp_sqsum_bypass` 可跳过 sum（把原始输入直接喂给 LUT，用于把 CDP 当作纯逐点 LUT 激活引擎，而非 LRN）；`reg2dp_mul_bypass` 可跳过最后的乘法。

#### 4.2.3 源码精读

[数据类型解码（NV_NVDLA_CDP_dp.v:L298-L318）](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_dp.v#L298-L318)：把 `reg2dp_input_data_type` 解码成三个互斥使能 `int8_en / int16_en / fp16_en`（分别对应编码 0/1/2）。这三个使能贯穿整条流水，决定每级按 int8（每拍 8 元素）/ int16（每拍 4 元素）/ fp16 选择数据切分与计算单元。

[sqsum_bypass 使能（NV_NVDLA_CDP_dp.v:L320-L326）](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_dp.v#L320-L326)：`sqsum_bypass_en = (reg2dp_sqsum_bypass == 1)`。

[bufferin 与 sum 平方和通路的旁路（NV_NVDLA_CDP_dp.v:L370-L381）](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_dp.v#L370-L381)：当 `sqsum_bypass_en` 为真时，`bufin_pd/bufin_pvld` 被强制置 0，bufferin→sum 这条平方和通路被关掉；否则走正常 LRN。紧接着的 [sum 单元例化（NV_NVDLA_CDP_dp.v:L384-L399）](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_dp.v#L384-L399) 用 `reg2dp_normalz_len` 控制邻域宽度，输出 `sum2itp_pd`（Σ）。

[sum 内部的平方计算（NV_NVDLA_CDP_DP_sum.v:L46-L57）](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_DP_sum.v#L46-L57)：声明了 `int16_sq_0..11`（33 位平方积）与 `int8_msb_sq_*` 等寄存器——名字里的 `sq`（square）直接说明这一级在做“平方”，随后才会累加成 Σ。

[LUT 控制器与旁路选择（NV_NVDLA_CDP_dp.v:L420-L481）](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_dp.v#L420-L481)：`lutctrl_in_pd = sqsum_bypass_en ? lutctrl_in_sqsum_bypass : sum2itp_pd`——正常 LRN 时 LUT 的输入是 Σ；旁路时是原始输入本身。LUT_ctrl 据此算出查表索引 `dp2lut_X_entry_*` / `dp2lut_Xinfo_*`，交给 [lut 表查表（NV_NVDLA_CDP_dp.v:L484-L570）](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_dp.v#L484-L570)，再由 [intp 插值（NV_NVDLA_CDP_dp.v:L574-L662）](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_dp.v#L574-L662) 输出 `intp2mul_pd_*`（f 的值）。

[mul 乘法单元（NV_NVDLA_CDP_dp.v:L676-L702）](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_dp.v#L676-L702)：mul 同时接收两路——`intp2mul_pd_*`（系数 f）与 `sync2mul_pd`（原始激活 a），二者相乘输出 `mul2ocvt_pd`。受 `reg2dp_mul_bypass` 控制。

进入乘法叶子单元 [MUL_unit（NV_NVDLA_CDP_DP_MUL_unit.v:L121-L181）](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_DP_MUL_unit.v#L121-L181) 可以看到两种实现：
- 定点路径：`mul_int_lsb = $signed(intp2mul_pd_0) * $signed(datin_pd_lsb)`（[L128](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_DP_MUL_unit.v#L128)），即“系数 × 原始激活”的有符号定点乘；
- 浮点路径：例化 [HLS_fp17_mul（NV_NVDLA_CDP_DP_MUL_unit.v:L158-L170）](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_DP_MUL_unit.v#L158-L170)，把 `datin_pd`（a）与 `intp2mul_pd_0`（f）都按 fp17 浮点相乘（fp17 内部格式见 u6-l4）。
最后由 [输出选择（NV_NVDLA_CDP_DP_MUL_unit.v:L180-L181）](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_DP_MUL_unit.v#L180-L181) 按 `fp16_en_sync` 在浮点积与定点积之间二选一。

> 一句话总结本节：**sum 把邻域平方和算出来，lut+intp 把它变成缩放系数，mul 把系数乘回原始激活——三者接力完成一次 LRN。**

#### 4.2.4 代码实践

**目标**：在源码里“数”出 sum 与 mul 是如何接到同一份输入上的，验证它们确实协作。

1. 在 [NV_NVDLA_CDP_dp.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_dp.v) 中找到 `syncfifo` 例化（L348-L367），观察它把 `cvt2sync_*` 扇出成 `sync2itp_*` / `sync2mul_*` / `sync2ocvt_*` 三路——`sync2mul` 就是保留给 mul 用的“原始激活 a”。
2. 分别打开 [sum.v:L46-L57](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_DP_sum.v#L46-L57) 与 [MUL_unit.v:L128](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_DP_MUL_unit.v#L128)：前者是“平方”（为 Σ 服务），后者是“系数×激活”（为最终 LRN 输出服务）。
3. 设想 `reg2dp_mul_bypass=1` 的效果：跟踪 [mul.v:L245-L246](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_DP_mul.v#L245-L246)，`mul2ocvt_pd_f = mul_bypass_en ? intp_out_ext : {...}`——bypass 时输出就是 intp 的结果（即 f 本身），相当于把 CDP 当作纯查表激活。

**预期结果**：你能画出 sum 与 mul 共享同一份 `syncfifo` 输出、却在 mul 处才汇合相乘的连接关系。**待本地验证**：在仿真中设 `sqsum_bypass=0, mul_bypass=0` 跑一个 LRN 层，比对 C-model 输出。

#### 4.2.5 小练习与答案

**练习 1**：为什么 mul 需要同时拿到 `intp2mul_pd`（系数）和 `sync2mul_pd`（原始激活）两路输入？
**答**：因为 LRN 输出 \(b_x = a_x \cdot f(\Sigma)\) 同时依赖原始激活 \(a_x\) 和系数 \(f(\Sigma)\)。`syncfifo` 把原始激活一路旁路保留下来（`sync2mul`），系数由 sum→lut→intp 另一条通路算出，二者在 mul 相乘。

**练习 2**：`sqsum_bypass=1` 时 CDP 还是在做 LRN 吗？
**答**：不是。此时 sum 平方和通路被关闭，LUT 的输入变成原始激活本身，CDP 退化为一个“逐点查表激活”引擎（用 LUT 逼近任意逐元素函数），不再做跨通道归一化。

---

### 4.3 定点↔浮点转换：cvtin 与 cvtout

#### 4.3.1 概念说明

CDP 对外支持三种数据格式：int8、int16、fp16；但**内部**的 sum（平方和）与 LUT（非线性函数）必须用**浮点**计算，原因有二：

1. **动态范围**：邻域平方和 Σ 的量级随通道数和激活幅度剧烈变化，定点很容易溢出或丢精度；浮点的指数域天然覆盖大动态范围。
2. **非线性函数**：\((\cdot)^{-\beta}\) 这类曲线用定点查表需要极多表项，浮点下用“指数 + 尾数”分离的方式查表更省表项、更精确。

因此 CDP 在数据通路两头各放一个转换器：**cvtin** 把外部定点/浮点输入规整成内部计算格式并做线性缩放；**cvtout** 把内部结果转回外部格式并做饱和。两个转换器都遵循 NVDLA 统一的“RSC（可重配置缩放）三参数”模型：

\[
\text{out} = \text{truncate}_{\text{shifter}}\big((\text{in} + \text{offset}) \times \text{scale}\big)
\]

其中 offset / scale / shifter 由寄存器配置，`>> shifter` 完成定标。这一模型与 SDP（u5-l1）的 cvt 单元同构。

#### 4.3.2 核心流程

- **cvtin（输入侧）**：用 `reg2dp_datin_offset / datin_scale / datin_shifter`。它输出两份：`cvt2buf_pd`（去 bufferin/sum，做平方和）与 `cvt2sync_pd`（去 syncfifo，作为原始激活旁路）。
- **cvtout（输出侧）**：用 `reg2dp_datout_offset / datout_scale / datout_shifter`，并统计 `dp2reg_*_out_saturation`（发生过饱和的元素个数，供软件排查精度损失）。

#### 4.3.3 源码精读

[cvtin 例化与缩放参数（NV_NVDLA_CDP_dp.v:L329-L345）](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_dp.v#L329-L345)：把 `datin_offset/scale/shifter` 接进 cvtin。

[cvtin 内部把三参数接到 RSC 转换单元（NV_NVDLA_CDP_DP_cvtin.v:L221-L268）](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_DP_cvtin.v#L221-L268)：可以看到 `cfg_alu_in_rsc_z = reg2dp_datin_offset`（加偏置）、`cfg_mul_in_rsc_z = reg2dp_datin_scale`（乘系数）、`cfg_truncate_rsc_z = reg2dp_datin_shifter`（截位移位）——正是上文 RSC 模型的三参数。cvtin 例化了多个这样的转换单元（对应每拍多个元素并行）。

[cvtout 例化（NV_NVDLA_CDP_dp.v:L721-L740）](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_dp.v#L721-L740)：输出转换的 ready 直接接 `cdp_dp2wdma_ready`，输出 `cdp_dp2wdma_pd`（79 位）送 WDMA。

[cvtout 内部缩放参数与饱和计数（NV_NVDLA_CDP_DP_cvtout.v:L49-L56 与 L253-L254）](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_DP_cvtout.v#L49-L56)：声明了 `sat_cnt`（饱和计数器）与影偶两组 `dp2reg_d0/d1_out_saturation`；[L253-L254](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_DP_cvtout.v#L253-L254) 把 `datout_offset / datout_scale` 接到 RSC 单元（datout_shifter 同样接到 cfg_truncate）。一旦转换结果超出目标格式表示范围，就饱和到最大/最小值并累加 `sat_cnt`。

> 浮点实现：浮点路径下的乘法用的是 [HLS_fp17_mul（MUL_unit.v:L158-L170）](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_DP_MUL_unit.v#L158-L170)——NVDLA 内部统一采用 fp17 中间浮点格式（u6-l4），cvtin/cvtout 在格式边界完成 fp16↔fp17 与定点↔浮点的桥接。所以“输入先转浮点、算完再转回”指的是：定点输入经 cvtin 规整成内部浮点参与 sum/LUT/mul，结果再经 cvtout 转回目标定点/浮点格式。

#### 4.3.4 代码实践

**目标**：理解 cvtin/cvtout 为何必须存在，并用三参数公式量化一次缩放。

1. 阅读 [cvtin.v:L221-L268](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_DP_cvtin.v#L221-L268)，确认输入转换 = `(in + datin_offset) × datin_scale`，再按 `datin_shifter` 移位定标。
2. 假设要把 int8 输入（范围 ±127）映射到内部浮点计算范围，软件应如何选 `datin_scale`（放大）与 `datout_scale`（缩小）？写下你的取值思路。
3. 阅读 [cvtout.v:L49-L56](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_DP_cvtout.v#L49-L56)，找出 `sat_cnt` 在何时自增，并说明软件读 `OUT_SATURATION` 寄存器能发现什么问题。

**预期结果**：你能解释“若不做 cvtin 直接把 int8 喂给 sum，平方和会很快溢出 16 位”，从而理解浮点中间格式的必要性；并能说出 `OUT_SATURATION` 非零意味着输出发生了限幅、可能需要调整 scale/shifter。**待本地验证**：在 C-model 里对比“开启/关闭输入缩放”下的 LRN 数值误差。

#### 4.3.5 小练习与答案

**练习 1**：为什么 LRN 的平方和 Σ 不用定点算？
**答**：邻域平方和的数值随通道数与激活幅度变化剧烈，定点易溢出或丢精度；浮点的指数域能覆盖大动态范围，更适合累加平方。

**练习 2**：cvtout 的 `OUT_SATURATION` 计数器变非零，说明什么？
**答**：说明输出转换时有元素超出了目标格式（int8/int16/fp16）的表示范围，被饱和限幅。软件应据此调整 `datout_scale/shifter` 或检查上游数值范围，否则会引入精度损失。

---

### 4.4 RDMA / WDMA 读写通路与配置入口（衔接模块）

#### 4.4.1 概念说明

CDP 的读写通路与 SDP/PDP 完全同构（见 u4-l1/u4-l2 的 IG→cq→EG 模型），本节只点出 CDP 特有的两点：**RDMA 有独立寄存器页**，以及**完成中断分两组上报 GLB**。

#### 4.4.2 核心流程

- **RDMA**：`ig`（ingress）按描述符游走源端 3D 地址发读请求 → `cq`（命令队列）记录在途事务上下文 → `eg`（egress）按 id 把返回数据送回 DP。RDMA 拥有独立 CSB 端口。
- **WDMA**：把 DP 算完的结果写回 MCIF/CVIF，完成后产生 done 中断。
- **中断**：`cdp2glb_done_intr_pd[1:0]` 两位交替点亮，对应 producer/consumer 两个影偶组（与 CACC/PDP 一致），最终汇入 GLB 的 `done_source`（见 u2-l4）。

#### 4.4.3 源码精读

[RDMA 顶层 IG→cq→EG→reg（NV_NVDLA_CDP_rdma.v:L124-L221）](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_rdma.v#L124-L221)：与 MCIF/CVIF 的同构三级结构。注意 RDMA 自己例化了一个 `u_slcg` 与一个 `u_reg`（`NV_NVDLA_CDP_RDMA_reg`），说明读 DMA 的配置寄存器独立成页。

[csb_master 对 CDP 的地址译码（NV_NVDLA_csb_master.v:L902 与 L1617）](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L902)：`select_cdp = (core_byte_addr & addr_mask) == 32'h0000f000`（CDP 核心，基址 0xf000），`select_cdp_rdma = ... == 32'h0000e000`（CDP RDMA，基址 0xe000）。这证实 CDP 占两个 4KB 寄存器页：0xe000 配 RDMA、0xf000 配核心（含 DP/WDMA/LUT）。

[WDMA 与 done 中断（NV_NVDLA_cdp.v:L326-L356）](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_cdp.v#L326-L356)：WDMA 输出 `cdp2glb_done_intr_pd[1:0]`（2 位），WDMA 与 core 共用同一组核心寄存器（注释 `wdma share with core`），这与 RDMA 独立成页形成对照。

#### 4.4.4 代码实践

**目标**：在 csb_master 里确认 CDP 的两个寄存器页基址，并区分哪些寄存器属于 RDMA、哪些属于核心。

1. 打开 [csb_master.v:L902](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L902) 与 [csb_master.v:L1617](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L1617)，记录 CDP 核心=0xf000、CDP RDMA=0xe000。
2. 对照 [CDP_REG_dual.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_REG_dual.v)（核心，含 LRN_CFG/OP_ENABLE/DATA/DOUT_*）与 RDMA_reg（读 DMA 的 SRC_BASE_ADDR/WIDTH/HEIGHT 等），区分配置对象。

**预期结果**：你能写出“配 CDP 时，源地址/尺寸写 0xe00 页，LRN 参数/数据类型/LUT 写 0xf00 页”的结论。

#### 4.4.5 小练习与答案

**练习**：为什么 CDP 的 RDMA 要单独占一个寄存器页，而 WDMA 与核心共用？
**答**：因为 RDMA 的逐层源描述符（地址、尺寸、stride）需要在当前层运行期间就被 CPU 预装到下一组影偶寄存器（双缓冲无缝接跑，见 u2-l3），独立成页便于软件流水地配置读通路；WDMA 的目的参数与核心计算参数同属一层配置，故共用核心页。

---

## 5. 综合实践

把本讲三个最小模块（LRN 总体架构、intp/lut/mul/sum 计算单元、定点↔浮点转换）串起来，完成下面这个“读源码 + 画框图 + 写伪配置”的综合任务。

**任务**：为单个 LRN 层梳理一条完整的“配置→运行→完成”链路。

1. **画框图**：以 [NV_NVDLA_CDP_dp.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_dp.v) 为准，画出 `cvtin → syncfifo → {bufferin→sum} / {sync2mul} → LUT_ctrl → lut → intp → mul → cvtout` 的完整数据通路，标注每级的位宽（如 `cdp_rdma2dp_pd` 86 位、`sync2mul_pd` 72 位、`mul2ocvt_pd` 200 位）与 sqsum_bypass / mul_bypass 两个旁路开关的位置。
2. **标注 LRN 三段**：在框图上用三种颜色分别圈出“求 Σ（sum）”、“求 f(Σ)（lut+intp）”、“求 a·f（mul）”，并在 sum 旁注明 `reg2dp_normalz_len` 控制邻域宽度。
3. **写伪配置序列**（仅写寄存器语义，不要求真实偏移）：
   - 写 0xe00 页：SRC_BASE_ADDR / WIDTH / HEIGHT / CHANNEL / SRC_RAM_TYPE / INPUT_DATA_TYPE；
   - 写 0xf00 页：DATA_TYPE、DIN_OFFSET/SCALE/SHIFTER、DOUT_OFFSET/SCALE/SHIFTER、LRN_CFG.normalz_len、（可选）LUT 表内容、SQSUM_BYPASS=0、MUL_BYPASS=0、DST_*、INTERRUPT_PTR；
   - 写 0xf00 页 OP_ENABLE 启动；轮询 GLB done_status 中 CDP 对应位（见 u2-l4）。
4. **回答两个关键问题**：
   - 为什么 sum 在浮点域、而输入/输出是 int8？→ 因为平方和动态范围大，定点易溢出。
   - 若只想要一个逐点 sigmoid 激活（不要 LRN），应如何配置 CDP？→ 置 SQSUM_BYPASS=1 让原始输入直送 LUT，并用 LUT 写入 sigmoid 采样点。

**预期结果**：得到一张完整的 CDP 数据通路框图 + 一份分层寄存器配置清单。**待本地验证**：若有 C-model（见 u7-l3），用同一组配置跑 LRN，比对 RTL 仿真输出。

## 6. 本讲小结

- CDP 是 NVDLA 的**通道维**后处理引擎，核心算子是跨通道 **LRN**；它对每个空间位置、沿通道方向滑动一个宽度由 `normalz_len` 决定的窗口。
- LRN 在硬件里被拆成三段可实现的运算：**sum** 求邻域平方和 Σ、**lut+intp** 用查表+插值逼近 f(Σ)、**mul** 把 f(Σ) 乘回原始激活——sum 与 mul 是 LRN 的“左右手”。
- 计算数据通路 [NV_NVDLA_CDP_dp.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdp/NV_NVDLA_CDP_dp.v) 是一条 `cvtin → syncfifo → {sum} / {sync2mul} → lut → intp → mul → cvtout` 流水线；`syncfifo` 把同一份输入扇出，使 sum 算 Σ 与 mul 乘原始激活能并行进行。
- **定点↔浮点转换**由两头的 cvtin / cvtout 完成，统一采用 `(in+offset)×scale>>shifter` 的 RSC 模型；内部 sum/LUT/mul 用 fp17 浮点（`HLS_fp17_mul`），以应对平方和的大动态范围与非线性函数。
- CDP 占两个 4KB 寄存器页：**0xe000 配 RDMA**（独立）、**0xf000 配核心/WDMA/LUT**；完成中断 `cdp2glb_done_intr_pd[1:0]` 分两组上报 GLB。
- `SQSUM_BYPASS` 可把 CDP 退化为纯逐点 LUT 激活引擎；`MUL_BYPASS` 可跳过最后一步乘法——两个旁路开关体现了 CDP 设计的复用性。

## 7. 下一步学习建议

- **横向对比**：回到 u5-l1（SDP）与 u5-l2（PDP），把三者的 RDMA/WDMA 骨架与计算核心做一张对照表，巩固“后处理引擎通用结构”的直觉。
- **浮点原语**：本讲反复出现的 `HLS_fp17_mul` 属于 vlibs 浮点单元，建议接着学 **u6-l4（浮点运算单元 fp17/fp32）**，搞清 fp17 内部格式与 fp16/fp32 互转，理解 cvtin/cvtout 在格式边界的细节。
- **LUT 机制深挖**：本讲只点到“查表+插值”，SDP（u5-l1）的 LUT 讲了更完整的 LE/LO 两段表与段外斜率外推，二者机制同源，可对照阅读 `NV_NVDLA_CDP_DP_lut.v` 与 `NV_NVDLA_CDP_DP_intp.v`。
- **端到端**：学完本讲后可继续 u5-l4（Rubik），然后进入 **u8-l4（端到端编程一个网络层）**，把卷积+SDP+PDP+CDP 串成一个完整网络的后处理链。
