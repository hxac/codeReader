# 推理上下文：InferShape / InferDataType / InferShapeRange

## 1. 本讲目标

图编译阶段，框架拿到一个算子节点后，第一件事不是执行它，而是「推导」它：输出的形状是什么？输出的数据类型是什么？如果输入 shape 里有未知维度（-1），输出的 shape 可能落在什么范围？这三件事分别由三个推理函数完成，而它们与框架之间的唯一数据通道，就是本讲的三个上下文类：

- `gert::InferShapeContext` —— shape 推导
- `gert::InferDataTypeContext` —— 数据类型推导
- `gert::InferShapeRangeContext` —— shape 范围推导（配合 `gert::Range`）

学完本讲，你应该能够：

1. 说清三种推理上下文各自的职责边界，以及为什么拆成三个而不是一个。
2. 理解 Shape / ShapeRange 推导接口的「指针返回 + 空值失败」设计。
3. 独立编写一个 element-wise 算子的 InferShape 函数骨架。

## 2. 前置知识

本讲直接建立在前两讲（u3-l1、u3-l2）的结论之上，先快速回顾：

- **上下文是一块裸内存的类型化视图**。`InferShapeContext` 的继承链是 `KernelRunContext → KernelContext → ExtendedKernelContext → InferShapeContext`，整条链**零新增数据成员**，派生类只是给同一块内存换一个「解读方式」。每个头文件末尾的 `static_assert(std::is_standard_layout<...>)` 就是把这一布局契约固化成编译期检查。
- **槽位序列**。`KernelRunContext` 的 `values` 柔性数组里，输入槽在前、输出槽紧随其后。`KernelContext::GetInputPointer<T>(i)` 的含义是：取出第 `i` 个输入槽（一个 `Chain`），再把 `Chain` 里的数据按 `T*` 解释。
- **IR 索引 vs 物理槽位**。算子 IR 原型里的输入（可能未实例化、可能实例化多个）与节点上实际的物理输入槽不是一回事，`ExtendedKernelContext::GetDynamicInputPointer` 通过 `AnchorInstanceInfo` 完成两者翻译。
- **失败语义**：框架侧接口不抛异常，取不到就返回空指针（或 `DT_UNDEFINED`、`GRAPH_FAILED`）。

还需要两个第 u2-l4 讲的老朋友：

- `gert::Shape`：定长 POD（最多 25 维），`GetDimNum`/`GetDim`/`SetDim`/`AppendDim`/`SetDimNum` 是基本操作。
- `gert::Tensor`：执行期张量，含 `StorageShape`、`StorageFormat`、`DataType`、`TensorData`。

最后解释一个本讲的新名词——**ShapeRange（shape 范围）**：当用户用动态 shape（如 `(-1, 128)`）建图时，某个维度的具体值编译期未知，只能给出「最小可能是 1，最大可能是 1024」这样的区间。`Range<T>` 就是承载「最小值指针 + 最大值指针」的容器，`Range<Shape>` 表示一个 shape 的上下界。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [inc/external/exe_graph/runtime/infer_shape_context.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/infer_shape_context.h) | `InferShapeContext` 全部接口，本讲主角 |
| [inc/external/exe_graph/runtime/infer_datatype_context.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/infer_datatype_context.h) | `InferDataTypeContext` 全部接口 |
| [inc/external/exe_graph/runtime/infer_shape_range_context.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/infer_shape_range_context.h) | `InferShapeRangeContext` 全部接口 |
| [inc/external/exe_graph/runtime/range.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/range.h) | `Range<T>` 模板：min/max 双指针容器 |
| inc/external/exe_graph/runtime/extended_kernel_context.h | 基类，提供 `GetDynamicInputPointer` 等翻译原语（u3-l2 已精读） |
| inc/external/exe_graph/runtime/kernel_context.h | 更底层基类，提供 `GetInputPointer<T>`/`GetOutputPointer<T>`（u3-l1 已精读） |
| inc/external/register/op_impl_registry.h | 推理函数的签名定义与注册入口 |
| tests/ut/base/testcase/context_builder_unittest.cc | 用 `OpInferShapeContextBuilder` 构造 `InferShapeContext` 的真实单测 |

这三个上下文头文件的共同特点：**全部是头文件内联实现，没有对应的 .cc 文件**——因为它们只是对基类访问原语的薄封装，不引入任何新状态。

## 4. 核心概念与源码讲解

### 4.1 InferShapeContext：shape 推导上下文

#### 4.1.1 概念说明

框架在图编译或运行时推导 shape 时，会为算子准备好一块 `KernelRunContext` 内存：每个输入占一个槽（槽里是指向输入 `Tensor` 或 `Shape` 的 `Chain`），每个输出占一个槽（槽里是**预先分配好但内容待填**的 `Shape`）。然后把这块内存包装成 `InferShapeContext *` 传给算子注册的 InferShape 函数，由算子负责把输出槽里的 `Shape` 填好。

所以 `InferShapeContext` 的接口天然分成两组：

- **读**：`GetInputShape` / `GetInputTensor` 系列——读输入槽（const）。
- **写**：`GetOutputShape`——拿输出槽的可写指针，往里填推导结果。

注意它是三个推理上下文中唯一**没有 `Set` 字样写接口**的一个：写 shape 不是调用 `SetOutputShape(index, shape)`，而是拿到 `Shape *` 后直接操作 `Shape` 自身的方法（`SetDimNum`、`AppendDim`、`SetDim`）。原因很简单——`gert::Shape` 本身就是 POD 值对象，输出槽已经在框架内存里，直接改即可，无需搬运。

#### 4.1.2 核心流程

一次 InferShape 调用的完整流程：

```text
框架侧（Builder/执行器）                        算子侧（InferShape 函数）
─────────────────────────                      ─────────────────────────
1. 为每个输入挂 Tensor/Shape 槽
2. 为每个输出预分配空 Shape 槽
3. 组装 KernelRunContext 内存
4. reinterpret_cast 成 InferShapeContext*
        │
        └──────►  5. 读 GetInputShape(0) 等接口
                  6. 按算子语义计算输出 shape
                  7. GetOutputShape(0)->SetDimNum(n)
                     + AppendDim/SetDim 逐维填值
                  8. 返回 0（成功）或非 0（失败）
9. 框架检查返回值，读取输出槽中的 Shape
```

按 IR 索引访问的翻译规则（承接 u3-l2）：`GetRequiredInputShape(ir_index)` 和 `GetOptionalInputShape(ir_index)` 都是 `GetDynamicInputPointer<Shape>(ir_index, 0)` 的语法糖，即取该 IR 输入第 0 个实例；`GetDynamicInputShape(ir_index, relative_index)` 才能取到第 N 个实例。

#### 4.1.3 源码精读

类的定义与访问模板的原语复用：

[infer_shape_context.h:30-39](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/infer_shape_context.h#L30-L39) —— `InferShapeContext` 公有继承 `ExtendedKernelContext`，`GetInputShape` 只有一行：把基类的 `GetInputPointer<Shape>(index)` 转发出来。这就是「零新增数据成员、纯类型化视图」的落点。

[infer_shape_context.h:46-58](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/infer_shape_context.h#L46-L58) —— 输入侧还可以按 `Tensor` 视角读取。注释里有一个重要信息：只有算子被配置为 `'data'` 数据依赖时，返回的 `Tensor` 里才存有 Host 内存地址，否则地址为空——也就是说 InferShape 阶段**通常只能依赖 shape/format/dtype 等元信息，看不到真实数据**。

[infer_shape_context.h:76-90](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/infer_shape_context.h#L76-L90) —— `DYNAMIC_INPUT` 的读取需要两个下标：`ir_index`（IR 原型中的位置）+ `relative_index`（实例化后的相对序号）。注释举例：某 DYNAMIC_INPUT 实例化了 3 个输入，则 `relative_index` 有效范围是 [0, 2]。

[infer_shape_context.h:98-108](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/infer_shape_context.h#L98-L108) —— `GetRequiredInputTensor` / `GetRequiredInputShape` 的实现都是 `GetDynamicInputPointer<T>(ir_index, 0)`，与 OPTIONAL 版本**代码完全相同**——REQUIRED/OPTIONAL 的差异不在访问代码，而在上层语义：OPTIONAL 未实例化时返回空指针是「正常情况」，REQUIRED 返回空指针则意味着框架组装出错。

[infer_shape_context.h:115-119](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/infer_shape_context.h#L115-L119) —— 唯一的「写」接口 `GetOutputShape`：注意它**不是 const 成员函数**（其余读接口都是），因为要返回可写的 `Shape *`；末尾的 `static_assert` 再次固化标准布局约束。

底层原语在基类中的实现（复习 u3-l1/u3-l2，行号供查证）：

- [kernel_context.h:217-224](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_context.h#L217-L224) —— `GetInputPointer<T>`：先按 `i` 取输入 `Chain`，越界返回空，再 `GetPointer<T>()` 取数据指针。
- [kernel_context.h:273-280](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_context.h#L273-L280) —— `GetOutputPointer<T>`：同一个 `Chain` 的可写版本。
- [extended_kernel_context.h:200-210](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/extended_kernel_context.h#L200-L210) —— `GetDynamicInputPointer`：IR 索引 → `AnchorInstanceInfo` → `GetInstanceStart() + relative_ins_index` 物理槽位。

注册入口——推理函数长什么样：

[op_impl_registry.h:62-65](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/op_impl_registry.h#L62-L65) —— 三类推理函数的签名统一是「裸函数指针、入参一个上下文指针、返回 `UINT32`（0 为成功）」：

```cpp
using InferShapeKernelFunc = UINT32 (*)(InferShapeContext *);
using InferShapeRangeKernelFunc = UINT32 (*)(InferShapeRangeContext *);
using InferDataTypeKernelFunc = UINT32 (*)(InferDataTypeContext *);
```

[op_impl_registry.h:87-89](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/op_impl_registry.h#L87-L89) —— 注册时通过 `OpImplRegisterV2::InferShape(...)` 等链式方法挂接。这套注册体系将在 u4-l4 专题展开。

真实用法可参照单测：[context_builder_unittest.cc:223-227](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/context_builder_unittest.cc#L223-L227) —— 用 Builder 构造出 `InferShapeContext` 后，`ctx->GetInputShape(2)` 读回的 Shape 与写入的 `StorageShape` 的 origin shape 相等；而 `ctx->GetOutputShape(0)->GetDimNum()` 为 0，正是「输出槽已分配、内容待算子填写」的直接证据。

#### 4.1.4 代码实践

**实践目标**：为一个假想的 element-wise 算子 `MyAdd`（两个输入同形，输出与输入同形）写出完整的 InferShape 函数，并掌握输出槽的填充套路。

**操作步骤**（源码阅读 + 编写型实践，可在本仓 `tests/ut/base/testcase/` 下新建一个演示文件，或仅在本机用任意编辑器练习）：

1. 阅读 [infer_shape_context.h:37-117](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/infer_shape_context.h#L37-L117)，列出读接口与写接口。
2. 打开 [shape.h:141-209](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/shape.h#L141-L209)，确认 `GetDimNum` / `GetDim` / `SetDimNum` / `SetDim` / `AppendDim` 的签名。
3. 编写如下函数（**示例代码**，非仓库已有代码）：

```cpp
#include "exe_graph/runtime/infer_shape_context.h"

// MyAdd: element-wise 加法，输出 shape 与输入 0 相同
ge::graphStatus MyAddInferShape(gert::InferShapeContext *context) {
  // 1. 读输入 0 的 shape，空指针即失败
  const gert::Shape *x1 = context->GetInputShape(0);
  if (x1 == nullptr) {
    return ge::GRAPH_FAILED;
  }
  // 2. （可选）校验输入 1 同形
  const gert::Shape *x2 = context->GetInputShape(1);
  if (x2 == nullptr || x2->GetDimNum() != x1->GetDimNum()) {
    return ge::GRAPH_FAILED;
  }
  // 3. 取输出 0 的可写 shape，逐维搬运
  gert::Shape *y = context->GetOutputShape(0);
  if (y == nullptr) {
    return ge::GRAPH_FAILED;
  }
  y->SetDimNum(x1->GetDimNum());
  for (size_t i = 0; i < x1->GetDimNum(); ++i) {
    y->SetDim(i, x1->GetDim(i));
  }
  return ge::GRAPH_SUCCESS;
}
```

4. 对照 [context_builder_unittest.cc:185-231](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/context_builder_unittest.cc#L185-L231) 的 `CreateInferShapeContextOK`，思考：如果把它改成对 `MyAddInferShape` 的单测，`InputTensors` 应该传几个张量、`IONum` 应该怎么设。

**需要观察的现象 / 预期结果**：若把上述函数接到 Builder 构造的上下文上（Builder 用法在 u5-l1 精讲），`GetOutputShape(0)` 读出的各维应与输入 0 完全一致；把 `IONum(2, 1)` 改成 `IONum(1, 1)` 后，`GetInputShape(1)` 应返回空指针、函数返回 `GRAPH_FAILED`。本地未搭建编译环境时，本实践为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`InferShapeContext` 为什么没有 `SetOutputShape` 这样的显式写接口，而 `InferDataTypeContext` 却有 `SetOutputDataType`？

**答案**：shape 槽里放的是 `gert::Shape` 这个 POD 值对象，框架已预分配好，算子拿到 `Shape *` 后用 `Shape` 自带方法原地修改即可；而 DataType 槽里放的是一个 `ge::DataType` 枚举值，上下文把它包装成「取值 + 赋值」两个函数（`GetOutputDataType`/`SetOutputDataType`）更直观。两者本质都是「改写输出槽」，只是封装粒度不同。

**练习 2**：`GetRequiredInputShape(0)` 和 `GetInputShape(0)` 有什么区别？

**答案**：`GetInputShape(index)` 的入参是**物理槽位**下标，直接按节点上实际实例化的输入顺序取；`GetRequiredInputShape(ir_index)` 的入参是 **IR 原型**中的输入序号，内部经 `GetDynamicInputPointer(ir_index, 0)` 先查 `AnchorInstanceInfo` 翻译成物理槽位再取第 0 个实例。当算子原型里有 OPTIONAL_INPUT 未实例化时，两种下标会错位，此时必须用 IR 索引版本的接口。

**练习 3**：`GetInputTensor` 的注释说「若算子被配置为 'data' 数据依赖，Tensor 中保存了 Host 内存地址，反之为 nullptr」，这提示 InferShape 阶段的设计原则是什么？

**答案**：InferShape 属于元信息推导阶段，默认**只依赖 shape/format/dtype 等编译期可知的描述信息，不读真实张量数据**，因此绝大多数算子的 Tensor 地址为空；只有显式声明了数据依赖的算子（如某些 shape 依赖数据的算子）才会拿到 Host 数据地址。这让推导阶段可以快速、且不依赖设备内存。

### 4.2 InferDataTypeContext：数据类型推导上下文

#### 4.2.1 概念说明

很多算子的输出数据类型由输入决定（如 element-wise 算子输出 dtype = 输入 dtype），框架无法替算子拍板，于是把「输出 dtype 槽」交给算子填——这就是 `InferDataTypeContext` 的全部职责。它与 `InferShapeContext` 的关键差异在于**返回值风格**：

- shape 接口返回 `const Shape *`，失败给空指针；
- dtype 是 4 字节枚举（`ge::DataType`），返回枚举值本身，失败给哨兵值 `ge::DT_UNDEFINED`。

这体现了 metadef 上下文设计的通用取舍：**小对象直接传值 + 哨兵值报错，大对象传指针 + 空指针报错**。

#### 4.2.2 核心流程

```text
GetInputDataType(index):
  槽位 i 的 Chain → GetPointer<ge::DataType>() → 空则 DT_UNDEFINED → 否则解引用返回

SetOutputDataType(index, datatype):
  输出槽 i 的 Chain → 拿 ge::DataType* → 空则 GRAPH_FAILED → 否则 *ptr = datatype
```

#### 4.2.3 源码精读

[infer_datatype_context.h:30-36](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/infer_datatype_context.h#L30-L36) —— `GetInputDataType`：取指针、判空、解引用三步走，`DT_UNDEFINED` 是失败哨兵。

[infer_datatype_context.h:43-76](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/infer_datatype_context.h#L43-L76) —— OPTIONAL / DYNAMIC / REQUIRED 三种 IR 输入的读取，套路与 shape 版本逐字对应，只是 `GetDynamicInputPointer` 的模板参数换成 `ge::DataType`。注意 `ge::DataType` 是 4 字节枚举，在 `Chain` 里走「sizeof ≤ 8 → 内联存储」的分支（见 [kernel_context.h:24-27](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_context.h#L24-L27)）。

[infer_datatype_context.h:97-104](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/infer_datatype_context.h#L97-L104) —— 唯一的写接口 `SetOutputDataType`：成功返回 `GRAPH_SUCCESS`，输出槽非法返回 `GRAPH_FAILED`。这是本讲三个上下文中**唯一显式返回 `graphStatus` 的接口**。

另有一个易被忽略的便利设计：[op_impl_registry.h:90-91](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/op_impl_registry.h#L90-L91) 的注释提到，注册体系还内置了「一种 datatype 推导规则，将第一个输入的 datatype 作为所有输出的 datatype」的通用规则——最常见的场景框架已代劳，只有特殊算子才需要自己写 `InferDataType` 函数。

#### 4.2.4 代码实践

**实践目标**：写出 `MyAdd` 的 InferDataType 函数，体会「值 + 哨兵」与「指针 + 判空」两种风格的差异。

**操作步骤**：仿照 4.1.4，编写（**示例代码**）：

```cpp
ge::graphStatus MyAddInferDataType(gert::InferDataTypeContext *context) {
  const ge::DataType dtype = context->GetInputDataType(0);
  if (dtype == ge::DT_UNDEFINED) {  // 哨兵值即失败
    return ge::GRAPH_FAILED;
  }
  return context->SetOutputDataType(0, dtype);
}
```

**需要观察的现象 / 预期结果**：输入 0 为 `DT_FLOAT` 时输出 0 被置为 `DT_FLOAT`；把输入槽数减为 0 后，`GetInputDataType(0)` 返回 `DT_UNDEFINED`、函数整体返回 `GRAPH_FAILED`。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `GetInputShape` 返回指针而 `GetInputDataType` 返回值？

**答案**：`Shape` 是数百字节的大对象且需要多步操作（读维数、读某维、写某维），传指针避免拷贝并支持原地写；`ge::DataType` 是 4 字节枚举，拷贝代价可忽略，直接返回值并把失败编码进 `DT_UNDEFINED` 哨兵，调用方代码更短。

**练习 2**：`SetOutputDataType` 内部拿到输出槽指针后写 `*output_dtype = datatype`，这一步为什么不会破坏 ABI 或内存安全？

**答案**：输出槽内存由框架侧 Builder 预先分配（属于 `KernelRunContext` 的 values 区域），`ge::DataType` 是 C 枚举、大小固定，写操作只是覆盖同一类型对象的值，不改变任何结构布局；真正越界的情况已被「槽位下标 ≥ output_size 时 `GetOutput` 返回空 → 判空返回 `GRAPH_FAILED`」挡住。

### 4.3 Range 与 InferShapeRangeContext：shape 范围推导上下文

#### 4.3.1 概念说明

动态 shape 场景下（如 batch 维未知），编译期无法给出确定的输出 shape，只能给出范围：每个维度「最小 x、最大 y」。`InferShapeRangeContext` 负责推导这个范围。它的输入输出槽里放的不是 `Shape`，而是 `Range<Shape>`（shape 范围）或 `TensorRange = Range<Tensor>`（张量范围，数据依赖算子用）。

`Range<T>` 的设计很值得玩味：它**不拥有**数据，只持有「最小值指针 + 最大值指针」两个指针。也就是说，min 和 max 两个 `T` 对象存在别处（框架分配的槽内存里），`Range` 只是把它们绑成一对。好处是 `Range` 自身是固定 56 字节的浅 POD，拷贝、跨 ABI 传递都是平凡操作；代价是使用时必须保证指针非空、生命周期由外部管理。

#### 4.3.2 核心流程

```text
框架侧:
  为输入 i 分配 min_shape / max_shape 两个 Shape 对象
  在槽里放入 Range<Shape>{&min_shape, &max_shape}
  为输出 i 预分配空的 min/max Shape 对 + Range 槽
算子侧:
  GetInputShapeRange(i) → const Range<Shape>*
      → ->GetMin() / ->GetMax() 读上下界
  GetOutputShapeRange(i) → Range<Shape>*（可写）
      → ->GetMin()->SetDim(0, lo); ->GetMax()->SetDim(0, hi) 逐维填范围
```

若上下界相同，可用单指针构造 `Range(T *same_ele)`，min/max 指向同一对象。

#### 4.3.3 源码精读

[range.h:20-45](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/range.h#L20-L45) —— `Range<T>` 模板：默认构造为双空指针；`Range(T *min, T *max)` 构造区间；`explicit Range(T *same_ele)` 表示「最小最大相同」，两个指针指向同一对象——这是一个隐含约定：写 min 会同时改 max，用它时务必清楚这一别名效应。每个构造函数都把 `reserved_` 清零（memset 的 misra 告警屏蔽注释是仓库编码规范痕迹）。

[range.h:53-59](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/range.h#L53-L59) —— `operator==` 先比指针（快速路径，还顺带处理了单指针构造的别名情况），指针不等再解引用比对象值——注意这里**没有判空**，对默认构造的空 Range 调用 `==` 会解引用空指针，这是调用方必须自查的边界。

[range.h:65-107](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/range.h#L65-L107) —— `SetMin`/`SetMax`/`GetMin`/`GetMax`：const 与非 const 两套 Get，非 const 版本返回可写指针，是算子填输出范围的入口。

[range.h:110-112](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/range.h#L110-L112) —— 成员布局：两个指针 + 40 字节 `reserved_` 保留字段。注释「32+8, do not directly use when only 8-byte left」说明保留区有既定的使用纪律，不能随意挤占——固定总大小（56 字节）本身也是 ABI 契约的一部分。

[infer_shape_range_context.h:19-32](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/infer_shape_range_context.h#L19-L32) —— `TensorRange` 别名与 `GetInputShapeRange`：与 `InferShapeContext::GetInputShape` 逐字同构，只是模板参数从 `Shape` 换成 `Range<Shape>`。`sizeof(Range<Shape>)` 为 56 字节 > 8，因此在 `Chain` 中走「指针存储」分支——这也解释了为何这些接口统一返回 `const Range<Shape> *`（指向槽内数据），而不是 `Range<Shape>` 值。

[infer_shape_range_context.h:40-52](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/infer_shape_range_context.h#L40-L52) —— `TensorRange` 读取接口：与 InferShapeContext 的 `GetInputTensor` 一样带「data 依赖才有效」的注释，同样支持 OPTIONAL/DYNAMIC/REQUIRED 三类 IR 访问。

[infer_shape_range_context.h:108-112](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/infer_shape_range_context.h#L108-L112) —— `GetOutputShapeRange` 是唯一非 const 接口；末尾 `static_assert` 的报错文案写的是 `"The class InferShapeContext must be a POD"`——从 `InferShapeRangeContext` 复制粘贴来的笔误，可作为一个「读源码时也要带着怀疑精神」的小趣闻。

#### 4.3.4 代码实践

**实践目标**：写出 `MyAdd` 的 InferShapeRange 函数，掌握「通过 Range 的 min/max 指针填输出」的双写套路。

**操作步骤**：编写（**示例代码**）：

```cpp
ge::graphStatus MyAddInferShapeRange(gert::InferShapeRangeContext *context) {
  const gert::Range<gert::Shape> *in_range = context->GetInputShapeRange(0);
  if (in_range == nullptr || in_range->GetMin() == nullptr || in_range->GetMax() == nullptr) {
    return ge::GRAPH_FAILED;  // Range 自不判空，边界要调用方自己防
  }
  gert::Range<gert::Shape> *out_range = context->GetOutputShapeRange(0);
  if (out_range == nullptr || out_range->GetMin() == nullptr || out_range->GetMax() == nullptr) {
    return ge::GRAPH_FAILED;
  }
  // element-wise：输出范围 = 输入 0 的范围，min、max 各抄一份
  const auto copyShape = [](const gert::Shape &src, gert::Shape *dst) {
    dst->SetDimNum(src.GetDimNum());
    for (size_t i = 0; i < src.GetDimNum(); ++i) {
      dst->SetDim(i, src.GetDim(i));
    }
  };
  copyShape(*in_range->GetMin(), out_range->GetMin());
  copyShape(*in_range->GetMax(), out_range->GetMax());
  return ge::GRAPH_SUCCESS;
}
```

**需要观察的现象 / 预期结果**：输入范围 `{min=(1,128), max=(32,128)}` 时，输出 0 的范围应逐维相等；若输入 Range 由 `Range(T *same_ele)` 单指针构造（min、max 同址），输出 min、max 也会一致。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`Range<T>` 为什么持有指针而不是直接存两份 `T`？

**答案**：一是省内存与拷贝成本：`T` 可能是 `Shape`（约 200 字节）甚至 `Tensor`，且 min/max 对象本来就要作为独立槽位数据供其他阶段使用；二是让 `Range` 保持小尺寸浅拷贝 POD（56 字节固定），放入 `Chain`、跨 ABI 传递都安全。代价是引入空指针与别名（单指针构造）两类风险，需要调用方自查。

**练习 2**：`InferShapeContext`、`InferDataTypeContext`、`InferShapeRangeContext` 三个类在继承上有什么共同点？为什么要这样设计？

**答案**：三者都公有继承 `ExtendedKernelContext`，**都不新增任何数据成员**，只是基于同一块 `KernelRunContext` 内存提供不同的类型化读取模板（`Shape` / `ge::DataType` / `Range<Shape>`）。这样框架只需组装一份上下文内存，就能以极小的代价切换「视图」调用不同的推导函数；同时每个类末尾的 `static_assert(is_standard_layout)` 把「零成员、标准布局」固化为编译期 ABI 约束。

**练习 3**：如果给 `Range<T>` 增加一个虚析构函数，会带来什么问题？

**答案**：会引入 vptr，`Range` 不再是 standard_layout（POD），头文件末尾针对使用方的布局契约被破坏；且 56 字节固定布局中 `reserved_` 的位置被 vptr 挤占，新旧 so 混链时 min/max 指针读取错位——这正是 metadef 全线禁用虚函数、用函数指针/模板做分发的原因（参见 u3-l1 的 Chain 设计）。

## 5. 综合实践

把三个推导函数串起来，完成 `MyAdd` 的完整「类型推导三件套」：

1. 在一个练习文件中（可放 `tests/ut/base/testcase/` 下新建，或独立 demo），同时实现 4.1.4 的 `MyAddInferShape`、4.2.4 的 `MyAddInferDataType`、4.3.4 的 `MyAddInferShapeRange`。
2. 写一个 main（或 gtest 用例）：
   - 参照 [context_builder_unittest.cc:165-183](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/context_builder_unittest.cc#L165-L183) 的写法，用 `OpInferShapeContextBuilder` 设置 `OpType("MyAdd")`、`IONum(2, 1)` 并挂两个同形输入张量，`Build()` 后调用 `MyAddInferShape`，断言输出 shape 与输入一致；
   - 想办法让 `GetInputShape(1)` 返回空（例如把第二个输入去掉），断言函数返回 `GRAPH_FAILED`。
3. 若想真正编译运行：`bash tests/run_test.sh -u` 会自动收集 `tests/ut/base/testcase/*.cc`（见 u1-l2），无需改 CMake。没有本地环境时，完成代码编写与逐行走查即可，运行结果标注「待本地验证」。
4. 进阶思考：把 `MyAddInferShape` 中「逐维 SetDim」换成 `*y = *x1;`（Shape 的赋值）是否可行？到 [shape.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/shape.h) 中确认 `Shape` 是否支持赋值运算符、赋值时拷贝多少个维度，并写下结论。

## 6. 本讲小结

- 图编译阶段的三类推导由三个上下文承担：`InferShapeContext`（形状）、`InferDataTypeContext`(数据类型)、`InferShapeRangeContext`（动态 shape 的范围），三者都零新增成员，只是同一块 `KernelRunContext` 内存的不同类型化视图。
- 接口设计遵循「大对象传指针 + 空指针报错，小对象传值 + 哨兵值报错」：shape/range 系列返回指针，dtype 系列返回值并以 `DT_UNDEFINED` 表示失败，唯一的 `graphStatus` 返回出现在 `SetOutputDataType`。
- 输出槽由框架预分配、算子负责填写：shape 直接改 `Shape *`，dtype 用 `SetOutputDataType`，range 通过 `Range` 的 `GetMin()/GetMax()` 可写指针双写。
- `Range<T>` 是「min/max 双指针」的浅 POD，不拥有数据；单指针构造存在 min/max 别名效应，且 `operator==` 不判空，边界须调用方自查。
- REQUIRED/OPTIONAL/DYNAMIC 三类 IR 访问接口在三个上下文中结构完全一致，全部落到基类 `GetDynamicInputPointer` 的「IR 索引 → AnchorInstanceInfo → 物理槽位」翻译。
- 三类推理函数以 `UINT32 (*)(XxxContext *)` 裸函数指针形式经 `OpImplRegisterV2::InferShape/InferDataType/InferShapeRange` 注册，注册链路在 u4-l4 展开。

## 7. 下一步学习建议

本讲结束后，单元三还剩最后一讲 **u3-l5 TilingData**：tiling 参数的序列化与传递，它将补齐 `TilingContext::GetTilingData<T>` 写入的那块字节流的来龙去脉。随后进入单元四「算子注册与发现」，其中 **u4-l4 OpImplRegistry 与 OpImplSpaceRegistry** 会把本讲反复出现的 `InferShapeKernelFunc` 等函数指针如何被注册、按算子名组织并查询讲透。若你想先动手验证本讲的推导函数，可提前阅读 **u5-l1 ContextBuilder 体系**，那里完整讲解 `OpInferShapeContextBuilder` 的 `InputTensors/IONum/Build` 用法。此外，官方 API 文档 `docs/zh/api/gert_namespace/infershapecontext/` 目录下对 `GetInputShape`、`GetOutputShape` 等每个接口都有独立说明页，可作查阅手册。
