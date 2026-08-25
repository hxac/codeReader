# 命名轴与前向映射

## 1. 本讲目标

上一讲（u10-l1）我们拆开了 `TileLayout` 的构造：`S[...]` 放分片、`R[...]` 加副本、offset 做平移，并知道 `apply()` 只计算基础坐标。本讲把放大镜对准两件事：**坐标里的"轴"到底有哪些名字、各自指什么硬件位置**，以及 **`apply()` 这台"逻辑坐标 → 物理坐标"的机器内部如何运转**。学完本讲你应该能够：

1. 说出 TIRx 布局中常用命名轴（`laneid`、`warpid`、`tid_in_wg`、`TLane`、`TCol`、`m` 等）各自的含义，并特别说明默认轴 `m` 的语义如何随 buffer 的 scope 变化。
2. 手推并用代码实现 `TileLayout.apply()` 的前向映射：行主序展平 → 按 shard extent 切分 → 逐分量按"步长@轴"记账 → 叠加 offset。
3. 用命名轴推导 Blackwell TMEM 累加器布局与 block-scaled MMA 的 scale-factor 布局（含 replication）的物理坐标。
4. 对 8-bit / 16-bit / 32-bit 的 buffer，把以"元素个数"计的 stride 换算到硬件的 32-bit TMEM 列。

## 2. 前置知识

本讲默认你已读过两篇前置讲义，这里只做要点回温：

- **u4-l2（命名轴的概念版）**：布局函数的返回值可以不是单个线性地址，而是一个"按轴名记账"的坐标字典；记法仍是 `S[(shape):(strides)]`，只是每个 stride 标注归属的物理轴（如 `4@laneid`）；同一根轴被多个 iter 引用时贡献相加；可用"元素数守恒"（逻辑元素总数 = 各轴取值数之积）检验布局是否双射。
- **u10-l1（TileLayout 三件套）**：`TileLayout(S[shape:strides] + R[replica_shape:replica_stride] + offset)`；内部每个 iter 是 `(extent, stride, axis)` 三元组；完整语义是集合 \( L(x) = \{D(x) + r + O \mid r \in R\} \)，而 `apply()` 只返回 \( D(x) + O \)。

还需要两个基础概念：

- **行主序展平（row-major flatten）**：把多维坐标压成一个整数下标。对形状 \( (S_0, S_1, \ldots) \)，最右维变化最快。这正是 PyTorch/CUDA 里连续张量的下标规则。
- **按给定 extents 切分（unflatten）**：展平的逆操作——用一个基数序列把整数下标拆回多个分量。C 语言里手工分离"年/月/日"就是一次切分。

一个容易踩的命名坑，本讲会反复强调：**逻辑坐标里的字母 \( m, n \)**（GEMM 语境下的行号/列号）与**默认物理轴 `m`**（布局记法里的线性轴）只是重名，毫无关系。书中 GEMM 内核的累加器形状是 \( M \times N \)，而 `1@m` 说的是"沿默认线性轴前进 1"。写练习脚本时建议逻辑坐标用 `(i, j)`，避免与轴名混淆。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [chapter_tirx_layout_api/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md) | 本讲主源码：TIRx Layout API 一章的正文，含命名轴表、forward mapping 算法、TMEM 与 scale-factor 实例 |
| [static/tirx-layout-demo/layout-demo.js](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/static/tirx-layout-demo/layout-demo.js) | 章首交互演示的脚本，其中用 JavaScript 忠实复刻了 TVM `layout.py` 的展平/切分/前向映射，是可运行的参考实现 |
| [static/tirx-layout-demo/index.html](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/static/tirx-layout-demo/index.html) | 演示页面本体，可点击逻辑元素查看物理坐标与全部副本 |

本讲不运行 GPU 内核，所有实践只需 Python（纯标准库即可）；安装了 `apache-tvm` 的读者可以额外做"对拍"验证。

## 4. 核心概念与源码讲解

### 4.1 命名轴族

#### 4.1.1 概念说明

布局里的轴**不是匿名维度**。每个名字要么指向一个真实的硬件坐标（线程的 lane、warp 编号、TMEM 的行列），要么指向一个编译器定义的布局坐标（默认线性轴 `m`）。这一设计解决的问题是：**一个线性整数地址无法描述"数据分布在多个线程的寄存器里"或"数据分布在 TMEM 的二维行列里"这类物理摆放**——它们天然是多坐标的。

两条纪律贯穿始终：

1. **轴名是布局的一部分**。`1@tx` 与 `1@tid_in_wg` 数值相同但指不同硬件位置；`1@laneid` 与 `1@TLane` 同理——前者是线程侧的 lane 编号，后者是 TMEM 数据侧的行地址。
2. **同一根轴可以被多个 iter 引用，贡献相加**。一个 shard 里两次出现 `laneid` 完全合法。

#### 4.1.2 核心流程

认识命名轴的流程就是"查表 → 辨义 → 验证"：

1. 拿到一个布局表达式，先把每个 stride 的 `@轴名` 查表归类：线程侧（`tx`/`laneid`/`warpid`/`tid_in_wg`）、CTA 侧（`bx` 等）、TMEM 侧（`TLane`/`TCol`）、默认线性（`m`）。
2. 对默认轴 `m`，追问一句：这个 buffer 的 scope 是什么？scope 决定 `m` 落在哪种存储上。
3. 用元素数守恒检验：逻辑元素总数应等于各轴占用位置数之积。

#### 4.1.3 源码精读

所有轴对象和布局对象都住在 `tvm.tirx.layout` 里，章首给出完整导入清单：

[chapter_tirx_layout_api/index.md:32-50](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L32-L50)

这段导入列出了 `TileLayout`、`ComposeLayout`、`S`、`R`，以及本讲的主角——命名轴 `laneid`、`warpid`、`tid_in_wg`、`TLane`、`TCol`、`m`，还有三个便捷构造器。注意这些轴**是参与 Python 求值的真实对象**，不是字符串标签：`4@laneid` 是用轴对象做 `@` 运算得到的表达式。

官方的命名轴总表：

[chapter_tirx_layout_api/index.md:190-204](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L190-L204)

翻译成中文速查表：

| 轴 | 含义 |
|---|---|
| `bx`, `by`, `bz` | CTA 在 grid 中的坐标 |
| `cbx`, `cby`, `cbz` | CTA 在 cluster 内的坐标 |
| `tx` | 线程在 CTA 内的坐标 |
| `warpid`, `laneid` | warp 编号；线程在其 warp 内的 lane 编号 |
| `wgid`, `tid_in_wg`, `wid_in_wg` | warpgroup 编号；线程/warp 在 warpgroup 内的位置 |
| `m` | 默认线性物理轴；后端存储由 buffer 的 scope 决定 |
| `TLane`, `TCol` | TMEM 的 Lane 方向与 Col 方向 |

紧接着 L204 那段话就是两条纪律的原文出处：轴名属于布局、不同轴上的相同整数代表不同硬件位置；并预告了本讲 4.4 的主题——`TCol` 的 stride 以 buffer 元素计，只有元素宽度为 32 bit 时才与硬件 Col 一一对应。

一个把多根轴揉在一起的真实布局——寄存器 fragment：

[chapter_tirx_layout_api/index.md:52-60](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L52-L60)

```python
frag = TileLayout(
    S[(8, 2, 4, 2) : (4@laneid, 1@warpid, 1@laneid, 1)]
)
```

这段代码把一个 `(8,2,4,2)` 的逻辑 tile 摆到 warp 的寄存器里：第 1、3 个 iter 都贡献 `laneid`（相加），第 2 个 iter 走 `warpid`，最后一个 stride 没写轴名，落到默认轴 `m`。

`m` 到底是什么？关键澄清在这里：

[chapter_tirx_layout_api/index.md:62-64](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L62-L64)

这段话说明了三件事：其一，数据布局章（u4-l2）用过的 `@reg` 记号在当前 TIRx API 中**没有注册独立的 `reg` 轴**；其二，当布局挂在寄存器支撑的 local buffer 上时，默认轴 `m` 就表示"该线程本地的线性槽位"——buffer 的 scope 决定数据住在寄存器里，`m` 在此语境下不暗含 global 或 shared memory；其三，对 `m`、`TCol` 这类存储轴，stride 一律以 **buffer 元素**为单位计量（4.4 展开）。

`tid_in_wg` 与 `m` 配合的现成例子是构造器 `wg_local_layout`：

[chapter_tirx_layout_api/index.md:366-370](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L366-L370)

`wg_local_layout(cols, rows=128)` 返回一个 warpgroup 本地寄存器 tile：逻辑行映射到 `tid_in_wg`、行内列映射到该线程的 `m` 轴；默认 `rows=128` 时 128 个线程各持有整行。同一个 `m`，挂到 SMEM buffer 上就是共享内存线性下标，挂到 local buffer 上就是线程本地槽位——**变的是 scope，不变的是"线性"这个语义**。

#### 4.1.4 代码实践

**实践目标**：建立"轴名 ≠ 匿名维度"的肌肉记忆，并确认自己环境中轴对象可用。

**操作步骤**：

1. 把上面的中文速查表抄一遍，合上表，对 `S[(8,2,4,2):(4@laneid,1@warpid,1@laneid,1)]` 的每个 stride 说出轴名与它描述的硬件位置。
2. 已按 u1-l3 安装 `apache-tvm` 的读者，把章首 [L32-50](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L32-L50) 的导入语句原样写进一个 `.py` 文件执行（TIRx 依赖源码检视，不能塞进 `python -c`），然后打印 `laneid`、`TLane`、`m` 三个对象。
3. 未安装 TVM 的读者跳过步骤 2，改用浏览器打开交互演示（[chapter_tirx_layout_api/index.md:66-94](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L66-L94)），在预设里选择带 `laneid`/`warpid` 的布局，点击元素观察坐标字典里出现哪些轴名。

**需要观察的现象**：坐标字典的键是轴名字符串（如 `laneid`、`warpid`、`m`），而不是一个数字；演示页还会额外列出 replica 产生的多个物理副本。

**预期结果**：能不查表说出每个轴的含义；安装了 TVM 的读者能成功导入且不报 `ImportError`（此步依赖本地环境，待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：`1@laneid` 和 `1@TLane` 都是整数 1，为什么是不同的物理位置？

**答案**：`laneid` 是线程侧坐标——"第几个线程的寄存器"；`TLane` 是 TMEM 数据侧坐标——"TMEM 第几行"。轴名是布局的一部分，数值只有在指定轴上才有意义。一个元素"放在 5 号线程手里"和"放在 TMEM 第 5 行"是完全不同的两件事。

**练习 2**：为什么当前 TIRx API 用默认轴 `m` 代替 u4-l2 讲过的 `@reg`？这样安全吗？

**答案**：因为布局总是附着在具体 buffer 上，而 buffer 的 scope（local/shared/tmem）已经唯一确定了后端存储；寄存器 local buffer 上的 `m` 自然解释为线程本地线性槽位，不会再与 global/shared 混淆，所以单独注册 `reg` 轴是冗余的。安全性来自"scope 决定存储、`m` 只表达线性位置"这一分工——原文见 [L62](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L62-L64)。

**练习 3**：`wg_local_layout(cols=64)`（默认 `rows=128`）的 tile 里，一个逻辑元素由哪几根轴共同定位？每线程持有多少个元素？

**答案**：行由 `tid_in_wg` 定位（128 个线程各占一行），行内 64 列由该线程的 `m` 轴定位；每线程持有 64 个元素，总共 \( 128 \times 64 = 8192 \) 个，元素数守恒成立。

### 4.2 forward mapping：apply() 的内部运转

#### 4.2.1 概念说明

前向映射（forward mapping）就是 `TileLayout.apply()` 做的事：**给定一个逻辑坐标，算出它的基础物理坐标** \( D(x) + O \)。它是我们检验一个布局是否写对的唯一可执行窗口——布局写错（stride 或轴标错）时，`apply()` 的输出会立刻与手推结果不符。

注意边界：`apply()` **不枚举 replica**。副本信息留在 `layout.replica` 里，由消费这个布局的 tile 操作处理。所以 `apply()` 的返回值只是集合 \( L(x) \) 中的基础位置，不是全部位置。

#### 4.2.2 核心流程

`apply()` 支持三种输入形式（原文见 [L206-214](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L206-L214)）：

```python
layout.apply(linear_coord)                    # 已展平的整数
layout.apply(*shard_coord)                    # 每个 shard iter 一个分量
layout.apply(*logical_coord, shape=input_shape)  # 逻辑坐标 + 逻辑形状
```

第三种形式能看全求值过程，分四步：

1. **展平**：把逻辑坐标 \( x = (x_0, \ldots, x_{r-1}) \) 按逻辑形状 \( (S_0, \ldots, S_{r-1}) \) 行主序压成单个整数：

\[ \text{flat} = x_0 \prod_{j=1}^{r-1} S_j + x_1 \prod_{j=2}^{r-1} S_j + \cdots + x_{r-2} S_{r-1} + x_{r-1} \]

2. **切分**：把 flat 按 shard 的 extents \( (e_0, e_1, \ldots, e_{n-1}) \) 从最低位起逐段拆出分量 \( (c_0, c_1, \ldots, c_{n-1}) \)，满足 \( 0 \le c_k < e_k \)。

3. **记账**：第 \( k \) 个分量贡献 \( c_k \cdot s_k @ a_k \)；**同一根轴上的贡献相加**。

4. **平移**：叠加固定 offset \( O \)。

写成伪代码：

```text
def apply(x, shape):
    flat   = row_major_flatten(x, shape)
    c[0..n-1] = split(flat, shard_extents)
    phys = {}
    for k in range(n):
        phys[shard[k].axis] += c[k] * shard[k].stride
    for axis, v in offset.items():
        phys[axis] += v
    return phys          # 只含基础坐标，不含 replica
```

前两种输入形式只是跳步：`linear_coord` 已经是 flat（跳过第 1 步）；`shard_coord` 直接给出每个 iter 的分量（跳过第 1、2 步）。第三种形式还允许逻辑形状的秩与 shard extents 不同，只要 flat 下标落在 shard 覆盖的逻辑范围内即可。

#### 4.2.3 源码精读

算法的权威描述：

[chapter_tirx_layout_api/index.md:216-258](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L216-L258)

这段正文依次给出展平公式、按 extents 切分、分量贡献式 \( c_k s_k @ a_k \)、同轴相加再加 offset，最后说明前两种输入形式跳过哪些步骤。

书中自带的手推实例（本讲的锚点例子）：

[chapter_tirx_layout_api/index.md:262-288](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L262-L288)

对布局 `S[(8,2,4,2):(4@laneid,1@warpid,1@laneid,1)] + R[2:4@warpid] + 5@warpid`，在 `(8,16)` 的输入 tile 上求 `apply(1, 3, shape=[8,16])`：

```python
layout.apply(1, 3, shape=[8, 16])
# {"laneid": 5, "warpid": 5, "m": 1}
```

三步推导（书中原样给出）：`(1,3)` 展平为 19；按 `(8,2,4,2)` 切分得 `(c0,c1,c2,c3) = (1,0,1,1)`；乘各自 stride 得基础坐标 `laneid=5, warpid=0, m=1`，再加 offset `5@warpid` 得 `warpid=5`。同时书中给出整个 tile 上的闭式解：

```text
laneid = 4 * i + (floor(j / 2) mod 4)
warpid = floor(j / 8) + 5
m      = j mod 2
```

replica `R[2:4@warpid]` 使 tile 操作还需处理 `warpid=9` 一侧的副本，但 `apply()` 不返回它——原文在 [L260](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L260) 与 [L278](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L278) 各强调一次。

这份算法在书仓的交互演示里有份"可运行的注释"——演示脚本注明它镜像了 TVM `layout.py` 的 `_flatten_coord` / `_split_coord` 加前向映射：

[static/tirx-layout-demo/layout-demo.js:257-290](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/static/tirx-layout-demo/layout-demo.js#L257-L290)

其中 `flattenCoord` 用 `flat = flat * shape[i] + coord[i]` 累乘实现行主序展平；`splitCoord` 从最高 iter 往低位逐段取模拆分；`forwardBase` 先展平切分，再把每个分量按 `phys[axis] += comps[k] * stride` 记账、最后叠加 offset——与正文伪代码逐行对应。读 JS 与读正文互为校验。

#### 4.2.4 代码实践

**实践目标**：用纯 Python 把 `apply()` 复刻出来，跑通书中锚点例子，确认你真正理解了四步流程。

**操作步骤**：

1. 新建 `apply_mini.py`（示例代码，非项目原有文件），写入以下内容：

```python
def flatten_coord(coord, shape):            # 步骤 1：行主序展平
    flat = 0
    for i in range(len(shape)):
        flat = flat * shape[i] + coord[i]
    return flat

def split_coord(flat, extents):             # 步骤 2：按 extents 切分
    res = [0] * len(extents)
    for i in range(len(extents) - 1, 0, -1):
        res[i] = flat % extents[i]
        flat //= extents[i]
    res[0] = flat
    return res

def apply_layout(shard, offset, coord, shape):   # 步骤 3+4：记账并平移
    comps = split_coord(flatten_coord(coord, shape), [e for e, _, _ in shard])
    phys = {}
    for (_, stride, axis), c in zip(shard, comps):
        phys[axis] = phys.get(axis, 0) + c * stride
    for axis, v in offset.items():
        phys[axis] = phys.get(axis, 0) + v
    return phys

frag   = [(8, 4, "laneid"), (2, 1, "warpid"), (4, 1, "laneid"), (2, 1, "m")]
offset = {"warpid": 5}
print(apply_layout(frag, offset, (1, 3), [8, 16]))
```

2. 运行 `python3 apply_mini.py`。
3. 加一段断言，对全部 \( 0 \le i < 8,\ 0 \le j < 16 \) 验证脚本输出等于书中闭式解 `laneid=4*i+(j//2)%4, warpid=j//8+5, m=j%2`。
4. 再调用 `apply_layout(frag, offset, split_coord(19, [8, 16]), [8, 16])` 模拟 `apply(linear_coord)` 形式。

**需要观察的现象**：步骤 2 打印的字典；步骤 3 断言是否全通过；步骤 4 与步骤 2 结果是否相同。

**预期结果**：打印 `{'laneid': 5, 'warpid': 5, 'm': 1}`——与书中 [L265-267](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L262-L288) 文档记载的返回值一致；闭式解断言全通过；linear 形式结果相同。以上为依据书中文档与手推的预期，脚本在本地跑通即完成验证（本讲义编写环境未能执行，待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`layout.apply(19)`（linear 形式）和 `layout.apply(1, 3, shape=[8,16])` 结果一样吗？为什么？

**答案**：一样。`(1,3)` 在形状 `(8,16)` 下行主序展平恰为 \( 1 \times 16 + 3 = 19 \)；linear 形式跳过的正是展平这一步，后续切分、记账、平移完全相同。

**练习 2**：把锚点例子的 offset `5@warpid` 改成 `5@laneid`，返回字典变成什么？

**答案**：基础坐标仍为 `laneid=5, warpid=0, m=1`，但平移落到 laneid 轴上，返回 `{'laneid': 10, 'warpid': 0, 'm': 1}`。offset 加在哪根轴上由其 `@轴名` 决定，与数值大小无关。

**练习 3**：为什么 `apply()` 拒绝枚举 replica？这样设计的好处是什么？

**答案**：因为"一个逻辑元素有多个物理副本"表达的是**生产/消费方式**（谁写、谁读哪份副本），而非位置计算——这由消费布局的 tile 操作决定（例如 scale-factor 的四份副本由 `tcgen05.cp` 的组播产生）。`apply()` 保持纯函数式的"一进一出"，职责单一；副本信息留在 `layout.replica`，由 tile 操作按需展开。

### 4.3 TMEM 布局示例：累加器与 scale factor

#### 4.3.1 概念说明

命名轴最能发挥威力的地方是 Blackwell TMEM——一个 \( 128 \text{ Lane} \times 512 \text{ Col} \)、每格 32 bit 的二维片上存储（回顾 u2-l2、u7-l3）。用 `TLane`/`TCol` 两根轴，TMEM 布局可以写成普通 `TileLayout`，不需要任何特殊语法。

本模块看两个真实布局：

- **累加器布局**：逻辑元素与 TMEM 坐标一一对应，纯 shard、无 replica。这是 `tcgen05.mma` 写 D 的标准摆放。
- **scale-factor 布局**：block-scaled MMA（MXFP8/NVFP4）的 SFA/SFB 需要**同一组逻辑 scale factor 同时出现在四个 32-lane 分区**，好让每个 warp 的 32-lane 窗口都读得到——这就用上 `R[...]`。

两者共用同一套 `TileLayout` 模型，差别只在有没有 replica。

#### 4.3.2 核心流程

对一个 TMEM 布局做前向映射，流程与 4.2 完全相同，只是轴换成 `TLane`/`TCol`：

1. 判断逻辑形状是否等于 shard extents——相等时切分分量就是逻辑坐标本身（最常见也最好读）。
2. 每个分量按 `stride@TLane` 或 `stride@TCol` 记账、同轴相加。
3. 若有 replica，另行枚举 \( r \) 得到完整集合 \( L(x) \)。

累加器布局 \( S[(2,128,112):(112@TCol,\ 1@TLane,\ 1@TCol)] \) 的映射结果：

\[ \text{TLane} = l, \qquad \text{TCol} = 112a + c \]

其中 \( (a,l,c) \) 是逻辑坐标。第二个式子之所以成立，是因为第一、三两个 iter 都贡献 `TCol`（\( 112a \) 与 \( c \) 相加）——又一次"同轴贡献相加"。

scale-factor 原子 \( S[(32,\ s_f):(1@TLane,\ 1@TCol)] + R[4:32@TLane] \) 的完整集合：

\[ L(r, s) = \{\, (\text{TLane} = r + 32q,\ \text{TCol} = s) \mid q \in \{0,1,2,3\} \,\} \]

即 32 行的一组 scale factor 同时出现在 lane \( 0\text{–}31 \)、\( 32\text{–}63 \)、\( 64\text{–}95 \)、\( 96\text{–}127 \) 四个分区。

#### 4.3.3 源码精读

先看最简单的 TMEM 布局如何写、如何挂到 buffer 上：

[chapter_tirx_layout_api/index.md:14-28](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L14-L28)

`S[(128,256):(1@TLane,1@TCol)]` 描述一个 \( 128\times256 \) 的 TMEM tile：逻辑行落 `TLane`、逻辑列落 `TCol`。随后同一段代码演示把它分别交给 `pool.alloc` 与 `T.decl_buffer`——从此 buffer 自带物理布局，tile 操作不必复述"哪些 lane/寄存器/线性位置持有元素"。

真实内核里的累加器布局（注意 extents 不是 2 的幂）：

[chapter_tirx_layout_api/index.md:290-315](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L290-L315)

```python
layout = TileLayout(
    S[(2, 128, 112) : (112@TCol, 1@TLane, 1@TCol)]
)
```

这段代码与配套正文给出：逻辑形状与 shard extents 同为 `(2,128,112)`，切分分量即逻辑坐标；元素 \( (a,l,c) \) 落在 \( \text{TLane}=l \)、\( \text{TCol}=112a+c \)；128-lane 的 iter 填满全部 TMEM 行，另两个 iter 合计覆盖 \( [0,224) \) 共 224 个 TCol 位置。L315 还解释了为什么故意用 112：**TMEM 布局维度不必是 2 的幂**，两个 112 列的区域正好覆盖 224 列而无需填充到 256——一个 block-scaled FP8 GEMM 可以据此为"两个累加器 stage + scale factor"留出 TMEM，而不是让单个累加器 tile 独占 256 列。

scale-factor 布局与 replication：

[chapter_tirx_layout_api/index.md:317-346](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L317-L346)

```python
scale = TileLayout(
    S[(32, sf_per_mma) : (1@TLane, 1@TCol)]
    + R[4 : 32@TLane]
)
```

这段代码定义了反复出现的 `32xsf_per_mma` 原子：shard 先给出 \( \text{TLane}=r,\ \text{TCol}=s \)；replica 再以步长 32 沿 `TLane` 复制出四份（\( q \in \{0,1,2,3\} \)），使每个 warp 的 32-lane TMEM 窗口都能访问同一组 scale factor（这正对应 u4-l3、u5-l3 讲过的 `.warpx4` 组播数据通路）。正文同时说明：完整布局还要在外面套 M 维与 K-scale-block 维的 iter，这个原子只描述单次 MMA 读到的局部模式。

收尾一句话点破两者的统一性（L346）：**累加器与 scale factor 用同一个 `TileLayout` 模型——前者一一映射，后者在同样的 `TLane`/`TCol` 空间里加了 replication。**

#### 4.3.4 代码实践

**实践目标**：用 4.2 的 `apply_layout` 函数验证两个 TMEM 布局的闭式解，体会"TMEM 布局没有任何特殊语法"。

**操作步骤**：

1. 在 `apply_mini.py` 中追加（示例代码）：

```python
tmem = [(2, 112, "TCol"), (128, 1, "TLane"), (112, 1, "TCol")]
print(apply_layout(tmem, {}, (1, 37, 100), [2, 128, 112]))

sf, sf_shape = [(32, 1, "TLane"), (4, 1, "TCol")], [32, 4]
base = apply_layout(sf, {}, (5, 3), sf_shape)
copies = [dict(TLane=base["TLane"] + 32 * q, TCol=base["TCol"]) for q in range(4)]
print(base, copies)
```

2. 运行并对照手推：累加器布局中 \( (1,37,100) \) 展平为 \( 128\times112 + 37\times112 + 100 = 18580 \)，再按 `(2,128,112)` 切分回 `(1,37,100)`。
3. 打开交互演示，选 TMEM 类预设，点几个元素核对坐标字典的键是 `TLane`/`TCol`。

**需要观察的现象**：累加器输出只有 `TLane`/`TCol` 两个键；scale-factor 的 `base` 与 `copies` 恰差 32 的倍数。

**预期结果**：累加器输出 `{'TLane': 37, 'TCol': 212}`（\( 112\times1+100=212 \)）；scale-factor 输出 base `{'TLane': 5, 'TCol': 3}`，copies 为 TLane 取 5/37/69/101、TCol 恒为 3 的四个字典。以上为手推预期，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么累加器布局敢用 extent 112？行主序展平/切分对此有障碍吗？

**答案**：没有障碍。展平和切分只做乘法、取模和整除，对基数没有任何 2 的幂要求；2 的幂只是让硬件移位实现更方便的常见特例。书中选 112 是刻意的资源规划——两个 112 列区域恰好覆盖 224 列，为 scale factor 等留出 TMEM（[L315](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L309-L315)）。

**练习 2**：`R[4:32@TLane]` 里的两个数 4 和 32 各是什么？把 32 误写成 16 会怎样？

**答案**：4 是副本个数（replica iter 的 extent），32 是相邻副本沿 `TLane` 的间距。写成 16 后四个副本落在 lane 5/21/37/53，只覆盖前两个 32-lane 分区，后两个 warp 窗口读不到这组 scale factor——layout 双射性检查发现"每元素 4 个副本"的账面对不上实际覆盖范围前，错误通常在运行期才暴露。

**练习 3**：累加器布局 `S[(2,128,112):(112@TCol,1@TLane,1@TCol)]` 中，为什么第一和第三个 iter 都标 `@TCol`？

**答案**：它们分别编码逻辑坐标 \( a \)（哪一段 112 列）与 \( c \)（段内列偏移），物理上都落在 TCol 轴上，前向映射时贡献相加得 \( 112a+c \)。"同轴相加"正是用多根逻辑维拼一根物理轴的机制。

### 4.4 元素位宽换算：从元素 stride 到 32-bit 硬件列

#### 4.4.1 概念说明

存储轴（`m`、`TCol`）的 stride 以 **buffer 元素**为单位，而 TMEM 硬件的每一列固定是 **32 bit**。两者只在元素宽度恰为 32 bit 时一一对应；对 8-bit / 16-bit 的 buffer，相邻若干元素会**打包进同一个硬件列**：

| 元素宽度 | 每个硬件列装几个元素 | 换算 |
|---|---|---|
| 32 bit（如 fp32） | 1 | 硬件列 = \( s \)，无槽位 |
| 16 bit（如 fp16/bf16） | 2 | 硬件列 = \( \lfloor s/2 \rfloor \)，槽位 = \( s \bmod 2 \) |
| 8 bit（如 UE8M0 scale factor） | 4 | 硬件列 = \( \lfloor s/4 \rfloor \)，字节位 = \( s \bmod 4 \) |

统一公式（元素宽度 \( w \) bit）：

\[ \text{硬件列} = \left\lfloor \frac{s}{32/w} \right\rfloor, \qquad \text{槽位} = s \bmod \frac{32}{w} \]

为什么这件事重要：布局写的是"第 \( s \) **个元素**在第 \( s \) **列**"，但硬件只认 32-bit 列。读布局的人若不做换算，会把 8-bit scale factor 的 4 列误当成 4 个硬件列，TMEM 占用估算直接差 4 倍。

#### 4.4.2 核心流程

拿到一个带 `@TCol`（或 `@m`）stride 的布局后按三步换算：

1. 查 buffer 的 dtype，确定元素宽度 \( w \)。
2. 把元素坐标 \( s \) 经上面的公式映射为 (硬件列, 槽位)。
3. 用换算后的硬件列数核对 TMEM 用量预算（对照 u7-l3 的分配约束：nCols 只有 32/64/128/256/512 五档）。

对 16-bit 的情形，这与 u7-l4 讲过的 `tcgen05.ld/st` 的 `.pack::16b` 打包规则是同一件事的两侧：FA4 用 `tmem` 与 `tmem_as_f16` 双视图寻址同一块物理存储时，fp16 视图列 \( j \) 对应物理列 \( \lfloor j/2 \rfloor \)、槽位 \( j \bmod 2 \)。

#### 4.4.3 源码精读

总规则只有两句话，写在章首：

[chapter_tirx_layout_api/index.md:62-64](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L62-L64)

这段话给出两条纪律：其一（承接 4.1）寄存器 buffer 上的 `m` 是线程本地线性位置；其二，`m`、`TCol` 等存储轴的 stride 以 buffer 元素计——32-bit TMEM buffer 里沿 `TCol` 前进一个元素等于前进一个硬件 Col，而 8-bit / 16-bit buffer 中若干相邻元素打包进一个硬件 Col，并预告 scale-factor 例子会把这件事落到实处。

命名轴表下方的补充警告：

[chapter_tirx_layout_api/index.md:204](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L204)

这句话明确限定：**`TCol` stride 与硬件 Col 一一对应仅当元素宽度为 32 bit**。

落 to 具体数字的地方在 scale-factor 小节：

[chapter_tirx_layout_api/index.md:328-344](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L328-L344)

这段正文对 8-bit scale-factor buffer 给出换算：`TCol` 坐标仍以元素计，四个相邻元素位置打包进一个 32-bit 硬件列，因此硬件列与字节位分别是 \( s//4 \) 与 \( s\%4 \)；随后才叠加 replica 的 \( \text{TLane} = r + 32q \)。

#### 4.4.4 代码实践

**实践目标**：实现统一的位宽换算函数，并对三种位宽打印对照表。

**操作步骤**：

1. 在 `apply_mini.py` 中追加（示例代码）：

```python
def hw_col(elem_index, bits):
    per_col = 32 // bits
    return elem_index // per_col, elem_index % per_col

for bits in (32, 16, 8):
    print(bits, "bit:", [hw_col(s, bits) for s in range(8)])
```

2. 运行，观察元素下标 0–7 在三种位宽下的 (硬件列, 槽位)。
3. 回答：`sf_per_mma = 4` 的 8-bit scale factor 原子 `S[(32,4):(1@TLane,1@TCol)]` 总共占用几个硬件列？

**需要观察的现象**：32-bit 时槽位恒为 0；16-bit 时每两个元素共享一列；8-bit 时每四个元素共享一列。

**预期结果**：32-bit 输出 `(0,0),(1,0),...,(7,0)`；16-bit 输出 `(0,0),(0,1),(1,0),(1,1),...`；8-bit 输出 `(0,0),(0,1),(0,2),(0,3),(1,0),...`。步骤 3 的答案：4 个元素恰占 **1** 个硬件列（\( s=0..3 \) 全落在列 0 的四个字节位）。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：一个 `(128, 64)` 的 fp16（16-bit）累加器视图落在 TMEM，物理上占多少个硬件列？

**答案**：64 个 fp16 视图列两两打包，占 \( 64/2 = 32 \) 个硬件列。这与 u7-l4 的结论一致：128 个 fp16 恰占 64 个物理列的另一半视角——换算方向不同，公式相同。

**练习 2**：把布局 `S[(32, sf_per_mma):(1@TLane, 1@TCol)]` 的 buffer 从 8-bit 换成 16-bit，`sf_per_mma=4` 时硬件占用如何变化？stride 要改吗？

**答案**：16-bit 下 4 个元素占 2 个硬件列（翻倍）。布局里的 `1@TCol` stride **不用改**——它以元素计，语义不变；变化的只是"元素 → 硬件列"的换算比例。这正是"布局以元素为单位"这一设计的好处：换 dtype 不必重写布局，只需重算物理占用。

**练习 3**：为什么 TIRx 不直接把 `TCol` 的 stride 定义成"硬件 32-bit 列数"，省去换算？

**答案**：以元素为单位让布局与 dtype 解耦（见练习 2），同一份 `S[...]` 可以挂到不同宽度的 buffer 上；硬件列数只在估算 TMEM 占用、对齐分配档位（u7-l3 的五档 nCols）时才需要。若以硬件列为单位，每次换 dtype 都得重写所有 stride，且无法用"元素数守恒"直接检验双射性。

## 5. 综合实践

把四个模块串成一个自检脚本 `practice_u10_l2.py`（示例代码，纯标准库、无 GPU 要求）。它复刻 `apply()` 并覆盖本讲全部知识点：

```python
# —— 复刻 TileLayout.apply() 的前向映射（对应 4.2）——
def flatten_coord(coord, shape):
    flat = 0
    for i in range(len(shape)):
        flat = flat * shape[i] + coord[i]
    return flat

def split_coord(flat, extents):
    res = [0] * len(extents)
    for i in range(len(extents) - 1, 0, -1):
        res[i] = flat % extents[i]
        flat //= extents[i]
    res[0] = flat
    return res

def apply_layout(shard, offset, coord, shape):
    comps = split_coord(flatten_coord(coord, shape), [e for e, _, _ in shard])
    phys = {}
    for (_, stride, axis), c in zip(shard, comps):
        phys[axis] = phys.get(axis, 0) + c * stride
    for axis, v in offset.items():
        phys[axis] = phys.get(axis, 0) + v
    return phys

def hw_col(elem_index, bits):                 # 对应 4.4
    per_col = 32 // bits
    return elem_index // per_col, elem_index % per_col

# —— 布局一：laneid/warpid 复合 fragment（对应 4.1/4.2）——
frag   = [(8, 4, "laneid"), (2, 1, "warpid"), (4, 1, "laneid"), (2, 1, "m")]
offset = {"warpid": 5}
got = apply_layout(frag, offset, (1, 3), [8, 16])
assert got == {"laneid": 5, "warpid": 5, "m": 1}, got     # 书中锚点例子
assert all(apply_layout(frag, offset, (i, j), [8, 16]) ==
           dict(laneid=4*i + (j//2) % 4, warpid=j//8 + 5, m=j % 2)
           for i in range(8) for j in range(16))          # 全 tile 闭式解

# —— 布局二：TMEM 累加器（对应 4.3）——
tmem = [(2, 112, "TCol"), (128, 1, "TLane"), (112, 1, "TCol")]
assert all(apply_layout(tmem, {}, (a, l, c), [2, 128, 112]) ==
           dict(TLane=l, TCol=112*a + c)
           for a in range(2) for l in range(128) for c in range(112))

# —— 布局三：scale-factor 原子 + replica 枚举（对应 4.3）——
sf = [(32, 1, "TLane"), (4, 1, "TCol")]
base = apply_layout(sf, {}, (5, 3), [32, 4])
full = [dict(TLane=base["TLane"] + 32*q, TCol=base["TCol"]) for q in range(4)]
assert base == {"TLane": 5, "TCol": 3}
assert [d["TLane"] for d in full] == [5, 37, 69, 101]

# —— 位宽换算（对应 4.4）——
assert [hw_col(s, 8)  for s in range(8)] == [(0,0),(0,1),(0,2),(0,3),(1,0),(1,1),(1,2),(1,3)]
assert [hw_col(s, 16) for s in range(4)] == [(0,0),(0,1),(1,0),(1,1)]
assert hw_col(7, 32) == (7, 0)
print("all assertions passed")
```

**任务要求**：

1. 运行脚本，确认打印 `all assertions passed`。所有断言值均来自书中文档记载的例子（[L262-288](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L262-L288)）与手推算术；本讲义编写环境未能执行，待本地验证。
2. 扩展一：给布局一加上 `R[2:4@warpid]` 的完整副本枚举（模仿布局三的写法），验证副本落在本章所说的 warps 5/6 与 9/10。
3. 扩展二（可选，需按 u1-l3 安装 `apache-tvm==0.26.0`）：把书中 [L173-177](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L173-L177) 的完整布局（S + R + offset）构造为真实 `TileLayout`，调用其 `apply(1, 3, shape=[8,16])`，与你的 `apply_layout` 输出对拍。布局代数不需要 GPU；若 API 行为与本书记载有出入，以你安装的 TVM 版本实测为准（待本地验证）。

## 6. 本讲小结

- **命名轴不是匿名维度**：`bx/tx/laneid/warpid/tid_in_wg` 是线程侧坐标，`TLane/TCol` 是 TMEM 数据侧坐标；轴名是布局的一部分，不同轴上的相同整数代表不同硬件位置，同轴贡献相加。
- **默认轴 `m` 的语义随 buffer scope 变化**：挂在寄存器 local buffer 上即是线程本地线性槽位（替代旧记号 `@reg`），挂在 SMEM/GMEM buffer 上是对应存储的线性下标；scope 定存储，`m` 只表达线性。
- **前向映射四步**：行主序展平 → 按 shard extents 切分 → 每个分量按 `stride@axis` 记账（同轴相加）→ 叠加 offset；`apply()` 只返回基础坐标 \( D(x)+O \)，replica 留给消费布局的 tile 操作。
- **TMEM 布局没有特殊语法**：累加器 `S[(2,128,112):(112@TCol,1@TLane,1@TCol)]` 是纯 shard（且维度可非 2 的幂，\( \text{TCol}=112a+c \)）；scale factor 在同一 `TLane/TCol` 空间加 `R[4:32@TLane]`，四副本覆盖四个 32-lane 分区。
- **存储轴 stride 以元素计**：硬件列固定 32 bit，元素宽度 \( w \) 时每列装 \( 32/w \) 个元素，硬件列 \( =\lfloor s/(32/w)\rfloor \)、槽位 \( = s \bmod (32/w) \)；只有 32-bit 元素才与 `TCol` 一一对应。

## 7. 下一步学习建议

本讲结束后，你已经能读懂并手推任何**仿射**的 `TileLayout`。下一讲 u10-l3（ComposeLayout 与 swizzle 变换）处理仿射表达不了的部分：共享内存的 XOR swizzle 是非仿射的地址置换，TIRx 用 `ComposeLayout(per_element, swizzle_len, atom_len, tile_layout)` 把"先算线性 `m` 地址、再做 XOR 重排"两步复合起来。建议：

1. 先做本讲综合实践，确保 `apply_mini.py` 全部断言通过。
2. 预读主源码的 [ComposeLayout 一节（L374-470）](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L374-L470)，注意它要求内部 tile layout 只产出一根 `m` 轴线性地址——这正是本讲默认轴 `m` 的直接延续。
3. 对照 u4-l4 的 XOR swizzle 公式（`mapped_col = c ⊕ r`）与 bank conflict 分析，思考"为什么 swizzle 必须独立于 `S[...]` 之外"。
