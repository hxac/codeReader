# 公共 Tiling 设施：tiling_util、tiling_cache 与复用

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `common/inc/op_host` 与 `matmul/common/op_host` 两处公共 Host 侧设施分别提供了什么，以及 `CeilDiv`/`FloorAlign` 这类数学对齐工具的真实来源与复用链。
2. 理解 TilingCache 的工作原理：哈希键怎么构造、命中与未命中两条路径怎么走、为什么它能加速「相同 shape 重复执行」的场景。
3. 识别真实算子（quant_batch_matmul_v4、layer_norm_v3 等）是如何复用这些公共设施的：继承 `TilingBaseClass`、跨算子继承 tiling 类、注册多策略模板。
4. 亲手为 AddExample 接入一个最小 tiling 缓存，并设计验证命中路径的方法。

本讲是 u4-l1（切分算法）、u4-l2（TilingData/TilingKey 契约）之后的收尾篇：前两讲讲「怎么算 tiling」，本讲讲「别人已经算好的轮子在哪里、怎么用」。

## 2. 前置知识

- **Host 侧 tiling 回调**：框架在执行算子前回调 `*_tiling.cpp` 里注册的 tiling 函数，产出 TilingData（POD 字节契约）、BlockDim、TilingKey 三样交付件（见 u4-l1/u4-l2）。
- **纯函数与缓存的前提**：一个 tiling 函数如果只依赖「输入 shape/dtype/属性 + 平台信息（核数、UB 大小）」，那么同样的输入必然得到同样的结果——这叫确定性。确定性的计算才有资格被缓存。
- **哈希表与冲突**：缓存通常用「输入特征 → 哈希值 → 键」的方式索引。不同输入可能哈希到同一个键（哈希冲突），所以取值时还要做一次逐字段比对（`operator==`）兜底。
- **读写锁（RWLock / shared_mutex）**：多线程环境下，读操作可以并发（共享锁），写操作必须独占（排他锁）。tiling 缓存是进程级全局对象，必须加锁。
- **上板执行 vs 仿真**：本讲的实践默认在真实 Atlas 环境运行；无卡环境可参考 u8-l3 的 NPU Simulator。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [common/inc/op_host/tiling_util.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_host/tiling_util.h) | 公共小工具：标量 shape 归一化 `EnsureNotScalar`、Regbase 架构判断 `IsRegbaseSocVersion` |
| [matmul/common/op_host/math_util.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/matmul/common/op_host/math_util.h) | 对 CANN 包头文件 `util/math_util.h`（`Ops::Base::CeilDiv` 等的真身）的仓库侧包装层 |
| [common/inc/op_host/tiling_cache.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_host/tiling_cache.h) | 通用 tiling 缓存模板类（RWLock 版，命名空间 `Ops::NN::HostTiling`） |
| [matmul/common/op_host/op_tiling/tiling_cache.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/matmul/common/op_host/op_tiling/tiling_cache.h) | 另一份近似实现（`std::shared_mutex` 版，命名空间 `Ops::NN`），被真实算子实际引用 |
| [common/inc/op_host/cache_runinfo.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_host/cache_runinfo.h) + [common/src/op_host/cache_runinfo.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/src/op_host/cache_runinfo.cpp) | `CacheTilingContext`（整体保存/恢复 tiling 结果）与 `GenericHashItem`（缓存条目通用外壳） |
| [common/inc/op_host/tiling_base.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_host/tiling_base.h) | `TilingBaseClass`：固化 8 步 tiling 流程的模板基类（u4-l2 已讲注册表，本讲补框架本体） |
| [examples/add_example/op_host/add_example_tiling.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp) | 教学样例的 tiling 实现，本讲实践的操作对象 |
| [matmul/quant_batch_matmul_v3/.../quant_batch_matmul_v3_basic_tiling.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/matmul/quant_batch_matmul_v3/op_host/op_tiling/quant_batch_matmul_v3_basic_tiling.cpp) | 生产算子使用 tiling 缓存的第一手范例 |
| [norm/layer_norm_v3/op_host/arch35/layer_norm_v3_tiling_arch35.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/norm/layer_norm_v3/op_host/arch35/layer_norm_v3_tiling_arch35.cpp) | 非 matmul 算子复用同一套缓存设施的范例 |
| [matmul/quant_batch_matmul_v4/op_host/op_tiling/quant_batch_matmul_v4_tiling_registry.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/matmul/quant_batch_matmul_v4/op_host/op_tiling/quant_batch_matmul_v4_tiling_registry.cpp) | v4 量化 matmul：8 个 tiling 策略的注册与按芯片分发 |

一个先说清楚的事实纠正：**`CeilDiv`/`FloorAlign` 并不定义在本仓库的 `common/inc/op_host/tiling_util.h` 里**。它们的真身在 CANN 包提供的头文件 `util/math_util.h`（不在本仓库，编译时来自已安装的 CANN 环境）的 `Ops::Base` 命名空间；本仓库的复用入口是 `matmul/common/op_host/math_util.h` 这层薄包装。本讲会沿这条链讲清楚。

## 4. 核心概念与源码讲解

### 4.1 公共小工具 tiling_util.h：别在每个算子里重抄一遍

#### 4.1.1 概念说明

写 tiling 函数时有两类高频碎活：

1. **标量归一化**：输入可能是 0 维标量（shape 为 `()`），而下游计算统一按 1 维 `{1}` 处理更省心——需要把 `()` 换成 `{1}`。
2. **架构判断**：Regbase 类架构（`NpuArch::DAV_3510`、`DAV_5102`）在 tiling 策略上要走不同分支，判断逻辑到处都要用。

这两件事如果每个算子自己抄一份，就是上百份重复代码。`common/inc/op_host/tiling_util.h` 把它们收敛成公共函数。

#### 4.1.2 核心流程

```text
EnsureNotScalar(shape):
    if shape 是标量() -> 返回全局常量 {1}
    otherwise          -> 原样返回 shape 的引用

IsRegbaseSocVersion(context):
    从 context 取平台信息 -> 查询当前 NpuArch
    -> NpuArch ∈ {DAV_3510, DAV_5102} ? 是 : 否
```

注意 `EnsureNotScalar` 返回的是**引用**：标量路径返回静态常量 `g_vec_1_shape` 的引用，非标量路径返回入参引用，零拷贝。这里有一个 C++ 生命周期细节——不能返回临时构造的 `gert::Shape{1}` 的引用，所以静态常量是必须的。

#### 4.1.3 源码精读

[common/inc/op_host/tiling_util.h:L26-L32](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_host/tiling_util.h#L26-L32) 定义了静态常量 shape 与 Regbase 架构集合（用一个 `static const std::set` 承载，进程内只构造一次）：

```cpp
static const gert::Shape g_vec_1_shape = {1};

static bool IsRegbaseNpuArch(NpuArch npuArch)
{
    const static std::set<NpuArch> regbaseNpuArchs = {NpuArch::DAV_3510, NpuArch::DAV_5102};
    return regbaseNpuArchs.find(npuArch) != regbaseNpuArchs.end();
}
```

[common/inc/op_host/tiling_util.h:L34-L48](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_host/tiling_util.h#L34-L48) 为 `TilingParseContext` 和 `TilingContext` 各提供一个重载：先包一层 `platform_ascendc::PlatformAscendC` 再取 `GetCurNpuArch()`——这正是 u4-l1 讲过的「查平台信息」标准姿势。

[common/inc/op_host/tiling_util.h:L50-L56](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_host/tiling_util.h#L50-L56) 就是标量归一化本体，五行搞定。

对照反面教材：教学样例 [examples/add_example/op_host/add_example_tiling.cpp:L45-L64](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L45-L64) 自己重新定义了 `g_vec_1_shape` 和一模一样的 `EnsureNotScalar`——教学样例为了自包含可以理解，但生产算子应当用公共版。真实复用者例如 [optim/sparse_apply_rms_prop/op_host/arch35/sparse_apply_rms_prop_tiling.cpp:L201](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/optim/sparse_apply_rms_prop/op_host/arch35/sparse_apply_rms_prop_tiling.cpp#L201) 直接 `using Ops::NN::OpTiling::EnsureNotScalar;`，以及 [quant/dequant_swiglu_quant/op_host/dequant_swiglu_quant_tiling.cpp:L556](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/quant/dequant_swiglu_quant/op_host/dequant_swiglu_quant_tiling.cpp#L556) 用 `Ops::NN::OpTiling::IsRegbaseSocVersion(context)` 做架构分支。

#### 4.1.4 代码实践

1. **实践目标**：把 add_example 的「私有实现」换成公共设施，确认行为不变。
2. **操作步骤**：
   - 打开 `examples/add_example/op_host/add_example_tiling.cpp`，它已经 `#include "op_host/tiling_util.h"`（见 [L22](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L22)），说明头文件路径是通的；
   - 删除本地的 `g_vec_1_shape`（L45）与 `EnsureNotScalar`（L58-L64），在 `namespace optiling` 里加 `using Ops::NN::OpTiling::EnsureNotScalar;`；
   - 按 u1-l2 的流程重新 `bash build.sh --pkg --soc=${soc_version} --ops=add_example` 并安装。
3. **需要观察的现象**：编译通过；`bash build.sh --run_example add_example eager cust --vendor_name=custom` 输出与改动前一致。
4. **预期结果**：结果不变（因为两个实现逻辑相同），但代码少约 15 行。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`EnsureNotScalar` 为什么必须返回引用而不是值？

**答案**：调用方拿到的 `gert::Shape` 会继续以 `const&` 传给 `GetDimNum()`/`GetShapeSize()` 等接口；返回引用避免拷贝。更关键的是，若写成返回局部临时对象的引用会悬垂；而值返回虽安全但改动函数签名会波及所有调用点。静态常量 + 引用是零开销且线程安全（只读）的方案。

**练习 2**：`IsRegbaseSocVersion` 为什么提供 `TilingContext` 和 `TilingParseContext` 两个重载，而不是让调用方自己取平台信息？

**答案**：两种 context 是框架在不同阶段（tiling 执行 / 编译信息解析）传入的不同类型，取平台信息的入口一样但类型不兼容。提供两个重载把「包一层 PlatformAscendC 再查 NpuArch」的样板代码收敛到一处，调用方一行判断即可。

### 4.2 CeilDiv/FloorAlign：数学对齐工具的真实来源与复用链

#### 4.2.1 概念说明

u4-l1 已经用过这四个函数，这里补齐「它们从哪来」。tiling 本质是整数规划：把 N 个元素分给 C 个核、把块大小对齐到 32 字节边界，离不开这四个整型运算：

\[ \text{CeilDiv}(a, b) = \left\lceil \frac{a}{b} \right\rceil \qquad \text{FloorDiv}(a, b) = \left\lfloor \frac{a}{b} \right\rfloor \]

\[ \text{CeilAlign}(a, b) = \text{CeilDiv}(a, b) \cdot b \qquad \text{FloorAlign}(a, b) = \text{FloorDiv}(a, b) \cdot b \]

取整方向的选型原则（u4-l1 结论复述）：**向上取整防漏元素，向下对齐防越界**。

#### 4.2.2 核心流程

复用链是一条三层路径：

```text
CANN 包头文件 util/math_util.h          <- 真身：Ops::Base::CeilDiv 等（不在本仓库）
        ↑ #include
matmul/common/op_host/math_util.h       <- 仓库侧包装：Ops::NN::MathUtil 类 + ops:: 自由函数
        ↑ #include
各算子 tiling 源码                       <- 直接用 Ops::Base:: 或包装层
```

#### 4.2.3 源码精读

[matmul/common/op_host/math_util.h:L17](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/matmul/common/op_host/math_util.h#L17) 第一行就 `#include "util/math_util.h"`——真身在 CANN 安装包里（编译时由 include 路径解析，本仓库没有这个文件），提供 `Ops::Base::CeilDiv / CeilAlign / FloorDiv / FloorAlign` 模板函数。

[matmul/common/op_host/math_util.h:L21-L39](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/matmul/common/op_host/math_util.h#L21-L39) 包装成 `Ops::NN::MathUtil` 类（`CeilDivision`/`Align` 两个名字的别名）：

```cpp
class MathUtil {
public:
    template <typename T>
    static T CeilDivision(T num1, T num2)
    {
        return Ops::Base::CeilDiv(num1, num2);
    }
    ...
};
```

[matmul/common/op_host/math_util.h:L43-L67](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/matmul/common/op_host/math_util.h#L43-L67) 再在 `namespace ops` 下铺一层同名自由函数（`CeilDiv`/`FloorAlign`/`FloorDiv`/`CeilAlign`），方便老代码无脑替换。这是一个典型的「适配器分层」：底层换实现（CANN 升级），上层算子代码不动。

回到消费端，[examples/add_example/op_host/add_example_tiling.cpp:L215-L222](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L215-L222) 是 u4-l1 讲过的核切分 + UB 切分公式，直接用 `Ops::Base` 原名：

```cpp
tiling->blockFactor = Ops::Base::CeilDiv(totalIdx, coreNum);
int64_t usedCoreNum = Ops::Base::CeilDiv(totalIdx, tiling->blockFactor);
...
tiling->ubFactor = Ops::Base::FloorAlign(Ops::Base::FloorDiv((ubCanUse / TYPE_SIZE), BUFFER_NUM), ubBlockSize);
```

生产算子的用法一样，例如 [matmul/quant_batch_matmul_v4/op_host/op_tiling/arch35/quant_batch_matmul_v4_tiling.cpp:L24-L32](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/matmul/quant_batch_matmul_v4/op_host/op_tiling/arch35/quant_batch_matmul_v4_tiling.cpp#L24-L32) 同时引入了 CANN 原生头和仓库包装层。

#### 4.2.4 代码实践

1. **实践目标**：通过日志观察 `CeilDiv`/`FloorAlign` 在不同规模下的取值，把公式「跑活」。
2. **操作步骤**：
   - 在 `AddExampleTilingFunc` 中 `context->SetBlockDim(usedCoreNum);` 之前加一行 `OP_LOGI(context, "totalIdx=%ld coreNum=%ld blockFactor=%u ubFactor=%u", totalIdx, coreNum, tiling->blockFactor, tiling->ubFactor);`
   - 重新编译安装后，修改样例输入 shape 分别为 `{1,1,1,1}`（1 个元素）、`{8,8,8,8}`（4096 个元素）、`{16,16,16,16}`（65536 个元素），各跑一次。
3. **需要观察的现象**：`blockFactor` 随 totalIdx 变化，`ubFactor` 在三次运行中保持不变（它只由 UB 容量、TYPE_SIZE、BUFFER_NUM 决定，与输入规模无关——u4-l1 的结论）。
4. **预期结果**：例如 `totalIdx=1` 时 `blockFactor=1`、`usedCoreNum=1`；`totalIdx=65536`、`coreNum=50` 时 `blockFactor=⌈65536/50⌉=1311`、`usedCoreNum=⌈65536/1311⌉=50`。具体核数取决于你的芯片型号，待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`ubFactor` 的计算为什么是「FloorDiv 之后再 FloorAlign」，而不是先 CeilAlign 再除？

**答案**：UB 空间是硬上限。先 `FloorDiv(ubCanUse/TYPE_SIZE, BUFFER_NUM)` 保证 6 块 tensor 总和**不超过** UB；再 `FloorAlign(..., 32字节块)` 保证单块大小满足 DataCopy 对齐且仍然**不超过**预算。若用向上取整，任何一步都可能超出 UB 容量导致越界写。

**练习 2**：仓库里已经有 `Ops::Base::CeilDiv`，为什么 `matmul/common/op_host/math_util.h` 还要再包一层 `ops::CeilDiv`？

**答案**：历史与兼容。不同时期、不同来源的算子代码使用的命名空间约定不同（`ops::`、`Ops::NN::MathUtil::`），包装层让存量代码无需大改就能统一到同一底层实现，也隔离了 CANN 升级带来的接口变动。

### 4.3 TilingCache：把「重算」变成「查表」

#### 4.3.1 概念说明

推理/训练场景里，同一个算子往往被**相同 shape 反复调用几万次**（每一层、每一个 step）。tiling 是纯 Host 侧计算，涉及平台查询、除法对齐、有时还有很重的策略搜索（matmul 的 tiling 可能耗时数百微秒）。如果每次都从头算，Host 侧就成了吞吐瓶颈。

TilingCache 的思路很朴素：

\[ \text{key} = H(\text{shape}, \text{dtype}, \text{attrs}, \text{平台参数}) \]

第一次算完把结果存进进程级 `std::map`；后续同 key 先查表，命中则直接把缓存的 TilingData/BlockDim/TilingKey 恢复进 context，跳过整个计算。

代价与边界：缓存上限 `kMaxTilingCacheEntryNum = 500` 条（防内存膨胀，超限直接不存）；哈希可能冲突，所以 `Get` 时还要用 `operator==` 逐字段比对完整输入；缓存条目**永不淘汰**（没有 LRU），靠条数上限兜底。

#### 4.3.2 核心流程

```text
tiling 函数入口
    ├─ 构造 HashInput：把影响结果的所有字段（shape、dtype、attrs、核数…）打包成 POD
    ├─ key = 哈希(HashInput)
    ├─ cache.Get(key, hashInput, item)
    │     ├─ 命中（key 存在 且 字段比对一致）
    │     │     └─ item.GetContext(context)：恢复 TilingData/BlockDim/TilingKey → 返回成功
    │     └─ 未命中
    │           └─ 正常执行完整 tiling 计算
    │               └─ item.SetContext(context, hashInput) + cache.Add(key, hashInput, item)
    └─ 返回
```

并发安全：`Get` 走共享锁（可并发读），`Add`/`Replace` 走排他锁。

#### 4.3.3 源码精读

**缓存模板本体**。仓库里有两份近似实现：

- [common/inc/op_host/tiling_cache.h:L27-L47](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_host/tiling_cache.h#L27-L47) 是 `Add`：加写锁 → 查容量上限（[L25](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_host/tiling_cache.h#L25) 定义 `kMaxTilingCacheEntryNum = 500`）→ key 已存在则**直接返回不覆盖**（首次结果被固化）→ 否则插入并 `size_++`。注意 `Add` 的 `hash_input` 参数在这份实现里参与了容量检查但实际上没参与插入判断——它是模板签名的一部分，真正的输入比对发生在 `Get`。
- [common/inc/op_host/tiling_cache.h:L66-L83](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_host/tiling_cache.h#L66-L83) 是 `Get`：读锁 → 按 key 查 map → **二次校验** `hash_input == iter->second.input()`（哈希冲突兜底，比对失败同样返回 false）→ 拷出 `value` 返回 true。注意 [L74](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_host/tiling_cache.h#L74) 要求 `HashItem` 类型必须提供 `input()` 方法——这个约定由下面的 `GenericHashItem` 满足。
- 这份实现用自带的 `Ops::NN::HostTiling::RWLock`（定义在 [common/inc/op_host/lock.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_host/lock.h)），手工加解锁；另一份 [matmul/common/op_host/op_tiling/tiling_cache.h:L24-L79](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/matmul/common/op_host/op_tiling/tiling_cache.h#L24-L79) 逻辑相同，改用 C++17 `std::shared_mutex` + RAII 锁guard，代码更短。**实际被算子 include 的是 matmul 版**（quant_batch_matmul_v3、layer_norm_v3、repeat_interleave 等），`common/inc` 版是公共目录下的等价设施。

**缓存条目外壳**。[common/inc/op_host/cache_runinfo.h:L19-L37](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_host/cache_runinfo.h#L19-L37) 的 `CacheTilingContext` 把 tiling 的全部交付物（tilingKey、numBlocks、tilingData 字节缓冲、workspace 等）打包成可拷贝对象，`Save()` 从 context 里取走、`Load()` 灌回 context——实现体在 [common/src/op_host/cache_runinfo.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/src/op_host/cache_runinfo.cpp)，随公共库链接进算子宿主库。[common/inc/op_host/cache_runinfo.h:L39-L68](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_host/cache_runinfo.h#L39-L68) 的 `GenericHashItem<T>` 则把「键 + 值」组合成缓存条目，并提供 TilingCache 约定的 `input()` 方法。二者配合，任何算子只需定义自己的键结构就能获得缓存能力。

**生产用法（matmul 系）**。[matmul/quant_batch_matmul_v3/op_host/op_tiling/quant_batch_matmul_v3_tiling_cache.h:L35-L69](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/matmul/quant_batch_matmul_v3/op_host/op_tiling/quant_batch_matmul_v3_tiling_cache.h#L35-L69) 定义键类型 `QuantBatchMatmulV3HashInput`：把 M/N/K、各 batch 维、6 个 dtype、核数、以及一个**位域** `QuantBatchMatmulV3BitField`（transA/transB/hasBias 等 8 个布尔压进一个 32bit 字）全部铺成 POD 字段，`operator==` 直接 `memcmp` 整个结构体——又快又不容易漏字段。[matmul/quant_batch_matmul_v3/op_host/op_tiling/quant_batch_matmul_v3_basic_tiling.cpp:L1275-L1286](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/matmul/quant_batch_matmul_v3/op_host/op_tiling/quant_batch_matmul_v3_basic_tiling.cpp#L1275-L1286) 是标准使用姿势：

```cpp
QuantBatchMatmulV3HashItem hashValue(inputParams_, aicoreParams_);
uint32_t tilingKey = Ops::NN::MurmurHash(&(hashValue.input()), sizeof(hashValue.input()));
static MMBasicTilingHash tilingHashCache;              // 进程级静态缓存对象
if (tilingHashCache.Get(tilingKey, hashValue.input(), hashValue)) {
    OP_LOGD(..., "tiling is in cache, ...");
    basicTiling_ = hashValue.GetTiling();
    return true;                                        // 命中：跳过全部 tiling 计算
}
```

哈希函数是 [matmul/common/op_host/op_tiling/hash.h:L22](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/matmul/common/op_host/op_tiling/hash.h#L22) 声明的 `MurmurHash`（实现同目录 hash.cpp）——对 POD 字节流做 Murmur 哈希，分布均匀且与字段无关，所以键结构怎么加字段都不用改哈希代码。

**生产用法（非 matmul）**。[norm/layer_norm_v3/op_host/arch35/layer_norm_v3_tiling_arch35.cpp:L41-L42](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/norm/layer_norm_v3/op_host/arch35/layer_norm_v3_tiling_arch35.cpp#L41-L42) 用公共 `GenericHashItem` 拼出缓存类型；[L93-L99](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/norm/layer_norm_v3/op_host/arch35/layer_norm_v3_tiling_arch35.cpp#L93-L99) 命中路径直接 `hash_item.GetContext(*context)` 把缓存整体灌回 context 后返回成功；[L118](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/norm/layer_norm_v3/op_host/arch35/layer_norm_v3_tiling_arch35.cpp#L118) 在算完后 `Add` 回缓存。这一份代码就是本讲综合实践的模板。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：不写代码，通过阅读三处调用点总结「谁有资格进键」。
2. **操作步骤**：对照阅读 `QuantBatchMatmulV3HashInput`（上面链接 L47-L68）与 `LayerNormV3CacheKeyWord`（layer_norm_v3 同文件 L41 附近）两个键结构，列出它们包含的字段类别。
3. **需要观察的现象**：两边都包含了「输入 shape 全部维度 + 全部输入 dtype + 布尔类属性 + 核数」，**不包含**数据本身（数值）和输出指针。
4. **预期结果**：结论——键必须覆盖所有影响 tiling 结果的量；凡是没进键的量在缓存命中时就被「视为相同」，漏一个就是错切分的 bug。

#### 4.3.5 小练习与答案

**练习 1**：`Add` 在 key 已存在时静默返回、不覆盖旧值。如果你升级了 tiling 算法、但进程内同 key 第二次计算出了不同结果，会发生什么？怎么解决？

**答案**：仍然返回第一次（旧）的结果，且 `Get` 的字段比对会通过——因为键没变。同一进程内算法变了但键不变属于「确定性被破坏」，缓存语义上不允许；若确实需要在运行期用新结果替换旧值，应使用 `Replace`（见 [common/inc/op_host/tiling_cache.h:L49-L64](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_host/tiling_cache.h#L49-L64)），它会先 erase 再 emplace。

**练习 2**：缓存上限 500 条、无淘汰。一个服务交替使用 600 种不同 shape 会发生什么？

**答案**：前 500 种 shape 依次填满缓存后，`Add` 因 `size_ >= kMaxTilingCacheEntryNum` 直接返回，后 100 种永远未命中、每次都全量重算；且已缓存的 500 条不会为新 shape 让位（无 LRU）。这是「以空间换时间 + 有界内存」的工程取舍。

**练习 3**：为什么 `Get` 里已经按 key 查到 map 项，还要再做一次 `hash_input == iter->second.input()` 比对？

**答案**：key 是 32 位哈希，不同输入可能碰撞出同一 key。二次比对完整键字段才能保证命中项确实是同一组输入，比对失败按未命中处理（重新计算），正确性不受哈希碰撞影响。

### 4.4 tiling_base.h 与真实算子的复用全景

#### 4.4.1 概念说明

u4-l2 讲过 `REGISTER_*_TILING_TEMPLATE` 注册表，本讲补上它的孪生基座：`TilingBaseClass` 把一次 tiling 拆成固定的 8 个步骤（取 shape/属性 → 取平台信息 → 能力判断 → 算切分 → 算高阶 API tiling → 算 workspace → 保存 → dump 日志），子类只覆写钩子。`quant_batch_matmul_v4` 在此之上展示了三种复用姿势的叠加：

1. **模板方法模式**：8 个 tiling 类都继承 `TilingBaseClass`（或间接经过 v3 的中间基类）；
2. **跨算子继承**：v4 的策略类直接继承 `quant_batch_matmul_v3` 的 `QuantBatchMatmulV3BasicTiling`，白捡 v3 已沉淀的切分逻辑与缓存；
3. **注册表分发**：8 个策略按优先级注册，运行期按芯片型号挑选梯队尝试。

#### 4.4.2 核心流程

`TilingBaseClass::DoTiling()` 的 8 步流水（返回值语义是关键）：

```text
GetShapeAttrsInfo ──┐失败→ 直接失败
GetPlatformInfo  ───┤失败→ 直接失败
IsCapable? ─────────┤false → 返回 GRAPH_PARAM_INVALID（"我不行，换下一个策略类"）
DoOpTiling ─────────┤
DoLibApiTiling ─────┤
GetWorkspaceSize ───┤
PostTiling ─────────┤
SetTilingKey(GetTilingKey()) + DumpTilingInfo → GRAPH_SUCCESS
```

注册表拿到 `GRAPH_PARAM_INVALID` 就尝试下一个优先级的策略类，拿到 `GRAPH_SUCCESS` 立即收工——这就是 v4 按芯片分发的基础。

#### 4.4.3 源码精读

[common/inc/op_host/tiling_base.h:L70-L102](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_host/tiling_base.h#L70-L102) 是 `DoTiling()` 本体，注释明确写了三种返回值的含义（成功 / 失败 / 本类不支持需继续尝试）。[L108-L124](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_host/tiling_base.h#L108-L124) 声明 8 个纯虚钩子，子类各填各的；[L126-L142](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_host/tiling_base.h#L126-L142) 的 `DefaultTilingInfoDump` 把 tiling data 按 `uint32_t` 逐字打日志（每 640 字符切一段防截断），调 tiling 问题时开 DEBUG 日志就能看到全部切分参数。[L35-L58](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_host/tiling_base.h#L35-L58) 定义的 `AiCoreParams`/`CompileInfoCommon` 正是 4.3 节键结构里「平台参数」字段的来源。

[matmul/quant_batch_matmul_v4/op_host/op_tiling/quant_batch_matmul_v4_tiling_registry.cpp:L31-L48](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/matmul/quant_batch_matmul_v4/op_host/op_tiling/quant_batch_matmul_v4_tiling_registry.cpp#L31-L48) 注册 8 个策略类并给出 0~7 的优先级常量；[L50-L80](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/matmul/quant_batch_matmul_v4/op_host/op_tiling/quant_batch_matmul_v4_tiling_registry.cpp#L50-L80) 的 `QuantBatchMatmulV4TilingFunc` 按芯片分发：910B/910_93/KIRINX90 走 `{MSD, PERBLOCK, PERGROUP}` 梯队，支持 s8s4 mmad 的走 LUT 策略，其余走 `{BASIC_PERBLOCK, PERGROUP_ARCH35, WEIGHT_QUANT_MX_SWAT, REG_BASE}` 四级梯队——同一份代码服务多代芯片，靠的就是「策略类 + 优先级 + PARAM_INVALID 回退」这套机制。

[matmul/quant_batch_matmul_v4/op_host/op_tiling/quant_batch_matmul_v4_msd_tiling.h:L29-L45](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/matmul/quant_batch_matmul_v4/op_host/op_tiling/quant_batch_matmul_v4_msd_tiling.h#L29-L45) 展示跨算子继承：v4 的 `QuantBatchMatmulV4MsdTiling` 直接 `: public QuantBatchMatmulV3BasicTiling`（include 的是 v3 目录下的三个头文件），并覆写 `IsCapable`/`DoOpTiling` 等钩子——v3 已验证的公共逻辑被 v4 原样复用，v4 只写差异部分。同时 [L25-L27](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/matmul/quant_batch_matmul_v4/op_host/op_tiling/quant_batch_matmul_v4_msd_tiling.h#L25-L27) include 了 `op_host/tiling_base.h` 与 `op_host/tiling_templates_registry.h` 两份公共头。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：走通一条「策略被选中」的完整路径，理解 PARAM_INVALID 回退。
2. **操作步骤**：
   - 从 `QuantBatchMatmulV4TilingFunc`（上面链接 L50）出发，假设芯片是 ascend910b，确定它会尝试的策略顺序（MSD → PERBLOCK → PERGROUP）；
   - 打开 MSD 策略实现，找到其 `IsCapable()` 的判断条件，构造一个会让它返回 false 的输入组合（例如某个 v4 尚不支持的 dtype/量化组合，具体条件以源码为准）；
   - 回答：此时最终由哪个策略接手？
3. **需要观察的现象**：注册表 `DoTilingImpl` 依次调用梯队中每个策略的 `DoTiling()`，直到某个返回非 `GRAPH_PARAM_INVALID`。
4. **预期结果**：MSD 返回 `GRAPH_PARAM_INVALID` 后，注册表继续尝试 PERBLOCK；只要梯队里有一个 `GRAPH_SUCCESS` 即成功，全部 PARAM_INVALID/FAILED 则整体失败。待本地验证（可用 u7-l1 的 UT 框架跑 `tests/ut/op_host/test_quant_batch_matmul_v4_tiling.cpp` 观察）。

#### 4.4.5 小练习与答案

**练习 1**：`TilingBaseClass::DoTiling()` 里 `IsCapable()` 在 `GetPlatformInfo()` 之后才调用，为什么顺序不能反过来？

**答案**：`IsCapable()` 的判断通常依赖平台信息（核数、UB/L1/L0 大小、是否支持某 intrinsic），必须先取到平台信息才有判断依据。顺序固定也保证所有子类的执行框架一致——这正是模板方法模式的价值。

**练习 2**：v4 直接继承 v3 的 tiling 类， risks（风险）是什么？

**答案**：v3 与 v4 形成 Compile-time 级耦合：v3 改动公共钩子行为会静默影响 v4；v3 的缓存键若没包含 v4 特有的输入差异，v4 命中缓存可能拿到按 v3 语义算的切分。所以跨算子继承要求键结构和钩子语义有清晰的契约注释，并依赖两侧的 UT（`tests/ut/op_host/test_quant_batch_matmul_v4_*_tiling.cpp`）兜底。

## 5. 综合实践

**任务：为 AddExample 接入 tiling 缓存，并验证命中路径。**

AddExample 的 tiling 结果只依赖四个量：`totalIdx`（shape 乘积）、`dataType`、`coreNum`、`ubSize`，完全确定——是理想的缓存对象。模板照抄 layer_norm_v3 的「Get → 命中即返回 / 未命中算完 Add」三段式。

1. **准备**：按 u1-l2 完成环境准备，确认 `bash build.sh --run_example add_example eager cust --vendor_name=custom` 当前可跑通。

2. **改 tiling 源码**：编辑 `examples/add_example/op_host/add_example_tiling.cpp`，新增（以下为**示例代码**，非仓库原有内容）：

   ```cpp
   #include "op_host/tiling_cache.h"   // Ops::NN::HostTiling::TilingCache
   #include "op_host/cache_runinfo.h"  // GenericHashItem

   namespace optiling {
   // 键：覆盖所有影响 tiling 结果的量（示例代码）
   struct AddExampleHashInput {
       int64_t totalNum;
       int32_t dataType;
       int64_t coreNum;
       uint64_t ubSize;
       bool operator==(const AddExampleHashInput& rhs) const
       {
           return memcmp(this, &rhs, sizeof(AddExampleHashInput)) == 0;
       }
   };
   using AddExampleHashItem = optiling::GenericHashItem<AddExampleHashInput>;
   } // namespace optiling
   ```

   然后在 `AddExampleTilingFunc` 里，把现有的「步骤 1~2（取平台信息、取 shape）」之后、「步骤 4（写 tiling data）」之前插一层缓存（示例代码）：

   ```cpp
   AddExampleHashInput hashInput{totalIdx, static_cast<int32_t>(dataType), coreNum, ubSize};
   uint32_t hashKey = static_cast<uint32_t>(totalIdx) ^ (static_cast<uint32_t>(dataType) << 16) ^
                      (static_cast<uint32_t>(coreNum) << 24);
   static Ops::NN::HostTiling::TilingCache<AddExampleHashInput, AddExampleHashItem> cache;
   AddExampleHashItem item;
   if (cache.Get(hashKey, hashInput, item)) {
       OP_LOGI(context, "AddExample tiling cache hit");   // 命中：整体恢复并直接返回
       item.GetContext(*context);
       return ge::GRAPH_SUCCESS;
   }
   // ……原有的 memset / blockFactor / ubFactor / SetBlockDim / SetTilingKey 逻辑保持不动 ……
   AddExampleHashItem newItem;
   if (newItem.SetContext(*context, hashInput)) {
       cache.Add(hashKey, hashInput, newItem);
   }
   ```

   要点：`GenericHashItem::SetContext` 会在**当时**把 context 里已写好的 TilingData/BlockDim/TilingKey 快照存档，所以必须放在全部 SetXxx 之后；命中路径的 `GetContext` 则一次性灌回。注意手动异或的 key 碰撞概率远高于 MurmurHash，但 `Get` 里的 `operator==` 比对保证正确性——生产代码请换成 `MurmurHash`。

3. **编译安装**：`bash build.sh --pkg --soc=${soc_version} --ops=add_example -j16`，安装 run 包（u1-l2 流程）。

4. **验证命中**（两条路任选）：
   - **样例路径**：把 `examples/add_example/examples/test_aclnn_add_example.cpp` 里的执行段（第二段 aclnn 调用 + 同步）复制一份再执行一次（同一 shape 连续调用两次），重新 `bash build.sh --run_example add_example eager cust --vendor_name=custom`。同一进程内第二次调用 tiling 时应出现 `AddExample tiling cache hit` 日志。
   - **UT 路径**：在 `examples/add_example/tests/ut/op_host/test_add_example_tiling.cpp` 中新增用例：同一组 shape/dtype 连续构造两次 tiling 上下文并调用 tiling 函数，断言两次都返回 `GRAPH_SUCCESS` 且两次得到的 TilingData 字段（`totalNum`/`blockFactor`/`ubFactor`）一致——一致性即证明命中路径恢复的结果与重算相同。

5. **预期结果**：功能输出与改前完全一致（缓存不改变 tiling 结果，只是省掉重算）；日志或 UT 断言能证明第二次走了缓存。改 shape 再调用则不应命中（键不同）。
6. 若无真实环境，上述运行结果均为**待本地验证**；可先只完成代码修改与 UT 用例编写，用 u8-l3 的仿真方式验证。

## 6. 本讲小结

- `CeilDiv`/`FloorAlign` 等对齐工具的真身在 CANN 包头文件 `util/math_util.h` 的 `Ops::Base` 命名空间（不在本仓库），仓库侧复用入口是 `matmul/common/op_host/math_util.h` 的包装层；取整方向的选择原则是「向上防漏元素、向下防越界」。
- `common/inc/op_host/tiling_util.h` 收敛了 `EnsureNotScalar`（标量归一化）与 `IsRegbaseSocVersion`（Regbase 架构判断）两类高频碎活，生产算子用 `using` 直接复用，教学样例则保留了本地重复实现。
- TilingCache 是「键 = 全部影响 tiling 结果的输入特征（POD + memcmp 比对）→ map 查表」的进程级缓存：上限 500 条、无淘汰、`Add` 不覆盖旧值、`Get` 用字段二次比对兜底哈希冲突；读写锁保证多线程安全。
- 缓存条目的通用外壳是 `cache_runinfo.h` 的 `CacheTilingContext`/`GenericHashItem`——`Save/Load` 整体快照与恢复 TilingData、BlockDim、TilingKey，任何算子定义好键结构即可接入。
- `TilingBaseClass` 用 8 步模板方法固化 tiling 流程，`GRAPH_PARAM_INVALID` 表示「本策略不行，换下一个」；quant_batch_matmul_v4 展示了三种复用的叠加：继承基类 + 注册 8 个优先级策略按芯片分发 + 直接继承 v3 的 tiling 类。

## 7. 下一步学习建议

- **u5-l1（Ascend C 编程模型）**：tiling 产出的 `blockFactor`/`ubFactor` 如何在 kernel 侧变成 `CopyIn/Compute/CopyOut` 的循环边界，是 TilingData 契约的消费端。
- **u7-l1（UT 框架）**：本讲综合实践提到的「两次调用断言结果一致」需要用 UT 框架落地，顺便系统学习 `tests/ut` 的组织方式。
- **源码延伸阅读**：对照 [matmul/quant_batch_matmul_v3/op_host/op_tiling/quant_batch_matmul_v3_basic_tiling.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/matmul/quant_batch_matmul_v3/op_host/op_tiling/quant_batch_matmul_v3_basic_tiling.cpp) 中 `DoBasicTiling` 的完整缓存逻辑（含 `PrintBasicTiling`），体会生产级键设计比你给 AddExample 写的版本多了哪些字段、为什么。
