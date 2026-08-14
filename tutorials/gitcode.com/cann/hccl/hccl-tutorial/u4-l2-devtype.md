# 设备类型与能力识别

## 1. 本讲目标

学完本讲，你应当能够：

- 列出 `HcclDevType` 枚举的全部取值，并说明每个值代表哪一类昇腾芯片（910 / 910B / 950 / 960 等）。
- 说清一个形如 `"Ascend910B4"` 的 SOC 名称字符串，是如何一步步被翻译成 `HcclDevType::DEV_TYPE_910B` 的。
- 解释 `HcclGetDeviceType` 在运行期如何通过 `aclrtGetSocName` 探测芯片、如何用线程局部（thread_local）缓存避免重复探测。
- 理解「设备类型」这一字段为何会在执行链路的下游成为诸多高级特性（AICPU Task Cache、CCU 版本、OutPlace 新流程）的「能力开关」。

本讲是 Unit 4「公共基础」的第二篇，承接 [u4-l1 算法类型 AlgType](u4-l1-algtype.md)：u4-l1 讲的是「算法（alg）」维度的枚举，本讲讲的是「设备（device）」维度的枚举。两者最终都会挂到贯穿执行链路的中央容器 `OpParam` 上（见 [u2-l3 OpParam](u2-l3-opparam-and-check.md)），分别从不同角度影响算子如何执行。

## 2. 前置知识

在进入源码前，先用三句话建立直觉。

**什么是 SOC 名称？** 每一颗昇腾 NPU 在出厂时都有一个「System on Chip 版本号」，例如 `Ascend910`、`Ascend910B4`、`Ascend950PR_958b`。CANN 运行时库提供了一个函数 `aclrtGetSocName()`，程序在设备上运行时调用它，就能拿到当前这张卡的 SOC 名称字符串。

**为什么 HCCL 需要自己再抽象一层 `HcclDevType`？** 因为同一代芯片会有很多 SOC 子型号（910A / 910B / 910ProA ……），但对 HCCL 来说，它们的通信能力是一样的——只要知道「这是 910 家族」就够了。所以 HCCL 把几十个细粒度的 SOC 字符串归并成少数几个粗粒度的「设备类型枚举」，下游代码只需对枚举值做判断，不必关心具体子型号。

**为什么这一层很关键？** HCCL 的很多特性是「按芯片代际开放」的：例如 AICPU Task Cache、CCU 硬化通信单元、OutPlace 新执行流程，并不是所有芯片都支持。设备类型就是这些特性在运行期的「准入凭证」。后面 4.x 节会逐一展开。

> 小贴士：本讲只讲「设备类型是什么、怎么得到」。至于「得到之后，CCU / Task Cache 具体怎么用」，属于后续 [Unit 5 通信引擎模板](u5-l1-aicpu-template-kernel.md) 的内容，本讲只在「综合实践」里点到为止地串一下。

## 3. 本讲源码地图

本讲涉及的源码很少而精，集中在 `src/common/` 下的两个文件：

| 文件 | 作用 | 本讲用到 |
| --- | --- | --- |
| [src/common/dev_type.h](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/dev_type.h) | 声明 `HcclDevType` 枚举、`HCCL_SOC_VER_CONVERT` 映射表、`HcclGetDeviceType` C 接口 | 枚举定义、映射表、weak alias 宏 |
| [src/common/dev_type.cc](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/dev_type.cc) | 实现 `HcclGetDeviceType`：探测 SOC 名称、翻译成枚举、线程局部缓存 | 探测、子串匹配、缓存逻辑 |

此外，本讲在「综合实践」与「交叉引用」中会用到三个**下游消费方**（用来证明设备类型确实是个能力开关），它们不在本讲的精读范围，但你会看到 HCCL 如何使用设备类型：

- `src/common/hccl_common.h` —— `IsOutPlaceDevice` 用设备类型判断是否走新流程（承接 [u2-l2](u2-l2-op-entry-dispatch.md) 的兼容闸门）。
- `src/ops/op_common/template/aicpu/task_cache/aicpu_task_cache_policy.cc` —— AICPU Task Cache 仅对 950 开放。
- `src/ops/op_common/template/ccu/ccu_kernel_utils.h` —— CCU 按 950 与否选择 V1 / V2 接口。

## 4. 核心概念与源码讲解

### 4.1 HcclDevType 设备类型枚举

#### 4.1.1 概念说明

`HcclDevType` 是 HCCL 内部的「对内芯片类型」枚举。它把外部世界形形色色的 SOC 子型号，归并成一组稳定的、与通信能力对齐的代际标签。下游所有「这个特性是否可用」的判断，都是对这个枚举值做比较，而不是去解析字符串。

为什么要做这种归并？因为同一个枚举值对应的通信能力是相同的，而具体子型号会随着产品迭代不断增多。如果下游代码到处写 `if (socName == "Ascend910B4")`，每出一个新型号就要改一遍；改成 `if (devType == DEV_TYPE_910B)` 之后，新型号只要在映射表里补一行即可，下游代码完全不动。

#### 4.1.2 核心流程

枚举本身是一张「编号 → 代际」的对照表。值得注意的设计点有三：

1. **连续编号**：从 0 开始连续递增，便于用作数组下标或日志里的整数打印。
2. **末尾哨兵 `DEV_TYPE_COUNT`**：它的值等于「有效枚举项个数」，既当边界，又当「未知 / 未初始化」的占位值（后面缓存逻辑会用到）。
3. **`enum class`**：强类型枚举，不会和其它整数隐式转换，避免把设备类型和算法类型搞混。

#### 4.1.3 源码精读

枚举定义在头文件里，注释里标了「对内芯片类型」：

[src/common/dev_type.h:55-67](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/dev_type.h#L55-L67) —— 声明 `HcclDevType` 枚举，9 个成员 + 1 个 `COUNT` 哨兵。

关键几行的含义：

- `DEV_TYPE_910 = 0`：昇腾 910 系列（训练初代）。
- `DEV_TYPE_310P3 = 1` / `DEV_TYPE_310P1 = 3`：昇腾 310 系列（PG / AG 推理卡）。
- `DEV_TYPE_910B = 2`：昇腾 910B 系列（910 二代训练卡，如 910B1/B2/B3/B4）。
- `DEV_TYPE_910_93 = 4`：910 的 9391/9381 等型号（注释标注部分为「预留类型，当前暂不支持」）。
- `DEV_TYPE_NOSOC = 5`：「无 SOC」，用于 host 侧编译或无设备上下文的场景。
- `DEV_TYPE_950 = 6`：**昇腾 950（A5）**，本讲的重要主角，许多新特性的准入线。
- `DEV_TYPE_MC62 = 7`：MC62 类型（枚举里保留，本讲映射表中暂未出现对应 SOC 字符串）。
- `DEV_TYPE_960 = 8`：昇腾 960 系列。
- `DEV_TYPE_COUNT = 9`：哨兵，等于有效项个数，也兼作「未初始化」标记。

> 注意区分：`DEV_TYPE_950`（枚举值 = 6）与 `"Ascend950"`（SOC 名称字符串）是两个层面的东西，本讲 4.2、4.3 讲的就是怎么从后者得到前者。

#### 4.1.4 代码实践

**实践目标**：亲手把枚举值与代际含义对上号，建立「整数 ↔ 芯片代际」的直觉。

**操作步骤**：

1. 打开 [src/common/dev_type.h:56-67](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/dev_type.h#L56-L67)。
2. 自行抄录下表（左侧已给出，右侧留给你填）：

   | 枚举值（整数） | 枚举名 | 你填：代表哪一代芯片 |
   | --- | --- | --- |
   | 0 | DEV_TYPE_910 | |
   | 1 | DEV_TYPE_310P3 | |
   | 2 | DEV_TYPE_910B | |
   | 6 | DEV_TYPE_950 | |
   | 8 | DEV_TYPE_960 | |
   | 9 | DEV_TYPE_COUNT | |

3. 思考：为什么 `DEV_TYPE_MC62 = 7` 在本讲后面读到的映射表里找不到对应 SOC 字符串？这说明了枚举与映射表之间是什么关系（提示：枚举是「全集」，映射表是「当前实际能探测到的子集」）。

**需要观察的现象**：枚举是「能力代际」全集，而 4.2 的映射表只覆盖「当前 CANN 能识别的 SOC 名称」，二者并不要求一一对应。

**预期结果**：你能口述「950 = A5 代、910B = 二代训练卡」，并理解 `DEV_TYPE_COUNT` 既是边界也是「未知」占位。

#### 4.1.5 小练习与答案

**练习 1**：为什么不把 `HcclDevType` 写成普通 `enum` 而要用 `enum class`？

> **参考答案**：`enum class` 是强类型枚举，不会与整数或其它枚举隐式转换。HCCL 里同时有「设备类型」「算法类型」等多套枚举，强类型能防止把 `DEV_TYPE_950` 误当成 `AlgTypeLevel0` 之类的值传错，编译期就拦住。

**练习 2**：`DEV_TYPE_COUNT` 这个成员承担了哪两个职责？

> **参考答案**：① 作为「有效枚举项个数」的边界值（值为 9）；② 作为「尚未探测 / 未知」的占位初值——线程局部缓存 `g_deviceType` 就是初始化成它，用来表示「还没测过」。

---

### 4.2 HCCL_SOC_VER_CONVERT：SOC 名称到设备类型的映射表

#### 4.2.1 概念说明

`HCCL_SOC_VER_CONVERT` 是一张「字符串 → 枚举」的查表字典。它的职责很单一：把 `aclrtGetSocName()` 返回的精确子型号字符串，翻译成 4.1 节里的粗粒度设备类型。

这张表体现了 HCCL 对「同代多型号」的处理哲学：**归并**。你会看到十几个不同的 910 子型号，全部映射到同一个 `DEV_TYPE_910`；多个 910B 子型号，全部映射到 `DEV_TYPE_910B`。新增芯片型号时，维护工作只集中在这张表里。

#### 4.2.2 核心流程

映射的查找逻辑可以概括为一条优先级链（先子串、后查表）：

```text
SOC 名称 socName
   │
   ├─ 包含子串 "Ascend950"      → DEV_TYPE_950        （子串前缀匹配，4.3 节实现）
   ├─ 包含子串 "Ascend960"/"Ascend910_96" → DEV_TYPE_960  （子串前缀匹配）
   └─ 在 HCCL_SOC_VER_CONVERT 中精确查找
            ├─ 命中 → 对应枚举值
            └─ 未命中 → 报错 HCCL_E_RUNTIME
```

为什么 950 / 960 要走子串匹配，而其它走精确查表？因为这两代的子型号命名规则多样（如 `Ascend950PR_958b` 等），用「家族前缀子串」一次兜住，比在表里逐个列举更稳健。具体实现见 4.3.3。

#### 4.2.3 源码精读

映射表定义在头文件中，是一个 `std::unordered_map<std::string, HcclDevType>`：

[src/common/dev_type.h:69-97](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/dev_type.h#L69-L97) —— `HCCL_SOC_VER_CONVERT` 字典，把每个 SOC 子型号字符串映射到对应的 `HcclDevType`。

读这张表时请注意几类典型条目：

- **归并到同一代**：`Ascend910` / `Ascend910A` / `Ascend910B` / `Ascend910ProA` / `Ascend910ProB` / `Ascend910PremiumA` 这 6 个字符串，全部映射到 `DEV_TYPE_910`（[第 76-81 行](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/dev_type.h#L76-L81)）。这就是「归并」。
- **910B 家族**：`Ascend910B1` / `B2` / `B2C` / `B3` / `B4` / `B4-1` 全部映射到 `DEV_TYPE_910B`（[第 82-87 行](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/dev_type.h#L82-L87)）。
- **带注释的临时映射**：`Ascend310B1 → DEV_TYPE_310P3` 旁有一段中文注释「临时映射……torch_npu 未与 hccl 的 so 解耦；计划 20250630 完成解耦，解耦后删除」（[第 74-75 行](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/dev_type.h#L74-L75)）。这说明映射表里也存在「过渡性」条目，读源码时要留意这类注释。
- **预留类型**：`Ascend910_9392` / `9382` 旁注释「预留类型，当前版本暂不支持，待跟随后续版本节奏交付」（[第 90-92 行](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/dev_type.h#L90-L92)）——即便预留，也先映射到 `DEV_TYPE_910_93` 占位。
- **host 占位**：`"nosoc" → DEV_TYPE_NOSOC`（[第 97 行](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/dev_type.h#L97)）。

值得特别留意：**950 和 960 的映射是「残缺」的**——表里只有一个 `Ascend950PR_958b → DEV_TYPE_950`，没有任何 960 条目。这正是因为它们的真正识别路径在 4.3 的子串分支里，表里的少数条目只是「保底」。

#### 4.2.4 代码实践

**实践目标**：通过读表，体会「归并」如何把型号爆炸收敛成代际枚举。

**操作步骤**：

1. 打开 [src/common/dev_type.h:69-97](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/dev_type.h#L69-L97)。
2. 统计：映射到 `DEV_TYPE_910` 的 SOC 字符串有几个？映射到 `DEV_TYPE_910B` 的有几个？映射到 `DEV_TYPE_310P3` 的有几个？
3. 假设华为下个月发布 `Ascend910B5`，请回答：要让 HCCL 正确识别它，需要改哪一处？需要改下游所有判断设备类型的算子代码吗？

**需要观察的现象**：同代多型号被映射到同一个枚举值。

**预期结果**：① `DEV_TYPE_910` 有 6 个、`DEV_TYPE_910B` 有 6 个、`DEV_TYPE_310P3` 有 5 个（含临时映射的 `Ascend310B1`）。② 只需在 `HCCL_SOC_VER_CONVERT` 里加一行 `{"Ascend910B5", HcclDevType::DEV_TYPE_910B}`，**无需改动任何下游算子代码**——这正是归并设计的收益。

#### 4.2.5 小练习与答案

**练习 1**：为什么 950 / 960 不像 910 那样把每个子型号都列进映射表？

> **参考答案**：因为 950 / 960 的子型号命名较多样（如 `Ascend950PR_958b`），用「家族前缀子串匹配」一次覆盖所有变体更稳健；而 910 的子型号相对固定且数量有限，精确列在表里更清晰。两种策略各有适用场景，4.3 节的代码把它们组合成一条优先级链。

**练习 2**：映射表里 `Ascend310B1 → DEV_TYPE_310P3` 带了一段「临时映射」注释。如果你是这个模块的维护者，看到这段注释后应该做什么？

> **参考答案**：注释写明了计划于 20250630 完成 torch_npu 与 hccl 的 so 解耦后删除该临时条目。维护者应跟踪该解耦进度，在解耦完成后移除这行临时映射，避免遗留过期逻辑。

---

### 4.3 HcclGetDeviceType：运行期检测与线程局部缓存

#### 4.3.1 概念说明

`HcclGetDeviceType` 是把 4.1、4.2 串起来的运行期入口：调用一次，返回当前卡的 `HcclDevType`。它的实现有三个看点：

1. **探测**：调用 CANN 运行时 `aclrtGetSocName()` 拿到 SOC 字符串。
2. **翻译**：按 4.2 的优先级链（子串优先 → 查表）把字符串变成枚举。
3. **缓存**：用 `thread_local` 变量记住结果，同一线程后续调用直接返回，避免每次算子下发都去问运行时。

#### 4.3.2 核心流程

`__HcclGetDeviceType`（`HcclGetDeviceType` 的真实实现，二者关系见 4.3.3 的 weak alias）的执行流程如下：

```text
__HcclGetDeviceType(devType&)
  │
  ├─ 1. 命中缓存？  g_deviceType != DEV_TYPE_COUNT  → 直接返回 g_deviceType
  │                                                   （同线程首次之后的快速路径）
  │
  ├─ 2. 探测 SOC：HcclGetSocVer(socName)
  │      └─ 非 AICPU_COMPILE 时调用 aclrtGetSocName()；空指针则报错
  │
  ├─ 3. 翻译（优先级链）：
  │      ├─ socName 含 "Ascend950"               → DEV_TYPE_950
  │      ├─ socName 含 "Ascend960"/"Ascend910_96" → DEV_TYPE_960
  │      └─ HCCL_SOC_VER_CONVERT.find(socName)
  │            ├─ 命中 → 对应枚举
  │            └─ 未命中 → HCCL_ERROR + 返回 HCCL_E_RUNTIME
  │
  └─ 4. 写缓存：g_deviceType = devType，返回
```

缓存的关键设计：`g_deviceType` 是 **`thread_local`** 而非进程级全局。这意味着每个线程各缓存一份、各探测一次。多线程训练程序里，绑定到不同 NPU 的线程各自独立完成探测，互不污染。

> 一个微妙的点：`thread_local` 意味着「同一逻辑设备在不同线程会被重复探测」。这对正确性无害（同一张卡的 SOC 名称固定），代价仅是每个线程多一次 `aclrtGetSocName` 调用，相比一次算子下发的开销可忽略。

#### 4.3.3 源码精读

**线程局部缓存变量**，初值就是哨兵 `DEV_TYPE_COUNT`，表示「未探测」：

[src/common/dev_type.cc:16-17](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/dev_type.cc#L16-L17) —— 匿名命名空间里的 `thread_local HcclDevType g_deviceType = DEV_TYPE_COUNT`。

**SOC 名称探测**，封装了 `aclrtGetSocName()`，并用 `AICPU_COMPILE` 宏区分编译目标：

[src/common/dev_type.cc:19-31](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/dev_type.cc#L19-L31) —— `HcclGetSocVer`：非 AICPU 侧编译时调用 `aclrtGetSocName()`，空指针则用 `CHK_PRT_RET` 打日志并返回 `HCCL_E_RUNTIME`；AICPU 侧编译时函数体为空（device 侧无此运行时接口）。

**主实现 `__HcclGetDeviceType`**，依次完成「缓存命中检查 → 探测 → 子串翻译 → 查表翻译 → 回写缓存」：

[src/common/dev_type.cc:38-75](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/dev_type.cc#L38-L75) —— 设备类型检测主逻辑。逐段含义：

- [第 40-43 行](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/dev_type.cc#L40-L43)：缓存快速路径。`LIKELY` 提示「绝大多数调用会命中缓存」，直接把 `g_deviceType` 赋给输出参数返回。注意判等用的是 `!= DEV_TYPE_COUNT`——任何有效枚举值（0~8）都不会等于 9，所以「测过」必然跳过探测。
- [第 45-46 行](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/dev_type.cc#L45-L46)：探测 SOC 名称。
- [第 50-55 行](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/dev_type.cc#L50-L55)：950 的子串前缀匹配 `socName.find("Ascend950")`，命中即赋值并回写缓存。
- [第 57-63 行](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/dev_type.cc#L57-L63)：960 的子串匹配，兼容 `Ascend960` / `ascend960` / `Ascend910_96` 三种写法。
- [第 65-74 行](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/dev_type.cc#L65-L74)：精确查 `HCCL_SOC_VER_CONVERT` 表；未命中则 `HCCL_ERROR` 报「illegal chipver」并返回 `HCCL_E_RUNTIME`；命中则赋值并回写缓存。

**weak alias 机制**——这是 HCCL↔HCOMM 两仓解耦设计的一部分（详见 [u6-l1 dlsym 动态加载](u6-l1-dlsym-mechanism.md)）：

[src/common/dev_type.h:99-100](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/dev_type.h#L99-L100) —— 定义 `hccl_weak_alias` 宏，借助 GCC `__attribute__((weak, alias))` 让符号 `HcclGetDeviceType` 成为 `__HcclGetDeviceType` 的弱别名。

[src/common/dev_type.cc:76](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/dev_type.cc#L76) —— `hccl_weak_alias(__HcclGetDeviceType, HcclGetDeviceType)`：对外暴露的 `HcclGetDeviceType` 默认指向 HCCL 自己的 `__HcclGetDeviceType` 实现；由于是弱符号，HCOMM 仓（或测试桩 `test/.../aclrt_stub.cc`）可以用一个强符号定义覆盖它。这就是为什么 `dev_type.h` 里声明的是 `HcclGetDeviceType`，而 `.cc` 里实现的是带双下划线前缀的 `__HcclGetDeviceType`。

> 术语解释：**弱符号（weak symbol）**指可以被另一个同名「强符号」覆盖的符号。链接时若存在强符号，弱符号被忽略。这里 HCCL 提供「弱默认实现」，允许 HCOMM 提供更强的实现来接管，体现两仓解耦、可独立演进的架构约束。

#### 4.3.4 代码实践

**实践目标**：跟踪一次完整的设备类型探测，验证「首次探测、后续命中缓存」的行为，并理解缓存的作用域。

**操作步骤**（源码阅读型实践）：

1. 打开 [src/common/dev_type.cc:38-75](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/dev_type.cc#L38-L75)。
2. 假设当前线程第一次调用 `HcclGetDeviceType`，卡片是 `Ascend910B4`。请按代码顺序写下每一步的 `socName` 与 `devType` 取值：
   - 进入时 `g_deviceType` = ?
   - `HcclGetSocVer` 后 `socName` = ?
   - 子串分支能否命中？（`find("Ascend950")` / `find("Ascend960")`）
   - 查表后 `devType` = ?，`g_deviceType` 被写成什么？
3. 紧接着第二次调用 `HcclGetDeviceType`：这次会执行到第几行就返回？`aclrtGetSocName` 还会被调用吗？
4. 找到测试桩 `test/st/algorithm/utils/src/hccl_proxy/aclrt_stub.cc`，看它是否提供了一个强符号的 `HcclGetDeviceType` 来覆盖弱别名，思考这为什么能让 ST 在没有真实 NPU 的环境里固定设备类型。

**需要观察的现象**：首次走完整探测链并回写缓存；第二次命中第 40-43 行的快速路径，不再访问运行时。

**预期结果**：
- 第一次：`g_deviceType` 初值 `DEV_TYPE_COUNT`(9) → `socName="Ascend910B4"` → 两个子串分支都不命中 → 查表得到 `DEV_TYPE_910B`(2) → `g_deviceType` 写成 2。
- 第二次：`g_deviceType(2) != DEV_TYPE_COUNT(9)` 成立，直接走第 41-42 行返回，**不再调用 `aclrtGetSocName`**。
- ST 桩用强符号覆盖弱别名，从而在纯 host 环境把设备类型钉死成测试所需值。

> 若你手头有装好驱动固件的真实 NPU 环境，可进一步在任意算子入口加一行 `HcclDevType t; HcclGetDeviceType(t); HCCL_INFO("devtype=%d", (s32)t);` 观察实际探测结果；否则以上为「源码阅读型实践」，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `g_deviceType` 的 `thread_local` 去掉，改成普通 `static` 全局变量，会有什么不同？

> **参考答案**：变成进程级缓存：整个进程只有第一次调用会真正探测，之后所有线程共享同一份结果。好处是探测次数更少；潜在风险是若进程内不同线程绑定不同代际的卡（异构场景），一个线程的探测结果会污染其它线程。当前用 `thread_local` 是「每个线程各探测一次」，在多卡多线程场景更安全。

**练习 2**：`__HcclGetDeviceType` 里对 950、960 的判断为何放在查表之前？如果放到查表之后会怎样？

> **参考答案**：放在前面是为了让 950/960 的「家族前缀子串」优先命中，覆盖所有子型号变体（多数子型号并不在 `HCCL_SOC_VER_CONVERT` 表里，例如没有任何 960 条目）。若放到查表之后，那些不在表里的 950/960 子型号会先撞上「未命中 → 报错」分支，直接返回 `HCCL_E_RUNTIME`，导致合法芯片识别失败。

**练习 3**：`HcclGetDeviceType` 在 `AICPU_COMPILE` 宏打开时，`HcclGetSocVer` 的函数体被 `#ifndef` 整段去掉，`socName` 保持空串。结合 weak alias，你认为 AICPU 侧的设备类型由谁提供？

> **参考答案**：AICPU 侧编译时本文件不再自行探测（`socName` 为空会落入查表失败）。此时设备类型应由 HCOMM 仓或 AICPU 侧提供的强符号 `HcclGetDeviceType` 覆盖弱别名来给出。这正是 weak alias「默认实现可被强符号接管」的设计意图——host 侧用本文件实现，device/AICPU 侧由对应仓接管。

---

## 5. 综合实践

本讲的实践任务把三个模块串起来，并提前点亮设备类型作为「能力开关」在下游的作用——这也是后续 [Unit 5](u5-l1-aicpu-template-kernel.md) 的引子。

**任务**：列出 `HcclDevType` 的全部取值，并说明 `Ascend950`（`DEV_TYPE_950`）这一设备类型为何会影响 AICPU Task Cache、CCU 等特性的可用性。

**操作步骤**：

1. **列枚举**：从 [src/common/dev_type.h:56-67](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/dev_type.h#L56-L67) 抄出全部取值与整数编码，做成一张表。

2. **追数据流**：确认设备类型是怎么进入执行链路的。打开 [src/ops/all_reduce/all_reduce_op.cc:172-184](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/all_reduce_op.cc#L172-L184)（`FillAllReduceOpParam`）——它调用 `HcclGetDeviceType(deviceType)` 并写入 `param.deviceType`。由此可见，本讲探测出的设备类型，会挂在 `OpParam.deviceType` 上，随参数容器流向下游每一个组件。

3. **看它如何成为能力开关**：分别在下游找到三处对 `DEV_TYPE_950` 的判断，并用一句话概括每处的语义：

   - **OutPlace 新流程闸门**：[src/common/hccl_common.h:263-272](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/hccl_common.h#L263-L272) 中 `shouldGoOutPlace` 判断「只有 950 或 960 才走 OutPlace 新流程」，这正是 [u2-l2](u2-l2-op-entry-dispatch.md) 里算子入口那道 `IsOutPlaceDevice` 兼容闸门的来源。
   - **AICPU Task Cache 准入**：[src/ops/op_common/template/aicpu/task_cache/aicpu_task_cache_policy.cc:27-30](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/template/aicpu/task_cache/aicpu_task_cache_policy.cc#L27-L30) 中 `IsAicpuTaskCacheEnable` 明确写「`deviceType != DEV_TYPE_950` 则不支持」——即 Task Cache **只对 950 开放**。
   - **CCU 版本选择**：[src/ops/op_common/template/ccu/ccu_kernel_utils.h:23-33](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/template/ccu/ccu_kernel_utils.h#L23-L33) 中 `GetCcuVersion` 用 `deviceType == DEV_TYPE_950 ? CCU_V1 : CCU_V2`，即 950 走 CCU V1 接口、其它走 V2（并再查 `HcommIsSupportCcuV2()` 决定是否降级）。

4. **形成结论**：用一段话回答「950 为何影响这些特性」。要点提示：
   - 950（A5）是新一代芯片，其硬件提供了 AICPU Task Cache、CCU V1、OutPlace 新执行流程所需的能力；老一代（910/910B 等）硬件不具备或不开放这些能力。
   - HCCL 不在编译期写死，而是在**运行期**通过 `HcclGetDeviceType` 探测当前芯片，再用枚举判断决定开放哪些代码路径——这就是「设备类型 = 能力开关」的本质，也是 HCCL 一套代码兼容多代芯片的关键。

**需要观察的现象**：同一个 `param.deviceType` 字段，在下游不同子系统中分别成为 OutPlace、Task Cache、CCU 版本三条路径的准入条件。

**预期结果**：你能画出一条链 `aclrtGetSocName → __HcclGetDeviceType(缓存) → OpParam.deviceType → {IsOutPlaceDevice, IsAicpuTaskCacheEnable, GetCcuVersion}`，并说清 950 之所以「特权」是因为它是当前唯一同时具备这些新硬件能力的代际。CCU V2 / Task Cache 的具体机制留到 [Unit 5](u5-l1-aicpu-template-kernel.md) 深入。

> 本综合实践以源码阅读与链路梳理为主，无需运行；若要观察日志中的实际设备类型，需在真实 NPU 环境运行（**待本地验证**）。

## 6. 本讲小结

- `HcclDevType` 是 HCCL 的「对内芯片类型」枚举，把外部几十个 SOC 子型号归并成 9 个代际标签加一个 `COUNT` 哨兵。
- `HCCL_SOC_VER_CONVERT` 是「SOC 字符串 → 设备枚举」的查表字典，新增型号只需在此表补一行，下游算子代码无需改动。
- `HcclGetDeviceType` 在运行期经 `aclrtGetSocName()` 探测，按「子串优先（950/960）、查表兜底」翻译成枚举，并用 `thread_local` 变量按线程缓存结果。
- weak alias（`__HcclGetDeviceType` ↔ 弱符号 `HcclGetDeviceType`）让默认实现可被 HCOMM 仓或测试桩的强符号覆盖，体现两仓解耦约束。
- 探测出的设备类型挂在 `OpParam.deviceType` 上，在下游成为 OutPlace 新流程、AICPU Task Cache、CCU 版本等特性的「能力开关」，这是 HCCL 一套代码兼容多代芯片的关键。
- 读映射表时要留意「临时映射」「预留类型」这类注释，它们反映了芯片型号与配套软件协同演进的过渡状态。

## 7. 下一步学习建议

- **横向对照**：回到 [u4-l1 AlgType](u4-l1-algtype.md)，对比「设备维度枚举」与「算法维度枚举」在设计上的异同（都用 `enum class`、都有哨兵、都挂到 `OpParam`），加深对 HCCL「枚举即能力标签」风格的理解。
- **纵向追下游**：继续 [u4-l3 环境变量与算法配置](u4-l3-env-config.md)，看 `alg_env_config.cc` 如何在 950 上默认开启 AICPU Task Cache（本讲已点到 `alg_env_config.cc:837-838`），把「设备类型」与「配置系统」接起来。
- **进入引擎层**：当你想真正搞懂 Task Cache 与 CCU 的内部机制时，进入 [Unit 5](u5-l1-aicpu-template-kernel.md)：[u5-l2 AICPU Task Cache](u5-l2-aicpu-task-cache.md) 会展开 `IsAicpuTaskCacheEnable` 之后的命中与回放流程，[u5-l4 CCU 模板](u5-l4-ccu-template.md) 会展开 `GetCcuVersion` 之后的 Mission/URMA 下发。
- **理解解耦**：若对 weak alias 与 dlsym 的关系感兴趣，可先读 [u6-l1 dlsym 动态加载机制](u6-l1-dlsym-mechanism.md)，看 HCCL 如何在运行期加载 HCOMM 并允许其覆盖弱符号。
