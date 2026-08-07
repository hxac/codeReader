# TCAM 最长前缀路由查找

## 1. 本讲目标

数据面引擎（DPE）要把每个收到的数据包送到正确的物理出口（eth1~eth4 或 CPU），并把包和一个 WireGuard peer 绑定。这需要一个**路由查找**环节：根据包里的 IPv4 目的地址，查路由表，决定「从哪个口发、属于哪个 peer」。

本讲聚焦这一环节的硬件实现。读完本讲，你应当能够：

- 说清**最长前缀匹配（LPM）**要解决什么问题，以及为什么本项目用**并行 TCAM** 而不是树形查找。
- 读懂 `dpe_route_mem.sv` 如何用一行并行比较 + 一个优先编码器实现 64 条目的 TCAM，并理解「软件按前缀长度降序排序、硬件首匹配即最长」这一软硬分工。
- 读懂 `dpe_egress_ip_lookup.sv` 如何协调三件事：CPU 经 CSR 配置路由表、跨 128 位 beat 缓存包头做查表、把查表结果写回 AXIS 侧带元数据。
- 手工模拟一次目的 IP 的命中过程，并选出 `best_idx`。

> 一个必须先知道的事实：当前 HEAD 处于 Phase1 PoC，`dpe_egress_ip_lookup` 与 `dpe_route_mem` 这条完整的 TCAM 查找链**源码已经写好，但在 `dpe.sv` 中被注释掉了**，实际综合进去的是直通的 `dpe_dummy_switch` 加一个普通双口 RAM。本讲解读的是这条「写好但暂未上线」的真实代码，并在最后说明它如何被重新接回主干。

## 2. 前置知识

### 2.1 为什么需要最长前缀匹配（LPM）

路由表里每条规则形如「前缀 `prefix` + 掩码 `mask` → 动作」。掩码决定关心多少位：`/8` 只看最高 8 位（很宽泛），`/24` 看最高 24 位（更具体），`/32` 看全部 32 位（精确到一台主机）。

一个目的 IP 往往同时匹配多条规则。例如 `192.168.1.100` 既匹配 `192.168.1.0/24`，也匹配 `192.168.0.0/16`，还匹配 `192.168.1.100/32`。路由的规矩是：**匹配的位越多越优先**，即选掩码中 1 最多的那条——这就是「最长前缀匹配」。

### 2.2 CAM 与 TCAM

- **CAM（Content Addressable Memory）**：按内容寻址的存储器。给一个 key，所有条目**同一拍并行比较**，命中者给出索引。相当于把「for 循环查找」做成了一拍完成的硬件。
- **TCAM（Ternary CAM）**：每比特除了 0/1 还有「don't care」，正好用来表达掩码——掩码为 0 的比特位「不在乎」。所以路由查找天然适合 TCAM。

代价是功耗和面积：64 条 TCAM 意味着 64 套 32 位比较器一拍全部工作。但本项目的目标场景路由表最多 64 条（见 `README.md`），在这个规模下并行 TCAM 比多拍树形查找更简单、延迟更低，代价可接受。

### 2.3 匹配判定的等价公式

对每条目 `k`，命中判定为：

\[
\text{hit}[k] \;=\; \big((\text{req\_ip} \;\&\; \text{mask}[k]) == (\text{prefix}[k] \;\&\; \text{mask}[k])\big)
\]

先把请求 IP 和表项前缀都「按掩码清零不关心的位」，再比较——等价于**只比较网络前缀那一段**。掩码为 0 的主机位被抹掉，不影响命中。

### 2.4 本讲在 DPE 中的位置

承接 u4-l1：DPE 是「5 输入 → 多路复用器 mux → 处理流水线 → 解复用器 demux → 5 输出」的三段式骨架。本讲的 IP 查找就位于「处理流水线」中，它读取包头里的目的 IP，把查表结果写进侧带元数据 `tuser_dst`（出口）和 `tid`（peer index），供下游 demux 据此分发。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [1.hw/ip.dpe/dpe_route_mem.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_route_mem.sv) | TCAM 表本体：存储 prefix/mask/dst/peer，做并行比较与优先编码。 |
| [1.hw/ip.dpe/dpe_egress_ip_lookup.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_egress_ip_lookup.sv) | 顶层包装：桥接 CSR 配置、跨 beat 缓存包头、驱动查表、回填元数据。 |
| [1.hw/ip.dpe/dpe.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv) | DPE 顶层。可见 TCAM 实例被注释、由 dummy_switch 顶替的 PoC 现状。 |
| [1.hw/ip.dpe/README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/README.md) | 该模块的设计说明，明确「降序排序 + 首匹配即最长」。 |
| [3.build/csr_build/csr.rdl](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl) | routing_table 的单一真源规格（external regfile）。 |
| [1.hw/ip.infra/dpe_pkg.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/dpe_pkg.sv) | 出口地址编码常量（0=CPU、1~4=eth、5/6 组播、7 广播）。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**①TCAM 并行比较**（在 `dpe_route_mem` 里把 64 条目一拍比完）、**②最长前缀匹配**（靠软件降序排序 + 硬件首匹配保证）、**③包缓冲与元数据赋值**（在 `dpe_egress_ip_lookup` 里协调 CSR、跨 beat 缓存、回填元数据）。

---

### 4.1 TCAM 并行比较

#### 4.1.1 概念说明

`dpe_route_mem` 是一张「路由存储 + 并行查找」二合一的表。它要同时服务两个主人：

- **控制面 CPU**：通过 CSR 接口逐条**写/读**表项（prefix、mask、peer、dst）。
- **数据面流水线**：给一个目的 IP，要求**一拍**给出命中结果。

「一拍给出结果」是关键。传统 RAM 查找要先给地址、再等数据，而且要按某种顺序遍历；CAM 的思路反过来——把请求 IP 广播给**所有**条目，让它们各自一拍内自报是否命中，再用一个编码器把「谁命中了」编成一个索引。

#### 4.1.2 核心流程

```text
                ┌──────────── 64 条目的存储 ────────────┐
   写/读(CSR) → │ cam_prefix[] cam_mask[] ram_dst[] ... │
                └──────────────────────────────────────┘
                              │ 每条目各拉一根比较线
   req_ip ─────┬──────────────┼────────────── ... ──────┐
               ▼              ▼                         ▼
            hit_vec[0]     hit_vec[1]    ...        hit_vec[63]
               └──────────────┴────────────── ... ──┘
                              ▼ 优先编码（升序扫描，首匹配胜）
                          best_idx / hit_found
                              ▼
              resp_dst / resp_peer / resp_bypass（或默认值）
```

写与查表互不阻塞：写用 `always_ff`（寄存器数组），查表用 `assign` + `always_comb`（纯组合），二者分属不同时钟路径但共用同一份存储数组。

#### 4.1.3 源码精读

**参数与存储结构**：默认 64 条目，每条目存 32 位 prefix、32 位 mask、3 位 dst、8 位 peer、1 位 bypass。

[dpe_route_mem.sv:51-57](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_route_mem.sv#L51-L57)：定义 `ENTRY_COUNT=64`、索引位宽 `ROUTE_IDX_W=$clog2(64)=6`，以及三种**默认动作**（未命中时用）。

[dpe_route_mem.sv:94-100](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_route_mem.sv#L94-L100)：五块存储数组。注意命名：带 `cam_` 前缀的参与并行比较（prefix/mask），带 `ram_` 前缀的是命中后按索引读取的动作字段（dst/peer/bypass）。

[dpe_route_mem.sv:108-113](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_route_mem.sv#L108-L113)：CPU 写入逻辑。四个独立的字节写使能（`we_prefix/we_mask/we_peer/we_dst`）分别写到各自数组，由上游按访问地址译码产生。

> 深读观察：`ram_bypass`（[L100](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_route_mem.sv#L100)）在这里**没有任何写使能**，写入逻辑里找不到它。这意味着 `resp_bypass`（[L160](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_route_mem.sv#L160)）读到的永远是没有被赋过值的存储，是个未启用字段。下游用 `USE_BYPASS_FROM_MEM=0` 规避了它（见 4.3.3）。这是真实代码里的一个「预留但未接线」的痕迹，不是笔误。

**并行比较（核心一行）**：用 `generate` 把同一份比较逻辑复制 64 份，每份独立比较一个条目。

[dpe_route_mem.sv:128-135](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_route_mem.sv#L128-L135)：这就是 §2.3 那个公式的硬件实现。`hit_vec[k]` 是一根 1 位线，64 根线在同一拍全部由组合逻辑算出，没有任何时钟等待。

> 深读观察：`req_valid`（[L81](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_route_mem.sv#L81)）虽然出现在端口表里，但模块体内**从未使用**它。也就是说，`resp_*` 一旦 `req_ip` 变化就会跟着变，查表是「常开」的组合逻辑，`req_valid` 只是给上游留的语义占位。

**优先编码（首匹配胜）**：在 64 根 `hit_vec` 线里选出最小索引。

[dpe_route_mem.sv:141-153](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_route_mem.sv#L141-L153) 用一个 `for` 循环从 `i=0` 升序扫描，配合 `!hit_found` 门控，**只记录第一次命中**的索引。这正是后面 4.2 节要讲的 LPM 关键。

[dpe_route_mem.sv:155-160](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_route_mem.sv#L155-L160)：命中时按 `best_idx` 从 `ram_*` 读动作字段；未命中时回退到三种默认值（默认发往 CPU、peer 0、不 bypass）。这就是「默认路由/丢弃」的处理方式。

#### 4.1.4 代码实践

**实践目标**：看清「并行比较」到底是不是真的 64 路同时工作。

**操作步骤**：

1. 打开 [dpe_route_mem.sv:130-135](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_route_mem.sv#L130-L135) 的 `generate ... for` 块。
2. 注意 `genvar k` 在综合时会被展开成 64 份独立的 32 位比较器，彼此**没有依赖、没有优先级**——这是「并行」的本质。
3. 再看 [L141-L153](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_route_mem.sv#L141-L153) 的优先编码 `for` 循环，它**有**先后（升序 + `!hit_found` 门控），所以是「比较并行、选索串行（但都组合）」。

**需要观察的现象**：比较用的是 `generate for`（空间展开，无优先级），选索用的是 `always_comb for`（行为级，靠门控实现首匹配）。两种 `for` 在综合后形态不同。

**预期结果**：理解 `hit_vec` 全部并行，`best_idx` 是组合逻辑链上的优先编码。待本地验证：若用 Vivado 综合此模块，可观察其关键路径是否经过这 64 路比较 + 编码树。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `generate for` 改成普通 `always_comb for` 来计算 `hit_vec`，功能会变吗？综合结果呢？

> **答案**：功能不变（都能算出 64 个命中位）。但 `always_comb` 里的循环在综合时可能被工具串行化推断、产生优先级逻辑；`generate for` 则明确展开成 64 个并行比较器，更贴近 TCAM 的并行意图，时序更可控。

**练习 2**：`route_req_ip` 是 32 位，但以太网包头里 IP 地址是大端（网络序）。`req_ip` 这 32 位应该理解成大端还是小端的数值？

> **答案**：应理解成「自然二进制数值」。上游 `dpe_egress_ip_lookup` 在提取时做了字节重排（见 4.3.3），把网络序的 4 个字节拼成一个数值正确的 32 位 `req_ip`（如 `192.168.1.1` → `0xC0A80101`），所以这里的 `& mask` 比较与软件路由表里 `ip & mask` 语义一致。

---

### 4.2 最长前缀匹配

#### 4.2.1 概念说明

§2.1 已说明：同一 IP 可能命中多条规则，要选掩码最长者。问题在于，硬件的优先编码器（4.1.3）是**按索引升序**选第一个命中，它本身**不懂前缀长度**。那它怎么保证选出最长前缀？

答案是软硬分工——**软件负责排序，硬件负责首匹配**：

- 控制面 CPU 在写路由表时，把条目按前缀长度**从长到短**（最具体的 `/32` 在索引 0，最宽泛的在末尾）排列。
- 硬件升序扫描、首匹配胜，于是「第一个命中」天然就是「前缀最长」的那条。

这样硬件就不需要理解掩码里 1 的个数，只管「谁排前面就选谁」。

#### 4.2.2 核心流程

```text
   软件配置阶段（CPU 经 CSR 写表）：
     按前缀长度降序填入 entry[0..63]
       entry[0]: /32  最具体 ──┐
       entry[1]: /24           │  升序索引 = 优先级
       ...                     │
       entry[N]: /0  默认路由 ──┘

   硬件查表阶段（每包一次）：
     hit_vec[0..63] 并行计算
     for i=0..63：第一个 hit_vec[i]=1 即 best_idx  ← 首匹配 = 最长前缀
```

关键不变量：**只要软件维持降序排序，首匹配就等于最长前缀匹配**。这个不变量在 [README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/README.md) 与 `dpe_route_mem.sv` 文件头的注释里都被明确写明。

[dpe_route_mem.sv:45-48](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_route_mem.sv#L45-L48)：文件头注释直接点明「Relies on software/driver to sort entries by Longest Prefix Match (LPM), where the most specific route is at Index 0」。

[README.md:17](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/README.md#L17)：说明排序由控制面 CPU 在经 CSR 更新路由表时完成。

#### 4.2.3 源码精读

LPM 在源码层面没有单独的「LPM 模块」，它是 `dpe_route_mem` 优先编码器（4.1.3 的 [L141-L153](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_route_mem.sv#L141-L153)）与软件排序约定的**组合效果**。这里只补充两点：

**未命中 = 默认路由**：当所有 `hit_vec` 都为 0（包括没有显式默认路由 `/0` 时），`hit_found=0`，[L155-L160](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_route_mem.sv#L155-L160) 让 `resp_dst/peer/bypass` 回退到三种参数默认值。通常把 `DEFAULT_DST_PORT` 配成某个物理口或 CPU，就实现了「缺省出口」。若想用显式默认路由，只需在表里加一条 `mask=0` 的条目（放在降序末尾），它对所有 IP 都命中，作为兜底。

**前缀长度由 mask 决定，不由字段显式存**：表里只存 `prefix` 和 `mask`，前缀长度等于 `mask` 中 1 的个数，软件据此排序。硬件从不数 1 的个数——这正是把 LPM 难题转嫁给软件排序的体现。

#### 4.2.4 代码实践

**实践目标**：手工模拟一次 LPM 命中，验证「降序排序 + 首匹配」确实选出最长前缀。

**给定路由表（已按前缀长度降序，索引即优先级）**：

| 索引 | prefix（点分十进制） | mask | 前缀长度 | dst | peer |
|------|----------------------|------|----------|-----|------|
| 0 | 192.168.1.100 | 255.255.255.255 (`/32`) | 32 | eth2 (3'd2) | 5 |
| 1 | 192.168.1.0 | 255.255.255.0 (`/24`) | 24 | eth2 (3'd2) | 5 |
| 2 | 192.168.0.0 | 255.255.0.0 (`/16`) | 16 | CPU (3'd0) | 0 |

**操作步骤**：对下面三个请求 IP，逐条套用公式 `hit[k] = ((req_ip & mask[k]) == (prefix[k] & mask[k]))`，升序找第一个命中。

**① 请求 `req_ip = 192.168.1.100`（`0xC0A80164`）**

- k=0：`(0xC0A80164 & 0xFFFFFFFF) == 0xC0A80164` → 命中 → `best_idx=0`（首匹配即停）。
- 结果：`dst=eth2`，`peer=5`。✓ 选中最具体的 `/32`。

**② 请求 `req_ip = 192.168.1.50`（`0xC0A80132`）**

- k=0：`0xC0A80132 == 0xC0A80164`？否。
- k=1：`(0xC0A80132 & 0xFFFFFF00)=0xC0A80100 == (0xC0A80100 & 0xFFFFFF00)=0xC0A80100` → 命中 → `best_idx=1`。
- 结果：`dst=eth2`，`peer=5`。✓ 命中 `/24`。

**③ 请求 `req_ip = 192.168.5.5`（`0xC0A80505`）**

- k=0：否；k=1：`(…& 0xFFFFFF00)=0xC0A80500 ≠ 0xC0A80100`，否；k=2：`(0xC0A80505 & 0xFFFF0000)=0xC0A80000 == 0xC0A80000` → 命中 → `best_idx=2`。
- 结果：`dst=CPU`，`peer=0`。✓ 命中 `/16`。

**预期结果**：三个请求分别落到索引 0/1/2，正是不走更宽泛规则、各取最长前缀。

**反例验证（理解排序为何关键）**：把上表的索引 0 和 2 对调（即 `/16` 排最前）。对请求 ①`192.168.1.100`：k=0 的 `/16` 先命中 → `best_idx=0`，于是返回 `/16` 的 `dst=CPU` 而非 `/32` 的 `eth2`。**这就是未排序会导致的错误**，印证了软件必须维持降序排序。

#### 4.2.5 小练习与答案

**练习 1**：如果两条规则前缀长度相同（例如两条不同网段的 `/24`），它们的相对顺序重要吗？

> **答案**：不重要。前缀长度相同意味着它们覆盖的网段互不重叠（否则就是配置冲突），一个 IP 不会同时命中两条同长度的规则，所以谁排前面不影响结果。

**练习 2**：硬件优先编码器选「最小索引」。若软件把最具体的规则放到了**最大**索引，会发生什么？

> **答案**：首匹配会选到一个较宽泛（较短前缀）的条目，查表结果错误。这违反了 [README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/README.md) 与文件头注释规定的不变量，是配置 Bug。

---

### 4.3 包缓冲与元数据赋值

#### 4.3.1 概念说明

`dpe_egress_ip_lookup` 是 `dpe_route_mem` 的「经纪人」，它把查表能力包装成 DPE 流水线里的一个处理级。它同时做三件看似不相关的事：

1. **控制面桥接**：CPU 经 CSR 的 `routing_table` external regfile 读写表项，这里负责把 CSR 的请求译码成 `dpe_route_mem` 的写/读接口。
2. **跨 beat 缓存包头（store-and-forward）**：128 位 AXIS 一个 beat 只装 16 字节，而 IPv4 目的地址落在包头的第 2、3 个 beat 里。查表又需要 IP 拼齐才能发起。所以要把前几个 beat 暂存起来，等 IP 拼齐、查表完成后再继续往下发。
3. **回填元数据**：查表结果（dst/peer/bypass）要写到输出 AXIS 的侧带信号 `tuser_dst`/`tid`/`tuser_bypass_stage`，让下游 demux 据此分发。

#### 4.3.2 核心流程（FSM）

```text
S_IDLE  ──来包──┬─ bypass_all=1 ? ──是──> 直接用入侧元数据，flush_limit=0
                │                          │
                │ 否                       ▼
                └─> S_COLLECT（收第1~2个beat，拼出完整 req_ip，存入 beat_buf）
                                  │
                                  ▼
                           S_DECIDE（发 req_valid，组合查表一拍出结果，锁存 dst/peer）
                                  │
                                  ▼
                           S_FLUSH（把 beat_buf 里缓存的头几个 beat 重发出去，带新元数据）
                                  │ 缓存清空后
                                  ▼
                           S_FWD（剩余 beat 直通，元数据已定）──tlast──> S_IDLE
```

关键点：查表本身是组合的、零拍延迟，但发起查表前必须先收齐 IP（跨 2 个 beat），所以引入 `S_COLLECT` 的暂存；查表后又要重发暂存的 beat，所以有 `S_FLUSH`。

#### 4.3.3 源码精读

**CSR 桥接：输入多路、输出分路**

[dpe_egress_ip_lookup.sv:62-67](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_egress_ip_lookup.sv#L62-L67)：端口用 `hwif_out`/`hwif_in` 两个数组，每个元素对应一条路由表项——这是 `external regfile` 在 RTL 侧的形态（承接 u3-l1）。

[dpe_egress_ip_lookup.sv:83-99](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_egress_ip_lookup.sv#L83-L99)：输入多路器。扫描 64 个表项，找出当前有 `req` 的那个，记下 `mem_idx`（哪条表项）、`addr_offset`（表项内哪个字段）、`is_wr`、`wr_data`。CSR 同一时刻只应有一个表项在请求。

[dpe_egress_ip_lookup.sv:104-138](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_egress_ip_lookup.sv#L104-L138)：输出分路与写译码。用 `addr_offset[3:2]` 区分 4 个字段：`2'b00`→prefix、`2'b01`→mask、`2'b10`→peer、`2'b11`→dst，分别拉高对应的 `we_*` 写使能送给 `dpe_route_mem`。读应答按同样偏移把 `rdata_*` 选回到对应表项的 `rd_data`。

> 对照 csr.rdl：[csr.rdl:527-579](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L527-L579) 里每条 `entry` 也正好是 4 个 reg（ip、mask、peer_idx、dst），与上面 4 个偏移一一对应。注意 csr.rdl 的 `peer_idx` 是 `[5:0]`（6 位），而 RTL 的 `ram_peer`/`wdata_peer` 是 8 位——规格与实现存在轻微位宽差异（规格更紧），使用时应以 csr.rdl 为准避免越界。

**`dpe_route_mem` 实例化**

[dpe_egress_ip_lookup.sv:148-171](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_egress_ip_lookup.sv#L148-L171)：把写/读接口接 CSR 桥接的结果，把 `req_ip`/`req_valid` 接数据面 FSM 的结果，把 `resp_*` 接回 FSM。

**跨 beat 包缓冲**

[dpe_egress_ip_lookup.sv:183-197](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_egress_ip_lookup.sv#L183-L197)：定义 `beat_t`（一个 128 位 beat 的全字段快照）和 `beat_buf[0:2]`——一个 3 项的循环缓冲，用来暂存包头的前 3 个 beat。

**字节序与 IP 提取（重点）**：以太网帧头是 14 字节（6 目的 MAC + 6 源 MAC + 2 EtherType），IP 头从第 15 字节开始，而 IPv4 目的地址（DA）在 IP 头里偏移 16 字节，即帧的第 30~33 字节。每个 128 位 beat = 16 字节，所以：

- beat 0：帧字节 0~15
- beat 1：帧字节 16~31 → DA 的高 2 字节（DA[3]、DA[2]）落在本 beat 的第 14、15 字节位置
- beat 2：帧字节 32~47 → DA 的低 2 字节（DA[1]、DA[0]）落在本 beat 的第 0、1 字节位置

又因为 AXIS `tdata` 是**小端**打包（第 `p` 字节占据 `tdata[p*8+7 : p*8]`），而网络头是**大端（网络序）**，所以要把字节顺序「反过来」拼，才能得到数值正确的 `req_ip`：

[dpe_egress_ip_lookup.sv:320-334](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_egress_ip_lookup.sv#L320-L334)：在 `S_COLLECT` 里逐 beat 提取：

- beat 1：`ipv4_da[31:24] <= tdata[119:112]`（DA[3]，最高字节），`ipv4_da[23:16] <= tdata[127:120]`（DA[2]）。
- beat 2：`ipv4_da[15:8] <= tdata[7:0]`（DA[1]），`ipv4_da[7:0] <= tdata[15:8]`（DA[0]，最低字节）。

这样拼出的 `ipv4_da`/`route_req_ip` 是数值正确的 32 位 IP（如 `192.168.1.100` → `0xC0A80164`），可以直接送进 §2.3 的比较公式。

**FSM：DECIDE 触发查表**

[dpe_egress_ip_lookup.sv:257-264](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_egress_ip_lookup.sv#L257-L264)：进入 `S_DECIDE` 时拉高 `route_req_valid` 一拍，把组合查表结果 `route_dst/route_peer/route_bypass` 锁存进 `decided_*`。注意 `decided_bypass` 由参数 `USE_BYPASS_FROM_MEM` 选择：默认 `1'b0`，取入侧的 `tuser_bypass_stage`（这正是 4.1.3 里那个未接线 `ram_bypass` 的规避措施）。

**FSM：FLUSH 重发缓存 + 直通**

[dpe_egress_ip_lookup.sv:266-280](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_egress_ip_lookup.sv#L266-L280)：`S_FLUSH` 把 `beat_buf` 里暂存的头几个 beat 重发（此时查表已完成）；缓存清空后进入 `S_FWD`，剩余 beat 直接从入侧搬到出侧，直到 `tlast` 回 `S_IDLE`。

**输出元数据赋值（本讲的「产物」）**

[dpe_egress_ip_lookup.sv:369-382](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_egress_ip_lookup.sv#L369-L382)：FLUSH 期从 `beat_buf` 取数据，FWD 期直通入侧；无论哪期，侧带元数据都用**查表后的** `decided_dst`/`decided_peer`/`decided_bypass` 覆盖：

```systemverilog
assign m_axis.tuser_dst          = decided_dst;   // 出口（送 demux 分发）
assign m_axis.tid                = decided_peer;  // peer index（送 WG 加解密选密钥）
assign m_axis.tuser_bypass_stage = decided_bypass;
```

这正是本讲与 u4-l1/u4-l3 的衔接点：本模块把路由决策结果写进 `tuser_dst`（权威目的）和 `tid`（peer index），下游 demux 按 `tuser_dst` 分发，加密级按 `tid` 选 peer 密钥。

**bypass_all 快速通路**：[dpe_egress_ip_lookup.sv:224-234](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_egress_ip_lookup.sv#L224-L234)，若入侧 `tuser_bypass_all=1`，在 `S_IDLE` 直接采用入侧元数据、`flush_limit=0`，跳过 COLLECT/DECIDE，只 FLUSH 第一个 beat 后即转 FWD——即「本处理级放行、不查表」。

#### 4.3.4 代码实践

**实践目标**：确认「PoC 现状」——这条 TCAM 查找链在顶层确实未上线，看清它如何被接回。

**操作步骤**：

1. 打开 [dpe.sv:78-92](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv#L78-L92)：可见当前**实际在线**的处理级是 `dpe_dummy_switch`（直通）。
2. 看 [dpe.sv:94-103](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv#L94-L103)：`dpe_egress_ip_lookup` 的实例化**整段被注释**。
3. 看 [dpe.sv:105-121](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv#L105-L121)：当前 `routing_table` 落地成一个普通 `tdp_ram`（8 位地址），CPU 可读写，但数据面并不读取它做查表——与 4.1~4.2 描述的 TCAM 查找不同。
4. 想象「上线」操作：去掉 [L95-L103](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv#L95-L103) 的注释、把 `u_egress.s_axis/m_axis` 接到 `muxed_1`/`muxed_2`、并替换掉 dummy_switch，即可让本讲的查表链生效。

**需要观察的现象**：当前 `dpe.sv` 里 `muxed_1 → dummy_switch → muxed_2` 是纯直通；TCAM 那条线只在注释里。这说明 u4-l2/u4-l3 里数据包的 `tuser_dst` 是由 CPU/上游预先设好的，而非硬件查表得出。

**预期结果**：理解本讲解读的代码是「写好待上线」状态。综合当前的 `top.filelist` 产出的 bitstream 不会包含这条 TCAM。

#### 4.3.5 小练习与答案

**练习 1**：为什么查表前必须先 `S_COLLECT` 两个 beat，而不是收到第一个 beat 就查？

> **答案**：IPv4 目的地址分散在第 2、3 个 beat（帧字节 30~33）里，单个 beat 拿不全 32 位 DA。必须收齐 DA 的 4 个字节才能拼出 `req_ip` 发起查表，所以需要暂存前几个 beat。

**练习 2**：`beat_buf` 是 3 项，`flush_limit` 在 DECIDE 后设为 `2`。如果来了一个总长只有 1 个 beat 的超短包（`tlast` 在第 0 beat 就拉高），FSM 还能正确处理吗？

> **答案**：`S_FLUSH` 的退出条件之一是 `beat_buf[buf_rd_ptr].last`（[L268](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_egress_ip_lookup.sv#L268)）。若第 0 beat 就带 `last`，FLUSH 重发它后立即回 `S_IDLE`，`flush_limit` 不影响提前结束。不过这种超短包不含完整 IP 头，DA 拼不全，查表结果未必有意义——实际以太网最小帧 64 字节，不会出现 1 beat 的合法 IP 包。

## 5. 综合实践

把三个模块串起来，做一次「端到端纸面推演」。

**场景**：路由表如下（已降序排序）：

| idx | prefix | mask | dst | peer |
|-----|--------|------|-----|------|
| 0 | 10.0.0.0 | 255.0.0.0 (`/8`) | eth1 (3'd1) | 2 |
| 1 | 0.0.0.0 | 0.0.0.0 (`/0`，默认路由) | CPU (3'd0) | 0 |

**任务**：

1. 一个目的 IP 为 `10.1.2.3` 的包进入 `dpe_egress_ip_lookup`。画出它经历 `S_IDLE → S_COLLECT → S_DECIDE → S_FLUSH → S_FWD` 时，`beat_buf`、`route_req_ip`、`decided_dst`、`decided_peer` 各自的变化。
2. 手算 `hit_vec[0]` 与 `hit_vec[1]`，确认 `best_idx` 与最终 `m_axis.tuser_dst`、`m_axis.tid`。
3. 再对一个目的 IP 为 `192.168.0.1` 的包重复一遍，确认它落到默认路由（idx 1）。

**参考答案**：

- 包 `10.1.2.3`（`0x0A010203`）：
  - COLLECT 阶段拼出 `route_req_ip = 0x0A010203`。
  - DECIDE：`hit_vec[0] = ((0x0A010203 & 0xFF000000)=0x0A000000 == (0x0A000000 & 0xFF000000)=0x0A000000)` → 命中；首匹配 → `best_idx=0`。
  - 结果：`decided_dst = eth1 (3'd1)`，`decided_peer = 2` → `m_axis.tuser_dst=1`、`m_axis.tid=2`。
- 包 `192.168.0.1`（`0xC0A80001`）：
  - `hit_vec[0]`：`(0xC0A80001 & 0xFF000000)=0xC0000000 ≠ 0x0A000000` → 不命中。
  - `hit_vec[1]`：`(0xC0A80001 & 0x00000000)=0 == 0` → 命中（`/0` 对所有 IP 命中）→ `best_idx=1`。
  - 结果：`decided_dst = CPU (3'd0)`，`decided_peer = 0`。

这个练习把「跨 beat 缓存拼 IP → 并行比较 → 首匹配选最长前缀 → 回填元数据」整条链一次走完。

## 6. 本讲小结

- 路由查找用**并行 TCAM**：`dpe_route_mem` 用 `generate for` 把 64 条目的 32 位比较一拍全部算出（`hit_vec`），优先编码器升序选首匹配。
- **最长前缀匹配**靠软硬分工：软件按前缀长度降序写表，硬件「首匹配即最长」，硬件本身不数掩码中 1 的个数。
- 匹配判定公式 `((req_ip & mask) == (prefix & mask))` 等价于「只比网络前缀段」；未命中回退到三种参数默认值（默认出口/peer/bypass）。
- `dpe_egress_ip_lookup` 是个三合一包装：CSR 桥接（external regfile 4 字段译码）、跨 128 位 beat 的 3 项循环缓冲（store-and-forward）、把查表结果写进 `tuser_dst`/`tid` 侧带元数据。
- **字节序陷阱**：AXIS `tdata` 小端、网络头大端，DA 跨 beat 1/2，提取时必须按 `tdata[119:112]/[127:120]/[7:0]/[15:8]` 反序拼成数值正确的 `req_ip`。
- **PoC 现状**：这条完整的 TCAM 查找链源码已写好，但在 `dpe.sv` 中被注释，当前实际跑的是 `dpe_dummy_switch` 直通 + 普通 `tdp_ram`；当前 bitstream 里数据包的 `tuser_dst` 由上游预设，并非硬件查表得出。

## 7. 下一步学习建议

- 下一讲 **u4-l5（WireGuard 封装/解封装与加解密数据流）** 会进入处理流水线的下一级——拿到本讲填好的 `tid`（peer index）去选密钥，做 WG 解封装/加解密/再封装。建议先回顾 u4-l1 的 `tuser_dst`/`tid` 元数据定义。
- 若想加深对「external regfile 如何在 RTL 落地」的理解，可结合 u3-l1（csr.rdl 的 routing_table 规格）与本讲的 CSR 桥接代码对照阅读，理解 PeakRDL 生成的 `hwif_out`/`hwif_in` 数组接口如何被手写代码消费。
- 对查表时序感兴趣的读者，可在本地用 Vivado 综合 `dpe_route_mem`，观察 64 路并行比较 + 优先编码树的关键路径长度，体会 README 里「TCAM 功耗/关键路径代价」这句话的物理含义。
