# Instruction 与 Tensor：IR 的两大主角

## 1. 本讲目标

上一讲（u3-l3）我们建立了 Tilus IR 的「骨架」：`Program / Function / Stmt` 这一层不可变的语句树。但骨架本身只是容器——真正承载语义的是挂在 `InstStmt` 叶子上的两类对象：

- **Instruction（指令）**：描述「做什么」——一次加载、一次加法、一次同步、一次张量核乘加。
- **Tensor（张量）**：描述「在谁之上做」——一块位于寄存器、共享内存、显存或张量内存上的数据。

本讲结束后，你应当能够：

1. 说清一条 `Instruction` 的三段式结构 `output / inputs / attributes`，并能用 `inst.attributes` 取出指令特有的附加参数。
2. 区分**功能指令（functional）**与**副作用指令（side-effecting）**，并解释为什么 Tilus 用一份**显式白名单**而非「`output is None`」来判定——这是死代码消除（DCE）能安全删指令的前提。
3. 理解四种 `Tensor`（Register / Shared / Global / TMemory）对应的 GPU 内存层次，以及它们采用的**身份相等（identity equality）**语义：两个字段完全相同的张量仍是两个不同的 IR 节点。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**编译器为什么需要「指令」和「张量」这两个概念？**
GPU 内核本质上是「把数据从一处搬到另一处、并对它做计算」。Tilus 把这两件事分别抽象成张量（数据在哪、长什么样）和指令（对数据做什么）。这样编译器就能分别处理：布局推理（layout inference）只关心张量，而死代码消除、代码生成只关心指令。职责分离是编译器 IR 的常见设计。

**什么是「纯计算」和「副作用」？**

- **纯计算（pure / functional）**：给定相同输入，永远产生相同输出，且不改变机器任何其他状态。例如 `a + b`——只要结果没人用，整条指令删掉程序行为不变。
- **副作用（side effect）**：会改变机器状态或必须按顺序发生。例如「写显存」「同步线程」「分配共享内存」。即便它的返回值没人用，这个「写/同步/分配」动作本身也必须发生，否则程序就错了。

这条「纯 vs 有副作用」的区分，是后面 DCE 删指令的唯一依据。

**什么是「身份相等」？**
普通 Python 对象默认按字段值比较：两个 `Point(x=1, y=2)` 用 `==` 比较为相等。但在编译器 IR 里，我们常常需要「两个长得一模一样的张量，仍然是两个不同的对象」——因为它们在程序里代表两份不同的存储。Tilus 让所有 IR 节点按**对象身份**（`id()`）判等，即 `a == b` 当且仅当 `a is b`。

> 如果上一讲的术语（`Function / body / SeqStmt / InstStmt`、frozen dataclass、`with_*` 更新范式）你已经熟悉，可直接进入第 4 节。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `python/tilus/ir/node.py` | 所有 IR 节点的基类 `IRNode`，定义了**身份相等**的 `__eq__` / `__hash__`，是本讲第三个模块的根基。 |
| `python/tilus/ir/inst.py` | `Instruction` 基类，定义 `output / inputs` 与 `attributes` 属性，以及一组类型化访问器。是本讲第一个模块的核心。 |
| `python/tilus/ir/tensor.py` | 四种张量 `RegisterTensor / SharedTensor / GlobalTensor / TMemoryTensor`，分别对应寄存器、共享内存、显存、张量内存。是本讲第三个模块的核心。 |
| `python/tilus/ir/instructions/generic.py` | 与架构无关的「通用指令」集合，本讲用它做功能/副作用的分类练习。 |
| `python/tilus/transforms/dead_code_elimination.py` | 死代码消除 Pass，内含**功能指令白名单** `FUNCTIONAL_INST_TYPES` 与 `_is_functional`，是第二个模块「白名单而非 output 判定」的直接证据。 |

## 4. 核心概念与源码讲解

### 4.1 Instruction 的三段式结构：output / inputs / attributes

#### 4.1.1 概念说明

一条 Tilus 指令可以统一地看成「**产出 → 消费 → 配置**」三段：

- **output（产出）**：指令计算后产出的张量，类型是 `Optional[Tensor]`。注意是 `Optional`——很多指令**没有产出**（例如写显存、同步），它们的 `output` 就是 `None`。
- **inputs（消费）**：指令读取的输入张量，固定是一个 tuple，元素是各种 `Tensor`。
- **attributes（配置）**：除了 output/inputs 之外，每条指令自带的「附加参数」。例如加载指令需要知道「从哪个偏移读」「读哪些维度」，这些不是张量，而是标量表达式和维度编号，它们构成了 attributes。

把所有指令统一成这个三段式的好处是：**通用的变换器（IRRewriter）可以不关心具体指令类型，统一地重写它们的 output/inputs/attributes**。这正是上一讲 CLAUDE.md 提到的 `visit_Instruction` 通用处理机制的基础。

#### 4.1.2 核心流程

指令对象的生命周期：

```text
用户在 __call__ 里写 self.load_global(...) 等调用
        │  (转译器 Transpiler 拦截)
        ▼
调用对应指令的 Instruction.create(...) 工厂方法
        │  create 负责校验形状/dtype，并填好 output/inputs/额外字段
        ▼
得到一个 frozen 的 Instruction 实例（带 output/inputs/attributes）
        │  (Transpiler 包装)
        ▼
InstStmt(inst)  ── 挂进 Function.body 语句树
        │  (后续 Pass 读取)
        ▼
用 inst.output / inst.inputs / inst.attributes 访问三段
```

关键点：每条具体指令通过一个 `@staticmethod create(...)` 工厂构造，工厂内部负责形状校验，并把「附加参数」存进指令自己的 dataclass 字段（例如 `LoadGlobalInst` 的 `offsets`、`dims`）。`attributes` 属性则是把这些额外字段**统一暴露成一个字典**，方便通用遍历。

#### 4.1.3 源码精读

`Instruction` 基类只声明了两个字段，却用一组属性方法撑起整个指令体系。

[python/tilus/ir/inst.py:24-27](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/inst.py#L24-L27) 定义了基类与两个核心字段：

```python
@dataclass(frozen=True, eq=False)
class Instruction(IRNode):
    output: Optional[Tensor]
    inputs: tuple[Tensor, ...]
```

- `frozen=True`：实例不可变，任何修改都要新建对象（上一讲的 `with_*` / `dataclasses.replace` 范式）。
- `eq=False`：不按字段值比较，改用身份相等（详见 4.3）。
- `output` 可为 `None`，`inputs` 永远是 tuple（哪怕空也写 `inputs=()`）。

为了在代码里少写 `isinstance` 断言，基类提供了一组**类型化访问器**，例如 [python/tilus/ir/inst.py:34-37](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/inst.py#L34-L37)：

```python
@property
def register_output(self) -> RegisterTensor:
    assert isinstance(self.output, RegisterTensor), self.output
    return self.output
```

同理还有 `shared_output`、`global_output`、`tmemory_output`，以及单输入版的 `register_input`、`shared_input`、`global_input`、`tmemory_input` 等（见 [python/tilus/ir/inst.py:44-87](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/inst.py#L44-L87)）。它们既起类型收窄作用，也充当运行时断言：写错了指令张量类型会立刻报错。

最巧妙的是 `attributes` 属性，见 [python/tilus/ir/inst.py:89-96](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/inst.py#L89-L96)：

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

它把 dataclass 的 `__dict__` 里**除 output/inputs 之外的所有字段**打包成字典。以加载指令为例，[python/tilus/ir/instructions/generic.py:70-77](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/instructions/generic.py#L70-L77)：

```python
class LoadGlobalInst(Instruction):
    offsets: tuple[Expr, ...]
    dims: tuple[int, ...]

    @staticmethod
    def create(x, offsets, dims, output) -> LoadGlobalInst:
        return LoadGlobalInst(output=output, inputs=(x,),
                              offsets=tuple(offsets), dims=tuple(dims))
```

对一条 `LoadGlobalInst` 调用 `inst.attributes` 就会得到 `{"offsets": (...), "dims": (...)}`。这正是 CLAUDE.md 反复强调「**指令的 attributes 里常藏着 Hidet 的 Var**」的原因——通用遍历器要扫描 `inst.attributes.values()` 才不会漏掉这些标量信息。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `output / inputs / attributes` 三段，体会 `create` 工厂如何把附加字段塞进 attributes。

**操作步骤**（源码阅读型 + 待本地验证的小脚本）：

1. 打开 `python/tilus/ir/instructions/generic.py`，挑三条指令对比它们 `create` 里给 `output` 传了什么：
   - `AddInst.create`（第 389 行附近）：`output=output`，有产出。
   - `StoreGlobalInst.create`（第 81 行附近）：`output=None`，无产出。
   - `LoadGlobalInst.create`（第 71 行附近）：`output=output`，且额外带 `offsets/dims`。
2. 写一段最小脚本（**示例代码**，需在已安装 tilus 的环境运行）：

   ```python
   from tilus.hidet.ir.dtypes import float32
   from tilus.ir.tensor import GlobalTensor, RegisterTensor
   from tilus.ir.layout import RegisterLayout
   from tilus.ir.instructions.generic import LoadGlobalInst, AddInst

   # 构造一个全局张量视图和一个寄存器张量作为输出
   g = GlobalTensor.create(float32, layout=__import__("tilus.ir.layout", fromlist=["GlobalLayout"]).GlobalLayout(...))  # 简化，实际需给 layout
   ```

   > 上述仅示意结构，完整 layout 构造较繁琐。更简单的做法是直接阅读 `tests/transforms/test_dead_code_elimination.py` 里如何用 `RegisterTensor.create(dtype, shape=(...))` 构造张量、再调用 `AddInst.create(x, y, output)` 构造指令。

3. 拿到一条 `AddInst` 实例 `inst` 后，打印 `inst.output`、`inst.inputs`、`inst.attributes`。

**需要观察的现象**：

- `inst.attributes` 对 `AddInst` 应为空字典 `{}`（它除 output/inputs 外没有额外字段）。
- 对 `LoadGlobalInst`，`inst.attributes` 应包含 `offsets` 与 `dims` 两个键。

**预期结果**：`AddInst` 的 attributes 为空，`LoadGlobalInst` 的 attributes 含偏移与维度信息。若你的环境无法直接构造 layout，可改为纯阅读 `generic.py` 中各 `create` 方法，手工列出每条指令的额外字段。（完整脚本运行结果：**待本地验证**。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `inputs` 永远是 tuple，即使指令没有输入也要写 `inputs=()`？

**参考答案**：保证类型统一。通用变换器可以无条件地用 `for t in inst.inputs` 遍历，不必先判 `None`；同时也让指令可哈希、可作为不可变 frozen dataclass 字段。`SyncThreadsInst.create` 就写成 `inputs=()`（见 [python/tilus/ir/instructions/generic.py:711-715](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/instructions/generic.py#L711-L715)）。

**练习 2**：`inst.attributes` 是怎么实现「自动收集所有附加字段」的？如果新增一条指令忘了把某字段写进 dataclass 会怎样？

**参考答案**：它遍历 `self.__dict__`，跳过 `output`/`inputs`，其余全部收进字典。新增指令的字段必须作为 dataclass 字段声明才会进入 `__dict__`；若用普通属性赋值（非 dataclass 字段），在 `frozen=True` 下会直接报错，因此不会「悄悄漏掉」。

---

### 4.2 功能指令 vs 副作用指令：白名单说了算

#### 4.2.1 概念说明

这是本讲最重要、也最容易踩坑的一个区分。先给出定义：

- **功能指令（functional）**：纯计算，产出全靠 inputs 决定，无任何副作用。例如 `AddInst`、`CastInst`、`SliceRegisterInst`、`DotInst`。它们的安全性极高：**如果产出张量没人用，整条指令删掉不影响程序**。
- **副作用指令（side-effecting）**：会对机器状态产生影响或必须按序执行。例如 `StoreGlobalInst`（写显存）、`SyncThreadsInst`（同步）、`AllocateSharedInst`（分配共享内存）。**无论产出是否被使用，这些指令都不能删**。

一个**诱人但错误**的判定方法是「看 `output is None`」：很多人会想「没有产出的指令就是副作用指令」。这在 Tilus 里是错的，原因有二：

1. 存在「**有产出但是副作用**」的指令：`AllocateSharedInst` 产出一块 `SharedTensor`，但它的语义是「分配共享内存」这一动作，删掉就没内存可用了。
2. 存在「**有产出但跨线程同步、不可当纯计算删**」的指令：`ScanInst`（前缀扫描）产出寄存器张量，但它需要跨线程协作，不能像 `AddInst` 那样随意消除。

因此 Tilus 采用一份**显式白名单** `FUNCTIONAL_INST_TYPES`：某条指令是否「功能」，完全由它是否在这个 tuple 里决定，与 `output` 是否为 `None` 无关。

#### 4.2.2 核心流程

死代码消除（DCE）判定一条指令能否删除的流程：

```text
对 Function 里的每条指令 inst：
  ├─ 若 inst 是功能指令（在 FUNCTIONAL_INST_TYPES 里）
  │     └─ 且 inst.output 不为 None 且该 output 张量从未被任何活跃指令消费
  │           └─ 删除整条指令（返回 None → 变成空 SeqStmt）
  │
  ├─ 若 inst 是「副作用但产出可选」的指令（如原子操作）
  │     └─ 且其 output 寄存器无人用
  │           └─ 保留指令（副作用必须发生），但把 output 改写成 None
  │              这样代码生成可省掉目的寄存器，发「无返回值」的 PTX
  │
  └─ 其余副作用指令
        └─ 无条件保留
```

注意第二步的精妙之处：原子操作（如 `AtomicSharedInst`）既必须发生（读-改-写的副作用），又常常返回「旧值」寄存器。当旧值没人用时，DCE 不删指令，而是把 `output` 置 `None`，让发射器生成不带目的操作数的 PTX。这正依赖「白名单 + 单独的可选产出列表」两套机制。

活跃性传播是一个**不动点迭代**：功能指令 A 的 inputs 是否「有用」，取决于 A 的 output 是否有用；而 A 的 output 是否有用，又取决于下游指令……所以需要反复传播直到集合不再变化。

#### 4.2.3 源码精读

白名单定义在 DCE 文件里，见 [python/tilus/transforms/dead_code_elimination.py:76-113](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/dead_code_elimination.py#L76-L113)：

```python
FUNCTIONAL_INST_TYPES: tuple[Type[Instruction], ...] = (
    AllocateRegisterInst, SliceRegisterInst, CastInst,
    ElementwiseUnaryBaseInst,    # NegInst/AbsInst/ClipInst/ElementwiseUnaryInst
    ElementwiseBinaryBaseInst,   # AddInst/SubInst/MulInst/DivInst/ModInst/...
    WhereInst, RepeatInst, ReduceInst, ViewInst, SqueezeInst, UnsqueezeInst, TransposeInst,
    LoadGlobalInst, LoadSharedInst, LoadGlobalGenericInst,
    SliceGlobalInst, SliceSharedInst, GlobalViewInst,
    DotInst, SimtDotInst, ...
)
```

判定函数极简，纯靠 `isinstance`，见 [python/tilus/transforms/dead_code_elimination.py:128-129](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/dead_code_elimination.py#L128-L129)：

```python
def _is_functional(inst: Instruction) -> bool:
    return isinstance(inst, FUNCTIONAL_INST_TYPES)
```

注意它**没有**看 `inst.output`。收集阶段，对每条指令分流，见 [python/tilus/transforms/dead_code_elimination.py:166-178](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/dead_code_elimination.py#L166-L178)：

```python
def visit_Instruction(self, inst: Instruction) -> None:
    if _is_functional(inst):
        self.functional_insts.append(inst)        # 暂存，待活跃性判定
    else:
        # 副作用指令：所有 inputs 无条件标记为「已用」
        for tensor in inst.inputs:
            self.used_tensors.add(id(tensor))
        ...
```

这里有两个关键设计：副作用指令的输入**立即**算作活跃（因为指令本身要执行，它读的数据就有意义）；而功能指令的输入是否活跃，要等下游判定后再传播，见不动点循环 [python/tilus/transforms/dead_code_elimination.py:213-229](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/dead_code_elimination.py#L213-L229)：

```python
changed = True
while changed:
    changed = False
    for inst in self.functional_insts:
        if inst.output is not None and id(inst.output) in self.used_tensors:
            for tensor in inst.inputs:
                if self._mark_used(tensor):
                    changed = True
```

真正执行删除的逻辑在消除器里，见 [python/tilus/transforms/dead_code_elimination.py:244-254](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/dead_code_elimination.py#L244-L254)：

```python
def visit_Instruction(self, inst: Instruction) -> Instruction | None:
    if _is_functional(inst) and inst.output is not None and id(inst.output) not in self.used_tensors:
        return None                                    # 功能指令：产出无人用 → 删除
    if (_is_side_effecting_with_optional_output(inst)
            and inst.output is not None
            and id(inst.output) not in self.used_tensors):
        return dataclasses.replace(inst, output=None)  # 原子类：保留副作用，去掉产出寄存器
    return super().visit_Instruction(inst)
```

现在用 `generic.py` 里的真实指令验证「白名单而非 output」的说法。**反例一：有产出却是副作用**——`AllocateSharedInst` 见 [python/tilus/ir/instructions/generic.py:657-661](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/instructions/generic.py#L657-L661)：

```python
class AllocateSharedInst(Instruction):
    @staticmethod
    def create(output: SharedTensor) -> AllocateSharedInst:
        return AllocateSharedInst(output=output, inputs=())
```

它 `output` 不为 `None`，但**不在** `FUNCTIONAL_INST_TYPES` 里——因为它的语义是「分配一块共享内存」这个动作，删掉后续就没有可用内存了。同理 `AllocateGlobalInst`（[python/tilus/ir/instructions/generic.py:664-673](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/instructions/generic.py#L664-L673)）。

**反例二：无产出的副作用**——`StoreGlobalInst` 见 [python/tilus/ir/instructions/generic.py:80-87](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/instructions/generic.py#L80-L87)，`output=None`，负责把寄存器数据写回显存，是内核输出结果的唯一途径，绝不可删。

**反例三：有产出却非功能**——`ScanInst`（前缀扫描）见 [python/tilus/ir/instructions/generic.py:525-560](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/instructions/generic.py#L525-L560)，它产出 `RegisterTensor`，但需要跨 warp/线程协作，故不在功能白名单中，DCE 永远保留它。

> 文件头注释把这套设计讲得很清楚，见 [python/tilus/transforms/dead_code_elimination.py:15-25](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/dead_code_elimination.py#L15-L25)：功能指令在产出未用时整条删除；副作用指令则保留动作、仅在产出寄存器未用时改写为 `output=None`。

#### 4.2.4 代码实践

**实践目标**：在 `generic.py` 中亲手分类功能指令与副作用指令，并用白名单佐证「副作用指令不可被消除」。

**操作步骤**：

1. 打开本讲的两个文件并对照阅读：
   - 白名单：`python/tilus/transforms/dead_code_elimination.py` 第 76–113 行。
   - 候选指令：`python/tilus/ir/instructions/generic.py` 全部指令类。
2. 对 `generic.py` 里**每一条**指令，依次判断：
   - 它的 `create` 是否把 `output` 设为 `None`？
   - 它是否出现在 `FUNCTIONAL_INST_TYPES` 白名单里？
3. 填写下表（这里给出若干行的参考答案，其余请自行补全）：

   | 指令 | output 是否 None | 在白名单？ | 类别 | 为何不可/可消除 |
   | --- | --- | --- | --- | --- |
   | `AddInst` | 否 | 是 | 功能 | 产出纯计算，无人用即可删 |
   | `CastInst` | 否 | 是 | 功能 | 同上 |
   | `LoadGlobalInst` | 否 | 是 | 功能 | 加载结果若不消费可删 |
   | `StoreGlobalInst` | 是 | 否 | 副作用 | 写显存是内核输出途径，删了结果丢失 |
   | `StoreSharedInst` | 是 | 否 | 副作用 | 写共享内存供他线程读，删了数据竞争/丢失 |
   | `SyncThreadsInst` | 是 | 否 | 副作用 | 同步语义，删了线程间看到旧数据 |
   | `AllocateSharedInst` | 否 | 否 | 副作用 | 有产出但语义是「分配内存」动作，删了无内存可用 |
   | `AllocateGlobalInst` | 否 | 否 | 副作用 | 同上，分配 workspace |
   | `ScanInst` | 否 | 否 | 副作用 | 有产出但跨线程协作，不可当纯计算删 |
   | `AssignInst` | 是 | 否 | 副作用 | 写入目标寄存器，是赋值动作 |

4. 重点观察「`output` 是否 None」与「是否功能」**并不一致**的行（`AllocateSharedInst`、`AllocateGlobalInst`、`ScanInst`），用一句话说明为何这些指令即便有产出也属副作用。

**需要观察的现象**：你会发现 `output is None` 这一列和「在白名单」这一列**不是简单取反**关系——存在「有产出却不功能」的指令，这正是「不能用 output 判定」的实证。

**预期结果**：`generic.py` 中约一半指令（`Store*`、`Sync*`、`Allocate*`、`Free*`、`Print*`、`Assign*`、`Exit`、`Nop`、`Scan`）属副作用类，它们因改变机器状态或必须按序执行而不可被 DCE 消除。（本实践为源码阅读型，结论可直接从两个文件得出。）

#### 4.2.5 小练习与答案

**练习 1**：假设有人提议「为了简化，把 `AllocateSharedInst` 也放进功能白名单」。这会带来什么后果？

**参考答案**：若某块共享内存的 `SharedTensor` 在某分支里未被后续指令消费，DCE 会把 `AllocateSharedInst` 整条删掉。但分配动作没了，后续若在别的路径写这块内存就会越界/访问未分配地址。共享/全局内存分配是真实副作用，必须保留。

**练习 2**：原子指令 `AtomicSharedInst` 属于哪一类？DCE 如何处理它？

**参考答案**：它属于「副作用但产出可选」类（见 `SIDE_EFFECTING_WITH_OPTIONAL_OUTPUT_INST_TYPES`，[python/tilus/transforms/dead_code_elimination.py:120-125](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/dead_code_elimination.py#L120-L125)）。读-改-写的副作用必须发生，所以指令不删；但当它返回的「旧值」寄存器无人用时，DCE 把 `output` 改写成 `None`，让发射器生成不带目的操作数的原子 PTX。

---

### 4.3 Tensor 的四种形态与身份相等语义

#### 4.3.1 概念说明

指令操作的对象是张量。Tilus 按张量**所在的 GPU 内存层次**把它们分成四种，一一对应硬件存储：

| 张量类型 | 所在内存 | 可见性 | 典型用途 |
| --- | --- | --- | --- |
| `RegisterTensor` | 寄存器（每线程私有，分布式存放） | 线程私有 | 计算、累加器、临时变量 |
| `SharedTensor` | 片上共享内存（shared memory） | 线程块内共享 | 线程间数据交换、分块复用 |
| `GlobalTensor` | 显存（DRAM） | 全网格可见 | 内核输入/输出 |
| `TMemoryTensor` | 张量内存（TMEM，Blackwell 专属） | SM 内张量核私有 | 第五代张量核的专用存储 |

这四种张量都继承自 `Tensor` 基类，共享一个 `dtype` 字段；但它们的形状/布局字段不同：寄存器、共享、TMEM 张量用 `shape` + `optional_layout`，而全局张量直接用 `layout`（因为全局布局里就含 shape）。

**身份相等（identity equality）** 是它们的共同语义。用数学语言区分两种相等：

- 结构相等：\( t_1 =_{\text{struct}} t_2 \) 当且仅当两者所有字段（dtype、shape、layout）完全相同。
- 身份相等：\( t_1 =_{\text{id}} t_2 \) 当且仅当 \( \mathrm{id}(t_1) = \mathrm{id}(t_2) \)（同一个 Python 对象）。

Tilus 选择身份相等：**两个字段完全相同的张量，仍是两个不同的 IR 节点**。这有两个直接后果：

1. `IRRewriter` 的记忆化字典（memo）以对象 id 为键——同一个张量只被改写一次。
2. 缓存键、活跃性集合（`used_tensors`）都用 `id(tensor)` 做成员判定，绝不会把「长得像」的两个张量误认成一个。

#### 4.3.2 核心流程

身份相等在 IR 里的体现：

```text
所有 IR 节点继承 IRNode
        │
        ├── __hash__  返回 id(self)
        └── __eq__    返回 (self is other)

dataclass 装饰器: @dataclass(frozen=True, eq=False)
        │  eq=False 阻止 dataclass 自动生成「按字段比较」的 __eq__/__hash__
        ▼
于是任何 IRNode 子类（Instruction、Tensor、Stmt…）都按身份判等
        │
        ▼
后果：memo 字典、used_tensors 集合、缓存键 全部以 id() 为准
```

一个常被忽略的细节：`@dataclass(frozen=True, eq=False)` 里的 `eq=False` 至关重要。若写成 `eq=True`，dataclass 会按字段值生成 `__eq__` 和 `__hash__`，从而覆盖 `IRNode` 的身份相等实现，导致两个「同字段」张量被当成同一个——这正是 Tilus 要避免的。

#### 4.3.3 源码精读

身份相等的根基在基类，见 [python/tilus/ir/node.py:18-31](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/node.py#L18-L31)：

```python
@dataclass(frozen=True, eq=False)
class IRNode:
    def __str__(self): ...
    def __hash__(self):
        return id(self)
    def __eq__(self, other):
        return self is other
```

`__hash__` 直接用 `id(self)`，`__eq__` 退化为 `is`。所有子类只要保持 `eq=False`，就继承这套身份语义。

`Tensor` 基类只声明 `dtype`，见 [python/tilus/ir/tensor.py:29-39](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L29-L39)：

```python
@dataclass(frozen=True, eq=False)
class Tensor:
    dtype: DataType
```

四种张量都在 `dtype` 之外各有侧重。寄存器张量最常用，`optional_layout` 体现了「布局可延迟绑定」的设计，见 [python/tilus/ir/tensor.py:81-97](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L81-L97)：

```python
@dataclass(frozen=True, eq=False)
class RegisterTensor(Tensor):
    shape: tuple[int, ...]
    optional_layout: Optional[RegisterLayout] = None
```

`optional_layout` 可为 `None`：转译阶段张量往往只有 shape、没有布局，等布局推理 Pass（见 u4-l5）跑完才用 `with_layout` 填上，见 [python/tilus/ir/tensor.py:170-187](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L170-L187)。注意 `with_layout` 用 `dataclasses.replace` 返回**新对象**，不改原对象——身份相等下，填了布局的张量是一个全新的 IR 节点。

共享内存张量结构类似，但额外提供 `nbytes` / `storage_nbytes`（考虑 swizzle 后可能更大），见 [python/tilus/ir/tensor.py:546-630](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L546-L630)。TMEM 张量限定 lane 数必须是 32/64/128（Blackwell 硬件约束），见 [python/tilus/ir/tensor.py:658-708](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L658-L708)。全局张量则把 shape 直接挂在 layout 上，见 [python/tilus/ir/tensor.py:764-794](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L764-L794)。

最后看一个体现「身份相等服务于 IR」的有趣细节：`RegisterTensor.__bool__` 恒为 `True`，见 [python/tilus/ir/tensor.py:206-210](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L206-L210)：

```python
def __bool__(self):
    # 让 `if inst.output:` 能用来判断指令是否有产出张量
    return True
```

因为 `__eq__` 被身份相等占用（`==` 不再比较值），代码里判断「是否有产出」必须用 `inst.output is not None` 或 `if inst.output:`（借助恒真的 `__bool__`），而**不能用** `inst.output == None`。

#### 4.3.4 代码实践

**实践目标**：用一个最小脚本验证身份相等语义，理解「同字段 ≠ 同对象」。

**操作步骤**（**示例代码**，需在已安装 tilus 的环境运行）：

```python
from tilus.hidet.ir.dtypes import float32
from tilus.ir.tensor import RegisterTensor

# 构造两个字段完全相同的寄存器张量
t1 = RegisterTensor.create(dtype=float32, shape=(16, 16))
t2 = RegisterTensor.create(dtype=float32, shape=(16, 16))

print("t1 == t2 :", t1 == t2)   # 期望 False：身份相等
print("t1 is t2 :", t1 is t2)   # 期望 False：不同对象
print("hash equal:", hash(t1) == hash(t2))  # 期望 False：hash=id
print("t1.dtype == t2.dtype:", t1.dtype == t2.dtype)  # 期望 True：dtype 仍按值比较

# 放进集合：会被当成两个不同元素
s = {t1, t2}
print("set size:", len(s))      # 期望 2
```

**需要观察的现象**：尽管 `t1` 与 `t2` 的 dtype、shape 完全一致，`t1 == t2` 为 `False`，集合里它们是两个独立元素。

**预期结果**：身份相等让两个「长得一样」的张量各自独立，这正是 IR 节点能被精确追踪、改写、记忆化的前提。（运行结果：**待本地验证**，但依据 `node.py` 的实现，上述断言应成立。）

#### 4.3.5 小练习与答案

**练习 1**：如果把 `RegisterTensor` 的装饰器从 `@dataclass(frozen=True, eq=False)` 改成 `@dataclass(frozen=True, eq=True)`，会发生什么？

**参考答案**：dataclass 会按字段（dtype/shape/optional_layout）生成 `__eq__` 与 `__hash__`，覆盖 `IRNode` 的身份相等。于是两个同字段张量被判等、hash 相同，放进 memo 字典或 `used_tensors` 集合时会被误当作同一个，导致变换器漏改或 DCE 误删。这正是 `eq=False` 不可省的原因。

**练习 2**：为什么 `GlobalTensor` 没有 `shape` 字段，而 `RegisterTensor`/`SharedTensor` 有？

**参考答案**：全局张量的 shape 直接编码在 `GlobalLayout` 里（且 shape 可以是符号表达式，如运行时维度 `n`），所以 `GlobalTensor` 只持有一个 `layout`，通过 `.shape` 属性向 layout 取值（见 [python/tilus/ir/tensor.py:783-794](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L783-L794)）。寄存器/共享张量在转译期往往先有 shape、布局后填，故把 `shape` 作为独立字段、`optional_layout` 可空。

---

## 5. 综合实践

把本讲三块知识串起来：用 IR 工具亲手构造一段含「活指令 + 死功能指令 + 副作用指令」的小片段，推断 DCE 的行为，再用指令收集器验证。

**任务**：构造一个最小 `Function`，其 body 包含三条指令：

1. 一条 `LoadGlobalInst`（功能，产出 `ra`）；
2. 一条 `AddInst`（功能，产出 `rb`，**且 `rb` 不被任何指令消费**——即死代码）；
3. 一条 `StoreGlobalInst`（副作用，把 `ra` 写回显存）。

**操作步骤**：

1. 参考 `tests/transforms/test_dead_code_elimination.py`（CLAUDE.md 推荐的 IR 构造范式）中构造张量与指令、再用 `Function.create(...)` 包成函数的写法。
2. 用 `SeqStmt` 把三条 `InstStmt` 串成 body。
3. **先做纸面推断**：
   - `AddInst` 的 output `rb` 无人用 → 功能指令且产出未用 → 应被 DCE 删除。
   - `LoadGlobalInst` 的 output `ra` 被 `StoreGlobalInst` 消费 → 活跃，保留。
   - `StoreGlobalInst` 是副作用 → 无条件保留。
4. 用 `ir.tools.instruction_collector.collect_instructions(func)` 在 DCE 前后各统计一次指令数量（CLAUDE.md 提到该工具用于「按类型统计指令」）。
5. 也可用 `from tilus.transforms import dead_code_elimination`（或经 `get_default_passes`）对该函数跑一遍 DCE，对比前后 IR。

**预期结果**：DCE 后 `AddInst` 消失，`LoadGlobalInst` 与 `StoreGlobalInst` 保留；`collect_instructions` 报告 `AddInst` 数量从 1 变 0。这个结果同时印证了三个模块：Instruction 三段式（output 决定可删性）、白名单判定（Add 在白名单、Store 不在）、身份相等（`rb` 这个对象的 id 不在 `used_tensors` 里）。

> 若无 GPU/无法完整编译，本实践可退化为「纸面推断 + 阅读 DCE 源码核对」的源码阅读型实践；只要你能正确推断出三条指令各自的去留，就达到了本讲目标。（完整运行：**待本地验证**。）

## 6. 本讲小结

- 一条 `Instruction` 统一为 `output / inputs / attributes` 三段；`attributes` 自动收集除 output/inputs 外的所有 dataclass 字段，是通用变换器扫描标量信息的入口。
- 功能 vs 副作用由**显式白名单** `FUNCTIONAL_INST_TYPES` 判定，**与 `output is None` 无关**：`AllocateSharedInst`/`ScanInst` 有产出却是副作用，`StoreGlobalInst` 无产出更是副作用。
- DCE 只删「功能且产出未用」的指令；副作用指令一律保留，原子类副作用指令在产出未用时仅把 `output` 改写为 `None`。
- 四种 `Tensor`（Register/Shared/Global/TMemory）对应寄存器、共享内存、显存、张量内存四个层次；`optional_layout` 让布局可延迟绑定。
- 所有 IR 节点采用**身份相等**（`__hash__=id`、`__eq__` 即 `is`），靠 `@dataclass(eq=False)` 保护；它是变换器记忆化、活跃性集合、缓存键精确运作的根基。
- 判断「指令是否有产出」要用 `inst.output is not None`，而非 `== None`（身份相等下后者语义已被占用）。

## 7. 下一步学习建议

本讲把 IR 的两大主角讲清了，但还停留在「单条指令/单个张量」的层面。接下来：

1. **u3-l5（IR 工具：验证、打印与收集）**：学会用 `verify` 在编译前校验程序、用 `printer` 打印整棵 IR、用 `collect_instructions` 统计指令——这些都是本讲综合实践里用到的工具，正式学一遍能让调试更顺手。
2. **u4（布局系统）**：本讲多次提到 `optional_layout` 与布局推理，但没展开。U4 会系统讲 `RegisterLayout / SharedLayout / GlobalLayout / TMemoryLayout` 的结构与代数运算，是 Tilus 最具特色的部分。
3. **u5-l3（死代码消除与标量分析）**：本讲只读了 DCE 的判定逻辑，U5-l3 会结合真实测试完整讲解 DCE Pass 的编写与端到端测试范式，建议在读 U5 前回看本讲的 `FUNCTIONAL_INST_TYPES` 与不动点传播作为铺垫。

继续阅读建议：先通读 `python/tilus/ir/inst.py` 与 `python/tilus/ir/tensor.py` 全文（都不长），再带着本讲的分类表去浏览 `generic.py`，建立「看到指令名就能猜出它功能还是副作用」的直觉。
