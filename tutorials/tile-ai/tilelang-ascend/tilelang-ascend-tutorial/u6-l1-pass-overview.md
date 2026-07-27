# 编译 Pass 全景与配置

## 1. 本讲目标

前面 u1-l5 我们走过一遍 JIT 的「总流程」，知道一个 `@T.prim_func` 最终会经 `LowerAndLegalize` → `OptimizeForTarget` → `device_codegen` 三步变成 Ascend C 源码；u4-l3 我们又看到 `AscendSyncInsert` 这种 pass 怎么被一个 `tl.ascend_auto_sync` 开关点亮。但这两讲都只摸到了 pass 流水线的「局部」，本讲要退后一步，把整条 pass 流水线**一次性看全**。

本讲回答三个问题：

1. tile-lang 到底对 TIR 做了**多少次**改写？这些 pass **按什么顺序**排？为什么这样排？
2. 用户写在 `@tilelang.jit(pass_configs=...)` 里的那些 `tl.*` 字符串，**靠什么机制**传到 C++ pass 内部、决定一个 pass 开不开关？
3. 同样一份 kernel 源码，为什么 `target="ascendc"` 和 `target="pto"` 跑出来的 pass 行为会不一样——那个「按 target 自动默认值」是怎么实现的？

学完本讲你应当掌握：

- 完整列出 `LowerAndLegalize` 与 `OptimizeForTarget` 两个阶段里的全部 pass、各自职责，并能解释「为什么某个 pass 必须排在这个位置」。
- 理解 `PassConfigKey` 这个字符串枚举体系、`tl.*` 与 `tir.*` 两套命名空间，以及每个开关默认是开还是关。
- 读懂 `process_default_pass_config` 如何按 target（ascendc/pto/auto）填充「用户没写但该有的」默认值，以及 `PassContext` 如何把这份配置字典原封不动地送到每个 C++ pass 的 `ctx->GetConfig<Bool>(...)` 调用里。

## 2. 前置知识

进入本讲前，请确认你已经理解以下概念（均来自前序讲义）：

- **TIR 与 PrimFunc**（u2-l1、u1-l5）：前端 `@T.prim_func` 在函数定义时被静态解析成一份**与后端无关**的 TIR `PrimFunc`，它本身不可执行，必须先经一连串改写再 codegen。
- **Pass 是什么**（u1-l3、u1-l5）：一个 pass 就是一次「输入 `IRModule`、输出 `IRModule`」的纯变换，tile-lang 把若干 pass 串成一条流水线。Python 侧每个 pass 都是 `tilelang/transform/__init__.py` 里对 `_ffi_api.X()` 的薄封装，背后对应一个 C++ 里 `TVM_REGISTER_GLOBAL("tl.transform.X")` 注册的实现。
- **JIT 三阶段总流程**（u1-l5）：`@tilelang.jit` → `tilelang.lower()`（内部跑 `LowerAndLegalize` + `OptimizeForTarget` 两阶段）→ `device_codegen`（按 `target.model` 分发 ascendc / pto 两条 codegen）→ bisheng 编译加载。本讲就是要把 `lower()` 里那两阶段**拆开**看。
- **至少一个具体 pass 的样子**（u4-l3 的 `AscendSyncInsert`、u3-l1 的 `AscendInferBufferScope`）：知道一个 pass 会做「自门控（开关关闭就 `return f` 不改）+ 真正改写 IR」两件事。本讲会大量复用这些例子。

一个贯穿全讲的关键直觉：**pass 流水线的两阶段分工，本质是「先让它对，再让它快」**。`LowerAndLegalize` 负责把高层、可能非法的 TileLang 语义**降级并合法化**成一份正确的底层 TIR；`OptimizeForTarget` 再在这份正确 TIR 上做**面向硬件的优化**（软件流水、存储重写、同步插入、内存规划）。这个分工决定了每个 pass 该落在哪一阶段。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [tilelang/engine/phase.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py) | **本讲核心**：`LowerAndLegalize`（L49–L90）与 `OptimizeForTarget`（L93–L121）两个函数，逐行列出全部 pass 的调用顺序。 |
| [tilelang/engine/lower.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/lower.py) | `lower()`（L193–L237）编排两阶段与 `device_codegen`；`device_codegen`（L159–L170）按 `target.model` 分发 ascendc/pto。 |
| [tilelang/transform/pass_config.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/transform/pass_config.py) | `PassConfigKey` 枚举（L10–L95）、`_TARGET_PASS_DEFAULTS`（L103–L108）、`_apply_target_pass_defaults`（L134–L170）、`process_default_pass_config`（L220–L221）。 |
| [tilelang/transform/__init__.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/transform/__init__.py) | 每个 pass 的 Python 薄封装与 `get_pass_context()`（L25–L27）；`PassContext` 在 L10 从 TVM 导入。 |
| [tilelang/jit/\_\_init\_\_.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/__init__.py) | `compile()` 在 L85–L91 调 `process_default_pass_config` 注入 per-target 默认值。 |
| [tilelang/jit/kernel.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/kernel.py) | L227 `with tvm.transform.PassContext(opt_level=3, config=pass_configs)` 建立 PassContext，`tilelang.lower()` 在其内部运行。 |
| [src/transform/ascend_sync_insert.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_sync_insert.cc) | C++ pass 读取配置的范本：L46–L71 用 `TVM_REGISTER_PASS_CONFIG_OPTION` 注册键、用 `ctx->GetConfig<Bool>(...)` 读取并自门控。 |
| [testing/python/language/test_ascend_compile_flags.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/testing/python/language/test_ascend_compile_flags.py) | 本讲实践依据：用 `process_default_pass_config` + `pass_configs` 验证 per-target 默认值与开关行为，无需 NPU 即可运行。 |

---

## 4. 核心概念与源码讲解

### 4.1 Pass 流水线全景：从 `lower()` 看两阶段分工

#### 4.1.1 概念说明

读者第一次接触「pass 流水线」时，常会有两个困惑：**为什么要这么多 pass？** 和 **为什么不写成一个大的变换？**

答案是**可组合性**与**关注点分离**。tile-lang 的前端语义很丰富（`T.copy`、`T.gemm_v0`、`T.Parallel`、`T.Pipelined`…），而最终 codegen 只认一套窄得多的底层 IR。如果用一个巨型函数做转换，每加一个前端特性都要改这个函数、且无法独立测试。拆成一串小 pass 后，每个 pass 只解决一件事（如「把 `T.Parallel` 循环降级成向量指令」「给 buffer 推断存储层级」），可以独立开关、独立测试、自由排序。

tile-lang 把这些 pass 分成**两个阶段函数**，这就是本讲要反复对照的两个名字：

- **`LowerAndLegalize`**（降级 + 合法化）：把高层 TileLang 语义翻译成底层 TIR，并保证这份 TIR **合法**（buffer 有明确 scope、tile op 已展开、循环已向量化、内存访问有边界保护）。
- **`OptimizeForTarget`**（面向目标硬件优化）：在合法 TIR 上做**让它跑得更快**的改写（软件流水、缓冲扁平化、存储重写、自动同步、内存规划）。

这两个阶段之外，还有一个**收尾的 `device_codegen`**：它不再做 pass 级改写，而是按 `target.model` 把 TIR 翻译成 Ascend C 源码（ascendc）或 PTO IR（pto），交给 bisheng 编译。本讲聚焦两阶段 pass，codegen 留给 u6-l2 详讲。

#### 4.1.2 核心流程

整个「TIR → 源码」的编排都发生在 `lower()` 里，三步顺序不可调换：

```text
tilelang.lower(prim_func, target, platform)
   │
   │  ① 先把 platform 字符串贴到每个 PrimFunc 的属性上（供 C++ pass 读取）
   │  ② 构造一个 Target 对象（kind=llvm, model=用户指定的 ascendc/pto/auto）
   │
   ├─ mod = LowerAndLegalize(mod, target)      # 阶段一：降级 + 合法化
   ├─ mod = OptimizeForTarget(mod, target, platform)  # 阶段二：硬件优化
   └─ codegen_mod = device_codegen(mod, target, platform)  # 收尾：TIR → 源码
        │
        └─ return CompiledArtifact(source=codegen_mod.get_source())
```

**关键顺序约束**有三条，理解它们就理解了整条流水线的设计：

1. **`LowerAndLegalize` 必须先于 `OptimizeForTarget`**：阶段二的所有优化（软件流水、同步插入）都假设 IR 已经是合法底层形态；如果还在跑阶段一就插同步，结构随时会被后续 pass 推翻。
2. **`device_codegen` 必须最后**：codegen 只认扁平化、向量化、同步已插好之后的最终 IR。
3. **`platform` 经由 PrimFunc 属性传递**：有些 pass（如 u4-l3 的 `AscendSyncInsertVS` 对 A5 平台的特殊处理）需要知道目标平台代际，但 pass 的 Python 封装签名里并没有 `platform` 参数，于是 `lower()` 在进阶段一之前就把 `platform` 写进每个函数的 `npu_platform` 属性，C++ pass 直接从 PrimFunc 属性读。

#### 4.1.3 源码精读

整个编排函数 `lower()` 几乎只做「准备 → 两阶段 → codegen」三件事，没有别的逻辑：

[lower() 的三阶段编排 — tilelang/engine/lower.py:L220-L232](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/lower.py#L220-L232) —— 把 `platform` 注入 PrimFunc 属性（C++ pass 据此判断 A2/A3/A5），构造 Target，再依次跑 `LowerAndLegalize`、`OptimizeForTarget`、`device_codegen`。

注意第 224 行 `target = tvm.target.Target({"kind": "llvm", "model": target})`：tile-lang 复用了 TVM 的 Target 概念，但 `kind` 固定写 `"llvm"`，真正的后端差异（ascendc 还是 pto）藏在 `model` 字段里，由后续 codegen 按 `target.model` 分发——这正是 u6-l2 要讲的「双 codegen」入口。

`device_codegen` 本身非常薄，只做一次 `Simplify` 然后按 `target.model` 选注册函数：

[device_codegen 按 target.model 分发 — tilelang/engine/lower.py:L159-L170](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/lower.py#L159-L170) —— `ascendc`（或 `auto`）走 `target.build.tilelang_ascend`，`pto` 走 `target.build.tilelang_ascend_pto`，其它直接报错。

#### 4.1.4 代码实践

**实践目标**：在没有 NPU 的机器上，也能完整跑通 `lower()` 的两阶段 pass（不触发 bisheng 编译），亲眼看到「TIR → 改写后的 TIR」。

**操作步骤**：

1. 复用 u1-l4 的 `examples/gemm/example_gemm.py`，拿到它的 `@T.prim_func`（设为 `main`）。
2. 写一小段脚本，只调用 `tilelang.lower`，**不**调用 kernel 执行：

   ```python
   # 示例代码（非项目原有文件）
   import tilelang
   # main 是 example_gemm.py 里 build() 返回的 @T.prim_func
   artifact = tilelang.lower(main, target="ascendc")
   print(artifact.mod.script())   # 两阶段 pass 跑完之后的最终 TIR
   ```

3. 对比 `main.script()`（pass 之前）与 `artifact.mod.script()`（pass 之后），看 `T.copy`、`T.gemm_v0` 这些高层语义是否已经被展开成底层 buffer 读写与算术。

**需要观察的现象**：pass 之前的 TIR 里还能看到 `T.copy(...)`、`T.gemm_v0(...)` 这类高层调用；pass 之后它们应被替换为 `tir.call_extern` / 累加循环 / 数据搬运语句，buffer 上也带上了 `shared.l1`、`wmma.accumulator` 这类 scope。

**预期结果**：`artifact.mod.script()` 能正常打印，且高层 tile op 已消失。**若机器未装 CANN/tile-lang wheel，本步无法运行，属「待本地验证」。**

#### 4.1.5 小练习与答案

**练习 1**：如果把 `lower()` 里 `LowerAndLegalize` 和 `OptimizeForTarget` 的调用顺序对调，会发生什么？

> **答案**：`OptimizeForTarget` 里的 `InjectSoftwarePipeline`、`AscendSyncInsert` 会作用在「还没降级、还含高层 tile op、buffer 还没 scope」的 IR 上，要么直接崩溃（找不到它假设的结构），要么产出无意义的结果。这正说明两阶段顺序是硬约束，不是风格偏好。

**练习 2**：`lower()` 为什么把 `platform` 写进 PrimFunc 属性，而不是加到每个 pass 的参数里？

> **答案**：因为 pass 的 Python 封装签名（见 `__init__.py`）大部分是 `def XxxPass()` 无参形式，统一对应一个无参 C++ pass；给每个 pass 都加 `platform` 参数会破坏这套规整的 FFI。改用 PrimFunc 属性，pass 内部按需读取，签名保持干净。

---

### 4.2 LowerAndLegalize：语义降级与合法化

#### 4.2.1 概念说明

阶段一的使命是**把高层、后端无关的 TileLang 语义，翻译成一份正确且合法的底层 TIR**。所谓「合法」包括：每个 buffer 有确定的物理存储层级（L1/UB/L0A/L0B/L0C）、每个 `T.Parallel` 循环都变成了向量指令、每个 `T.tile.*` 都展开成了真实算子、跨核搬运都补上了 GM 中转、内存访问都有边界保护。

这一阶段里有几个读者已经在前面讲义里见过的「重头 pass」，本节把它们排成一张地图，重点是看清**谁依赖谁**。

#### 4.2.2 核心流程

阶段一共 15 步，按职责可归为五组：

```text
LowerAndLegalize(mod, target):
  ── 组1 准备 ─────────────────────────────────────────────
   1. InjectTmpBuffer          # 为 vector API 预分配临时 buffer
   2. AscendInferBufferScope   # 推断每个 buffer 的物理 scope（dynamic → l1/ub/l0*）
   3. AscendVidReduction       # vid 消除：threads=2 时把 UB 减半、注入 vid 偏移
   4. BufferShapeCollector     # 收集 buffer 形状信息
   5. tir.transform.BindTarget # 把 target 信息绑到 module
   6. HostProcesser            # 识别并筛出 host 侧 tiling 数据
   7. tir.transform.Simplify
  ── 组2 向量降级 ─────────────────────────────────────────
   8. AscendLowerParallelToVector  # T.Parallel 循环 → 向量指令
  ── 组3 布局与 tile op 降级 ─────────────────────────────
   9. LayoutInference          # 推断并传播片上布局（zN/nZ）
  10. CollectBufferShapes
  11. LowerTileOp              # 高层 tile op → 底层操作
  12. AscendTailMaskPropagation# UB tail 有效区域改写（默认关，self-gate）
  ── 组4 跨核与合法化 ─────────────────────────────────────
  13. AscendWorkspaceReduction # 虚拟跨核搬运 → 两阶段 GM 中转
  14. LegalizeVectorizedLoop   # 保证向量化循环合法
  15. LegalizeSafeMemoryAccess # 给内存访问加边界保护
  ── 收尾 ────────────────────────────────────────────────
  16. tir.transform.Simplify   # 清理合法化引入的冗余条件
```

**顺序背后的依赖关系**（这是理解「为什么排这里」的钥匙）：

- `AscendVidReduction`（步骤 3）必须紧跟 `AscendInferBufferScope`（步骤 2）：它要改写 UB 的 shape，而「哪些 buffer 是 UB」正是上一步刚推断出来的；它还要求 `threadIdx.x` extent==2 才生效，否则整个 pass 是 no-op（详见 u5-l3）。
- `AscendLowerParallelToVector`（步骤 8）排在 `Simplify`、`BindTarget` 之后，`LayoutInference`、`LowerTileOp` 之前：它产出的向量指令是后两者的输入。
- `LowerTileOp`（步骤 11）必须在 `LayoutInference` 之后：布局推断要在 tile op 还未展开时沿算子链传播；展开之后就太晚了。
- `AscendWorkspaceReduction`（步骤 13）依赖 `LowerTileOp` 已经把 `copy_l0c_to_ub` 这类跨 CV 搬运显式化，才能识别并改写成两阶段 GM 中转（详见 u5-l4）。

#### 4.2.3 源码精读

整个阶段一就是 `phase.py` 里一个线性函数，每一行一个 pass：

[LowerAndLegalize 全部 15 步 — tilelang/engine/phase.py:L49-L90](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py#L49-L90) —— 注释清楚地标注了每个 pass 的职责，例如 L52 `AscendInferBufferScope`「按上下文推断 scope」、L65 `AscendLowerParallelToVector`「把 parallel 循环降级成向量指令」、L78 `AscendTailMaskPropagation` 还特意说明它「self-gates on `TL_ASCEND_TAIL_MASK`、默认关、非 tail kernel 不受影响」。

注意 L61 有一行被注释掉的 `FrontendLegalize`，L89 有一行被注释掉的 `LoopVectorizeDynamic`——这是开发者保留的「备选 pass」，目前默认不走，但能看出流水线是可裁剪的。

每个 pass 的 Python 封装都极薄。以 `AscendVidReduction` 为例，`__init__.py` 里只是把调用转给 C++：

[AscendVidReduction 的薄封装 — tilelang/transform/__init__.py:L429-L438](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/transform/__init__.py#L429-L438) —— `_ffi_api.AscendVidReduction()` 对应 C++ 里 `TVM_REGISTER_GLOBAL("tl.transform.AscendVidReduction")`（定义在 `src/transform/ascend_vid_reduction.cc`）。**所有 pass 都是这种「Python 无逻辑、C++ 干活」的结构**，本讲后续遇到任何 pass，都可以用同样的方式在 `src/transform/` 找到其实现。

#### 4.2.4 代码实践

**实践目标**：辨认阶段一里哪些是 **Ascend 专属 pass**、哪些是 TVM 通用 pass。

**操作步骤**：

1. 打开 [phase.py L49–L90](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py#L49-L90)。
2. 对每个 pass，看它的调用前缀：`tilelang.transform.Xxx` 一般是 tile-lang/Ascend 自定义，`tir.transform.Xxx` 是上游 TVM 带来的。
3. 对每个 `tilelang.transform.*` pass，在 `src/transform/` 下用文件名猜对应 `.cc`（如 `AscendLowerParallelToVector` → `ascend_lower_parallel_to_vector.cc`），并 `git grep "tl.transform.AscendLowerParallelToVector"` 确认注册点。

**需要观察的现象 / 预期结果**：阶段一里 `tir.transform.*` 的只有 `BindTarget`、`Simplify` 两个（共 3 次调用），其余全是 `tilelang.transform.*`。可见阶段一「Ascend 化」程度很高。**本实践为纯源码阅读型，不依赖 NPU。**

#### 4.2.5 小练习与答案

**练习 1**：为什么 `AscendVidReduction` 排在 `AscendInferBufferScope` **之后**而不是之前？

> **答案**：vid 消除要改写 UB 的第 0 维 shape、注入 GM 偏移，前提是「已经知道哪些 buffer 是 UB」。`AscendInferBufferScope` 才把 dynamic scope 钉死成 `shared.ub`/`shared.l1`/`wmma.*`。顺序反了，vid pass 根本找不到要改的 UB buffer。

**练习 2**：`AscendTailMaskPropagation` 的注释为什么强调它「default off、self-gate」？

> **答案**：tail mask 改写只对「非对齐 tail tile」有意义，对齐 kernel 不需要；若默认开启会无谓改写所有 kernel。它通过读 `tl.ascend_tail_mask` 配置（默认 False）实现自门控，关着时直接 `return f`，所以非 tail kernel 完全不受影响。

---

### 4.3 OptimizeForTarget：硬件优化与收尾

#### 4.3.1 概念说明

阶段二的前提是：**IR 已经正确且合法**（阶段一的产物）。它的任务是让这份 IR **在目标硬件上跑得更快**。这里的「快」来自几个方向：让搬运与计算重叠（软件流水）、让 Cube 与 Vector 协同（CV 分离与跨核流水）、让片上缓冲占用更小（存储重写与内存规划）、让数据依赖不出错（同步插入）。

阶段二的另一个特点是：**收尾性质**。它排在 codegen 正前方，所以阶段二最后几个 pass（`AscendMemoryPlanning`、`AscendSyncInsert`、`AscendSyncInsertVS`）必须在「所有结构性改写都做完之后」才跑——否则同步图、地址规划随时会被后续 pass 推翻。

#### 4.3.2 核心流程

阶段二共 20 步，按职责归为五组：

```text
OptimizeForTarget(mod, target, platform):
  ── 组1 流水与 CV 协同（最前，结构性最强）─────────────────
   1. tir.PlanAndUpdateBufferAllocationLocation
   2. CrossCorePipeline      # Cube↔Vector 跨核流水拆分
   3. CombineCV              # 自动 Cube/Vector scope 分离
   4. PipelinePlanning       # 给 copy/计算语句贴 stage 标签
   5. InjectSoftwarePipeline # 拆 prefetch/main/tail 三段、缓冲多版本化
   6. AscendLowerOpaqueBlock
  ── 组2 索引与缓冲扁平化 ─────────────────────────────────
   7. tir.NarrowDataType(32)
   8. ConfigIndexBitwidth
   9. Flatten2DBuffer        # ND → 2D
  10. FlattenBuffer          # 多维 → 一维偏移
  11. tir.Simplify
  12. VectorizeLoop          # 剩余可向量化循环
  ── 组3 存储与循环清理 ───────────────────────────────────
  13. AscendStorageRewrite   # buffer 复用/放置/地址分配
  14. tir.UnrollLoop
  15. tir.RenormalizeSplitPattern
  16. tir.Simplify / RemoveNoOp / RewriteUnsafeSelect / HoistIfThenElse
  ── 组4 内存规划 ────────────────────────────────────────
  17. AscendMemoryPlanning   # 自动缓冲复用（self-gate，默认关）
  ── 组5 同步收尾（最后两个 pass，紧挨着）─────────────────
  18. AscendSyncInsert       # 主同步 pass（7 条流水线，self-gate）
  19. AscendSyncInsertVS     # VS 补充 pass（self-gate）
```

**顺序背后的关键依赖**：

- `CrossCorePipeline`（步骤 2）→ `CombineCV`（步骤 3）：跨核流水先识别并拆出「外层波 + 内层 stage」两层循环并留下 `stage_loop`/`tl_cross_interval` 注解；`CombineCV` 消费这些注解，把核间同步落到 `set/wait_cross_flag`（详见 u5-l1、u5-l2）。顺序反了，CV 分离拿不到流水信息。
- `PipelinePlanning`（步骤 4）→ `InjectSoftwarePipeline`（步骤 5）：前者只贴标签，后者据标签真正拆循环。这是经典的「规划—执行」两段式（详见 u3-l6）。
- `AscendStorageRewrite`（步骤 13）→ `AscendMemoryPlanning`（步骤 17）：前者先做地址分配与基本复用，后者在其上做更激进的缓冲复用规划。
- `AscendSyncInsert`（步骤 18）与 `AscendSyncInsertVS`（步骤 19）**紧挨着排在最后**：同步插入需要结构完全稳定。两者都自门控、默认（ascendc 下）关闭，详见 4.4 与 u4-l3。

#### 4.3.3 源码精读

阶段二同样是一个线性函数，但比阶段一多了 `target` 与 `platform` 两个参数（同步 pass 需要它们）：

[OptimizeForTarget 全部 20 步 — tilelang/engine/phase.py:L93-L121](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py#L93-L121) —— 注意 L96 `pass_ctx = tilelang.transform.get_pass_context()` 取出**当前** PassContext，供 L109 `VectorizeLoop(enable_vectorize=allow_vectorize(pass_ctx=pass_ctx))` 读取 `tir.disable_vectorize` 配置决定是否真向量化。这是「pass 运行时实时读配置」的一个活样本（4.5 节会讲这个配置从哪来）。

L110 `AscendStorageRewrite(is_npu=check_npu_availability())`：这个 pass 是 GPU/CPU/NPU 共用的，靠 `is_npu` 参数切到 Ascend 路径——是阶段二里少数「跨后端共享」的 pass 之一。

L118–L119 两个同步 pass 是阶段二的句号，也是 u4-l3 的主角。

#### 4.3.4 代码实践

**实践目标**：定位阶段二里两个同步 pass 的位置，并解释为什么它们必须排在最后。

**操作步骤**：

1. 打开 [phase.py L93–L121](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py#L93-L121)。
2. 找到 `AscendSyncInsert` 与 `AscendSyncInsertVS`，数一下它们前面有几个 pass（答案：18 个）。
3. 回顾 u4-l3：`AscendSyncInsert` 靠「算子→流水线映射 + 数据依赖 + 物理地址重叠」检测冒险并插同步。思考：如果把它移到 `FlattenBuffer`（步骤 10）之前会怎样？

**需要观察的现象 / 预期结果**：两个同步 pass 是阶段二最后两个、也是整条流水线最后两个 pass（再往后只有 codegen 的那次 `Simplify`）。**本实践为源码阅读型，不依赖 NPU。**

#### 4.3.5 小练习与答案

**练习 1**：为什么 `AscendMemoryPlanning` 排在 `AscendStorageRewrite` 之后、`AscendSyncInsert` 之前？

> **答案**：内存规划要在存储重写把地址铺好之后做更激进的缓冲复用；又必须在同步插入之前，因为同步插入要依据最终的 buffer 读写关系建依赖图，buffer 都还没规划稳定就插同步会插错位置。

**练习 2**：阶段二里 `tir.transform.*` 的 pass 明显比阶段一多（`NarrowDataType`、`UnrollLoop`、`RenormalizeSplitPattern`、`HoistIfThenElse`…），这说明什么？

> **答案**：阶段二大量复用上游 TVM 的通用 TIR 优化（这些 pass 与后端无关），只在「流水/CV/存储/同步」等 Ascend 特有点上插入自定义 pass。tile-lang 的策略是「能复用 TVM 就复用，只在必须处自定义」。

---

### 4.4 PassConfigKey 与配置体系

#### 4.4.1 概念说明

前面三个模块都在讲「pass 干了什么」，本节起回答「**怎么控制一个 pass 开不开**」。

tile-lang 的配置统一用**字符串键**表示，所有合法键集中定义在 `PassConfigKey` 这个 `str, Enum` 里。它有两点设计值得注意：

1. **继承 `str`**：每个枚举成员**本身就是一个字符串**（`PassConfigKey.TL_ASCEND_AUTO_SYNC == "tl.ascend_auto_sync"` 为真），所以既能当字典 key、又能直接传给只认字符串的 TVM C++ 层。
2. **两套命名空间**：`tl.*` 是 tile-lang 自定义配置，`tir.*` 是上游 TVM 既有的配置（如 `tir.disable_vectorize`）。

用户通过 `@tilelang.jit(pass_configs={PassConfigKey.X: True})` 传入一份字典，这份字典最终会原样塞进 `PassContext.config`，C++ pass 用同样的字符串键去读。

#### 4.4.2 核心流程

一个配置项从「用户写出来」到「被 C++ pass 读到」的链路：

```text
@tilelang.jit(pass_configs={PassConfigKey.TL_ASCEND_AUTO_SYNC: True})
        │  （键是 PassConfigKey 枚举成员，值是 True/False/int）
        ▼
tilelang.compile(..., pass_configs=...)            # jit/__init__.py
        │  process_default_pass_config(target, pass_configs)  # 补 per-target 默认值
        ▼
JITKernel(pass_configs=...)                        # kernel.py
        │
        ▼
with tvm.transform.PassContext(opt_level=3, config=pass_configs):  # 建立 PassContext
        tilelang.lower(...)                        # 两阶段 pass 在此上下文内运行
                │
                ▼ （每个 pass 内部）
        ctx->GetConfig<Bool>("tl.ascend_auto_sync", Bool(false))  # C++ 读取
```

#### 4.4.3 源码精读

`PassConfigKey` 把所有合法键集中声明，每个键都带 docstring 说明默认值：

[PassConfigKey 枚举 — tilelang/transform/pass_config.py:L10-L95](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/transform/pass_config.py#L10-L95) —— Ascend 相关键集中在 L38–L60，例如 `TL_ASCEND_AUTO_SYNC`（默认 False）、`TL_ASCEND_MEMORY_PLANNING`（默认 False）、`TL_ASCEND_AUTO_CV_COMBINE`（默认 False）、`TL_ASCEND_TAIL_MASK`（默认 False）。

> ⚠️ **一个易踩的坑**：`TL_ASCEND_AUTO_CV_SYNC` 这个枚举名的**字符串值是 `"tl.ascend_auto_cross_core_sync"`**（L50），名字和值不一致。这是因为「CV 同步」语义上等价于「跨核（cross core）同步」。引用这个键时，C++ 侧（`src/transform/ascend_combinecv.cc`）认的是字符串值，而不是枚举名。

C++ 侧读取配置的标准范式（以 `AscendSyncInsert` 为例）分两步：先用 `TVM_REGISTER_PASS_CONFIG_OPTION` 注册键（让 TVM 知道这个键合法、类型是 Bool），再用 `ctx->GetConfig<Bool>(key, 默认值)` 读取：

[C++ pass 读取配置并自门控 — src/transform/ascend_sync_insert.cc:L46-L71](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_sync_insert.cc#L46-L71) —— L48 注册键 `tl.ascend_auto_sync`，L67–L71 读取它，若为 False 直接 `return f`（原样返回，不改 IR），这就是「自门控」。`AscendMemoryPlanning` 用完全相同的范式（[ascend_memory_planning.cc:L46-L82](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_memory_planning.cc#L46-L82)）。

#### 4.4.4 代码实践

**实践目标**：建立「配置键 ↔ pass ↔ 默认值」三者的对应关系。

**操作步骤**：

1. 打开 [pass_config.py L38–L60](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/transform/pass_config.py#L38-L60)，列出所有 `TL_ASCEND_*` 键及其 docstring 给的默认值。
2. 对每个键，`git grep "<键的字符串值>" src/transform/` 找到消费它的 C++ pass，确认它点亮的是哪个 pass。
3. 画一张三列表：`PassConfigKey 名 | 字符串值 | 点亮的 pass`。

**需要观察的现象 / 预期结果**：会得到类似 `TL_ASCEND_AUTO_SYNC | tl.ascend_auto_sync | AscendSyncInsert`、`TL_ASCEND_AUTO_CV_COMBINE | tl.ascend_auto_cv_combine | CombineCV` 的映射。注意 `TL_ASCEND_AUTO_CV_SYNC` 的值是 `tl.ascend_auto_cross_core_sync` 而非字面的 `cv_sync`。**本实践为源码阅读型，不依赖 NPU。**

#### 4.4.5 小练习与答案

**练习 1**：为什么 `PassConfigKey` 要继承 `str`，而不是普通 `Enum`？

> **答案**：TVM 的 `PassContext.config` 和 C++ 的 `GetConfig` 都以字符串为键。若 `PassConfigKey` 只是普通 Enum，传给 C++ 前要手动 `.value` 取字符串；继承 `str` 后枚举成员**就是**字符串，可以无缝地既当类型安全的 Python 常量、又当 C++ 认的键，省去转换。

**练习 2**：一个 pass「默认关」和「不注册配置项」有什么区别？

> **答案**：默认关的 pass（如 `AscendSyncInsert`）仍然**在流水线里被调用**，只是进入后读配置为 False 就立刻 `return f`，几乎零开销；同时用户随时能通过 `pass_configs={...: True}` 点亮它。若干脆不注册配置项，则没有这个开关旋钮。所以「默认关 + self-gate」是 tile-lang 让实验性 pass 安全常驻流水线的标准做法。

---

### 4.5 配置的传递：`process_default_pass_config` 与 PassContext

#### 4.5.1 概念说明

前节讲了「键怎么定义、怎么读」，本节讲两个剩下的关键问题：

1. **per-target 默认值**：u4-l3 提过「pto 默认开 VS、ascendc 默认全关」——这个「按 target 不同而不同」的默认行为是怎么实现的？答案就是 `process_default_pass_config`。
2. **PassContext 如何把配置送到 C++**：Python 的 `pass_configs` 字典怎么变成 C++ pass 里 `ctx->GetConfig` 能读到的东西？答案是 TVM 的 `PassContext` 机制 + `with` 上下文。

#### 4.5.2 核心流程

**per-target 默认值的填充**发生在 `tilelang.compile()` 最开头，**早于**一切 pass：

```text
tilelang.compile(func, target, pass_configs)
   │
   ├─ pass_configs = process_default_pass_config(target, pass_configs)
   │        │  （target="auto"/"" 视作 "ascendc"）
   │        │  对 _TARGET_PASS_DEFAULTS[target] 里每个键：
   │        │    若用户没显式设过 → 补上默认值
   │        │    若用户已显式设过 → 保留用户值（用户优先）
   │        ▼
   ├─ compile_flags = resolve_compile_flags(target, pass_configs, ...)  # 派生 bisheng 标志
   └─ cached(...)  → JITKernel(pass_configs=...)
```

当前只有一条 per-target 默认值：**pto 默认开启 `TL_ASCEND_AUTO_SYNC_VS`**（ascendc 不开）。`"auto"` 会被当成 `"ascendc"` 处理。

**PassContext 的建立**发生在 `JITKernel` 编译时，用 Python 的 `with` 把配置字典压入「当前 PassContext」，`lower()` 及其内部所有 pass 都在这个上下文里运行，`get_pass_context()` 取出的就是它。

#### 4.5.3 源码精读

per-target 默认值表非常短，目前只有 pto 一项：

[_TARGET_PASS_DEFAULTS 表 — tilelang/transform/pass_config.py:L103-L108](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/transform/pass_config.py#L103-L108) —— `ascendc` 对应空字典（无特殊默认），`pto` 默认开 `TL_ASCEND_AUTO_SYNC_VS`。要给新 target 加默认值，只需在这里加一行。

填充逻辑的核心是「**用户显式设置永远优先**」：

[_apply_target_pass_defaults 的「用户优先」逻辑 — tilelang/transform/pass_config.py:L134-L170](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/transform/pass_config.py#L134-L170) —— L156–L159 先把用户传入的键（可能是 `PassConfigKey` 枚举或裸字符串）规范化成字符串放进新 dict；L165–L168 再遍历 per-target 默认，**只有当该键不在用户 dict 里**才补上。返回的是新 dict，原输入不被修改。L162 还处理了 `"auto"` / 空字符串都视作 `"ascendc"` 的归一化。

对外的薄封装就一行，`compile()` 直接调它：

[process_default_pass_config 入口 — tilelang/transform/pass_config.py:L220-L221](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/transform/pass_config.py#L220-L221) 与 [compile() 调用处 — tilelang/jit/__init__.py:L85-L91](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/__init__.py#L85-L91) —— 注意填充发生在**缓存键计算之前**，所以「pto 默认开 VS」会被算进缓存键，不同 target 的同一 kernel 不会撞缓存。

PassContext 的建立是整条配置链路的「最后一跳」：

[JITKernel 建立 PassContext — tilelang/jit/kernel.py:L227-L233](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/kernel.py#L227-L233) —— `with tvm.transform.PassContext(opt_level=3, config=pass_configs)` 把（已被填充默认值的）配置字典挂到当前上下文；紧接着的 `tilelang.lower(...)` 在此上下文内运行，于是阶段一的 `AscendInferBufferScope`、阶段二的 `AscendSyncInsert` 等所有 pass 都能用 `ctx->GetConfig<Bool>(...)` 读到这份配置。阶段二里 `get_pass_context()`（[__init__.py:L25-L27](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/transform/__init__.py#L25-L27)，本质是 `PassContext.current()`）取出的也正是这个上下文。

> 旁注：除了 pass 配置，`lower()` 还会把 `platform`（A2/A3/A5）写进 PrimFunc 的 `npu_platform` 属性（见 4.1.3），这同样是「让 per-target 信息到达 C++ pass」的一条通道，只是走的是函数属性而非 PassContext.config。`AscendSyncInsertVS` 对 A5 的特殊处理就走这条通道。

#### 4.5.4 代码实践

**实践目标**：用项目自带的测试，亲眼验证 `process_default_pass_config` 的「per-target 默认 + 用户优先」行为，**无需 NPU**。

**操作步骤**：

1. 阅读 [test_ascend_compile_flags.py 的参数化用例 — testing/python/language/test_ascend_compile_flags.py:L51-L70](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/testing/python/language/test_ascend_compile_flags.py#L51-L70)，其中 `_pc(target, vs)` 就是包装过的 `process_default_pass_config`。
2. 重点看这一组断言（注释已点明语义）：

   ```python
   ("ascendc", False, {}, None, ["-O2"]),               # ascendc 不开 VS
   ("ascendc", True,  {}, None, ["-O3", "--cce-auto-sync=off"]),  # VS 配对
   ("pto",    None,   {}, None, ["-O3"]),               # pto 默认 VS 开
   ```

   理解：`target="pto", vs=None` 表示「不显式设 VS、用 pto 自己的默认」，结果 VS 被默认点亮。

3. 运行该文件里的纯逻辑测试（不依赖 NPU 的那些）：

   ```bash
   # 示例命令（非项目脚本，仅运行标志解析相关的纯逻辑用例）
   pytest testing/python/language/test_ascend_compile_flags.py \
       -k "resolve_compile_flags or no_environ_mutation or cache_key"
   ```

**需要观察的现象**：`process_default_pass_config("pto", None)` 返回的 dict 里会自动多出 `tl.ascend_auto_sync_vs: True`；而 `process_default_pass_config("ascendc", None)` 返回空 dict。

**预期结果**：上述纯逻辑用例全部通过。**若环境未装 tile-lang wheel，pytest 无法运行，属「待本地验证」**；但通过阅读断言即可确认行为。

#### 4.5.5 小练习与答案

**练习 1**：用户写 `@tilelang.jit(target="pto", pass_configs={PassConfigKey.TL_ASCEND_AUTO_SYNC_VS: False})`，最终 VS 是开还是关？

> **答案**：**关**。因为 `_apply_target_pass_defaults` 的 L165–L168 只在「用户没设过」时才补默认值；用户显式写了 `False`，pto 的默认 `True` 不会覆盖它。这就是「用户优先」原则。

**练习 2**：为什么 `process_default_pass_config` 必须在**缓存键计算之前**调用（见 `compile()` 的顺序）？

> **答案**：因为 per-target 默认值会影响生成的代码（如 pto 默认开 VS，会改变同步 pass 行为和 bisheng 标志）。如果先算缓存键再补默认，两个 target 的同一 kernel 可能算出相同缓存键却生成不同代码，造成缓存污染。先补默认、再算键，才能保证「相同配置→相同键、不同配置→不同键」。

---

## 5. 综合实践

本讲的核心实践任务是规格里指定的**对照 `phase.py` 列出两阶段全部 pass，并解释四个代表 pass 各自落在哪个阶段、为什么**。请按下表完成：

**步骤 1 — 列全 pass**：打开 [phase.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py)，把 `LowerAndLegalize`（L49–L90）与 `OptimizeForTarget`（L93–L121）里**每一个** pass 抄进一张表，标注：阶段、序号、pass 名、一行职责（看注释）、是否 self-gate（去对应 `.cc` 里找 `GetConfig`）。

**步骤 2 — 定位四个代表 pass 并解释**：

| pass | 所在阶段 / 行号 | 为什么排在这里 |
|------|----------------|----------------|
| `AscendVidReduction` | LowerAndLegalize / L54 | 属「语义降级」：要把 UB shape 减半、注入 vid 偏移，必须在结构还灵活时、且紧跟 `AscendInferBufferScope`（L52，刚推断出谁是 UB）之后；它不能放到阶段二，因为阶段二的流水/扁平化都假设 UB 形状已定。 |
| `AscendLowerParallelToVector` | LowerAndLegalize / L65 | 属「向量化降级」：把 `T.Parallel` 循环翻译成向量指令，是 `LayoutInference`（L67）与 `LowerTileOp`（L70）的输入，必须在它们之前；放阶段二毫无意义，因为阶段二不再处理高层循环语义。 |
| `AscendMemoryPlanning` | OptimizeForTarget / L117 | 属「硬件优化-内存」：做激进的缓冲复用，必须在 `AscendStorageRewrite`（L110，先铺地址）之后、`AscendSyncInsert`（L118，依据最终 buffer 关系建依赖图）之前；它 self-gate（`tl.ascend_memory_planning` 默认关）。 |
| `AscendSyncInsert` | OptimizeForTarget / L118 | 属「收尾-同步」：靠数据依赖 + 地址别名检测冒险插同步，**必须**在所有结构性改写（流水、扁平化、存储、内存规划）都完成之后，否则同步图会被推翻；所以它和 `AscendSyncInsertVS`（L119）紧挨着排在阶段二最后。它 self-gate（`tl.ascend_auto_sync` 默认关）。 |

> **判断口诀**：问自己「这个 pass 改的是**语义结构**还是**硬件执行细节**？」前者（降级、scope 推断、向量化、tile op 展开、跨核中转）进 `LowerAndLegalize`；后者（流水重叠、缓冲复用、同步插入）进 `OptimizeForTarget`。再问「它依赖谁的产出 / 谁依赖它的产出？」决定阶段内的相对顺序。

**步骤 3 — 验证（可选，需 NPU）**：参考 [test_ascend_compile_flags.py:L253-L310](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/testing/python/language/test_ascend_compile_flags.py#L253-L310) 的 `_build_sync_dependency_kernel`，分别用 `TL_ASCEND_AUTO_SYNC=True/False` 编译同一个 kernel，用 `get_kernel_source()` 对比生成代码里是否多了 `PipeBarrier/SetFlag/WaitFlag`——这能直观看到「阶段二的 pass 受 4.4/4.5 的配置控制」。

## 6. 本讲小结

- tile-lang 把 TIR 改写拆成**两阶段**：`LowerAndLegalize`（降级 + 合法化，15 步）让 IR 正确合法，`OptimizeForTarget`（硬件优化，20 步）让它跑得快，最后 `device_codegen` 按 `target.model` 分发 ascendc/pto 两条 codegen。两阶段顺序、阶段内 pass 顺序都是硬依赖，不可随意调换。
- `AscendVidReduction`、`AscendLowerParallelToVector` 落在阶段一（语义降级），`AscendMemoryPlanning`、`AscendSyncInsert` 落在阶段二（硬件优化/收尾）；判断依据是「改语义结构 vs 改执行细节」+「依赖关系」。
- 配置体系统一用字符串键，集中定义在 `PassConfigKey`（继承 `str` 的枚举），分 `tl.*`（tile-lang）与 `tir.*`（TVM）两套命名空间；C++ pass 用 `TVM_REGISTER_PASS_CONFIG_OPTION` 注册键、用 `ctx->GetConfig<Bool>(key, 默认)` 读取并自门控。
- `process_default_pass_config` 按 target（ascendc/pto/auto）在 `compile()` 最开头填充 per-target 默认值（目前仅 pto 默认开 `TL_ASCEND_AUTO_SYNC_VS`），「用户显式设置永远优先」，且填充发生在缓存键计算之前以防缓存污染。
- 配置字典经 `with tvm.transform.PassContext(opt_level=3, config=pass_configs)` 压入上下文，`lower()` 及其所有 pass 在此上下文内运行；`platform` 则经 PrimFunc 的 `npu_platform` 属性这条独立通道到达 C++ pass。

## 7. 下一步学习建议

- **u6-l2（Ascend C / PTO 双 Codegen）**：本讲停在 `device_codegen` 门口，下一讲就拆开 `target.build.tilelang_ascend` 与 `target.build.tilelang_ascend_pto` 两个 codegen，看 TIR 如何变成 Ascend C 源码与 PTO IR。
- **u6-l5（内存规划与存储重写）**：想深入本讲只点到为止的 `AscendMemoryPlanning` 与 `AscendStorageRewrite`，看它们如何降低片上内存占用。
- **u6-l6（Tile Op lowering 与 Tail Mask）**：想看清阶段一里 `LowerTileOp`、`AscendTailMaskPropagation`、`LegalizeVectorizedLoop`、`LegalizeSafeMemoryAccess` 这组合法化四件套的内部细节。
- **回看 u4-l3**：带着本讲「整条流水线」的视角重读 `AscendSyncInsert`，你会更清楚它为什么必须排在阶段二最末、以及它和 `AscendSyncInsertVS` 的紧邻关系。
- **动手**：试着给 `LowerAndLegalize` 或 `OptimizeForTarget` 加一个临时的 `print(mod.script())` 断点（只读实验，**不要提交**），对比某个 pass 前后的 IR 差异——这是理解 pass 行为最直接的方式。
