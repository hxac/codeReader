# Cube/Vector 分离与 CombineCV

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 Ascend NPU 上 **Cube 核（AIC）** 与 **Vector 核（AIV）** 是如何协同的：同一份编译产物、两核各跑一半，靠 `resource_scope` 属性在 codegen 时切分。
- 区分 **两条 CV 分离路线**：Expert 模式手写 `T.Scope("C"/"V")`（u4-l1 已建立）与 Developer 模式由 `CombineCV` pass **自动分离**。
- 掌握 `CombineCV` pass 的工作原理：它如何把一段「混合」的 kernel 体，按算子/缓冲归属拆成 cube 与 vec 两份代码，并用 `resource_scope` 包裹。
- 理解两个控制开关——`TL_ASCEND_AUTO_CV_COMBINE`（开启自动 CV 分离）与 `TL_ASCEND_AUTO_CV_SYNC`（开启自动核间同步）——分别触发什么、缺一不可的关系。
- 会用 `get_kernel_source()` 在生成的 C++ 代码中确认 Cube 与 Vector 被分到了不同的 scope。

本讲是 u5（CV 分离与跨核机制）单元的第一讲，承接 u4-l1（Expert 内存分配与 `T.Scope`）、u4-l2（同步原语）、u4-l3（自动同步插入），为 u5-l2（跨核流水）、u5-l3（vid 消除）、u5-l4（workspace 消除）铺垫。

## 2. 前置知识

在进入源码前，先用三段话把「为什么需要 CV 分离」讲透。

**硬件侧：一个 AI Core 里有两类计算单元。** Ascend A2/A3 的一个 AI Core 内部，Cube 单元（AIC，负责矩阵乘 `Mmad`）与 Vector 单元（AIV，负责逐元素、reduce 等向量指令）**并行**存在，且二者 **不能直接交换数据**。Cube 的结果落在 L0C，Vector 的数据在 Unified Buffer（UB），二者之间唯一的通道是 **Global Memory（GM）/L2**：Cube 先把 L0C 写回 GM 的 workspace，Vector 再从 GM 读进 UB。这就是 u3-l2 讲过的「跨 CV 搬运」物理本质。

**CV 核配比（ratio）。** 一个 AI Core 固定有 1 个 Cube，但有 1 个或 2 个 Vector 子核，即配比 `1:1` 或 `1:2`。这正是 u2-l2 讲的 `threads=1`/`threads=2` 的硬件来源：`threads=2` 声明 `1:2` 配比。编程指南明确指出这一点：

> 因为 A2/A3 的 CV 核配比可以为 1:2 或 1:1，可以通过 vid 指定当前 vector 的索引。
> ——[docs/TileLang-Ascend Programming Guide.md:344](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L344)

**软件侧：同一份 kernel，两核各取所需。** tile-lang 不为 Cube 和 Vector 各编一份 binary，而是把两者的代码 **拼在同一份 `.so`** 里，用宏在运行期挑选：

- Ascend C 后端生成 `if ASCEND_IS_AIC { ... }` / `if ASCEND_IS_AIV { ... }`；
- PTO 后端生成 `#if defined(__DAV_<arch>_CUBE__) ... #endif` / `#if defined(__DAV_<arch>_VEC__) ... #endif`。

物理上 Cube 核跑 AIC 分支、Vector 核跑 AIV 分支，它们通过 GM workspace 完成数据交接。**「CV 分离」就是把一段逻辑连续的 kernel 体，拆成「归 Cube 的语句」和「归 Vector 的语句」两组，分别裹进这两个宏分支。**

那么谁来拆？两条路：

| 路线 | 谁负责拆 | 标志 |
|------|----------|------|
| Expert 模式 | **用户手写** `T.Scope("C")`/`T.Scope("V")` 直接贴 `resource_scope` 属性 | 显式控制 |
| Developer 模式 | **`CombineCV` pass 自动拆**，用户什么都不用写 | `TL_ASCEND_AUTO_CV_COMBINE=True` |

本讲的主角就是 Developer 模式下的 `CombineCV` pass，以及配套的自动核间同步 `TL_ASCEND_AUTO_CV_SYNC`。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/transform/ascend_combinecv.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_combinecv.cc) | 本讲核心。`CombineCV` pass、`CVCombineEmitter`（拆分）、`AutoInsertCrossCoreSync`（自动核间同步）全部在此。 |
| [examples/developer_mode/matmul_add_developer.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/matmul_add_developer.py) | Developer 模式 `matmul + add` 示例，同时跨越 Cube（gemm）与 Vector（add）两个 scope，是验证 CV 分离的最佳样本。 |
| [tilelang/engine/phase.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py) | `OptimizeForTarget` 阶段里 `CrossCorePipeline → CombineCV` 的调用顺序。 |
| [tilelang/transform/pass_config.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/transform/pass_config.py) | 两个开关 `TL_ASCEND_AUTO_CV_COMBINE` / `TL_ASCEND_AUTO_CV_SYNC` 的定义。 |
| [src/target/codegen_ascend.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc) | Ascend C codegen 把 `resource_scope` 翻译成 `if ASCEND_IS_AIC/AIV`。 |
| [src/target/codegen_ascend_pto.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend_pto.cc) | PTO codegen 把 `resource_scope` 翻译成 `#if defined(__DAV_*_CUBE/VEC__)`。 |
| [src/ir.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/ir.cc) | `T.Scope` 前端，Expert 模式贴 `resource_scope` 的入口。 |

## 4. 核心概念与源码讲解

### 4.1 resource_scope 属性：CV 分离的「唯一契约」

#### 4.1.1 概念说明

无论 Expert 还是 Developer 模式，CV 分离最终都归结为同一个 TIR 层的契约：给一段语句贴上 `resource_scope` 属性，取值 `0`（Cube/AIC）或 `1`（Vector/AIV）。下游所有 pass（同步插入、存储重写）与 codegen 都只认这个属性。

- Expert 模式：`T.Scope("C")` / `T.Scope("V")` 在前端解析时直接贴属性。
- Developer 模式：用户不写，由 `CombineCV` pass 推断后贴。

理解这一点很关键：**`CombineCV` pass 的全部产出，就是两个 `resource_scope` 属性包裹的语句块。** 之后的 codegen 与运行期行为，两条路线完全一致。

#### 4.1.2 核心流程

Expert 模式贴属性的入口在 `Scope()` 函数：

```text
T.Scope("C")  →  Attr("resource_scope", 0)   # Cube
T.Scope("V")  →  Attr("resource_scope", 1)   # Vector
```

codegen 收到该属性后，按后端选不同语法：

- **Ascend C**：`resource_scope==0` → `if ASCEND_IS_AIC { ... }`；`==1` → `if ASCEND_IS_AIV { ... }`。
- **PTO**：`==0` → `#if defined(__DAV_<arch>_CUBE__) ... #endif`；`==1` → `#if defined(__DAV_<arch>_VEC__) ... #endif`，且 Vector 分支开头自动插入 `set_mask_norm()`/`set_vector_mask(-1,-1)`。

编译出的 `.so` 只有一份，Cube 核与 Vector 核加载同一份产物，靠宏各自命中自己的分支。

#### 4.1.3 源码精读

Expert 模式 `T.Scope` 的实现——`scope_name=="V"` 时 `scope_id=1`，否则 `0`，封进 `Attr("resource_scope", scope_id)`：

[src/ir.cc:495-506](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/ir.cc#L495-L506) —— 中文说明：前端 `T.Scope` 把 scope 名（"C"/"V"）转成 `resource_scope` 属性值（0/1），这是 Expert 模式贴属性的唯一点。

Ascend C codegen 消费该属性——`0` 取名 `AIC`、`1` 取名 `AIV`，打印 `if ASCEND_IS_<name> {`：

[src/target/codegen_ascend.cc:787-799](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L787-L799) —— 中文说明：codegen 把 `resource_scope` 翻成 `if ASCEND_IS_AIC/AIV { ... }` 的宏分支，Cube/Vector 核运行时各命中一段。

PTO codegen 消费该属性——`0` 取名 `CUBE`、`1` 取名 `VEC`，并按平台拼成 `__DAV_C220_/C310_` 宏，Vector 分支额外设 mask：

[src/target/codegen_ascend_pto.cc:3620-3643](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend_pto.cc#L3620-L3643) —— 中文说明：PTO 路线用 `#if defined(__DAV_<arch>_CUBE/VEC__)` 切分，并把当前 scope 记进 `current_resource_scope_` 供指令生成参考。

#### 4.1.4 代码实践

**实践目标**：确认「同一份 kernel 源码、靠宏切分两核」这一机制真实存在。

**操作步骤**：

1. 打开 `examples/developer_mode/matmul_add_developer.py`，运行它（详见 4.2.4）。
2. 在脚本末尾加一行打印生成代码：
   ```python
   print(func.get_kernel_source()[0].source)  # 示例代码：打印第一份生成源码
   ```
3. 在输出的 C++ 源码里搜索 `ASCEND_IS_AIC` 与 `ASCEND_IS_AIV`（Ascend C 后端）。

**需要观察的现象**：源码里同时出现两个 `if` 分支——一个里是 `Mmad`/`copy_gm_to_l1` 等 Cube 操作，另一个里是 `Add`/`copy_gm_to_ub` 等 Vector 操作。

**预期结果**：两段代码共享同一份 `call` 入口、同一份 buffer 声明，靠 `if ASCEND_IS_AIC { ... } if ASCEND_IS_AIV { ... }` 区分。若使用 PTO 后端，则看到的是 `#if defined(__DAV_C220_CUBE__)` / `#if defined(__DAV_C220_VEC__)`。

> 若本地无 NPU，可只编译不运行：`get_kernel_source()` 在 `lower` 完成后即可调用，无需真实设备。无法确认运行结果时标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `resource_scope` 属性值写反（Cube 段贴成 `1`、Vector 段贴成 `0`），运行时会发生什么？

**参考答案**：两核仍各跑一段，但跑反了——Cube 核去执行 Vector 指令（`Add` 等），Vector 核去执行 Cube 指令（`Mmad`）。由于不同核的指令集与可用存储不同，多半会触发非法指令或访存错误。这说明 `resource_scope` 是纯软件约定、靠宏在运行期挑选，本身不做任何正确性校验。

**练习 2**：为什么 tile-lang 不为 Cube 和 Vector 分别编译两份 `.so`？

**参考答案**：因为 Cube 与 Vector 同属一个 AI Core、共享 cid 与 GM workspace、需要协同完成一个 tile 的计算。两核必须看到一致的内存布局与启动参数，编成同一份产物、用宏切分，既保证一致性，又减少 host 侧 launch 与缓存管理的开销。

---

### 4.2 CombineCV pass —— Developer 模式自动 CV 分离（TL_ASCEND_AUTO_CV_COMBINE）

#### 4.2.1 概念说明

Developer 模式下，用户写的是一段 **逻辑连续、混在一起** 的 kernel 体：一会儿 `T.gemm_v0`（Cube），一会儿 `T.copy(C_L0, c_ub)`（跨 CV），一会儿 `T.tile.add`（Vector）。用户并不显式声明哪句归 Cube、哪句归 Vector。

`CombineCV` pass 的工作就是 **替用户把这段混合体拆开**：

1. 生成两份代码 `cube_code` 与 `vec_code`，都来自 **同一个 body**；
2. cube 侧只保留 Cube 操作（gemm、`copy_gm_to_l1`、`copy_l0c_to_gm`、L1/L0 buffer 访问），Vector 操作置空（`Evaluate(0)`）；
3. vec 侧只保留 Vector 操作（`copy_gm_to_ub`、`copy_ub_to_gm`、UB 访问、`tile.*`），Cube 操作置空；
4. 把两份代码分别裹进 `resource_scope=0` 与 `resource_scope=1`，串成一个 `SeqStmt`。

这个 pass 受开关 `TL_ASCEND_AUTO_CV_COMBINE`（注册名 `tl.ascend_auto_cv_combine`，默认 `False`）控制——**不开就不拆**，整个 body 视作单 scope（即纯 Cube 或纯 Vector kernel）。

#### 4.2.2 核心流程

拆分的判定依据有两套，组合使用：

**依据一：算子名 → 位置映射表 `callnodeMapPos_`。** pass 内置一张表，把每个底层 intrinsic 映射到 `"cube"` 或 `"vec"`：

| intrinsic | 归属 |
|-----------|------|
| `copy_gm_to_l1` / `copy_l1_to_l0a` / `copy_l1_to_l0b` / `copy_l0c_to_gm` / `gemm_v0` | cube |
| `copy_gm_to_ub` / `copy_ub_to_gm` / `copy_ub_to_ub` / `atomic_add_ub_to_gm` | vec |
| `atomic_add_l0c_to_gm` | cube |
| `shared.l1` / `wmma.*` | cube |
| `shared.ub` | vec |

遇到一个 `EvaluateNode`（函数调用语句），查表得知它是 cube 还是 vec。在生成 cube 代码时，vec 语句被替换为 `Evaluate(0)`（空操作）并「关掉开关」；反之亦然。

**依据二：buffer scope 反查 `checkBufferScope`。** 表里没直接命中的语句，pass 会取其参数 buffer 的 scope（从 `location_map_` 查），`shared.l1`→cube、`shared.ub`→vec，再决定保留与否。

**特殊保留：`printf`。** 调试打印 `tl.ascend_printf` 不属于任何 scope，两份代码都保留（`IsRetainedInBothScopes`）。

整体流程伪代码：

```text
CombineCV::VisitStmt_(BlockRealize "tilelang_root"):
    cube_code = CVCombineEmitter(is_aiv=false).Visit(body)   # 仅留 cube 语句
    vec_code  = CVCombineEmitter(is_aiv=true ).Visit(body)   # 仅留 vec 语句
    if auto_cross_core_sync:                                  # 见 4.3
        AutoInsertCrossCoreSync(cube_code, vec_code)
    cube_body = Attr("resource_scope", 0, cube_code)
    vec_body  = Attr("resource_scope", 1, vec_code)
    block.body = SeqStmt({cube_body, vec_body})
```

注意它在 pass 流水线中的位置：`CombineCV` 紧跟在 `CrossCorePipeline` 之后，且 **早于** `PipelinePlanning`/`InjectSoftwarePipeline`（软件流水）与 `AscendSyncInsert`（核内自动同步）：

[tilelang/engine/phase.py:98-99](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py#L98-L99) —— 中文说明：`OptimizeForTarget` 阶段里 `CrossCorePipeline` 先规划跨核流水，紧接着 `CombineCV` 把体拆成 Cube/Vector 两 scope，后续的软件流水与同步插入再分别作用于两份代码。

#### 4.2.3 源码精读

**开关注册与读取**。两个配置项都通过 `TVM_REGISTER_PASS_CONFIG_OPTION` 注册成 PassContext 配置：

[src/transform/ascend_combinecv.cc:32-39](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_combinecv.cc#L32-L39) —— 中文说明：`tl.ascend_auto_cv_combine`（CV 分离总开关）与 `tl.ascend_auto_cross_core_sync`（自动核间同步开关）在此注册，默认都为 `False`。

`Substitute()` 入口读这两个开关；`ascend_auto_cv_combine` 为假则 **直接返回原函数**，不做任何拆分：

[src/transform/ascend_combinecv.cc:832-842](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_combinecv.cc#L832-L842) —— 中文说明：先把所有 buffer 的 scope 收进 `location_map_`（供反查），再读两个开关；CV 分离关闭时直接返回。

**拆分核心：`CVCombineEmitter` 的「保留/置空」逻辑**。处理一个函数调用语句时，按「我当前生成的是哪一侧」+「这条语句属于哪一侧」决定保留还是置空：

[src/transform/ascend_combinecv.cc:702-753](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_combinecv.cc#L702-L753) —— 中文说明：先查算子名表（judgement 1），命中则「属于本侧就开开关、属于对侧就关开关」；命中不到再查参数 buffer 的 scope（judgement 2）；既不属于本侧、当前开关又关着，就把这条语句替换成空操作 `Evaluate(0)`。

**buffer 读写按 scope 切分**。对 `BufferStore`，按 buffer 的 `scope()` 决定——生成 vec 代码时只保留 `shared.ub` 的写，生成 cube 代码时只保留非 `shared.ub` 的写：

[src/transform/ascend_combinecv.cc:755-770](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_combinecv.cc#L755-L770) —— 中文说明：`BufferStore` 的归属完全由 buffer scope 决定，UB 写归 Vector、L1/L0 写归 Cube。

**算子→位置映射表**。这张表是「哪些算子算 Cube、哪些算 Vector」的权威清单：

[src/transform/ascend_combinecv.cc:790-814](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_combinecv.cc#L790-L814) —— 中文说明：`callnodeMapPos_` 把每个底层搬运/计算/gemm intrinsic 标记为 cube 或 vec，是拆分判定的第一依据。

**贴属性收尾**。拆出 `cube_code`/`vec_code` 后，分别裹 `resource_scope=0/1` 并串成 `SeqStmt`：

[src/transform/ascend_combinecv.cc:848-873](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_combinecv.cc#L848-L873) —— 中文说明：命中 `tilelang_root` block 时，用两个 `CVCombineEmitter`（`is_aiv=false/true`）分别生成 cube 与 vec 代码，可选地插核间同步，再各裹 `resource_scope` 属性、拼成一个序列——这就是 Developer 模式 CV 分离的最终产出。

**pass 注册**。Python 侧 `tilelang.transform.CombineCV()` 经 FFI 调到 C++ 的 `CombineCV()`：

[src/transform/ascend_combinecv.cc:883-892](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_combinecv.cc#L883-L892) —— 中文说明：`CombineCV` 注册为 `CreatePrimFuncPass`，名字 `tl.CombineCV`，并暴露为全局 `tl.transform.CombineCV`。

[tilelang/transform/__init__.py:395-403](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/transform/__init__.py#L395-L403) —— 中文说明：Python 侧薄封装，转发到 FFI。

#### 4.2.4 代码实践

**实践目标**：用 Developer 模式写一个跨 Cube/Vector 的 `matmul + add`，开启 `TL_ASCEND_AUTO_CV_COMBINE`，在生成代码里确认 Cube 与 Vector 被拆进了不同 scope。

**操作步骤**：

1. 直接运行现成示例（它已经把所需开关全开）：
   ```bash
   python examples/developer_mode/matmul_add_developer.py
   ```
   预期看到 `init successful!` 与 `Kernel Output Match!`。
2. 阅读该示例的 pass 配置与 kernel 体，理解「为什么它需要 CV 分离」：

   [examples/developer_mode/matmul_add_developer.py:9-15](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/matmul_add_developer.py#L9-L15) —— 中文说明：一次性开齐四个相关开关——`AUTO_CV_COMBINE`（拆 CV）、`AUTO_SYNC`（核内自动同步）、`MEMORY_PLANNING`（缓冲复用）、`AUTO_CV_SYNC`（核间自动同步）。

   [examples/developer_mode/matmul_add_developer.py:52-66](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/matmul_add_developer.py#L52-L66) —— 中文说明：先在 K 循环里 `T.gemm_v0`（Cube）累加到 `C_L0`，再把 `C_L0` 搬到 `c_ub`（跨 CV）、把 `D` 搬到 `d_ub`，最后 `T.tile.add`（Vector）并写回 GM——天然横跨 Cube 与 Vector。
3. 在脚本末尾追加（示例代码）：
   ```python
   src = func.get_kernel_source()[0].source
   print(src)
   ```
4. 在打印的 C++ 里定位 `ASCEND_IS_AIC`（或 PTO 的 `__DAV_*_CUBE__`）分支，确认里面只有 `Mmad`、`copy_gm_to_l1` 等；再定位 `ASCEND_IS_AIV` 分支，确认里面有 `Add`、`copy_gm_to_ub` 等。

**需要观察的现象**：原本一段连续的 kernel 体，在生成代码里变成了两个互斥的宏分支，Cube 操作与 Vector 操作被严格分到两边。

**预期结果**：两个分支共存于同一函数，`C = A@B` 的累加在 Cube 分支、`+ D` 在 Vector 分支，`C_L0 → c_ub` 的搬运被 workspace 消除（u5-l4 详述）翻译成了 Cube 写 GM / Vector 读 GM 的两阶段。

> 无 NPU 时仍可走 `get_kernel_source()` 观察生成代码（只编译不运行）；运行验证「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `T.copy(C_L0, c_ub)`（L0C→UB）这条语句不会出现在最终生成代码的任何一侧？

**参考答案**：因为 L0C 属 Cube、UB 属 Vector，这条搬运是「跨 CV」的，物理上无法直连。它会被更早的 `AscendWorkspaceReduction` pass（见 u5-l4）拆成两阶段：`copy_l0c_to_gm`（写 workspace，归 Cube）与 `copy_gm_to_ub`（读 workspace，归 Vector）。所以 `CombineCV` 看到的已经是拆完后的两条 GM 搬运，自然一条落 Cube、一条落 Vector。

**练习 2**：若把 `TL_ASCEND_AUTO_CV_COMBINE` 关掉（其余开关不变），这个 `matmul + add` kernel 会怎样？

**参考答案**：`CombineCV` 直接返回原函数，不拆 scope，整个 body 不带 `resource_scope` 属性。codegen 不会生成 `if ASCEND_IS_AIC/AIV` 切分，所有语句会被当作单 scope 处理；跨 CV 的搬运、`tile.add` 与 `gemm` 混在一起无法落到正确的核，编译或运行会出错。这正是「跨核流水/混合 CV kernel 必须开 CV 分离」的原因。

---

### 4.3 自动核间同步 TL_ASCEND_AUTO_CV_SYNC

#### 4.3.1 概念说明

CV 分离解决了「哪句归哪个核」，但没解决 **两核之间的数据可见性**。回到硬件事实：Cube 写完 GM workspace，Vector 怎么知道数据就绪、可以读了？二者并行运行、无隐式 ordering，必须显式同步。

这正是 u4-l2 讲的手写 `T.set_cross_flag` / `T.wait_cross_flag` 的场景。`TL_ASCEND_AUTO_CV_SYNC`（注册名 `tl.ascend_auto_cross_core_sync`，默认 `False`）让编译器 **自动** 插好这对 flag，免去手写。README 把两个开关并列为「自动插 AIC↔AIV 同步」的必要条件：

[README.md:587-598](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/README.md#L587-L598) —— 中文说明：要自动插入 `CrossCoreSetFlag`/`CrossCoreWaitFlag`，需同时开 `TL_ASCEND_AUTO_CV_COMBINE` 与 `TL_ASCEND_AUTO_CV_SYNC`。

编程指南更进一步强调：用核间流水时这俩 **必须** 同时开：

> 使用核间流水线时，必须开启自动 CV 分离和自动 CV 间同步插入功能：`"tl.ascend_auto_cv_combine": True, "tl.ascend_auto_cross_core_sync": True`
> ——[docs/TileLang-Ascend Programming Guide.md:1175](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L1175)

#### 4.3.2 核心流程

自动核间同步的关键洞察是：**Cube 与 Vector 之间唯一的耦合点，就是对同一块 GM workspace 的「写→读」**。因此 pass 以 workspace 名字为键，把两侧的搬运语句两两配对，再插 flag。

具体三步：

1. **收集同步点（`CrossCoreSyncCollector`）**：分别遍历 cube_code 与 vec_code，找出所有「与 GM 相关」的搬运语句。一张配置表给出每条搬运是写还是读、属于哪条 pipe（MTE2/FIX/MTE3）：

   | 搬运 | 写/读 | pipe |
   |------|-------|------|
   | `copy_gm_to_l1` | 读 | MTE2 |
   | `copy_l0c_to_gm` | 写 | FIX |
   | `copy_gm_to_ub` | 读 | MTE2 |
   | `copy_ub_to_gm` | 写 | MTE3 |
   | `atomic_add_ub_to_gm` | 写 | MTE3 |
   | `atomic_add_l0c_to_gm` | 写 | FIX |

   只有参数里带 `workspace` 名字的搬运才算同步点（`FetchWorkspaceName`）。

2. **按 workspace 配对（`AutoInsertCrossCoreSync::AutoInsert`）**：把两侧同步点按 workspace 名分组。同一 workspace 下，cube 与 vec 的同步点 **数量必须相等**、且 **必须是一写一读**，否则 `LOG(FATAL)`。每对分配一个共享的 `sync_flag_id`。

3. **插 flag（`CrossCoreSyncInserter`）**：
   - **写方**：搬运语句 **之后** 插 `tl.ascend_auto_set_cross_flag`（置位）；
   - **读方**：搬运语句 **之前** 插 `tl.ascend_auto_wait_cross_flag`（等待）；
   - 配合 `cross_interval`（跨核流水里每 N 次迭代才同步一次）时，flag 会被包进条件分支：写方在 `stage_var % interval == interval-1` 或最后一轮置位、读方在 `stage_var % interval == 0` 等待。

`FindTargetLoopDepth` 还会把 flag 尽量外提到合适的循环层（在两侧行为对称的最深共同循环上），减少 flag 总数。

最终这对 intrinsic 经 codegen 落成与 u4-l2 手写同族的 `AscendC::CrossCoreSetFlag`/`CrossCoreWaitFlag`（Ascend C）或 PTO 的 `set/wait_cross_flag` 指令——**语义一致、名异**。

#### 4.3.3 源码精读

**GM 搬运的写/读与 pipe 配置表**：

[src/transform/ascend_combinecv.cc:167-175](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_combinecv.cc#L167-L175) —— 中文说明：`GM_COPY_CFG_INFOS` 把六类 GM 搬运标成写/读并绑定到具体硬件 pipe（MTE2/FIX/MTE3），是判定同步点性质的基础。

**按 workspace 配对并分配 flag id**：

[src/transform/ascend_combinecv.cc:370-400](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_combinecv.cc#L370-L400) —— 中文说明：以 workspace 名分组，校验两侧同步点「数量相等、读写互补」，给每对分配共享 `sync_flag_id`，并求解 flag 该外提到哪层循环。

**生成 set/wait flag 语句**：

[src/transform/ascend_combinecv.cc:321-341](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_combinecv.cc#L321-L341) —— 中文说明：分别发射 `tl.ascend_auto_set_cross_flag`（带 model id、pipe、flag id）与 `tl.ascend_auto_wait_cross_flag`，这两个 intrinsic 后续由 codegen 翻成 `CrossCoreSetFlag/WaitFlag`。

**插 flag 的方向：写后置位、读前等待**：

[src/transform/ascend_combinecv.cc:262-289](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_combinecv.cc#L262-L289) —— 中文说明：`AttachSyncStmt` 规定——写方「先搬、后置位」，读方「先等待、后搬」；带 `cross_interval` 时 flag 被条件化（`GenSyncCondition`），实现跨核流水的稀疏同步。

**在 CombineCV 内的触发点**：只有 `ascend_auto_cross_core_sync` 为真时，才对拆出的两份代码做同步插入：

[src/transform/ascend_combinecv.cc:858-866](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_combinecv.cc#L858-L866) —— 中文说明：CV 拆分完成后、贴 `resource_scope` 之前，若开了核间同步就调用 `AutoInsertCrossCoreSync::AutoInsert(cube_code, vec_code)`，这正是两开关「必须同开」的实现原因——同步依赖拆分产出的两份代码。

#### 4.3.4 代码实践

**实践目标**：对比 `TL_ASCEND_AUTO_CV_SYNC` 开与关时，生成代码里 `CrossCoreSetFlag/WaitFlag` 的有无。

**操作步骤**：

1. 复制 `matmul_add_developer.py` 为 `matmul_add_nosync.py`（示例代码，仅用于本地对比，勿提交）。
2. 在副本里把 `TL_ASCEND_AUTO_CV_SYNC` 改成 `False`，其余不变：
   ```python
   pass_configs = {
       tilelang.PassConfigKey.TL_ASCEND_AUTO_CV_COMBINE: True,
       tilelang.PassConfigKey.TL_ASCEND_AUTO_SYNC: True,
       tilelang.PassConfigKey.TL_ASCEND_MEMORY_PLANNING: True,
       tilelang.PassConfigKey.TL_ASCEND_AUTO_CV_SYNC: False,  # 关掉核间同步
   }
   ```
3. 两份脚本分别 `print(func.get_kernel_source()[0].source)`。
4. 在两份输出里搜索 `SetFlag` / `CrossCoreSetFlag`（Ascend C）或 `set_cross`（PTO）。

**需要观察的现象**：开启版在 Cube 写 workspace 之后、Vector 读 workspace 之前，各有一对 `SetFlag`/`WaitFlag`（flag id 相同）；关闭版完全没有这对调用。

**预期结果**：开启版能正确运行并 `Kernel Output Match!`；关闭版由于 Cube/Vector 间无数据可见性保证，结果大概率错误或偶发错误（取决于时序），这正好反证自动核间同步的必要性。运行结果「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：自动核间同步为什么以「workspace 名」配对，而不是按语句出现顺序配对？

**参考答案**：因为 Cube 与 Vector 的语句在各自 scope 里执行的 pipe 不同（Cube 的 `copy_l0c_to_gm` 走 FIX、Vector 的 `copy_gm_to_ub` 走 MTE2），且两侧循环结构可能不对称。以 workspace 名为键，保证配对的是「同一块数据的生产者与消费者」，语义正确；而顺序配对在多 workspace、跨核流水（`cross_interval`）下会错位。源码里 `cube_ws_map`/`vec_ws_map` 就是按名分组后逐一配对的。

**练习 2**：`DEFAUT_MODEL_ID = 2` 出现在 `set_cross_flag` 的参数里（[src/transform/ascend_combinecv.cc:41](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_combinecv.cc#L41) 及 [L321-L329](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_combinecv.cc#L321-L329)），它对应 u4-l2 里 `set_cross_flag` 的哪个参数？

**参考答案**：对应 `mode` 参数。`mode=2` 是「同组 AIC↔AIV」的默认模式（u4-l2 已建立），即同一 AI Core 内 Cube 与 Vector 子核之间的核间同步。自动同步 pass 把这个默认值固定写死，免去用户每次手填。

---

## 5. 综合实践

把本讲三个要点串起来，完成一个小任务：**手工「关闭」自动 CV 分离，改用 Expert 模式的 `T.Scope` 复现同样的拆分**。

1. 以 `examples/developer_mode/matmul_add_developer.py` 为基础，复制一份 `matmul_add_manual_scope.py`（示例代码，本地练习用）。
2. 把四个自动开关全部去掉，改用 Expert 写法：
   - 用 `T.alloc_L1`/`alloc_L0C`/`alloc_ub` 显式分配（u4-l1）；
   - 用 `with T.Scope("C"):` 包裹 `T.copy(GM→L1)`、`T.gemm_v0`（或展开成 `T.copy(L1→L0A/L0B)` + `T.mma`）；
   - 用 `with T.Scope("V"):` 包裹 `T.copy(GM→UB)` 读 D、`T.tile.add`、写回 GM；
   - 跨 CV 的 `C_L0 → c_ub` 用 `T.copy`（让 workspace 消除 pass 自动拆两阶段）；
   - 核间同步用手写 `T.set_cross_flag` / `T.wait_cross_flag`（u4-l2）。
3. 分别对「Developer 自动版」和「Expert 手写版」调用 `get_kernel_source()`，对比两份生成代码：
   - 两者的 `if ASCEND_IS_AIC/AIV` 分支结构是否一致？
   - 自动版的 `CrossCoreSetFlag/WaitFlag` 与你手写的位置是否对应？
4. 记录结论：**Developer 模式自动 CV 分离 + 自动核间同步，等价于 Expert 模式手写 `T.Scope` + 手写 cross flag**，只是把「拆分判定」与「flag 配对」从人脑交给了编译器。

> 这个对比能让你深刻理解 `CombineCV` 与 `AutoInsertCrossCoreSync` 到底替你省了什么。运行正确性「待本地验证」。

## 6. 本讲小结

- Ascend 一个 AI Core 内 Cube（AIC）与 Vector（AIV）并行、且不能直连数据，必须经 GM/L2 workspace 中转；二者加载 **同一份 `.so`**，靠 `resource_scope` 属性在 codegen 时切成 `if ASCEND_IS_AIC/AIV`（Ascend C）或 `#if __DAV_*_CUBE/VEC__`（PTO）两段。
- **CV 分离的契约只有一个：`resource_scope` 属性（0=Cube, 1=Vector）。** Expert 模式由 `T.Scope("C"/"V")` 手贴（[src/ir.cc:495](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/ir.cc#L495)），Developer 模式由 `CombineCV` pass 自动贴。
- **`CombineCV` pass（开关 `TL_ASCEND_AUTO_CV_COMBINE`）** 把一段混合 kernel 体，按算子名表 `callnodeMapPos_` + buffer scope 反查，拆成 cube/vec 两份，各裹 `resource_scope`；位于 `OptimizeForTarget` 里 `CrossCorePipeline` 之后、软件流水与同步插入之前。
- **`TL_ASCEND_AUTO_CV_SYNC`（`tl.ascend_auto_cross_core_sync`）** 在 CV 拆分后，以 workspace 名为键把 Cube 的写与 Vector 的读配对，自动插 `set_cross_flag`（写后）/`wait_cross_flag`（读前），支持 `cross_interval` 稀疏同步；它依赖 CV 拆分的产出，故两开关 **必须同开**。
- 自动 CV 分离 + 自动核间同步，与 Expert 手写 `T.Scope` + 手写 cross flag **语义等价**，差别仅在「判定由编译器做还是人做」。

## 7. 下一步学习建议

- **u5-l2 跨核流水（CrossCorePipeline）**：本讲多次提到 `cross_interval` 与「Cube 写 workspace、Vector 读 workspace」的时序，下一讲会把这条跨核流水正式展开，讲 `CrossCorePipeline` pass 如何与 `T.Pipelined` 配合实现 inter-core 流水。
- **u5-l3 Vid 消除与自动 CV 配比**：本讲的 `matmul_add_developer.py` 用了 `threads=2`，下一讲解释它如何让 UB 申请/搬运不再考虑内核排布，并联动 CV 配比。
- **u5-l4 Workspace 消除**：本讲反复出现的「`C_L0 → c_ub` 被拆成两阶段 GM 搬运」正是 workspace 消除 pass 的产物，下一讲专题讲解。
- 建议同步精读：[examples/flash_attention/flash_attn_bhsd_cc_sync.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/flash_attention/flash_attn_bhsd_cc_sync.py)，它是「免手写 flag」的完整 CV 协同案例，能巩固本讲两个开关的实战用法。
