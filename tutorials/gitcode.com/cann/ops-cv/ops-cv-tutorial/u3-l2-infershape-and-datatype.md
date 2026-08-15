# Shape 与数据类型推导：Infershape 机制

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 InferShape / InferDataType 在算子执行链路中的位置和触发时机。
2. 掌握 `IMPL_OP_INFERSHAPE` 注册模式，能独立读懂任意算子的 `*_infershape.cpp`。
3. 理解「输出 shape 由谁决定」：add_example 的直通传播 vs resize_bilinear_v2 的按输入值推导。
4. 理解输出 dtype 的两类推导规则：继承输入 vs 由属性（`dtype` attr）驱动并带合法性校验。
5. 会使用 `common/inc/op_api/infershape_utils.h` 中的 `IsConstTensor` 等公共工具。
6. 梳理清楚 aclnnResize 的 `scales` 参数如何一步步变成算子内部的 `size` 输入，并最终决定输出 shape。

## 2. 前置知识

**什么是 Shape 推导（InferShape）？**

框架在真正执行算子之前，必须先知道输出的 shape，才能为输出分配内存、做图优化和内存复用。但用户调用算子时通常只给输入，不给输出 shape——输出 shape 需要算子自己「算出来」，这段逻辑就是 InferShape。同理，输出是什么数据类型（float16？float32？），由 InferDataType 决定。

**推导发生在什么时候？**

回顾 u3-l1 的调用链：aclnn 第一段接口（GetWorkspaceSize）把算子登记进 `aclOpExecutor` 后，框架在准备阶段会回调算子注册的 InferShape / InferDataType 函数；图模式（GE）下则在构图和图编译阶段回调。无论哪种模式，推导都发生在 kernel 真正执行之前。

**两个关键上下文类型（gert = GE Runtime）：**

- `gert::InferShapeContext`：提供 `GetInputShape(i)` 读输入 shape、`GetInputTensor(i)` 读输入的常量值、`GetInputDesc(i)` 读 format 等描述信息、`GetOutputShape(i)` 拿到可写的输出 shape。
- `gert::InferDataTypeContext`：提供 `GetInputDataType(i)` / `SetOutputDataType(i, dtype)`，以及 `GetAttrs()` 读算子属性。

**什么是「常量输入（const tensor）」？**

有些输入（如 resize 的 `size`）在推导时刻值已经确定，框架会把它的值传进上下文，InferShape 可以直接读取数据内容来计算输出 shape——这类输入叫常量输入。是否为常量，正要用本讲的公共工具 `IsConstTensor` 判断。

**动态 shape 与 UnknownRank：**

 Atlas 上支持动态 shape（某维为 -1）甚至 UnknownRank（维度数都未知，记为 -2），InferShape 必须处理这些退化情况，本讲会看到 resize_bilinear_v2 的处理代码。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [examples/add_example/op_host/add_example_infershape.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/op_host/add_example_infershape.cpp) | 最简范本：输出 shape/dtype 完全继承输入 |
| [image/resize_bilinear_v2/op_host/resize_bilinear_v2_infershape.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/resize_bilinear_v2_infershape.cpp) | 进阶范本：按常量输入值推导 shape，按属性推导 dtype |
| [common/inc/op_api/infershape_utils.h](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/common/inc/op_api/infershape_utils.h) | 公共工具：常量张量判断 |
| [image/resize_bilinear_v2/op_host/resize_bilinear_v2_def.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/resize_bilinear_v2_def.cpp) | 算子定义：输入/输出/属性声明，推导的数据来源契约 |
| [image/resize_bilinear_v2/op_api/aclnn_resize.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/aclnn_resize.cpp) | aclnn 层：把 scales 换算成 size 常量张量的上游链路 |
| [examples/add_example/op_graph/add_example_graph_infer.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/op_graph/add_example_graph_infer.cpp) | 图模式侧的 InferDataType 注册（对照用） |

> 注：`resize_bilinear_v2_infershape.cpp` 中 `#include "util/shape_util.h"` 提供的 `SetUnknownShape` / `IsUnknownRank` 来自 CANN toolkit 头文件，不在本仓库内，故不附仓库链接。

## 4. 核心概念与源码讲解

### 4.1 InferShape 的注册与最简实现：add_example

#### 4.1.1 概念说明

一个算子的推导逻辑通过 `IMPL_OP_INFERSHAPE(算子名)` 宏注册到框架。注册时可以挂两类函数：

- `.InferShape(fn)`：推导输出 shape；
- `.InferDataType(fn)`：推导输出 dtype。

add_example（\( y = x_1 + x_2 \)，这里只有一个输入 x）是最简单的情形：**输出 shape 和 dtype 都与输入完全相同**，推导就是「抄一遍」。它是理解所有其他算子 infershape 文件的起点。

#### 4.1.2 核心流程

```text
框架回调 InferShapeAddExample(context)
  ├─ 取输入 0 的 shape（只读）
  ├─ 取输出 0 的 shape（可写）
  ├─ 设置输出维数 = 输入维数
  ├─ 逐维复制 dim
  └─ 返回 GRAPH_SUCCESS
```

#### 4.1.3 源码精读

[add_example_infershape.cpp:36-60](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/op_host/add_example_infershape.cpp#L36-L60) 是 InferShape 主体：先 `GetInputShape(0)` / `GetOutputShape(0)` 并用 `OP_CHECK_NULL_WITH_CONTEXT` 判空，然后 `SetDimNum` + 循环 `SetDim` 把输入 shape 逐维复制到输出。注意所有指针来自框架，判空是仓库级约定。

[add_example_infershape.cpp:71-82](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/op_host/add_example_infershape.cpp#L71-L82) 是 InferDataType：一行 `GetInputDataType(0)` 读入、一行 `SetOutputDataType(0, ...)` 写出，输出 dtype 与输入一致。

[add_example_infershape.cpp:86](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/op_host/add_example_infershape.cpp#L86) 是注册入口：

```cpp
IMPL_OP_INFERSHAPE(AddExample).InferShape(InferShapeAddExample).InferDataType(InferDataTypeAddExample);
```

这一行把两个函数绑到 `AddExample` 这个算子名上——名字必须与 def 文件里 `OP_ADD(AddExample)` 注册的名字一致（Host-Device 跨侧约定之一，见 u3-l1）。

顺带对照：图模式侧还有一份 dtype 推导注册在 [add_example_graph_infer.cpp:24-36](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/op_graph/add_example_graph_infer.cpp#L24-L36)，用的是 `IMPL_OP(AddExample).InferDataType(...)` 宏、服务于 GE 图编译；op_host 侧的 `IMPL_OP_INFERSHAPE` 则服务于算子执行框架。同一个算子两处注册、各管一条通路。

#### 4.1.4 代码实践

1. **实践目标**：确认 add_example 推导逻辑对任意输入 shape 成立。
2. **操作步骤**：打开 `examples/add_example/op_host/add_example_infershape.cpp`，把 `InferShapeAddExample` 中 `yShape->SetDim(i, dim);` 临时改为 `yShape->SetDim(i, dim + 1);`（仅本地阅读实验，勿提交），重新编译 add_example 算子包并运行 aclnn 样例。
3. **需要观察的现象**：样例在第一段接口（GetWorkspaceSize）或输出内存环节即报错——因为推导出的输出 shape 与样例预分配的输出 tensor shape 不一致。
4. **预期结果**：报 shape 不匹配类错误；改回后恢复正常。这印证了 InferShape 结果直接约束输出内存的分配。**待本地验证**（依赖配套 CANN 环境）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `xShape` 用 `const gert::Shape*` 而 `yShape` 用 `gert::Shape*`？
**答案**：输入 shape 对推导函数是只读的（框架保证输入不可被算子修改），输出 shape 是推导的写入目标，需要非 const 指针。

**练习 2**：`IMPL_OP_INFERSHAPE(AddExample)` 中的 `AddExample` 写错成 `AddExample2` 会发生什么？
**答案**：注册会挂到一个不存在的算子名上，`AddExample` 算子在框架需要推导时找不到回调，通常导致算子无法执行或走默认推导失败；它必须与 def 文件 `OP_ADD` 注册名严格一致。

### 4.2 按输入值推导输出 shape：resize_bilinear_v2 的 InferShape

#### 4.2.1 概念说明

resize（双线性插值缩放）的输出 shape 不再等于输入：N、C 两维不变，H、W 由「目标尺寸」决定。目标尺寸从哪来？看 def 文件：算子有第二个输入 `size`（int32 张量，含目标 H/W），推导时它的值已经是常量，可以读取。于是推导规则是：

\[ \text{outShape} = \text{inShape},\quad \text{outShape}[H] = \text{size}[0],\quad \text{outShape}[W] = \text{size}[1] \]

其中 H/W 在 shape 中的下标由输入 format 决定（NCHW 时为 2/3，NHWC 时为 1/2）。

先看契约：[resize_bilinear_v2_def.cpp:41-56](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/resize_bilinear_v2_def.cpp#L41-L56) 声明了输入 `x`、输入 `size`（`ValueDepend(OPTIONAL)` 表示该输入的**值**可被推导阶段依赖）、输出 `y`；[resize_bilinear_v2_def.cpp:58-61](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/resize_bilinear_v2_def.cpp#L58-L61) 声明了 `align_corners`、`half_pixel_centers`、`dtype`、`scales` 四个属性。注意：**`scales` 属性在 InferShape 里并没有被使用**（详见 4.4 节的梳理）。

#### 4.2.2 核心流程

```text
InferShape4Resize2DWithConstSize(context)
  ├─ 取 x shape、y shape、size 张量（各判空）
  ├─ GetSizeFor2D
  │    ├─ 非常量 → 输出 H/W 置为 UNKNOWN_DIM(-1)，直接返回
  │    └─ 常量且 int32 → 读出 size[0]→H, size[1]→W（元素数必须为 2）
  ├─ 取 x 的 OriginFormat（只允许 NCHW/NHWC）
  └─ ResizeInfershapeFor2D
       ├─ x 为 UnknownRank → 输出整体置 (-1,-1,-1,-1)
       ├─ 否则要求 x 为 4D，y = x（先整体复制）
       └─ 按 format 找到 H/W 下标，覆盖为 size 给出的目标值
```

#### 4.2.3 源码精读

[resize_bilinear_v2_infershape.cpp:37-50](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/resize_bilinear_v2_infershape.cpp#L37-L50)：模板函数 `GetSizeValueFor2D` 从常量张量读值。`size_tensor->GetData<T>()` 拿到数据指针，`GetShapeSize()` 校验元素个数必须为 2，然后填进 `OutInfo`（H/W）。

[resize_bilinear_v2_infershape.cpp:52-72](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/resize_bilinear_v2_infershape.cpp#L52-L72)：`GetSizeFor2D` 是分支入口。第一行就用公共工具 `Ops::Cv::IsConstTensor` 判断是否常量：不是常量时输出 H/W 置 `ge::UNKNOWN_DIM`（-1，动态 shape 语义）并打印 WARN 后正常返回；是常量则按 dtype 分派，当前只支持 int32，其他 dtype 用 `OP_LOGE_FOR_INVALID_DTYPE` 报错。

[resize_bilinear_v2_infershape.cpp:74-109](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/resize_bilinear_v2_infershape.cpp#L74-L109)：`ResizeInfershapeFor2D` 完成三件事——校验 format 只能是 NCHW/NHWC（L80-83）；处理 UnknownRank（`-2`，输出全 -1）和 4D 校验（L86-98，先 `*y_shape = *x_shape` 整体复制）；最后按 format 计算 H/W 下标（NHWC 为 1/2，NCHW 为 2/3）并覆盖两个维度（L100-103）。

[resize_bilinear_v2_infershape.cpp:111-135](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/resize_bilinear_v2_infershape.cpp#L111-L135)：入口函数 `InferShape4Resize2DWithConstSize`，串联取 shape → 取 size 值 → 取 format → 调推导，任一步失败返回 `ge::GRAPH_FAILED`。

[resize_bilinear_v2_infershape.cpp:183-186](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/resize_bilinear_v2_infershape.cpp#L183-L186)：注册处多了一行 add_example 没有的东西：

```cpp
IMPL_OP_INFERSHAPE(ResizeBilinearV2)
    .InferShape(InferShape4Resize2DWithConstSize)
    .InputsDataDependency({IN_SIZE})
    .InferDataType(InferDtype4ResizeBilinearV2);
```

`.InputsDataDependency({IN_SIZE})` 告诉框架：推导时需要 `size` 这个输入的**数据值**（而不只是 shape）。没有这行，`GetInputTensor` 拿到的内容不可靠——这是「按值推导」型算子的必备声明，与 def 文件里的 `ValueDepend(OPTIONAL)` 呼应。

#### 4.2.4 代码实践

1. **实践目标**：用具体输入验证推导规则。
2. **操作步骤**：打开 `image/resize_bilinear_v2/examples/test_aclnn_resize.cpp`，找到构造输入与 `aclFloatArray* scales` 的代码段阅读；设定输入 `self` shape 为 `(1, 1, 4, 4)`、`scales = {2.0, 2.0}`，按 4.4 节结论计算输出 shape 应为 `(1, 1, 8, 8)`，再对照样例中 `out` tensor 的构造 shape 是否一致；随后把 scales 改为 `{1.5, 1.5}` 重算（应为 `(1, 1, 6, 6)`，因 \(4 \times 1.5 = 6\)）。
3. **需要观察的现象**：样例中 out 的 shape 与你的手算结果一致；若故意把 out shape 构造错，第一段接口会报参数错误。
4. **预期结果**：手算、样例构造、实际运行三者一致。**待本地验证**（依赖配套 CANN 环境）。

#### 4.2.5 小练习与答案

**练习 1**：输入 `x` shape 为 `(2, 3, -1, -1)`（H/W 动态），`size` 为常量 `[240, 320]`，NCHW format，输出 shape 是什么？
**答案**：`(2, 3, 240, 320)`。整体复制输入后仅覆盖 H/W 两维（下标 2/3），N、C 与动态维度中未被覆盖的部分保持原值——这里 H/W 恰好是被覆盖的维度。

**练习 2**：为什么 `size` 不是常量时函数返回 `true`（成功）而不是失败？
**答案**：非常量是合法的动态 shape 场景，此时输出 H/W 推不出来，置为 `UNKNOWN_DIM`(-1) 表示「运行时才能确定」，推导本身没有错；这是动态 shape 算子的标准处理方式。

### 4.3 InferDataType：从直通到属性驱动

#### 4.3.1 概念说明

输出 dtype 的推导有两类典型模式：

| 模式 | 代表 | 规则 |
| --- | --- | --- |
| 直通传播 | add_example | 输出 dtype = 输入 dtype |
| 属性驱动 + 合法性校验 | resize_bilinear_v2 | 默认 float32；若设置了 `dtype` 属性则按属性，且校验输入/输出 dtype 组合合法 |

#### 4.3.2 核心流程

```text
InferDtype4ResizeBilinearV2(context)
  ├─ 输出先置为 DT_FLOAT（默认值）
  ├─ 取 attrs；attrs 为空或未配置 → 直接成功（保持默认）
  ├─ 读第 3 个属性 dtype（ATTR_2_IDX=2）
  ├─ 校验其只能是 float32/float16/bfloat16/uint8
  ├─ 校验输入 x dtype 与目标 dtype 的组合：
  │    x=float32  → 目标不能是 float16/bfloat16（降精度禁止）
  │    x=float16 ↔ 目标=bfloat16 互斥
  └─ 通过后 SetOutputDataType(0, 目标 dtype)
```

#### 4.3.3 源码精读

[resize_bilinear_v2_infershape.cpp:137-181](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/resize_bilinear_v2_infershape.cpp#L137-L181)：完整实现。几个值得注意的细节：

- L146 先无条件把输出置为 `ge::DT_FLOAT`，属性缺失时这就是最终结果——「有默认值」的属性驱动推导都建议这么写，保证任何路径下输出 dtype 都被赋值。
- L153 `attrsPtr->GetAttrPointer<int64_t>(ATTR_2_IDX)` 按下标取属性（属性顺序见 def 文件 L58-61：`align_corners`(0)、`half_pixel_centers`(1)、`dtype`(2)、`scales`(3)），返回空表示未配置。
- L161-164 白名单校验；L168-175 是两张组合约束表（禁止 float32→半精度降精度、禁止 fp16/bf16 互转），失败用 `OP_LOGE` 报出人能读懂的信息。

这套约束与 def 文件中的 dtype 对列表 [resize_bilinear_v2_def.cpp:18-23](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/resize_bilinear_v2_def.cpp#L18-L23) 一一对应（`valueDataTypeX` 与 `valueDataTypeY` 按下标配对），def 管「编译期允许哪些组合」，InferDataType 管「运行期这个具体组合输出是什么」。

#### 4.3.4 代码实践

1. **实践目标**：体会 dtype 属性对输出的影响。
2. **操作步骤**：阅读 `image/resize_bilinear_v2/examples/test_aclnn_resize.cpp` 中输出 tensor 的 dtype 构造；对照 aclnn 接口签名（`image/resize_bilinear_v2/op_api/aclnn_resize.h`）确认默认路径下输出为 float；再思考：若想输出 float16，输入应选什么 dtype？
3. **需要观察的现象**：样例默认输入输出均为 float32；按约束表，float16 输出要求输入是 float16（float32 输入配 float16 输出会被 L168-170 拒绝）。
4. **预期结果**：能复述合法输入/输出 dtype 组合表。**待本地验证**（依赖配套 CANN 环境）。

#### 4.3.5 小练习与答案

**练习 1**：输入 dtype 为 bfloat16，`dtype` 属性设为 float16，会发生什么？
**答案**：推导失败返回 `GRAPH_FAILED`。L171-175 明确禁止 `xDtype=float16 且 out=bfloat16` 或 `xDtype=bfloat16 且 out=float16`，两者互斥。

**练习 2**：为什么不把 `uint8` 输出和 `float32` 输入的组合也禁掉？
**答案**：本讲只依据源码：L161-164 白名单允许 uint8，L168-175 的两条约束都未涉及 uint8，因此 float32 输入 + uint8 输出在推导层是被允许的（是否有业务意义属于算子语义层问题）。读源码时要以代码为准，不要臆测约束。

### 4.4 公共工具与 scales→size 的完整链路

#### 4.4.1 概念说明

本模块回答讲义规格中的核心问题：**「当 scales 变化时，输出 shape 到底是怎么推导出来的？」**

结论先行：在当前实现中，InferShape 并不直接消费 `scales`；`scales` 在 **aclnn 层**被换算成一个 int32 的 `size` 常量张量，再作为算子的 `size` 输入进入 4.2 节的推导。链路是：

```text
用户调用 aclnnResize(self, scales, mode, out)
  └─ aclnn 层（CreateSizesRegBase / CreateSizesV35）
       sizesList = [ int64(H × scales_h), int64(W × scales_w) ]   ← 截断取整
       转成 int32 常量张量 size
  └─ 算子 size 输入（InputsDataDependency 声明按值依赖）
  └─ InferShape4Resize2DWithConstSize 读 size 值 → 覆盖输出 H/W
```

即 scales 决定输出的真正公式是：

\[ \text{outH} = \lfloor \text{inH} \times \text{scales}_h \rfloor,\qquad \text{outW} = \lfloor \text{inW} \times \text{scales}_w \rfloor \]

（`static_cast<int64_t>` 向零截断。）

#### 4.4.2 核心流程

以输入 `(1, 1, 4, 4)`、NCHW 为例：

| scales | 计算 | 输出 shape |
| --- | --- | --- |
| {2.0, 2.0} | \(4 \times 2.0 = 8\) | (1, 1, 8, 8) |
| {1.5, 1.5} | \(4 \times 1.5 = 6\) | (1, 1, 6, 6) |
| {0.75, 0.75} | \(4 \times 0.75 = 3\) | (1, 1, 3, 3) |
| {0.6, 0.6} | \(4 \times 0.6 = 2.4 \to 2\) | (1, 1, 2, 2)（截断） |
| {0.5, 0.5}（若 inH=5） | \(5 \times 0.5 = 2.5 \to 2\) | 截断为 2 |

另外，def 文件里那个 `scales` **属性**（[resize_bilinear_v2_def.cpp:61](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/resize_bilinear_v2_def.cpp#L61)，默认 `{0.0f, 0.0f}`）在本仓库的 InferShape 与 kernel 链路中均未被消费（全仓库检索 `scales` 的命中只有 proto 声明、aclnn 参数检查和 def 声明），属于为接口兼容保留的声明；aclnn 源码里也有一行注释直接说明这一点。

#### 4.4.3 源码精读

公共工具 [infershape_utils.h:23-32](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/common/inc/op_api/infershape_utils.h#L23-L32)：`Ops::Cv::IsConstTensor` 判断张量是否携带常量值——指针非空且（有数据地址，或 shape size 为 0 的占位）即视为常量。它就是 4.2 节 `GetSizeFor2D` 分支的判断依据，任何「按输入值推导」的算子都应复用它而不是自己手写判断。

上游换算一（新架构路径）[aclnn_resize.cpp:209-239](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/aclnn_resize.cpp#L209-L239)：`CreateSizesRegBase` 先校验 `scales` 元素数与 `self` 维数一致，再按 format 取 H/W 下标，做 `int64(inDim × scale)` 截断换算成 `sizesList`，最后经 `executor->AllocIntArray` + `ConvertToTensor` 变成 int32 张量交给算子——**注意这一步只登记不执行，符合 u2-l2 讲的「第一段记账」模型**。

上游换算二（V35 老架构路径）[aclnn_resize.cpp:201-207](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/aclnn_resize.cpp#L201-L207)：`CreateSizesV35` 更直接——用户传入的 `out` tensor 的 shape 本身就含目标 H/W，把 out 的 shape 向量转成 int32 张量即可。

容差校验 [aclnn_resize.cpp:103-121](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/aclnn_resize.cpp#L103-L121)：新架构路径还反向校验用户预分配的 `out` shape 是否落在 \([\lfloor \text{inH}(s-\varepsilon) \rfloor, \lfloor \text{inH}(s+\varepsilon) \rfloor]\) 区间内，给浮点换算留出容差。而 L81 的注释明确写道：`The scale parameter is retained for interface consistency and is currently not used in computations.`（scales 为接口一致性保留）。

#### 4.4.4 代码实践

1. **实践目标**：亲手走通「scales → 输出 shape」的推导，验证截断规则。
2. **操作步骤**：
   - 阅读两个算子的 InferShape 实现（本文 4.1、4.2 精读段落）；
   - 手算表格：输入 `(1, 1, 4, 4)` NCHW，分别取 scales = `{2.0,2.0}`、`{1.5,1.5}`、`{0.6,0.6}`，按公式写出输出 shape；
   - 打开 `image/resize_bilinear_v2/examples/test_aclnn_resize.cpp`，比照样例中 `out` 的构造 shape 与你的手算结果；
   - 如有环境：编译安装算子包后用 `bash build.sh --run_example resize_bilinear_v2 eager custom`（自定义包模式，参数以 `docs/zh/invocation/quick_op_invocation.md` 为准）运行样例，观察日志中的输入输出 shape。
3. **需要观察的现象**：手算与样例构造一致；`{0.6, 0.6}` 时输出 H/W 为 2（\(4 \times 0.6 = 2.4\) 截断），验证截断而非四舍五入。
4. **预期结果**：三条 scales 取值的输出 shape 分别为 `(1,1,8,8)`、`(1,1,6,6)`、`(1,1,2,2)`。运行部分**待本地验证**（依赖配套 CANN 环境）。

#### 4.4.5 小练习与答案

**练习 1**：`InputsDataDependency({IN_SIZE})` 删掉后，`GetSizeFor2D` 大概率走到哪个分支？
**答案**：走到 `IsConstTensor` 为假的分支，输出 H/W 被置为 `UNKNOWN_DIM`(-1)。因为框架不知道推导需要 size 的值，可能不传递常量数据，算子只能按动态 shape 降级处理。

**练习 2**：为什么 `CreateSizesRegBase` 里要先检查「scales 元素数 == self 维数」而不是「== 2」？
**答案**：aclnnResize 是按维度给 scale 的通用接口（scales 长度等于输入维数），而换算成算子 `size` 时只取 H/W 两个维度对应的 scale（NCHW 下取下标 2/3，NHWC 下取 1/2），所以先按维数校验完整性，再按 format 挑出空间两维。

**练习 3**：`IsConstTensor` 中 `GetAddr() == nullptr` 时为什么还要看 `GetShapeSize() == 0`？
**答案**：shape size 为 0 的张量（空张量）本身没有数据地址，但仍可在推导阶段被当作「已知的常量空输入」处理；返回 true 让上层逻辑统一走常量分支而不是误判为非法。

## 5. 综合实践

**任务：为 resize_bilinear_v2 写一份「输出 shape/dtype 推导说明卡」并用三个用例验证。**

1. 阅读三个源文件：`examples/add_example/op_host/add_example_infershape.cpp`、`image/resize_bilinear_v2/op_host/resize_bilinear_v2_infershape.cpp`、`common/inc/op_api/infershape_utils.h`。
2. 产出一张说明卡，包含：
   - 推导触发点（框架在何时回调，参考 u3-l1 调用链图）；
   - 输出 shape 推导伪代码（含 format 分支、UnknownRank、size 非常量三种情况）；
   - 输出 dtype 推导规则表（默认值、属性覆盖、非法组合）；
   - scales → size 的换算公式与截断规则，以及 `InputsDataDependency` 的作用。
3. 设计三个验证用例并手算预期：
   - `(2, 3, 240, 320)` NCHW + size `[480, 640]` → 输出 `(2, 3, 480, 640)`；
   - `(1, 8, 5, 5)` NHWC + scales `{0.5, 0.5}` → 输出 `(1, 8, 2, 2)`（\(5 \times 0.5 = 2.5\) 截断，注意 NHWC 下 H/W 在下标 1/2）；
   - `(1, 3, -1, -1)` + size 非常量 → 输出 `(1, 3, -1, -1)`（H/W 保持未知）。
4. 有环境的话，把前两个用例改进 `test_aclnn_resize.cpp` 跑通；无环境则对照样例源码静态核对构造代码。运行部分**待本地验证**。

## 6. 本讲小结

- InferShape / InferDataType 通过 `IMPL_OP_INFERSHAPE(算子名)` 注册，在 kernel 执行前由框架回调，决定输出内存的形状与类型。
- add_example 展示最简模式：输出 shape/dtype 逐项复制输入；resize_bilinear_v2 展示进阶模式：整体复制输入后，按常量输入 `size` 的值覆盖 H/W 两维。
- 「按输入值推导」需要两件配套：def 文件 `ValueDepend(OPTIONAL)` + 注册处 `InputsDataDependency({输入下标})`，判断常量用公共工具 `Ops::Cv::IsConstTensor`。
- 推导函数必须处理动态 shape：维度未知用 `UNKNOWN_DIM`(-1)，UnknownRank(-2) 时输出整体置 -1，且都算推导成功。
- InferDataType 有两类模式：直通传播（输出=输入）与属性驱动（`dtype` 属性覆盖默认 float32，并做输入/输出组合合法性校验）。
- aclnnResize 的 `scales` 参数在 aclnn 层被截断换算成 int32 `size` 常量张量（\(\text{outDim} = \lfloor \text{inDim} \times \text{scale} \rfloor\)），def 中的 `scales` 属性仅为兼容保留、不参与推导。

## 7. 下一步学习建议

本讲解决了「输出 shape/dtype 怎么来」，下一讲 **u3-l3 Tiling 机制：算子切分与多核并行** 将解决「知道了 shape，数据怎么切给多个 AI Core」：以 `add_example_tiling.cpp` 为主线，讲解 `GetPlatformInfo`、`GetShapeAttrsInfo`、TilingData 填充与 `IMPL_OP_OPTILING` 注册——那是 op_host 侧另一半核心工作。建议提前浏览 `examples/add_example/op_host/add_example_tiling.cpp` 与 `common/inc/op_host/tiling_base.h`。
