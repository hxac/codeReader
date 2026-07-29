# TritonToLinalg：TTIR 到 Linalg 算子转换

## 1. 本讲目标

本讲精读 Ascend 后端 ttir_to_linalg 流水线的「收官」pass——`triton-to-linalg`。在 u4-l1 里我们看到，这条流水线前面跑了一长串「预处理」pass（结构化、离散掩码、标量化、hivm/hfusion/llvm 等），它们都在为最后一步做准备；真正把 Triton 方言（`tt.*`）系统性地翻译成 Linalg / Memref / Tensor 方言的，正是本 pass。

学完本讲你应当能够：

- 说清 `triton-to-linalg` 这个 pass 的整体转换框架：类型转换器（TypeConverter）、合法性判定（Legalization）、转换模式注册（Conversion Patterns）三者如何协作。
- 解释 `LoadConverter` / `StoreConverter` 如何把 `tt.load` / `tt.store` 变成 `memref::CopyOp`、`bufferization::MaterializeInDestinationOp` 等「块搬运」操作。
- 解释 `ReduceConverter` / `MatmulConverter`（以及 `ScanConverter`、`ArgMax/ArgMin`）如何把 `tt.reduce` / `tt.dot` / `tt.scan` / `tl.argmax` 变成 `linalg::ReduceOp` / `linalg::MatmulOp` 等 Linalg 命名算子。
- 理解为什么 Linalg IR 是这条流水线的「终点产物」——它是后续 BiSheng 编译器的输入。

本讲依赖 u4-l1（你已经知道 `ttir_to_linalg` 函数在 `compiler.py` 里的位置和整体 pass 编排）。

## 2. 前置知识

阅读本讲前，你需要建立以下几个直觉。如果你对某个概念已经很熟，可以跳过。

**Triton 方言（tt dialect）与 Linalg 方言。** Triton 把 Python kernel 先翻译成一种「和硬件无关、但仍是 Triton 风格」的中间表示，称为 TTIR，里面的算子带 `tt.` 前缀，例如 `tt.load`、`tt.store`、`tt.dot`、`tt.reduce`、`tt.splat`。而 Linalg（Linear Algebra）是 MLIR 生态里一套更通用、更结构化的 dialect，算子带 `linalg.` 前缀，例如 `linalg.matmul`、`linalg.reduce`、`linalg.generic`、`linalg.fill`。Linalg 的特点是「显式声明循环结构（indexing_maps + iterator_types）」，非常适合后端做进一步Lowering。Triton-Ascend 选择把 TTIR 翻成 Linalg，是为了把「Triton 私有算子」收敛到「行业标准 IR」，从而交给华为的 BiSheng 编译器。

**指针 vs Memref。** Triton 里地址用 `tt.ptr<...>` 表示，它是一个「指向元素的指针」，可以做成张量 `tensor<MxN x !tt.ptr<f32>>`。而 MLIR 的 Memref（`memref<MxNxf32>`）是「一块带形状、带步长的内存」。两者模型不同：前者是「一堆指针」，后者是「一块连续内存」。所以转换的第一步，往往是把 `tt.ptr` 翻成 `memref`。本讲的 `TritonTypeConverter` 就干这件事。

**OpConversionPattern（算子转换模式）。** MLIR 的 Dialect Conversion 框架要求你为「每一种要翻译的源算子」写一个「转换模式」（pattern）。每个 pattern 有一个 `matchAndRewrite(op, adaptor, rewriter)` 方法：`op` 是源算子，`adaptor` 是「已经按类型转换器重写过的操作数」，`rewriter` 是用来构造新算子、删除旧算子的工具。框架会反复应用这些 pattern，直到所有算子都「合法」（legal）。

**合法性（Legality）与 applyPartialConversion。** 转换框架需要一个「目标」（ConversionTarget）来声明「什么样的算子算合法」。例如「`linalg` 方言合法」「带 `tt.ptr` 的算子非法」。`applyPartialConversion` 会不断尝试把「非法」算子用注册的 pattern 改写掉，直到没有非法算子为止。理解「pattern 负责改写、target 负责判定是否还要改」这个分工，是读本 pass 源码的钥匙。

如果你对 Linalg 的 `indexing_maps` / `iterator_types` 完全陌生，建议先记住一句话：**`linalg` 算子 = 一组输入输出张量 + 一段标量计算体（region）+ 描述「每个维度是 parallel 还是 reduction」的标注**。后面看到 `linalg.reduce`、`linalg.matmul` 时再对照体会。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [third_party/ascend/lib/TritonToLinalg/TritonToLinalgPass.cpp](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/TritonToLinalgPass.cpp) | pass 主体：`runOnOperation` 编排整个转换流程；`TritonTypeConverter` 做 ptr→memref 类型转换；`addDynamicLegal` 判定合法性；`populateTritonToLinalgConversionPatterns` 注册所有转换 pattern。 |
| [third_party/ascend/lib/TritonToLinalg/LoadStoreConverter.cpp](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/LoadStoreConverter.cpp) | `LoadConverter` / `StoreConverter` / `AtomicRMWConverter` / `AtomicCASConverter` 等：把 `tt.load` / `tt.store` / `tt.atomic_*` 翻成 memref 搬运与 linalg.generic。 |
| [third_party/ascend/lib/TritonToLinalg/TritonOpConverter.cpp](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/TritonOpConverter.cpp) | `ReduceConverter` / `ScanConverter` / `MatmulConverter` / `DotScaledConverter` 等：把归约、scan、矩阵乘等「计算类」算子翻成 linalg 命名算子。 |
| [third_party/ascend/include/TritonToLinalg/TritonOpConverter.h](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToLinalg/TritonOpConverter.h) | `ReductionOpBaseConverter` 模板基类：reduce/scan 共用的归约体分析、初值常量生成等逻辑。 |
| [third_party/ascend/include/TritonToLinalg/ArgMinMaxConverter.h](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToLinalg/ArgMinMaxConverter.h) | `ArgMinMaxBaseConverter` / `ArgMaxConverter` / `ArgMinConverter`：把 `tl.argmax` / `tl.argmin` 翻成「带索引的 linalg.reduce」。 |
| [third_party/ascend/backend/compiler.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py) | `ttir_to_linalg` 函数：在 pass 流水线里把本 pass 注册进去（`add_triton_to_linalg`）。 |

阅读建议：先看 Pass.cpp 的 `runOnOperation` 把全局流程看懂（4.1），再分别读 LoadStoreConverter.cpp（4.2）和 TritonOpConverter.cpp（4.3）。

---

## 4. 核心概念与源码讲解

### 4.1 triton-to-linalg：转换框架总览

#### 4.1.1 概念说明

`triton-to-linalg` 是一个标准的 MLIR「Dialect Conversion」pass。它的任务可以用一句话概括：**把模块里所有「仍然是 Triton 风格」的算子（`tt.load`、`tt.dot`、`tt.reduce`、`tt.ptr` 类型……），翻译成「Linalg / Memref / Tensor / arith」这套后端能消化的 IR**。

要做到这件事，MLIR 框架要求三件套配合：

1. **TypeConverter（类型转换器）**：声明「源类型 → 目标类型」的映射。本 pass 的核心是 `tt.ptr<T>` → `memref<?xT>`、`tensor<...x!tt.ptr<T>>` → `memref<...xT>`。只有类型先变，算子的操作数才能喂给后续的 pattern。
2. **ConversionTarget（合法性目标）**：声明哪些算子「已经合法，不要再动」，哪些「非法，必须被改写」。本 pass 把 `linalg`、`memref`、`arith`、`scf` 等方言标为合法，把「还带 `tt.ptr`」「还是 `tt.scan`」「作用在 tensor 上的 arith」等标为非法。
3. **Conversion Patterns（转换模式集合）**：为每一种非法算子提供一个改写方案。`populateTritonToLinalgConversionPatterns` 里注册了大约 40 个 pattern，覆盖 load/store/atomic/reduce/scan/matmul/broadcast/reshape……

最后由 `applyPartialConversion(module, target, patterns)` 反复应用 pattern，直到 IR 里没有非法算子。

#### 4.1.2 核心流程

`runOnOperation()` 的整体流程（已去掉与本讲主线无关的旁支）大致是：

```
1. 探测 kernel 是否含 tt.dot / tt.dot_scaled  -> existDot（决定 mix_mode）
2. 探测 kernel 是否含 SIMT 算子              -> existSIMTOp（决定 parallel_mode）
3. 预处理：tensor descriptor、implicit permute、strided load/store 改写
4. 跑 MarkTensorKindPass（给访存参数标 tensor_kind，供 profiling）
5. 跑一批 canonicalizer pattern（load/store/标量数学的规范化）
6. 跑 UseAnalysis（分析每个值的使用模式，辅助后续转换）
7. 构造 TritonTypeConverter + ConversionTarget（addDynamicLegal）
8. 注册所有转换 pattern（populateTritonToLinalgConversionPatterns）
9. 给每个 kernel 函数注入 program_id / num_programs 参数（addProgramInfo）
10. applyPartialConversion  ← 真正执行所有 pattern 改写
11. 转换函数 prologue/epilogue：tt.func -> func.func，写 mix_mode/parallel_mode 属性
12. 收尾：CSE + canonicalizer，计算 PointerCastOp 大小，插入 workspace/syncBlockLock 参数
```

其中第 10 步是「主力」，第 1、2、11 步则负责把「整个 kernel 的计算类别」以属性形式告诉后端（`mix_mode`、`parallel_mode`），这两件事会直接影响运行时分配多少物理核（见 u2-l2、u5-l3）。

> 小贴士：第 1、2 步的探测顺序很关键。源码里特意把 `existSIMTOp` 的探测放到「strided load/store 改写」之后，因为那一步才会「物化」出 `IndirectLoadOp`/`IndirectStoreOp`，提前探测会漏掉它们，导致 `parallel_mode` 被错判为 `simd`，最终运行时报错。

#### 4.1.3 源码精读

**类型转换器**——这是整个 pass 的地基。`tt.ptr` 与「元素是指针的张量」都要变成 memref：

[TritonToLinalgPass.cpp:188-221](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/TritonToLinalgPass.cpp#L188-L221) 定义 `TritonTypeConverter`。关键逻辑：把 `tt.ptr<T>` 映射为 `memref<?xT>`（一维动态大小）；把 `tensor<shape x !tt.ptr<T>>` 映射为 `memref<shape x T>`。还有一个细节：`ptr<i1>` / `tensor<i1>` 会被「提升」成 `i8`，因为布尔在内存里实际占 8 位，不能误导后端编译器。

**合法性判定**——声明「什么样的 IR 还需要继续改」：

[TritonToLinalgPass.cpp:511-595](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/TritonToLinalgPass.cpp#L511-L595) 的 `addDynamicLegal` 把 `linalg`、`memref`、`arith`、`scf`、`tensor`、`func` 等方言整体标为合法（[L513-L519](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/TritonToLinalgPass.cpp#L513-L519)）。其中两条「动态合法性」最值得注意：

- `tt.func` 只有当「函数签名里已经没有 `tt.ptr`」时才算合法（[L534-L536](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/TritonToLinalgPass.cpp#L534-L536)）——这驱动类型转换器去改写函数签名。
- 作用在 **tensor** 上的 `arith`/`math` 算子被标为非法（[L579-L594](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/TritonToLinalgPass.cpp#L579-L594)）：因为 Linalg 风格里，逐元素运算应当变成 `linalg` 命名算子（或 `linalg.generic`），而不是保留 tensor 上的 arith。当 `namedOps=true` 时这条会放宽（见下文「命名算子模式」）。

**pattern 注册**——这是「目录」，列出本 pass 能翻译哪些算子：

[TritonToLinalgPass.cpp:668-751](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/TritonToLinalgPass.cpp#L668-L751) 的 `populateTritonToLinalgConversionPatterns` 一次性注册了几乎所有 converter。你能在里面找到本讲三大主角：

- 访存类：`LoadStoreConverter::StoreConverter`、`LoadConverter`、`AtomicRMWConverter`、`AtomicCASConverter`（[L676-L683](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/TritonToLinalgPass.cpp#L676-L683)）
- 归约类：`ArgMinConverter`、`ArgMaxConverter`、`ReduceConverter`、`ScanConverter`（[L690-L693](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/TritonToLinalgPass.cpp#L690-L693)）
- 矩阵乘类：`MatmulConverter`、`DotScaledConverter`（[L721-L722](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/TritonToLinalgPass.cpp#L721-L722)）

末尾的 [L748-L750](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/TritonToLinalgPass.cpp#L748-L750) 还有个开关：当 `namedOps=false` 时，调用 MLIR 自带的 `populateElementwiseToLinalgConversionPatterns`，把所有「作用在 tensor 上的逐元素 arith/math」统一翻成 `linalg` 逐元素算子。这正是「动态合法性」里那条 tensor-arith 非法规则的对应改写方案。

> 「命名算子模式」(named_ops)：注意 `compiler.py` 在 ttadapter 阶段调用本 pass 时传的是 `named_ops=True`（见 [compiler.py:1275](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L1275)）。这意味着逐元素算子不会被强行翻成 linalg，而是保留为命名 arith/math 算子交给后端——后端（BiSheng）自己有更强的逐元素融合能力。所以你 dump 出来的 ttadapter IR 里，逐元素运算往往是 `arith.addf` 这类，而非 `linalg.generic`。

**全局编排**——把上面这些拼起来：

[TritonToLinalgPass.cpp:928-1091](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/TritonToLinalgPass.cpp#L928-L1091) 是 `runOnOperation` 的主干。其中 [L935-L944](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/TritonToLinalgPass.cpp#L935-L944) 探测 `existDot`（遍历找 `tt.dot` / `tt.dot_scaled`）；[L1088-L1091](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/TritonToLinalgPass.cpp#L1088-L1091) 执行 `applyPartialConversion`，这是真正驱动所有 pattern 跑起来的那一行。

**函数属性 mix_mode / parallel_mode**——转换最后，把探测结果写成属性：

[TritonToLinalgPass.cpp:468-484](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/TritonToLinalgPass.cpp#L468-L484) 在 `convertTTFunc` 里：若 kernel 含 `tt.dot`，则 `mix_mode = "mix"`（需要 Cube 核参与），否则 `"aiv"`（纯向量）；若含 SIMT 算子，则 `parallel_mode = "mix_simd_simt"`，否则 `"simd"`。这两个属性会随 IR 一路传到运行时，决定 launcher 如何分配物理核与 localMemory（见 u5-l3）。这也是为什么第 1、2 步的探测必须准确。

#### 4.1.4 代码实践

**实践目标**：在命令行层面确认本 pass 确实是「TTIR → Linalg」的那一步，并理解它产出的 `ttadapter.mlir` 就是 Linalg IR。

**操作步骤**：

1. 准备好环境（见 u1-l3）后，进入 tutorials 目录：
   ```bash
   cd third_party/ascend/tutorials
   export TRITON_DEBUG=1
   python 01-vector-add.py
   ```
2. 编译完成后，打开 dump 目录（通常在 `~/.triton/dump/<某 hash>/`），找到两个文件：`kernel.ttir.mlir`（本 pass 的**输入**）和 `kernel.ttadapter.mlir`（本 pass 的**输出**）。
3. 用文本编辑器或 `grep` 对比两个文件：
   ```bash
   grep -c "tt\."  kernel.ttir.mlir       # 输入里 tt. 算子的数量
   grep -c "tt\."  kernel.ttadapter.mlir  # 输出里 tt. 算子的数量
   grep -n "memref\|linalg" kernel.ttadapter.mlir
   ```

**需要观察的现象**：

- `ttir.mlir` 里能搜到 `tt.load`、`tt.store`、`tt.func`、`tt.make_range` 等 `tt.` 算子。
- `ttadapter.mlir` 里几乎不再有 `tt.` 算子，取而代之的是 `memref.`、`func.func`、`arith.`、`linalg.` 等。

**预期结果**：`ttadapter.mlir` 中的 `tt.` 算子计数应大幅减少甚至归零（少数 `tt.splat` 等可能保留给后续处理），证明本 pass 完成了「方言切换」。

**待本地验证**：不同 kernel、不同版本下，`ttadapter.mlir` 里残留的 `tt.` 算子种类可能略有差异，以你本机 dump 为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `addDynamicLegal` 要把「作用在 tensor 上的 arith」标为非法，而「作用在 scalar 上的 arith」标为合法？

**参考答案**：因为 Linalg 模型要求「张量级计算」用 `linalg` 算子表达（显式 indexing_maps / iterator_types），后端才能做结构化 Lowering；标量 arith 已经是终态，无需再翻。把 tensor-arith 标非法，正是为了驱动框架用 `populateElementwiseToLinalgConversionPatterns` 把它们翻成 linalg（或当 `named_ops=True` 时放宽，留给后端融合）。

**练习 2**：`TritonTypeConverter` 为什么要把 `ptr<i1>` 变成 `memref<?xi8>` 而不是 `memref<?xi1>`？

**参考答案**：因为硬件内存的最小可寻址单位是字节（8 bit），布尔值在内存里实际占用 8 位。若保留 `i1`，会误导后端编译器对内存布局的判断，所以统一提升为 `i8`。

---

### 4.2 LoadConverter / StoreConverter：访存的 Linalg/Memref 化

#### 4.2.1 概念说明

`tt.load` / `tt.store` 在 TTIR 里长这样：给一个「指针张量」、一个「掩码」、一个「越界填充值 other」，把数据搬进/搬出。它有四个变体要处理：

- 无 mask 的连续访存（最简单，vector-add 就是这种）。
- 带「连续矩形 mask」的访存（softmax 里 `mask = offs < N` 这种）。
- 带「边界检查」`boundary_check` 的访存（`make_tensor_ptr` 风格）。
- 标量访存（指针不是张量）。

转换的核心思想是：**把「Triton 的指针语义」降级成「memref 的内存搬运」**。具体来说：

- `tt.load` → 分配一块局部 memref（`memref::AllocOp`），用 `memref::CopyOp` 把全局内存拷进来，再用 `bufferization::ToTensorOp` 把它「包装」回 tensor，喂给后续的 tensor 计算。
- `tt.store` → 反过来，用 `bufferization::MaterializeInDestinationOp` 把一个 tensor「物化」写进目标 memref。

mask 怎么处理？思路是：先用 mask 分析出「真正有效的子区域」（一个 SubView / ExtractSlice），只搬运这个子区域；需要 `other` 填充时，先 `linalg.fill` 把整块填上 other，再搬运有效部分。

#### 4.2.2 核心流程

以无 mask 的 `tt.load` 为例，转换流程是：

```
tt.load %ptr : tensor<Mx!tt.ptr<f32>>  →  tensor<Mxf32>
        ↓  (LoadConverter)
1. %ptr 已经被 TypeConverter 变成 memref<Mxf32>
2. %alloc = memref.alloc : memref<Mxf32>          # 分配局部缓冲
3. memref.copy %ptr, %alloc                        # 全局 → 局部
4. %t = bufferization.to_tensor %alloc (writable)  # 包装回 tensor
5. 用 %t 替换 tt.load 的结果
```

带 mask 时多两步：用 `MaskState` 解析 mask 得到「有效子区域」，对源 memref 和目标 alloc 各取一个 SubView，再 `memref.copy` 子区域；若带 `other`，先 `linalg.fill` 填充。

`tt.store` 的流程对称：无 mask 时直接 `bufferization.materialize_in_destination %val into %ptr`；带 mask 时对 val 取 ExtractSlice、对 ptr 取 SubView，再 materialize。

#### 4.2.3 源码精读

**LoadConverter 主体**——最经典的「alloc + copy + to_tensor」三连：

[LoadStoreConverter.cpp:230-545](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/LoadStoreConverter.cpp#L230-L545) 是 `LoadConverter::matchAndRewrite`。无 mask 分支在 [L436-L471](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/LoadStoreConverter.cpp#L436-L471)，核心两句：

```cpp
// LoadStoreConverter.cpp:452
auto copyOp = rewriter.create<memref::CopyOp>(loc, ptr, allocOp);
```

这行把「全局 memref `ptr`」整块拷贝到「局部 `allocOp`」。随后调用 `toTensorAndReplace`：

[LoadStoreConverter.cpp:87-103](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/LoadStoreConverter.cpp#L87-L103) 的 `toTensorAndReplace` 用 `bufferization::ToTensorOp` 把局部 memref 包装成 tensor（`restrict writable` 表示这块 buffer 是独占、可写的），再用它替换原 `tt.load`。这正是「tt.load 结果 = 一个 tensor」的来源。

> 旁支（了解即可）：[L316-L358](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/LoadStoreConverter.cpp#L316-L358) 处理带 `discreteAttrName` 属性的 load——这是 u4-l4 里 `triton-to-unstructure` 把离散访存标量循环化后留下的特殊形态，本 pass 要把它「重新聚合」成一个 memref。此外 [L445-L451](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/LoadStoreConverter.cpp#L445-L451) 还有一个「去交错（deinterleave）」优化：当最后一维 stride==2 时，尝试把 load 合并成一次更宽的访存。

**StoreConverter 主体**——`materialize_in_destination` 是主角：

[LoadStoreConverter.cpp:1096-1180](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/LoadStoreConverter.cpp#L1096-L1180) 是 `StoreConverter::matchAndRewrite`。无 mask 分支非常短（[L1155-L1161](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/LoadStoreConverter.cpp#L1155-L1161)）：

```cpp
// LoadStoreConverter.cpp:1156-1158
auto storeOp = rewriter.create<bufferization::MaterializeInDestinationOp>(
    loc, val, ptr);
storeOp.setWritable(true);
```

`materialize_in_destination %val into %ptr` 的语义就是「把 tensor `val` 写到 memref `ptr` 里」，正好对应 `tt.store`。带连续 mask 时（[L1163-L1179](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/LoadStoreConverter.cpp#L1163-L1179)），先用 `MaskState.getExtractSlice` / `getSubview` 取出 val 与 ptr 的有效子区域，再 materialize。

**带边界检查的访存**（`make_tensor_ptr` 风格）：

[LoadStoreConverter.cpp:366-434](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/LoadStoreConverter.cpp#L366-L434) 处理 `boundary_check`：计算出每维的「有效大小」`boundarySizes`，分别对源、目标取 SubView（[L419-L423](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/LoadStoreConverter.cpp#L419-L423)），再做 `memref::CopyOp`。若声明了 padding（`PAD_NAN` / `PAD_ZERO`），先用 `fillTensorWithOtherForMaskScenario` 把整块填上填充值（[L416-L418](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/LoadStoreConverter.cpp#L416-L418)），再覆盖有效部分——这正是 Triton 里 `padding_option=` 的实现。

**原子操作**（atomic）——翻成 `linalg.generic` 或 HIVM/HFusion 原子算子：

[LoadStoreConverter.cpp:574-734](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/LoadStoreConverter.cpp#L574-L734) 的 `AtomicRMWConverter` 把 `tt.atomic_rmw` 翻译成带 `GenericAtomicRMW` 属性的 `linalg.generic`（文件顶部 [L550-L573](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/LoadStoreConverter.cpp#L550-L573) 的注释给出了一模一样的输入输出示例）。值得注意的是，它会按「硬件是否支持该 atomic 类型」分流：硬件支持的（如 f16/f32 的 ADD/MAX/MIN）走 `hivm.store`，否则走 `hfusion.atomic_rmw`/`hfusion.store` 的软件模拟（[L699-L727](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/LoadStoreConverter.cpp#L699-L727)）。`AtomicCASConverter`（[L736-L873](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/LoadStoreConverter.cpp#L736-L873)）则把 compare-and-swap 翻成一段含 `scf.if` 的 `linalg.generic`，并打上 `Software` 属性表示用软件模拟。

#### 4.2.4 代码实践

**实践目标**：在真实 dump 里看到 `tt.load` / `tt.store` 变成 `memref.copy` / `bufferization.materialize_in_destination`。

**操作步骤**：

1. 仍然在 `TRITON_DEBUG=1` 下运行 `01-vector-add.py`。
2. 打开 dump 目录里的 `kernel.ttadapter.mlir`。
3. 搜索 `memref.copy` 和 `materialize_in_destination`：
   ```bash
   grep -n "memref.copy\|materialize_in_destination\|memref.alloc" kernel.ttadapter.mlir
   ```
4. 对照 `kernel.ttir.mlir` 里原来的 `tt.load` / `tt.store`，数一数它们的对应关系。

**需要观察的现象**：

- 每个 `tt.load` 大致对应一个 `memref.alloc` + `memref.copy` + `bufferization.to_tensor`。
- 每个 `tt.store` 大致对应一个 `bufferization.materialize_in_destination`。

**预期结果**：vector-add 是无 mask 访存，所以走的就是上面最简洁的「alloc+copy+to_tensor」与「materialize」路径，与 4.2.2 的流程图一一吻合。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `LoadConverter` 要先 `memref.alloc` 一块局部缓冲，再 `memref.copy`，而不是直接用全局 memref？

**参考答案**：因为后续计算消费的是 **tensor**（Linalg 风格），而 tensor 需要一块「独占可写」的内存作为载体。`bufferization.to_tensor` 要求底层 memref 是可写的；全局输入 memref 是只读的、且可能被多个 program 共享，不能直接拿来当 tensor 的 buffer。所以先 alloc 一块局部缓冲，把数据拷进来，再包装成 tensor。

**练习 2**：`tt.store` 带 mask 时，为什么对 val 用 `ExtractSlice`、对 ptr 用 `SubView`，而不是两边都用同一种？

**参考答案**：因为 val 是 **tensor**（值语义），ptr 是 **memref**（引用语义）。对 tensor 取子区域要用 `tensor.extract_slice`（产生新 tensor），对 memref 取子区域要用 `memref.subview`（产生指向原 buffer 的视图）。`MaterializeInDestinationOp` 正好接收「一个 tensor 源 + 一个 memref 目标」，所以两侧用不同的子区域算子。

---

### 4.3 ReduceConverter / MatmulConverter：归约与矩阵乘的 Linalg 命名算子化

#### 4.3.1 概念说明

「计算类」算子（reduce、scan、argmax、matmul）的转换思路和访存不同：它们要变成 **Linalg 的命名算子**（named op），因为命名算子携带了明确的语义，后端可以识别并映射到昇腾的 Cube/Vector 指令。

先建立 Linalg 命名算子的直觉。一个 `linalg.reduce` 大致长这样：

```
%init = linalg.fill ... : tensor<...>     # 用归约初值填充
%out = linalg.reduce ins(%src : tensor<MxNxf32>) outs(%init : tensor<Mxf32>)
       dimensions = [1]                    # 沿第 1 维归约
  ^bb0(%a, %b):                           # 标量归约体
    %r = arith.addf %a, %b : f32
    linalg.yield %r : f32
```

它有三个关键部分：输入、输出（初值）、一段标量归约体。归约的数学含义是：

\[
\text{out}[i] = a \oplus a \oplus \dots \oplus a \quad\text{沿归约维累计}
\]

其中 \(\oplus\) 是归约算子（add/max/min/and/...），\(a\) 是该算子的「幺元」（add 是 0，mul 是 1，max 是 \(-\infty\)）——这个幺元就是 `linalg.fill` 的填充值。

类似地：

- `linalg.matmul` = `C[i,j] += A[i,k] * B[k,j]`，自带最内维归约（即「乘加」），对应 `tt.dot`。
- `linalg.batch_matmul` = 批量版的 matmul，对应 3D 的 `tt.dot`。

`ReduceConverter` / `MatmulConverter` / `ScanConverter` / `ArgMinMax` 的任务，就是把 `tt.reduce` / `tt.dot` / `tt.scan` / `tl.argmax` 翻译成上面这些命名算子，并**正确生成归约初值**。

#### 4.3.2 核心流程

**ReduceConverter 的分流（模板基类决定）**：

[triton-to-linalg] 对 `tt.reduce` 的处理由模板基类 `ReductionOpBaseConverter` 统一入口，先分析「归约体里有几个真实算子」：

```
matchAndRewrite(reduce):
  realReductionOps = getRealReductionOps(reduce)   # 过滤掉纯类型转换 op
  if realReductionOps.size() == 1:
      convertToTargetOp(...)        # 单算子归约：直接用 arith op 构造 linalg.reduce 体
  else:
      convertToTargetOpExtended(...) # 多算子归约：把整个 tt.reduce 体 clone 进 linalg.reduce
```

「真实算子」很关键：Triton 的 reduce 体里可能掺着 `extf`/`truncf`/`bitcast` 这类**纯类型转换**（例如 bf16→f32→bf16 的精度提升），它们不算「真正的归约计算」，分析时会被过滤掉。

**单算子归约**（`convertToTargetOp`）流程：

```
1. rop = 真实归约算子（如 arith.addf）
2. baseConst = getReductionBaseConstOp(rop)   # 按 rop 选幺元：add→0, mul→1, max→-∞
3. initTensor = linalg.fill(baseConst)        # 用幺元填初值
4. linalg.reduce ins(source) outs(initTensor) dimensions=[axis]:
     yield rop(lhs, rhs)                      # 体里直接放对应 arith op
5. 若是 1D 向量归约，结果再 tensor.extract 成标量
```

**MatmulConverter 流程**：

```
1. elemTy = 结果元素类型
2. if 浮点且不是 f32（如 bf16/f16）:
     # 先把累加器 C 提升到 f32，做 f32 matmul，再截断回 bf16，并标注 round_mode=RINT
     C_fp32 = extf C
     linalg.matmul A,B -> C_fp32
     truncf C_fp32 -> C  (round_mode=RINT)
   else:
     linalg.matmul A,B -> C      # 2D
     # 或 linalg.batch_matmul      # 3D
3. 给 matmul 打上 input_precision 属性（对应 tl.dot 的 input_precision 参数）
```

为什么 bf16 要绕一圈 f32？因为昇腾 Cube 单元做低精度矩阵乘时，累加在 f32 里做精度更高，最后再按确定舍入模式（RINT）截回低精度，避免精度损失。

#### 4.3.3 源码精读

**归约基类——统一入口与「真实算子」分析**：

[TritonOpConverter.h:248-264](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToLinalg/TritonOpConverter.h#L248-L264) 是 `ReductionOpBaseConverter::matchAndRewrite` 的 `final` 入口：调 `getRealReductionOps`，按数量分流到 `convertToTargetOp` / `convertToTargetOpExtended`。

[TritonOpConverter.h:273-303](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToLinalg/TritonOpConverter.h#L273-L303) 的 `getRealReductionOps` 值得细看：它从 yield 的操作数「反向」追溯（worklist），只保留真正参与结果计算的 op（[L280-L290](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToLinalg/TritonOpConverter.h#L280-L290)），再排除 `extf/truncf/bitcast`（[L298-L301](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToLinalg/TritonOpConverter.h#L298-L301)）。这保证了「单算子 vs 多算子」的判定不被类型转换 op 干扰。

**归约幺元——按算子类型选初值**：

[TritonOpConverter.h:325-384](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToLinalg/TritonOpConverter.h#L325-L384) 的 `getReductionBaseConstOp` 用 `TypeSwitch` 把每种归约算子映射到它的幺元常量：`AddF→0.f`、`MulF→1.f`、`MaximumF→-∞`、`MinimumF→+∞`、`AndI→全1`、`OrI→0`……这正是上面数学公式里 \(a\)（幺元）的来源。

**ReduceConverter 单算子路径**：

[TritonOpConverter.cpp:1080-1157](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/TritonOpConverter.cpp#L1080-L1157) 的 `convertToTargetOp`。先校验算子是否受支持（`isReductionOpSupported`，[L998-L1003](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/TritonOpConverter.cpp#L998-L1003)），再生成初值与 `linalg.reduce`：

[TritonOpConverter.cpp:1136-1148](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/TritonOpConverter.cpp#L1136-L1148) 是核心构造：

```cpp
Value finalResult =
    rewriter.create<linalg::ReduceOp>(
        loc, ValueRange{source}, ValueRange{initTensor},
        SmallVector<int64_t>{axis},
        [&](OpBuilder &opBuilder, Location loc, ValueRange inputs) {
          Value result = this->computeReduceResultWithCompileFlag(...);
          opBuilder.create<linalg::YieldOp>(loc, result);
        }).getResult(0);
```

这就是「source 进、initTensor 出、沿 axis 归约、体里放归约算子」的标准 `linalg.reduce`。若源是 1D（向量归约），结果再用 `tensor.extract` 退成标量（[L1150-L1153](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/TritonOpConverter.cpp#L1150-L1153)）。多算子路径 `convertToTargetOpExtended`（[L1159-L1216](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/TritonOpConverter.cpp#L1159-L1216)）则把整个 `tt.reduce` 体 clone 进 `linalg.reduce`，保留多个算子。

**ScanConverter（前缀归约，对应 tl.associative_scan）**：

[TritonOpConverter.cpp:1218-1660](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/TritonOpConverter.cpp#L1218-L1660) 的 `ScanConverter` 把 `tt.scan` 翻成 `linalg` 的前缀扫描（cumsum 等）。注意 pass 主体里有个特殊判定 `isSimt1DCumsum`（[TritonToLinalgPass.cpp:120-147](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/TritonToLinalgPass.cpp#L120-L147)）：只有「单 add 的 cumsum 且可塌缩成 1D」才会被标记走 SIMT 模板，其余 scan 留在 SIMD——这会反过来影响前面 4.1 提到的 `parallel_mode` 判定。

**MatmulConverter——dot 到 linalg.matmul**：

[TritonOpConverter.cpp:2183-2231](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/TritonOpConverter.cpp#L2183-L2231) 的 `MatmulConverter::matchAndRewrite`。它按结果秩选 `linalg.matmul`（2D）或 `linalg.batch_matmul`（3D）（[L2194-L2200](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/TritonOpConverter.cpp#L2194-L2200)）；低精度分支（[L2215-L2224](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/TritonOpConverter.cpp#L2215-L2224)）先 `extf` 到 f32 做 matmul，再 `truncf` 回原精度并打 `round_mode=RINT`：

```cpp
// TritonOpConverter.cpp:2195
return rewriter.template create<linalg::MatmulOp>(op.getLoc(), operands, results);
```

最后给 matmul 设置 `input_precision` 属性（[L2228-L2229](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/TritonOpConverter.cpp#L2228-L2229)），对应 `tl.dot(..., input_precision=...)`。

**ArgMax / ArgMin——「带索引」的归约**：

[ArgMinMaxConverter.h:274-294](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToLinalg/ArgMinMaxConverter.h#L274-L294) 的 `ArgMinMaxBaseConverter` 把 `tl.argmax`/`tl.argmin` 翻成一个 **双输出** 的 `linalg.reduce`：一个输出归约值（max/min），一个输出对应的索引。它会构造两个初值张量——值用 `-∞`（argmax）/`+∞`（argmin），索引用一个大整数或 `-1`（取决于 `tie_break`，[L254-L262](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToLinalg/ArgMinMaxConverter.h#L254-L262)），然后把原 reduce 体 clone 进去，并打上 `ReduceWithIndex` 属性告诉后端「这是个带索引的归约」（[L299-L302](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToLinalg/ArgMinMaxConverter.h#L299-L302)）。`ArgMaxConverter`/`ArgMinConverter`（[ArgMinMaxConverter.h:319-349](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToLinalg/ArgMinMaxConverter.h#L319-L349)）只是提供「比较方向」与「幺元」的特化（实现见 [ArgMinMaxConverter.cpp:62-111](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/ArgMinMaxConverter.cpp#L62-L111)）。

#### 4.3.4 代码实践

**实践目标**：选择 `dot` 和 `reduce` 两个算子，在 dump 出的 Linalg IR 里找到它们对应的 lowering 结果并解释。

**操作步骤**：

1. 在 `TRITON_DEBUG=1` 下分别运行两个教程：
   ```bash
   export TRITON_DEBUG=1
   python 03-matrix-multiplication.py   # 含 tl.dot
   python 02-fused-softmax.py           # 含 tl.max / tl.sum（reduce）
   ```
2. 在各自 dump 目录找到 `kernel.ttadapter.mlir`。
3. 对 matmul，搜索 `linalg.matmul`：
   ```bash
   grep -n "linalg.matmul\|linalg.batch_matmul\|input_precision\|round_mode" <matmul dump>/kernel.ttadapter.mlir
   ```
4. 对 softmax，搜索 `linalg.reduce` 与 `linalg.fill`：
   ```bash
   grep -n "linalg.reduce\|linalg.fill\|linalg.yield" <softmax dump>/kernel.ttadapter.mlir
   ```
5. 对照 `kernel.ttir.mlir` 里原来的 `tt.dot` / `tt.reduce`，确认一一对应。

**需要观察的现象**：

- matmul 的 `tt.dot`（2D）变成 `linalg.matmul`；若你改用 grouped/batched 形态，会看到 `linalg.batch_matmul`。低精度（如 bf16）时，`linalg.matmul` 会被 `arith.extf` / `arith.truncf` 夹住，并能搜到 `round_mode` 属性。
- softmax 的 `tl.max` 变成一个 `linalg.reduce`（归约体里是 `arith.maximumf` 之类），前面带一个用 `-∞` 填充的 `linalg.fill`；`tl.sum` 同理变成体里是 `arith.addf` 的 `linalg.reduce`。

**预期结果**：

- 你能在 `ttadapter.mlir` 中清楚指认：`tt.dot → linalg.matmul`，`tt.reduce → linalg.reduce + linalg.fill`，并能解释 linalg.fill 里的常量为何是该归约算子的幺元。

**待本地验证**：softmax 教程若开启了某些优化，部分 reduce 可能在更早的 pass 被改写或融合，dump 里看到的具体形态以本机为准；若想看最「干净」的 reduce，可临时写一个只含 `tl.sum` 的最小 kernel。

#### 4.3.5 小练习与答案

**练习 1**：`tt.reduce` 的归约体里包含一个 `arith.extf`（bf16→f32）和一个 `arith.addf`。`getRealReductionOps` 会返回几个 op？走 `convertToTargetOp` 还是 `convertToTargetOpExtended`？

**参考答案**：返回 1 个 op（`arith.addf`）。因为 `extf` 属于被排除的「纯类型转换」op，不计入「真实归约算子」。因此 `realReductionOps.size() == 1`，走单算子路径 `convertToTargetOp`。

**练习 2**：为什么 `MatmulConverter` 对 bf16 的 `tt.dot` 要先 `extf` 到 f32 再 `truncf` 回来？这个 `round_mode=RINT` 又是干什么用的？

**参考答案**：昇腾 Cube 单元在 f32 累加器里做乘加精度更高，低精度直接累加会有精度损失，所以先把累加器 C 提升到 f32、做 f32 matmul、再截断回 bf16。`round_mode=RINT` 指定截断时用「就近偶数舍入（round to nearest even）」，保证舍入行为确定且与前端语义一致，避免不同实现间出现 1 ULP 的差异。

**练习 3**：`ArgMaxConverter` 生成的 `linalg.reduce` 有几个输出？分别是什么？

**参考答案**：2 个输出。一个是「归约值」（最大值本身，初值 `-∞`），另一个是「索引」（最大值所在位置，初值依 `tie_break` 取一个大整数或 `-1`），并附带 `ReduceWithIndex` 属性告诉后端这是带索引的归约。

---

## 5. 综合实践

**任务**：写一个最小的 Triton kernel，同时包含 `tl.dot` 与 `tl.sum`（或 `tl.max`），编译后在 `ttadapter.mlir` 里「逐行解释」这两个算子的 lowering 链路。

建议步骤：

1. 参照 `tutorials/03-matrix-multiplication.py`，把 kernel 简化为：对一个 `[M, K]` 的矩阵 A 做 `tl.dot(A, A_T)` 得到 `[M, M]`，再对结果沿某一维 `tl.sum` 得到 `[M]`。注意保持 BLOCK_SIZE 为 constexpr。
2. 用 `@triton.jit` 装饰，写好 grid 与 `tl.load`/`tl.store`。
3. 在 `TRITON_DEBUG=1` 下运行，打开 `kernel.ttir.mlir` 与 `kernel.ttadapter.mlir`。
4. 画一张对照表：

   | 源算子（ttir） | 产物算子（ttadapter） | 关键辅助算子 | 你的解释 |
   |---|---|---|---|
   | `tt.dot` | `linalg.matmul` | （bf16 时）`extf`/`truncf` + `round_mode` | 矩阵乘 + 最内维归约 |
   | `tt.reduce`(sum) | `linalg.reduce` | `linalg.fill`(0) | 沿指定维加法归约 |
   | `tt.load` | `memref.copy` + `bufferization.to_tensor` | `memref.alloc` | 全局→局部→tensor |
   | `tt.store` | `bufferization.materialize_in_destination` | — | tensor→目标 memref |

5. 验证你的解释：修改 `tl.dot` 的 `input_precision` 或把数据类型改成 bf16，观察 `ttadapter.mlir` 里 `input_precision` 属性与 `round_mode` 是否如期出现/变化。

**验收标准**：你能指着 `ttadapter.mlir` 的每一行，说清它来自哪个 `tt.` 算子、为什么多出那些辅助算子（alloc/fill/extf/truncf）。若某些产物无法解释，记下来作为后续学习 u4-l6（其他 lowering pass）与 u8（CV 流水线）的切入点。

## 6. 本讲小结

- `triton-to-linalg` 是 ttir_to_linalg 流水线的「收官」pass，用 MLIR Dialect Conversion 框架（TypeConverter + ConversionTarget + Patterns）把 `tt.*` 系统性翻译成 `linalg`/`memref`/`tensor`/`arith`。
- `TritonTypeConverter` 是地基：`tt.ptr<T>` → `memref<?xT>`，`tensor<...x!tt.ptr<T>>` → `memref<...xT>`，`ptr<i1>` 提升为 `i8`。
- `LoadConverter`/`StoreConverter` 把访存降级为「alloc + `memref.copy` + `to_tensor`」与「`materialize_in_destination`」；mask 变成 SubView/ExtractSlice，padding 变成 `linalg.fill`；atomic 翻成带属性标记的 `linalg.generic` 或 HIVM/HFusion 原子算子。
- `ReduceConverter`/`ScanConverter`/`ArgMinMax` 把归约类算子翻成 `linalg.reduce`（双输出 + `ReduceWithIndex` 用于 argmax/argmin），关键是用 `getRealReductionOps` 分析归约体、用 `getReductionBaseConstOp` 选对幺元。
- `MatmulConverter` 把 `tt.dot` 翻成 `linalg.matmul`/`linalg.batch_matmul`，低精度时绕道 f32 累加并标注 `round_mode=RINT`，并保留 `input_precision`。
- pass 末尾还会给 `func.func` 写入 `mix_mode`（是否含 dot）与 `parallel_mode`（是否含 SIMT 算子）属性，直接影响运行时的物理核分配。
- 产物 Linalg IR 是后续 BiSheng 编译器的输入，本 pass 是「Triton 世界」到「Ascend 编译世界」的边界。

## 7. 下一步学习建议

- **u4-l6（其他 lowering pass）**：本讲聚焦 `triton-to-linalg`，但 ttir_to_linalg 流水线里还有 `triton-to-annotation`、`triton-to-hivm`、`triton-to-hfusion`、`triton-to-llvm` 等 pass，它们负责把编译提示、跨核同步、直方图、内联汇编映射到 Ascend 专用方言，建议接着读。
- **u8（Cube-Vector 融合与流水线）**：本讲看到 `linalg.matmul`/`linalg.reduce` 后，后端如何把它们调度到 Cube/Vector 双单元、如何做流水线，是 u8 的主题；`mix_mode`/`parallel_mode` 属性会在那里被消费。
- **u5（运行时驱动）**：本讲写入的 `mix_mode`/`parallel_mode`、`syncBlockLock`/`workspace` 参数，会在 u5 的 launcher 里被读取并用于 `rtKernelLaunch`，可以把两讲对照阅读。
- **源码延伸**：如果想深入「归约」这条线，建议读 [TritonOpConverter.h:243-429](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToLinalg/TritonOpConverter.h#L243-L429) 的 `ReductionOpBaseConverter` 全貌，以及 [TritonToLinalgPass.cpp:668-751](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/TritonToLinalgPass.cpp#L668-L751) 里你还没细看的 converter（如 `DotScaledConverter`、`GatherConverter`、`HistogramConverter`），它们覆盖了更专门的算子。
