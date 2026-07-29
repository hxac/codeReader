# 讲义：Pass 框架与 IRRewriter/IRVisitor

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 Tilus 的「变换（Pass）」是如何被组织、串联与执行的，并能解释 `Pass` / `PassContext` / `apply_transforms` 三者的分工。
- 理解 `IRRewriter`（改写）与 `IRVisitor`（只读遍历）这两套访问者模式的工作原理，特别是「`visit_*` 按类型分派」和「基于身份（identity）的记忆化」两大机制。
- 识别访问者模式的常见陷阱：为什么 `visit_Expr` 在只读遍历里不下钻、为什么指令的 `attributes` 也必须被扫描、为什么记忆化会阻止重复访问。
- 自己动手写一个 `IRVisitor` 子类，统计一棵真实 IR 树里 `ForStmt` 的数量。

本讲是 [u3-l5 IR 工具：验证、打印与收集](u3-l5-ir-tools-and-verification.md) 的直接延续：那一讲的 `collect` / `collect_instructions` 就是建立在 `IRVisitor` 之上的。本讲把镜头从「工具」拉到「框架」，为下一讲 [u5-l2 默认变换流水线](u5-l2-default-transform-pipeline.md) 打基础——所有的默认 Pass 都跑在本讲讲解的这套框架上。

## 2. 前置知识

阅读本讲前，建议你已经掌握以下概念（前三讲的结论）：

- **Tilus IR 是不可变的**：`Program / Function / Stmt / Instruction / Tensor` 全部是 frozen dataclass，任何「修改」都返回一个新对象（详见 u3-l3）。
- **身份相等（identity equality）**：IR 节点的 `__eq__` / `__hash__` 等价于 Python 的 `is`（基于 `id`）。判断两个对象「是否同一个」用 `is` 而不是 `==`（详见 u3-l4）。这是本讲的关键——变换器全程用 `is` 比较新旧子树。
- **编译流水线**：`drivers.build_program` 把一段 `__call__` 编译成 `.so`，其中高层优化阶段 `optimize_program` 会顺序运行一组 Pass（详见 u3-l1）。
- **InstStmt**：把一条 `Instruction` 挂进语句树的叶子节点（详见 u3-l3、u3-l4）。

如果你对「编译器里的 Pass 是什么」完全陌生，可以这样理解：**Pass 就是一个「输入一个程序、输出一个（通常更好的）程序」的纯函数**。把很多个 Pass 串起来，就是一条「变换流水线」。

> 小贴士：本讲会反复出现 `memo`（记忆化字典）这个词。它不是缓存优化，而是访问者模式的核心数据结构——既用来「避免重复访问同一节点」，也被改写器当作「变量替换表」来用。这一点是理解 Tilus 变换器的钥匙。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [`python/tilus/transforms/base.py`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/base.py) | 定义 `Pass`、`PassContext`、`apply_transforms`，是整个变换框架的骨架。 |
| [`python/tilus/ir/functors/functor.py`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/functors/functor.py) | 定义 `IRFunctor`（基类）、`IRRewriter`（改写）、`IRVisitor`（只读遍历）三套访问者。 |
| [`python/tilus/transforms/instruments/instrument.py`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/instruments/instrument.py) | `PassInstrument`：只观察、不改动 IR 的「仪器」基类。 |
| [`python/tilus/transforms/instruments/dump_ir.py`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/instruments/dump_ir.py) | `DumpIRInstrument`：把每个 Pass 之后的 IR 落盘的仪器。 |
| [`python/tilus/transforms/let_propogation.py`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/let_propogation.py) | 一个具体的 Pass（`LetPropogationPass`），演示 `IRRewriter` 的典型写法。 |
| [`python/tilus/ir/tools/instruction_collector.py`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tools/instruction_collector.py) | `InstructionCollector`：一个最小的 `IRVisitor` 子类，演示只读遍历。 |
| [`python/tilus/drivers.py`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py) | `optimize_program`：把 Pass 与 PassContext 真正组装起来的地方。 |
| `CLAUDE.md` | 项目维护笔记，列出了访问者模式的常见陷阱。 |

---

## 4. 核心概念与源码讲解

### 4.1 Pass / PassContext / apply_transforms 框架

#### 4.1.1 概念说明

Tilus 的高层优化阶段会把一组「变换」顺序应用到 `Program` 上。这套机制围绕三个角色展开：

- **`Pass`**：一个变换。输入 `Program`，输出 `Program`。它知道如何处理单个 `Function`，框架负责遍历程序里的所有函数。
- **`PassContext`**：一次「变换运行」的上下文。它持有若干「仪器（Instrument）」——这些仪器在每个 Pass 执行前后被通知，但**只观察、不改动 IR**。典型的仪器是「把 IR 转储到磁盘」的 `DumpIRInstrument`。
- **`apply_transforms`**：把上面两者串起来的函数。它按顺序运行每个 Pass，并在每个 Pass 前后回调上下文里的仪器。

一句话总结三者的关系：**`Pass` 负责「改」，`PassContext` 负责「看」，`apply_transforms` 负责「把改和看按顺序串起来」。**

#### 4.1.2 核心流程

`apply_transforms` 的执行流程如下（伪代码）：

```
ctx = PassContext.current()              # 取当前上下文（没有就建一个默认的）
ctx.before_all_passes(prog)              # 通知所有仪器：整个流水线开始
for transform in transforms:             # 顺序遍历每个 Pass
    ctx.before_pass(transform.name, prog)# 通知仪器：某个 Pass 即将开始
    prog = transform(prog)               # 运行 Pass，得到新 Program
    ctx.after_pass(transform.name, prog) # 通知仪器：某个 Pass 结束
ctx.after_all_passes(prog)               # 通知仪器：整个流水线结束
return prog
```

关键点是「仪器只在 Pass 之间被通知」。这正是 `DumpIRInstrument` 能逐 Pass 导出 IR 的原理：它在 `after_pass` 里把当前 `prog` 打印成文本写盘。

#### 4.1.3 源码精读

先看 `Pass` 基类（[python/tilus/transforms/base.py:68-83](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/base.py#L68-L83)）：

```python
class Pass:
    def __init__(self) -> None:
        self.name: str = self.__class__.__name__.removesuffix("Pass")

    def __call__(self, prog: Program) -> Program:
        return self.process_program(prog)

    def process_program(self, program: Program) -> Program:
        functions = {name: self.process_function(func) for name, func in program.functions.items()}
        if all(a is b for a, b in zip(functions.values(), program.functions.values())):
            return program          # 所有函数都没变 → 原样返回
        else:
            return Program.create(functions)

    def process_function(self, function: Function) -> Function:
        raise NotImplementedError()
```

要点：

- `name` 自动从类名去掉 `"Pass"` 后缀得到（如 `LetPropogationPass` → `LetPropogation`），用于仪器日志的文件名。
- `process_program` 遍历每个函数调用 `process_function`（子类必须实现）。**它有一个重要的短路优化**：如果处理后的每个函数 `is` 原函数（即没有任何函数被实际改动），就直接返回原 `program`，避免构造无意义的新对象。这正是身份相等的用武之地。
- 子类只需重写 `process_function`，不必关心 `Program` 层面的包装。

再看 `PassContext`（[python/tilus/transforms/base.py:25-65](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/base.py#L25-L65)），它用类变量 `_ctx` 维护「当前上下文」，并通过 `with` 语句进出：

```python
class PassContext:
    _ctx = None

    def __enter__(self):
        assert PassContext._ctx is None      # 同一时刻只能有一个活跃上下文
        PassContext._ctx = self
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        ...
        PassContext._ctx = None

    def dump_ir(self, dump_dir: Path) -> None:
        self.instruments.append(DumpIRInstrument(dump_dir))

    @staticmethod
    def current() -> PassContext:
        if PassContext._ctx is None:
            return PassContext()        # 没有活跃上下文时给一个空的默认上下文
        else:
            return PassContext._ctx
```

`PassContext.current()` 是「取当前上下文」的入口：在 `with PassContext()` 块内返回那个上下文，在块外返回一个空的默认上下文（仪器列表为空）。四个钩子方法 `before_all_passes / before_pass / after_pass / after_all_passes` 只是依次回调 `instruments` 里每个仪器对应的方法。

`apply_transforms` 本身很薄（[python/tilus/transforms/base.py:86-112](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/base.py#L86-L112)）：

```python
def apply_transforms(prog: Program, transforms: Sequence[Pass]) -> Program:
    ctx = PassContext.current()
    ctx.before_all_passes(prog)
    for transform in transforms:
        ctx.before_pass(transform.name, prog)
        prog = transform(prog)
        ctx.after_pass(transform.name, prog)
    ctx.after_all_passes(prog)
    return prog
```

「仪器」基类 `PassInstrument` 是四个空方法（[python/tilus/transforms/instruments/instrument.py:18-29](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/instruments/instrument.py#L18-L29)）：

```python
class PassInstrument:
    def before_all_passes(self, program: Program) -> None: pass
    def before_pass(self, pass_name: str, program: Program) -> None: pass
    def after_pass(self, pass_name: str, program: Program) -> None: pass
    def after_all_passes(self, program: Program) -> None: pass
```

最典型的仪器是 `DumpIRInstrument`（[python/tilus/transforms/instruments/dump_ir.py:53-63](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/instruments/dump_ir.py#L53-L63)）：它在 `after_pass` 里把当前程序打印出来写到 `<count>_<pass_name>.txt`。这就是「逐 Pass 导出 IR」功能的实现。

最后看这套框架如何被组装进编译流水线——`optimize_program`（[python/tilus/drivers.py:82-93](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py#L82-L93)）：

```python
transforms: list[Pass] = get_default_passes()        # 取默认 Pass 列表
...
with PassContext() as ctx:                           # 进入上下文
    if tilus.option.get_option("debug.dump_ir"):     # 若开启了 dump_ir 选项
        ctx.dump_ir(cache_dir / "ir")                # 给上下文挂上转储仪器
    return apply_transforms(program, transforms)     # 运行流水线
```

这段代码把三个角色一次性接好：`get_default_passes()` 提供「改」，`with PassContext()` 圈定作用域，`apply_transforms` 负责执行并在中间穿插「看」（`dump_ir` 选项决定是否转储）。

#### 4.1.4 代码实践

**实践目标**：亲手用 `Pass` / `PassContext` / `apply_transforms` 跑一个最小变换，并验证仪器的回调时机。

**操作步骤**（示例代码，运行需已安装 tilus）：

```python
# 示例代码：一个「什么都不改」的空 Pass + 一个记录回调的仪器
from tilus.transforms.base import Pass, PassContext, apply_transforms
from tilus.transforms.instruments.instrument import PassInstrument

class NopPass(Pass):
    def process_function(self, function):
        return function            # 原样返回

class TraceInstrument(PassInstrument):
    def before_all_passes(self, program):  print("  [trace] before_all_passes")
    def after_all_passes(self, program):   print("  [trace] after_all_passes")
    def before_pass(self, name, program):  print(f"  [trace] before_pass: {name}")
    def after_pass(self, name, program):   print(f"  [trace] after_pass:  {name}")

# prog 需要是一个真实 Program，可用 u3-l5 的方式构造一个最小 Function
# prog = Program.create({"demo": make_minimal_function()})

with PassContext() as ctx:
    ctx.instruments.append(TraceInstrument())
    apply_transforms(prog, [NopPass(), NopPass()])
```

**需要观察的现象**：`before_all_passes` 只打印一次，随后每个 `Nop` 各打印一对 `before_pass` / `after_pass`，最后 `after_all_passes` 打印一次。

**预期结果**：调用顺序严格为「首 → (pass1 前, pass1 后) → (pass2 前, pass2 后) → 尾」。因为 `NopPass.process_function` 原样返回，`process_program` 的身份短路会让最终返回的 `prog is 原prog`。

> 说明：上面省略了构造 `prog` 的细节。最简单的做法是参考 `tests/transforms/test_dead_code_elimination.py` 里的 `_make_program(...)`（[tests/transforms/test_dead_code_elimination.py:64-66](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/tests/transforms/test_dead_code_elimination.py#L64-L66)），用它造一个最小 `Program` 即可。本实践的精确打印次序「待本地验证」（取决于你构造的具体 prog）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `process_program` 里要用 `all(a is b ...)` 而不是 `all(a == b ...)`？

> **答案**：Tilus IR 节点采用身份相等（`__eq__` 即 `is`，见 u3-l4），二者在这里结果一致；但语义上「函数有没有被改动」指的是「是不是同一个对象」，用 `is` 表意更准确，也避免触发任何被重载过的 `__eq__`。

**练习 2**：如果不进入 `with PassContext()` 块，直接调用 `apply_transforms` 会怎样？

> **答案**：`PassContext.current()` 发现 `_ctx is None`，会返回一个空的默认上下文（仪器列表为空）。流水线照常运行，只是没有任何仪器被回调。

---

### 4.2 IRRewriter / IRVisitor 访问者模式

#### 4.2.1 概念说明

`Pass` 只规定了「对每个函数做一次变换」的外壳，真正「遍历并改写 IR 树」的工作交给访问者（functor）。Tilus 提供两套现成的访问者：

- **`IRRewriter`**：改写器。访问每个节点后返回一个新节点；如果子树没变，就原样返回旧节点（身份比较）。用于**实现变换**。
- **`IRVisitor`**：只读遍历器。访问每个节点后返回 `None`，只是「走一遍」并可能收集信息。用于**分析、统计、校验**（如 u3-l5 的 `collect_instructions`、`verify`）。

两者都继承自 `IRFunctor`，共享同一套「`visit_*` 按类型分派 + 记忆化」的分发机制。

#### 4.2.2 核心流程

访问者的核心方法是 `visit(node)`，它做三件事：

1. **算记忆化键**：对 `list/tuple/dict` 用 `id(node)`，对原始值（`str/int/float/bool`）用 `(type, value)`，对其它（IR 节点）用节点本身（因为节点身份相等，自身即可作键）。
2. **查表**：若键已在 `self.memo` 里，直接返回缓存结果，不再重复访问。
3. **分派**：用一串 `isinstance` 判断节点类型，调用对应的 `visit_<类型名>` 方法；找不到再走通用 fallback。算完结果写回 `memo`。

```
visit(node):
    key = memo_key(node)
    if key in memo: return memo[key]        # 命中缓存
    ret = <按 isinstance 分派到 visit_XXX>   # 分派
    memo[key] = ret                          # 写缓存
    return ret
```

对「指令（Instruction）」这类有大量子类的节点，分派是用 `getattr` 动态查找的（见下文源码），这是支持「子类只重写它关心的那一种指令」的关键。

#### 4.2.3 源码精读

先看分发与记忆化的核心（[python/tilus/ir/functors/functor.py:54-63](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/functors/functor.py#L54-L63) 与 [python/tilus/ir/functors/functor.py:149-150](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/functors/functor.py#L149-L150)）：

```python
def visit(self, node):
    key: Hashable
    if isinstance(node, (list, tuple, dict)):
        key = id(node)
    elif isinstance(node, (str, int, float, bool)):
        key = (type(node), node)
    else:
        key = node                 # IR 节点本身（身份相等）即可作键
    if key in self.memo:
        return self.memo[key]
    ...                            # isinstance 分派到 visit_XXX
    self.memo[key] = ret
    return ret
```

注意第 4 行：因为 IR 节点 `__hash__`/`__eq__` 基于身份，`node` 直接就是合法的字典键。这也解释了 u3-l5 里 `collect` 为什么能「遍历完直接扫 `memo.keys()`」——所有访问过的节点都留在 `memo` 里。

再看指令的**动态分派**（[python/tilus/ir/functors/functor.py:66-83](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/functors/functor.py#L66-L83)）：

```python
if isinstance(node, InstStmt):
    ret = self.visit_InstStmt(node)
elif isinstance(node, Instruction):
    method_name = "visit_" + node.__class__.__name__
    visit_method = getattr(self.__class__, method_name, None)
    if visit_method is None:
        ret = self.visit_Instruction(node)      # 通用 fallback
    else:
        ret = visit_method(self, node)          # 子类专属处理
```

含义：对于一条指令，先尝试找子类是否定义了 `visit_<指令类名>`（如 `visit_AddInst`）；没有就退回通用的 `visit_Instruction`。这让你写变换时**只需重写关心的那一种指令**，其余指令自动走通用处理。

接下来看 `IRRewriter` 的「未变则原样返回」范式。以 `visit_ForStmt` 为例（[python/tilus/ir/functors/functor.py:323-329](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/functors/functor.py#L323-L329)）：

```python
def visit_ForStmt(self, stmt: ForStmt) -> Stmt:
    extent = self.visit(stmt.extent)
    body = self.visit(stmt.body)
    if extent is stmt.extent and body is stmt.body:   # 子树都没变
        return stmt                                    # 原样返回
    else:
        return ForStmt(stmt.iter_var, extent, body, stmt.unroll_factor)
```

要点：

1. 先递归改写子节点（`extent`、`body`）。
2. 用 `is` 比较新旧子节点；只要有一个变了才构造新 `ForStmt`，否则原样返回旧节点。这让「未触及的子树」自动保持身份不变，最终 `process_program` 的短路优化才能生效。

最精妙的是 `visit_InstStmt`——它是「在变换里删除一条指令」的官方入口（[python/tilus/ir/functors/functor.py:305-314](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/functors/functor.py#L305-L314)）：

```python
def visit_InstStmt(self, stmt: InstStmt) -> Stmt:
    inst_or_stmt = self.visit(stmt.inst)
    if isinstance(inst_or_stmt, Stmt):
        return inst_or_stmt            # 指令被改写成了一条语句
    elif isinstance(inst_or_stmt, Instruction):
        return InstStmt(inst_or_stmt)  # 改写成了新指令
    elif inst_or_stmt is None:
        return SeqStmt(())             # 指令被「删除」→ 空语句
    else:
        raise ValueError(...)
```

也就是说：如果你让 `visit_<某指令>` 返回 `None`，这条 `InstStmt` 就会塌缩成空的 `SeqStmt(())`——这就是「消除一条指令」的标准做法（DCE、lowering 都用它，详见 u5-l3、u5-l4）。

而通用 `visit_Instruction` 负责「把指令的 output/inputs/attributes 都改写一遍」（[python/tilus/ir/functors/functor.py:453-465](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/functors/functor.py#L453-L465)）：

```python
def visit_Instruction(self, inst):
    output = self.visit(inst.output)
    inputs = self.visit(inst.inputs)
    attributes = {key: self.visit(value) for key, value in inst.attributes.items()}
    if (output is inst.output and inputs is inst.inputs
            and all(a is b for a, b in zip(attributes.values(), inst.attributes.values()))):
        return inst
    else:
        return dataclasses.replace(inst, output=output, inputs=inputs, **attributes)
```

注意它**也遍历了 `inst.attributes`**——这一点的重要性见 4.3.3。

最后看 `IRVisitor`（只读）。它的所有 `visit_*` 都返回 `None`，例如（[python/tilus/ir/functors/functor.py:507-509](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/functors/functor.py#L507-L509)）：

```python
def visit_ForStmt(self, stmt: ForStmt) -> None:
    self.visit(stmt.extent)
    self.visit(stmt.body)
```

它不构造新对象，只是「往下走」。u3-l5 的 `InstructionCollector` 就是最典型的例子（[python/tilus/ir/tools/instruction_collector.py:22-28](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tools/instruction_collector.py#L22-L28)）：

```python
class InstructionCollector(IRVisitor):
    def __init__(self):
        super().__init__()
        self.instructions: List[Instruction] = []
    def visit_Instruction(self, inst: Instruction) -> None:
        self.instructions.append(inst)
```

它只重写了 `visit_Instruction`，每访问到一条指令就追加到列表——其它节点走默认的只读遍历。

一个真实 Pass 的写法：`LetPropogationRewriter`（[python/tilus/transforms/let_propogation.py:24-82](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/let_propogation.py#L24-L82)）。它演示了「把 `memo` 当替换表」的技巧（[python/tilus/transforms/let_propogation.py:58-77](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/let_propogation.py#L58-L77)）：

```python
def visit_LetStmt(self, stmt: LetStmt) -> Stmt:
    bind_vars, bind_values = [], []
    for bind_var, bind_value in zip(stmt.bind_vars, stmt.bind_values):
        bind_value = self.visit(bind_value)
        if isinstance(bind_value, Var) and bind_value in self.let_vars:
            self.memo[bind_var] = bind_value     # ★ 把 var 映射成另一个 var
            continue                              # 该绑定被「吸收」
        ...
```

第 4 行 `self.memo[bind_var] = bind_value` 的含义是：**告诉后续遍历「以后再遇到 `bind_var` 这个 Var，就直接当成 `bind_value`」**。因为 `IRRewriter.visit_Expr` 会用 `self.memo` 作为 hidet 表达式的重写映射（[python/tilus/ir/functors/functor.py:278-281](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/functors/functor.py#L278-L281)），把 `memo` 里登记的替换应用到标量表达式上。于是 `memo` 同时承担了「避免重复访问」和「变量替换表」两重职责——这是读 Tilus 变换器时最容易忽略、却最关键的设计。

对应的 Pass 外壳极简（[python/tilus/transforms/let_propogation.py:85-88](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/let_propogation.py#L85-L88)）：

```python
class LetPropogationPass(Pass):
    def __call__(self, prog):                  # 注意：这里直接吃 Function
        rewriter = LetPropogationRewriter()
        return rewriter(prog)
```

> 小提示：`LetPropogationPass.__call__` 的签名实际按 `Function` 处理，与基类 `Pass.__call__(prog: Program)` 略有出入——它依赖外层调用约定，理解时以「实例化一个 rewriter，对函数跑一次」为准。

#### 4.2.4 代码实践

**实践目标**：写一个 `IRVisitor` 子类，统计一棵 IR 树里 `ForStmt` 的数量，并在嵌套循环上验证计数正确。

**操作步骤**（示例代码）：

```python
# 示例代码：统计 ForStmt 数量
from tilus.hidet.ir.expr import Var, as_expr
from tilus.hidet.ir.dtypes import int32
from tilus.ir.stmt import SeqStmt, ForStmt
from tilus.ir.functors import IRVisitor

class ForStmtCounter(IRVisitor):
    def __init__(self):
        super().__init__()
        self.count = 0
    def visit_ForStmt(self, stmt):           # 4.2.3 提到：必须手动继续往下走
        self.count += 1
        self.visit(stmt.body)                # ← 不写这行，内层循环不会被统计！

# 构造一棵 i ∈ [0,8) { j ∈ [0,4) { } } 的嵌套循环
i, j = Var("i", int32), Var("j", int32)
inner = ForStmt(j, as_expr(4), SeqStmt(()), unroll_factor=None)   # 字段顺序见 stmt.py:41-50
outer = ForStmt(i, as_expr(8), inner, unroll_factor=None)

counter = ForStmtCounter()
counter(outer)            # IRFunctor.__call__ → visit
print(counter.count)      # 期望：2
```

**需要观察的现象**：`counter.count` 应为 2（外层 + 内层各一个 `ForStmt`）。

**预期结果**：输出 `2`。如果把 `visit_ForStmt` 里的 `self.visit(stmt.body)` 删掉，`count` 会变成 `1`——因为内层 `ForStmt` 位于外层的 `body` 里，不主动下钻就访问不到。这正是「IRVisitor 的 `visit_*` 需要自己驱动递归」的体现（`IRVisitor.visit_ForStmt` 默认实现就调了 `self.visit(stmt.body)`，[python/tilus/ir/functors/functor.py:507-509](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/functors/functor.py#L507-L509)）。

> 进阶：要在一个「真实内核」的函数上跑计数，可像 CLAUDE.md 建议的那样用 `InstantiatedScript._jit_instance_for(...)` 取到 `JitInstance`，再读 `ji.transpiled_programs[0]` 拿到转译后的 `Program`，对其函数跑 `counter`。该路径需要 GPU 与 JIT，精确计数「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`IRRewriter` 与 `IRVisitor` 都继承自 `IRFunctor`，它们最大的区别是什么？

> **答案**：`IRRewriter` 的每个 `visit_*` 返回新节点（并做「未变则原样返回」的身份短路），用于改写 IR；`IRVisitor` 的每个 `visit_*` 返回 `None`，只遍历不改，用于分析/统计/校验。

**练习 2**：想让一个 rewriter 把某条指令「删掉」，应该让对应的 `visit_<指令>` 返回什么？

> **答案**：返回 `None`。`visit_InstStmt` 检测到 `visit(inst.inst)` 返回 `None` 时，会把该语句塌缩成空的 `SeqStmt(())`（[functor.py:311-312](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/functors/functor.py#L311-L312)）。

---

### 4.3 访问者模式的常见陷阱

#### 4.3.1 概念说明

访问者模式很强大，但有几个「看起来在干活、其实没干活」的陷阱，CLAUDE.md 把它们专门列了出来。踩中这些陷阱时，变换不会报错，但会**静默漏掉某些节点**，导致结果不正确。本模块逐一拆解。

#### 4.3.2 核心流程

三个最常踩的陷阱：

1. **`visit_Expr` 在只读遍历里是空操作**：`IRVisitor.visit_Expr` 直接 `pass`，不下钻 Hidet 标量表达式。表达式里藏的 `Var` 不会被访问到。
2. **记忆化阻止重复访问**：一旦某节点进了 `memo`，再次 `visit` 直接返回缓存，不会再次触发你的 `visit_*`。
3. **指令的 `attributes` 里藏着 Hidet 表达式**：很多指令把 barrier 地址、offset 等 `Var` 存在 `output/inputs` 之外的 dataclass 字段里，分析时必须一起扫。

#### 4.3.3 源码精读

**陷阱一：`visit_Expr` 不下钻。** 看 `IRVisitor` 的实现（[python/tilus/ir/functors/functor.py:488-489](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/functors/functor.py#L488-L489)）：

```python
def visit_Expr(self, expr: Expr) -> None:
    pass          # ← 不递归进 expr 内部
```

后果：如果你用 `IRVisitor` 想「收集程序里用到的所有 `Var`」，对 `extent`、`offset` 这种 Hidet 表达式，`collect` 收不到藏在里面的 `Var`。

> 对比：`IRRewriter.visit_Expr` **会**处理表达式——它调用 hidet 的 `rewrite(expr, rewrite_map=self.memo)`（[python/tilus/ir/functors/functor.py:278-281](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/functors/functor.py#L278-L281)）。所以「不下钻」是 `IRVisitor` 独有的问题。

正确做法（CLAUDE.md 明确给出）：改用 `hidet.ir.tools.collect(expr, Var)` 显式收集，或自己遍历 `inst.attributes` 并手动调用 hidet 的 collect。u3-l5 讲过的 `python/tilus/ir/tools/collector.py` 里的 `collect` 就是复用 `IRVisitor.memo` 的便捷封装（[python/tilus/ir/tools/collector.py:21-31](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tools/collector.py#L21-L31)）。

**陷阱二：记忆化阻止重复访问。** `visit` 一开头就查表（[python/tilus/ir/functors/functor.py:62-63](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/functors/functor.py#L62-L63)）。一旦某 `Var` 被访问过，它在多个表达式里出现时只会触发一次你的 `visit_*`。如果你需要「在每个出现点都做点事」，不能依赖访问者自动重复触发——而要自己显式扫描（例如直接遍历 `inst.attributes.values()` 再调 `hidet_collect`）。

> 注意：这个特性在改写器里是**好事**——它正好实现了「一个 Var 只被替换一次」。`LetPropogationRewriter` 写 `self.memo[bind_var] = bind_value`（[let_propogation.py:64](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/let_propogation.py#L64)）就是利用这一点让后续所有引用自动替换。陷阱只出现在「你以为会重复触发、其实不会」的分析场景。

**陷阱三：指令 attributes 藏着表达式。** 看 `Instruction.attributes` 的定义（[python/tilus/ir/inst.py:90-96](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/inst.py#L90-L96)）：

```python
@property
def attributes(self) -> dict[str, Any]:
    attrs = {}
    for k, v in self.__dict__.items():
        if k in ["output", "inputs"]:
            continue            # 除 output/inputs 外的字段都是 attributes
        attrs[k] = v
    return attrs
```

很多指令（如 barrier 地址、访存 offset、dims）把这些信息存在 dataclass 字段里，它们都是 `attributes`，且可能是 Hidet `Var`/`Expr`。如果你只看 `inst.output` 和 `inst.inputs`，就会漏掉这些标量信息。

好消息：通用 `visit_Instruction` 已经帮你遍历了 `inst.attributes`（[python/tilus/ir/functors/functor.py:453-456](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/functors/functor.py#L453-L456)）：

```python
output = self.visit(inst.output)
inputs = self.visit(inst.inputs)
attributes = {key: self.visit(value) for key, value in inst.attributes.items()}   # ★ 含 attributes
```

所以**改写器**会正确处理 attributes；但**只读分析**时若你自己遍历指令，别忘了把 `inst.attributes` 也纳入扫描。

CLAUDE.md 还提示了第四个相关陷阱：`TensorItemValueStmt` / `TensorItemPtrStmt` 把 Hidet `Var` 绑定到张量值/指针，是「张量世界」与「标量世界」的桥。判断活跃性（某张量/Var 是否还被使用）时，必须同时考虑张量本身和被绑定的 `Var`（[python/tilus/ir/functors/functor.py:545-551](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/functors/functor.py#L545-L551)）。这在 u5-l3 的死代码消除里会再次出现。

#### 4.3.4 代码实践

**实践目标**：亲手验证「`visit_Expr` 不下钻」这一陷阱。

**操作步骤**（示例代码）：

```python
# 示例代码：观察 IRVisitor 漏掉表达式内部的 Var
from tilus.hidet.ir.expr import Var, as_expr
from tilus.hidet.ir.dtypes import int32
from tilus.ir.functors import IRVisitor

class VarCollector(IRVisitor):
    def __init__(self):
        super().__init__()
        self.vars = []
    def visit_Var(self, v):            # 假装 IRVisitor 会回调 Var
        self.vars.append(v)

extent = as_expr(8) + Var("k", int32)  # 表达式内部含一个 Var "k"
VarCollector().visit(extent)
# 用 VarCollector.vars 看是否收集到 "k"
```

**需要观察的现象**：`VarCollector.vars` 很可能是空的——因为 `IRVisitor.visit_Expr` 是 `pass`，根本不会进入 `as_expr(8) + Var("k", int32)` 内部去回调 `visit_Var`。

**预期结果**：用上述自定义 visitor 收不到 `k`；改用 `from tilus.hidet.ir.tools import collect` 后 `collect(extent, Var)` 才能拿到。精确行为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：你写了一个 `IRVisitor` 子类想统计程序里所有 `Var` 的使用次数，结果数字明显偏小。最可能的原因是什么？

> **答案**：`IRVisitor.visit_Expr` 是空操作（[functor.py:488-489](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/functors/functor.py#L488-L489)），藏在 Hidet 标量表达式内部的 `Var` 不会被访问到。应改用 `hidet.ir.tools.collect(expr, Var)`。

**练习 2**：`IRRewriter` 也会遇到「attributes 藏 Var」的问题吗？

> **答案**：通常不会。通用 `visit_Instruction` 已经遍历了 `inst.attributes`（[functor.py:455-456](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/functors/functor.py#L455-L456)）。只有当你自己手写遍历、绕过 `visit_Instruction` 时才会漏掉。

**练习 3**：为什么 `LetPropogationRewriter` 里写 `self.memo[bind_var] = bind_value` 能让「后续所有对 `bind_var` 的引用自动被替换」？

> **答案**：因为 `IRRewriter.visit_Expr` 用 `self.memo` 作为 hidet 表达式的替换表（[functor.py:278-281](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/functors/functor.py#L278-L281)）。把 `bind_var → bind_value` 登记进 `memo` 后，任何含 `bind_var` 的表达式在改写时都会被替换。

---

## 5. 综合实践

把本讲三块内容串起来，完成下面这个综合小任务：

> **任务**：写一个名叫 `LoopDepthMeter` 的工具，它对一个 `Function` 计算「所有 `ForStmt` 的总嵌套深度」。要求：
> 1. 用 `Pass` 外壳 + `IRVisitor` 内核的组合（外层继承 `Pass`、`process_function` 内实例化 visitor）。
> 2. visitor 在进入 `visit_ForStmt` 时把「当前深度 +1」、访问完 body 后「−1」，并记录历史最大深度。
> 3. 把它套在一个含嵌套循环的最小 `Function` 上，打印最大嵌套深度。
> 4. （选做）打开 `tilus.option.debug.dump_ir`，编译一个 naive matmul，到缓存目录的 `ir/` 下观察「`LetPropogation` 等 Pass 之后的 IR」长什么样，体会 Pass 框架与仪器的配合。

实现提示：

- `Pass.process_function` 里 `visitor = LoopDepthMeter(); visitor(func); return func`（只分析、不改，原样返回函数即可）。
- 维护一个 `self.depth` 和 `self.max_depth`，在 `visit_ForStmt` 开头 `self.depth += 1; self.max_depth = max(...)`，递归 `self.visit(stmt.body)` 后 `self.depth -= 1`。
- 构造嵌套循环的 IR 可复用 4.2.4 的 `ForStmt(...)` 模式，`ForStmt` 字段顺序为 `iter_var, extent, body, unroll_factor`（[python/tilus/ir/stmt.py:41-50](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/stmt.py#L41-L50)）。

这个任务同时用到了「Pass 外壳」「IRVisitor 遍历」「身份相等下手动驱动递归」三个知识点，做完你就真正掌握了本讲。

## 6. 本讲小结

- Tilus 的变换框架由三件套组成：**`Pass` 负责「改」**（对每个函数跑一次变换，未变则原样返回）、**`PassContext` 负责「看」**（持有仪器、用 `with` 圈定作用域）、**`apply_transforms` 负责串联**（按序运行 Pass 并在前后回调仪器）。
- 访问者分两套：**`IRRewriter` 改写**（返回新节点、`is` 短路、`None` 即删除指令）、**`IRVisitor` 只读遍历**（返回 `None`、用于统计/校验）；二者共享 `IRFunctor` 的「`visit_*` 按类型分派 + `memo` 记忆化」机制。
- 分派对指令子类用 `getattr(self.__class__, "visit_<类名>", None)` 动态查找，让你只重写关心的指令，其余走通用 `visit_Instruction`。
- `memo` 一身二职：既是「避免重复访问」的缓存，也是改写器的「变量替换表」（`visit_Expr` 用它做 hidet 表达式重写）。
- 四大陷阱要牢记：**`IRVisitor.visit_Expr` 不下钻**（用 `hidet.ir.tools.collect`）；**记忆化不重复触发**；**指令 `attributes` 藏 Hidet 表达式**（[inst.py:90-96](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/inst.py#L90-L96)）；**`TensorItem*Stmt` 是张量/标量世界的桥**，活跃性分析要同时看二者。
- `optimize_program`（[drivers.py:82-93](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py#L82-L93)）把这三件套与 `debug.dump_ir` 选项接好，是观察整套机制的入口。

## 7. 下一步学习建议

- 下一讲 [u5-l2 默认变换流水线 get_default_passes](u5-l2-default-transform-pipeline.md) 会逐个拆解 `get_default_passes()`（[python/tilus/transforms/__init__.py:31-45](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/__init__.py#L31-L45)）里那十几个 Pass 的顺序与职责，你会看到本讲的框架被一组真实变换填满。
- 想看「`visit_InstStmt` 返回 `None` 删指令」的真实用法，直接读 [u5-l3 死代码消除与标量分析](u5-l3-dead-code-elimination-and-scalar-analysis.md) 与 `python/tilus/transforms/dead_code_elimination.py`。
- 想动手扩展，参考 `tests/transforms/test_dead_code_elimination.py` 的 `_make_function` 模式，用本讲的 `IRVisitor`/`IRRewriter` 写一个自己的小 Pass，为 [u8-l5 扩展开发：自定义 Pass 与新增指令](u8-l5-writing-custom-pass-and-extension.md) 做铺垫。
- 调试时，组合 `tilus.option.debug.dump_ir()` + 本讲的「Pass 前后仪器回调」理解，对照缓存目录 `ir/` 下每个 `<count>_<pass>.txt` 看变换逐步推进。
