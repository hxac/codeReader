# Tilus IR 结构：Program/Function/Stmt

## 1. 本讲目标

上一讲（u3-l2）我们看到了 Transpiler 如何把用户 `__call__` 的 Python 代码翻译成一棵「Tilus IR 语句树」。本讲我们停下来，正式认识这棵树本身的「骨架」——它由哪些不可变数据结构组成、字段分别承载什么信息。

学完本讲，你应当能够：

- 说出 `Program`、`Function`、`Metadata`、`Analysis` 四个容器类型的字段构成与各自职责；
- 理解 Tilus IR 为什么选择「不可变（immutable）+ 身份相等（identity equality）」的设计，并掌握 `with_*` 系列「更新即拷贝」方法；
- 识别 `Stmt` 层级中的每一种节点（`SeqStmt`、`ForStmt`、`IfStmt`、`LetStmt`、`InstStmt`、`ThreadGroupStmt` 等），并能预测它们被 printer 打印出来的样子；
- 用 `Function.create` / `SeqStmt` / `InstStmt` 手工拼出一个极简 `Function`，并用 printer 打印它。

## 2. 前置知识

- **frozen dataclass**：Python `@dataclass(frozen=True)` 生成的数据类，实例一旦创建其字段不可重新赋值，是函数式（不可变）数据建模的常用手段。
- **身份相等 vs 结构相等**：身份相等指「两个对象是内存里的同一个对象」（`a is b`）；结构相等指「两个字段值完全一样」（`a == b`）。Tilus 的张量和 IR 节点默认采用身份相等，这点很关键，后面会反复用到。
- **AST / 语句树（statement tree）**：程序的嵌套表示，叶子是单条语句，分支节点用 `SeqStmt`、`ForStmt` 等组织子语句。本讲假设你已经读过 u3-l2，知道 `__call__` 体最终会变成 `Function.body: Stmt`。
- **hidet 的 `Var` / `Expr`**：Tilus IR 的叶子（标量变量与表达式）借用了内嵌 `hidet` 子包的类型，本讲把它们当作「标量符号」即可。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [python/tilus/ir/prog.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/prog.py) | 定义 `Program`，一个程序 = 若干个 `Function` 的映射。 |
| [python/tilus/ir/func.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/func.py) | 定义 `Analysis`、`Metadata`、`Function`，携带网格、线程块与标量分析信息。 |
| [python/tilus/ir/stmt.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/stmt.py) | 定义 `Stmt` 语句树的全部节点。 |
| [python/tilus/ir/node.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/node.py) | `IRNode` 基类，统一身份相等与打印入口。 |
| [python/tilus/ir/utils/frozendict.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/utils/frozendict.py) | `frozendict`：不可变、可哈希的字典，用于 `Program.functions`、`Metadata.param2divisibility` 等。 |
| [python/tilus/ir/tools/printer.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tools/printer.py) | `IRPrinter`：把 IR 树渲染成人类可读文本，本讲实践要用到。 |

## 4. 核心概念与源码讲解

### 4.1 Program / Function / Metadata：不可变 IR 的容器层

#### 4.1.1 概念说明

编译器内部需要一个稳定、可哈希、可比较的中间表示。Tilus 把整个 IR 设计成**不可变的函数式数据结构**：任何「修改」都不会就地改动对象，而是返回一个新对象。这样做有两个直接好处：

1. **可以安全地共享子树**：变换（Pass）在重写一棵大树时，未被触碰的子树会被原样复用，节省内存与时间。
2. **可以作为字典键 / 哈希对象**：u3-l1 提过，缓存键基于「程序文本的哈希」，而能稳定哈希的前提正是结构不可变。

三个容器自上而下的关系是：

```
Program  = frozendict[str, Function]      # 一个程序，若干个函数
Function = name + params + body + metadata # 一个 GPU kernel
Metadata = grid_blocks + cluster_blocks + block_indices + num_warps + param2divisibility + analysis
```

`Program` 是最外层壳，通常只含一个 `Function`（对应一个 kernel）；`Function` 才是真正存放逻辑的地方；`Metadata` 把「这个 kernel 怎么启动」（网格多大、几个 warp、参数整除性）与「标量分析结果」打包在一起，单独成块。

#### 4.1.2 核心流程

构造与「更新」一个容器的统一范式是：

- **创建**：用静态方法 `Xxx.create(...)`，内部把可变入参（list/dict）冻结成不可变结构（tuple / frozendict）。
- **更新**：用 `with_<字段>(...)` 方法，借助 `dataclasses.replace` 复制除指定字段外的全部内容、返回新对象。原对象保持不变。

```text
Program.create({"f": func})            # 普通 dict → frozendict
        .with_function(new_func)        # 复制 + 替换一项，返回新 Program

Function.create(name, params, body, metadata)
         .with_body(new_body)           # 只换 body，其余字段不变
         .with_metadata(new_metadata)
```

`Metadata` 的几个字段含义（启动一个 CUDA kernel 所需的全部信息都浓缩在这里）：

| 字段 | 类型 | 含义 |
|------|------|------|
| `grid_blocks` | `(Expr, Expr, Expr)` | 网格维度（线程块数），是关于参数的表达式，如 `cdiv(M, 64)`。 |
| `cluster_blocks` | `(int, int, int)` | 线程块簇（cluster）维度，Hopper+ 的协作线程块组。 |
| `block_indices` | `(Var, Var, Var)` | 对应 `blockIdx.x/y/z` 的三个 hidet `Var`。 |
| `num_warps` | `int` | 每个线程块的 warp 数（编译期常量）。 |
| `param2divisibility` | `frozendict[Var,int]` | 来自 `self.assume` 的参数整除性提示（u2-l3）。 |
| `analysis` | `Optional[Analysis]` | 标量分析产物：每个 `Var` 的整除性、下界、上界（u5-l3）。 |

其中 `Analysis` 又是三个 `frozendict[Var, int]` 的打包，由 `scalar_analyze` Pass 填充，供后续的界感知化简使用。

#### 4.1.3 源码精读

**Program** 只有两个字段和一个 `frozendict`：

[python/tilus/ir/prog.py:L24-L35](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/prog.py#L24-L35) — `Program` 是 `IRNode` 的 frozen dataclass，`functions` 为 `frozendict[str, Function]`；`create` 把普通 dict 冻结，`with_function` 追加/替换一个函数后返回新 Program。注意 `eq=False`（见 4.2.3 对基类的解释）。

`frozendict` 是一个继承自 `dict` 的不可变字典——它把所有写方法（`__setitem__`/`update`/`pop`/`clear`…）全部改成抛 `TypeError`，并实现 `__hash__`：

[python/tilus/ir/utils/frozendict.py:L21-L54](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/utils/frozendict.py#L21-L54) — 写操作被拦截抛错；`__hash__` 用 `hash(tuple(sorted(self.items())))`，因此 `frozendict` 可作为字典键与集合元素，这正是它能进入缓存键哈希的前提。

**Metadata** 与 **Analysis**：

[python/tilus/ir/func.py:L27-L41](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/func.py#L27-L41) — `Analysis` 把 divisibility / lower_bound / upper_bound 三个映射冻结打包，`empty()` 提供空分析。

[python/tilus/ir/func.py:L44-L80](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/func.py#L44-L80) — `Metadata.create` 用断言保证三个三元组长度为 3，`with_analysis` / `with_grid_blocks` / `with_param2divisibility` 用 `dataclasses.replace` 做不可变更新。

**Function**：

[python/tilus/ir/func.py:L83-L111](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/func.py#L83-L111) — `Function` 四字段 `name/params/body/metadata`；`params` 冻结成 `tuple[Var, ...]`；三个 `with_*` 方法分别替换 body、name、metadata。

> 小贴士：`Metadata` 用的是 `@dataclass(frozen=True)`（**没有** `eq=False`），所以两个内容相同的 Metadata 是**结构相等**的；而 `Program`/`Function` 带了 `eq=False`，走身份相等。这是项目里有意的区分：元数据可以按值复用，函数体则按身份跟踪。

#### 4.1.4 代码实践

**目标**：用 3 行 Python 验证 `Program`/`Function` 的「不可变 + 身份相等」语义。

**步骤**（直接 `python` 交互式运行即可）：

```python
# 示例代码
from tilus.ir.func import Function, Metadata
from tilus.hidet.ir.expr import Var, as_expr
from tilus.hidet.ir.primitives.cuda.vars import blockIdx
from tilus.hidet.ir.type import PointerType, scalar_type
from tilus.ir.stmt import SeqStmt
from tilus.ir.prog import Program

meta = Metadata.create(
    grid_blocks=[as_expr(1), as_expr(1), as_expr(1)],
    cluster_blocks=[1, 1, 1],
    block_indices=[blockIdx.x, blockIdx.y, blockIdx.z],
    num_warps=1,
)
p = Var("p", PointerType(scalar_type("float32")))
f = Function.create(name="k", params=[p], body=SeqStmt.create([]), metadata=meta)
prog1 = Program.create({"k": f})
prog2 = prog1.with_function(f)   # “更新”：返回新对象，原对象不变

print(prog1 is prog2)            # 期望 False —— 不可变更新产生新对象
print(f == f)                    # 期望 True（身份相等，和自己比）
print("a" == "a")                # 对照：字符串是结构相等
```

**观察现象**：`prog1 is prog2` 为 `False`，说明 `with_function` 没有就地改 `prog1`，而是新建了 `Program`。

**预期结果**：依次打印 `False`、`True`、`True`。如对 `f == f` 改成 `f == 另一个内容相同的 Function`，由于 `eq=False` 走身份判断，结果会是 `False`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Metadata` 不加 `eq=False`，而 `Program`/`Function` 要加？

**答案**：`Metadata` 是纯数据快照（网格大小、整除性等），按值比较更自然、也方便在不同函数间复用；而 `Function`/`Program` 在变换过程中会被频繁复制、共享，用身份相等能让变换器（`IRRewriter`）的记忆化字典（memo）按对象身份精确命中，避免「两个内容相同但本应区分的函数」被误判为同一个。`IRNode` 基类统一把 `__eq__`/`__hash__` 定义为身份（见 4.2.3），`eq=False` 就是关闭 dataclass 自动生成的结构相等版本，避免它覆盖基类行为。

**练习 2**：`with_body` 会不会改变原 `Function`？

**答案**：不会。它内部调用 `dataclasses.replace(self, body=new_body)`，复制其余字段后返回**新的** `Function`；原对象的 `body` 仍指向旧语句树。

---

### 4.2 Stmt 层级：语句树的所有节点

#### 4.2.1 概念说明

`Function.body` 是一棵 `Stmt`（statement）树。`Stmt` 是所有语句节点的抽象基类，下面派生出一组 frozen dataclass，对应你能在一门编程语言里写出的所有「语句」：顺序、循环、分支、声明、赋值、返回、求值，外加两类 Tilus 特有的——`InstStmt`（包裹一条张量指令）与 `ThreadGroupStmt`（收窄执行线程）。

可以把它理解成一个「精简版的 Python AST」，但有两点不同：

1. 它是**不可变**的（和容器层一样）；
2. 它专门为 GPU kernel 服务——`ForStmt` 带了 `unroll_factor`，`ThreadGroupStmt` 直接表达「这一段只由哪几个线程执行」。

#### 4.2.2 核心流程

下面这张表把全部 `Stmt` 子类按用途分组，给出字段与对应的「源代码样子」（即 printer 渲染结果）：

| 分组 | Stmt | 关键字段 | printer 渲染 |
|------|------|----------|--------------|
| 结构 | `SeqStmt` | `seq: tuple[Stmt,...]` | 多条语句顺序排列 |
| 循环 | `ForStmt` | `iter_var, extent, body, unroll_factor` | `#pragma unroll` + `for v in range(ext):` |
| 分支 | `IfStmt` | `cond, then_body, else_body` | `if cond: ... else: ...` |
| 分支 | `WhileStmt` | `cond, body` | `while cond: ...` |
| 跳转 | `BreakStmt` / `ReturnStmt` | — | `break` / `return` |
| 标量 | `DeclareStmt` | `var, init` | `declare %v: type = init` |
| 标量 | `AssignStmt` | `var, value` | `%v = value` |
| 标量 | `LetStmt` | `bind_vars, bind_values, body` | `let ... = ...` 一段作用域 |
| 标量 | `EvaluateStmt` | `expr, pred` | 只为副作用求值一个表达式 |
| 张量-标量桥接 | `TensorItemPtrStmt` / `TensorItemValueStmt` | `tensor, ptr_var/var, space` | 把张量绑定成 hidet 标量 Var |
| 指令 | `InstStmt` | `inst: Instruction` | 一条张量指令（u3-l4 详讲） |

执行（想象）流程：从 `body`（通常是一个 `SeqStmt`）出发，自上而下、自左向右遍历；遇到 `ForStmt`/`IfStmt` 就递归进入其 `body`；遇到 `InstStmt` 就执行其 `inst`；遇到 `ThreadGroupStmt` 就把内部 `body` 的执行权交给指定线程段。

`ForStmt.extent` 是循环上界（hidet `Expr`），`unroll_factor` 是个提示（不是强制），取值约定见源码注释：`None` 不标注、`-1` 全展开、`n≥1` 按 n 展开——这与 u2-l3 里 `self.range(unroll=...)` 的取值一一对应。

#### 4.2.3 源码精读

所有 `Stmt` 都继承自同一个基类，而基类又继承自 `IRNode`：

[python/tilus/ir/node.py:L18-L31](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/node.py#L18-L31) — `IRNode` 统一了三件事：`__hash__` 返回 `id(self)`、`__eq__` 判 `self is other`（身份相等），`__str__` 进入当前 `PrintContext` 调用 printer。所以**任何 IR 节点都按身份比较、都可哈希**，这是变换器记忆化的根基。

`Stmt` 基类本身只是个空标记：

[python/tilus/ir/stmt.py:L26-L28](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/stmt.py#L26-L28) — `Stmt` 是带 `eq=False` 的 frozen dataclass，本身无字段，纯粹用于类型约束「函数体必须是 Stmt」。

典型的「容器型」语句 `SeqStmt` 与循环 `ForStmt`：

[python/tilus/ir/stmt.py:L31-L50](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/stmt.py#L31-L50) — `SeqStmt.create` 把列表冻结成 tuple；`ForStmt` 四字段中 `unroll_factor` 的取值约定写在注释里（`None`/`-1`/`n`）。

printer 如何把 `ForStmt` 渲染回带 `#pragma unroll` 的代码：

[python/tilus/ir/tools/printer.py:L256-L265](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tools/printer.py#L256-L265) — 依据 `unroll_factor` 决定打印 `#pragma unroll` 还是 `#pragma unroll N`，随后打印 `for v in range(ext):` 并缩进 body。

一个便利的拼接函数 `seq_stmt`：

[python/tilus/ir/stmt.py:L171-L176](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/stmt.py#L171-L176) — `seq_stmt` 能直接吃一串 `Instruction`（自动包成 `InstStmt`），且当只有一个元素时**直接返回该 Stmt 而不裹一层 `SeqStmt`**，避免多余嵌套——很多变换的输出都靠它收尾。

#### 4.2.4 代码实践（源码阅读型）

**目标**：把「Stmt 字段」与「printer 渲染」对应起来，训练「看到 IR 就能脑补出打印文本」的直觉。

**步骤**：

1. 打开 [python/tilus/ir/tools/printer.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tools/printer.py) 的 `visit_ForStmt`（L256）、`visit_IfStmt`（L284）、`visit_LetStmt`（L314）、`visit_SeqStmt`（L250）四个方法。
2. 对照 [python/tilus/ir/stmt.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/stmt.py) 里这四种 Stmt 的字段定义。
3. 在纸上手写下面这段伪 IR 被 printer 渲染后的样子（假设 `cond = %v0`，body 是一条 `EvaluateStmt`）：

```text
SeqStmt([
    ForStmt(iter=i, extent=8, body=EvaluateStmt(expr=i*2), unroll_factor=-1),
    IfStmt(cond=%v0, then_body=EvaluateStmt(expr=1), else_body=None),
])
```

**需要观察的现象**：`ForStmt` 上方是否出现 `#pragma unroll`；`IfStmt` 没有 else 时是否打印 `else:`。

**预期结果**：

```python
#pragma unroll
for %v0 in range(8):
    (%v0 * 2)
if %v1:
    1
```

（变量编号 `%v0/%v1` 由 printer 按访问顺序自动分配，实际数字可能不同；缩进 4 空格。）精确空白格式可待本地用 4.3.4 的方法打印验证。

#### 4.2.5 小练习与答案

**练习 1**：`seq_stmt([a, b])` 与 `SeqStmt.create([a, b])`（其中 a、b 是 Stmt）结果一样吗？当只传一个元素时呢？

**答案**：传两个及以上元素时结果结构相同（都是 `SeqStmt`）；但**只传一个元素时不同**——`seq_stmt` 会直接返回那个 Stmt，而 `SeqStmt.create` 仍会包一层单元素 `SeqStmt`。所以「能不套就不套」时用 `seq_stmt`。

**练习 2**：`EvaluateStmt` 里的 `pred` 字段是做什么用的？

**答案**：`pred` 是一个可选的布尔表达式，充当**运行时谓词（guard）**——只有 `pred` 为真的线程才执行该表达式求值。它常被代码生成器用来实现「部分线程参与」的副作用表达式，是线程级控制流的低层表达。

---

### 4.3 InstStmt 与 ThreadGroupStmt：两条核心语句

#### 4.3.1 概念说明

`Stmt` 树里有两类节点是 Tilus 区别于普通语言 AST 的关键，它们直接服务于「tile-level + 张量」编程模型：

- **`InstStmt`**：把一条张量指令（`Instruction`，u3-l4 详讲）挂进语句树。它是叶子节点，不嵌套子 Stmt，却承载了 99% 的真实计算（load/dot/store/mma/tma…）。可以说「`Function` 的 body 是一棵 `Stmt` 树，叶子几乎全是 `InstStmt`」。
- **`ThreadGroupStmt`**：把一段 `body` 的执行权收窄到「一段连续的线程」。这是 u2-l3 讲过的 `with self.thread_group(begin, n)` / `single_thread()` / `single_warp()` 在 IR 层的落脚点。

#### 4.3.2 核心流程

`InstStmt` 与指令的关系：

```text
Instruction            ← 纯数据：output + inputs + attributes（如 offsets, op, ptr）
    │
    └─ InstStmt(inst)  ← 把它包成 Stmt，挂进语句树（顺序、循环、分支里）
```

当 printer 遇到 `InstStmt`，它先 `visit(stmt.inst)`，由 `visit_Instruction` 负责把 `output = InstName(inputs, attr=val, ...)` 渲染出来；指令名是类名去掉 `Inst` 后缀（如 `AddInst` → `Add`）。

`ThreadGroupStmt` 的线程语义：

- `thread_begin`：子组首线程相对**父线程组**的偏移。
- `num_threads`：执行 body 的线程数。
- 特殊值 `thread_begin == -1`（**elect-any**）：硬件可自由挑选任意一段连续且自然对齐的线程。`num_threads=1` 等价于 `elect.sync`（任选 1 个线程），`num_threads=32` 等价于「任选 1 个 warp」。这种模式让后端能用统一寄存器/统一谓词选线程，避免分支发散。

`thread_begin` 是**相对父组**的，因此 `ThreadGroupStmt` 可以嵌套——内层的绝对线程号 = 沿父链累加（u2-l3 的 `ThreadGroupStack`）。

#### 4.3.3 源码精读

`InstStmt` 极其简单，仅一个字段：

[python/tilus/ir/stmt.py:L166-L168](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/stmt.py#L166-L168) — `InstStmt` 只持有 `inst: Instruction`。注意它**不**自己存 output/inputs，那些都在 `Instruction` 内部。

`Instruction` 如何暴露字段（为下一讲 u3-l4 做铺垫）：

[python/tilus/ir/inst.py:L24-L96](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/inst.py#L24-L96) — `Instruction` 固定字段为 `output: Optional[Tensor]` 与 `inputs: tuple[Tensor,...]`；`attributes` 是个 property，遍历 `__dict__` 排除 `output/inputs` 后返回其余字段（如 `offsets`、`op`、`ptr`）。这是「所有指令统一接口」的根基。

printer 如何分别渲染 `InstStmt` 与 `Instruction`：

[python/tilus/ir/tools/printer.py:L247-L248](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tools/printer.py#L247-L248) — `visit_InstStmt` 直接委托给 `visit(stmt.inst)`。

[python/tilus/ir/tools/printer.py:L360-L398](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tools/printer.py#L360-L398) — `visit_Instruction` 渲染 `输出 = 指令名(输入..., 属性=值...)`，行尾还附上输出张量类型的注释。指令名由 `__class__.__name__.removesuffix("Inst")` 得到。

`ThreadGroupStmt` 的字段与详细 docstring（含 elect-any 语义）：

[python/tilus/ir/stmt.py:L53-L92](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/stmt.py#L53-L92) — docstring 解释了 `thread_begin=-1` 时硬件可任选对齐线程段，`num_threads=1` 即 `elect.sync`、`num_threads=32` 即「统一谓词选一个 warp」，并强调需为 2 的幂。

printer 把 elect-any 渲染成 `elect_any`：

[python/tilus/ir/tools/printer.py:L267-L282](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tools/printer.py#L267-L282) — `thread_begin == -1` 时打印 `thread_begin=elect_any`，否则打印具体数字。

#### 4.3.4 代码实践（源码阅读型）

**目标**：在真实编译产物里统计 `InstStmt` 数量，感受「body 的叶子几乎全是 InstStmt」。

**步骤**：

1. 选一个你已经能跑通的内核（如 u1-l3 的 vector_add）。在脚本开头加上：
   ```python
   import tilus
   tilus.option.cache_dir("/tmp/tilus-ir-lab")
   tilus.option.debug.dump_ir()          # 导出每个 Pass 后的 IR（见 u3-l1）
   ```
2. 运行内核一次，打开缓存目录 `/tmp/tilus-ir-lab/.../ir/` 下**最早期**（如 `00_declare_to_let.txt` 或最接近转译输出的那份）的 IR 文件。
3. 肉眼数一下：整个 `def ...(...):` 函数体里，有多少行形如 `输出 = 指令名(...)` 的指令语句（即 `InstStmt`），有多少行是 `for`/`if`/`let`（结构性 Stmt）。

**需要观察的现象**：load/store/cast/add 这类指令行（`InstStmt`）的数量远多于 `for`/`if`/`let`，验证「叶子几乎全是 InstStmt」。

**预期结果**：例如 vector_add 中应能看到 `GlobalView`、`LoadGlobal`、`Add`、`Cast`、`StoreGlobalGeneric` 等若干指令行；`with thread_group(...)`/`for` 等结构性语句数量很少。具体指令种类与行数取决于内核，若你的环境为 compile-only（无 GPU），dump_ir 仍可落盘，可直接阅读 `ir/` 目录里的文本，无需真正 launch。

#### 4.3.5 小练习与答案

**练习 1**：`InstStmt` 为什么不直接把 `output`/`inputs` 放在自己身上，而要包一层 `Instruction`？

**答案**：为了让「语句结构」与「指令语义」解耦。`Stmt` 树只关心顺序、循环、分支、线程划分等**控制结构**；具体「这条指令算什么、读写哪些张量、有哪些属性」全部封装在 `Instruction` 子类里。这样变换器（`IRRewriter`）可以用一套通用的 `visit_InstStmt` → `visit_Instruction` 机制处理所有指令，新增指令类型时不必改动 Stmt 层。

**练习 2**：`ThreadGroupStmt(thread_begin=-1, num_threads=32)` 表达的是什么执行模式？为什么比 `thread_begin=0` 更好？

**答案**：表达「从父组里任选一个 warp 来执行 body」（elect-any）。相比硬编码 `thread_begin=0`（只能由 0 号 warp 执行，需要 `threadIdx/32==0` 这种发散分支来判断），elect-any 允许硬件用**统一谓词**挑任意一个 warp，避免分支发散、提升效率。这正是 `single_warp()` 默认走 elect-any 的原因。

---

## 5. 综合实践

把本讲的容器层、Stmt 层、`InstStmt` 串起来：**手工拼出一个极简的 `Function`（两个寄存器相加，结果写回全局指针），并用 printer 打印它**。这个练习直接复刻项目测试 `tests/transforms/test_dead_code_elimination.py` 里构造 IR 的范式，是最可靠的「从零搭建 IR」练手方式。

**目标**：不写任何 `tilus.Script`、不经过转译器，纯靠 `Function.create` / `SeqStmt` / `InstStmt` 与真实指令的 `create()` 拼出一个 `Function`，再用 `print_context()` 打印验证。

**操作步骤**：把下面这段「示例代码」存成 `build_ir.py` 并运行（无需 GPU，全程在 Python 层完成）：

```python
# 示例代码：手工构造一个 add 内核的 Function 并打印
from tilus.hidet.ir.dtypes import float32
from tilus.hidet.ir.expr import Var, as_expr
from tilus.hidet.ir.primitives.cuda.vars import blockIdx
from tilus.hidet.ir.type import PointerType
from tilus.ir.func import Function, Metadata
from tilus.ir.instructions.generic import (
    AllocateRegisterInst,
    AddInst,
    StoreGlobalGenericInst,
)
from tilus.ir.stmt import InstStmt, SeqStmt
from tilus.ir.tensor import RegisterTensor
from tilus.ir.tools.printer import print_context

# 1) 两个寄存器输入：用 AllocateRegisterInst 分配并初始化为 0
alloc_a = AllocateRegisterInst.create(
    output=RegisterTensor.create(dtype=float32, shape=(4,)),
    f_init=lambda _: float32.zero,
)
a = alloc_a.output
alloc_b = AllocateRegisterInst.create(
    output=RegisterTensor.create(dtype=float32, shape=(4,)),
    f_init=lambda _: float32.zero,
)
b = alloc_b.output

# 2) 加法指令：a + b -> acc
acc = RegisterTensor.create(dtype=float32, shape=(4,))
add = AddInst.create(a, b, acc)

# 3) 把 acc 存回全局指针 p（偏移 0）
p = Var("p", PointerType(float32))
store = StoreGlobalGenericInst.create(x=acc, ptr=p, f_offset=lambda _: 0)

# 4) 三条指令包成 InstStmt，串成 body
body = SeqStmt.create([InstStmt(alloc_a), InstStmt(alloc_b), InstStmt(add), InstStmt(store)])

# 5) Metadata：单线程块、单 warp、(1,1,1) cluster
metadata = Metadata.create(
    grid_blocks=[as_expr(1), as_expr(1), as_expr(1)],
    cluster_blocks=[1, 1, 1],
    block_indices=[blockIdx.x, blockIdx.y, blockIdx.z],
    num_warps=1,
)

# 6) 组装 Function
func = Function.create(name="add_kernel", params=[p], body=body, metadata=metadata)

# 7) 必须进入 PrintContext 才能给张量/变量自动命名
with print_context():
    print(func)
```

**需要观察的现象**：打印输出应包含三段——函数签名 `def add_kernel(p: ~float32):`、以 `#` 注释形式呈现的 `Metadata`（grid_blocks/cluster_blocks/num_warps），以及函数体里的若干 `%rN = Allocate/...`、`%rN = Add(...)`、`StoreGlobalGeneric(...)` 指令行。

**预期结果**：你能看到一条 `Add(%r0, %r1)  # register, float32[4], ...` 形式的行，这正是 `visit_Instruction` 在行尾附上的输出张量类型注释；同时 `Metadata` 被渲染成注释块。这证明你手工拼出的 IR 与转译器产出的 IR 在结构上完全同构——后者只是规模更大而已。

> 备注：此构造直接对应 `tests/transforms/test_dead_code_elimination.py` 中的 `_make_function` / `_alloc` / `_store` 辅助函数，可确信其 API 用法准确。若你对某个指令的 `create` 签名有疑问，最权威的参照就是该测试文件与 `python/tilus/ir/instructions/generic.py`。

**进阶（可选）**：把这个 `func` 包进 `Program.create({"add_kernel": func})`，然后调用 `tilus.ir.tools.verify`（u3-l5）校验其合法性，观察 verify 是否对「acc 被 store 消费、a/b 被分配」这类数据流提出要求。

## 6. 本讲小结

- Tilus IR 是一套**不可变**的 frozen dataclass：`Program = frozendict[str, Function]`，`Function = name + params + body + metadata`，所有「修改」都通过 `with_*` 方法返回新对象。
- **身份相等**（`IRNode` 的 `__eq__`/`__hash__` 基于 `id`）是 IR 节点与张量的默认语义，是变换器记忆化字典的根基；`frozendict` 则提供了「可哈希的不可变映射」。
- `Metadata` 浓缩了启动 kernel 的全部信息（`grid_blocks`/`cluster_blocks`/`block_indices`/`num_warps`/`param2divisibility`/`analysis`），`Analysis` 是标量分析（整除性/上下界）的产物。
- `Stmt` 是一棵精简语句树：结构（`SeqStmt`）、循环（`ForStmt` 带 `unroll_factor`）、分支（`IfStmt`/`WhileStmt`）、标量（`DeclareStmt`/`AssignStmt`/`LetStmt`/`EvaluateStmt`）、张量-标量桥接（`TensorItem*Stmt`）。
- 两条 Tilus 特色语句：`InstStmt(inst)` 把张量指令挂进树（叶子几乎全是它），`ThreadGroupStmt` 收窄执行线程（`thread_begin=-1` 即 elect-any）。
- `str(node)` 会进入 `PrintContext` 调用 `IRPrinter`，把任意 IR 节点渲染成可读文本——这是读 IR、写变换、做实践时最常用的工具。

## 7. 下一步学习建议

- **下一讲 u3-l4（Instruction 与 Tensor）** 会钻进 `Instruction` 内部：`output/inputs/attributes` 的精确语义、功能指令与副作用指令的白名单区分、四种 `Tensor`（Register/Shared/Global/TMemory）的身份相等语义，与本讲的 `InstStmt` 无缝衔接。
- **u3-l5（IR 工具：验证、打印与收集）** 会系统讲 `verify`/`printer`/`collect_instructions`，本讲实践里出现的 `print_context()`、`Metadata.create` 的断言等都将在那里得到完整解释。
- 想立刻看到「真实」的大 IR：按 u3-l1 的方法对一个 matmul 开启 `debug.dump_ir()`，到缓存目录 `ir/` 下阅读完整的 `Function` 文本，对照本讲的 Stmt 表逐行辨认节点类型。
