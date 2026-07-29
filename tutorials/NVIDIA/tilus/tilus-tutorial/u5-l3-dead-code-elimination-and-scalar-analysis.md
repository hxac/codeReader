# 死代码消除与标量分析

## 1. 本讲目标

本讲聚焦 Tilus 默认变换流水线（u5-l2）的「收尾三连」——`bound_aware_simplify`、`analyze_scalar`、`dead_code_elimination`。这三个 Pass 看似各自独立，其实构成一条数据依赖链：**标量分析**先推导出每个整型变量的取值范围与整除性，**界感知化简**消费这些信息折叠比较与循环，**死代码消除**最后清扫无人使用的功能指令。学完本讲，你应当能够：

- 说清 DCE 用「功能指令白名单 + 活跃性不动点」判定一条指令能否删除，以及为何副作用指令永远不能删（但可以把原子指令的 `output` 改写为 `None`）。
- 理解 `ScalarSet` 这个「整除性 + 上下界」抽象域，以及 `analyze_scalar` 如何用不动点迭代 + 加宽（widening）保证终止。
- 读懂 `Analysis` 数据结构（`divisibility` / `lower_bound` / `upper_bound`）如何挂在 `Function.metadata` 上，并被界感知化简消费。
- 会参照官方测试构造一段含死代码的 IR，运行 DCE 并用 `collect_instructions` 验证消除结果。

## 2. 前置知识

在进入源码前，先用三段通俗的话建立直觉。

**什么是死代码消除（Dead Code Elimination, DCE）？** 一个程序里如果算出了一个值却从没被任何人使用，那这次计算就是「死」的，可以删掉。难点不在于「删」这个动作，而在于判断「真的没人用吗」——因为 Tilus IR 里指令之间通过张量（`Tensor`）互相连接，要顺着 `output → inputs` 的链条追到根，才知道某个中间结果是否真的无人消费。

**什么是标量分析（Scalar Analysis）？** 这里分析的「标量」是 Hidet 表达式里的整型 `Var`，比如循环变量、偏移量、`blockIdx`。编译器想知道的不是「这个变量等于几」，而是「它可能取哪些值」——能否被某数整除、最小/最大是多少。这类信息能帮助折叠 `if`/`for`、化简地址计算。Tilus 用一个叫 `ScalarSet` 的抽象域来近似「一个变量可能的整数集合」。

**什么是界感知化简（Bound-Aware Simplify）？** 拿到标量分析给出的上下界后，编译器可以做很多「显然成立」的化简：如果循环次数恒为 0 就删掉整个循环，恒为 1 就把循环体 inline 进来；如果 `if` 条件恒真/恒假就只保留一个分支；如果 `a <= b` 在已知上下界下必然成立，就把比较折叠成 `true`。

> **前置承接**：本讲假定你已读过 u3-l4（功能指令 vs 副作用指令、`FUNCTIONAL_INST_TYPES` 白名单、Tensor 的身份相等）、u3-l5（`collect_instructions` 等工具）、u5-l1（`Pass`/`IRRewriter`/`IRVisitor` 框架）与 u5-l2（默认流水线 `get_default_passes` 的 12 个 Pass 及其顺序）。本讲讲解的三个 Pass 正是 u5-l2 流水线的最后三个。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/tilus/transforms/dead_code_elimination.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/dead_code_elimination.py) | DCE Pass 的全部实现：白名单、活跃性收集器、消除器 |
| [python/tilus/transforms/scalar_analyze.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/scalar_analyze.py) | 标量分析 Pass 的薄封装，真正逻辑在 analyzers 子包 |
| [python/tilus/ir/analyzers/scalar_analyzer.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/analyzers/scalar_analyzer.py) | `ScalarSet` 抽象域、`ScalarSetAnalyzer`、`analyze_scalar` 不动点算法 |
| [python/tilus/transforms/bound_aware_simplify.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/bound_aware_simplify.py) | 界感知化简 Pass：消费 `Analysis` 折叠比较/循环/分支 |
| [python/tilus/ir/func.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/func.py) | `Analysis` 与 `Metadata` 数据结构定义 |
| [tests/transforms/test_dead_code_elimination.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/tests/transforms/test_dead_code_elimination.py) | DCE 的官方端到端测试，是本讲实践的模板 |

## 4. 核心概念与源码讲解

### 4.1 死代码消除（DCE）：白名单与活跃性

#### 4.1.1 概念说明

DCE 要回答的问题是：「这条指令算出来的张量，到底有没有人用？」如果没人用，且这条指令本身**没有副作用**（不会改内存、不会同步），那它就是纯计算，删掉不影响程序行为。

这里的关键判定标准不是「`output is None`」（u3-l4 已强调过），而是「是否在功能指令白名单 `FUNCTIONAL_INST_TYPES` 里」。这带来三类指令、三种处理方式：

| 指令类别 | 例子 | DCE 处理 |
| --- | --- | --- |
| 功能指令（pure） | `AddInst`、`CastInst`、`LoadGlobalInst`、`DotInst` | 若 `output` 无人用 → **整条删除** |
| 带可选输出的副作用指令 | `AtomicSharedInst` 等 4 种原子指令 | 指令**保留**（RMW 必须发生），但把 `output` 改写为 `None`，让 codegen 发射不带目的寄存器的 PTX |
| 纯副作用指令 | `StoreGlobalInst`、`SyncThreadsInst` | **原样保留**，其 `inputs` 无条件标记为活跃 |

为什么原子指令要特殊对待？因为原子操作（read-modify-write）的「写回」是副作用必须保留，但它返回的「旧值」寄存器常常被丢弃。Tilus 用 `dataclasses.replace(inst, output=None)` 生成一条新指令，保留副作用、丢掉没人要的返回值。

#### 4.1.2 核心流程

DCE 分两遍跑（典型的工作列表算法）：

```
Pass 1: 收集活跃张量（UsedTensorCollector）
  ├─ 遍历每个指令
  │   ├─ 副作用指令：把所有 inputs 标记为 used
  │   ├─ 带可选输出的副作用指令：inputs 标记 used，并记录到待处理列表
  │   └─ 功能指令：暂存，等传播
  ├─ 处理 TensorItem*Stmt（张量↔标量桥）：仅当绑定的 Var 被表达式引用才标记 used
  └─ 不动点传播：反复扫描功能指令列表
        若某功能指令的 output ∈ used → 把它的 inputs 也标记为 used
        直到不再变化

Pass 2: 消除（DeadCodeEliminator）
  ├─ 功能指令且 output ∉ used → 返回 None（InstStmt 塌缩为空 SeqStmt）
  ├─ 带可选输出的副作用指令且 output ∉ used → replace(output=None)
  └─ 其余原样返回
```

这里有个性能优化：`DeadCodeEliminationPass.process_function` 在跑 Pass 2 之前先做一次 `has_dead` 检查，如果发现根本没有可删的死代码，就直接 `return function`（返回原对象），让上层的 `Pass.process_program` 用 `is` 短路判定「函数没变」，避免无谓的 IR 重建。

#### 4.1.3 源码精读

**白名单定义**——这是「能否删」的唯一判据，覆盖寄存器运算、加载、视图、随机数、甚至 `DotInst`/`AllocBarrierInst`（它们虽有产出但被认定为纯计算）：

[python/tilus/transforms/dead_code_elimination.py:76-113](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/dead_code_elimination.py#L76-L113) 定义了功能指令白名单 `FUNCTIONAL_INST_TYPES`；紧接其后的 [python/tilus/transforms/dead_code_elimination.py:120-125](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/dead_code_elimination.py#L120-L125) 定义了「带可选输出的副作用指令」第二张表，正是 4 种原子指令。

**活跃性收集器**——`UsedTensorCollector` 继承只读的 `IRVisitor`。它的 `visit_Instruction` 把指令分流：

```python
# python/tilus/transforms/dead_code_elimination.py:166-178
def visit_Instruction(self, inst: Instruction) -> None:
    if _is_functional(inst):
        self.functional_insts.append(inst)
    else:
        # Side-effecting: all inputs are unconditionally used
        for tensor in inst.inputs:
            self.used_tensors.add(id(tensor))
        if _is_side_effecting_with_optional_output(inst):
            self.side_effecting_optional_insts.append(inst)
    for value in inst.attributes.values():
        self._collect_expr_vars(value)
```

注意两个细节：(1) 用 `id(tensor)` 而非 tensor 本身做集合元素——因为 Tensor 是身份相等（`eq=False`），`id()` 是最稳的判等方式；(2) 扫描 `inst.attributes.values()` 收集 Hidet `Var`，对应 u5-l1 提醒过的「指令 attributes 里藏 Hidet 表达式」陷阱。

**不动点传播**——功能指令的活跃性是「逆向 + 链式」的：一条加法的 output 没人用，它的 inputs 也可能跟着变成死的。所以需要反复扫描直到稳定：

```python
# python/tilus/transforms/dead_code_elimination.py:222-229
changed = True
while changed:
    changed = False
    for inst in self.functional_insts:
        if inst.output is not None and id(inst.output) in self.used_tensors:
            for tensor in inst.inputs:
                if self._mark_used(tensor):
                    changed = True
```

**消除器**——`DeadCodeEliminator` 继承 `IRRewriter`（改写器，返回新节点）。它的 `visit_Instruction` 返回 `None` 即让 `InstStmt` 塌缩为空 `SeqStmt`（u5-l1 讲过的「删除指令的标准入口」）：

```python
# python/tilus/transforms/dead_code_elimination.py:244-254
def visit_Instruction(self, inst: Instruction) -> Instruction | None:
    if _is_functional(inst) and inst.output is not None and id(inst.output) not in self.used_tensors:
        return None                              # 整条删除
    if (_is_side_effecting_with_optional_output(inst)
            and inst.output is not None
            and id(inst.output) not in self.used_tensors):
        return dataclasses.replace(inst, output=None)   # 保留副作用，丢返回值
    return super().visit_Instruction(inst)
```

**Pass 编排与短路**——[python/tilus/transforms/dead_code_elimination.py:268-292](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/dead_code_elimination.py#L268-L292) 是 `DeadCodeEliminationPass.process_function`：先收集+传播，再用 `has_dead` 判定，无死代码直接返回原函数，否则才跑消除器。

#### 4.1.4 代码实践

**实践目标**：参照官方测试，亲手构造一段含「死链」的 IR，跑 DCE，用 `collect_instructions` 验证消除效果。

**操作步骤**（新建 `dce_demo.py`）：

```python
# 示例代码：手动构造 IR 并运行 DCE
from tilus.hidet.ir.dtypes import float32
from tilus.hidet.ir.expr import Var
from tilus.hidet.ir.type import PointerType
from tilus.hidet.ir.primitives.cuda.vars import blockIdx
from tilus.hidet.ir.expr import as_expr
from tilus.ir.func import Function, Metadata
from tilus.ir.instructions.generic import (
    AddInst, MulInst, AllocateRegisterInst, StoreGlobalGenericInst,
)
from tilus.ir.prog import Program
from tilus.ir.stmt import InstStmt, SeqStmt
from tilus.ir.tensor import RegisterTensor
from tilus.ir.tools.instruction_collector import collect_instructions
from tilus.transforms.dead_code_elimination import dead_code_elimination_pass


def alloc(shape=(4,)):
    out = RegisterTensor.create(dtype=float32, shape=shape)
    inst = AllocateRegisterInst.create(output=out, f_init=lambda _: float32.zero)
    return inst, out


# 构造：a + b -> add_out；add_out * a -> mul_out；但只 store(a)
# 故 add_out、mul_out 都无人消费，整条链是死的
alloc_a, a = alloc()
alloc_b, b = alloc()
add_out = RegisterTensor.create(dtype=float32, shape=(4,))
add_inst = AddInst.create(a, b, add_out)
mul_out = RegisterTensor.create(dtype=float32, shape=(4,))
mul_inst = MulInst.create(add_out, a, mul_out)      # 死：mul_out 无人用
p = Var("p", PointerType(float32))
store = StoreGlobalGenericInst.create(x=a, ptr=p, f_offset=lambda _: 0)  # 只存 a

body = SeqStmt(tuple(InstStmt(i) for i in [alloc_a, alloc_b, add_inst, mul_inst, store]))
func = Function.create(
    name="demo", params=[p], body=body,
    metadata=Metadata.create(
        grid_blocks=[as_expr(1), as_expr(1), as_expr(1)],
        cluster_blocks=[1, 1, 1],
        block_indices=[blockIdx.x, blockIdx.y, blockIdx.z],
        num_warps=1,
    ),
)
prog = Program.create({"demo": func})


def count(prog, cls):
    f = list(prog.functions.values())[0]
    return sum(1 for i in collect_instructions(f) if isinstance(i, cls))


print("优化前 Add/Mul/Store:", count(prog, AddInst), count(prog, MulInst), count(prog, StoreGlobalGenericInst))
opt = dead_code_elimination_pass()(prog)
print("优化后 Add/Mul/Store:", count(opt, AddInst), count(opt, MulInst), count(opt, StoreGlobalGenericInst))
```

**需要观察的现象**：由于 `mul_inst` 的 `mul_out` 无人用，它先被判定为死；而不动点传播会进一步发现 `add_inst` 的 `add_out` 只被 `mul_inst` 用（而 `mul_inst` 本身已死），于是 `add_inst` 也变成死的、连带 `alloc_b`（`b` 仅喂给死的 add）也被删。

**预期结果**：`Add/Mul/Store` 优化前为 `1 1 1`，优化后为 `0 0 1`，且 `AllocateRegisterInst` 数量从 2 降到 1（只剩 `alloc_a`）。这与官方测试 `test_chain_elimination` 的断言一致（[tests/transforms/test_dead_code_elimination.py:119-136](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/tests/transforms/test_dead_code_elimination.py#L119-L136)）。

> 若你的环境未配好 GPU/编译链，本实践只涉及纯 IR 构造与 Pass 运行，不触发 codegen，应当无需 GPU 即可运行；如遇导入问题则为环境配置问题，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果把上面例子里 `store` 改成 `StoreGlobalGenericInst.create(x=add_out, ...)`（存 `add_out` 而非 `a`），优化后 `Add/Mul` 各剩几个？

**答案**：`Add` 剩 1（`add_out` 被 store 消费，活跃），`Mul` 剩 0（`mul_out` 仍无人用）。这正是 `test_partial_chain_elimination` 覆盖的「只删死尾、保留活前缀」场景（[tests/transforms/test_dead_code_elimination.py:139-154](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/tests/transforms/test_dead_code_elimination.py#L139-L154)）。

**练习 2**：一个 `AtomicSharedInst`（op=add）的 `output`（旧值寄存器）没人用，DCE 之后这条指令的 `output` 字段会变成什么？指令本身会被删吗？

**答案**：指令本身**不会**被删（原子 RMW 是副作用必须保留），但 `output` 会被改写为 `None`，使 codegen 发射不带目的寄存器的原子 PTX。见 `test_atomic_shared_unused_output_is_nulled_not_eliminated`（[tests/transforms/test_dead_code_elimination.py:253-267](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/tests/transforms/test_dead_code_elimination.py#L253-L267)）。

### 4.2 标量分析（Scalar Analysis）：ScalarSet 抽象与不动点

#### 4.2.1 概念说明

标量分析的目标是给函数体内每个整型 `Var` 估算一个「可能取值集合」的近似。Tilus 用 `ScalarSet` 这个抽象域来表示：一个集合由三要素刻画——**整除性** `divisibility`、**下界** `lower_bound`、**上界** `upper_bound`。形式化地，一个 `ScalarSet(div=d, lb=l, ub=u)` 代表：

\[
\{\, n \in \mathbb{Z} \;\mid\; d \mid n \;\land\; (l \neq \text{None} \Rightarrow n \ge l) \;\land\; (u \neq \text{None} \Rightarrow n \le u) \,\}
\]

例如 `ScalarSet(divisibility=2, lower_bound=0, upper_bound=10)` 表示 \(\{0,2,4,6,8,10\}\)；`divisibility=1` 表示所有整数（通集）。

为什么用「整除性」而不只是区间？因为在 GPU 内核里，循环步长、张量分块、向量化宽度都要求地址对齐，知道「`offset` 一定是 16 的倍数」比知道「`offset` 在 0~1023」更有用——它能让 codegen 用更宽的加载、让地址化简掉取模。

`analyze_scalar` 最终把每个 `Var` 的 `ScalarSet` 三要素汇总成 `Analysis` 对象，挂到 `Function.metadata.analysis` 上，供后续 Pass（尤其是界感知化简）消费。

#### 4.2.2 核心流程

标量分析是一个标准的**抽象解释（abstract interpretation）**流程：

```
1. 初始化种子的 ScalarSet
   ├─ 函数参数：从 metadata.param2divisibility 取整除性，下界设 0（假设非负）
   └─ block_indices（blockIdx.x/y/z）：下界 0，上界 = grid_blocks[i] - 1（若为常量）

2. 收集所有「定义整型变量」的语句（DeclareStmt / LetStmt / ForStmt / AssignStmt）
   把这些变量初始化为空集（lower_bound=0, upper_bound=-1，即 ∅）

3. 不动点迭代
   反复对每条定义语句：
     rhs_set = ScalarSetAnalyzer(表达式)        # 用抽象运算递归求值
     new_set = old_set ∪ rhs_set                 # 合并多分支
     若 new_set ≠ old_set → 更新，标记 changed
   直到一轮下来无任何变化

4. 加宽（widening）保证终止
   对自引用变量（如 i = i + 1），上下界会无限发散
   用 lower_count / upper_count 计数，超过 UPDATE_COUNT_LIMIT(=10)
   就把对应界置为 None（= ±∞，即 lattice 的顶），继续迭代到稳定

5. 收尾
   若某变量最终是空集 → 抛 ValueError（说明 IR 里有不可达路径）
   否则把 (div, lb, ub) 三个字典打包成 Analysis，写回 metadata
```

抽象域上的运算是「精确结果的最小可表示上近似」。比如两个集合相加，新集合的整除性取 `gcd`、上下界相加：

\[
\text{div}(S_a + S_b) = \gcd(d_a, d_b),\quad
\text{lb} = l_a + l_b,\quad
\text{ub} = u_a + u_b
\]

#### 4.2.3 源码精读

`scalar_analyze.py` 只是个薄封装，把整个函数交给 `analyze_scalar`：

```python
# python/tilus/transforms/scalar_analyze.py:20-22
class AnalyzeScalarPass(Pass):
    def process_function(self, function: Function) -> Function:
        return analyze_scalar(function)
```

真正的算法在 [python/tilus/ir/analyzers/scalar_analyzer.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/analyzers/scalar_analyzer.py)。先看 `ScalarSet` 的抽象运算，以加法为例（整除性取 gcd、上下界分别相加）：

```python
# python/tilus/ir/analyzers/scalar_analyzer.py:176-190
def __add__(self, other: ScalarSet) -> ScalarSet:
    if self.is_empty() or other.is_empty():
        return self.empty_set()
    div = gcd(self.divisibility, other.divisibility)
    lb = None
    if self.lower_bound is not None and other.lower_bound is not None:
        lb = self.lower_bound + other.lower_bound
    ub = None
    if self.upper_bound is not None and other.upper_bound is not None:
        ub = self.upper_bound + other.upper_bound
    return ScalarSet(divisibility=div, lower_bound=lb, upper_bound=ub)
```

`ScalarSetAnalyzer` 是把这些抽象运算挂到 Hidet 表达式节点上的访问器，按运算符分派（`visit_Add` → `+`、`visit_Mod` → `%` 等），对 `generic_min`/`generic_max` 调用还有专门的 `ScalarSet.min/max`（[python/tilus/ir/analyzers/scalar_analyzer.py:321-371](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/analyzers/scalar_analyzer.py#L321-L371)）。

`analyze_scalar` 的不动点主循环（注意加宽计数的处理）：

```python
# python/tilus/ir/analyzers/scalar_analyzer.py:529-540
for var, original_set, union_set in zip(var_list, original_sets, union_sets):
    if union_set != original_set:
        if has_smaller_lower_bound(union_set, original_set):
            lower_count[var] += 1
            if lower_count[var] > UPDATE_COUNT_LIMIT:   # 加宽：下界发散 → 置 None
                union_set.lower_bound = None
        if has_larger_upper_bound(union_set, original_set):
            upper_count[var] += 1
            if upper_count[var] > UPDATE_COUNT_LIMIT:   # 加宽：上界发散 → 置 None
                union_set.upper_bound = None
        var2set[var] = union_set
        updated = True
```

最终把结果打包成 `Analysis` 并写回 `metadata`（[python/tilus/ir/analyzers/scalar_analyzer.py:556-562](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/analyzers/scalar_analyzer.py#L556-L562)）。`Analysis` 本身是 `frozendict` 三件套（不可变、可哈希，是缓存与身份相等的根基）：

```python
# python/tilus/ir/func.py:27-41
@dataclass(frozen=True, eq=False)
class Analysis:
    divisibility: frozendict[Var, int]
    lower_bound: frozendict[Var, int]
    upper_bound: frozendict[Var, int]
    ...
```

而 `Metadata.param2divisibility` 则是种子的来源——它由 u5-l2 讲过的 `lower_assume` 从 `self.assume(x % c == 0)` 落地而来（[python/tilus/ir/func.py:45-51](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/func.py#L45-L51)）。这就把 u2-l3 的 `assume` 提示与本讲的标量分析串了起来。

#### 4.2.4 代码实践

**实践目标**：直观感受 `ScalarSet` 的抽象运算，理解「上近似」语义。

**操作步骤**（在 Python REPL 里）：

```python
from tilus.ir.analyzers.scalar_analyzer import ScalarSet
# A = {0,2,4,6,8,10}，B = {0,3,6,9}
A = ScalarSet(divisibility=2, lower_bound=0, upper_bound=10)
B = ScalarSet(divisibility=3, lower_bound=0, upper_bound=10)
print("A + B =", A + B)          # 整除性 gcd(2,3)=1，界 [0,20]
print("A * B =", A * B)          # 整除性 6，界 [0,100]
print("A | B =", A | B)          # 并集：gcd=1，界 [0,10]
C = ScalarSet(divisibility=4, lower_bound=0, upper_bound=8)
print("C // const2 =", C // ScalarSet(divisibility=2, lower_bound=2, upper_bound=2))
```

**需要观察的现象**：`A + B` 的整除性为什么会变成 1（因为 `gcd(2,3)=1`，即两个不同步长集合相加后无法保证任何公共整除性）；`A | B`（并集）的整除性也是 `gcd(2,3)=1`。

**预期结果**：`A + B` 为 `ScalarSet(lower_bound=0, upper_bound=20)`（整除性 1 不打印）；`A | B` 为 `ScalarSet(lower_bound=0, upper_bound=10)`。这些是抽象域运算的直接体现，无需 GPU。具体打印格式以本地运行为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `analyze_scalar` 必须用不动点迭代，而不能一遍「拓扑排序」就求完？

**答案**：因为变量可能自引用或经 `if`/`for` 形成循环依赖（如 `i = i + 1`、`x = cond ? a : x`），不存在严格的拓扑序。不动点迭代配合加宽能在保证终止的前提下逼近不动点。

**练习 2**：若某个变量最终求出的 `ScalarSet` 是空集（`is_empty()` 为真），`analyze_scalar` 会怎么做？为什么？

**答案**：会抛 `ValueError`（[python/tilus/ir/analyzers/scalar_analyzer.py:547-554](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/analyzers/scalar_analyzer.py#L547-L554)）。空集意味着该变量在所有路径上都无合法取值，通常说明 IR 里有不可达分支或定义缺失，是编译期应当暴露的错误而非默默继续。

### 4.3 界感知化简（Bound-Aware Simplify）

#### 4.3.1 概念说明

`bound_aware_simplify` 是标量分析的直接消费者。它从 `Function.metadata.analysis` 读出每个变量的 `divisibility` / `lower_bound` / `upper_bound`，灌进两个化简引擎：

- **`RuleBasedSimplifier`**（来自内嵌的 hidet）：基于 `BoundInfo`（精确值或上下界）做代数化简，如地址表达式折叠。
- **`ScalarSetBasedSimplifier`**：基于 `ScalarSet` 做比较折叠——能在已知上下界时把 `a <= b` 直接判定为 `true`/`false`。

这个 Pass 能做几类「显而易见但收益大」的化简：折叠恒真/恒假的比较、删除 0 次循环、展开 1 次循环、消除死分支、常量传播 `LetStmt`。这些化简让后续 codegen 不必为永远不执行的代码发射 PTX。

#### 4.3.2 核心流程

```
BoundAwareSimplifyRewriter.visit_Function(func):
  1. 先跑 analyze_scalar(func) 拿到最新 analysis
  2. 把 analysis 里每个变量的 (div, lb, ub) 同时灌进：
       - bound_info_simplifier（hidet 的 BoundInfo 分析器）
       - scalar_set_analyzer（Tilus 的 ScalarSet 分析器）
  3. 遍历整棵 IR：
       visit_Expr:   先 ScalarSet 化简、再 BoundInfo 化简
       visit_LessThan/visit_LessEqual: 比较上下界 → 折叠成 true/false
       visit_ForStmt: extent 恒为 0 → 删；恒为 1 → inline（iter_var 代换为 0）
       visit_IfStmt:  cond 折叠成常量 → 只留 then/else 分支
       visit_LetStmt: bind_value 是常量 → 代入 memo（常量传播）
```

其中比较折叠的核心判据是「上界 ≤ 下界」之类的不等关系。例如对 `a <= b`，若已知 `a` 的上界 ≤ `b` 的下界，则该比较必然成立：

\[
\text{ub}(a) \le \text{lb}(b) \;\Longrightarrow\; (a \le b) \equiv \text{true}
\]

#### 4.3.3 源码精读

`visit_Function` 是数据入口——它先调用 `analyze_scalar` 刷新 `analysis`，再把三要素分别喂给两个化简器（[python/tilus/transforms/bound_aware_simplify.py:79-96](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/bound_aware_simplify.py#L79-L96)）。注意它**不依赖流水线里前一个 `analyze_scalar_pass`**，而是自己再跑一次，保证拿到的是当前 IR 的最新界（因为本 Pass 之前的 Pass 可能改过 IR）。

比较折叠——以 `LessThan` 为例，比较两端的上下界即可判定（`LessEqual` 同理在 [python/tilus/transforms/bound_aware_simplify.py:41-53](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/bound_aware_simplify.py#L41-L53)）：

```python
# python/tilus/transforms/bound_aware_simplify.py:55-67
def visit_LessThan(self, e: LessThan) -> Expr:
    a = self.visit(e.a)
    b = self.visit(e.b)
    sa = self.analyzer(a)
    sb = self.analyzer(b)
    if sa.upper_bound is not None and sb.lower_bound is not None and sa.upper_bound < sb.lower_bound:
        return boolean.true       # a 的最大值仍 < b 的最小值 → 必然 a < b
    if sa.lower_bound is not None and sb.upper_bound is not None and sa.lower_bound >= sb.upper_bound:
        return boolean.false      # a 的最小值仍 >= b 的最大值 → 必然不成立
    ...
```

循环折叠——`visit_ForStmt` 用 hidet 的 `BoundAnalyzer` 求 `extent` 的精确值，若为 0 删空、为 1 则把 `iter_var` 代换为常量 0 并 inline 循环体（[python/tilus/transforms/bound_aware_simplify.py:124-137](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/bound_aware_simplify.py#L124-L137)）：

```python
# python/tilus/transforms/bound_aware_simplify.py:127-135
if bound.value is not None and bound.value in [0, 1]:
    if bound.value == 0:
        return SeqStmt(())                         # 0 次循环：整段删除
    else:
        self.bound[stmt.iter_var] = BoundInfo(value=0)
        self.memo[stmt.iter_var] = int32.zero      # iter_var 代换为常量 0
        ...
        return self.visit(stmt.body)               # 1 次循环：inline
```

常量传播——`visit_LetStmt` 遇到 `bind_value` 是 `Constant` 时，把它写进 `memo`，使后续对该 `bind_var` 的引用被替换为常量（[python/tilus/transforms/bound_aware_simplify.py:103-122](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/bound_aware_simplify.py#L103-L122)）。这里 `memo` 同时充当 u5-l1 讲过的「改写器变量替换表」。

> 此外该 Rewriter 还为 `CopyAsyncGenericInst`/`LoadGlobalGenericInst`/`StoreGlobalGenericInst` 的 `axes` 注入 `[0, extent-1]` 的界（[python/tilus/transforms/bound_aware_simplify.py:169-185](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/bound_aware_simplify.py#L169-L185)），让访存轴的下标比较也能被折叠。

#### 4.3.4 代码实践

**实践目标**：用 `dump_ir` 观察界感知化简对真实内核的改动，找到被折叠/删除的循环或分支。

**操作步骤**：

1. 在一个简单 matmul（如 `examples/matmul/matmul_v0.py`）运行前加上：
   ```python
   import tilus
   tilus.option.cache_dir("dce-demo-cache")
   tilus.option.debug.dump_ir(True)
   ```
2. 运行内核，进入缓存目录 `dce-demo-cache/programs/<hash>/ir/`。
3. 找到 `bound_aware_simplify` 之前与之后的两个 IR 文件（文件名含 pass 名），用 `diff` 对比。

**需要观察的现象**：寻找形如 `for (...)` 循环消失、`if (cond)` 只剩一个分支、或某个比较表达式 `i < N` 被替换成 `True`/`False` 的变化。由于 v0 较简单，变化可能不多。

**预期结果**：在 `bound_aware_simplify` 后的 IR 里，至少能看到一些由 `analyze_scalar` 推出的界被用于化简地址或比较表达式；若无可见变化也属正常（说明该内核没有可折叠的恒定结构），可换更复杂的 `matmul_v2` 重试。具体差异待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`bound_aware_simplify` 为什么在 `visit_Function` 开头要**再调用一次** `analyze_scalar(func)`，明明流水线里它前面就有一个 `analyze_scalar_pass`？

**答案**：因为本 Pass 内部对 IR 的化简（如常量传播、循环 inline）会改变变量与表达式，且它前面的 `layout_inference`/`lower_load_store` 等 Pass 也可能已经改过 IR。为确保喂给两个化简器的界是最新的，它不依赖外层流水线缓存的 analysis，而是自己重算一次，保证正确性。

**练习 2**：`visit_ForStmt` 把 1 次循环 inline 时，为什么要把 `stmt.iter_var` 同时写进 `self.memo` 和 `self.bound_info_simplifier.memo`？

**答案**：`self.memo` 是 Tilus `IRRewriter` 的变量替换表，控制本 Rewriter 对 `iter_var` 的代换；`self.bound_info_simplifier.memo` 是 hidet 化简器自己的替换表，控制它内部对表达式的化简。两者作用于不同层级，必须同步设置成 `int32.zero`，才能让循环体内所有对循环变量的引用一致地变成常量 0。

## 5. 综合实践

把三个 Pass 串起来理解它们的协作。设计任务：**追踪一条「死代码 + 可化简循环」的 IR 经过收尾三连后的变化**。

1. 构造一个最小 `Function`，满足：
   - 含一个 `analyze_scalar` 能推出精确界的变量（例如把某循环 `extent` 经 `assume` 约束为常量，或直接用一个常量 `LetStmt`）。
   - 含一个依赖于该界的 `ForStmt`（让 `bound_aware_simplify` 能把它折叠成 0 或 1 次）。
   - 含一条结果从未被消费的功能指令（让 DCE 能删）。
2. 按 `get_default_passes` 的真实顺序手动应用最后三个 Pass：
   ```python
   from tilus.transforms.bound_aware_simplify import bound_aware_simplify_pass
   from tilus.transforms.scalar_analyze import analyze_scalar_pass
   from tilus.transforms.dead_code_elimination import dead_code_elimination_pass
   f1 = bound_aware_simplify_pass()(prog)
   f2 = analyze_scalar_pass()(f1)
   f3 = dead_code_elimination_pass()(f2)
   ```
3. 在每一步后用 `collect_instructions` 与 `IRPrinter` 打印，记录：哪些循环被 inline 了、哪些指令被删了、`metadata.analysis` 在何时被填充/刷新。
4. 写一段总结：解释为什么顺序必须是「界感知化简 → 标量分析（刷新）→ DCE」，而不是别的顺序。

**参考答案要点**：界感知化简依赖界（来自其内部自调的 `analyze_scalar`），会把可折叠的循环/分支删掉，从而产生新的死代码（被删循环体内的指令、被剪分支里的指令）；紧接着再跑一次 `analyze_scalar` 刷新界（因为 IR 变了）；最后 DCE 才能扫掉这些「因化简而暴露」的死指令。若把 DCE 放最前，就扫不到这些新产生的死代码。

## 6. 本讲小结

- DCE 用「功能指令白名单 `FUNCTIONAL_INST_TYPES`」判定可删性，配合「活跃性不动点传播」逆向追 `output → inputs` 链，连带删除整条死链；副作用指令永不删除，但原子指令的 `output` 会被改写为 `None`。
- 活跃性用 `id(tensor)` 做集合元素（因 Tensor 身份相等），并需扫描 `inst.attributes` 与 `TensorItem*Stmt` 绑定的 `Var` 才不漏判。
- 标量分析用 `ScalarSet`（整除性 + 上下界）抽象域做抽象解释，不动点迭代 + 加宽（`UPDATE_COUNT_LIMIT=10`）保证终止，结果打包成 `Analysis` 挂到 `metadata.analysis`。
- `param2divisibility`（来自 `assume`）与 `block_indices` 是标量分析的两大种子来源，把 u2-l3 的提示与本讲的数值分析串通。
- 界感知化简是 `Analysis` 的消费者，靠「上界 ≤ 下界」之类关系折叠比较、删 0 次循环、inline 1 次循环、传播常量；它自调 `analyze_scalar` 保证界最新。
- 收尾三连的顺序（`bound_aware_simplify → analyze_scalar → dead_code_elimination`）有因果依赖：化简产生新死代码，刷新界后再由 DCE 清扫。

## 7. 下一步学习建议

- **进入后端**：本讲是 Tilus IR 变换的最后一讲。下一单元 U6 转向后端，建议先读 [u6-l1 generate_ir_module：Tilus IR → Hidet IR](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py)，看经过本讲优化后的 IR 如何被 `FunctionCodegen` 翻译成 CUDA C。
- **深入标量分析**：若对抽象解释感兴趣，可继续阅读 `python/tilus/ir/analyzers/scalar_analyzer.py` 中 `ScalarSet` 的 `__floordiv__`/`__mod__`/`__mul__` 实现，体会「上近似」在带符号运算时的取舍，以及 `c_style_div` 为何要模拟 C 的向零取整。
- **延伸到 hidet 层**：`bound_aware_simplify` 复用的 `RuleBasedSimplifier`/`BoundAnalyzer` 来自内嵌的 hidet（`tilus.hidet.transforms.rule_based_simplifier`），可对照阅读理解 Tilus 与 hidet 两层 IR 的分工。
- **动手扩展**：参照 u8-l5，尝试写一个依赖 `metadata.analysis` 的自定义 Pass（如「利用整除性把连续小循环合并」），并按 u5-l1 的方法用 `apply_transforms` 注册测试。
