# axi_pkg：辅助函数与配置结构体

## 1. 本讲目标

[u2-l1 axi_pkg：类型与常量](u2-l1-axi-pkg-types.md) 把 `axi_pkg` 里的宽度常量、typedef、`BURST_*`/`RESP_*`/`CACHE_*`/`ATOP_*` 这些 localparam 读透了。但 `axi_pkg` 远不止是一个「常量字典」——它后半部分还藏着两类重要资产：

1. 一批 **`function automatic` 辅助函数**：给定 `size`/`len`/`burst`/`addr`，它们替你算出每拍该访问哪个字节、某次访问算不算可缓存、合并两个响应该取哪个优先级、一条 AXI 通道净荷到底多宽。这些函数是 `axi_demux`、`axi_dw_downsizer`、`axi_test` 等模块背后共享的「算术大脑」。
2. 一组 **配置结构体**：`xbar_cfg_t`、`xbar_rule_64_t`/`xbar_rule_32_t`、`xbar_latency_e`。它们是参数化模块（尤其 `axi_xbar`）对外暴露的「配置入口」——你不用改一行 RTL，只填一个 struct，就能把一个交叉开关的端口数、ID 宽度、地址映射规模、流水线深度全部定下来。

读完本讲，你应该能够：

- 调用 `num_bytes`、`aligned_addr`、`beat_addr` 等函数，手算一次突发里每一拍的字节范围；
- 看懂 `aw_width`/`w_width`/…/`req_width`/`rsp_width` 这一族「通道净荷宽度」函数如何由宽度常量相加得出，并能解释它们在打平（flatten）端口时的用途；
- 对照 `doc/axi_xbar.md` 的字段表，逐字段说清 `xbar_cfg_t` 的含义，并能照着测试台的写法**亲手写出一个 `xbar_cfg_t` 实例**；
- 理解 `xbar_latency_e` 枚举（`NO_LATENCY`、`CUT_ALL_AX` 等）如何用 10 个比特位编码「在哪个通道插 spill 寄存器」，并知道文档推荐用 `CUT_ALL_AX` 的原因。

本讲**承接** u2-l1 已建立的「宽度常量 ↔ typedef」配对关系——很多函数内部就是把这些宽度常量加起来。本讲**不**重复讲 `BURST_*`/`RESP_*`/`CACHE_*` 的取值含义，只把它们当作已知输入。

## 2. 前置知识

- 读过 [u2-l1](u2-l1-axi-pkg-types.md)，知道 `axi_pkg` 是全库唯一的 `package`、位于 Level 0，并知道 `size_t`、`len_t`、`burst_t`、`resp_t`、`cache_t`、`atop_t` 这些 typedef 的位宽来源。
- 读过 [u1-l3 AXI4 协议快速回顾](u1-l3-axi-protocol-primer.md)，知道 `len` = 拍数 − 1、`size` 表示每拍 \(2^{\text{size}}\) 字节、`BURST_INCR`/`WRAP` 的地址推进规则。
- 读得懂 SystemVerilog 的 `function automatic ... endfunction`、`struct packed`、`enum`、`typedef`、移位运算 `<<`/`>>`、位拼接 `{}`。不要求会写，能读懂即可。

几个新词先对齐：

- **净荷宽度（payload width）**：一条 AXI 通道（如 AW）上所有有效字段（不含握手 `valid`/`ready`）拼起来的总比特数。`aw_width` 之类的函数就是算它。
- **spill 寄存器（spill register）**：在一条通道上插入的一级寄存器，用来切断组合路径、改善时序，代价是加一拍延迟。`LatencyMode` 就是「在哪里插 spill」的总开关。
- **配置结构体（config struct）**：把一整套模块参数打包成一个 `struct packed`，传一个参数即可配置整模块。`xbar_cfg_t` 就是典型。

## 3. 本讲源码地图

本讲围绕两个文件，一个是「实现」，一个是「文档」：

| 文件 | 作用 |
|------|------|
| [src/axi_pkg.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv) | 全库 `package`。本讲精读它的**函数区**（L116–L541，含突发地址计算、cache/响应辅助、通道宽度计算、`iomsb`）和**配置区**（L449–L536，含 `xbar_latency_e`、`xbar_cfg_t`、`xbar_rule_64_t`/`xbar_rule_32_t`）。 |
| [doc/axi_xbar.md](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_xbar.md) | `axi_xbar` 的官方文档。本讲引用它的 **Configuration 字段表**（L42–L55）和 **Pipelining and Latency 小节**（L59–L65），用来逐字段解释 `xbar_cfg_t` 与 `LatencyMode` 的工程含义。 |

为证明这些配置真实被使用，本讲引用一个「消费者」作为活样本：

| 文件 | 用到的 `axi_pkg::` 内容 |
|------|--------------------------|
| [test/tb_axi_xbar.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_xbar.sv) | 用 `axi_pkg::xbar_cfg_t` 实例化配置、用 `axi_pkg::CUT_ALL_AX` 选延迟模式、用 `axi_pkg::xbar_rule_32_t` 定义地址规则（L66–L84）。这是本讲代码实践的范本。 |

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：突发地址/字节计算函数、cache/响应与通道宽度辅助函数、`xbar_latency_e` 延迟模式、`xbar_cfg_t` 与地址规则结构体。

### 4.1 突发地址与字节范围计算函数

#### 4.1.1 概念说明

AXI 一次突发（burst）由地址通道上的 `addr`、`size`、`len`、`burst` 四个字段完全决定。给定这四个数，每一拍该访问哪几个字节，是协议规定死的——但手算容易出错（尤其 `WRAP` 回卷）。`axi_pkg` 把这套算术抽成一组纯函数，让 `axi_demux`、`axi_dw_downsizer`、`axi_test` 等模块复用同一份正确实现，而不是各自重写。

先建立直觉。`size` 字段表示**每拍传输多少字节**：

\[ \text{Number\_Bytes} = 2^{\text{size}} \]

例如 `size=2` 表示每拍 4 字节。`len` 字段表示**拍数 − 1**（见 u1-l3），所以一次突发的总字节数是 \(\text{Number\_Bytes} \times (\text{len}+1)\)。

对 `BURST_INCR`，第 \(N\) 拍（\(N\) 从 0 起）的地址是「对齐地址 + \(N \times \text{Number\_Bytes}\)」。对 `BURST_WRAP`，地址递增到回卷边界后会绕回低位。`axi_pkg` 把这两种情况统一进同一个 `beat_addr`。

#### 4.1.2 核心流程

一拍字节地址的推算流程（对应函数调用链）：

1. `num_bytes(size)` → \(2^{\text{size}}\)，每拍字节数。
2. `aligned_addr(addr, size)` → 把 `addr` 的低 `size` 位清零，得到本突发的对齐地址。
3. 若 `burst == BURST_WRAP`：`wrap_boundary(addr, size, len)` → 算出回卷下界；否则跳过。
4. `beat_addr(addr, size, len, burst, i)` → 第 `i` 拍的地址（INCR 直接线性递增；WRAP 越界则减去回卷区间大小）。
5. `beat_lower_byte` / `beat_upper_byte` → 由 `beat_addr` 进一步算出这一拍落在 `strobe_width`（总线字节数）内的最低/最高字节下标，供 `wstrb` 生成使用。

`WRAP` 的回卷规则（来自 ARM IHI 0022 的 A3-51）：

\[
\text{Wrap\_Boundary} = \left\lfloor \frac{\text{Start\_Addr}}{\text{Number\_Bytes} \times \text{Burst\_Length}} \right\rfloor \times (\text{Number\_Bytes} \times \text{Burst\_Length})
\]

其中 `Burst_Length = len + 1`，且 `WRAP` 只允许长度 2/4/8/16。

#### 4.1.3 源码精读

**每拍字节数**——一行移位搞定，`shortint`（16 位）足以容纳最大 \(2^7=128\) 字节：

[src/axi_pkg.sv:L115-L118](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L115-L118) — `num_bytes`：返回 \(1 \ll \text{size}\)，即每拍 \(2^{\text{size}}\) 字节，是后续所有地址/字节计算的基石。

**对齐地址**——右移再左移同一位数，等价于把低 `size` 位清零：

[src/axi_pkg.sv:L125-L128](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L125-L128) — `aligned_addr`：`(addr >> size) << size`，清掉低 `size` 位得到对齐基地址。

注意它操作的是 `largest_addr_t`（128 位）而非 `addr_t`。这是刻意设计——一个函数就能服务于任意位宽地址，靠综合器裁掉无用高位：

[src/axi_pkg.sv:L120-L123](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L120-L123) — `largest_addr_t`：刻意定义成 128 位「过宽」地址，使地址类函数对更窄地址通用，多余位交由综合器优化。

**回卷边界**——按 `WRAP` 允许的四种长度（2/4/8/16，对应 `len` 的 1/3/7/15）分别移位，移位量 = `size + log2(长度)`：

[src/axi_pkg.sv:L130-L163](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L130-L163) — `wrap_boundary`：按 `len` 分支，把 `Number_Bytes` 乘以突发的 2/4/8/16 倍来对齐，得到回卷下界；并用 `assume` 在仿真期断言 `len` 合法。

**逐拍地址**——把 INCR 与 WRAP 合在一个函数：先按 INCR 规则算 `ret_addr`，若越出 `[wrap_boundary, wrap_boundary + 区间大小)` 则减回区间大小：

[src/axi_pkg.sv:L165-L196](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L165-L196) — `beat_addr`：第 `i_beat` 拍地址；`BURST_FIXED` 不动地址，其余按 `aligned_addr + i_beat * num_bytes(size)` 推进，WRAP 越界则回卷。

**字节下标**——`beat_addr` 的下游，给写数据生成 `wstrb` 用，`strobe_width` 是总线字节数（如 64 位总线 = 8）：

[src/axi_pkg.sv:L198-L216](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L198-L216) — `beat_lower_byte`/`beat_upper_byte`：由 `beat_addr` 折算出该拍在总线内触及的最低/最高字节下标，第一拍与后续拍规则不同（第一拍受起始地址偏移影响）。

#### 4.1.4 代码实践

**实践目标**：用纸笔（或脚本）验证 `beat_addr` 对一次 `WRAP` 突发的回卷是否符合预期，建立对函数的信任。

**操作步骤**：

1. 设 `addr=0x04, size=2（每拍 4 字节）, len=3（共 4 拍）, burst=BURST_WRAP, strobe_width=8`。
2. 手算 `num_bytes(2)` = \(2^2=4\)。
3. 手算 `aligned_addr(0x04, 2)` = `(0x04 >> 2) << 2` = `0x04`。
4. 手算 `wrap_boundary(0x04, 2, 3)`：`len=3` 走 `4'b11` 分支，移位量 `size+2 = 4`，`(0x04 >> 4) << 4` = `0x00`。回卷区间大小 = `num_bytes * (len+1)` = \(4 \times 4 = 16\)，即地址在 `[0x00, 0x10)` 内回卷。
5. 逐拍套 `beat_addr`：第 0 拍 `0x04`；第 1 拍 `0x04 + 1*4 = 0x08`；第 2 拍 `0x04 + 2*4 = 0x0C`；第 3 拍 `0x04 + 3*4 = 0x10`，但 \(0x10 \ge 0x00 + 16\)，回卷成 `0x10 - 16 = 0x00`。

**需要观察的现象**：四拍地址序列为 `0x04, 0x08, 0x0C, 0x00`——绕回了对齐的最低位，正是 `WRAP` 的典型行为。

**预期结果**：与上述序列一致。这是 cache 行填充类访问的典型地址模式。

> 说明：以上是**手算/阅读型实践**，不修改任何 RTL。若想上机验证，可在任意 testbench 里 `initial` 块中 `$display` 调用 `axi_pkg::beat_addr(...)` 打印结果（属于示例代码，非项目原有调用）。

#### 4.1.5 小练习与答案

**练习 1**：`size=3, len=0`（单拍）时，`num_bytes` 和一次突发总字节各是多少？
**答案**：`num_bytes(3)` = \(2^3 = 8\) 字节；总字节 = `num_bytes * (len+1)` = \(8 \times 1 = 8\) 字节。

**练习 2**：为什么 `wrap_boundary` 的 `case` 只覆盖 `len` = 1/3/7/15 四个值？
**答案**：因为 AXI 规定 `BURST_WRAP` 的长度只能是 2/4/8/16 拍，对应 `len`（拍数−1）= 1/3/7/15。其余 `len` 值在仿真期会被 `assume` 断言报错（见源码 L139–L141）。

---

### 4.2 cache 标志、响应优先级与通道宽度辅助函数

#### 4.2.1 概念说明

除了突发地址，`axi_pkg` 还提供三组「小而常用」的辅助函数：

1. **cache 位解码**：`CACHE_*` 是 4 个可位或的标志位（u2-l1 已讲取值）。`bufferable(cache)` / `modifiable(cache)` 把「某位是否置位」变成单比特布尔值，给下游决定能否改写事务特性。
2. **响应合并优先级**：`resp_precedence(a, b)` 在合并两路响应时给出「该取哪个」。互联里一个事务可能被复制到多个从端，最后要把多个 `resp` 合成一个，必须有确定的优先级。
3. **通道净荷宽度**：`aw_width`/`w_width`/`b_width`/`ar_width`/`r_width` 把一条通道所有字段宽度相加；`req_width`/`rsp_width` 再把五通道连同握手位打包成「请求 struct / 响应 struct」的总宽。它们是 `include/axi/typedef.svh` 宏（见 u2-l4）背后打平端口的算术依据。

#### 4.2.2 核心流程

- `bufferable(cache)`：`|(cache & CACHE_BUFFERABLE)` —— 与掩码相与再归约或，任一匹配位为 1 即真。
- `resp_precedence(a, b)`：以 `DECERR > SLVERR > OKAY > EXOKAY` 为序（注释称之为本库的约定，非协议强制），用 `unique case (resp_a)` 逐对比较返回较高优先级者。
- `aw_width(addr_w, id_w, user_w)` = 把 `id_w + addr_w + LenWidth + SizeWidth + BurstWidth + LockWidth + CacheWidth + ProtWidth + QosWidth + RegionWidth + AtopWidth + user_w` 全加起来（注意 AW 比 AR 多一个 `AtopWidth`）。`w_width` 含数据、strobe（`data_width/8`）、last 各一位；`b_width` 含 id + resp；以此类推。

#### 4.2.3 源码精读

**cache 位解码**——`modifiable` 与 `bufferable` 同构：

[src/axi_pkg.sv:L218-L226](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L218-L226) — `bufferable`/`modifiable`：用 `|(cache & CACHE_xxx)` 把单个 cache 标志位抽成布尔值。

**响应优先级**——逐响应码分支比较，注释详细解释了为何 `OKAY` 优先于 `EXOKAY`（独占访问成功与否）、`DECERR` 为何优先于 `SLVERR`（路由失败早于从端错误）：

[src/axi_pkg.sv:L282-L319](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L282-L319) — `resp_precedence`：返回两响应中优先级更高者；优先级 `DECERR > SLVERR > OKAY > EXOKAY`。

**通道宽度族**——以 `aw_width` 为例，把宽度常量逐项相加（这些常量正是 u2-l1 讲过的 `LenWidth`、`AtopWidth` 等）：

[src/axi_pkg.sv:L321-L327](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L321-L327) — `aw_width`：把 AW 通道所有字段宽度相加，注意它包含 `AtopWidth`，而读地址 `ar_width` 不含。

对照 `ar_width`（少了 `AtopWidth`）：

[src/axi_pkg.sv:L343-L348](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L343-L348) — `ar_width`：与 `aw_width` 公式相同但**没有 `AtopWidth`**——因为原子操作只在写地址上携带，这是读/写地址宽度差别的来源。

`req_width` 把 AW/W/AR 三通道净荷加上各自的 `valid` 位与 `R`/`B` 的 `ready` 位相加，得到整条「请求 struct」宽度：

[src/axi_pkg.sv:L358-L368](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L358-L368) — `req_width`：把三个请求通道净荷与握手位累加成请求 struct 的总宽。

#### 4.2.4 代码实践

**实践目标**：通过阅读理解「AW 比 AR 宽 `AtopWidth` 位」这一结论从何而来。

**操作步骤**：

1. 打开 [src/axi_pkg.sv:L321-L348](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L321-L348)，把 `aw_width` 与 `ar_width` 的求和式逐项对比。
2. 找出唯一只出现在 `aw_width` 里的项（答案：`AtopWidth`，u2-l1 给出其值为 6）。
3. 用 `Grep` 在 `src/` 下搜索 `aw_width` 与 `ar_width` 的调用点，确认它们被用来声明扁平端口宽度。

**预期结果**：两个公式除 `AtopWidth` 外完全相同，所以 AW 通道比 AR 通道多 6 位（即 `atop_t` 的宽度）。这正是 u1-l1 提到的「写地址携带原子操作编码」在位宽上的直接体现。**待本地验证**：具体调用点取决于 `include/axi/port.svh`（u2-l4 详讲）。

#### 4.2.5 小练习与答案

**练习 1**：`resp_precedence(RESP_OKAY, RESP_SLVERR)` 返回什么？为什么？
**答案**：返回 `RESP_SLVERR`。因为优先级 `SLVERR > OKAY`——`SLVERR` 表示事务失败，合并时应优先于表示成功的 `OKAY`（见源码 L294–L301 的 `RESP_OKAY` 分支）。

**练习 2**：给定 `data_width=64, user_width=0`，`w_width` 返回多少？
**答案**：按公式 `data_width + data_width/8 + 1 + user_width` = \(64 + 8 + 1 + 0 = 73\)。其中 8 是 strobe 位（每字节一位），1 是 `last` 位。

---

### 4.3 xbar_latency_e：用 10 个比特位编码延迟模式

#### 4.3.1 概念说明

`axi_xbar` 内部是「demux 阵列 + mux 阵列」（见 u1-l1、u6-l1）。每个 demux 和 mux 的五条通道（AW/W/B/AR/R）都可以选择是否插一级 spill 寄存器来切断组合路径。组合数是 \(2^{(5+5)\times2}\) 种，太多。`axi_pkg` 的做法是：给每个「通道 × 端口侧」分配一个比特位，用一个 10 位向量 `LatencyMode` 一次性表达「在哪里切片」，再用 `enum` 命名几种常用组合。

#### 4.3.2 核心流程

- 定义 10 个 `localparam` 位掩码，每个对应一个「切片点」：5 个 demux 通道位（`DemuxAw/W/B/Ar/R`，位 9–5）+ 5 个 mux 通道位（`MuxAw/W/B/Ar/R`，位 4–0）。
- `xbar_latency_e` 是 `enum bit [9:0]`，成员由这些掩码按位或组合而成（如 `CUT_ALL_AX = DemuxAw | DemuxAr | MuxAw | MuxAr`）。
- 在 `xbar_cfg_t` 里，`LatencyMode` 字段类型是 `bit [9:0]`（而非 enum 本身），所以你既能传一个枚举成员，也能自己按位或拼一个自定义模式。

#### 4.3.3 源码精读

10 个位掩码，每个是一次左移，注意 demux 在高位段、mux 在低位段：

[src/axi_pkg.sv:L449-L469](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L449-L469) — `DemuxAw..DemuxR`、`MuxAw..MuxR`：10 个单热点位掩码，每个对应一个通道切片点。

枚举把常用组合命名出来，`NO_LATENCY` 是全 0（纯组合），`CUT_ALL_PORTS` 是全 1：

[src/axi_pkg.sv:L470-L479](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L470-L479) — `xbar_latency_e`：6 个常用延迟模式，全部由上面的掩码按位或得到；`CUT_ALL_AX` 只切地址通道（AW/AR），是文档推荐配置。

对照文档说明推荐配置与环路约束：

[doc/axi_xbar.md:L59-L65](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_xbar.md#L59-L65) — Pipelining and Latency：推荐 `LatencyMode = CUT_ALL_AX` 且 `FallThrough = 0`；并警告两个互连的 xbar 必须用 `CUT_SLV_PORTS`/`CUT_MST_PORTS`/`CUT_ALL_PORTS` 之一，否则未切通道会形成时序环路。

#### 4.3.4 代码实践

**实践目标**：把 6 个枚举成员「翻译」回 10 位掩码，确认你能读懂每个模式到底切了哪些通道。

**操作步骤**：

1. 对照 [src/axi_pkg.sv:L470-L479](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L470-L479)，把每个成员展开成「DemuxAw | DemuxAr | …」的形式。
2. 列一张表：哪些模式切了地址通道（AW/AR）、哪些切了全部通道、哪些是纯组合。
3. 回答：为什么互连两个 xbar 时，`CUT_ALL_AX` 不可用而 `CUT_SLV_PORTS` 可用？

**预期结果**：

| 成员 | 切片点 | 切地址? | 切全部? |
|------|--------|:---:|:---:|
| `NO_LATENCY` | 无 | ✗ | ✗ |
| `CUT_SLV_AX` | Demux AW/AR | ✓（demux 侧） | ✗ |
| `CUT_MST_AX` | Mux AW/AR | ✓（mux 侧） | ✗ |
| `CUT_ALL_AX` | Demux+Mux 的 AW/AR | ✓ | ✗ |
| `CUT_SLV_PORTS` | Demux 全五通道 | ✓ | 仅 demux 侧 |
| `CUT_MST_PORTS` | Mux 全五通道 | ✓ | 仅 mux 侧 |
| `CUT_ALL_PORTS` | 全部十通道 | ✓ | ✓ |

互连两个 xbar 时，未切的通道会形成跨两个 xbar 的组合环路；`CUT_ALL_AX` 只切地址通道，W/B/R 通道仍可能成环，所以必须用把某侧五通道全切的 `CUT_SLV_PORTS`/`CUT_MST_PORTS`/`CUT_ALL_PORTS`。

#### 4.3.5 小练习与答案

**练习 1**：`CUT_SLV_AX` 与 `CUT_MST_AX` 的区别是什么？
**答案**：前者只在 **demux（slave 侧）** 的 AW/AR 插寄存器，后者只在 **mux（master 侧）** 的 AW/AR 插。两者合起来就是 `CUT_ALL_AX`。

**练习 2**：若你想「只切 W 通道」来缓解写数据关键路径，但不动地址通道，能直接用某个枚举成员吗？
**答案**：不能。现有 6 个枚举成员没有「只切 W」的组合。但因为 `LatencyMode` 字段是 `bit [9:0]`，你可以自定义 `DemuxW | MuxW` 作为模式值（属于示例用法，项目内未直接出现该组合）。

---

### 4.4 xbar_cfg_t 与地址规则结构体

#### 4.4.1 概念说明

`axi_xbar` 有十几个参数。如果把它们写成十几个独立的 `#(...)` 参数，例化时极易写错顺序、漏填。`axi_pkg` 的解法是把它们打包成一个 `struct packed`——`xbar_cfg_t`，例化时只传一个 `Cfg` 参数，字段按名字赋值，清晰且不易错。这体现了本库「组合优于配置」之外的另一原则：**把一组强相关的参数当作一个整体来传递**。

地址映射（address map）是 `axi_xbar` 的核心输入：一组「起始地址–结束地址–目标 master 端口」规则。`axi_pkg` 提供两种预定义规则类型 `xbar_rule_64_t`（64 位地址）和 `xbar_rule_32_t`（32 位地址），省得你自己写 struct。

#### 4.4.2 核心流程

构造一个 xbar 配置的标准三步：

1. **定端口数与并发**：填 `NoSlvPorts`/`NoMstPorts`（注意命名：slave 端口接的是 master 模块，master 端口接的是 slave 模块）、`MaxMstTrans`/`MaxSlvTrans`（每端口在途事务上限）。
2. **定协议宽度**：填 `AxiIdWidthSlvPorts`（slave 端口 ID 宽度）、`AxiIdUsedSlvPorts`（用于判唯一性的低位 ID 位数）、`AxiAddrWidth`、`AxiDataWidth`。master 端口 ID 宽度由内部自动算成 `AxiIdWidthSlvPorts + $clog2(NoSlvPorts)`。
3. **定流水线与映射**：选 `LatencyMode`、填 `PipelineStages`、`FallThrough`、`NoAddrRules`；按需置 `UniqueIds`。

地址规则用对应位宽的 `xbar_rule_*_t` 数组，匹配规则为「`addr >= start_addr && addr < end_addr`」（前闭后开），重叠时高位规则优先。

#### 4.4.3 源码精读

`xbar_cfg_t` 全字段，每个字段都带文档注释：

[src/axi_pkg.sv:L481-L522](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L481-L522) — `xbar_cfg_t`：13 个字段的 `struct packed`，是 `axi_xbar` 的完整配置入口；`LatencyMode` 字段类型是 `bit [9:0]`（见 4.3）。

字段含义与文档字段表逐条对应：

[doc/axi_xbar.md:L42-L55](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_xbar.md#L42-L55) — Configuration 字段表：逐字段定义 `NoSlvPorts`…`NoAddrRules`，并说明 master 端口 ID 宽度须为 `AxiIdWidthSlvPorts + $clog2(NoSlvPorts)`。

两种地址规则类型，仅地址位宽不同：

[src/axi_pkg.sv:L524-L536](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L524-L536) — `xbar_rule_64_t`/`xbar_rule_32_t`：含 `idx`（目标 master 端口索引）、`start_addr`、`end_addr` 三字段，地址位宽分别为 64/32。

一个完整的真实实例（来自 `axi_xbar` 的测试台），是本讲综合实践的范本：

[test/tb_axi_xbar.sv:L66-L80](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_xbar.sv#L66-L80) — 真实 `xbar_cfg_t` 实例：按字段名赋值，`LatencyMode` 取 `axi_pkg::CUT_ALL_AX`，`FallThrough` 取 `1'b0`，正是文档推荐的组合。

#### 4.4.4 代码实践

**实践目标**：为「2 个 slave 端口、3 个 master 端口」的 xbar 写出一个合法的 `xbar_cfg_t` 实例，并选择合适的 `LatencyMode`。

**操作步骤**：

1. 打开 [doc/axi_xbar.md:L42-L55](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_xbar.md#L42-L55) 的字段表，逐行确认每个字段含义。
2. 仿照 [test/tb_axi_xbar.sv:L66-L80](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_xbar.sv#L66-L80) 的写法，按下表填值。
3. 选 `LatencyMode`：单 xbar、目标频率中等时用文档推荐的 `axi_pkg::CUT_ALL_AX`，并把 `FallThrough` 设 `1'b0`。

参考填值（**示例代码**，非项目原有，可直接粘进你自己的 testbench）：

```systemverilog
// 示例代码：2 slave 端口（接 2 个 master 模块）、3 master 端口（接 3 个 slave 模块）
localparam axi_pkg::xbar_cfg_t XbarCfg = '{
  NoSlvPorts:         32'd2,
  NoMstPorts:         32'd3,
  MaxMstTrans:        32'd10,
  MaxSlvTrans:        32'd6,
  FallThrough:        1'b0,
  LatencyMode:        axi_pkg::CUT_ALL_AX,   // 文档推荐：地址通道各插 2 级
  PipelineStages:     32'd0,
  AxiIdWidthSlvPorts: 32'd4,                 // master 端口 ID 宽度自动 = 4 + clog2(2) = 5
  AxiIdUsedSlvPorts:  32'd4,                 // 用满 ID 位宽以避免误冲突
  UniqueIds:          1'b0,
  AxiAddrWidth:       32'd32,                // 对应 xbar_rule_32_t
  AxiDataWidth:       32'd64,
  NoAddrRules:        32'd3                  // 每个 master 端口至少一条规则
};
```

**需要观察的现象**：注意 `NoSlvPorts=2` 与 master 端口 ID 宽度的关系——`$clog2(2)=1`，所以 master 端口 ID 宽度 = `4 + 1 = 5`，比 slave 端口多 1 位（u1-l1 已给的结论）。

**预期结果**：上述 struct 在语义上等价于「2 进 3 出、64 位数据、32 位地址、地址通道切两级 spill」的交叉开关配置。若接到真实 `axi_xbar` 上应能正常综合。

#### 4.4.5 小练习与答案

**练习 1**：上例中若把 `AxiIdWidthSlvPorts` 改为 6，master 端口 ID 宽度变成多少？为什么？
**答案**：变成 \(6 + \lceil\log_2 2\rceil = 6 + 1 = 7\)。多出的 1 位由内部 mux 用来携带源 slave 端口索引，以便把 B/R 响应路由回正确端口（详见 u5-l3 `axi_mux`）。

**练习 2**：为什么 `AxiIdUsedSlvPorts` 通常填成等于 `AxiIdWidthSlvPorts`？填小会怎样？
**答案**：填满可避免「假冲突」——只要低位不同的 ID 就判为不同事务，不误停顿。填小则只看更少的低位，面积更小、延迟更低，但会把高位不同、低位相同的 ID 误判为同一事务而提前 stall（见 [doc/axi_xbar.md:L82-L86](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_xbar.md#L82-L86) 的 Ordering and Stalls）。

---

## 5. 综合实践

把本讲四个模块串起来：**为一个 2×3 的 `axi_xbar` 写出配置，并推演它会在哪里插 spill 寄存器、一次 WRAP 读的字节序列是什么。**

任务步骤：

1. **写配置**：用 4.4.4 的 `XbarCfg` 作为起点，把 `LatencyMode` 从 `CUT_ALL_AX` 改成 `CUT_SLV_PORTS`，并说明这次改动会让哪些通道多出寄存器（答案：demux 侧的 AW/W/B/AR/R 全部）。
2. **拆延迟模式**：把 `CUT_SLV_PORTS` 展开成位掩码（`DemuxAw | DemuxW | DemuxB | DemuxAr | DemuxR`），对照 [src/axi_pkg.sv:L451-L469](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L451-L469) 验证。
3. **算字节**：假设某 master 经该 xbar 发起 `addr=0x0C, size=2, len=3, burst=BURST_WRAP` 的读，用 4.1 的函数手算四拍地址（应得 `0x0C, 0x10→回卷, …`，需自行定回卷边界并验证）。
4. **选规则类型**：因 `AxiAddrWidth=32`，确认地址规则数组应声明为 `axi_pkg::xbar_rule_32_t rule_t [2:0]`（对照 [test/tb_axi_xbar.sv:L84](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_xbar.sv#L84)）。

完成后，你应当能解释：配置 struct（字段）→ 延迟模式（位掩码）→ 通道寄存器（切片点）→ 地址规则（结构体）这条从「配置」到「硬件行为」的完整链路。

## 6. 本讲小结

- `axi_pkg` 的函数区是全库共享的「算术大脑」：`num_bytes`/`aligned_addr`/`wrap_boundary`/`beat_addr`/`beat_lower_byte`/`beat_upper_byte` 把突发的地址与字节范围算清楚，复用于 demux、dw 转换、testbench。
- `bufferable`/`modifiable` 抽取 cache 标志位；`resp_precedence` 以 `DECERR > SLVERR > OKAY > EXOKAY` 合并响应；`aw_width`/…/`req_width`/`rsp_width` 由宽度常量相加得到通道/struct 净荷宽度，且 `aw_width` 比 `ar_width` 多一个 `AtopWidth`。
- `xbar_latency_e` 用 10 个单热点位掩码（`DemuxAw..MuxR`）编码「在哪里插 spill 寄存器」，6 个枚举成员命名常用组合；字段类型是 `bit [9:0]` 故可自定义。
- 文档推荐 `LatencyMode = CUT_ALL_AX` 且 `FallThrough = 0`；两个互连的 xbar 必须用 `CUT_*_PORTS` 之一以避免时序环路。
- `xbar_cfg_t` 把 `axi_xbar` 的十几个参数打包成一个按字段名赋值的 struct；master 端口 ID 宽度自动为 `AxiIdWidthSlvPorts + $clog2(NoSlvPorts)`。
- `xbar_rule_64_t`/`xbar_rule_32_t` 定义地址映射规则（`idx` + 前闭后开区间），匹配规则为高位规则优先。

## 7. 下一步学习建议

- 下一讲 [u2-l3 axi_intf：SystemVerilog 接口](u2-l3-axi-intf.md) 会讲 `AXI_BUS`/`AXI_BUS_DV` 接口与 modport——本讲的 `xbar_cfg_t` 正是配合这些接口使用的，掌握了 struct 再看接口会更顺。
- 随后 [u2-l4 typedef / assign / port 宏体系](u2-l4-typedef-assign-port-macros.md) 会揭示本讲的 `aw_width`/`req_width` 等宽度函数如何在 `port.svh` 宏里被用来生成扁平 AXI 端口，建议对照阅读。
- 想立刻看到 `xbar_cfg_t` 在真实 RTL 中的最终落地，可直接跳到 [src/axi_xbar.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv)（u6-l1 精讲），看它如何把 `Cfg` 的每个字段拆给内部 demux/mux。
