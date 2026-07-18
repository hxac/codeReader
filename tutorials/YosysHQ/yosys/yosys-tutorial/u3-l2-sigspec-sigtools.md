# SigSpec / SigBit / SigChunk 与 sigtools

## 1. 本讲目标

在 [u2-l3](u2-l3-wire-cell-sigspec.md) 里，我们已经认识了网表的三大零件：Wire（线网）、Cell（单元）、SigSpec（信号说明）。本讲要把镜头推近，钻进 SigSpec 的内部结构，并解决一个在写 Pass 时一定会撞上的核心难题：

> 「同一段信号在网表里往往有多种等价写法（整根线、切片、拼接、经 `assign` 改名），我怎么判断两个 SigSpec 是不是其实是同一个信号？」

学完本讲，你应当能够：

1. 说清 `SigSpec` / `SigBit` / `SigChunk` 三者在内存里到底长什么样，以及 `SigSpec` 为什么要做 CHUNK / BITS 双重表示。
2. 掌握 `SigMap`：理解它如何用「并查集（union-find）」把因 `assign` 而等价的信号归并到同一个「规范代表位」，并知道为什么常量位会被优先选作代表。
3. 会用 `kernel/sigtools.h` 提供的 `SigPool`、`SigSet` 等辅助类，完成「收集一组信号」「按位反查驱动/使用它的单元」这类高频分析任务。

本讲是后续所有「需要分析信号连接关系」的 Pass（`opt_expr`、`opt_merge`、`fsm_extract`、`scc` 等）的公共前置知识。

## 2. 前置知识

本讲假设你已经读过 [u2-l3](u2-l3-wire-cell-sigspec.md)，知道：

- 一个 `RTLIL::Module` 拥有若干 `Wire` 和 `Cell`，`Cell` 用「端口名 → SigSpec」的字典表达连接，并不直接持有 `Wire*`。
- `SigSpec` 是描述「一段信号」的通用语言，能涵盖整根线、线切片、常数、以及它们的拼接；它由 `SigChunk`（块）和 `SigBit`（位）组成。
- 位的取值状态有六种：`S0 / S1 / Sx / Sz / Sa / Sm`。

此外，本讲会用到两个 C++ 概念，先用一句话解释：

- **联合体（union）**：让多个字段共享同一块内存，同一时刻只有其中一个「活跃」。`SigSpec` 和 `SigBit` 都用到它来省内存。
- **并查集（union-find / disjoint-set）**：一种维护「若干不相交集合」的数据结构，核心操作是 `find`（找到某元素所在集合的代表）和 `merge`（合并两个集合）。它能在均摊几乎 \(O(1)\) 的时间里判断「这两个东西是不是同一组」。`SigMap` 的归一化能力正建立在它之上。

## 3. 本讲源码地图

本讲涉及的关键源码文件：

| 文件 | 作用 |
| --- | --- |
| `kernel/rtlil.h` | 定义 `RTLIL::State`（位状态枚举）、`SigChunk`、`SigBit`、`SigSpec` 的数据结构与全部成员函数签名。 |
| `kernel/rtlil.cc` | `SigSpec` 部分方法的实现（如 `is_wire`、`known_driver`、`parse` 等）。 |
| `kernel/sigtools.h` | 信号分析工具箱：`SigPool`、`SigSet`、`SigMapView`、`SigMap`、`SigValMap`。本讲的「下半场」全部围绕它。 |
| `kernel/hashlib.h` | `SigMap` 底层依赖的并查集模板 `mfp`（merge-find-promote）。理解它能让你真正看懂 `SigMap`。 |
| `passes/opt/opt_expr.cc` | 真实 Pass 案例：用 `SigMap` + `SigPool` 找出「未被驱动的信号」并改成常量。 |
| `passes/cmds/scc.cc` | 真实 Pass 案例：用 `SigSet<RTLIL::Cell*>` 实现「给定一个单元的输出，找出它驱动的所有下游单元」——这正是本讲的综合实践任务。 |

## 4. 核心概念与源码讲解

### 4.1 SigSpec / SigBit / SigChunk 的两层结构

#### 4.1.1 概念说明

回忆 [u2-l3](u2-l3-wire-cell-sigspec.md)：Cell 的端口值是一个 `SigSpec`，而 `SigSpec` 内部是「块」和「位」两个层次。本节我们打开这三个结构体，看清它们的字段，并解释 `SigSpec` 内部那个精巧的「双重表示」设计。

为什么要分三层？

- **`SigBit` 是最小单位**：一根线上的某一位，或一个常量位（`S0/S1/...`）。它是做集合、做哈希的最小粒度（`SigPool`、`SigMap` 都以位为单位工作）。
- **`SigChunk` 是「连续的一段」**：要么是某根 wire 上 `[offset, offset+width)` 这一段连续位，要么是一串连续常量位。它用 `(wire, offset, width)` 或 `(data, width)` 就能描述，紧凑高效。
- **`SigSpec` 是「一串 chunk」**：把若干 chunk 串起来，就能表达任意拼接信号，例如 `{a[7:4], 4'b1010, b}`。

层级关系可以写成：

\[
\text{SigSpec} = [\ \text{SigChunk}_1,\ \text{SigChunk}_2,\ \ldots\ ],\qquad
\text{SigChunk} = \text{wire 上的一段} \ |\ \text{一段常量},\qquad
\text{SigBit} = \text{展开后的单 bit}
\]

一个 SigSpec 既可以从 chunk 角度看（「由哪些连续段组成」），也可以展开成 bit 角度看（「从低位到高位逐位是什么」）。`SigSpec` 的内部表示正是为了同时高效支持这两种视角。

#### 4.1.2 核心流程

- 位状态：`RTLIL::State` 用一个字节表示一位的值，`S0/S1` 是确定逻辑值，`Sx` 是未定义/冲突，`Sz` 是高阻，`Sa` 是 don't-care，`Sm` 是内部标记位。
- `SigChunk` 字段：`wire`（非空表示是线，空表示是常量）、`data`（仅当 `wire==NULL` 时存放常量位向量，LSB 在 index 0）、`width`、`offset`。
- `SigBit` 字段：`wire` 加一个 `union { State data; int offset; }`——当 `wire==NULL` 时 union 存的是 `State`（常量值），当 `wire!=NULL` 时存的是 `offset`（在线上的下标）。用 union 省掉一个字段。
- `SigSpec` 双重表示：内部用一个 `union { SigChunk chunk_; vector<SigBit> bits_; }` 加一个 `rep_` 标记，记录当前活跃的是 `CHUNK` 还是 `BITS`。绝大多数 SigSpec 要么是「单个 chunk」（很常见，比如一整根 wire），要么是「逐位展开」。很多操作（`size()`、`operator[]` 读）都能在两种表示上直接跑；少数操作（逐位改写、排序）需要先 `unpack()` 成 BITS 形式。
- 哈希惰性缓存：`SigSpec` 带一个 `AtomicHash hash_`，首次需要哈希时才计算并缓存，内容被修改时清空。这让 `SigSpec` 可以放进 `hashlib` 的 `pool` / `dict` 里做 O(1) 查找。

#### 4.1.3 源码精读

**位状态枚举**，注意 `Sx` 同时表示「未定义值」与「多重驱动冲突」——这与下一节 `SigMap` 检测冲突有关：

[State 枚举 — kernel/rtlil.h:33-40](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L33-L40)

这段定义了位的六种取值，是 `SigBit`/`SigChunk` 描述常量位的基础。

**`SigChunk` 结构体**，注意它要么是「一段 wire」要么是「一段常量」，靠 `wire` 是否为空区分：

[SigChunk 结构体 — kernel/rtlil.h:1301-1326](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L1301-L1326)

字段含义：`wire` 指向所属线（常量时为空）、`data` 仅常量时用、`width` 是段宽、`offset` 是在线上的起始下标（LSB 为 0）。一组重载的构造函数让你能从 `Const`、`Wire*`、`Wire* + offset + width`、单个 `SigBit` 等方便地构造一个 chunk。

**`SigBit` 结构体**，注意 `wire` 与 `union{data, offset}` 的二选一关系：

[SigBit 结构体 — kernel/rtlil.h:1328-1354](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L1328-L1354)

当 `wire != NULL`，union 里存的是 `offset`（这一位在 wire 上的下标）；当 `wire == NULL`，存的是 `State data`（常量值）。`is_wire()` 是判别用的便捷方法。正因为 `SigBit` 如此紧凑且可哈希，它才适合做信号分析的「最小硬币」。

**`SigSpec` 的双重表示**，看私有字段的 union 与 `rep_` 标记：

[SigSpec 私有表示 — kernel/rtlil.h:1446-1471](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L1446-L1471)

这里定义了 `rep_`（CHUNK 或 BITS）、惰性哈希 `hash_`、以及 `union { SigChunk chunk_; vector<SigBit> bits_; }`。`init_empty_bits()` 把空 SigSpec 初始化成空的 BITS 形式；`inline_unpack()` 在需要逐位访问前，把 CHUNK 形式「拆」成 BITS 形式。注意析构 `destroy()` 必须按 `rep_` 调用对应成员的析构函数——这是用 union 必须承担的责任。

**`size()` 与 `operator[]`**，展示两种表示都能直接服务读取操作：

[size 与 operator[] — kernel/rtlil.h:1616-1629](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L1616-L1629)

`size()` 在 CHUNK 形式下返回 `chunk_.width`，在 BITS 形式下返回向量长度——两种表示共用同一个 `size()`。常量版 `operator[]` 也能在 CHUNK 形式下直接算出第 `index` 位，无需展开，是典型的「快路径」。

**`SigSpec` 的能力分类一览**（查询 / 变换 / 分析 / 转换 / 解析）：

[SigSpec 主要公共方法 — kernel/rtlil.h:1637-1752](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L1637-L1752)

按用途归类，写 Pass 时最常用的是：

- 查询：`size()`、`empty()`、`is_wire()`、`is_chunk()`、`is_bit()`、`lsb()/msb()/front()/back()`。
- 迭代：`begin()/end()`（逐 `SigBit`）、以及 `chunks()` 视图（逐 `SigChunk`）。
- 变换：`append()`、`replace(pattern, with)`、`remove(pattern)`、`extract(offset, length)`、`extend_u0()`、`reverse()`。
- 分析：`is_fully_const()`、`is_fully_zero()/ones/def/undef()`、`as_bool()/as_int()/as_const()`、`known_driver()`。
- 转换：`as_wire()`、`as_chunk()`、`as_bit()`、`to_sigbit_set()/pool()/vector()/map()/dict()`。
- 解析：静态方法 `parse()` / `parse_sel()` / `parse_rhs()`——把字符串（脚本里写的信号表达式）解析成 `SigSpec`。

> 关于 `chunks()` 视图：旧版 `SigSpec` 内部曾直接存一个 `vector<SigChunk> chunks_`，现在改为「按需重建一个 chunk」的只读视图（见 `struct Chunks`，约 [kernel/rtlil.h:1529-1612](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L1529-L1612)）。这是为了配合上面的双重表示——你仍可以 `for (auto &c : sigspec.chunks())` 遍历，但底层已不再常驻 chunk 向量。

#### 4.1.4 代码实践

**实践目标**：用 `write_rtlil` 的文本输出，亲手验证「一个 SigSpec 在文本里就是 chunk 的拼接」，并对应到 `SigChunk` 的字段。

**操作步骤**：

1. 准备一个小 Verilog 文件 `sig_demo.v`（示例代码）：

   ```verilog
   module sig_demo(input [7:0] a, input [3:0] b, output [11:0] y);
       assign y = {a, b};          // 整根拼接
       wire [3:0] hi = a[7:4];     // wire 切片
       wire [3:0] lo = a[3:0];
       wire [1:0] m = b[1:0];
   endmodule
   ```

2. 运行 `yosys` 并执行：

   ```
   yosys> read_verilog sig_demo.v
   yosys> write_rtlil sig_demo.rtlil
   ```

3. 打开 `sig_demo.rtlil`，找到 `cell` 与 `connect` 行。

**需要观察的现象**：

- `connect \y { \a [7:0] \b [3:0] }` 这样的写法，正是「一个 SigSpec = 多个 SigChunk 串联」的文本形式：`\a [7:0]` 是一个 wire chunk，`\b [3:0]` 是另一个 wire chunk。
- 切片 `a[7:4]` 在文本里写成 `\a [7:4]`，对应 `SigChunk{ wire=a, offset=4, width=4 }`（注意 RTLIL 规定 LSB 为 0，所以 `a[7:4]` 的 `offset` 是 4）。

**预期结果**：你能在 RTLIL 文本里一一指认出 `wire`/`offset`/`width`，把抽象的 `SigChunk` 字段落到具体文本上。

#### 4.1.5 小练习与答案

**练习 1**：`SigBit` 为什么用 `union { State data; int offset; }`，而不是直接并存两个字段？

**参考答案**：因为 `wire == NULL` 时这一位是常量，只需要 `State`（一个字节）；`wire != NULL` 时这一位是线上的某位，只需要 `offset`（一个 int）。二者互斥，用 union 共用内存可省一个字段，且 `is_wire()` 一次判断即可区分。这是「同一时刻只有一种活跃」的典型 union 用法。

**练习 2**：`SigSpec` 的 CHUNK 与 BITS 双重表示，分别适合什么场景？

**参考答案**：CHUNK 表示（单个 `SigChunk`）适合「整根 wire / 单个连续段」这种极常见情况，省内存、`size()` 与 `operator[]` 都是 O(1)；BITS 表示（`vector<SigBit>`）适合需要逐位随机改写、排序、去重的场景。很多只读操作两种表示都能跑，写操作前会调用 `unpack()`/`inline_unpack()` 转成 BITS。

**练习 3**：`is_fully_const()` 与 `has_const()` 有何区别？

**参考答案**：`is_fully_const()` 当且仅当 SigSpec 的**每一位**都是常量位（`wire==NULL`）才返回真；`has_const()` 只要其中有**任意一位**是常量就返回真。前者描述「整段都是常数」，后者描述「夹带了常数位」。

---

### 4.2 SigMap：信号归一化与多重驱动

#### 4.2.1 概念说明

这是本讲最重要、也是写 Pass 时最常踩坑的概念。

问题从何而来？考虑这样的网表片段：

```
wire [3:0] a;
wire [3:0] alias_of_a;
assign alias_of_a = a;          // 模块级 assign，存在 Module::connections_
```

现在某个 Cell 的输出端口接的是 `a`，而另一个 Cell 的输入端口接的是 `alias_of_a`。从「电学」上讲，它们是同一组 4 根线；但在 RTLIL 数据结构里，两个 `SigSpec` 长得不一样（一个引用 `a`，一个引用 `alias_of_a`）。如果你直接用 `==` 比较位、或用 `SigBit` 作哈希键，会把「其实是同一位」的两个 SigBit 当成不同键，分析就错了。

`SigMap` 就是解决这个问题的「翻译器」：它把模块里因 `assign` 而彼此相连的位，**归并到同一个「规范代表位（canonical representative）」**。于是无论你拿到的是 `a[2]` 还是 `alias_of_a[2]`，经过 `sigmap(...)` 之后都会得到同一个 `SigBit`。这样一来：

- 用归一化后的 `SigBit` 作哈希键，等价位就不会重复登记。
- 如果某位被 `assign` 到常量（如 `assign x = 1'b0;`），归一化后它的代表位就是那个常量位——Pass 据此就能发现「这个信号其实恒为 0」。

一句话：**`SigMap` 把「网表里对同一信号的不同书写」折叠成唯一代表**，让信号分析变得可靠。

#### 4.2.2 核心流程

`SigMap` 的归一化能力来自一个并查集 `mfp<SigBit>`（merge-find-promote）。整体流程：

1. **构造**：`SigMap sigmap(module);` 时，调用 `set(module)`，遍历该模块的全部模块级连接 `module->connections()`（即所有 `assign` 形如 `lhs = rhs`），对每一对 `(lhs[i], rhs[i])` 调用 `add(lhs, rhs)`。
2. **建立等价类**：`add` 把 `from[i]` 与 `to[i]` 在并查集里 `imerge`（合并到同一集合）。并查集让「同一集合」的所有位共享一个代表。
3. **常量优先为代表**：如果合并的某一方是常量位（`wire==NULL`），就 `ipromote` 把它**提升**为该集合的代表。语义上即「常量驱动优先」——这是 `opt_expr` 等优化能识别常量驱动的前提。
4. **查询**：调用 `sigmap(bit)` 或 `sigmap(sigspec)`（其实是 `SigMapView::apply`），对每个位做一次 `find`，返回其规范代表位。
5. **更新**：如果 Pass 在分析过程中又新增了 `assign`（`module->connect(...)`），需要重新 `sigmap.set(module)`（或新建一个 `SigMap`），因为并查集是构造时刻连接关系的快照。

并查集的均摊复杂度接近 \(O(\alpha(n))\)（其中 \(\alpha\) 是反阿克曼函数，对任何实际规模都可视为常数），所以 `sigmap` 逐位查询非常快。

#### 4.2.3 源码精读

**`SigMap` 的整体注释与构造**，文档直白说明了它「把相连的 SigBit 映射到规范代表，常量位会被提升为代表」：

[SigMap 类与构造 — kernel/sigtools.h:276-289](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/sigtools.h#L276-L289)

注意 `SigMap final : public SigMapView`，它复用基类 `SigMapView` 提供的 `apply` / `operator()` 查询接口，自己只负责「建设并查集」。

**`set(module)`：从模块连接重建并查集**：

[SigMap::set — kernel/sigtools.h:302-313](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/sigtools.h#L302-L313)

这里先按连接总位数 `reserve` 容量（避免反复扩容），再逐条 `add(it.first, it.second)`。`it.first`/`it.second` 是 `SigSig`（`pair<SigSpec, SigSpec>`）的左右两端——也就是每条 `assign` 的 lhs 和 rhs。注意它**只看模块级 `connections()`，不看 cell 端口连接**；cell 端口的信号经 `sigmap` 后会被映射到由这些 assign 决定的代表位。

**`add(from, to)`：逐位合并并处理常量提升**：

[SigMap::add — kernel/sigtools.h:316-339](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/sigtools.h#L316-L339)

逐位地：先 `lookup` 出 `from[i]`、`to[i]` 各自的当前代表下标 `bfi`、`bti`，只要其中至少一方是「真信号位」（`bf.wire || bt.wire`，过滤掉两个都是常量的无意义合并），就 `imerge` 合并；若某一侧是常量（`wire==nullptr`），则 `ipromote` 把它提升为代表。这正是「常量优先」的实现。

**查询接口 `SigMapView::apply` / `operator()`**：

[SigMapView — kernel/sigtools.h:240-274](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/sigtools.h#L240-L274)

`apply(bit)` 把 `bit` 替换成 `database.find(bit)`（其规范代表）；`apply(sigspec)` 逐位 apply；`operator()(bit)` / `operator()(sigspec)` / `operator()(wire)` 则是「不修改入参、返回归一化副本」的便捷形式。这就是 Pass 里到处可见的 `sigmap(cell->getPort(ID::A))` 写法。

**底层并查集 `mfp`**：理解 `imerge`/`ipromote`/`find` 的语义，`SigMap` 就彻底透明了：

[mfp 类与 ifind/imerge/ipromote — kernel/hashlib.h:1363-1470](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/hashlib.h#L1363-L1470)

要点：

- `ifind(i)`：沿 `parents` 链找到集合根（代表），并顺手做路径压缩，加速后续查询。注释还专门论证了它在并发 `ifind` 调用下的正确性。
- `imerge(i, j)`：让 `ifind(j)` 成为合并后集合的根。
- `ipromote(i)`：把节点 `i` 提升为新根，把它原先所在集合的所有节点都改挂到 `i` 下——这正是「常量位被选为代表」的实现。
- `find(a)`：返回元素 `a` 所在集合的代表元素本身（不是下标），供 `SigMapView::apply` 直接替换。

#### 4.2.4 代码实践

**实践目标**：阅读真实 Pass `opt_expr` 的 `replace_undriven`，理解「为什么每个信号在进 `SigPool` 之前都要先过一遍 `sigmap`」。

**操作步骤**：

1. 打开 [passes/opt/opt_expr.cc:35-67](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt_expr.cc#L35-L67)。
2. 关注这三行模式：

   ```cpp
   SigMap sigmap(module);                 // 建立该模块的归一化器
   SigPool driven_signals, used_signals;  // 用「位」为单位的集合
   ...
   driven_signals.add(sigmap(conn.second));  // 先归一化，再加入集合
   ```

3. 追问自己：如果把 `sigmap(conn.second)` 换成直接 `conn.second`，会出什么错？

**需要观察的现象**：

- 每一处 `add` / `extract` / `check` 之前，信号都被 `sigmap(...)` 包了一层。
- 该函数的目标是「找出无人驱动的信号，改成 `Sx`」。判断「是否被驱动」靠的是 `SigPool` 的集合运算（`all_signals.del(driven_signals)` 再 `export_all()`）。这些集合运算的正确性，完全依赖「等价位被折叠成同一个键」，也就是依赖 `sigmap`。

**预期结果**：你能解释「`sigmap` 是 SigPool/SigSet 这类按位集合工具能正确工作的前提条件」——没有归一化，同一信号的不同别名会被当成不同位，集合差集就算错。

> 待本地验证：可尝试构造一个含 `assign alias = orig;` 的小设计，用 `yosys -g` 调试或在自己写的 Pass 里打印 `sigmap` 前后的 `SigBit`，亲眼看 `orig[0]` 与 `alias[0]` 被映射到同一个代表位。

#### 4.2.5 小练习与答案

**练习 1**：`SigMap` 是依据什么建立信号等价关系的？cell 的端口连接关系会被它自动纳入吗？

**参考答案**：`SigMap` 只依据**模块级连接** `module->connections()`（即 `assign` 语句）建立等价类。它**不会**自动把 cell 端口连接纳入——cell 端口信号是在你调用 `sigmap(port_sig)` 时被「翻译」到由 assign 决定的代表位。所以如果一个信号只通过 cell 端口相连、没有任何 `assign`，`SigMap` 不会把它们合并。

**练习 2**：为什么 `add` 里遇到常量位要做 `ipromote`？

**参考答案**：因为常量是「确定的驱动值」。把常量位提升为集合代表，意味着这组相连信号归一化后都指向那个常量——Pass 就能据此判断「这段信号其实是常量 0/1」，这是常量传播/死代码消除的关键依据。如果反过来让某个普通 wire 当代表，常量信息就被「藏」起来了。

**练习 3**：在分析过程中，如果一个 Pass 调用了 `module->connect(lhs, rhs)` 新增了一条 assign，原来的 `SigMap` 还能用吗？

**参考答案**：不能直接用了。`SigMap` 是构造时连接关系的快照，新增 assign 后并查集已过时。需要重新 `sigmap.set(module)`，或新建一个 `SigMap`（`opt_expr.cc` 里就出现了 `SigMap sm2(module);` 这种「重建」写法）。

---

### 4.3 sigtools 辅助类：SigPool 与 SigSet

#### 4.3.1 概念说明

有了 `SigMap` 把信号折叠成规范位，接下来要做的往往是两类高频操作：

1. **维护「一组信号位」**：这些位被驱动了 / 被使用了 / 被选中了。需要快速「加入、删除、判存在、求差集」。这就是 `SigPool`。
2. **维护「位 → 若干附加数据」的反查表**：例如「每一位被哪些 cell 当作输入使用」「每一位被哪个 cell 驱动」。给定一个信号，反查出关联的 cell 集合。这就是 `SigSet<T>`。

它们都以 `SigBit`（通常是 `sigmap` 归一化后的位）为键，配合 `SigMap` 一起使用，是写分析型 Pass 的「标准积木」。

> 关于命名：`SigPool` 用 `pool`（yosys 自研哈希集合，见 [u3-l3](u3-l3-idstring-const-hashlib.md) 会讲到的 hashlib），`SigSet<T>` 用 `dict<bitDef_t, std::set<T>>`——即「每个位对应一个 T 的有序集合」。

#### 4.3.2 核心流程

**`SigPool`（位集合）**：

- 内部就是 `pool<bitDef_t> bits;`，其中 `bitDef_t` 是 `pair<Wire*, int>`（wire 指针 + 位偏移），自带哈希。
- `add(sig)` / `del(sig)`：逐位加入或删除（自动忽略常量位 `bit.wire==NULL`）。
- `add(other)` / `del(other)`：集合并/差。
- `check(bit)` / `check_any(sig)` / `check_all(sig)`：判单个、任一、全部存在。
- `extract(sig)` / `remove(sig)`：从 `sig` 中取出在池中的位 / 不在池中的位，返回新 SigSpec。
- `export_all()`：把池里所有位导出成一个 SigSpec。

**`SigSet<T>`（位 → T 集合 反查表）**：

- 内部是 `dict<bitDef_t, std::set<T>> bits;`。
- `insert(sig, data)`：把 `data` 关联到 `sig` 的每一位。
- `erase(sig, data)`：移除关联。
- `find(sig)` / `find(sig, result)`：返回「与 `sig` 任意一位相关联的全部 T」的集合。

**典型用法模式**（找下游 cell）：建一张 `SigSet<Cell*> sig2user`，遍历所有 cell，对每个 cell 的每个**输入**端口做 `sig2user.insert(sigmap(input_sig), cell)`。之后想查「某个 cell 的输出驱动了哪些下游 cell」，只需对该 cell 的**输出**信号调用 `sig2user.find(sigmap(output_sig))`，拿到的就是所有「把这些信号当输入」的 cell——即下游 cell。这正是下一节综合实践要复刻的 `scc` 套路。

#### 4.3.3 源码精读

**`SigPool` 的核心方法**：

[SigPool 类 — kernel/sigtools.h:27-140](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/sigtools.h#L27-L140)

注意几个细节：`bitDef_t` 用 `pair<Wire*, int>` 而不是直接用 `SigBit`（设计选择，便于稳定哈希）；所有 `add/del/check` 都先判 `bit.wire != NULL` 自动跳过常量位；`extract`/`remove` 是构造「池内/池外」子 SigSpec 的利器。

**`SigSet<T>` 的核心方法**：

[SigSet 类 — kernel/sigtools.h:142-231](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/sigtools.h#L142-L231)

`insert(sig, data)` 把 `data` 登记到 `sig` 的每一位；`find(sig, result)` 把「与 sig 任一位相关的所有 T」并集进 `result`。这就是「按信号反查关联对象」的通用工具。

**真实案例：`scc`（强连通分量）用 `SigSet<Cell*>` 找下游 cell**：

[scc.cc 构建位→cell 反查表 — passes/cmds/scc.cc:127-195](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/cmds/scc.cc#L127-L195)

这段代码（结合 `NewCellTypes ct` 判定端口方向）为每个 cell 收集归一化后的 `inputSignals` 与 `outputSignals`，然后：

- `sigToNextCells.insert(inputSignals, cell);`（[第 194 行](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/cmds/scc.cc#L194)）——把「cell 读哪些输入位」登记成反查表。
- `sigToNextCells.find(cellToNextSig[cell], cellToNextCell[cell]);`（[第 199 行](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/cmds/scc.cc#L199)）——对 cell 的**输出**位反查「谁把它们当输入」，得到下游 cell 集合。

这正是本讲综合实践要复刻的算法。

**另一个常见工具 `SigValMap<Val>`**（顺带了解）：它和 `SigMap` 一样做归一化，但每个等价类还附带一个可「累加」（`operator|=`）的值 `Val`。例如可以给每个信号位挂上「驱动它的 cell」信息。它同样继承 `SigMapView`，因此查询接口与 `SigMap` 一致：

[SigValMap 类 — kernel/sigtools.h:368-467](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/sigtools.h#L368-L467)

#### 4.3.4 代码实践

**实践目标**：用 `SigPool` 模拟 `opt_expr::replace_undriven` 的核心思路——区分「被驱动」与「被使用」的信号集合。

**操作步骤**（源码阅读型实践）：

1. 阅读 [passes/opt/opt_expr.cc:35-94](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt_expr.cc#L35-L94)。
2. 在纸上画出三个 `SigPool`（`driven_signals`、`used_signals`、`all_signals`）的数据流向：
   - 遍历所有 cell 端口：输出端口位进 `driven_signals`，输入端口位进 `used_signals`。
   - 遍历所有 wire：`port_input` 进 `driven_signals`，`port_output`/`keep` 进 `used_signals`，全部进 `all_signals`。
   - `all_signals.del(driven_signals)` → 得到「完全无人驱动」的信号。
3. 对照 `SigPool` 的 `add` / `del` / `extract` / `export_all` 方法，确认每一步用了哪个 API。

**需要观察的现象**：

- 所有信号进池子前都过了 `sigmap`，确保别名被折叠。
- 集合差 `all_signals.del(driven_signals)` 直接给出「undriven」集合，再把它们 `module->connect(sig, Sx)` 改成未定义。

**预期结果**：你能复述「`SigPool` + `SigMap` 如何用三五行集合运算完成『找未驱动信号』这一原本需要手写图遍历的任务」。

#### 4.3.5 小练习与答案

**练习 1**：`SigPool` 和 `SigSet<T>` 的本质区别是什么？

**参考答案**：`SigPool` 只是一个「位的集合」，回答「某位在不在里面」；`SigSet<T>` 是「位 → T 集合」的映射，回答「与某些位相关联的 T 都有哪些」。前者用于「圈定一组信号」，后者用于「按信号反查关联对象（如 cell）」。

**练习 2**：`SigPool::add` 为什么对 `bit.wire == NULL` 的位直接跳过？

**参考答案**：常量位（`wire==NULL`）没有「物理线」身份，不属于任何具体的可驱动/可使用信号；而且常量位往往数量巨大（如宽常量），把它们登记进集合既无意义又浪费。所以 `add/del/check` 系列都只处理真信号位。

**练习 3**：如果不用 `sigmap` 归一化就直接把 cell 端口位塞进 `SigSet<Cell*>`，反查下游 cell 的结果会怎样？

**参考答案**：会把同一信号的不同别名当成不同位。于是一个 cell 经 `assign alias = orig;` 用 `alias` 作输入、另一个用 `orig` 作输入时，它们的位不被合并，反查表里就建不起关联，`find` 会漏掉本应被认作「下游」的 cell。归一化是保证反查完整性的前提。

---

## 5. 综合实践：给定一个 cell 输出，找出它驱动的所有下游 cell

本任务把本讲三块内容串起来：用 `SigSpec` 读取端口信号，用 `SigMap` 归一化，用 `SigSet<Cell*>` 建反查表，最终回答「这个 cell 的输出驱动了哪些下游 cell」。

### 5.1 实践目标

实现一个分析算法（描述清楚即可，鼓励写成自定义 Pass），输入：一个 `RTLIL::Module *module` 和一个 `RTLIL::Cell *target`；输出：所有「把 `target` 任一输出位当作输入」的 cell 集合。

### 5.2 算法步骤（伪代码）

```cpp
// 示例代码：仅说明算法思路，未编译
std::set<RTLIL::Cell*> find_downstream(RTLIL::Module *module,
                                       RTLIL::Cell *target)
{
    SigMap sigmap(module);                      // (1) 建立归一化器
    SigSet<RTLIL::Cell*> sig2user;              // (2) 位 -> 使用它的 cell

    // 需要一个端口方向判定器，例如 NewCellTypes（见 u3-l4）
    NewCellTypes ct; ct.setup_internals(); ct.setup_stdcells();

    // (3) 建反查表：遍历每个 cell 的输入端口，登记到 sig2user
    for (auto cell : module->cells()) {
        for (auto &conn : cell->connections()) {
            if (ct.cell_input(cell->type, conn.first)) {
                sig2user.insert(sigmap(conn.second), cell);  // 归一化后登记
            }
        }
    }

    // (4) 查询：收集 target 的全部输出位，归一化后反查
    RTLIL::SigSpec out_sig;
    for (auto &conn : target->connections()) {
        if (ct.cell_output(target->type, conn.first)) {
            out_sig.append(sigmap(conn.second));
    }

    std::set<RTLIL::Cell*> downstream;
    sig2user.find(out_sig, downstream);         // (5) 反查下游
    return downstream;
}
```

### 5.3 操作步骤

1. **理解数据来源**：注意 `sig2user.insert` 用的是**输入**端口，而最终 `find` 用的是 `target` 的**输出**端口——这一「输入建表、输出查询」的对称设计，正好把「输出 → 谁把它当输入」接起来。可对照 [passes/cmds/scc.cc:194-199](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/cmds/scc.cc#L194-L199) 验证。
2. **端口方向**：判定一个端口是输入还是输出，不能只看连接字典，需要单元类型信息。`scc.cc` 用 `NewCellTypes ct` 的 `cell_input` / `cell_output`（关于 `NewCellTypes`，详见 [u3-l4](u3-l4-internal-cell-library.md)）。对未知类型的黑盒，可退化为「全部当作既输入又输出」，或参考 `scc.cc` 的 `isInput/isOutput` 兜底逻辑。
3. **对照 Module::connections_**：题目提示可对照 `RTLIL::Module::connections()`（[kernel/rtlil.h:2105-2108](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L2105-L2108)）理解——那是模块级 `assign` 的存储，是 `SigMap` 建表的依据；而本任务的 cell 连接关系来自 `cell->connections()`（每个 cell 的端口连接字典）。两者不要混淆。

### 5.4 需要观察的现象

- 若 `module` 里存在 `assign alias = target_out;`，则读 `alias` 的 cell 也应出现在 `downstream` 中——这正是 `sigmap` 折叠别名的功劳。去掉 `sigmap` 后这类下游会丢失。
- 若 `target` 的输出被接到一个常量驱动的线（`assign x = 1'b0;`），归一化后该位变成常量，`SigSet` 会因常量位被跳过而不登记——这是符合预期的（常量位无下游可寻）。

### 5.5 预期结果

> 待本地验证（需将其实现为可加载 Pass 或在 `examples/cxx-api` 风格的小程序中调用）：对一个含 `a -> b -> c` 三级组合逻辑的小设计，对 `a` 调用 `find_downstream` 应得到 `{b, c}` 中所有直接读 `a` 输出的 cell（即直接下游，不是传递闭包；要传递闭包需自行 BFS，可参考 `scc` 的 `workQueue` 推进方式）。

## 6. 本讲小结

- `SigSpec` / `SigChunk` / `SigBit` 是三级结构：`SigSpec` = 一串 `SigChunk` = 展开后的 `SigBit`；`SigSpec` 内部用 `CHUNK`/`BITS` 双重表示 + 惰性哈希，兼顾「单段信号」的省内存与「逐位操作」的灵活性。
- `SigBit` 用 `union { State data; int offset; }` 区分常量位与线位；`SigChunk` 用 `wire/offset/width` 描述连续段。
- **`SigMap` 是本讲的核心**：它用并查集 `mfp<SigBit>` 把模块 `assign` 连通的位归并到唯一「规范代表位」，并把常量位提升为代表——这让「同一信号的不同书写」可被等价对待，是所有按位分析的前提。
- `sigtools` 的 `SigPool`（位集合）与 `SigSet<T>`（位→T 反查表）配合 `SigMap`，能用几行集合运算完成「找未驱动信号」「找下游 cell」等高频任务。
- 写 Pass 的黄金模式：`SigMap sigmap(module);` 之后，凡是要进 `pool`/`dict`/`SigSet` 的位，都先 `sigmap(...)` 归一化。
- `SigMap` 是连接关系的一个**快照**；若 Pass 新增了 `module->connect(...)`，必须重建 `SigMap`。

## 7. 下一步学习建议

- 下一讲 [u3-l3 IdString、Const 与 hashlib](u3-l3-idstring-const-hashlib.md) 会补齐「命名系统」与「容器底层」：你会看到本讲反复用到的 `pool`、`dict`、`idict`、`mfp` 全都来自 `hashlib`，理解它们能让 `SigMap`、`SigPool` 的实现细节彻底通透。
- 之后 [u3-l4 内部单元库](u3-l4-internal-cell-library.md) 会讲清 `NewCellTypes` / `celltypes.h`——这是本任务里判定端口方向所依赖的工具。
- 想看 `SigMap`+`SigSet` 的更多实战，推荐直接读 `passes/opt/opt_expr.cc`、`passes/opt/opt_merge.cc`、`passes/fsm/fsm_extract.cc`，它们是本讲套路的「教科书级」范本。
