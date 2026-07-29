# IR 工具：验证、打印与收集

## 1. 本讲目标

前三讲我们认识了 Tilus IR 的骨架（Program/Function/Stmt）和它的两大主角（Instruction/Tensor）。但 IR 是一堆**不可变（immutable）**的 frozen dataclass，用普通 `print` 看不出结构，也难直接「肉眼找 bug」。本讲就来补上阅读和调试 IR 必备的四件套工具：

学完本讲，你应当能够：

1. 理解 `verify` 在编译流水线中扮演的「第 0 步合法性校验」角色，并能读懂它抛出的错误。
2. 会用 `IRPrinter` / `PrintContext` 把任意 IR 节点渲染成人类可读文本，并看懂张量/变量的自动命名规则。
3. 会用 `collect` 与 `collect_instructions` 统计、检索 IR 中的节点，用于调试和写测试。

一句话：前三讲教「IR 是什么」，本讲教「怎么用工具看 IR、查 IR、找 IR 里的错误」。

## 2. 前置知识

本讲默认你已经掌握以下内容（来自 u3-1 ~ u3-4）：

- **编译流水线**（u3-1）：`drivers.build_program` 分六阶段，第 0 阶段就是 `verify(prog)`。本讲会回到这个调用点。
- **IR 结构**（u3-3）：`Program`（`frozendict[str, Function]`）、`Function`（name/params/body/metadata）、`Stmt` 语句树，以及 **身份相等**（`__hash__` 即 `id`、`__eq__` 即 `is`）这一关键设计。
- **Instruction 与 Tensor**（u3-4）：指令的三段结构 `output / inputs / attributes`，四种张量（Register/Shared/Global/TMemory）。
- **访问者模式**（u3-3 提到）：本讲的四个工具全部建立在 `IRVisitor` 这个只读访问者之上。

还需要两个 Python 基础：

- `dataclass`：Tilus IR 节点都是 `@dataclass(frozen=True, eq=False)`。
- 装饰器与上下文管理器（`with`）：打印工具用到了 `PrintContext` 上下文。

> 小提示：如果你对「为什么 IR 要做成不可变 + 身份相等」还有疑问，建议先回看 u3-3 的「身份相等」一节。本讲工具的很多行为（命名、记忆化、收集）都源自这个设计。

## 3. 本讲源码地图

本讲聚焦 `python/tilus/ir/tools/` 目录，外加两处调用/支撑点：

| 文件 | 作用 | 本讲角色 |
| --- | --- | --- |
| `python/tilus/ir/tools/verifier.py` | `verify` 校验器 + 错误对象（`Diagnostic`/`Diagnostics`/`VerificationError`） | **核心：合法性校验** |
| `python/tilus/ir/tools/printer.py` | `IRPrinter` 渲染器 + `PrintContext` 上下文 | **核心：人类可读打印** |
| `python/tilus/ir/tools/collector.py` | 通用 `collect(node, types)` | **核心：按类型收集节点** |
| `python/tilus/ir/tools/instruction_collector.py` | 专用 `collect_instructions(func)` | **核心：统计指令** |
| `python/tilus/ir/tools/__init__.py` | 工具包的对外出口 | 看清导出了哪些名字 |
| `python/tilus/ir/node.py` | `IRNode.__str__` 接到默认 printer | 解释为什么 `print(func)` 就能看 IR |
| `python/tilus/ir/functors/functor.py` | `IRVisitor`（只读访问者）基类 | 四个工具的共同地基 |
| `python/tilus/drivers.py` | `build_program` 第 0 步调用 `verify` | 串回编译流水线 |
| `tests/transforms/test_dead_code_elimination.py` | 用 `collect_instructions` 写测试的范例 | 实践任务的参考模板 |

## 4. 核心概念与源码讲解

本讲按「**校验 → 打印 → 收集**」的顺序拆成三个最小模块。三者都建立在 `IRVisitor` 之上，所以我们先花一句话统一地基：

`IRVisitor`（[python/tilus/ir/functors/functor.py:472-588](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/functors/functor.py#L472-L588)）是一个只读访问者：`visit(node)` 会按节点类型分派到 `visit_<ClassName>`，每个节点访问一次（靠 `memo` 记忆化），默认的 `visit_*` 大多返回 `None` 且递归遍历子节点。本讲的 verify / collect / collect_instructions 都是「继承 `IRVisitor` + 重写几个 `visit_*` + 读自己累加的状态」。

### 4.1 verify：编译前的合法性校验

#### 4.1.1 概念说明

`verify` 是 `build_program` 的**第 0 步**：在真正做任何优化、降级、代码生成之前，先把 IR 通读一遍，检查它是否「自洽」。如果不自洽，就直接抛异常终止编译——这比让它悄悄生成错误的 CUDA 再在 nvcc 处崩掉要好诊断得多。

需要注意的是，`IRVerifier` 是一个**增量式（incremental）校验器**：它目前只实现了少数几条规则（本讲会精读到的 `LoadSharedInst` / `StoreSharedInst` 的形状匹配）。这意味着「通过了 verify」并不等于「绝对正确」，而是「已知的这些规则没有被违反」。新增检查时，只需给 `IRVerifier` 加一个 `visit_<InstClass>` 方法即可，这正是它设计成访问者的好处。

#### 4.1.2 核心流程

`verify` 的执行过程可以概括为：

```
verify(prog)
  └─ IRVerifier()(prog)          # 以访问者遍历整个 Program
        └─ 每遇到一条指令 → visit_<InstClass> 检查规则
              └─ 不合法 → self.error(...) → 追加一条 Diagnostic
  └─ 若 self.diagnostics 非空
        └─ raise VerificationError(Diagnostics(prog, diagnostics))
```

关键设计：校验器**不立即抛异常**，而是先把所有问题都收集进 `self.diagnostics`，最后一次性汇报。这样可以一次性看到全部错误，而不是「修一个、跑一次、再报下一个」。

#### 4.1.3 源码精读

先看入口函数 [`verify`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tools/verifier.py#L140-L144)：

```python
def verify(prog: Program) -> None:
    verifier = IRVerifier()
    verifier(prog)                       # 等价于 verifier.visit(prog)
    if verifier.diagnostics:
        raise VerificationError(Diagnostics(prog=prog, diagnostics=verifier.diagnostics))
```

它接收的是 **`Program`**（不是单个 Function），因为 `IRVisitor.visit_Program` 会自动遍历其中的所有函数（见 [functor.py:494-495](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/functors/functor.py#L494-L495)）。`verifier(prog)` 其实是 `IRFunctor.__call__` → `visit`。

再看 [`IRVerifier`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tools/verifier.py#L96-L137) 的两条规则，以 `visit_LoadSharedInst` 为例（`LoadSharedInst` 表示「从共享内存搬到寄存器」）：

```python
def visit_LoadSharedInst(self, inst: LoadSharedInst) -> None:
    shared_shape = vector(inst.shared_input.shape)
    register_shape = vector(inst.register_output.shape)
    if len(shared_shape) != len(register_shape) or any(vector(shared_shape) != vector(register_shape)):
        self.error(
            inst,
            "The shared tensor shape [{}] does not match the register tensor shape [{}].",
            inst.shared_input.shape,
            inst.register_output.shape,
        )
```

要点解读：

- `vector(...)`（来自 `tilus.ir.utils`）把 `shape` 包成一个可逐元素比较的 `Vector` 对象，避免 `any` 对普通 list 的歧义（它的 `__bool__` 会主动抛错，见 [vector.py:29-30](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/utils/vector.py#L29-L30)）。
- `self.error(context, message, *items)` 记录一条诊断。`message` 里的 `{}` 是占位符，`items` 会按顺序填进去——构造时会强制校验占位符与实参数量一致。

错误对象三件套（[verifier.py:32-93](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tools/verifier.py#L32-L93)）：

- [`Diagnostic`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tools/verifier.py#L32-L52)：单条诊断，含 `context`（出错的 IR 节点）、`message`（带 `{}` 占位符）、`items`（填充占位符的节点）。它会在构造时断言占位符个数等于 `items` 个数，这个不变量可写成：

\[
  \mathrm{len}(\textit{items}) \;=\; \textit{message.count}(\texttt{"\{\}"})
\]

- [`Diagnostics`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tools/verifier.py#L55-L85)：诊断集合。它的 `__str__` 会调用 `IRPrinter` 把整棵 `prog` 打印出来，再逐条列出错误，并标注每条错误来自哪个 `context`——这正是「打印工具」与「校验工具」的天然配合。
- [`VerificationError`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tools/verifier.py#L88-L93)：最终抛出的异常，把 `Diagnostics` 包了一层，`__str__` 直接转交。

最后回到流水线，看 `verify` 的真实调用点 [drivers.py:313](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py#L313)，注释写明 `# 0. verify the program`，紧接着才是第 1 步 `optimize_program`。这说明校验失败时，整个内核根本不会进入优化与代码生成。

#### 4.1.4 代码实践

**实践目标**：亲手触发一次 `VerificationError`，看清它的报文长什么样。

**操作步骤**：

1. 阅读真实测试 [tests/ir/tools/verifier/test_verify_load_shared.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/tests/ir/tools/verifier/test_verify_load_shared.py)，它定义了一个最小 `Script`：声明一个 shared 张量和一个 register 张量，然后 `load_shared` 把前者搬进后者。
2. 仿照它写一段脚本，故意让 shared 和 register 的形状不一致（如 `[4,4]` vs `[8,8]`）：

```python
# 示例代码：演示触发 verify 失败（参考 tests/ir/tools/verifier/test_verify_load_shared.py）
import tilus
from tilus.ir.tools import VerificationError, verify

class DemoLoadShared(tilus.Script):
    def __init__(self, shared_shape, register_shape):
        super().__init__()
        self.shared_shape = shared_shape
        self.register_shape = register_shape

    def __call__(self):
        self.attrs.warps = 2
        self.attrs.blocks = 1
        regs = self.register_tensor(dtype=tilus.float32, shape=self.register_shape)
        smem = self.shared_tensor(dtype=tilus.float32, shape=self.shared_shape)
        self.load_shared(src=smem, out=regs)

# 注意：shared 与 register 形状不一致，verify 应当失败
script = DemoLoadShared(shared_shape=[4, 4], register_shape=[8, 8])
program = script._jit_instance_for().transpiled_programs[0]

try:
    verify(program)
    print("verify 通过（不应发生）")
except VerificationError as e:
    print("verify 失败，报文如下：")
    print(e)
```

**需要观察的现象**：

- 程序进入 `except` 分支，打印出 `Verification failed with 1 errors:` 开头的报文。
- 报文里会先用 `IRPrinter` 渲染整段 IR，再指出 `LoadShared` 指令处 `[4, 4]` 与 `[8, 8]` 不匹配。

**预期结果**：`VerificationError` 被抛出且包含形状不匹配的描述；若改成 `shared_shape=[8,8], register_shape=[8,8]` 则 `verify(program)` 正常返回、不抛异常（这正是该测试的 `success=True` 用例）。

**待本地验证**：上述确切报文文本（含张量自动命名 `%r0`/`%s0` 等）需在本地运行确认。

#### 4.1.5 小练习与答案

**练习 1**：如果想让 `verify` 还能检查 `StoreSharedInst`（从寄存器写回共享内存）的形状匹配，应该怎么做？

> **参考答案**：不需要改 `verify` 函数本身。`IRVerifier` 里已经有 [`visit_StoreSharedInst`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tools/verifier.py#L127-L137) 做了几乎一样的检查。若要新增对其他指令的检查，只需在 `IRVerifier` 中加一个 `visit_<InstClass>` 方法并在其中调用 `self.error(...)`，访问者会自动分派到它。

**练习 2**：为什么 `verify` 要收集全部诊断后再一次性抛出，而不是遇到第一个就抛？

> **参考答案**：一次性汇报能让你在一次运行里看到 IR 中所有不合法之处，避免「修一个、再跑、又报一个」的低效循环；同时错误对象的 `__str__` 还会把整棵 IR 打印出来辅助定位。

### 4.2 printer：把 IR 渲染成人类可读文本

#### 4.2.1 概念说明

IR 是一堆不可变 dataclass，直接 `print` 一个 `Function` 只会得到难看的对象表示。`IRPrinter` 的工作是把它渲染成**形似 Python、带注释**的可读文本，这是阅读、调试、以及上面 `Diagnostics.__str__` 输出错误时的统一底座。

它有三个让输出可读的关键设计：

1. **自动命名**：张量和变量在第一次出现时按类型自动编号——寄存器张量 `%r0/%r1`、共享 `%s0`、全局 `%g0`、张量内存 `%t0`；标量变量 `%v0`、指针 `%p0`。命名靠「身份相等」的 dict（`tensor2name`/`var2name`）记忆。
2. **布局折叠**：布局（Layout）对象很长，反复打印会刷屏。printer 把较长的布局文本替换成短键（`layout_0`、`shared_layout_0` 等），并在函数末尾以注释形式集中列出键的含义。
3. **类型注释**：每条有产出的指令行尾会加 `# register, float32[...]` 注释，直接告诉你产出张量的类型与布局。

最妙的是：你**根本不需要手动 new 一个 printer**。`IRNode.__str__` 会自动取「当前打印上下文」里的 printer，所以 `print(func)` / `print(inst)` 就能直接看到渲染结果。

#### 4.2.2 核心流程

打印的分派与 4.1 的访问者同源（`IRPrinter` 继承 `IRFunctor`），只是每个 `visit_*` 返回的是 `Doc`（一种可拼接的文本文档对象），最终 `str(doc)` 得到字符串：

```
print(func)
  └─ IRNode.__str__
        └─ PrintContext.current()   # 取当前 printer（没有就临时建一个）
              └─ printer(func)      # = visit_Function
                    ├─ 打印函数签名 def name(params):
                    ├─ visit_FuncMetadata（grid/cluster/warps/analysis 注释）
                    ├─ visit(func.body)（递归各类 Stmt）
                    └─ 末尾打印 layout 键的注释对照表
```

#### 4.2.3 源码精读

先看打印是怎么「无感」接入的——[node.py:20-25](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/node.py#L20-L25)：

```python
def __str__(self):
    from tilus.ir.tools.printer import PrintContext
    printer = PrintContext.current()
    return str(printer(self))
```

所以**任何 `IRNode` 子类**（Function/Stmt/Instruction/Tensor/Layout）都能直接 `print`。`PrintContext.current()` 在没有显式上下文时会临时 `IRPrinter()` 一个（[printer.py:505-510](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tools/printer.py#L505-L510)）。

接着看指令是如何渲染的——[`visit_Instruction`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tools/printer.py#L360-L398)：

```python
def visit_Instruction(self, inst: Instruction) -> Doc:
    doc = Doc()
    if inst.output is not None:
        doc += self.visit(inst.output) + " = "          # %r0 = ...
    inst_name = inst.__class__.__name__.removesuffix("Inst")   # AddInst -> Add
    doc += inst_name + "("
    items = []
    if len(inst.inputs):
        items.append(self.visit(inst.inputs))           # 位置参数：inputs
    for k, v in inst.attributes.items():                # 关键字参数：attributes
        ...
        items.append("{}={}".format(k, v_doc))
    ...
    doc += ")"
    if inst.output is not None:
        doc += "  # " + self.get_tensor_type(inst.output)   # 行尾类型注释
    return doc
```

这正是把「指令三段结构 `output/inputs/attributes`」渲染成 `%r2 = Add(%r0, %r1)  # register, float32[...]` 的地方。注意它用 `inst.output is not None`（**身份判断**，呼应 u3-4 的提醒：判断产出有无要用 `is not None` 而非 `== None`）。

再看自动命名——以 [`visit_RegisterTensor`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tools/printer.py#L418-L440) 为代表，四种张量命名规则一致：

```python
def visit_RegisterTensor(self, tensor: RegisterTensor) -> Doc:
    if tensor not in self.tensor2name:                  # 身份相等：tensor2name 以张量对象为键
        self.tensor2name[tensor] = "%r" + str(self.register_count)
        self.register_count += 1
    return Text(self.tensor2name[tensor])
```

因为张量是身份相等（`eq=False`，`__hash__` 即 `id`），`tensor not in self.tensor2name` 比较的就是对象身份，所以同一个张量在全文里始终用同一个名字。

最后看「函数级」渲染 [`visit_Function`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tools/printer.py#L215-L245)：它先打印 `def name(params):`，再以注释打印 `Metadata`（`visit_FuncMetadata` 会输出 `grid_blocks`/`cluster_blocks`/`num_warps`/`analysis`），然后缩进打印函数体，最后把所有 layout 键的对照表以注释附在末尾。这套输出格式与 `Diagnostics.__str__` 的渲染完全一致。

补充一个进阶点：当你需要「在不污染全局打印状态」的情况下打印（例如在测试里），可以用 [`print_context()`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tools/printer.py#L513-L515) 上下文管理器或 [`use_standalone_printer`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tools/printer.py#L518-L530) 装饰器，它们会新建并压入一个独立的 `IRPrinter`，退出时弹出。

#### 4.2.4 代码实践

**实践目标**：打印一段真实 IR，观察自动命名、类型注释与 layout 键对照表。

**操作步骤**：接续 4.1.4 的脚本（形状匹配、`success=True` 的版本，使其能正常通过 verify），在拿到 `program` 后：

```python
# 示例代码：打印整棵 IR 树
func = list(program.functions.values())[0]
print(func)        # 触发 IRNode.__str__ -> IRPrinter
```

**需要观察的现象**：

1. 输出形似 `def <name>(%p0: ~float32):` 的函数签名。
2. 顶部注释里能看到 `grid_blocks = [...]`、`num_warps = 2` 等 metadata。
3. 函数体里每条指令形如 `%r0 = AllocateRegister(...)  # register, float32[8, 8]`。
4. 文件末尾有 `# layout_0: RegisterLayout(...)` 这样的注释，给出布局键的完整含义。

**预期结果**：得到一段可读的、形似 Python 的 IR 文本，其中张量名形如 `%r0/%s0`、变量名形如 `%v0/%p0`，长布局被折叠成短键。

**待本地验证**：确切的指令名、张量编号顺序需在本地运行确认（不同 schedule 下编号可能不同）。

#### 4.2.5 小练习与答案

**练习 1**：为什么打印同一个 `Function` 两次，第二次的张量编号可能从 `%r0` 重新开始？

> **参考答案**：因为命名状态（`register_count` 等）存在 `IRPrinter` 实例上。`IRNode.__str__` 每次在没有活动 `PrintContext` 时会新建一个 `IRPrinter()`（见 `PrintContext.current()` 的兜底逻辑），计数器随之归零。若用 `with print_context():` 包住多次打印，则共享同一个 printer，编号会延续。

**练习 2**：指令渲染时为什么用 `inst.output is not None` 而不是 `inst.output != None`？

> **参考答案**：`IRNode` 重载了 `__eq__` 为身份相等（`self is other`），`inst.output != None` 实际是 `inst.output is not None` 的等价写法，但项目约定统一用 `is not None` 更明确，也避免被误读成「值比较」（参见 u3-4 的提醒）。

### 4.3 collector 与 instruction_collector：收集与统计

#### 4.3.1 概念说明

阅读 IR 时，我们常需要回答这类问题：「这个函数里有几条 `AddInst`？」「里面有几个 `ForStmt`？」「所有被加载的全局张量是哪些？」`collect` 与 `collect_instructions` 就是回答这类问题的两把尺子。

- **`collect(node, types)`**（通用收集器）：遍历以 `node` 为根的 IR，返回所有「类型属于 `types`」的节点。可以用来收任意节点——语句、表达式、张量、布局、指令都行。
- **`collect_instructions(func)`**（专用指令收集器）：只针对 `Function`，按访问顺序返回其中**所有指令**的列表。它是在测试和调试里用得最多的工具——比如统计某条 Pass 前后某类指令的数量变化。

两者都基于同一个事实：`IRVisitor` 用 `memo` 记录「访问过哪些节点」，`collect` 直接读 `visitor.memo.keys()` 来取全部已访问节点，非常巧妙。

> ⚠️ **重要陷阱（来自 CLAUDE.md / u3-3）**：`IRVisitor.visit_Expr` 是个 **no-op**，它**不会**下钻到 Hidet 标量表达式内部。因此用 `collect(node, [Var])` 去收集「表达式里的 Var」时，嵌在 `Expr` 里的 `Var` 根本不会被访问、也就不会出现在 `memo` 里。要收集表达式内部的 `Var`，必须改用 Hidet 的 `hidet.ir.tools.collect(expr, Var)`，而不是 Tilus 的 `collect`。本讲的 `collect` 能可靠收到的，是那些**直接**作为语句/指令字段出现的节点（如 `ForStmt.iter_var`、`Metadata.block_indices`、指令的 inputs/output 等）。

#### 4.3.2 核心流程

两个收集器的流程几乎相同，差别只在「收集什么」与「从哪里取结果」：

```
# collect（通用）
collect(node, types)
  └─ IRVisitor().visit(node)     # 只遍历，结果都落在 memo 里
  └─ 遍历 visitor.memo.keys()，挑出 isinstance(node, types) 的

# collect_instructions（专用）
collect_instructions(func)
  └─ InstructionCollector()(func)
        └─ visit_Instruction：把每条指令 append 进 self.instructions
  └─ 返回 self.instructions（按访问顺序）
```

#### 4.3.3 源码精读

先看通用的 [`collect`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tools/collector.py#L21-L31)：

```python
def collect(node: IRNode, types: Sequence[Any]) -> list[Any]:
    visitor = IRVisitor()
    visitor.visit(node)            # 遍历；所有访问过的节点都进了 visitor.memo
    types = tuple(types)
    ret = []
    for node in visitor.memo.keys():        # 直接复用 memo 作为「已访问节点集合」
        if isinstance(node, types):
            ret.append(node)
    return ret
```

这里的妙处在于：`IRVisitor` 的 `memo` 本来是给「记忆化、避免重复访问」用的（[functor.py:62-63](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/functors/functor.py#L62-L63) 与 [functor.py:149](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/functors/functor.py#L149)），`collect` 直接把它当「全量已访问节点集合」来过滤，零额外存储。注意它收的是**所有类型**的已访问节点，再按 `types` 筛，所以既能收指令，也能收语句、张量、布局。

再看专用的 [`collect_instructions`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tools/instruction_collector.py#L22-L34)：

```python
class InstructionCollector(IRVisitor):
    def __init__(self) -> None:
        super().__init__()
        self.instructions: List[Instruction] = []

    def visit_Instruction(self, inst: Instruction) -> None:
        self.instructions.append(inst)         # 每访问到一条指令就收集


def collect_instructions(prog: Function) -> List[Instruction]:
    collector = InstructionCollector()
    collector(prog)
    return collector.instructions
```

注意两点：

1. 它**只重写了 `visit_Instruction`**——`IRVisitor.visit_InstStmt` 默认会 `visit(stmt.inst)`（[functor.py:500-501](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/functors/functor.py#L500-L501)），从而把每条 `InstStmt` 里的指令送进来。这就是「访问者自动分派」的好处。
2. 形参名叫 `prog` 但类型标注是 `Function`——它实际接收的是一个 **`Function`**（不是 `Program`）。这与 `verify`（接收 `Program`）不同，调用时要注意区分。

它在真实测试里怎么用？看 [tests/transforms/test_dead_code_elimination.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/tests/transforms/test_dead_code_elimination.py) 的辅助函数：

```python
def _count_insts(program: Program, inst_type) -> int:
    func = list(program.functions.values())[0]
    insts = collect_instructions(func)                       # 取出全部指令
    return sum(1 for inst in insts if isinstance(inst, inst_type))   # 按类型计数
```

这个 `_count_insts` 是「统计某类指令数量」的标准范式，我们的实践任务就基于它。

#### 4.3.4 代码实践

**实践目标**：对一段真实 IR，用 `collect_instructions` 统计各类指令的数量，并把它们打印成一张表。

**操作步骤**：接续 4.2.4 拿到的 `program`/`func`：

```python
# 示例代码：统计指令类型分布
from collections import Counter
from tilus.ir.tools.instruction_collector import collect_instructions
from tilus.ir.tools import collect       # 通用收集器，顺便对比

func = list(program.functions.values())[0]
insts = collect_instructions(func)

# 1) 按指令类名统计数量
counts = Counter(type(inst).__name__ for inst in insts)
print("指令类型分布：")
for name, n in counts.most_common():
    print(f"  {name}: {n}")

# 2) 对比：用通用 collect 也能收指令（结果应包含上面那些指令）
from tilus.ir.inst import Instruction
all_insts = collect(func, [Instruction])
print(f"collect 统计到 {len(all_insts)} 条指令，collect_instructions 统计到 {len(insts)} 条")
```

**需要观察的现象**：

1. 第 1 步打印出类似 `AllocateRegisterInst: 1`、`LoadSharedInst: 1` 等计数表。
2. 第 2 步两种方式的指令数**相等**——因为 `collect(func, [Instruction])` 与 `collect_instructions(func)` 走的是同一套访问，只是取结果的途径不同（一个从 memo 过滤，一个在 `visit_Instruction` 里累加）。

**预期结果**：得到一张指令类型计数表；两种收集方式统计的指令总数一致。

**待本地验证**：具体指令种类与计数取决于 `DemoLoadShared` 经转译后生成的 IR，需在本地运行确认。

#### 4.3.5 小练习与答案

**练习 1**：`collect_instructions(func)` 的形参类型是 `Function`，而 `verify(prog)` 接收的是 `Program`。如果你手上只有一个 `Program`，如何拿到其中的指令？

> **参考答案**：先取出函数再收集。一个 `Program` 是 `frozendict[str, Function]`，所以 `func = list(program.functions.values())[0]`（单函数程序）或遍历所有函数，再调用 `collect_instructions(func)`。这正是测试里 `_count_insts` 的做法。

**练习 2**：为什么用 `collect(func, [Var])` 可能收集不到藏在指令 `offset` 表达式里的 `Var`？

> **参考答案**：因为 `IRVisitor.visit_Expr` 是 no-op，不会下钻到 Hidet 标量表达式内部（见 CLAUDE.md 的「IRVisitor/IRRewriter Pitfalls」）。嵌在 `Expr` 里的 `Var` 不会被访问，也就不会进入 `memo`。要收集表达式内部的 `Var`，应使用 `hidet.ir.tools.collect(expr, Var)`。

## 5. 综合实践

把本讲三个工具串起来，完成一次「端到端的 IR 体检」：

1. 用 `Script._jit_instance_for().transpiled_programs[0]` 拿到一个真实 `Program`（可复用 4.1.4 的 `DemoLoadShared`，形状用合法的 `[8,8]/[8,8]`）。
2. **打印**：`print(list(program.functions.values())[0])`，通读渲染后的 IR，人工识别其中有哪些指令。
3. **统计**：用 `collect_instructions` + `Counter` 生成指令类型计数表，与你人工识别的结果对照。
4. **校验**：调用 `verify(program)` 确认它通过；再构造一个形状不匹配的版本，确认 `verify` 抛出 `VerificationError` 并能打印出带 IR 渲染的报文。
5. **进阶**：尝试用 `collect(func, [ForStmt])` 统计 `ForStmt` 数量；并验证「`collect(func, [Var])` 收不到表达式内部 Var」这一陷阱（例如对比 `len(collect(func, [Var]))` 与肉眼在打印输出里看到的 `%v*` 个数）。

> 这个任务把「校验 → 打印 → 收集」三件套用在了同一个 `Program` 上，正好对应你在真实开发中调试一条 Pass、排查一个内核时的典型工作流：先 `verify` 保证合法，再 `print` 看结构，再用 `collect_instructions` 量化变化。

## 6. 本讲小结

- `verify(prog)` 是 `build_program` 的**第 0 步**，基于 `IRVisitor` 的增量式校验器，收集全部 `Diagnostic` 后一次性抛 `VerificationError`；新增检查只需给 `IRVerifier` 加 `visit_<InstClass>`。
- `IRPrinter` 把 IR 渲染成形似 Python 的可读文本，靠**身份相等**的 dict 自动命名（`%r0/%s0/%v0`），长布局折叠成键并在函数末尾注释对照；`IRNode.__str__` 通过 `PrintContext.current()` 无缝接入，所以 `print(func)` 直接可用。
- `collect(node, types)` 复用 `IRVisitor.memo` 当作「已访问节点集合」做通用过滤；`collect_instructions(func)` 则在 `visit_Instruction` 里按序累加，是测试里统计指令的标准工具。
- 四个工具同根同源，都建立在 `IRVisitor` 之上，差别只在「重写哪个 `visit_*`」与「从哪里取结果」。
- **关键陷阱**：`IRVisitor.visit_Expr` 不下钻 Hidet 表达式，因此 `collect` 收不到藏在表达式内部的 `Var`；要收集表达式内部的标量节点须用 Hidet 的 `hidet.ir.tools.collect`。
- `collect_instructions` 接收 `Function`、`verify` 接收 `Program`，调用时注意区分。

## 7. 下一步学习建议

本讲只是「会用工具看 IR」。接下来：

- **进入变换层（U5）**：本讲的 `collect_instructions` 正是下一讲 [u5-1 Pass 框架与 IRRewriter/IRVisitor](u5-l1-pass-framework-irrewriter.md) 的核心道具——你会看到 `IRRewriter`（访问者的「可写」兄弟）如何基于同样的分派机制改写 IR，而 `IRPrinter` + `collect_instructions` 则是验证 Pass 效果的必备搭档。
- **动手读一个真实 Pass**：带着本讲工具去读 [python/tilus/transforms/dead_code_elimination.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/dead_code_elimination.py)，并参照 [tests/transforms/test_dead_code_elimination.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/tests/transforms/test_dead_code_elimination.py) 里 `_count_insts` 的用法，体会「打印 + 收集 + 校验」三件套在真实测试里的组合。
- **调试技巧**：学完 U5 后，结合 `tilus.option.debug.dump_ir()`（u3-1 提到），你会得到每个 Pass 之后的 IR 快照——那时本讲的 `IRPrinter` 渲染规则将成为你读懂这些快照的「字典」。
