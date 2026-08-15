# 数据类型、格式与公共上下文概念

## 1. 本讲目标

读完本讲，你应该能够：

1. 说出 aclTensor 的数据类型（dtype）体系，看懂文档中 `FLOAT16`、`BF16`、`FLOAT8_E4M3FN` 等简写对应的真实含义。
2. 说出数据格式（format）的含义，区分 ND 等常见格式与 `FRACTAL_NZ`、`FRACTAL_Z` 等 NPU 私有格式，并理解「Tensor 维度必须与 format 一致」的校验规则。
3. 理解什么是非连续 Tensor（shape/strides/offset 三元组），以及算子定义中 `AutoContiguous()` 声明式连续化的作用。
4. 掌握 aclnn 调用的常见返回码，遇到 161001、561003 这类错误时知道去哪查。
5. 能独立从两个算子的 `*_def.cpp` 中提取「类型-格式支持矩阵」并对比差异——这是阅读任何 ops-nn 算子的第一课。

本讲是 u3-l1（算子原型定义）的「背景知识补全篇」：u3-l1 讲的是 `Input().DataType().Format()` 这条声明链怎么写，本讲讲的是这条链上每个值**到底是什么意思**。

## 2. 前置知识

本讲不需要新的编程技能，但需要以下已经建立的概念（来自前面的讲义）：

- **Host 与 Device**：算子的定义、shape 推导、tiling 跑在 Host 侧（CPU）；真正的计算跑在 Device 侧（NPU 的 AI Core）。
- **aclTensor**：aclnn 两段式接口中描述一个 Tensor 的「描述符」，包含 dtype、format、shape、strides、offset 等信息（见 u2-l1）。
- **def 文件**：`op_host/*_def.cpp` 用 `OpDef` 基类声明算子的输入输出规格，其中 `DataType({...})` 与 `Format({...})` 两个列表按下标一一配对成「候选槽位」（见 u3-l1）。
- **AI Core 矢量指令与 Cube 单元**：矢量算子逐元素处理数据；Cube 单元做矩阵乘，对数据在内存中的分块排布有特殊要求（这点会在讲 format 时展开）。

一个通俗类比：如果把一个 Tensor 比作一箱货物，那么 **dtype 是「每个包裹里装的是什么」（float 还是 int，占几个字节）**，**format 是「包裹在箱子里的码放方式」（按什么顺序、按什么轴语义摆放）**，**strides/offset 是「这箱货是不是完整连续地码满，还是稀稀拉拉地挑着放」**。算子的 def 文件就是提前声明「我收货时接受哪些包裹类型和码放方式」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [docs/zh/context/data_type.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/context/data_type.md) | 数据类型简写对照表：文档里的 `FLOAT16` 等简写与 `ACL_FLOAT16` 等真实类型的映射 |
| [docs/zh/context/data_format.md](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/context/data_format.md) | 数据格式说明：常见格式（ND/NCHW/NHWC 等）、维度一致性规则、私有格式（FRACTAL_NZ 等） |
| [docs/zh/context/non_contiguous_tensor.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/context/non_contiguous_tensor.md) | 非连续 Tensor 的定义：用 shape/strides/offset 三元组描述的「稀疏视图」 |
| [docs/zh/context/aclnn_return_code.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/context/aclnn_return_code.md) | aclnn 返回码表：参数错误、runtime 错误、内部异常三大类 |
| [examples/add_example/op_host/add_example_def.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_def.cpp) | 教学算子 AddExample 的定义文件：dtype/format 声明 + `AutoContiguous()` 实例 |
| [activation/gelu/op_host/gelu_def.cpp](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/gelu_def.cpp) | 生产算子 gelu 的定义文件：**不使用** `AutoContiguous`（改为在 aclnn 适配层处理）的对照样本 |
| [matmul/quant_batch_matmul_v4/op_host/quant_batch_matmul_v4_def.cpp](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/matmul/quant_batch_matmul_v4/op_host/quant_batch_matmul_v4_def.cpp) | 量化融合 matmul 的定义文件：大量 dtype 候选槽位（INT8/HIFLOAT8/FLOAT8_E4M3FN/FLOAT4_E2M1/INT4 等）的极端样本 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：数据类型、数据格式、非连续 Tensor 与 AutoContiguous、aclnn 返回码。

### 4.1 数据类型（DataType）：从 ACL_FLOAT 到 FLOAT4_E2M1

#### 4.1.1 概念说明

数据类型回答的问题是：**Tensor 中每个元素占多少字节、按什么编码解释**。这决定了：

- **内存开销与搬运效率**：fp32 是 4 字节，fp16/bf16 是 2 字节，fp8 是 1 字节。dtype 越窄，GM↔UB 的搬运量越小，这正是量化算子存在的意义。
- **精度**：fp32 > bf16/fp16 > fp8 > fp4/int4，逐级变窄。
- **kernel 模板分支**：u3-l1 与 u1-l4 已经见过——AddExample 的 tiling key 0 对应 float、1 对应 int32_t，每个 dtype 槽位在 binary json 中对应一份独立的预编译二进制。

文档（如 `op_list.md` 中每个算子的参数说明、各算子 README、`aclnn_gelu.h` 的头文件注释）在描述支持的 dtype 时不会写 `ACL_FLOAT16` 这样的全名，而是写简写 `FLOAT16`。两者之间是一张固定的对照表。

#### 4.1.2 核心流程

dtype 在算子调用链上的流转：

```text
用户构造 aclTensor（aclCreateTensor 指定 ACL_FLOAT16）
        │
        ▼
aclnn 第一段 GetWorkspaceSize：校验实际 dtype 是否落在 def 文件声明的候选列表里
        │   不在 → 返回 ACLNN_ERR_PARAM_INVALID (161002)
        ▼
根据 dtype 选定一个候选槽位 → 决定使用哪份预编译二进制 / tiling key 分支
        │
        ▼
Device 侧 kernel 按对应模板参数（如 half / int32_t）解释内存
```

命名规则速记（针对 fp8/fp4 这类新类型，命名来自浮点数的位段分配 `EmM`：E 位指数 + M 位尾数）：

| 简写 | 含义直觉 |
| --- | --- |
| FLOAT / FLOAT32 | 4 字节标准单精度 |
| FLOAT16 / BF16 | 2 字节半精度；BF16 牺牲尾数位数换取与 fp32 相同的指数范围，训练场景常用 |
| INT8 / INT4 | 量化整数，配合 scale 参数还原真实数值 |
| FLOAT8_E4M3FN / FLOAT8_E5M2 | 1 字节浮点：E4M3FN 精度略高（常用于权重/激活），E5M2 范围更大（常用于梯度类场景） |
| FLOAT4_E2M1 | 半字节（4 bit）浮点，最窄的浮点类型 |
| HIFLOAT8 | 华为定义的 1 字节浮点格式 |

> 上述「直觉」列是帮助记忆的背景说明；各类型的精确位段定义以 data_type.md 指向的《Runtime 运行时 API》文档为准。

#### 4.1.3 源码精读

data_type.md 用一张表完成了「简写 ↔ 真实类型」的全部映射，关键部分如下：

[docs/zh/context/data_type.md:L8-L36](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/context/data_type.md#L8-L36) —— 数据类型简写表：`ACL_FLOAT` 可简写为 `FLOAT` 或 `FLOAT32`，`ACL_FLOAT8_E4M3FN` 简写为 `FLOAT8_E4M3FN`，依次类推。注意简写**不区分大小写**。这张表是阅读一切算子参数文档的「密码本」。

而 def 文件里出现的却是第三种写法——`ge::` 命名空间下的枚举：

[examples/add_example/op_host/add_example_def.cpp:L44-L49](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_def.cpp#L44-L49) —— AddExample 输入 x1 的声明：`.DataType({ge::DT_FLOAT, ge::DT_INT32})` 表示 x1 支持 float 和 int32 两个候选槽位。三套写法的对应关系是：def 源码写 `ge::DT_FLOAT16`，aclnn 层的真实类型是 `ACL_FLOAT16`，文档简写是 `FLOAT16`。

再看一个 dtype 极端丰富的例子——量化融合 matmul：

[matmul/quant_batch_matmul_v4/op_host/quant_batch_matmul_v4_def.cpp:L25-L49](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/matmul/quant_batch_matmul_v4/op_host/quant_batch_matmul_v4_def.cpp#L25-L49) —— 输入 x1 的 DataType 列表长达 84 个候选槽位，覆盖 `DT_INT8`、`DT_HIFLOAT8`、`DT_FLOAT8_E4M3FN`、`DT_FLOAT8_E5M2`、`DT_FLOAT4_E2M1`、`DT_INT4` 等。注意：列表里允许重复项——同一个 `DT_FLOAT8_E4M3FN` 出现多次，因为每个槽位是与 Format 列表按下标配对的整体组合，重复的 dtype 可以搭配不同的上下文（不同槽位在运行时由不同的量化组合命中）。这正是 u3-l1 讲的「DataType 与 Format 两列表按下标一一配对成候选槽位」的体现。

#### 4.1.4 代码实践

1. **实践目标**：建立「三套 dtype 写法」的肌肉记忆。
2. **操作步骤**：
   - 打开 data_type.md 的简写表；
   - 打开 `activation/gelu/op_host/gelu_def.cpp` 第 25 行，看到 `.DataType({ge::DT_BF16, ge::DT_FLOAT16, ge::DT_FLOAT})`；
   - 亲手把这三项翻译成 ACL 全名（`ACL_BF16` / `ACL_FLOAT16` / `ACL_FLOAT`）和文档简写（`BF16` / `FLOAT16` / `FLOAT`）。
3. **需要观察的现象**：gelu 不支持任何整型，这与 AddExample 支持 `DT_INT32` 形成对比——GELU 是超越函数，对整数输入无意义。
4. **预期结果**：完成下面的小对照表（答案见 4.1.5）。

#### 4.1.5 小练习与答案

**练习 1**：文档说某算子输入支持 `FLOAT8_E4M3FN`，你在 C++ 样例里 `aclCreateTensor` 时应传什么 dtype？在 def 文件里会看到什么写法？

> **答**：样例里传 `ACL_FLOAT8_E4M3FN`；def 文件里写 `ge::DT_FLOAT8_E4M3FN`。

**练习 2**：为什么量化 matmul 算子（如 quant_batch_matmul_v4）大量使用 INT8/FP8/FP4 而不是 FLOAT？

> **答**：矩阵乘的数据搬运量与计算量巨大，窄 dtype（1 字节甚至半字节）能把搬运量降低到 fp32 的 1/4 甚至 1/8，配合量化 scale 还原精度，是带宽受限场景下典型的性能手段（详见 u6-l2）。

### 4.2 数据格式（Format）：ND、NCHW/NHWC 与私有格式

#### 4.2.1 概念说明

数据格式回答的问题是：**多维 Tensor 的各个轴是什么业务语义，数据按什么规则在内存里排布**。

- **ND**（N-dim）：最通用的格式——多维 Tensor 按低维优先（row-major）连续排布，不赋予任何轴业务语义。绝大多数非 CNN 算子（Add、Gelu、Matmul 等）都只要求 ND。
- **NCHW / NHWC / HWCN / NDHWC / NCDHW / NC / NCL**：CNN 类格式。N=Batch、C=Channel、H=Height、W=Width、D=Depth、L=Length。卷积类算子必须知道轴语义才能计算，例如 2D 卷积需要知道哪个维度是 Batch、哪个是 Channel。
- **私有格式**：`ACL_FORMAT_FRACTAL_NZ`、`ACL_FORMAT_FRACTAL_Z`、`ACL_FORMAT_NC1HWC0`、`ACL_FORMAT_NDC1HWC0`、`ACL_FORMAT_FRACTAL_Z_3D` 等。这些是 NPU 硬件（尤其 Cube 矩阵单元）偏好的分块排布格式。直觉上，Cube 单元按固定大小的数据块（如 \(C_0 \times C_0\) 的小块）取数效率最高，NZ/Z 格式就是把普通矩阵重新切分、按块摆放的格式。其精确的排布原理，data_format.md 指向《Ascend C 算子开发指南》的「数据排布格式」章节，此处不展开细节。**当前绝大多数 aclnn API 不支持私有格式**，只有个别 API 显式声明支持时才可用。

#### 4.2.2 核心流程

一个重要校验规则：**对声明了非 ND 格式的 Tensor，其维度数必须与 format 的字母个数一致**。

```text
5D Tensor → 只能是 NCDHW / NDHWC（或 ND，若 API 声明支持）
4D Tensor → 只能是 NCHW / NHWC / HWCN（或 ND）
3D Tensor → 只能是 NCL（或 ND）
2D Tensor → 只能是 NC（或 ND）
其他维度  → 只能是 ND
```

注意括号里的限定：如果 API 的参数说明**没有**标明支持 ND，强行设置 ND 也会校验报错。

调用链上 format 的流转与 dtype 类似：用户在 `aclCreateTensor` 里设置 format → aclnn 第一段校验它是否落在 def 文件 `Format({...})` 声明的候选列表（同样与 DataType 按下标配对）→ 不匹配则返回参数错误。

#### 4.2.3 源码精读

[docs/zh/context/data_format.md:L11-L30](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/context/data_format.md#L11-L30) —— 使用说明与常见格式清单：明确「目前大部分算子 API 都支持 ND」；CNN 类 API（如 aclnnConvolution）才要求带业务语义的格式；并给出了上面那段「维度数与 format 一致」的规则原文。

[docs/zh/context/data_format.md:L32-L36](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/context/data_format.md#L32-L36) —— 私有格式说明：列出 `FRACTAL_Z`、`FRACTAL_NZ`、`NC1HWC0` 等私有格式，并说明当前绝大多数 aclnn API 不支持，个别声明的以该 API 实际描述为准。

回到 def 文件看声明：

[examples/add_example/op_host/add_example_def.cpp:L46-L48](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_def.cpp#L46-L48) —— x1 的 `.Format({ge::FORMAT_ND, ge::FORMAT_ND})`：两个候选槽位（对应 FLOAT 和 INT32）都只接受 ND。逐元素加法不需要任何轴语义，这是矢量算子的典型声明。注意 `UnknownShapeFormat` 也配了两个 ND——它声明的是「动态 shape 尚未确定时」format 如何占位推导。

[matmul/quant_batch_matmul_v4/op_host/quant_batch_matmul_v4_def.cpp:L50-L66](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/matmul/quant_batch_matmul_v4/op_host/quant_batch_matmul_v4_def.cpp#L50-L66) —— 即便是重度量化的 Cube 类算子，x1 的 Format 列表也全是 `ge::FORMAT_ND`。这说明：**对外的 API 接口层是 ND，NZ/Z 等私有分块排布是算子内部（tiling/kernel 或框架图优化阶段）自行完成的**，用户不需要、也不应该手工构造 NZ 格式的输入。

#### 4.2.4 代码实践

1. **实践目标**：体会「ND 是 API 层的通用语言」。
2. **操作步骤**：
   - 在仓库里搜索 def 文件中非 ND 的 Format 声明：`Grep` 模式 `FORMAT_FRACTAL|FORMAT_NCHW|FORMAT_NHWC`，glob `**/op_host/*_def.cpp`；
   - 统计命中与未命中的算子数量级。
3. **需要观察的现象**：绝大多数（几乎全部）def 文件的 Format 列表里只有 `ge::FORMAT_ND`；少数 CNN 类或特殊算子才会出现 `FORMAT_NCHW` 等声明。
4. **预期结果**：得出「ops-nn 中 format 差异主要体现在少数 CNN 类算子上，读写 API 层基本可以按 ND 单一格式理解」的结论。若个别版本搜索结果与此不符，以实际搜索结果为准（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：你给一个 3D Tensor（如 shape `(2, 8, 8)`）设置 `ACL_FORMAT_NCHW`，会发生什么？

> **答**：校验报错。NCHW 是 4 字母格式，要求 4D Tensor；3D Tensor 只能用 NCL 或 ND。错误会在 aclnn 第一段（GetWorkspaceSize）的参数校验中返回。

**练习 2**：既然 Cube 单元喜欢 NZ 格式，为什么 quant_batch_matmul_v4 的 def 文件里 Format 全是 ND？

> **答**：def 文件声明的是**对外 API 接口**接受的格式；ND 是最通用的用户侧格式。NZ 分块是算子内部执行时的物理排布，由算子实现/框架在内部转换，不暴露给调用者。这体现了「接口简单、内部优化」的分层设计。

### 4.3 非连续 Tensor 与 AutoContiguous

#### 4.3.1 概念说明

一个 Tensor 不一定「独占一整块连续内存」。它可以只是某块大内存上的一个**视图**，用三元组描述：

- **shape**：逻辑形状，如 \((6, 5)\)；
- **strides**：每个维度上相邻两个逻辑元素的间隔（以元素计），如 \((10, 1)\)；
- **offset**：首元素相对底层地址 addr 的偏移，如 22。

PyTorch 的切片/转置/广播天然产生非连续 Tensor（如 `x[:, 2:7]`、`x.t()`），所以 aclnn 算子必须面对「输入可能非连续」的现实。这带来两个影响：

1. **不能假设元素在内存里挨着**：kernel 里的 DataCopy 搬运必须按 strides 寻址，或者先做连续化。
2. **性能与通用性的取舍**：连续化（拷贝成紧凑排布）最省 kernel 的实现复杂度，但多一次搬运。

ops-nn 提供两种处理路线（u3-l1 已提及，这里从「数据」的角度补全）：

- **声明式**：def 文件里对某个输入/输出调用 `.AutoContiguous()`——框架在调用算子前自动把它连续化，kernel 无需关心 strides。
- **适配层手动处理**：生产算子 gelu 不用 `AutoContiguous`，而是在 aclnn 适配层显式插入 Contiguous 算子（见 u6-l1 的 Gelu = Contiguous → Gelu → ViewCopy 链），换取对非连续场景更精细的控制。

#### 4.3.2 核心流程

用文档中的示例 1 画出来。底层内存是一个 \(10 \times 10\) 的连续区域，Tensor 为 shape=\((6,5)\)、strides=\((10,1)\)、offset=22：

```text
逻辑元素 (i, j) 的物理位置 = offset + i * strides[0] + j * strides[1]
                            = 22 + i * 10 + j
```

即取底层大矩阵的「第 2~7 行、第 2~6 列」这个子矩形。strides[1]=1 说明维度 1（列方向）在内存中连续；strides[0]=10 说明相邻两行间隔 10 个元素——行与行之间在内存里并不紧挨着。

文档示例 2 更极端：strides=\((20,2)\)，连最内维都不是 1——每个逻辑元素之间都隔着空洞，相当于带步长的切片。两个示例的完整排布图建议回到文档原文对照阅读。

#### 4.3.3 源码精读

[docs/zh/context/non_contiguous_tensor.md:L3-L5](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/context/non_contiguous_tensor.md#L3-L5) —— 定义：目前大部分算子 API 的输入 aclTensor 支持非连续 Tensor，即用 \((shape, strides, offset)\) 表示。

[docs/zh/context/non_contiguous_tensor.md:L9-L21](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/context/non_contiguous_tensor.md#L9-L21) —— 示例 1 的完整内存排布图与解读：strides 描述相邻元素间隔，stride 为 1 的维度连续，offset 是首元素相对 addr 的偏移。

def 文件里的两种路线对照：

[examples/add_example/op_host/add_example_def.cpp:L44-L49](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_def.cpp#L44-L49) —— AddExample 对 x1（x2、y 同理，见 [L51-L63](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_def.cpp#L51-L63)）声明 `.AutoContiguous()`：教学算子选择让框架全权处理非连续输入，kernel 实现最简单。

[activation/gelu/op_host/gelu_def.cpp:L23-L32](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/gelu_def.cpp#L23-L32) —— gelu 的输入声明链到 `UnknownShapeFormat` 为止，**没有** `AutoContiguous()`。非连续输入由 aclnn 适配层插入的 Contiguous 处理（u6-l1 将精读该适配层源码）。

#### 4.3.4 代码实践

1. **实践目标**：直观验证 AutoContiguous 的存在感。
2. **操作步骤**：
   - 阅读 AddExample 的调用样例 `examples/add_example/examples/test_aclnn_add_example.cpp`，注意其中构造 aclTensor 时 strides 是按 shape 逐维累乘计算的（即样例给的输入本身是连续的）；
   - 若有运行环境：修改样例，把其中一个输入的 strides 故意改大（例如 shape `(8, 8)` 却传 strides `(16, 1)`，同时把 device 内存按 strides 上的量级准备数据），重新 `bash build.sh --run_example add_example eager cust` 观察；
   - 无运行环境时改为源码阅读：确认 `add_example_tiling.cpp` 与 kernel 中从头到尾只用了 `totalLength = shape 乘积`，没有任何 strides 参数——这就是「连续化之后 kernel 可以无视 strides」的证据。
3. **需要观察的现象**：带非连续 strides 的输入经 AutoContiguous 连续化后仍能算出正确结果（kernel 侧完全无感知）；若去掉 def 里的 `AutoContiguous()` 再编译，同样的非连续输入结果将不正确（kernel 按连续假设寻址）。
4. **预期结果**：理解「AutoContiguous 是框架插队做的一次隐式拷贝」，代价是额外搬运，收益是 kernel 简单可靠。运行结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：shape=\((4,3)\)、strides=\((20,2)\)、offset=22 的 Tensor，逻辑元素 \((1, 2)\) 在底层内存的第几个位置（以元素计）？

> **答**：\(22 + 1 \times 20 + 2 \times 2 = 46\)。公式：位置 = offset + Σ(逻辑下标 × 对应 stride)。

**练习 2**：既然 AutoContiguous 这么方便，为什么生产算子 gelu 不用它？

> **答**：AutoContiguous 是「一刀切」的隐式连续化，输入即使本身连续也可能付出判断开销，且无法针对场景定制。gelu 把非连续处理放到 aclnn 适配层显式编排（只在需要时插 Contiguous 算子，再经 ViewCopy 回写），获得更细的控制度。这是「声明式简便」与「适配层灵活」的经典取舍。

### 4.4 aclnn 返回码：出错了去哪查

#### 4.4.1 概念说明

aclnn 两段式接口的每个函数都返回一个 int 状态码。看到一串数字（如 161001）不知道含义时，第一反应应该是查 `docs/zh/context/aclnn_return_code.md`；异常状态码还可以用 `aclGetRecentErrMsg` 接口取回错误详情。

返回码分三大类，正好对应调用链的三个阶段：

| 类别 | 码段 | 对应阶段 |
| --- | --- | --- |
| 参数错误 | 161xxx | 第一段 GetWorkspaceSize 的入参校验（nullptr、dtype/format/shape 不满足约束） |
| runtime 错误 | 361xxx | 下发到 NPU runtime 时出错 |
| 内部异常 | 561xxx | 算子库内部：infershape 失败、tiling 失败、找不到 kernel、json 加载失败等 |

#### 4.4.2 核心流程

```text
aclnnXxxGetWorkspaceSize 返回非 0
    ├─ 161001 (PARAM_NULLPTR)     → 检查是否传了空指针
    ├─ 161002 (PARAM_INVALID)     → 对照头文件注释检查 dtype/format/shape 组合
    └─ 561003 (FIND_KERNEL_ERROR) → 算子二进制包未安装，检查编译安装与 LD_LIBRARY_PATH
aclnnXxx 执行段返回非 0
    └─ 361001 (RUNTIME_ERROR)     → runtime 侧异常，用 aclGetRecentErrMsg 取详情
```

#### 4.4.3 源码精读

[docs/zh/context/aclnn_return_code.md:L8-L14](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/context/aclnn_return_code.md#L8-L14) —— 常见返回码表：`ACLNN_SUCCESS`=0，`ACLNN_ERR_PARAM_NULLPTR`=161001，`ACLNN_ERR_PARAM_INVALID`=161002，`ACLNN_ERR_RUNTIME_ERROR`=361001。

[docs/zh/context/aclnn_return_code.md:L22-L38](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/context/aclnn_return_code.md#L22-L38) —— 内部异常码表节选：`561001` infershape 出错、`561002` tiling 异常、`561003` 查找 kernel 异常（**可能因为算子二进制包未安装**——这是 u1-l2 讲过的「改了算子源码必须重新 --pkg 编译安装」的直接排障线索）、`561111`/`561112` 算子信息 json 的 dtype/二进制加载失败。

结合本讲主题最值得记住的一条：如果传入的 dtype/format 组合不在 def 文件的候选槽位里，返回的就是 `161002 (ACLNN_ERR_PARAM_INVALID)`——**def 文件就是参数校验的第一道闸门**（u3-l1 的结论在返回码层面的落点）。

#### 4.4.4 代码实践

1. **实践目标**：亲手触发一次参数校验错误并读懂它。
2. **操作步骤**（需运行环境，待本地验证）：
   - 复制 `test_aclnn_add_example.cpp`，把构造 x1 的 aclTensor 的 dtype 从 `ACL_FLOAT` 改成 `ACL_BF16`（AddExample 的 def 只声明了 FLOAT/INT32）；
   - 重新运行样例，观察第一段 `aclnnAddExampleGetWorkspaceSize` 的返回值。
3. **需要观察的现象**：返回值应为 `161002`（ACLNN_ERR_PARAM_INVALID），且程序在第一段就安全退出，不会走到执行段。
4. **预期结果**：验证「def 文件的 DataType 候选列表 = 参数校验规则」。若返回码与预期不同，用 `aclGetRecentErrMsg` 取详情对照返回码表。

#### 4.4.5 小练习与答案

**练习 1**：调用返回 561003，最可能的原因是什么？

> **答**：算子二进制 kernel 未找到——典型原因是算子二进制包没有安装（或安装的 vendor 包版本与调用环境不匹配），应回到 build.sh 重新 `--pkg` 编译并安装 run 包（参见 u1-l2）。

**练习 2**：161002 和 561001 都可能和 shape 有关，如何区分？

> **答**：161002 是**入参**校验失败（如输入 shape/dtype 组合非法），发生在第一段最开始；561001 是算子库**内部**做 infershape 时失败（如输入 shape 之间不满足算子的推导约束），发生在第一段的内部流程中。前者查头文件参数注释，后者查算子的 infershape 实现（u3-l2）。

## 5. 综合实践

**任务：整理一份「类型-格式支持矩阵」，对比 add_example 与 quant_batch_matmul_v4。**

这个任务把本讲四个模块串起来：dtype 体系（模块 1）、format 候选（模块 2）、def 文件声明链的读法（承接 u3-l1），以及最后用返回码知识设计验证手段（模块 4）。

操作步骤：

1. 打开 [examples/add_example/op_host/add_example_def.cpp:L44-L63](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_def.cpp#L44-L63)，提取 x1/x2/y 三个张量的 DataType、Format、UnknownShapeFormat、是否 AutoContiguous；
2. 打开 [matmul/quant_batch_matmul_v4/op_host/quant_batch_matmul_v4_def.cpp:L23-L66](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/matmul/quant_batch_matmul_v4/op_host/quant_batch_matmul_v4_def.cpp#L23-L66)，对 x1 做同样提取（候选槽位很多，可按 dtype 去重归类统计，不必逐槽位抄写）；
3. 产出一张 Markdown 表格，形如：

   | 维度 | add_example | quant_batch_matmul_v4 (x1) |
   | --- | --- | --- |
   | dtype 候选数 | 2（FLOAT/INT32） | 84 个槽位，去重后 8 种（INT8/HIFLOAT8/FLOAT8_E4M3FN/FLOAT8_E5M2/FLOAT4_E2M1/INT4/FLOAT/…） |
   | format | 全部 ND | 全部 ND |
   | 候选槽位组织 | dtype 一一对应 format | 同一 dtype 重复出现、按下标与 format 配对 |

   （表中 quant_batch_matmul_v4 的具体数字请以你实际读到的列表为准，自行核对。）
4. 在表格下方用 3~5 句话回答：为什么逐元素教学算子只需要 2 个槽位，而量化 Cube 算子需要几十个槽位？这和「每个槽位对应一份预编译二进制/一个 tiling key 分支」（u3-l1、u4-l2 的结论）有什么关系？
5. （可选，需运行环境）从矩阵里挑一个 add_example 不支持的 dtype（如 BF16）跑 4.4.4 的实践，验证返回 161002，把返回码记录进矩阵备注。

**参考结论方向**：逐元素算子对 dtype 的处理逻辑是同一套模板（类型参数化），槽位少；量化 matmul 的每个槽位对应一种「x1 dtype × x2 dtype × 量化模式」的合法组合，运行时按实际输入命中其中一条，因此槽位多且允许重复——这是「声明式枚举所有合法组合」的工程代价换来的精确校验与按需编译。

## 6. 本讲小结

- **dtype 三套写法**：def 源码 `ge::DT_*` ↔ aclnn 真实类型 `ACL_*` ↔ 文档简写（`FLOAT16`、`FLOAT8_E4M3FN` 等，见 data_type.md 对照表，不区分大小写）。
- **format 是轴语义**：ND 是无语义的通用格式，绝大多数 API 只支持 ND；CNN 类 API 才要求 NCHW/NHWC 等；非 ND 格式要求维度数与字母数一致；FRACTAL_NZ/Z 等私有格式是 NPU 内部偏好的分块排布，基本不对外。
- **DataType 与 Format 在 def 文件里按下标配对成候选槽位**，允许重复项；quant_batch_matmul_v4 的 84 槽位是「枚举所有合法量化组合」的极端例子。
- **非连续 Tensor 用 (shape, strides, offset) 描述**，元素物理位置 = offset + Σ(下标 × stride)；处理路线有声明式 `AutoContiguous()`（add_example）与适配层显式 Contiguous（gelu）两种。
- **返回码分三段**：161xxx 参数错误（对照 def 候选与头文件注释）、361xxx runtime 错误、561xxx 内部异常（561003 ≈ 二进制包未安装）；异常详情用 `aclGetRecentErrMsg` 获取。

## 7. 下一步学习建议

本讲补全了读 def 文件所需的全部背景概念，接下来按依赖关系有两条路：

1. **进入 tiling（u4-l1「Tiling 机制入门」）**：dtype 决定了每个元素的字节宽度，这直接影响 tiling 中 UB 切分的块大小计算——`TYPE_SIZE` 这类元素宽度常量（u3-l1 提过）将在 tiling 源码中大量出现。
2. **横向对比更多 def 文件**：用综合实践的方法再读 2~3 个不同大类算子的 def（如 `norm/layer_norm`、`index/*`），感受「dtype/format 候选矩阵」如何随算子语义变化，为 u6-l3 的算子大类巡礼做铺垫。

此外，docs/zh/context/ 目录下还有 `broadcast_relationship.md`（广播，u3-l2 已用）、`two_phase_api.md`（两段式，u6-l1 将用）、`quant_mode_introduction.md`（量化模式，u6-l2 将用）等姊妹篇，本讲的方法论——「读算子前先读它的上下文文档」——同样适用于它们。
