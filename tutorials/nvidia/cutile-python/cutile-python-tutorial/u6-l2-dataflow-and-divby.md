# 数据流分析与整除性传播

## 1. 本讲目标

本讲紧接 u6-l1 的 Pass 流水线，深入其中一环：**数据流分析（dataflow analysis）与整除性传播（divisibility propagation）**。学完后你应当能够：

- 说清 cuTile 为什么要在 IR 上跑一遍数据流分析，它分析的两类「事实」分别是什么；
- 看懂 `dataflow_analysis` 如何用一个**不动点循环**把别名集（alias set）与整除性（divisibility）沿 IR 传播，遇到 if/for/while 时如何「取交集」；
- 理解 `TupleConstraint` 如何被递归地拆解成叶子参数注册（`_register_tuple_params`），以及静态形状（`shape_constant`）如何被折叠成整除性事实；
- 解释 `add_divby_pass` 把整除性「落成」一条条 `AssumeDivBy` 操作的时机、对象与规则；
- 掌握 `AssumeDivBy` 这一 IR 操作与用户 API `ct.assume_divisible_by` 的关系，以及为何对已知常量要做校验。

## 2. 前置知识

本讲默认你已经学完：

- **u5-l5**（IR 核心：`IRContext`/`Builder`/`Block`/`Var`/`Operation`）——你会读到 `Var`、`Operation`、`operand()`/`attribute()`/`nested_block()` 字段、`traverse()`、`result_vars` 等概念；
- **u5-l6**（类型系统）——`ArrayTy` 如何展开成 `1 + 2*ndim` 个叶子（base_ptr + 形状 + 步长），以及 `shape` 字段如何用 `None`/`int` 二义性承载静态形状；
- **u6-l1**（Pass 流水线总览与 DCE）——`_transform_ir` 里各 Pass 的执行顺序，尤其是「数据流分析的结果被 `add_divby` 与 `token_order` 共享」这一约束。

几个本讲会用到的术语先对齐：

- **别名（alias）**：两个指针指向同一块显存。别名分析回答「这两个指针会不会指向同一地址」，编译器据此决定能否重排/合并访存。
- **整除性（divisibility）**：一个标量值或地址能否被某整数 \(d\) 整除，即 \(x \bmod d = 0\)。GPU 上对齐的访存（16B/32B/128B）远快于未对齐访存，整除性事实是 TMA、向量化 load 的前提。
- **不动点（fixpoint）**：反复迭代一个分析直到结果不再变化。控制流（循环、分支）会让事实沿多条路径流动，需要反复传播直至收敛。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/cuda/tile/_passes/dataflow_analysis.py` | 数据流分析主体：定义 `DataPredicate`/`DataflowResult`，逐参数播种事实，沿 IR 树传播别名集与整除性，跑不动点循环。包含 `_register_tuple_params`（递归处理 tuple）与 `_get_array_predicates`（静态形状折叠）。 |
| `src/cuda/tile/_passes/propagate_divby.py` | `add_divby_pass`：扫描需要整除性提示的访存操作（`MakeTensorView`/`LoadPointer`/`StorePointer`），在它们前面插入 `AssumeDivBy`，并把后续 operand 重映射到 assume 之后的新值。 |
| `src/cuda/tile/_ir/ops.py` | `AssumeDivBy` 操作定义、`assume_div_by` 辅助函数（含对常量的校验）、`@impl(ct.assume_divisible_by)` 用户 API 实现，以及 `MakeTensorView`/`LoadPointer`/`StorePointer`/`PointerOffset` 等被分析/被改写的操作。 |
| `src/cuda/tile/_compile.py` | `_transform_ir`：把 `dataflow_analysis` 与 `add_divby_pass` 编进优化流水线（`dataflow` 结果同时喂给 `token_order`）。 |
| `src/cuda/tile/compilation/_signature.py` | `ArrayConstraint`（`shape_constant`/`shape_divisible_by`/`base_addr_divisible_by`/`stride_constant` 等字段）、`TupleConstraint`，以及调用约定门控 `_validate_constraint_support`。 |
| `test/test_propagate_divby.py` | 整除性传播的行为测试，是本讲代码实践的直接依据。 |

## 4. 核心概念与源码讲解

按五个最小模块展开：`dataflow_analysis` → `_register_tuple_params` → `shape_constant folding` → `add_divby_pass` → `AssumeDivBy`。

### 4.1 dataflow_analysis：别名集与整除性的不动点分析

#### 4.1.1 概念说明

hir2ir 产出的 IR 是「忠于源码」的：每个变量等于它字面上该等于的值，编译器**不会自动知道** `x.shape[0]` 是 16 的倍数、也不会自动知道两个数组参数是否别名。但下游优化（向量化 load、TMA、循环外提）非常依赖这两类事实。

`dataflow_analysis` 就是在 IR 上做一次**正向数据流分析**，给每个 `Var` 推断出两类谓词：

1. **别名集（alias set）**：用一个整数位掩码表示「这个变量可能与哪些别名组重叠」。全集 `ALIAS_UNIVERSE = -1`（所有位都为 1）表示「可能与任何东西别名」，空集 `ALIAS_EMPTY = 0` 表示「不与任何东西别名」。
2. **整除性（div_by）**：一个正整数 \(d\)，表示「该变量的值是 \(d\) 的倍数」。\(d=1\) 表示「不做任何整除性假设」。

把这两类事实塞进一个不可变 dataclass `DataPredicate`，再用 `DataflowResult`（`var 名 → DataPredicate` 的字典）整体返回。

#### 4.1.2 核心流程

分析分三步：

1. **播种（seeding）**：遍历内核参数约束（`parameter_constraints`），按每个约束的类型（`Array`/`Scalar`/`List`/`Tuple`）把初始谓词写进 tracker。例如 `ArrayConstraint` 会为 base_ptr 播种 `base_addr_divisible_by`，为每个 shape/stride 维度播种各自的整除性（含静态常量折叠，见 4.4）。
2. **传播（propagation）**：递归遍历 IR 树（`_analyze_aliases_in_block`），对每种操作按其语义推算结果变量的谓词。例如：
   - `Assign`（变量别名桥）：结果 = 源；
   - `RawBinaryArithmeticOperation`：整数 `add`/`sub` 的整除性是 \(\gcd(x_{div}, y_{div})\)，`mul` 是 \(x_{div}\cdot y_{div}\)；
   - `PointerOffset`（指针加偏移）：新指针的整除性是 \(\gcd(\text{ptr}_{div}, \text{offset}_{div})\)，其中偏移的整除性还要乘上元素字节宽度；
   - 控制流（`IfElse`/`Loop`/`Continue`/`Break`/`EndBranch`）：沿多条路径汇合时**取并集（unify）**——别名集按位或、整除性取 gcd。
3. **不动点循环**：传播会改写 tracker，改写过的 tracker 标记 `dirty`；只要还 dirty，就再扫一遍整棵 IR，直到收敛。

\[
\text{unify}(p_1, p_2):\quad
\text{alias}=p_1.\text{alias}\,|\,p_2.\text{alias},\quad
\text{div}=\gcd(p_1.\text{div},\,p_2.\text{div})
\]

为什么控制流汇合要用 gcd？因为如果一个变量在 then 分支是 16 的倍数、在 else 分支是 8 的倍数，那汇合后我们**只能保证**它是 \(\gcd(16,8)=8\) 的倍数——这是「两条路径都成立的最强」结论。

#### 4.1.3 源码精读

谓词与结果容器 [`_passes/dataflow_analysis.py:L32-L54`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dataflow_analysis.py#L32-L54) 定义了 `DataPredicate.unify`（按位或 + gcd）、`replace`（用 `dataclasses.replace` 改一个字段）与 `DataflowResult`。

主入口 [`dataflow_analysis`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dataflow_analysis.py#L93-L124) 的不动点循环：

```python
for flat_params, constraint in parameter_constraints:
    if isinstance(constraint, (ArrayConstraint, ScalarConstraint)):
        _register_leaf_param(state, constraint, flat_params, alias_set_mapper)
    elif isinstance(constraint, ListConstraint):
        ...
    elif isinstance(constraint, TupleConstraint):
        _register_tuple_params(state, constraint, flat_params, 0, alias_set_mapper)

_analyze_aliases_in_block(root_block, state, None, None)
while state.dirty:           # 不动点：只要本轮有更新就再扫一遍
    state.reset_dirty()
    _analyze_aliases_in_block(root_block, state, None, None)
```

`dirty` 标记来自 `_Tracker.update`：只有当新谓词与旧谓词**不同**时才置位 [`_passes/dataflow_analysis.py:L209-L218`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dataflow_analysis.py#L209-L218)，这保证了循环必然终止（谓词只会变「更保守」，单调有界）。

逐操作传播的大 switch 在 [`_analyze_aliases_in_block`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dataflow_analysis.py#L271-L393)。几个关键分支：

- `Assign` 直接 propagate（结果 = 源）[L276-L277](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dataflow_analysis.py#L276-L277)；
- `AssumeDivBy` 把 `div_by` **替换**为声明的 divisor（覆盖旧值）[L278-L281](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dataflow_analysis.py#L278-L281)；
- `RawBinaryArithmeticOperation` 与 `Unary` 调 `_get_divisibility_for_binary_op` / `_get_divisibility_for_unary_op` 算新整除性 [L336-L345](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dataflow_analysis.py#L336-L345)；
- 控制流（`Loop`/`Continue`/`Break`/`IfElse`/`EndBranch`）把 init/next/output 谓词 propagate 给 body/result 变量，递归进入子块 [L346-L388](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dataflow_analysis.py#L346-L388)。

二值/一元算术的整除性推导规则在 [`_get_divisibility_for_binary_op`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dataflow_analysis.py#L249-L259)：仅对整数类型计算，`add`/`sub` 取 gcd、`mul` 取乘积；非整数或其它函数一律返回 1（不假设）。

#### 4.1.4 代码实践

阅读 [`test_divby_add`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_propagate_divby.py#L384-L395) 与 [`test_divby_sub`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_propagate_divby.py#L398-L408)：两个数组的第 0 维形状分别设为 8 的倍数与 4 的倍数，内核里 `start = x.shape[0] + y.shape[0]`，分析后 slice 出来的子数组 `base_ptr` 整除性应为 \(\gcd(32\text{B},\, \gcd(8,4)\times 4\text{B})=\gcd(32,16)=16\)。

1. **实践目标**：验证「整数加法的整除性 = gcd」这条传播规则。
2. **操作步骤**：在本机装好 `cuda-tile[tileiras]` 与 torch 后，运行 `pytest test/test_propagate_divby.py::test_divby_add -q`。
3. **需要观察的现象**：测试断言 `get_op_divby(body, MakeTensorView) == [{'base_ptr': 16, 'shape[0]': 4}]`，即切片视图的 base_ptr 收到 16B 对齐提示、shape[0] 收到 4 的整除提示。
4. **预期结果**：测试通过。具体运行结果**待本地验证**（需要 GPU 与已安装的包）。
5. 若无 GPU，可改用 `compile_tile(..., return_final_ir=True)` 拿到 `body` 后在 Python 里遍历 `body.traverse()` 自行打印 `AssumeDivBy` 的 `divisor`，对照断言手工核算。

#### 4.1.5 小练习与答案

**练习 1**：一个 `Var` 在 then 分支是 32 的倍数、在 else 分支是 12 的倍数。汇合后 `div_by` 是多少？`add_divby_pass` 最终会插入的 `AssumeDivBy.divisor` 又是多少？

答案：汇合取 \(\gcd(32,12)=4\)。`add_divby_pass` 只关心 2 的幂（见 4.4），`4 & -4 = 4`，所以插入的 `AssumeDivBy.divisor = 4`（且 `min(4, 1024) = 4`）。

**练习 2**：为什么 `_Tracker.update` 在「新谓词 == 旧谓词」时直接 return 不置 dirty？

答案：这是不动点算法的收敛保证。谓词只往「更保守」方向单调变化（alias 位只增、div_by 只降），且只有变化才需要再传播；一旦全表不再变化，循环自然终止。

---

### 4.2 _register_tuple_params：递归处理 TupleConstraint

#### 4.2.1 概念说明

u3-l7 引入了 tuple 参数：一组相关参数可以打包成一个 Python `tuple` 传入内核，对应 `TupleConstraint`（其 `items` 是若干子约束）。但数据流分析是**按扁平 Var**工作的——一个 tuple 参数在 IR 里已经被 `_create_kernel_parameters`（u5-l2）拆成了一串扁平 `Var`（tuple 里每个数组又拆成 base_ptr + 形状 + 步长）。

于是问题来了：怎么把一个 `TupleConstraint` 的整除性/别名假设，「对齐」到它展开后的那一串扁平 Var 上？答案就是递归函数 `_register_tuple_params`：它沿着 `TupleConstraint.items` 一层一层往下走，遇到叶子（数组/标量）就播种，遇到嵌套 tuple 就递归，遇到 list 就按 list 的扁平布局播种，并用一个共享的 `offset` 游标在扁平 Var 列表里前进。

#### 4.2.2 核心流程

```
_register_tuple_params(constraint, flat_params, offset):
    for item in constraint.items:
        if item 是 Array/Scalar 叶子:
            n = (1 + 2*ndim) if Array else 1
            _register_leaf_param(item, flat_params[offset:offset+n])
            offset += n
        elif item 是 Tuple:
            offset = _register_tuple_params(item, flat_params, offset)   # 递归
        elif item 是 List:
            播种 base_ptr（含 alias_groups / elements_may_alias）
            播种 size_var 为 ALWAYS_TRUE
            offset += 2
    return offset
```

关键点：**每个叶子数组的扁平宽度是 `1 + 2*ndim`**（1 个 base_ptr + ndim 个 shape + ndim 个 stride），与 u5-l6 的 `ArrayTy` 展开规则、以及 `_get_array_predicates` 返回的谓词列表长度严格一致——所以 `zip(vars, predicates, strict=True)` 能一一对应。`offset` 在递归调用之间用返回值串联，保证多层嵌套 tuple 也能正确对齐。

#### 4.2.3 源码精读

[`_register_tuple_params`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dataflow_analysis.py#L69-L90) 的核心片段：

```python
for item in constraint.items:
    if isinstance(item, (ArrayConstraint, ScalarConstraint)):
        n = 1 + 2 * item.ndim if isinstance(item, ArrayConstraint) else 1
        _register_leaf_param(state, item, flat_params[offset:offset + n], alias_set_mapper)
        offset += n
    elif isinstance(item, TupleConstraint):
        offset = _register_tuple_params(state, item, flat_params, offset, alias_set_mapper)
    elif isinstance(item, ListConstraint):
        ...
        offset += 2
```

数组叶子宽度 `1 + 2*ndim` 与 [`_register_leaf_param`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dataflow_analysis.py#L57-L66) 里调用 `_get_array_predicates` 的返回顺序（base_ptr → 形状 → 步长）对齐。

注意 `dataflow_analysis` 主入口 [L113-L114](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dataflow_analysis.py#L113-L114) 在遇到 `TupleConstraint` 时从 `offset=0` 起调用本函数；而非 tuple 的数组/标量/list 走的是与递归版**对称**的扁平逻辑 [L99-L112](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dataflow_analysis.py#L99-L112)——两段代码刻意保持一致，确保 tuple 与非 tuple 的播种行为完全等价。

#### 4.2.4 代码实践

1. **实践目标**：理解「tuple 参数的扁平宽度」与 `offset` 对齐。
2. **操作步骤**：纸上推演。设内核签名是 `def k(a: tuple[ct.Array, ct.Array], s)`，两个数组都是 2D float32（ndim=2）。
3. **需要观察的现象**：这个 tuple 会展开成多少个扁平 Var？
4. **预期结果**：每个 2D 数组 = `1 + 2*2 = 5` 个 Var（base_ptr + 2 shape + 2 stride），两个数组共 10 个；`offset` 从 0 走到 10。第一个数组的 base_ptr 是 `flat_params[0]`，第二个是 `flat_params[5]`。
5. 这只是源码阅读型推演，无需运行；可对照 u5-l6 的 `flatten_block_parameters` 确认。

#### 4.2.5 小练习与答案

**练习**：为什么 tuple 里嵌一个 list，`offset` 只前进 2，而嵌一个 2D 数组却前进 5？

答案：list 在 IR 里被压成 `ptr<int64> + int32 长度` 两个 Var（见 u5-l6 的 `ListTy`），所以 list 本身的存储只占 2 个扁平槽；而数组要表达 base_ptr + ndim 形状 + ndim 步长，2D 时就是 5 个。list 元素的整除性走的是 `list_array_tracker`（`_AggregatePredicate`），与 base_ptr 分开存放。

---

### 4.3 shape_constant folding：把静态形状折叠成整除性事实

#### 4.3.1 概念说明

u3-l7 引入了静态形状特化：用 `ArrayAnnotation(static_shape_dims=(0,))` 把数组某一维声明为编译期常量（对应 `ArrayConstraint.shape_constant`）。这带来两个独立的好处：

1. **特化 cubin**：那一维从运行时参数变成编译期字面量，不同取值编译出不同 cubin（代价是可能编译爆炸）。
2. **额外的整除性事实**：如果第 0 维静态等于 16，那它当然是 16 的倍数——编译器可以据此推断访存对齐。

`dataflow_analysis` 的 `_get_array_predicates` 专门负责把第 2 点做出来：在为每个 shape/stride 维度生成谓词时，**如果该维度有静态常量值，就把 `div_by` 直接设成那个常量值**，从而把静态形状「折叠」成一条整除性事实，让后续优化白捡一条对齐信息。同理 `stride_constant`（已知步长，如 C-contiguous 末维步长=1）也会被折叠。

#### 4.3.2 核心流程

`_get_array_predicates(constraint)` 返回一个谓词列表，顺序与扁平 Var 一致：

1. **base_ptr 谓词**：`div_by = constraint.base_addr_divisible_by`，alias 集来自 `alias_groups`；
2. **每个 shape 维度**：`zip(shape_constant, shape_divisible_by)`，若 `shape_constant[i] is not None` 则 `div_by = shape_constant[i]`，否则用 `shape_divisible_by[i]`；
3. **每个 stride 维度**：同理用 `stride_constant` 覆盖 `stride_divisible_by`。

折叠规则可以写成：

\[
\text{div\_by}_i =
\begin{cases}
\text{shape\_constant}_i & \text{若该维有静态常量}\\
\text{shape\_divisible\_by}_i & \text{否则}
\end{cases}
\]

注意 `_signature.py` 的构造器 [`_remove_redundant_divisibility_constraints`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_signature.py#L498-L508) 已经保证：当 `shape_constant[i]` 非 None 时，`shape_divisible_by[i]` 必须能整除它（否则构造期就报错），并被改写为 1（标记「已由常量接管」）。所以 dataflow 这里只需无条件用常量覆盖即可，逻辑很干净。

#### 4.3.3 源码精读

[`_get_array_predicates`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dataflow_analysis.py#L127-L149) 的折叠片段（shape 与 stride 两段对称）：

```python
# Shape 谓词：静态值折叠成整除性事实
for static, div_by in zip(constraint.shape_constant, constraint.shape_divisible_by, strict=True):
    if static is not None:
        div_by = static
    ret.append(DataPredicate(alias_set=ALIAS_UNIVERSE, div_by=div_by, may_alias_internally=True))
```

字段定义见 [`ArrayConstraint`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_signature.py#L97-L104)，其中 `shape_constant` 的文档 [L69-L75](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_signature.py#L69-L75) 明确：「若 `shape_constant[i]` 已设，则 `shape_divisible_by[i]` 冗余且必须兼容」。

而静态形状本身需要 `cutile_python_v2` 调用约定，由 [`_validate_constraint_support`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_signature.py#L511-L529) 在 `KernelSignature` 构造期门控（`any(x is not None for x in constraint.shape_constant) and cconv.version < 2` 即报错）。

#### 4.3.4 代码实践

阅读 [`test_static_shape_seed_from_array_arg`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_propagate_divby.py#L103-L112)：内核声明 `static_shape_dims=(0,)`，约束里 `shape_const=(16, None)`。测试断言生成的 `MakeTensorView.shape` 只剩 1 个动态维度（第 0 维被静态化为常量 16）。

1. **实践目标**：验证静态形状同时「特化 cubin」与「折叠为整除性事实」两件事。
2. **操作步骤**：把约束改成 `shape_const=(32, None)`，对照 [`test_seed_from_array_arg`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_propagate_divby.py#L95-L100) 的断言风格，手算 `get_op_divby(body, MakeTensorView)` 里 `shape[0]` 应为多少。
3. **需要观察的现象**：`shape[0]` 的 `AssumeDivBy.divisor` 应等于静态值的最小 2 的幂因子——32 → 32，24 → 8（因为 `add_divby_pass` 只取 2 的幂）。
4. **预期结果**：`shape_const=(32,)` → `shape[0]: 32`。**待本地验证**。
5. 再对照 [`test_shape_constant_without_annotation_is_dynamic`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_propagate_divby.py#L115-L122) 理解：仅在**约束**里给 `shape_const` 而不给 `static_shape_dims` 注解时，形状并不会被静态化（仍是动态的），但它仍会作为整除性事实被折叠——这是「整除性收益」与「特化收益」可解耦的体现。

#### 4.3.5 小练习与答案

**练习 1**：用户在 `static_shape_dims=(0,)` 的数组上又写了 `ct.assume_divisible_by(x.shape[0], 4)`，但实际静态值是 7。会发生什么？

答案：`assume_div_by` 检测到 `x.shape[0]` 是已知常量 7，且 `7 % 4 != 0`，于是抛 `TileTypeError("not divisible")`。这正是 [`test_assume_divisible_by_error_on_contradicting_constant_shape`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_propagate_divby.py#L245-L253) 的覆盖。

**练习 2**：为什么 `_get_array_predicates` 里 shape/stride 谓词的 `alias_set` 是 `ALIAS_UNIVERSE` 而不是来自 `alias_groups`？

答案：别名只对**指针（地址）**有意义，shape/stride 是「数值」而非地址，没有别名概念，故标记为「可能与任何东西别名」（即不做别名约束），只承载整除性信息。

---

### 4.4 add_divby_pass：把整除性落成 AssumeDivBy

#### 4.4.1 概念说明

`dataflow_analysis` 只是**算出**了每个 Var 的 `div_by`，但 IR 里并没有把这些事实「告诉」后端——`div_by` 只存在于分析结果字典里。真正向编译器传递对齐信息的载体是 IR 操作 `AssumeDivBy`（见 4.5）。

`add_divby_pass` 的职责就是：在**真正能用上对齐信息的访存操作**前面，插入 `AssumeDivBy`。它不是对所有 Var 都插，只对那几类「吃指针/形状/步长、并能因此生成更快指令」的操作插——具体是 `MakeTensorView`（数组视图，对应 `ct.load`/`ct.store`/slice 的底层）、`LoadPointer`、`StorePointer`（对应 `ct.gather`/`ct.scatter` 的底层）。

#### 4.4.2 核心流程

pass 分两遍：

1. **扫描（`_scan_block`）**：递归遍历 IR，把所有 `_OPS_NEED_ASSUME` 操作的全部输入 Var 名收集进 `candidates` 集合。
2. **改写（`_rewrite_block`）**：再次遍历，对每个出现在 `candidates` 里的 Var，调用 `_add_assume_divby`：
   - 查 `df_result[var].div_by`；
   - 取其**最小 2 的幂因子** `power_of_2_d = min(divisor & -divisor, 1024)`（`x & -x` 是经典的「提取最低置 1 位」技巧）；
   - 若 `power_of_2_d > 1`，新建一个 `result_var`，插入 `AssumeDivBy(divisor=power_of_2_d, x=var)`，并在 `var_map` 里把原 Var 重映射到 `result_var`；
   - 用 `_remap_operands` 把后续操作里对该 Var 的引用改成 `result_var`，使 assume 生效。

为什么只取 2 的幂？因为 GPU 对齐优化（TMA、向量化）只关心 2 的幂对齐；`assume_divisible_by(n, 12)` 这种非纯 2 幂的 divisor，提取出 `12 & -12 = 4`，只把 4 这一有用的对齐信息下沉——见 [`test_assume_divisible_by_non_power_of_two_divisor`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_propagate_divby.py#L182-L197)。

\[
\text{power\_of\_2\_d} = \min(\text{div\_by}\ \&\ (-\text{div\_by}),\ 1024)
\]

#### 4.4.3 源码精读

需要插 assume 的操作清单 [`_OPS_NEED_ASSUME`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/propagate_divby.py#L11)：

```python
_OPS_NEED_ASSUME = (MakeTensorView, LoadPointer, StorePointer)
```

核心改写函数 [`_rewrite_block`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/propagate_divby.py#L31-L47)：先对 block 入口参数插 assume（`block.params` 也可能是 candidates），再逐 op 处理——把「当前 op 的结果 Var 中属于 candidates 的」在其产生后立刻插 assume，递归处理 nested blocks，最后用 `block[:] = new_ops` 整块替换。

插入逻辑 [`_add_assume_divby`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/propagate_divby.py#L62-L86)：

```python
MAX_DIVBY = 1024
divisor = df_result[x.name].div_by
power_of_2_d = min(divisor & -divisor, MAX_DIVBY)
if power_of_2_d > 1:
    result_var = x.ctx.make_var_like(x)
    ...
    op = AssumeDivBy(divisor=power_of_2_d, x=x, result_vars=(result_var,), loc=x.loc)
    op_list.append(op)
    var_map[x.name] = result_var
    # 同步更新 df_result，让后续传播看到 assume 后的新值
    df_result.predicates[result_var.name] = DataPredicate(..., div_by=power_of_2_d, ...)
```

值得注意的细节：插入 assume 后，它**就地更新了 `df_result`**（给 `result_var` 写入新谓词）。这是因为 `df_result` 接下来还要被 `token_order_pass` 使用（见 [`_transform_ir`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L100-L106)），让 token 排序也能看到对齐后的别名信息——这正是 u6-l1 强调的「dataflow 结果被 divby 与 token_order 共享」。

`MAX_DIVBY = 1024` 是一个工程上限，避免极端整除性（如一个超大常量）生成无意义的巨型 divisor。

#### 4.4.4 代码实践

1. **实践目标**：观察 `add_divby_pass` 为哪些操作的哪些输入插入了 `AssumeDivBy`。
2. **操作步骤**：以 [`test_seed_from_array_arg`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_propagate_divby.py#L95-L100) 为模板，构造一个 `base_div=16, stride_div=(8,1), shape_div=(4,1)` 的 2D 数组约束，编译后遍历 IR：
   ```python
   # 示例代码（基于 test_propagate_divby.get_ir）
   body = get_ir(kernel, (array_arg(ndim=2, base_div=16, stride_div=(8, 1), shape_div=(4, 1)),))
   for op in body.traverse():
       if isinstance(op, AssumeDivBy):
           print(op.divisor, op.x.name, "->", op.result_var.name)
   ```
3. **需要观察的现象**：会看到 4 条 `AssumeDivBy`，divisor 分别为 16（base_ptr）、4（shape[0]）、8（stride[0]）；它们紧贴在 `MakeTensorView` 之前。
4. **预期结果**：与测试断言 `{'base_ptr': 16, 'shape[0]': 4, 'stride[0]': 8}` 一致。`stride[1]` 的 divisor 为 1，不插 assume。**待本地验证**。
5. 想直接看 MLIR/字节码产物，可设环境变量 `CUDA_TILE_DUMP_TILEIR=1` 或 `CUDA_TILE_LOGS=CUTILEIR`（见 [`_debug.py`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_debug.py#L10-L18)），在 dump 里定位 `assume_div_by` 操作。

#### 4.4.5 小练习与答案

**练习 1**：`div_by = 12` 时，`add_divby_pass` 插入的 `AssumeDivBy.divisor` 是多少？为什么？

答案：`12 & -12 = 4`（12 的二进制 `1100`，最低置 1 位是 4），所以 divisor=4。因为 GPU 对齐优化只吃 2 的幂，12 = 4×3 里只有 4 这部分对齐信息可用。

**练习 2**：为什么 pass 要分「扫描」和「改写」两遍，而不是一遍搞定？

答案：扫描阶段需要先确定「哪些 Var 是访存操作的输入」这个全局集合，因为一个 Var 可能在多处被消费、也可能在它产生之前就被 block 参数引用。两遍式让 `_add_assume_divby` 通过 `var_map` 去重——同一个 Var 只插一次 assume，所有消费点统一重映射到同一个 `result_var`。

---

### 4.5 AssumeDivBy 与 assume_divisible_by：向编译器传递对齐、并对常量校验

#### 4.5.1 概念说明

`AssumeDivBy` 是一条**无副作用、仅传递假设**的 IR 操作：它声明「`x` 是 `divisor` 的倍数」，结果值等于 `x`（值不变），只是给后端编译器挂上一条对齐提示。它有两个来源：

1. **编译器自动插入**：即 4.4 的 `add_divby_pass`，把分析出的整除性下沉成操作；
2. **用户显式声明**：用户在内核里写 `ct.assume_divisible_by(x, d)`，由 `@impl(ct.assume_divisible_by)` 落到 `assume_div_by` 辅助函数，再发射 `AssumeDivBy`。

第二条路径有一条重要的安全网：如果 `x` 是**已知常量**（如 `Constant` 参数或静态形状），`assume_div_by` 会当场校验 `val % divisor == 0`，否则抛 `TileTypeError`——把「用户承诺与已知事实矛盾」的错误前移到编译期，而不是留到运行时产生错误结果。`divisor=1` 或 `None` 时是 no-op；运行时禁用（`CUDA_TILE_TESTING_DISABLE_DIV=1`）也是 no-op。

#### 4.5.2 核心流程

```
assume_div_by(x, divisor):
    if divisor is None or divisor == 1 or 禁用: return x          # no-op
    if x 是常量:
        if val % divisor != 0:
            raise TileTypeError("not divisible")                  # 常量校验
        return x                                                  # 常量本身已满足，无需插 op
    return add_operation(AssumeDivBy, ..., x=x, divisor=divisor)  # 动态值才插 op
```

`AssumeDivBy` 在字节码层编码为一条 `Assume` 指令，携带 `DivBy(divisor)` 谓词（与 `AssumeBounded` 共用 `encode_AssumeOp`，只是谓词类型不同）。

#### 4.5.3 源码精读

操作定义 [`AssumeDivBy`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ops.py#L612-L621)：

```python
@dataclass(eq=False)
class AssumeDivBy(Operation, opcode="assume_div_by"):
    divisor: int = attribute()
    x: Var = operand()

    def generate_bytecode(self, ctx):
        return bc.encode_AssumeOp(ctx.builder, ctx.typeid_of(self.result_var),
                                  ctx.get_value(self.x), bc.DivBy(self.divisor))
```

辅助函数 [`assume_div_by`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ops.py#L624-L634) 含常量校验：

```python
if x.is_constant():
    val = x.get_constant()
    if val % divisor != 0:
        raise TileTypeError(f"Value {val} is not divisible by {divisor}: "
                            f"`assume_divisible_by` contradicts a known constant")
    return x
return add_operation(AssumeDivBy, x.get_type(), x=x, divisor=divisor)
```

用户 API 实现 [`assume_divisible_by_impl`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ops.py#L637-L647) 做参数校验：`x` 必须是整数标量、`divisor` 必须是正整数常量，否则抛 `TileTypeError`。其 stub 签名见 [`_stub.py`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_stub.py#L1214-L1241)。

而 `dataflow_analysis` 在遇到已存在的 `AssumeDivBy` 时，会把 `div_by` **替换**为 `divisor`（见 4.1.3 的 L278-L281），使得用户手写的 assume 也能继续向下游传播——自动插入与手动声明走的是同一套传播机制。

#### 4.5.4 代码实践

1. **实践目标**：体验 `assume_divisible_by` 的常量校验与 no-op 行为。
2. **操作步骤**：
   - 运行 `pytest test/test_propagate_divby.py::test_assume_divisible_by_error_on_contradicting_constant -q`（`Constant(7)` 对 4 取模不为 0，应报错）；
   - 运行 `pytest test/test_propagate_divby.py::test_assume_divisible_by_divisor_one_is_noop -q`（divisor=1 应完全不产生 `AssumeDivBy`）。
3. **需要观察的现象**：前者抛 `TileTypeError(match="not divisible")`；后者断言 `[op for op in body.traverse() if isinstance(op, AssumeDivBy)] == []`。
4. **预期结果**：两条测试均通过，验证「常量校验」与「divisor=1 no-op」两条规则。**待本地验证**。
5. 进阶：对照 [`test_assume_divisible_by_propagates_to_dynamic_slice`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_propagate_divby.py#L149-L166) 看 `assume_divisible_by(start_factor, 32)` 的整除性如何经乘法（`bid(0) * start_factor`）传播到 slice 的 base_ptr。

#### 4.5.5 小练习与答案

**练习 1**：`assume_divisible_by(x, 16)` 中，`divisor=16` 是 attribute 还是 operand？为什么不能是 operand？

答案：是 `attribute`（编译期常量）。对齐优化要求 divisor 在编译期已知；若它是运行时 operand，编译器无法据此选择指令，assume 就失去意义。所以 `assume_divisible_by_impl` 用 `require_constant_int(divisor)` 强制其为常量。

**练习 2**：为什么对已知常量 `x`，`assume_div_by` 在校验通过后 `return x` 而不插 `AssumeDivBy`？

答案：常量值已经字面已知，编译器可以直接读出它的所有对齐性质，无需再插一条 assume 操作来「告诉」自己；插了反而是冗余 IR。校验失败的则报错——assume 的承诺与已知常量矛盾。

---

## 5. 综合实践

把本讲五个模块串起来，做一个「对照 dump 说明 add_divby 注入了哪些整除性假设、静态形状如何成为整除性事实」的小任务。

**任务**：写一个最小的 slice 内核，分别用「`base_addr_divisible_by` + `shape_divisible_by`」与「`static_shape_dims`」两种约束编译，对比 dump 出的 `AssumeDivBy` 与 `MakeTensorView`。

```python
# 示例代码（仿照 test/test_propagate_divby.py 的 get_ir / array_arg）
from typing import Annotated
import cuda.tile as ct
from cuda.tile._ir.ops import AssumeDivBy, MakeTensorView
from cuda.tile._compile import compile_tile
from cuda.tile.compilation import KernelSignature, ArrayConstraint
from cuda.tile._cext import CallingConvention

def get_ir(func, args):
    sig = KernelSignature(args, CallingConvention.cutile_python_v2())
    [body] = compile_tile(func, [sig], return_final_ir=True, return_cubin=False).final_ir
    return body

# (A) 仅用整除性假设：base 对齐 16B，第 0 维形状是 4 的倍数
def kern_a(x):
    y = x.slice(axis=0, start=0, stop=4)
    ct.store(y, (0,), ct.load(y, (0,), (1,)))

ca = ArrayConstraint(ct.float32, 1, index_dtype=ct.int32,
                     base_addr_divisible_by=16, stride_lower_bound_incl=0,
                     stride_constant=(1,), shape_divisible_by=(4,),
                     alias_groups=[], may_alias_internally=False)
body_a = get_ir(kern_a, (ca,))

# (B) 用静态形状：把第 0 维特化为编译期常量 16
def kern_b(x: Annotated[ct.Array, ct.ArrayAnnotation(static_shape_dims=(0,))]):
    y = x.slice(axis=0, start=0, stop=4)
    ct.store(y, (0,), ct.load(y, (0,), (1,)))

cb = ArrayConstraint(ct.float32, 1, index_dtype=ct.int32,
                     base_addr_divisible_by=16, stride_lower_bound_incl=0,
                     stride_constant=(1,), shape_constant=(16,),
                     alias_groups=[], may_alias_internally=False)
body_b = get_ir(kern_b, (cb,))

for name, body in [("A 整除性假设", body_a), ("B 静态形状", body_b)]:
    print(f"--- {name} ---")
    for op in body.traverse():
        if isinstance(op, AssumeDivBy):
            print(f"  AssumeDivBy divisor={op.divisor}  {op.x.name} -> {op.result_var.name}")
        elif isinstance(op, MakeTensorView):
            print(f"  MakeTensorView shape_len={len(op.shape)}")
```

**操作步骤**：

1. 安装 `cuda-tile[tileiras]` 与 torch（GPU 环境）；
2. 把上面脚本存为 `inspect_divby.py` 并运行；
3. 对照两个版本的输出。

**需要观察与解释**：

- 版本 A：会看到 base_ptr（16）、shape[0]（4）两条 `AssumeDivBy`，`MakeTensorView.shape` 长度仍为 1（动态形状）。
- 版本 B：`shape[0]` 的整除性来自静态常量 16（折叠），`MakeTensorView.shape` 长度为 0（第 0 维被静态化进 `ArrayTy.shape`，不再是动态参数，参见 u5-l6）。base_ptr 仍为 16。
- 结论：静态形状比纯整除性假设更强——它既折叠成整除性事实（白捡对齐），又消掉一个运行时参数（特化 cubin）。

**预期结果**：脚本输出两组 `AssumeDivBy`/`MakeTensorView` 报告，差异如上。**待本地验证**（需 GPU 与已安装包）；若想完全离线，可用 `pytest test/test_propagate_divby.py -k "seed_from_array_arg or static_shape_seed"` 间接验证同样的事实。

## 6. 本讲小结

- `dataflow_analysis` 在 IR 上跑一个**不动点循环**，为每个 Var 推断两类谓词：**别名集**（位掩码）与**整除性**（`div_by`）；控制流汇合时取并集（别名按位或、整除性取 gcd）。
- `_register_tuple_params` 递归处理 `TupleConstraint`，用 `offset` 游标把 tuple 的整除性/别名假设对齐到展开后的扁平 Var；叶子数组的扁平宽度是 `1 + 2*ndim`。
- `_get_array_predicates` 把 `shape_constant`/`stride_constant` **折叠**成整除性事实（`div_by = static`），让静态形状既特化 cubin、又白送一条对齐信息。
- `add_divby_pass` 只对 `MakeTensorView`/`LoadPointer`/`StorePointer` 的输入插 `AssumeDivBy`，且只取 `div_by` 的**最小 2 的幂因子**（`x & -x`，上限 1024），插完后就地更新 `df_result` 供 `token_order_pass` 共享。
- `AssumeDivBy` 是无副作用的「对齐假设」操作；用户 API `ct.assume_divisible_by` 与自动插入走同一套传播；对已知常量会做编译期校验，矛盾即抛 `TileTypeError`。
- `divisor=1`、`None` 或 `CUDA_TILE_TESTING_DISABLE_DIV=1` 时全程 no-op，便于测试时关闭本优化。

## 7. 下一步学习建议

- **下一讲 u6-l3（token_order_pass）**：本讲产出的 `df_result`（含 assume 后更新的别名集）会被 token 排序复用——理解 token 链时回头看本讲的 `DataPredicate.alias_set` 会非常自然。
- **u6-l4（循环外提与 FMA 重写）**：`hoist_loop_invariants` 必须晚于 token_order，且整除性事实会帮助判断 load 是否可外提；可结合本讲读 `code_motion.py`。
- **继续阅读源码**：想看「别名集如何被 token_order 消费」可读 `src/cuda/tile/_passes/token_order.py`；想看「静态形状如何进入 `ArrayTy.shape`」可回看 u5-l6 的 `type.py` 与 u5-l2 的 `_get_array_ty`。
- **跑测试巩固**：`pytest test/test_propagate_divby.py -v` 覆盖了本讲几乎每条规则，是最佳的自测材料。
