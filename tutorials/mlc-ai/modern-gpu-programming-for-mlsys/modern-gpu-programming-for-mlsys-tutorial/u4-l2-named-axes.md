# 第 4 单元第 2 讲：命名轴——从线性地址到物理坐标

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清**命名轴（named axes）**解决了什么问题：把布局函数 \( f_D(x) \) 的返回值从「一个整数线性地址」推广为「一组带名字的物理坐标」。
2. 用 `@TLane` / `@TCol` 描述 TMEM 的二维 (Lane, Column) 地址空间，并**区分 TMEM 的 Lane 地址坐标与线程的 lane ID**——这是两个不同空间、不同规模的概念。
3. 读懂 warp 级寄存器 fragment 的布局记号：`@laneid`、`@reg`、`@warpid`，并能手工推导「逻辑元素 → (warpid, laneid, 本地槽位)」的完整映射。
4. 体会这套记号的统一性：shape/strides 的点积规则完全不变，唯一的变化是每个 stride 都**归属到一个显式命名的物理轴**上。

本讲依赖 u4-l1 的 Shape-Stride 模型与一般布局函数 \( f_D(x)=\sum_k c_k s_k \)。上一讲的 \( f_D(x) \) 返回一个整数；本讲让它返回一个「坐标字典」。

## 2. 前置知识

阅读本讲前，请确认你理解以下概念（不熟悉也没关系，下面用通俗语言快速补齐）：

- **布局函数（u4-l1）**：\( S[(\text{shape}):(\text{strides})] \) 先按 shape 把扁平逻辑索引 \( x \) unflatten 成坐标 \( (c_0,\ldots,c_{n-1}) \)，再与 strides 点积得到物理位置。分块布局 `S[(4,2,2,4):(16,4,8,1)]` 是上一讲的终点。
- **线程层级与 laneid（u2-l1）**：warp 是 32 个线程的锁步执行单位，`laneid = threadIdx.x % 32` 标识线程在 warp 内的编号（0–31）；warpgroup 由 4 个 warp 组成，warp 在组内有编号 `warpid`（0–3）。
- **TMEM 的物理结构（u2-l2）**：Blackwell 的 Tensor Memory 是二维片上存储：128 个 Lane 行 × 最多 512 个 32-bit 列，存放 Tensor Core 的累加器。
- **寄存器 fragment**：Tensor Core 的 warp 集体指令不把矩阵当作整块数据，而是把一个 tile **打散分布到 warp 内 32 个线程各自的寄存器**里；每个线程持有的那一小份就叫它的 register fragment（fragment = 碎片）。
- **「地址」与「坐标」的区别**：一维存储（GMEM/SMEM 的线性视图）里一个数字就能定位一个元素；二维存储（如 TMEM）或者「分散在多个线程手里」的数据（fragment），则需要**多个数字**才能定位。

一个直觉性的铺垫：上一讲我们把 64 个元素排进一条一维内存，得到一个 0–63 的整数；本讲要回答的是——如果这 64 个元素不是躺在一条内存里，而是**每 2 个一组塞进 32 个线程的寄存器**，或者**排进一个 128×512 的二维阵列**，布局函数的输出该长什么样？

## 3. 本讲源码地图

本讲的主战场是书章「Data Layout and Its Notation」的命名轴一节，配合两个交互演示与两处后续章节的复用：

| 文件 | 作用 |
| --- | --- |
| [chapter_data_layout/index.md:L173-L245](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L173-L245) | 本讲核心：「Named Axes」整节——引言（L173–L177）、TMEM 二维地址空间（L179–L205）、寄存器 fragment（L207–L245） |
| [zh/chapter_data_layout/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_data_layout/index.md) | 同一章的中文镜像，结构与英文版一一对应，可对照阅读 |
| [_extra/demo/tiled_layout.html](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tiled_layout.html) | 上一讲的终点：`S[(4,2,2,4):(16,4,8,1)]`，所有 stride 都落在**同一根隐式线性轴**上。本讲以它作对照 |
| [_extra/demo/thread_register.html](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/thread_register.html) | 本讲核心演示：`S[(8,4,2):(4@laneid,1@laneid,1@reg)]`，点击单元格显示它归属的 lane 与 fragment 槽位 |
| [chapter_tmem/index.md:L14-L16](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L14-L16) | TMEM 章开篇，一句话点破「TMEM Lane 是地址坐标，不是线程的 lane ID」 |
| [chapter_tirx_layout_api/index.md:L52-L64](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L52-L64) | warp 级 fragment `S[(8,2,4,2):(4@laneid,1@warpid,1@laneid,1)]` 的出处（本讲综合实践会手推它；该章本身是 u10 的主题） |

## 4. 核心概念与源码讲解

### 4.1 命名轴：让 \( f_D(x) \) 返回「带名字的坐标」

#### 4.1.1 概念说明

上一讲的布局把每个元素映射到**一个线性内存地址**。但有些 GPU 存储空间用一个数字根本定位不了物理位置，书中点名的两个直接例子就是 **TMEM** 和**寄存器 fragment**（见 [chapter_data_layout/index.md:L173-L177](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L173-L177)）：TMEM 是二维阵列，要 (行, 列) 两个坐标；fragment 的数据散在多个线程手里，要 (哪个 lane, 哪个槽位) 两个坐标。

命名轴的解法非常克制：**记号一个字都不改**，仍是 `S[(shape):(strides)]`，只是给每个 stride 标注它归属的物理轴，例如 `4@laneid` 表示「这一步移动 4 个 laneid 单位」。于是：

- 普通线性内存其实也有一根地址轴，记作 `@m`——以前只是省略不写；
- \( f_D(x) \) 的返回值从「一个整数」变成「按轴名累加的一组坐标」，可以想成一个字典 `{"laneid": 21, "reg": 1}`。

关键认知：**shape/strides 的点积规则原封不动**。shape 仍然决定 \( x \) 如何分解成坐标，strides 仍然决定每个坐标贡献多少偏移，唯一的自由度是这个偏移落在**哪根轴**上。同一根轴被多个 iter 引用时，贡献**相加**；不同轴互不干扰。

#### 4.1.2 核心流程

推广后的一般布局函数：

\[ f_D(x) = \sum_{k=0}^{n-1} c_k \cdot (s_k\ @\text{axis}_k), \qquad (c_0,\ldots,c_{n-1})=\operatorname{unflatten}(x; e_0,\ldots,e_{n-1}) \]

求值过程用伪代码描述：

```text
输入: shape = (e0, ..., en-1), strides = (s0, ..., sn-1), axes = (a0, ..., an-1)
coords = unflatten(x, shape)              # 与上一讲完全相同
result = { 轴名: 0 }                       # 一张按轴名索引的累加表
for k in 0 .. n-1:
    result[axes[k]] += coords[k] * strides[k]   # 同轴多次出现则累加
返回 result                                 # 例如 {"TLane": 5, "TCol": 3}
```

对照上一讲的分块布局 `S[(4,2,2,4):(16,4,8,1)]`：它等价于把所有 stride 挂在同一根隐式轴 `@m` 上，即 `S[(4,2,2,4):(16@m,4@m,8@m,1@m)]`，四个贡献相加后塌缩成一个整数地址——**线性地址布局只是命名轴布局在单轴上的退化特例**。这正是 [tiled_layout.html:L23](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tiled_layout.html#L23) 标题里那组无 tag strides 的含义。

#### 4.1.3 源码精读

**① 为什么需要命名轴。**书在 [chapter_data_layout/index.md:L173-L177](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L173-L177) 开门见山：前面的布局把每个元素映射到线性内存地址，但某些 GPU 存储空间需要**多于一个坐标**才能确定一个物理位置，TMEM 与寄存器 fragment 是两个直接例子。

**② 把线性地址轴 `@m` 显式化。**书在 [chapter_data_layout/index.md:L196-L205](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L196-L205) 写道：普通线性内存只有一根地址轴 `@m`；把这个 tag 写明确，一个行主序 `8×16` 内存 tile 就是

```text
S[(8, 16) : (16@m, 1@m)]

(row, col) = unflatten(x; 8, 16)
f_D(x) = (row·16 + col)@m
```

这段代码说明：行主序布局 = 两个坐标分量都投影到同一根 `@m` 轴上相加。**我们一直在用的「地址」其实是命名轴世界里最平凡的一根轴。**

#### 4.1.4 代码实践

**实践目标**：亲手实现「按轴累加」的求值器，验证 `@m` 显式化后与上一讲的点积公式给出相同结果。

**操作步骤**（以下为示例代码，纯 Python，无 GPU 依赖）：

```python
def unflatten(x, shape):
    coords, rest = [], x
    for e in reversed(shape):          # 从最低维开始取余数
        coords.append(rest % e)
        rest //= e
    return tuple(reversed(coords))

def named_apply(x, shape, strides, axes):
    """命名轴版布局函数：返回 {轴名: 坐标} 字典（示例代码）"""
    coords = unflatten(x, shape)
    out = {}
    for c, s, a in zip(coords, strides, axes):
        out[a] = out.get(a, 0) + c * s  # 同轴多次出现则累加
    return out

# 行主序 8×16，@m 显式化
for x in [0, 1, 16, 17, 127]:
    print(x, named_apply(x, (8, 16), (16, 1), ("m", "m")))
```

**需要观察的现象**：所有元素都返回单键字典 `{"m": ...}`；`x=16` 与 `x=17` 的 `m` 值相差 1（同一行相邻列），`x=1` 与 `x=16` 相差 15。

**预期结果**（由公式推导）：输出依次为 `m = 0, 1, 16, 17, 127`，与上一讲 `addr = row·16 + col` 的点积结果完全一致——验证「线性地址 = 单轴命名轴布局」。

#### 4.1.5 小练习与答案

**练习 1**：`S[(4,2,2,4):(16@m,4@m,8@m,1@m)]` 中元素 `x=43` 的 `m` 坐标是多少？
**答案**：`unflatten(43;(4,2,2,4)) = (2,1,0,3)`（`43//16=2`，`(43//8)%2=1`，`(43//4)%2=0`，`43%4=3`），点积 `2·16+1·4+0·8+3 = 39`。

**练习 2**：为什么说上一讲的 Shape-Stride 模型是命名轴模型的特例？
**答案**：把所有 stride 挂到同一根 `@m` 轴上，各分量的贡献相加塌缩为一个整数，即线性地址；shape/strides 的取值规则与求值步骤完全没有变化。

**练习 3**：如果一个布局里两根不同的轴各返回一个坐标，元素总数与坐标组合数应满足什么关系？
**答案**：布局应是双射——每个逻辑元素对应唯一的物理坐标组合，因此元素总数 = 各轴取值数目的乘积（例如 64 个元素 = 32 个 laneid × 2 个 reg 槽位）。这是后面检验 fragment 布局正确性的基本工具。

### 4.2 TMEM 的二维 (TLane, TCol) 地址空间

#### 4.2.1 概念说明

Blackwell TMEM 天生是二维的：每个 CTA 有 **128 个 lane 行**和**最多 512 个 32-bit 列**（[chapter_data_layout/index.md:L179-L184](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L179-L184)）。因此确定一个 TMEM 位置**必须同时给出 lane 坐标和列坐标**——单根线性轴无法区分这两个维度。

本讲最容易踩的坑是把 **TMEM 的 Lane** 与**线程的 lane ID** 混为一谈。TMEM 章开篇有一句原话专门澄清（[chapter_tmem/index.md:L14](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L14)）：*PTX 称它的两个地址坐标为 Lane 和 Column；在 TIRx 布局记号里对应 `TLane` 与 `TCol`。**TMEM 的 Lane 是一个地址坐标，不是线程的 lane ID。*** 区分要点：

| | TMEM 的 `TLane` | 线程的 `laneid` |
| --- | --- | --- |
| 属于哪个空间 | **数据侧**：TMEM 存储阵列的行号 | **访问侧**：warp 内线程的编号 |
| 取值范围 | 0–127（每个 CTA 128 行） | 0–31（每个 warp 32 线程） |
| 谁使用它 | 描述累加器数据摆在哪 | 描述哪个线程在执行访存 |

两者的联系要等到 TMEM 章的 warp 访问窗口才建立：warpgroup 内 4 个 warp 各自只能访问固定的 32 个 TLane 位置窗口（[chapter_tmem/index.md:L88-L97](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L88-L97)），本讲只需记住「名字相似、空间不同」，窗口细节留给 u7-l3。

#### 4.2.2 核心流程

TMEM 用 `@TLane` 和 `@TCol` 两根轴。一个 `128×256` 的累加器 tile 的布局是（[chapter_data_layout/index.md:L186-L194](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L186-L194)）：

```text
S[(128, 256) : (1@TLane, 1@TCol)]

(row, col) = unflatten(x; 128, 256)
f_D(x) = row@TLane + col@TCol
```

求值步骤：

1. `unflatten(x; 128, 256)` 得 `(row, col)`，`row = x // 256`，`col = x % 256`；
2. `row` 乘 stride `1` 累加到 `TLane` 轴，`col` 乘 stride `1` 累加到 `TCol` 轴；
3. \( f_D(x) \) 返回 `{"TLane": row, "TCol": col}`。

这里 \( f_D(x) \) **不再返回一个整数地址，而是同时返回 `TLane=row` 与 `TCol=col`**——这是命名轴与上一讲最本质的分界（原话见 [chapter_data_layout/index.md:L196](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L196)）。stride 也不必都是 1：例如把逻辑列打包进 32-bit 硬件列时（8-bit/16-bit 数据多个共用一列），`@TCol` 的 stride 会以「每前进多少个逻辑元素跨一个硬件列」的形式出现——那是 u10 的 scale-factor 例子。

#### 4.2.3 源码精读

**① 二维结构的动机。**[chapter_data_layout/index.md:L181-L183](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L181-L183) 说明：每个 CTA 有 128 个 lane 行与最多 512 个 32-bit 列，因此一个 TMEM 位置需要 lane 与 column 两个坐标。这段代码对应书中的 TMEM 网格插图（`../img/tmem_grid.png`），一个 `128×256` 累加器占据整块 128 行 × 256 列区域。

**② 布局记号与返回值形态。**[chapter_data_layout/index.md:L186-L197](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L186-L197) 给出 `S[(128,256):(1@TLane,1@TCol)]` 及其求值式，并明确写出「\( f_D(x) \) 不再返回单个整数地址，而是同时返回 `TLane=row` 和 `TCol=col`」；紧接着用 `@m` 对照说明普通线性内存只有一根地址轴。

**③ TMEM 章 的呼应。**TMEM 章开篇（[chapter_tmem/index.md:L12-L16](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L12-L16)）回顾：PTX 的两个地址坐标 Lane/Column 对应 TIRx 的 `TLane`/`TCol`，每个 `(Lane, Column)` 单元 32 bit，且 **TMEM Lane 是地址坐标而非线程 lane ID**。后续在 TIRx 程序里给 TMEM buffer 挂布局时（如 `S[(128,256):(1@TLane,1@TCol)]`），逻辑坐标 `(m,n)` 到硬件坐标的换算就由这根轴完成，代码可以继续写 `tmem[m,n]`。

#### 4.2.4 代码实践

**实践目标**：用 4.1 的 `named_apply` 枚举 TMEM 布局的若干元素，确认坐标取值范围与双射性。

**操作步骤**（示例代码，接 4.1.4 的定义）：

```python
shape, strides, axes = (128, 256), (1, 1), ("TLane", "TCol")
for x in [0, 1, 256, 257, 5 * 256 + 3, 32767]:
    print(x, named_apply(x, shape, strides, axes))

# 双射性检查：全部 32768 个元素的坐标组合应互不重复
seen = set()
for x in range(128 * 256):
    seen.add(tuple(named_apply(x, shape, strides, axes).items()))
print("unique positions:", len(seen), "expected:", 128 * 256)
```

**需要观察的现象**：`x` 每加 1 只有 `TCol` 加 1；`x` 加 256 才使 `TLane` 加 1（因为 `unflatten(x;128,256)` 中 256 是低维大小）。

**预期结果**（由公式推导）：`x=0 → (TLane 0, TCol 0)`，`x=1 → (0, 1)`，`x=256 → (1, 0)`，`x=257 → (1, 1)`，`x=1283 → (5, 3)`，`x=32767 → (127, 255)`；双射检查输出 `unique positions: 32768 expected: 32768`。注意逻辑行只到 127——`TLane` 的取值范围由 shape 第一维 128 决定，与硬件的 128 行恰好对齐。

#### 4.2.5 小练习与答案

**练习 1**：`S[(128,256):(1@TLane,1@TCol)]` 中，逻辑元素 `(row=64, col=100)` 的 `x` 是多少？物理坐标是什么？
**答案**：`x = 64·256 + 100 = 16484`；物理坐标 `TLane=64, TCol=100`（两根轴 stride 都是 1，物理坐标即逻辑坐标）。

**练习 2**：为什么不能用一根线性轴（比如把 TMEM 看作 `128×512` 个 32-bit 字的一维数组）来描述 TMEM 布局？
**答案**：从「能否算出位置」看似乎可以，但会丢失两件关键信息：其一，TMEM 沿**列**动态分配（`tcgen05.alloc` 按 Column 预留、每列含全部 128 个 Lane，见 [chapter_tmem/index.md:L7-L8](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L7-L8)），分配与 warp 访问窗口都以 Lane/Column 为单位操作；其二，Tensor Core 写累加器、`tcgen05.ld/st` 搬数据都按 (Lane, Column) 寻址，硬件指令的语义本身就是二维的。命名轴让布局直接对齐硬件的地址语义。

**练习 3**：一个 warp 的 `tcgen05.ld` 只能访问 32 个 TLane 位置。若某 warp 的窗口是 TLane 32–63，`S[(128,256):(1@TLane,1@TCol)]` 描述的累加器中哪些逻辑行它读得到？
**答案**：逻辑行 `row` 与 `TLane` 一一对应（stride 1），因此该 warp 能读到 `row = 32…63` 这 32 行；读完整 128 行需要 warpgroup 的 4 个 warp 各读自己的窗口（详见 u7-l3 / [chapter_tmem/index.md:L88-L97](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L88-L97)）。

### 4.3 寄存器 fragment 映射：laneid 与 reg

#### 4.3.1 概念说明

命名轴的第二个现场是 Tensor Core 的**寄存器 fragment**。以书中一个 m8n8 风格的 fragment 为例（[chapter_data_layout/index.md:L209-L214](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L209-L214)）：逻辑上它是 `8×8` 的 tile、64 个元素；物理上这些元素**分布在 warp 的 32 个 lane 里，每个 lane 持有 2 个 fragment 槽位**。

于是「元素在哪」这个问题有了两个组成部分：**哪个 lane 拥有它**，以及**它在该 lane 的哪个槽位**。`laneid` 一个坐标不够用——这正是需要第二根轴的原因：

- `@laneid`：warp 内的 lane 编号（0–31），`laneid = threadIdx.x % 32`；
- `@reg`：该 lane **本地**的 fragment 槽位编号（lane-local 坐标）。注意它是布局层面的逻辑槽位；具体指令还可能把多个低精度元素**打包进一个 32-bit 硬件寄存器**（[chapter_data_layout/index.md:L231-L234](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L231-L234)）。

这解释了 fragment 布局为什么天然是「跨线程」的：布局函数的输出不再是「内存里的第几个字」，而是「**哪个线程的第几个寄存器槽位**」。读懂它，你才能明白 `mma.sync` 之后每个线程手里那几个数对应矩阵的哪些元素——这是 u5 单元三代 Tensor Core 布局的地基。

#### 4.3.2 核心流程

该 fragment 的映射规则（[chapter_data_layout/index.md:L216-L219](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L216-L219)）：

```text
laneid = row·4 + col//2
reg    = col%2
```

写成命名轴布局（[chapter_data_layout/index.md:L236-L245](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L236-L245)）：

```text
S[(8, 4, 2) : (4@laneid, 1@laneid, 1@reg)]

(c0, c1, c2) = unflatten(x; 8, 4, 2) = (row, col//2, col%2)
f_D(x) = (c0·4 + c1·1)@laneid + c2·1@reg
```

求值步骤：

1. 扁平索引 `x = row·8 + col`，按 shape `(8,4,2)` unflatten：`c0 = x//8 = row`，`c1 = (x//2)%4 = col//2`，`c2 = x%2 = col%2`；
2. `c0·4` 与 `c1·1` **都累加到 `@laneid` 轴**（同一物理轴出现两次，贡献相加），`c2·1` 累加到 `@reg` 轴；
3. 得到 `laneid = row·4 + col//2`，`reg = col%2`。

注意三处细节：

- **`@laneid` 出现了两次**——shape 的第一个分量（行）以 stride 4 贡献 laneid，第二个分量（列对）以 stride 1 贡献 laneid，两者相加才得到 lane 编号；
- `laneid` 取值 `0…31`（`row∈0..7`、`col//2∈0..3`，`4·row + col//2` 最大 `28+3=31`），`reg∈{0,1}`，组合数 `32×2=64` 恰等于元素总数——**双射成立**；
- 书给的手推样例：元素 43 在 `(row=5, col=3)`，`laneid = 5·4 + 3//2 = 21`，`reg = 3%2 = 1`，即「lane 21 拥有它、占用该 lane 的槽位 1」（[chapter_data_layout/index.md:L228-L229](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L228-L229)）。

#### 4.3.3 源码精读

**① fragment 的概念与公式。**[chapter_data_layout/index.md:L209-L219](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L209-L219) 给出：逻辑 `8×8` tile 的 64 个元素分布在 32 个 lane、每 lane 两个槽位，lane ID 不足以定位元素，物理位置由 (lane, 槽位) 两部分组成，随后写出 `laneid = row·4 + col//2`、`reg = col%2` 两条映射。

**② 交互演示的实现。**演示 [_extra/demo/thread_register.html](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/thread_register.html) 的标题就是这条布局（[L20](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/thread_register.html#L20)：`Layout: S[(8, 4, 2) : (4@laneid, 1@laneid, 1@reg)]`），其核心映射函数与书的公式逐字对应：

```javascript
// TileLayout(S[(8, 4, 2) : (4@laneid, 1@laneid, 1@reg)])
function threadLane(r, c) { return r * 4 + Math.floor(c / 2); }
function threadReg(r, c)  { return c % 2; }
```

这段代码（[thread_register.html:L59-L61](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/thread_register.html#L59-L61)）就是 `laneid = row·4 + col//2`、`reg = col%2` 的 JavaScript 直译。点击单元格后，公式条（[L163-L169](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/thread_register.html#L163-L169)）逐项展开 `addr = row×4@laneid + (col//2)×1@laneid + (col%2)×1@reg`，直观展示「同一轴两次贡献相加」；下方的线程视图表格（[L140-L151](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/thread_register.html#L140-L151)）则列出被选中 lane 的 reg 0 / reg 1 两个槽位各放的元素。图例（[L243](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/thread_register.html#L243)）注明 `laneid = threadIdx.x % 32`，把布局坐标与线程身份挂上钩。

**③ 记号在后续章节被直接复用。**这个 `8×8` atom 不是一次性示例：Tensor Core 三代演进章在讲 Ampere `mma.sync.m16n8k16` 的 C/D fragment 时再次引用同一条布局 `S[(8,4,2):(4@laneid,1@laneid,1@reg)]`，并说明「前两个坐标决定 lane ID，最后一个决定该 lane 内的 fragment 槽位」（[chapter_layout_generations/index.md:L119](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L119) 与 [L134](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L134)）。完整的 C/D fragment 沿 M 方向包含两个这样的局部模式——u5-l1 将展开精读。

**④ 向 warp 级扩展：`@warpid` 登场。**当 fragment 大到要跨多个 warp 时，坐标里还要加上「哪个 warp」。TIRx Layout API 章给出了同时使用 lane 轴与 warp 轴的例子（[chapter_tirx_layout_api/index.md:L52-L58](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L52-L58)）：

```python
frag = TileLayout(
    S[(8, 2, 4, 2) : (4@laneid, 1@warpid, 1@laneid, 1)]
)
```

这段代码定义了一个 128 元素（`8·2·4·2`）的 fragment：第一个与第三个 iter 都贡献 `laneid`，第二个 iter 贡献 `warpid`，最后一个 stride 无轴 tag、落在默认轴 `m` 上（[L60](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L60)）。书还特别说明（[L62](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L62)）：数据布局章用 `@reg` 区分 lane 本地槽位，而当前 TIRx API **不注册独立的 `reg` 轴**——布局挂到寄存器局部 buffer 上时，默认轴 `m` 就表示「该线程本地的线性位置」，buffer 的 scope 决定数据在寄存器里。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：手工推导并脚本验证两组 fragment 映射——warp 内的 `(8,4,2)` 布局（`@laneid` + `@reg`）与 warp 级的 `(8,2,4,2)` 布局（`@warpid` + `@laneid` + 默认轴 `m`）——把每个逻辑元素落到 `(warpid, laneid, 本地槽位)`。

**操作步骤**：

1. **手推**：先不看代码，对 `(8,4,2)` 布局手工填 6 个样本——元素 `x = 0, 1, 2, 8, 42, 43`（提示：先求 `(row, col)`，再套 `laneid = row·4 + col//2`、`reg = col%2`）。
2. **脚本全表**：运行下面的脚本（示例代码，接 4.1.4 的 `unflatten` / `named_apply`），打印 64 个元素的完整对照表并核对双射：

```python
# ---- warp 内 fragment: S[(8,4,2):(4@laneid,1@laneid,1@reg)] ----
shape, strides, axes = (8, 4, 2), (4, 1, 1), ("laneid", "laneid", "reg")
print("x    (row,col)  ->  laneid  reg")
for x in range(64):
    row, col = divmod(x, 8)
    pos = named_apply(x, shape, strides, axes)
    print(f"{x:3}  ({row},{col})    ->  {pos['laneid']:5}  {pos['reg']}")

seen = set()
for x in range(64):
    pos = named_apply(x, shape, strides, axes)
    seen.add((pos["laneid"], pos["reg"]))
print("unique (laneid,reg):", len(seen))          # 预期 64

# ---- warp 级 fragment: S[(8,2,4,2):(4@laneid,1@warpid,1@laneid,1)] ----
# 默认轴 m = lane 本地槽位
shape2, strides2, axes2 = (8, 2, 4, 2), (4, 1, 1, 1), ("laneid", "warpid", "laneid", "m")
print("x    ->  warpid  laneid  m")
for x in [0, 1, 2, 8, 16, 43, 127]:
    pos = named_apply(x, shape2, strides2, axes2)
    print(f"{x:3}  ->  {pos['warpid']:5}  {pos['laneid']:5}  {pos['m']}")

seen2 = set()
for x in range(128):
    pos = named_apply(x, shape2, strides2, axes2)
    seen2.add((pos["warpid"], pos["laneid"], pos["m"]))
print("unique (warpid,laneid,m):", len(seen2))    # 预期 128
```

3. **交互演示抽查**：按 u1-l2 的方式本地构建书站（`sphinx-build -b html . _build/html` 后用 `python -m http.server -d _build/html 8000` 预览），打开 Data Layout 章节的 Thread + Register 演示，点击元素 43、42、21，与脚本输出逐行核对。

**需要观察的现象**：

- `(8,4,2)` 表中，`x` 与 `x+1`（同行的相邻列）**大多落在同一个 lane**（`col//2` 相同），只有跨过偶数列边界时才换 lane；同一 lane 的两个元素恰是 `reg=0` 与 `reg=1`；
- `(8,2,4,2)` 表中，`x` 每加 2 换一个 lane（`laneid` 在 0–31 间滚动），`x` 加 8 翻转 `warpid`，`x` 的奇偶决定 `m`；
- 两张表的 unique 计数分别等于元素总数，说明布局是双射。

**预期结果**（由公式推导，待本地验证）：

| x | (8,4,2): (row,col) → laneid, reg | (8,2,4,2): warpid, laneid, m |
| --- | --- | --- |
| 0 | (0,0) → lane 0, reg 0 | warp 0, lane 0, m 0 |
| 1 | (0,1) → lane 0, reg 1 | warp 0, lane 0, m 1 |
| 2 | (0,2) → lane 1, reg 0 | warp 0, lane 1, m 0 |
| 8 | (1,0) → lane 4, reg 0 | warp 1, lane 0, m 0 |
| 16 | (2,0) → lane 8, reg 0 | warp 0, lane 4, m 0 |
| 42 | (5,2) → lane 21, reg 0 | warp 1, lane 9, m 0 |
| 43 | (5,3) → lane 21, reg 1 | warp 1, lane 9, m 1 |
| 127 | (15,7)* → lane 31, reg 1 | warp 1, lane 31, m 1 |

\* 注意 `x=127` 在 `8×8` 矩阵中不存在（该矩阵只有 0–63），此处 127 仅出现在 `(8,2,4,2)` 列；`(8,4,2)` 列的最后一行对应 `x=63 → (7,7) → lane 31, reg 1`。元素 43 的 `(lane 21, reg 1)` 与书中点击示例（[chapter_data_layout/index.md:L228-L229](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L228-L229)）一致；`(8,2,4,2)` 的手推依据是 `c=(x//16, (x//8)%2, (x//2)%4, x%2)` 与 `f_D(x)=(c0·4+c2)@laneid + c1@warpid + c3@m`（示例：`x=16 → c=(1,0,0,0) → laneid 4`；`x=8 → c=(0,1,0,0) → warpid 1`）。

#### 4.3.5 小练习与答案

**练习 1**：`S[(8,4,2):(4@laneid,1@laneid,1@reg)]` 中，lane 9 持有哪两个逻辑元素？各占哪个槽位？
**答案**：由 `laneid = row·4 + col//2 = 9` 得 `row = 9//4 = 2`、`col//2 = 1`，即 `col ∈ {2,3}`；因此 lane 9 持有 `(2,2)=18`（reg 0）与 `(2,3)=19`（reg 1）。

**练习 2**：把 `(8,2,4,2)` 布局的四个坐标分量与四根轴的对应关系写出来，并说明为什么 `laneid` 的取值恰好覆盖 0–31。
**答案**：`c0=x//16`（stride 4 → laneid）、`c1=(x//8)%2`（stride 1 → warpid）、`c2=(x//2)%4`（stride 1 → laneid）、`c3=x%2`（stride 1 → 默认轴 m）。`laneid = c0·4 + c2`，其中 `c0∈0..7`、`c2∈0..3`，`4·c0+c2` 取遍 0–31 且无重复（混合进制唯一分解），故覆盖全部 32 个 lane。

**练习 3**：数据布局章用 `@reg`，TIRx API 章却说不注册 `reg` 轴、用默认轴 `m` 代替。两者矛盾吗？
**答案**：不矛盾，是同一概念在不同抽象层的记法。数据布局章用 `@reg` 强调「lane 本地的 fragment 槽位」这一物理直觉；TIRx API 章说明当前 API 不注册独立的 `reg` 轴，当布局挂在寄存器局部 buffer 上时，默认线性轴 `m` 就表示该线程本地的线性位置，buffer 的 scope 决定数据在寄存器中（[chapter_tirx_layout_api/index.md:L62](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L62)）。读代码时见到无 tag 的 stride 落在 `m` 上，先看 buffer 的 scope 再判断它指全局内存还是线程本地槽位。

## 5. 综合实践

**任务**：把本讲三个布局统一进一个「迷你命名轴引擎」，像 TIRx API 那样用 `(extent, stride, axis)` 三元组（iter）定义布局（[chapter_tirx_layout_api/index.md:L118-L124](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L118-L124)），支持求值、全表打印与双射校验，然后用它复核本讲所有手推结果。

```python
class NamedLayout:                          # 示例代码
    def __init__(self, iters):              # iters: [(extent, stride, axis), ...]
        self.iters = iters

    def apply(self, x):                     # 前向映射: x -> 坐标字典
        rest, coords = x, []
        for extent, _, _ in reversed(self.iters):   # 从最低维开始分解
            coords.append(rest % extent)
            rest //= extent
        coords.reverse()
        out = {}
        for (_, stride, axis), c in zip(self.iters, coords):
            out[axis] = out.get(axis, 0) + c * stride  # 同轴多次出现则累加
        return out

    def check_bijective(self):              # 双射校验: 坐标组合数 == 元素总数
        total = 1
        for extent, _, _ in self.iters:
            total *= extent
        return len({tuple(self.apply(x).items()) for x in range(total)}) == total

layouts = {
    "row-major 8x16 (@m)":     NamedLayout([(8, 16, "m"), (16, 1, "m")]),
    "TMEM 128x256":            NamedLayout([(128, 1, "TLane"), (256, 1, "TCol")]),
    "fragment (8,4,2)":        NamedLayout([(8, 4, "laneid"), (4, 1, "laneid"), (2, 1, "reg")]),
    "warp frag (8,2,4,2)":     NamedLayout([(8, 4, "laneid"), (2, 1, "warpid"),
                                            (4, 1, "laneid"), (2, 1, "m")]),
}
for name, lay in layouts.items():
    print(f"{name:24} bijective: {lay.check_bijective()}")
print(layouts["TMEM 128x256"].apply(5 * 256 + 3))          # {'TLane': 5, 'TCol': 3}
print(layouts["fragment (8,4,2)"].apply(43))               # {'laneid': 21, 'reg': 1}
print(layouts["warp frag (8,2,4,2)"].apply(43))            # {'laneid': 9, 'warpid': 1, 'm': 1}
```

**要求**：

1. 四个布局的 `check_bijective` 应全部为 `True`（待本地验证）；
2. 三行 `apply` 输出应与 4.2.4 / 4.3.4 的手推值一致（TMEM 元素 1283 → `(TLane 5, TCol 3)`；元素 43 在 `(8,4,2)` 下 → `(laneid 21, reg 1)`，在 `(8,2,4,2)` 下 → `(warpid 1, laneid 9, m 1)`）；
3. 思考题：`apply` 是**前向映射**（逻辑 → 物理）。尝试为 `fragment (8,4,2)` 写反向映射 `inv({laneid, reg}) -> x`（提示：`row = laneid//4`，`col = 2·(laneid%4) + reg`），并用它验证 `apply` 与 `inv` 互逆——TMA 章与 GEMM 章的很多布局推理，本质上就是在做这个方向的反推。

## 6. 本讲小结

- **命名轴把布局函数推广为返回坐标字典**：\( f_D(x)=\sum_k c_k\,(s_k\ @\text{axis}_k) \)，shape/strides 规则不变，stride 归属到显式命名的物理轴；同一轴被多个 iter 引用时贡献相加。
- **线性地址只是一根轴的特例**：普通内存显式化后是 `@m`，行主序 `8×16` 即 `S[(8,16):(16@m,1@m)]`，两个分量投影到同一根轴上相加。
- **TMEM 是二维地址空间**：`@TLane`（0–127）× `@TCol`（最多 512，每列 32 bit），`S[(128,256):(1@TLane,1@TCol)]` 的 \( f_D \) 同时返回 `TLane` 与 `TCol`；**TMEM 的 Lane 是数据侧地址坐标，不是线程的 lane ID**。
- **寄存器 fragment 是跨线程的布局**：m8n8 atom 用 `@laneid` + `@reg`（lane 本地槽位），`S[(8,4,2):(4@laneid,1@laneid,1@reg)]` 给出 `laneid = row·4 + col//2`、`reg = col%2`；同一物理轴（laneid）出现两次是这类布局的常态。
- **warp 级 fragment 引入 `@warpid`**：`S[(8,2,4,2):(4@laneid,1@warpid,1@laneid,1)]` 用 2 个 warp × 32 lane × 2 槽位装下 128 个元素；当前 TIRx API 以默认轴 `m` 充当 lane 本地位置，`@reg` 只是教学记号。
- **双射性是自检工具**：元素总数必须等于各轴取值数的乘积，`32×2=64`、`2×32×2=128`——手推任何 fragment 布局后都应做这道检查。

## 7. 下一步学习建议

本讲让 \( f_D(x) \) 返回了「一组坐标」，但每个逻辑元素仍然只有**一个**物理位置。下一讲 **u4-l3「复制（Replication）与偏移（Offset）」** 处理两个方向的扩展：`R[shape:strides]` 描述同一逻辑元素出现在**多个**物理位置（例如 block-scaled MMA 的 scale factor 被 `.warpx4` 广播到 TMEM 四个 32-lane 分区），`O[...]`/固定 offset 表示坐标的固定平移。建议先读 [chapter_data_layout/index.md:L247-L353](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L247-L353)，思考热身问题：本讲的 `S[(32,…):(1@TLane,…)]` 只能把 SFA 摆进一个 32-lane 分区，四个分区的副本该用什么记号表达？之后 u4-l4 讲 swizzle（XOR 地址置换如何与仿射布局复合），u5 单元则把本讲的 `(8,4,2)` atom 放进 Ampere `m16n8k16` 的完整 fragment 映射，u7-l3（TMEM 章）会展开 warp 的 32-lane 访问窗口——届时 `TLane` 与 `laneid` 的关系将被彻底接通。
