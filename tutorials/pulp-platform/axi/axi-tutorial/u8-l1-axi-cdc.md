# axi_cdc：基于 Gray FIFO 的跨时钟域

## 1. 本讲目标

学完本讲后，你应当能够：

1. 说清「为什么不能把两套不同时钟的 AXI 接口直接连在一起」——即亚稳态（metastability）问题，以及用「同步器 + Gray 码指针」的异步 FIFO 解决它的原理。
2. 读懂 `axi_cdc` 的端口布局：`src_*`（slave 侧，`src_clk_i`）与 `dst_*`（master 侧，`dst_clk_i`）各自接哪五条 AXI 通道、共用一组 `axi_req_t`/`axi_resp_t` 结构体。
3. 解释为何每条 AXI 通道都独立配一个 Gray CDC FIFO，以及请求方向（AW/W/AR，src→dst）与响应方向（B/R，dst→src）的 FIFO「写半 / 读半」分别落在哪一侧时钟域。
4. 说出 `LogDepth`、`SyncStages` 两个参数的含义，并能针对每条通道写出**必须单独约束的三条异步路径**（数据阵列、写指针、读指针）。

## 2. 前置知识

在进入源码前，先用三段通俗话术补齐本讲需要的数字设计常识。

**异步时钟与亚稳态。** 一个触发器在每个时钟沿采样其 D 端。如果 D 恰好在时钟沿附近的「建立/保持时间窗口」内发生变化，触发器可能进入**亚稳态**——输出既不是干净的 0 也不是干净的 1，而是在一个中间电平上停留一段不确定的时间，最终随机收敛到 0 或 1。把这种「脏电平」直接送进组合逻辑，会导致后级一些门看到 0、另一些门看到 1，产生完全错误的数据。当发送方（`src_clk`）和接收方（`dst_clk`）是两个互不相关的时钟时，数据跳变几乎总会随机落在接收时钟的采样窗口里，因此**裸连必错**。

**两拍同步器。** 标准对策是把「要跨域的单比特信号」先串两级触发器（即**同步器**）再使用。第一级可能亚稳态，但给它一整个时钟周期去收敛，到第二级采样时大概率已稳定。级数越多，亚稳态「没来得及收敛」的概率越小，平均无故障时间（MTBF）随级数近似指数上升——这正是 `SyncStages` 参数控制的旋钮。

**为什么指针要 Gray 码。** 同步器只能可靠地传**单比特**。可异步 FIFO 需要传递多比特的「写指针 / 读指针」来报告对方「我写到哪了 / 我读到哪了」。若直接传二进制指针，一次 `+1` 可能有多个比特同时翻转（如 3'b011 → 3'b100 三位全变），由于布线延迟不同，接收端可能采样到任意中间值（000、001、111…），指针就「跳飞」了。**Gray 码**保证任意相邻两值只有一个比特不同，于是异步采样时要么看到旧值、要么看到新值，绝不出现垃圾中间值——这正是 Gray 编码用于 CDC FIFO 的根本理由。

> 本讲承接 **u7-l1（axi_fifo 与 spill_register）**：`spill_register` / 普通 FIFO 只解决「同一时钟域内的缓冲与切路径」，而本讲的异步 FIFO 解决的是「两个时钟域之间能不能传」。两者结构都依赖 common_cells 原语，但安全前提完全不同。

## 3. 本讲源码地图

| 文件 | 层级 | 作用 |
|------|------|------|
| `src/axi_cdc.sv` | Level 3 | **本讲主角**。把一组 `src_*`（slave 侧）与 `dst_*`（master 侧）AXI 端口接起来的整块 CDC；内部例化 `axi_cdc_src` + `axi_cdc_dst` 两半，用 `(* async *)` 标注的扁平阵列把它们连起来。同时提供 `axi_cdc_intf`（AXI_BUS 接口外壳）与 `axi_lite_cdc_intf`（AXI-Lite 版）。 |
| `src/axi_cdc_src.sv` | Level 2 | CDC 的 **src 时钟域半边**：为五条通道各例化一个 `cdc_fifo_gray_*` 原语的写半或读半，全部用 `src_clk_i`。 |
| `src/axi_cdc_dst.sv` | Level 2 | CDC 的 **dst 时钟域半边**：与 src 对称，全部用 `dst_clk_i`。 |
| `src/axi_intf.sv` | Level 1 | 定义 `AXI_BUS_ASYNC_GRAY` 接口（src/dst 拆分版用到的「数据阵列 + Gray 指针」bundle），是 u8-l2 的入口，本讲仅引用其结构。 |
| `test/tb_axi_cdc.sv` | test | 跨域测试台：上/下游用不同周期时钟，`axi_rand_master` ↔ `axi_cdc_intf` ↔ `axi_rand_slave`，并用队列做 AB 比对自检。 |

> 注意：`cdc_fifo_gray_src` / `cdc_fifo_gray_dst` 这两个原语**不在本仓库内**，它们来自外部依赖 `common_cells 1.39.0`（见 [`Bender.yml:24`](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Bender.yml#L24)）。本讲在讲它们时，讲的是「Gray 异步 FIFO 的一般机制」加上「从 axi 端口侧能直接观察到的契约」，不臆测其内部实现细节。

## 4. 核心概念与源码讲解

本讲把 `axi_cdc` 拆成三个最小模块递进讲解：先讲异步 CDC 的通用原理，再讲 `axi_cdc` 怎么把五通道布局成 Gray FIFO，最后讲两个参数和必须约束的三条路径。

### 4.1 异步跨时钟域与 Gray FIFO 原理

#### 4.1.1 概念说明

`axi_cdc` 的全部「魔法」都不在它自己写的代码里——它只是把外部 `cdc_fifo_gray` 原语**每条通道摆一个**。所以理解本模块，首先得理解「Gray 异步 FIFO」这个数字设计经典结构解决了什么：

- **问题**：两个无关时钟域之间要可靠地传一组数据（这里是一整拍 AXI 通道载荷，比如 AW 通道的 addr/id/len/size…），且要能报告「满了别写 / 空了别读」。
- **思路**：用一个双口 RAM（这里实现为 `2**LogDepth` 个寄存器组成的阵列）当缓冲；写侧维护写指针、读侧维护读指针；**指针跨域传给对方**，对方据此判断满 / 空。指针用 Gray 码传（单比特变化，可安全同步），判断满 / 空时再在本地转回二进制比较。
- **关键性质**：「满」由写侧判（用同步过来的读指针），「空」由读侧判（用同步过来的写指针）。由于同步会延迟，满 / 空的判断是**保守**的：可能以为满了其实还能写、以为空了其实已经有数据——但这只损失一点吞吐，**绝不丢数据、绝不读垃圾**，这正是 CDC 设计追求的安全性。

`axi_cdc` 模块头部注释把这条契约直接写给了使用者：

> For each of the five AXI channels, this module instantiates a CDC FIFO, whose push and pop ports are in separate clock domains. IMPORTANT: For each AXI channel, you MUST properly constrain three paths through the FIFO…
> ——见 [`src/axi_cdc.sv:19-23`](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc.sv#L19-L23)

#### 4.1.2 核心流程

一个 Gray 异步 FIFO 的数据通路可概括为下面五步循环（以一次「写」为例）：

1. **写侧**：`valid && ready` 握手 → 把数据写入 `mem[wptr]` → `wptr` 在本地 Gray 递增。
2. **写指针跨域**：Gray 编码的 `wptr` 经 `SyncStages` 级同步器送到读侧时钟域。
3. **读侧**：用同步过来的 `wptr` 与本地 `rptr` 比较 → 非「空」则 `valid && ready` 握手 → 读出 `mem[rptr]` → `rptr` Gray 递增。
4. **读指针跨域**：Gray 编码的 `rptr` 经同步器送回写侧。
5. **满 / 空判定**：写侧用同步的 `rptr` 判「满」（压住 `ready`），读侧用同步的 `wptr` 判「空」（压住 `valid`）。

Gray 码与二进制的转换（背景知识，非本仓库代码）：

- 二进制 `b` 转 Gray：\[ g_i = b_i \oplus b_{i+1}, \quad \text{最高位 } g_{n-1} = b_{n-1} \] 等价的紧凑式为 \( g = b \oplus (b \gg 1) \)。
- 由于相邻 Gray 值只有一个比特不同，异步采样最坏只晚一拍看到新指针，**绝不会看到非法中间值**。

指针位宽取 `LogDepth + 1` 位（比寻址所需的 `LogDepth` 位多一位），这个「多出来的最高位」专门用来区分「满」与「空」——因为当读写指针地址部分相等时，可能是「写追上读（满）」也可能是「读追上写（空）」，靠最高位是否相等来区分两者。你会在 `axi_cdc` 的端口里看到所有指针都声明为 `[LogDepth:0]`，正是这个原因。

#### 4.1.3 源码精读

虽然 `cdc_fifo_gray` 的实现在外部 common_cells，但 `axi_cdc` 的信号声明已经把它「对外暴露的三类异步信号」写得清清楚楚：

[src/axi_cdc.sv:49-58](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc.sv#L49-L58) —— 这段声明了每条通道跨域传递的三类信号：

```systemverilog
aw_chan_t [2**LogDepth-1:0] async_data_aw_data;   // 数据阵列：2**LogDepth 个槽
// ... w/b/ar/r 同理 ...
logic          [LogDepth:0] async_data_aw_wptr, async_data_aw_rptr,
                            async_data_w_wptr,  async_data_w_rptr,
                            // ... 每通道一对 wptr/rptr ...
```

- `async_data_*_data`：宽 `2**LogDepth` 的数据阵列，即 FIFO 的存储体，跨域直接走多根线（**数据靠 FIFO 协议保证一致，不走 Gray**，只有指针走 Gray）。
- `async_data_*_wptr` / `async_data_*_rptr`：宽 `LogDepth+1` 的 Gray 指针，各跨域一次。
- **这三类（数据、写指针、读指针）正是模块注释反复强调「必须约束」的三条路径**——后面 4.3 会展开。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：建立「一个 Gray CDC FIFO = 数据阵列 + 一对跨域 Gray 指针」的直觉。
2. **步骤**：
   - 打开 [`src/axi_cdc.sv:49-58`](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc.sv#L49-L58)。
   - 数一数：五条通道，每条声明了 1 个数据阵列 + 1 个 wptr + 1 个 rptr，共 `5 × 3 = 15` 组跨域信号。
3. **需要观察的现象**：数据阵列位宽是 `2**LogDepth`，指针位宽是 `LogDepth+1`，两者都随 `LogDepth` 变化——印证「FIFO 深 = `2**LogDepth`，指针需多一位区分满 / 空」。
4. **预期结果**：你能向同事口述「`LogDepth=2` 时每条通道有 4 个数据槽、3 位指针」。
5. 本步为纯阅读，无需运行工具，**确定性结论**。

#### 4.1.5 小练习与答案

**练习 1**：为什么跨域传递的是「Gray 指针」而不是「Gray 化的数据」？
**答**：数据每拍都可能整体变化、跨域时多位同时翻转无法用 Gray 保证；但数据不需要被「采样成正确值」——它写入 FIFO 后由本地时钟读出，靠「写完才能读」的 FIFO 协议保证一致即可。真正会被对端「异步采样并立即比较」的只有指针，而指针每次只 `+1`，Gray 化后单比特变化，可安全同步。所以 Gray 只作用在指针上。

**练习 2**：指针为什么用 `LogDepth+1` 位而不是 `LogDepth` 位？
**答**：`LogDepth` 位只能编码 `2**LogDepth` 个地址，无法区分「满」与「空」（两者读写地址都相等）。多用一位作「折回位」，当地址部分相等而这一位不同则为「满」、相同则为「空」。

---

### 4.2 axi_cdc 的五通道布局与 src/dst 拆分

#### 4.2.1 概念说明

AXI 有五条**方向不同**的通道（回顾 u1-l3）：请求方向的 AW/W/AR 由 master 发出，响应方向的 B/R 由 slave 发出。跨时钟域时，每条通道的数据流方向决定了「FIFO 的写半边落在哪个时钟域、读半边落在哪个时钟域」：

- 请求通道（AW/W/AR）：数据从 src（master 侧）流向 dst（slave 侧）→ **写半边在 src_clk 域、读半边在 dst_clk 域**。
- 响应通道（B/R）：数据从 dst（slave 侧）流回 src（master 侧）→ **写半边在 dst_clk 域、读半边在 src_clk 域**。

`axi_cdc` 把这些 FIFO 半边按时钟域打包成两个子模块：`axi_cdc_src`（全在 `src_clk_i`）与 `axi_cdc_dst`（全在 `dst_clk_i`）。这样一个完整 CDC 被拆成「分处两个时钟域的两块」，便于在层级化设计中分别综合、分别约束（u8-l2 会专门讲这种拆分动机）。

#### 4.2.2 核心流程

`axi_cdc` 顶层自己**几乎不写逻辑**，只做三件事：

1. 声明 15 组 `(* async *)` 跨域信号（见 4.1.3）。
2. 例化 `i_axi_cdc_src`，把 src 侧的 `axi_req_t`/`axi_resp_t` 与这些跨域信号接上。
3. 例化 `i_axi_cdc_dst`，把 dst 侧的 `axi_req_t`/`axi_resp_t` 与同一批跨域信号接上。

每条通道的「写半 / 读半」落点如下表（这是本讲最该记的一张表）：

| 通道 | AXI 方向 | 数据流向 | 写半（push）所在模块 / 时钟 | 读半（pop）所在模块 / 时钟 |
|------|----------|----------|------------------------------|-----------------------------|
| AW | 请求 | src→dst | `axi_cdc_src` / `src_clk_i` | `axi_cdc_dst` / `dst_clk_i` |
| W  | 请求 | src→dst | `axi_cdc_src` / `src_clk_i` | `axi_cdc_dst` / `dst_clk_i` |
| AR | 请求 | src→dst | `axi_cdc_src` / `src_clk_i` | `axi_cdc_dst` / `dst_clk_i` |
| B  | 响应 | dst→src | `axi_cdc_dst` / `dst_clk_i` | `axi_cdc_src` / `src_clk_i` |
| R  | 响应 | dst→src | `axi_cdc_dst` / `dst_clk_i` | `axi_cdc_src` / `src_clk_i` |

> **关键术语辨析**：`cdc_fifo_gray_src` / `cdc_fifo_gray_dst` 原语名里的 `_src`/`_dst` 指的是「FIFO 的写半 / 读半」（拓扑角色），**不是**「src 时钟域 / dst 时钟域」。例如响应通道 B 的**读半**（`cdc_fifo_gray_dst`）被例化在 `axi_cdc_src` 里、用 `src_clk_i` 驱动——名字里带 `dst` 却跑在 src 时钟。这种「名字重载」初看易混，记住「`_src`=写半、`_dst`=读半」就不会错。

#### 4.2.3 源码精读

**顶层 `axi_cdc` 的端口与参数** —— [src/axi_cdc.sv:24-47](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc.sv#L24-L47)：

```systemverilog
module axi_cdc #(
  parameter type aw_chan_t = logic, ... parameter type axi_resp_t = logic,
  parameter int unsigned LogDepth   = 1,   // FIFO 深 = 2**LogDepth
  parameter int unsigned SyncStages = 2    // 指针同步器级数
) (
  input  logic      src_clk_i,    input  logic      dst_clk_i,
  input  logic      src_rst_ni,   input  logic      dst_rst_ni,
  input  axi_req_t  src_req_i,    output axi_req_t  dst_req_o,
  output axi_resp_t src_resp_o,   input  axi_resp_t dst_resp_i
);
```

要点：两侧各自有**独立的时钟与复位**（异步复位各自处理）；src 侧是 slave 端口（`req_i`/`resp_o`），dst 侧是 master 端口（`req_o`/`resp_i`），与 u2-l4 的 `req_t`/`resp_t` 约定完全一致。通道类型 `aw_chan_t` 等是参数化类型，由调用方（如 `axi_cdc_intf`）用 `AXI_TYPEDEF_*` 宏生成。

**顶层把两半连起来** —— [src/axi_cdc.sv:60-122](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc.sv#L60-L122)：例化 `i_axi_cdc_src` 与 `i_axi_cdc_dst`，把 15 组跨域信号用 `(* async *)` 属性逐根对接。`(* async *)` 是给综合 / 时序工具的提示：**这些路径不要按单一时钟去约束**，它们是跨域的、由 FIFO 协议保证安全的——这正是 4.3 要讲的「三条路径」需要专门处理的原因。

**`axi_cdc_src` 内部：五条通道的 FIFO 半边** —— [src/axi_cdc_src.sv:60-154](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc_src.sv#L60-L154)。以 AW（请求，写半）与 B（响应，读半）对照：

```systemverilog
// AW 请求：src 侧发起 → 写半 (cdc_fifo_gray_src)，src_clk_i 驱动
cdc_fifo_gray_src #(.T(aw_chan_t), ...) i_cdc_fifo_gray_src_aw (
  .src_clk_i, .src_rst_ni,
  .src_data_i  (src_req_i.aw), .src_valid_i(src_req_i.aw_valid),
  .src_ready_o (src_resp_o.aw_ready),
  .async_data_o(async_data_master_aw_data_o),
  .async_wptr_o(async_data_master_aw_wptr_o),
  .async_rptr_i(async_data_master_aw_rptr_i) );

// B 响应：数据回到 src 侧 → 读半 (cdc_fifo_gray_dst)，仍用 src_clk_i 驱动
cdc_fifo_gray_dst #(.T(b_chan_t), ...) i_cdc_fifo_gray_dst_b (
  .dst_clk_i(src_clk_i), .dst_rst_ni(src_rst_ni),      // ← 注意：读半跑在 src 时钟
  .dst_data_o(src_resp_o.b), .dst_valid_o(src_resp_o.b_valid),
  .dst_ready_i(src_req_i.b_ready),
  .async_data_i(async_data_master_b_data_i),
  .async_wptr_i(async_data_master_b_wptr_i),
  .async_rptr_o(async_data_master_b_rptr_o) );
```

注意 B 通道那行 `.dst_clk_i(src_clk_i)`：原语端口名叫 `dst_clk_i`（读半时钟），实际接的是 `src_clk_i`——这就是 4.2.2 强调的「名字重载」的实物证据。`axi_cdc_dst`（[src/axi_cdc_dst.sv:60-155](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc_dst.sv#L60-L155)）是镜像结构：请求通道用读半、响应通道用写半，全部 `dst_clk_i`。

> 代码里还散布着 `` `ifdef QUESTA `` 分支（如 [src/axi_cdc_src.sv:62-66](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc_src.sv#L62-L66)），用 `logic [$bits(...)-1:0]` 扁平向量替代结构体类型参数，绕开 Questa 对带结构体类型参数的 bug；其他工具（VCS 等）则直接用结构体。这是兼容多 EDA 工具的小细节，不影响功能。

#### 4.2.4 代码实践（运行测试台）

1. **目标**：用两个不同周期的时钟实际驱动 `axi_cdc`，验证跨域读写功能正确。
2. **步骤**：
   - 阅读 [`test/tb_axi_cdc.sv:27-32`](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_cdc.sv#L27-L32)：上游（src）时钟 `TCLK_UPSTREAM = 10ns`、下游（dst）时钟 `TCLK_DOWNSTREAM = 3ns`——刻意不同的周期，制造真实异步关系。
   - 阅读 DUT 例化 [`test/tb_axi_cdc.sv:118-131`](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_cdc.sv#L118-L131)：用的是接口外壳 `axi_cdc_intf`，`LOG_DEPTH=2`（即 FIFO 深 4）。
   - 运行（需要 QuestaSim + Bender，先按 u1-l4 装好工具链）：
     ```bash
     make compile.log          # 生成并编译按 Level 排序的文件列表
     make sim-axi_cdc.log      # 只跑 tb_axi_cdc
     # 或直接：scripts/run_vsim.sh axi_cdc
     ```
3. **需要观察的现象**：日志里 `axi_rand_master` 向上游发 1000 笔随机读写（[`tb_axi_cdc.sv:152-155`](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_cdc.sv#L152-L155)），`axi_rand_slave` 在下游响应（[`tb_axi_cdc.sv:173-176`](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_cdc.sv#L173-L176)）；两个 monitor 进程用队列做 AB 比对（[`tb_axi_cdc.sv:197-258`](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_cdc.sv#L197-L258)）——上游把 AW/W/AR 入队、对 B/R 出队断言；下游对 AW/W/AR 出队断言、把 B/R 入队。任何一拍跨域数据出错都会触发 `assert` 失败。
4. **预期结果**：仿真结尾打印 `Errors: 0,`（脚本据此判通过，见 u1-l4）。**待本地验证**：具体能否跑通取决于本机是否装了 QuestaSim 与能否拉到 common_cells 依赖。
5. **延伸**：用 `--random-seed` 跑多种子回归，观察不同 `sv_seed` 下是否都 `Errors: 0,`——随机种子正是放大 CDC 时序 bug 的手段。

#### 4.2.5 小练习与答案

**练习 1**：在 `axi_cdc_src` 里，B 通道用的是 `cdc_fifo_gray_dst`（读半），却接在 `src_clk_i` 上。请解释为什么。
**答**：B 是响应通道，数据由 dst（slave）侧产生、流回 src（master）侧。`axi_cdc_src` 整个模块都跑在 src 时钟域，所以它持有的只能是 B 通道的「读半」（把已经跨域过来的 B 数据按 src 时钟读出来交给 master）。原语名 `_dst` 表示「FIFO 读半」这一拓扑角色，与时钟域无关。

**练习 2**：`axi_cdc` 顶层自己写了多少行真正的「逻辑」？为什么这么少？
**答**：几乎为零——它只声明跨域信号、例化 `i_axi_cdc_src`/`i_axi_cdc_dst` 并把它们连线。这是「组合优于配置」哲学（见 u1-l1）的体现：跨域安全性的全部难度都封装在 common_cells 的 `cdc_fifo_gray` 原语里，`axi_cdc` 只负责按 AXI 五通道把它们「每条摆一个」并接好端口。

---

### 4.3 LogDepth、SyncStages 与三条异步路径约束

#### 4.3.1 概念说明

`axi_cdc` 对外暴露两个旋钮（[src/axi_cdc.sv:32-35](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc.sv#L32-L35)）：

- **`LogDepth`**（默认 1）：FIFO 深度 = \(2^{\text{LogDepth}}\)。深度越大，越能吸收两域速率差、吞吐越高，但面积（每通道一组 `2**LogDepth` 槽的寄存器阵列）也越大。
- **`SyncStages`**（默认 2）：指针同步器的级数。级数越多，亚稳态 MTBF 越高，但指针跨域延迟多一拍，对「满 / 空」判定的保守程度略增（吞吐微降）。

此外，模块注释反复强调的「**三条路径必须正确约束**」是综合 / STA 阶段的硬性要求，不是 RTL 逻辑问题。每条 AXI 通道、每个 CDC FIFO 都有三条物理上跨时钟域的路径需要告诉时序工具「别用单一时钟去卡它们」。

#### 4.3.2 核心流程

**深度与位宽的关系**：

\[
\text{FIFO 深度} = 2^{\text{LogDepth}}, \qquad \text{指针位宽} = \text{LogDepth}+1
\]

默认 `LogDepth=1` → 每通道深 2、指针 2 位；`tb_axi_cdc` 用 `LOG_DEPTH=2` → 深 4、指针 3 位。tb 之所以给到 4，是因为上下游时钟频率差异大（10ns vs 3ns，下游快 ~3.3 倍），深一点的 FIFO 能减少「写侧被满信号压住」的次数、保住随机测试的吞吐。

**三条异步路径**（每个通道、每个 FIFO 都有，五通道共 15 组）：直接对应 4.1.3 看到的三类信号——

1. **数据阵列路径**：`async_data_*_data`（写侧时钟域 → 读侧时钟域），多比特，靠 FIFO 协议保证读时已稳定。
2. **写指针路径**：`async_data_*_wptr`（写侧 → 读侧），Gray 码，经 `SyncStages` 同步器。
3. **读指针路径**：`async_data_*_rptr`（读侧 → 写侧），Gray 码，经 `SyncStages` 同步器。

时序工具默认会试图用某个时钟去约束所有路径，对这三条跨域路径会报「无相关时钟」或错误地用捕获时钟去检查建立时间——必须显式声明它们是异步的（`set_false_path` 或 `set_clock_groups -asynchronous`，工具语法的 SDC/XDC 各异）。

#### 4.3.3 源码精读

参数声明见 [src/axi_cdc.sv:32-35](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc.sv#L32-L35)；它们被原样下传给 `i_axi_cdc_src`/`i_axi_cdc_dst`（[src/axi_cdc.sv:68-69](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc.sv#L68-L69)），再各自下传给每个 `cdc_fifo_gray_*` 原语的 `LOG_DEPTH`/`SYNC_STAGES`（如 [src/axi_cdc_src.sv:67-68](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc_src.sv#L67-L68)）。

`(* async *)` 综合属性散布在 `i_axi_cdc_src`/`i_axi_cdc_dst` 的例化端口映射上（[src/axi_cdc.sv:75-89](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc.sv#L75-L89)、[src/axi_cdc.sv:107-121](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc.sv#L107-L121)）。它是 RTL 层面的「自我声明」：作者已确认这些线是跨域的。但 `(* async *)` 只对部分工具有效，**它不能替代你在约束文件里写明这三条路径**——这也是模块注释用「IMPORTANT / MUST」措辞的原因。

> 接口外壳 `axi_cdc_intf`（[src/axi_cdc.sv:130-193](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc.sv#L130-L193)）只是把 `AXI_BUS` 接口用 `AXI_TYPEDEF_*` / `AXI_ASSIGN_*` 宏翻译成结构体再喂给 `axi_cdc`，是标准的「接口外壳 + 结构体内核」范式（u2-l4），不引入新的跨域路径。

#### 4.3.4 代码实践（写约束）

1. **目标**：为 `axi_cdc` 写出针对每条通道、覆盖三条路径的异步约束。
2. **步骤**（以 AW 通道为例，其余四通道类推）：
   - 识别三个跨域对象：`async_data_aw_data[*]`、`async_data_aw_wptr`、`async_data_aw_rptr`。
   - 用你所用工具的语法把它们声明为跨 `src_clk`/`dst_clk` 的异步路径。下面是一段**示例 SDC**（本仓库未随附约束文件，此为示例代码，需按你的实例层次调整层级路径）：
     ```tcl
     # 示例代码：声明 src_clk 与 dst_clk 互为异步
     set_clock_groups -asynchronous \
         -group [get_clocks src_clk] \
         -group [get_clocks dst_clk]
     # 或对每条路径显式设 false path（AW 通道示例）
     set_false_path -from [get_clocks src_clk] -to   [get_clocks dst_clk] \
         -through [get_pins -hier "*i_cdc_fifo_gray_*_aw*/*"]
     ```
3. **需要观察的现象**：加上约束后，STA 报告里这 15 组路径不再出现「无法找到相关时钟」或「跨域建立时间违例」的错误；去掉约束则会冒出大量跨域违例——这正是「必须约束」的可观测证据。
4. **预期结果**：每条通道的三条路径都被工具识别为 async、不计入同域时序检查。**待本地验证**：具体命令与层级路径取决于综合工具与你的例化实例名。
5. **延伸思考**：把 `SyncStages` 从 2 改成 1 重新综合做 STA，观察 MTBF 报告（部分工具提供）的变化——这是「同步器级数 ↔ 可靠性」最直观的体验。

#### 4.3.5 小练习与答案

**练习 1**：把 `LogDepth` 从 1 加大到 4，对面积、吞吐、MTBF 各有什么影响？
**答**：面积——每通道数据阵列从 2 槽变 16 槽，五通道共翻 8 倍寄存器；吞吐——FIFO 更深，更能吸收两域速率差，随机压测下被「满 / 空」压住的概率下降，吞吐上升；MTBF——基本不变，因为 MTBF 由 `SyncStages`（同步器级数）决定，与 FIFO 深度无关。

**练习 2**：为什么不能「干脆把 `(* async *)` 去掉、也不写约束」让工具自己处理？
**答**：工具默认用某个时钟去约束所有路径，对真正跨域的 15 组路径会要么报「无相关时钟」要么错误地用捕获时钟检查建立时间，产生海量虚假违例，掩盖真实问题；更糟的是可能把跨域路径当成同域路径去优化时序，反而插入不必要逻辑或误判。显式约束是让工具「知道这条路是安全的、别管它」，与 RTL 的 `(* async *)` 相互印证、缺一不可。

---

## 5. 综合实践

把本讲三块知识串成一条完整的「跨域互联」任务：

**场景**：你有一个跑在 50 MHz（周期 20ns）的 CPU（master），要访问一个跑在 200 MHz（周期 5ns）的外设子系统（slave），两者用 AXI4 互联。

1. **选模块**：用 `axi_cdc_intf`（接口外壳）而非裸 `axi_cdc`，省去手写 `AXI_TYPEDEF_*` 的样板——参考 [`src/axi_cdc.sv:130-193`](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc.sv#L130-L193)。CPU 侧接 `src`（slave，50 MHz），外设侧接 `dst`（master，200 MHz）。
2. **定参数**：因下游（200 MHz）比上游快 4 倍，请求方向（src→dst）几乎不会积压，`LogDepth=1`（深 2）通常够；但响应方向数据回得快、上游读得慢，B/R 的 FIFO 易被「满」压住，可考虑整体 `LogDepth=2`（深 4）。`SyncStages` 保持默认 2。
3. **画框图**：画出五条通道、每条通道一个 Gray FIFO，标注请求通道「写半在 src、读半在 dst」、响应通道相反——即 4.2.2 那张表。
4. **写约束**：为五条通道 × 三条路径 = 15 组路径写异步约束（4.3.4）。
5. **验证**：仿照 [`test/tb_axi_cdc.sv`](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_cdc.sv) 把两套时钟设成 20ns / 5ns，用 `axi_rand_master`/`axi_rand_slave` + 队列 monitor 跑随机回归，确认 `Errors: 0,`。

完成此实践，你就具备了一个最小可用的「双时钟域 AXI 子网」。

## 6. 本讲小结

- `axi_cdc` 是一座「五通道各一个 Gray 异步 FIFO」的跨时钟域桥，它自己几乎不写逻辑，安全性全部封装在外部 common_cells 的 `cdc_fifo_gray` 原语里（依赖 `common_cells 1.39.0`）。
- 跨域安全靠**两件套**：同步器（亚稳态收敛）+ **Gray 码指针**（单比特变化，可安全异步采样）；数据本身不走 Gray，靠 FIFO「写完才能读」的协议保证一致。
- 每条通道的 FIFO 按 AXI 方向决定半边落点：请求通道（AW/W/AR）写半在 src、读半在 dst；响应通道（B/R）相反。原语名 `_src`/`_dst` 指「写半 / 读半」拓扑角色，**不指时钟域**——别被名字骗了。
- `axi_cdc` 被拆成 `axi_cdc_src`（全 src_clk）与 `axi_cdc_dst`（全 dst_clk）两半，用 15 组 `(* async *)` 扁平信号互连，便于分别综合与约束（u8-l2 专门讲这种拆分）。
- 两个参数：`LogDepth`（深度 = \(2^{\text{LogDepth}}\)）权衡面积与吞吐；`SyncStages`（默认 2）权衡 MTBF 与延迟。
- 每条通道**必须单独约束三条路径**：数据阵列、写指针、读指针；`(* async *)` 属性不能替代约束文件里的显式异步声明。

## 7. 下一步学习建议

- **直接后继**：u8-l2 将拆解 `axi_cdc_src`/`axi_cdc_dst` 的独立接口版（`axi_cdc_src_intf`/`axi_cdc_dst_intf`）以及它们用的 `AXI_BUS_ASYNC_GRAY` 接口（[src/axi_intf.sv:358-406](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_intf.sv#L358-L406)），讲清「把一个 CDC 一分为二」的工程动机与异步阵列如何穿越模块边界。
- **深入底层**：去 `common_cells` 仓库读 `cdc_fifo_gray.sv` 原文，对照本讲讲的「数据阵列 + Gray 指针 + 同步器」三件套，验证满 / 空判定逻辑。
- **横向联系**：回顾 u7-l1（`axi_fifo` / `spill_register`），对比「同域 FIFO」与「异步 FIFO」在安全前提上的本质区别；在 u15-l4「异构网络设计实战」里，`axi_cdc` 会和 `axi_xbar`、`axi_dw_converter`、`axi_iw_converter` 一起拼成真实的多域片上网络。
