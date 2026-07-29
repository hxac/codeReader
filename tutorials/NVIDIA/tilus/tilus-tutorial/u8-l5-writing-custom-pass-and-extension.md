# 扩展开发：自定义 Pass 与新增指令

## 1. 本讲目标

本讲是整本学习手册的收尾篇。前面八单元我们一直以「阅读者」的视角理解 Tilus：从编程模型、IR 结构、布局系统、变换流水线，一直到后端代码生成与高性能内核实践。本讲把视角切换为「扩展者」——学完本讲，你应该能够：

1. **写一个自定义 Pass**：继承 `Pass`、用 `IRRewriter`/`IRVisitor` 遍历并改写 Tilus IR，把它注册进默认流水线 `get_default_passes()`，并用 `dump_ir` 验证它确实被加入了流水线。
2. **理解新增一条指令需要同步的「四件套」**：IR 定义、布局推理/验证规则、发射器、发射器注册，以及它们各自挂载到全局注册表的方式。
3. **掌握端到端测试范式**：既会用「手工构造 IR」做单元级测试（参考 `tests/transforms/test_dead_code_elimination.py`），也会用 `InstantiatedScript._jit_instance_for(...).transpiled_programs[0]` 做从 `Script` 到 `Program` 的集成级测试。

## 2. 前置知识

本讲默认你已经读过下面三篇讲义，并建立了相应认知（本讲不会重复它们已讲清的内容，只做承接）：

- **u5-3 死代码消除与标量分析**：DCE 以功能指令白名单 `FUNCTIONAL_INST_TYPES` 为可删判据、副作用指令永不删；它就是我们「自定义 Pass 的最佳范例」。
- **u5-1 Pass 框架与 IRRewriter/IRVisitor**：`Pass`/`PassContext`/`apply_transforms` 三件套、`IRRewriter`（改）与 `IRVisitor`（看）共享 `IRFunctor` 的 `visit_*` 分派 + `memo` 记忆化、以及四大访问者陷阱。
- **u6-2 EmitterBase 与发射器注册机制**：`BaseInstEmitter` 基类、全局 `REGISTRY`、`@register_emitter(inst_cls, target)` 装饰器、`resolve_inst_emitter` 的两步派单。

如果这三种机制你已熟悉，本讲就是「把它们拼成一个完整的二次开发工作流」。另需补充两点背景：

- **不可变 IR 的修改范式**：所有 IR 节点是 `@dataclass(frozen=True, eq=False)`，用身份相等（`__eq__`/`__hash__` 基于 `id`）。要「改」一个节点不是原地赋值，而是通过 `dataclasses.replace` 或 `with_*` 方法返回一个新对象。`IRRewriter.visit_Instruction` 已经替你封装好了这套替换（见 4.1.3）。
- **Pass 在流水线中的唯一入口**：`drivers.optimize_program` 调用 `get_default_passes()` 得到 Pass 列表，再交给 `apply_transforms` 顺序执行。要让你的 Pass 被「正常编译一个内核」时自动触发，唯一的办法就是让它出现在这个列表里。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `python/tilus/transforms/base.py` | `Pass`/`PassContext`/`apply_transforms` 三件套——自定义 Pass 的基类与运行器。 |
| `python/tilus/transforms/__init__.py` | `get_default_passes()`——默认流水线的「唯一清单」，注册 Pass 的落点。 |
| `python/tilus/transforms/dead_code_elimination.py` | DCE 全部实现，自定义 Pass 的最佳范例（含 `IRVisitor` 收集 + `IRRewriter` 改写）。 |
| `python/tilus/ir/functors/functor.py` | `IRFunctor`/`IRRewriter`/`IRVisitor`——遍历与改写 IR 的访问者骨架。 |
| `python/tilus/ir/inst.py` | `Instruction` 基类——新增指令的根基（`output/inputs/attributes`）。 |
| `python/tilus/ir/instructions/generic.py` | `CastInst`/`AddInst` 等指令定义——新增 IR 指令的范例。 |
| `python/tilus/ir/layout/inference/rule.py` | `LayoutInferenceRule`/`register_rule`——布局推理规则的注册机制。 |
| `python/tilus/backends/emitter.py` | `BaseInstEmitter`/`register_emitter`/`REGISTRY`——发射器与注册。 |
| `python/tilus/backends/emitters/cast.py` | `CastInst` 的真实发射器——新增发射器的范例。 |
| `python/tilus/transforms/instruments/dump_ir.py` | `DumpIRInstrument`——验证 Pass 是否进入流水线的工具。 |
| `python/tilus/drivers.py` | `optimize_program`——把 `get_default_passes` 与 `apply_transforms` 接起来的地方。 |
| `tests/transforms/test_dead_code_elimination.py` | 端到端测试范式：手工构造 IR + 断言指令数量。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 自定义 Pass**、**4.2 新增指令的四件套**、**4.3 端到端测试范式**。

### 4.1 自定义 Pass：Pass 框架与 IRRewriter

#### 4.1.1 概念说明

「Pass（变换遍）」是对整个 `Program` 做一次结构到结构变换的单元。Tilus 的 Pass 模型极简：

- 一个 Pass 对每个 `Function` 跑一次（`process_function`），把所有函数的结果汇总成一个新 `Program`（`process_program`）。
- Pass **本身无状态地持有「改」的能力**，真正遍历 IR 的工作委托给访问者 `IRRewriter`（要改）或 `IRVisitor`（只看）。
- Pass 不直接被调用，而是被装进一个列表，由 `apply_transforms` 顺序执行；执行前后还会回调 `PassContext` 里的仪器（instrument），`DumpIRInstrument` 就是这样一个仪器。

写自定义 Pass，本质上就是回答三个问题：**遍历什么、改写什么、挂在哪里**。

#### 4.1.2 核心流程

一个「先收集信息、再改写 IR」的 Pass，典型流程是两遍走（DCE 就是这个套路）：

```
Pass.process_function(func):
    ┌─ Pass 1（IRVisitor 只读遍历）─────────────────┐
    │  collector.visit(func)        # 遍历整棵语句树  │
    │  collector.propagate()        # 不动点求所需信息 │
    └────────────────────────────────────────────────┘
    if 没有可改的东西:
        return func                 # 短路：原样返回
    ┌─ Pass 2（IRRewriter 改写）─────────────────────┐
    │  rewriter = MyRewriter(collector.info)         │
    │  return rewriter.visit(func)  # 返回新 Function │
    └────────────────────────────────────────────────┘
```

两个细节决定了这套流程能跑通：

1. **身份短路**：`process_program` 在汇总后，如果所有函数都「未变」（用 `is` 判断），就原样返回原 `Program`；`IRRewriter` 的每个 `visit_*` 也用 `is` 判断子节点是否变化，未变就返回原对象。这让「无所事事的 Pass」几乎零开销。
2. **指令删除的标准入口**：`IRRewriter.visit_InstStmt` 规定——当 `visit(stmt.inst)` 返回 `None` 时，这条 `InstStmt` 会被塌缩成空的 `SeqStmt(())`。所以「删除一条指令」=「让 `visit_Instruction` 对它返回 `None`」。

#### 4.1.3 源码精读

**(a) Pass 基类与身份短路**

[`python/tilus/transforms/base.py:68-83`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/base.py#L68-L83) 定义了 `Pass`：构造时把类名去掉 `Pass` 后缀作为 `name`（DCE 类 `DeadCodeEliminationPass` → 名字 `"DeadCodeElimination"`，这个名字会出现在 dump 的文件名里）；`process_program` 对每个函数调 `process_function`，并用 `all(a is b ...)` 判断是否真的改过。

```python
class Pass:
    def __init__(self) -> None:
        self.name: str = self.__class__.__name__.removesuffix("Pass")

    def process_program(self, program: Program) -> Program:
        functions = {name: self.process_function(func) for name, func in program.functions.items()}
        if all(a is b for a, b in zip(functions.values(), program.functions.values())):
            return program          # 全部未变 → 原样返回
        else:
            return Program.create(functions)

    def process_function(self, function: Function) -> Function:
        raise NotImplementedError()  # 子类必须实现
```

要点：`process_function` 是唯一必须重写的方法；只要你的 Pass 对某个函数返回了「不同的对象」，`process_program` 就会构造新 `Program`。

**(b) apply_transforms：Pass 的运行器与仪器回调**

[`python/tilus/transforms/base.py:86-112`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/base.py#L86-L112) 是 Pass 列表的执行引擎：取当前 `PassContext`，在每个 Pass 前后回调仪器的 `before_pass`/`after_pass`。

```python
def apply_transforms(prog, transforms):
    ctx = PassContext.current()
    ctx.before_all_passes(prog)
    for transform in transforms:
        ctx.before_pass(transform.name, prog)
        prog = transform(prog)          # Pass.__call__ → process_program
        ctx.after_pass(transform.name, prog)
    ctx.after_all_passes(prog)
    return prog
```

注意 `PassContext.current()`：没有显式 `with PassContext()` 时返回一个空默认上下文（[`base.py:60-65`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/base.py#L60-L65)），此时没有仪器、什么都不落盘。要让 `dump_ir` 生效，必须先 `ctx.dump_ir(path)` 把 `DumpIRInstrument` 塞进 `ctx.instruments`（[`base.py:57-58`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/base.py#L57-L58)）。

**(c) DCE：自定义 Pass 的最佳范例**

DCE 把「两遍走」用得淋漓尽致。第一遍 [`UsedTensorCollector(IRVisitor)`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/dead_code_elimination.py#L136-L229) 收集「哪些张量被用到」，关键在 `visit_Instruction`：功能指令记入待判定列表、副作用指令的 inputs 直接标记为已用。

```python
def visit_Instruction(self, inst):
    if _is_functional(inst):
        self.functional_insts.append(inst)
    else:
        for tensor in inst.inputs:        # 副作用指令：输入必活
            self.used_tensors.add(id(tensor))
```

随后 [`propagate()`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/dead_code_elimination.py#L213-L229) 做不动点：只要某功能指令的 `output` 在 `used_tensors` 里，就把它的 `inputs` 也标记为已用，反复直到收敛。这是「逆向活跃性传播」。

第二遍 [`DeadCodeEliminator(IRRewriter)`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/dead_code_elimination.py#L232-L264) 真正改写：功能指令产出无人用就返回 `None`（删）；副作用指令产出无人用就用 `dataclasses.replace(inst, output=None)` 改写（留副作用、弃产出寄存器）。

```python
def visit_Instruction(self, inst):
    if _is_functional(inst) and inst.output is not None and id(inst.output) not in self.used_tensors:
        return None                      # 删除整条指令
    if (_is_side_effecting_with_optional_output(inst) ...):
        return dataclasses.replace(inst, output=None)   # 保留副作用，弃产出
    return super().visit_Instruction(inst)
```

最后由 [`DeadCodeEliminationPass.process_function`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/dead_code_elimination.py#L267-L292) 把两遍串起来，并在 `has_dead` 为假时直接 `return function`（短路）。工厂函数 [`dead_code_elimination_pass()`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/dead_code_elimination.py#L295-L296) 返回实例，这是注册到流水线的标准形态。

**(d) IRRewriter 的「免费」指令改写**

如果你只是想「把指令的某些属性换成新的」，连 `visit_Instruction` 都不用自己写——基类 [`IRRewriter.visit_Instruction`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/functors/functor.py#L453-L465) 已经递归改写 `output/inputs/attributes` 三段，全都没变就返回原对象，否则用 `dataclasses.replace` 生成新指令：

```python
def visit_Instruction(self, inst):
    output = self.visit(inst.output)
    inputs = self.visit(inst.inputs)
    attributes = {key: self.visit(value) for key, value in inst.attributes.items()}
    if output is inst.output and inputs is inst.inputs and all(...):
        return inst                      # 未变
    return dataclasses.replace(inst, output=output, inputs=inputs, **attributes)
```

而 [`visit_InstStmt`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/functors/functor.py#L305-L314) 负责把指令改写结果包回语句：指令返回 `None` 就塌缩成空 `SeqStmt(())`，返回 `Instruction` 就重新包成 `InstStmt`。

#### 4.1.4 代码实践

**实践目标**：写一个只读 Pass `DotCountPass`，统计每个函数里 `DotInst`（矩阵乘指令）的数量并打印；把它追加进默认流水线，用 `dump_ir` 验证它出现在流水线末尾。

**操作步骤**：把下面这段代码存成 `dotcount_demo.py`（示例代码，非项目原有文件）：

```python
# 示例代码：一个只读的自定义 Pass
from tilus.ir.func import Function
from tilus.ir.functors import IRVisitor
from tilus.ir.inst import Instruction
from tilus.ir.instructions.cuda.mma_dot import DotInst   # 块级 dot 在 IR 里是 DotInst
from tilus.transforms.base import Pass


class DotCountVisitor(IRVisitor):
    def __init__(self):
        super().__init__()
        self.count = 0

    def visit_Instruction(self, inst: Instruction) -> None:
        if isinstance(inst, DotInst):
            self.count += 1


class DotCountPass(Pass):
    def process_function(self, function: Function) -> Function:
        v = DotCountVisitor()
        v.visit(function)
        print(f"[DotCountPass] {function.name}: {v.count} DotInst")
        return function          # 只读 Pass：原样返回，不触发重写
```

要点对照源码：`DotCountPass` 继承 `Pass`、只实现 `process_function`、原样返回 `function`（身份短路保证零开销）；`DotCountVisitor` 继承 `IRVisitor`、重写 `visit_Instruction`、用 `isinstance` 过滤——这正是 `UsedTensorCollector` 与 `InstructionCollector` 的同款写法。如果你只是想数指令，其实可以直接用现成的 `collect_instructions(func)`（[`instruction_collector.py:31-34`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tools/instruction_collector.py#L31-L34)），自己写 visitor 是为了演示机制。

**把它挂进流水线并验证**。下面这段（示例代码）在不修改任何源码的前提下，把 `DotCountPass` 追加到默认流水线末尾，并打开 `dump_ir`：

```python
# 示例代码：非侵入式地把自定义 Pass 接入流水线
from pathlib import Path
import tilus
from tilus.transforms import get_default_passes, PassContext, apply_transforms

tilus.option.cache_dir("/tmp/tilus-dotcount")

# 拿一个真实转译好的 matmul Program（Script 的 __call__ 参数请对照 examples/matmul 对应文件）
# 下面这行需要在能 import 到该 Script 的环境里运行，具体实参签名待本地验证：
# prog = SomeMatmulScript(...)._jit_instance_for(*args).transpiled_programs[0]

dump_dir = Path("/tmp/tilus-dotcount/ir")
with PassContext() as ctx:
    ctx.dump_ir(dump_dir)                                  # 注入 DumpIRInstrument
    apply_transforms(prog, get_default_passes() + [DotCountPass()])
```

**需要观察的现象**：

1. 控制台应打印类似 `[DotCountPass] launch: N DotInst`（`N` 取决于该 matmul 的 K 维分块数）。
2. `dump_dir` 目录下会出现一串 `0_Original.txt`、`1_DeclareToLet.txt`、…、`12_DeadCodeElimination.txt`，最后多出一个 `13_DotCount.txt`——这个文件的出现就证明你的 Pass 被加入了流水线。文件名里的 `DotCount` 正是 `Pass.name`（类名 `DotCountPass` 去掉 `Pass` 后缀），命名规则来自 [`DumpIRInstrument.after_pass`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/instruments/dump_ir.py#L53-L63)。

> 注：上面对真实 matmul `Script` 的调用，其指针/尺寸实参须与对应示例文件的 `__call__` 签名一致，具体参数**待本地验证**。若只想快速验证 Pass 机制而手边没有合适的 `Script`，可改用 4.3.4 里手工构造 `DotInst` 的方式，无需 GPU。

**关于「真正进生产流水线」**：上面的 `get_default_passes() + [DotCountPass()]` 是非侵入式的临时接入。若要让「正常 `kernel(...)` 调用」也触发它，需要把 `DotCountPass()` 加进 [`get_default_passes()`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/__init__.py#L31-L45) 返回的列表（这会修改项目源码，仅建议在你自己的 fork 里做）。一个现成的「运行时追加 Pass」范例是 `optimize_program`：当 `options.debug_block` 非空时，它把 `inject_print_instruction_pass(...)` 追加到默认列表末尾（[`drivers.py:84-87`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py#L84-L87)）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `DotCountPass.process_function` 改成 `return function`（原样返回），`apply_transforms` 之后整个 `Program` 对象会不会变？为什么？

> **答案**：不会变。`process_function` 原样返回同一个 `function` 对象，`process_program` 里 `all(a is b ...)` 为真，于是原样返回原 `Program`（[`base.py:75-80`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/base.py#L75-L80)）。这正是只读 Pass 零开销的来源。

**练习 2**：DCE 为什么用 `IRVisitor` 做第一遍、`IRRewriter` 做第二遍，而不是一遍 `IRRewriter` 搞定？

> **答案**：因为删除一条功能指令，可能让它上游的指令也变成死代码（连锁删除）。这需要先求出完整的活跃集合（不动点），再据此一次性改写。若边遍历边删，访问顺序会影响结果，且 `IRRewriter` 的 `memo` 记忆化会让同一节点不会被二次处理，难以表达「删 A 后才知 B 也该删」。

---

### 4.2 新增指令的四件套

#### 4.2.1 概念说明

Tilus 的指令从「在 IR 里被定义」到「能在生成的 CUDA 里跑起来」，要穿过四个相互独立、各自挂载到全局注册表的环节。把它们叫「四件套」：

1. **IR 定义**：一个 `Instruction` 子类（frozen dataclass）+ 一个 `create(...)` 工厂。它决定指令在 IR 树里长什么样、有哪些输入输出与属性。
2. **布局推理/验证规则**：一条 `LayoutInferenceRule`（可选多条）+ 一条 `LayoutValidationRule`，用 `@register_rule(YourInst)` 挂表。它告诉布局推理引擎「这条指令的输入输出布局如何互相推导」。
3. **发射器**：一个 `BaseInstEmitter` 子类，实现 `emit(inst)`。它把这条指令翻译成 Hidet IR（最终落到 CUDA C/PTX）。
4. **发射器注册**：用 `@register_emitter(YourInst, target=...)` 把发射器挂进 `REGISTRY`，并指明它适用于哪个 GPU target。

四者缺一：IR 有定义但没布局规则 → 布局推理报错；有布局规则但没发射器 → codegen 阶段 `resolve_inst_emitter` 找不到发射器而报错；有发射器但没注册 → 同样找不到。此外，若你希望该指令可被 DCE 当作「纯计算」处理，还要把它加进 `FUNCTIONAL_INST_TYPES`（这是「第五个」可选挂载点，见 4.2.4）。

#### 4.2.2 核心流程

新增一条指令的全流程：

```
用户在 __call__ 里写 self.your_op(...)
            │  (转译器把它包成 InstStmt)
            ▼
   ┌─ ① IR 定义 ──────────────┐
   │  YourInst(Instruction)    │  ← output / inputs / attributes
   │  + YourInst.create(...)   │
   └───────────────────────────┘
            │  (布局推理阶段)
            ▼
   ┌─ ② 布局规则 ──────────────────────────────┐
   │  @register_rule(YourInst)                  │
   │  class YourRule(LayoutInferenceRule): ...  │  ← 输入输出布局互推
   │  @register_rule(YourInst)                  │
   │  class YourVal(LayoutValidationRule): ...  │  ← 事后校验相容性
   └─────────────────────────────────────────────┘
            │  (codegen 阶段)
            ▼
   ┌─ ③ 发射器 ───────────────────────────────┐
   │  class YourEmitter(BaseInstEmitter):      │
   │      def emit(self, inst): ...            │  ← 翻译成 Hidet IR
   └────────────────────────────────────────────┘
            │  (import 期自动登记)
            ▼
   ┌─ ④ 发射器注册 ─────────────────────────────┐
   │  @register_emitter(YourInst, target=nvgpu) │  ← 写进 REGISTRY
   │  class YourEmitter(...): ...               │
   └─────────────────────────────────────────────┘
            │
            ▼
   生成的 source.cu 里出现对应的 CUDA/PTX
```

注意 ③ 和 ④ 通常合并在同一个文件里写：`@register_emitter(...)` 直接装饰 `YourEmitter` 类，import 该文件时登记就自动完成。

#### 4.2.3 源码精读

**(a) IR 定义：Instruction 基类与 CastInst**

[`python/tilus/ir/inst.py:24-96`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/inst.py#L24-L96) 是所有指令的根基：`@dataclass(frozen=True, eq=False)`、`output` 可空、`inputs` 是固定 tuple，并提供 `register_output`/`shared_input` 等类型化访问器。`attributes` 是一个动态属性，自动收集除 `output`/`inputs` 外的所有字段：

```python
@property
def attributes(self) -> dict[str, Any]:
    attrs = {}
    for k, v in self.__dict__.items():
        if k in ["output", "inputs"]:
            continue
        attrs[k] = v
    return attrs
```

这就是为什么通用变换器（如 `IRRewriter.visit_Instruction`）和 DCE 都能用 `inst.attributes` 扫描到指令里藏的 Hidet `Var`、偏移表达式等标量信息。

一条最小指令的定义范例是 [`CastInst`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/instructions/generic.py#L300-L306)：仅有一个输入 `x`、一个输出，没有额外属性，配一个 `create` 工厂：

```python
class CastInst(Instruction):
    @staticmethod
    def create(x: RegisterTensor, output: RegisterTensor) -> CastInst:
        return CastInst(output=output, inputs=(x,))
```

带运算语义的指令（如 [`AddInst`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/instructions/generic.py#L390-L396)）还会多一个 `f_compute`，供 elementwise 发射器复用，但 IR 层面它仍是普通属性。

**(b) 布局推理规则：register_rule**

[`python/tilus/ir/layout/inference/rule.py:56-88`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/inference/rule.py#L56-L88) 定义 `LayoutInferenceRule.inference(ctx, inst)`：返回「张量 → 布局」的映射，表示「这次我能为这些张量补上布局」，补不了就返回空 dict。注册靠 [`register_rule(inst_type)`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/inference/rule.py#L100-L112)：它根据传入类是 `LayoutInferenceRule` 还是 `LayoutValidationRule` 分别塞进两个全局字典（`_inference_rules` 允许多条、`_validation_rules` 只允许一条，重复登记会报错）。

最简单的规则是「一元指令输入输出布局相同」，见 [`UnaryRule`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/inference/inference_rules/elementwise_unary.py#L21-L36)，它同时为 `CastInst` 和 `ElementwiseUnaryInst` 注册——注意装饰器可以叠放：

```python
@register_rule(ElementwiseUnaryInst)
@register_rule(CastInst)
class UnaryRule(LayoutInferenceRule):
    @staticmethod
    def inference(ctx, inst):
        x = inst.register_input
        y = inst.register_output
        if x.optional_layout is not None and y.optional_layout is not None:
            return {}
        elif x.optional_layout is not None:
            return {y: x.layout}      # 前向：输入推输出
        elif y.optional_layout is not None:
            return {x: y.layout}      # 反向：输出推输入
        else:
            return {}                 # 两边都没布局，留给别的规则
```

查表时 [`get_inference_rules`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/inference/rule.py#L115-L131) 会沿 `__mro__` 继承父类的规则，所以子类指令自动复用父类规则（如所有 `ElementwiseUnaryBaseInst` 子类都能用基类注册的规则）。

**(c) 发射器：BaseInstEmitter**

[`python/tilus/backends/emitter.py:34-256`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitter.py#L34-L256) 是发射器基类，继承 `StmtBuilder`，本身近乎无状态——跨指令的共享状态都挂在 `FunctionCodegen` 上（通过 `self._codegen` 访问）。它提供两类通用能力：

- `get_or_allocate_var(tensor)`（[`emitter.py:81-100`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitter.py#L81-L100)）：把一个张量惰性映射到一个 Hidet 变量（寄存器张量映射成 `tensor_var`、共享/全局张量映射成 `tensor_pointer_var`），首次访问时声明、之后复用。这是「张量世界 → 标量变量世界」的桥梁。
- 一堆只读属性：`current_num_threads`、`tensor2var`、`contexts`、`num_warps`、`analysis` 等，让发射器能查到当前线程组规模、已分配的变量、`EmitContexts`、标量分析结果。

子类只需实现 `emit(self, inst)`（[`emitter.py:255-256`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitter.py#L255-L256)）。

**CastInst 的发射器**是一个极佳范例。其基类 [`CastInstBaseEmitter.emit`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cast.py#L73-L94) 先为输出张量分配变量、登记进 `tensor2var`，再按 `(src_dtype, dst_dtype)` 查「特化表」选实现，查不到就用通用的逐元素 `cast_generic`：

```python
def emit(self, inst: CastInst) -> None:
    src = inst.inputs[0]
    dst = inst.register_output
    self.size = dst.local_size
    var = self.declare(tensor_var("casted_{}".format(dst.dtype.short_name), ...))
    self.tensor2var[dst] = var          # 输出张量 ↔ 变量
    ...
    if (src_dtype, dst_dtype) in self.specialized_cast:
        impl = self.specialized_cast[(src_dtype, dst_dtype)]
    else:
        impl = self.cast_generic
    impl(src_var, dst_var)
```

**(d) 发射器注册：register_emitter**

[`python/tilus/backends/emitter.py:259-283`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitter.py#L259-L283) 的 `register_emitter(inst_cls, *, target=None)` 是装饰器工厂：把「指令类 → {target → 发射器类}」写进全局 `REGISTRY`（[`emitter.py:36`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitter.py#L36)），`target` 默认 `gpgpu_any`（所有 GPU），同一 `(指令, target)` 重复登记会报错。

[`NvgpuCastInstEmitter`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cast.py#L101-L121) 用 `@register_emitter(CastInst, target=nvgpu_any)` 登记，并在构造时把一堆 PTX 向量化 cast（`prmt`/`lop3`/`fma_f16x2` 等）填进特化表；而 [`AmdgpuCastInstEmitter`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cast.py#L499-L503) 用 `target=amdgpu_any` 登记同一指令的另一份实现。这就是「同一指令在不同 target 上挂不同发射器」的样板。

#### 4.2.4 代码实践

**实践目标**：以 `CastInst` 为参照，把「四件套」逐个在源码里定位，形成一张「新增指令清单」。

**操作步骤**：按下表逐项打开文件、确认行号，理解每一件套的真实形态。

| 件套 | 文件 | 关键代码 |
| --- | --- | --- |
| ① IR 定义 | `python/tilus/ir/instructions/generic.py` | `CastInst` 与 `CastInst.create` |
| ② 布局规则 | `python/tilus/ir/layout/inference/inference_rules/elementwise_unary.py` | `@register_rule(CastInst)` + `UnaryRule` |
| ③ 发射器 | `python/tilus/backends/emitters/cast.py` | `CastInstBaseEmitter.emit` |
| ④ 发射器注册 | `python/tilus/backends/emitters/cast.py` | `@register_emitter(CastInst, target=nvgpu_any)` |

**需要观察的现象与预期结果**：

1. 在 `cast.py` 里搜 `register_emitter`，应看到 `CastInst` 至少被登记了两次（`nvgpu_any` 与 `amdgpu_any`），印证「按 target 分发」。
2. `CastInst` 没有出现在 `FUNCTIONAL_INST_TYPES` 里？其实它通过基类 `ElementwiseUnaryBaseInst`……不对——仔细看 [`FUNCTIONAL_INST_TYPES`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/dead_code_elimination.py#L76-L113) 会发现它直接显式列出了 `CastInst`。结论：判断「功能 vs 副作用」走的是这个**显式白名单**，而非自动推断。因此**新增一条纯计算指令时，若希望它可被 DCE 消除，必须手动把它（或其基类）加进这个白名单**——这是四件套之外的「第五个挂载点」。
3. `CastInst` 的布局规则 `UnaryRule` 同时服务 `CastInst` 和 `ElementwiseUnaryInst`，印证「一条规则可服务多条指令、靠 `@register_rule` 叠放」。

> 这一步是源码阅读型实践，不涉及运行；结论「白名单需手动维护」是新增指令时最容易遗漏的点。

#### 4.2.5 小练习与答案

**练习 1**：你新增了一条纯计算指令 `FooInst`，IR 定义和发射器都写好了，也注册了发射器，但运行时报「找不到布局验证规则」。漏了哪一件套？

> **答案**：漏了 ② 中的 `LayoutValidationRule`。每条指令**必须**有且仅有一条验证规则（`_validation_rules` 不允许缺失，[`rule.py:134-150`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/inference/rule.py#L134-L150) 会在缺失时抛错）。推理规则（`LayoutInferenceRule`）可以是零条或多条，但验证规则不可少。

**练习 2**：为什么 `register_emitter` 对同一 `(指令, target)` 重复登记会报错，而 `register_rule` 的推理规则允许同一指令挂多条？

> **答案**：发射器是「给定 target 下的唯一实现」，二义会让人无法决定生成哪份代码，故必须唯一；布局推理规则是「启发式线索」，多条规则按全局优先级排队、各负责补一部分张量的布局（[`inference.py`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/inference/inference.py) 用不动点迭代挑第一条能推进的），多一条不影响确定性，反而能覆盖更多情形。

---

### 4.3 端到端测试范式

#### 4.3.1 概念说明

写完 Pass 或指令后，如何验证它对？Tilus 提供两个层次的测试范式，对应两类「输入 IR」的来源：

- **单元级（手工构造 IR）**：直接用 `Function.create`、`SeqStmt`、`InstStmt` 和指令 `create()` 方法拼出一个极小 `Function`，断言 Pass 跑完后的指令数量/形态。优点是不依赖 GPU、不依赖转译器，输入完全可控。`tests/transforms/test_dead_code_elimination.py` 是标准范例。
- **集成级（真实转译产物）**：从一个真实的 `tilus.Script` 出发，用 `InstantiatedScript._jit_instance_for(*args)` 拿到 `JitInstance`，再取 `.transpiled_programs[0]` 得到转译好的 `Program`，对其跑 `verify`、`collect_instructions` 或你的 Pass。这能验证「从用户代码到 IR」整条链，常用于校验器（verifier）测试。

CLAUDE.md 里把这两条总结为：「Build test IR directly using `Function.create(...)` … See `tests/transforms/test_dead_code_elimination.py` for examples」以及「use `InstantiatedScript._jit_instance_for(...)` to get a `JitInstance`, then access `ji.transpiled_programs[0]` for the `Program`」。

#### 4.3.2 核心流程

两条范式的调用骨架：

```
# 单元级
_make_function([inst1, inst2, ...])        # 拼 SeqStmt(InstStmt(inst) ...)
  → Program
opt_prog = your_pass()(prog)
assert _count_insts(opt_prog, SomeInst) == N   # 用 collect_instructions 统计

# 集成级
script = YourScript(...)
ji = script._jit_instance_for(*args)        # JitInstance（不编译，只转译）
ji.programs()                               # 触发转译/编译（按需）
prog = ji.transpiled_programs[0]            # 取第一份转译产物
verify(prog) / your_pass()(prog) / collect_instructions(func)
```

#### 4.3.3 源码精读

**(a) 单元级：拼 IR + 断言指令数**

[`tests/transforms/test_dead_code_elimination.py`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/tests/transforms/test_dead_code_elimination.py#L47-L72) 提供了三个测试夹具，是写 Pass 测试的模板：

- [`_make_function(insts)`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/tests/transforms/test_dead_code_elimination.py#L47-L61)：把一串指令包成 `SeqStmt(tuple(InstStmt(inst) for inst in insts))`，配上最小合法 `Metadata`（grid/cluster/num_warps 等），用 `Function.create` 产出函数。
- [`_make_program(insts)`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/tests/transforms/test_dead_code_elimination.py#L64-L66)：再包一层成单函数 `Program`。
- [`_count_insts(program, inst_type)`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/tests/transforms/test_dead_code_elimination.py#L69-L72)：用 `collect_instructions(func)` 取出全部指令，按类型计数。

一个典型用例 [`test_eliminate_unused_add`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/tests/transforms/test_dead_code_elimination.py#L88-L101)：构造「产出无人用的 `AddInst`」，断言 DCE 跑完后 `AddInst` 数从 1 变 0、而副作用指令 `StoreGlobalGenericInst` 仍为 1：

```python
def test_eliminate_unused_add():
    alloc_a, a = _alloc()
    alloc_b, b = _alloc()
    out_add = RegisterTensor.create(dtype=float32, shape=(4,))
    add_inst = AddInst.create(a, b, out_add)
    store = _store(a)            # 只用了 a，没用 add 的产出
    prog = _make_program([alloc_a, alloc_b, add_inst, store])
    assert _count_insts(prog, AddInst) == 1
    opt_prog = dead_code_elimination_pass()(prog)
    assert _count_insts(opt_prog, AddInst) == 0
    assert _count_insts(opt_prog, StoreGlobalGenericInst) == 1
```

这个三段式（构造 → 跑 Pass → 断言计数）就是写任意 Pass 单元测试的标准骨架。

**(b) 集成级：_jit_instance_for + transpiled_programs**

[`InstantiatedScript._jit_instance_for`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L887-L904) 接收 `__call__` 的实参，绑定参数、计算 `jit_key`，返回或新建一个 `JitInstance`——它**只做转译与编译的准备工作，本身可廉价反复调用**：

```python
def _jit_instance_for(self, *args, **kwargs) -> JitInstance:
    ...
    jit_key, _ = extract_keys(args, self.const_params, self.tuning_params)
    jit_instance = self.jit_instances.get(jit_key, None)
    if jit_instance is None:
        jit_instance = JitInstance(self.script_cls, self.params, self.build_options, self.schedules, jit_key)
        self.jit_instances[jit_key] = jit_instance
    return jit_instance
```

`JitInstance` 上 [`transpiled_programs`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L388-L395) 是一个 `list[Program]`（每个 schedule 一份转译产物）。真实测试里的用法见 [`tests/ir/tools/verifier/test_verify_load_shared.py:44`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/tests/ir/tools/verifier/test_verify_load_shared.py#L44)：

```python
program = script._jit_instance_for().transpiled_programs[0]
verify(program)
```

注意：对需要指针/尺寸实参的 `Script`（如 matmul），`_jit_instance_for()` 要传入匹配签名的参数；仅当 `__call__` 全部参数有默认值时才能空参调用（如上面的 `DemoLoadShared`）。

#### 4.3.4 代码实践

**实践目标**：用单元级范式为 4.1 的 `DotCountPass` 写一个不依赖 GPU 的测试，验证它能正确数出 `DotInst`。

**操作步骤**：仿照 `test_dead_code_elimination.py` 的夹具，写如下测试（示例代码）。关键是用 `DotInst.create(...)` 拼出含 dot 的 IR——`DotInst` 的 `create` 签名需以源码为准，下面给出结构示意，具体参数**待本地确认**：

```python
# 示例代码：为 DotCountPass 写单元测试
from tilus.ir.instructions.cuda.mma_dot import DotInst
from tilus.ir.tensor import RegisterTensor
from tilus.hidet.ir.dtypes import float16, float32
# 复用 test_dead_code_elimination.py 里的 _make_function / _make_program 模式

def test_dot_count_pass_counts_dot():
    # 1) 构造：a, b 为 fp16 输入，acc 为 fp32 累加器（shape/布局需与 DotInst 要求一致）
    a = RegisterTensor.create(dtype=float16, shape=(64, 16))   # shape 待确认
    b = RegisterTensor.create(dtype=float16, shape=(16, 64))   # shape 待确认
    acc = RegisterTensor.create(dtype=float32, shape=(64, 64)) # shape 待确认
    dot = DotInst.create(a=a, b=b, c=acc, out=acc)             # 签名以源码为准
    # 2) 跑 Pass
    prog = _make_program([dot])
    # 3) 断言：DotCountPass 不改 IR，但应数到 1 个 DotInst
    new_prog = DotCountPass()(prog)
    assert new_prog is prog                                     # 只读 Pass：身份相等
```

**需要观察的现象与预期结果**：

1. `DotCountPass()(prog)` 应原样返回同一个 `prog` 对象（`assert new_prog is prog` 通过）——再次验证只读 Pass 的身份短路。
2. 运行时控制台应打印 `[DotCountPass] test: 1 DotInst`。
3. 若 `DotInst.create` 的参数名/张量 shape 与源码不符，会在构造阶段报 `InstructionError` 或断言失败——此时打开 `python/tilus/ir/instructions/cuda/mma_dot.py` 核对 `create` 签名与约束。

> `DotInst.create` 的确切签名与本测试的 shape 合法性**待本地验证**；本实践的重点是掌握「拼 IR → 跑 Pass → 断言」的三段式骨架，而非 dot 的具体几何。

#### 4.3.5 小练习与答案

**练习 1**：单元级测试里为什么强调 `assert new_prog is prog`（用 `is` 而非 `==`）？

> **答案**：Tilus IR 节点是 `eq=False` 的 frozen dataclass，`__eq__`/`__hash__` 都基于 `id`，`==` 和 `is` 行为一致但 `is` 更直白地表达「同一个对象」的语义。对只读 Pass 而言，`is` 成立意味着 `process_program` 的身份短路生效、Pass 没有产生任何新对象，这正是我们想断言的「零副作用」。

**练习 2**：`_jit_instance_for(...)` 与直接 `kernel(...)` 调用有什么区别？为什么要专门用它做测试？

> **答案**：`kernel(...)` 会一路走到编译、加载 `.so`、启动 CUDA 内核，依赖 GPU 且慢；`_jit_instance_for(...)` 只到「转译出 `Program`」为止（取 `.transpiled_programs[0]`），不一定要真正编译运行（按需调用 `.programs()` 才触发编译）。测试校验器、布局推理、自定义 Pass 时，往往只需要 IR 这一层产物，用它可以脱离 GPU、快速、确定性地拿到真实转译结果。

---

## 5. 综合实践

把本讲三个最小模块串成一个完整的二次开发小任务：**为 Tilus 新增一个「只读统计 Pass」并完成从实现到测试到流水线验证的全流程**。

1. **实现**（对应 4.1）：写 `DotCountPass`，用 `IRVisitor` 统计 `DotInst`，打印计数，`process_function` 原样返回。
2. **单元测试**（对应 4.3）：仿照 `test_dead_code_elimination.py`，手工构造一个含 `DotInst` 的 `Function`，断言 `DotCountPass()(prog) is prog` 且打印出正确计数。
3. **流水线验证**（对应 4.1 + 4.3）：用 `apply_transforms(real_prog, get_default_passes() + [DotCountPass()])` 配合 `PassContext.dump_ir(...)`，在落盘目录里确认多出了 `N_DotCount.txt`；其中 `real_prog` 用 `SomeMatmulScript(...)._jit_instance_for(*args).transpiled_programs[0]` 获取（参数**待本地验证**）。
4. **横向对照**（对应 4.2）：打开 `CastInst` 的四件套（IR 定义 / `@register_rule` / `emit` / `@register_emitter`），在笔记里画一张「如果我新增一条 `FooInst`，需要在哪四个文件动刀」的清单，并标注「若它是纯计算，还要加进 `FUNCTIONAL_INST_TYPES`」这个第五挂载点。

完成上述四步后，你就具备了给 Tilus 做 Pass 级与指令级扩展的完整肌肉记忆。

## 6. 本讲小结

- 自定义 Pass = 继承 `Pass`、实现 `process_function`、用 `IRVisitor` 收集 / `IRRewriter` 改写；`process_program` 与 `visit_*` 的身份短路让只读 Pass 零开销。
- 删除一条指令的标准入口是让 `IRRewriter.visit_Instruction` 对它返回 `None`（塌缩成空 `SeqStmt`）；改写属性则可白嫖基类 `visit_Instruction` 的 `dataclasses.replace`。
- Pass 的唯一生产入口是 `get_default_passes()`；非侵入式接入用 `get_default_passes() + [YourPass()]`，`optimize_program` 里追加 `inject_print_instruction_pass` 是现成范例。
- 新增一条指令需同步「四件套」：IR 定义（`Instruction` 子类 + `create`）、布局推理/验证规则（`@register_rule`）、发射器（`BaseInstEmitter.emit`）、发射器注册（`@register_emitter`）；纯计算指令还要手动加进 DCE 的 `FUNCTIONAL_INST_TYPES`。
- 端到端测试有两套范式：单元级用 `Function.create` + `collect_instructions` 断言（`test_dead_code_elimination.py` 为模板），集成级用 `script._jit_instance_for(*args).transpiled_programs[0]` 取真实转译产物。
- `Pass.name`（类名去掉 `Pass` 后缀）会出现在 `dump_ir` 落盘的文件名里，是验证「Pass 是否进入流水线」的最直接证据。

## 7. 下一步学习建议

本讲已覆盖手册全部八单元。接下来建议你用本讲教的方法做一次「真刀真枪」的扩展练习来巩固：

1. **写一个有实际改写效果的 Pass**：比如仿照 DCE，写一个「常量折叠」Pass（把两个常量输入的 `AddInst` 折叠成单个 `AllocateRegisterInst`），用单元级三段式测试验证。
2. **新增一条真实的自定义指令**：选一个简单的纯计算指令（如 `CopyInst`，语义等同 `CastInst` 同 dtype），把四件套逐个补齐，用 `_jit_instance_for` 跑通端到端、在缓存目录的 `source.cu` 里确认它生成的代码。
3. **继续精读的源码**：[`transforms/`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/__init__.py#L31-L45) 下其余 Pass（`bound_aware_simplify`、`let_propagation`）是进阶改写范式的富矿；[`backends/emitters/`](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitter.py#L259-L283) 下各类发射器则是理解「布局如何变成 PTX」的最佳教材。

至此，你已从「能用 Tilus」走到「能读懂、能改造 Tilus」。祝二次开发顺利。
