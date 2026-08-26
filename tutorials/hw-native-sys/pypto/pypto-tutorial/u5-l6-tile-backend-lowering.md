# Tile 后端降级链：从「能跑的 Tile 代码」到「硬件友好的 2D 指令序列」

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `tile_pto_passes` 这一段流水线（第 12~17 个 Pass）各自负责什么，以及它们为什么必须按这个顺序排。
2. 理解**复合算子拆解**（LowerCompositeOps）：为什么 `tile.sin` 必须在代码生成前被拆成 `tile.muls`/`tile.adds` 等原语，以及 Cody-Waite 多项式降级的基本形态。
3. 掌握**高维 Tile 摊平**（FlattenTileNdTo2D）：PTO-ISA 只接受 2D Tile 这一硬件约束，`[A,B,C,D] → [A·B·C, D]` 的合并规则，以及 `tile.batch_matmul_acc` 被逐批次展开的方式——特别是 **`init_cond` 谓词如何被逐行带地（verbatim）透传**到每个展开出的 `tile.matmul_acc`。
4. 理解**自动矩阵乘 L0 分块**（AutoTileMatmulL0）：K-loop + 两级流水 + 谓词化累加体的骨架，以及**行收窄累加器为何要在 `tile.create(compact=True)` 种子上声明 compact 模式**。
5. 知道收尾两步（CanonicalizeTileSlice 折叠 `tile.slice`、InferTileMemorySpace 推断内存空间）在整个降级链中的位置与分工。

本讲承接 u5-l5（Tensor 到 Tile 的降级）：上一讲结束时，Tensor 级算子已经被 `ConvertTensorToTileOps` 改写成 `tile.*` 调用；本讲从「已经全是 Tile 算子的 IR」继续往下走，直到 IR 形态贴近目标指令集。

## 2. 前置知识

- **复合算子（composite op）与原语（primitive op）**：原语是能一一映射到片上指令的算子（如 `tile.muls`、`tile.add`）；复合算子是"高层便利写法"（如 `tile.sin`、分布式集合通信 `pld.tensor.allreduce`），硬件没有对应指令，必须在编译期拆成原语序列。
- **Tile 的秩（rank）与物理形状**：Tile 是片上固定尺寸数据块（u1-l4、u2-l4 已建立）。DSL 允许 Tile 跟随张量拥有 3D/4D 形状，但硬件上的片上缓冲是**二维**的——这就是本讲摊平 Pass 存在的根本原因。
- **valid_shape 与物理形状**：物理形状必须是编译期常量（硬件缓冲按物理尺寸分配），有效区 `valid_shape` 表示物理盒内真正有数据的子矩形，可以是运行期值（u2-l4、u4-l4 已建立）。
- **矩阵乘的四级存储链**：GM → Mat（L1）→ Left/Right（L0A/L0B）→ Acc（L0C），`tile.matmul` 结果的浮点 dtype 由累加器固定为 FP32（u2-l4 已建立）。L0A/L0B/L0C 容量有限，所以需要"分块"。
- **init_cond 谓词**：累加算子族（`matmul_acc`/`gemv_acc`/`batch_matmul_acc`）可选的第四个操作数，BoolLike；谓词为真时**覆写**累加器而非累加，是 split-K 的 `k == 0` 惯用法（u2-l4、u4-l6 已建立：因为谓词可依赖循环变量、必须进 use-def 链，所以注册为操作数而非 kwarg）。
- **`dump_passes`**：编译时打开逐 Pass IR 导出，会在输出目录生成 `passes_dump/`，每个 Pass 前后各一份 IR 文本，是本讲所有实践的观察手段（u3-l5 已建立）。

## 3. 本讲源码地图

| 文件 | 作用 |
| ---- | ---- |
| [python/pypto/ir/pass_manager.py](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/pass_manager.py#L176-L186) | `tile_pto_passes` 元组：本讲六个 Pass 的执行顺序唯一来源 |
| [src/ir/transforms/lower_composite_ops_pass.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/lower_composite_ops_pass.cpp) | Pass 12：复合算子拆解（sin/cos 多项式、分布式集合通信） |
| [src/ir/transforms/flatten_tile_nd_to_2d/](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/flatten_tile_nd_to_2d/pass.cpp) | Pass 13：按职责拆成 pass/analysis/rewrite/rewrite_utils/batch_matmul/transpose/verification 七个文件 |
| [src/ir/transforms/flatten_tile_nd_to_2d/batch_matmul.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/flatten_tile_nd_to_2d/batch_matmul.cpp) | Pass 13 中 `tile.batch_matmul[_acc]` 的逐批次展开（含 init_cond 透传） |
| [src/ir/transforms/op_conversion_registry.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/op_conversion_registry.cpp) | Pass 10 用的张量→Tile 转换表；`tensor.matmul_acc` 在这里决定走 2D 还是 batch 形式 |
| [src/ir/transforms/auto_tile_matmul_l0_pass.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/auto_tile_matmul_l0_pass.cpp) | Pass 15：L0 分块、K-loop、谓词化累加体、compact 累加器种子 |
| [src/ir/transforms/canonicalize_tile_slice_pass.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/canonicalize_tile_slice_pass.cpp) | Pass 16：`tile.slice` → `tile.extract` 规范化 |
| [src/ir/transforms/infer_tile_memory_space_pass.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/infer_tile_memory_space_pass.cpp) | Pass 17：内存空间（Vec/Mat/Acc 等）推断与 `tile.move` 插入 |
| [docs/en/dev/passes/12-lower_composite_ops.md](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/12-lower_composite_ops.md) / [13-flatten_tile_nd_to_2d.md](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/13-flatten_tile_nd_to_2d.md) / [15-auto_tile_matmul_l0.md](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/15-auto_tile_matmul_l0.md) | 三个 Pass 的官方文档（英文权威版） |
| tests/ut/ir/transforms/test_lower_composite_ops.py、test_flatten_tile_nd_to_2d.py、test_auto_tile_matmul_l0.py | 三个 Pass 的结构化 before/after 测试 |

先看顺序的唯一来源。[pass_manager.py:L176-L186](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/pass_manager.py#L176-L186) 定义了 `tile_pto_passes`：`lower_composite_ops` → `flatten_tile_nd_to_2d` → `legalize_tile_cast`（第 14 个，本讲不展开）→ `auto_tile_matmul_l0` → `canonicalize_tile_slice` → `infer_tile_memory_space`。这个顺序的逻辑是：**先拆掉硬件没有的算子（12），再把形状规范化成硬件唯一接受的 2D（13），然后才能在统一的 2D 形态上做容量驱动的分块决策（15），最后规范化数据搬运形式（16）并补齐内存空间（17）**。顺序颠倒任何一个都会出错——比如先分块再摊平，分块器就得同时处理任意秩的形状。

## 4. 核心概念与源码讲解

### 4.1 复合算子拆解：LowerCompositeOps（第 12 个 Pass）

#### 4.1.1 概念说明

代码生成器（u6 将精读）的职责是把每个 `tile.*` 算子映射成 PTO 虚拟指令。如果 IR 里存在硬件没有对应指令的"复合算子"（`tile.sin`、`tile.cos`、`pld.tensor.*` 集合通信），代码生成器就得为每个算子各写一套展开逻辑——这会把高层语义泄漏进后端。LowerCompositeOps 的设计选择是：**在进入 Tile 形态规范化之前，把所有复合算子拆成纯原语组合**，让代码生成器永远只面对原语。

这个 Pass 是"查表 + 替换"模式的标准范本：新增一个复合算子只需要写一个规则函数、在表里加一行，分发器本身零改动。

#### 4.1.2 核心流程

```text
对函数内每条 var = Call(...) 赋值语句:
  1. 用算子名查 kRules 分发表
  2. 查不到 → 原样返回（结构性 no-op）
  3. 查到 → 调用规则函数 Lower<Op>Rule(call, args, builder)
     - 规则用 LoweringBuilder 逐条追加原语语句（Bind 一个临时 Var）
     - 最终结果绑定回原目标 Var（下游引用名字不变）
  4. 用生成的 SeqStmts 替换原语句
```

以 `tile.sin` 为例，拆解是**固定形状的原语菜谱**（约 33 条语句）：

1. **范围归约**（Cody-Waite 四段 π 分割）：把 \(x\) 写成 \(x = k\pi + t\)，\(t \in [-\pi/2, \pi/2]\)。FP32 无法精确表示 π，单次 `x - k·π` 的相对误差会随 \(|k|\) 线性放大，所以把 π 拆成
   \[ \pi \approx \pi_{V2} + \pi_{C1} + \pi_{C2} + \pi_{C3} + \pi_{C4} \]
   每段做一次减法，让灾难性抵消只发生在最细的尺度上。
2. **符号计算**：无分支地算出 \((-1)^k\)，用恒等式 \(\lfloor k/2 \rfloor \cdot 4 - 2k + 1\)（写 \(k = 2m + r\)，代入得 \(1 - 2r\)，即偶 \(k\) 为 \(+1\)、奇 \(k\) 为 \(-1\)）。
3. **Horner 多项式**：9 次奇多项式 \(t \cdot P(t^2)\) 逼近 \(\sin t\)，\(P(u) = (((R_0 u + R_1)u + R_2)u + R_3)u + 1\)。

#### 4.1.3 源码精读

分发表与查找函数在 [lower_composite_ops_pass.cpp:L2207-L2221](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/lower_composite_ops_pass.cpp#L2207-L2221)：`kRules` 是算子名到规则函数的静态映射，`tile.sin`/`tile.cos` 加上七个 `pld.tensor.*` 集合通信各占一行；查不到返回 `nullptr`。

[lower_composite_ops_pass.cpp:L2236-L2259](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/lower_composite_ops_pass.cpp#L2236-L2259) 是分发器 `LowerCompositeOpsMutator`：它重写的是 **`VisitStmt_(AssignStmt)` 而非 `VisitCall`**——因为一次三角函数展开要往周围语句序列里拼接约 33 条语句，每条都需要一个新临时 `Var`；在 `VisitCall` 里做这件事需要"一次返回多个表达式"，`IRMutator` 不支持。拿到规则后，先对实参做变量重映射（`VisitArgs`），再交给规则 + `LoweringBuilder`（类定义在 [L250](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/lower_composite_ops_pass.cpp#L250)），最终把结果绑回原目标 `Var`，下游使用者的名字与身份不变。

`LowerSinCos`（[L756](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/lower_composite_ops_pass.cpp#L756)，规则入口 `LowerSinRule` 在 [L814](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/lower_composite_ops_pass.cpp#L814)）用一个 `is_cos` 参数同时服务 sin 与 cos——两者共用同一个 Horner 多项式，差别只在范围归约（cos 的 k 取整偏移 0.5，且中途加 π/2）。常量表（`PI_V2`、`R0`~`R3` 等 FP32 字面量）与逐语句菜谱在 [docs/en/dev/passes/12-lower_composite_ops.md:L146-L177](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/12-lower_composite_ops.md#L146-L177) 有完整对照表。

两个值得记住的性质（文档 [L13](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/12-lower_composite_ops.md#L13)、[L185-L187](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/12-lower_composite_ops.md#L185-L187)）：

- **结构性 no-op**：程序里没有注册过的复合算子时，所有语句原样穿过。
- **幂等**：拆解产物只含原语（`tile.muls`/`tile.adds`/`tile.add`/`tile.sub`/`tile.mul`/`tile.cast` 等），而分发器只重写注册的复合调用，跑第二遍什么也不改。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到一条 `tile.sin` 被拆成一串原语。
2. **操作步骤**：
   - 写一个最小 InCore 算子，片上对 FP32 Tile 调 `pl.sin`（`pl.*` 统一入口按操作数类型分发到 `tile.sin`，见 u2-l3）；
   - 用 `ir.compile(..., dump_passes=True)` 编译，打开输出目录的 `passes_dump/`；
   - 找到 `lower_composite_ops` 的 before/after 两份 IR 文本，做文本 diff。
3. **需要观察的现象**：before 里的一行 `tile.sin` 在 after 里变成约 33 条赋值，全是 `tile.muls` / `tile.adds` / `tile.add` / `tile.sub` / `tile.mul` / `tile.cast`（cast 的 mode 为 `round`/`rint`/`floor`）；原目标变量名保留在最后一条赋值的左侧。
4. **预期结果**：after 中不再出现 `tile.sin` 字样；常量 `0.31830988…`（1/π 头部）与 `3.140625`（π 头部）出现在菜谱前几条。参考测试 [tests/ut/ir/transforms/test_lower_composite_ops_numerical.py](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/transforms/test_lower_composite_ops_numerical.py)（NumPy 对照，绝对误差 ≤ ~1e-5）。dump 目录内各 Pass 文件的具体命名以本地运行为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么这个 Pass 重写 `VisitStmt_(AssignStmt)` 而不是 `VisitCall`？
答案：一次拆解要向周围语句序列拼接约 33 条语句、每条一个新临时 `Var`；`IRMutator` 的表达式访问器一次只能返回一个表达式，无法表达"一条语句变多条"。语句级重写可以直接返回 `SeqStmts`。

**练习 2**：sin 与 cos 共用一个 Horner 多项式，为什么是安全的？
答案：两者的差别只在范围归约——到 `t` 进入多项式时，sin 与 cos 的 `t` 都已落在 \([-\pi/2, \pi/2]\)，同一组系数在该区间上同时对两者达到逼近精度，无需两套系数。

**练习 3**：如果把一个新的复合算子 `tile.exp_approx` 加入这个 Pass，需要改动分发器吗？
答案：不需要。只需写一个 `LowerExpApproxRule(call, args, builder)` 规则函数，并在 `kRules` 表里加一行 `{"tile.exp_approx", &LowerExpApproxRule}`（[L2208-L2218](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/lower_composite_ops_pass.cpp#L2208-L2218) 的既定模式）。

### 4.2 高维 Tile 摊平：FlattenTileNdTo2D（第 13 个 Pass）

#### 4.2.1 概念说明

PTO-ISA 只接受 2D Tile——片上缓冲（UB/L0A/L0B/L0C）在硬件上就是二维的。但 DSL 里 Tile 可以跟随张量拥有 3D/4D 形状（例如对一个 `[2, 3, 4]` 张量做 `pl.load` 得到 `[2, 3, 4]` 的 Tile）。这个 Pass 把所有秩大于 2 的 Tile 操作摊平成 2D：**合并除最后一维外的所有维度**，即

\[ [D_0, D_1, \ldots, D_{k-1}, D_k] \rightarrow [D_0 \cdot D_1 \cdots D_{k-1},\ D_k] \]

例如 `[2, 3, 4]` 变成 `[6, 4]`。为什么保留最后一维？因为硬件 Tile 的"行/列"语义、分形布局（fractal）与转置都是围绕最后一维（列连续方向）定义的；最后一维保持不变，摊平才是保序的行主序重排。

对**批次矩阵乘**，摊平有更深一层的含义：`ConvertTensorToTileOps`（pass 10）先把高秩矩阵乘的"高层意图"保留为 `tile.batch_matmul` / `tile.batch_matmul_acc`，让本 Pass 成为**唯一的规范化展开点**——把 `acc[B, M, N] += lhs[B, M, K] @ rhs[B, K, N]` 展开成逐批次的 2D `tile.matmul` / `tile.matmul_acc`。

**本次版本更新（重点）**：`tile.batch_matmul_acc` 现在会把它可选的 `init_cond` 谓词**原样透传**给每一个展开出的 `tile.matmul_acc`。在这之前，上游 `tensor.matmul_acc` 携带 `init_cond` 且操作数秩大于 2 时会被直接拒绝（提示改用 2D 操作数循环）；现在两条路径都放行。

#### 4.2.2 核心流程

Pass 分三阶段（每个 InCore 类函数：InCore/AIC/AIV；编排与 Opaque 函数原样返回）：

```text
1. analysis（前置校验，只读）:
   - 每个 Tile 的物理形状必须是静态 ConstInt（valid_shape 允许动态）
   - 归约算子必须沿最后一维
   - >2D 的 tile.read/write/slice、不能连续塌缩的 >2D tile.assemble → 拒绝
2. rewrite（逐语句改写）:
   - tile.load >2D        → 结果 Tile 重建为 2D；自然 Nz Mat 装载还插入 shape-only 的 2D tensor.view
   - tile.store           >2D 张量时注入原张量秩的 shapes 作为第 4 操作数供后端重建分区
   - tile.create/tile.full >2D → 直接用摊平后的 2D 形状重建
   - tile.batch_matmul[_acc] → 逐批次展开（见 4.2.3）
   - 其他 >2D 算子        → 变量替换后按 2D 类型重建调用
   - 1D/2D 算子           → 不动
3. verification（后置校验）: TileOps2D 属性验证器独立复核只剩合法秩
```

**init_cond 透传的推理**（本次新增代码的注释原文逻辑）：`init_cond` 在批次展开中是**循环不变量**——每个展开出的 `tile.matmul_acc` 是累加器中自己那条行带（row band）的**唯一写者**，所以"覆写而不是累加"逐带成立，谓词应当逐字转发；如果悄悄丢弃，`k == 0` 那一步就会向一个未初始化的累加器做累加。

#### 4.2.3 源码精读

**合并规则**。[rewrite_utils.cpp:L74-L88](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/flatten_tile_nd_to_2d/rewrite_utils.cpp#L74-L88) 的 `ComputeMergedShape` 就是上面公式的实现：除最后一维外逐维相乘（带正数与溢出检查），返回 `{merged, last}`。与之配套的 `ComputeMergedValidShape`（[L110-L117](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/flatten_tile_nd_to_2d/rewrite_utils.cpp#L110-L117)）合并 `valid_shape` 时**容忍动态表项**（静态因子折叠成一个 ConstInt，恒等因子 1 丢弃），所以动态有效区（如 `min(CHUNK, s - c)` 尾巴）能活着穿过摊平，而不是被重置成物理全尺寸。

**通用摊平**。以 `tile.create`/`tile.full` 为例，[rewrite.cpp:L865-L893](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/flatten_tile_nd_to_2d/rewrite.cpp#L865-L893)：结果 Tile 秩大于 2 时，取摊平后的 `{merged, last}` 重建形状元组，其余实参做变量替换后用注册表重新 `Create`（类型推断重跑），≤2D 直接穿过。`tile.load` 的结果摊平在 [rewrite.cpp:L695](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/flatten_tile_nd_to_2d/rewrite.cpp#L695) 附近，同样调用 `ComputeMergedShape`。

**batch_matmul_acc 的展开与 init_cond 透传**（本次更新核心）。[batch_matmul.cpp:L758-L791](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/flatten_tile_nd_to_2d/batch_matmul.cpp#L758-L791) 是 `LowerBatchMatmulAcc` 的入口：校验累加器（acc）在摊平后是 2D 后，第 781-791 行构造透传闭包——

```cpp
// batch_matmul.cpp:786-791（真实源码）
const ExprPtr init_cond = call->args_.size() == 4 ? Substitute(call->args_[3], ctx.var_map) : nullptr;
auto make_matmul_acc = [&](const ExprPtr& acc, const ExprPtr& lhs, const ExprPtr& rhs) {
  std::vector<ExprPtr> mm_args = {acc, lhs, rhs};
  if (init_cond) mm_args.push_back(init_cond);
  return op_registry.Create("tile.matmul_acc", mm_args, span);
};
```

四操作数（带谓词）时取出 `args_[3]` 做变量替换后，**所有**展开点统一经 `make_matmul_acc` 造调用，谓词无一遗漏。展开有两条路径：

- **快路径（batch_count == 1，[L871-L894](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/flatten_tile_nd_to_2d/batch_matmul.cpp#L871-L894)）**：批次维乘积为 1 时累加器本来就是 `[M, N]`，逐批切片是恒等变换，直接发一条 `tile.matmul_acc`（[L889](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/flatten_tile_nd_to_2d/batch_matmul.cpp#L889) 的 `make_matmul_acc`），完全绕开 Acc 内存里的 slice/assemble。
- **通用路径（[L896-L925](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/flatten_tile_nd_to_2d/batch_matmul.cpp#L896-L925)）**：对每个批次 `i` 发 `tile.slice`（第 `i * M` 行起的行带，[L912-L916](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/flatten_tile_nd_to_2d/batch_matmul.cpp#L912-L916)）→ `tile.matmul_acc`（[L918](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/flatten_tile_nd_to_2d/batch_matmul.cpp#L918)）→ `tile.assemble` 写回同一行带（[L922-L924](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/flatten_tile_nd_to_2d/batch_matmul.cpp#L922-L924)），SSA 链式更新 `current_acc`。注意：通用路径结构上完整，但按上游注释（见下）批次乘积大于 1 的累加形最终仍不可用——每批累加器是 L0C 里的跨步行窗口，MAD 无法寻址。

**上游的放行决策**。谓词能走到 batch 形式，前提是 pass 10 的转换表放行。[op_conversion_registry.cpp:L1128-L1149](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/op_conversion_registry.cpp#L1128-L1149) 的 `tensor.matmul_acc` 转换：任一操作数秩大于 2 就选 `tile.batch_matmul_acc`，否则 `tile.matmul_acc`；关键在第 1140-1146 行的注释与一行代码——`init_cond rides along on both forms`：四实参时无条件把谓词附到输出上。注释还说明了唯一保留的限制：**批次乘积大于 1 的拒绝发生在本 Pass 之后，且原因与 init_cond 无关**（每批累加器是 MAD 无法寻址的跨 stride L0C 行窗口）。对比上一版本：这里原来是一个 `CHECK_SPAN(!nd, ...)` 硬拒绝，要求"对批次维循环、用 2D 操作数"。

> 一个诚实的提醒：Python 侧文档串还没跟上这次变化——[python/pypto/language/op/tensor_ops.py:L697-L698](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/tensor_ops.py#L697-L698) 仍写着 "Only 2D operands support the predicate; loop over the batch dimension instead"。C++ 转换层与摊平层已经放行（且 `src/ir/op/tensor_ops/matmul.cpp` 的类型推断只要求操作数 ≥ 2D），所以"带谓词 + 高秩操作数"在当前 HEAD 能走通到 batch_count == 1 的快路径；但文档串的建议（批次维乘积为 1 或自行循环）仍是实践中最稳妥的写法。

**Pass 属性**（[docs/en/dev/passes/13-flatten_tile_nd_to_2d.md:L201-L207](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/13-flatten_tile_nd_to_2d.md#L201-L207)）：要求 SSAForm/IncoreTileOps/NormalizedStmtStructure，产出 TileOps2D（新增）并保持前两者——这正对应 u5-l1 讲过的"produced 且已验即跳过"的属性机制。

#### 4.2.4 代码实践

1. **实践目标**：把文档里的经典例子亲手跑一遍，看 `[2, 3, 4] → [6, 4]`。
2. **操作步骤**（示例代码，基于 [docs/en/dev/passes/13-flatten_tile_nd_to_2d.md:L113-L137](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/13-flatten_tile_nd_to_2d.md#L113-L137) 的 before/after 用例改写）：

   ```python
   # 示例代码：3D Tile 加法，观察 FlattenTileNdTo2D 的摊平
   import pypto as pl
   import torch
   from pypto import ir

   @pl.jit
   def add3d(x: pl.Tensor[[2, 3, 4], pl.FP32]) -> pl.Out[pl.Tensor[[2, 3, 4], pl.FP32]]:
       with pl.at(level=pl.Level.CORE_GROUP):
           t: pl.Tile[[2, 3, 4], pl.FP32] = pl.load(x, [0, 0, 0], [2, 3, 4])
           y = pl.add(t, t)
           pl.store(y, [0, 0, 0])
   ```

   - 先 `kernel.compile(...)`（或首次调用触发编译）并带 `dump_passes`，然后进入 `passes_dump/`；
   - 取 `flatten_tile_nd_to_2d` 的 before/after 两份文本 diff。
3. **需要观察的现象**：`tile.load` 的结果注解从 `Tile[[2, 3, 4], FP32]` 变成 `Tile[[6, 4], FP32]`；`tile.add` 的实参类型同步变 2D；`pl.store` 仍写回 `[2, 3, 4]` 的张量，且 IR 里 store 多出一个 `(2, 3, 4)` 形状元组（第 4 操作数，原张量秩分区信息，仅存在于变换后的 IR，DSL 源码不变）。
4. **预期结果**：after 中不再有任何秩大于 2 的 Tile 类型；`tile.load` 直接产出 2D Tile（不插入 `tile.reshape`）。断言式测试见 [tests/ut/ir/transforms/test_flatten_tile_nd_to_2d.py](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/transforms/test_flatten_tile_nd_to_2d.py)。

#### 4.2.5 小练习与答案

**练习 1**：为什么摊平保留最后一维而不是第一维？
答案：硬件 Tile 的行/列、分形布局（N-fractal）与转置语义都围绕最后一维（列方向、连续内存方向）定义；保留最后一维，摊平才是保序的行主序重排，`tile.load` 的源窗口、NZ 分形装载等才能继续对上硬件语义。

**练习 2**：`batch_count == 1` 为什么要专门走快路径？
答案：批次乘积为 1 时累加器已是 `[M, N]`，逐批切片是恒等变换；快路径直接发一条 `tile.matmul_acc`，避免了在 Acc 内存里做 slice/assemble——后者正是通用路径在 L0C 上不可寻址的形态（[L871-L874](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/flatten_tile_nd_to_2d/batch_matmul.cpp#L871-L874) 注释）。

**练习 3**：如果 `init_cond` 在批次展开时被丢弃（而不是透传），第一个出错的现象会是什么？
答案：`k == 0` 那一步不会覆写累加器而是向其累加，而快路径下该累加器正是未初始化的 `tile.create` 占位（或上一轮的脏值），数值结果错误——且是静默错误，不报编译错（[L781-L785](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/flatten_tile_nd_to_2d/batch_matmul.cpp#L781-L785) 注释原话："a silently dropped predicate would accumulate into an uninitialized accumulator on the k == 0 step"）。

### 4.3 自动矩阵乘 L0 分块：AutoTileMatmulL0（第 15 个 Pass）

#### 4.3.1 概念说明

摊平后的 `tile.matmul` 是全尺寸 `(M, N, K)` 操作数——几乎总是大于 Cube 单元的 L0A/L0B/L0C 容量。这个 Pass 为每个静态 2D 的 `tile.matmul` / `tile.matmul_acc` / `tile.matmul_bias` 挑一个装得进 L0 的 `(m, n, k)`，把调用改写成**两段流水的 K-loop**：循环体里用 `tile.extract` 把 `[m, k]` / `[k, n]` 的窄条从 Mat 搬进 Left/Right，累加进 Acc 驻留的 iter-arg。循环标记 `ForKind::Pipeline` 且 `pipeline_stages=2`，下游 `LowerPipelineLoops`（u6-l5）会把它落成两深度的乒乓。

挑 `(m, n, k)` 不是拍脑袋：`ChooseL0Tile` 做**穷举 roofline 搜索**——对每个合法对齐的 `(m, n, k, stationarity, dbC)` 设计点估算墙钟（L1→L0 装载带宽、MAD 发射开销、FIXPIPE 排空成本三者的 max），取最小。返回 `m == M && n == N` 时只切 K；返回 `m < M` 或 `n < N` 时输出装不下 L0C，还要把输出切成 `⌈M/m⌉ × ⌈N/n⌉` 网格。

**累加体的谓词化**（承接 u2-l4 的 init_cond）：新鲜 `tile.matmul` 的循环体是一条**带 `ko == 0` 谓词的 `tile.matmul_acc`**——第一个 L0 块覆写累加器、之后累加，于是 create/call/yield/return_var 整条链构造上就是**一个** L0C 缓冲。它取代的旧形态是剥皮的 `if ko == 0`：那会给一个逻辑值造出两个 Acc 缓冲再在 phi 处汇合——没有任何支持的目标能实现这种形态，因为读 L0C 的只有 FIXPIPE 排空，不存在 Acc→Acc 拷贝来调和两个分支。

**本次版本更新（重点）：compact 累加器种子**。当左操作数的**有效行数**无法被证明等于物理行数（行收窄，例如 valid 16 行、物理 64 行）时，种子改为 `tile.create(..., compact=True)` 声明的紧凑占位。背景（u4-l4 已建立 TileView.compact 概念）：MAD 从 L0A 操作数的**有效**行取 M，把乘积按 N-fractal 行距 \(\lceil M/16 \rceil \times 16\) 铺进 L0C；只有 compact Tile 会让读取方**重算这个行距**而不是用物理行数。`tile.matmul_acc` **继承**其累加操作数的 compact 模式——所以一个非 compact 的种子会把每一步累加、以及循环之后的 `tile.store`/`tile.tpush_to_aiv` 都拖回物理行距，第一个 N-fractal 之上全部错位（issues #2470、#2510）。

#### 4.3.2 核心流程

```text
对每个 InCore 函数内的 tile.matmul / tile.matmul_acc / tile.matmul_bias:
  1. 过滤: 实参必须是 Var/IterArg（AsVarLike）的静态 2D TileType；
     右操作数须驻留 Mat；arity 4 只对累加形合法（新鲜 matmul 没有累加器可谓词）
  2. ChooseL0Tile: 按后端容量 L0a/L0b/L0c + 代价模型穷举选 (m, n, k) 与设计点
  3. (m,n,k) == (M,N,K) → 已是 L0 尺寸，跳过
  4. 造 K-loop:
     - tile.matmul       → 种子 = tile.create([m,n], dtype, target_memory=Acc)
                           循环体 = tile.matmul_acc(c_iter, sa, sb, ko == 0)   ← 谓词化
     - tile.matmul_acc   → 种子 = 调用方累加器直接穿 iter-arg；
                           3 操作数拼写: 无谓词（累加器首轮已存活，绝不能覆写）
                           4 操作数拼写: 体 = tile.matmul_acc(..., user_cond and ko == 0)
     - tile.matmul_bias  → 头剥第一个 K 块（直线 tile.matmul_bias 初始化）+ 循环 matmul_acc
     - 每轮 tile.extract(src, r, c, shape, target_memory=Left|Right) 取窄条
  5. m < M 或 n < N → 输出网格: 每个子块独立 K-loop，store 链式 SSA 串联
  6. K 不被 k 整除 → 只循环整块，尾部宽度 K − ⌊K/k⌋·k 直线剥出（3 操作数、无谓词）
```

#### 4.3.3 源码精读

**谓词化循环体**。[auto_tile_matmul_l0_pass.cpp:L550-L559](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/auto_tile_matmul_l0_pass.cpp#L550-L559) 的 `BuildMatmulBody` 是新鲜 `tile.matmul` 的体：`MakeEq(ko_var, 0)` 造出 `ko == 0` 谓词，四实参创建 `tile.matmul_acc`，随后 yield 回 iter-arg——一条语句完成"首块覆写、后续累加"。`tile.matmul_acc` 的体在 [L568-L588](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/auto_tile_matmul_l0_pass.cpp#L568-L588) 的 `BuildMatmulAccBody`：3 操作数时不加谓词；4 操作数时在 [L573-L582](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/auto_tile_matmul_l0_pass.cpp#L573-L582) 把调用方谓词与生成的 `ko == 0` **做 AND**——语义分工在注释里写得很清楚：调用方谓词指"**用户规约的**第一个 K 步"，`ko == 0` 指"该步内的**第一个 L0 块**"，两者同时成立才覆写。实参与匹配的过滤在 [L906-L914](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/auto_tile_matmul_l0_pass.cpp#L906-L914)：`has_init_cond = is_acc && args_.size() == expected_arity + 1`——arity 4 只对累加形合法。

**compact 种子（本次更新核心）**。[L380-L391](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/auto_tile_matmul_l0_pass.cpp#L380-L391) 的 `BuildAccInit` 新增 `compact` 形参：为真时往 `tile.create` 的 kwargs 里追加 `{"compact", true}`。决策点在 [L407-L437](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/auto_tile_matmul_l0_pass.cpp#L407-L437) 的 `BuildAccInitWithValidShape`：

```cpp
// auto_tile_matmul_l0_pass.cpp:428-433（真实源码，节选）
const bool rows_narrowed =
    ProveValidExtentEqual(valid_m, MakeIndex(physical_m, span)) != ProofResult::kTrue;
auto storage = BuildAccInit(physical_m, physical_n, dtype, name_hint + "_storage", span, rows_narrowed);
...
auto narrowed_call =
    reg.Create("tile.set_validshape", {storage->var_, std::move(valid_m), std::move(valid_n)}, span);
```

`ProveValidExtentEqual` 是三值证明器（u4-l4 讲过 ProofResult 的三值语义）：只有当有效行数**被证明等于**物理行数（`kTrue`）才保持历史形态（`kUnknown` 也按收窄处理——保守正确）；否则种子带 `compact=True` 创建，再由 `tile.set_validshape` 收窄有效区，而 `set_validshape` **继承**存储的 compact 模式，于是 iter-arg、每一步向它累加的 `tile.matmul_acc`（该算子继承累加操作数的模式）、循环后的读取者三方都与硬件实际使用的行距一致。

**为什么"声明在 create 上"而不是"事后盖到类型上"**（[docs/en/dev/passes/15-auto_tile_matmul_l0.md:L202-L206](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/15-auto_tile_matmul_l0.md#L202-L206)）：紧随其后的 `InferTileMemorySpace`（pass 17）会对实参变化过的每个调用**重新推断**，丢弃 Pass 事后贴的类型精化；而 kwarg 会被推断器每次重新读取——**声明才是能活下来的东西**。产出的契约由 `AccCompactValid` 验证器守护（u5-l1 讲过它的产出/失效窗口）。

种子形态在 IR 里长这样（文档示例，[15-auto_tile_matmul_l0.md:L188-L191](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/15-auto_tile_matmul_l0.md#L188-L191)）：

```python
c_l0_init_storage = pl.tile.create([64, 128], pl.INT32, target_memory=Acc, compact=True)
c_l0_init = pl.tile.set_validshape(c_l0_init_storage, 16, 128)
```

**Pass 属性与后端中立**：Required = Produced = `SSAForm, SplitIncoreOrch, IncoreTileOps, TileOps2D, NormalizedStmtStructure`（属性保持式改写，[L321-L327](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/15-auto_tile_matmul_l0.md#L321-L327)）。所有容量/对齐（`GetL0aCapacityBytes`、`GetL0FractalAlignment`、`GetL0cMAlignment`…）都从 `PassContext::Current()->GetBackendHandler()` 读（[L289-L305](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/15-auto_tile_matmul_l0.md#L289-L305) 的对照表）——Pass 本体不知道自己在为 910B 还是 950 生成代码（u6-l3 会展开 BackendHandler）。

#### 4.3.4 代码实践

1. **实践目标**：看一次完整的 K-loop 改写，并对照三个位置（种子、谓词、extract）。
2. **操作步骤**（示例代码）：

   ```python
   # 示例代码：全尺寸 Mat 驻留 matmul，观察 AutoTileMatmulL0 的 K-loop 改写
   import pypto as pl

   @pl.jit
   def mm(x: pl.Tensor[[128, 256], pl.FP16],
          w: pl.Tensor[[256, 128], pl.FP16]) -> pl.Out[pl.Tensor[[128, 128], pl.FP32]]:
       with pl.at(level=pl.Level.CORE_GROUP):
           a = pl.load(x, [0, 0], [128, 256])
           b = pl.load(w, [0, 0], [256, 128])
           c = pl.matmul(a, b)
           pl.store(c, [0, 0])
   ```

   - 带 `dump_passes` 编译，取 `auto_tile_matmul_l0` 的 before/after 两份文本 diff；
   - 再向后翻到 `lower_pipeline_loops` 的 dump，看 `ForKind::Pipeline` 被落成什么样（预习 u6-l5）。
3. **需要观察的现象**：after 中出现 `tile.create([...], dtype=..., target_memory=Acc)` 种子、一条 `pl.pipeline` 循环（`stage=2`）、循环体内两条 `tile.extract(..., target_memory=Left/Right)` 与**一条**四实参 `tile.matmul_acc(c_iter, sa, sb, ko == 0)`。
4. **预期结果**：没有 `if ko == 0` 分支、没有任何 Acc→Acc 拷贝；原 `tile.matmul` 消失。可与 [tests/ut/ir/transforms/test_auto_tile_matmul_l0.py](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/transforms/test_auto_tile_matmul_l0.py) 中 before/after 断言用例的期望形态互相印证。选块 `(m, n, k)` 的具体值依赖后端容量与代价模型，属待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：谓词化体（`matmul_acc(c_iter, sa, sb, ko == 0)`）相比剥皮体（`if ko == 0: matmul else matmul_acc`）省掉了什么？
答案：省掉了两个 Acc 缓冲与它们之间的 phi。剥皮体的 then 臂会新铸一个 Acc 缓冲，两臂在 phi 汇合需要 Acc→Acc 拷贝来调和——而 L0C 只有 FIXPIPE 排空一个读者，这种拷贝不存在。谓词化让 create/call/yield/return_var 链构造上共享一个 L0C 缓冲。

**练习 2**：`rows_narrowed` 用 `!= ProofResult::kTrue` 而不是 `== kFalse`，两者差在哪？
答案：ProofResult 是三值的（kTrue/kFalse/kUnknown）。`== kFalse` 只在"证明不相等"时收窄；`!= kTrue` 在"证明不相等"**和"证明不出来"**两种情况都收窄——无法证明时按危险处理，是保守正确的选择。

**练习 3**：为什么 compact 模式要声明在 `tile.create` 的 kwarg 上，而不是 Pass 事后改写类型节点？
答案：下一个 Pass（InferTileMemorySpace）会重新推断实参变化过的调用，Pass 事后贴的类型精化会被丢弃；kwarg 则被推断器每次重新读取，声明随 IR 语义存活（[15-auto_tile_matmul_l0.md:L202-L206](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/15-auto_tile_matmul_l0.md#L202-L206)）。

### 4.4 降级链收尾：CanonicalizeTileSlice（16）与 InferTileMemorySpace（17）

#### 4.4.1 概念说明

主降级（摊平 + 分块）完成后，还剩两类"形态噪声"要清理：

- **`tile.slice` 的规范化（pass 16）**：`tile.slice` 是"Mat Tile 的子窗口"高层构造，PTO ISA 对 Mat 上的 `pto.subview` 只支持零拷贝别名。但一个独立的 Mat slice 后面若跟着触发惰性物化的消费者，就会尝试 `loc=mat → loc=mat` 的 `pto.textract`——L1→L1 的 DMA 路径硬件不支持。这个 Pass 把能折叠的 slice 折进消费者：被 `tile.extract` 消费就把偏移加进 extract 的行列索引；被矩阵乘操作数消费就换成直接的 `tile.extract(src, or, oc, shape, target_memory=Left|Right)`（与 4.3 中 AutoTile 发的 extract 同构）。
- **内存空间推断（pass 17）**：摊平 Pass **故意**只做形状降级、不发任何 `tile.move`（[batch_matmul.cpp:L850-L868](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/flatten_tile_nd_to_2d/batch_matmul.cpp#L850-L868) 的注释明确了这条职责边界）。累加器的 Vec/Acc 往返、可重定向生产者（`tile.create`）的 target_memory 改写、所需的 `tile.move` 插入，全部推迟到这里统一解决——这避免了混合 CUBE/VECTOR 内核里曾经导致验证失败的跨核 Vec→Acc move（issue #1235）。

#### 4.4.2 核心流程

```text
CanonicalizeTileSlice (16):
  对每个 Mat 驻留 tile.slice:
    消费者是 tile.extract(s, ir, ic, shape)
      → tile.extract(src, ir + or, ic + oc, shape)          （偏移并入索引）
    消费者是 tile.matmul[_acc/_bias] 操作数
      → tile.extract(src, or, oc, shape, target_memory=Left|Right)
  另处理两类 Vec slice: col_expand_* 消费 (#1640/#2010)、非 32 字节对齐 (#1789)

InferTileMemorySpace (17):
  DemandCollector (访问器)  : 沿 inherit-input 算子回传输入内存空间约束
  TileMemorySpaceAnalyzer  : 把 yield 的内存空间回传到 iter_arg 及其初值
  MoveCollector            : 收集必须插入的 tile.move
  TileMemorySpaceMutator   : 改写可重定向生产者的 target_memory、刷新 TileView、落 move
```

#### 4.4.3 源码精读

[canonicalize_tile_slice_pass.cpp:L16-L60](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/canonicalize_tile_slice_pass.cpp#L16-L60) 的文件头注释完整陈述了动机与折叠规则，核心恒等式是：

```text
extract(slice(src, _, [or, oc]), ir, ic, shape) == extract(src, ir + or, ic + oc, shape)
```

它还点明了一个衔接事实：**FlattenTileNdTo2D 展开批次矩阵乘时，每个批次页就是一条 `tile.slice`**（页偏移 = `batch_index * page_rows`）——所以 pass 13 发出的 slice 正是 pass 16 的主要食物，这是 16 紧跟在 15 之后的直接原因。

pass 17 的四个组件类锚点在 [infer_tile_memory_space_pass.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/infer_tile_memory_space_pass.cpp#L84-L552)：`DemandCollector`（[L84](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/infer_tile_memory_space_pass.cpp#L84)）、`TileMemorySpaceAnalyzer`（[L164](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/infer_tile_memory_space_pass.cpp#L164)）、`MoveCollector`（[L488](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/infer_tile_memory_space_pass.cpp#L488)）、`TileMemorySpaceMutator`（[L552](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/infer_tile_memory_space_pass.cpp#L552)）。与 4.3 的 compact 话题呼应：`matmul_acc` 在注册表里声明的 Acc `input_constraint` 由 DemandCollector 沿"继承输入"的算子链回传（[batch_matmul.cpp:L850-L868](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/flatten_tile_nd_to_2d/batch_matmul.cpp#L850-L868) 注释把这条链路逐组件写明），Analyzer 又把 yield 的空间回传到 iter_arg **及其初值**——这正是"kwarg 声明能活、类型精化会被重推"的机制来源。

#### 4.4.4 代码实践

1. **实践目标**：在一份 dump 里同时看到 slice 被折叠、move 被插入。
2. **操作步骤**：沿用 4.2.4 的 `add3d` 与 4.3.4 的 `mm` 两份 dump 目录；分别打开 `canonicalize_tile_slice` 与 `infer_tile_memory_space` 的 before/after diff。
3. **需要观察的现象**（`mm` 更典型）：15 之后存在的 Mat 驻留 `tile.slice`（若有）在 16 之后消失或并入 `tile.extract` 的索引；17 的 after 里出现显式 `tile.move(..., target_memory=...)`（当数据需要跨空间搬运时），且各 Tile 注解的 `Mem.*` 空间标签完整。
4. **预期结果**：17 之后 IR 中每个 Tile 的内存空间都确定，可直接进入 `allocate_memory_addr`（u5-l7）的地址分配。具体出现哪些 move 依算子与后端而定，待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：pass 13 展开批次矩阵乘发出的 `tile.slice`，为什么不能留到代码生成再处理？
答案：PTO ISA 的 `pto.subview` 在 Mat 上只是零拷贝别名；独立 slice 后跟触发惰性物化的消费者会尝试 `loc=mat → loc=mat` 的 `pto.textract`，L1→L1 DMA 不被支持（[canonicalize_tile_slice_pass.cpp:L27-L30](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/canonicalize_tile_slice_pass.cpp#L27-L30) 附近注释）。

**练习 2**：摊平 Pass 为什么"故意"不发 `tile.move`？
答案：职责分离：形状降级与内存空间决策解耦后，Vec/Acc 往返、生产者重定向、move 插入可以在掌握全局信息的 pass 17 统一决定——混在 pass 13 里逐点决策曾导致混合核里出现验证失败的跨核 Vec→Acc move（issue #1235，[batch_matmul.cpp:L850-L868](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/flatten_tile_nd_to_2d/batch_matmul.cpp#L850-L868) 注释）。

**练习 3**：`DemandCollector` 回传的"Acc input_constraint"最终落到了 4.3 中哪个 IR 元素上？
答案：落到累加器生产者（`tile.create` 种子）的 `target_memory=Acc` kwarg 及其 TileView 上——推断器改写 kwarg 并刷新隐式 TileView，而不是事后改类型节点（这正是 compact 声明在 create kwarg 上的同一机制）。

## 5. 综合实践

把本讲两条主线（摊平 + 谓词透传）串成一个任务：

**任务 A：3D Tile 的摊平观察。** 写一个 3D Tile 参与的算子（可直接用 4.2.4 的 `add3d`，或把 `pl.add` 换成任意逐元素算子），带 `dump_passes` 编译，取 `flatten_tile_nd_to_2d` 的 before/after 两份 IR，回答：`[2, 3, 4]` 变成了什么形状？`tile.load` 的源窗口 offsets/shapes 是否保持 3D（源张量坐标系不变）？`pl.store` 多出的第 4 操作数是什么、为什么后端需要它？

**任务 B：init_cond 谓词的全程跟踪。** 写一个带 `init_cond` 的 `tensor.matmul_acc`，操作数取**批次维乘积为 1** 的高秩形态（示例代码）：

```python
# 示例代码：batch 维乘积为 1 的 tensor.matmul_acc + init_cond
import pypto as pl

@pl.jit
def mm_acc_splitk(
    x: pl.Tensor[[1, 128, 256], pl.FP16],   # 批次维 = 1
    w: pl.Tensor[[1, 256, 128], pl.FP16],
) -> pl.Out[pl.Tensor[[1, 128, 128], pl.FP32]]:
    with pl.at(level=pl.Level.CORE_GROUP):
        acc: pl.Tensor[[1, 128, 128], pl.FP32] = pl.zeros_like_full  # 占位说明：acc 需真实构造
        for k0 in pl.range(0, 256, 128):
            a = pl.load(x, [0, 0, k0], [1, 128, 128])
            b = pl.load(w, [0, k0, 0], [1, 128, 128])
            acc = pl.matmul_acc(acc, a, b, init_cond=(k0 == 0))
        pl.store(acc, [0, 0, 0])
```

（注：上面是骨架示意——`acc` 的初值构造请参照 `examples/beginner/05_matmul.py` 与 u2-l4 的 K 维累加范式补全，谓词写法见 [tensor_ops.py:L685-L698](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/tensor_ops.py#L685-L698) 的 docstring 示例。）然后沿 dump 逐 Pass 追踪谓词的去向：

1. **pass 10（convert_tensor_to_tile_ops）之后**：调用应已变成 `tile.batch_matmul_acc`（秩 > 2 触发 batch 形式），且第四个操作数带着 `k0 == 0`——对应 [op_conversion_registry.cpp:L1140-L1146](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/op_conversion_registry.cpp#L1140-L1146) 的放行；
2. **pass 13（flatten_tile_nd_to_2d）之后**：批次乘积为 1 走快路径，应只剩**一条** 2D `tile.matmul_acc(acc, lhs, rhs, k0 == 0)`——谓词被逐字透传（[batch_matmul.cpp:L786-L791](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/flatten_tile_nd_to_2d/batch_matmul.cpp#L786-L791)）；
3. **pass 15（auto_tile_matmul_l0）之后**：若 K 被切分成多个 L0 块，循环体的谓词应变成 `k0 == 0 and ko == 0`（[auto_tile_matmul_l0_pass.cpp:L573-L582](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/auto_tile_matmul_l0_pass.cpp#L573-L582)）；若整个 K 一块装下（直线块），谓词应原样保留 `k0 == 0`；
4. **数值验证**：与 `torch.matmul` 的结果做 `torch.allclose` 对照，并故意把 `init_cond=(k0 == 0)` 去掉重跑一次，观察首步累加进脏累加器造成的数值偏差——这就是练习 4.2.5-3 的活体版本。

各 dump 文件名、谓词在第 2/3 步的确切打印形态（位置参数还是 `init_cond=` 关键字——u4-l7 讲过不同算子的打印差异）以本地运行为准，属待本地验证。

## 6. 本讲小结

- `tile_pto_passes`（12~17）是一条顺序不可交换的硬件化降级链：**拆复合算子（12）→ 摊平成 2D（13）→ cast 合法化（14）→ L0 分块（15）→ slice 规范化（16）→ 内存空间推断（17）**，每一步都为下一步准备前置形态（如 13 产出 `TileOps2D` 是 15 的 required 属性）。
- LowerCompositeOps 是"查表 + 规则函数"范本：`kRules` 一行注册一个复合算子，分发器零改动；产物只含原语，因此幂等。
- FlattenTileNdTo2D 的摊平规则是"合并前 k−1 维、保留最后一维"，`valid_shape` 的合并不要求静态；**批次维乘积为 1 的 `tile.batch_matmul_acc` 走快路径直发一条 2D `tile.matmul_acc`，且 `init_cond` 谓词被逐字透传**（本次更新），高批次乘积因 L0C 跨步窗口不可寻址仍不可用（原因与谓词无关）。
- AutoTileMatmulL0 用穷举 roofline 选 `(m, n, k)`，产出的 K-loop 体是**谓词化的单条 `tile.matmul_acc`**——一个 L0C 缓冲、无 phi；调用方 `init_cond` 与生成的 `ko == 0` 做 AND。
- **行收窄累加器的种子用 `tile.create(compact=True)` 声明**（本次更新）：MAD 按有效行数决定 L0C 的 N-fractal 行距 \(\lceil M/16 \rceil \times 16\)，`matmul_acc` 继承累加操作数的 compact 模式，声明在 kwarg 上才能活过 InferTileMemorySpace 的重推断，契约由 `AccCompactValid` 验证。
- 16 与 17 是清理与补全：16 把 Mat slice 折进消费者（13 发的批次页 slice 是其主要输入），17 统一补齐内存空间与 `tile.move`（13 刻意留白）。

## 7. 下一步学习建议

- **u5-l7（内存规划三部曲）**：本讲结束时所有 Tile 的内存空间已定（pass 17），下一讲讲 MemRef 如何被创建、按生命周期复用、最终分配地址——其中"循环携带写回的排序/破环"正好接上本讲 K-loop 的 iter-arg 形态。
- **u6-l1（PTO 代码生成）**：带着本讲的产物去读 CodeEmitter——`tile.extract` → `pto.textract`、`tile.matmul_acc` → `pto.tmad`、谓词如何选择指令形态；行收窄累加器在 C2V 推送上的 L0C pitch 处理正是 compact 契约的消费端。
- **延伸阅读**：[docs/en/dev/passes/15-auto_tile_matmul_l0.md](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/15-auto_tile_matmul_l0.md) 的 "Cost model & design space" 一节（roofline 公式与设计空间枚举）、`src/ir/transforms/utils/l0_tile_chooser.cpp`（选择器本体）、[tests/ut/ir/transforms/test_flatten_tile_nd_to_2d.py](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/transforms/test_flatten_tile_nd_to_2d.py)（按算子逐条对照摊平前后形态的最佳速查表）。
