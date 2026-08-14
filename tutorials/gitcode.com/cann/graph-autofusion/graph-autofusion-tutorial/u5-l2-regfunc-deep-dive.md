# reg_func 注册函数详解（reduce/compare）

## 1. 本讲目标

上一讲（u5-l1）我们看清了 ASCIR 算子注册的「骨架」：`REG_ASC_IR` 宏 + `.Impl()` 把一个算子绑定到 `AscIrImpl` 三元组（ATT 实现创建器、codegen 实现创建器、dtype 约束）。但那套机制只回答了「算子在哪里登记」，并没有回答「算子具体怎么算」。

本讲承接 u5-l1，钻进三元组里那个 **codegen 实现创建器** 所指向的代码，回答一个具体问题：

> 一个融合算子在运行时到底需要多大、需要几块片上临时缓冲（UB temp buffer）？这个信息从哪里来、怎么算、又给谁用？

我们以 `reduce.cpp`（归约类算子）与 `compare.cpp`（比较类算子）为样本，读透 `reg_func/` 目录下「算子专属 buffer 大小函数」的写法。学完后你应当能够：

- 说清 `reg_func` 函数在整个 ASCIR 注册体系里的位置，以及它与 `REG_ASC_IR` 的关系。
- 读懂 `CalcReduceTmpSize` / `GetCompareNormalTmpSize` 的符号化形状推导逻辑。
- 理解 `TmpBufDesc` 的两个字段（`size`、`life_time_axis_id`）如何作为 tiling 的「占位输入」被下游消费。
- 对照 reduce、compare、tanh 三种复杂度，写出一个新算子注册函数的最小要素清单。

## 2. 前置知识

在进入源码前，先用大白话把几个概念补齐。这些概念在前几讲已出现过，这里只做与本讲强相关的最小回顾。

- **片上临时缓冲（UB temp buffer / TmpBuf）**：昇腾 AI Core 的 Vector 算子在 UB（Unified Buffer）里计算。有些高阶 API（如归约、比较）在内部需要几块「垫片」缓冲来放中间结果——就像你在草稿纸上算一道大题时需要几块临时空白处。`reg_func` 函数的职责就是：**在编译期告诉系统，这块草稿要多大、要几块、活多久**。
- **动态 shape 与符号表达式 `Expression`**：融合编译器要支持动态 shape，所以 buffer 大小不能写死成数字，而是用符号表达式（`Expression`）表示，例如「`4 * s1`」（s1 是某个维度变量）。最终具体字节数在 tiling（ATT，见 u7）阶段才求解出来。这正是上一讲 u5-l1 强调的「注册只登记、求解靠下游」。
- **block 与 repeat**：硬件上数据搬运与计算的最小粒度。一个 block = 32 字节；一次 repeat 处理 256 字节；单条指令最多 255 个 repeat。这些常数会在 buffer 钳制里反复出现。
- **`AscNode`**：ASCIR 图里的算子节点（见 u4-l2，`AscNode : public Node`）。它的 `.inputs` / `.outputs` 携带 dtype、轴（axis）、重复次数（repeats）、步长（strides）、向量化轴（vectorized_axis/strides）等调度信息，`reg_func` 就是从这些字段里「读出形状」。

> 一句话定位：**`reg_func` 是 codegen 实现类里 `CalcTmpBufSize()` 方法的外置实现体，专门负责「为某个算子算出它需要的片上临时缓冲描述符列表」。** 它不是注册宏本身，而是注册绑定的 codegen 实现类所调用的「算子专属逻辑」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `autofuse/ascir/reg_func/reduce.cpp` | 归约类算子（Max/Min/Sum/Mean/Prod/ArgMax…）的临时缓冲大小函数 `CalcReduceTmpSize` |
| `autofuse/ascir/reg_func/compare.cpp` | 比较类算子（Ge/Eq/Ne/Gt/Le/Lt）的临时缓冲大小函数 `GetCompareNormalTmpSize` 及 6 个薄封装 |
| `autofuse/ascir/reg_func/defalut_reg_func.h` | 所有 `CalcXxxTmpSize` 的统一声明（注意文件名 `defalut` 为仓库原有拼写） |
| `autofuse/ascir/reg_func/default_reg_func.cpp` | 公共 helper：`GetTmpBuffer`、`CalcDefaultTmpSize`、`GetInputSize` 等 |
| `autofuse/ascir/reg_func/tanh.cpp` | 最简样例：`CalcTanhTmpSize` 直接复用默认大小 |
| `autofuse/ascir/generator/v1_ascir_codegen_impl.h` | codegen 实现类（三元组中的「codegen 实现」），其 `CalcTmpBufSize` 调用 reg_func |
| `autofuse/ascir/generator/ascir_builtin_ops_v1.cpp` | `REG_ASC_IR(Max)` / `REG_ASC_IR(Ge)` 等，把实现类登记进注册表 |
| `autofuse/inc/graph_metadef/graph/ascendc_ir/ascendc_ir_core/ascendc_ir_def.h` | `struct TmpBufDesc` 的定义 |

## 4. 核心概念与源码讲解

### 4.1 reg_func 函数结构：注册体系里的「算子专属逻辑」

#### 4.1.1 概念说明

u5-l1 讲过，一个算子的注册项是 `AscIrImpl` 三元组：

1. ATT 实现创建器（负责 tiling 建模）
2. **codegen 实现创建器**（负责代码生成）
3. dtype 约束

本讲关注第 2 项。codegen 实现是一个继承自 `AscIrCodegen` 的类，例如 `MaxAscIrCodegenImpl`、`GeAscIrCodegenImpl`。它重写了若干虚函数：`GetApiCallName()`、`GetApiName()`、`LoadApiHeaderFiles()`、`IncludeApiHeaderFiles()`、`IsNodeValid()`，以及我们今天的主角 **`CalcTmpBufSize(const AscNode &node)`**。

`CalcTmpBufSize` 的返回类型是统一的：

```cpp
std::vector<std::unique_ptr<TmpBufDesc>>
```

也就是「一组临时缓冲描述符」。每个 codegen 实现类通常把这个方法体直接转交给 `reg_func/` 目录下一个同名的自由函数。这样做的好处是：**算子的 buffer 大小逻辑可以脱离类定义、单独放在 `.cpp` 里维护，并独立做单元测试。**

#### 4.1.2 核心流程

从注册到 buffer 大小被算出，调用链是这样的：

```text
REG_ASC_IR(Max)                  // ascir_builtin_ops_v1.cpp：登记算子
  .Impl(... MaxAscIrCodegenImpl ...)   // 三元组之「codegen 实现」
        │
        ▼ （下游取实现类后调用）
MaxAscIrCodegenImpl::CalcTmpBufSize(node)   // v1_ascir_codegen_impl.h
        │  return CalcReduceTmpSize(node);
        ▼
CalcReduceTmpSize(node)           // reduce.cpp：真正的算子专属逻辑
        │
        ▼
返回 vector<TmpBufDesc>           // 一组「buffer 占位描述符」
```

每个 codegen 实现类的 `CalcTmpBufSize` 都只是「一行转发」。看 `MaxAscIrCodegenImpl`：

[autofuse/ascir/generator/v1_ascir_codegen_impl.h:626-635](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/generator/v1_ascir_codegen_impl.h#L626-L635) —— `MaxAscIrCodegenImpl::CalcTmpBufSize` 把活儿交给 `CalcReduceTmpSize`，并声明自己的 API 名是 `ReduceMax`、调用模板是 `ReduceApiCall`。

比较类同理，看 `GeAscIrCodegenImpl`：

[autofuse/ascir/generator/v1_ascir_codegen_impl.h:929-939](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/generator/v1_ascir_codegen_impl.h#L929-L939) —— `GeAscIrCodegenImpl::CalcTmpBufSize` 转发给 `CalcGeTmpSize`。

而这两条转发链的「起点」是注册宏 `REG_ASC_IR`。以 Max 与 Ge 为例：

[autofuse/ascir/generator/ascir_builtin_ops_v1.cpp:350-356](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/generator/ascir_builtin_ops_v1.cpp#L350-L356) —— `REG_ASC_IR(Max)` 用 `.Impl(...)` 绑定 `MaxAscIrCodegenImpl`，并约束 `T ∈ {DT_FLOAT16, DT_FLOAT}`。

[autofuse/ascir/generator/ascir_builtin_ops_v1.cpp:532-540](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/generator/ascir_builtin_ops_v1.cpp#L532-L540) —— `REG_ASC_IR(Ge)` 绑定 `GeAscIrCodegenImpl`，`T1 ∈ {FP16, FP32, INT32, INT64}`，输出 `T2 = DT_UINT8`。

#### 4.1.3 源码精读：统一的函数签名与 TmpBufDesc

所有 `CalcXxxTmpSize` 都在 `defalut_reg_func.h` 里集中声明，签名完全一致：

[autofuse/ascir/reg_func/defalut_reg_func.h:46](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/reg_func/defalut_reg_func.h#L46) —— `CalcReduceTmpSize` 的声明。

[autofuse/ascir/reg_func/defalut_reg_func.h:37](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/reg_func/defalut_reg_func.h#L37) —— `CalcGeTmpSize` 的声明。

返回的元素 `TmpBufDesc` 只有两个字段：

[autofuse/inc/graph_metadef/graph/ascendc_ir/ascendc_ir_core/ascendc_ir_def.h:349-352](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/inc/graph_metadef/graph/ascendc_ir/ascendc_ir_core/ascendc_ir_def.h#L349-L352) —— `TmpBufDesc`：`size`（符号表达式，字节）+ `life_time_axis_id`（生命周期标记，默认 `-1`）。注释说明 `-1` 表示 API 级生命周期、`>= 0` 表示 loop 级。

> 目录组织约定：`reg_func/` 下**一个算子一个 `.cpp`**（reduce.cpp、compare.cpp、tanh.cpp、div.cpp…），公共能力（默认大小、输入尺寸、scalar 判定）收敛在 `default_reg_func.cpp`。这种「算子逻辑分散、公共 helper 集中」的布局，正是为了让新算子能照葫芦画瓢地新增。

#### 4.1.4 代码实践：追踪一条转发链

**实践目标**：验证「`REG_ASC_IR` → codegen 实现类 → `reg_func` 自由函数」这条三段式转发链确实成立。

**操作步骤**：

1. 打开 `autofuse/ascir/generator/ascir_builtin_ops_v1.cpp`，搜索 `REG_ASC_IR(Min)`，记下它 `.Impl(...)` 里绑定的 codegen 类名（应为 `MinAscIrCodegenImpl`）。
2. 打开 `autofuse/ascir/generator/v1_ascir_codegen_impl.h`，找到 `MinAscIrCodegenImpl`，看它的 `CalcTmpBufSize` 转发给哪个自由函数（应为 `CalcReduceTmpSize`，与 Max 共用）。
3. 确认 `CalcReduceTmpSize` 的定义确实在 `autofuse/ascir/reg_func/reduce.cpp`。

**需要观察的现象**：Min 与 Max 共用同一个 `CalcReduceTmpSize`——也就是说「一个 reg_func 函数可以被多个 codegen 实现类复用」，这正是 reduce 类算子（Max/Min/Sum/Mean/Prod/Any/All/ArgMax 系列）共享 sizing 逻辑的根因。

**预期结果**：三段链路一一对应，且能解释「为什么 reduce 家族算子的 buffer 逻辑只写了一份」。

### 4.2 形状推导：用符号表达式算出 buffer 大小

#### 4.2.1 概念说明

`reg_func` 函数的核心难度不在 C++ 语法，而在**形状推导**：从一个 `AscNode` 的输入/输出属性里，反推出「这块临时缓冲该有多大」。因为支持动态 shape，所有尺寸都用 `Expression` 符号表达，配合 `sym::` 命名空间下的运算符（`Mul`、`Add`、`Align`、`Min`、`Max`）来组装公式。

reduce 与 compare 代表两类典型推导：

- **reduce（归约）**：要在某个轴上「压扁」数据，buffer 大小取决于「参与归约的元素数（R 轴）」与「保留的元素数（A 轴）」如何切分。
- **compare（比较）**：buffer 大小高度依赖输入 dtype（FP16/FP32/INT32/INT64 字节数不同）与比较模式（EQ/NE 有特殊路径）。

#### 4.2.2 核心流程

**reduce.cpp 的推导思路**（归约把一个轴消掉，需区分 R 轴与 A 轴）：

```text
读 outputs[0] 的 vectorized_strides
  │  对每个轴判断：
  │    若 输出步长=0 且 输入步长≠0  → 这是「被归约的轴」(R)，累乘进 r_in_ub
  │    否则                          → 这是「保留的轴」(A)，累乘进 a_in_ub
  ▼
按算子类型分支：
  Sum/Mean/Prod（需累加）  → 高阶API缓冲 = byte(a*r) [+256 if Prod][+32 if isAr]；再加一块 UB 间缓冲 byte(a)
  Max/Min/Any/All          → 高阶API缓冲 = byte(a*r)；再加一块 UB 间缓冲 byte(a)
  ArgMax 系列              → 上述基础上再加 index/value 临时缓冲（多块，不同生命周期）
```

其中把元素数转成字节数、做 32 字节对齐的两个 helper 非常关键：

[autofuse/ascir/reg_func/reduce.cpp:28-34](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/reg_func/reduce.cpp#L28-L34) —— `GetAlignSize` 对齐到 `kFloatAlignSize=8` 个 float（即 32 字节，一个 block）；`GetByteSize` 把元素数乘以 `sizeof(float)=4` 得到字节数。

注意 reduce.cpp 这里**硬编码按 float（4 字节）算**，这与注册侧 Max/Sum 的 dtype 约束（`T ∈ {FP16, FP32}`，且 FP16 在归约内部会升到 fp32 累加）是一致的——u3-l1 讲过的「块内统一升 fp32 累加」在这里落到了 sizing 上。

A/R 轴分离的核心循环：

[autofuse/ascir/reg_func/reduce.cpp:61-74](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/reg_func/reduce.cpp#L61-L74) —— 遍历输出的向量化步长，用 `SymbolicUtils::StaticCheckEq(..., sym::kSymbolZero)` 静态判断某轴步长是否为 0，从而把 R 轴（归约轴）与 A 轴（保留轴）的元素数分别累乘到 `r_in_ub_exp` 与 `a_in_ub_exp`。

随后按是否需要累加分两支产出 `TmpBufDesc`：

[autofuse/ascir/reg_func/reduce.cpp:76-95](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/reg_func/reduce.cpp#L76-L95) —— Sum/Mean/Prod 支：高阶 API 缓冲 `byte(a*r)`（Prod 额外 `+256`、AR 布局 `+32`），生命周期 `-1`；再加一块 UB 间缓冲 `byte(a)`，生命周期 `0`。`IsNeedAccumulation` 的判定见 reduce.cpp:36-41（仅 Sum/Mean/Prod 为真）。

**compare.cpp 的推导思路**（按 dtype × 模式分发）：

```text
在 vectorized_axis 里挑出两个最小的 axis_id（axis_ids[0] < axis_ids[1]）
  ▼
axis_ids[1] 存在（双轴）时，按 dtype 分支：
  FP16/FP32（或 INT32+EQ/NE）  → byte = Max(288*repeat, noLoopSize)，钳制后单块
  INT32                        → byte = 256 * repeat * stride，单块
  INT64 + EQ/NE                → byte = repeat*stride*256*2 + 256，单块
  INT64 + 其他(GT/LT/GE/LE)    → byte = repeat*stride * Align(last_axis,32) * 8 * 5，单块（INT64 需 5 倍缓冲）
  其它                         → CalcDefaultTmpSize
  ▼
axis_ids[1] 不存在（单轴）时：byte = dtype字节 * repeat * stride，INT64 再 ×5
```

compare 的入口分发函数：

[autofuse/ascir/reg_func/compare.cpp:25-46](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/reg_func/compare.cpp#L25-L46) —— `GetCompareNormalTmpSize` 先在 `vectorized_axis` 中找出两个最小的 axis id，作为后续公式里的 `axis_ids[0]`、`axis_ids[1]`。

dtype 分支主体：

[autofuse/ascir/reg_func/compare.cpp:46-95](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/reg_func/compare.cpp#L46-L95) —— 按 FP16/FP32、INT32、INT64(EQ/NE)、INT64(其它) 四档分别组装 `total_size`，INT64 比较类明显需要更多临时空间（×5 或 ×2+常数）。

而六个比较算子的 `CalcXxxTmpSize` 只是带不同 `mode` 字符串的薄封装：

[autofuse/ascir/reg_func/compare.cpp:109-131](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/reg_func/compare.cpp#L109-L131) —— `CalcGeTmpSize("GE")`、`CalcEqTmpSize("EQ")`、`CalcNeTmpSize("NE")`、`CalcGtTmpSize("GT")`、`CalcLeTmpSize("LE")`、`CalcLtTmpSize("LT")` 全部转调 `GetCompareNormalTmpSize`；`mode` 仅在 INT32/INT64 分支里影响路径（EQ/NE 有专属公式）。

#### 4.2.3 用数学公式看一个例子

以 compare 的 INT64 比较类（GT/LT/GE/LE，非 EQ/NE）为例，buffer 字节数为：

\[
\text{size} = \text{repeat}_{a_0} \cdot \text{stride}_{a_1} \cdot \mathrm{Align}(\text{repeat}_{\text{last}},\,32) \cdot 8 \cdot 5
\]

其中 \(a_0,a_1\) 是两个最小向量化轴下标，`Align(·,32)` 表示向上对齐到 32 字节（一个 block），系数 \(8\) 来自 INT64 的 8 字节与一个内部系数，\(5\) 表示「需要 5 倍临时缓冲」（compare.cpp:88 处的注释 `Five times the temp buf is required.` 对应同一倍数关系）。

而 reduce 累加支的高阶 API 缓冲字节数更直观：

\[
\text{api\_size} = 4 \cdot (a_{\text{ub}} \cdot r_{\text{ub}}) + \Delta,\qquad
\Delta =
\begin{cases}
256 & \text{Prod} \\
256+32 & \text{Prod 且 AR 布局} \\
32 & \text{AR 布局} \\
0 & \text{其它}
\end{cases}
\]

这里 \(4\) 即 `sizeof(float)`，\(256\) 与 \(32\) 来自 reduce.cpp:18-19 的 `kPerformanceOptimization` 与 `kBlockSize` 常数。

#### 4.2.4 代码实践：读测试理解「正确大小」

**实践目标**：通过 UT 断言，验证自己对 compare 公式的理解。

**操作步骤**：

1. 打开 `autofuse/tests/ut/ascir/reg_func/test_reg_func_compare.cpp`，看 `CalcGeTmpSize_ShouldReturnCorrectSize_WhenNodelsValid`（约 177 行起）。
2. 关注它如何构造 `Data → Load → Ge → Store → Output` 一条图，把 `vectorized_strides` 设成 `{s1, Symbol(1)}`。
3. 读断言 `ASSERT_EQ(result[0]->size, sym::Min(sym::Max(288*s1, compareNormalTmpSize), MAX_TMP_BUFFER_SIZE))`。

**需要观察的现象**：测试里 `compareNormalTmpSize = 8*s0 + 128*((s0*2+3)/4)`，这正是 `GetCompareNormalNoLoopTmpSize`（compare.cpp:15-23）在 FP32 下的展开。`MAX_TMP_BUFFER_SIZE = 255*256+32 = 65312`，对应 `GetTmpBuffer` 的钳制上限。

**预期结果**：你能口述「Ge 在 FP32、双轴下，buffer 大小 = Min(Max(288*repeat, noLoop), 65312)」，并理解每一项的来源。

#### 4.2.5 小练习与答案

**练习 1**：reduce.cpp 为什么把 `GetByteSize` 硬编码成 `sizeof(float)=4`，而不是按输入 dtype 取字节大小？compare.cpp 又是怎么做的？

> **参考答案**：reduce 的注册侧 dtype 约束只允许 FP16/FP32，且归约内部统一升 fp32 累加（见 u3-l1），所以 sizing 按 4 字节算即可。compare 支持的 dtype 更广（FP16/FP32/INT32/INT64），字节数差异大，故 compare.cpp:17 用 `typeSize = dtype==FP16 ? 2 : 4` 等方式按 dtype 动态取值。

**练习 2**：compare 的六个算子为什么共用 `GetCompareNormalTmpSize`，却要传一个 `mode` 字符串？

> **参考答案**：六个比较算子的 buffer 结构高度相似（都是单块、生命周期 -1），可共用主函数；但 INT32/INT64 下 EQ/NE 与其它比较的底层实现不同（EQ/NE 有更省空间的路径），所以用 `mode` 在内部做最小分支，避免写六份几乎重复的代码。

### 4.3 tiling / 属性占位：TmpBufDesc 是给谁的「占位符」

#### 4.3.1 概念说明

读到这儿你可能有个疑问：`reg_func` 算出来的 `size` 还是个带符号变量（如 `4 * s1`）的表达式，并不是一个具体字节数。那它到底有什么用？

答案是：**`TmpBufDesc` 是一份「占位契约」**。它告诉下游的 tiling（ATT，见 u7）与内存分配（buffer_allocate，见 u6-l4）：

- 这个算子需要**几块**临时缓冲（`vector` 里有几个元素）；
- 每块的**相对大小公式**（`size` 表达式，tiling 求解出具体 shape 后再代值）；
- 每块的**生命周期**（`life_time_axis_id`，决定能否与别的缓冲复用同一块 UB）。

换句话说，`reg_func` 在编译期只给出「形状占位」，真正的字节数要等 ATT 把 tiling 表达式求解完才落定。这就是本讲 minimal module「tiling/属性占位」的准确含义：**reg_func 产出 tiling 的输入占位，同时读取算子的属性（dtype、type、mode）来决定占位内容。**

#### 4.3.2 核心流程

```text
reg_func 输出 vector<TmpBufDesc>
  │
  ├─ size（符号表达式） ──► ATT/tiling 代入具体 shape ──► 具体字节数 ──► UB 内存分配
  │
  └─ life_time_axis_id ──► buffer_allocate 据此做生命周期复用
        ├─ -1 ：API 级，用完即弃，可在不同算子间复用
        └─ >=0：loop 级，跨循环迭代存活，绑定到第 N 层循环轴
```

生命周期字段在 reduce.cpp 里被刻意区分使用。累加支与非累加支都产出两块缓冲，一块标 `-1`（高阶 API 内部用），一块标 `0`（UB 间，跨一次迭代）：

[autofuse/ascir/reg_func/reduce.cpp:86-95](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/reg_func/reduce.cpp#L86-L95) —— `desc2 = {api_size, -1}`（API 级）与 `desc3 = {a_size, 0}`（loop 级，axis 0）。

ArgMax 系列更复杂，需要保留「历史最大值」跨多轮 R 轴分核累加，于是出现生命周期 `0/1/2` 的多块缓冲：

[autofuse/ascir/reg_func/reduce.cpp:108-130](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/reg_func/reduce.cpp#L108-L130) —— ArgMax/ArgMaxMultiRPhase2 产出生命周期 0/1/2 三块（index 临时、value 临时、历史最大结果），ArgMaxMultiRPhase1 产出 0/1 两块。注释清楚说明了每块的用途。

另一处「占位约束」来自公共 helper `GetTmpBuffer`——它会对 size 做**硬件上限钳制**，因为单条 Vector 指令最多处理 255 个 repeat：

[autofuse/ascir/reg_func/default_reg_func.cpp:22-29](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/reg_func/default_reg_func.cpp#L22-L29) —— `GetTmpBuffer` 用 `sym::Min(tmp_size, 255*256+32)` 把单块缓冲钳到 65312 字节内，生命周期统一 `-1`。

最简单的「占位」莫过于默认大小：当一个算子没有特殊 sizing 需求时，直接给一块固定 8192 字节的缓冲：

[autofuse/ascir/reg_func/default_reg_func.cpp:31-34](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/reg_func/default_reg_func.cpp#L31-L34) —— `CalcDefaultTmpSize` 返回 `GetTmpBuffer(8192)`，是绝大多数简单 unary 算子的兜底。

tanh 就是这样一个最简样例——它的 `reg_func` 全文只有一行：

[autofuse/ascir/reg_func/tanh.cpp:14-16](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/reg_func/tanh.cpp#L14-L16) —— `CalcTanhTmpSize` 直接 `return CalcDefaultTmpSize(node)`，即一块 8192 字节、生命周期 -1 的缓冲。

> 「属性」这一面也别忽略：compare.cpp 里的 `mode`、reduce.cpp 里的 `node.GetType()`（"Sum"/"Prod"/"ArgMax"）、以及从 `node_inputs[0].attr.dtype` 取 dtype，都是 reg_func 在读「算子属性」来决定 sizing。注册侧 `REG_ASC_IR(Ge)` 里 `T1 ∈ {FP16,FP32,INT32,INT64}` 这条 dtype 约束（见 4.1.2），则是「属性占位」的另一半——它先在注册表里圈定合法 dtype，reg_func 再据此分支。

#### 4.3.3 代码实践：用 UT 观察生命周期与缓冲块数

**实践目标**：亲眼确认 reduce 产出 2 块缓冲、compare 产出 1 块，并核对生命周期字段。

**操作步骤**：

1. 打开 `autofuse/tests/ut/ascir/reg_func/test_reg_func_reduce.cpp`，看 `CalcReduceTmpSize_test_0`（约 150 行）。
2. 注意它构造的是 `Max`（非累加）图，断言 `ASSERT_EQ(result.size(), 2)`。
3. 再打开 `test_reg_func_compare.cpp` 的 `CalcGeTmpSize_...`（约 177 行），断言 `ASSERT_EQ(result.size(), 1)` 且 `result[0]->life_time_axis_id == -1`。

**需要观察的现象**：reduce 因为「高阶 API 缓冲 + UB 间缓冲」两块结构恒为 2；compare 只有单块 API 级缓冲（`-1`）。两份 UT 都用 `extern` 声明引用 reg_func 的内部符号（见 test_reg_func_compare.cpp:24-29），说明这些函数本是 `reg_func/` 内部实现、靠 UT 直接白盒测试。

**预期结果**：你能解释「为什么 Max 的 result 是 2 块、Ge 的 result 是 1 块」，并知道每块的 `life_time_axis_id` 含义。

#### 4.3.4 小练习与答案

**练习 1**：如果一个新的 unary 算子（比如某个自定义激活函数）在 UB 里计算时不需要任何中间缓冲，它的 `CalcXxxTmpSize` 该返回什么？

> **参考答案**：返回一个**空 vector**（`return {};`）即可，表示该算子不需要临时缓冲。但仓库里多数 unary 算子（如 tanh）出于稳妥会复用 `CalcDefaultTmpSize` 给一块 8192 字节缓冲；是否需要 buffer 取决于底层 AscendC API 的实现，需对照该 API 的 tiling 要求确认。

**练习 2**：`GetTmpBuffer` 为什么要把 size 钳到 `255*256+32`？

> **参考答案**：硬件上单条 Vector 指令最多 255 个 repeat、每 repeat 256 字节，再加 1 个 block（32 字节）余量，构成单次 API 内临时缓冲的物理上限（见 default_reg_func.cpp:17-20 的常数）。超过这个上限的公式毫无意义，故在编译期就用 `sym::Min` 钳住。

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个「新增一个算子」的纸面任务（仓库里其实已有 tanh，正好用来对照自检）：

> **任务**：假设要新增一个 unary 算子 `Tanh`（逐元素双曲正切），请对照 reduce.cpp 与 compare.cpp，写出它的 `reg_func` 注册函数应包含的「最小要素清单」，并说明每条要素分别落在调用链的哪一段。

请按下面的骨架作答，然后与仓库真实实现对照：

1. **声明**：在 `defalut_reg_func.h` 加一行 `CalcTanhTmpSize` 声明（落在 4.1 的统一签名）。
2. **实现**：在 `reg_func/tanh.cpp` 写 `CalcTanhTmpSize`，因为 tanh 无特殊 buffer 需求，转调 `CalcDefaultTmpSize`（落在 4.2/4.3 的「默认占位」）。
3. **绑定**：在 `v1_ascir_codegen_impl.h` 定义 `TanhAscIrCodegenImpl`，其 `CalcTmpBufSize` 转发 `CalcTanhTmpSize`，并给出 `GetApiCallName="UnaryApiTmpCall"`、`GetApiName="Tanh"`、`IncludeApiHeaderFiles={"adv_api/math/tanh.h"}`（落在 4.1 的 codegen 实现类）。
4. **登记**：在 `ascir_builtin_ops_v1.cpp` 用 `REG_ASC_IR(Tanh).Input("x","T").Output("y","T").ComputeType(kComputeElewise).Impl(...)` 把实现类登记进注册表，并给 dtype 约束（落在 4.1 的注册宏）。

**自检方式**：打开仓库里真实的 `autofuse/ascir/reg_func/tanh.cpp`（见 4.3.2 引用）与 `v1_ascir_codegen_impl.h` 的 `TanhAscIrCodegenImpl`，逐条核对你的清单是否覆盖了这四处改动。如果你写漏了「在 `defalut_reg_func.h` 加声明」这一步，编译时会报未声明符号——这正是最小要素清单的价值。

> 说明：本任务为源码阅读型实践，不要求在本机编译运行；如确需上板验证新增算子，请回到 u1-l3/u1-l4 的构建与环境讲义，按 `-j 8` 约定编译 Autofuse。

## 6. 本讲小结

- `reg_func` 不是注册宏本身，而是 codegen 实现类 `CalcTmpBufSize()` 的**外置实现体**，调用链为 `REG_ASC_IR → XxxCodegenImpl::CalcTmpBufSize → CalcXxxTmpSize`。
- 所有 `CalcXxxTmpSize` 签名统一为 `vector<unique_ptr<TmpBufDesc>>(const AscNode&)`，集中声明在 `defalut_reg_func.h`，公共 helper 在 `default_reg_func.cpp`。
- 形状推导全部用**符号表达式** `Expression` + `sym::` 运算符完成；reduce 按 A/R 轴分离 + 是否累加分支，compare 按 dtype × mode 分发。
- `TmpBufDesc` 是给 tiling 与内存分配的**占位契约**：`size` 是待求解的字节公式，`life_time_axis_id`（`-1` API 级 / `>=0` loop 级）决定缓冲复用。
- `GetTmpBuffer` 会把单块缓冲钳到硬件上限 `255*256+32=65312` 字节；无特殊需求的算子可复用 `CalcDefaultTmpSize`（8192 字节），tanh 即最简样例。
- reg_func 同时读取算子「属性」（dtype、`GetType()`、`mode`）来决定 sizing，与注册侧的 dtype 约束互为表里。

## 7. 下一步学习建议

- 本讲只覆盖了三元组中的 **codegen 实现侧** 的 buffer sizing。下一步建议进入 **u5-l3（AscendC API 头文件与算子能力）**，看 codegen 实现类里 `GetApiName()`/`LoadApiHeaderFiles()` 指向的真实 AscendC 算子头（如 `reduce.h`、`compare.h`），理解「buffer 占位」最终如何落到一条条设备 API 调用。
- 若想顺着 `TmpBufDesc.size` 这个符号表达式往下追，看它如何被求解成具体字节数，可跳到 **u7（ATT 自动 Tiling）**，尤其是 u7-l2 的表达式生成与求解器。
- 若想看 buffer 描述符如何被内存分配消费、生命周期如何参与复用，可读 **u6-l4（调度任务生成与内存分配）**。
- 想动手加一个新算子的读者，可结合仓库 skill `af-reg-ascir`（ASCIR 新增/更新的完整修改面），它正是把本讲「最小要素清单」自动化成注册、regbase、Codegen、Python、UT/ST 的修改方案。
