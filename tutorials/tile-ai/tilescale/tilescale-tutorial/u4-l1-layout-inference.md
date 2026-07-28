# Layout 推理机制

## 1. 本讲目标

本讲深入 TileLang 编译流水线中最核心、也最「魔法」的一步——**布局推理（Layout Inference）**。读完本讲，你应当能够：

- 说清 `Layout` / `Fragment` 这两个数据结构各自记录了什么，以及「线程级布局（thread-level layout）」到底指什么。
- 复述 `LayoutInference` pass 的四步算法：严格约束（kStrict）→ 常规传播（kCommon）→ 自由枚举（kFree）→ 别名收尾，并理解「自由模式下选寄存器最少的方案」这一关键策略。
- 看懂为什么 `T.gemm` 的累加器必须是 fragment、它的布局是如何由 mma/wgmma 指令形状决定的。
- 会用 pass 配置 `tl.layout_visualization_enable` 打开布局可视化，并解读打印出的 `Shape / Thread / Index` 三行输出。

本讲承接 u3-l3（`LowerAndLegalize` 阶段总览）。在那里我们提到 `LayoutInference` 把结果以 `kLayoutMap` 注解贴回 IR、而下一个 pass `LowerTileOp` 才真正读取注解生成 TMA/mma 指令。本讲就把这个「注解是怎么算出来的」彻底讲透。

## 2. 前置知识

阅读本讲前，请确认你已经理解以下概念（u2、u3 已建立）：

- **显存层级与 fragment**：`T.alloc_shared` 分配 shared memory，`T.alloc_fragment` 分配寄存器 fragment。fragment 的元素是「打散分布在 warp 的各线程上」的，这正是它能对接 tensor core 的原因（见 u2-l2）。
- **Tile 算子是高层 intrin**：`T.copy` / `T.gemm` 在前端只是 `tl.tileop.copy` / `tl.tileop.gemm` 的 `call_intrin`，真正的 TMA / mma 指令要等 `LowerTileOp` 才出现（见 u3-l2、u3-l3）。
- **编译阶段位置**：`LayoutInference` 位于 `LowerAndLegalize` 中，紧跟在 `LayoutReducer` 之后、`LowerTileOp` 之前（见 u3-l3）。
- **GPU 线程模型**：一个 threadblock 含若干 warp，每个 warp 含 32 线程；mma/wgmma 等 tensor core 指令把一个矩阵 tile 的计算分配到一组线程上，每个线程持有该 tile 的若干元素。

如果你对 tensor core 的「一个 warp 算一个 16×8（Ampere mma.m16n8k16）或 64×N（Hopper wgmma）的结果 tile」没有印象，也不必担心——本讲会用具体数字带你看懂。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`src/layout/layout.h`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/layout/layout.h) | `LayoutNode` / `FragmentNode` 的 C++ 类声明与关键字段定义 |
| [`src/layout/layout.cc`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/layout/layout.cc) | `Layout` / `Fragment` 的核心实现：`Forward`、`Inverse`、`ThreadExtent`、`FullyReplicated`、`DebugOutput` 等 |
| [`src/op/operator.h`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/operator.h) | `InferLevel` 枚举、`LayoutInferArgs`、`TileOperatorNode::InferLayout` 虚接口 |
| [`src/transform/layout_inference.cc`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/layout_inference.cc) | **本讲主角**：`LayoutInference` pass 的全部算法（工作队列 + 三级推理 + 自由模式枚举） |
| [`src/op/gemm.cc`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/gemm.cc) | `T.gemm` 算子的 `InferLayout`，给出 C 累加器的 fragment 布局 |
| [`src/op/copy.cc`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/copy.cc) | `T.copy` 算子的 `InferLayout`，决定 shared memory 的 swizzle/TMA 布局 |
| [`tilelang/layout/layout.py`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/layout/layout.py) / [`fragment.py`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/layout/fragment.py) | `Layout` / `Fragment` 的 Python 封装，转发到 `_ffi_api` |
| [`tilelang/analysis/layout_visual.py`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/analysis/layout_visual.py) | 布局可视化 pass：读 `layout_map` 注解、打印 + 画图 |
| [`tilelang/engine/phase.py`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py) | 把 `LayoutInference` 与可视化挂进 `LowerAndLegalize` 流水线 |
| [`examples/visual_layout_inference/visual_layout_inference.py`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/visual_layout_inference/visual_layout_inference.py) | 官方可视化示例，本讲代码实践的范本 |

> 注意：大纲里写的 `tilelang/transform/layout_inference.cc` 实际路径是 `src/transform/layout_inference.cc`（C++ 源码在 `src/` 下）。同理 `tilelang/transform/layout_inference` 在 Python 侧只是一个对外名，真正实现在 `src/`。

## 4. 核心概念与源码讲解

### 4.1 Layout / Fragment 数据结构与线程布局

#### 4.1.1 概念说明：为什么需要「布局」这个东西

考虑这样一段你早已熟悉的 TileLang 代码（来自 quickstart）：

```python
C_local = T.alloc_fragment((block_M, block_N), accum_dtype)
T.gemm(A_shared, B_shared, C_local)
```

`C_local` 在逻辑上是一个 `(block_M, block_N)` 的二维 tile，比如 `32×32 = 1024` 个元素。但它在物理上**不是**一块连续的 1024 元素数组——它分散在一个 threadblock 的 128 个线程的寄存器里。问题是：

- 逻辑坐标 `(i, j)` 的那一个元素，到底落在**哪个线程**上？
- 落在那个线程的**第几个寄存器槽**里？

这两个映射一旦确定，就称这个 fragment 拥有一个确定的**线程级布局**。它不是任意选的：tensor core 指令（Volta 的 mma、Ampere 的 mma.m16n8k16、Hopper 的 wgmma、Blackwell 的 tcgen05）对「哪个线程持有哪些元素」有**硬性、固定**的要求。只有 fragment 的布局恰好匹配指令的输入/输出分布，编译器才能把 `T.gemm` 翻译成正确的硬件指令。

`LayoutInference` 这个 pass 的任务，就是为每一个 `fragment`（以及需要 TMA 搬运的 `shared` 缓冲区）推导出这样一个线程级布局，然后以注解的形式贴回 IR，交给下一个 pass（`LowerTileOp`）去真正生成指令。

#### 4.1.2 核心流程：Layout 是正向映射，Fragment 多了线程维

TileLang 用两个 C++ 类来表示布局，都定义在 `src/layout/layout.h`：

- **`Layout`**：描述「逻辑下标 → 线性化输出下标」的仿射映射。它只有两个字段：`input_size_`（输入各维大小）和 `forward_index_`（一组正向表达式）。
- **`Fragment`**：继承自 `Layout`，**额外**带一个「线程维度」：`forward_thread_`（哪个线程）、`replicate_size_`（复制份数）、`thread_range_`（线程范围绑定）。

换句话说，`Layout` 只回答「这元素排在第几号位置」，而 `Fragment` 还要回答「这元素归哪个线程、在线程内排第几号」。shared memory 通常用 `Layout`（因为它对全体线程可见、按地址访问），而寄存器 fragment 必须用 `Fragment`。

正向映射的形式化定义如下。对一个 `d` 维输入、输入占位符为 \(i_0,\dots,i_{d-1}\) 的 Layout，其第 \(k\) 个输出维度的下标由一个仿射表达式给出：

\[
\text{forward\_index}_k \;=\; f_k(i_0, i_1, \dots, i_{d-1})
\]

对 Fragment，还多一个复制占位符 \(r\) 与线程映射：

\[
\text{tid} \;=\; g(i_0, \dots, i_{d-1}, r), \qquad
\text{slot} \;=\; h(i_0, \dots, i_{d-1})
\]

其中 \(\text{tid}\) 是线程号、\(\text{slot}\) 是该线程内寄存器槽号。`Forward(vars)` 就是把具体下标代入这些表达式求值。

#### 4.1.3 源码精读

**字段定义** —— [`src/layout/layout.h:44-94`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/layout/layout.h#L44-L94) 给出 `LayoutNode`，关键字段是 `forward_index_` 与 `input_size_`；[`src/layout/layout.h:107-164`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/layout/layout.h#L107-L164) 给出 `FragmentNode`，在 `LayoutNode` 之上多了 `forward_thread_`、`replicate_size_`、`thread_range_`：

```cpp
// LayoutNode（layout.h:92-93）
Array<PrimExpr> forward_index_;   // 逻辑下标 -> 线性输出下标 的仿射表达式
Array<PrimExpr> input_size_;      // 输入各维大小

// FragmentNode（layout.h:161-163）
Range thread_range_;
PrimExpr forward_thread_;         // 逻辑下标 (+ 复制因子) -> 线程号
PrimExpr replicate_size_;         // 复制份数（全复制时 = 线程数）
```

**占位符** —— [`src/layout/layout.cc:32-35`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/layout/layout.cc#L32-L35) 定义了两个全局占位符：`InputPlaceholder(i)` 返回 `_i0/_i1/...`（第 i 个输入维度），`ReplicationPlaceholder()` 返回 `_rep`（复制维度）。所有 `forward_*` 表达式都用这些占位符书写，便于统一替换。

**Forward / 构造** —— [`src/layout/layout.cc:51-58`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/layout/layout.cc#L51-L58) 是 `LayoutNode` 构造函数，构造时即把表达式化简；[`src/layout/layout.cc:132-160`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/layout/layout.cc#L132-L160) 的 `Forward(vars)` 把具体下标代入占位符、返回线性输出下标：

```cpp
Array<PrimExpr> LayoutNode::Forward(const Array<PrimExpr> &vars) const {
  // 取最后 InputDim() 个变量做替换
  ...
  Map<Var, PrimExpr> vmap;
  for (size_t i = 0; i < InputDim(); i++)
    vmap.Set(InputPlaceholder(i), transform_vars[i]);
  return forward_index_.Map([&](const PrimExpr &e) {
    return Substitute(e, vmap);            // 代入具体下标
  });
}
```

`Forward` 是 `LowerTileOp` 重算访存下标时的核心调用：拿到 fragment 的布局后，把 IR 里「逻辑坐标 `(i,j)`」代入 `Forward`，就得到该元素在线程内的寄存器槽号。

**Fragment 的线程数 / 全复制** —— [`src/layout/layout.cc:613-619`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/layout/layout.cc#L613-L619) 的 `ThreadExtent()` 推断这个 fragment 占用了多少线程；[`src/layout/layout.cc:554-558`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/layout/layout.cc#L554-L558) 的 `FullyReplicated` 构造一个「每个线程都持有一份完整副本」的 fragment——这是给索引缓冲、mask 这类需要全体线程统一访问的 buffer 用的：

```cpp
Fragment Fragment::FullyReplicated(Array<PrimExpr> shape, PrimExpr thread_extent) {
  return Fragment(shape, {}, ReplicationPlaceholder(), thread_extent, std::nullopt);
}
```

注意它的 `forward_thread` 恒等于 `_rep`（复制占位符），意味着「线程号 = 复制号」，每个线程拿到的元素集合完全相同。

**Inverse（反推）** —— [`src/layout/layout.cc:248-300`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/layout/layout.cc#L248-L300) 的 `InverseWithLevel()` 复用 TVM 的 `arith::DetectIterMap` + `InverseAffineIterMap`，从线性位置反推逻辑下标。反推在「把一个已有布局搬到形状不同的 buffer（Reshape）」「校验两个布局是否等价」时都要用到。

**DebugOutput（可视化打印的源头）** —— [`src/layout/layout.cc:682-693`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/layout/layout.cc#L682-L693) 的 `FragmentNode::DebugOutput()` 拼出 `Fragment(shape -> output, replicate, thread, forward_thread, forward_index)` 字符串，可视化 pass 打印的 `Shape/Thread/Index` 三行就是从这里取的。

**Python 封装** —— [`tilelang/layout/layout.py:13-57`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/layout/layout.py#L13-L57) 的 `Layout` 把 `shape` 与 `forward_fn` 转成 IterVar 列表再调 `_ffi_api.Layout`；[`tilelang/layout/fragment.py:14-102`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/layout/fragment.py#L14-L102) 的 `Fragment` 同理，多了 `forward_thread_fn` / `replicate`。它们都只是薄封装，真正逻辑在 C++ 侧。

#### 4.1.4 代码实践：手工构造一个 8×8 mma 的 C fragment

**实践目标**：不依赖编译，直接在 Python 里构造一个 fragment，理解「逻辑下标 → (线程, 寄存器槽)」的映射。

**操作步骤**：

1. 写一个最小脚本（**示例代码，非项目原有**）：

```python
import tilelang.language as T
# 一个 warp(32线程) 做 8x8 mma 的 C 累加器典型布局：每线程持 8 个 fp16 元素
frag = T.Fragment(
    shape=[16, 8],          # 逻辑 tile 16x8 = 128 元素
    forward_thread_fn=lambda i, j: (i % 8) * 4 + (j % 8) // 2,  # -> 线程号(0..31)
    forward_index_fn=lambda i, j: (j % 2),                      # -> 寄存器槽(0,1)
)
print(frag)                  # 调 _DebugOutput
print("thread_size", frag.get_thread_size())
```

2. 重点观察 `__repr__` 打印的 `Shape / thread / index`。

**需要观察的现象 / 预期结果**：`Shape` 应为 `[16, 8] -> [...]`，`thread_size` 应为 32（一个 warp）。`forward_index` 的输出维度数量决定了每个线程持几个寄存器槽。**待本地验证**：精确表达式取决于 TVM 当前的化简结果，但线程总数应是 32。

#### 4.1.5 小练习与答案

**练习 1**：为什么 shared memory 的缓冲区通常用 `Layout` 而寄存器 fragment 必须用 `Fragment`？

> **答**：shared memory 对 threadblock 内所有线程按地址可见，访问时只需要「逻辑下标 → 地址」的线性映射，故 `Layout` 足够；而 fragment 的元素散布在各线程的私有寄存器里，必须额外知道「哪个线程、线程内第几个槽」，因此需要 `Fragment` 多出的 `forward_thread_` 维度。

**练习 2**：`FullyReplicated` 的 `forward_thread` 为什么等于 `_rep`？

> **答**：全复制表示每个线程持有一份完整副本，「线程号」与「复制号」一一对应，故 `forward_thread = _rep`，使得不同线程取到相同下标集合。

---

### 4.2 LayoutInference pass 算法思路

#### 4.2.1 概念说明：布局是「约出来」的，不是「指定」的

一个 kernel 里往往有多个 tile op 协作：`T.copy` 把数据从 global 搬到 shared，`T.gemm` 读 shared 写 fragment，`T.copy` 再把 fragment 搬回 global。每个 op 对涉及的 buffer 都有自己的布局**约束**：

- `T.gemm` 的累加器 C **必须**是某个由 mma/wgmma 指令形状决定的 fragment 布局（硬约束）。
- `T.copy(global → shared)` 在 Hopper 上若要用 TMA，则 shared buffer **必须**是某种 swizzle 布局（强约束）。
- 但一个同时被「gemm 读」和「copy 写」的 shared buffer，其布局要**同时**满足两边——这就是约束的传播与求解。

`LayoutInference` 的本质就是：**把每个 op 当作一个带约束的布局生产者，沿着 buffer 的使用关系传播约束，求解出一组全局一致的布局**。当存在多种可行解时（某些 buffer 没有硬约束），它会在「自由模式」下枚举各种方案，挑出**总寄存器占用最少**的那一个。

每个 op 通过实现虚函数 `InferLayout(args, level)` 来声明自己的约束。`level`（推理强度）控制它愿意在多宽松的条件下给出布局：

| `InferLevel` | 取值 | 语义 |
| --- | --- | --- |
| `kStrict` | 2 | 只给硬约束（如 gemm 的 C 累加器），不容协商 |
| `kCommon` | 1 | 常规传播：邻居布局已知时给出一致布局 |
| `kFree` | 0 | 最宽松：允许在邻居布局未知时主动猜测/枚举 |

#### 4.2.2 核心流程：四步求解

整个 pass 由 `BufferUseDefCollector::Run()` 编排，逻辑上分四步（伪代码）：

```
step 0  浮动 fragment buffer  -> FullyReplicated   # 在 TileOp 之外被访问(如 if 条件里)的 fragment
step 1  for op in ops: op.InferLayout(kStrict)      # 收集所有硬约束
step 2  BFS 工作队列: op.InferLayout(kCommon)       # 沿 buffer 使用关系传播
step 3  InferInFreeMode(kFree)                       # 未定 buffer: 连通分量内枚举, 选寄存器最少
step 4  按 storage Var 收尾别名 buffer               # 同一底层存储的多个 buffer 互相补全
```

关键设计：

- **工作队列（BFS）+ 优先级**：每当某个 op 推断出一个 buffer 的布局，就把「也用到这个 buffer 的其它 op」重新入队（因为它们现在可能有能力给出布局了）。入队时 [`EnqueueWithPriority`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/layout_inference.cc#L465-L480) 把「自身已有已知布局锚点」的 op 放到队首，加速收敛。
- **自由模式的寄存器代价模型**：对每个「连通分量」（通过 `UnionFind` 按 buffer 共享关系聚类），尝试把分量内**每个**op 当作推理起点各跑一遍，计算该方案下所有 fragment 的输出形状乘积之和作为寄存器代价，取最小者：

\[
\text{cost} \;=\; \sum_{b\,\in\,\text{fragments}}\;\prod_{s\,\in\,\text{OutputShape}(b)} s
\]

- **结果落点**：所有布局装进 `LayoutInferenceResult.layout_map`，最后由 `LayoutInferencer::VisitStmt_` 把它作为 `attr::kLayoutMap` 注解贴到每个 `Block` 上。它**不直接改写**指令——真正的下标重算与指令生成是下一个 pass `LowerTileOp` 的事（见 u3-l3）。

#### 4.2.3 源码精读

**虚接口** —— [`src/op/operator.h:75-85`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/operator.h#L75-L85) 定义每个 tile 算子必须实现的两个虚函数：`Lower`（生成指令）与 `InferLayout(args, level)`（声明布局约束）。`LayoutInferArgs`（[`operator.h:61-71`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/operator.h#L61-L71)）把 target、线程范围、当前 `layout_map`、analyzer 等上下文打包传进去。`InferLevel` 枚举见 [`operator.h:29-33`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/operator.h#L29-L33)。

**四步编排** —— [`src/transform/layout_inference.cc:310-335`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/layout_inference.cc#L310-L335) 是 `Run()` 的核心，四步一气呵成：

```cpp
// step 0: 浮动 fragment -> 全复制
for (const auto &[buffer, thread_bounds] : floating_fragment_buffers_) {
  auto frag = Fragment::FullyReplicated(buffer->shape, thread_bounds->extent);
  layout_map.Set(buffer, frag);
}
// step 1: 严格约束
for (int i = 0; i < num_infer; i++)
  RunInferStep(i, InferLevel::kStrict, false, layout_map, strict_layout_map, q, in_queue);
// step 2: 常规传播 (BFS)
FinishInferQueue(InferLevel::kCommon, layout_map, strict_layout_map, q, in_queue);
// step 3: 放宽到自由模式重跑
InferInFreeMode(layout_map, strict_layout_map);
```

`LayoutInferenceResult` 结构见 [`layout_inference.cc:61-65`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/layout_inference.cc#L61-L65)：`layout_map`（buffer→布局）、`for_map`/`predicate_map`（并行循环的布局与谓词）。

**单步推理 + 别名传播** —— [`layout_inference.cc:74-119`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/layout_inference.cc#L74-L119) 的 `RunInferStep` 调用 `next->InferLayout(...)`，再把返回的 `(buffer, layout)` 更新进 `layout_map`；其中 [`layout_inference.cc:128-168`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/layout_inference.cc#L128-L168) 的 `propagate_alias` lambda 会把布局传播给「共享同一底层 `data` Var」的兄弟 buffer（reshape 别名场景），必要时调 `Reshape` 换形。

**自由模式枚举** —— [`layout_inference.cc:995-1149`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/layout_inference.cc#L995-L1149) 的 `InferInFreeMode` 是最体现「智能」的部分。它先用 `UnionFind` 把所有 op 按 buffer 共享关系聚成连通分量（[`layout_inference.cc:1005-1051`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/layout_inference.cc#L1005-L1051)），然后对每个分量「尝试每个 op 当根、各跑一次推理、统计寄存器代价、取最小」（[`layout_inference.cc:1060-1148`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/layout_inference.cc#L1060-L1148)）。代价计算在 [`layout_inference.cc:1108-1137`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/layout_inference.cc#L1108-L1137)：

```cpp
int64_t reg_num = 0;
for (const auto &[buffer, layout] : tmp_layout_map) {
  if (auto frag = layout.as<Fragment>()) {
    int64_t frag_reg_num = 1;
    for (auto i : frag.value()->OutputShape())
      frag_reg_num *= *as_const_int(i);      // 该 fragment 每线程的元素数
    reg_num += frag_reg_num;                  // 累加所有 fragment 的寄存器占用
  }
}
if (reg_num < min_reg_num) { best_layout_map = tmp_layout_map; ... }
```

遇到 `LayoutConflictException` / `NormalizeIterException` / `LoopLayoutInjectiveException` 时该方案作废，换下一个根重试（[`layout_inference.cc:1094-1106`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/layout_inference.cc#L1094-L1106)）。

**结果落点** —— [`layout_inference.cc:1187-1199`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/layout_inference.cc#L1187-L1199) 的 `LayoutInferencer::VisitStmt_(Block)` 把 `layout_map` 贴成 `attr::kLayoutMap` 注解，并 `ICHECK` 每个 `local.framgent`（注：源码里 fragment 的 scope 字符串就是这么拼写的）buffer 都已被推理出布局：

```cpp
for (auto buffer : block->alloc_buffers)
  if (buffer.scope() == "local.framgent")
    ICHECK(result_.layout_map.count(buffer)) << "Cannot inference fragment layout for " << buffer;
block_ptr->annotations.Set(attr::kLayoutMap, result_.layout_map);
```

**pass 注册** —— [`layout_inference.cc:1253-1272`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/layout_inference.cc#L1253-L1272) 把整个流程注册为 `tl.LayoutInference`（`CreatePrimFuncPass`），先跑 `ParallelLoopTransformer` 融合并行循环，再调 `LayoutInferencer::Substitute` 完成推理与注解。

**一个具体算子：gemm 的硬约束** —— [`src/op/gemm.cc:594-675`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/gemm.cc#L594-L675) 的 `GemmNode::InferLayout` 按目标架构分流。以 Ampere/Turing/SM120 mma 路径为例（[`gemm.cc:630-667`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/gemm.cc#L630-L667)）：

```cpp
ICHECK(IsFragmentBuffer(c_)) << "MMA only supports C in local.fragment scope, got " << c_.scope();
auto fragment = makeGemmFragmentC(m_, n_, m_ / warp_m, n_ / warp_n, c_->dtype.bits());
results.Set(c_, fragment->BindThreadRange(thread_range));     // C 累加器布局: 硬约束
...
results.Set(a_, makeGemmABLayout(mat_stride, mat_continuous, ...));  // A shared: swizzle 布局
```

注意三件事：(1) C 必须是 fragment，否则直接报错——这就是 u2-l3 强调的「gemm 累加器必须是 fragment」的编译期根因；(2) C 的布局由 `makeGemmFragmentC(block_m, block_n, warp_m, warp_n, ...)` 生成，warp 切分 `(m_/warp_m, n_/warp_n)` 决定了每个 warp 负责的结果子块；(3) A/B 若在 shared 则给一个 swizzle 的 `Layout`（供 TMA/LDSM 用），若在 fragment 则给 `makeGemmFragmentA/B`。Hopper 的 wgmma 路径走 `makeGemmFragmentCHopper`（[`gemm.cc:668-675`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/gemm.cc#L668-L675)），形状规则不同。

**copy 的 TMA 约束** —— [`src/op/copy.cc:348-505`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/copy.cc#L348-L505) 的 `CopyNode::InferLayout` 会读 `disable_tma_lower` 配置决定是否走 TMA，并在 `kFree` 级别（[`copy.cc:468-505`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/copy.cc#L468-L505)）为还没定下布局的 shared 目标端挑一个 TMA 友好的 swizzle 布局——这正是「自由模式」要枚举的来源之一。

#### 4.2.4 代码实践：跟踪一个 matmul 的布局推理过程

**实践目标**：用日志开关观察 `LayoutInference` 内部「严格 → 常规 → 自由」三步分别产出了哪些 buffer 布局。

**操作步骤**：

1. 复制 `examples/quickstart.py` 的 matmul 部分（**示例代码，基于项目已有示例修改**）。
2. 在编译前设置 TileLang 的调试日志级别，让 `DLOG(INFO)` 输出（pass 内部大量用 `DLOG(INFO)` 打印推理过程）：

```python
import tilelang
import tilelang.language as T

tilelang.set_log_level("INFO")   # 打开内部 INFO 日志, 暴露 LayoutInference 的逐步推理

@tilelang.jit(out_idx=[2])
def matmul(M, N, K, block_M=32, block_N=32, block_K=32, dtype="float16", accum_dtype="float32"):
    @T.prim_func
    def gemm(A: T.Tensor((M, K), dtype), B: T.Tensor((K, N), dtype),
             C: T.Tensor((M, N), dtype)):
        with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=128) as (bx, by):
            A_shared = T.alloc_shared((block_M, block_K), dtype)
            B_shared = T.alloc_shared((block_K, block_N), dtype)
            C_local  = T.alloc_fragment((block_M, block_N), accum_dtype)
            T.clear(C_local)
            for k in T.Pipelined(T.ceildiv(K, block_K), num_stages=3):
                T.copy(A[by*block_M, k*block_K], A_shared)
                T.copy(B[k*block_K, bx*block_N], B_shared)
                T.gemm(A_shared, B_shared, C_local)
            T.copy(C_local, C[by*block_M, bx*block_N])
    return gemm

kernel = matmul(128, 128, 128)
```

3. 编译时观察 stderr。

**需要观察的现象**：

- `[InferLayout] all participating operators:` 列出参与推理的所有 op（gemm/copy/parallel）。
- `[InferInFreeMode] Final selection is attempt_infer_root = X`：最终选中的推理根。
- 各 buffer 的 `DebugOutput`，如 `A_shared` 是一个 swizzle 的 `Layout`、`C_local` 是一个 `Fragment(...)`。

**预期结果**：你能从日志里看到 `C_local` 被赋予一个 fragment 布局，其 `OutputShape` 乘积等于 `block_M*block_N / threads_per_warp_group` 量级（如 `32×32=1024` 元素、128 线程 → 每线程 8 槽）。**待本地验证**：精确表达式随 GPU 架构（Ampere mma vs Hopper wgmma）不同。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `T.gemm` 的 C 累加器布局是「严格约束」而不是「自由枚举」？

> **答**：C 的元素分布必须精确匹配 mma/wgmma 指令的输出分布，否则指令取不到正确的累加器寄存器，结果就错了。这是硬件硬性要求，不可协商，故在 `kStrict` 级别由 `makeGemmFragmentC` 直接给定。

**练习 2**：`InferInFreeMode` 为什么要把「共享同一 buffer 的 op」聚成一个连通分量再枚举，而不是逐个 op 独立选？

> **答**：共享同一 buffer 的 op 必须对该 buffer 达成**一致**的布局（一个 buffer 只能有一个布局）。若各自独立选会冲突，所以要把它们绑在一个分量里联合求解，并在分量内枚举「以哪个 op 为根」，再整体比较寄存器代价。

**练习 3**：`LayoutInference` 把结果贴成 `kLayoutMap` 注解后，为什么自己不直接生成 mma 指令？

> **答**：关注点分离。`LayoutInference` 只负责「求解布局」这一纯逻辑问题；把布局翻译成具体的 TMA/mma/cp.async 指令、重算访存下标，是下游 `LowerTileOp` 的职责（见 u3-l3）。这样布局推理算法与目标指令生成解耦，便于独立调试与扩展。

---

### 4.3 布局可视化

#### 4.3.1 概念说明：把看不见的寄存器分布画出来

fragment 的线程级布局是一组仿射表达式，直接读 `forward_thread` / `forward_index` 既枯燥又容易出错。TileLang 提供了一个**可视化 pass**，在 `LayoutInference` 之后、`LowerTileOp` 之前运行，把每个 fragment 的布局打印成人类可读的三行文本，或画成彩色示意图（PNG/PDF/SVG），展示「逻辑下标 ↔ 线程号 ↔ 寄存器槽」的对应关系。

> ⚠️ **避坑**：[`tilelang/analysis/layout_visual.py:47`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/analysis/layout_visual.py#L47) 的 docstring 里写的环境变量名 `TL_ENABLE_LAYOUT_VISUALIZATION` 是**过时且不准确**的。真正控制可视化的是两个 **pass 配置项**（见 [`tilelang/transform/pass_config.py:84-91`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/transform/pass_config.py#L84-L91)）：`tl.layout_visualization_enable`（开关）与 `tl.layout_visualization_formats`（格式）。文档 [`docs/tutorials/debug_tools_for_tilelang.md:179`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/docs/tutorials/debug_tools_for_tilelang.md#L179) 与官方示例用的也是这两个 pass 配置，以它们为准。

#### 4.3.2 核心流程

```
用户在 @tilelang.jit 里传 pass_configs={TL_LAYOUT_VISUALIZATION_ENABLE: True, ...FORMATS: "txt"}
        |
        v
LowerAndLegalize 流水线执行 LayoutInference  -> 每个 Block 贴上 kLayoutMap 注解
        |
        v  (phase.py:168-170, 紧跟在 LayoutInference 之后)
LayoutVisual(mod)  ->  读 should_enable_layout_visual() 与 get_layout_visual_formats()
        |
        v
_LayoutVisualVisitor 遍历每个 Block, 读 annotations["layout_map"]
        |
        v  对每个 Fragment:
print_fragment_format -> 打印 Shape / Thread / Index 三行 (txt)
plot_layout           -> 画 PNG/PDF/SVG 图 (非 txt 格式)
```

格式取值（来自 [`phase.py:79-104`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L79-L104)）：

| `FORMATS` 取值 | 行为 |
| --- | --- |
| 未设 / 空 | 默认仅 `["txt"]`（但仅当 `ENABLE=True` 才生效） |
| `"txt"` | 仅控制台文本 |
| `"png"` / `"pdf"` / `"svg"` | 仅对应格式图（`txt` 部分在 visitor 里被过滤，见下） |
| `"all"` | txt + png + pdf + svg |
| `"txt,svg"` | 逗号分隔多格式 |

#### 4.3.3 源码精读

**pass 配置键** —— [`tilelang/transform/pass_config.py:84-91`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/transform/pass_config.py#L84-L91) 定义 `TL_LAYOUT_VISUALIZATION_ENABLE` 与 `TL_LAYOUT_VISUALIZATION_FORMATS`，分别映射到字符串 `"tl.layout_visualization_enable"` / `"tl.layout_visualization_formats"`。

**使能判断 + 格式解析** —— [`tilelang/engine/phase.py:72-104`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L72-L104) 的 `should_enable_layout_visual` / `get_layout_visual_formats` 从 pass context 读配置；注意 `get_layout_visual_formats` 在未设时返回 `["txt"]`，并对非法格式抛 `ValueError`。

**挂载点** —— [`tilelang/engine/phase.py:107-111`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L107-L111) 的 `LayoutVisual` 是入口；[`phase.py:168-172`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L168-L172) 表明它在 `LayoutInference` 之后、`LowerTileOp` 之前被调用——这正是注解刚贴好、还没被消费的窗口：

```python
mod = tilelang.transform.LayoutInference()(mod)
LayoutVisual(mod)                            # 可视化: 读 layout_map 注解
mod = tilelang.transform.LowerTileOp()(mod)  # 之后才真正生成指令
```

**可视化 visitor** —— [`tilelang/analysis/layout_visual.py:32-78`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/analysis/layout_visual.py#L32-L78) 的 `_LayoutVisualVisitor` 在 `visit_block_` 里检查 `op.annotations["layout_map"]`，对每个 `Fragment` 去重后调 `print_fragment_format` 打印、并对非 txt 格式调 `plot_layout` 画图。注意构造函数 [`layout_visual.py:62`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/analysis/layout_visual.py#L62) 显式把 `"txt"` 从画图格式列表里剔除（`formats_list = [f for f in formats if f != "txt"]`），因为 txt 走 `print` 而非 `plot_layout`。

**三行文本的来源** —— [`layout_visual.py:9-29`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/analysis/layout_visual.py#L9-L29) 的 `print_fragment_format` 打印 `Shape`（`get_input_shape -> get_output_shape`）、`Thread`（`forward_thread`）、`Index`（`forward_index`）三行，正对应 4.1 节的 `Fragment` 字段。

**官方示例** —— [`examples/visual_layout_inference/visual_layout_inference.py:6-12`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/visual_layout_inference/visual_layout_inference.py#L6-L12) 展示了通过 `@tilelang.jit(..., pass_configs={...})` 打开可视化的标准写法：

```python
@tilelang.jit(
    out_idx=[-1],
    pass_configs={
        tilelang.PassConfigKey.TL_LAYOUT_VISUALIZATION_ENABLE: True,
        tilelang.PassConfigKey.TL_LAYOUT_VISUALIZATION_FORMATS: "svg",
    },
)
def matmul(M, N, K, block_M, block_N, block_K, ...):
    ...
```

#### 4.3.4 代码实践：解读 gemm 累加器布局如何匹配 mma 形状

**实践目标**：开启 txt 可视化，读懂 `C_local` 的三行输出，并解释它为何匹配 mma 指令的形状要求。

**操作步骤**：

1. 把官方示例的格式从 `"svg"` 改成 `"txt"`（避免需要画图依赖），其余不变：

```python
# 示例代码: 基于 examples/visual_layout_inference/visual_layout_inference.py 修改
@tilelang.jit(
    out_idx=[-1],
    pass_configs={
        tilelang.PassConfigKey.TL_LAYOUT_VISUALIZATION_ENABLE: True,
        tilelang.PassConfigKey.TL_LAYOUT_VISUALIZATION_FORMATS: "txt",
    },
)
def matmul(M=128, N=128, K=128, block_M=32, block_N=32, block_K=32,
           dtype=T.float16, accum_dtype=T.float32):
    @T.prim_func
    def gemm(A: T.Tensor((M, K), dtype), B: T.Tensor((K, N), dtype),
             C: T.Tensor((M, N), dtype)):
        with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=128) as (bx, by):
            A_shared = T.alloc_shared((block_M, block_K), dtype)
            B_shared = T.alloc_shared((block_K, block_N), dtype)
            C_local  = T.alloc_fragment((block_M, block_N), accum_dtype)
            T.clear(C_local)
            for k in T.Pipelined(T.ceildiv(K, block_K), num_stages=3):
                T.copy(A[by*block_M, k*block_K], A_shared)
                T.copy(B[k*block_K, bx*block_N], B_shared)
                T.gemm(A_shared, B_shared, C_local)
            T.copy(C_local, C[by*block_M, bx*block_N])
    return gemm

matmul()   # 编译即触发可视化打印
```

2. 运行后查看控制台输出。

**需要观察的现象 / 预期结果**：依据官方示例注释（[`visual_layout_inference.py:52-57`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/visual_layout_inference/visual_layout_inference.py#L52-L57)），`C_local` 的输出形如：

```
C_local inferenced layout:
  Shape: [32, 32] -> [8]
  Thread: _j // 16 * 64 + _i // 16 * 32 + _i % 8 * 4 + _j % 8 // 2
  Index:  [_j % 16 // 8 * 4 + _i % 16 // 8 * 2 + _j % 2]
```

**解读要点**（这是本实践的核心）：

- **`Shape: [32,32] -> [8]`**：逻辑 tile 是 `32×32 = 1024` 个元素；输出维度 `[8]` 表示**每个线程持 8 个寄存器槽**。校验：`1024 元素 / 8 槽 = 128 线程`，恰等于 `threads=128`。✓
- **`Thread` 表达式**：把逻辑坐标 `(i,j)`（即占位符 `_i, _j`）映射到线程号 `0..127`。注意它把 32×32 的 tile 切成了 `2×2` 个 `16×16` 子块（`_i//16`, `_j//16`），每个子块内部再按 `8×8` 的 mma 结果形状分配线程——这正是 Ampere `mma.m16n8k16` 系列「每 4 线程持一行」分布的体现。
- **`Index` 表达式**：给出该元素在所属线程的 8 个寄存器槽里的编号（`0..7`）。它回答「线程内的第几个寄存器」。
- **匹配 mma 形状**：mma 指令一次产生固定形状（如 `m16n8`）的结果，按固定模式散布到 warp 的 32 线程；`makeGemmFragmentC` 生成的上述布局正是为了让 `LowerTileOp` 把 `T.gemm` 翻译成 mma 时，C 的元素分布与指令输出分布逐元素对齐。

若你在 Hopper GPU 上运行，`Thread/Index` 表达式会不同（走 wgmma 的 `makeGemmFragmentCHopper`），但「逻辑元素总数 = 线程数 × 每线程槽数」这个守恒关系不变。**待本地验证**：具体表达式取决于你的 GPU 架构与 TileLang 版本。

#### 4.3.5 小练习与答案

**练习 1**：`LayoutVisual` 为什么必须放在 `LayoutInference` 之后、`LowerTileOp` 之前？

> **答**：它要读的 `layout_map` 注解是 `LayoutInference` 产出的；而 `LowerTileOp` 会消费这些注解并把高层 tile op 降级成低层指令（fragment buffer 可能被改写），之后布局信息就不再是原始形态。所以只有这两个 pass 之间的窗口能稳定读到完整的 fragment 布局。

**练习 2**：把 `FORMATS` 设成 `"png"` 时，为什么控制台不会打印三行文本？

> **答**：`_LayoutVisualVisitor.__init__` 里 `formats_list = [f for f in formats if f != "txt"]`，即 `"txt"` 之外的所有格式都只走 `plot_layout` 画图分支；而三行文本由 `print_fragment_format` 无条件打印——但根据 `phase.py`，当 `FORMATS` 显式设为非 txt 时，`get_layout_visual_formats` 返回的列表不含 `txt`……（实际行为：文本打印由 visitor 内的 `print_fragment_format` 触发，与 formats 列表是否含 txt 无关，它总会打印；formats 列表只决定是否额外画图）。**结论**：txt 文本总会打印，非 txt 格式决定是否**额外**出图。

---

## 5. 综合实践：用布局可视化诊断一个 fragment kernel

把本讲三个模块串起来。请完成以下任务：

1. **编写**一个比 quickstart 多一步的 kernel：在 `T.gemm` 之后、写回 global 之前，对 `C_local` 做一次 `T.reduce_max(C_local, C_max, dim=1, clear=True)`（沿行方向取最大值，`C_max` 是 `(block_M,)` 的 fragment）。参考 u2-l3 的 reduce 用法。
2. **开启可视化**：用 `pass_configs={TL_LAYOUT_VISUALIZATION_ENABLE: True, TL_LAYOUT_VISUALIZATION_FORMATS: "txt"}` 编译。
3. **解读输出**：
   - 找到 `C_local` 的布局，验证「元素总数 = 线程数 × 每线程槽数」守恒。
   - 找到 `C_max` 的布局。它是由 `LayoutReducer`（u3-l3 提到的、在 `LayoutInference` 之前运行的 reducer 预布局 pass）预先设定的，对比它与 `C_local` 布局的差异，思考 reduce 沿 dim=1 时为什么需要特定的线程分布（涉及跨线程 AllReduce）。
4. **回答**：如果你把 `block_N` 从 32 改成 64，`C_local` 的 `Shape` 输出维度（每线程槽数）会如何变化？先用守恒关系预测，再运行验证。

**预期**：你会看到 fragment 布局如何从 gemm 的硬约束传播到 reduce 的输入，并理解「布局推理 + reducer 预布局」是如何协同保证 reduce 能落到正确的跨线程规约指令上的。reduce 的具体布局路径见 u2-l3 与 `LayoutReducer`（本讲不展开）。

## 6. 本讲小结

- **布局 = 线程级映射**：`Layout` 记录「逻辑下标 → 线性位置」的仿射映射；`Fragment` 在其上多了「→ 线程号 + 线程内寄存器槽 + 复制份数」。这是描述 tensor core 所需数据分布的统一抽象。
- **推理而非指定**：`LayoutInference` 把每个 tile op 当作带约束的布局生产者，沿 buffer 使用关系传播求解。`InferLevel`（kStrict/kCommon/kFree）控制每个 op 愿意给出的约束强度。
- **四步算法**：浮动 buffer 全复制 → 严格约束（如 gemm 的 C）→ BFS 常规传播 → 自由模式枚举（连通分量内试每个根、选总寄存器代价最小的方案）→ 别名收尾。
- **结果落点**：布局以 `attr::kLayoutMap` 注解贴回每个 Block，`LayoutInference` 自己不改指令；真正的下标重算与 TMA/mma 指令生成由下游 `LowerTileOp` 完成。
- **可视化是 pass 配置**：用 `tl.layout_visualization_enable` + `tl.layout_visualization_formats`（通过 `@tilelang.jit(pass_configs=...)` 设置）打开，在 `LayoutInference` 与 `LowerTileOp` 之间打印 `Shape/Thread/Index` 三行或出图。`layout_visual.py` docstring 里的环境变量名是过时的，别被误导。
- **形状匹配指令**：fragment 的 `OutputShape` 乘积 = 逻辑元素数 / 线程数，它由 mma/wgmma 指令的固定输出分布决定（`makeGemmFragmentC` 系列），这是「gemm 累加器必须是 fragment」的编译期根因。

## 7. 下一步学习建议

- **向下看指令生成**：本讲的布局注解是如何被消费的？建议下一讲学 **u4-l2 软件流水线** 与 **u3-l3/u3-l4 的 `LowerTileOp`**，看 `LayoutInference` 的产物如何变成 TMA/mma 指令。
- **横向看 warp 特化**：Hopper 的 wgmma 配合 warp specialization 会引入更复杂的布局（生产 warp 与消费 warp 不同布局），对应 **u4-l3 Warp 特化与 Hopper wgmma**。
- **深入算子侧**：若你想为自己的 tile op 实现 `InferLayout`，精读 [`src/op/gemm.cc`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/gemm.cc) 与 [`src/op/copy.cc`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/copy.cc) 的 `InferLayout`，并对照 [`src/layout/gemm_layouts.cc`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/layout/gemm_layouts.cc) 里 `makeGemmFragmentC` 等工厂函数，理解不同 SM 架构的布局工厂差异。
- **调试技巧延伸**：阅读 [`docs/tutorials/debug_tools_for_tilelang.md`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/docs/tutorials/debug_tools_for_tilelang.md) 把布局可视化与 IR 打印、`T.print` 运行时打印结合起来，形成完整的 kernel 调试工作流。
