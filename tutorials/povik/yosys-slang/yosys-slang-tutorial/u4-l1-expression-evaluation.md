# EvalContext：把 SystemVerilog 表达式求值为 RTLIL SigSpec

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `EvalContext` 在 sv-elab 中扮演的角色，以及它持有的几个关键状态（`netlist`、`procedural`、`const_`、`lvalue`）。
- 读懂 `EvalContext::operator()` 这条「中央分派」函数：它如何先用常量折叠捷径，再按 `expr.kind` 走 switch 把各类 slang 表达式翻译成 `RTLIL::SigSpec`。
- 对照源码说出一个二元运算（如 `a + b`）和一个位/元素选择（如 `data[i]`）分别会生成哪些 RTLIL 单元。
- 理解 `apply_conversion` / `apply_nested_conversion` 如何处理类型转换与符号扩展。
- 了解流拼接 `{<<n{...}}` / `{>>n{...}}` 在 `streaming()` 中如何被重新排列成位流。

本讲是单元 4「表达式求值与左值」的总纲，后续 u4-l2（LValue 分析）、u4-l3（AddressingResolver 动态寻址）会在本讲建立的「求值入口」之上继续下沉。

## 2. 前置知识

在进入本讲前，请确保你已经理解（来自 u3 单元）：

- **slang AST 与 RTLIL 的职责边界**：slang 把 SystemVerilog 解析成带类型的 AST（`ast::Expression` 及其子类），sv-elab 遍历这棵 AST，在 `RTLIL::Module` 画布上建线、建单元。`EvalContext` 正是「把一个 AST 表达式节点翻译成一段 `RTLIL::SigSpec`」的翻译器。
- **`RTLIL::SigSpec`**：Yosys 里「一段信号」的通用表示，可以是常量、若干 `SigBit`、一根线的若干位。本讲里 `operator()` 的返回值就是它。
- **`RTLILBuilder` 的「五步模式」与属性机制**（u3-l2）：组合单元几乎都遵循「常量折叠 → 特化优化 → `add_y_wire` 建输出线 → `canvas->addXxx` 建单元 → `bless_cell` 盖印属性」。其中 `bless_cell` 会把 `staged_attributes`（含 `src` 源码位置与用户 `(* attr *)`）盖到刚建的单元上。
- **Variable / VariableBits**（u3-l3）：过程块里的左值身份卡与「HDL 意图」位级抽象，由 `EvalContext::variable()` / `lhs()` 产出，本讲只在「读变量」时触及，细节不再重复。

几个本讲会用到的 slang 概念，先用一句话解释：

- **`ast::ExpressionKind`**：slang 给每个表达式节点打的「种类标签」，如 `BinaryOp`、`ElementSelect`、`Conversion`、`Streaming`、`IntegerLiteral`。`operator()` 的 switch 就按它分派。
- **`expr.eval(const_)`**：slang 自带的「编译期常量求值」。如果一个表达式全部由常量构成，slang 能直接算出它的 `ConstantValue`，sv-elab 据此短路掉单元生成。
- **bitstream width**：一个类型「摊平成位流」后的位数，是 sv-elab 与 RTLIL 对齐位宽的统一标尺。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/slang_frontend.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h) | `EvalContext` 结构体声明，列出所有求值入口方法 |
| [src/slang_frontend.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc) | `operator()`、`apply_conversion`、`apply_nested_conversion`、`streaming`、常量辅助函数、`TestSlangExprPass` 自测命令 |
| [src/builder.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc) | `RTLILBuilder::Biop/Unop/Mux/ReduceBool` 等组合单元封装（被 `operator()` 调用） |
| [src/addressing.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/addressing.cc) | `AddressingResolver`，处理 `ElementSelect`/`RangeSelect` 的静态切片与动态 mux 寻址 |
| [tests/various/expr.sv](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/expr.sv) + [expr.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/expr.ys) | 表达式自测：用 `$t(expr)` 把「slang 常量求值」与「RTLIL 单元求值」做逐项比对 |

## 4. 核心概念与源码讲解

### 4.1 EvalContext：表达式的求值上下文

#### 4.1.1 概念说明

`EvalContext` 是「一个表达式求值会话」的全部上下文。它本身不懂 SystemVerilog 语法，也不直接持有 RTLIL 画布；它把三样东西捏在一起：

1. **`netlist`（`NetlistContext&`）**：真正持有 `RTLIL::Module` 画布、线网缓存、诊断容器的中枢（u3-l1）。所有「建单元」「建线」「报诊断」最终都落到 `netlist.xxx()`。
2. **`procedural`（`ProceduralContext*`，可为空）**：如果当前在 `always`/`initial` 过程块里求值，它指向该过程块上下文；如果在连续赋值/实例连接等「非过程」场景求值，它为 `nullptr`。这个指针决定了「读变量」走哪条路（见 4.2）。
3. **`const_`（`ast::EvalContext`）**：slang 自己的编译期求值上下文，用于常量折叠捷径。

> 一句话：`EvalContext` = 「拿着 slang AST 节点 + 画笔（netlist）+ 可选的过程块状态（procedural），产出一段 `RTLIL::SigSpec`」的函数对象。

#### 4.1.2 核心流程

`EvalContext` 提供一组「求值入口」，最常用的是 `operator()`，其它入口各有专门用途：

```
operator()(expr)        → 通用求值，输出 RTLIL::SigSpec（本讲主角，4.2）
eval_signed(expr)       → 同上，但保证结果可当「有符号」解读（补一个 S0 符号位）
sva(expr)               → 按 SVA 规则求值（置 in_sva_expression 标志后调 operator()）
lhs(expr)               → 把左值表达式描述成 VariableBits（u3-l3，不在本讲展开）
variable(symbol)        → 为一个 ValueSymbol 构造 Variable 身份卡
apply_conversion(...)   → 处理一次类型转换（4.3）
apply_nested_conversion → 递归剥多层嵌套转换（4.3）
streaming(expr)         → 求值流拼接（4.4）
```

`EvalContext` 有两个构造函数，分别对应「非过程」与「过程块」两种使用场景：[slang_frontend.cc:L1684-L1694](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1684-L1694)。注意 `ProceduralContext` 内部自带一个 `EvalContext eval` 成员（见 u5-l1），所以过程块里的求值天然带着 `procedural` 指针；而非过程场景则由 `NetlistContext` 临时构造一个 `procedural == nullptr` 的 `EvalContext`。

#### 4.1.3 源码精读

`EvalContext` 的结构体声明：[slang_frontend.h:L143-L195](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L143-L195)。关键成员与含义：

```cpp
struct EvalContext {
    NetlistContext &netlist;        // 画笔与诊断中枢
    ProceduralContext *procedural;  // 非空=过程块内；空=连续赋值/连接

    ast::EvalContext const_;        // slang 编译期求值上下文（常量折叠用）
    const ast::Expression *lvalue = nullptr;  // 当前赋值的左值（供 LValueReference 用）

    // 函数重入时区分不同层级的 automatic 变量（u3-l3）
    Yosys::dict<const ast::Scope *, int> scope_nest_level;

    RTLIL::SigSpec operator()(ast::Expression const &expr);  // 中央分派
    // ...
    bool ignore_ast_constants = false;   // 测试用：强制走单元生成而非常量折叠
    bool in_sva_expression = false;      // 当前是否在 SVA 求值中
};
```

读变量时构造身份卡的 `variable()`：[slang_frontend.cc:L599-L607](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L599-L607)。

```cpp
Variable EvalContext::variable(const ast::ValueSymbol &symbol)
{
    if (ast::VariableSymbol::isKind(symbol.kind) &&
            symbol.as<ast::VariableSymbol>().lifetime == ast::VariableLifetime::Automatic) {
        return Variable::from_symbol(&symbol, find_nest_level(symbol.getParentScope()));
    } else {
        return Variable::from_symbol(&symbol);
    }
}
```

这段代码说明：automatic 生命周期（函数局部）的变量会被打上「所在作用域的重入层级」，从而让递归函数不同层级的同名局部变量互不混淆（这是 u3-l3 讲过的 `Variable::Local` + `depth`）；其余变量（静态、线网等）则不带层级。

`eval_signed` 是给「需要带符号位的地址/选择表达式」用的薄包装：[slang_frontend.cc:L1674-L1682](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1674-L1682)。它对「无符号数值类型」在最高位前补一个 `S0`，使结果总能被当作有符号数解读——`AddressingResolver` 在解析动态索引时会用到它。

#### 4.1.4 代码实践

**实践目标**：确认 `procedural` 指针如何区分两种求值场景。

**操作步骤**：

1. 在 [src/slang_frontend.cc:L1684-L1694](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1684-L1694) 找到两个构造函数，记下它们如何设置 `procedural`。
2. 用 `Grep` 搜索 `EvalContext(netlist`（不带 procedural 的构造）与 `EvalContext(procedural`（带 procedural 的构造）的调用点，观察分别出现在「连续赋值/实例连接」还是「过程块」代码里。

**需要观察的现象**：非过程构造点应出现在 `PopulateNetlist` 处理 `assign`、实例端口连接等位置；过程构造点应出现在 `ProceduralContext` 构造里。

**预期结果**：你会看到 `ProceduralContext` 持有一个 `EvalContext eval` 成员并用「过程版」构造初始化；而 `NetlistContext` 内部做连续赋值求值时用的是「非过程版」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `EvalContext` 要同时保留 slang 的 `const_` 而不是自己实现常量求值？

**参考答案**：因为 slang 已经在语义分析阶段实现了完整、符合 IEEE 1800 语义的编译期求值（含 `x`/`z`、符号、位宽截断等）。sv-elab 复用它做常量折叠捷径，既避免重复造轮子，又能保证「常量结果」与仿真语义一致。

**练习 2**：`ignore_ast_constants` 字段注释里写着「flag for testing」。结合 4.2 节，猜猜它被置 true 时会发生什么。

**参考答案**：它会让 `operator()` 的常量折叠捷径失效（字面量除外），从而**强制**走 switch 分支、真正生成 RTLIL 单元。`TestSlangExprPass` 正是借此把「slang 常量结果」与「RTLIL 单元结果」做对比（见 4.2.4 与第 5 节）。

---

### 4.2 operator()：核心分派与常量折叠捷径

#### 4.2.1 概念说明

`EvalContext::operator()` 是整个表达式翻译的「中央分派函数」。它的契约很简单：**给我一个 slang 表达式节点，还你一段位宽等于该表达式 bitstream width 的 `RTLIL::SigSpec`**。所有「在 RTLIL 里表示这个表达式值」所需的线与单元，都由它在 `netlist` 画布上隐式建好。

它有两个设计要点：

1. **常量折叠捷径**：能编译期算出来的，绝不建单元，直接返回常量 `SigSpec`。
2. **大 switch 分派**：折叠失败或本就含运行时信号时，按 `expr.kind` 逐类翻译。

#### 4.2.2 核心流程

```
operator()(expr)
  ├─ 0. AttributeGuard + transfer_attrs：把 src 源码位置与用户 (* attr *) 暂存，
  │     使本次求值生成的所有单元都自动带上这些属性（机制见 u3-l2）
  ├─ 1. 类型前置检查
  │     · untyped 类型         → unimplemented(expr)（如未解析的 interconnect）
  │     · Streaming            → 拒绝（流拼接另有 streaming() 入口，4.4）
  │     · 非定长且非 void      → 诊断 diag::FixedSizeRequired，返回全 Sx
  ├─ 2. 常量折叠捷径（默认开启）
  │     expr.eval(const_) 成功？→ convert_const → 返回常量 SigSpec（goto done）
  ├─ 3. switch(expr.kind)：逐类翻译
  │     NamedValue/HierarchicalValue → 读变量（procedural? substitute_rvalue : convert_static）
  │     UnaryOp    → Unop($neg/$not/$reduce_*/$logic_not...)
  │     BinaryOp   → Biop($add/$sub/$mul/$shl/$eq.../...)    ← 本讲重点
  │     Conversion → apply_conversion / streaming            ← 4.3 / 4.4
  │     IntegerLiteral → convert_svint
  │     RangeSelect  → AddressingResolver::shift_down
  │     ElementSelect → AddressingResolver::mux（或 $memrd_v2） ← 本讲重点
  │     Concatenation/Replication/AssignmentPattern → 逐段求值后逆序拼接
  │     ConditionalOp → Mux
  │     MemberAccess  → extract_struct_field
  │     Call → 系统任务($display/$clog2/$past...) 或函数调用
  │     ...        default → 诊断 diag::LangFeatureUnsupported
  └─ 4. 收尾
        error: 返回 RTLIL::SigSpec(Sx, bitstream_width)
        done:  断言 ret.size() == bitstream_width，返回 ret
```

注意「逆序拼接」是 RTLIL `SigSpec` 的特性：它按「低位在前」append，而 SystemVerilog 拼接 `{a, b}` 里 `a` 是高位，所以代码里把各段求值后**逆序** append。

#### 4.2.3 源码精读

**入口与前奏**（属性绑定 + 类型检查 + 常量折叠）：[slang_frontend.cc:L1205-L1240](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1205-L1240)。其中常量折叠捷径的关键片段：

```cpp
if (/* flag for testing */ !ignore_ast_constants ||
        expr.kind == ast::ExpressionKind::IntegerLiteral ||
        expr.kind == ast::ExpressionKind::RealLiteral ||
        /* ... 其它字面量 ... */) {
    auto const_result = expr.eval(this->const_);
    if (const_result) {
        auto converted = netlist.convert_const(const_result, expr.sourceRange.start());
        if (converted) { ret = *converted; goto done; }   // 短路：不建任何单元
        else { goto error; }
    }
}
```

常量结果由 `convert_const` → `convert_svint` 转成 RTLIL 常量：`convert_svint` 把 slang 的四态位（0/1/x/z）逐一映射成 `RTLIL::S0/S1/Sx/Sz`，见 [slang_frontend.cc:L220-L232](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L220-L232)；`convert_const` 再按 slang `ConstantValue` 的种类（integer/unpacked/string…）分发，遇到 real/queue/union 等不可综合类型则报 `diag::UnsupportedBitConversion`，见 [slang_frontend.cc:L234-L286](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L234-L286)。

整个 switch 主体：[slang_frontend.cc:L1242-L1662](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1242-L1662)。

**重点一：二元运算 `BinaryOp`**：[slang_frontend.cc:L1382-L1456](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1382-L1456)。它先递归求出左右操作数 `left`/`right`，再把 slang 的 `BinaryOperator` 映射成 RTLIL 单元类型，最后调 `netlist.Biop(...)`：

```cpp
const ast::BinaryExpression &biop = expr.as<ast::BinaryExpression>();
RTLIL::SigSpec left = (*this)(biop.left());
RTLIL::SigSpec right = (*this)(biop.right());

RTLIL::IdString type;
switch (biop.op) {
case ast::BinaryOperator::Add:      type = ID($add); break;
case ast::BinaryOperator::Subtract: type = ID($sub); break;
case ast::BinaryOperator::Multiply: type = ID($mul); break;
/* ... $and/$or/$xor/$eq/$ne/$lt/$shl/$pow ... */
default: ast_unreachable(biop);
}

// 移位的符号修正：$shl/$shr 的左操作数按无符号；所有移位的右操作数(移位量)按无符号
if (type.in(ID($shr), ID($shl)))   a_signed = false;
if (type.in(ID($shr), ID($shl), ID($sshr), ID($sshl))) b_signed = false;

ret = netlist.Biop(type, left, right, a_signed, b_signed, expr.type->getBitstreamWidth());
```

`netlist.Biop` 落到 `RTLILBuilder::Biop`：[builder.cc:L333-L431](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L333-L431)。它正是 u3-l2 讲过的「五步模式」：全常量→直接 `RTLIL::const_add(...)` 算出常量；否则做若干特化优化（比较的三值短路、`$logic_and/or` 短路、`$mul` 高位零缩窄），再 `add_y_wire` 建输出线、`canvas->addCell(op)` 建单元、设 `A/B/Y` 端口与 `A_WIDTH/B_WIDTH/A_SIGNED/B_SIGNED/Y_WIDTH` 参数、`bless_cell` 盖印属性。因此一个 `a + b` 在非常量情况下会生成一个 `$add` 单元：

```
$add 单元：  A = a,  B = b,  Y = <新建的中间线>
            A_SIGNED/B_SIGNED 由 a/b 是否有符号决定
            Y_WIDTH  = 表达式位宽
```

> 类比地，一元运算走 `Unop`（[builder.cc:L433-L460](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L433-L460)），三目运算符 `c ? a : b` 走 `Mux`（[builder.cc:L175-L186](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L175-L186)，`S==1 选 b`）。

**重点二：元素/位选择 `ElementSelect`**：[slang_frontend.cc:L1496-L1532](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1496-L1532)。分两条路：

```cpp
const ast::ElementSelectExpression &elemsel = expr.as<ast::ElementSelectExpression>();

if (netlist.is_inferred_memory(elemsel.value()) && !in_sva_expression) {
    // 被识别为存储器的数组 → 直接发一个读端口 $memrd_v2
    RTLIL::Cell *memrd = netlist.canvas->addCell(netlist.new_id(), ID($memrd_v2));
    memrd->setParam(ID::MEMID, id);
    /* ... ARST_VALUE/SRST_VALUE/INIT_VALUE 等 ... */
    memrd->setPort(ID::ADDR, (*this)(elemsel.selector()));
    ret = netlist.add_placeholder_signal(width);
    memrd->setPort(ID::DATA, ret);
    break;
}

// 普通向量/打包数组的位选择或元素选择 → 交给 AddressingResolver
AddressingResolver addr(*this, elemsel);
ret = addr.mux((*this)(elemsel.value()), elemsel.type->getBitstreamWidth());
```

`AddressingResolver::mux`（[addressing.cc:L277-L286](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/addressing.cc#L277-L286)）又分两路：

- **静态索引**（`raw_signal.is_fully_def()`，如 `data[2]`）：直接 `extract(val, output_len)`，**不建任何单元**，只是从 `val` 里截取对应位。
- **动态索引**（如 `data[i]`，`i` 是运行时信号）：调 `raw_mux`（[addressing.cc:L234-L275](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/addressing.cc#L234-L275)），用 `$bmux`（按地址选一行）+ `$lt`/`$ge`（判定越界，越界位输出 `x`）+ `$mux`（用地址最高位在正/负区间分支间选择）组合出选择电路。这是 u4-l3 的主题，本讲只点到为止。

范围选择 `data[hi:lo]` / `data[base +: w]` 走 `RangeSelect` 分支：[slang_frontend.cc:L1489-L1495](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1489-L1495)，调用 `AddressingResolver::shift_down`（[addressing.cc:L306-L331](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/addressing.cc#L306-L331)），静态时仍是 `extract`，动态时用 `$shiftx` 实现可变移位。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：为一个二元加法和一个动态位选择，分别写出 `EvalContext` 的求值路径与产生的 RTLIL 单元，再用项目自测命令验证。

**操作步骤**：

1. 阅读自测命令 `TestSlangExprPass`：[slang_frontend.cc:L3942-L4052](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3942-L4052)。对每个 `$t(expr)`，它做两次求值并比对（[slang_frontend.cc:L4026-L4027](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L4026-L4027)）：

   ```cpp
   SigSpec ref  = netlist.eval(*expr);          // slang 常量求值（参考答案）
   SigSpec test = amended_eval(*expr);          // amended_eval.ignore_ast_constants = true
   //                                            // → 强制 operator() 真正建单元
   ```

   两者相等则通过。也就是说：`ref` 是「应该是什么值」，`test` 是「RTLIL 单元算出来是什么值」。

2. **二元加法**：在 [tests/various/expr.sv:L46](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/expr.sv#L46) 有 `$t(-8'd1 + 1)`。写出它的求值路径：

   ```
   -8'd1 + 1   (BinaryOp, Add)
     ├─ left  = operator()(-8'd1)   → 走常量折叠捷径 → 常量 SigSpec(8'hff)
     ├─ right = operator()(1)       → 常量折叠 → 常量 SigSpec(32'd1)（默认整数）
     ├─ type = $add
     └─ netlist.Biop($add, left, right, a_signed, b_signed, Y_WIDTH)
   ```

   但注意：在 `test_slangexpr` 里 `ignore_ast_constants=true`，所以 `-8'd1` 与 `1` 仍会折叠（字面量豁免），而**加法本身**会走 `Biop` 的常量分支（两边都常量）直接算出 `8'h00`，**不建 `$add` 单元**。要让 `$add` 真正出现，需要操作数里含运行时信号——这正是下面综合实践（第 5 节）要你构造的场景。

3. **动态位选择**：参考 [tests/unit/bitsel.sv](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/bitsel.sv)，`wire o = data[sel];` 中 `sel` 是输入信号。写出求值路径：

   ```
   data[sel]   (ElementSelect)
     ├─ value = operator()(data) → 命中 NamedValue → convert_static(data) → SigSpec(全部位)
     ├─ 命中 is_inferred_memory? 否（普通 wire）
     └─ AddressingResolver addr(*this, elemsel);
        addr.mux(data, 1)
          └─ raw_signal 非常量 → raw_mux → $bmux + $ge/$lt + $mux
   ```

   预期产生的单元：一个 `$bmux`（按 `sel` 选出 `data` 的某一位）、一个 `$ge`/`$lt`（越界判定，决定是否输出 `x`）、一个 `$mux`（用 `sel` 符号位在正/负范围间选择）。

**需要观察的现象**：静态位选择 `data[2]` 应当**一个单元都不建**（仅 `extract`）；动态位选择 `data[sel]` 才会建上述 mux 链。

**预期结果**：`test_slangexpr expr.sv` 最终打印 `N tests passed.`（该测试是项目既有用例，预期通过）。

**运行方式**：在已编译好 `slang.so`（或内置版 Yosys）的环境里执行 `yosys -p "test_slangexpr tests/various/expr.sv"`，或通过 `tests/run.sh` 跑 CTest 中的 expr 用例。若你尚未构建，构建步骤见 u8-l3；构建命令的具体输出**待本地验证**。

> 说明：本实践是「源码阅读 + 路径预测」型，单元预测基于对 `Biop`/`AddressingResolver::mux` 的源码分析；真正运行前不要假设命令输出，亲手跑一次以核对。

#### 4.2.5 小练习与答案

**练习 1**：`operator()` 开头那句 `AttributeGuard guard(netlist); transfer_attrs(netlist, expr, guard);` 有什么用？如果删掉会怎样？

**参考答案**：它把当前表达式的源码位置 `src` 与用户写的 `(* attr *)` 属性暂存到 `staged_attributes`，使本次求值**递归过程中**新建的单元（经 `bless_cell`）自动带上这些属性（机制见 u3-l2）。删掉后，综合出的单元将丢失源码定位，下游 `show`/调试与属性传递都会受影响。

**练习 2**：为什么 `data[2]`（常量下标）几乎不生成单元，而 `data[i]`（变量下标）会生成一串 mux？

**参考答案**：常量下标时 `AddressingResolver::raw_signal` 是全定义常量，`mux`/`shift_down` 走 `extract` 早退路径，仅做编译期位截取；变量下标时地址在运行时才知道，必须用 `$bmux`/`$shiftx` 等电路「算出」选哪一位，并辅以越界判定。详见 u4-l3。

**练习 3**：`operator()` 末尾 `done:` 标签处的断言 `ret.size() == bitstream_width` 想保证什么？

**参考答案**：保证每条求值路径产出的 `SigSpec` 位宽都与表达式类型的 bitstream width 一致——这是 sv-elab 与 RTLIL 对齐位宽的不变式，违反通常意味着某条翻译分支有 bug。

---

### 4.3 apply_conversion / apply_nested_conversion：类型转换

#### 4.3.1 概念说明

SystemVerilog 里有大量隐式与显式类型转换：整型之间位宽/符号扩展、`type'(expr)` 强制转换、赋值与连接时的隐式调整。slang 把这些都表示成 `ConversionExpression` 节点。`operator()` 的 `Conversion` 分支并不自己处理转换逻辑，而是委托给 `apply_conversion`。

还有一个 `apply_nested_conversion`，专门处理「转换套转换」的链（常见于实例端口连接、模式赋值里），把一串嵌套 `Conversion` 一层层剥开逐个应用。

#### 4.3.2 核心流程

`Conversion` 分支（[slang_frontend.cc:L1457-L1482](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1457-L1482)）三种情况：

```
conv.isConstCast            → const 转换：内部按普通过程规则求值（临时关掉 SVA 标志）
operand 是 Streaming        → 调 streaming() 求位流，再用 S0 补齐到目标位宽（4.4）
其它（普通转换）            → apply_conversion(conv, operator()(operand))
```

`apply_conversion` 本体（[slang_frontend.cc:L783-L801](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L783-L801)）按「源类型 → 目标类型」分三类：

```
integral → integral : extend_u0(目标位宽, sign_extend)
             · 符号扩展与否取决于 conversionKind：
                 Propagated（隐式传递）→ 看目标类型 isSigned
                 其它（显式转换）       → 看源类型 isSigned
bitstream → bitstream : 要求位宽相等，原样返回
其它                  → unimplemented
```

整型扩展的数学含义：若 `sign_extend` 为真，高位用符号位填充；否则填 0。设源值为 \( v \)、目标位宽 \( W \)、源位宽 \( w \)，则

\[
\text{result}[i] =
\begin{cases}
v[i] & 0 \le i < w \\
v[w-1] & \text{sign\_extend} \land i \ge w \\
0 & \text{otherwise}
\end{cases}
\]

`RTLIL::SigSpec::extend_u0(W, sign_extend)` 正是这条语义。

`apply_nested_conversion`（[slang_frontend.cc:L803-L814](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L803-L814)）是递归剥壳：

```cpp
if (expr.kind == EmptyArgument)  return op;                 // 壳底
if (expr.kind == Conversion) {
    auto &conv = expr.as<ConversionExpression>();
    RTLIL::SigSpec value = apply_nested_conversion(conv.operand(), op);  // 先剥内层
    return apply_conversion(conv, value);                                // 再应用本层
}
```

它被 `connection_lhs`（[slang_frontend.cc:L699-L708](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L699-L708)）等用于「把一段已物化的连线 `link` 沿着右端嵌套转换语义对齐到左端」。

#### 4.3.3 源码精读

`apply_conversion` 关键片段：[slang_frontend.cc:L783-L801](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L783-L801)

```cpp
const ast::Type &from = conv.operand().type->getCanonicalType();
const ast::Type &to   = conv.type->getCanonicalType();

if (from.isIntegral() && to.isIntegral()) {
    bool sign_extend = (conv.conversionKind == ast::ConversionKind::Propagated)
                            ? to.isSigned() : from.isSigned();
    op.extend_u0((int) to.getBitWidth(), sign_extend);
    return op;
} else if (from.isBitstreamType() && to.isBitstreamType()) {
    require(conv, from.getBitstreamWidth() == to.getBitstreamWidth());
    return op;
} else {
    unimplemented(conv);
}
```

一个直接例子来自 [tests/various/expr.sv:L48](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/expr.sv#L48)：`8'shff + $unsigned(1)`。`8'shff` 是 8 位有符号 `-1`，`$unsigned(1)` 通过系统任务 `$unsigned`（在 `Call` 分支里直接返回操作数、不改位，见 [slang_frontend.cc:L1624-L1626](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1624-L1626)）变为无符号。两者相加时，slang 会插入 `Conversion` 节点统一位宽与符号，最终交给 `apply_conversion` 做扩展，再交给 `$add`。

> 留意 `Propagated` 与非 `Propagated` 的区别：隐式传递的转换看**目标**符号（因为语境决定语义），显式 `type'(x)` 看**源**符号。这避免了「显式转换被语境悄悄改了符号」的意外。

#### 4.3.4 代码实践

**实践目标**：观察一次有符号扩展转换产生的位变化。

**操作步骤**：

1. 在 [tests/various/expr.sv:L49](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/expr.sv#L49) 找到 `$t((8'd5 - 8'd8) + 16'd1)`。`(8'd5 - 8'd8)` 是无符号 8 位减法得 `8'hfd`，再与 16 位 `1` 相加前会被转换成 16 位。
2. 手算：无符号 `8'hfd`（=253）扩展到 16 位是 `16'h00fd`；加 `16'd1` 得 `16'h00fe`。
3. 把它与一个「有符号源」对照：`$t(8'shff + 16'd1)` 中 `8'shff`（=-1）按符号扩展到 16 位是 `16'hffff`，加 1 得 `16'h0000`。

**需要观察的现象**：同样是「8 位全 1」的源，按无符号扩展（高位补 0）与按有符号扩展（高位补 1）结果不同。

**预期结果**：上述两条都能被 `test_slangexpr` 验证通过（既有用例）。手算与自测结果应当一致；若不一致，先检查你对 `apply_conversion` 里 `sign_extend` 取源/目标符号的判断是否正确。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `apply_conversion` 对 integral→integral 直接 `extend_u0`，而不建 `$pos` 之类的单元？

**参考答案**：因为「位宽/符号扩展」在 RTLIL 里只是信号位的复制/补零/补符号位，属于纯连线，`extend_u0` 直接改写 `SigSpec` 即可，无需任何单元。仅当真正改变值（如求反、求补）才需要单元（走 `Unop($pos/$neg)`）。

**练习 2**：`apply_nested_conversion` 为什么写成递归，而不是循环？

**参考答案**：因为嵌套 `Conversion` 是树状的（每层 `conv.operand()` 可能又是 `Conversion`），递归天然贴合 AST 结构：先递归剥到最内层（`EmptyArgument` 壳底），再逐层返回时应用每一层的 `apply_conversion`。逻辑清晰且不易错。

---

### 4.4 streaming：流拼接求值

#### 4.4.1 概念说明

SystemVerilog 的流拼接运算符 `{<<n{expr}}`（反向）与 `{>>n{expr}}`（正向）把操作数当成「位流」，按切片大小 `n` 重新排列，常用于串行化/反串行化（如把一个字节的位序反转）。它本质是「位级别的重新排列」，不涉及运算，所以 sv-elab 不需要算术单元，只需把求值出的 `SigSpec` 按规则重排。

`operator()` 显式拒绝 `Streaming` 种类（[slang_frontend.cc:L1217](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1217)），因为流拼接必须由专门的入口 `EvalContext::streaming()` 处理，并且它通常出现在 `Conversion` 里（如 `byte_t'({<<1{8'hd6}})`）。

#### 4.4.2 核心流程

`streaming()` 的处理分两步（[slang_frontend.cc:L741-L781](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L741-L781)）：

```
1. 组装「原始位流」cat：
   for each stream in expr.streams():
       item = (stream 是嵌套 Streaming) ? streaming(子流) : operator()(stream.operand)
       收集到 parts[]
   逆序 append parts → cat      // 同 Concatenation：RTLIL 低位在前，SV 左操作数是高位

2. 按切片大小 slice 重排：
   if slice == 0:  return cat   // 不切分，整段作为一个块（{<<0{...}} 罕见）
   else: 把 cat 按 slice 位切成多段，段间顺序反转后重新拼接
```

> 关键直觉：`{<<n{...}}` 与 `{>>n{...}}` 的区别在 slang 层已经体现为「切片大小」与「流的走向」。对 sv-elab 而言，落到 `streaming()` 时只剩一件事——把位流按 `slice` 切块再按 SV 顺序排好。`{<<1{8'hd6}}`（位反转）等价于 slice=1 的逐位反转；`{>>4{...}}` 等价于按 4 位一组正向重组。

`Conversion` 分支对流操作数的处理（[slang_frontend.cc:L1467-L1480](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1467-L1480)）：先 `streaming()` 求出位流，若位流短于目标类型，用 `S0` 在高位补齐。

另有左值侧的 `streaming_lhs()`（[slang_frontend.cc:L711-L739](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L711-L739)）：与 `streaming()` 同构，但产出的是 `VariableBits`（HDL 意图左值），用于 `{<<n{lhs}} = rhs` 这类流赋值。本讲只了解它存在即可。

#### 4.4.3 源码精读

`streaming` 重排核心：[slang_frontend.cc:L741-L781](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L741-L781)

```cpp
RTLIL::SigSpec EvalContext::streaming(ast::StreamingConcatenationExpression const &expr)
{
    require(expr, expr.isFixedSize());
    RTLIL::SigSpec cat;
    std::vector<RTLIL::SigSpec> parts;
    auto streams = expr.streams();
    for (auto stream : streams) {
        require(*stream.operand, !stream.withExpr);   // 不支持 with 子句
        auto& op = *stream.operand;
        RTLIL::SigSpec item;
        if (op.kind == ast::ExpressionKind::Streaming)
            item = streaming(op.as<ast::StreamingConcatenationExpression>()); // 嵌套递归
        else
            item = (*this)(*stream.operand);
        parts.push_back(item);
    }
    // SigSpec 低位在前；流按源序求值，故逆序 append 保持 SV 顺序
    for (auto part_it = parts.rbegin(); part_it != parts.rend(); ++part_it)
        cat.append(*part_it);

    int slice = expr.getSliceSize();
    if (slice == 0) {
        return cat;
    } else {
        RTLIL::SigSpec reorder;
        std::vector<RTLIL::SigSpec> slices;
        for (int i = 0; i < cat.size(); i += slice)
            slices.push_back(cat.extract(i, std::min(slice, cat.size() - i)));
        for (auto part_it = slices.rbegin(); part_it != slices.rend(); ++part_it)
            reorder.append(*part_it);           // 段顺序反转
        return reorder;
    }
}
```

一个完整例子贯穿「求值 + 转换 + 补零」：[tests/various/expr.sv:L107](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/expr.sv#L107) 的 `$t(byte_t'({<<1{8'hd6}}))`。

- `8'hd6` = 二进制 `11010110`。
- `{<<1{...}}`：slice=1，逐位反转 → `01101011` = `8'h6b`。
- 外层 `byte_t'(...)` 是 integral→integral 转换，位宽相同，原样返回。

嵌套流的例子：[tests/various/expr.sv:L158](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/expr.sv#L158) `$t(byte_t'({>>3{4'h6, {>>2{4'h7}}}}))`，演示了 `streaming()` 对嵌套 `Streaming` 操作数的递归处理。

#### 4.4.4 代码实践

**实践目标**：手算一个流反转，与 `test_slangexpr` 的结果对照。

**操作步骤**：

1. 取 [tests/various/expr.sv:L107-L114](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/expr.sv#L107-L114) 的几条流用例，如 `$t(byte_t'({<<2{8'hd6}}))`。
2. 手算：`8'hd6 = 1101_0110`。slice=2，切成 4 段（每段 2 位）：`11 01 01 10`；段顺序反转：`10 01 01 11` → `1001_0111` = `8'h97`。
3. 运行 `yosys -p "test_slangexpr tests/various/expr.sv"`，确认这一条与其它流用例一起通过。

**需要观察的现象**：slice 越大，重排越「粗」；slice=1 是逐位反转。

**预期结果**：手算的 `8'h97` 与自测中 `ref`（slang 常量求值）一致；`test`（经 `streaming()` 重排得到的 RTLIL 信号）也一致，故该 `$t` 通过。运行命令的具体打印**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`streaming()` 里为什么先求各段、再「逆序 append」？

**参考答案**：因为 `RTLIL::SigSpec::append` 是「低位在前」，而 SystemVerilog 流拼接 `{a, b}` 中 `a` 在高位。求值时按源序（`a`、`b`）收集到 `parts`，组装时逆序 append 才能让 `a` 落在高 位、`b` 落在低位，与 SV 语义一致。这与 `Concatenation` 分支的处理同理。

**练习 2**：`operator()` 为什么在最前面就 `ast_invariant(..., expr.kind != Streaming)` 拒绝流拼接？

**参考答案**：因为流拼接需要「先求位流再按切片重排」的特殊两阶段处理，不能简单递归当成普通表达式；它必须经 `Conversion` 分支或显式 `streaming()` 入口进入。提前拒绝是为了防止某条路径误把 `Streaming` 当普通表达式递归求值。

---

## 5. 综合实践

把本讲四条主线串起来：**自己写一个含「运行时信号」的二元加法和一个动态位选择，让 `operator()` 真正建出 `$add` 与 mux 单元，再用等价性思路验证。**

因为 `test_slangexpr` 的 `$t` 要求表达式可被 slang 完全常量求值（不能用 net/static 变量，但**可以用 automatic 局部变量与函数参数**，见 [expr.sv:L7-L9](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/expr.sv#L7-L9) 的注释），所以我们可以借助「函数 + automatic 变量」造出含运行时信号的表达式。

**任务**：复制 `tests/various/expr.sv` 为 `my_expr.sv`，在 `module top` 内新增两个函数：

```systemverilog
// 例 1：二元加法 —— a 是 automatic 变量，非常量，逼出 $add
function automatic [7:0] add_one(input logic [7:0] a);
    add_one = a + 8'd1;
endfunction

// 例 2：动态位选择 —— i 是 automatic 变量，逼出 mux 寻址链
function automatic logic pick(input logic [7:0] data, input logic [2:0] i);
    pick = data[i];
endfunction

initial begin
    $t(add_one(8'd5));
    $t(pick(8'b1011_0110, 3'd4));
end
```

**要求**：

1. **路径预测**（写在本子上即可）：
   - `add_one(8'd5)`：`a + 8'd1` → `BinaryOp(Add)` → 左操作数 `a`（automatic，非常量）→ 走 `Biop($add, ...)`，应生成一个 `$add` 单元。求值后 slang 常量侧得 `8'd6`。
   - `pick(..., 3'd4)`：`data[i]` → `ElementSelect` → `AddressingResolver::mux`，因 `i` 是运行时值（但 `test_slangexpr` 里 `i=4` 会被求成常量…注意：`ignore_ast_constants` 只影响是否短路，`pick` 内 `i` 是参数，slang 内联后仍可能折叠）。请预测：若 `i` 折叠成常量 `4`，则 `mux` 走静态 `extract`，**不建 mux**，结果为 `data[4]=1`。
2. **对比**：把 `pick` 的 `i` 改成由更复杂、slang 无法折叠的式子驱动（例如经函数返回值），观察 `AddressingResolver` 是否仍走静态路径。这一步的判定依据是阅读 `raw_signal.is_fully_def()` 的来源（[addressing.cc:L351-L354](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/addressing.cc#L351-L354)）。
3. **运行验证**：`yosys -p "test_slangexpr my_expr.sv"`，确认所有 `$t` 通过（即 `ref == test`）。

**预期结果**：`add_one` 用例验证 `$add` 单元的求值与 slang 常量一致；`pick` 用例在静态下标时验证 `extract` 正确。运行输出**待本地验证**。

> 进阶：若想真正「看到」生成的 `$add` 单元，可改用一个普通模块端口（`input logic [7:0] a; wire [7:0] y = a + 8'd1;`），用 `read_slang` 读入后 `show` 或 `dump` 网表，观察 `$add` 单元的 `A/B/Y` 端口与 `A_SIGNED` 等参数。这属于 u8-l1 讲的「网表检视」实践。

## 6. 本讲小结

- `EvalContext` 是「把一个 slang AST 表达式翻译成 `RTLIL::SigSpec`」的函数对象，核心状态是 `netlist`（画笔）、`procedural`（是否过程块内）、`const_`（slang 常量求值）。
- `operator()` 是中央分派：先用 `AttributeGuard` 绑定源码属性，再用 `expr.eval(const_)` 做常量折叠捷径，失败后按 `expr.kind` 走大 switch 翻译，末尾断言位宽一致。
- 二元运算经 `BinaryOp` 分支映射到 `$add/$sub/$mul/$shl/...` 单元，由 `RTLILBuilder::Biop` 按「五步模式」生成；元素/位选择经 `ElementSelect` 交给 `AddressingResolver`，静态下标仅 `extract`、动态下标才建 `$bmux`/`$shiftx` 等。
- 类型转换由 `apply_conversion` 处理（integral 间用 `extend_u0` 做符号/零扩展；`Propagated` 看目标符号、显式转换看源符号），嵌套转换由 `apply_nested_conversion` 递归剥壳。
- 流拼接由 `streaming()` 处理：先组装位流、逆序 append，再按切片大小重排段顺序；`operator()` 显式拒绝 `Streaming` 种类，强制走该入口。
- `TestSlangExprPass`（`test_slangexpr`）把「slang 常量求值（参考）」与「`ignore_ast_constants=true` 的单元求值（受测）」逐项比对，是验证 `operator()` 正确性的关键自测。

## 7. 下一步学习建议

- **u4-l2 LValue：左值的结构化分析**：本讲的 `operator()` 求的是「右值」，左值侧（含拼接、范围选择、成员访问、存储器写）由 `LValue::analyze` 与 `EvalContext::lhs()` 负责，建议接着学。
- **u4-l3 AddressingResolver：动态位/数组寻址**：本讲只点到 `mux`/`shift_down`，动态寻址的 `$bmux`/`$shiftx`/demux/shift_up 电路细节是下一讲的全部内容。
- **想立刻动手验证**：先跳到 **u8-l1 测试体系**，学会构建 `slang.so` 并跑 `test_slangexpr` 与 `tests/various/expr.ys`，再回来做本讲的实践，体验会更完整。
- **延伸阅读源码**：`operator()` 的 `Call` 分支（[slang_frontend.cc:L1603-L1654](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1603-L1654)）展示了 `$clog2`/`$countones`/`$past` 等系统任务如何映射成 RTLIL，以及函数调用如何委托给 `StatementExecutor`，这是通向 u5 单元（过程块建模）的桥梁。
