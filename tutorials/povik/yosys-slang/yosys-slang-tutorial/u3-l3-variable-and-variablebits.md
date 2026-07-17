# Variable 与 VariableBits：HDL 意图的位级表示

## 1. 本讲目标

前两讲（u3-l1、u3-l2）我们认识了两件东西：`NetlistContext` 这个「中枢对象」，以及 `RTLILBuilder` 这支「画笔」。画笔在 `RTLIL::Module` 画布上画的是**已经确定**的电路——具体的线（`RTLIL::Wire`/`SigSpec`）和具体的单元（`$add`/`$mux`/`$dff`…）。但 SystemVerilog 的过程块（`always`/`initial`）里，事情没那么干脆：同一个变量在不同 `if` 分支里可能被赋予不同的值，局部变量可能还没有任何一根线对应，动态索引 `a[i]` 的左值在编译期甚至点不出具体是哪一位。

本讲就来拆解 sv-elab 为此专门引入的一组「**HDL 意图**」位级抽象：`Variable`、`VariableBit`、`VariableChunk`、`VariableBits`。它们和 `RTLIL::SigSpec` 长得很像，却承担完全不同的职责。

学完本讲你应当能够：

- 说清 `Variable` 的 `Kind` 分类（`Static`/`Local`/`EscapeFlag`/`Dummy`/`Invalid`），以及 `from_symbol` 如何把一个 slang 符号归入 `Static` 或 `Local`。
- 读懂 `VariableBits` 的「chunk / bits 双态」存储：什么时候是一段连续 chunk，什么时候被摊平成逐位 vector，以及它怎样被当成轻量哈希键使用。
- 区分「HDL 意图左值（`VariableBits`）」与「最终网表信号（`RTLIL::SigSpec`）」，并能指出 `VariableBits` 在哪几个代码点被**解析（resolve）**成真实信号。
- 回答本讲的核心实践问题：为什么 sv-elab 不直接用 `RTLIL::SigSpec` 表示过程块里的左值。

## 2. 前置知识

在进入源码前，先建立四点直觉。

**第一，「左值（LValue）」与「右值（RValue）」的差别。** 在 `a = b + c` 里，`a` 是左值（被赋值的对象），`b + c` 是右值（要计算出来的值）。综合器处理右值时，要的是「算出一根信号」；处理左值时，要的是「这根信号该写回哪里」。这「写回哪里」在过程块里恰恰是最棘手的部分。

**第二，过程块用「case 树」而不是直接连线。** sv-elab 把一个 `always` 块建模成一棵模仿 RTLIL `SwitchRule`/`CaseRule` 的 case 树（这是 u3-l4 的主题）。意思是：翻译阶段并不立刻把每次赋值变成一根硬连线，而是把「在哪个分支、哪个变量的哪一位、被赋成什么值」**记录下来**，等整棵树搭好再统一 lower 成 `RTLIL::Process`。换句话说，左值在很长一段时间内需要一种**抽象的、尚未物化成线**的表示。

**第三，为什么哈希键很关键。** 上一讲提到 `NetlistContext` 持有几个按位记录状态的成员，例如 `driven_variables`（被驱动变量）、`special_net_drivers`（wand/wor 这类特殊线网的驱动）、`initial_state`（初值）。它们都是「字典 / 集合」，键是「某个变量的某一位」。这就要求「某变量某一位」必须是一个**轻量、可哈希、可排序**的对象。`RTLIL::SigSpec` 是个又重又会随合并而变化的对象，并不适合当键。

**第四，union 与 `std::variant`。** C++ 的 `union` 让多个字段共享同一块内存；`std::variant<T1, T2>` 则是一个「类型安全的 union」，同一时刻只持有其中一种类型，可以用 `holds_alternative`/`get` 查询当前是哪种。本讲里 `Variable` 用 union 复用存储，`VariableBits` 用 variant 在「一段 chunk」和「逐位 vector」之间二选一。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [src/variables.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/variables.h) | `VariableBit`、`VariableChunk`、`VariableBits` 三个结构的全部声明，含双态存储与各种迭代器。 |
| [src/variables.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/variables.cc) | `Variable` 的方法实现：`from_symbol`/`escape_flag`/`dummy`、`operator<`、`hash_label`、`bitwidth`、`text`。 |
| [src/slang_frontend.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h) | `Variable` 类声明（含 `Kind` 枚举与 union 存储）、`EvalContext::variable` 声明、`VariableState`、`NetlistContext` 里以 `VariableBit` 为键的若干字典。 |
| [src/slang_frontend.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc) | 解析点：`EvalContext::variable`、`EvalContext::lhs`、`VariableState::evaluate`/`set`、`NetlistContext::convert_static`，以及右值读取时对 `Variable` 的分派。 |
| [tests/unit/complex_lhs.sv](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/complex_lhs.sv) | 一个含静态赋值、位选择、拼接的真实测试，本讲用它做实践样本。 |

> 提醒：`Variable` 类**没有**放在 `variables.h`，而是声明在 [src/slang_frontend.h:95](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L95)；位级的 `VariableBit`/`VariableChunk`/`VariableBits` 才在 `variables.h` 里。文件里第 51–52 行的注释点出了对应关系：「`VariableChunk` 之于 `VariableBit`，就像 `RTLIL::SigChunk` 之于 `RTLIL::SigBit`」。

## 4. 核心概念与源码讲解

### 4.1 Variable：HDL 变量的「身份卡」

#### 4.1.1 概念说明

把一个 SystemVerilog 变量（比如 `logic [7:0] a;`）翻译成电路时，sv-elab 需要一种**不立刻承诺「它对应哪根线」**的中间表示。`Variable` 就是这张「身份卡」：它只记录「这是哪个符号、属于哪一类」，至于「这个变量在当前过程块里此刻的值是多少」「它最终物化成哪根线」，都留给后续阶段决定。

关键在于它把变量分成五类 `Kind`，不同类的解析规则天差地别。参见 [src/slang_frontend.h:95-103](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L95-L103)：

```cpp
class Variable {
public:
    enum Kind {
        Static,       // 静态生存期：模块级 net/reg、static 变量，对应一根持久的线
        Local,        // 自动生存期：过程块/function 里的局部变量，无持久线
        EscapeFlag,   // 控制流「逃逸标志」：break/continue/return 的辅助信号
        Dummy,        // 占位：分析失败或不可表达时塞一个「宽度已知、值为 x」的替身
        Invalid       // 未初始化
    } kind;
```

为什么必须区分 `Static` 与 `Local`？因为综合语义不同：

- `Static` 变量有**持久存储**——它最终就是画布上一根 `RTLIL::Wire`（或端口），在任何分支里读它都读同一根线。
- `Local`（automatic）变量**没有**持久线。它在 `function` 里随调用而生成、随返回而消失；在一个过程块的 case 树里，它的「当前值」是用一张按位映射表临时记住的（见 4.3 节的 `VariableState`）。同一份源码里同一个局部变量，在不同嵌套层级（reentrant scope）下还会产生**不同的** `Local` 身份——这就是 `depth`（嵌套层级）字段的用途。

#### 4.1.2 核心流程：从 slang 符号到 Variable

slang 侧给出的符号类型是 `ast::ValueSymbol`。`Variable::from_symbol` 负责把它归类，参见 [src/variables.cc:26-37](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/variables.cc#L26-L37)：

```cpp
Variable Variable::from_symbol(const ast::ValueSymbol *symbol, int depth)
{
    assert(symbol);
    if (ast::VariableSymbol::isKind(symbol->kind) &&
            symbol->as<ast::VariableSymbol>().lifetime == ast::VariableLifetime::Automatic) {
        assert(depth >= 0);
        return Variable(Local, symbol, depth);   // 自动变量 → Local，带嵌套层级
    } else {
        assert(depth == -1);                       // 静态变量不允许带层级
        return Variable(Static, symbol, 0);
    }
}
```

判据很直白：**只有 `VariableSymbol` 且 `lifetime == Automatic` 才算 `Local`，其余（net、`static` 变量、参数化端口等）统统归 `Static`**。注意 `depth` 的契约——`Local` 要求 `depth >= 0`，`Static` 要求 `depth == -1`（即「无层级」）。这个 `depth` 不是 `from_symbol` 自己算的，而是调用方 `EvalContext::variable` 通过查 `scope_nest_level` 表得到的（见 4.3 节）。

另外两个工厂方法服务于非符号变量，参见 [src/variables.cc:39-53](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/variables.cc#L39-L53)：

```cpp
Variable Variable::escape_flag(int id) { ... var.kind = EscapeFlag; var.id = id; ... }
Variable Variable::dummy(uint64_t width) { ... var.kind = Dummy; var.width = width; ... }
```

- `escape_flag(id)`：每个循环 / 函数会注册一个「逃逸标志」变量（详见 u5-l4），它只占 1 位，用来在 case 树里标记「我们已经 break/continue/return 了」。它没有对应的 slang 符号，只有一个自增 `id`。
- `dummy(width)`：分析失败、或遇到了无法静态确定的左值时（见 4.3 节 `EvalContext::lhs`），返回一个「宽度已知、内容是 x」的替身，避免后续代码崩在空指针上。

#### 4.1.3 源码精读：union 存储、哈希与排序

`Variable` 把三类数据塞进一个 union 共享内存，参见 [src/slang_frontend.h:124-130](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L124-L130)：

```cpp
private:
    union {
        const ast::ValueSymbol *symbol;   // Static / Local 用：指向 slang 符号
        uint64_t width;                    // Dummy 用：占位宽度
        int id;                            // EscapeFlag 用：标志编号
    };
    int depth = 0;                         // 仅 Local 有意义：嵌套层级
```

这是典型的「用一个枚举 `kind` 给 union 当标签（tag）」的手法。读 union 前必须先看 `kind` 决定取哪个字段，否则是未定义行为。源码里每个访问方法都严格按 `kind` 分派，例如 `bitwidth()`，参见 [src/variables.cc:207-216](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/variables.cc#L207-L216)：

```cpp
uint64_t Variable::bitwidth() const
{
    switch (kind) {
    case Static:
    case Local:      return symbol->getType().getBitstreamWidth();  // 问 slang 要位宽
    case EscapeFlag: return 1;
    case Dummy:      return width;
    default:         log_abort();
    }
}
```

`Static`/`Local` 的位宽来自 slang 类型系统的 `getBitstreamWidth()`（按位的「比特流宽度」，与 SV 的位宽语义一致；结构体也会被打平成连续位）；`EscapeFlag` 恒为 1 位；`Dummy` 用记录的 `width`。

要把 `Variable` 当哈希键，需要一个稳定的「标签」。`hash_label()` 把三类信息打包成一个 tuple，参见 [src/variables.cc:183-196](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/variables.cc#L183-L196)：

```cpp
Variable::HashLabel Variable::hash_label() const
{
    void *ptr = 0;
    uint64_t num = depth;
    switch (kind) {
    case Static:
    case Local:      ptr = (void *)symbol; break;   // 用符号指针
    case EscapeFlag: num = id; break;               // 用 id
    case Dummy:      num = width; break;            // 用 width
    default:         log_abort();
    }
    return std::make_tuple((int)kind, ptr, num);
}
```

注意 `Static` 与 `Local` 共享同一个判分支，但 `num` 不同：`Static` 的 `depth` 恒为 0，而同一个 slang 符号若在不同嵌套层级被引用，`Local` 会带上不同的 `depth`，从而产生**不同**的 `hash_label`。这正是 reentrant 函数局部变量互不混淆的关键。`operator==` 直接比较 `hash_label`，参见 [src/slang_frontend.h:114](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L114)。

`operator<` 则提供一套与 `getHierarchicalPath` 一致的确定序，用于把变量排成稳定顺序（详见 [src/variables.cc:162-181](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/variables.cc#L162-L181)，以及辅助函数 `order_symbols_within_scope`/`order_scopes`）。确定序的好处是：无论遍历顺序如何，同一组变量最终排出的顺序一致，让生成的网表可复现。

#### 4.1.4 代码实践：把符号分进两类

1. **实践目标**：亲手验证 `from_symbol` 的 `Static`/`Local` 判据，理解 `depth` 的契约。
2. **操作步骤**：
   - 打开 [src/variables.cc:26-37](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/variables.cc#L26-L37)。
   - 对照下面这段「示例代码」（非项目原有，仅用于理解）：

     ```systemverilog
     module m(input logic [7:0] din, output logic [7:0] dout);
         logic [7:0] sreg;              // 模块级 net/reg → Static, depth=-1 传入
         function automatic logic [7:0] inc(logic [7:0] x);  // automatic
             logic [7:0] tmp = x + 1;   // tmp → Local, depth=当前嵌套层级
             return tmp;
         endfunction
     endmodule
     ```
   - 为 `sreg`、`din`、`dout`、`tmp` 各写一句「`from_symbol` 会返回什么 Kind、`depth` 是多少」。
3. **需要观察的现象**：`din`/`dout` 是端口（端口底层也是 `ValueSymbol`，但 lifetime 不是 Automatic），所以它们也走 `Static` 分支；只有 `function automatic` 内部的 `tmp` 命中 `Local` 分支且 `depth >= 0`。
4. **预期结果**：`sreg`/`din`/`dout` → `Static`（`depth == -1`，断言通过）；`tmp` → `Local`（`depth` 由 `EvalContext::variable` 查 `scope_nest_level` 给出）。如果你在 `from_symbol` 里把 `depth == -1` 的断言改成 `log("depth=%d", depth)`，对 `sreg` 应打印 `-1`。
5. **待本地验证**：实际打印需要自行加日志并重新编译运行（见 u8-l3 的构建方式），本讲不假装已运行。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `EscapeFlag` 和 `Dummy` 不需要指向 slang 符号？

> **答案**：`EscapeFlag` 是 sv-elab 自己为了建模 `break`/`continue`/`return` 而**凭空造出来**的控制流信号，源码里没有对应的 SV 符号；`Dummy` 是分析失败时的占位替身，只需要一个宽度，同样没有符号。它们用 `id`/`width` 而非 `symbol` 指针来区分身份。

**练习 2**：同一个 slang 符号，在 reentrant 函数的不同调用层级会得到几个不同的 `Variable`？

> **答案**：会得到**多个**不同的 `Local` `Variable`——它们 `kind` 都是 `Local`、`symbol` 指针相同，但 `depth`（嵌套层级）不同，于是 `hash_label` 不同，在按位映射表里互不覆盖。这正是引入 `depth` 的意义。

---

### 4.2 VariableBit / VariableChunk / VariableBits：位级「HDL 意图」片段

#### 4.2.1 概念说明

`Variable` 描述「这是哪个变量」，但过程块里的赋值常常只动变量的**一部分**——`a[3] = 1`、`a[7:4] = x`、`{a[1:0], b}` 等。于是需要位级的「HDL 意图」片段，它们和 RTLIL 那一组一一对应，文件注释说得明明白白，参见 [src/variables.h:51-52](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/variables.h#L51-L52)：

> `VariableChunk` is to `VariableBit` what `RTLIL::SigChunk` is to `RTLIL::SigBit`.

也就是说：

- `VariableBit` ↔ `RTLIL::SigBit`：一个抽象的「位」。
- `VariableChunk` ↔ `RTLIL::SigChunk`：同属一个变量、地址连续的一段位。
- `VariableBits` ↔ `RTLIL::SigSpec`：一串位（可能来自不同变量、不连续）。

**关键差别**：`RTLIL::SigSpec` 最终指向画布上**真实存在的线**；而 `VariableBit` 只指向「某个 `Variable` 的第 `offset` 位」，**不承诺那根线已经存在**。这正是它能当「HDL 意图」的原因——它是抽象的、可比较的、可哈希的，物化推迟到最后一步。

#### 4.2.2 核心流程：一个位的身份与一段 chunk

`VariableBit` 极简，参见 [src/variables.h:20-49](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/variables.h#L20-L49)：

```cpp
struct VariableBit {
    Variable variable;
    uint64_t offset;
    typedef std::tuple<Variable, uint64_t> Label;
    Label label() const { return std::make_tuple(variable, offset); }
    bool operator==(const VariableBit &other) const { return label() == other.label(); }
    bool operator<(const VariableBit &other) const { return label() < other.label(); }
    ...
};
```

它的全部身份就是 `(variable, offset)` 这对值。相等与大小比较都基于 `label()` 这个 tuple——这意味着同一个 `Variable` 的不同位天然有序（按 `offset` 排），不同 `Variable` 之间则按 `Variable::operator<` 的确定序排列。`hash_into`/`hash` 也吃这个 tuple，所以 `VariableBit` 可以直接当 `Yosys::dict`/`pool` 的键。

`VariableChunk` 是「同变量、地址连续的一段」，参见 [src/variables.h:53-84](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/variables.h#L53-L84)：

```cpp
struct VariableChunk {
    Variable variable;
    uint64_t base;
    uint64_t length;
    uint64_t bitwidth() const { return length; }
    VariableBit operator[](uint64_t key) const {
        log_assert(key < length);
        return VariableBit{variable, base + key};   // chunk 内按相对下标取位
    }
    ...
};
```

它用 `(variable, base, length)` 三元组描述一段，并提供 `operator[]` 把相对下标换算成绝对 `offset` 的 `VariableBit`。

#### 4.2.3 核心流程：VariableBits 的 chunk / bits 双态存储

`VariableBits` 是本讲最精巧的部分。它用 `std::variant` 在两种存储形态间二选一，参见 [src/variables.h:86-90](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/variables.h#L86-L90)：

```cpp
class VariableBits {
    std::variant<VariableChunk, std::vector<VariableBit>> storage_;
    bool is_chunk() const { return std::holds_alternative<VariableChunk>(storage_); }
    ...
};
```

两种形态是：

1. **chunk 态**：`storage_` 持有一个 `VariableChunk`。此时整串位**同属一个变量、地址连续**，用 `(variable, base, length)` 三个数就能表达任意宽度。这是一个「紧凑表示」，即使 1000 位也只占常数空间。
2. **bits 态**：`storage_` 持有一个 `std::vector<VariableBit>`。此时位来自不同变量、或不连续，必须逐位记录。

为什么搞两种形态？因为绝大多数左值是「一整段连续变量」或「几段连续片段」，chunk 态又省内存又便于迭代；只有发生拼接、混入不同变量时才需要摊平。从 chunk 态到 bits 态的「摊平」由 `unpack()` 完成，参见 [src/variables.h:101-111](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/variables.h#L101-L111)：

```cpp
void unpack() {
    if (!is_chunk()) return;            // 已经是 bits 态，直接返回
    VariableChunk chunk = as_chunk();
    std::vector<VariableBit> bits;
    bits.reserve(chunk.length);
    for (uint64_t i = 0; i < chunk.length; i++)
        bits.push_back(chunk[i]);
    storage_ = std::move(bits);          // 切换到 bits 态
}
```

`unpack()` 是单向的：一旦摊平就不再合并回 chunk。很多会改变内容的操作（`append` 跨变量、`remove`、`sort`、`reserve`）都先调 `unpack()` 确保自己在 bits 态下工作。

构造函数最能体现「优先 chunk 态」的策略，参见 [src/variables.h:113-129](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/variables.h#L113-L129)：

```cpp
VariableBits() : storage_(VariableChunk{{}, 0, 0}) {}                // 空 = 长度0的chunk
VariableBits(const VariableBit &bit) : storage_(VariableChunk{bit.variable, bit.offset, 1}) {}
VariableBits(const VariableChunk &chunk) : storage_(chunk) {}
VariableBits(const Variable &variable)
    : storage_(VariableChunk{variable, 0, variable.bitwidth()}) {}   // 整个变量=一段chunk
```

注意即便从单个 `VariableBit` 构造，也存成「长度 1 的 chunk」而非「单元素 vector」——尽量停留在 chunk 态。

`append` 是双态维护的主战场，它会在「能续成连续 chunk」时原地扩展，否则摊平，参见 [src/variables.h:163-178](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/variables.h#L163-L178)（追加单个位）与 [src/variables.h:180-207](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/variables.h#L180-L207)（追加另一段 `VariableBits`）：

```cpp
void append(const VariableBit bit) {
    if (is_chunk()) {
        auto &chunk = as_chunk();
        if (chunk.length == 0) { chunk = {bit.variable, bit.offset, 1}; return; }
        if (bit.variable == chunk.variable && bit.offset == chunk.base + chunk.length) {
            chunk.length++;                  // 正好续上：原地扩展，仍是 chunk 态
            return;
        }
        unpack();                            // 续不上：摊平
    }
    as_bits().push_back(bit);
}
```

这段逻辑保证：拼接 `a` 的连续位不会触发摊平，而一旦 `a` 后面接 `b` 的位，就一次性摊平成 vector。

#### 4.2.4 核心流程：两种迭代器——逐位与「合并段」

`VariableBits` 提供两套迭代：

**逐位迭代**（`begin()`/`end()` 返回 `const_bit_iterator`），参见 [src/variables.h:263-301](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/variables.h#L263-L301)。它对两种态统一：`operator*` 调 `(*container)[index]`，而 `operator[]` 在 chunk 态用 `chunk[index]`、bits 态用 `vector[index]`（参见 [src/variables.h:141-147](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/variables.h#L141-L147)）。注意它是 **random-access** 迭代器，但每解一次引用都要算一次 `chunk[index]`，对 chunk 态并不「贵」。

**「合并段」迭代**（`chunks()` / `chunk_spans()`）更值得注意。即便底层已经是 bits 态（位可能来自任意位置），它也会在遍历时**把相邻同变量、同地址的位重新合并成 `VariableChunk`** 再吐出来。核心是 `iterator_base::fixup_chunk()`，参见 [src/variables.h:338-346](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/variables.h#L338-L346)：

```cpp
void fixup_chunk() {
    auto &bits = container.as_bits();
    while (offset + chunk.length < bits.size() &&
            bits[offset + chunk.length].variable == chunk.variable &&
            bits[offset + chunk.length].offset == chunk.base + chunk.length) {
        chunk.length++;                      // 向后贪心合并连续位
    }
}
```

也就是说，`chunks()` 总是给出「最大化的连续段」。这一点非常重要：下游的解析代码（4.3 节）拿到的是「段」而非「散位」，可以一段一段地批量处理，把「同一段静态变量」一次性映射成一段连续信号，效率高得多。`chunk_spans()` 还额外带上每段在整体里的 `(offset, length)` 位置，方便对齐到右值的对应片段。

#### 4.2.5 源码精读：Dummy 探测与 special net 探测

两个工具方法说明了 `VariableBits` 如何被用来做「性质查询」。

`has_dummy_bits()` 判断这串位里有没有占位 `Dummy`，参见 [src/variables.h:250-259](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/variables.h#L250-L259)：

```cpp
bool has_dummy_bits() const {
    if (is_chunk())
        return as_chunk().variable.kind == Variable::Dummy;
    for (auto chunk : chunks())
        if (chunk.variable.kind == Variable::Dummy) return true;
    return false;
}
```

它直接利用了「chunk 态只有一个变量」的事实——chunk 态只需看那一个变量的 `kind`。

`has_special_nets()` 判断是否含 wand/wor 这类「特殊线网」（多驱动需特殊合并），实现放在 .cc 里，参见 [src/variables.cc:246-253](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/variables.cc#L246-L253)，借助 `Variable::is_special_net()`（[src/variables.cc:236-244](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/variables.cc#L236-L244)）逐段判断 `NetSymbol` 的 `netType`。这类查询之所以放在 `VariableBits` 上，是因为调用方手里往往只有 `VariableBits`（一组左值位），需要据此决定走哪条解析分支。

#### 4.2.6 代码实践：观察 chunk 态何时被摊平

1. **实践目标**：用 `tests/unit/complex_lhs.sv` 里的赋值，预测每个左值会构造出 chunk 态还是 bits 态的 `VariableBits`。
2. **操作步骤**：
   - 阅读 [tests/unit/complex_lhs.sv:7-14](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/complex_lhs.sv#L7-L14)。

     ```systemverilog
     a = bg;                 // (1) 整个 a：4位连续
     a[sel] = data;          // (2) 动态位选：lhs() 不处理，走 AddressingResolver（u4-l3）
     {c[sel], d[~sel]} = ... // (3) 含动态位选的拼接：同样不在 lhs() 范围内
     ```
   - 只看能被 `EvalContext::lhs` 静态处理的情形，比如把 (1) 换成 `{a[1:0], a[3:2]} = bg`（纯静态拼接）。
3. **需要观察的现象**：纯整段赋值 `a = bg` 构造出的 `VariableBits` 自始至终是 **chunk 态**（`VariableChunk{a, 0, 4}`）；而静态拼接 `{a[1:0], a[3:2]}` 在追加第二段时，因为第二段的 `base`（2）正好等于第一段 `base+length`（0+2），会被 `append` **原地合并**成长度 4 的 chunk，仍不摊平。只有拼接**不同变量**（如 `{a, b}`）才触发 `unpack()`。
4. **预期结果**：同变量连续片段永远留在 chunk 态；跨变量拼接进入 bits 态。这正是双态存储的意义。
5. **待本地验证**：如需眼见为实，可在 [src/variables.h:175](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/variables.h#L175) 的 `unpack()` 里加一行 `log("VariableBits unpacked");`，跑 `complex_lhs.sv` 观察触发时机。

#### 4.2.7 小练习与答案

**练习 1**：`VariableBits` 为什么不直接用一个 `std::vector<VariableBit>` 算了？

> **答案**：为了效率。常见的左值是一整段连续变量（如 `a`、`a[7:0]`），用单个 `VariableChunk`（三个数）就能表达任意宽度，省去 vector 的堆分配与逐位存储；只有真正跨变量或不连续时才摊平。同时，`chunks()` 迭代器即便在 bits 态也会重新合并连续段，让下游能按段批量处理。

**练习 2**：`VariableBit` 和 `RTLIL::SigBit` 在「指向什么」上有何本质区别？

> **答案**：`RTLIL::SigBit` 指向画布上**已存在**的一根线（或线上的某一位 / 一个常量），是「物化后」的信号；`VariableBit` 只指向「某个 `Variable` 的第 `offset` 位」，**不承诺**对应的线已存在。所以 `VariableBit` 适合在过程块翻译期当抽象左值和哈希键，等需要真实信号时再解析。

---

### 4.3 EvalContext::variable 与解析点：从抽象左值到真实信号

#### 4.3.1 概念说明

前面两节造出了一张张「身份卡」和一串串「位片段」，但它们终究要在某个时刻变成 `RTLIL::SigSpec`。这中间有两个关键环节：

1. **入口**：`EvalContext::variable(symbol)`——把一个 slang `ValueSymbol` 包装成 `Variable`（必要时带嵌套层级）。它是「拿到身份卡」的标准入口。
2. **解析（resolve）点**：把 `Variable`/`VariableBits` 翻译成 `RTLIL::SigSpec` 的地方。主要有两处：
   - `NetlistContext::convert_static`：把 `Static` 变量物化成画布上的线，是**非过程**上下文（连续赋值、端口连接等）的解析点。
   - `VariableState::evaluate`：过程块里读变量时，先查「按位可见赋值表」，查不到再回退到静态线。

`EvalContext` 本身上一讲（u3-l1）已提过：它是 `NetlistContext` 持有的求值器（`netlist.eval`），负责把 slang 表达式求值成 `RTLIL::SigSpec`。本节聚焦其中与 `Variable` 相关的部分。

#### 4.3.2 核心流程：EvalContext::variable 怎么算 depth

`EvalContext::variable` 是 `from_symbol` 的薄包装，但多干了一件关键事——为 `Local` 变量算出正确的 `depth`。参见 [src/slang_frontend.cc:599-607](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L599-L607)：

```cpp
Variable EvalContext::variable(const ast::ValueSymbol &symbol)
{
    if (ast::VariableSymbol::isKind(symbol.kind) &&
            symbol.as<ast::VariableSymbol>().lifetime == ast::VariableLifetime::Automatic) {
        return Variable::from_symbol(&symbol, find_nest_level(symbol.getParentScope()));
    } else {
        return Variable::from_symbol(&symbol);   // depth 默认 -1 → Static
    }
}
```

`find_nest_level` 沿着父作用域链向上找最近登记过层级的 scope，参见 [src/slang_frontend.cc:588-597](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L588-L597)：

```cpp
int EvalContext::find_nest_level(const ast::Scope *scope)
{
    const ast::Scope *upper_scope = scope;
    while (upper_scope && !scope_nest_level.count(upper_scope))
        upper_scope = upper_scope->asSymbol().getParentScope();
    ast_invariant(scope->asSymbol(), upper_scope != nullptr);
    return scope_nest_level.at(upper_scope);
}
```

`scope_nest_level` 这张表由进入 automatic 作用域时（如调函数）通过 `EnterAutomaticScopeGuard`（声明见 [src/slang_frontend.h:198-205](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L198-L205)）登记。注释点明了它的目的，参见 [src/slang_frontend.h:150-152](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L150-L152)：「隔离 reentrant 作用域（即函数）的 automatic 变量」。同一段函数体被递归调用时，不同调用层级会得到不同 `depth`，从而让各层的局部变量互不串台。

#### 4.3.3 核心流程：左值的入口 EvalContext::lhs

过程块里分析左值的标准入口是 `EvalContext::lhs`，它返回 `VariableBits`。参见 [src/slang_frontend.cc:620-641](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L620-L641)：

```cpp
VariableBits EvalContext::lhs(const ast::Expression &expr, bool silent)
{
    ast_invariant(expr, expr.kind != ast::ExpressionKind::Streaming);  // 流拼接另走 streaming_lhs
    auto analyzed_lvalue = LValue::analyze(*this, expr, silent);
    if (!analyzed_lvalue)
        return Variable::dummy(expr.type->getBitstreamWidth());        // 分析失败 → Dummy 占位
    if (!analyzed_lvalue->is_static()) {
        if (!silent) netlist.add_diag(diag::UnsupportedLhs, expr.sourceRange);
        return Variable::dummy(expr.type->getBitstreamWidth());        // 非静态（含动态寻址）→ Dummy + 诊断
    }
    VariableBits ret = analyzed_lvalue->evaluate_vbits();
    log_assert(ret.bitwidth() == expr.type->getBitstreamWidth());
    return ret;
}
```

这里有两个要点：

- **`lhs()` 只负责「静态」左值**。它把表达式交给 `LValue::analyze`（u4-l2 的主题）做结构化分析；若分析结果不是静态的（比如含 `a[i]` 这种动态索引），就发 `UnsupportedLhs` 诊断并返回一个 `Dummy`。**动态寻址和流拼接由调用方另行处理**（分别走 u4-l3 的 `AddressingResolver` 和 `streaming_lhs`），这正是函数上方注释的承诺，参见 [src/slang_frontend.h:173-177](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L173-L177)。
- **`Dummy` 在这里充当「失败兜底」**：宽度仍然正确（等于表达式位宽），但内容是 x，让后续流程不至于崩在空值上。

#### 4.3.4 源码精读：解析点之一——convert_static

`NetlistContext::convert_static` 是**非过程上下文**把 `VariableBits` 物化成真实信号的解析点，参见 [src/slang_frontend.cc:3394-3418](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3394-L3418)：

```cpp
RTLIL::SigSpec NetlistContext::convert_static(VariableBits bits)
{
    RTLIL::SigSpec ret;
    for (auto vchunk : bits.chunks()) {                 // 用「合并段」迭代，一段一段处理
        switch (vchunk.variable.kind) {
        case Variable::Static: {
            const RTLIL::SigSpec &signal = wire(*vchunk.variable.get_symbol());  // 拿到变量对应的线
            if (signal.is_chunk())
                ret.append(signal.as_chunk().extract((int)vchunk.base, (int)vchunk.length));
            else
                ret.append(signal.extract((int)vchunk.base, (int)vchunk.length)); // 截取对应位段
            break;
        }
        case Variable::Dummy:
            ret.append(add_placeholder_signal(vchunk.length, "dummy"));  // 占位线
            break;
        default:
            log_abort();                                 // Local/EscapeFlag 不该出现在这里
        }
    }
    return ret;
}
```

两个细节值得品味：

- 它用 `bits.chunks()` 而非逐位迭代，从而**按连续段**一次性 `extract` 出信号——这正是 4.2.4 节「合并段迭代器」的用武之地。
- 它**只接受 `Static` 与 `Dummy`**：`Static` 变成画布上的线段，`Dummy` 变成一根匿名占位线（`add_placeholder_signal`）。`Local`/`EscapeFlag` 在非过程上下文里不该出现，故 `log_abort()`。

#### 4.3.5 源码精读：解析点之二——VariableState::evaluate

在**过程块**里读一个变量，走的是另一条路：先查「按位可见赋值表」`visible_assignments`，查不到才回退到静态线。这就是 `VariableState::evaluate`，参见 [src/slang_frontend.cc:529-543](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L529-L543)：

```cpp
RTLIL::SigSpec VariableState::evaluate(NetlistContext &netlist, VariableBits vbits)
{
    RTLIL::SigSpec ret;
    for (auto vbit : vbits) {                            // 这里逐位处理
        if (vbit.variable.kind == Variable::Dummy) {
            ret.append(RTLIL::Sx);                       // Dummy → 常量 x
        } else if (visible_assignments.count(vbit)) {
            ret.append(visible_assignments.at(vbit));    // 该位被本过程赋过值 → 用赋值结果
        } else {
            log_assert(vbit.variable.kind == Variable::Static);
            ret.append(netlist.wire(*vbit.variable.get_symbol())[(int)vbit.offset]);  // 否则回退到静态线
        }
    }
    return ret;
}
```

`VariableState` 是 `ProceduralContext` 里的成员（声明见 [src/slang_frontend.h:320-333](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L320-L333)），它的 `visible_assignments` 是一张 `Yosys::dict<VariableBit, RTLIL::SigBit>`——**键正是 `VariableBit`**。这张表记录「在当前 case 分支里，这一位变量被赋成了什么信号」。写入由 `VariableState::set` 完成，参见 [src/slang_frontend.cc:511-527](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L511-L527)：

```cpp
void VariableState::set(VariableBits lhs, RTLIL::SigSpec value)
{
    log_assert(lhs.bitwidth() == (uint64_t)value.size());
    for (uint64_t i = 0; i < lhs.bitwidth(); i++) {
        VariableBit bit = lhs[i];
        if (!revert.count(bit)) {                       // 记录原值，便于分支结束时 restore
            if (visible_assignments.count(bit)) revert[bit] = visible_assignments.at(bit);
            else                          revert[bit] = RTLIL::Sm;   // Sm 表示「原本不存在」
        }
        visible_assignments[bit] = value[i];            // 用左值的 VariableBit 作键
    }
}
```

`set` 用 `VariableBit` 当键逐位写入，并把旧值存进 `revert` 以便离开 `if` 分支时回滚（`restore`，参见 [src/slang_frontend.cc:559-586](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L559-L586)）。这正是「过程块左值必须抽象」的根本原因——同一个变量在不同分支有不同映射，必须用「抽象位」当键先攒着，最后再 lower。

最后看一眼**读取分派**：`EvalContext::operator()` 处理 `NamedValue`/`HierarchicalValue` 时，会判断当前是否在过程块里，决定走哪条解析点，参见 [src/slang_frontend.cc:1311-1322](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1311-L1322)：

```cpp
Variable variable1 = variable(symbol.as<ast::ValueSymbol>());
log_assert((bool) variable1);
if (procedural && (!in_sva_expression || variable1.kind != Variable::Static)) {
    ...
    ret = procedural->substitute_rvalue(variable1);     // 过程块：走 substitute_rvalue（内部用 VariableState）
} else {
    ret = netlist.convert_static(variable1);            // 非过程：直接物化静态线
}
```

非过程上下文 → `convert_static`；过程块 → `substitute_rvalue`（内部对 `Initial` 过程用 `initial_locals_state`/`initial_state`，对其余过程用 `VariableState` 的可见赋值表，实现见 [src/procedural.cc:313](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L313) 起）。

#### 4.3.6 代码实践：回答核心问题——为什么不直接用 SigSpec

1. **实践目标**：用本节学到的两个解析点，亲口解释「为什么 sv-elab 不直接用 `RTLIL::SigSpec` 表示过程块左值」，并指出 `VariableBits` 何时被解析成真实信号。
2. **操作步骤**：
   - 打开 [src/slang_frontend.cc:511-527](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L511-L527)（`VariableState::set`）与 [src/slang_frontend.cc:529-543](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L529-L543)（`evaluate`）。
   - 假设 `RTLIL::SigSpec` 直接当左值：在 `if` 分支里给 `a` 赋了某个临时信号，离开分支要回滚时，你**没有**一个稳定的「变量 a 的第 3 位」键可查——`SigSpec` 已经被合并、可能指向别处，也无法区分「这次赋值 vs 上一次赋值」。而 `VariableBit` 始终是 `(Variable, offset)`，是稳定键。
3. **需要观察的现象**：`visible_assignments` 与 `revert` 的键类型都是 `VariableBit`（见 [src/slang_frontend.h:321](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L321)）；上一讲提到的 `driven_variables`、`special_net_drivers`、`initial_state` 也都以 `VariableBit` 为键（[src/slang_frontend.h:573-583](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L573-L583)）。
4. **预期结果**（参考答案，见 4.3.7）。
5. **待本地验证**：无运行步骤，纯源码阅读型实践。

#### 4.3.7 小练习与答案

**练习 1**：列出 `VariableBits` 被解析成真实信号的两个主要代码点，并说明各自适用什么上下文。

> **答案**：(1) `NetlistContext::convert_static`（[src/slang_frontend.cc:3394](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3394)）——非过程上下文，把 `Static` 变量物化成画布线段，`Dummy` 物化成占位线；(2) `VariableState::evaluate`（[src/slang_frontend.cc:529](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L529)）——过程块读变量，先查 `visible_assignments` 命中则用赋值结果，未命中且为 `Static` 则回退到静态线，`Dummy` 解析为常量 `Sx`。

**练习 2**：为什么 `convert_static` 对 `Local`/`EscapeFlag` 直接 `log_abort()`？

> **答案**：`convert_static` 处理的是「已经物化、有持久线」的变量，只有 `Static` 满足；`Local` 变量在过程块里靠 `VariableState` 的按位映射临时持有值、没有持久线，`EscapeFlag` 是控制流辅助信号——它们都不该出现在非过程的静态解析路径里，出现即代表调用方逻辑错误，故直接 abort。

**练习 3**：`EvalContext::lhs` 遇到 `a[i]`（动态索引）时会返回什么？

> **答案**：因为 `a[i]` 不是静态左值，`LValue::analyze` 的 `is_static()` 返回 false，`lhs()` 会（在非 silent 模式下）发出 `diag::UnsupportedLhs` 诊断，并返回一个 `Variable::dummy(expr.type->getBitstreamWidth())` 构造的 `VariableBits`。真正的动态寻址由调用方另行走 `AddressingResolver`（u4-l3）处理。

## 5. 综合实践

把本讲的三块知识串起来，做一次「**从 slang 符号到真实信号的全程追踪**」。

**任务**：给定下面这段「示例代码」（非项目原有，仅用于练习追踪）：

```systemverilog
module m(input logic [3:0] bg, output logic [3:0] q);
    logic [3:0] a;                  // (A) 模块级变量
    always_comb begin
        a = bg;                     // (B) 静态整段赋值
        q = a + 4'd1;               // (C) 读 a，再赋给端口 q
    end
endmodule
```

请按下列步骤追踪，每一步都给出**涉及的代码行**与**变量的 `Kind`/存储态**：

1. **(A) 声明**：`a` 作为模块级 `logic`，当某处调用 `EvalContext::variable(a的符号)` 时，走 [src/slang_frontend.cc:599-607](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L599-L607) 的哪个分支？得到什么 `Kind`、`depth`？
2. **(B) 左值 `a`**：`EvalContext::lhs`（[src/slang_frontend.cc:620](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L620)）返回的 `VariableBits` 是 chunk 态还是 bits 态？为什么？（提示：整段连续变量 → 单 chunk）
3. **(B) 写入**：这次赋值最终调用 `VariableState::set`（[src/slang_frontend.cc:511](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L511)），它往 `visible_assignments` 里写入了哪几个键？键的类型是什么？
4. **(C) 读 `a`**：在 `q = a + 1` 里读 `a` 时，`EvalContext::operator()` 走 [src/slang_frontend.cc:1313-1322](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1313-L1322) 的过程块分支，调用 `substitute_rvalue`，最终在 `VariableState::evaluate`（[src/slang_frontend.cc:529](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L529)）里命中 `visible_assignments` 还是回退到 `netlist.wire(...)`？为什么？（提示：`a` 在本过程里刚被赋值过）

**参考结论**：(1) `Static`，`depth=-1`；(2) chunk 态，`VariableChunk{a,0,4}`；(3) 写入 4 个 `VariableBit` 键 `(a,0)..(a,3)`，类型 `VariableBit`；(4) 命中 `visible_assignments`，返回 (B) 中写入的信号（即 `bg`），而不是回退到 `a` 的线——这体现了「过程块里左值的抽象映射优先于静态线」。

完成本任务后，你应当能完整复述：一个变量的符号如何变成 `Variable`、如何拼成 `VariableBits`、如何作为键被 `VariableState` 记录、最后如何在读回时被解析成真实信号。

## 6. 本讲小结

- `Variable` 是一张「身份卡」，用 `Kind`（`Static`/`Local`/`EscapeFlag`/`Dummy`/`Invalid`）给变量分类，并用 union 共享 `symbol`/`width`/`id` 三种存储；`from_symbol` 据 `VariableLifetime::Automatic` 判定 `Local` 还是 `Static`。
- `Local` 变量带一个 `depth`（嵌套层级），使 reentrant 函数不同调用层级的同名局部变量互不混淆；`hash_label` 把 `(kind, 指针/数值, depth)` 打包成稳定的哈希/相等键。
- `VariableBit`/`VariableChunk`/`VariableBits` 是与 `RTLIL::SigBit`/`SigChunk`/`SigSpec` 平行的位级抽象，但**不指向已存在的线**，只描述「某 `Variable` 的某些位」。
- `VariableBits` 用 `std::variant` 在「一段连续 chunk」与「逐位 vector」间二选一，优先停留在紧凑的 chunk 态，只有跨变量拼接等情形才 `unpack()` 摊平；`chunks()` 迭代器总是给出最大化的连续段。
- `EvalContext::variable` 是拿 `Variable` 的标准入口，为 `Local` 变量查 `scope_nest_level` 算 `depth`；`EvalContext::lhs` 只处理静态左值，动态寻址与流拼接交由调用方另走他路。
- `VariableBits` 的两个解析点是 `NetlistContext::convert_static`（非过程，`Static`→线段、`Dummy`→占位线）与 `VariableState::evaluate`（过程块，先查 `visible_assignments` 再回退静态线）。
- 引入 `VariableBits`（而非直接用 `RTLIL::SigSpec`）的根本原因：过程块的 case 树需要「抽象、稳定、可哈希、可回滚」的按位键来记录「某分支里某变量某位被赋成什么」，而 `SigSpec` 既过早物化又不适合当键。

## 7. 下一步学习建议

- **u3-l4（Case 与 Switch）**：本讲反复提到「过程块用 case 树」，下一讲正式拆解 `Case::Action` 里的 `lvalue`/`mask`/`unmasked_rvalue`，你会看到 `VariableBits` 如何作为 case 动作的左值落地。建议先读 [src/cases.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/cases.h)。
- **u4-l2（LValue：左值的结构化分析）**：本讲多次引用 `LValue::analyze` 与 `evaluate_vbits`，那是左值分析的完整 machinery，含拼接、范围选择、成员访问等 descriptor。
- **u4-l3（AddressingResolver）**：本讲指出动态索引 `a[i]` 不走 `lhs()`，那里就是它的归宿——看 demux/mux 如何处理动态位选。
- **u5-l1（ProceduralContext 与 VariableState）**：本讲的 `VariableState::set/evaluate/restore` 是过程块建模的核心数据结构，u5-l1 会把它和 `save`/`restore` 的分支回滚机制讲透。
