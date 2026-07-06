# 编译优化与 mir/nvvm transforms

## 1. 本讲目标

本讲聚焦 cuda-oxide 流水线**中段**的几道 IR 变换（transform）pass：

- `mem2reg`：把栈槽提升为 SSA 值；
- **循环分析与展开**（`#[unroll]` / `#[unroll(N)]`），以及它依赖的 `LoopInfo`、`induction` 两项只读分析和一组「规范化」子步骤；
- `nvvm-transforms` 的 `legalize_for_nvvm`：在 lowering 之后、导出之前，把现代 LLVM 方言改写成 libNVVM 能接受的形态。

学完后你应该能够：

1. 说出 `dialect-mir` 在变成 LLVM IR 之前/之后分别经过哪些变换，以及它们为何要按这个顺序排列；
2. 解释 `mem2reg` 的输入输出形态（栈槽 ↔ SSA），以及为什么循环展开必须在它之后；
3. 读懂 `#[unroll]` 全展开与 `#[unroll(N)]` 部分展开的差异，包括「安全预算」「活出值规范化」「常量下标折叠」几个关键设计；
4. 说清楚 `legalize_for_nvvm` 在 legacy（LLVM 7）与 modern（Blackwell+）两条路径上分别做什么；
5. 理解本轮 PR #314 的关键架构变化：这些变换的**调用编排**被统一收进 rustc 无关的新 crate `cuda-oxide-codegen`，全工具链后段只有一份实现。

本讲是 u4-l4（MIR Lowering 鸟瞰）的直接后续：u4-l4 讲「dialect → LLVM IR」那一步，本讲讲那一步**前后**插入的优化与合法化。

## 2. 前置知识

- **IR / 方言（dialect）**：cuda-oxide 用 Pliron（一个类 MLIR 的多方言 IR 框架）承载中间表示。`dialect-mir`（前缀 `mir.`）贴近 Rust 语义，`dialect-nvvm`（前缀 `nvvm.`）贴近 PTX/NVVM 指令，lowering 之后是 `llvm` 方言。本讲只关心 `mir` 与 `llvm` 两层。
- **基本块（basic block）与 CFG**：一个函数是一张由基本块组成的有向图（控制流图 CFG）。块内是直线顺序的 op，块末尾是分支终止符（terminator），如 `mir.goto`、`mir.cond_br`。
- **SSA 与 phi / 块参数**：SSA（静态单赋值）要求每个值只被定义一次。当控制流汇合时，「上一轮取哪个值」用块参数（pliron）或 phi 节点（LLVM）表达。这是 `mem2reg` 的产物。
- **支配（dominance）**：块 A 支配 B，指从函数入口到 B 的每条路径都必经 A。循环头（header）支配整个循环体。支配关系是循环识别、SSA 合法性的基石。
- **mem2reg 之前**：mir-importer 产出的 `dialect-mir` 采用「alloca + load/store」模型——每个非 ZST 局部变量占一个栈槽，写即 store、读即 load（见 u4-l2）。这是循环展开前的输入形态。
- **libNVVM / NVVM IR**：当内核用到浮点数学函数时，cuda-oxide 走 NVVM IR 路径，把生成机器码交给 NVIDIA 的 libNVVM + nvJitLink（见 u4-l5）。libNVVM 接受的是 **LLVM 7 风格**的 NVVM IR，这就是 `legalize` pass 存在的原因。

> 术语提示：本讲出现「规范化（canonicalize）」有两个不同含义，不要混淆——一是 pliron 自带的全局清理 pass（`sccp` / `simplify_cfg` / `dce`），二是 `mir-transforms/src/canonicalize.rs` 这个**专供展开器使用**的循环形状归一化模块。下文会明确区分。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/cuda-oxide-codegen/src/pipeline.rs` | 后段流水线的**单一编排者** `compile_translated_module`：依次调用准备（mem2reg+unroll）→ lowering → legalize → 导出。本讲用它定位各变换的位置。 |
| `crates/cuda-oxide-codegen/src/prep.rs` | `prepare_mir_module`：把 verify → mem2reg → verify → unroll → verify 串成一个「准备」阶段，被两个前端（rustc 与 standalone）共用。 |
| `crates/mir-transforms/src/lib.rs` | `mir-transforms` crate 入口，声明 `analyses` / `canonicalize` / `unroll` 三个子模块。 |
| `crates/mir-transforms/src/unroll.rs` | 循环展开 pass 主体：`unroll_annotated_loops`、`full_unroll`、`partial_unroll`，以及常量下标折叠 `fold_constant_index_in_copies`。 |
| `crates/mir-transforms/src/canonicalize.rs` | 展开前的循环形状归一化：`merge_backedges`（统一回流边）、`close_header_liveouts`（活出值改走块参数）。 |
| `crates/mir-transforms/src/analyses/mod.rs` | 只读分析子模块入口：`loop_info` 与 `induction`。 |
| `crates/mir-transforms/src/analyses/loop_info.rs` | `LoopInfo`：识别自然循环、header/latch/preheader。 |
| `crates/mir-transforms/src/analyses/induction.rs` | `induction`：归纳变量与 trip count 分析（简化版 scalar evolution）。 |
| `crates/nvvm-transforms/src/lib.rs` | `legalize_for_nvvm` 公共入口，按 `NvvmIrDialect` 分派到两条路径。 |
| `crates/nvvm-transforms/src/legalize.rs` | legalize 主体：legacy 全套兼容改写 + modern 仅做位操作 intrinsic 宽度改写。 |
| `crates/rustc-codegen-cuda/examples/unroll_smoke/src/main.rs` | 覆盖全/部分展开、嵌套、早退、多回流边等几乎所有形态的 smoke 测试，是本讲代码实践的素材。 |

## 4. 核心概念与源码讲解

### 4.1 变换在流水线中的位置：cuda-oxide-codegen 的单一编排

#### 4.1.1 概念说明

#314 之前，后段流水线（verify / mem2reg / unroll / lower / export / llc）散落在 mir-importer 等处，rustc 前端与（未来的）实验性前端有各自走法，容易「静默漂移」——两边对同一份 `dialect-mir` 做不同的后段处理，导致同样的内核在两条路径下编出不同 PTX。

#314 抽出 rustc 无关的新 crate `cuda-oxide-codegen`，把「翻译之后的所有破坏性阶段」收拢成**唯一一份**编排函数 `compile_translated_module`。前端只负责把 `dialect-mir` 装配好（标好 kernel entry），后段就完全交给这一个函数。本讲的全部变换都由它驱动，因此先看清这张「列车时刻表」。

#### 4.1.2 核心流程

`compile_translated_module` 对一个已翻译的 `dialect-mir` 模块依次做：

1. （可选）dump 翻译后的 `dialect-mir`；
2. **准备阶段** `prepare_mir_module`：verify → `mem2reg` → verify → `unroll_annotated_loops` → verify（见 4.2、4.3）；
3. 若有设备 extern 声明，补上 extern 声明 op；
4. **lowering** `lower_to_llvm`：`dialect-mir` / `dialect-nvvm` → `llvm` 方言（u4-l4 详讲）；
5. 判定是否需要走 NVVM IR 路径（用了 libdevice 或前端显式请求）；
6. 若走 NVVM IR：选目标、定 `NvvmIrDialect`（legacy 还是 modern）→ **`legalize_for_nvvm`**（见 4.4）；
7. **整模块 verify**（lowering 后），失败被归类为 `PipelineError::LoweredVerification`；
8. 导出 LLVM IR 文本；
9. 出 PTX（经 `llc`）或返回 NVVM IR（跳过 `llc`，交给宿主侧 libNVVM/nvJitLink）。

关键不变量：mem2reg 与 unroll 在 **lowering 之前**（操作 `dialect-mir`），legalize 在 **lowering 之后**（操作 `llvm` 方言）。这决定了它们的输入载体完全不同——mem2reg/unroll 看到的是 `mir.alloca` / `mir.goto` / `mir.cond_br`，legalize 看到的是 `llvm.call` / `llvm.fneg` / `llvm.bitcast`。

#### 4.1.3 源码精读

编排函数签名与模块级文档说明它是「翻译之后所有破坏性阶段的唯一拥有者」：

[crates/cuda-oxide-codegen/src/pipeline.rs:150-156](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L150-L156) — `compile_translated_module` 入口，接受已翻译的 `dialect-mir` 模块，产出 PTX 或 NVVM IR。

[crates/cuda-oxide-codegen/src/pipeline.rs:169-192](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L169-L192) — 关键的「是否提升+展开」决策：`promote_and_unroll = !debug_kind.variables_enabled()`。完整 debug 模式（`variables_enabled`）故意保留局部变量在内存里以便调试器查看，于是跳过 mem2reg 与 unroll；普通构建则调用 `prepare_mir_module`。

[crates/cuda-oxide-codegen/src/pipeline.rs:206-211](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L206-L211) — lowering 调用 `lower_to_llvm(ctx, module, !request.backend.no_fma)`，第二个参数是 FMA 收缩开关（见 u6-l3）。

[crates/cuda-oxide-codegen/src/pipeline.rs:244-275](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L244-L275) — 仅在走 NVVM IR 时：解析目标、按目标是否「legacy LLVM」选择 `NvvmIrDialect::LegacyLlvm7` 或 `Modern`，再调用 `nvvm_transforms::legalize_for_nvvm`。

[crates/cuda-oxide-codegen/src/pipeline.rs:283-289](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L283-L289) — lowering 之后的整模块 verify，错误经 `as_lowered_verification` 重映射为 `LoweredVerification`，与翻译期 / mem2reg 期的 `Verification` 区分（u4-l4 已建立此术语）。

#### 4.1.4 代码实践

1. **目标**：在源码层确认「mem2reg/unroll 在 lowering 前、legalize 在 lowering 后」这条铁律。
2. **步骤**：打开 `pipeline.rs`，从 `compile_translated_module` 起向下，分别标出 `prepare_mir_module`、`lower_to_llvm`、`legalize_for_nvvm`、`verify_operation`、`export_llvm_ir` 五个调用点的行号。
3. **观察**：注意 `prepare_mir_module` 拿到的还是 `dialect-mir`（参数类型无关，但紧跟其后的 dump 标题写的是 `dialect-mir module (after preparation)`），而 `legalize_for_nvvm` 紧跟在 `lower_to_llvm` 之后、`export_llvm_ir` 之前。
4. **预期结果**：你能画出一张五步顺序图，并指出 legalize 操作的已经是 `llvm` 方言而非 `mir` 方言。
5. 待本地验证：若本机装了 nightly 与 CUDA Toolkit，可对任意示例运行 `cargo oxide pipeline <name>` 配合 verbose，观察输出里的 `=== Running shared mem2reg + annotated loop-unroll preparation ===`、`=== Lowering ... ===`、`=== Legalizing ... ===` 三条阶段提示是否按上述顺序出现。

#### 4.1.5 小练习与答案

**练习 1**：为什么 mem2reg 不能放在 lowering 之后？

> **答**：mem2reg 操作的是 `dialect-mir` 的 `mir.alloca` / `mir.load` / `mir.store`，把这些栈槽提升为 SSA 值并改写 `mir.cond_br` 的块参数。lowering 之后这些 op 已被翻译成 `llvm.alloca` / `llvm.load` / `llvm.store` 与 phi，mem2reg 的识别模式不再适用；而且循环展开依赖 SSA 形态（块参数表达循环携带值），必须在 lowering 之前完成。

**练习 2**：完整 debug 构建（`variables_enabled`）为何跳过 mem2reg+unroll？

> **答**：调试器需要能按名字查看局部变量，这要求变量保留为可寻址的栈槽。mem2reg 会把栈槽消解成寄存器 SSA 值，破坏可寻址性；unroll 又会复制循环体、改变行号映射。因此 debug 构建里 `promote_and_unroll` 为 false，`prepare_mir_module` 在 verify 后直接返回。

### 4.2 mem2reg 与规范化：把栈槽提升为 SSA

#### 4.2.1 概念说明

mir-importer 产出的是「alloca + load/store」形态：每个局部变量先 `mir.alloca` 一个栈槽，写即 `mir.store`，读即 `mir.load`。这对翻译器很简单（一一对应 MIR），但对后续优化是灾难——循环携带的累加器 `acc` 在每次循环里都是「load → 运算 → store」，看不出它是「每次加一个常量」的归纳变量，循环展开也无从下手。

`mem2reg` 把这种栈槽提升为 SSA 值：当一个变量在某个块被定值、在汇合点被读取时，用「块参数」表达「从哪条入边来就取哪个值」（LLVM 里等价于 phi 节点）。提升后，`acc` 变成 header 块的一个参数，preheader 给初值、latch 给「下一次的值」——归纳变量分析才能识别它。

cuda-oxide **不自己实现** mem2reg，而是直接调用 pliron 提供的 `pliron::opts::mem2reg::mem2reg`。本节重点是它在流水线里的位置、输入输出，以及它如何为展开器铺路。

「规范化」在 cuda-oxide 里有两层：一是 pliron 自带的全局清理 pass（常量传播 `sccp`、CFG 化简 `simplify_cfg`、死代码消除 `dce`），循环展开器在展开前后都调用它们；二是 `canonicalize.rs` 里**专门给展开器用**的循环形状归一化（见 4.3）。两者都叫 canonicalize，但层次不同。

#### 4.2.2 核心流程

`prepare_mir_module`（prep.rs）是 mem2reg 与 unroll 的共同壳子：

1. `verify_operation(module)`：先校验输入合法（翻译期已校验过，这里是双保险）；
2. 若 `promote_and_unroll` 为 false（debug 构建），直接返回；
3. `mem2reg(module, ctx, &mut analyses)`：栈槽 → SSA；
4. `verify_operation(module, "module post-mem2reg")`：提升后再校验；
5. `unroll_annotated_loops(module, ctx, &mut analyses)`：见 4.3；
6. `verify_operation(module, "module post-unroll")`：展开后再校验。

每一步都用 `PipelineError::Verification` 包装失败信息，并打上阶段名（`"mem2reg"` / `"loop-unroll"` / `"module post-unroll"` 等），便于定位是哪一道 pass 炸了。

#### 4.2.3 源码精读

[crates/cuda-oxide-codegen/src/prep.rs:13-18](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/prep.rs#L13-L18) — `MirPreparation` 结构体，唯一字段 `promote_and_unroll` 控制是否做提升与展开。

[crates/cuda-oxide-codegen/src/prep.rs:25-53](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/prep.rs#L25-L53) — `prepare_mir_module` 全身。注意三次 `verify_operation`：进、mem2reg 后、unroll 后各一次。第 36 行调用 `pliron::opts::mem2reg::mem2reg`，第 45 行调用 `mir_transforms::unroll::unroll_annotated_loops`。文档注释强调它是「the one shared post-translation orchestrator」，rustc 与 standalone 两个前端共用。

[crates/mir-transforms/src/lib.rs:6-17](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-transforms/src/lib.rs#L6-L17) — crate 顶层文档明确：「这些 pass 跑在流水线中部，在 mem2reg 之后、lowering 之前」，并指出第一个 pass 是循环展开，由 `#[unroll]` / `#[unroll(N)]` 注解触发。

#### 4.2.4 代码实践

1. **目标**：理解 mem2reg 对循环携带值的改写——这是展开器能否工作的前提。
2. **步骤**：读 `unroll_smoke` 里的 `partial_unroll` 内核（`let mut acc: u32 = 0; let mut i: u32 = 0; while i < n { acc = acc.wrapping_add(i); i += 1; }`）。在没有 mem2reg 时，`acc` 与 `i` 都是栈槽；mem2reg 后，它们变成循环 header 的两个块参数，preheader 边给 `(0, 0)`，latch 边给 `(acc + i, i + 1)`。
3. **观察**：对照 `analyses/induction.rs` 的文档（4.3 会读），它说「after mem2reg, a loop's per-iteration values live as the header block's block arguments」——这正是 mem2reg 的产物。
4. **预期结果**：你能用一句话说清「为什么展开器必须在 mem2reg 之后」——因为展开器要靠 header 块参数识别归纳变量与 reduction，而那是 mem2reg 才建立的形态。
5. 待本地验证：若开启 dump（`pipeline.rs` 的 `dump_mir`），可在「preparation」前后各看到一份 `dialect-mir`，前一份有 `mir.alloca` / `mir.load` / `mir.store`，后一份消失、改用块参数。

#### 4.2.5 小练习与答案

**练习 1**：`prepare_mir_module` 为何要在 mem2reg 与 unroll 之间各插一次 verify？

> **答**：mem2reg 与 unroll 都会重写 CFG 与值定义。如果 mem2reg 引入了非法 IR（例如块参数类型不匹配），却不在它之后立刻校验，bug 会延迟到 unroll 甚至 lowering 才暴露，定位极困难。每道破坏性 pass 之后立即 verify，把「翻译正确但优化错了」与「翻译就错了」分开，是 IR 编译器的标准做法。

**练习 2**：`PipelineError::Verification` 的 `name` 字段在这里分别取什么值？

> **答**：mem2reg 失败时 `name = "mem2reg"`，unroll 失败时 `name = "loop-unroll"`，而 `verify_operation` 自身失败时 `name` 取自传入的标签（`"module"` / `"module post-mem2reg"` / `"module post-unroll"`）。注意 lowering **之后**的 verify 失败会被 `as_lowered_verification` 改判为 `LoweredVerification`，不在这里。

### 4.3 循环分析与展开：mir-transforms 的 unroll

这是本讲最重的一节，对应 `mir-transforms` crate 的全部内容：两项只读分析（`loop_info`、`induction`）、一组循环形状归一化（`canonicalize.rs`）、以及展开主体（`unroll.rs`）。

#### 4.3.1 概念说明

**循环展开（loop unrolling）** 通过复制循环体来减少迭代次数（甚至消除循环），用更大的代码体积换更少的循环开销与更多的优化机会。最朴素的例子：

```text
i = 0; while i < 4 { f(i); i += 1 }   展开成：   f(0); f(1); f(2); f(3);
```

cuda-oxide 提供两种注解（由 `#[kernel]` 宏读取并改写成循环内的 `mir.unroll_hint` op）：

- `#[unroll]`：**全展开**，要求 trip count 编译期可知，展开后不剩循环；
- `#[unroll(N)]`：**部分展开**，每次迭代跑 N 份体拷贝，留一个处理余数的「remainder loop」。trip count 可以运行期才知道。

要安全地展开一个循环，pass 需要回答一连串问题：哪些块构成一个循环？循环跑几次？计数器怎么变？多入口怎么办？多个 `continue`、`break` 怎么办？body 在循环外被引用的值（live-out）怎么处理？为此 `mir-transforms` 把这些问题拆给两份**只读分析**：

- `LoopInfo`（`analyses/loop_info.rs`）：识别自然循环，给出 header / latches / body blocks / preheader / exiting / exit blocks，以及循环嵌套森林；
- `induction`（`analyses/induction.rs`）：识别归纳变量（IV）、reduction、invariant，并在能算时给出 trip count——一份简化版的 scalar evolution。

分析只读、变换只写，二者分离，未来其他循环 pass（如向量化）可复用同一份分析。

#### 4.3.2 核心流程

`unroll_annotated_loops` 的总体形状是「逐函数、逐循环、由内向外」：

1. **早退**：若整个模块没有任何 `mir.unroll_hint`，逐字节不动直接返回（保证无注解时零副作用）。
2. **逐函数**：只处理含 hint 的函数，先对它跑 `simplify_cfg`（把宏调用常带来的孤立块并回循环体）。
3. **逐循环、由内向外**：每轮重新计算支配树与 `LoopInfo`（因为上一轮展开改写了 CFG），挑出「最小的、还带 hint 的」循环——最小即最内层。
4. **形状归一化**（canonicalize.rs，见下）。
5. **归纳分析**：`induction::analyze` 算出 IV、step、trip count。
6. **展开**：`full_unroll`（factor=0）或 `partial_unroll`（factor≥2）。
7. **清理**：对该函数跑 `sccp` + `simplify_cfg` + `dce`——折叠常量下标、删死分支、删不可达的原始循环块。
8. **整模块 verify**：只有发生过改动才跑。

**形状归一化**（canonicalize.rs）是展开器专用的两个小 pass，名字易与全局「canonicalize」混淆，其实只服务于展开：

- `merge_backedges`：把多条回流边（多个 `continue`）通过一个新建的统一 latch 块汇合，使展开器只需处理单 latch。这是 LLVM `LoopSimplify` 的 `insertUniqueBackedgeBlock` 子步骤。
- `close_header_liveouts`：当循环被全展开、header 即将消失时，header 的块参数（循环携带值）若在循环外被直接引用，必须先改走「外部块的块参数」传递（一种 LCSSA 风格改写），否则 header 一删这些引用就悬空。

**安全预算**是另一条主线。展开会复制代码，无界复制会撑爆编译器。`unroll.rs` 设了三个硬上限：

\[ \text{copies} \le 1024,\quad \text{copies} \times \text{blocks/copy} \le 8192,\quad \text{copies} \times \text{ops/copy} \le 65536 \]

任一超限即「大声警告 + 不展开」，绝不静默截断。

**全展开 vs 部分展开** 的关键差异：

- 全展开：trip count 必须是编译期常量 T；铺 T 份体拷贝，第 k 份的计数器是字面量 `init + k*step`；原始循环块变不可达，由 `simplify_cfg` 删除。要求证明 IV 在到达终值前不会溢出（`full_iv_stays_in_range`）。
- 部分展开：trip count 可运行期知晓；新建一个「main loop」header，body 是 N 份拷贝链式串联，每轮计数器步进 `N*step`；原循环被留作 remainder。需要构造守卫 `i + (N-1)*step < bound` 决定「一整组还放得下就进 main loop，否则进 remainder」。**部分展开不支持早退/多出口**（main 与 remainder 的出口值合并留作后续）。

**常量下标折叠**（`fold_constant_index_in_copies`）是部分展开的核心收益。部分展开后，main loop 的计数器只取 `init, init+N·step, init+2N·step, …`。当体里出现形如 `(iv ± const) & MASK`（MASK+1 是 2 的幂）或 `(iv ± const) % 2^k` 时，只要窗口 `2^k` 整除展开步长 `N·step`，每个拷贝里的这个值在每次迭代都相等——可以换成字面量。这是 GEMM 流水线「stage index」的标准模式（见 `partial_fold` 示例）。

折叠的充要条件（数学上）：

设 `iv = init + step_jump · t`（`step_jump = N · step`），对窗口 `W`（2 的幂），展开窗口内偏移 `offset`：

\[ ((\text{init} + \text{offset}) + \text{step\_jump} \cdot t) \bmod W \]

当 \( W \mid \text{step\_jump} \) 时，\( \text{step\_jump} \cdot t \bmod W = 0 \)，故上式恒等于 \( (\text{init} + \text{offset}) \bmod W \)，与 `t` 无关——折叠为常量。这正是 `fold_constant_index_in_copies` 判定 `step_jump % window == 0` 的依据。

#### 4.3.3 源码精读

[crates/mir-transforms/src/analyses/mod.rs:6-19](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-transforms/src/analyses/mod.rs#L6-L19) — 分析子模块文档，明确「分析只读、pass 只写」的分层，命名遵循 pliron 约定（无 `-analysis` 后缀，靠目录标识）。

[crates/mir-transforms/src/analyses/loop_info.rs:6-40](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-transforms/src/analyses/loop_info.rs#L6-L40) — `LoopInfo` 概念词典：支配、回流边、自然循环、reducible CFG（Rust MIR + mem2reg 恒为 reducible，故循环成干净的嵌套森林）。这段文档值得逐条读，是理解后续所有循环 pass 的基础。

[crates/mir-transforms/src/analyses/induction.rs:6-50](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-transforms/src/analyses/induction.rs#L6-L50) — 归纳分析文档：把 header 块参数标为 BasicIv（`{init, step}`，给出 recurrence `value = init + step·iter`）、Reduction（循环携带累加器，如 `acc`）、Invariant（不变量）。trip count 从出口测试 `IV <pred> bound` 读出。文档明确这是「谨慎的、简化版 scalar evolution」，不认识的形状一律报 `Unknown` / `None`，绝不猜。

[crates/mir-transforms/src/canonicalize.rs:6-24](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/canonicalize.rs#L6-L24) — 归一化模块文档，配图说明多回流边如何经统一 latch 汇合。注意它只服务展开器，与 pliron 的全局 canonicalize 不同。

[crates/mir-transforms/src/canonicalize.rs:40-48](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-transforms/src/canonicalize.rs#L40-L48) — `CanonicalizeOutcome` 三态：`Unchanged`（已是规范形）/ `Changed`（已改写，必须丢弃缓存的分析）/ `Unsupported`（CFG 信息不足以安全改写，调用方告警并跳过）。

[crates/mir-transforms/src/canonicalize.rs:179-261](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-transforms/src/canonicalize.rs#L179-L261) — `merge_backedges`：先校验每条回流边的合法性，再新建一个 `unified` 块插入到 header 前，让它 `mir.goto` 到 header，把所有旧回流边重定向到 `unified`。

[crates/mir-transforms/src/unroll.rs:6-42](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-transforms/src/unroll.rs#L6-L42) — 展开模块的完整文档，说明 `#[unroll]` / `#[unroll(N)]` 的语义、当前支持与限制（仅识别显式 counted `while` 循环，不识别 range `for`；部分展开需正 step、`<`/`<=` 测试、循环不变 bound）。

[crates/mir-transforms/src/unroll.rs:81-94](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-transforms/src/unroll.rs#L81-L94) — 三个安全预算常量 `MAX_UNROLL_COPIES = 1024`、`MAX_CLONED_BLOCKS = 8192`、`MAX_CLONED_OPS = 65536`，分别限制单注解的最大拷贝数、克隆块数、克隆 op 数。

[crates/mir-transforms/src/unroll.rs:172-200](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-transforms/src/unroll.rs#L172-L200) — `unroll_annotated_loops` 入口与「无 hint 即逐字节不动」的早退（`has_hints` 检查）。

[crates/mir-transforms/src/unroll.rs:381-390](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-transforms/src/unroll.rs#L381-L390) — 单函数展开后的清理三连：`sccp`（常量传播，把常量下标折成字面量）+ `simplify_cfg`（删不可达的原始循环块、化简死分支）+ `dce`（删无用 op）。外层 `if function_changed` 保证无注解的函数绝不被这套清理触碰。

[crates/mir-transforms/src/unroll.rs:899-933](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-transforms/src/unroll.rs#L899-L933) — `full_unroll` 入口：要求 trip count 为常量、过克隆预算、过 live-out 安全检查、过 IV 不溢出检查（`full_iv_stays_in_range`），任一不过即 `UnrollOutcome::Skipped(reason)`。

[crates/mir-transforms/src/unroll.rs:1070-1105](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-transforms/src/unroll.rs#L1070-L1105) — `partial_unroll` 入口：要求 factor ≥ 2、正 step、`<`/`<=` 测试、循环不变 bound。注意第 1138-1145 行专门拒绝「bound 是循环携带值」的情形（如 `carried_bound` 示例），因为守卫 `i + (N-1)*step < bound` 只在 bound 每轮都不变时才成立，否则是 miscompile。

[crates/mir-transforms/src/unroll.rs:1282-1363](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-transforms/src/unroll.rs#L1282-L1363) — `fold_constant_index_in_copies` 的详细文档与实现。文档给出 N=4、`(iv+j) & 3` 折成 `0,1,2,3` 的完整推导，并列出所有触发条件（窗口是 2 的幂、整除展开步长、`%` 仅对无符号、`M>0`）。第 1350 行的判定 `window & (window - 1) != 0`（非 2 的幂）与 `step_jump % window != 0`（不整除）即上面数学推导的直接编码。

> 示例代码片段（非项目原文，为讲解精简）：
> ```text
> // 部分展开 N=4 后，main loop 计数器 iv 只取 0,4,8,12,...
> // copy j 的 stage 索引 (iv + j) & 3：
> //   j=0 -> iv & 3 = 0   j=1 -> 1   j=2 -> 2   j=3 -> 3
> // 全是编译期常量，fold 把每个 & 3 op 换成字面量。
> ```

#### 4.3.4 代码实践

本节实践是本讲的「综合实验台」，对应规格里要求对照 `unroll_smoke` 用 `cargo oxide pipeline` 观察展开前后 IR 差异。这里给出最小可走步骤。

1. **目标**：亲眼看到 `#[unroll]` 全展开把循环变成直线代码、`#[unroll(4)]` 部分展开把 stage 索引折成常量。
2. **步骤**：
   - 打开 `unroll_smoke/src/main.rs`，定位三个最经典的内核：
     - [crates/rustc-codegen-cuda/examples/unroll_smoke/src/main.rs:31-45](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/unroll_smoke/src/main.rs#L31-L45) — `full_unroll`：`#[unroll] while i < 8`，trip count 恒为 8，应被全展开。
     - [crates/rustc-codegen-cuda/examples/unroll_smoke/src/main.rs:49-62](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/unroll_smoke/src/main.rs#L49-L62) — `partial_unroll`：`#[unroll(4)] while i < n`，n 运行期可知，应部分展开并留 remainder。
     - [crates/rustc-codegen-cuda/examples/unroll_smoke/src/main.rs:68-81](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/unroll_smoke/src/main.rs#L68-L81) — `partial_fold`：`#[unroll(4)]` + `acc += i & 3`，是 GEMM stage 模式，展开后 `(i+j) & 3` 应折成 `0,1,2,3`。
   - 在能跑工具链的环境（无需 GPU，`pipeline` 只编译到 PTX）执行：
     ```bash
     cargo oxide pipeline unroll_smoke
     ```
   - 在产物目录里找到 `full_unroll` 与 `partial_fold` 对应的 `dialect-mir` 中间文件（pipeline 会在 dump 开启时写出「preparation 前后」两份）。若无 dump，可临时设 `CUDA_OXIDE_VERBOSE=1` 观察阶段提示。
3. **观察**：
   - `full_unroll`：preparation 后应看不到 `while`/`mir.cond_br` 回边，body 被复制 8 份，`i & 3` 已折成常量（0,1,2,3,0,1,2,3）。
   - `partial_fold`：应看到一个 main loop（body 4 份拷贝）+ 一个 remainder loop；4 份拷贝里的 `& 3` op 已被字面量 `0/1/2/3` 替换。
4. **预期结果**：你能指着 IR 说「这里展开过、这里做了常量折叠」，并解释为何 remainder loop 不折叠（其余数计数器不整除 4，低 2 位会变）。
5. 待本地验证：`pipeline` 子命令的具体 dump 文件路径与开关名以本机 `cargo oxide pipeline --help` 与 `crates/cargo-oxide/README.md` 为准；若环境不允许运行，按上面源码读法做「源码阅读型实践」同样有效——阅读 `fold_constant_index_in_copies` 的注释，手动验证 `(iv+0)&3 … (iv+3)&3` 在 `iv` 为 4 的倍数时确实恒为 `0,1,2,3`。

#### 4.3.5 小练习与答案

**练习 1**：`carried_bound` 内核（`hi` 每轮递减）加了 `#[unroll(4)]`，会发生什么？

> **答**：`partial_unroll` 在 [unroll.rs:1138-1145](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-transforms/src/unroll.rs#L1138-L1145) 检测到 bound（`hi`）既是 header 块参数又在循环内被改写，判定「bound 非循环不变量」，返回 `Skipped` 并打印警告，循环原样保留。这是「宁可不展开也不 miscompile」原则的体现——guard `i + 3 < hi` 在 hi 每轮变小时不再正确。

**练习 2**：为什么展开器「由内向外」处理嵌套循环，且每轮重算 `LoopInfo`？

> **答**：展开一个外层循环会把它的 body（含内层循环）整个克隆到每个拷贝里；若先展开外层，内层的 hint 会被复制成多份，再去重很麻烦。先展开内层、消费掉它的 hint，再克隆外层时内层已是普通代码，hint 不会被复制。又因为每轮展开都重写 CFG（删块、建新块），旧的支配树与 `LoopInfo` 会失效，所以每轮必须从新计算的支配树重建 `LoopInfo`——这正是 `unroll.rs:231-236` 每轮新建 `AnalysisManager` 与 `DomInfo` 的原因。

**练习 3**：`merge_backedges` 在什么情况下返回 `Unchanged`？

> **答**：当循环只有一条回流边，且该边已是「从 latch 直接 `mir.goto` 到 header（且 header 是 latch 的第 0 个后继、latch 没有其他后继）」的规范形态时，`merge_backedges` 直接返回 `Unchanged`，不做任何改写（见 [canonicalize.rs:229-238](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-transforms/src/canonicalize.rs#L229-L238)）。`partial_continue_paths`（两个 `continue`）则会被改写成 `Changed`。

### 4.4 nvvm-transforms：为 libNVVM 做合法化（legalize）

#### 4.4.1 概念说明

当内核走 NVVM IR 路径（用到 libdevice 数学函数，或前端显式请求），产物不是 PTX 而是 `.ll`，交给宿主侧的 libNVVM + nvJitLink 编译成 cubin（见 u4-l5）。问题在于：**libNVVM 接受的 NVVM IR 基于固定的 LLVM 版本**——CUDA 12 的 libNVVM 对应 LLVM 7，而 cuda-oxide 的 lowering 产出的是「现代」LLVM 方言 op。两者的差异必须由一道 pass 抹平，这就是 `legalize_for_nvvm`。

legalize 在 **lowering 之后、文本导出之前** 运行，操作的是 `llvm` 方言 op（不是 `mir` 方言）。它有两条路径，按目标架构分派：

- **legacy（LLVM 7）**：pre-Blackwell 目标，跑「全套兼容改写」——把现代 op 改写成 LLVM 7 子集，遇到无法表达的就报错。
- **modern**：Blackwell 及更新目标，保留现代 op，但仍做一项 NVVM 通用的兼容改写（位操作 intrinsic 的整数宽度）。

#### 4.4.2 核心流程

`legalize_for_nvvm`（lib.rs）按 `NvvmIrDialect` 分派：

- `LegacyLlvm7` → `legalize_for_legacy_nvvm`；
- `Modern` → `legalize_nvvm_bit_intrinsics`。

**legacy 全套改写**的骨架是「先全模块校验、再统一改写、最后再校验」：

1. **收集**所有 op；
2. **校验**（改写前）：拒绝不可移植的 f16 类型、拒绝尚未合法化的原子/fence、逐个校验候选改写目标的签名；
3. **改写**：清 nneg 标志、`llvm.fneg` → 异号 XOR、整数饱和 intrinsic → 展开成 `select`/`icmp`、i128 位操作 intrinsic → 拆成 i64、warp shuffle/vote → 改用 legacy 聚合 intrinsic、`memcpy/memmove/memset` intrinsic 换旧命名；
4. **删除**已废弃的 intrinsic 声明；
5. **再校验**（`verify_legacy_subset`）：确保没有遗漏的现代 op/标志。

**modern 路径**只做位操作 intrinsic 的 i128 → i64 拆分（NVVM 的位 intrinsic 上限到 i64，与架构无关），其余现代 op、f16、原子、intrinsic 签名都不动。

关键设计：legacy 路径对**尚未合法化的原子与 fence 直接报错**（而非「用未验证的语义 emit」）。这意味着 pre-Blackwell 走 NVVM IR 路径时，原子操作会被拒绝——文档明确建议这种场景改走普通 PTX 输出或 Blackwell NVVM 目标。

#### 4.4.3 源码精读

[crates/nvvm-transforms/src/lib.rs:6-39](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/nvvm-transforms/src/lib.rs#L6-L39) — crate 文档与 `legalize_for_nvvm` 分派。注释清楚说明：「pre-Blackwell 收 LLVM 7 兼容 op；Blackwell+ 保留现代 op，只做 NVVM 范围的兼容改写」。

[crates/nvvm-transforms/src/legalize.rs:6-10](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/nvvm-transforms/src/legalize.rs#L6-L10) — legalize 模块文档：在 lowering 后运行，保持语义，遇到无等价 legacy 形态时报错。

[crates/nvvm-transforms/src/legalize.rs:51-103](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/nvvm-transforms/src/legalize.rs#L51-L103) — `legalize_for_legacy_nvvm` 主体。注意第 57-61 行「先全模块校验再改写」的循环，与第 1197 行的 `verify_legacy_subset` 收尾。

[crates/nvvm-transforms/src/legalize.rs:111-165](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/nvvm-transforms/src/legalize.rs#L111-L165) — `legalize_nvvm_bit_intrinsics`：modern 路径，只改写位 intrinsic 宽度，f16/原子/现代 op 全保留。

[crates/nvvm-transforms/src/legalize.rs:251-275](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/nvvm-transforms/src/legalize.rs#L251-L275) — `reject_unsupported_op`：legacy 路径拒绝原子 load/store/rmw/cmpxchg、fence、debug-value。错误信息建议「改用普通 PTX 输出或 Blackwell NVVM 目标」。这是「不可移植就报错、绝不静默」原则的又一处体现。

[crates/nvvm-transforms/src/legalize.rs:400-422](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/nvvm-transforms/src/legalize.rs#L400-L422) — `rewrite_fneg`：把 `llvm.fneg` 改写成 `bitcast → XOR 符号位 → bitcast`。注释强调这是「精确的符号位翻转」，比 `0.0 - x` 更强（后者会改变 NaN 与 +0.0 的行为）。

[crates/nvvm-transforms/src/legalize.rs:468-525](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/nvvm-transforms/src/legalize.rs#L468-L525) — `rewrite_integer_sat`：把 `llvm.sadd.sat` / `llvm.usub.sat` 等饱和 intrinsic 展开成 `add + icmp + select` 的饱和逻辑。无符号用 `icmp ULT` 检测回绕，有符号用符号比较判定是否溢出到上下界。

[crates/nvvm-transforms/src/legalize.rs:553-565](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/nvvm-transforms/src/legalize.rs#L553-L565) — `legacy_bit_rewrite`：列出需要改写的 i128 位 intrinsic（`bswap` / `bitreverse` / `ctpop` / `ctlz` / `cttz` / `fshl` / `fshr`）。它们被 `split_i128` 拆成两个 i64，分别调用对应的 `llvm_*_i64` intrinsic，再组合回来——因为 NVVM 的位 intrinsic 上限是 i64。

[crates/nvvm-transforms/src/legalize.rs:1197-1243](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/nvvm-transforms/src/legalize.rs#L1197-L1243) — `verify_legacy_subset`：改写后的最终校验，确保模块里不再残留 `fneg`、`true` 的 nneg 标志、或任何未改写的目标 intrinsic。这是 legalize 自身的「双保险 verify」。

#### 4.4.4 代码实践

1. **目标**：通过 legalize 的单元测试理解它的输入输出契约，而不必真跑 libNVVM。
2. **步骤**：阅读 `legalize.rs` 末尾的 `#[cfg(test)] mod tests`（从第 1245 行起）。重点看：
   - `fneg_is_an_exact_sign_bit_toggle_and_module_still_verifies`（约 L1293）：构造一个 `llvm.fneg`，legalize 后断言它被换成 2 个 `bitcast` + 1 个 `xor`，且模块仍能 verify。
   - `unsupported_i128_bit_intrinsics_expand_to_legacy_i64_operations`（约 L1407）：对 `llvm_bswap_i128` 等，legalize 后断言不再出现 i128 版本，而出现 `llvm_bswap_i64`。
   - `unsupported_atomic_load_fails_before_mutating_other_ops`（约 L1721）：构造原子 load，断言 legalize 报错且**模块未被改动**（before == after）。
3. **观察**：注意每个测试都同时断言两件事——改写结果正确，以及 `module.verify()` 仍通过。这正是 legalize 的契约：改写后必须仍是合法 IR。
4. **预期结果**：你能复述「fneg 怎么改、i128 位 intrinsic 怎么拆、原子为何被拒」三条规则。
5. 待本地验证：可单独跑这些测试 `cargo test -p nvvm-transforms`（无需 GPU、无需 libNVVM，纯 IR 层）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 modern 路径仍要改写位 intrinsic 的宽度，却不动 f16 与原子？

> **答**：位 intrinsic 的 i128 上限是 NVVM 自身的限制（与架构无关，Blackwell 也只接受到 i64），所以两条路径都要改。而 f16 标量类型、现代原子语义、intrinsic 签名这些，Blackwell+ 的现代 NVVM 直接支持，故 modern 路径保留不动——只有 legacy（CUDA 12 LLVM 7）才不接受，需在 legacy 路径里拒绝或改写。

**练习 2**：legacy 路径里 `remove_nneg`（[legalize.rs:309-320](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/nvvm-transforms/src/legalize.rs#L309-L320)）为什么「清零但保留属性」，而不是删除属性？

> **答**：pliron-LLVM 的 `ZExtOp`/`UIToFPOp` 实现了 NNegFlag 接口，其 verifier 要求该属性必须存在（即使为 false）。LLVM 7 文本又无法解析 `nneg` 关键字。所以折中：把语义位清零（导出器会省略 false 值），同时保留属性以满足方言不变量。测试 `nneg_is_cleared_but_required_dialect_attribute_is_retained` 专门守护这一点。

## 5. 综合实践

把本讲四块知识串起来，完成一个「追踪一条 `#[unroll]` 从注解到 PTX」的端到端阅读任务：

1. **起点**：在 `unroll_smoke/src/main.rs` 选 `full_unroll` 内核，它有 `#[unroll] while i < 8` 与 `acc += i & 3`。
2. **第一阶段（宏 → IR）**：回忆 u2-l1，`#[kernel]` 宏把 `#[unroll]` 翻译成循环内的 `mir.unroll_hint` op（factor=0）。在 `unroll.rs` 的 `collect_hints`（[unroll.rs:1526-1540](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-transforms/src/unroll.rs#L1526-L1540)）确认这是 pass 找注解的入口。
3. **第二阶段（mem2reg）**：在 `prep.rs:36` 的 `mem2reg` 调用处，说明 `acc` 与 `i` 如何从栈槽变成 header 块参数。
4. **第三阶段（展开）**：跟踪 `unroll_annotated_loops` → `full_unroll`，说明 trip count=8、铺 8 份拷贝、每份计数器是字面量 `0..7`，`i & 3` 因计数器已是字面量而由后续 `sccp` 折成常量（注意：全展开不靠 `fold_constant_index_in_copies`，那个只服务部分展开；全展开的折叠走普通常量传播，见 `unroll.rs:1322-1324` 的注释）。
5. **第四阶段（lowering + 可能的 legalize）**：若该内核用了 libdevice 数学函数，则 lowering 后走 `legalize_for_nvvm`；`full_unroll` 没有用，所以直接出 PTX。在 `pipeline.rs` 里指出这一分叉点（L213-214 的 `should_emit_nvvm_ir`）。
6. **产出**：写一段文字说明「`#[unroll]` 注解经过哪几道 pass、各 pass 操作哪层 IR、各自的前置条件」，并标注每道 pass 对应的源文件与函数。

如果环境允许，用 `cargo oxide pipeline unroll_smoke` 配合 verbose/dump 取到中间 IR，对照你的文字描述逐条验证；否则按上面源码读法做纯阅读型实践。

## 6. 本讲小结

- cuda-oxide 的 IR 变换分两簇：**lowering 前**操作 `dialect-mir` 的 mem2reg + 循环展开；**lowering 后**操作 `llvm` 方言的 NVVM legalize。两簇载体不同、职责不同。
- #314 起，这些变换的**调用编排**统一收进 rustc 无关的 `cuda-oxide-codegen::compile_translated_module`，rustc 与 standalone 两个前端共用同一份后段，杜绝漂移；`prepare_mir_module` 把 verify → mem2reg → verify → unroll → verify 串成准备阶段。
- mem2reg 把「alloca + load/store」提升为 SSA 块参数，是归纳变量识别与循环展开的前提；本讲不实现它，直接复用 pliron。
- 循环展开是「分析只读、pass 只写」分层架构的范例：`LoopInfo` 识别循环、`induction` 算归纳变量与 trip count、`canonicalize.rs` 归一化循环形状、`unroll.rs` 做全/部分展开，三道安全预算（1024 拷贝 / 8192 块 / 65536 op）与 IV 溢出检查守住编译时间。
- 全展开要求编译期 trip count、靠字面量计数器 + 常量传播折 `i & 3`；部分展开容忍运行期 trip count、靠 `fold_constant_index_in_copies` 在窗口整除展开步长时折 stage 索引，但拒绝早退、多出口、循环携带 bound 等会 miscompile 的形状。
- NVVM legalize 在 lowering 后把现代 LLVM 方言改写成 libNVVM 接受的形态：legacy（LLVM 7）全套改写（fneg→XOR、饱和 intrinsic→select、i128 位 intrinsic→i64 拆分、shuffle/vote→legacy 聚合、内存 intrinsic 改名）并拒绝原子/fence；modern 仅做位 intrinsic 宽度改写。改写前后各 verify 一次。

## 7. 下一步学习建议

- **u6-l3（mir-lower 深潜）**：本讲多次提到「lowering」，那一步的算术 op 转换、FMA 收缩、libdevice 调用细节是 u6-l3 的主题，读完能让 4.1 里「lowering 那一步」真正落地。
- **u6-l6（独立后端 cuda-oxide-codegen 深潜）**：本讲的 `compile_translated_module` 就在那个 crate 里，u6-l6 会讲它的 experimental 公共 API（`CodegenModule` / `Compiler` / `CompileOptions` / `Target`）与 `Toolchain` 发现，帮你把「变换编排」与「工具链发现」拼成完整后端图景。
- **u6-l4（端到端新增 intrinsic）**：若你想给 cuda-oxide 加新的设备能力，u6-l4 的五阶段流水线与本讲的「分析只读、pass 只写」分层是同一套工程哲学的不同应用。
- **继续阅读源码**：若对循环优化感兴趣，可深读 `analyses/induction.rs` 全文（reduction/invariant 识别、trip count 推导），以及 `unroll.rs` 的 `LoopShape::analyze_shape`（理解展开器支持/拒绝的循环形状边界），它们是写新的循环 pass（如向量化、循环交换）的现成脚手架。
