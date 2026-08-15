# Shape 推导：Infershape 的实现与验证

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 Infershape 在算子执行全流程中的位置，以及它在什么时机被触发、被谁调用。
2. 读懂 `*_infershape.cpp` 的标准写法：`InferShape`（推导输出 shape）与 `InferDataType`（推导输出数据类型）两个函数，以及 `IMPL_OP_INFERSHAPE` 注册宏。
3. 理解广播（broadcast）规则：当算子的两个输入 shape 不同时，输出 shape 如何推导。
4. 会使用仓库提供的 `InfershapeContextPara` + `ExecuteTestCase` 框架编写并运行一个 infershape UT 用例。

本讲承接 u3-l1（算子原型定义 `*_def.cpp`）。`*_def.cpp` 声明"算子是谁、有哪些输入输出"，本讲的 `*_infershape.cpp` 则回答"输出张量的 shape 和 dtype 是什么"。

## 2. 前置知识

在阅读本讲前，用通俗语言建立几个概念：

- **静态 shape 与动态 shape**：用户调用算子时给定了输入张量的 shape（例如 `(1, 128, 128, 64)`）。框架需要在**真正执行 kernel 之前**就知道输出张量多大——因为要为输出申请内存。shape 里可能出现 `-1` 表示"该维度编译期未知"（动态 shape），这正是 shape "推导" 而非 "复制" 的原因。
- **Host 侧与 Device 侧**：infershape 完全运行在 Host（CPU）侧，是编译/下发阶段的逻辑，不涉及 NPU 计算。它与 tiling（u4 单元）、kernel（u5 单元）同属 op_host/op_kernel 分工中的 Host 侧推导环节。
- **gert 命名空间**：GE Runtime 的缩写。`gert::Shape`、`gert::InferShapeContext` 等类型来自 CANN 的 GE 框架头文件，ops-nn 只是实现回调并注册。
- **广播（broadcast）**：两个 shape 不同的张量做逐元素运算时，小张量按规则"拉伸"成与大张量兼容的形状。详见 4.3 节。
- **googletest（gtest）**：Google 的 C++ 单元测试框架，`TEST_F` 宏定义一个测试用例。ops-nn 的 UT 全部基于它。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [examples/add_example/op_host/add_example_infershape.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_infershape.cpp) | 教学算子 AddExample 的 shape/dtype 推导实现，本讲精读对象 |
| [examples/add_example/tests/ut/op_host/test_add_example_infershape.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/tests/ut/op_host/test_add_example_infershape.cpp) | AddExample 的 infershape UT 用例，本讲实践的模板 |
| [tests/ut/common/infershape_case_executor.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/common/infershape_case_executor.h) | infershape UT 的公共执行器接口（`ExecuteTestCase`） |
| [tests/ut/common/infer_shape_context_faker.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/common/infer_shape_context_faker.h) | UT 侧伪造 `InferShapeContext` 的工具（`InfershapeContextPara`） |
| [docs/zh/context/broadcast_relationship.md](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/context/broadcast_relationship.md) | 官方广播规则文档 |
| [optim/lamb_apply_weight_assign/op_host/lamb_apply_weight_assign_infershape.cpp](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/optim/lamb_apply_weight_assign/op_host/lamb_apply_weight_assign_infershape.cpp) | 生产算子中真实的广播推导案例 |

## 4. 核心概念与源码讲解

本讲的最小模块：

1. Infershape 在执行流程中的位置与触发时机
2. AddExample 的 InferShape / InferDataType 实现
3. 广播规则与 BroadcastShape 的生产用法
4. infershape UT 框架：InfershapeContextPara 与 ExecuteTestCase

### 4.1 Infershape 的位置与触发时机

#### 4.1.1 概念说明

回顾 u1-l3 建立的认知：一个算子工程包含 op_host、op_kernel、op_api、op_graph 等交付件。其中 op_host 里有三个 Host 侧源文件分工明确：

- `*_def.cpp`：声明算子身份证（上一讲）。
- `*_infershape.cpp`（本讲）：根据输入 shape 推导输出 shape 和 dtype。
- `*_tiling.cpp`（u4 单元）：根据 shape 计算切分参数。

infershape 要解决的问题是：**框架在调用 kernel 之前，必须先知道输出张量占多大内存、是什么类型**。谁来推导？不能让框架硬编码（上千个算子各有各的推导规则），所以每个算子自己注册一个推导回调，框架在需要时调用它。

#### 4.1.2 核心流程

以 aclnn eager 调用（u2-l1）为例，infershape 的触发时机大致是：

```text
用户调用 aclnnXxx
    └─ 第一段 GetWorkspaceSize：参数校验
         └─ 框架需要确定输出 aclTensor 的 shape/dtype
              └─ 查算子注册表，调用该算子注册的 InferShape 回调   ← 本讲主角
              └─ 调用 InferDataType 回调
         └─ 确定 workspace 大小
    └─ 第二段：异步下发 tiling → kernel 执行
```

在 GE 图模式（u2-l2）下触发点更早：图编译阶段就要为每个节点推导输出描述，同样通过 `IMPL_OP_INFERSHAPE` 注册的回调完成。

#### 4.1.3 源码精读

注册发生在 infershape 文件的最后一行：

[examples/add_example/op_host/add_example_infershape.cpp:L85-L88](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_infershape.cpp#L85-L88) —— 用 `IMPL_OP_INFERSHAPE(AddExample)` 把两个推导函数挂到 AddExample 算子上：`.InferShape(...)` 注册 shape 推导，`.InferDataType(...)` 注册 dtype 推导。这与 `*_def.cpp` 里的 `OP_ADD` 宏（u3-l1）一样是"静态注册"模式：宏在全局对象初始化时把信息写入算子注册表，后续框架按算子名查找。注意函数本身是 `static` 的——它们只通过注册表被间接调用，不对外暴露符号。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：确认 infershape 注册与调用链的对应关系。
2. **操作步骤**：在仓库根目录执行 `grep -rn "IMPL_OP_INFERSHAPE" activation/gelu/`，找到 gelu 的 infershape 注册行；再对照 [activation/gelu/op_host/](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/gelu_def.cpp) 里的 `OP_ADD(Gelu)`，确认两处算子名字符串一致。
3. **需要观察的现象**：每个 `OP_ADD(Xxx)` 注册的算子，都能在同目录找到一个 `IMPL_OP_INFERSHAPE(Xxx)`。
4. **预期结果**：gelu 存在 `IMPL_OP_INFERSHAPE(Gelu).InferShape(...)` 这样的注册语句。

#### 4.1.5 小练习与答案

**练习 1**：如果 `*_def.cpp` 用 `OP_ADD(Foo)` 注册，而 infershape 文件写成 `IMPL_OP_INFERSHAPE(Fooo)`（多了一个字母），会发生什么？

**答案**：注册表里找不到 `Fooo` 这个算子的原型定义，编译期或加载期会报"算子未注册/原型缺失"类错误；即便侥幸通过，`Foo` 也会因为没有推导函数而在运行时无法推出输出 shape。所以两处算子名必须严格一致。

**练习 2**：infershape 运行在 Host 还是 Device？它能看到输入张量的**数据**吗？

**答案**：运行在 Host（CPU）侧。它只能通过 `context` 拿到输入的 shape、dtype、format 等**描述信息**，拿不到（也不应该依赖）张量的具体数值。

### 4.2 AddExample 的 InferShape / InferDataType 实现

#### 4.2.1 概念说明

AddExample 是逐元素相加：`y = x1 + x2`（教学版约定两输入同 shape）。逐元素运算且无归约、无尺寸变化时，输出 shape 就是输入 shape 的"传播"——这是最简单的一类推导。数据类型同理：float 进 float 出。

#### 4.2.2 核心流程

```伪代码
InferShapeAddExample(context):
    xShape = context->GetInputShape(0)        # 取输入 0 的 shape
    yShape = context->GetOutputShape(0)       # 取输出 0 的 shape 占位
    yShape.SetDimNum(xShape.GetDimNum())      # 维度数相同
    for i in 0..xShape.GetDimNum():
        yShape.SetDim(i, xShape.GetDim(i))    # 逐维复制
    return GRAPH_SUCCESS
```

#### 4.2.3 源码精读

[examples/add_example/op_host/add_example_infershape.cpp:L37-L61](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_infershape.cpp#L37-L61) —— `InferShapeAddExample` 的完整逻辑：先 `GetInputShape(IDX_0)` 拿输入 shape，`OP_CHECK_NULL_WITH_CONTEXT` 做空指针防御（为空直接报错返回，这是所有 Host 侧代码的标准姿势），然后逐维把 x 的维度复制到 y。注意 `gert::Shape` 是"先 SetDimNum 再 SetDim"的两步写法，维度值里的 `-1`（动态维度）会被原样透传——这也是教学样例 UT 里能出现 `{1, -1, -1, 64}` 期望输出的原因。

[examples/add_example/op_host/add_example_infershape.cpp:L72-L83](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_infershape.cpp#L72-L83) —— `InferDataTypeAddExample`：取输入 0 的 dtype，直接写到输出 0。`gert::InferDataTypeContext` 提供与 InferShapeContext 平行的 Get/Set 接口。

另外注意 [L26](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_infershape.cpp#L26) 的 `static constexpr int64_t IDX_0 = 0`：生产代码习惯用命名常量代替裸数字索引，避免多输入算子里 `GetInputShape(3)` 这类"魔法数字"。

#### 4.2.4 代码实践（源码阅读型 + 本地验证）

1. **实践目标**：直观感受"shape 传播"。
2. **操作步骤**：
   - 打开 [examples/add_example/examples/test_aclnn_add_example.cpp](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/examples/test_aclnn_add_example.cpp)，找到构造输入 `aclTensor` 时传入的 shape（u1-l4 已走过该样例）。
   - 把输入 shape 从二维改为 `{8, 8, 8, 8}`（同步修改 shape 乘积与输入输出 vector 长度，u1-l4 总结过的三处一致约束），重新 `bash build.sh --run_example add_example eager cust` 运行。
3. **需要观察的现象**：输出张量 shape 随输入变为 4 维 `{8,8,8,8}`，打印数据个数变为 4096。
4. **预期结果**：输出 shape 恒等于输入 shape——这正是 `InferShapeAddExample` 逐维复制的结果。（本实践需在配套 NPU 环境执行，结果待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：把循环里的 `yShape->SetDim(i, dim)` 改成 `yShape->SetDim(i, dim * 2)`，输出会怎样变化？这合法吗？

**答案**：推导出的输出每个维度都是输入的两倍。语法上能编译通过，但语义上与 kernel 实现（逐元素相加，元素数取自 tiling 的 totalIdx）不一致，会导致输出内存与实际写入数据量不匹配，属于逻辑 bug。说明 infershape 必须与 kernel/tiling 的约定保持一致，不能孤立编写。

**练习 2**：为什么输出 shape 由 `context->GetOutputShape(0)` 返回的指针就地修改，而不是 return 一个 shape？

**答案**：框架预先为输出准备了 shape 占位对象，推导函数通过指针把结果写进去，返回值只用于报告成功/失败（`ge::graphStatus`）。这是 C 接口风格"出参 + 状态码"的约定，也便于统一错误处理。

### 4.3 广播规则与 BroadcastShape 的生产用法

#### 4.3.1 概念说明

AddExample 约定两输入同 shape，所以"复制"就够了。但真实算子大量支持**广播**：`a.shape=(2,2,3)` 与 `b.shape=(2,3)` 相加，b 会被自动扩展。此时输出 shape 既不是 a 的也不是 b 的，必须按广播规则推导。这是 infershape 真正"推理"的含量所在。

#### 4.3.2 核心流程

依据 [docs/zh/context/broadcast_relationship.md](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/context/broadcast_relationship.md)，三条规则：

- **规则 1（左侧补 1）**：维度数不一致时，所有数组向最长者看齐，不足的部分在**左侧**补 1。如 \(a.shape=(2,2,3)\)、\(b.shape=(2,3)\)，则 b 先变为 \((1,2,3)\)。
- **规则 2（拉伸 1）**：维度数一致后，某数组某维为 1 则拉伸到对方大小。上例 b 进一步变为 \((2,2,3)\)，与 a 相同。
- **规则 3（不兼容报错）**：维度数一致且对应维既不相等也无 1 时，广播失败，算子执行报错。

输出 shape 的每 个维度取两输入对应维度的最大值：

\[
out_i = \max(a_i, b_i) \quad \text{（在左侧补 1对齐之后）}
\]

文档还给出一条数据类型相关的限制：当参与广播的数据类型落在 COMPLEX64、COMPLEX128、DOUBLE、INT16、UINT16、UINT64 中时，"连续的需要广播的轴与连续的不需要广播的轴合并后的维度"须小于 6，否则广播失败。写支持矩阵时别忽略这条。

#### 4.3.3 源码精读

ops-nn 的生产算子不手写上述循环，而是复用 CANN 头文件 `infershape_broadcast_util.h` 提供的 `BroadcastShape`（该头文件来自 CANN toolkit，不在本仓库内）：

[optim/lamb_apply_weight_assign/op_host/lamb_apply_weight_assign_infershape.cpp:L27-L49](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/optim/lamb_apply_weight_assign/op_host/lamb_apply_weight_assign_infershape.cpp#L27-L49) —— `InferShape4LambApplyWeightAssign`：取两个输入 shape 与输出 shape，调用 `BroadcastShape(update_shape, param_shape, output_shape)` 完成广播推导；返回 false 时用 `OP_LOGE` 打日志并 `return ge::GRAPH_FAILED`（`OP_CHECK_IF` 宏是"条件成立则报错返回"的标准封装）。L43-L46 还处理了一个生产细节：**全标量输入**广播后得到 0 维空 shape `()`，代码将其归一为 `(1,)`，注释解释了原因——动态 shape 编译期的 DFX 生成会对空 shape 做 reduce 连乘（无初值）而报 TypeError。这类"踩坑注释"是读生产代码最有价值的部分之一。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：用广播规则手工推导，再对照工具函数验证。
2. **操作步骤**：
   - 手工推导：\(a.shape=(5,1,5,5,1,1)\)、\(b.shape=(5,5,5,5,5,5)\)，写出广播后的输出 shape（这正是官方文档"限制"一节的第二个例子）。
   - 执行 `grep -rn "BroadcastShape" optim/ loss/ norm/ --include=*infershape.cpp | head -20`，任选一个算子，读它的 InferShape 函数，确认其在什么输入索引之间做广播、失败时如何报错。
3. **需要观察的现象**：广播推导失败路径都有 `OP_LOGE + return ge::GRAPH_FAILED` 的组合。
4. **预期结果**：手工推导结果为 \((5,5,5,5,5,5)\)，且该例合并后维度为 4，小于 6，广播成功。

#### 4.3.5 小练习与答案

**练习 1**：\(a.shape=(1,3)\)、\(b.shape=(3,1)\)，输出 shape 是什么？

**答案**：\((3,3)\)。两维均满足"一方为 1 则拉伸"，a 拉到 \((3,3)\)，b 也拉到 \((3,3)\)。

**练习 2**：\(a.shape=(2,2,3)\)、\(b.shape=(2,3)\) 相加，为什么 b 是左侧补 1 成为 \((1,2,3)\) 而不是右侧补成 \((2,3,1)\)？

**答案**：广播规则 1 明确"左侧填充 1"，这保证了维度语义对齐——shape 最右侧维度对应最内层（变化最快）的轴，右侧补 1 会改变语义（相当于在轴的排列里插入新轴而不是对齐已有轴）。这与 NumPy 的行为一致。

**练习 3**：如果要把 AddExample 改造成支持广播（x1 任一 shape、x2 可广播），`InferShapeAddExample` 需要怎么改？

**答案**：不能只复制输入 0 的 shape，需要取两个输入 shape，调用 `BroadcastShape(x1Shape, x2Shape, yShape)`（或手写规则 1+规则 2 的循环），失败时报错返回；同时 tiling 与 kernel 也要支持按广播后 shape 处理两输入各自的步进——infershape 只是三层联动中的一层。

### 4.4 infershape UT 框架：InfershapeContextPara 与 ExecuteTestCase

#### 4.4.1 概念说明

infershape 是纯 Host 逻辑，不需要 NPU 就能测试，是最容易写 UT 的部分。仓库在 [tests/ut/common/](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/common/infershape_case_executor.h) 提供了公共设施：`InfershapeContextPara` 描述一个测试场景（算子名 + 输入描述列表 + 输出描述列表），`ExecuteTestCase` 负责伪造 `InferShapeContext`、调用注册的真实推导函数、比对期望结果。测试作者只需要"填表格"。

#### 4.4.2 核心流程

```伪代码
构造 InfershapeContextPara:
    opName = "AddExample"
    inputTensorDesc = [ {shape, dtype, format}, {shape, dtype, format} ]   # 两个输入
    outputTensorDesc = [ {空shape, dtype, format} ]                        # 输出占位
expectOutputShape = [ 期望的输出 shape ]

ExecuteTestCase(para, 期望返回码, expectOutputShape):
    faker 把 para 变成假的 InferShapeContext
    按算子名从注册表找到 IMPL_OP_INFERSHAPE 注册的 InferShape 回调并执行
    断言返回码与期望一致、输出 shape 与 expectOutputShape 一致
```

#### 4.4.3 源码精读

[examples/add_example/tests/ut/op_host/test_add_example_infershape.cpp:L22-L37](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/tests/ut/op_host/test_add_example_infershape.cpp#L22-L37) —— 现有用例 `add_example_infershape_test1`：两个输入的 shape 均为 `{1, -1, -1, 64}`（含两个动态维度 `-1`），`ge::DT_FLOAT16` + `ge::FORMAT_ND`；输出描述里 shape 留空 `{{}, {}}`（`StorageShape` 的占位）；期望输出 shape `{1, -1, -1, 64}`，期望返回码 `ge::GRAPH_SUCCESS`。注意每个输入描述内层是两个 shape 列表——对应 `gert::StorageShape` 的存储 shape 与原始 shape 两个成员（见 [tests/ut/common/infer_shape_context_faker.h:L25-L28](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/common/infer_shape_context_faker.h#L25-L28) 的 `TensorDescription` 构造函数）。

[tests/ut/common/infershape_case_executor.h:L16-L18](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/common/infershape_case_executor.h#L16-L18) —— `ExecuteTestCase` 的签名：第二个参数是期望的 `ge::graphStatus`（默认 `GRAPH_FAILED`，即不传时期望失败），第三个是期望输出 shape 列表。测试**负向路径**（shape 不合法应报错）时只需传 `ge::GRAPH_FAILED`。

用例如何被纳入编译？看 [examples/add_example/tests/ut/op_host/CMakeLists.txt:L11-L15](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/tests/ut/op_host/CMakeLists.txt#L11-L15)：目录下所有源文件被 `add_modules_ut_sources` 以 `OP_INFERSHAPE_MODULE_NAME` 模式收集——**新增用例文件放进这个目录即可被自动编入**，无需改 CMake。

#### 4.4.4 代码实践：为 AddExample 新增 infershape UT 用例（本讲核心实践）

1. **实践目标**：为 AddExample 编写一个新的 UT 用例，覆盖标量输入与 4 维输入两组 shape，断言输出 shape 推导正确。

2. **操作步骤**：

   a. 复制 [test_add_example_infershape.cpp](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/tests/ut/op_host/test_add_example_infershape.cpp) 中的既有用例，在其后新增两个 `TEST_F`（以下为示例代码，按仓库既有格式编写）：

   ```cpp
   // 示例代码：4 维静态 shape
   TEST_F(AddExampleInfershape, add_example_infershape_test_4d)
   {
       gert::InfershapeContextPara infershapeContextPara(
           "AddExample",
           {
               {{{8, 8, 8, 8}, {8, 8, 8, 8}}, ge::DT_FLOAT, ge::FORMAT_ND},
               {{{8, 8, 8, 8}, {8, 8, 8, 8}}, ge::DT_FLOAT, ge::FORMAT_ND},
           },
           {
               {{{}, {}}, ge::DT_FLOAT, ge::FORMAT_ND},
           });
       std::vector<std::vector<int64_t>> expectOutputShape = {
           {8, 8, 8, 8},
       };
       ExecuteTestCase(infershapeContextPara, ge::GRAPH_SUCCESS, expectOutputShape);
   }

   // 示例代码：标量（0 维）输入
   TEST_F(AddExampleInfershape, add_example_infershape_test_scalar)
   {
       gert::InfershapeContextPara infershapeContextPara(
           "AddExample",
           {
               {{{}, {}}, ge::DT_FLOAT, ge::FORMAT_ND},
               {{{}, {}}, ge::DT_FLOAT, ge::FORMAT_ND},
           },
           {
               {{{}, {}}, ge::DT_FLOAT, ge::FORMAT_ND},
           });
       std::vector<std::vector<int64_t>> expectOutputShape = {
           {},
       };
       ExecuteTestCase(infershapeContextPara, ge::GRAPH_SUCCESS, expectOutputShape);
   }
   ```

   b. 按 [docs/zh/install/compile.md](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/install/compile.md) 的 UT 说明，先安装依赖再跑 add_example 的 Host 侧 UT：

   ```bash
   pip3 install -r tests/requirements.txt
   bash build.sh -u --ophost --ops=add_example
   ```

3. **需要观察的现象**：
   - gtest 输出中出现两个新用例名 `add_example_infershape_test_4d` 与 `add_example_infershape_test_scalar`；
   - `InferShapeAddExample` 对标量输入返回 `GRAPH_SUCCESS`，输出 shape 为空 `()`（`GetDimNum()==0`，逐维复制循环零次）——即"shape 传播"对 0 维输入自然成立；
   - 最终输出 `[ PASSED ] N tests.`。

4. **预期结果**：两个用例均通过。若标量用例失败，请回看 4.3.3 提到的生产代码细节——`BroadcastShape` 会把全标量归一为 `(1,)`，而 AddExample 用的是"逐维复制"，二者对 0 维的处理策略不同，这本身就是值得记录的观察点。（本实践需在配套环境执行，运行结果待本地验证。）

   补充一个负向实验（可选）：把 4 维用例的 `expectOutputShape` 故意改成 `{8, 8, 8, 4}` 再跑，应看到 gtest 报 `FAILED`，用于确认断言机制真实生效。

#### 4.4.5 小练习与答案

**练习 1**：`ExecuteTestCase` 的第二个参数默认值是什么？为什么默认值是这个？

**答案**：默认 `ge::GRAPH_FAILED`（见 [infershape_case_executor.h:L17](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/common/infershape_case_executor.h#L17)）。这是一种"缺省从严"的设计：不显式声明期望成功的用例默认按失败路径校验，避免测试作者漏传期望值时用例静默通过。

**练习 2**：UT 里的 `InfershapeContextPara` 是伪造的 context，被测的推导函数是真实注册的那个吗？

**答案**：是。faker 只伪造了"框架传参"这一层（构造 `InferShapeContext`），随后 `ExecuteTestCase` 按算子名从注册表取出 `IMPL_OP_INFERSHAPE` 注册的真实 `InferShapeAddExample` 执行，因此 UT 能真实覆盖业务推导逻辑。

**练习 3**：infershape UT 与 kernel UT（u7-l2 将讲）对环境的依赖有何不同？

**答案**：infershape 是纯 Host 侧 C++ 逻辑，UT 不需要 NPU，任何能编译运行 gtest 的环境即可；kernel UT 涉及 Ascend C 代码的仿真执行，依赖 CANN 仿真组件（安装 `tests/requirements.txt` 中的依赖），环境要求更高。

## 5. 综合实践

**任务：给 AddExample 建立一份"shape 推导行为说明书"并用 UT 固化。**

1. 通读 [add_example_infershape.cpp](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_infershape.cpp)，确认当前推导策略是"输出 = 输入 0 的 shape 逐维复制，dtype 同输入 0"。
2. 在 [test_add_example_infershape.cpp](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/tests/ut/op_host/test_add_example_infershape.cpp) 中补充三组用例，覆盖三类典型输入：
   - 动态 shape（已有用例，含 `-1`）；
   - 4 维静态 shape（本讲 4.4.4 的用例）；
   - 标量 0 维 shape（本讲 4.4.4 的用例）。
3. 运行 `bash build.sh -u --ophost --ops=add_example`，确认全部通过；再故意改错一个期望值，确认能失败。
4. 写一份简短笔记回答：如果把 AddExample 升级为支持广播（对照 4.3.3 的 `BroadcastShape` 用法），infershape、tiling、kernel 三处各需要动什么？只需列清单，不必实现。

这个任务把"读推导逻辑 → 写测试固化行为 → 思考扩展影响面"串成闭环，也是后续 tiling（u4）、kernel（u5）、完整测试体系（u7）学习的入口。

## 6. 本讲小结

- `*_infershape.cpp` 是 op_host 的 shape/dtype 推导交付件，运行在 Host 侧，在 aclnn 第一段或图编译阶段被框架回调。
- 注册入口是 `IMPL_OP_INFERSHAPE(算子名).InferShape(...).InferDataType(...)`，算子名必须与 `OP_ADD` 严格一致。
- AddExample 的推导是"输入 shape 逐维复制"：先 `SetDimNum` 再逐维 `SetDim`，`-1` 动态维度原样透传；空指针防御用 `OP_CHECK_NULL_WITH_CONTEXT`。
- 广播规则三条：左侧补 1 对齐维度数、维度为 1 则拉伸、不兼容则报错；生产算子用 `BroadcastShape` 工具函数，失败路径 `OP_LOGE + GRAPH_FAILED`；特定 dtype 下还有"合并维度 < 6"的限制。
- infershape UT 用 `InfershapeContextPara` 填场景、`ExecuteTestCase(para, 期望码, 期望shape)` 执行断言；新用例文件放入 `tests/ut/op_host/` 即自动编入，`bash build.sh -u --ophost --ops=add_example` 运行。
- 标量（0 维）shape 是边界场景：AddExample 的复制策略输出仍为 `()`，而 `BroadcastShape` 体系会归一为 `(1,)`，读生产代码时要注意这类差异。

## 7. 下一步学习建议

- 下一讲（u3-l3）转向算子开发的公共背景：数据类型、数据格式（ND/NZ/FRAC）与非连续 Tensor，这些决定了 infershape 之外 `*_def.cpp` 中 DataType/Format 声明的含义。
- 顺带阅读：在 [docs/zh/op_list.md](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/op_list.md) 里挑一个支持广播的双输入算子（如 optim 下的 lamb 系列），对照其 `*_infershape.cpp` 验证本讲的广播推导理解。
- u4 单元将进入 tiling：shape 确定后，Host 侧如何把总元素量切分给多个 AI Core——infershape 的输出正是 tiling 的输入之一，两讲合起来构成完整的 Host 侧推导链。
