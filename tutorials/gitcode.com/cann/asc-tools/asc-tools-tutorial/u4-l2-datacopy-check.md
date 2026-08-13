# DataCopy 搬运类校验

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚「搬运类 API」为什么需要一类专门的校验器，它与向量计算类校验的关注点有何不同。
- 读懂 `DataCopy`、`DataCopyPad`、`DataCopySlice` 三个变体校验器的开源实现，并指出它们各自检查的关键条件。
- 解释 32 字节对齐、scope 合法性、搬运指令数上限等硬件约束是如何用源码表达出来的。
- 对照 `add.asc` 中的 `DataCopy` 调用，推断它会触发哪些检查，并能动手构造一个会触发报错的用例。

本讲是 [u4-l1 校验基类与通用检查机制](u4-l1-base-check-framework.md) 的直接延续，复用其中讲过的「三层结构」「`ASCENDC_CHECK` 宏」「基类通用检查」等结论。

## 2. 前置知识

在进入本讲前，请确认你已理解下面这些来自前置讲义的概念：

- **搬运（DataCopy）是什么**：在 Ascend C 里，`DataCopy` 负责在 Global Memory（GM，显存）与片上缓冲（UB/L1 等）之间、或片上缓冲彼此之间搬运数据，对应 NPU 上由 DMA 引擎（MTE2/MTE3）执行的指令。参见 [u2-l2 Ascend C 算子源码与 .asc 核函数结构](u2-l2-asc-kernel-source.md) 中 `add.asc` 的 `CopyIn/CopyOut` 三段式。
- **api_check 三层结构**：入口函数层（`CheckFuncXxxImpl`）→ 校验器子类（`TikcppXxxCheck`）→ 基类与通用函数（`TikcppBaseCheck`）。参见 [u4-l1](u4-l1-base-check-framework.md)。
- **`ASCENDC_CHECK` / `ASCENDC_CHECK_AND_LOG` 宏**：以「失败即 `return false`」短路退出，错误经 `CHECK_LOG_ERROR` 输出，最终在 CPU 域运行算子时记入 `*_npuchk.log`。参见 [u4-l1](u4-l1-base-check-framework.md)。
- **npuchk 钩子链**：cpudebug 在 CPU 域执行算子时，会经由 npuchk 类 stub 调用这些 `CheckFuncXxxImpl`，做到「边跑边查」。参见 [u3-l3 Stub 注册与内建函数转义](u3-l3-stub-registration.md)。

本讲引入的几个新术语：

| 术语 | 含义 |
| --- | --- |
| scope（存储位置） | 一个 Tensor 物理上落在哪块硬件存储里：GM / UB / L1 / L0A / L0B / L0C / BIAS 等 |
| 逻辑位置 vs 物理位置 | 源码里写的 `TPosition`（如 `VECIN`/`VECCALC`）是「逻辑位置」，需要经 `GetPhyType` 映射到「物理位置」`Hardware`（如 `UB`）才能查对齐表 |
| burst（搬运粒度） | DMA 一次搬运的最小/单位长度，不同 scope 组合的单位字节数不同 |
| 32B 对齐 | 地址相对硬件基址的偏移必须是 32 字节的整数倍，这是 UB 的基本约束 |

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [cpudebug/utils/include/utils/kernel_check_data_copy_util.h](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/utils/include/utils/kernel_check_data_copy_util.h) | 定义搬运类校验的**参数结构体**（`CopyApiParams`、`DataCopyApiParams`、`DataCopyPadApiParams`、`DataCopySliceApiParams`）、若干辅助模板（`GetBurstLenUnit` 等）和**入口函数声明** |
| [cpudebug/src/api_check/inc/kernel_data_copy_check.h](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/inc/kernel_data_copy_check.h) | `TikcppDataCopyCheck` 校验器类的声明 |
| [cpudebug/src/api_check/kernel_data_copy_check.cpp](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_data_copy_check.cpp) | `DataCopy` 的对齐校验实现，含关键的 `alignSizeMap` |
| [cpudebug/src/api_check/inc/kernel_data_copy_pad_check.h](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/inc/kernel_data_copy_pad_check.h) / [kernel_data_copy_pad_check.cpp](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_data_copy_pad_check.cpp) | `DataCopyPad` 变体校验器（padding 合法性） |
| [cpudebug/src/api_check/inc/kernel_data_copy_slice_check.h](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/inc/kernel_data_copy_slice_check.h) / [kernel_data_copy_slice_check.cpp](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_data_copy_slice_check.cpp) | `DataCopySlice` 变体校验器（scope、形状一致性、指令数、对齐） |
| [cpudebug/src/api_check/kernel_check_util.cpp](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_check_util.cpp) | 三个入口函数 `CheckFuncDataCopy*Impl` 的实现：实例化校验器并调用 `CheckAllHighLevel()` |
| [examples/02_cpudebug/add.asc](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc) | 实践对象：里面的 `DataCopy` 调用就是我们要分析的样本 |

补充：基类通用检查（`CheckTensorScope`/`CheckBufferSizeOverFlow`/`CheckTensorAddrAlign`）在 [cpudebug/src/api_check/inc/kernel_base_check.h](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/inc/kernel_base_check.h) 中声明，已在 u4-l1 讲过，本讲直接复用。

## 4. 核心概念与源码讲解

### 4.1 DataCopy 校验：地址对齐与硬件差异

#### 4.1.1 概念说明

`DataCopy` 是 Ascend C 的「搬运总管」。在真实 NPU 上，搬运由 DMA 引擎执行，硬件对源地址、目的地址有严格的对齐要求；一旦不满足，轻则结果错乱，重则触发硬件异常。

cpudebug 的做法是：在 CPU 域孪生执行这条 `DataCopy` 之前，先经由 npuchk 钩子调用 `CheckFuncDataCopyImpl`，做一次「虚拟硬件检查」。这样可以把原本要上了真机才发现的「地址没对齐」一类问题，前移到 CPU 阶段就暴露出来。

注意：**开源可见的 `DataCopy` high-level 校验只做对齐检查**，不显式做「搬运长度是否超出 Tensor 容量」的越界检查（后者在 `DataCopySlice` 变体和更底层的机制里有，见 4.2、4.3）。这是一个需要诚实面对的边界。

#### 4.1.2 核心流程

`DataCopy` 校验的调用链非常短：

```text
CheckFuncDataCopyImpl(params, "DataCopy")        # 入口（kernel_check_util.cpp）
  ├─ ASCENDC_CHECK_INTRI_NAME(intriName)         # 内建函数名非空校验
  ├─ 构造 TikcppDataCopyCheck{"DataCopy", params} # 实例化校验器
  └─ chkIns.CheckAllHighLevel()                  # 总入口
       └─ ASCENDC_CHECK(CheckAddrAlign())        # 只做地址对齐
            ├─ CheckDataCopyAlign(srcAddr, srcPos, isSrc=true)
            └─ CheckDataCopyAlign(dstAddr, dstPos, isSrc=false)
                 └─ 按 pos 查 alignSizeMap → CheckTensorAddrAlign(addr, pos, alignBytes, ...)
```

关键设计是**按物理位置查对齐粒度**：GM 是 1 字节对齐（相当于不查），L1/UB 要求 32 字节，L0A/L0B/L0C 要求 512 字节，BIAS 要求 64 字节。这套差异完全由一张 `alignSizeMap` 表达。

#### 4.1.3 源码精读

先看入口函数，它体现了 u4-l1 说的「入口函数层」的统一写法——校验名、构造校验器、调 `CheckAllHighLevel`：

[cpudebug/src/api_check/kernel_check_util.cpp:61-69](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_check_util.cpp#L61-L69) —— `CheckFuncDataCopyImpl`：先校验 `intriName` 非空，再构造 `TikcppDataCopyCheck` 并调用 `CheckAllHighLevel()`。

其中 `ASCENDC_CHECK_INTRI_NAME` 是一个共用的防呆宏，名字为空指针或空串时记日志并 `return false`：

[cpudebug/src/api_check/kernel_check_util.cpp:23-29](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_check_util.cpp#L23-L29) —— 名字防呆宏。

再看校验器本身。`CheckAllHighLevel` 只调了 `CheckAddrAlign`，确认了 4.1.1 说的「只做对齐」：

[cpudebug/src/api_check/kernel_data_copy_check.cpp:21-25](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_data_copy_check.cpp#L21-L25) —— `CheckAllHighLevel` 用 `ASCENDC_CHECK(CheckAddrAlign())` 把对齐检查包起来（失败即 `return false`）。

核心是这张对齐粒度表，以及「GM 跳过」的特判：

```cpp
std::map<Hardware, uint32_t> alignSizeMap = {
    {Hardware::GM, 1},
    {Hardware::L1, 32},
    {Hardware::UB, 32},
    {Hardware::L0A, 512 * ((dataType + 1) / sizeof(uint16_t))},
    {Hardware::L0B, 512 * ((dataType + 1) / sizeof(uint16_t))},
    {Hardware::L0C, 512 * ((dataType + 1) / sizeof(uint16_t))},
    {Hardware::BIAS, 64},
};
if (pos != static_cast<uint8_t>(Hardware::GM)) {
    uint32_t alignBytes = alignSizeMap[static_cast<Hardware>(pos)];
    ... CheckTensorAddrAlign(addr, pos, alignBytes, isSrc ? "src" : "dst");
}
```

[cpudebug/src/api_check/kernel_data_copy_check.cpp:27-50](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_data_copy_check.cpp#L27-L50) —— `CheckDataCopyAlign`：按 scope 取对齐粒度，GM 直接跳过，其余交给基类 `CheckTensorAddrAlign`。

[cpudebug/src/api_check/kernel_data_copy_check.cpp:51-56](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_data_copy_check.cpp#L51-L56) —— `CheckAddrAlign`：分别对源、目的各做一次对齐检查，两者都通过才返回 `true`。

校验器类的声明很简单，继承 `TikcppBaseCheck`，持有一个 `DataCopyApiParams&` 成员：

[cpudebug/src/api_check/inc/kernel_data_copy_check.h:22-32](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/inc/kernel_data_copy_check.h#L22-L32) —— `TikcppDataCopyCheck` 类声明。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：把 `add.asc` 里的两条 `DataCopy` 与本讲的检查条件对上号。

**操作步骤**：

1. 打开 [examples/02_cpudebug/add.asc](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc)，定位 `CopyIn` 与 `CopyOut`：

   ```cpp
   AscendC::DataCopy(xLocal, xGm[progress * TILE_LENGTH], TILE_LENGTH);   // GM -> UB
   AscendC::DataCopy(zGm[progress * TILE_LENGTH], zLocal, TILE_LENGTH);   // UB -> GM
   ```

   见 [add.asc:59-60](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L59-L60) 与 [add.asc:77](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L77)。

2. 对 `CopyIn`：源是 `xGm`（GM），目的是 `xLocal`（UB）。按 `alignSizeMap`，源侧 `GM` 跳过、目的侧 `UB` 需 32 字节对齐。
3. 对 `CopyOut`：源是 `zLocal`（UB），目的是 `zGm`（GM）。源侧 `UB` 需 32 字节对齐、目的侧 `GM` 跳过。

**需要观察的现象**：源/目的只要有一侧落在 UB/L1，就会触发 32 字节对齐检查；落在 GM 的一侧不查。

**预期结果**：`add.asc` 正常运行时，`xLocal`/`zLocal` 由 `AllocTensor` 从 UB 分配，天然 32 字节对齐，因此 `CheckAddrAlign` 通过，不会产生 `*_npuchk.log` 报错。

> 说明：本实践是「源码阅读型」，无需运行；若要实际运行样例，参见 [u1-l4 一键编译与运行第一个样例](u1-l4-build-and-first-sample.md)。

#### 4.1.5 小练习与答案

**练习 1**：如果一次 `DataCopy` 的源在 L0A、目的在 UB，按 `alignSizeMap` 各自要求多少字节对齐？
**答案**：源 L0A 要求 `512 * ((dataType + 1) / sizeof(uint16_t))` 字节对齐（与数据类型相关），目的 UB 要求 32 字节对齐。

**练习 2**：为什么 `CheckDataCopyAlign` 里要特判 `pos != Hardware::GM`？
**答案**：GM 在表里取值就是 1（1 字节对齐），任何地址都满足，等价于「不查」；特判是为了避免对 GM 地址做无意义的取模与日志准备。

### 4.2 pad/slice 变体校验

#### 4.2.1 概念说明

`DataCopy` 还有两个常用变体：

- **`DataCopyPad`**：搬运的同时在目的侧做左右填充（left/right padding），常用于把不规则长度补齐到硬件要求的块。
- **`DataCopySlice`**：支持多维切片搬运，每维可以指定起止下标与步长（`SliceInfo`），常用于从 GM 抽取一个不连续子张量到 UB。

它们参数更复杂，校验重点也不同于纯对齐：**Pad 关注填充量的合法性**，**Slice 关注切片形状的一致性与搬运指令数的上限**。

参数结构体的继承关系如下（全部定义在 util 头文件里）：

```text
DataCopyBaseParams            # 公共：地址、dtype、scope、blockCount/blockLen/stride
  ├─ DataCopyApiParams        # 普通 DataCopy
  └─ DataCopyPadApiParams     # + isPad/leftPadding/rightPadding/paddingValue
DataCopySliceApiParams        # 独立结构：多维 shape + SliceInfo[] + isGM2UB
```

[cpudebug/utils/include/utils/kernel_check_data_copy_util.h:81-113](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/utils/include/utils/kernel_check_data_copy_util.h#L81-L113) —— `DataCopyBaseParams` 公共字段。
[cpudebug/utils/include/utils/kernel_check_data_copy_util.h:126-145](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/utils/include/utils/kernel_check_data_copy_util.h#L126-L145) —— `DataCopyPadApiParams` 新增的填充字段。
[cpudebug/utils/include/utils/kernel_check_data_copy_util.h:147-184](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/utils/include/utils/kernel_check_data_copy_util.h#L147-L184) —— `DataCopySliceApiParams`，含 `srcShape/dstShape` 与 `srcSliceInfo/dstSliceInfo` 数组。

其中 `SliceInfo` 是切片描述的五元组：

[cpudebug/utils/kernel_utils.h:87-105](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/utils/kernel_utils.h#L87-L105) —— `SliceInfo { startIndex, endIndex, stride, burstLen, shapeValue }`。

#### 4.2.2 核心流程

**Pad 校验流程**：

```text
CheckFuncDataCopyPadImpl → TikcppDataCopyPadCheck::CheckAllHighLevel
  └─ ASCENDC_CHECK(CheckPadParamters())
       ├─ leftPadding  ≤ ⌊32 / srcDtypeBytes⌋   否则报错
       ├─ rightPadding ≤ ⌊32 / srcDtypeBytes⌋   否则报错
       └─ 若 isPad 且 dtype==B64(B64_BYTE_SIZE=8) 且 paddingValue!=0  → 报错
```

填充上限用元素个数表达，且「填充总字节数 ≤ 32」：

\[
\text{paddingLimit} = \left\lfloor \frac{32}{\text{srcDtypeBytes}} \right\rfloor
\]

**Slice 校验流程**（明显更重）：

```text
CheckFuncDataCopySliceImpl → TikcppDataCopySliceCheck::CheckAllHighLevel
  ├─ CheckTensorScope(logicPos, UB, ...)        # 必须落在 UB
  ├─ CheckDataCopyInstrsNum(...)                # src 与 dst 指令数相等 且 ≤ MAX_SLICE_SIZE(=1536)
  ├─ CheckBufferSizeOverFlow(sizeNum, bufferSizeMap[pos])  # 不超过 UB 容量
  ├─ CheckSliceInfoParamters(...)               # 逐维检查 SliceInfo 一致性（6 条规则）
  ├─ CheckDataCopyIntrsParamters(...)           # block count 整除、stride 是 UB_BLOCK_SIZE(32) 倍数
  └─ CheckAddrAlign()                           # 32 字节对齐
```

其中搬运指令总数的计算（`DataCopyGetTotalInstrsNum`）对每一维做：

\[
\text{currentCount}_i = \frac{(\text{endIndex}_i - \text{startIndex}_i + 1) + \text{stride}_i}{1 + \text{stride}_i}, \quad \text{totalInstrsNum} = \prod_i \text{currentCount}_i
\]

#### 4.2.3 源码精读

**Pad 实现**很短，重点就是填充上限与 B64 特判：

[cpudebug/src/api_check/kernel_data_copy_pad_check.cpp:21-48](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_data_copy_pad_check.cpp#L21-L48) —— `CheckPadParamters`：常量 `dataCopyPadPaddingSizeLimit = 32`，左右填充各自不得超过 `32/srcDtypeBytes` 个元素；B64 类型且开启填充时 `paddingValue` 必须为 0。

[cpudebug/src/api_check/kernel_data_copy_pad_check.cpp:50-54](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_data_copy_pad_check.cpp#L50-L54) —— `CheckAllHighLevel` 用 `ASCENDC_CHECK` 包住 `CheckPadParamters`。

**Slice 实现**则丰富得多。先看总的 `CheckAllHighLevel`，注意它把 6 类检查按顺序串起来，任何一步失败都会经 `ASCENDC_CHECK` 短路返回：

[cpudebug/src/api_check/kernel_data_copy_slice_check.cpp:159-174](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_data_copy_slice_check.cpp#L159-L174) —— Slice 的 `CheckAllHighLevel`：scope 校验里 `supportPos = "VECIN/VECOUT/VECCALC"`，说明 slice 的 UB 侧用逻辑位置 `VECIN/VECOUT/VECCALC` 表达。

逐维检查 `SliceInfo` 的 6 条规则（dst 与 src 的 burstLen 相等、非首维 burstLen 必为 1、start < end、end < shape 等）：

[cpudebug/src/api_check/kernel_data_copy_slice_check.cpp:21-73](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_data_copy_slice_check.cpp#L21-L73) —— `CheckSliceInfoParamters`，任一条不满足即 `CHECK_LOG_ERROR` + `return false`。

指令数计算与上限校验：

[cpudebug/src/api_check/kernel_data_copy_slice_check.cpp:75-91](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_data_copy_slice_check.cpp#L75-L91) —— `DataCopyGetTotalInstrsNum` 按上式逐维累乘。

[cpudebug/src/api_check/kernel_data_copy_slice_check.cpp:138-157](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_data_copy_slice_check.cpp#L138-L157) —— `CheckDataCopyInstrsNum`：要求 `srcIntrsNum == dstIntrsNum`，且都不超过 `MAX_SLICE_SIZE`（值为 `6 * 256 = 1536`，定义见 [kernel_utils_constants.h:32](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/utils/include/utils/kernel_utils_constants.h#L32)）。

block count 整除与 stride 对齐校验：

[cpudebug/src/api_check/kernel_data_copy_slice_check.cpp:93-136](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_data_copy_slice_check.cpp#L93-L136) —— `CheckDataCopyIntrsParamters`：`oneSliceLen = burstLen * UB_BLOCK_SIZE / srcDtypeBytes + stride`，要求 `totalLen % oneSliceLen == 0`；并要求 stride 折算回字节数后是 `UB_BLOCK_SIZE`（32）的整数倍。`UB_BLOCK_SIZE = 32` 见各架构 ini 头文件，例如 [ascend910B1_ini.h:37](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/inc/ascend910B1_ini.h#L37)。

两个入口函数 `CheckFuncDataCopyPadImpl` / `CheckFuncDataCopySliceImpl` 与 4.1 的 DataCopy 入口同构：

[cpudebug/src/api_check/kernel_check_util.cpp:71-89](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_check_util.cpp#L71-L89) —— 分别构造 `TikcppDataCopyPadCheck` / `TikcppDataCopySliceCheck` 并调用各自的 `CheckAllHighLevel()`。

#### 4.2.4 代码实践（读测试理解行为）

**实践目标**：用单元测试里的「反例」验证你对 slice 校验规则的理解。

**操作步骤**：

1. 打开 [tests/ut/testcase/tikcpp_api_check/test_data_copy_check.cpp](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/ut/testcase/tikcpp_api_check/test_data_copy_check.cpp)。
2. 看 `TEST_DATA_COPY_SLICE_API_CHECK` 的参数化用例（第 43–162 行）。其中第一条 `expect=true`（合法），其余多为 `expect=false`（非法）。
3. 以第 55–63 行的用例为例：

   ```cpp
   {{16, 71, 7, 3, 88}, {0, 2, 1, 1, 3}},  // srcSliceInfo[0].burstLen=7, dst...burstLen=3
   ...
   false  // expect
   ```

   `srcSliceInfo[0]` 的 `burstLen=7` 与 `dstSliceInfo[0]` 的 `burstLen=3` 不等。

4. 对照 [kernel_data_copy_slice_check.cpp:24-30](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_data_copy_slice_check.cpp#L24-L30) 的第一条规则（`dstSliceInfo[i].burstLen != srcSliceInfo[i].burstLen` 即报错），确认这条用例正是在此处失败。

**需要观察的现象**：每个 `expect=false` 用例都能在 `CheckSliceInfoParamters` / `CheckDataCopyIntrsParamters` 里找到对应的一条失败规则。

**预期结果**：测试断言 `EXPECT_EQ(flag, param.expect)` 全部成立；你能为每个反例指出「失败在第几条规则」。

> 说明：本实践为「读测试断言理解行为」，不依赖 NPU 硬件；若想本地运行 UT，可参考 [u9-l3 单元测试体系](u9-l3-unit-testing.md)（待该讲义发布）。

#### 4.2.5 小练习与答案

**练习 1**：`DataCopyPad` 在 `int8_t`（`srcDtypeBytes=1`）下，`leftPadding` 最大允许多少？在 `float`（`srcDtypeBytes=4`）下呢？
**答案**：`paddingLimit = 32 / srcDtypeBytes`。int8 下为 32，float 下为 8。即「填充总字节 ≤ 32」。

**练习 2**：为什么 B64（`srcDtypeBytes=8`）类型开启填充时，`paddingValue` 必须为 0？
**答案**：见 [CheckPadParamters:40-46](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_data_copy_pad_check.cpp#L40-L46)，这是硬件对 64 位类型填充值的约束，非零值不被支持，校验器据此直接报错。

**练习 3**：Slice 校验里 `MAX_SLICE_SIZE` 的值是多少？它的作用是什么？
**答案**：`6 * 256 = 1536`（[kernel_utils_constants.h:32](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/utils/include/utils/kernel_utils_constants.h#L32)）。它限制一次 `DataCopySlice` 展开后的总搬运指令数上限，超过即要求重新设置 `sliceInfo` 参数。

### 4.3 对齐与范围约束的统一表达

#### 4.3.1 概念说明

把 4.1、4.2 三个校验器放在一起看，会发现它们本质上都在表达两类硬件约束：

1. **对齐约束**：地址相对某硬件基址的偏移，必须是规定粒度的整数倍。
2. **范围约束**：访问量不得超过该 scope 的容量。

差异只在于**粒度**与**容量来源**：

| 校验器 | 对齐粒度来源 | 范围/上限检查 |
| --- | --- | --- |
| `DataCopy` | `alignSizeMap`（GM=1, L1/UB=32, L0A/B/C=512, BIAS=64） | high-level 层开源代码不显式做 |
| `DataCopyPad` | 复用基类（本类只查 padding） | padding 元素上限 `32/dtypeBytes` |
| `DataCopySlice` | `alignBytes_ = 32`（`dataCopySliceAlignSize`） | UB 容量 `bufferSizeMap`、指令数 `MAX_SLICE_SIZE` |

要理解这些约束，必须先理清「逻辑位置 → 物理位置」的映射，因为对齐粒度是按**物理位置 `Hardware`** 查表的，而源码里写的是**逻辑位置 `TPosition`**。

#### 4.3.2 核心流程

```text
源码里的 TPosition (逻辑位置，如 VECIN/VECCALC)
   │  GetPhyType()  或  ConstDefiner::positionHardMap
   ▼
Hardware (物理位置，如 UB)
   │  alignSizeMap[Hardware]  /  bufferSizeMap[pos]
   ▼
对齐粒度(字节)  /  容量上限(字节)
```

对齐的数学条件是：

\[
(\text{addr} - \text{base}) \bmod \text{alignBytes} = 0
\]

- `DataCopy` 走基类 `CheckTensorAddrAlign(addr, pos, alignBytes, ...)`，基址与偏移由 `phyPos` 决定（见 u4-l1）。
- `DataCopySlice` 自己算基址：`ubBaseAddr = GetHardwareBaseAddr(Hardware::UB)`，再 `dstAbsPos = dstAddr - ubBaseAddr`，要求 `dstAbsPos % 32 == 0`。

#### 4.3.3 源码精读

`TPosition`（逻辑位置）与 `Hardware`（物理位置）两个枚举：

[cpudebug/utils/kernel_event.h:27-47](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/utils/kernel_event.h#L27-L47) —— `TPosition`：`GM/A1/A2/B1/B2/C1/C2/CO1/CO2/VECIN/VECOUT/VECCALC/...`。
[cpudebug/utils/kernel_event.h:50](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/utils/kernel_event.h#L50) —— `Hardware : uint8_t { GM, UB, L1, L0A, L0B, L0C, BIAS, FIXBUF, MAX }`。

两者之间的映射函数（按 `__NPU_ARCH__` 有不同分支）：

[cpudebug/utils/kernel_event.h:240-267](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/utils/kernel_event.h#L240-L267) —— `GetPhyType(TPosition)`：把逻辑位置映射成物理位置，例如 `VECIN/VECOUT/VECCALC → UB`、`A1/B1 → L1`、`A2 → L0A` 等。

CPU 仿真下，每块硬件存储有一段模拟出来的基址，`ConstDefiner::GetHardwareBaseAddr` 返回它：

[cpudebug/utils/include/utils/kernel_utils_mode_cpu.h:92-98](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/utils/include/utils/kernel_utils_mode_cpu.h#L92-L98) —— `GetHardwareBaseAddr(Hardware)` 从 `hardwareCpuBufferMap` 取基址。

于是 Slice 的对齐检查把「绝对地址」换算成「UB 内偏移」再判 32 字节对齐：

```cpp
uint64_t ubBaseAddr = reinterpret_cast<uint64_t>(ConstDefiner::Instance().GetHardwareBaseAddr(Hardware::UB));
if (param_.isGM2UB) {
    uint64_t dstAbsPos = param_.dstAddr - ubBaseAddr;
    ASCENDC_CHECK_AND_LOG(((dstAbsPos % alignBytes_) == 0), { CHECK_LOG_ERROR(...); });
} else { ... srcAbsPos = param_.srcAddr - ubBaseAddr; ... }
```

[cpudebug/src/api_check/kernel_data_copy_slice_check.cpp:176-196](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_data_copy_slice_check.cpp#L176-L196) —— Slice 的 `CheckAddrAlign`：`alignBytes_` 默认为类常量 `dataCopySliceAlignSize = 32`（见 [kernel_data_copy_slice_check.h:37-38](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/inc/kernel_data_copy_slice_check.h#L37-L38)）。

容量上限的来源：基类 `CheckBufferSizeOverFlow(localSize, bufferSize, errMsg)`，其中 `bufferSize` 来自 `GlobalParams::Instance().bufferSizeMap.at(pos)`（按物理位置查各 scope 的容量，UB 容量如 `ascend910B1_ini.h` 中的 `UB_SIZE = 196608`，见 [ascend910B1_ini.h:31](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/inc/ascend910B1_ini.h#L31)）。声明在：

[cpudebug/src/api_check/inc/kernel_base_check.h:101](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/inc/kernel_base_check.h#L101) —— `CheckBufferSizeOverFlow` 声明（实现与用法已在 u4-l1 讲过）。

最后，两个宏把所有检查的「失败语义」统一成「记日志 + `return false`」：

[cpudebug/src/api_check/kernel_check_params.h:39-44](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_check_params.h#L39-L44) —— `ASCENDC_CHECK(x)`：`!(x)` 即 `return false`。
[cpudebug/src/api_check/kernel_check_params.h:46-52](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_check_params.h#L46-L52) —— `ASCENDC_CHECK_AND_LOG(cond, behavior)`：`!(cond)` 时先执行 `behavior`（通常是一句 `CHECK_LOG_ERROR`）再 `return false`。

#### 4.3.4 代码实践（构造一个会报错的用例）

**实践目标**：亲手触发一次搬运类校验失败，观察错误是如何被报告的。

最稳的路径是构造一个 `DataCopyPad` 反例——因为它的合法条件（`leftPadding ≤ 32/srcDtypeBytes`、B64 下 `paddingValue==0`）是纯算术、确定性的，且实现完全开源。

**操作步骤**（源码阅读 + 推理；运行待本地验证）：

1. 假设你在算子里写了一条 `float` 类型的 `DataCopyPad`，并把 `leftPadding` 设为 `20`。
2. 按 [CheckPadParamters:24-31](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_data_copy_pad_check.cpp#L24-L31) 推算：`paddingLimit = 32 / 4 = 8`，而 `20 > 8`。
3. 因此 `CheckPadParamters` 会 `CHECK_LOG_ERROR("Failed to check leftPadding value in DataCopyPad, its valid range is 0 ~ 8, current value is 20.")` 并 `return false`。
4. 失败沿 `ASCENDC_CHECK` 一路返回到 npuchk 钩子，最终在 CPU 域运行算子时写入 `*_npuchk.log`（参见 [u5-l1 npu check 错误体系与检查机制](u5-l1-npuchk-error-system.md)）。

**需要观察的现象**：日志里应出现 `Failed to check leftPadding value in DataCopyPad` 字样，并给出合法范围与当前值。

**预期结果**：`leftPadding=20`（float）→ 报错；改回 `≤ 8` → 通过。

> 关于「构造搬运越界用例观察 ASSERT」的诚实说明：对于**普通 `DataCopy`**（非 pad/slice），开源可见的 high-level 校验**只做对齐**，并不显式比较「搬运长度 vs Tensor 容量」。因此若你想观察「越界」类报错，可靠的选择是用 **`DataCopySlice`**（其 `CheckBufferSizeOverFlow` 与 `CheckDataCopyInstrsNum` 会显式报错），或依赖 cpudebug 更底层的硬件仿真保护（u3-l1 讲过的 `mmap` + `mprotect` 头尾页会在真正越界写时触发 `SIGSEGV`）。普通 `DataCopy` 直接越界时的具体表现，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `DataCopySlice::CheckAddrAlign` 要用 `dstAddr - ubBaseAddr`，而不是直接判 `dstAddr % 32`？
**答案**：因为 `dstAddr` 是进程地址空间里的绝对地址（CPU 仿真下 UB 只是一段 `mmap` 出来的内存），它的低位是否为 0 取决于基址；只有换算成「UB 内偏移」`dstAbsPos` 再判 `% 32`，才真正反映硬件意义上的 32 字节对齐。

**练习 2**：`alignSizeMap` 与 `bufferSizeMap` 都是「按硬件位置查表」，它们查的东西有什么本质区别？
**答案**：`alignSizeMap` 查的是**对齐粒度**（地址必须是多少字节的倍数），用于 `CheckTensorAddrAlign`；`bufferSizeMap` 查的是**容量上限**（这块存储一共多大），用于 `CheckBufferSizeOverFlow`。前者管「地址合法」，后者管「不超容量」。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个小任务：

**任务**：为 `add.asc` 设计一个「带填充的搬运」场景，并预测它的校验结果。

1. 假设你想把 `xGm` 的数据搬到 UB 时，左侧补 4 个 0、右侧补 4 个 0（`float` 类型）。请写出对应的 `DataCopyPad` 调用骨架（标注为「示例代码」，不是项目原有代码）：

   ```cpp
   // 示例代码：仅用于说明校验参数，非 add.asc 原有逻辑
   AscendC::DataCopyPad(xLocal, xGm[progress * TILE_LENGTH], TILE_LENGTH,
                        false, /*leftPadding*/ 4, /*rightPadding*/ 4, /*paddingValue*/ 0);
   ```

2. 用本讲的结论推断它会经过哪些校验：
   - 走 `CheckFuncDataCopyPadImpl` → `TikcppDataCopyPadCheck::CheckAllHighLevel` → `CheckPadParamters`。
   - `paddingLimit = 32 / 4 = 8`，`leftPadding=4`、`rightPadding=4` 均未超限，`paddingValue=0`，故应通过。
3. 把 `leftPadding` 改成 `10`，重新推断：`10 > 8`，应在 `CheckPadParamters` 报错，日志提示合法范围 `0 ~ 8`。
4. 进一步思考：如果改成 `int8_t`（`srcDtypeBytes=1`），`leftPadding=10` 还会报错吗？（答案：不会，因为 `paddingLimit=32`。）

**交付物**：一张表，列出「dtype / leftPadding / rightPadding / paddingValue → 预期校验结果（通过/失败及原因）」，并指出失败时命中的是 [CheckPadParamters](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_data_copy_pad_check.cpp#L21-L48) 的哪一行判断。

> 若条件允许，可在 CPU 域实际运行验证你的推断；若暂无环境，本任务作为「源码阅读 + 推理」完成即可，相关运行步骤参见 [u1-l4](u1-l4-build-and-first-sample.md) 与 [u2-l1](u2-l1-cpudebug-workflow.md)。

## 6. 本讲小结

- 搬运类 API 的校验由「入口函数 `CheckFuncDataCopy*Impl` → 校验器子类 `Tikcpp*Check` → 基类通用检查」三层构成，与 u4-l1 的总体框架完全一致。
- **普通 `DataCopy`** 的 high-level 开源校验**只做地址对齐**，对齐粒度由 `alignSizeMap` 按物理位置决定（GM 不查，L1/UB=32B，L0A/B/C=512B，BIAS=64B）。
- **`DataCopyPad`** 在此基础上检查填充量：左右填充元素数 ≤ `32/srcDtypeBytes`，B64 类型开启填充时 `paddingValue` 必须为 0。
- **`DataCopySlice`** 检查最重：scope 必须是 UB、逐维 `SliceInfo` 一致性、搬运指令数 ≤ `MAX_SLICE_SIZE(1536)`、stride 折算字节是 32 的倍数、UB 容量不溢出、32 字节对齐。
- 三类校验本质都是在表达「对齐 + 范围」两类硬件约束，差异只在粒度来源（`alignSizeMap` / `dataCopySliceAlignSize` / `UB_BLOCK_SIZE`）与容量来源（`bufferSizeMap`）。
- 所有失败都经 `ASCENDC_CHECK` / `ASCENDC_CHECK_AND_LOG` 短路返回，最终在 CPU 域运行算子时记入 `*_npuchk.log`，供 [u5](u5-l1-npuchk-error-system.md) 解析。

## 7. 下一步学习建议

- 接着学 [u4-l3 向量计算类校验](u4-l3-vector-check.md)：看 `repeat/mask/stride` 参数如何决定一次向量计算所需的最小 Tensor 容量，那里的 `CheckTensorSizeOverflow` 与本讲的「范围约束」是同一套数学基础。
- 然后进入 [u5-l1 npu check 错误体系与检查机制](u5-l1-npuchk-error-system.md)：本讲反复提到的「失败写入 `*_npuchk.log`」，在 u5 会讲清楚它对应的 `ErrorBuffer`/`ErrorWrite` 等错误类型与典型场景。
- 若你对「校验器是怎么被扩展的」感兴趣，可直接跳到 [u10-l1 扩展 API 校验器与二次开发](u10-l1-extend-api-check.md)，本讲的 `kernel_data_copy_check.cpp` 正是那里的范例之一。
