# LValue：左值的结构化分析

## 1. 本讲目标

学完本讲你应该能够：

- 说清楚 sv-elab 为什么不把赋值号左边的左值（LHS）当成普通右值表达式去求值，而要先做一遍「结构化分析」。
- 识别 `LValue` 的五种 descriptor（变量、拼接、范围选择、成员访问、存储器写），并为一条复合左值画出 descriptor 树。
- 跟读 `LValue::analyze` 的大 switch，知道每种 slang 表达式种类落到哪个分支、产出哪种 descriptor。
- 解释 `is_static()` 的判定规则，以及静态左值如何通过 `evaluate_vbits()` 衔接到 u3-l3 的 `VariableBits`；动态左值如何在 `assign_to_lvalue_with_masking` 里被递归「摊平」成真实电路。
- 准确说出 `is_contiguous_slice()` 在当前代码树中的真实状态（已声明、字段有维护，但访问器本身无定义）。

## 2. 前置知识

本讲承接 [u4-l1 表达式求值](u4-l1-expression-evaluation.md)。先回顾三个要点：

- `EvalContext::operator()` 把 slang 表达式求值成「右值」`RTLIL::SigSpec`——回答「这条线**现在的值**是什么」。
- 但赋值语句 `lhs = rhs`、`lhs <= rhs` 里，`lhs` 是「**要把值写到哪里**」，方向相反。把 `lhs` 当右值求值，通常只会得到「当前读出来的旧值」，而不是「待写入的目标」。
- u3-l3 引入了 `Variable` / `VariableBit` / `VariableBits` 这一组「HDL 意图」位级抽象，用来在过程块的 case 树里稳定地标记「某变量的某些位」。

几个术语澄清：

- **左值（LHS / lvalue）**：赋值号左边，表示写入目标。
- **右值（RHS / rvalue）**：赋值号右边，表示要写入的值。
- **静态（static）左值**：所指的位在编译期就能确定，例如 `a`、`a[7:4]`、`s.field`，不需要运行时 mux/demux 电路。
- **动态（dynamic）左值**：含有运行时才确定的索引，例如 `a[i]`、`a[i+:4]`，需要寻址电路。
- **bitstream（位流）**：slang 把任何可综合类型按「位」线性展开的顺序；sv-elab 用 `getBitstreamWidth()` 统一度量位宽，是 LValue 全程使用的长度单位。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/lvalue.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/lvalue.cc) | LValue 的全部实现：`analyze` 分派、五个工厂方法、`is_static`、`evaluate_vbits` |
| [src/slang_frontend.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h) | `LValue` 类声明（五个 descriptor 子结构、私有字段）、`EvalContext::lhs`、`assign_to_lvalue_with_masking` 声明 |
| [src/slang_frontend.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc) | `EvalContext::lhs`：调用 `analyze` 并要求静态，否则降级为 dummy |
| [src/procedural.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc) | `assign_to_lvalue_with_masking`：动态左值的递归摊平与存储器写单元 `$memwr_v2` 的发射 |
| [src/addressing.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/addressing.cc) | `AddressingResolver::is_static` 与 `extract<VariableBits>`：范围/元素选择的静态判定与位提取 |
| [src/variables.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/variables.h) | `VariableBits` 目标类型（u3-l3），`evaluate_vbits` 的产出 |

## 4. 核心概念与源码讲解

### 4.1 LValue：左值的结构化容器

#### 4.1.1 概念说明

赋值语句的左边可以很复杂：`{a, b[3:0]}` 是拼接、`s.field` 是结构体成员访问、`mem[i]` 是存储器写、`a[i+:4]` 是动态范围选择。sv-elab 不能简单地「求值」它们，因为：

1. 左值关心的是**位置**，不是**值**。
2. 部分左值（动态索引、存储器写）需要生成**寻址电路**，而不是一根连线。
3. 即便是静态左值，sv-elab 也希望把它描述成 u3-l3 的 `VariableBits`（「某变量的某些位」），以便喂给过程块的 case 树，而不是过早物化成 `RTLIL::SigSpec`。

于是 sv-elab 引入 `LValue`：一个**结构化的、可递归的左值描述容器**。它不直接产生 RTLIL，而是先把 slang 左值表达式「翻译」成一棵由五种基本形态组成的树，再由消费方（`evaluate_vbits` 或 `assign_to_lvalue_with_masking`）决定如何落地。

#### 4.1.2 核心流程

`LValue` 的生命周期是「分析 → 判定 → 消费」三步：

```
slang 左值表达式
      │  LValue::analyze(context, expr)   ← 大 switch 分派
      ▼
LValue 对象（descriptor 树 + bitsize + static_ + contiguous_slice_）
      │
      ├── 若 is_static()==true
      │     └─ evaluate_vbits()  → VariableBits  （喂给 case 树 / lhs()）
      │
      └── 否则（动态）
            └─ assign_to_lvalue_with_masking(...) 递归摊平 → RTLIL 单元
```

关键在于：`LValue` 自己**不建任何 RTLIL 单元**（存储器写除外，那个是在消费阶段建的）。它只负责「描述」。

#### 4.1.3 源码精读

`LValue` 的公共 API 很小——一个静态工厂族加上两个查询方法：

```cpp
// src/slang_frontend.h:702-720（节选）
class LValue {
public:
    static std::optional<LValue> analyze(EvalContext &context, const ast::Expression &expr, bool silent=false);
    static LValue variable(Variable variable);
    static LValue concatenation(std::vector<LValue> elements);
    static LValue rangeSelect(LValue inner, AddressingResolver resolver, uint64_t bitsize);
    static LValue memberAccess(LValue inner, uint64_t base_offset, uint64_t bitsize);
    static LValue memoryWrite(Variable variable, RTLIL::SigSpec address, uint64_t bitsize);

    bool is_static();
    bool is_contiguous_slice();
    VariableBits evaluate_vbits();   // 仅当 is_static()==true 时可用
    ...
};
```

这五个 `static` 工厂对应五种 descriptor，`analyze` 是它们的统一入口。详见 [src/slang_frontend.h:L702-L753](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L702-L753)：声明公共 API、私有 descriptor 子结构、以及私有构造函数。

#### 4.1.4 代码实践

- **实践目标**：建立「LValue 是描述容器、不直接建单元」的直觉。
- **操作步骤**：打开 `src/lvalue.cc`，通读整个文件（只有约 226 行）。注意 `analyze`、五个工厂、`is_static`、`evaluate_vbits` 各占多少行。
- **观察现象**：你会发现除了 `MemoryWrite` 分支（它在 `procedural.cc` 的消费阶段才真正建 `$memwr_v2` 单元），`lvalue.cc` 里几乎不出现 `canvas->addXxx`、`addCell` 之类的 RTLIL 构造调用。
- **预期结果**：确认 LValue 是「纯描述层」，RTLIL 物化发生在它的消费方。
- 待本地验证：你可以在 `lvalue.cc` 内搜索 `canvas` / `addCell`，应只在概念上为零。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `evaluate_vbits()` 的注释写「Only available if `is_static()` true」？
**答**：因为动态左值（含运行时索引或存储器写）无法在编译期确定具体是「哪些位」，自然无法折叠成一个静态的 `VariableBits`；强行调用会在 `evaluate_vbits` 内部的 `log_assert(static_)` 处断言失败。

**练习 2**：`LValue` 用 `std::optional<LValue>` 作为 `analyze` 的返回类型，而不是直接返回 `LValue`，原因可能是什么？
**答**：左值可能「不可综合」（例如流拼接、动态大小的类型），此时 `analyze` 需要一个「失败」语义来表达「这条左值我处理不了」，`std::nullopt` 正是这个失败标志，同时错误诊断已在 `analyze` 内部上报。

---

### 4.2 五种 descriptor：左值的五种基本形态

#### 4.2.1 概念说明

`LValue` 内部用一个 `std::variant` 持有五种「descriptor」之一，分别对应 SystemVerilog 左值的五种基本写法：

| descriptor | 对应 SV 写法 | 含义 |
|------------|-------------|------|
| `Variable` | `a` | 整个变量 |
| `Concatenation` | `{a, b[3:0]}` | 多个左值拼接 |
| `RangeSelect` | `a[7:4]`、`a[i]`、`a[i+:4]` | 位/元素范围选择 |
| `MemberAccess` | `s.field` | 结构体成员访问 |
| `Variable` 之外的 `MemoryWrite` | `mem[i]`（存储器写） | 写存储器某个地址 |

其中 `RangeSelect` 和 `MemberAccess` 都持有一个 `inner` 子 `LValue`，从而可以递归嵌套（如 `s.arr[i].field`）。

#### 4.2.2 核心流程

每个工厂方法做三件事：构造对应 descriptor、累加 `bitsize`、按规则计算两个布尔字段 `static_` 与 `contiguous_slice_`。两个字段的传播规则如下：

```
static_（是否编译期可定）:
  Variable       = true
  Concatenation  = 所有 element 的 static_ 之 AND
  RangeSelect    = inner.static_  AND  resolver.is_static()
  MemberAccess   = inner.static_            （成员偏移总是编译期常量）
  MemoryWrite    = false                    （地址是运行时信号）

contiguous_slice_（是否单个变量的连续段）:
  Variable       = true
  Concatenation  = false                    （拼接跨变量，不连续）
  RangeSelect    = 继承 inner
  MemberAccess   = 继承 inner
  MemoryWrite    = false
```

`static_` 决定了走 `evaluate_vbits`（静态捷径）还是 `assign_to_lvalue_with_masking`（动态摊平）。

#### 4.2.3 源码精读

五个 descriptor 子结构定义在 [src/slang_frontend.h:L723-L741](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L723-L741)：

```cpp
struct Concatenation { std::vector<LValue> elements; };
struct RangeSelect   { std::unique_ptr<AddressingResolver> resolver;
                       std::unique_ptr<LValue> inner; };
struct MemberAccess  { uint64_t base_offset; std::unique_ptr<LValue> inner; };
struct MemoryWrite   { Variable target; RTLIL::SigSpec address; };
```

注意 `RangeSelect::resolver` 与两个 `inner` 都用 `std::unique_ptr` 持有——因为 `LValue` 自身放在 `std::variant` 里，需要保证递归成员的地址稳定、可前向声明。

工厂方法对两个布尔字段的赋值，见 [src/lvalue.cc:L157-L194](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/lvalue.cc#L157-L194)。例如拼接把 `contiguous_slice_` 硬编码为 `false`：

```cpp
// src/lvalue.cc:162-172（节选）
LValue LValue::concatenation(std::vector<LValue> elements) {
    uint64_t size_total = 0;
    bool static_ = true;
    for (auto &element : elements) {
        size_total += element.bitsize;
        static_ &= element.static_;          // 任一元素动态则整体动态
    }
    return LValue(Concatenation{std::move(elements)}, size_total, static_,
                  /* contiguous_slice_= */ false);
}
```

而范围选择会同时依赖内层与 `resolver`：

```cpp
// src/lvalue.cc:174-181（节选）
LValue LValue::rangeSelect(LValue inner, AddressingResolver resolver, uint64_t bitsize) {
    bool static_ = inner.static_ && resolver.is_static();   // 索引也必须编译期确定
    bool contiguous_slice_ = inner.contiguous_slice_;       // 连续性继承内层
    ...
}
```

`resolver.is_static()` 的实现非常直接——它只看选择索引是否是「全确定的常量」，见 [src/addressing.cc:L351-L354](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/addressing.cc#L351-L354)：

```cpp
bool AddressingResolver::is_static() {
    return raw_signal.is_fully_def();
}
```

> **关于 `is_contiguous_slice()` 的诚实说明**：访问器 `is_contiguous_slice()` 在 [src/slang_frontend.h:L717](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L717) 处声明，底层数据字段 `contiguous_slice_` 也由各工厂方法正确维护（规则见上文表格）。但在当前代码树中，**该访问器没有定义体，也没有任何调用点**（全仓搜索 `is_contiguous_slice` 只命中这一处声明）。因此本讲只描述「字段被如何维护」，**不**断言它在运行时有任何实际效果——这部分属于「待确认 / 待本地验证」。

#### 4.2.4 代码实践

- **实践目标**：亲手核对两个布尔字段的传播规则。
- **操作步骤**：对照 [src/lvalue.cc:L157-L194](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/lvalue.cc#L157-L194) 的五个工厂，逐一填下面这张表。

| 工厂 | `static_` 来源 | `contiguous_slice_` |
|------|---------------|---------------------|
| `variable` | ? | ? |
| `concatenation` | ? | ? |
| `rangeSelect` | ? | ? |
| `memberAccess` | ? | ? |
| `memoryWrite` | ? | ? |

- **观察现象 / 预期结果**：你应当得到与 4.2.2 表格完全一致的结论。
- 待本地验证：可写一段小程序实例化这几个工厂并打印 `is_static()` 来确认（注意 `is_contiguous_slice()` 目前不可链接，见上文说明）。

#### 4.2.5 小练习与答案

**练习 1**：`MemoryWrite` 的 `static_` 为什么恒为 `false`？
**答**：存储器写的地址 `address` 是一个 `RTLIL::SigSpec`（运行时信号），写哪个字在编译期并不固定，所以它不可能是静态左值，也无法折叠成 `VariableBits`。

**练习 2**：为什么 `RangeSelect` 和 `MemberAccess` 要存 `std::unique_ptr<LValue> inner`，而不是直接存 `LValue inner`？
**答**：`LValue` 是「含 variant 的递归类型」，而 `std::variant` 要求其备选类型在定义点已是完整类型。`LValue` 持有自身类型的子对象，必须借助指针（`unique_ptr`）打破「类型大小依赖于自身」的循环，同时前向声明即可。

---

### 4.3 LValue::analyze：从 slang 表达式到 LValue 树

#### 4.3.1 概念说明

`LValue::analyze` 是「把 slang 左值表达式翻译成 `LValue` 树」的总入口。它是一个静态方法，接收 `EvalContext` 与一个 slang 表达式，返回 `std::optional<LValue>`（失败返回 `std::nullopt`）。它的实现是一个按 `expr.kind` 分派的大 switch，结构清晰：每个 case 处理一种左值形态，多数 case 会**递归调用自身**来分析「内层」左值。

#### 4.3.2 核心流程

```
analyze(context, expr):
  断言 expr 不是 Streaming（流拼接另走 streaming_lhs）
  若 expr 类型不是固定大小 → 报 FixedSizeRequired，返回 nullopt

  switch expr.kind:
    HierarchicalValue / NamedValue  → 变量分支
        （含 inferred-memory 守卫、ModportPort 改写）
        → variable(context.variable(symbol))
    RangeSelect                     → 范围选择分支
        analyze(内层) → rangeSelect(inner, resolver, bitsize)
    ElementSelect                   → 元素选择 = 单元素范围选择
        若是 inferred memory 且非 initial → memoryWrite(...)
        否则 analyze(内层) → rangeSelect(inner, addr, bitsize)
    Concatenation                   → 拼接分支
        逐个 analyze 操作数 → concatenation(elements)
    MemberAccess                    → 成员访问分支
        analyze(内层) → memberAccess(inner, bit_offset, bitsize)
    Conversion（同宽位流转换）      → 透明穿透到 operand
    default                         → 报 UnsupportedLhs，返回 nullopt
```

注意一个设计要点：`ElementSelect`（`a[i]`）在普通变量上被当成「宽度为 1 的范围选择」处理；只有在被推断为存储器（`is_inferred_memory`）且不在 initial 过程里时，才走 `memoryWrite` 分支。

#### 4.3.3 源码精读

入口的前置检查见 [src/lvalue.cc:L28-L49](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/lvalue.cc#L28-L49)：先拒绝 Streaming、再拒绝动态大小类型，然后进入 switch。变量分支里有一段对「推断存储器」的守卫——在非 initial 过程里直接把存储器当左值是非法的，存储器写应当更早被 `assign_rvalue_inner` 拦下：

```cpp
// src/lvalue.cc:52-64（节选）—— 存储器左值守卫
if (context.netlist.is_inferred_memory(symbol)) {
    if (!(context.procedural &&
          context.procedural->timing.kind == ProcessTiming::Initial)) {
        if (!silent)
            context.netlist.add_diag(diag::BadMemoryExpr, expr.sourceRange);
        return std::nullopt;
    }
}
```

元素选择的双重身份见 [src/lvalue.cc:L88-L110](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/lvalue.cc#L88-L110)：先判断是否存储器写，否则构造一个 `AddressingResolver` 并复用 `rangeSelect`：

```cpp
// src/lvalue.cc:94-109（节选）
if (context.netlist.is_inferred_memory(ese.value()) &&
        context.procedural->timing.kind != ProcessTiming::Initial) {
    RTLIL::SigSpec address = context(ese.selector());   // 右值求值地址
    auto variable = Variable::from_symbol(&ese.value().as<ast::ValueExpressionBase>().symbol);
    return LValue::memoryWrite(variable, address, ese.type->getBitstreamWidth());
}
std::optional<LValue> inner = analyze(context, ese.value());
...
AddressingResolver addr(context, ese);
return LValue::rangeSelect(std::move(*inner), std::move(addr), expr.type->getBitstreamWidth());
```

拼接与成员访问的递归结构见 [src/lvalue.cc:L111-L136](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/lvalue.cc#L111-L136)。成员访问用 `bitstream_member_offset(member)` 算出字段在位流里的偏移——这个辅助函数对解包结构体做了 MSB/LSB 顺序换算，见 [src/slang_frontend.cc:L189-L208](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L189-L208)。

最后是兜底分支 [src/lvalue.cc:L137-L154](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/lvalue.cc#L137-L154)：同宽位流 `Conversion` 会透明穿透到操作数（这样 `typed'(lhs)` 不会丢信息），其余一律落到 `default` 报 `UnsupportedLhs`。

涉及的三条诊断消息（可在 `src/diag.cc` 核对）：
- `FixedSizeRequired`：「expression of type {} with dynamic size unsupported for synthesis」
- `BadMemoryExpr`：「unsupported operation on a memory variable」
- `UnsupportedLhs`：「unsupported assignment target expression」

#### 4.3.4 代码实践

- **实践目标**：把一条具体左值映射到它的 descriptor 树。
- **操作步骤**：阅读测试 [tests/unit/field.sv](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/field.sv)，它含一个 packed struct 与两条连续赋值：
  ```systemverilog
  assign struct_signal[FIELD0_WIDTH+FIELD1_WIDTH-1:FIELD1_WIDTH] = in0;  // 范围选择
  assign struct_signal[FIELD1_WIDTH-1:0] = in1;                          // 范围选择
  ```
- **观察现象**：这两条左值都形如 `变量[常量范围]`。按 4.3.2 的流程，它们会先命中 `NamedValue`（得到 `variable(...)`），外层命中 `RangeSelect`。
- **预期结果**：画出树 `RangeSelect( inner=Variable(struct_signal), resolver=静态 )`，且因范围是常量、内层是变量，整体 `is_static()==true`。

#### 4.3.5 小练习与答案

**练习 1**：`a[i]`（`i` 是运行时变量）与 `a[3]`（常量）在 `analyze` 里分别走哪条路径？产出的 `static_` 是什么？
**答**：两者都进入 `ElementSelect` 分支。若 `a` 不是存储器，都构造 `AddressingResolver` 并产出 `RangeSelect`。区别在 `resolver.is_static()`：`a[3]` 的 `raw_signal` 是全确定常量 → `is_static()==true`；`a[i]` 的 `raw_signal` 含运行时位 → `is_static()==false`，于是 `LValue::static_==false`，后续走动态摊平路径。

**练习 2**：为什么 `analyze` 要在方法开头显式拒绝 `Streaming`？
**答**：流拼接 `{<<n{...}}` / `{>>n{...}}` 作为左值有特殊的位重排语义，sv-elab 用单独的 `EvalContext::streaming_lhs` 处理（见 u4-l1），不能套用这五种 descriptor，因此 `analyze` 在入口就 `ast_invariant` 拦截，避免误入通用路径。

---

### 4.4 静态、连续与求值：LValue 与 VariableBits 的衔接

#### 4.4.1 概念说明

得到 `LValue` 之后，消费方有两条路：

- **静态捷径**：`is_static()==true` 时，调用 `evaluate_vbits()` 把整棵 descriptor 树折叠成一个 `VariableBits`（u3-l3 的「某变量的某些位」）。这条路径用于连续赋值、过程块的静态左值等。
- **动态摊平**：`is_static()==false` 时，由 `assign_to_lvalue_with_masking` 递归地遍历 descriptor 树，对每一层生成对应的 RTLIL 寻址电路（mux/demux/shift）或存储器写单元。

本节聚焦静态捷径与两个入口：`EvalContext::lhs`（要求静态）和 `evaluate_vbits`。

#### 4.4.2 核心流程

`EvalContext::lhs` 是「我需要一个静态左值的 `VariableBits`」的入口，流程为：

```
lhs(expr):
    analyzed = LValue::analyze(*this, expr, silent)
    若失败 → 返回 dummy 变量（诊断已在 analyze 内上报）
    若 not analyzed.is_static() → 报 UnsupportedLhs，返回 dummy
    返回 analyzed.evaluate_vbits()        ← 折叠成 VariableBits
```

而 `evaluate_vbits()` 按 descriptor 递归折叠：

```
evaluate_vbits():  （入口断言 static_）
  Variable       → VariableBits(该变量全部位)
  Concatenation  → 逆序遍历 elements（SV 拼接是 MSB-first）逐个 append
  RangeSelect    → inner.evaluate_vbits() → resolver.extract<VariableBits>(vbits, bitsize)
  MemberAccess   → inner.evaluate_vbits() → vbits.extract(base_offset, bitsize)
```

#### 4.4.3 源码精读

`EvalContext::lhs` 的完整实现见 [src/slang_frontend.cc:L620-L641](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L620-L641)，它的注释也点明了「不处理动态寻址与流拼接，调用方需另行处理」：

```cpp
// src/slang_frontend.cc:620-641（节选）
VariableBits EvalContext::lhs(const ast::Expression &expr, bool silent) {
    ast_invariant(expr, expr.kind != ast::ExpressionKind::Streaming);
    auto analyzed_lvalue = LValue::analyze(*this, expr, silent);
    if (!analyzed_lvalue)
        return Variable::dummy(expr.type->getBitstreamWidth());   // 失败占位
    if (!analyzed_lvalue->is_static()) {
        if (!silent)
            netlist.add_diag(diag::UnsupportedLhs, expr.sourceRange);
        return Variable::dummy(expr.type->getBitstreamWidth());
    }
    VariableBits ret = analyzed_lvalue->evaluate_vbits();
    log_assert(ret.bitwidth() == expr.type->getBitstreamWidth());
    return ret;
}
```

`evaluate_vbits` 的分派见 [src/lvalue.cc:L201-L224](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/lvalue.cc#L201-L224)。拼接分支的「逆序」值得注意——SystemVerilog 拼接 `{a, b}` 里 `a` 在高位，所以要从 `elements` 的**末尾**开始 append，才能让 `VariableBits` 的位序与位流一致：

```cpp
// src/lvalue.cc:207-213（节选）
} else if (auto concat = std::get_if<Concatenation>(&descriptor)) {
    VariableBits ret;
    ret.reserve(bitsize);
    auto &els = concat->elements;
    for (auto it = els.rbegin(); it != els.rend(); it++)   // 逆序！
        ret.append(it->evaluate_vbits());
    return ret;
}
```

范围选择分支委托给 `AddressingResolver::extract<VariableBits>`，它用 dummy 位填充「选择窗口落在变量外」的部分，见 [src/addressing.cc:L138-L154](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/addressing.cc#L138-L154)。

动态侧的衔接在 `assign_to_lvalue_with_masking`，它先尝试静态捷径，否则按 descriptor 递归，见 [src/procedural.cc:L354-L383](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L354-L383)：

```cpp
// src/procedural.cc:354-362（节选）—— 静态优先
void assign_to_lvalue_with_masking(...) {
    if (lvalue.is_static()) {
        context.update_variable_state(
            assign.sourceRange.start(), lvalue.evaluate_vbits(), rvalue, mask, blocking);
        return;
    }
    // 否则按 Concatenation / RangeSelect / MemberAccess / MemoryWrite 递归摊平
    ...
}
```

这条「静态优先、否则递归摊平」的分叉，正是 `LValue` 两个布尔字段（`static_`）存在的意义：让消费方一眼判断能否走廉价捷径。`MemoryWrite` 分支则在此处直接发射 `$memwr_v2` 单元，详见 [src/procedural.cc:L393-L432](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L393-L432)。

#### 4.4.4 代码实践

- **实践目标**：看清「静态 vs 动态」如何决定落地方式。
- **操作步骤**：在 [src/procedural.cc:L354-L437](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L354-L437) 中定位四个分支：静态捷径（358 行）、`Concatenation` 递归（364 行）、`RangeSelect` 递归（372 行）、`MemoryWrite` 发单元（393 行）。
- **观察现象**：静态捷径只调用 `update_variable_state`（写入 case 树的变量状态），不建任何单元；而 `RangeSelect` 的动态分支会调用 `resolver.shift_up` / `resolver.demux`——这些才真正生成 mux/demux 电路。
- **预期结果**：理解「同样的 `a[i]`，常量 `i` 走捷径、变量 `i` 走电路」这条分叉的代码位置。
- 待本地验证：可对照 u4-l3（AddressingResolver）进一步看 `shift_up`/`demux` 的电路含义。

#### 4.4.5 小练习与答案

**练习 1**：`EvalContext::lhs` 在左值非静态时为什么返回 `Variable::dummy(...)` 而不是直接报错终止？
**答**：sv-elab 采用「先攒诊断、继续翻译」的策略（见 u2-l4）。返回 dummy 让调用方拿到一个宽度正确的占位变量继续推进，避免一处错误引发连锁崩溃；真正的错误已经通过 `UnsupportedLhs` 诊断上报，最终会在诊断检查阶段统一处理。

**练习 2**：`evaluate_vbits` 的 `RangeSelect` 分支调用 `resolver.extract<VariableBits>(...)`，为什么 `extract` 模板要特化 `VariableBits` 版本？
**答**：因为 `extract` 同时服务于右值（`RTLIL::SigSpec`，见 u4-l1）和左值（`VariableBits`）。两者「提取一段位」的语义相同，但底层数据结构不同：`SigSpec` 版本用 `Sx` 填充越界位，`VariableBits` 版本用 `Variable::dummy` 填充。模板特化让同一套寻址数学复用于两种表示。可对照 [src/addressing.cc:L138-L174](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/addressing.cc#L138-L174) 的两个特化。

---

## 5. 综合实践

**任务**：为一个「同时含拼接与成员访问」的左值画出 `LValue` 的 descriptor 树，并判断它是否静态、是否连续切片。

**素材**：测试 [tests/various/assignment_pattern_lhs.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/assignment_pattern_lhs.ys) 中有这样一条左值（嵌套 packed struct + 数组 + 拼接）：

```systemverilog
typedef struct packed { logic a; logic b; } foo_t;
typedef struct packed { foo_t foo; logic c; } bar_t;
...
always_comb
    '{'{'{ga1, gb1}, gc1}, '{'{ga2, gb2}, gc2}} = a_i;
```

> 说明：`'{...}` 是赋值模式（assignment pattern），sv-elab 会把它拆解成多条普通左值赋值（见 `assign_rvalue` 中的 `SimpleAssignmentPatternExpression` 处理）。拆解后的核心左值形态是嵌套拼接 `{ {...{ga1,gb1}}, gc1 }, ...`，其中每个 `ga1/gb1` 又可视为 `Variable`。

**步骤**：

1. 选其中一条拆解后的左值，例如外层拼接 `{X, Y}`（`X = '{'{ga1,gb1}, gc1}`，`Y` 同理）。
2. 按 4.2 的规则画出 descriptor 树：
   ```
   Concatenation
   ├─ element[1] (高位) = Concatenation{ Concatenation{ga1, gb1}, gc1 }
   └─ element[0] (低位) = Concatenation{ Concatenation{ga2, gb2}, gc2 }
   ```
3. 判定 `static_`：每个叶子都是 `Variable`（`static_=true`），拼接是子元素之 AND，所以整棵树 `static_=true`。
4. 判定 `contiguous_slice_`：最外层是 `Concatenation` → `false`（即便每个叶子是单变量，拼接跨多个变量，整体不是「单个变量的连续段」）。
5. 追踪落地：因为 `static_==true`，`assign_to_lvalue_with_masking` 走静态捷径，调用 `evaluate_vbits()` 逆序折叠成 `VariableBits`，再交给 `update_variable_state` 写入 case 树。

**验证方式**：运行该测试（`tests/various/assignment_pattern_lhs.ys` 用 `sat ... -prove-asserts` 做可满足性证明），观察断言 `a1 === ga1` 等是否全部通过，从而侧面确认左值拆解与位序正确。

> 待本地验证：本实践为「源码阅读 + 推理」型，实际运行需要先按 u8-l3 的方式构建 yosys-slang。若无法构建，至少应能独立完成第 2–4 步的画树与判定。

## 6. 本讲小结

- `LValue` 是 sv-elab 的「左值描述容器」：它**不直接建 RTLIL 单元**（存储器写除外），只把 slang 左值表达式翻译成一棵可递归的 descriptor 树。
- 五种 descriptor 覆盖了 SV 左值的全部可综合形态：`Variable`、`Concatenation`、`RangeSelect`、`MemberAccess`、`MemoryWrite`。
- `LValue::analyze` 用按 `expr.kind` 分派的大 switch 完成翻译；`ElementSelect` 在普通变量上退化为「单元素范围选择」，仅对推断存储器才走 `MemoryWrite`。
- 两个布尔字段 `static_` 与 `contiguous_slice_` 在工厂方法里按规则传播：`static_` 决定走静态捷径还是动态摊平。
- 静态左值通过 `evaluate_vbits()` 折叠成 u3-l3 的 `VariableBits`，由 `EvalContext::lhs` 对外暴露；动态左值由 `assign_to_lvalue_with_masking` 递归摊平成 mux/demux/`$memwr_v2` 电路。
- 访问器 `is_contiguous_slice()` 在当前代码树中**已声明但无定义、无调用**；底层数据字段 `contiguous_slice_` 由工厂维护。本讲只描述字段语义，不断言访问器有运行时效果。

## 7. 下一步学习建议

- 阅读 [u4-l3 AddressingResolver：动态位/数组寻址](u4-l3-addressing-resolver.md)，深入 `shift_up` / `demux` / `mux` / `extract` 是如何把动态 `RangeSelect` 左值变成真实寻址电路的。
- 回到 [u5-l1 ProceduralContext 与 VariableState](u5-l1-procedural-context.md)，看 `evaluate_vbits()` 产出的 `VariableBits` 是如何被 `update_variable_state` 写进过程块 case 树的——那是 LValue 静态捷径的真正终点。
- 想了解存储器写左值 `MemoryWrite` 的来龙去脉，可跳读 [u7-l1 存储器推断与初始化](u7-l1-memory-inference.md)，看 `is_inferred_memory` 如何决定 `a[i]` 走 `MemoryWrite` 而非普通 `RangeSelect`。
