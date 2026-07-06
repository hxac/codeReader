# Pass 流水线总览与死代码消除

## 1. 本讲目标

上一讲（u5-l4）我们看到了 `hir2ir` 如何把「万物皆 Call」的 HIR 翻译成具体的 Tile IR Operation。但 `hir2ir` 产出的 IR 是「忠于源码」的——它会原样保留中间临时值、变量赋值、未被使用的计算，甚至为了忠实翻译而生成的桥接操作。这样的 IR 直接交给后端编字节码会很臃肿，也缺乏后端优化所需的额外信息。

本讲要回答的问题是：**`hir2ir` 产出的「裸 IR」要经过哪些变换（Pass），才会变成最终交给字节码生成器的「干净 IR」？**

学完本讲，你应该能够：

1. 掌握 `_transform_ir` 中各优化 Pass 的**调用顺序**，并能解释每一步为何排在那里（谁依赖谁）。
2. 理解 **死代码消除（DCE, Dead Code Elimination）** 如何用一个「数据流图 + 可达性传播」算法，从「有副作用的操作」出发反向生长出「有用变量集合」，再剪掉其余操作。
3. 理解 DCE 为什么必须区分「有副作用（store、return）」与「无副作用（纯计算）」操作，以及 `MemoryEffect` 在其中扮演的角色。
4. 理解为什么在 DCE 之前要先跑一遍 `eliminate_assign_ops` 来消除赋值操作，否则 DCE 会把「有用的赋值」误判为死代码。

本讲覆盖三个最小模块：`_transform_ir`（Pass 流水线）、`eliminate_assign_ops`（消除赋值）、`dead_code_elimination_pass`（死代码消除）。

## 2. 前置知识

### 2.1 编译流水线的位置（来自 u5-l2）

回顾 [_compile.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py) 中 `_IrKeeper.get_final_ir` 的流程：它先用 `_create_kernel_parameters` 创建参数、用 `hir2ir` 生成函数体 IR，然后调用 `_transform_ir(func_body, ...)` 对 IR 做一串变换，最后才把干净 IR 交给 `generate_bytecode_for_kernel`。本讲聚焦的正是 `_transform_ir` 这一环节。

### 2.2 IR 的核心构件（来自 u5-l5）

- `Operation`：所有 IR 操作的基类，声明式地用 `operand()`/`attribute()`/`nested_block()` 描述输入；每个子类通过 `opcode="..."` 注册助记符，通过 `memory_effect=...` 声明副作用等级。
- `Var`：SSA 风格的值句柄。
- `Block`：指令容器，有 `params`（块入口参数，承载循环/分支的 phi 语义）和 `operations`。
- `Operation.all_inputs()`：返回该操作的所有 operand（输入 Var）。
- `Operation.result_vars`：该操作产出的结果 Var 元组。
- 控制流体（`Loop`/`IfElse`/`Continue`/`Break`/`EndBranch`/`Return`）会带 `nested_block`，形成树状 IR。

### 2.3 副作用与「有用」的判据

一条 IR 操作是否「必须保留」，取决于它是否有**副作用**：写显存（`ct.store`）、终止函数（`return`）这类操作即使结果没人用也不能删。而纯粹的 `tile_load`/算术等没有副作用，只要它们的结果不被任何「必须保留」的操作链路用到，就是死代码。本讲的关键数据结构 `MemoryEffect` 枚举正是用来标注这件事的。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/cuda/tile/_compile.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py) | `_transform_ir` 定义于此（Pass 流水线的总编排），并 import 各 Pass。 |
| [src/cuda/tile/_passes/dce.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dce.py) | `dead_code_elimination_pass` 的完整实现：构建数据流图、传播「有用集合」、剪枝。 |
| [src/cuda/tile/_passes/eliminate_assign_ops.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/eliminate_assign_ops.py) | `eliminate_assign_ops`：在 DCE 之前消除 `Assign` 操作。 |
| [src/cuda/tile/_ir/ir.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py) | `MemoryEffect` 枚举、`Operation.__init_subclass__`（副作用注册）、`Mapper`（变量重映射）。 |
| [src/cuda/tile/_ir/core_ops.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/core_ops.py) | `Assign` 操作定义，以及 `assign`/`store_var` 辅助函数（赋值操作的来源）。 |
| [test/test_dce.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_dce.py) | DCE 的端到端测试，是理解「剪了什么」的最佳现实例子。 |

## 4. 核心概念与源码讲解

### 4.1 `_transform_ir`：优化 Pass 流水线

#### 4.1.1 概念说明

`_transform_ir` 是 cuTile 的**优化流水线编排器**。它接收 `hir2ir` 产出的「裸函数体 IR」，对其施加一串有序变换，输出「干净 IR」。这条流水线不是随便排的：很多 Pass 之间存在**数据依赖**——后面的 Pass 依赖前面 Pass 产出的结构或信息，前面 Pass 又依赖后面 Pass 才能正确工作（如循环不变量外提必须晚于 token 排序）。理解这条顺序，就理解了 cuTile 优化层的骨架。

关键术语：**Pass**（对整棵 IR 树做一次遍历变换的函数）、**副作用**（store/return）、**数据流分析**（别名与整除性不动点分析，其结果被多个 Pass 共享）。

#### 4.1.2 核心流程

`_transform_ir` 的执行顺序如下（箭头表示「先于」）：

```
eliminate_assign_ops   ── 消除 Assign 操作（DCE 的前提）
        │
dead_code_elimination_pass (第 1 次)  ── 剪掉死代码，缩小后续 Pass 的工作量
        │
dataflow_analysis      ── 计算别名集 + 整除性（结果被下面两个 Pass 共享）
        │
add_divby_pass         ── 注入 AssumeDivBy（对齐信息），被 token_order 之前的 DCE 后的 IR 使用
        │
token_order_pass       ── 为内存操作排 token 链（保证 GPU 内存模型正确性）
        │
rewrite_patterns       ── 模式重写（如 FMA 融合）
        │
hoist_loop_invariants  ── 循环不变量外提（必须在 token_order 之后！）
        │
unhoist_partition_views (版本门控 V_13_3 以下)  ── 把外提过头的 MakePartitionView 挪回原位
        │
split_loops            ── 循环分裂
        │
dead_code_elimination_pass (第 2 次)  ── 再剪一次：上面变换可能新造出死代码
```

三个要点：

1. **DCE 跑两次**：开头一次（清理 `hir2ir` 的忠实冗余），结尾一次（清理后续变换制造的冗余）。
2. **`dataflow_analysis` 的结果被 `add_divby_pass` 与 `token_order_pass` 共享**，所以它只算一次、排在两者之前。
3. **`hoist_loop_invariants` 必须在 `token_order_pass` 之后**，否则会把 `load` 错误地外提到循环外。

#### 4.1.3 源码精读

Pass 的 import 集中在 [_compile.py:L45-L64](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L45-L64) —— 这里能一眼看到流水线涉及的全部 Pass 模块。流水线本体定义在 [_compile.py:L95-L120](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac070f064c98286029ac756101b/src/cuda/tile/_compile.py#L95-L120)：

```python
def _transform_ir(func_body, bytecode_version, param_constraints):
    eliminate_assign_ops(func_body)
    dead_code_elimination_pass(func_body)
    dataflow_result = dataflow_analysis(func_body, param_constraints)

    if not CUDA_TILE_TESTING_DISABLE_DIV:
        add_divby_pass(func_body, dataflow_result)

    if not CUDA_TILE_TESTING_DISABLE_TOKEN_ORDER:
        token_order_pass(func_body, dataflow_result)

    rewrite_patterns(func_body)

    # Loop invariant code motion needs to run after the token order pass.
    # Otherwise, it may incorrectly hoist load operations out of the loop.
    hoist_loop_invariants(func_body)

    if bytecode_version < BytecodeVersion.V_13_3:
        unhoist_partition_views(func_body)

    split_loops(func_body)
    dead_code_elimination_pass(func_body)
```

注意两个细节：

- `dataflow_result` 被同时传给 `add_divby_pass` 和 `token_order_pass`——这就是上面说的「分析结果共享」。
- 两个环境开关 `CUDA_TILE_TESTING_DISABLE_DIV` 与 `CUDA_TILE_TESTING_DISABLE_TOKEN_ORDER`（测试用）可关掉对应 Pass；本讲的 DCE 与 `eliminate_assign_ops` 不受开关控制，**始终执行**。

#### 4.1.4 代码实践

**实践目标**：确认 `_transform_ir` 的调用顺序与「DCE 跑两次」的事实。

**操作步骤**（源码阅读型）：

1. 打开 [_compile.py:L95-L120](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L95-L120)。
2. 用笔把 9 个 Pass 调用按出现顺序编号。
3. 圈出 `dataflow_result` 这个变量被传递给了哪两个 Pass。

**需要观察的现象**：

- `dead_code_elimination_pass` 出现两次（第 2 行与倒数第 2 行）。
- `hoist_loop_invariants` 紧跟在 `token_order_pass` 之后，且上方有注释解释原因。

**预期结果**：能复述「eliminate_assign → DCE → dataflow → divby → token_order → rewrite → hoist → unhoist? → split → DCE」这条链，并指出 dataflow 结果被复用。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `dataflow_analysis` 只调用一次，却能给两个不同的 Pass 用？
**参考答案**：因为别名集与整除性是 IR 的**固有属性**，只取决于 IR 结构与参数约束，与后续变换无关；算一次后结果（`DataflowResult`）以对象形式传给 `add_divby_pass` 和 `token_order_pass` 各取所需，避免重复计算。

**练习 2**：如果注释掉开头的 `eliminate_assign_ops(func_body)`，直接跑 DCE，可能会出什么问题？
**参考答案**：`Assign` 是把一个变量「重命名」式地绑定到另一个值上的桥接操作（见 4.2），它本身没有副作用、结果也可能没人直接引用。DCE 可能把一条「其结果被赋值给一个有用变量」的 `Assign` 链路误判为死代码而剪掉，导致后续 Pass 看到的 IR 语义残缺。

---

### 4.2 `eliminate_assign_ops`：先消除赋值操作

#### 4.2.1 概念说明

`Assign` 操作是 IR 层的「变量绑定」：`y = assign(x)` 表示「让名字 `y` 指向值 `x`」。它源自 `hir2ir` 翻译 Python 变量赋值与函数参数绑定的过程——每次写局部变量都会调用 `store_var`，而 `store_var` 内部就是 `append_verbatim(Assign(...))`。

问题在于：`Assign` 是一个**纯粹的别名桥**，它没有计算、没有副作用，却占了一条 IR 指令。如果不消除它，DCE 会因为它「结果没人用」而把它（甚至它依赖的计算）整条剪掉。所以 DCE 之前必须先把 `Assign` 「折叠」掉——把所有对 `Assign` 结果变量的引用，直接替换为它指向的原始值。

关键术语：**Assign（赋值/别名操作）**、**Mapper（变量重映射表）**。

#### 4.2.2 核心流程

```
1. 遍历整棵 IR 树，对每个 Assign(result_var=R, value=V)：
     - 查 V 自己是否也是某个被折叠 Assign 的结果（链式穿透）
     - 在 orig_var 表里记下：R -> 最终原始 Var
     - 在 Mapper 里登记：把 R 重映射到最终原始 Var
     - 把这条 Assign 从其所在 Block 的指令列表里删掉
2. 用 Mapper 对整棵树 clone 一遍：所有 operand 中对 R 的引用都换成最终原始 Var
3. 用克隆结果替换 root_block
```

第 1 步「链式穿透」很关键：若 `c = assign(b)` 而 `b = assign(a)`，则 `c` 应直接映射到 `a` 而非 `b`。代码用 `orig_var.get(op.value.name, op.value)` 实现：取 `value` 在表里登记的最终来源，没有就用 `value` 自身。

#### 4.2.3 源码精读

`Assign` 操作定义在 [_ir/core_ops.py:L561-L573](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/core_ops.py#L561-L573)，只有一个 operand `value`：

```python
@dataclass(eq=False)
class Assign(Operation, opcode="assign"):
    value: Var = operand()
```

它的来源是 `assign` 辅助函数 [_ir/core_ops.py:L588-L590](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac070f064c98286029ac756101b/src/cuda/tile/_ir/core_ops.py#L588-L590)，被 `store_var`（[_ir/core_ops.py:L593](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/core_ops.py#L593)）调用。而 `store_var` 在前端被大量使用：绑定函数参数（[_passes/hir2ir.py:L205-L213](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/hir2ir.py#L205-L213)）、写回循环结果（[_ir/control_flow_ops.py:L247](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py#L247)）等。所以裸 IR 里会散布很多 `Assign`。

消除逻辑在 [_passes/eliminate_assign_ops.py:L9-L26](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/eliminate_assign_ops.py#L9-L26)：

```python
def eliminate_assign_ops(root_block: ir.Block):
    def walk(block):
        new_ops = []
        for op in block:
            if isinstance(op, Assign):
                var = orig_var.get(op.value.name, op.value)   # 链式穿透
                orig_var[op.result_var.name] = var
                mapper.set_var(op.result_var, var)            # 登记重映射
            else:
                for nested_block in op.nested_blocks:
                    walk(nested_block)
                new_ops.append(op)
        block[:] = new_ops

    mapper = ir.Mapper(root_block.ctx, preserve_vars=True)
    orig_var = dict()
    walk(root_block)
    root_block[:] = [op.clone(mapper) for op in root_block]
```

注意末尾的 `op.clone(mapper)`：它用 [Mapper](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L269-L295)（`preserve_vars=True`，即保留原 Var 名字而非生成新名字）把每条非 Assign 操作里对「已折叠变量」的引用，替换为最终原始 Var。`Mapper.set_var` 要求名字不在表中（[ir.py:L293-L295](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L293-L295)），这与 SSA「每个名字只定义一次」吻合——同一条 IR 里 `Assign` 的 `result_var` 不会重复。

#### 4.2.4 代码实践

**实践目标**：理解「裸 IR 里的 Assign」长什么样、消除后变成什么样。

**操作步骤**（源码阅读型）：

1. 设想一个极简内核 `def k(x): a = ct.load(x,(0,),(1,)); ct.store(x,(1,),a)`。
2. 在 `hir2ir` 阶段，函数参数 `x` 的绑定、局部 `a` 的赋值，都会经由 `store_var` 生成 `Assign` 操作。
3. 对照 [eliminate_assign_ops.py:L9-L26](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/eliminate_assign_ops.py#L9-L26) 推演：`Assign(a, <load结果>)` 被折叠后，`store` 操作里对 `a` 的引用会被替换成 `<load结果>` 本身，`Assign` 指令消失。

**需要观察的现象**：消除后，`store` 直接消费 `load` 的结果，中间不再有 `assign` 桥。

**预期结果**：能说出「`Assign` 是 IR 层的变量别名桥，`eliminate_assign_ops` 把它折叠成直接引用，为 DCE 扫清障碍」。运行结果待本地验证（需要 dump 出 `eliminate_assign_ops` 之前的 IR，目前无公开开关，可借助 4.3 的 dump 观察消除后的最终 IR）。

#### 4.2.5 小练习与答案

**练习 1**：`orig_var.get(op.value.name, op.value)` 为什么要 `.get` 而不是 `orig_var[op.value.name]`？
**参考答案**：因为 `op.value` 可能本身就是一个「非 Assign 结果」的原始 Var（如表里没有它的登记），此时 `.get(name, default)` 返回 `op.value` 自身，实现「链式穿透到尽头」。直接索引会在 KeyError 上崩溃。

**练习 2**：`Mapper(preserve_vars=True)` 与默认模式有何不同？为何这里要用它？
**参考答案**：`preserve_vars=True` 时 `clone_var` 不新建 Var，而是把旧名字映射到「目标 Var」并复用之（[ir.py:L278-L285](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L278-L285)）。这里要把 `Assign` 的结果变量**就地替换**成原始值、不引入新名字，所以必须保留模式。

---

### 4.3 `dead_code_elimination_pass`：数据流驱动的死代码消除

#### 4.3.1 概念说明

死代码消除（DCE）的目标是：**删掉所有「计算了但没人用、且没有副作用」的操作**。它的核心是一个经典算法——「从根（有副作用的操作）出发，反向标记所有可达的变量，不可达的就是死代码」。

cuTile 的 DCE 有三个特别之处：

1. **副作用判据来自 `MemoryEffect`**：只有 `STORE`（写显存）和 `Return`（终止函数）算「必须保留」的根；纯 `LOAD` 与算术不是根。
2. **控制流体（Loop/IfElse）也参与数据流图**：它们被赋予伪变量名 `$cf.N`，使得「循环/分支本身是否需要保留」「内部的 break/continue 是否需要保留」也能用同一套可达性算法判定。
3. **携带值（carried values）的精细化剪枝**：循环可能只用到部分携带值，DCE 会按「body 变量或 result 变量是否被用」逐个裁剪循环的 initial/continue/break 值，被裁掉的位置用 `MakeDummy` 占位以保持结构完整。

关键术语：**数据流图（dataflow graph）**、**可达性传播（reachability propagation）**、**伪变量 `$cf`**、**携带值（carried values）**、**MakeDummy（占位操作）**。

#### 4.3.2 核心流程

DCE 分三步（见 [dce.py:L15-L30](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dce.py#L15-L30)）：

```
步骤 1：_build_dataflow_graph   遍历 IR 树，构建
        graph: 每个变量名 -> 它依赖的变量名列表
        used : 初始的「有用变量集合」（所有 must-keep 操作的直接输入）

步骤 2：_find_used_variables     以 used 为种子，沿 graph 反向传播，
        不断把「被有用变量依赖的变量」加入 used，直到不动点。

步骤 3：_prune_block             再遍历 IR 树，删掉「结果变量全部不在 used 里、
        且非 must-keep」的操作；对控制流体做携带值裁剪与 MakeDummy 占位。
```

用集合论的语言描述可达性：设 `Used₀` 为 must-keep 操作的直接输入集合，闭包定义为

\[
\text{Used} = \mu\, S.\ \bigl(\,\text{Used}_0 \cup \{ v \mid \exists u \in S,\ v \in \text{deps}(u) \}\,\bigr)
\]

即反复把「`Used` 中某变量的全部依赖」并入 `Used`，直到不再增长（不动点）。最终 `Used` 之外的变量都是死代码。

**`$cf` 伪变量的四条建边规则**（见 [dce.py:L33-L83](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dce.py#L33-L83) 的注释）：

| 规则 | 含义 |
|------|------|
| `CF_COND` | `IfElse` 依赖其 `cond`；`for` 循环依赖 `start/stop/step`。 |
| `CF_NESTED` | 嵌套操作依赖其父 `Loop`/`IfElse` 的 `$cf`（保证父被保留则子也被保留）。 |
| `CF_DEFINED_VARS` | 循环 body/result 变量依赖该循环；`IfElse` result 变量依赖该 `IfElse`。 |
| `CF_BREAK_CONTINUE` | `Break`/`Continue` 让其最内层循环依赖包含它的最内层控制流体（保证循环保留则其 break/continue 也保留）。 |

#### 4.3.3 源码精读

**主入口与三步**（[dce.py:L15-L30](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dce.py#L15-L30)）：

```python
def dead_code_elimination_pass(root_block: Block) -> None:
    graph: Dict[str, List[str] | Tuple[str, ...]] = dict()
    used: Set[str] = set()
    op_to_cf_name: Dict[Operation, str] = dict()
    _build_dataflow_graph(graph, used, op_to_cf_name, root_block, None, None, None, None)
    _find_used_variables(graph, used)
    _prune_block(root_block, used, op_to_cf_name, loop_mask=(), end_branch_mask=())
```

**副作用判据 `_must_keep`**（[dce.py:L206-L207](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dce.py#L206-L207)）：

```python
def _must_keep(op: Operation) -> bool:
    return op.memory_effect == MemoryEffect.STORE or isinstance(op, Return)
```

这就是「根」的定义——只有写显存（`ct.store`）和返回值。`MemoryEffect` 是一个**有序枚举**（[ir.py:L252-L258](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L252-L258)），`NONE < LOAD < STORE`，每个 Operation 子类在 `__init_subclass__` 时通过 `memory_effect=` 声明（[ir.py:L519-L525](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L519-L525)）。

**普通操作的建图**（[dce.py:L192-L203](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dce.py#L192-L203)，`else` 分支）：

```python
else:
    deps = tuple(v.name for v in op.all_inputs())   # 该操作的所有 operand
    if innermost_cf_name is not None:
        deps += (innermost_cf_name,)                # CF_NESTED：依赖父控制流体
    if _must_keep(op):
        used.update(deps)                           # 根：把它的输入加入种子集合
    for dst_var in op.result_vars:
        graph[dst_var.name] = deps                  # 结果变量依赖这些输入
```

**可达性传播 `_find_used_variables`**（[dce.py:L210-L217](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dce.py#L210-L217)）是一个典型的**工作表（worklist）不动点算法**：从 `used` 出发，对每个有用变量展开它的 `graph` 依赖并入集合，直到队空。

**剪枝 `_prune_block`**（[dce.py:L220-L288](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dce.py#L220-L288)）的核心判定在最末（[dce.py:L286-L287](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dce.py#L286-L287)）：一条普通操作被保留，当且仅当「它的某个 result_var 在 `used` 里」或「它是 must-keep」。

```python
elif any(r.name in used_vars for r in op.result_vars) or _must_keep(op):
    new_ops.append(op)
```

**携带值裁剪与 MakeDummy 占位**：当循环只用到部分携带值时，未被用到的 initial/continue/break 值会被裁掉，但其位置不能留空（会破坏循环的结构不变量）。[_replace_pruned_with_dummy](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dce.py#L358-L369)（[dce.py:L358-L369](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dce.py#L358-L369)）在这些位置插入 `MakeDummy` 操作生成占位 Var：

```python
def _make_dummy_like(v: Var, new_ops: list[Operation]) -> Var:
    dummy_var = v.ctx.make_var_like(v)
    dummy_var.set_type(v.get_type())
    new_ops.append(MakeDummy(result_vars=(dummy_var,), loc=dummy_var.loc))
    return dummy_var
```

[dce.py:L291-L356](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dce.py#L291-L356) 的注释给了两个绝佳的例子：当循环结果变量被用、但 body 变量未被用时，initial 与 continue 值会变 `<pruned>` 并被 `MakeDummy` 替换；反之亦然。这正是 [test/test_dce.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_dce.py) 三个测试用例验证的行为。

#### 4.3.4 代码实践

**实践目标**：写一个含「未使用中间 tile」的内核，dump 出最终 IR，确认死代码已被剪掉；并对照源码说明 DCE 前后的差异。

**操作步骤**：

1. 准备一个内核，其中 `dead` 是一个 load 出来却从不 store 的 tile：

```python
# 示例代码：仅用于 dump IR，不实际启动
import cuda.tile as ct

@ct.kernel
def k(x):
    live = ct.load(x, (0,), (4,))      # 有用：会被 store
    dead = ct.load(x, (0,), (4,))      # 死代码：结果从不被使用
    dead = dead + 1                    # 死代码：依赖 dead
    ct.store(x, (0,), live)
```

2. 用环境变量打开「cuTile IR 日志」，它会打印**经过 `_transform_ir`（含 DCE）后的最终 IR**到 stderr：

   环境变量映射见 [_context.py:L48-L52](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_context.py#L48-L52)（`CUTILEIR` → `log_cutile_ir`）。`log_cutile_ir` 在 `_IrKeeper.get_final_ir` 里于 `_transform_ir` 之后触发打印（[_compile.py:L408-L411](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L408-L411)）。

   ```bash
   CUDA_TILE_LOGS=CUTILEIR python -c "
   import cuda.tile as ct, torch
   # 需构造一次真实 launch 来触发 JIT 编译；此处略去 host 端张量与 launch 代码
   "
   ```

3. （可选，更精确）改用 `compile_tile` 直接拿 final IR，仿照 [test/test_dce.py:L20-L25](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_dce.py#L20-L25) 的 `get_ir` 辅助函数：

```python
# 示例代码
from cuda.tile._compile import compile_tile
from cuda.tile.compilation import ArrayConstraint, KernelSignature
from cuda.tile._cext import CallingConvention
import cuda.tile as ct

def get_ir(func):
    x = ArrayConstraint(dtype=ct.int32, ndim=1, index_dtype=ct.int32,
                        stride_lower_bound_incl=0, alias_groups=(), may_alias_internally=False)
    sig = KernelSignature([x], CallingConvention.cutile_python_v1(), symbol="k")
    [body] = compile_tile(func, [sig], return_final_ir=True, return_cubin=False).final_ir
    return body

print(get_ir(k).to_string())
```

**需要观察的现象**：

- dump 出的最终 IR 里**只有一条 `tile_load`**（对应 `live`）和一条 `tile_store`；`dead` 那条 `tile_load` 与 `dead + 1` 的算术操作**不出现**。
- 对照「DCE 之前」的概念 IR：`hir2ir` 会忠实生成两条 `tile_load` + 一条 `add`，其中第二条 `tile_load` 与 `add` 因结果不在 `used` 集合里而被剪掉。

**预期结果**：能指出 dump 中消失的那条 load/add 正是 DCE 根据 `_must_keep`（仅 `tile_store` 是根）反向传播后判定为不可达而死代码。完整运行需本地 GPU 与已安装的 `tileiras`；若仅想观察 IR 结构，`return_final_ir=True`（步骤 3）不触发 cubin 编译，门槛更低。运行结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：如果一个内核里只有 `ct.load` 而没有任何 `ct.store` 或 `return`，DCE 会把它剪成什么样？
**参考答案**：`used` 的种子集合（`Used₀`）为空，因为没有 must-keep 操作。可达性传播后 `Used` 仍为空，于是 `_prune_block` 会删掉所有 `tile_load`，函数体几乎被清空。这符合语义——一个不写出任何结果、不返回值的内核是纯死代码。

**练习 2**：为什么 `tile_load`（`MemoryEffect.LOAD`）不是 `_must_keep` 的根，而 `tile_store`（`STORE`）是？
**参考答案**：`load` 只是读入数据供后续计算用，若其结果没人用，删掉它不影响程序可观察行为（不改变显存内容、不影响其他 block）；而 `store` 会**写显存**，是程序对外的可观察副作用，删掉会改变结果。`return` 同理（决定内核返回值）。所以根集合只含「改变外部状态」的操作。

**练习 3**：循环的某个携带值被剪掉后，为何要插入 `MakeDummy` 而不是直接缩短循环的参数列表？
**参考答案**：因为「循环结构」要求 initial/body/result 三组值一一对应、且 continue/break 的 values 与 body_vars 一一对应（见 [dce.py:L291-L356](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dce.py#L291-L356) 的两例）。`_prune_block` 已经用 `_select_by_mask` 缩短了「整组被裁」的携带值，但「组内某位置被裁而组未整体裁掉」时，必须用 `MakeDummy` 占位以维持位置对齐，保证字节码生成阶段（u7-l1）能正常编码。

## 5. 综合实践

把本讲三个模块串起来：用一个**带未使用循环变量的内核**，验证「`eliminate_assign_ops` 折叠赋值 → DCE 剪掉死循环变量」的联动效果。

参考 [test/test_dce.py:L28-L39](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_dce.py#L28-L39) 的 `test_unused_loop_var`：

```python
# 示例代码
def kernel(x):
    a = 0                  # 死：循环里改写但从不使用
    t = ct.load(x, (0,), (1,))
    for i in range(10):
        a = a + 1          # 死：a 不被使用
        t = t + 1          # 活：t 最终被 store
    ct.store(x, (1,), t)
```

任务：

1. 用 4.3.4 步骤 3 的 `get_ir` 拿到 final IR 并打印。
2. 找到 IR 里的 `Loop` 操作，检查它的 `body_vars`——预期**只有 `t`**，`a` 已被 DCE 整条剪掉（对照 `test_unused_loop_var` 的断言 `[v.get_original_name() for v in loop.body_vars] == ["t"]`）。
3. 反推 DCE 之前的概念 IR：循环原本有两个携带值 `a` 和 `t`，`eliminate_assign_ops` 把 `a = a + 1` 翻译出的 `Assign` 折叠后，`a` 的结果变量仍不在 `used` 集合（因为 `a` 没有喂给任何 store），于是 `_prune_block` 用 mask `(False, True)` 把 `a` 这条携带值整组裁掉。
4. 写一段话解释：如果**没有先跑 `eliminate_assign_ops`**，`a = a + 1` 的 `Assign` 链会不会让 DCE 误判？为什么？

**预期结果**：能复述「assign 折叠 → 数据流图建边 → 可达性传播 → 携带值裁剪」四步在这个具体内核上的作用，并指出 `a` 是在哪一步被判定为死代码。运行结果待本地验证。

## 6. 本讲小结

- `_transform_ir` 是 cuTile 的优化 Pass 编排器，顺序为：`eliminate_assign_ops → DCE → dataflow_analysis → add_divby → token_order → rewrite → hoist → (unhoist) → split → DCE`；DCE 跑两次，dataflow 结果被 divby 与 token_order 共享。
- DCE 之前必须先 `eliminate_assign_ops`：`Assign` 是 IR 层的变量别名桥，若不先折叠，DCE 会把「结果被赋值给有用变量」的 Assign 链误判为死代码。
- `eliminate_assign_ops` 用一张 `orig_var` 表做链式穿透，再用 `Mapper(preserve_vars=True)` 把所有引用替换为最终原始 Var。
- DCE 是「从副作用根反向传播可达性」的经典算法：根 = `MemoryEffect.STORE` 或 `Return`；用工作表算法求 `Used` 闭包，闭包外的变量即死代码。
- 控制流体（`Loop`/`IfElse`）通过 `$cf.N` 伪变量参与数据流图，靠 `CF_COND/CF_NESTED/CF_DEFINED_VARS/CF_BREAK_CONTINUE` 四条规则保证「父保留则子与 break/continue 也保留」。
- 携带值按「body 或 result 变量是否被用」逐个裁剪，裁掉的位置用 `MakeDummy` 占位以维持循环结构对齐。

## 7. 下一步学习建议

下一讲 **u6-l2「数据流分析与整除性传播」**会深入本讲提到的 `dataflow_analysis`：它如何追踪别名集与整除性谓词、如何把静态形状折叠为整除性事实，以及 `add_divby_pass` 如何据此插入 `AssumeDivBy`。

之后可以按顺序阅读：

- **u6-l3「内存序 Token 排序」**：解释 `token_order_pass` 为何必须排在 `hoist` 之前，以及 token 链如何保证 GPU 内存模型正确性。
- **u6-l4「代码外提、循环分裂与模式重写」**：展开 `hoist_loop_invariants`/`split_loops`/`rewrite_patterns` 的细节。
- 想从更高层回顾这条流水线在编译全程的位置，可重读 **u5-l2「compile_tile 流水线」**。
