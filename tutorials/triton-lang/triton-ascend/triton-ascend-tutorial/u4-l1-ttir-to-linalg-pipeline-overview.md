# ttir_to_linalg：Ascend pass 编排总览

## 1. 本讲目标

在 u3 系列里，我们把编译流水线看成了「阶段名 → 处理函数」的一条传送带：`make_ttir` 跑完通用优化后，产物会被送进下一个阶段。本讲就**深入这下一个阶段**——`ttir_to_linalg`，它是 Ascend 后端最「重」的一段，几乎所有 Ascend 专属变换都集中在这里。

读完本讲，你应当能够：

1. 说清 `ttir_to_linalg` 在整条链路中的位置——它注册为哪个阶段、它的输入输出是什么、什么情况下会被完全跳过。
2. 把 `ttir_to_linalg` 内部那条 Ascend pass 流水线**完整画出来**，并知道每个 pass 大致负责什么。
3. 区分两类「开关」：哪些 pass 是**有条件注册**（满足条件才会出现在管道里），哪些 pass 是**始终注册、靠参数调整行为**。
4. 理解 `ir.pass_manager` 是怎么把 Python 侧的 `add_*` 调用串成一条 C++ pass 管道，并与命令行工具 `triton-opt --pass-pipeline=...` 一一对应。

本讲是第 4 单元（Ascend MLIR pass 流水线）的「总图」——后续 u4-l2 ~ u4-l6 会逐个钻进具体的 pass 细节。先把总图印在脑子里，再去读细节，才不会迷路。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（来自 u1、u3-l2、u3-l3）：

- **TTIR**：Triton 把 Python kernel 翻译出的、与目标硬件无关的中间表示（MLIR 方言 `tt`）。`make_ttir` 已对它跑过一遍通用优化。
- **Linalg / memref**：MLIR 里更接近「显式循环 + 张量/缓冲」的方言。Ascend 后端最终要把 TTIR 算子（`tt.dot`、`tt.load` 等）变换成这些方言，再交给 BiSheng 工具链编成 `.o`。
- **阶段流水线**：`AscendBackend.add_stages` 把「阶段名 → 处理函数」按插入顺序登记进一个 `dict`，core 按序执行。默认（`use_bytecode=True`）路径为 `ttir → ttadapter → mlirbc → bcmlir → npubin`。
- **`NPUOptions`**：Ascend 后端的不可变编译选项数据类。它的字段会进入一个沿途累积的 `metadata` 字典，被各阶段读写。

几个本讲会用到的术语：

- **pass（通道）**：对 IR 做一次特定变换的单元，例如把 `tt.load` 改写成 `memref.load`。
- **pass manager（pass 管理器）**：一个容器，按添加顺序存放若干 pass，调用 `run` 时依次执行它们。
- **`ttadapter`**：`ttir_to_linalg` 这个阶段对外暴露的阶段名（沿用历史命名，源自「triton-adapter」），它产出的 IR 文本会落盘为 `kernel.ttadapter.mlir`。

## 3. 本讲源码地图

本讲围绕两个文件，并把一个核心数据结构贯穿始终：

| 文件 | 作用 |
| --- | --- |
| `third_party/ascend/backend/compiler.py` | Ascend 后端核心。本讲精读其中的 `ttir_to_linalg`（阶段函数）、`add_stages`（阶段注册）、`NPUOptions`/`__post_init__`（开关来源），以及 `make_ttir`、`_parse_linalg_metadata` 的边界。 |
| `third_party/ascend/bin/triton-mlir-opt.cpp` | `triton-mlir-opt` 工具的入口。本讲用它说明「命令行工具 / pass 管道」这一对应关系，并区分它与 `ttir_to_linalg` 实际使用的 `triton-opt` 工具。 |

贯穿始终的核心对象是 `ir.pass_manager`——它是 `triton._C.libtriton` 提供的 C++ 绑定（[compiler.py:36](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L36) 处的 `from triton._C.libtriton import ir, passes, ascend`），`ttir_to_linalg` 正是靠它把一串 `ascend.passes.ttir.add_*` 调用组装成一条可执行的 pass 管道。

## 4. 核心概念与源码讲解

### 4.1 ttir_to_linalg：这一步在整条链路中的位置

#### 4.1.1 概念说明

`ttir_to_linalg` 是 Ascend 后端的核心阶段函数。它的职责是：**把经过 `make_ttir` 优化的 TTIR，通过一长串 Ascend 专属 pass，变换成「以 linalg / memref 算子为主」的 IR 文本**（即 `ttadapter` IR）。之后这条 IR 会被送进 npubin 阶段，由 BiSheng 编译器编成可在 NPU 上执行的 `.o`。

这里有个容易混淆的命名点：函数名叫 `ttir_to_linalg`，但它注册的阶段名是 `ttadapter`，落盘文件叫 `kernel.ttadapter.mlir`。「to_linalg」指的是它最终的 lowering 目标（TTIR 算子 → linalg/memref 算子），而 `ttadapter` 是这个阶段在流水线里的对外身份。所以当我们说「ttadapter 阶段」「ttadapter IR」时，指的就是 `ttir_to_linalg` 的产物。

还有一条重要的边界：`ttir_to_linalg` **只做 IR 变换、产出文本**，它**不负责**解析 `kernel_name`、`mix_mode` 这些「运行时元数据」。那一步（`_parse_linalg_metadata`）发生在更后面的 npubin 阶段——它读的正是 `ttir_to_linalg` 产出的这段 IR 文本（见 u3-l3）。本讲我们只关心「IR 是怎么一步步被变换出来的」，不碰元数据解析。

#### 4.1.2 核心流程

`ttir_to_linalg` 的执行可以概括为五步：

1. **读 TTIR 文本**：`ttir_code = str(mod)`，把当前 IR 模块序列化成字符串。
2. **判定 AutoBlockify 黑名单**：检查 kernel 里是否含 atomic、inline-asm、volatile 等「顺序敏感」算子，决定是否禁用 AutoBlockify（详见 4.4）。
3. **从 `metadata` 读取一批开关**：这些开关几乎全部来自 `NPUOptions`，决定哪些 pass 怎么跑。
4. **构造 pass manager，按固定顺序「塞入」一串 pass**，然后 `pm.run(mod, 'ttir_to_linalg')` 一次性执行。
5. **回写少量 metadata、导出 coalesce 信息**，返回变换后的 IR 文本。

第 4 步是本讲的主角，我们把它单独放到 4.3 讲。

#### 4.1.3 源码精读

先看阶段注册——`ttir_to_linalg` 是怎么被挂到流水线上的：

[third_party/ascend/backend/compiler.py:1269-1290](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L1269-L1290) —— `add_stages`：把 `ttir_to_linalg` 绑定到 `ttadapter` 阶段。关键三处：

- 第 1271 行：`stages["ttir"]` 永远绑定 `make_ttir`（上一阶段）。
- 第 1272-1274 行：若 `options.force_simt_only` 为真，则**只**注册 `ttir → npubin`（走纯 SIMT 路径 `ttir_to_npubin`）并立即 `return`——**`ttir_to_linalg` 被完全跳过**。这就是为什么 u3-l2 强调 `force_simt_only` 会「跳过 Linalg 主线」。
- 第 1275 行：否则 `stages["ttadapter"]` 绑定 `ttir_to_linalg(..., named_ops=True)`。注意这里把 `named_ops=True` 传了进去，它会一路传到最后的 `add_triton_to_linalg`，影响算子命名（详见 u4-l5）。

再看 `ttir_to_linalg` 本体的骨架：

[third_party/ascend/backend/compiler.py:155-190](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L155-L190) —— 函数入口：读 `ttir_code`、判 AutoBlockify 黑名单、从 `metadata` 读取 `compile_on_910_95`、`force_simt_template`、`enable_sync_block_lock`、`enable_mask_fallback_conversion`、`optimize_dynamic_offset`、`auto_blockify_size` 等开关，然后 `pm = ir.pass_manager(mod.context)` 建立一个空的 pass 管理器。

[third_party/ascend/backend/compiler.py:254-264](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L254-L264) —— 管道收尾：`pm.run(mod, 'ttir_to_linalg')` 执行全部 pass；随后 `_adjust_metadata_by_module_result` 根据动态 CV 流水线的返回码回写 metadata，`_export_coalesce_metadata` 读出 coalesce 因子，最后返回 `str(mod)`。

注意一个细节：第 175 行取了 `triton_adapter_opt_path = _get_triton_adapter_opt_path()`，但整个函数体里**并没有用到它**（全仓库仅在此处赋值、无引用）。这属于历史遗留变量——本阶段实际的 lowering 完全在进程内通过 pass manager 完成，并不真的调用 `triton-adapter-opt`。读源码时看到它不必困惑。

#### 4.1.4 代码实践

**实践目标**：确认「`ttir_to_linalg` 注册为 `ttadapter` 阶段，且在纯 SIMT 模式下被跳过」。

**操作步骤**（源码阅读型实践，无需 NPU）：

1. 打开 [compiler.py:1269](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L1269)，顺着 `if options.force_simt_only:` 这条分支，画出两种 `stages` 字典的内容差异。
2. 对照 u3-l2 的 `__post_init__`：`compile_mode="simt_only"` 会让 `force_simt_only=True`，从而命中这条 `return`。

**需要观察的现象**：在 `force_simt_only=True` 分支里，`stages` 里**没有** `ttadapter`/`mlirbc`/`bcmlir` 这些键，只有 `ttir` 和 `npubin` 两个键。

**预期结果**：你能画出两张「阶段字典」对比表——一张默认模式（`ttir → ttadapter → mlirbc → bcmlir → npubin`），一张纯 SIMT 模式（`ttir → npubin`）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ttir_to_linalg` 产出的文件叫 `kernel.ttadapter.mlir`，而函数名却叫 `ttir_to_linalg`？

> **参考答案**：函数名描述的是**变换目标**（把 TTIR 算子 lowering 成 linalg/memref 算子）；`ttadapter` 是这个阶段在**流水线里的注册名**（阶段键名），源自历史命名「triton-adapter」。落盘文件名沿用阶段名，所以是 `kernel.ttadapter.mlir`。

**练习 2**：`ttir_to_linalg` 自己会调用 `_parse_linalg_metadata` 吗？

> **参考答案**：不会。`_parse_linalg_metadata` 在后面的 npubin 阶段（`linalg_to_bin_enable_npu_compile_910_95` / `linalg_to_bin_enable_npu_compile_A2_A3`）才被调用，它解析的正是 `ttir_to_linalg` 产出的那段 IR 文本。本阶段只产出 IR、不解析元数据。

---

### 4.2 ir.pass_manager：pass 管理器与命令行的对应

#### 4.2.1 概念说明

`ttir_to_linalg` 之所以能把十几个 pass 串成一条管道，靠的是 `ir.pass_manager`。它是 MLIR 的 Python 绑定：调用 `ir.pass_manager(mod.context)` 得到一个**空的** pass 管理器（绑定到当前 IR 的 context）；随后每调用一次 `ascend.passes.ttir.add_xxx(pm, ...)`，就向这个管理器**追加一个 pass**；最后 `pm.run(mod, name)` 按追加顺序依次执行它们。

这套机制有一个非常实用的副产品：**进程内跑的 pass 管道，可以完整地还原成一条命令行**。这是因为 MLIR 每个 pass 都注册了一个「文本化的管道名」，`pm.get_pipeline_str()` 能把整条管道拼成一段文本，再交给 `triton-opt --pass-pipeline=<文本>` 就能在命令行里**复现**同样的变换。这意味着：当某个 kernel 在 `ttir_to_linalg` 阶段出了问题，你不必在 Python 里反复调试，可以直接拿 dump 出来的 TTIR 文件 + 打印出的管道字符串，用 `triton-opt` 单步复现。

> 术语提示：`triton-opt`（`_get_triton_opt_path`）是把 Triton 与 Ascend 方言/ pass 一起编译进去的标准 MLIR `mlir-opt` 工具，注释里写明它「用于把 ttir 转成 ttadapter」，正是对应 `ttir_to_linalg` 这一阶段。它和本目录下的 `triton-mlir-opt`（4.2.3 会讲）是两个不同的工具，不要混淆。

#### 4.2.2 核心流程

`ttir_to_linalg` 用 pass manager 的流程：

1. `pm = ir.pass_manager(mod.context)`：建立空管道。
2. `pm.enable_debug()`：开启调试（影响 dump 行为）。
3. 一连串 `ascend.passes.ttir.add_xxx(pm, ...)` / `passes.common.add_xxx(pm)`：按顺序追加 pass（4.3 详列）。
4. （可选）`opt.debug` 为真时，用 `pm.get_pipeline_str()` 拼出 `triton-opt` 命令并打印。
5. `pm.run(mod, 'ttir_to_linalg')`：执行整条管道。

#### 4.2.3 源码精读

建立管道与打印命令行的关键片段：

[third_party/ascend/backend/compiler.py:190-192](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L190-L192) —— 建立空 pass 管理器并开启调试；注释里还保留了一行被注释掉的 `add_auto_blockify`（AutoBlockify 不在这里以 pass 形式追加，而是由别处驱动，见 4.4）。

[third_party/ascend/backend/compiler.py:240-254](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L240-L254) —— debug 分支：用 `pm.get_pipeline_str()` 拼出等价的 `triton-opt` 命令并打印 `[DEBUG] cmd list:`，然后才 `pm.run`。这条命令就是「把进程内管道搬到命令行」的钥匙。

工具解析在 utils 里：

[third_party/ascend/backend/utils.py:213-227](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/utils.py#L213-L227) —— `_get_triton_opt_path`（对应 `ttir_to_linalg` 阶段的 `triton-opt`，注释写明「用于把 ttir 转成 ttadapter」）与 `_get_triton_mlir_opt_path`（对应 `triton-mlir-opt`，用于 mlirbc 阶段的 MLIR↔Bytecode 转换）。两者都经 `_get_tool_path` 在「已安装包目录 / `TRITON_BUILD_DIR` / `PATH`」三处查找。

再看 `triton-mlir-opt` 这个工具本身（注意：它**不**参与 `ttir_to_linalg`，了解它是为了和 `triton-opt` 区分）：

[third_party/ascend/bin/triton-mlir-opt.cpp:32-42](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/bin/triton-mlir-opt.cpp#L32-L42) —— `main`：注册所有 MLIR 方言 + BishengIR 方言，调用 MLIR 标准的 `MlirOptMain`。它本质上是一个「带 BishengIR 方言的 mlir-opt」，服务于 mlirbc 阶段。它同样接受 `--pass-pipeline=`，但 `ttir_to_linalg` 用的是另一个工具 `triton-opt`。

#### 4.2.4 代码实践

**实践目标**：亲眼看到 `ttir_to_linalg` 跑的 pass 管道被还原成一条 `triton-opt` 命令。

**操作步骤**：

1. 准备一个能跑的 Triton kernel（用 u1-l4 的 vector-add 即可），并在脚本里加 `import os; os.environ["TRITON_DEBUG"] = "1"`（需在 `import triton` 之前设置）。
2. 运行脚本，在 stderr / stdout 中搜索 `[DEBUG] cmd list:`。

**需要观察的现象**：会看到一行形如
`triton-opt <某路径>/kernel.ttir.mlir --pass-pipeline=<一长串管道文本> --mlir-print-debuginfo -o <某路径>/kernel.ttadapter.mlir`
的输出。

**预期结果**：那条 `--pass-pipeline=` 后面的文本，就是 4.3 里列出的全部 pass 按顺序拼成的管道字符串。把这条命令复制出来、把输入路径换成 dump 出的 `kernel.ttir.mlir`，理论上就能在命令行复现 `ttir_to_linalg` 的变换（具体能否直接跑通取决于工具链是否齐全，若环境不具备请标注「待本地验证」）。

#### 4.2.5 小练习与答案

**练习 1**：`pm.run(mod, 'ttir_to_linalg')` 的第二个参数 `'ttir_to_linalg'` 是什么作用？

> **参考答案**：它是该次 `run` 的**调试标签 / dump 命名标识**，用于在 dump 目录里区分不同阶段（`make_ttir`、`ttir_to_linalg` 等）的产物。它不是 pass 名，也不改变变换内容。

**练习 2**：`triton-opt` 和 `triton-mlir-opt` 分别服务于哪个阶段？

> **参考答案**：`triton-opt` 服务于 `ttadapter` 阶段（即 `ttir_to_linalg`，把 ttir 转成 ttadapter）；`triton-mlir-opt` 服务于 `mlirbc` 阶段（把 Linalg IR 在 MLIR 文本与 Bytecode 之间转换，并兼容 AscendNPU-IR 自定义算子）。两者都是基于 MLIR `MlirOptMain` 的 `*-opt` 工具，但注册的方言/用途不同。

---

### 4.3 ascend.passes.ttir：pass 的注册与完整编排顺序

#### 4.3.1 概念说明

`ascend.passes.ttir` 是 `triton._C.libtriton.ascend.passes.ttir` 这个 C++ 绑定模块（[compiler.py:36](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L36) 的导入）。它的每个 `add_xxx(pm, ...)` 函数都把**一个具体的 Ascend C++ pass** 追加进 pass 管理器；可选参数则用来「微调」该 pass 的行为。换言之，`ttir_to_linalg` 的主体就是一连串对 `ascend.passes.ttir` 的调用——把这些调用按顺序读懂，就等于读懂了「TTIR 是怎么一步步被搬到 Linalg 的」。

这些 pass 大致分工如下（细节留待 u4-l2 ~ u4-l6）：

| pass（`add_` 前缀省略） | 作用概要 | 详解讲义 |
| --- | --- | --- |
| `triton_control_flow_opt` | Triton 控制流优化（预处理） | — |
| `dag_sync` / `dag_scope` / `dag_ssbuffer` | 跨核 DAG 调度建模（仅 `add_auto_scheduling` 时） | — |
| `triton_to_structure` | 把指针/掩码表达式张量化、建模为结构化访存 | u4-l2 |
| `discrete_mask_access_conversion` | 离散掩码的 load/store 转 select 序列 | u4-l3 |
| `triton_to_annotation` | 把 `compile_hint` 等标注下沉到 annotation 方言 | u4-l6 |
| `triton_to_unstructure` | 离散/非连续访存展开为标量循环 | u4-l4 |
| `triton_to_hivm` | 跨核同步（`sync_block_*`）下沉到 hivm 方言 | u4-l6 |
| `triton_to_hfusion` | 直方图等融合 | u4-l6 |
| `triton_to_llvm` | 内联汇编（`inline_assembly`）映射到 CCE intrinsic | u4-l6 |
| `bubble_up_operation` | 把 extract 等操作上提，便于后续优化 | u4-l4 |
| `triton_to_linalg` | 把 TTIR 算子系统性 lowering 成 linalg/memref 算子 | u4-l5 |
| `dynamic_cv_pipeline` | Cube-Vector 动态流水线（仅 950/CV 开关时） | u8-l2 |

#### 4.3.2 核心流程

下面是 `ttir_to_linalg` 里 pass 的**完整追加顺序**。这是本讲最重要的「总图」：

```
                       ttir_to_linalg 的 pass 管道
┌──────────────────────────────────────────────────────────────────────┐
│ ① add_triton_control_flow_opt                          【恒定】      │
│                                                                      │
│ ② [仅当 add_auto_scheduling]                                         │
│      add_dag_sync → add_dag_scope → cse → canonicalizer             │
│      → add_dag_ssbuffer → cse → canonicalizer                        │
│                                                                      │
│ ③ add_triton_to_structure(mask_fallback, optimize_dynamic_offset)   │
│ ④ add_discrete_mask_access_conversion(910_95, simt_tpl, sync_lock)  │
│ ⑤ add_triton_to_annotation                                           │
│ ⑥ add_triton_to_unstructure(910_95, simt_tpl)                       │
│ ⑦ add_triton_to_hivm                                                 │
│ ⑧ add_triton_to_hfusion(910_95)                                     │
│ ⑨ add_triton_to_llvm                                                 │
│ ⑩ add_bubble_up_operation                                            │
│ ⑪ add_triton_to_structure(mask_fallback, optimize_dynamic_offset)  │ ← 再跑一次
│ ⑫ add_triton_to_linalg(False, named_ops, nd2nz, select_ana, 910_95) │
│                                                                      │
│ ⑬ [仅当 enable_dynamic_cv_pipeline]                                  │
│      set_enable_cube_block_merge / set_enable_ub_refine_opt          │
│      / set_enable_buffer_insert_optimization                         │
│      → add_dynamic_cv_pipeline(910_95)                              │
│                                                                      │
│ ⑭ [仅当 intra/inter/load_cache_num 给定] set_buffer_count(...)      │
│                                                                      │
│ ─────────────── pm.run(mod, 'ttir_to_linalg') ───────────────        │
└──────────────────────────────────────────────────────────────────────┘
```

几点要特别注意：

- **`triton_to_structure` 跑了两次**（③ 和 ⑪）。第一次在结构化建模前，第二次在 `bubble_up_operation` 之后、最终 lowering 之前，用来清理被前面 pass（尤其是展开/上提）再次扰动的访存表达式。
- 真正把 TTIR 算子「翻译」成 linalg/memref 算子的是末尾的 ⑫ `add_triton_to_linalg`，前面的 ③~⑪ 都是「预处理 / 归整」，为最后这一步扫清障碍。
- ⑬ 的动态 CV 流水线是「950 / CV 融合」专属的额外管道，默认（非 950）不出现。

#### 4.3.3 源码精读

[third_party/ascend/backend/compiler.py:194-214](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L194-L214) —— 主体管道（① ~ ⑫）：从 `add_triton_control_flow_opt` 开始，依次追加各 pass，参数即开关。注意第 203 行与第 212 行是同一个 `add_triton_to_structure` 的两次调用；第 213-214 行的 `add_triton_to_linalg(False, named_ops, ...)` 是最终 lowering，第二个实参 `named_ops` 就是从 `add_stages` 一路传进来的 `True`。

[third_party/ascend/backend/compiler.py:215-226](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L215-L226) —— 动态 CV 流水线块（⑬）：仅当 `metadata["enable_dynamic_cv_pipeline"]` 为真时执行。注意它在追加 pass 之前，先用 `set_*` 系列把若干模块级属性写进 IR 模块（如 `set_enable_buffer_insert_optimization`），这些属性会被 CV 流水线内部的 `AddMultiBufferInnerScope` pass 在运行时读取——注释明确要求「必须在 `add_dynamic_cv_pipeline` 之前调用」。

[third_party/ascend/backend/compiler.py:228-238](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L228-L238) —— 缓冲数量设置（⑭）：当 `intra_cache_num` / `inter_cache_num` / `load_cache_num` 三个 metadata 字段中任一非空时，用 `set_buffer_count(mod, "INTRA|INTER|LOAD", n)` 写入模块。

#### 4.3.4 代码实践

**实践目标**：把上面的总图与真实 dump 对应起来，验证「顺序」与「重复」。

**操作步骤**：

1. 重复 4.2.4 的步骤，开启 `TRITON_DEBUG`，拿到 `[DEBUG] cmd list:` 里的 `--pass-pipeline=` 文本。
2. 在该文本里依次找出与 ① ~ ⑫ 对应的 pass 名（MLIR 文本管道里 pass 名通常是 `triton_ascend-xxx` 这类带连字符的形式）。

**需要观察的现象**：与 `triton_to_structure` 对应的那个 pass 名，在管道字符串里应**出现两次**。

**预期结果**：你能把管道字符串里的 pass 一一映射回本节的 ① ~ ⑫；若你的硬件不是 950、且没开 `enable_dynamic_cv_pipeline`，则 ⑬ 的 `dynamic_cv_pipeline` 不应出现。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `add_triton_to_structure` 要追加两次？

> **参考答案**：第一次（③）在离散掩码/标量展开之前做结构化建模；中间的 `triton_to_unstructure`、`bubble_up_operation` 等 pass 会再次扰动访存表达式，所以在最终 lowering 前再跑一次（⑪）把表达式重新归整，确保最后的 `triton_to_linalg` 拿到规整的输入。

**练习 2**：最终把 TTIR 算子翻译成 linalg/memref 算子的是哪个 pass？

> **参考答案**：⑫ `add_triton_to_linalg(False, named_ops, enable_nd2nz_on_vector, enable_select_analysis, compile_on_910_95)`。它前面的 pass 都是预处理/归整。

---

### 4.4 开关条件：硬件与编译模式如何分流 pass

#### 4.4.1 概念说明

`ttir_to_linalg` 里看似每个 pass 都「无条件」被追加，但仔细区分，其实有两类开关，务必分清：

- **第一类：条件注册**——满足条件，pass **才会出现在管道里**。本阶段只有两块：
  - `add_auto_scheduling`（默认 `False`）：决定是否追加 ② 那一整块 DAG 调度 pass。
  - `enable_dynamic_cv_pipeline`：决定是否追加 ⑬ 动态 CV 流水线块。
- **第二类：始终注册、参数调参**——pass **一定会跑**，但传入的参数会改变它的行为。绝大多数 pass 属于这类，参数几乎都来自 `metadata`（其源头是 `NPUOptions`）：
  - `compile_on_910_95`：是否为 950 类硬件（`ascend910_95` / `ascend950` / `910_958b`）。它同时喂给 ④⑥⑧⑫⑬ 多个 pass。
  - `force_simt_template`：由默认 `compile_mode="unstructured_in_simt"` 派生为 `True`，喂给 ④⑥。
  - `enable_sync_block_lock`、`enable_mask_fallback_conversion`、`optimize_dynamic_offset`、`enable_nd2nz_on_vector`、`enable_select_analysis`：各自微调对应 pass。

此外还有一个「管道外」的开关影响 AutoBlockify（它不在 ①~⑭ 里追加，而是先于管道判定，再交给后续阶段）：

- `auto_blockify_size`（默认 `1`）：当检测到 atomic / inline-asm / volatile / cache 修饰等「顺序敏感」算子（AutoBlockify 黑名单），或未开启 `TRITON_ALL_BLOCKS_PARALLEL` 时，被强制设为 `1`（即不并行展开逻辑块）。

#### 4.4.2 核心流程

开关的「取值链」可以概括为：

```
NPUOptions 字段  ──(parse_options 的 lazy 探测)──▶  metadata 字段  ──▶  add_xxx(pm, 参数)
```

两个典型的 lazy 探测：

- `compile_on_910_95`：若用户没显式给，则调用 `is_compile_on_910_95()`，它通过 `acl.get_soc_name()` 判断当前 SoC 名是否含 `ascend910_95` / `ascend950` / `910_958b`。
- `enable_dynamic_cv_pipeline`：若用户没显式给，**默认就等于 `is_compile_on_910_95()`**——也就是说，950 硬件默认开启动态 CV 流水线（⑬ 会执行）。

而 `force_simt_template` 来自 `compile_mode`：

- `compile_mode="simd"` → `parallel_mode="simd"`，`force_simt_template` 保持默认 `False`。
- `compile_mode="unstructured_in_simt"`（**默认**） → `force_simt_template=True`。
- `compile_mode="simt_only"` → `force_simt_only=True`，在 `add_stages` 里早退，**根本到不了 `ttir_to_linalg`**。

#### 4.4.3 源码精读

[third_party/ascend/backend/compiler.py:159-170](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L159-L170) —— AutoBlockify 黑名单判定：当 `TRITON_ALL_BLOCKS_PARALLEL` 开启且尚未判定时，用 `_get_auto_blockify_blacklist_reasons(ttir_code)` 扫描 TTIR 文本；命中黑名单就把 `auto_blockify_size` 置 1 并打印警告。

[third_party/ascend/backend/compiler.py:177-189](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L177-L189) —— 从 `metadata` 读出所有第二类开关（`compile_on_910_95`、`force_simt_template`、`enable_sync_block_lock`、`enable_mask_fallback_conversion`、`optimize_dynamic_offset`、`auto_blockify_size`、`enable_mixed_cv` 等）；第 188-189 行实现「黑名单或未开并行时，`auto_blockify_size` 强制为 1」。

开关来源（NPUOptions 与探测函数）：

[third_party/ascend/backend/compiler.py:1213-1232](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L1213-L1232) —— `parse_options`：把选项字典白名单过滤后构造 `NPUOptions`；对 `compile_on_910_95`、`enable_dynamic_cv_pipeline` 做 lazy 探测（`None` 时才填）。

[third_party/ascend/backend/compiler.py:1111-1126](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L1111-L1126) —— `__post_init__`：由 `compile_mode` 派生 `force_simt_only` / `force_simt_template` / `parallel_mode`。注意默认 `compile_mode="unstructured_in_simt"` 会把 `force_simt_template` 置 `True`。

[third_party/ascend/backend/utils.py:39-50](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/utils.py#L39-L50) —— `is_compile_on_910_95`：用 `acl.get_soc_name()` 判断是否 950 类硬件。

[third_party/ascend/backend/utils.py:53-64](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/utils.py#L53-L64) —— `AUTO_BLOCKIFY_BLACKLIST_RULES`：atomic、inline elementwise asm、volatile load、带 cache 修饰的 load/store 四类算子触发黑名单。

[third_party/ascend/backend/utils.py:349-354](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/utils.py#L349-L354) —— `_is_auto_map_parallel_blocks_enabled`（读环境变量 `TRITON_ALL_BLOCKS_PARALLEL`，默认 `true`）与 `_get_auto_blockify_blacklist_reasons`。

#### 4.4.4 代码实践

**实践目标**：用一个 kernel，对比「950 默认」与「关闭 CV 流水线」两种配置下管道的差异。

**操作步骤**：

1. 用一个含 `tl.dot` 的 kernel（如 u1 系列里的矩阵乘 tutorial），开启 `TRITON_DEBUG` 跑一次，记录 `[DEBUG] cmd list:` 里的 `dynamic_cv_pipeline` 是否出现。
2. 在 kernel 调用处显式传入 `TRITON_KERNEL_OVERRIDE` 之外的方式关闭 CV 流水线——即通过编译选项 `enable_dynamic_cv_pipeline=False`（可在 `@triton.jit` 调用时经由 `triton.compiler.compile` 的 options 传入，或用环境/调试钩子；具体传参入口请以本地版本为准，标注「待本地验证」）。

**需要观察的现象**：第 1 步（950 默认）的管道字符串里应含 `dynamic_cv_pipeline` 相关 pass；第 2 步关闭后该 pass 消失。

**预期结果**：你能用本节的「两类开关」模型解释这一差异——`enable_dynamic_cv_pipeline` 属于**第一类（条件注册）**，它直接决定 ⑬ 是否出现在管道里。

#### 4.4.5 小练习与答案

**练习 1**：`compile_on_910_95` 属于哪一类开关？它影响哪些 pass？

> **参考答案**：属于**第二类（始终注册、参数调参）**。它作为参数喂给 ④ `discrete_mask_access_conversion`、⑥ `triton_to_unstructure`、⑧ `triton_to_hfusion`、⑫ `triton_to_linalg`、⑬ `dynamic_cv_pipeline` 等多个 pass，改变它们在 950 上的具体行为，但不决定这些 pass 是否注册。

**练习 2**：默认 `compile_mode="unstructured_in_simt"` 时，`force_simt_template` 是 `True` 还是 `False`？这会让 `ttir_to_linalg` 被跳过吗？

> **参考答案**：是 `True`（由 `__post_init__` 派生）。它**不会**让 `ttir_to_linalg` 被跳过——只有 `force_simt_only=True`（即 `compile_mode="simt_only"`）才会在 `add_stages` 里早退。`force_simt_template=True` 只是作为参数喂给 ④⑥ 两个 pass。

**练习 3**：在 950 硬件上，默认会不会执行动态 CV 流水线（⑬）？

> **参考答案**：会。`enable_dynamic_cv_pipeline` 默认等于 `is_compile_on_910_95()`，950 硬件上为 `True`，所以 ⑬ 默认执行（除非用户显式关闭）。

## 5. 综合实践

把本讲三件事——「位置、管道、开关」——串起来完成下面这个任务：

**任务**：为 `ttir_to_linalg` 画一张「带开关标注的完整 pass 流水线图」，并据此预测一次真实编译的管道。

要求：

1. **画图**：把 4.3.2 的 ①~⑭ 完整画出。用三种颜色/标记区分：
   - 「恒定」pass（如 ①、⑤、⑦、⑨、⑩、⑫）；
   - 「条件注册」pass（② `add_auto_scheduling` 块、⑬ `enable_dynamic_cv_pipeline` 块）——标注各自的启用条件；
   - 「参数调参」pass（③④⑥⑧⑫）——标注关键参数（`compile_on_910_95`、`force_simt_template` 等）。
2. **预测**：假设你在 **非 950 硬件**、默认 `compile_mode`、一个**纯向量** kernel（不含 `tl.dot`、不含 atomic）上运行，预测管道里会出现哪些 pass、不会出现哪些 pass。重点判断：⑬ 是否出现？② 是否出现？
3. **验证**：开启 `TRITON_DEBUG` 实际运行，用 `[DEBUG] cmd list:` 里的 `--pass-pipeline=` 文本核对你的预测。

**参考预测**（非 950 + 默认模式 + 纯向量 kernel）：

- ⑬ **不出现**：非 950 时 `enable_dynamic_cv_pipeline` 默认为 `False`。
- ② **不出现**：`add_auto_scheduling` 默认为 `False`。
- ①③④⑤⑥⑦⑧⑨⑩⑪⑫ 出现，其中 ③ 与 ⑪ 是同一个 `triton_to_structure` 的两次出现。
- AutoBlockify：纯向量、无黑名单算子、`TRITON_ALL_BLOCKS_PARALLEL` 默认开启 → `auto_blockify_size` 保持用户值（默认 1）。

## 6. 本讲小结

- `ttir_to_linalg` 注册为 **`ttadapter` 阶段**，位于 `make_ttir` 之后、npubin 之前；产出落盘为 `kernel.ttadapter.mlir`。纯 SIMT 模式（`force_simt_only`）会在 `add_stages` 里早退，**完全跳过**它。
- 它的核心是 `ir.pass_manager`：建立空管道 → 一串 `ascend.passes.ttir.add_*` 追加 pass → `pm.run`。`pm.get_pipeline_str()` 可把整条管道还原成 `triton-opt --pass-pipeline=...` 命令，便于命令行复现与调试。
- pass 编排有固定顺序，关键特征：`triton_to_structure` 跑两次；最终 lowering 是末尾的 `add_triton_to_linalg`；中间的 pass 都是预处理/归整。
- 开关分两类：**条件注册**（`add_auto_scheduling`、`enable_dynamic_cv_pipeline`，决定 pass 是否出现）与**参数调参**（`compile_on_910_95`、`force_simt_template` 等，只改行为）。950 硬件默认开启动态 CV 流水线。
- `triton-opt`（对应 ttadapter 阶段）与 `triton-mlir-opt`（对应 mlirbc 阶段、带 BishengIR 方言）是两个不同的工具，不要混淆。

## 7. 下一步学习建议

本讲只画了「总图」，没有钻进任何单个 pass 的内部。建议按以下顺序逐个深入（它们都属同一单元 u4）：

- **u4-l2 TritonToStructured**：从 ③ `triton_to_structure` 开始，看指针/掩码表达式是如何被建模为结构化访存的。
- **u4-l3 / u4-l4**：接着读 ④ 离散掩码转换 与 ⑥ 离散访存标量化 + ⑩ `bubble_up_operation`，理解「离散访存」这条支线。
- **u4-l5 TritonToLinalg**：精读末尾 ⑫ `triton_to_linalg`，看 TTIR 算子最终如何变成 linalg/memref 算子。
- **u4-l6 其他 lowering**：覆盖 ⑤⑦⑧⑨（annotation / hivm / hfusion / llvm）这批「恒定」pass。
- 之后若对 ⑬ 动态 CV 流水线感兴趣，可直接跳到 **u8-l2 DynamicCVPipeline**。
