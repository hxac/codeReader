# 核心数据类型：Tensor、VariantPack 与 SVector

## 1. 本讲目标

通过本讲，你将掌握 ATB（Ascend Transformer Boost）中最基础、但贯穿整个项目的数据结构。具体目标是：

- 理解 `atb::Dims`、`atb::TensorDesc`、`atb::Tensor` 三者如何由内而外描述一个张量：先描述形状，再描述数据类型与排布，最后挂载真实内存。
- 掌握 `atb::VariantPack` 如何把一批输入张量和输出张量"打包"交给算子执行。
- 看懂 `atb::Status` 与 `atb::ErrorType` 错误码体系，能在出错时根据返回值定位问题类型。
- 理解 `atb::SVector` 这个 ATB 自研容器的"栈优先、溢出转堆"设计，明白它为何能频繁出现在前面所有结构里。

学完本讲后，你将能独立读懂任意一段 ATB 调用算子的代码中"数据是怎么组织的"。

## 2. 前置知识

在阅读本讲前，建议你已经建立以下认知（来自前序讲义）：

- **ATB 的定位**：它是位于深度学习框架与 CANN/昇腾 NPU 之间的加速库，把 Transformer 中的高频结构做成融合算子（详见 u1-l1）。
- **Host 与 Device**：Host 指 CPU（负责下发任务），Device 指 NPU（负责真正计算）。一个张量的数据可能存放在 Host 内存，也可能存放在 Device 内存。
- **aclDataType / aclFormat**：这两个类型来自 CANN 的 `acl/acl.h`，分别表示张量的数据类型（如 `ACL_FLOAT`、`ACL_FLOAT16`、`ACL_BF16`）和数据排布格式（如 `ACL_FORMAT_ND` 表示普通多维、`ACL_FORMAT_FRACTAL_NZ` 表示昇腾特有的分块排布）。本讲不展开它们的取值表，只需知道它们是"枚举值"即可。

如果你对"形状（shape）"、"维度（dim）"、"数据类型（dtype）"这些深度学习基础术语完全陌生，建议先补充相关知识再继续。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [include/atb/types.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/types.h) | 定义 `Dims`、`TensorDesc`、`Tensor`、`VariantPack`、`Status`、`ErrorType`、`Node`、`GraphParam` 等核心数据类型。本讲的主战场。 |
| [include/atb/svector.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/svector.h) | 定义 ATB 自研容器模板 `SVector<T>`，以及 `MAX_SVECTOR_SIZE`、`DEFAULT_SVECTOR_SIZE` 等常量。 |
| [example/op_demo/demo_util.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/demo_util.h) | 提供 `CreateTensor` 等辅助函数，展示如何手工构造一个 `atb::Tensor`。是本讲"实践依据"的来源。 |
| [example/op_demo/faupdate/faupdate_demo.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/faupdate/faupdate_demo.cpp) | faupdate 算子的 C++ 调用 demo，展示如何组装一个完整的 `VariantPack`。 |

> 提示：`types.h` 内部 `#include "atb/svector.h"`，所以这两个文件是一个整体：`SVector` 是底层容器，`types.h` 的多数结构都建立在它之上。

---

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

1. **4.1 Dims、TensorDesc 与 Tensor**：从形状到内存的层层包装。
2. **4.2 VariantPack**：算子输入输出的"集装箱"。
3. **4.3 Status 与 ErrorType**：错误码体系。
4. **4.4 SVector**：贯穿以上所有结构的自研容器。

---

### 4.1 Dims、TensorDesc 与 Tensor

#### 4.1.1 概念说明

在 ATB 里，"一个张量"被拆成三层来描述，从内到外分别是：

- **Dims**：纯粹的"形状"信息，只回答"每一维多大、一共几维"。比如一个 `[8, 16384]` 的二维张量。
- **TensorDesc**（张量描述符）：在 `Dims` 基础上，再补上"数据类型"（dtype）和"数据排布格式"（format）。它完整刻画了"这个张量里的数是什么形态"，但**不含任何真实数据**。
- **Tensor**（张量）：在 `TensorDesc` 基础上，再挂上"真实内存地址"（Device 内存、Host 内存）和"内存大小"，成为一个可以被算子读写的实体。

为什么要拆三层？因为 ATB 的算子执行是**两段式**的（详见 u1-l6）：先做形状推导（InferShape），此时只需要 `TensorDesc`；真正执行时才需要内存地址。把"描述"和"数据"分离，就能在不碰真实数据的前提下完成大量检查与规划工作。

#### 4.1.2 核心流程

构造一个张量的典型流程：

```text
1. 确定 dtype（如 ACL_FLOAT）、format（如 ACL_FORMAT_ND）、shape（如 [8, 16384]）
2. 把 shape 填进 Dims：dimNum=2, dims[0]=8, dims[1]=16384
3. 组装 TensorDesc{dtype, format, shape}
4. 根据 shape 和 dtype 计算需要多少字节
5. 在 Device 上申请内存，把地址写进 Tensor.deviceData
6. 把字节数写进 Tensor.dataSize
```

其中第 4 步的字节数计算可以用公式表达。设形状为 \(d_0 \times d_1 \times \cdots \times d_{n-1}\)，每个元素占 \(b\) 字节，则：

\[
\text{dataSize} = b \times \prod_{i=0}^{n-1} d_i
\]

例如 `ACL_FLOAT`（32 位浮点，\(b=4\)）形状为 \([8, 16384]\)，则 \(\text{dataSize} = 4 \times 8 \times 16384 = 524288\) 字节。

#### 4.1.3 源码精读

先看最内层的 `Dims`。它是一个固定大小的数组加一个计数器：

[include/atb/types.h:84-94](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/types.h#L84-L94) 定义了 `Dims`。其中 `dims` 是一个长度为 `MAX_DIM`（即 8）的定长数组，存放每一维的大小；`dimNum` 记录实际维数，取值范围是 \((0, 8]\)（大于 0、不超过 8）。也就是说 ATB 的张量**最多 8 维**，这是由 [include/atb/types.h:30-31](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/types.h#L30-L31) 的 `MAX_DIM` 决定的。

再看 `TensorDesc`，它在 `Dims` 基础上加两个字段：

[include/atb/types.h:96-110](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/types.h#L96-L110) 定义了 `TensorDesc`，包含 `dtype`（数据类型，默认 `ACL_DT_UNDEFINED`）、`format`（排布格式，默认 `ACL_FORMAT_UNDEFINED`）和 `shape`（一个 `Dims`）。注意文件中有一条重要警告：**Atlas 推理系列产品不支持 `ACL_BF16`（bf16）类型数据**。

最外层的 `Tensor` 挂载真实内存：

[include/atb/types.h:112-127](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/types.h#L112-L127) 定义了 `Tensor`，关键字段是：

- `desc`：类型为 `TensorDesc`，承载前面说的形状/类型/排布信息。
- `deviceData`：`void *`，指向 NPU（Device）内存地址，默认 `nullptr`。
- `hostData`：`void *`，指向 CPU（Host）内存地址，默认 `nullptr`。
- `dataSize`：`uint64_t`，表示 `deviceData` 或 `hostData` 指向内容的字节数，默认 0。

一个 `Tensor` 可以同时持有 Device 地址和 Host 地址（某些场景下需要两者），但真正参与算子执行的主要是 `deviceData`。

来看真实代码如何手工构造一个 `Tensor`。demo 的辅助函数 `CreateTensor` 是最佳样例：

[example/op_demo/demo_util.h:64-77](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/demo_util.h#L64-L77) 展示了完整过程：先把 `dtype`、`format`、`shape.dimNum`、`shape.dims[i]` 逐个填进 `tensor.desc`；再用 `atb::Utils::GetTensorSize(tensor)` 计算字节数赋给 `tensor.dataSize`；最后调用 CANN 的 `aclrtMalloc` 在 Device 上申请内存并把地址写入 `tensor.deviceData`。这段代码就是"4.1.2 核心流程"的落地实现。

#### 4.1.4 代码实践

**实践目标**：通过阅读 `CreateTensor`，亲手写一段构造 `Tensor` 描述的伪代码，并算出 dataSize。

**操作步骤**：

1. 打开 [include/atb/types.h:112-127](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/types.h#L112-L127) 确认 `Tensor` 的字段。
2. 打开 [example/op_demo/demo_util.h:64-77](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/demo_util.h#L64-L77) 对照真实写法。
3. 在纸上或注释里写出如下伪代码（**示例代码**，非项目原有代码）：

```cpp
// 示例代码：手工描述一个 shape=[8,16384] 的 float 张量
atb::Tensor t;
t.desc.dtype  = ACL_FLOAT;            // 32位浮点
t.desc.format = ACL_FORMAT_ND;        // 普通多维排布
t.desc.shape.dimNum = 2;              // 2 维
t.desc.shape.dims[0] = 8;
t.desc.shape.dims[1] = 16384;
// dataSize 由框架工具计算（等价于 4 * 8 * 16384 = 524288）
t.dataSize = atb::Utils::GetTensorSize(t);
// deviceData 需后续用 aclrtMalloc 申请，这里暂置空
```

**需要观察的现象**：每个字段赋值前后，`t` 的状态从"全默认值"变为"一个有意义的张量描述"。

**预期结果**：你能用一句话说明 `desc` 描述的是"长相"、`deviceData` 是"真实数据地址"、`dataSize` 是"字节数"。

> 说明：本步骤只构造描述信息，**不**真正调用 `aclrtMalloc`，因此可在任意能编译 ATB 头文件的环境验证描述字段，但完整运行需要 NPU 设备——若无可运行环境，标注"待本地验证"。

#### 4.1.5 小练习与答案

**练习 1**：一个形状为 `[2, 4, 8]`、数据类型为 `ACL_FLOAT16`（每个元素 2 字节）的张量，它的 `dataSize` 是多少字节？

**参考答案**：\(2 \times 2 \times 4 \times 8 = 128\) 字节。

**练习 2**：`Dims` 中 `dimNum` 的取值范围是什么？为什么有上限？

**参考答案**：取值范围是 \((0, 8]\)，即 1 到 8。上限由 [include/atb/types.h:30-31](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/types.h#L30-L31) 的 `MAX_DIM = 8` 决定，因为 `dims` 是定长数组 `int64_t dims[MAX_DIM]`，超出 8 维无处存放。

---

### 4.2 VariantPack

#### 4.2.1 概念说明

一个算子通常有多个输入张量和多个输出张量。ATB 用一个结构体把它们统一打包，这个结构体就是 `VariantPack`（变体包/参数包）。

你可以把它想象成一个"集装箱"：

- `inTensors`：装着所有**输入**张量的格子。
- `outTensors`：装着所有**输出**张量的格子。

每个格子本身是一个 `SVector<Tensor>`（即一组 `Tensor`）。算子执行时，框架就是从这个集装箱里取出输入、把结果写回输出。

#### 4.2.2 核心流程

使用 `VariantPack` 的典型流程：

```text
1. 声明 atb::VariantPack variantPack;
2. 准备 N 个输入 Tensor，放进 variantPack.inTensors
3. 准备 M 个输出 Tensor（至少要把 desc 填好，地址可先空），放进 variantPack.outTensors
4. 把 variantPack 传给 op->Setup(...) 做校验与形状推导
5. 把同一个 variantPack 传给 op->Execute(...) 真正计算
```

关键点：**输入输出顺序必须与算子定义一致**。例如某算子要求输入顺序是 `[query, key, value]`，那么 `inTensors[0]` 必须是 query，依此类推。顺序错乱会导致校验失败或计算错误。

#### 4.2.3 源码精读

`VariantPack` 的定义非常精简：

[include/atb/types.h:129-141](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/types.h#L129-L141) 定义了 `VariantPack`，只有两个字段：`inTensors` 和 `outTensors`，都是 `SVector<Tensor>`。这里就能看到 `SVector` 的身影——它就是 4.4 节要讲的自研容器。

来看 faupdate demo 是如何真实组装一个 `VariantPack` 的：

[example/op_demo/faupdate/faupdate_demo.cpp:72-78](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/faupdate/faupdate_demo.cpp#L72-L78) 展示了完整组装过程：

- 第 73 行声明 `atb::VariantPack variantPack;`。
- 第 74 行调用 `PrepareInTensor(...)`，它内部构造了两个输入 `Tensor`（`lse`、`localout`），通过 `inTensors = {lse, localout};`（初始化列表赋值）放入输入。
- 第 76-78 行构造一个输出 `Tensor output`，再用 `outTensors = {output};` 放入输出。

`{lse, localout}` 这种写法能直接赋值给 `SVector<Tensor>`，依赖的正是 4.4 节将讲的 `operator=(std::initializer_list<T>)`。

#### 4.2.4 代码实践

**实践目标**：读懂 faupdate demo 的"输入输出装填"环节，并模仿写出装填两个输入、一个输出的伪代码。

**操作步骤**：

1. 阅读 [example/op_demo/faupdate/faupdate_demo.cpp:72-78](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/faupdate/faupdate_demo.cpp#L72-L78)。
2. 跟进 `PrepareInTensor`，看它如何构造 `lse`、`localout`（[faupdate_demo.cpp:28-41](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/faupdate/faupdate_demo.cpp#L28-L41)）。
3. 写出（**示例代码**）：

```cpp
// 示例代码：组装一个含 2 输入、1 输出的 VariantPack
atb::VariantPack vp;
vp.inTensors  = {inputA, inputB};   // 顺序 = 算子定义的输入顺序
vp.outTensors = {outputTensor};     // 顺序 = 算子定义的输出顺序
```

**需要观察的现象**：`inTensors.size()` 变为 2、`outTensors.size()` 变为 1。

**预期结果**：能说清楚"输入输出顺序必须与算子定义一致，否则 Setup 会报错"。

> 若无 NPU 环境，无法真正跑通 Setup/Execute，标注"待本地验证"。

#### 4.2.5 小练习与答案

**练习 1**：`VariantPack` 的 `inTensors` 和 `outTensors` 是什么类型？

**参考答案**：都是 `atb::SVector<atb::Tensor>`（见 [types.h:129-141](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/types.h#L129-L141)）。

**练习 2**：为什么强调输入输出张量的顺序要与算子定义一致？

**参考答案**：因为框架按下标取张量（如 `inTensors[0]` 当作第一个输入）。顺序错乱会让算子读到错误的张量，轻则 `Setup` 校验失败（返回 `ERROR_INVALID_TENSOR_NUM` 等错误码），重则形状碰巧匹配却算出错误结果。

---

### 4.3 Status 与 ErrorType

#### 4.3.1 概念说明

ATB 几乎所有接口都返回一个状态码，告诉你"成功了没、失败是哪一类失败"。这个状态码的类型就是 `Status`，失败的具体类别由 `ErrorType` 枚举列出。

- `Status`：本质就是 `int32_t`，`0` 表示成功（`NO_ERROR`），非 `0` 表示某种错误。
- `ErrorType`：一个枚举，列出约 25 种常见错误类别，覆盖参数、图、运行时、内存、通信等。

掌握这套错误码，是后续调试（详见 u7-l2 日志与性能）的基础。

#### 4.3.2 核心流程

错误码处理的典型流程：

```text
1. 调用某接口，拿到 Status s
2. 若 s == NO_ERROR(0)：成功，继续
3. 若 s != 0：根据 s 的值，对照 ErrorType 枚举判断错误类别
   - 例如 ERROR_INVALID_TENSOR_DTYPE 表示数据类型不对
   - ERROR_OUT_OF_DEVICE_MEMORY 表示 Device 显存不足
4. 在日志中打印 s，定位问题
```

demo 里用一个宏 `CHECK_STATUS` 统一处理：返回值非 0 时打印错误码与文档链接，再向上返回。详见 [example/op_demo/demo_util.h:30-41](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/demo_util.h#L30-L41)。

#### 4.3.3 源码精读

`Status` 的定义极其简单：

[include/atb/types.h:28-29](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/types.h#L28-L29) 写明 `using Status = int32_t;`。所以你完全可以把它当一个普通整数来比较、打印。

`ErrorType` 是一个普通的 `enum : int`，从 0 开始递增：

[include/atb/types.h:38-69](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/types.h#L38-L69) 列出了全部错误类别。挑几个最常用的理解：

- `NO_ERROR = 0`：正确。
- `ERROR_INVALID_PARAM`：无效参数（排第 1 个非零错误）。
- `ERROR_INVALID_TENSOR_DTYPE` / `ERROR_INVALID_TENSOR_FORMAT` / `ERROR_INVALID_TENSOR_DIM` / `ERROR_INVALID_TENSOR_SIZE`：Tensor 的类型/格式/维度/大小不对——这四个是和本讲 4.1 直接相关的校验错误。
- `ERROR_INVALID_IN_TENSOR_NUM`：算子输入 Tensor 数量与定义不一致——和本讲 4.2 直接相关。
- `ERROR_OUT_OF_DEVICE_MEMORY` / `ERROR_OUT_OF_HOST_MEMORY`：Device/Host 内存不足。
- `ERROR_CANN_ERROR`：调用 CANN 接口出错。
- `ERROR_HCCL_FAIL`：HCCL 通信接口调用失败（通信算子相关，详见 u5-l1）。

注意：因为 `ErrorType` 是 `enum : int`（不是 `enum class`），它的成员可以隐式转成 `int`，所以函数里直接 `return atb::ErrorType::NO_ERROR;` 就能作为 `Status` 返回（demo 里大量这样用，如 [faupdate_demo.cpp:40](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/faupdate/faupdate_demo.cpp#L40)）。

同文件还有一个 `LogLevel` 枚举（注意它是 `enum class`，用法不同），用于日志级别，本讲只作了解：

[include/atb/types.h:76-82](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/types.h#L76-L82) 定义了 `DEBUG / INFO / WARN / ERROR / NONE` 五个级别，是 u7-l2 日志讲义的前置。

#### 4.3.4 代码实践

**实践目标**：写一个最小错误处理片段，模拟"构造一个 dtype 不对的 Tensor 描述，观察错误码类别"。

**操作步骤**：

1. 阅读 [include/atb/types.h:38-69](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/types.h#L38-L69)，记下与你构造 Tensor 相关的几个错误码。
2. 阅读 demo 的 `CHECK_STATUS` 宏 [demo_util.h:30-41](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/demo_util.h#L30-L41)。
3. 写出（**示例代码**）：

```cpp
// 示例代码：用 CHECK_STATUS 风格处理返回值
atb::Status s = someOp->Setup(variantPack, workspaceSize, context);
if (s != atb::NO_ERROR) {
    std::cout << "Setup 失败，错误码: " << s << std::endl;
    // 若 s 对应 ERROR_INVALID_TENSOR_DTYPE，说明是 dtype 没设对
}
```

**需要观察的现象**：故意把某个输入 `Tensor.desc.dtype` 留成默认值 `ACL_DT_UNDEFINED`，调用 Setup 时大概率返回 `ERROR_INVALID_TENSOR_DTYPE`。

**预期结果**：能根据返回的数字，对照 `ErrorType` 枚举说出"这是哪一类错误"。

> 完整复现需要 NPU 环境（Setup 会在 Device 上做校验）。无环境时标注"待本地验证"。

#### 4.3.5 小练习与答案

**练习 1**：`Status` 的底层类型是什么？什么值表示成功？

**参考答案**：`Status` 是 `int32_t`（见 [types.h:28-29](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/types.h#L28-L29)），`0`（即 `NO_ERROR`）表示成功。

**练习 2**：如果调用算子时输入张量数量给少了，最可能命中哪个错误码？

**参考答案**：`ERROR_INVALID_IN_TENSOR_NUM`（算子输入 Tensor 数量与定义不一致）。

**练习 3**：`ErrorType` 是 `enum : int`，`LogLevel` 是 `enum class`，二者在使用上有什么差别？

**参考答案**：`ErrorType` 可隐式转 `int`，故能直接当 `Status` 返回、当整数比较；`LogLevel` 是强类型枚举，必须写 `LogLevel::INFO` 这样的完整限定，不能与 `int` 隐式转换。

---

### 4.4 SVector

#### 4.4.1 概念说明

`SVector` 是 ATB 自研的动态数组容器模板 `SVector<T>`，地位上类似 `std::vector<T>`。你已经在前面见过它很多次了：`VariantPack` 里的 `SVector<Tensor>`、`Node` 里的 `SVector<uint32_t>` 等等。

为什么不用 `std::vector` 而要自研？核心原因是**性能与确定性**：

- `std::vector` 一创建就在堆上分配内存，频繁构造/销毁会带来 `malloc/free` 开销。
- ATB 在推理热路径上会频繁创建小型容器（如临时装几个 `Tensor`）。`SVector` 采用**"栈优先、溢出转堆"**的小缓冲优化（Small Buffer Optimization, SBO）：少量元素直接放在对象内部的栈数组里，不触发任何堆分配；元素较多时才升级到堆内存。

#### 4.4.2 核心流程

`SVector` 的存储状态机可以概括为两种模式：

```text
[栈模式]                            [堆模式]
storage_[65] 定长数组   --溢出-->    heap_ 指针指向 malloc 的内存
capacity_ = 0                        capacity_ = 256
最多容纳 64 个有效元素                最多容纳 256 个元素
```

关键阈值来自 [include/atb/svector.h:26-33](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/svector.h#L26-L33)：

- `DEFAULT_SVECTOR_SIZE = 64`：栈模式容量，元素数 ≤ 64 时走栈。
- `MAX_SVECTOR_SIZE = 256`：硬上限，无论如何都不能超过 256 个元素。
- `CHECK_BOUND = true`：默认开启边界检查（越界会抛异常）。

从栈升级到堆的时机：当 `push_back` / `insert` 让元素数超过 64 时，调用内部的 `MoveToHeap()`，把栈数组搬到一个 `malloc` 出来的 256 长度堆数组里，后续都在堆上操作。

#### 4.4.3 源码精读

先看类声明与三个常量：

[include/atb/svector.h:36-43](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/svector.h#L36-L43) 是 `SVector` 模板类声明，注释说明它"封装动态数组的顺序容器"。

默认构造函数揭示栈模式的初始状态：

[include/atb/svector.h:48-53](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/svector.h#L48-L53) 把 `size_` 设为 0，并把栈数组 `storage_` 的前 `DEFAULT_SVECTOR_SIZE` 个元素清零。此刻 `heap_` 仍为 `nullptr`，处于栈模式。

`push_back` 是触发"栈→堆"升级的关键函数：

[include/atb/svector.h:149-166](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/svector.h#L149-L166) 展示了三段逻辑：若已在堆模式，检查是否到 `capacity_` 上限；若在栈模式且已满 64 个，调用 `MoveToHeap()` 升级；否则直接写入栈数组。

升级动作本身在私有方法 `MoveToHeap`：

[include/atb/svector.h:591-601](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/svector.h#L591-L601) `malloc` 一块 `MAX_SVECTOR_SIZE`（256）长度的堆内存，把原栈数组里的 64 个元素逐一搬过去，并把 `capacity_` 置为 256。这就是"溢出转堆"的实现。

私有成员揭示了双存储布局：

[include/atb/svector.h:585-589](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/svector.h#L585-L589) 包含：`capacity_`（当前容量，栈模式为 0）、`size_`（当前元素数）、`storage_[DEFAULT_SVECTOR_SIZE + 1]`（长度 65 的栈数组，+1 是留作边界缓冲）、`heap_`（堆指针，非空表示处于堆模式）。注意 `storage_` 长度是 65 而容量是 64——这是常见的 SBO 安全余量写法。

访问元素带边界检查：

[include/atb/svector.h:222-234](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/svector.h#L222-L234) 的 `operator[]` 在下标越界时抛 `std::out_of_range`。`at()` 的实现完全一致（见同文件 [262-274](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/svector.h#L262-L274)）。因为 `CHECK_BOUND` 默认为 `true`，所以 ATB 容器的访问是"安全但略慢"的。

`reserve` 用于预分配（性能技巧）：

[include/atb/svector.h:401-420](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/svector.h#L401-L420) 在已知最终元素数时一次性 `malloc` 好堆内存，避免后续多次升级。注意它会**清空内部数据**，只调整容量。

#### 4.4.4 代码实践

**实践目标**：用 `SVector` 的初始化列表赋值（正是 `VariantPack` 装填时用的写法），直观感受它的用法。

**操作步骤**：

1. 阅读 [include/atb/svector.h:518-547](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/svector.h#L518-L547) 的 `operator=(std::initializer_list<T>)`，确认 `{...}` 赋值是合法的。
2. 回看 faupdate demo 的 `inTensors = {lse, localout};`（[faupdate_demo.cpp:39](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/faupdate/faupdate_demo.cpp#L39)），确认它就是调用这个 `operator=`。
3. 写出（**示例代码**）：

```cpp
// 示例代码：体验 SVector 的初始化列表赋值与边界检查
atb::SVector<int> sv;
sv = {10, 20, 30};        // 调用 operator=(initializer_list)
std::cout << sv.size();   // 打印 3
std::cout << sv.at(0);    // 打印 10
// sv.at(5);              // 越界，会抛 std::out_of_range
```

**需要观察的现象**：`size()` 随赋值变为 3；`at(0)` 取到第一个元素；越界访问抛异常。

**预期结果**：能说清楚"用 `{...}` 给 `SVector` 赋值时，元素少走栈、元素多（>64）走堆，但都对用户透明"。

> 这段代码只需 ATB 头文件即可编译验证（不涉及 Device），可在装了 CANN 开发包的 Host 上本地验证。

#### 4.4.5 小练习与答案

**练习 1**：一个 `SVector` 默认能放多少个元素而不触发堆分配？硬上限是多少？

**参考答案**：默认可放 64 个（`DEFAULT_SVECTOR_SIZE`）而不 `malloc`；硬上限 256 个（`MAX_SVECTOR_SIZE`），超过会抛 `MaxSizeExceeded`。

**练习 2**：`SVector` 的 `operator[]` 在越界时会怎样？为什么？

**参考答案**：会抛 `std::out_of_range` 异常。因为常量 `CHECK_BOUND` 默认为 `true`，访问函数内部对 `i >= size_` 做了检查（见 [svector.h:222-234](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/svector.h#L222-L234)）。

**练习 3**：为什么 `VariantPack` 用 `SVector<Tensor>` 而不是 `std::vector<Tensor>`？

**参考答案**：因为推理热路径频繁构造小容器，`SVector` 的栈优先策略能避免绝大多数 `malloc/free`，性能与确定性更好；同时它对元素数量有明确上限（256），便于框架做容量规划。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个综合任务（对应本讲的 `practice_task`）。

**任务**：参考 faupdate demo 与 `CreateTensor` 辅助函数，编写一段完整的伪代码，构造一个"包含 1 个输入 Tensor、1 个输出 Tensor 的 `VariantPack`"，并对每个关键字段写一句中文注释说明含义；最后写一个带错误处理的 Setup 调用。

**建议步骤**：

1. 先复习 [example/op_demo/demo_util.h:64-77](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/demo_util.h#L64-L77) 的 `CreateTensor`（怎么填 `desc`、算 `dataSize`、申请 `deviceData`）。
2. 再复习 [example/op_demo/faupdate/faupdate_demo.cpp:72-78](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/faupdate/faupdate_demo.cpp#L72-L78) 的 `VariantPack` 组装。
3. 自己写出如下结构的伪代码（**示例代码**）：

```cpp
// 示例代码：综合实践——构造输入 Tensor 描述 + VariantPack
atb::Tensor inTensor;
inTensor.desc.dtype  = ACL_FLOAT;          // 数据类型：32位浮点
inTensor.desc.format = ACL_FORMAT_ND;      // 排布：普通多维
inTensor.desc.shape.dimNum = 2;            // 2 维
inTensor.desc.shape.dims[0] = 8;           // 第 0 维大小
inTensor.desc.shape.dims[1] = 16384;       // 第 1 维大小
inTensor.dataSize = atb::Utils::GetTensorSize(inTensor); // 字节数
// inTensor.deviceData 需用 aclrtMalloc 申请（此处略）

atb::Tensor outTensor;                     // 输出至少把 desc 填好
outTensor.desc.dtype  = ACL_FLOAT;
outTensor.desc.format = ACL_FORMAT_ND;
outTensor.desc.shape.dimNum = 2;
outTensor.desc.shape.dims[0] = 16384;
outTensor.desc.shape.dims[1] = 128;

atb::VariantPack variantPack;
variantPack.inTensors  = {inTensor};       // 装输入（SVector 初始化列表赋值）
variantPack.outTensors = {outTensor};      // 装输出

// 带 Status / ErrorType 的错误处理
uint64_t workspaceSize = 0;
atb::Status s = op->Setup(variantPack, workspaceSize, context);
if (s != atb::ErrorType::NO_ERROR) {
    std::cout << "Setup 失败，错误码: " << s << std::endl; // 对照 ErrorType 枚举排查
    return s;
}
```

**自检清单**：

- [ ] 能指出 `desc`、`deviceData`、`hostData`、`dataSize` 各自含义。
- [ ] 能说清 `inTensors` / `outTensors` 是 `SVector<Tensor>` 且顺序需与算子定义一致。
- [ ] 能解释 `Status == 0` 为成功，非 0 对照 `ErrorType` 查类别。
- [ ] 能说清 `SVector` 用 `{...}` 赋值、少则走栈多则走堆。

> 真正跑通 Setup/Execute 需要 NPU 设备与已初始化的 Context（见 u1-l5）。无设备时，本任务作为"源码阅读 + 伪代码练习"完成即可，运行部分标注"待本地验证"。

---

## 6. 本讲小结

- ATB 用 **三层结构**描述一个张量：`Dims`（形状）→ `TensorDesc`（加 dtype/format）→ `Tensor`（加 deviceData/hostData/dataSize）。
- `Tensor` 把"描述"和"真实内存"绑定在一起，但描述可以独立用于形状推导，这是两段式执行（Setup→Execute）的基础。
- `VariantPack` 是算子的"输入输出集装箱"，`inTensors` 和 `outTensors` 都是 `SVector<Tensor>`，顺序必须与算子定义一致。
- 所有接口返回 `Status`（即 `int32_t`），`0` 为成功；失败类别见 `ErrorType` 枚举，其中 `ERROR_INVALID_TENSOR_*` 系列与本讲数据结构直接相关。
- `SVector<T>` 是 ATB 自研的栈优先、溢出转堆容器，默认走栈（容量 64）、超过才 `malloc`（上限 256），用于推理热路径的零开销小容器。
- 这些类型是后续所有讲义的"通用词汇"：无论是 Operation 接口（u1-l6）、Context 资源池（u3-l5），还是图算子的 `Node`/`GraphParam`（u5-l2），都建立在本讲的数据类型之上。

## 7. 下一步学习建议

- **下一讲 u1-l5（Context 上下文与执行流管理）**：本讲只构造了"数据"，但没有"运行环境"。下一讲讲解 `Context` 如何管理全局资源与执行流，是把本讲 `Tensor`/`VariantPack` 真正送进算子执行的前提。
- **u1-l6（Operation 接口与单算子执行流程）**：把本讲的 `VariantPack` 与 `Setup`/`Execute` 接口完整串起来，是本讲 4.2/4.3 的自然延伸。
- **延伸阅读源码**：在 [include/atb/types.h:163-207](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/types.h#L163-L207) 中提前浏览 `Node` 与 `GraphParam`——它们用到了本讲的 `SVector`、`Chunk`、`ReshapeFunc`，能为 u5-l2 图算子原理打下印象。
