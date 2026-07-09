# SDP：单点数据处理器（BN/EW/LUT）

## 1. 本讲目标

CACC（卷积累加器）吐出的是「裸」的卷积结果，但一个真实网络层在卷积之后往往还要做：加偏置（bias）、做缩放、做 BatchNorm、做 ReLU/PReLU 激活、甚至做 sigmoid/tanh 这类非线性激活。这些运算有一个共同特征——**它们对特征图的每个激活点（activation）独立作用，点与点之间互不影响**。NVDLA 把这一整类「逐点（element-wise / single-point）后处理」交给一个专用引擎：**SDP（Single-point Data Processor，单点数据处理器）**。

学完本讲，你应当能够：

1. 说清 SDP 在整条数据通路中的位置：它紧接 CACC，是卷积输出后的第一道后处理。
2. 说出 SDP 的三个核心子操作 **BS / BN / EW** 分别做什么、为什么级联成三级流水。
3. 理解每一级「乘法（mul）+ 算术逻辑单元（alu）+ ReLU」的小流水结构，以及操作数可以来自「寄存器立即数」或「存储中的逐通道参数」两种来源。
4. 说明 EW 级独有的 **LUT 查表激活**（两段表 + 线性插值）如何实现任意非线性激活。
5. 画出 SDP 的 **读 DMA（mrdma/brdma/nrdma/erdma）+ 计算核 + 写 DMA（wdma）** 数据通路，并区分 flying（直连 CACC）与 non-flying（从存储读输入）两种工作模式。

## 2. 前置知识

本讲承接 **u3-l6（CACC）**，假定你已经了解：

- **卷积主流水线**：`CDMA → CBUF → CSC → CMAC → CACC`。CACC 把 CMAC 的部分和累加、舍入、饱和后，以 512 位（16 个通道、每通道 32 位）的宽带口 `cacc2sdp_pd[513:0]` 交付给下游。SDP 就是这个「下游」。
- **逐点运算（pointwise / element-wise）**：\[ y = f(x) \]，输出只依赖同一位置的输入，没有跨空间、跨通道的滑动窗口。这区别于后面 u5-l2 的 PDP（池化，有空间窗口）和 u5-l3 的 CDP（LRN，有跨通道窗口）。
- **影子（shadow）/ producer-consumer 配置**：见 u2-l3。SDP 的层参数也是双组轮换的，op_en 点火、done 完成的套路和 CDMA/CACC 一致。
- **存储接口 MCIF/CVIF**：见 u4。SDP 的各个 DMA 通过 `sdp2mcif_*` / `sdp2cvif_*` 把读写请求送到这两套接口。

几个本讲要用到的小概念：

- **激活点（activation）**：特征图里的一个数值。INT8 下是 1 字节，INT16 下是 2 字节，FP16 下是半精度浮点。
- **吞吐（throughput）**：一个模块每周期处理多少个激活点。SDP 的 BS/BN 级吞吐较高（本仓库 `SDP_BS_THROUGHPUT_16` / `SDP_BN_THROUGHPUT_16`，每周期 16 字节），EW 级吞吐较低（`SDP_EW_THROUGHPUT_4`，每周期 4 字节）——原因后面讲。
- **逐通道（per-channel）参数**：BN 的缩放因子、偏置这类参数通常每个输出通道一个值，需要从存储里按通道读取，而不是一个常数广播给所有点。

## 3. 本讲源码地图

SDP 的全部源码在 `vmod/nvdla/sdp/`，文件很多，本讲聚焦下面几张「地图」：

| 文件 | 作用 |
| --- | --- |
| `NV_NVDLA_sdp.v` | SDP 顶层。例化 4 大块：`u_rdma`（读 DMA 子系统）、`u_core`（计算核）、`u_wdma`（写 DMA）、`u_reg`（寄存器/影子配置）。是理解 SDP 全貌的入口。 |
| `NV_NVDLA_SDP_core.v` | 计算核。内含 `cmux`（输入选择）、`u_bs`/`u_bn`（两级 CORE_x）、`u_ew`（CORE_y）、`u_c`（输出转换 CORE_c）。三级 BS→BN→EW→CVT 流水就在这里拼成。 |
| `NV_NVDLA_SDP_CORE_y.v` | EW 级。把 mul-cvt、alu-cvt、core、idx、lut、inp、dpunpack 串成一条带 LUT 的逐点流水，是本讲最精彩的一张图。 |
| `NV_NVDLA_SDP_wdma.v` | 写 DMA。把处理结果拆成写命令、写数据，经 MCIF/CVIF 写回存储，并上报 done 中断。 |
| `NV_NVDLA_SDP_mrdma.v` / `NV_NVDLA_SDP_brdma.v` | 两个读 DMA 的代表。`mrdma` 在 non-flying 模式下读输入特征图；`brdma` 读 BS 级所需的逐通道参数。二者结构相同（ig→cq→eg）。 |
| `spec/defs/nv_full.spec` | 锁定 SDP 的能力开关与吞吐（`SDP_BS/BN/EW/LUT_ENABLE`、`SDP_*_THROUGHPUT_*`）。 |

> 提示：`vmod/nvdla/sdp/` 里有几十个 `NV_NVDLA_SDP_CORE_x.v`、`..._Y_lut.v`、`..._MRDMA_ig.v` 等文件，且不少是 eperl 展开后体积巨大（上百万行级）。本讲只读「连接关系清楚、逻辑可读」的顶层与封装文件，不钻进自动生成的巨型文件内部。

## 4. 核心概念与源码讲解

### 4.1 SDP 逐点后处理：它在整条通路里的位置与职责

#### 4.1.1 概念说明

一个典型的卷积网络层（以 INT8 推理为例）后处理长这样：

\[
y = \mathrm{LUT}\Big(\mathrm{ReLU}\big(\mathrm{BN}\big(\mathrm{BiasScale}(\,\text{conv\_out}\,)\big)\Big)\Big)
\]

其中：

- **BiasScale（BS）**：加偏置、做一次乘加缩放。常见用法就是把训练好的 bias 加到卷积结果上。
- **BatchNorm（BN）**：做 \[ y = \gamma \cdot x + \beta \] 形式的归一化（折叠成 scale + shift）。每个通道一组 \(\gamma,\beta\)。
- **Element-wise（EW）**：最灵活的一级，支持乘、加减（SUM）、取大（MAX）、取小（MIN）、相等比较（EQL），以及 LUT 激活。常用来做 PReLU、element-wise add（残差支路）、sigmoid/tanh 等。

这三级的**顺序是固定的**：BS → BN → EW → 输出转换（CVT）。每一级都可以被 `*_bypass` 单独旁路（bypass），需要哪级开哪级。一个纯 ReLU+缩放层，往往只启用 BS（用它的 mul 做缩放 + ReLU）就够，EW 的 LUT 完全不必开。

#### 4.1.2 核心流程

SDP 顶层把工作分成「取参数/取输入 → 计算 → 写结果」三件并行的事，由三个大例化承担：

```text
          (配置) csb2sdp_req ──► u_reg ──────────────────────────► 影子寄存器
                                                                    │ reg2dp_*
          ┌───────────────────────────────────────────────────────┘
          ▼
  flying?──► cmux ──► [BS=u_bs] ──► [BN=u_bn] ──► [EW=u_ew] ──► [CVT=u_c] ──┬──► u_wdma ──► 写回存储
   是: cacc2sdp        ▲ mul/alu        ▲ mul/alu       ▲ mul/alu/lut        └──► sdp2pdp（直送 PDP）
   否: u_rdma.mrdma    │                │               │
          ▲            │ brdma          │ nrdma         │ erdma
          │            │ (读 BS 参数)   │ (读 BN 参数)  │ (读 EW 参数)
         读输入特征    └────────────────┴───────────────┴── 这些是 u_rdma 里的 4 个子 DMA
```

要点：

1. **输入有两种来源**，由 `flying_mode` 选择（见 4.5）：flying 模式直接吃 CACC 的实时输出；non-flying 模式用 `mrdma` 从存储把输入特征图读进来。
2. **三级逐点计算** BS→BN→EW 共用同一条 512 位（16 通道×32 位）数据总线，前级输出直接喂后级输入；每级各有一个独立的 `*_bypass` 开关与对应的读 DMA 取参数。
3. **输出有两种去向**，由 `output_dst` 选择：写回存储（WDMA），或直接送给 PDP（flying 到下一级池化）。

#### 4.1.3 源码精读

SDP 顶层 `NV_NVDLA_sdp.v` 只做「连线」，例化四块。注意第 632–635 行的注释一语道破配置寄存器的归属：读 DMA 有自己独立的配置寄存器组（`u_rdma`），而写 DMA 与计算核共享同一组寄存器（`u_reg`）。

[顶层例化 u_rdma / u_wdma / u_core / u_reg:NV_NVDLA_sdp.v:363-754](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/sdp/NV_NVDLA_sdp.v#L363-L754) — 这四段实例化是 SDP 全部行为的总入口：`u_rdma`（读 DMA，行 363）把外部存储响应 `mcif2sdp_*_rd_rsp` 等转成 4 路参数流 `sdp_brdma2dp_*` / `sdp_nrdma2dp_*` / `sdp_erdma2dp_*` 与输入流 `sdp_mrdma2cmux_*`；`u_core`（行 503）做三级逐点运算，吃 CACC 实时数据 `cacc2sdp_*` 与各路参数，产出 `sdp_dp2wdma_*` 与 `sdp2pdp_*`；`u_wdma`（行 454）把结果写回并产生 `sdp2glb_done_intr_pd`；`u_reg`（行 636）持有影子配置并把 `reg2dp_*` 配置信号扇给 core 与 wdma。

顶层端口侧也能一眼看出 SDP 的对外关系：输入来自 CACC（`cacc2sdp_*`），输出走向 PDP（`sdp2pdp_*`）或存储（`sdp2mcif_wr_req_*` / `sdp2cvif_wr_req_*`），中断汇总到 GLB（`sdp2glb_done_intr_pd`）。

[SDP 对外端口:NV_NVDLA_sdp.v:107-184](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/sdp/NV_NVDLA_sdp.v#L107-L184) — 注意 `cacc2sdp_pd` 是 514 位（512 数据 + 2 掩码位），`sdp2pdp_pd` 是 256 位，二者宽度不同正反映了 SDP 内部做了「16 通道 32 位 → 输出精度」的重排。

#### 4.1.4 代码实践

**实践目标**：建立「SDP = 4 大例化块」的总体印象，验证本节画的框图。

**操作步骤**：

1. 打开 `vmod/nvdla/sdp/NV_NVDLA_sdp.v`，定位到行 363、454、503、636 四个实例化。
2. 对每个实例，在文件顶部的端口声明区（行 107–223）找到它对应的对外信号。例如 `u_core` 的输入 `cacc2sdp_*` 在行 107–109；`u_wdma` 的输出 `sdp2glb_done_intr_pd` 在行 170。
3. 回答：SDP 一共有几路独立的「读 DMA」请求通往 MCIF/CVIF？（提示：看 `sdp2mcif_rd_req_*`、`sdp_b2mcif_rd_req_*`、`sdp_e2mcif_rd_req_*`、`sdp_n2mcif_rd_req_*` 这几组。）

**需要观察的现象 / 预期结果**：你会看到 4 组带前缀 `_b` / `_e` / `_n` / 无前缀的读请求口，分别对应 brdma / erdma / nrdma / mrdma。这说明 SDP 最多同时发起 4 路并行读，分别取 BS 参数、EW 参数、BN 参数、输入特征。具体对应关系**待本地验证**（可在仿真波形里按 `valid` 拉高顺序确认）。

#### 4.1.5 小练习与答案

**练习 1**：SDP 的三级 BS/BN/EW 能否调换顺序？为什么？
**答案**：不能。顺序在 RTL 里硬连线为 BS→BN→EW（见 4.3 的级间 mux）。这是 NVDLA 的设计约定：通常 bias 在最前（最先抵消卷积的偏移），BN 次之，EW 最灵活放最后并独占 LUT。

**练习 2**：如果某一层只想做「加固定偏置 10」，应该启用哪一级、操作数从哪里来？
**答案**：启用 BS 级的 alu 子操作，algo 选 SUM，operand=10，`bs_alu_src=0`（立即数，来自寄存器），`bs_mul_bypass=1`（跳过乘法），BN/EW 整级 `*_bypass=1`。这样不需要任何读 DMA 取参数。

---

### 4.2 BS/BN/EW 三级子操作：mul + alu + relu 的小流水

#### 4.2.1 概念说明

BS、BN、EW 三级虽然职责不同，但**内部结构高度同构**：每一级都由三段小操作串成：

1. **mul（乘法缩放）**：\[ y = (x \times \text{operand}) \gg \text{shift} \] 支持带符号移位，可选 PReLU 模式（对负值用不同缩放）。
2. **alu（算术/逻辑）**：\[ y = x \;{\rm op}\; \text{operand} \]，op 由 2 位 `*_alu_algo` 选择：
   - `2'b00` = **MAX**
   - `2'b01` = **MIN**
   - `2'b10` = **SUM**（加法）
   - `2'b11` = **EQL**（相等比较，EW 专用，用于「输出是否相等」的状态统计）
3. **relu**：\[ y = \max(0, x) \]，可旁路；mul 段的 `prelu` 选项还可把它变成 PReLU。

> 这里的 `alu_algo` 编码来自自动生成的寄存器枚举，在 `cmod/include/arnvdla.uh` 中明确定义为 MAX=0/MIN=1/SUM=2/EQL=3，是理解 alu 行为的「权威字典」。

**操作数的两种来源**是 SDP 最关键的设计点之一，用 `*_src` 一位选择：

- `src = 0`：操作数是寄存器里的**立即数**（immediate），对所有通道、所有像素广播同一个值。适合「全局偏置」「固定缩放」。
- `src = 1`：操作数从**存储里的参数立方体（cube）**逐通道读取，经对应的读 DMA（brdma/nrdma/erdma）送进来。适合「逐通道 BN 参数」「逐通道 PReLU 斜率」。

也就是说，BS/BN/EW 既是「算术单元」，又是「参数调度器」——是否启用对应读 DMA、是否每个周期都吃一条参数，完全由 `src` 与 `*_bypass` 决定。

#### 4.2.2 核心流程

每一级内部一个激活点的处理流程（以 BS 为例，BN 同理）：

```text
   输入 x (来自 cmux 或上一级)
        │
        ▼
   mul:  x*m_op >> m_shift     ── m_op 来自 {寄存器立即数 | brdma 读来的逐通道参数}
        │                         (bs_mul_src 选择；bs_mul_bypass 可整段跳过)
        ▼
   alu:  prev <op> a_op         ── op ∈ {MAX,MIN,SUM}，a_op 来源同上
        │                         (bs_alu_src 选择；bs_alu_bypass 可整段跳过)
        ▼
   relu: max(0, v)              ── bs_relu_bypass 可跳过；配合 mul.prelu 做 PReLU
        │
        ▼
   输出 → 下一级 (BN)
```

EW 级在此基础上多了一条 **LUT 支路**（见 4.4），并且 mul/alu 各自还带一个 **cvt（convert）**子段，用于在进入运算前对参数做定点缩放（`scale/truncate/offset`），让外部下发的浮点参数能转成硬件所需的定点格式。

#### 4.2.3 源码精读

BS 与 BN 共用同一个模块 `NV_NVDLA_SDP_CORE_x`（只是例化名和参数不同），EW 用功能更强的 `NV_NVDLA_SDP_CORE_y`。它们的使能与级间旁路逻辑全在 `NV_NVDLA_SDP_core.v` 里。

首先看「三级是否启用」的判定——直接由各级的 `*_bypass` 反相得到：

[BS/BN/EW 使能逻辑:NV_NVDLA_SDP_core.v:577-589](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/sdp/NV_NVDLA_SDP_core.v#L577-L589) — `cfg_bs_en = (bs_bypass==0)`，BN/EW 同理。注意第 587 行额外定义了 `cfg_mode_eql`：当 EW 的 alu 选择 EQL（algo==3）且未旁路时，进入「相等比较」特殊模式，对应后面 CORE_c 里的 `cfg_mode_eql_rsc_z`，用来在输出阶段做逐点相等性统计（`dp2reg_status_unequal`）。

接着看 BS→BN 之间的级间多路选择，这是「旁路某级就把它短路掉」的实现：

[BS 到 BN 的级间 mux:NV_NVDLA_SDP_core.v:857-861](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/sdp/NV_NVDLA_SDP_core.v#L857-L861) — 若 `cfg_bs_en=0`（BS 被旁路），则 BN 的输入直接取 cmux 的原始数据，跳过 `u_bs`；同时反向把 ready 接到 `bn_data_out_prdy`，保证握手不被悬空。BN→EW（行 927–931）、EW→CVT（行 1008–1012）是同样的模式。

BS 级实例本身，注意它的时钟是**门控时钟** `nvdla_gated_bcore_clk`（空闲时关钟省电），参数从 `bs_alu_in_*` / `bs_mul_in_*` 进入（这俩来自 brdma）：

[BS 级实例 u_bs:NV_NVDLA_SDP_core.v:810-849](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/sdp/NV_NVDLA_SDP_core.v#L810-L849) — 关键端口：`cfg_alu_op_rsc_z`（alu 操作数）、`cfg_mul_op_rsc_z`（mul 操作数）、`cfg_alu_algo_rsc_z`（MAX/MIN/SUM）、`cfg_alu_src_rsc_z` / `cfg_mul_src_rsc_z`（操作数来源：立即数 vs DMA）、`cfg_mul_prelu_rsc_z`、`cfg_relu_bypass_rsc_z`。BN 级 `u_bn`（行 880–919）端口完全同构，只是前缀换成 `bn_`、时钟换成 `nvdla_gated_ncore_clk`。

那 brdma 读来的「逐通道参数」是怎么接到 BS 的？答案在 4.1 里看到的那几个 pipe：

[BS 参数输入 pipe:NV_NVDLA_SDP_core.v:471-502](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/sdp/NV_NVDLA_SDP_core.v#L471-L502) — `sdp_brdma2dp_mul_*` 经 `pipe_p1` 打拍后变成 `bs_mul_in_*`（256 位数据 + 1 位 layer_end），`sdp_brdma2dp_alu_*` 经 `pipe_p2` 变成 `bs_alu_in_*`。也就是说 brdma 一次能同时给 BS 喂「乘法参数」和「alu 参数」两路。BN（行 504–537，接 nrdma）、EW（行 539–572，接 erdma）是同样的双路参数结构。

最后看「参数流是否真正启用」的门控——它把 `*_src`、`*_bypass` 与 `cfg_*_en` 合起来，决定要不要去读 DMA 拿参数：

[参数流使能门控:NV_NVDLA_SDP_core.v:669-730](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/sdp/NV_NVDLA_SDP_core.v#L669-L730) — 以 `bs_alu_in_en` 为例（行 671–681）：只有「BS 启用 && alu 未旁路 && alu_src==1（参数来自存储）」三者同时成立，才会拉起 `bs_alu_in_en`，从而 `bs_alu_in_vld/prdy` 才有效，brdma 才会被真正消费。当一层结束（`*_layer_end`）或层完成（`dp2reg_done`）时清零。这套逻辑保证：用立即数时完全不触发 DMA，省带宽。

#### 4.2.4 代码实践

**实践目标**：把「一级 = mul + alu + relu + 可选 DMA 参数」的结构在源码里坐实。

**操作步骤**：

1. 在 `NV_NVDLA_SDP_REG_dual.v` 行 30–72 观察影子寄存器暴露的全部字段，按 `bs_*` / `bn_*` / `ew_*` 三组分类，列出每组里 mul 段、alu 段、relu 段各有哪些字段。
2. 在 `NV_NVDLA_SDP_core.v` 行 591–653 的配置装载块里，确认这些字段在 `op_en_load`（一层开始时）一次性打入 `cfg_*`，运行期间不再变化（保证一层内行为稳定）。
3. 对照行 669–730，验证：「只用立即数」（所有 `*_src=0`）时，所有 `*_in_en` 都为 0，于是 brdma/nrdma/erdma 不会被消费。

**需要观察的现象 / 预期结果**：你会看到 `bs_alu_in_en = cfg_bs_en && (!reg2dp_bs_alu_bypass) && (reg2dp_bs_alu_src==1)`。把 `src` 改成 0，该使能即清零——这就是「立即数模式不读存储」的硬件依据。

#### 4.2.5 小练习与答案

**练习 1**：BS 的 mul 段和 alu 段都能各自独立旁路吗？
**答案**：能。`bs_mul_bypass` 与 `bs_alu_bypass` 是两个独立位，可任意组合。最「轻」的 BS 配置是 mul/alu 都旁路、只留 relu。

**练习 2**：为什么 alu 只在 EW 级支持 EQL，而 BS/BN 不支持？
**答案**：EQL 用于逐点「输出是否等于某个参考值」的统计（结果汇到 `dp2reg_status_unequal`），它需要配合输出转换级（CORE_c）的 `cfg_mode_eql`（行 587、1042）才有意义，所以只挂在最末的 EW 级。BS/BN 是中间级，没有这种统计语义。

---

### 4.3 输入选择与级间流水：cmux、flying 模式与 CVT 输出

#### 4.3.1 概念说明

SDP 的输入不总是来自 CACC。两种工作模式：

- **flying 模式（`flying_mode=1`）**：SDP 直接消费 CACC 实时输出的卷积结果，一边算一边把结果送出，全程不落地存储。这是最常见的「卷积 → 立刻后处理」链路，省一次读写。
- **non-flying 模式（`flying_mode=0`）**：SDP 用 `mrdma` 从存储把一个**已经写好的**特征图读进来做后处理（例如对某个中间层单独做激活）。此时 CACC 不参与。

输入选好后，经三级 BS/BN/EW，再过一道 **CVT（输出转换，CORE_c）**：它把内部计算精度（proc_precision）按 \[ y = (x \times \text{scale} + \text{offset}) \gg \text{shift} \] 转换并饱和到目标输出精度（out_precision，如 INT8/INT16/FP16），并产生最终的 `sdp2pdp` / `sdp_dp2wdma` 流。

输出也有两个去向，由 `output_dst` 选：

- `output_dst=0`：写回存储（WDMA）。
- `output_dst=1`：直接 flying 送给 PDP（下一级池化）。

#### 4.3.2 核心流程

```text
   cacc2sdp_pd ──┐
                 ├─► cmux(cfg_flying_mode_on) ──► 512b 数据流
   mrdma2cmux ──┘            │
                             ▼
   [BS]──►[BN]──►[EW]──►[CVT: scale/shift/offset + 饱和到 out_precision]
                                    │
                    ┌───────────────┴───────────────┐
                    ▼                               ▼
        output_dst=1: sdp2pdp            output_dst=0: sdp_dp2wdma ──► u_wdma
```

#### 4.3.3 源码精读

cmux 是一个很小的模块，核心就是「二选一 + 一个使能」：

[cmux 的 flying 选择:NV_NVDLA_SDP_cmux.v:215-223](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/sdp/NV_NVDLA_SDP_cmux.v#L215-L223) — `cmux_pd = cfg_flying_mode_on ? cacc_pd : sdp_mrdma2cmux_pd`；ready 信号也按模式回给 CACC 或 mrdma。`cfg_flying_mode_on` 在行 186 由 `reg2dp_flying_mode==1` 载入。注意行 217–218：非 flying 模式下 `cacc_rdy` 恒为 0，CACC 不会往 SDP 送数；反之亦然——保证两路互不干扰。

CVT 输出与「WDMA / PDP」二选一在 core.v 末尾：

[输出到 WDMA 或 PDP 的选择:NV_NVDLA_SDP_core.v:1051-1059](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/sdp/NV_NVDLA_SDP_core.v#L1051-L1059) — `cfg_mode_pdp = (output_dst==1)`；当走 PDP 时 `core2wdma_pd` 强制为 0，`core2pdp_pd` 才有数据；同时 valid 交叉受另一路 ready 控制（行 1055–1056），避免一边反压导致另一边丢数。CVT 模块本身 `u_c`（行 1031–1046）拿到 `cfg_scale/cfg_offset/cfg_shift` 与 proc/out 精度，做最后的定点换算。

#### 4.3.4 代码实践

**实践目标**：理解 flying 与 non-flying 两种模式下，数据从哪里进、SDP 反压谁。

**操作步骤**：

1. 读 `NV_NVDLA_SDP_cmux.v` 行 180–225，确认 `cfg_flying_mode_on` 的载入条件（是否随 op_en_load）。
2. 在 `NV_NVDLA_sdp.v` 顶层端口里找到 `cacc2sdp_ready` 的最终驱动来源（它由 cmux 经 core 产出）。
3. 思考：non-flying 模式下，CACC 与 SDP 之间是否还有数据交互？

**预期结果**：non-flying 模式下 `cacc2sdp_ready` 被 cmux 拉低，CACC 的输出会被 CACC 自己的反压机制（见 u3-l6 的 delivery buffer）接住；SDP 这一层只跟 mrdma 打交道。具体 CACC 行为**待对照 u3-l6 复习**。

#### 4.3.5 小练习与答案

**练习**：flying 模式 + `output_dst=1` 时，数据全程有没有落存储？
**答案**：没有。输入来自 CACC 实时流，输出直送 PDP，SDP 内部也不落地（只有各级寄存器/打拍）。这是 NVDLA 最省带宽的「卷积→后处理→池化」直连链路。

---

### 4.4 EW 级与 LUT 激活：两段表 + 线性插值

#### 4.4.1 概念说明

BS/BN 只能做「乘 + 加 + ReLU」这类线性运算。但 sigmoid、tanh、GELU 这类**非线性激活**无法用一次乘加表达。SDP 的解法是：在 EW 级挂一张 **LUT（Look-Up Table，查找表）**，用分段线性近似任意函数。

NVDLA 的 LUT 设计有几个聪明点：

1. **两张子表**：`LE`（Linear/Exponential 段，覆盖较大输入范围、用指数/线性）和 `LO`（Logarithmic 段，覆盖另一段）。`lut_le_start/end`、`lut_lo_start/end` 定义各自的定义域，`*_index_select` / `*_index_offset` 控制如何把输入映射到表项索引。
2. **段外处理（oflow/uflow）**：输入超出表范围时，用 `*_slope_oflow_scale/shift`、`*_slope_uflow_scale/shift` 给一段线性外推斜率，并按 `*_priority` 决定 oflow 与 uflow 谁优先。
3. **线性插值（interpolation, inp）**：查表得到相邻两个表项后，`inp` 模块做线性插值，让分段折线看起来更平滑，降低量化误差。
4. **混合优先级（hybrid）**：`lut_hybrid_priority` 决定当输入同时落在 LE 与 LO 重叠区时，用哪张表的结果。
5. **软件可在线写表**：`lut_int_*` 一组接口允许 CPU 在运行时把表内容写进 LUT RAM（`lut_int_data_wr`、`lut_int_addr`、`lut_int_table_id`），所以 LUT 内容不是固化的。

LUT 可整体旁路（`ew_lut_bypass=1`），此时 EW 只做线性 mul/alu，跳过 idx/lut/inp 三段。

#### 4.4.2 核心流程

EW 级（`NV_NVDLA_SDP_CORE_y`）的内部流水：

```text
 ew_data_in (512b=16通道) ──► dppack ──► core( mul→alu→relu ) ──┐
                                                                 │
   erdma 参数 ──► mul_cvt / alu_cvt ──► core 的操作数 ───────────┘
                                                                 │
                                            ┌─ ew_lut_bypass=1 ──► dpunpack ──► ew_data_out
                                            │
                                            └─ ew_lut_bypass=0 ──► idx(算表索引) ──► lut(查表) ──► inp(插值) ──► dpunpack ──► ew_data_out
```

注意 `dppack`/`dpunpack`：它们把 16 通道的宽带（512 位）「打包」成 EW 实际处理的较窄宽度再「拆包」回去——这正是 `SDP_EW_THROUGHPUT_4`（每周期 4 字节）远低于 BS/BN 的 16 的硬件体现：EW 单周期处理的通道数少，所以要靠 pack/unpack 做宽度适配与节拍缓冲。

#### 4.4.3 源码精读

EW 级在 `NV_NVDLA_SDP_CORE_y.v` 里是一个清晰的串行流水。先看它如何按 `ew_lut_bypass` 在「直通」与「走 LUT」两条路之间切换：

[LUT 旁路多路选择:NV_NVDLA_SDP_CORE_y.v:445-447](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/sdp/NV_NVDLA_SDP_CORE_y.v#L445-L447) 与 [LUT 输出回接:NV_NVDLA_SDP_CORE_y.v:527-529](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/sdp/NV_NVDLA_SDP_CORE_y.v#L527-L529) — 第 445 行：旁路时 `idx_in_pvld=0`（不去算索引），core 输出直接交给 dpunpack；第 527 行：旁路时 `unpack_in_pd = core_out_pd`，否则取 `inp_out_pd`（插值结果）。两段合起来就是图里的那个二选一开关。

再看 LUT 三段的实例：

[LUT 三段 idx/lut/inp:NV_NVDLA_SDP_CORE_y.v:449-525](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/sdp/NV_NVDLA_SDP_CORE_y.v#L449-L525) — `u_idx`（行 449）拿 core 输出与 `cfg_lut_le/lo_start`、`*_index_select`、`*_index_offset` 计算查表索引，并按 `oflow/uflow/hybrid_priority` 处理越界；`u_lut`（行 473）是真正的查表 RAM，同时承载软件在线写表接口 `reg2dp_lut_int_*`（行 482–486）和命中统计 `dp2reg_lut_le_hit/lo_hit/oflow/uflow`；`u_inp`（行 515）做线性插值，输出最终激活值。注意 `u_lut` 的数据口很宽（`lut2inp_pd[739:0]`、`idx2lut_pd[323:0]`），因为一次要返回多个表项供插值。

mul/alu 各自前面的 cvt 子段（把外部定点/浮点参数转成运算格式）：

[EW 的 mul-cvt 与 alu-cvt:NV_NVDLA_SDP_CORE_y.v:335-393](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/sdp/NV_NVDLA_SDP_CORE_y.v#L335-L393) — `u_alu_cvt`（行 347）和 `u_mul_cvt`（行 378）结构相同，都用 `cfg_*_cvt_scale/offset/truncate` 对 erdma 读来的参数做 \[ p' = (p \times \text{scale} + \text{offset}) \gg \text{truncate} \]，再喂给 core 的 alu/mul 端口。这是 EW 独有的（BS/BN 没有这层 cvt，因为它们的参数语义更简单）。

EW 配置在一层开始时（`op_en_load`）一次性载入 `cfg_*`，运行期间不变：

[EW 配置载入:NV_NVDLA_SDP_CORE_y.v:239-327](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/sdp/NV_NVDLA_SDP_CORE_y.v#L239-L327) — 注意第 299 行 `cfg_ew_lut_bypass`、第 257 行 `cfg_ew_mul_prelu` 都在这里载入，与前面 core.v 的 `cfg_ew_en` 配合决定 EW 行为。

#### 4.4.4 代码实践

**实践目标**：搞清「一次 sigmoid 激活」在 SDP 里要走哪些子模块。

**操作步骤**：

1. 假设要做 sigmoid。sigmoid 是非线性，只能靠 LUT。所以配置上：`ew_bypass=0`、`ew_lut_bypass=0`、mul/alu 按 `ew_mul_bypass=1`/`ew_alu_bypass=1` 旁路（或用它们做必要的定点调整），BS/BN 视情况旁路。
2. 在 `NV_NVDLA_SDP_CORE_y.v` 里顺着行 409（core）→ 445（判断）→ 449（idx）→ 473（lut）→ 515（inp）→ 533（dpunpack）走一遍数据通路。
3. 查 spec：确认本仓库 LUT 是启用的（`SDP_LUT_ENABLE`）。

**需要观察的现象 / 预期结果**：sigmoid 这种 S 型曲线，典型做法是把定义域分成 LE（中间陡峭段，指数/线性细分）和 LO（两侧饱和段）两张表，再用 inp 插值。具体表项内容需由软件按本层激活范围离线计算后通过 `lut_int_*` 写入——**待本地验证**写表时序。

#### 4.4.5 小练习与答案

**练习 1**：如果激活函数就是普通 ReLU，需要开 LUT 吗？
**答案**：不需要。ReLU 用 EW（或 BS/BN）的 relu 子段（`*_relu_bypass=0`）即可，把 `ew_lut_bypass=1`。LUT 留给非线性函数。

**练习 2**：LUT 的表内容是硬件固化的吗？
**答案**：不是。表内容通过 `reg2dp_lut_int_*`（含写使能 `lut_int_data_wr`、地址 `lut_int_addr`、表号 `lut_int_table_id`）由软件在线写入 `u_lut` 内的 RAM（行 482–486），所以每层可以用不同的激活曲线。

---

### 4.5 RDMA/WDMA 数据通路：四个读 DMA + 一个写 DMA

#### 4.5.1 概念说明

SDP 之所以需要**四个读 DMA**，是因为它要取四种不同的东西：

| 读 DMA | 取什么 | 送给谁 |
| --- | --- | --- |
| `mrdma`（M = feature Map） | 输入特征图（仅 non-flying 模式） | cmux → BS |
| `brdma`（B = BS 参数） | BS 级逐通道参数（乘法/alu 操作数） | BS 的 mul/alu 端 |
| `nrdma`（N = BN 参数） | BN 级逐通道参数 | BN 的 mul/alu 端 |
| `erdma`（E = EW 参数） | EW 级逐通道参数 | EW 的 mul/alu 端 |

每个读 DMA 的内部结构与 u4 讲过的 MCIF/CVIF「IG→CQ→EG」同构：

- **ig（ingress，入口）**：按 `src_base_addr/line_stride/surface_stride/width/height/channel/batch` 游走地址，向 MCIF/CVIF 发读请求。
- **cq（context queue，上下文队列）**：用一个小 FIFO 记录「已发请求、尚未返回」的上下文，把 ig 与 eg 解耦（处理存储器的乱序返回）。
- **eg（egress，出口）**：接收存储返回的 512 位数据，按上下文重排成参数流，送给对应的计算级；同时产生 `dp2reg_done`。

写侧只有一个 `wdma`：把 CVT 的结果（`sdp_dp2wdma_*`）拆成「写命令 + 写数据」，按目的地址布局（`dst_base_addr/*_stride`、`batch_number`）写回 MCIF/CVIF，并在全部写返回后拉 `sdp2glb_done_intr_pd` 上报 GLB。

> 重要：这四个读 DMA 与一个写 DMA 都是**独立可旁路、独立时钟门控**的。一层里如果只用立即数（所有 `*_src=0`），mrdma/brdma/nrdma/erdma 全程不发请求；如果 flying 模式且输出直送 PDP，wdma 也不写存储。SDP 的实际带宽消耗完全由配置决定。

#### 4.5.2 核心流程

```text
   ┌─────── 四个读 DMA（结构相同: ig→cq→eg） ───────┐
   │ mrdma: 取输入特征   ──► cmux                    │
   │ brdma: 取 BS 参数    ──► BS.mul/BS.alu           │
   │ nrdma: 取 BN 参数    ──► BN.mul/BN.alu           │
   │ erdma: 取 EW 参数    ──► EW.mul_cvt/EW.alu_cvt   │
   └──────────────────────────────────────────────────┘
        各 ig 发 sdp2mcif_rd_req_* / sdp2cvif_rd_req_*  ──► MCIF / CVIF ──► 外部存储
        各 eg 收 mcif2sdp_*_rd_rsp_* / cvif2sdp_*_rd_rsp_*

   计算 core 产出 sdp_dp2wdma_* ──► u_wdma ──► sdp2mcif_wr_req_* / sdp2cvif_wr_req_* ──► 存储写
   wdma 完成写返回 ──► sdp2glb_done_intr_pd[1:0] ──► GLB
```

#### 4.5.3 源码精读

以 `mrdma` 为典型看「ig→cq→eg + op_load 状态机」的封装：

[mrdma 的 ig/cq/eg 三段:NV_NVDLA_SDP_mrdma.v:125-224](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/sdp/NV_NVDLA_SDP_mrdma.v#L125-L224) — 行 125–137 是层处理状态机：`op_load = op_en & ~layer_process`，点火后 `layer_process` 置 1，直到 `eg_done`（行 137 的 `dp2reg_done`）才清零，保证一层只点火一次。行 152 的 `u_ig` 在门控时钟 `nvdla_gated_clk` 下发读请求（`sdp2mcif/cvif_rd_req_*`）；行 182 的 `u_cq` 是上下文 FIFO（`ig2cq_*` ↔ `cq2eg_*`）；行 196 的 `u_eg` 收存储响应、产出 `sdp_mrdma2cmux_*` 并给 `eg_done`。`brdma`（`NV_NVDLA_SDP_brdma.v`）结构完全一致，只是参数口换成 `sdp_brdma2dp_alu/mul_*`，宽度略有不同（cq 上下文 16 位 vs mrdma 13 位）。

[brdma 与 mrdma 的同构对照:NV_NVDLA_SDP_brdma.v:132-236](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/sdp/NV_NVDLA_SDP_brdma.v#L132-L236) — 注意行 160–189 的 `u_ig` 多了 `reg2dp_brdma_data_mode/size/use` 几个字段，用于控制 BS 参数的打包方式（参数可以是「每通道一个」或更紧凑的格式）；其余 ig/cq/eg 三段与 mrdma 如出一辙。

写侧 `wdma` 采用「命令 + 数据」分离的设计：

[wdma 的 gate/cmd/dat/dmaif 四段:NV_NVDLA_SDP_wdma.v:126-245](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/sdp/NV_NVDLA_SDP_wdma.v#L126-L245) — 行 129–140 是处理状态机（`op_load`/`processing`/`dp2reg_done`）。`u_cmd`（行 152）根据目的几何（`dst_base_addr/*_stride`、`width/height/channel/batch_number`、`out_precision`）生成写地址命令流（`cmd2dat_spt_*` / `cmd2dat_dma_*`）；`u_dat`（行 181）把 core 送来的 `sdp_dp2wdma_pd` 与命令对齐，产出 `dma_wr_req_*`，并统计 NaN、不等、stall 等状态；`u_dmaif`（行 218）把写请求经 MCIF/CVIF 发出（`sdp2mcif/cvif_wr_req_*`），收写完成（`mcif/cvif2sdp_wr_rsp_complete`），最终产 `sdp2glb_done_intr_pd[1:0]`（行 229）——这两位交替点亮两层影子组的中断，与 u3-l6 CACC 的做法一致。`u_gate`（行 142）提供 wdma 自己的门控时钟。

`u_dmaif` 还有一组调试计数：`dp2reg_status_nan_output_num`、`dp2reg_status_unequal`、`dp2reg_wdma_stall`，分别统计输出 NaN 数量、EQL 不等命中数、WDMA 被反压周期数，供性能与正确性分析。

#### 4.5.4 代码实践

**实践目标**：验证「四个读 DMA + 一个写 DMA」与外部存储接口的连接关系。

**操作步骤**：

1. 在 `NV_NVDLA_sdp.v` 顶层端口区（行 29–51、79–96）数一数 SDP 拥有多少组通往 MCIF/CVIF 的读请求口与多少组写请求口。
2. 在 `NV_NVDLA_SDP_rdma.v`（u_rdma 的封装）里确认它把 mrdma/brdma/nrdma/erdma 四个子 DMA 的请求汇拢/分发到这些对外口上。
3. 对照 `NV_NVDLA_SDP_wdma.v` 行 218–245 的 `u_dmaif`，确认写请求与写完成的中断上报路径。

**需要观察的现象 / 预期结果**：顶层应有 4 组读请求口（`sdp2mcif_rd_req_*`、`sdp_b2mcif_*`、`sdp_e2mcif_*`、`sdp_n2mcif_*`，CVIF 同）与 1 组写请求口（`sdp2mcif_wr_req_*`，CVIF 同）。读多写少正反映了 SDP「要取很多种参数、只产一份结果」的特性。具体端口计数**待本地确认**。

#### 4.5.5 小练习与答案

**练习 1**：为什么 SDP 的读 DMA 有四个，而写 DMA 只有一个？
**答案**：读侧要取「输入特征 + BS/BN/EW 三套参数」共四类各不相同的立方体，来源地址、布局、用途都不同，所以各配一个独立 DMA；写侧只产出一个结果立方体，一个 wdma 足够。

**练习 2**：`sdp2glb_done_intr_pd` 为什么是 2 位？
**答案**：对应影子（producer/consumer）两组配置。一层处理完成时，按当前 consumer 组点亮对应那一位中断，GLB（见 u2-l4）据此在 `done_source` 里置位，配合下一组的无缝切换，实现流水不停顿。

---

## 5. 综合实践：为一个 ReLU+缩放层规划 SDP 配置

**任务背景**：假设有一层卷积，CACC 输出 INT16 结果，要求对该输出做 \[ y = \mathrm{ReLU}(x \times 0.125) \]，并把结果以 INT8 写回存储。请规划 SDP 的配置，并说清数据通路。

**分析与配置规划**：

1. **整体模式**：卷积后立刻后处理，用 **flying 模式**（`flying_mode=1`），输入来自 CACC，不走 mrdma。
2. **精度**：`proc_precision = INT16`（内部按 INT16 算），`out_precision = INT8`（写回用 INT8）。
3. **启用哪级**：只需一次乘法缩放 + ReLU，**启用 BS 级**即可，BN/EW 整级旁路。
   - BS 的 **mul**：operand = 0.125 的定点表示。0.125 = 1/8，可表示为「乘 1、右移 3」，所以 `bs_mul_operand=1`、`bs_mul_shift_value=3`、`bs_mul_src=0`（立即数，所有通道一样）、`bs_mul_bypass=0`。
   - BS 的 **alu**：不需要，`bs_alu_bypass=1`。
   - BS 的 **relu**：`bs_relu_bypass=0`（开启 ReLU）。
   - BN、EW：`bn_bypass=1`、`ew_bypass=1`（整级旁路；EW 的 LUT 自然也不需要）。
4. **LUT 是否必需**：**不必需**。ReLU 是线性分段函数，用 relu 子段即可，`ew_lut_bypass` 无关（因为 EW 已整级旁路）。
5. **输出转换（CVT）**：INT16 → INT8 需要再缩放/截断。若 0.125 已在 BS 完成，CVT 只做精度收窄：可设 `cvt_scale=1`、`cvt_shift=0`（或按需做饱和截位），由 CORE_c 的饱和逻辑保证 INT8 范围。
6. **输出目的地**：写回存储，`output_dst=0`。

**数据通路**（请你在源码里逐段确认）：

```text
CACC ──cacc2sdp(514b)──► cmux(flying) ──► BS: mul(×1>>3) → relu ──► BN(bypass) ──► EW(bypass)
   ──► CVT(INT16→INT8 饱和) ──► sdp_dp2wdma ──► u_wdma ──► sdp2mcif_wr_req ──► MCIF ──► 存储写
   wdma 完成 ──► sdp2glb_done_intr_pd ──► GLB done_source[sdp]
```

**验证步骤**（源码阅读型，无需真实综合）：

1. 在 `NV_NVDLA_SDP_core.v:577-589` 确认 `cfg_bs_en=1`、`cfg_bn_en=cfg_ew_en=0` 后，行 857、927 的级间 mux 会把 BN、EW 短路。
2. 在 `NV_NVDLA_SDP_core.v:810-849`（u_bs）确认 mul 取 `cfg_bs_mul_operand/shift`、relu 由 `cfg_bs_relu_bypass` 控制。
3. 在 `NV_NVDLA_SDP_core.v:669-699` 确认：因为 `bs_mul_src=0` 且 `bs_alu_bypass=1`，`bs_mul_in_en`/`bs_alu_in_en` 均为 0，所以 **brdma 全程不发请求**——这一层完全不读参数存储。
4. 在 `NV_NVDLA_SDP_wdma.v:218-245` 确认结果经 `u_dmaif` 写回，并在完成时上报 `sdp2glb_done_intr_pd`。

**预期结果**：这是一个「最小读带宽」的 SDP 配置——0 个读 DMA 活跃（flying 输入 + 全立即数参数），仅 wdma 写一路。若把缩放因子改成「每通道不同」（真实的 per-channel scale），则需 `bs_mul_src=1`，brdma 会活跃起来去存储取每通道的 `mul_operand`。两种配置的带宽差异正是 `src` 位的设计意义。

> 说明：以上寄存器字段名取自 `NV_NVDLA_SDP_REG_dual.v` 与 `arnvdla.uh`；具体的定点编码（0.125 到底用哪组 scale/shift）依赖编译器/驱动层的量化策略，**待本地验证**。

## 6. 本讲小结

- **SDP 是逐点后处理引擎**，紧接 CACC，对卷积输出做「每个激活点独立」的元素级运算。
- **三级固定流水 BS → BN → EW**，每级 = mul + alu + relu，每级可独立 bypass；EW 额外支持 cvt 与 LUT。
- **操作数两种来源**：`src=0` 寄存器立即数（广播，不读存储）；`src=1` 经 brdma/nrdma/erdma 从存储取逐通道参数。
- **alu 算法集** MAX/MIN/SUM/EQL，其中 EQL 仅 EW 级支持，配合输出级做逐点相等统计。
- **EW 的 LUT** 用 LE/LO 两段表 + 段外斜率外推 + 线性插值（inp）逼近任意非线性激活，表内容软件可在线写入；可整体旁路。
- **数据通路 = 四个读 DMA（mrdma/brdma/nrdma/erdma，各 ig→cq→eg）+ 计算核 + 一个写 DMA（wdma）**；输入由 flying 模式（CACC）或 mrdma 二选一，输出由 output_dst 决定写存储或直送 PDP。
- **吞吐差异**：BS/BN 每周期 16 字节，EW 每周期 4 字节（`SDP_*_THROUGHPUT_*`），EW 用 dppack/dpunpack 做宽度适配。

## 7. 下一步学习建议

- **u5-l2（PDP）**：SDP 处理「逐点」，PDP 处理「空间窗口池化」。建议接着读 `vmod/nvdla/pdp/`，对比两者 cal1d/cal2d 与 SDP 逐点流的差异。
- **u5-l3（CDP）**：CDP 做跨通道的 LRN，结构上也有 lut/intp，可与本讲 4.4 的 LUT 机制对照阅读。
- **u2-l3 / u2-l4**：若对影子配置（producer/consumer）与 done 中断聚合（`sdp2glb_done_intr_pd → GLB done_source`）还不够熟，回头复习这两讲。
- **源码延伸**：想深入 LUT 实现，可读 `NV_NVDLA_SDP_CORE_Y_idx.v` / `..._Y_lut.v` / `..._Y_inp.v`（注意它们是 eperl 展开的大文件，建议先读 `cmod/sdp` 里对应的 C 参考模型理解语义，再回来看 RTL）。
