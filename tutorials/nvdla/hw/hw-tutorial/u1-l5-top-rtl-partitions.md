# 顶层 RTL：NV_nvdla.v 与分区结构

## 1. 本讲目标

学完本讲，你应当能够：

- 读懂 `NV_nvdla` 顶层模块的对外端口：CSB 配置口、两组 AXI 存储接口（`core2dbb` 与 `core2cvsram`）、中断、时钟/复位以及电源分区信号。
- 说清楚顶层把整个加速器拆成了哪几个 **partition（分区）** 实例，每个分区里实际装着哪个/哪些引擎。
- 建立「**端口 → 分区**」的归属直觉：给定一个对外端口，能立刻判断它连到哪个分区实例、进而连到哪个内部引擎。
- 纠正一个常见的简化说法——本文会反复用源码证明：**配置（csb_master/glb）和存储接口（mcif/cvif）并不在「a 分区」或「m 分区」，而是集中在 partition_o 这个「中央枢纽」里**。

> 阅读原则（承接 u1-l1）：文档与口口相传的说法仅供参考，**一切以仓库源码为准**。本讲所有结论都给出了行号和永久链接，你可以逐条核对。

## 2. 前置知识

本讲是「入门单元」的收尾，默认你已经具备（承接 u1-l1～u1-l4）：

- **RTL / Verilog 模块例化**：上层模块用 `module_name instance_name ( .port(signal), ... );` 把下层模块「接线」起来。本讲顶层的「分区」就是 6 个这样的例化实例。
- **CSB 配置总线**：NVDLA 内部统一的寄存器读写总线，是 CPU 编程各引擎的唯一入口（详见 u2-l1）。顶层暴露的 `csb2nvdla_*` 就是这条总线的对外端口。
- **AXI 总线五个通道**：写地址 `AW`、写数据 `W`、写响应 `B`、读地址 `AR`、读数据 `R`。顶层有两组 AXI：`nvdla_core2dbb_*`（接主存 DBB）和 `nvdla_core2cvsram_*`（接片上 CVSRAM）。
- **trace-player 仿真**：u1-l4 跑的 `simv` 顶层例化的 DUT 就是本讲的 `NV_nvdla`。
- **时钟域 / 复位 / 电源岛**：SoC 里常把不同功能块放在不同时钟域、不同电源开关（power island）下，以降低功耗。NVDLA 的「分区」本质上就是 **时钟域 + 电源岛 + 复位域** 的边界。

一个心智模型先记住：`NV_nvdla` 像一块主板，对外只伸出几组「插座」（端口），对内把所有功能芯片（引擎）插在若干块「子板」（分区）上。本讲的任务就是把插座→子板→芯片的对应关系画清楚。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `vmod/nvdla/top/NV_nvdla.v` | 顶层模块，约 3300 行。前 ~180 行是端口声明，中间大量内部 `wire`，约 1165 行起依次例化 6 个分区。 |
| `vmod/nvdla/top/NV_NVDLA_partition_a.v` | partition_a：内部例化 **CACC（累加器）**。 |
| `vmod/nvdla/top/NV_NVDLA_partition_c.v` | partition_c：内部例化 **CDMA + CBUF + CSC**（卷积前段）。 |
| `vmod/nvdla/top/NV_NVDLA_partition_m.v` | partition_m：内部例化 **CMAC（乘加阵列）**；被例化两次（ma/cmac_a 与 mb/cmac_b）。 |
| `vmod/nvdla/top/NV_NVDLA_partition_p.v` | partition_p：内部例化 **SDP（单点后处理器）**。 |
| `vmod/nvdla/top/NV_NVDLA_partition_o.v` | partition_o：**中央枢纽**，例化 csb_master、glb、mcif、cvif、bdma、rubik、cdp、pdp、复位同步、obs 等。 |

> 注意：`apb2csb`（APB→CSB 桥）**不在** `NV_nvdla` 顶层里——顶层对外的 `csb2nvdla_*` 已经是 CSB 协议，`apb2csb` 桥位于 SoC 集成侧 / 测试平台侧（u2-l1 会详讲）。

## 4. 核心概念与源码讲解

### 4.1 NV_nvdla 顶层端口

#### 4.1.1 概念说明

`NV_nvdla` 是 NVDLA 对外暴露的「黑盒」边界。SoC 集成者（或仿真 testbench）只需要连这几组端口就能用上整个加速器：

- 一组 **CSB 配置口**（CPU 读写寄存器，启动引擎）。
- 两组 **AXI 存储接口**（搬特征图/权重/输出：`core2dbb` 接主存，`core2cvsram` 接片上 SRAM）。
- 一根 **中断线** `dla_intr`（引擎做完事通知 CPU）。
- 两组 **时钟**、**复位**、**测试/低功耗控制** 以及 **按分区的电源岛控制** `nvdla_pwrbus_ram_*_pd`。

#### 4.1.2 核心流程（端口分组）

```text
NV_nvdla 对外端口（按功能分组）
├─ 时钟/复位/控制 : dla_core_clk, dla_csb_clk, *reset*, test_mode, global_clk_ovr_on, tmc2slcg_disable_clock_gating
├─ CSB 配置口     : csb2nvdla_* (请求), nvdla2csb_* (响应)
├─ AXI 主存 DBB   : nvdla_core2dbb_{aw,w,b,ar,r}_*   (AW/W/B/AR/R 五通道)
├─ AXI 片上SRAM   : nvdla_core2cvsram_{aw,w,b,ar,r}_*
├─ 中断           : dla_intr
└─ 电源岛控制     : nvdla_pwrbus_ram_{c,ma,mb,p,o,a}_pd   ← 每个分区一个
```

注意最后那一组 `nvdla_pwrbus_ram_*_pd`：它有 `c / ma / mb / p / o / a` 六个，正好对应后面要讲的 **六个分区实例**（m 被拆成 ma、mb）。这从一个侧面证明：**分区 ≈ 电源岛 ≈ 独立的 RAM 电源开关**。

#### 4.1.3 源码精读

模块声明与端口列表从第 16 行开始：

[模块声明 NV_nvdla(...)](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L16-L86) —— 这 70 行是顶层的全部对外端口，注释里的 `//|< i` 表示输入、`//|> o` 表示输出。

CSB 配置口（请求 6 根 + 响应 3 根）：

[CSB 配置端口 csb2nvdla_* / nvdla2csb_*](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L24-L32) —— 一次 CSB 写需要 `valid/ready/addr(16位)/wdat(32位)/write/nposted`，读结果通过 `nvdla2csb_valid/data/wr_complete` 返回。

两组 AXI 存储接口（主存 DBB 与片上 CVSRAM）：

[AXI 主存接口 core2dbb 五通道](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L33-L55) 与 [AXI CVSRAM 接口 core2cvsram 五通道](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L56-L78) —— 每组都是标准 AXI 五通道；数据位宽 `wdata/rdata` 都是 **512-bit**，地址 **64-bit**，`awlen/arlen` 是 4-bit（突发的「拍数−1」）。

中断与电源岛控制：

[中断 dla_intr 与电源岛 nvdla_pwrbus_ram_*_pd](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L79-L85) —— 只有 **一根** 中断线 `dla_intr`，所有引擎的中断在内部聚合后从这里输出。

时钟归属在端口声明区用注释点明：

[时钟声明与归属注释](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L91-L92) —— `dla_core_clk` 驱动所有 AXI memif；`dla_csb_clk` 驱动 CSB 配置口。**CSB 与 memif 处于不同时钟域**，所以二者之间必须有跨时钟域同步（由 partition_o 承担，见 4.4）。

#### 4.1.4 代码实践

**目标**：用肉眼 / `grep` 数清顶层端口到底有哪几类、各属哪个时钟域。

**步骤**：

1. 打开 `NV_nvdla.v`，定位第 16 行 `module NV_nvdla (` 到第 86 行的 `);`。
2. 用编辑器搜索 `//|< i` 与 `//|> o`，分别统计输入、输出端口数量。
3. 搜索 `nvdla_core2dbb_` 与 `nvdla_core2cvsram_`，确认每组都正好有 `aw/w/b/ar/r` 五个通道的前缀。
4. 搜索 `nvdla_pwrbus_ram_`，确认有 `c / ma / mb / p / o / a` 六个电源岛输入。

**需要观察的现象 / 预期结果**：

- 你会发现 CSB 请求是 6 个信号、AXI 每组 5 通道、电源岛恰好 6 个。
- 端口列表里 **没有任何** `apb_*` 信号——印证了 apb2csb 桥在顶层之外。

> 本实践为「源码阅读型」，无需运行仿真；若想验证，可执行 `grep -c '//|< i' vmod/nvdla/top/NV_nvdla.v` 统计输入端口数。运行结果与你的手工计数是否一致，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`csb2nvdla_addr` 的位宽是多少？为什么这么窄？
**答**：16 位（[NV_nvdla.v:104](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L104)）。因为 CSB 是 **寄存器配置总线**，16 位地址按字寻址已足以覆盖全部引擎的寄存器空间，不需要像主存那样用 64 位地址。

**练习 2**：为什么顶层有两套几乎一样的 AXI 接口（`core2dbb` 与 `core2cvsram`）？
**答**：`core2dbb` 接片外/系统主存（DBB），容量大但延迟高；`core2cvsram` 接片上 CVSRAM，容量小但延迟低。引擎可按需把热数据放在 CVSRAM 加速访问（u4-l1/u4-l3 详讲）。

### 4.2 六大分区总览：端口归属与「命名真相」

#### 4.2.1 概念说明

你可能在别处见过这样一句简化口诀：「a=配置、c=卷积、m=存储接口、o=复位观测、p=后处理」。**这句话对 nvdlav1 这个仓库的源码是不准确的**，容易把人带偏。本节用源码给出真实的分区角色表，并指出一个最关键的事实：

> **partition_o 是「中央枢纽」**——配置总线 csb_master、全局控制 glb、两套存储接口 mcif/cvif，以及 bdma/rubik/cdp/pdp/复位/观测，**全部** 装在 partition_o 里。CSB 配置口和两组 AXI memif 这三组对外端口，**全都连到 partition_o**。

而 partition_a / partition_c / partition_m / partition_p 这四个「引擎分区」各自 **只装一个** 计算引擎。真实的助记如下：

| 实例 | 模块文件 | 内部例化的引擎 | 助记 |
|------|----------|----------------|------|
| `u_partition_a` | `NV_NVDLA_partition_a.v` | **CACC**（累加器） | A = **A**ccumulator |
| `u_partition_c` | `NV_NVDLA_partition_c.v` | **CDMA + CBUF + CSC**（卷积前段） | C = **C**onvolution 流水前段 |
| `u_partition_ma` | `NV_NVDLA_partition_m.v` | **CMAC**（乘加阵列，cmac_a 半） | M = **MAC** |
| `u_partition_mb` | `NV_NVDLA_partition_m.v` | **CMAC**（乘加阵列，cmac_b 半） | M = **MAC** |
| `u_partition_p` | `NV_NVDLA_partition_p.v` | **SDP**（单点后处理器） | P = **P**oint |
| `u_partition_o` | `NV_NVDLA_partition_o.v` | csb_master + glb + mcif + cvif + bdma + rubik + cdp + pdp + 复位同步 + obs | O = **O**thers（其余一切） |

注意 partition_m 这个模块文件被 **例化了两次**（ma 与 mb），分别对应 CMAC 阵列的两半（cmac_a / cmac_b）。

#### 4.2.2 核心流程（端口 → 分区归属）

```text
对外端口                       连到的分区实例        再到内部引擎
─────────────────────────────────────────────────────────────────
csb2nvdla_*  (CSB 请求)   ──┐
nvdla2csb_*  (CSB 响应)   ──┼──> u_partition_o ──> csb_master ──> 各引擎寄存器
core2dbb_*   (AXI 主存)   ──┤                    mcif
core2cvsram_*(AXI CVSRAM)──┤                    cvif
dla_intr     (中断)       ──┘                    glb(中断聚合) ──> core_intr

引擎间数据(卷积主流水) : u_partition_c(CDMA/CSC) ─ u_partition_m(CMAC) ─ u_partition_a(CACC) ─ u_partition_p(SDP)
```

这张表是本讲最重要的结论：**凡是「对外」的端口，几乎都落在 partition_o**；a/c/m/p 四个分区只管「埋头算」，对外的联络（配置、访存、中断）统一由 partition_o 转接。

#### 4.2.3 源码精读

顶层依次例化六个分区（顺序为 o → c → ma → mb → a → p），每个例化前都有醒目的注释横幅：

- [partition_o 例化](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L1162-L1165)（注释 `NVDLA Partition O` + `u_partition_o (`）
- [partition_c 例化](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L1921-L1924)
- [partition_ma 例化](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L2520-L2523) 与 [partition_mb 例化](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L2520-L2523)（同一个模块 `NV_NVDLA_partition_m`，两个实例）
- [partition_a 例化](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L3108-L3111)（注释 `NVDLA Partition A` 在第 3108-3110 行）
- [partition_p 例化](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L3158-L3161)
- [endmodule 收尾](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L3303)

> 说明：上面 ma/mb 共用一条分区例化段的引用，因为它们紧挨在一起（先 ma 后 mb）；a 与 p 的例化同样相邻。具体行号在 4.3/4.4 的精读中再精确给出。

#### 4.2.4 代码实践

**目标**：亲手验证「六个分区实例」与「每个分区内部装什么」两张表。

**步骤**：

1. 在 `NV_nvdla.v` 中搜索 `u_partition_`，列出全部实例名（应为 `u_partition_o/c/ma/mb/a/p` 共 6 个）。
2. 对每个分区 `.v` 文件，搜索顶层引擎例化（如 `NV_NVDLA_cacc u_NV_NVDLA_cacc`），确认该分区里到底装了哪个引擎。

**需要观察的现象 / 预期结果**：与 4.2.1 的表完全一致——a 装 cacc、c 装 cdma/cbuf/csc、m 装 cmac、p 装 sdp、o 装一堆基础设施。如果有人告诉你「m 是存储接口」，你现在能用这条证据反驳他。**待本地验证**。

#### 4.2.5 小练习与答案

**练习**：为什么 partition_m 要被例化两次（ma、mb），而其他分区只例化一次？
**答**：因为 CMAC 乘加阵列在物理上分成对称的两半（cmac_a 与 cmac_b），分别对应输入数据通路与权重通路（或两套并行的 MAC），每半是一个独立的电源岛（`nvdla_pwrbus_ram_ma_pd` 与 `_mb_pd`），所以用同一个 `partition_m` 模块例化两次，分别接到 `cmac_a` / `cmac_b` 信号组。

### 4.3 引擎分区：partition_c / partition_m / partition_a / partition_p

这四个分区各装一个/一组 **计算引擎**，是「埋头干活」的部分。它们之间通过顶层的内部 `wire` 串成卷积主流水（u3 单元详讲）。

#### 4.3.1 概念说明

- **partition_c（卷积前段）**：装 CDMA（取数）+ CBUF（缓冲）+ CSC（分发调度）。它从存储取特征图与权重，缓存后按节拍喂给 MAC。
- **partition_m ×2（MAC 阵列）**：装 CMAC，做大规模并行乘加。cmac_a 与 cmac_b 两半。
- **partition_a（累加器）**：装 CACC，把 CMAC 的部分和累加、叠加偏置、按精度交付结果。
- **partition_p（单点后处理）**：装 SDP，对 CACC 输出逐点做缩放/归一化/激活等元素级运算。

> 注意：PDP/CDP/Rubick 这些「后处理」引擎 **不在** partition_p，而在 partition_o（见 4.4）。所以「p = 后处理」也是不准确的简化——partition_p 只有 SDP。

#### 4.3.2 核心流程（数据在引擎分区间的流向）

```text
取数          缓冲/分发         乘加(两半)         累加           单点后处理
CDMA ──> CBUF ──> CSC ──────> CMAC_a ─┐
                       └──> CMAC_b ──┴──> CACC ──> SDP ──>(再到 partition_o 的 PDP/CDP/...)
```

这条链横跨 c / m / a / p 四个分区，靠顶层 `NV_nvdla.v` 里上千根内部 `wire`（如 `sc2mac_dat_a_*`、`mac_a2accu_*`、`cacc2sdp_*`）把它们点对点连起来。

#### 4.3.3 源码精读

**partition_c** 内部三件套（在 `NV_NVDLA_partition_c.v` 中）：

- [CDMA 例化](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_c.v#L1600-L1600) `NV_NVDLA_cdma u_NV_NVDLA_cdma (`
- [CBUF 例化](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_c.v#L1669-L1669) `NV_NVDLA_cbuf u_NV_NVDLA_cbuf (`
- [CSC 例化](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_c.v#L1698-L1698) `NV_NVDLA_csc u_NV_NVDLA_csc (`

**partition_m** 内部（在 `NV_NVDLA_partition_m.v` 中）：

- [CMAC 例化](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_m.v#L635-L635) `NV_NVDLA_cmac u_NV_NVDLA_cmac (` —— 同一个模块，在顶层被例化两次。

顶层里两个实例的端口对照（验证 ma 接 cmac_a、mb 接 cmac_b）：

- [u_partition_ma 接 csb2cmac_a](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L2523-L2531) —— 端口名是 `csb2cmac_a_req_*` / `cmac_a2csb_resp_*`。
- [u_partition_mb 接 csb2cmac_b](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L2817-L2825) —— 同样的模块端口名，但接到 `csb2cmac_b_req_*` / `cmac_b2csb_resp_*` 信号（靠例化时的信号映射区分两半）。

**partition_a** 内部（在 `NV_NVDLA_partition_a.v` 中）：

- [CACC 例化](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_a.v#L170-L170) `NV_NVDLA_cacc u_NV_NVDLA_cacc (` —— 顶层例化见 [u_partition_a](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L3111-L3123)，其端口 `csb2cacc_req_*`、`mac_a2accu_*`、`cacc2sdp_*` 直接暴露了它就是累加器。

**partition_p** 内部（在 `NV_NVDLA_partition_p.v` 中）：

- [SDP 例化](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_p.v#L328-L328) `NV_NVDLA_sdp u_NV_NVDLA_sdp (` —— 顶层例化见 [u_partition_p](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L3161-L3174)，端口 `csb2sdp_*`、`cacc2sdp_*`、`sdp2pdp_*`、`sdp2mcif/cvif_*` 表明它是单点后处理器。

> 另外，四个分区文件里都各自例化了自己的 `NV_NVDLA_reset u_partition_X_reset` 与 `NV_NVDLA_sync3d` 同步器（例如 [partition_a 的复位例化](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_a.v#L143-L143)）。这再次印证「分区 = 复位/时钟域边界」。u6-l1 会专题讲复位与时钟门控。

#### 4.3.4 代码实践

**目标**：验证「引擎分区 = 时钟/复位/电源域边界」，并看懂 CMAC 两半如何区分。

**步骤**：

1. 打开 `NV_NVDLA_partition_m.v`，确认它只例化了 `NV_NVDLA_cmac`（第 635 行）+ 复位/同步原语，没有别的引擎。
2. 回到 `NV_nvdla.v`，对比 [u_partition_ma 的端口映射](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L2523-L2531) 与 [u_partition_mb 的端口映射](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L2817-L2825)：两处 `.csb2cmac_a_req_pvld (...)` 括号里分别是 `csb2cmac_a_req_dst_pvld` 与 `csb2cmac_b_req_dst_pvld`。
3. 在四个分区文件里各搜索一次 `NV_NVDLA_reset u_`，确认每个分区都有独立复位例化。

**需要观察的现象 / 预期结果**：四个引擎分区结构高度对称（都是「一个主引擎 + reset + sync3d」），区别只在主引擎种类与电源岛编号。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：partition_a 里只有一个引擎，它是谁？为什么「a」不代表「配置」？
**答**：是 CACC（[partition_a.v:170](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_a.v#L170)）。「a」是 **A**ccumulator 的缩写。配置（csb_master/glb）实际在 partition_o。

**练习 2**：CMAC 阵列两半（ma/mb）用的是同一个 RTL 模块吗？靠什么区分？
**答**：是同一个 `NV_NVDLA_partition_m` 模块（[partition_m.v:635](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_m.v#L635)）。靠顶层例化时把模块端口分别连到 `cmac_a_*` 或 `cmac_b_*` 信号组来区分（[NV_nvdla.v:2523](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L2523) vs [:2817](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L2817)）。

### 4.4 partition_o：配置、存储接口与后处理的真正归属

#### 4.4.1 概念说明

partition_o 是「**O**thers」——凡是不属于 a/c/m/p 四个计算引擎的功能，全装在这里。它包含：

- **配置中枢**：`csb_master`（CSB 地址译码与扇出）、`glb`（全局状态与中断聚合）。
- **两套存储接口**：`mcif`（接 DBB 主存）、`cvif`（接 CVSRAM）。
- **桥 DMA 与其余后处理**：`bdma`、`rubik`、`cdp`、`pdp`（注意 SDP 不在这里）。
- **复位/时钟/观测**：`NV_NVDLA_core_reset`、`NV_NVDLA_sync3d` 系列、`obs` 观测。

正因为这些都在 partition_o，所以 **CSB 配置口、两组 AXI memif、中断线 这三组对外端口全都连到 `u_partition_o`**——这是 4.2 那张归属表的事实依据。同时，`nvdla_core_rstn` 这个核心复位也由 partition_o 里的 `NV_NVDLA_core_reset` 产生，再回灌给所有分区。

#### 4.4.2 核心流程（partition_o 内部 + 对外端口）

```text
外部 CSB ──> csb2nvdla_* ──> [u_partition_o] csb_master ──(csb2*_req)──> 各引擎寄存器(含 o 内的 bdma/cdp/pdp/glb...)
                                                            ^
各引擎 done 中断 ──(xxx2glb_done_intr)──> glb ──> core_intr ──> dla_intr (对外)

访存请求(各引擎) ──> [u_partition_o] mcif ──> nvdla_core2dbb_* (对外 AXI)
                                  cvif ──> nvdla_core2cvsram_* (对外 AXI)

dla_reset_rstn ──> [u_partition_o] core_reset ──> nvdla_core_rstn ──> 回灌所有分区
```

#### 4.4.3 源码精读

顶层把对外端口连进 partition_o 的关键映射（这是本讲实践题的直接答案）：

- [CSB 请求端口连入 partition_o](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L1208-L1213) —— `.csb2nvdla_valid(csb2nvdla_valid)` 等，证明 **csb2nvdla_* 属于 partition_o**。
- [core2dbb AXI 写地址通道连入 partition_o](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L1267-L1271) —— `.mcif2noc_axi_aw_awvalid(nvdla_core2dbb_aw_awvalid)` 等，证明 **nvdla_core2dbb 的写地址通道连到 partition_o**（再经 mcif）。
- [core2cvsram AXI 通道连入 partition_o](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L1228-L1242) —— `.cvif2noc_axi_*(nvdla_core2cvsram_*)`，证明 CVSRAM 接口也归 partition_o（再经 cvif）。
- [中断映射 core_intr → dla_intr](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L1309-L1309) —— `.core_intr(dla_intr)`，证明 **唯一中断线由 partition_o 输出**（来自 glb 的中断聚合）。

partition_o 内部到底装了什么（在 `NV_NVDLA_partition_o.v` 中）：

- [核心复位 core_reset 例化](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_o.v#L1703-L1703) `NV_NVDLA_core_reset u_sync_core_reset (` —— 产生 `nvdla_core_rstn`。
- [csb_master 例化](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_o.v#L1771-L1771) —— 配置总线扇出中枢。
- [mcif 例化](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_o.v#L1923-L1923) 与 [cvif 例化](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_o.v#L2108-L2108) —— 两套存储接口。
- [bdma / rubik / cdp / pdp 例化](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_o.v#L2294-L2425)（第 2294/2336/2378/2425 行）—— 桥 DMA 与后处理引擎全在这。
- [glb 例化](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_o.v#L2476-L2476) —— 全局配置与中断聚合。

可以看到，partition_o 一家就占了顶层接近一半的逻辑量，是名副其实的「中央枢纽」。

#### 4.4.4 代码实践（本讲主实践）

**目标**：回答规格里的实践题——「找到 `csb2nvdla_*` 端口所属的分区，并追踪 `nvdla_core2dbb` AXI 写地址通道连接到哪个分区实例」。

**步骤**：

1. 在 `NV_nvdla.v` 搜索 `csb2nvdla_valid`，找到它在端口声明（[第 24/102 行](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L102-L107)）与在哪个例化的端口映射里出现。答案：[第 1208 行，u_partition_o 的端口表](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L1208-L1213)。
2. 在 `NV_nvdla.v` 搜索 `nvdla_core2dbb_aw_awvalid`，定位它的端口声明（[第 33/114 行](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L114-L118)）与例化映射。答案：[第 1267 行，u_partition_o 的 `.mcif2noc_axi_aw_awvalid(...)`](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L1267-L1271)。
3. 可选：用一条命令同时验证两点：
   ```bash
   grep -nE 'csb2nvdla_valid|nvdla_core2dbb_aw_awvalid' vmod/nvdla/top/NV_nvdla.v | grep '\.(csb2nvdla_valid|mcif2noc_axi_aw_awvalid)'
   ```

**需要观察的现象 / 预期结果**：

- `csb2nvdla_*` 全部出现在 `u_partition_o (...)` 的端口映射里 → **CSB 配置口属于 partition_o**。
- `nvdla_core2dbb_aw_awvalid/awid/awlen/awaddr` 出现在 `u_partition_o` 的 `.mcif2noc_axi_aw_*(...)` → **core2dbb AXI 写地址通道连到 partition_o（经 mcif 对外）**。

> 结论：两组端口都归 **`u_partition_o`**，而不是 a 或 m。命令的精确输出 **待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：顶层唯一的 `dla_intr` 是哪个分区产生的？它内部又来自哪？
**答**：由 partition_o 产生（[NV_nvdla.v:1309](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L1309) `.core_intr(dla_intr)`）。内部来自 partition_o 里 `glb`（[partition_o.v:2476](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_o.v#L2476)）聚合的各引擎 `done_intr`（u2-l4 详讲）。

**练习 2**：为什么把 csb_master/mcif/cvif/glb/bdma/cdp/pdp 这些「不是计算引擎」的东西都塞进同一个 partition_o？
**答**：这些是「共享基础设施」，需要统一处理跨时钟域（CSB 在 `dla_csb_clk`、memif 在 `dla_core_clk`）、统一的中断聚合与统一的电源/复位管理。把它们集中在一个分区里，便于做跨域同步与时序收敛，也让计算引擎分区（a/c/m/p）保持精简、独立开关电源。

## 5. 综合实践

把本讲知识串起来，完成一张「**NV_nvdla 端口 → 分区 → 引擎**」全景图：

1. 在一张白纸上画出 6 个分区方框：`u_partition_o`（画大一点，居中/靠上）、`u_partition_c`、`u_partition_ma`、`u_partition_mb`、`u_partition_a`、`u_partition_p`。
2. 在每个方框里写上它装的引擎（对照 4.2.1 的表）。
3. 从顶层端口引线：
   - `csb2nvdla_*` 与 `dla_intr` → `u_partition_o`；
   - `nvdla_core2dbb_*` → `u_partition_o`(mcif) → 对外主存；
   - `nvdla_core2cvsram_*` → `u_partition_o`(cvif) → 对外 CVSRAM。
4. 画出卷积主流水的数据链：`c(CDMA/CSC) → ma/mb(CMAC) → a(CACC) → p(SDP)`，并在 SDP 之后标一个箭头「→ partition_o 的 PDP/CDP」。
5. 标注时钟域：CSB 口在 `dla_csb_clk`，memif 与引擎在 `dla_core_clk`，二者在 partition_o 内完成跨域。

**验收标准**：合上源码，你能凭这张图回答任意一个对外端口「归哪个分区、再到哪个引擎」。若画完发现 `csb2nvdla` 或 `core2dbb` 指向了 a/c/m，说明被旧口诀误导了，请回到 4.4.3 的链接核对修正。

> 进阶（可选）：用 `grep -n 'u_partition_' vmod/nvdla/top/NV_nvdla.v` 把六个例化的起止行号标在方框旁，再对每个分区文件 `grep -n 'NV_NVDLA_.*u_NV_NVDLA_'` 写出内部引擎行号，做成一份可点击的「源码索引」。运行细节 **待本地验证**。

## 6. 本讲小结

- `NV_nvdla` 顶层对外端口只有几类：CSB 配置口、两组 AXI memif（`core2dbb`/`core2cvsram`）、一根中断 `dla_intr`、双时钟与按分区的电源岛控制（共 6 个 `nvdla_pwrbus_ram_*_pd`）。
- 顶层把逻辑拆成 6 个分区实例：`u_partition_o/c/ma/mb/a/p`，其中 `partition_m` 模块被例化两次（ma、mb）。
- **真实分区角色**（以源码为准）：a=CACC、c=CDMA+CBUF+CSC、m=CMAC（两半）、p=SDP、o=其余一切（csb_master/glb/mcif/cvif/bdma/rubik/cdp/pdp/复位/obs）。
- **partition_o 是中央枢纽**：CSB 口、两组 AXI memif、中断线 **全都连到 `u_partition_o`**——「a=config / m=memif」的旧口诀对 nvdlav1 源码不成立。
- 每个分区自带 `NV_NVDLA_reset` 与 `sync3d`，且各占一个电源岛，印证「分区 = 时钟域 = 复位域 = 电源岛」。
- `dla_intr` 由 partition_o 内 `glb` 聚合各引擎 `done_intr` 后经 `core_intr` 输出；`nvdla_core_rstn` 由 partition_o 内 `NV_NVDLA_core_reset` 产生并回灌所有分区。

## 7. 下一步学习建议

- 进入 **u2 单元（配置空间与寄存器子系统）**：从 u2-l1 的 CSB 协议与 apb2csb 桥开始，理解本讲 `csb2nvdla_*` 这组端口背后的事务细节。
- 接着读 **u2-l2 csb_master**：本讲看到 partition_o 里的 `csb_master`（[partition_o.v:1771](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_o.v#L1771)）如何把单一 CSB 扇出到所有引擎。
- 再进入 **u3 单元（卷积主流水线）**：深入本讲串联的 `c → m → a` 链（CDMA/CSC/CMAC/CACC），看清楚那些 `sc2mac_*`、`mac_a2accu_*` 内部 wire 究竟承载什么数据。
- 想先了解存储接口的读者可跳到 **u4-l1**，看 `mcif`/`cvif` 如何把本讲的 `core2dbb`/`core2cvsram` 落到具体读写通路。
