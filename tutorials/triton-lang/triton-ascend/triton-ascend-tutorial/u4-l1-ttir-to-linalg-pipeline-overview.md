# ttir_to_linalg：Ascend pass 编排总览

## 1. 本讲目标

本讲是「Ascend 编译后端 MLIR pass 流水线」单元的**总览篇**。读完本讲，你应该能够：

- 在 [compiler.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py) 的 `ttir_to_linalg` 中，逐行说出从 TTIR 到 Linalg IR 之间所有 pass 的**注册顺序**。
- 说清每个 pass 的**启用条件**，重点是 `compile_on_910_95`（950 代芯片）和 `enable_dynamic_cv_pipeline`（CV 流水线）这两个总开关，以及 `add_auto_scheduling`、`force_simt_template` 等条件分支。
- 理解 `ir.pass_manager`、`ascend.passes.ttir` 与命令行工具 `triton-opt --pass-pipeline=...` 三者之间的**对应关系**，知道如何把 Python 里的 pass 流水线「搬」到命令行复现。

本讲只做**编排总览**——告诉你「有哪些 pass、按什么顺序、谁开关它们」。每个 pass 内部究竟做了什么 IR 变换，留给后续讲义（u4-l2 到 u4-l6）逐篇精读。

## 2. 前置知识

阅读本讲前，请确认你已经掌握（来自 u1、u3 单元）：

- **TTIR**：Triton 上游产出的、与硬件无关的中间表示。`@triton.jit` 的 Python kernel 先被翻译成 TTIR（u3-l1）。
- **Linalg IR**：更贴近张量运算的 MLIR 方言，是 Triton-Ascend 交给 BiSheng 工具链继续往下编（最终到 `.o`）的输入形式之一。
- **编译阶段（stage）**：u3-l2 讲过，`AscendBackend.add_stages` 把每个编译阶段登记为「阶段名 → 处理函数」，core 按顺序执行，如 `ttir → ttadapter → mlirbc → bcmlir → npubin`。本讲的 `ttir_to_linalg` 正是 `ttadapter` 阶段的实现。
- **NPUOptions**：u3-l2 讲过的不可变编译选项 `@dataclass(frozen=True)`，其中的 `compile_mode` 会在 `__post_init__` 里派生出 `force_simt_only`、`force_simt_template`、`parallel_mode` 等字段。

几个**初学者容易混淆的术语**，先在这里厘清：

| 术语 | 含义 |
|---|---|
| MLIR pass | 对 IR 做一次（或一组）变换的步骤，如「把 ttir.load 变成 linalg 算子」 |
| pass manager（pass 管理器） | 一个容器，按你添加的顺序把多个 pass 串成一条流水线，然后一次性跑完 |
| pass 流水线字符串 | pass manager 序列化出的文本，形如 `builtin.module(triton-to-linalg, ...)`，能直接喂给 `triton-opt` |
| `ascend.passes.ttir` | C++ 编译出的 Python 绑定模块，提供 `add_triton_to_linalg(pm, ...)` 这类「往 pm 里注册一个 pass」的函数 |

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|---|---|
| [third_party/ascend/backend/compiler.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py) | Ascend 编译后端主体。本讲的绝对主角是其中 `ttir_to_linalg` 函数（L155-L264） |
| [python/triton/compiler/compiler.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/compiler/compiler.py) | Triton core 的编译总调度。`compile` 函数用 `**options.__dict__` 把 NPUOptions 灌进 `metadata`（L279-L285），这是理解所有门控字段来源的关键 |
| [third_party/ascend/bin/triton-mlir-opt.cpp](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/bin/triton-mlir-opt.cpp) | 一个 C++ 命令行驱动，注册 MLIR + BiShengIR 的全部方言与 pass，让 `.mlir` 文件可以在命令行被这些 pass 处理（L32-L42） |

`ascend.passes.ttir` 和 `ir.pass_manager` 本身是 C++ 编译产物（`libtriton` 的一部分），没有可直接读的源码文件，本讲通过它们在 Python 侧的**调用方式**来讲解。

## 4. 核心概念与源码讲解

### 4.1 pass manager 与 ascend.passes.ttir：编译阶段的「指令清单」

#### 4.1.1 概念说明

把编译想象成一条流水线：原材料（TTIR）进，成品（Linalg IR）出。流水线上有十几道工序（pass），每道工序只改一小部分 IR。

`ir.pass_manager`（pass 管理器）就是这份**工序清单**的容器：你按顺序往里 `add_xxx(pm, ...)` 一道道工序，最后调一次 `pm.run(mod, '阶段名')` 让它一口气跑完。这样做有两个好处：

1. **顺序可控**：pass 之间有强依赖（比如「结构化」pass 必须在「转 linalg」之前），写死顺序能避免乱序出错。
2. **可序列化**：pass manager 能把整份清单导出成一个字符串，让命令行工具照着复现。

`ascend.passes.ttir` 是 Ascend 后端用 C++（基于 MLIR 的 TableGen/Pass 机制）编译出来的 Python 绑定模块。它提供的是一个个 `add_XXX(pm, 参数...)` 函数——**不是**直接执行变换，而是「把名为 XXX 的 pass 挂到 pm 上」。真正执行要等到 `pm.run`。这种「先登记、后统一执行」的模式，和 u3-l2 里 `add_stages` 登记编译阶段是同一种设计哲学。

#### 4.1.2 核心流程

`ttir_to_linalg` 内部对 pass manager 的使用，用伪代码描述就是：

```text
pm = ir.pass_manager(mod.context)          # 1. 用模块所属 context 建一个空 pm
pm.enable_debug()                           # 2. 打开调试（影响日志/流水线串导出）
ascend.passes.ttir.add_XXX(pm, 参数...)     # 3. 按固定顺序，逐个登记 pass（十几个）
... （条件分支里再登记额外 pass） ...
pm.run(mod, 'ttir_to_linalg')               # 4. 一次性把整条流水线跑在模块上
```

关键点：第 3 步「登记」不改变 `mod`，第 4 步 `pm.run` 才真正就地改写 `mod`。所以你在源码里看到的 `add_*` 顺序，就是最终生效的 pass 顺序。

#### 4.1.3 源码精读

构造 pass manager 的两行，见 [third_party/ascend/backend/compiler.py:L190-L191](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L190-L191)：

```python
pm = ir.pass_manager(mod.context)
pm.enable_debug()
```

这两行用模块 `mod` 自带的 `context`（MLIR 里管理方言、类型的上下文）创建一个空的 pass manager。`enable_debug()` 让 manager 在后续 `pm.run` 时能打印诊断信息，并保证 `get_pipeline_str()` 能还原出可复现的流水线。

注意第 192 行有一行**被注释掉**的代码：

```python
# ascend.passes.ttir.add_auto_blockify(pm, auto_blockify_size)
```

这说明 AutoBlockify（把 grid 映射到物理核的并行块优化，见 u2-l2、u2-l3）**当前不在这个函数里登记**，而是由 BiSheng 工具链在更下游处理。读源码时遇到注释掉的 pass 要留心：它代表「设计上有这一步，但当前实现把它挪走了」。

最后统一执行，见 [third_party/ascend/backend/compiler.py:L254](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L254)：

```python
pm.run(mod, 'ttir_to_linalg')
```

第二个参数 `'ttir_to_linalg'` 是阶段标签，主要用于 debug dump 时给输出打标记。

#### 4.1.4 代码实践

> **实践目标**：确认 `add_*` 函数只是「登记」、`pm.run` 才「执行」。
>
> **操作步骤**：
> 1. 打开 [compiler.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py)，定位到 L190 的 `pm = ir.pass_manager(...)`。
> 2. 数一数 L194 到 L214 之间调用了多少个 `ascend.passes.ttir.add_*` 与 `passes.common.add_*`（不计注释）。
> 3. 确认直到 L254 的 `pm.run(mod, 'ttir_to_linalg')` 之前，没有任何一行真正改写 `mod`。
>
> **需要观察的现象**：登记阶段（L194-L226）都是 `add_xxx(pm, ...)` 形式；真正的执行只有 L254 一处。
>
> **预期结果**：你会看到主线大约 12~13 个 `add_*` 调用（含一次 `add_triton_to_structure` 的重复调用），全部在 `pm.run` 之前。
>
> 待本地验证：若你在装好环境的 NPU 机器上跑，可在 `pm.run` 前后各 `print(str(mod)[:200])`，确认前 `mod` 几乎未变、后才变成 linalg 形态。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ascend.passes.ttir.add_triton_to_linalg(pm, ...)` 不会立刻改变 IR？
**答案**：它只是把「triton-to-linalg」这个 pass 登记进 pass manager `pm`。所有变换要等到 `pm.run(mod, ...)` 统一执行时才生效。

**练习 2**：`pm = ir.pass_manager(mod.context)` 为什么必须传入 `mod.context`？
**答案**：pass manager 需要知道在哪个 MLIR context 下工作——方言、类型、属性都注册在 context 里。脱离了模块的 context，pass 无法识别模块里的算子。

---

### 4.2 ttir_to_linalg 的位置与输入：ttadapter 阶段

#### 4.2.1 概念说明

要理解 `ttir_to_linalg` 内部做了什么，先得知道它在整条编译链里的**坐标**。回顾 u3-l2：`AscendBackend.add_stages` 登记了一条阶段流水线。本讲的 `ttir_to_linalg` 就被绑在名为 `ttadapter` 的阶段上——它的输入是上一阶段 `make_ttir` 产出的优化后 TTIR（文本），输出是 Linalg IR（文本），供下游 `mlirbc`/`bcmlir` 阶段进一步处理。

这个命名容易让人误解：阶段名叫 `ttadapter`（Triton Adapter），但实际调用的函数叫 `ttir_to_linalg`。原因是它内部借用了 `triton_adapter` 工具链的能力来完成 lowering，所以阶段名沿用了 adapter 的叫法。

#### 4.2.2 核心流程

在 `force_simt_only=False`（默认的混合模式）路径下，阶段顺序是：

```text
make_ttir (ttir) ──► ttir_to_linalg (ttadapter) ──► [mlirbc ──► bcmlir] ──► npubin
   ↑ 通用 pass 优化        ↑ 本讲主角               ↑ bytecode 中转        ↑ BiSheng 出 .o
```

- 入口：函数签名 `ttir_to_linalg(mod, metadata, opt, *, named_ops=False)`，`mod` 是 TTIR 模块。
- 出口：`return str(mod)`——把跑完 pass 后的模块重新序列化成 Linalg IR 文本字符串。

> 注意：只有 `force_simt_only=True`（`compile_mode="simt_only"`）时才会跳过这条主线，改走 `ttir_to_npubin` 直达二进制（见 u6）。本讲聚焦默认主线。

#### 4.2.3 源码精读

阶段绑定见 [third_party/ascend/backend/compiler.py:L1269-L1275](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L1269-L1275)：

```python
def add_stages(self, stages, options, language):
    if self.target.backend == "npu":
        stages["ttir"] = lambda src, metadata: make_ttir(src, metadata, options)
        if options.force_simt_only:
            stages["npubin"] = (lambda src, metadata: ttir_to_npubin(src, metadata, options))
            return
        stages["ttadapter"] = lambda src, metadata: ttir_to_linalg(src, metadata, options, named_ops=True)
```

可以看到：`ttadapter` 阶段调用的正是 `ttir_to_linalg`，而且默认把 `named_ops=True` 传进去（这会影响 linalg 输出是否带具名算子，供运行时识别）。

函数入口与出口见 [third_party/ascend/backend/compiler.py:L155-L158](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L155-L158) 与 [L264](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L264)：

```python
def ttir_to_linalg(mod, metadata, opt, *, named_ops=False):
    # use triton_adapter to lower Triton-MLIR to linalg
    ttir_code = str(mod)          # 入口：拿到 TTIR 文本
    ...
    return str(mod)               # 出口：返回 Linalg IR 文本
```

L158 的 `ttir_code = str(mod)` 看似只是取文本，但它还有第二个用途：后面用正则扫描这段文本，判断 kernel 是否含 AutoBlockify 黑名单算子（L162-L170）。这是一个**运行时静态分析**的小细节——在跑 pass 之前先读一遍 IR 文本做策略判断。

#### 4.2.4 代码实践

> **实践目标**：在阶段流水线里定位 `ttir_to_linalg`，确认它前后各是谁。
>
> **操作步骤**：
> 1. 阅读 [add_stages](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L1269-L1290)（L1269-L1290）。
> 2. 画出 `use_bytecode=True`（默认）且 `force_simt_only=False` 时的完整阶段链。
> 3. 标注每个阶段对应的处理函数。
>
> **预期结果**：`ttir(make_ttir) → ttadapter(ttir_to_linalg) → mlirbc(linalg_to_bc_by_triton_mlir_opt) → bcmlir(bc_to_linalg_by_bishengir_opt) → npubin(linalg_to_bin_...)`。
>
> 待本地验证：开启 `TRITON_DEBUG=1` 跑一个 kernel，在 dump 目录里应能看到 `kernel.ttir.mlir`（make_ttir 产物）和 `kernel.ttadapter.mlir`（本函数产物）两个文件，印证它在两者之间。

#### 4.2.5 小练习与答案

**练习 1**：阶段名是 `ttadapter`，但函数名是 `ttir_to_linalg`，这两者矛盾吗？
**答案**：不矛盾。阶段名反映「用了 triton_adapter 工具链」，函数名反映「这一步把 TTIR lower 成 Linalg IR」。它们是同一阶段的不同命名视角。

**练习 2**：为什么 `ttir_to_linalg` 返回的是字符串 `str(mod)` 而不是模块对象？
**答案**：core 的阶段流水线约定——除末阶段返回 `bytes`，其余阶段之间用文本字符串传递 IR。这样每个阶段都可以独立 dump、独立用命令行工具处理，便于调试。

---

### 4.3 pass 编排总览：主线 pass 的完整顺序

#### 4.3.1 概念说明

这是本讲的核心：`ttir_to_linalg` 里到底登记了哪些 pass，按什么顺序。这些 pass 大致可分为四类：

1. **控制流优化**：整理 Triton 控制流（if/scf），为后续变换扫清障碍。
2. **结构化与离散访存处理**：把指针运算、掩码访存「张量化」（结构化），把无法张量化的离散访存拆成可处理的形式。这是本单元 u4-l2~u4-l4 的主题。
3. **方言 lowering**：把同步、直方图、内联汇编等昇腾特性映射到专用方言（hivm/hfusion/llvm）。这是 u4-l6 的主题。
4. **最终转 linalg**：把整理好的 ttir 算子系统性转成 linalg/memref 算子。这是 u4-l5 的主题。

#### 4.3.2 核心流程

下面是**主线 pass 的固定顺序**（忽略条件分支，先看骨架）。顺序就是源码里自上而下的调用顺序：

```text
1. add_triton_control_flow_opt           控制流优化
   ── (可选) add_auto_scheduling 分支 ──
2. add_triton_to_structure               指针/掩码张量化（第 1 次）
3. add_discrete_mask_access_conversion   离散掩码访存转换
4. add_triton_to_annotation              compile_hint → annotation 方言
5. add_triton_to_unstructure             离散访存标量化
6. add_triton_to_hivm                    跨核同步 → hivm 方言
7. add_triton_to_hfusion                 直方图 → hfusion 方言
8. add_triton_to_llvm                    内联汇编 → llvm/cce
9. add_bubble_up_operation               extract 上提优化
10. add_triton_to_structure              指针/掩码张量化（第 2 次，兜底）
11. add_triton_to_linalg                 ★ 最终 TTIR → Linalg
   ── (可选) enable_dynamic_cv_pipeline 分支 ──
12. add_dynamic_cv_pipeline              Cube-Vector 动态流水线（CV，见 u8）
```

几个要点：

- `add_triton_to_structure` 出现**两次**（第 2 步和第 10 步）。第一次处理原始 TTIR；第二次在 `add_bubble_up_operation` 之后兜底，处理被「上提」操作新暴露出来的指针表达式。
- 真正把 TTIR 变成 Linalg IR 的是第 11 步 `add_triton_to_linalg`，前 10 步都是「为这一步做准备」的预处理 pass。
- 第 12 步 `add_dynamic_cv_pipeline` 是**条件 pass**，默认不一定开启（见 4.4）。

#### 4.3.3 源码精读

主线 pass 的登记代码见 [third_party/ascend/backend/compiler.py:L194-L214](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L194-L214)：

```python
ascend.passes.ttir.add_triton_control_flow_opt(pm)
if (metadata["add_auto_scheduling"]):
    ascend.passes.ttir.add_dag_sync(pm)
    ascend.passes.ttir.add_dag_scope(pm)
    passes.common.add_cse(pm)
    passes.common.add_canonicalizer(pm)
    ascend.passes.ttir.add_dag_ssbuffer(pm)
    passes.common.add_cse(pm)
    passes.common.add_canonicalizer(pm)
ascend.passes.ttir.add_triton_to_structure(pm, enable_mask_fallback_conversion, optimize_dynamic_offset)
ascend.passes.ttir.add_discrete_mask_access_conversion(pm, compile_on_910_95, force_simt_template,
                                                       enable_sync_block_lock)
ascend.passes.ttir.add_triton_to_annotation(pm)
ascend.passes.ttir.add_triton_to_unstructure(pm, compile_on_910_95, force_simt_template)
ascend.passes.ttir.add_triton_to_hivm(pm)
ascend.passes.ttir.add_triton_to_hfusion(pm, compile_on_910_95)
ascend.passes.ttir.add_triton_to_llvm(pm)
ascend.passes.ttir.add_bubble_up_operation(pm)
ascend.passes.ttir.add_triton_to_structure(pm, enable_mask_fallback_conversion, optimize_dynamic_offset)
ascend.passes.ttir.add_triton_to_linalg(pm, False, named_ops, enable_nd2nz_on_vector, enable_select_analysis,
                                        compile_on_910_95)
```

注意中间穿插的 `passes.common.add_cse` / `add_canonicalizer`（出现在 `add_auto_scheduling` 分支里）——它们是 MLIR 通用的公共子表达式消除和规范化 pass，不属于 `ascend.passes`，作用是「在 dag 系列变换之间清理冗余」。这印证了 u3-l3 讲过的：cse/canonicalizer 是所有后端共享的通用 pass。

`add_auto_scheduling` 分支（[L195-L202](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L195-L202)）是一个较少开启的可选调度优化，会额外登记 `add_dag_sync` / `add_dag_scope` / `add_dag_ssbuffer` 三个与跨核数据流（SSBUFFER）相关的 pass，默认 `add_auto_scheduling=False`（见 4.4.3），所以日常编译这条分支不进。

#### 4.3.4 代码实践

> **实践目标**：把上面的「骨架顺序」和源码逐行对上号。
>
> **操作步骤**：
> 1. 打印本讲的「主线 pass 顺序」清单。
> 2. 在 [compiler.py L194-L214](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L194-L214) 里，为每个 `add_*` 标上序号 1~11。
> 3. 特别圈出 `add_triton_to_structure` 的两次出现，以及每个带 `compile_on_910_95` 参数的 pass。
>
> **需要观察的现象**：哪些 pass 接收 `compile_on_910_95` 参数？（`add_discrete_mask_access_conversion`、`add_triton_to_unstructure`、`add_triton_to_hfusion`、`add_triton_to_linalg`）哪些完全不接参数？（`add_triton_to_annotation`、`add_triton_to_hivm`、`add_triton_to_llvm`、`add_bubble_up_operation`）
>
> **预期结果**：参数的多少暗示了 pass 对硬件代际/模式的敏感度——接 `compile_on_910_95` 的 pass 内部会按芯片代际走不同分支；不接参数的 pass 与硬件代际无关。

#### 4.3.5 小练习与答案

**练习 1**：`add_triton_to_structure` 为什么要在流水线里跑两次？
**答案**：第一次把原始 TTIR 里的指针/掩码表达式张量化；第二次在 `add_bubble_up_operation`（extract 上提）之后，处理因为算子上提而新产生的、尚未结构化的指针表达式，作为兜底。

**练习 2**：在主线 11 个 pass 里，哪一个是真正把 TTIR「变成」Linalg IR 的？
**答案**：第 11 个 `add_triton_to_linalg`。前 10 个都是预处理——整理控制流、处理访存、lowering 昇腾专用方言，为最终转 linalg 创造条件。

---

### 4.4 两大门控开关：compile_on_910_95（950）与 enable_dynamic_cv_pipeline

#### 4.4.1 概念说明

`ttir_to_linalg` 里的 pass 不是每次都全跑，而是由一组**门控字段**决定。这些字段全部来自 `NPUOptions`，再经 Triton core 的 `compile` 函数灌进 `metadata`。本小节聚焦两个最重要的总开关：

- **`compile_on_910_95`**：是否在「910_95」代芯片（即 Ascend 910B3 / 950 系列，社区常简称 **950**）上编译。它被当作布尔参数传给多个 pass，让这些 pass 内部针对 950 的硬件特性走不同分支。**这就是学习目标里说的「950 开关」。**
- **`enable_dynamic_cv_pipeline`**：是否启用 **Cube-Vector 动态流水线**（DynamicCVPipeline，u8 单元主题）。它门控整个第 12 步 `add_dynamic_cv_pipeline` 分支，以及一批配套选项的改写。

此外还有几个次要条件：`add_auto_scheduling`（门控 dag 调度分支）、`force_simt_template`（由 `compile_mode="unstructured_in_simt"` 派生，传给离散访存 pass）、`enable_sync_block_lock`、`enable_mask_fallback_conversion`、`optimize_dynamic_offset` 等，它们都是传给个别 pass 的细粒度开关。

#### 4.4.2 核心流程

字段来源链路（这是理解所有门控的关键）：

```text
NPUOptions (dataclass)  ──parse_options()──►  options (含 compile_on_910_95, enable_dynamic_cv_pipeline ...)
        │                          │
        │                   __post_init__ 派生 force_simt_only / force_simt_template / parallel_mode
        ▼
core compile():  metadata = {"hash", "target", **options.__dict__, ...}
        │                  ▲ 把 NPUOptions 的每个字段原样铺进 metadata
        ▼
ttir_to_linalg(mod, metadata, opt):  读 metadata["compile_on_910_95"] 等做分支
```

两个总开关的默认值（都走**懒初始化**，在 `parse_options` 里）：

| 字段 | 默认值来源 | 默认值 |
|---|---|---|
| `compile_on_910_95` | `is_compile_on_910_95()`（硬件探测） | 取决于目标芯片是否为 950 代 |
| `enable_dynamic_cv_pipeline` | 默认等于 `is_compile_on_910_95()` | 即 **950 代默认开启** CV 流水线 |

CV 流水线分支内部还会做一批**副作用改写**：把 `set_workspace_multibuffer` 置 0、强制 `enable_mixed_cv=True`、`disable_auto_inject_block_sync=True`，再登记 `add_dynamic_cv_pipeline`。

#### 4.4.3 源码精读

门控字段的**读取**见 [third_party/ascend/backend/compiler.py:L177-L186](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L177-L186)：

```python
enable_nd2nz_on_vector = metadata["enable_nd2nz_on_vector"]
enable_select_analysis = metadata["enable_select_analysis"]
compile_on_910_95 = metadata["compile_on_910_95"]
force_simt_template = metadata["force_simt_template"]
enable_sync_block_lock = metadata["enable_sync_block_lock"]
enable_mask_fallback_conversion = metadata["enable_mask_fallback_conversion"]
optimize_dynamic_offset = metadata["optimize_dynamic_offset"]
auto_blockify_size = metadata["auto_blockify_size"]
```

这些字段**在本文件里只读不写**，说明它们是从外部灌进 `metadata` 的。灌入发生在 Triton core 的 `compile`，见 [python/triton/compiler/compiler.py:L279-L285](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/compiler/compiler.py#L279-L285)：

```python
metadata = {
    "hash": hash,
    "target": target,
    **options.__dict__,     # ← NPUOptions 的全部字段都进来了
    **env_vars,
}
```

两个开关的**懒初始化**见 [third_party/ascend/backend/compiler.py:L1219-L1224](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L1219-L1224)：

```python
# Lazy init compile_on_910_95 if not provided
if options.compile_on_910_95 is None:
    object.__setattr__(options, "compile_on_910_95", is_compile_on_910_95())
# Lazy init enable_dynamic_cv_pipeline if not provided
if options.enable_dynamic_cv_pipeline is None:
    object.__setattr__(options, "enable_dynamic_cv_pipeline", is_compile_on_910_95())
```

注意：因为 `NPUOptions` 是 `@dataclass(frozen=True)`（不可变），所以这里用 `object.__setattr__` 绕过冻结限制来赋值（见 [L988-L989](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L988-L989)）。`enable_dynamic_cv_pipeline` 默认取 `is_compile_on_910_95()`，所以**950 代芯片默认启用 CV 流水线**。

CV 流水线分支本身见 [third_party/ascend/backend/compiler.py:L215-L226](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L215-L226)：

```python
if metadata["enable_dynamic_cv_pipeline"]:
    metadata["set_workspace_multibuffer"] = 0
    metadata["enable_mixed_cv"] = True
    metadata["disable_auto_inject_block_sync"] = True
    ascend.passes.ttir.set_enable_cube_block_merge(metadata["enable_cube_block_merge"])
    ascend.passes.ttir.set_enable_ub_refine_opt(mod, metadata["enable_ub_refine_opt"])
    # Must run before add_dynamic_cv_pipeline because the driven
    # AddMultiBufferInnerScope pass reads the module-level
    # `ssbuffer.insertionOptimization` attribute (set here) at run time.
    ascend.passes.ttir.set_enable_buffer_insert_optimization(mod, metadata["enable_buffer_insert_optimization"])
    ascend.passes.ttir.add_dynamic_cv_pipeline(pm, compile_on_910_95)
```

读这段要注意三件事：

1. **副作用改写 metadata**：开启 CV 流水线会强制改写多个 metadata 字段，这些改写会影响下游 npubin 阶段的代码生成。
2. **`set_*` 不是登记 pass**：`set_enable_cube_block_merge` / `set_enable_ub_refine_opt` / `set_enable_buffer_insert_optimization` 是「设置模块级属性/全局开关」，不是 `add_*` 形式的 pass 登记。它们要在 `add_dynamic_cv_pipeline` **之前**调用——注释明确解释了原因：被驱动的 `AddMultiBufferInnerScope` pass 会在运行时读取这里设置的模块属性。
3. **最后才登记 `add_dynamic_cv_pipeline(pm, compile_on_910_95)`**：CV 流水线本身是第 12 个、也是最靠后的主线 pass。

#### 4.4.4 代码实践

> **实践目标**：区分哪些 pass 受 950 开关控制、哪些受 CV 流水线开关控制。
>
> **操作步骤**：
> 1. 在主线 pass 清单里，把接收 `compile_on_910_95` 参数的 pass 涂一种颜色（受 950 影响）。
> 2. 把 `add_dynamic_cv_pipeline` 及它前面的 `set_*` 代码块涂另一种颜色（受 CV 流水线影响）。
> 3. 写一句话总结：「950 影响的是若干个 pass 的**内部行为**；CV 流水线影响的是**是否多跑一整个 pass 分支**。」
>
> **需要观察的现象**：950 参数是「细粒度」的（让个别 pass 内部分流），而 CV 流水线是「粗粒度」的（整块代码加不加）。
>
> **预期结果**：受 950 影响的 pass 有 `add_discrete_mask_access_conversion`、`add_triton_to_unstructure`、`add_triton_to_hfusion`、`add_triton_to_linalg`、`add_dynamic_cv_pipeline`；受 CV 流水线影响的只有 `enable_dynamic_cv_pipeline` 分支这一块。
>
> 待本地验证：在 950 机器上跑同一 kernel 两次，一次 `enable_dynamic_cv_pipeline` 默认（开）、一次显式设为关，dump 出的 `kernel.ttadapter.mlir` 会不同——开启时 linalg 之上会多出 CV 流水线插入的标记（详见 u8）。

#### 4.4.5 小练习与答案

**练习 1**：`metadata["compile_on_910_95"]` 在 `ttir_to_linalg` 里被赋值过吗？它的值从哪来？
**答案**：在本文件里只读不写。它来自 `NPUOptions.compile_on_910_95`，由 core 的 `compile` 用 `**options.__dict__` 铺进 `metadata`；`NPUOptions` 里若用户未指定，则在 `parse_options` 里用 `is_compile_on_910_95()` 硬件探测懒初始化。

**练习 2**：为什么 `set_enable_buffer_insert_optimization` 必须在 `add_dynamic_cv_pipeline` 之前调用？
**答案**：注释说明，被 CV 流水线驱动的 `AddMultiBufferInnerScope` pass 在**运行时**（即 `pm.run` 执行时）会读取这里设置的模块级属性 `ssbuffer.insertionOptimization`。若顺序反了，该属性尚未写入，pass 读不到正确值。这体现了「登记顺序 = 执行顺序」之外，还有「属性必须先于读取它的 pass 登记」这一约束。

---

### 4.5 pass manager 与 triton-opt 命令行的对应关系

#### 4.5.1 概念说明

MLIR 生态有一条贯穿始终的设计：**同一组 pass 既能在 Python 里用 pass manager 跑，也能在命令行用 `*-opt --pass-pipeline="..."` 跑**，两者等价。Triton-Ascend 在 `ttir_to_linalg` 里专门利用了这一点——当 `opt.debug=True` 时，它会把 Python 里登记的 pass 流水线导出成一条命令行，打印出来，让你能脱离 Python 环境复现调试。

承担命令行执行的 C++ 工具是 `triton-opt`（以及结构更全的 `triton-mlir-opt`）。后者就是我们本讲列出的源码文件 [triton-mlir-opt.cpp](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/bin/triton-mlir-opt.cpp)：它的 `main` 注册了 MLIR 和 BiShengIR 的**全部方言与 pass**，然后交给 MLIR 通用的 `MlirOptMain` 去解析命令行参数、加载 `.mlir`、按 `--pass-pipeline` 跑流水线。

#### 4.5.2 核心流程

对应关系如下：

```text
Python 侧                                     命令行侧
────────────────────────────────             ────────────────────────────────
pm = ir.pass_manager(ctx)                     triton-opt kernel.ttir.mlir \
ascend.passes.ttir.add_XXX(pm, ...)   ══►      --pass-pipeline="<流水线字符串>" \
...                                                 --mlir-print-debuginfo \
str = pm.get_pipeline_str()  ────────►        -o kernel.ttadapter.mlir
pm.run(mod, 'ttir_to_linalg')
```

- `pm.get_pipeline_str()` 把 manager 里登记的所有 pass 序列化成一段形如 `builtin.module(cse, canonicalizer, triton-to-linalg{...}, ...)` 的文本。
- 这段文本被原样塞进命令行的 `--pass-pipeline=`。
- 于是命令行跑出来的结果，和 Python 里 `pm.run` 的结果**理论上一致**——这就是「复现调试」的基础。

#### 4.5.3 源码精读

导出命令行的 debug 代码块见 [third_party/ascend/backend/compiler.py:L240-L252](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L240-L252)：

```python
if opt.debug:
    # Print the equivalent triton-opt command line so the pass
    # pipeline can be reproduced and debugged outside of Python.
    print_src_path, print_dst_path = _get_dump_paths(metadata["hash"], src_path, dst_path)
    cmd = [
        _get_triton_opt_path(),
        print_src_path,
        f"--pass-pipeline={pm.get_pipeline_str()}",
        "--mlir-print-debuginfo",
        "-o",
        print_dst_path,
    ]
    print(f"[DEBUG] cmd list: {shlex.join(cmd)}")
```

注意第 247 行 `f"--pass-pipeline={pm.get_pipeline_str()}"`——这就是 Python pass manager 与命令行 `triton-opt` 之间的**那座桥**。注释明确写了它的用途：让 pass 流水线能在 Python 之外被复现和调试。这个 debug 块在 `pm.run`（L254）之前执行，所以打印出的是「即将运行」的流水线。

命令行工具的 C++ 实现见 [third_party/ascend/bin/triton-mlir-opt.cpp:L32-L42](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/bin/triton-mlir-opt.cpp#L32-L42)：

```cpp
int main(int argc, char **argv) {
  mlir::DialectRegistry registry;
  mlir::registerAllDialects(registry);        // 注册所有 MLIR 方言
  bishengir::registerAllDialects(registry);   // 注册所有 BiShengIR 方言
  return mlir::asMainReturnCode(mlir::MlirOptMain(
      argc, argv, "Triton-Ascend optimizer driver\n", registry));
}
```

它本身**不含任何 pass 逻辑**——只是把方言和 pass 注册进一个 registry，然后交给 MLIR 标准的 `MlirOptMain`。`MlirOptMain` 负责解析 `--pass-pipeline`、加载输入 `.mlir`、依次执行流水线里的 pass、写出结果。从 [bin/CMakeLists.txt](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/bin/CMakeLists.txt) 可以看到，它链接了 MLIR 全套 pass 库和 BiShengIR 的各方言库（HACC/HFusion/HIVM/Annotation 等），所以才能识别这些昇腾专用方言里的算子和 pass。

> 三个名字相近的工具，别混（路径在 [utils.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/utils.py) 里由 `_get_*_path` 解析）：
> - `triton-opt`：本讲 debug 命令用的工具，跑 TTIR→ttadapter 的 pass 流水线。
> - `triton-mlir-opt`：上面的 [triton-mlir-opt.cpp](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/bin/triton-mlir-opt.cpp)，用于 `mlirbc` 阶段把 Linalg IR 文本转成 MLIR 字节码。
> - `triton_adapter_opt`：`ttir_to_linalg` 开头 L175 取过它的路径，是 triton_adapter 工具链的 opt 工具。

#### 4.5.4 代码实践

> **实践目标**：拿到 `ttir_to_linalg` 的等价命令行，体会 Python 与命令行的对应。
>
> **操作步骤**：
> 1. 在装好 Triton-Ascend 的环境里设 `export TRITON_DEBUG=1`。
> 2. 跑一个简单 kernel（如 `tutorials/01-vector-add.py`）。
> 3. 在输出里搜索 `[DEBUG] cmd list:`，找到 `triton-opt ... --pass-pipeline=...` 那一行。
> 4. 把 `--pass-pipeline=` 后面的字符串单独复制出来，观察里面依次列出的 pass 名（如 `triton-to-structure`、`triton-to-linalg` 等），与本讲 4.3 的清单对照。
>
> **需要观察的现象**：命令行里 pass 的出现顺序，与 [compiler.py L194-L226](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L194-L226) 里 `add_*` 的调用顺序一致；带开关参数的 pass 会带上 `{参数}` 后缀。
>
> **预期结果**：你能在 pipeline 字符串里数出与 4.3 相同数量的主线 pass，顺序吻合，从而验证「Python 登记 = 命令行执行」。
>
> 待本地验证：能否直接用这条命令行对 dump 出的 `kernel.ttir.mlir` 复现 `kernel.ttadapter.mlir`（结果应一致）。

#### 4.5.5 小练习与答案

**练习 1**：`pm.get_pipeline_str()` 解决了什么问题？
**答案**：它把 Python 里用 `add_*` 登记的 pass 流水线序列化成文本，使得这条流水线可以直接作为 `--pass-pipeline=` 喂给 `triton-opt`，实现「在 Python 之外、纯命令行复现调试」。

**练习 2**：[triton-mlir-opt.cpp](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/bin/triton-mlir-opt.cpp) 里为什么不写任何 pass 的实现？
**答案**：所有 pass 都是作为库注册进 registry 的（链接自 MLIR 和 BiShengIR 的 pass 库）。这个 cpp 只是「装配工」：把方言和 pass 注册好，交给 MLIR 通用的 `MlirOptMain` 去解释命令行并执行。真正的 pass 实现在 `third_party/ascend/lib/` 下的各 pass 目录里（后续讲义会精读）。

---

## 5. 综合实践

**任务**：画出 `ttir_to_linalg` 的完整 pass 流水线图，并标注门控条件。这是本讲学习目标的综合检验。

**操作步骤**：

1. 准备一张流程图（纸笔或画图工具均可），纵向从上到下画 12~13 个方框，每个方框代表一个 pass（含主线 11 个 + CV 分支 1 个 + 可选的 dag 分支）。
2. 用**三种颜色/线型**区分：
   - **黑色实线**：无条件执行的主线 pass（如 `add_triton_control_flow_opt`、`add_triton_to_annotation`、`add_triton_to_hivm`、`add_triton_to_llvm`、`add_bubble_up_operation`）。
   - **蓝色**：接收 `compile_on_910_95`（950）参数的 pass（`add_discrete_mask_access_conversion`、`add_triton_to_unstructure`、`add_triton_to_hfusion`、`add_triton_to_linalg`），在方框角上标「950」。
   - **绿色虚线框**：受条件门控的整块分支——`add_auto_scheduling` 门控的 dag 三连，以及 `enable_dynamic_cv_pipeline` 门控的 CV 块（含 `set_*` 与 `add_dynamic_cv_pipeline`）。
3. 在两个重复的 `add_triton_to_structure` 之间画一根连线，标注「第二次为兜底」。
4. 结合本讲 4.5 的实践，开启 `TRITON_DEBUG=1`，把 `[DEBUG] cmd list:` 里的 `--pass-pipeline` 字符串和你的图逐项核对。

**预期产出**：一张能回答以下三个问题的图——
- 主线 pass 的完整顺序是什么？（4.3）
- 哪些 pass 受 950 开关影响？（4.4）
- CV 流水线分支挂在哪、它开启时会改写哪些 metadata？（4.4.3）

**待本地验证**：图上每个方框的 pass 名，都应能在 [compiler.py L194-L226](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L194-L226) 找到对应行，并在 debug 打印的 pipeline 字符串里找到同名条目。

## 6. 本讲小结

- `ttir_to_linalg` 是 `ttadapter` 编译阶段的实现，把上一阶段 `make_ttir` 产出的 TTIR 文本 lower 成 Linalg IR 文本，输入输出都是字符串。
- 它用 `ir.pass_manager` 构造一个 pass 管理器，用 `ascend.passes.ttir.add_*` 系列函数**按固定顺序登记**十几个 pass，最后用一次 `pm.run` 统一执行；登记顺序就是执行顺序。
- 主线顺序为：控制流优化 → （可选 dag 调度）→ 结构化 → 离散掩码转换 → annotation → 标量化 → hivm → hfusion → llvm → bubble-up → 二次结构化 → **转 linalg** → （可选 CV 流水线）。
- 真正产出 Linalg IR 的是 `add_triton_to_linalg`，前面所有 pass 都是预处理；`add_triton_to_structure` 会跑两次，第二次为兜底。
- 两个总门控开关：`compile_on_910_95`（950，作为参数影响若干 pass 内部行为）与 `enable_dynamic_cv_pipeline`（CV 流水线，门控一整个 pass 分支与一批 metadata 改写；950 代默认开启）。
- 所有门控字段都源自 `NPUOptions`，由 core 的 `compile` 用 `**options.__dict__` 灌进 `metadata`——这是理解 `ttir_to_linalg` 分支逻辑的钥匙。
- `pm.get_pipeline_str()` 把 Python 流水线导出成文本，与 `triton-opt --pass-pipeline=...` 一一对应；`TRITON_DEBUG=1` 时会打印等价命令行，可在命令行复现调试。

## 7. 下一步学习建议

本讲是「总览」，接下来按 pass 分组逐篇深入：

- **u4-l2（TritonToStructured）**：精读主线第 2/10 步 `add_triton_to_structure`，看它如何把指针运算和掩码建模成 `PtrState`/`MaskState` 并张量化。
- **u4-l3（DiscreteMaskAccessConversion）**与 **u4-l4（TritonToUnstructure）**：精读第 3、5 步，看离散掩码访存如何被转换或标量化，以及 `compile_on_910_95`/`force_simt_template` 参数如何让它们走不同分支。
- **u4-l5（TritonToLinalg）**：精读第 11 步 `add_triton_to_linalg`，看最终的 TTIR→Linalg 算子映射（Load/Store/Reduce/Matmul 等 Converter）。
- **u4-l6（其他 lowering pass）**：精读 annotation/hivm/hfusion/llvm 四个方言 lowering pass。
- **u8 单元**：当你想理解本讲第 12 步 `add_dynamic_cv_pipeline` 分支时，再去读 u8-l2/u8-l3 的 DynamicCVPipeline 源码。

建议在进入下一篇之前，先完成本讲「综合实践」的流程图——它是后续所有 pass 精读的导航地图。
