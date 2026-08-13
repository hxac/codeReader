# 入参构造：ArgumentsBuilder

## 1. 本讲目标

在 u1-l5 中我们把 `Atvoss::ArgumentsBuilder{}.inputOutput(...).build()` 当作黑盒使用，只要求「调用顺序对得上 `Compute()` 里的 `PlaceHolder<N>`」。本讲把这个黑盒打开。读完本讲，你应当能够：

1. 说出 `ArgumentsBuilder` 链式调用 `.inputOutput(...).attr(...).build()` 背后的「不可变构造器 + 类型累积」机制。
2. 看懂 `InputOutputCollector` / `AttrCollector` 如何用 `std::tuple_cat` 把每一次链式调用的参数累积进一个不断变宽的 `std::tuple` 类型里。
3. 解释 `inputOutput` 里那两条 `static_assert` 为什么只允许 `Atvoss::Tensor` 与标量、禁止指针。
4. 画出 `build()` 产物的两层 `std::tuple` 结构，并追踪 `DeviceAdapter::Run` 是如何按 `PlaceHolder` 的序号从中取参数的。

## 2. 前置知识

本讲是 Host 侧的纯 C++ 模板话题，不涉及 NPU 计算逻辑。阅读前请确认你已理解（见前置讲义）：

- **`PlaceHolder<N, T, ParamUsage>` 与序号空间**（u2-l2）：`Compute()` 里用 `PlaceHolder<1,...>`、`PlaceHolder<2,...>` 从 1 起连续编号声明入参/出参，序号 `N` 是运行时入参对齐的唯一凭据。
- **`Atvoss::Tensor<T>`**（u2-l5）：Host 侧的轻量包装，只存「设备指针 + 形状」，不持有内存；`data()` 返回设备指针。
- **算子运行时 10 步**（u1-l5）：`ArgumentsBuilder` 出现在第 7 步（构造参数信息），产物 `arguments` 交给第 8 步的 `deviceOp.Run(arguments, stream)`。

本讲会用到几个 C++17 标准库工具，先用一句话解释：

- **`std::tuple<T...>`**：把若干个**类型各异**的值打包成一个对象，用 `std::get<I>(t)` 按下标 `I` 取第 `I` 个（从 0 起）。
- **`std::tuple_cat(a, b)`**：把两个 tuple「拼接」成一个更长的 tuple，类型也对应拼接。
- **`std::forward_as_tuple(x...)`**：构造一个**元素全是引用**的 tuple（不拷贝实参），常用于「只是临时把已有变量串起来」。
- **`std::make_tuple(x...)`**：构造一个**拷贝了实参值**的 tuple。
- **折叠表达式（fold expression）** `(... && cond)`：对一包参数逐个求 `cond` 再做逻辑与，相当于 `cond_1 && cond_2 && ...`。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `include/utils/arguments/arguments.h` | **本讲主角**。`ArgumentsBuilder`、`ArgumentsBuilderImpl`、两个 Collector、`AttrMap` 全在这里，约 130 行。 |
| `include/elewise/device/device_adapter.h` | 消费 `arguments` 的地方。`Run` 从中取出参数、做 tiling、启动 kernel。 |
| `include/utils/tensor.h` | `Atvoss::Tensor<T>` 的定义，是 `inputOutput` 允许的主要类型。 |
| `include/utils/utility.h` | `IsSpecializationOf_v` 模板萃取，支撑 `static_assert` 的类型判定。 |
| `examples/muls/muls.cpp` | 标量参与运算的真实样例，本讲用它演示 `inputOutput(in, scalar, out)` 的顺序。 |
| `tests/ut/host/test_arguments.cpp` | Host 侧单测，直接断言 `build()` 产物的内部结构，是理解 tuple 下标最好的素材。 |

## 4. 核心概念与源码讲解

### 4.1 ArgumentsBuilder 链式构造

#### 4.1.1 概念说明

`ArgumentsBuilder` 是用户在 Host 侧构造算子入参的唯一入口，用法是一行链式语句：

```cpp
auto arguments = Atvoss::ArgumentsBuilder{}.inputOutput(in, scalar, out).build();
```

这里有两个名字相近、但职责不同的类型，初学时最容易混淆：

- **`ArgumentsBuilder`**：一个**空的标签结构体**，本身不存任何数据。它只提供两个「入口方法」`inputOutput(...)` 和 `attr(...)`，负责做类型校验并创建出第一个真正的构造器对象。
- **`ArgumentsBuilderImpl<InOutCollector, AttrCollector>`**：真正干活的构造器，内部持有两个 Collector（分别管「输入输出」和「属性」）。链式调用其实是在 `ArgumentsBuilderImpl` 上进行的。

这种设计叫**不可变构造器（immutable builder）+ 类型累积（type-state）**：每一次链式调用都返回一个**全新的** `ArgumentsBuilderImpl`，它的模板参数（两个 Collector 的类型）比上一次「更宽」。也就是说，累积的进度不是存在某个运行时变量里，而是**编码在对象的 C++ 类型里**。当你写下 `.inputOutput(in, scalar, out)` 时，编译器其实已经把「三个参数的类型」写进了返回值的类型名中。

#### 4.1.2 核心流程

把链式调用拆成三步来看：

1. **入口**：`ArgumentsBuilder{}` 创建空标签对象；调用 `.inputOutput(...)` 做类型校验，再用 `std::forward_as_tuple` 把实参（按引用）打包，构造出第一个 `ArgumentsBuilderImpl`。
2. **累积**：后续每调用一次 `.inputOutput(...)` 或 `.attr(...)`，`ArgumentsBuilderImpl` 都会调用对应 Collector 的 `Add...` 方法，用 `std::tuple_cat` 拼出一个更宽的 tuple，并返回一个模板参数更新后的新 `ArgumentsBuilderImpl`。原对象不变。
3. **收口**：`.build()` 把两个 Collector 内部的 tuple 再套进一个外层 tuple，作为最终产物返回。

用伪代码描述这一行的演化：

```
ArgumentsBuilder{}                                  // 空标签
  .inputOutput(in, scalar, out)                     // → ArgumentsBuilderImpl<Collector<(in,scalar,out)>, Collector<()>>
  .build()                                          // → tuple< (in,scalar,out) , () >
```

#### 4.1.3 源码精读

先看入口结构体 `ArgumentsBuilder`，它是这一切的起点：

[include/utils/arguments/arguments.h:L101-L129](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/arguments/arguments.h#L101-L129) —— `ArgumentsBuilder` 空标签结构体，提供 `inputOutput` 与 `attr` 两个入口方法，创建第一个 `ArgumentsBuilderImpl`。

注意它的 `inputOutput` 里先做两条 `static_assert`（4.3 节详解），然后用 `std::forward_as_tuple` 把实参按引用打包，并构造出初始的两个 Collector（输入输出 Collector 装入参数、属性 Collector 为空 `std::tuple<>`），最后交推导指引返回 `ArgumentsBuilderImpl`。

再看真正的构造器 `ArgumentsBuilderImpl`：

[include/utils/arguments/arguments.h:L70-L99](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/arguments/arguments.h#L70-L99) —— `ArgumentsBuilderImpl` 持有两个 Collector；`inputOutput`/`attr` 各自只增长自己负责的 Collector，返回新的 `ArgumentsBuilderImpl`；`build()` 把两个 tuple 套进外层 tuple。

这里有一个精妙之处：`inputOutput` 方法只动 `inOutCollector`（把 `attrCollector` 原样透传），`attr` 方法只动 `attrCollector`。所以两者**可以任意交错、反复调用**，例如 `.inputOutput(a).attr("k",1).inputOutput(b)` 也能正常累积。第 98–99 行的推导指引（deduction guide）让 `ArgumentsBuilderImpl{ic, ac}` 能自动推出模板参数，省去手写冗长的类型。

#### 4.1.4 代码实践

**实践目标**：在纸上推导一次两步链式调用的返回类型，体会「类型即状态」。

**操作步骤**：

1. 假设有 `Atvoss::Tensor<float> t1, t2;` 与 `float s;`。
2. 写出 `Atvoss::ArgumentsBuilder{}.inputOutput(t1, s)` 的返回类型中 `InOutCollector` 的模板参数（提示：`std::tuple<T1&, float&>`，`T1` 为 `Tensor<float>`）。
3. 在其后继续接 `.inputOutput(t2)`，用 `std::tuple_cat` 的语义写出新的 `InOutCollector` 模板参数。

**需要观察的现象**：每一次链式调用后，返回值类型的「tuple 越来越长」；而原来的对象类型保持不变。

**预期结果**：

- 第 2 步：`InputOutputCollector<std::tuple<Tensor<float>&, float&>>`
- 第 3 步：`InputOutputCollector<std::tuple<Tensor<float>&, float&, Tensor<float>&>>`

> 待本地验证：以上类型推导可在本机用一段只含 `<type_traits>`、`<tuple>` 与 `static_assert(std::is_same_v<...>)` 的小程序（不依赖 CANN）编译验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ArgumentsBuilder` 本身不存数据，而是立刻返回一个 `ArgumentsBuilderImpl`？

**参考答案**：`ArgumentsBuilder` 只承担「入口 + 类型校验」职责。把累积状态放进 `ArgumentsBuilderImpl` 的模板参数里，可以让「已经收集了哪些参数」成为编译期类型信息，从而在 `build()` 时由 `DeviceAdapter` 端做精确的类型推导与下标校验；而入口处的两条 `static_assert` 只需写一次。

**练习 2**：`.attr("dim", 5).inputOutput(t)` 与 `.inputOutput(t).attr("dim", 5)` 这两种顺序，最终的 `build()` 产物有区别吗？

**参考答案**：没有区别。因为外层 tuple 的第 0 个位置永远是「所有 inputOutput 累积的结果」、第 1 个位置永远是「所有 attr 累积的结果」，两个 Collector 互相独立，调用顺序不影响最终的两层结构（只是内部各 Collector 的累积顺序对应了链上调用顺序，但位置固定）。

### 4.2 InputOutputCollector / AttrCollector

#### 4.2.1 概念说明

`ArgumentsBuilderImpl` 把「输入输出」和「属性」两类参数分别交给两个 Collector 管理。它们是同构的小工具：内部都持有一个 `std::tuple`，提供一个 `Add...` 方法，用 `std::tuple_cat` 把新内容拼进去、返回一个模板参数更新后的新 Collector。

两类参数在语义和存储方式上有重要差别：

- **`InputOutputCollector`**：管理算子的张量入参/出参与标量入参（即 `Compute()` 里 `PlaceHolder` 对应的实参）。这些实参往往是已经存在的外部变量（如 `Atvoss::Tensor`、`float scalar`），所以 Collector 用 **`std::forward_as_tuple`（存引用）**，避免拷贝、也避免设备指针的浅拷贝语义出错。
- **`AttrCollector`**：管理 Host 侧的「属性」键值对（`AttrMap<Key, Value>`）。属性一般是字面量（如 `.attr("dim", 5)`），用 **`std::make_tuple`（存值）** 把键值对拷贝进去。

`AttrMap` 是一个极简的键值对结构体（两个公开成员 `key`、`value`），由工厂函数 `MakeAttr` 构造。

#### 4.2.2 核心流程

`Add` 方法每调用一次，类型演化如下（以 `InputOutputCollector` 为例）：

\[
\text{Collector}\langle \text{Tuple}_{\text{old}}\rangle
\;\xrightarrow{\text{AddInputOutput}(x,y)\!
}\;
\text{Collector}\langle\, \text{Concat}(\text{Tuple}_{\text{old}},\ \langle x\&,\ y\&\rangle)\,\rangle
\]

其中 `Concat` 就是 `std::tuple_cat` 的类型层面表现，由辅助 traits `ConcatTuples` 完成：

[include/utils/arguments/arguments.h:L33-L42](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/arguments/arguments.h#L33-L42) —— `ConcatTuples` 在类型层面把两个 tuple 的元素序列拼成一个。

#### 4.2.3 源码精读

`AttrMap` 与 `MakeAttr`：

[include/utils/arguments/arguments.h:L21-L31](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/arguments/arguments.h#L21-L31) —— `AttrMap<Key,Value>` 键值对与工厂函数 `MakeAttr`。

`InputOutputCollector`（注意第 51 行用的是 `forward_as_tuple`，存引用）：

[include/utils/arguments/arguments.h:L44-L55](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/arguments/arguments.h#L44-L55) —— `InputOutputCollector` 用 `std::tuple_cat` + `std::forward_as_tuple` 累积输入输出实参（按引用）。

`AttrCollector`（注意第 64 行用的是 `make_tuple`，存值，且每个元素是 `MakeAttr(key, value)` 产出的 `AttrMap`）：

[include/utils/arguments/arguments.h:L57-L68](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/arguments/arguments.h#L57-L68) —— `AttrCollector` 用 `std::tuple_cat` + `std::make_tuple(MakeAttr(...))` 累积属性键值对（按值）。

#### 4.2.4 代码实践

**实践目标**：理解「引用 vs 值」两种存储方式的差异，预测传临时量的后果。

**操作步骤**：

1. 阅读上面两段源码，确认 `AddInputOutput` 用 `forward_as_tuple`、`AddAttr` 用 `make_tuple`。
2. 设想如下（**错误示例**，仅作分析，不建议运行）代码：

   ```cpp
   // 错误示例：把临时 Tensor 传给 inputOutput
   auto arguments = Atvoss::ArgumentsBuilder{}.inputOutput(Atvoss::Tensor<float>(ptr, shape)).build();
   deviceOp.Run(arguments, stream); // arguments 持有的是已销毁临时对象的引用
   ```

3. 解释为什么这会导致悬垂引用；而 `.attr("dim", 5)` 里 `5` 是字面量，拷贝进 tuple 没有这个问题。

**需要观察的现象 / 预期结果**：`inputOutput` 的实参必须在 `deviceOp.Run` 使用 `arguments` 期间一直存活（典型做法是把 `Tensor`/标量声明为函数局部变量，贯穿整个 Run 生命周期，如 muls 样例）。`attr` 因为按值拷贝则无此约束。

> 待本地验证：实际工程中不会出现这种悬垂引用，因为样例（见 4.4 节）都把 `in`/`out`/`scalar` 声明为 `Run` 函数内的局部变量，生命周期覆盖了 `deviceOp.Run`。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `AddInputOutput` 里的 `std::forward_as_tuple` 改成 `std::make_tuple`，对 `Atvoss::Tensor` 实参会有什么影响？

**参考答案**：`Tensor` 会被拷贝。由于 `Tensor` 只持有「设备指针 + 形状」且定义了默认析构（不释放设备内存，见 `tensor.h`），拷贝本身是浅拷贝、语义上仍指向同一块 GM，所以功能上「可能」仍正确；但这违背了「不持有内存、只包装」的轻量设计意图，也带来不必要的拷贝开销。因此框架选择按引用累积。

**练习 2**：`MakeAttr(key, value)` 的返回类型是什么？连续两次 `.attr("a", 1).attr("b", 2.0)` 后，`AttrCollector` 的 tuple 类型是什么？

**参考答案**：返回 `AttrMap<decltype(key), decltype(value)>`。两次调用后，由于两次的 key/value 类型不同（假设 `"a"`/`"b"` 同为 `const char*`、`1` 为 `int`、`2.0` 为 `double`），tuple 类型为 `std::tuple<AttrMap<const char*, int>, AttrMap<const char*, double>>`。

### 4.3 入参类型约束 static_assert

#### 4.3.1 概念说明

`ArgumentsBuilder::inputOutput` 不是「什么都能传」。入口处有两条 `static_assert` 在编译期把关：

1. **禁止指针**：所有实参都不能是指针类型。
2. **只允许 `Atvoss::Tensor` 或标量**：每个实参要么是 `Atvoss::Tensor<...>` 的特化，要么是标量（`int`、`float` 等满足 `std::is_scalar_v` 的类型）。

为什么要卡这两条？答案在「消费端」`DeviceAdapter`。算子启动前，`TransformArgs` 会对每个实参做分派：标量原样透传，`Tensor` 调 `GetPtr()` 取设备指针。如果允许裸指针进来，框架就无法区分「这是一个标量值，还是一块设备内存地址」，类型系统就失语了。所以约束不是刁难，而是把「张量 vs 标量」的二分在入口就钉死。

#### 4.3.2 核心流程

约束用 C++17 折叠表达式对一包参数逐个判定：

\[
(\cdots\ \&\&\ \text{allowed}(T_i))\quad\text{其中}\quad
\text{allowed}(T) = \text{IsTensor}(T)\ \lor\ \text{is\_scalar}(T)
\]

`std::decay_t<T>` 先把 `T` 的引用、`const` 等修饰剥掉，得到裸类型再判定。`IsSpecializationOf_v<Atvoss::Tensor, U>` 判断 `U` 是否形如 `Tensor<某类型>`；它依赖的萃取定义在 `utility.h`。`Tensor` 自身则用一个空成员别名 `IsTensor` 打了「标记」，配合 `IsTensor_v` 也能识别（DeviceAdapter 里用的就是这个版本）。

#### 4.3.3 源码精读

两条 `static_assert`（注意折叠表达式 `(... && ...)`）：

[include/utils/arguments/arguments.h:L105-L111](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/arguments/arguments.h#L105-L111) —— 入口处禁止指针、只允许 `Tensor` 与标量的两条编译期断言。

支撑判定的类型萃取 `IsSpecializationOf`：

[include/utils/utility.h:L21-L28](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/utility.h#L21-L28) —— `IsSpecializationOf` 判断某类型是否是给定类模板的特化。

`Tensor` 自身的标记成员（第 75 行的 `using IsTensor = void;`）：

[include/utils/tensor.h:L22-L81](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/tensor.h#L22-L81) —— `Atvoss::Tensor<T>` 定义；第 75 行的 `IsTensor` 标记让 `IsTensor_v` 能在别处识别它。

消费端的 `TransformArgs`，解释了「为什么必须是 Tensor 或标量」：

[include/elewise/device/device_adapter.h:L46-L57](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/device/device_adapter.h#L46-L57) —— `TransformArgs` 用 `if constexpr` 把标量原样透传、把 `Tensor` 转为设备指针，这正是入口类型约束的存在理由。

#### 4.3.4 代码实践

**实践目标**：亲手触发两条 `static_assert`，看懂报错信息。

**操作步骤**：

1. 在 muls 样例的 `Run` 函数里，把第 179 行临时改成下面三种写法之一，分别编译（用 `bash scripts/build.sh -DSOC=ascend950 muls`）：

   ```cpp
   // (a) 传入裸指针 —— 触发「禁止指针」断言
   auto arguments = Atvoss::ArgumentsBuilder{}.inputOutput(deviceInput).build();
   // (b) 传入一个 struct（非 Tensor、非标量）—— 触发「只允许 Tensor/标量」断言
   auto arguments = Atvoss::ArgumentsBuilder{}.inputOutput(options).build();
   // (c) 正确写法：Tensor + 标量
   auto arguments = Atvoss::ArgumentsBuilder{}.inputOutput(in, scalar, out).build();
   ```

2. 记录 (a)、(b) 两条编译错误各自命中的 `static_assert` 文案。

**需要观察的现象**：(a) 命中 `"Pointer types are not allowed..."`；(b) 命中 `"Only Atvoss::Tensor and scalar types are allowed..."`；(c) 正常编译。

**预期结果**：两条断言文案与源码第 107、111 行完全一致。改完后**务必还原**第 179 行。

> 待本地验证：实际编译错误文案与编译器（bisheng）版本相关，但命中的断言位置不变。

#### 4.3.5 小练习与答案

**练习 1**：`float&`（带引用）能通过第二条 `static_assert` 吗？为什么源码里要写 `std::decay_t<InitialInputOutput>`？

**参考答案**：能通过。`std::is_scalar_v<float>` 为真，但 `float&` 直接喂给 `is_scalar_v` 时，引用类型需先退化。`std::decay_t` 把 `float&` 退化成 `float`、剥掉 `const` 等，确保判定针对裸类型。

**练习 2**：如果有人想传一个 `std::vector<float>` 当输入，会发生什么？正确的做法是什么？

**参考答案**：`std::vector` 既不是 `Tensor` 特化也不是标量，会编译失败。正确做法是先把数据搬到 Device 显存（`aclrtMalloc`/`aclrtMemcpy`），再用设备指针构造 `Atvoss::Tensor<float>` 传入。这正是 muls 样例第 5、6、7 步做的事。

### 4.4 build() 产物结构与消费

#### 4.4.1 概念说明

链式调用的终点 `.build()` 返回一个**两层嵌套的 `std::tuple`**：

\[
\text{arguments} \;=\; \big\langle\ \underbrace{\text{inputOutputTuple}}_{\text{第 0 位}},\ \underbrace{\text{attrTuple}}_{\text{第 1 位}}\ \big\rangle
\]

- **第 0 位**：一个 tuple，按 `inputOutput(...)` 的调用顺序，存放所有张量与标量实参（引用）。
- **第 1 位**：一个 tuple，存放所有 `AttrMap` 键值对（值）。

最关键的一条对应规则：**`inputOutput` 的第 `k` 个实参（从 0 起），对应 `Compute()` 里 `PlaceHolder<k+1>`**。即 `PlaceHolder` 的序号 `N` 是 1-based，而 tuple 下标是 0-based：

\[
\text{PlaceHolder}\langle N\rangle \ \longleftrightarrow\ \text{inputOutputTuple}[N-1]
\]

这一点在消费端 `DeviceAdapter` 里被严格兑现：取参数时用的下标是 `ParamType::number - 1`。

#### 4.4.2 核心流程

`DeviceAdapter::Run(arguments, stream)` 消费产物的三步（沿用 u1-l3/u2-l7 的框架，这里只看它如何拆 `arguments`）：

1. **取输入输出 tuple**：`auto argTuple = std::get<0>(arguments);` —— 拿到第 0 位。
2. **准备参数**：`PrepareParams` 对每个 `PlaceHolder`（其 `number` 即序号 `N`），用 `std::get<N-1>(argTuple)` 取出对应实参，构造运行期参数对象。
3. **算 tiling**：`CalculateTiling(arguments, opParam)` 把**整个** `arguments`（含第 1 位的 attr tuple）传给调度器，供 `MakeScheduleConfig` 使用。

也就是说：**第 0 位用于取参数和启动 kernel，第 1 位（attr）供 tiling/调度配置消费**。属性是框架预留的扩展通道（API 文档说明 attr 供 `scheduleConfig` 计算使用），本仓库的默认调度策略（`UniformSegment`、`DefaultBlockSchedule`）在示例中未读取 attr，因此样例里常见到不带 `.attr(...)` 的写法。

#### 4.4.3 源码精读

`build()` 的实现，一眼看到外层两层 tuple：

[include/utils/arguments/arguments.h:L92-L95](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/arguments/arguments.h#L92-L95) —— `build()` 返回 `std::make_tuple(inputOutput, attrs)`，即两层 tuple。

muls 样例的完整构造与对应 `PlaceHolder`：

[examples/muls/muls.cpp:L32-L34](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/muls/muls.cpp#L32-L34) —— `Compute()` 里 `PlaceHolder<1>=in`、`PlaceHolder<2>=scalar`、`PlaceHolder<3>=out`。

[examples/muls/muls.cpp:L179](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/muls/muls.cpp#L179) —— `inputOutput(in, scalar, out)` 顺序与上面三个 `PlaceHolder` 一一对应（`in→1`、`scalar→2`、`out→3`）。

消费端 `DeviceAdapter::Run` 拆开产物：

[include/elewise/device/device_adapter.h:L97-L124](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/device/device_adapter.h#L97-L124) —— `Run` 用 `std::get<0>(arguments)` 取输入输出 tuple（第 104 行），交给 `PrepareParams` 与 `ConvertArgs`；把整个 `arguments` 交给 `CalculateTiling`（第 110 行）。

序号 `N` 到下标 `N-1` 的映射就在 `ConstructParam`：

[include/elewise/device/device_adapter.h:L182-L192](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/device/device_adapter.h#L182-L192) —— `ConstructParam` 用 `ParamType::number - 1` 作为 tuple 下标取实参，这就是 `PlaceHolder<N> ↔ inputOutput[N-1]` 的兑现点。

最有说服力的是 Host 侧单测，它直接对 `build()` 产物做下标断言：

[tests/ut/host/test_arguments.cpp:L88-L100](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/ut/host/test_arguments.cpp#L88-L100) —— `inputOutput(t1, t2, a, t3)` 后，`std::get<0>(arguments)` 取第 0 位，再 `std::get<2>(...)` 取第 3 个实参 `a`，断言等于 `1.0f`。

#### 4.4.4 代码实践

**实践目标**：用 Host 侧单测亲眼验证两层 tuple 的下标结构（无需 NPU，可在本机跑）。

**操作步骤**：

1. 阅读 `tests/ut/host/test_arguments.cpp` 第 88–100 行，理解 `inputOutput(t1, t2, a, t3)` 与断言 `std::get<2>(std::get<0>(arguments)) == 1.0f` 的对应关系：`a` 是第 3 个实参（下标 2），所以两层 `get` 先取第 0 位再取下标 2。
2. 编译并运行 host 单测（来自 u1-l2 的构建方式）：

   ```bash
   bash scripts/build.sh -DSOC=ascend950 --host_ut
   ```

3. 观察终端是否输出 `AtvossArgumentsTestCase` 通过。

**需要观察的现象**：单测通过，证明 `build()` 产物第 0 位确实是「按 `inputOutput` 顺序排列的实参 tuple」。

**预期结果**：googletest 打印 `[  PASSED  ]` 中包含 `AtvossArgumentsTestCase`。

> 待本地验证：本机是否已配置 `ASCEND_HOME_PATH` 等 CANN 环境变量；host 单测不依赖真机，但编译仍需 CANN 工具链头文件。

#### 4.4.5 小练习与答案

**练习 1**：muls 样例里 `inputOutput(in, scalar, out)`，`PlaceHolder<2>` 对应的实参是什么？`DeviceAdapter` 会用哪个下标去取它？

**参考答案**：对应 `scalar`（`float`）。`PlaceHolder<2>` 的 `number=2`，`DeviceAdapter` 用 `std::get<2-1>(argTuple) = std::get<1>(argTuple)` 取它，即 inputOutput tuple 的第 2 个实参。

**练习 2**：如果 `Compute()` 里声明了 `PlaceHolder<1>`、`PlaceHolder<2>`、`PlaceHolder<4>`（跳过了 3），而 `inputOutput` 只传了 3 个参数，会怎样？

**参考答案**：会出问题。序号空间要求从 1 起连续（见 u2-l2），跳号在表达式收集阶段就会触发 `static_assert`；即便绕过，`DeviceAdapter` 按 `number-1` 下标取参也会越界或错位。因此 `inputOutput` 的参数个数与顺序必须与 `PlaceHolder` 的连续序号严格一致。

## 5. 综合实践

把本讲四个模块串起来，完成下面这个综合任务。

**任务**：为一个「2 个 Tensor 输入 + 1 个 scalar + 1 个 Tensor 输出」的算子（例如 `out = (in1 + in2) * scale`）写出完整的 `ArgumentsBuilder` 构造语句，并说明顺序对应关系。

**操作步骤**：

1. **设计 `Compute()` 与序号**（参照 muls 样例的写法）：

   ```cpp
   // 示例代码：仅展示 PlaceHolder 与表达式结构，非仓库已有算子
   struct MyCompute {
       template <template <typename> class Tensor>
       __host_aicore__ constexpr auto Compute() const
       {
           auto in1   = Atvoss::PlaceHolder<1, Tensor<float>, Atvoss::ParamUsage::IN>();
           auto in2   = Atvoss::PlaceHolder<2, Tensor<float>, Atvoss::ParamUsage::IN>();
           auto scale = Atvoss::PlaceHolder<3, float,        Atvoss::ParamUsage::IN>();
           auto out   = Atvoss::PlaceHolder<4, Tensor<float>, Atvoss::ParamUsage::OUT>();
           return (out = (in1 + in2) * scale);
       }
   };
   ```

2. **在 Host 侧构造实参并组装**（接在 u1-l5 的 ACL 初始化、内存分配与 H2D 拷贝之后）：

   ```cpp
   // 示例代码：in1/in2/out 为已分配并拷贝好设备数据的 Atvoss::Tensor<float>
   Atvoss::Tensor<float> in1(deviceIn1, shapeArray, dims);
   Atvoss::Tensor<float> in2(deviceIn2, shapeArray, dims);
   Atvoss::Tensor<float> out(deviceOut,  shapeArray, dims);
   float scale = 2.0f;

   // 顺序必须与 PlaceHolder 序号一一对应：1→in1, 2→in2, 3→scale, 4→out
   auto arguments = Atvoss::ArgumentsBuilder{}.inputOutput(in1, in2, scale, out).build();
   deviceOp.Run(arguments, stream);
   ```

3. **附加一个 attr**（体验第 1 位 tuple，可选）：把上一步改为

   ```cpp
   auto arguments = Atvoss::ArgumentsBuilder{}
                       .inputOutput(in1, in2, scale, out)
                       .attr("axis", 1)
                       .build();
   ```

   并在纸上画出此时 `arguments` 的两层结构。

**需要观察的现象 / 预期结果**：

- `inputOutput` 的 4 个实参顺序为 `in1, in2, scale, out`，分别对应 `PlaceHolder<1..4>`。
- `build()` 产物结构为：

  \[
  \big\langle\,( \text{in1\&},\ \text{in2\&},\ \text{scale\&},\ \text{out\&}\,),\ ( \text{AttrMap}(\text{"axis"},1)\,)\,\big\rangle
  \]

  （不带 attr 时第 1 位为空 `std::tuple<>`。）
- `DeviceAdapter::Run` 用 `std::get<0>` 取第 0 位，再按 `number-1` 把 `in1/in2/scale/out` 分别喂给 `PlaceHolder<1..4>`。

> 待本地验证：完整运行需要真机或 cannsim；若只想验证入参结构，可仿照 `test_arguments.cpp` 写一个只断言 `std::get<>` 下标的 host 单测，用 `--host_ut` 编译运行。

## 6. 本讲小结

- `ArgumentsBuilder` 是空标签入口，真正累积状态的是 `ArgumentsBuilderImpl`；链式调用每次返回一个模板参数更宽的新对象，**累积进度编码在类型里**。
- 两个 Collector 分工：`InputOutputCollector` 用 `forward_as_tuple` **按引用**存张量/标量；`AttrCollector` 用 `make_tuple` **按值**存 `AttrMap` 键值对；两者都靠 `std::tuple_cat` 拼接。
- `inputOutput` 入口有两条 `static_assert`：禁止指针、只允许 `Atvoss::Tensor` 与标量——这是为了配合消费端 `TransformArgs` 的「标量透传 / Tensor 取指针」二分分派。
- `.build()` 产出**两层 tuple**：第 0 位是 inputOutput 实参 tuple，第 1 位是 attr tuple。
- **顺序契约**：`PlaceHolder<N>` 的序号 `N`（1-based）对应 `inputOutput` 的第 `N` 个实参；消费端用 `std::get<N-1>` 取参，所以 `Compute()` 的序号必须从 1 连续，且与 `inputOutput` 顺序严格一致。
- attr 是供 `scheduleConfig`/tiling 使用的扩展通道，默认调度策略在示例中未读取它，故样例常省略 `.attr(...)`。

## 7. 下一步学习建议

本讲把「入参怎么进、怎么被取」讲透了，但 `deviceOp.Run(arguments, stream)` 内部仍只展开了「取参数」这一步。建议接下来：

- **u2-l7（Device 层）**：跟进 `DeviceAdapter::Run` 的完整三步 `PrepareParams → CalculateTiling → LaunchKernelWithDataTuple`，看 `arguments` 如何驱动 tiling 与 kernel 启动。
- **u2-l8 / u2-l9（Kernel / Block 层）**：看 `CalculateTiling` 如何把任务切到多核、再切到单核 Tile，理解 attr 这类 host 属性未来会在哪一层被消费。
- 若想巩固本讲的 tuple/类型萃取，可继续阅读 `tests/ut/host/test_arguments.cpp` 以及 `docs/api/ArgumentsBuilder_*.md` 三份 API 文档（`inputOutput`、`attr`、`build`），它们与本讲源码一一对应。
