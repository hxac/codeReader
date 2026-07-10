# PDP：平面数据处理器（池化）

## 1. 本讲目标

PDP（Planar Data Processor，平面数据处理器）是 NVDLA 后处理流水线中的「池化引擎」。学完本讲，你应当能够：

- 说清 PDP 在做什么：在特征图的每个通道「平面」内做空间池化（max / average / min），不跨通道、不跨 batch。
- 解释 PDP 为什么把二维池化拆成「水平 1D 池化（cal1d）＋垂直 2D 池化（cal2d）」两级来做，并理解这种可分离（separable）分解对 max/min/average 的数学正确性。
- 读懂 `pooling_MAX / pooling_MIN / pooling_SUM` 三个函数如何用同一套硬件复用 int8 / int16 / fp16 三种精度，以及 average 池化为何用「倒数乘」而非除法。
- 描述 PDP_rdma 如何把待池化的输入特征图读进来、PDP_wdma 如何把结果写回去，以及完成时如何通过 `pdp2glb_done_intr_pd` 向 GLB 上报中断。

本讲承接 u3-l6（CACC 累加器）与 u5-l1（SDP），因为 CACC 的输出 → SDP → PDP 是一条直通的「卷积结果 → 逐点后处理 → 池化」数据链。

## 2. 前置知识

在进入源码前，先用三段通俗的话把概念铺平。

**什么是池化（pooling）。** 卷积得到一张特征图（宽 W × 高 H × 通道 C）。池化用一个滑动窗口（比如 2×2）扫过每个通道平面，每个窗口只保留一个值：取最大值叫 max pooling、取平均叫 average pooling、取最小叫 min pooling。它的作用是「下采样」——缩小宽高、保留主要信息、减少后续计算量。注意池化**只在同一个通道内做空间聚合，不把不同通道混在一起**，所以叫「平面」数据处理器。

**二维池化为什么可以拆成两步。** 一个二维窗口的最大值 =「先对每一行取最大，再对这些行的最大值取最大」。min 同理；average 则是「先对每行求和，再对各行求和，最后除以总个数」。换句话说：

\[
\text{pool}_{2D}(W) = \text{pool}_{垂直}\big(\text{pool}_{水平}(\text{每一行})\big)
\]

对 max / min 严格成立，对 average 也是「先和后除」。NVDLA 正是利用这个性质，把一个二维池化拆成**水平方向（cal1d）**和**垂直方向（cal2d）**两段流水。这样做的好处是：行方向先压缩一遍，列方向只需缓存少数几行（行缓冲 line buffer）即可，硬件面积远小于「一次把整个二维窗口的像素都搬进来」。

**精度与数据格式。** PDP 支持 int8、int16、fp16 三种输入/输出精度（由 `reg2dp_input_data` 选择：`2'h0`=int8、`2'h1`=int16、`2'h2`=fp16）。一个数据 beat 在内部被打包成 4 个「通道槽」，int8 模式下每个槽还能塞两个 8-bit 值，所以一个 beat 能并行处理 8 个 int8 像素或 4 个 int16/fp16 像素。

> 关键术语回顾（来自前置讲义）：**cube** = 一段三维数据块（宽×高×通道）；**RDMA/WDMA** = 读/写 DMA 子模块；**影偶（shadow）配置** = producer/consumer 两组寄存器轮换，引擎运行中可预装下一层参数；**done_intr** = 引擎完成时上报 GLB 的中断脉冲；**flying mode** = 数据「在飞」，即不落存储、直接从上一级（SDP）接到 PDP。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `vmod/nvdla/pdp/NV_NVDLA_pdp.v` | PDP 顶层，例化 rdma / core / wdma / reg / nan / slcg 六大块，是端口与连线的总装车间。 |
| `vmod/nvdla/pdp/NV_NVDLA_PDP_core.v` | 计算核顶层，例化 preproc → cal1d → cal2d 三级池化流水。 |
| `vmod/nvdla/pdp/NV_NVDLA_PDP_CORE_cal1d.v` | 水平 1D 池化：在一行内按 kernel_width/stride 取 max/min/sum，并负责选输入源（RDMA 或 flying SDP）。 |
| `vmod/nvdla/pdp/NV_NVDLA_PDP_CORE_unit1d.v` | cal1d/cal2d 共用的「1D 池化单元」，定义 `pooling_MAX/MIN/SUM` 与 `pooling_fun`。 |
| `vmod/nvdla/pdp/NV_NVDLA_PDP_CORE_cal2d.v` | 垂直 2D 池化：用行缓冲缓存若干行 1D 结果，按 kernel_height 做垂直聚合，对 average 做倒数乘。 |
| `vmod/nvdla/pdp/NV_NVDLA_PDP_CORE_preproc.v` | 预处理：把 flying 模式下从 SDP 直送来的 256-bit 数据整理成 cal1d 需要的 76-bit beat。 |
| `vmod/nvdla/pdp/NV_NVDLA_PDP_rdma.v` | 读 DMA：从 MCIF/CVIF 把输入特征图读进来，内部 ig→cq→eg 三级（同 u4 讲的存储接口结构）。 |
| `vmod/nvdla/pdp/NV_NVDLA_PDP_wdma.v` | 写 DMA：把池化结果写回 MCIF/CVIF，层末产生 done 中断。 |
| `vmod/nvdla/csb_master/NV_NVDLA_csb_master.v` | 配置路由器：地址译码得出 PDP 寄存器页基址（0xD000）与 PDP_RDMA 页基址（0xC000）。 |
| `vmod/nvdla/pdp/NV_NVDLA_PDP_REG_dual.v` | 自动生成的影偶寄存器文件，给出每个配置字段的偏移（DATA_FORMAT、POOLING_KERNEL_CFG、RECIP_KERNEL_* 等）。 |

## 4. 核心概念与源码讲解

### 4.1 PDP 空间池化总览

#### 4.1.1 概念说明

PDP 是一个「读入一张 cube → 池化 → 写出一张更小的 cube」的引擎。它的核心算子只有一个：在通道平面内、用一个二维窗口做空间聚合。CPU 通过寄存器告诉它：池化类型（max/average/min）、窗口大小（kernel_width × kernel_height）、滑动步长（stride_width × stride_height）、输入/输出 cube 的尺寸与地址、padding、以及（仅 average 需要）窗口面积的倒数。

一个关键设计取舍：**二维池化被拆成水平、垂直两段**。这等价于「先在行方向压缩，再在列方向压缩」，对 max/min 完全等价，对 average 也只需保证「先求和、最后除一次」。这样 cal2d 只需缓存 `kernel_height` 行的中间结果，避免了一次性聚集整个二维窗口的像素。

#### 4.1.2 核心流程

PDP 顶层数据流可以画成下面这条链（箭头方向是数据流向）：

```
                (flying mode: SDP 256-bit 直送)
                         │
   MCIF/CVIF ──读──► PDP_rdma ──76-bit──► ┌─► nan 预处理 ─┐
                                          └──────────────┘
                                                  │ (76-bit)
                                                  ▼
            输入选择(datin_src_cfg) ──► preproc ──► cal1d(水平1D) ──112-bit──► cal2d(垂直2D)
                                                                                  │ (64-bit)
                                                                                  ▼
                                                                  PDP_wdma ──写──► MCIF/CVIF
                                                                       │
                                                                       └─ done_intr ──► GLB
```

逐步说明：

1. **取数**：`PDP_rdma` 把输入 cube 从片外主存（MCIF/DBB）或片上 CVSRAM（CVIF）读入，输出 76-bit 的数据 beat。若 `flying_mode=0`（在飞模式），数据不进 RDMA，而是由 SDP 直接 256-bit 推给 preproc。
2. **预处理**：`preproc` 把 flying 的 256-bit 整理成 cal1d 统一的 76-bit beat（64-bit 数据 `pre2cal1d_data` + 12-bit 边信息 `pre2cal1d_info`）。`nan` 模块在 RDMA 输出上做 NaN/Inf 检测与可选清零。
3. **水平 1D 池化（cal1d）**：在一行内，按 kernel_width 与 stride 滑动窗口，调用 `pooling_MAX/MIN/SUM` 得到行方向压缩结果，输出 112-bit beat（4 个 28-bit 单元）。
4. **垂直 2D 池化（cal2d）**：把若干行 cal1d 结果存进行缓冲，攒够 kernel_height 行后做垂直方向的同类聚合；average 模式再乘以预存的倒数，得到最终每个窗口一个值，输出 64-bit beat。
5. **写回**：`PDP_wdma` 把 64-bit 结果装配成 512-bit 原子写回存储；整层写完且读完毕时，经 `intr_fifo` 产出 `pdp2glb_done_intr_pd[1:0]` 双脉冲上报 GLB。

#### 4.1.3 源码精读

PDP 顶层 [NV_NVDLA_pdp.v:239-269](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/pdp/NV_NVDLA_pdp.v#L239-L269) 例化读 DMA `u_rdma`，把 `mcif2pdp_rd_rsp_*` / `cvif2pdp_rd_rsp_*` 接成输入、`pdp_rdma2dp_*` 作为去往计算核的输出。注意一个配置分工（见 [NV_NVDLA_pdp.v:434-435](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/pdp/NV_NVDLA_pdp.v#L434-L435) 注释）：**RDMA 拥有自己独立的配置寄存器与独立 CSB 端口** `csb2pdp_rdma_req_*`（页基址 0xC000），而 **WDMA 与 core 共用主配置寄存器**（页基址 0xD000）。

顶层用两根简单的译码线标记当前模式（[NV_NVDLA_pdp.v:275-276](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/pdp/NV_NVDLA_pdp.v#L275-L276)）：`fp16_en = (input_data==2'h2)`、`aver_pooling_en = (pooling_method==2'h0)`。这两根线决定了一个 fp16 专用时钟门 `u_slcg_fp16` 是否开启——只有「fp16 且 average」时才点亮浮点加法树时钟（[NV_NVDLA_pdp.v:298-306](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/pdp/NV_NVDLA_pdp.v#L298-L306)），其余情况省电。

计算核顶层 [NV_NVDLA_PDP_core.v:230-348](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/pdp/NV_NVDLA_PDP_core.v#L230-L348) 把三级流水串起来：`u_preproc`（行 230）→ `u_cal1d`（行 251）→ `u_cal2d`（行 305）。注意它们之间的握手宽度逐级变化：preproc→cal1d 是 76-bit（`pre2cal1d_pd`），cal1d→cal2d 是 112-bit（`pooling1d_pd`），cal2d→wdma 是 64-bit（`pdp_dp2wdma_pd`）——这正是「水平压缩、再垂直压缩」在数据宽度上的体现。

配置寄存器由 [NV_NVDLA_PDP_REG_dual.v:535-765](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/pdp/NV_NVDLA_PDP_REG_dual.v#L535-L765) 定义。几个对池化最关键的寄存器（同属影偶组，字段名见注释）：

| 寄存器 | 字段 | 含义 |
| --- | --- | --- |
| `DATA_FORMAT` | input_data | 0=int8, 1=int16, 2=fp16 |
| `OPERATION_MODE_CFG` | flying_mode / pooling_method / split_num | 数据来源 / 池化类型(0=avg,1=max,2=min) / 宽度切分数 |
| `POOLING_KERNEL_CFG` | kernel_width/height, stride_width/height | 窗口大小与步长（字段值为「实际尺寸 − 1」） |
| `POOLING_PADDING_CFG` | pad_left/right/top/bottom | 各方向补边像素数 |
| `POOLING_PADDING_VALUE_x_CFG` | pad_value_1x..7x | average 模式下补边乘以倍数后的值（x1..x7） |
| `RECIP_KERNEL_HEIGHT/WIDTH` | recip_kernel_height/width | average 用的窗口面积倒数（软件预算） |
| `DATA_CUBE_IN/OUT_*` | 尺寸/地址/stride | 输入输出 cube 的几何与布局 |

地址译码在 csb_master 里得出 PDP 页基址：[NV_NVDLA_csb_master.v:1227](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L1227) `select_pdp = ((core_byte_addr & addr_mask) == 32'h0000d000)`，RDMA 页为 0xC000（[NV_NVDLA_csb_master.v:1487](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L1487)）。

#### 4.1.4 代码实践

**目标**：在源码中确认「PDP 把二维池化拆成水平/垂直两级」这件事，并定位每个配置字段。

**步骤**：
1. 打开 `NV_NVDLA_PDP_core.v`，找到 230/251/305 行的三处例化，确认顺序是 preproc→cal1d→cal2d。
2. 在 `NV_NVDLA_PDP_REG_dual.v` 搜索 `POOLING_KERNEL_CFG_0`，确认 `kernel_width`、`kernel_stride_width`、`kernel_height`、`kernel_stride_height` 四个字段挂在同一个 32-bit 寄存器上。
3. 在 `NV_NVDLA_PDP_REG_dual.v` 搜索 `RECIP_KERNEL_WIDTH_0`，确认其地址为 `0xd038 & 0xfff = 0x038`（即 PDP 页内偏移 0x38）。

**预期结果**：你会看到水平/垂直方向的参数是**对称分布**的（width 一组、height 一组），这正是「两级池化」在寄存器层面的投影——硬件对两个方向一视同仁。

**待本地验证**：若手头有可读寄存器的仿真环境，可在运行 `pdp_max_pooling_int16` trace 前/后读回 `OPERATION_MODE_CFG`，确认 `pooling_method` 字段为 `2'h1`（max）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 average 池化也能用「先水平后垂直」两段实现？会不会因为中间除两次而丢精度？
> **答**：两段都只做「求和」(pooling_SUM)，不做除法；除法（实际是乘倒数）只在 cal2d 最末端做一次。所以中间没有二次取整，精度与一次性平均等价。

**练习 2**：`u_slcg_fp16` 这个时钟门的点亮条件是 `slcg_op_en[2] & fp16_en & aver_pooling_en`，为什么还要 AND 上 `aver_pooling_en`？
> **答**：fp16 的专用加法树（`HLS_fp16_to_fp17`、`fp16_4add` 等）只在 average 池化时才需要求和；max/min 只做比较、不累加，用不到这套浮点加法器，故此时关钟省电。

### 4.2 cal1d 与 cal2d：水平与垂直两级池化计算单元

#### 4.2.1 概念说明

这一节是 PDP 的「算术核心」。cal1d 处理水平方向：把一行像素按窗口和步长压缩；cal2d 处理垂直方向：缓存若干行 cal1d 的结果，再做一次同类压缩。两者复用同一组「1D 池化单元」函数 `pooling_MAX / pooling_MIN / pooling_SUM`，由 `pooling_fun` 按 `pooling_type` 选择。

精度复用是这里的精髓：同一个 22-bit 数据槽，int8 时拆成「高 11-bit + 低 11-bit」两个符号扩展后的 8-bit 值（一次比/加两个），int16 时是一个符号扩展后的 16-bit，fp16 时是一个 16-bit 浮点。用一组比较器/加法器，靠 `reg2dp_int8_en/int16_en/fp16_en` 在函数里三选一，硬件不重复。

#### 4.2.2 核心流程

**输入源选择（cal1d 入口）**：

```
datin_src_cfg == 1 (off_flying)  →  数据来自 PDP_rdma（从存储读）
datin_src_cfg == 0 (on_flying)   →  数据来自 SDP 直送（不落存储）
```

**水平 1D 池化（cal1d 主体）**：

```
for 每一行:
    按 stride 滑动宽度为 kernel_width 的窗口
    对窗口内元素: pooling_MAX / pooling_MIN / pooling_SUM（依 pooling_type）
    输出一个 112-bit beat（4×28-bit，含有效掩码/坐标）
```

**垂直 2D 池化（cal2d 主体）**：

```
行缓冲 line_buffer 缓存最近若干行 cal1d 结果
当攒满 pooling_size_v 行:
    对同一列上的 pooling_size_v 个值再做一次 pooling_MAX/MIN/SUM
    若 average: 结果 *= recip_height（预算倒数）   # 实为乘法
    输出 64-bit beat 给 WDMA
```

**池化类型译码**（在 cal2d 与 unit1d 中一致）：

```
pooling_type == 2'h0  →  mean（average）
pooling_type == 2'h1  →  max
pooling_type == 2'h2  →  min
```

> average 的「除法」：硬件不做除法器，而是由软件预先算好窗口面积的定点倒数，写进 `RECIP_KERNEL_WIDTH/HEIGHT`，cal2d 用一次乘法近似 `sum / N`。

#### 4.2.3 源码精读

**输入源二选一**在 cal1d 里 [NV_NVDLA_PDP_CORE_cal1d.v:506-513](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/pdp/NV_NVDLA_PDP_CORE_cal1d.v#L506-L513)：

```verilog
assign off_flying_en = (datin_src_cfg == 1'h1);   // 走 RDMA
assign on_flying_en  = (datin_src_cfg == 1'h0);   // 走 SDP flying
assign pdp_datin_pd_f_mux0  = off_flying_en ? pdp_rdma2dp_pd[75:0] : sdp2pdp_pd[75:0];
assign pdp_datin_pvld_mux0  = off_flying_en ? pdp_rdma2dp_valid    : sdp2pdp_valid;
assign pdp_rdma2dp_ready = pdp_datin_prdy_mux0 & off_flying_en;
assign sdp2pdp_ready     = pdp_datin_prdy_mux0 & on_flying_en;
```

注意 ready 信号也按模式回送给对应的一方，确保没被选中的源不会被误握手。

**三种池化算子函数**定义在共用单元里。先看取最大值 `pooling_MAX`（[NV_NVDLA_PDP_CORE_unit1d.v:249-290](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/pdp/NV_NVDLA_PDP_CORE_unit1d.v#L249-L290)）：对 int8，把 22-bit 拆成 `data0_msb/data0_lsb` 两个 11-bit 分别比；对 int16 整体有符号比较；对 fp16 则按符号位分情况比（正正、负负、正负）。`pooling_MIN`（[NV_NVDLA_PDP_CORE_unit1d.v:203-247](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/pdp/NV_NVDLA_PDP_CORE_unit1d.v#L203-L247)）结构对称、只是取小。求和 `pooling_SUM`（[NV_NVDLA_PDP_CORE_unit1d.v:292-325](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/pdp/NV_NVDLA_PDP_CORE_unit1d.v#L292-L325)）则是带符号加法。

最终由 `pooling_fun`（[NV_NVDLA_PDP_CORE_unit1d.v:328-370](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/pdp/NV_NVDLA_PDP_CORE_unit1d.v#L328-L370)）按类型派发，并处理 fp16 的 NaN 传播（任一操作数是 NaN 时直接透传 NaN）。类型译码在这几行（[NV_NVDLA_PDP_CORE_unit1d.v:342-344](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/pdp/NV_NVDLA_PDP_CORE_unit1d.v#L342-L344)）：

```verilog
min_pooling  = (pooling_type == 2'h2);
max_pooling  = (pooling_type == 2'h1);
mean_pooling = (pooling_type == 2'h0);
```

cal2d 里有一份结构相同的 `pooling_fun`（[NV_NVDLA_PDP_CORE_cal2d.v:3814-3831](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/pdp/NV_NVDLA_PDP_CORE_cal2d.v#L3814-L3831)），只是输入宽度变成 112-bit（4×28-bit，对应一级压缩后的更宽数据）。两份函数同名同语义，体现「同一算子用在两个方向」。

**垂直窗口大小**有个 0-based 编码：[NV_NVDLA_PDP_CORE_cal2d.v:1437](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/pdp/NV_NVDLA_PDP_CORE_cal2d.v#L1437) `pooling_size_v[3:0] = pooling_size_v_cfg[2:0] + 1`。也就是说配置字段写 1 表示窗口高 2，写 2 表示高 3。**所以 2×2 池化时 kernel_width_cfg 与 kernel_height_cfg 都写 1**。

**行缓冲行数**根据窗口高度动态分配（[NV_NVDLA_PDP_CORE_cal2d.v:1256-1260](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/pdp/NV_NVDLA_PDP_CORE_cal2d.v#L1256-L1260)），`buffer_lines_0/1/2` 决定要缓存几行 cal1d 结果才能开始垂直聚合。

**average 的倒数乘**：cal2d 把软件预算的倒数锁存到本地寄存器 `reg2dp_recip_height_use / reg2dp_recip_width_use`（[NV_NVDLA_PDP_CORE_cal2d.v:887-888](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/pdp/NV_NVDLA_PDP_CORE_cal2d.v#L887-L888)），在求和后做定点乘法完成「除以窗口面积」。这就是 PDP 不需要除法器的原因。

#### 4.2.4 代码实践：2×2 max pooling 如何用 cal1d/cal2d 取最大值

**目标**：用一个具体的 2×2 max pooling，把「水平取 max → 垂直取 max」两步走通，并说明窗口数据如何由 RDMA 预取。

**场景设定**（示例数据，非项目源码）：

```
输入 4×4 特征图（单通道，int16）：
  [ 1  3 | 2  4 ]
  [ 5  7 | 6  8 ]
  -----------------
  [ 9 11 |10 12 ]
  [13 15 |14 16 ]
窗口 2×2，stride 2，无 padding。期望输出 2×2：
  [7 8]    即 max(1,3,5,7)=7, max(2,4,6,8)=8
  [15 16]  即 max(9,11,13,15)=15, max(10,12,14,16)=16
```

**两步分解**：

1. **cal1d 水平 max**：对每一行做 2-wide 窗口、stride 2 的 max。
   - 行0：max(1,3)=3，max(2,4)=4 → `[3 4]`
   - 行1：max(5,7)=7，max(6,8)=8 → `[7 8]`
   - 行2：max(9,11)=11，max(10,12)=12 → `[11 12]`
   - 行3：max(13,15)=15，max(14,16)=16 → `[15 16]`
   这些行结果被 cal2d 的行缓冲逐行存下（`buffer_lines_*` 按 `pooling_size_v=2` 行汇聚）。
2. **cal2d 垂直 max**：对相邻 2 行的 cal1d 结果再取列向 max。
   - 行0&1：max(3,7)=7，max(4,8)=8 → `[7 8]`
   - 行2&3：max(11,15)=15，max(12,16)=16 → `[15 16]`
   最终输出 `[7 8 / 15 16]`，与一次性二维 max 完全一致（max 的可分离性）。

**RDMA 预取的角色**：上述输入特征图按行铺在存储里。`PDP_rdma` 依据 `SRC_BASE_ADDR_LOW/HIGH`、`SRC_LINE/SURFACE_STRIDE`、`DATA_CUBE_IN_WIDTH/HEIGHT` 游走地址，一行行发出读请求；每行像素经 ig→cq→eg 回到 PDP 后，cal1d 立刻对该行做水平压缩——也就是说**窗口的「水平两列」是同一行里相邻取到并比较的，而「垂直两行」是靠 cal2d 的行缓冲把第 N 行暂存、等第 N+1 行到来后再比较**。这就是「水平即时比、垂直缓存比」的分工。

**操作步骤**：
1. 打开 `NV_NVDLA_PDP_CORE_unit1d.v` 的 `pooling_MAX`（249 行），跟踪 `max_16int_ff = ($signed(int16_data0) > $signed(int16_data1))`，确认 int16 走的是整体有符号比较、结果选大的那一个。
2. 打开 `NV_NVDLA_PDP_CORE_cal2d.v:1437`，确认 `pooling_size_v = cfg + 1`，验证「2×2 → 字段写 1」。
3. 打开 `NV_NVDLA_PDP_core.v:251-300`，确认 cal1d 输出的 `pooling1d_pd[111:0]` 正是 cal2d 的输入 `pooling1d_pd`（行 312），即水平结果直喂垂直级。

**预期结果**：你应当能在脑中（或纸上）复现上面的两步分解，并理解 cal2d 行缓冲里始终只保留「当前窗口高度」那么多行，不需要把整张图存下。

#### 4.2.5 小练习与答案

**练习 1**：若把上面例子改成 2×2 **average** pooling（无 padding），两步分别会算出什么？最后那一步「除法」在哪做？
> **答**：cal1d 水平求和：行0 `[1+3, 2+4]=[4,6]`，行1 `[12,14]`，行2 `[20,22]`，行3 `[28,30]`；cal2d 垂直求和：行0&1 `[4+12, 6+14]=[16,20]`，行2&3 `[48,52]`；最后 cal2d 用锁存的 `recip_height*recip_width`（即 1/4）做一次乘法，得 `[4,5]` 与 `[12,13]`。除法（乘倒数）只在 cal2d 末端做一次。

**练习 2**：int8 模式下一个数据 beat 能处理几个像素？依据是什么？
> **答**：8 个。`pooling_fun` 把 88-bit 拆成 4 个 22-bit 槽，int8 时每个槽再拆 `{msb[10:0], lsb[10:0]}` 两个符号扩展的 8-bit 值，所以 4 槽 × 2 = 8 个 int8 像素（见 `pooling_MAX` 里 `data0_msb/data0_lsb` 的处理）。

### 4.3 RDMA 读通路与 WDMA 写通路（含 done 中断）

#### 4.3.1 概念说明

PDP 的存储访问和 u4 讲过的 MCIF/CVIF 完全衔接：PDP_rdma 是「读客户端」，PDP_wdma 是「写客户端」。RDMA 把输入 cube 读进来喂给计算核；WDMA 把池化结果写回存储，并在整层完成后向 GLB 上报 done 中断。注意 PDP 的 RDMA 拥有**独立的 CSB 配置端口**（基址 0xC000），而 WDMA 与 core 共用主配置端口（基址 0xD000）。

#### 4.3.2 核心流程

**读通路（PDP_rdma）**：与所有引擎 DMA 同构的 ig→cq→eg 三级。
- ig（ingress）：按 `DATA_CUBE_IN_*`、`SRC_*`、`PARTIAL_WIDTH_IN_*` 游走地址，生成发往 MCIF/CVIF 的读请求。
- cq（命令队列）：记录在途读事务的上下文，等待数据返回。
- eg（egress）：按返回数据的 id 把 512-bit 响应分发回 PDP，输出 76-bit 的 `pdp_rdma2dp_pd` 给计算核。

**写通路（PDP_wdma）**：
- 接收 cal2d 的 64-bit 结果 `pdp_dp2wdma_pd`，按 `DST_*`、`DATA_CUBE_OUT_*`、`PARTIAL_WIDTH_OUT_*` 装配成 512-bit 写事务发往 MCIF/CVIF。
- 整层写完（`op_done`）且 RDMA 也读完（`reading_done_flag`）时，产生 `wdma_done`，进而 `dp2reg_done`。
- done 脉冲写入 `intr_fifo`，按 `interrupt_ptr`（0 或 1，对应影偶组）读出，生成 `pdp2glb_done_intr_pd[1:0]` 双线脉冲上报 GLB。

**flying 模式的特殊性**：当 `flying_mode=0`（数据在飞、来自 SDP）时，RDMA 不读存储，WDMA 的「读完毕」标志由 `op_load & on_fly_en` 置位（因为不存在 RDMA 读尾巴）。

#### 4.3.3 源码精读

RDMA 顶层例化 ig 子模块（[NV_NVDLA_PDP_rdma.v:136-160](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/pdp/NV_NVDLA_PDP_rdma.v#L136-L160)），ig 拿到 `reg2dp_src_base_addr_*`、`reg2dp_cube_in_*`、`reg2dp_partial_width_in_*` 等几何参数后，向 `pdp2mcif_rd_req_*` / `pdp2cvif_rd_req_*` 发读请求；返回数据由 eg 汇成 `pdp_rdma2dp_pd[75:0]`。内部可见 `ig2cq_*` / `cq2eg_*` 三级握手信号（[NV_NVDLA_PDP_rdma.v:85-94](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/pdp/NV_NVDLA_PDP_rdma.v#L85-L94)）。

WDMA 的「层完成」判定是本节重点（[NV_NVDLA_PDP_wdma.v:295-340](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/pdp/NV_NVDLA_PDP_wdma.v#L295-L340)）：

```verilog
assign on_fly_en  = reg2dp_flying_mode == 1'h0;
assign off_fly_en = reg2dp_flying_mode == 1'h1;
// reading_done_flag: 标记输入侧已读完（off_fly 时等 rdma2wdma_done，on_fly 时 op_load 即可）
// wdma_done = op_done & reading_done_flag   （正常结束）
//          或 waiting_rdma & reading_done_flag （曾等 RDMA 收尾）
assign op_done      = reg_cube_last & is_last_beat & dat_accept;
assign dp2reg_done  = wdma_done;
```

这段逻辑解决一个真实问题：**层切换时 RDMA 可能还在读旧层的尾巴**，所以必须等 `reading_done_flag` 置位才能认定本层真正结束，避免误判。

中断上报用一个 intr_fifo 把完成事件按影偶组排队（[NV_NVDLA_PDP_wdma.v:2349-2376](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/pdp/NV_NVDLA_PDP_wdma.v#L2349-L2376)）：

```verilog
assign intr_fifo_wr_pd   = reg2dp_interrupt_ptr;     // 0 或 1：本轮属哪个影偶组
assign intr_fifo_wr_pvld = wdma_done;                // 层完成时入队
...
pdp2glb_done_intr_pd[0] <= intr_fifo_rd_pvld & intr_fifo_rd_prdy & (intr_fifo_rd_pd==0);
pdp2glb_done_intr_pd[1] <= intr_fifo_rd_pvld & intr_fifo_rd_prdy & (intr_fifo_rd_pd==1);
```

也就是说 `pdp2glb_done_intr_pd[1:0]` 是两根交替点亮的脉冲线，对应 producer/consumer 两组——这与 u2-l4 讲的 GLB「每个引擎 2 个影偶组、共 16 个中断源」完全对齐：PDP 占 `done_source` 里的 2 位。

#### 4.3.4 代码实践

**目标**：跟踪一次 PDP 读请求的发出与一次写结果的中断上报路径。

**步骤**：
1. 在 `NV_NVDLA_PDP_rdma.v` 找到 `pdp2mcif_rd_req_valid` / `pdp2cvif_rd_req_valid` 的驱动（ig 子模块输出），确认它们最终连到顶层 [NV_NVDLA_pdp.v:239-269](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/pdp/NV_NVDLA_pdp.v#L239-L269) 的同名端口，再经 partition_o 进入 MCIF/CVIF。
2. 在 `NV_NVDLA_PDP_wdma.v:340` 确认 `dp2reg_done = wdma_done`，再在 2367/2374 行确认 `pdp2glb_done_intr_pd[0/1]` 的产生条件。
3. 回到顶层 [NV_NVDLA_pdp.v:330-345](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/pdp/NV_NVDLA_pdp.v#L330-L345)，确认 WDMA 的 `pdp2glb_done_intr_pd[1:0]` 直接连到 PDP 顶层输出端口，最终送达 GLB 的中断控制器。

**预期结果**：你会看到「读请求」与「写完成中断」是两条互不干扰的路径——RDMA 只读不写、WDMA 只写不读，二者通过 `rdma2wdma_done` 这一根信号在层末做一次同步握手。

**待本地验证**：在仿真中跑 `pdp_max_pooling_int16`，在波形里抓 `pdp2glb_done_intr_pd` 与 `mcif2pdp_wr_rsp_complete`，确认每个完成脉冲都对应一次写响应完成。

#### 4.3.5 小练习与答案

**练习 1**：为什么 WDMA 要等到 `reading_done_flag` 才认定 `wdma_done`，而不是写完最后一个 beat 就认定？
> **答**：因为 off_flying 模式下，输入来自 RDMA 读存储；当 WDMA 写完最后一批数据时，RDMA 可能还在读旧层的尾巴（层切换窗口）。不等 RDMA 收尾就上报 done，会让 regfile 过早切换影偶组、误把旧层数据算进新层。`reading_done_flag` 保证了「输入侧也确实读完」。

**练习 2**：PDP 的 done 中断为什么是两根线 `pdp2glb_done_intr_pd[1:0]` 而不是一根？
> **答**：对应 producer/consumer 两个影偶组（`interrupt_ptr` 为 0 或 1）。两层配置可以背靠背无缝接跑：当前层完成点亮一组中断、同时另一组已在配置下一层，GLB 据此分别置位 `done_source` 里 PDP 的两位。

## 5. 综合实践

把本讲三节串起来，完成一次「读懂并跑通一个真实 PDP 池化」的任务。

仓库里自带一条真实的 PDP 测试激励：`verif/traces/traceplayer/pdp_max_pooling_int16`。请按下列步骤综合实践：

1. **读激励**：按 u1-l4 讲过的方法，在 `verif/sim` 下执行
   ```
   make run TESTDIR=../traces/traceplayer/pdp_max_pooling_int16
   ```
   在生成的 `verif/sim/_**test**_/` 结果目录里找到 `test.log`，确认 `checktest` 判为 PASSED。
2. **解析配置**：打开该 trace 的 `input.txn`（CSB 写序列），定位写到 PDP 页（地址高字节 0xD0）的几条写命令，找出它给 `OPERATION_MODE_CFG`（pooling_method 应为 1=max）、`POOLING_KERNEL_CFG`（kernel_width/height）、`DATA_FORMAT`（int16=1）填的值。
3. **画数据通路图**：在一张图上标出 `MCIF/CVIF → PDP_rdma →(76b)→ nan → preproc → cal1d →(112b)→ cal2d →(64b)→ PDP_wdma → MCIF/CVIF`，并在 cal2d 处画出「行缓冲暂存 kernel_height 行 → 垂直 max」的小框。
4. **对照源码自检**：用第 2 步读到的 kernel 尺寸，确认 `pooling_size_v = cfg + 1`（cal2d.v:1437）与你期望的实际窗口高度一致；确认 cal1d 输入选择（cal1d.v:506-513）与该 trace 的 `flying_mode` 一致。

> 若本地没有 VCS/Verilator 环境无法实际运行，第 1 步可标注「待本地验证」，但第 2~4 步的源码阅读与画图部分必须完成——它们不依赖仿真器。

## 6. 本讲小结

- PDP 是 NVDLA 的池化引擎，在每个通道平面内做空间池化，支持 **average（method=0）/ max（1）/ min（2）** 与 **int8/int16/fp16** 三种精度。
- 二维池化被**可分离**地拆成「水平 1D（cal1d）＋垂直 2D（cal2d）」两级，对 max/min 严格等价、对 average 只在末端做一次除法（实为乘倒数）。
- 三种池化算子由 `pooling_MAX/MIN/SUM` 三个函数实现，靠 `reg2dp_int8_en/int16_en/fp16_en` 在同一硬件上复用三种精度；average 用软件预算的 `RECIP_KERNEL_*` 倒数做定点乘，省去除法器。
- 配置字段是 0-based：`pooling_size_v = cfg + 1`，所以 2×2 池化的 kernel 字段写 1。
- 数据通路为 **PDP_rdma（ig→cq→eg）读入 → preproc/nan → cal1d → cal2d → PDP_wdma 写回**；flying 模式下输入直接来自 SDP，不进 RDMA。
- 完成中断 `pdp2glb_done_intr_pd[1:0]` 经 intr_fifo 按 `interrupt_ptr` 交替点亮两个影偶组，上报 GLB 的 `done_source`；WDMA 用 `reading_done_flag` 等待 RDMA 收尾后再认定 `wdma_done`，避免层切换误判。

## 7. 下一步学习建议

- **横向对比**：阅读 u5-l3（CDP/LRN）与 u5-l1（SDP），把三个后处理引擎的「读 DMA → 计算 → 写 DMA」骨架做对比——你会发现 PDP/CDP/SDP 的 RDMA/WDMA 结构高度同构，差异只在计算核。
- **纵向深入存储接口**：PDP_rdma 的 ig→cq→eg 三级是 u4-l1/u4-l2 讲的 MCIF 客户端模式的实例，可对照阅读 `NV_NVDLA_PDP_RDMA_ig/cq/eg.v` 与 `NV_NVDLA_MCIF_READ_ig.v`，理解引擎侧 DMA 与 MCIF 的接口契约。
- **中断与影偶**：回到 u2-l4（GLB）确认 PDP 占据 `done_source` 的哪两位，再到 u2-l3 理解 producer/consumer 切换如何与本讲的 `interrupt_ptr` 配合实现无缝接跑。
- **参考模型**：若想验证你对池化行为的理解，可阅读 `cmod/` 下 PDP 的 C 参考模型（与 RTL 一一对应），用它生成期望输出与 RTL 仿真比对。
