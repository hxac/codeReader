# csb_master：中央配置路由器

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `csb_master` 在 NVDLA 配置链中的「中央枢纽」地位：它是单一 CSB 端口扇出到所有引擎的唯一分发点。
- 读懂它如何用 **4KB 对齐的地址译码** 把请求分发到 glb/gec/mcif/cvif/bdma/cdma/csc/cmac_a/cmac_b/cacc/sdp/sdp_rdma/pdp/pdp_rdma/cdp/cdp_rdma/rubik 共 17 个引擎寄存器口。
- 解释多路响应如何用 **OR 汇拢 + zero-one-hot 断言** 安全合并回单一响应通路。
- 理解 falcon（配置时钟）↔ core（核心时钟）两个时钟域之间由 **一对异步 FIFO** 桥接，并用格雷码指针做跨域握手。

本讲承接 [u2-l1 CSB 总线协议与 apb2csb 桥](u2-l1-csb-bus-apb2csb.md)。上一讲讲的是「CSB 请求/响应包长什么样、apb2csb 怎么把 APB 翻译成 CSB」；本讲往下走一层，看 CSB 包**进入芯片之后**由谁转发给十几个引擎。

## 2. 前置知识

- **CSB（Configuration Space Bus）**：CPU 编程 NVDLA 各引擎寄存器的唯一入口，详细握手见上一讲。请求包 `{nposted, write, wdat[31:0], addr[15:0]}`，响应包 `{type, error, data[31:0]}`。
- **valid/ready 握手**：发送方拉 `valid`，接收方拉 `ready`，同一时钟沿两者都为高时数据才算传递一次（一次 "pop"）。
- **时钟域与跨时钟域（CDC）**：NVDLA 至少有两个时钟——`nvdla_falcon_clk`（配置/CSB 时钟）与 `nvdla_core_clk`（核心计算时钟）。信号从一个时钟域进另一个时钟域，必须经过同步器或异步 FIFO，否则会采到亚稳态。
- **地址译码（address decode）**：把一段地址空间切成若干子区间，每个子区间对应一个下游设备，用地址高位比较来选中某一个。
- **影偶（shadow）/寄存器文件**：每个引擎内部都有一组 CSB 寄存器（`_CSB_reg.v` / `_dual_reg.v`），csb_master 不关心寄存器细节，只负责把请求送到对应引擎的寄存器口。

> 一句话定位：csb_master 是 NVDLA 配置空间的「**1 进 17 出**」路由器，外加一对跨时钟域 FIFO。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `vmod/nvdla/csb_master/NV_NVDLA_csb_master.v` | 主体：端口、地址译码分发、多路响应合并、对外 CSB 端口寄存器。本讲绝大部分内容出自这里。 |
| `vmod/nvdla/csb_master/NV_NVDLA_CSB_MASTER_falcon2csb_fifo.v` | **请求**异步 FIFO：falcon→core，4 深 ×50 bit，带格雷码指针与 SLCG 门控。 |
| `vmod/nvdla/csb_master/NV_NVDLA_CSB_MASTER_csb2falcon_fifo.v` | **响应**异步 FIFO：core→falcon，2 深 ×34 bit，结构同上。 |
| `vmod/nvdla/top/NV_NVDLA_partition_o.v` | 例化点：第 1771 行 `NV_NVDLA_csb_master u_NV_NVDLA_csb_master (...)`，把 csb_master 与各引擎连起来；其中 `csb2gec_*` 在第 2482 行被送入 `NV_NVDLA_glb`。 |

> 提醒：csb_master 的两个 FIFO 文件名里 "falcon2csb" / "csb2falcon" 的 "csb" 指的是 csb_master 模块本身（它在 core 域），不是指 falcon 域的 CSB 端口。看实例名更直观：`u_fifo_csb2nvdla`（请求进）和 `u_fifo_nvdla2csb`（响应出）。

## 4. 核心概念与源码讲解

本讲的三个最小模块：

1. **CSB 地址译码分发**（请求方向：1 路 → 17 路）
2. **多路响应合并**（响应方向：19 路 → 1 路）
3. **跨时钟域 FIFO**（falcon ↔ core 的两座桥）

它们在数据流上的相对位置如下：

```
                  falcon 域                          core 域
 CPU ──csb2nvdla──► [falcon2csb FIFO] ──core_req──► [地址译码] ──csb2*_req──► 17 个引擎
 (请求)            (请求, 4深)        (1路)        (1→17 出)                   寄存器口

 CPU ◄─nvdla2csb── [csb2falcon FIFO] ◄─core_resp── [响应OR合并] ◄──*2csb_resp── 17 个引擎
 (响应)            (响应, 2深)        (1路)        (19→1 入)                    + dummy
```

---

### 4.1 CSB 地址译码分发

#### 4.1.1 概念说明

外部 CPU 只看到**一根** CSB 端口（`csb2nvdla_*`），但 NVDLA 内部有十几个独立引擎（卷积的 CDMA/CSC/CMAC/CACC、后处理的 SDP/PDP/CDP、存储的 MCIF/CVIF/BDMA、全局的 GLB、重排的 Rubik……），每个引擎都有自己的寄存器组。csb_master 的第一项职责就是**地址译码**：根据请求里的地址，把这一次访问送到唯一正确的引擎。

NVDLA 给每个引擎划分了 **4KB（0x1000）的寄存器地址窗口**，引擎基址按 4KB 对齐排列。csb_master 用地址的最高若干位做一次相等比较，命中哪个引擎就把请求送过去；一个都不命中则送给「dummy 客户端」，保证访问未映射地址也不会挂死总线。

#### 4.1.2 核心流程

请求方向的处理（全部在 `nvdla_core_clk` 域）：

1. 请求从 falcon 域经请求 FIFO 进入 core 域，得到 `core_req_pvld` / `core_req_pd`（见 4.3）。
2. `core_req_prdy` 恒为 1（见 [NV_NVDLA_csb_master.v:468](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L468-L468)），所以只要 `core_req_pvld` 有效，本拍就「pop」一次：`core_req_pop_valid = core_req_pvld & core_req_prdy`（[L604](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L604-L604)）。
3. 把字地址左移 2 位还原成字节地址：`core_byte_addr = {core_req_addr, 2'b0}`（[L608](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L608-L608)）。
4. 构造掩码 `addr_mask`，只保留地址的高 6 位（bit[17:12]，即 4KB 页号）：`addr_mask = {6{1'b1}}, {12{1'b0}}` = `0xFC000`（[L621](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L621-L621)）。
5. 每个引擎一个 `select_xxx = ((core_byte_addr & addr_mask) == 32'h0000X000)` 相等比较，同一拍有且仅有一个 select 为真（或都不命中→dummy）。
6. 命中的引擎用一个 1 深的 valid/ready 暂存器把请求接住，再以 `csb2xxx_req_pvld/prdy/pd` 送给该引擎。

地址译码表（按基址排序，全部来自源码实测）：

| 引擎 | 基址 | select 比较语句 | 备注 |
|------|------|-----------------|------|
| GLB | 0x0000 | [L1032](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L1032-L1032) | 全局配置/中断 |
| GEC | 0x1000 | [L837](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L837-L837) | 由 `NV_NVDLA_glb` 接收（见 partition_o:2482），GLB 的第二组寄存器窗 |
| MCIF | 0x2000 | [L1552](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L1552-L1552) | 主存接口 |
| CVIF | 0x3000 | [L1097](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L1097-L1097) | CVSRAM 接口 |
| BDMA | 0x4000 | [L1422](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L1422-L1422) | 桥 DMA |
| CDMA | 0x5000 | [L1292](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L1292-L1292) | 卷积取数 |
| CSC  | 0x6000 | [L772](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L772-L772) | 卷积分发 |
| CMAC_A | 0x7000 | [L642](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L642-L642) | MAC 阵列 A 半 |
| CMAC_B | 0x8000 | [L1162](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L1162-L1162) | MAC 阵列 B 半 |
| CACC | 0x9000 | [L967](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L967-L967) | 累加器 |
| SDP_RDMA | 0xA000 | [L707](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L707-L707) | SDP 读 DMA |
| SDP  | 0xB000 | [L1357](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L1357-L1357) | 单点后处理 |
| PDP_RDMA | 0xC000 | [L1487](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L1487-L1487) | PDP 读 DMA |
| PDP  | 0xD000 | [L1227](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L1227-L1227) | 池化 |
| CDP_RDMA | 0xE000 | [L1617](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L1617-L1617) | CDP 读 DMA |
| CDP  | 0xF000 | [L902](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L902-L902) | LRN |
| Rubik | 0x10000 | [L1682](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L1682-L1682) | 数据重排 |

合计 **17 个** `csb2*_req_pvld` 输出端口（与模块端口列表 [L27-L111](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L27-L111) 一致）。

#### 4.1.3 源码精读

**① 请求打包与字节地址还原。** 外部 `csb2nvdla_*` 五个信号被打包成 50 位 `csb2nvdla_pd`，再跨域进 core 域拆回 `core_req_*`：

[NV_NVDLA_csb_master.v:452-452](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L452-L452) —— 把 `{nposted, write, wdat[31:0], addr[15:0]}` 打包成 50 位。

[NV_NVDLA_csb_master.v:596-608](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L596-L608) —— 在 core 域拆出 `core_req_addr/write/nposted`，并 `core_byte_addr = {core_req_addr, 2'b0}` 把字地址还原成字节地址（注释也说明：CSB 给的是字对齐地址，译码需要字节地址）。

**② 一个引擎的完整译码+暂存模板（以 CMAC_A 为例）。** 17 个引擎的代码几乎是复制粘贴的同一段模板，看懂这一个就看懂全部：

[NV_NVDLA_csb_master.v:638-700](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L638-L700) —— CMAC_A 的译码与请求暂存，关键四步：

- `select_cmac_a = ((core_byte_addr & addr_mask) == 32'h00007000)`（L642）：地址命中 0x7000 页。
- `cmac_a_req_pvld_w`（L652-655）与 `csb2cmac_a_req_pvld_w`（L662-665）：一组「set/clear」式 next-state 逻辑，构成 1 深的 skid buffer——命中且 pop 时拉高，下游 `csb2cmac_a_req_prdy` 接走后清零。
- `csb2cmac_a_req_en`（L672）：数据捕获使能，决定何时把请求内容锁存。
- `csb2cmac_a_req_pd`（L700）：把锁存的请求重新打包成 63 位送给 CMAC_A 的寄存器口。

**③ dummy 客户端——未命中地址的兜底。** 这是容易被忽略但很重要的一笔：

[NV_NVDLA_csb_master.v:1763-1781](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L1763-L1781) —— `select_dummy = ~(所有 select 之或)`，即 17 个引擎都不命中时为真。

[NV_NVDLA_csb_master.v:1834-1838](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L1834-L1838) —— dummy 永远回一个 `data=0, error=0` 的响应，且 `dummy_resp_valid_w = csb2dummy_req_pvld & (nposted | read)`：读访问或非投递写都会立即给响应。**效果：访问未映射地址不会让总线挂死**，CPU 也能拿到一个确定的完成信号。

> 注意：还有一个 `afbif`（AF 接口）通路，在本仓库里被强制关闭：`select_afbif = 1'b0`（[L625](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L625-L625)）、`afbif_resp_pvld = 1'b0`（[L2371](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L2371-L2371)）。它只在响应合并里占一个「永远为 0」的席位，实际不参与译码。

#### 4.1.4 代码实践

**实践目标**：亲手统计 csb_master 的扇出规模，验证「1 进 17 出」的说法，并理解未命中地址的行为。

**操作步骤**：

1. 打开 `vmod/nvdla/csb_master/NV_NVDLA_csb_master.v`。
2. 在模块端口列表（L12-L112）里，数 `output ... csb2*_req_pvld` 的个数（即向引擎输出的请求 valid）。预期是 17 个：glb、gec、mcif、cvif、bdma、cdma、csc、cmac_a、cmac_b、cacc、sdp_rdma、sdp、pdp_rdma、pdp、cdp_rdma、cdp、rbk。
3. 用编辑器搜索 `select_` 开头的赋值，把每个引擎的基址填进上面的译码表，核对一遍。
4. 思考：若 CPU 向一个**不在表里**的地址（例如 0x12000，介于 Rubik 0x10000 之后）发起一次读访问，会选中谁？响应是什么？

**需要观察的现象**：

- 端口数应为 **17** 个 `csb2*_req_pvld`。
- 0x12000 不会命中任何引擎的 select，因而 `select_dummy` 为真，dummy 客户端立即返回 `data=0, error=0` 的读响应——总线不会挂起。

**预期结果**：你应当得到一张与上表一致的 17 行译码表，并能解释 dummy 兜底机制。

**待本地验证**：若要实际观察 dummy 响应，可在仿真里向未映射地址发一次 CSB 读（参考 [u1-l4](u1-l4-first-simulation.md) 的 trace-player 流程），在波形上确认 `nvdla2csb_valid` 仍会拉高一次且 `nvdla2csb_data` 为 0。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `core_req_prdy` 可以恒为 1（[L468](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L468-L468)），csb_master 却不会丢请求？

**答案**：因为每一个被 pop 的请求都会被**命中引擎的 1 深 skid buffer**（如 `cmac_a_req_pvld` 那组寄存器）接住；即便下游引擎暂时不 ready，请求也已被锁存进 csb_master 内部，core_req 侧就可以立刻接收下一个。所以 core_req 侧不必反压。

**练习 2**：地址 `0x0000B004` 会送到哪个引擎？为什么？

**答案**：送 SDP（基址 0xB000）。`core_byte_addr & addr_mask` 只看 bit[17:12] = `0xB`，低 12 位（含 `0x004` 这个寄存器偏移）被掩掉，因此整页 0xB000–0xBFFF 都归 SDP。

---

### 4.2 多路响应合并

#### 4.2.1 概念说明

请求是「1 进 17 出」，响应则是反过来的「19 进 1 出」——17 个引擎、外加 dummy、外加关闭的 afbif，每一路都可能产生响应。csb_master 必须把这些响应汇拢成一根 `core_resp_*` 通路，再经响应 FIFO 送回 falcon 域的 `nvdla2csb_*` 端口。

合并的难点是**安全性**：同一拍若有两个引擎同时给响应，简单的按位 OR 会让数据撞在一起出错。csb_master 的设计前提是**任意一拍至多一个引擎响应**（因为请求是串行 pop、逐个下发的），并用一个 **zero-one-hot 断言**在仿真期持续守护这一不变式。

#### 4.2.2 核心流程

响应方向（core 域）：

1. 每个引擎回送的 `xx2csb_resp_valid` / `xx2csb_resp_pd` 先各自**打一拍寄存器**对齐时序（例如 CMAC_A 见 [L1911-L1927](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L1911-L1927)）。
2. `core_resp_pd` 用带掩码的按位 OR 把 19 路响应数据合并：某路 valid 为真时其数据被选中，否则贡献全 0。
3. `core_resp_pvld` 是 19 路 valid 的简单 OR。
4. 一个 `nv_assert_zero_one_hot` 断言（宽度 19）保证任意拍最多一路 valid。
5. `core_resp_*` 写入响应 FIFO（csb2falcon），跨域到 falcon 域。
6. 在 falcon 域，按响应包的 `type` 位（bit[33]）区分：`type=0` 是读响应 → 驱动 `nvdla2csb_valid/data`；`type=1` 是写完成 → 驱动 `nvdla2csb_wr_complete`。

响应包格式（34 位）与上一讲一致：

| 位 | 含义 |
|----|------|
| [31:0] | data（读返回数据） |
| [32]   | error |
| [33]   | type（0=读响应，1=写完成） |

#### 4.2.3 源码精读

**① 19 路 OR 合并响应数据与有效位。**

[NV_NVDLA_csb_master.v:2234-2252](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L2234-L2252) —— `core_resp_pd`：每一项形如 `({34{xxx_resp_valid}} & xxx_resp_pd)`，valid 为真才选中该路数据，19 路相或。这正是「至多一路 valid」前提下安全的合并方式。

[NV_NVDLA_csb_master.v:2254-2272](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L2254-L2272) —— `core_resp_pvld`：19 路 valid 的纯 OR。

**② zero-one-hot 断言守护不变式。**

[NV_NVDLA_csb_master.v:2303-2303](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L2303-L2303) —— `nv_assert_zero_one_hot` 把 19 路响应 valid 拼成一条向量，断言它「全 0 或恰好一个 1」。一旦仿真里出现两路同时响应，会立即报 "Error! Multiple response!"。这条断言是整个 OR 合并方案的安全锚。

**③ 在 falcon 域按 type 位分拆读/写响应。**

[NV_NVDLA_csb_master.v:486-488](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L486-L488) —— `nvdla2csb_rresp_is_valid`（type=0，读响应）与 `nvdla2csb_wresp_is_valid`（type=1，写完成）。

[NV_NVDLA_csb_master.v:538-571](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L538-L571) —— 三个 always 块把读响应驱动成 `nvdla2csb_valid`/`nvdla2csb_data`，把写完成驱动成 `nvdla2csb_wr_complete`。注意它们都工作在 `nvdla_falcon_clk` 域。

#### 4.2.4 代码实践

**实践目标**：理解响应如何从 17 个引擎「汇拢」回单一 `nvdla2csb_data`。

**操作步骤**：

1. 在 `NV_NVDLA_csb_master.v` 定位 [L2234-L2252](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L2234-L2252) 的 `core_resp_pd` 赋值。
2. 数一数这个 OR 表达式里有多少个被 `&` 掩码的项（应为 19：afbif + 17 引擎 + dummy）。
3. 跟踪其中一路（例如 `cacc_resp_pd`）：往上找到 [L2006-L2022](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L2006-L2022)，确认 CACC 的 `cacc2csb_resp_*` 先被打了一拍寄存器才进入合并。
4. 顺着 `core_resp_*` 进入 [L470-L482](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L470-L482) 的响应 FIFO，再追到 [L538-L571](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L538-L571) 的 `nvdla2csb_*` 输出寄存器。
5. 用一段话写出：一次 CACC 读寄存器的响应，从 `cacc2csb_resp_valid` 到 `nvdla2csb_valid` 经过了哪几级寄存器、跨了哪个时钟域。

**需要观察的现象**：响应数据在 core 域先打一拍、合并、进响应 FIFO、在 falcon 域再驱动输出；中间恰好**跨越一次 core→falcon 时钟域**。

**预期结果**：你能画出 `cacc2csb_resp → cacc_resp(寄存器) → core_resp(OR) → 响应FIFO → nvdla2csb_resp → nvdla2csb_valid/data` 这条完整链路，并指出 FIFO 是域边界。

#### 4.2.5 小练习与答案

**练习 1**：既然用 OR 合并，为什么不会出现两个引擎的数据「撞」在一起？

**答案**：因为请求是 csb_master 逐拍、逐个下发的，同一时刻只有一个引擎在处理请求并产生响应；加上 `nv_assert_zero_one_hot` 断言保证「至多一路 valid」，所以 OR 合并是安全的。若该不变式被破坏，断言会在仿真期立即报错。

**练习 2**：读响应和写完成信号在对外端口上是同一根线吗？

**答案**：不是。它们共用 34 位响应包，但在 falcon 域按 `type` 位（bit[33]）分拆：读响应走 `nvdla2csb_valid` + `nvdla2csb_data`，写完成走单独的 `nvdla2csb_wr_complete`（见 [L486-L488](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L486-L488) 与 [L538-L571](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L538-L571)）。

---

### 4.3 跨时钟域 FIFO

#### 4.3.1 概念说明

CSB 对外端口（`csb2nvdla_*` / `nvdla2csb_*`）工作在 **falcon 时钟域**（见端口注释 [L120-L121](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L120-L121)），而 csb_master 的译码、分发、各引擎寄存器都工作在 **core 时钟域**。两个时钟异步，必须跨域。

csb_master 用**一对异步 FIFO** 解决：

- **请求 FIFO**（实例 `u_fifo_csb2nvdla`，模块 `falcon2csb_fifo`）：falcon 写、core 读，搬运 50 位请求包，4 深度。
- **响应 FIFO**（实例 `u_fifo_nvdla2csb`，模块 `csb2falcon_fifo`）：core 写、falcon 读，搬运 34 位响应包，2 深度。

这两个 FIFO 都是 NVDLA 内部 FIFO 生成器（fifogen）产出的标准异步 FIFO：用 **格雷码（Gray code）读/写指针** 跨域比较，用 `p_STRICTSYNC3DOTM_C_PPP` 严格同步器打两拍，并带 SLCG（二级时钟门控）省电。

> 为何用格雷码？多位指针跨时钟域采样时，若用普通二进制计数，多位同时翻转容易采到中间值；格雷码每次只翻转一位，跨域比较「相等/不等」时即使采到旧值也至多差一个刻度，不会出错。FIFO 源码里有大段注释解释这一算法（见 [csb2falcon_fifo.v:227-279](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_CSB_MASTER_csb2falcon_fifo.v#L227-L279)）。

#### 4.3.2 核心流程

请求跨域（falcon→core）：

1. falcon 域把 `csb2nvdla_*` 打包成 50 位写入请求 FIFO（`wr_clk = nvdla_falcon_clk`）。
2. core 域从 FIFO 读出 `core_req_pd`（`rd_clk = nvdla_core_clk`），供 4.1 的译码使用。
3. FIFO 的 `wr_ready` 反馈给外部 `csb2nvdla_ready`；core 侧 `core_req_prdy` 恒为 1（csb_master 总能立即取走）。

响应跨域（core→falcon）：

1. core 域把合并后的 `core_resp_pd`（34 位）写入响应 FIFO（`wr_clk = nvdla_core_clk`）。
2. falcon 域读出 `nvdla2csb_resp_pd`（`rd_clk = nvdla_falcon_clk`），`rd_ready` 恒为 1（外部总在接）。

#### 4.3.3 源码精读

**① csb_master 里两个 FIFO 的例化。**

[NV_NVDLA_csb_master.v:454-466](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L454-L466) —— 请求 FIFO `u_fifo_csb2nvdla`（`falcon2csb_fifo`）：`wr_clk=nvdla_falcon_clk`、`rd_clk=nvdla_core_clk`、`wr_data=csb2nvdla_pd[49:0]`、`rd_data=core_req_pd[49:0]`。

[NV_NVDLA_csb_master.v:470-482](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L470-L482) —— 响应 FIFO `u_fifo_nvdla2csb`（`csb2falcon_fifo`）：`wr_clk=nvdla_core_clk`、`rd_clk=nvdla_falcon_clk`、`wr_data=core_resp_pd[33:0]`、`rd_data=nvdla2csb_resp_pd[33:0]`。注意它的 `rd_ready` 接 `1'b1`，`core_resp_prdy` 是 FIFO 回报的写侧 ready。

**② FIFO 内部结构（以请求 FIFO `falcon2csb_fifo` 为例）。**

[NV_NVDLA_CSB_MASTER_falcon2csb_fifo.v:221-231](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_CSB_MASTER_falcon2csb_fifo.v#L221-L231) —— 例化 `NV_NVDLA_CSB_MASTER_falcon2csb_fifo_flopram_rwa_4x50`，名字里的 `4x50` 即 **4 深度 × 50 位**，用触发器（flop）实现的分布式 RAM。

[NV_NVDLA_CSB_MASTER_falcon2csb_fifo.v:321-362](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_CSB_MASTER_falcon2csb_fifo.v#L321-L362) —— 写侧格雷码指针 `wr_pushing_gray_cntr` 经三个 `p_STRICTSYNC3DOTM_C_PPP` 同步器（3 位指针需 3 个）打到读侧，读侧比较不等即产生 `rd_pushing`。

[NV_NVDLA_CSB_MASTER_falcon2csb_fifo.v:403-435](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_CSB_MASTER_falcon2csb_fifo.v#L403-L435) —— 反方向：读侧 `rd_popping` 的格雷码指针同步回写侧，产生 `wr_popping`，用于更新写侧计数与满标志。

**③ 格雷码计数器。**

[NV_NVDLA_CSB_MASTER_falcon2csb_fifo.v:1165-1192](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_CSB_MASTER_falcon2csb_fifo.v#L1165-L1192) —— `gray_cntr_strict`（纯组合给 next 值）与 `gray_cntr`（带 clk/reset 的寄存器版）。3 位格雷码的 next 表达式为：

\[
\text{gray\_next} = \{\, \text{gray}[2] \oplus (\text{polarity}\ \&\ \sim\text{gray}[0]),\quad \text{gray}[1] \oplus (\text{polarity}\ \&\ \text{gray}[0]),\quad \text{gray}[0] \oplus (\sim\text{polarity})\,\}
\]

其中 `polarity = gray[0] ^ gray[1] ^ gray[2]`。

**④ 响应 FIFO 的差别。**

[NV_NVDLA_CSB_MASTER_csb2falcon_fifo.v:195-205](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_CSB_MASTER_csb2falcon_fifo.v#L195-L205) —— 响应 FIFO 用 `flopram_rwa_2x34`，即 **2 深度 × 34 位**；指针只有 2 位，因此同步器只需 2 个（[L304-L325](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_CSB_MASTER_csb2falcon_fifo.v#L304-L325)）。两个 FIFO 结构完全同构，只是数据宽度（50 vs 34）和深度（4 vs 2）不同。

> 设计直觉：请求 FIFO 做 4 深，是为了让 CPU 能连续投递几条配置写而不被 core 侧反压；响应 FIFO 只做 2 深，因为响应产生速率低（一次请求才一个响应），且 falcon 侧 `rd_ready` 恒为 1，不会积压。

#### 4.3.4 代码实践

**实践目标**：搞清两个 FIFO 各自的宽度、深度、时钟方向，并能解释跨域握手为何安全。

**操作步骤**：

1. 打开 `NV_NVDLA_CSB_MASTER_falcon2csb_fifo.v`，定位 [L221](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_CSB_MASTER_falcon2csb_fifo.v#L221-L221) 的 RAM 例化，记下 `4x50`。再读 [L318-L365](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_CSB_MASTER_falcon2csb_fifo.v#L318-L365) 的同步器，数一数同步 `wr_pushing` 用了几个 `p_STRICTSYNC3DOTM_C_PPP`（应为 3 个）。
2. 打开 `NV_NVDLA_CSB_MASTER_csb2falcon_fifo.v`，做同样统计：深度 2、宽度 34、同步器 2 个。
3. 回到 `NV_NVDLA_csb_master.v` 的 [L454-L482](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L454-L482)，确认两个 FIFO 的 `wr_clk`/`rd_clk` 分别接的是 falcon 还是 core 时钟。
4. 在注释 [L227-L279](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_CSB_MASTER_csb2falcon_fifo.v#L227-L279)（csb2falcon）里阅读格雷码跨域算法说明，对照 4.3.3 的公式理解。

**需要观察的现象**：宽度=每次跨域搬运的数据位数；深度=RAM 行数；同步器个数=指针位宽。三者一一对应。

**预期结果**：你能填出下表——

| FIFO | 实例名 | 方向 | 宽度 | 深度 | 同步器数 |
|------|--------|------|------|------|----------|
| 请求 | u_fifo_csb2nvdla | falcon→core | 50 | 4 | 3 |
| 响应 | u_fifo_nvdla2csb | core→falcon | 34 | 2 | 2 |

**待本地验证**：上述宽度/深度均由实测源码得出；若仓库版本更新，请以 `flopram_rwa_NxM` 名字与同步器实例数为准重新核对。

#### 4.3.5 小练习与答案

**练习 1**：为什么请求 FIFO 的指针是 3 位、响应 FIFO 是 2 位？

**答案**：异步 FIFO 的格雷码指针位宽由深度决定——深度 4 需要 2 位地址外加 1 位额外折回位共 3 位（`4x50` → 3 位指针）；深度 2 需要 1 位地址外加 1 位共 2 位（`2x34` → 2 位指针）。指针位宽 = 同步器个数，所以分别是 3 个和 2 个。

**练习 2**：假设 core 时钟比 falcon 时钟快很多，请求 FIFO 会满吗？满之后会发生什么？

**答案**：一般不会满——core 侧 `core_req_prdy` 恒为 1，请求一进 core 域就被取走译码，FIFO 几乎总是空的。即便极端情况下短时积压，4 深也够缓冲；只有当 core 侧持续不取（理论不会发生）才会满，届时 FIFO 的 `wr_ready` 拉低，外部 `csb2nvdla_ready` 跟着拉低，反压住 CPU，不会丢数据。

---

## 5. 综合实践

把三个模块串起来，完成一次「**跟踪一条 CSB 写请求的完整旅程**」的源码阅读任务：

**任务**：CPU 要向 CSC（卷积分发器）的某个寄存器写一个值。地址假设为 `0x00006010`，写数据自定。

**要求画出/写出**：

1. **入口打包**：`csb2nvdla_*` 五个信号如何在 [L452](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L452-L452) 打包成 50 位 `csb2nvdla_pd`。
2. **跨域**：这个包如何经请求 FIFO（falcon→core，4 深）变成 `core_req_pd`，并拆出 `core_req_addr`（[L596-L608](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L596-L608)）。
3. **译码**：`0x6010 & addr_mask` 命中哪个 `select_`？（应为 CSC，[L772](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L772-L772)）
4. **下发**：请求如何进入 CSC 专用的暂存器并出现在 `csb2csc_req_pvld/prdy/pd`（[L775-L830](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L775-L830)）。
5. **响应**：CSC 处理完后回送的 `csc2csb_resp_*` 如何被打一拍（[L1949-L1965](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L1949-L1965)）、并入 `core_resp_pd` 的 OR（[L2234-L2252](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L2234-L2252)）。
6. **跨域回程**：响应如何经响应 FIFO（core→falcon，2 深）回到 falcon 域，按 `type=1` 驱动 `nvdla2csb_wr_complete`（[L565-L571](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L565-L571)）。

**交付物**：一张标注了「信号名 → 行号 → 所在时钟域」的端到端时序流程图，并指出全程两次跨时钟域的位置。

**待本地验证**：若要动态观察，可参照 [u1-l4](u1-l4-first-simulation.md) 跑一个 sanity trace 并用 `DUMP=1 DUMPER=VERDI` 打开波形，在 `u_NV_NVDLA_csb_master` 实例里追踪上述信号。

## 6. 本讲小结

- **csb_master 是 NVDLA 配置空间的中央路由器**，被例化在 `partition_o` 第 1771 行，把单一 CSB 端口扇出到所有引擎。
- **地址译码按 4KB 对齐**：用 `addr_mask`（`0xFC000`）取地址 bit[17:12] 做相等比较，命中 17 个引擎之一；都不命中则进 dummy 客户端，回 `data=0,error=0`，避免总线挂死。
- **共 17 个 `csb2*_req_pvld` 输出**：glb、gec、mcif、cvif、bdma、cdma、csc、cmac_a、cmac_b、cacc、sdp_rdma、sdp、pdp_rdma、pdp、cdp_rdma、cdp、rbk；其中 gec 实际由 GLB 模块接收。
- **响应合并是 19 路 OR**（17 引擎 + dummy + 关闭的 afbif），靠 `nv_assert_zero_one_hot` 断言保证「至多一路 valid」。
- **跨时钟域靠一对异步 FIFO**：请求 FIFO（falcon→core，50 位，4 深）与响应 FIFO（core→falcon，34 位，2 深），均用格雷码指针 + 严格同步器。
- **读/写响应在 falcon 域按 type 位分拆**：`type=0` 走 `nvdla2csb_valid/data`，`type=1` 走 `nvdla2csb_wr_complete`。

## 7. 下一步学习建议

- **往下走进引擎寄存器**：下一篇 [u2-l3 寄存器文件与影偶配置机制](u2-l3-register-files-shadow-config.md) 讲 `csb2*_req_pd` 进入引擎后如何被 `_CSB_reg.v` / `_dual_reg.v` 接收，以及 producer/consumer 影偶配置。
- **往上追寄存器生成**：这些 `_CSB_reg.v` 是由 SystemRDL 经 Ordt 自动生成的，见 [u8-l2 寄存器规格与 RDL/Ordt 生成](u8-l2-rdl-ordt-reggen.md)。
- **横向对比另一条 CSB 通路**：可对照 [u2-l1](u2-l1-csb-bus-apb2csb.md) 的 apb2csb，理解「外部 APB → CSB → csb_master → 引擎」整条配置链。
- **想深入异步 FIFO**：本讲的 FIFO 由 fifogen 生成，涉及大量库原语（`p_STRICTSYNC3DOTM_C_PPP`、`NV_CLK_gate_power`），可在 [u6-l2 FIFO 与 vlibs 库原语](u6-l2-fifo-vlibs-primitives.md) 系统学习。
