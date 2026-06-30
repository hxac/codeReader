# axi_cdc_src / axi_cdc_dst 拆分

## 1. 本讲目标

本讲承接 u8-l1（你已经知道 `axi_cdc` 为五条通道各配一个 Gray 异步 FIFO），回答一个工程层面的问题：

> 既然 `axi_cdc` 已经能完整地跨越两个时钟域，为什么库还要把它**再拆成** `axi_cdc_src` 和 `axi_cdc_dst` 两个子模块？

学完本讲你应当能够：

- 说清「把 CDC 一分为二」的工程动机（层级化综合、分时钟域布局、边界约束）。
- 列出 src 与 dst 之间那 15 组异步信号（数据阵列 + Gray 指针），并解释它们为什么不带时钟、为什么安全。
- 识破一个命名陷阱：`cdc_fifo_gray_src` / `cdc_fifo_gray_dst` 里的 `_src` / `_dst` 指的是 FIFO 的「写半 / 读半」，**不是**时钟域。
- 在自己的设计里分别例化 `axi_cdc_src` 与 `axi_cdc_dst`，并用 `AXI_BUS_ASYNC_GRAY` 接口把它们正确连起来。

## 2. 前置知识

阅读本讲前，请确认你已掌握以下概念（在 u8-l1 中建立）：

- **亚稳态与异步跨越**：两个无相位关系的时钟不能裸连，需要「同步器 + Gray 码指针」两件套。数据本身不走 Gray，靠 FIFO「写完才能读」的协议保证一致。
- **Gray 异步 FIFO 的三个端口组**：写半（`cdc_fifo_gray_src`）有 `src_data_i` / `src_valid_i` / `src_ready_o` 和异步 `async_data_o` / `async_wptr_o` / `async_rptr_i`；读半（`cdc_fifo_gray_dst`）有 `dst_data_o` / `dst_valid_o` / `dst_ready_i` 和异步 `async_data_i` / `async_wptr_i` / `async_rptr_o`。
- **AXI 五通道与方向**：请求通道 AW/W/AR 由 Master 发、src→dst 流；响应通道 B/R 由 Slave 发、dst→src 流。
- **`(* async *)` 属性**：综合属性，告诉工具「这条路径跨时钟域，**不要**按同步路径分析它」。

> 一句话复习：`axi_cdc`（单体）= 把 5 个 Gray FIFO 塞进同一个模块，左侧 `src_clk_i`、右侧 `dst_clk_i`。本讲研究的就是「把这一个模块沿时钟边界切开」会发生什么。

## 3. 本讲源码地图

| 文件 | 层级 | 作用 |
| --- | --- | --- |
| [src/axi_cdc.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc.sv) | Level 3 | **重组点**：把 `axi_cdc_src` 与 `axi_cdc_dst` 背靠背接起来，二者之间用 15 组 `(* async *)` 信号互连。功能上等价于「单体 CDC」。 |
| [src/axi_cdc_src.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc_src.sv) | Level 2 | **源时钟域那一半**：所有寄存器只被 `src_clk_i` 驱动；对内是 AXI req/resp 结构体端口，对外是一组异步 master 端口。 |
| [src/axi_cdc_dst.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc_dst.sv) | Level 2 | **目的时钟域那一半**：所有寄存器只被 `dst_clk_i` 驱动；对内 AXI req/resp 结构体端口，对外异步 slave 端口。 |
| [src/axi_intf.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_intf.sv) | Level 1 | 定义 `AXI_BUS_ASYNC_GRAY` 接口（含 Master/Slave modport），把那 15 组异步信号打包，供分体例化时连线。 |
| [Bender.yml](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Bender.yml) | — | 编译层级清单，证明 src/dst（Level 2）先于 axi_cdc（Level 3）编译。 |
| [test/tb_axi_cdc.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_cdc.sv) | — | 跨时钟域回归测试，双时钟（10ns / 3ns）+ 双侧 scoreboard。 |

> 提示：`axi_cdc_src.sv` 和 `axi_cdc_dst.sv` 文件末尾还各带一个 `_intf`（接口外壳）和一个 `axi_lite_*` 版本，本讲主要看结构体内核版（`axi_cdc_src` / `axi_cdc_dst`），外壳版在 4.2 节一带而过。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：

- **4.1 拆分的工程动机**（对应 `axi_cdc`：为什么要存在这样一个「重组点」）。
- **4.2 异步信号契约**（数据阵列 + Gray 指针 + `AXI_BUS_ASYNC_GRAY`）。
- **4.3 写半/读半原语与时钟域归属**（命名陷阱，对应 src/dst 内部例化）。

### 4.1 拆分的工程动机：让时钟边界变成物理边界

#### 4.1.1 概念说明

`axi_cdc`（单体）在**功能**上完全正确：它左边接 `src_clk_i`、右边接 `dst_clk_i`，五条通道各跨一个 FIFO。那么为什么还要拆？

因为**物理实现**（综合、布局布线、时序分析）更希望「同一时钟域的所有寄存器被聚拢在一个清晰边界内」。原因有三：

1. **分块综合（hierarchical synthesis）**：大型 SoC 常把每个时钟域做成一个独立的综合 block。如果 CDC 是单体模块，它就同时含有两个时钟，无法干净地归入任一 block。拆开后，`axi_cdc_src` 整块属于 src 域、`axi_cdc_dst` 整块属于 dst 域，各自只看到一个时钟。
2. **静态时序分析（STA）**：跨域路径必须设为 `set_false_path` 或 `set_max_delay -datapath_only`。把这条路径显式做成模块端口，约束点就一目了然——「src/dst 之间的那 15 组信号」。单体模块里则要靠 `(* async *)` 属性去告诉工具，不够显式。
3. **布局（floorplan）**：两个时钟域常落在不同区域（甚至不同电源域）。把 CDC 切成两半，每半跟着自己的域走，连线最短。

关键结论：**拆分不改变功能**——`axi_cdc` 与「`axi_cdc_src` + `axi_cdc_dst` 接起来」编译出的网表是等价的。拆分纯粹是给物理流程让路。

#### 4.1.2 核心流程

```text
                 时钟域 SRC                              时钟域 DST
        ┌──────────────────────┐              ┌──────────────────────┐
AXI     │                      │  15 组异步    │                      │   AXI
master  │   axi_cdc_src        │  信号(无时钟) │   axi_cdc_dst        │  slave
───────►│   (只认 src_clk_i)   │══════════════►│   (只认 dst_clk_i)   │───────►
        │                      │  data+wptr+rptr                      │
        └──────────────────────┘   ←(* async *)→└──────────────────────┘
```

- src 半的输出：请求通道的数据 + 写指针（往 dst 推），以及响应通道的读指针（从 dst 拉）。
- dst 半的输入：与上面对应的同一组异步信号。
- **二者之间没有任何时钟或复位连线**，全部是「数据阵列 + Gray 指针」。

#### 4.1.3 源码精读

`axi_cdc` 顶层就是「重组点」。它的端口同时含两个时钟域的时钟与 req/resp：

[src/axi_cdc.sv:36-47](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc.sv#L36-L47) — 双时钟、双复位、src 侧 req/resp 与 dst 侧 req/resp。这是一个模块同时「看见」两个时钟的根源。

接着它声明了 src 与 dst 之间共享的那 15 组信号（5 个数据阵列 + 10 个指针）：

[src/axi_cdc.sv:49-58](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc.sv#L49-L58) — 这就是「物理上的时钟边界」。注意指针位宽是 `[LogDepth:0]`，即 `LogDepth+1` 位：低位 `LogDepth` 位用于寻址深度为 \(2^{\text{LogDepth}}\) 的 FIFO，最高位用于区分「满」与「空」（经典 Gray FIFO 技巧）。深度公式为

\[
\text{FIFO 深度} = 2^{\text{LogDepth}}, \qquad \text{指针位宽} = \text{LogDepth}+1.
\]

然后例化 src 半，把 15 组信号**逐根打上 `(* async *)`** 后连出去：

[src/axi_cdc.sv:60-90](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc.sv#L60-L90) — `i_axi_cdc_src`，每条异步连线前的 `(* async *)` 是给综合工具的明确指令：**这条路径跨域，别按同步 setup/hold 检查**。这一段就是「单体 CDC 里那段本该是模块边界的、被属性标注的隐式边界」。

dst 半对称地例化，把同一组 15 个信号接回来：

[src/axi_cdc.sv:92-122](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc.sv#L92-L122) — `i_axi_cdc_dst`。src 的 `_o`（输出）对应 dst 的 `_i`（输入），反之亦然。两端共 30 个 `(* async *)` 标注，一一咬合。

层级关系在编译清单里也有印证：src/dst 是 Level 2，`axi_cdc` 是 Level 3——

[Bender.yml:40-46 与 79](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Bender.yml#L40-L46) —— `axi_cdc_dst.sv` 与 `axi_cdc_src.sv` 在 Level 2（与 `axi_cut`、`axi_demux_simple` 同级），而 [Bender.yml:79](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Bender.yml#L79) 的 `axi_cdc.sv` 在 Level 3，正好比它依赖的两个半块高一级。

#### 4.1.4 代码实践

**实践目标**：验证「`axi_cdc` 仅仅是 src + dst 的重组」，从而理解拆分的物理意义。

**操作步骤**：

1. 打开 `src/axi_cdc.sv`，定位到 [第 49–58 行](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc.sv#L49-L58) 的 15 组信号声明。
2. 把 `i_axi_cdc_src`（第 60–90 行）和 `i_axi_cdc_dst`（第 92–122 行）两段例化并排对照，画出每个通道（AW/W/B/AR/R）在两例化之间的连线，确认 src 的某个 `_o` 永远接到 dst 的同名 `_i`。
3. 数一下两段例化里 `(* async *)` 标注的总数。

**需要观察的现象**：

- src 例化有 15 个 `(* async *)`，dst 例化也有 15 个，两两对齐。
- `axi_cdc` 主体除这 15 组 wire 和两个例化外，**没有任何逻辑**（没有 always、没有 assign 以外的组合）——它纯粹是「插座」。

**预期结果**：你会得到一张「src 输出 ↔ dst 输入」的对照表，证明 `axi_cdc` 是零逻辑的重组层；这恰好说明：把它替换成「两块分处不同 clock block 的 src/dst + 一束 async 线」在功能上毫无损失，只是把那条隐式的 `(* async *)` 边界提升成了显式的模块/物理边界。

**待本地验证**：若你有 DC 或 Genus 环境，可分别对单体 `axi_cdc` 与「src + dst 分体」做 elaborate，比对两者例化出的 `cdc_fifo_gray_*` 数量与类型是否一致（应完全一致）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `axi_cdc` 顶层那 30 个 `(* async *)` 属性全部删掉，功能会错吗？综合/STA 会怎样？

> **答案**：功能不会错（仿真完全一样）。但综合/STA 会把那 15 条跨域路径当作**同步路径**去检查 setup/hold，由于两侧时钟无关，这些路径必然「违例」，工具要么报大量 timing violation，要么无法自动识别为 false path。`(* async *)` 是给工具的合法化声明，不是逻辑的一部分。

**练习 2**：为什么 src/dst 是 Level 2，而 `axi_cdc` 是 Level 3？

> **答案**：编译层级反映依赖。`axi_cdc_src`、`axi_cdc_dst` 只依赖 Level 0–1（`axi_pkg`、`axi_intf` 及外部 `cdc_fifo_gray_*`），所以是 Level 2；`axi_cdc` 同时例化这两个 Level 2 模块，按「本包内最长依赖链 + 1」自然落到 Level 3。这印证了「先有半块、后有重组层」的构建顺序。

### 4.2 异步信号契约：数据阵列 + Gray 指针

#### 4.2.1 概念说明

src 与 dst 之间的「15 组信号」遵循一个严格的契约：

- **数据**：每个通道一份数据阵列，形如 `aw_chan_t [2**LogDepth-1:0]`——它不是一拍数据，而是**整个 FIFO 存储体**的硬件实现被劈成两半：写半负责把数据写进阵列，读半负责从阵列读出，阵列本体物理上跨越边界（由综合工具落在边界附近）。
- **指针**：每个通道两个 Gray 码指针 `wptr`（写指针）与 `rptr`（读指针），位宽 `[LogDepth:0]`。指针用 Gray 编码，保证每次递增只翻转一比特，跨域采样时即使被同步器「打一拍」也不会出现中间态。
- **没有 valid/ready**：跨域边界上**不存在** AXI 那种 `valid/ready` 握手线。握手被 FIFO 的满/空判定取代——写半看「满」决定能不能写、读半看「空」决定能不能读，而满/空恰恰由这两根 Gray 指针经同步器比较得到。

这就是为什么这束线安全：数据靠 FIFO「写完才读」语义，指针靠 Gray + 同步器，二者都没有裸露的异步多比特握手。

#### 4.2.2 核心流程

每个通道在边界上恰好三根「线束」：

```text
        写半(src 侧请求 / dst 侧响应)            读半(对侧)
        ─────────────────────────────            ──────────
data:   async_data_o  ─────────────────────►    async_data_i   (数据阵列，T[2**LogDepth-1:0])
wptr:   async_wptr_o  ─────────────────────►    async_wptr_i   (Gray 写指针，[LogDepth:0])
rptr:                             ◄─────────────  async_rptr_o  (Gray 读指针，[LogDepth:0])
        async_rptr_i
```

- `wptr` 与 `data` 同向（从写半到读半），`rptr` 反向（从读半回写半）。
- 5 个通道 × 3 束 = **15 组异步信号**，正是 `axi_cdc` 里 `(* async *)` 的数量来源。

#### 4.2.3 源码精读

`axi_cdc_src` 的端口就是这份契约的「master 视角」——它输出请求通道的 data/wptr、输入请求通道的 rptr；对响应通道方向相反：

[src/axi_cdc_src.sv:36-58](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc_src.sv#L36-L58) — 上半是同步 slave 端口（`src_clk_i`、`src_req_i`、`src_resp_o`），下半是异步 master 端口，逐通道列出 data/wptr/rptr。注意命名 `async_data_master_*`：这里的 `master` 指的是「FIFO 写半对外呈 master 端」，与 AXI master/slave 无关，4.3 节会专门辨析。

`axi_cdc_dst` 的端口是「slave 视角」，方向镜像：

[src/axi_cdc_dst.sv:36-58](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc_dst.sv#L36-L58) — 上半是异步 slave 端口（`async_data_slave_*`），下半是同步 master 端口（`dst_clk_i`、`dst_req_o`、`dst_resp_i`）。把这两个文件并排看，你会发现 src 的每个 `_o`/`_i` 与 dst 的同名 `_i`/`_o` 一一对应。

为了让「分体例化」时不必手写 15 组线，库提供接口 `AXI_BUS_ASYNC_GRAY`：

[src/axi_intf.sv:359-406](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_intf.sv#L359-L406) — 它把 5 个数据阵列与 10 个指针打包成一个 bundle，并用 `Master` / `Slave` 两个 modport 预设方向。`Master` 对请求通道（AW/W/AR）输出 data+wptr、输入 rptr；对响应通道（B/R）方向相反——这正与 `axi_cdc_src` 的端口视角吻合。

接口外壳 `axi_cdc_src_intf` 就用 `AXI_BUS_ASYNC_GRAY.Master` 把异步侧引到端口：

[src/axi_cdc_src.sv:159-175](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc_src.sv#L159-L175) —— `src` 是同步 `AXI_BUS.Slave`、`dst` 是异步 `AXI_BUS_ASYNC_GRAY.Master`；随后 [src/axi_cdc_src.sv:211-225](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc_src.sv#L211-L225) 把内部 `i_axi_cdc_src` 的扁平异步端口逐根连到 `dst.*`。`axi_cdc_dst_intf` 用 `.Slave` 镜像之（[src/axi_cdc_dst.sv:160-176](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc_dst.sv#L160-L176)）。

> 工具兼容小贴士：src/dst 里每个原语例化前都有一段 `\`ifdef QUESTA` —— 见 [src/axi_cdc_src.sv:62-66](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc_src.sv#L62-L66)。Questa 对结构体类型参数有 bug，所以改用 `logic [$bits(chan_t)-1:0]` 扁平向量；其它工具（如 VCS）反过来对 `$bits()` 构造的类型参数有问题，于是走结构体分支。这是「为兼容多种 EDA 工具而保持灵活」的典型编码（与 u16-3 的 EDA 兼容主题呼应）。

#### 4.2.4 代码实践

**实践目标**：在分体拓扑下，亲眼看到 src 与 dst 之间靠「一束 async 线」通信，而不是 AXI 握手。

**操作步骤**：

1. 读接口外壳 `axi_cdc_src_intf`（[src/axi_cdc_src.sv:159-228](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc_src.sv#L159-L228)）与 `axi_cdc_dst_intf`（[src/axi_cdc_dst.sv:160-229](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc_dst.sv#L160-L229)）。
2. 在脑中（或纸上）把它们对接：`src_intf.dst`（`AXI_BUS_ASYNC_GRAY.Master`）直接连到 `dst_intf.src`（`AXI_BUS_ASYNC_GRAY.Slave`），因为 modport 方向互补，所有 data/wptr/rptr 自动对齐。
3. 对照 `AXI_BUS_ASYNC_GRAY` 的字段（[src/axi_intf.sv:381-404](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_intf.sv#L381-L404)），数清楚里面共有几个 data 阵列、几根指针。

**需要观察的现象**：

- 该接口含 5 个 data 阵列 + 10 根指针（每通道 wptr/rptr 各一），共 15 组信号。
- `Master` 与 `Slave` modport 对每根线的 input/output 恰好相反，因此两个 modport 实例直接 `.` 相连即合法。

**预期结果**：你会确认「分体 CDC 的边界 = 一个 `AXI_BUS_ASYNC_GRAY` 实例」，连线上没有任何 `valid/ready` 或时钟，全部是数据阵列与 Gray 指针。

#### 4.2.5 小练习与答案

**练习 1**：为什么边界上的数据用「整个 FIFO 阵列」跨域，而不是单拍数据 + valid/ready？

> **答案**：因为跨两个无关时钟域时，多比特数据 + valid/ready 的握手本身不可靠（valid 被「打一拍」同步会丢拍，数据多比特有冒险）。Gray 异步 FIFO 把存储体放在边界上，用「写完才能读」的 FIFO 协议规避了跨域握手：写半只看本域的满标志、读半只看本域的空标志，二者靠 Gray 指针比较得到，单比特翻转变同步器友好。

**练习 2**：`LogDepth=2` 时，每个通道的数据阵列有几项？指针几位？

> **答案**：阵列深度 \(2^{\text{LogDepth}}=2^2=4\) 项；指针位宽 `[LogDepth:0]=[2:0]`，即 3 位（2 位寻址 + 1 位满空判别）。

### 4.3 写半/读半原语与时钟域归属：命名陷阱

#### 4.3.1 概念说明

这是本讲最容易踩坑、也最值得讲透的一点。

外部原语叫 `cdc_fifo_gray_src` 和 `cdc_fifo_gray_dst`。**这两个名字里的 `_src` / `_dst` 指的是 FIFO 的「写半（push）/ 读半（pop）」，不是时钟域。** 读半/写半的判据是端口形态：

- `cdc_fifo_gray_src` = 写半：有 `src_data_i` / `src_valid_i` / `src_ready_o`（推数据进 FIFO），异步侧输出 `async_data_o` / `async_wptr_o`、输入 `async_rptr_i`。
- `cdc_fifo_gray_dst` = 读半：有 `dst_data_o` / `dst_valid_o` / `dst_ready_i`（从 FIFO 弹数据），异步侧输入 `async_data_i` / `async_wptr_i`、输出 `async_rptr_o`。

而 AXI 五通道里：

- **请求通道 AW/W/AR**：数据流 src→dst。**写半在 src 域、读半在 dst 域。**
- **响应通道 B/R**：数据流 dst→src。**写半在 dst 域、读半在 src 域**（方向反过来了！）。

于是出现一个看似矛盾、实则正确的现象：

> `axi_cdc_dst`（住在 dst 时钟域）里，对响应通道 B/R 例化的是 **`cdc_fifo_gray_src`**（写半）——因为响应是从 dst 这边「写」回 src 的。

同理，`axi_cdc_src`（住在 src 时钟域）里，对响应通道 B/R 例化的是 **`cdc_fifo_gray_dst`**（读半）。

#### 4.3.2 核心流程

把「通道方向 × 写半/读半 × 落在哪个域」整理成一张表：

| 通道 | 方向 | 写半（`_src` 原语）所在 | 读半（`_dst` 原语）所在 | 写半时钟接谁 |
| --- | --- | --- | --- | --- |
| AW | src→dst | `axi_cdc_src` | `axi_cdc_dst` | `src_clk_i` |
| W | src→dst | `axi_cdc_src` | `axi_cdc_dst` | `src_clk_i` |
| AR | src→dst | `axi_cdc_src` | `axi_cdc_dst` | `src_clk_i` |
| B | dst→src | `axi_cdc_dst` | `axi_cdc_src` | `dst_clk_i` |
| R | dst→src | `axi_cdc_dst` | `axi_cdc_src` | `dst_clk_i` |

判别口诀：**「谁推数据，谁就是写半（`_src` 原语）；写半的时钟接数据源所在域。」** 请求由 src 推，所以 AW/W/AR 的写半在 src 域、接 `src_clk_i`；响应由 dst 推，所以 B/R 的写半在 dst 域、接 `dst_clk_i`。

#### 4.3.3 源码精读

先看 `axi_cdc_src` 内部，**请求通道**例化写半（原语端口名 `src_clk_i` 直接接本模块的 `src_clk_i`）：

[src/axi_cdc_src.sv:60-78](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc_src.sv#L60-L78) —— AW 的 `cdc_fifo_gray_src`（写半），`src_data_i ← src_req_i.aw`，时钟沿用 `src_clk_i`。W、AR 同理（第 80–135 行）。

而**响应通道**在 `axi_cdc_src` 里例化的是读半 `cdc_fifo_gray_dst`，且注意它的 `dst_clk_i` 端口接的是**本模块的 `src_clk_i`**：

[src/axi_cdc_src.sv:99-116](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc_src.sv#L99-L116) —— B 通道的 `cdc_fifo_gray_dst`（读半）：`.dst_clk_i(src_clk_i)`、`.dst_data_o(src_resp_o.b)`。这是因为 `axi_cdc_src` 整块属于 src 时钟域，所以它内部**任何**寄存器（哪怕是读半的 `dst_*` 端口）都必须挂在 `src_clk_i` 上。R 通道同理（第 137–154 行）。

`axi_cdc_dst` 正好镜像：请求通道是读半（接 `dst_clk_i`），响应通道是写半：

[src/axi_cdc_dst.sv:100-117](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc_dst.sv#L100-L117) —— B 通道的 `cdc_fifo_gray_src`（写半）：`.src_clk_i(dst_clk_i)`、`.src_data_i(dst_resp_i.b)`。关键点：原语端口叫 `src_clk_i`，但接到却是 `dst_clk_i`——因为这一段逻辑住在 dst 域、数据从 dst 这边推。这就是「原语名 ≠ 时钟域」的铁证。

把两份源码并排读完，你会得到一张「5 通道 × {src 半例化的原语, dst 半例化的原语}」对照表，与 4.3.2 完全吻合。

#### 4.3.4 代码实践

**实践目标**：亲手验证命名陷阱——证明「写半/读半」与「src/dst 时钟域」是两个独立维度。

**操作步骤**：

1. 打开 `src/axi_cdc_dst.sv`，定位 B 通道例化 [第 100–117 行](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc_dst.sv#L100-L117)。
2. 确认它例化的原语名是 `cdc_fifo_gray_src`（写半），但其 `.src_clk_i` 端口连的是 `dst_clk_i`。
3. 翻到 `src/axi_cdc_src.sv` 的 B 通道例化 [第 99–116 行](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc_src.sv#L99-L116)，确认它例化的是 `cdc_fifo_gray_dst`（读半），`.dst_clk_i` 连的是 `src_clk_i`。
4. 列出全部 5 个通道在两个半块里各自例化的原语名与所接时钟，得到 4.3.2 的那张表。

**需要观察的现象**：

- 请求通道（AW/W/AR）：src 半例化 `_src` 原语接 `src_clk_i`；dst 半例化 `_dst` 原语接 `dst_clk_i`。「名字与时钟域同向」——这一组不会骗人。
- 响应通道（B/R）：src 半例化 `_dst` 原语接 `src_clk_i`；dst 半例化 `_src` 原语接 `dst_clk_i`。「名字与时钟域反向」——陷阱就在这里。

**预期结果**：你得到一张表，清楚显示「`_src`/`_dst` 原语名」描述 FIFO 写/读半，「`src_clk_i`/`dst_clk_i` 端口」描述接哪个时钟域，二者交叉组合。今后读到任何 `cdc_fifo_gray_*`，先看它接的是哪个时钟，再判断它是写半还是读半，就不会被名字误导。

**待本地验证**：可选地用 `make sim-tb_axi_cdc.log`（参见 u1-l4）跑一次回归。测试台 [test/tb_axi_cdc.sv:118-131](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_cdc.sv#L118-L131) 用的是单体 `axi_cdc_intf`，两套时钟 10ns / 3ns（[test/tb_axi_cdc.sv:27-31](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_cdc.sv#L27-L31)）。若日志出现 `Errors: 0,` 即跨域功能正确。

#### 4.3.5 小练习与答案

**练习 1**：`axi_cdc_dst` 里为 B 通道例化的原语叫 `cdc_fifo_gray_src`，可它的 `src_clk_i` 端口却接 `dst_clk_i`。请解释这种「名实不符」为什么不矛盾。

> **答案**：原语名后缀 `_src` / `_dst` 表示 **FIFO 的写半 / 读半**（看端口形态：有 `src_data_i` 的是写半、有 `dst_data_o` 的是读半），与时钟域无关。B 通道响应由 dst 侧产生并推回 src，所以**写半**必须落在 dst 域，于是 `axi_cdc_dst` 例化写半原语 `cdc_fifo_gray_src`，并把它形如 `src_clk_i` 的时钟端口接到本域的 `dst_clk_i` 上。命名沿用了 FIFO 原语自身的端口命名习惯，而非本设计的时钟域命名。

**练习 2**：若把 `axi_cdc_src` 里 B 通道读半原语的 `.dst_clk_i` 误接到 `dst_clk_i`（而非 `src_clk_i`），会发生什么？

> **答案**：那一段读半寄存器就跑在了另一个时钟域，整个模块不再「单一时钟域」。仿真可能在采样上出现亚稳态/数据错乱（因为读半的 `dst_*` 端口逻辑与同模块内其它逻辑时钟不同），综合 STA 也会报出域内多时钟路径。这正是拆分要避免的——`axi_cdc_src` 必须做到「内部只见 `src_clk_i`」，所以所有原语（无论名字）的时钟端口都接到 `src_clk_i`。

## 5. 综合实践

把本讲三个最小模块串起来，完成一次「分体 CDC」的源码阅读与接线分析：

1. **读 `axi_cdc` 顶层**（[src/axi_cdc.sv:60-122](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc.sv#L60-L122)），列出它把哪些信号以异步阵列形式连到 src/dst。预期答案：AW/W/B/AR/R 五个通道各一组 `{data, wptr, rptr}`，共 15 组，每组两段例化各打一次 `(* async *)`。

2. **画一张分体拓扑图**：左侧 `axi_cdc_src`（标 `src_clk_i`）、右侧 `axi_cdc_dst`（标 `dst_clk_i`），中间用一个 `AXI_BUS_ASYNC_GRAY` 实例（或 15 条线）相连；标注请求通道 data/wptr 由 src→dst、rptr 由 dst→src，响应通道方向相反。

3. **填原语分配表**：对每个通道，标出 src 半和 dst 半各例化 `cdc_fifo_gray_src` 还是 `cdc_fifo_gray_dst`，以及该原语的时钟端口接 `src_clk_i` 还是 `dst_clk_i`（应与 4.3.2 一致）。

4. **解释拆分对时序约束的好处**（用一段话）：拆分把那条 `(* async *)` 边界提升为显式的模块/物理边界，使每个半块只含单一时钟、可独立分块综合、可把跨域约束精确施加在边界端口上、并可按时钟域就近布局。

> 进阶（可选）：仿照 `tb_axi_cdc` 的双时钟骨架（[test/tb_axi_cdc.sv:50-64](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_cdc.sv#L50-L64)），改用 `axi_cdc_src_intf` + `axi_cdc_dst_intf`（中间用 `AXI_BUS_ASYNC_GRAY` 连）替换原本的单体 `axi_cdc_intf`，确认两侧 scoreboard 仍能比对通过。**待本地验证**（需要 vsim/verilator 环境）。

## 6. 本讲小结

- `axi_cdc`（单体）功能上等价于 `axi_cdc_src` + `axi_cdc_dst` 拼接；顶层本身**零逻辑**，只是 15 组 `(* async *)` 信号把两半咬合的「重组插座」。
- 拆分的动机是**物理实现**：让每个半块只含单一时钟，便于分块综合、精确施加跨域约束、按时钟域就近布局——与功能正确性无关。
- src 与 dst 之间的契约是「**数据阵列 + Gray 指针**」，每通道 3 束（data/wptr/rptr），5 通道共 15 组；边界上**没有 valid/ready、没有时钟**。
- `AXI_BUS_ASYNC_GRAY` 接口（Master/Slave modport）把 15 组异步信号打包，是分体例化时的标准连线载体。
- **命名陷阱**：原语名 `cdc_fifo_gray_src`/`_dst` 指 FIFO 的**写半/读半**，不是时钟域；请求通道写半在 src 域、响应通道写半在 dst 域，所以 `axi_cdc_dst` 里反而出现 `cdc_fifo_gray_src`、且其 `src_clk_i` 端口接 `dst_clk_i`。
- 阅读口诀：**「谁推数据谁就是写半；写半的时钟接数据源所在域。」**

## 7. 下一步学习建议

- **横向巩固**：回到 u8-l1，把「Gray FIFO 同步器 + 三条约束路径」与本讲的「分体 + async 边界」对照，理解「安全机制（原语）」与「拆分工程（外壳）」是两层正交设计。
- **进入数据宽度族（U11）**：CDC 解决「时钟不同」，`axi_dw_converter`（u11-l3）解决「位宽不同」，二者在异构网络里常背靠背出现（见 u15-l4 综合实战）。
- **综合实战（u15-l4）**：把 cdc、dw_converter、iw_converter、isolate、xbar 组成跨域、跨宽度、跨 ID 的完整互联，体会本讲「分体约束」在真实 SoC 互联中的落地方式。
- **方法学（u16-3）**：本讲提到的 `(* async *)` 与三条约束路径，将在「时序、流水线策略与 EDA 兼容」一讲中与 `LatencyMode`、false-path 约束系统化讨论，建议在那里再回看本讲的边界处理。
