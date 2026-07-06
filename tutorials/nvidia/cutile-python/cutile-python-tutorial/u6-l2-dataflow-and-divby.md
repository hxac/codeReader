# 数据流分析与整除性传播

## 1. 本讲目标

本讲承接 [u6-l1（Pass 流水线总览与死代码消除）](./u6-l1-pass-pipeline-and-dce.md)，深入优化流水线中最关键的一段「数据分析 + 信息注入」：**数据流分析（dataflow analysis）** 与 **整除性传播（divisibility propagation）**。

学完本讲你应该能够：

- 说清 cuTile 为什么需要在编译期知道「某个指针 / 某个形状值是否能被某个数整除」；
- 读懂 `dataflow_analysis` 如何用「别名集 + 整除性」两个维度做不动点分析，并把每条 IR 操作当作一条传播规则；
- 解释 `TupleConstraint` 是如何被 `_register_tuple_params` 递归拆解、静态形状 `shape_constant` 是如何被折叠成整除性事实的；
- 理解 `add_divby_pass` 如何挑选候选变量、提取「最大的 2 的幂因子」并落地为 `AssumeDivBy` 操作；
- 区分「自动注入」与「用户手动 `ct.assume_divisible_by` 注入」两条路径，以及后者对已知常量做编译期校验的行为。

---

## 2. 前置知识

在进入源码前，先用通俗语言建立三个直觉。

### 2.1 为什么要告诉编译器「这个地址对齐」

GPU 上的访存指令对**对齐（alignment）**非常敏感。一个基地址若对齐到 16 字节，硬件往往能用更宽的向量访存（甚至 TMA）一次搬一整块数据；若编译器无法确认对齐，就只能保守地拆成更窄、更慢的访问。

问题是：cuTile 的数组基地址、shape、stride 在 IR 里只是普通的 SSA 变量（运行时值），编译器默认「什么都不知道」。数据流分析的任务，就是把分散在各处的线索（约束里的 `base_addr_divisible_by`、静态形状、用户断言、运算的代数性质）**汇拢**起来，给每个变量打上一个「它至少能被谁整除」的标签；`add_divby_pass` 再把这些标签**物化**为显式的 `AssumeDivBy` 操作，交给后端 `tileiras` 去利用。

### 2.2 不动点与格（lattice）的直觉

数据流分析本质是一个**单调增长**的过程：每个变量的信息只会「越来越确定」，绝不会缩水。我们定义一个二元运算 `unify`（合并两条信息），它满足：

- 幂等：`unify(a, a) == a`
- 交换、结合

于是反复扫描整棵 IR 树，只要某次扫描后**没有任何变量的信息发生变化**（即 `dirty == False`），就称达到了**不动点（fixpoint）**，分析结束。因为信息只增不减且取值有限，该过程必然终止。本讲不展开抽象解释理论，只需记住：「反复传播直到稳定」。

### 2.3 整除性的代数传播规则

整除性有一组很好用的代数性质，数据流分析正是按它们来传递 `div_by`：

- 若 \( x \) 能被 \( a \) 整除、\( y \) 能被 \( b \) 整除，则 \( x+y \) 与 \( x-y \) 都能被 \( \gcd(a,b) \) 整除；
- \( x \cdot y \) 能被 \( a \cdot b \) 整除；
- \( |x| \)、\( -x \) 的整除性同 \( x \)；
- 字面量整数常量 \( c \) 可被 \( |c| \) 整除。

其中 \( \gcd \) 用 `math.gcd` 计算。注意这些规则**只对整数类型成立**，浮点不参与整除性传播。

> 阅读本讲前，你应当已经了解 Tile IR 的 `IRContext / Block / Var / Operation`（见 [u5-l5](./u5-l5-ir-core.md)）、Pass 流水线顺序与 DCE（见 [u6-l1](./u6-l1-pass-pipeline-and-dce.md)）、以及参数注解树与 `ParameterConstraint` 体系（见 [u5-l1](./u5-l1-kernel-decorator-and-annotated-function.md) 与 [u3-l7](./u3-l7-tuple-args-and-static-shape.md)）。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/cuda/tile/_passes/dataflow_analysis.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dataflow_analysis.py) | 数据流分析主体：定义 `DataPredicate`（别名集 + 整除性）、参数种子、按操作类型的传播规则、不动点迭代。 |
| [src/cuda/tile/_passes/propagate_divby.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/propagate_divby.py) | `add_divby_pass`：扫描候选变量、提取 2 的幂因子、插入 `AssumeDivBy` 并重映射操作数。 |
| [src/cuda/tile/_ir/ops.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ops.py) | `AssumeDivBy` 操作定义、`assume_div_by` 助手、用户接口 `assume_divisible_by` 的实现、候选操作 `MakeTensorView` 等。 |
| [src/cuda/tile/compilation/_signature.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_signature.py) | `ArrayConstraint`（含 `shape_constant` / `base_addr_divisible_by`）、`TupleConstraint` 等约束定义。 |
| [src/cuda/tile/_compile.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py) | `_transform_ir`：把 `dataflow_analysis` 与 `add_divby_pass` 串进 Pass 流水线。 |
| [test/test_propagate_divby.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_propagate_divby.py) | 端到端断言测试，是理解「期望行为」的最佳入口。 |

---

## 4. 核心概念与源码讲解

### 4.1 数据流分析框架：DataPredicate、别名集与不动点迭代

#### 4.1.1 概念说明

`dataflow_analysis`（最小模块 **dataflow_analysis**）做两件事：为 IR 中每个 `Var` 维护一个**数据谓词（DataPredicate）**，再用一组**传播规则**把已知信息沿 def-use 链条流动起来，直到不动点。

一个 `DataPredicate` 只刻画两个维度：

- **`alias_set`**：这个变量（通常是指针）可能与哪些别的变量指向同一块显存。用「别名组」的位集表示。
- **`div_by`**：这个值**至少**能被哪个正整数整除（`1` 表示毫无信息）。
- `may_alias_internally`：同一数组两个不同下标是否可能落到同一地址（例如 stride=0），用来抑制某些 load/store 优化。

注意：`alias_set` 服务于「能不能交换 / 删除某些访存」（与 [u6-l3 token 排序](./u6-l3-token-order-pass.md) 配合），`div_by` 服务于「访存对齐优化」。本讲聚焦 `div_by`，但二者共享同一套传播机制。

#### 4.1.2 核心流程

整个分析可以看作「**种子 + 传播 + 不动点**」三步：

```text
1. 种子：遍历 parameter_constraints，按约束类型为每个扁平参数 Var 写入初始 DataPredicate
        （ArrayConstraint → base_ptr/shape/stride 一串谓词；
         ScalarConstraint → always_true；
         ListConstraint/TupleConstraint → 见 4.2/4.3）
2. 传播：从 root_block 深度遍历每条 Operation，按操作类型套用传播规则，
        把输入 Var 的谓词推导到输出 Var（_Tracker.update 自动 unify 并置 dirty）。
        控制流体（Loop/IfElse/TileReduce/TileScan）递归遍历其 nested_block，
        并在汇合点（continue/break/end_branch）把分支信息流入循环/分支结果 Var。
3. 不动点：只要本轮 dirty，就 reset_dirty 再扫一遍；dirty 不再置位即结束。
```

`_Tracker.update` 是单调格的关键：仅当合并后的新谓词与旧谓词**不同**时才置 `dirty`，保证信息只增不减、循环必定终止。

#### 4.1.3 源码精读

`DataPredicate` 是个冻结 dataclass，核心是 `unify`（格的 join）与 `replace`：

- [src/cuda/tile/_passes/dataflow_analysis.py:L32-L46](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dataflow_analysis.py#L32-L46) —— `unify` 把两个谓词的 `alias_set` 按位或、`div_by` 取 `gcd`、`may_alias_internally` 按位或。这正是「合并两条信息、取最弱公共保证」。

别名集用整数位集表达，两个常量刻画边界：

- [src/cuda/tile/_passes/dataflow_analysis.py:L27-L29](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dataflow_analysis.py#L27-L29) —— `ALIAS_UNIVERSE = -1` 表示「可能与任何东西别名」（最弱/最保守），`ALIAS_EMPTY = 0`。

主入口 `dataflow_analysis` 把种子与迭代串起来：

- [src/cuda/tile/_passes/dataflow_analysis.py:L93-L124](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dataflow_analysis.py#L93-L124) —— 先按约束类型分发到 `_register_leaf_param` / `ListConstraint` 分支 / `_register_tuple_params`（L98-L116）；然后 `while state.dirty` 反复调 `_analyze_aliases_in_block`（L120-L122），最后 `finalize()` 导出每个变量名到谓词的字典。

`_Tracker` 与 `_State` 是单调容器：

- [src/cuda/tile/_passes/dataflow_analysis.py:L201-L224](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dataflow_analysis.py#L201-L224) —— `update` 在旧谓词存在时做 `unify`，**只有发生变化才置 `dirty=True`**（L215-L216 提前返回），这是不动点能停下来的根本。

整数的代数传播规则集中在两个小函数里：

- [src/cuda/tile/_passes/dataflow_analysis.py:L249-L259](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dataflow_analysis.py#L249-L259) —— 二元整运算：`add/sub → gcd(x_div, y_div)`、`mul → x_div * y_div`，非整数返回 `1`。对应 §2.3 的两条公式。
- [src/cuda/tile/_passes/dataflow_analysis.py:L262-L268](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dataflow_analysis.py#L262-L268) —— 一元 `abs/neg` 保持整除性不变。

每条操作的处理在 `_analyze_aliases_in_block` 中以 `if/elif` 链表达（这是「传播规则表」）：

- [src/cuda/tile/_passes/dataflow_analysis.py:L271-L393](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dataflow_analysis.py#L271-L393) —— 例如 `Assign` 直接 propagate（L276-L277）；`RawBinaryArithmeticOperation` 用上面那个函数算新 `div_by`（L336-L340）；`TypedConst` 的整数值把 `div_by` 设为 `abs(value)`（L327-L333）；`Loop` 把 `initial_values` 流入 `body_vars` 与 `result_vars`（L350-L359）并递归体块；末尾 `else` 分支对未知操作把结果设为 `always_true`（L390-L393）。

#### 4.1.4 代码实践

**目标**：直观感受「不动点」为什么要多轮。

1. 阅读 [src/cuda/tile/_passes/dataflow_analysis.py:L120-L122](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dataflow_analysis.py#L120-L122) 的 `while state.dirty` 循环。
2. 在脑海中构造一条「先用后定义」的链：例如一个 `Loop` 的结果变量回喂给下一轮的 `body_vars`，信息要等第二轮扫描才能从 `initial_values` 经循环体流到 `result_vars`，再经 `Continue` 倒灌回 `body_vars`。
3. **观察现象**：单轮扫描不足以让携带值（carried values）拿到稳定谓词，需要至少两轮。
4. **预期结果**：理解为什么必须用「不动点循环」而非「单次遍历」。本实践为源码阅读型，**待本地验证**：可在 `_analyze_aliases_in_block` 入口处加一行计数日志，对带循环的内核统计实际扫描轮数。

#### 4.1.5 小练习与答案

**练习 1**：若 `x.div_by = 6`、`y.div_by = 4`，那么 `x + y` 与 `x * y` 的 `div_by` 分别是多少？

> **答案**：`x+y → gcd(6,4) = 2`；`x*y → 6*4 = 24`。

**练习 2**：为什么 `_Tracker.update` 在「新谓词等于旧谓词」时必须提前返回、不置 `dirty`？

> **答案**：否则 `dirty` 永远为真，不动点循环无法终止。只有「真正发生变化」才需要再扫一轮。

---

### 4.2 参数种子：从 ArrayConstraint 到谓词，静态形状折叠

#### 4.2.1 概念说明

数据流分析的「初始事实」全部来自**参数约束（ParameterConstraint）**。其中信息最丰富的是 `ArrayConstraint`：它一次为一个数组生成 \(1 + 2 \cdot \text{ndim} \) 个扁平谓词——1 个基地址指针、`ndim` 个 shape、`ndim` 个 stride。

最小模块 **shape_constant folding** 描述一个关键优化：当某个维度被 `shape_constant` / `stride_constant` 特化为编译期常量时（即 [u3-l7](./u3-l7-tuple-args-and-static-shape.md) 讲的静态形状特化），它的整除性是「绝对已知」的——一个值为 16 的常量当然能被 16 整除。分析直接把这个常量值当作 `div_by` 折叠进去，让后端也吃到了静态形状的红利，而不只是把它当普通运行时值。

#### 4.2.2 核心流程

`ArrayConstraint` 上有两组容易混淆的字段，二选一地决定每个维度的 `div_by`：

```text
对每个维度 d：
  若 shape_constant[d] 不是 None（静态常量）  → div_by = shape_constant[d]      # 折叠
  否则                                          → div_by = shape_divisible_by[d]  # 仅假设
stride 同理：stride_constant[d] 优先于 stride_divisible_by[d]
基地址指针：div_by = base_addr_divisible_by
```

注意单位：基地址与指针的 `div_by` 是**字节**（地址本身以字节计），而 shape / stride 的 `div_by` 是**数组元素**。二者在 `PointerOffset` 处会按元素位宽换算到同一量纲（见 4.4 的 `PointerOffset` 分支）。

构造期 `_remove_redundant_divisibility_constraints` 还会做一致性校验：静态常量必须能被同名维度的 `*_divisible_by` 整除，否则报错；通过后把冗余的 `*_divisible_by` 置 1。

#### 4.2.3 源码精读

种子生成函数 `_get_array_predicates`：

- [src/cuda/tile/_passes/dataflow_analysis.py:L127-L149](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dataflow_analysis.py#L127-L149) —— L129-L131 是基地址指针谓词（`div_by = base_addr_divisible_by`）；**L135-L140 是静态 shape 折叠**：`if static is not None: div_by = static`，否则用 `shape_divisible_by`；L143-L148 对 stride 做同样处理。shape/stride 谓词的 `alias_set` 设为 `ALIAS_UNIVERSE`（它们不是指针，别名维度无意义）。

约束侧的字段定义与校验：

- [src/cuda/tile/compilation/_signature.py:L69-L92](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_signature.py#L69-L92) —— `shape_constant` 文档明确写「If `shape_constant[i]` is set, `shape_divisible_by[i]` is redundant and must be compatible … it is then ignored」，对应折叠语义。
- [src/cuda/tile/compilation/_signature.py:L498-L508](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_signature.py#L498-L508) —— `_remove_redundant_divisibility_constraints`：常量必须能被对应 `*_divisible_by` 整除（L504-L506 校验），通过后把后者置 `1`（L507）。

一个端到端断言可以把抽象语义落地为可观察事实：

- [test/test_propagate_divby.py:L95-L100](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_propagate_divby.py#L95-L100) —— `base_div=16, stride_div=(8,1), shape_div=(4,1)` 的数组，其 `MakeTensorView` 输入最终拿到 `{'base_ptr': 16, 'shape[0]': 4, 'stride[0]': 8}` 的整除性。这张表就是「种子 → 谓词」最直观的样例。

#### 4.2.4 代码实践

**目标**：用静态形状特化让一个维度从「无信息」变成「绝对整除」。

1. 阅读对照测试 [test/test_propagate_divby.py:L103-L112](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_propagate_divby.py#L103-L112)（`test_static_shape_seed_from_array_arg`）与 [test/test_propagate_divby.py:L115-L122](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_propagate_divby.py#L115-L122)（`test_shape_constant_without_annotation_is_dynamic`）。
2. 前者把 `shape_const=(16, None)` 与注解 `static_shape_dims=(0,)` 配对，断言该维变成了 `is_constant()` 且等于 16。
3. **观察现象**：仅有 `shape_constant` 而无 `static_shape_dims` 注解时，shape 仍是动态的（后者断言 `not … is_constant()`）——说明「约束里的静态值」必须配合「注解」才会真正特化进 IR；但**整除性折叠不依赖注解**，只要 `shape_constant` 非 None 就立刻吃红利。
4. **预期结果**：理解「形状特化（IR 改变）」与「整除性折叠（仅谓词改变）」是两件事，后者门槛更低、总是生效。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：一个 `float16`、`ndim=2` 数组的 `ArrayConstraint` 一共生成几个谓词？分别是什么？

> **答案**：\(1 + 2 \cdot 2 = 5\) 个：1 个 base_ptr、2 个 shape 维、2 个 stride 维。dtype 不影响个数，只影响指针字节换算时的位宽。

**练习 2**：`shape_constant=(12, None)` 且 `shape_divisible_by=(4, 1)`。校验能否通过？通过后该维的 `div_by` 是多少？

> **答案**：`12 % 4 == 0`，校验通过；折叠后该维 `div_by = 12`（取常量值，而非 4），并把 `shape_divisible_by[0]` 置 1。

---

### 4.3 tuple 与 list 参数的递归注册：_register_tuple_params

#### 4.3.1 概念说明

[u3-l7](./u3-l7-tuple-args-and-static-shape.md) 引入了 tuple 参数：一组相关参数被打包成单个 Python `tuple` 传入，在 IR 里被「先拆后装」成一串扁平 Var。最小模块 **_register_tuple_params** 回答：当约束是嵌套的 `TupleConstraint` 时，数据流分析怎么把每个叶子（数组/标量）的谓词正确地映射到这串扁平 Var 上？

答案是**递归遍历约束树**，用一个游标 `offset` 在扁平 Var 列表上推进，每访问一个叶子就消费对应数量的 Var（数组消费 \(1+2\cdot\text{ndim}\) 个、标量消费 1 个、嵌套 tuple 递归、list 消费 2 个）。这棵约束树与 `_create_kernel_parameters` 产出的扁平 Var 序列是同构的——两者按相同顺序展开，所以游标能精确对齐。

#### 4.3.2 核心流程

```text
_register_tuple_params(state, constraint, flat_params, offset, alias_set_mapper):
    for item in constraint.items:
        数组叶子 → 消费 1+2*ndim 个 Var，调 _register_leaf_param
        标量叶子 → 消费 1 个 Var，   调 _register_leaf_param
        嵌套 tuple → 递归 _register_tuple_params（用返回值推进 offset）
        list 叶子 → 消费 2 个 Var（base_ptr + size），base_ptr 走 list 专用谓词
    返回更新后的 offset
```

list 与 tuple 都可能**任意嵌套**（`tuple[list[Array], Array]` 也行），递归处理保证任意深度都能正确对齐。

#### 4.3.3 源码精读

- [src/cuda/tile/_passes/dataflow_analysis.py:L69-L90](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dataflow_analysis.py#L69-L90) —— `for item in constraint.items`（L71）按约束定义顺序遍历；L72-L75 处理数组/标量叶子并推进 `offset += n`；L76-L77 对嵌套 tuple **用返回值推进 offset**（这是递归的关键）；L78-L89 处理 list 叶子：base_ptr 写入别名谓词、把元素谓词挂到 `list_array_tracker`、size_var 设为 `always_true`，最后 `offset += 2`。

入口处分发到该函数的位置：

- [src/cuda/tile/_passes/dataflow_analysis.py:L113-L114](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dataflow_analysis.py#L113-L114) —— `dataflow_analysis` 主循环里，`TupleConstraint` 分支调用 `_register_tuple_params(state, constraint, flat_params, 0, alias_set_mapper)`。

叶子注册函数与 list 的非递归分支（对照理解）：

- [src/cuda/tile/_passes/dataflow_analysis.py:L57-L66](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dataflow_analysis.py#L57-L66) —— `_register_leaf_param`：数组用 `_get_array_predicates` 生成一串谓词并 `zip` 写入对应 Var，标量直接 `always_true`。
- [src/cuda/tile/_passes/dataflow_analysis.py:L101-L112](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dataflow_analysis.py#L101-L112) —— 顶层 `ListConstraint` 分支，逻辑与 tuple 内的 list 分支一致，只是不需要 offset 游标。

#### 4.3.4 代码实践

**目标**：手算一个嵌套 tuple 的扁平 Var 占用，验证游标对齐。

1. 给定约束 `TupleConstraint([ArrayConstraint(ndim=2), TupleConstraint([ScalarConstraint, ArrayConstraint(ndim=1)])])`。
2. 按 `_register_tuple_params` 的规则数扁平 Var 数量：外层第一项是 2 维数组 → \(1+2\cdot2=5\) 个；第二项是嵌套 tuple → 标量 1 个 + 1 维数组 \(1+2\cdot1=3\) 个 = 4 个；合计 **9 个**。
3. **观察现象**：offset 依次为 `0→5`（第一项数组）、`5→6`（嵌套标量）、`6→9`（嵌套 1 维数组），最终返回 9。
4. **预期结果**：每个叶子的谓词都落在了正确的扁平 Var 上。本实践为源码阅读型，**待本地验证**：可写一个最小 tuple 内核用 `compile_tile(..., return_final_ir=True)` dump 出 IR，数入口 Block 的参数个数。

#### 4.3.5 小练习与答案

**练习 1**：为什么嵌套 tuple 分支用的是 `offset = _register_tuple_params(...)`（用返回值赋值），而不是 `offset += 某个固定数`？

> **答案**：嵌套 tuple 内部的元素个数取决于它自己的结构（可能再嵌套），只有递归完成后才知道消费了多少个 Var，所以必须用返回值。

**练习 2**：list 叶子为什么消费恰好 2 个扁平 Var？

> **答案**：list 在 IR 里被表达为「base_ptr（指向元素数组的指针）+ size（int32 长度）」（见 [u5-l6 ListTy](./u5-l6-ir-type-system.md)），所以是 2 个；元素本身的 shape/stride 信息存进 `list_array_tracker` 而非扁平参数。

---

### 4.4 add_divby_pass：把整除性事实落地为 AssumeDivBy 操作

#### 4.4.1 概念说明

数据流分析只是「在 Python 编译器内部记了一本账」，后端 `tileiras` 看不到这本账。最小模块 **add_divby_pass** 的职责是把账本里**对访存真正有用**的整除性事实，物化成显式的 IR 操作 `AssumeDivBy`，让它能进入字节码、被 `tileiras` 看到。

它只关心**访存相关操作的输入**：`MakeTensorView`（普通 `ct.load`/`ct.store` 经它建立数组视图）、`LoadPointer`（`ct.gather`）、`StorePointer`（`ct.scatter`）。对这三个操作的每个输入 Var，若它携带的 `div_by` 含有「2 的幂因子」，就在它被消费前插入一条 `AssumeDivBy`，把消费方看到的那份值替换成「带了对齐假设的新值」。

> 为什么只取 2 的幂？因为 GPU 访存对齐优化只认 2 的幂字节对齐。一个 `div_by = 12`（含因子 4）的指针，有意义的部分是 `4`；`div_by = 7`（无 2 的幂因子）则不插入任何假设。

#### 4.4.2 核心流程

```text
add_divby_pass(root_block, df_result):
  1. _scan_block  → 收集候选：所有 MakeTensorView/LoadPointer/StorePointer 的输入 Var 名
  2. _rewrite_block（递归每个 block）：
       a. 对 block 的每个入口 param，若在候选集中 → 插入 AssumeDivBy
       b. 顺序遍历每条 op：
            - 先用 _remap_operands 把操作数替换成「带假设的版本」
            - 对该 op 的每个候选 result_var → 在其后插入 AssumeDivBy
       c. block[:] = new_ops  原地替换
  3. _add_assume_divby(x):
       divisor = df_result[x].div_by
       power_of_2_d = min(divisor & -divisor, 1024)   # 提取最大的 2 的幂因子，封顶 1024
       if power_of_2_d > 1:
           新建 result_var，发 AssumeDivBy(divisor=power_of_2_d, x=x)
           更新 var_map[x] = result_var，并同步刷新 df_result 里新 Var 的谓词
```

关键技巧 `divisor & -divisor` 是位运算经典手法：它取出一个整数二进制里**最低位的 1**，即「最大的、能整除该数的 2 的幂」。例如 `12 & -12 == 4`、`16 & -16 == 16`、`7 & -7 == 1`。

#### 4.4.3 源码精读

候选集合由这三类操作界定：

- [src/cuda/tile/_passes/propagate_divby.py:L11-L18](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/propagate_divby.py#L11-L18) —— `_OPS_NEED_ASSUME = (MakeTensorView, LoadPointer, StorePointer)`，主入口 `add_divby_pass` 先 `_scan_block` 再 `_rewrite_block`。

扫描与重写：

- [src/cuda/tile/_passes/propagate_divby.py:L21-L28](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/propagate_divby.py#L21-L28) —— `_scan_block` 递归把每个访存操作的全部输入加入候选集（`op.all_inputs()`），含嵌套块。
- [src/cuda/tile/_passes/propagate_divby.py:L31-L47](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/propagate_divby.py#L31-L47) —— `_rewrite_block`：先给 block 入口 param 补假设（L36-L37），再顺序处理每条 op——先重映射操作数（L41），再为候选结果 Var 补假设（L40, L42-L43），最后递归嵌套块（L44-L45）、原地替换 `block[:] = new_ops`（L47）。

提取 2 的幂并发射操作的核心：

- [src/cuda/tile/_passes/propagate_divby.py:L62-L86](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/propagate_divby.py#L62-L86) —— L68 `MAX_DIVBY = 1024` 封顶；L70 `power_of_2_d = min(divisor & -divisor, MAX_DIVBY)`；L71-L78 当其 `> 1` 时新建 `result_var`、构造 `AssumeDivBy` 追加进 `op_list`、登记 `var_map`；L79-L84 同步把新 Var 的谓词写回 `df_result`（让后续 token 排序也能看到）。注意 L66-L67 的去重：已在 `var_map` 里则直接返回，避免对同一变量重复假设。

操作数重映射把「旧 Var」替换为「带假设的新 Var」：

- [src/cuda/tile/_passes/propagate_divby.py:L50-L59](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/propagate_divby.py#L50-L59) —— `_remap_operands` 用 `var_map.get(v.name, v)` 逐个替换，支持单 Var 与 tuple 形式的操作数，最后用 `dataclasses.replace` 生成新 op。

流水线里的调用位置与开关：

- [src/cuda/tile/_compile.py:L100-L106](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L100-L106) —— `dataflow_analysis` 算出 `dataflow_result`（L100）；`if not CUDA_TILE_TESTING_DISABLE_DIV: add_divby_pass(...)`（L102-L103）；紧接着 `token_order_pass` 复用同一份 `dataflow_result`（L106）。注意 divby 与 token 共享分析结果，所以 dataflow 必须先于二者完成。
- [src/cuda/tile/_debug.py:L12-L13](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_debug.py#L12-L13) —— `CUDA_TILE_TESTING_DISABLE_DIV` 环境变量门控整个 pass，是调试/对照实验的开关。

#### 4.4.4 代码实践

**目标**：对照 dump，确认 `add_divby_pass` 为哪些操作注入了 `AssumeDivBy`。

1. 写一个最小内核：`ct.store(x, (0,0), 0)`，用一个 `base_addr_divisible_by=16, stride_divisible_by=(8,1), shape_divisible_by=(4,1)` 的 `ArrayConstraint` 编译（直接照搬 [test/test_propagate_divby.py:L95-L100](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_propagate_divby.py#L95-L100) 的断言）。
2. 用 `compile_tile(..., return_final_ir=True)` 拿到 IR body，遍历 `body.traverse()` 收集所有 `AssumeDivBy`，记录每个的 `divisor` 与对应 `x.name`。
3. 再找所有 `MakeTensorView`，看它的 `base_ptr / shape[i] / stride[i]` 是否都已被重映射成「AssumeDivBy 的结果 Var」。
4. **观察现象**：`base_ptr` 应带 `divisor=16`、`shape[0]` 带 `4`、`stride[0]` 带 `8`，恰好是种子里非 1 的整除性。
5. **预期结果**：与测试断言 `{'base_ptr': 16, 'shape[0]': 4, 'stride[0]': 8}` 一致。**待本地验证**（需要 GPU 与 `tileiras` 环境；无环境时可只读测试断言理解行为）。

#### 4.4.5 小练习与答案

**练习 1**：`div_by = 24` 时，`_add_assume_divby` 插入的 `AssumeDivBy.divisor` 是多少？`div_by = 18` 呢？`div_by = 7` 呢？

> **答案**：`24 & -24 = 8`；`18 & -18 = 2`；`7 & -7 = 1`（`power_of_2_d` 不大于 1，**不插入**任何 AssumeDivBy）。

**练习 2**：为什么 `add_divby_pass` 必须在 `dataflow_analysis` 之后、而又要把结果共享给 `token_order_pass`？

> **答案**：它依赖 dataflow 算出的 `div_by` 谓词；同时它对 `df_result` 的改写（新增 AssumeDivBy 结果 Var 的谓词）和别名信息也要被 token 排序看到，所以三者顺序是「dataflow → add_divby → token_order」，且共用同一份 `dataflow_result`。

---

### 4.5 AssumeDivBy 操作与 assume_divisible_by 的编译期校验

#### 4.5.1 概念说明

最小模块 **AssumeDivBy** 有两条引入路径：

1. **自动**：`add_divby_pass` 从约束与代数传播推导出来，无声插入。
2. **手动**：用户在内核里写 `ct.assume_divisible_by(x, d)`，明确告诉编译器「我保证 `x` 能被 `d` 整除」，常用于编译器推不出来的场景（如 `n = ct.bid(0) + 128; ct.assume_divisible_by(n, 128)`）。

`AssumeDivBy` 在 IR 里是一条独立的 Operation（opcode `assume_div_by`），它**不改变值**，只是给同一个值「贴一张对齐标签」，结果 Var 在后续被当成「已知整除」使用。它最终编进字节码里的 `AssumeOp` + `DivBy` 谓词，交给 `tileiras`。

手动路径多一道**编译期校验**：若被断言的值恰好是个已知整数常量，且该常量并不能被 `d` 整除，编译器立刻报 `TileTypeError`——把「用户撒谎」的 bug 前移到编译期，而不是留作运行时未定义行为。

#### 4.5.2 核心流程

```text
assume_div_by(x, divisor):
    if divisor 为 None/1 或 CUDA_TILE_TESTING_DISABLE_DIV → 直接返回 x（no-op）
    if x.is_constant():
        if x.get_constant() % divisor != 0:
            raise TileTypeError("contradicts a known constant")   # 编译期校验
        return x                                                    # 已知满足，无需发 op
    return add_operation(AssumeDivBy, x=x, divisor=divisor)        # 发射 op

assume_divisible_by_impl(x, divisor):    # 用户接口 ct.assume_divisible_by
    校验 x 是整数标量、divisor 是正整数常量 → 调 assume_div_by
```

#### 4.5.3 源码精读

`AssumeDivBy` Operation 的定义与字节码生成：

- [src/cuda/tile/_ir/ops.py:L612-L621](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ops.py#L612-L621) —— `@dataclass(eq=False) class AssumeDivBy(Operation, opcode="assume_div_by")`，带 `divisor: int = attribute()` 与 `x: Var = operand()`；`generate_bytecode` 调 `bc.encode_AssumeOp(... bc.DivBy(self.divisor))`，把假设写进字节码。

`assume_div_by` 助手（含编译期校验）：

- [src/cuda/tile/_ir/ops.py:L624-L634](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ops.py#L624-L634) —— L625 处理 no-op（`divisor is None or divisor == 1 or CUDA_TILE_TESTING_DISABLE_DIV`）；L627-L633 是**编译期校验**：`x.is_constant()` 时若 `val % divisor != 0` 抛 `TileTypeError`，否则直接返回 `x`（已知满足就不发 op）；L634 才真正 `add_operation(AssumeDivBy, ...)`。

用户接口实现：

- [src/cuda/tile/_ir/ops.py:L637-L647](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ops.py#L637-L647) —— `assume_divisible_by_impl` 校验 `x` 是整数标量（L640-L642）、`divisor` 是正整数常量（L643-L646），再调 `assume_div_by`。
- [src/cuda/tile/_stub.py:L1214-L1240](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_stub.py#L1214-L1240) —— 用户侧 stub `assume_divisible_by`，文档写明「The caller is responsible for the correctness of the claim」，并给出 `n = ct.bid(0) + 128; n = ct.assume_divisible_by(n, 128)` 的范式。

校验行为有专门的测试钉住：

- [test/test_propagate_divby.py:L237-L247](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_propagate_divby.py#L237-L247) —— `test_assume_divisible_by_error_on_contradicting_constant` 与 `…_shape`：对一个已知为常量的值（包括静态形状 `x.shape[0]`）断言一个不整除的除数，期望抛错。

#### 4.5.4 代码实践

**目标**：触发手动路径的编译期校验，观察它如何拦截「矛盾断言」。

1. 写一个内核：`n = ct.assume_divisible_by(x.shape[0], 4)`，其中 `x` 用 `static_shape_dims=(0,)` 特化、并把该维 `shape_constant` 设为 14（不能被 4 整除）。
2. 调 `compile_tile` 编译。
3. **观察现象**：编译期直接抛 `TileTypeError`，信息含「`assume_divisible_by` contradicts a known constant」。
4. **预期结果**：把 `shape_constant` 改成 16 后编译通过，且因为「已知满足」，IR 里**不会**出现多余的 `AssumeDivBy`（被 L633 提前返回）。**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`AssumeDivBy` 操作改变它输入的值吗？它存在的意义是什么？

> **答案**：不改值，只贴「对齐标签」。意义是把整除性这一编译期事实显式化，使其能进入字节码并被后端 `tileiras` 用于更宽的向量化访存。

**练习 2**：为什么对**已知常量**做 `assume_divisible_by` 时，编译器选择「校验后不发 op」，而不是「无条件发 op」？

> **答案**：常量的整除性编译器自己就能算，没必要再发一条 op 占空间；但若用户断言与常量事实矛盾，说明用户写错了，必须立刻报错而非生成错误的内核。

---

## 5. 综合实践

把「自动种子 + 静态形状折叠 + AssumeDivBy 物化 + 手动断言」串起来，做一次完整的对照实验。

**任务**：实现一个一维 copy 内核 `k(x, out)`，用三种配置分别编译并 dump IR，对比 `AssumeDivBy` 的注入情况。

```python
# 示例代码（仅说明用法，非项目自带示例）
import cuda.tile as ct
from cuda.tile.compilation import (
    ArrayConstraint, KernelSignature, CallingConvention,
)
from cuda.tile._compile import compile_tile
from cuda.tile._ir.ops import AssumeDivBy, MakeTensorView

@ct.kernel
def k(x, out):
    t = ct.load(x, (0,), (16,))
    ct.store(out, (0,), t)

def dump_assumes(constraint):
    sig = KernelSignature([constraint, constraint], CallingConvention.cutile_python_v2())
    [body] = compile_tile(k._pyfunc, [sig], return_final_ir=True, return_cubin=False).final_ir
    assumes = {op.result_var.name: op.divisor
               for op in body.traverse() if isinstance(op, AssumeDivBy)}
    views = [op for op in body.traverse() if isinstance(op, MakeTensorView)]
    return assumes, len(views)

# 配置 A：仅基地址对齐 16 字节
cA = ArrayConstraint(ct.float32, 1, index_dtype=ct.int32,
                     base_addr_divisible_by=16,
                     stride_lower_bound_incl=0, alias_groups=[], may_alias_internally=False)
# 配置 B：在 A 基础上把 stride 设为常量 1（C-contiguous）
cB = ArrayConstraint(ct.float32, 1, index_dtype=ct.int32,
                     base_addr_divisible_by=16, stride_constant=(1,),
                     stride_lower_bound_incl=0, alias_groups=[], may_alias_internally=False)
# 配置 C：在 A 基础上把 shape 特化为常量 16
cC = ArrayConstraint(ct.float32, 1, index_dtype=ct.int32,
                     base_addr_divisible_by=16, shape_constant=(16,),
                     stride_lower_bound_incl=0, alias_groups=[], may_alias_internally=False)
```

请完成：

1. 调用 `dump_assumes` 对三个配置分别打印 `assumes` 字典。
2. **对照 dump 说明**：配置 A 应只为 `base_ptr` 注入 `divisor=16`（stride/shape 的 `div_by=1` 不注入）；配置 B 的 `stride_constant=(1,)` 折叠后 `div_by=1`，应**不**注入（验证 `power_of_2_d` 不大于 1 时不发 op），并思考「stride 常量 1」为什么对对齐没意义；配置 C 应为 `shape[0]` 注入 `divisor=16`（静态形状折叠生效）。
3. 在内核里再加一句手动断言版本：定义 `k2` 在 load 前写 `start = ct.assume_divisible_by(out.shape[0], 16)`（需配合 `static_shape_dims`），对比「自动折叠」与「手动断言」产生的 `AssumeDivBy` 是否一致。
4. 设环境变量 `CUDA_TILE_TESTING_DISABLE_DIV=1` 重新编译配置 C，确认 IR 里**完全没有** `AssumeDivBy`——这验证了 pass 的总开关。

**预期结果**：你能用一张表总结「哪种约束字段 → 哪个 Var 的 `AssumeDivBy` → divisor 取值」，并能解释「2 的幂提取」与「常量矛盾校验」两道关卡。整套实验需要 GPU 与 `tileiras` 环境；无环境时，对照 [test/test_propagate_divby.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_propagate_divby.py) 的断言阅读行为即可，相关现象均**待本地验证**。

---

## 6. 本讲小结

- 数据流分析为每个 IR 变量维护一个 `DataPredicate`（**别名集 + 整除性 `div_by`**），通过 `_Tracker.update` 的单调 `unify` 反复传播直到**不动点**。
- 整除性按代数规则流动：整数加/减取 `gcd`、乘取乘积、`abs/neg` 保持、整数字面量取绝对值；浮点不参与。
- `ArrayConstraint` 一次生成 \(1+2\cdot\text{ndim}\) 个谓词；**静态形状/步长常量被直接折叠为 `div_by`**（`shape_constant folding`），让后端无需特化 IR 也能吃到对齐红利。
- `TupleConstraint` 由 `_register_tuple_params` **递归**展开，用 `offset` 游标在扁平 Var 序列上精确对齐每个叶子；list 消费 2 个 Var。
- `add_divby_pass` 只对访存操作（`MakeTensorView`/`LoadPointer`/`StorePointer`）的输入，取 `div_by` 的**最大 2 的幂因子**（封顶 1024）落地为 `AssumeDivBy`，并重映射操作数。
- 用户也可用 `ct.assume_divisible_by` 手动注入；若被断言的是已知整常量且不整除，编译期直接抛 `TileTypeError`，把错误前移。

---

## 7. 下一步学习建议

- 下一讲 [u6-l3 内存序 Token 排序](./u6-l3-token-order-pass.md) 会复用本讲产出的 `dataflow_result`（尤其 `alias_set`）来为访存定序，建议重点对比二者如何共享同一份分析、各自又消费哪个字段。
- 想更系统地理解「假设类」操作，可继续阅读 [src/cuda/tile/_ir/ops.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ops.py) 中 `AssumeDivBy` 的姐妹操作 `AssumeBounded`（有界性假设），它们的字节码都走 `encode_AssumeOp`。
- 若对「静态形状如何改变 IR 与 JIT 缓存键」感兴趣，可结合 [u3-l7](./u3-l7-tuple-args-and-static-shape.md) 与 [test/test_array.py:L124-L134](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_array.py#L124-L134)（`test_static_shape_standalone_recompile`）继续追踪。
- 想验证本讲行为的读者，建议通读 [test/test_propagate_divby.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_propagate_divby.py)，它是「期望现象」最完整的清单。
