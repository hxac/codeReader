# IR 核心：IRContext、Builder、Block、Var、Operation

## 1. 本讲目标

上一讲（u5-l4）我们看到，`hir2ir` 把「万物皆 Call」的 HIR 一层层分派，最终往一个「当前构建器」里写入一条条**具体的 IR Operation**。但那些 Operation、它们操作的值、装它们的容器，到底长什么样？本讲就把 Tile IR 的核心数据结构彻底拆开。

学完本讲，你应该能够：

1. 掌握 `IRContext` 作为「跨整个内核的全局注册表」的职责——所有 `Var` 的类型、常量、宽松类型、聚合值都集中存在这里，而不是存在 `Var` 对象上。
2. 理解 `Var` 是一个**轻量 SSA 值句柄**：它只持有名字、位置、上下文指针，真正的「属性」全部以名字为键委托给 `IRContext`。
3. 理解 `Operation` 基类如何用 `operand()` / `attribute()` / `nested_block()` 三种**声明式字段**把「输入」分类，并用 `__init_subclass__` 在类定义时自动登记 opcode 与字段清单。
4. 理解 `Builder` 是**线程局部**的「当前构建器」，用上下文管理器压栈/出栈，`add_operation(...)` 是发射 IR 的唯一入口。
5. 理解 `Block` 是带 `params`（入口参数）的指令容器，支持类列表的增删改与递归 `traverse`，控制流的循环体/分支体都是作为 `nested_block` 挂在某个 Operation 上的子 `Block`。

本讲覆盖五个最小模块：`IRContext`、`Var`、`Operation`、`Builder`、`Block`。它们全部定义在 [src/cuda/tile/_ir/ir.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py) 中，配合 [src/cuda/tile/_ir/core_ops.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/core_ops.py) 里的具体 Operation 子类来理解。

## 2. 前置知识

### 2.1 HIR 与 IR 的差别（来自 u5-l3、u5-l4）

HIR（高层 IR）是「万物皆 Call」的归一结构，连 `a + b` 都只是对 `operator.add` 的一次调用。IR（Tile IR）比它**具体**一层：每个操作都有明确的 `opcode`（如 `raw_binary_arith`、`loop`、`ifelse`）、明确的输入字段、明确的类型。HIR 是「被翻译的对象」，IR 是「翻译的产物」。

### 2.2 SSA（静态单赋值）

SSA 要求每个变量在整个函数里只被赋值一次。好处是数据流清晰、便于做优化 pass（如 u6 的死代码消除）。代价是控制流汇合点需要 phi 语义来「按来路选值」——cuTile 没有显式 phi 指令，而是把汇合语义编码进「块入口参数 + `PhiState` 类型/常量统一」（u5-l4 已讲）。本讲只需记住：**IR 里每产生一个新结果，就生成一个全新的 `Var`**，绝不复用旧名字承接新值。

### 2.3 flyweight（享元）模式

某些类型对象（如 `TileTy`）会被频繁、重复地创建。为了避免内存爆炸，`TileTy.__new__` 用一个全局字典缓存：相同 `(dtype, shape)` 永远返回同一个对象。这叫 flyweight——「相同内容只存一份」。理解这一点能解释为什么 IR 里到处可以直接用 `==` 比较类型。

### 2.4 threading.local

`Builder` 用 `threading.local` 保存「当前构建器」。`threading.local` 是一个**每个线程各自独立**的存储：线程 A 设的值，线程 B 看不到。这样多个线程同时编译不同内核时，各自的「当前 Builder」互不干扰，又无需把 Builder 沿调用链一层层传递。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/cuda/tile/_ir/ir.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py) | **本讲主战场**：`IRContext`、`Var`、`Builder`、`Block`、`Operation` 基类、三种字段工厂 `operand/attribute/nested_block`、`PhiState`/`LoopVarState`、`Mapper`。 |
| [src/cuda/tile/_ir/core_ops.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/core_ops.py) | 具体核心 Operation 子类（`TypedConst`、`Assign`、`TilePrintf`）与 `@impl` 实现（`loosely_typed_const`、`build_tuple`、`assign` 等），用于看「Operation 子类怎么写」。 |
| [src/cuda/tile/_ir/arithmetic_ops.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/arithmetic_ops.py) | `RawBinaryArithmeticOperation`（`add/sub/mul/...` 的统一 Operation）与本讲实践任务所参照的「add」范式。 |
| [src/cuda/tile/_ir/control_flow_ops.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py) | `Loop`/`IfElse`/`Continue`/`EndBranch`——展示 `nested_block()` 字段如何挂载子 Block。 |
| [src/cuda/tile/_ir/type.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/type.py) | `Type`/`TensorLikeTy`/`TileTy`（flyweight）——`Var` 持有的类型对象。 |
| [src/cuda/tile/_compile.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py) | `_IrKeeper.get_final_ir`——**真实**的 `IRContext`+`Builder` 构造现场，供实践任务对照。 |

## 4. 核心概念与源码讲解

### 4.1 IRContext：跨整个内核的全局注册表

#### 4.1.1 概念说明

`IRContext` 是「编译一个内核」所需的全部共享状态的容器。你可以把它理解成一个**以 `Var` 名字为主键的、横跨整个内核的数据库**：

- 谁有什么类型？→ `typemap`
- 谁是编译期常量、常量值是多少？→ `constants`
- 谁的「宽松类型（loose type）」是什么？→ `_loose_typemap`（loose type 来自 u2-l4 的 loosely typed 常量）
- 谁是「聚合值（tuple/dict/dataclass）」、内部由哪些子 Var 组成？→ `_aggregate_values`

此外它还保管名字分配器（`_all_vars` / `_counter_by_name` / `_temp_counter`）、字节码版本（`tileiras_version`）、类型扩展钩子（`typing_hooks`）。**关键设计决策**：这些属性全部存在 `IRContext` 上，而不是存在每个 `Var` 上。这样 `Var` 极度轻量，复制/克隆一个 Var 几乎零成本，类型信息又能在优化 pass 改写 IR 时集中维护。

#### 4.1.2 核心流程

一个内核的 IR 生命周期大致是：

1. `compile_tile` 调用 `_IrKeeper.get_final_ir`，**新建一个 `IRContext`**（每个 signature 一份）。
2. 进入一个 `Builder` 上下文（见 4.4）。
3. `_create_kernel_parameters` 用 `ctx.make_var(...)` 为每个内核参数创建入口 `Var`，并 `set_type` / `set_aggregate` 登记到 `IRContext`。
4. `hir2ir` 遍历 HIR，每算出一个中间结果就用 `ctx.make_temp(...)` 拿一个全新的临时 Var，类型/常量随之写入 `IRContext`。
5. 优化 pass（u6）读写这些表，做 DCE、常量传播、外提等。
6. 后端（u7）把每个 `Var` 的类型查出来，编进字节码类型表。

#### 4.1.3 源码精读

`IRContext` 的字段集中初始化在构造器里：

[ir.py:38-51](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L38-L51) —— `IRContext.__init__` 声明上面提到的全部表与计数器。

名字分配的核心是 `make_var`：遇到重名就追加 `.0/.1/...` 后缀，并把「显示名 → 原始名」的映射存进 `_all_vars`：

[ir.py:60-65](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L60-L65) —— `make_var` 保证名字唯一，返回一个绑定到本上下文的 `Var`。

`make_temp` 是「匿名临时变量」工厂，名字形如 `$0`、`$1`，是 SSA 临时值的来源：

[ir.py:70-71](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L70-L71) —— `make_temp` 用 `_temp_counter` 产生单调递增的 `$N` 名字。

当优化 pass 需要「照着一个旧 Var 造一个同类型新 Var」时（如循环不变量外提），`copy_type_information` 一次性把类型/宽松类型/常量/聚合值四张表里的记录整体复制过去：

[ir.py:76-84](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L76-L84) —— `copy_type_information` 在四张表里逐项搬运 src→dst。

#### 4.1.4 代码实践

详见本讲第 5 节综合实践。本模块的练习见 4.1.5。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Var` 不自己存 `type`，而要存到 `IRContext.typemap`？
**答案**：为了让 `Var` 保持轻量句柄（复制/克隆廉价），同时让类型信息能在优化 pass 改写 IR 时集中维护与查询；多张表共用同一个名字主键，也避免了类型/常量/聚合信息分散在多处导致不一致。

**练习 2**：`make_var("x", loc)` 被调用三次，三次返回的 `Var.name` 分别是什么？
**答案**：第一次 `"x"`（不冲突直接用），第二次 `"x.0"`，第三次 `"x.1"`——重名时追加 `.<计数>` 后缀，计数由 `_counter_by_name["x"]` 这个 `itertools.count` 产生。

---

### 4.2 Var：轻量 SSA 值句柄

#### 4.2.1 概念说明

`Var` 是 Tile IR 里的「值」。它本身**几乎不存东西**，只有三个字段：`name`（唯一标识）、`loc`（源码位置，用于报错）、`ctx`（指向所属 `IRContext`）。所有「这个值是什么类型、是不是常量、是不是聚合值」的查询，全部以 `self.name` 为键委托给 `ctx` 的某张表。

这种设计有两个直接后果：

- `Var` 是**不可变的标识**：同一个 SSA 值从头到尾用同一个 `Var` 对象（或至少同一个名字）指代，符合 SSA。
- 类型/常量可以**事后补充**：先 `ctx.make_var` 拿到一个「裸」Var，稍后推断出类型再 `set_type`——这正是 `Builder._add_operation` 创建临时 Var 后立即给它 `set_type` 的模式。

一个 Var 还可能是**聚合值**（tuple/dict/dataclass），此时它内部由一组子 Var 组成，`flatten_aggregate()` 会递归地把这棵树展平成叶子 Var 序列——这是常量传播（`PhiState`）和内核参数序列化的基础。

#### 4.2.2 核心流程

Var 上的操作分四组，每组都是「查表 / 写表」：

| 维度 | 读 | 写 |
|------|----|----|
| 类型 | `get_type()` / `get_type_allow_invalid()` | `set_type(ty)` |
| 宽松类型 | `get_loose_type()` | `set_loose_type(ty)` |
| 常量 | `is_constant()` / `get_constant()` | `set_constant(value)` |
| 聚合 | `is_aggregate()` / `get_aggregate()` | `set_aggregate(agg)` |

其中 `get_type` 在类型缺失或为 `InvalidType` 时会抛 `TileInternalError` / `TileTypeError`；而 `get_type_allow_invalid` 只在底层缺失时抛内部错误，允许把 `InvalidType`（一种「类型推断失败的占位」）原样返回，供汇合点统一处理。

#### 4.2.3 源码精读

`Var` 是一个泛型类 `Var[Generic[T]]`，构造器只接三个字段：

[ir.py:170-174](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L170-L174) —— `Var.__init__` 只保存 `name/loc/ctx`。

类型访问委托给上下文，且区分「严格失败」与「允许 InvalidType」两种：

[ir.py:179-189](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L179-L189) —— `get_type` 把 `InvalidType` 翻译成 `TileTypeError`；`get_type_allow_invalid` 只在键完全不存在时抛内部错误。

聚合展平是递归的，遇到聚合值就深入其子项，否则把自己作为叶子 yield 出来：

[ir.py:238-243](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L238-L243) —— `flatten_aggregate` 把任意嵌套的 tuple/aggregate 展平为叶子 Var 序列。

`Var` 的字符串表示就是它的名字，使得打印 IR 时可直接显示：

[ir.py:245-249](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L245-L249) —— `__repr__` 带位置，`__str__` 仅名字。

#### 4.2.4 代码实践

详见第 5 节。本模块练习见 4.2.5。

#### 4.2.5 小练习与答案

**练习 1**：一个 `Var` 对象身上有几个实例属性？分别是什么？
**答案**：三个：`name`（str）、`loc`（`Loc`）、`ctx`（`IRContext`）。其余如类型、常量、宽松类型、聚合值都不在 Var 上，而存在 `ctx` 的各张表里。

**练习 2**：`get_type()` 和 `get_type_allow_invalid()` 的区别是什么？
**答案**：当类型为 `InvalidType`（类型推断失败的占位）时，`get_type()` 会抛 `TileTypeError`，`get_type_allow_invalid()` 则原样返回 `InvalidType`；两者在「该 Var 完全没有类型记录」时都抛 `TileInternalError`。`allow_invalid` 版本供汇合点 `PhiState` 等「允许暂时拿不到确切类型」的逻辑使用。

---

### 4.3 Operation：声明式字段的具体操作基类

#### 4.3.1 概念说明

`Operation` 是所有具体 IR 操作的基类。它的设计哲学是「**用 dataclass 字段 + 元数据标签声明输入，由基类自动归类**」。一个 Operation 子类只需：

1. 继承 `Operation`，并通过类关键字 `opcode="..."` 声明助记符（如 `"raw_binary_arith"`），可选 `terminator=True`（是否终止所在 Block，如 `continue`/`yield`）和 `memory_effect=`（`NONE`/`LOAD`/`STORE`）。
2. 用三种字段工厂标注每个字段：
   - `operand()`：**操作数**——另一个 `Var`（或 `Var` 元组），代表「来自其他 Operation 的输入值」。
   - `attribute()`：**属性**——编译期常量，如算术运算的函数名 `"add"`、舍入模式。它不是 SSA 值，不参与数据流。
   - `nested_block()`：**嵌套块**——一个子 `Block`，用于控制流（循环体 `body`、分支体 `then_block`/`else_block`）。

基类在 `__init_subclass__`（类定义那一刻）扫一遍子类的 `__annotations__`，按字段元数据把它们分进三个清单 `_operand_names` / `_attribute_names` / `_nested_block_names`。这样后续的遍历、克隆、序列化、pretty-print 全都能统一处理，无需每个子类自己写样板代码。

每个 Operation 还带 `result_vars: tuple[Var, ...]`（它产出的结果值）和 `loc`（源码位置），并需实现 `generate_bytecode(ctx)` 以便后端（u7）把它编进字节码。

#### 4.3.2 核心流程

一个 Operation 子类从「定义」到「生效」的流程：

1. **定义类**：写 `@dataclass(eq=False)` + `class Foo(Operation, opcode="foo")`，字段用 `operand/attribute/nested_block` 标注。
2. **类定义触发** `__init_subclass__`：登记 `_opcode`、`_is_terminator`、`memory_effect`，并把字段名分成三组。
3. **构造实例**：由 `Builder._add_operation` 用 `op_class(**attrs_and_operands, loc=..., result_vars=...)` 实例化，`__post_init__` 校验 operand 是 Var、nested_block 是 Block。
4. **被遍历**：优化 pass 通过 `op.operands` / `op.attributes` / `op.nested_blocks` 统一访问；`op.all_inputs()` 拿到全部输入 Var 用于数据流分析。
5. **被克隆**：`op.clone(mapper)` 按三组字段分别复制，operand 经 `mapper` 重映射到新 Var。
6. **被序列化**：后端调 `op.generate_bytecode(ctx)` 产出字节码。

字段三分类对应一个 `IntEnum`：

[ir.py:486-489](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L486-L489) —— `_FieldKind` 把字段分成 OPERAND / ATTRIBUTE / NESTED_BLOCK。

三个工厂函数用 dataclass 的 `metadata` 打标签：

[ir.py:495-507](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L495-L507) —— `attribute/operand/nested_block` 都是 `dataclasses.field(...)` 的薄封装，靠 `metadata` 携带各自的 `_FieldKind`，并强制 `kw_only=True`。

#### 4.3.3 源码精读

`__init_subclass__` 是整个机制的引擎，它在类定义时遍历注解，把字段归入三组，并强制「每个字段必须三选一」：

[ir.py:519-545](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L519-L545) —— `__init_subclass__` 按 `metadata` 中的 `_FieldKind` 把字段名分到 operand/attribute/nested_block 三个清单，未标注的字段直接报错。

`__post_init__` 在每次实例化时校验：operand 必须是 Var（聚合 operand 仅允许 Array/List，参见注释），nested_block 必须是 Block：

[ir.py:547-562](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L547-L562) —— `__post_init__` 做结构校验。

三组字段的统一访问入口是 property：

[ir.py:594-616](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L594-L616) —— `operands`/`attributes`/`nested_blocks` 返回只读映射；`all_inputs` 把所有 operand（含元组里的）扁平 yield。

来看三个真实子类的写法，体会「attribute vs operand vs nested_block」。

`RawBinaryArithmeticOperation`（即 `a+b`/`a*b`/… 的统一 Operation）：`fn` 是属性（`"add"` 这种编译期常量），`lhs`/`rhs` 是 operand：

[arithmetic_ops.py:358-363](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/arithmetic_ops.py#L358-L363) —— `fn/rounding_mode/flush_to_zero` 是 attribute，`lhs/rhs` 是 operand。

`IfElse`：`cond` 是 operand，`then_block`/`else_block` 是两个 nested_block：

[control_flow_ops.py:274-277](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py#L274-L277) —— `IfElse` 用两个 `nested_block()` 挂载 then/else 子 Block。

`Continue`：它是 terminator（终止所在 Block），并带一个 operand `values`（回传给循环头的携带值）：

[control_flow_ops.py:432-433](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py#L432-L433) —— `Continue(opcode="continue", terminator=True)`，字段 `values` 是 operand。

最后看一个最简单的「纯 attribute」Operation——`TypedConst`，它把一个 Python 字面量烘焙成 IR 常量，没有 operand，只有一个 attribute `value`：

[core_ops.py:198-204](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/core_ops.py#L198-L204) —— `TypedConst` 仅含 attribute `value`，并实现 `generate_bytecode`。

#### 4.3.4 代码实践

详见第 5 节。本模块练习见 4.3.5。

#### 4.3.5 小练习与答案

**练习 1**：给 `RawBinaryArithmeticOperation`，它的 `fn`（如 `"add"`）为什么是 `attribute()` 而不是 `operand()`？
**答案**：`fn` 是**编译期常量**（运算助记符），不是来自其它 Operation 的 SSA 值，不参与数据流分析；`operand` 必须是 `Var`。把编译期常量放进 attribute，让遍历器/优化 pass 能一眼区分「输入值」与「配置参数」。

**练习 2**：一个 Operation 子类忘记给某字段加 `operand()/attribute()/nested_block()` 标注，会发生什么？
**答案**：`__init_subclass__` 在类定义时会抛 `TypeError`，提示该字段必须三选一——这是 cuTile 强制的「无裸字段」约定，保证每个字段都能被统一归类与处理。

**练习 3**：`op.result_var` 和 `op.result_vars` 有何区别？
**答案**：`result_vars` 是结果 Var 元组（多数 op 只有一个结果）；`result_var` 是便捷属性，当且仅当结果数为 1 时返回那个 Var，否则抛 `ValueError`。多结果 op（如返回 token 的 `TilePrintf`）必须用 `result_vars`。

---

### 4.4 Builder：线程局部的「当前构建器」

#### 4.4.1 概念说明

`Builder` 是发射 IR 的**唯一入口**。它的核心职责：维护一个「当前正在构建的指令序列 `_ops`」，提供 `add_operation(op_class, result_ty, attrs_and_operands)` 来实例化并追加一条 Operation。

两个关键设计：

1. **线程局部单例**。模块级 `_current_builder` 是一个 `threading.local`，属性 `.builder` 指向「当前 Builder」。`Builder.get_current()` 取它。于是 IR 生成的代码无需把 Builder 沿调用链传递——任何深处只要 `Builder.get_current()` 就能拿到当前构建器。
2. **上下文管理器压栈**。`with Builder(ctx, loc) as b:` 会把旧 Builder 存进 `_prev_builder`、把自己设为当前；退出时恢复。这天然支持嵌套（如 `enter_nested_block` 进入子 Block 的构建）。

`add_operation` 的工作模式（也是 SSA 的体现）：若调用者没提供 `result`，就用 `ctx.make_temp(loc)` 生成一个全新临时 Var；接着 `set_type` 给它登记类型；然后 `op_class(**attrs_and_operands, loc=loc, result_vars=...)` 实例化 Operation 并 append 到 `_ops`；若该 op 是 terminator，则把 builder 标记为 `is_terminated`（之后再 add 会断言失败）。

#### 4.4.2 核心流程

`add_operation(op_class, result_ty, attrs_and_operands)` 的内部步骤：

1. 若有 `block_restriction`，先校验该 op 是否被允许（如某些块禁止出现 store）。
2. 断言 `not is_terminated`——已终止的块不能再加操作。
3. 决定结果 Var：调用者传了 `result` 就复用（并 `force_type`），否则 `make_temp` 造新临时。
4. 给结果 Var `set_type(result_ty)`。
5. `op_class(**attrs_and_operands, loc=self._loc, result_vars=result_vars)` 实例化。
6. `_ops.append(new_op)`；若是 terminator 则 `is_terminated = True`。
7. 返回结果 Var（单结果直接返回该 Var，多结果返回元组）。

模块级还有三个便捷函数，全部转发到当前 Builder：`add_operation`、`add_operation_variadic`、`make_aggregate`（[ir.py:298-311](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L298-L311)），这样 impl 函数里写 `add_operation(...)` 即可，无需显式拿 Builder。

#### 4.4.3 源码精读

线程局部存储与「取当前 Builder」：

[ir.py:479-483](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L479-L483) —— `_current_builder` 是 `threading.local`，每线程独立的 `.builder`。

[ir.py:440-444](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L440-L444) —— `get_current()` 取当前 Builder，没有则断言失败。

上下文管理器压栈/出栈：

[ir.py:455-466](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L455-L466) —— `__enter__` 保存旧 Builder 并设自己为当前；`__exit__` 恢复。

`add_operation` 的核心实现 `_add_operation`（含单/多结果两条路径与 terminator 标记）：

[ir.py:370-407](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L370-L407) —— `_add_operation` 创建/复用结果 Var、设类型、实例化 Operation、append、按 terminator 置位。

`enter_nested_block` 是构造控制流体的标准手法：它先记下当前 Builder，新建一个空 `Block`，开一个**新 Builder** 作为子上下文 yield 出去；退出后把新 Builder 累积的 ops `extend` 进那个 Block：

[ir.py:469-476](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L469-L476) —— `enter_nested_block` 用「新 Builder + 末尾 extend」的模式产出嵌套 Block。

真实使用现场——`_IrKeeper.get_final_ir` 正是用 `with ir.Builder(ir_ctx, loc) as ir_builder:` 打开构建期，在里面创建参数并跑 `hir2ir`，最后把 `ir_builder.ops` 装进函数体 `Block`：

[_compile.py:390-404](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L390-L404) —— 这是 `IRContext` + `Builder` 在真实编译流水线里的构造现场。

#### 4.4.4 代码实践

详见第 5 节。本模块练习见 4.4.5。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `_current_builder` 要用 `threading.local` 而不是普通模块变量？
**答案**：cuTile 允许多线程并发编译不同内核。普通模块变量是全局共享的，会互相覆盖；`threading.local` 让每个线程看到各自的「当前 Builder」，互不干扰，又免去把 Builder 显式传遍调用链。

**练习 2**：在一个已经 `is_terminated=True` 的 Builder 上再调 `add_operation` 会怎样？
**答案**：`_add_operation` 第一关就 `assert not self.is_terminated` 失败，抛 `AssertionError`。这保证一个 Block 不会在终止操作（如 `yield`/`continue`）之后再追加操作，维护 IR 结构合法性。

---

### 4.5 Block：带入口参数的指令容器

#### 4.5.1 概念说明

`Block` 是「一段线性 IR 指令序列」的容器。除了 `_operations` 列表，它还带两个重要属性：

- `params: tuple[Var, ...]`：**块入口参数**。这是 cuTile 实现 phi 语义的关键——控制流汇合点（循环头、if-else 汇合）的「按来路选值」是通过「块带参数 + 调用方传入对应实参」表达的，等价于函数式 fold 的参数。函数体 Block 的 `params` 就是内核参数。
- `loc`：该 Block 对应的源码位置。

`Block` 还提供类列表接口（`__getitem__`/`__setitem__`/`__delitem__`/`__len__`/`__iter__`），方便优化 pass 直接按下标读写、替换、删除指令。`traverse()` 会**递归**地先深入每个 op 的 nested_block、再 yield 该 op，从而遍历整棵包含嵌套块的 IR 树。`remove_if(pred)` 在本块及所有嵌套块中删除满足谓词的 op 并返回删除总数。

一个 Operation 的 nested_block 字段持有的就是子 `Block`。所以一棵完整的 IR 是：「顶层函数体 Block」里装着一串 Operation，其中控制流 Operation（`Loop`/`IfElse`）又挂着子 Block，子 Block 里再装 Operation……形成树状结构。

#### 4.5.2 核心流程

构造一个函数体 Block 的典型流程（对照 4.4.3 的 `_compile.py` 现场）：

1. `with Builder(ctx, loc) as ir_builder:` 在临时构建期里发射所有顶层 op。
2. `func_body = Block(ctx, loc)` 新建函数体块。
3. `func_body.params = ...` 设置入口参数（内核参数序列）。
4. `func_body.extend(ir_builder.ops)` 把构建期累积的 op 装进去。

嵌套子 Block 则由 `enter_nested_block` 自动构造（见 4.4.3）。

#### 4.5.3 源码精读

`Block` 构造与基本容器接口：

[ir.py:717-728](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L717-L728) —— `Block.__init__` 初始化 `ctx/_operations/params/loc`；`append`/`extend` 追加 op。

类列表的按下标修改/删除最终都走 `_replace`：

[ir.py:742-752](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L742-L752) —— `__setitem__`/`__delitem__` 归一到 `_replace(slice, new_ops)`，支持切片替换。

递归遍历（先深入 nested_block，再 yield 当前 op）：

[ir.py:786-790](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L786-L790) —— `traverse` 递归 yield 整棵 IR 树的 op。

`remove_if` 在本块及所有嵌套块中删除，并累计计数（优化 pass 常用）：

[ir.py:792-803](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L792-L803) —— `remove_if` 先删本块，再对各 op 的 nested_block 递归。

`to_string` 把 Block 渲染成 `(params):\n  op1\n  op2 ...`，是 IR dump 的基础：

[ir.py:772-784](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L772-L784) —— `to_string` 渲染参数行与各 op。

#### 4.5.4 代码实践

详见第 5 节。本模块练习见 4.5.5。

#### 4.5.5 小练习与答案

**练习 1**：`Block.traverse()` 的遍历顺序是「先 op 后 nested_block」还是「先 nested_block 后 op」？为什么这个顺序有意义？
**答案**：**先 nested_block 后 op**（见源码：对每个 op 先 `yield from b.traverse()` 再 `yield op`）。这是一种后序遍历，常见用途是「自底向上」的转发数据流分析——先处理完嵌套块内的 op，再处理包裹它们的 op。

**练习 2**：函数体 Block 的 `params` 装的是什么？
**答案**：内核的入口参数序列（`Var`），即 `_create_kernel_parameters` 创建并展平后的非恒定参数 Var。它们既是「函数签名」也是 phi 语义里的「块入口参数」。

## 5. 综合实践

> **实践目标**：把本讲五个最小模块（`IRContext`/`Var`/`Operation`/`Builder`/`Block`）串成一条完整的「发射一条 IR」的心智链路，并能描述沿途每个对象的状态变化。

本实践分两部分：**A. 源码阅读型**（追踪真实构造现场）+ **B. 心智建模型**（手写一段最小 IR 并描述对象状态）。

### A. 追踪真实的 IR 构造现场

**操作步骤**：

1. 打开 [_compile.py:386-404](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L386-L404) 的 `_IrKeeper.get_final_ir`。
2. 逐行标注它如何：
   - 新建 `IRContext`（`ir.IRContext(...)`，带入 `tileiras_version` 与 `typing_hooks`）；
   - 用 `with ir.Builder(ir_ctx, loc) as ir_builder:` 打开构建期；
   - 在构建期内 `_create_kernel_parameters(...)` 创建参数 Var（写入 `IRContext` 的各张表）；
   - 调 `hir2ir(...)` 发射顶层 op（每条结果都是一个新的临时 Var）；
   - 退出 `with` 后，新建函数体 `Block`、设 `params`、`extend(ir_builder.ops)`。

**需要观察的现象 / 预期结果**：你能用一句话说清「`ir_builder.ops` 里的 op 是怎么从 Builder 流进 Block 的」——答：构建期 op 暂存在 Builder 的 `_ops`，退出上下文后由调用方手动 `extend` 进 Block；Builder 本身不持有 Block，它只负责「累积 op + 管理当前构建状态」。

### B. 心智建模：手写一条 `add` IR

下面是一段**示例代码（非项目原有代码）**，用本讲的五个组件构造「两个常量相加」的 IR。它演示了 `operand()/attribute()` 字段、`Builder.add_operation` 的「自动造临时 Var + 设类型」流程，以及结果如何进入 `Block`：

```python
# 示例代码（非项目原有，仅用于演示 IRContext/Var/Builder/Operation/Block 的协作）
from dataclasses import dataclass
from cuda.tile._exception import Loc
from cuda.tile._ir.ir import IRContext, Builder, Block, Operation, Var, operand
from cuda.tile._ir.type import TileTy
from cuda.tile._datatype import default_int_type  # 一个 int32 DType（flyweight）

@dataclass(eq=False)
class MyAdd(Operation, opcode="my_add"):
    lhs: Var = operand()
    rhs: Var = operand()

loc = Loc.unknown()
ctx = IRContext(log_ir_on_error=False,
                tileiras_version=<BytecodeVersion>,   # 真实值由编译流水线探测，见 u7-l2
                typing_hooks=<TypingHooks>)           # 真实值是 _TileTypingHooks，见 _compile.py:360

scalar_i32 = TileTy(default_int_type, ())             # flyweight：零维 tile = int32 标量

with Builder(ctx, loc) as b:
    a = ctx.make_var("a", loc); a.set_type(scalar_i32); a.set_constant(2)
    c = ctx.make_var("c", loc); c.set_type(scalar_i32); c.set_constant(3)
    # add_operation 内部：make_temp 造 "$0" → set_type(scalar_i32) → 实例化 MyAdd → append
    s = b.add_operation(MyAdd, scalar_i32, dict(lhs=a, rhs=c))

block = Block(ctx, loc)
block.extend(b.ops)
print(block)
```

**操作步骤**：在脑中逐行执行这段代码，然后回答「对象状态」问题（见下方「预期结果」）。

> 说明：上面 `<BytecodeVersion>` 与 `<TypingHooks>` 这两个构造参数在真实场景由编译流水线（u5-l2、u7-l2）提供，单独运行这段脚本需要先准备好它们（可分别取 `cuda.tile._bytecode.version.BytecodeVersion` 的某个版本与 `_compile._TileTypingHooks()`）。因此本实践的运行结果**待本地验证**——重点是心智模型，而非直接执行。

**需要观察的现象 / 预期结果**：逐对象描述状态——

| 对象 | 关键状态 |
|------|----------|
| `ctx`（IRContext） | `_all_vars` 含 `a/c/$0`；`typemap[a]=typemap[c]=typemap[$0]=scalar_i32`；`constants[a]=2, constants[c]=3`；`_temp_counter` 已递增到 1 |
| `a`、`c`（Var） | 各只有 `name/loc/ctx` 三字段；类型与常量通过名字查 `ctx` 得到 |
| `s`（Var） | 即 `ctx.make_temp` 产出的 `$0`；`name="$0"`；不是常量；类型 `scalar_i32` |
| Builder `b` | `_ops` 含 1 条 `MyAdd`；`is_terminated=False`（MyAdd 非 terminator） |
| MyAdd 实例 | `op="my_add"`；`operands={lhs:a, rhs:c}`；`attributes={}`；`result_vars=($0,)`；`loc=loc` |
| `block`（Block） | `params=()`；`operations` 含上述 1 条 op |

对照真实代码可验证：`MyAdd` 的字段归类发生在类定义时的 `__init_subclass__`（[ir.py:519-545](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L519-L545)）；`add_operation` 的「造临时 Var + 设类型 + append」逻辑在 [ir.py:370-407](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L370-L407)；`MyAdd` 的真实等价物是 `RawBinaryArithmeticOperation`（[arithmetic_ops.py:358-363](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/arithmetic_ops.py#L358-L363)），区别仅在于真实版本多几个 attribute（`fn/rounding_mode/flush_to_zero`）并实现了 `generate_bytecode`。

## 6. 本讲小结

- `IRContext` 是「一个内核一份」的全局注册表：所有 `Var` 的类型、常量、宽松类型、聚合值都以名字为键集中存此，并提供 `make_var`/`make_temp`/`copy_type_information`。
- `Var` 是轻量 SSA 句柄，只持 `name/loc/ctx`；类型/常量/聚合全部委托 `IRContext` 查表，可事后 `set_*` 补充。
- `Operation` 用 `operand()`/`attribute()`/`nested_block()` 三种声明式字段标注输入，`__init_subclass__` 在类定义时自动把它们归类并登记 opcode/terminator/memory_effect——子类只需写字段，无需样板代码。
- `Builder` 是线程局部的「当前构建器」，用上下文管理器压栈；`add_operation` 自动造临时 Var、设类型、实例化并 append，是发射 IR 的唯一入口。
- `Block` 是带 `params`（入口参数 = phi 语义）的指令容器，提供类列表接口与递归 `traverse`；控制流体作为 `nested_block` 挂在 Operation 上，形成树状 IR。
- 整棵 IR 的形态：顶层函数体 Block 装一串 Operation，其中 `Loop`/`IfElse` 等挂子 Block，子 Block 再装 Operation——`Var` 跨越这棵树传递数据，`IRContext` 是它们共享的后台。

## 7. 下一步学习建议

本讲建立了 Tile IR 的「数据骨架」。接下来：

- **u5-l6（类型系统）**：深入 `Type` 层级——`TileTy`（本讲提到的 flyweight）、`ArrayTy`/`ListTy`/`TupleTy` 等聚合类型如何 flatten 成可序列化的参数，以及 `TypingHooks` 扩展点。这是理解 `Var.set_type` 到底存了什么的下一层。
- **u5-l7（Stub 与实现注册）**：看 `@impl`/`ImplRegistry` 如何把用户 API（如 `ct.add`）接到本讲的 `Operation` 子类与 `Builder.add_operation`——补上「impl 函数里那一行 `add_operation(...)` 是怎么被找到的」。
- **u6-l1（Pass 流水线与 DCE）**：看优化 pass 如何遍历本讲的 `Block.traverse`、读写 `IRContext.typemap`、用 `Block.__setitem__` 改写指令序列。
- 建议阅读：[src/cuda/tile/_ir/ir.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py) 全文（尤其 `PhiState`/`LoopVarState`/`Mapper`，本讲只点到），以及 [src/cuda/tile/_ir/core_ops.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/core_ops.py) 中 `loosely_typed_const`/`build_tuple` 如何组合使用本讲五件套。
