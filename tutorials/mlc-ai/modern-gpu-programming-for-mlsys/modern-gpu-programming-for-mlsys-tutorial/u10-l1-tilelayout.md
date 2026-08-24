# u10-l1 TileLayout：S、R 与 offset

## 1. 本讲目标

前两单元里，「布局」一直是一个被引用的结论：u4 系列讲义告诉我们布局是「逻辑索引到物理位置的函数」，u9-l3 又把它列为 TIRx 三要素中决定**地址计算**的那一环。但直到现在，我们看到的布局都是现成的——`hgemm_v1` 里一句 `layout=TileLayout(S[(128, 512) : (1@TLane, 1@TCol)])` 拿来就用。本讲把这台机器拆开：**布局对象本身是怎么构造出来的**。

读完本讲，你应该能够：

1. **用 `S[...]` 与 `R[...]` 构造布局**：写出 `TileLayout(S[shape : strides] + R[replica_shape : replica_strides] + offset)` 的完整形式，并说清每个组成部分的职责。
2. **解释 shard/replica/offset 三要素的组合语义**：对一个逻辑坐标 \(x\)，说出布局描述的物理位置集合 \(L(x) = \{D(x) + r + O \mid r \in R\}\) 中每一项从哪来；并解释为什么 `apply()` 只计算其中一项。
3. **把布局挂到 buffer 上**：在 `pool.alloc(..., layout=...)` 与 `T.decl_buffer(..., layout=...)` 两处挂载点上附加布局，让 tile 操作直接消费布局信息，而不必重述「哪个元素在哪个 lane、哪个寄存器、哪块线性存储」。

本讲的实践**不需要 Blackwell GPU**——布局的构造与求值是宿主机上的整数映射计算。你只需要按 u1-l3 装好 `apache-tvm`；完全没有 tvm 环境时，本讲也提供了手推 + 交互演示核对的替代路径。

## 2. 前置知识

本讲站在两块基石上，先把这些结论用通俗语言复述一遍。

**Shape-Stride 模型（u4-l1）**。布局的本质是函数：输入逻辑索引，输出物理位置。最简单的形态是 `S[(shape):(strides)]`——物理位置等于索引与步长的点积。行主序、列主序只是两组不同的参数。本讲要把「物理位置」从一根线性地址轴推广到**命名轴**。

**命名轴（u4-l2）**。strides 上用 `@轴名` 标注每个维度落到哪根物理轴，例如 `4@laneid` 表示「该分量乘 4 后贡献到 laneid 这根轴」。同一个轴被多个 iter 引用时贡献**相加**。TMEM 的二维地址用 `@TLane`/`@TCol` 描述；寄存器 fragment 用 `@laneid` 加默认轴 `m`。检验布局的基本工具是**元素数守恒**：逻辑元素总数必须等于各物理轴取值数之积（双射）。

**复制与偏移（u4-l3）**。replication 用 `R[shape : strides]` 引入独立于逻辑索引的副本坐标，让同一个逻辑元素出现在多个物理位置（如 scale factor 广播到四个 32-lane 分区）；offset 只做固定平移、不产生副本。当时给出的集合语义 \(L(x)=\{D(x)+r+O \mid r\in R\}\) 正是本讲要落地成 API 的东西。

**三要素中的 layout（u9-l3）**。TIRx 里每项 tile 操作由 scope（谁执行）、layout（数据摆哪）、dispatch（走哪条路径）刻画；layout 对应编译器生成的**地址计算**。铁律是：读写两端对同一元素必须给出相同物理位置。

**hgemm_v1 的三个布局现场（u9-l1）**。单 tile GEMM 内核里已经出现过三处布局：SMEM 的 A/B 用 `mma_shared_layout`（128B swizzle）、TMEM 累加器用 `TileLayout(S[(128,512):(1@TLane,1@TCol)])`、回写视图用 `S[(128, BLK_N):(1@tid_in_wg, 1)]`。本讲第 4.4 节会回到这份真实源码。

**TMEM 结构（u2-l2、u7-l3）**。TMEM 是 128 Lane × 最多 512 Col、每格 32 bit 的二维片上存储；书中内核一律分配 512 列再按列切片。这解释了为什么 TMEM 布局用两根命名轴而不是一根线性地址。

如果对以上任何一条只剩模糊印象，建议先回看对应讲义；本讲的价值在于把「布局的数学」变成「布局的 API」。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注的部分 |
| --- | --- | --- |
| [chapter_tirx_layout_api/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md) | TIRx Layout API 章正文（英文），本讲的主源码 | L14-64 记号与导入、L96-188 三要素构造、L190-288 求值、L290-346 TMEM/scale 实例、L348-372 构造器 |
| [zh/chapter_tirx_layout_api/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_tirx_layout_api/index.md) | 上一文件的中文镜像，与英文版**逐行对齐**（两文件均为 542 行） | 同上；中文读者可对照阅读 |
| [static/tirx-layout-demo/index.html](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/static/tirx-layout-demo/index.html) | 交互式布局演示的页面骨架 | L130-147 三个一等输入（shape / dtype+swizzle / 布局表达式）、L150-162 逻辑与物理两个面板 |
| [static/tirx-layout-demo/layout-demo.js](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/static/tirx-layout-demo/layout-demo.js) | 演示的解析器与前向映射器，注释声明其逻辑镜像 `tvm/python/tvm/tirx/layout.py` | L54-101 文法与 `parseTerm`、L259-308 flatten/split/前向映射/replica 枚举、L856-872 预设清单 |
| [chapter_intro_tirx/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md) | `hgemm_v1` 内核完整源码，第 4.4 节的挂载现场 | L92-93 SMEM 布局、L109-131 `pool.alloc`/`decl_buffer`、L154-158 `view` 挂布局 |

演示文件放在 `static/` 目录，Sphinx 构建时由 `html_static_path = ["static"]` 拷入站点的 `_static/`（[conf.py:47](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/conf.py#L47)），所以正文 iframe 里引用的是 `../_static/tirx-layout-demo/index.html`（[chapter_tirx_layout_api/index.md:78](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L78)）。

## 4. 核心概念与源码讲解

先看本章开篇的一句话总结：`TileLayout` 用 `S[...]`、`R[...]` 和一个 offset 描述「逻辑 tile 如何摆放到命名轴上」——[chapter_tirx_layout_api/index.md:7-9](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L7-L9)。本章只做三件事：构造、附加、检视（[L12](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L12)）。本讲按「TileLayout 对象 → shard → replica+offset → 挂载」四个模块展开。

### 4.1 模块一：TileLayout 对象——从记号到 iter 三元组

#### 4.1.1 概念说明

`TileLayout` 是 TIRx 中**主仿射布局对象**（primary affine layout object）。「仿射」指它的求值只涉及「乘系数再相加」：逻辑坐标乘 stride、按轴累加、再加固定偏移——没有 XOR、没有查表。这个限制是刻意的：仿射布局足以描述寄存器 fragment、TMEM tile、scale factor 这些「乘加就能定位」的场景；至于 SMEM swizzle 那种非仿射的 XOR 地址置换，TIRx 用另一个对象 `ComposeLayout` 单独承载（下一讲 u10-l3 的主题）。

在纸上，u4 系列已经用 `S[(128, 256) : (1@TLane, 1@TCol)]` 这样的记号描述过一个位于 TMEM 的 tile。本讲第一个收获是：**同一条记号在 TIRx 程序里直接就是构造表达式**——把 `S[...]` 包进 `TileLayout(...)` 即可得到可挂载到 buffer 的对象。

一个容易被忽略的细节：`1@TLane` 里的 `@` 借用的是 Python 的**中缀矩阵乘运算符**。`S[(128, 256) : (1@TLane, 1@TCol)]` 是合法的 Python 表达式——`1 @ TLane` 在整数 `1` 与轴对象 `TLane` 之间求值，产出一个「stride 1，落 TLane 轴」的项。这就解释了为什么 `tvm.tirx.layout` 的导入清单里必须出现 `laneid`、`TLane`、`TCol` 这些名字：它们不是注释性的助记符，而是**参与表达式求值的真实对象**，名字不在作用域里，方括号里的式子根本无法求值。

#### 4.1.2 核心流程

从导入到构造的完整流程：

1. **导入**：从 `tvm.tirx.layout` 引入布局类（`TileLayout`、`ComposeLayout`）、记号（`S`、`R`）与命名轴（`laneid`、`warpid`、`tid_in_wg`、`TLane`、`TCol`、`m` 等），以及三个常用构造器（`tcgen05_atom_layout`、`tmem_datapath_layout`、`wg_local_layout`）。
2. **构造**：把 `S[shape : strides]`（可选拼接 `R[...]` 与 offset）传入 `TileLayout(...)`。
3. **内部表示**：API 内部把每个 iter 存成一个三元组 `(extent, stride, axis)`——`extent` 是该 iter 的取值个数，`stride` 是每走一步移动的距离，`axis` 标明沿哪根物理轴移动。
4. **附加**：把布局对象通过 `layout=` 参数挂到 buffer 上（第 4.4 节）。

用伪代码表示这条流水线（**示例伪代码**，帮助理解，非项目源码）：

```text
S[(shape) : (strides)]          # Python 求值 → 一串 (extent, stride, axis) 三元组
        │
TileLayout( S[...] + R[...] + offset )
        │
        ├─ shard  : [(extent, stride, axis), ...]   # 决定基础坐标
        ├─ replica: [(extent, stride, axis), ...]   # 决定额外副本
        └─ offset : {axis: 位移, ...}               # 决定整体平移
```

#### 4.1.3 源码精读

先看记号如何变成对象。章节正文给出从纸面记号到挂载的三行代码——[chapter_tirx_layout_api/index.md:20-28](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L20-L28)（中文版 [zh/chapter_tirx_layout_api/index.md:20-28](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_tirx_layout_api/index.md#L20-L28)）：这段代码先用 `S[(128, 256) : (1@TLane, 1@TCol)]` 构造 TMEM 布局，再分别示范 `pool.alloc(shape, dtype, layout=layout)` 与 `T.decl_buffer(shape, dtype, scope=scope, layout=layout)` 两个挂载点。紧接其后的一句是本模块的宗旨：buffer 从此「携带」自己的物理布局，tile 操作可以直接使用这份信息，而不必重述哪些 lane、哪些寄存器、哪些线性存储位置持有它的元素（[L30](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L30)）。

完整的导入清单在 [chapter_tirx_layout_api/index.md:32-50](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L32-L50)：

```python
from tvm.tirx.layout import (
    TileLayout,
    ComposeLayout,
    S,
    R,
    laneid,
    warpid,
    tid_in_wg,
    TLane,
    TCol,
    m,
    tcgen05_atom_layout,
    tmem_datapath_layout,
    wg_local_layout,
)
```

注意清单里 `S` 与 `R` 排在轴名之前——`S[...]` 是记号的入口（下标语法作用在 `S` 这个对象上），轴名则是方括号内 `@` 运算的右操作数。`hgemm_v1` 用的正是这份清单的一个子集：`from tvm.tirx.layout import TileLayout, S, TLane, TCol, tid_in_wg`（[chapter_intro_tirx/index.md:77](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L77)）——只导入用到的轴，因为源码检视解析要求每个出现在表达式里的名字都可解析（u1-l3）。

接下来是本模块的核心数据结构。`TileLayout` 的标准写法与 iter 三元组定义在 [chapter_tirx_layout_api/index.md:96-124](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L96-L124)：`TileLayout(S[shape : strides])` 中，`S[...]` 供给一串 iter extents 与 strides，把逻辑 tile 映射到命名轴上的**基础位置**；API 内部每个 iter 是 `(extent, stride, axis)` 三元组。对照 u4-l1 的 shape-stride 模型你会发现变化只有一处：**stride 不再贡献到单一线性地址，而是各自归属一根显式命名的轴**。

章节随即给出一个四 iter 的寄存器 fragment 例子（[L52-58](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L52-L58)）：

```python
frag = TileLayout(
    S[(8, 2, 4, 2) : (4@laneid, 1@warpid, 1@laneid, 1)]
)
```

两个观察点。其一，`laneid` 出现了**两次**——第 1、3 两个 iter 都贡献到它，最终 laneid 的值是两份贡献之和（u4-l2 已见过 `S[(8,4,2):(4@laneid,1@laneid,1@reg)]` 的同款手法）。其二，最后一个 stride 是裸的 `1`、没有 `@` 标签，因此使用默认轴 `m`（[L60](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L60)）。关于 `m` 轴要记住章节的两条说明：

- 当前 TIRx API **没有注册独立的 `reg` 轴**；布局挂在寄存器-backed 的 local buffer 上时，默认轴 `m` 表示该线程的本地线性位置，由 buffer scope 决定数据住在寄存器里（[L62](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L62)）。这正是 u4-l3 里「TIRx API 以默认轴 m 代替 @reg」这条结论的出处。
- 对 `m`、`TCol` 这类**存储轴**，stride 以 **buffer 元素**为单位：32-bit TMEM buffer 里沿 `TCol` 前进一个元素等于前进一个 32-bit 硬件列；8/16-bit buffer 则是若干相邻元素打包进一个硬件列（[L64](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L64)）。位宽换算的细节留给 u10-l2。

演示侧可以核对「裸整数默认落 m 轴」这条规则：`layout-demo.js` 的 `parseTerm` 在 token 是纯整数时直接返回 `{ stride: n, axis: 'm' }`（[layout-demo.js:98](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/static/tirx-layout-demo/layout-demo.js#L98)），而该解析器的文法注释明确声明「镜像 layout.py：`S[shape:stride] + R[shape:stride] + offset`，stride/offset 项形如 `n@axis`」（[layout-demo.js:54-56](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/static/tirx-layout-demo/layout-demo.js#L54-L56)）。

#### 4.1.4 代码实践

**实践目标**：亲手构造章节开篇的 TMEM 布局，确认「纸面记号 = 构造表达式」，并观察 `apply()` 返回的坐标字典形态。

**操作步骤**（以下为**示例代码**，非项目源码；保存为 `tilelayout_lab.py`，因为 TIRx 的布局表达式依赖源码检视解析，不能放进 `python -c`）：

```python
# tilelayout_lab.py —— 本讲实践的第 1 步
from tvm.tirx.layout import TileLayout, S, TLane, TCol

# 章节开篇的 TMEM 累加器布局：128 行 × 256 列
tmem_layout = TileLayout(S[(128, 256) : (1@TLane, 1@TCol)])

# 取几个逻辑坐标，打印基础物理坐标
for coord in [(0, 0), (5, 7), (127, 255)]:
    print(coord, "->", tmem_layout.apply(*coord, shape=[128, 256]))
```

运行：`python tilelayout_lab.py`（环境按 u1-l3 装 `apache-tvm==0.26.0` 与 `cuda-bindings`）。

**需要观察的现象**：输出的每一行应是一个「轴名 → 值」的字典（章节给出的返回形态形如 `{"laneid": 5, "warpid": 5, "m": 1}`，见 [chapter_tirx_layout_api/index.md:264-267](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L264-L267)）。

**预期结果**（手推：本布局的 shard extents 与逻辑 shape 同为 `(128, 256)`，拆分即是恒等，行贡献到 `TLane`、列贡献到 `TCol`）：

```text
(0, 0)   -> {'TLane': 0, 'TCol': 0}
(5, 7)   -> {'TLane': 5, 'TCol': 7}
(127, 255) -> {'TLane': 127, 'TCol': 255}
```

本机未运行，以上为手推期望值，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么导入清单里必须出现 `TLane`、`laneid` 这些轴名？去掉会怎样？

**答案**：`1@TLane` 中的 `@` 是 Python 的中缀运算符（矩阵乘），`TLane` 是参与求值的真实对象。名字不在作用域里，`S[...]` 方括号内的表达式会直接抛 `NameError`。这也是 u1-l3 强调「内核必须写在文件或 notebook 单元格里」的又一原因——源码检视解析要能拿到完整的名字解析环境。

**练习 2**：`S[(8, 2, 4, 2) : (4@laneid, 1@warpid, 1@laneid, 1)]` 中最后一个 `1` 没写 `@`，它落到哪根轴？布局挂在寄存器 local buffer 上时它表示什么？

**答案**：落到默认轴 `m`。挂在寄存器 local buffer 上时，`m` 表示该线程本地的线性槽位——当前 API 未注册独立的 `reg` 轴，用 `m` 兼任（[chapter_tirx_layout_api/index.md:60-62](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L60-L62)）。

**练习 3**：用元素数守恒检验 `frag` 布局的双射性。

**答案**：shard 各 extent 之积 \(8\times2\times4\times2=128\)，等于逻辑 tile \(8\times16=128\)。再验证各轴取值数：laneid \(=4e_0+e_2\)，\(e_0\in[0,8)\)、\(e_2\in[0,4)\)，恰好取遍 0–31 共 32 个值；warpid \(=e_1\in\{0,1\}\)；\(m=e_3\in\{0,1\}\)。\(32\times2\times2=128\)，守恒成立，映射是双射。

### 4.2 模块二：Shard——分片映射

#### 4.2.1 概念说明

shard（分片）由 `S[...]` 构造，回答的问题是：**逻辑 tile 的每个元素落在哪**。它把逻辑索引切分到一个或多个 iter 上，产生**基础物理坐标** \(D(x)\)。

它仍是 u4-l1 那条普通的 shape-and-stride 规则，唯一的变化是每个 stride 都归属一根显式命名的轴，而不是贡献到一根线性地址（[chapter_tirx_layout_api/index.md:128-130](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L128-L130)）。你可以把 shard 理解为「先拆索引、再按轴记账」：拆出来的每个分量乘自己的 stride，记到对应轴的账上；同一根轴收到多笔就相加。

为什么要允许多个 iter 落同一根轴（比如 `frag` 里两次 `@laneid`）？因为物理坐标本身是多维的、且一根轴可以被逻辑形状的多个维度共享。把「逻辑怎么切」（shape/extents）与「物理怎么摆」（strides/axes）解耦，正是 u4-l1「shape 与 strides 正交」结论在 API 里的延续。

#### 4.2.2 核心流程

shard 的求值分三步（这也是交互演示展示的基本求值过程，[chapter_tirx_layout_api/index.md:94](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L94)）：

1. **展平（flatten）**：把逻辑坐标 \(x=(x_0,\dots,x_{r-1})\) 在逻辑 shape \((S_0,\dots,S_{r-1})\) 下按行主序展成单个整数：

\[ \text{flat} = \sum_{i=0}^{r-1} x_i \prod_{j>i} S_j \]

2. **拆分（split）**：按 shard 各 iter 的 extent 把 flat 拆回 \(n\) 个分量 \((c_0, c_1, \dots, c_{n-1})\)——第 \(k\) 个分量的取值范围是 \([0, \text{extent}_k)\)。
3. **记账（contribute）**：第 \(k\) 个分量贡献 \(c_k \cdot s_k\) 到轴 \(a_k\)；同轴相加，最后加上固定 offset。

写成公式：对逻辑坐标 \(x\)，基础坐标 \(D(x)\) 在每根轴 \(a\) 上的取值为

\[ D(x)_a \;=\; \sum_{k\,:\,a_k=a} c_k(x)\, s_k \]

**注意**：逻辑 shape 的秩不必等于 shard extents 的个数——中间隔着 flatten/split 两次坐标变换，只要 flat 索引落在 shard 表示的逻辑范围内即可。`apply()` 的三种输入形式（线性坐标 / shard 坐标 / 带 shape 的逻辑坐标）只是跳过其中不同步骤，详细推导是 u10-l2 的主题；本讲只需用第三种形式来做「构造对不对」的抽查。

#### 4.2.3 源码精读

求值三步的正式定义在 [chapter_tirx_layout_api/index.md:216-258](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L216-L258)：先按行主序展平逻辑坐标，再按 shard extents 拆分得到 \((c_0,\dots,c_{n-1})\)，若 shard iter \(k\) 的 stride 是 \(s_k\)、轴是 \(a_k\)，则分量 \(c_k\) 贡献 \(c_k \cdot s_k @ a_k\)；「对同一轴的贡献相加，再加上固定 offset，得到的坐标字典就是 `apply()` 的返回值」（[L250-256](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L250-L256)）。

章节最珍贵的是这个**带完整数值的算例**（[L262-278](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L262-L278)）。对完整三件套布局（shard + `R[2:4@warpid]` + `5@warpid`，下一模块展开），在 `(8, 16)` 输入 tile 上取逻辑坐标 `(1, 3)`：

```python
layout.apply(1, 3, shape=[8, 16])

# {"laneid": 5, "warpid": 5, "m": 1}
```

三步演算：

1. `(1, 3)` 在行主序 `(8, 16)` 下展平：flat \(= 1\times16+3=19\)。
2. 按 shard extents `(8, 2, 4, 2)` 拆分：\(19 = 1\times16+0\times8+1\times4+1\times2+1\)，得 \((c_0,c_1,c_2,c_3)=(1,0,1,1)\)。
3. 记账：laneid \(=1\times4+1\times1=5\)，warpid \(=0\times1=0\)，m \(=1\)；再加 offset `5@warpid` 得 warpid \(=5\)。

章节还给出了整个 `(8, 16)` tile 上的闭式映射（[L280-288](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L280-L288)）：

```text
laneid = 4 * i + (floor(j / 2) mod 4)
warpid = floor(j / 8) + 5
m      = j mod 2
```

即 shard 与 offset 把 tile 摆到 warp 5、6 上，replica 再在 warp 9、10 上加一份副本。

再补一个 **TMEM 的非 2 次幂例子**（[L290-315](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L290-L315)）：

```python
layout = TileLayout(
    S[(2, 128, 112) : (112@TCol, 1@TLane, 1@TCol)]
)
```

逻辑 shape 与 shard extents 同为 `(2, 128, 112)`，三个拆分分量就是逻辑坐标本身，对元素 \((a, l, c)\)：

\[ \text{TLane} = l, \qquad \text{TCol} = 112a + c \]

extent 为 128 的 iter 以 `1@TLane` 填满全部 128 条 TMEM Lane 行，另外两个 iter 合计覆盖 224 个 TCol 位置。**TMEM 布局的维度不必是 2 的幂**——列 iter 可以直接用 112，两个这样的区域恰好覆盖 224 列而不必把 extent 凑到 128；真实内核会刻意这样选形，例如 block-scaled FP8 GEMM 可以为两个累加器 stage 加 scale factor 预留 TMEM，而不是让单个累加器 tile 独占 256 列（[L315](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L315)）。这条结论呼应 u7-l3 的分配策略：起步即分配 512 列，再用布局的列偏移在里面切片。

最后验证演示与正文的求值逻辑一致：`layout-demo.js` 的 `flattenCoord`/`splitCoord`/`forwardBase` 三函数（[layout-demo.js:259-290](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/static/tirx-layout-demo/layout-demo.js#L259-L290)）依次实现「行主序展平 → 按 extent 从低位拆 → 各分量乘 stride 记到 `phys[it.axis]`」，文件头注释声明这套逻辑镜像 `tvm/python/tvm/tirx/layout.py` 的 `_flatten_coord` / `_split_coord` 与前向映射（[layout-demo.js:24-27](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/static/tirx-layout-demo/layout-demo.js#L24-L27)）。所以演示里点击元素看到的算式，与真实 `apply()` 做的是同一套整数运算。

#### 4.2.4 代码实践

**实践目标**：把 4.2.3 的手推算例变成可执行验证——用 `apply()` 复算章节的 `(1, 3)` 例子，再独立推一个新坐标并核对。

**操作步骤**（**示例代码**，追加到 `tilelayout_lab.py`）：

```python
# 第 2 步：shard 求值抽查
from tvm.tirx.layout import R, laneid, warpid, m

layout = TileLayout(
    S[(8, 2, 4, 2) : (4@laneid, 1@warpid, 1@laneid, 1)]
    + R[2 : 4@warpid]
    + 5@warpid
)

# (a) 章节算例：逻辑 (1,3) 在 shape (8,16) 下
print(layout.apply(1, 3, shape=[8, 16]))

# (b) 自选坐标：逻辑 (3, 5) 在 shape (8,16) 下
print(layout.apply(3, 5, shape=[8, 16]))
```

**需要观察的现象**：(a) 的输出应与章节给出的注释完全一致；(b) 先自己手推再运行对照。

**预期结果**：(a) `{"laneid": 5, "warpid": 5, "m": 1}`——这是章节原文写出的返回值（[chapter_tirx_layout_api/index.md:264-267](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L264-L267)）。(b) 手推：flat \(=3\times16+5=53\)，拆分 \(53=6\times8+1\times4+1\times2+1\) 得 \((6,1,1,1)\)，laneid \(=24+1=25\)、warpid \(=1+5=6\)、m \(=1\)，期望 `{"laneid": 25, "warpid": 6, "m": 1}`。**待本地验证**。

**无 tvm 环境的替代路径**：打开交互演示（本地构建站点 `_build/html/_static/tirx-layout-demo/index.html`，或直接用浏览器打开 `static/tirx-layout-demo/index.html`；也可用深链 `.../index.html?preset=3` 直接载入本例，见 [layout-demo.js:926-939](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/static/tirx-layout-demo/layout-demo.js#L926-L939)），选预设「Tensor-core tile (doc example)」（shape `8, 16`，正是本布局），点击编号 19 的格子（逻辑坐标 (1,3)）。底部公式栏会依次显示：元素 19 的逻辑坐标、shard 拆分 `(1, 0, 1, 1)`、各项 `1·4@laneid + 0·1@warpid + 1·1@laneid + 1·1@m`、offset、基础位置，以及 replica 展开出的两个物理位置（[layout-demo.js:695-741](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/static/tirx-layout-demo/layout-demo.js#L695-L741) 的 `drawFormula`）。再点元素 53 核对 (b)。

#### 4.2.5 小练习与答案

**练习 1**：手推 TMEM 布局 `S[(2, 128, 112) : (112@TCol, 1@TLane, 1@TCol)]` 中元素 `(1, 64, 50)` 的物理坐标。

**答案**：shape 与 extents 相同，分量即坐标本身：TLane \(=64\)，TCol \(=112\times1+50=162\)。

**练习 2**：为什么 `(8, 16)` 的逻辑 tile 可以用 `(8, 2, 4, 2)` 四个 shard iter 描述？中间发生了什么？

**答案**：求值先按逻辑 shape 行主序展平成 flat（\(\in[0,128)\)），再按 shard extents 拆分。两次坐标变换衔接了「逻辑形状的秩」与「iter 个数」，二者不必相等，只需 flat 索引范围一致（元素数守恒）。`frag` 布局正是借这步把 2 维逻辑 tile 切成「行 × warp 间列组 × lane 间列 × 槽位」四段。

**练习 3**：把演示里的 shape 改成 `8, 8` 而布局表达式不动，会发生什么？

**答案**：shard 元素总数 \(8\times2\times4\times2=128\neq 8\times8=64\)，双射破坏；演示在状态栏给出警告「⚠ shard total 128 ≠ shape total 64 —— 映射可能不合法」（[layout-demo.js:548-552](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/static/tirx-layout-demo/layout-demo.js#L548-L552)）。这 就是元素数守恒检验的可视化版本。

### 4.3 模块三：Replica 与 Offset——副本与平移

#### 4.3.1 概念说明

shard 只能描述**一一对应**的布局：一个逻辑元素一个物理位置。硬件里大量场景打破这个限制——block-scaled MMA 的 scale factor 必须同时出现在四个 32-lane 分区，让每个 warp 的 TMEM 窗口都读得到（u5-l3、u7-l2）。这就需要两类扩展：

- **Replica（复制）**，由 `R[shape : strides]` 构造：描述同一逻辑元素的**额外物理副本**。关键性质是 replica iter **不依赖逻辑索引**——无论问的是哪个元素，副本都按同一组偏移枚举。它只「登记副本在哪」，副本如何产生或使用由消费该布局的 tile 操作决定（如 `tcgen05.cp` 的 `.warpx4` 组播，u5-l3）。
- **Offset（偏移）**，记作 `O`：加到**每个**映射坐标上的固定平移。它不产生副本，用途是选中 tile 的起始坐标，或把多个 tile 摆进同一硬件资源的不同区域。

三者组合的语义是集合级的。对逻辑坐标 \(x\)，shard 产生基础坐标 \(D(x)\)，完整布局描述的物理位置集合为（[chapter_tirx_layout_api/index.md:160-168](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L160-L168)）：

\[ L(x) \;=\; \{\, D(x) + r + O \;\mid\; r \in R \,\} \]

没有 replica 时把 \(R\) 看作只含零偏移，集合退化为单点；有 replication 时每个副本一点。**当前 `layout.apply()` 只计算基础坐标 \(D(x)+O\)，不枚举 \(R\)**；replica iters 保存在 `layout.replica` 里，由使用该布局的 tile 操作处理——这条设计决策在章首概览（[L8](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L8)）与 Forward Mapping 一节（[L260](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L260)）各强调了一次。

#### 4.3.2 核心流程

完整构造形式与读法（[chapter_tirx_layout_api/index.md:170-180](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L170-L180)）：

```python
layout = TileLayout(
    S[(8, 2, 4, 2) : (4@laneid, 1@warpid, 1@laneid, 1)]
    + R[2 : 4@warpid]
    + 5@warpid
)
```

从左到右读：`S[...]` 放置逻辑 tile；`R[2 : 4@warpid]` 在相隔 4 个 warp 处加第二份副本；`5@warpid` 把所有位置整体平移 5。若三件对象已单独构造好，也可用 `TileLayout.from_iters(shard, replica, offset)` 组装（[L182-188](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L182-L188)）——内核代码通常直接写 `S[...]`/`R[...]` 记号，因为形状、strides 与轴一目了然。

replica 的枚举规则：`R[e : s@axis]` 产生 \(e\) 个偏移 \(0, s, 2s, \dots, (e-1)s\)，全部落在 `axis` 轴上。多个 `R` 项（或多项式 R）做笛卡尔积式的展开。`apply()` 与完整枚举的分工：

```text
apply(x)          →  D(x) + O                 # 单点，程序里算地址用
L(x)（完整语义）  →  { D(x) + r + O | r ∈ R } # 全体副本，tile 操作负责处理
```

演示侧则**枚举**副本：`physOwners` 从基础坐标出发，对每个 replica iter 把已有位置 × extent 逐一复制并加 \(k\cdot\text{stride}\)（[layout-demo.js:292-308](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/static/tirx-layout-demo/layout-demo.js#L292-L308)），函数头注释正是集合公式 `L(x) = { D(x) + r + O | r in R }`。这就是「点一个格子、箭头指向多个物理位置」的实现——演示替你做了真实 `apply()` 故意不做的那步。

#### 4.3.3 源码精读

Replica 与 Offset 两节的定义（[chapter_tirx_layout_api/index.md:132-158](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L132-L158)）：`R[2 : 4@warpid]` 沿 warpid 轴放两份副本、相隔 4 个 warp；「replica 把一个逻辑元素描述成拥有若干物理坐标，它记录副本归属何处；消费该布局的 tile 操作决定这些副本如何被生产或使用」（[L144](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L144)）。offset 则「加到每个映射坐标上」，`5@warpid` 把整个布局沿 warpid 平移 5，「可以选中 tile 的起始坐标，或把若干 tile 摆到同一硬件资源的不同区域」（[L146-158](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L146-L158)）。

最重要的应用现场是 **scale-factor 布局**（[L317-346](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L317-L346)）。累加器布局是一对一的，而 block-scaled MMA 要求同一组 scale factor 对多个 warp 窗口可见，于是用 replication。反复出现的 `32xsf_per_mma` 原子：

```python
scale = TileLayout(
    S[(32, sf_per_mma) : (1@TLane, 1@TCol)]
    + R[4 : 32@TLane]
)
```

对逻辑 scale 坐标 \((r, s)\)，shard 先产生 TLane \(=r\)、TCol \(=s\)；replica 再以 32 为步长在 TLane 上复制四份：

\[ \text{TLane} = r + 32q,\quad q\in\{0,1,2,3\}, \qquad \text{TCol} = s \]

于是这个 32 行的组同时出现在 lane 0–31、32–63、64–95、96–127 四个分区，每个 warp 的 32-lane TMEM 窗口都能访问同一份 scale factor（[L337-344](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L337-L344)）。注意这与 u4-l3 推过的 `.warpx4` 复制是同一件事的 API 视角。另外此处若 buffer 是 8-bit scale factor，`TCol` 仍以 buffer 元素计：相邻 4 个元素打包进一个 32-bit 硬件列，硬件列号与字节位置分别是 \(s//4\) 与 \(s\%4\)（[L335](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L335)）——位宽换算留待 u10-l2 展开。

收尾一句值得记住：**累加器与 scale factor 用的是同一个 `TileLayout` 模型**——累加器布局通常把每个元素映射到单个 TMEM 坐标，scale-factor 布局只是在同一 `TLane`/`TCol` 空间里加了 replication（[L346](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L346)）。一套记号覆盖两类硬件现场，这正是三件套设计的价值。

回到 4.2 的 `frag` 完整例子核对 replica 的效果：`apply(1,3)` 只返回 `warpid=5` 一点，而 `R[2:4@warpid]` 告诉 tile 操作还要处理 `warpid=5` 与 `warpid=9` 两处（[L278](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L278)）；全 tile 视角则是「shard+offset 摆到 warp 5、6，replica 在 warp 9、10 加副本」（[L288](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L288)）。在演示里选「Tensor-core tile (doc example)」预设，物理面板的行轴正是 warpid，你会直接看到 5、6、9、10 四行——后两行就是副本。

#### 4.3.4 代码实践

**实践目标**：验证「`apply()` 只算基础坐标、replica 留在 `layout.replica`」这条关键设计。

**操作步骤**（**示例代码**，追加到 `tilelayout_lab.py`）：

```python
# 第 3 步：replica 与 offset 的分工
sf_per_mma = 4
scale = TileLayout(
    S[(32, sf_per_mma) : (1@TLane, 1@TCol)]
    + R[4 : 32@TLane]
)

# (a) apply() 只返回基础坐标
for coord in [(0, 0), (5, 3), (31, 0)]:
    print(coord, "->", scale.apply(*coord, shape=[32, sf_per_mma]))

# (b) 手动枚举 replica：L(x) = { D(x) + r | r in R }
for coord in [(5, 3)]:
    r_, s_ = coord
    copies = [{"TLane": r_ + 32 * q, "TCol": s_} for q in range(4)]
    print("all copies of", coord, "->", copies)

# (c) 观察 replica 信息存放处
print("replica iters:", scale.replica)
```

**需要观察的现象**：(a) 每行只返回**一个**字典、TLane 值在 0–31 之间（没有 \(+32/+64/+96\) 的副本）；(b) 是你自己补齐的四份副本；(c) 打印 `scale.replica` 的内容。

**预期结果**：(a) `(0,0)→{'TLane':0,'TCol':0}`、`(5,3)→{'TLane':5,'TCol':3}`、`(31,0)→{'TLane':31,'TCol':0}`（手推，**待本地验证**）；(b) `[{'TLane': 5, 'TCol': 3}, {'TLane': 37, 'TCol': 3}, {'TLane': 69, 'TCol': 3}, {'TLane': 101, 'TCol': 3}]`；(c) 应能看到 replica 的 iter 信息（extent 4、stride 32、轴 TLane）——具体打印格式依 tvm 版本而定，**待本地验证**。

**演示核对**：演示选预设「Blackwell tensor memory (TLane/TCol)」（shape `4, 8`，表达式 `S[(2,4,4):(4@TCol,1@TLane,1@TCol)]`，[layout-demo.js:867](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/static/tirx-layout-demo/layout-demo.js#L867)）。注意演示有 1024 个元素的渲染上限（[layout-demo.js:49](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/static/tirx-layout-demo/layout-demo.js#L49)），\(128\times256=32768\) 的原尺寸画不出来，所以预设用缩小版 shape、映射语义与正文示例一致（[layout-demo.js:853-855](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/static/tirx-layout-demo/layout-demo.js#L853-L855) 的注释）。点击元素 11（逻辑 (1,3)）：手推 flat \(=11=0\times16+2\times4+3\)，TLane \(=2\)、TCol \(=0\times4+3=3\)；点击元素 31（逻辑 (3,7)）：\(31=1\times16+3\times4+3\)，TLane \(=3\)、TCol \(=7\)。核对公式栏与物理面板的高亮位置是否一致。

#### 4.3.5 小练习与答案

**练习 1**：`R[2 : 4@warpid]` 与 offset `4@warpid` 都涉及「4」和「warpid」，二者区别是什么？

**答案**：`R[2:4@warpid]` 枚举两个副本偏移 \(\{0, 4\}\)，**每个逻辑元素多一份物理位置**；offset `4@warpid` 只把所有坐标整体平移 4，**不增加份数**。前者是集合变大，后者是集合搬家。

**练习 2**：`scale.apply(5, 3)` 会返回几份坐标？另外三份去哪了？

**答案**：一份，`{'TLane': 5, 'TCol': 3}`。当前 `apply()` 只计算基础坐标 \(D(x)+O\)；replica iters 保存在 `layout.replica` 中，由消费该布局的 tile 操作处理（如 `tcgen05.cp` 的 `.warpx4` 组播一次性产出四份副本）。

**练习 3**：把 `scale` 布局的 `R[4 : 32@TLane]` 改成 `R[2 : 64@TLane]`，副本落在哪些 lane？这样的布局还能满足「每个 warp 窗口都能读到 scale factor」吗？

**答案**：副本偏移为 \(\{0, 64\}\)，32 行组只出现在 lane 0–31 与 64–95 两个分区；lane 32–63、96–127 两个 warp 窗口读不到，不满足要求。四 warp 窗口各取一段 32-lane，需要步长 32 的 4 份副本。

### 4.4 模块四：把布局挂到 buffer 上

#### 4.4.1 概念说明

构造出的布局对象只有**附加到 buffer** 才进入内核的生命周期。TIRx 提供两个挂载点：

- **`pool.alloc(shape, dtype, layout=...)`**：在 `T.SMEMPool()` 的共享内存池里分配 buffer 并声明其物理布局；
- **`T.decl_buffer(shape, dtype, scope=..., layout=...)`**：为一个（往往由硬件另行分配的）存储区域声明 buffer 视图并附上布局——TMEM 就走这条路，因为它的基地址来自 `tcgen05.alloc` 写回的地址槽（u7-l3）。

挂载之后，「哪个元素在哪」不再散落在各条 tile 操作的参数里，而是**随 buffer 携带**：任何消费这个 buffer 的 tile 操作直接读它的布局。这直接服务于 u9-l3 的铁律——读写两端对同一元素给出相同位置；布局挂在 buffer 上，就是让两端引用**同一份**位置描述。

还有一个工程上的便利：内核很少手写每个硬件布局。TIRx 为反复出现的模式提供构造器——`tmem_datapath_layout(datapath, rows, cols)` 返回 `tcgen05.mma` 写出的 TMEM 累加器布局（`datapath="D"` 是 M=128 的直映射，`"F"` 是 M=64 的散布映射）；`tcgen05_atom_layout(instr_shape, tensor_shape, dtype)` 返回与 `tcgen05.ld/st` 搬运形状对应的寄存器 tile 布局；`wg_local_layout(cols, rows=128)` 返回 warpgroup 本地寄存器 tile。**三者都返回由同样的 iters 与命名轴构成的普通 `TileLayout` 对象**，只是常见硬件映射的便捷包装（[chapter_tirx_layout_api/index.md:348-372](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L348-L372)）。看懂了本讲的 S/R/offset，就看懂了它们的内部。

#### 4.4.2 核心流程

布局进入内核的完整链路：

1. **准备**：导入记号与轴名；对 SMEM 布局可直接用 `mma_shared_layout` 这类辅助函数生成（内部是 ComposeLayout 包装的 swizzle 布局，u10-l3 展开）。
2. **SMEM 挂载**：`pool = T.SMEMPool()` → `pool.alloc(shape, dtype, layout=...)` → `pool.commit()`。
3. **TMEM 挂载**：warp 0 执行 `tcgen05.alloc` 把基地址写入 SMEM 槽 → `T.decl_buffer(shape, dtype, scope="tmem", allocated_addr=..., layout=TileLayout(...))`。
4. **寄存器视图挂载**：对 `T.alloc_local` 出的寄存器数组用 `.view(shape, layout=...)` 附加布局。
5. **消费**：tile 操作（`Tx.cta.copy`、`Tx.gemm_async`、`Tx.wg.copy_async`）按 buffer 携带的布局生成地址计算。

#### 4.4.3 源码精读

现在回到 `hgemm_v1` 的真实源码，看三个挂载点。第一处，SMEM 池分配 A/B 时挂 swizzle 布局（[chapter_intro_tirx/index.md:109-116](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L109-L116)）：

```python
pool = T.SMEMPool()
tmem_addr = pool.alloc((1,), "uint32")
mma_bar = pool.alloc((1,), "uint64", align=8)
pool.move_base_to(1024)
Asmem = pool.alloc((BLK_M, BLK_K), a_type, layout=A_layout)
Bsmem = pool.alloc((BLK_N, BLK_K), b_type, layout=B_layout)
pool.commit()
```

`A_layout`、`B_layout` 来自 `mma_shared_layout(a_type, SwizzleMode.SWIZZLE_128B_ATOM, (BLK_M, BLK_K))`（[L92-93](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L92-L93)）——注意池里还混着**不带布局**的分配（`tmem_addr`、`mma_bar`）：布局是 buffer 的可选属性，同步原语之类的「裸字节」不需要它。`move_base_to(1024)` 把数据区挪到 1KB 边界之后，为的是满足 128B swizzle atom 的对齐要求。

第二处，TMEM 累加器的 `decl_buffer` 挂载（[chapter_intro_tirx/index.md:128-131](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L128-L131)）：

```python
tmem = T.decl_buffer(
    (128, 512), "float32", scope="tmem", allocated_addr=tmem_addr[0],
    layout=TileLayout(S[(128, 512) : (1@TLane, 1@TCol)])
)
```

这正是本讲 4.1.4 构造的那类 TMEM 布局（只是列数 512）：shape `(128, 512)`、scope `"tmem"`、基地址取自 `tcgen05.alloc` 写回的槽，布局把行映射到 `TLane`、列映射到 `TCol`。后续 `Tx.gemm_async(tmem[:, :BLK_N], ...)` 只写前 128 列——512 列的声明对应 u7-l3 的分配纪律（一次到位最大需求，再按列切片），切片区间直接用在了 tile 操作的索引里。

第三处，寄存器回写视图（[chapter_intro_tirx/index.md:154-158](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L154-L158)）：

```python
Dreg = T.alloc_local((BLK_N,), acc_type)
Dreg_f16 = T.alloc_local((BLK_N,), d_type)
Dreg_wg = Dreg.view(128, BLK_N,
                    layout=TileLayout(S[(128, BLK_N) : (1@tid_in_wg, 1)]))
Tx.wg.copy_async(Dreg_wg[:, :], tmem[:, :BLK_N])
```

`T.alloc_local` 分配的原始寄存器数组没有形状语义，`.view(128, BLK_N, layout=...)` 把 warpgroup 的 128 个线程各自的一段本地槽位**重看成** `(128, BLK_N)` 的 tile：行落到 `tid_in_wg`（每线程一行）、行内元素落到默认轴 `m`（线程本地槽位）。下一条 `Tx.wg.copy_async` 就在这两个「携带布局的 buffer」之间搬运，lowering 据此选出 warp 集体的 `tcgen05.ld`（u7-l4）。

章节对这三处的总结一锤定音：「**layout 决定 tile 如何映射到物理位置**。`A_layout`/`B_layout` 用 128B swizzle 把 A、B 放进 SMEM；`tmem` 的 `TileLayout` 把累加器映到 `TLane`/`TCol`；`Dreg_wg` 视图用 `tid_in_wg` 给每个线程分一行结果。要使 MMA 或拷贝正确工作，每个生产或消费该 tile 的操作都必须认同每个逻辑元素的物理位置。」（[chapter_intro_tirx/index.md:224](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L224)）

#### 4.4.4 代码实践

**实践目标**：源码阅读型实践——在 `hgemm_v1` 里定位全部布局挂载点，并为每处填写「挂载 API / 存储空间 / 用到的命名轴 / 消费它的 tile 操作」四列。

**操作步骤**：

1. 打开 [chapter_intro_tirx/index.md:84-170](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L84-L170)（`hgemm_v1` 完整源码）。
2. 找出所有出现 `layout=` 的行，按出现顺序抄下它们的调用形式。
3. 对每处回答：这个 buffer 住在哪个存储空间（SMEM/TMEM/寄存器）？布局用了哪些轴？下游哪条 tile 操作消费它？
4. 追加一问：`tmem` 的 `decl_buffer` 为什么声明 512 列，而 `Tx.gemm_async` 只用 `tmem[:, :BLK_N]`？

**需要观察的现象／预期结果**：应得到一张三行的表（答案要点）：

| 挂载点 | API | 空间 | 命名轴 | 消费者 |
| --- | --- | --- | --- | --- |
| `Asmem`/`Bsmem` | `pool.alloc(..., layout=A_layout)` | SMEM | `m`（128B swizzle，经 ComposeLayout） | `Tx.cta.copy` 写入、`Tx.gemm_async` 读取 |
| `tmem` | `T.decl_buffer(..., layout=TileLayout(...))` | TMEM | `TLane`、`TCol` | `Tx.gemm_async` 写入、`Tx.wg.copy_async` 读取 |
| `Dreg_wg` | `Dreg.view(..., layout=TileLayout(...))` | 寄存器 | `tid_in_wg`、`m` | `Tx.wg.copy_async` 写入、`Tx.cast`/`Tx.copy` 读取 |

第 4 问答案：TMEM 分配只允许 32/64/128/256/512 五档列数且同 CTA 多次分配须单调不增，故起步即声明 512 列（u7-l3 的分配纪律）；布局覆盖全部 512 列，MMA 通过列切片 `[:, :BLK_N]` 只用前 128 列。若手边有 Blackwell GPU，可按 u9-l2 的回路编译运行验证内核行为不变；无 GPU 时本实践为纯源码阅读，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `tmem_addr` 与 `mma_bar` 的 `pool.alloc` 不带 `layout=` 参数？

**答案**：它们是「裸字节」——一个存 TMEM 基地址的 uint32 槽、一个 mbarrier 对象，不承载需要按命名轴定位元素的 tile 数据。布局是 tile 数据 buffer 的属性，不是所有 SMEM 分配的必备项。

**练习 2**：`Dreg_wg` 的布局 `S[(128, BLK_N) : (1@tid_in_wg, 1)]` 中，未标记的 `1` 落在哪根轴？它在此处表示什么？

**答案**：默认轴 `m`。挂在寄存器 local buffer 的视图上时，`m` 表示线程本地的线性槽位——128 个线程每人一行、行内 `BLK_N` 个元素依次排在本线程的连续寄存器里。

**练习 3**：`tmem_datapath_layout`、`tcgen05_atom_layout`、`wg_local_layout` 三个构造器与本讲手写的 `TileLayout(S[...])` 是什么关系？

**答案**：它们只是便捷包装，返回由同样的 iters 与命名轴构成的普通 `TileLayout` 对象（[chapter_tirx_layout_api/index.md:372](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L372)）。会用 `S[...]` 就能读懂（必要时也能改写）它们的产物。

## 5. 综合实践

把本讲四个模块串成一个可运行的小实验：**构造三个布局 → `apply()` 抽查 → 手动枚举 replica → 用交互演示逐一核对**，最终产出一份「布局速查表」。

**任务**：完善并运行下面的脚本（**示例代码**，汇总前面三步即为完整版 `tilelayout_lab.py`）：

```python
# tilelayout_lab.py —— u10-l1 综合实践
from tvm.tirx.layout import (
    TileLayout, S, R, laneid, warpid, TLane, TCol, m,
)

# ① TMEM 累加器布局（章节开篇的 128x256 例子原样构造）
tmem_layout = TileLayout(S[(128, 256) : (1@TLane, 1@TCol)])

# ② 带 replication 的 scale-factor 原子（章节 32xsf_per_mma 例子）
sf_per_mma = 4
scale = TileLayout(
    S[(32, sf_per_mma) : (1@TLane, 1@TCol)] + R[4 : 32@TLane]
)

# ③ 三件套齐发的 fragment（章节完整形式）
frag = TileLayout(
    S[(8, 2, 4, 2) : (4@laneid, 1@warpid, 1@laneid, 1)]
    + R[2 : 4@warpid] + 5@warpid
)

print("== ① TMEM ==")
for c in [(0, 0), (5, 7), (127, 255)]:
    print(c, "->", tmem_layout.apply(*c, shape=[128, 256]))

print("== ② scale：apply 只给基础坐标，replica 自己枚举 ==")
for c in [(0, 0), (5, 3), (31, 0)]:
    print(c, "base ->", scale.apply(*c, shape=[32, sf_per_mma]))
r_, s_ = 5, 3
print("copies:", [{"TLane": r_ + 32 * q, "TCol": s_} for q in range(4)])
print("layout.replica:", scale.replica)

print("== ③ frag ==")
print((1, 3), "->", frag.apply(1, 3, shape=[8, 16]))
print((3, 5), "->", frag.apply(3, 5, shape=[8, 16]))
```

**验收清单**（每项都要在笔记里落到一行）：

1. ① 的输出是否与手推一致（`TLane`=行、`TCol`=列）？
2. ② 的 `apply` 是否只返回一点？手动枚举是否得到 lane 5/37/69/101 四份副本？
3. ③ 的 `(1,3)` 是否与章节原文一致（`{"laneid": 5, "warpid": 5, "m": 1}`）？
4. 打开演示（本地构建站点或 `static/tirx-layout-demo/index.html`，支持 `?preset=3` 深链）：
   - 预设「Tensor-core tile (doc example)」点元素 19：公式栏的拆分 `(1,0,1,1)`、基础位置、两个 owner（warpid 5 与 9）是否与脚本一致；物理面板 warp 行是否为 5、6、9、10。
   - 预设「Blackwell tensor memory (TLane/TCol)」点元素 11 与 31：TLane/TCol 是否为 (2,3) 与 (3,7)。
   - 选「Shard + replica」预设观察不带 offset 的最简副本形态，再手改表达式加 `+ 1@warpid` 看整体平移效果。
5. 写下你的「布局速查表」：每个布局一行，记录 `S`/`R`/`offset` 三列与适用硬件现场（TMEM 累加器 / scale factor / 寄存器 fragment）。

运行结果在本机**待本地验证**；若没有 tvm 环境，第 1–3 步改为手推（本讲已给出全部推导），第 4 步照做——演示不依赖任何安装。

## 6. 本讲小结

- **`TileLayout` 是 TIRx 的主仿射布局对象**，标准形式 `TileLayout(S[shape : strides] + R[replica_shape : replica_strides] + offset)`；API 内部把每个 iter 存为 `(extent, stride, axis)` 三元组，`S[...]` 里的 `@` 是 Python 中缀运算符，轴名是必须导入的真实对象。
- **shard 产生基础坐标 \(D(x)\)**：求值三步「行主序展平 → 按 extents 拆分 → 分量乘 stride 按轴记账、同轴相加」；逻辑 shape 的秩与 iter 个数解耦，约束是元素数守恒（双射）。
- **replica 登记副本、offset 整体平移**：完整语义是集合 \(L(x)=\{D(x)+r+O \mid r\in R\}\)；`apply()` 只算基础坐标 \(D(x)+O\)，replica iters 留在 `layout.replica` 由消费该布局的 tile 操作处理（如 `.warpx4` 组播）。
- **布局挂载三现场**：`pool.alloc(..., layout=...)`（SMEM）、`T.decl_buffer(..., layout=...)`（TMEM）、`.view(..., layout=...)`（寄存器视图）；挂载后 buffer 携带物理布局，tile 操作直接消费，保证读写两端认同同一份位置描述。
- **一套记号覆盖多类硬件现场**：TMEM 累加器是一对一映射，scale factor 在同一 `TLane`/`TCol` 空间加 replication；`tmem_datapath_layout` 等构造器只是普通 `TileLayout` 的便捷包装。
- **交互演示镜像真实实现**：`layout-demo.js` 的解析器与 flatten/split/前向映射逻辑镜像 `tvm/python/tvm/tirx/layout.py`，且替你枚举 replica——它是核对构造正确性的免费测试机。

## 7. 下一步学习建议

下一讲 **u10-l2「命名轴与前向映射」**深挖本讲只抽查使用的求值机制：`apply()` 三种输入形式的完整推导、命名轴体系（`bx`/`tx`/`warpid`/`laneid`/`tid_in_wg`/`m`/`TLane`/`TCol` 全表）、默认轴 `m` 的含义随 buffer scope 变化的规则，以及 8/16-bit 元素到 32-bit 硬件列的位宽换算——本讲 4.3.3 里 scale factor 的 \(s//4\)、\(s\%4\) 将在那里展开成完整方法。再往后 **u10-l3「ComposeLayout 与 swizzle 变换」**处理仿射布局覆盖不了的 XOR 地址置换，解释 `hgemm_v1` 里 `A_layout` 的真身。源码阅读方面，建议回头重读 [chapter_tirx_layout_api/index.md:190-288](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L190-L288)（命名轴表与前向映射）并对照 `layout-demo.js` 的同名函数，体会「文档—演示—tvm 实现」三者的同构；等进入 FA4（单元十四）时，你会看到 scale-factor 布局在真实内核里的完整形式（带 M/K 外层 iter 的版本）。
