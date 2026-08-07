# u9-l1 HNSW 向量索引

## 1. 本讲目标

本讲进入「向量检索、语义缓存与记忆」单元（U9）的第一站，聚焦 `pkg/hnsw` 这个**纯 Go 实现的 HNSW（Hierarchical Navigable Small World，分层可导航小世界）**库。

读完本讲，你应当能够：

- 说清 HNSW 为什么能把相似度搜索从暴力搜索的 \(O(n)\) 降到近似 \(O(\log n)\)，以及它是用什么数据结构做到的。
- 解释三个核心参数 `M` / `EfConstruction` / `EfSearch` 各自控制什么、如何影响「召回率 vs 延迟」的取舍。
- 读懂 `Add`（插入建图）、`Search`（分层贪心 + beam search）、`Clear`（清空）的真实源码流程，包括双堆（min-heap + max-heap）在图层遍历里的作用。
- 理解距离计算热点 `dotProductSIMD` 如何在运行时按 CPU 能力分派到 AVX-512 / AVX2 / 标量三条路径。
- 动手跑一个「不同 `efSearch` 下的召回率与耗时」扫描实验。

承接：u8-l4 讲过嵌入提供者（`pkg/embedding`）把文本变成 `[]float32` 向量。本讲回答「拿到一堆向量之后，如何高效地找出最相似的那几个」——这是语义缓存、记忆检索、工具检索共同的下层能力。

## 2. 前置知识

### 向量、点积与余弦相似度

一个嵌入（embedding）是一个固定长度的浮点向量 \(a \in \mathbb{R}^d\)。两个向量的**点积（dot product）**定义为：

\[
a \cdot b = \sum_{i=1}^{d} a_i b_i
\]

如果两个向量都被 **L2 归一化**（长度为 1），那么点积就等于**余弦相似度（cosine similarity）**，取值落在 \([-1, 1]\)，越大越相似。Semantic Router 的嵌入在送进 HNSW 前默认按归一化处理，所以本包直接用点积当相似度，不再单独算余弦。

### 近邻搜索：精确 vs 近似

给定一个查询向量 \(q\) 和库里 \(n\) 个向量，想找最相似的 top-k：

- **暴力搜索（brute-force）**：把 \(q\) 和每一个向量都算一次点积，再排序。复杂度 \(O(n \cdot d)\)，\(n\) 一大就慢。
- **近似最近邻（ANN, Approximate Nearest Neighbor）**：牺牲一点点精度，换 \(O(\log n)\) 级别的查询。HNSW 是当前最主流的图索引 ANN 算法之一。

### 优先队列（堆）

HNSW 的图层遍历要用到两种堆：

- **最小堆（min-heap）**：堆顶是距离最小的元素，用来「总是先扩展最近的候选」。
- **最大堆（max-heap）**：堆顶是距离最大的元素，用来「保留最近的 ef 个、把最差的顶出去」。

本包用手写数组堆实现，稍后会读到。

## 3. 本讲源码地图

本讲只涉及一个包，共 4 个文件：

| 文件 | 作用 | 是否需要 CGO |
| --- | --- | --- |
| [src/semantic-router/pkg/hnsw/hnsw.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/hnsw/hnsw.go) | HNSW 主体：`Index` / `Node` 数据结构、`Add` / `Search` / `Clear`、图层遍历、邻居选择、两个堆 | 是（构建标签 `!windows && cgo`） |
| [src/semantic-router/pkg/hnsw/simd_distance_amd64.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/hnsw/simd_distance_amd64.go) | amd64 平台的距离计算分派：按 CPU 特征选 AVX-512 / AVX2 / 标量 | 是（构建标签 `amd64 && !purego`） |
| [src/semantic-router/pkg/hnsw/simd_distance_amd64.s](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/hnsw/simd_distance_amd64.s) | Plan9 汇编写的 `dotProductAVX2` / `dotProductAVX512`，逐 8 / 16 个 float32 做融合乘加 | — |
| [src/semantic-router/pkg/hnsw/simd_distance_generic.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/hnsw/simd_distance_generic.go) | 非 amd64（或 `purego` 构建标签）下的纯标量回退实现 | 否 |

> 一个工程上的重要事实：`pkg/hnsw` 是一个**自包含、可复用的纯 Go 库**。在本仓库当前代码里，它没有被其他业务包直接 `import`（业务侧的语义缓存 / 记忆走的是各自后端——Milvus、Qdrant、Redis/Valkey 的**服务端 HNSW 索引**，或 `pkg/cache` 内自带的专用 HNSW 遍历）。换句话说，本包更像是仓库内置的「进程内 ANN 参考实现 / 备用引擎」，理解它就理解了 Semantic Router 所有向量检索能力的算法内核。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**① HNSW 图结构与配置参数**、**② 增查与清空（Add / Search / Clear）**、**③ SIMD 距离优化**。

### 4.1 HNSW 图结构与配置参数

#### 4.1.1 概念说明

为什么要造一种新的图结构？想象你要在一座大城市里找「离我最近的咖啡馆」。暴力做法是把全城每家店都量一遍距离——店越多越慢。HNSW 借鉴了**跳表（skip list）**的思路：把节点分层，最底层（layer 0）放**全部**节点且连接稠密，越往上节点越稀疏、连接越长（「高速公路」）。查询时先在顶层稀疏图里大步逼近目标区域，再逐层下沉到底层做精细搜索，从而把复杂度压到近似 \(O(\log n)\)。

三个关键直觉：

1. **分层 = 不同尺度的导航**：上层稀疏图负责「远距离快速定位」，底层稠密图负责「近距离精确召回」。
2. **概率分层**：每个节点插入时按概率被分配到一个最高层级，绝大多数节点只存在于 layer 0，少数能往上长。
3. **可导航小世界（NSW）**：每个节点维护少量邻居，既包含近距离邻居（精确）也包含少量远距离邻居（跳板），使图具备「小世界」特性——任意两点间跳数很少。

#### 4.1.2 核心流程：层级是如何形成的

新节点插入时，先用一个**指数分布**随机抽样决定它的层级 `level`。本包的抽样公式是：

\[
\text{level} = \left\lfloor -\ln\bigl(\max(10^{-9},\; 1-U)\bigr) \cdot m_L \right\rfloor,\qquad m_L = \frac{1}{\ln M}
\]

其中 \(U \sim \text{Uniform}[0,1)\) 是一个随机数。因为 \(1-U\) 同样服从均匀分布，\(-\ln(1-U)\) 服从参数为 1 的指数分布。可以推出一个节点**被分配到第 \(l\) 层及以上**的概率为：

\[
P(\text{level} \ge l) = M^{-l}
\]

也就是说，每往上一层，节点数大约缩为原来的 \(1/M\)。对 \(M=16\)，约只有 6.25% 的节点能进入 layer 1。于是 \(n\) 个节点的图期望层数约为 \(\log_M n\)，查询时每层只访问少量节点，总体近似 \(O(\log n)\)。

#### 4.1.3 源码精读

**节点 `Node`** 持有向量、按层组织的邻居表、以及该节点出现的最高层：

[src/semantic-router/pkg/hnsw/hnsw.go:27-32](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/hnsw/hnsw.go#L27-L32) —— `Node` 结构：`neighbors` 是「层 → 邻居 ID 列表」的 map，`maxLayer` 记录该节点最高出现在哪一层。

**索引 `Index`** 是核心容器，注意它用一把 `sync.RWMutex` 保证「并发读安全、写互斥」：

[src/semantic-router/pkg/hnsw/hnsw.go:35-47](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/hnsw/hnsw.go#L35-L47) —— `Index` 的全部字段。重点理解四个量：`M`（每层每节点新建连接数，默认 16）、`Mmax`（上层每节点最大连接数，等于 `M`）、`Mmax0`（layer 0 最大连接数，等于 `2*M`，底层更稠密）、`ml`（上面公式里的 \(m_L\) 归一化因子）。

**配置 `Config` 与默认值**：

[src/semantic-router/pkg/hnsw/hnsw.go:49-71](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/hnsw/hnsw.go#L49-L71) —— 三个可调参数及其含义：

| 参数 | 默认 | 控制什么 | 调大的影响 |
| --- | --- | --- | --- |
| `M` | 16 | 每个节点每层的连接数；决定图的密度与内存 | 召回率↑、内存↑、构建略慢 |
| `EfConstruction` | 200 | 建图时动态候选列表大小 | 索引质量↑、建图耗时↑ |
| `EfSearch` | 50 | 查询时动态候选列表大小 | 查询召回率↑、查询延迟↑ |

**`NewIndex` 构造函数**给零值填默认，并算出派生量：

[src/semantic-router/pkg/hnsw/hnsw.go:80-103](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/hnsw/hnsw.go#L80-L103) —— 注意 `Mmax0 = M * 2`（底层允许双倍连接），以及 `ml = 1.0 / math.Log(float64(cfg.M))` 正是上面层分布公式里的 \(m_L\)。

**层级抽样 `selectLevel`**：

[src/semantic-router/pkg/hnsw/hnsw.go:242-245](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/hnsw/hnsw.go#L242-L245) —— 这里用 `math/rand/v2` 的 `rand.Float64()`。这是一个有历史故事的点：早期版本用过「基于时间戳的伪随机」（提交 `1fc9158e`），后来被修正为标准库 v2 的自动播种随机源，避免并发插入时多节点拿到相同层级。

#### 4.1.4 代码实践（阅读 + 推算）

1. **目标**：用纸笔验证层分布公式，建立对「为什么是 \(O(\log n)\)」的直觉。
2. **步骤**：
   - 取 \(M=16\)、\(n=1024\) 个节点。
   - 用公式 \(P(\text{level} \ge l) = M^{-l}\) 估算各层的期望节点数。
3. **预期结果**：layer 0 有 1024 个；layer ≥1 约 \(1024/16 = 64\) 个；layer ≥2 约 4 个；layer ≥3 不到 1 个。也就是说期望最高层大约在 2~3 层，对应 \(\log_{16}1024 \approx 2.5\)。节点数翻 16 倍，层数才涨 1，这就是对数复杂度的来源。
4. 结论标注：本练习为推算型，**无需运行**；数值是期望值，实际运行会有随机波动。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `M` 从 16 调到 4，`NewIndex` 里 `ml` 会变成多少？对图结构有什么影响？

> **答案**：\(m_L = 1/\ln 4 \approx 0.721\)（\(M=16\) 时约为 0.361）。\(m_L\) 变大意味着同样的随机数会映射到更高层数，于是节点更容易「往上长」，层数变多但每层更稀疏。极端情况下图会更高更瘦，查询时上层跳数增多。

**练习 2**：`Mmax0 = 2*M` 这个设计为什么只对 layer 0 翻倍？

> **答案**：layer 0 装了全部节点，是精确召回的主战场，需要更稠密的连接避免漏掉近邻；上层只是导航骨架，节点少，连接数翻倍既无必要也浪费内存。

### 4.2 增、查与清空（Add / Search / Clear）

> ⚠️ **诚实说明**：本讲的大纲模块名写作「增删查」，但真实源码里 **`pkg/hnsw` 没有按节点删除（Delete）的方法**。删除操作只提供**整体清空 `Clear()`**。如果你看到别处宣称「HNSW 支持 delete」，那指的是 Milvus/Qdrant 等**外部向量库的服务端 HNSW**，不是本包。下面按实际代码讲解「增 / 查 / 清空」。

#### 4.2.1 概念说明

插入（`Add`）要做两件事：给新节点抽一个层级，然后在它出现的每一层里找到最近的若干邻居、建立双向连接，并在邻居连接数超限时做剪枝。查询（`Search`）则自顶向下：在上层用贪心搜索（只要最近的 1 个）快速下沉到目标区域，到底层用 beam search（保留 ef 个候选）做精细搜索，最后按相似度排序返回 top-k。

#### 4.2.2 核心流程

**插入 `addNode`（调用方持写锁）**：

```
addNode(id, embedding):
  level ← selectLevel()                 # 抽层级
  若是首个节点 → 直接作为 entryPoint，返回
  for lc from min(level, maxLayer) downto 0:   # 从高到低逐层接入
      candidates ← searchLayer(embedding, entryPoint, efConstruction, lc)
      M_lc ← Mmax0 if lc==0 else Mmax
      neighbors ← selectNeighbors(candidates, M_lc, embedding)   # 选最近的 M 个
      建立 id ↔ neighbors 的双向连接
      对每个被连接的邻居：若其连接数 > M_lc，则用 selectNeighbors 剪枝
  若 level > maxLayer → 更新 entryPoint 与 maxLayer
```

**图层遍历 `searchLayer`（核心算法）** 用两个堆做 best-first beam search：

```
searchLayer(query, entryPoint, ef, layer):
  visited ← {entryPoint}
  candidates ← minHeap([entryPoint])      # 按距离「近→远」弹出，决定扩展顺序
  results ← maxHeap([entryPoint])          # 按「远→近」堆顶，保留最近的 ef 个
  while candidates 非空:
      current ← candidates 弹出最近者
      if results 已满 且 current 比堆顶(最差结果)还远 → break   # 收敛判据
      for neighbor in current.neighbors[layer]:
          若未访问 → 计算距离，尝试加入 candidates 与 results（results 超过 ef 则弹出最差）
  return results 中全部 id
```

**查询 `SearchWithEf`**：

```
SearchWithEf(query, k, ef):
  # 第一阶段：从最高层到 layer 1，每层贪心找最近 1 个，作为下沉入口
  current ← entryPoint
  for lc from maxLayer downto 1:
      current ← searchLayer(query, current, ef=1, lc)[0]
  # 第二阶段：layer 0 做 beam search
  ef ← max(efSearch, k)  （或显式传入的 ef，并对 2*n 封顶）
  candidates ← searchLayer(query, current, ef, 0)
  把 candidates 重新算点积相似度 → 排序降序 → 截取前 k 个返回
```

**暴力搜索 `SearchAll`**：直接遍历所有节点算点积，作为精确 ground truth，适合小索引或需要精确结果的场景（也是后面测召回率的对照基准）。

**`Clear`**：重建空 map、重置 `entryPoint=-1`、`maxLayer=-1`，整体清空。

#### 4.2.3 源码精读

**对外入口 `Add` / `AddBatch`**——都先抢写锁再委托 `addNode`：

[src/semantic-router/pkg/hnsw/hnsw.go:106-121](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/hnsw/hnsw.go#L106-L121) —— `Add` 插入单条，`AddBatch` 接 `map[int][]float32` 批量插入，共用同一把写锁。

**建图主体 `addNode`**——逐层接入 + 双向连接 + 邻居剪枝：

[src/semantic-router/pkg/hnsw/hnsw.go:248-303](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/hnsw/hnsw.go#L248-L303) —— 关键点：① `for lc := min(level, h.maxLayer); lc >= 0; lc--` 只在新节点能到达的层及以下建连；② `M := h.Mmax; if lc == 0 { M = h.Mmax0 }` 让底层连更多邻居；③ 给邻居加反向连接后立即检查 `len > M` 并剪枝，保持图的度数约束。

**双堆遍历 `searchLayer` + `tryAddNeighbor`**——HNSW 查询/建图共用同一套 beam search：

[src/semantic-router/pkg/hnsw/hnsw.go:306-363](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/hnsw/hnsw.go#L306-L363) —— `candidates`（minHeap）决定「下一步扩展谁」，`results`（maxHeap）维护「当前最近的 ef 个」。`tryAddNeighbor` 里的判据 `results.len() >= ef && dist >= results.peekDist()` 是性能与质量的平衡：只有比现有最差结果更好、或结果集还没满时才纳入。

**邻居选择 `selectNeighbors`**——简单「按距离取最近 m 个」启发式：

[src/semantic-router/pkg/hnsw/hnsw.go:366-404](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/hnsw/hnsw.go#L366-L404) —— 注意这是 HNSW 论文里的「简单选择策略（select-neighbors-simple）」，即纯按距离排序截取；并非论文里更复杂的「启发式选择策略」（后者会额外考虑候选之间互相远离以提升图连通性）。简单策略实现直观、速度快，是常见工程取舍。

**查询入口 `Search` / `SearchWithEf` / `SearchAll`**：

[src/semantic-router/pkg/hnsw/hnsw.go:125-185](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/hnsw/hnsw.go#L125-L185) —— `Search` 是 `SearchWithEf(..., ef=0)` 的便捷调用；`SearchWithEf` 先上层贪心（ef=1）再底层 beam search，并对 `ef` 做了 `max(efSearch, k)` 与 `2*n` 封顶两道保护，避免极端参数下的过量计算；最后对所有候选**重新算一次点积**再排序，保证返回的 `Similarity` 字段准确。

[src/semantic-router/pkg/hnsw/hnsw.go:189-208](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/hnsw/hnsw.go#L189-L208) —— `SearchAll` 暴力遍历，作为精确基准。

**辅助与清空 `Size` / `GetEmbedding` / `Clear`**：

[src/semantic-router/pkg/hnsw/hnsw.go:211-237](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/hnsw/hnsw.go#L211-L237) —— 全部加读/写锁保护；`Clear` 是唯一的「删除」语义，整表清空。

**两个手写堆**——本包没有用 `container/heap`，而是手写了 min-heap 与 max-heap：

[src/semantic-router/pkg/hnsw/hnsw.go:423-565](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/hnsw/hnsw.go#L423-L565) —— `maxHeap` 额外提供了 `peekDist()`（看堆顶距离而不弹出）和 `items()`（导出全部 id），正是 `searchLayer` 的收敛判据与返回值所需。

#### 4.2.4 代码实践（最小插入与查询）

下面是一段**示例代码**（仓库中不存在，供读者保存为 `pkg/hnsw/example_test.go` 来体验）。它构造 5 个二维归一化向量，插入后查询与 `q` 最相似的 2 个：

```go
// 示例代码：保存为 src/semantic-router/pkg/hnsw/example_test.go
//go:build !windows && cgo

package hnsw

import (
	"fmt"
	"math"
	"testing"
)

func TestExample_MinimalSearch(t *testing.T) {
	idx := NewIndex(DefaultConfig())
	// 5 个归一化向量
	vecs := map[int][]float32{
		0: normVec(1, 0),
		1: normVec(0, 1),
		2: normVec(1, 1),
		3: normVec(-1, 0),
		4: normVec(0, -1),
	}
	idx.AddBatch(vecs)

	q := normVec(0.9, 0.1) // 与 0 号(1,0) 最接近
	for _, r := range idx.Search(q, 2) {
		fmt.Printf("id=%d  similarity=%.3f\n", r.ID, r.Similarity)
	}
}

func normVec(x, y float32) []float32 {
	n := float32(math.Sqrt(float64(x*x + y*y)))
	return []float32{x / n, y / n}
}
```

1. **目标**：跑通最小 Add→Search 闭环，理解返回值含义。
2. **操作步骤**：把上面文件放到 `src/semantic-router/pkg/hnsw/` 下，确保 `CGO_ENABLED=1`，执行 `cd src/semantic-router && go test -run TestExample_MinimalSearch -v ./pkg/hnsw/`。
3. **观察现象**：打印的 `id=0` 应排在最前（其向量 `(1,0)` 与查询 `(0.9,0.1)` 点积最高）。
4. **预期结果**：第一行是 `id=0 similarity≈0.994`（点积 \(0.9\cdot1+0.1\cdot0=0.9\)，归一化后约 0.994），第二行是 `id=2` 或其他。**精确数值待本地验证**（取决于归一化与浮点）。
5. 若运行报「build constraints excluded」，多半是 CGO 未启用或平台不符，见 4.3.4 的说明。

#### 4.2.5 小练习与答案

**练习 1**：`searchLayer` 里的收敛判据 `currentDist > results.peekDist()`（结果集已满时）省略掉会怎样？

> **答案**：会退化成「把当前层所有可达节点都遍历一遍」，丧失提前终止能力，查询复杂度从近似 \(O(\log n)\) 恶化，接近全图扫描。该判据是 beam search 控制成本的关键。

**练习 2**：为什么 `SearchWithEf` 在最后还要对所有候选「重新算一次点积再排序」，而不是直接用 `searchLayer` 内部的距离？

> **答案**：`searchLayer` 内部用的是 `distance = -点积`（见 4.3），且堆里只保证「最近的 ef 个」被保留，并未按最终相似度严格排序输出。重新计算并排序能保证返回结果是干净的「相似度降序」top-k，也避免了内部状态泄漏。

**练习 3**：本包为何不提供按节点 `Delete`？

> **答案**：在图索引里删一个节点要修复它的所有邻居的连接、可能破坏图的 navigability，实现复杂且易引入性能/正确性问题。本包选择只提供 `Clear()` 整体清空；需要单条删除语义的场景，项目交给 Milvus/Qdrant 等外部向量库的服务端 HNSW 去处理。

### 4.3 SIMD 距离优化

#### 4.3.1 概念说明

HNSW 的几乎全部算力都花在一件事上：**反复计算两个向量的点积**（建图时找邻居、查询时比较候选）。一次查询可能要算成百上千次点积，每次点积又是对 \(d\) 维（常用 768、1024）向量做乘加。所以点积必须尽可能快。

**SIMD（Single Instruction, Multiple Data）** 是 CPU 的单指令多数据并行能力：一条指令同时处理多个浮点数。x86 上：

- **AVX2**：256 位 YMM 寄存器，一次处理 **8 个 float32**。
- **AVX-512**：512 位 ZMM 寄存器，一次处理 **16 个 float32**。

配合 **FMA（Fused Multiply-Add）** 指令 `VFMADD231PS`，可在一条指令里完成「乘 + 加」的累加，把点积主循环压到极致。

#### 4.3.2 核心流程：运行时分派 + 距离定义

本包用 Go 的**构建标签（build tag）** + **运行时 CPU 检测**双层分派：

1. **编译期**：amd64 且未加 `purego` 标签时，编译 `simd_distance_amd64.go`（含汇编）；否则编译 `simd_distance_generic.go`（纯标量）。
2. **运行期**（仅 amd64 路径）：`init()` 里用 `golang.org/x/sys/cpu` 探测 `HasAVX512F` / `HasAVX2`，`dotProductSIMD` 按向量长度与 CPU 能力选 AVX-512 / AVX2 / 标量。

距离的定义藏在 `Index.distance` 里：**距离 = 负点积**。因为嵌入已归一化，点积即余弦相似度，取负后「相似度越高 → 距离越小」，正好喂给 min-heap（近的先扩展）。

#### 4.3.3 源码精读

**Go 分派层**——按 CPU 特征与向量长度选路径：

[src/semantic-router/pkg/hnsw/simd_distance_amd64.go:10-42](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/hnsw/simd_distance_amd64.go#L10-L42) —— `init()` 在启动时探测一次 CPU 特征；`dotProductSIMD` 的分派规则是：`hasAVX512 && minLen>=16` 走 AVX-512，否则 `hasAVX2 && minLen>=8` 走 AVX2，再否则标量。短向量（不足一个 SIMD 宽度）直接走标量，避免无谓的尾处理开销。

**AVX2 汇编**——每轮处理 8 个 float32：

[src/semantic-router/pkg/hnsw/simd_distance_amd64.s:1-55](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/hnsw/simd_distance_amd64.s#L1-L55) —— `VXORPS Y0,Y0,Y0` 清零累加器；主循环用 `VMOVUPS` 各载入 8 个 float32，再用 `VFMADD231PS Y1,Y2,Y0`（Y0 += Y1*Y2）做融合乘加；尾部用 `VEXTRACTF128`/`VHADDPS` 做水平求和，不足 8 的余数标量处理。

**AVX-512 汇编**——每轮处理 16 个 float32，结构同上但用 ZMM 寄存器：

[src/semantic-router/pkg/hnsw/simd_distance_amd64.s:57-113](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/hnsw/simd_distance_amd64.s#L57-L113) —— 主循环 `VFMADD231PS Z1,Z2,Z0` 一次累加 16 个乘积，理论吞吐是 AVX2 的两倍；水平求和需要多一步把 512 位降阶到 128 位。

**标量回退**——非 amd64 或 `purego` 构建用：

[src/semantic-router/pkg/hnsw/simd_distance_generic.go:1-22](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/hnsw/simd_distance_generic.go#L1-L22) —— 一个朴素的 for 循环逐元素乘加，保证可移植性（ARM、或用 `purego` 做纯 Go 构建时仍可用）。

**距离 = 负点积**：

[src/semantic-router/pkg/hnsw/hnsw.go:406-411](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/hnsw/hnsw.go#L406-L411) —— `distance` 返回 `-dotProductSIMD(a,b)`，把「相似度越大越好」翻成「距离越小越好」，统一了堆的比较方向。这也是为什么 `SearchWithEf` 最终对外又把点积当 `Similarity` 直接返回（再取一次负号就还原成相似度）。

#### 4.3.4 代码实践（阅读 + 构建）

1. **目标**：理解三层分派，并确认本机走哪条路径。
2. **步骤**：
   - 阅读上面四个代码点，在汇编里找到 AVX2 与 AVX-512 各自的 `VFMADD231PS` 指令。
   - 运行 `cat /proc/cpuinfo | grep -o 'avx2\|avx512f' | sort -u`（Linux）确认本机 CPU 特征。
   - 注意 `hnsw.go` 顶部的构建标签 `//go:build !windows && cgo`：该包**只在非 Windows 且启用 CGO 时编译**。因此实践命令需要 `CGO_ENABLED=1`，且目前不支持 Windows 原生构建。
3. **预期结果**：在支持 AVX2 的 x86 机器上，768 维点积会走 AVX2 路径；支持 AVX-512 的机器（较新服务器 CPU）走 AVX-512。**具体 CPU 特征待本地验证**。
4. 若想量化加速比，可自行写一个 `BenchmarkDotProduct`（内部测试 `package hnsw`，分别调用 `dotProductSIMD` 与 `dotProductScalar`），但**精确耗时待本地验证**——通常 SIMD 路径在长向量上比标量快数倍。

#### 4.3.5 小练习与答案

**练习 1**：为什么分派条件里除了 `hasAVX2` 还要加 `minLen >= 8`？

> **答案**：AVX2 一次处理 8 个 float32，向量短于 8 时主循环一次都转不起来，反而要靠标量尾处理，不如直接走标量。`minLen >= 8` 是避免「为 SIMD 而 SIMD」反而变慢的门槛。

**练习 2**：把构建标签换成 `purego` 重新编译，`dotProductSIMD` 会走哪条路径？性能如何？

> **答案**：`purego` 标签使 `simd_distance_generic.go` 生效、amd64 版被排除，于是即使 CPU 支持 AVX2 也只能走纯标量 for 循环。这是「纯 Go、无汇编」可移植构建的代价——能在更多环境编译，但点积明显更慢。

## 5. 综合实践：`efSearch` 召回率与耗时扫描

这是本讲的主实践，把三个模块串起来：建一个规模可观的索引，用 `SearchAll`（暴力）当 ground truth，对比不同 `efSearch` 下 `SearchWithEf` 的**召回率（recall@k）**与**耗时**，亲眼看到「召回率 vs 延迟」的取舍曲线。

把下面这段**示例代码**保存为 `src/semantic-router/pkg/hnsw/recall_sweep_test.go`：

```go
// 示例代码：efSearch 召回率与耗时扫描
//go:build !windows && cgo

package hnsw

import (
	"math"
	"math/rand/v2"
	"testing"
	"time"
)

func normRandVec(dim int, rng *rand.Rand) []float32 {
	v := make([]float32, dim)
	var sum float64
	for i := range v {
		v[i] = float32(rng.NormFloat64())
		sum += float64(v[i]) * float64(v[i])
	}
	n := float32(math.Sqrt(sum))
	for i := range v {
		v[i] /= n
	}
	return v
}

func TestRecallSweep_EfSearch(t *testing.T) {
	const (
		n   = 2000 // 库大小
		dim = 128  // 向量维度
		k   = 10   // top-k
	)
	rng := rand.New(rand.NewPCG(1, 2)) // 固定种子，结果可复现

	idx := NewIndex(Config{M: 16, EfConstruction: 200})
	vecs := make([][]float32, n)
	for i := 0; i < n; i++ {
		vecs[i] = normRandVec(dim, rng)
		idx.Add(i, vecs[i])
	}

	queries := make([][]float32, 50)
	for i := range queries {
		queries[i] = normRandVec(dim, rng)
	}

	for _, ef := range []int{10, 20, 50, 100, 200} {
		var hits int
		start := time.Now()
		for _, q := range queries {
			gt := idx.SearchAll(q, k) // 暴力搜索 = ground truth
			gtSet := make(map[int]struct{}, k)
			for _, r := range gt {
				gtSet[r.ID] = struct{}{}
			}
			approx := idx.SearchWithEf(q, k, ef) // HNSW 近似
			for _, r := range approx {
				if _, ok := gtSet[r.ID]; ok {
					hits++
				}
			}
		}
		recall := float64(hits) / float64(len(queries)*k)
		t.Logf("efSearch=%-4d recall@%d=%.3f  总耗时=%v", ef, k, recall, time.Since(start))
	}
}
```

1. **实践目标**：验证「`efSearch` 越大召回率越高、耗时越长」，并找到性价比拐点。
2. **操作步骤**：
   - 确认在 `src/semantic-router/` 目录下，执行 `CGO_ENABLED=1 go test -run TestRecallSweep_EfSearch -v ./pkg/hnsw/`。
3. **需要观察的现象**：随 `ef` 从 10 → 200，`recall@10` 单调上升并逐渐逼近 1.0；总耗时也单调上升。
4. **预期结果**：典型趋势大致如下（**具体数值待本地验证**，取决于 CPU、数据随机性）：

   | efSearch | recall@10（参考） | 趋势 |
   | --- | --- | --- |
   | 10 | ~0.65 | 快但漏得多 |
   | 50（默认） | ~0.92 | 默认性价比点 |
   | 200 | ~0.99 | 接近精确，但更慢 |

5. **进阶实验**（可选）：
   - 把 `M` 从 16 改成 8 与 32，对比相同 `efSearch` 下的召回率（`M` 越大图越稠密、召回越高、内存越大）。
   - 把 `dim` 改成 768（贴近真实嵌入维度），观察耗时上升幅度。
   - 若想看 SIMD 的贡献，可在支持 AVX2 的机器上用 `purego` 标签重编（`CGO_ENABLED=1 go test -tags purego ...`，注意会走标量点积）对比耗时——**结果待本地验证**。

> 设计提示：这条 recall/延迟曲线正是 Semantic Router 在语义缓存里调参的依据——`efSearch` 太小会「该命中却没命中」（缓存召回率低），太大又会让每次缓存查询变贵。本包给了默认值 50，是一个偏保守的通用平衡点。

## 6. 本讲小结

- **HNSW 用分层图把相似度搜索压到近似 \(O(\log n)\)**：上层稀疏做远距离导航，底层稠密做精确召回；节点层级按 \(P(\text{level}\ge l)=M^{-l}\) 的指数分布抽样，期望层数约 \(\log_M n\)。
- **三个参数各有分工**：`M` 控制图密度与内存，`EfConstruction` 控制建图质量，`EfSearch` 控制查询的「召回率 vs 延迟」取舍。
- **建图 = 抽层 + 逐层 beam search 找邻居 + 双向连接 + 剪枝**；查询 = 上层贪心（ef=1）下沉 + 底层 beam search（ef 个候选）+ 重排取 top-k。核心遍历用 **min-heap（扩展顺序）+ max-heap（保留 ef 个最近）** 双堆，配合 `currentDist > peekDist()` 提前终止。
- **本包只支持 `Add` / `AddBatch` / `Search` / `SearchAll` / `Clear`**，没有按节点删除；删除语义交给外部向量库的服务端 HNSW。
- **距离 = 负点积**（嵌入已归一化，点积即余弦相似度）；点积是性能热点，`dotProductSIMD` 在 amd64 上按 CPU 能力分派 AVX-512 / AVX2 / 标量，汇编用 FMA 指令一次处理 16 / 8 个 float32，非 amd64 走纯标量回退。
- **构建约束**：`hnsw.go` 带 `!windows && cgo` 标签，实践时须 `CGO_ENABLED=1` 且在非 Windows 平台运行。

## 7. 下一步学习建议

本讲解完了「进程内 ANN 引擎」的算法内核。接下来：

- **u9-l2 向量存储与多后端**：看 `pkg/vectorstore` 如何把「切块 → 嵌入 → 写入」串成摄入流水线，以及 Milvus / Qdrant / Valkey / LlamaStack 等后端如何用各自的服务端 HNSW（如 `entity.NewIndexHNSW`）替代本包的进程内索引——理解「自建 HNSW」与「数据库 HNSW」的分工。
- **u9-l3 语义缓存**：看 `pkg/cache` 如何在 HNSW 之上做请求-响应相似度缓存、用户作用域隔离与两级 hybrid 缓存，把本讲的召回率/延迟取舍用到真实缓存命中决策里。
- **u9-l4 智能体记忆**：看 `pkg/memory` 如何用向量检索 + BM25 重排选出记忆，进一步体会点积相似度在「语义」之外的混合排序用法。

如果想再深入算法本身，建议对照 HNSW 原论文（Malkov & Yashunin, 2016/2018）的「Algorithm 1~5」逐行对照本包的 `addNode` / `searchLayer` / `selectNeighbors`，重点比较本包用的「简单邻居选择」与论文的「启发式邻居选择」之差异。
