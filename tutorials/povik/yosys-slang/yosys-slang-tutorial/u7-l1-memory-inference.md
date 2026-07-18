# 存储器推断与初始化

## 1. 本讲目标

本讲解决一个问题：sv-elab 在翻译 SystemVerilog 时，凭什么决定把一个数组「当成存储器（memory）」来翻译，而不是当成一堆普通触发器？以及存储器的内容初始化（赋值模式、`$readmemh`/`$readmemb`）又是怎样落到 RTLIL 上的。

学完后你应当能够：

- 说清 `InferredMemoryDetector` 的「先收候选、再淘汰」两遍扫描算法，以及哪些用法会让一个数组「失去」存储器资格。
- 理解 `ram_style`/`rom_style` 等用户提示属性的作用，以及 `--no-implicit-memories` 开关如何收紧推断。
- 掌握存储器声明、读端口（`$memrd_v2`）、写端口（`$memwr_v2`）的发射位置。
- 了解 `$readmemh`/`$readmemb` 如何在编译期把数据文件加载成 `$meminit_v2` 初始化单元，以及 `add_memory_init` 如何处理位对齐与大/小端。

本讲依赖 u6-l2（触发器发射与异步复位），因为你需要知道「一个 `always @(posedge clk)` 块如何被识别为 ff 过程」之后，才能理解为何只有该同步体里的非阻塞写才会被当作存储器写。

## 2. 前置知识

**SystemVerilog 里的「数组」与「存储器」**。在 SV 中，`reg [7:0] x[0:255];` 声明了一个「非压缩数组（unpacked array）」——256 个 8 位的字。综合工具可以选择两种实现：一是把它展平成 256×8=2048 个独立的触发器，二是把它推断成一块「存储器」（RAM/ROM 原语）。后者通常面积更小、更贴近真实硬件（BRAM/SRAM）。

**RTLIL 的存储器模型**。Yosys 的 RTLIL 不直接有「RAM 单元」，而是用一组字级单元协作表达一块存储器：

- `RTLIL::Memory`：一块存储器的「声明」，含字宽 `width`、字数 `size`、起始下标 `start_offset`。
- `$memrd_v2`：一个读端口（给地址、出数据）。
- `$memwr_v2`：一个写端口（给地址、数据、使能）。
- `$meminit_v2`：一段初始化数据（上电时把某些字写成常量）。

后续 Yosys 的 `memory_collect` pass 会把这些零散的端口「收拢」成单个 `$mem_v2`，再由 `memory_map` 映射成具体工艺的 BRAM/寄存器。**sv-elab 只负责发出字级的 `$memrd_v2`/`$memwr_v2`/`$meminit_v2`，不负责收拢与映射**。

**「HDL 意图」与「真实网表」的分层**。回顾 u3-l3/u4-l2：sv-elab 在翻译过程块时，先把左值记录成抽象的 `VariableBits`（HDL 意图），并不立即物化成线。存储器推断恰好利用了这一点——一个数组在翻译期先被标成「候选存储器」，后续读/写代码据此走专门的存储器端口路径，而不是把它当成普通变量去建线。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/memory.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/memory.h) | 定义 `InferredMemoryDetector`：两遍扫描挑选并淘汰存储器候选。 |
| [src/slang_frontend.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc) | `detect_memories` 调用入口、`is_inferred_memory` 判定、存储器声明、`$memrd_v2` 读端口、`handle_readmem`。 |
| [src/builder.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc) | `RTLILBuilder::add_memory_init` 与 `emit_meminit_cell`：发射 `$meminit_v2`。 |
| [src/procedural.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc) | `$memwr_v2` 写端口发射，以及初始化路径里对 `add_memory_init` 的调用。 |
| [src/diag.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc) | `MemoryNotInferred`、`BadMemoryExpr` 等存储器相关诊断码的文案与级别。 |
| [tests/various/mem_inference.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/mem_inference.ys) | 推断规则的黑盒验证用例。 |
| [tests/various/meminit.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/meminit.ys) | `$meminit_v2` 在各种范围方向下的初始化验证。 |
| [tests/various/readmem.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/readmem.ys) | `$readmemh`/`$readmemb` 的加载验证。 |

## 4. 核心概念与源码讲解

### 4.1 InferredMemoryDetector：存储器候选的筛选与淘汰

#### 4.1.1 概念说明

存储器推断要回答的核心问题是：**哪些数组该被当成存储器？** 一个朴素的想法是「凡是数组就是存储器」，但这会误伤很多场景——比如一个数组被整个读出（`q <= x;` 而非 `q <= x[i];`）、被作为函数参数传递、或者被一个组合逻辑里的阻塞赋值写入，这些用法都无法用「地址端口」表达，强行推断成存储器反而出错。

sv-elab 的策略是「**先宽后严**」的两遍扫描：

1. **收候选**：默认把所有满足基本形状（模块级、静态生存期、固定范围的非压缩数组）的变量都收进候选集合 `memory_candidates`。
2. **淘汰（disqualify）**：再遍历一次整个设计，凡是发现某候选变量被「存储器无法支持」的方式使用了，就把它从候选集合里剔除。

`--no-implicit-memories` 开关把第一步收紧成「只有带 `ram_style`/`rom_style` 等提示属性的数组才进候选」，给用户一个显式控制的入口。如果用户给了提示属性却仍被淘汰，sv-elab 会报 `MemoryNotInferred` 错误，避免「我以为它会变成 RAM，结果没有」的静默失败。

#### 4.1.2 核心流程

`InferredMemoryDetector` 同时继承三样东西（见 [src/memory.h:33-48](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/memory.h#L33-L48)）：

- `TimingPatternInterpretor`：复用 u6-l1 讲过的时序模式分类器，从而能区分 ff/comb/initial 过程。
- `ast::ASTVisitor`：遍历 slang AST。
- `DiagnosticIssuer`：能上报诊断。

它的 `process(root)` 方法做两遍扫描（[src/memory.h:50-76](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/memory.h#L50-L76)）：

```
第一遍 first_pass（用 makeVisitor，只收候选）：
  对每个 VariableSymbol：
    若 lifetime==Static && 类型是非压缩数组 && 有固定范围
       && (未启用 --no-implicit-memories 或 带用户提示属性)
       && 处于模块级（非函数形参、非嵌套实例作用域）：
         加入 memory_candidates

第二遍 *this（用 ASTVisitor，边走边淘汰）：
  按 AST 节点 kind 分派 handle：
    ValueExpressionBase  → 整体引用变量 → disqualify
    PortSymbol           → 变量连到端口 → disqualify
    AssignmentExpression → 整体写左值 / 索引边界是候选 → disqualify
    ElementSelect        → 元素读：value 是普通变量则放行，否则递归
    ExpressionStatement  → 非阻塞写 + wr_allowed：索引写算存储器写，放行
    ProceduralBlockSymbol → interpret()（分类成 ff/comb/initial）
    InstanceSymbol       → 跨层次端口连接里出现的候选 → disqualify
```

**关键状态 `wr_allowed`**（[src/memory.h:157](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/memory.h#L157)）：只有进入「ff 过程的同步体」时才被置真。它保证「存储器写」必须是**时钟驱动的非阻塞赋值** `x[a] <= data`，而不是组合逻辑里的阻塞赋值。这是存储器推断与时序识别（u6-l1/u6-l2）的衔接点。

#### 4.1.3 源码精读

**收候选的条件**（[src/memory.h:52-63](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/memory.h#L52-L63)）逐条对应「数组能当存储器」的最低要求：静态生存期、非压缩数组、固定范围、（可选）提示属性、模块级声明、非形参。

**用户提示属性的识别**（[src/memory.h:103-113](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/memory.h#L103-L113)）维护一个被认可属性名集合：`ram_block`、`rom_block`、`ram_style`、`rom_style`、`ramstyle`、`romstyle`、`syn_ramstyle`、`syn_romstyle`，并在 `--no-implicit-memories` 时作为进入候选的门票。

**淘汰函数 `disqualify`**（[src/memory.h:115-125](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/memory.h#L115-L125)）做两件事：若该变量本在候选集合里且用户给了提示属性，则报 `MemoryNotInferred` 并附一条 `NoteUsageBlame` 指出是哪处用法阻止了推断；然后把它从 `memory_candidates` 擦除。

**非阻塞写的特殊处理**（[src/memory.h:159-197](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/memory.h#L159-L197)）是全讲最精巧的一段。当 `wr_allowed` 为真且语句是非阻塞赋值时，它把左值「剥洋葱」：

- `x[a] <= data`：左值是 `ElementSelect`，其 `value()` 是普通变量 `x` → 判定为「潜在存储器写」，**直接 return 不淘汰** `x`，只把选择子 `a` 当表达式访问一遍（淘汰把别的候选当索引的情况）。
- `x[i+:w] <= data`：左值是 `RangeSelect`，访问边界 `i` 后下钻到 `value()` 继续。
- 整体写 `x <= ...` 或 `x.f <= ...` 的成员写：走 `fallback`（`LHSVisitor`）→ 淘汰。

`LHSVisitor`（[src/memory.h:78-101](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/memory.h#L78-L101)）是配套的左值访问器：遇到 `ElementSelect`/`RangeSelect` 时，把 `value()` 递归给自己、把选择子交给主访问器；遇到普通变量引用则 `disqualify`。这正是「整体写左值 = 淘汰」的判定点。

**ff 过程把 `wr_allowed` 打开**（[src/memory.h:214-226](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/memory.h#L214-L226)）：`handle_ff_process` 先访问 prologue 与异步分支体（`wr_allowed` 仍为假），再在访问同步体 `sync_body` 前后翻转 `wr_allowed`。这把「同步非阻塞写」精准地圈定为唯一合法的存储器写。

**候选集合交接给 NetlistContext**（[src/slang_frontend.cc:2532-2536](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L2532-L2536)）：扫描完成后，`PopulateNetlist::detect_memories` 把 `mem_detect.memory_candidates` 整体赋值给 `netlist.detected_memories`。之后全仓库都通过 `is_inferred_memory()` 查询这张表，而不再触碰探测器本身。

#### 4.1.4 代码实践

**实践目标**：用 `tests/various/mem_inference.ys` 的几个 case 直观感受「收候选—淘汰」规则。

**操作步骤**：

1. 打开 [tests/various/mem_inference.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/mem_inference.ys)。
2. 关注第 1–12 行的 case：`reg [255:0] x[255:0];`，`always @(posedge clk)` 里同时有 `q <= x[a];`（元素读）和 `x[a] <= data;`（非阻塞元素写）。运行后第 12 行 `select -assert-count 1 t:$mem_v2` 期望恰好 1 块存储器。
3. 对照第 41–56 行的 case：同样的数组，但 `always_comb` 里多了 `x[0] = data2;`（**组合阻塞写**）。第 56 行 `select -assert-none t:$mem_v2` 期望**没有**存储器。
4. （可选）若本地已编译好 `slang.so`，运行 `yosys -m slang.so mem_inference.ys` 观察是否通过。

**需要观察的现象**：

- 第 1 个 case：`x` 既被元素读、又被同步非阻塞元素写，两处都「放行」，`x` 留在候选集合 → 推断成存储器。
- 第 4 个 case：组合逻辑里的阻塞写 `x[0] = data2` 让 `wr_allowed` 为假，走 `stmt.expr.visit(*this)` → `handle(AssignmentExpression)` → `LHSVisitor` 淘汰 `x` → 不推断。

**预期结果**：两个断言（`-assert-count 1` 与 `-assert-none`）都成立。若无法本地运行，明确标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：把第 1 个 case 里的 `q <= x[a];` 改成 `q <= x;`（整体读），推断结果会怎样？为什么？

**参考答案**：不再推断存储器。`q <= x` 的右值是 `ValueExpressionBase`（整体引用 `x`），`handle(AssignmentExpression)` 访问右值时命中 `handle(ValueExpressionBase)` → `disqualify(x)`。存储器只能按地址逐字读，无法表达「一次读出整个数组」。

**练习 2**：`--no-implicit-memories` 对第 1 个 case（无属性）和第 3 个 case（带 `(* ram_block *)`）分别有什么影响？

**参考答案**：`disallow_implicit` 为真时，收候选条件 `(!disallow_implicit || find_user_hint(symbol))` 要求必须带提示属性。第 1 个 case 无属性 → 不进候选 → 第 25 行 `select -assert-none t:$mem_v2` 成立（无存储器）；第 3 个 case 带 `ram_block` → 进候选 → 第 39 行 `select -assert-count 1 t:$mem_v2` 成立。

**练习 3**：为什么 `InferredMemoryDetector::handle_initial_process` 的函数体是空的（[src/memory.h:228-231](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/memory.h#L228-L231)）？

**参考答案**：注释写明「Initial processes have no influence over memory inference」。`initial` 块里的赋值（如 `x = '{...}` 或 `$readmemh`）是初始化数据，既不是存储器读端口也不是写端口，不会改变「这个数组能不能当存储器」的判定，所以探测器跳过它。初始化由 `add_memory_init`（见 4.4）单独处理。

### 4.2 is_inferred_memory 的下游效应：声明、读端口与写端口

#### 4.2.1 概念说明

`InferredMemoryDetector` 只产出一张「谁是存储器」的表。这张表如何影响最终的 RTLIL？答案是一个贯穿全仓库的查询函数 `is_inferred_memory()`：凡是碰到数组变量的地方，都先问一句「它是存储器吗？」，是则走存储器专属路径，否则走普通变量路径。

这条查询在三处产生分叉：

1. **声明阶段**：是存储器 → 建 `RTLIL::Memory`；否则 → 建普通线。
2. **表达式求值（右值读）**：`x[a]` 是存储器元素读 → 发 `$memrd_v2`；否则交给 `AddressingResolver` 当普通数组寻址。
3. **左值分析（写）**：`x[a] = ...` 是存储器元素写 → 发 `$memwr_v2`；否则当普通变量部分位写。

#### 4.2.2 核心流程

```
查询入口 is_inferred_memory(symbol) → 查 detected_memories 表
   │
   ├── 声明 (PopulateNetlist)：建 RTLIL::Memory（width/size/start_offset）
   │
   ├── 右值读 EvalContext::operator() ElementSelect 分支：
   │       发 $memrd_v2（地址=selector，数据=占位线），返回占位线
   │
   └── 左值写 LValue::analyze ElementSelect 分支：
           构造 LValue::MemoryWrite{target, address, width}
           → ProceduralContext::assign_to_lvalue_with_masking 发 $memwr_v2
```

#### 4.2.3 源码精读

**查询函数**（[src/slang_frontend.cc:478-488](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L478-L488)）只是对 `detected_memories` 集合的封装；表达式重载先确认是变量引用再取其 symbol 查表。

**存储器声明**（[src/slang_frontend.cc:2579-2595](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L2579-L2595)）：建一个 `RTLIL::Memory`，`width` 取数组元素的 bitstream 宽度，`start_offset` 与 `size` 取自数组的固定范围，并登记到 `canvas->memories` 与 `emitted_mems`（后者用来给每个写端口编号）。否则调用 `add_wire(sym)` 当普通线。

**读端口 `$memrd_v2`**（[src/slang_frontend.cc:1496-1527](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1496-L1527)）：当 `ElementSelect` 的 `value()` 是存储器时，直接建一个组合读端口——`CLK_ENABLE=false`、`EN=1`、`ADDR=selector`，并用 `add_placeholder_signal` 建一根输出线接到 `DATA`。注意这是**组合读**：sv-elab 不在这里判断它是否在时钟块里，时序语义交给后续 `$memwr_v2` 与下游 pass 处理。

**写端口 `$memwr_v2`**（[src/procedural.cc:393-432](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L393-L432)）由左值是 `LValue::MemoryWrite` 时触发。它根据 `ProcessTiming` 决定时钟：

- 组合型（`Implicit`）：`CLK_ENABLE=false`，`CLK=x`。
- 边沿触发型（`EdgeTriggered`）：`CLK_ENABLE=true`，时钟取自 `timing.triggers[0]`，并断言只有一个触发沿（`require(assign, timing.triggers.size() == 1)`）。

每个写端口拿一个自增的 `PORTID`（[src/procedural.cc:413-425](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L413-L425)），并用 `PRIORITY_MASK` 记录「同一块存储器里，哪些更早的写端口优先级低于本端口」——这正是同一周期内多次写同一地址时「后者胜出」语义的来源。写使能 `EN` 把位掩码 `mask` 与「当前 case 使能 ∧ 背景使能」相与（[src/procedural.cc:426-428](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L426-L428)）。

**左值分叉点**（[src/lvalue.cc:88-100](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/lvalue.cc#L88-L100)）：`LValue::analyze` 对 `ElementSelect` 先问 `is_inferred_memory`，是且非 initial 过程则构造 `LValue::memoryWrite`；否则退化成「单元素范围选择」交给 `AddressingResolver`。另外，整体引用一个存储器变量（不是按索引）会触发 `BadMemoryExpr` 诊断（[src/lvalue.cc:52-63](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/lvalue.cc#L52-L63)），除非是在 initial 过程里做初始化。

#### 4.2.4 代码实践

**实践目标**：观察「同一份数组写法」在「是否推断存储器」下的 RTLIL 差异。

**操作步骤**：

1. 准备一段最小 SV：

   ```systemverilog
   module top(input clk, input [7:0] a, input [7:0] data, output reg [7:0] q);
       reg [255:0] x[255:0];
       always @(posedge clk) begin
           q <= x[a];
           x[a] <= data;
       end
   endmodule
   ```

2. 用 `read_slang` 读入后，先不跑 `memory_collect`，执行 `show` 或 `dump`，观察是否出现 `$memrd_v2`、`$memwr_v2` 与 `mem x` 声明。
3. 再跑 `memory_collect`，观察这些端口是否被收拢成单个 `$mem_v2`。

**需要观察的现象**：`read_slang` 之后就能看到字级的 `$memrd_v2`/`$memwr_v2`（sv-elab 的产物）；`memory_collect` 之后才出现 `$mem_v2`（Yosys 下游 pass 的产物）。

**预期结果**：能清晰区分「sv-elab 发出的字级端口」与「下游收拢后的 `$mem_v2`」。若本地无 `slang.so`，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：读端口 `$memrd_v2` 为什么总是 `CLK_ENABLE=false`、`EN=1`？

**参考答案**：sv-elab 在表达式求值时只表达「读取发生的组合语义」——给地址、出数据。读操作本身不绑定某个时钟沿；是否寄存读结果、是否门控，由后续 Yosys pass（如 `memory_collect`/`memory_map`）根据与之配对的写端口时序决定。所以读端口恒为组合、恒使能。

**练习 2**：`emitted_mems[id].num_wr_ports` 自增（[src/procedural.cc:413](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L413)）的作用是什么？

**参考答案**：为同一块存储器的每个写端口分配一个唯一的 `PORTID`，并据此构造 `PRIORITY_MASK`。当多个写端口在同一周期写同一地址时，`PRIORITY_MASK` 编码了端口间的优先级（更早发射的、同存储器的端口位被置 1），下游 pass 据此实现「高优先级端口胜出」的正确冲突语义。

### 4.3 handle_readmem：$readmemh/$readmemb 的编译期加载

#### 4.3.1 概念说明

`$readmemh("file.hex", x)` 与 `$readmemb("file.bin", x)` 是 SV 的系统任务，用于在仿真「启动阶段」把数据文件灌进存储器。在综合里，它们没有运行时语义——文件内容在编译期就是已知的常量。因此 sv-elab 把它们当作**编译期初始化指令**：读文件、解析每一行的字面量、把数据当成对存储器的常量赋值，最终走和「`initial x = '{...}`」相同的路径，落成 `$meminit_v2` 单元。

这与 u5 讲的「initial 过程被求值成常量初始化」一脉相承：`$readmemh` 出现在 `initial` 块里，而 `InferredMemoryDetector` 又明确声明 initial 过程不参与推断（4.1 练习 3），所以这里的 `x` 必须靠别的途径（提示属性或默认推断）已经是存储器。

#### 4.3.2 核心流程

`handle_readmem`（[src/slang_frontend.cc:937](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L937) 起）的处理链：

```
1. 解析第 1 个参数（文件名），要求编译期常量字符串
2. 打开文件；失败则相对「readmem 所在源文件」的目录再试一次（支持相对路径）
   仍失败 → ReadmemFileNotFound
3. 校验第 2 个参数是存储器左值（非压缩数组 + EmptyArgumentExpression 右值）
   否则 → UnsupportedLhs
4. 可选解析第 3、4 个参数（start_addr / finish_addr），要求常量且在范围内
5. 逐行扫描文件：
   - 跳过 // 与 /* */ 注释
   - 遇到 @地址：先把已缓冲数据 flush，再跳到新地址（hex）
   - 否则把 token 按十六进制/二进制解析成一个字，追加到缓冲
6. flush_data：把缓冲数据按字宽切片，处理大/小端方向，
   调用 do_simple_assign 把它赋给存储器的对应区段
```

`do_simple_assign` 是 u5-l3 讲过的赋值落地函数；当左值是存储器时，它会触发 4.4 的 `add_memory_init` 发射 `$meminit_v2`。

#### 4.3.3 源码精读

**文件查找的两段式**（[src/slang_frontend.cc:958-979](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L958-L979)）：先按字面路径打开；失败后取「调用 `$readmem` 那条源语句所在源文件」的父目录（借助 `global_sourcemgr->getFullPath`）再拼一次。这让 `readmem_relative.sv` 这类用例能找到与源码同目录的数据文件。

**左值与范围校验**（[src/slang_frontend.cc:981-1013](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L981-L1013)）：第 2 个参数必须是 `AssignmentExpression`（`x = ...` 形式，右值为空占位），目标类型必须是非压缩数组；可选的 start/finish 地址必须是常量且落在数组范围内，否则分别报 `ErrorNonconstantArgument` 或 `ReadmemAddressOutsideOfRange`。

**逐行解析与 `@` 跳转**（[src/slang_frontend.cc:1066-1136](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1066-L1136)）：先剥掉多行注释；逐 token 处理——`//` 起始则忽略本行余下内容；`@` 起始则把后继十六进制解析为新地址，先 flush 已缓冲数据再跳转；否则交给 `str_to_state_vect` 把 token 解析成一个字（[src/slang_frontend.cc:905-933](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L905-L933) 支持十六进制/二进制、`x`/`z` 值，并按 MSB 把字截断或扩展到 `mem_width`）。

**flush 与方向处理**（[src/slang_frontend.cc:1044-1064](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1044-L1064)）：把缓冲数据切成整字，根据「填充方向 `increment`」与「数组范围是否降序」决定是否按字倒序（`reverse_data`），算出这段数据在数组里的 HDL 基地址 `hdl_base`，再换算成存储器的零基位偏移 `base`，最后用 `do_simple_assign` 赋给 `target.extract(base*word_size, ...)`。这套基地址换算是为了把「SV 的 `[3:0]` 降序范围」与「RTLIL 存储器的零基线性地址」对齐。

**收尾校验**（[src/slang_frontend.cc:1141-1145](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1141-L1145)）：当显式给了 finish 地址且没有 `@` 跳转时，若实际读到的字数与 `[start, finish]` 区间长度不符，报 `ReadmemWordsRangeMismatch`。

#### 4.3.4 代码实践

**实践目标**：用 [tests/various/readmem.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/readmem.ys) 验证 `$readmemh` 的加载与等价性。

**操作步骤**：

1. 阅读 readmem.ys 第 1–23 行的 `top1`：用 `$readmemh("readmem_data.hex", x, 0, 3)` 加载 `x`，同时用赋值模式 `y = '{...}` 给「对照数组」`y` 同样的数据；再用 `always_comb` 断言 `x[addr] === y[addr]`。
2. 跟踪 `readmem_data.hex`（同目录）的内容，确认它就是 `'{8'h01, 8'h2a, 8'hff, 8'h80}`。
3. 关注第 49–71 行的 `top3`：先 `x = '{8{8'h00}}` 清零，再 `$readmemh(..., x, 1)` 从下标 1 开始覆盖，验证 start 地址偏移。
4. 关注第 73–94 行的 `top4`：数据文件 `readmem_at.hex` 内含 `@` 地址跳转，验证 flush 与跳转逻辑。

**需要观察的现象**：每个 case 最后都用 `sat -verify -enable_undef -prove-asserts` 证明「`$readmemh` 加载的存储器」与「赋值模式初始化的对照存储器」在所有地址上完全相等。

**预期结果**：四个 case 的 SAT 证明全部通过。这等价于证明了 `handle_readmem` 的解析（含方向、start 偏移、`@` 跳转）与 `add_memory_init` 的初始化路径产出一致。若本地无法运行，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `handle_readmem` 要尝试「相对源文件目录」打开数据文件？

**参考答案**：用户写 `$readmemh("data.hex", x)` 时，`data.hex` 通常是相对路径。综合是在某个工作目录下执行的，而数据文件常与源码放在一起。sv-elab 取「调用 `$readmem` 的源文件所在目录」作为后备基目录，能让相对路径在不同工作目录下都正确解析，对应 `readmem_relative.sv` 用例。

**练习 2**：`$readmemh` 的第 3、4 个参数（start/finish）超出数组范围会怎样？

**参考答案**：会报 `ReadmemAddressOutsideOfRange`（[src/slang_frontend.cc:1007-1012](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1007-L1012) 与 1025–1030），并直接 return，不加载任何数据。这是编译期校验，避免发出越界的 `$meminit_v2`。

### 4.4 add_memory_init：$meminit_v2 初始化单元的发射

#### 4.4.1 概念说明

无论初始化数据来自赋值模式 `x = '{7, 1}` 还是 `$readmemh`，最终都汇入同一条「把一段常量写进存储器某些字」的通路：`RTLILBuilder::add_memory_init`。它发射 Yosys 的 `$meminit_v2` 单元——一块「上电时把 `DATA` 写到从 `ADDR` 起的 `WORDS` 个字」的初始化描述。

这里有一个工程难点：**位对齐**。初始化数据是按「bitstream」线性给出的，但存储器按「字」存放。如果数据起始位置不是字的整数边界（比如某个 `add_memory_init` 调用的 `bit_offset` 落在字的中间），一个 `$meminit_v2` 就表达不了——因为它总是整字写。sv-elab 的解法是把一段未对齐的数据**最多拆成 3 段**：头部的半字（带掩码只写有效位）、中部的整字（全 1 掩码）、尾部的半字。

#### 4.4.2 核心流程

`add_memory_init`（[src/builder.cc:649](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L649) 起）：

```
输入：存储器名 name、位偏移 bit_offset、是否大端 big_endian、数据 data
取 mem = canvas->memories[name]
若 bit_offset 不是 mem->width 的整数倍：
    → 拆出头部半字，用 Sx 填充到整字，掩码只在有效位为 1
    → emit_meminit_cell（字偏移 = bit_offset/width）
若还有剩余且已对齐：
    → 整字段，掩码全 1
    → emit_meminit_cell
若还有尾部不足一字：
    → 拆出尾部半字，同样用 Sx 填充、掩码标注有效位
    → emit_meminit_cell
断言：processed == data.size()（全部数据已落盘）
```

`emit_meminit_cell`（[src/builder.cc:626-647](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L626-L647)）建一个 `$meminit_v2` 单元，设 `MEMID/WORDS/WIDTH/ABITS`，`PRIORITY` 取自一个自增计数器 `meminit_prio_counter`，`ADDR` 按大/小端换算（大端时把字偏移「镜像」到数组末尾），`DATA` 在大端时按字倒序。

**调用点**有两处，都体现「initial 过程的数据落到存储器」：

- **过程块初始化**（[src/procedural.cc:278-285](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L278-L285)）：当 `do_simple_assign` 落到一个 `Static` 存储器变量时，除记录 `initial_state` 外，调用 `add_memory_init`，`big_endian` 由数组范围是否降序决定。`$readmemh` 走的也是这条路径。
- **声明初值**：`initialization.cc` 在处理变量声明时，对存储器目标用一个 `ProcessTiming::initial` 的临时 `ProceduralContext` 求值（[src/initialization.cc:96-97](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/initialization.cc#L96-L97) 注释明确「Use ProceduralContext to get `$meminit` emission if the target is a memory」），从而把声明初值也汇入同一条 `$meminit_v2` 通路。

#### 4.4.3 源码精读

**头部半字拆分**（[src/builder.cc:667-679](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L667-L679)）：`offset_in_cell = bit_offset % mem->width` 是落在字内的位置。构造 `data1` 为 `[Sx × offset_in_cell] + [真实数据 length] + [Sx × 剩余]`，`mask1` 对应为 `[S0...] + [S1 × length] + [S0...]`——只有真实数据那几位掩码为 1，其余位写 `x`（不覆盖）。

**整字段**（[src/builder.cc:681-687](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L681-L687)）：剩余数据量除以字宽取整，掩码全 1，直接整字写。

**尾部半字**（[src/builder.cc:689-698](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L689-L698)）：不足一字的尾巴，结构与头部对称。

**`emit_meminit_cell` 的大端处理**（[src/builder.cc:642-645](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L642-L645)）：`ADDR` 在大端时取 `start_offset + (size - word_offset - nwords)`（镜像到末尾），`DATA` 用 `reverse_data` 按字倒序。这让 SV 的 `[3:0]` 降序数组与 RTLIL 的升序线性地址对齐。`reverse_data`（[src/builder.cc:615-623](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L615-L623)）按字宽分组倒置。

**诊断码**（[src/diag.cc:180-186](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L180-L186)）：`MemoryNotInferred`（Netlist 1020，Error）在「带属性却被淘汰」时报；`BadMemoryExpr`（1019，Error）在「整体引用存储器」时报；`NoteUsageBlame`（1021，Note）作为前者附注指出罪魁用法。声明见 [src/diag.cc:73-75](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L73-L75)。

#### 4.4.4 代码实践

**实践目标**：用 [tests/various/meminit.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/meminit.ys) 验证 `$meminit_v2` 在不同范围方向下的正确性。

**操作步骤**：

1. 阅读 meminit.ys：它有 5 个 case，分别声明 `reg [3:0] x[1:0]`、`[0:1]`、`[1:2]`、`[2:1]`、`[0:-1]`，每个 case 都用 `initial x = '{7, 1}` 初始化，并对照 `y` 做断言。
2. 对每个 case，画出「数组范围方向（升/降序）→ `big_endian` 取值 → `$meminit_v2` 的 ADDR 与 DATA 是否倒序」的对应关系。
3. 关注第 91–112 行的 `[0:-1]` case（含负下标）：它是 dump 模式，便于直接观察生成的 `$meminit_v2` 参数。

**需要观察的现象**：降序范围（`[1:0]`、`[2:1]`）触发 `big_endian=true`，`DATA` 按字倒序、`ADDR` 镜像；升序范围（`[0:1]`、`[1:2]`、`[0:-1]`）则 `big_endian=false`，数据原序。最终 5 个 case 的 `sat -prove-asserts` 都通过，说明 `x` 与对照 `y` 在所有下标上相等。

**预期结果**：无论范围方向、无论下标正负，`add_memory_init` 都把 `'{7, 1}` 正确写入对应字。若本地无法运行，标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么一段未对齐的初始化数据「最多」拆成 3 个 `$meminit_v2`？

**参考答案**：因为未对齐只可能发生在数据的「头」和「尾」：头部半字需要单独一个带掩码的单元把有效位写进去，中部都是整字用一个全 1 掩码的单元批量写，尾部若再剩不足一字又需一个带掩码的单元。所以最多「头 + 中 + 尾」3 个；若数据本身整字对齐，就只有中部一段。

**练习 2**：`PRIORITY` 参数（[src/builder.cc:638](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L638)）来自自增计数器，它的作用是什么？

**参考答案**：当同一存储器被多次初始化（例如先 `x = '{0}` 清零，再 `$readmemh` 覆盖部分字）时，会发出多个 `$meminit_v2`。`PRIORITY` 给它们排序：下游 pass 按 priority 从低到高应用，高优先级（后发的）覆盖低优先级，从而正确表达「后写胜出」的初始化顺序语义。

## 5. 综合实践

把本讲三条主线串起来，做一个端到端的小任务。

**任务**：写一个 ROM 模块，用 `(* rom_block *)` 显式要求推断存储器，并用 `$readmemh` 加载内容，最后验证它能被综合成单块 `$mem_v2` 且内容正确。

**参考骨架**（示例代码，非项目原有文件）：

```systemverilog
module rom #(parameter DEPTH = 16, parameter W = 8)
            (input [$clog2(DEPTH)-1:0] addr, output [W-1:0] dout);
    (* rom_block *)
    reg [W-1:0] mem [0:DEPTH-1];

    initial begin
        mem = '{DEPTH{8'h00}};
        $readmemh("rom_data.hex", mem);
    end

    assign dout = mem[addr];
endmodule
```

**要求完成的步骤**：

1. **推断侧**：解释为什么必须用 `--no-implicit-memories` 加 `(* rom_block *)`（或默认推断）才能让 `mem` 成为存储器候选；指出 `assign dout = mem[addr]` 走的是 4.2 的 `$memrd_v2` 路径。
2. **初始化侧**：说明 `mem = '{DEPTH{8'h00}}` 与 `$readmemh` 分别经由哪条调用链（`do_simple_assign` → `add_memory_init`）落成 `$meminit_v2`，并解释「先清零后加载」时 `PRIORITY` 如何保证加载覆盖清零。
3. **验证侧**：参照 readmem.ys，写一个对照数组 `expected`，用 `always_comb assert(mem[addr] === expected[addr])`，跑 `read_slang` → `chformal -lower` → `memory_collect` → `select -assert-count 1 t:$mem_v2` → `memory_map` → `sat -prove-asserts`。
4. **诊断侧**：故意把 `mem[addr]` 改成整体读 `mem`（例如临时加一句 `wire [W*DEPTH-1:0] bad = {<<8{mem}};`），观察是否触发 `BadMemoryExpr`。

**预期结果**：能分别说清「推断（4.1）」「读端口（4.2）」「加载与初始化（4.3/4.4）」三段在源码里的对应位置，并通过等价性/SAT 验证。运行命令若无本地环境，标注「待本地验证」。

## 6. 本讲小结

- sv-elab 用 `InferredMemoryDetector` 做「先收候选、再淘汰」两遍扫描：模块级静态非压缩数组默认进候选，但凡被整体引用、连端口、阻塞写、当索引边界使用就被 `disqualify`。
- `wr_allowed` 把「合法存储器写」精准限定为 ff 过程同步体里的非阻塞元素写，这是推断与时序识别（u6）的衔接点；`--no-implicit-memories` 收紧成「必须带 `ram_style`/`rom_style` 等属性」，带属性却被淘汰会报 `MemoryNotInferred`。
- 候选表经 `detect_memories` 交给 `detected_memories`，全仓库通过 `is_inferred_memory()` 查询，分叉出三条路径：声明 `RTLIL::Memory`、读端口 `$memrd_v2`、写端口 `$memwr_v2`（带 `PORTID`/`PRIORITY_MASK` 表达同周期冲突语义）。
- `$readmemh`/`$readmemb` 被当作编译期初始化：`handle_readmem` 解析文件（支持相对路径、`@` 跳转、注释、x/z），经 `do_simple_assign` 汇入与赋值模式相同的初始化通路。
- `add_memory_init` 把常量数据落成 `$meminit_v2`，用「头—中—尾」最多三段拆分处理位未对齐，用 `big_endian` 与 `reverse_data` 处理 SV 降序范围与 RTLIL 升序地址的对齐，用 `PRIORITY` 表达多次初始化的覆盖顺序。

## 7. 下一步学习建议

- **u7-l2 层次处理**：存储器声明存活在单个 `NetlistContext`/realm 里；当模块被展平（`should_dissolve`）或保留层次时，存储器与端口的归属会随之变化，建议结合 u7-l2 理解 `realm` 与存储器的关系。
- **u7-l3 黑盒导入导出**：若一块存储器来自外部 IP（黑盒），其端口宽度与参数限制由 `export_blackbox_to_rtlil` 的诊断把关，可作为存储器推断的对照学习。
- **阅读下游 pass**：sv-elab 只发字级 `$memrd_v2`/`$memwr_v2`/`$meminit_v2`；建议在 Yosys 里跟踪 `memory_collect`（收拢成 `$mem_v2`）与 `memory_map`（映射到 BRAM/寄存器），完整理解一块 SV 数组如何变成真实硬件存储器。
- **延伸阅读**：对照 [tests/various/readmem_diag.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/readmem_diag.ys) 了解 `$readmem` 的各种错误路径诊断，巩固 4.3 的校验逻辑。
