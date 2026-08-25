# ComposeLayout 与 swizzle 变换

## 1. 本讲目标

前两讲（u10-l1、u10-l2）我们把 `TileLayout` 的三件套（`S[...]`、`R[...]`、offset）和命名轴前向映射拆解完毕。但 `TileLayout` 有一个边界：它是**仿射**的——物理坐标只能是"索引 × 步长"的乘加组合。而第四讲（u4-l4）讲过的 SMEM swizzle 靠 **XOR** 重排地址，XOR 不是乘加，塞不进 `S[...]`。本讲就解决"这个非仿射变换如何在 TIRx 里表达"。学完本讲你应该能够：

1. 复述 swizzle 为什么能消除 bank conflict：先用 `S[...]` 把逻辑元素映到默认轴 `m` 上的线性元素地址，再用一个位域置换把它打散，使列访问不再挤在同一组 bank。
2. 写出 `ComposeLayout` 的求值公式，说清三个**位**参数 `per_element`（M）、`swizzle_len`（B）、`atom_len`（S）各自的物理含义与约束 \( S \ge B \)。
3. 按数据类型与 SMEM swizzle 模式（32B/64B/128B）选择参数，并解释 fp16 的 128B swizzle 为何是 `(3, 3, 3)`、fp32 时哪个参数要变。
4. 手推 `(8, 64)` fp16 tile 在 128B swizzle 下的地址公式 \( \mathrm{addr} = 64i + 8(q \oplus i) + r \)，并核对列访问的 bank 分布 `0, 4, 8, …, 28`。

本讲不运行 GPU 内核，所有实践用纯 Python（标准库即可）复刻 `ComposeLayout` 的求值；装了 `apache-tvm` 或打开书站交互演示的读者可以做对拍。

## 2. 前置知识

本讲站在两份前置讲义的肩膀上，只回温要点：

- **u4-l4（swizzle 的硬件版）**：SMEM 分 32 个 bank，字节地址 `addr` 的 bank 是 \( \text{bank} = \lfloor \text{addr}/4 \rfloor \bmod 32 \)；同一 wavefront 内落到同 bank 不同地址的访问被串行化，冲突重数就是额外周期数。行主序布局"利行伤列"：行读地址连续自然散开，列读的地址间隔等于行距，容易反复命中同一组 bank。8×8 演示的解法是 \( \text{mapped\_col} = c \oplus r \)，XOR 的双射性同时保住行读与列读，自反性（\( \oplus \) 两次还原）让读写两端共用同一条公式。
- **u10-l1 / u10-l2（TileLayout 与前向映射）**：`TileLayout(S[shape : strides] + R[...] + offset)`；`apply()` 四步求值——行主序展平、按 shard extents 切分、逐分量按"步长@轴"记账、叠加 offset；默认轴 `m` 是线性物理轴，buffer 的 scope 决定它落在哪种存储上。

两个本讲要新用的概念：

- **仿射（affine）**：输出可以写成"输入的常数倍之和再加常数"。\( 64i + j \) 是仿射；\( q \oplus i \) 不是——XOR 按位异或，无法写成乘加。这就是 swizzle 必须放在 `S[...]` 之外的原因。
- **位域（bit field）**：一个整数的某一段连续二进制位。swizzle 公式操作的是线性元素地址的三个位域：低位保留域、XOR 目标域、XOR 源域。

一个命名提醒（u10-l2 讲过）：逻辑坐标 \( (i, j) \) 是 tile 的行列号，与默认物理轴 `m` 只是重名；本讲的公式里 \( m \) 特指"`@m` 轴上的线性元素地址"。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [chapter_tirx_layout_api/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md) | 本讲主源码：`ComposeLayout` 定义、Why Swizzle、The Swizzle Transform、Choosing Swizzle Parameters、`(8,64)` fp16 完整算例 |
| [chapter_data_layout/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md) | u4-l4 对应章节：bank 公式、wavefront、XOR 规则、sector/atom、模式选择规则与"非仿射、组合表达"的结论 |
| [chapter_intro_tirx/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md) | `hgemm_v1` 内核：`mma_shared_layout` 的真实用法，佐证"内核不手推 swizzle 参数" |
| [static/tirx-layout-demo/layout-demo.js](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/static/tirx-layout-demo/layout-demo.js) | 章首交互演示的脚本：用 JavaScript 复刻了 TVM `compose_layout.cc` 的 `ComposeLayoutNode::Apply`，并实现"dtype+模式 → 三参数"的解析 |
| [img/scripts/gen_swizzle_conflict.py](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_swizzle_conflict.py) | 生成"行写无冲突 / 列读 8-way 冲突"示意图的 matplotlib 脚本 |

## 4. 核心概念与源码讲解

### 4.1 为什么 swizzle：一个 tile、两个读取方向、一套地址

#### 4.1.1 概念说明

GEMM 内核里的 SMEM tile 有两个"顾客"：写入端（TMA 或线程拷贝）希望一行一行连续写；读取端（MMA 的矩阵描述符、`ldmatrix`）常常要按列取数据。任何**单一**简单布局只能讨好一方——行主序让行访问天然跨 bank，却让列访问以行距为间隔反复命中同一组 bank；列主序正好反过来。

swizzle 的思路不是换布局，而是**在保持逻辑形状不变的前提下重排物理地址**：让地址的低位依赖行号的高位，使一列元素按行推进时被推到不同 bank 上。在 API 层面，这件事分两步：

1. 一个仿射的 `TileLayout` 先把逻辑元素 \( (i,j) \) 映到默认轴 `m` 上的线性元素地址；
2. 一个 XOR 位域置换再重排这个地址。

第一步你已经会了；本讲的主角是第二步，以及把两步打包的 `ComposeLayout`。

#### 4.1.2 核心流程

判断"要不要 swizzle、swizzle 救了谁"的流程：

1. 写出 tile 的仿射布局，求出每个逻辑元素的线性元素地址 \( m \)（字节地址 = \( m \times \) 元素宽度）。
2. 枚举你关心的访问模式（一行？一列？一个 16B 向量？），对每个访问算 \( \text{bank} = \lfloor \text{addr}/4 \rfloor \bmod 32 \)。
3. 若某个模式把多个不同地址压进同一 bank，数冲突重数——这就是被串行化的周期数。
4. 选择 swizzle 参数让该模式的 bank 尽量互异，同时不破坏另一个模式的连续性。

#### 4.1.3 源码精读

Layout API 章用一个行主序 `(8, 64)` float16 tile 引出问题：

[chapter_tirx_layout_api/index.md:396-410](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L396-L410)

这段说：`TileLayout(S[(8, 64) : (64@m, 1@m)])` 下元素 \( (i,j) \) 的线性元素地址是 \( m = 64i + j \)；每行 64 个 fp16 = 128 字节；于是一组线程若读**同一列** \( j \) 的不同行，相邻地址相差 128 字节，"可能反复落进同一组 bank"——这正是 u4-l4 手推过的 8-way 冲突。最后一句给出 swizzle 的定义：让地址低位依赖行号高位，把列访问打散。

bank 公式与 wavefront 的原文在数据布局章（u4-l4 已精读，这里只引用回温）：

[chapter_data_layout/index.md:434-451](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L434-L451)

要点：\( \text{bank} = (\text{addr} \div 4) \bmod 32 \)；冲突只在同一 wavefront 内评估（16B 访问一个 wavefront 8 个 lane），同地址是广播不算冲突。

8×8 演示对应的 XOR 规则原文：

[chapter_data_layout/index.md:473-482](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L473-L482)

仓库里还有一张配套示意图的生成脚本，它的说明文字把矛盾一句话讲透：

[img/scripts/gen_swizzle_conflict.py:1-5](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_swizzle_conflict.py#L1-L5) —— 脚本自述：中间是按 bank group（=列号）着色的 8×8 行主序 tile，左边是合并写的一行（8 个不同 bank，无冲突），右边是 ldmatrix 的列读（全在一个 bank，8-way 冲突）。

[img/scripts/gen_swizzle_conflict.py:76-78](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_swizzle_conflict.py#L76-L78) —— 图注结论："一行横跨 8 个 bank group（互异）；一列只有一个 bank group（×8）。swizzle 把列 c 存到 \( c \oplus r \)，让两者都互异。"

#### 4.1.4 代码实践

**实践目标**：不靠想象，用脚本量化"行读无冲突、列读 8-way 冲突"。

**操作步骤**（示例代码，纯标准库）：

```python
# 示例代码：统计 (8,64) fp16 行主序 tile 的 bank 分布
ROWS, COLS, ELEM_BYTES = 8, 64, 2

def bank(elem_addr):                 # fp16: 字节地址 = 2*addr
    return (elem_addr * ELEM_BYTES // 4) % 32

row = [bank(64 * 0 + j) for j in range(COLS)]        # 读第 0 行
col = [bank(64 * i + 5) for i in range(ROWS)]        # 读第 5 列
print("row banks:", sorted(set(row)), "distinct =", len(set(row)))
print("col banks:", sorted(set(col)), "distinct =", len(set(col)))
```

**需要观察的现象**：行读的 bank 互异（无冲突）；列读 8 个地址的 bank 全部相同。

**预期结果**：`row` 一行 64 个 fp16 共 128 字节，恰好扫过全部 32 个 bank（每个 bank 命中两个元素）；`col` 的 8 个地址全部落在 **bank 2**，因为 \( \lfloor (64i+5) \times 2 / 4 \rfloor \bmod 32 = (32i + 2) \bmod 32 = 2 \)——列地址间隔 128 字节恰是 bank 周期（32 bank × 4B）的整数倍，模 32 后余数不再变化。具体数值以本地运行输出为准（待本地验证），但"行读 32 个 bank 互异、列读只剩 1 个 bank"这一结论与书中的推导一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么说"行主序利行伤列"是布局的必然，而不是实现失误？

**答案**：bank 由字节地址模 32 决定。行主序下行内相邻元素地址差 1，必然扫过不同 bank；列内相邻元素地址差等于整行字节数（本例 128B），是 128B = 32 bank × 4B 的整数倍，模 32 后余数不变，于是整列映射到同一个 bank。任何单一仿射布局的行距是常数，都无法同时让"行差 1"和"列差非 bank 周期整数倍"——这正是 u4-l4 说"简单布局只能讨好一方"的原因。

**练习 2**：同 bank 的两次访问一定会串行化吗？

**答案**：不一定。按数据布局章 L441-L451，需同时满足"同一 wavefront"与"不同地址"：同一 wavefront 内同 bank 不同地址才冲突；同地址会广播；不同 wavefront（如 16B 访问的 lane 0 与 lane 8 分属不同 wavefront）之间不比较。

### 4.2 XOR swizzle 变换：三个位宽参数

#### 4.2.1 概念说明

XOR 置换无法写进 `S[...]`（它非仿射），所以 TIRx 把它做成一个**独立的地址变换**，与仿射布局组合。`ComposeLayout` 携带三个整数参数，注意它们都是**位数**，不是字节数：

| 参数 | 记号 | 含义 |
|---|---|---|
| `per_element` | M | 地址低 M 位保持不变——保住一个向量组（如 16B sector 内的元素）的连续性 |
| `swizzle_len` | B | 参与 XOR 的位域宽度——目标位域是 \( [0, B) \)，共 \( 2^B \) 个"槽"被置换 |
| `atom_len` | S | 两个位域的距离——源位域是 \( [S, S+B) \)，即从高 S 位取键 XOR 下来 |

合法约束是 \( S \ge B \)：源位域与目标位域不重叠。此时变换是**对合**（再做一次 XOR 回到原值），读写两端可以共用同一条公式——这与 u4-l4 讲的自反性是同一件事。`swizzle_inner=True` 是常规方向（高位 XOR 进低位）；`False` 镜像方向（低位 XOR 进高位）。

#### 4.2.2 核心流程

`ComposeLayout` 的求值是严格的"先仿射、后置换"两步：

```text
逻辑坐标 (i, j, ...)
   │  tile_layout.apply()          —— 仿射，唯一物理轴必须是 @m
   ▼
线性元素地址 m
   │  ① low = m & ((1<<M)-1)       —— 保留低 M 位
   │  ② x   = m >> M               —— 腾出战场
   │  ③ x2  = x ^ ((x >> S) & ((1<<B)-1))
   │  ④ addr = (x2 << M) | low     —— 拼回低位
   ▼
重排后的线性元素地址 addr（仍是同一逻辑元素，物理位置变了）
```

注意第 ③ 步：XOR 的值 \( (x \gg S) \,\&\, \text{mask} \) 本身小于 \( 2^B \)，所以它只改写 \( x \) 的低 B 位——那正是目标位域；\( x \) 的高位原样保留。这就是"用高位当键、置换低位槽"的按位实现。

#### 4.2.3 源码精读

`ComposeLayout` 的定义段：

[chapter_tirx_layout_api/index.md:374-390](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L374-L390)

这段给了三条规定：①传入的 `tile_layout` 必须只产 `@m` 轴上的线性地址（求值时它先算地址，swizzle 参数再重排）；②`swizzle_inner=True` 是下文描述的常规方向，`False` 是镜像；③一个"裸 swizzle"可以用"只覆盖一个 swizzle 周期的恒等 TileLayout"组合出来。

三个参数与完整公式：

[chapter_tirx_layout_api/index.md:412-442](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L412-L442)

原文强调了三件事：三个参数都是位数不是字节数（L422）；M 保留低位使"一小群相邻元素保持连续"，B 是参与 XOR 的位宽，S 是两个位域的距离（L424）；合法 swizzle 要求 \( S \ge B \)（L442）。并且"变换不改变 tile 里有哪些逻辑元素，只改它们在 SMEM 里的物理地址；后续 MMA 读的还是同一个逻辑 tile，只是 bank 访问模式不同"（L444）。

章首交互演示的脚本忠实复刻了 TVM C++ 实现（注释里写明 mirror `src/tirx/ir/layout/compose_layout.cc` 的 `ComposeLayoutNode::Apply`）：

[static/tirx-layout-demo/layout-demo.js:322-333](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/static/tirx-layout-demo/layout-demo.js#L322-L333)

对照读：`base = 1 << per_element`、`innerMask = (1 << swizzle_len) - 1`、`outerMask = innerMask << atom_len`；inner 方向 `x ^ ((x & outerMask) >> atom_len)`，outer（即 `swizzle_inner=False`）方向 `x ^ ((x & innerMask) << atom_len)`——与章中公式逐项对应，只是把"先移位后取 mask"写成了"先取 mask 后移位"。

演示还实现了"swizzle 只适用于唯一物理轴是 `@m` 的布局"这条守卫：

[static/tirx-layout-demo/layout-demo.js:341-349](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/static/tirx-layout-demo/layout-demo.js#L341-L349)

理由在注释里：XOR 置换的是**线性 SMEM 地址**；若布局已经把元素散到 `laneid`/`TLane` 等存储轴上，就没有"线性地址"可置换，bank 视图也无意义。

现在手推 `(8, 64)` fp16 + 128B swizzle（\( M=B=S=3 \)）的闭式解。设 \( j = 8q + r \)（\( q \) 是 16B 向量编号、\( r \) 是向量内槽位），则 \( m = 64i + 8q + r \)：

\[ \text{low} = r,\quad x = 8i + q,\quad (x \gg 3) = i,\quad x_2 = 8i + (q \oplus i) \]

\[ \boxed{\ \mathrm{addr} = 64i + 8\,(q \oplus i) + r\ } \]

第 ③ 步能写成 \( 8i + (q \oplus i) \) 是因为键 \( i \)（3 位）只影响 \( x \) 的低 3 位，而那 3 位恰好是 \( q \)，\( 8i \) 的低 3 位全零。这正是书中算例的公式：

[chapter_tirx_layout_api/index.md:486-509](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L486-L509)

取第 0 列（\( q=r=0 \)）得 \( \mathrm{addr} = 8(i \oplus 0) + 64i = 72i \)。fp16 的 bank 是 \( \lfloor \mathrm{addr}/2 \rfloor \bmod 32 \)，于是 \( \lfloor 72i/2 \rfloor \bmod 32 = 36i \bmod 32 = 4i \)：

[chapter_tirx_layout_api/index.md:513-537](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L513-L537)

书中的表：8 行分别落 bank 0, 4, 8, 12, 16, 20, 24, 28——8 个互异 bank，冲突消失；而不 swizzle 时同一列 \( m = 64i \)、\( \lfloor 64i/2 \rfloor \bmod 32 = 0 \)，8 行全挤 bank 0。

#### 4.2.4 代码实践

**实践目标**：把上面五步公式实现成函数，复现书中 bank 表。

**操作步骤**（示例代码）：

```python
# 示例代码：实现 ComposeLayout 的 swizzle 变换
def swizzle(m, per_element, swizzle_len, atom_len, inner=True):
    mask = (1 << swizzle_len) - 1
    low = m & ((1 << per_element) - 1)
    x = m >> per_element
    x2 = x ^ ((x >> atom_len) & mask) if inner else x ^ ((x & mask) << atom_len)
    return (x2 << per_element) | low

ROWS, COLS = 8, 64
for i in range(ROWS):                       # 第 0 列，128B swizzle: M=B=S=3
    m = 64 * i
    a = swizzle(m, 3, 3, 3)
    print(f"i={i}  m={m:3d}  addr={a:3d}  bank={(a * 2 // 4) % 32}")
```

**需要观察的现象**：`addr` 依次是 0, 72, 144, …；bank 依次 0, 4, 8, …, 28。

**预期结果**：与书中 L521-L529 的表逐行一致；再打印 `swizzle(m,3,3,3)` 作用两次的结果应等于 `m`（对合性，因为 \( S \ge B \) 两个位域不重叠）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `per_element` 要保留低 M 位？若 M=0 会怎样？

**答案**：低 M 位对应"一个向量组内的元素"（fp16 的 16B sector 含 8 个元素，M=3）。保留它们意味着 swizzle 只搬运**整个向量组**，组内相对顺序不动。若 M=0，XOR 会打散组内元素：TMA/`ldmatrix` 的 16B 连续读写被拆到不连续地址，连续访问本身反而制造冲突，且下游按向量取数的指令无法工作。

**练习 2**：为什么要求 \( S \ge B \)？

**答案**：目标位域是 \( [0,B) \)、源位域是 \( [S,S+B) \)。\( S \ge B \) 保证两域不重叠，于是 XOR 键 \( (x \gg S)\&\text{mask} \) 不受本变换影响——对同一个地址再做一次同样的 XOR 就还原（对合）。这让写端（TMA 写入）与读端（MMA 描述符）使用**同一条**地址公式。若 \( S < B \) 两域重叠，变换仍是一一映射但不再是自反的，读写两端要各维护一套互逆公式，极易出错。

**练习 3**：用闭式解说明第 0 行（\( i=0 \)）的元素地址完全不变。

**答案**：\( \mathrm{addr} = 64\cdot 0 + 8(q \oplus 0) + r = 8q + r = m \)。XOR 键为 0 时置换是恒等——所以 swizzle 后每个 atom 的"第一行"充当了未置换的基准行。

### 4.3 swizzle 参数选择：数据类型 × 模式

#### 4.3.1 概念说明

三个参数不该由人手推。书中的态度很明确（L470）：**数据类型与描述符模式（descriptor mode）通常直接决定配置**，内核作者的责任只有一个——让 TIRx 布局、TMA 描述符、MMA 指令对 SMEM 物理排布的描述保持一致。

参数的来源可以归纳成三条规则（与演示脚本 `computeSwizzle` 的解析逻辑一致）：

1. \( M = \log_2(\text{一个 16B 向量里的元素个数}) = \log_2(16/\text{元素字节}) \)。fp16 → \( \log_2 8 = 3 \)；fp32 → \( \log_2 4 = 2 \)；fp8 → \( \log_2 16 = 4 \)。
2. \( B = \log_2(\text{swizzle 行宽}/16\text{B}) \)，即一行内 16B sector 的个数取对数：SWIZZLE_128B → 3、64B → 2、32B → 1。
3. \( S = 3 \) 对应 SWIZZLE_128B atom 的固定几何（atom 是 8 行 × 128B 的 1KB 块；以 16B sector 计是 8×8，行距恰好贡献 3 个地址位）。64B/32B 模式的 atom 更窄，XOR 键按 `row//2`、`row//4` 共享（u6-l2 讲过）。

模式本身怎么选？数据布局章给了实用规则：**选 tile 的连续维能支撑的最大行宽**——atom 行宽 N 字节要求连续维至少 N 字节、最好被它整除；连续维 ≥ 128B（64 个 fp16）首选 SWIZZLE_128B，否则退到 64B 或 32B。

#### 4.3.2 核心流程

给定一个 SMEM tile，确定 swizzle 配置的流程：

```text
元素位宽 bits
   → per_element M = log2(128 / bits)          （128B swizzle 下的向量组）
tile 连续维字节数 W = cols × bits / 8
   → swizzle 模式 = 最大的 N ∈ {128,64,32} 满足 W ≥ N 且 N | W（尽量整除）
   → swizzle_len B = log2(N / 16)
   → atom_len S = 3（128B 族 atom 的行几何）
   → ComposeLayout(per_element=M, swizzle_len=B, atom_len=S, tile_layout=行主序@m)
```

最后一步是自检：用 4.2 的脚本枚举你关心的访问模式，确认 bank 互异。

#### 4.3.3 源码精读

"选择参数"一节的原文：

[chapter_tirx_layout_api/index.md:446-470](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L446-L470)

书中给出的推导：fp16 下 16B 向量含 8 个元素，\( M = \log_2 8 = 3 \)；128B swizzle 用 `(per_element=3, swizzle_len=3, atom_len=3)`；"128 字节是 swizzle atom 中一行的宽度，完整 atom 含 8 行；这组参数保住每个连续 16B 向量组、同时置换更高的地址位，把列访问摊到不同 bank"。最后一句是纪律："内核代码通常不应手工推导这些参数……唯一的要求是 TIRx 布局、TMA 描述符与 MMA 对 SMEM 排布达成一致。"

挂到 SMEM 分配上的完整写法：

[chapter_tirx_layout_api/index.md:472-484](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L472-L484)

真实内核确实不手推参数——`hgemm_v1` 从 `tma_utils` 的辅助函数拿布局：

[chapter_intro_tirx/index.md:72-93](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L72-L93)

`A_layout = mma_shared_layout(a_type, SwizzleMode.SWIZZLE_128B_ATOM, (BLK_M, BLK_K))`——输入恰好就是 4.3.2 流程的前两步：数据类型 + swizzle 模式 + tile 形状。随后布局随分配挂到 buffer 上：

[chapter_intro_tirx/index.md:109-116](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L109-L116)

`pool.alloc((BLK_M, BLK_K), a_type, layout=A_layout)`——本讲 4.4 会看到这正是 `ComposeLayout` 的挂载点。

演示脚本把"dtype+模式 → 参数"的解析写成了代码（注释注明 mirror `tma_utils.mma_shared_layout`）：

[static/tirx-layout-demo/layout-demo.js:335-376](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/static/tirx-layout-demo/layout-demo.js#L335-L376)

对照三行关键代码：`SWIZZLE_LEN = { none: 0, '32': 1, '64': 2, '128': 3 }`（B 的来源）；`per_element = (128 / bits).bit_length() - 1`（M 的来源，如 fp16 → 3、fp32 → 2）；`atom_len = 3, inner = true`（S 固定 3、常规方向）。

模式选择规则的原文（数据布局章）：

[chapter_data_layout/index.md:545-551](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L545-L551)

#### 4.3.4 代码实践

**实践目标**：造一张"数据类型 × 模式 → 三参数"的速查表，并与演示脚本的公式对拍。

**操作步骤**（示例代码）：

```python
# 示例代码：按演示脚本的规则生成参数表
def params(bits, mode_bytes):
    return dict(
        per_element=(128 // bits).bit_length() - 1,   # M
        swizzle_len=int(mode_bytes // 16).bit_length() - 1,  # B = log2(sectors)
        atom_len=3,                                   # S（128B 族 atom 行几何）
    )

for name, bits in [("fp8", 8), ("fp16", 16), ("fp32", 32)]:
    for mode in (32, 64, 128):
        print(f"{name:4s} SWIZZLE_{mode}B ->", params(bits, mode))
```

**需要观察的现象**：`per_element` 只随位宽变（fp8=4、fp16=3、fp32=2）；`swizzle_len` 只随模式变（32B=1、64B=2、128B=3）；`atom_len` 恒为 3。

**预期结果**：fp16 + 128B 得 `(3,3,3)`，与书中的 `ComposeLayout` 调用一致；fp32 + 128B 得 `(2,3,3)`——这正是综合实践第二问的答案骨架。

#### 4.3.5 小练习与答案

**练习 1**：一个 `(8, 32)` fp16 tile（连续维 32 元素 = 64 字节）应选什么模式与参数？

**答案**：连续维 64B 只能支撑 64B 行宽，选 SWIZZLE_64B；参数为 `per_element=3`（仍是 fp16 的 8 元素向量）、`swizzle_len=2`（64B/16B = 4 个 sector）、`atom_len=3`。注意行宽变窄后 XOR 只置换 4 个槽，消除冲突的能力相应减弱——这是"连续维不足 128B 就退档"的代价。

**练习 2**：把 `(8, 64)` 的元素从 fp16 换成 fp32，除了 `per_element` 从 3 变 2，还有什么隐患？

**答案**：行宽从 128B 变成 256B——**一行跨两个 swizzle atom**。此时线性地址里"atom 内行号"与"行内 atom 编号"的位混在一起：\( x = m \gg 2 = 16i + q' \)（\( q' \in [0,16) \) 是行内 16B 向量编号），XOR 源位域 \( [3,6) \) 取到的是 \( (2i + \lfloor q'/8 \rfloor) \bmod 8 \)，行号只贡献 2 个位。手推第 0 列（\( q'=0 \)）得 \( \mathrm{addr} = 64i + 4\,(2i \bmod 8) \)，8 行的 bank 依次为 0, 8, 16, 24, 0, 8, 16, 24——只剩 4 个互异 bank，2-way 冲突（未 swizzle 时是 8-way，仍是改善但没除净）。修法是把连续维限制到一个 atom 宽：把 tile 看成 `(8, 2, 32)`（最内维 32 个 fp32 = 128B），此时 \( x = 8i + q' \)、XOR 源就是完整的行号 \( i \)，第 0 列地址 \( 36i \)、bank 回到 0, 4, 8, …, 28。这正是 u6-l1/u6-l2 讲过的"SWIZZLE_128B 下 box 最内连续维不得超过 128 字节"在布局侧的镜像。此手推表请以综合实践的脚本输出为准（待本地验证）。

### 4.4 ComposeLayout：把两步装进一个布局对象

#### 4.4.1 概念说明

`ComposeLayout` 的角色是**打包与传播**：它把"仿射映射 + XOR 置换"封装成一个布局对象，挂到 SMEM buffer 上，此后**所有**访问这个 tile 的操作——TMA 写入、MMA 矩阵描述符读取、epilogue 回写——都从同一个对象得到同一套地址计算。没有它，每个访问点都要各自手写一遍 XOR（Ampere 时代的做法，u5-l1 讲过），任何一处漏写或写错模式，数据就"字节到了、元素认错"。

两个使用要点：①被组合的 `tile_layout` 必须只产 `@m` 线性地址（有 `laneid`/`TLane` 等存储轴的布局没有可置换的线性地址）；②`swizzle_inner=False` 提供镜像方向，日常用 `True`。

#### 4.4.2 核心流程

一个 swizzled SMEM buffer 的完整求值链：

```text
逻辑元素 (i, j)
   │ TileLayout(S[(8,64):(64@m,1@m)])        仿射：m = 64i + j
   ▼ 线性元素地址 m
   │ XOR 位域置换 (M, B, S)                   非仿射：addr = f(m)
   ▼ 物理元素地址 addr
   │ × 元素宽度、÷ 4、mod 32                  bank = ⌊addr·w/4⌋ mod 32
   ▼ SMEM bank
```

配套的一致性纪律（数据布局章 L563-L566 原文）：**访问同一 tile 的每个操作必须使用同一 swizzle 模式**；组合布局负责真正的地址变换；不同硬件单元的 swizzle 要求不同，且随 GPU 代际变化。

#### 4.4.3 源码精读

章首总览对 `ComposeLayout` 的一句话定位：

[chapter_tirx_layout_api/index.md:4-10](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L4-L10)

正文把它与 `TileLayout` 并列成两件套：`TileLayout` 用 `S`/`R`/offset 描述逻辑 tile 在命名轴上的摆放、`apply()` 算基础坐标；`ComposeLayout` 把仿射映射与 XOR 置换组合起来。

"XOR 不是仿射、所以要组合"的原文（数据布局章结尾）：

[chapter_data_layout/index.md:558-566](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L558-L566)

两步视图（`S[...]` 先映到 `@m`，swizzle 再重排）、"XOR 置换不是仿射，故不属于仿射布局本身，而是与之组合的独立地址变换"、以及"同一 tile 的所有操作必须同一 swizzle 模式"三条，都是这里的原话。

Layout API 章的完整三段示例——行主序 tile、`ComposeLayout` 包装、挂到 buffer：

[chapter_tirx_layout_api/index.md:456-484](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L456-L484)

第一段是书中原始调用（L459-L466），第二段把组合后的 `layout` 落到 SMEM 分配上（L472-L484）：`layout = ComposeLayout(per_element=3, swizzle_len=3, atom_len=3, tile_layout=tile)`，"组合后的 layout 挂到 shared-memory buffer 上"。与 `hgemm_v1` 的 `pool.alloc(..., layout=A_layout)` 对照（u9-l1 精读过），`A_layout` 正是这样一个对象——只不过由 `mma_shared_layout` 代劳生成。

章末总结把整章 API 收拢成一段话，其中对 swizzle 的收束是：

[chapter_tirx_layout_api/index.md:542](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L542)

tcgen05 相关布局用 `tmem_datapath_layout`、`tcgen05_atom_layout` 等构造器；其余仿射布局用 `S[...]`、`R[...]` 与 offset；SMEM swizzle 用 `ComposeLayout(per_element, swizzle_len, atom_len, tile_layout)` 套在一个产线性 `m` 地址的 tile 布局上——本讲的全部内容就是这最后半句的展开。

另外，章首交互演示直接接受 `ComposeLayout(...)` 表达式作为输入（解析器在 [static/tirx-layout-demo/layout-demo.js:164-214](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/static/tirx-layout-demo/layout-demo.js#L164-L214)），也可以用 dtype+模式下拉框自动解析参数（4.3.3 的 `computeSwizzle`），两条路都能点选逻辑元素查看重排后的物理地址与 bank。

#### 4.4.4 代码实践

**实践目标**：写出一个 swizzled SMEM buffer 的完整声明（阅读型实践），并在演示里验证。

**操作步骤**：

1. 阅读并抄录 [chapter_tirx_layout_api/index.md:472-484](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L472-L484) 的两段代码，标注：`tile` 只产 `@m` 地址；`ComposeLayout` 的三个参数来自 4.3 的规则；组合结果通过 `layout=` 挂到分配上。
2. 打开书站构建产物（或本地 `python -m http.server -d _build/html 8000` 后访问）中的 TIRx layout demo，在布局输入框填入 `ComposeLayout(per_element=3, swizzle_len=3, atom_len=3, tile_layout=TileLayout(S[(8,64):(64@m,1@m)]))`，点选 `(1, 5)` 与 `(7, 40)` 两个逻辑元素。
3. 同时用 4.2.4 的 `swizzle()` 函数手算这两个元素的 `addr`。

**需要观察的现象**：演示显示的物理坐标（`m` 轴取值）与脚本输出一致；行 0 的元素地址与未 swizzle 相同。

**预期结果**：\( (1,5) \)：\( m=69 \)、\( q=0,r=5 \)、addr \( = 64+8+5=77 \)（差 +8）；\( (7,40) \)：\( m=488 \)、\( q=5,r=0 \)、\( 5 \oplus 7 = 2 \)、addr \( = 448+16=464 \)（差 −24）。地址差都是 8 的倍数（向量级搬运），且可正可负。演示中的具体显示以本地打开为准（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `ComposeLayout` 要求 `tile_layout` 只产 `@m` 轴的地址？

**答案**：XOR 置换的对象是一个**线性地址**的位。若布局已经把元素分布到 `laneid`、`TLane` 等存储轴，坐标是一个字典而非一个整数，没有"位"可言，置换无从下手；而且 bank 冲突只对 SMEM 的线性地址空间有意义（演示 `swizzleApplies` 的注释，L341-L349）。所以 swizzle 永远是"纯内存布局"上的最后一层。

**练习 2**：如果 TMA 按某模式写入 SMEM，而 MMA 描述符声明了另一个 swizzle 模式，会发生什么？

**答案**：不会有任何报错——字节都会到齐，但 MMA 按**它自己声明的**排布去解释这些字节，逻辑元素与物理位置错位，结果数值错误。这就是 u5-l2 讲过的"描述符必须与 SMEM 实际字节一致"、u6-l1 讲过的"tensor map、SMEM 布局与 MMA 指令必须描述同一物理排布"。`ComposeLayout` 挂在 buffer 上让三个访问点共享同一个对象，正是消除这类错配的机制。

## 5. 综合实践

把本讲四块内容串成一个可运行的完整任务（纯 Python，无需 GPU）：

**任务**：对 `(8, 64)` fp16 tile 应用 128B swizzle，打印 swizzle 前后同一逻辑元素的线性地址差；然后把元素换成 fp32，回答参数如何调整并验证效果。

```python
# 示例代码：综合实践（完整脚本，纯标准库）
def swizzle(m, per_element, swizzle_len, atom_len, inner=True):
    mask = (1 << swizzle_len) - 1
    low = m & ((1 << per_element) - 1)
    x = m >> per_element
    x2 = x ^ ((x >> atom_len) & mask) if inner else x ^ ((x & mask) << atom_len)
    return (x2 << per_element) | low

def report(name, rows, cols, elem_bytes, M, B, S):
    print(f"--- {name}: ({rows},{cols}) {elem_bytes}B, per_element={M} swizzle_len={B} atom_len={S}")
    print("采样元素的地址差（delta 为 8/4 的倍数表示按向量组搬运）:")
    for (i, j) in [(0, 9), (1, 5), (2, 0), (7, 40)]:
        m, a = cols * i + j, swizzle(cols * i + j, M, B, S)
        print(f"  (i={i},j={j:2d}) m={m:3d} addr={a:3d} delta={a-m:+3d}")
    for col in (0, 8):                     # 看两列的 bank 分布
        banks = [swizzle(cols * i + col, M, B, S) * elem_bytes // 4 % 32 for i in range(rows)]
        raw   = [(cols * i + col) * elem_bytes // 4 % 32 for i in range(rows)]
        print(f"  第 {col} 列 bank：未 swizzle {sorted(set(raw))} ({len(set(raw))} 个) "
              f"-> swizzle 后 {sorted(set(banks))} ({len(set(banks))} 个)")

report("fp16 + SWIZZLE_128B", 8, 64, 2, 3, 3, 3)   # 书中算例
report("fp32 + SWIZZLE_128B", 8, 64, 4, 2, 3, 3)   # 一行跨两个 atom
report("fp32 + SWIZZLE_128B, 连续维切到 32", 8, 32, 4, 2, 3, 3)
```

**要回答的两个问题与预期观察**：

1. **fp16 部分**：`(1,5)` 的 delta 是 +8、`(7,40)` 是 −24、`(0,9)` 是 0；第 0 列 bank 从"未 swizzle 全挤 1 个"变为 `{0,4,8,…,28}` 共 8 个——与书中 L507-L537 的算例逐项一致（这部分有书的表格背书）。
2. **fp32 部分**：`per_element` 必须从 3 改成 2，理由是 16B 向量里只有 4 个 fp32 元素（\( M=\log_2(16/4)=2 \)），`swizzle_len` 与 `atom_len` 在 128B 模式下保持 3 与 3。但 `(8,64)` fp32 一行 256B 跨两个 atom，第 0 列预期只剩 4 个互异 bank（2-way 冲突）；把连续维切到 32 个元素（128B，即 `(8, 2, 32)` 视图）后预期恢复 8 个互异 bank。后两张表是按章中公式手推的结果，请以脚本实际输出为准（待本地验证）。

**扩展（可选）**：若安装了 `apache-tvm==0.26.0`，可按 u10-l1 的方式构造真实 `TileLayout` 与 `ComposeLayout` 对象对拍；或在书站交互演示中把 dtype 切到 fp32、模式切到 128B，点选同一列的 8 个元素直接观察 bank 视图。

## 6. 本讲小结

- swizzle 解决"一个 tile、两个读取方向"的两难：先用仿射 `TileLayout` 求出 `@m` 轴的线性元素地址，再用 XOR 位域置换重排它，逻辑元素集合不变、bank 分布改变。
- XOR 非仿射，塞不进 `S[...]`；`ComposeLayout(per_element, swizzle_len, atom_len, tile_layout)` 把仿射映射与置换打包，三个参数都是**位数**：M 保留向量组连续、B 是被置换的槽宽、S 是源位域的距离，且要求 \( S \ge B \)（两域不重叠 ⇒ 变换是对合，读写共用一条公式）。
- `(8,64)` fp16 + 128B swizzle 的闭式解是 \( \mathrm{addr} = 64i + 8(q \oplus i) + r \)；第 0 列地址 \( 72i \)、bank \( 4i \)，从 8-way 冲突降到无冲突。
- 参数选择有规则而非手推：\( M=\log_2(16\text{B}/\text{元素字节}) \)、\( B=\log_2(\text{模式行宽}/16\text{B}) \)、`atom_len=3`；fp16+128B 即 `(3,3,3)`，fp32 时 M 降为 2。真实内核用 `mma_shared_layout(dtype, SwizzleMode, shape)` 生成。
- 连续维不得超过 swizzle 行宽：fp32 下 `(8,64)` 一行 256B 跨两个 atom，列读只摊到 4 个 bank；切成 128B 宽的视图才恢复无冲突——这与 u6 讲的 TMA box 宽度约束互为镜像。
- 一致性纪律：同一 tile 的所有访问（TMA 写、MMA 描述符读、epilogue）必须使用同一 swizzle 模式；`ComposeLayout` 挂在 buffer 上让各方共享同一套地址计算。

## 7. 下一步学习建议

单元十到此完结，你已经集齐 TIRx 布局系统的全部三块拼图：`TileLayout`（仿射 + 命名轴）、replication/offset（u10-l1）、`ComposeLayout`（swizzle）。接下来两条路：

1. **主线路（建议）**：进入单元十一，读 `chapter_gemm_basics/index.md` 的 Step 1。那里 `mma_shared_layout` 生成的 swizzle 布局将第一次在完整内核里服役——`Tx.cta.copy` 写入、`Tx.gemm_async` 读取共用同一个 `A_layout`。带着本讲的问题去读：**谁是这个布局的写端、谁是读端？两端各自怎么拿到地址？**
2. **回望路线**：若想看 swizzle 的"消费端"细节，可回读 u5-l2（Hopper wgmma 矩阵描述符如何编码 swizzle 模式）与 u6-l1/u6-l2（TMA 如何在写入路径上顺带完成 swizzle、3D TMA 如何搬运多个 atom）。三代硬件对同一物理排布的三种消费方式，能反过来加深你对"`ComposeLayout` 只是声明、变换由各硬件单元执行"的理解。
