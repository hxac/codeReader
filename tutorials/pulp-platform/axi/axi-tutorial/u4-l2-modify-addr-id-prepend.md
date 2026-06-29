# axi_modify_address 与 axi_id_prepend

## 1. 本讲目标

上一讲（u4-l1）我们学了三个「不改写任何字段、只决定切不切断组合路径」的连接器：`axi_join`、`axi_cut`、`axi_multicut`。本讲往上走一步，学习两个会**主动改写 AXI 字段**的连接器：

- `axi_modify_address`：转发请求时，把 AW/AR 通道的**地址**替换成外部提供的新地址；
- `axi_id_prepend`：转发请求时，在 ID 的**最高位前面拼接**若干比特，回送响应时再把这些比特**剥离**掉。

学完本讲你应该能够：

1. 用 `axi_modify_address` 实现地址翻译 / 重映射（例如整体偏移 `+0x1000`、地址宽度转换）；
2. 说清 `axi_id_prepend` 为什么在层级化互联里能「保留来源信息」，并明白它正是上一讲提到的「xbar master 端口 ID 比 slave 端口宽」这件事的具体实现；
3. 看懂这两个模块「请求端口与响应端口拆开」的接线结构，知道新地址 / 新 ID 是从哪个端口喂进去的。

---

## 2. 前置知识

本讲默认你已经掌握 u4-l1（`axi_join` 的纯连线语义、`req_t`/`resp_t` 结构体、valid/ready 握手）以及以下两个更早的概念：

- **五通道与方向**（u1-l3 / u2-l3）：写事务走 AW/W/B、读事务走 AR/R。AW/W/AR 由请求方（Master）发出，B/R 由响应方（Slave）发出。本讲把「请求方向」（slv→mst）和「响应方向」（mst→slv）分得很清楚，请记住这两股方向相反的数据流。
- **`req_t` / `resp_t` 结构体**（u2-l4）：请求方驱动的信号打包成 `req_t`（含 AW/W/AR 载荷 + valid、B/R 的 ready），响应方驱动的信号打包成 `resp_t`（含 B/R 载荷 + valid、AW/AR 的 ready）。通道结构体（如 `aw_chan_t`）是 `struct packed { id; addr; len; size; burst; ... }`，`id` 在最高位。

两个本讲要反复用到的关键事实（来自 typedef.svh，下面会引用源码）：

- AW/AR/B/R 四个通道结构体里，`id` 都是**第一个字段**，也就是 packed 后的**最高位**；
- AXI 协议要求：`valid` 拉高后、握手（`valid && ready`）发生前，通道载荷**必须保持稳定**（u2-l3）。

后者是理解本讲多处「地址必须在握手期间保持稳定」这类约束的根。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/axi_modify_address.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_modify_address.sv) | 地址改写连接器，含结构体版 `axi_modify_address` 与接口版 `axi_modify_address_intf` |
| [src/axi_id_prepend.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_prepend.sv) | ID 前置/剥离连接器，支持多总线阵列 |
| [src/axi_mux.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv) | `axi_id_prepend` 的真实调用方：每个 slave 端口拼上自己的端口号 |
| [test/tb_axi_modify_address.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_modify_address.sv) | `axi_modify_address` 的随机自检测试台，是本讲实践的范本 |
| [include/axi/typedef.svh](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/include/axi/typedef.svh) | 通道结构体定义，解释「为什么截断高位 = 剥离 prepend 的 ID」 |

---

## 4. 核心概念与源码讲解

### 4.1 axi_modify_address：地址改写连接器

#### 4.1.1 概念说明

`axi_modify_address` 解决的问题是：在把一条 AXI 总线原样转发给下游时，**想把地址换掉**。典型场景：

- 地址翻译 / 重映射：上游看到的地址和下游物理地址之间有一个固定关系（偏移、分页、查表结果）；
- 地址宽度转换：上游是 32 位地址、下游是 48 位地址（或反过来）；
- 在一个地址译码器旁边，把「逻辑地址」翻译成「物理地址」再发给真正的从端。

这个模块最重要的设计取舍是：**它自己不算新地址**。新地址是模块外部算好、通过一个独立端口 `mst_aw_addr_i` / `mst_ar_addr_i` 喂进来的。模块本体只做一件事——把请求里除了地址以外的所有字段原样复制，地址字段替换成外部输入。

这种「算法在外面、模块只做替换」的分工，让同一个模块可以服务于任意翻译规则（加常数、查表、分页拼接……），符合本库「组合优于配置」的哲学（u1-l1）：需要什么翻译就在外面接什么组合逻辑，而不是往模块里塞参数。

#### 4.1.2 核心流程

模块是**纯组合逻辑**，没有任何寄存器，数据流是直通的：

```
            请求方向 (slv -> mst)                   响应方向 (mst -> slv)
┌──────────┐   slv_req_i   ┌─────────────────┐  mst_req_o   ┌──────────┐
│ 上游     │ ────────────▶ │                 │ ───────────▶ │ 下游     │
│ (master) │               │ axi_modify_     │              │ (slave)  │
│          │ ◀──────────── │   address       │ ◀─────────── │          │
└──────────┘   slv_resp_o  │                 │  mst_resp_i  └──────────┘
                            └─────────────────┘
                                   ▲  ▲
                  mst_aw_addr_i ───┘  └─── mst_ar_addr_i
                  （外部算好的新 AW/AR 地址）
```

- **请求方向**：把 `slv_req_i` 的每个字段复制到 `mst_req_o`，唯独 `aw.addr ← mst_aw_addr_i`、`ar.addr ← mst_ar_addr_i`；
- **响应方向**：`slv_resp_o = mst_resp_i`，B/R 响应**原样回送**，一个比特都不改；
- W 通道、所有 valid/ready 握手信号都原样直通（它们都被打包在 `slv_req_i` / `mst_resp_i` 里一起复制过去）。

因为地址只是「替换」不是「参与计算」，所以请求方向没有任何时序冒险——但要满足一个关键约束（见 4.1.3）。

#### 4.1.3 源码精读

**模块端口**：请求与响应是拆开的两组端口，外加两个地址输入：

[src/axi_modify_address.sv:L18-L40](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_modify_address.sv#L18-L40) —— 注意四个类型参数：`slv_req_t`（slave 侧请求，含 slave 地址宽度）、`mst_addr_t`（master 侧地址类型）、`mst_req_t`（master 侧请求，含 master 地址宽度）、`axi_resp_t`（两侧共用响应类型）。slave 与 master 的地址宽度可以不同，这就是它能做「地址宽度转换」的来源。

端口里有两个关键注释（L32、L34）：

> AW address on master port; must remain stable while an AW handshake is pending.

这句话是本模块使用上**最重要的约束**：`mst_aw_addr_i`（以及 `mst_ar_addr_i`）必须在一次 AW（或 AR）握手完成前保持稳定。原因有二：

1. 模块把 `mst_aw_addr_i` 直接复制进 `mst_req_o.aw.addr`。而 AXI 规定 `aw_valid` 拉高期间载荷不得变化（u2-l3）。因此 `mst_aw_addr_i` 也不得在这段时间内变化。
2. 这条约束落在调用方头上，因为模块本身无从知道「地址该在什么时候保持」，只能由外部供给地址的电路保证。

**核心赋值**——一个结构体整体赋值，逐字段复制：

[src/axi_modify_address.sv:L42-L79](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_modify_address.sv#L42-L79) —— 关键看 AW 通道：

```systemverilog
aw: '{
  id:     slv_req_i.aw.id,
  addr:   mst_aw_addr_i,     // <-- 只有这里换成了外部输入
  len:    slv_req_i.aw.len,
  size:   slv_req_i.aw.size,
  burst:  slv_req_i.aw.burst,
  ...                          // 其余字段全部照抄 slv_req_i
  default: '0                  // 结构体里未列出的字段补 0
},
aw_valid: slv_req_i.aw_valid,  // 握手信号也照抄
```

AR 通道同理（L62-L76），只有 `addr: mst_ar_addr_i` 是替换来的，其余字段与 `slv_req_i` 一致。W 通道（L59-L60）和 `b_ready`、`r_ready`（L61、L77）更是整体照抄。

[src/axi_modify_address.sv:L81](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_modify_address.sv#L81) —— `assign slv_resp_o = mst_resp_i;`：响应方向一行解决，整块直通。

**接口版 `axi_modify_address_intf`**：内核只面对结构体，外面套一层 `AXI_BUS` 接口。它先用 typedef 宏为 slave / master 两侧分别声明**两套**地址宽度不同的 AW/AR 通道类型，再用 `AXI_ASSIGN_TO_REQ` / `AXI_ASSIGN_FROM_RESP` 宏在接口与结构体之间搬数据：

[src/axi_modify_address.sv:L113-L128](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_modify_address.sv#L113-L128) —— 注意 `slv_aw_chan_t` 用 `slv_addr_t`，`mst_aw_chan_t` 用 `mst_addr_t`，二者地址宽度可以不同。这正是测试台里 32 位 slave → 48 位 master 能跑通的依据。

#### 4.1.4 代码实践

> 实践目标：用 `axi_modify_address_intf` 把所有访问地址整体偏移 `+0x1000`，并用一个最小测试台验证下游收到的地址确实被改写了。

**操作步骤**（基于现有测试台 `test/tb_axi_modify_address.sv` 改造，这是「源码阅读 + 接线」型实践）：

1. 复制 `test/tb_axi_modify_address.sv` 为一份新的最小 TB（例如 `tb_axi_modify_addr_offset.sv`），把它当成骨架。
2. 让 slave / master 地址宽度相等，都设 32 位（偏移不改宽度）：
   ```systemverilog
   parameter int unsigned AXI_SLV_PORT_ADDR_WIDTH = 32,
   parameter int unsigned AXI_MST_PORT_ADDR_WIDTH = 32,
   ```
3. 把原 TB 中「低 12 位照抄、高位随机化成页号」的逻辑（[test/tb_axi_modify_address.sv:L159-L166](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_modify_address.sv#L159-L166)）替换成一句加法：
   ```systemverilog
   // 把上游地址整体偏移 +0x1000
   assign mst_aw_addr = upstream.aw_addr + 32'h1000;
   assign mst_ar_addr = upstream.ar_addr + 32'h1000;
   ```
   - 这里用接口信号 `upstream.aw_addr` 直接参与运算，结果喂给 `mst_aw_addr_i`，模块会把它替换进 `mst_req_o.aw.addr`。
   - 因为加法是**纯组合**且其输入 `upstream.aw_addr` 在 `aw_valid` 期间天然稳定（AXI 协议保证），所以 `mst_aw_addr` 也会在握手期间稳定，自动满足 4.1.3 的约束。
4. 保留原 TB 的自检断言（[test/tb_axi_modify_address.sv:L213-L215](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_modify_address.sv#L213-L215)），其中 `aw_exp.addr` 由 `always_comb` 算成 `mst_aw_addr`（[test/tb_axi_modify_address.sv:L194-L199](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_modify_address.sv#L194-L199)），断言比较 `downstream`（下游）实际收到的 AW 与期望 AW 全等。

**运行方式**（参考 u1-l4）：

```bash
make sim-axi_modify_addr_offset.log   # 命名约定：sim-<tb 名>.log
```
或直接：
```bash
bender script vsim -t test -t rtl
# 编译后 vsim -t 1ps -c work.tb_axi_modify_addr_offset
```

**需要观察的现象**：

- 下游 `downstream.aw_addr` 比上游 `upstream.aw_addr` 恰好大 `0x1000`，对读地址同理。
- 自检断言 `aw` / `ar` 全程不报 `$error`，仿真日志出现 `Errors: 0,`。

**预期结果**：所有事务通过，下游看到的地址 = 上游地址 + 0x1000，B/R 响应原样回到上游。**待本地验证**：本环境未挂接仿真器，实际波形需你在本地跑通后确认。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `assign mst_aw_addr = upstream.aw_addr + 32'h1000;` 改成由一个寄存器在每个时钟沿重新随机化的值，会出什么问题？

> **答案**：会违反「`mst_aw_addr_i` 必须在 AW 握手期间保持稳定」的约束。当 `aw_valid` 高、`aw_ready` 还没来时，若 `mst_aw_addr` 在某个沿跳变，下游会在同一笔事务里看到两个不同的地址，违反 AXI「valid 期间载荷稳定」规则，可能导致下游锁存到错误地址。

**练习 2**：为什么模块本体不直接提供一个 `addr_offset` 参数、内部做加法，而要把新地址放到外部端口？

> **答案**：为了保持模块的通用性。把「如何算新地址」留给调用方，模块就能支持任意翻译规则（偏移、查表、分页拼接、宽度转换……）。如果内置加法，就只能做线性偏移，遇到查表翻译还得另写一个模块——违背「组合优于配置」（u1-l1）。

**练习 3**：B/R 响应需要做对应的「反向地址改写」吗？

> **答案**：不需要。B/R 通道里根本没有地址字段（B 只有 `id/resp/user`，R 只有 `id/data/resp/last/user`），所以 [src/axi_modify_address.sv:L81](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_modify_address.sv#L81) 直接整块 `slv_resp_o = mst_resp_i` 即可。

---

### 4.2 axi_id_prepend：ID 前置 / 剥离

#### 4.2.1 概念说明

`axi_id_prepend` 解决的问题是：在把若干条 AXI 总线汇聚到一条更宽 ID 的总线上时，**怎样记住每一笔事务来自哪个上游端口**，好让响应能正确路由回去。

办法很直接：在请求方向的 ID **最高位前面拼接**一段「来源标签」（`pre_id_i`），到了响应方向再把这段标签**剥离**掉。

- 请求方向（slv→mst）：`mst.id = {pre_id_i, slv.id}`，新 ID 比 原 ID 宽 `PreIdWidth` 位；
- 响应方向（mst→slv）：把宽 ID 截回窄 ID，剥掉高位。

这个机制正是 u1-l1 留下的那个结论——**xbar 的 master 端口 ID 比 slave 端口宽 ⌈log2(NoSlvPorts)⌉ 位**——的物理实现：每个 slave 端口把自己的端口号拼进 ID 高位，响应回来时按高位分发。

#### 4.2.2 核心流程

```
请求方向 (slv -> mst)                          响应方向 (mst -> slv)
slv_aw.id[SW-1:0]  ──┐                  ┌── slv_b.id[SW-1:0]
                     │ concat 高位      │ 截断高位
pre_id_i[PW-1:0] ────┤──────────────▶   │◀──────────────
                     ▼                  ▼
                mst_aw.id[MW-1:0]   mst_b.id[MW-1:0]

其中 MW = SW + PW,  PW = AxiIdWidthMstPort - AxiIdWidthSlvPort
```

- 请求方向用拼接 `{pre_id_i, slv_id}`：原 ID 占低 `SW` 位，标签占高 `PW` 位；
- 响应方向用**位流类型转换**（bit-stream cast）：把宽通道结构体直接转回窄通道结构体，靠的是「`id` 在结构体最高位」这一排列（见 4.2.3）；
- W 通道和所有 valid/ready 信号原样直通（它们不含 ID，或不含需要改写的字段）。

宽度关系是个硬约束：master 端口 ID 必须比 slave 端口**宽**，且

\[
\text{PreIdWidth} = \text{AxiIdWidthMstPort} - \text{AxiIdWidthSlvPort}
\]

#### 4.2.3 源码精读

**参数与派生宽度**：

[src/axi_id_prepend.sv:L18-L34](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_prepend.sv#L18-L34) —— `PreIdWidth` 是派生参数（`DO NOT OVERWRITE`），由两个端口 ID 宽度之差算出。`pre_id_i` 的位宽就是 `PreIdWidth`（[src/axi_id_prepend.sv:L35](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_prepend.sv#L35)）。`NoBus` 允许一次处理多条总线（阵列），每条都拼同一个 `pre_id_i`。

**请求方向：拼接高位**：

[src/axi_id_prepend.sv:L81-L92](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_prepend.sv#L81-L92) —— 先看 `if (PreIdWidth == 0)` 分支：当两侧 ID 等宽时，无需拼接，AW/AR 直接整体赋值直通。否则在 `gen_prepend` 分支里：

```systemverilog
mst_aw_chans_o[i] = mst_aw_chan_t'(slv_aw_chans_i[i]);  // 先整体位流转换
mst_aw_chans_o[i].id = {pre_id_i, slv_aw_chans_i[i].id[AxiIdWidthSlvPort-1:0]};
```

第一行先把 slave 通道结构体按位流转换成 master 通道结构体（地址、len 等字段因为位置一致而被保留），第二行再把 ID 字段显式重写为 `{pre_id_i, 原 ID}`。AR 通道处理方式完全相同。

**响应方向：靠结构体字段顺序自动截断**：

[src/axi_id_prepend.sv:L93-L96](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_prepend.sv#L93-L96) —— 关键两行：

```systemverilog
assign slv_b_chans_o[i] = slv_b_chan_t'(mst_b_chans_i[i]);
assign slv_r_chans_o[i] = slv_r_chan_t'(mst_r_chans_i[i]);
```

源码注释解释了为什么这一句就够（L93-L94）：

> The ID is in the highest bits of the struct, so an assignment from a channel with a wide ID to a channel with a shorter ID correctly cuts the prepended ID.

要理解它，需要回到 typedef.svh 里 B / R 通道的字段顺序：

[include/axi/typedef.svh:L61-L66](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/include/axi/typedef.svh#L61-L66) —— B 通道是 `struct packed { id; resp; user; }`，`id` 在最前（最高位）。R 通道（[L85-L92](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/include/axi/typedef.svh#L85-L92)）同样是 `struct packed { id; data; resp; last; user; }`，`id` 也在最高位。

位流类型转换 `narrow_t'(wide)` 会保留低位、丢弃高位。由于 `id` 在最高位，丢弃的正好是 `id` 的高位（也就是 `pre_id_i` 那一段），保留下来的低位正好是「原 ID + resp + user」——这就是「剥离」的全部实现。一句话：**请求方向显式拼接，响应方向靠 `id` 在结构体最高位 + 位流截断自动剥离**。

**握手与 W 通道直通**：

[src/axi_id_prepend.sv:L99-L110](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_prepend.sv#L99-L110) —— W 通道载荷、以及五通道全部 valid/ready 信号都是一一对应的 `assign` 直通，因为它们不含 ID。

**断言自检**：

[src/axi_id_prepend.sv:L114-L158](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_prepend.sv#L114-L158) —— 一组 `initial` 断言检查宽度关系（如 `mst id > slv id`），一组 `assert final` 检查拼接 / 剥离是否真的正确。例如 [L123-L125](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_prepend.sv#L123-L125) 验证 master AW ID 的低 `SW` 位等于 slave AW ID，[L135-L137](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_prepend.sv#L135-L137) 验证 B 通道剥离正确。这些断言只在仿真生效（`pragma translate_off` / `ifndef VERILATOR`）。

**真实调用方：axi_mux**：

[src/axi_mux.sv:L211-L227](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L211-L227) —— `axi_mux` 给每个 slave 端口 `i` 例化一个 `axi_id_prepend`，并喂入 `.pre_id_i(switch_id_t'(i))`，也就是**把 slave 端口号当成来源标签拼进 ID 高位**。响应回到 mux 时，mux 就能从 `r_id` / `b_id` 的高位读出端口号，把响应分发回正确的 slave 端口。这正是 u1-l1「master 端口 ID 比 slave 端口宽 ⌈log2(NoSlvPorts)⌉ 位」的具体落地。

#### 4.2.4 代码实践

> 实践目标：阅读 `axi_mux` 中 `axi_id_prepend` 的实例化，亲手验证「端口号被拼进 ID 高位」这一机制，并理解它如何支撑响应路由。

**操作步骤**（源码阅读 + 推理型实践）：

1. 打开 [src/axi_mux.sv:L211-L227](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L211-L227)，确认 `NoBus = 32'd1`（每端口一条总线）、`pre_id_i = switch_id_t'(i)`。
2. 找到 `MstAxiIDWidth` 与 `SlvAxiIDWidth` 的定义（在 `axi_mux` 参数区），验证 `MstAxiIDWidth = SlvAxiIDWidth + $clog2(NoSlvPorts)`，即 `PreIdWidth = $clog2(NoSlvPorts)`。
3. 假设 `NoSlvPorts = 4`、`SlvAxiIDWidth = 4`，则 `PreIdWidth = 2`、`MstAxiIDWidth = 6`。请手算下表：

| Slave 端口 i | pre_id_i | 上游原 ID（4'b0011） | 下游 master ID（6 位） |
| --- | --- | --- | --- |
| 0 | 2'b00 | 0011 | 00_0011 |
| 1 | 2'b01 | 0011 | 01_0011 |
| 2 | 2'b10 | 0011 | 10_0011 |
| 3 | 2'b11 | 0011 | 11_0011 |

4. 在 `axi_mux` 里搜索 `switch_r_id`（[src/axi_mux.sv:L202](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L202)），看 mux 如何从返回的 R 响应 ID 的高位反解出端口号，从而把数据分发回正确 slave 端口。

**需要观察的现象 / 预期结果**：

- 同一个上游 ID（如 `0011`）来自不同 slave 端口时，下游看到的 master ID 高位不同，互不冲突；
- 响应回来时，剥离高位后，每个 slave 端口只收到 `0011` 这一原始 ID，路由正确；
- `PreIdWidth = 0`（即 `NoSlvPorts = 1`）时走 [src/axi_id_prepend.sv:L82-L84](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_prepend.sv#L82-L84) 的直通分支，零开销。

**待本地验证**：上述端口号→ID 映射可在 `test/tb_axi_mux.sv` 的随机回归里通过波形确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么响应方向不需要显式写 `{slv_id 部分}` 的拼接，而用一句类型转换就够了？

> **答案**：因为 typedef.svh 里 B/R 通道把 `id` 放在 `struct packed` 的最高位（[L61-L66](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/include/axi/typedef.svh#L61-L66)）。位流转换 `narrow_t'(wide)` 丢高位、保低位，正好丢掉 `id` 的高位（`pre_id_i` 那段），保留原 ID 与其它字段。若 `id` 不在最高位，这一招就会失效。

**练习 2**：如果设计者把 `AxiIdWidthMstPort` 设成等于 `AxiIdWidthSlvPort`，模块会怎样？

> **答案**：`PreIdWidth = 0`，generate 走 `gen_no_prepend` 分支（[L82-L84](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_prepend.sv#L82-L84)），AW/AR 整体直通；同时 `pre_id_i` 位宽为 0（空向量）。此时模块等价于一个不拼 ID 的纯连接器。

**练习 3**：`axi_id_prepend` 能像 `axi_modify_address` 那样支持两侧地址宽度不同吗？

> **答案**：不能，也不需要。`axi_id_prepend` 只动 ID 字段，地址（`addr`）在请求方向经位流转换原样保留、在响应方向根本不经过它（地址只在 AW/AR 上，由请求方向处理）。两侧地址宽度由各自通道结构体类型决定，本模块不约束也不改写地址。

---

## 5. 综合实践

把本讲两个模块串起来，设计一个「带来源标签的地址翻译」小连接器：上游是一条 ID 宽 4 位、地址宽 32 位的 AXI 总线；下游是一条 ID 宽 6 位、地址宽 48 位的总线。要求：

1. 在 ID 高位拼上固定来源标签 `pre_id_i = 2'b01`；
2. 同时把地址整体偏移 `+0x1000`；
3. 响应能正确回到上游。

请回答并设计：

- 这两步该用哪个模块、按什么顺序串联？（提示：`axi_modify_address` 改地址、`axi_id_prepend` 改 ID，二者字段正交，可以背靠背串联。）
- 画出两模块之间的结构体 / 接口连线草图，标出哪一段是「宽 ID + 新地址」、哪一段是「窄 ID + 旧地址」。
- 指出串联点上的关键约束：`axi_modify_address` 要求「新地址在握手期间稳定」，`axi_id_prepend` 是纯组合直通——这个串联会引入死锁或冒险吗？为什么？

> 参考思路：上游 → `axi_modify_address`（替换地址为 `addr + 0x1000`，ID 不变）→ `axi_id_prepend`（ID 高位拼 `2'b01`，地址直通）→ 下游。由于两模块都是纯组合、字段正交（一个只改 addr、一个只改 id），串联等价于一个同时改 addr 和 id 的组合逻辑，无寄存器、无反馈，不会死锁。响应方向反向：下游 B/R → `id_prepend` 剥离高位 → `modify_address` 整块直通（B/R 无地址）→ 上游。

---

## 6. 本讲小结

- `axi_modify_address` 是**纯组合**的地址替换器：请求方向把 AW/AR 的 `addr` 换成外部输入 `mst_aw_addr_i` / `mst_ar_addr_i`，其余字段（含 W、valid/ready、atop）原样复制；响应方向 `slv_resp_o = mst_resp_i` 整块直通。
- 新地址**由外部计算并喂入**，模块本身不算地址——这是「组合优于配置」的体现，使其能支持任意翻译规则与地址宽度转换。
- 关键使用约束：`mst_aw_addr_i` / `mst_ar_addr_i` **必须在对应握手完成前保持稳定**，否则违反 AXI「valid 期间载荷稳定」规则。
- `axi_id_prepend` 在请求方向把 `pre_id_i` 拼到 ID 最高位前（`{pre_id_i, slv_id}`），在响应方向靠「`id` 在结构体最高位 + 位流类型转换」自动剥离高位。
- `PreIdWidth = AxiIdWidthMstPort - AxiIdWidthSlvPort`，等于 0 时走纯直通分支；master 端口 ID 必须比 slave 端口宽。
- `axi_mux` 给每个 slave 端口拼上其端口号（`pre_id_i = switch_id_t'(i)`），这就是 xbar「master 端口 ID 更宽、用于路由响应」的物理实现。

---

## 7. 下一步学习建议

本讲的两个模块都属于「改写型连接器」，是更复杂模块的零件：

- 接下来学 **u4-l3（axi_delayer）**，看第三类连接器如何在不改写字段的前提下给每个通道施加随机延迟，用于验证。
- 之后进入 **U5 多路复用与路由核心**：`axi_demux` / `axi_mux`。其中 `axi_mux` 会直接调用本讲的 `axi_id_prepend`，学完本讲你已经掌握了它最关键的子零件。
- 如果你对「ID 宽度」这条线感兴趣，可以跳读 **U10（ID 宽度管理）** 里的 `axi_id_remap` / `axi_iw_converter`，它们处理的是比 `id_prepend` 更一般的「宽 ID ↔ 窄 ID」转换（需要维护在途事务表，而非简单拼接）。
