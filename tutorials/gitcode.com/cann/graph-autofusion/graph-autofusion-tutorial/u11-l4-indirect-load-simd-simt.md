# IndirectLoad SIMD/SIMT 地址计算与执行

## 1. 本讲目标

IndirectLoad（间接访存）对应 `y = x[index]` 这类取数模式，是 embedding 查表、torch.gather、FlashAttention 索引等场景的核心瓶颈点。本讲围绕 v35 平台（昇腾 950）本次优化的 IndirectLoad 实现，讲清三件事：

1. **SIMD 与 SIMT 两套寻址模式的差异**：SIMD 走 Vector 单元的数据通路（MicroAPI 寄存器级 Gather），SIMT 走标量线程并行的直通 GM 管线。
2. **策略选择**：SIMD 侧的四种地址模式（kDirect/kDensePow2/kDenseGeneric/kStrided）如何派发；SIMT 侧五种 AddressPolicy 如何在编译期按形状/布局选定。
3. **api_call 代码生成**：`reg_indirect_load_api_call.cpp` 如何依据模板 id（SK/SIMD/SIMT）与逻辑视图，生成不同的设备端调用代码。

学完后，你应该能读懂一张含 IndirectLoad 的融合图从「模板选择 → 策略推导 → 设备代码打印」的完整链路，并能回答「什么形状走 SIMD、什么形状走 SIMT、什么形状退化到 SK」。

## 2. 前置知识

### 2.1 间接访存（Gather）

普通 DataCopy 的源地址是连续的；间接访存的源地址由一张**索引张量**（index tensor）逐元素决定。设输入 `x` 的 shape 为 `[s_axis, ...]`，在 `axis` 维上做查表：

\[ y[i_0, \dots, i_{axis}, \dots] = x[i_0, \dots, \text{index}[i_0, \dots, i_{axis}, \dots], \dots] \]

即：**除 axis 维外的坐标原样保留，axis 维的坐标换成 index 里的值**。这个语义决定了后面所有地址计算的公式。

### 2.2 SIMD 与 SIMT

- **SIMD**（Single Instruction Multiple Data）：昇腾 Vector 单元的工作方式。一条指令处理一个 repeat 的多个元素，用 `MaskReg` 控制哪些 lane 有效，用 `RegTensor` 表示向量寄存器。本仓 MicroAPI（`MicroAPI::DataCopyGather` 等）就是寄存器级 SIMD 原语。
- **SIMT**（Single Instruction Multiple Threads）：标量线程并行。每个线程独立处理一个（或步长为 `blockDim.x` 的若干）元素，代码形态就是普通 `for` 循环加下标访问，编译为 VF（Vector Fraction）标量线程块。

直觉对比：SIMD 吞吐高但要求按 repeat 组织数据、地址要向量化计算；SIMT 灵活、天然适合「每元素独立寻址 + 可内联标量变换」的场景，但单元素吞吐低。IndirectLoad 恰好卡在两者之间，所以 v35 同时实现并按场景选择。

### 2.3 魔数除法（Magic Number Division）

SIMT 每个线程要把线性下标 `output_index` 分解成各维坐标，需要除法。硬件标量除法很慢，常用技巧是把「除以常数 d」改写为「乘以魔数 m 再右移 s」：

\[ q = \lfloor n / d \rfloor \approx \lfloor m \cdot n / 2^{s} \rfloor \]

`IndirectLoadGetUintDivMagicAndShift` 就是在设备上现算这组 `(magic, shift)` 的实现。

### 2.4 与前面讲义的衔接

- u6-l4 已讲过：调度期 `indirect_load_schedule_case_generator` 会为 IndirectLoad 节点枚举 SIMD（含 GatherApi 变体）/SIMT/SK 候选，并为 Broadcast 生产者构建 `BuildFinalTensorView` 逻辑视图。
- u8-l3 已讲过：codegen 的 api_call 层负责打印「调用语句」，`autofuse/ascendc`（这里是 `autofuse/v35/ascendc`）提供「函数定义」，两端同名对接。
- u11-l1 已讲过：v35 是平台增量目录，源码合流进 `aihac_codegen`，运行期按注册表分流。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `autofuse/v35/ascendc/api_regbase/indirect_load_simd_policy.h` | SIMD 的策略层：索引加载策略（IndexPolicy）、取数策略（ValuePolicy）、地址模式枚举与地址计算原语 |
| `autofuse/v35/ascendc/api_regbase/indirect_load_simd.h` | SIMD 的执行层：按地址模式派发、repeat 循环、寄存器 Gather 与 GatherApi 两个入口 |
| `autofuse/v35/ascendc/api_regbase/indirect_load_simt.h` | SIMT 全套：五种 AddressPolicy、魔数除法、VF 线程 kernel 与线程数派发 |
| `autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp` | api_call 代码生成器：解析模板 id 与逻辑视图，为 SK/SIMD/SIMT 打印不同调用代码 |
| `autofuse/common/indirect_load_utils.h` | 公共元数据：`TemplateRole`、`Implementation`、`IndirectLoadLayoutKind`、逻辑视图结构 |
| `autofuse/tests/ut/codegen/api_call/test_codegen_indirect_load_api_call.cpp` | codegen 侧 UT：手工建图验证 SIMD/SIMT 代码生成 |
| `autofuse/tests/v35/st/backend_e2e_v2/indirect_load_store_test/` | e2e 用例：store/broadcast/stride_zero/torch_gather_strided 四类场景 |

## 4. 核心概念与源码讲解

### 4.1 SIMD/SIMT 寻址模式

#### 4.1.1 概念说明

无论 SIMD 还是 SIMT，IndirectLoad 的核心都是把「输出线性位置」翻译成「输入源地址」。翻译分两步：

1. **索引地址**（index_offset）：输出位置落在 index 张量的哪个元素上，取出 `index_value`；
2. **输入地址**（input_offset）：`index_value × input_axis_stride`，再补上 axis 之外各维的坐标偏移（outer 偏移 + inner 偏移）。

SIMD 与 SIMT 的差别在**这一翻译由谁执行、以什么粒度执行**：

- SIMD：翻译在 Vector 寄存器里按 lane 并行完成（`Arange` 生成 lane 序号，`Mul/Add/And/Div` 向量化算地址），随后 `DataCopyGather` 一次性取一个 repeat。
- SIMT：翻译由每个标量线程对自己的 `output_index` 单独完成，取数就是对 `__gm__` 指标的普通下标访问 `x[input_offset]`。

#### 4.1.2 核心流程

SIMD 寻址的统一公式（对 lane 位置 `p = output_position + repeat_base + lane`）：

\[ \text{src} = \text{index}[p] \times \text{input\_inner} + \text{inner\_offset}(p) \]

其中 `inner_offset(p)` 按 axis 内层布局有三种求法：

| 模式 | inner_offset 求法 | 条件 |
|---|---|---|
| kDirect | 0（index 即源位置） | axis 是最后一维（Axis+1==Rank） |
| kDensePow2 | `p & (index_inner - 1)`（位与代替取模） | 内层连续且 index_inner 是 2 的幂 |
| kDenseGeneric | `p % index_inner`（向量除法） | 内层连续但 index_inner 非 2 的幂 |
| kStrided | 逐维 DivMod 递推 | 内层不连续（input/index 非 dense） |

SIMT 寻址则统一为「分解坐标 → 查 index → 乘 stride → 加 base」：

```
output_index ──(除法分解)──> outer 坐标 与 inner 坐标
address.input_base = outer * input_axis_span + inner
indirect_index     = FusedBody::Index(address.index_offset)   // 可内联标量链
input_offset       = input_base + indirect_index * input_axis_stride
```

#### 4.1.3 源码精读

**（1）SIMD 地址上下文与四种模式**

`IndirectLoadSimdAddressContext` 是 SIMD 寻址的全部输入，五个字段：输出起点、输入实际大小、输入 axis 步长、index 内层元素数、内层布局是否匹配：

[autofuse/v35/ascendc/api_regbase/indirect_load_simd_policy.h:L269-L282](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simd_policy.h#L269-L282)

这段定义了 `IndirectLoadSimdAddressMode` 四值枚举（kDirect/kDensePow2/kDenseGeneric/kStrided），是 SIMD 侧策略选择的全部可能。

`InitIndirectLoadSimdAddressContext` 在设备入口处从形状参数（2×Rank 个：前 Rank 个 size、后 Rank 个 stride）推这个上下文——它顺带验证内层各维 stride 是否恰好等于期望连乘值（`inner_layout_matches`）：

[autofuse/v35/ascendc/api_regbase/indirect_load_simd.h:L279-L301](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simd.h#L279-L301)

**（2）SIMD 向量化地址计算**

`IndirectLoadSimdApplyAddress` 是四种模式的地址翻译中枢。注意几个技巧：`kDirect` 直接 return（index 就是源下标）；`kDensePow2` 用 `And` 代替取模；`kStrided` 走 `IndirectLoadSimdAddInnerOffset` 逐维 DivMod 递推（模板参数 `Dim` 编译期递归展开）：

[autofuse/v35/ascendc/api_regbase/indirect_load_simd_policy.h:L337-L364](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simd_policy.h#L337-L364)

向量取模没有现成指令，`IndirectLoadSimdDivMod` 用「除、乘、减」三步合成：

[autofuse/v35/ascendc/api_regbase/indirect_load_simd_policy.h:L288-L308](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simd_policy.h#L288-L308)

**（3）SIMD repeat 执行循环**

`IndirectLoadSimdRunMode` 把 `actual_size` 切成「整数 repeat + 尾部」，整段用 `ALL()` 全掩码、尾部用 `UpdateMask(tail_count)` 部分掩码；每个 repeat 先 `IndexPolicy::Load` 装索引、再算地址、最后由 `Action::Commit` 执行（Gather 或写偏移）。注意 `uint16_t` 值类型 + kDirect 模式有专门的 Pair 快路径：

[autofuse/v35/ascendc/api_regbase/indirect_load_simd.h:L139-L178](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simd.h#L139-L178)

**（4）16 位值类型的 65536 窗口**

MicroAPI 的 `DataCopyGather` 对 16 位 dtype 只接受 `uint16_t` 索引（最大寻址 65536）。当输入超过 65536 元素时，`IndirectLoadSimdValuePolicy<X, sizeof(uint16_t)>` 用滑动窗口把全局下标换成窗口内 16 位下标，窗口内元素用掩码筛出，逐窗 Gather：

[autofuse/v35/ascendc/api_regbase/indirect_load_simd_policy.h:L164-L211](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simd_policy.h#L164-L211)

这是 SIMD 路径「大输入不失效」的关键设计：正确性靠 `window_mask` 逐窗过滤，代价是窗口数 `ceil(input_actual_size / 65536)` 次循环。

**（5）SIMT 魔数除法与五种 AddressPolicy**

SIMT 把除法预先换成乘法+移位。`IndirectLoadGetUintDivMagicAndShift` 在构造 Policy 时对每个维度算出 `(magic, shift)`；2 的幂除数直接用 `position-1` 作 shift：

[autofuse/v35/ascendc/api_regbase/indirect_load_simt.h:L35-L50](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simt.h#L35-L50)

五种 Policy 按特化程度递减排列：

| Policy | 特化条件 | 寻址代价 |
|---|---|---|
| `IndirectLoadSimtStaticPowerOfTwoPolicy` | 内层跨距与 axis 跨距都是编译期 2 的幂常数 | 两次移位/位与 |
| `IndirectLoadSimtStaticInnerPolicy` | 仅内层跨距是 2 的幂常数，axis 跨距运行期 | 一次 UintDiv + 位与 |
| `IndirectLoadSimtStructuredMagicPolicy` | 三张量 dense 且同形，跨距运行期 | 两次 UintDiv |
| `IndirectLoadSimtRecursivePolicy` | 一般 dense，任意 rank | rank 次 UintDiv 递推 |
| `IndirectLoadSimtStridedPolicy` | input 或 index 非 dense | 递推 + 双掩码（input/index 分开计 stride） |

最特化的示例（全是移位和位与，零除法）：

[autofuse/v35/ascendc/api_regbase/indirect_load_simt.h:L84-L100](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simt.h#L84-L100)

最通用的 `IndirectLoadSimtRecursivePolicy` 用模板递归的 `IndirectLoadSimtAddressDecoder` 从高维到低维逐维分解坐标，`AddCoordinate` 跳过 axis 维（axis 维坐标由 index 决定）：

[autofuse/v35/ascendc/api_regbase/indirect_load_simt.h:L153-L189](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simt.h#L153-L189)

**（6）SIMT 线程执行与线程数派发**

每个 VF 线程按 `threadIdx.x` 步进 `blockDim.x` 处理元素，`FusedBody::Index/Output` 允许把 index 链与输出链上的标量变换（cast、算术）**内联**进同一次访存——这是 SIMT 相对 SIMD 的独特收益（SIMD 必须先落 UB 再做向量计算）：

[autofuse/v35/ascendc/api_regbase/indirect_load_simt.h:L233-L267](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simt.h#L233-L267)

`DispatchIndirectLoadSimt` 按 `actual_size` 分档选线程数（128/256/512/1024/2048），且只有 32 位偏移类型才允许 2048 线程：

[autofuse/v35/ascendc/api_regbase/indirect_load_simt.h:L290-L315](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simt.h#L290-L315)

#### 4.1.4 代码实践

**实践目标**：通过阅读对比两份头文件，归纳 SIMD 与 SIMT 各自的适用场景，并验证一个具体判断。

**操作步骤**（源码阅读型实践，无需上板）：

1. 打开 `autofuse/v35/ascendc/api_regbase/indirect_load_simd.h`，定位 `IndirectLoadSimd` 公开入口（L371-L385），确认它按「第一个参数是否为 `LocalTensor<uint8_t>`（tmp 缓冲）」分流 Strided 与 Dense 两条实现。
2. 打开 `autofuse/v35/ascendc/api_regbase/indirect_load_simt.h`，定位 `IndirectLoadSimt` 两个重载（L318-L338），确认输入是 `__gm__ X *`（直接读 GM）、输出可以是 GM 指针或 UB LocalTensor。
3. 回答：SIMD 的输入输出都在哪里？SIMT 的输入在哪里？这决定了它们各自适合挂在融合图的什么位置。

**需要观察的现象 / 预期结果**（待本地验证部分标注）：

- SIMD：全链路 `__ubuf__`，即必须先经 MTE2 把 x 和 index 搬进 UB，适合「先搬进 UB、再融合后续 vector 计算」的主流水线路径；kStrided 模式还需要一个 tmp UB 缓冲逐元素算字节偏移（[indirect_load_simd.h:L319-L368](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simd.h#L319-L368)）。
- SIMT：输入直接 `__gm__`，省掉整段 MTE2 搬运，且可把 index 链/输出链的标量变换内联进访存表达式；适合「查表后直接落 GM」或「形状不规则、无法向量化对齐」的场景。
- 两者都不是无条件最优：SIMD 的 16 位大输入要走 65536 窗口循环；SIMT 的 Recursive 策略每线程要做 rank 次通用除法。上板性能对比**待本地验证**（可用 u3-l3 的 profiling 方法看 `aiv_vec_time` 与 VF 时间）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `IndirectLoadSimdValuePolicy<X, sizeof(uint16_t)>` 在 `input_actual_size > 65536U` 时必须走窗口循环，而 32 位值类型不用？

**答案**：MicroAPI 的 `DataCopyGather` 以 `RegTensor<uint16_t>` 作 16 位值类型的 gather 索引，16 位无符号数最大寻址 65535；超过 65536 个输入元素时全局下标装不下，必须按 65536 大小的窗口把全局下标减去 `window_base` 换成局部下标，并用 `window_mask` 只让落在窗口内的 lane 参与 gather。32 位值类型直接用 `uint32_t` 索引，可整段寻址，无需窗口。

**练习 2**：SIMT 的 `IndirectLoadSimtRecursivePolicy::AddCoordinate` 中，为什么 `if constexpr (Dim != Axis)` 要跳过 axis 维？

**答案**：IndirectLoad 语义是「axis 维的输入坐标由 index 张量决定」。线性分解得到的 axis 维坐标只用于定位 index 张量中的元素（经 `shape[Rank + Dim]` 计入 index_offset），不能用来累加输入 `x` 的 `input_base`；axis 维对输入的贡献是 `indirect_index * input_axis_stride`，在 `IndirectLoadSimtCompute` 中单独累加。

**练习 3**：`IndirectLoadSimdDispatch` 中 `RunReuse` 分支为什么要求 `index_inner` 是 2 的幂、且 `reuse_elements` 能被其整除？

**答案**：RunReuse 是 kDensePow2 的进一步特化——当输出较大（超过一个寄存器宽度）且内层元素数整除寄存器元素数时，`p & (index_inner-1)` 的结果在每个 repeat 内完全一致，可以预先用 `IndirectLoadSimdInitInnerOffset`（对 lane 序号做一次位与）算出 `inner_offset`，循环内只剩 `Mul + Add` 两条向量指令，省掉逐 repeat 的 Arange/Adds/And 序列。这两个条件保证掩码/对齐不会在 repeat 边界出缝。

### 4.2 simd_policy 策略

#### 4.2.1 概念说明

本模块讲「策略层」如何把一套执行引擎复用成多种行为。SIMD 侧的策略体系由三层正交的策略对象组成：

1. **IndexPolicy**（按 index dtype 特化）：索引张量怎么装进寄存器——int32 直接搬，int64 要 `DeInterleave` 拆高低位；
2. **ValuePolicy**（按值 dtype 特化）：取数后怎么 Gather——32 位直取，16 位走窗口；
3. **AddressMode**（按布局选择）：地址怎么算——Direct/Pow2/Generic/Strided/Reuse。

三者组合出的 `IndirectLoadSimdModeTraits` / `IndirectLoadSimdRegTraits` 同时承担**能力探测**（`kSupported`）职责：不支持的组合在编译期 `static_assert` 报错，而不是运行期失败。

#### 4.2.2 核心流程

SIMD 派发决策树（`IndirectLoadSimdDispatch`，编译期 + 运行期混合条件）：

```
Axis + 1 == Rank ?                        ── 是 ──> kDirect（最便宜）
inner_layout_matches == false ?           ── 是 ──> kStrided（逐维 DivMod）
index_inner 是 2 的幂 ?
  ├─ 是：actual_size > 寄存器元素数 且 整除 ──> RunReuse（预计算内层偏移）
  │      否则                              ──> kDensePow2（位与取模）
  └─ 否                                   ──> kDenseGeneric（向量取模）
```

注意前两条是运行期 `if`（依赖上下文值），地址模式本身作为模板参数编译期展开——这是「运行期数据决定走哪套编译期代码」的典型手法。

#### 4.2.3 源码精读

**（1）IndexPolicy 的 int32/int64 双特化**

主模板把 `kSupported` 置 false 作兜底，只有两个显式特化可用。int64 特化的 `LoadHalf` 用 `DataCopyUnAlign` 搬原始 64 位数据后 `DeInterleave` 把交织的低 32 位抽到 `index`、高 32 位丢到 `high`——因为后续地址运算统一用 `uint32_t` 寄存器：

[autofuse/v35/ascendc/api_regbase/indirect_load_simd_policy.h:L59-L122](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simd_policy.h#L59-L122)

`DataCopyUnAlign` + `POST_MODE_UPDATE` 让连续的 repeat 装载自动推进地址（存在 `LoadState` 里），循环内无需重算索引指针。

**（2）能力矩阵与 repeat 宽度**

`IndirectLoadSimdRegTraits` 汇总三层策略的 `kSupported`，并规定每 repeat 元素数：16 位值类型按 16 位装（除非地址模式非 Direct，此时索引用 32 位寄存器、按 32 位计），其余按索引寄存器宽度：

[autofuse/v35/ascendc/api_regbase/indirect_load_simd_policy.h:L381-L397](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simd_policy.h#L381-L397)

值类型支持清单用宏批量展开（int16/uint16/half/bfloat16/int32/uint32/float）：

[autofuse/v35/ascendc/api_regbase/indirect_load_simd_policy.h:L124-L141](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simd_policy.h#L124-L141)

**（3）派发决策树本体**

[autofuse/v35/ascendc/api_regbase/indirect_load_simd.h:L235-L257](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simd.h#L235-L257)

注意 `Action` 是比模式更高一层的策略：同一个派发树既服务 `IndirectLoadSimdGatherAction`（取数并写 y），也服务 `IndirectLoadSimdOffsetAction`（只算并落盘字节偏移，供 GatherApi 用）。两个 Action 定义在 [indirect_load_simd.h:L55-L93](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simd.h#L55-L93)，`OffsetAction::CommitPair` 里 `count > elements_per_reg` 的判断保证第二段偏移只在需要时写出。

**（4）GatherApi 变体：复用死缓冲**

`IndirectLoadSimdGatherApi` 走「先用寄存器算字节偏移 → 复用已消费完的 index UB 当偏移缓冲 → 调原生 `Gather`」路线，并明确注释了动机——省一块偏移缓冲：

[autofuse/v35/ascendc/api_regbase/indirect_load_simd.h:L387-L404](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simd.h#L387-L404)

`IndirectLoadSimdStoreByteOffsets` 负责把元素下标乘 `sizeof(X)` 变成字节偏移（原生 Gather 吃的是字节偏移）：

[autofuse/v35/ascendc/api_regbase/indirect_load_simd_policy.h:L411-L417](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simd_policy.h#L411-L417)

#### 4.2.4 代码实践

**实践目标**：亲手推一个具体形状在 SIMD 派发树上的落点。

**操作步骤**：

1. 设输入 `x` shape/stride 为 `(4, 100), stride (100, 1)`，index/output 为 `(2, 100), stride (100, 1)`，axis=1，rank=2。按 `InitIndirectLoadSimdAddressContext`（L279-L301）手算：`Axis+1==Rank` 成立吗？落入哪个模式？
2. 改成 axis=0、rank=2（即 `(100, 4)` 输入、axis 维在前）：此时内层是 4 个元素，`index_inner=4` 是 2 的幂。再设 `actual_size=256`、寄存器宽度按 `VECTOR_REG_WIDTH/sizeof(uint32_t)=64` 估算（具体数值以待本地编译环境为准），判断走 kDensePow2 还是 RunReuse。
3. 打开 UT `autofuse/tests/ut/codegen/api_call/test_codegen_indirect_load_api_call.cpp` 的 `BuildSimdGraph`（L70 起），对照它构造的 `strides = {s1, One}` dense 图，说明 codegen 侧给出的逻辑视图为什么能通过 `inner_layout_matches` 检查。

**预期结果**：

1. axis=1 是最后一维，`Axis+1==Rank` 成立，走 **kDirect**——index 值直接就是源位置，零地址运算。
2. `actual_size=256 > 64` 且 `64 % 4 == 0`，走 **RunReuse**。
3. UT 中 input/index 均为 dense 行主序，内层各维 stride 等于连乘期望值，`inner_layout_matches=true`。运行 UT 验证**待本地进行**（`sh build.sh -u autofuse_framework`，具体模块名以 build.sh 路由表为准）。

#### 4.2.5 小练习与答案

**练习 1**：`IndirectLoadSimdModeTraits` 为什么在「16 位值类型且模式非 kDirect」时把 `kElementsPerRepeat` 从 `VECTOR_REG_WIDTH/sizeof(uint16_t)` 降为 `VECTOR_REG_WIDTH/sizeof(uint32_t)`？

**答案**：非 kDirect 模式下地址翻译在 `uint32_t` 的 `source_index` 寄存器里做，且 `IndexPolicy::Load` 每次装载的也是 32 位元素；此时瓶颈是索引通道而非值通道，repeat 按索引寄存器能装的元素数计，避免索引装载与值写回的 lane 数错配。kDirect 模式下地址翻译为空操作，可按 16 位值满宽处理（Pair 路径）。

**练习 2**：`IndirectLoadSimdGatherApi` 与默认 `IndirectLoadSimd`（寄存器 Gather）的本质区别是什么？为什么前者还要一次 `PipeBarrier<PIPE_V>()`？

**答案**：寄存器 Gather 用 `MicroAPI::DataCopyGather` 直接在寄存器间完成取数与写回；GatherApi 则先把每个元素的**字节偏移**写进 UB（复用 index 的原缓冲），再调原生 `Gather(y, x, offsets, 0, actual_size)` 完成搬运。偏移从寄存器落到 UB、再到被 `Gather` 读取，中间跨越同一 Vector 管线内的数据依赖，需要 `PipeBarrier<PIPE_V>()` 保证写偏移先于读偏移可见。

### 4.3 api_call 代码生成

#### 4.3.1 概念说明

设备端头文件只是「函数定义」，真正被编进融合 kernel 的是 `IndirectLoadRegApiCall` 打印出的「调用语句」。这个生成器是三模板合一的：

- **SK 模板**（`kIndirectLoadSK`）：走 `AscendC::IndirectLoadSk`，UB 缓冲 + 标量逐元素路线（本讲不展开，作为对比基线）；
- **SIMD 模板**（`kIndirectLoadSimd`）：打印 `IndirectLoadSimd` 或 `IndirectLoadSimdGatherApi` 调用；
- **SIMT 模板**（`kIndirectLoadSimt`）：不打印「一句调用」，而是**生成一整套结构体**——GM 指针 Context、内联标量链 FusedBody（Index/Output 两个求值器）、再打印 `IndirectLoadSimt` 调用。

模板 id 来自调度期（u6-l4 的 `indirect_load_schedule_case_generator` 枚举候选时写入节点属性），codegen 只是消费方。

#### 4.3.2 核心流程

```
ParseAttr
  ├─ 读 axis 属性、GetTemplateAxes / GetTemplateLogicalView / GetImplementation
  ├─ 校验 template_id ∈ {SK, SIMD, SIMT}
  └─ 若 SIMT → ParseSimtAttr：收集 index 链/输出链节点、GM 张量、定位 Store 或 post-Reduce

Generate（分发）
  ├─ SK / SIMD → tensor 版 Generate → GenerateSk / GenerateSimd（打印一句设备函数调用）
  └─ SIMT → 无 tensor 版 Generate → GenerateSimt
        ├─ GenerateFuncDefinition（函数定义阶段）：BuildSimtCodegenPlan + 生成 Context/FusedBody 结构体
        └─ GenerateSimt（语句阶段）：定位 block-inner 轴变量 → 打印 IndirectLoadSimt 调用
```

SIMT 的 codegen 计划（`SimtCodegenPlan`）推导顺序：

```
strided = input 或 index 布局非 dense
structured = 三张量均 dense 且 index/output 同形、input 非 axis 维同形
static_spans = 内层跨距/axis 跨距全是编译期常量
policy = strided ? kStrided
        : structured ? (pow2 ? kStaticPowerOfTwo : inner_pow2 ? kStaticInner
                      : magic 可表示 ? kStructuredMagic : kRecursive)
        : kRecursive（默认）
offset_type = 三张量最大偏移都 ≤ uint32 上限 且 各魔数约束满足 ? "uint32_t" : "uint64_t"
```

#### 4.3.3 源码精读

**（1）属性解析与模板路由**

`ParseAttr` 读 axis、模板轴与逻辑视图，校验 template id 三选一；SIMT 走更重的 `ParseSimtAttr`：

[autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp:L596-L621](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp#L596-L621)

`ParseSimtAttr` 分两条路：有 post-Reduce 消费者时输出留在 UB（`has_post_reduce_`），否则向后追到唯一 Store、输出直接落 GM：

[autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp:L623-L656](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp#L623-L656)

**（2）SIMT 计划推导**

`BuildSimtCodegenPlan` 是策略选择的 host 侧镜像：先按符号表达式算 inner_span/output_axis_span，再依 strided/structured/static_spans 三级条件选 policy，最后综合各魔数位宽约束决定 offset 类型：

[autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp:L257-L297](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp#L257-L297)

其中「structured」的判定要求三张量 dense 且同形（axis 维除外）：

[autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp:L208-L221](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp#L208-L221)

**（3）SK/SIMD 的调用打印**

tensor 版 `Generate` 明确只服务 SK 与 SIMD 两个模板，SIMT 由无 tensor 版重载接管：

[autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp:L708-L727](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp#L708-L727)

`GenerateSimd` 打印的关键内容：dense 布局时按「actual_size、output offset、input_actual_size、axis size、index sizes、input strides」传参；strided 布局时换成 tmp 缓冲加三组 size/stride（3×Rank）；若调度期选了 `Implementation::kGatherApi` 且布局 dense，则改调 `IndirectLoadSimdGatherApi`：

[autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp:L776-L828](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp#L776-L828)

`BuildTensorWindowInfo` 负责把调度期的逻辑视图整理成 sizes/strides，其中对「合轴后向量步长为 1」的维度做了防塌缩修正（注释解释了 `[s6, s7]` 会被折叠成 `[1, 1]` 的风险）：

[autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp:L93-L130](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp#L93-L130)

**（4）SIMT 的结构体生成**

`GenerateFuncDefinition` 为 SIMT 生成两个 struct：Context（持有 index/输出链涉及的 GM 指针）与 FusedBody（含 `Index`、`Output` 两个 `__simt_callee__` 静态求值器）：

[autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp:L658-L706](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp#L658-L706)

求值器本体由 `GenerateSimtEvaluator` 按拓扑序展开标量链：每个节点经 `AscIrCodegenV2::GenerateSimtScalarExpr` 打印一行标量表达式，GM 输入直接变成 `context.gm_<id>[output_index]`：

[autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp:L526-L570](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp#L526-L570)

进入 SIMT 区域的节点有严格门禁：只允许 GM Load、Store 和 SIMT 内联变换，且变换必须走标量发射而非 VectorFunc：

[autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp:L452-L461](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp#L452-L461)

**（5）SIMT 调用打印**

`GenerateSimtInvocation` 拼出 `AscendC::IndirectLoadSimt<...>` 完整模板实参（值 dtype、输出 dtype、FusedBody、Policy 类型），实参里区分 post-Reduce（UB 输出，带向量化元素数与 block 偏移换算）与直落 GM（`outer_tb_var_loop_size` 计元素数）两种形态：

[autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp:L830-L865](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp#L830-L865)

`GenerateSimt` 外层先在 current_axis 里找到从 outer_axis 派生的 block-inner 轴变量（找不到直接断言失败——SIMT 必须有多核切分上下文），再打印 GM 指针转换、Context 初始化与调用：

[autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp:L867-L899](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp#L867-L899)

最后，生成器经工厂自注册（u8-l3 讲过的 `ApiCallRegister` 机制）：

[autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp:L901-L902](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.cpp#L901-L902)

**（6）公共元数据：模板角色与布局种类**

调度期与 codegen 期共享的词汇表在 `autofuse/common/indirect_load_utils.h`：`TemplateRole` 标出节点在模板中的角色（SIMD 输入预处理、SIMT 边界、SIMT 内联变换、SK 等），`Implementation` 区分默认 SIMD 与 GatherApi 变体，`IndirectLoadLayoutKind` 给出 dense/zero-stride-compact/strided/unsupported 四类布局：

[autofuse/common/indirect_load_utils.h:L26-L43](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/common/indirect_load_utils.h#L26-L43)

[autofuse/common/indirect_load_utils.h:L70-L80](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/common/indirect_load_utils.h#L70-L80)

#### 4.3.4 代码实践

**实践目标**：追踪一次完整的选择与生成链路，验证「policy 如何依据形状落到具体 C++ 类型」。

**操作步骤**：

1. 读 UT `autofuse/tests/ut/codegen/api_call/test_codegen_indirect_load_api_call.cpp` 的 `BuildSimdGraph`（[L70-L120](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/tests/ut/codegen/api_call/test_codegen_indirect_load_api_call.cpp#L70-L120)），注意它是用符号尺寸 `s0..s3`（动态 shape）建图的。对照 `BuildSimtCodegenPlan` 回答：动态 shape 下 `TryGetStaticSpans` 会成功吗？policy 落到哪一档？
2. 把图中 `s0..s3` 换成常量（UT 文件里提供了常量版构造函数 `ILTestGraph(name, size0, size1, size2, size3)`），再推一遍 policy 与 `offset_type`。
3. 运行该 UT（命令参照 u12-l1：`sh build.sh -u autofuse_framework`，过滤 `test_codegen_indirect_load_api_call`），在断言或 dump 输出中确认生成的调用语句里出现的模板类型与你的推导一致。

**需要观察的现象 / 预期结果**：

1. 动态 shape 下 `sizes` 是符号表达式，`TryGetNonNegativeConst` 失败 → `static_spans=false` → 若三张量 dense 同形则 policy 落 **kStructuredMagic**（运行期魔数除法），否则落 **kRecursive**。
2. 常量形状且内层跨距为 2 的幂时升到 **kStaticInner** 甚至 **kStaticPowerOfTwo**；偏移上限决定 `uint32_t/uint64_t`。
3. 步骤 3 的运行结果**待本地验证**（需要完整构建环境）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 SIMT 模板需要在 `GenerateFuncDefinition`（函数定义阶段）生成 struct，而 SIMD 只需在语句阶段打印一句调用？

**答案**：`IndirectLoadSimt` 的模板参数里包含 `FusedBody`——index 链与输出链的内联标量变换体，它是一个完整的 struct 定义（Context + 两个求值器），必须先在 kernel 源文件里定义好才能被引用。SIMD 的 `IndirectLoadSimd/IndirectLoadSimdGatherApi` 模板参数只有 dtype、Rank、Axis 等基本量，不需要新生成类型，一句调用即可。

**练习 2**：`GetSimtPolicyArgs` 对 `kStaticPowerOfTwo` 返回空串、对 `kStaticInner` 只传一个参数、对 `kStructuredMagic` 传四个，为什么参数个数不一样？

**答案**：参数个数与「多少跨距是编译期常量」一一对应。kStaticPowerOfTwo 的全部四个跨距（InnerSpan/OutputAxisSpan/InputAxisStride/InputAxisSpan）都是模板参数（[indirect_load_simt.h:L84-L100](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simt.h#L84-L100)），运行期零参数；kStaticInner 只有 axis 跨距是运行期值，构造函数收一个参数算魔数；kStructuredMagic 的四个跨距全运行期，构造时逐一算 `(magic, shift)`；kRecursive/kStrided 则要传完整的形状/步长序列。

**练习 3**：若 SIMT 区域里混入了一个 `VectorFunc` 节点会发生什么？

**答案**：`ValidateSimtRegionNode` 会断言失败（`IndirectLoad SIMT transform must use scalar emission`）。SIMT 求值器按每线程一个元素展开标量表达式，向量算子既无法在标量语义下发射、也没有对应的 `GenerateSimtScalarExpr` 实现，所以调度期就必须把这类节点挡在 SIMT 区域之外（`TemplateRole::kSimtInlineTransform` 只放行内联变换）。

## 5. 综合实践

**任务**：以「为一个新场景选模板」为主线，把本讲三个模块串起来。

场景：`weight`（fp32，shape 静态 `[4096, 128]`，dense）按 `idx`（int32，shape `[1024, 128]`）在 axis=0 查表，查出的行还要逐元素乘一个 GM 标量因子后直接写回 GM 输出。

1. **选模板**：输出链是「逐元素标量变换 + 直落 GM」，没有后续 vector 融合——这正是 SIMT 直通 GM 管线的目标场景（`TemplateRole::kSimtDirectGmBoundary`/`kSimtInlineTransform`）。写出你排除 SIMD 的理由（提示：SIMD 需要先整段搬 UB，变换还要再起 vector 计算）。
2. **推 policy**：按 `BuildSimtCodegenPlan` 手推：inner_span=128、output_axis_span=1024×128=131072、input_axis_stride=128、input_axis_span=4096×128，全为常量且 128/131072 均为 2 的幂 → `kStaticPowerOfTwo`；三张量最大偏移 ≤ 4096×128 远小于 uint32 上限 → `offset_type="uint32_t"` → 线程数可到 2048。
3. **写生成代码预期**：参照 `GenerateSimtEvaluator` 的展开方式，手工写出 `IndirectLoadSimtBody_...::Output` 求值器的伪代码（`context.gm_x[...]` 取数、乘因子、返回），再对照 `GenerateSimtInvocation` 写出调用语句骨架。
4. **验证**：在 `autofuse/tests/v35/st/backend_e2e_v2/indirect_load_store_test/` 下找最接近的 e2e 用例（`indirect_load_store_backend_generator.cpp` 等）作为参照，说明你会如何新增一个用例（改 generator 建图 + 加 cmake 用例清单）。上板对比 SIMD/SIMT 两个候选的实际耗时**待本地验证**。

## 6. 本讲小结

- IndirectLoad 的地址翻译统一为「定位 index 元素 → 取值乘 input_axis_stride → 补 axis 外坐标偏移」，SIMD 在向量寄存器里按 lane 并行算，SIMT 由每个标量线程独立算。
- SIMD 侧由三层正交策略（IndexPolicy 按 index dtype、ValuePolicy 按 dtype 位宽、AddressMode 按布局）组合，派发树按「末维直取 → 内层连续（2 的幂位与/通用取模/Reuse 特化）→ 逐维 DivMod」从便宜到贵排序；16 位大输入靠 65536 滑动窗口保正确性。
- SIMT 侧五种 AddressPolicy 按特化程度递减（静态 2 的幂 → 静态内层 → 结构化魔数 → 递归 → 跨步），核心技巧是把运行期除法换成魔数乘法+移位；线程数按 actual_size 分档，2048 线程仅限 32 位偏移。
- SIMT 的独特价值是 GM 直通与标量链内联：index 链和输出链的变换被生成为 `FusedBody::Index/Output` 求值器，与访存融合在一次线程执行里；进入该区域的节点受 `ValidateSimtRegionNode` 严格门禁。
- codegen 侧 `IndirectLoadRegApiCall` 三模板合一：SK/SIMD 打印一句设备函数调用（dense 与 strided 参数形态不同，GatherApi 是变体），SIMT 则额外生成 Context/FusedBody 结构体并按 `BuildSimtCodegenPlan` 选定 policy 模板实参与偏移类型。
- 模板 id 与逻辑视图由调度期（u6-l4 的 case generator）写入节点属性，`autofuse/common/indirect_load_utils.h` 中的 `TemplateRole/Implementation/IndirectLoadLayoutKind` 是两期共享的词汇表。

## 7. 下一步学习建议

- 回读 u6-l4 的 `indirect_load_schedule_case_generator.cpp`，把「候选枚举 + `BuildFinalTensorView`」与本讲的「模板消费」拼成完整闭环，重点关注 `TemplateRole` 是如何被逐节点打上的。
- 阅读 e2e 用例 `autofuse/tests/v35/st/backend_e2e_v2/indirect_load_store_test/` 下的四个 generator（store/broadcast/stride_zero/torch_gather_strided），对照 `IndirectLoadLayoutKind` 理解每类布局来自什么样的框架算子。
- 结合 u12-l1 学过的测试调度方式，实际跑一遍 indirect_load 相关 UT/e2e，并用 `AUTOFUSE_DFX_FLAGS`（u3-l3）dump 出生成的 kernel 源码，验证本讲推导的 policy 选择。
- 若关注性能，可衔接 u7 系列的 ATT 建模：IndirectLoad 候选的取舍目前在调度侧靠合法性门禁（打分恒 0 占位，见 u6-l4），可思考若为其建立 cost model 应度量哪些量（窗口循环次数、DivMod 次数、线程数档位）。
