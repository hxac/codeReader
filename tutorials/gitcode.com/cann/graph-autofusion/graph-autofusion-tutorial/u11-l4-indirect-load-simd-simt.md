# IndirectLoad SIMD/SIMT 地址计算与执行

## 1. 本讲目标

本讲聚焦 v35 平台上 IndirectLoad（间接加载，即 `y = x[index]` 形态的 gather）的设备端实现与代码生成。学完本讲，你应该能够：

1. 说清 SIMD 与 SIMT 两种寻址/执行模式的本质差异，以及各自适合的场景。
2. 读懂 `indirect_load_simd_policy.h` 中的策略体系：IndexPolicy / ValuePolicy / 四种 AddressMode，以及 `IndirectLoadSimdDispatch` 的分发顺序。
3. 理解 SIMT 侧五类地址策略（静态二次幂、静态内层、Structured Magic、递归、跨步）的选择依据，以及 magic number 除法、线程数阶梯等加速技巧。
4. 追踪 `reg_indirect_load_api_call.cpp` 如何根据模板 ID 与逻辑视图，生成三种不同形态的设备端调用代码。

本讲对应仓库最近一次性能优化提交 `650647ec`（perf: 优化 IndirectLoad SIMD/SIMT 地址计算与执行），其自述目标是：SIMD 用 MicroAPI 寄存器 Gather 复用 index 加载与 byte offset 构造；SIMT 按 shape 选择地址策略并按数据规模选择线程数；按地址范围选择 32/64 位 offset；性能策略仅用于连续（dense）布局，其他布局回退原有实现。

## 2. 前置知识

- **间接加载（IndirectLoad / gather）**：普通 DataCopy 的源地址是连续可推导的，而 gather 的每个输出元素的源地址由 index 张量的值决定：`y[o] = x[index[o]]`。地址不可预知、访存不连续，是 embedding、torch.gather 等场景的核心开销。
- **SIMD（Single Instruction Multiple Data）**：向量执行模式。一条指令对一整条向量寄存器（`VECTOR_REG_WIDTH` 字节宽）里的多个 lane 同时运算。昇腾 MicroAPI（`MicroAPI::RegTensor`、`MicroAPI::MaskReg` 等）就是面向向量单元的细粒度编程接口。SIMD 路径的数据在 UB（Unified Buffer，片上统一缓冲）中流转。
- **SIMT（Single Instruction Multiple Threads）**：线程执行模式。把一段标量计算发射成一簇线程，每个线程处理一个元素，用 `threadIdx.x / blockDim.x` 的网格跨步循环覆盖全部元素（`__simt_vf__` / `Simt::VF_CALL` 是发射原语）。SIMT 路径直接 GM→GM 或 GM→UB，天然适合不连续访存。
- **逻辑视图与布局分类**：上游（u6-l4 的 IndirectLoad 场景生成器）会给每个 IndirectLoad 节点挂一份 `TemplateLogicalView`，把 input/index/output 三张张量的 sizes 与 strides 用符号表达式描述，并把布局分成 `kDense`（连续）、`kZeroStrideCompact`（零步距压缩）、`kStrided`（跨步）、`kUnsupported` 四类。本讲的"快速路径"只服务 dense 布局。
- **除法魔数（magic number division）**：硬件除法很慢。若除数 d 在编译期或构造期已知，可预先算出魔数 M 与移位数 s，把 `n / d` 变成一次乘法加一次移位：

  \[ q = \left\lfloor \frac{n}{d} \right\rfloor \approx \left\lfloor \frac{n \cdot M}{2^{32+s}} \right\rfloor \]

  本讲 SIMT 侧大量使用这一技巧（`Simt::UintDiv`）。

建议先复习 u11-l1（v35 平台扩展机制）与 u8-l3（api_call 体系与 `ApiCallFactory` 自注册）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [autofuse/v35/ascendc/api_regbase/indirect_load_simd_policy.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simd_policy.h) | SIMD 路径的策略层：index 加载策略（int32/int64）、按值 dtype 的 gather 策略、地址上下文与四种地址模式的地址修正函数 |
| [autofuse/v35/ascendc/api_regbase/indirect_load_simd.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simd.h) | SIMD 路径的执行层：寄存器 Gather 主体、四种地址模式的分发、dense/strided 两个对外的 `IndirectLoadSimd` 入口与 `IndirectLoadSimdGatherApi` |
| [autofuse/v35/ascendc/api_regbase/indirect_load_simt.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simt.h) | SIMT 路径：五类地址策略、线程核函数、按规模分档的线程数调度与对外入口 `IndirectLoadSimt` |
| [autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp) | codegen 侧：解析节点属性、规划 SIMT 策略、打印三种调用（SK/SIMD/SIMT）的设备端代码 |
| [autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.h) | `IndirectLoadRegApiCall` 类声明与成员（模板 ID、逻辑视图、SIMT 区域元数据） |
| [autofuse/common/indirect_load_utils.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/common/indirect_load_utils.h) | 公共契约：`TemplateRole`、`Implementation`、`IndirectLoadLayoutKind`、`TemplateLogicalView` 等跨模块共享类型 |

## 4. 核心概念与源码讲解

### 4.1 IndirectLoad 的地址问题与 SIMD/SIMT 两种寻址模式

#### 4.1.1 概念说明

设输出逻辑坐标沿 rank 分解为三段：外层维（dim < axis）、被索引轴（dim == axis）、内层维（dim > axis），则每个输出元素的源偏移为：

\[ \text{src\_idx} = \underbrace{O_{\text{outer}}}_{\text{外层偏移}} + \underbrace{\text{index}[o] \cdot S_{\text{axis}}}_{\text{被索引轴}} + \underbrace{I_{\text{inner}}}_{\text{内层偏移}} \]

三种偏移的"已知时机"不同：内层偏移随 lane 位置线性变化（编译期可知其规律），index 值必须运行期加载，外层偏移在当前 block 窗口内往往是常量。两种执行模式的差异正来自于"谁来算这个公式"：

| 维度 | SIMD（向量模式） | SIMT（线程模式） |
| --- | --- | --- |
| 计算单元 | Vector 单元，一条指令处理整条向量寄存器 | 线程簇，每线程一个元素 |
| 数据位置 | x/index/y 都在 UB | x 在 GM，y 可在 GM 或 UB |
| 地址计算 | 向量指令在寄存器里并行算 `index*S+I`，用掩码处理尾部 | 每线程用标量整数运算算自己的偏移 |
| 尾部处理 | `UpdateMask` 部分掩码 | 网格跨步循环天然覆盖任意规模 |
| 附加能力 | 需要与上下游 UB 算子融合时收益最大 | 可把 index 侧/output 侧的逐元素小算子内联进来 |
| 典型场景 | dense 布局、规模规整、需与 Vector 融合链衔接 | 大规模、跨步/递归布局、或直接 GM→GM 落盘 |

#### 4.1.2 核心流程

codegen 为同一类节点支持三种模板：SK（超核路径，u10 展开）、SIMD、SIMT。选择记录在节点的 `TemplateId` 属性上，由 optimize 阶段的 IndirectLoad 场景生成器（u6-l4）枚举候选后交给调度打分体系。粗略决策：

```text
IndirectLoad 节点
 ├─ TemplateId = kIndirectLoadSk   → GenerateSk   （超核路径，走 tmp buffer + 标量偏移表）
 ├─ TemplateId = kIndirectLoadSimd → GenerateSimd （UB 向量 Gather，dense 快速路径 / strided 回退）
 └─ TemplateId = kIndirectLoadSimt → GenerateSimt （线程簇 GM 直取，含内联变换）
```

#### 4.1.3 源码精读

模板 ID 的合法性检查在 codegen 侧的属性解析处完成：

[reg_indirect_load_api_call.cpp:596-621](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp#L596-L621)——`ParseAttr` 读取 `axis` 属性（支持负数轴，`axis_ = axis + rank` 归一化）、模板轴、post-reduce 消费者，并断言模板 ID 必须是 SK/SIMD/SIMT 三者之一；SIMT 模板再进入 `ParseSimtAttr` 做区域元数据收集。

布局分类的契约定义在公共头里：

[indirect_load_utils.h:70-86](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/common/indirect_load_utils.h#L70-L86)——`IndirectLoadLayoutKind` 定义 kDense/kZeroStrideCompact/kStrided/kUnsupported 四档，`TemplateLogicalView` 携带 input/index/output 三张张量的 sizes/strides 符号表达式，是本讲所有策略判断的输入。

#### 4.1.4 代码实践

1. **实践目标**：在源码层面确认"三种模板、两条执行模式"的分叉点。
2. **操作步骤**：
   - 打开 `reg_indirect_load_api_call.cpp`，定位 `Generate`（张量版）与 `Generate`（无张量版）两个重载，观察它们分别接受哪类模板 ID。
   - 用 `grep -n "kIndirectLoadSimd\|kIndirectLoadSimt\|kIndirectLoadSK" autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp` 数出每类模板的引用点。
3. **需要观察的现象**：张量版 `Generate` 只服务 SK 与 SIMD；SIMT 走的是无张量参数的重载（因为它绕过 UB 张量抽象、直接用 GM 指针）。
4. **预期结果**：三条分支互斥，由 `template_id_` 唯一决定。
5. 本实践为纯源码阅读，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：为什么 SIMT 入口的 `Generate` 重载不接收 `inputs/outputs` 张量引用？

**答案**：SIMT 路径直接以 `__gm__` 指针访问全局内存（`GenerateSimt` 中 `input_ptr = (__gm__ ...)input.GetPhyAddr()`），输出要么直接写 GM（`output_gm_tensor_`）、要么写入 post-reduce 后的 UB 张量，均不在标准 Tensor 抽象的输入输出位置上，因此走只带 `current_axis` 的重载（见 [reg_indirect_load_api_call.cpp:729-738](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp#L729-L738)）。

**练习 2**：若 index 张量是 int64，SIMD 是否还能走寄存器 Gather？

**答案**：能。`IndirectLoadSimdIndexPolicy<int64_t>` 是支持的特化（kSupported = true），它把 64 位 index 拆成两半用 `DeInterleave` 抽出低 32 位（详见 4.2.3）。

### 4.2 SIMD 路径：indirect_load_simd_policy 策略体系与地址模式分发

#### 4.2.1 概念说明

`indirect_load_simd_policy.h` 是 SIMD 路径的"策略层"，用模板特化把三件可变的事拆开：

1. **IndexPolicy**：index 张量怎么从 UB 加载进向量寄存器（按 index dtype 选择：int32 / int64）。
2. **ValuePolicy**：值怎么 gather 回来（按值 dtype 的位宽选择：32 位 / 16 位）。
3. **AddressMode**：地址修正怎么做（按布局规律选择：kDirect / kDensePow2 / kDenseGeneric / kStrided）。

这种"正交拆分 + 编译期分发"是整个文件的设计核心：dtype 与布局互不纠缠，新增一种组合只需补一个特化。

#### 4.2.2 核心流程

SIMD 主体的执行流程（dense 布局）：

```text
IndirectLoadSimd<x, index, Rank, Axis>(x, index, y, ...)
 └─ 按"第4个参数是否为 tmp<uint8_t>"区分 dense / strided 两个实现
IndirectLoadSimdDenseImpl
 ├─ InitIndirectLoadSimdAddressContext：从 shape/stride 推导
 │    index_inner（内层元素数）、input_inner（轴 stride）、
 │    inner_layout_matches（内层是否真连续）、output_position（块内位置）
 └─ IndirectLoadSimdRegGather → IndirectLoadSimdDispatch（地址模式分发）
      ├─ Axis+1==Rank                    → kDirect   （index 即地址，零修正）
      ├─ !inner_layout_matches           → kStrided  （逐维 DivMod 修正）
      ├─ index_inner 为 2 的幂
      │    ├─ 规模大且整除 repeat 宽度   → RunReuse  （循环外预计算内层偏移）
      │    └─ 否则                       → kDensePow2（And 掩码取模）
      └─ 其他                            → kDenseGeneric（Div+Mul+Sub 取模）

每个 repeat（一条向量寄存器的元素数）：
  IndexPolicy::Load(source_index)                  # 加载一段 index
  IndirectLoadSimdApplyAddress<Mode>(...)          # source_index *= input_inner; += inner_offset
  Action::Commit<ValuePolicy>(...)                 # DataCopyGather + DataCopy 写回 y
```

四种地址模式下，核心公式 \(\text{src} = \text{index} \cdot S_{\text{axis}} + I_{\text{inner}}\) 中 \(I_{\text{inner}}\) 的求法不同：

- **kDirect**：无内层维（Axis 是最后一维），\(I_{\text{inner}} = 0\)，index 值本身就是元素偏移，零修正。
- **kDensePow2**：内层连续且 \(I_{\text{inner}}\) 周期是 2 的幂，用 `position & (index_inner - 1)` 一条与门完成取模。
- **kDenseGeneric**：周期非 2 的幂，用 Div/Mul/Sub 三条指令完成取模。
- **kStrided**：内层不连续，需逐维做 DivMod 并乘以各维 stride 累加（`IndirectLoadSimdAddInnerOffset` 递归展开）。

#### 4.2.3 源码精读

**IndexPolicy：index 加载。**

[indirect_load_simd_policy.h:21-57](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simd_policy.h#L21-L57)——`IndirectLoadSimdIndexPolicy<int32_t>` 特化：用 `MicroAPI::DataCopyUnAlignPre` 准备非对齐加载状态，`Load` 以 `POST_MODE_UPDATE`（加载后自动前移地址）方式把 index 分批搬进 `RegTensor<uint32_t>`；`LoadPair` 一次装两条寄存器并分别为它们生成有效掩码。

[indirect_load_simd_policy.h:59-122](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simd_policy.h#L59-L122)——`<int64_t>` 特化：寄存器按 32 位装载数据，`LoadHalf` 先装 count×2 个 32 位字，再用 `MicroAPI::DeInterleave` 把低/高 32 位交错分离，只留低 32 位作为有效 index（高位被丢弃，意味着这条路径假设 index 值不超 uint32 范围）。

**ValuePolicy：按值位宽的 gather。**

[indirect_load_simd_policy.h:148-162](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simd_policy.h#L148-L162)——32 位值类型（float/int32/uint32）：最简路径，`DataCopyGather(value, x, source_index, valid_mask)` 一条指令完成寄存器级 gather，再 `DataCopy` 写回 y。

[indirect_load_simd_policy.h:164-211](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simd_policy.h#L164-L211)——16 位值类型（half/bfloat16 等）：`DataCopyGather` 的索引操作数是 16 位寄存器，于是先把 32 位 source_index 与零寄存器 `DeInterleave` 压成 16 位 gather_index；当输入规模超过 65536（16 位可寻址上限）时，进入**窗口循环**：把输入切成若干个 65536 元素窗口，用 `CompareScalar` 生成"index 落在本窗口内"的掩码，减去窗口基址后再 gather。这是"用掩码运算换取更窄索引位宽"的典型取舍。

**地址上下文与地址修正。**

[indirect_load_simd_policy.h:269-282](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simd_policy.h#L269-L282)——`IndirectLoadSimdAddressContext` 五字段（块内输出位置、输入规模、轴 stride、内层周期、内层是否连续）与 `IndirectLoadSimdAddressMode` 四档枚举，是分发判断的全部依据。

[indirect_load_simd_policy.h:337-364](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simd_policy.h#L337-L364)——`IndirectLoadSimdApplyAddress` 按 Mode 用 `if constexpr` 在编译期裁剪出各自的地址修正序列：kDirect 直接返回；其余先 `Arange + Adds` 构造 lane 位置，`Mul` 完成 `index * input_inner`，再按模式分别走 And 掩码（kDensePow2）、Div/Mul/Sub 取模（kDenseGeneric）或递归 DivMod（kStrided）。

**执行层与分发。**

[indirect_load_simd.h:104-122](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simd.h#L104-L122)——`IndirectLoadSimdRunRepeat` 是单个 repeat 的三步主体：Load index → ApplyAddress → `Action::Commit<ValuePolicy>`。`Action` 是策略化的"提交动作"：`IndirectLoadSimdGatherAction` 直接寄存器 gather 写 y（[indirect_load_simd.h:55-71](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simd.h#L55-L71)），`IndirectLoadSimdOffsetAction` 只把字节偏移写进 offsets 缓冲供 `Gather` API 消费（[indirect_load_simd.h:73-93](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simd.h#L73-L93)）——同一套地址计算被两个 Action 复用，正是本次提交"复用 index 加载和 byte offset 构造"的落点。

[indirect_load_simd.h:139-178](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simd.h#L139-L178)——`IndirectLoadSimdRunMode`：主体按 `full_repeats / tail_count` 拆循环与尾部，尾部用 `UpdateMask(tail_count)` 生成部分掩码。注意 L151 的特化分支：16 位值 + kDirect 模式改走 `RunPair` 双寄存器配对路径，提高满载率。

[indirect_load_simd.h:180-218](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/autofuse/v35/ascendc/api_regbase/indirect_load_simd.h)——`IndirectLoadSimdRunReuse`（实际行号 L180-L218，链接见 [indirect_load_simd.h:180-218](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simd.h#L180-L218)）：当规模大于一条寄存器宽度且周期整除时，把"内层偏移 = (output_position + lane) & (index_inner-1)"在整个循环外一次算好存进 `inner_offset` 寄存器，循环体内只剩 `Mul + Add` 两条指令——这就是 RunReuse 相比 kDensePow2 的进一步省指令版本。

[indirect_load_simd.h:235-257](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simd.h#L235-L257)——`IndirectLoadSimdDispatch`：按 4.2.2 流程图顺序做地址模式分发，所有判断都是运行期布尔/整数判断（依赖 tiling 后的实际 shape），但每个分支内部用模板参数 `Mode` 编译期裁剪。

[indirect_load_simd.h:319-368](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simd.h#L319-L368)——`IndirectLoadSimdStridedImpl`：strided 回退路径，逐元素标量计算 `index_offset → 读 index 值 → src_idx`，写入 uint32 字节偏移表（复用 tmp 缓冲），经 `SetFlag/WaitFlag<HardEvent::S_V>` 保证标量侧写入对向量侧可见后，调用原生 `Gather` API。编译期递归的 `IndirectLoadSimdInnerOffset/OuterOffset`（[indirect_load_simd.h:22-53](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simd.h#L22-L53)）用 `Dim` 递归模板把多维 DivMod 在编译期展开。

[indirect_load_simd.h:387-404](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simd.h#L387-L404)——`IndirectLoadSimdGatherApi`：先用 `IndirectLoadSimdBuildOffsets`（复用同一套 Dispatch/Action 机制，只是 Action 换成 OffsetAction）把 index 缓冲**原地**改写成字节偏移表（L394-396 的注释点明：index 在此后即是"死"数据，免去了额外分配一块缓冲），再调用原生 `Gather`。

#### 4.2.4 代码实践

1. **实践目标**：验证"同一套地址计算，两种提交动作"的复用结构。
2. **操作步骤**：
   - 在 `indirect_load_simd.h` 中分别定位 `IndirectLoadSimdGatherAction::Commit` 与 `IndirectLoadSimdOffsetAction::Commit`，比较它们的参数列表与函数体。
   - 再看 `IndirectLoadSimdRegGather`（L259）与 `IndirectLoadSimdBuildOffsets`（L270）如何仅通过模板参数 `Action` 的不同，共享整个 `IndirectLoadSimdDispatch`。
3. **需要观察的现象**：两个入口的 dispatch 逻辑逐行相同，唯一差异是 Action 模板实参。
4. **预期结果**：能画出"IndexPolicy × ValuePolicy × AddressMode × Action"的四维权ization矩阵，并指出每种组合由哪些模板参数决定。
5. 本实践为纯源码阅读，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `IndirectLoadSimdRunReuse` 要求 `(reuse_elements & (context.index_inner - 1U)) == 0U`？

**答案**：RunReuse 在循环外用 `Arange + output_position` 直接生成 lane 的全局位置，再用 `& (index_inner-1)` 取内层偏移；若周期（index_inner）不能整除寄存器宽度（reuse_elements），相邻 repeat 的起始位置与周期不对齐，预计算的 `inner_offset` 只对第一个 repeat 正确，后续 repeat 会错位。整除保证偏移模式在每个 repeat 内完全重复。

**练习 2**：16 位值类型在输入超过 65536 元素时为什么要进窗口循环？窗口边界如何参与 gather？

**答案**：gather 索引寄存器是 16 位，最大只能表示 65535，直接寻址覆盖不了更大输入。窗口循环把输入切成 ≤65536 的窗口，对每个窗口用 `CompareScalar(GE window_base)` 与 `CompareScalar(LT window_end)` 生成窗口掩码，只有 index 落入窗口的 lane 才参与本窗口的 gather（掩码置空保证不重复取数），并把 index 减去窗口基址压回 16 位范围。

**练习 3**：`IndirectLoadSimd` 对外入口（L371-L385）如何区分 dense 与 strided 调用？

**答案**：靠第一个可选参数的类型——若 `FirstArg` 是 `LocalTensor<uint8_t>`（tmp 缓冲）则走 `IndirectLoadSimdStridedImpl`（strided 需要临时缓冲存字节偏移表），否则走 `IndirectLoadSimdDenseImpl`，并用 `static_assert(sizeof...(Args) == 2+3*Rank / 3+2*Rank)` 在编译期校验两类调用的参数个数。

### 4.3 SIMT 路径：五类地址策略与线程束发射

#### 4.3.1 概念说明

SIMT 侧的每线程计算仍是 4.1 的偏移公式，但"偏移怎么算"被抽象成可替换的 **AddressPolicy**：一个带 `GetAddress(output_index)` 成员的策略对象。策略分五档，按"形状信息在编译期已知多少"从快到慢排列：

| 策略 | 已知条件 | 地址计算代价 |
| --- | --- | --- |
| `StaticPowerOfTwoPolicy` | 内层周期与轴跨度都是编译期 2 的幂常量 | 两次移位/与 |
| `StaticInnerPolicy` | 内层周期是编译期 2 的幂，轴跨度运行期给定 | 一次 magic 除 + 移位/与 |
| `StructuredMagicPolicy` | 三张张量 dense 且形状对齐，跨度运行期给定 | 两次 magic 除 |
| `RecursivePolicy` | 一般 dense，任意 rank | rank 次 magic 除（递归解码） |
| `StridedPolicy` | input/index 带跨步布局 | 递归解码 + 非零步距掩码过滤 |

**magic 除法**：构造期对每个维度算出 `(magic, shift)` 对，运行期用 `Simt::UintDiv(x, magic, shift)` 代替硬件除法（对应 2 节的公式 \[ q = \lfloor n \cdot M / 2^{32+s} \rfloor \]）。

#### 4.3.2 核心流程

```text
IndirectLoadSimt<x, y, FusedBody, AddressPolicy, Context>(x, y, ctx, actual_size, output_offset, policy_args)
 ├─ 构造 AddressPolicy（从 policy_args 初始化 magic/shift 或常量）
 └─ DispatchIndirectLoadSimt：按 actual_size 选线程数
      ≤128→128  ≤256→256  ≤512→512  ≤1024→1024  其余→2048（仅 32 位 offset 时）
      └─ Simt::VF_CALL<IndirectLoadSimtKernel<ThreadNum>>(...)
           每线程：for (i = threadIdx.x; i < actual_size; i += blockDim.x)
             address = policy.GetAddress(output_offset + i)     # {index_offset, input_base}
             indirect_index = FusedBody::Index(address.index_offset, context)
             y[output_index] = FusedBody::Output(x[input_base + indirect_index * stride], ...)
```

两个关键设计：

1. **FusedBody 内联**：index 侧与 output 侧的逐元素小算子（如 relu、exp、类型转换）不必先落成中间张量，而是生成 `Index`/`Output` 两个标量求值函数，在线程内联执行——省掉中间搬运。
2. **32/64 位 offset 选择**：若三张张量的最大元素偏移都不超 uint32 上限，则 `OffsetType = uint32_t`，寄存器与带宽减半；否则用 uint64_t。

#### 4.3.3 源码精读

[indirect_load_simt.h:16-50](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simt.h#L16-L50)——魔数计算：`IndirectLoadGetUintDivMagic` 用 64 轮长除法在设备上算出"最优加一魔数"；`IndirectLoadGetUintDivMagicAndShift` 为 32/64 位两种位宽分别求 `(magic, shift)`，2 的幂除数直接退化为移位位数。

[indirect_load_simt.h:84-100](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simt.h#L84-L100)——`StaticPowerOfTwoPolicy`：`IndirectLoadLog2` 在编译期把 2 的幂常量转成移位数，`GetAddress` 只剩两次移位与一次与，是最便宜的地址策略；跨度全部作为模板参数（`ULL` 字面量）编进代码。

[indirect_load_simt.h:124-151](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simt.h#L124-L151)——`StructuredMagicPolicy`：构造期对 inner_span 与 output_axis_span 各算一对 magic，`GetAddress` 做两次 `Simt::UintDiv`，把 output_index 分解为 outer 与 inner 两段。

[indirect_load_simt.h:153-230](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simt.h#L153-L230)——`RecursivePolicy` 与 `StridedPolicy`：都持 `shape/magic/shift` 数组，`IndirectLoadSimtAddressDecoder`（[indirect_load_simt.h:70-81](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simt.h#L70-L81)）以 `Dim` 递归模板从高位维逐维"除取坐标"，每维坐标乘以 stride 累加进 `input_base`。差异在于：Strided 版多一份 index 侧 stride（`shape[2*Rank + dim]`），并用 `InputStrideMask/IndexStrideMask` 两个位掩码在编译期跳过零步距维，`kUsesInputAxis` 也由掩码按位判定。

[indirect_load_simt.h:233-267](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simt.h#L233-L267)——`IndirectLoadSimtCompute`：每线程的三步——`GetAddress` 得 `{index_offset, input_base}`，`FusedBody::Index` 求出 indirect_index，按 `kUsesInputAxis` 决定是否乘轴 stride，最后 `FusedBody::Output` 产出输出值。`IndirectLoadSimtKernel/UbKernel` 是两个线程核（分别写 GM 与写 UB），网格跨步循环覆盖全部元素。

[indirect_load_simt.h:290-315](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simt.h#L290-L315)——`DispatchIndirectLoadSimt` 的线程数阶梯：128/256/512/1024 按规模逐档翻倍；超过 1024 时只有 32 位 offset 策略才升到 2048 线程（`IndirectLoadUse2048Threads`，[L285-288](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simt.h#L285-L288)），64 位 offset 时每线程工作量更大、线程数减半以平衡占用。

[indirect_load_simt.h:318-338](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simt.h#L318-L338)——两个对外入口：GM 输出版发射后做 `V_MTE3` 事件同步（保证向量核随后经 MTE3 搬出的正确性），UB 输出版只需 `PipeBarrier<PIPE_V>`。`actual_size == 0` 时跳过发射但保留同步，保证流水线语义一致。

#### 4.3.4 代码实践

1. **实践目标**：量化五类策略的地址计算代价差异。
2. **操作步骤**：
   - 依次打开五个 Policy 结构体的 `GetAddress`，数一数每个里面的算术操作条数（移位/与/乘/magic 除）。
   - 对照 `IndirectLoadSimtAddressDecoder::Call`，数一数 Recursive 策略在 rank=3 时会执行几次 `Simt::UintDiv`。
3. **需要观察的现象**：策略从 StaticPowerOfTwo 到 Recursive，操作数单调递增；StaticPowerOfTwo 没有任何除法。
4. **预期结果**：rank=3 的 Recursive 需要 3 次 magic 除（每维一次），而 StructuredMagic 只需 2 次（outer/inner 两段），StaticPowerOfTwo 0 次。
5. 本实践为纯源码阅读，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `StridedPolicy` 要用位掩码（`InputStrideMask`）而不是直接遍历全部维度？

**答案**：掩码在编译期以 `if constexpr` 裁剪掉零步距维度的乘加——零步距维的坐标对偏移无贡献，展开它们是纯浪费；同时 `kUsesInputAxis` 也由掩码推出，被索引轴本身不应计入 input_base。这让同一份代码在不同布局下自动生成最小指令集。

**练习 2**：SIMT 输出写 GM 的入口为什么在发射后要 `SetFlag/WaitFlag<HardEvent::V_MTE3>`？

**答案**：SIMT 线程写完 GM/UB 后，后续流水级（如 MTE3 搬出）必须看到写入结果。事件同步建立"向量线程完成 → MTE3 可搬出"的 happens-before 关系；写 UB 的版本只需向量流水内部屏障（`PipeBarrier<PIPE_V>`），因为消费方还在向量流水线上。

### 4.4 reg_indirect_load_api_call：代码生成全链路

#### 4.4.1 概念说明

`IndirectLoadRegApiCall` 是 api_call 体系（u8-l3）在 v35 的 IndirectLoad 特化：它不生成"一行调用"那么简单，而是要在**编译期**把 SIMT 的策略选好、把 FusedBody 的标量表达式逐节点发射出来。核心是"两份产物"：

1. `GenerateFuncDefinition`：在 kernel 源码的函数定义区生成 `Context` 结构体（持有 GM 指针）与 `FusedBody` 结构体（含 `Index`/`Output` 两个标量求值方法）。
2. `Generate`：在调用点生成对 `AscendC::IndirectLoadSk/IndirectLoadSimd/IndirectLoadSimt<...>` 的调用语句，模板实参与策略实参全部实例化到位。

#### 4.4.2 核心流程

```text
ParseAttr(node)
 ├─ 读 axis / TemplateAxes / post-reduce 消费者 / TemplateId / TemplateLogicalView
 └─ (SIMT) ParseSimtAttr：沿 index 生产者链与 output 消费链反向收集
      index_nodes_ / output_nodes_ / simt_gm_tensors_（区域元数据）

GenerateFuncDefinition（仅 SIMT）
 ├─ BuildSimtCodegenPlan：选 policy（5 档）与 offset_type（uint32/uint64）
 ├─ 生成 Context 结构体：每张依赖的 GM 张量一个 __gm__ 指针成员
 └─ GenerateSimtEvaluator("Index"/"Output")：对区域节点拓扑排序地发射
      标量表达式，汇成 FusedBody 的两个 __simt_callee__ 方法

Generate（调用点）
 ├─ SK  ：AscendC::IndirectLoadSk<x, index, Rank, Axis>(x, index, y, tmp, ...)
 ├─ SIMD：AscendC::IndirectLoadSimd / IndirectLoadSimdGatherApi<...>
 │        （layout dense 且 Implementation==kGatherApi 时选 GatherApi 变体）
 └─ SIMT：AscendC::IndirectLoadSimt<x, y, Body, Policy, Context>(
            input_ptr, y_ptr/ub张量, context, size, offset, policy_args...)
```

`BuildSimtCodegenPlan` 的策略选择逻辑（对应源码 L257-297）：

```text
if 输入或 index 布局非 dense        → kStrided（并构建两张非零步距掩码）
else if 三张张量 dense 且形状对齐    （structured）
    if inner_span 与 output_axis_span 都是静态 2 的幂 → kStaticPowerOfTwo
    elif 仅 inner_span 是静态 2 的幂                  → kStaticInner
    elif 跨度可静态求得且不超 int64                   → kStructuredMagic
    else                                             → kRecursive
offset_type = uint32_t 当且仅当
    三张张量最大元素偏移 ≤ uint32 上限
    且各 policy 的 magic 除数都在 uint32/int32 安全范围内
```

#### 4.4.3 源码精读

**编译期策略规划。**

[reg_indirect_load_api_call.cpp:257-297](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp#L257-L297)——`BuildSimtCodegenPlan`：按上面的伪代码选出 `SimtAddressPolicy` 五档之一与 `offset_type`；`TryGetStaticSpans`（[L157-179](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp#L157-L179)）负责把符号表达式尝试折叠成常量跨度（带溢出检查的 `CheckedMul/CheckedAdd`），`IsStructuredSimt`（[L208-221](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp#L208-L221)）判定"三张张量 dense、index 与 output 逐维同形、input 除被索引轴外同形"。

[reg_indirect_load_api_call.cpp:227-235](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp#L227-L235)——`CanUseUint32Offsets`：用最大元素偏移（`TryGetMaxElementOffset`，对每维 `(size-1)*stride` 带溢出检查地累加）判定能否用 32 位 offset。

**SIMT 区域收集与标量发射。**

[reg_indirect_load_api_call.cpp:623-656](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp#L623-L656)——`ParseSimtAttr`：无 post-reduce 时沿"唯一输出消费者链"找到终止的 `Store` 节点（`FindSimtOutputStore`），把 Store 的 GM 输出登记为直写目标；有 post-reduce 时改为收集 Reduce 的输入生产链。`CollectSimtBackwardNodes`（[L421-440](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp#L421-L440)）以工作表方式反向遍历，`ValidateSimtRegionNode`（[L452-461](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp#L452-L461)）保证区域内只允许 Load/Store 与"SIMT 内联变换"节点——这就是 SIMT FusedBody 的边界约束。

[reg_indirect_load_api_call.cpp:526-570](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp#L526-L570)——`GenerateSimtEvaluator`：按区域内节点的依赖顺序逐个发射 `dtype v_<tensor_id> = <expr>;`，表达式由各算子的 V2 codegen 实现（`EmitSimtScalarExpr` → `AscIrCodegenV2::GenerateSimtScalarExpr`，[L56-70](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp#L56-L70)）以**标量形态**生成；GM Load 节点被翻译成 `context.gm_<id>[output_index]`。这正是"算子融合进 SIMT 线程体"的实现机制。

**调用点生成。**

[reg_indirect_load_api_call.cpp:776-828](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp#L776-L828)——`GenerateSimd`：先由 `BuildTensorWindowInfo`（[L93-130](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp#L93-L130)）构建本地窗口的逻辑 strides（对 vectorized 轴改用向量化的单位步距，避免合并轴把逻辑步距塌缩成 1），再在 L808-810 选择 API 名：dense 布局且 `Implementation == kGatherApi` 时用 `IndirectLoadSimdGatherApi`（原生 Gather 变体），否则用 `IndirectLoadSimd`；strided 情况多传一个 tmp 缓冲实参。

[reg_indirect_load_api_call.cpp:830-865](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp#L830-L865)——`GenerateSimtInvocation`：策略类型由 `GetSimtPolicyType`（[L327-345](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp#L327-L345)）拼成模板实参（如 `AscendC::IndirectLoadSimtStaticPowerOfTwoPolicy<uint32_t, 64ULL, 1024ULL, ...>`），策略构造实参由 `GetSimtPolicyArgs`（[L347-367](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp#L347-L367)）按档位决定——静态策略传空、StructuredMagic 传四个跨度、递归/跨步传完整的 sizes+strides 列表；post-reduce 场景输出写 UB 张量并把 output_offset 按 `block_dim_offset + outer_tb_var` 的块级偏移换算。

[reg_indirect_load_api_call.cpp:867-899](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp#L867-L899)——`GenerateSimt`：组装最终调用点——取输入 GM 指针、（无 post-reduce 时）取输出 GM 指针、初始化 Context（`GenerateSimtContextInitializer`，[L581-592](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp#L581-L592)），再经 `FindCurrentAxisVar`（[L369-379](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp#L369-L379)）找到外层 block-inner 轴对应的循环变量作为规模实参。

[reg_indirect_load_api_call.cpp:901-902](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp#L901-L902)——文件末尾的 `ApiCallRegister<IndirectLoadRegApiCall> register_indirect_load_reg_api_call("IndirectLoadRegApiCall")`：沿用 u8-l3 讲过的自注册机制，把本生成器登记进 `ApiCallFactory`。

#### 4.4.4 代码实践

1. **实践目标**：亲手走一遍"策略选择 → 调用语句"的映射，并验证生成器已接入测试体系。
2. **操作步骤**：
   - 阅读单测 [autofuse/tests/ut/codegen/api_call/test_codegen_indirect_load_api_call.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/tests/ut/codegen/api_call/test_codegen_indirect_load_api_call.cpp)，找出其中构造 rank/axis/shape 的用例，记下它断言的生成代码里出现的策略类型名。
   - 阅读端到端用例目录 [autofuse/tests/v35/st/backend_e2e_v2/indirect_load_store_test/](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/tests/v35/st/backend_e2e_v2/indirect_load_store_test/CMakeLists.txt)，注意用例命名里的 `simd` / `simt` / `gather` / `pow2` 后缀与 [scripts/test/run_autofuse_test.sh](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/scripts/test/run_autofuse_test.sh#L774-L783) 中登记的用例名（如 `indirect_load_rank3_axis1_pow2_simt_e2e_v2`）的对应关系。
   - 推演一个具体输入：rank=3、axis=1、output shape = [4, 8, 16]、input shape = [4, 32, 16]、全 dense。手工执行 `BuildSimtCodegenPlan`：inner_span = 16（2 的幂），output_axis_span = 8×16 = 128（2 的幂），三张张量 dense 且 index/output 同形 → 应选 `kStaticPowerOfTwo`，且最大偏移 4×32×16 = 2048 ≤ uint32 上限 → offset_type = uint32_t。
   - 若有可运行的昇腾环境，可运行 `sh build.sh -u autofuse_framework` 触发含本生成器的 UT（具体可用模块组合以 `sh build.sh --help` 与 u1-l3 的路由表为准）。
3. **需要观察的现象**：单测断言中的策略类型字符串与你手工推演的结果一致；e2e 用例名中的 `pow2_simt` 对应 StaticPowerOfTwo 策略路径。
4. **预期结果**：上述 rank=3 例子应生成形如 `AscendC::IndirectLoadSimtStaticPowerOfTwoPolicy<uint32_t, 16ULL, 128ULL, 16ULL, 512ULL>` 的模板实参（其中 input_axis_stride=16、input_axis_span=32×16=512）。
5. 编译/运行步骤**待本地验证**（本环境无昇腾硬件）；策略推演部分纯源码可完成。

#### 4.4.5 小练习与答案

**练习 1**：一个动态 shape 图（sizes 是符号表达式而非常量）会落入哪档 SIMT 策略？

**答案**：`TryGetStaticSpans` 折叠常量失败（`static_spans = false`），于是 `static_power_of_two` 与 `static_inner` 均为假，`magic_divisors_supported` 因 `!static_spans` 而为真 → 落入 `kStructuredMagic`（若 structured 成立）或 `kStrided`（若布局非 dense）。跨度实参改由 `tpipe.tiler.Size(...)` 在运行期求值传入。

**练习 2**：为什么 `BuildTensorWindowInfo` 对 dense / zero-stride-compact 布局直接返回逻辑视图的 strides，而对其他布局要重算"压缩步距"？

**答案**：dense 与零步距压缩布局在构建本地窗口后，逻辑 row-major 步距仍然成立，直接可用；但经过 vectorized 轴合并的布局可能对每个源轴暴露单位步距，直接使用会把如 [s6, s7] 的 index 轴塌缩成 [1, 1]，导致 SIMT 地址解码错误。重算逻辑从最内维向外累计 `compact_stride = stride × size`，并对 vectorized 轴回填真实向量步距（源码 L100-128 的注释即说明这一动机）。

**练习 3**：`GenerateSk` 生成的调用为什么需要 API 级 tmp 缓冲，而 dense 的 SIMD 不需要？

**答案**：SK 与 strided SIMD 都要逐元素构造 uint32 字节偏移表（存在 tmp 缓冲里再交给原生 `Gather`），所以 `tmp_buf_id.find(-1L)` 必须命中；dense SIMD 走寄存器 Gather，偏移全程留在向量寄存器中，不需要任何临时缓冲（`IndirectLoadSimdGatherApi` 更是把已经用完的 index 缓冲原地改写成偏移表，连这块内存都省了）。

## 5. 综合实践

**任务：为一个新的 IndirectLoad 用例推演全链路生成结果。**

设定输入：rank=2、axis=1、output shape = [1024, 300]（fp16 值、int32 index）、input shape = [1024, 4096] dense、index 与 output 同形 dense，且该节点上游挂了一个逐元素 `Exp`（作用在 gather 结果上）、下游直接 `Store` 到 GM。

1. 判定模板与布局：三张张量全 dense，optimize 阶段可枚举 SIMD 与 SIMT 两类候选模板。
2. SIMT 侧推演（对照 4.4）：
   - inner_span = 300（**不是** 2 的幂）→ `kStaticPowerOfTwo` 排除；若 shape 静态已知且 300×1024 的跨度不超限 → `kStaticInner`；若 shape 动态 → `kStructuredMagic`。
   - 值为 fp16 但 offset 关注的是元素偏移：最大偏移 1024×4096 ≈ 4.2M ≤ uint32 上限 → `offset_type = uint32_t`。
   - `Exp` 会被 `CollectSimtRegionNodes` 收进 output 侧区域，`GenerateSimtEvaluator` 在 `FusedBody::Output` 里发射 `v_xxx = exp(...)` 标量表达式，而不是先落 UB 再算。
3. SIMD 侧推演（对照 4.2）：
   - fp16（16 位值）+ axis 为最后一维（kDirect）→ 走 `RunMode` 中的 `RunPair` 双寄存器配对特化分支。
   - 输入规模 1024×4096 > 65536 → 16 位 gather 需要窗口循环。
4. 验证方式：在 [autofuse/tests/ut/codegen/api_call/test_codegen_indirect_load_api_call.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/tests/ut/codegen/api_call/test_codegen_indirect_load_api_call.cpp) 中仿照既有用例补一个同形状的用例（仅作为学习性修改，不要提交），断言生成的策略类型与 API 名；或在有环境下开启 `AUTOFUSE_DFX_FLAGS`（u3-l3）dump 生成的设备源码，人工核对你推演出的模板实参与 FusedBody 表达式。运行结果**待本地验证**。

## 6. 本讲小结

- IndirectLoad 的地址公式 \(\text{src} = O_{\text{outer}} + \text{index} \cdot S_{\text{axis}} + I_{\text{inner}}\) 中三种偏移的已知时机不同，SIMD/SIMT 两套实现的差异本质是"用什么执行单元、在什么时机算这个公式"。
- SIMD 路径用"IndexPolicy × ValuePolicy × AddressMode × Action"四维模板正交拆分：index 加载按 int32/int64 特化，值 gather 按 32/16 位特化（16 位超 65536 走窗口循环），地址修正按 kDirect/kDensePow2/kDenseGeneric/kStrided/RunReuse 五档从"零修正"到"逐维 DivMod"递增代价。
- SIMT 路径把地址计算抽象成五档 AddressPolicy（静态二次幂 > 静态内层 > Structured Magic > 递归 > 跨步），用 magic number 除法替代硬件除法，按数据规模在 128~2048 线程间分档，并按最大偏移选择 32/64 位 offset。
- `IndirectLoadSimdGatherApi` 把用完的 index 缓冲原地改写成字节偏移表、`RunReuse` 把内层偏移提到循环外、SIMT 把 index/output 侧小算子内联进线程体——这三处分别是"复用 index 加载""复用偏移构造""融合执行"三个优化宣言的落点。
- codegen 侧 `IndirectLoadRegApiCall` 两段式工作：`GenerateFuncDefinition` 生成 Context/FusedBody 结构体（SIMT 专属），`Generate` 按 TemplateId 生成 SK/SIMD/SIMT 三种调用语句；策略选择（`BuildSimtCodegenPlan`）全部基于 `TemplateLogicalView` 的符号布局在编译期完成，dense 才走快速路径，其他布局回退原有实现。

## 7. 下一步学习建议

- 下一讲 u11-l5 将进入 v2 特殊函数算子注册链路，看 ASCIR 注册 → reg_func → codegen 的完整新增算子路径，与本讲的"区域节点标量发射"（`GenerateSimtScalarExpr`）形成呼应。
- 建议继续阅读 [autofuse/optimize/task_generator/indirect_load_schedule_case_generator.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/optimize/task_generator/indirect_load_schedule_case_generator.cpp)，弄清 SIMD/SIMT/SK 候选模板是谁枚举、`TemplateId` 与 `TemplateLogicalView` 是在哪一步挂到节点上的（衔接 u6-l4）。
- 想深入 MicroAPI 的读者可以对照 v35 平台的 `DataCopyGather`/`DeInterleave`/`MaskDeInterleave` 等原语语义，体会 16 位 gather 的窗口技巧在寄存器层面的真实形态。
