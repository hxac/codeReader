# Op 定义与注册机制

## 1. 本讲目标

学完本讲，你应该能够：

- 用一句话说清 TensorFlow 里 **Op（operation，算子）** 到底是什么，以及它与 u2 讲过的 `Operation` 对象、u3 讲过的图节点 `Node` 的区别。
- 读懂一个 `OpDef`：知道它的 `name`、`input_arg`、`output_arg`、`attr` 四个核心字段分别声明了什么。
- 看懂 `REGISTER_OP("Add")...` 这种链式写法背后真正发生了什么：宏 → 构建器 → 全局注册表。
- 解释「注册发生在程序运行的哪个阶段」：为什么说注册是**启动期登记、惰性求值**的两段式过程。
- 自己挑一个 `core/ops` 下的 op，列出它的输入、输出与属性，并讲清它何时被加入全局表。

本讲只聚焦一个最小模块：`core.framework.op`（对应的 `op.h` / `op.cc`）。配套会引用定义数据结构的 `op_def.proto`、构建器 `op_def_builder.h`，以及底层初始化机制 `registration/registration.h`。

## 2. 前置知识

在进入源码前，先建立两个直觉。

**第一，Op 是「计算原语的说明书」，不是计算本身。** 回顾 u3-l1：图里的每个 `Node` 在序列化后的 `NodeDef` 中都有一个 `op` 字段，它是一个**字符串**，比如 `"Add"`。但这个字符串本身不会算加法——它只是一个名字。运行时拿到 `"Add"` 这个名字后，必须去某个地方查「这个名字到底代表什么操作、接受几个输入、什么类型、产出什么」。那张「名字 → 说明书」的对照表，就是本讲要讲的**全局 Op 注册表（OpRegistry）**，而「说明书」就是 **OpDef**。

换句话说：

- `tf.constant` / `tf.add` 在 Python 侧创建的 `Operation`（u2-l4）→ 是用户能摸到的对象；
- 图里的 `NodeDef.op = "Add"`（u3-l1）→ 是序列化后留下的一个名字；
- 本讲的 `OpDef` → 是这个名字背后那份「这张说明书」。

三者指向同一个概念，但出现在不同的层次。

**第二，注册表是「键值存储 + 工厂模式」。** 你可以把 OpRegistry 想成一张巨大的 `map<string, OpDef>`。这张表在程序一启动时就开始被填充，填充的方式不是手写一堆 `table["Add"] = ...`，而是每个 op 用一行 `REGISTER_OP(...)` 宏「自报家门」，由 C++ 的**静态全局对象初始化**机制自动登记进去。这正是 u1-l5 里讲 DirectSession 时提到的「静态自动注册」同款手法。

如果你对「静态全局对象的初始化时机」「lambda 捕获」不熟，本讲会用伪代码讲清，不必担心。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用它做什么 |
| --- | --- | --- |
| `tensorflow/core/framework/op_def.proto` | 用 protobuf 定义 `OpDef` / `ArgDef` / `AttrDef` 的数据结构 | 看「说明书」长什么样 |
| `tensorflow/core/framework/op_def_builder.h` | `OpDefBuilder` 链式构建器与 `OpRegistrationData` | 看 `.Input().Output().Attr()` 如何拼出 OpDef |
| `tensorflow/core/framework/op.h` | `OpRegistry`、`REGISTER_OP` 宏、`OpDefBuilderWrapper` | 看注册的对外 API 与宏定义 |
| `tensorflow/core/framework/op.cc` | `OpRegistry` 的实现、`OpDefBuilderWrapper::operator()` | 看注册表如何存储、如何惰性求值 |
| `tensorflow/core/framework/registration/registration.h` | `InitOnStartupMarker`、`SHOULD_REGISTER_OP` | 看启动期初始化的底层机制 |
| `tensorflow/core/ops/math_ops.cc` | 用 `REGISTER_OP` 声明 `Add` 等 op | 作为真实例子来精读 |

记住一条主线：**宏（op.h）→ 包装器 operator()（op.cc）→ 把工厂塞进 OpRegistry::Global()（op.cc）→ 首次 LookUp 时工厂被调用、OpDef 落表（op.cc）**。后续四节就沿着这条线展开。

## 4. 核心概念与源码讲解

### 4.1 Op 的本质：OpDef 这份「说明书」

#### 4.1.1 概念说明

一个 Op 在 TF 内部就是一份结构化的「说明书」，它回答四个问题：

1. **你叫什么名字？**（`name`，如 `"Add"`）
2. **你吃几个输入、每个是什么类型？**（`input_arg`）
3. **你吐几个输出、每个是什么类型？**（`output_arg`）
4. **你有哪些「图构造期」可配置的旋钮？**（`attr`，比如数据类型 `T`）

注意第 4 点：很多 op 的输入输出类型并不是写死的，而是由一个**属性（attr）**参数化。比如 `Add` 既能算 `int32` 也能算 `float`，这不是两个 op，而是一个 `Add`，它的类型由属性 `T` 决定。这一点是 TF op 系统的关键设计，也是为什么 `Add` 的输入写成 `x: T` 而不是 `x: float`。

#### 4.1.2 核心流程

`OpDef` 本身是一个 protobuf 消息，结构大致如下（伪代码）：

```
OpDef {
  string name;                  // 如 "Add"
  repeated ArgDef input_arg;    // 输入列表，每个 ArgDef 描述一个输入
  repeated ArgDef output_arg;   // 输出列表
  repeated AttrDef attr;        // 属性列表
  ...
}
```

其中描述每个输入/输出的 `ArgDef` 用一组互斥字段表达「类型从哪来」：

- `type`：类型写死（如 `float`）；
- `type_attr`：类型由某个属性决定（如指向属性 `T`）；
- `number_attr`：表示「一串同类型张量」，数量由某个 `int` 属性决定；
- `type_list_attr`：表示「一串不同类型张量」。

而 `AttrDef` 描述一个属性：它有名字、类型（如 `"type"`、`"int"`、`"list(string)"`），还可能有默认值、最小值约束、允许值集合（`allowed_values`）。

#### 4.1.3 源码精读

`OpDef` 的 protobuf 定义在 [core/framework/op_def.proto:L34-L37](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_def.proto#L34-L37)，这里声明了消息起点与 `name` 字段——这就是 op 的「身份证号」。

输入与输出共用同一种 `ArgDef` 结构，见 [core/framework/op_def.proto:L40-L82](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_def.proto#L40-L82)。注意其中的 `type` / `type_attr` / `number_attr` / `type_list_attr` 四个字段，正是上面流程里说的「类型从哪来」的四种来源。随后 `OpDef` 用两个 `repeated ArgDef` 字段把它们挂为输入与输出：[core/framework/op_def.proto:L84-L88](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_def.proto#L84-L88)。

属性则由 `AttrDef` 描述，见 [core/framework/op_def.proto:L97-L133](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_def.proto#L97-L133)，关键看它的 `type`（第 105 行）、`default_value`（第 109 行）和 `allowed_values`（第 131 行）。最后由 [core/framework/op_def.proto:L133](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_def.proto#L133) 的 `repeated AttrDef attr = 4;` 收进 OpDef。

一句话：OpDef = 名字 + 一张输入表 + 一张输出表 + 一张属性表，全部是 protobuf，因此可以序列化、可以跨语言读取。

#### 4.1.4 代码实践

**目标**：用一张表格把 `Add` 这条说明书的四要素填出来。

**步骤**：

1. 打开 [core/ops/math_ops.cc:L417-L424](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/ops/math_ops.cc#L417-L424)，找到 `REGISTER_OP("Add")`。
2. 对照本节的 `OpDef`/`ArgDef`/`AttrDef` 定义，把下表填完：

| OpDef 字段 | Add 的内容 |
| --- | --- |
| `name` | `"Add"` |
| `input_arg` | 两个：`x: T`、`y: T`（类型都由属性 `T` 决定） |
| `output_arg` | 一个：`z: T` |
| `attr` | 一个：`T`，取值限定在 `{bfloat16, half, float, ...}` 集合内 |

**观察现象**：你会看到输入 `x` 和 `y` 都引用同一个属性 `T`——这正是「两个输入必须同类型」这一约束的**声明式**表达。约束不是写在 C++ kernel 里的 if 判断，而是通过「两个 ArgDef 指向同一个 `type_attr`」让框架自动校验。

**预期结果**：能口述「`Add` 是一个有 2 输入 1 输出、带一个类型属性 `T` 的 op」。

#### 4.1.5 小练习与答案

**练习 1**：如果一个 op 想表达「接受任意数量的同类型输入」，应该用 `ArgDef` 的哪个字段？

**答案**：用 `number_attr`（指向一个 `int` 属性来定数量）配合 `type` 或 `type_attr`（定类型）。

**练习 2**：`AttrDef.allowed_values` 解决什么问题？对照 `Add` 的 `T` 说明。

**答案**：它限定属性的合法取值集合。`Add` 的 `T` 通过 `.Attr("T: {bfloat16, half, float, ...}")` 把允许的类型列出来，这些类型最终落到 `AttrDef.allowed_values`，框架据此拒绝 `Add` 用 `bool` 之类不支持的类型。

### 4.2 OpDefBuilder：链式拼装说明书

#### 4.2.1 概念说明

直接手写 protobuf 的 `OpDef` 太繁琐，于是 TF 提供了一个**链式构建器** `OpDefBuilder`：你用一行行 `.Input(...)`、`.Output(...)`、`.Attr(...)` 把说明书的各部分「挂」上去，最后调用 `Finalize()` 把它们整理成一份合法的 `OpDef`。这正是 `REGISTER_OP("Add").Input("x: T")...` 这种写法能成立的原因。

构建器接收的字符串遵循一套迷你 DSL（领域专用语言），例如：

- `.Attr("T: {float, int32}")` → 名为 `T` 的类型属性，取值限定在集合内；
- `.Attr("T: {float,int32}= float")` → 同上，但默认值是 `float`；
- `.Input("x: T")` → 一个名为 `x` 的输入，类型由属性 `T` 决定；
- `.Input("inputs: N * T")` → `N` 个同类型输入（`N` 必须是某个 `int` 属性）。

#### 4.2.2 核心流程

```
OpDefBuilder builder("Add");     // 1. 以名字起手
builder.Input("x: T");           // 2. 把字符串解析后存进 inputs_ 列表
builder.Input("y: T");
builder.Output("z: T");
builder.Attr("T: {...}");
builder.SetShapeFn(...);         // 3. 可选：挂一个形状推导函数
builder.Finalize(&op_reg_data);  // 4. 把列表整理成 OpDef，校验后写回 op_reg_data
```

`Finalize` 是收口步骤：它把暂存在 `attrs_` / `inputs_` / `outputs_` 里的字符串逐条解析、组装成 protobuf 的 `AttrDef` / `ArgDef`，塞进 `op_reg_data.op_def`，并把形状推导函数挂到 `op_reg_data.shape_inference_fn`。注意它**只报解析错误**，更深的语义问题要靠后续的 `ValidateOpDef()` 才能发现。

#### 4.2.3 源码精读

`OpDefBuilder` 类定义在 [core/framework/op_def_builder.h:L144-L278](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_def_builder.h#L144-L278)。其中 `.Attr()` 的文档（[L149-L180](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_def_builder.h#L149-L180)）详细列出了属性字符串能写哪些类型与约束语法；`.Input()`/`.Output()` 的文档（[L182-L198](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_def_builder.h#L182-L198)）解释了 `<number>*<type>`、`<type-list>` 等表达方式。

`Finalize` 的契约见 [core/framework/op_def_builder.h:L258](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_def_builder.h#L258)，注释明确：它把请求整理成 `OpDef` 与形状推导函数，且「必须在上述所有方法之后调用」。

`Finalize` 的产物落到 `OpRegistrationData`——它是「说明书 + 配套函数」的打包，见 [core/framework/op_def_builder.h:L67-L76](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_def_builder.h#L67-L76)。可以看到它除了 `op_def`，还挂着 `shape_inference_fn`（形状推导）、`type_ctor` / `fwd_type_fn`（类型推导）等回调。

真实例子就是 `Add`，见 [core/ops/math_ops.cc:L417-L424](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/ops/math_ops.cc#L417-L424)：

```cpp
REGISTER_OP("Add")
    .Input("x: T")
    .Input("y: T")
    .Output("z: T")
    .Attr("T: {bfloat16, half, float, double, ...}")
    .SetShapeFn(shape_inference::BroadcastBinaryOpShapeFn);
```

这里链式调用的每个方法，最终都转发到 `OpDefBuilder`。

#### 4.2.4 代码实践

**目标**：理解「字符串 DSL → OpDef 字段」的映射。

**步骤**：

1. 在 [core/ops/math_ops.cc:L426-L435](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/ops/math_ops.cc#L426-L435) 阅读 `REGISTER_OP("AddV2")`，它比 `Add` 多了 `.SetIsAggregate()` 和 `.SetIsCommutative()`。
2. 对每个链式调用，写出它最终影响 `OpDef` 或 `OpRegistrationData` 的哪个字段。例如 `.SetIsCommutative()` 会打开 OpDef 里的「可交换」布尔标记（可被 Grappler 等优化器利用）。

**观察现象**：`AddV2` 与 `Add` 的输入输出结构几乎一样，区别只在属性 `T` 的允许集合更宽、以及多了两个语义标记。

**预期结果**：能说明「`.Attr/.Input/.Output` 决定数据结构，`.SetXxx` 决定优化器可见的语义标记」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `.Attr("T: {float, int32}")` 里 `T` 用大写？

**答案**：按惯例，**能被框架/形状推导自动推断出来的属性用大写**，需要用户显式提供的用小写。`T` 这种类型属性通常能从输入张量推断，故用大写。

**练习 2**：`Finalize` 与 `ValidateOpDef` 各负责什么？

**答案**：`Finalize` 把字符串解析、组装成 `OpDef`，主要报「格式/语法」错误；`ValidateOpDef` 则在 OpDef 已成型后做更深的语义校验（如输入引用了不存在的属性）。两者职责分离。

### 4.3 REGISTER_OP 宏：把声明变成启动期注册

#### 4.3.1 概念说明

到目前为止，`OpDefBuilder` 还只是一个「能拼出 OpDef 的工具」，没人调用它。真正让成百上千个 op 自动进入注册表的，是 `REGISTER_OP` 这个**宏**。

它的妙处在于利用 C++ 的**静态存储期对象初始化**：每个 `REGISTER_OP(...)` 都在文件作用域里展开成一个全局静态变量，其初始化表达式带有副作用——这个副作用就是「把自己登记进全局注册表」。因为全局静态变量的初始化在 `main` 之前（动态库则在加载时）就会执行，所以 op 的登记是**全自动、无需手动调用**的。

#### 4.3.2 核心流程

`REGISTER_OP("Add")` 的展开链路（简化）：

```
1. REGISTER_OP("Add")
2.   → TF_NEW_ID_FOR_INIT(REGISTER_OP_IMPL, "Add", false)   // 用 __COUNTER__ 生成唯一名
3.   → static InitOnStartupMarker register_opN =
        TF_INIT_ON_STARTUP_IF(false || SHOULD_REGISTER_OP("Add"))
        << OpDefBuilderWrapper("Add")                        // 构造包装器
4.   → InitOnStartupMarker::operator<<(...) 内部调用 wrapper.operator()()
5.   → OpRegistry::Global()->Register( 一个捕获了 builder 的 lambda )
```

第 3 步是关键：`TF_INIT_ON_STARTUP_IF(cond)` 是一个用三元运算符模拟 `#ifdef` 的技巧——当 `cond` 为编译期常量 `true` 时，`<<` 后面的 wrapper 才被求值（进而被调用）；为 `false` 时则**根本不求值**，于是链接器可以把对应代码剥掉。这正是「选择性注册」（selective registration）用来给移动端瘦身的机制：把用不到的 op 在编译期就排除。

第 5 步注册进表的，**不是 OpDef 本身**，而是一个**工厂 lambda**——它捕获了 builder，等到将来被调用时才 `builder.Finalize(...)` 真正产出 OpDef。这个「先登记工厂、后求值」的设计是下一节惰性求值的基础。

#### 4.3.3 源码精读

宏本体在 [core/framework/op.h:L318-L320](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op.h#L318-L320)：

```cpp
#define REGISTER_OP(name)        \
  TF_ATTRIBUTE_ANNOTATE("tf:op") \
  TF_NEW_ID_FOR_INIT(REGISTER_OP_IMPL, name, false)
```

它委托给 `REGISTER_OP_IMPL`（[core/framework/op.h:L312-L316](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op.h#L312-L316)），后者展开成一个带副作用的静态全局变量：

```cpp
static InitOnStartupMarker const register_op##ctr TF_ATTRIBUTE_UNUSED =
    TF_INIT_ON_STARTUP_IF(is_system_op || SHOULD_REGISTER_OP(name))
    << ::tensorflow::register_op::OpDefBuilderWrapper(name);
```

底层的 `InitOnStartupMarker` 与 `TF_INIT_ON_STARTUP_IF` 定义在 [core/framework/registration/registration.h:L104-L136](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/registration/registration.h#L104-L136)。其中 `operator<<` 对「可调用对象」会执行 `std::forward<T>(v)()`（[L109-L112](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/registration/registration.h#L109-L112)），这正是触发 `wrapper.operator()` 的地方。`SHOULD_REGISTER_OP` 默认恒为 `true`（[registration.h:L74](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/registration/registration.h#L74)），仅在开启选择性注册时才由外部 `ops_to_register.h` 改写。该文件开头的那段注释（[registration.h:L20-L28](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/registration/registration.h#L20-L28)）把「定义（编译期可用）」与「使用（运行期加入注册表）」两侧讲得很清楚，值得读一遍。

被 `<<` 触发的 `OpDefBuilderWrapper::operator()` 实现在 [core/framework/op.cc:L292-L297](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op.cc#L292-L297)：

```cpp
InitOnStartupMarker OpDefBuilderWrapper::operator()() {
  OpRegistry::Global()->Register(
      [builder = std::move(builder_)](OpRegistrationData* op_reg_data)
          -> absl::Status { return builder.Finalize(op_reg_data); });
  return {};
}
```

这段是整条链路的「咽喉」：它把一个「将来会调用 `builder.Finalize`」的 lambda 注册进 `OpRegistry::Global()`。注意 lambda 是**按值移动捕获** builder 的，所以即便包装器本身是临时对象，builder 也安全地存活在闭包里。

#### 4.3.4 代码实践

**目标**：体会「宏 → 静态全局变量 → 启动期副作用」这一机制。

**步骤**（源码阅读型）：

1. 在 `tensorflow/core/ops/` 目录下，用搜索工具统计 `REGISTER_OP(` 出现的次数（这是 TF 内置 op 数量的量级，通常上千）。
2. 任选一个 op 声明，手动把它「翻译」成 4.3.2 中的五步展开，重点写出第 5 步那个 lambda 长什么样。

**观察现象**：你不会在 `core/ops/*.cc` 里看到任何 `int main()` 或显式的 `RegisterAll()` 调用——所有 op 都是靠散落在各文件的 `REGISTER_OP` 宏，借助链接进来的目标文件里的静态全局变量初始化「自动汇聚」到注册表的。

**预期结果**：能用一句话解释「为什么写一行 `REGISTER_OP` 就等于注册了 op」——因为它展开成一个启动期求值的静态全局变量。

#### 4.3.5 小练习与答案

**练习 1**：`REGISTER_SYSTEM_OP` 与 `REGISTER_OP` 的区别是什么？

**答案**：`REGISTER_SYSTEM_OP`（[op.h:L325-L328](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op.h#L325-L328)）把宏里的 `is_system_op` 置为 `true`，使其**无条件注册**，即便开启了选择性注册也不被裁掉；普通 `REGISTER_OP` 则可能被 `SHOULD_REGISTER_OP` 在编译期排除。

**练习 2**：第 5 步注册的是 lambda 而不是 OpDef，这样做有什么好处？

**答案**：延迟真正构造 OpDef 的时机，避免程序一启动就构造上千份 OpDef（哪怕很多用不上）；工厂模式也让注册表可以统一控制「何时批量求值」（见 4.4）。

### 4.4 OpRegistry：全局注册表与惰性求值

#### 4.4.1 概念说明

所有 `REGISTER_OP` 最终都汇聚到同一个**全局单例** `OpRegistry::Global()`。它对外提供两个核心能力：

- **登记**（`Register`）：把一个工厂函数收下；
- **查询**（`LookUp`）：给定 op 名字（如 `"Add"`），返回对应的 `OpRegistrationData`（含 OpDef）。

运行时在构造图、校验 `NodeDef` 时，就是调 `LookUp("Add")` 拿到说明书来确认「这个节点合法、输入输出对得上」。

它的另一个关键设计是**惰性求值（lazy evaluation）**：登记阶段只把工厂函数攒进一个 `deferred_` 列表，**并不立即构造 OpDef**；直到第一次有人 `LookUp` / `Export` 时，才批量调用这些工厂、把 OpDef 真正填进哈希表。

#### 4.4.2 核心流程

注册表内部用一个布尔位 `initialized_` 区分两态：

```
状态 A（initialized_ == false）：启动期
  └─ Register(factory) 只把 factory push_back 进 deferred_ 列表

状态 B（initialized_ == true）：首次查询触发
  └─ MustCallDeferred() 遍历 deferred_，对每个 factory：
        factory(...)  →  builder.Finalize(...)  →  得到 OpDef
        ValidateOpDef(op_def)
        registry_[op_def.name()] = OpRegistrationData   // 以名字为键落表
     清空 deferred_
  └─ 之后 LookUp 直接查 registry_ 这张哈希表，O(1)
```

查询路径 `LookUp` 先用共享锁读 `registry_`（快路径）；若尚未初始化或没命中，走 `LookUpSlow` 触发上面的批量求值。这是一种**双重检查 + 懒初始化**模式。

从「平均查询代价」看，稳态下是一次哈希查表：

\[ \text{平均查询代价} \approx O(1) \]

而一次性初始化代价被均摊到整个进程生命周期，只发生一次。

#### 4.4.3 源码精读

`OpRegistry` 类声明在 [core/framework/op.h:L69-L178](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op.h#L69-L178)。`Register`（[op.h:L76](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op.h#L76)）与 `LookUp`（[op.h:L78-L79](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op.h#L78-L79)）是两个核心方法，单例入口是 `Global()`（[op.h:L94](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op.h#L94)）。

`Register` 的实现见 [core/framework/op.cc:L56-L63](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op.cc#L56-L63)：

```cpp
void OpRegistry::Register(const OpRegistrationDataFactory& op_data_factory) {
  mutex_lock lock(mu_);
  if (initialized_) {
    TF_QCHECK_OK(RegisterAlreadyLocked(op_data_factory));
  } else {
    deferred_.push_back(op_data_factory);   // 启动期：只攒着
  }
}
```

注意分支：若已经初始化过，新来的 op 会**立即**落表；否则进 `deferred_` 攒着——这就是「启动期登记、惰性求值」的代码体现。

惰性求值的核心在 `MustCallDeferred`，见 [core/framework/op.cc:L211-L220](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op.cc#L211-L220)：它遍历 `deferred_`，对每个工厂调 `RegisterAlreadyLocked`，并把 `initialized_` 置 `true`、清空 `deferred_`。真正「造 OpDef 并落表」的逻辑在 `RegisterAlreadyLocked`，见 [core/framework/op.cc:L236-L255](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op.cc#L236-L255)，其中关键是：

```cpp
auto op_reg_data = std::make_unique<OpRegistrationData>();
absl::Status s = op_data_factory(op_reg_data.get());   // 调工厂 → Finalize 出 OpDef
if (s.ok()) s = ValidateOpDef(op_reg_data->op_def);    // 语义校验
if (s.ok() &&
    !registry_.try_emplace(op_reg_data->op_def.name(), std::move(op_reg_data)).second) {
  s = absl::AlreadyExistsError(...);                    // 重名报错
}
```

可以看到「名字」就是哈希键（`op_def.name()`），重名会被 `try_emplace` 的返回值检测到并报 `AlreadyExistsError`。

查询的快/慢路径在 [core/framework/op.cc:L87-L99](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op.cc#L87-L99)（`LookUp`）与 [core/framework/op.cc:L101-L135](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op.cc#L101-L135)（`LookUpSlow`）。快路径用 `tf_shared_lock` 读 `registry_`；未命中则进 `LookUpSlow`，其内 `MustCallDeferred()`（[L109](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op.cc#L109)）触发首次批量求值。查不到时返回的友好报错在 `OpNotFound`，见 [core/framework/op.cc:L67-L78](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op.cc#L67-L78)——这正是你见过的那种 `Op type not registered '...' in binary running on ...` 报错的来源。

单例本身见 [core/framework/op.cc:L257-L261](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op.cc#L257-L261)，是一个函数内 `static` 指针，保证线程安全的首次初始化。

#### 4.4.4 代码实践

**目标**：把「注册发生在哪个阶段」用一张时间线讲清楚。

**步骤**：

1. 阅读上面的 `Register` / `MustCallDeferred` / `LookUpSlow` 三段代码。
2. 画出程序的时间线，标注下列事件分别发生在哪个阶段（**库加载 / main 之前**、**首次构造或运行图时**、**每次查询时**）：
   - `OpDefBuilderWrapper::operator()` 被调用，工厂被 push 进 `deferred_`；
   - `builder.Finalize` 被调用，OpDef 真正生成；
   - `registry_["Add"]` 被填上；
   - 用户 `tf.add(x, y)` 触发对 `"Add"` 的查询。

**观察现象**：你会发现「登记」与「真正构造 OpDef」之间隔了一段任意长的时间——只要没人查，OpDef 就永远不会被构造。

**预期结果**：能回答「注册发生在哪个阶段」——**登记发生在库加载/main 之前（静态初始化），而 OpDef 的真正构造发生在首次查询时（惰性求值）**。这是本讲的核心结论之一。

> 提示：若想亲眼看到「全部已注册 op」，C++ 侧可调 `OpRegistry::Global()->DebugString(true)`（[op.h:L91](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op.h#L91)）或 `Export`（[op.h:L87](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op.h#L87)）打印 `OpList`。具体运行方式**待本地验证**（取决于你是否本地编译了 TF C++）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `LookUp` 的快路径用 `tf_shared_lock`（共享锁）而不是排他锁？

**答案**：稳态下注册表只读不写，共享锁允许多线程并发查询、不互相阻塞，只有在 `LookUpSlow` 首次初始化时才升级为排他锁 `mutex_lock`，兼顾安全与性能。

**练习 2**：如果两个不同文件里都写了 `REGISTER_OP("Add")`，会发生什么？

**答案**：两次都会登记成功（工厂进 `deferred_`），但当工厂被求值、第二个试图 `registry_.try_emplace("Add", ...)` 时会失败，返回 `AlreadyExistsError("Op with name Add")`（见 `RegisterAlreadyLocked`）。注册失败会被 `TF_QCHECK_OK` 视为致命错误。

## 5. 综合实践

把本讲四节串起来，完成下面这个贯穿性小任务。

**任务**：选一个你感兴趣的 op（例如 `core/ops/array_ops.cc` 里的 `Identity`、`ConcatV2`，或 `core/ops/math_ops.cc` 里的 `Add`、`MatMul`），完整复述它从「一行宏」到「能被运行时查到」的全过程。

**要求产出一份笔记，包含**：

1. **数据结构层**：列出该 op 的 `OpDef` 四要素——`name`、每个 `input_arg`、每个 `output_arg`、每个 `attr`（标出属性的类型与约束）。
2. **构建层**：画出该 op 的 `REGISTER_OP(...)` 链式调用，并说明每行 `.Input/.Output/.Attr/.SetXxx` 分别落到 `OpDef` / `OpRegistrationData` 的哪个字段。
3. **宏展开层**：写出该 `REGISTER_OP` 展开后的静态全局变量，指出它的初始化副作用是调用 `OpDefBuilderWrapper::operator()`。
4. **注册表层**：说明这个副作用把一个捕获了 builder 的工厂 lambda 注册进了 `OpRegistry::Global()` 的 `deferred_` 列表。
5. **求值时机**：明确写出该 op 的 OpDef **何时**真正被构造并落入 `registry_`（首次 `LookUp` 时），以及之后查询的代价是 \(O(1)\)。

**自检**：如果第 5 步你写成了「`REGISTER_OP` 执行时立即构造 OpDef」，那就错了——回去重读 4.4。正确表述必须区分「启动期登记工厂」与「首次查询时惰性构造」两个阶段。

完成后，你就把本讲的核心——**Op = 声明式说明书；注册 = 宏驱动的启动期登记 + 惰性求值；查询 = 名字到 OpDef 的哈希查表**——完整内化了。

## 6. 本讲小结

- **Op 是说明书不是计算**：`OpDef` 用 protobuf 声明一个 op 的 `name` / `input_arg` / `output_arg` / `attr`，回答「叫什么、吃什么、吐什么、有哪些旋钮」。
- **类型靠属性参数化**：输入输出的类型常由 `type_attr` 指向某个属性（如 `T`），「两个输入同类型」这种约束是声明式表达的。
- **OpDefBuilder 是链式 DSL**：`.Input/.Output/.Attr` 把字符串解析后攒起来，`Finalize` 收口成 OpDef。
- **REGISTER_OP 利用静态初始化**：宏展开成带副作用的全局静态变量，副作用就是把「一个工厂 lambda」登记进全局注册表，因此无需手动调用。
- **OpRegistry 是惰性求值的全局单例**：启动期只攒工厂到 `deferred_`，首次 `LookUp` 时才批量构造 OpDef 落入以名字为键的哈希表，稳态查询 \(O(1)\)。
- **「注册发生在哪个阶段」要分两段答**：登记发生在库加载/main 之前，OpDef 真正构造发生在首次查询时。

## 7. 下一步学习建议

本讲只讲了 op 的**声明与注册**，还没讲它的**计算实现**。一个 op 名字（如 `"Add"`）能被查到说明书，但真正在 CPU/GPU 上算出结果靠的是 **OpKernel**。建议下一讲学习 **u4-l2 OpKernel 与 Compute 接口**（`core/framework/op_kernel.h` / `op_kernel.cc`），理解：

- 为什么一个 Op 会有**多个** Kernel（CPU 版、GPU 版、不同 dtype 版）；
- `OpKernel::Compute(OpKernelContext*)` 这个契约如何承载真正的计算；
- 内核又是如何通过 `REGISTER_KERNEL_BUILDER` 注册、并与本讲的 Op 注册表配合的（kernel 注册表按「op 名 + 设备 + 类型」三维索引）。

如果你对声明里的形状推导函数（`SetShapeFn`）更感兴趣，可以先旁听 **u4-l3 Op 实现与属性/形状推导**，看 `core/ops` 下的形状推导函数如何在不执行 op 的情况下静态推出输出形状。两条线最终都汇合到 **u4-l4 C API 与 pywrap**，揭示 Python 侧的 `tf.add` 如何跨语言走到这里注册好的 op 上。
