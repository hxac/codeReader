# AddressingResolver：动态位/数组寻址

## 1. 本讲目标

本讲聚焦 sv-elab 中一个「小而精」的部件——`AddressingResolver`。它负责把 SystemVerilog 里所有「下标会变」的索引表达式翻译成 RTLIL 电路。学完本讲你应当能够：

- 说清 `ElementSelect`（`a[i]`，取一个元素）与 `RangeSelect`（`a[i+:w]`、`a[i-:w]`、`a[h:l]`，取一段）这两类动态索引表达式，分别是怎么变成 RTLIL 单元的。
- 区分「读路径」（从被索引对象里取出若干位）与「写路径」（把若干位散布回被索引对象），并把它们对应到 `mux/shift_down` 与 `demux/shift_up` 四个核心方法。
- 理解 `is_static()` 这一判定如何让编译期常量索引走「不建任何单元」的纯接线捷径。
- 为一段含动态索引的 SystemVerilog 代码，预言它会生成哪些 RTLIL 单元（`$bmux`、`$shiftx`、`$demux`、`$shift` 等），并能用 `read_slang` 加以验证。

本讲是单元 4「表达式求值与左值」的第三篇，承接 [u4-l1 EvalContext](u4-l1-expression-evaluation.md)（表达式求值入口）与 [u4-l2 LValue](u4-l2-lvalue-analysis.md)（左值结构化分析）。`EvalContext::operator()` 遇到 `ElementSelect/RangeSelect` 时会把活儿派给 `AddressingResolver`；`LValue` 在分析「动态左值」时也持有一个 `AddressingResolver`。所以本讲其实是把 u4-l1 / u4-l2 里被刻意留白的「动态寻址到底怎么变成电路」补齐。

## 2. 前置知识

阅读本讲前，建议先建立以下直觉：

- **字位流（bitstream）布局**：sv-elab 内部把任何向量/数组都看成一段「低位在左」的位流。一个 `[7:0]` 向量的第 0 位就是位流的第 0 位。所有索引运算最终都要换算成「位流里的第几位」。
- **降序（descending）与升序（ascending）范围**：SystemVerilog 允许 `[7:0]`（降序，最常见）和 `[0:7]`（升序，大端）两种声明。slang 用 `slang::ConstantRange` 同时记录 `left`、`right` 与方向 `isDescending()`。升序范围的位序与位流布局相反，所以需要一次「按位取反」来对齐——这是本讲最容易踩坑的点。
- **RTLIL 字级单元动物园**：回顾 [u3-l2 RTLILBuilder](u3-l2-rtlil-builder-cells.md) 提到的几个单元——`$mux`（二选一）、`$bmux`（N 选一，按选择码从一捆数据里挑一段）、`$shift`/`$shiftx`（桶形移位，后者越界填 `x`）、`$demux`（一路广播到 N 路中的某一路）。本讲的动态寻址正是用这几种单元搭出来的。
- **静态与动态**：如果索引在编译期就是常量（如 `a[2]`），翻译时可以直接「接线」，不必建任何选择单元；如果索引是运行时信号（如 `a[i]`），就必须建 mux/shift 类电路。`AddressingResolver` 用 `raw_signal.is_fully_def()` 区分这两者。

> 名词对照：本讲里「选择码 / 选择信号」= 用来决定取哪一位/哪一段的那个信号（即 `a[i]` 里的 `i`）；「被索引对象 / 载体」= `a`；「步长 stride」= 每一步索引跨多少位（向量逐位时是 1，数组取元素时是元素位宽）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/addressing.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/addressing.cc) | `AddressingResolver` 的全部实现：构造、`mux`/`demux`/`shift_up`/`shift_down`/`extract`/`embed`。本讲主战场。 |
| [src/slang_frontend.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h) | `AddressingResolver` 类声明（成员与公有/私有方法清单），以及 `LValue` 对它的持有方式。 |
| [src/slang_frontend.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc) | 「读路径」调用点：`EvalContext::operator()` 在 `RangeSelect`/`ElementSelect` 分支里创建并使用 `AddressingResolver`。 |
| [src/lvalue.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/lvalue.cc) | 「左值路径」：`LValue::analyze` 把 `RangeSelect`/`ElementSelect` 左值包成一个持有 `AddressingResolver` 的 descriptor。 |
| [src/procedural.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc) | 「写路径」调用点：`assign_to_lvalue_with_masking` 对 `RangeSelect` 左值调用 `demux`/`shift_up`。 |
| [src/builder.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc) | `RTLILBuilder` 提供的原子电路构建方法（`Bmux`/`Demux`/`Shift`/`Shiftx`/`Mux`），是 `AddressingResolver` 的「砖块」。 |

## 4. 核心概念与源码讲解

### 4.1 AddressingResolver：把 SV 索引归一化成「位流地址」

#### 4.1.1 概念说明

`ElementSelect`（`a[i]`，取一个元素）和 `RangeSelect`（`a[i+:w]`、`a[i-:w]`、`a[h:l]`，取一段）的「取哪几位」取决于三件事：

1. 索引表达式本身（`i`，可能是常量也可能是运行时信号）；
2. 被索引对象声明的范围与方向（`[7:0]` 还是 `[0:7]`，`range.right` 是多少）；
3. 每一步索引跨多少位（`stride`：向量逐位时是 1，数组取元素时是元素位宽）。

`AddressingResolver` 的职责，就是在构造时把这三件事「揉」成两个归一化的量：

- `raw_signal`：选择信号的 RTLIL 表示（已经处理好符号与大端翻转）；
- `base_offset`：一个编译期整数偏移。

两者相加永远等于「被选中的最低位在位流里的零基下标」：

\[
p \;=\; \text{raw\_signal} \;+\; \text{base\_offset}
\]

这条不变量在类声明里用注释写得很清楚（见下方源码精读）。有了 \(p\) 和 `stride`，后续的 mux/shift 操作就只是在「位流」上做平移与抽取，不再关心 SV 的范围语法细节。这正是「归一化」的价值：复杂的 SV 索引语法被压缩成一个 `(raw_signal, base_offset, stride)` 三元组。

#### 4.1.2 核心流程

构造一个 `AddressingResolver` 的流程：

1. 取出被索引对象的固定范围 `range`（要求类型必须有 fixed range，否则触发 `require` 失败）。
2. 根据范围方向（降序/升序）与选择信号的符号，调用 `interpret_index` 把选择信号归一化成 `raw_signal` 并算出 `base_offset`。
3. 设定 `stride`：
   - `ElementSelect`：`stride = sel.type->getBitstreamWidth()`（被选元素的位宽）。
   - `RangeSelect`：若被索引对象是数组，`stride = 元素位宽`；否则 `stride = 1`（即逐位的向量切片）。

读/写时分派：

```
读（EvalContext::operator()）
├─ ElementSelect (a[i])            → addr.mux(value, element_width)
└─ RangeSelect  (a[i+:w] 等)       → addr.shift_down(value, slice_width)

写（assign_to_lvalue_with_masking，左值是 RangeSelect）
├─ stride == 写入宽度（写一个元素） → rvalue.repeat(...width) + resolver.demux(mask, inner_width)
└─ 否则（写一段）                    → resolver.shift_up(rvalue, true,  inner_width)
                                      resolver.shift_up(mask,    false, inner_width)
```

#### 4.1.3 源码精读

类声明集中展现了「归一化三元组 + 四个方向的操作」这一设计。注意私有成员 `raw_signal` 与 `base_offset` 上方的注释——它点明了那条不变量：

[slang_frontend.h:662-694 `AddressingResolver` 类声明](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L662-L694) —— 公有区是四个方向的操作（`shift_up`/`demux`/`mux`/`shift_down`）加模板 `extract` 与 `is_static`；私有区是 `interpret_index`、各种 `*_bitwise`/`raw_*`/`embed` 辅助，以及那个关键的归一化状态 `(raw_signal, base_offset)`。注释「these summed together are the zero-based index of the bottom item of the selection」就是 \(p = \text{raw\_signal} + \text{base\_offset}\)。

归一化的核心是 `interpret_index`，它处理大端翻转：

[src/addressing.cc:25-38 `interpret_index`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/addressing.cc#L25-L38) —— 降序范围（最常见）直接 `raw_signal = signal` 并用 `range.right` 算偏移；升序范围则 `raw_signal = netlist.Not(signal)`（按位取反，把大端序翻成位流序），并对偏移做补偿。源码注释「We might want some other handling of big-endian indexing」说明这是已知需要小心的一段。

两个构造函数分别对应两类索引表达式：

[src/addressing.cc:40-48 `ElementSelect` 构造](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/addressing.cc#L40-L48) —— 用 `eval.eval_signed(sel.selector())` 求出（带符号的）选择信号，再交给 `interpret_index`；`stride` 取被选元素的位宽。

[src/addressing.cc:50-85 `RangeSelect` 构造](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/addressing.cc#L50-L85) —— 按 `Simple`/`IndexedUp`/`IndexedDown` 三种 SV 语法分别处理：`Simple`（`a[h:l]`）两端都按常量求值（`sel.left().eval(eval.const_)`）；`IndexedUp`（`a[i+:w]`）/`IndexedDown`（`a[i-:w]`）则把动态那一端经 `interpret_index` 归一化。

#### 4.1.4 代码实践

**实践目标**：亲手验证「索引到地址」的归一化，并体会大端翻转。

**操作步骤**（源码阅读型）：

1. 打开 [src/addressing.cc:25-38](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/addressing.cc#L25-L38)。
2. 对 `[7:0] data`（降序），手工代入 `data[3]`：`range.right = 0`，默认 `width_down = 1`，走 `if (range.isDescending())` 分支，得 `base_offset = -0 - 1 + 1 = 0`，`raw_signal = 3`。于是 \(p = 3\)，即取位流第 3 位——正确。
3. 对 `[0:7] data`（升序）代入 `data[3]`：走 `else` 分支，`base_offset = 7 - 1 + 1 = 7`，再 `+1 = 8`，`raw_signal = Not(3)`。由于大端序下「索引 3」在位流里位置靠后，这里靠取反加偏移把它对齐到位流坐标。

**需要观察的现象**：降序时 `raw_signal` 就是索引原值；升序时多了一次 `Not`。

**预期结果**：两种范围最终都满足 \(p = \text{raw\_signal} + \text{base\_offset}\) 给出位流零基下标。

> 说明：升序范围下 `Not` 的位宽取决于选择信号位宽，精确的十进制换算需结合位宽；本实践重在建立「降序直通、升序翻转」的直觉，精确数值留作小练习。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ElementSelect` 构造里用 `eval.eval_signed(...)` 而不是普通的 `eval(...)`？

**答案**：`eval_signed` 在类型是无符号数值时，会在高位补一个 `0` 符号位（见 [src/slang_frontend.cc:1674-1682](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1674-L1682)）。动态索引的越界判定（`raw_mux`/`raw_demux` 里的 `$ge`/`$lt`）需要正确的符号语义，而 SV 允许负索引（如 `[4:-2]`、`[-7:-2]` 的向量），必须按有符号数比较。

**练习 2**：构造函数里第一行 `require(sel, sel.value().type->hasFixedRange())` 失败会怎样？

**答案**：`require` 宏（[src/slang_frontend.h:645-647](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L645-L647)）在条件不成立时调用 `unimplemented_`，相当于「该 SV 构造尚未支持」，会中断该表达式的翻译并上报诊断。它要求被索引对象有确定的范围（如定长 packed vector 或定长数组），动态长度的类型不在支持范围内。

### 4.2 demux / mux：单元素的「读出」与「写入」

#### 4.2.1 概念说明

`mux` 与 `demux` 处理「单元素」的读写：索引跨 `stride` 位，且要读/写的也是 `stride` 位。

- **读 `a[i]`**（`mux`）：从一段长向量里，按 `i` 选出连续 `stride` 位。这本质上是一个「N 选一」的多路选择器，对应 RTLIL 的 `$bmux`。
- **写 `a[i] = v`**（`demux`）：把一份 `stride` 位的值，写回到长向量的第 `i` 个位置，其余位保持原样。这是 `$bmux` 的逆运算，对应 RTLIL 的 `$demux`（把一路数据广播到 N 路中由选择码指定的那一路）。

两者都还要处理 **越界（out-of-range）**：当 `i` 超出被索引对象的范围时，SV 语义规定读取得到 `x`、写入被忽略。`AddressingResolver` 用 `$ge`/`$lt` 比较器算出一个「有效」标志，再用 `$mux` 把越界结果盖成 `x`（读）或把数据门控成 0（写）。

#### 4.2.2 核心流程

`mux` 的决策树（读单元素）：

```
mux(val, output_len=stride)
├─ raw_signal 是常量？ → extract(val)：纯接线，不建单元
└─ 否则（动态）
   └─ raw_mux(两端用 x 补齐后的 val, from, to, stride)
        ├─ 负索引段：Bmux 选 + Ge 判有效 + Mux 盖 x
        ├─ 非负索引段：Bmux 选 + Lt 判有效 + Mux 盖 x
        └─ 用 raw_signal 的符号位 Mux 合并正负两段
```

`demux` 是它的镜像（写单元素）：用 `raw_demux` 把 `stride` 位数据广播成「每位置一份」，越界处经门控置 0，再抽出目标宽度。

#### 4.2.3 源码精读

读路径入口在 `EvalContext::operator()` 的 `ElementSelect` 分支（注意它先排除了「推断存储器」的情形，那条路走 `$memrd_v2`，不属于本讲）：

[src/slang_frontend.cc:1496-1531 `ElementSelect` 读路径](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1496-L1531) —— 先判 `is_inferred_memory` 走 `$memrd_v2`；否则创建 `AddressingResolver addr(*this, elemsel)`，再 `addr.mux((*this)(elemsel.value()), elemsel.type->getBitstreamWidth())` 得到读取结果。

`mux` 本体很短，核心是「静态走捷径，动态走 `raw_mux`」：

[src/addressing.cc:277-286 `mux`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/addressing.cc#L277-L286) —— `raw_signal.is_fully_def()` 时直接 `extract`；否则把 `val` 用 `x` 在两端补齐到覆盖正负索引范围，交给 `raw_mux`。

`raw_mux` 是真正建电路的地方，它把选择范围按「负索引」与「非负索引」两段分别建 `$bmux`，再用比较器判有效：

[src/addressing.cc:234-275 `raw_mux`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/addressing.cc#L234-L275) —— 关键行：`netlist.Bmux(val_padded, sel)` 建 N 选一选择器；`netlist.Ge(...)`/`netlist.Lt(...)` 判定索引是否落在合法区间；`netlist.Mux(Sx, bmux_result, valid)` 在越界时把输出盖成 `x`。最后 `netlist.Mux(positive, negative, raw_signal.msb())` 用索引符号位合并正负两段。

写路径（左值为单元素）在 `assign_to_lvalue_with_masking` 里。注意一个微妙之处：**`demux` 作用在掩码 `mask` 上，而要写的值 `rvalue` 被「复制」铺满所有位置**——这样内层左值拿到的是「每个位置都填了同一个 rvalue」，只有掩码选中那一处真正生效：

[src/procedural.cc:372-383 `RangeSelect` 左值写路径](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L372-L383) —— 当 `stride == lvalue.bitsize`（即写一个完整元素）时，把 `rvalue.repeat(range.width())` 复制铺满，把 `resolver.demux(mask, inner->bitsize)` 的结果作为新掩码，递归写回内层左值。

`demux` 调 `raw_demux` 建真正的 `$demux` 单元：

[src/addressing.cc:224-232 `demux`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/addressing.cc#L224-L232) 与 [src/addressing.cc:176-222 `raw_demux`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/addressing.cc#L176-L222) —— `raw_demux` 用 `netlist.Mux(0, val, valid)` 在越界时把数据门控为 0，再用 `netlist.Demux(val_gated, sel)` 广播到各位置；正、负两段各自单独建一个 `$demux`，最后拼接。

底层砖块在 `RTLILBuilder`：

[src/builder.cc:260-271 `Bmux`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L260-L271) —— N 选一选择器（`$bmux`）；常量选择码时退化为纯 `extract`，不建单元。

[src/builder.cc:74-85 `Demux`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L74-L85) —— 一入 N 出的广播（`$demux`），选择码为常量时同样退化为纯接线（用零填充拼出选中位置）。

#### 4.2.4 代码实践

**实践目标**：为一段含动态索引读取 `a[i]` 的 SystemVerilog，预言并验证生成的 mux 电路，再对比静态索引。

**操作步骤**：

1. 新建一个最小设计 `dyn_read.sv`（示例代码，非项目原有文件）：

   ```systemverilog
   module dyn_read(input [7:0] a, input [2:0] i, output y);
       assign y = a[i];      // 动态单元素读取
   endmodule
   ```

2. 用 `read_slang` 读入并查看网表（需本地已构建 `slang.so`，命令供参考）：

   ```tcl
   # yosys -m slang.so
   read_slang dyn_read.sv
   prep; show
   ```

3. 把 `a[i]` 改成静态 `a[2]`，重复上述步骤。

**需要观察的现象**：

- 动态版 `a[i]`：依据 [src/addressing.cc:234-275](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/addressing.cc#L234-L275)，应出现一个 `$bmux`（按 `i` 选择），若干 `$ge`/`$lt`（越界判定），以及一个 `$mux`（越界时把输出盖成 `x`）。
- 静态版 `a[2]`：依据 [src/addressing.cc:277-286](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/addressing.cc#L277-L286) 的 `is_fully_def` 分支，应不产生任何选择单元，`y` 直接连到 `a` 的某一位。

**预期结果**：动态版生成 `$bmux` 等单元；静态版零单元、纯连线。

> 「待本地验证」：`show` 的具体单元 ID 与形状取决于本地 Yosys 版本与 `prep` 优化，请以实际输出为准。本实践对单元类型的预言来自对上述源码的静态阅读。

#### 4.2.5 小练习与答案

**练习 1**：`raw_mux` 为什么要分成「负索引段」和「非负索引段」两段分别建 `$bmux`，最后再用符号位合并？

**答案**：`$bmux` 的选择码是无符号的（按位宽取模寻址），无法直接表达负索引。SV 允许负索引（如 `[4:-2]` 的向量，`i` 可以是负数），所以代码把合法范围切成负、非负两段，各用一个大小为 2 的幂的 `$bmux`（`std::bit_ceil` 向上取整），再用索引的符号位（`raw_signal.msb()`）在两段结果间二选一。

**练习 2**：越界读取时输出为 `x` 是怎么实现的？

**答案**：`raw_mux` 用 `netlist.Ge`/`netlist.Lt` 算出 `valid`（索引是否落在合法区间内），再用 `netlist.Mux(RTLIL::SigSpec(RTLIL::Sx, stride), bmux_result, valid)`：`valid=0` 时输出 `x`，`valid=1` 时输出正常选择结果。这与 [tests/unit/bitsel.sv:13](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/bitsel.sv#L13) 里 `assert(o === 1'bx)` 的越界断言一致——该测试正是用 `wire o = data[sel];` 配合负索引范围（`[4:-2]`、`[-7:-2]` 等）来验证这一行为。

### 4.3 shift_up / shift_down：范围切片的「散布」与「抽取」

#### 4.3.1 概念说明

当索引不是「取一个元素」而是「取一段」（`a[i+:w]`、`a[i-:w]`），或写左值是「写一段」时，`stride` 不再等于读写宽度。此时 `mux`/`demux`（假设每个位置一份拷贝）会变得很大，不如直接用一个 **桶形移位器（barrel shifter）**：把整段被索引对象当成一串位，按 `i` 左移或右移，再截取目标宽度。

- **读一段**（`shift_down`）：把载体按 `i` 右移并截取 `output_len` 位。越界位填 `x`（符合 SV 语义），用 `$shiftx`。
- **写一段**（`shift_up`）：把要写的值按 `i` 左移到正确位置，越界位在「值」侧填 `x`、在「掩码」侧填 0（这样越界的写不会真正生效）。

当 `stride > 1`（例如对数组做 `arr[i+:w]` 跨多个元素）时，代码把位流按 `stride` 拆成多个「位平面」，对每个平面分别移位再交错拼回——这是对「多比特元素」的推广。

#### 4.3.2 核心流程

`shift_down`（读一段）决策树：

```
shift_down(val, output_len)
├─ raw_signal 是常量？ → extract(val)：纯接线
├─ stride == 1？       → shift_down_bitwise：一个 $shiftx 搞定
└─ stride > 1？        → 按 stride 拆位平面，每平面一个 $shiftx，再交错拼回
```

`shift_up`（写一段）与之镜像，但区分 `oor_undef`：写「值」时越界填 `x`，写「掩码」时越界填 `0`。

#### 4.3.3 源码精读

读路径入口（`RangeSelect` 在 `EvalContext::operator()`）：

[src/slang_frontend.cc:1489-1495 `RangeSelect` 读路径](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1489-L1495) —— 创建 `AddressingResolver addr(*this, sel)`，再 `addr.shift_down((*this)(sel.value()), sel.type->getBitstreamWidth())`。

`shift_down` 的三分支：

[src/addressing.cc:306-331 `shift_down`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/addressing.cc#L306-L331) —— 常量走 `extract`；`stride==1` 走 `shift_down_bitwise`；`stride>1` 走位平面拆分（内层循环收集同相位的位）。

`shift_down_bitwise` 用一个 `$shiftx`（越界填 `x`）实现右移抽取：

[src/addressing.cc:288-304 `shift_down_bitwise`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/addressing.cc#L288-L304) —— 关键行 `netlist.Shiftx(val2, raw_signal, true, shifted_len)`，并用 `base_offset` 在两端补位以对齐。

写路径（左值为一段）：

[src/procedural.cc:378-383 `shift_up` 写值与写掩码](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L378-L383) —— 对 `rvalue` 调 `shift_up(..., true, ...)`（越界填 `x`），对 `mask` 调 `shift_up(..., false, ...)`（越界填 `0`），再递归写回内层左值。

`shift_up` 的三分支与 `shift_down` 对称：

[src/addressing.cc:111-136 `shift_up`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/addressing.cc#L111-L136) —— 常量走 `embed`；`stride==1` 走 `shift_up_bitwise`；`stride>1` 走位平面拆分。

[src/addressing.cc:87-109 `shift_up_bitwise`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/addressing.cc#L87-L109) —— 注意它对 `raw_signal` 取反（`netlist.Neg(raw_signal, true)`），因为「左移」与「右移」方向相反；并根据 `oor_undef` 选择 `Shiftx`（填 `x`）或 `Shift`（填 `0`）。

底层砖块：

[src/builder.cc:242-249 `Shiftx`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L242-L249) 与 [src/builder.cc:207-240 `Shift`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L207-L240) —— 分别对应 `$shiftx`（越界填 `x`）与 `$shift`（按符号/零填充）。两者都在 `b` 为常量时退化为纯 `extract` 拼接，不建单元。

> 项目自带的真实用例见 [tests/unit/partsel_down.sv](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/partsel_down.sv)：其中 [行 9 `data[sel-:2]`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/partsel_down.sv#L9) 即 `IndexedDown` 范围选择（覆盖本节 `shift_down`），[行 70 `data[sel-:2] = i2;`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/partsel_down.sv#L70) 是范围写（覆盖本节 `shift_up`），同文件的 `data[sel]` 则是单元素选择（覆盖上一节 `mux`）。

#### 4.3.4 代码实践

**实践目标**：观察 `a[i-:w]` 这类范围读取生成的 `$shiftx`，并理解 `base_offset` 的对齐作用。

**操作步骤**：

1. 新建示例 `dyn_slice.sv`（示例代码）：

   ```systemverilog
   module dyn_slice(input [7:0] a, input [2:0] i, output [1:0] y);
       assign y = a[i-:2];   // 动态范围读取，宽度 2
   endmodule
   ```

2. 用 `read_slang` 读入并 `show`（命令供参考）：

   ```tcl
   # yosys -m slang.so
   read_slang dyn_slice.sv
   prep; show
   ```

**需要观察的现象**：依据 [src/addressing.cc:288-304](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/addressing.cc#L288-L304)，应出现一个 `$shiftx` 单元（被索引对象 `a` 作数据、`i` 作移位量），其输出截取 2 位作为 `y`。

**预期结果**：单个 `$shiftx` 实现「按 `i` 右移并取低 2 位」，越界时对应位为 `x`。

> 「待本地验证」：具体单元形状以本地输出为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `shift_up_bitwise` 里要对 `raw_signal` 取反（`Neg`），而 `shift_down_bitwise` 里直接用？

**答案**：`shift_down` 是「把载体右移以取出低位」，移位方向与索引增大方向一致；`shift_up` 是「把要写入的值左移到对应高位」，方向相反，所以要把选择信号取负，把「左移 k 位」转成「移位器右移 -k 位」。两者最终都落在 `$shift`/`$shiftx` 的同一套移位语义上。

**练习 2**：`stride > 1` 时为什么不能直接用一个 `$shiftx`？

**答案**：`$shiftx` 是「逐位移位」的，而 `stride > 1` 表示「以 `stride` 位为一个不可拆分的元素」。直接逐位移位会把跨元素的位错位地混在一起。所以代码按相位把位流拆成 `stride` 个「位平面」（每个平面里的位彼此间隔 `stride`），每平面单独移位，再把结果按原相位交错拼回（见 [src/addressing.cc:315-324](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/addressing.cc#L315-L324)）。

### 4.4 静态捷径：is_static / extract / embed

#### 4.4.1 概念说明

四个动态方法（`mux`/`demux`/`shift_up`/`shift_down`）的第一行几乎都是「如果索引是常量，就走捷径」。这是因为 **常量索引不需要任何选择/移位电路**——取哪几位在编译期就已确定，直接用 RTLIL 的 `extract`（取若干位）或拼接线即可。

`is_static()` 就是这个判定的对外接口：它返回 `raw_signal.is_fully_def()`，即「归一化后的选择信号是一个确定的常量」。

这条捷径与 [u4-l2 LValue](u4-l2-lvalue-analysis.md) 紧密联动：`LValue::rangeSelect` 工厂方法用 `resolver.is_static()` 决定整个左值是否 `static_`；若静态，则最终走 `LValue::evaluate_vbits` → `resolver->extract<VariableBits>(...)`，把左值折叠成 [u3-l3 VariableBits](u3-l3-variable-and-variablebits.md) 的按位键，完全绕开 RTLIL 单元的生成。

#### 4.4.2 核心流程

```
is_static() = raw_signal.is_fully_def()

静态路径（不建任何单元）
├─ 读：  extract<SigSpec>(val, width)         → 纯 SigSpec::extract + x 填充
├─ 写：  embed(val, output_len, stride, pad)  → 纯拼接，pad 为 0 或 x
└─ 左值：extract<VariableBits>(val, width)    → 纯 VariableBits::extract + dummy 填充
```

`extract` 与 `embed` 的共同点是：在编译期用 `std::clamp` 把越界部分裁掉，用常量 `x`/`0`/`dummy` 填充，再 `extract` 出合法段。整个过程零单元。

#### 4.4.3 源码精读

判定本身只有一行：

[src/addressing.cc:351-354 `is_static`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/addressing.cc#L351-L354) —— `return raw_signal.is_fully_def();`。

`LValue` 如何消费它：

[src/lvalue.cc:174-181 `LValue::rangeSelect`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/lvalue.cc#L174-L181) —— `static_ = inner.static_ && resolver.is_static()`，把 `AddressingResolver` 连同内层左值一起存进 `RangeSelect` descriptor。

静态左值的最终折叠：

[src/lvalue.cc:214-216 `evaluate_vbits` 的 `RangeSelect` 分支](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/lvalue.cc#L214-L216) —— 先递归折叠内层左值，再 `resolver->extract<VariableBits>(inner_vbits, bitsize)`。

`extract` 的两个模板特化——一个作用于已物化的 `RTLIL::SigSpec`（读路径），一个作用于「HDL 意图」的 `VariableBits`（静态左值路径），算法完全相同，只是填充物不同（`Sx` vs `Variable::dummy`）：

[src/addressing.cc:138-154 `extract<VariableBits>`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/addressing.cc#L138-L154) 与 [src/addressing.cc:156-174 `extract<RTLIL::SigSpec>`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/addressing.cc#L156-L174) —— 两者都断言 `raw_signal.is_fully_def()`，用 `std::clamp` 把 `[start, end)` 裁到合法区间，越界处分别填 `dummy` / `Sx`。

静态写的「散布」用 `embed`：

[src/addressing.cc:333-349 `embed`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/addressing.cc#L333-L349) —— 与 `extract` 对称：按 `offset` 在两侧填 `padding`（`0` 或 `x`），中间嵌入选中段。注意 `shift_up` 在常量分支调用它时，对 `rvalue` 传 `Sx`、对 `mask` 传 `S0`（见 [src/addressing.cc:113-114](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/addressing.cc#L113-L114)）。

> 真实用例见 [tests/unit/static_element_select.sv](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/static_element_select.sv)：[行 11 `wire actual = data[IDX];`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/static_element_select.sv#L11) 中 `IDX` 是参数（编译期常量），它用 [行 15 `assert(actual === expected)`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/static_element_select.sv#L15) 验证 `data[IDX]` 等价于 `data >> OFFSET`——即「静态选择 = 纯移位/接线、零选择单元」。文件里还有 `data[IDX]` 作用于 `[MSB:LSB][1:0]` 数组（`stride=2`）的版本。

#### 4.4.4 代码实践

**实践目标**：直观对比「同一表达式的静态与动态两种形态」在网表上的差异。

**操作步骤**：

1. 准备两个对照模块（示例代码）：

   ```systemverilog
   module s_static(input [7:0] a, output y); assign y = a[3]; endmodule
   module s_dyn  (input [7:0] a, input [2:0] i, output y); assign y = a[i]; endmodule
   ```

2. 分别 `read_slang` + `prep` + `show`，对比两者为 `y` 生成的电路。

**需要观察的现象**：

- `s_static`：`a[3]` 是常量索引，`is_static()` 为真，走 [src/addressing.cc:156-174 `extract`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/addressing.cc#L156-L174)，`y` 应直接连到 `a[3]`，无任何单元。
- `s_dyn`：走 `raw_mux`，应出现 `$bmux` 等单元。

**预期结果**：静态零单元、动态有选择单元。

> 「待本地验证」：以本地 `show` 输出为准。

#### 4.4.5 小练习与答案

**练习 1**：`extract` 有两个模板特化（`RTLIL::SigSpec` 与 `VariableBits`），为什么算法可以完全一样？

**答案**：因为两者都只是「在一段定长位序列上，按常量下标取若干位」这一纯结构操作——`SigSpec::extract` 与 `VariableBits::extract` 提供了同名的按区间截取语义。唯一区别是越界填充物：物化的 `SigSpec` 用 `Sx`（最终会进 RTLIL 网表），而「HDL 意图」的 `VariableBits` 用 `Variable::dummy()`（占位，不对应真实信号，留给后续变量状态合并处理，参见 [u3-l3](u3-l3-variable-and-variablebits.md)）。

**练习 2**：如果 `is_static()` 返回真，但 `extract` 里 `raw_signal.as_const().as_int(true)` 得到的偏移使选中段完全越界（例如对 `[7:0]` 取 `a[100]`），会发生什么？

**答案**：`std::clamp` 会把 `[start, end)` 裁成空区间，合法段长度为 0，整段输出全部是填充物（`Sx` 或 `dummy`）。这与 SV 语义「越界读取得 `x`」一致。

## 5. 综合实践

把本讲四个最小模块串起来，做一个「动态索引读取 + 写入」的端到端跟踪任务。

**设计**（示例代码，综合了读取与写入）：

```systemverilog
module dyn_rw(input clk, input [2:0] i, input [1:0] d, output reg [1:0] q);
    reg [7:0] packed_mem;       // packed 向量，用于触发 AddressingResolver 路径
    always @(posedge clk) begin
        packed_mem[i*2 +: 2] <= d;   // 动态写一段（左值 RangeSelect → shift_up）
        q              <= packed_mem[i*2 +: 2]; // 动态读一段（右值 RangeSelect → shift_down）
    end
endmodule
```

> 说明：此处 `packed_mem` 是 packed 向量，仅用于触发 `AddressingResolver` 路径；若写成真正的 `reg [7:0] m[0:3]` 数组且被非阻塞赋值，sv-elab 会走「推断存储器」路径（`$memwr_v2`/`$memrd_v2`，见 [src/slang_frontend.cc:1500-1526](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1500-L1526)），那是 [u7-l1 存储器推断](u7-l1-memory-inference.md) 的主题，不在本讲范围。这里用 `+: 2` 的位切片（`stride==1`）正是为了让它走 `shift_up`/`shift_down` 而非存储器。

**任务步骤**：

1. **读路径**：跟踪 `packed_mem[i*2 +: 2]`（右值）。它在 `EvalContext::operator()` 命中 `RangeSelect` 分支（[src/slang_frontend.cc:1489-1494](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1489-L1494)），构造 `AddressingResolver`，由于 `i*2` 非常量、`stride==1`，走 `shift_down` → `shift_down_bitwise`，预言将生成单个 `$shiftx`。
2. **写路径**：跟踪 `packed_mem[i*2 +: 2] <= d`（左值）。它先经 `LValue::analyze` 命中 `RangeSelect` 分支（[src/lvalue.cc:75-87](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/lvalue.cc#L75-L87)）；再到 `assign_to_lvalue_with_masking`（[src/procedural.cc:378-383](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L378-L383)），因 `stride != lvalue.bitsize`（`stride==1`，`bitsize==2`）走 `shift_up`：对 `d` 调 `shift_up(..., true, ...)`（越界填 `x`），对 `mask` 调 `shift_up(..., false, ...)`（越界填 `0`）。
3. **静态对照**：把读取改成 `packed_mem[2 +: 2]`（常量起点），重新跟踪，确认走 `extract` 捷径、零单元。
4. **验证**：用 `read_slang` + `prep` + `show` 对照你的预言（「待本地验证」）。

**预期成果**：你能为「动态读一段」「动态写一段」「静态读」三种情形分别画出 `AddressingResolver` 内部的调用路径与生成的 RTLIL 单元类型，并解释越界处理的来龙去脉。

## 6. 本讲小结

- `AddressingResolver` 是 `ElementSelect`/`RangeSelect` 两类索引表达式的「地址归一化器」，把 SV 的范围语法、方向、步长压缩成 `(raw_signal, base_offset, stride)` 三元组，其不变量是 \(p = \text{raw\_signal} + \text{base\_offset}\) 即被选最低位的位流零基下标。
- **读单元素** 用 `mux` → `raw_mux`（`$bmux` + `$ge`/`$lt` 越界判定 + `$mux` 盖 `x`）；**写单元素** 用 `demux` → `raw_demux`（`$demux` + 门控），且写路径里 `demux` 作用于掩码、`rvalue` 被复制铺满所有位置。
- **读一段** 用 `shift_down` → `shift_down_bitwise`（`$shiftx`，越界填 `x`）；**写一段** 用 `shift_up` → `shift_up_bitwise`（对索引取负，按 `oor_undef` 选 `$shiftx`/`$shift`），且对「值」与「掩码」分别用 `x` 与 `0` 填充。
- `stride > 1`（数组跨元素）时，代码按相位拆「位平面」分别移位再交错拼回，避免逐位移位破坏元素边界。
- `is_static()`（`raw_signal.is_fully_def()`）是所有方法的公共捷径：常量索引走 `extract`/`embed`，零单元、纯接线，并与 [u4-l2 LValue](u4-l2-lvalue-analysis.md) 的静态左值折叠联动。
- 越界语义（读得 `x`、写被忽略）由比较器算出的 `valid` 标志 + `$mux` 门控统一实现，与项目测试 `tests/unit/bitsel.sv`、`partsel_down.sv`、`static_element_select.sv` 的断言一致。

## 7. 下一步学习建议

- 阅读 [src/addressing.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/addressing.cc) 全文，重点对照 `raw_mux` 与 `raw_demux` 的「正负两段」结构，体会它们互为逆运算的关系。
- 回到 [u4-l2 LValue](u4-l2-lvalue-analysis.md)，把本讲的 `is_static` / `extract<VariableBits>` 套进 `LValue::evaluate_vbits`，理解「动态左值」如何递归降级成 mux/demux 电路。
- 进入单元 5「过程块建模」，看 [src/procedural.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc) 里 `assign_to_lvalue_with_masking` 如何把本讲的 `demux`/`shift_up` 与过程块的 `VariableState`、掩码混合（`$bwmux`）结合起来。
- 若对真正的数组存储器感兴趣，前往 [u7-l1 存储器推断](u7-l1-memory-inference.md)，那里讲解 `ElementSelect` 命中「推断存储器」时走的 `$memrd_v2`/`$memwr_v2` 路径——它与本讲的 `mux`/`demux` 是互斥的两条分支。
