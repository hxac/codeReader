# RTLILBuilder：RTLIL 单元动物园

## 1. 本讲目标

上一讲（u3-l1）我们认识了 `NetlistContext` 这个「中枢对象」，知道它多重继承了 `RTLILBuilder` 和 `DiagnosticIssuer`，并且持有一块叫 `canvas` 的 `RTLIL::Module` 画布。本讲就把放大镜对准 `RTLILBuilder` 这一侧，拆开看它究竟是怎样一笔一笔把电路「画」到画布上的。

学完本讲你应当能够：

- 说清 `RTLILBuilder` 在 sv-elab 里扮演的「画笔」角色，以及它反复使用的「常量折叠 → 建输出线 → 建单元 → 盖印属性」五步模式。
- 读懂 `Mux`、`Bwmux`、`Biop`、`Unop` 等组合单元封装方法，并指出它们各自创建的 RTLIL 单元（`$mux`/`$bwmux`/`$add`/`$neg`…）。
- 读懂 `add_dff`、`add_dffe`、`add_aldff`、`add_aldffe`、`add_dual_edge_aldff` 等时序单元方法，理解它们的「降级」回退逻辑。
- 解释 `bless_cell`、`staged_attributes` 与 `AttributeGuard` 三者构成的「属性暂存」机制——为什么属性的传递要做成 RAII 栈式作用域。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**第一，什么是 RTLIL 单元（cell）。** Yosys 内部用一种叫 RTLIL 的中间表示来描述电路。一个模块（`RTLIL::Module`）里有两类东西：线（`Wire`/`SigSpec`）和单元（`Cell`）。一个单元就像一个元器件符号，例如：

- `$add`：加法器，端口 `A`、`B` 为输入，`Y` 为输出，参数 `A_WIDTH`/`B_WIDTH`/`Y_WIDTH`/`A_SIGNED`/`B_SIGNED` 描述位宽与符号性。
- `$mux`：二选一多路器，`Y = S ? B : A`。
- `$dff`：D 触发器，`CLK` 为时钟，`D` 为输入，`Q` 为输出，参数 `CLK_POLARITY`/`WIDTH`。
- `$dffe`：带使能的 D 触发器，比 `$dff` 多一个 `EN` 端口和 `EN_POLARITY` 参数。
- `$aldff`/`$aldffe`：带异步加载（asynchronous load）的触发器，多出 `ALOAD`/`AD` 端口。

「画电路」本质上就是：在 `Module` 画布上 `addWire` 建线、`addCell` 建单元、`setPort`/`setParam` 连端口与设参数。

**第二，什么是常量折叠（constant folding）。** 如果一个单元的所有输入都是编译期常量（`is_fully_const()`），那就没必要真的生成一个硬件单元——直接在编译期把结果算出来返回一个常量 `SigSpec` 即可。这是 `RTLILBuilder` 几乎每个方法开头都有的「捷径」，它能让最终网表更精简。

**第三，什么是 RAII 与 staged（暂存的）属性。** C++ 的 RAII 指用对象的构造/析构来管理资源。sv-elab 生成一个单元时，往往希望把源码位置（`src` 属性）和用户写的 `(* attr=value *)` 一并挂到这个单元上。但生成单元的代码分布在很多层调用里，逐层透传属性参数太啰嗦。于是 `RTLILBuilder` 在自身存了一个「暂存属性表」`staged_attributes`，配合 `AttributeGuard` 这个栈对象：进入一段作用域时保存当前暂存表、允许往里塞属性、离开时自动恢复。真正建单元时，`bless_cell` 把当前暂存表一次性盖到单元上。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [src/builder.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc) | `RTLILBuilder` 所有方法的实现，是本讲的主战场。 |
| [src/slang_frontend.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h) | `RTLILBuilder` 结构体声明、`AttributeGuard` 类声明、`NetlistContext` 的多重继承关系。 |
| [src/slang_frontend.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc) | 调用方：`transfer_attrs` 怎么往 `AttributeGuard` 里塞属性，以及 `handle_ff_process` 怎么调用 `add_dffe`/`add_aldffe` 发射触发器。 |
| [tests/unit/dff.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/dff.ys) | 一个真实等价性测试，用 `q <= d + 1` 同时产生 `$add` 与 `$dffe`，是本讲实践的参照样本。 |

> 提醒：本项目没有独立的 `builder.h`，`RTLILBuilder` 的声明就放在 `src/slang_frontend.h` 里。

## 4. 核心概念与源码讲解

### 4.1 RTLILBuilder：把 HDL 意图落到 RTLIL 画布的「画笔」

#### 4.1.1 概念说明

`RTLILBuilder` 是一个纯「输出端」抽象：它不懂 SystemVerilog，只懂怎样在 `RTLIL::Module` 画布上画线、画单元。它对外暴露一组语义化方法（`Mux`、`Biop`、`add_dffe`…），对内调用 Yosys 的 `Module::addMux`/`addCell` 等底层 API。

它的状态非常少，核心就三件套：

```cpp
struct RTLILBuilder {
    using SigSpec = RTLIL::SigSpec;
    RTLIL::Module *canvas;                                   // 画布：当前正在画的模块
    Yosys::dict<RTLIL::IdString, RTLIL::Const> staged_attributes; // 暂存属性表
    slang::SourceRange staged_source_range;                  // 暂存源码位置
    bool staged_source_range_valid = false;
    unsigned next_id = 0;                                    // 自动命名计数器
    ...
};
```

参见 [src/slang_frontend.h:359-369](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L359-L369)。`canvas` 指向当前模块；`staged_attributes` 与 `staged_source_range` 服务于 4.4 节的属性机制；`next_id` 用来给匿名单元/线自动起名。

上一讲提过 `NetlistContext` 多重继承自 `RTLILBuilder`，参见 [src/slang_frontend.h:537](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L537)：

```cpp
struct NetlistContext : RTLILBuilder, public DiagnosticIssuer {
```

所以代码里到处可见 `netlist.Mux(...)`、`netlist.add_dffe(...)`——`netlist` 本身「就是」一个 builder。

#### 4.1.2 核心流程：反复出现的「五步模式」

`RTLILBuilder` 里绝大多数组合方法都遵循同一个套路：

1. **常量折叠捷径**：若输入全是常量，直接调 `RTLIL::const_xxx(...)` 算出常量结果并 `return`，不建任何单元。
2. **特化优化**：针对一些平凡情形（如选择信号恒为 0/1）直接返回某个输入，省掉一个单元。
3. **建输出线**：调 `add_y_wire(width)` 申请一根匿名输出线。
4. **建单元**：调 `canvas->addXxx(...)`（或 `addCell` + `setPort`/`setParam`）创建单元并连端口。
5. **盖印属性**：调 `bless_cell(cell)` 把暂存属性与源码位置挂到单元上，返回输出线。

`add_y_wire` 与 `new_id` 是其中最底层的两个工具：

```cpp
std::string RTLILBuilder::new_id(std::string base) {
    if (base.empty())
        return std::string("$") + std::to_string(next_id++);
    else
        return std::string("$") + base + "$" + std::to_string(next_id++);
}

std::pair<std::string, SigSpec> RTLILBuilder::add_y_wire(int width) {
    std::string id = new_id();
    return {id, canvas->addWire(id + "y", width)};
}
```

参见 [src/builder.cc:38-50](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L38-L50)。注意命名约定：匿名对象一律以 `$` 开头（Yosys 里 `$` 前缀表示自动生成的、非用户可见的名字），输出线名是「`$id`y」。

#### 4.1.3 源码精读：以 `Mux` 为模板

`Mux` 是五步模式最干净的范例：

```cpp
SigSpec RTLILBuilder::Mux(SigSpec a, SigSpec b, SigSpec s) {
    log_assert(a.size() == b.size());
    log_assert(s.size() == 1);
    if (s[0] == RTLIL::S0)       // 特化：选择信号恒为 0 → 取 a
        return a;
    if (s[0] == RTLIL::S1)      // 特化：选择信号恒为 1 → 取 b
        return b;
    auto [id, y] = add_y_wire(a.size());        // 步骤 3：建输出线
    bless_cell(canvas->addMux(id, a, b, s, y)); // 步骤 4+5：建 $mux 并盖印
    return y;
}
```

参见 [src/builder.cc:175-186](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L175-L186)。这里没有常量折叠分支（因为只要 `s` 是常量，下面的特化分支就已经处理了），其余步骤一目了然。

语义要点：`Mux(a, b, s)` 的返回值是「`s` 为真时取 `b`、为假时取 `a`」，即 \(\,Y = S\,?\,B : A\,\)。这与 Yosys `$mux` 单元的定义 `Y = S ? B : A` 完全一致——`addMux(id, a, b, s, y)` 把 `a` 接到单元的 `A` 端口、`b` 接到 `B` 端口。所以当你看到调用 `netlist.Mux(a, b, s)`，就要在脑海里把它翻译成 RTLIL 单元 `$mux`，端口 `A=a, B=b, S=s, Y=输出`。

#### 4.1.4 代码实践：第一次动手看单元

**实践目标**：确认 `Mux` 真的会生成一个 `$mux` 单元，并看清它的端口连接。

**操作步骤**：

1. 在任意目录新建 `mux_demo.sv`：

```systemverilog
module mux_demo(input logic sel,
                input logic [3:0] a, b,
                output logic [3:0] y);
    assign y = sel ? b : a;
endmodule
```

2. 新建 `run.ys`：

```tcl
read_slang mux_demo.sv
write_rtlil
```

3. 用加载了插件的方式运行（README 介绍：内置版本用 `read_slang` 直接可用，老版本用 `yosys -m slang` 加载插件，参见 [README.md:64-76](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/README.md#L64-L76)）：

```bash
yosys run.ys
```

**需要观察的现象**：标准输出里会打印一段 RTLIL 文本，其中含一个 `cell $mux` 块。

**预期结果**：类似下面的结构（具体 `$` 编号会变）——

```
cell $mux $1
  parameter \WIDTH 4
  connect \A \a
  connect \B \b
  connect \S \sel
  connect \Y \y
end
```

注意 `\A` 接的是 `a`（选择信号为假时的分支），`\B` 接的是 `b`（为真时的分支），正好对应 `sel ? b : a` 与 `Mux(a, b, sel)` 的调用顺序。

> 如果你的 Yosys 没有内置 sv-elab、又没装插件，这一步会报 `read_slang` 找不到。可改为直接阅读本节引用的源码作为「源码阅读型实践」。

#### 4.1.5 小练习与答案

**练习 1**：`Mux` 为什么不需要像 `Biop` 那样单独写一段「输入全常量」的常量折叠分支？

**参考答案**：因为只要选择信号 `s` 是常量，`s[0] == RTLIL::S0` / `S1` 两个特化分支就会直接返回 `a` 或 `b`；而 `a`/`b` 本身若也是常量，返回的就是常量 `SigSpec`，等价于折叠。所以特化分支已经覆盖了常量情形，无需重复。

**练习 2**：`add_y_wire` 返回的输出线名字形如 `$3y`，为什么以 `$` 开头？

**参考答案**：Yosys 约定 `$` 前缀的名字是工具自动生成的内部名，不会与用户源码里的标识符（用 `\` 转义的前缀）冲突，也便于下游识别「这是中间信号」。

---

### 4.2 组合单元的封装：Mux / Bwmux / Biop / Unop

#### 4.2.1 概念说明

组合单元指没有时钟、没有记忆、输出由当前输入完全决定的单元。sv-elab 把它们分成几类封装：

- **单比特选择类**：`Mux`（二选一，单个选择位）、`Bwmux`（按位选择，每个位各自有一个选择位）。
- **二元运算类**：`Biop`（Binary Operation）统一处理加减乘除、按位与或异或、比较、移位等所有 `$a OP b` 形态。
- **一元运算类**：`Unop`（Unary Operation）处理取正、取负、逻辑非、按位非、各种 reduce（归约）运算。
- **其它专用类**：`Bmux`（多路选择，多位选择信号）、`Shift`/`Shiftx`（移位）、`Le/Ge/Lt/Eq`（比较，单独封装以便走三值逻辑）、`Not`/`Neg`、`CountOnes`/`Clog2` 等。

之所以要封装，是因为直接调 Yosys 的 `addCell`+`setPort`+`setParam` 很啰嗦，而且每个单元都要重复「常量折叠 + 命名 + 盖印属性」的逻辑。封装后，上层（`EvalContext`、`AddressingResolver` 等）只需一句 `netlist.Biop(ID($add), a, b, ...)` 就能拿到结果信号。

#### 4.2.2 核心流程

**`Bwmux`（按位多路器）**：和 `Mux` 不同，它的选择信号 `s` 与数据等宽，每一位独立选择。语义是对每个 bit \(i\)：

\[
Y_i = S_i\,?\,B_i : A_i
\]

当 `s` 全常量时，逐位拼出一个常量结果直接返回；否则建 `$bwmux` 单元。这个「逐位常量拼接」也是一种折叠。`Bwmux` 在过程块里很常用——部分位赋值时，未赋值的位要保留原值，就用按位选择把「新值」和「背景值」混合（4.3 节会看到 `procedural.cc` 里的 `Bwmux(rvalue_background, unmasked_rvalue, mask)`）。

**`Biop`（二元运算）**：分三段处理。

1. **常量折叠宏**：用一个 `OP(type)` 宏批量展开，若 `a`、`b` 都全常量，则对每种运算符 `type` 调 `RTLIL::const_##type(...)` 直接算出常量。
2. **三值比较优化**：对 `$le/$lt/$gt/$ge` 四个比较，用三值逻辑（0/1/x）逐位推carry，能在含 `x` 的输入下给出更精确的常位结果。
3. **建单元**：对剩余情形，建一个类型为 `op`（如 `$add`）的通用单元，设 `A/B/Y` 端口与五个位宽/符号参数。

**`Unop`（一元运算）**：结构与 `Biop` 几乎对称——常量折叠宏 + 建单元，只是只有一个输入 `A`。

#### 4.2.3 源码精读

先看 `Bwmux` 的常量折叠与建单元：

```cpp
SigSpec RTLILBuilder::Bwmux(SigSpec a, SigSpec b, SigSpec s) {
    log_assert(a.size() == b.size());
    log_assert(a.size() == s.size());
    if (s.is_fully_const()) {                       // 折叠：逐位选择拼常量
        SigSpec result(RTLIL::Sx, a.size());
        for (int i = 0; i < a.size(); i++) {
            if (s[i] == RTLIL::S0)      result[i] = a[i];
            else if (s[i] == RTLIL::S1) result[i] = b[i];
        }
        return result;
    }
    auto [id, y] = add_y_wire(a.size());
    bless_cell(canvas->addBwmux(id, a, b, s, y));   // 建 $bwmux
    return y;
}
```

参见 [src/builder.cc:188-205](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L188-L205)。

再看 `Biop` 的常量折叠宏段（一段宏展开二十多种运算符的折叠）：

```cpp
if (a.is_fully_const() && b.is_fully_const()) {
#define OP(type) \
    if (op == ID($##type)) \
        return RTLIL::const_##type(a.as_const(), b.as_const(), a_signed, b_signed, y_width);
    OP(add) OP(sub) OP(mul) OP(divfloor) OP(div) OP(mod)
    OP(and) OP(or) OP(xor) OP(xnor)
    OP(eq) OP(ne) OP(nex) OP(eqx) OP(ge) OP(gt) OP(le) OP(lt)
    OP(logic_and) OP(logic_or)
    OP(sshl) OP(sshr) OP(shl) OP(shr) OP(pow)
#undef OP
}
```

参见 [src/builder.cc:336-366](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L336-L366)。`ID($##type)` 是 Yosys 的宏，把 `add` 拼成 `ID($add)`；`RTLIL::const_##type` 则拼成 `RTLIL::const_add`。这是一种很常见的「用宏批量生成同类分支」的技巧，避免手写二十多个 `if`。

`Biop` 末尾才真正建单元（这是五步模式的第 4、5 步）：

```cpp
auto [id, y] = add_y_wire(y_width - msb_zeroes);
Cell *cell = canvas->addCell(id, op);            // op 形如 $add / $mul ...
cell->setPort(RTLIL::ID::A, a);
cell->setPort(RTLIL::ID::B, b);
cell->setParam(RTLIL::ID::A_WIDTH, a.size());
cell->setParam(RTLIL::ID::B_WIDTH, b.size());
cell->setParam(RTLIL::ID::A_SIGNED, a_signed);
cell->setParam(RTLIL::ID::B_SIGNED, b_signed);
cell->setParam(RTLIL::ID::Y_WIDTH, y_width - msb_zeroes);
cell->setPort(RTLIL::ID::Y, y);
bless_cell(cell);
return {SigSpec(RTLIL::S0, msb_zeroes), y};
```

参见 [src/builder.cc:419-430](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L419-L430)。这里有一个小区别于「标准五步」：`$mul` 在无符号乘法且高位确定全 0 时，会先算出高位零的个数 `msb_zeroes`，只建一个变窄的乘法器，再在结果前面拼上若干位 `S0` 返回。这是 builder 主动做的「结果宽度优化」，避免生成过宽的乘法器。

`Unop` 的结构与 `Biop` 对称，见 [src/builder.cc:433-460](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L433-L460)，此处不再贴全。

调用方示例：`EvalContext::operator()` 里求值二元运算与一元运算时直接调 `netlist.Biop`/`netlist.Unop`，参见 [src/slang_frontend.cc:1334](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1334) 与 [src/slang_frontend.cc:1374](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1374)。

#### 4.2.4 代码实践：用真实测试看 `$add` 单元

**实践目标**：借助现成的测试 `tests/unit/dff.ys`，看清一句 `q <= d + 1` 是怎样变成 `$add` 单元的。

**操作步骤**：

1. 打开 [tests/unit/dff.ys:1-39](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/dff.ys#L1-L39)。其中 gate 设计（被测实现）是：

```systemverilog
module dff_iff01_gate(input logic clk, input logic en,
                      input logic [3:0] d, output logic [3:0] q);
    always_ff @(posedge clk iff en) begin
        q <= d + 1;
    end
endmodule
```

2. 关注紧随其后的 gold 网表（手写的期望结果），其中有一段：

```
cell $add $2
  parameter \A_SIGNED 0
  parameter \A_WIDTH 4
  parameter \B_SIGNED 0
  parameter \B_WIDTH 1
  parameter \Y_WIDTH 4
  connect \A \d
  connect \B 1'1
  connect \Y $1
end
```

**需要观察的现象**：`d + 1` 这个二元加法被翻译成了一个 `$add` 单元，`A` 接 4 位的 `d`，`B` 接 1 位常量 `1'1`，结果送到中间线 `$1`。

**预期结果**：对照 [src/builder.cc:419-430](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L419-L430) 的建单元代码，可见 gold 网表里的端口/参数命名（`A`/`B`/`Y`/`A_WIDTH`/`B_WIDTH`/`Y_WIDTH`/`A_SIGNED`/`B_SIGNED`）与 `Biop` 里 `setPort`/`setParam` 设置的完全一致——这就是 `Biop(ID($add), d, 1, false, false, 4)` 的产物。注意这里 `d` 和 `1` 都不全为常量（`d` 是输入信号），所以没有走常量折叠，而是真的建了 `$add`。

#### 4.2.5 小练习与答案

**练习 1**：`Bwmux` 与 `Mux` 的选择信号宽度要求有什么不同？分别对应 SV 里的什么场景？

**参考答案**：`Mux` 的 `s` 恰好 1 位，整体二选一；`Bwmux` 的 `s` 与数据等宽，逐位独立选择。`Mux` 对应 `sel ? b : a` 这种整体选择；`Bwmux` 对应「只改某些位、其余位保留」的部分赋值场景（过程块里用掩码混合新值与背景值）。

**练习 2**：`Biop` 里为什么对 `$mul` 要单独计算 `msb_zeroes` 并缩窄单元？

**参考答案**：无符号乘法若某些高位输入恒为 0，乘积高位必然为 0，建一个更窄的乘法器再补零，比直接建满宽乘法器更省面积、更利于下游优化。

**练习 3**：若把 `dff.ys` 里的 `q <= d + 1` 改成 `q <= 4'd3 + 4'd5`（两端都常量），`Biop` 会生成 `$add` 单元吗？

**参考答案**：不会。两个输入都 `is_fully_const()`，会命中 [src/builder.cc:336-366](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L336-L366) 的 `OP(add)` 分支，直接返回常量 `4'd8`，不建任何单元。

---

### 4.3 时序单元：add_dff / add_dffe / add_aldff / add_aldffe

#### 4.3.1 概念说明

时序单元是有时钟、有记忆的单元，对应 SV 里的 `always_ff`、锁存器等。sv-elab 的命名规律很整齐：

| builder 方法 | 生成的 RTLIL 单元 | 含义 |
| --- | --- | --- |
| `add_dff` | `$dff` | 基础 D 触发器，时钟沿采样 |
| `add_dffe` | `$dffe` | 带使能 EN 的 D 触发器 |
| `add_aldff` | `$aldff` | 带异步加载（ALOAD/AD）的 D 触发器 |
| `add_aldffe` | `$aldffe` | 既带使能又带异步加载 |
| `add_dual_edge_aldff` | 两个 `$dff`/`$aldff` + 一个 `$mux` | 双沿（BothEdges）时钟的软件模拟 |

异步加载（asynchronous load，`aload`）对应 SV 里 `always_ff @(posedge clk or posedge rst)` 这类带异步复位的写法——当 `aload` 有效时，触发器立刻把 `AD` 端口的值加载进 `Q`，不必等时钟沿。

#### 4.3.2 核心流程：层层降级

时序方法的一个重要设计是「降级（fallback）」：更复杂的单元在某些输入退化为常量时，会回退成更简单的单元。例如 `add_dffe` 在使能信号 `en` 恒为有效时，本质上等价于一个普通 `$dff`，于是直接调 `add_dff`：

```cpp
void RTLILBuilder::add_dffe(...) {
    if (en.is_fully_def() && en.as_bool() == en_polarity) {
        add_dff(name, clk, d, q, clk_polarity);   // 降级为 $dff
        return;
    }
    RTLIL::Cell *cell = canvas->addDffe(canvas->uniquify(id(name)),
                                        clk, en, d, q, clk_polarity, en_polarity);
    bless_cell(cell);
}
```

参见 [src/builder.cc:522-534](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L522-L534)。`add_aldffe` 同理——使能恒有效时降级为 `add_aldff`，见 [src/builder.cc:545-557](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L545-L557)。

最朴素的基础方法 `add_dff` 反而没有降级分支，只是简单建单元 + 盖印：

```cpp
void RTLILBuilder::add_dff(std::string_view name, const RTLIL::SigSpec &clk,
        const RTLIL::SigSpec &d, const RTLIL::SigSpec &q, bool clk_polarity) {
    RTLIL::Cell *cell = canvas->addDff(canvas->uniquify(id(name)), clk, d, q, clk_polarity);
    bless_cell(cell);
}
```

参见 [src/builder.cc:515-520](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L515-L520)。`add_aldff` 结构相同，只是多一组 `aload`/`ad` 端口，见 [src/builder.cc:536-543](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L536-L543)。

#### 4.3.3 源码精读：双沿触发的软件模拟

`add_dual_edge_aldff` 是本节最有趣的方法。RTLIL 的 `$dff` 只支持单沿（posedge 或 negedge），但 SV 允许 `@(edge clk)`（双沿）。builder 的做法不是发明新单元，而是「用两个单沿触发器 + 一个 mux 软件模拟」：

```cpp
void RTLILBuilder::add_dual_edge_aldff(const std::string &base_name, RTLIL::SigSpec clk,
        RTLIL::SigSpec aload, RTLIL::SigSpec d, RTLIL::SigSpec q, RTLIL::SigSpec ad,
        bool aload_polarity) {
    RTLIL::Wire *pos_q = canvas->addWire(canvas->uniquify(base_name + "$pos$q"), d.size());
    RTLIL::Wire *neg_q = canvas->addWire(canvas->uniquify(base_name + "$neg$q"), d.size());
    // ... 一个上升沿 FF（pos_q）、一个下降沿 FF（neg_q），都采样同一个 d ...
    // 行为：clk=0 时选 neg_q（在下降沿捕获），clk=1 时选 pos_q（在上升沿捕获）
    RTLIL::Cell *mux = canvas->addMux(canvas->uniquify(base_name + "$mux"),
            /*A=*/neg_q, /*B=*/pos_q, /*S=*/clk, /*Y=*/q);
    bless_cell(mux);
}
```

参见 [src/builder.cc:473-513](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L473-L513)。注意最后的 mux：`A=neg_q, B=pos_q, S=clk, Y=q`，即 \(Q = \text{clk}\,?\,\text{pos\_q} : \text{neg\_q}\)。这正是 4.1 节 `Mux(a,b,s)` 语义的直接体现——这里没用 `Mux()` 封装，而是直接调底层 `addMux`，因为两个 FF 输出已经是具名线，不需要再 `add_y_wire`。

**调用方上下文**：这些方法在 `handle_ff_process`（处理 always_ff 的核心）里被大量调用。典型片段：

```cpp
AttributeGuard guard(netlist);                 // 开启属性作用域
transfer_attrs(netlist, symbol, guard);        // 把符号的属性/源位置塞进暂存表
...
netlist.add_dffe(base_name,
                 timing.triggers[0].signal,    // CLK
                 event_guard,                  // EN
                 assigned.extract(...),        // D
                 netlist.convert_static(named_chunk), // Q
                 timing.triggers[0].edge_polarity,
                 true);
```

参见 [src/slang_frontend.cc:1940-1968](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1940-L1968)。注意 `AttributeGuard` 把这一批触发器的源属性绑定到了对应符号上——这正是 4.4 节要讲清的机制。含异步复位的分支则改调 `add_aldffe`，见 [src/slang_frontend.cc:2004-2013](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L2004-L2013)。

#### 4.3.4 代码实践：追踪 `add_dffe` 到 `$dffe`

**实践目标**：把 4.2.4 里那个 `dff.ys` 测试的另一半看完——验证带使能的触发器确实变成了 `$dffe`，并核对端口连接与 `add_dffe` 的参数一一对应。

**操作步骤**：

1. 继续看 [tests/unit/dff.ys:29-38](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/dff.ys#L29-L38) 的 gold 网表：

```
cell $dffe $3
  parameter \EN_POLARITY 1'1
  parameter \CLK_POLARITY 1'1
  parameter \WIDTH 4
  connect \CLK \clk
  connect \D $1
  connect \Q \q
  connect \EN \en
end
```

2. 对照 builder 源码 [src/builder.cc:522-534](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L522-L534) 里 `canvas->addDffe(name, clk, en, d, q, clk_polarity, en_polarity)` 的参数顺序。

**需要观察的现象**：gold 网表里 `CLK=clk`、`EN=en`、`D=$1`（来自上一个 `$add` 的输出）、`Q=q`，参数 `EN_POLARITY=1`、`CLK_POLARITY=1`、`WIDTH=4`。

**预期结果**：端口与参数完全对得上 `add_dffe` 的调用——`clk` 来自 `timing.triggers[0].signal`，`en` 来自 `event_guard`（注意源 `@(posedge clk iff en)` 里的 `iff en` 被翻译成了使能 `EN=en`），`D` 是 `d + 1` 的结果 `$1`，`Q` 是输出 `q`。由于 `en` 是真实输入信号（非常量），没有触发「降级为 `$dff`」的分支，所以确实生成了 `$dffe`。

**待本地验证**：若你本地装好了插件，可自行把 `tests/unit/dff.ys` 跑一遍（`yosys -p "script tests/unit/dff.ys"`，路径按实际仓库调整），观察 `equiv_status -assert` 是否通过；若没有插件，上述「读 gold 网表对照源码」即是完整的源码阅读型实践。

#### 4.3.5 小练习与答案

**练习 1**：`add_dffe` 在什么条件下会降级成 `add_dff`？为什么这个降级是安全的？

**参考答案**：当使能 `en` 是全定值且 `en.as_bool() == en_polarity`（即 EN 恒为有效）时降级。因为 EN 恒有效意味着触发器每个时钟沿都采样，使能失去意义，等价于普通 `$dff`。生成更简单的单元对下游综合有利。

**练习 2**：`add_dual_edge_aldff` 用「两个 FF + 一个 mux」模拟双沿。这个 mux 的选择信号是什么？为什么这样选？

**参考答案**：选择信号是时钟 `clk` 本身：`clk=1` 时选上升沿 FF 的输出 `pos_q`，`clk=0` 时选下降沿 FF 的输出 `neg_q`。因为上升沿 FF 在 `clk` 由 0→1 时更新，稳定后 `clk=1` 期间应取它的值；下降沿 FF 在 `clk` 由 1→0 时更新，稳定后 `clk=0` 期间应取它的值。

**练习 3**：`add_aldffe` 与 `add_dffe` 相比多了哪些端口？分别承载什么 SV 语义？

**参考答案**：多了 `ALOAD`（异步加载触发信号）与 `AD`（异步加载值）。对应 SV 里 `@(posedge clk or posedge rst)` 的异步复位/置位——`ALOAD` 有效时，`Q` 立刻取 `AD` 的值，不等时钟沿。

---

### 4.4 属性暂存：bless_cell、staged_attributes 与 AttributeGuard

#### 4.4.1 概念说明

到目前为止，我们一直把 `bless_cell(cell)` 当成一个「收尾步骤」一笔带过。这一节专门讲清它，因为它解决的是一个工程性问题：**怎样把源码位置和用户属性挂到「即将生成」的单元上，而不用把属性参数穿过十几层函数调用。**

需求是这样的：sv-elab 希望每个生成的 RTLIL 单元都带上 `src` 属性（指向 SV 源码位置，方便调试与报错回溯），并且若 SV 源码里写了 `(* keep, maxfanout=10 *)` 这类属性，也要传递到对应单元上。但生成单元的代码路径很深（`EvalContext` → `Biop` → `bless_cell`），如果把「当前属性」作为参数层层传递，函数签名会非常臃肿。

解法是「全局暂存 + RAII 作用域」：

- `RTLILBuilder` 自身维护一个暂存表 `staged_attributes` 和一个暂存源码位置 `staged_source_range`。
- 进入一段需要绑定属性的代码时，构造一个 `AttributeGuard` 栈对象：它先**保存并清空**当前暂存表；之后往里 `set` 属性；离开作用域时（析构）自动**恢复**原来的暂存表。
- 这段代码内每创建一个单元，`bless_cell` 就把当前暂存表的内容**复制**到该单元的 `attributes` 上。

这样，属性就「天然地」只作用于在该作用域内创建的单元。

#### 4.4.2 核心流程

用一个伪代码时序说明一次带属性的单元生成：

```
{                                          // 进入表达式 a + b 的求值
    AttributeGuard guard(netlist);         // 1. 保存当前 staged_attributes，清空
    transfer_attrs(netlist, expr, guard);  // 2. 把表达式 a+b 的源位置/属性塞进 staged
    //    guard.set_source(expr.sourceRange)
    //    guard.set("maxfanout", 10)

    SigSpec y = netlist.Biop($add, a, b, false, false, 4);
    //    ↳ Biop 内部 addCell($add) 后调 bless_cell(cell)
    //      ↳ cell->attributes = staged_attributes;   // 复制到单元
    //      ↳ cell->attributes[src] = format_src(...); // 加源位置
}                                          // 3. guard 析构，恢复原 staged_attributes
```

关键在于：`bless_cell` 复制的是 builder 此刻的暂存表，而暂存表由外层最近的 `AttributeGuard` 控制。这是一个「隐式上下文」，用 RAII 保证成对进出、异常安全。

#### 4.4.3 源码精读

先看 `bless_cell` 本体——它做两件事：复制暂存属性表，并在缺 `src` 时补上格式化源位置：

```cpp
void RTLILBuilder::bless_cell(RTLIL::Cell *cell) {
    cell->attributes = staged_attributes;
    if (staged_source_range_valid && !cell->attributes.count(ID::src)) {
        auto src = format_src(staged_source_range);
        if (!src.empty())
            cell->attributes[ID::src] = src;
    }
}
```

参见 [src/builder.cc:52-60](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L52-L60)。注意 `!cell->attributes.count(ID::src)` 这层判断：如果调用方已显式在暂存表里塞过 `src`，就不覆盖；同时源位置字符串是「惰性格式化」的——只在真要建单元时才调 `format_src`，因为很多表达式叶子根本不会产生单元，提前格式化纯属浪费（注释见 [src/slang_frontend.h:364-367](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L364-L367)）。

再看 `AttributeGuard` 的 RAII 三件套（构造/析构/set）：

```cpp
class AttributeGuard {
public:
    AttributeGuard(RTLILBuilder &builder) : builder(builder) {
        save.swap(builder.staged_attributes);                 // 保存并清空暂存表
        save_source_range = builder.staged_source_range;
        save_source_range_valid = builder.staged_source_range_valid;
        builder.staged_source_range_valid = false;
    }
    ~AttributeGuard() {
        save.swap(builder.staged_attributes);                 // 恢复暂存表
        builder.staged_source_range = save_source_range;
        builder.staged_source_range_valid = save_source_range_valid;
    }
    void set(RTLIL::IdString id, RTLIL::Const value) {
        builder.staged_attributes[id] = value;                // 往暂存表塞属性
    }
    void set_source(slang::SourceRange source_range) {
        builder.staged_source_range = source_range;
        builder.staged_source_range_valid = true;
    }
    ...
};
```

参见 [src/slang_frontend.h:437-471](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L437-L471)。`save.swap(builder.staged_attributes)` 这个「交换」技巧同时完成了「保存旧值」与「清空当前表」两件事——交换后 `save` 持有旧表，`builder.staged_attributes` 变成空的。析构时再交换回来，旧表回归 builder。

**属性从哪来**：`transfer_attrs` 把 slang AST 节点上挂的属性搬到 guard 里：

```cpp
void transfer_attrs(NetlistContext &netlist, T &from, AttributeGuard &guard) {
    guard.set_source(source_location(from));              // 源位置
    for (auto attr : global_compilation->getAttributes(from)) {
        if (auto value = convert_attr_value(netlist, attr))
            guard.set(id(attr->name), *value);            // 用户属性
    }
}
```

参见 [src/slang_frontend.cc:320-330](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L320-L330)。所以标准用法是「两句一组」：

```cpp
AttributeGuard guard(netlist);
transfer_attrs(netlist, expr, guard);   // 表达式：src/slang_frontend.cc:1210-1211
```

这是 `EvalContext::operator()` 的开头，见 [src/slang_frontend.cc:1210-1211](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1210-L1211)。`handle_ff_process` 里给触发器绑定属性用的是同一模式，见 [src/slang_frontend.cc:1940-1941](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1940-L1941)；过程块语句里也类似，见 [src/statements.h:133](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L133)。

#### 4.4.4 代码实践：观察 `bless_cell` 写下的 `src` 属性

**实践目标**：验证 `bless_cell` 真的给生成的单元挂上了 `src` 属性（指向 SV 源码位置）。

**操作步骤**：

1. 复用 4.1.4 的 `mux_demo.sv`，把 `run.ys` 改成打印属性：

```tcl
read_slang mux_demo.sv
select mux_demo:%
setattr -mod -set src_nonempty 1     ;# 占位，确保模块被选中
write_rtlil
```

2. 运行后查看输出的 RTLIL 文本里那个 `$mux` 单元。

**需要观察的现象**：`$mux` 单元块里应含一行形如 `attribute \src "mux_demo.sv:2.7-3.17"` 的属性行。

**预期结果**：`bless_cell` 把 `staged_source_range`（由 `EvalContext` 开头的 `AttributeGuard` + `transfer_attrs` 设置）格式化后写入了单元的 `src` 属性，于是每个单元都能追溯到它来自 SV 哪一行。若你的 Yosys 版本对 `src` 属性显示有差异，也可在 Yosys 里用 `select` + `show -attrs` 查看单元属性。

**待本地验证**：具体的 `src` 字符串格式（文件名、行列号）取决于本地路径与 slang 的源范围表示，请以实际输出为准。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `staged_source_range` 要做成「惰性格式化」（只在 `bless_cell` 时才调 `format_src`），而不是在 `set_source` 时就格式化好？

**参考答案**：因为大量表达式节点最终会被常量折叠掉、不产生任何单元（如 4.2 里两个常量相加）。若进 `set_source` 就格式化，这些根本不建单元的节点也会白白付出字符串格式化的开销。延后到 `bless_cell`——即确知要建单元时——才格式化，能省掉这些无用功。

**练习 2**：如果一段代码里嵌套构造了两个 `AttributeGuard`，内层 `set` 的属性会泄漏到外层创建的单元上吗？为什么？

**参考答案**：不会。内层 guard 构造时已把外层暂存表 `swap` 走并清空，内层 `set` 的属性只存在于内层暂存表；内层 guard 析构时又把外层暂存表换回。所以外层创建的单元只看到外层暂存表的内容，属性作用域是正确嵌套隔离的。

**练习 3**：`bless_cell` 里 `!cell->attributes.count(ID::src)` 这个判断保护了什么场景？

**参考答案**：保护调用方已显式在 `staged_attributes` 里塞了自定义 `src` 的场景。`cell->attributes = staged_attributes` 之后，若暂存表里已有 `src`，就不再用 `format_src(staged_source_range)` 覆盖它，尊重显式值。

## 5. 综合实践

把本讲四个模块串起来：写一个**同时产生 `$mux` 与 `$dffe`** 的小设计，跑通后用源码解释每一处单元的来历，并验证 `bless_cell` 的属性绑定。

**设计文件 `combo.sv`**：

```systemverilog
module combo(input  logic clk, en, sel,
             input  logic [3:0] a, b,
             output logic [3:0] q);
    always_ff @(posedge clk iff en)
        q <= sel ? b : a;
endmodule
```

**脚本 `run.ys`**：

```tcl
read_slang combo.sv
proc
write_rtlil combo.il
```

**操作步骤**：

1. 用 `yosys run.ys`（内置版本直接跑；老版本用 `yosys -m slang run.ys`）运行，得到 `combo.il`。
2. 打开 `combo.il`，找到这两个单元：
   - 一个 `$mux`：来自 `sel ? b : a`，由 [src/builder.cc:175-186](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L175-L186) 的 `Mux(a, b, sel)` 生成，端口 `A=a, B=b, S=sel`，输出接到 `$dffe` 的 `D`。
   - 一个 `$dffe`：来自带 `iff en` 的 `always_ff`（`iff en` 在单沿下被支持，翻译为使能 `EN=en`），由 [src/slang_frontend.cc:1961-1967](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1961-L1967) 的 `add_dffe` 调用生成，`CLK=clk, EN=en, D=$mux的输出, Q=q`。
3. 检查这两个单元的 `attribute \src ...` 行，确认它们都指向 `combo.sv` 的对应行——这是 `bless_cell`（[src/builder.cc:52-60](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L52-L60)）与 `handle_ff_process` 开头的 `AttributeGuard`（[src/slang_frontend.cc:1940-1941](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1940-L1941)）合作的结果。

**需要观察的现象**：`proc` 之后，模块里恰好一个 `$mux` 喂给一个 `$dffe`，`$mux` 的选择位是 `sel`，`$dffe` 的使能是 `en`。

**预期结果**：与本讲的单元语义完全吻合——组合部分用 `$mux`（4.1/4.2），时序部分用 `$dffe`（4.3），两者都带 `src` 属性（4.4）。这也正好复用了 4.2.4、4.3.4 里 `dff.ys` 已验证过的 `$add`→`$dffe` 模式，只是把上游的 `$add` 换成了 `$mux`。

**待本地验证**：具体单元编号、`src` 字符串格式随环境变化；若没有插件，可对照本讲引用的源码完成「源码阅读型」版本的练习——画出 `sel ? b : a` 在 `q <= ...` 赋值中的调用链：`EvalContext` 求值三元 → `netlist.Mux(a,b,sel)` → `addMux` + `bless_cell`，再由 `handle_ff_process` 把结果接到 `add_dffe` 的 `D` 端。

## 6. 本讲小结

- `RTLILBuilder` 是 sv-elab 的「画笔」，只负责在 `RTLIL::Module` 画布上建线/建单元，其组合方法几乎都遵循「常量折叠 → 特化 → 建输出线 → 建单元 → `bless_cell`」的五步模式。
- 组合单元由 `Mux`（`$mux`）、`Bwmux`（`$bwmux`，按位选择）、`Biop`（`$add`/`$mul`/比较/移位…）、`Unop`（`$neg`/`$not`/reduce…）封装，统一隐藏了端口/参数设置的繁琐细节。
- 时序单元 `add_dff`/`add_dffe`/`add_aldff`/`add_aldffe` 对应 `$dff`/`$dffe`/`$aldff`/`$aldffe`，并带「使能恒有效则降级」的回退逻辑；双沿触发用「两个 FF + 一个 mux」软件模拟。
- `bless_cell` + `staged_attributes` + `AttributeGuard` 构成 RAII 式属性暂存机制，让源码位置与用户属性无需穿透调用栈就能绑定到对应单元上。
- `tests/unit/dff.ys` 是贯穿全讲的活样本：`q <= d + 1` 同时演示了 `Biop` 生 `$add` 与 `add_dffe` 生 `$dffe`。

## 7. 下一步学习建议

本讲只讲了「单元怎么发出来」，还没讲「为什么发这个单元、不发那个」。建议下一步：

- 学习 **u3-l3（Variable 与 VariableBits）**：理解过程块左值的位级抽象，看 `Bwmux` 里「背景值/新值/掩码」混合的真正用途。
- 学习 **u3-l4（Case 与 Switch）**：看 `Bwmux` 与掩码如何嵌进过程块的 case 树。
- 若对触发器选择逻辑感兴趣，可直接跳到 **u6-l1（TimingPatternInterpretor）** 与 **u6-l2（触发器发射与异步复位）**，那里会讲清 `handle_ff_process` 是如何在 `add_dffe`/`add_aldffe`/`add_dual_edge_aldff` 之间做选择的。
