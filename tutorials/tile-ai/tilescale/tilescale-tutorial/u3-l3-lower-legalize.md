# LowerAndLegalize：前端 Tile IR 合法化

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `LowerAndLegalize` 这一编译阶段在整条编译链路中的位置，以及它和 `PreLowerSemanticCheck`、`OptimizeForTarget` 的分工。
- 逐 pass 说出合法化阶段做了哪些事：绑定目标、负索引合法化、注入假设、表达式化简、reducer 预布局、fragment/shared 布局推理、高层 tile op 降级、安全访存合法化。
- 理解 `LayoutInference` 如何为 `fragment`/`shared` 缓冲区推导「线程级布局」（即每个寄存器/线程负责哪些元素），以及它为何是后续 `mma`/`wgmma`/`TMA` 指令能正确工作的前提。
- 理解 `LowerTileOp` 如何把前端 `tl.tileop.copy`/`tl.tileop.gemm` 等 intrin 降级成具体的硬件指令调用。
- 通过开关 `pass_config`（如 `tl.disable_tma_lower`）并对比 lower 后的 IR，亲手验证某个 pass 改变了什么。

本讲只讲「合法化阶段」本身，不展开 `OptimizeForTarget` 里的软件流水、warp 特化（那是 u3-l4 的内容），也不展开单个 pass 的全部算法细节（那是 u4 的内容）。

## 2. 前置知识

承接 [u3-l1 编译总览](u3-l1-compile-overview.md)：`tilelang.lower` 是编译器主入口，编译分三大阶段——`PreLowerSemanticCheck`（只校验不改 IR）→ `LowerAndLegalize`（本讲主题）→ `OptimizeForTarget`（目标相关优化）。`LowerAndLegalize` 的输入是一个 `IRModule`，里面是前端解析得到的、仍带有高层 tile op 的 `PrimFunc`。

你还需要记住以下两个关键认知（来自 [u3-l2 前端解析](u3-l2-frontend-tir.md)）：

- 前端阶段，所有 `T.*` 计算原语都还只是**待降级的高层 intrin**。例如 `T.copy` 对应 `tl.tileop.copy` 的 `tir.call_intrin`，`T.gemm` 对应 `tl.tileop.gemm`。真正的 TMA、`mma` 等硬件指令**此刻还没有出现**，要等本讲的 `LowerTileOp` 把它们生成出来。
- `fragment`（`alloc_fragment` 分配的缓冲区）不是连续数组，而是**元素打散分布在 warp 的各线程寄存器上**、专门对接 tensor core 的存储层级。每个 fragment 具体怎么分布到线程上（线程级布局），编译器需要去「推理」，这正是 `LayoutInference` 的核心职责。

本讲还会用到几个 TIR 基础概念：

- **PrimFunc**：TVM 的 IR 函数，由参数、buffer_map、语句树（Stmt）构成。
- **pass**：流水线里的一道变换工序，输入一个 `IRModule`，输出一个新的 `IRModule`。
- **PassContext**：编译时的全局上下文，携带一组 `config`（旋钮）控制各 pass 的行为，例如 `tl.disable_tma_lower`。
- **Target**：目标设备描述（如 CUDA + 某个 SM 架构），下游 pass 会读取它来决定走哪条代码路径。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tilelang/engine/phase.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py) | 定义三大阶段的 pass 编排；本讲的 `LowerAndLegalize` 就在这里逐 pass 串起来。 |
| [tilelang/engine/lower.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/lower.py) | 编译主入口，调用 `PreLowerSemanticCheck` → `LowerAndLegalize` → `OptimizeForTarget`，再做 host/device 拆分与 codegen。 |
| [src/transform/legalize_negative_index.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/legalize_negative_index.cc) | 负索引合法化 pass 的 C++ 实现。 |
| [src/transform/inject_assumes.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_assumes.cc) | 注入「假设」约束（buffer 形状 > 0 等），加速 TVM 的符号证明器。 |
| [src/transform/layout_inference.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/layout_inference.cc) | 布局推理 pass 的 C++ 实现，本讲最重要的源码之一。 |
| [src/transform/layout_reducer.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/layout_reducer.cc) | reducer 缓冲区的预布局 pass，在 `LayoutInference` 之前运行。 |
| [src/transform/lower_tile_op.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/lower_tile_op.cc) | 把高层 tile op 降级为硬件指令的核心 pass。 |
| [src/op/operator.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/operator.cc) | `ParseOperator`：从 TIR Call 还原出 `TileOperator` 对象，是 `LowerTileOp` 的分发入口。 |
| [tilelang/transform/pass_config.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/transform/pass_config.py) | 所有 pass 旋钮（`PassConfigKey`）的定义。 |

## 4. 核心概念与源码讲解

`LowerAndLegalize` 的总入口在 [tilelang/engine/phase.py:130-187](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L130-L187)。它把十几个 pass 按固定顺序串起来，把「带高层 tile op 的前端 IR」逐步变成「可直接进入目标相关优化与 codegen 的 IR」。

下面把它拆成 4 个最小模块来理解。

### 4.1 前置合法化：绑定目标、修正索引、化简表达式

#### 4.1.1 概念说明

在真正做布局推理和 tile op 降级之前，编译器要先把 IR「收拾干净」：

- **绑定目标**：让下游所有 pass 都能读到 target 信息（例如是不是 CUDA、是不是 Hopper）。
- **负索引合法化**：前端允许写 `A[-1]` 这种 Python 风格的负下标，但 TIR 的底层访存指令不认负索引，必须转成 `shape[i] + (-1)` 这种非负形式。
- **注入假设**：主动告诉符号证明器一些已知事实（如 buffer 形状一定大于 0），让后续 `Simplify` 能化简掉大量边界判断。
- **化简**：合并常量、消除冗余表达式，让后面的 pass 看到的 IR 尽可能简单。

这一组 pass 的共同特点是：**它们不关心 tile op 的语义，只做 IR 层面的「规范化」**。

#### 4.1.2 核心流程

本模块对应 `LowerAndLegalize` 的前几行（[phase.py:152-164](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L152-L164)）：

```
BindTarget(target)
  ↓
(可选) LetInline                  # 若开启 tl.force_let_inline
  ↓
AddWrapperForSingleBufStore       # 为单 buffer store 包一层，方便 permuted layout 处理
  ↓
LegalizeNegativeIndex             # 负索引 → 非负索引
  ↓
InjectAssumes                     # 注入 assume 约束
  ↓
Simplify                          # 表达式化简
```

其中负索引合法化的数学含义很直白：对任意缓冲区第 \(i\) 维，若索引 \(x_i\) 被证明为负（\(x_i < 0\)），则替换为

\[
x_i' = \text{shape}_i + x_i
\]

这样访问范围被映射到 \([0, \text{shape}_i)\) 之内。

#### 4.1.3 源码精读

**LegalizeNegativeIndex** 分两步走：先用 `NegativeIndexAnalyzer` 分析每个 `BufferLoad/BufferStore` 的各维索引符号（非负 / 负 / 未知），再用 `NegativeIndexRewriter` 把判为负的索引改写。索引符号用一个三态枚举表示：

```cpp
// src/transform/legalize_negative_index.cc
enum class IndexSignState { kNonNegative, kNegative, kUnknown };  // L25
```

分析时，对每维索引调用 `analyzer_.CanProve(idx >= 0)` 来判定符号（[legalize_negative_index.cc:38-117](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/legalize_negative_index.cc#L38-L117)）。改写时只对判为 `kNegative` 的维度套用 \( \text{shape}_i + x_i \)：

```cpp
// src/transform/legalize_negative_index.cc
ffi::Array<PrimExpr> UpdateIdx(...) {
  for (size_t i = 0; i < indices.size(); ++i) {
    if (state_vec[i] != IndexSignState::kNegative) continue;
    new_indices.Set(i, analyzer_->Simplify(buffer_shape[i] + indices[i]));  // L176
  }
  return new_indices;
}
```

注意它还能处理向量索引（`Ramp`/`Broadcast`，即向量化访存），见 [legalize_negative_index.cc:62-112](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/legalize_negative_index.cc#L62-L112)。当索引符号无法证明时，会打一条 `DLOG(WARNING)`，但**不会**强行改写——因为后续的 `LegalizeSafeMemoryAccess`（4.4 节）会兜底加越界保护。

**InjectAssumes** 做的事是：扫描所有 buffer 的 shape，对其中**非整数常量**的符号维度（即动态 shape），插入一条 `with attr::tilelang_assume(shape > 0)` 的假设。这些假设会被 TVM 的符号证明器读到，从而在后续 `Simplify` 里消去形如 `if (shape > 0) ...` 的判断。核心构建逻辑：

```cpp
// src/transform/inject_assumes.cc
Stmt build(Stmt body) {
  for (const auto &e : items) {
    auto simplified = analyzer.Simplify(GT(e.expr, make_zero(e.expr->dtype)));  // L77
    body = AttrStmt(simplified, tir::attr::tilelang_assume, StringImm(...), body);  // L86
  }
  return body;
}
```

它还会把前端写出的 `T.assume(cond)`（以 `Evaluate` 形式出现的）转换成同样的 `AttrStmt`（[inject_assumes.cc:100-144](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_assumes.cc#L100-L144)）。

`Simplify` 是 TileLang 自己的增强版化简 pass（`tilelang.transform.Simplify`），相比上游 TVM 的版本，它对动态符号边界（`kSymbolicBound`）做了额外支持（见 phase.py 里 L182 处的 TODO 注释）。

#### 4.1.4 代码实践

**目标**：直观看到 `LegalizeNegativeIndex` 把负索引改写成了什么。

**操作步骤**：

1. 写一个会用负索引的小 kernel（这里为「示例代码」，非项目原有文件）：

```python
# 示例代码：观察负索引合法化
import tilelang
import tilelang.language as T

@T.prim_func
def neg_index(A: T.Tensor((16,), "float32"), B: T.Tensor((16,), "float32")):
    with T.Kernel(1, threads=128) as bx:
        # 用负索引写最后一维（Python 风格）
        B[15] = A[-1]

artifact = tilelang.lower(neg_index, target="cuda")
artifact.device_mod.show()   # 打印 lower 后的 TIR
```

2. 在 `device_mod.show()` 的输出里定位 `A[-1]` 这一处访存。

**需要观察的现象**：lower 后的 IR 里，对 `A` 的那一维索引不再是 `-1`，而是被替换成形如 `16 + (-1)` 并化简为 `15` 的非负表达式。

**预期结果**：负索引消失，所有访存索引都是非负的。若你看到的仍是负数，说明该索引被判定为 `kUnknown`，将由 4.4 节的 `LegalizeSafeMemoryAccess` 兜底。

> 注意：本例只为触发负索引改写，真实 kernel 里负下标并不常见。运行结果**待本地验证**（取决于 `Simplify` 是否把 `16 + (-1)` 折成 `15`）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `LegalizeNegativeIndex` 要在 `InjectAssumes` 之前运行，而不是之后？

**参考答案**：`LegalizeNegativeIndex` 依赖 `analyzer_.CanProve(idx < 0)` 判定符号；如果先注入 assume，某些原本可判负的索引可能被假设约束改写或掩盖，反而影响改写判定。保持「先修正显式负索引、再注入通用假设」的顺序更稳定，也让 `InjectAssumes` 的产物能被紧随其后的 `Simplify` 充分利用。

**练习 2**：如果一个索引被判定为 `kUnknown`，`LegalizeNegativeIndex` 会怎么处理？

**参考答案**：不改写，只打一条 `DLOG(WARNING)`，并把它留给后续的 `LegalizeSafeMemoryAccess` pass 通过加越界 `if` 保护来兜底。

---

### 4.2 布局推理：LayoutReducer + LayoutInference

这是整个合法化阶段最关键、也最 TileLang 特有的部分。

#### 4.2.1 概念说明

回忆 [u2-l2](u2-l2-tile-alloc.md)：`fragment` 缓冲区的元素是**打散分布在 warp 各线程的寄存器上**的。但「具体第几个元素落在第几个线程上」这件事，前端并没有写死——它由编译器根据 tile op 的需求来**推理**。这个推理过程就是 `LayoutInference`。

为什么必须推理？因为不同的硬件指令对寄存器布局有硬性要求：

- `mma`（Ampere 及以前）要求 fragment A/B/C 满足特定的「行/列分布」。
- `wgmma`（Hopper）要求 operand 来自符合布局的 shared memory 描述符。
- `tcgen05`（Blackwell）又有一套自己的要求。

`LayoutInference` 要为每个 fragment/shared 缓冲区找到一个**线程级布局** `Fragment`（一个描述「逻辑元素 → (线程, 寄存器)」映射的对象），使得它同时满足「消费它的 tile op」和「生产它的 tile op」两边的约束，并且尽量少占寄存器。

`LayoutReducer` 则是一个前置步骤：它专门处理 `alloc_reducer` + `finalize_reducer`（见 [u2-l3](u2-l3-compute-primitives.md)）涉及的 `local.reducer` 缓冲区，在 `LayoutInference` 之前先给它们定好布局（ALL 复制 / NONE 分片），并把 scope 从 `local.reducer` 归一化掉。

#### 4.2.2 核心流程

布局推理的整体思路是一个**带优先级的迭代推断**（在 [layout_inference.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/layout_inference.cc) 的 `BufferUseDefCollector::Run` 里实现）：

```
1. 收集阶段 (Collect)
   - 遍历 IR，把每个 tile op（copy/gemm/reduce/parallel 循环）登记进 infer_list_
   - 建立 use_list_：记录「每个 fragment 缓冲区被哪些 tile op 使用」
   - 识别 floating fragment（在 tile op 之外被访问的 fragment，必须全复制）

2. 推断阶段 (Run)
   step 0: 给 floating fragment 赋「全复制」布局
   step 1: 严格推断（kStrict）—— 每个 op 给出硬约束布局
   step 2: BFS 普通推断（kCommon）—— 沿数据流传播布局
   step 3: 自由推断（kFree, InferInFreeMode）—— 对每个连通分量
           枚举不同「推断根」，选寄存器数最少的那套方案
   step 4: 对别名缓冲区（同 data Var）补全/reshape 布局

3. 回写阶段 (LayoutInferencer)
   - 把推断出的 layout_map 挂到 Block 的 attr::kLayoutMap 注解上
   - 把 parallel 循环的布局挂到 For 的 attr::kParallelLoopLayout 注解上
   - 注意：本 pass 不展开循环、不改 buffer 形状，只「贴标签」
```

关键点：`LayoutInference` **本身并不真正改写访存指令**，它只把推理出的布局以**注解（annotation）**形式贴到 IR 上。真正的「按布局改写下标」发生在后面的 `LowerTileOp`（4.3 节）。

`LayoutReducer` 的 reducer 布局构造逻辑（[layout_reducer.cc:214-225](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/layout_reducer.cc#L214-L225)）：

- 若复制类型为 `ALL`：构造 `Fragment::FullyReplicated(shape, thread_extent)`，每个线程都持有一份完整副本。
- 若复制类型为 `NONE`：把缓冲区按线性下标对线程数取模分配，即每个线程负责不同元素。

#### 4.2.3 源码精读

**布局推断的核心调用**——每个 tile op 通过 `InferLayout` 返回它对缓冲区的布局约束（[layout_inference.cc:112-119](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/layout_inference.cc#L112-L119)）：

```cpp
// src/transform/layout_inference.cc  (BufferUseDefCollector::RunInferStep)
auto updates = next->InferLayout(LayoutInferArgs{target_, thread_bounds,
                                                 layout_map, cur_analyzer,
                                                 buffer_oob, {}, let_var_to_expr_},
                                 level);
```

这里 `next` 是一个 `TileOperator`（由 `ParseOperator` 从 TIR Call 还原，见 [operator.cc:30-39](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/operator.cc#L30-L39)）。不同 op 返回不同布局：`GemmOp` 会返回满足 mma/wgmma 形状的 fragment 布局，`CopyOp` 会根据 src/dst 推导搬运布局，`ParallelOp`（即 `T.Parallel` 循环）会推导循环到线程的映射。

`InferInFreeMode` 是布局推理里最有「优化味」的部分（[layout_inference.cc:1060-1148](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/layout_inference.cc#L1060-L1148)）：它用并查集（`UnionFind`）把「共享 fragment 缓冲区」的 op 划分成连通分量，然后对每个连通分量**枚举每一个 op 当推断根**，分别跑一遍推断，最后选**总寄存器数最少**的那套布局。寄存器数按下式估算：

\[
\text{reg\_num} = \sum_{\text{buffer } b} \prod_{i} \text{OutputShape}_b[i]
\]

即所有 fragment 的输出形状乘积之和（[layout_inference.cc:1110-1127](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/layout_inference.cc#L1110-L1127)）。这一步是 TileLang 自动为 GEMM/Attention 选出低寄存器占用布局的关键。

**结果回写**——`LayoutInferencer` 把布局贴成注解（[layout_inference.cc:1187-1199](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/layout_inference.cc#L1187-L1199)）：

```cpp
// src/transform/layout_inference.cc  (LayoutInferencer::VisitStmt_)
for (auto buffer : block->alloc_buffers) {
  if (buffer.scope() == "local.framgent") {   // 注意源码里 framgent 是历史拼写
    ICHECK(result_.layout_map.count(buffer)) << "Cannot inference fragment layout for " << buffer;
  }
}
auto block_ptr = block.CopyOnWrite();
block_ptr->annotations.Set(attr::kLayoutMap, result_.layout_map);   // L1197
```

这段代码还隐含一个**不变量**：每个 `local.fragment` 缓冲区都必须能被推断出布局，否则直接报错。这正是为什么写 kernel 时 fragment 的访问模式必须能被 tile op 语义解释——否则推理器无从下手。

`LayoutReducer` 的 pass 入口很薄（[layout_reducer.cc:396-402](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/layout_reducer.cc#L396-L402)），它还顺带把 `T.fill`/`T.finalize_reducer` 配对（用一个 `inside_reducer_range_` 栈跟踪），并在 `finalize_reducer` 调用上补一个 reducer op 枚举参数（[layout_reducer.cc:315-338](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/layout_reducer.cc#L315-L338)）。

#### 4.2.4 代码实践

**目标**：让 `LayoutInference` 把它推理出的 fragment 布局**可视化**出来，直观看到「每个寄存器对应哪些元素」。

**操作步骤**：

1. 用 [examples/quickstart.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py) 里的 matmul kernel，编译时打开布局可视化旋钮：

```python
# 示例代码：开启布局可视化
import tilelang
import tilelang.language as T
from tilelang.transform import PassContext

# （此处省略 matmul_relu_kernel 的定义，直接复用 examples/quickstart.py 的 kernel）

with PassContext(config={
    "tl.layout_visualization_enable": True,
    "tl.layout_visualization_formats": "txt",
}):
    kernel = tilelang.jit(pass_configs={
        "tl.layout_visualization_enable": True,
        "tl.layout_visualization_formats": "txt",
    })(matmul)(M=1024, N=1024, K=1024, block_M=128, block_N=128, block_K=32)
```

2. 运行后，在工作目录下查找生成的布局可视化文件（通常是 `*.txt`，文件名与 kernel/buffer 相关）。

**需要观察的现象**：可视化文件里会画出 `C_local`（fragment 累加器）的线程布局——哪些 (i, j) 元素归同一个线程、布局是否匹配 `wgmma`/`mma` 要求的形状（如每 warp 至少 16 行）。

**预期结果**：能在一个文本/图形里看到 fragment 的元素到线程的映射网格；改变 `block_M/block_N` 后布局网格会相应变化。具体可视化产物路径与渲染细节**待本地验证**。

> 旋钮定义见 [pass_config.py:84-91](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/transform/pass_config.py#L84-L91)；可视化由 [phase.py:107-111](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L107-L111) 的 `LayoutVisual` 在 `LayoutInference` 之后触发。

#### 4.2.5 小练习与答案

**练习 1**：`LayoutInference` 推断失败时，常见原因是什么？

**参考答案**：最常见的是某个 `local.fragment` 缓冲区的访问模式无法被任何 tile op 解释——例如在 `T.Parallel`/`T.copy`/`T.gemm` 之外直接用普通下标读写 fragment，又不在「floating」情形里。此时 `LayoutInferencer::VisitStmt_(BlockNode)` 的 `ICHECK` 会报 `Cannot inference fragment layout for <buffer>`。

**练习 2**：为什么 `LayoutInference` 只「贴注解」而不直接改写下标？

**参考答案**：因为布局推理的结果还要被多个后续 pass 共享（`LowerTileOp` 改写下标、`LowerParallelLoop` 展开循环、code 生成阶段选指令）。把布局统一存成 `attr::kLayoutMap` 注解，既避免重复推理，也让各 pass 能各取所需。真正按下标重写的责任落在 `LowerTileOp`。

---

### 4.3 高层 tile op 降级：LowerTileOp

#### 4.3.1 概念说明

`LayoutInference` 贴好布局注解后，IR 里仍然是一堆 `tl.tileop.copy` / `tl.tileop.gemm` 这样的高层 intrin——它们只是「占位符」，硬件并不认识。`LowerTileOp` 的职责就是：**读取布局注解，把每个高层 tile op 降级成具体的硬件指令调用**。

具体来说，它要做三件事：

1. **按布局改写缓冲区**：把带 `kLayoutMap` 注解的 buffer 替换成「按布局重新整型」后的新 buffer（fragment 的打散布局被「物化」成新的 shape），并把所有 `BufferLoad/BufferStore` 的下标用 `layout->Forward(...)` 重算。
2. **降级 tile op**：对每个 tile op 调用其 `Lower(...)` 方法，生成真正的指令——`T.copy` 被降级成 TMA / cp.async / LDSM 等搬运指令，`T.gemm` 被降级成 mma / wgmma / tcgen05 指令。
3. **决定 TMA 路径**：扫描降级结果里是否出现了 TMA 指令；据此回写 `tl.disable_tma_lower` 配置，影响后续 `OptimizeForTarget` 是否走 TMA+warp 特化分支。

#### 4.3.2 核心流程

```
对每个 Evaluate(tile_op_call) 节点：
  1. ParseOperator(stmt)  →  得到 TileOperator 对象（copy/gemm/reduce/...）
  2. 构造 workspace 回调（为需要动态 shared 的 op 申请 shared.dyn 缓冲区）
  3. 从 analyzer 读出 thread_bounds
  4. tile_op->Lower(LowerArgs{target, thread_bounds, thread_var,
                             callback, layout_map, buffer_remap, ...})
     → 返回降级后的 Stmt（含具体硬件指令）
  5. 递归访问降级结果，继续处理内部节点

对每个 BufferLoad/BufferStore：
  若 buffer 在 buffer_remap_ 里（即有布局）：
    用 layout->Forward(indices) 重算下标，指向新 buffer
```

`LowerTileOp` 内部还维持一张 `buffer_remap_`（旧 buffer → 按布局整型后的新 buffer），降级完成后用 `LayoutRemapRewriter` / `RemapBufferRewriter` 把整张 IR 的引用统一切换（[lower_tile_op.cc:213-216](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/lower_tile_op.cc#L213-L216)）。

#### 4.3.3 源码精读

**降级入口**——对 `Evaluate` 节点的处理（[lower_tile_op.cc:608-657](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/lower_tile_op.cc#L608-L657)）。关键几行：

```cpp
// src/transform/lower_tile_op.cc  (LowerTileOpPass::VisitStmt_(EvaluateNode))
auto tile_op = ParseOperator(tvm::ffi::GetRef<Stmt>(op));   // L614  还原 TileOperator
if (!tile_op.defined())
  return IRMutatorWithAnalyzer::VisitStmt_(op);
AddWorkspaceCallback callback = [this](int num_elem, DataType dtype) {
  auto workspace = decl_buffer({PrimExpr(num_elem)}, dtype, "workspace", "shared.dyn");  // L619
  ...
};
...
auto lowered = tile_op->Lower(                                       // L652
    LowerArgs{target_, thread_bounds, thread_var_->var, callback,
              layout_map_, buffer_remap_, let_var_to_expr},
    analyzer_);
return IRMutatorWithAnalyzer::VisitStmt(lowered);
```

`ParseOperator` 的实现很简单——查 `TLOpBuilder` 属性表（[operator.cc:30-39](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/operator.cc#L30-L39)），把 TIR Call 交给对应 op 的构造器。`tile_op->Lower(...)` 的具体实现在各 op 自己的 `.cc`（如 `src/op/gemm.cc`、`src/op/copy.cc`），是 u7 的内容，本讲只需知道「它产出真正的指令」。

**按布局改写下标**——以 `BufferStore` 为例（[lower_tile_op.cc:530-546](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/lower_tile_op.cc#L530-L546)）：

```cpp
// src/transform/lower_tile_op.cc  (VisitStmt_(BufferStoreNode))
if (buffer_remap_.count(buffer)) {
  auto new_indices = layout_map_[buffer]->Forward(store->indices);   // L534  按布局前向映射下标
  auto new_buffer = buffer_remap_[store->buffer];
  layout_remap_.Set(new_buffer, layout_map_[store->buffer]);
  return BufferStore(new_buffer, store->value, new_indices);
}
```

`layout->Forward(indices)` 就是把「逻辑元素下标」翻译成「按布局物化后的新 buffer 下标」。`BufferLoad` 的处理完全对称（[lower_tile_op.cc:509-528](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/lower_tile_op.cc#L509-L528)）。

**fragment → local 的 scope 归一化**——布局物化时，`makeBufferWithLayout` 会把 `local.fragment` 的存储域改成普通 `local`（寄存器），因为布局已经把「打散」信息编码进 shape 了（[lower_tile_op.cc:33-43](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/lower_tile_op.cc#L33-L43)）：

```cpp
// src/transform/lower_tile_op.cc  (makeBufferWithLayout)
if (ptr_type->storage_scope == "local.fragment") {
  new_type = PointerType(ptr_type->element_type, "local");   // L40  fragment → local
}
```

**TMA 路径决策**——降级时若发现 IR 里出现了 `tma_load`/`tma_store`，就置 `has_tma_=true`，并据此回写 `kDisableTMALower`（[lower_tile_op.cc:217-225](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/lower_tile_op.cc#L217-L225)）：

```cpp
// src/transform/lower_tile_op.cc
if (!opt_disable_tma_lower.value_or(Bool(false))) {
  ctxt->config.Set(kDisableTMALower, Bool(!substituter.has_tma_));   // L224
}
```

意思是：如果用户没显式禁用 TMA，且降级结果里**没有**用到 TMA，就把 `disable_tma_lower` 设为 true——这样后续 `OptimizeForTarget` 就不会去走 TMA+warp 特化那条（本 kernel 用不上的）分支。这是个很巧妙的「按实际指令反向决定后续 pass 走向」的设计。

> 这一节顺带覆盖 `LowerL2Persistent`（[phase.py:174](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L174)）：它把前端标注的 L2 persistent（L2 驻留）映射合法化成底层 attr，实现见 `src/transform/lower_l2_persistent_annotation.cc`，本讲不展开。

#### 4.3.4 代码实践

**目标**：通过开关 `tl.disable_tma_lower`，对比 lower 后 IR 里 `T.copy` 被降级成了什么指令，体会 `LowerTileOp` 的「自动选路」。

**操作步骤**：

1. 复用 [examples/quickstart.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py) 的 `matmul_relu_kernel`，分别以默认配置和禁用 TMA 的配置 lower（示例代码）：

```python
import tilelang
import tilelang.language as T
from tilelang.transform import PassContext

# ... 复用 quickstart.py 里的 matmul_relu_kernel 定义，这里记为 fn: PrimFunc

# (a) 默认：让编译器自己选（Hopper 上会选 TMA）
artifact_default = tilelang.lower(fn, target="cuda")

# (b) 禁用 TMA：强制走 cp.async / 普通 SIMT 拷贝
with PassContext(config={"tl.disable_tma_lower": True}):
    artifact_no_tma = tilelang.lower(fn, target="cuda")

print("===== 默认 =====")
artifact_default.device_mod.show()
print("===== 禁用 TMA =====")
artifact_no_tma.device_mod.show()
```

2. 在两份输出里定位 `T.copy(A[...], A_shared)` 这一条搬运被降级后的样子。

**需要观察的现象**：

- 默认输出里（若在 Hopper/有 TMA 的目标上）能看到 `T.tma_load` 之类的 TMA 指令调用。
- 禁用 TMA 的输出里，同一条搬运会变成 `cp.async`（`T.ptx_cp_async_*`）或普通 `T.buffer` load/store。
- 两份 IR 里 fragment 累加器 `C_local` 的 shape 与 `kLayoutMap` 注解相同（因为 `LayoutInference` 的结果不受这个旋钮影响）——这正说明 **LayoutInference 先行、LowerTileOp 后随**。

**预期结果**：搬运指令不同，但 fragment 布局注解一致；据此能说清「`LayoutInference` 改变的是『数据怎么分布到线程/寄存器』，而 `LowerTileOp` 负责把搬运/计算落成具体指令」。

> 运行结果**待本地验证**：是否出现 TMA 取决于目标 GPU 是否支持（`have_tma`/`is_hopper`，见 [phase.py:6](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L6) 的 import）。在非 Hopper 卡上即使不禁用，也只会走 cp.async。

#### 4.3.5 小练习与答案

**练习 1**：`LowerTileOp` 里 `makeBufferWithLayout` 把 `local.fragment` 改成了 `local`，为什么这样做是安全的？

**参考答案**：因为「fragment 的元素如何分布到线程」这一信息已经被 `LayoutInference` 编码进了新 buffer 的 shape 与 `layout->Forward` 的下标映射里。改名为普通 `local` 只是去掉语义标签，物理上仍是一组线程私有寄存器，访问时已按下标映射正确寻址，因此安全。

**练习 2**：如果用户没有显式设置 `tl.disable_tma_lower`，最终是否走 TMA 分支由谁决定？

**参考答案**：由 `LowerTileOp` 降级结果里**是否出现 TMA 指令**（`has_tma_`）决定——降级产物里有 TMA，`disable_tma_lower` 被置 false，后续走 TMA+warp 特化分支；否则置 true。见 [lower_tile_op.cc:221-225](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/lower_tile_op.cc#L221-L225)。

---

### 4.4 收尾合法化：向量化与安全访存

#### 4.4.1 概念说明

`LowerTileOp` 之后，IR 里已经出现了真正的指令，但还可能有两类隐患：

- **不合法的向量化循环**：`T.Parallel` 经布局推理后可能产生一些向量宽度不合法的循环，需要合法化或退化为标量循环。
- **可能越界的访存**：当 tile 不能整除问题规模（如 `K` 不是 `block_K` 的整数倍）时，最后一个 tile 的访存会越界。`LegalizeSafeMemoryAccess` 会给这类访存自动加上 `if` 越界保护。

加完保护后，IR 里会多出一些重复的条件判断，所以需要**再做一次 `Simplify`** 把冗余条件清掉。

#### 4.4.2 核心流程

本模块对应 `LowerAndLegalize` 的最后几行（[phase.py:176-187](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L176-L187)）：

```
LegalizeVectorizedLoop     # 合法化向量化循环（非法则退化）
  ↓
LegalizeSafeMemoryAccess   # 给可能越界的 global 访存加 if 保护
  ↓
Simplify                   # 二次化简，清掉安全检查引入的冗余条件
  ↓
HoistNonRestrictParams     # 把 root-block 上的注解提升到 PrimFunc 属性
  ↓
return mod                 # 进入 OptimizeForTarget（u3-l4）
```

#### 4.4.3 源码精读

**LegalizeSafeMemoryAccess** 只关心 **global** 缓冲区的越界——因为 TileLang 程序里越界只可能发生在 global（shared/fragment 都是按 tile 大小精确分配的）。判定逻辑由 `GlobalMemChecker` 承担（[legalize_safe_memory_access.cc:33-80](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/legalize_safe_memory_access.cc#L33-L80)）：

```cpp
// src/transform/legalize_safe_memory_access.cc
void VisitExpr_(const BufferLoadNode *op) final {
  if (IsGlobalBuffer(op->buffer)) {                       // 只管 global
    CheckBufferIndices(op->buffer, op->indices, /*is_load=*/true);
  }
  ...
}
bool IsGlobalBuffer(const Buffer &buffer) {
  return buffer.scope() == "global";                      // L67
}
```

当某维索引的上界可能超过 shape 时，它会把对应访存包进一个 `if (index < shape)` 保护里（具体实现在 `loop_partition.h` 提供的分区工具）。这也是为什么前端写 `T.copy`、`T.Parallel` 时通常**不用手动处理边界 tile**——合法化阶段会自动兜底。

**HoistNonRestrictParams** 把散落在 root block 上的注解（如 `kLayoutMap`、restrict 信息）提升到 `PrimFunc` 的属性层，方便 host/device 拆分与 codegen 统一读取。

> 关于 `tl.disable_safe_memory_legalize` 旋钮（[pass_config.py:49-50](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/transform/pass_config.py#L49-L50)）：当确信自己的访存不会越界时，可关闭此 pass 以省掉保护分支、略微提速；但需自行保证安全。

#### 4.4.4 代码实践

**目标**：观察 `LegalizeSafeMemoryAccess` 自动加上的越界保护。

**操作步骤**：

1. 用一个**不能整除**的尺寸触发残余 tile（示例代码）：

```python
import tilelang
import tilelang.language as T

@T.prim_func
def copy_nondiv(A: T.Tensor((100,), "float32"), B: T.Tensor((100,), "float32")):
    with T.Kernel(T.ceildiv(100, 32), threads=128) as bx:
        A_shared = T.alloc_shared((32,), "float32")
        T.copy(A[bx * 32], A_shared)        # 最后一个 bx 会读越界
        T.copy(A_shared, B[bx * 32])

artifact = tilelang.lower(copy_nondiv, target="cuda")
artifact.device_mod.show()
```

2. 在输出里定位最后一个 tile（`bx` 取到边界）对 `A` / `B` 的访存。

**需要观察的现象**：对 global 缓冲区 `A`、`B` 的 load/store 外面，应该被包了一层 `if (bx * 32 + offset < 100)` 之类的保护；对 shared `A_shared` 的访问则**没有**保护（它不越界）。

**预期结果**：只有 global 访存带保护，shared/fragment 访存不带。这印证了 `LegalizeSafeMemoryAccess` 只管 global 的设计。运行结果**待本地验证**（保护的具体形式取决于分区工具）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `LegalizeSafeMemoryAccess` 之后还要再跑一次 `Simplify`？

**参考答案**：安全检查会给同一 tile 内的多次访存分别加 `if` 保护，产生大量重复或可合并的条件。二次 `Simplify` 能把这些冗余条件折叠掉，避免生成出充斥着重复分支的低质代码。

**练习 2**：为什么合法化只针对 global 缓冲区？

**参考答案**：shared memory 和 fragment 都是按 tile 形状精确分配的局部缓冲区，访问下标天然落在分配范围内，不会越界；只有 global 张量在 tile 不整除问题规模时才可能读出界。所以只对 global 加保护即可，省去对局部缓冲区的无谓检查。

---

## 5. 综合实践

把本讲四个模块串起来：写一个 matmul kernel，**只观察合法化阶段**的 IR 变化（不让 `OptimizeForTarget` 干扰）。

由于 `tilelang.lower` 会把三大阶段跑完，我们可以借助「逐 pass 手动 apply」的方式隔离出 `LowerAndLegalize` 的中间产物。

**任务**：

1. 复用 [examples/quickstart.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py) 的 `matmul_relu_kernel`，先把它定义成一个独立的 `@T.prim_func`（不带 `@tilelang.jit`）。
2. 参照 [phase.py:130-187](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L130-L187) 的顺序，**手动逐个 apply** 关键 pass，每步打印 IR（示例代码）：

```python
import tilelang
import tilelang.language as T
from tvm import tir
from tvm.target import Target

# fn = matmul_relu_kernel 这个 PrimFunc
mod = tir.IRModule({"main": fn})
target = Target("cuda", host="llvm")

mod = tir.transform.BindTarget(target)(mod)
print("=== 1. LegalizeNegativeIndex 后 ===")
mod = tilelang.transform.LegalizeNegativeIndex()(mod)
mod = tilelang.transform.InjectAssumes()(mod)
mod = tilelang.transform.Simplify()(mod)
mod.show()

print("=== 2. LayoutInference 后（出现 kLayoutMap 注解）===")
mod = tilelang.transform.LayoutReducer()(mod)
mod = tilelang.transform.LayoutInference()(mod)
mod.show()

print("=== 3. LowerTileOp 后（高层 intrin 消失，出现具体指令）===")
mod = tilelang.transform.LowerTileOp()(mod)
mod.show()
```

3. 在三份 IR 里分别记录：
   - 第 1 步：负索引是否被消除、`assume` 是否出现。
   - 第 2 步：`C_local`（fragment）上是否多了 `kLayoutMap` 注解；注解里描述的线程分布是什么。
   - 第 3 步：`tl.tileop.copy` / `tl.tileop.gemm` 是否被替换成了 TMA / cp.async / mma / wgmma 指令；fragment 的 `local.fragment` scope 是否变成了 `local`。

**预期结果**：你能用一张表把「输入 IR → 合法化后 IR」的每处变化对应到本讲的某个 pass。这张表就是本讲的「知识地图」。

> 注意：手动 apply pass 时需要保证 target 已绑定（`BindTarget` 必须先跑），否则 `LowerTileOp` 会因读不到 target attr 而 `ICHECK` 失败（见 [lower_tile_op.cc:208-209](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/lower_tile_op.cc#L208-L209)）。各 pass 对象从 `tilelang.transform` 导入（见 [transform/__init__.py:39-58](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/transform/__init__.py#L39-L58)）。具体可观察到的 IR 细节**待本地验证**。

## 6. 本讲小结

- `LowerAndLegalize` 是编译的第二大阶段，介于只校验的 `PreLowerSemanticCheck` 和目标相关的 `OptimizeForTarget` 之间，输入带高层 tile op 的 IR，输出可直接进入优化的 IR（编排见 [phase.py:130-187](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L130-L187)）。
- 前置 pass 做规范化：`BindTarget` 绑目标、`LegalizeNegativeIndex` 消负索引（\( \text{shape}_i + x_i \)）、`InjectAssumes` 注入假设、`Simplify` 化简。
- `LayoutReducer` 给 reducer 缓冲区预布局（ALL 全复制 / NONE 取模分片）；`LayoutInference` 用带优先级的迭代推断（含 `InferInFreeMode` 枚举选最少寄存器方案）为每个 fragment/shared 推导线程级布局，并**以 `kLayoutMap` 注解形式贴回**，不直接改写指令。
- `LowerTileOp` 读取布局注解，用 `layout->Forward` 改写下标、把 `local.fragment` 归一化为 `local`，并调用各 tile op 的 `Lower(...)` 生成 TMA / cp.async / mma / wgmma 等真实指令；它还会据降级结果里有无 TMA 反向设置 `disable_tma_lower`。
- 收尾 pass 兜底：`LegalizeVectorizedLoop` 合法化向量化、`LegalizeSafeMemoryAccess` 只给 global 访存加越界 `if` 保护，再用一次 `Simplify` 清掉冗余条件，最后 `HoistNonRestrictParams` 提升注解。
- 调试旋钮都在 `tilelang.transform.PassConfigKey`（[pass_config.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/transform/pass_config.py)），通过 `PassContext` 或 `@tilelang.jit(pass_configs=...)` 设置。

## 7. 下一步学习建议

- 进入 [u3-l4 OptimizeForTarget](u3-l4-optimize-target.md)：合法化后的 IR 在此进入目标相关优化——软件流水、warp 特化、`FlattenBuffer`、`SplitHostDevice` 等。重点看它如何根据 `LowerTileOp` 设下的 `disable_tma_lower` 在「TMA+warp 特化分支」与「普通分支」之间分流。
- 若对布局推理的算法细节（`Fragment` 数据结构、`InferInFreeMode` 的连通分量枚举、`ProveFragmentContains`）感兴趣，可先读 u4 的布局推理深入讲义，再回到本讲对照源码。
- 想理解某个 tile op 具体降级成什么指令，可去看 `src/op/copy.cc`、`src/op/gemm.cc` 等各 op 的 `Lower` 实现（u7-l1），本讲只用了它们的「出口」。
