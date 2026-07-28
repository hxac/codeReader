# 内存布局推断 Layout/Fragment

## 1. 本讲目标

在 [u2-l4](u2-l4-memory-hierarchy.md) 中我们提到：用 `T.alloc_fragment` 分配的 fragment buffer，其逻辑下标与物理寄存器并不一一对应，而是由编译器自动把整块 tile 分发到各线程。本讲就来拆解「分发」这件事到底是怎么发生的。

学完本讲，你应该能够：

- 说清楚 **Layout** 和 **Fragment** 这两个数据结构分别描述什么、二者如何继承。
- 理解 **Forward / Inverse / Reshape** 三个核心操作在数学上做了什么、各自服务于编译的哪一步。
- 跟上 `tl.LayoutInference` 这个 pass 的执行流程：它是如何从一组 TileOp（Copy/Gemm/Parallel）反推出每个 fragment 的寄存器布局的。
- 理解 **swizzle 布局**为什么能消除 shared memory 的 bank conflict，以及它在源码里是怎么定义的。

本讲是 [u4-l1](u4-l1-lowering-pipeline.md) lowering 流程的延续，也是后续 [u4-l2](u4-l2-tileop-and-gemm-dispatch.md)（Gemm 分派）和 [u6](u6-l1-mma-intrinsics-overview.md)（张量核 intrinsics）的理论地基。

## 2. 前置知识

阅读本讲前，请确保理解以下概念（在前序讲义中均已建立）：

- **fragment 内存层级**：见 [u2-l4](u2-l4-memory-hierarchy.md)。`local.fragment` 对应寄存器文件，逻辑上是一个二维 tile，物理上被划分给一个 warp 内的所有线程。
- **TIR / PrimFunc / SBlock**：见 [u2-l1](u2-l1-prim-func-and-type-system.md)。TileLang kernel 在 `@T.prim_func` 重写后变成一棵 TIR，函数体由若干 `SBlock`（带 `alloc_buffers` 的块）和 `For` 循环组成。
- **TileOp 与 InferLayout**：见 [u4-l2](u4-l2-tileop-and-gemm-dispatch.md)。每个 tile 算子（Copy/Gemm/Parallel）都继承自 `TileOperatorNode`，其 `InferLayout()` 方法能根据输入张量的布局推断出输出 fragment 该用什么布局。
- **lowering 流水线**：见 [u4-l1](u4-l1-lowering-pipeline.md)。`tl.LayoutInference` 是设备侧 pass 流水线里的一环，发生在 host/device 拆分之后、codegen 之前。

如果你对 GPU shared memory 的 bank（存储银行）概念不熟，可以这样理解：shared memory 被切成 32 个 bank，连续 4 字节轮流分给不同 bank。如果一个 warp 的 32 个线程同时访问落在**同一个 bank** 的不同地址，就会发生 **bank conflict**，访问被串行化、带宽骤降。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/layout/layout.h` | 定义 `LayoutNode` / `FragmentNode` 两个核心类的接口，以及布局相关 attribute 字符串常量。 |
| `src/layout/layout.cc` | 实现 `Layout` / `Fragment` 的构造、`Forward` / `Inverse` / `Reshape` / `Repeat` 等方法，并注册到 FFI 供 Python 调用。 |
| `src/layout/swizzle_mode.h` / `.cc` | 定义 `SwizzleMode` 枚举（NONE / 32B / 64B / 128B）及其对硬件 descriptor 字段的投影。 |
| `src/layout/gemm_layouts.cc` | 实现 `MakeFullBankSwizzleLayout` 等具体 swizzle 布局，以及 `DetectSwizzleMode` / `MergeSwizzleLayouts`。 |
| `src/transform/layout_inference.cc` | 本讲主角：`tl.LayoutInference` pass 的全部实现，包含布局收集器、推断器、IR 改写器。 |
| `tilelang/layout/layout.py` | `Layout` 的 Python 包装，提供 `inverse()` / `reshape()` 等便捷方法。 |
| `examples/plot_layout/layout_swizzle.py` | 实践用例：用 `plot_layout` 可视化三种 swizzle 布局。 |

## 4. 核心概念与源码讲解

### 4.1 Layout 与 Fragment 的数学模型

#### 4.1.1 概念说明

先建立一个直觉：所谓「布局」，本质上就是一个**索引映射函数**。

用户在 kernel 里写的是逻辑坐标——比如一个 16×16 的 fragment tile 上的 `(i, j)`。但寄存器文件是线性的、按线程私有的一维数组组织的。Layout 要回答的问题就是：

> 给定逻辑坐标 \((i, j)\)，它在物理上落在「第几号输出槽」？

最简单的布局是行优先线性映射：

\[
\text{index} = i \times W + j
\]

其中 \(W\) 是 tile 的列数。但 TileLang 要支持 MMA/WGMMA/MFMA 这类张量核指令，这些指令对数据在寄存器里的排布有严格要求（比如 `mma.m16n8k16` 要求 A 矩阵的 8 行被打散到 warp 的 lane 0~7）。所以 Layout 必须能表达任意仿射甚至带 XOR 的映射。

**Layout** 描述的就是这种纯索引函数。而 **Fragment** 是 Layout 的子类，它额外多了一维信息：每个逻辑元素**归哪个线程所有**（`forward_thread`），以及这份数据被复制了几份（`replicate_size`）。文档里那句「derive a Layout object `T.Fragment`, which determines how to allocate the corresponding register files for each thread」说的正是这个 Fragment——[docs/get_started/overview.md:82](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/get_started/overview.md#L82)。

#### 4.1.2 核心流程

一个 Layout 在 C++ 侧只由两个字段决定：

- `input_size_`：输入形状，即逻辑 tile 的各维大小。
- `forward_index_`：一组关于「占位变量」的 `PrimExpr`，描述如何把输入坐标映射到输出坐标。

这里的「占位变量」是关键设计。`layout.cc` 里预分配了一组符号变量作为通配符：

```cpp
Var InputPlaceholder(size_t idx) {
  return getPlaceholder(std::string{'_', char('i' + idx)});
}
```

也就是说，`InputPlaceholder(0)` 返回名为 `_i0` 的 `Var`，`InputPlaceholder(1)` 返回 `_i1`，依此类推（最多 16 个）。Fragment 还多一个 `_rep`（`ReplicationPlaceholder()`）表示复制维度。

`forward_index_` 里的表达式就是用这些占位符写成的，比如行优先布局的 `forward_index_` 就是 `[_i0 * W + _i1]`（单维输出，已被展平）。当真正要用这个布局时，调用 `Forward(vars)` 把占位符替换成真实下标即可——[src/layout/layout.cc:398-426](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/layout/layout.cc#L398-L426)。

输出形状 `OutputShape()` 不是显式存储的，而是对 `forward_index_[i] + 1` 做整数区间分析（`analyzer.int_set(...)`）反推出来的——[src/layout/layout.cc:367-396](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/layout/layout.cc#L367-L396)。

#### 4.1.3 源码精读

`LayoutNode` 和 `FragmentNode` 的类定义在 `layout.h` 里，是典型的「基类 + 派生类」结构：

```cpp
class LayoutNode : public ffi::Object {
public:
  LayoutNode(ffi::Array<PrimExpr> input_size,
             ffi::Array<PrimExpr> forward_index);
  size_t InputDim() const { return input_size_.size(); }
  size_t OutputDim() const { return forward_index_.size(); }
  // ...
protected:
  ffi::Array<PrimExpr> forward_index_;
  ffi::Array<PrimExpr> input_size_;
};

class FragmentNode : public LayoutNode {
  // ...
protected:
  Range thread_range_;
  PrimExpr forward_thread_;
  PrimExpr replicate_size_;
};
```

Fragment 多出的 `forward_thread_` 是一个关于占位符的表达式，给定逻辑坐标 \((i,j)\)（以及可选的复制维 `_rep`），返回**线程号**。构造函数如下——[src/layout/layout.cc:792-806](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/layout/layout.cc#L792-L806)：

```cpp
FragmentNode::FragmentNode(Array<PrimExpr> input_size,
                           Array<PrimExpr> forward_index,
                           PrimExpr forward_thread, PrimExpr replicate_size) {
  input_size_ = input_size;
  replicate_size_ = replicate_size;
  // ...
  forward_thread_ = analyzer.Simplify(forward_thread);
  if (forward_index.empty()) {
    // 如果没给 forward_index，就根据 forward_thread 自动推导一个
    forward_index = {infer_fragment_index(GetVarMap(), forward_thread_, &analyzer)};
  }
  // ...
}
```

注意这个细节：Fragment 的 `forward_index`（每个线程内部的局部寄存器槽位）允许留空，构造时会调用 `infer_fragment_index` 从 `forward_thread` 自动推导——[src/layout/layout.cc:773-790](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/layout/layout.cc#L773-L790)。这说明 `forward_thread` 与 `forward_index` 之间有冗余，给定其一通常能推出另一个。

还有一个特殊的便捷构造——**全复制** Fragment，每个线程持有一份完整拷贝（常用于 index/mask buffer，所有线程都要读同一个值）：

```cpp
Fragment Fragment::FullyReplicated(Array<PrimExpr> shape, PrimExpr thread_extent) {
  return Fragment(shape, {}, ReplicationPlaceholder(), thread_extent, std::nullopt)
      ->BindThreadRange(Range(0, thread_extent));
}
```

这里 `forward_thread = _rep`，意思是「线程号就等于复制维下标」，于是所有线程都拿到全部元素——[src/layout/layout.cc:844-849](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/layout/layout.cc#L844-L849)。

#### 4.1.4 代码实践

实践目标：用 Python 直接构造并打印一个 Layout，建立对「布局 = 索引函数」的直觉。

操作步骤（这是源码阅读 + 轻量运行型实践，无需 GPU）：

1. 写一个最小脚本 `my_layout_demo.py`：
   ```python
   import tilelang.language as T
   # 行优先布局：4x4 tile，逻辑(i,j) -> 物理展平坐标 i*4+j
   layout = T.Layout([4, 4], lambda i, j: (i * 4 + j,))
   print("input_shape :", layout.get_input_shape())
   print("output_shape:", layout.get_output_shape())
   print("forward_index:", layout.get_forward_index())
   print("map (2,3)   ->", layout.map_forward_index([2, 3]))
   ```
2. 运行 `python my_layout_demo.py`。

需要观察的现象：
- `get_forward_index()` 打印出的表达式里应该能看到占位变量（形如 `_i0`, `_i1`）。
- `map_forward_index([2,3])` 应返回 `[11]`（因为 \(2\times4+3=11\)）。
- `output_shape` 应为 `[16]`（4×4=16 个展平槽位）。

预期结果：你会直观看到 Layout 内部并不是存一张「表」，而是存一个带占位符的符号表达式，查询时才把真实下标代入求值。

> 待本地验证：若当前环境未安装 `tilelang`，可先按 [u1-l2](u1-l2-build-and-install.md) 完成构建；纯 CPU、`target=llvm` 即可运行，不依赖 GPU。

#### 4.1.5 小练习与答案

**练习 1**：`LayoutNode` 有 `InputDim()` 和 `OutputDim()` 两个方法。对一个「16×16 tile 展平成一维」的布局，二者分别是多少？

答案：`InputDim() = 2`（输入是二维坐标 `(i,j)`），`OutputDim() = 1`（输出是一维展平坐标）。输出维数等于 `forward_index_` 数组的长度。

**练习 2**：Fragment 相比 Layout 多了哪两个字段？它们分别描述什么？

答案：多了 `forward_thread_`（逻辑坐标 → 线程号）和 `replicate_size_`（数据被复制的份数）。前者决定 tile 如何切给 warp 内各线程，后者用于全复制场景。

---

### 4.2 Forward / Inverse / Reshape

#### 4.2.1 概念说明

有了布局对象，编译器在两个方向上都要做坐标变换：

- **正向（Forward）**：从逻辑坐标 → 物理坐标。比如生成「线程 T 把寄存器槽 L 写回 shared memory 的 (i,j)」这类代码时，需要把 (i,j) 映射成物理位置。
- **逆向（Inverse）**：从物理坐标 → 逻辑坐标。codegen 阶段更常用——当我们要遍历一个 fragment 的所有寄存器槽时，需要反查每个槽位对应哪个逻辑元素，才能正确发射 load/store 指令。

而 **Reshape** 解决的是另一个问题：当同一个底层存储被以不同形状或不同 dtype「重看」一遍时（比如把 `[16,16]` 的 fp16 buffer 重解释成 `[8,32]`，或 `f32 → i8` 改变位宽），如何在不破坏物理排布的前提下换一套逻辑形状。

#### 4.2.2 核心流程

**Forward** 的实现很直白——把占位符替换成真实下标再化简（[src/layout/layout.cc:398](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/layout/layout.cc#L398) 已贴过）。它支持「末尾 InputDim 个变量参与变换、前面其余变量原样透传」的约定，方便组合布局。

**Inverse** 则借助了 TVM 的 `arith` 模块。核心思路是：把 `forward_index_` 当成一个仿射迭代映射，先用 `DetectIterMap` 检测它是否可逆、再用 `InverseAffineIterMap` 求出逆映射——[src/layout/layout.cc:597-651](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/layout/layout.cc#L597-L651)：

```cpp
std::pair<Layout, arith::IterMapLevel>
LayoutNode::InverseWithLevel(bool require_padding_guard) const {
  // ...
  auto level = (is_static_shape && !require_padding_guard)
                   ? arith::IterMapLevel::Bijective
                   : arith::IterMapLevel::NoCheck;
  arith::IterMapResult res =
      arith::DetectIterMap(forward_index_, GetVarMap(), 1, level, &analyzer);
  // ...
  auto inv = arith::InverseAffineIterMap(res->indices, outputs);
  // ...
  return {Layout(outputs_shape, backward_index), level};
}
```

这里有个重要设计——`level` 取值：

- **Bijective（双射）**：当所有形状都是静态常量时，要求映射严格一一对应，可放心求逆。
- **NoCheck**：当存在符号维（动态形状）或需要 padding guard 时，放宽检查，依赖后续 runtime guard 保证安全。

对于 **Fragment**，它的 `Inverse` 实现是个巧妙的复用：把 `forward_thread` 当成「额外的一个输入维」，拼接到普通 Layout 上再调基类的 `InverseWithLevel`——[src/layout/layout.cc:944-954](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/layout/layout.cc#L944-L954)：

```cpp
std::pair<Layout, arith::IterMapLevel>
FragmentNode::InverseWithLevel(bool require_padding_guard) const {
  auto input_size_copy = input_size_;
  input_size_copy.push_back(ReplicateExtent());          // 多加一个复制维
  auto forward_index_copy = forward_index_;
  forward_index_copy.push_back(                            // 多加一个输出：线程号
      Substitute(forward_thread_,
                 {{ReplicationPlaceholder(), InputPlaceholder(InputDim())}}));
  auto fwd = Layout(input_size_copy, forward_index_copy);
  return fwd->InverseWithLevel(require_padding_guard);    // 复用基类求逆
}
```

换句话说，Fragment 的求逆 =「把线程维临时并入普通 Layout」+「调用基类求逆」。这是面向对象复用的典型用法。

**Reshape** 则要处理位宽变化。它的核心约束是一个守恒律——[src/layout/layout.cc:653-709](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/layout/layout.cc#L653-L709)：

\[
\prod \text{InputShape} \times \text{rescale\_num} \;=\; \prod \text{shape} \times \text{rescale\_den}
\]

其中 `rescale_num / rescale_den` 是新旧元素位宽之比。例如 `f32 → i8`（32b→8b）用 `rescale_num=32, rescale_den=8`，元素数变 4 倍但物理字节不变。实现上先把新形状的下标展平成 `flat_index`，再按比例折算回旧下标，代入原 `forward_index_`：

```cpp
PrimExpr flat_index = ComputeFlatIndex(shape, new_vars);
PrimExpr old_flat_index = floordiv(flat_index * rescale_den, rescale_num);
Array<PrimExpr> original_indices =
    RecoverOriginalIndices(InputShape(), old_flat_index);
Array<PrimExpr> new_forward_index = SubstituteForwardIndex(
    forward_index_, InputShape(), original_indices, az);
```

对于 fp4 这类**亚字节（sub-byte）** dtype（两个 fp4 共占一字节），普通展平会丢失「同一字节内的第几个元素」信息，于是 `TryPackedSubtypeReshape` 会额外增加一个「pack lane」输出维来保留这个结构——[src/layout/layout.cc:112-183](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/layout/layout.cc#L112-L183)。

#### 4.2.3 源码精读

除了三大操作，Layout 还提供一组「组合算子」用来从小的「原子」布局拼出大布局：

- **Repeat(dim, factor)**：沿某一维重复 `factor` 次，并在输出最前面加一个「重复组号」维度——[src/layout/layout.cc:428-466](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/layout/layout.cc#L428-L466)。
- **Expand(leading_shape)**：在最前面加几个「原样透传」的维度——[src/layout/layout.cc:468-508](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/layout/layout.cc#L468-L508)。
- Fragment 还有 **Replicate(n)** / **DeReplicate()** / **CondenseReplicateVar()** 用来调整复制维——[src/layout/layout.cc:556-589](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/layout/layout.cc#L556-L589)。

这些算子的共同模式是：构造新的占位符变量、建立新旧占位符之间的映射 `vmap`、用 `Substitute` 把旧表达式里的占位符替换掉。理解了 `Reshape`，再看这些就是同一套思路的变体。

所有这些方法最后都通过 FFI 注册暴露给 Python，例如 `tl.Layout_inverse`、`tl.Layout_reshape`——[src/layout/layout.cc:1070-1171](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/layout/layout.cc#L1070-L1171)。Python 侧 `tilelang/layout/layout.py` 的 `Layout.inverse()` / `reshape()` 就是薄包装。

#### 4.2.4 代码实践

实践目标：观察 Forward 与 Inverse 互为逆运算。

操作步骤：

1. 在上一节的脚本基础上继续：
   ```python
   import tilelang.language as T
   L = T.Layout([4, 4], lambda i, j: (i * 4 + j,))
   # 正向：(2,3) -> 11
   fwd = L.map_forward_index([2, 3])
   print("forward (2,3) ->", fwd)
   # 求逆，再正向验证
   inv = L.inverse()
   print("inverse layout:", inv)
   print("inverse map 11 ->", inv.map_forward_index([11]))
   ```
2. 运行并观察。

需要观察的现象：
- `inv.map_forward_index([11])` 应返回 `[2, 3]`，与正向输入一致——说明 Forward 与 Inverse 互逆。
- `inv` 的 `input_shape` 是 `[16]`，`output_shape` 是 `[4,4]`，正好和 `L` 颠倒。

预期结果：你亲手验证了「布局是一对互逆的索引函数」这一核心事实，这正是 codegen 能在物理寄存器与逻辑坐标间自由换算的基础。

> 待本地验证：具体打印格式可能因 TVM 版本略有差异，但互逆关系应当成立。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Fragment 的 `Inverse` 要把 `forward_thread` 拼成「额外一个输出维」再调基类求逆，而不是单独写一套求逆逻辑？

答案：因为「线程号」本质上也是逻辑坐标到物理空间（线程 × 局部槽位）映射的一部分。把它并入后，Fragment 的求逆就退化成普通 Layout 的求逆，复用 TVM 已有的 `DetectIterMap` + `InverseAffineIterMap` 实现，避免重复造轮子。

**练习 2**：把一个 `[16,16]` 的 fp32 buffer（每元素 32 bit）reshape 成 `[16,64]` 的 int8（每元素 8 bit），`rescale_num` 和 `rescale_den` 分别取多少？

答案：`rescale_num=32, rescale_den=8`。元素数从 256 变 1024（×4），但总比特数不变（\(256\times32 = 1024\times8 = 8192\)）。守恒律 \(256\times32 = 1024\times8\) 成立。

---

### 4.3 layout_inference pass

#### 4.3.1 概念说明

前面两节讲的是「布局对象本身长什么样」。本节回答一个更关键的问题：**这些 Fragment 布局是谁、怎么、什么时候填出来的？**

答案就是 `tl.LayoutInference` 这个 pass。它的职责是：扫一遍 device PrimFunc，对其中所有 `local.fragment` buffer，推断出一个 Fragment 布局，并把这个布局表挂到对应的 `SBlock` 注解上，供后续 codegen 使用。

文档对它的定位是：「TileLang uses a Layout Inference Pass during compilation to derive a Layout object `T.Fragment`, which determines how to allocate the corresponding register files for each thread」——[docs/get_started/overview.md:82](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/get_started/overview.md#L82)。

#### 4.3.2 核心流程

整个 pass 分成「收集 → 推断 → 改写」三阶段，主入口是一个 PrimFunc pass——[src/transform/layout_inference.cc:1311-1324](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/layout_inference.cc#L1311-L1324)：

```cpp
tvm::transform::Pass LayoutInference() {
  auto pass_func = [=](PrimFunc f, const IRModule &m, const PassContext &ctx) {
    ThreadBindingCollector collector;
    collector(f->body);
    bool has_thread_binding = !collector.thread_binding_.empty();
    bool skip_thread_partition = !has_thread_binding;
    f = LayoutInferencer::Substitute(std::move(f), skip_thread_partition);
    ParallelLoopLayoutValidator::Validate(f->body);
    return f;
  };
  return CreatePrimFuncPass(pass_func, 0, "tl.LayoutInference", {});
}
```

`LayoutInferencer::Substitute` 内部依次做三件事——[src/transform/layout_inference.cc:1206-1216](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/layout_inference.cc#L1206-L1216)：

1. 先跑 `ParallelLoopFuser::Fuse` 合并可融合的 parallel 循环。
2. `BufferUseDefCollector::Collect(f)` 遍历 IR，把所有 TileOp 和它用到的 fragment buffer 收集起来，建立 use-def 关系。
3. `collector.Run()` 执行真正的布局推断，返回 `LayoutInferenceResult`。
4. `LayoutInferencer` 作为 mutator 再走一遍 IR，把结果挂到注解上。

**`Run()` 的四步推断**是最有意思的部分——[src/transform/layout_inference.cc:333-488](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/layout_inference.cc#L333-L488)：

| 步骤 | InferLevel | 做什么 |
|------|-----------|--------|
| step 0 | — | 给「浮动」fragment（在 TileOp 之外被访问的 buffer，比如 `if mask[i]`）赋全复制布局 |
| step 1 | `kStrict` | 严格推断：每个 op 必须给出确定布局，冲突即报错 |
| step 2 | `kCommon` | BFS 队列传播：有已知布局 anchor 的 op 优先入队（`EnqueueWithPriority`） |
| step 3 | `kFree` | 松弛重跑：对每个连通分量，尝试每个 op 当根，挑**寄存器数最少**的方案 |
| step 4 | — | 收尾：把同存储 Var 的别名 buffer 用 `Reshape` 对齐 |

每个 TileOp 的 `InferLayout()` 返回一组 `{buffer: layout}` 更新，`RunInferStep` 负责把这些更新并入 `layout_map`——[src/transform/layout_inference.cc:116-313](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/layout_inference.cc#L116-L313)。若同一 buffer 被两个 op 推出不一致布局，先尝试合并 swizzle（见 4.4），合并失败则 `LOG(FATAL)` 报冲突。

step 3 的 `InferInFreeMode` 是「最优化」环节：它用并查集（`UnionFind`）把共享 buffer 的 op 连成连通分量，对每个分量枚举每个 op 作推断起点，统计该方案下所有 fragment 的总寄存器数（各 Fragment `OutputShape` 乘积之和），取最小者——[src/transform/layout_inference.cc:1047-1201](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/layout_inference.cc#L1047-L1201)。这是 TileLang 自动选择较省寄存器布局的关键。

最终 `LayoutInferencer` 把 `layout_map` 挂到 `SBlock` 的 `attr::kLayoutMap` 注解上，把 parallel 循环的布局挂到 `For` 的 `attr::kParallelLoopLayout`——[src/transform/layout_inference.cc:1239-1298](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/layout_inference.cc#L1239-L1298)。这两个 attribute 字符串定义在 [src/layout/layout.h:319-331](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/layout/layout.h#L319-L331)。

伪代码概括整个 pass：

```
输入: device PrimFunc（含若干 SBlock + fragment buffer + TileOp）
1. Fuse 并行循环
2. 遍历 IR，收集 TileOp 列表 infer_list_ 与 use-def 表 use_list_
3. Run():
   step0: 浮动 fragment → FullyReplicated
   step1: kStrict 推断（强制一致）
   step2: kCommon BFS 传播（带优先级）
   step3: kFree 枚举根，选最少寄存器方案
   step4: 别名 buffer 用 Reshape 对齐
4. mutator: 把 layout_map 写进 SBlock.annotation["layout_map"]
            把 loop_layout 写进 For.annotation["parallel_loop_layout"]
输出: 带 layout_map 注解的 PrimFunc
```

#### 4.3.3 源码精读

收集阶段有几个值得注意的细节。`BufferUseDefCollector` 继承自 `IRVisitorWithAnalyzer`，它在访问 IR 时：

- 遇到 `Call` 节点就 `ParseOperator` 判断是不是 TileOp（Copy/Gemm/Reduce…），是的话加入 `infer_list_`，并把参数里出现的 fragment buffer 登记到 `use_list_`——[src/transform/layout_inference.cc:554-617](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/layout_inference.cc#L554-L617)。
- 遇到 `For` 且 `kind == kParallel`，构造一个 `ParallelOp` 并收集它的访问顺序——[src/transform/layout_inference.cc:678-758](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/layout_inference.cc#L678-L758)。
- 遇到 `SBlock` 且带 `kLayoutMap` 注解（用户/前置 pass 显式标注的布局），直接作为 `annotated_layout_map_` 起点——[src/transform/layout_inference.cc:760-810](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/layout_inference.cc#L760-L810)。

还有一个 metax 分支相关的细节：如果 Copy 带了 `is_async_copy` 注解且 target 是 MACA，目标 buffer 会被记入 `maca_async_copy_buffers_`，在推断时**跳过**（MACA 异步拷贝的 buffer 不参与普通布局推断）——[src/transform/layout_inference.cc:604-608](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/layout_inference.cc#L604-L608)。这是 MACA 后端在公共 pass 里留下的一处针对性逻辑，后续 [u7-l4](u7-l4-maca-pipeline.md) 会展开。

BFS 的优先级策略也值得一提：`EnqueueWithPriority` 会把「操作的输入 buffer 已有布局」的 op 放到队头（`push_front`），其余放队尾（`push_back`）——[src/transform/layout_inference.cc:537-552](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/layout_inference.cc#L537-L552)。这能让推断沿着数据依赖「顺流而下」，更快收敛、减少冲突。

#### 4.3.4 代码实践

实践目标：跟踪 GEMM 的 layout inference，确认 fragment buffer 最终拿到了布局。

操作步骤（源码阅读 + 日志型实践）：

1. 打开 [examples/gemm/example_gemm.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm.py)，找到其中 `T.alloc_fragment` 的累加器 `C_local`。
2. 在编译前设置环境变量打开 debug 日志（源码里大量使用 `DLOG(INFO)`，仅在 debug 构建下输出）：
   ```bash
   export TVM_LOG_DEBUG=1
   python examples/gemm/example_gemm.py
   ```
3. 在日志里搜索 `[InferLayout]`、`Enforced layout maps`、`processing component` 等关键字。

需要观察的现象：
- 日志会列出所有参与推断的 TileOp（`[InferLayout] all participating operators`）。
- `Enforced layout maps` 段会打印每个 buffer 推断出的 `Fragment(...)`，能看到 `forward_thread`、`replicate`、`thread` 等字段。
- `processing component` 段能看到 step 3 自由模式对每个连通分量的枚举过程。

预期结果：你会看到 `C_local` 这类 fragment buffer 被赋予了一个描述「16×16 tile 如何分给 warp 内 32 个线程」的 Fragment，这正是 4.1 节抽象模型在真实 GEMM 里的具象化。

> 待本地验证：`DLOG` 仅在 debug 模式编译时输出；release 构建下看不到。若日志为空，请确认按 [u1-l2](u1-l2-build-and-install.md) 用 debug 模式编译（`cmake -DCMAKE_BUILD_TYPE=Debug`）。

#### 4.3.5 小练习与答案

**练习 1**：`Run()` 的 step 3（`InferInFreeMode`）为什么要「枚举每个 op 当根、取寄存器最少的方案」？直接固定从第一个 op 开始推断不行吗？

答案：因为同一个连通分量内，从不同 op 出发会得到不同的布局方案，寄存器占用也不同。固定起点可能恰好选到一份「每个线程持有较多寄存器」的方案，浪费寄存器。枚举所有起点再取最小，是一种贪心的寄存器占用最小化，能在不改变语义的前提下压低寄存器压力。

**练习 2**：如果一个 fragment buffer 在 TileOp 之外（比如 `if` 条件里）被访问，pass 会怎么处理？为什么？

答案：会被 `ComputeFloatingFragmentBuffers` 识别为「浮动」buffer，在 step 0 赋予 `FullyReplicated` 布局（每个线程持完整拷贝）。因为这种访问的访问模式无法从 TileOp 语义推断，全复制是唯一安全的选择——代码注释 [src/transform/layout_inference.cc:912-931](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/layout_inference.cc#L912-L931) 有完整解释。

---

### 4.4 swizzle 布局

#### 4.4.1 概念说明

swizzle（搅动/重排）是 GPU kernel 里消除 shared memory bank conflict 的经典手段。本节讲它在 TileLang 里是怎么用 Layout 来表达的。

先说清楚问题。GPU shared memory 有 32 个 bank，每个 bank 每周期服务一个地址。考虑一个 8 行 × N 列的 fp16 tile 存在 shared memory：同一行相邻元素轮流分到不同 bank，但**同一列的元素往往落在同一个 bank**。而 MMA/ldmatrix 这类指令恰恰喜欢按列读取（一次读 8 个片段），结果 8 个并发读全打到同一个 bank，发生严重 bank conflict。

swizzle 的解法是：在写入 shared memory 时，对列号做一个**与行号相关的 XOR**，让同一列的元素被打散到不同 bank。读取时做同样的 XOR 还原。因为 XOR 是自反的（\(a \oplus b \oplus b = a\)），数据语义不变，但物理落点被「搅匀」了。

XOR 的位数决定了 swizzle 的粒度，对应硬件 descriptor 的不同模式：32B / 64B / 128B。

#### 4.4.2 核心流程

TileLang 用一个 `SwizzleMode` 枚举统一描述这几种粒度——[src/layout/swizzle_mode.h:26-95](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/layout/swizzle_mode.h#L26-L95)：

```cpp
class SwizzleMode : public ffi::Enum {
public:
  static SwizzleMode None() { return Get("NONE"); }
  static SwizzleMode Swizzle32B() { return Get("SWIZZLE_32B"); }
  static SwizzleMode Swizzle64B() { return Get("SWIZZLE_64B"); }
  static SwizzleMode Swizzle128B() { return Get("SWIZZLE_128B"); }
  // none->1, 32B->32, 64B->64, 128B->128
  int ByteWidth() const { /* ... */ }
  // shared memory 基址对齐要求（字节）
  int SmemAlignment() const { /* ... */ }
};
```

枚举的声明顺序决定了它们的稠密序数（0..3），恰好等于 CUDA 的 `CU_TENSOR_MAP_SWIZZLE_*` 值——[src/layout/swizzle_mode.cc:18-30](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/layout/swizzle_mode.cc#L18-L30)。`ByteWidth()` 给出 swizzle 粒度字节数，`SmemAlignment()` 给出 shared memory 基址必须对齐到的边界（粒度 × 8 行），否则硬件会以错相应用 swizzle 导致数据错乱——[src/layout/swizzle_mode.h:73-85](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/layout/swizzle_mode.h#L73-L85)。

具体的三种 swizzle 布局在 `gemm_layouts.cc` 实现。以「全 bank」(128B, 3-bit XOR) 为例——[src/layout/gemm_layouts.cc:582-600](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/layout/gemm_layouts.cc#L582-L600)：

```cpp
static Layout MakeFullBankSwizzleLayout2D(int stride, int continuous,
                                          int element_size) {
  // Swizzle 3 bit
  Var i = InputPlaceholder(0);
  Var j = InputPlaceholder(1);
  int vector_size = 128 / element_size;
  PrimExpr ts = FloorDiv(i, 8);            // 行的 8 行块号
  PrimExpr s = FloorMod(i, 8);             // 块内行号 0..7
  PrimExpr tc = FloorDiv(FloorDiv(j, vector_size), 8);
  PrimExpr c = FloorMod(FloorDiv(j, vector_size), 8);
  PrimExpr vec = FloorMod(j, vector_size);
  PrimExpr c_swizzle = xor8x8(c, s);       // 关键：列块号 XOR 行号
  PrimExpr index = vec + (c_swizzle + s * 8) * vector_size;
  return Layout(Array<PrimExpr>{stride, continuous}, {tc, ts, index});
}
```

核心就是 `c_swizzle = xor8x8(c, s)`：把列块号 `c` 与块内行号 `s` 做 8×8 的 XOR。这样第 0 行列块 `c` 落在 bank `c`，第 1 行落在 `c⊕1`，…，第 7 行落在 `c⊕7`，于是同一列在 8 行里被均匀打散到 8 个不同 bank。

- **Quarter-bank (32B, 1-bit XOR)**：`xor2x2(c, s/4)` —— [src/layout/gemm_layouts.cc:522-542](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/layout/gemm_layouts.cc#L522-L542)
- **Half-bank (64B, 2-bit XOR)**：`xor4x4(c, s/2)` —— [src/layout/gemm_layouts.cc:553-571](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/layout/gemm_layouts.cc#L553-L571)

XOR 位数越多，打散能力越强，但对形状对齐的要求也越严（`continuous` 必须是 `vector_size × {2,4,8}` 的倍数）。

#### 4.4.3 源码精读

swizzle 在 layout inference 里不是「显式标注」的，而是通过**模式匹配**识别。`DetectSwizzleMode` 把一个布局与三种标准 swizzle 布局做结构相等比较，命中哪个就返回对应模式——[src/layout/gemm_layouts.cc:1083-1114](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/layout/gemm_layouts.cc#L1083-L1114)：

```cpp
SwizzleMode DetectSwizzleMode(const Layout &layout, const Buffer &buffer) {
  // ...
  if (stride_ok && info.continuous % (vector_size * 2) == 0) {
    if (StructuralEqual()(layout, MakeQuarterBankSwizzleLayout(buffer))) {
      return SwizzleMode::Swizzle32B();
    }
  }
  // ... Half (64B) ... Full (128B) ...
  return SwizzleMode::None();
}
```

当 layout inference 里两个 op 对同一 shared buffer 推出的 swizzle 不一致时，`MergeSwizzleLayouts` 会取**更小粒度**的那个作为折中，而不是直接报错——[src/layout/gemm_layouts.cc:1116-1143](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/layout/gemm_layouts.cc#L1116-L1143)。这个合并逻辑被 4.3 节 `RunInferStep` 在检测到 swizzle 冲突时调用——[src/transform/layout_inference.cc:265-275](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/layout_inference.cc#L265-L275)。

值得注意的是：swizzle 适用于 **shared** buffer（不是 fragment），因为它优化的是 shared memory 的 bank 访问。fragment（寄存器）不存在 bank conflict 概念。

#### 4.4.4 代码实践

实践目标：用 `plot_layout` 可视化 swizzle 布局，亲眼看到 XOR 如何把同列元素打散。

操作步骤（本讲的**核心实践**，对应任务要求的「结合 examples/plot_layout 可视化 GEMM 的 fragment 布局，解释 swizzle 如何改善 bank conflict」）：

1. 直接运行仓库自带的可视化脚本——[examples/plot_layout/layout_swizzle.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/plot_layout/layout_swizzle.py)：
   ```bash
   cd examples/plot_layout
   python layout_swizzle.py
   ```
2. 它会调用 `make_quarter_bank_swizzled_layout` / `make_half_bank_swizzled_layout` / `make_full_bank_swizzled_layout`（Python 包装），分别在 `./tmp/` 下生成 PDF/PNG。
3. 打开生成的 `swizzle_quarter_8x16.png`（quarter, 32B, 8×16 fp16）和 `swizzle_full_8x64.png`（full, 128B, 8×64 fp16）。

需要观察的现象：
- 在 `swizzle_quarter_8x16` 的图里，每格标注的是该逻辑位置被映射到的物理展平坐标。你会看到：**前 4 行（row 0~3）是恒等映射**，**后 4 行（row 4~7）的两个 8 元素半区被交换**——这正是 1-bit XOR（`s/4` 把 8 行分成两组）的效果。
- 在 `swizzle_full_8x64` 里，每相邻两行的列块号都做了 3-bit XOR，散布更剧烈。
- 对比三种图：粒度越大，同一列在 8 行内被打散到的 bank 越分散。

如何解释 bank conflict 改善：以 fp16（每元素 2 字节）、shared memory 32 个 bank、每 bank 4 字节为例，一个 8×8 的列读取如果不 swizzle，8 行的同列元素落同一 bank，8 次访问串行 → 8 倍冲突。加上 full-bank swizzle 后，8 行的同列元素经 XOR 落到 8 个不同 bank，8 次访问并行 → 0 冲突。

预期结果：你能用图直观说明「swizzle = 用行号 XOR 列块号，把同列访问打散到不同 bank」，并理解源码里 `xor8x8(c, s)` 那一行的物理含义。

> 待本地验证：`plot_layout` 依赖 `matplotlib`（`pip install matplotlib`）。生成的图片在 `examples/plot_layout/tmp/` 下。若只想看映射数值不想出图，可在脚本里加 `print(layout)` 观察文本形式的 `forward_index`。

#### 4.4.5 小练习与答案

**练习 1**：quarter/half/full 三种 swizzle 分别是几位 XOR？对应 `SwizzleMode` 的哪些值？

答案：分别是 1-bit (`xor2x2`)、2-bit (`xor4x4`)、3-bit (`xor8x8`)，对应 `Swizzle32B` / `Swizzle64B` / `Swizzle128B`。位数越多粒度越大、对齐要求越严。

**练习 2**：为什么 swizzle 冲突时 `MergeSwizzleLayouts` 取**更小**粒度而不是更大？

答案：更小粒度的 swizzle 是更大粒度的「子集」（约束更弱）。如果两个 op 一个要求 32B、一个要求 128B，只有 32B 能同时满足两者的可读性约束（128B 要求更严的对齐，32B 的 op 无法保证）。取小者保证两边都能正确读写。

**练习 3**：swizzle 主要优化的是 shared memory 还是 fragment（寄存器）？为什么？

答案：shared memory。因为 bank conflict 是 shared memory 的物理特性（32 bank 串行化）；寄存器是每线程私有的，不存在 bank conflict 概念，所以 fragment 布局不涉及 swizzle。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成下面这个端到端的小任务：

**任务**：手写一个最小 GEMM 的 fragment 布局分析。

1. 用 `plot_layout` 同时可视化两样东西：
   - shared buffer 上的 swizzle 布局（参考 `examples/plot_layout/layout_swizzle.py`）。
   - fragment 上的 MMA load 布局（参考 `examples/plot_layout/README.md` 给出的 `make_mma_load_base_layout`，它构造一个把 16×16 shared tile 映射到 MMA fragment 的 `T.Fragment`）。
2. 对照两张图回答：
   - swizzle 图里，shared tile 的列元素是如何被打散到不同 bank 的？（对应 4.4）
   - fragment 图里，shared tile 的每个元素被分给了哪个线程号、哪个局部寄存器槽？（对应 4.1/4.2）
   - 二者的关系是：shared 的 swizzle 布局由 `MakeFullBankSwizzleLayout` 这类函数直接给出；fragment 布局则是由 `tl.LayoutInference` pass（4.3）在编译期从 TileOp 的 `InferLayout()` 自动推断出来的——`MakeGemmFragmentA/B/C` 这族工厂函数（[src/layout/gemm_layouts.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/layout/gemm_layouts.cc)）是它们的后端实现。
3. 进阶：在 GEMM kernel 上把 swizzle 关掉（若示例里有相关开关）或手动改 shared buffer 形状不满足对齐约束，观察 `DetectSwizzleMode` 是否回退到 `None`、以及 bank conflict 是否恶化。这部分可结合 [u8-l2](u8-l2-swizzle-persistent-splitk.md) 的性能对比展开。

> 待本地验证：第 3 步的具体开关取决于示例版本；若无显式开关，可通过把 `continuous` 改成非 `vector_size×{2,4,8}` 倍数来触发 `ICHECK` 失败，从报错信息反向体会对齐约束。

## 6. 本讲小结

- **Layout = 索引映射函数**，由 `input_size_` + `forward_index_`（关于占位符 `_i0/_i1/...` 的表达式）刻画；**Fragment = Layout + 线程维**，多了 `forward_thread_` 和 `replicate_size_`，描述 tile 如何切给 warp 内线程。
- **Forward** 把逻辑坐标代入占位符求物理坐标；**Inverse** 借 TVM `DetectIterMap` + `InverseAffineIterMap` 求逆，静态形状走 Bijective、动态形状走 NoCheck；**Reshape** 用 `rescale_num/den` 守恒律处理形状/dtype 变换，亚字节 dtype 额外加 pack lane。
- **`tl.LayoutInference` pass** 分收集→推断→改写三阶段，推断内部走「strict → BFS common → free 枚举选最少寄存器 → 别名对齐」四步，结果挂到 `SBlock` 的 `layout_map` 注解和 `For` 的 `parallel_loop_layout` 注解。
- **swizzle 布局**用「行号 XOR 列块号」把同列元素打散到不同 bank，消除 shared memory bank conflict；粒度分 32B/64B/128B 三档，由 `DetectSwizzleMode` 模式匹配识别、冲突时取更小粒度合并。
- 本 pass 内有一处 metax 专属逻辑：MACA 的 `is_async_copy` buffer 会被跳过普通布局推断，是 MACA 后端在公共 pass 里埋的钩子，将在 [u7-l4](u7-l4-maca-pipeline.md) 展开。

## 7. 下一步学习建议

- **继续编译流水线**：读 [u4-l4](u4-l4-software-pipeline.md) 软件流水线，看 `T.Pipelined` 下译后如何与本讲的 fragment 布局配合。
- **深入张量核**：读 [u6-l1](u6-l1-mma-intrinsics-overview.md) MMA intrinsics，看 `TensorCoreIntrinEmitter` 如何消费本讲推断出的 Fragment 布局去发射具体指令。
- **回到 GEMM 全链路**：读 [u6-l3](u6-l3-gemm-example-walkthrough.md)，把本讲的布局推断、[u4-l2](u4-l2-tileop-and-gemm-dispatch.md) 的 gemm 分派、软件流水线串成一个完整 GEMM。
- **性能视角**：读 [u8-l2](u8-l2-swizzle-persistent-splitk.md)，从性能角度对比 swizzle 开关、persistent kernel、splitk 等策略。
- **源码延伸**：想看更多布局工厂函数（`MakeGemmFragmentA/B/C`、`MakeGemmABLayoutHopper` 等），直接读 `src/layout/gemm_layouts.cc` 与 `src/layout/cute_layout.cc`，它们是各后端 MMA 布局的具体定义库。
