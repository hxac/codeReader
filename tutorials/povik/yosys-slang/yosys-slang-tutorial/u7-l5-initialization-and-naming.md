# 变量初始化与命名生成

## 1. 本讲目标

本讲聚焦 sv-elab（原 yosys-slang）翻译流程中两个看似琐碎、实则贯穿全局的收尾主题：**变量的初值如何落到 RTLIL**，以及**网表里的每根线、每个单元的名字从哪里来**。

学完后你应当能够：

- 说清一行 `reg [7:0] a = 8'h63;` 的初值 `8'h63` 经历了哪两个阶段、最终变成 RTLIL 里的什么东西。
- 区分「寄存器驱动的位」「组合驱动的位」「完全没人驱动的位」三种情况在收尾时的不同处理。
- 理解结构体/数组变量在被触发器驱动时，如何得到形如 `$driver$\s.a` 的人类可读子字段名。
- 掌握 `NetlistContext::id` / `hdlname` 的转义规则，并能解释为什么 RTLIL 里既有 `\a` 又有 `$driver$\a` 这样的名字。

本讲是单元 7 的收尾篇，依赖 [u3-l3 变量与 VariableBits](u7-l5-initialization-and-naming.md) 建立的「HDL 意图位级表示」认知：初值与命名都围绕 `VariableBit` / `VariableChunk` 这组抽象键展开。

## 2. 前置知识

在进入源码前，先建立三条直觉。

**直觉一：初值不是「一次性写下去」的，而是「先记小本本，再统一结账」。**
SystemVerilog 里初值来源很杂：变量声明带的 `= 初值`、`initial` 块里的赋值、输出端口的默认值……它们散落在 AST 各处。sv-elab 不愿意在每遇到一处就立刻往 RTLIL 里塞一条连线，而是先用一张「按位记录」的表 `initial_state` 把「这个变量的第 i 位初值是什么」存起来，等整个模块体翻译完，再统一决定每位的初值该怎么落地。这样能避免「初值」和「真实驱动」互相打架。

**直觉二：初值的落地方式取决于这位「被谁驱动」。**
- 如果某位被触发器/锁存器驱动（register_driven），它的初值应当挂成那根线的 `init` 属性——这是 Yosys 表达「上电复位值」的标准方式。
- 如果某位被组合逻辑驱动，初值没意义（驱动源会覆盖它），直接忽略。
- 如果某位根本没人驱动，就把初值当作一根「常量驱动」接上去。

**直觉三：名字分「内部名」和「展示名」两套。**
Yosys RTLIL 里，以 `$` 开头的名字（如 `$1`、`$add$3`）是「私有/内部名」，会被各种优化 pass 自由改名、合并；而不以 `$` 开头的名字（如 `a`、`clk`）是「公共名」，对应源码里用户写的标识符，应当尽量保留。为了让公共名在 RTLIL 字符串表示里和私有名区分开，Yosys 用反斜杠 `\` 做转义前缀。sv-elab 的 `id()` 产出转义后的内部名，`hdlname()` 产出给用户看的原始 HDL 层次名。

> 名词速查：`VariableBit`/`VariableChunk`/`VariableBits` 是 sv-elab 描述「某变量的某些位」的轻量键（详见 u3-l3）；`bitstream`（位流）是 sv-elab 全程统一的位宽度量，把结构体字段、数组元素都拍平成连续位序列后从 0 开始编号。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/initialization.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/initialization.cc) | 初值的「记账」与「结账」两个函数，是本讲主角 |
| [src/naming.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/naming.cc) | 把一段位流按结构体/数组类型切成带名字的子字段 |
| [src/slang_frontend.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc) | `id`/`hdlname`/`convert_static`/`add_wire` 的实现，以及初值函数的调用点 |
| [src/builder.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc) | `new_id`/`add_placeholder_signal`，私有名的生成器 |
| [src/procedural.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc) | `update_variable_state` 的 `Initial` 分支——初值如何写进 `initial_state` |

## 4. 核心概念与源码讲解

### 4.1 evaluate_decl_initializers：求值声明初值

#### 4.1.1 概念说明

`reg [7:0] a = 8'h63;` 里 `= 8'h63` 这部分叫「声明初值（declared initializer）」。sv-elab 在进入一个实例体（realm）的主翻译之前，会先扫一遍所有静态生存期、固定位宽的变量，把它们的声明初值求出来。

关键设计：**这个阶段不直接生成任何 RTLIL 单元或连线**，而是把每位初值写进一张「按位记账表」`netlist.initial_state`。等后续主翻译跑完（其中可能还有 `initial` 块进一步改写初值），再由 4.2 的函数统一结账。

为什么用 `VariableBit` 当键？因为此刻变量对应的真实 RTLIL 线虽然已经建好（由更早的 `add_internal_wires` 创建），但「初值」属于 HDL 意图，用稳定的位级键记录可以和 u3-l3、u5 的过程块记账体系无缝衔接。

#### 4.1.2 核心流程

```
evaluate_decl_initializers(netlist)
  └─ visit_netlist_variables(netlist, [&](symbol) {    # 遍历静态固定位宽变量
        构造 ProceduralContext context(netlist, ProcessTiming::initial)   # 假装在 initial 块里
        找初值表达式 initializer：
            1) symbol.getInitializer()                 # 普通声明初值
            2) 否则查 output 端口的 backref 默认值
        if (有 initializer)
            context.do_simple_assign(... eval.variable(symbol), eval(*initializer), blocking=true)
        else if (类型非四态)
            do_simple_assign(... convert_const(类型默认值=0))   # 两态类型隐式赋 0
     })
```

`do_simple_assign` 最终走到 `update_variable_state`。因为这里 `timing.kind == ProcessTiming::Initial`，所以走的是 [src/procedural.cc:250-291](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L250-L291) 的「初值捷径」分支：不压 `Case::Action`、不建 `Process`，而是把每位常量直接写进 `netlist.initial_state`。

#### 4.1.3 源码精读

遍历助手 `visit_netlist_variables` 用一个 ASTVisitor 收集候选变量，并在 dissolve（展平）的子实例里继续下钻，但显式不进入过程块（过程块由别处翻译）：

[src/initialization.cc:23-45](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/initialization.cc#L23-L45) — 选出 `isFixedSize()` 且 `lifetime == Static` 的 `VariableSymbol`；接口（interface）端口的 modport 变量在 [src/initialization.cc:47-78](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/initialization.cc#L47-L78) 单独处理。

主函数体：

[src/initialization.cc:81-127](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/initialization.cc#L81-L127) — 对每个变量 new 一个 `ProceduralContext(... ProcessTiming::initial)`，这是「把声明初值当成 initial 过程里的一条阻塞赋值」的关键。关键片段（精简）：

```cpp
ProceduralContext context(netlist, ProcessTiming::initial);
if (initializer)
    context.do_simple_assign(symbol.location, context.eval.variable(symbol),
                             context.eval(*initializer), true);   // blocking=true
else if (!symbol.getType().isFourState())
    if (auto converted = netlist.convert_const(symbol.getType().getDefaultValue(), ...))
        context.do_simple_assign(..., *converted, true);          // 两态类型补 0
```

`eval.variable(symbol)` 把变量身份卡（u3-l3 的 `Variable`）取出来当左值，`eval(*initializer)` 把初值表达式求成 `RTLIL::SigSpec`（在 initial 语境下必然是常量，否则下一步会报错）。

`ProcessTiming::initial` 分支把初值写进记账表：

[src/procedural.cc:273-286](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L273-L286) — 对 `Static` 变量的每位执行 `netlist.initial_state[chunk[i]] = rvalue[...].data`；若该变量被识别为存储器，则另调 `add_memory_init` 发 `$meminit_v2`（衔接 [u7-l1 存储器推断](u7-l1-memory-inference.md)，本讲不展开）。

调用时机：在 `PopulateNetlist::handle(InstanceBodySymbol)` 里，先建内部线、再求初值、再跑主体、最后结账：

[src/slang_frontend.cc:2649-2666](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L2649-L2666) — 注意 `evaluate_decl_initializers` 在 `visitDefault(body)`（主体翻译）**之前**，`finalize_variable_initialization` 在**之后**。顺序很重要：主体里的 `initial` 块会覆盖/补充 `initial_state`，必须在结账前跑完。

#### 4.1.4 代码实践

**目标**：验证「声明初值先写进 `initial_state`，而非直接发连线」。

**步骤**：

1. 打开 [src/initialization.cc:97](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/initialization.cc#L97)，在 `ProceduralContext context(netlist, ProcessTiming::initial);` 下方临时加一行调试日志（**示例代码，仅用于观察，勿提交**）：

   ```cpp
   log("[init-debug] variable=%s initializer=%s\n",
       std::string(symbol.name).c_str(), initializer ? "yes" : "no");
   ```

2. 重新编译插件（参考 [u8-l3 构建系统](u8-l3-build-systems.md)）。

3. 用一段最小设计跑 `read_slang`：

   ```tcl
   read_slang <<EOF
   module top;
     reg [7:0] a = 8'h63;
     reg [7:0] b;        // 无初值，四态 reg
     reg logic [7:0] c;  // 两态，无初值
   endmodule
   EOF
   ```

**需要观察的现象**：日志应分别为 `a` 打印 `initializer=yes`，`b`/`c` 打印 `initializer=no`；其中 `c`（两态）会走到 `convert_const(getDefaultValue())` 的隐式补 0 分支，而 `b`（四态）既无初值、类型又是四态，于是什么都不写（保持 `Sx`）。

**预期结果**：`a` 的 8 位在 `initial_state` 里被记成 `0x63`；`c` 被记成 `0x00`；`b` 不在表里（读取时回退 `Sx`）。**待本地验证**：若你不在主线源码加日志，也可改用 4.2 的 `show` 观察最终网表反推。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `evaluate_decl_initializers` 要把 `ProceduralContext` 的 timing 设成 `ProcessTiming::initial`，而不是普通的组合型？

**答案**：因为声明初值在硬件语义上等价于「上电时执行一次」，正是 `initial` 过程的含义；设成 `initial` 后，`update_variable_state` 才会走 [src/procedural.cc:250](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L250) 的捷径分支，把常量写进 `initial_state`，而不是去建 `Process`/`Case::Action`。

**练习 2**：一个 `output reg` 端口没有显式初值，但它的端口声明里写了默认值，sv-elab 能取到吗？

**答案**：能。[src/initialization.cc:104-114](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/initialization.cc#L104-L114) 通过遍历 `getFirstPortBackref()` 链，找到 `direction == Out` 且带 `getInitializer()` 的端口，把它的默认值当作该变量的初值。

---

### 4.2 finalize_variable_initialization：把初值落到 RTLIL

#### 4.2.1 概念说明

主体翻译跑完后，`initial_state` 这张「小本本」记满了每位初值。`finalize_variable_initialization` 负责「结账」：把记账表里的内容翻译成 RTLIL 里看得见、摸得着的东西。

但它不是无脑地把每位都接成常量——那会和真实驱动冲突。它按「这位被谁驱动」分三种情况处理，这正是本模块的核心。

#### 4.2.2 核心流程

对每个静态变量（非存储器）的每一位：

```
state = initial_state[vbit]  (缺省 Sx)
if register_driven(vbit):        # 被触发器/锁存器驱动
    attr_value[i] = state        # 攒进 init 属性常量
elif driven(vbit):               # 仅被组合逻辑驱动
    (什么都不做，驱动源说了算)
else:                            # 没人驱动
    cl.append(signal[i]); cr.append(state)   # 攒进一条「接常量」连线
# 末尾：
if attr_value 非全 x:  wire->attributes[ID::init] = attr_value
netlist.connect(cl, cr)
```

> 注：代码里有一条不变量 `register_driven` 蕴含 `driven`（[src/initialization.cc:155-156](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/initialization.cc#L155-L156)），所以三分支互斥覆盖全部情况。

#### 4.2.3 源码精读

[src/initialization.cc:129-180](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/initialization.cc#L129-L180) — 结账主函数。关键片段（精简）：

```cpp
auto variable = Variable::from_symbol(&symbol);
if (netlist.is_inferred_memory(*variable_symbol)) {
    // 存储器交给 $meminit_v2，这里 Nothing to do
} else {
    auto signal = netlist.convert_static(variable);     // 变量→真实 SigSpec
    RTLIL::Const attr_value(RTLIL::Sx, signal.size());
    for (int i = 0; i < signal.size(); i++) {
        VariableBit vbit(variable, i);
        bool register_driven = netlist.register_driven_variables.count(vbit);
        bool driven = netlist.driven_variables.count(vbit);
        RTLIL::State state = netlist.initial_state.at(vbit, RTLIL::Sx);
        if (register_driven) {
            attr_value.set(i, state);                   // 情况①：攒 init 属性
        } else if (!driven) {
            cl.append(signal[i]); cr.append(state);     // 情况③：接常量
        }                                               // 情况②：组合驱动，跳过
    }
    if (!attr_value.is_fully_undef())
        wire->attributes[ID::init] = attr_value;        // 把 init 挂到线上
    netlist.connect(cl, cr);
}
```

这里 `convert_static(variable)`（[src/slang_frontend.cc:3394-3418](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3394-L3418)）把 u3-l3 的 HDL 意图键解析成真实 `RTLIL::SigSpec`：对 `Static` 变量，从 `wire_cache` 取出对应的线再做位段 `extract`。

`register_driven_variables` 这个集合是在翻译触发器/锁存器过程块时由 `register_driven` 填充的（见 [u6-l2](u6-l2-flipflop-and-async-reset.md)/[u6-l3](u6-l3-latch-inference.md)）。所以「这个变量是不是寄存器驱动」是结账前就已经确定的事实，本函数只是消费它。

#### 4.2.4 代码实践

**目标**：对比「寄存器驱动位」与「无人驱动位」在最终网表里的不同长相。

**步骤**：

1. 准备一段同时包含两种情况的设计：

   ```tcl
   read_slang <<EOF
   module top(input logic clk);
     reg [7:0] a = 8'h63;              // 寄存器驱动
     reg [7:0] lonely = 8'hff;         // 无人驱动
     always_ff @(posedge clk) a <= a + 8'd1;
   endmodule
   EOF
   show
   ```

2. 在生成的 RTLIL 里定位 `\a` 与 `\lonely` 两根线。

**需要观察的现象**：

- `\a` 被 `$dffe`（或 `$dff`）的 `Q` 驱动；它身上应带 `attribute \init 8'01100011`（即 `8'h63`）。这是情况①。
- `\lonely` 没有任何单元驱动它；它的初值 `8'hff` 应体现为一条 `connect \lonely 8'11111111`。这是情况③。

**预期结果**：寄存器驱动的 `\a` 用 `init` 属性表达上电值；无人驱动的 `\lonely` 用常量连线表达。这正是「初值落地方式取决于驱动者」的体现。**待本地验证**：具体单元名（`$dffe`/`$dff`）与 `$add` 是否被常量折叠掉，取决于你本地 Yosys 的 `proc`/优化结果。

#### 4.2.5 小练习与答案

**练习 1**：如果一个变量既被组合逻辑驱动、又写了初值，初值会出现在网表里吗？

**答案**：不会。在 [src/initialization.cc:165-168](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/initialization.cc#L165-L168) 的三分支里，`driven` 且非 `register_driven` 的位落入「情况②」，既不进 `attr_value` 也不进 `cl/cr`，被直接忽略——因为组合驱动源会在任何时刻覆盖初值，保留它没有意义。

**练习 2**：`attr_value` 初始化为全 `Sx`，最后用 `is_fully_undef()` 判断要不要挂 `init` 属性。为什么不是无条件挂？

**答案**：如果一个寄存器变量的所有位都缺初值（全 `Sx`），挂一个全 x 的 `init` 属性是冗余噪音；只有至少有一位带确定初值时才挂，能保持网表干净。

---

### 4.3 generate_subfield_names：结构体/数组的子字段命名

#### 4.3.1 概念说明

当一个结构体或数组变量被触发器驱动时（例如 `struct { logic [3:0] a; logic [3:0] b; } s;` 在 `always_ff` 里被整体赋值），sv-elab 会为它的每个子字段单独发一个触发器（见 [u6-l2 handle_ff_process](u6-l2-flipflop-and-async-reset.md)）。为了让这些触发器有可读的名字（比如 `$driver$\s.a`），需要一个函数：给定「被驱动的一段位流」和「变量的类型」，把它切成一组「带名字的子片段」。

这就是 `generate_subfield_names` 的职责。它的产出 `NamedChunk` 就是 `pair<VariableChunk, string>`（[src/slang_frontend.h:654](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L654)）——「这段位 + 它叫什么」。

#### 4.3.2 核心流程

递归函数 `subfield_names` 沿类型树下行，遇到三类节点分别处理：

```
subfield_names(chunk, type_offset, type, prefix):
    先用 bitstream 区间裁剪：chunk 与 [type_offset, type_offset+W) 不相交就 return
    if type 是 struct:
        for each 字段 field: 递归(prefix + "." + field.name, 偏移 += field_offset)
    elif type 是数组(非简单位向量):
        for each 下标 i:    递归(prefix + "[" + i + "]", 偏移 += i*stride)
    else:  # 叶子：标量/打包向量
        求 chunk 与本叶子的重叠 [lo, hi)
        若整段命中:  emit(chunk, prefix)
        若部分命中:  emit(子chunk, prefix + "[hi-1:lo]")
```

位流偏移是核心不变量。结构体字段在位流里的起点由 `bitstream_member_offset(field)` 给出（[src/slang_frontend.cc:189](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L189)），数组元素按步长 `stride = elementType.getBitstreamWidth()` 排列，且用 `range.translateIndex(i)` 处理升/降序范围：

\[ \text{elemOffset}(i) = \text{translateIndex}(i) \times \text{stride} \]

末尾还有一条断言保证切出来的所有子片段位宽之和等于输入 `chunk` 的位宽（[src/naming.cc:78-81](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/naming.cc#L78-L81)），即「不漏位、不重位」。

#### 4.3.3 源码精读

入口断言 + 调用递归：

[src/naming.cc:69-84](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/naming.cc#L69-L84) — 校验 chunk 与类型位宽一致后，从 `type_offset=0`、`prefix=""` 起步递归。

struct 分支：

[src/naming.cc:33-40](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/naming.cc#L33-L40) — 遍历 `FieldSymbol`，前缀拼 `.字段名`，偏移加上字段在位流里的位置。

数组分支：

[src/naming.cc:41-51](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/naming.cc#L41-L51) — 遍历每个下标，前缀拼 `[i]`。

叶子分支（部分命中时拼 `[hi-1:lo]`）：

[src/naming.cc:52-66](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/naming.cc#L52-L66) — 注意 `TODO: use hdl indices`，目前切片下标用的是位流零基下标而非源码里的 HDL 范围，这是已知的小限制。

消费方在 `handle_ff_process` 里：

[src/slang_frontend.cc:1944-1947](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1944-L1947) — 对每个命名子片段，用 `"$driver$" + unescaped_id(symbol) + name` 拼出触发器基名，再把对应的位段接成 `$dffe` 的 `D`/`Q`。于是结构体字段 `s.a` 的触发器就叫 `$driver$\s.a`。

#### 4.3.4 代码实践

**目标**：亲眼看到一个结构体变量的子字段触发器名字。

**步骤**：

1. 用一段结构体 + `always_ff` 设计：

   ```tcl
   read_slang <<EOF
   module top(input logic clk);
     typedef struct packed {
       logic [3:0] hi;
       logic [3:0] lo;
     } pair_t;
     pair_t p;
     always_ff @(posedge clk) p <= ~p;
   endmodule
   EOF
   show
   ```

2. 在输出里找名字含 `$driver$` 的单元。

**需要观察的现象**：应能看到两个触发器单元，名字大致形如 `$driver$\p.hi` 与 `$driver$\p.lo`（各 4 位），分别对应结构体的两个字段；而不是一个整体的 `$driver$\p`。

**预期结果**：`generate_subfield_names` 把 8 位的 `p` 切成 `p.hi`（高 4 位）和 `p.lo`（低 4 位）两个 `NamedChunk`，每个生成一个独立命名的触发器。**待本地验证**：确切计数器后缀（`$0`、`$1` 等）依构建而异。

#### 4.3.5 小练习与答案

**练习 1**：`subfield_names` 开头有两段区间裁剪 `if (chunk.base >= type_offset + W) return;` 和 `if (chunk.base + bitwidth <= type_offset) return;`，它们解决什么问题？

**答案**：递归会下钻到类型树的每个字段/元素，但被驱动的 `chunk` 往往只覆盖其中一部分。这两段判断在「该子类型与 chunk 完全不相交」时提前剪枝返回，避免无意义递归，同时保证相交的子片段一定会被 emit。

**练习 2**：为什么叶子分支里整段命中（`lo==0 && hi==width`）和部分命中要分开处理？

**答案**：整段命中时，子片段就是整个变量本身，名字直接用 `prefix`（如 `p.hi`），无需再带 `[h:l]` 切片后缀，名字更干净；部分命中时才需要用 `[hi-1:lo]` 标明切的是哪几位。

---

### 4.4 NetlistContext::id / hdlname：标识符的转义与生成

#### 4.4.1 概念说明

前三个模块都在讲「值」，本模块讲「名」。sv-elab 为每个符号（变量、实例、单元）生成两种名字：

- **`id(symbol)`**：RTLIL 内部用的标识符。公共名会被 `RTLIL::escape_id` 转义（加 `\` 前缀），用作线/单元的真实名字。
- **`hdlname(symbol)`**：用户可见的 HDL 层次名，作为 `hdlname` 属性挂在对象上，便于反查到源码。

两者底层都调用 `build_hiername`，差别仅在「分隔符」：`id` 用 `.`（点），`hdlname` 用空格。还有一个 `unescaped_id`，返回不带 `\` 转义的点分路径，供拼接派生名（如 `$driver$\p.hi`）使用。

此外还有一套「私有名」生成器 `new_id`，产出形如 `$1`、`$driver$foo$2` 的内部名，供那些没有对应 HDL 符号的中间线/单元使用。

#### 4.4.2 核心流程

```
id(symbol)        = escape_id(build_hiername(netlist, symbol, "."))
hdlname(symbol)   =       build_hiername(netlist, symbol, " ")
unescaped_id(...) =       build_hiername(netlist, symbol, ".")

build_hiername(symbol):
    build_hierpath2(从父作用域向上递归到 realm，用 sep 拼接)
    追加 symbol.name
    若是实例数组: 追加 [下标]

new_id(base):
    base 空  -> "$" + next_id++
    否则     -> "$" + base + "$" + next_id++
```

转义规则要点：Yosys 的 `RTLIL::escape_id` 对「不以 `$` 开头」的名字加 `\` 前缀（公共名），对以 `$` 开头的名字原样保留（私有名）。所以用户写的变量 `a` 在 RTLIL 文本里写作 `\a`，而 sv-elab 自造的中间量写作 `$1`、`$driver$...`。`new_id` 永远以 `$` 起头，故永远是私有名。

#### 4.4.3 源码精读

`id`/`hdlname`/`unescaped_id` 三件套：

[src/slang_frontend.cc:3098-3116](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3098-L3116) — `id` 多走一步 `RTLIL::escape_id`，另外两个不转义。

`build_hiername`：

[src/slang_frontend.cc:3077-3096](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3077-L3096) — 用 `build_hierpath2` 向上拼路径，再追加 `symbol.name`；对实例数组额外追加 `[下标]`。`build_hierpath2` 本身在 [src/slang_frontend.cc:2940-2961](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L2940-L2961) 递归到 `realm` 为止，并支持 `scopes_remap`（modport 重映射）。

私有名生成器 `new_id`：

[src/builder.cc:38-44](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L38-L44) — 用 `RTLILBuilder::next_id` 自增计数器拼出唯一名。

三者的交汇点在 `add_wire`——为变量建线时同时设好公共名和 `hdlname` 属性：

[src/slang_frontend.cc:3141-3160](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3141-L3160) — 关键几行（精简）：

```cpp
AttributeGuard guard(*this);
guard.set(ID::hdlname, hdlname(symbol));          // 展示名属性
transfer_attrs(*this, symbol, guard);
RTLIL::SigSpec sig = add_placeholder_signal(type.getBitstreamWidth(), id(symbol), true);
                                                   // public_name=true -> 用 id() 当线名
wire_cache[&symbol] = sig;
```

`add_placeholder_signal` 决定用「公共名」还是「私有名」：

[src/builder.cc:704-717](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L704-L717) — `public_name=true` 时用 `id(name_suggestion)`（转义公共名），否则用 `new_id(...)`（私有名）。

> 顶层模块名走另一条但同源的路径：`module_type_id` 在 [src/slang_frontend.cc:210-218](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L210-L218)，对特化实例会在模块名后拼 `$<层次路径>` 以区分。

#### 4.4.4 代码实践

**目标**：在同一份网表里同时看到「公共名 `\a`」和「私有名 `$driver$...`」，并理解它们各自来源。

**步骤**：

1. 复用 4.2 的设计并查看网表文本：

   ```tcl
   read_slang <<EOF
   module top(input logic clk);
     reg [7:0] a = 8'h63;
     always_ff @(posedge clk) a <= a + 8'd1;
   endmodule
   EOF
   show
   ```

2. 定位三处名字：变量 `a` 对应的线、`a+1` 产生的加法单元、驱动 `a` 的触发器单元。

**需要观察的现象**：

- 线 `\a`：名字来自 `id(symbol)` = `escape_id("a")`，在 `add_wire` 里建出。
- 触发器单元 `$driver$\a`（私有名）：名字来自 `"$driver$" + unescaped_id(symbol) + ""`（整段命中，name 为空），在 [src/slang_frontend.cc:1947](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1947) 拼出。
- 加法中间线 `$1` 之类（私有名）：由 `new_id()`/`add_y_wire` 生成。

**预期结果**：你能指着网表里每个名字说出「它是 `id()` 产出的公共转义名，还是 `new_id()`/`$driver$` 拼出的私有名」。这就是本模块要建立的命名直觉。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `id()` 要对名字做 `escape_id` 转义，而 `hdlname()` 不用？

**答案**：`id()` 的产物要直接当作 RTLIL 线/单元的名字字符串，必须遵守 Yosys「公共名加 `\` 前缀」的文本约定，否则会被当成私有名；`hdlname()` 只是挂在一个属性里给人看，不参与 RTLIL 名字解析，所以保持原始点分/空格分隔的 HDL 层次名即可。

**练习 2**：`new_id("foo")` 和 `new_id()`（无参）分别长什么样？为什么都要以 `$` 开头？

**答案**：分别是 `$foo$<n>` 和 `$<n>`（`<n>` 是自增计数器）。以 `$` 开头标记它们是 sv-elab 内部生成的私有名，Yosys 的优化 pass 可以自由改名、合并，而不会与用户源码里的公共标识符冲突。

---

## 5. 综合实践

把四个模块串起来，追踪一个「含初值的 reg 同时被触发器驱动」的完整旅程。准备设计：

```tcl
read_slang <<EOF
module top(input logic clk);
  reg [7:0] a = 8'h63;
  always_ff @(posedge clk) a <= a + 8'd1;
endmodule
EOF
show
```

请按顺序回答并自行核对：

1. **记账阶段**（4.1）：`a` 的声明初值 `8'h63` 在 `evaluate_decl_initializers` 里经 `do_simple_assign` → `update_variable_state` 的 `Initial` 分支，被写进 `netlist.initial_state` 的哪些键？值分别是什么？
   - *参考答案*：键是 `VariableBit(a, 0..7)` 共 8 个，值是 `01100011`（即 `0x63` 各位）。

2. **驱动阶段**（命名，4.4 + 4.3）：`always_ff` 被识别为触发器型（见 u6），`handle_ff_process` 为 `a` 发射触发器。该触发器单元叫什么名字？它的 `Q` 连到哪根线？那根线又叫什么、由谁创建？
   - *参考答案*：触发器叫 `$driver$\a`（`$driver$` + `unescaped_id(a)` + 空后缀，整段命中无 `[h:l]`）；其 `Q` 连到线 `\a`；`\a` 由 `add_wire` 用 `id(a)` = `escape_id("a")` 创建，并挂 `hdlname` 属性 `a`。

3. **结账阶段**（4.2）：`finalize_variable_initialization` 处理 `a` 时，因为它的每位都 `register_driven`，初值落到哪里？
   - *参考答案*：落到线 `\a` 的 `init` 属性，即 `wire->attributes[ID::init] = 8'01100011`；不走 `connect` 常量连线（那是「无人驱动」才用的）。

4. **反推**：如果在最终网表里看到 `wire [7:0] \a` 既有 `attribute \init 8'01100011`、又被 `$driver$\a` 的 `Q` 驱动，请用本讲的三阶段模型解释这两个事实的来源。
   - *参考答案*：`init` 来自 4.1 记账 + 4.2 寄存器驱动结账；`$driver$\a` 的 `Q` 驱动来自 u6 的触发器发射，其名字来自 4.4/4.3 的命名拼接。

完成后再做一个小变化：把 `reg [7:0] a = 8'h63;` 改成不带初值的 `reg [7:0] a;`，重新 `show`，确认 `\a` 上的 `init` 属性消失（变成全 x，故不挂），以此验证你对「记账表为空 → 结账无 init」链路的理解。

## 6. 本讲小结

- sv-elab 把变量初值处理拆成「先记账、后结账」两步：`evaluate_decl_initializers` 在主体翻译前把初值写进按位表 `initial_state`，`finalize_variable_initialization` 在主体翻译后把它落到 RTLIL。
- 记账靠 `ProceduralContext(... ProcessTiming::initial)` 走 `update_variable_state` 的初值捷径，不建 `Process`、不压 `Case::Action`，只填 `initial_state[VariableBit]`。
- 结账按驱动类型三分：寄存器驱动位挂 `init` 属性、组合驱动位忽略、无人驱动位接常量连线。
- `generate_subfield_names` 沿类型树递归，把一段位流切成带 `.字段`/`[下标]`/`[h:l]` 名字的子片段，供结构体/数组变量的子字段触发器命名。
- `id()` 产出经 `escape_id` 转义的公共名（如 `\a`），`hdlname()` 产出展示用 HDL 层次名，`new_id()` 产出 `$` 起头的私有内部名——三者交汇于 `add_wire`。

## 7. 下一步学习建议

- 本讲是单元 7 的最后一篇。若你想看初值的「存储器特例」如何落地为 `$meminit_v2`，回顾 [u7-l1 存储器推断与初始化](u7-l1-memory-inference.md)。
- 想深入「寄存器驱动位」是怎么被判定出来的，回到 [u6-l2 触发器发射与异步复位](u6-l2-flipflop-and-async-reset.md) 看 `register_driven` 的填充时机。
- 准备进入单元 8：建议从 [u8-l1 测试体系](u8-l1-test-infrastructure.md) 开始，学习如何用 `equiv_make`/`equiv_induct` 编写等价性测试来验证本讲描述的初值/命名行为是否正确。
- 若你打算为新的 SV 构造添加翻译，[u8-l4 扩展开发](u8-l4-extending-and-contributing.md) 会告诉你 `require`/`unimplemented` 宏与各 `handle` 扩展点的位置——本讲的初值与命名机制是任何新构造落地时都会触及的公共底座。
