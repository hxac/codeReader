# Element-wise 与 T.Parallel

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `T.Parallel` 在 IR 层到底是什么、它解决了「Tile 内逐元素计算如何映射到 Ascend Vector 向量指令」这个问题。
- 用 `T.Parallel` 的一维 / 二维写法，结合符号 API（`+`、`*`、`T.exp`、`T.max` 等）写出一段逐元素 kernel。
- 理解 `AscendLowerParallelToVector` 这个 pass 是如何把「并行循环 + 符号表达式」递归拆解、翻译成一条条 Ascend 向量指令的，包括临时缓冲、广播、标量运算三条分支。
- 掌握另一种等价写法——`T.tile.*`（`T.tile.add`/`T.tile.exp` 等）显式向量范式，并理解它与 `T.Parallel + 符号 API` 的关系。
- 知道为什么复杂表达式会膨胀出多个临时 UB 缓冲，以及为什么要开启自动缓冲复用（`TL_ASCEND_MEMORY_PLANNING`）。

## 2. 前置知识

本讲建立在已经学完 u3-l1（内存层级与分配）、u3-l2（数据搬运 `T.copy`）、u3-l4（Reduce 原语）的基础上。开始前，请先回忆以下几点：

- **Ascend 的存储层级**：GM（全局显存）→ L1（Cube 核缓存）→ UB / Unified Buffer（Vector 核缓存）→ L0A/L0B/L0C（寄存器级）。逐元素计算发生在 **Vector 核**，数据必须先搬到 **UB**。
- **什么是 Vector 指令**：Ascend 的 Vector 单元一次能处理「一整块」连续元素（例如一次性对 256 个 fp16 做加法），而不是一个一个标量算。这类似 GPU 上的 SIMT，但粒度更粗，是一条「向量指令」。
- **`T.Scope("V")`**：把一段计算显式划到 Vector 执行域（详见 u4-l1）。本讲的逐元素代码都应落在 `T.Scope("V")` 或默认 Vector 上下文里。
- **符号表达式 vs 显式指令**：你可以写 `c = a + b`（像 NumPy 一样），也可以写 `T.tile.add(c, a, b)`（像调用一条硬件指令）。本讲的核心就是这两种写法的等价性与各自背后的编译流程。

一个直觉性的类比：`T.Parallel` 之于 Ascend，就像「`for` 循环 + 整块向量化」之于 CPU。你写的是「对这块 Tile 的每个元素独立做同一件事」，编译器负责把这句话翻成「一条吃掉整块数据的向量指令」。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [tilelang/language/parallel.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/parallel.py) | `T.Parallel` 的 Python 前端定义，非常薄，只是构造一个 `ForKind::kParallel` 的循环帧。 |
| [tilelang/language/__init__.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/__init__.py) | 把 `Parallel` 暴露成 `T.Parallel`，把 `ascend_tile` 模块暴露成 `T.tile`。 |
| [src/transform/ascend_lower_parallel_to_vector.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_lower_parallel_to_vector.cc) | **核心 pass**：把并行循环 + 符号表达式翻译成 Ascend 向量指令 / copy / broadcast 调用。 |
| [tilelang/language/ascend_tile.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend_tile.py) | `T.tile.*` 显式向量范式（`add`/`sub`/`mul`/`div`/`exp`/`max` …）的前端实现，直接发射 `tl.ascend_*` intrinsic。 |
| [tilelang/engine/phase.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py) | 编译流水线，标注 `AscendLowerParallelToVector` 落在 `LowerAndLegalize` 阶段，缓冲复用落在 `OptimizeForTarget` 阶段。 |
| [examples/elementwise/elementwise_add.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/elementwise/elementwise_add.py) | 最小的逐元素示例（向量加），用 `T.tile.add` 写法。 |
| [examples/softmax/example_online_softmax.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/softmax/example_online_softmax.py) | online softmax 真实算子，密集使用 `T.tile.*` 与 `T.reduce_*`，是本讲综合实践的参照。 |
| [docs/TileLang-Ascend Programming Guide.md](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md) | 官方编程指南第 4.1.3.3 / 4.1.4.1 节，逐元素算符表与 `T.Parallel` 语法说明。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **`T.Parallel`**：前端语义——Tile 内元素级并行循环。
2. **`AscendLowerParallelToVector` pass**：把上述循环翻译成向量指令的编译流程。
3. **`T.tile.*`（ascend_tile）**：与之等价的显式向量范式，以及广播、临时缓冲与缓冲复用。

### 4.1 T.Parallel：Tile 内元素级并行的 IR 抽象

#### 4.1.1 概念说明

一段 Ascend Vector kernel 的典型流程是：

1. 把大张量切成若干 **Tile**；
2. 把每个 Tile 从 GM 搬到片上 **UB**；
3. 在 UB 上做 **Load → Compute → Store**；
4. 其中 **Compute** 阶段，用向量指令一次处理 Tile 内的所有元素。

`T.Parallel` 就是用来写第 4 步「Compute」的原语。它在 IR 层是一个 `ForKind::kParallel` 的循环，语义是「循环体内每一次迭代彼此独立、可以并行」。它**隐藏了硬件细节**：你不用关心 Vector 指令一次吃多少元素、不用手写 repeat/mask，只需声明「对这个 Tile 的每个元素做同一件事」。

官方编程指南对此的定位是（[Programming Guide:903-916](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L903-L916)）：

> `T.Parallel` 是用于表达 Tile 内元素级并行计算的基本原语。在 IR 层面，它抽象出表示数据并行语义的并行循环，同时隐藏硬件细节，从而简化内核开发并提高其可移植性。

`T.Parallel` 设计上有两个目标（[Programming Guide:920-934](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L920-L934)）：

- **与主线 IR 对齐、后端可移植**：鼓励在循环体里用符号数学 API（`T.exp`、`T.max`、`+`、`*`），而不是直接调低层向量操作，这样同一份代码也能跑在 CPU/GPU 上。
- **与 Ascend 向量能力协同**：同时提供 `T.tile.xxx` 形式的向量原语，用户可在「符号 API」和「显式向量内联函数」之间灵活选择。

#### 4.1.2 核心流程

`T.Parallel` 的写法非常朴素，就是一个 `for` 循环：

```text
# 1D：对一个一维 Tile 做逐元素操作
for j in T.Parallel(block_N):
    c_ub[j] = a_ub[j] + b_ub[j]

# 2D：对一个二维 Tile 做逐元素操作
for (i, j) in T.Parallel(block_M // VEC_NUM, block_N):
    c_ub[i, j] = a_ub[i, j] + b_ub[i, j]
```

它在解析期被翻译成一个 TIR `For` 节点，关键属性是 `kind = kParallel`。后续的 `AscendLowerParallelToVector` pass 会识别这个属性，把整个循环折叠成「一条吃掉整块 UB 数据的向量指令」。也就是说：

\[ \text{for } j \in \text{Parallel}(N):\ c[j]=a[j]+b[j] \quad\Longrightarrow\quad \text{AscendC::Add}(c, a, b, N) \]

支持 1D 和 2D 两种形态，但**不支持 3D 及以上**的 `T.Parallel` 嵌套（pass 会直接报错，见 4.2.3）。

#### 4.1.3 源码精读

前端的 `Parallel` 定义极其简短——它只负责构造一个带 `kParallel` 语义的循环帧，几乎不含 Ascend 专用逻辑（[parallel.py:10-31](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/parallel.py#L10-L31)）：

```python
def Parallel(*extents: tir.PrimExpr, coalesced_width: Optional[int] = None):
    """Tools to construct nested parallel for loop."""
    annotations: Dict[str, Any] = {}
    if coalesced_width is not None:
        annotations.update({"coalesced_width": coalesced_width})
    return _ffi_api.Parallel(extents, annotations)
```

它的可变参数 `*extents` 就是各维循环长度，`len(extents)` 决定了循环嵌套层数（1 个→1D，2 个→2D）。真正的「并行语义如何变成向量指令」全部后置到 C++ pass，前端不关心硬件。

`T.Parallel` 与 `T.tile` 的暴露都集中在 `__init__.py`（[__init__.py:30](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/__init__.py#L30) 与 [__init__.py:86](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/__init__.py#L86)）：

```python
from .parallel import Parallel          # → T.Parallel
...
from . import ascend_tile as tile       # → T.tile.add / T.tile.exp / ...
```

注意：循环体里出现的 `+`、`*`、`T.exp`、`T.max` 等符号 API，其实是 TVM 的 TIR 表达式（例如 `T.exp` 会生成一个 `tir.exp(...)` Call 节点）。它们之所以最后能变成 Ascend 指令，是因为 4.2 节的 pass 里有一张「TIR 算子 → Ascend 算子」的映射表。这是理解「两种写法等价」的关键。

#### 4.1.4 代码实践

**实践目标**：确认 `T.Parallel` 在前端只是一个并行循环，符号表达式是普通 TIR 表达式。

**操作步骤**：

1. 打开 [examples/elementwise/elementwise_add.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/elementwise/elementwise_add.py)，定位 `T.Scope("V")` 段（[elementwise_add.py:38-46](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/elementwise/elementwise_add.py#L38-L46)），目前它用的是 `T.tile.add` 显式写法：
   ```python
   with T.Scope("V"):
       T.copy(A[bx * block_M + vid * block_M // VEC_NUM, by * block_N], a_ub)
       T.copy(B[bx * block_M + vid * block_M // VEC_NUM, by * block_N], b_ub)
       T.barrier_all()
       T.tile.add(c_ub, a_ub, b_ub)      # ← 显式向量范式
       T.barrier_all()
       T.copy(c_ub, C[bx * block_M + vid * block_M // VEC_NUM, by * block_N])
   ```
2. 把 `T.tile.add(c_ub, a_ub, b_ub)` 替换成等价的符号 + `T.Parallel` 写法（**示例代码，便于对照**）：
   ```python
   for (i, j) in T.Parallel(block_M // VEC_NUM, block_N):
       c_ub[i, j] = a_ub[i, j] + b_ub[i, j]
   ```
3. 运行 `python examples/elementwise/elementwise_add.py`。

**需要观察的现象**：终端仍输出 `Kernel Output Match!`。

**预期结果**：两种写法最终生成的 Ascend C 代码里，核心都是同一条 `AscendC::Add`（或等价向量加）指令。可用 `func.get_kernel_source()`（见 u1-l5）对照两份生成代码的向量指令是否一致。若本地无 NPU 环境，则标记「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `T.Parallel` 改成普通 `for i in range(...)`（即 `T.serial`），程序还能跑通吗？生成的指令会有什么不同？

> **答案**：能跑通（语义正确），但 pass 无法把它识别为「整块向量化」，会退化为「逐元素标量循环」，指令数量暴增、性能急剧下降。`T.Parallel` 是向量化得以发生的语义触发器。

**练习 2**：`T.Parallel(block_M, block_N)` 接收两个 extent，这两个数字分别对应什么？为什么示例里第一维常常写成 `block_M // VEC_NUM`？

> **答案**：两个 extent 分别是 Tile 的行数和列数。写成 `block_M // VEC_NUM` 是因为默认 `threads=None` 时一个 Tile 由 2 个 Vector 子核（`VEC_NUM=2`）分担，每个子核只处理一半的行（详见 u2-l2 的 vid 切分）。

---

### 4.2 AscendLowerParallelToVector：把并行循环翻译成向量指令

#### 4.2.1 概念说明

`T.Parallel` 本身只是「声明这里可以并行」，真正把它落到 Ascend 硬件的是一个编译 pass：`AscendLowerParallelToVector`（注册名 `tl.AscendLowerParallelToVector`）。它的任务是：

- 找到每一个 `kParallel` 循环；
- 分析循环体里的 `BufferStore`（即 `c_ub[i,j] = <表达式>`）；
- 把 `<表达式>` 递归拆解成一串 Ascend 向量指令（加/减/乘/除/min/max/exp/...）、UB→UB/UB→GM 的 copy、以及必要的广播；
- 对无法向量化的循环，安全地退化为普通串行循环（`kSerial`）。

这个 pass 是「符号 API」路线能工作的全部秘密。理解了它，你就能预测一段 `T.Parallel` 代码会生成多少条向量指令、会不会引入临时缓冲。

#### 4.2.2 核心流程

pass 的大致处理流程（伪代码）：

```text
VisitStmt(For kind=kParallel):
    if 是 1D Parallel 且 body 是 BufferStore/Seq:
        尝试 TryVectorizeStoreSeq → 成功则替换为一串向量指令
    elif 是 2D Parallel 嵌套(Parallel→Parallel→store):
        检查是否支持 2D 向量化 → 成功则折叠为 2D 向量指令
    elif 是 3D Parallel:
        LOG(FATAL) 直接报错
    else:  # 无法向量化
        把 kParallel 降级为 kSerial，避免 codegen 崩溃

TryVectorizeStoreSeq(store, element_count):
    DetectVectorPlan(store)          # 判定 1D/2D、inner_vec_len、outer_extent
    对每个 store:
        VectorizeStoreAsRowBody:
            DecomposeExpression(value):
                - 若 value 是单 BufferLoad      → 生成 copy
                - 若 value 是 cast(L0C load)     → 生成 L0C→dst copy
                - 若 value 是一元算子            → 生成向量 unary call
                - 若 value 是二元算子:
                    · 两边都简单  → 一条 binary/scalar 向量指令
                    · 一边复杂    → 先把复杂侧 Decompose 到临时缓冲，再一条 binary
                    · 两边都复杂  → 各自 Decompose 到临时缓冲，再一条 binary
```

关键数据结构是一张算子映射表（[ascend_lower_parallel_to_vector.cc:37-42](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_lower_parallel_to_vector.cc#L37-L42) 与 [:56-122](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_lower_parallel_to_vector.cc#L56-L122)），它把 TIR 的 `tir.exp`/`AddNode`/`MulNode`/... 一一对应到 `tl.ascend_exp`/`tl.ascend_add`/`tl.ascend_mul`/...。

#### 4.2.3 源码精读

**(1) 算子映射表**——这是「符号表达式能变成 Ascend 指令」的依据。一元表（[ascend_lower_parallel_to_vector.cc:37-42](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_lower_parallel_to_vector.cc#L37-L42)）：

```cpp
const std::unordered_map<std::string, Op> kTIRUnaryOpMap = {
    {"tir.exp",   tl::ascend_exp()},
    {"tir.log",   tl::ascend_ln()},
    {"tir.sqrt",  tl::ascend_sqrt()},
    {"tir.rsqrt", tl::ascend_rsqrt()},
    {"tir.fabs",  tl::ascend_abs()}};
```

也就是说，你在循环体里写的 `T.exp(x)`（TVM 生成 `tir.exp` 节点）会被这张表替换成 `tl.ascend_exp` intrinsic。二元表用同样方式登记了 `Add/Sub/Mul/Div/Min/Max` 以及位运算（[:71-93](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_lower_parallel_to_vector.cc#L71-L93)）。ReLU 是个特例：`max(x, 0)` 会被识别成 `tl.ascend_relu`（见 `IsUnaryOp` 中对 `MaxNode` 的处理，[:1053-1078](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_lower_parallel_to_vector.cc#L1053-L1078)）。

**(2) 入口：识别 Parallel 循环并尝试向量化**（[ascend_lower_parallel_to_vector.cc:265-336](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_lower_parallel_to_vector.cc#L265-L336)）。这段代码依次处理三种形态：1D Parallel、`Parallel→Parallel` 的 2D 嵌套、以及无法向量化时的退化路径。其中 3D 嵌套直接报错（[:298-302](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_lower_parallel_to_vector.cc#L298-L302)）：

```cpp
if (third_for && third_for->kind == ForKind::kParallel) {
  LOG(FATAL) << "Unsupported: 3D or higher dimensional parallel loops detected. "
             << "Only 1D and 2D parallel loops are supported for T.Parallel.";
}
```

注意一个重要的健壮性设计：**任何无法向量化的 `kParallel` 都会被降级为 `kSerial`**（[:286-295](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_lower_parallel_to_vector.cc#L286-L295)），注释说明原因——Ascend 没有原生 parallel-for，残留的 `kParallel` 一旦流到 codegen 会触发 `Find undefined Variable v_thread` 错误。所以 pass 保证「向量化失败也绝不留下非法 IR」。

**(3) 拆解表达式：`DecomposeExpression`**（[ascend_lower_parallel_to_vector.cc:879-1015](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_lower_parallel_to_vector.cc#L879-L1015)）。这是 pass 的「大脑」。它对等号右侧表达式做模式匹配：

- 单纯 `BufferLoad` → 生成 copy（含一种安全保护：UB→UB 但行宽不同时拒绝，[:909-916](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_lower_parallel_to_vector.cc#L909-L916)）；
- `cast(L0C load)` → 生成 L0C→dst 的 copy（[:887-898](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_lower_parallel_to_vector.cc#L887-L898)）；
- 一元/二元算子 → 调对应生成函数。

对于二元算子，它按「左右操作数是简单还是复杂」分四种情况（[:970-1012](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_lower_parallel_to_vector.cc#L970-L1012)）。其中最关键的是「两边都复杂」时——例如 `c = a*b + a/b`——pass 会先各申请一个临时缓冲，分别算出 `a*b` 和 `a/b`，再相加（[:987-1012](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_lower_parallel_to_vector.cc#L987-L1012)）：

```cpp
if (left_is_complex && right_is_complex) {
  Buffer lhs_tmp = CreateTempBufferLike(output_buffer, element_count, inner_vec_len);
  Buffer rhs_tmp = CreateTempBufferLike(output_buffer, element_count, inner_vec_len);
  ...
  DecomposeExpression(operands[0], lhs_tmp, ...);  // 先算 a*b 进 lhs_tmp
  DecomposeExpression(operands[1], rhs_tmp, ...);  // 再算 a/b 进 rhs_tmp
  statements->push_back(GenerateBinaryVectorCall(... lhs_tmp, rhs_tmp ...));  // 再相加
}
```

这正是编程指南里说的「复杂表达式会被拆成多个更简单的表达式，并分配临时缓冲」（[Programming Guide:960-978](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L960-L978)）。

**(4) 标量与广播**：当某个操作数不含向量维下标（例如 `c_ub[i,j] = a_ub[i,j] + 1` 或 `c_ub[i,j] = a_ub[i,j] * b_ub[i]`），pass 走 `GenerateScalarVectorCall` / `GenerateBufferScalarVectorCall`（[:1269-1347](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_lower_parallel_to_vector.cc#L1269-L1347)），把二元算子换成带 `s` 后缀的标量变体（`ascend_adds`/`ascend_muls`/...）；对 1D→2D 的广播，则走 `CanBroadcast`（[:1509-1543](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_lower_parallel_to_vector.cc#L1509-L1543)）+ `GenerateBroadcastStmt`（[:1640-1698](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_lower_parallel_to_vector.cc#L1640-L1698)）先做一次广播再计算。

**(5) GM 输出的中转**：若 `T.Parallel` 直接写 GM（输出 buffer 是 global），pass 会先在 UB 里算好一块临时缓冲，再插一条 `ascend_copy` 把结果搬回 GM（[:632-641](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_lower_parallel_to_vector.cc#L632-L641) 与 [:784-789](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_lower_parallel_to_vector.cc#L784-L789)）。这解释了「Vector 计算必须发生在 UB」这一约束。

**(6) pass 注册**（[ascend_lower_parallel_to_vector.cc:1874-1883](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_lower_parallel_to_vector.cc#L1874-L1883)）：

```cpp
tvm::transform::Pass AscendLowerParallelToVector() {
  auto pass_func = [=](PrimFunc f, IRModule m, PassContext ctx) {
    auto new_func = AscendLowerParallelToVector::Substitute(std::move(f));
    return new_func;
  };
  return CreatePrimFuncPass(pass_func, 0, "tl.AscendLowerParallelToVector", {});
}
TVM_REGISTER_GLOBAL("tl.transform.AscendLowerParallelToVector")
    .set_body_typed(AscendLowerParallelToVector);
```

它在编译流水线里的位置是 `LowerAndLegalize` 阶段、紧跟 `Simplify` 之后（[phase.py:64-65](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py#L64-L65)），即在 `LayoutInference` / `LowerTileOp` 之前就把 `T.Parallel` 展平成向量 intrinsic。

#### 4.2.4 代码实践

**实践目标**：用「源码阅读」方式，验证 pass 对不同表达式的拆解策略。

**操作步骤**：

1. 打开 [src/transform/ascend_lower_parallel_to_vector.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_lower_parallel_to_vector.cc)，在 `DecomposeExpression`（[:879](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_lower_parallel_to_vector.cc#L879)）处对照下列三段假想 kernel，预测各自会命中哪条分支、生成几条向量指令：
   - `c[i,j] = a[i,j] + b[i,j]` → 命中 `HandleSimpleCase`，1 条 `ascend_add`。
   - `c[i,j] = T.exp(a[i,j])` → 命中 `IsUnaryOp`，1 条 `ascend_exp`。
   - `c[i,j] = a[i,j]*b[i,j] + a[i,j]/b[i,j]` → 命中「两边都复杂」分支，2 个临时缓冲 + 3 条指令（`mul`/`div`/`add`）。
2. 在 [examples/elementwise/elementwise_add.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/elementwise/elementwise_add.py) 基础上写一个临时脚本，分别编译上述三段，调用 `func.get_kernel_source()` 查看生成的 Ascend C 代码。

**需要观察的现象**：第三段（复杂表达式）的生成代码里应出现额外的 UB 临时 buffer 声明，以及 `Mul`/`Div`/`Add` 三条向量指令。

**预期结果**：与预测一致。若本地无 NPU，无法 `get_kernel_source`，则标记「待本地验证」，仅完成源码侧的分支预测练习即可。

#### 4.2.5 小练习与答案

**练习 1**：为什么 pass 在向量化失败时要把 `kParallel` 降级为 `kSerial`，而不是直接报错？

> **答案**：因为 Ascend 没有原生 parallel-for 语义。残留的 `kParallel` 流到 codegen 会被当成线程维度 `v_thread` 绑定，触发 `Find undefined Variable v_thread` 错误。降级为串行循环是「保正确性」的兜底，确保任何合法的 `T.Parallel` 都能编出可运行的 kernel（哪怕慢）。

**练习 2**：`c[i,j] = a[i,j] * b[i,j] + a[i,j] / b[i,j]` 会产生几个临时 UB 缓冲？为什么指南建议开启自动缓冲复用？

> **答案**：2 个（`lhs_tmp`、`rhs_tmp`，各与 Tile 等大）。因为每多一层嵌套，临时缓冲数量就可能翻倍，而它们的生命周期其实互不重叠。开启 `TL_ASCEND_MEMORY_PLANNING`（`AscendMemoryPlanning` pass，[phase.py:117](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py#L117)）后，编译器会按活跃度复用这些缓冲，显著降低 UB 占用。

---

### 4.3 T.tile.* 显式向量范式与广播、临时缓冲

#### 4.3.1 概念说明

除了「`T.Parallel` + 符号 API」，TileLang 在 Ascend 上还提供另一套等价范式：`T.tile.*`。它是一组**显式向量内联函数**，每调用一次就直接发射一条 Ascend 向量指令，例如：

- `T.tile.add(dst, src0, src1)` —— `dst = src0 + src1`
- `T.tile.mul(dst, src0, src1)` —— `dst = src0 * src1`
- `T.tile.exp(dst, src0)` —— `dst = exp(src0)`
- `T.tile.max / min / sub / div / abs / sqrt / rsqrt / ln / relu / ...`

两者**语义等价、生成代码一致**，区别只在于：

| 维度 | `T.Parallel` + 符号 API | `T.tile.*` 显式范式 |
| --- | --- | --- |
| 写法 | `c[i,j] = a[i,j] + b[i,j]` | `T.tile.add(c, a, b)` |
| 抽象层级 | 高，像 NumPy | 低，接近硬件指令 |
| 可移植性 | 好（CPU/GPU/Ascend 通用） | 仅 Ascend |
| 复杂表达式 | 编译器自动拆解 + 临时缓冲 | 用户手动拆成多条语句 |
| 与 reduce/广播配合 | 自然 | 需配合 `T.tile.broadcast` |

何时用哪种？官方建议（[Programming Guide:1032-1066](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L1032-L1066)）：追求清晰与可移植用符号 API；追求对每条指令的精确控制（如手写高性能 softmax/attention）用 `T.tile.*`。本仓库的 [examples/softmax/example_online_softmax.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/softmax/example_online_softmax.py) 就全程使用 `T.tile.*`。

#### 4.3.2 核心流程

`T.tile.*` 的实现模式高度统一：取三个 buffer 的 `access_ptr`（读/写指针）和元素数 `size`，发射一个 `tir.call_intrin("tl.ascend_<op>", ...)`。以二元 `add` 为例：

```text
T.tile.add(dst, src0, src1):
    dst_ptr  = dst.access_ptr("w")
    src0_ptr = src0.access_ptr("r")
    校验 size(dst) == size(src0)
    若 src1 是标量/单元素 BufferLoad → 发射 tl.ascend_adds（标量变体）
    若 src1 是 Buffer/Region         → 发射 tl.ascend_add（向量变体）
```

广播不是自动的：如果你要把一个 `[M,1]` 的 buffer 广播成 `[M,N]` 参与 `T.tile.mul`，需要先显式调用 `T.tile.broadcast(dst_2d, src_1d)` 得到一个 `[M,N]` 的 buffer，再传入 `mul`。这一点与 `T.Parallel` 路线（编译器自动广播）不同。

#### 4.3.3 源码精读

**(1) 二元算子统一入口 `binary_op`**（[ascend_tile.py:797-853](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend_tile.py#L797-L853)）。它根据 `src1` 的类型分三条路：`BufferLoad`（单元素）走 `tl.ascend_<op>s` 标量变体；`PrimExpr/float/int` 标量也走 `s` 变体；`Buffer/Region` 走向量变体 `tl.ascend_<op>`。关键片段：

```python
if isinstance(src1, (PrimExpr, float, int)):
    return T.call_intrin("handle", tir.op.Op.get(f"tl.ascend_{op}s"),
                         dst_ptr, src0_ptr, src1, size_0)
elif isinstance(src1, BufferRegion):
    ...
    return T.call_intrin("handle", tir.op.Op.get(f"tl.ascend_{op}"),
                         dst_ptr, src0_ptr, src1_ptr, size_0)
```

注意这里的命名约定：向量变体是 `tl.ascend_add`，标量变体是 `tl.ascend_adds`（多一个 `s`）——这与 4.2 节 pass 里 `GenerateScalarVectorCall` 选 `ascend_adds` 完全一致，印证了两条路线殊途同归。`add/sub/mul/div/max/min` 都是这个 `binary_op` 的薄封装（[:856-919](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend_tile.py#L856-L919)）：

```python
def add(dst, src0, src1):
    return binary_op(dst, src0, src1, "add")
def mul(dst, src0, src1):
    return binary_op(dst, src0, src1, "mul")
```

**(2) 一元算子 `unary_op`**（[ascend_tile.py:944-967](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend_tile.py#L944-L967)）。`exp/ln/abs/sqrt/rsqrt/relu` 都走这里，发射 `tl.ascend_<op>`（无标量变体）。例如（[:970-977](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend_tile.py#L970-L977)）：

```python
def exp(dst, src0):
    return unary_op(dst, src0, "exp")
```

**(3) 广播 `T.tile.broadcast`**（[ascend_tile.py:2058-2151](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend_tile.py#L2058-L2151)）。它把 1D `[R]` 视图化成 `[1, R]` 或 `[R, 1]`，再发射 `tl.ascend_broadcast`。这正是 `T.Parallel` 路线里「`c[i,j] = a[i,j] * b[i]`」那种行广播在显式范式下的对应物——只不过这里要用户自己调一次。

**(4) 真实算子里的密集使用**：在 online softmax 里，可以看到 `T.tile.max/sub/exp/mul/broadcast/add` 串成一条数据流（[example_online_softmax.py:76-85](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/softmax/example_online_softmax.py#L76-L85)）：

```python
T.reduce_max(a_cal, tile_max, dim=-1)            # reduce（u3-l4）
T.tile.max(tile_max, prev_max, tile_max)         # m_j = max(m_{j-1}, x_j)
T.tile.sub(tmp_exp, prev_max, tile_max)          # m_{j-1} - m_j
T.tile.exp(tmp_exp, tmp_exp)                     # exp(m_{j-1} - m_j)
T.tile.mul(tmp_exp, prev_sum, tmp_exp)           # s_{j-1} * exp(...)
T.tile.broadcast(tile_max_2d, tile_max)          # 广播成 [M,N]
T.tile.sub(a_cal, a_cal, tile_max_2d)            # x_j - m_j
T.tile.exp(a_cal, a_cal)                         # exp(x_j - m_j)
T.reduce_sum(a_cal, tile_sum, dim=-1)
T.tile.add(prev_sum, tile_sum, tmp_exp)
```

这段是「`T.tile.*` + reduce + broadcast」三件套的标准组合，建议精读。

#### 4.3.4 代码实践

**实践目标**：用 `T.tile.*` 复现一个最简单的「exp + 减最大值 + 除以和」序列，理解显式范式下每条指令的对应关系。

**操作步骤**：

1. 阅读 [examples/softmax/example_online_softmax.py:76-100](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/softmax/example_online_softmax.py#L76-L100)。
2. 在本地复制该文件为 `my_softmax.py`，把第二段（second pass，[:92-103](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/softmax/example_online_softmax.py#L92-L103)）的核心三步单独标注：
   - `T.tile.sub(a_cal, a_cal, prev_max_2d)` —— 减最大值
   - `T.tile.exp(a_cal, a_cal)` —— exp
   - `T.tile.div(a_cal, a_cal, prev_sum_2d)` —— 除以和
3. 运行 `python my_softmax.py`。

**需要观察的现象**：对每组 `(M, N, block_M, block_N, dtype)` 配置，终端输出 `Test pass!`，最后输出 `Kernel Output Match!`。

**预期结果**：与 PyTorch 的 `F.softmax(a, dim=1)` 数值对齐（fp16/bf16 用 `rtol=1e-2`，fp32 用 `rtol=1e-4`）。若本地无 NPU 则标记「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`T.tile.add(c, a, 2)` 和 `T.tile.add(c, a, b)`（b 是与 a 同形 buffer）生成的 intrinsic 名字分别是什么？为什么不同？

> **答案**：前者是 `tl.ascend_adds`（标量变体，`s` 后缀），后者是 `tl.ascend_add`（向量变体）。因为 Ascend 硬件对「向量 + 标量」和「向量 + 向量」是两条不同指令，前者只需一个广播后的标量寄存器，后者要读一整块 UB。

**练习 2**：用 `T.tile.*` 实现 `c = a*b + a/b`，需要几条指令？是否需要临时缓冲？

> **答案**：3 条（`mul` 进临时 t1、`div` 进临时 t2、`add` c=t1+t2），需要 2 个临时 UB 缓冲（或让 `mul` 直接写进 c 再原地加，可省到 1 个）。这正是 `T.Parallel` 路线里 pass 自动帮你做的事——显式范式下要你自己规划。

---

## 5. 综合实践

**任务**：实现一个「单 Tile 内的行向 softmax」算子，要求**同时给出两种写法并验证它们数值一致**。这是把本讲三个最小模块（`T.Parallel`、`AscendLowerParallelToVector`、`T.tile.*`）串起来的综合练习。

**算法**（对每个 Tile 的每一行）：

\[ m_i = \max_j x_{i,j}, \quad e_{i,j} = \exp(x_{i,j} - m_i), \quad s_i = \sum_j e_{i,j}, \quad y_{i,j} = \frac{e_{i,j}}{s_i} \]

其中逐元素的 `exp / 减 / 除` 用本讲的原语，行 max/sum 用 u3-l4 的 reduce。

**写法 A：`T.Parallel` + 符号 API**（示例代码骨架，供参考补全）：

```python
@tilelang.jit(out_idx=[1])
def softmax_parallel(M, N, block_M, block_N, dtype="float16"):
    m_num = T.ceildiv(M, block_M)
    n_num = T.ceildiv(N, block_N)
    VEC_NUM = 2
    rows = block_M // VEC_NUM

    @T.prim_func
    def main(A: T.Tensor((M, N), dtype), B: T.Tensor((M, N), dtype)):
        with T.Kernel(m_num * n_num, is_npu=True) as (cid, vid):
            bx = cid // n_num
            by = cid % n_num
            a_ub = T.alloc_ub((rows, block_N), dtype)
            e_ub = T.alloc_ub((rows, block_N), dtype)
            row_max = T.alloc_ub((rows,), dtype)   # 每行一个 max
            row_sum = T.alloc_ub((rows,), dtype)

            with T.Scope("V"):
                T.copy(A[bx*block_M + vid*rows, by*block_N], a_ub)
                T.reduce_max(a_ub, row_max, dim=-1)          # m_i
                # 逐元素：e = exp(a - m)，m[i] 沿列广播
                for (i, j) in T.Parallel(rows, block_N):
                    e_ub[i, j] = T.exp(a_ub[i, j] - row_max[i])
                T.reduce_sum(e_ub, row_sum, dim=-1)          # s_i
                # 逐元素：y = e / s，s[i] 沿列广播
                for (i, j) in T.Parallel(rows, block_N):
                    B_kernel_tmp[i, j] = e_ub[i, j] / row_sum[i]
                T.copy(..., B[...])   # 写回 GM（或直接在 GM 上 Parallel，由 pass 中转 UB）
    return main
```

> 注：上面 `B_kernel_tmp` 需另开 UB 缓冲再 `T.copy` 回 GM；也可直接对 GM 写，由 `AscendLowerParallelToVector` 自动插 UB 中转（见 4.2.3 第 5 点）。

**写法 B：`T.tile.*` 显式范式**（参照 [example_online_softmax.py:90-99](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/softmax/example_online_softmax.py#L90-L99) 的 second pass）：

```python
with T.Scope("V"):
    T.copy(A[..., ...], a_ub)
    T.reduce_max(a_ub, row_max, dim=-1)
    T.tile.broadcast(row_max_2d, row_max)        # [rows] → [rows, block_N]
    T.tile.sub(e_ub, a_ub, row_max_2d)           # e = a - m
    T.tile.exp(e_ub, e_ub)                        # e = exp(e)
    T.reduce_sum(e_ub, row_sum, dim=-1)
    T.tile.broadcast(row_sum_2d, row_sum)
    T.tile.div(e_ub, e_ub, row_sum_2d)           # y = e / s
    T.copy(e_ub, B[..., ...])
```

**验证步骤**：

1. 两种写法分别编译，用 `func.get_kernel_source()` 抽取生成代码，对比核心向量指令（`Exp`/`Sub`/`Div`/`ReduceMax`/`ReduceSum`）是否一致。
2. 用同一组输入 `a = torch.randn(M, N).npu()` 跑两个 kernel，与 `torch.nn.functional.softmax(a, dim=1)` 对比，`torch.testing.assert_close` 通过。
3. 思考：写法 B 里你「手动」插入了 `broadcast`；写法 A 里这件事是谁做的？（答：`AscendLowerParallelToVector` 的 `CanBroadcast`/`GenerateBroadcastStmt`。）

**预期结果**：两种写法数值一致、生成指令高度相似；写法 A 更简洁、写法 B 对每条指令可控。若本地无 NPU，至少完成源码对照与指令预测，标记「待本地验证」。

## 6. 本讲小结

- `T.Parallel` 在 IR 层就是一个 `ForKind::kParallel` 循环，语义是「Tile 内逐元素独立并行」，前端定义极薄（[parallel.py:10-31](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/parallel.py#L10-L31)），硬件细节全部后置到 C++ pass。
- `AscendLowerParallelToVector` 是把并行循环翻译成 Ascend 向量指令的核心 pass，落在 `LowerAndLegalize` 阶段（[phase.py:65](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py#L65)）；它用一张 TIR→Ascend 算子映射表（[:37-42](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_lower_parallel_to_vector.cc#L37-L42)）驱动 `DecomposeExpression`（[:879-1015](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_lower_parallel_to_vector.cc#L879-L1015)）递归拆解表达式。
- 复杂表达式（两边都复杂）会引入临时 UB 缓冲，故指南强烈建议开启 `TL_ASCEND_MEMORY_PLANNING`（[phase.py:117](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py#L117)）做缓冲复用。
- `T.tile.*`（[ascend_tile.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend_tile.py)）是与符号 API 等价的显式向量范式，每调用一次直接发射 `tl.ascend_*` intrinsic；标量变体带 `s` 后缀（`ascend_adds` 等），与 pass 内选用的指令完全一致。
- 广播在两条路线里处理方式不同：`T.Parallel` 路线由 pass 自动广播（`c[i,j] = a[i,j] * b[i]`），`T.tile.*` 路线需用户先调 `T.tile.broadcast`。
- pass 对任何无法向量化的 `kParallel` 都降级为 `kSerial`（[:286-295](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_lower_parallel_to_vector.cc#L286-L295)），3D Parallel 直接报错（[:298-302](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_lower_parallel_to_vector.cc#L298-L302)）。

## 7. 下一步学习建议

- **u3-l6（T.Pipelined）**：本讲的逐元素计算常和搬运（`T.copy`）叠加，`T.Pipelined` 让「搬运」和「Vector 计算」重叠起来以掩盖延迟，是 softmax/GEMM 提速的关键。
- **u4-l1（Expert 内存分配与 Cube/Vector Scope）**：本讲聚焦 Vector 域（`T.Scope("V")`），下一单元会讲 Cube 域以及如何在同一 kernel 里协调 Cube 矩阵乘与 Vector 逐元素。
- **u6-l1（编译 Pass 全景）**：把本讲的 `AscendLowerParallelToVector` 放回两阶段 pass 流水线里整体理解，看清它和 `LayoutInference`、`AscendMemoryPlanning`、`AscendSyncInsert` 的先后与依赖。
- **精读 [examples/softmax/example_online_softmax.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/softmax/example_online_softmax.py) 与 [examples/flash_attention/flash_attn_bhsd.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/flash_attention/flash_attn_bhsd.py)**：这两个算子密集使用 `T.tile.*` + reduce + broadcast + broadcast，是检验本讲掌握程度的最佳真实样本。
