# Op 实现与属性/形状推导

## 1. 本讲目标

本讲是「Op 与 Kernel 注册机制」单元的第三讲，承接 u4-l1（`REGISTER_OP` 与全局 `OpRegistry`）和 u4-l2（`OpKernel::Compute`）。

学完本讲，你应该能够：

1. 说出 `tensorflow/core/ops/` 这个目录到底装了什么——它只写 Op 的「声明」（输入、输出、属性）和「形状推导函数」，**不写任何计算代码**。
2. 看懂 `.SetShapeFn(...)` 这一行，理解它把一个 `OpShapeInferenceFn` 挂到了 Op 注册表里。
3. 掌握形状推导函数的标准写法：拿到一个 `InferenceContext* c`，读输入形状、读属性，然后用一套「形状代数」算出输出形状并 `c->set_output(...)`。
4. 区分「可复用形状函数库 `common_shape_fns.h`」与「内联在 `array_ops.cc` 里的局部形状函数」，知道何时复用、何时手写。
5. **解释清楚：为什么形状推导必须独立于 kernel 实现。** 这是本讲的核心问题，也是后续理解 MLIR/Grappler 图优化、`tf.function` tracing 的前提。

---

## 2. 前置知识

本讲默认你已经掌握 u4-l1、u4-l2 的关键概念，下面只做最简提醒：

- **Op（OpDef）= 说明书**：由 protobuf 消息描述，含 `name`、`input_arg`、`output_arg`、`attr` 四类字段，声明「这个 op 长什么样」，由 `REGISTER_OP` 在程序启动期登记进全局 `OpRegistry`。
- **OpKernel = 干活的工人**：一个 Op 按「设备 × 元素类型」会有多个 kernel，每个 kernel 子类必须实现 `Compute(OpKernelContext*)`，在 u4-l2 已讲。
- **形状（shape）与秩（rank）**：张量的形状是一组维度，例如 `[2,3]`；秩是维度的个数，例如 `[2,3]` 的秩是 2。动态图里有的维度在「建图/tracing 期」可能未知（记为 `?`），需要运行时才知道。

本讲引入两个新对象：

- **`InferenceContext`**：形状推导函数收到的「工具箱」对象，封装了所有对输入形状、属性、张量常量的访问，以及一套形状构造与运算方法。
- **`OpShapeInferenceFn`**：形状推导函数的类型签名 `absl::Status(shape_inference::InferenceContext* c)`，返回 `absl::Status` 表示「成功算出形状」或「输入不合法」。

> 关键直觉：**形状推导发生在「建图 / tracing」阶段，比真正在设备上跑 kernel 早得多。** 它是静态分析，不是执行。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲角色 |
| --- | --- | --- |
| `tensorflow/core/ops/array_ops.cc` | 数组类 Op（Concat、Transpose、Reshape、Shape、Gather…）的 **声明 + 形状推导函数** | 主角：最丰富的形状推导示例集合 |
| `tensorflow/core/framework/common_shape_fns.h` | 可复用形状函数库（`UnchangedShape`、`ScalarShape`、`BroadcastBinaryOpShapeFn`、`ConcatShape`…） | 主角：跨 op 复用的「形状函数积木」 |
| `tensorflow/core/framework/shape_inference.h` | 定义 `InferenceContext`、`ShapeHandle`、`DimensionHandle` 等类型 | 工具箱：理解形状函数能调用什么 |
| `tensorflow/core/framework/op_def_builder.h` | 定义 `OpShapeInferenceFn` 类型与 `OpRegistrationData::shape_inference_fn` | 接线点：形状函数挂在哪 |
| `tensorflow/core/framework/op.h` | `OpDefBuilderWrapper::SetShapeFn` | 接线点：`.SetShapeFn(...)` 的实现 |
| `tensorflow/core/common_runtime/shape_refiner.cc` | `ShapeRefiner::RunShapeFn`：**真正调用形状函数的地方** | 证据：形状推导在何时、被谁调用 |
| `tensorflow/core/kernels/concat_op.cc` | Concat 的 **计算实现**（`ConcatBaseOp::Compute`）与 kernel 注册 | 对照组：证明 kernel 与形状推导是两份独立代码 |
| `tensorflow/core/ops/array_ops_test.cc` | 形状函数的单元测试（`INFER_OK` / `INFER_ERROR`） | 实践依据：如何验证形状推导行为 |

---

## 4. 核心概念与源码讲解

本讲围绕四个最小模块展开：

- **4.1 `core/ops` 目录：Op 的「声明层」与形状推导入口**
- **4.2 形状推导函数与 `InferenceContext`**
- **4.3 可复用形状函数库 `common_shape_fns`**
- **4.4 ops 与 kernels 的分工（为什么形状推导独立于 kernel）**

### 4.1 core/ops 目录：Op 的「声明层」与形状推导入口

#### 4.1.1 概念说明

很多初学者打开 TF 源码想找「某个 op 的实现」，第一反应是去 `core/kernels/`。但有一个更前置的目录 `core/ops/`，它和 kernels 的分工是这样的：

- `core/ops/*.cc`：**只声明 Op**（输入、输出、属性、形状推导函数、文档），不含任何循环、不做任何数值计算。
- `core/kernels/*.cc`：**只实现 Op 的计算**（`Compute` 方法、设备特化、kernel 注册）。

`core/ops/` 按领域拆成多个文件，例如 `array_ops.cc`（数组操作）、`math_ops.cc`（数学运算）、`nn_ops.cc`（神经网络）、`image_ops.cc`、`linalg_ops.cc` 等。你可以把它们理解成「一张张登记表」。

每一张登记表里，`REGISTER_OP("名字")` 串起一条链式调用：

```
REGISTER_OP("名字")
    .Input("...")      // 声明输入张量
    .Output("...")     // 声明输出张量
    .Attr("...")       // 声明属性（如 T: type）
    .SetShapeFn(...)   // ★ 挂上形状推导函数
```

`.SetShapeFn(...)` 是本讲的主角：它把一个形状推导函数绑定到这个 Op 上。当你构造图（或 `tf.function` tracing）时，框架会用这个函数去推算每个输出张量的形状，而**完全不需要去执行 kernel**。

#### 4.1.2 核心流程

形状推导在整个 TF 流水线里的位置大致如下：

```
用户写 op 调用
   │
   ▼
建图 / tf.function tracing（core/ops 的 SetShapeFn 在此被调用）
   │   ← ShapeRefiner::RunShapeFn 执行形状推导
   │   ← 每个节点得到输出形状（可能含 ?）
   ▼
图优化（Grappler / MLIR，依赖形状做融合、常量折叠）
   │
   ▼
真正在设备上执行 kernel（core/kernels 的 Compute）
```

注意：**形状推导发生在 tracing/建图阶段**，远早于 kernel 执行。这意味着框架必须在「不跑 kernel」的前提下算出形状——这正是本讲要解释的核心设计。

#### 4.1.3 源码精读

先看一个最简单的形状推导：`Identity` op 的输出形状等于输入形状。

[array_ops.cc:1317-1322](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/ops/array_ops.cc#L1317-L1322) —— `Identity` 声明，形状函数直接复用 `shape_inference::UnchangedShape`：

```cpp
REGISTER_OP("Identity")
    .Input("input: T")
    .Output("output: T")
    .Attr("T: type")
    .SetForwardTypeFn(full_type::ReplicateInput())
    .SetShapeFn(shape_inference::UnchangedShape);
```

再看一个把「输出是标量」固化的例子：

[array_ops.cc:1687-1691](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/ops/array_ops.cc#L1687-L1691) —— `Rank` 输出一个 `int32` 标量（秩是一个数）：

```cpp
REGISTER_OP("Rank")
    .Input("input: T")
    .Output("output: int32")
    .Attr("T: type")
    .SetShapeFn(shape_inference::ScalarShape);
```

`tf.rank(x)` 的返回值是一个标量 `[]`，形状与 `x` 无关，这里直接用现成的 `ScalarShape`。

`.SetShapeFn(...)` 本身只是把函数转交给 builder：

[op.h:278-281](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op.h#L278-L281)：

```cpp
OpDefBuilderWrapper& SetShapeFn(OpShapeInferenceFn fn) {
  builder_.SetShapeFn(std::move(fn));
  return *this;
}
```

它的类型 `OpShapeInferenceFn` 定义在 `op_def_builder.h`：

[op_def_builder.h:64-65](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_def_builder.h#L64-L65)：

```cpp
typedef std::function<absl::Status(shape_inference::InferenceContext* c)>
    OpShapeInferenceFn;
```

也就是说，形状推导函数就是「接收一个 `InferenceContext*`、返回 `absl::Status`」的可调用对象。它最终被存进 `OpRegistrationData`，和 `OpDef` 一起挂在全局注册表里：

[op_def_builder.h:67-76](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_def_builder.h#L67-L76)：

```cpp
struct OpRegistrationData {
  ...
  OpDef op_def;
  OpShapeInferenceFn shape_inference_fn;   // ★ 形状函数就住在这里
  ...
};
```

#### 4.1.4 代码实践

1. **实践目标**：在 `array_ops.cc` 中任选一个 op，指出它的形状推导函数「来源」是哪一种（① 直接复用 `common_shape_fns` 库函数；② 内联 lambda；③ 调用本文件 anonymous namespace 里的局部函数）。
2. **操作步骤**：
   - 打开 `tensorflow/core/ops/array_ops.cc`。
   - 挑选 `Identity`、`Rank`、`Slice`、`ConcatV2`、`Transpose`、`Reshape`、`Shape`、`Gather` 这几个 op 中的任意一个，定位它的 `REGISTER_OP(...)` 块。
   - 看 `.SetShapeFn(...)` 括号里写的是什么。
3. **需要观察的现象**：你会发现括号里有三种写法——
   - `shape_inference::UnchangedShape` / `shape_inference::ScalarShape` / `shape_inference::SliceShape`：**来自公共库 `common_shape_fns.h`**。
   - `[](InferenceContext* c) {...}`：**内联 lambda**，逻辑只此 op 独有。
   - `TransposeShapeFn` / `PadShapeFn` / `SetOutputShapeForReshape`：**本文件 anonymous namespace 里的局部函数**，复用度介于前两者之间。
4. **预期结果**：例如 `Slice`（[array_ops.cc:1702-1709](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/ops/array_ops.cc#L1702-L1709)）的 `SetShapeFn(shape_inference::SliceShape)` 来自公共库；而 `Transpose`（[array_ops.cc:1428-1434](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/ops/array_ops.cc#L1428-L1434)）的 `SetShapeFn(TransposeShapeFn)` 来自本文件第 126 行的局部函数。

#### 4.1.5 小练习与答案

**练习 1**：`Size` op（`tf.size`）的形状函数是什么？为什么用它？
> **答案**：[array_ops.cc:1694-1699](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/ops/array_ops.cc#L1694-L1699) 中 `.SetShapeFn(shape_inference::ScalarShape)`。因为 `tf.size(x)` 返回的是元素总数——一个标量，形状恒为 `[]`，与 `x` 形状无关，所以直接复用 `ScalarShape`。

**练习 2**：`ConcatV2` 的形状函数为何写成 `.SetShapeFn(shape_inference::ConcatV2Shape)` 而不是内联 lambda？
> **答案**：拼接逻辑（沿 axis 合并维度）较复杂，且会被多个版本（`Concat`、`ConcatV2`、MKL 的 `_MklConcatV2`）复用，所以抽成公共库函数 `ConcatV2Shape`。见 [array_ops.cc:535-542](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/ops/array_ops.cc#L535-L542)。

---

### 4.2 形状推导函数与 InferenceContext

#### 4.2.1 概念说明

形状推导函数长这样：`absl::Status(InferenceContext* c)`。`InferenceContext` 是框架在调用你的形状函数前**为你组装好的工具箱**，它知道：

- 每个输入张量的**静态形状**（`c->input(idx)` 返回 `ShapeHandle`）。
- 某些输入张量的**常量值**（如果该输入是编译期常量，`c->input_tensor(idx)` 返回 `const Tensor*`，否则返回 `nullptr`）。
- Op 的**属性**（`c->GetAttr("axis", &axis)`）。
- 一套**形状构造与代数运算**方法（`MakeShape`、`Concatenate`、`Subshape`、`Merge`、`Add`、`Multiply`、`Divide`…）。

形状函数的职责只有一句：**用这些工具算出每个输出形状，然后 `c->set_output(idx, shape)`。** 如果输入不合法（如秩不匹配），返回一个错误 `absl::Status`，而不是抛异常。

`ShapeHandle`/`DimensionHandle` 是「指针式」的轻量句柄，真正的 `Shape`/`Dimension` 对象由 `InferenceContext` 拥有、随其生命周期销毁（见 [shape_inference.h:46-67](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/shape_inference.h#L46-L67) 的说明）。所以你**不能**自己 `new Shape`，只能找 `InferenceContext` 要。

#### 4.2.2 核心流程

一个典型形状推导函数的骨架：

```
1. 取输入形状：       ShapeHandle in = c->input(0);
2. 约束/校验秩：       TF_RETURN_IF_ERROR(c->WithRankAtLeast(in, 1, &in));
3. 读属性：           TF_RETURN_IF_ERROR(c->GetAttr("axis", &axis));
4. （可选）读常量输入： const Tensor* perm = c->input_tensor(1);
5. 用形状代数构造输出： Concatenate / Subshape / ReplaceDim / Add / ...
6. 写回输出：          c->set_output(0, out);
7. 返回状态：          return absl::OkStatus();
```

关键约定：

- 未知维度记为 `kUnknownDim = -1`，未知秩记为 `kUnknownRank = -1`（[shape_inference.h:236-237](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/shape_inference.h#L236-L237)）。
- 形状函数**必须能优雅地处理未知情况**：当维度未知时，尽量返回「秩已知、维度未知」的形状（如 `[?, ?]`），而不是直接返回完全未知。这正是 TF 形状推导「尽力而为、逐层收紧」的风格。

#### 4.2.3 源码精读

`InferenceContext` 的类注释直接点明了它的角色：

[shape_inference.h:224-234](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/shape_inference.h#L224-L234)：

```cpp
// Shape inference functions registered on ops in REGISTER_OP implement
// their shape functions in terms of this InferenceContext. ...
// The shape inference function calls functions on the context, and should call
// set_output() to set the shape on all outputs.
class InferenceContext {
```

最常用的几组方法（按用途归类）：

| 类别 | 代表方法（行号见 shape_inference.h） | 作用 |
| --- | --- | --- |
| 输入 | `input(idx)`（L343）、`input_tensor(idx)`（L350）、`num_inputs()`（L346） | 取输入形状 / 常量值 |
| 输出 | `set_output(idx, shape)`（L397）、`num_outputs()`（L401） | 写回输出形状 |
| 属性 | `GetAttr(name, &val)`（L407-413） | 读 OpDef 里的 attr |
| 形状查询 | `Rank(s)`（L434）、`Dim(s, idx)`（L419）、`Value(d)`（L440）、`FullyDefined(s)`（L453） | 查秩/维度/数值 |
| 秩约束 | `WithRank`（L471）、`WithRankAtLeast`（L472）、`WithRankAtMost`（L475） | 断言秩，不满足则返回错误 |
| 构造形状 | `MakeShape`（L531）、`UnknownShape`（L535）、`UnknownShapeOfRank`（L538）、`Scalar`（L541）、`Vector`（L544）、`Matrix`（L547） | 造新形状 |
| 形状代数 | `Concatenate`（L522）、`Subshape`（L505-518）、`ReplaceDim`（L526）、`Merge`（L486） | 拼接/切片/替换/合并 |
| 维度代数 | `Add`（L618）、`Subtract`（L622）、`Multiply`（L626）、`Divide`（L614）、`Min`（L632）、`Max`（L637） | 维度级四则运算 |

看一个真实、完整的内联形状函数——`Diag`（输出形状 = 输入形状自我拼接，例如 `[m] → [m,m]`、`[a,b] → [a,b,a,b]`）：

[array_ops.cc:797-811](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/ops/array_ops.cc#L797-L811)：

```cpp
REGISTER_OP("Diag")
    .Input("diagonal: T")
    .Output("output: T")
    .Attr("T: {...}")
    .SetShapeFn([](InferenceContext* c) {
      ShapeHandle in = c->input(0);
      TF_RETURN_IF_ERROR(c->WithRankAtLeast(in, 1, &in));  // 至少 1 维
      ShapeHandle out;
      TF_RETURN_IF_ERROR(c->Concatenate(in, in, &out));    // 拼接自身
      c->set_output(0, out);
      return absl::OkStatus();
    });
```

再看 `Shape` op（返回输入的秩构成的一维向量）：它把形状函数抽成本文件 anonymous namespace 的具名函数 `ShapeShapeFn`，因为它要被 `Shape` 和 `ShapeN` 两个 op 复用：

[array_ops.cc:1573-1595](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/ops/array_ops.cc#L1573-L1595)：

```cpp
absl::Status ShapeShapeFn(InferenceContext* c) {
  for (int i = 0; i < c->num_inputs(); ++i) {
    DimensionHandle dim;
    if (c->RankKnown(c->input(i))) {
      dim = c->MakeDim(c->Rank(c->input(i)));   // 维度值 = 输入的秩
    } else {
      dim = c->UnknownDim();                    // 秩未知 → 维度未知
    }
    c->set_output(i, c->Vector(dim));           // 输出是一维向量
  }
  return absl::OkStatus();
}
```

一个更精细的例子是 `Transpose`：它需要读 `perm` 这个**常量输入**的值，才能确定输出每个维度来自输入的哪个维度；若 `perm` 不是常量（`nullptr`），就只能退回到「秩已知、维度全未知」：

[array_ops.cc:126-190](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/ops/array_ops.cc#L126-L190) —— 关键片段：

```cpp
absl::Status TransposeShapeFn(InferenceContext* c) {
  ShapeHandle input = c->input(0);
  ...
  const Tensor* perm = c->input_tensor(1);        // 尝试读 perm 的常量值
  ...
  if (perm != nullptr) {
    // 已知 perm：输出第 i 维 = 输入第 perm[i] 维
    for (int32_t i = 0; i < rank; ++i) {
      int64_t in_idx = data[i];
      dims[i] = c->Dim(input, in_idx);
    }
  } else {
    // perm 不是常量：只能给出秩，维度全未知
    for (int i = 0; i < rank; ++i) dims[i] = c->UnknownDim();
  }
  c->set_output(0, c->MakeShape(dims));
  return absl::OkStatus();
}
```

这正是「尽力而为」的典型：能算多准算多准，算不准就给一个相对宽松但仍带信息的形状。

> 旁注：`c->input_tensor(idx)` 不仅是「读常量」，它同时会**标记**「我这次推导需要这个输入的值」。`ShapeRefiner` 据此会尝试常量物化并**重新运行**形状函数（见 [shape_refiner.cc:774-801](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/shape_refiner.cc#L774-L801)）。这是 TF 形状推导能逐步收紧的机制基础。

#### 4.2.4 代码实践

1. **实践目标**：读懂一个内联形状函数，并能口述「它读了什么、算了什么、写了什么」。
2. **操作步骤**：
   - 打开 `tensorflow/core/ops/array_ops.cc`，找到 `REGISTER_OP("BroadcastTo")`（[L480-L520](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/ops/array_ops.cc#L480-L520)）。
   - 对照上面那张 API 表，逐行标注每个 `c->...` 调用属于哪一类（输入/输出/属性/形状代数）。
3. **需要观察的现象**：注意它先用 `c->WithRank(shape_in, 1, &shape_in)` 断言第二个输入是一维向量，再用 `c->MakeShapeFromShapeTensor(1, &out)` 把这个一维向量「翻译」成输出形状，最后逐维 `c->Merge` 检查广播兼容性。
4. **预期结果**：你应当能用三句话复述 `BroadcastTo` 的形状推导——「读 `shape` 输入→造出目标形状→与输入形状做逐维 merge 校验」。无需运行任何代码，这是纯源码阅读实践。

#### 4.2.5 小练习与答案

**练习 1**：形状函数为什么不直接 `return` 一个形状，而要 `c->set_output(idx, shape)` 再 `return OkStatus()`？
> **答案**：因为一个 op 可能有**多个输出**，需要逐个 `set_output`；而返回值 `absl::Status` 专门用来表达「成功 or 输入不合法」。把「数据通道（set_output）」和「控制通道（return status）」分开，是多输出 op 的清晰写法。

**练习 2**：`kUnknownDim` 为什么是 `-1` 而不是 `0`？
> **答案**：维度值必须是非负整数（`0` 表示该维大小为 0，是合法的空维度），所以用 `-1` 这个「不可能的合法值」来专门标记「未知」（见 [shape_inference.h:236](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/shape_inference.h#L236) 与 `Dimension(int64_t value)` 的 `DCHECK`，[shape_inference.h:892-898](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/shape_inference.h#L892-L898)）。

---

### 4.3 可复用形状函数库 common_shape_fns

#### 4.3.1 概念说明

很多 op 的输出形状规则是相同的，例如：

- `Identity`、`ZerosLike`、`OnesLike`、`Cast`、`CheckNumerics`……都是「输出形状 = 输入形状」。
- `tf.rank`、`tf.size`……都是「输出是标量」。
- 逐元素二元运算（`Add`、`Mul`……）都是「输出形状 = 两个输入按 numpy 广播后的形状」。

如果把同样的逻辑在每个 op 里都内联一遍，会产生大量重复。TF 把这些常见规则抽成 `core/framework/common_shape_fns.h`（实现多数在 `common_shape_fns.cc`），放在 `shape_inference::` 命名空间下，供 `core/ops/*.cc` 统一复用。这就是本讲的第二个最小模块 `core.framework.common_shape_fns`。

它和 4.2 节的关系是：**`common_shape_fns` 里的函数本身也是 `OpShapeInferenceFn`，只是已经替你写好了。**

#### 4.3.2 核心流程

判断一个 op 该用哪种形状函数的策略：

```
形状规则是不是「常见套路」？
  ├─ 是 → 优先在 common_shape_fns.h 找现成函数（UnchangedShape / ScalarShape / ...）
  │       多个 op 共用，一处实现、处处复用
  ├─ 否，但和本文件里别的 op 相似 → 写进本文件 anonymous namespace 的具名函数
  │       （如 array_ops.cc 里的 TransposeShapeFn / PadShapeFn）
  └─ 否，且只此一个 op 用 → 直接写内联 lambda
```

#### 4.3.3 源码精读

`common_shape_fns.h` 里的函数大多是**单行 inline**，简洁到一目了然。最常用的几个：

[common_shape_fns.h:46-47](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/common_shape_fns.h#L46-L47) —— 把 input(0) 的形状原样搬到 output(0)：

```cpp
// Transfers shape of input(0) to output(0).
absl::Status UnchangedShape(shape_inference::InferenceContext* c);
```

[common_shape_fns.h:81-85](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/common_shape_fns.h#L81-L85) —— 输出标量：

```cpp
inline absl::Status ScalarShape(shape_inference::InferenceContext* c) {
  c->set_output(0, c->Scalar());
  return absl::OkStatus();
}
```

[common_shape_fns.h:50-56](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/common_shape_fns.h#L50-L56) —— 「先断言秩、再 Unchanged」的带校验版本，体现 `TF_RETURN_IF_ERROR` 的用法：

```cpp
inline absl::Status UnchangedShapeWithRank(shape_inference::InferenceContext* c,
                                           int32_t rank) {
  ShapeHandle out;
  TF_RETURN_IF_ERROR(c->WithRank(c->input(0), rank, &out));
  c->set_output(0, out);
  return absl::OkStatus();
}
```

[common_shape_fns.h:253-255](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/common_shape_fns.h#L253-L255) —— 二元广播形状（numpy broadcasting）：

```cpp
inline absl::Status BroadcastBinaryOpShapeFn(InferenceContext* c) {
  return BroadcastBinaryOpOutputShapeFn(c, 0);
}
```

对两个形状 \(s_x\) 和 \(s_y\)，广播规则是「从末尾起逐维对齐，每维取较大值；其中一个为 1 时向另一个看齐」。其结果形状的第 \(i\) 维：

\[
out_i = \max(x_i,\, y_i) \quad (\text{其中 } x_i=1 \text{ 或 } y_i=1 \text{ 时取另一个})
\]

库函数 `BroadcastBinaryOpOutputShapeFnHelper`（[common_shape_fns.h:234-238](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/common_shape_fns.h#L234-L238)）封装了这一逻辑，并被所有逐元素二元 op 复用。

更复杂的库函数（如 `ConcatV2Shape`、`SliceShape`、`ReductionShape`、`MatMulShape`、`Conv2DShape`）则声明在头文件、实现在 `.cc`，因为它们涉及较长的推导。`array_ops.cc` 里大量 op 正是直接调用它们：

[array_ops.cc:1702-1709](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/ops/array_ops.cc#L1702-L1709)：

```cpp
REGISTER_OP("Slice")
    ...
    .SetShapeFn(shape_inference::SliceShape);
```

[array_ops.cc:535-542](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/ops/array_ops.cc#L535-L542)：

```cpp
REGISTER_OP("ConcatV2")
    ...
    .SetShapeFn(shape_inference::ConcatV2Shape);
```

> 一句话记住 `common_shape_fns` 的价值：**它把「形状规则」从「op 声明」里解耦出来，做成可复用的积木**，于是几十个 op 可以共享同一段经过测试的形状推导代码。

#### 4.3.4 代码实践

1. **实践目标**：统计 `array_ops.cc` 中有多少 op 复用了 `common_shape_fns` 的函数。
2. **操作步骤**：在 `tensorflow/core/ops/array_ops.cc` 中检索 `.SetShapeFn(shape_inference::` 出现的次数（用编辑器搜索或 `grep`）。
3. **需要观察的现象**：你会看到 `UnchangedShape`、`ScalarShape`、`ExplicitShape`、`UnchangedShape`、`ConcatV2Shape`、`SliceShape`、`MatrixDiagV2Shape` 等被反复使用；其中 `UnchangedShape` 出现频率最高（`Identity`、`ZerosLike`、`OnesLike`、`StopGradient`、`PreventGradient`、`CheckNumerics`、`MatrixBandPart`、`FakeQuantWithMinMaxArgs`…）。
4. **预期结果**：你会确认「绝大多数形状规则的 op 都复用了公共库」，只有规则特殊的 op 才手写 lambda。这能直观体会公共库的复用价值。

#### 4.3.5 小练习与答案

**练习 1**：`tf.constant` 对应的 `Const` op 形状是怎么推出来的？为什么它没法用 `UnchangedShape`？
> **答案**：`Const` 没有输入（只有 `value` 属性），形状来自属性里那个 `TensorProto` 的 `tensor_shape`，见 [array_ops.cc:722-738](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/ops/array_ops.cc#L722-L738)：它 `GetAttr("value", &proto)` 后逐维 `c->MakeDim(shape.dim_size(i))`。因为「没有输入形状可复制」，所以不能用 `UnchangedShape`，必须手写内联 lambda。

**练习 2**：如果一个 op 忘了写 `.SetShapeFn(...)`，会发生什么？
> **答案**：注册时 `OpRegistrationData::shape_inference_fn` 为空。框架在 `ShapeRefiner::RunShapeFn` 里检测到为空就**回退到 `shape_inference::UnknownShape`**，即该 op 的所有输出形状都被当作完全未知，见 [shape_refiner.cc:759-767](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/shape_refiner.cc#L759-L767)。这正是 TF 推荐「每个 op 都应提供形状函数」的原因——否则下游优化和类型检查都会丢失信息。

---

### 4.4 ops 与 kernels 的分工（为什么形状推导独立于 kernel）

#### 4.4.1 概念说明

这是本讲要回答的核心问题（也是实践任务的设问）：**形状推导为什么要从 kernel 实现里剥离出来、单独放在 `core/ops/`？**

一句话回答：**因为形状推导必须在「不执行 kernel」的前提下完成。**

具体看，TF 有大量消费者需要在「拿到形状」之后、但「远早于 kernel 执行」之前就用到形状信息：

1. **建图 / tracing 阶段**：构造图节点时就要给每个张量打上静态形状，供类型检查和错误提示。
2. **`tf.function` tracing**：追踪 Python 函数成图时，形状是控制流展开（如 `tf.while_loop` 的 continuation 判定）的依据。
3. **Grappler 图优化**：常量折叠、布局变换、算子融合都需要形状信息；而这些 pass 根本不跑 kernel。
4. **MLIR / XLA lowering**：把 TF 图编译成设备代码时，需要形状来确定算子的输出尺寸。
5. **自动微分**：反向图的形状推导依赖前向形状。

如果形状推导绑死在 kernel 里，那意味着「想知道形状就得先把 kernel 跑一遍」——而 kernel 需要真实数据、需要绑定具体设备（CPU/GPU/TPU）、需要真实输入（包括 placeholder 这种运行时才有的值）。这在静态分析阶段既不可能、也不可接受。

因此 TF 的设计是：

- **`core/ops/`**：声明 + 形状推导，**设备无关、类型无关**（形状通常与 dtype、设备无关），在 tracing 期就能跑。
- **`core/kernels/`**：每个设备/类型的计算实现，**设备相关、类型相关**，运行期才跑。

同一个 op 名字（如 `"Concat"`）在两个目录里各出现一次：ops 文件声明它的签名与形状函数，kernels 文件提供它的计算。

#### 4.4.2 核心流程

```
REGISTER_OP("ConcatV2")                      ← core/ops/array_ops.cc：声明 + SetShapeFn(ConcatV2Shape)
        │ (op 名字 "ConcatV2" 进入全局 OpRegistry)
        │
        ▼
ShapeRefiner::RunShapeFn                     ← core/common_runtime/shape_refiner.cc：tracing 期调用 shape_inference_fn
        │ （不碰任何 kernel）
        ▼
REGISTER_KERNEL_BUILDER(Name("ConcatV2")...) ← core/kernels/concat_op.cc：运行期才实例化、执行 Compute
```

关键点：**ops 文件与 kernels 文件通过「op 名字」这一字符串松耦合**，二者在源码上完全独立，甚至在不同 BUILD 目标里。形状推导从不调用 kernel，kernel 也不调用形状推导。

#### 4.4.3 源码精读

先看 ops 侧——`ConcatV2` 的声明与形状函数（注意这里**没有任何计算代码**）：

[array_ops.cc:535-542](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/ops/array_ops.cc#L535-L542)：

```cpp
REGISTER_OP("ConcatV2")
    .Input("values: N * T")
    .Input("axis: Tidx")
    .Output("output: T")
    .Attr("N: int >= 2")
    .Attr("T: type")
    .Attr("Tidx: {int32, int64} = DT_INT32")
    .SetShapeFn(shape_inference::ConcatV2Shape);
```

再看 kernels 侧——同一个 `"Concat"` 名字的**计算实现**，它继承 `OpKernel`、实现 `Compute`：

[concat_op.cc:48-66](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/kernels/concat_op.cc#L48-L66)：

```cpp
template <typename Device, typename T, AxisArgumentName AxisArgName>
class ConcatBaseOp : public OpKernel {
  ...
  void Compute(OpKernelContext* c) override {
    const Tensor& concat_dim_tensor = c->input(axis_input_index_);
    ...
  }
```

[concat_op.cc:193-194](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/kernels/concat_op.cc#L193-L194)：

```cpp
#define REGISTER_CONCAT(type)                            \
  REGISTER_KERNEL_BUILDER(Name("Concat")                 \
```

注意这里的对照：

| 维度 | `core/ops/array_ops.cc`（声明 + 形状） | `core/kernels/concat_op.cc`（计算） |
| --- | --- | --- |
| 何时运行 | 建图 / tracing 期 | 真正执行期 |
| 依赖设备 | 否（形状与设备无关） | 是（按 `Device` 模板特化 CPU/GPU） |
| 依赖 dtype | 多数否（形状与 dtype 无关） | 是（按 `T` 模板特化） |
| 输入数据 | 只用静态形状 + 少量常量值 | 需要真实张量数据 |
| 关键 API | `InferenceContext`、`set_output` | `OpKernelContext`、`Compute` |

最后，形状函数究竟在何处被调用？答案在 `ShapeRefiner::RunShapeFn`——注意它调用的是 `op_reg_data->shape_inference_fn`（即 ops 侧注册的函数），与 kernel 毫无关系：

[shape_refiner.cc:759-767](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/shape_refiner.cc#L759-L767)：

```cpp
if (op_reg_data->shape_inference_fn) {
  VLOG(4) << "Running shape inference function for node \""
          << node->name() << "\" ...";
  TF_RETURN_IF_ERROR(c->Run(op_reg_data->shape_inference_fn));
} else {
  // 没注册形状函数 → 回退到完全未知
  TF_RETURN_IF_ERROR(c->Run(shape_inference::UnknownShape));
}
```

这一段是「形状推导独立于 kernel」最硬的证据：**框架运行形状推导时，拿的是注册表里的函数指针，根本不创建、不调用任何 kernel。**

#### 4.4.4 代码实践（对应本讲核心实践任务）

1. **实践目标**：选一个 op，分别找到它的「声明 + 形状函数」和「kernel 实现」，并用一句话解释「为什么形状推导必须独立于 kernel」。
2. **操作步骤**：
   - 选 `Concat`（或 `ConcatV2`）。
   - 在 `tensorflow/core/ops/array_ops.cc` 第 525 行附近找到它的 `REGISTER_OP("Concat")`（[L525-L533](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/ops/array_ops.cc#L525-L533)），确认它的形状函数来自 `shape_inference::ConcatShape`。
   - 在 `tensorflow/core/kernels/concat_op.cc` 第 48 行找到 `ConcatBaseOp::Compute`（[L48-L66](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/kernels/concat_op.cc#L48-L66)），确认这才是真正的数值拼接。
   - 比较两份代码的依赖：ops 侧只 `#include "common_shape_fns.h"`；kernels 侧 `#include` 了大量 Eigen/device 头文件。
3. **需要观察的现象**：两份代码物理上完全分离，唯一的联系是字符串 `"Concat"`。
4. **预期结果**：你能写下这样的解释——
   > 「形状推导在 tracing/建图期就要为每个张量算出静态形状，供类型检查、Grappler 优化、MLIR/XLA 编译、自动微分使用；这些阶段都**不执行 kernel**，而 kernel 又依赖具体设备、dtype 和真实输入数据。因此形状推导只能放在设备无关、类型无关、可在静态期运行的 `core/ops/` 里，由 `InferenceContext` 驱动；计算实现则留在 `core/kernels/`。二者通过 op 名字松耦合，互不调用。」
5. **若想进一步验证**（可选，待本地验证）：在 `array_ops_test.cc` 中有大量形状函数测试，例如 `INFER_OK(op, "[4,3];[8,2];[8]", "in0")`（[array_ops_test.cc:36](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/ops/array_ops_test.cc#L36)）。它在不编译任何 kernel 的前提下，仅用 `ShapeInferenceTestOp("TensorScatterUpdate")` 构造一个上下文、调用形状函数、断言输出形状——这恰好证明形状推导可脱离 kernel 独立运行。

#### 4.4.5 小练习与答案

**练习 1**：如果某个 op 同时有 CPU kernel 和 GPU kernel，它的形状函数要写几份？
> **答案**：**一份就够**。形状推导与设备无关，所以无论 CPU 还是 GPU，输出形状都一样。这正体现了「形状推导独立于 kernel」的红利——一份形状函数服务所有设备/类型的 kernel。

**练习 2**：为什么 `ShapeRefiner` 在形状函数为空时要回退到 `UnknownShape`，而不是报错？
> **答案**：为了前向兼容。历史上有一些 op（尤其是 deprecated 的，如 [array_ops.cc:3426-3431](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/ops/array_ops.cc#L3426-L3431) 的 `BatchMatrixDiag` 显式用 `UnknownShape`）没提供精确形状函数。回退到未知能让图继续构造下去，代价是下游优化丢失信息，而非让整个建图失败。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个「op 形状推导三问」小任务。挑选 `Reshape` 这个 op（它最能体现形状推导的「尽力而为」），回答下面三问：

1. **它的声明在哪、形状函数来自哪里？**
   - 定位 `tensorflow/core/ops/array_ops.cc` 中 `REGISTER_OP("Reshape")`（[L1405-L1413](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/ops/array_ops.cc#L1405-L1413)）。你会看到它的 `SetShapeFn` 调用了本文件 anonymous namespace 里的 `SetOutputShapeForReshape`（[L192-L285](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/ops/array_ops.cc#L192-L285)）。

2. **它的形状函数如何处理「形状输入里有一个 -1」这种特殊情形？**
   - 读 `SetOutputShapeForReshape`：当目标形状里恰好有一个未知维度（`out_unknown_idx >= 0`）且输入元素总数已知时，它用 `c->Divide(已知元素总数, 已知输出元素数, /*evenly_divisible=*/true, &inferred_dim)` 反推这一维（[L252-L260](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/ops/array_ops.cc#L252-L260)）。若不能整除，`Divide` 会返回错误。这就是 `tf.reshape(x, [-1, 3])` 能在 tracing 期推出形状的原理。

3. **对照它的 kernel，说明「形状推导独立于 kernel」在这里带来什么好处？**
   - `Reshape` 的 kernel（在 `core/kernels/`）只是搬运数据、不做形状计算；而形状校验（如「能否整除」「元素总数是否匹配」）全部在 tracing 期由形状函数完成。于是用户写错形状时，**在建图/tracing 阶段就会立刻报错**，而不必等到运行 kernel 时才崩溃——这就是把形状推导从 kernel 剥离出来的直接收益。

> 交付物：用一段话总结这三问，并把 `Reshape` 的形状函数调用链画成 `REGISTER_OP → SetShapeFn → SetOutputShapeForReshape → MakeShapeFromShapeTensor / Divide` 的小流程图。

---

## 6. 本讲小结

- `core/ops/` 目录是 Op 的**声明层**：用 `REGISTER_OP` 串起 `Input/Output/Attr/SetShapeFn`，**不含任何计算代码**；按领域拆成 `array_ops.cc`、`math_ops.cc` 等多个「登记表」。
- `.SetShapeFn(fn)` 把一个类型为 `OpShapeInferenceFn`（即 `absl::Status(InferenceContext*)`）的函数存进 `OpRegistrationData::shape_inference_fn`，挂到全局 `OpRegistry`。
- 形状函数通过 `InferenceContext` 这个工具箱读取输入形状、常量值与属性，用一套**形状代数**（`Concatenate/Subshape/ReplaceDim/Merge` 与维度级 `Add/Multiply/Divide/...`）算出输出形状并 `set_output`；遇到未知要「尽力而为」地返回带秩的部分已知形状。
- `common_shape_fns.h` 是**可复用形状函数库**（`UnchangedShape`、`ScalarShape`、`BroadcastBinaryOpShapeFn`、`ConcatShape`…），把常见形状规则做成积木，让几十个 op 共享同一段经过测试的代码。
- 形状推导由 `ShapeRefiner::RunShapeFn` 在**建图/tracing 期**调用，**完全不碰 kernel**；与 kernel 计算实现（`core/kernels/`）通过 op 名字松耦合。
- **形状推导独立于 kernel** 的根本原因：tracing/优化/编译/微分都需要形状，且都发生在 kernel 执行之前；而 kernel 依赖设备、dtype 与真实数据，无法在静态期运行。

---

## 7. 下一步学习建议

- **横向阅读其他 ops 文件**：用本讲的方法读 `tensorflow/core/ops/math_ops.cc`、`tensorflow/core/ops/nn_ops.cc`，体会不同领域复用 `common_shape_fns` 的模式。
- **深入形状推导的执行引擎**：阅读 `tensorflow/core/common_runtime/shape_refiner.cc` 的 `RunShapeFn` 与常量物化重跑机制，理解形状如何被逐步收紧；这是 u6-l3（Grappler 图优化器）和 u7-l1（MLIR TF dialect）的前置知识。
- **看形状函数怎么被测试**：阅读 `tensorflow/core/ops/array_ops_test.cc` 和 `tensorflow/core/framework/shape_inference_testutil.h`，学会用 `INFER_OK` / `INFER_ERROR` 这套断言。
- **下一讲 u4-l4（C API 与 pywrap）**：将从「Python 怎么最终触达这些 C++ op」的角度，把 u4-l1～u4-l3 的声明/kernel/形状三件套与 Python 侧连接起来，补齐 op 机制的最后一环。
