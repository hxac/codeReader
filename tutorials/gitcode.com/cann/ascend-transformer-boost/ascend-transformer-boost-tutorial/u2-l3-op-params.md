# 算子参数体系与公共枚举

## 1. 本讲目标

前两讲（u2-l1、u2-l2）你已经分别用 C++ 和 Python 跑通了一个 ATB 算子，见到了 `LinearParam`、`FaUpdateParam` 这样的「参数结构体」。本讲把这些零散的例子收敛成一张**全局地图**：ATB 所有望 70+ 推理算子的参数，到底遵循怎样的统一约定。

读完本讲，你应当能够：

1. 说清楚每个 `XxxParam` 结构体的**统一骨架**——为什么都用 POD 风格的字段、为什么末尾总有一个 `uint8_t rsv[N]` 预留字段，以及这个预留字段如何充当**版本兼容闸门**。
2. 默写出 `infer` 命名空间下的几个**公共枚举**：`InputLayout`、`QuantType`、`ActivationType`、`CommMode`，并知道它们被哪些算子复用。
3. 拿到一个陌生算子名（如 `SelfAttention`、`Linear`），能**按图索骥**在 `infer_op_params.h` 里找到它的 `Param` 定义，读懂每个关键字段的含义与取值约束。
4. 识别「嵌套枚举」这一复杂 Param 的组织手法，看懂 `SelfAttentionParam` 这种字段众多的结构体是怎么被分门别类管起来的。

> 一个直觉：ATB 的 Param 就像一张「算子配置单」。同一个算子（比如 Linear）能做普通矩阵乘、能做量化矩阵乘、能做爱因斯坦求和，全靠你在配置单上勾选不同的枚举值。本讲要做的，就是把这张配置单的**栏目规范**和**常用勾选项**讲透。

## 2. 前置知识

本讲假设你已建立以下认知（来自第 1 单元与前两讲）：

- **Operation 与 Param 的关系**：算子由工厂模板 `CreateOperation<OpParam>` 创建，`OpParam` 决定算子的具体行为；`Operation` 是抽象基类，定义 `GetName/InferShape/Setup/Execute` 等接口（见 u1-l6）。
- **两段式执行**：`Setup` 在 Host 侧做校验与形状推导，`Execute` 才异步下发到 Device（见 u1-l6）。
- **基础数据类型**：`aclDataType`（如 `ACL_FLOAT16`、`ACL_BF16`、`ACL_DT_UNDEFINED`）来自 CANN 的 `acl/acl.h`；`SVector<T>` 是 ATB 自研容器（见 u1-l4）。
- **Status / ErrorType**：接口返回 `Status`（即 `int32_t`，0 为成功），失败类别见 `ErrorType`，如 `ERROR_INVALID_PARAM`（见 u1-l4）。

此外需要两个背景概念：

- **POD（Plain Old Data）结构体**：可以「按字节拷贝」、没有虚函数、成员都是普通类型的 C++ 结构体。ATB 的 Param 几乎都是 POD，便于在 Host/Device 之间、Python/C++ 之间（经 pybind11）整体搬运与序列化。
- **量化（Quantization）**：把原本 float16 的权重/激活压缩成 int8 等低比特以省显存、提性能；`quantType`、`outDataType` 这类字段就是用来声明量化方式的。

## 3. 本讲源码地图

本讲涉及的源码文件及其作用：

| 文件 | 作用 |
|------|------|
| [include/atb/infer_op_params.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h) | **本讲主战场**：`atb::infer` 命名空间下全部推理算子的 Param 结构体与公共枚举（InputLayout/QuantType/ActivationType/CommMode） |
| [include/atb/common_op_params.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/common_op_params.h) | `atb::common` 命名空间下的通用 Param（`EventParam`、`IfCondParam`），被推理/训练/图算子共用 |
| [src/atb/operation/op_param_funcs.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/op_param_funcs.h) | `OPERATION_PARAM_FUNCS` 与 `OP_PARAM_RSV_CHECK` 宏：生成每个算子的创建/克隆/更新三件套，并强制校验 `rsv` 全 0 |
| [include/atb/operation.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/operation.h) | `CreateOperation`/`CloneOperationParam`/`UpdateOperationParam` 模板声明（被上面的宏特化） |

> 命名约定速查：`atb::infer::XxxParam` 是推理算子参数（本讲主角）；`atb::common::XxxParam` 是跨场景通用参数（控制流、流同步）。看到 `XxxParam` 这个后缀，就知道它是某个算子（或某一类操作）的配置单。

## 4. 核心概念与源码讲解

### 4.1 Param 的统一骨架与 rsv 版本兼容机制

#### 4.1.1 概念说明

打开 `infer_op_params.h`，你会看到几十个结构体，长相惊人地一致：若干带默认值的普通字段，末尾总跟着一句 `uint8_t rsv[N] = {0};`。这不是巧合，而是 ATB 对所有 Param 的**硬性约定**。我们先抽出这个统一骨架：

```cpp
struct XxxParam {
    // ... 若干业务字段，几乎都带默认值 ...
    bool someFlag = false;
    aclDataType outDataType = ACL_DT_UNDEFINED;
    enum SomeType : int { ... };          // 需要分类的取值，用嵌套枚举
    SomeType someType = SOME_DEFAULT;
    uint8_t rsv[N] = {0};                  // 末尾的「预留字段」，N 因算子而异
};
```

理解这套骨架要抓住三点：

1. **POD + 默认值**：所有字段都有默认值，用户只需设置关心的那几个，其余保持默认即可。结构体可整体按字节拷贝。
2. **`XxxParam` 命名**：算子名 `Linear` 对应 `LinearParam`，`SelfAttention` 对应 `SelfAttentionParam`，一一对应，按名字就能找到定义。
3. **末尾 `rsv[N]`**：reserved（预留）的缩写，是一段**故意空着、必须全 0** 的字节。它的真正用途是**版本兼容闸门**——下面 4.1.2/4.1.3 重点讲。

#### 4.1.2 核心流程：rsv 如何充当版本兼容闸门

设想一个真实场景：你用某一版的 `infer_op_params.h` 编译了上层应用，里面 `LinearParam` 有 8 个业务字段 + `rsv[21]`。下一版 ATB 给 Linear 加了一个新字段 `enAccum`，于是把它从 `rsv` 里「切」一块出来用，`rsv` 随之缩短。**如果不加防护**，旧版应用带着偏大的 `Param` 结构体去调新版库（或反过来），内存布局错位，会得到难以排查的错误。

ATB 的做法是：约定 `rsv` 这段字节**当前版本不使用、必须全 0**；在 `CreateOperation` 与 `UpdateOperationParam` 的入口处，逐字节检查 `rsv` 是否全 0，只要出现非 0 字节，立刻拒绝并提示「请检查编译版本」。这样一旦版本不匹配（旧程序误填了新版的字段，落到 `rsv` 区间），就会**立刻报错**而不是静默错位。

```text
CreateOperation<XxxParam>(param, &op)
        │
        ▼
OPERATION_PARAM_FUNCS 生成的特化版本
        │
        ├─ 1) 逐字节遍历 param.rsv
        │      若任一字节 != 0 → ATB_LOG(ERROR) + return ERROR_INVALID_PARAM
        │
        ├─ 2) ParamCheck(param)   算子自定义的业务校验
        │
        └─ 3) new OpName(param)   真正构造算子对象
```

#### 4.1.3 源码精读

检查逻辑写在 `OPERATION_PARAM_FUNCS` 宏里。这个宏为每个算子「展开」成 `CreateOperation`/`CloneOperationParam`/`UpdateOperationParam` 三个模板特化（呼应 u1-l6 提到的「一行注册即获得创建/克隆/更新三件套」）。其中 `rsv` 检查位于创建入口：

[src/atb/operation/op_param_funcs.h:13-24](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/op_param_funcs.h#L13-L24) —— 宏开头即遍历 `opParam.rsv`，发现非 0 字节就拒绝：

```cpp
#define OPERATION_PARAM_FUNCS(OpName, OpParamType)                                     \
    template <> Status CreateOperation(const OpParamType &opParam, Operation **operation) \
    {                                                                                  \
        ...                                                                            \
        for (uint8_t i : opParam.rsv) {                                                \
            if (i != 0) {                                                              \
                ATB_LOG(ERROR) << "param rsv has a non-zero value, "
                                  "please check the compilation version.";             \
                return ERROR_INVALID_PARAM;                                            \
            }                                                                          \
        }                                                                              \
        if (!ParamCheck(opParam)) { ... }                                              \
        *operation = new (std::nothrow) OpName(opParam);                               \
        ...
```

[src/atb/operation/op_param_funcs.h:72-80](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/op_param_funcs.h#L72-L80) —— 同样的检查被抽成独立宏 `OP_PARAM_RSV_CHECK`，供 `Setup` 等其他入口复用。

回到 Param 本身，随便挑两个看 `rsv` 的形态：`RopeParam` 末尾是 `uint8_t rsv[8]`（[include/atb/infer_op_params.h:1614-1616](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1614-L1616)），而通信类算子（如 `AllReduceParam`）末尾是 `uint8_t rsv[64]`（[include/atb/infer_op_params.h:1201-1204](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1201-L1204)）。`rsv` 长度因算子演进历史而异，但**必须全 0** 这一约定全库统一。

> 顺带说明 `operator==`：不少 Param（如 `RopeParam`、`AllGatherVParam`）额外定义了 `inline bool operator==`，它**故意不比较 `rsv`**，只比业务字段。这是因为 `UpdateOperationParam` 会用 `operator==` 判断「参数是否变化」，`rsv` 既然恒为 0 就没必要参与比较。见 [include/atb/infer_op_params.h:1626-1629](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1626-L1629)（`RopeParam::operator==`）。

#### 4.1.4 代码实践

**目标**：亲手触发一次 `rsv` 校验，直观感受「版本兼容闸门」的存在。

**操作步骤**（源码阅读 + 思想实验）：

1. 在 [src/atb/operation/op_param_funcs.h:19-24](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/op_param_funcs.h#L19-L24) 确认：只要 `opParam.rsv` 有非 0 字节，`CreateOperation` 就返回 `ERROR_INVALID_PARAM`。
2. 设想你写了一段（伪）代码，故意往 `LinearParam.rsv[0]` 写入 1，再调用 `CreateOperation`：

   ```cpp
   // 示例代码（仅作思想实验，不要写进真实程序）
   atb::infer::LinearParam param;
   param.rsv[0] = 1;                 // 故意污染预留字段
   atb::Operation *op = nullptr;
   auto st = atb::CreateOperation(param, &op);
   // st 会被判定为失败
   ```

3. 对照宏逻辑推断返回值与日志。

**需要观察的现象**：`CreateOperation` 不会构造出算子对象（`op` 仍为 `nullptr`），返回非 0 状态码，且日志里出现 `param rsv has a non-zero value, please check the compilation version.`。

**预期结果**：返回 `ERROR_INVALID_PARAM`（非 0），日志含上述提示。运行结果**待本地验证**（需在已安装 ATB 的昇腾环境编译运行）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `rsv` 检查放在 `CreateOperation` 入口，而不是放在每个算子自己的 `Setup` 里？

> **答案**：放在统一入口（宏生成的 `CreateOperation`）可以**一处实现、全算子生效**，避免每个算子重复写；同时「版本不匹配」是结构体布局层面的问题，应当在对象构造之前就拦住，越早越好。算子自定义的业务校验才放到 `ParamCheck` / `Setup` 里。

**练习 2**：`XxxParam::operator==` 通常不比较 `rsv`，这是否会造成「两个 Param 实际不等却被判等」？

> **答案**：不会。因为 `rsv` 按约定**恒为全 0**（非 0 会在创建时被拒绝），所以它不携带任何业务信息，比较与否不影响相等性判断。不比较它反而省去了无意义的逐字节对比。

---

### 4.2 infer 命名空间的公共枚举

#### 4.2.1 概念说明

除了 `rsv`，Param 里有另一类「跨算子复用」的构件——**公共枚举**。它们定义在 `atb::infer` 命名空间作用域（而不是某个结构体内部），因此所有算子都能直接引用。掌握这少数几个枚举，你就能读懂一大片算子的关键字段。

四个最重要的公共枚举：

| 枚举 | 解决的问题 | 典型取值 | 被谁用 |
|------|-----------|---------|--------|
| `InputLayout` | 注意力算子的数据排布 | `TYPE_BSND` / `TYPE_BNSD` | `SelfAttention`、`PagedAttention`、`MultiLatentAttention`、`RingMLA` |
| `QuantType` | 是否量化、用什么比特宽度 | `QUANT_UNQUANT`(0) / `QUANT_INT8`(2) | 归一化、注意力等（注意：很多结构体内部还各自重定义了同名 `QuantType`，见 4.4） |
| `ActivationType` | 用哪种激活函数 | `ACTIVATION_RELU` / `ACTIVATION_GELU` / `ACTIVATION_SWIGLU_FORWARD` 等 | `ActivationParam`、融合算子 `FusedAddTopkDivParam` |
| `CommMode` | 通信算子的并发模型 | `COMM_MULTI_PROCESS` / `COMM_MULTI_THREAD` | 所有集合通信算子（AllReduce/AllGather/...） |

#### 4.2.2 核心流程：枚举值如何驱动算子行为

公共枚举本质是「算子的模式开关」。以 `InputLayout` 为例，同一个 `SelfAttention` 算子，传入 `TYPE_BSND` 时要求 Q/K/V 张量排布为 `[Batch, Seq, headNum, headDim]`，传入 `TYPE_BNSD` 时则要求 `[Batch, headNum, Seq, headDim]`——算子内部的 Tiling 与 Kernel 选择会据此走不同分支。其余枚举同理：**枚举值不同 ⇒ 算子的计算模式 / 数据格式 / 通信方式不同**。

```text
Param 里的枚举字段
        │
        ▼
Setup 阶段：根据枚举值选择 Runner / Kernel 分支、校验输入格式
        │
        ▼
Execute 阶段：按选定分支下发到 Device
```

需要特别留意的一个「坑」：`QuantType` 这个名字在 `infer` 命名空间作用域有一个定义，在 `AllReduceParam`、`SelfAttentionParam`、`LinearParallelParam` 等结构体**内部又各自重定义了同名枚举**，取值还不完全一样。读到 `quantType` 字段时，务必确认它用的是**哪一个** `QuantType`（详见 4.4）。

#### 4.2.3 源码精读

**`InputLayout`** —— 注意力张量的两种排布，[include/atb/infer_op_params.h:39-42](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L39-L42)：

```cpp
enum InputLayout : int {
    TYPE_BSND = 0, //!< 默认值，表示数据排布为BSND
    TYPE_BNSD      //!< 表示数据排布为BNSD
};
```

**`QuantType`（命名空间作用域版）** —— [include/atb/infer_op_params.h:49-57](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L49-L57)。注意 `QUANT_UNDEFINED` 与 `QUANT_UNQUANT` 都等于 0（都表示「不量化」），且注释标明 int4/int16/float8 等当前不支持：

```cpp
enum QuantType : int {
    QUANT_UNDEFINED = 0, //!< 不量化
    QUANT_UNQUANT = 0,   //!< 不量化
    QUANT_INT8 = 2,      //!< int8量化
    // QUANT_INT4 / QUANT_INT16 / QUANT_FLOAT8 / QUANT_FLOAT16 当前不支持
};
```

**`ActivationType`** —— [include/atb/infer_op_params.h:79-91](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L79-L91)，覆盖 ReLU/GELU/SWISH/SiLU/SwiGLU 等常见激活，末尾的 `ACTIVATION_MAX` 仅作边界哨兵：

```cpp
enum ActivationType : int {
    ACTIVATION_UNDEFINED = 0,       //!< 未定义
    ACTIVATION_RELU,                //!< RELU
    ACTIVATION_GELU,                //!< GELU
    ACTIVATION_FAST_GELU,           //!< 快速 GELU（近似，速度更快）
    ACTIVATION_SWISH,               //!< SWISH
    ACTIVATION_SWIGLU_FORWARD,      //!< Swiglu 正向
    ACTIVATION_SWIGLU_BACKWARD,     //!< Swiglu 反向（仅 Atlas 800I A2）
    ACTIVATION_SIGMOID,             //!< SIGMOID
    ACTIVATION_FASTER_GELU_FORWARD, //!< 简化 FastGelu
    ACTIVATION_MAX,                 //!< 枚举最大值, 非激活类型
};
```

**`CommMode`** —— [include/atb/infer_op_params.h:98-102](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L98-L102)，区分多进程与多线程两种通信并发模型：

```cpp
enum CommMode : int {
    COMM_UNDEFINED = -1, //!< 未定义
    COMM_MULTI_PROCESS,  //!< 指定多进程通信
    COMM_MULTI_THREAD,   //!< 指定多线程通信
};
```

此外还有 `DynamicQuantType`（对称/非对称动态量化，[include/atb/infer_op_params.h:64-68](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L64-L68)），用在归一化算子里，思路与 `QuantType` 一致。

#### 4.2.4 代码实践

**目标**：建立「算子名 → 公共枚举字段」的检索能力。

**操作步骤**：

1. 在 `infer_op_params.h` 中用搜索定位 `InputLayout inputLayout` 出现在哪些 Param 里（预期：`SelfAttentionParam`、`PagedAttentionParam`、`MultiLatentAttentionParam`、`RingMLAParam`）。
2. 对每一处，记录它的默认值（应当都是 `TYPE_BSND`）。
3. 思考：如果你的模型把 Q/K/V 按 `[B, N, S, D]`（即 headNum 在前）排布，应当把这些字段设成什么？

**需要观察的现象**：`inputLayout` 字段在多个注意力算子里反复出现，默认值统一为 `TYPE_BSND`。

**预期结果**：上述四个注意力 Param 都含 `InputLayout inputLayout = TYPE_BSND;`；BNSD 排布时应显式设为 `TYPE_BNSD`，否则算子会按 BSND 解析导致形状/数值错误。运行结果**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`QuantType` 里 `QUANT_UNDEFINED` 和 `QUANT_UNQUANT` 都是 0，这样定义有什么好处？

> **答案**：让「未设置」（undefined）和「显式不量化」（unquant）在语义上区分、在取值上等价。这样即使用户忘了设 `quantType`，默认 0 也安全地表示「不量化」，避免「未定义」引发意外行为；同时代码里用哪个名字都更易读。

**练习 2**：`ActivationType` 末尾的 `ACTIVATION_MAX` 能不能作为一个有效激活类型传入？

> **答案**：不能。它只是「边界哨兵」，用于内部判断「某取值是否超出枚举范围」，注释明确写「非激活类型」。同理 `ElewiseParam::ElewiseType` 里的 `ELEWISE_TYPE_MAX` 也是这个用法（见 4.3.3）。

---

### 4.3 精读 LinearParam：矩阵乘算子的参数设计

#### 4.3.1 概念说明

`LinearParam` 是 ATB 里**出场频率最高**的 Param——大模型里几乎所有投影层（QKV 投影、MLP 的升维降维）都是它。它把「矩阵乘 + 偏置 + 量化 + 爱因斯坦求和 + 原地累加」这些曾经常用多个算子串起来才能完成的事，压缩成一个算子的多种模式。理解了它，你就理解了 ATB「融合算子」的设计哲学：**一个 Param，多种行为**。

#### 4.3.2 核心流程：字段如何组合出不同行为

`LinearParam` 的字段可分为三组：

1. **形状控制**：`transposeA` / `transposeB`——决定输入矩阵 `x`(A) 与权重 `weight`(B) 是否先转置再做矩阵乘。默认 `transposeB=true`，对应权重按 `[out, in]` 存储这一业界惯例。
2. **融合开关**：`hasBias`（是否加偏置）、`enAccum`（是否把结果累加到既有缓冲，而非覆盖）、`outDataType`（是否在出口做反量化）。
3. **模式选择**：`matmulType`（普通乘 vs 爱因斯坦求和）、`quantMode`（按通道 vs 按 token 量化）。

```text
y = ( matmul( transpose?(x), transpose?(weight) ) [+ bias] ) [反量化到 outDataType]
                 │                        │            │            │
              transposeA               transposeB    hasBias    outDataType!=UNDEFINED
```

#### 4.3.3 源码精读

[include/atb/infer_op_params.h:1391-1471](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1391-L1471) —— `LinearParam` 全貌。关键字段摘录（删注释、保留默认值）：

```cpp
struct LinearParam {
    enum MatmulType : uint8_t { MATMUL_UNDEFINED = 0, MATMUL_EIN_SUM };
    enum QuantMode  : uint8_t { QUANT_UNDEFINED, PER_CHANNEL, PER_TOKEN };

    bool transposeA = false;                 // A 矩阵默认不转置
    bool transposeB = true;                  // B(权重) 默认转置 → 权重按 [out,in] 给
    bool hasBias = true;                     // 默认叠加偏置
    aclDataType outDataType = ACL_DT_UNDEFINED; // UNDEFINED=浮点；否则量化反量化出口类型
    bool enAccum = false;                    // 默认不累加
    MatmulType matmulType = MATMUL_UNDEFINED; // 默认普通矩阵乘
    QuantMode quantMode = QUANT_UNDEFINED;    // 量化粒度
    uint8_t rsv[21] = {0};
};
```

几个要点对照源码注释：

- **`transposeB=true` 的含义**（[L1410-L1418](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1410-L1418)）：当 `transposeA=false, transposeB=true` 时，`x` 形状 `[m,k]`、`weight` 形状 `[n,k]`，结果 `[m,n]`。这正是 u2-l2 综合实践里「权重按 `(out_features, in_features)` 给」的来源。
- **`outDataType` 区分浮点 / 量化**（[L1438](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1438)）：值为 `ACL_DT_UNDEFINED` 表示浮点 Linear；若设成 `ACL_FLOAT16`/`ACL_BF16`，则表示量化场景下出口要反量化到该类型。这一「用 `ACL_DT_UNDEFINED` 当开关」的写法在 ATB 里非常普遍，应牢记。
- **互斥约束**：`enAccum=true` 时仅支持 `hasBias=false`；量化场景下 `enAccum` 仅支持 `false`（[L1442-L1450](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1442-L1450)）。这类「字段之间互相约束」是 `ParamCheck` 在 `Setup` 阶段要拦截的内容。

> 同源变体：`LinearParallelParam`（Linear + 集合通信，见 [L1482-L1579](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1482-L1579)）、`LinearSparseParam`（稀疏量化 Linear，[L1590-L1603](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1590-L1603)）共享同一套设计思路，区别在「额外叠了通信 / 压缩」。

#### 4.3.4 代码实践

**目标**：用 `LinearParam` 的字段组合，描述三种常见用法（即「同一算子，三种行为」）。

**操作步骤**：

1. 读 [include/atb/infer_op_params.h:1391-1471](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1391-L1471)，填写下表（示例代码，非项目原有）：

   | 用法 | transposeA | transposeB | hasBias | outDataType | matmulType |
   |------|-----------|-----------|---------|-------------|-----------|
   | 普通带偏置全连接（浮点） | false | true | true | ACL_DT_UNDEFINED | MATMUL_UNDEFINED |
   | 无偏置浮点矩阵乘 | false | true | ? | ? | ? |
   | 量化矩阵乘（出口 fp16） | ? | true | ? | ACL_FLOAT16 | ? |

2. 为「无偏置浮点矩阵乘」写一段 Python（torch_atb）伪代码，对照 u2-l2 的写法：

   ```python
   # 示例代码
   import torch_atb
   p = torch_atb.LinearParam()
   p.has_bias = False
   # 其余字段保持默认：transpose_b=True, out_data_type=未设置(浮点), ...
   linear = torch_atb.Operation(p)
   ```

**需要观察的现象**：仅靠改动 `hasBias`、`outDataType` 等几个字段，就能让同一个 `Linear` 算子表达多种计算形态，无需切换算子。

**预期结果**：表中第 2 行 `hasBias=false, outDataType=ACL_DT_UNDEFINED, matmulType=MATMUL_UNDEFINED`；第 3 行 `transposeA=false, hasBias` 依量化配置而定（注释提示量化下非 800I A2 仅支持 `hasBias=true`）。运行结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `LinearParam` 默认 `transposeB=true` 而不是 `false`？

> **答案**：业界权重几乎都按 `[out_features, in_features]` 存储（便于按输出通道切分、加载），而矩阵乘要求 A 的列数 = B 的行数，因此需要把权重转置后参与运算。把默认值设成最常见情形（`transposeB=true`），用户多数情况下无需改动。

**练习 2**：`outDataType = ACL_DT_UNDEFINED` 在 `LinearParam` 里扮演什么角色？

> **答案**：它既是「输出数据类型」字段，又兼任「浮点 / 量化」的模式开关。`UNDEFINED` 表示浮点场景、输出与输入同类型；设成具体类型（如 `ACL_FLOAT16`）则进入量化场景，表示把量化计算的结果反量化成该类型输出。

---

### 4.4 精读 SelfAttentionParam：复杂 Param 与嵌套枚举

#### 4.4.1 概念说明

如果说 `LinearParam` 是「简洁多模式」的代表，`SelfAttentionParam` 就是「复杂多维度」的典范——它要把计算类型、mask 种类、KV Cache 配置、量化方式、缩放策略、SWA 滑窗、MLA 合并……全部塞进一个结构体。ATB 的应对手法是**嵌套枚举**：把每一类「取值集合」声明成结构体内部的 `enum`，从而把几十个字段组织得井井有条。学会读这种结构体，你就掌握了应对任意复杂 Param 的通用方法。

#### 4.4.2 核心流程：嵌套枚举如何组织复杂配置

`SelfAttentionParam` 内部声明了 8 个枚举，每个枚举管一个「维度」的开关：

```text
SelfAttentionParam
├─ CalcType    : UNDEFINED / ENCODER / DECODER / PA_ENCODER / PREFIX_ENCODER
├─ KernelType  : DEFAULT / HIGH_PRECISION / EXP_M8V2   (内核精度)
├─ ClampType   : UNDEFINED / MIN_MAX                    (是否对 score 做 clamp)
├─ MaskType    : UNDEFINED / NORM / ALIBI / ...COMPRESS / SLIDING_WINDOW / CAUSAL
├─ KvCacheCfg  : K_CACHE_V_CACHE / K_BYPASS_V_BYPASS    (是否走 KV Cache)
├─ ScaleType   : TOR / LOGN                             (QK^T 的缩放策略)
├─ QuantType   : UNQUANT / DEQUANT_FUSION / QKV_OFFLINE / QKV_ONLINE
└─ CacheType   : NORM / SWA                              (KV Cache 是否固定窗长)
```

每个枚举配合一个同名小写字段（如 `enum MaskType {...}; MaskType maskType = MASK_TYPE_UNDEFINED;`）。用户配置时按「维度」逐一勾选即可，不必面对一张扁平的字段海。

#### 4.4.3 源码精读

[include/atb/infer_op_params.h:1704-1851](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1704-L1851) —— `SelfAttentionParam` 全貌。先看两个最具代表性的嵌套枚举。

**`MaskType`**（[L1741-L1752](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1741-L1752)）—— 列出所有 mask 形态，从最普通的倒三角到各种压缩/alibi/滑窗变体：

```cpp
enum MaskType : int {
    MASK_TYPE_UNDEFINED = 0,             //!< 全0 mask
    MASK_TYPE_NORM,                      //!< 倒三角 mask
    MASK_TYPE_ALIBI,                     //!< alibi mask
    MASK_TYPE_NORM_COMPRESS,             //!< 倒三角压缩 mask
    MASK_TYPE_ALIBI_COMPRESS,            //!< alibi 压缩 mask
    MASK_TYPE_SLIDING_WINDOW_NORM,       //!< sliding window attention mask
    MASK_TYPE_CAUSAL_MASK,               //!< mask 内部生成
    ...
};
```

**`CalcType`**（[L1710-L1716](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1710-L1716)）—— 决定走 FlashAttention 还是 PagedAttention 路径：

```cpp
enum CalcType : int {
    UNDEFINED = 0,  //!< decoder&encoder for flashAttention
    ENCODER,        //!< encoder for flashAttention
    DECODER,        //!< decoder for flashAttention
    PA_ENCODER,     //!< encoder for pagedAttention
    PREFIX_ENCODER, //!< prefix encoder for flashAttention
};
```

再看业务字段区（[L1798-L1850](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1798-L1850)），关键字段含义：

```cpp
int32_t headNum = 0;        // query 头数，需 > 0
int32_t kvHeadNum = 0;      // kv 头数；0 表示与 headNum 一致(用于 GQA 判断)
float qScale = 1;           // 对 Q 的缩放
float qkScale = 1;          // Q*K^T 之后的缩放（即 1/√d 通常在此体现）
bool batchRunStatusEnable = false; // 是否开启动态 batch
CalcType calcType = UNDEFINED;
MaskType maskType = MASK_TYPE_UNDEFINED;
KvCacheCfg kvcacheCfg = K_CACHE_V_CACHE;
InputLayout inputLayout = TYPE_BSND;   // ← 复用 4.2 讲的公共枚举
uint32_t mlaVHeadSize = 0;  // >0 时开启 MLA 合并 kvcache
CacheType cacheType = CACHE_TYPE_NORM;
uint32_t windowSize = 0;    // >0 开启 SWA 滑动窗口
uint8_t rsv[64] = {0};
```

三个值得专门指出的设计：

- **公共枚举的复用**：`inputLayout` 字段用的就是 4.2 讲的 `InputLayout`（`TYPE_BSND`/`TYPE_BNSD`），证明「公共枚举 + 嵌套枚举」可以共存于同一结构体。
- **`kvHeadNum=0` 的隐式语义**（[L1806-L1808](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1806-L1808)）：0 不表示「没有 KV 头」，而表示「KV 头数与 query 头数相等」（即 MHA）；非 0 才是 GQA/MQA。这是又一个「用 0 当隐式默认」的例子。
- **`QuantType` 又被重定义了一次**（[L1777-L1783](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1777-L1783)）：这里的 `SelfAttentionParam::QuantType` 取值是 `TYPE_QUANT_UNQUANT/TYPE_DEQUANT_FUSION/TYPE_QUANT_QKV_OFFLINE/TYPE_QUANT_QKV_ONLINE`，与命名空间作用域的 `QuantType`（`QUANT_INT8`...）**完全不同**。所以读 `param.quantType` 时一定要看它属于哪个结构体。

> 类比参照：`PagedAttentionParam`（[L1859-L1963](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1859-L1963)）、`MultiLatentAttentionParam`（[L2899-L2984](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L2899-L2984)）、`RingMLAParam`（[L3255-L3313](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L3255-L3313)）都是同一套「公共枚举 + 嵌套枚举 + 业务字段 + rsv」的写法，读会一个，其余触类旁通。

#### 4.4.4 代码实践（本讲主实践任务）

**目标**：在 `infer_op_params.h` 中找出 `LinearParam` 与 `SelfAttentionParam` 的全部字段，分类解释关键字段含义。

**操作步骤**：

1. 打开 [include/atb/infer_op_params.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h)，定位 `LinearParam`（L1391）与 `SelfAttentionParam`（L1704）。
2. 为每个结构体整理一张分类表，分三栏：**嵌套枚举 / 业务字段 / 预留字段**。
3. 用一句话解释下列关键字段（答案见下方「预期结果」）：`transposeB`、`outDataType`、`kvHeadNum`、`qkScale`、`inputLayout`、`maskType`、`windowSize`。

**需要观察的现象**：两个结构体都遵循 4.1 的统一骨架（默认值字段 + 末尾 `rsv`）；`SelfAttentionParam` 因能力更多而嵌套了 8 个枚举，`LinearParam` 仅 2 个。

**预期结果**（关键含义）：

| 字段 | 含义 |
|------|------|
| `LinearParam::transposeB` | 权重是否转置参与矩阵乘，默认 true（权重按 `[out,in]` 给） |
| `LinearParam::outDataType` | 输出类型；`ACL_DT_UNDEFINED`=浮点，否则为量化反量化出口类型 |
| `SelfAttentionParam::kvHeadNum` | KV 头数；0 表示与 query 头数相等（MHA），非 0 为 GQA/MQA |
| `SelfAttentionParam::qkScale` | 在 Q·K^T 之后乘的缩放系数（常对应 1/√d） |
| `SelfAttentionParam::inputLayout` | Q/K/V 排布，复用公共枚举 `InputLayout`（BSND/BNSD） |
| `SelfAttentionParam::maskType` | 注意力 mask 形态（倒三角/alibi/滑窗/内部生成…） |
| `SelfAttentionParam::windowSize` | >0 开启 SWA 滑动窗口，限制 KV Cache 长度以省显存 |

运行结果**待本地验证**（本实践为源码阅读型，无需运行；若要运行算子需昇腾环境）。

#### 4.4.5 小练习与答案

**练习 1**：`SelfAttentionParam::QuantType` 与命名空间作用域的 `atb::infer::QuantType` 同名不同义，如何在不看注释时区分一个 `quantType` 字段用的是哪一个？

> **答案**：看它**所属的作用域**。若字段声明为 `QuantType quantType;` 且结构体内部定义了 `enum QuantType`，则用的是结构体内部的版本（如 `TYPE_QUANT_QKV_ONLINE`）；若结构体内部未定义 `QuantType`（如归一化算子），则用的是命名空间作用域版本（如 `QUANT_INT8`）。C++ 的名称隐藏规则决定了内层枚举会遮蔽外层同名枚举。

**练习 2**：`SelfAttentionParam` 里同时有 `qScale` 和 `qkScale` 两个缩放系数，它们作用点有何不同？

> **答案**：`qScale` 作用在 **Q 本身**（在参与点积之前对 Q 做缩放）；`qkScale` 作用在 **Q·K^T 的结果**之后（即 softmax 之前的 score 缩放，常用于体现 1/√d）。两者作用阶段不同，故分开声明。

---

### 4.5 common 命名空间与通信参数复用模式

#### 4.5.1 概念说明

除了 `infer`，ATB 还有一个 `common` 命名空间（定义在 `common_op_params.h`），存放**跨推理/训练/图算子通用**的 Param——目前主要是控制流与流同步用的 `EventParam`、`IfCondParam`。它们不对应某个具体计算算子，而是服务于「图算子里做条件分支、做流间同步」这类通用控制需求，故单独成文件、供所有场景复用。

与此同时，回看 `infer_op_params.h` 里的**通信算子**（`AllReduceParam`、`AllGatherParam`、`BroadcastParam`、`ReduceScatterParam`、`SendParam`、`RecvParam`、`AllToAllParam`…），你会发现它们共享一套高度相似的「通信七件套」字段。识别这套模板，就能举一反三读懂所有通信算子的 Param。

#### 4.5.2 核心流程：通信 Param 的复用模板

几乎所有通信 Param 都长这样（字段顺序也基本一致）：

```text
struct XxxCommParam {
    int rank = 0;                 // 本卡编号
    int rankSize = 0;             // 通信域总卡数
    int rankRoot = 0;             // 主卡编号
    std::string backend = "hccl"; // 通信后端："hccl" / "lccl" / ...
    HcclComm hcclComm = nullptr;  // 通信域指针(空则由 ATB 创建)
    CommMode commMode = COMM_MULTI_PROCESS; // 多进程/多线程
    std::string rankTableFile;    // 集群拓扑配置文件路径
    std::string commDomain;       // 通信域名标识(多通信域时用)
    // ... 个别算子的专属字段 ...
    uint8_t rsv[64] = {0};
};
```

`backend` 字段决定底层用 HCCL（CANN 官方集合通信库）还是 LCCL（轻量通信库），是通信算子最关键的模式开关。

#### 4.5.3 源码精读

**`common::EventParam`** —— [include/atb/common_op_params.h:38-61](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/common_op_params.h#L38-L61)，用于在流之间做同步（Record 一个 Event / Wait 一个 Event）：

```cpp
struct EventParam {
    enum OperatorType : int {
        UNDEFINED = 0,  // 不做任何操作
        RECORD,         // 在 Stream 中记录一个 Event
        WAIT            // 阻塞 Stream，直至指定 Event 完成
    };
    aclrtEvent event;
    OperatorType operatorType = UNDEFINED;
    uint8_t rsv[16] = {0};
};
```

注意它同样遵守 4.1 的统一骨架：嵌套枚举 + 业务字段 + `rsv`，且定义了不比较 `rsv` 的 `operator==`（[common_op_params.h:71-74](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/common_op_params.h#L71-L74)）——证明 `common` 与 `infer` 共享同一套 Param 约定。

**`common::IfCondParam`** —— [include/atb/common_op_params.h:81-102](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/common_op_params.h#L81-L102)，供图算子做条件分支：持有一个回调 `handle` 与两个分支 `opA`/`opB`，回调返回 true 走 opA、false 走 opB。这里 Param 里出现了 `Operation *` 指针成员，属于控制流专有字段。

**通信七件套实例**：以 `AllReduceParam` 为例，[include/atb/infer_op_params.h:1163-1204](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1163-L1204)：

```cpp
struct AllReduceParam {
    enum QuantType : int { QUANT_TYPE_UNQUANT=0, QUANT_TYPE_PER_TENSOR, ... }; // 又一个重定义的 QuantType
    int rank = 0;
    int rankSize = 0;
    int rankRoot = 0;
    std::string allReduceType = "sum";   // 本算子专属：sum/prod/max/min
    std::string backend = "hccl";
    HcclComm hcclComm = nullptr;
    CommMode commMode = COMM_MULTI_PROCESS;
    std::string rankTableFile;
    std::string commDomain;
    QuantType quantType = QUANT_TYPE_UNQUANT;
    aclDataType outDataType = ACL_DT_UNDEFINED;
    uint8_t rsv[64] = {0};
};
```

`AllGatherParam`（[L1040-L1071](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1040-L1071)）、`BroadcastParam`（[L1237-L1265](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1237-L1265)）、`SendParam`/`RecvParam`（[L2311-L2382](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L2311-L2382)）字段结构几乎相同，差别只在「本算子专属的那一两个字段」（如 `allReduceType`、`destRank`/`srcRank`）。

#### 4.5.4 代码实践

**目标**：验证「通信七件套」是跨算子的通用模板。

**操作步骤**：

1. 在 `infer_op_params.h` 中分别打开 `AllReduceParam`、`AllGatherParam`、`BroadcastParam` 三个结构体。
2. 逐一核对它们是否都含 `rank/rankSize/rankRoot/backend/hcclComm/commMode/rankTableFile/commDomain` 这 8 个字段，并记录每个算子的**专属字段**与 `backend` 默认值。
3. 思考：为什么这些字段不抽成一个公共基类结构体，而要在每个算子里重复声明？

**需要观察的现象**：三个结构体的通信字段几乎逐字一致，仅专属字段不同；`backend` 默认值大多是 `"hccl"`，但 `ReduceScatterParam` 的默认是 `"lccl"`（[L1298](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1298)）。

**预期结果**：确认通信七件套模板成立。不抽基类的原因：Param 必须保持 POD（可整体按字节拷贝 / 序列化 / pybind 暴露），且每个算子的 `rsv` 长度、专属字段、`operator==` 都不同，扁平重复反而最简单稳妥。运行结果**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`EventParam` 里 `RECORD` 和 `WAIT` 各自的作用是什么？为什么要成对使用？

> **答案**：`RECORD` 在某个流上记录一个完成事件（不打断流的执行），`WAIT` 让另一个流阻塞直至该事件完成。成对使用即可实现「流 A 算完之后，流 B 才能开始」的跨流依赖，是多流并行时做同步的核心手段（呼应 u1-l5 的多流话题）。

**练习 2**：`AllReduceParam` 内部也定义了一个 `QuantType`，它与命名空间作用域的 `QuantType` 取值有何不同？

> **答案**：`AllReduceParam::QuantType` 的取值是 `QUANT_TYPE_UNQUANT/QUANT_TYPE_PER_TENSOR/QUANT_TYPE_PER_CHANNEL`（针对通信的量化粒度），而命名空间作用域的 `atb::infer::QuantType` 取值是 `QUANT_UNQUANT/QUANT_INT8/...`（针对算子计算的量化比特宽度）。同名却各自服务于「通信量化」与「计算量化」两个不同语境，这正是 4.2.2 提到的「同名 QuantType 坑」的又一例。

---

## 5. 综合实践

设计一个把本讲全部要点串起来的**配置单编写**任务（纯源码阅读型，无需 NPU）：

**场景**：你要为一个 decoder-only 大模型配置两个核心算子——MLP 的投影层（`Linear`）和因果注意力（`SelfAttention`）。请仅凭 `infer_op_params.h` 完成：

1. **查字段**：写出 `LinearParam` 与 `SelfAttentionParam` 各自的「嵌套枚举清单」与「业务字段清单」，并标出哪些字段复用了 4.2 的公共枚举。
2. **做选择**：针对以下需求，给出每个字段应填的值（或保持默认的理由）：
   - Linear：权重按 `[out,in]` 存储、需要偏置、浮点计算（不量化）。
   - SelfAttention：标准 MHA（KV 头数 = query 头数）、因果 mask（倒三角）、BSND 排布、KV Cache 走默认 `K_CACHE_V_CACHE`、不开启 SWA。
3. **避坑检查**：指出下列两个错误写法各违反了哪条约定/约束：
   - (a) 把 `param.rsv[0] = 1;` 后调用 `CreateOperation`。
   - (b) 在 `SelfAttentionParam` 里把 `quantType` 设成 `QUANT_INT8`（命名空间作用域的值）。

**参考要点**：

- (1) Linear 嵌套 `MatmulType`/`QuantMode`；SelfAttention 嵌套 `CalcType`/`KernelType`/`ClampType`/`MaskType`/`KvCacheCfg`/`ScaleType`/`QuantType`/`CacheType`。`inputLayout` 复用公共枚举 `InputLayout`。
- (2) Linear：保持 `transposeB=true`、`hasBias=true`、`outDataType=ACL_DT_UNDEFINED`（默认即可）；SelfAttention：`kvHeadNum=0`（MHA）、`maskType=MASK_TYPE_NORM`、`inputLayout=TYPE_BSND`（默认）、`kvcacheCfg=K_CACHE_V_CACHE`（默认）、`windowSize=0`（默认，不开启 SWA）。
- (3) (a) 违反「`rsv` 必须全 0」，会被 `OPERATION_PARAM_FUNCS` 拒绝并返回 `ERROR_INVALID_PARAM`（见 4.1.3）；(b) 类型不匹配——`SelfAttentionParam::quantType` 用的是结构体内部的 `QuantType`，其有效值是 `TYPE_QUANT_UNQUANT` 等，`QUANT_INT8` 属于另一个 `QuantType`，编译期/校验期都会出错（见 4.4.3）。

运行结果**待本地验证**。

## 6. 本讲小结

- 所有 `XxxParam` 遵循统一骨架：**带默认值的 POD 字段 +（按需的）嵌套枚举 + 末尾 `uint8_t rsv[N]`**；算子名与 Param 名一一对应（`Linear`→`LinearParam`）。
- `rsv` 是「必须全 0」的预留字段，充当**版本兼容闸门**：`OPERATION_PARAM_FUNCS` 宏在 `CreateOperation`/`UpdateOperationParam` 入口逐字节检查，非 0 即返回 `ERROR_INVALID_PARAM` 并提示检查编译版本。
- `atb::infer` 命名空间有四个公共枚举：`InputLayout`（BSND/BNSD）、`QuantType`（量化比特宽度）、`ActivationType`（激活函数种类）、`CommMode`（多进程/多线程），被大量算子复用。
- 复杂 Param（如 `SelfAttentionParam`）用**嵌套枚举**把多维度开关分类管理；同名 `QuantType` 会在不同结构体里被重定义、取值不同，读 `quantType` 字段务必确认其所属作用域。
- `LinearParam` 用「字段组合」表达多行为（普通乘/量化/爱因斯坦求和/原地累加），`outDataType=ACL_DT_UNDEFINED` 兼任「浮点/量化」开关。
- `atb::common` 命名空间（`EventParam`/`IfCondParam`）提供控制流与流同步通用 Param；通信算子则共享一套「rank/rankSize/rankRoot/backend/hcclComm/commMode/rankTableFile/commDomain + `rsv[64]`」的七件套模板。

## 7. 下一步学习建议

- **横向收尾**：本讲是「算子调用实战」单元（u2）的最后一篇。至此你已掌握 C++ 调用（u2-l1）、Python 调用（u2-l2）、参数体系（本讲）三件套，可以举一反三地调用任意推理算子了。
- **纵向深入框架**：若想知道 `CreateOperation` 创建出的算子对象，其 `Setup`/`Execute` 内部到底怎么跑到 Kernel，进入第 3 单元：从 u3-l1（OperationBase 模板基类）→ u3-l2（Runner 执行单元）→ u3-l3（AclnnRunner 适配 CANN）依次读。本讲反复出现的 `OPERATION_PARAM_FUNCS`、`ParamCheck` 正是挂在 `OperationBase` 这条链路上的。
- **按算子专题深读**：第 4 单元会逐族精讲关键算子——Linear 族（u4-l1）、Normalization（u4-l2）、Self-Attention 与 KV Cache（u4-l4、u4-l5）、RoPE（u4-l6）、MLA（u4-l7）、采样/MoE（u4-l8）。届时你会再次回到本讲提到的这些 Param，结合执行链路理解每个字段在 Kernel 层的真实效果。
- **自定义算子**：若你想**自己加一个 Param**，第 6 单元（u6-l3、u6-l4）会讲如何定义 Param、写 `param_to_json` 序列化、在 `atb_ops_info.ini` 声明输入输出规格——本讲的 `rsv` 约定与命名规范就是那时必须遵守的硬性规则。
