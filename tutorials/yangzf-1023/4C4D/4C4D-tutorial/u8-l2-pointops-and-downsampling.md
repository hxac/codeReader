# u8-l2 pointops2 与点云下采样策略

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 pointops2 子包中 `furthestsampling` 与 `knnquery` 两个 CUDA 算子的 Python 包装层（`autograd.Function`）与 CUDA 核函数，说清它们的输入输出形状约定。
2. 理解 pointops2 的「变长批次 + offset 前缀和」批处理约定，并解释 `utils/general_utils.py` 中 `fps`/`knn` 封装为什么用 `torch.cumsum` 构造 offset。
3. 精读 `scene/dataset_readers.py` 的下采样分支，对比 `downsample_method='random'` 与 `'fps'` 两种策略在「有放回抽样」「密度均匀性」上的本质差异，理解它们对 4D 高斯初始几何覆盖的影响。

本讲属于专家层，视角是「自底向上」：u2-l2 已经从数据链路顶部看过 `readColmapSceneInfo`，本讲钻到它的最底层——CUDA 采样核函数，把中间的每一层封装都拆开。

## 2. 前置知识

- **最远点采样（FPS, Farthest Point Sampling）**：一种贪心的点云采样算法。给定 \( N \) 个点，要选出 \( k \) 个「彼此离得尽量远」的代表点。做法是：先选一个种子点，然后反复执行「在剩余点中，找出距离**已选集合最近距离**最大的那个点，加入集合」。它是一种 max-min 准则的贪心近似，能产出空间上分布均匀的子集，常用于点云深度学习的下采样。
- **K 近邻查询（KNN）**：给一组参考点和一个查询点，返回参考点中距离最近的 \( k \) 个点的索引与距离。本项目中它以「每个线程维护一个大小为 \( k \)的最大堆，线性扫描批次内所有点」的朴素方式实现。
- **变长批次的 offset 约定**：pointops2 出身于点云 transformer 系代码，它不用 `(B, N, 3)` 的规整张量进 CUDA，而是把所有批次的点**首尾拼接**成一个扁平的 `(总点数, 3)` 张量，再用一个长度为 `B` 的 `offset` 整数数组记录每个批次的**结束位置前缀和**（ inclusive cumsum）。例如三个批次各有 1000、500、3500 个点，则 `offset = [1000, 1500, 5000]`。
- **有放回 vs 无放回抽样**：`np.random.randint(0, N, k)` 产生的索引**可能重复**（有放回），而 FPS 每一步选的都是未选过的点（无放回）。这一点会直接影响两种下采样策略的性质。
- **`autograd.Function`**：PyTorch 自定义算子的基类。pointops2 里每个算子都是一个只实现 `forward` 的 `Function`——索引类算子本身不参与求导（索引只用于 gather），这与 u4-l2 讲过的 `_RasterizeGaussians` 属同一模式。
- 承接 u1-l2：pointops2 编译后提供两个可 import 的名字——Python 包 `pointops2`（`setup.py` 把 `package_dir` 指向 `functions/`）与 CUDA 扩展模块 `pointops2_cuda`；承接 u2-l2：`num_pts` 是下采样上限，只有当点云超过它时才触发下采样。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [pointops2/functions/pointops.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/pointops2/functions/pointops.py) | pointops2 的 Python 包装层，定义 `FurthestSampling`、`KNNQuery` 等十几个 `autograd.Function`；本讲只关注前两个 |
| [pointops2/src/pointops_api.cpp](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/pointops2/src/pointops_api.cpp) | pybind11 登记表，把所有 `*_cuda` C++ 函数注册给 Python 模块 `pointops2_cuda` |
| [pointops2/src/sampling/sampling_cuda.cpp](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/pointops2/src/sampling/sampling_cuda.cpp) / [sampling_cuda_kernel.cu](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/pointops2/src/sampling/sampling_cuda_kernel.cu) | FPS 的 C++ 薄壳与真正的 CUDA 核函数 |
| [pointops2/src/knnquery/knnquery_cuda_kernel.cu](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/pointops2/src/knnquery/knnquery_cuda_kernel.cu) | KNN 的 CUDA 核函数（最大堆 + 堆排序） |
| [utils/general_utils.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/general_utils.py) | 主框架侧的 `fps`/`knn` 封装：把规整的 `(b, n, 3)` 张量翻译成 pointops2 的「扁平 + offset」约定 |
| [scene/dataset_readers.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py) | 消费方：`readColmapSceneInfo` 的下采样分支，random 与 fps 的分岔点 |
| [scene/__init__.py](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py) / [train.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py) | 参数链：`--downsample_method` 与 `num_pts` 如何一路传到下采样分支 |

## 4. 核心概念与源码讲解

### 4.1 furthestsampling 与 knnquery：pointops2 的两个 CUDA 算子

#### 4.1.1 概念说明

pointops2 是一个「 borrowed 」子包：文件头注明其注意力部分来自 Point Transformer 系作者（[pointops2/functions/pointops.py:1-4](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/pointops2/functions/pointops.py#L1-L4)），本项目只用到它十几 个算子中的两个：

- `furthestsampling(xyz, offset, new_offset)` → 采样索引 `idx (m)`：从每个批次的 \( n \) 个点中选出 \( m \) 个最远点，返回**全局扁平索引**。
- `knnquery(nsample, xyz, new_xyz, offset, new_offset)` → `(idx (m, nsample), dist (m, nsample))`：为每个查询点在**同批次**的参考点中找 \( k \) 近邻，返回局部有序（升序）的索引与**距离（已开方）**。

在 4C4D 中它们的分工是：`furthestsampling` 负责初始点云下采样（4.3 节），`knnquery` 是 pointops2 其余组合功能（`queryandgroup`、`interpolation` 等）的底层原语，主框架当前没有直接消费 `knnquery`，但 `utils/general_utils.knn` 为它准备了等价封装（4.2 节）。

值得对比的是 simple-knn 子包的 `distCUDA2`：它回答「每个点到最近邻的**距离平方**」（数值），用于 `create_from_pcd` 初始化高斯尺度；而 `knnquery` 回答「最近的 \( k \) 个点是**谁**」（索引）。两者是同族不同物。

#### 4.1.2 核心流程

FPS 的贪心准则形式化为：

\[
p_{j+1} = \arg\max_{p \in P \setminus S_j} \; d(p, S_j), \qquad d(p, S_j) = \min_{s \in S_j} \|p - s\|
\]

其中 \( S_j \) 是已选集合。若每步都重新计算 \( d(p, S_j) \)，复杂度是 \( O(m \cdot n \cdot |S|) \)；CUDA 核的关键优化是**增量维护**一个 `tmp` 数组：

\[
\text{tmp}[k] \leftarrow \min\left(\text{tmp}[k],\; \|p_k - p_{\text{old}}\|^2\right)
\]

即每选出一个新点 `old`，只需用它更新一遍所有点的「到已选集合的最小距离平方」，于是总复杂度降为 \( O(m \cdot n) \)。`tmp` 初始化为 `1e10`，`idx[0]` 固定取批次段内的**第一个点**（不是随机种子）。

CUDA 侧的并行结构：

```
网格 = b 个 block（一个 block 独占一个批次段）
block 内:
  idx[start_m] = start_n                      # 种子点 = 段内第 0 个点
  循环 j = start_m+1 .. end_m-1:              # 还要选 m-1 个点
    每个线程 stride 扫描段内点 k:
      d = 平方距离(p_k, p_old)
      tmp[k] = min(tmp[k], d)                 # 增量更新最近距离
      记录本线程的最大值 best/besti
    共享内存归约（__update 二叉树取 max）→ 全 block 的最远点
    idx[j] = 该最远点
```

knnquery 则是「一线程一查询」：每个线程先由 `new_offset` 反查自己属于哪个批次段，再线性扫描该段的全部参考点，用大小为 `nsample` 的**最大堆**维护当前最近邻集合（堆顶是「当前第 k 近」），最后堆排序成升序输出。

#### 4.1.3 源码精读

先看 Python 包装层。`FurthestSampling.forward` 做的事非常薄：断言连续、计算 `n_max`（供 block 内 stride 使用）、分配输出 `idx` 与工作数组 `tmp`、调用 CUDA：

```python
class FurthestSampling(Function):
    @staticmethod
    def forward(ctx, xyz, offset, new_offset):
        """
        input: xyz: (n, 3), offset: (b), new_offset: (b)
        output: idx: (m)
        """
        assert xyz.is_contiguous()
        n, b, n_max = xyz.shape[0], offset.shape[0], offset[0]
        for i in range(1, b):
            n_max = max(offset[i] - offset[i-1], n_max)
        idx = torch.cuda.IntTensor(new_offset[b-1].item()).zero_()
        tmp = torch.cuda.FloatTensor(n).fill_(1e10)
        pointops_cuda.furthestsampling_cuda(b, n_max, xyz, offset, new_offset, tmp, idx)
        del tmp
        return idx
```

[pointops2/functions/pointops.py:14-31](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/pointops2/functions/pointops.py#L14-L31) 定义了 `FurthestSampling`：`tmp` 是长度 `n` 的「到已选集合最小距离平方」缓存，填 `1e10` 相当于正无穷；`idx` 长度取 `new_offset[b-1]`（即总采样数 \( m \)）；返回的是 `IntTensor`（调用方要自己 `.long()` 才能当索引用）。注意它**没有 `backward`**——采样输出是索引，不参与求导。

`KNNQuery.forward` 同样是纯编舞：

```python
idx = torch.cuda.IntTensor(m, nsample).zero_()
dist2 = torch.cuda.FloatTensor(m, nsample).zero_()
pointops_cuda.knnquery_cuda(m, nsample, xyz, new_xyz, offset, new_offset, idx, dist2)
return idx, torch.sqrt(dist2)
```

[pointops2/functions/pointops.py:34-49](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/pointops2/functions/pointops.py#L34-L49) 定义了 `KNNQuery`：`new_xyz is None` 时自查自询（第 41 行）；CUDA 返回平方距离，Python 侧开方成欧氏距离。这两个包装经 [pointops2/src/pointops_api.cpp:16-18](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/pointops2/src/pointops_api.cpp#L16-L18) 的 pybind11 登记表（`m.def("furthestsampling_cuda", ...)` 与 `m.def("knnquery_cuda", ...)`）进入 `pointops2_cuda` 模块，再经 [pointops2/src/sampling/sampling_cuda.cpp:7-15](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/pointops2/src/sampling/sampling_cuda.cpp#L7-L15) 这样的 C++ 薄壳（只做 `data_ptr<float>()` 取裸指针后转发 launcher）抵达核函数——与 u4-l2 讲过的光栅化五层架构完全同构。

CUDA 核函数开头，一个 block 通过 `offset`/`new_offset` 定位自己批次的四条边界：

```cu
if (bid == 0) { start_n = 0; end_n = offset[0]; start_m = 0; end_m = new_offset[0]; old = 0; }
else {
    start_n = offset[bid - 1];  end_n = offset[bid];
    start_m = new_offset[bid - 1]; end_m = new_offset[bid];
    old = offset[bid - 1];
}
...
if (tid == 0) idx[start_m] = start_n;
```

[sampling_cuda_kernel.cu:20-39](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/pointops2/src/sampling/sampling_cuda_kernel.cu#L20-L39)：第 0 个批次从 0 起算，第 `bid` 个批次的起点是 `offset[bid-1]`——这正是「前缀和」约定的消费方式；第 39 行把种子点固定为**段内第一个点**（确定性种子，不随机）。

主循环每轮先算所有点到上一个选中点 `old` 的距离，增量更新 `tmp` 并记录各线程的局部最优：

```cu
for (int j = start_m + 1; j < end_m; j++) {
    int besti = start_n; float best = -1;
    float x1 = xyz[old * 3 + 0]; ... // 上一个选中点
    for (int k = start_n + tid; k < end_n; k += stride) {
        float d = (x2-x1)*(x2-x1) + ...;
        float d2 = min(d, tmp[k]);   // 增量最近距离
        tmp[k] = d2;
        besti = d2 > best ? k : besti;  best = d2 > best ? d2 : best;
    }
    dists[tid] = best; dists_i[tid] = besti;
```

[sampling_cuda_kernel.cu:42-61](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/pointops2/src/sampling/sampling_cuda_kernel.cu#L42-L61)：`tmp[k]` 始终存「点 k 到已选集合的最小距离平方」，`min(d, tmp[k])` 一行完成增量维护。随后 [sampling_cuda_kernel.cu:64-123](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/pointops2/src/sampling/sampling_cuda_kernel.cu#L64-L123) 用 `__update` 辅助函数做共享内存二叉树归约取全局最大，最终 [sampling_cuda_kernel.cu:125-127](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/pointops2/src/sampling/sampling_cuda_kernel.cu#L125-L127) 写出 `idx[j] = old`。launcher（[sampling_cuda_kernel.cu:131-171](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/pointops2/src/sampling/sampling_cuda_kernel.cu#L131-L171)）按 `opt_n_threads(n_max)` 为模板参数 `block_size` 选择编译期特化。

knnquery 侧，每个线程先定位自己的批次段（线性扫描 offset）：

```cu
int bt_idx = get_bt_idx(pt_idx, new_offset);
int start = (bt_idx == 0) ? 0 : offset[bt_idx - 1];
int end = offset[bt_idx];
```

[knnquery_cuda_kernel.cu:51-80](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/pointops2/src/knnquery/knnquery_cuda_kernel.cu#L51-L80)：`get_bt_idx` 的 `while(1)` 线性搜索**要求 offset 单调递增**——`cumsum` 天然保证这一点。核心扫描段在 [knnquery_cuda_kernel.cu:86-107](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/pointops2/src/knnquery/knnquery_cuda_kernel.cu#L86-L107)：`float best_dist[100]` 是**寄存器数组，硬性限定 `nsample ≤ 100`**；距离小于堆顶就替换并 `reheap` 重整最大堆，最后 `heap_sort` 升序输出。注意查询只在**同批次段**内进行——跨段绝不串点，这是 offset 约定的语义保证。

#### 4.1.4 代码实践

**实践目标**：不运行 CUDA，仅通过「调用链追踪 + 形状推演」确认两个算子的张量契约。

1. 操作步骤：
   - 从 [utils/general_utils.py:17](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/general_utils.py#L17) 的 import 出发，依次打开 `pointops.py` → `pointops_api.cpp` → `sampling_cuda.cpp` → `sampling_cuda_kernel.cu`，在纸上画出这条调用链（共五层）。
   - 假设输入 `xyz (50000, 3)`、`offset [50000]`、`new_offset [10000]`，在纸上写出：`n`、`b`、`n_max`、`tmp` 的形状与初值、`idx` 的形状与 dtype。
   - 再为 `knnquery(16, xyz, new_xyz(10000,3), [50000], [10000])` 写出 `idx` 与 `dist2` 的形状，并回答：第 7000 个查询点会扫描哪一段参考点？
2. 需要观察的现象：`idx` 的 dtype 是 `torch.int32`（`cuda.IntTensor`），而 PyTorch 高级索引要求 `long`——检查 4.2 节封装里 `.long()` 出现的位置。
3. 预期结果：`n=50000, b=1, n_max=50000, tmp=(50000,) 填 1e10, idx=(10000,) int32`；knnquery 输出 `(10000, 16)` 的 idx 与 dist；第 7000 个查询点落在批次 0，扫描 `[0, 50000)` 全段。
4. 若有 GPU 且已 `pip install -e ./pointops2`，可另跑一个最小脚本实测上述形状（`assert idx.shape == ...`）。无 GPU 时本实践为纯阅读型，**结论待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`furthestsampling` 的第一个采样点是谁？这意味着什么？

答案：是批次段内的第 0 个点（[sampling_cuda_kernel.cu:39](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/pointops2/src/sampling/sampling_cuda_kernel.cu#L39) 的 `idx[start_m] = start_n`）。这意味着 FPS 结果是**确定性**的：同一点云两次调用结果完全一致（无随机种子参与），但也意味着如果点云存储顺序改变了（比如按无序字典重排），采样起点会随之改变。

**练习 2**：`knnquery` 中 `float best_dist[100]` 这个寄存器数组暗示了什么限制？

答案：`nsample` 最多 100。超过 100 会越界写寄存器数组（未定义行为）；这是一个没有运行时检查的隐式约束，只写在代码里，调用方必须自律。

**练习 3**：为什么 `FurthestSampling` 只实现 `forward` 而不实现 `backward` 也不报错？

答案：因为它不在需要梯度的路径上——输出是整型索引，调用方（`dataset_readers`）在下采样时处于无梯度上下文，索引仅用于 `pcd.points[mask]` 这类 gather 操作；只有当某个带 `requires_grad` 的张量「穿过」该 Function 参与求导时，PyTorch 才会要求 `backward`，这里不存在这种情况。

### 4.2 fps/knn 封装与 offset 的 cumsum 约定

#### 4.2.1 概念说明

`utils/general_utils.py` 末尾的两个函数是主框架与 pointops2 之间的「翻译层」：主框架习惯用规整的 `(b, n, 3)` 张量，而 pointops2 要扁平 `(N, 3)` + offset。封装做的唯一实质性工作就是**构造 offset 与 new_offset**，以及在 `knn` 里做一次「全局索引 → 批内局部索引」的换算。

为什么用 `torch.cumsum` 构造 offset？三个理由：

1. **免填充（packed representation）**：变长批次若走 `(B, N_max, 3)` 规整张量必须补零到最长批次，显存与计算都被浪费；扁平拼接 + 前缀和让每个批次只占自己实际长度的内存，一次 kernel 启动处理全部批次（FPS 的网格维度恰好是 `b`，一个 block 独占一个批次）。
2. **O(1) 段定位**：核函数里 `start = offset[bid-1], end = offset[bid]` 两条读数即得边界；knnquery 的 `get_bt_idx` 也依赖「offset 单调递增」才能线性扫出所属段——`cumsum` 的输出天然单调。
3. **输入格式天然对齐**：调用方手里的每批次长度就是 `[n]*b` 这样的一维表，`full` 出该表再 `cumsum` 是最直接的「结束位置前缀和」生成方式，且与 `int32`（`.int()`）的要求匹配——C++ 侧读的是 `data_ptr<int>()`。

#### 4.2.2 核心流程

两个封装的数据流（以 `fps(x, k)`、`x: (b, n, 3)` 为例）：

```
x (b, n, 3)
  ├─ view(-1, 3) + contiguous        → 扁平化 (b*n, 3)，按批次首尾拼接
  ├─ offset     = cumsum([n]*b).int()  → [n, 2n, ..., b*n]     每批次结束位置
  ├─ new_offset = cumsum([k]*b).int()  → [k, 2k, ..., b*k]     每批次采样数结束位置
  └─ idx = furthestsampling(x, offset, new_offset).long()
→ 返回 (b*k,) 的全局扁平索引（int64）
```

`knn(x, src, k)` 多一步换算：CUDA 返回的 `idx` 是参考点扁平数组里的**全局**索引，封装用 `idx - (src_offset - m)` 减去各批次的起点（`src_offset[i] - m` 恰为批次 `i` 的首元素位置），还原成**批内局部**索引，方便上层直接 `src[batch_idx, local_idx]` 取点。

#### 4.2.3 源码精读

```python
def knn(x, src, k, transpose=False):
    if transpose:
        x = x.transpose(1, 2).contiguous()
        src = src.transpose(1, 2).contiguous()
    b, n, _ = x.shape
    m = src.shape[1]
    x = x.view(-1, 3)
    src = src.view(-1, 3)
    x_offset = torch.full((b,), n, dtype=torch.long, device=x.device)
    src_offset = torch.full((b,), m, dtype=torch.long, device=x.device)
    x_offset = torch.cumsum(x_offset, dim=0).int()
    src_offset = torch.cumsum(src_offset, dim=0).int()
    idx, dists = knnquery(k, src, x, src_offset, x_offset)
    idx = idx.view(b, n, k) - (src_offset - m)[:, None, None]
    return idx.long(), dists.view(b, n, k)
```

[utils/general_utils.py:170-184](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/general_utils.py#L170-L184) 是 `knn` 封装：`transpose` 兼容 `(b, 3, n)` 输入；`full + cumsum + .int()` 三连构造 offset（第 178-181 行）；第 182 行注意参数顺序——`xyz` 位传的是**参考点 src**，`new_xyz` 位传的是**查询点 x**，offset 也随之成对调换；第 183 行完成全局→局部索引换算。**诚实的观察**：全仓库检索下来，`knn` 目前没有任何调用方，主框架真正消费的只有 `fps`；`knn` 是一个「备好未用」的工具函数。

```python
def fps(x, k):
    b, n, _ = x.shape
    x = x.view(-1, 3).contiguous()
    offset = torch.full((b,), n, dtype=torch.long, device=x.device)
    new_offset = torch.full((b,), k, dtype=torch.long, device=x.device)
    offset = torch.cumsum(offset, dim=0).int()
    new_offset = torch.cumsum(new_offset, dim=0).int()
    idx = furthestsampling(x, offset, new_offset).long()
    return idx
```

[utils/general_utils.py:186-194](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/general_utils.py#L186-L194) 是 `fps` 封装：它假设**各批次等长**（都为 `n`，都要采 `k` 个），因此 `full` 一次即可；若要处理变长批次，调用方需自行构造 offset 直接调 `furthestsampling`。返回的 `idx` 是 `(b*k,)` 的扁平全局索引——对 `b=1`（下采样的实际用法）它就是原始点列表里的朴素索引，可以直接当 `mask` 用。

#### 4.2.4 代码实践

**实践目标**：亲手验证 offset 的语义，理解「前缀和 = 每批次结束位置」。

1. 实践目标：构造一个 3 批次变长点云，检查 offset 与 new_offset 的数值，并验证 FPS 返回的索引确实分段落在各自批次内。
2. 操作步骤（示例代码，需 GPU + 已编译 pointops2）：

```python
# 示例代码：验证 offset 约定
import torch
from utils.general_utils import fps

sizes, k = [1000, 500, 3500], 100
# 变长批次只能绕过等长的 fps 封装，直接调底层算子
from pointops2.functions.pointops import furthestsampling
xyz = torch.randn(sum(sizes), 3).cuda()
offset = torch.cuda.IntTensor([1000, 1500, 5000])      # cumsum([1000,500,3500])
new_offset = torch.cuda.IntTensor([100, 200, 300])     # cumsum([100]*3)
idx = furthestsampling(xyz, offset, new_offset).long()
seg1 = idx[(idx >= 0) & (idx < 1000)]
seg2 = idx[(idx >= 1000) & (idx < 1500)]
seg3 = idx[idx >= 1500]
print(len(seg1), len(seg2), len(seg3))   # 期望 100 100 100
```

3. 需要观察的现象：三段各得 100 个索引，且每段索引都严格落在自己批次的 `[start, end)` 区间内；对比 `torch.cumsum(torch.tensor([1000,500,3500]), 0)` 与手写的 `offset` 完全一致。
4. 预期结果：批次互不串点，证明 offset 前缀和正确定界；无 GPU 时可先用 `torch.cumsum` 部分验证 offset 数值（CPU 可跑），采样部分**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`fps` 封装为什么必须 `.contiguous()`？

答案：`view(-1, 3)` 只在内存连续时可安全执行，且 CUDA 侧 `FurthestSampling.forward` 第一行就 `assert xyz.is_contiguous()`，C++ 层按行指针偏移 `xyz[k*3+0]` 读数据，非连续内存会读到错误的坐标。

**练习 2**：`knn` 封装第 183 行 `idx.view(b, n, k) - (src_offset - m)[:, None, None]` 中，`src_offset - m` 为什么恰好是各批次起点？

答案：`src_offset[i] = (i+1) * m`（等长批次的前缀和），而批次 `i` 的首元素全局位置是 `i * m`，两者差恰为 `m`，故 `src_offset[i] - m = i*m`。对任意全局索引 `g ∈ [i*m, (i+1)*m)`，减去 `i*m` 即得批内局部索引。

**练习 3**：如果把 `offset` 传成 `[1000, 500, 1500]`（第二个批次比第一个短却排前面那样乱序的非单调序列），会发生什么？

答案：FPS 核会算出负的段长（`end_n - start_n < 0`），循环体不执行，产出错误或空的采样；knnquery 的 `get_bt_idx` 依赖 offset 单调递增做线性查找，非单调会死循环或返回错误批次。cumsum 生成的序列严格非降，是这一约定成立的前提。

### 4.3 readColmapSceneInfo 的下采样分支：random vs fps

#### 4.3.1 概念说明

u2-l2 已经讲过 `readColmapSceneInfo` 的总装配流程，本讲只钻「点云加工」这一步的下采样分岔。设计动机：4C4D 用 MASt3R 重建出的初始点云可能有几百万点，直接全部变成 4D 高斯会让训练起点就背上巨大的光栅化与致密化负担，因此需要按 `num_pts` 上限下采样。两种策略的性质截然不同：

| 维度 | `random` | `fps` |
|---|---|---|
| 抽样方式 | `np.random.randint`，**有放回**（索引可重复） | FPS 贪心，**无放回**（索引唯一） |
| 分布倾向 | **保密度**：密的地方采得多，稀的地方采得少 | **反密度**（均匀化）：优先覆盖离已选点最远的区域，稀疏区域被补齐 |
| 确定性 | 随机（受全局种子影响） | 确定（种子点固定为第 0 个点） |
| 计算成本 | \( O(k) \)，CPU 即可 | \( O(k \cdot n) \)，需 CUDA |
| 对初始几何的影响 | 保留 MASt3R 点云的密度分布（表面平坦区可能过采样，细薄结构可能欠采样） | 覆盖更均匀，细薄/稀疏结构更容易被保留 |

对 4D 高斯初始化的连锁影响：初始空间尺度由近邻距离决定（[scene/gaussian_model.py:422](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L422) 的 `distCUDA2`），random 下采样在密集区留下大量彼此很近的点（尺度初始化偏小），稀疏区则可能出现覆盖空洞；fps 下采样的近邻距离分布更紧致，尺度初始化更一致。

#### 4.3.2 核心流程

分支的完整决策流（承接 u2-l2 的点云加工段）：

```
points3D.bin → storePly 转换缓存 → fetchPly 读回 BasicPointCloud
    ↓
if num_pts_ratio > 1.001:        # 增强：向点云外接盒加随机点（此分支会丢 time 字段）
    在 mean±[0.5, 2.0, 0.5] 盒内均匀撒 (ratio-1)*N 个点，与原点云拼接
    ↓
if 点数 > num_pts 且 num_pts > 0:      # 下采样闸门
    ├─ 'random': mask = np.random.randint(0, N, num_pts)      # 有放回
    ├─ 'fps'   : mask = fps(points.cuda()[None], num_pts)     # 无放回、需 GPU
    └─ 其他     : raise ValueError
    ↓
mask 同时作用于 xyz / rgb / normals / time（若存在）
    ↓
若 time 存在：按 time_duration 过滤后重建 BasicPointCloud
```

参数从命令行到分支的传递链：`train.py --downsample_method`（默认 `random`，仅允许 `fps`/`random`）→ `training()` → `Scene(downsample_method=...)` → `readColmapSceneInfo(downsample_method=...)`。

#### 4.3.3 源码精读

分支本体只有六行，却藏着两个策略的全部差异：

```python
if pcd.points.shape[0] > num_pts and num_pts > 0:
    if downsample_method == 'random':
        mask = np.random.randint(0, pcd.points.shape[0], num_pts)
    elif downsample_method == 'fps':
        mask = fps(torch.from_numpy(pcd.points).cuda()[None], num_pts).cpu().numpy()
    else:
        raise ValueError(f"Unsupported downsample method: {downsample_method}")
```

[scene/dataset_readers.py:324-330](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L324-L330)：`random` 分支用的是 `np.random.randint`——**有放回**抽样，理论上 `mask` 里会有重复索引（点数远大于 `num_pts` 时重复比例约为 \( k^2/2N \)，例如 10 万点采 3 万约 4.5% 重复），最终点数恰好等于 `num_pts` 但有效点略少；`fps` 分支三步走——`torch.from_numpy(...).cuda()` 搬上 GPU、`[None]` 补出 `fps` 封装要求的 `(1, N, 3)` 批次维、`.cpu().numpy()` 取回索引数组。`raise ValueError` 兜底说明这是个受 `choices` 约束的封闭枚举（[train.py:423](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L423)）。

mask 随后被一致地应用到全部属性：

```python
if pcd.time is not None:
    times = pcd.time[mask]
else:
    times = None
xyz = pcd.points[mask]
rgb = pcd.colors[mask]
normals = pcd.normals[mask]
```

[scene/dataset_readers.py:331-344](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L331-L344)：xyz/rgb/normals/time 四个数组用**同一个 mask** 过滤再重组 `BasicPointCloud`——这就是 4.2 节强调「fps 返回的是原始数组朴素索引」的意义：它必须能与 numpy 花式索引直接互通。注意一个诚实细节：COLMAP 路线的 ply 由 `storePly` 生成（[scene/dataset_readers.py:160-175](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L160-L175) 的 dtype 只有 x/y/z/nx/ny/nz/rgb 八列，**没有 time 字段**），因此 `fetchPly` 返回的 `pcd.time` 恒为 `None`，时间过滤分支在 COLMAP 数据上实际不会触发。

函数签名与调用链的上游：

[scene/dataset_readers.py:255-258](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L255-L258) 定义 `readColmapSceneInfo(..., num_pts=100_000, downsample_method='random')`；[scene/__init__.py:52-54](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L52-L54) 把 `Scene` 收到的 `num_pts` 与 `downsample_method` 原样转发进来。Blender 路线则硬编码了 random：[scene/dataset_readers.py:481-483](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L481-L483)，fps 一行被注释掉——4D 动态数据（COLMAP/N3V 路线）才需要关心下采样策略的选择。

最后是 u1-l4 已标记过的「恒真守卫」在本分支的回响：

```python
parser.add_argument('--initial_num_pts', type=int, default=-1)
...
if args.initial_num_pts is not None:      # 默认 -1，is not None 恒真
    args.num_pts = args.initial_num_pts
```

[train.py:392](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L392) 与 [train.py:448-449](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L448-L449)：该守卫在 yaml 合并**之后**执行且条件恒真，所以 `num_pts` 实际总被 `initial_num_pts` 覆盖——不传参时默认 `-1`，令 [dataset_readers.py:324](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L324) 的闸门 `num_pts > 0` 为假，**下采样分支完全不触发**（官方 yaml 里的 `num_pts: 300_000` 也会被这行覆盖掉，见 [configs/dynerf/flame_steak.yaml:3](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/configs/dynerf/flame_steak.yaml#L3)）。要让 `--downsample_method` 真正生效，必须显式给 `--initial_num_pts`（或在 yaml 写 `initial_num_pts`）一个正数。诊断时先看输出目录 `training_params.txt` 里 `num_pts` 的最终值。

#### 4.3.4 代码实践

**实践目标**：用 1 万点合成点云对比 random 与 fps 下采样到 1 千点后的最近邻距离分布，直观看到「保密度 vs 均匀化」。

1. 操作步骤（示例代码；`fps` 分支需 GPU，CPU 参考实现可无 GPU 完成实验）：

```python
# 示例代码：random vs fps 下采样对比（保存为 compare_downsample.py，在仓库根目录运行）
import numpy as np, torch, matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

N, K, seed = 10_000, 1_000, 0
rng = np.random.default_rng(seed)
# 构造不均匀点云：90% 挤在一个小球里，10% 散在大球里（模拟 MASt3R 点云密度失衡）
n1, n2 = 9_000, 1_000
c1 = rng.normal(0, 0.3, (n1, 3))                       # 稠密团块
c2 = rng.normal(0, 3.0, (n2, 3))                       # 稀疏外围
xyz = np.concatenate([c1, c2]).astype(np.float32)

# 策略一：random（与 dataset_readers.py:326 逐字对齐，有放回）
mask_rand = np.random.randint(0, N, K)

# 策略二：fps（优先走项目封装；无 GPU 时退回 CPU 参考实现）
try:
    from utils.general_utils import fps
    mask_fps = fps(torch.from_numpy(xyz).cuda()[None], K).cpu().numpy()
    print('fps backend: pointops2 CUDA')
except Exception as e:
    print('fps backend: CPU reference,', e)
    sel, dist2 = [0], ((xyz - xyz[0]) ** 2).sum(1)     # 与 CUDA 核同语义：种子=第 0 点
    for _ in range(1, K):
        nxt = int(dist2.argmax()); sel.append(nxt)
        dist2 = np.minimum(dist2, ((xyz - xyz[nxt]) ** 2).sum(1))
    mask_fps = np.array(sel)

def nn_dist(p):                                        # 集合内两两最近邻距离
    t = torch.from_numpy(p)
    d = torch.cdist(t, t) + torch.eye(len(t)) * 1e9
    return d.min(1).values.numpy()

for name, mask in [('random', mask_rand), ('fps', mask_fps)]:
    p = xyz[mask]
    dup = len(mask) - len(set(mask.tolist()))
    d = nn_dist(p)
    print(f'{name}: 点数={len(p)}, 重复索引={dup}, NN距离 mean={d.mean():.4f} '
          f'std={d.std():.4f} min={d.min():.4f}')

fig, ax = plt.subplots(figsize=(6, 4))
ax.hist(nn_dist(xyz[mask_rand]), bins=50, alpha=0.6, label='random')
ax.hist(nn_dist(xyz[mask_fps]), bins=50, alpha=0.6, label='fps')
ax.set_xlabel('nearest-neighbor distance'); ax.set_ylabel('count')
ax.legend(); fig.savefig('compare_downsample.png', dpi=120)
```

```bash
python compare_downsample.py
```

2. 需要观察的现象：`random` 的直方图出现一个**极小距离尖峰**（稠密团块内部采出的点彼此紧贴），且 `重复索引` 一栏通常非零（有放回的证据，约 5% 量级）；`fps` 的直方图整体右移、更集中，`min` 明显更大（无近距离贴点、无重复索引）。
3. 预期结果：random 的 NN 距离呈「长尾 + 小距离尖峰」的双峰形态（继承了原始密度分布），fps 的 NN 距离近似单峰且均值更大（密度被均匀化）。具体数值依赖种子与构造，**待本地验证**；无 GPU 时 CPU 参考实现给出的结论方向一致。
4. 思考题（对应任务第二问）：对照 4.2 节，`offset` 用 `cumsum` 构造是因为 pointops2 采用「变长批次扁平拼接」的内存布局，前缀和既能免填充、又给核函数提供 O(1) 的段边界（且天然单调，满足 `get_bt_idx` 的查找前提）。

#### 4.3.5 小练习与答案

**练习 1**：`--downsample_method fps` 但忘了设 `--initial_num_pts`，会发生什么？

答案：什么都不发生。[train.py:448-449](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L448-L449) 的恒真守卫把 `num_pts` 覆盖为默认 `-1`，[dataset_readers.py:324](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L324) 的 `num_pts > 0` 为假，下采样分支被整段跳过，点云原样进入训练。验证方法：看 `training_params.txt` 中 `num_pts=-1`。

**练习 2**：两种策略最终输出的点数都是 `num_pts` 吗？

答案：都是，但含义不同。random 输出 `num_pts` 个**可能含重复**的采样（同一原始点被复制多份，有效点数略少）；fps 输出 `num_pts` 个**互不相同**的索引。若点云还带 time 字段，[dataset_readers.py:339-343](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L339-L343) 的时间过滤还会再削掉一批，最终点数可能低于 `num_pts`。

**练习 3**：为什么 `fps` 分支要 `[None]` 补一个维度，而 `random` 分支不需要？

答案：`utils/general_utils.fps` 的契约是 `(b, n, 3)` 批量张量（它要先 `view(-1, 3)` 扁平化并构造 offset），`[None]` 把 `(N, 3)` 变成 `(1, N, 3)` 即单批次；`np.random.randint` 只操作索引数组，与形状无关。

## 5. 综合实践

**任务：把下采样策略接入一次真实训练并预测其对初始几何的影响。**

1. 选一个已准备好的 COLMAP 格式数据集（或用 u2-l5 的 `n3v2colmap.py` 产物），确认 `sparse/0/points3D.ply` 存在且点数已知（记为 \( N \)，可用 `plyfile` 统计）。
2. 跑两组短训练（例如 `--iterations 3000`），唯一变量是下采样策略：

```bash
# 组 A：random（注意 initial_num_pts 必须显式给正数，否则分支不触发）
python train.py --config configs/dynerf/flame_steak.yaml \
    --initial_num_pts 100000 --downsample_method random \
    --model_path output/ds_rand --iterations 3000 --test_iterations 2500

# 组 B：fps（唯一差异是 downsample_method）
python train.py --config configs/dynerf/flame_steak.yaml \
    --initial_num_pts 100000 --downsample_method fps \
    --model_path output/ds_fps --iterations 3000 --test_iterations 2500
```

3. 训练前先写下预测（这是本实践的核心）：两组的初始点数相同（10 万），但 fps 组的初始近邻距离分布更均匀、random 组在稠密表面区更密。据此预测：TensorBoard 中 `total_points` 曲线前期斜率、`opacity_histogram` 的形态、以及 2500 迭代时测试 PSNR 的相对高低。
4. 训练后核对：读两个输出目录的 `training_params.txt` 确认 `num_pts=100000` 且 `downsample_method` 如预期；对比 TensorBoard 的 `total_points` 与测试 PSNR；把 4.3.4 节脚本改成读取两组 `input.ply`（各取前 1 万点）复算 NN 距离直方图，验证「均匀化」确实发生在真实数据上。
5. 交付物：一张三列表格（random / fps / 差异说明），涵盖初始 NN 距离统计、2500 迭代点数、2500 迭代 PSNR，外加一段 200 字分析：均匀覆盖对 4D 高斯早期几何学习（`distCUDA2` 尺度初始化 + 致密化梯度统计）意味着什么。若无 GPU 训练条件，交付物改为基于本讲两个实践脚本的实验设计文档，标注「待本地验证」。

## 6. 本讲小结

- pointops2 暴露 `furthestsampling`（FPS 选点，返回全局扁平索引，无 backward）与 `knnquery`（k 近邻，返回升序索引与已开方距离，`nsample ≤ 100` 的隐式上限）两个 CUDA 算子，经 `autograd.Function` 薄包装 + pybind11 登记表 + C++ 裸指针薄壳三层抵达核函数。
- FPS 核用 `tmp` 数组增量维护「每点到已选集合的最小距离平方」，把复杂度压到 \( O(m \cdot n) \)；一个 block 独占一个批次，种子点固定为段内第 0 个点，结果确定可复现。
- pointops2 的批处理约定是「变长批次扁平拼接 + offset 前缀和」：`utils/general_utils.py` 的 `fps`/`knn` 用 `full + cumsum + .int()` 构造 offset，`knn` 还负责全局索引到批内局部索引的换算；`knn` 封装当前无调用方，`fps` 是主框架唯一下采样入口。
- `readColmapSceneInfo` 的下采样分支里，`random` 是 `np.random.randint` **有放回**抽样（保密度、有重复索引），`fps` 是**无放回**均匀化采样（反密度、覆盖稀疏区），二者用同一 mask 过滤 xyz/rgb/normals/time。
- 陷阱：`train.py` 的 `initial_num_pts` 恒真守卫会覆盖 `num_pts`（默认 -1），不显式给正值时下采样分支整段不触发，`--downsample_method` 形同虚设；Blender 路线则硬编码 random。

## 7. 下一步学习建议

- 下一讲 u8-l3（稀疏视角策略全景与消融设计）会把本讲的 `downsample_method`、`num_pts`/`num_pts_ratio` 与 MASt3R 稠密初始化、opacity decay 一起放进「4 视角稀疏输入」的系统视角，建议先完成本讲综合实践再读。
- 若想继续深挖 pointops2 的其余算子，可读 [pointops2/functions/pointops.py:648-693](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/pointops2/functions/pointops.py#L648-L693) 的 `queryandgroup` 与 `Divide2Patch`——它们是 `knnquery` + `furthestsampling` 的组合应用，展示了 offset 约定如何支撑「先采样锚点、再按锚点分组」的点云 transformer 典型流水线。
- 对照阅读 simple-knn 的 `distCUDA2`（消费点在 [scene/gaussian_model.py:422](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L422)）：理解「KNN 取值」与「KNN 取索引」两个同族算子如何在初始化链路中一前一后配合。
- 想理解 FPS 为何是「好的」采样，可检索覆盖半径（covering radius）与 max-min 准则的关系，思考它与 u5-l4 致密化的 clone/split 在「几何覆盖」目标上的呼应。
