# Rubik：数据重排引擎（reshape/contract）

## 1. 本讲目标

学完本讲，读者应当能够：

- 说出 Rubik（魔方）引擎在 NVDLA 中的定位：它是一个**只搬数据、不做计算**的张量布局重排（reshape/reformat）引擎，位于中央枢纽 partition_o。
- 区分 Rubik 的三种重排模式 **contract / split / merge**，并知道每种模式解决哪一类「上一层产出的布局 ≠ 下一层期望的布局」的问题。
- 读懂 `seq_gen`（访问序列生成器）如何像「大脑」一样，按宽度/高度/通道/反卷积步长游走源立方体，生成一串读请求和写命令。
- 读懂 `rf_core`（4 KB 双 bank 重排缓冲）+ `rf_ctrl`（地址编排器）如何用「写入地址模式」和「读出地址模式 + 字节移位」两种不同的访问图案完成一次数据重排。
- 追踪一条完整的「读→缓冲→重排→写→中断」数据通路，并理解读写如何经 `dma` 在 MCIF（主存 DBB）与 CVIF（片上 CVSRAM）之间二选一路由。

## 2. 前置知识

本讲默认你已经掌握 u4-l1（存储接口架构），并了解以下概念：

- **张量布局（tensor layout）**：一个特征图在内存里如何摆放。常见两种极端是「**packed / 通道交织**」（同一像素的多个通道紧挨着放，类似 NHWC）和「**planar / 通道分平面**」（每个通道独占一块连续内存，类似 NCHW）。NVDLA 内部各引擎对布局有偏好，前后两层的偏好不一致时就需要重排。
- **原子（atom）**：NVDLA 在存储接口上搬运的最小单位，对 Rubik 而言一个 atom = 32 字节（`RUBIK_ATOM_CUBE_SIZE = 32`）。一次 DMA 事务搬运若干个 atom。
- **MCIF / CVIF**：两套结构同构的对外存储接口（见 u4-l1）。MCIF 接片外主存 DBB，CVIF 接片上 CVSRAM。Rubik 的源端、目的端可各自二选一。
- **影偶（shadow / producer-consumer）配置**：见 u2-l3。Rubik 的操作参数放在 `dual_reg` 影偶寄存器里，CPU 写一组、引擎跑另一组，实现无停顿接跑。本讲的 `dp2reg_consumer` 就是当前 consumer 组指示。
- **精度**：Rubik 支持 int8（`in_precision=0`，1 字节/元素，32 元素/atom）、int16/fp16（`in_precision=1`，2 字节/元素，16 元素/atom）。

> 一个直觉：Rubik 就像一个「内存里的转置/重排工人」。它读一块布局 A 的数据进一个小缓冲，再按布局 B 的地址顺序写出去——**数据值一个都不改，只是换了个摆放方式**。所以源地址立方体和目的地址立方体的「元素总数和精度」必须一致，变的只是各维的 stride（步长）。

## 3. 本讲源码地图

本讲涉及的关键源码文件（均在 `vmod/nvdla/rubik/` 下，外加 `csb_master`、`cmod/rubik`）：

| 文件 | 作用 |
| --- | --- |
| [NV_NVDLA_rubik.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_rubik.v) | Rubik 顶层，例化 8 个子模块并把它们用内部 wire 连成读/写两条通路 |
| [NV_NVDLA_RUBIK_seq_gen.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_RUBIK_seq_gen.v) | 访问序列生成器（「大脑」）：游走源/目的立方体，产出读请求、写命令、rf 读写命令 |
| [NV_NVDLA_RUBIK_rf_ctrl.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_RUBIK_rf_ctrl.v) | 重排缓冲的地址编排器：按模式生成 rf 写地址、读地址与字节掩码 |
| [NV_NVDLA_RUBIK_rf_core.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_RUBIK_rf_core.v) | 4 KB 双 bank SRAM 重排缓冲，含按模式的字节移位/重组数据通路 |
| [NV_NVDLA_RUBIK_dma.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_RUBIK_dma.v) | DMA 包装：按 `ram_type` 把读写请求路由到 MCIF 或 CVIF，并合并响应 |
| [NV_NVDLA_RUBIK_dual_reg.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_RUBIK_dual_reg.v) | 影偶寄存器文件（含 `rubik_mode`、`in_precision` 等字段与寄存器偏移） |
| [NV_NVDLA_RUBIK_intr.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_RUBIK_intr.v) | 完成中断：在写完成 + 层结束时点亮 2 位影偶 done 上报 GLB |
| cmod/rubik/NV_NVDLA_rbk.cpp | C 参考模型，三种模式的访问序列算法是理解 RTL 行为的最佳注释 |

辅助子模块（本讲会点到但不深读）：`wr_req`（组装写命令+写数据）、`dr2drc`（读响应转 data_fifo）、`slcg`（二级时钟门控）、`regfile`（手写的影偶切换大脑）。

## 4. 核心概念与源码讲解

### 4.1 Rubik 布局重排：一个「只搬运、不计算」的引擎

#### 4.1.1 概念说明

NVDLA 的卷积主链（CDMA→…→CACC）和后处理（SDP/PDP/CDP）各自对数据布局有要求。当**上一层产生的布局，无法直接被下一层消费**时——例如反卷积（deconvolution）会把小特征图按步长「撑大」产生大量隐式空洞，或者上一层输出的是通道交织格式而下一层要通道分平面——就需要在层与层之间插入一次「纯数据搬运 + 重排」。

Rubik 就是这个搬运工人。它的输入和输出都是存储里的特征图立方体，**元素值完全不变**，变的是：

- 每一维（宽度 W、高度 H、通道 C）在内存里的 **stride（步长）**；
- 通道维度是 **交织（packed）** 还是 **分平面（planar）**；
- 对 deconv，是否把按 `deconv_x_stride / deconv_y_stride` 撑开的稀疏布局 **contract（收缩）** 成紧凑布局。

因为只搬不改，Rubik 的源端和目的端可以是两块完全独立的存储区域（地址、ram_type 都可不同）。

#### 4.1.2 核心流程

Rubik 内部是一条「读→缓冲→重排→写」的流水，由顶层 `NV_NVDLA_rubik.v` 用 8 个子模块拼出：

```text
            ┌─────────── seq_gen (访问序列大脑) ───────────┐
            │  游走源立方体 → rd_req (读请求)               │
            │  游走目的立方体 → dma_wr_cmd (写命令)          │
            │  计算 rf 几何  → rf_wr_cmd / rf_rd_cmd        │
            └──────────────────────────────────────────────┘
   读通路:  rd_req ─► dma ─► [MCIF/CVIF] ─► rd_rsp
            rd_rsp ─► dr2drc ─► data_fifo(512b)
            data_fifo ─► rf_ctrl ─► rf_core(写, 按 rf_wr_addr)
   重排:    rf_core(读, 按 rf_rd_addr) + 字节移位/重组
   写通路:  rf_core ─► dma_wr_data ─► wr_req(+ dma_wr_cmd) ─► wr_req_pd
            wr_req_pd ─► dma ─► [MCIF/CVIF] ─► wr_rsp_complete
   收尾:    wr_rsp_complete + 层结束 ─► intr ─► rubik2glb_done_intr_pd[1:0]
```

要点：

1. **seq_gen 是唯一的「地址发生器」**，它既决定从哪里读、也决定往哪里写，还决定缓冲里怎么摆放（rf 命令）。
2. **rf_core 是唯一的数据存储**，源数据先进它、重排后的数据再从它出。
3. **读写分离**：读通路有独立的 `rd_req/rd_rsp`，写通路有独立的 `wr_req/wr_rsp_complete`，二者可并行（一边搬入一边搬出）。
4. **三个 SLCG 时钟门控**：seq_gen、dr2drc、rf_core 各用一个门控时钟 `nvdla_op_gated_clk_0/1/2`，空闲时关钟省电。

#### 4.1.3 源码精读

顶层端口只有四类，印证了「Rubik 是个挂在 CSB + 双 memif 上的搬运工」：

[NV_NVDLA_rubik.v:11-46](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_rubik.v#L11-L46) —— 端口只有：CSB 配置口 `csb2rbk_*`、读请求/响应到 MCIF 与 CVIF 各一对、写请求/完成到 MCIF 与 CVIF 各一对、2 位 `rubik2glb_done_intr_pd` 中断，以及时钟/复位/SLCG 控制。注意 **Rubik 同时拥有 mcif 和 cvif 两套读口、两套写口**，所以源/目的可任意组合。

8 个子模块的例化与连线：

[NV_NVDLA_rubik.v:179-401](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_rubik.v#L179-L401) —— 依次例化 `u_regfile`（影偶寄存器）、`u_intr`（中断）、`u_dma`（双 memif 路由）、`u_seq_gen`（序列大脑）、`u_wr_req`（写命令组装）、`u_rf_ctrl`（地址编排）、`u_dr2drc`（读响应→data_fifo）、`u_rf_core`（重排缓冲）。可以看清：
- `rd_req_pd[78:0]`（seq_gen→dma→memif）、`rd_rsp_pd[513:0]`（memif→dma→dr2drc→`data_fifo_pd[511:0]`）；
- `rf_wr_cmd_pd`（seq_gen→rf_ctrl，描述本次写入的几何）、`rf_rd_cmd_pd`（seq_gen→rf_ctrl，描述本次读出的几何）；
- `dma_wr_cmd_pd[77:0]`（seq_gen→wr_req，写命令/目的地址）、`dma_wr_data_pd[513:0]`（rf_core→wr_req，重排后的数据）；
- 最终 `wr_req_pd[514:0]`（wr_req→dma→memif 写）。

[NV_NVDLA_rubik.v:404-432](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_rubik.v#L404-L432) —— 三个 `NV_NVDLA_RUBIK_slcg` 实例分别生成 `nvdla_op_gated_clk_0/1/2`，由 `slcg_op_en[2:0]`（来自 regfile）分别使能。

寄存器页基址在 csb_master 里被硬编码为 `0x10000`：

[csb_master.v:1682](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L1682) —— `select_rbk = ((core_byte_addr & addr_mask) == 32'h00010000)`，即 Rubik 寄存器页基址 = 0x10000（见 u2-l2 的 4KB 地址译码）。

#### 4.1.4 代码实践

**实践目标**：在源码层面把 Rubik 的读、写两条通路「走通」，确认它确实不做任何算术运算。

**操作步骤**：

1. 打开 [NV_NVDLA_rubik.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_rubik.v)，从 `u_seq_gen` 的 `rd_req_pd` 输出（L304）开始，顺着 `rd_req_pd → u_dma → rbk2mcif/cvif_rd_req_*`（L245/L255）追到顶层对外的读请求端口。
2. 再从对外的 `mcif/cvif2rbk_rd_rsp_pd`（L237/L234）追回 `u_dma` 的 `rd_rsp_pd`（L265）→ `u_dr2drc` → `data_fifo_pd`（L378）→ `u_rf_ctrl` → `u_rf_core` 的 `rf_wr_data`（L389）。
3. 反向追写通路：`u_rf_core` 的 `dma_wr_data_pd`（L397）→ `u_wr_req` → `wr_req_pd`（L329）→ `u_dma` → 顶层 `rbk2mcif/cvif_wr_req_*`。

**需要观察的现象**：在整条通路上，没有任何 `+ - *` 之类的算术；数据宽度从读入的 512 位、到 rf 内部、再到写出的 512 位，**值不变，只有地址和字节掩码在变**。

**预期结果**：你能画出一张「读入 data_fifo → rf_core 重排 → dma_wr_data」的框图，并确认 Rubik 是纯搬运引擎。

> 若无法在本地跑仿真，本实践为「源码阅读型实践」，结论已由源码静态确认。

#### 4.1.5 小练习与答案

**练习 1**：Rubik 的对外端口里，为什么有 mcif 和 cvif **两套**读口、**两套**写口，而不是像 CACC 那样只有一套？

**参考答案**：因为 Rubik 的源端和目的端可以分别落在不同的存储上——源数据可能在片外 DBB（走 MCIF），重排后的结果可能想放进片上 CVSRAM（走 CVIF）供下一层低延迟读取。两套接口允许「源/目的任意组合」，而 CACC 只把结果交给下游，单一接口足够。

**练习 2**：Rubik 重排后元素值会改变吗？源端和目的端什么必须一致？

**参考答案**：不会，Rubik 只改布局不改数值。源端和目的端的**元素总数与精度**必须一致（只是 W/H/C 各维 stride 和通道交织方式变了）。

---

### 4.2 三种重排模式：contract / split / merge

#### 4.2.1 概念说明

`rubik_mode`（2 位，寄存器 `MISC_CFG` 字段）选择三种重排模式，由 seq_gen 和 rf_ctrl 各自解码：

| `rubik_mode` | 名称 | 解决的问题 | 典型用途 |
| --- | --- | --- | --- |
| `2'b00` | **contract**（收缩） | deconv 把特征图按 `deconv_x/y_stride` 撑大成稀疏布局，需收缩成紧凑布局供卷积引擎消费 | 反卷积（transpose-conv）后处理 |
| `2'b01` | **split**（拆分） | 把 **packed（通道交织）** 布局拆成 **planar（通道分平面）** 布局 | 交织→分平面 |
| `2'b10` | **merge**（合并） | 把 **planar（通道分平面）** 布局合并成 **packed（通道交织）** 布局 | 分平面→交织 |

> 三种模式都「reshape」数据，但 contract 额外处理反卷积的步长空洞，split/merge 是一对互逆的通道布局转换。模式名来自 C 参考模型 `cmod/rubik/NV_NVDLA_rbk.cpp` 的 `RubikRdmaSequenceContract/Split/Merge` 三个函数。

#### 4.2.2 核心流程

三种模式在「地址游走方式」和「缓冲访问图案」上不同。以 C 参考模型（最清晰的算法描述）为准：

- **merge**（planar→packed）：源端每个通道独占一块内存（`planar_stride` 分隔）。对每个输出 surface（一组最多 `element_per_atom` 个通道：int8 为 32、int16/fp16 为 16），按 H→W 逐行读入，**把来自不同平面的通道拼到同一行**。地址：`base + h*line_stride + ch*planar_stride + w*element_byte`（见 [rbk.cpp:517](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/rubik/NV_NVDLA_rbk.cpp#L517)）。

- **split**（packed→planar）：merge 的逆操作。源端通道按 surface 交织（`surf_stride` 分隔），逐 surface 顺序读入，写出时每个 surface 落到独立平面。地址：`base + surface*surf_stride + h*line_stride + w*ATOM_CUBE_SIZE`（见 [rbk.cpp:445](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/rubik/NV_NVDLA_rbk.cpp#L445)）。

- **contract**（deconv 收缩）：用 `stride_x = deconv_x_stride+1`、`stride_y = deconv_y_stride+1`。按「输出 surface → 输入高度 → stride_y → 输入宽度 → stride_x」多层嵌套，把按步长散开的元素聚拢。地址里出现 `(stride_y_iter*stride_x + stride_x_iter + surface_in_iter)*surface_out_num` 这一项（见 [rbk.cpp:368](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/rubik/NV_NVDLA_rbk.cpp#L368)），正是「收缩稀疏网格」的体现。

#### 4.2.3 源码精读

模式解码在 seq_gen 里（rf_ctrl 里也有同样解码）：

[NV_NVDLA_RUBIK_seq_gen.v:311-314](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_RUBIK_seq_gen.v#L311-L314) —— `m_contract = (mode==0)`、`m_split = (mode==1)`、`m_merge = (mode==2)`、`m_byte_data = (in_precision==0)`（int8 为字节、否则为半字）。这两个 `m_*` 开关贯穿整个 seq_gen 和 rf_ctrl，决定了所有 stride、计数器循环和掩码的选择。

模式决定的关键 stride（节选）：

[NV_NVDLA_RUBIK_seq_gen.v:344-372](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_RUBIK_seq_gen.v#L344-L372) —— 例如 `intern_stride = m_contract ? cube_stride : m_merge ? planar_stride : 8'h40`，`width_stridem = m_merge ? (m_byte_data?1:2) : 3'h4`，`chn_stride` 按 split/contract 用 surf_stride、merge 用 planar_stride。这些三元选择正是「同一硬件、三套地址图案」的来源。

contract 模式的「收缩步长」由 deconv 寄存器驱动：

[NV_NVDLA_RUBIK_seq_gen.v:488-503](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_RUBIK_seq_gen.v#L488-L503) —— `inwidth_mul_dx = inwidth*(x_stride+1)-1`、`inheight_mul_dy = inheight*(y_stride+1)-1`，正是「撑大后的稀疏尺寸」，contract 把它收缩回 `inwidth/inheight` 的紧凑尺寸。

寄存器字段确认模式编码与默认值：

[NV_NVDLA_RUBIK_dual_reg.v:196](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_RUBIK_dual_reg.v#L196) —— `misc_cfg_0_out = {22'b0, in_precision, 6'b0, rubik_mode}`，即 `MISC_CFG`（偏移 0x0c）的 bit[1:0]=`rubik_mode`、bit[9:8]=`in_precision`；复位默认 `in_precision=2'b01`（int16）、`rubik_mode=2'b00`（contract），见 [dual_reg.v:334-335](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_RUBIK_dual_reg.v#L334-L335)。

#### 4.2.4 代码实践

**实践目标**：用一个具体例子说清「何时该用哪种模式」。

**操作步骤**：

1. 设想一个网络层：上一层是普通卷积，输出 **packed（通道交织）** 特征图；下一层是 PDP 池化，它按通道平面处理，更希望 **planar** 布局。问：该用哪种模式？
2. 在 [NV_NVDLA_rubik.v:155](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_rubik.v#L155)（`reg2dp_rubik_mode[1:0]`）对应的 `MISC_CFG` 寄存器（偏移 0x1000c）里填入对应模式值。
3. 对照 [rbk.cpp 的 RubikRdmaSequenceSplit](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/rubik/NV_NVDLA_rbk.cpp#L389-L455)（packed→planar）确认源端用 `surf_stride`、写出按 surface 分平面。

**需要观察的现象 / 预期结果**：

- packed→planar 应选 **split（mode=1）**；反过来 planar→packed 选 **merge（mode=2）**。
- 若是反卷积后的稀疏特征图要喂给卷积引擎，选 **contract（mode=0）**，并要额外配 `deconv_x_stride / deconv_y_stride`（寄存器 `DECONV_STRIDE`，偏移 0x10054）。
- 把 NHWC 风格（通道交织）重排为引擎期望的分平面布局，对应 **split**。

> 本实践为「源码阅读 + 配置规划型」，不依赖仿真。

#### 4.2.5 小练习与答案

**练习 1**：merge 与 split 的关系是什么？如果连续做一次 split 再做一次 merge，数据会变成怎样？

**参考答案**：二者互逆（planar↔packed）。理论上先 split 再 merge（参数对称）应恢复原数据；这正是验证 Rubik 正确性的常用方法。

**练习 2**：为什么 contract 模式需要 `deconv_x_stride / deconv_y_stride`，而 split/merge 不需要？

**参考答案**：contract 专门处理反卷积产生的「按步长撑开的稀疏网格」，必须知道步长才能把散开的元素聚拢回紧凑布局；split/merge 只是通道维度的交织↔分平面互换，不涉及空间维度的稀疏化，所以不需要 deconv 步长。

---

### 4.3 seq_gen：访问序列的「大脑」

#### 4.3.1 概念说明

`seq_gen`（sequence generator）是 Rubik 的地址发生器，同时驱动三条输出：

- **`rd_req`（读请求）**：游走**源**立方体，生成一串「地址 + 长度」的 DMA 读请求。
- **`dma_wr_cmd`（写命令）**：游走**目的**立方体，生成一串写命令（含目的地址、长度、是否需要 ack）。
- **`rf_wr_cmd / rf_rd_cmd`（缓冲几何命令）**：告诉 `rf_ctrl` 本次搬入/搬出在 4 KB 缓冲里排成几行几列（即「乒乓阵列的总行数、总列数」）。

它本质上是一组**多层嵌套计数器**（deconv_x → 宽度 → deconv_y → 高度 → 通道），每命中一层边界就把当前基地址加上对应 stride，从而精确地遍历任意 stride 的 3D 立方体。

#### 4.3.2 核心流程

读序列生成的伪代码（以 contract 为例，split/merge 的循环层数不同）：

```text
on op_en rising edge (init_set):  rd_addr = src_base;  各 base = src_base
while rubik_en:
    发读请求 rd_req = {rd_addr<<5, size}        // size 以 32B(atom) 为单位
    if rd_req_accept:                            // 握手成功
        rd_addr += intern_stride                 // 同一行内前进
        if 命中 deconv_x 边界:   rd_addr = rd_width_base + width_stride
        if 命中 宽度边界:        rd_addr = rd_dy_base + cubey_stride
        if 命中 deconv_y 边界:   rd_addr = rd_line_base + line_stride
        if 命中 高度边界:        rd_addr = rd_chn_base + chn_stride
    if 命中 通道边界(rd_channel_end):  rubik 收尾
```

写序列（`wr_addr` 游走 `dest_base`）与之同构，只是 stride 换成 `out_*_stride`。两条序列**各自独立游走自己的立方体**，由 rf 缓冲在中间「对齐」。

#### 4.3.3 源码精读

启动与单次触发：

[NV_NVDLA_RUBIK_seq_gen.v:507-524](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_RUBIK_seq_gen.v#L507-L524) —— `rubik_en` 在 `reg2dp_op_en` 拉起时置 1、在 `dp2reg_done`（层结束）时清 0；`init_set = rubik_en & ~rubik_en_d` 捕获启动那一拍，用于把所有基地址初始化到 `src_base/dest_base`。

读请求格式：

[NV_NVDLA_RUBIK_seq_gen.v:551-560](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_RUBIK_seq_gen.v#L551-L560) —— `rd_req_pd[63:0] = {rd_addr, 5'h0}`（地址左移 5 位 = 以 32B 为粒度的字节地址），`rd_req_pd[78:64]` 是本次长度（按模式取 contract/split/merge 的 `*_rd_size`，单位 atom，自减 1 编码），`rd_req_type = datain_ram_type`（0→CVIF，1→MCIF）。

读地址游走（最核心的一段）：

[NV_NVDLA_RUBIK_seq_gen.v:594-612](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_RUBIK_seq_gen.v#L594-L612) —— 典型的「边界命中则跳到上一层基地址 + 该维 stride」写法：`rd_height_end→rd_chn_base+chn_stride`、`rd_dy_end|rd_mwdth_end→rd_line_base+line_stride`、`rd_cwdth_end→rd_dy_base+cubey_stride`、`rd_dx_end|rd_plar_end→rd_width_base+width_stride`、否则 `rd_req_accept→rd_addr+intern_stride`。每个 `*_base` 指针都在对应边界同步更新（见 L615-672 的 5 个 base 寄存器），保证多层嵌套的正确回卷。

嵌套计数器（决定何时命中各层边界）：

[NV_NVDLA_RUBIK_seq_gen.v:677-765](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_RUBIK_seq_gen.v#L677-L765) —— `rd_dx_cnt`（deconv x）、`rd_width_cnt`、`rd_dy_cnt`（deconv y）、`rd_line_cnt`（高度）、`rd_chn_cnt`（通道）。注意 contract/merge 会推进 `rd_dx_cnt`，而 split 用 `rd_mwdth`（一次 8 个 atom）推进宽度，差异正对应三种模式不同的循环结构。`rd_channel_end`（L752）是整层读序列的总结束条件。

写地址游走（对称结构）：

[NV_NVDLA_RUBIK_seq_gen.v:832-850](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_RUBIK_seq_gen.v#L832-L850) —— `wr_addr` 从 `dest_base` 出发，按 `out_intern_stride/out_width_stride/out_line_stride/out_chn_stride` 游走目的立方体；写命令 `dma_wr_cmd_pd = {wr_req_done, size, wr_addr<<5}`（见 [L793-795](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_RUBIK_seq_gen.v#L793-L795)）。

#### 4.3.4 代码实践

**实践目标**：跟踪 contract 模式下一次读序列的地址推进，理解「边界命中跳 base」机制。

**操作步骤**：

1. 假设 contract 模式、`deconv_x_stride=1`（即 `x_stride=2`）、`inwidth=4`、`inheight=1`、单通道。打开 [NV_NVDLA_RUBIK_seq_gen.v:594-612](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_RUBIK_seq_gen.v#L594-L612)。
2. 手算前若干拍的 `rd_addr`：`init_set` 时 `rd_addr=src_base`；每次 `rd_req_accept` 加 `intern_stride`（contract 下 = `cube_stride`）；当 `rd_dx_cnt==x_stride`（命中 deconv_x 边界 `rd_dx_end`）时，跳到 `rd_width_base + width_stride`。
3. 对照计数器推进 [L681-692](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_RUBIK_seq_gen.v#L681-L692) 与 C 模型 [RubikRdmaSequenceContract](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/rubik/NV_NVDLA_rbk.cpp#L302-L387) 的四层 for/while 循环（surface_out → height → stride_y → width → stride_x）。

**需要观察的现象**：每当一个内层计数器到顶，地址不是简单 +1，而是跳回上一层 base 再加该维 stride，这正是任意 stride 立方体遍历的正确做法。

**预期结果**：你能列出前 ~8 拍 `rd_addr` 的值序列，并与 C 模型的 `payload_addr` 公式一致。

> 具体数值待本地验证（取决于 stride 配置）；RTL 行为由源码静态确认。

#### 4.3.5 小练习与答案

**练习 1**：`init_set` 为什么必须在「op_en 上升沿那一拍」单独处理，而不是复位时一次性初始化？

**参考答案**：因为 Rubik 用影偶配置，CPU 可以在引擎跑第 N 层时预装第 N+1 层参数。每层启动（op_en 上升）都要把所有基地址重新设到该层的 `src_base/dest_base`，所以用 `init_set` 捕获每次启动沿，而非只在全局复位时初始化一次。

**练习 2**：seq_gen 同时输出读请求和写命令，二者游走的是同一个立方体吗？

**参考答案**：不是。读请求游走**源**立方体（`src_base` + 源 stride），写命令游走**目的**立方体（`dest_base` + 目的 stride）。两者元素总数相同但布局（stride、通道交织）不同，这正是「重排」的体现。

---

### 4.4 读写命令通路：rf 缓冲、dma 路由与完成中断

#### 4.4.1 概念说明

seq_gen 只产生「地址序列」，真正存数据、做重排的是这一节的三个部分：

- **rf_core**：一块 4 KB 的双 bank SRAM 重排缓冲（C 模型里 `RUBIK_INTERNAL_BUF_SIZE=2048`，即每 bank 2 KB）。源数据按 rf 写地址写入、按 rf 读地址读出，**写地址图案 ≠ 读地址图案**就完成了重排；再叠加字节级移位/重组，实现 contract/split/merge 三种变换。
- **rf_ctrl**：rf_core 的「地址编排器」。它消费 seq_gen 的 `rf_wr_cmd/rf_rd_cmd`（几何），生成具体的 `rf_wr_addr/rf_rd_addr` 和 `rf_rd_mask`（字节掩码），并用 2 位读写指针做 bank 间的乒乓。
- **dma + wr_req + intr**：把读请求/写命令按 `ram_type` 路由到 MCIF 或 CVIF，并在所有写完成、层结束时点亮 2 位影偶 done 中断上报 GLB。

#### 4.4.2 核心流程

```text
【读入 + 写入 rf】
  rd_rsp(513b) ─► dr2drc ─► data_fifo(512b)
  rf_ctrl: data_fifo_rdy = rf_wr_cmd_open & rf_wr_rdy & ~rf_full & data_fifo_vld
           rf_wr_addr 按 rf_wr_cmd 的行列数 + 模式步进
           rf_wr_data = data_fifo_pd (原样写入)
  rf_core: 把 512b 拆成 32×16b 写进对应 bank 的 32 个 nv_ram_rws_32x16

【重排 + 读出】
  rf_ctrl: rf_rd_addr 按 rf_rd_cmd 的行列数 + 模式步进(与写地址图案不同)
           rf_rd_mask 按模式算出每段有效字节数
  rf_core: 按 rf_rd_addr 读出 → shift512_16b / data_recomb 按模式做字节移位重组
           → dma_wr_data_pd = {2'b掩码, 512b数据}

【写出】
  wr_req: 把 seq_gen 的 dma_wr_cmd(目的地址/长度) + rf_core 的 dma_wr_data 组成 wr_req_pd(515b)
  dma:    wr_req_type(=dataout_ram_type) 选 MCIF/CVIF 发写请求
  完成后 wr_rsp_complete ─► intr ─► rubik2glb_done_intr_pd[1:0]
```

容量与原子关系：每个 atom = 32B，rf_core 每 bank = 2048B = 64 个 atom，2 bank = 128 个 atom；rf 行地址 5 位（32 行），每行 512b = 64B。

#### 4.4.3 源码精读

**(a) rf_ctrl 的地址编排——重排的「几何」**

[NV_NVDLA_RUBIK_rf_ctrl.v:183-186](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_RUBIK_rf_ctrl.v#L183-L186) —— 同样的模式解码 `m_contract/m_split/m_byte_data`。

[NV_NVDLA_RUBIK_rf_ctrl.v:240-257](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_RUBIK_rf_ctrl.v#L240-L257) —— `rf_wr_addr` 步进：contract 模式下一行写完（`rf_wr_col_end`）跳到 `{rf_wr_rcnt_inc[2:0],2'b0}`（行间跳 4），非 split/contract 的半字模式跳 `{rf_wr_rcnt_inc[3:0],1'b0}`（跳 2），否则 +1。**写地址的「跳法」就编码了写入布局**。

[NV_NVDLA_RUBIK_rf_ctrl.v:358-375](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_RUBIK_rf_ctrl.v#L358-L375) —— `rf_rd_addr` 步进：与写地址图案**不同**（contract 跳 `{rf_rd_rcnt_inc[2:0],2'b0}`、split 半字跳 `{rf_rd_rcnt_inc[3:0],1'b0}`）。**读地址图案 ≠ 写地址图案，正是重排的本质**。

[NV_NVDLA_RUBIK_rf_ctrl.v:436-446](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_RUBIK_rf_ctrl.v#L436-L446) —— `rf_rd_mask`：按模式算出两个 32B atom 各自的有效字节数（contract 恒 0x20/0x20、split 按 `remain_byte` 分段、merge 按 `merge_byte_mask`）。掩码用于 rf_core 输出时屏蔽无效字节。

[NV_NVDLA_RUBIK_rf_ctrl.v:461-480](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_RUBIK_rf_ctrl.v#L461-L480) —— 2 位 `rf_wptr/rf_rptr` 做 bank 乒乓：`rf_full`/`rf_nempty` 由两指针比较得出，写完一行 `rf_wr_done` 推 wptr、读完一组 `rf_rd_done` 推 rptr。

**(b) rf_core 的双 bank 重排缓冲——重排的「数据通路」**

[NV_NVDLA_RUBIK_rf_core.v:569-578](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_RUBIK_rf_core.v#L569-L578) —— 「register file 0：含 32 个 RAM，每个 32×16」。注释与实例印证：**每 bank = 32 个 `nv_ram_rws_32x16`，32×16b=512b=64B/行，32 行 = 2 KB**。

[NV_NVDLA_RUBIK_rf_core.v:923-930](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_RUBIK_rf_core.v#L923-L930) —— 「register file 1」是第二个 bank，`re = rf_rd_osel & rf_rd_pop`、`we = rf_wr_osel & rf_wr_pop`（与 bank0 取反），由 `rf_wptr[0]/rf_rptr[0]` 选择写哪个 bank、读哪个 bank，实现乒乓。

[NV_NVDLA_RUBIK_rf_core.v:533-543](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_RUBIK_rf_core.v#L533-L543) —— 读出后的字节级重组：`merge*_rd_data = shift512_16b(rd_data_raw, oaddr)`（按读地址做 16b 粒度桶形移位）、`merge_rd_data = data_recomb16/8(...)`（重组）、contract 则做 256b 半swap。最终 `rf_rd_data` 按模式三选一。**这一步的移位/重组，把「按写地址顺序存的数据」还原成「按读地址期望的布局」**。

[NV_NVDLA_RUBIK_rf_core.v:557-567](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_RUBIK_rf_core.v#L557-L567) —— 用 `rf_rd_mask` 生成 `byte_mask_h/l`，对 512b 数据的高/低 256b 做 `data_mask` 屏蔽，拼成 `dma_wr_data_pd = {2'b掩码, 512b数据}`（514b）。

**(c) dma 的双 memif 路由**

[NV_NVDLA_RUBIK_dma.v:186-187](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_RUBIK_dma.v#L186-L187) —— 读请求按 `rd_req_type`（=`datain_ram_type`）路由：`cv_rd_req_vld = rd_req_vld & (type==0)`（CVIF）、`mc_rd_req_vld = rd_req_vld & (type==1)`（MCIF）。

[NV_NVDLA_RUBIK_dma.v:331-332](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_RUBIK_dma.v#L331-L332) —— 写请求同理按 `wr_req_type`（=`dataout_ram_type`）路由到 CVIF（type=0）或 MCIF（type=1）。**源、目的的 ram_type 各自独立**，所以可做 DBB↔CVSRAM 之间的重排搬运。

**(d) 完成中断（乒乓 2 位）**

[NV_NVDLA_RUBIK_intr.v:84-90](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_RUBIK_intr.v#L84-L90) —— `rubik2glb_done_intr_pd[1:0]` 两位分别对应两个影偶组：`[0]=wr_rsp_complete & layer0_done`、`[1]=wr_rsp_complete & layer1_done`。即**所有写都收到 complete 且该层计算结束**才点亮对应组的 done，符合 PDP/CDP 同款的「双脉冲乒乓上报 GLB」模式（见 u2-l4、u5-l2）。`wr_rsp_complete` 来自 dma 的写响应聚合（[dma.v:380](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_RUBIK_dma.v#L380) 的 `require_ack` 逻辑）。

#### 4.4.4 代码实践

**实践目标**：规划一次「CVSRAM→DBB 的 split 重排」所需的寄存器配置，并指出读、写各走哪条 memif。

**操作步骤**：

1. 任务：源特征图在片上 CVSRAM（packed 布局），要 split 成 planar 写到片外 DBB。
2. 在 Rubik 寄存器页（基址 0x10000）配置：
   - `MISC_CFG`（0x0c）：`rubik_mode=2'b01`（split）、`in_precision` 按精度；
   - `DAIN_RAM_TYPE`（0x10）：`datain_ram_type=0`（CVIF）；`DAOUT_RAM_TYPE`（0x30）：`dataout_ram_type=1`（MCIF）；
   - `DATAIN_SIZE_0/1`（0x14/0x18）：源 W/H/C；`DAIN_ADDR_HIGH/LOW`（0x1c/0x20）：源地址；各源 stride（0x24/0x28）；
   - `DATAOUT_SIZE_1`（0x34）：目的通道；`DAOUT_ADDR_HIGH/LOW`（0x38/0x3c）：目的地址；各目的 stride（0x40/0x4c/0x50）；
   - 最后写 `OP_ENABLE`（0x08）点火。
3. 对照 [dma.v:186-187 与 331-332](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_RUBIK_dma.v#L186-L187) 确认：读走 CVIF（type=0）、写走 MCIF（type=1）。

**需要观察的现象 / 预期结果**：读请求出现在 `rbk2cvif_rd_req_*`、写请求出现在 `rbk2mcif_wr_req_*`；所有写完成（`wr_rsp_complete`）且层结束后，`rubik2glb_done_intr_pd` 对应位脉冲，GLB `done_source` 的 rubik 位（见 u2-l4）置位、顶层 `dla_intr` 触发。

> 寄存器偏移由 [NV_NVDLA_RUBIK_dual_reg.v:154-176](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_RUBIK_dual_reg.v#L154-L176) 静态确认；实际端到端结果待本地仿真验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么重排是靠「写地址图案 ≠ 读地址图案 + 字节移位」实现的，而不是靠算术逻辑？

**参考答案**：因为 Rubik 不改数值、只改布局。同一块数据按图案 A 写入 SRAM、再按图案 B 读出，读出顺序就反映了布局 B；再叠加字节级桶形移位/重组（merge 的 `shift512_16b`、contract 的半 swap），就能完成通道交织↔分平面、deconv 稀疏↔紧凑等变换。用地址+移位代替算术，省面积、省功耗。

**练习 2**：rf_core 为什么用两个 bank（register file 0 和 1）做乒乓？

**参考答案**：为了读、写可以并行且不冲突。seq_gen 往一个 bank 写入新一批数据时，rf_ctrl 可以同时从另一个 bank 读出上一批重排好的数据；用 `rf_wptr[0]/rf_rptr[0]` 切换，避免同 bank 读写竞争，提升吞吐。

---

## 5. 综合实践

**任务**：为一个「反卷积层 → 普通卷积层」的网络设计 Rubik 的使用方案，并把知识串起来。

背景：反卷积（deconv）会在输出特征图里按 `deconv_x_stride/deconv_y_stride` 撑开产生稀疏布局；后续普通卷积引擎（CDMA/CSC）需要紧凑布局才能高效取数。请：

1. **选模式**：根据 4.2 判断该用 contract / split / merge 中的哪一个，并说明理由。
2. **定位关键寄存器**：在 [NV_NVDLA_RUBIK_dual_reg.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_RUBIK_dual_reg.v) 里找出需要配置的寄存器（模式、deconv 步长、源/目的地址与 size、源/目的 ram_type），给出各自的偏移（提示：MISC_CFG=0x0c、DECONV_STRIDE=0x54、DAIN_*/DAOUT_* 见 L154-176）。
3. **追通路**：画出本层数据从「源存储 → seq_gen 读请求 → dma → dr2drc → rf_ctrl/rf_core 重排 → wr_req → dma → 目的存储 → 中断」的完整框图，标注读、写各走 MCIF 还是 CVIF（由 `datain_ram_type/dataout_ram_type` 决定）。
4. **验证思路**：参考 4.2.5 的思路，说明如何用「contract 后再 contract 的逆操作」或对照 C 模型 [RubikRdmaSequenceContract](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/rubik/NV_NVDLA_rbk.cpp#L302-L387) 的输出做黄金比对。

**参考要点**：应选 **contract（mode=0）**，并配置 `deconv_x_stride/deconv_y_stride`（DECONV_STRIDE 偏移 0x54）以及 `contract_stride_0/1`（偏移 0x44/0x48）；读地址游走由 seq_gen 的 `rd_dx_cnt/rd_dy_cnt` 配合 `cubey_stride=cube_stride*(x_stride+1)`（见 [seq_gen.v:359-365](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/rubik/NV_NVDLA_RUBIK_seq_gen.v#L359-L365)）完成收缩；完成后经 `rubik2glb_done_intr_pd` 上报 GLB。具体数值待本地仿真验证。

## 6. 本讲小结

- **Rubik 是纯搬运的张量布局重排引擎**，挂在 partition_o，只改布局不改数值，源/目的可分别落在 MCIF（DBB）或 CVIF（CVSRAM）。
- **三种模式**：contract（mode=0，收缩 deconv 稀疏布局）、split（mode=1，packed→planar）、merge（mode=2，planar→packed），由 `MISC_CFG.rubik_mode` 选择，编码同时出现在 seq_gen 与 rf_ctrl 的 `m_contract/m_split/m_merge` 开关。
- **seq_gen 是地址大脑**：用多层嵌套计数器（deconv_x→宽→deconv_y→高→通道）游走源/目的立方体，靠「边界命中跳上一层 base + stride」实现任意 stride 遍历，同时输出读请求、写命令和 rf 几何命令。
- **重排的本质是「写地址图案 ≠ 读地址图案 + 字节移位」**：rf_ctrl 编排两种不同的地址图案与字节掩码，rf_core 用 4 KB 双 bank SRAM 存数据并做 `shift512_16b`/`data_recomb` 等字节级重组。
- **rf_core 容量**：2 bank × (32 个 `nv_ram_rws_32x16`) = 2 × 2 KB = 4 KB，每 atom 32B；两 bank 用 `rf_wptr[0]/rf_rptr[0]` 乒乓，读写并行不冲突。
- **完成中断**：所有写收到 `wr_rsp_complete` 且层结束后，`intr` 点亮 2 位 `rubik2glb_done_intr_pd` 影偶 done 上报 GLB，与 PDP/CDP 同款。

## 7. 下一步学习建议

- **回到中断聚合**：结合 u2-l4（GLB）看 `rubik2glb_done_intr_pd` 如何进入 `done_source` 并经 `core_intr = OR(~mask & status)` 汇总到顶层 `dla_intr`。
- **端到端编程**：进入 u8-l4（端到端集成），把 Rubik 作为「层间布局适配器」串进一个完整网络（如 BDMA 预装 → 卷积 → Rubik 重排 → 下一层卷积）的启动序列。
- **对照 C 参考模型**：读 [cmod/rubik/NV_NVDLA_rbk.cpp](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/rubik/NV_NVDLA_rbk.cpp) 的三个 `RubikRdmaSequence*` 与 `RubikDataPathSequence*` 函数，它们是 RTL 行为最清晰的算法注释，也是做黄金比对的参考。
- **横向对比同类搬运引擎**：与 u4-l4（BDMA）对比——BDMA 做存储间「等布局」搬运，Rubik 做「变布局」搬运，二者寄存器模型和 done 上报机制高度相似。
