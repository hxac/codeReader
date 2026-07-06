# IR 到字节码：ir2bytecode

## 1. 本讲目标

在 [u5-l5](u5-l5-ir-core.md) 里，我们已经知道 cuTile 内核在编译期会被表示成一棵由 `IRContext / Builder / Block / Var / Operation` 组成的 Tile IR 树。这棵树是「给人读、给优化 pass 改」的内存对象，但后端编译器 `tileiras` 并不直接吃 Python 对象——它吃的是一段**线性的二进制字节码**（TileIR bytecode，落盘后缀通常是 `.tileirbc`）。

本讲要回答的问题就是：**这棵树形的 IR，是怎么被「压扁」成一段线性字节码流的？**

学完本讲，你应当能够：

1. 说清楚 `generate_bytecode_for_kernel` 这个总入口的执行顺序，以及它与 `compile_tile` 流水线的关系。
2. 理解 `BytecodeContext` 如何在「IR 里的 `Var`」与「字节码里的 `Value`」这两套命名空间之间做翻译，并维护类型表、常量表与值映射。
3. 掌握 `CodeBuilder.new_op` 如何为每个操作分配一个单调递增的 SSA value id。
4. 理解控制流操作（`Loop` / `IfElse`）的「嵌套 region」是如何用一个临时 `bytearray` 缓冲区递归编码，再拼接回主缓冲区的。

## 2. 前置知识

本讲默认你已经掌握 [u5-l5](u5-l5-ir-core.md) 的全部内容。下面三句话帮助你回忆与本讲最相关的部分：

- **Tile IR 是一棵树**。函数体是一个 `Block`，`Block` 里是一串 `Operation`；控制流操作（`Loop` / `IfElse`）通过 `nested_block()` 字段把子 `Block` 挂在自己身上，从而形成树。`Block.traverse()` 就是一棵深度优先遍历这棵树的迭代器（[ir.py:L786-L790](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L786-L790)）。
- **`Var` 是 SSA 值的句柄**。每个 `Operation` 有 `result_vars`（产出若干 `Var`）和若干 `operand()`（消费若干 `Var`）。`Var` 本身很轻，真正的类型/常量信息存在 `IRContext` 的查表里。
- **字节码侧也有一套「值」**，叫 `bc.Value`，本质就是「一个整数 id」（`value_id`）。后端字节码是一串指令，每条指令引用其它指令的结果时，用的就是这个 id。

所以本讲的核心，就是**把「`Var` + 树形 `Block` 嵌套」翻译成「`Value(value_id)` + 线性字节码」**。用一句话概括：树 → 线性流。

> 名词对照速查
>
> | IR 侧（树） | 字节码侧（线性流） |
> |---|---|
> | `Var`（一个 SSA 值的名字） | `bc.Value`（一个 `value_id: int`） |
> | `Operation`（一条 IR 指令） | 一段 opcode + 属性 + 操作数的字节序列 |
> | `Block`（指令容器，可有参数） | 一个 region 内的「块」（带块参数类型） |
> | `IRContext.typemap` / `IRContext.constants` | `TypeTable` / `ConstantTable` |
> | `Loop` / `IfElse` 的 `nested_block()` | 字节码里的 nested region |

## 3. 本讲源码地图

本讲涉及的关键文件及其职责：

| 文件 | 职责 |
|---|---|
| [`src/cuda/tile/_ir2bytecode.py`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir2bytecode.py) | 本讲主战场。定义 `BytecodeContext`、`generate_bytecode_for_block`、总入口 `generate_bytecode_for_kernel`。 |
| [`src/cuda/tile/_bytecode/code_builder.py`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/code_builder.py) | 定义 `CodeBuilder`（线性字节码发射器）、`Value`（字节码值 id）、`NestedBlockBuilder`（嵌套 region 的临时缓冲管理）。 |
| [`src/cuda/tile/_ir/ir.py`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py) | `Operation.generate_bytecode` 抽象方法、`Block`、`nested_block()` 字段定义。提供「被翻译的对象」。 |
| [`src/cuda/tile/_ir/control_flow_ops.py`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py) | `Loop` / `IfElse` / `Continue` / `Break` / `EndBranch` 等控制流操作，是嵌套 region 编码的典型案例。 |
| [`src/cuda/tile/_bytecode/encodings.py`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/encodings.py) | 一堆 `encode_*Op` 函数，每个对应一种字节码操作；是 `CodeBuilder` 之上的「指令模板」。 |
| [`src/cuda/tile/_bytecode/writer.py`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/writer.py) | `BytecodeWriter` / `write_bytecode`，负责整体 section 布局（函数表、全局、常量、类型、字符串表）。 |
| [`src/cuda/tile/_compile.py`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py) | `_get_bytecode` 调用本讲总入口，并提供 `CUDA_TILE_DUMP_BYTECODE` 落盘机制。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**总入口 `generate_bytecode_for_kernel`**、**桥梁 `BytecodeContext`**、**SSA id 分配 `CodeBuilder.new_op`**、**嵌套 region 编码（控制流）**。前三者是「线性」翻译，第四者解决「树形」如何塞进「线性」。

### 4.1 总入口：generate_bytecode_for_kernel

#### 4.1.1 概念说明

`generate_bytecode_for_kernel` 是 ir2bytecode 阶段的总入口。它接收「一个内核的函数体 `Block`」，把它编码进一个 `BytecodeWriter`。注意它的粒度：**一次调用只编码一个函数（一个 signature）**，外层 `_get_bytecode` 会按 signature 数量循环调用它（见下方源码精读）。

它要完成三件事：

1. **准备函数头**：把参数类型查表成 `TypeId`，注册调试信息，并用 `writer.function(...)` 上下文开启一个新函数，拿到一个 `CodeBuilder` 和参数的 `Value` 列表。
2. **建立翻译桥梁**：构造一个 `BytecodeContext`，把入口参数 `Var → Value` 的映射预先填好。
3. **递归翻译**：调用 `generate_bytecode_for_block(ctx, func_body)`，对函数体里的每条 `Operation` 逐条降级。

#### 4.1.2 核心流程

伪代码描述其骨架：

```
generate_bytecode_for_kernel(func_body, symbol, compiler_options, sm_arch, writer):
    # 1. 处理 entry hints（occupancy / num_ctas / num_worker_warps），
    #    旧字节码版本要做 sm_arch 特化折叠
    hints = 按 target 构建 EntryHints

    # 2. 参数类型 IR→TypeId，并准备调试信息映射
    param_type_ids = [typeid(type_table, p.get_type()) for p in func_body.params]

    # 3. 开启一个函数上下文：写入函数名/签名/标志，得到 (builder, param_values)
    with writer.function(...) as (builder, param_values):
        # 4. 构造翻译桥梁
        ctx = BytecodeContext(builder, type_table, ..., ir_ctx=func_body.ctx, sm_arch)
        # 5. 把入口参数 Var 绑定到字节码 Value
        for var, value in zip(func_body.params, param_values):
            ctx.set_value(var, value)
        # 6. 递归翻译函数体
        generate_bytecode_for_block(ctx, func_body)
```

#### 4.1.3 源码精读

入口签名与 hints 处理。注意旧版本（`< V_13_3`）会把按 target 的 hints 折叠成单一 `sm_arch` 的特化映射（[_ir2bytecode.py:L507-L527](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir2bytecode.py#L507-L527)）：

```python
def generate_bytecode_for_kernel(func_body, symbol, compiler_options, sm_arch, writer, anonymize_debug_attr):
    version = writer.version
    hints_by_target = compiler_options.hints_by_target()
    if version < BytecodeVersion.V_13_3:
        specialized_hints = dict(hints_by_target.get("default", {}))
        specialized_hints.update(hints_by_target.get(sm_arch, {}))
        hints_by_target = {sm_arch: specialized_hints}
    ...
```

开启函数上下文、建桥梁、绑定入口参数、递归翻译，是入口的最后四步（[_ir2bytecode.py:L529-L550](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir2bytecode.py#L529-L550)）：

```python
    param_type_ids = [typeid(writer.type_table, p.get_type()) for p in func_body.params]
    ...
    with writer.function(name=symbol, parameter_types=param_type_ids, result_types=(),
                         entry_point=True, hints=hints, debug_attr=func_debug_attr) as (builder, param_values):
        ctx = BytecodeContext(builder=builder, type_table=writer.type_table, ...,
                              ir_ctx=func_body.ctx, sm_arch=sm_arch)
        for var, value in zip(func_body.params, param_values, strict=True):
            ctx.set_value(var, value)
        generate_bytecode_for_block(ctx, func_body)
```

`writer.function` 是一个上下文管理器：进入时把函数名、签名类型、entry 标志写进主缓冲区，并为本函数创建一个**独立的 `CodeBuilder`**（其 `buf` 是全新的 `bytearray`）；退出时把这个独立 `buf` 的长度和内容追加回主缓冲区（[writer.py:L80-L106](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/writer.py#L80-L106)）。

逐条降级发生在 `generate_bytecode_for_block`：对块里每条 `Operation`，调用它自己的 `generate_bytecode(ctx)`，拿到结果 `bc.Value`，再写回 `ctx` 的值映射（[_ir2bytecode.py:L478-L492](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir2bytecode.py#L478-L492)）：

```python
def generate_bytecode_for_block(ctx, block):
    for op in block.operations:
        with ctx.loc(op.loc):
            result_values = op.generate_bytecode(ctx)
            if isinstance(result_values, bc.Value):
                result_values = (result_values,)
            for result_var, val in zip(op.result_vars, result_values, strict=True):
                ctx.set_value(result_var, val)
```

外层调用方在 [_compile.py:L420-L432](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L420-L432)：每个 signature 调用一次本入口，全部装进同一个 `writer`。

#### 4.1.4 代码实践

**实践目标**：定位 `generate_bytecode_for_kernel` 在编译流水线里的位置，并确认「一个内核函数 → 一次调用」。

**操作步骤**：

1. 打开 [_compile.py:L420-L432](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L420-L432)，阅读 `_get_bytecode`。
2. 顺着 `compile_tile` 看它如何调用 `_get_bytecode`（[_compile.py:L488](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L488)）。
3. 在 `generate_bytecode_for_kernel` 的入口加一行临时 `print(f"encoding kernel: {symbol}")`（仅本地调试，不要提交），观察调用次数。

**需要观察的现象 / 预期结果**：当内核因不同 `Constant` 取值被特化成多个 signature 时，本入口会被调用多次——这印证了「按 signature 翻译」。**待本地验证**：精确次数取决于你传入了多少个 `KernelSignature`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `writer.function` 要给每个函数一个**独立的 `CodeBuilder.buf`**，而不是直接写到主缓冲区？

> **参考答案**：这样函数体的字节码是自包含的一段，退出上下文时只需写一次「长度 + 内容」追加到主缓冲区，便于后端按函数定位、也便于后续 `num_functions` 校验（[writer.py:L118](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/writer.py#L118)）。这也与函数表 section 的布局对齐。

**练习 2**：入口里 `for var, value in zip(func_body.params, param_values)` 的作用是什么？如果删掉会怎样？

> **参考答案**：它把「函数入口参数 `Var`」绑定到字节码侧 `writer.function` 分配的入口 `Value`。若删掉，函数体内任何引用入口参数的操作在 `ctx.get_value(var)` 时会因映射缺失而报错（`get_value` 直接查 `_value_map`）。

---

### 4.2 桥梁：BytecodeContext

#### 4.2.1 概念说明

`BytecodeContext` 是整个翻译过程的「工作台」。它同时持有 IR 侧的查表（来自 `IRContext` 的 `typemap` / `constants`）和字节码侧的发射器（`builder` / `type_table`），并在两者之间维护一张**值映射表 `_value_map: Dict[str, bc.Value]`**——键是 IR `Var` 的名字，值是字节码 `Value`。

可以把它的职责分成四块：

1. **值映射**：`get_value` / `set_value` 把 `Var` 翻译成 `Value`，禁止重复绑定。
2. **类型/常量查表**：`typeof` / `is_constant` / `get_constant` 直接代理 `IRContext`。
3. **类型/常量去重表**：通过 `type_table` 把 IR `Type` 翻译成可序列化的 `TypeId`（`typeid()` 函数）。
4. **便利工具**：`cast` / `bitcast` / `constant` / `index_tuple` / `load_store_hints` 等封装常见编码组合。

#### 4.2.2 核心流程

`BytecodeContext` 在「翻译一条 `Operation`」时的角色：

```
op.generate_bytecode(ctx):
    1. for each operand var:  ctx.get_value(var)   → 拿到字节码 Value
    2. （必要时）ctx.constant(...) / ctx.cast(...)  → 合成新 Value
    3. 调用某个 encode_*Op(ctx.builder, ...)        → 发射字节码，返回新 Value
    4. return 新 Value
# 回到 generate_bytecode_for_block：ctx.set_value(op.result_var, 新Value)
```

关键不变量：**一个 IR `Var` 在 `_value_map` 里只能出现一次**。这与 SSA 的「定义唯一」语义一致。

#### 4.2.3 源码精读

`BytecodeContext.__init__` 集中展示它持有什么（[_ir2bytecode.py:L346-L364](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir2bytecode.py#L346-L364)）：

```python
class BytecodeContext:
    def __init__(self, builder, type_table, debug_attr_map, global_section, ir_ctx, sm_arch):
        self.builder = builder
        self.type_table = type_table
        self._debug_attr_map = debug_attr_map
        self.global_section = global_section
        self._typemap: Dict[str, Type] = ir_ctx.typemap       # IR 侧类型表
        self._constants: Dict[str, Any] = ir_ctx.constants     # IR 侧常量表
        self._value_map: Dict[str, bc.Value] = {}              # Var.name → Value
        self._array_base_ptr: Dict[str, bc.Value] = {}         # 数组基指针缓存
        self._list_partition_views: Dict[str, bc.Value] = {}   # list 视图缓存
        self.sm_arch = sm_arch
        self.innermost_loop = None                             # 当前最内层循环（4.4 用）
```

值映射的两个核心方法：`get_value` 直接查表，`set_value` 检测重复绑定（[_ir2bytecode.py:L396-L409](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir2bytecode.py#L396-L409)）：

```python
    def get_value(self, var: Var) -> bc.Value:
        return self._value_map[var.name]

    def set_value(self, var: Var, value: bc.Value) -> None:
        name = var.name
        if name in self._value_map:
            raise ValueError(f"Variable {name} is already in the value map")
        self._value_map[name] = value
```

类型与常量查询代理 `IRContext`（[_ir2bytecode.py:L381-L394](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir2bytecode.py#L381-L394)）：

```python
    def typeof(self, var):       return self._typemap[var.name]
    def typeid_of(self, var):    return typeid(self.type_table, self.typeof(var))
    def is_constant(self, var):  return var.name in self._constants
    def get_constant(self, var): return self._constants[var.name]
```

`constant()` 是「把一个 Python 值变成字节码常量 `Value`」的封装：先按 MLIR `DenseElementsAttr` 规则把值编成字节，再用 `encode_ConstantOp` 发射（[_ir2bytecode.py:L433-L449](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir2bytecode.py#L433-L449)）。常量字节化的细节见 `_constant_to_bytes`（[_ir2bytecode.py:L92-L105](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir2bytecode.py#L92-L105)）。

最后看一个真实 `Operation` 如何用 `ctx`。`TileReshape.generate_bytecode` 是最朴素的模板：取操作数 → 取结果类型 → 调 `encode_ReshapeOp` → 返回新 `Value`（[arithmetic_ops.py:L57-L61](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/arithmetic_ops.py#L57-L61)）：

```python
    def generate_bytecode(self, ctx):
        x_value = ctx.get_value(self.x)
        res_type_id = ctx.typeid_of(self.result_var)
        return bc.encode_ReshapeOp(ctx.builder, res_type_id, x_value)
```

#### 4.2.4 代码实践

**实践目标**：通过阅读一个简单算子，掌握「IR Var → 字节码 Value」的标准三步套路。

**操作步骤**：

1. 打开 [arithmetic_ops.py:L97-L105](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/arithmetic_ops.py#L97-L105)，读 `TileBroadcast.generate_bytecode`。
2. 对比它和 `TileReshape.generate_bytecode`，找出共同模式。
3. 再读 [_ir2bytecode.py:L411-L420](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir2bytecode.py#L411-L420) 的 `cast`，理解当 dtype/shape 不一致时 `ctx` 会自动插入额外的 `BroadcastOp` / 类型转换 op。

**预期结果**：你会发现几乎所有「单操作数、单结果」的算子都是同一个套路。这就解释了为什么 ir2bytecode 几乎没有「中央分派逻辑」——分派被下放到每个 `Operation.generate_bytecode`，是一种典型的**多态分派（每个 op 自己知道怎么编码自己）**。

#### 4.2.5 小练习与答案

**练习 1**：`set_value` 为什么要禁止重复绑定同一个 `Var` 名？

> **参考答案**：IR 是 SSA 形式，每个 `Var` 只定义一次；若重复绑定，意味着同一条字节码 `Value` 被两个 IR `Var` 复用，或某个 `Var` 被定义了两次——前者会导致后续操作数错乱，后者违反 SSA。报错能尽早暴露上游 pass 的 bug。

**练习 2**：`typeid()` 函数把 IR `Type` 翻译成 `TypeId` 时，背后依赖 `ctx.type_table` 的什么性质？

> **参考答案**：`type_table` 是一张去重表（flyweight），相同结构（dtype+shape 等）的类型只存一份并返回同一个 `TypeId`，从而让字节码的类型表 section 紧凑且可被多次引用。

---

### 4.3 SSA value id 分配：CodeBuilder.new_op

#### 4.3.1 概念说明

`CodeBuilder` 是「线性字节码发射器」：它持有一段 `buf: bytearray`、一个单调递增的 `next_value_id`、操作计数 `num_ops`，以及与全局共享的 `string_table` / `constant_table` / `debug_attr` 列表。

它最核心的方法是 `new_op`：**每发射一条无嵌套 region 的普通操作，就消耗一个（或几个）新的 value id 作为该操作的结果**。这些 id 就是字节码里其它指令引用本指令结果时用的「寄存器号」。

`Value` 本身极简，只是一个带 `value_id` 的 dataclass（[code_builder.py:L17-L19](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/code_builder.py#L17-L19)）：

```python
@dataclass
class Value:
    value_id: int
```

#### 4.3.2 核心流程

`new_op` 的三种模式：

```
new_op(num_results=None):   # 默认：单结果
    记录 debug_attr；num_ops += 1
    返回 Value(next_value_id);  next_value_id += 1

new_op(num_results=0):      # 无结果（如 store / return / yield）
    记录 debug_attr；num_ops += 1
    返回 None

new_op(num_results=k):      # 多结果
    记录 debug_attr；num_ops += 1
    返回 (Value(id), Value(id+1), ..., Value(id+k-1));  next_value_id += k
```

注意调用顺序的微妙之处：`encode_*Op` 函数总是**先把 opcode、属性、操作数逐字节写进 `buf`，最后才调 `new_op`** 来「认领结果 id」。也就是说，一条操作在字节码里的布局是：`opcode | flags | 结果类型 | 属性 | 操作数`，而「结果 id」是 `new_op` 在写完之后才分配并返回的（它不写进 `buf`，而是由后端按出现顺序隐式编号）。

#### 4.3.3 源码精读

`CodeBuilder` 的字段定义（[code_builder.py:L58-L67](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/code_builder.py#L58-L67)）：

```python
@dataclass
class CodeBuilder:
    buf: bytearray
    version: BytecodeVersion
    string_table: StringTable
    constant_table: ConstantTable
    debug_attr_per_op: List[DebugAttrId]
    next_value_id: int = 0
    cur_debug_attr: DebugAttrId = DebugAttrId(0)
    num_ops: int = 0
```

`new_op` 三分支实现（[code_builder.py:L69-L79](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/code_builder.py#L69-L79)）：

```python
    def new_op(self, num_results=None):
        self.debug_attr_per_op.append(self.cur_debug_attr)
        self.num_ops += 1
        if num_results is None:
            ret = Value(self.next_value_id)
            self.next_value_id += 1
            return ret
        elif num_results == 0:
            return None
        else:
            return self._make_value_tuple(num_results)
```

多结果由 `_make_value_tuple` 批量分配一段连续 id（[code_builder.py:L89-L93](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/code_builder.py#L89-L93)）：

```python
    def _make_value_tuple(self, length):
        end = self.next_value_id + length
        ret = tuple(Value(i) for i in range(self.next_value_id, end))
        self.next_value_id = end
        return ret
```

来看一个真实 `encode_*Op` 如何「先写正文，最后 `new_op`」。`encode_Exp2Op` 把 opcode(24)、结果类型、标志、操作数逐个写进 `_buf`，然后 `return code_builder.new_op()`（[encodings.py:L630-L645](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/encodings.py#L630-L645)）：

```python
def encode_Exp2Op(code_builder, result_type, source, flush_to_zero):
    _buf = code_builder.buf
    encode_varint(24, _buf)                       # Opcode
    encode_typeid(result_type, _buf)              # Result types
    encode_varint(bool(flush_to_zero), _buf)      # Flags
    encode_operand(source, _buf)                  # Operands
    return code_builder.new_op()
```

而无结果操作（如 `Continue`）会显式传 `num_results=0`（[encodings.py:L513-L525](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/encodings.py#L513-L525)）：

```python
def encode_ContinueOp(code_builder, operands):
    _buf = code_builder.buf
    encode_varint(17, _buf)                       # Opcode
    encode_sized_typeid_seq((), _buf)             # Variadic result types（空）
    encode_varint(len(operands), _buf)            # Operands
    encode_unsized_variadic_operands(operands, _buf)
    return code_builder.new_op(0)                 # 0 个结果 → 返回 None
```

操作数引用别的 `Value` 时，写的就是它的 `value_id`（[code_builder.py:L158-L159](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/code_builder.py#L158-L159)）：

```python
def encode_operand(val, buf):
    encode_varint(val.value_id, buf)
```

#### 4.3.4 代码实践

**实践目标**：亲手追一遍「两条算术指令」的 id 分配，建立对 `next_value_id` 单调递增的直觉。

**操作步骤**：假设函数入口已分配 `%0`（参数）。依次发射两条指令：

1. `encode_ReshapeOp(builder, ty, %0)` → 内部调 `new_op()`，返回 `%1`，`next_value_id` 由 1 变 2。
2. `encode_BroadcastOp(builder, ty2, %1)` → 操作数引用 `%1`（写进 `buf` 的是整数 1），`new_op()` 返回 `%2`。

**需要观察的现象 / 预期结果**：`buf` 里第二条指令的操作数字段会出现 `1`（即 `%1` 的 id）。`next_value_id` 与 `num_ops` 都随每条单结果指令加 1。这是一段「示例推理」，**待本地验证**：可在 `new_op` 加 `print(self.num_ops, ret)` 观察。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `new_op` 在「写完指令正文之后」才分配结果 id，而不是「先分配再写」？

> **参考答案**：因为结果 id 不写进 `buf`——后端按指令出现顺序隐式给每条指令的结果编号。`new_op` 的职责只是把当前 `next_value_id` 分配出去并推进指针，必须在指令字节落盘之后调用，才能保证顺序与编号一致。

**练习 2**：`encode_ContinueOp` 传 `num_results=0`，那 `Continue` 的「下一轮携带值」是如何表达的？

> **参考答案**：携带值作为 `Continue` 的**操作数**写入（`operands` 即下一轮的 body 参数值），结果数为 0。字节码侧通过「操作数 = 上一条对应结果的 Value」来表达回传，IR 侧则由 `Continue.values` 字段承载（[control_flow_ops.py:L436-L439](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py#L436-L439)）。

---

### 4.4 控制流的嵌套 region 编码

#### 4.4.1 概念说明

前面三个模块解决的都是「线性」问题：一条接一条指令往下写。但 IR 是树形的：`Loop` 和 `IfElse` 把子 `Block` 挂在自己身上。如何把「父指令 + 嵌套子块」塞进一条线性字节码流？

cuTile 的方案是 MLIR 风格的 **region**：一条控制流操作的字节码布局是「opcode + 头部 + 若干个 region」，每个 region 内部又是一个完整的「块（带参数类型）+ 指令序列」。技术上靠两个东西实现：

1. `CodeBuilder.new_op_with_nested_blocks`：标记「这条操作有 N 个结果、M 个 region」，返回一个 `NestedBlockBuilder`。
2. `NestedBlockBuilder.new_block`：用**临时缓冲区切换**的技巧——进入子块时把 `code_builder.buf` 换成一个全新的 `bytearray`，子块编码完毕后把临时缓冲「长度 + 内容」追加回原缓冲。这天然实现了「先写子块、再拼回父流」的递归结构。

> 一个关键直觉：嵌套 region 的编码是**深度优先**的。父操作的头部先写一半（opcode/类型/操作数），然后暂停，先把它所有 region 的字节写完，最后这些 region 字节整体插入到父操作之后、下一条兄弟指令之前。

#### 4.4.2 核心流程

以 `Loop`（for 循环）为例，整个编码过程的控制流：

```
Loop.generate_bytecode(ctx):
    取 initial_values、result_type_ids、start/stop/step 的 Value
    if 是 for 循环:
        nested = encode_ForOp(builder, result_types, start, stop, step, init, unsignedCmp=False)
        # encode_ForOp 写 opcode(41)/结果类型/标志/操作数，
        # 然后 new_op_with_nested_blocks(len(result_types), 1)  ← 声明 1 个 region
    else:  # while 循环
        nested = encode_LoopOp(builder, result_types, init)

    with nested.new_block(block_arg_type_ids) as block_args:   # 进入唯一的子块
        把归纳变量、携带变量绑定到 block_args（ctx.set_value）
        generate_bytecode_for_block(ctx, self.body)            # 递归翻译循环体
    return nested.done()                                       # 认领循环结果 Value
```

`NestedBlockBuilder.new_block` 的缓冲区切换（示意）：

```
进入 new_block(arg_type_ids):
    orig_buf = code_builder.buf
    写「1 个块」「参数类型列表」到 orig_buf
    code_builder.buf = bytearray()        # 换成空临时缓冲
    yield block_args（= _make_value_tuple(len(arg_type_ids))）
    # —— 此期间所有子指令都写进临时缓冲 ——
    encode_varint(num_ops, orig_buf)      # 子块指令数
    orig_buf.extend(临时缓冲)             # 拼回主缓冲
    code_builder.buf = orig_buf           # 恢复
```

#### 4.4.3 源码精读

`Loop` 操作的字段定义：`start/stop/step` 是操作数（while 循环时为 `None`），`initial_values` 是携带值，`body` 是 `nested_block()`（[control_flow_ops.py:L33-L39](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py#L33-L39)）：

```python
@dataclass(eq=False)
class Loop(Operation, opcode="loop"):
    start: Var | None = operand()
    stop: Var | None = operand()
    step: Var | None = operand()
    initial_values: tuple[Var, ...] = operand()
    body: Block = nested_block()
```

`Loop.generate_bytecode` 完整实现。注意 for 与 while 的分派（`encode_ForOp` vs `encode_LoopOp`），以及「绑定块参数 → 递归翻译 body → `done()` 认领结果」三段式（[control_flow_ops.py:L54-L79](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py#L54-L79)）：

```python
    def generate_bytecode(self, ctx):
        types = tuple(x.get_type() for x in self.body_vars)
        initial_values = [ctx.get_value(v) for v in self.initial_values]
        result_type_ids = [typeid(ctx.type_table, ty) for ty in types]

        if self.is_for_loop:
            start, stop, step = (ctx.get_value(x) for x in (self.start, self.stop, self.step))
            nested_builder = bc.encode_ForOp(ctx.builder, result_type_ids, start, stop, step,
                                             initial_values, unsignedCmp=False)
            induction_var_type_id = ctx.typeid_of(self.induction_var)
            block_arg_type_ids = (induction_var_type_id, *result_type_ids)
        else:
            nested_builder = bc.encode_LoopOp(ctx.builder, result_type_ids, initial_values)
            block_arg_type_ids = result_type_ids

        with nested_builder.new_block(block_arg_type_ids) as block_args, ctx.enter_loop(self):
            block_args = iter(block_args)
            if self.is_for_loop:
                ctx.set_value(self.induction_var, next(block_args))   # 归纳变量
            for var, value in zip(self.body_vars, block_args, strict=True):
                ctx.set_value(var, value)                              # 携带变量
            generate_bytecode_for_block(ctx, self.body)                # 递归
        return nested_builder.done()
```

`encode_ForOp` 写完头部后返回 `NestedBlockBuilder`（注意最后一行 `new_op_with_nested_blocks(len(result_types), 1)`——`1` 表示 1 个 region，`len(result_types)` 是循环结果数）（[encodings.py:L823-L848](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/encodings.py#L823-L848)）：

```python
def encode_ForOp(code_builder, result_types, lowerBound, upperBound, step, initValues, unsignedCmp):
    _buf = code_builder.buf
    encode_varint(41, _buf)                              # Opcode
    encode_sized_typeid_seq(result_types, _buf)         # Variadic result types
    ...
    encode_operand(lowerBound, _buf); encode_operand(upperBound, _buf); encode_operand(step, _buf)
    encode_unsized_variadic_operands(initValues, _buf)  # Operands
    return code_builder.new_op_with_nested_blocks(len(result_types), 1)
```

`new_op_with_nested_blocks` 把 region 数写进 `buf`，并返回 `NestedBlockBuilder`（[code_builder.py:L81-L87](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/code_builder.py#L81-L87)）：

```python
    def new_op_with_nested_blocks(self, num_results, num_blocks):
        self.debug_attr_per_op.append(self.cur_debug_attr)
        self.num_ops += 1
        encode_varint(num_blocks, self.buf)
        return NestedBlockBuilder(self, num_results=num_results, num_blocks=num_blocks)
```

`NestedBlockBuilder.new_block` 是嵌套编码的灵魂——临时缓冲切换与 `next_value_id` / `num_ops` 的保存恢复（[code_builder.py:L31-L51](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/code_builder.py#L31-L51)）：

```python
    @contextmanager
    def new_block(self, arg_type_ids):
        assert self._num_blocks > 0
        self._code_builder.buf.append(1)          # number of blocks in region (always 1)
        encode_varint(len(arg_type_ids), self._code_builder.buf)
        for t in arg_type_ids:
            encode_typeid(t, self._code_builder.buf)
        orig_buf = self._code_builder.buf
        orig_next_value_id = self._code_builder.next_value_id
        orig_num_ops = self._code_builder.num_ops
        self._code_builder.num_ops = 0
        self._code_builder.buf = bytearray()      # 切换到临时缓冲
        try:
            yield self._code_builder._make_value_tuple(len(arg_type_ids))
            encode_varint(self._code_builder.num_ops, orig_buf)  # 子块指令数
            orig_buf.extend(self._code_builder.buf)              # 拼回主缓冲
            self._num_blocks -= 1
        finally:
            self._code_builder.next_value_id = orig_next_value_id  # 恢复 id 计数
            self._code_builder.num_ops = orig_num_ops
            self._code_builder.buf = orig_buf
```

注意一个精妙之处：进入子块时 `_make_value_tuple` 会**推进 `next_value_id` 来给块参数分配 id**，但退出时 `next_value_id` 被**恢复**到进入前的值。这意味着子块参数的 id 与父操作的「结果 id 区间」是重叠复用的——这是字节码格式允许的，因为块参数的作用域仅限该 region。

`done()` 在所有 region 都写完后，认领父操作的 N 个结果（[code_builder.py:L53-L55](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/code_builder.py#L53-L55)）：

```python
    def done(self):
        assert self._num_blocks == 0
        return self._code_builder._make_value_tuple(self._num_results)
```

分支 `IfElse` 是同一机制的「两次 `new_block`」版本：先 `encode_IfOp` 拿到 `NestedBlockBuilder`，再依次对 then/else 各开一个块（[control_flow_ops.py:L279-L290](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py#L279-L290)）：

```python
    def generate_bytecode(self, ctx):
        cond_val = ctx.get_value(self.cond)
        result_types = tuple(ctx.typeof(v) for v in self.result_vars)
        result_type_ids = tuple(typeid(ctx.type_table, t) for t in result_types)
        nested_builder = bc.encode_IfOp(ctx.builder, result_type_ids, cond_val)
        for block in (self.then_block, self.else_block):
            with nested_builder.new_block(()):
                generate_bytecode_for_block(ctx, block)
        return nested_builder.done()
```

每个分支以 `EndBranch`（`yield`）终止，它把分支结果作为操作数写出去（[control_flow_ops.py:L501-L505](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py#L501-L505)）；循环体则以 `Continue` / `Break` 终止（[control_flow_ops.py:L436-L439](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py#L436-L439)、[control_flow_ops.py:L468-L471](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py#L468-L471)）。

#### 4.4.4 代码实践

**实践目标**：跟踪一个含 `for` 循环的内核，描述其 IR `Loop` 如何被编码成带 nested region 的字节码片段。这是本讲的核心实践。

**操作步骤**：

1. 准备一个最小累加内核（示例代码，非项目原有）：

   ```python
   # 示例代码：仅供理解，非仓库内置 sample
   import cuda.tile as ct

   @ct.kernel
   def accumulate(a: ct.Array, out: ct.Array):
       acc = ct.int32(0)
       for k in range(4):       # K 维累加，acc 是携带值
           tile = ct.load(a, (k,), (1,))
           acc = acc + ct.sum(tile)
       ct.store(out, (0,), acc)
   ```

2. 用 `compile_tile` 拿到 final IR 的文本形式（IR 侧「树」的视图）：

   ```python
   from cuda.tile._compile import compile_tile
   from cuda.tile.compilation import KernelSignature
   # 具体签名构造略；return_final_ir=True 会返回 IR 文本
   ```

   IR 文本里你会看到形如 `for %i in range(%lo, %hi, %step) (with %acc = %init) do: ...` 的 `Loop` 操作，其中 `%acc` 是携带变量，循环体是一个嵌套 `Block`。

3. 对照 [control_flow_ops.py:L54-L79](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py#L54-L79) 手动跟踪编码：
   - `encode_ForOp` 写 opcode 41、结果类型（`acc` 的类型）、操作数（`lo/hi/step/init`），然后 `new_op_with_nested_blocks(1, 1)`。
   - `new_block((induction_ty, acc_ty))`：写「1 个块」「2 个参数类型」，切到临时缓冲，分配块参数 id，绑定 `%i` 和 `%acc`。
   - 递归 `generate_bytecode_for_block` 把 `load / sum / add / continue` 逐条写进临时缓冲。
   - 退出 `new_block`：写临时缓冲里的指令数，把临时缓冲拼回主缓冲，恢复 `next_value_id`。
   - `done()` 认领 1 个循环结果（即最终 `%acc`）。

4.（可选）设置环境变量导出二进制字节码做交叉验证：

   ```bash
   export CUDA_TILE_DUMP_BYTECODE=/tmp/tile_dump
   # 运行内核后，在 /tmp/tile_dump 下会出现 *.tileirbc 二进制文件
   ```

   落盘机制见 [_compile.py:L493-L499](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L493-L499)。

**需要观察的现象 / 预期结果**：

- IR 文本里 `Loop` 的嵌套 `Block` 在字节码里对应一段被「长度前缀」包裹的子指令序列，夹在 `ForOp` 头部之后、下一条兄弟指令之前。
- 子块以一条 `ContinueOp`（opcode 17）结尾，其操作数是「新一轮的 `acc`」。
- 因为 `next_value_id` 在退出 `new_block` 时被恢复，循环体内部使用的 value id 与循环外部的 id 区间会有重叠——这是 region 作用域隔离的结果。

**待本地验证**：上述内核能否直接 `compile_tile` 取决于你是否正确构造了 `KernelSignature` 与宿主 `Array`；若仅做源码阅读型跟踪，可跳过运行，直接对照源码完成第 3 步的纸面推演。

#### 4.4.5 小练习与答案

**练习 1**：`NestedBlockBuilder.new_block` 退出时为什么要把 `next_value_id` **恢复**到进入前的值，而不是继续递增？

> **参考答案**：块参数和块内指令的 value id 只在该 region 内有效，是局部编号。父操作的结果 id 由 `done()` 在 region 写完后从「进入前的 `next_value_id`」开始分配。若不恢复，子块消耗的 id 会与父操作结果、兄弟操作的 id 错位，导致后端反序列化时引用错乱。

**练习 2**：`IfElse` 有两个子块（then/else），但 `encode_IfOp` 内部调用 `new_op_with_nested_blocks(N, 2)`（2 个 region）。这两个 region 在字节码里是如何区分先后与「结果汇合」的？

> **参考答案**：两个 region 按 `new_block` 的调用顺序先后排列（先 then 后 else，见 [control_flow_ops.py:L286-L288](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py#L286-L288)）。每个分支以 `EndBranch`（`yield`）结尾，把本分支的结果值作为操作数写出；后端根据「两个 yield 的对应位置」把两侧结果汇合成 `IfOp` 的 N 个结果 `Value`（由 `done()` 认领）。

**练习 3**：循环体的「携带值」在 IR 侧由 `Loop.initial_values`（进入值）和 `Continue.values`（回传值）共同表达。在字节码侧，这两者分别对应 `encode_ForOp` 的哪部分？

> **参考答案**：`initial_values` 作为 `ForOp` 的**变长操作数** `initValues` 写在头部（[encodings.py:L847](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/encodings.py#L847)）；每轮回传的 `Continue.values` 则是子块末尾 `ContinueOp` 的操作数（[encodings.py:L523-L524](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/encodings.py#L523-L524)）。

## 5. 综合实践

把本讲四个模块串起来，做一个「**树 → 线性**」的全链路跟踪任务。

**任务**：选一个同时含有算术与循环的内核（可直接借用 [`samples/MatMul.py`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/samples/MatMul.py) 中沿 K 维累加的 GEMM 内核），完成下列跟踪：

1. **入口定位**：在 [_compile.py:L420-L432](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L420-L432) 找到 `generate_bytecode_for_kernel` 的调用点，确认它「按 signature 循环」。
2. **桥梁建立**：画出 `generate_bytecode_for_kernel` 内 `writer.function → BytecodeContext → set_value(入口参数)` 的对象关系图，标注 `_value_map` 的初始内容。
3. **线性算术**：取循环体里的一条 `mma`/`add` 操作，写出它 `generate_bytecode(ctx)` 的三步：`get_value(操作数) → encode_*Op → new_op()`，标出分配到的 value id。
4. **嵌套循环**：定位 `Loop.generate_bytecode`，写出 `encode_ForOp → new_block（临时缓冲切换）→ 递归 body → done()` 的完整时序，并解释「子块字节为何插入在 ForOp 头部之后、兄弟指令之前」。
5. **交叉验证**：设置 `CUDA_TILE_DUMP_BYTECODE` 导出 `.tileirbc`，用十六进制工具找到 opcode 41（`ForOp`），核对其后紧跟的「1 个 region / 块参数类型 / 子块指令数 / 子块字节」结构是否与你的纸面推演一致。

**验收标准**：你能用一句话向同伴解释「为什么 IR 是树、字节码是线性的，而 `NestedBlockBuilder.new_block` 的临时缓冲切换恰好把树拍扁成了带括号的线性结构」。

> 提示：第 5 步依赖二进制反序列化能力；若仅做源码阅读，可改为用 `compile_tile(..., return_final_ir=True)` 打印 IR 文本，对照 IR 文本里的 `Loop` 与本讲的编码源码完成验证。MLIR 文本导出（`CUDA_TILE_DUMP_TILEIR`）依赖一个非公开的内部扩展 `cuda.tile_internal._internal_cext`，可能不可用（[_compile.py:L502-L514](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L502-L514)），**待本地验证**。

## 6. 本讲小结

- `generate_bytecode_for_kernel` 是 ir2bytecode 的总入口，按「函数」粒度工作：开 `writer.function` → 建 `BytecodeContext` → 绑定入口参数 → 调 `generate_bytecode_for_block` 递归翻译。
- `BytecodeContext` 是 IR 与字节码之间的桥梁，核心是一张 `_value_map: Var.name → bc.Value`，外加对 `IRContext` 类型/常量查表的代理；它强制每个 `Var` 只能绑定一次，呼应 SSA 语义。
- `CodeBuilder.new_op` 用单调递增的 `next_value_id` 为每条操作分配结果 id；`encode_*Op` 的固定套路是「先写 opcode/类型/属性/操作数进 `buf`，最后调 `new_op` 认领结果」。
- 控制流的嵌套 region 靠 `new_op_with_nested_blocks` + `NestedBlockBuilder.new_block` 实现：进入子块时切换到临时 `bytearray`、保存并恢复 `next_value_id` 与 `num_ops`，退出时把「指令数 + 子字节流」拼回主缓冲——这就是把「树形 IR」拍扁成「带括号的线性字节码」的关键机制。
- `Loop`（for 用 `encode_ForOp`、while 用 `encode_LoopOp`）与 `IfElse`（`encode_IfOp` + 两次 `new_block`）是这一机制的两个典型案例；分支以 `EndBranch`/`yield` 终止，循环以 `Continue`/`Break` 终止。

## 7. 下一步学习建议

- 本讲产出的字节码是一个**二进制容器**，其 section 布局（函数表/全局/常量/类型/字符串表）与版本门控是下一讲 [u7-l2 字节码格式与版本](u7-l2-bytecode-format-and-versioning.md) 的主题，建议紧接着读，理解 `write_bytecode` 的整体布局与本讲 `CodeBuilder.buf` 如何被装进函数 section。
- 想了解这段字节码如何被送进 `tileiras` 编译成 cubin，继续看 [u7-l3 tileiras 编译器调用与 cubin 生成](u7-l3-tileiras-and-cubin.md)。
- 若你对「IR 操作为什么有 `operand/attribute/nested_block` 三类声明式字段」还想加深理解，可回头重读 [u5-l5 IR 核心](u5-l5-ir-core.md) 与 [u5-l7 Stub 与实现注册](u5-l7-stub-and-impl-registry.md)；本讲的每个 `generate_bytecode` 正是建立在那套字段分类之上的。
- 想看「嵌套 region」在更高层（HIR）是如何被构造出来的，参考 [u5-l4 HIR 到 IR：hir2ir](u5-l4-hir-to-ir.md) 里的 `loop_impl` / `if_else_impl`——它们产出本讲消费的 `Loop` / `IfElse` 操作。
