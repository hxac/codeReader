# Lowering 变换：load_store 与 param_only_expr

## 1. 本讲目标

本讲聚焦 Tilus IR 变换流水线中的三处「降级」（lowering）变换：`lower_assume`、`lower_param_only_expr`、`lower_load_store`。它们都做同一类事——**把抽象、贴近用户意图的高层指令，改写为更具体、更贴近后端能直接生成的形式**。

读完本讲，你应该能够：

- 说清楚「lowering（降级）」在编译器里到底降的是什么、为什么要降。
- 看懂 `lower_load_store` 如何把 `LoadGlobalInst`/`StoreGlobalInst`/`CopyAsyncInst` 这些**张量级**访存指令，改写成带「指针 + 逐元素地址表达式 + 掩码」的 `*GenericInst`。
- 理解 `lower_param_only_expr` 为什么要求「只依赖参数」，以及它如何用 `LetStmt` 绑定把中间变量内联掉。
- 理解 `lower_assume` 如何把用户写的 `self.assume(a % c == 0)` 提示，落地成 `metadata.param2divisibility`，供后续标量分析消费。
- 会用 `debug.dump_ir` 抓取某条 Pass 前后的 IR，亲手对比 `lower_load_store` 的产物。

## 2. 前置知识

本讲默认你已经学过：

- **u3-l3 / u3-l4**：Tilus IR 的语句树（`Program/Function/Stmt/InstStmt`）以及 `Instruction(output/inputs/attributes)` 与四种 `Tensor` 的身份相等语义。
- **u3-l2**：转译器如何把 `self.load_global(...)` 这类调用变成 `InstStmt`。
- **u5-l1**：`Pass` 框架与 `IRRewriter`（`visit_*` 分派 + `memo` 记忆化，返回 `None` 会把 `InstStmt` 塌缩为空 `SeqStmt`）。
- **u5-l2**：默认流水线 `get_default_passes()` 的 12 个 Pass 及其顺序。

### 什么是 lowering（降级）

Tilus 是分两层的编译器（详见 u3-l1）：

1. **高层（Tilus IR）**：以**张量**为一等公民，指令直接操作张量，描述「意图」。例如 `LoadGlobalInst` 说的是「把这个全局张量的某个 tile 加载到这个寄存器张量」。
2. **底层（Hidet IR / CUDA C）**：贴近硬件，最终要变成「每个线程对哪个标量地址做什么」。

**Lowering 就是把高层指令逐步改写成底层能照着生成的形式。** 它不是优化（不追求更快），而是「翻译准备」——把只有高层才懂的语义（张量、布局、切片偏移）展开成底层看得懂的要素（指针、标量地址、循环、掩码）。

本讲的三条 Pass 都属于 lowering：

| Pass | 降的是什么 | 产物 |
| --- | --- | --- |
| `lower_assume` | `AssumeInst`（用户提示） | `metadata.param2divisibility`（整除性字典） |
| `lower_param_only_expr` | 引用了中间变量的 `grid_blocks` / 全局张量大小 | 只依赖参数的表达式 |
| `lower_load_store` | `LoadGlobalInst`/`StoreGlobalInst`/`CopyAsyncInst` | `*GenericInst`（指针 + 逐元素偏移 + 掩码） |

它们在默认流水线中的位置（[python/tilus/transforms/\_\_init\_\_.py:31-45](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/__init__.py#L31-L45)）：

```
1  declare_to_let
2  let_propagation
3  lower_assume            ← 本讲
4  lower_param_only_expr   ← 本讲
5  analyze_scalar
6  lower_print_tmem_tensor
7  layout_inference
8  lower_load_store        ← 本讲
9  layout_inference
10 bound_aware_simplify
11 analyze_scalar
12 dead_code_elimination
```

注意三条 Pass 不挨在一起：`lower_assume` 和 `lower_param_only_expr` 在流水线**前段**（提示落地、表达式规范化），`lower_load_store` 在流水线**中段**、被两次 `layout_inference` 夹在中间（因为它需要先读寄存器张量已经推理出来的布局，再改写访存指令）。这一点 u5-l2 已解释过，本讲不再重复，只讲三条 Pass 各自的内部机制。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/tilus/transforms/lower_load_store.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/lower_load_store.py) | 把三类高层访存指令改写为 `*GenericInst` |
| [python/tilus/transforms/lower_param_only_expr.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/lower_param_only_expr.py) | 把 `grid_blocks` 与全局张量大小里引用的中间变量内联成参数 |
| [python/tilus/transforms/lower_assume.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/lower_assume.py) | 把 `AssumeInst` 解析成 `param2divisibility` |
| [python/tilus/ir/instructions/generic.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/instructions/generic.py) | `LoadGlobalInst`/`StoreGlobalInst`/`AllocateGlobalInst` 与降级后的 `LoadGlobalGenericInst`/`StoreGlobalGenericInst` |
| [python/tilus/ir/instructions/cuda/cp_async.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/instructions/cuda/cp_async.py) | `CopyAsyncInst` 与降级后的 `CopyAsyncGenericInst` |
| [python/tilus/ir/instructions/hints.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/instructions/hints.py) | `AssumeInst` 定义 |
| [python/tilus/ir/builders/stmt_builder.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/builders/stmt_builder.py) | `load_global_generic`/`store_global_generic`/`copy_async_generic`/`tensor_ptr` 构造方法 |
| [python/tilus/hidet/ir/utils/index_transform.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/hidet/ir/utils/index_transform.py) | `index_within_bound`（越界掩码生成） |
| [python/tilus/ir/func.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/func.py) | `Metadata`（`grid_blocks`/`param2divisibility`） |

---

## 4. 核心概念与源码讲解

### 4.1 lower_load_store：全局访存降级为 generic 形式

#### 4.1.1 概念说明

用户在 `__call__` 里写的 `self.load_global(g, offsets, dims)`、`self.store_global(g, r, offsets, dims)`、`self.copy_async(g, s, ...)`，转译后分别成为三条**张量级**指令（operand 是张量）：

- [LoadGlobalInst](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/instructions/generic.py#L70-L77)：从一个 `GlobalTensor` 的某个切片，加载到一个 `RegisterTensor`。只记 `offsets`（每维基址）和 `dims`（切片跨哪些维），**不记逐元素地址**。
- [StoreGlobalInst](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/instructions/generic.py#L80-L87)：反向，把寄存器张量写回全局张量的某个切片。
- [CopyAsyncInst](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/instructions/cuda/cp_async.py#L25-L53)：异步拷贝，全局→共享内存。

这种「张量 + 切片」的写法对人很友好，但后端 emitter 要把它变成 PTX 的 `ld.global`/`st.global`/`cp.async`，必须知道**每个线程具体访问哪个标量地址**。`lower_load_store` 就是把这件事提前做好：在 IR 层把张量级指令展开成带「指针 + 逐元素偏移函数 + 越界掩码函数」的 `*GenericInst`。

降级后的指令长这样（关键字段都是标量，不再依赖张量语义）：

- [LoadGlobalGenericInst](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/instructions/generic.py#L238-L255)：`ptr`（指向全局内存的标量指针 Var）、`axes`（遍历寄存器张量逐元素的一组索引 Var）、`offset`（每个元素的线性偏移 Expr）、`mask`（越界掩码 Expr）。
- [StoreGlobalGenericInst](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/instructions/generic.py#L258-L275)：字段同上，输入是待写的寄存器张量。
- [CopyAsyncGenericInst](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/instructions/cuda/cp_async.py#L56-L70)：`dst` 是共享张量，外加 `ptr/offset/mask/evict`。

一句话总结搬运语义：**降级前**指令说「搬这块 tile」；**降级后**指令说「对寄存器/共享张量里的每一个元素，按 `ptr + offset(i)` 取地址、用 `mask(i)` 决定是否真读写」。

#### 4.1.2 核心流程

降级的关键是先把 `(offsets, dims, layout)` 这组「切片几何信息」翻译成两个函数：

- `f_offset(indices) -> 线性偏移`：给定寄存器张量逐元素的索引 `indices`，算出它在全局内存里的线性偏移。
- `f_mask(indices) -> bool`：给定同样的索引，判断该元素是否在全局张量的合法范围内（越界则置掩码为假，对应 PTX 的谓词化访存，避免越段访问）。

有了这两个函数，再调 `StmtBuilder` 的 generic 构造方法即可生成新指令。整体流程（对每条被处理的指令）：

```
1. 用 super().visit_Instruction(inst) 先重写其 input/output 张量与 attributes
2. 取出全局张量 g，调 sb.tensor_ptr(g) 得到标量指针 Var ptr
3. 用 get_funcs(offsets, dims, g.layout, check_bounds) 构造 (f_offset, f_mask)
4.   LoadGlobal  -> sb.load_global_generic(ptr, f_offset, f_mask) 产出 LoadGlobalGenericInst
    StoreGlobal -> sb.store_global_generic(r, ptr, f_offset, f_mask)
    CopyAsync   -> sb.copy_async_generic(dst, ptr, f_offset, f_mask, evict)
5. 把原 InstStmt（返回的新指令替换它）
```

`get_funcs` 里「切片 → 全局索引 → 线性偏移」的映射是核心几何，下面用伪代码 + 数学说明。设全局张量有 \(D\) 个维度，切片跨 `dims` 这几个维度：

- 切片有 \(|\text{dims}|\) 个轴，记逐元素索引为 \(i_0,\dots,i_{|\text{dims}|-1}\)。
- 全局索引 \(G_d\) 在被切片的维度 \(d=\text{dims}[k]\) 上等于 \(\text{offsets}[d] + i_k\)，其余维度等于 \(\text{offsets}[d]\)。
- 线性偏移 = `layout(G_0, ..., G_{D-1})`，其中 `layout` 是 `GlobalLayout` 这个「多维索引 → 线性偏移」的纯函数（见 u4-l3）。
- 掩码为真当且仅当 \(\forall d:\ 0 \le G_d < \text{shape}_d\)。

数学上，掩码就是合取式：

\[
\text{mask}(i) = \bigwedge_{d} \left(0 \le \text{offsets}[d] + \Delta_d(i) < \text{shape}_d\right)
\]

其中 \(\Delta_d(i)\) 是切片索引到第 \(d\) 维全局索引的增量。不检查越界（`check_bounds=False`）时掩码恒为真。

#### 4.1.3 源码精读

**入口类** [LowerLoadStoreRewriter](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/lower_load_store.py#L31-L101) 只重写三种指令，其余指令由 `IRRewriter` 默认行为原样转发。

**几何翻译器 `get_funcs`**——把切片几何变成两个闭包：

```python
# lower_load_store.py:32-51
@staticmethod
def get_funcs(offsets, dims, layout, check_bounds=True):
    def f_global_indices(indices):
        global_indices = list(offsets)                       # 每维基址
        for i, dim in enumerate(sorted(dims)):
            global_indices[dim] = global_indices[dim] + indices[i]  # 切片维加上逐元素索引
        return global_indices

    def f_offset(indices):
        return layout(*f_global_indices(indices))            # GlobalLayout: 多维索引 -> 线性偏移

    def f_mask(indices):
        if not check_bounds:
            return boolean.true                              # 不校验时掩码恒真
        global_indices = f_global_indices(indices)
        return index_within_bound(global_indices, 0, layout.shape)  # 0 <= idx < shape 逐维合取

    return f_offset, f_mask
```

注意 `sorted(dims)`：切片轴与全局维度的对应按维度编号排序，保证 `indices[i]` 与 `dims` 的顺序无关地映射到正确维度。

**改写 `LoadGlobalInst`**——把张量加载换成指针 + 偏移/掩码：

```python
# lower_load_store.py:53-71
def visit_LoadGlobalInst(self, inst: LoadGlobalInst) -> Stmt:
    inst = super().visit_Instruction(inst)            # 先重写 inst 的张量/属性
    sb = StmtBuilder()
    global_tensor = inst.inputs[0].as_global_tensor()
    register_tensor = inst.register_output
    ptr = sb.tensor_ptr(global_tensor)                # 取标量设备指针 Var
    f_offset, f_mask = self.get_funcs(offsets=inst.offsets, dims=inst.dims, layout=global_tensor.layout)
    self.memo[inst.register_output] = sb.load_global_generic(   # 关键：把产出记进 memo
        dtype=global_tensor.dtype, shape=register_tensor.shape,
        layout=register_tensor.layout, ptr=ptr,
        f_offset=f_offset, f_mask=f_mask)
    return sb.flush_stmts()
```

这里有一个 u5-l1 讲过的关键技巧：`self.memo[inst.register_output] = <新寄存器张量>`。`IRRewriter` 用 `memo` 做「旧 IR 节点 → 新 IR 节点」的替换表。`LoadGlobalGenericInst` 产出一个**新的** `RegisterTensor`（在 [load_global_generic](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/builders/stmt_builder.py#L1298-L1313) 里 `RegisterTensor.create(...)` 新建），所以必须把「旧产出张量 → 新产出张量」登记进 `memo`，下游所有引用旧张量的指令才会被正确改写成引用新张量。`StoreGlobalInst` 不产出张量，故无需这一步。

**改写 `StoreGlobalInst` 与 `CopyAsyncInst`** 结构完全对称，只是输入张量与构造方法不同（[lower_load_store.py:73-101](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/lower_load_store.py#L73-L101)）。`CopyAsyncInst` 多了两点：`dims` 可能为 `None`（表示源/目的同秩、跨全部维），需在本地补成 `range(len(shape))`；并透传 `check_bounds` 与 `evict`（缓存逐出提示）。

**掩码来自哪里**——[index_within_bound](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/hidet/ir/utils/index_transform.py#L130-L144) 把逐维的 \(0 \le \text{idx} < \text{upper}\) 用 `logical_and` 串成一个布尔表达式：

```python
# index_transform.py:141-144
conditions = [logical_and(lower <= idx, idx < upper)
              for lower, idx, upper in zip(lower_bound, indices, upper_bound)]
return logical_and(*conditions)
```

这个布尔表达式就是 PTX 谓词寄存器的来源，emitter 据此决定每个线程是否真正发出访存。

**Pass 外壳**——标准 `Pass` 子类，`process_function` 跑一次 rewriter（[lower_load_store.py:104-111](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/lower_load_store.py#L104-L111)）。

#### 4.1.4 代码实践

**实践目标**：用 `debug.dump_ir` 抓取 `lower_load_store` 前后的 IR，亲眼看到张量级 `load_global`/`store_global` 被替换成 `*GenericInst`。

**操作步骤**：

1. 写一个最小内核（可复用 `examples/vector_add/vector_add.py` 或 `examples/matmul/matmul_v0.py`），在调用内核前加上：
   ```python
   import tilus
   tilus.option.cache_dir("tmp-lower-cache")
   tilus.option.debug.dump_ir()      # 开启逐 Pass 落盘
   ```
2. 运行内核（删除过期的 `tmp-lower-cache` 以强制重编译）。
3. 在缓存目录里找到 Tilus IR 落盘目录（路径形如 `tmp-lower-cache/.../ir/`），其中文件按 `<序号>_<PassName>.txt` 命名（命名规则见 [dump_ir.py:50-63](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/instruments/dump_ir.py#L50-L63)）。按默认流水线顺序，相关文件为：
   - `7_LayoutInference.txt`（lower_load_store **之前**）
   - `8_LowerLoadStore.txt`（lower_load_store **之后**）
4. 对比这两个文件。

**需要观察的现象**：

- `7_LayoutInference.txt` 里还能看到形如 `r1 = load_global(g0, offsets=..., dims=...)` 的张量级指令（具体打印文本以本地为准）。
- `8_LowerLoadStore.txt` 里这些指令消失了，取而代之的是 `LoadGlobalGenericInst`/`StoreGlobalGenericInst`，它们带 `ptr`、`offset`、`mask` 字段，operand 不再是全局张量而是标量指针 Var。

**预期结果**：每条高层访存指令被一一替换为带逐元素偏移/掩码的 generic 指令，且替换前后寄存器张量的引用被正确重连（下游 `dot`/`cast` 等指令仍指向同一逻辑张量）。若使用的是带共享内存的内核（如 `examples/matmul/matmul_v2.py` 起的版本），还会看到 `CopyAsyncInst` 被替换为 `CopyAsyncGenericInst`——这正是「全局→共享内存的搬运指令」被具象化的痕迹。

**注意**：本实践需要 GPU 与可运行环境；若当前机器无支持 GPU，可只做「源码阅读型实践」：对照 4.1.3 的代码，手推一条 `load_global(g, offsets=[m, n], dims=[0, 1])` 经过 `get_funcs` 后，`f_offset` 与 `f_mask` 的表达式形式。具体打印文本「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `visit_LoadGlobalInst` 里必须写 `self.memo[inst.register_output] = ...`，而 `visit_StoreGlobalInst` 不用？

> **答案**：`LoadGlobalGenericInst` 产出一个**新的** `RegisterTensor`，下游指令引用的是旧的产出张量，必须用 `memo` 建立旧→新的映射，`IRRewriter` 才能把下游引用改写过来。`StoreGlobalInst` 不产出张量（`output=None`），只是消费一个寄存器张量写回全局，没有需要重连的产出。

**练习 2**：若一个内核声明 `check_bounds=False`（如 vector_add 要求 `n % block_elems == 0`、不做越界检查），降级后 `mask` 字段会是什么？

> **答案**：`f_mask` 直接返回 `boolean.true`（[lower_load_store.py:46-47](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/lower_load_store.py#L46-L47)），emitter 据此生成不带谓词的访存，省掉每元素的越界判断开销。

---

### 4.2 lower_param_only_expr：化简只依赖参数的表达式

#### 4.2.1 概念说明

后端在两个地方需要「**只依赖函数参数**」的表达式（详见 [lower_param_only_expr.py:26-44](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/lower_param_only_expr.py#L26-L44) 的模块文档）：

1. `AllocateGlobalInst` 产出张量的 `size`/`shape`/`offset`（workspace 大小必须由参数决定，不能含循环变量等运行时量）。
2. `Metadata.grid_blocks`（启动网格的线程块数，若不是常量，也必须只依赖参数）。

但用户写程序时，完全可能先用 `let x = M // block_m` 这样的中间变量来算这些值——可读性好，却让表达式里掺进了非参数变量。`lower_param_only_expr` 就是把这些中间变量**内联展开**，直到表达式里只剩参数（或允许的变量）。

它有一个硬前置：所有「只赋值一次的 `DeclareStmt`」必须先被转成 `LetStmt`——这正是流水线里 [declare_to_let](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/__init__.py#L33) 排在它前面的原因（见模块文档最后一段）。

#### 4.2.2 核心流程

算法很直接（文档原话转述）：

1. 在遍历 `LetStmt` 时，把每个 `bind_var -> bind_value` 的绑定记进字典 `var2expr`。
2. 对每个「应为参数专属」的表达式，反复用 `var2expr` 替换其中的非参数变量，直到：
   - 表达式只剩参数（成功，返回）；或
   - 表达式里还有变量，但它们既不在 `var2expr` 里、也不在允许集合里（失败，抛 `ValueError`——通常是引用了循环变量或多次赋值的变量）。

用数学语言：给定表达式 \(e\)、参数集 \(P\)、绑定映射 \(\sigma\)，不断作用 \(\sigma\) 直到不动点：

\[
e_{n+1} = \sigma(e_n),\quad \text{停止当 } \mathrm{vars}(e_n) \subseteq P \cup A
\]

其中 \(A\) 是额外允许的变量（如 `GlobalLayout.axes`）。若某变量 \(v \in \mathrm{vars}(e_n)\) 既不在 \(P\cup A\) 也不在 \(\sigma\) 的定义域，则报错。

「只赋值一次」这个前置是终止性的保证：`let` 绑定是单赋值的、无环，反复内联必然收敛。

#### 4.2.3 源码精读

**核心方法 [lower_param_only_param](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/lower_param_only_expr.py#L63-L78)** 就是上面的不动点循环：

```python
# lower_param_only_expr.py:63-78
def lower_param_only_param(self, expr, allow_vars=()):
    rewrite_map = self.var2expr
    allow_vars = self.params + allow_vars
    while True:
        used_vars = collect(expr, [Var])                 # 收集表达式里所有 Var
        if all(v in allow_vars for v in used_vars):
            return expr                                   # 只剩参数/允许变量，成功
        else:
            if not any(v in self.var2expr for v in used_vars if v not in allow_vars):
                illegal = [v for v in used_vars if v not in self.var2expr and v not in allow_vars]
                raise ValueError("Used variables {} that is not parameter ...".format(illegal))
            expr = rewrite(expr, rewrite_map)             # 用 let 绑定替换非参数变量
```

`collect(expr, [Var])` 来自 hidet 工具，作用是把表达式里所有 `Var` 节点收集成列表；`rewrite(expr, rewrite_map)` 按 `Var -> Expr` 做变量代换。这里用的是 hidet 版的 `collect`/`rewrite`（而非 Tilus 的 `IRVisitor.collect`），因为要下钻到 Hidet 标量表达式内部——这是 u5-l1 提醒过的陷阱（`IRVisitor.visit_Expr` 不下钻）。

**收集 let 绑定** [visit_LetStmt](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/lower_param_only_expr.py#L94-L102)：进入 `LetStmt` 时把绑定登记进 `var2expr`，再访问 body。注意它**不删 let**（只是借用绑定做内联），body 未变就原样返回。

**两处应用点**：

- [visit_Function](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/lower_param_only_expr.py#L80-L92)：对 `metadata.grid_blocks` 的三个分量分别做内联，用 `with_grid_blocks` 生成新 metadata。
- [visit_AllocateGlobalInst](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/lower_param_only_expr.py#L104-L121)：对全局张量 layout 的 `size`/`shape`/`offset` 做内联（`offset` 额外允许 `global_layout.axes`），重建 layout 与张量，并把旧张量→新张量登记进 `memo`。

注意 `visit_Function` 同时承担「遍历 body」和「处理 metadata」两件事，且都用 `is` 做短路判断（body 与三个 grid 分量都未变就原样返回 func），这是不可变 IR 的标准写法（u5-l1 的 `Pass` 框架会在此基础上再做程序级 `is` 短路）。

#### 4.2.4 代码实践

**实践目标**：观察 `lower_param_only_expr` 如何把 `grid_blocks` 里引用的中间变量内联成纯参数表达式。

**操作步骤**：

1. 同 4.1.4，开启 `dump_ir`、设缓存目录、运行一个 matmul 内核（其 `grid_blocks = (cdiv(M, block_m), cdiv(N, block_n), 1)` 通常含 `let` 中间变量）。
2. 对比 `3_LowerAssume.txt`（本 Pass **之前**）与 `4_LowerParamOnlyExpr.txt`（本 Pass **之后**）。

**需要观察的现象**：在「之前」的文件里，`grid_blocks` 的分量可能引用某个 `let` 变量；在「之后」的文件里，这些变量被展开成只含函数参数（如 `M`、`N`、`block_m`）的表达式。

**预期结果**：`AllocateGlobal`（若有 workspace）与 `grid_blocks` 的表达式都不再含非参数的中间 `let` 变量。是否出现 `let` 变量取决于上游 `declare_to_let`/`let_propagation` 的展开情况，「待本地验证」具体表达式。

#### 4.2.5 小练习与答案

**练习 1**：为什么本 Pass 要求 `declare_to_let` 先跑？如果跳过它会怎样？

> **答案**：本 Pass 靠 `LetStmt` 的 `bind_var -> bind_value` 收集变量到表达式的映射来内联。若中间变量还以 `DeclareStmt`（声明后再赋值）的形式存在，`visit_LetStmt` 根本看不到这些绑定，`var2expr` 为空，遇到非参数变量就会直接抛 `ValueError`。`declare_to_let` 把「只赋值一次的 Declare」转成 `LetStmt`，正是为了让本 Pass 能拿到绑定。

**练习 2**：`visit_AllocateGlobalInst` 处理 `offset` 时多传了 `allow_vars=global_layout.axes`，为什么 `size`/`shape` 不需要？

> **答案**：`GlobalLayout` 的 `offset` 是「axes（符号坐标变量）的线性组合」（见 u4-l3），这些 `axes` 是 layout 自带的合法自由变量，不属于函数参数，但应当被允许出现在 offset 表达式里。`size`/`shape` 是纯数值表达式，没有这种自由变量，所以只允许参数。

---

### 4.3 lower_assume：把 assume 提示落地为整除性

#### 4.3.1 概念说明

用户在 `__call__` 里写 `self.assume(a % c == 0)`（详见 u2-l3），是给编译器一个**单向承诺**：参数 `a` 能被 `c` 整除。它在 IR 里变成 [AssumeInst](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/instructions/hints.py#L40-L46)，条件是一个布尔表达式。

`AssumeInst` 本身不产生任何代码，但它携带的信息对后续优化极其有用——比如标量分析可以据此知道「`a` 能被 16 整除」，从而把 `a // 16 * 16` 化简为 `a`，或证明某循环不会越界。`lower_assume` 的任务就是把这些散落在 IR 树各处的 `AssumeInst`**提取、合并**，写进 `metadata.param2divisibility`（一个 `Var -> int` 的字典），然后让 `AssumeInst` 在 IR 里消失（其 `visit_` 返回 `None`，`InstStmt` 塌缩为空语句）。

#### 4.3.2 核心流程

1. 遍历整棵 IR，对每条 `AssumeInst`：
   - 把它的条件按 `LogicalAnd` 拆成若干合取项（`a % c == 0 and b % d == 0` 拆成两项）。
   - 对每个形如 `a % c == 0`（`a` 是参数 `Var`、`c` 是整常数）的项，记录 `param2divisibility[a] = c`；若同一参数有多个承诺，取最小公倍数 lcm。
   - 任何无法识别的项（不是 `参数 % 常数 == 0` 形式）直接抛 `RuntimeError`。
2. 遍历结束后，把收集到的 `param2divisibility` 与原有 metadata 里的合并（再次取 lcm），用 `with_param2divisibility` 生成新 metadata。

为什么用 lcm？因为「`a` 能被 6 整除」与「`a` 能被 4 整除」合在一起等价于「`a` 能被 \(\mathrm{lcm}(6,4)=12\) 整除」。最小公倍数恰好是合并两个整除承诺的最强正确结论：

\[
c_1 \mid a \;\land\; c_2 \mid a \iff \mathrm{lcm}(c_1, c_2) \mid a
\]

#### 4.3.3 源码精读

**拆解合取项** [visit_AssumeInst](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/lower_assume.py#L29-L63)：用一个栈把 `LogicalAnd` 展平成项列表，再逐项识别：

```python
# lower_assume.py:31-39  把 a0 and a1 and ... 拆成 [a0, a1, ...]
stack = [inst.condition]
terms = []
while stack:
    expr = stack.pop()
    if isinstance(expr, LogicalAnd):
        stack.append(expr.a); stack.append(expr.b)
    else:
        terms.append(expr)
```

**识别整除承诺**（[lower_assume.py:42-63](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/lower_assume.py#L42-L63)）只接受非常严格的形式：`Equal(Mod(Var, Constant), Constant(0))`，即 `a % c == 0`，且 `a` 必须是函数参数。命中则记 lcm，否则 `RuntimeError`：

```python
# lower_assume.py:57-61
divisor = int(term.a.b.value)          # c
if a in self.param2divisibility:
    self.param2divisibility[a] = lcm(self.param2divisibility[a], divisor)  # 多次承诺取 lcm
else:
    self.param2divisibility[a] = divisor
```

注意 `visit_AssumeInst` 返回 `None`——这正是 u5-l1 讲过的「删除指令」标准入口：`IRRewriter.visit_InstStmt` 见到指令访问结果为 `None`，就把该 `InstStmt` 塌缩成空 `SeqStmt`，于是 `AssumeInst` 从 IR 中消失。

**收尾合并** [visit_Function](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/lower_assume.py#L65-L80)：先让父类 `visit_Function` 遍历 body（途中各 `visit_AssumeInst` 把承诺攒进 `self.param2divisibility`），再把结果与原 metadata 的 `param2divisibility` 合并（又一次 lcm）：

```python
# lower_assume.py:74-80
param2divisibility = updated_func.metadata.param2divisibility.copy()
for var in self.param2divisibility:
    if var in param2divisibility:
        param2divisibility[var] = lcm(param2divisibility[var], self.param2divisibility[var])
    else:
        param2divisibility[var] = self.param2divisibility[var]
return updated_func.with_metadata(updated_func.metadata.with_param2divisibility(param2divisibility))
```

两次 lcm 是因为整除承诺可能有两个来源：转译器在 `__call__` 签名分析时已经把部分调优参数的可整除性写进了 metadata（u2-l1/u2-l4 讲过的「调优指纹」），本 Pass 再并入用户显式写的 `assume`。合并后，[Metadata.param2divisibility](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/func.py#L44-L80) 就成了下游 [analyze_scalar](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/__init__.py#L37) 的种子（u5-l3）。

#### 4.3.4 代码实践

**实践目标**：验证 `assume` 提示被合并进 `metadata.param2divisibility` 并从 IR 中消失。

**操作步骤**：

1. 在一个 matmul 内核的 `__call__` 里加一行提示（若没有的话），例如 `self.assume(M % block_m == 0)`。
2. 开启 `dump_ir` 运行。
3. 对比 `2_LetPropogation.txt`（之前）与 `3_LowerAssume.txt`（之后）；并在 `3_LowerAssume.txt` 顶部查看函数的 `param2divisibility` 字段（IRPrinter 会把 metadata 打印在函数签名附近）。

**需要观察的现象**：

- 之前：IR 树里有 `AssumeInst` 语句。
- 之后：`AssumeInst` 消失；`param2divisibility` 字典里出现 `{M: block_m}`（或对应的具体整数）。

**预期结果**：`AssumeInst` 被消除，整除性进入 metadata。具体打印文本「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：若用户写 `self.assume(a % 6 == 0 and a % 4 == 0)`，`param2divisibility[a]` 最终是多少？

> **答案**：`lcm(6, 4) = 12`。两个合取项分别记 6 和 4，对同一参数 `a` 取最小公倍数得 12。

**练习 2**：为什么 `visit_AssumeInst` 对 `self.assume(a > 0)` 这样的提示会抛 `RuntimeError`？

> **答案**：本 Pass 的识别模式只接受 `参数 % 常数 == 0` 这一种合取项（[lower_assume.py:44-63](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/lower_assume.py#L44-L63)）。`a > 0` 是上下界提示而非整除性，不在支持范围内，落入 `else` 分支抛 `RuntimeError`。当前 `assume` 仅用于表达整除性约束（u2-l3）。

---

## 5. 综合实践

把三条 Pass 串起来观察一次完整降级。选 `examples/matmul/matmul_v0.py`（naive matmul，含 `load_global`/`store_global` 且通常带 `assume` 与 `cdiv` 形式的 `grid_blocks`）作为对象。

1. 在调用内核前设置：
   ```python
   import tilus
   tilus.option.cache_dir("tmp-lower")
   tilus.option.debug.dump_ir()
   ```
   删除旧 `tmp-lower` 后运行内核。
2. 进入 `tmp-lower/.../ir/`，依次阅读：
   - `2_LetPropogation.txt` → `3_LowerAssume.txt`：确认 `AssumeInst` 消失、`param2divisibility` 被填充（4.3）。
   - `3_LowerAssume.txt` → `4_LowerParamOnlyExpr.txt`：确认 `grid_blocks` 里的中间变量被内联成纯参数表达式（4.2）。
   - `7_LayoutInference.txt` → `8_LowerLoadStore.txt`：确认 `LoadGlobalInst`/`StoreGlobalInst` 被替换为带 `ptr/offset/mask` 的 `*GenericInst`（4.1）。
3. 回答三个问题：
   - `lower_assume` 产出的 `param2divisibility`，最终被流水线里哪个 Pass 消费？（提示：往后翻到 `5_AnalyzeScalar.txt` 与 `10_BoundAwareSimplify.txt`。）
   - `lower_load_store` 替换出的 `LoadGlobalGenericInst` 的 `offset` 字段是一个什么样的表达式？它依赖哪些变量？
   - 如果把 `lower_load_store` 从流水线里删掉，后端 codegen 会卡在哪一步？为什么？

**预期结果**：你能用一句话说清每条 Pass「吃了什么、吐了什么」，并能指出它们的产物分别被下游哪个 Pass / 后端哪一步使用。具体 IR 文本「待本地验证」。

> 参考：`param2divisibility` 被 `analyze_scalar`（u5-l3）当作种子消费，支撑 `bound_aware_simplify` 的界感知化简；`LoadGlobalGenericInst` 的 `offset` 是「指针基址 + `GlobalLayout` 对全局索引的线性映射」的标量表达式，依赖切片 offsets 与逐元素索引 axes；删掉 `lower_load_store` 后，后端 emitter 找不到 `LoadGlobalInst` 对应的发射器（发射器按指令类注册，见 u6-l2），或即便有也会因为张量语义未展开而无法生成逐线程地址。

## 6. 本讲小结

- **Lowering 的本质是「翻译准备」**：把只有高层才懂的语义（张量、布局、切片、提示）展开成底层看得懂的要素（指针、标量地址、参数表达式、整除性字典），不是优化。
- **`lower_load_store`** 用 `get_funcs` 把 `(offsets, dims, layout)` 翻译成 `f_offset`/`f_mask` 两个闭包，再借助 `StmtBuilder` 的 generic 方法把三类张量级访存指令改写成带「指针 + 逐元素偏移 + 越界掩码」的 `*GenericInst`；产新寄存器张量的指令须登记 `memo` 以重连下游引用。
- **`lower_param_only_expr`** 收集所有 `LetStmt` 绑定，对 `grid_blocks` 与 `AllocateGlobal` 的大小/形状/偏移做不动点内联，使其只依赖函数参数；它强依赖前置的 `declare_to_let`。
- **`lower_assume`** 把 `AssumeInst` 的合取条件解析为 `参数 % 常数 == 0`，用 lcm 合并进 `metadata.param2divisibility`，并让 `AssumeInst`（返回 `None`）从 IR 消失；该字典随后成为标量分析的种子。
- 三条 Pass 位置不同：`lower_assume`/`lower_param_only_expr` 在前段做规范化，`lower_load_store` 在中段被两次 `layout_inference` 夹击——因为它必须先读已推理出的寄存器布局。
- 调试这三条 Pass 的统一手段是 `debug.dump_ir`，文件命名规则为 `<序号>_<PassName>.txt`（`PassName` = 类名去掉 `Pass` 后缀）。

## 7. 下一步学习建议

- **向后看消费方**：本讲的产物会被后续 Pass 消费——读 [scalar_analyze.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/scalar_analyze.py) 看 `param2divisibility` 如何变成 `Analysis.divisibility`（u5-l3 已讲概览，可深入源码），读 [bound_aware_simplify.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/bound_aware_simplify.py) 看它如何用界信息化简。
- **向后端看落地**：`LoadGlobalGenericInst`/`StoreGlobalGenericInst`/`CopyAsyncGenericInst` 最终如何变成 PTX？这是 U6（后端与代码生成）的主题，建议接着学 **u6-l1（generate_ir_module）** 与 **u6-l2（EmitterBase 与发射器注册）**，看 emitter 如何读 `ptr/offset/mask` 生成每线程的标量访存。
- **想动手扩展**：若你想新增一条需要 lowering 的高层指令，本讲的三条 Pass 是最好的模板——都是「重写某类 `InstStmt`、用 `StmtBuilder` 拼出新指令、必要时登记 `memo`」的套路，结合 u8-l5 的自定义 Pass 实践可以快速上手。
