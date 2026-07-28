# L2 swizzle、栅格化与 persistent/splitk

## 1. 本讲目标

本讲聚焦「让 GEMM 算得更快」的三种并行调度策略，它们都不改变算子的数学含义，只改变**线程块如何瓜分输出矩阵、如何访问显存**。读完本讲你应当能够：

- 说清楚 **L2 cache 友好的栅格化（rasterization）** 为什么能加速，以及 `T.use_swizzle` 在编译流水线里走到哪一步。
- 看懂一个 **persistent kernel（持久化线程块）**：为什么只启动等于 SM 数的线程块、`T.Persistent` 如何把 tile 分配给这些常驻块。
- 理解 **splitk**：当 M/N 两个维度的 tile 数不足以填满 GPU 时，如何沿 K 维把任务再切成多份并行，并用 `atomic_add` 汇聚结果。
- 区分两类容易混淆的「swizzle」：本讲讲的 **L2 threadblock swizzle** 与 u4-l3 讲过的 **shared-memory bank swizzle** 不是一回事。

本讲面向**专家层**读者，默认你已经读完 u6-l3（完整 GEMM 例子）与 u4-l3（内存布局推断），熟悉「搬进来—算—搬出去」范式、内存层级与 fragment 布局。

## 2. 前置知识

### 2.1 GPU 的存储层级与 L2 cache

一块 GPU 有很多个 **SM（Streaming Multiprocessor，流式多处理器）**，每个 SM 上同时跑若干个**线程块（thread block / CTA）**。显存访问有大致这样的层级：

| 层级 | 位置 | 容量 | 速度 | 谁能访问 |
|------|------|------|------|----------|
| 寄存器 / fragment | 每个线程私有 | 极小 | 最快 | 单线程 |
| shared memory | 每个块内 | ~100 KB | 很快 | 块内所有线程 |
| **L2 cache** | 全局共享 | 几十 MB | 较快 | **所有 SM 共享** |
| HBM（global） | 显存 | 几十 GB | 慢 | 所有线程 |

关键点：**L2 cache 是所有 SM 共享、且容量有限**。如果一个线程块刚把一块 B 矩阵的列从 HBM 搬进来，下个调度的线程块恰好也用到同一块 B，那么这块 B 可能还留在 L2 里，可以直接命中、不必再走一趟 HBM。**调度顺序决定了 L2 命中率。**

### 2.2 grid、blockIdx 与 tile 的对应

回顾 u2-l2：`with T.Kernel(*grid, threads=...) as (bx, by):` 里，`grid` 决定启动多少个线程块，`bx/by` 是编译期绑定的 `blockIdx.x / blockIdx.y`。对 GEMM，最朴素的切法是 grid = `(ceildiv(M, block_M), ceildiv(N, block_N))`，每个线程块算输出矩阵 C 中一块 `block_M × block_N` 的 tile。

**硬件调度器**（不是你的代码）决定「第 `t` 个被调度执行的线程块，是哪一个 `(bx, by)`」。绝大多数 GPU 采用**按线性编号行优先**的调度顺序：先把 `blockIdx.x + blockIdx.y * gridDim.x` 最小的块分发给空闲 SM。本讲的所有策略，本质上都是在**重排「线性编号 → tile 坐标」的映射**，让相邻被调度的块尽量共享输入数据。

### 2.3 三种策略要解决的问题

| 策略 | 解决的问题 | 手段 |
|------|-----------|------|
| **栅格化 / use_swizzle** | 相邻块不共享数据 → L2 命中率低 | 重排 blockIdx→tile 映射 |
| **persistent kernel** | 每块只算一个 tile 就退出 → launch 与切换开销大、且无法自己做 L2 友好的遍历 | 只启动 SM 数个常驻块，循环遍历所有 tile |
| **splitk** | M/N 维 tile 太少，grid 太小填不满所有 SM | 把 K 维也切成多份，用第三维 grid 并行 |

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [examples/gemm/example_gemm_persistent.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm_persistent.py) | 对比 persistent 与 non-persistent 两个 GEMM，含 `T.use_swizzle` 与 `T.Persistent` 用法 |
| [examples/gemm_splitk/example_tilelang_gemm_splitk.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm_splitk/example_tilelang_gemm_splitk.py) | splitk GEMM：用第三维 grid 切 K 维，`atomic_add` 汇聚 |
| [src/cuda/transform/persist_threadblock.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/transform/persist_threadblock.cc) | CUDA 侧 `tl.PersistThreadblock` pass，处理 grid 级同步 |
| [examples/plot_layout/layout_swizzle.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/plot_layout/layout_swizzle.py) | 可视化 shared-memory **bank swizzle** 布局（用于辨析两类 swizzle） |
| [tilelang/language/annotations.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/annotations.py) | `use_swizzle` 注解的实现（一行打 attr） |
| [src/tl_templates/cuda/threadblock_swizzle.h](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/tl_templates/cuda/threadblock_swizzle.h) | 设备端 `rasterization2DRow/Column` 模板（真正的重排逻辑） |
| [src/ir.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/ir.cc) | `T.Persistent` 的下译 `PersistentFor`（tile_id 分解算法） |
| [src/cuda/codegen/codegen_cuda.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/codegen/codegen_cuda.cc) | codegen 把 `use_swizzle` 注解印成 `tl::rasterization2DRow<...>()` 调用 |

## 4. 核心概念与源码讲解

### 4.1 栅格化（rasterization）与 L2 局部性

#### 4.1.1 概念说明

**栅格化（rasterization）** 在这里指「把二维 tile 网格按某种顺序展平成一维线性编号」的过程，即 `blockIdx → tile 坐标 (bx, by)` 的映射函数。最朴素的是**行优先（row-major）栅格化**：

\[
\text{linear\_id} = bx + by \times \text{gridDim.x}
\]

硬件按 `linear_id` 从小到大调度线程块。问题在于：朴素行优先时，**相邻两个被调度的块**通常是同一行里相邻列的两个 tile。它们共享输入矩阵 A 的**同一行块** \(A[by \times block_M : (by+1) \times block_M,\ :]\)，但 B 的列块各不相同。

这不一定是坏事——它们复用了 A。真正的麻烦出在「**波（wave）**」结构上：GPU 一次能同时容纳约 `sm_num` 个线程块（一波），一波执行完才轮到下一波。当一波结束后，L2 里残留的是上一波末尾几个块用过的数据。如果**下一波开头的块**恰好还能用到这些数据，就命中 L2；否则 L2 里的内容被换出去，白搬了。

**栅格化策略**就是主动设计一个非朴素的映射，让「相邻被调度的块」、以及「相邻两波的交界处」尽量共享输入数据，从而提高 L2 命中率。这是一种**零成本**的优化——计算量不变、显存读写总量不变，只换映射函数。

#### 4.1.2 核心流程

TileLang 采用经典的 **panel（面板）swizzle** 思路，把整个 grid 切成若干个宽为 `panel_width` 的面板：

```
朴素行优先:               panel swizzle (panel_width=2):
row0: 0 1 2 3            panel0(正向): 0 1
row1: 4 5 6 7            panel1(反向):     3 2     ← 相邻 panel 反向
row2: 8 9 ...            panel2(正向): 4 5
                         panel3(反向):     7 6
```

- 把线性编号 `block_idx` 按 `panel_size = panel_width × gridDim.x` 分成多个 panel。
- **同一 panel 内**：`panel_width` 个相邻块落在相邻行、同一列附近，彼此共享某些输入 tile。
- **panel 之间交替正反方向**（`panel_idx & 1` 奇偶判）：让相邻两 panel 的数据访问在空间上紧邻，最大化 L2 复用窗口。

直觉公式：被调度的两个相邻块若共享同一块输入 \(D\)（大小 \(S_D\) 字节），且 \(S_D \le\) L2 可用容量，则第二块命中 L2，省下一次 HBM 读取。设共享概率为 \(p\)，则有效 HBM 流量近似降为：

\[
\text{traffic}_{\text{eff}} \approx (1 - p) \cdot \text{traffic}_{\text{naive}}
\]

`panel_width` 越大，一次「锁」在 L2 里共享的数据越多，但受 L2 容量限制不能无限大。典型取值在个位到十几。

#### 4.1.3 源码精读

真正的重排逻辑是一个**设备端 C++ 模板**，被 codegen `#include` 进生成的 kernel（属于 u5-l4 讲过的 header-only `tl_templates` 库）：

[threadblock_swizzle.h:11-27](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/tl_templates/cuda/threadblock_swizzle.h#L11-L27) —— 行优先 panel swizzle 的核心实现：

```cpp
template <int panel_width> TL_DEVICE dim3 rasterization2DRow() {
  const unsigned int block_idx = blockIdx.x + blockIdx.y * gridDim.x;
  const unsigned int grid_size = gridDim.x * gridDim.y;
  const unsigned int panel_size = panel_width * gridDim.x;
  const unsigned int panel_offset = block_idx % panel_size;
  const unsigned int panel_idx = block_idx / panel_size;
  const unsigned int total_panel = tl::ceil_div(grid_size, panel_size);
  const unsigned int stride =
      panel_idx + 1 < total_panel
          ? panel_width
          : (grid_size - panel_idx * panel_size) / gridDim.x;
  // 关键：相邻 panel 交替正反方向
  const unsigned int col_idx = (panel_idx & 1)
                                   ? gridDim.x - 1 - panel_offset / stride
                                   : panel_offset / stride;
  const unsigned int row_idx = panel_offset % stride + panel_idx * panel_width;
  return {col_idx, row_idx, blockIdx.z};   // 返回「新的 blockIdx」
```

读法：函数读真实的 `blockIdx`，算出**逻辑上这个块应当处理的 tile 坐标** `(col_idx, row_idx)` 并返回一个新的 `dim3`。codegen 会在 kernel 开头把这行赋给一个**局部变量也叫 `blockIdx`**，从而「遮蔽（shadow）」掉真实的 `blockIdx`，后续 body 里用的 `bx, by` 就自动拿到了 swizzle 后的坐标。

`panel_idx & 1` 是「奇偶反向」的体现；`stride` 在最后一个不满的 panel 上做了收尾修正。列优先版本 `rasterization2DColumn`（同文件 [L29-45](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/tl_templates/cuda/threadblock_swizzle.h#L29-L45)）只是把行列角色对调。

Python 侧有一个对应的纯描述类（不直接参与 codegen，供 roller 调优器描述空间用）：

[rasterization.py:30-47](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/carver/roller/rasterization.py#L30-L47) —— `Rasterization2DRow`，注释里画了面板走向的示意图。

#### 4.1.4 代码实践（源码阅读型）

**目标**：手算一个最小 grid 的 block→tile 映射，验证你读懂了 `rasterization2DRow`。

**步骤**：

1. 设 `gridDim.x = 4`（4 列 tile）、`gridDim.y = 4`（4 行 tile），`panel_width = 2`。
2. 对 `block_idx = 0..7`（前两个 panel），手工套用 [threadblock_swizzle.h:11-27](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/tl_templates/cuda/threadblock_swizzle.h#L11-L27) 的公式，算出每个 `block_idx` 对应的 `(col_idx, row_idx)`。
3. 把结果画成 4×4 网格，在每个格子里填上「第几个被调度的块」。

**预期结果**：你会看到 panel 0（`block_idx 0..7`，因为 `panel_size = 2*4 = 8`）正向铺满前 2 行；panel 1 反向铺接下来 2 行。相邻 panel 的列访问方向相反。

**待本地验证**：数值结果取决于你手算，鼓励写个小 Python 脚本复刻公式打印表格。

#### 4.1.5 小练习与答案

**练习 1**：把 `panel_width` 设成 1，`rasterization2DRow` 退化成什么？
**答案**：`panel_size = gridDim.x`，每个 panel 恰好是一整行；`panel_idx & 1` 让奇偶行反向。这时 panel swizzle 退化为「奇偶行蛇形（zig-zag）」栅格化，相邻两行反向遍历。

**练习 2**：为什么 `panel_width` 不能取得和 `gridDim.y` 一样大？
**答案**：那样一个 panel 就吃掉整个 grid，相当于没有 panel 切分；且共享数据量可能超过 L2 容量，反而互相挤出 cache。panel 的意义在于把共享控制在「一波 SM 能容纳、L2 能装下」的粒度。

---

### 4.2 T.use_swizzle —— L2 友好的栅格化注解

#### 4.2.1 概念说明

`T.use_swizzle(panel_size, order="row")` 是 4.1 节栅格化策略的 DSL 入口。它本身**不产生任何计算**，只往 kernel 的 IR 上打一个属性注解 `threadblock_swizzle_pattern`，告诉 codegen「请在 kernel 开头插入一段 panel swizzle」。这是典型的「DSL 只描述意图、由编译器落地」模式（回顾 u1-l4）。

注意区分：本节的 `T.use_swizzle` 是 **L2 threadblock swizzle**（重排线程块调度顺序，服务于 L2 命中率）。它和 u4-l3 讲的 **shared-memory bank swizzle**（`make_full_bank_swizzled_layout` 等，重排 shared memory 内元素布局以消除 bank conflict）是**两个不同层级**的优化，4.2.4 会用一个可视化例子帮你把两者分清。

#### 4.2.2 核心流程

`T.use_swizzle` 从 DSL 到生成代码的三步：

```
DSL:  T.use_swizzle(10)
       │  打 attr: threadblock_swizzle_pattern = ("rasterization2DRow", 10)
       ▼
IR:   SBlock 上挂着一个 attr 节点，value 是 tvm_tuple(device_func, panel_size)
       │  codegen 遍历到这个 attr
       ▼
源码: const dim3 blockIdx = tl::rasterization2DRow<10>();   // 遮蔽真实 blockIdx
      ... kernel body 用到的 bx, by 自动变成 swizzle 后坐标 ...
```

`order="row"` 选 `rasterization2DRow`，`order="column"` 选 `rasterization2DColumn`。`panel_size` 直接成为模板参数 `panel_width`。

#### 4.2.3 源码精读

DSL 入口极薄，只做一件事——打属性：

[annotations.py:21-26](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/annotations.py#L21-L26) —— `use_swizzle` 的全部实现：

```python
def use_swizzle(panel_size: int, order: str = "row", enable: bool = True):
    """Annotate a kernel to use a specific threadblock swizzle pattern."""
    device_func = "rasterization2DRow" if order == "row" else "rasterization2DColumn"
    if not enable:
        return None
    return attr(None, "threadblock_swizzle_pattern", tvm_tuple(device_func, panel_size))
```

注意 `enable=False` 时返回 `None`——这就是「关闭 swizzle」的开关，综合实践里会用到它。

codegen 侧，CUDA codegen 在遍历 attr 节点时识别这个 key，把 `tvm_tuple` 解包成函数名和 panel 大小，印出对应的模板调用：

[codegen_cuda.cc:4732-4764](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/codegen/codegen_cuda.cc#L4732-L4764) —— 关键片段：

```cpp
} else if (op->attr_key == "threadblock_swizzle_pattern") {
    // 从 tvm_tuple 解出 (device_func, panel_size)
    ...
    ICHECK(!func_name.empty() && panel_size > 0);
    ...
    } else {
      this->stream << "const dim3 blockIdx = tl::" << func_name << "<"
                   << panel_size << ">();\n";   // ← 印出 swizzle 调用
    }
    this->VisitStmt(op->body);
    return;
```

读法：它声明一个局部 `const dim3 blockIdx`，**遮蔽**了真实的硬件 `blockIdx`，于是 body 里所有对 `blockIdx.x/y` 的引用都拿到了 swizzle 后的逻辑坐标。MACA codegen 也消费同一个 attr（u7-l2），HIP 同理——这是三后端共享的机制。

#### 4.2.4 代码实践

**目标**：用 `layout_swizzle.py` 可视化 **bank swizzle**，并对照区分它与本节的 **L2 swizzle**；再看一眼真实生成代码里的 `rasterization2DRow`。

**步骤**：

1. 运行可视化脚本（无需 GPU，纯布局计算）：

   ```bash
   python examples/plot_layout/layout_swizzle.py
   ```

   它调用 `make_quarter_bank_swizzled_layout` / `make_half_bank_swizzled_layout` / `make_full_bank_swizzled_layout`（见 [layout/swizzle.py:93-126](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/layout/swizzle.py#L93-L126)），分别对应 32B / 64B / 128B 三档，会打印布局对象并在 `plot_layout` 里生成可视化（参考 [layout_swizzle.py:13-32](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/plot_layout/layout_swizzle.py#L13-L32)）。

2. **需要观察的现象**：每个 swizzle 布局把「行号 XOR 列块号」打散，让落在同一 bank 的元素错开。这是 **shared memory 层**的 swizzle。

3. 编译一个带 `T.use_swizzle(10)` 的 GEMM（用 [example_gemm_persistent.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm_persistent.py) 里的 `matmul_non_persistent`），调用 `get_kernel_source()` 查看生成代码，在 kernel 开头找到 `const dim3 blockIdx = tl::rasterization2DRow<10>();` 这一行。

**预期结果**：你能清楚说出——bank swizzle 改的是 **shared memory 内一个 tile 的元素排布**（u4-l3），而 `T.use_swizzle` 改的是 **blockIdx 到 tile 坐标的映射**（本节）。两者正交，可以同时开。

**待本地验证**：`plot_layout` 的图形输出与生成源码的具体行号以本地运行为准。

#### 4.2.5 小练习与答案

**练习 1**：`T.use_swizzle(10, enable=False)` 和直接删掉这行，编译结果有区别吗？
**答案**：没有。`enable=False` 时 `use_swizzle` 返回 `None`，不打任何 attr，等价于没写。这是一个便于「开关对比」的语法糖。

**练习 2**：为什么 `T.use_swizzle` 的参数叫 `panel_size`，而模板里叫 `panel_width`？
**答案**：DSL 层用「面板大小」这个更直观的名字，它被原样传成模板参数 `panel_width`（面板在行方向跨的 tile 行数）。两者是同一个量。

---

### 4.3 persistent kernel —— 持久化线程块

#### 4.3.1 概念说明

**persistent kernel（持久化线程块）** 是另一种调度策略。朴素 kernel 里，每个线程块算完一个 tile 就退出，硬件再启动下一批块——这带来 **kernel launch 与块切换开销**，且块对「自己将处理哪些 tile」毫无掌控（全靠硬件栅格化决定）。

persistent kernel 的思路是：**只启动 `sm_num` 个线程块**（正好填满一波 SM），让它们**常驻不退出**，在一个 `for` 循环里依次处理所有 tile。好处有二：

1. 省掉反复 launch / 退出的开销。
2. 程序员可以**自主决定遍历 tile 的顺序**，从而在 kernel 内部做 L2 友好的 tile swizzle（比 4.2 的硬件栅格化更可控）。

#### 4.3.2 核心流程

persistent kernel 的启动结构：

```
grid = (sm_num,)                      ← 只启动 SM 数个块
threads = 256
每个 block_id ∈ [0, sm_num):
    for 每个 tile (按 wave 遍历所有 m_blocks × n_blocks 个 tile):
        算这一块 C 的 tile
```

由于 tile 总数 `num_tiles = m_blocks × n_blocks` 通常远大于 `sm_num`，需要 `waves = ceildiv(num_tiles, sm_num)` 轮（「波」）。第 `block_id` 个块负责的 tile 线性编号序列是：

\[
\text{tile\_id}(w, \text{block\_id}) = \text{sm\_num} \times w + \text{block\_id}, \quad w = 0, 1, \dots, \text{waves}-1
\]

再把 `tile_id` 反解成 `(bx, by)`。TileLang 提供两种写法：

- **原语写法** `T.Persistent(domain, sm_num, block_id)`：编译器自动生成 wave 循环与 tile_id 分解。
- **手写写法**：自己用 `T.serial(waves)` 加一段 `tile_id` 分解公式。

两种写法都内置了一种 **group swizzle**：把 `group_size` 个相邻 tile 沿列方向打包成一组，让同组 tile 共享 A 的行块（CUTLASS 经典 swizzle），同样是服务 L2 局部性。

#### 4.3.3 源码精读

[example_gemm_persistent.py:35-83](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm_persistent.py#L35-L83) —— 一个文件同时给出 persistent 与 non-persistent 对照。先看 persistent kernel 的启动与两条路径：

```python
sm_num = driver.get_num_sms()                 # 取设备 SM 数
m_blocks = T.ceildiv(M, block_M)
n_blocks = T.ceildiv(N, block_N)
waves = T.ceildiv(m_blocks * n_blocks, sm_num)
group_size = 8

with T.Kernel(sm_num, threads=threads) as (block_id):   # ← 只启动 sm_num 个块
    ...
    if use_persistent_primitive:
        # 原语写法：编译器生成 wave 循环 + tile_id 分解
        for bx, by in T.Persistent(
            [T.ceildiv(M, block_M), T.ceildiv(N, block_N)], sm_num, block_id):
            ...算这一块 C...
    else:
        # 手写写法：显式 wave 循环 + group swizzle 分解
        for w in T.serial(waves):
            tile_id = sm_num * w + block_id
            bx = (tile_id // group_size) % m_blocks
            by = (tile_id % group_size) + (tile_id // group_size) // m_blocks * group_size
            if bx * block_M < M and by * block_N < N:
                ...算这一块 C...
```

`driver.get_num_sms()` 在 CUDA 与 MACA 上都有实现（[cuda_driver.py:111](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/carver/arch/driver/cuda_driver.py#L111)、[maca_driver.py:105](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/carver/arch/driver/maca_driver.py#L105)），所以 persistent kernel 在 metax 分支同样可用。

原语写法 `T.Persistent` 的下译逻辑在 C++ 侧，核心是把 `tile_id` 反解成多维坐标并加上**越界保护**：

[ir.cc:134-213](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/ir.cc#L134-L213) —— `PersistentFor` 的关键片段：

```cpp
auto waves = ceildiv(padded_domain_size, wave_size);   // wave 数
auto loop_var = Var("w", waves.dtype());
...
n->f_make_for_loop = [=](...) -> Stmt {
    PrimExpr rem = loop_var * wave_size + index;       // tile_id = w * sm_num + block_id
    for (int i = grouped_domain.size() - 1; i >= 1; --i) {
      idxs.Set(i, truncmod(rem, grouped_domain[i]));   // 逐维分解
      rem = truncdiv(rem, grouped_domain[i]);
    }
    idxs.Set(0, rem);
    ...
    // 越界保护：tile_id 超出总数则 break
    auto out_if = IfThenElse(
        padded_domain_size <= (loop_var * wave_size + index),
        Evaluate(Call(..., tvm::tl::loop_break(), {})), Stmt());
    ...
    Stmt outer = For(loop_var, 0, waves, ForKind::kSerial, new_body, ...);
    ...
};
```

读法：`wave_size` 就是 `sm_num`，`index` 就是 `block_id`。它把 domain（tile 网格）按 `group_size` 重整（`grouped_domain`），再对 `tile_id` 做带 group 的多维分解——这正是手写路径里那两行 `bx/by` 公式的 generalized 版本。最后的 `out_if` 处理 `num_tiles` 不是 `sm_num` 整数倍时多余的那几个块（用 `loop_break` 提前退出）。

此外 CUDA 还有一个薄薄的 pass `tl.PersistThreadblock`，当 kernel 里出现 grid 级同步（`sync_grid`）时，给函数标上 `use_cooperative_groups` 属性：

[persist_threadblock.cc:24-65](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/transform/persist_threadblock.cc#L24-L65) —— 它扫描 `sync_grid()` 调用，若存在则加 `kUseCooperativeGroups` 属性，提示启动时需要协作组支持（persistent kernel 跨块同步时需要）。

#### 4.3.4 代码实践

**目标**：在同一台机器上对比 persistent 与 non-persistent 的延迟。

**步骤**：

1. 运行对照脚本（需 CUDA 或 MACA 设备）：

   ```bash
   python examples/gemm/example_gemm_persistent.py --M 8192 --N 8192 --K 8192
   ```

2. 脚本会先后编译并 benchmark `matmul_persistent` 与 `matmul_non_persistent`（见 [example_gemm_persistent.py:99-119](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm_persistent.py#L99-L119)），最后打印 `Persistent GEMM Speedup`。

3. **需要观察的现象**：两者都先做 `assert_allclose` 数值校验，再 `do_bench(warmup=500)` 测延迟。persistent 版通常会略快（收益主要来自 launch 开销减少与可控的 tile 遍历顺序）。

**预期结果**：speedup 略大于 1；在大矩阵、block 较小时收益更明显（因为 tile 数多、launch/切换开销占比大）。

**待本地验证**：具体加速比依赖设备与参数，需本地实跑。

#### 4.3.5 小练习与答案

**练习 1**：如果 `num_tiles < sm_num`（矩阵很小，tile 数比 SM 还少），persistent kernel 还值得用吗？
**答案**：基本不值得。此时一波就处理完了，没有「遍历多个 tile」可省；persistent 反而引入额外的 wave 循环与越界判断。persistent 适合 `num_tiles ≫ sm_num` 的大矩阵。

**练习 2**：手写路径里 `group_size` 起什么作用？
**答案**：它把 `group_size` 个相邻 tile 沿 N（列）方向打包，让同组内的块共享 A 的同一行块——这是 persistent 版本的 L2 友好 swizzle，作用与 4.2 的 `T.use_swizzle` 同源，只是发生在 kernel 内部的 tile 遍历顺序上。

---

### 4.4 splitk —— 把 K 维切分给多个线程块

#### 4.4.1 概念说明

GEMM 的并行度天然来自 M、N 两个维度：grid = `(m_blocks, n_blocks)`。当 **M 或 N 很小**（例如某些推理 batch、或 GEMV 场景），`m_blocks × n_blocks` 可能比 `sm_num` 还小，**grid 填不满所有 SM**，大量 SM 空闲。

**splitk（K 维切分）** 的思路：把 reduction 维 K 也切成 `split_k` 段，每段由不同的线程块独立计算部分和，最后把这些部分和**累加**到同一个输出 C。这样 grid 从二维变三维：

\[
\text{grid} = (\text{m\_blocks},\ \text{n\_blocks},\ \text{split\_k})
\]

并行度直接乘以 `split_k`。代价是：多个块要**写同一块 C**，必须用**原子加（atomic_add）**汇聚，带来额外开销与数值精度问题。因此 splitk 是「**用 atomic 开销换取并行度**」的策略，只在 grid 太小时才划算。

#### 4.4.2 核心流程

```
splitK = K // split_k                 # 每段 K 的长度
grid = (ceildiv(N, block_N), ceildiv(M, block_M), split_k)
with T.Kernel(...) as (bx, by, bz):   # bz 是 K 段编号
    # 每个块只遍历 K 中属于自己那一段
    for ko in Pipelined(ceildiv(splitK, block_K), num_stages=0):
        copy A 的对应 K 段
        copy B 的对应 K 段
        gemm 累加到本地 C_local
    # 把本地部分和原子加到全局 C
    Parallel:
        atomic_add(C[...], C_local[...])
```

关键三处：

1. **K 段切分**：`bz`（第三维 blockIdx）决定本块负责 `K` 中第 `bz * splitK .. (bz+1) * splitK` 这一段。
2. **本地累加**：每个块在自己的 `C_local` fragment 里累加本段 K 的乘积和，无竞争。
3. **原子汇聚**：最后用 `T.atomic_add` 把 `C_local` 加到全局 C 的对应 tile。

注意这个例子里 K 维的 pipeline 用了 `num_stages=0`（不开软件流水，回顾 u4-l4：0 表示不流水），因为 splitk 例子侧重演示切分逻辑而非极致性能。

#### 4.4.3 源码精读

[example_tilelang_gemm_splitk.py:5-26](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm_splitk/example_tilelang_gemm_splitk.py#L5-L26) —— 整个 splitk kernel 极其短小，正好说明问题：

```python
@tilelang.jit
def matmul(A, B, C, block_M, block_N, block_K, split_k, ...):
    M, N, K = T.const("M, N, K")
    splitK = K // split_k                              # 每段 K 长度

    with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), split_k,
                  threads=128) as (bx, by, bz):       # ← 第三维 = split_k
        A_shared = T.alloc_shared((block_M, block_K), dtype)
        B_shared = T.alloc_shared((block_K, block_N), dtype)
        C_local = T.alloc_fragment((block_M, block_N), accum_dtype)

        T.clear(C_local)
        for ko in T.Pipelined(T.ceildiv(splitK, block_K), num_stages=0):
            T.copy(A[by * block_M, bz * splitK + ko * block_K], A_shared)   # A 的 K 偏移含 bz
            T.copy(B[bz * splitK + ko * block_K, bx * block_N], B_shared)   # B 的 K 偏移含 bz
            T.gemm(A_shared, B_shared, C_local)

        for i, j in T.Parallel(block_M, block_N):
            T.atomic_add(C[by * block_M + i, bx * block_N + j], C_local[i, j])  # 原子汇聚
```

读法要点：

- **坐标含 `bz`**：A 的第二维（K 维）起点是 `bz * splitK + ko * block_K`，B 的第一维（K 维）起点同理——这正是「K 维切分给 `bz`」的体现。
- **`C_local` 在块内私有**：每个块累加的是自己那段 K 的部分和，`T.clear` 后累加，无竞争。
- **`T.atomic_add`**：`split_k` 个块都往同一块 `C` 写，必须原子加。`atomic_add` 定义在 [language/atomic.py:185](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/atomic.py#L185)。

主函数里用 `c = torch.zeros(M, N)` 初始化 C（必须先清零，因为要累加），随后与参考结果 `a @ b` 对比：

[example_tilelang_gemm_splitk.py:43-48](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm_splitk/example_tilelang_gemm_splitk.py#L43-L48) —— 注意 `c` 必须初始化为 0，否则 `atomic_add` 会累加到垃圾值上。

#### 4.4.4 代码实践

**目标**：改 `split_k` 观察并行度与原子开销的此消彼长。

**步骤**：

1. 运行默认例子（M=N=K=1024，split_k=4）：

   ```bash
   python examples/gemm_splitk/example_tilelang_gemm_splitk.py
   ```

2. 把 [L36](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm_splitk/example_tilelang_gemm_splitk.py#L36) 的 `split_k` 依次改成 `1`（退化为普通 GEMM，无 atomic）、`4`、`8`，重新运行。
   - 注意：`split_k` 必须整除 `K / block_K` 之外，还得整除 K 本身（因为 `splitK = K // split_k` 要参与循环），改值时确认 `K % split_k == 0`。

3. **需要观察的现象**：`split_k=1` 时 `bz` 维为 1，无 atomic；`split_k>1` 时出现 `atomic_add`。数值结果应始终与 `a @ b` 一致（`rtol=1e-2`）。

**预期结果**：小矩阵下，`split_k` 增大先提速（填满 SM），后因 atomic 开销增大而减速，存在一个最优点。

**待本地验证**：最优点随设备与矩阵规模变化，需本地实跑。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `C` 在调用前必须用 `torch.zeros` 清零，而普通 GEMM（无 splitk）不需要？
**答案**：splitk 用 `atomic_add` 把多个块的部分和**累加**到 C；若 C 初始是垃圾值，结果就错了。普通 GEMM 每个块独占一块 C、是直接覆盖写（`T.copy(C_local, C[...])`），不依赖初值。

**练习 2**：splitk 为什么通常配 `accum_dtype=float32`、`out_dtype=float32`，即使输入是 fp16？
**答案**：fp16 的表示范围与精度不足以承担「多个部分和相加」，容易溢出或丢精度。用 fp32 累加器与输出，再配合 atomic_add 的浮点累加，能保证数值稳定。这也是例子签名里 `accum_dtype=T.float32, out_dtype=T.float32` 的原因。

---

## 5. 综合实践

把本讲三个策略串起来，完成下面两个任务。

### 任务 A：开启 / 关闭 `T.use_swizzle` 对比 GEMM 延迟

1. 以 [example_gemm_persistent.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm_persistent.py) 中的 `matmul_non_persistent`（[L7-32](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm_persistent.py#L7-L32)）为基准，它在 [L21](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm_persistent.py#L21) 调了 `T.use_swizzle(10)`。
2. 复制一份为 `matmul_non_persistent_noswiz`，把 `T.use_swizzle(10)` 改成 `T.use_swizzle(10, enable=False)`（回顾 4.2.3：等价于不打 attr）。
3. 在同一 `M=N=K=8192`、相同 `block_M/N/K`、`num_stages`、`threads` 下分别 `compile` + `get_profiler().do_bench(warmup=500)`，记录两者的延迟与 TFLOPS。
4. 用 `get_kernel_source()` 对比两份生成代码，确认开启版在 kernel 开头多了 `const dim3 blockIdx = tl::rasterization2DRow<10>();`。
5. **解释你观察到的差异**：开启 swizzle 后，相邻调度的线程块共享更多输入数据，L2 命中率提升，延迟应略降。把你的实测数字填入下表（待本地验证）：

   | 配置 | 延迟 (ms) | TFLOPS | 生成代码是否含 rasterization2DRow |
   |------|-----------|--------|-----------------------------------|
   | use_swizzle 开 | 待填 | 待填 | 是 |
   | use_swizzle 关 | 待填 | 待填 | 否 |

### 任务 B：阅读 splitk 例子，说明 K 维如何被切给多个线程块

阅读 [example_tilelang_gemm_splitk.py:14-26](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm_splitk/example_tilelang_gemm_splitk.py#L14-L26)，用你自己的话写一段说明，覆盖：

1. `splitK = K // split_k` 把 K 等分成几段？
2. `bz`（第三维 grid）如何决定一个块处理 K 的哪一段？（写出 A、B 的 K 维起点表达式）
3. 为什么末尾用 `T.atomic_add` 而不是普通 `T.copy`？
4. 画一张示意图：把 K 维沿 `split_k=4` 切开，标出 4 个线程块各自负责的 K 区间与它们共同写入的同一块 C。

**交付物**：一张对比表（任务 A）+ 一段文字说明与示意图（任务 B）。

## 6. 本讲小结

- **栅格化（rasterization）** 决定 `blockIdx → tile 坐标` 的映射；朴素行优先不一定 L2 友好。**panel swizzle** 把 grid 切成面板、相邻面板交替正反方向，让相邻调度的块共享输入、提升 L2 命中率。
- **`T.use_swizzle`** 只打一个 `threadblock_swizzle_pattern` 注解，codegen 把它印成设备端的 `tl::rasterization2DRow<panel_size>()`，靠**局部变量遮蔽真实 blockIdx** 生效；`enable=False` 即关闭。
- **persistent kernel** 只启动 `sm_num` 个常驻块，循环遍历所有 tile，省 launch 开销并自主控制 tile 遍历顺序；`T.Persistent` 原语由 [ir.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/ir.cc) 的 `PersistentFor` 下译，内含 group swizzle 与越界保护。
- **splitk** 在 M/N 维 tile 数不足以填满 SM 时，沿 K 维再切 `split_k` 份，用第三维 grid 并行，末尾 `atomic_add` 汇聚；本质是「用 atomic 开销换并行度」。
- **两类 swizzle 要分清**：本讲的 L2 threadblock swizzle（`T.use_swizzle`，重排块调度）与 u4-l3 的 shared-memory bank swizzle（`make_*_bank_swizzled_layout`，重排 tile 内元素）正交，可同时开启。
- 这三种策略都**不改变算子的数学含义**，是纯调度优化，常与软件流水线（u4-l4）、张量核（u6）叠加使用。

## 7. 下一步学习建议

- **自动调优**：这些调度参数（`panel_size`、`block_M/N/K`、`num_stages`、`split_k`）手工调很繁琐，下一讲 **u8-l1 autotuner** 讲解如何让编译器自动搜索最优配置，正好承接本讲。
- **性能剖析**：本讲多次用到 `do_bench` 测延迟，**u8-l3 性能剖析与基准测试** 会深入 `profiler` 模块与 `do_bench` 的精确计时原理（含 L2 冲刷）。
- **L2 持久化的另一面**：metax 分支还有硬件级 L2 cache 持久化策略（`annotate_l2_hit_ratio` + `LowerL2Persistent`，见 [lower_l2_persistent_annotation.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/transform/lower_l2_persistent_annotation.cc)），它与本讲的栅格化是不同层级的 L2 优化，已在 u7-l4 讨论过，可对照阅读。
- **源码延伸**：想理解 panel swizzle 在集群（cluster）场景的扩展，可读 [threadblock_swizzle.h:54-109](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/tl_templates/cuda/threadblock_swizzle.h#L54-L109) 的 `rasterization2DRowWithCluster`。
