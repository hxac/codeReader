# 循环与控制流原语

## 1. 本讲目标

本讲聚焦 TileLang kernel 里的「循环」和「控制流」怎么写。学完后你应当能够：

1. 在 `@T.prim_func` 里用 `if / 三元 / while / break / continue` 表达条件与跳转，并清楚区分「TIR 运行期条件」与「Python 编译期常量折叠」。
2. 掌握五种循环原语的语法和适用场景：`T.serial`（串行）、`T.unroll`（展开）、`T.Parallel`（元素级并行）、`T.Pipelined`（软件流水）、`T.Persistent`（持久化调度）。
3. 分清哪些循环原语来自上游 TVM、哪些是 tile-lang 自己加的，并能从源码层面找到它们的定义。
4. 理解 `T.Pipelined` 与 `T.Persistent` 的基本形态（细节留到 u3-l6 / u3-l7 展开）。

本讲是 u2-l1（kernel 定义与类型）的延续：u2-l1 解决「函数签名怎么写」，本讲解决「函数体里的循环和分支怎么写」。

## 2. 前置知识

在进入源码前，先建立两个关键心智模型。

**模型一：所有循环都是 TIR 的「For 节点」。**
你在 kernel 里写的 `for i in T.serial(N):` 并不是 Python 的 `for`，而是被 TVM 脚本解析器捕获成 TensorIR 里的一个 `For` 循环节点。不同的 `T.xxx` 只是给这个 `For` 节点打上不同的「循环类型（ForKind）」标注，编译器后续据此决定是「老老实实顺序执行」「展开成多份」还是「改写成向量指令」。换句话说，**循环原语 = 普通 for + 一种调度提示**。

**模型二：条件分两种，命运完全不同。**
TileLang kernel 用标准 Python 的 `if / elif / else` 写分支，但条件表达式有两种来源：

- **TIR 表达式**：如 `i < N`，其中 `i` 是循环变量、`N` 是张量维度。这类条件**保留在生成的 TIR 里**，最终变成设备侧的真实分支指令。
- **Python 常量/布尔**：如 `True`、或闭包里写死的 `BLOCK_TAIL = True`。这类条件在**解析期就被求值并折叠（优化掉）**，根本不会出现在 TIR 里。

这个区别是本讲最重要的概念，后面会反复用到。如果你写 `if True:`，它等价于无条件执行；写 `if i < N:`，它才会生成真正的分支。

> 术语提示：TIR（TensorIR）是 TVM 的中间表示，`@T.prim_func` 的函数体就是一段 TIR；ForKind 是 TIR `For` 节点上标注循环类型的字段（如 `Serial`、`Unrolled`、`Parallel` 等）。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `tilelang/language/tir/ir.py` | 定义 `serial`、`unroll` 等基础循环原语（薄封装 TVM） |
| `tilelang/language/parallel.py` | 定义 `T.Parallel`（元素级并行循环） |
| `tilelang/language/pipeline.py` | 定义 `T.Pipelined`（软件流水循环） |
| `tilelang/language/persistent.py` | 定义 `T.Persistent`（持久化调度循环） |
| `tilelang/language/builtin.py` | 定义 `T.loop_break`（`break` 背后的 TIR intrinsic） |
| `tilelang/language/__init__.py` | 把上述原语汇入 `T.` 命名空间 |
| `docs/TileLang-Ascend Programming Guide.md` | 3.4 节（循环与控制流总览）与 4.1.4 节（调度原语详解） |
| `examples/gemm/example_gemm.py` | 真实的 `T.serial` K 循环累加示例 |
| `examples/gemm/example_gemm_persistent.py` | 真实的 `T.Persistent` + `T.serial` 示例 |
| `examples/quant_batch_matmul/example_quant_matmul.py` | 真实的 `T.Parallel` 元素级示例 |

---

## 4. 核心概念与源码讲解

### 4.1 条件与控制流基础：if / 三元 / while / break / continue

#### 4.1.1 概念说明

这一模块不引入新的 `T.` 循环原语，但要先把和循环「配套使用」的条件与跳转讲清楚，因为后面的 `T.serial` / `T.unroll` 循环体里几乎都会用到它们。核心有三点：

1. **`if / elif / else` 用 Python 语法写，但条件必须是 TIR 表达式**才会变成设备侧分支；Python 常量会被折叠。
2. **三元表达式** `(A if cond else B)` 同样遵循「TIR 条件保留、Python 条件折叠」的规则。
3. **`while` / `break` / `continue`** 是标准 Python 语法，被脚本解析器 lowering 成 TIR 的 while 循环与跳出 intrinsic；其中 `break` 在底层对应 `tl.loop_break` 这个 intrinsic。

#### 4.1.2 核心流程

条件与跳转的处理流程：

```text
你写的 Python if / while / break
        │  TVM 脚本解析器（@T.prim_func 触发）
        ▼
   条件是 Python 常量？ ──是──> 解析期求值并折叠，不进 TIR
        │ 否
        ▼
   作为 TIR If / While 节点保留
        │  break ──> lowering 为 tl.loop_break intrinsic
        ▼
   进入后续 lowering / codegen，变成设备侧分支与跳转
```

要点：解析发生在「JIT 首次调用」那一刻（见 u1-l5），所以 Python 闭包里的常量在解析期就确定了值；而张量维度 `N`、循环变量 `i` 在解析期是符号，只能作为 TIR 表达式保留到运行期。

#### 4.1.3 源码精读

官方文档对条件与跳转的总览在 Programming Guide 3.4 节：

[docs/TileLang-Ascend Programming Guide.md:363-379](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L363-L379) —— 明确说明 `if` 条件应为 TIR 表达式，Python 普通布尔值被视为编译时常量会被折叠，并给出 `if i < N` 与三元 `(A[i] if i < N else 0)` 的写法。

`while` 与 `break / continue` 的支持说明在同节后半段：

[docs/TileLang-Ascend Programming Guide.md:462-479](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L462-L479) —— 指出 `while` 循环条件需为 TIR 表达式，若检测到死循环会编译报错；并说明 `break / continue` 可在 `T.serial / T.unroll / T.Parallel / while` 中使用。

`break` 在底层的 intrinsic 实现如下（两处等价定义，分别服务于不同前端路径）：

```python
# tilelang/language/builtin.py:328-331
def loop_break():
    """Break out of the innermost loop.
    """
    return tir.call_intrin("handle", tir.op.Op.get("tl.loop_break"))
```

[tilelang/language/builtin.py:328-331](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/builtin.py#L328-L331) —— `break` 被实现为名为 `tl.loop_break` 的 TIR intrinsic，语义是「跳出最内层循环」。

[tilelang/language/customize.py:231-233](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/customize.py#L231-L233) —— 另一处等价定义，供 customize 前端路径使用。

> 说明：日常写 kernel 时直接用 Python 的 `break` / `continue` 关键字即可，解析器会自动把它们 lowering 成上面的 `tl.loop_break` 等 intrinsic；`T.loop_break()` 是给需要在表达式中显式触发的进阶场景预留的接口。

#### 4.1.4 代码实践

**实践目标**：亲手验证「Python 常量折叠」与「TIR 条件保留」的差别。

**操作步骤**：

1. 准备一个最小 kernel（示例代码，非项目原有文件），观察 `func.get_kernel_source()` 输出。

```python
# 示例代码
import tilelang
import tilelang.language as T

KEEP_ELSE = False   # Python 闭包常量

@tilelang.jit
def demo(N: int):
    @T.prim_func
    def main(A: T.Tensor((N,), "float16"), B: T.Tensor((N,), "float16")):
        with T.Kernel(1, is_npu=True) as (cid,):
            a_ub = T.alloc_ub((N,), "float16")
            T.copy(A[0], a_ub)
            for i in T.serial(N):
                if KEEP_ELSE:                 # Python 常量 -> 折叠
                    B[i] = a_ub[i]
                if i < N:                     # TIR 条件 -> 保留
                    B[i] = a_ub[i]
    return main
```

2. 调用 `func.get_kernel_source()` 查看生成的设备代码。
3. 把 `KEEP_ELSE` 改成 `True`，再次查看。

**需要观察的现象**：

- `if KEEP_ELSE:` 在 `False` 时整个分支**消失**（折叠），在 `True` 时分支体被**无条件内联**。
- `if i < N:` 始终作为真实分支保留在生成的代码里。

**预期结果**：两处条件的命运不同，证明常量在解析期被吃掉、TIR 条件被保留。**待本地验证**（取决于本机是否具备 CANN 与 NPU 环境，可先用 `get_kernel_source()` 观察代码而不真正运行）。

#### 4.1.5 小练习与答案

**练习 1**：下面两段写法在生成的 TIR 里有何不同？

```python
# (a)
for i in T.serial(N):
    if i + 1 > 0:
        C[i] = A[i]
# (b)
FLAG = True
for i in T.serial(N):
    if FLAG:
        C[i] = A[i]
```

**答案**：(a) 的 `i + 1 > 0` 是 TIR 表达式，会作为真实分支保留；(b) 的 `FLAG` 是 Python 常量 `True`，解析期折叠，`if` 消失、`C[i] = A[i]` 被无条件执行。

**练习 2**：为什么 Programming Guide 说 `while` 条件「需要是 TIR expression」？如果写成 `while True:` 会怎样？

**答案**：因为 `while` 的循环次数必须在运行期可判断，TIR 表达式（如 `i < N`）才能保留到设备侧；`while True` 是 Python 常量，解析器无法判断退出条件，会被识别为潜在死循环而**编译报错**。

---

### 4.2 基础循环：T.serial 与 T.unroll

#### 4.2.1 概念说明

`T.serial` 和 `T.unroll` 是最基础的两个循环原语，二者签名几乎一致、都来自上游 TVM，差别仅在「调度提示」：

- **`T.serial(start, stop)`**：构造普通的顺序 `for` 循环，循环变量在 `[start, stop)` 内逐次递增，**不展开、不并行**。这是写 K 维分块累加、多层嵌套最内层控制时的默认选择。
- **`T.unroll(start, stop)`**：在 `T.serial` 基础上附加「**循环展开**」提示。编译器会把循环体复制多份，省去循环开销、便于指令调度，常用于**循环次数较小且已知**（如一个 tile 内的微内核循环）。

二者都允许只传一个参数：`T.serial(N)` 等价于 `T.serial(0, N)`，即循环变量取 `[0, N)`。

#### 4.2.2 核心流程

```text
for i in T.serial(N):       -> TIR For(kind=Serial)  -> 顺序执行 N 次
for k in T.unroll(K_TILE):  -> TIR For(kind=Unrolled)-> 复制 K_TILE 份循环体
```

展开的本质是「用代码体积换循环开销」：若 `T.unroll(K_TILE)` 里 `K_TILE=8`，则生成代码里会出现 8 份循环体。因此：

- `K_TILE` 太小（如 1~2）：展开收益有限。
- `K_TILE` 太大：代码体积膨胀、可能撑爆指令缓存，反而变慢。

经验上，`T.unroll` 适合「一个 tile 内、几十以内的固定小循环」；而**总迭代次数大、或运行期才确定**的循环（如 K 分块数）应使用 `T.serial` 或 `T.Pipelined`。

#### 4.2.3 源码精读

两个原语都在 `tilelang/language/tir/ir.py` 中定义，是对 TVM 的薄封装：

```python
# tilelang/language/tir/ir.py:12-15
def serial(start: PrimExpr,
           stop: PrimExpr = None,
           *,
           annotations: Dict[str, Any] = None) -> frame.ForFrame:
    ...
    return _ir.serial(start=start, stop=stop, annotations=annotations)
```

[tilelang/language/tir/ir.py:12-34](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/tir/ir.py#L12-L34) —— `serial` 直接转发到 TVM 的 `tvm.script.ir_builder.tir.ir.serial`，返回一个 `ForFrame`（for 上下文帧）。

[tilelang/language/tir/ir.py:87-109](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/tir/ir.py#L87-L109) —— `unroll` 同样转发到 TVM 的 `_ir.unroll`，与 `serial` 仅差一个 ForKind。

它们如何进入 `T.` 命名空间？看 `__init__.py` 的导入顺序：

[tilelang/language/__init__.py:14](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/__init__.py#L14) 与 [tilelang/language/__init__.py:19](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/__init__.py#L19) —— 第 14 行 `from tvm.script.parser.tir import *` 先引入 TVM 版的 `serial/unroll`；第 19 行 `from .tir.ir import *` 再用 tile-lang 自己的版本**覆盖**。所以你写的 `T.serial` 走的是 tile-lang 的薄封装，但最终语义与 TVM 完全一致。

官方语法说明在 Programming Guide 3.4 节：

[docs/TileLang-Ascend Programming Guide.md:382-403](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L382-L403) —— 给出 `T.serial(N)`、`T.serial(0, N)` 与 `T.unroll(K_TILE)` 的写法，并指出 `T.unroll` 是「把展开提示传给 TIR」的高级模式。

真实项目里 `T.serial` 最典型的用法是 GEMM 里沿 K 分块累加：

[examples/gemm/example_gemm.py:42](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm.py#L42) —— `for k in T.serial(loop_k):`，其中 `loop_k = T.ceildiv(K, K_L1)` 是运行期才知道的总块数，因此必须用 `T.serial`（不能 unroll）。

#### 4.2.4 代码实践

**实践目标**：用 `T.serial` 套 `T.unroll` 写一个长度为 `K` 的点积累加循环，并用 `if` 处理非整除的尾块边界。这是本讲的主实践。

**操作步骤**：

1. 新建脚本（示例代码），按下述骨架实现 `C[i] = sum_k A[i,k] * B[i,k]`。外层 `T.serial` 把 `K` 按 `K_TILE` 分块，内层 `T.unroll` 展开每个小块，`if k < K` 兜住尾块。

```python
# 示例代码
import tilelang
import tilelang.language as T
import torch

@tilelang.jit(out_idx=[-1])
def dot(M, K, K_TILE, dtype="float16", accum_dtype="float"):
    @T.prim_func
    def main(
        A: T.Tensor((M, K), dtype),
        B: T.Tensor((M, K), dtype),
        C: T.Tensor((M,), accum_dtype),
    ):
        with T.Kernel(M, is_npu=True) as (cid,):
            a_ub = T.alloc_ub((K,), dtype)
            b_ub = T.alloc_ub((K,), dtype)
            acc_ub = T.alloc_ub((1,), accum_dtype)   # 标量累加器

            with T.Scope("V"):
                T.copy(A[cid, 0], a_ub)
                T.copy(B[cid, 0], b_ub)
                T.tile.fill(acc_ub, 0.0)
                T.barrier_all()

                # 外层串行：按 K_TILE 分块，块数运行期确定 -> 用 T.serial
                for k_blk in T.serial(T.ceildiv(K, K_TILE)):
                    # 内层展开：每块内 K_TILE 次，固定小循环 -> 用 T.unroll
                    for kk in T.unroll(K_TILE):
                        k = k_blk * K_TILE + kk
                        if k < K:                    # TIR 边界：处理非整除尾块
                            acc_ub[0] = acc_ub[0] + a_ub[k] * b_ub[k]

                T.barrier_all()
                T.copy(acc_ub, C[cid])
    return main


func = dot(M=64, K=100, K_TILE=8)          # K=100 不能被 K_TILE=8 整除，会触发尾块 if
print(func.get_kernel_source())            # 观察生成的设备代码
```

2. 调用 `func.get_kernel_source()`，定位内层循环是否被展开成 8 份，并确认 `if (k < K)` 作为真实分支保留。
3. （有 NPU 环境时）构造 `a, b = torch.randn(...).half().npu()`，运行 `c = func(a, b)`，与 `ref = (a * b).sum(dim=-1)` 对比。

**需要观察的现象**：

- `T.unroll(K_TILE=8)` 让内层循环体在生成代码里出现约 8 份拷贝。
- `if k < K:` 在 `K=100, K_TILE=8` 时，最后一块（`k_blk=12`）里 `k` 会取到 96..103，其中 100..103 被 `if` 屏蔽，保证只累加有效元素。
- 输出与 torch 参考值在容差内一致。

**预期结果**：`Kernel Output Match!`。**待本地验证**（精确数值需在具备 CANN/NPU 的机器上运行；无硬件时可先做 `get_kernel_source()` 静态验证）。注意：该骨架基于项目真实原语（`alloc_ub`/`T.copy`/`T.tile.fill`/`T.barrier_all`/`T.Scope("V")`，见 [examples/elementwise/elementwise_add.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/elementwise/elementwise_add.py)）拼装，具体能否直接编译以本机环境为准。

#### 4.2.5 小练习与答案

**练习 1**：把上面内层循环从 `T.unroll(K_TILE)` 换成 `T.serial(K_TILE)`，`get_kernel_source()` 会有什么变化？

**答案**：循环体不再被复制展开，而是变成一个带循环计数器的真实循环；代码体积变小，但少了展开带来的指令调度空间。

**练习 2**：为什么 GEMM 的 K 分块循环（`for k in T.serial(loop_k)`）不能直接用 `T.unroll`？

**答案**：`loop_k = T.ceildiv(K, K_L1)` 是运行期才确定的符号，且通常较大；`T.unroll` 针对的是「小而固定」的循环，对未知大循环展开既不可行也会导致代码爆炸。

---

### 4.3 元素级并行：T.Parallel

#### 4.3.1 概念说明

`T.Parallel(ext0, ext1, ...)` 用来表达「**Tile 内的元素级并行计算**」。它构造一个（可嵌套的）并行循环，循环体里通常是一条「对 buffer 元素的赋值/运算」语句，编译器会把它映射到 Ascend Vector 核的向量指令。它是写 element-wise 算子（如 exp、add、softmax 的逐元素步）的主力原语。

和 `T.serial` 的本质区别：`T.serial` 是「顺序、标量语义」的循环；`T.Parallel` 是「数据并行、向量语义」的循环——它的迭代之间**无依赖、可并行**，因此编译器可放心地向量化。

#### 4.3.2 核心流程

```text
for (i, j) in T.Parallel(block_M, block_N):
    c_ub[i, j] = T.exp(a_ub[i, j])      # 一条元素级语句
        │  AscendLowerParallelToVector 等 pass
        ▼
   映射为 Vector 核的向量指令（一条指令处理多个元素）
```

`T.Parallel` 支持一维和二维（甚至多维）形式，并原生支持：

- **广播**：右侧操作数维度小于左侧时自动广播（如 `c_ub[i,j] = b_ub[j] + 5`）。
- **行/列切分**：可与外层 `range`（顺序行）配合，实现「行顺序、列并行」。
- **临时缓冲**：复杂表达式会被拆成带临时 buffer 的多条语句。

#### 4.3.3 源码精读

`T.Parallel` 是 tile-lang 自己定义的原语（不像 serial/unroll 来自 TVM）：

```python
# tilelang/language/parallel.py:10-30
def Parallel(*extents: tir.PrimExpr, coalesced_width: Optional[int] = None):
    """Tools to construct nested parallel for loop.
       This can be used to create element-wise tensor expression."""
    annotations: Dict[str, Any] = {}
    if coalesced_width is not None:
        annotations.update({"coalesced_width": coalesced_width})
    return _ffi_api.Parallel(extents, annotations)
```

[tilelang/language/parallel.py:10-30](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/parallel.py#L10-L30) —— 接收若干 `extents` 作为各维规模，可选 `coalesced_width` 控制合并访存宽度，最终调用 C++ 侧 `_ffi_api.Parallel` 构造并行 `ForFrame`。

[tilelang/language/__init__.py:30](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/__init__.py#L30) —— `from .parallel import Parallel`，把 `T.Parallel` 注入命名空间。

官方对 `T.Parallel` 的详细说明在 Programming Guide 4.1.4.1：

[docs/TileLang-Ascend Programming Guide.md:405-428](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L405-L428) —— 3.4 节里对 `T.Parallel` 的速览（一维/二维运算、GM→UB 拷贝场景）。

[docs/TileLang-Ascend Programming Guide.md:903-1031](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L903-L1031) —— 4.1.4.1 完整说明：语法、行/列切分、广播、维度不匹配处理，以及「符号 API（`T.exp`/`+`/`*`）」与「显式 `T.tile.xxx`」两种等价范式。

真实项目中的二维 `T.Parallel` 用法：

[examples/quant_batch_matmul/example_quant_matmul.py:80-81](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/quant_batch_matmul/example_quant_matmul.py#L80-L81) —— `for bm_v, bn_v in T.Parallel(block_M_2, block_N):`，典型的二维元素级并行。

> 关键结论（详见 u3-l5）：在 Ascend 上，`T.Parallel` 会被 `AscendLowerParallelToVector` pass 改写为 Vector 核向量指令；它和 `T.tile.*`（显式向量范式）二者等价、可混用。

#### 4.3.4 代码实践

**实践目标**：体会 `T.Parallel` 的广播与维度不匹配处理。

**操作步骤**：

1. 阅读下面来自 Programming Guide 的写法（项目官方示例）：

```python
# 来自 docs/TileLang-Ascend Programming Guide.md 4.1.4.1
for (i, j) in T.Parallel(block_M // VEC_NUM, block_N):
    c_ub[i, j] = b_ub[j] + 5      # b_ub 是 1 维，c_ub 是 2 维 -> 自动广播
```

2. 在本机把它改写成一个完整可运行的 element-wise kernel：`C = exp(A) + 5`，A、C 形状 `(M, N)`，block 切分参考 [examples/elementwise/elementwise_add.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/elementwise/elementwise_add.py)。
3. 分别尝试：(a) 用 `T.Parallel` + 符号 API `T.exp(...)`；(b) 用显式 `T.tile.exp(...)`。

**需要观察的现象**：两种写法生成的设备代码在向量指令层面等价；广播让 `b_ub[j]`（或常数 `5`）自动作用到整行。

**预期结果**：两种范式输出一致。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`T.Parallel` 与 `T.serial` 在 ForKind 上的根本区别是什么？

**答案**：`T.serial` 是 Serial 顺序循环（标量语义、迭代有先后）；`T.Parallel` 是 Parallel 数据并行循环（向量语义、迭代无依赖可并行），后者会被 lower 成向量指令。

**练习 2**：下面写法是否合法？为什么？

```python
for (i, j) in T.Parallel(block_M, block_N):
    c_ub[i, j] = c_ub[i, j-1] + 1     # 依赖相邻元素
```

**答案**：不推荐/不合法。`T.Parallel` 假定迭代间无数据依赖，而 `c_ub[i, j-1]` 引入了迭代间依赖，违背并行语义；这种递推应改用 `T.serial`。

---

### 4.4 软件流水：T.Pipelined（基本形态）

#### 4.4.1 概念说明

`T.Pipelined(iters, num_stages=...)` 在 `T.serial` 的基础上叠加「**软件流水**」：把循环里的「搬运」和「计算」重叠起来执行，用访存时间掩盖计算时间（反之亦然）。它是性能优化最常用的循环原语之一。

关键参数 `num_stages`：表示生产者与消费者之间**最多缓冲的份数**，控制重叠度。`num_stages=0` 表示不启用流水。本讲只讲基本形态，详细的三段式（prefetch / main / tail）调度与 pass 实现留到 u3-l6。

#### 4.4.2 核心流程

以「每个循环迭代 = 搬 A + 搬 B + 算 gemm」为例，`num_stages=2` 时：

```text
无流水：  copyA0 copyB0 gemm0 | copyA1 copyB1 gemm1 | ...
num_stages=2（预取 2 份）：
  t0: copyA0 copyB0
  t1: copyA1 copyB1
  t2: copyA2 copyB2  gemm0      <- 计算与搬运开始重叠
  t3: copyA3 copyB3  gemm1
  t4:                   gemm2
  t5:                   gemm3
```

即「先预取 `num_stages` 份数据，再进入主体（一边搬下一份一边算上一份），最后排空尾部」。重叠使得内存搬运的开销被计算掩盖。

#### 4.4.3 源码精读

`T.Pipelined` 同样是 tile-lang 自定义原语：

```python
# tilelang/language/pipeline.py:11-20
def Pipelined(
    start: tir.PrimExpr,
    stop: tir.PrimExpr = None,
    num_stages: int = 0,
    order: list[int] | None = None,
    stage: list[int] | None = None,
    sync: list[list[int]] | None = None,
    group: list[list[int]] | None = None,
    cross_interval: int = 1,
):
```

[tilelang/language/pipeline.py:11-52](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/pipeline.py#L11-L52) —— 支持 `(iters)` 或 `(start, stop)` 两种范围写法；`num_stages=0` 时不启用流水；`cross_interval` 与跨核同步相关（见 u5-l2）；`order/stage/sync/group` 是进阶的手工流水编排参数（本讲不展开）。当只传一个参数时，`stop=start; start=0`。

[tilelang/language/__init__.py:31](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/__init__.py#L31) —— 注入 `T.Pipelined`。

官方说明在 Programming Guide 3.4 与 4.1.4.2：

[docs/TileLang-Ascend Programming Guide.md:430-450](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L430-L450) —— 给出 `T.Pipelined(T.ceildiv(K, BK), num_stages=3)` 的典型形态与三段式时序示意。

[docs/TileLang-Ascend Programming Guide.md:1068-1175](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L1068-L1175) —— 4.1.4.2 完整说明，包含 intra-core（单核内搬运/计算重叠）与 inter-core（Cube↔Vector 跨核流水）两种场景，以及重要约束：**核间流水与核内流水不能同时开启**；核间流水必须配合自动 CV 分离与跨核同步（见 u5-l1/u5-l2）。

> 提示：`T.Pipelined` 的实际 lowering 由 `InjectSoftwarePipeline` / `PipelinePlanning` 等 pass 完成，细节见 u3-l6。

#### 4.4.4 代码实践

**实践目标**：把一个 K 循环从 `T.serial` 升级为 `T.Pipelined`，直观感受「重叠」。

**操作步骤**：

1. 打开 [examples/gemm/example_gemm.py:42](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm.py#L42)，确认当前的 `for k in T.serial(loop_k):` 写法。
2. 把它改为 `for k in T.Pipelined(loop_k, num_stages=2):`（示例修改）。
3. 调用 `func.get_kernel_source()` 对比前后生成的代码：流水版会出现「预取-主体-排空」的结构（如多份 A_L1/B_L1 缓冲、循环外的 prologue/epilogue）。

**需要观察的现象**：流水版生成的代码体积变大（多份缓冲与排空代码），但搬运与计算的依赖被解开。

**预期结果**：功能不变（仍输出 `Kernel Output Match!`），代码结构改变。**待本地验证**；性能变化需用 msprof 采集（见 u7-l4）。

#### 4.4.5 小练习与答案

**练习 1**：`num_stages=0` 与 `num_stages=2` 的区别是什么？

**答案**：`num_stages=0` 不启用流水，等价于普通顺序循环；`num_stages=2` 预取 2 份数据，让第 k 次计算与第 k+2 次搬运重叠。

**练习 2**：为什么 Programming Guide 强调「核间流水与核内流水不能同时开启」？

**答案**：两者都依赖对同一批缓冲与同步的精细排布，同时开启会产生资源/同步冲突，编译器无法保证正确性，因此规定只能二选一（详见 u5-l2）。

---

### 4.5 持久化调度：T.Persistent（基本形态）

#### 4.5.1 概念说明

`T.Persistent(domain, wave_size, index, group_size=8)` 解决的是「**数据块在多个 AI Core 间怎么调度**」的问题。默认情况下，一组数据块会被「轮询」分给各核；而 `T.Persistent` 让「**相邻的一组数据块归同一个核处理**」，从而让该核加载的数据更容易命中 L2 cache，减少反复换入换出。它是一种对 cache 更友好的调度策略。基本形态如下，调度细节留到 u3-l7。

#### 4.5.2 核心流程

```text
普通 T.Kernel:  block0->core0, block1->core1, block2->core0, ...  (轮询，跳着访问)
T.Persistent :  block0,block1,...->core0;  block_k,block_k+1,...->core1; ...  (相邻成组)
```

参数含义：

- `domain`：各维度的 tile 总数列表，如 `[ceildiv(M, block_M), ceildiv(N, block_N)]`。
- `wave_size`：一波里参与的核数（常等于 `core_num`）。
- `index`：当前核的 id（通常传入 `T.Kernel` 给的 `cid`）。
- `group_size`：每个核一次「领」多少个相邻 tile（默认 8）。

#### 4.5.3 源码精读

```python
# tilelang/language/persistent.py:10-29
def Persistent(
    domain: list[tir.PrimExpr],
    wave_size: tir.PrimExpr,
    index: tir.PrimExpr,
    group_size: tir.PrimExpr | None = 8,
):
    return _ffi_api.Persistent(domain, wave_size, index, group_size)
```

[tilelang/language/persistent.py:10-29](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/persistent.py#L10-L29) —— 接收 `domain / wave_size / index / group_size`，构造持久化调度的 `ForFrame`。

[tilelang/language/__init__.py:32](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/__init__.py#L32) —— 注入 `T.Persistent`。

官方说明在 Programming Guide 3.4 与 4.1.4.3：

[docs/TileLang-Ascend Programming Guide.md:452-460](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L452-L460) —— 3.4 节速览。

[docs/TileLang-Ascend Programming Guide.md:1177-1200](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L1177-L1200) —— 4.1.4.3 说明：分批调度让相邻块归同核、提升 cache 命中；随机调度则导致 cache 反复换入换出、浪费带宽。

真实项目用法（`T.Persistent` 套 `T.serial`）：

[examples/gemm/example_gemm_persistent.py:30-42](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_persistent.py#L30-L42) —— `for bx, by in T.Persistent([ceildiv(M, block_M), ceildiv(N, block_N)], core_num, cid):` 外层做持久化调度，内层 `for k in T.serial(loop_k):` 做 K 分块累加。这正是「`T.Persistent` + `T.serial`」的标准组合。

[testing/python/language/test_tilelang_ascend_language_l1_to_l0.py:587-595](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/testing/python/language/test_tilelang_ascend_language_l1_to_l0.py#L587-L595) —— 测试用例中的等价写法，注释明确指出 `T.Persistent` 以 cache 友好的顺序驱动 tile grid。

#### 4.5.4 代码实践

**实践目标**：对比「普通 `T.Kernel`」与「`T.Persistent`」两种调度在同一 GEMM 下的结构差异。

**操作步骤**：

1. 打开 [examples/gemm/example_gemm.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm.py)（普通版，用 `cid` 解码出 `bx, by`）。
2. 打开 [examples/gemm/example_gemm_persistent.py:30-42](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_persistent.py#L30-L42)（持久化版，`bx, by` 由 `T.Persistent` 直接给出）。
3. 画出两者「tile → core」的分配示意，标注相邻 tile 是否归同一核。

**需要观察的现象**：普通版用 `bx = cid // n_num; by = cid % n_num`，tile 按行优先轮询分配；持久化版由 `T.Persistent` 保证相邻 tile 成组归属。

**预期结果**：两份代码计算结果一致，但调度顺序不同。**待本地验证**（结构差异可通过阅读源码直接确认，无需硬件）。

#### 4.5.5 小练习与答案

**练习 1**：`T.Persistent` 的 `index` 参数通常传什么？为什么？

**答案**：通常传 `T.Kernel` 上下文给的 `cid`（核 id），因为每个核需要根据自己的 id 决定「我该处理哪一组相邻 tile」。

**练习 2**：`T.Persistent` 优化的是访存的哪个层级？

**答案**：主要优化 **L2 cache 命中率**——通过让相邻 tile 归同一核，减少大块数据在 L2 的反复换入换出。它不改变单核内的 L1/UB 访问模式。

---

## 5. 综合实践

把本讲五种循环原语串起来，做一次「循环类型识别与改写」训练：

1. 以 [examples/gemm/example_gemm_persistent.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_persistent.py) 为对象，逐层标注每个循环用了哪种原语，并说明理由：
   - 外层 `for bx, by in T.Persistent(...)` —— 为什么用持久化而不是普通 `T.Kernel`？
   - 内层 `for k in T.serial(loop_k)` —— 为什么用 serial 而不是 unroll？
2. 做三组改写并（用 `get_kernel_source()`）对比生成代码（示例任务）：
   - (a) 把内层 K 循环改为 `T.unroll(loop_k)`：观察代码爆炸，理解「unroll 只适合小固定循环」。
   - (b) 把内层 K 循环改为 `T.Pipelined(loop_k, num_stages=2)`：观察 prologue/epilogue 出现。
   - (c) 在 K 循环体内加一个 `if k == 0: ... else: ...` 的 TIR 条件（如 `init=(k==0)`，参考 [example_gemm_persistent.py:38](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_persistent.py#L38)），确认它是运行期分支而非被折叠。
3. （有硬件时）用 msprof 对比 (a)(b) 的性能，体会「unroll 爆炸 vs pipelined 加速」。**待本地验证**。

这个任务覆盖了 `T.Persistent`（调度）、`T.serial`（顺序大循环）、`T.unroll`（小固定循环）、`T.Pipelined`（流水）、以及 `if` TIR 条件，把本讲全部内容串成一条线。

## 6. 本讲小结

- 所有循环原语本质都是「TIR `For` 节点 + 一种 ForKind 调度提示」，理解这一点就能举一反三。
- **条件分两种**：Python 常量在解析期被折叠（不进 TIR），TIR 表达式（如 `i < N`）作为真实分支保留到运行期——这是写 kernel 最易踩坑的点。
- `T.serial` / `T.unroll` 来自 TVM（薄封装），分别用于「运行期大循环」和「小固定循环展开」。
- `T.Parallel` / `T.Pipelined` / `T.Persistent` 是 tile-lang 自定义原语，分别解决「元素级并行」「软件流水」「cache 友好调度」。
- `while` / `break` / `continue` 用标准 Python 语法，被 lowering 成 TIR；`break` 底层是 `tl.loop_break` intrinsic。
- `T.Pipelined` 与 `T.Persistent` 的细节（三段式调度、pass 实现、跨核流水）将在 u3-l6 / u3-l7 / u5-l2 展开。

## 7. 下一步学习建议

- 掌握了循环与控制流后，下一单元（u3）进入 **Developer 模式核心原语**：建议先读 u3-l1（内存分配）与 u3-l2（`T.copy` 数据搬运），因为本讲的循环只有配合 `alloc_*` 与 `T.copy` 才能构成真实算子。
- 想深入 `T.Pipelined` 的三段式与 pass 实现，直接跳读 u3-l6（`InjectSoftwarePipeline` / `PipelinePlanning`）。
- 想深入 `T.Persistent` 的调度策略，跳读 u3-l7。
- 想了解 `T.Parallel` 如何被改写成向量指令，跳读 u3-l5（`AscendLowerParallelToVector`）。
- 继续阅读源码：把 [tilelang/language/__init__.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/__init__.py) 的导入逐行过一遍，确认 `T.` 命名空间里每个原语的来源（TVM vs tile-lang 自定义）。
