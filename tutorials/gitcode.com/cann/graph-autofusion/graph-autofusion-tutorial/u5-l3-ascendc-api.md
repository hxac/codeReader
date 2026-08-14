# AscendC API 头文件与算子能力

## 1. 本讲目标

本讲聚焦 Autofuse 数据流中「算子能力」的最后一环：codegen 生成的设备 kernel 里，每一个算子真正执行计算的那段 C++ 代码从哪里来。

学完后你应该能够：

- 说清 `autofuse/ascendc/api/` 目录下头文件是什么、解决了什么问题，以及它们与 CANN 原生 AscendC API 的关系。
- 读懂 reduce、datacopy、broadcast 三类代表性头文件的接口签名与内部分发逻辑。
- 用一句话讲明白「ascendc/api 与 codegen 的衔接」：codegen 拼接**调用语句**，ascendc/api 提供**函数定义**，而定义本身是以**原始字符串字面量**的形式被整段嵌入设备 kernel 源码的。
- 知道新增一个算子的设备端实现，需要在哪些文件里登记。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**第一，什么是 AscendC。** AscendC 是昇腾 AI Core 的算子开发语言，提供一批面向硬件指令的 C++ 模板 API（如 `WholeReduceSum`、`DataCopyPad`、`Add`、`Abs`）。带 `__aicore__` 标记的函数运行在设备端（AI Core 上），而非主机端（host）。本讲看到的函数几乎都带 `__aicore__`，说明它们是「跑在芯片上」的代码。

**第二，片上两级存储与「块/重复」两个单位。** 回顾 u3-l1：AI Core 有全局内存（GM/HBM）和片上统一缓冲（UB）。Vector 算子先把数据从 GM 搬进 UB（MTE2 指令），计算完再搬回 GM（MTE3 指令）。硬件对 UB 的访问以「块（block）」和「重复（repeat）」为单位对齐：

- `ONE_BLK_SIZE` = 32 字节，是一个内存块的大小。一个块能装多少个元素取决于 dtype：`BlkSize<T>() = 32 / sizeof(T)`，例如 `half`（2 字节）是 16 个，`float`（4 字节）是 8 个。
- `ONE_REPEAT_BYTE_SIZE` = 256 字节，是一次 Vector 重复能处理的字节数；`MAX_REPEAT_TIME` = 255，是一条指令里最大的重复次数。

\[ \text{BlkSize}(T) = \frac{32}{\text{sizeof}(T)} \quad\text{（元素个数）} \]

这两个单位会在本讲代码里反复出现，理解它们就能看懂大量「对齐」「分块循环」逻辑。

**第三，承接前两讲。** u5-l1 讲了 ASCIR 算子的**注册**（`REG_ASC_IR` 把元数据 + ATT 实现 / codegen 实现 / dtype 约束三元组登记进全局表）；u5-l2 讲了 codegen 实现里的 `CalcTmpBufSize`（即 reg_func，估算算子需要多大片上临时缓冲）。本讲回答最后一个问题：codegen 实现最终**生成的调用语句**指向哪里？答案是 `ascendc/api` 下的设备端封装函数。三讲合起来，才是一个算子「从注册到能在芯片上跑」的完整链路。

## 3. 本讲源码地图

本讲涉及的关键文件分为两组。

**第一组：算子能力提供方（本讲主角）**

| 文件 | 作用 |
|------|------|
| `autofuse/ascendc/api/reduce.h` | 归约类算子的设备端封装：`WholeReduceXxxAdapt` 适配层、`ReduceLast` 模板、`ReduceSumInt32` |
| `autofuse/ascendc/api/datacopy.h` | 搬运类算子封装：`DataCopyPadExtend`（GM↔UB）、`DataCopyExtend`（UB↔UB） |
| `autofuse/ascendc/api/broadcast.h` | 广播类算子封装：`Broadcast` 按 dtype 与形状分发 |
| `autofuse/ascendc/api/utils.h` | 公共工具：`KernelUtils::BlkSize/BlkNum/RptSize`、`Min/Max/Ceiling/Mod` |
| `autofuse/ascendc/api/CMakeLists.txt` | 构建期把每个 `.h` 包装成原始字符串字面量 `*_str.h` |

**第二组：算子能力消费方（codegen 一侧，用于讲清衔接）**

| 文件 | 作用 |
|------|------|
| `autofuse/codegen/ascendc_api_registry.cpp` | 启动期把 `*_str.h` 注册进全局表 `api_to_file` |
| `autofuse/codegen/ascendc_api_registry.h` | 注册表单例：`GetFileContent(api_name)` 取出某个头的源码字符串 |
| `autofuse/codegen/codegen_kernel.cpp` | 生成 kernel 时，按图中算子**按需**把头源码字符串拼进 kernel 源码 |
| `autofuse/codegen/api_call/datacopy/load_api_call.cpp` | codegen 的「Load」调用生成器，产出 `DataCopyPadExtend(...)` 调用语句 |
| `autofuse/codegen/api_call/broadcast/broadcast_api_call.cpp` | codegen 的「Broadcast」调用生成器，产出 `Broadcast(...)` 调用语句 |
| `autofuse/codegen/api_call/reduce/reduce_api_call.cpp` | codegen 的「Reduce」调用生成器，产出 `ReduceSumInt32(...)` 等调用语句 |

一句话记忆：**左侧（ascendc/api）提供「函数定义」，右侧（codegen）提供「函数调用」，两边靠相同的函数名与签名对接。**

## 4. 核心概念与源码讲解

### 4.1 ascendc/api 算子分类

#### 4.1.1 概念说明

`autofuse/ascendc/api/` 目录里放着四十多个 `.h` 头文件（reduce.h、datacopy.h、broadcast.h、abs.h、compare.h、cast.h、concat.h、gather.h、transpose.h、各类 scalar_*.h ……）。它们不是凭空另造一套算子，而是 **CANN 原生 AscendC API 之上的二次封装**，命名上多用 `...Extend` 后缀来暗示「这是原生接口的扩展版」。

为什么要做这层封装？三个动机：

1. **补齐 dtype 与场景**。原生 API 不一定覆盖所有数据类型。例如原生 `WholeReduceSum` 不支持 `int32_t`，于是 reduce.h 里用 `Add` + `Brcb` + `GatherMask` 手搓了一个 `ReduceSumInt32`（见 4.2.1）。
2. **统一签名**。原生同类 API 的参数个数 / 顺序不一致（如 `WholeReduceSum` 是 7 参，`WholeReduceMax/Min` 带 `order` 是 8 参）。封装层把它们抹平成统一签名，让上层用同一个函数指针类型来调用（见 4.2.1 的 `Adapt` 层）。
3. **承担复杂调度逻辑**。对齐 / 不对齐、大 k 拆分、按 repeat 循环展开这些细节，封装层一次性写好，上层只需传形状参数即可。

#### 4.1.2 核心流程

按「职责」而非「文件」给这些头文件分类，大致是这样的地图：

```text
ascendc/api/*.h
├── 搬运类（DMA，对应 MTE2/MTE3）
│   └── datacopy.h        GM↔UB / UB↔UB 数据搬运
├── 计算类（Vector）
│   ├── elewise：abs.h neg.h reciprocal.h rsqrt.h sigmoid.h sign.h gelu.h ...
│   ├── reduce：reduce.h reduce_max.h reduce_prod.h reduce_any.h reduce_init.h argmax.h
│   ├── broadcast：broadcast.h duplicate.h
│   ├── compare：compare.h compare_v2.h where.h clipbyvalue.h
│   ├── cast/逻辑/取整：cast.h logical.h floor_div.h true_div.h remainder.h ...
│   └── 标量：scalar_add/sub/mul/div/maximum/minimum.h
├── 形状类
│   └── concat.h gather.h transpose.h removepad.h
└── 公共基础
    ├── utils.h              BlkSize/RptSize/Min/Max/Ceiling/Mod
    ├── brc_inline_api.h     广播用到的内联基础指令
    └── transpose_base_type.h
```

这些头文件有三个共同特征：都是 **`template` + `inline __aicore__`** 函数（编译期内联进设备 kernel、随 dtype 实例化），都依赖 CANN 提供的 `LocalTensor<T>` / `GlobalTensor<T>` 张量抽象，都通过 `utils.h` 的 `KernelUtils` 工具统一计算块/重复单位。

#### 4.1.3 源码精读

先看公共基础 `utils.h`。几乎所有头文件都要用它来把「元素个数」换算成「块数 / 重复数」：

[autofuse/ascendc/api/utils.h:L143-L166](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascendc/api/utils.h#L143-L166) —— `KernelUtils::BlkSize/BlkNum/RptSize/MaxRptSize`，把 dtype 字节数与硬件常数 `ONE_BLK_SIZE`（32）、`ONE_REPEAT_BYTE_SIZE`（256）、`MAX_REPEAT_TIME`（255）换算成元素单位。后续 reduce/broadcast 里大量出现的 `BlkSize<T>()`、`MAX_REPEAT_TIME` 都来自这里。

再看一个典型的 elewise 封装 `abs.h`，它最能体现「补齐 dtype + 统一签名」的封装动机：

[autofuse/ascendc/api/abs.h:L26-L55](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascendc/api/abs.h#L26-L55) —— `AbsExtend`：对 `float/half` 直接转调原生 `AscendC::Abs`；对 `int32_t`（原生 Abs 不支持）则用「取反加一 + Max」(`Not + Adds + Max`) 手搓绝对值，并处理了 `dst` 与 `src` 同地址的「原地计算」场景。这正是封装层补齐能力的典型样例。

#### 4.1.4 代码实践

**实践目标**：建立「按文件名识别算子类别」的导航能力。

**操作步骤**：

1. 打开 `autofuse/ascendc/api/` 目录（本仓库 `ls autofuse/ascendc/api/`）。
2. 把文件名按上面的「搬运 / 计算 / 形状 / 公共」四类对号入座。
3. 任选一个 elewise 头文件（如 `neg.h`、`reciprocal.h`），用 Grep 找它内部转调的原生 AscendC 接口名（搜索 `AscendC::` 或 `inline __aicore__`）。

**需要观察的现象**：每个 `*Extend` 函数内部，要么直接调用一个原生 `AscendC::Xxx`，要么用更底层的指令（`Not`/`Add`/`Max`/`Duplicate` 等）组合出语义。

**预期结果**：你会发现封装层通常很薄（薄到一行转发），只有在「原生不支持某 dtype / 某场景」时才会变厚（如 abs.h 的 int32 分支、reduce.h 的 ReduceSumInt32）。

#### 4.1.5 小练习与答案

**练习 1**：`datacopy.h` 属于上面四类中的哪一类？它对应的硬件指令段是 MTE2 还是 MTE3？
> **答**：搬运类（DMA）。它两个方向都覆盖：GM→UB 是 MTE2（搬入），UB→GM 是 MTE3（搬出）。

**练习 2**：`scalar_add.h` 与 `abs.h` 都属于计算类，但前者是「标量」子类。猜一下 `scalar_add` 与普通二元 Add 在语义上的区别。
> **答**：`scalar_add` 是「张量 + 标量」的逐元素加（一个操作数是广播出来的标量），而普通 Add 是「张量 + 张量」。封装层把它单独成文件，是因为标量场景可以用更高效的 `Adds` 指令而非通用二元指令。

---

### 4.2 关键 API 形态

本节精读 reduce / datacopy / broadcast 三类代表性头文件，建立「看签名、看分发」的阅读能力。

#### 4.2.1 概念说明

三类算子的封装各有侧重：

- **datacopy（搬运）**：核心是填好 `DataCopyExtParams` 参数结构，处理好「字节 vs 元素」「stride 单位换算」。它有方向（load/store 两个重载）。
- **reduce（归约）**：核心难点是「归约轴可能很长（k 很大），一次指令做不完」，需要把归约拆成分块循环；还要把原生 7 参 / 8 参接口适配成统一签名。
- **broadcast（广播）**：核心难点是「形状关系多变」（首维广播、中间维广播、带 stride 广播、int64 拆半），需要大量 `if` 分支。

#### 4.2.2 核心流程

**reduce 的两层结构**：

```text
上层 codegen 调用：ReduceSum / ReduceSumInt32 / ReduceMax ...
                          │
        ┌─────────────────┴──────────────────┐
   适配层（Adapt）                      归约调度层（ReduceLast）
   WholeReduceSumAdapt                   按 (m, k) 大小分 4 个分支
   WholeReduceMaxAdapt                   大 k 时用 BinaryFunc 合并部分归约
   WholeReduceMeanAdapt（=Sum+Muls）            │
                          │                     │
                          └──► 原生 WholeReduceSum / Max / Min（CANN 提供）
```

适配层的价值：把原生接口参数个数 / 顺序的差异抹平，让上层用一个**统一的函数指针类型**（9 个参数）来模板化调用，无需关心底层是 Sum 还是 Max。

**broadcast 的分发树**：

```text
Broadcast(dst, src, src_m, src_k, src_z, dst_m, dst_k, dst_z, tmp_buf)
   │  if constexpr SupportType<T, ...>   按 dtype 分发
   ├──► BroadcastInt64        (int64/uint64：拆成 int32 处理)
   ├──► BroadcastWithCast     (int8/uint8：按 uint16 重解释)
   └──► BroadcastCommon       (half/float/int32...)
          │  按 src/dst 的 (m,k,z) 形状关系分发
          ├──► BroadcastFirstDim     (1,B)->(A,B)  首维广播
          ├──► BroadcastWithStride   带跨度的末维广播
          └──► AscendC::Broadcast    原生广播
```

#### 4.2.3 源码精读

**reduce.h —— 适配层抹平签名差异。** 注意四个 `WholeReduceXxxAdapt` 的签名完全相同（9 个参数，末尾都有 `ReduceOrder order, const int32_t k`），但内部转调的原生接口参数不同：

[autofuse/ascendc/api/reduce.h:L13-L44](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascendc/api/reduce.h#L13-L44) —— `WholeReduceSumAdapt` 内部调 7 参的 `WholeReduceSum`（不传 order）；`WholeReduceMaxAdapt/MinAdapt` 调 8 参的 `WholeReduceMax/Min`（传 order）；`WholeReduceMeanAdapt` 则是「先 Sum 再 `Muls`（乘以 1/k）」。四个函数对外签名一致，对内各取所需——这就是适配层。

**reduce.h —— ReduceLast 模板按 (m,k) 分支调度。** 模板参数 `WholeReduceFunc` 就是上面适配函数的类型，`BinaryFunc` 用于大 k 时合并多次部分归约：

[autofuse/ascendc/api/reduce.h:L46-L78](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascendc/api/reduce.h#L46-L78) —— 按 `m`、`k` 与硬件单位 `m_repeat_size`、`k_repeat_size` 的关系分四种情况：小 m 小 k 直接一次 `WholeReduceFunc`；大 m 按行循环；大 k 则分段归约后用 `BinaryFunc`（如 `Add`）累加。这把「一次做不完的归约」拆成了可被 tiling 控制的循环。

**reduce.h —— ReduceSumInt32 补齐 int32 能力。** 原生 `WholeReduceSum` 不支持 int32，于是用 `Add`（按 mask 折半累加）+ `Brcb`（块复制）+ `GatherMask`（按掩码收集）手工实现：

[autofuse/ascendc/api/reduce.h:L187-L210](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascendc/api/reduce.h#L187-L210) —— 入口 `ReduceSumInt32` 用 `static_assert` 限定只支持 AR/RA 两种归约模式和 int32_t，再按模式分发到 `ReduceSumByLastAxis`（末轴归约）或 `BinaryReduceByFirstAxis`（首轴归约）。

**datacopy.h —— 两个方向的 DataCopyPadExtend。** 一个重载 GM→UB（load），一个 UB→GM（store），都封装了 `DataCopyExtParams` 的单位换算：

[autofuse/ascendc/api/datacopy.h:L13-L40](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascendc/api/datacopy.h#L13-L40) —— `blockLen`/`srcStride`/`dstStride` 在两个方向上的「字节 vs 块」换算正好相反：GM→UB 时 `dstStride` 除以 `align_num` 折成块数；UB→GM 时 `srcStride` 除以 `align_num`。这种方向性差异由两个重载分别承担，上层只需按 load/store 各调一次同名函数。

**broadcast.h —— Broadcast 按 dtype 顶层分发。** 用 `if constexpr (SupportType<T, ...>)` 在编译期选实现：

[autofuse/ascendc/api/broadcast.h:L511-L525](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascendc/api/broadcast.h#L511-L525) —— `Broadcast` 三分支：int64/uint64 走 `BroadcastInt64`（拆成 int32 处理）；int8/uint8 走 `BroadcastWithCast`（按 uint16 重解释）；half/float/int32 等走 `BroadcastCommon`。不支持的类型直接 `ASSERT(false)`。

**broadcast.h —— BroadcastCommon 按形状关系分发。** 这是 broadcast 逻辑最集中的地方：

[autofuse/ascendc/api/broadcast.h:L296-L333](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascendc/api/broadcast.h#L296-L333) —— 按 `src` 与 `dst` 的 (m,k,z) 关系分流：`(1,B)->(A,B)` 首维广播走 `BroadcastFirstDim`；`(A,1)->(A,B)` 末维广播按是否带 stride 选 `BroadcastWithStride` 或原生 `AscendC::Broadcast`；`(1,1)->(A,B)` 走原生广播。每个分支内部又按对齐与否（`dst_k * sizeof(T) % ONE_BLK_SIZE`）再分。

#### 4.2.4 代码实践

**实践目标**：读懂一个 vector 算子头文件的完整签名与内部分发。

**操作步骤**：

1. 打开 `autofuse/ascendc/api/broadcast.h`，定位 8 参数版 `Broadcast`（[L511-L525](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascendc/api/broadcast.h#L511-L525)）。
2. 写下它的完整签名：

   ```cpp
   template <typename T>
   inline __aicore__ void Broadcast(const LocalTensor<T> &dst, const LocalTensor<T> &src,
                                    const uint32_t src_m, const uint32_t src_k, const uint32_t src_z,
                                    const uint32_t dst_m, const uint32_t dst_k, const uint32_t dst_z,
                                    LocalTensor<uint8_t> &tmp_buf, const uint32_t last_dim_stride = 1);
   ```

3. 追踪 `T = half` 时的调用路径：`Broadcast` →（`SupportType` 命中最后一条）→ `BroadcastCommon` → 若 `src_m==1 && src_k==dst_k` → `BroadcastFirstDim`。

**需要观察的现象**：`Broadcast` 的参数是「源三维 (src_m, src_k, src_z) + 目的三维 (dst_m, dst_k, dst_z)」的形状描述，而不是张量本身的 shape；`tmp_buf` 是片上临时缓冲（由 u5-l2 的 reg_func 估算大小）。

**预期结果**：你能用一句话描述 codegen 会如何调用它——「按计算出的源/目的形状三元组，调用 `Broadcast(dst, src, src_m, src_k, src_z, dst_m, dst_k, dst_z, tmp_buf, last_dim_stride)`，dtype 由模板参数 `T` 在编译期确定」。这正是 4.3 里 `BroadcastApiCall` 实际生成的语句。

#### 4.2.5 小练习与答案

**练习 1**：`WholeReduceMeanAdapt` 为什么内部调的是 `WholeReduceSum` 而不是某个 `WholeReduceMean`？
> **答**：硬件没有原生的 Mean 指令。Mean = Sum / N，所以封装层先调 `WholeReduceSum` 归约求和，再用 `Muls` 乘以 `1/k`（即除以元素数 k）得到均值。这也解释了它为什么需要多一个 `k` 参数。

**练习 2**：`DataCopyPadExtend` 为什么需要两个重载，而不是用一个函数加方向参数？
> **答**：两个方向的参数类型不同（一个是 `GlobalTensor` 源 + `LocalTensor` 目的，另一个相反），C++ 靠参数类型重载，比加运行时方向标志更安全、零开销。这也让 load/store 两侧的 stride 单位换算各自独立、互不干扰。

---

### 4.3 与 codegen 的衔接（字符串嵌入机制）

这是本讲最关键、也最容易误解的一节。读完它你才会真正理解大纲里「ascendc/api 为 codegen 提供算子能力」这句话。

#### 4.3.1 概念说明

先排除一个常见误解：**ascendc/api 的头文件并不是被 codegen 用普通的 `#include` 引入的。** 因为生成的设备 kernel 是一段**自包含的 C++ 源码字符串**，它会被送到独立的设备编译器编译；codegen 不能依赖固定的头文件搜索路径。于是项目用了一个巧妙办法——**把整个头文件的源码，当作一段字符串，原样拼进生成的 kernel 源码里**。

这样，codegen 与 ascendc/api 的关系就拆成了两半：

- **codegen 的 `api_call` 层**：负责生成「**调用语句**」文本，例如 `DataCopyPadExtend(ub, gm[off], 1, 128, 0, 0);`。
- **ascendc/api 的头文件**：提供「**函数定义**」，作为字符串被嵌入 kernel，让上面那句调用有定义可指。

两端靠**相同的函数名 + 相同的签名**对接。这就是衔接的本质。

#### 4.3.2 核心流程

完整的四步机制如下：

```text
① 构建期（CMake）
   每个 .h  ──cat|sed 加 R"===( )==="──►  <name>_str.h   （头源码 → 原始字符串字面量）

② codegen 启动期（静态注册）
   *_str.h  ──读成 std::string──►  api_to_file 映射  ──RegisterApi──►  AscendCApiRegistry 单例
                                 {"reduce.h": "...源码...", "datacopy.h": "...源码..."}

③ codegen 生成期（按需取内容）
   遍历图节点 ──► node 的 codegen impl ──► LoadApiHeaderFiles() 返回 ["reduce.h", ...]
                                          ──► GetFileContent("reduce.h") 取出源码字符串

④ 拼进 kernel 源码（去重）
   ss << file;   把头源码整段拼进 kernel .cpp；用集合去重，每个头只嵌一次

   并行地，api_call 层生成调用语句：DataCopyPadExtend(...) / Broadcast(...) / ReduceSumInt32(...)
```

关键点：嵌入是**按需**的——图里用了哪些算子，就把对应头的源码嵌进去；没用到的算子头不会进 kernel。`utils.h` 与 `brc_inline_api.h` 是公共基础，无条件嵌入。

#### 4.3.3 源码精读

**① 构建期：sed 把头源码包成原始字符串字面量。**

[autofuse/ascendc/api/CMakeLists.txt:L52-L73](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascendc/api/CMakeLists.txt#L52-L73) —— `add_custom_command` 对每个 `.h` 执行 `cat reduce.h | sed '1i\R"===(' | sed '$a\)==="' > reduce_str.h`，即在文件最前面插一行 `R"===(`、最后面插一行 `)==="`。效果是把 reduce.h 的全部内容变成了一个合法的 C++ 原始字符串字面量 `R"===( ... )==="`，写入 `reduce_str.h`。这样 `reduce_str.h` 本身就可以被当作一段字符串字面量来 `#include`。

**② 启动期：把字符串注册进全局表。**

[autofuse/codegen/ascendc_api_registry.cpp:L19-L31](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascendc_api_registry.cpp#L19-L31) —— 一个静态 `Register` 对象（程序启动时自动构造）把每个 `*_str.h` 读成 `std::string`（如 `kAscendcBroadcastStr`、`kAscendcDatacopyStr`、`kAscendcReduceStr`）。注意写法 `const std::string kAscendcBroadcastStr = { #include "broadcast_str.h" };`——直接把字符串字面量文件包含进来初始化 string。

[autofuse/codegen/ascendc_api_registry.cpp:L176-L218](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascendc_api_registry.cpp#L176-L218) —— 把这些 string 组装成 `api_to_file` 映射（`"broadcast.h" -> kAscendcBroadcastStr`、`"datacopy.h" -> ...`、`"reduce.h" -> ...`），调用 `AscendCApiRegistry::GetInstance().RegisterApi(api_to_file)` 登记进单例。`GetFileContent(api_name)` 则是取出口（[L229-L233](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascendc_api_registry.cpp#L229-L233)）。

**③④ 生成期：按图中算子取出源码并拼进 kernel，去重。**

[autofuse/codegen/codegen_kernel.cpp:L3688-L3713](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/codegen/codegen_kernel.cpp#L3688-L3713) —— 遍历图所有节点，取每个节点 codegen impl 的 `LoadApiHeaderFiles()` 返回的头名（如 `"reduce.h"`），用 `GetFileContent(header_str)` 取出源码字符串，`ss << file;` 整段拼进 kernel 源码；用 `kernel_file_ptr` 集合按字符串地址去重，保证每个头只嵌入一次。

[autofuse/codegen/codegen_kernel.cpp:L2508-L2516](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/codegen/codegen_kernel.cpp#L2508-L2516) —— `utils.h`（`utils_str.h`）与 `brc_inline_api.h`（`brc_inline_api_str.h`）作为公共基础，无条件拼进每个 kernel。

**另一端：api_call 层生成「调用语句」。** 这是与上面「函数定义」对接的「函数调用」。

[autofuse/codegen/api_call/datacopy/load_api_call.cpp:L49-L51](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/codegen/api_call/datacopy/load_api_call.cpp#L49-L51) —— `LoadApiCall::Generate` 用 `std::stringstream` 拼出 `DataCopyPadExtend(ub, gm[offset], block_count, block_len, src_stride, dst_stride);`。这里的 `DataCopyPadExtend` 正是 datacopy.h 里定义的函数。

[autofuse/codegen/api_call/datacopy/store_api_call.cpp:L64-L66](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/codegen/api_call/datacopy/store_api_call.cpp#L64-L66) —— `StoreApiCall::Generate` 同样拼 `DataCopyPadExtend(gm[offset], ub, ...)`，方向相反，对应 datacopy.h 的另一个重载。

[autofuse/codegen/api_call/broadcast/broadcast_api_call.cpp:L195-L199](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/codegen/api_call/broadcast/broadcast_api_call.cpp#L195-L199) —— `BroadcastOneAxis` 拼出 `Broadcast(dst[off], src[off], src_m, src_k, src_z, dst_m, dst_k, dst_z, tmp_buf_id, last_dim_stride);`，签名与 broadcast.h 的 `Broadcast` 完全对应。

[autofuse/codegen/api_call/reduce/reduce_api_call.cpp:L79-L100](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/codegen/api_call/reduce/reduce_api_call.cpp#L79-L100) —— reduce 调用生成器还有「语义重写」：`ReduceMean` 实际生成对 `ReduceSum` 的调用；而 `ReduceSum + int32_t` 则改名为 `ReduceSumInt32`（对应 reduce.h 里手搓的那个实现）。这正好呼应 4.2.1 里 `WholeReduceMeanAdapt` 与 `ReduceSumInt32` 的存在。

至此整条链路闭合：**codegen 拼调用语句 → ascendc/api 提供同名的函数定义（以字符串嵌入 kernel）→ 设备编译器把两者编译成一个完整 kernel。**

#### 4.3.4 代码实践

**实践目标**：跟踪一个具体算子（Load），把「定义嵌入」与「调用生成」两端对上。

**操作步骤**：

1. 在 [load_api_call.cpp:L49](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/codegen/api_call/datacopy/load_api_call.cpp#L49) 确认调用语句里用的函数名是 `DataCopyPadExtend`。
2. 在 [datacopy.h:L14](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascendc/api/datacopy.h#L14) 确认同名函数 `DataCopyPadExtend` 的 GM→UB 重载签名，与调用语句的实参（`ub, gm[off], block_count, block_len, src_stride, dst_stride`）一一对应。
3. 在 [ascendc_api_registry.cpp:L186](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/codegen/ascendc_api_registry.cpp#L186) 确认 `"datacopy.h"` 已注册进表。
4. 推理：只要图里出现了 Load 节点，codegen 就会把 `datacopy.h` 的源码嵌进 kernel（步骤 ③④），同时生成 `DataCopyPadExtend(...)` 调用（api_call 层），编译后二者对接。

**需要观察的现象**：调用语句的实参个数、顺序、单位（`block_len` 是元素数还是字节数？）必须与定义的形参完全一致，否则设备编译期报错。这正是「签名对接」的刚性约束。

**预期结果**：你能画出一张时序图，标清四步（sed 包装 → 启动注册 → 按需取内容 → 拼进 kernel）各自发生在构建期还是运行期、各自对应哪个文件。

> 说明：本实践为「源码阅读型实践」，无需上板运行；若要验证最终嵌入效果，可参考 u3-l3 的 `AUTOFUSE_DFX_FLAGS` dump 出 host/device 源码，在 device `.cpp` 中直接看到被嵌入的 `DataCopyPadExtend` 定义与调用。运行结果「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 ascendc/api 用「字符串嵌入」而不是让设备 kernel 直接 `#include "reduce.h"`？
> **答**：生成的设备 kernel 是一段自包含源码字符串，会被独立送给设备编译器，不能假设固定的头文件搜索路径。把定义以字符串嵌入，保证 kernel 源码自洽、可移植，也方便按图中算子按需裁剪（只用到的头才嵌入）。

**练习 2**：如果新增一个算子的设备端封装 `foo.h`，要让 codegen 能用上它，至少要改哪几处？
> **答**：四处：① 在 `ascendc/api/CMakeLists.txt` 的 `ascendc_api_extend_src` 列表加 `foo.h`；② 在 `ascendc_api_registry.cpp` 加 `#include "foo_str.h"`、对应 string 变量，并在 `api_to_file` 加 `{"foo.h", kAscendcFooStr}`；③ 实现对应的 codegen `api_call` 生成器来产出调用语句；④ （若需 tiling）在 ASCIR 注册里挂上 ATT/codegen 实现（见 u5-l1）。其中 ①②③ 是本讲直接相关的最小改动面。

**练习 3**：`utils.h` 为什么在 `IncludeAndDefines` 里**无条件**嵌入，而 reduce.h 是**按需**嵌入？
> **答**：`utils.h` 里的 `KernelUtils::BlkSize/RptSize/Min/Max` 是几乎所有算子封装都要用的公共基础，任何 kernel 都可能依赖，故无条件嵌入省去逐算子登记的麻烦；reduce.h 这类是特定算子的实现，只有图里真用了 reduce 才需要，按需嵌入可减小生成 kernel 的体积。

---

## 5. 综合实践

把本讲三个模块串起来，做一次「全链路追踪」。

**任务**：假设融合子图里有这样一个算子序列——`Load(x) → Broadcast(x) → Add → Store(y)`。请回答：

1. **能力来源**：这四个步骤分别会用到 `ascendc/api/` 下哪些头文件里的函数？（提示：Load/Store 用 datacopy.h，Broadcast 用 broadcast.h，Add 用 elewise 类。）
2. **签名对接**：写出 Broadcast 这一步 codegen 会生成的调用语句骨架（参照 [broadcast_api_call.cpp:L195-L199](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/codegen/api_call/broadcast/broadcast_api_call.cpp#L195-L199)），并指出它与 [broadcast.h:L512](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascendc/api/broadcast.h#L512) 的 `Broadcast` 定义如何通过参数一一对应。
3. **嵌入裁剪**：这四个算子的定义里，哪些头会被嵌入生成的 kernel，哪些不会？依据是什么（参照 [codegen_kernel.cpp:L3688-L3713](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/codegen/codegen_kernel.cpp#L3688-L3713) 的按需 + 去重逻辑）？
4. **dtype 影响**：若 `x` 是 `int64`，Broadcast 这一步会走 broadcast.h 里哪条 `if constexpr` 分支？对嵌入的代码有何影响？

**参考思路**：

1. datacopy.h（DataCopyPadExtend 两个方向）、broadcast.h（Broadcast）、elewise 的 add 封装（或直接原生 Add）。
2. `Broadcast(dst[off], src[off], src_m, src_k, src_z, dst_m, dst_k, dst_z, tmp_buf_id, last_dim_stride);`——实参与定义形参 `(dst, src, src_m, src_k, src_z, dst_m, dst_k, dst_z, tmp_buf, last_dim_stride)` 按位置对应。
3. 会嵌入：datacopy.h、broadcast.h、add 对应头，以及无条件的 utils.h、brc_inline_api.h。不会嵌入：图里没出现的算子头（如 reduce.h、cast.h）。依据是 `LoadApiHeaderFiles()` 只返回图中节点声明依赖的头，并用集合去重。
4. 走 `BroadcastInt64` 分支（[broadcast.h:L516-L517](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascendc/api/broadcast.h#L516-L517)），它会把 int64 拆成两个 int32 处理；嵌入的源码不变（整个 broadcast.h 都嵌进去），差异由编译期 `if constexpr` 选择。

## 6. 本讲小结

- `autofuse/ascendc/api/*.h` 是 **CANN 原生 AscendC API 之上的二次封装**，动机是补齐 dtype/场景、统一签名、承担对齐与分块调度；函数都标 `template + inline __aicore__`，跑在设备端。
- 目录按职责分四类：搬运（datacopy）、计算（elewise/reduce/broadcast/compare/cast/scalar）、形状（concat/gather/transpose）、公共基础（utils/brc_inline_api）。`KernelUtils::BlkSize/RptSize` 是换算块/重复单位的公共工具。
- 三类代表各有侧重：**reduce** 用 `Adapt` 适配层抹平原生 7/8 参签名、用 `ReduceLast` 模板按 (m,k) 分支、用 `ReduceSumInt32` 补 int32；**datacopy** 用两个重载分别承担 GM↔UB 的 load/store；**broadcast** 按 dtype 与 (m,k,z) 形状多层 `if constexpr`/`if` 分发。
- **衔接的本质**：codegen 的 `api_call` 层生成「**调用语句**」，ascendc/api 提供「**函数定义**」，两端靠相同函数名 + 签名对接。
- 定义以**字符串嵌入**进 kernel：CMake 用 sed 把 `.h` 包成原始字符串字面量 `*_str.h` → 启动期注册进 `AscendCApiRegistry` → 生成期按图中算子 `GetFileContent` 按需取出、`ss << file` 拼进 kernel、集合去重。嵌入是按需的，`utils.h`/`brc_inline_api.h` 无条件嵌入。
- 新增设备端算子封装的最小改动面：`api/CMakeLists.txt` + `ascendc_api_registry.cpp` + 对应 `api_call` 生成器（+ ASCIR 注册）。

## 7. 下一步学习建议

- **顺数据流向下**：本讲讲清了「算子能力的来源」，下一步进入 u8 读懂 `Codegen::Generate` 主流程与 `api_call` 工厂如何按算子类型选择调用生成器（u8-l1、u8-l3）。本讲的 `LoadApiCall/BroadcastApiCall/ReduceApiCall` 正是 u8-l3 `api_call` 体系的具体成员。
- **回看注册**：若想理解「节点 codegen impl 的 `LoadApiHeaderFiles()` / `IncludeApiHeaderFiles()` 返回的头名是怎么来的」，可回看 u5-l1 的 `AscIrImpl`（codegen 实现创建器）——那里登记的正是本讲 `GetFileContent` 查询用的头名。
- **平台扩展对照**：u11-l2 会讲 v35 平台下的 `ascendc/api_cube/`（matmul/conv2d 等 cube 类算子），与本讲的 vector 类 API 形成对照——cube 算子的 tiling 与 API 形态更复杂，但「字符串嵌入 + 签名对接」的衔接机制是一样的。
- **验证手段**：想亲眼看到「被嵌入的定义 + 生成的调用」同框，可结合 u3-l3 的 `AUTOFUSE_DFX_FLAGS` dump 出 device 源码阅读。
