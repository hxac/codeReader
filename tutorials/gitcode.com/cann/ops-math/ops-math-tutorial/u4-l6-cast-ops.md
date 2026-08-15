# 类型转换算子 cast：模板化 tiling 与性能重构

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 cast 算子「表驱动 + 编译期模板」的双层分发机制：host 侧用 `castMap` 表在运行时校验类型组合，device 侧用同一张表生成的 `GetCastPolicy` 特化在编译期选定 `CAST_TEMPLATE_*` 实现版本。
2. 掌握 2025 年 cast 性能重构后 host/device 共享的普通结构体 `CastTilingData`（仅 `shapeSize` / `coreNum` / `ubFormer` 三个字段），以及它与旧版十余个字段 tiling 数据的差异和设计取舍。
3. 理解 kernel 侧如何通过 `ComputeCastDerivedTiling` 自行推导 `blockFormer`、`ubLoop`、`ubTail` 等分块循环参数，而不再依赖 tiling 传入。
4. 能列举至少两种 `CAST_TEMPLATE_*` 模板常量及其适用场景（如 direct、two-cast、micro 系列的 deinter/interleave 等）。

本讲是单元四的第 4 篇，承接 u4-l3（形态变换类算子）与 u3-l3（tiling 框架与模板注册）的认知：u3-l3 讲的是「多个算子复用一个 tiling 模板」，本讲讲的是「一个算子内部按类型组合复用十余个 kernel 模板」——同一套注册思想的另一个方向。

## 2. 前置知识

阅读本讲前，请先确认理解以下概念（在前几讲已建立，此处只做针对性回顾）：

- **tiling 与 TilingBaseClass**：tiling 在 host 侧执行，负责把总数据量切分成多个核（block）、每个核若干次 UB 迭代。`Ops::Base::TilingBaseClass` 把这个过程固化为 7 个有序回调（GetPlatformInfo → GetShapeAttrsInfo → DoOpTiling → … → PostTiling），见 u2-l4。
- **tiling key 与模板参数**：CANN 的 kernel 二进制可以按「tiling key + 模板参数」编译出多个版本，运行时按 tiling 阶段设置的 key 选择执行哪一份。`REGISTER_OPS_TILING_TEMPLATE` 即把 tiling 类注册进模板（u3-l3）。
- **UB / VL / 寄存器（Reg）**：UB 是 Vector 核上的片上缓存；VL（向量寄存器位宽，本算子代码中 `VL_BIT_SIZE = 2048` bit）是一次向量指令能处理的比特数；MicroAPI 的 `Reg::DataCopy` / `Reg::Cast` / `Reg::Interleave` 等是在向量寄存器层面操作的细粒度指令，比传统 `AscendC::Cast`（UB 级）开销更低（u2-l5、u3-l2）。
- **Interleave / DeInterleave（交织/解交织）**：`Reg::Interleave` 把一个寄存器的元素与另一个寄存器（通常是全零）交错排列成两个寄存器；`DeInterleave` 是反操作。类型转换中它们用于「窄类型拓宽」时插入 0 字节、或「宽类型压缩」前聚拢有效字节，是本讲 micro 模板命名的由来。
- **dst_type 属性**：cast 与 broadcast_to 的 `size` 类似，输出类型由属性（`int32_t` 编码的 acl DataType）决定，host 侧需校验属性与输出 tensor 类型一致。

## 3. 本讲源码地图

| 文件 | 作用 |
| ---- | ---- |
| [math/cast/README.md](https://github.com/gitcode.com/cann/ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/README.md) | 算子规格：支持的类型组合、精度约束（如 INT32→INT8 只保证 (-2048, 1920) 无误差） |
| [math/cast/op_host/arch35/cast_tiling.h](https://github.com/gitcode.com/cann/ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_host/arch35/cast_tiling.h) | `CastCompileInfo`、`CastMapSt`（类型转换策略表项）、`CastTiling` 类声明 |
| [math/cast/op_host/arch35/cast_tiling.cpp](https://github.com/gitcode.com/cann/ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_host/arch35/cast_tiling.cpp) | tiling 实现：平台信息获取、类型组合查表、`ubFormer`/核数计算、tiling 数据填充与注册 |
| [math/cast/op_kernel/arch35/cast_tiling_data.h](https://github.com/gitcode.com/cann/ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/arch35/cast_tiling_data.h) | 重构后的 host/device 共享普通结构体 `CastTilingData`（三个字段） |
| [math/cast/op_kernel/arch35/cast_struct.h](https://github.com/gitcode.com/cann/ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/arch35/cast_struct.h) | `CAST_TEMPLATE_*` / `CAST_MODE_REG_*` 常量定义、`CAST_POLICY_DEFINE` 宏与 `castMap` 策略表 |
| [math/cast/op_kernel/arch35/cast_impl.h](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/arch35/cast_impl.h) | 各模板的 kernel 实现：`CastDirect`/`CastDstBool`/`CastThrough`/`CastUint1`/`CastTwo`/`CastMicro` 及 `ComputeCastDerivedTiling` |
| [math/cast/op_kernel/cast_apt.cpp](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/cast_apt.cpp) | `__global__ __aicore__ void cast` 入口：按 `GetCastPolicy` 编译期分发到具体模板 |

注意一个细节：tiling 数据结构体放在了 `op_kernel/arch35/` 目录下而不是 `op_host/`，正是「host 与 device 共享同一份头文件」这一设计的物理体现——它已经不属于 host 专属。

## 4. 核心概念与源码讲解

### 4.1 cast tiling 类：从策略表到三个数字

#### 4.1.1 概念说明

cast 的 tiling 要回答三个问题：

1. **这次转换合不合法？** 27 种左右的数据类型两两组合有数百种，其中只有一部分被支持，且每种支持组合还带有各自的取整模式（RINT/TRUNC/ROUND…）、中间类型、寄存器搬运模式。这个「类型组合 → 策略」的映射被组织成一张**表**。
2. **用几个核？** 输入可能只有几十个元素，也可能上亿，不能无脑占满所有核。
3. **一次 UB 迭代处理多少元素？** 由 UB 容量、输入/输出（甚至中间类型）的位宽共同决定，且必须按 VL 对齐。

`CastTiling` 继承 `TilingBaseClass`，把这三个问题的答案最终浓缩成 `shapeSize`、`coreNum`、`ubFormer` 三个数字写入 tiling 数据。

#### 4.1.2 核心流程

`CastTiling` 的七个回调按固定顺序执行：

```text
GetPlatformInfo()   取 coreNum / ubSize / vlBitSize（有平台信息走 PlatformAscendC，否则走编译信息缓存）
        │
GetShapeAttrsInfo() 读 x/y 的 dtype 与 dst_type 属性：
        │           ① 校验 dst_type 属性 == y 的 dtype
        │           ② 在 castMap 表中查 (srcType, dstType) 组合 → policy_（查不到即报错）
        │           ③ 校验 INT4 输出时 last dim 能被 2 整除、x 与 y shape 相同、非空 tensor
        │           ④ 记录 shapeSize_
        ▼
DoOpTiling()        由 policy 位宽算 ubFormer_；由 shapeSize 与最小负载阈值算 usedCoreNum_
        ▼
GetTilingKey()      返回固定模板 key（GET_TPL_TILING_KEY(0)）
GetWorkspaceSize()  固定 16MB 最小 workspace
        ▼
PostTiling()        写入 CastTilingData 三字段；设置 blockDim = blockNum；SetTilingKey
```

核数计算采用「需求驱动、上限封顶」的策略：

\[
\text{coreNum} = \min\left(\left\lceil \frac{\text{shapeSize} \times \text{inBits}}{4\text{K} \times 8} \right\rceil,\ \text{coreNum}_{sys}\right)
\]

即按每个核至少分到约 4KB（以 bit 计 `PER_CORE_MIN_UB_BIT = 4 * 1024 * 8`）的输入数据来估算需要多少核，避免小 tensor 上百核空转。

`ubFormer` 的计算按模板类别分四种公式（直接转换/输出 BOOL/输入 UINT1/两次转换），以最常见的直接转换为例：

\[
\text{ubCap} = \frac{(\text{ubSize} - \text{reserve}) \times 4}{\text{inBits} + \text{outBits}}, \qquad
\text{ubFormer} = \left\lfloor \frac{\text{ubCap}}{\text{alignNum}} \right\rfloor \times \text{alignNum}
\]

其中 `alignNum = vlBitSize / inBits` 保证 ubFormer 是单次向量指令处理量的整数倍，`×4` 是因为输入输出队列各配 `bufferNum_ = 2` 份双缓冲。

#### 4.1.3 源码精读

**tiling 类声明与策略表项**。[cast_tiling.h:31-59](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_host/arch35/cast_tiling.h#L31-L59) 定义了 `CastMapSt`：每一项含源/目的/中间类型、模板 id、两个取整模式和寄存器搬入/搬出模式——一张表项就是一条「转换路径说明书」。而 [cast_tiling.h:61-97](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_host/arch35/cast_tiling.h#L61-L97) 是 `CastTiling` 类：注意成员变量里 `usedCoreNum_` 和 `ubFormer_` 是**类的普通成员**（而非旧版直接持有的 `CastTilingData tilingData_` 成员），这是重构后「先算数字、最后一次性序列化」的写法。

**查表校验类型组合**。[cast_tiling.cpp:155-168](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_host/arch35/cast_tiling.cpp#L155-L168) 用 `std::find_if` 在 `castMap` 数组里按 (srcType, dstType) 查找策略项，查到则存入 `policy_`，查不到则报「不支持该转换」错误——这张表同时承担了**合法性校验**和**策略选择**双重职责。在此之前，[cast_tiling.cpp:147-153](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_host/arch35/cast_tiling.cpp#L147-L153) 还校验了 `dst_type` 属性必须与输出 tensor 的 dtype 一致。

**ubFormer 按模板分档计算**。[cast_tiling.cpp:223-261](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_host/arch35/cast_tiling.cpp#L223-L261) 中 `GetUbFormer` 按模板 id 分四档：普通/微模板系列按输入+输出位宽分 UB；输出 BOOL 的分母换成 `outBits + 13`（因为需要额外的 mask/全零 buffer）；输入 UINT1 的按输出位宽的 3/2 倍折算；两次转换（TWO_CAST）则要**额外计入中间类型位宽**（[cast_tiling.cpp:250-260](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_host/arch35/cast_tiling.cpp#L250-L260)）——UB 里要同时容纳输入、中间、输出三份 buffer。

**核数与写入 tiling**。[cast_tiling.cpp:324-333](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_host/arch35/cast_tiling.cpp#L324-L333) 是需求驱动的核数计算（公式见上）。[cast_tiling.cpp:363-378](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_host/arch35/cast_tiling.cpp#L363-L378) 的 `PostTiling` 只往 `CastTilingData` 写三个字段，然后就地计算 `blockFormer`（向上取整到 8 的倍数）和 `blockNum`，用 `SetBlockDim(blockNum)` 启动相应数量的核。**注意 `blockFormer`/`blockNum` 只用于设置 blockDim，并没有写进 tiling 数据**——kernel 会自己重算这两个值（见 4.2）。

**注册**。[cast_tiling.cpp:406-408](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_host/arch35/cast_tiling.cpp#L406-L408) 同时完成两件事：`IMPL_OP_OPTILING` 把 tiling 入口挂到 Cast 算子；`REGISTER_OPS_TILING_TEMPLATE(Cast, CastTiling, 1)` 把 tiling 类注册为 1 号模板——这正是 u3-l3 讲过的模板注册机制在本算子的应用。

#### 4.1.4 代码实践

**实践目标**：验证「需求驱动核数」与「ubFormer 按 VL 对齐」两条规则。

**操作步骤**：

1. 阅读 [cast_tiling.cpp:324-333](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_host/arch35/cast_tiling.cpp#L324-L333) 与 [cast_tiling.cpp:211-235](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_host/arch35/cast_tiling.cpp#L211-L235)，用纸笔推算两组例子（假设 `ubSize = 196608` 字节、`vlBitSize = 2048`、系统核数 50）：
   - 例 A：FP16 → FP32，shapeSize = 65536；
   - 例 B：FP32 → INT8，shapeSize = 1000。
2. 对比例 A/B，写出每组的 `usedCoreNum`、`alignNum`、`ubFormer`。
3. 若本地有 NPU 环境且已安装算子包，可运行 cast 的样例 [math/cast/examples/test_aclnn_cast.cpp](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/examples/test_aclnn_cast.cpp)，在 `DoOpTiling` 的 `OP_LOGD`（[cast_tiling.cpp:335-337](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_host/arch35/cast_tiling.cpp#L335-L337)）处打开 DEBUG 日志核对推算结果。

**需要观察的现象**：例 A 中 shapeSize × 16bit ÷ 32768bit ≈ 32 核（< 50，故用 32）；例 B 中 1000 × 32bit ÷ 32768bit 向上取整 = 1 核——小 tensor 只占 1 个核。

**预期结果**：手算值与日志中 `usedCoreNum`/`ubFormer` 一致；`ubFormer` 一定是 `alignNum` 的整数倍。无 NPU 环境时，纸笔推算部分即为完成，日志核对部分**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `GetUbFormer` 里 TWO_CAST 模板的分母是 `inBits + outBits + midBits` 三项之和，而 DIRECT_CAST 只有前两项？

**答案**：TWO_CAST 在 UB 中要同时为输入、输出**和中间类型**各分配 buffer（kernel 侧 `CastTwo::Init` 会 `InitBuffer` 三次，见 [cast_impl.h:753-755](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/arch35/cast_impl.h#L753-L755)），UB 预算必须覆盖三份；DIRECT_CAST 只需输入输出两份。

**练习 2**：`IsSimt()`（[cast_tiling.cpp:200-209](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_host/arch35/cast_tiling.cpp#L200-L209) 为何对 INT64→DOUBLE 的组合特殊处理？

**答案**：double 运算在部分硬件路径上走 SIMT（标量）通道而非向量通道，需要额外保留 32KB（`SIMT_RESERVED_SIZE`）UB 空间，所以检测到该组合时从 `ubSize_` 中扣除并调用 `SetLocalMemorySize` 更新（[cast_tiling.cpp:218-222](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_host/arch35/cast_tiling.cpp#L218-L222)）。

**练习 3**：tiling 阶段已经算出了 `blockFormer` 和 `blockNum`（PostTiling），为什么不同时把它们写进 tiling 数据？

**答案**：这是本次性能重构的核心取舍——`blockFormer`/`blockNum` 可由 `shapeSize`/`coreNum`/`ubFormer` 三个原始数字经确定性公式推出（kernel 侧 `ComputeCastDerivedTiling` 重算一遍），属于「派生值」。只传原始值可以：(1) 把 tiling 数据从 12 个字段缩到 3 个，减小序列化与下发开销（重构目标即「极致头开销」）；(2) 消除 host/device 两份公式不一致的风险由单一公式源承担。

### 4.2 共享 tiling 数据结构体：从 12 字段宏结构到 3 字段普通结构体

#### 4.2.1 概念说明

传统 CANN tiling 数据用 `BEGIN_TILING_DATA_DEF` / `TILING_DATA_FIELD_DEF` 宏定义，这套宏会生成带序列化元信息的类型，注册后由框架统一编解码。它的代价是：结构 heavyweight、字段一多下发开销大、且 host/device 需要各自的解码路径。

2025 年的 cast 性能重构（提交 `757326a26`，"cast性能优化 / 开启极致头开销"）把它换成了一个**没有任何框架宏的普通 C++ 结构体**，放在 kernel 目录下由 host tiling 与 device kernel **共同 include**。配合 `REGISTER_TILING_DEFAULT` / `GET_TILING_DATA_PTR_WITH_STRUCT`（见 cast_apt.cpp:89-90），tiling 数据以近乎 memcpy 的方式直读，这就是「极致头开销」的含义。

#### 4.2.2 核心流程

重构前后的字段对照：

| 重构前（宏定义 TilingData，12 字段） | 重构后（普通结构体，3 字段） |
| ---- | ---- |
| `blockNum`（启动核数） | `shapeSize`（元素总数，int64_t） |
| `ubFormer`（单次 UB 处理数） | `coreNum`（实际使用核数，int32_t） |
| `blockFormer`（整核处理数） | `ubFormer`（单次 UB 迭代元素数，int32_t） |
| `ubLoopOfFormerBlock` / `ubLoopOfTailBlock` | ↑ 由 kernel 侧 `ComputeCastDerivedTiling` 重算 |
| `ubTailOfFormerBlock` / `ubTailOfTailBlock` | ↑ 同上 |
| `regCopyInStep` / `regCopyOutStep` | ↑ 变为**编译期常量**（模板参数，host 无需下发） |
| `ubFormerRegLoop` / `ubTailOfFormerRegLoop` / `ubTailOfTailRegLoop` | ↑ kernel 侧按编译期 `OneLoopCopyInBitSize` 重算 |

精简遵循一条清晰的判据：**能在编译期确定的（regCopy 步长，取决于类型组合 → 模板参数）下沉到编译期；能在 device 侧廉价重算的（各层循环/尾数，只需三个原始数字）下沉到 kernel；只有真正依赖运行时 shape 与硬件资源的（shapeSize、coreNum、ubFormer）才走 tiling 通道下发。**

#### 4.2.3 源码精读

**新结构体全貌**。[cast_tiling_data.h:21-27](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/arch35/cast_tiling_data.h#L21-L27)：一个只依赖 `<cstdint>` 的 plain struct，注释明确写着 "plain struct, shared by host tiling and device kernel"。

**旧结构体的模样（git 历史）**。在提交 `757326a26` 之前，[cast_tiling.h](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_host/arch35/cast_tiling.h) 中是：

```cpp
// 以下为 757326a26^（重构前）的代码，通过 git show 757326a26 可见，现已在仓库中删除
BEGIN_TILING_DATA_DEF(CastTilingData)
TILING_DATA_FIELD_DEF(int64_t, blockNum);    // 启动多少核处理
TILING_DATA_FIELD_DEF(int64_t, ubFormer);    // 一次ub处理的个数，开db后ub按照一半算
TILING_DATA_FIELD_DEF(int64_t, blockFormer); // 整核处理的个数
TILING_DATA_FIELD_DEF(int64_t, ubLoopOfFormerBlock);
TILING_DATA_FIELD_DEF(int64_t, ubLoopOfTailBlock);
TILING_DATA_FIELD_DEF(int64_t, ubTailOfFormerBlock);
TILING_DATA_FIELD_DEF(int64_t, ubTailOfTailBlock);
TILING_DATA_FIELD_DEF(int64_t, regCopyInStep);  // ub搬入reg，ub的步长
TILING_DATA_FIELD_DEF(int64_t, regCopyOutStep); // reg搬出到ub，ub的步长
TILING_DATA_FIELD_DEF(int64_t, ubFormerRegLoop);
TILING_DATA_FIELD_DEF(int64_t, ubTailOfFormerRegLoop);
TILING_DATA_FIELD_DEF(int64_t, ubTailOfTailRegLoop);
END_TILING_DATA_DEF;
REGISTER_TILING_DATA_CLASS(Cast, CastTilingData);
```

同一提交还把 `CastTiling` 类里直接持有的 `CastTilingData tilingData_` 成员删掉，换成 `usedCoreNum_` / `ubFormer_` 两个标量，序列化推迟到 `PostTiling` 一步完成。

**kernel 侧的派生计算**。[cast_impl.h:216-237](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/arch35/cast_impl.h#L216-L237) 定义了 `CastDerivedTiling` 结构与 `ComputeCastDerivedTiling(shapeSize, coreNum, ubFormer)`：`blockFormer` 取 `(shapeSize + coreNum - 1)/coreNum` 再向上对齐到 8 的倍数，随后依次导出 blockNum、blockTail、每块的 UB 循环数与尾数。每个模板类的 `Init` 第一件事就是调用它（如 [cast_impl.h:279](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/arch35/cast_impl.h#L279)）。注意它的公式与 host 侧 `PostTiling` 中计算 blockDim 的两行（[cast_tiling.cpp:369-370](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_host/arch35/cast_tiling.cpp#L369-L370)）**完全一致**——这是该设计成立的前提：同一段公式在 host 用于启动核、在 device 用于划分数据。

**regCopy 步长的编译期化**。旧版 `regCopyInStep`/`regCopyOutStep` 是 tiling 运行时字段；重构后由 [cast_impl.h:180-214](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/arch35/cast_impl.h#L180-L214) 的 `GetUbCopyInStep` / `GetUbCopyOutStep` 以 `constexpr` 函数从「搬运模式 + 类型」推得，并在 kernel 入口处（[cast_apt.cpp:149-154](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/cast_apt.cpp#L149-L154)）直接作为模板非类型参数传给 `CastMicro`——同一类型组合的 kernel 二进制里步长是立即数。

**入口处的读取方式**。[cast_apt.cpp:89-90](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/cast_apt.cpp#L89-L90) 用 `REGISTER_TILING_DEFAULT(CastTilingData)` + `GET_TILING_DATA_PTR_WITH_STRUCT` 取出结构体指针，之后所有模板类拿到的都是同一个 `__tiling_data_ptr__ CastTilingData*`。

#### 4.2.4 代码实践

**实践目标**：亲手完成「重构前后 tiling 字段对比」，并理解 kernel 侧派生循环的重建逻辑。

**操作步骤**：

1. 在仓库根目录执行：

   ```bash
   git log --oneline -- math/cast            # 找到 cast 性能重构提交 757326a26
   git show 757326a26 -- math/cast/op_host/arch35/cast_tiling.h   # 查看旧版 CastTilingData 定义
   git show 757326a26 -- math/cast/op_kernel/arch35/cast_impl.h | head -200  # 查看旧版 kernel 如何消费这些字段
   ```

2. 列一张对照表：旧 12 字段中，哪些变成了编译期模板参数、哪些变成 kernel 侧 `ComputeCastDerivedTiling`/`ubFormerRegLoop_` 的重算值、哪些保留为下发字段。
3. 追踪一条链：`CastDirect::Init` → `ComputeCastDerivedTiling`（[cast_impl.h:279](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/arch35/cast_impl.h#L279)）→ `Process` 中按 `isLastBlockFlag` 选择 `ubLoopOfTailBlock` 还是 `ubLoopOfFormerBlock`（[cast_impl.h:330-334](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/arch35/cast_impl.h#L330-L334)），确认 kernel 不再依赖任何 tiling 下发的循环字段。

**需要观察的现象**：`git show` 的 diff 中，旧版 `CastTilingData` 宏块整体被删除；旧版 `cast_impl.h` 中所有 `tilingData->blockFormer`、`tilingData->ubLoopOfFormerBlock` 之类字段访问被 `derived_.blockFormer`、`derived_.ubLoopOfFormerBlock` 成员访问替换。

**预期结果**：能回答「kernel 如何自行计算分块循环」——`Init` 阶段调用 `ComputeCastDerivedTiling(shapeSize, coreNum, ubFormer)`，用与 host 完全相同的向上取整/对齐公式重建 blockFormer→blockNum→blockTail→各层 ubLoop/ubTail；regCopy 步长与 regLoop 则由编译期模板参数 `RegCopyInStep`/`OneLoopCopyInBitSize` 在 [cast_impl.h:905-925](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/arch35/cast_impl.h#L905-L925) 推出。

#### 4.2.5 小练习与答案

**练习 1**：`CastTilingData` 三个字段的类型为什么是 `int64_t` + 两个 `int32_t`，而不是全 `int64_t`？

**答案**：`shapeSize` 理论上可超过 2^31 个元素，必须 64 位；`coreNum` 与 `ubFormer` 的量级（核数 ≤ 数千、ubFormer ≤ UB 容量/最小类型宽度）远在 int32 范围内，用 int32 各省 4 字节，把整个结构压到 16 字节，符合「极致头开销」的最小化目标。

**练习 2**：把结构体放在 `op_kernel/arch35/` 而不是 `op_host/arch35/`，除了象征意义还有什么实际好处？

**答案**：kernel 侧编译单元（cast_apt.cpp、cast_impl.h）原本不能 include op_host 目录的 tiling 宏头（那会引入 host 侧依赖）；放到 kernel 目录后 host 的 cast_tiling.h 反过来 include 它（[cast_tiling.h:22](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_host/arch35/cast_tiling.h#L22)），依赖方向变为 host → 共享头 ← kernel，两边天然看到同一份定义，避免两份结构漂移。

**练习 3**：如果未来某新硬件 VL 位宽不是 2048，这套「host 算 ubFormer、device 推循环」的方案还成立吗？

**答案**：ubFormer 由 host 按 `vlBitSize_`（来自 `Ops::Base::GetVRegSize(context_)`，[cast_tiling.cpp:67](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_host/arch35/cast_tiling.cpp#L67)）动态计算后**下发**，device 侧的 `VL_BIT_SIZE = 2048` 只用于推导 regCopy 步长等编译期量——后者按架构目录（arch35）隔离，新架构建自己的 arch 目录即可。方案依然成立，但要注意 host 下发的 ubFormer 与该架构编译期 VL 必须一致，这属于跨架构移植时需要核对的点。

### 4.3 cast kernel 模板实现：一张表、十六个模板、编译期分发

#### 4.3.1 概念说明

cast 的 kernel 面临的组合爆炸问题：约 27 种类型 × 27 种类型，其中支持的组合有 200 多条，每条的「最优寄存器级路径」都不同。cast 的解法是**两级分发**：

- **第一级（表驱动，host 运行时 + device 编译期同源）**：`cast_struct.h` 中每个支持的类型组合用一条 `CAST_POLICY_DEFINE(...)` 声明。这个宏是**双面的**：在 host 编译单元里展开为 `castMap[]` 数组的初始化项（运行时查表校验/选策略），在 device 编译单元（`__CCE_AICORE__`）里展开为 `GetCastPolicy<ST, DT>` 模板特化（编译期取策略）。一份声明，两个世界。
- **第二级（kernel 入口编译期分发）**：kernel 入口 `cast<id>` 以 `ORIG_DTYPE_X`/`ORIG_DTYPE_Y` 为模板参数编译出 N 份二进制；每份里 `GetCastPolicy<ORIG_DTYPE_X, ORIG_DTYPE_Y>` 的所有字段都是 `constexpr`，`if constexpr` 链在编译期就只保留一个模板类的实例化代码。

十六个 `CAST_TEMPLATE_*` 常量（[cast_struct.h:20-35](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/arch35/cast_struct.h#L20-L35)）按实现风格分三档：

| 档 | 模板 | 思路 | 典型组合 |
| ---- | ---- | ---- | ---- |
| 传统 UB 级 | `DIRECT_CAST`(1)、`TWO_CAST`(5) | 用 `AscendC::Cast` 在 UB 张量上直接转/经中间类型转两次 | FP16↔FP32、BF16→FP8（经 FP32） |
| 特判 | `DST_BOOL`(2)、`THROUGH`(3)、`SRC_UINT1`(4) | 输出 BOOL 用 Compare+Select；同宽符号变换纯搬运；UINT1 输入按位展开 | FP32→BOOL、INT32→UINT32、UINT1→FP16 |
| Micro/Reg 级（11 个） | `MIRCRO_INOUT`(6)、`MIRCRO_CAST`(7)、`MIRCRO_CAST_INTER`(8)、`MIRCRO_CAST_DEINTER`(9)、`MIRCRO_CAST_CAST_DEINTER`(10)、`MIRCRO_CAST_CAST`(11)、`MIRCRO_CAST_INTER_CAST`(12)、`MIRCRO_CAST_DEINTER_CAST`(13)、`MIRCRO_CAST_CAST_DEINTER_CAST`(14)、`MIRCRO_CAST_INTER_CAST_CAST`(15)、`MIRCRO_DEINTER_SHIFT`(16) | 数据搬进向量寄存器后，用 `Reg::Cast` + `Interleave`/`DeInterleave` + 打包搬运（`LoadDist`/`StoreDist`）在寄存器内完成转换与位打包，一次微指令循环处理一个或两个 VL | 降精度（INT32→INT8 走 PACK4_B32 搬出）、FP4/FP8↔宽类型（UNPACK4_B8 搬入）、INT32→INT4（移位打包） |

模板名中的 `INTER`/`DEINTER` 即 4.2 前置知识里的交织/解交织：**变宽（窄→宽）插零用 Interleave，变窄（宽→窄）聚拢用 DeInterleave**；`CAST_CAST` 表示链式两次 `Reg::Cast`（经中间类型）；`SHIFT` 表示用移位与位运算手工打包 4bit 类型。

#### 4.3.2 核心流程

以一条 micro 模板路径 FP32 → INT8（策略见 [cast_struct.h:386-387](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/arch35/cast_struct.h#L386-L387)）为例：

```text
编译期（该类型组合的 kernel 二进制内）：
  GetCastPolicy<DT_FLOAT, DT_INT8> →
    id=MIRCRO_CAST, 映射类型 FP32→INT32, castMode1=TRUNC,
    copyIn=NORM, copyOut=PACK4_B32
  ToLoadDist/ToStoreDist/ToRoundMode 把整数常量翻译成枚举
  GetUbCopyInStep/GetUbCopyOutStep 算出 regCopyInStep/OutStep（立即数）
        │ 实例化 CastMicro<MIRCRO_CAST, float, int8_t, float, int32_t, int32_t, ...>
        ▼
运行时（每个核）：
  Init:   ComputeCastDerivedTiling → 本核 GM 偏移、UB buffer、derived_ 循环参数
          ubFormerRegLoop_ = ⌈ubFormer×32bit / 2048bit⌉   （每微指令循环处理一个 VL）
  Process: 对每个 UB 迭代：
    CopyIn:  DataCopyPad 从 GM 搬 ubFormer 个 FP32 进 UB
    Compute: for regLoop 次：
               Reg::DataCopy(UB→vreg, POST_MODE_UPDATE)   // 自动步进
               Reg::Cast<int32_t, float, trait>(vregOut, vregIn)  // 截断取整
               Reg::DataCopy<vreg→UB, DIST_PACK4_B32>(…)  // 32bit 结果随路压成 4×8bit 打包写出
    CopyOut: DataCopyPad 从 UB 搬回 GM
```

寄存器搬运模式 `LoadDist`/`StoreDist` 的直觉：向量的「逻辑布局」（每元素 32bit）与「物理存储」（4 个 8bit 挤在 32bit 里）不一致时，`DIST_PACK4_B32` 让**搬运指令在搬出时顺带完成位压缩**，省掉额外的 pack 指令——这是 micro 模板比传统模板快的关键之一。

#### 4.3.3 源码精读

**双面宏**。[cast_struct.h:103-129](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/arch35/cast_struct.h#L103-L129) 是整个机制的枢纽：同一个 `CAST_POLICY_DEFINE`，在 `#ifdef __CCE_AICORE__`（device 编译）下展开为 `GetCastPolicy` 特化，否则展开为 `castMap[]` 的初始化项。host 的 `GetShapeAttrsInfo` 查表与 device 的编译期分发因此永远指向同一份数据。

**模板与搬运模式常量**。[cast_struct.h:20-50](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/arch35/cast_struct.h#L20-L50) 集中定义了 16 个 `CAST_TEMPLATE_*` 和 12 个 `CAST_MODE_REG_*`（后者会被 [cast_apt.cpp:21-58](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/cast_apt.cpp#L21-L58) 的 `ToLoadDist`/`ToStoreDist` 映射为 `Reg::LoadDist`/`Reg::StoreDist` 枚举）。部分模板旁有注释直接点明用途，如 `CAST_TEMPLATE_MIRCRO_CAST_CAST 11 // f4 <-> f16`、`CAST_TEMPLATE_MIRCRO_DEINTER_SHIFT 16 // int32 -> int4`。

**策略表节选（micro 系列的注释很有教学价值）**：

- [cast_struct.h:403-417](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/arch35/cast_struct.h#L403-L417)：`MIRCRO_CAST_INTER`（bf16→complex64，"一次交织，搬出是 2 个 VL"）与 `MIRCRO_CAST_CAST_DEINTER` 的注释完整描述了「bf16 搬入 reg → cast 到 fp32 → 交织 → cast 到 int64 → DeInterleave 转 int32 → pack4 搬出随路转 uint8」的多级流水线。
- [cast_struct.h:447-459](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/arch35/cast_struct.h#L447-L459)：`MIRCRO_CAST_CAST_DEINTER_CAST` 注释言明「内定了转换路径 f8 → f32 → bf16 → f4」——参数表无法完全表达的极特化路径，直接写死在 kernel 里。
- [cast_struct.h:475-477](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/arch35/cast_struct.h#L475-L477)：`MIRCRO_DEINTER_SHIFT`（int32→int4）不走 Cast 指令，而是 DeInterleave + And(0xF) + ShiftLefts(4) + Or 手工打包。

**kernel 入口分发**。[cast_apt.cpp:86-110](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/cast_apt.cpp#L86-L110)：`cast<id>` 入口先 `GET_TILING_DATA_PTR_WITH_STRUCT` 取 tiling，再把 `GetCastPolicy<ORIG_DTYPE_X, ORIG_DTYPE_Y>` 的全部策略字段装进 `constexpr` 局部变量，并用 `TypeGetTool`（[cast_impl.h:29-115](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/arch35/cast_impl.h#L29-L115)）把整数类型码映射为真实 C++ 类型别名——例如 INT8→UINT32 组合的映射类型是 `CAST_TPL_UINT32`（即 `uint32_t`），因为 micro 路径里 int8 数据会被当作 uint32 打包体处理。

**if constexpr 分发链**。[cast_apt.cpp:110-160](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/cast_apt.cpp#L110-L160)：五档分发（DIRECT/DST_BOOL/THROUGH/SRC_UINT1/TWO_CAST/micro 系列），每档实例化对应模板类并立刻 `Init + Process`。由于外层全是 `if constexpr`，最终二进制里每个类型组合只含一个模板的代码。不支持的组合 `isValid_` 为 false，直接 return（[cast_apt.cpp:93-95](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/cast_apt.cpp#L93-L95)）。

**代表性模板实现**：

- `CastDirect`（传统路径，[cast_impl.h:311-319](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/arch35/cast_impl.h#L311-L319)）：Compute 就一句 `Cast<DT, ST>(yLocal, xLocal, rMode_, len)`——UB 级一条指令。
- `CastTwo`（[cast_impl.h:781-802](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/arch35/cast_impl.h#L781-L802)）：先转到中间类型 `MT`，再从 `MT` 转到 `DT`；对 uint32 中间类型还有 `ReinterpretCast<int32_t>` 的特判（无符号到 int64 的精度陷阱）。
- `CastMicro::ComputeCast`（[cast_impl.h:1039-1096](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/arch35/cast_impl.h#L1039-L1096)）：micro 路径的标准形态——`Reg::CastTrait`（布局/饱和/mask 合并/取整四元组）按类型组合用 `if constexpr` lambda 在编译期选定，循环体内 DataCopy→Cast→DataCopy 三连，指针由 `POST_MODE_UPDATE` 自动步进。
- `CastMicro::ComputeCastDeinter`（[cast_impl.h:1146-1197](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/arch35/cast_impl.h#L1146-L1197)）：变宽转窄的样本——搬入**两个** vreg，各自 Cast 后 `DeInterleave` 聚拢有效元素再打包搬出。
- `CastMicro::Process`（[cast_impl.h:1688-1735](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/arch35/cast_impl.h#L1688-L1735)）：外层 UB 循环 + 末尾尾块单独处理；对 FP4/INT4 这类「两个元素共用一字节」的类型，GM 偏移与 blockLen 都要除以 2（`xRealFormer`/`yRealFormer`，用 `#if ORIG_DTYPE_X == DT_FLOAT4_E2M1 ...` 编译宏分支）。

#### 4.3.4 代码实践

**实践目标**：读懂至少两种 `CAST_TEMPLATE_*` 模板的适用场景，并验证「策略表 ↔ kernel 分发」的对应关系。

**操作步骤**：

1. 打开 [cast_struct.h:20-35](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/arch35/cast_struct.h#L20-L35)，抄下 16 个模板常量及注释。
2. 在 `castMap` 中各找一条使用下列模板的策略项，并写出它的完整转换路径（结合注释）：
   - `CAST_TEMPLATE_MIRCRO_CAST`（提示：float→int8，[cast_struct.h:386-387](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/arch35/cast_struct.h#L386-L387)）
   - `CAST_TEMPLATE_MIRCRO_DEINTER_SHIFT`（提示：int32→int4，[cast_struct.h:475-477](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/arch35/cast_struct.h#L475-L477)）
3. 对第 2 步的每条策略，跳到 [cast_apt.cpp:110-160](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/cast_apt.cpp#L110-L160) 确认它会走进哪个 `if constexpr` 分支、实例化哪个模板类；再进 [cast_impl.h](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/arch35/cast_impl.h) 找到对应 `ComputeXxx` 函数，数一数循环体内的 Reg 指令条数。
4. 若本地有 NPU 环境，运行 [math/cast/examples/test_aclnn_cast.cpp](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/examples/test_aclnn_cast.cpp) 样例（README 中标注的 aclnnCast 调用方式），把输入类型改为 FP32、`dst_type` 改为 INT8，验证走 MIRCRO_CAST 路径的结果正确性。

**需要观察的现象**：每条策略项的 8 个数字（id、src/dst/mid 映射类型、两个 castMode、两个 regCopy 模式）与 kernel 入口取出的 8 个 `constexpr` 变量一一对应；FP32→INT8 的 Compute 循环体只有 3 条 Reg 指令（搬入、Cast、打包搬出）。

**预期结果**：能独立讲出「FP32→INT8：UNPACK 无需、NORM 搬入一个 VL 的 float，Reg::Cast 截断转 int32，DIST_PACK4_B32 搬出时把 4 个 int8 压进 32bit」这条完整路径。第 4 歋试错运行**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 BOOL 相关的转换不放进 micro 模板体系，而是单独立了 `DST_BOOL` / `THROUGH` 两个特判模板？

**答案**：转 BOOL 的语义是「非零为真」，没有对应的 Reg::Cast 路径，需要 `CompareScalar` 生成 mask 再 `Select` 填 0/1（[cast_impl.h:434-445](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/arch35/cast_impl.h#L434-L445)）；反之 BOOL/int8 等同宽**同模**类型间的「转换」本质是纯比特搬运（语义重解释），用 `THROUGH` 的 `Copy` 即可（[cast_impl.h:542-550](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/arch35/cast_impl.h#L542-L550)），连 Cast 指令都不需要。

**练习 2**：`MIRCRO_CAST_INTER`（交织）与 `MIRCRO_CAST_DEINTER`（解交织）分别在什么形状变化下使用？

**答案**：`INTER` 用于输出比输入**宽**的组合（如 bf16→complex64，实部虚部需交错插入，注释「搬出是 2 个 VL」，[cast_struct.h:403-405](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/arch35/cast_struct.h#L403-L405)）：Interleave 把 cast 结果与全零寄存器交织，实现插零拓宽；`DEINTER` 用于输出比输入**窄**的组合（如 float→uint8，[cast_struct.h:409-410](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/arch35/cast_struct.h#L409-L410)）：两个 vreg 各自 Cast 后 DeInterleave 聚拢有效低位元素，再配合 PACK 搬出压缩。

**练习 3**：策略表中 `mapDtypeX`（映射类型）与 `srcType`（原始类型）为什么经常不一致？举一例。

**答案**：映射类型是「这条 micro 路径中数据在寄存器里的实际处理宽度」，不等于逻辑类型。例如 INT32→INT8 的 `mapDtypeX = CAST_TPL_INT32` 而 dst 映射为 `CAST_TPL_INT32`（[cast_struct.h:339-340](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/arch35/cast_struct.h#L339-L340)），因为 4 个 int8 结果以打包的 uint32 形态在寄存器/UB 间流动；又如 FP4 输入的 `srcType` 是 `uint8_t`（两元素一字节，[cast_struct.h:390-391](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/arch35/cast_struct.h#L390-L391)）。`TypeGetTool` 就是把映射类型码翻译成这些 C++ 类型的查表器。

## 5. 综合实践

**任务：给 cast 加一条（纸面）新类型组合，并跑通重构考古。**

假设需要支持一个假想的 `INT32 → FLOAT16` 组合（现实中可查表确认是否已存在），完成以下闭环：

1. **查规格**：读 [math/cast/README.md](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/README.md) 的参数表与精度约束，确认 INT32 与 FLOAT16 均在支持列表。
2. **查现状**：在 `castMap`（[cast_struct.h](https://github.com/gitcode.com/cann-ops-math/blob/6f9460138514f3a9e8555b8e83a3a4daef666b93/math/cast/op_kernel/arch35/cast_struct.h)）中搜索 `DT_INT32, DT_FLOAT16`，确认现有策略（提示：本讲 4.3.5 练习 3 引用过的行附近即有一条，走 `TWO_CAST` 经 FP32）。
3. **纸面改造**：假设要改成走 `MIRCRO_CAST`（Reg 级一次转换 + PACK_B32 打包搬出），写出需要改动的位置清单：策略表一行 `CAST_POLICY_DEFINE`、（若映射类型变化）`TypeGetTool` 是否已覆盖、kernel 侧是否需要新 `ComputeXxx` 还是复用 `ComputeCast`、tiling 侧 `GetUbFormer` 分档是否受影响。**只写清单，不改代码。**
4. **重构考古**：执行 4.2.4 的 git 命令，把旧 12 字段 tiling 数据的「去向表」补全（编译期化 / kernel 重算 / 保留下发）。
5. **验证认知**：向自己复述一遍完整链路——`dst_type` 属性 → host 查表 → 三个数字下发 → kernel 编译期取策略 → `ComputeCastDerivedTiling` 重建循环 → `Reg::Cast` 微指令循环。

**预期产出**：一份策略改动清单 + 一张字段去向表 + 一段 200 字左右的链路复述。有 NPU 环境的读者可加做：编译仓库（参照 u1-l3 的 `bash build.sh`）确认阅读理解未破坏任何东西；运行结果**待本地验证**。

## 6. 本讲小结

- cast 用**一张双面策略表**同时解决 host 运行时校验与 device 编译期分发：`CAST_POLICY_DEFINE` 在 host 展开为 `castMap[]` 数组项，在 device 展开为 `GetCastPolicy<ST, DT>` 特化，单一数据源保证两侧一致。
- 16 个 `CAST_TEMPLATE_*` 常量把 200+ 条类型组合归并为三档实现：传统 UB 级（DIRECT/TWO_CAST）、特判（DST_BOOL/THROUGH/SRC_UINT1）、micro/Reg 级（11 个，按 Interleave/DeInterleave/多次 Cast/移位打包等寄存器级路径细分）。
- 2025 年性能重构把 tiling 数据从宏定义的 12 字段精简为 host/device 共享的普通结构体 `CastTilingData`（`shapeSize`/`coreNum`/`ubFormer`），判据是「编译期能定的下沉编译期、device 能廉价重算的下沉 kernel」。
- kernel 侧 `ComputeCastDerivedTiling` 用与 host `PostTiling` 完全一致的公式重建 blockFormer/blockNum/各层循环与尾数，regCopy 步长则变成 `CastMicro` 的模板非类型参数。
- 寄存器搬运模式（`LoadDist`/`StoreDist`，如 `DIST_PACK4_B32`、`DIST_UNPACK4_B8`）让搬运指令顺带完成位压缩/解包，是 micro 模板性能优势的关键；窄↔宽转换分别用 DeInterleave（聚拢）与 Interleave（插零）。
- 这套「表驱动 + 编译期模板 + 最小 tiling 数据」的组合是处理**组合爆炸类算子**的通用范式，可迁移到其他按类型/属性多路特化的算子设计上。

## 7. 下一步学习建议

- **下一讲 u4-l4**（随机类算子：drop_out_v3 与 stateless 系列）将离开「纯计算」领域，看带随机数状态与正反向配套组织的算子类别。
- 想继续深挖本讲主题，建议阅读：
  - `math/add/op_host/arch35/add_tiling_arch35.cpp`，对照 TilingBaseClass 七回调在「单一计算路径」算子上的朴素形态，体会 cast 的表驱动扩展；
  - `common/inc/op_host/math_tiling_templates_registry.h`，把 u3-l3 的「多算子共享 tiling 模板」与本讲「单算子多 kernel 模板」两种复用方向对照成体系；
  - `Reg::` 系列 MicroAPI 接口的官方文档（CANN 算子开发指南），理解 `CastTrait`/`POST_MODE_UPDATE`/`UpdateMask` 的完整语义；
  - `git log -- math/cast` 中其余提交（如 `1a280d6af` 将 MicroAPI 统一迁移为 Reg 接口），观察 micro 模板体系的演化史。
