# GLB 全局配置与中断聚合

## 1. 本讲目标

NVDLA 有十几个独立引擎（卷积、池化、DMA、重排……），每个引擎跑完一层都会喊一声「我完成了」。如果让每个引擎各自拉一根中断线去找 CPU，SoC 的中断控制器会被灌爆，CPU 也无法一次看清「整批任务做完没有」。GLB（Global）模块就是来解决这个问题的——它是一张「中断汇总表 + 一个出口」。

学完本讲你应该能够：

1. 说清 GLB 由哪四个子模块组成、各自干什么，以及它们在 `NV_NVDLA_glb.v` 里的例化关系。
2. 解释 8 个引擎、每个引擎 2 个影偶（shadow）组，如何被压成一根 16 位的 `done_source` 中断源向量。
3. 掌握 `INTR_MASK` / `INTR_SET` / `INTR_STATUS` 三类寄存器的语义：掩码是「写 1 屏蔽」还是「写 1 放行」、SET 是写 1 置位、STATUS 是写 1 清除（W1C）。
4. 跟踪一次卷积完成后，中断如何从 CACC 的 `cacc2glb_done_intr_pd` 一路传到顶层 `core_intr` 输出，并理解中间为何要跨时钟域同步。

## 2. 前置知识

本讲承接 u2-l2（csb_master 中央配置路由器）与 u2-l3（寄存器文件与影偶配置机制），你需要已经知道：

- **CSB 配置总线**：CPU 读写 NVDLA 寄存器的唯一通道，请求包 `{nposted, write, wdat[31:0], addr}`，由 csb_master 按地址分发到各引擎。GLB 也是 csb_master 的一个下游客户端。
- **影偶（shadow）配置**：每个引擎有两组操作参数（group0/group1），引擎跑第 N 层时 CPU 把第 N+1 层参数预装到另一组，完成时翻转 consumer 无缝接跑。本讲会看到「完成」这件事如何变成中断信号——每个引擎因此给出 **2 位** done 信号，分别对应两组。
- **统一寄存器接口**：`reg_offset` / `reg_wr_en` / `reg_wr_data` / `reg_rd_data` 四件套，由自动生成的 `_CSB_reg.v` 实现地址译码与读写。GLB 的寄存器文件 `NV_NVDLA_GLB_CSB_reg.v` 就是这类自动生成文件。
- **时钟域**：NVDLA 内部有 `nvdla_core_clk`（核心计算时钟）与 `nvdla_falcon_clk`（配置/中断时钟，falcon 域）两个域。中断最终要送到 CPU 侧的 falcon 域，因此需要跨域同步。

一个通俗比喻：GLB 像一栋大楼的「前台」。十几个办公室（引擎）完工时各自按一下桌上的呼叫钮（done_intr），前台有一面 16 格的「呼叫灯板」（status），管理员可以遮住某些格子不想被打扰（mask），也可以手动按亮某格测试（set），灯亮着且没被遮的格子，前台就把大门铃按响（core_intr）——大楼外面（CPU）只听到一声铃，进门看灯板就知道是谁完了。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [vmod/nvdla/glb/NV_NVDLA_glb.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_glb.v) | GLB 顶层，声明 8 个引擎的 done 中断输入、CSB 配置口与 `core_intr` 输出，例化三个子模块 |
| [vmod/nvdla/glb/NV_NVDLA_GLB_csb.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_csb.v) | CSB 接口适配：拆包 CSB 请求、生成 `reg_offset/reg_wr_en/reg_wr_data`、把读数据打包成 CSB 响应，内部例化寄存器文件 |
| [vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v) | 自动生成的寄存器文件：4 个寄存器的地址译码、MASK 触发器、读多路选择 |
| [vmod/nvdla/glb/NV_NVDLA_GLB_ic.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_ic.v) | 中断控制器：聚合 8 引擎 done、维护 status 触发器、按 mask 计算 `core_intr`、跨域同步 |
| [vmod/nvdla/glb/NV_NVDLA_GLB_fc.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_fc.v) | 故障控制器（fault controller）：应答 fault 配置空间访问，当前实现为回 0 占位 |
| [vmod/nvdla/top/NV_NVDLA_partition_o.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_o.v) | 在 partition_o 中例化 `u_NV_NVDLA_glb`，把各引擎 done 信号与 `core_intr` 接到顶层 |
| [vmod/nvdla/cacc/NV_NVDLA_CACC_delivery_buffer.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_delivery_buffer.v) | CACC 完成信号的产生源头：用 `intr_sel` 把 `cacc_done` 拆成 group0/group1 两路中断 |
| [vmod/nvdla/car/NV_NVDLA_sync3d_c.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/car/NV_NVDLA_sync3d_c.v) | 单 bit 跨时钟域同步器（3D 同步链），用于把 `core_intr` 从 core 域送到 falcon 域 |

> 提醒：根据 u1-l5，GLB 并不在某个「配置分区」里，而是位于「中央枢纽」partition_o——CSB 口、两组 AXI memif、中断线 `dla_intr` 都从 partition_o 出入，`core_intr` 就是其中那根中断线在 GLB 内部的名字。

## 4. 核心概念与源码讲解

### 4.1 GLB 子模块分工

#### 4.1.1 概念说明

GLB 不是一个单体模块，而是一个由四个文件组成的小子系统。按数据流向可以这样分：

- **GLB_csb**：CSB 总线的「门卫」。CPU 的配置请求先到它这里，它把 63 位的 CSB 请求包拆成地址、数据、读写标志，转成统一的 `reg_offset/reg_wr_en/reg_wr_data` 接口喂给寄存器文件；读回来时再把数据打包成 CSB 响应。
- **GLB_CSB_reg**：自动生成的寄存器文件（由 SystemRDL 经 Ordt 生成，见 u8-l2）。它只负责「地址 → 寄存器」的译码、MASK 触发器的存储、读数据的多路选择。注意：**它不存储 status/set**，这两类寄存器的触发器被刻意留给 GLB_ic 实现（生成器注释 `to be implemented outside`）。
- **GLB_ic**：中断控制器（interrupt controller），是 GLB 的核心。它接收 8 个引擎的 done 信号、维护 16 个 status 触发器、按 mask 把未屏蔽的 status OR 起来产生 `core_intr`，并做跨时钟域同步。
- **GLB_fc**：故障控制器（fault controller）。它接的是另一条 CSB 子通路 `csb2gec`（gec = generic error control），当前实现是「永远就绪、读回 0、无错误」的占位，为将来扩展 fault 寄存器留位置。

> 为什么把 status 寄存器放在 ic 里、而不放在自动生成的 reg 文件里？因为 status 既会被硬件 done 信号置位、又会被 CPU 写 1 清除，是「软硬件都能改」的双源寄存器，自动生成器不便表达，所以留给手写的 ic 统一处理，避免双写冲突。

#### 4.1.2 核心流程

GLB 顶层 `NV_NVDLA_glb.v` 的组装流程：

1. 声明 8 个引擎的 `*_2glb_done_intr_pd[1:0]` 输入（每个引擎 2 位，对应两个影偶组）。
2. 声明两条 CSB 配置通路：`csb2glb_*`（主配置，给 csb/ic/reg）与 `csb2gec_*`（fault 配置，给 fc）。
3. 用一组 `wire` 把各子模块的 mask/status 信号连成网：mask 由 csb→reg 产生，status 由 ic 产生，二者在 ic 内部汇合。
4. 例化 `u_csb`、`u_fc`、`u_ic` 三个子模块；`u_csb` 内部再例化 `u_reg`。

数据流分两条主线：

- **配置线**：`csb2glb_req` → GLB_csb 拆包 → GLB_CSB_reg 译码写 mask（或产生 set/clear trigger）→ mask 输出回送给 ic。
- **中断线**：8×`done_intr_pd` → ic 聚合成 `done_source` → 与 mask 运算产生 `core_intr_w` → 寄存 → 跨域同步 → `core_intr` 输出。

#### 4.1.3 源码精读

GLB 顶层的端口与子模块例化。注意 8 个 done 中断输入与唯一的 `core_intr` 输出：

[NV_NVDLA_glb.v:11-37](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_glb.v#L11-L37) —— 模块端口：输入 8 个引擎的 `*_2glb_done_intr_pd`、两条 CSB 配置通路、双时钟与复位；输出唯一的 `core_intr` 与两组 CSB 响应。

每个 done 中断都是 2 位（影偶两组），见输入声明：

[NV_NVDLA_glb.v:59-73](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_glb.v#L59-L73) —— `input [1:0] sdp2glb_done_intr_pd;` 等 8 个引擎，每个 2 位。

三个子模块的例化位置：

[NV_NVDLA_glb.v:125-168](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_glb.v#L125-L168) —— 例化 `u_csb`（CSB 接口），把 `csb2glb_req` 接进去、把各引擎的 `*_done_mask0/1` 输出拉出、`*_done_status0/1` 输入接回。

[NV_NVDLA_glb.v:173-185](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_glb.v#L173-L185) —— 例化 `u_fc`（故障控制器），只接 `csb2gec_*` 这条 fault 配置通路与双时钟。

[NV_NVDLA_glb.v:190-239](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_glb.v#L190-L239) —— 例化 `u_ic`（中断控制器），8 个引擎 done 输入、mask 输入、status 输出、`core_intr` 输出全在这里汇合。注意 `.req_wdat(req_wdat[21:0])` 只传低 22 位给 ic，因为只有 [21:0] 是有意义的中断位。

GLB_csb 把 CSB 请求包拆解为统一寄存器接口：

[NV_NVDLA_GLB_csb.v:204-210](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_csb.v#L204-L210) —— 从 63 位 `req_pd` 拆出 `req_addr`、`req_wdat`、`req_write`、`req_nposted`。

[NV_NVDLA_GLB_csb.v:257-259](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_csb.v#L257-L259) —— `reg_offset = {req_addr[9:0], 2'b0}`（字地址左移两位成字节偏移，12 位覆盖 4 KB 配置空间），`reg_wr_en = req_vld & req_write`。

GLB_fc 的「永远就绪、回 0」占位实现：

[NV_NVDLA_GLB_fc.v:60-95](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_fc.v#L60-L95) —— `csb2gec_req_prdy=1`，读数据 `rresp_rdat=0`、错误 `rresp_error=0`，只对写与 nposted 读产生响应。

#### 4.1.4 代码实践

**实践目标**：确认 GLB 子系统的四文件层次关系。

**操作步骤**：

1. 打开 `NV_NVDLA_glb.v`，确认它只例化 `u_csb`、`u_fc`、`u_ic` 三个实例（没有直接例化 `u_reg`）。
2. 打开 `NV_NVDLA_GLB_csb.v`，找到 `NV_NVDLA_GLB_CSB_reg u_reg (` 这一行，确认寄存器文件是被 csb 包了一层。
3. 在 `NV_NVDLA_glb.v` 里数一下 `*_2glb_done_intr_pd` 输入共有几个，应是 8 个。

**需要观察的现象**：四文件构成两层例化（glb→{csb,fc,ic}，csb→reg），mask 信号从 reg 流向 ic，status 信号从 ic 流向 reg（供读回）。

**预期结果**：得到一张「glb 顶层 → u_csb/u_fc/u_ic → u_csb 内部 u_reg」的层次图，mask 与 status 在 reg 与 ic 之间双向流动。

### 4.2 done 中断聚合网络

#### 4.2.1 概念说明

NVDLA 有 8 类会喊「完成」的引擎：`sdp`、`cdp`、`pdp`、`bdma`、`rubik`、`cdma_dat`（特征图取数）、`cdma_wt`（权重取数）、`cacc`（累加器）。注意 CDMA 被拆成 `cdma_dat` 与 `cdma_wt` 两路——它内部同时搬运特征图和权重，两路各自独立报完成。卷积核心里的 CSC、CMAC 不直接报中断，它们的完成由下游 CACC 统一汇报。

每个引擎又因为影偶机制（u2-l3）同时存在两个组：引擎跑 group0 时，CPU 在预装 group1；group0 完成时引擎拉 `done_intr_pd[0]`，group1 完成时拉 `done_intr_pd[1]`。所以 8 类引擎 × 2 组 = **16 个独立的中断源**。GLB_ic 把这 16 个源拼成一根 16 位向量 `done_source[15:0]`，后续所有 mask/set/status 都按这 16 位一一对应。

#### 4.2.2 核心流程

`done_source` 的位拼接顺序（从低位到高位）：

| 位 | 引擎 | 组 | 位 | 引擎 | 组 |
| --- | --- | --- | --- | --- | --- |
| 0 | sdp | 0 | 8 | rubik | 0 |
| 1 | sdp | 1 | 9 | rubik | 1 |
| 2 | cdp | 0 | 10 | cdma_dat | 0 |
| 3 | cdp | 1 | 11 | cdma_dat | 1 |
| 4 | pdp | 0 | 12 | cdma_wt | 0 |
| 5 | pdp | 1 | 13 | cdma_wt | 1 |
| 6 | bdma | 0 | 14 | cacc | 0 |
| 7 | bdma | 1 | 15 | cacc | 1 |

引擎侧的 2 位编码（以 CACC 为例）：

```
cacc_done_intr_w[0] = cacc_done & ~intr_sel   // group0 完成
cacc_done_intr_w[1] = cacc_done & intr_sel    // group1 完成
```

`intr_sel` 是引擎当前刚完成的那一组的索引（0 或 1）。这样无论哪组完成，都只置对应那一 bit，绝不会两位同时拉起——ic 里也专门加了断言禁止「同周期两组都报完成」。

#### 4.2.3 源码精读

ic 把 8 个引擎的 2 位 done 拼成 16 位 `done_source`：

[NV_NVDLA_GLB_ic.v:164-170](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_ic.v#L164-L170) —— `done_source <= {cacc[1:0], cdma_wt[1:0], cdma_dat[1:0], rubik[1:0], bdma[1:0], pdp[1:0], cdp[1:0], sdp[1:0]};`，注意拼接顺序决定了上表的位映射，`sdp` 在最低位。

CACC 侧产生 2 位 done 的源头：

[NV_NVDLA_CACC_delivery_buffer.v:1530-1531](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_delivery_buffer.v#L1530-L1531) —— 用 `intr_sel` 把一个 `cacc_done` 拆成 group0/group1 两路，证实了「2 位 = 2 个影偶组」。

[NV_NVDLA_CACC_delivery_buffer.v:1545-1549](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_delivery_buffer.v#L1545-L1549) —— 寄存后输出 `cacc2glb_done_intr_pd`。

ic 里禁止「同周期两组同时完成」的断言（以 CACC 为例，其余引擎同理）：

[NV_NVDLA_GLB_ic.v:825-829](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_ic.v#L825-L829) —— `nv_assert_never ... "Error! CACC sends two interrupts at same cycle!"`，条件 `cacc2glb_done_intr_pd == 3'h3`（即 2'b11）。这与影偶「同一时刻只有一组在跑」的约束一致。

partition_o 中 GLB 的例化与条件连接：

[NV_NVDLA_partition_o.v:2488-2511](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_o.v#L2488-L2511) —— sdp/cdma_wt/cdma_dat/cacc 直连；cdp/pdp/bdma/rubik 用 `#ifdef NVDLA_*_ENABLE` 条件连接，未启用时接 `2'd0`。可见这 4 个后处理/搬运引擎是可裁剪的，而卷积核心的 cacc 不可裁剪。

#### 4.2.4 代码实践

**实践目标**：核对 16 位 `done_source` 的位映射表。

**操作步骤**：

1. 在 `NV_NVDLA_GLB_ic.v` 第 168 行的 `done_source` 拼接表达式里，从最低位往高位数，写下每一位对应的 `{引擎, 组}`。
2. 与本讲 4.2.2 的表格逐位比对。
3. 在 `NV_NVDLA_GLB_CSB_reg.v` 的 mask 字段装配表达式（第 183 行）里做同样比对，确认 mask 位与 done_source 位**完全对齐**。

**需要观察的现象**：两个文件里的位顺序一致，且中间都有一段 6 位的保留间隔（rubik 之后、cdma_dat 之前）。

**预期结果**：位映射表完全对齐——这是后续「mask 按 done_source 同位屏蔽」成立的前提。

**待本地验证**：若你修改了 spec 的引擎裁剪宏，位映射可能变化，需重新核对。

#### 4.2.5 小练习与答案

**练习 1**：为什么 CDMA 占了 `done_source` 的两位（cdma_dat、cdma_wt），而 CACC 只占一个引擎名？

**答案**：CDMA 内部同时搬特征图（dat）和权重（wt）两路数据，两路各自独立报完成，所以拆成两个中断源；CACC 是单一累加器，只在整批卷积算完时报一次完成，只是再按影偶组拆成 group0/group1 两 bit。

**练习 2**：`done_intr_pd[1:0] == 2'b11` 为何被断言禁止？

**答案**：影偶机制保证同一时刻只有一组在运行，完成时只该置位当前组对应的 bit。两位同时为 1 意味着两组同周期完成，违反影偶串行运行约束，属于设计错误。

### 4.3 mask / set / status 寄存器机制

#### 4.3.1 概念说明

GLB 的中断寄存器空间只有 4 个 32 位寄存器，却管着 16 个中断源，靠的是「按位对应」：

| 偏移 | 寄存器 | 语义 |
| --- | --- | --- |
| 0x0 | `NVDLA_GLB_S_NVDLA_HW_VERSION_0` | 只读，硬件版本号（major=0x31, minor=0x3030） |
| 0x4 | `NVDLA_GLB_S_INTR_MASK_0` | 读写，**写 1 屏蔽**该位中断（mask=1 不放行） |
| 0x8 | `NVDLA_GLB_S_INTR_SET_0` | 只写，**写 1 置位**对应 status（软件模拟中断用） |
| 0xc | `NVDLA_GLB_S_INTR_STATUS_0` | 读得到当前 pending；**写 1 清除**（W1C，中断应答） |

最容易踩坑的是 mask 的极性。看 ic 的核心表达式：

\[
\text{core\_intr\_w} = \bigvee_{i=0}^{15} \left( \neg\,\text{mask}_i \;\wedge\; \text{status}_i \right)
\]

也就是 `~mask & status`——**mask=0 表示放行，mask=1 表示屏蔽**。复位时所有 mask=0，即全部中断默认开启。这与「写 1 使能」的直觉相反，读代码时要特别注意那个取反 `~`。

status 是「软硬件双源」寄存器，每个 status bit 的下一拍值由三路决定：

\[
\text{status}_i^{next} =
\begin{cases}
1 & \text{if } (\text{done\_set}_i \vee \text{done\_source}_i) \\
0 & \text{if } \text{done\_wr\_clr}_i \\
\text{status}_i & \text{otherwise}
\end{cases}
\]

即：硬件 done 来了或软件写 SET，就置 1；软件写 STATUS 该位为 1，就清 0；否则保持。注意「置位优先于清零」——同周期既 set 又 clear 时，断言会报错禁止（见 4.4.3）。

#### 4.3.2 核心流程

一次「软件应答中断」的完整流程：

1. 某引擎完成 → `done_source[i]` 拉起 → 下一拍 `status[i]` 被置 1。
2. `core_intr_w` 因 `~mask[i] & status[i]` 拉起 → `core_intr` 输出（见 4.4）。
3. CPU 进中断，读 `INTR_STATUS`（0xc），看到第 i 位为 1，知道是哪个引擎的哪组完成。
4. CPU 处理完后，向 `INTR_STATUS` 写「第 i 位为 1」的数据 → 产生 `done_wr_clr[i]` → 下一拍 `status[i]` 清 0。
5. 若 CPU 暂时不想被某源打扰，向 `INTR_MASK`（0x4）写对应位为 1 屏蔽（status 仍会置位，只是不传到 core_intr）。
6. 若 CPU 想自测中断通路，向 `INTR_SET`（0x8）写对应位为 1 → 产生 `done_set[i]` → status 置位，等效于硬件完成。

#### 4.3.3 源码精读

寄存器地址译码（4 个寄存器的写使能）：

[NV_NVDLA_GLB_CSB_reg.v:176-179](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v#L176-L179) —— `INTR_MASK_0_wren`（0x4）、`INTR_SET_0_wren`（0x8）、`INTR_STATUS_0_wren`（0xc）、`HW_VERSION_0_wren`（0x0）。

三个中断寄存器的位装配（注意 mask/set/status 三者位布局完全一致，且中间有 6 位保留间隔）：

[NV_NVDLA_GLB_CSB_reg.v:183-185](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v#L183-L185) —— MASK/SET/STATUS 三行的拼接结构相同：`{10'b0, cacc×2, cdma_wt×2, cdma_dat×2, 6'b0, rubik×2, bdma×2, pdp×2, cdp×2, sdp×2}`。

HW_VERSION 常量：

[NV_NVDLA_GLB_CSB_reg.v:181-186](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v#L181-L186) —— `major=8'h31`、`minor=16'h3030`，读回 `{8'b0, minor, major}`。

写 SET/STATUS 寄存器时产生两个 trigger（注意名字里的 `_0` 指寄存器实例 INTR_SET_0，不是影偶组 0，它实际触发全部 16 位）：

[NV_NVDLA_GLB_CSB_reg.v:188-189](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v#L188-L189) —— `sdp_done_set0_trigger = INTR_SET_0_wren`、`sdp_done_status0_trigger = INTR_STATUS_0_wren`。

MASK 触发器（以 cacc 为例，复位为 0 即默认放行）：

[NV_NVDLA_GLB_CSB_reg.v:252-260](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v#L252-L260) —— `cacc_done_mask0 <= reg_wr_data[20]`、`cacc_done_mask1 <= reg_wr_data[21]`，即 mask 寄存器第 20/21 位控制 cacc 两组。

ic 把 trigger 还原成 16 位的 set / clr 向量（跳过中间 6 位保留段）：

[NV_NVDLA_GLB_ic.v:150-162](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_ic.v#L150-L162) —— `done_wr_clr = status0_trigger ? {req_wdat[21:16], req_wdat[9:0]} : 14'b0;`，`done_set` 同理用 `set0_trigger`。这里 `{req_wdat[21:16], req_wdat[9:0]}` 恰好跳过了 `req_wdat[15:10]` 这段保留位。

status 触发器的三路选择（以 cacc group0 为例，其余 15 位结构相同）：

[NV_NVDLA_GLB_ic.v:483-500](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_ic.v#L483-L500) —— `cacc_done_status0_w = (done_set[14] | done_source[14]) ? 1 : (done_wr_clr[14]) ? 0 : cacc_done_status0;`，完美对应 4.3.1 的三段式公式。

#### 4.3.4 代码实践

**实践目标**：列出所有 `*_done_mask0` 引擎并验证 mask 极性。

**操作步骤**：

1. 在 `NV_NVDLA_GLB_CSB_reg.v` 第 183 行的 `nvdla_glb_s_intr_mask_0_out` 拼接表达式里，从 bit0 到 bit15 列出所有 `*_done_mask0` 字段（共 8 个，每组一个 group0）。
2. 找到对应的 `*_done_mask0 <= reg_wr_data[?]` 赋值（第 242 行起），记下每个的位号。
3. 在 `NV_NVDLA_GLB_ic.v` 第 559 行起的 `core_intr_w` 表达式里，确认每一项都是 `~xxx_done_mask0 & xxx_done_status0` 形式。

**需要观察的现象**：mask 字段名、位号、core_intr 表达式三者一一对应；每项都有取反 `~`。

**预期结果**：得到一张表 `sdp_done_mask0=bit0, cdp_done_mask0=bit2, pdp_done_mask0=bit4, bdma_done_mask0=bit6, rubik_done_mask0=bit8, cdma_dat_done_mask0=bit16, cdma_wt_done_mask0=bit18, cacc_done_mask0=bit20`，且确认 mask=1 屏蔽、mask=0 放行。

#### 4.3.5 小练习与答案

**练习 1**：复位后 CPU 不写 MASK 寄存器，某个引擎完成会触发 `core_intr` 吗？

**答案**：会。复位时所有 `mask=0`，即 `~mask=1` 放行，只要 status 被硬件 done 置位，`core_intr` 就会拉起。MASK 默认全开。

**练习 2**：CPU 想暂时屏蔽 BDMA 的中断但不丢失「BDMA 完成过」这个事实，该怎么做？

**答案**：向 `INTR_MASK`（0x4）的 bit6/bdpa7 写 1 屏蔽 bdma group0/1。status 仍会被 done 置位并保持，只是不传到 core_intr；之后清 mask 即可看到 pending 的 status。

**练习 3**：为什么 SET 寄存器读回总是 0？

**答案**：SET 是只写（write-only）寄存器，csb 里把所有 `*_done_set*` 输入接 0（`NV_NVDLA_GLB_csb.v:145-160`），且 reg 文件不为 set 生成触发器（注释 `to be implemented outside`）。写 SET 只产生一个一周期 trigger 去置位 status，本身不存值，故读回 0。

### 4.4 core_intr 聚合与跨时钟域同步

#### 4.4.1 概念说明

16 个 status 经 mask 过滤后，OR 到一起就是总中断 `core_intr_w`。但还有两件事要处理：

1. **寄存一拍**：`core_intr_w` 是组合逻辑（直接 OR），需要打一拍 `core_intr_d` 稳定时序。
2. **跨时钟域**：status 与 mask 都在 `nvdla_core_clk` 域，但中断要送给 CPU/falcon 侧的 `nvdla_falcon_clk` 域。单 bit 信号跨异步时钟域，标准做法是用同步器（synchronizer）链——本讲用的是 `NV_NVDLA_sync3d_c`，一个带 DFT（可测性设计）钳位与仿真随机化的 3 级同步链。

同步器存在的根本原因：如果直接把 core 域的信号接到 falcon 域的触发器，当信号恰好在 falcon 采样沿附近翻转时，触发器可能进入亚稳态（metastable），输出在 0/1 之间悬置不定，可能被下游误判。同步链用多级触发器逐级「等」足够长时间，让亚稳态收敛到稳定 0 或 1 的概率极低（满足 MTBF 指标）。

#### 4.4.2 核心流程

中断从产生到输出的完整链路：

```
engine done_intr_pd[1:0]                (engine core clk)
   │
   ▼  ic: done_source[15:0] 寄存
done_source[i]
   │
   ▼  ic: status 三路选择寄存
status[i]  ── mask[i] ──> ~mask & status
   │
   ▼  ic: 16 路 OR (组合)
core_intr_w  (组合, core clk)
   │
   ▼  ic: 寄存一拍
core_intr_d  (core clk)
   │
   ▼  NV_NVDLA_sync3d_c: 3 级同步链
core_intr    (falcon clk) ──> partition_o ──> 顶层 dla_intr
```

关键不变式：同步器只用于「电平」类信号。这里 `core_intr` 是电平有效的——只要任一未屏蔽的 status 还为 1，`core_intr` 就持续为高，直到 CPU 写 STATUS 清掉它。电平信号过同步器是安全的（不怕偶尔多一拍少一拍，因为电平会持续）。

#### 4.4.3 源码精读

`core_intr_w` 的 16 路 OR（注意每项都是 `~mask & status`）：

[NV_NVDLA_GLB_ic.v:525-575](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_ic.v#L525-L575) —— 16 个 `(~xxx_done_mask0 & xxx_done_status0) | (~xxx_done_mask1 & xxx_done_status1)` 项 OR 在一起，覆盖全部 8 引擎 × 2 组。

寄存一拍到 core 域：

[NV_NVDLA_GLB_ic.v:576-582](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_ic.v#L576-L582) —— `core_intr_d <= core_intr_w`，时钟是 `nvdla_core_clk`。

跨域同步到 falcon 域：

[NV_NVDLA_GLB_ic.v:584-589](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_ic.v#L584-L589) —— 例化 `NV_NVDLA_sync3d_c u_sync_core_intr`，输入 `core_intr_d`（core 域）、时钟接 `nvdla_falcon_clk`、复位接 `nvdla_falcon_rstn`，输出 `core_intr`（falcon 域）。

同步器内部结构：

[NV_NVDLA_sync3d_c.v:11-16](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/car/NV_NVDLA_sync3d_c.v#L11-L16) —— 同步器端口 `clk/rst/sync_i/sync_o`。

[NV_NVDLA_sync3d_c.v:89-94](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/car/NV_NVDLA_sync3d_c.v#L89-L94) —— 内部例化 `sync3d_c_ppp`（3 级同步链本体），前面还串了 DFT xclamp 多路选择与仿真用的随机化逻辑（用于在仿真中注入亚稳态、验证同步链鲁棒性）。

禁止「同周期既置位又清零」的断言：

[NV_NVDLA_GLB_ic.v:638-640](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_ic.v#L638-L640) —— `nv_assert_never ... "Error! Set and clear interrupt concurrently!"`，条件 `sdp_done_status0_trigger & sdp_done_set0_trigger`，防止软件同周期既写 SET 又写 STATUS 造成 status 次态不确定。

#### 4.4.4 代码实践

**实践目标**：跟踪一次卷积完成后，中断从 CACC 到 `core_intr` 输出的完整路径。

**操作步骤**：

1. 从 [NV_NVDLA_CACC_delivery_buffer.v:1530-1531](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cacc/NV_NVDLA_CACC_delivery_buffer.v#L1530-L1531) 出发：假设 CACC 的 group0 完成（`intr_sel=0`），则 `cacc_done_intr_w[0]=1`。
2. 跟到 `cacc2glb_done_intr_pd[1:0]` → partition_o → `u_NV_NVDLA_glb.cacc2glb_done_intr_pd`。
3. 在 `NV_NVDLA_GLB_ic.v:168` 确认它落到 `done_source[14]`（cacc group0）。
4. 在 `NV_NVDLA_GLB_ic.v:483-500` 确认 `done_source[14]` 置位 `cacc_done_status0`。
5. 在 `NV_NVDLA_GLB_ic.v:573` 确认 `(~cacc_done_mask0 & cacc_done_status0)` 进入 `core_intr_w`（复位时 mask0=0，故放行）。
6. 经 `core_intr_d`（L580）→ `NV_NVDLA_sync3d_c`（L584）→ `core_intr` 输出。
7. 在 partition_o 顶层（[NV_NVDLA_partition_o.v:2487](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_o.v#L2487)）确认 `core_intr` 是 partition_o 的输出，最终接到顶层 `dla_intr`。

**需要观察的现象**：信号从 engine core 域出发，在 ic 内部全程 core 域，最后只在 `core_intr` 一处跨到 falcon 域；跨域点是 `u_sync_core_intr`。

**预期结果**：画出一条「CACC done → done_source[14] → cacc_done_status0 → core_intr_w → core_intr_d → sync3d_c → core_intr → dla_intr」的调用链，标注每段所在时钟域。

**待本地验证**：若开波形（`DUMP=1 DUMPER=VERDI`，见 u1-l4），可在 `debussy.fsdb` 里抓 `u_NV_NVDLA_glb.u_ic.core_intr_d` 与 `core_intr`，观察同步链带来的 2~3 拍 falcon 域延迟。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `core_intr` 用电平有效而不是脉冲有效？

**答案**：电平有效保证「只要 CPU 没应答清 status，中断就持续拉高」，不会因为同步链偶尔延迟而漏报。脉冲过同步链有被采样丢失的风险，电平不会。

**练习 2**：如果 CPU 收到 `core_intr` 后读 `INTR_STATUS` 发现全 0，可能是什么原因？

**答案**：最可能是该中断源被 MASK 屏蔽了——status 仍为 1 但被 `~mask` 挡住，所以 `core_intr` 拉起；但 CPU 读的是 STATUS（pending 位），若 status 已被别处清掉而新的又没来，也可能读到 0。更合理的排查是同时读 MASK 与 STATUS：pending 看 STATUS，被屏蔽的源看 MASK。

## 5. 综合实践

**任务**：编写一份「GLB 中断使能与应答」的伪 CSB 配置序列，并画出 CACC 完成后的中断传播框图。

**背景**：假设你要在 SoC 集成中用 GLB 管理 CACC 的卷积完成中断。CACC 的基址与 GLB 的基址由 csb_master 的地址译码决定（GLB 是 csb_master 的一个客户端）。GLB 寄存器组内偏移：MASK=0x4、SET=0x8、STATUS=0xc、HW_VERSION=0x0。

**要求**：

1. **读版本号**：向 GLB 基址 +0x0 发一次 CSB 读，预期读到 `major=0x31, minor=0x3030`（即数据形如 `0x00003030_0031` 的高低字节排列，按 `{8'b0, minor[15:0], major[7:0]}` 拼装）。请根据 [NV_NVDLA_GLB_CSB_reg.v:186](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v#L186) 算出确切 32 位读回值。
2. **只放行 CACC group0**：写 MASK（0x4），把 cacc_done_mask0（bit20）清 0 放行，其余可置 1 屏蔽（或按需）。给出要写的 32 位数据。
3. **启动卷积并等中断**：CACC group0 完成后，`cacc2glb_done_intr_pd[0]` 拉起 → `done_source[14]` → `cacc_done_status0` → `core_intr`。
4. **应答**：CPU 读 STATUS（0xc）看到 bit20=1，向 STATUS 写「bit20=1」清除（W1C）。

**交付物**：

- 一份伪代码序列（`write_reg(GLB+0x4, data)` / `read_reg(GLB+0x0)` 等形式，参考 u1-l4 的 trace 命令风格）。
- 一张中断传播框图，标注：CACC delivery_buffer → `cacc2glb_done_intr_pd[0]` → ic 的 `done_source[14]` → `cacc_done_status0` → `core_intr_w`（与 mask 运算）→ `core_intr_d` → `sync3d_c` → `core_intr`（falcon 域）→ `dla_intr`。每段标注时钟域（core / falcon）。

**预期结果**：

- 读版本号读回值 = `{8'b0, 16'h3030, 8'h31}` = `0x00303031`。
- 写 MASK 使 bit20=0、其余位按需（如全 1 仅放行 cacc0）= `0xFFEFFFFF`（bit20 清 0，其余 31 位为 1）。
- 应答写 STATUS = `0x00100000`（bit20=1，W1C 清 cacc_done_status0）。

> 待本地验证：上述位号与数据依赖本仓库 nvdlav1 固定配置；若引擎裁剪宏变化，位映射需按 `NV_NVDLA_GLB_CSB_reg.v:183-185` 重新推导。

## 6. 本讲小结

- GLB 是 NVDLA 的「中断汇总台」，由四个文件组成：`NV_NVDLA_glb.v` 顶层例化 `u_csb`/`u_fc`/`u_ic`，`u_csb` 内部再例化自动生成的 `u_reg`（`NV_NVDLA_GLB_CSB_reg.v`）。
- 8 类引擎（sdp/cdp/pdp/bdma/rubik/cdma_dat/cdma_wt/cacc）× 2 个影偶组 = 16 个中断源，被 ic 拼成 `done_source[15:0]`；mask/set/status 三类寄存器都按这 16 位一一对应。
- 三类中断寄存器语义：MASK（0x4，写 1 屏蔽、复位全 0 即默认放行）、SET（0x8，写 1 置位、只写读回 0）、STATUS（0xc，读得到 pending、写 1 清除 W1C）；另有 HW_VERSION（0x0，major=0x31/minor=0x3030）。
- status 是软硬件双源寄存器：硬件 `done_source` 置位、软件写 STATUS 清除、软件写 SET 也可置位；reg 文件不存 status，留给 ic 手写以避免双写冲突。
- `core_intr = OR(~mask & status)`，先在 core 域寄存一拍，再经 `NV_NVDLA_sync3d_c` 三级同步链跨到 falcon 域输出，最终接顶层 `dla_intr`。
- 断言守护两条不变式：单引擎不得同周期报两组完成（`done_intr_pd==2'b11` 禁止）、不得同周期既 set 又 clear status。

## 7. 下一步学习建议

- **进入卷积主流水线**：本讲讲清了「完成」如何上报，接下来按 u3-l1（卷积主流水线总览）进入 CDMA→CBUF→CSC→CMAC→CACC 的数据通路，看 CACC 是如何算完一层并产生本讲追踪的 `cacc_done`。
- **横向对照其他引擎的 done**：可顺手读 `vmod/nvdla/sdp`、`vmod/nvdla/bdma`、`vmod/nvdla/rubik` 中各自 `*2glb_done_intr_pd` 的产生逻辑，验证它们与 CACC 一样遵循「2 位 = 2 影偶组、不同周期报完成」的约定。
- **寄存器生成的源头**：本讲的 `NV_NVDLA_GLB_CSB_reg.v` 是自动生成的，u8-l2 会讲 SystemRDL（`spec/manual/test.rdl`）经 Ordt 生成 RTL/RAL/cmod 寄存器模型的多后端流程，届时可回看本讲印证生成器「哪些字段生成触发器、哪些留给手写」的规则。
- **跨时钟域同步原语**：若对 `NV_NVDLA_sync3d_c` 的 DFT 钳位与仿真随机化感兴趣，可预习 u6-l1（时钟域、复位与时钟门控）与 u6-l2（FIFO 与 vlibs 库原语），那里系统讲解 sync3d 系列与库原语复用约定。
