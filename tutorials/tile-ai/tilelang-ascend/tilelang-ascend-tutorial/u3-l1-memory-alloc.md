# 内存层级与分配原语

## 1. 本讲目标

本讲是「Developer 模式核心原语」的第一讲，聚焦**算子里那些数据该放在哪块存储上**。学完后你应该能够：

- 说清楚 tile-lang 的 `shared` / `fragment` 两层抽象与 Ascend 片上物理存储（GM、L1、UB、L0A/L0B/L0C）的对应关系。
- 用 Developer 模式的 `T.alloc_shared`、`T.alloc_fragment`、`T.alloc_var` 三类原语分配缓冲，并理解它们各自落在哪一层。
- 理解 `AscendInferBufferScope` 这个编译 pass 如何**根据上下文自动推断**一个缓冲最终落在 L1、UB 还是 L0A/L0B/L0C，从而让你不必显式指定物理存储。
- 对比 Developer 抽象与 Expert 的 `alloc_L1` / `alloc_L0C` 等显式原语，知道两者生成的代码在「scope 是手写还是编译器推」这件事上的差别。

> 前置：本讲承接 [u2-l2](./u2-l2-kernel-launch.md) 的 `T.Kernel` / `cid` 概念。我们假设你已经能写出一个最小的 kernel 上下文，并知道一个核要负责计算 C 的某一块。

## 2. 前置知识

### 2.1 Ascend 片上存储层级回顾

在 [u1-l1](./u1-l1-project-overview.md) 里我们提过 Ascend NPU 的存储层级。这里再细化一下，因为本讲完全围绕它展开：

| 物理存储 | 归属 | 作用 | 容量/速度 |
| --- | --- | --- | --- |
| GM（Global Memory / HBM） | 全片 | 存放输入输出张量 | 最大、最慢 |
| L1（L1 Buffer） | Cube 核 | 缓存 Cube（矩阵乘）的输入 tile | 片上、快 |
| UB（Unified Buffer） | Vector 核 | 缓存 Vector（逐元素/规约）的数据 | 片上、快 |
| L0A / L0B | Cube 核 | Cube 单元的 A、B 输入寄存器 | 最小、最快 |
| L0C（Accumulator） | Cube 核 | Cube 单元的累加结果寄存器 | 小、最快 |

一个关键事实：**Cube 计算（矩阵乘）的输入必须落在 L1，再搬进 L0A/L0B；Vector 计算（exp、reduce 等）的数据必须落在 UB**。数据不能凭空跨层，必须靠 `T.copy` 一步步搬运。这正是后面 u3-l2 要讲的内容，本讲只关心「怎么把一块缓冲声明在正确的层」。

### 2.2 GPU 类比（如果你熟悉 CUDA）

如果你写过 CUDA，可以这样类比：

- GM ≈ device memory（HBM）
- L1 / UB ≈ shared memory（片上高速缓存，只是 Ascend 把它拆成给 Cube 用的 L1 和给 Vector 用的 UB 两个）
- L0A / L0B / L0C ≈ 寄存器 / Tensor Core 的输入输出寄存器（wmma fragment）

tile-lang 把这两类存储抽象成两层：`shared`（≈ shared memory）和 `fragment`（≈ 寄存器/Tensor Core fragment）。你写 kernel 时只需声明「这是 shared 还是 fragment」，编译器再把它落到 L1 / UB 还是 L0A/L0B/L0C。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [tilelang/language/allocate.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/allocate.py) | Python 前端的分配原语定义，`alloc_shared` / `alloc_fragment` / `alloc_var` 及 Expert 的 `alloc_L1` 等都在这里 |
| [src/transform/ascend_infer_buffer_scope.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_infer_buffer_scope.cc) | `AscendInferBufferScope` pass 的 C++ 实现，负责把 `shared` / `fragment` 推断成具体物理 scope |
| [tilelang/engine/phase.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py) | pass 流水线，`AscendInferBufferScope` 被挂在 `LowerAndLegalize` 阶段 |
| [examples/developer_mode/gemm_developer.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/gemm_developer.py) | Developer 模式 GEMM 实例，用 `alloc_shared` + `alloc_fragment` |
| [examples/gemm/example_gemm_intrinsic.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic.py) | Expert 模式 GEMM 实例，用 `alloc_L1` / `alloc_L0A` / `alloc_L0B` / `alloc_L0C` 显式指定 |

## 4. 核心概念与源码讲解

### 4.1 概念说明：两层抽象与 scope 映射

#### 4.1.1 shared 与 fragment 是什么

tile-lang 把 Ascend 的五块片上存储**压缩成两个前端概念**：

- **shared**：片上高速缓存层，用来缓存 GM 和计算单元之间的中间数据块。在 Ascend 上它**动态地**落在 L1（给 Cube 用）或 UB（给 Vector 用）。你声明时不用管是 L1 还是 UB。
- **fragment**：寄存器层，专门给某个计算单元（主要是 Cube）做输入/输出。在 Ascend 上它落在 L0A / L0B / L0C 三块之一，同样由编译器根据上下文决定。

为什么只给两层抽象？因为初学者很难一次性记对「这个 tile 该放 L1 还是 UB、该放 L0A 还是 L0B」。Developer 模式的核心思想就是：**你只声明语义意图（shared 还是 fragment），物理位置由编译器推断**。这正是本讲后半段 `AscendInferBufferScope` pass 做的事。

而 Expert 模式则相反：它提供 `alloc_L1` / `alloc_ub` / `alloc_L0A` / `alloc_L0B` / `alloc_L0C` 等原语，让你**显式**指定物理存储，代价是你必须自己保证数据流和存储层级一致。两套抽象可以混用。

#### 4.1.2 TIR scope 字符串到物理存储的映射表

这套「逻辑 scope → 物理 scope」的映射在源码里有明确注释。见 [tilelang/language/allocate.py:128-137](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/allocate.py#L128-L137)：

```python
# shared -> dynamic (L1/UB, resolved by InferAllocScope)
# shared.l1 -> L1
# shared.ub -> UB
# wmma.matrix_a -> L0A
# wmma.matrix_b -> L0B
# wmma.accumulator -> L0C
```

理解要点：

- Developer 的 `alloc_shared` 默认 scope 是 `"shared"`（dynamic），`alloc_fragment` 默认是 `"local.fragment"`。这两个都是**待推断**的。
- Expert 的 `alloc_L1` / `alloc_ub` / `alloc_L0A` / `alloc_L0B` / `alloc_L0C` 则直接写出确定的 scope 字符串，**不再需要推断**。见 [tilelang/language/allocate.py:140-157](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/allocate.py#L140-L157)。

把这张表记牢，是理解本讲后面所有自动推断逻辑的基础。

#### 4.1.3 底层都是 T.alloc_buffer

无论是 Developer 还是 Expert 的原语，最终都只是 `T.alloc_buffer(shape, dtype, scope=...)` 的一层薄封装，差别只在传入的 `scope` 字符串。`scope` 是 TVM/TensorIR 里 buffer 的 `storage_scope` 属性，它决定了 codegen 阶段这块缓冲被当成 AscendC 的 `LocalTensor<L1>` 还是 `LocalTensor<UB>` 等等。所以「分配」在前端是声明式的，真正的物理分配发生在编译后端。

### 4.2 T.alloc_shared：分配 shared 层缓冲

#### 4.2.1 概念说明

`T.alloc_shared(shape, dtype)` 分配一块 shared 层缓冲。它的语义是「我要一块片上高速缓存，用来暂存 GM 和计算单元之间的 tile」。它**不指定** L1 还是 UB——这要等到 `AscendInferBufferScope` pass 看到这块缓冲被谁用之后再决定。

典型的 Developer GEMM 里，矩阵 A、B 的 tile 就声明成 shared，因为它们要从 GM 搬进来、再喂给 Cube。

#### 4.2.2 核心流程

```text
alloc_shared(shape, dtype)
   └─ 默认 scope = "shared"（dynamic）
   └─ （bool 特殊处理：scope 改为 "shared.ub"，见下）
   └─ T.alloc_buffer(shape, dtype, scope=scope)
   └─ 返回 TIR Buffer，scope 暂时是 "shared"，等待 pass 推断
```

#### 4.2.3 源码精读

[tilelang/language/allocate.py:31-46](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/allocate.py#L31-L46)：

```python
def alloc_shared(shape, dtype, scope="shared"):
    if dtype == "bool":
        # lei: This is a hack to handle bool type.
        # Because tilelang's merge smem pass cannot merge bool type currently.
        scope = "shared.ub"
    return T.alloc_buffer(shape, dtype, scope=scope)
```

要点：

- 默认 `scope="shared"`，即上面映射表里的 dynamic，等待 `InferAllocScope` 解析为 L1 或 UB。
- 有一个 bool 类型的特殊分支：bool 强制落到 `"shared.ub"`（UB）。注释说是因为 merge shared memory pass 暂时不能合并 bool 类型，所以直接钉死在 UB。这是一个「待确认能否放开」的历史 hack，了解即可。

真实使用见 [examples/developer_mode/gemm_developer.py:41-42](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/gemm_developer.py#L41-L42)：

```python
A_L1 = T.alloc_shared((block_M, K_L1), dtype)
B_L1 = T.alloc_shared((K_L1, block_N), dtype)
```

注意变量名虽然叫 `A_L1` / `B_L1`，但前端只是声明了 `shared`。这两块**最终**落到 L1，是因为它们后面会喂给 `T.gemm_v0`（Cube 计算），由 pass 推断出来的——名字只是给人看的，scope 以编译器推断为准。

### 4.3 T.alloc_fragment：分配 fragment（寄存器级）缓冲

#### 4.3.1 概念说明

`T.alloc_fragment(shape, dtype, scope='local.fragment')` 分配一块 fragment 层缓冲，对应 Ascend 的 L0A / L0B / L0C。它专门用来做计算单元的输入/输出，尤其是 Cube 矩阵乘的累加器。

在 GEMM 里，累加结果 C 的 tile 通常声明成 fragment，因为它是 `T.gemm_v0` 的输出，要落在 Cube 的 L0C 累加器里。注意累加器通常用更宽的精度（如 `accum_dtype="float"`）以抑制大 K 累加时的误差——这点在 [u2-l1](./u2-l1-kernel-and-dtype.md) 讲 dtype 时已经提过。

#### 4.3.2 核心流程

```text
alloc_fragment(shape, dtype)
   └─ 默认 scope = "local.fragment"
   └─ T.alloc_buffer(shape, dtype, scope="local.fragment")
   └─ 返回 TIR Buffer，scope 是 "local.fragment"，等待 pass 推断为 L0A/L0B/L0C
```

#### 4.3.3 源码精读

[tilelang/language/allocate.py:49-60](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/allocate.py#L49-L60)：

```python
def alloc_fragment(shape, dtype, scope="local.fragment"):
    return T.alloc_buffer(shape, dtype, scope=scope)
```

它比 `alloc_shared` 还简单——没有 bool 特例，纯粹把 `local.fragment` 交给 `alloc_buffer`。真正决定它是 L0A/L0B/L0C 的，是它出现在 `T.gemm_v0(...)` 调用里的**哪个位置**（A=第0个、B=第1个、C=第2个），这点在 4.5 的 pass 里详细讲。

真实使用见 [examples/developer_mode/gemm_developer.py:44](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/gemm_developer.py#L44)：

```python
C_L0 = T.alloc_fragment((block_M, block_N), accum_dtype)
```

后面 `T.gemm_v0(A_L1, B_L1, C_L0, init=(k == 0))` 把 `C_L0` 当作第三个参数（累加器位置），pass 据此把 `C_L0` 推断成 `wmma.accumulator`（L0C）。

> 提示：fragment 默认容量很小（寄存器级）。如果你声明了一个超大 fragment 算不下来，往往是因为这块本该是 shared（搬运算子的中间缓存）。一句话：**搬运/缓存用 shared，计算单元的输入输出用 fragment**。

### 4.4 T.alloc_var：分配标量变量

#### 4.4.1 概念说明

前面两个原语分配的都是**张量缓冲**（带 shape 的 tile）。`T.alloc_var(dtype, init=...)` 不同——它分配的是一个**单元素标量变量**，scope 默认 `"local.var"`，对应一个寄存器。用途包括：条件判断的标志位（如 `bool`）、循环计数器、临时标量（如 softmax 里的 `m`、`l`）。

它最大的特点是**支持初始化**：可以给一个常量初值，也可以用另一个变量/表达式初始化，而不必默认零值。

#### 4.4.2 核心流程

```text
alloc_var(dtype, [init], [scope])
   └─ 解析位置参数：一个 str 当 scope，一个非 str 当 init
   └─ buffer = T.alloc_buffer([1], dtype, scope="local.var")  # 注意 shape 固定 [1]
   └─ if init 是常量(int/float):
   │     block_attr({"tl.local_var_init": {buffer.data: init_const}})  # 标记编译期初值
   └─ else if init 是表达式:
         T.buffer_store(buffer, init, 0)  # 生成一条 store 语句
```

#### 4.4.3 源码精读

[tilelang/language/allocate.py:71-125](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/allocate.py#L71-L125) 是完整实现。关键两段：

参数解析（兼容多种历史写法），见 [tilelang/language/allocate.py:95-113](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/allocate.py#L95-L113)：允许 `alloc_var("int32", 1)`、`alloc_var("int32", "local.var")`、`alloc_var("int32", 1, "local.var")`、`alloc_var("int32", init=1)` 等多种形式。

分配与初始化，见 [tilelang/language/allocate.py:118-125](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/allocate.py#L118-L125)：

```python
buffer = T.alloc_buffer([1], dtype, scope=parsed_scope)
if parsed_init is not None:
    if isinstance(parsed_init, (int, float, IntImm, FloatImm)):
        init_const = tvm.tir.const(parsed_init, dtype)
        block_attr({"tl.local_var_init": {buffer.data: init_const}})
    else:
        T.buffer_store(buffer, parsed_init, 0)
return buffer
```

要点：

- 形状固定 `[1]`，所以它本质是个标量寄存器，不是 tile。
- 初值分两条路径：**常量**初值用 `block_attr({"tl.local_var_init": ...})` 标记（编译期直接知道初值，便于后续优化）；**表达式/变量**初值用 `T.buffer_store` 生成一条真实的 store 指令。
- Programming Guide 给了丰富示例，见 [docs/TileLang-Ascend Programming Guide.md:549-590](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L549-L590)：标志位 `T.alloc_var("bool", init=False)`、计数器 `T.alloc_var("int32", init=1)`、变量间初始化 `b = T.alloc_var("int32", init=a)` 等。

> 易混点：`alloc_var` 返回的是 shape `[1]` 的 buffer，用的时候通常 `T.buffer_load(var, 0)` 读、`T.buffer_store(var, val, 0)` 写，或者直接当表达式用（`if flag:`）。它不是张量，不能切片下标 `var[i]`。

### 4.5 AscendInferBufferScope pass：编译器自动推断 scope

这是本讲最核心的机制。前面三个原语都把物理位置「悬置」了，真正把它钉死的就是这个 pass。

#### 4.5.1 概念说明

`AscendInferBufferScope`（注册名为 `tl.InferAllocScope`）是一个 **PrimFunc pass**，作用是：遍历 kernel 里每个 buffer 的**使用上下文**，把 dynamic scope 推断成确定 scope。

- 对 `"shared"`：看这块缓冲是被 Cube 计算（gemm）用，还是 Vector 计算用，还是只在搬运链路里中转——据此决定是 `shared.l1` 还是 `shared.ub`。
- 对 `"local.fragment"`：看这块缓冲在 gemm 调用里处于 A/B/C 哪个位置——据此决定是 `wmma.matrix_a` / `wmma.matrix_b` / `wmma.accumulator`。

它运行在 `LowerAndLegalize` 阶段、几乎是最早的几个 pass 之一。见 [tilelang/engine/phase.py:49-52](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py#L49-L52)：

```python
def LowerAndLegalize(mod: IRModule, target: Target) -> IRModule:
    mod = tilelang.transform.InjectTmpBuffer(target)(mod)
    mod = tilelang.transform.AscendInferBufferScope()(mod)
    ...
```

之所以要排这么靠前，是因为后面的几乎所有 pass（布局推断 `LayoutInference`、tile lowering `LowerTileOp`、存储重写等）都依赖 buffer 已经有确定的物理 scope。

#### 4.5.2 核心流程

pass 的整体结构是一个 `ScopeCorrector`，分三步走（见 [src/transform/ascend_infer_buffer_scope.cc:30-46](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_infer_buffer_scope.cc#L30-L46)）：

```text
Correct(func):
  1. BufferUseCollector：收集每个 buffer 在哪些函数调用里被用、是不是 gemm、gemm 第几位
  2. InferCorrectScopes()：按规则推断每个 buffer 的 corrected_scope
  3. StmtExprMutator：用新 scope 重写所有 Allocate / buffer 引用
```

关键规则在 `InferCorrectScopes`，伪代码如下（对应源码的判断分支）：

```text
对每个 buffer，根据 original_scope 分情况：
  if original_scope == "local.fragment":      # fragment 推断
      if 在 gemm 里当 A(第0位): -> wmma.matrix_a   (L0A)
      elif 在 gemm 里当 B(第1位): -> wmma.matrix_b (L0B)
      elif 在 gemm 里当 C(第2位): -> wmma.accumulator (L0C)
      else: -> wmma.accumulator                  # 兜底
  elif original_scope == "shared":              # shared 推断
      if 只被 Vector 用:        -> shared.ub      (UB)
      elif 只被 Cube(gemm) 用:  -> shared.l1      (L1)
      elif Cube 和 Vector 都用: -> shared.l1      (默认 L1，冲突 TODO)
      else: 保持 shared
```

#### 4.5.3 源码精读

**判断一个函数是不是 Cube / Vector 计算**，见 [src/transform/ascend_infer_buffer_scope.cc:82-95](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_infer_buffer_scope.cc#L82-L95)：

```cpp
static bool IsGEMMFunction(const std::string &func_name) {
  return IsGEMMInternal(ToLower(func_name));   // 关键词 gemm / mma / matmul
}
static bool IsVectorFunction(const std::string &func_name) {
  std::string lower_name = ToLower(func_name);
  if (IsGEMMInternal(lower_name)) return false;
  static const std::vector<std::string> kVectorKeywords = {"copy", "memcpy", "dma"};
  return !ContainsAny(lower_name, kVectorKeywords);  // 非 gemm、非搬运 => Vector
}
```

注意它靠**函数名关键词**判断类别：名字含 `gemm`/`mma`/`matmul` 算 Cube；不含这些、也不含 `copy`/`memcpy`/`dma` 的算 Vector。搬运类（copy/dma）既不算 Cube 也不算 Vector，单独处理。

**判断 gemm 里 A/B/C 的位置**，见 [src/transform/ascend_infer_buffer_scope.cc:309-330](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_infer_buffer_scope.cc#L309-L330)：

```cpp
if (func_name.find("gemm") != ... || func_name.find("mma") != ...) {
  if (access_ptr_count == 0) return 0; // A -> L0A
  if (access_ptr_count == 1) return 1; // B -> L0B
  if (access_ptr_count == 2) return 2; // C -> L0C
}
```

即按 buffer 在 gemm 调用里是第几个 buffer 参数来定位：第 0 个是 A，第 1 个是 B，第 2 个是 C。这就是为什么 `T.gemm_v0(A_L1, B_L1, C_L0, ...)` 里的 `C_L0` 会被推断成 L0C——它是第 3 个 buffer（位置 2）。

**fragment 的推断分支**，见 [src/transform/ascend_infer_buffer_scope.cc:384-397](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_infer_buffer_scope.cc#L384-L397)：

```cpp
if (original_scope == "local.fragment") {
  if (use_info && !use_info->gemm_positions.empty()) {
    if (use_info->gemm_positions.count(0) > 0)      corrected_scope = "wmma.matrix_a"; // L0A
    else if (use_info->gemm_positions.count(1) > 0) corrected_scope = "wmma.matrix_b"; // L0B
    else if (use_info->gemm_positions.count(2) > 0) corrected_scope = "wmma.accumulator"; // L0C
    else                                            corrected_scope = "wmma.accumulator";
  } else {
    corrected_scope = "wmma.accumulator";   // 没进 gemm 也兜底到 L0C
  }
}
```

**shared 的推断分支**，见 [src/transform/ascend_infer_buffer_scope.cc:398-416](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_infer_buffer_scope.cc#L398-L416)：

```cpp
else if (original_scope == "shared") {
  if (use_info) {
    if (used_in_vector && !used_in_cube)        corrected_scope = "shared.ub";   // 只 Vector => UB
    else if (used_in_cube && !used_in_vector)   corrected_scope = "shared.l1";   // 只 Cube => L1
    else if (used_in_cube && used_in_vector)    corrected_scope = "shared.l1";   // 都用 => 暂定 L1
    else                                        corrected_scope = original_scope;
  }
}
```

注意「Cube 和 Vector 都用」目前固定落到 `shared.l1`，源码里有个 `CheckSharedBufferConflict` 直接 `return false`（[src/transform/ascend_infer_buffer_scope.cc:593-595](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_infer_buffer_scope.cc#L593-L595)），注释标记为 TODO——说明「同一块 shared 同时被 Cube 和 Vector 访问」这种跨核场景，scope 推断还是个开放问题，复杂情况留给后续 pass（如 CombineCV / workspace 消除，见 u5 单元）处理。

源码里还有一段针对**搬运中转 buffer** 的特殊推断：当一个 `shared` buffer 只出现在 `tl.ascend_copy`（纯搬运）里、没被 Cube/Vector 直接计算，pass 会看它的「对端」——如果它被拷给一个最终会变 L0A/L0B 的 buffer，说明它是 GM→L1 的中转，落 `shared.l1`；否则落 `shared.ub`。见 [src/transform/ascend_infer_buffer_scope.cc:446-534](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_infer_buffer_scope.cc#L446-L534)。这条路径解释了「为什么 Developer 模式下你只写搬运，编译器也能猜对中转缓冲该放 L1 还是 UB」。

**为 L1 buffer 注入默认 zN 布局**：推断完 scope 后，pass 还会给所有 `shared.l1` buffer 注入一个默认的 zN（按 Cube 硬件要求的分块布局）layout map，否则后续 `LowerTileOp` 无法计算正确的 1D 物理地址。见 [src/transform/ascend_infer_buffer_scope.cc:764-803](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_infer_buffer_scope.cc#L764-L803)。布局本身是 u4-l4 的主题，这里只需知道「推断 L1 的同时会顺手补一个默认布局」。

**pass 的注册与 Python 包装**：C++ 端注册为 `tl.transform.InferAllocScope`，见 [src/transform/ascend_infer_buffer_scope.cc:897-906](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_infer_buffer_scope.cc#L897-L906)；Python 端 `tilelang/transform/__init__.py:472-481` 把它包装成 `AscendInferBufferScope()`。

#### 4.5.4 代码实践

1. **实践目标**：直观看到 `AscendInferBufferScope` 如何把 Developer 写法里悬置的 scope 推断成确定的物理 scope。
2. **操作步骤**：
   - 打开 [examples/developer_mode/gemm_developer.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/gemm_developer.py)，在 `func = matmul(...)` 之后加一行 `print(func.get_kernel_source())`。
   - 运行该脚本（需要配置好 CANN / ASCEND_HOME_PATH，参考 [u1-l2](./u1-l2-install-and-build.md)）。首次调用 `func(a, b)` 会触发 JIT 全链路编译。
   - 在打印出的 Ascend C 源码里，找到 `_kernel` 函数，观察 `A_L1` / `B_L1` / `C_L0` 三个 buffer 被声明成了哪类 `LocalTensor`。
3. **需要观察的现象**：
   - `A_L1` / `B_L1`（前端是 `alloc_shared`）应被声明为 L1 级别的局部张量（Cube 输入）。
   - `C_L0`（前端是 `alloc_fragment`，gemm 第 2 位）应被声明为 L0C 累加器级别。
4. **预期结果**：源码里三块缓冲的物理存储类别与「A/B 喂给 Cube→L1、C 是累加器→L0C」完全对应，而你在前端一行都没写 L1/L0C。这就是自动推断的威力。
5. 如果当前环境没有真实 NPU，编译会停在 bisheng 阶段而拿不到完整 `.so`，但 `get_kernel_source()` 产出的 **Ascend C 源码** 在 bisheng 编译之前就已生成，因此仍可用于本实践。若无 NPU，结果记为「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：如果把 Developer GEMM 里的 `C_L0 = T.alloc_fragment(...)` 改成 `C_L0 = T.alloc_shared(...)`，pass 会把它推断成什么？还能正确运行吗？

> **参考答案**：`C_L0` 仍是 gemm 的第 2 个 buffer，按 fragment 分支会推断到 L0C；但改成 `alloc_shared` 后走 shared 分支——它「used_in_cube」为真，会被推断成 `shared.l1`，而不是 L0C。Cube 累加器必须落在 L0C，否则 codegen 会因为 gemm 的输出缓冲 scope 不符而报错或行为异常。结论：累加器必须用 fragment，不能用 shared。

**练习 2**：为什么 `alloc_shared` 对 bool 类型强制改成 `"shared.ub"` 而不交给 pass 推断？

> **参考答案**：因为上游的 merge shared memory pass 当前无法合并 bool 类型缓冲（见 [allocate.py:42-45](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/allocate.py#L42-L45) 注释）。为避免合并 pass 出问题，bool 直接钉死在 UB（`shared.ub`），跳过 dynamic 推断。这是一个兼容性 hack。

## 5. 综合实践

本实践的题目与学习目标直接对应：**用 Developer 模式写一个 GEMM，用 `alloc_shared` 分配 A/B 的 L1 缓冲、用 `alloc_fragment` 分配 C 的累加器；再写一个 Expert 版本用 `alloc_L1` / `alloc_L0C` 显式指定，对比两者生成的 Ascend C 源码差异。**

**步骤**：

1. **Developer 版**：直接以 [examples/developer_mode/gemm_developer.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/gemm_developer.py) 为模板（它就是用 `alloc_shared` + `alloc_fragment`），把 M=N=K 设成 1024、`block_M=128, block_N=256, K_L1=64`，加上 `print(func.get_kernel_source())`。

2. **Expert 版**：参考 [examples/gemm/example_gemm_intrinsic.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic.py)。为了把对比变量控制好，先**忽略**它里面的 `set_flag`/`wait_flag`/`use_swizzle` 等高级流水优化，只关注分配部分（[example_gemm_intrinsic.py:51-56](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic.py#L51-L56)）：

   ```python
   A_L1 = T.alloc_L1((S1, block_M, K_L1), dtype)   # 显式 L1
   B_L1 = T.alloc_L1((S1, K_L1, block_N), dtype)
   A_L0 = T.alloc_L0A((S2, block_M, block_K), dtype)  # 显式 L0A
   B_L0 = T.alloc_L0B((S2, block_K, block_N), dtype)  # 显式 L0B
   C_L0 = T.alloc_L0C((block_M, block_N), accum_dtype) # 显式 L0C
   ```

3. **对比**：分别 `get_kernel_source()`，找两份源码里这些缓冲的 `LocalTensor<...>` 模板参数。

**需要观察与回答的问题**：

- (a) Developer 版里 `A_L1`/`B_L1`/`C_L0` 最终的物理 scope，与 Expert 版里 `alloc_L1`/`alloc_L0C` 直接指定的物理 scope，是否**一致**？（预期：一致。这说明 Developer 模式让编译器做的事，Expert 模式是让你自己写出来。）
- (b) Developer 版**没有**显式的 L0A/L0B 缓冲，而 Expert 版有 `A_L0`/`B_L0`。这是因为 Developer 的 `T.gemm_v0` 把「L1→L0A/L0B→MMA」这步内部封装了（共享输入直接进 Cube），Expert 的 `T.mma` 则要求你显式声明 L0A/L0B。这正是两种抽象的「控制粒度」差异。
- (c) Expert 版多了 `set_flag`/`wait_flag` 流水同步，Developer 版没有——因为 Developer 靠 `TL_ASCEND_AUTO_SYNC` 自动插入（[gemm_developer.py:21](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/gemm_developer.py#L21)）。同步是 u4 单元的主题，这里只需注意到这个差异。

**预期结果**：两份源码最终落到**同样的物理存储类别**，但 Developer 版源码更短、没有手写 scope 和 flag；Expert 版源码更长、所有层级和同步都写死。一句话总结：**Developer = 让 `AscendInferBufferScope` 和自动同步 pass 替你做决定；Expert = 你自己做所有决定**。若无 NPU 环境，源码对比部分可完成、`.so` 运行验证记为「待本地验证」。

## 6. 本讲小结

- tile-lang 把 Ascend 的五块片上存储压缩成两层前端抽象：`shared`（≈ shared memory，对应 L1/UB）和 `fragment`（≈ 寄存器/Tensor Core fragment，对应 L0A/L0B/L0C）。
- `T.alloc_shared(shape, dtype)` 分配 shared 层缓冲（默认 scope `"shared"`，dynamic）；`T.alloc_fragment(shape, dtype)` 分配 fragment 层缓冲（默认 `"local.fragment"`）；两者都是 `T.alloc_buffer` 的薄封装。
- `T.alloc_var(dtype, init=...)` 分配单元素标量变量（scope `"local.var"`），支持常量/表达式初值，常用于标志位、计数器、临时标量。
- TIR scope 到物理存储的映射：`shared`→L1/UB（待推断）、`shared.l1`→L1、`shared.ub`→UB、`wmma.matrix_a/b/accumulator`→L0A/L0B/L0C。
- `AscendInferBufferScope` pass（`tl.InferAllocScope`）在 `LowerAndLegalize` 最早期运行，按 buffer 的使用上下文把 dynamic scope 钉死：fragment 看 gemm 位置（A→L0A、B→L0B、C→L0C），shared 看是被 Cube 还是 Vector 用（L1 vs UB）。
- Developer 模式的价值就是：你只声明语义层（shared/fragment），物理位置交给编译器推断；Expert 模式（`alloc_L1`/`alloc_L0C` 等）则让你显式指定，换取更细的控制粒度。

## 7. 下一步学习建议

本讲解决了「缓冲声明在哪一层」。接下来：

- **[u3-l2](./u3-l2-data-copy.md) 数据搬运 T.copy 与原子写回**：缓冲声明好之后，数据如何在 GM / L1 / UB / L0A/L0B/L0C 各层之间搬运，以及 `T.tile.atomic_add` 的语义。这是与本讲最紧密的下一讲。
- **[u3-l3](./u3-l3-gemm-mma.md) 矩阵计算 gemm_v0 / mma**：本讲多次提到 `T.gemm_v0(A, B, C)` 决定了 C 落在 L0C，下一讲会详细讲 gemm 的参数与累加语义。
- **进阶**：想理解「Cube 和 Vector 共用一块 shared」这种本讲标记为 TODO 的跨核场景，请到 [u5-l1](./u5-l1-combine-cv.md)（CV 分离与 CombineCV）。想理解 L1 buffer 的 zN 布局，请到 [u4-l4](./u4-l4-layout-swizzle.md)。
