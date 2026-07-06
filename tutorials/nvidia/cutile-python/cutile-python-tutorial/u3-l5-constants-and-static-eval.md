# 常量嵌入与编译期求值

## 1. 本讲目标

本讲聚焦 cuTile 中的「编译期」机制。到目前为止，你已经能写出 `load → compute → store` 的内核，并理解 tile 形状必须是编译期已知的 2 的幂。但有一类问题还没回答：

- 为什么 `ct.load(x, (pid,), shape=(tile_size,))` 里的 `tile_size` 必须是「编译期常量」，而 `pid` 却可以是运行时值？
- 如何在内核里对一个「只有运行时才知道真值」的条件做**编译期**断言，让它一旦不满足就直接报错、而不是把一个坏内核发到 GPU 上？
- 如何在编译期跑一小段**任意 Python 表达式**（甚至调用宿主函数），用它来在多个动态 tile 之间做选择？

学完本讲，你将掌握三个最小模块：

1. **Constant**：用 `ct.Constant[T]` 标注的参数会被「常量嵌入」，每个唯一取值都会单独生成一份 cubin。
2. **static_eval**：把一段 Python 表达式搬到**编译期**执行，常量当普通 Python 对象、动态 tile 当「代理对象」。
3. **static_assert**：在编译期断言一个条件，失败时抛出 `TileStaticAssertionError`。

并理解「常量性（constantness）」与上一讲（u2-l4）「严格类型常量 / 宽松类型常量」之间的衔接关系。

## 2. 前置知识

阅读本讲前，你需要已经掌握：

- **tile 与数组的数据模型**（u2-l2、u2-l3）：tile 形状编译期已知且每维为 2 的幂；数组是运行时可变的。
- **数据类型与类型提升**（u2-l4）：尤其要记得「字面量是 *宽松类型常量*（loosely typed），`ct.int16(5)` 是 *严格类型常量*（strictly typed）」。本讲会承接这条线。
- **load/store 范式**（u3-l1）：知道 `ct.bid`、`ct.load`、`ct.store` 的用法，以及 `index` 是瓦片空间索引、`shape` 是编译期常量。
- **kernel 是被翻译的 Python**（u3-l3）：tile code 里没有 Python 运行时，`if/for/while` 会被编译成 Tile IR；本讲的 `static_eval` 是这条规则里一个**特例**——它把一小段表达式重新交还给宿主 Python 执行。

一句话直觉：cuTile 的编译器在 `ct.launch` 时做 JIT。凡是「编译期就知道」的量，都可以被烘焙（bake）进生成的 IR；凡是「运行时才知道」的量，就只能作为内核参数传进去。本讲讲的就是**如何把一个量标记成编译期已知的，以及如何在编译期主动跑一段逻辑**。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/cuda/tile/_stub.py](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py) | 定义 `ConstantAnnotation` / `Constant` 类型提示，以及 `static_eval`、`static_assert` 的 stub 签名与文档。 |
| [src/cuda/tile/_annotated_function.py](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_annotated_function.py) | 从 kernel 参数注解里抽取 `constant_parameter_mask`（每个参数是否被常量嵌入）。 |
| [src/cuda/tile/_compile.py](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compile.py) | 在 IR 构建阶段把常量参数烘焙成字面量，使其从运行时签名中消失。 |
| [src/cuda/tile/_passes/ast2hir.py](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_passes/ast2hir.py) | 把 `static_eval` / `static_assert` 当作「关键字式函数」处理，把被包围的表达式编译成可在编译期执行的 lambda。 |
| [src/cuda/tile/_ir/static_eval_ops.py](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_ir/static_eval_ops.py) | `do_static_eval` / `do_static_assert` 的 IR 实现：真正在编译期执行表达式、判定断言。 |
| [src/cuda/tile/_dispatch_mode.py](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_dispatch_mode.py) | `StaticEvalMode`：编译期求值期间的调度模式，禁止在其中调用 tile 函数。 |
| [docs/source/execution.rst](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/docs/source/execution.rst) | 官方文档对「常量表达式 / 常量对象」与「常量嵌入」的权威定义。 |
| [test/test_static_assert.py](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_static_assert.py) | `static_assert` 各种成功 / 失败路径的端到端测试。 |

## 4. 核心概念与源码讲解

### 4.1 Constant：常量嵌入参数

#### 4.1.1 概念说明

很多内核参数天生就是「编译期才知道」的——最典型的就是 **tile 的形状**。硬件上的 tile load、张量核 mma、TMA 搬运，都要求 tile 形状在编译时固定下来。所以 `ct.load(x, (pid,), shape=(tile_size,))` 里的 `tile_size` 必须是个常量，而不能是「每次启动都不一样的运行时值」。

cuTile 用 `ct.Constant[T]` 类型注解来表达这件事。例如 quickstart 里的 vector_add：

```python
@ct.kernel
def vector_add(a, b, c, tile_size: ct.Constant[int]):
    pid = ct.bid(0)
    a_tile = ct.load(a, index=(pid,), shape=(tile_size,))   # tile_size 必须编译期已知
    ...
```

官方文档对「常量嵌入（constant embedded）」给出两条核心后果（见 [execution.rst:L163-L166](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/docs/source/execution.rst#L163-L166)）：

- 参数的**每一次使用**，都等价于被它的字面值替换。
- 参数的**每一个唯一取值**，都会单独生成一份内核的机器表示（cubin）。**注意：即使开启了 JIT 缓存，内核也是「每个唯一取值编译一次」。**

第二条是理解 `Constant` 性能模型的关键：它不是「运行时传参」，而是「把值钉死进代码里」。因此：

- 好处：值变成字面量后，编译器可以基于它做常量折叠、循环展开、对齐推理等优化（比如 `tile_size=16` 时编译器知道访存是对齐的）。
- 代价：取值组合多了会产生**编译爆炸**。例如三个 `Constant[int]` 参数各取 5 个值，就要编译 125 份内核。

> 与 u2-l4 的衔接：被嵌入的常量值在内核内部表现为「宽松类型常量」（字面量）。`Constant[int]` 里的 `int` 只是给静态类型检查器看的提示；真正决定算术结果类型的，仍是 u2-l4 的类型提升规则。所以 `tile_size + 1` 会得到一个宽松类型的常量，而 `tile_size + ct.int16(2)` 会得到严格类型 `int16` 常量。

#### 4.1.2 核心流程

把一个参数标记为 `Constant` 后，它在编译链路里经历这样的处理：

1. **抽取掩码**：`@ct.kernel` 装饰时，`get_annotated_function` 扫描每个参数的 `typing.Annotated` 元数据，看是否含 `ConstantAnnotation`，得到一个布尔元组 `constant_parameter_mask`。
2. **烘焙进 IR**：构建 IR 时，对于掩码为真的参数，编译器**不再**把它作为运行时参数创建 IR 变量，而是直接用 `loosely_typed_const(它的值)` 生成一个字面量常量节点；它也**不会**进入「非运行时参数列表」`nonconstant_flat_vars`。
3. **从签名消失**：因为该参数不进 `nonconstant_flat_vars`，最终 `func_body.params`（真正传给 GPU 的参数）里**没有它**——它已经被烘焙进函数体。
4. **每个唯一取值编译一次**：取值是签名的一部分。两个不同的 `tile_size` 对应两个不同的 IR（字面量不同）→ 两份 cubin；JIT 缓存的 key 包含该取值，所以「同值复用、异值重编」。

伪代码：

```
for each parameter (constraint, is_const, name):
    if is_const:
        var = loosely_typed_const(constraint.value)   # 烘焙成字面量
        # 不加入 nonconstant_flat_vars → 不进入运行时签名
    else:
        var = make_runtime_var(constraint)            # 普通 Scalar/Array/List 参数
        nonconstant_flat_vars.append(var)
func_body.params = 所有 nonconstant_flat_vars 的扁平化
```

#### 4.1.3 源码精读

`Constant` 本质是一个 `typing.Annotated` 提示，由一个空标记类 `ConstantAnnotation` 携带：

[src/cuda/tile/_stub.py:L974-L991](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py#L974-L991) 定义 `ConstantAnnotation` 与 `Constant = Annotated[T, ConstantAnnotation()]`，说明它可带类型（`Constant[int]`）也可不带（`Constant`，表示任意类型的常量）。

装饰器阶段，掩码被抽取出来：

[src/cuda/tile/_annotated_function.py:L31](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_annotated_function.py#L31) 逐参数构造 `constant_parameter_mask`；判定逻辑在 [L44](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_annotated_function.py#L44)——只要元数据里出现一个 `ConstantAnnotation` 实例，该参数就是常量参数。

kernel 类把这三个掩码（constant / int64_index / int64）透传给基类 `TileDispatcher`：

[src/cuda/tile/_execution.py:L121-L122](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_execution.py#L121-L122) 表明掩码会随 kernel 对象保存，供后续编译与签名推断使用。

真正「烘焙」发生在 `_create_kernel_parameters` 的常量分支：

[src/cuda/tile/_compile.py:L136-L140](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compile.py#L136-L140) ——对常量参数校验 `ConstantConstraint` 后，用 `loosely_typed_const(constraint.value, name=name)` 生成字面量。注意它走的是 `if is_const` 分支，**不进入** `else` 里的 `nonconstant_flat_vars.append(...)`（[L157](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compile.py#L157) 只在 else 分支里 append）。

随后 [src/cuda/tile/_compile.py:L284](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compile.py#L284) 用 `sum((vars for vars, _ in params.nonconstant_flat_vars), ())` 组装函数体的运行时参数列表——常量参数已经不在其中，因为它已被烘焙进 IR。

#### 4.1.4 代码实践

**实践目标**：亲手验证「每个唯一常量取值生成一份内核」。

**操作步骤**：

1. 把下面的脚本存为 `constant_embed.py`（它就是 quickstart 的简化版，见 [samples/quickstart/VectorAdd_quickstart.py:L15-L28](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/samples/quickstart/VectorAdd_quickstart.py#L15-L28)）：

   ```python
   import cupy as cp
   import cuda.tile as ct

   @ct.kernel
   def vector_add(a, b, c, tile_size: ct.Constant[int]):
       pid = ct.bid(0)
       a_tile = ct.load(a, index=(pid,), shape=(tile_size,))
       b_tile = ct.load(b, index=(pid,), shape=(tile_size,))
       ct.store(c, index=(pid,), tile=a_tile + b_tile)

   N = 1024
   a = cp.arange(N, dtype=cp.float32)
   b = cp.ones(N, dtype=cp.float32)
   c = cp.zeros(N, dtype=cp.float32)

   # 用两个不同的常量值各启动一次
   for ts in (16, 32):
       grid = (ct.cdiv(N, ts), 1, 1)
       ct.launch(cp.cuda.get_current_stream(), grid,
                 vector_add, (a, b, c, ts))
   ```

2. 开启「打印 cuTile IR」的日志，**用两个不同的 tile_size 各启动一次**：

   ```bash
   CUDA_TILE_LOGS=CUTILEIR python constant_embed.py
   ```

   （环境变量 `CUDA_TILE_LOGS` 的取值见 [src/cuda/tile/_context.py:L48-L52](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_context.py#L48-L52)：`CUTILEIR` 打印 cuTile IR，`TILEIR` 打印最终 TileIR。）

**需要观察的现象**：

- 终端会打印**两段** `==== CuTile IR for vector_add ====`，分别对应 `tile_size=16` 和 `tile_size=32`。
- 注意 IR 里 `load` 的 `shape` 直接是字面量 `16` / `32`，而不是一个运行时参数；`tile_size` **没有**出现在内核函数的运行时参数列表里。

**预期结果**：两次 `launch` 触发**两次独立编译**，产生两份不同的 cubin。若把两次都改成 `ts=16`，则第二次命中 JIT 缓存、不再重新编译。

> 待本地验证：上述日志行为依赖本机是否已正确安装 `cuda-tile[tileiras]` 并能访问 GPU；若无 GPU 环境，可改为「源码阅读型实践」——对照 [_compile.py:L136-L140](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compile.py#L136-L140) 解释「为什么常量参数不会出现在 `func_body.params` 里」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `tile_size` 的注解从 `ct.Constant[int]` 改成普通 `int`（不加 `Constant`），会发生什么？

**答案**：`tile_size` 不再被烘焙，而是变成一个普通的运行时 scalar 参数。但 `ct.load` 的 `shape` 要求编译期常量，于是会在编译期报错（shape 必须是常量）。这正是「tile 形状必须是常量」与「Constant 嵌入」绑在一起的原因。

**练习 2**：一个内核有 `TM: ct.Constant[int]`、`TN: ct.Constant[int]`、`TK: ct.Constant[int]` 三个常量参数，每个各取 4 个值。最坏情况下会编译多少份内核？

**答案**：4 × 4 × 4 = 64 份。这就是「常量嵌入」要把取值范围控制在合理内的原因——也是自动调优（u8-l3）在搜索 tile 尺寸时面临「编译爆炸」的根源。

---

### 4.2 static_eval：编译期求值

#### 4.2.1 概念说明

`ct.static_eval(expr)` 把 `expr` 这一段**用标准 Python 语义**（而不是 Tile 语义）在**编译期**执行一次。它的典型用途有两类：

1. **基于编译期条件，在多个动态值之间做选择**。例如 `N` 是常量，`x`、`y` 是动态 tile：

   ```python
   x_or_y = ct.static_eval(x if N % 2 == 0 else y)
   ```

   编译器在编译期根据 `N` 的奇偶，决定这段内核里到底用 `x` 还是 `y`——等价于「编译期 if」，从而避免在 GPU 上做运行时分支。

2. **在编译期跑一段任意 Python**（包括调用宿主函数、做循环算阶乘等），把结果作为常量喂回内核。

`static_eval` 的几个关键性质（见 stub 文档 [src/cuda/tile/_stub.py:L4216-L4247](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py#L4216-L4247)）：

- **被引用的变量按性质区分对待**：
  - 若是编译期常量（如 `Constant` 参数、字面量），它在表达式里就是一个**普通 Python 对象**（常量整数 3 就是一个值为 3 的 `int`）。
  - 若是动态值（tile / array），它被替换成一个**代理对象（proxy）**，只能查询编译期属性，如 `x.shape`、`x.dtype`，不能做运行时运算。
- **不允许执行任何运行时操作**：`ct.static_eval(x + 1)`（`x` 是动态 tile）会报错，因为 `+` 对 tile 是运行时运算。
- **它像关键字一样工作**：必须**直接按名字**调用（`ct.static_eval(...)` 或 `cuda.tile.static_eval(...)`），不能把它赋给别的变量再调用；它跳过对被包围表达式的 Tile 翻译，并允许使用完整 Python 语法（不受 tile code 子集限制）。

#### 4.2.2 核心流程

`static_eval` 的实现是「在 ast2hir 阶段把表达式包成 lambda，在 IR 构建阶段执行 lambda」：

1. **发现引用的局部变量**：ast2hir 把 `expr` 包成嵌套 lambda，借助 Python 的 `co_freevars` 自动找出它**实际引用**了哪些局部名。
2. **编译成可执行的 lambda**：把这些引用名作为参数，生成 `lambda p1, p2, ...: expr`，封装成 HIR 的 `StaticEvalExpression`（携带编译好的 Python 函数对象与「求值类别」）。
3. **发出 HIR 调用**：生成 `do_static_eval(StaticEvalExpression, *被引用局部量的 HIR 值)`。
4. **编译期执行**：IR 构建时，`do_static_eval_impl` 把每个局部量的 IR Var 转成「符号/代理」（常量→普通 Python 值，动态→代理对象），进入 `StaticEvalMode`，然后**真正调用** `expr.compiled_expr(*proxies)`——这一步运行在宿主 Python 解释器里。
5. **结果回灌**：把 Python 结果用 `sym2var` 转回 IR Var；`StaticEvalMode` 保证执行期间一旦调用 tile 函数就报错。

伪代码：

```
# ast2hir 阶段
used = sorted(inner_lambda.__code__.co_freevars)      # 表达式真正用到的局部名
final = compile(lambda *used: expr)
emit do_static_eval(StaticEvalExpression(final), *(load(name) for name in used))

# IR 构建阶段 (do_static_eval_impl)
proxies = [var2sym(v) for v in local_var_values]       # 常量→值, tile→代理
with StaticEvalMode(kind):
    result = expr.compiled_expr(*proxies)              # 在宿主 Python 里执行
return sym2var(result)                                 # 结果转回 IR
```

#### 4.2.3 源码精读

ast2hir 先把 `static_eval` / `static_assert` / `static_iter` 登记为「关键字式函数」，它们不走普通调用翻译：

[src/cuda/tile/_passes/ast2hir.py:L304-L305](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_passes/ast2hir.py#L304-L305) 列出这三个关键字式函数及其名字。

核心打包逻辑在 `_call_static_eval`：

[src/cuda/tile/_passes/ast2hir.py:L359-L392](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_passes/ast2hir.py#L359-L392) 把表达式层层包成 lambda 来探测 `co_freevars`（[L382](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_passes/ast2hir.py#L382)），再生成最终 lambda（[L387-L388](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_passes/ast2hir.py#L387-L388)），最后发出 `do_static_eval` 调用（[L391-L392](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_passes/ast2hir.py#L391-L392)）。注意 [L376-L379](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_passes/ast2hir.py#L376-L379) 会拒绝「在海象运算符 `:=` 里给局部变量赋值」。

真正执行的是 `do_static_eval_impl`：

[src/cuda/tile/_ir/static_eval_ops.py:L44-L70](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_ir/static_eval_ops.py#L44-L70) ——[L47](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_ir/static_eval_ops.py#L47) 把每个局部 Var 转成符号/代理；[L48-L60](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_ir/static_eval_ops.py#L48-L60) 进入 `StaticEvalMode` 后调用 `expr.compiled_expr(*local_proxies)`，任何非 `TileError` 的异常都被包成 `TileStaticEvalError`；[L62-L70](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_ir/static_eval_ops.py#L62-L70) 按求值类别处理结果（断言消息→字符串常量、`static_iter`→元组、普通→`sym2var`）。

`StaticEvalMode` 负责「禁止在其中调用 tile 函数」：

[src/cuda/tile/_dispatch_mode.py:L39-L54](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_dispatch_mode.py#L39-L54) 重写 `call_tile_function_from_host`：在编译期求值期间，任何 tile 函数（包括 `static_eval` 自身）被调用都会抛 `TileStaticEvalError`。

#### 4.2.4 代码实践

**实践目标**：用 `static_eval` 在编译期根据常量 `N` 的奇偶，选择两个动态 tile 之一。

**操作步骤**：

```python
import torch, cuda.tile as ct

@ct.kernel
def pick(x, y, N: ct.Constant[int]):
    # N 是常量，在编译期就能判断奇偶；x、y 是动态 tile
    chosen = ct.static_eval(x if N % 2 == 0 else y)
    ct.store(x, index=(0,), tile=chosen)   # 仅作占位，验证能编译通过

x = torch.zeros(4, dtype=torch.int32, device="cuda")
y = torch.full((4,), 7, dtype=torch.int32, device="cuda")
ct.launch(torch.cuda.current_stream(), (1,), pick, (x, y, 2))   # N=2 → 选 x
ct.launch(torch.cuda.current_stream(), (1,), pick, (x, y, 3))   # N=3 → 选 y
```

**需要观察的现象**：

- `N=2` 与 `N=3` 会编译成两份不同的内核（因为 `N` 是 `Constant`，且 `static_eval` 在编译期就把 `if/else` 折叠掉了）。
- 在 `CUDA_TILE_LOGS=CUTILEIR` 的输出里，对应内核里只会出现 `load x` 或 `load y` 之一，看不到运行时分支。

**预期结果**：两次启动都成功编译并运行；两次产生两份不同的 IR。

**另一个「源码阅读型」小实践**：阅读 [test/test_static_assert.py:L14-L18](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_static_assert.py#L14-L18) 中的 `factorial` 宿主函数，并注意 [L24](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_static_assert.py#L24) 里 `ct.static_assert(factorial(n) < 100)` 能在编译期调用它——这说明 `static_eval` 系列可以在编译期执行**任意 Python 函数**，只要它只依赖编译期常量。

> 待本地验证：运行结果需 GPU 环境；无环境时可仅做源码阅读。

#### 4.2.5 小练习与答案

**练习 1**：`x` 是动态 tile，写 `ct.static_eval(x + 1)` 会怎样？

**答案**：报错。`x` 是动态值，在 `static_eval` 里以代理对象出现，对它做 `+` 属于运行时运算，不在编译期求值允许的范围内。`static_eval` 只允许「常量算常量」或「读取动态值的编译期属性（如 `.shape`）」。

**练习 2**：`static_eval` 里的表达式允许使用 `for`、`def`、调用宿主函数吗？

**答案**：允许。被包围的表达式走的是**完整 Python 语法**（见 stub 文档 [L4245-L4246](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py#L4245-L4246)），不受 tile code 的 Python 子集限制；`test_static_assert.py` 里的 `factorial` 就是明证。唯一的硬限制是「不得给局部变量赋值（如 `:=`）」与「不得执行运行时操作 / 不得调用 tile 函数」。

---

### 4.3 static_assert：编译期断言

#### 4.3.1 概念说明

`ct.static_assert(cond, message=None)` 在**编译期**断言 `cond` 为真。它的意义在于**把错误前移**：与其让一个不合法的内核配置在 GPU 上跑出垃圾结果或超时，不如在 `ct.launch`（编译）阶段就直接拒绝。

它完全建立在 `static_eval` 之上（stub 文档 [src/cuda/tile/_stub.py:L4251-L4306](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py#L4251-L4306)）：

- `cond` 用与 `static_eval` 完全相同的规则求值，且**必须是编译期常量布尔**。
- 若 `cond` 为 `True`：编译正常继续，`message` 表达式**不会被求值**。
- 若 `cond` 为 `False`：用 `static_eval` 语义求值 `message`（若为 `None` 当作空串，否则 `str()` 转字符串），然后抛出 `TileStaticAssertionError`，信息形如 `"Static assertion failed: <message>"`。

典型用法是校验 `Constant` 参数的取值，例如断言 tile 形状合法、两个输入 dtype 相同等。和 `static_eval` 一样，它**必须直接按名字调用**，被它包围的表达式走完整 Python 语法。

#### 4.3.2 核心流程

1. **ast2hir 分派**：`static_assert(cond, msg=None)` 的 `cond` 与 `msg` 各自走一遍 `_call_static_eval`，类别分别是 `STATIC_ASSERT_CONDITION` 与 `STATIC_ASSERT_MESSAGE`；`msg` 被包成一个 HIR `Block`（惰性求值，只有断言失败才执行），最后发出 `do_static_assert(condition, message_block)`。
2. **IR 实现 `do_static_assert_impl`**：
   - 校验 `condition` 是常量（否则 `TileTypeError`）；
   - 校验 `condition` 是布尔（否则 `TileTypeError`）；
   - 若 `condition` 为真 → 直接返回，什么都不做；
   - 若为假 → 求值 message block，取其常量字符串，抛 `TileStaticAssertionError`。
3. **关键字保护**：若把 `static_assert` 赋给变量再调用（间接调用），落到 `static_assert_impl`，它直接抛 `TileSyntaxError`。

#### 4.3.3 源码精读

ast2hir 对 `static_assert` 的处理（参数个数校验 + cond/msg 分别打包）：

[src/cuda/tile/_passes/ast2hir.py:L323-L336](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_passes/ast2hir.py#L323-L336) ——`static_assert` 接收 1 或 2 个位置参数（[L325-L329](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_passes/ast2hir.py#L325-L329)），`message` 用 `STATIC_ASSERT_MESSAGE` 类别惰性打包，`condition` 用 `STATIC_ASSERT_CONDITION` 打包，最终发出 `do_static_assert`（[L336](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_passes/ast2hir.py#L336)）。

断言判定的 IR 实现：

[src/cuda/tile/_ir/static_eval_ops.py:L112-L133](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_ir/static_eval_ops.py#L112-L133) ——[L114-L115](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_ir/static_eval_ops.py#L114-L115) 校验「必须是编译期常量」；[L117-L119](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_ir/static_eval_ops.py#L117-L119) 校验「必须是布尔」；[L121-L122](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_ir/static_eval_ops.py#L121-L122) 为真则直接返回；[L124-L133](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_ir/static_eval_ops.py#L124-L133) 为假时求值 message block 并抛 `TileStaticAssertionError`。

间接调用保护：

[src/cuda/tile/_ir/static_eval_ops.py:L32-L35](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_ir/static_eval_ops.py#L32-L35) ——若不是按名字直接调用，落到这个 `@impl(ct.static_assert)`，直接抛 `TileSyntaxError("static_assert() must be used directly ...")`。`static_eval` 有对称的保护（[L26-L29](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_ir/static_eval_ops.py#L26-L29)）。

错误路径的端到端验证见测试：

- [test/test_static_assert.py:L21-L33](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_static_assert.py#L21-L33)：`n=4` 时 `factorial(4)=24<100` 通过；`n=7` 时 `factorial(7)=5040` 断言失败，抛 `TileStaticAssertionError`，信息以 `"Static assertion failed\n"` 开头。
- [test/test_static_assert.py:L105-L114](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_static_assert.py#L105-L114)：条件不是布尔（`ct.static_assert(n)`，`n` 是整数常量）→ `TileTypeError`。
- [test/test_static_assert.py:L117-L128](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_static_assert.py#L117-L128)：条件不是编译期常量（`cond = n > 2`，`n` 是运行时 scalar）→ `TileTypeError`，提示「must be a compile-time constant」。
- [test/test_static_assert.py:L92-L102](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_static_assert.py#L92-L102)：把 `ct.static_assert` 赋给变量再调用 → `TileSyntaxError`（必须直接按名字调用）。

#### 4.3.4 代码实践

**实践目标**：用 `static_assert` 在编译期校验 `Constant` 参数，并观察「通过 / 失败」两种情况。

**操作步骤**：

```python
import torch, cuda.tile as ct

def factorial(n):           # 宿主函数，编译期可调用
    r = 1
    for i in range(1, n + 1):
        r *= i
    return r

@ct.kernel
def kernel(x, n: ct.Constant):
    ct.static_assert(factorial(n) < 100,
                     f"{n}! = {factorial(n)}, that's too much.")
    ct.scatter(x, (), n)

x = torch.zeros((), dtype=torch.int32, device="cuda")
ct.launch(torch.cuda.current_stream(), (1,), kernel, (x, 4))   # 4!=24<100 → 通过

x = torch.zeros((), dtype=torch.int32, device="cuda")
ct.launch(torch.cuda.current_stream(), (1,), kernel, (x, 7))   # 7!=5040 → 断言失败
```

**需要观察的现象**：

- 第一次 `launch`（`n=4`）正常完成，`x` 被写入 4。
- 第二次 `launch`（`n=7`）在**编译阶段**抛出 `TileStaticAssertionError`，信息为 `"Static assertion failed: 7! = 5040, that's too much."`。注意 message 是用 f-string 在编译期求值得到的——这验证了 `message` 走 `static_eval` 语义。

**预期结果**：与 [test/test_static_assert.py:L51-L66](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_static_assert.py#L51-L66) 一致。

> 待本地验证：需 GPU 环境运行；无环境时可对照该测试断言理解行为。

#### 4.3.5 小练习与答案

**练习 1**：写 `ct.static_assert(x.dtype == y.dtype)`（`x`、`y` 是动态 tile），能编译通过吗？

**答案**：能。`x.dtype` / `y.dtype` 是动态 tile 的**编译期属性**（dtype 在编译期已知），`==` 比较的是两个 `DType` 常量对象，结果是编译期布尔常量，满足 `static_assert` 的要求。这正是 stub 文档 [L4276-L4277](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py#L4276-L4277) 给出的范例。

**练习 2**：`ct.static_assert(n > 0)`，其中 `n` 是**未加 `Constant`** 的普通 scalar 参数，会怎样？

**答案**：抛 `TileTypeError`，提示「`static_assert() condition must be a compile-time constant`」。`n` 没有被常量嵌入，`n > 0` 是运行时布尔而非编译期常量，不满足断言要求。要让它在编译期可用，应把 `n` 标注为 `ct.Constant`。

---

## 5. 综合实践

把三个模块串起来：写一个带两个 `Constant` 参数的小内核，用 `static_assert` 在编译期校验它们的合法性，用 `static_eval` 在编译期做一个分支选择，并观察「每个唯一取值编译一份」。

```python
import torch, cuda.tile as ct

@ct.kernel
def k(out, x, y, TILE: ct.Constant[int], MODE: ct.Constant[int]):
    # (1) static_assert：编译期校验 TILE 是 2 的幂且不小于 4
    ct.static_assert(TILE >= 4 and (TILE & (TILE - 1)) == 0,
                     f"TILE must be a power of two >= 4, got {TILE}")

    # (2) static_eval：编译期根据 MODE 选 x 或 y（等价于「编译期 if」）
    chosen = ct.static_eval(x if MODE == 0 else y)

    pid = ct.bid(0)
    a = ct.load(chosen, index=(pid,), shape=(TILE,))
    ct.store(out, index=(pid,), tile=a)

out = torch.zeros(8, dtype=torch.int32, device="cuda")
x = torch.ones(8, dtype=torch.int32, device="cuda")
y = torch.full((8,), 2, dtype=torch.int32, device="cuda")

# 四种 (TILE, MODE) 组合 → 最多编译四份内核
for TILE in (4, 8):
    for MODE in (0, 1):
        grid = (ct.cdiv(8, TILE), 1, 1)
        ct.launch(torch.cuda.current_stream(), grid, k, (out, x, y, TILE, MODE))
```

验证清单：

1. **常量嵌入**：用 `CUDA_TILE_LOGS=CUTILEIR` 观察，四次 `launch` 产生最多四段不同的 IR；`load` 的 `shape` 直接是字面量 4 或 8。
2. **static_assert 通过**：TILE=4、8 都满足「2 的幂且 ≥4」，正常编译。
3. **static_assert 失败**：把某次调用改成 `TILE=6`，应在编译期抛 `TileStaticAssertionError`，信息包含 `got 6`。
4. **static_eval 折叠**：在 IR 里只看到对 `x` 或对 `y` 的 load 之一，对应 `MODE` 的取值；没有运行时分支。

> 待本地验证：数值与日志行为需 GPU 环境确认；无环境时可做源码阅读——对照本讲引用的源码，解释「为什么 TILE=6 会在编译期、而不是运行时被拒绝」。

## 6. 本讲小结

- **Constant** 是一种类型注解，把参数标记为「常量嵌入」：编译时用 `loosely_typed_const` 把值烘焙成 IR 字面量，使其从运行时签名中消失；**每个唯一取值单独编译一份 cubin**，因此要警惕取值组合带来的编译爆炸。
- **常量嵌入的值在内核里表现为宽松类型常量**（承接 u2-l4），其算术结果类型仍由类型提升规则决定。
- **static_eval** 在**编译期**用完整 Python 语义执行一段表达式：常量当普通 Python 对象、动态 tile/array 当代理对象（只能查 `.shape`/`.dtype`），禁止运行时运算与 tile 函数调用，且必须直接按名字调用。
- **static_assert** 建立在 `static_eval` 上：编译期断言一个常量布尔条件，失败时抛 `TileStaticAssertionError`，把参数错误前移到编译阶段。
- 三者共享「关键字式函数」机制：在 ast2hir 里被特殊分派（[_passes/ast2hir.py:L304-L305](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_passes/ast2hir.py#L304-L305)），被包围的表达式跳过 Tile 翻译、走完整 Python 语法。
- **可观测手段**：`CUDA_TILE_LOGS=CUTILEIR` 打印 cuTile IR，可用来确认常量是否被烘焙、分支是否被折叠。

## 7. 下一步学习建议

- **下一讲 u3-l6（矩阵乘与张量核）** 会大量使用 `Constant[int]` 来传入 `TM/TN/TK` 分块尺寸，并依赖本讲的「常量嵌入」来让 `mma`/`load` 的形状在编译期固定——本讲是它的直接前置。
- 想理解「常量嵌入」在更底层如何影响字节码与缓存，可继续阅读 u7（后端字节码）与 u7-l4（JIT 磁盘缓存），重点看缓存 key 如何纳入常量取值。
- 想理解「关键字式函数」与 HIR 的关系，可在学完 u5（编译前端）后回头重读 [src/cuda/tile/_passes/ast2hir.py](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_passes/ast2hir.py) 与 [src/cuda/tile/_ir/static_eval_ops.py](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_ir/static_eval_ops.py)，你会更清楚地看到「编译期执行一段宿主 Python」是如何实现的。
