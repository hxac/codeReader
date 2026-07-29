# 默认变换流水线 get_default_passes

## 1. 本讲目标

上一讲（u5-l1）我们建立了「Pass + Instrument」的变换框架，以及 `IRRewriter`/`IRVisitor` 的访问者模式。本讲把镜头拉远，从**整条流水线**的视角，回答三个问题：

1. **顺序**：Tilus 在编译一个 `Script` 时，默认会按什么顺序运行哪些 Pass？每个 Pass 干什么？
2. **时机**：为什么 `layout_inference`（布局推理）要在流水线里**跑两次**——一次在 `lower_load_store` 之前，一次在之后？
3. **支撑**：标量分析（`analyze_scalar`）产出什么、被谁消费、为什么也要跑两次？

学完后，你应当能：默写出默认流水线的 12 个步骤及其职责；解释布局推理与 `lower_load_store` 的前后依赖关系；并用 `dump_ir` 把任意一个内核各 Pass 之后的 IR 导出来对照阅读。

## 2. 前置知识

本讲默认你已经掌握（来自前序讲义）：

- **Pass 框架**（u5-l1）：`Pass.process_function` 对每个函数跑一次变换，`apply_transforms` 按序运行 Pass 并在前后回调 `Instrument`；`IRRewriter` 返回新节点、`visit_*` 返回 `None` 会把 `InstStmt` 塌缩为空 `SeqStmt`。
- **布局推理**（u4-l5）：每条指令自带推理/验证规则，`LayoutInferencePass` 的流程是「先应用用户 `AnnotateLayoutInst` 硬种子 → `infer_layout` 不动点求解 → `verify_layouts` 校验」；寄存器/共享张量用 `optional_layout` 支持创建时留空（`None`），访问 `.layout` 在未绑定时会抛 `ValueError`。
- **IR 骨架**（u3-l3/u3-l4）：`Program`/`Function`/`Metadata`/`Stmt`；`Instruction` 的 `output/inputs/attributes`；功能指令与副作用指令的区别。

一个关键直觉先放在这里：Tilus IR 是**不可变**的，所有 Pass 都是「读旧 IR、返回新 IR」，且 `apply_transforms` 在每个 Pass 之后只保留新版本。所以「跑两次」并不是原地修改两次，而是流水线里出现了两个**独立的** Pass 实例。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `python/tilus/transforms/__init__.py` | 定义 `get_default_passes()`，返回默认 12 步流水线 |
| `python/tilus/drivers.py` | `optimize_program` 调用 `get_default_passes()`，并按 `debug.dump_ir` 挂载 `DumpIRInstrument` |
| `python/tilus/transforms/base.py` | `Pass`/`PassContext`/`apply_transforms` 框架（u5-l1 已讲） |
| `python/tilus/transforms/declare_to_let.py` | 把只赋值一次的 `DeclareStmt` 转成 `LetStmt` |
| `python/tilus/transforms/let_propogation.py` | 传播/内联 `LetStmt` 绑定 |
| `python/tilus/transforms/lower_assume.py` | 把 `AssumeInst` 落地为 `param2divisibility` |
| `python/tilus/transforms/lower_param_only_expr.py` | 把必须「只依赖参数」的表达式规范化 |
| `python/tilus/transforms/scalar_analyze.py` | 标量分析 Pass 入口（委托给 `ir/analyzers/scalar_analyzer.py`） |
| `python/tilus/ir/analyzers/scalar_analyzer.py` | `ScalarSet` 抽象域与不动点分析算法 |
| `python/tilus/transforms/lower_print_tmem_tensor.py` | 把对 TMEM 张量的打印降级为 `tcgen05_load` |
| `python/tilus/transforms/layout_inference.py` | 布局推理 Pass（u4-l5 已讲细节） |
| `python/tilus/transforms/lower_load_store.py` | 把高层 load/store 降级为 generic 形式 |
| `python/tilus/transforms/bound_aware_simplify.py` | 利用标量分析的界做界感知化简 |
| `python/tilus/transforms/dead_code_elimination.py` | 死代码消除（u5-l3 会深入） |
| `python/tilus/transforms/instruments/dump_ir.py` | `DumpIRInstrument`：每个 Pass 之后把 IR 落盘 |

---

## 4. 核心概念与源码讲解

### 4.1 流水线全景：get_default_passes 的十二步顺序

#### 4.1.1 概念说明

`get_default_passes()` 是 Tilus 高层（Tilus IR）优化的**唯一入口清单**。它返回一个有序的 `Pass` 列表，`drivers.optimize_program` 会原封不动地交给 `apply_transforms` 顺序执行。理解这条流水线，就理解了「从转译器产出的原始 IR」到「可以交给后端代码生成的规整 IR」之间发生了什么。

这 12 步可以粗分为四段：

1. **前端规范化**（declare_to_let → let_propagation）：把语句整理成规整的 `LetStmt` 形式。
2. **提示落地与表达式规范化**（lower_assume → lower_param_only_expr → analyze_scalar → lower_print_tmemory_tensor）：把用户提示转成 metadata、规整必须只依赖参数的表达式、跑一次标量分析、降级 TMEM 打印。
3. **布局与访存**（layout_inference → lower_load_store → layout_inference）：先推理布局、再降级访存指令、再补推理一次（本讲的核心）。
4. **收尾**（bound_aware_simplify → analyze_scalar → dead_code_elimination）：界感知化简、刷新分析、删死代码。

#### 4.1.2 核心流程

完整的顺序清单如下（注意 `layout_inference` 和 `analyze_scalar` 各出现两次）：

```text
1.  declare_to_let              把只赋值一次的 DeclareStmt → LetStmt
2.  let_propagation             内联/传播 LetStmt 绑定
3.  lower_assume                AssumeInst → metadata.param2divisibility
4.  lower_param_only_expr       规整“只依赖参数”的表达式（grid_blocks、AllocateGlobal 尺寸）
5.  analyze_scalar              标量分析①：产出 Analysis（divisibility/上下界）
6.  lower_print_tmemory_tensor  对 TMEM 张量的打印降级为 tcgen05_load
7.  layout_inference            布局推理①：为用户书写的高层张量补全布局
8.  lower_load_store            把 LoadGlobal/StoreGlobal/CopyAsync 降级为 generic 形式
9.  layout_inference            布局推理②：补全降级后新张量的布局并重新校验
10. bound_aware_simplify        利用界信息化简（删 0/1 次循环、折叠比较）
11. analyze_scalar              标量分析②：在化简后刷新 Analysis
12. dead_code_elimination       删除无人消费的功能指令
```

> 小提示：dump 出来的文件名里，Pass 名是类名去掉 `Pass` 后缀（如 `DeadCodeEliminationPass` → `DeadCodeElimination`），见 [base.py:68-70](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/base.py#L68-L70)。

#### 4.1.3 源码精读

流水线的定义只有十几行，却决定了整个高层优化的面貌：

[transforms/__init__.py:31-45](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/__init__.py#L31-L45) —— `get_default_passes()` 返回 12 个 Pass 实例，顺序即执行顺序。

真正调用它的地方是 `optimize_program`：

[drivers.py:62-93](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py#L62-L93) —— 它先 `transforms = get_default_passes()`，可选地追加一个 `inject_print_instruction_pass`（仅当设置了 `debug_block`），然后用 `with PassContext() as ctx:` 圈定作用域；若 `debug.dump_ir` 为真，就 `ctx.dump_ir(cache_dir / "ir")` 挂载仪器，最后 `apply_transforms(program, transforms)`。

`apply_transforms` 的循环正是「逐 Pass 推进、每个 Pass 前后回调仪器」：

[base.py:86-112](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/base.py#L86-L112) —— `for transform in transforms: prog = transform(prog)`，并在每步前后调用 `ctx.before_pass/after_pass`，`DumpIRInstrument` 就是在 `after_pass` 里把当前 `prog` 落盘的。

`Pass` 基类的 `process_program` 默认对每个函数跑一次 `process_function`，并用 `is` 短路（所有函数都没变就原样返回原 `Program`）：

[base.py:68-83](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/base.py#L68-L83)

#### 4.1.4 代码实践

**目标**：亲眼看到这 12 步在真实内核上的执行轨迹与每步耗时。

**步骤**：

1. 在脚本开头设置缓存目录并打开 `dump_ir`：

   ```python
   import tilus
   tilus.option.cache_dir("/tmp/tilus-pipeline-dump")
   tilus.option.debug.dump_ir(True)
   ```

2. 运行 `examples/matmul/matmul_v0.py` 中的 `MatmulV0`（用小尺寸如 128×128×128 触发一次 JIT 即可）。

3. 进入缓存目录 `/tmp/tilus-pipeline-dump/programs/<12位哈希>/ir/`，列出文件。

**需要观察的现象**：目录下应当有 `0_Original.txt`、`1_DeclareToLet.txt`、`2_LetPropogation.txt`（注意源码里就是这个拼写）、……、`7_LayoutInference.txt`、`8_LowerLoadStore.txt`、`9_LayoutInference.txt`、`10_BoundAwareSimplify.txt`、`11_AnalyzeScalar.txt`、`12_DeadCodeElimination.txt`，以及一个汇总耗时的 `lower_time.txt` 和可视化的 `programs.html`。

**预期结果**：共 13 份 IR 快照（原始 + 12 个 Pass 之后），`7_` 与 `9_` 同名都是 `LayoutInference`，`5_` 与 `11_` 同名都是 `AnalyzeScalar`。`lower_time.txt` 里能看到每个 Pass 的耗时。

> 若无 GPU 或不想真正 launch，仅触发 JIT 编译（调用一次内核即可）也会生成这些 IR 文件。具体文件命名规则见 [dump_ir.py:53-63](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/instruments/dump_ir.py#L53-L63)。

#### 4.1.5 小练习与答案

**练习 1**：`get_default_passes()` 返回的列表里，哪两个 Pass 出现了两次？为什么是这两个而不是别的？
**答案**：`layout_inference_pass()` 和 `analyze_scalar_pass()` 各出现两次。前者是因为 `lower_load_store` 前后都需要布局信息（详见 4.4）；后者是因为标量分析的界信息会被后续优化消费，且 IR 在化简后发生了变化需要刷新（详见 4.3、4.5）。

**练习 2**：如果你想临时禁用某个 Pass（比如调试时不想跑 DCE），最简单的改法在哪里？
**答案**：直接在 `get_default_passes()` 的返回列表里注释掉对应那一行即可（它是唯一的清单来源）。生产环境不建议这么做，但调试时这是最快定位「某个 Pass 是否引发了问题」的方法。

---

### 4.2 前端规范化：declare_to_let、let_propagation 与提示落地

#### 4.2.1 概念说明

转译器（u3-l2）产出的 IR 里，标量变量多以 `DeclareStmt`（带或不带初值）+ `AssignStmt` 的形式出现，这是直接照搬 Python 局部变量语义的结果。但后续 Pass（尤其是 `lower_param_only_expr`）更喜欢 `LetStmt` 这种「一次性绑定、不可变」的形式。这一段的三个 Pass 就是把 IR 整理成更规整、更利于分析的样子，并把用户的 `assume` 提示落到 metadata 上。

四个 Pass 职责一句话总结：

- **declare_to_let**：把「只被赋值一次、且未被取地址」的 `DeclareStmt` 改写成 `LetStmt`。
- **let_propagation**：当某个 `let` 变量绑定的值恰好是另一个已有的 `let` 变量时，直接用后者替换前者，去掉冗余绑定。
- **lower_assume**：把 `AssumeInst`（如 `a % 16 == 0`）解析成 `metadata.param2divisibility`，然后删除该指令。
- **lower_param_only_expr**：把「必须只依赖函数参数」的表达式（网格大小 `grid_blocks`、`AllocateGlobal` 的 size/shape/offset）内联展开，消除中间变量。

#### 4.2.2 核心流程

以 `declare_to_let` 为例，它先把整个函数扫一遍，统计每个变量被「声明带初值 / 被 Assign / 被取地址」的次数；只有计数恰好为 1（即只有那一次带初值的声明、之后再没被改、也没被取地址）的 `DeclareStmt` 才会被改写。改写发生在 `visit_SeqStmt` 里：把该声明及其之后的所有兄弟语句，整体包进一个 `LetStmt(bind_vars=[var], bind_values=[init], body=...)`。

`lower_assume` 的核心是把合取条件分解成若干 `a % c == 0` 形式的项，提取出「参数 a 能被 c 整除」这一事实，多次取最小公倍数后写进 `metadata.param2divisibility`，最后 `visit_AssumeInst` 返回 `None` 把这条提示指令从 IR 里抹掉。

`lower_param_only_expr` 依赖 `declare_to_let` 已经跑过（它的 docstring 明确写了这一点）：因为它的算法是「记录 `LetStmt` 的变量→表达式绑定，然后把目标表达式里非参数的变量不断用绑定展开，直到只剩参数」。如果变量还是 `DeclareStmt`，就没有统一的绑定可展开。

#### 4.2.3 源码精读

`declare_to_let` 的改写条件与算法在 docstring 里写得很清楚：

[declare_to_let.py:26-34](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/declare_to_let.py#L26-L34) —— 两个条件：从未被 `AssignStmt` 修改、从未被 `Address` 取地址。

真正的改写逻辑：

[declare_to_let.py:74-87](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/declare_to_let.py#L74-L87) —— 逆序扫描 `SeqStmt`，遇到满足条件的 `DeclareStmt` 时，把它及后续语句包成 `LetStmt`。

`let_propagation` 的核心是 `visit_LetStmt`：若绑定值本身是一个已在 `let_vars` 集合里的变量，就把当前绑定变量在 `self.memo` 里映射到那个旧变量（后续访问自动替换），从而跳过冗余绑定：

[let_propogation.py:58-77](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/let_propogation.py#L58-L77)

`lower_assume` 解析 `a % c == 0` 并取 lcm：

[lower_assume.py:29-63](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/lower_assume.py#L29-L63) —— 注意它**只认**这一种形状的合取项，遇到别的形状会直接 `raise RuntimeError`，这就是为什么 `assume` 只能用来表达参数的整除性（u2-l3 已述）。

随后把结果合并进 metadata：

[lower_assume.py:65-80](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/lower_assume.py#L65-L80)

`lower_param_only_expr` 的 docstring 解释了它为何必须排在 `declare_to_let` 之后：

[lower_param_only_expr.py:26-44](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/lower_param_only_expr.py#L26-L44)

它对 `AllocateGlobalInst` 的尺寸/形状/偏移做规范化（这些必须是「只依赖参数」的，因为要用于一次性分配 workspace）：

[lower_param_only_expr.py:104-121](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/lower_param_only_expr.py#L104-L121)

#### 4.2.4 代码实践

**目标**：观察 `declare_to_let` 如何把 `DeclareStmt` 变成 `LetStmt`。

**步骤**：

1. 沿用 4.1.4 的 dump 设置。
2. 打开 `ir/0_Original.txt`，搜索形如 `offset_m: int32 = ...` 的声明（在 naive matmul 里，`offset_m`/`offset_n` 就是只赋值一次的局部变量）。
3. 对比 `ir/1_DeclareToLet.txt` 中同一段。

**需要观察的现象**：原本平铺的 `DeclareStmt` + 后续语句，被包进了 `let offset_m: int32 = ... in <后续语句>` 的结构。

**预期结果**：`1_DeclareToLet.txt` 里 `let ... in ...` 嵌套明显增多，平铺的 `declare/assign` 减少。具体形态「待本地验证」（不同内核的变量结构不同）。

#### 4.2.5 小练习与答案

**练习 1**：如果一个变量先声明、然后在 `if` 分支里被赋值两次，`declare_to_let` 会改写它吗？
**答案**：不会。该变量的「赋值/取地址」计数会大于 1（声明带初值算 1 次，每次 `AssignStmt` 再 +1），不满足「恰好等于 1」的条件，所以保持 `DeclareStmt` 不变。这正是 `LetStmt`「不可变绑定」语义的要求。

**练习 2**：为什么 `lower_param_only_expr` 必须在 `declare_to_let` 之后？
**答案**：它的算法靠 `LetStmt` 的 `bind_var → bind_value` 映射来展开中间变量；如果中间变量还是 `DeclareStmt`（可被多次赋值），就没有唯一的绑定表达式可供展开。`declare_to_let` 先把只赋值一次的声明转成 `LetStmt`，才让这个展开算法可行（docstring 也明确说明了这一点）。

---

### 4.3 标量分析支撑：analyze_scalar 与 ScalarSet 抽象

#### 4.3.1 概念说明

编译器优化常常需要回答「这个整数变量可能取什么值」——比如「循环次数 `extent` 是不是 0 或 1」「`offset_m` 能被 64 整除吗」。Tilus 用一个叫 **`ScalarSet`** 的抽象域来近似每个整数变量的取值集合，再用不动点迭代求出全函数的解，最终把结果打包成 `Analysis` 挂到 `metadata.analysis` 上，供下游的界感知化简消费。

`ScalarSet` 用三个量刻画一个整数集合：

- `divisibility`（整除性）：集合里每个数都能被它整除；
- `lower_bound` / `upper_bound`（上下界）：可为 `None` 表示无界。

例如 `ScalarSet(divisibility=2, lower_bound=0, upper_bound=10)` 表示 \(\{0,2,4,6,8,10\}\)。

#### 4.3.2 核心流程

`AnalyzeScalarPass` 只是个薄壳，真正干活的是 `analyze_scalar`：

1. **初始化种子**：函数参数若在 `param2divisibility` 里（由 `lower_assume` 写入），则赋予相应整除性与下界 0；`blockIdx` 各维根据 `grid_blocks` 是否常量赋予 `[0, grid-1]` 或 `[0, +∞)`。
2. **收集定义点**：把所有 `DeclareStmt`/`LetStmt`/`ForStmt`/`AssignStmt`（整数类型）收进一个列表。
3. **不动点迭代**：反复用 `ScalarSet` 的代数运算（`+ - * // %`、并集 `|`）更新每个变量的集合，直到不再变化：

   \[
   \text{set}[v] \;\leftarrow\; \text{set}[v] \;\cup\; \text{ScalarSet}(\text{rhs}(v))
   \]

   对自引用变量（如 `v = v + 1`）会无法收敛，因此设置了 `UPDATE_COUNT_LIMIT = 10`：某个方向的界更新超过 10 次就置为 `None`（表示该方向无界），继续迭代。
4. **打包结果**：把整除性、上下界三个字典装进 `Analysis.create(...)`，写回 `metadata.analysis`。

下游 `bound_aware_simplify` 会在自己入口处**再调一次** `analyze_scalar`（见 4.5），所以标量分析 Pass 本身更像「把分析结果显式落到 metadata，供需要的人随时取用」。

#### 4.3.3 源码精读

Pass 入口极简：

[scalar_analyze.py:20-23](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/scalar_analyze.py#L20-L23) —— 直接委托 `analyze_scalar(function)`。

`ScalarSet` 的语义与示例在它的 docstring 里：

[ir/analyzers/scalar_analyzer.py:78-101](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/analyzers/scalar_analyzer.py#L78-L101)

不动点主循环（含 `UPDATE_COUNT_LIMIT` 防发散机制）：

[ir/analyzers/scalar_analyzer.py:494-544](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/analyzers/scalar_analyzer.py#L494-L544) —— 注意 `lower_count`/`upper_count` 分别记录某变量下界/上界「变小/变大」的次数，超限就把对应界置 `None`。

最终打包成 `Analysis`：

[ir/analyzers/scalar_analyzer.py:557-561](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/analyzers/scalar_analyzer.py#L557-L561) —— 只记录「有意义」的项（整除性≠1、界不为 `None`）。

整个函数的全貌（含种子初始化与定义点收集）：

[ir/analyzers/scalar_analyzer.py:428-564](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/analyzers/scalar_analyzer.py#L428-L564)

#### 4.3.4 代码实践

**目标**：理解 `ScalarSet` 的代数运算，亲手算一个例子。

**步骤**（源码阅读型实践，不需要 GPU）：

1. 打开 [ir/analyzers/scalar_analyzer.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/analyzers/scalar_analyzer.py)，阅读 `ScalarSet.__add__`（L176-190）与 `__floordiv__`（L242-265）。
2. 手算：设 `a = ScalarSet(divisibility=16, lower_bound=0)`（即参数 a 是 16 的非负倍数），`b = ScalarSet(divisibility=1, lower_bound=0, upper_bound=63)`（循环变量 b ∈ [0,63]）。求 `a + b` 与 `(a + b) // 16`。

**需要观察的现象**：`a + b` 的整除性会「退化」为 `gcd(16, 1) = 1`（因为 b 不保证整除 16），但 `(a + b) // 16` 经过整除运算后整除性又如何变化。

**预期结果**：`a + b = ScalarSet(divisibility=1, lower_bound=0, upper_bound=None)`（整除性丢失）。这正说明：**为什么仅靠 `assume(a % 16 == 0)` 还不够，编译器还需要 b 的上下界**才能把 `(a+b)//16` 这类地址表达式化简到可控范围。完整结果「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ScalarSet` 用「整除性 + 上下界」三元组，而不是直接枚举可能的整数？
**答案**：因为很多变量（如维度参数、循环变量）的取值范围很大甚至无界，枚举不可行。三元组是一个**抽象域**：它用常数空间刻画一个（可能无穷的）整数集合，并能对 `+ - * // %` 给出保守但可计算的近似（集合运算的「最小上界」）。这是经典的抽象解释（abstract interpretation）思路。

**练习 2**：`UPDATE_COUNT_LIMIT = 10` 解决什么问题？
**答案**：解决自引用变量（如 `v = v + 1`）导致不动点不收敛的问题。当某变量的下界/上界朝某方向持续变化超过 10 次，就把该方向的界置为 `None`（即「放弃追踪这个方向、视为无界」），让迭代继续往下走直到收敛，避免死循环。

---

### 4.4 布局推理的两阶段：为何 lower_load_store 前后各跑一次

#### 4.4.1 概念说明

这是本讲最关键的设计点。流水线里 `layout_inference` 出现了两次，中间夹着一个 `lower_load_store`：

```text
7.  layout_inference   ← 第一次
8.  lower_load_store
9.  layout_inference   ← 第二次
```

要理解为什么，先看 `lower_load_store` 做了什么：它把**高层**的访存指令（`LoadGlobalInst` / `StoreGlobalInst` / `CopyAsyncInst`）降级成**generic** 形式（`LoadGlobalGenericInst` / `StoreGlobalGenericInst` / `CopyAsyncGenericInst`）。generic 形式不再用「offsets + dims」的高层语义，而是显式给出「每线程如何计算字节地址」的 `f_offset` 回调与越界掩码 `f_mask`，更贴近后端代码生成。

而关键耦合在于：**降级时会读取寄存器张量已绑定的布局**。`lower_load_store` 在生成 generic load 时，要把原寄存器张量的 `layout` 原样传给新的 generic 寄存器张量。如果此时布局还没推理（`optional_layout` 为 `None`），访问 `.layout` 会直接抛 `ValueError`（u4-l1 建立的三态协议）。所以：

- **第一次 `layout_inference` 是 `lower_load_store` 的前置依赖**——必须先把高层张量的布局补全，降级才能读到 `.layout`。
- **`lower_load_store` 会新建一批寄存器/共享张量**（generic 指令的输出），且整棵 IR 的访存结构变了；**第二次 `layout_inference` 负责补全新张量的布局、并对降级后的整张 IR 重新做相容性校验**。

#### 4.4.2 核心流程

```text
高层 IR（含 LoadGlobalInst 等，寄存器张量 layout 多为 None）
        │
        ▼  ① layout_inference（第一次）
高层 IR（张量布局已补全，LoadGlobalInst.register_output.layout 有值）
        │
        ▼  ② lower_load_store
              把 LoadGlobalInst → LoadGlobalGenericInst
              读取并沿用 register_output.layout；新建 generic 寄存器张量
              用 global_layout 构造 f_offset / f_mask
        │
        ▼  ③ layout_inference（第二次）
              补全降级产生的新张量布局；verify_layouts 校验全程序相容
        │
        ▼  交给 bound_aware_simplify / DCE / 后端代码生成
```

#### 4.4.3 源码精读

`lower_load_store` 处理 `LoadGlobalInst` 时，**显式读取 `register_tensor.layout`** 并传给 generic 指令：

[lower_load_store.py:53-71](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/lower_load_store.py#L53-L71) —— 第 66 行 `layout=register_tensor.layout` 就是耦合点：若此时 `register_tensor` 的布局未绑定，这里会抛 `ValueError`。

地址计算回调 `f_offset` 与掩码 `f_mask` 由 `get_funcs` 用 `GlobalLayout` 构造（`layout(*global_indices)` 即把逻辑索引喂给全局布局得到字节偏移）：

[lower_load_store.py:31-51](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/lower_load_store.py#L31-L51)

`LayoutInferencePass` 本身（u4-l5 已详述）的三步流程在此处复用：

[layout_inference.py:69-100](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/layout_inference.py#L69-L100) —— `process_function` 里依次：`ApplyLayoutAnnotationRewriter`（应用用户硬种子）→ `infer_layout`（不动点求解）→ `verify_layouts`（校验，失败抛 `LayoutInferenceError`）。两次调用走的是**完全相同**的代码，只是输入 IR 不同。

#### 4.4.4 代码实践

**目标**：对比两次布局推理前后的张量布局，验证「第一次补全高层张量、第二次补全降级后新张量」。

**步骤**：

1. 沿用 4.1.4 的 dump 设置，编译 naive matmul。
2. 打开 `ir/6_LowerPrintTMemoryTensor.txt`（即第一次 layout_inference **之前**的 IR），观察 `register_tensor` 声明里**是否带布局**。
3. 打开 `ir/7_LayoutInference.txt`（第一次之后），观察同样张量是否被补上了 `RegisterLayout`。
4. 打开 `ir/8_LowerLoadStore.txt`，搜索 `load_global_generic` / `store_global_generic`，确认高层 load/store 已被替换。
5. 打开 `ir/9_LayoutInference.txt`（第二次之后），确认所有张量（含 generic 指令新建的）都有布局。

**需要观察的现象**：

- 第 6 步文件里，`register_tensor` 声明多半**没有**布局注解（`optional_layout=None`）；第 7 步文件里它们被填上了布局。
- 第 8 步文件里出现 `LoadGlobalGenericInst`/`StoreGlobalGenericInst`，它们引用的张量在第 9 步里都带上了布局。

**预期结果**：第一次 `layout_inference` 让高层张量从「无布局」变为「有布局」；`lower_load_store` 依赖这些布局生成 generic 指令；第二次 `layout_inference` 确保降级后的整张 IR 仍然「人人有布局且相容」。具体布局文本「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：如果把流水线里**第一次** `layout_inference` 删掉，直接跑 `lower_load_store`，会发生什么？
**答案**：`lower_load_store` 在 `visit_LoadGlobalInst` 里会访问 `register_tensor.layout`，而此时该寄存器张量的 `optional_layout` 还是 `None`，于是抛出 `ValueError`（u4-l1 的三态协议：未绑定时 `.layout` 报错）。所以第一次布局推理是 `lower_load_store` 的硬前置。

**练习 2**：第二次 `layout_inference` 在一个「没有共享内存、只有简单 load/store」的 naive matmul 上，可能几乎是空跑，为什么还要保留？
**答案**：两个原因。其一，`lower_load_store` 降级时会新建寄存器/共享张量，在更复杂的内核（带 async copy、共享内存分块）里这些新张量确实需要布局推理；naive matmul 只是恰好沿用原布局。其二，降级后 IR 结构改变，必须重新 `verify_layouts` 以保证整张程序布局相容——这一步无论如何都要做。保留第二次调用让流水线对任意内核都正确，而不是只对简单内核侥幸通过。

---

### 4.5 收尾：bound_aware_simplify 与 dead_code_elimination

#### 4.5.1 概念说明

布局与访存都处理完之后，IR 还可以做两件事：**化简**和**清扫**。

- **bound_aware_simplify**：利用标量分析给出的整除性与上下界，化简表达式与控制流。典型收益：把 `extent` 为 0 的 `ForStmt` 直接删成空语句、`extent` 为 1 的循环展开成单次执行、常量条件的 `IfStmt` 折叠、`a <= b` 这类比较在界已知时折叠成 `True/False`。
- **dead_code_elimination**：删除「产出无人消费」的功能指令（u3-l4 的白名单判定），以及对副作用指令但输出可选的情况（如原子指令的返回值未被用时，把 `output` 改写为 `None`，让代码生成跳过目的寄存器）。

为什么这之间还要**再跑一次 `analyze_scalar`**（第 11 步）？因为 `bound_aware_simplify` 会改变 IR（删循环、代常量），之前第 5 步算出的 `Analysis` 已经过时；而下游代码生成还会读 `metadata.analysis`，所以需要刷新一次。

#### 4.5.2 核心流程

`bound_aware_simplify` 在自己入口处**主动重算一次标量分析**，再用结果驱动两类化简器（`RuleBasedSimplifier` 基于 hidet 的界分析、`ScalarSetBasedSimplifier` 基于 `ScalarSet`）：

```text
visit_Function:
  func = analyze_scalar(func)          # 主动刷新，不依赖第 5 步
  把 analysis 的 divisibility/上下界灌进两个化简器的初始状态
  遍历 IR：
    ForStmt: 若 extent 的界为 0 → 删空；为 1 → 展开单次
    IfStmt:  若 cond 折成常量 → 只保留存活分支
    Expr:    用两个化简器化简（折叠比较、代常量）
```

`dead_code_elimination` 则分两趟：先 `UsedTensorCollector` + 不动点传播算出「哪些张量被用到」，再 `DeadCodeEliminator` 删除产出未被使用的功能指令、改写副作用指令的可选输出。

#### 4.5.3 源码精读

`bound_aware_simplify` 在入口主动重算标量分析：

[bound_aware_simplify.py:79-96](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/bound_aware_simplify.py#L79-L96) —— 第 80 行 `func = analyze_scalar(func)`，然后把 `analysis.lower_bound/upper_bound/divisibility` 灌进 `BoundInfo` 与 `ScalarSet` 两个分析器。

利用界消除 0/1 次循环：

[bound_aware_simplify.py:124-137](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/bound_aware_simplify.py#L124-L137) —— `bound.value == 0` 返回空 `SeqStmt`；`bound.value == 1` 把循环变量代成常量 0 并只访问循环体一次。

DCE 的功能指令白名单（u3-l4 已建立概念，u5-l3 会深入）：

[dead_code_elimination.py:76-113](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/dead_code_elimination.py#L76-L113)

DCE 的删除动作（返回 `None` 即塌缩 `InstStmt`；副作用可选输出改 `output=None`）：

[dead_code_elimination.py:244-254](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/dead_code_elimination.py#L244-L254)

#### 4.5.4 代码实践

**目标**：观察 `bound_aware_simplify` 如何吃掉「已知次数」的循环。

**步骤**：

1. 沿用 dump 设置。如果你在 naive matmul 的 `__call__` 里加一句 `self.assume(k_size % self.block_k == 0)`（让 `cdiv(k_size, block_k)` 的循环次数可被精确分析），重新编译。
2. 对比 `ir/9_LayoutInference.txt`（化简前）与 `ir/10_BoundAwareSimplify.txt`（化简后）里的 `for` 循环。

**需要观察的现象**：在合适的小尺寸下（例如 `k_size` 恰好让循环次数为 1），`for` 循环可能被展开成单次执行；某些 `if`/比较表达式可能被折叠。

**预期结果**：`10_BoundAwareSimplify.txt` 相比前一步，控制流更少、表达式更短。能否看到明显的循环消除「待本地验证」（取决于具体尺寸与 `assume` 是否给出足够强的界）。

> 注意：本实践需要修改 `examples/matmul/matmul_v0.py` 来加 `assume`。本讲义禁止修改源码，因此请在**自己拷贝的副本**上做这个实验，不要改动仓库内的原始 example。

#### 4.5.5 小练习与答案

**练习 1**：`bound_aware_simplify` 自己在入口就调了 `analyze_scalar`，那流水线第 5 步和第 11 步的 `analyze_scalar` 还有意义吗？
**答案**：有。第 5 步为它**之前**可能读 `metadata.analysis` 的环节提供分析（也保证 metadata 一致）；第 11 步是为它**之后**的消费方（代码生成等）提供一份反映「化简后 IR」的刷新分析。`bound_aware_simplify` 自己重算只是为了拿到「针对当前 IR 的最新分析」来驱动自身的化简，二者并不矛盾——它不信任上游留下的旧分析，但会把化简后的新分析留给下游。

**练习 2**：DCE 为什么不能删除 `StoreGlobalInst`（向显存写）这类指令，即使它的结果没人「读」？
**答案**：因为 `StoreGlobalInst` 是**副作用指令**（不在 `FUNCTIONAL_INST_TYPES` 白名单里）。它的意义在于写内存这个动作本身，而不是产出某个张量。DCE 只删除「功能且产出无人用」的指令；副作用指令一律保留。这正是 u3-l4 强调的「功能/副作用的区分靠白名单，而非看 output 是否为 None」。

---

## 5. 综合实践

把本讲的三条主线（顺序、布局推理时机、标量分析支撑）串起来，完成一次「全流水线导览」。

**任务**：用 `dump_ir` 导出 naive matmul 各 Pass 之后的 IR，回答下面四个问题。

**操作步骤**：

```python
import tilus
tilus.option.cache_dir("/tmp/tilus-full-pipeline")
tilus.option.debug.dump_ir(True)

# 复用 examples/matmul/matmul_v0.py 里的 MatmulV0
import torch, math
from matmul_v0 import MatmulV0   # 若不在 examples 目录，请把该文件加入 sys.path 或就地定义

matmul = MatmulV0()
M = N = K = 128
a = (torch.rand(M, K, dtype=torch.float16).cuda() - 0.5) / math.sqrt(K)
b = (torch.rand(K, N, dtype=torch.float16).cuda() - 0.5) / math.sqrt(K)
c = torch.empty(M, N, dtype=torch.float16).cuda()
matmul(M, N, K, a, b, c)
torch.cuda.synchronize()
```

然后在 `/tmp/tilus-full-pipeline/programs/<哈希>/ir/` 下完成：

1. **顺序**：列出 `0_Original.txt` 到 `12_DeadCodeElimination.txt` 的全部文件名，确认与 4.1.2 的清单一致（尤其 `7_`/`9_` 都是 `LayoutInference`、`5_`/`11_` 都是 `AnalyzeScalar`）。读 `lower_time.txt` 看哪个 Pass 最慢。
2. **前端规范化**：对比 `0_Original.txt` 与 `1_DeclareToLet.txt`，指出哪些 `DeclareStmt` 变成了 `LetStmt`。
3. **布局推理时机**：对比 `7_LayoutInference.txt`、`8_LowerLoadStore.txt`、`9_LayoutInference.txt`。在 `7_` 里确认高层寄存器张量已带布局；在 `8_` 里确认出现了 `*_generic` 访存指令；在 `9_` 里确认所有张量布局相容。
4. **标量分析支撑**：在 `5_AnalyzeScalar.txt` 与 `11_AnalyzeScalar.txt` 里找 `metadata` 的 `analysis` 字段（若打印出来），对比两次的 `divisibility`/`lower_bound`/`upper_bound` 是否因 `bound_aware_simplify` 改变了 IR 而不同。

**预期结果**：你应当能得到一张「12 步 × 每步职责 × 该步 IR 变化」的对照表，把本讲的所有结论落到具体文件上。各文件具体内容「待本地验证」。

> 若没有 GPU，可只触发 JIT 编译（调用一次内核即可生成 IR 文件），不必真正跑性能 benchmark。

---

## 6. 本讲小结

- `get_default_passes()` 返回 12 个 Pass，是高层（Tilus IR）优化的唯一清单，由 `drivers.optimize_program` 经 `apply_transforms` 顺序执行，可被 `debug.dump_ir` 逐 Pass 落盘。
- 流水线分四段：前端规范化（declare_to_let/let_propagation）→ 提示落地与表达式规范化（lower_assume/lower_param_only_expr/analyze_scalar/lower_print_tmemory_tensor）→ 布局与访存（layout_inference/lower_load_store/layout_inference）→ 收尾（bound_aware_simplify/analyze_scalar/dead_code_elimination）。
- **布局推理跑两次**的根本原因：`lower_load_store` 降级时要读取 `register_tensor.layout`，所以第一次推理是它的硬前置；降级又新建了 generic 张量并改变了 IR 结构，所以第二次推理负责补全与重新校验。
- **标量分析**用 `ScalarSet`（整除性 + 上下界）抽象域做不动点迭代，产出 `Analysis` 挂到 metadata；它支撑 `bound_aware_simplify` 做界感知化简，并在化简后再跑一次以刷新结果。
- `declare_to_let` 是 `lower_param_only_expr` 的前置（后者依赖 `LetStmt` 绑定来展开表达式）；`lower_assume` 把 `a % c == 0` 转成 `param2divisibility`，是标量分析整除性信息的来源之一。

## 7. 下一步学习建议

- **深入单个变换**：u5-l3 会专门拆解 `dead_code_elimination` 的活跃性分析与 `scalar_analyze` 的 `ScalarSet` 代数；u5-l4 会讲 `lower_load_store` 等 lowering 类 Pass。
- **布局推理细节**：若对两次 `layout_inference` 内部如何求解仍想深挖，回到 u4-l5，重点读 `infer_layout` 的优先级排序与 `LoadGlobalRule` 如何凭访存模式反推布局。
- **后端衔接**：本讲的产物（规整的 Tilus IR）接下来由 `generate_ir_module`（u6-l1）翻译成 Hidet IR 再代码生成。建议接着读 u6-l1，看「布局都已绑定的 IR」如何被 `FunctionCodegen` 消费。
- **动手实验**：在本地拷贝上尝试增删 `get_default_passes()` 里的某个 Pass，用 `dump_ir` 观察对最终 `source.cu` 的影响——这是理解每个 Pass 价值的最快方式。
