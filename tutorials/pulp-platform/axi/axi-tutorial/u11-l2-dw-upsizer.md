# axi_dw_upsizer：窄到宽

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚「窄到宽数据宽度转换」在转换什么：不是改一根线，而是把**多个窄 beat 合并成更少的宽 beat**，并相应改写地址、`size`、`len`、`strobe` 与响应拍数——它是上一讲 `axi_dw_downsizer` 的**镜像**。
- 推导 upsize 的核心数学：输出（宽）`len` **小于**输入（窄）`len`，并手算「4 拍 32 位读 → 2 拍 64 位读」的完整变换。
- 读懂 `axi_dw_upsizer` 的**读通路**：`AxiMaxReads` 个并行引擎、按 ID 路由、以及最关键的「**持有一拍宽 R、展开成多拍窄 R**」机制；读懂**写通路**：「累加多拍窄 W、按宽边界释放成一拍宽 W」与 B 通道直通。
- 看懂 `test/tb_axi_dw_upsizer.sv` 如何用随机主从 + `axi_dw_upsizer_monitor`（双端口黄金模型）自检宽度合并的正确性。

本讲依赖 **u11-l1（downsizer，宽到窄）**，并承接 **u1-l3（AXI 协议：len/size/burst/strobe）**、**u2-l2（`aligned_addr`/`beat_addr`/`modifiable` 函数）** 与 **u4-l1（接口外壳 + 结构体内核范式）**。如果你已读过 u11-l1，本讲会处处与 downsizer 对照，理解成本会低很多。

## 2. 前置知识

在进入源码前，先用三句话建立直觉。

**窄 beat 与宽 beat。** AXI 数据总线宽度固定，一个 beat（拍）的有效载荷由 `size` 决定：`num_bytes = 2^size`。32 位端口一拍最多搬 4 字节（`size=2`），64 位端口一拍最多搬 8 字节（`size=3`）。

**为什么窄到宽「多拍要合一拍」。** 假设上游（slave 端口，窄）发来 4 拍 32 位读（共 16 字节），下游（master 端口，宽）是 64 位、一拍能搬 8 字节。那么这 16 字节下游只需 **2 拍**就能搬完。所以窄→宽的本质是「**地址方向压缩、数据方向合并、响应方向再展开**」。

**关键区别于 downsize。** 上一讲 downsize 是「一拍宽拆多拍窄」，所以**输出 `len` 变大**、一个宽写可能产生多个窄 B 需要合并；本讲 upsize 是「多拍窄合一拍宽」，所以**输出 `len` 变小**、一个写事务始终只产生一个 B（**B 通道直接透传**）。两者的引擎结构因此并不对称，阅读时要随时对照。

> 术语提示：本库模块命名里 `slv` 端口 = **上游**侧、`mst` 端口 = **下游**侧（与 u6-l1 的 xbar 一致）。对 **upsizer** 而言：slv 端口是**窄**的（接发起方 master 设备），mst 端口是**宽**的（接目标 slave 设备）——模块头注释写得很直白：「Connects a narrow master to a wider slave.」

## 3. 本讲源码地图

| 文件 | 作用 | 层级 |
|---|---|---|
| `src/axi_dw_upsizer.sv` | 窄→宽转换内核（结构体版），本讲主角 | Level 2 |
| `src/axi_dw_converter.sv` | 统一分发器：按两端宽度选 up/down/null，并含接口外壳 `axi_dw_converter_intf` | Level 3 |
| `test/tb_axi_dw_upsizer.sv` | 32→64 的随机测试台 | — |
| `test/tb_axi_dw_pkg.sv` | 内含 `axi_dw_upsizer_monitor`，双端口黄金模型自检器 | — |
| `src/axi_pkg.sv` | 提供 `aligned_addr`、`beat_addr`、`modifiable` 等被复用函数 | Level 0 |

注意：测试台**不直接**例化 `axi_dw_upsizer`，而是例化统一外壳 `axi_dw_converter_intf`，由它在编译期按 `MstDataWidth > SlvDataWidth` 落到 upsize 分支（见 4.5）。

## 4. 核心概念与源码讲解

### 4.1 upsize 的本质与协议边界

#### 4.1.1 概念说明

`axi_dw_upsizer` 把一个**窄数据**的 slave 端口接到一个**宽数据**的 master 端口。AXI 五通道结构与握手规则原封不动；它要改写的是与**数据宽度耦合**的字段：

- `size`：上游窄端口用小 `size`（如 2，一拍 4 字节），下游宽端口一拍能搬 8 字节，应升到 `AxiMstPortMaxSize`（如 3）。
- `len`：多拍窄 beat 合并成少拍宽 beat，**输出 `len` 比输入小**。
- `addr`：INCR 突发每拍地址按**原始窄 `size`** 推进（用来定位字节车道），但下游看到的是按宽 `size` 步进的地址。
- `wstrb`（写）：多拍窄 `wstrb` 的比特被**累加拼装**进一拍宽 `wstrb` 的对应车道。
- 响应拍数：写事务始终一个 B（直通）；读事务一拍宽 R 被展开成多拍窄 R，需要重新组织 `last`。

模块头注释划定了**支持的突发类型边界**：不支持 WRAP（返回 SLVERR），单拍 WRAP（`len==0`）除外。

[src/axi_dw_upsizer.sv:18-19](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_upsizer.sv#L18-L19) — 头注释声明 WRAP 不支持、回 SLVERR。

#### 4.1.2 核心流程

upsizer 对每条通道的处理概括如下（注意读方向「先压缩再展开」的双向变换）：

```text
           ┌── 读: AR(窄,len大) ──► [upsize len] ──► AR(宽,len小) ──► 下游
上游(窄) ◄─┤                                  下游(宽) ──► R(宽)
           │   ◄── R(窄,len大) ◄── [持有宽R+lane steering 展开] ◄──┘
           │
           └── 写: AW(窄,len大) ──► [upsize len] ──► AW(宽,len小) ──► 下游
                W(窄,多拍)  ──► [累加拼装] ──► W(宽,少拍) ──► 下游
                ◄── B(直通) ◄── B ◄── 下游
```

- **读方向（AR→R）**：AR 通道把窄突发「压缩」成宽突发发往下游；R 通道把回来的宽 R 拍**按字节车道切开**，一拍宽 R 展开成多拍窄 R。用 `AxiMaxReads` 个**并行**引擎处理多个在途读，按 ID 路由到同一引擎以保序。
- **写方向（AW→W→B）**：单个写引擎顺序处理；多拍窄 W 在内部累加成一拍宽 W 再下发；B 通道直接透传（一个写事务只有一个 B）。

### 4.2 upsize 的核心数学：输出 `len` 如何变小

#### 4.2.1 概念说明

这是本讲最容易看错的地方。downsize 里输出 `len` 比输入**大**（一拍拆多拍）；upsize 里输出 `len` 比输入**小**（多拍合一拍）。关键公式在 AW、AR 两处各出现一次，完全相同。

upsizer 用 `axi_pkg::aligned_addr`（清低 `size` 位，向下对齐）和 `axi_pkg::beat_addr`（计算突发内第 `i` 拍的地址）来求**输出宽突发的长度**。

#### 4.2.2 核心流程与数学推导

设窄侧 `size = s_n`、宽侧 `MstMaxSize = s_w`（如 32→64 时 `s_n=2, s_w=3`）。对一段窄 INCR 突发（地址 `addr`、长度 `len_n`），输出宽 `len` 这样算：

\[
\text{start\_addr} = \text{aligned\_addr}(addr,\ s_w)
\]

\[
\text{end\_addr} = \text{aligned\_addr}\big(\text{beat\_addr}(addr,\ s_n,\ len_n,\ \text{INCR},\ len_n),\ s_w\big)
\]

\[
\text{len}_{out} = (\text{end\_addr} - \text{start\_addr})\ \gg\ s_w
\]

直觉：`beat_addr(..., len_n)` 给出**最后一拍窄 beat 的地址**；把它和首拍地址都向下对齐到宽 beat 边界，二者的差再除以宽 beat 大小，得到的正是「最后一拍宽 beat 的下标」，而 len 编码 = 下标，所以直接就是 `len_out`。

以窄 32 位、宽 64 位、`addr=0x0, size=2, len=3`（4 拍窄读）为例手算：

| 量 | 计算 | 值 |
|---|---|---|
| `start_addr` | `aligned_addr(0x0, 3)` | `0x0` |
| 末拍窄地址 | `aligned_addr(0,2) + 3×4` | `0xC` |
| `end_addr` | `aligned_addr(0xC, 3)` | `0x8` |
| `len_out` | `(0x8 - 0x0) >> 3` | `1`（→ 2 拍宽读）|

即下游看到 `addr=0x0, size=3, len=1` 的 INCR 读（2 拍，覆盖字节 0–15）。**4 拍窄 → 2 拍宽**，数据总量不变，`len` 减半。

#### 4.2.3 源码精读

AR 通道的 upsize 计算（`R_IDLE` 态内）：

[src/axi_dw_upsizer.sv:391-399](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_upsizer.sv#L391-L399) — 用 `aligned_addr` + `beat_addr` 算出宽侧 `len`，把 `size` 升到 `AxiMstPortMaxSize`，进入 `R_INCR_UPSIZE`。

AW 通道的 upsize 计算（写引擎接收新 AW 时），公式与上完全镜像：

[src/axi_dw_upsizer.sv:705-714](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_upsizer.sv#L705-L714) — AW 的 upsize 长度计算，进入 `W_INCR_UPSIZE`。

注意两处都先判断「`modifiable(cache)` 且 `len != 0`」才升宽：不可修改（Non-modifiable）事务、单拍事务都不做合并，直接 `PASSTHROUGH`。`modifiable` 来自 `axi_pkg`（见 u2-l2），它读 `cache` 字段的 Bufferable/Modifiable 位，决定事务能否被互联改写。

#### 4.2.4 代码实践

1. **目标**：亲手验证「窄 `len` → 宽 `len`」的换算，而不是凭感觉。
2. **步骤**：
   - 打开 `src/axi_dw_upsizer.sv` 第 391–399 行，对照上面的公式。
   - 取另一组数：窄 32 位、`addr=0x4, size=2, len=3`（首地址**不对齐**到 8 字节边界）。
   - 手算 `start_addr = aligned_addr(0x4, 3) = 0x0`、末拍地址 `= aligned_addr(0x4,2)+3×4 = 0x4+0xC = 0x10`、`end_addr = aligned_addr(0x10,3) = 0x10`、`len_out = (0x10-0x0)>>3 = 2`（→ 3 拍宽读）。
3. **观察现象**：首地址不对齐时输出 `len` 从对齐情形的 1 变成 2——因为要「向上取整」补齐跨越的宽 beat。
4. **预期结果**：你的手算结果应与源码公式一致；这也解释了为何 4.3 里「持有宽 R」机制必须按宽边界释放，而不是简单地每 2 拍窄 R 释放一拍宽 R。
5. 若想用仿真确认，可在测试台给 `axi_rand_master` 喂定向地址后查波形——**待本地验证**。

#### 4.2.5 小练习与答案

- **练习 1**：窄 32 位、宽 64 位、`addr=0x8, size=2, len=1`（2 拍窄读），输出宽 `len` 是多少？
  - **答**：`start=aligned_addr(0x8,3)=0x8`；末拍 `=aligned_addr(0x8,2)+1×4=0xC`；`end=aligned_addr(0xC,3)=0x8`；`len_out=(0x8-0x8)>>3=0`（→ **1 拍宽读**，因为两拍窄恰好落在同一个 8 字节宽 beat 内）。
- **练习 2**：为什么公式里要用 `beat_addr(..., len_n)` 取**最后一拍**而不是第一拍？
  - **答**：输出宽 `len` 编码的是「最后一拍宽 beat 在突发中的下标」；首拍地址只能定起点，末拍地址定终点，二者宽对齐后的跨度才反映需要多少个宽 beat。

### 4.3 读通路：并行引擎、同 ID 路由与「持有宽 R 展开」

#### 4.3.1 概念说明

读方向是 upsizer 最精巧的部分，因为它要做**双向变换**：AR 通道把窄突发压成宽突发（4.2），R 通道又要把回来的宽 R 拍**展开**成窄 R 拍。难点有两个：

1. **多在途并发**：上游可能同时挂起多个读事务，每个的 R 拍会交错返回。upsizer 用 `AxiMaxReads` 个**并行**引擎各管一个在途读，按 AXI ID 把事务路由到同一个引擎，保证同 ID 保序。
2. **一拍宽 R 展开成多拍窄 R**：下游回一拍宽 R（8 字节），上游要分多次（每次 4 字节）收。upsizer 不能立刻向下游确认（`r_ready`）这拍宽 R，必须**持有**它，每拍从中切出不同的字节车道给上游，直到这拍宽 R 的字节被取完。

#### 4.3.2 核心流程

```text
新 AR 到来 ──► [选引擎: 有同ID在途? 用它 : 选空闲引擎] ──► 引擎 t
                                                              │
                   AR(宽,len小) ◄── [引擎 t 内 upsize len] ◄──┘ ──► 下游
                                                              │
下游 R(宽) 到来 ──► [按 R.id 匹配引擎 t] ──► 引擎 t 持有宽 R
                                              │
              ┌──── 每拍: lane steering 切出 4 字节 ────► 上游 R(窄)
              │
              └──── 当本宽 beat 字节取完 或 突发结束 ───► r_ready=1 释放宽 R
```

引擎选择的关键：**同 ID 的事务必须路由到同一个引擎**，否则两个引擎各自回窄 R 会乱序。状态机用 `R_IDLE`/`R_INJECT_AW`/`R_PASSTHROUGH`/`R_INCR_UPSIZE` 四态。

#### 4.3.3 源码精读

引擎索引宽度定义——`AxiMaxReads` 个并行引擎需要一个下标来寻址：

[src/axi_dw_upsizer.sv:59-60](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_upsizer.sv#L59-L60) — `TranIdWidth` 用于「哪个引擎在处理哪个在途读事务」。

**引擎选择逻辑**：先找空闲引擎（`lzc` 优先编码），但如果已有引擎正在处理**同 ID** 的事务（`id_clash_upsizer`），则强制复用它以保序：

[src/axi_dw_upsizer.sv:286](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_upsizer.sv#L286) — 有 ID 冲突时复用同 ID 引擎，否则取最空闲引擎。

返回的 R 拍也要按 `r.id` 找回对应引擎：

[src/axi_dw_upsizer.sv:299-301](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_upsizer.sv#L299-L301) — 用 `mst_resp.r.id` 与各引擎记录的 `mst_ar_id` 比对，定位宽 R 该交给哪个引擎。

**「持有宽 R + lane steering 展开」是本节核心**。在 `R_PASSTHROUGH, R_INCR_UPSIZE` 态里：

[src/axi_dw_upsizer.sv:496](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_upsizer.sv#L496) — 重新生成窄侧 `last`：只有当下游宽 R 是 `last` **且**本引擎窄突发也到末拍（`burst_len==0`）时才拉高——因为一拍宽 R 的 `last` 不等于窄突发的 `last`。

[src/axi_dw_upsizer.sv:499-506](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_upsizer.sv#L499-L506) — **lane steering**：按 `mst_port_offset`/`slv_port_offset` 从宽 R 的 8 字节里切出当前窄 R 需要的 4 字节，放进 `r_data` 的对应车道。这就是 R 通道上「宽→窄」的数据切分。

[src/axi_dw_upsizer.sv:526-528](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_upsizer.sv#L526-L528) — **释放宽 R 的条件**：仅当窄突发取完（`burst_len==0`）**或**下一拍窄地址越过了宽 beat 边界时，才向下游置 `r_ready=1`。这就是「一拍宽 R 喂多拍窄 R」的物理实现——在字节取完前一直挂着 `r_ready=0`，把同一拍宽 R 复用多个窄拍。

三个 `rr_arb_tree` 仲裁器把多引擎的输入输出收拢：R 通道用 `AxiMaxReads` 选一合并回上游（`LockIn=1`）；AR 上游侧用 2 选 1 在「正常 AR」与「ATOP 注入的 AW」间切换；AR 下游侧用 `AxiMaxReads` 选一发出。

[src/axi_dw_upsizer.sv:96-114](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_upsizer.sv#L96-L114) — R 通道仲裁树：把 `AxiMaxReads` 个引擎的窄 R 输出合成一路交给上游 slave 端口。

#### 4.3.4 代码实践

1. **目标**：理解「一拍宽 R 展开成多拍窄 R」对吞吐与时序的影响。
2. **步骤**：阅读 [src/axi_dw_upsizer.sv:509-535](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_upsizer.sv#L509-L535)，跟踪一次握手：上游收一拍窄 R 后 `burst_len` 减 1、窄地址按 `orig_ar_size` 推进；只有越过宽边界或到末拍才置 `mst_r_ready_tran[t]=1`。
3. **观察现象**：在波形/日志里你会看到下游宽 R 的 `valid` 拉高后，`ready` 并不立刻跟进，而是等上游把窄拍吃够才拉高。
4. **预期结果**：对 32→64、窄 `len=3` 的读，下游只发 2 拍宽 R，上游却收到 4 拍窄 R——宽 R 的 `valid` 跨多个时钟周期保持高（pending），直到被释放。**待本地验证**。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 upsizer 需要 `AxiMaxReads` 个并行读引擎，而写引擎只有一个？
  - **答**：读事务的 R 响应可能**乱序交错**返回（不同 ID），需要按 ID 路由到独立引擎各自重组；写事务天然被 AW→W→B 顺序串成一条链，单引擎顺序处理即可，无需并行。
- **练习 2**：如果两个在途读事务**同 ID**，upsizer 会把它们分到两个引擎吗？
  - **答**：不会。[第 286 行](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_upsizer.sv#L286)的 `id_clash` 逻辑会强制复用同一引擎，以满足 AXI「同 ID 同方向保序」的要求。

### 4.4 写通路：累加窄 W、按边界释放宽 W、B 直通与 ATOP 注入

#### 4.4.1 概念说明

写方向是读方向的「对偶」。读是「一拍宽 R → 多拍窄 R」（切分），写则是「多拍窄 W → 一拍宽 W」（**合并**）。upsizer 在内部维护一个宽 beat 累加器，每收到一拍窄 W 就把对应字节车道填进去，**直到一个宽 beat 装满或突发结束**才向下游发一拍宽 W。

写通路还有两个简化点（相对 downsizer）：

- **B 通道直接透传**：一个写事务（无论窄端几拍）在宽端始终是**一个**事务、产生**一个** B，所以 B 不需要任何合并/拆分逻辑——这与 downsizer「多个窄 B 合并成一个」截然不同。
- **单写引擎**：写天然串行，不需要读那样的并行引擎阵列。

#### 4.4.2 核心流程

```text
窄 W(每拍4字节) ──► [按偏移填入宽 beat 累加器 w_data] ──► 累加
                                                          │
                         当 宽beat装满 或 突发末拍 ──► w_valid=1 下发一拍宽 W
                                                          │
下游 ◄── W(宽,少拍) ◄──────────────────────────────────────┘
下游 ──► B ──► [直通] ──► 上游 B
```

写状态机三态：`W_IDLE`/`W_PASSTHROUGH`/`W_INCR_UPSIZE`。

#### 4.4.3 源码精读

B 通道零逻辑透传（注意对比 downsizer 在此处的合并 FIFO）：

[src/axi_dw_upsizer.sv:592-595](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_upsizer.sv#L592-L595) — B 通道的 `b`/`b_valid`/`b_ready` 直接在上下游间互连，无缓冲、无延迟，因为 upsize 不改变写事务数量。

窄 W 的**序列化拼装**（按字节车道累加进宽 `w_data`，并搬对应的 `strb` 比特）：

[src/axi_dw_upsizer.sv:624-634](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_upsizer.sv#L624-L634) — 把窄 W 的数据字节和 `strb` 比特按偏移填进宽 beat；窄 `burst_len` 逐拍减 1，`w.last` 在窄突发末拍拉高。

**释放宽 W 的条件**（与读的「释放宽 R」对偶）：只有装满或突发末拍才置 `w_valid=1`：

[src/axi_dw_upsizer.sv:652-655](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_upsizer.sv#L652-L655) — `W_INCR_UPSIZE` 态下，仅当窄突发到末拍（`burst_len==0`）或越过宽 beat 边界时，才把累加好的宽 W 置 `w_valid` 下发。

**ATOP（原子操作）注入**：当上游 AW 带有 `ATOP_R_RESP` 位（即「带读响应的原子写」，见 u15-l1），它不仅产生 B 还会产生 R。upsizer 必须把这个 AW **也注入到读通路**，让某个读引擎去接收它的 R 响应：

[src/axi_dw_upsizer.sv:679-684](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_upsizer.sv#L679-L684) — 检测到 `aw.atop[ATOP_R_RESP]` 时，发起 `inject_aw_into_ar_req`，并在注入被授权后才回 `aw_ready`，把 AW 复用进读引擎。

[src/axi_dw_upsizer.sv:125-128](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_upsizer.sv#L125-L128) — 相应地，AR 上游仲裁器是 2 选 1：在「正常 AR」与「ATOP 注入的 AW」间复用，使原子写的 R 响应能走读引擎返回。

#### 4.4.4 代码实践

1. **目标**：看清「多拍窄 W 合并成一拍宽 W」时 `strb` 如何拼接。
2. **步骤**：读 [src/axi_dw_upsizer.sv:617-635](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_upsizer.sv#L617-L635)，注意 `mst_port_offset`/`slv_port_offset` 都从 `aw.addr` 的低位取出，决定窄字节填进宽 beat 的哪一段。
3. **观察现象**：若窄写 `addr=0x0, size=2, len=1`（2 拍窄 W，写字节 0–7），两拍的 `strb`（各 4 比特）会被拼成一个 8 比特宽 `strb`，且只发 **1 拍**宽 W。
4. **预期结果**：拼装后宽 W 的 `strb=8'b1111_1111`、`data` 为两拍窄数据的拼接，`last=1`。可用 4.5 的测试台 + monitor 验证。

#### 4.4.5 小练习与答案

- **练习 1**：为什么 upsizer 的 B 通道可以直接透传，而 downsizer 不行？
  - **答**：upsize 把多拍窄 W 合并成少拍宽 W，但**事务数量不变**（仍是一个写事务），所以宽端只回一个 B、窄端也只期待一个 B，无需合并。downsize 把一拍宽 W 拆成多拍窄 W 时，宽端可能对应多个窄端事务、多个窄 B，必须合并成一个 B 才能交回宽端上游。
- **练习 2**：`W_INCR_UPSIZE` 里，若窄突发末拍到达时宽 beat 还没装满（比如只写了 4 字节到一个 8 字节宽 beat），宽 W 会下发吗？
  - **答**：会。[第 654 行](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_upsizer.sv#L654)的条件是「`burst_len==0`（末拍）**或**越过宽边界」，末拍触发即下发；未填满的车道由 `strb` 标为无效，下游据此只写有效字节。

### 4.5 接口外壳、分发器与测试台黄金模型自检

#### 4.5.1 概念说明

和 downsizer 一样，upsizer 是**结构体内核**（端口是 `req_t`/`resp_t`），用户通常不直接例化它，而是通过统一外壳 `axi_dw_converter_intf`（接口版）使用，后者再调用 `axi_dw_converter`（结构体版分发器）。分发器在**编译期**按两端宽度三选一：等宽直通、宽>窄走 downsize、宽<窄走 upsize。本讲的 upsize 分支对应 `AxiMstPortDataWidth > AxiSlvPortDataWidth`。

#### 4.5.2 核心流程

```text
axi_dw_converter_intf (AXI_BUS 接口)
        │ AXI_TYPEDEF/ASSIGN 宏
        ▼
axi_dw_converter ──┬─ MstDW==SlvDW ─► assign 直通
                   ├─ MstDW >  SlvDW ─► axi_dw_downsizer   (u11-l1)
                   └─ MstDW <  SlvDW ─► axi_dw_upsizer     (本讲)
```

测试台 `tb_axi_dw_upsizer` 例化的就是这个外壳，并用 `axi_dw_upsizer_monitor` 做黄金模型自检：monitor 同时窥探窄 slave 端口和宽 master 端口，独立预测「窄事务应被合并成什么样的宽事务」，再逐拍比对实际下游观测，统计 `Tests Failed`。

#### 4.5.3 源码精读

分发器的 upsize 分支（编译期 `generate if`）：

[src/axi_dw_converter.sv:51-79](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_converter.sv#L51-L79) — `AxiMstPortDataWidth > AxiSlvPortDataWidth` 时例化 `axi_dw_upsizer`；等宽与 downsize 分支见同文件第 46–49、81–109 行。

测试台的关键配置：窄 32 / 宽 64、`AXI_MAX_READS=4`、随机主端开启 ATOP：

[test/tb_axi_dw_upsizer.sv:19-23](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_dw_upsizer.sv#L19-L23) — TB 参数：`TbAxiSlvPortDataWidth=32`、`TbAxiMstPortDataWidth=64`。

[test/tb_axi_dw_upsizer.sv:70-80](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_dw_upsizer.sv#L70-L80) — `axi_rand_master` 配置：`MAX_READ/WRITE_TXNS=8`、`AXI_ATOPS=1`（会发出原子操作，触发 4.4 的注入路径）。

[test/tb_axi_dw_upsizer.sv:115-127](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_dw_upsizer.sv#L115-L127) — DUT 是 `axi_dw_converter_intf`，`AXI_MAX_READS=4`（即 4 个并行读引擎）。

[test/tb_axi_dw_upsizer.sv:159-167](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_dw_upsizer.sv#L159-L167) — 例化 `axi_dw_upsizer_monitor` 做双端口自检。

monitor 里对「窄 INCR → 宽 INCR」的预测，用的正是 4.2 的同一公式（互为参照，说明内核与黄金模型一致）：

[test/tb_axi_dw_pkg.sv:801-813](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_dw_pkg.sv#L801-L813) — `mon_slv_port_ar` 预测宽侧 `len=(aligned_end-aligned_start)>>AxiMstPortMaxSize`、`size=AxiMstPortMaxSize`（单拍则保留原 size）。

[test/tb_axi_dw_pkg.sv:844](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_dw_pkg.sv#L844) — 用 `conv_ratio(mst_size, slv_size) = ceil(宽字节/窄字节)` 决定一拍宽 R 展开成几拍窄 R（32→64 时为 2）。

回归脚本对 upsizer 的宽度扫描（窄从 8 到 1024，宽至少为窄的 2 倍直至 1024）：

[scripts/run_vsim.sh:71-82](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh#L71-L82) — `axi_dw_upsizer` 回归：遍历所有「窄<宽」的 2 的幂组合，每种跑一次随机仿真。

#### 4.5.4 代码实践

1. **目标**：跑通 upsizer 测试台，确认黄金模型自检通过。
2. **步骤**：
   - 按 u1-l4 介绍的方法，执行 `make sim-axi_dw_upsizer.log`（等价于 `scripts/run_vsim.sh` 选中 `axi_dw_upsizer` 分支）。
   - 仿真结束后查看日志末尾 monitor 打印的 `Tests Expected / Conducted / Failed` 三行，以及仿真器统计行 `Errors: 0,`。
3. **观察现象**：日志里会有大量 `Master: AR with ID: ...`、`Got last B with ID: ...` 的进度行；若 monitor 发现实际下游事务与预测不符，会打印 `Slave: Unexpected AR/AW/W/R ...` 警告并累计 `Tests Failed`。
4. **预期结果**：`Tests Failed: 0` 且 `Errors: 0,`——表示在随机 200 读 + 200 写（含 ATOP）激励下，窄→宽合并与 R 展开全部正确。**待本地验证**（本环境无 vsim）。
5. 想看波形，可参考 `test/tb_axi_dw_upsizer.do` 里给出的层级路径 `.../gen_dw_upsize/i_axi_dw_upsizer/*` 添加信号。

#### 4.5.5 小练习与答案

- **练习 1**：为什么 `run_vsim.sh` 里 upsizer 的宽度循环是 `MstDataWidth` **大于** `SlvDataWidth`，而 downsizer 恰好相反？
  - **答**：upsizer 的 master 端口是宽的、slave 端口是窄的（窄→宽），所以 `MstDW > SlvDW`；downsizer 反之（宽→窄，`SlvDW > MstDW`）。两者合起来覆盖所有「不等宽」组合，分发器据此二选一。
- **练习 2**：monitor 用 `8'h??`/`1'b?`（don't-care）填充未被事务访问的字节车道，为什么？
  - **答**：upsize 合并/展开时，宽 beat 里**不在本次窄事务访问范围内**的字节是未定义的（upsizer 不写也不读它们），用 `x`/`?` 通配可避免黄金模型在这些位上误报失配（与 u3-l2 scoreboard 的 `8'hxx` 容忍同理）。

## 5. 综合实践

把 4.2 的数学、4.3 的读展开、4.5 的自检串起来，完成下面这个端到端小任务。

**任务**：预测并验证一次「窄 32 位、`addr=0x0, size=2, len=3` 的 INCR 读」经过 32→64 upsizer 后的完整变换。

1. **地址方向（AR）**：按 4.2 手算，写下下游宽端应看到的 AR 字段（`addr`/`size`/`len`/`burst`）。预期：`addr=0x0, size=3, len=1, burst=INCR`。
2. **读响应方向（R）**：按 4.3 推理，下游回 2 拍宽 R（字节 0–7、8–15），上游应收到 4 拍窄 R（字节 0–3、4–7、8–11、12–15），顺序保持。指出「第 1 拍宽 R 会被持有 2 个窄拍才释放」。
3. **交叉核对**：把你的预测与 monitor 的预测逻辑对比——[test/tb_axi_dw_pkg.sv:801-813](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_dw_pkg.sv#L801-L813) 与 [:844](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_dw_pkg.sv#L844)，确认 `conv_ratio(3,2)=2` 与你的「1 拍宽 R → 2 拍窄 R」一致。
4. **仿真确认**：跑 `make sim-axi_dw_upsizer.log`，确认 `Tests Failed: 0`。
5. **反思题**：若把 `AxiMaxReads` 从 4 改成 1（[test/tb_axi_dw_upsizer.sv:116](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_dw_upsizer.sv#L116)），读并发会降为 1，功能仍应正确但吞吐下降——想清楚为什么单引擎也能保序。

**交付物**：一张表格（窄侧 AR/R ↔ 宽侧 AR/R 的逐字段对照）+ 一段话解释「持有宽 R」的必要性。

## 6. 本讲小结

- `axi_dw_upsizer` 是 `axi_dw_downsizer` 的**镜像**：slv 端口窄、mst 端口宽，把**多个窄 beat 合并成更少的宽 beat**。
- 核心数学：输出宽 `len = (aligned_end − aligned_start) >> AxiMstPortMaxSize`，**小于**输入窄 `len`；AW、AR 两处公式相同。
- **读通路**用 `AxiMaxReads` 个并行引擎 + 按 ID 路由保序；最关键的机制是**持有一拍宽 R、用 lane steering 展开成多拍窄 R**，直到宽 beat 字节取完才向下游置 `r_ready`。
- **写通路**用单引擎累加多拍窄 W 成一拍宽 W，按宽边界或末拍释放；**B 通道直接透传**（事务数不变），这点与 downsizer 显著不同。
- 不支持 WRAP（回 SLVERR，单拍 WRAP 除外）；ATOP 带读响应的原子写通过把 AW **注入读通路** 处理。
- 用户经统一外壳 `axi_dw_converter_intf`（由 `axi_dw_converter` 按宽度分发）使用；测试台用 `axi_dw_upsizer_monitor` 黄金模型自检，与内核用同一套公式互为参照。

## 7. 下一步学习建议

- **下一讲 u11-l3**：精读 `axi_dw_converter` 这个统一入口，看它如何把 upsize/downsize/直通三路在编译期收口，并在异构网络里充当「数据宽度子网胶水」。
- **横向对照**：回到 u11-l1 把 downsizer 的「拆拍、合并 B」与本讲的「合拍、透传 B」并列重读，巩固「事务数变与不变」这条判别线。
- **向应用延伸**：upsize 常出现在「窄 DMA / 32 位 CPU 接到宽 DDR 控制器」的场合；学完 u15-l4（异构网络）后，可尝试画出「窄 master → upsizer → 宽 xbar → 宽 memory」的拓扑，体会宽度转换器在网络中的部署位置。
