# 3D TMA 与 128B swizzle 行布局

## 1. 本讲目标

学完本讲，你应该能够：

1. **用第三维（group 维）组织多个 swizzle atom**：解释为什么"行宽超过 128 字节"的 tile 不能作为一个 2D `SWIZZLE_128B` box 直接搬，以及如何通过 `group = j // 64` 把它重解释成三维张量、用一条 3D TMA 指令搬完。
2. **推导 128B swizzle 的行布局公式**：写出 `SWIZZLE_128B` 下 sector 的落点公式 `physical_sector = logical_sector XOR (row % 8)`，并进一步推导"128B 分组"与"256B 行步长"两种行布局各自的 bank sector 公式，判断它们的冲突重数。
3. **核对 tile 元素到 SMEM 地址的映射**：对任一逻辑坐标 `(row, j)`，算出它经 3D 重解释与写入时 swizzle 之后落在 SMEM 的字节地址，并用书中的交互演示逐格核对。

本讲是「TMA 异步数据搬运」单元的第二讲。上一讲（u6-l1）建立了 2D TMA 的基本模型：tensor map 描述符、单线程发起、写入时 swizzle，并留下一个入口问题——**box 最内连续维不得超过 swizzle 宽度（128B 模式下 fp16 恰为 64 个元素）**。本讲就把这个约束变成出发点：行更宽的 tile 怎么办。

## 2. 前置知识

本讲直接建立在 u6-l1 与 u4-l4 之上，先用三段话把需要的结论串起来。

**来自 u6-l1（TMA 基本模型）**：tensor map 是"静态档案"，登记 dtype、globalDim、globalStrides、boxDim 与 swizzle 模式；指令参数只给 tile 起始坐标、SMEM 目的地址与 mbarrier。TMA 引擎在写 SMEM 的路径上顺带应用 swizzle，发起线程不算 XOR 地址。演示场景里的 tile 是 8 行 × 8 个 sector（每行恰好 128B），swizzle 公式是 \( \text{physical\_sector} = \text{col} \oplus \text{row} \)——**那是因为 tile 只有 8 行**；本讲的 tile 有 16 行，公式会多出一个 `% 8`。

**来自 u4-l4（Swizzle）**：SMEM 划分为 32 个 bank，\( \text{bank} = (\text{addr} \div 4) \bmod 32 \)；同一 wavefront 内落在同 bank 不同地址的访问被串行化，冲突重数等于所需周期数。XOR swizzle 是双射（元素一个不少、一个不重），且自反（读写两端共用同一条公式）。atom（最小地址重复单元）的概念当时已提出，本讲正式把它作为主角。

**三个小单位**，本讲反复使用：

| 术语 | 含义 | 本讲场景下的尺寸 |
| --- | --- | --- |
| **sector** | 16 字节的最小观察/搬运单位 | 8 个 fp16 |
| **swizzle atom** | swizzle 的最小地址重复单元（8 行 × swizzle 宽度） | `SWIZZLE_128B` 下 8 行 × 8 sector = 1024B |
| **group（本讲新增）** | 把一条逻辑行按 128B 切出的段，3D 视图的最外维 | fp16 下 64 个元素 |

另外记住 u6-l1 的一致性纪律：**tensor map、SMEM 布局、后续 MMA 指令必须描述同一物理排布**。本讲所有地址推导，本质都是在把这句话展开成可计算的公式。

## 3. 本讲源码地图

本讲的"源码"是教材正文与配套交互演示，正文给出公式与约束，演示给出可悬停核对的逐 sector 映射：

| 文件 | 作用 |
| --- | --- |
| [chapter_tma/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md) | TMA 章正文。本讲主要读 L50-L127：`Using 3D TMA to Move Multiple Swizzle Atoms` 与 `128-Byte Swizzling and Row Layout` 两节 |
| [_extra/demo/tma_3d.html](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tma_3d.html) | 交互演示：左边 16×256 fp16 全局矩阵（每格 16B sector），选中 16×128 切片，一条 3D TMA 把两个 group 写入右侧 SMEM；内含 3D tensor map 的具体字段值 |
| [_extra/demo/tiling_constraint.html](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tiling_constraint.html) | 交互演示：正文"行布局"一节嵌入的对照实验，切换"两个 128B group"与"保留 256B 行步长"，直接报告所选列访问的 bank sector 与冲突重数 |
| [_extra/demo/swizzle_atom_general.html](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/swizzle_atom_general.html) | u4-l4 用过的 atom 总览演示：128B/64B/32B 三种 atom 形状与各自 XOR 公式，本讲借它把 atom 概念钉牢 |
| [zh/chapter_tma/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_tma/index.md) | 上述章节的中文镜像，内容同构 |

浏览演示的方式与 u6-l1 相同：构建书站（见 u1-l2）后阅读 TMA 章，或直接用浏览器打开仓库里的 HTML（其依赖的 `../viz-base.css`、`../viz-base.js` 同在 `_extra/` 下，直接打开可用）。

## 4. 核心概念与源码讲解

### 4.1 模块一：swizzle atom——重排只发生在 1024 字节之内

#### 4.1.1 概念说明

`SWIZZLE_128B` 的地址重排不是对整块 tile 施加的，而是限制在一个**重复单元**内进行。正文一句话定义了它：**`SWIZZLE_128B` 使用一个"8 行 × 128 字节"的重复单元，称为 swizzle atom；地址重排只发生在 atom 内部**——因此 TMA box 的最内连续维不得超过 128 字节，对 fp16 而言恰好是 64 个元素（[chapter_tma/index.md:L52](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L52)）。

这个定义直接解释两件事：

1. **为什么有 box 宽度上限**：重排不出 atom，一条 256B 宽的行如果作为一个 box 的最内维，跨 atom 的元素就无法用同一公式定位；
2. **为什么 atom 是"地址周期"**：SMEM 里 atom 逐个连续摆放，每个 atom 占 8 × 128B = 1024B。同一个逻辑 sector 编号在相邻 atom 里用同样的方式被打散——周期是 1024B。

对照 u5-l2 讲过的 Hopper 矩阵描述符：`SWIZZLE_128B` 的 atom 正是那个"8 × 128B 的 1KB 连续块"。三代架构共用同一个 atom 几何，只是消费它的机制不同（Hopper 靠描述符字段，Blackwell 靠 TMA 写入）。

#### 4.1.2 核心流程

atom 在 SMEM 中的摆放与 XOR 键的来源可以写成两条地址公式。设某元素位于第 \(a\) 个 atom 的第 \(r\) 行（atom 内行号 \(r \in [0,8)\)）、第 \(s\) 个逻辑 sector（\(s \in [0,8)\)，每 sector 16B），则其 SMEM 起始字节地址为：

\[
\mathrm{addr} = 1024\,a + 128\,r + 16\,\bigl(s \oplus (r \bmod 8)\bigr)
\]

注意 \(r \in [0,8)\) 时 \(r \bmod 8 = r\)，所以在**单个 atom 内**公式退化成 u6-l1 的 \(s \oplus r\)。把地址按 128B 跨度编号（\(\mathrm{addr} \gg 7\)）可以看出 XOR 键的来源：

\[
(\mathrm{addr} \gg 7) \bmod 8 = \bigl(8a + r\bigr) \bmod 8 = r
\]

即 **XOR 键就是该元素所在 128B 跨度编号的低 3 位**（1024B 的 atom 基址贡献 \(8a \bmod 8 = 0\)）。这一视角在模块三比较两种行步长时会再次发挥作用。

不同 swizzle 模式的 atom 形状不同，但都是"8 行 × 模式宽度"：

| 模式 | atom 形状 | XOR 公式 |
| --- | --- | --- |
| `SWIZZLE_128B` | 8 行 × 8 sector（1024B） | `swizzled_col = logical_col XOR row` |
| `SWIZZLE_64B` | 8 行 × 4 sector（512B） | `swizzled_col = logical_col XOR (row // 2)` |
| `SWIZZLE_32B` | 8 行 × 2 sector（256B） | `swizzled_col = logical_col XOR (row // 4)` |

（另有 16B 交错模式，不含 XOR swizzle。）宽度越窄，同一 XOR 键覆盖的行数越多——8 行里每 `div` 行共享一个键。

#### 4.1.3 源码精读

**（1）正文的 atom 定义。** [chapter_tma/index.md:L50-L52](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L50-L52)："Using 3D TMA to Move Multiple Swizzle Atoms"一节开头即给出三条信息：atom 是 8 行 × 128B 的重复单元；地址重排只发生在 atom 内部；因此 box 最内连续维不得超过 128B，fp16 恰为 64 个元素。

**（2）atom 总览演示的三种形状。** [_extra/demo/swizzle_atom_general.html:L150-L159](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/swizzle_atom_general.html#L150-L159) 的 `FMTS` 配置表是上表的出处：`128: {ac: 8, div: 1, formula: 'swizzled_col = logical_col XOR row'}`、`64: {ac: 4, div: 2, ... XOR (row // 2)}`、`32: {ac: 2, div: 4, ...}`。其副标题一句话点题："swizzle atom 是内存中应用 swizzle 的基本连续区域"（[_extra/demo/swizzle_atom_general.html:L85](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/swizzle_atom_general.html#L85)）。

**（3）3D 演示里的 atom 边界。** [_extra/demo/tma_3d.html:L203-L216](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tma_3d.html#L203-L216) 渲染 SMEM 侧每个 group 的 16 行时，在 `r === 8` 处给行标签和格子加上 `atom-start` 顶边框——那就是"第一个 atom 结束、第二个 atom 开始"的可见分界；每个 group 的标签也直接写着 `g0 (2 atoms)`（[_extra/demo/tma_3d.html:L186-L188](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tma_3d.html#L186-L188)）。

#### 4.1.4 代码实践

**实践：给几块 tile 数 atom、算基址（纸笔推导，无需 GPU）。**

1. **实践目标**：建立"atom 数量 = 行数方向堆叠 × 行宽方向分组"的直觉，并算出每个 atom 的 SMEM 基址。
2. **操作步骤**：对下面四块 fp16 tile，分别求 atom 个数，并按"atom 在 SMEM 中从 0 开始逐个连续摆放"写出各 atom 的基址（字节）：
   - ① 8 行 × 64 列（u6-l1 演示的 tile）；
   - ② 16 行 × 64 列；
   - ③ 16 行 × 128 列（本讲演示的切片）；
   - ④ 16 行 × 256 列（本讲演示左侧的完整矩阵）。
3. **需要观察的现象**：哪种情况**不需要** 3D TMA？行数超过 8 时是否必须加维？
4. **预期结果**：每行 64 个 fp16 = 128B，恰为一个 atom 宽。
   - ① 1 个 atom（8 行 × 128B = 1024B），基址 `[0]`——2D box 即可（u6-l1 的场景）；
   - ② 2 个 atom（16 行沿行方向堆成两段），基址 `[0, 1024]`——最内维仍是 64 元素 ≤ 上限，**2D box 就够，无需 3D**；
   - ③ 4 个 atom（行宽方向 2 组 × 行方向 2 段），基址 `[0, 1024, 2048, 3072]`——行宽 256B 超上限，**必须切组，即模块二的 3D TMA**；
   - ④ 8 个 atom（4 组 × 2 段），基址 `0, 1024, ..., 7168`。
   关键观察：**行数多只是 atom 纵向堆叠，不违反 box 约束；行宽超 128B 才是本讲要解决的问题**。以上为推导结果。

#### 4.1.5 小练习与答案

**练习 1**：`SWIZZLE_64B` 模式下 atom 多大？fp16 的 box 最内维上限是多少个元素？

**答案**：atom = 8 行 × 64B = 512B（4 个 sector）。box 最内维 ≤ 64B，fp16（2B）下即 32 个元素。XOR 键相应变为 `row // 2`：相邻两行共享一个键，8 行产生 4 个不同键（[_extra/demo/swizzle_atom_general.html:L150-L159](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/swizzle_atom_general.html#L150-L159)）。

**练习 2**：一个 32 行 × 64 列的 fp16 tile 被 TMA 写入 SMEM，第 2 个 atom 的基址是多少？它覆盖哪些行？

**答案**：基址 1024B；覆盖第 8–15 行（每个 atom 8 行）。第 0、1 个 atom 分别覆盖第 0–7 行与第 8–15 行，第 3 个 atom（基址 3072B）覆盖第 24–31 行。行数继续增加只是继续堆 atom，box 的最内维始终是 64 个 fp16，不违反 128B 约束。

**练习 3**：既然重排只发生在 atom 内部，跨 atom 的访问模式还受 swizzle 保护吗？

**答案**：XOR 键来自 128B 跨度编号（见 4.1.2 的推导），不同 atom 的行会得到各自不同的键，所以跨 atom 的列访问同样被打散——保护是逐 atom 独立提供、拼起来仍然有效的。但要注意：**同一 tile 的所有访问必须使用同一 swizzle 模式**（u4-l4 的一致性结论），不能一半 atom 用 128B、一半用 64B。

### 4.2 模块二：3D TMA——加一个 group 维，一次搬完多个 atom

#### 4.2.1 概念说明

现在处理真正的难题。取一个 **16 行 × 128 列的 fp16 切片**：每行 128 个 fp16 = 256 字节。作为 2D box，它的最内连续维是 256B，**超过** `SWIZZLE_128B` 的 128B 上限——无论怎么选模式都装不下（[chapter_tma/index.md:L54](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L54)）。

解法不是搬两次，而是**换一种坐标解释**：把每行切成两段 64 元素的组（group 0 = 第 0–63 列，group 1 = 第 64–127 列，各 128B），然后定义：

\[
\text{group} = j \,//\, 64, \qquad \text{col} = j \bmod 64, \qquad \text{global}[\text{row}, j] = \text{global3}[\text{group}, \text{row}, \text{col}]
\]

同一份数据于是有了三维 `(group=2, row=16, col=64)` 视图（[chapter_tma/index.md:L56-L69](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L56-L69)）。关键性质有三：

1. **不搬数据**：这个 reshape 只改变 tensor map 对坐标的解释，global memory 里的数据一个字节都不动（[chapter_tma/index.md:L70](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L70)）；
2. **满足约束**：加维之后最内维 `col` 只有 64 个 fp16 = 128B，恰好回到上限之内；
3. **一条指令搬完整切片**：box 在三个维度上分别是 64 列 × 16 行 × 2 组，TMA 引擎按三维坐标展开地址，把两个 group 各自写成 SMEM 里连续的 2KB 块（`g0`、`g1`）。

落到 SMEM 后，每个 group 16 行 = 2 个 atom（第 0–7 行第一个、第 8–15 行第二个），整个切片共 4 个 atom；开 `128B` 后，TMA 在**每个 atom 内部**按下式重排（[chapter_tma/index.md:L72-L74](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L72-L74)）：

\[
\text{physical\_sector} = \text{logical\_sector} \oplus (\text{row} \bmod 8)
\]

与 u6-l1 的 \( \text{col} \oplus \text{row} \) 相比只多了 `% 8`——因为行号现在可以超过 8，而 XOR 键只取 atom 内的行号。

#### 4.2.2 核心流程

一次 3D TMA 拷贝的完整规格（对应演示场景：全局矩阵 16×256 fp16，切片取前 128 列）：

```text
编译期：tensor map（静态）
  dtype         = f16
  globalDim     = [64, 16, 4]      # 内维在前：col=64, row=16, group=4
  globalStrides = [512B, 128B]     # row 步长 512B（整行 256 个 fp16），group 步长 128B
  boxDim        = [64, 16, 2]      # 一次搬 64 列 × 16 行 × 2 组 = 16×128 切片
  swizzle       = SWIZZLE_128B

内核里（每个 tile 一次，单线程发起）：
  cp.async.bulk.tensor.3d.shared::cta.global.mbarrier::complete_tx::bytes
      [smem], [tensormap, {col=0, row=0, group=gStart}], [mbar]

引擎写入 SMEM（对 box 内每个 16B sector）：
  group 块基址 = g * 2048          # 16 行 × 128B
  行内偏移     = r * 128 + ((c // 8) XOR (r % 8)) * 16
  # c 为 group 内列号（0..63），整块共 4 个 atom
```

两个易错点提前点出：

- **维度顺序从最内到最外**：图示按 `(group, row, col)` 讲解，但 tensor map 与 PTX 指令都把最内维排在最前面，所以指令坐标写作 `{col, row, group}`——顺序与图示相反（演示信息框专门加了这条 Note）；
- **group 与 atom 不是一回事**：group 是**行宽方向**的 128B 段（第三维的坐标），atom 是 **SMEM 里的 1024B 重复单元**；本场景中一个 group 含 2 个 atom，一个 box 含 2 group × 2 atom = 4 个 atom。

#### 4.2.3 源码精读

**（1）正文的完整推导。** [chapter_tma/index.md:L54-L74](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L54-L74)：L54 提出问题（一行 256B 无法直接充当 128B atom 的一行）；L56-L59 切组；L61-L69 给出 `group/col` 公式与 3D 视图；L70 强调 reshape 不搬数据；L72-L74 描述演示场景并给出"每 group 2 atom、整切片 4 atom、atom 内按 `logical_sector XOR (row % 8)` 重排"的结论。

**（2）演示的常量设定。** [_extra/demo/tma_3d.html:L108-L115](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tma_3d.html#L108-L115) 定义场景：全局矩阵 16 行（`GDR=16`）、32 个 sector 列（`GDC=32`，即 256 个 fp16）、每次拷贝 2 个 group（`NGROUPS=2`）、整行共 4 个 group（`TOTAL_GROUPS=4`）、每 group 8 个 sector（`GSECS=8`）、整行 512B（`ROW_BYTES=512`）、每 group 128B（`GRP_BYTES=128`）。页面副标题一句话概括技法："把每条 256 字节的行切成两个 128 字节的组，再对每组独立 swizzle"（[_extra/demo/tma_3d.html:L65-L66](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tma_3d.html#L65-L66)）。

**（3）演示信息框：一份带数值的 3D tensor map。** [_extra/demo/tma_3d.html:L142-L152](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tma_3d.html#L142-L152) 渲染的信息框包含四行：TensorMap 字段（`dtype=f16, globalDim=[64,16,4], globalStrides=[512B,128B], boxDim=[64,16,2], swizzle=CU_TENSOR_MAP_SWIZZLE_128B`）；3D 指令（`cp.async.bulk.tensor.3d...[smem], [tensormap, {0, 0, group}], [mbar]`）；坐标切分式（`16×128 fp16 slice → reshape(16, 2, 64).transpose(1, 0, 2) → (group=2, row=16, col=64)`）；以及"维度从内到外、指令顺序与图示相反"的 Note。`globalStrides` 的两个值都可手工复核：row 维一步跨整行 256 fp16 = 512B；group 维一步跨 64 fp16 = 128B。

**（4）演示的 swizzle 实现与悬停核对。** SMEM 侧每个 group 按"物理 sector \(p\) 显示逻辑列 \(p \oplus (r \bmod 8)\)"渲染（[_extra/demo/tma_3d.html:L203-L216](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tma_3d.html#L203-L216)，核心是 `logCol = p ^ (r % 8)`）；悬停全局侧某 sector 时，箭头标注 `col C XOR (row R % 8) = sector ...` 并高亮落点（[_extra/demo/tma_3d.html:L224](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tma_3d.html#L224)、[L303](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tma_3d.html#L303)）。"Col offset"按钮在 0/128 之间切换（[_extra/demo/tma_3d.html:L319-L332](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tma_3d.html#L319-L332)）：切到 128 时只有指令坐标里的 group 从 0 变成 2，tensor map 一字不动——这延续了 u6-l1"描述符静态、指令参数动态"的对照。

**（5）正文的 iframe 嵌入。** 演示经 [chapter_tma/index.md:L76-L82](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L76-L82) 嵌入正文，图注提示"切换 Col offset 选择原始矩阵的前/后 128 列，悬停蓝色区域内任一 cell 查看它的 16 字节 sector 落在共享内存的哪里"。

#### 4.2.4 代码实践

**实践：手推若干元素的 3D 坐标与指令参数（纸笔推导，无需 GPU）。**

1. **实践目标**：把"切片坐标 → 3D 坐标 → 指令坐标"的换算走熟，并检查自己写出的 tensor map 字段。
2. **操作步骤**：
   - 对 16×128 fp16 切片的下列元素 `(row, j)`，用 `group = j // 64`、`col = j % 64` 求三维坐标 `(group, row, col)`：(0, 0)、(0, 64)、(10, 45)、(15, 127)；
   - 写出 Col offset = 128（搬整矩阵的第 128–255 列，对应 group 2 和 group 3）时指令坐标 `{col, row, group}` 的三个分量；
   - 仿照演示信息框，为"全局矩阵 16×256 fp16、一次搬 16×128 切片"写出五个 tensor map 字段。
3. **需要观察的现象**：group 坐标在哪些元素间变化；`globalStrides` 两个分量分别对应哪一步。
4. **预期结果**：
   - (0,0)→(0,0,0)；(0,64)→(1,0,0)；(10,45)→(0,10,45)；(15,127)→(1,15,63)。
   - Col offset = 128 时，起始 group = 128 // 64 = 2，指令坐标为 `{col=0, row=0, group=2}`（与演示 L135-L139 拼出的文本一致）。
   - 字段：`dtype=f16, globalDim=[64,16,4], globalStrides=[512B,128B], boxDim=[64,16,2], swizzle=CU_TENSOR_MAP_SWIZZLE_128B`。
   以上为确定性推导；可打开演示页把 Col offset 切到 128，核对信息框里的指令坐标。

#### 4.2.5 小练习与答案

**练习 1**：reshape 成 `(group=2, row=16, col=64)` 之后，global memory 里的数据动了吗？谁在"动"？

**答案**：没动。reshape 只是 tensor map 对坐标的解释方式；真正按新坐标搬运数据的是 TMA 引擎——它按三维地址展开，把两个 group 分别写成 SMEM 里两块连续的 2KB。这就是"逻辑上重排、物理上由搬运者代劳"（[chapter_tma/index.md:L70](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L70)）。

**练习 2**：为什么 `globalDim` 是 `[64, 16, 4]` 而不是 `[4, 16, 64]`？`globalStrides = [512B, 128B]` 两个分量的顺序又为什么是这样？

**答案**：tensor map 与 PTX 都把**最内维排在最前**：col（64）最内、group（4）最外，所以写作 `[64, 16, 4]`；strides 同样按"除最内维外的维度、从内到外"排列——先 row 的 512B，再 group 的 128B。图示为了讲解方便用 `(group, row, col)` 顺序，读描述符时要反过来看（[_extra/demo/tma_3d.html:L142-L152](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tma_3d.html#L142-L152) 的 Note）。

**练习 3**：把 dtype 换成 fp32（4 字节），一条 128B 的 atom 宽度能装多少个元素？一个 16 行 × 128 列的 fp32 切片需要切成几个 group？

**答案**：fp32 下 128B = 32 个元素，atom 为 8 行 × 32 列（仍占 1024B）。切片每行 128 个 fp32 = 512B，需要切成 \(512/128 = 4\) 个 group，3D 视图为 `(group=4, row=16, col=32)`，boxDim 写作 `[32, 16, 4]`。每个 group 仍是 16 行 = 2 个 atom，所以整个切片共 4 × 2 = 8 个 atom、占 8KB SMEM。dtype 只改变"128B 里装几个元素"，不改变 atom 的字节几何（`tiling_constraint` 演示的 dtype 切换也只改每格元素数、不改地址映射）。

### 4.3 模块三：128B swizzle 的行布局——行步长决定 XOR 键

#### 4.3.1 概念说明

模块二解决了"怎么搬"，模块三回答"落到 SMEM 后行该怎么排"。仍以每行 16 个 sector（256B）的切片为例，有两种候选行布局：

- **布局 A（128B 分组）**：按模块二的 3D 视图落盘——先写满 g0 的 16 行，再写 g1 的 16 行；group 内相邻行相距 128B；
- **布局 B（保留 256B 行步长）**：保持直觉的行主序——每行 256B 连续，行内前 128B 是 span 0、后 128B 是 span 1。

正文先澄清一个容易误解的点：**`SWIZZLE_128B` 永远只在 128 字节跨度内置换数据**。即使一条逻辑行有 16 个 sector（256B），硬件也不会把整行当成一个 256B 的 swizzle 单元；真正需要区分的，是**两个 128B 跨度在内存里怎么排**（[chapter_tma/index.md:L86](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L86)）。

两种布局的差别最终体现在 **bank sector** 上：一次 16B 的 sector 访问覆盖 4 个相邻 bank，把 32 个 bank 按 4 个一组记为 S0–S7 共 8 个 bank sector（[chapter_tma/index.md:L88](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L88)）。bank sector 编号就是 \( (\mathrm{addr} \gg 4) \bmod 8 \)（u4-l4 的 bank 公式在 16B 对齐访问下的等价写法）。

#### 4.3.2 核心流程

考察典型访问模式：**从 8 个连续行里读同一个 sector 列**（MMA 取操作数时的常见形态），得到 8 个并行的 16B 访问。设列号为 `col`，`span = col // 8`、`local_col = col % 8`。

**布局 B（256B 行步长）**：sector 起始地址 \( \mathrm{addr} = 256\,\text{row} + 128\,\text{span} + \cdots \)，于是 XOR 键（128B 跨度编号的低 3 位）为：

\[
(\mathrm{addr} \gg 7) \bmod 8 = (2\,\text{row} + \text{span}) \bmod 8
\]

正文给出的 bank sector 公式（[chapter_tma/index.md:L90-L94](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L90-L94)）：

\[
\text{bank\_sector} = \text{local\_col} \oplus \bigl((2\,\text{row} + \text{span}) \bmod 8\bigr)
\]

行每前进一行，地址跨 2 个 128B 跨度，XOR 键每次 **+2**：8 行只产生 4 个不同的 bank sector，每个被访问两次——**2-way 冲突**。

**布局 A（128B 分组）**：坐标改写为 `(group, row, local_col)` 后，group 内相邻行相距 128B，XOR 键变为（[chapter_tma/index.md:L98-L102](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L98-L102)）：

\[
\text{bank\_sector} = \text{local\_col} \oplus (\text{row} \bmod 8)
\]

行每前进一行，XOR 键 **+1**：8 行取遍 8 个不同的 bank sector，访问完全并行。

两条公式其实是同一件事的两种行距：**XOR 键 = 128B 跨度编号 % 8**。行距 128B → 键 +1/行（8 个键全不同）；行距 256B → 键 +2/行（只剩 4 个键）。还要记住正文的定位说明：布局 B 只是为了展示行步长的影响而保留的对照——它的最内维是 256B，**本来就不能作为单个 `SWIZZLE_128B` TMA box**（[chapter_tma/index.md:L96](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L96)）。最后，正文 L127 收束全章约束：box 最内连续维不得超过所选 swizzle 宽度；数据比 swizzle 宽度窄时，SMEM 分配仍要预留完整宽度；应在 128B/64B/32B 中按 tile 宽度与访问模式取舍（[chapter_tma/index.md:L127](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L127)）。

#### 4.3.3 源码精读

**（1）正文的对照推导。** [chapter_tma/index.md:L84-L104](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L84-L104)：L86 澄清"硬件不把 256B 整行当一个 swizzle 单元"；L88 设定 16×16 sector 网格与 bank sector S0–S7；L90-L96 给出布局 B 公式并判定 2-way 冲突；L98-L104 给出布局 A 公式并判定 8 行 8 个不同 bank sector、可并行。

**（2）对照演示：两种行布局的开关。** [chapter_tma/index.md:L106-L125](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L106-L125) 嵌入了 `tiling_constraint.html`：选 `128B groups` 显式把两个跨度排成 g0/g1，选 `256B stride` 保留原行步长；黑框格子标出所选列的落点，下方汇总这批访问用到的 bank sector。演示源码里两种布局各用一行 XOR 实现这一对照（[_extra/demo/tiling_constraint.html:L238-L246](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tiling_constraint.html#L238-L246)）：分组时 `lc = p ^ (r % 8)`，保留 256B 步长时 `lc = p ^ ((r * 2 + gi) % 8)`。面板标题随之切换（"Stored as Two 128B Groups" vs "Preserve the 256B Row Stride"，[L213-L215](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tiling_constraint.html#L213-L215)）。

**（3）演示的状态栏与公式框。** 底部状态栏直接报告结论：无冲突时显示"8/8 sectors → 1 cycle — conflict-free"，有冲突时显示"N-way conflict → N cycles"（[_extra/demo/tiling_constraint.html:L283-L290](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tiling_constraint.html#L283-L290)）；公式框把两种布局的因果链写成一句话——分组版"XOR 值各不相同 → 无冲突"、256B 版"XOR 索引每行 +2 → 只有 4 个不同值 → 2-way 冲突"（[L293-L298](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tiling_constraint.html#L293-L298)）。演示的公式框标题也点明主旨："两种布局每个 swizzle 跨度都是 8 个 16B sector，是**行步长**决定了下一行逻辑行用哪个 XOR 索引"（[L118-L121](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tiling_constraint.html#L118-L121)）。

**（4）在 GEMM 内核里的落点。** 教材 GEMM 章取 `BLK_M, BLK_N, BLK_K = 128, 128, 64`，并用 `mma_shared_layout(a_type, SwizzleMode.SWIZZLE_128B_ATOM, (BLK_M, BLK_K))` 生成 A/B 的 SMEM 布局（[chapter_gemm_async/index.md:L92-L98](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L92-L98)）：`BLK_K=64` 个 fp16 使每条 K 行恰好 128B——tile 尺寸的选择直接落在本讲的约束上；128 行则堆成 16 个 atom，全部由同一份布局描述。深入留给单元十二。

#### 4.3.4 代码实践

**实践：手算两种行布局下列读的 bank sector 序列，再用演示核对（纸笔 + 浏览器）。**

1. **实践目标**：亲眼验证"行距 128B → 键 +1/行 → 无冲突；行距 256B → 键 +2/行 → 2-way 冲突"。
2. **操作步骤**：
   - 在 16×16 sector 网格中取**列 col = 5**（`span = 0`、`local_col = 5`），对行 `row = 0..7` 分别用两条公式计算 bank sector：
     - 布局 B：\( 5 \oplus ((2\,\text{row} + 0) \bmod 8) \)；
     - 布局 A：\( 5 \oplus (\text{row} \bmod 8) \)；
   - 统计两种序列的不同取值个数与最大重复次数，得出冲突重数与周期数；
   - 打开 `tiling_constraint.html`（构建书站或直接打开文件），Column 选 5，分别在 `128B groups` 与 `256B stride` 下查看底部 Bank Sector Activity 条与状态栏。
3. **需要观察的现象**：256B 模式下是否恰好 4 个 sector 各亮两次（标注 ×2）；128B 模式下是否 8 个 sector 各亮一次。
4. **预期结果**：
   - 布局 B：键序列 0,2,4,6,0,2,4,6 → bank sector 序列 5,7,1,3,5,7,1,3——4 个不同值、每个 2 次 → **2-way 冲突，2 周期**；
   - 布局 A：键序列 0,1,...,7 → bank sector 序列 5,4,7,6,1,0,3,2——8 个不同值 → **1 周期，无冲突**；
   - 演示状态栏应分别显示 `2-way conflict → 2 cycles` 与 `8/8 sectors → 1 cycle — conflict-free`（确定性计算；若书站未构建，手算部分即完整结论）。

#### 4.3.5 小练习与答案

**练习 1**：为什么布局 B 的 XOR 键"每行 +2"？

**答案**：因为 XOR 键取自 128B 跨度编号 \((\mathrm{addr} \gg 7) \bmod 8\)。布局 B 每行 256B = 2 个跨度，逻辑行号 +1 时地址多跨 2 个 128B 跨度，键随之 +2（模 8）。8 行里键序列 0,2,4,6,0,2,4,6，只剩 4 个不同值——这是"行步长决定 XOR 键"的直接后果。

**练习 2**：把列从 col = 5 换成 col = 13（即 span = 1），布局 B 的冲突会消失吗？

**答案**：不会。键变为 \((2\,\text{row} + 1) \bmod 8\)，序列 1,3,5,7,1,3,5,7——仍是 4 个不同值、每个 2 次，2-way 冲突不变。span 只让键整体偏移一个常数，不改变"每行 +2"的步进；冲突是行距造成的，与从哪个 span 开始读无关。

**练习 3**：一个 tile 的最内维只有 32B（如 16 个 fp16），却选了 `SWIZZLE_128B`，SMEM 分配要注意什么？

**答案**：SMEM 分配仍要按完整的 128B swizzle 宽度预留（每行留 128B，即便只用前 32B）——正文 L127 明确这一点。反过来说，这种"窄数据配宽模式"浪费空间，通常应改选 `SWIZZLE_32B`；模式选择要同时看 tile 宽度与访问模式。

## 5. 综合实践

把三个模块串成一个可运行的核对任务：**用脚本算出"第 2 个 atom"内若干元素的目标 SMEM 地址，并与 128B swizzle 行布局公式逐一核对**。纯 Python、无依赖、无需 GPU。

**任务 A：实现地址函数（示例代码，非项目原有代码）。**

```python
# 示例代码：3D TMA + SWIZZLE_128B 的 SMEM 地址计算
# 场景与 _extra/demo/tma_3d.html 一致：16x128 fp16 切片，一条 3D TMA 写入 SMEM
ROW_BYTES = 128                      # 每个 group 一行 = 64 个 fp16
GRP_ROWS = 16                        # 每个 group 16 行
GRP_BYTES = ROW_BYTES * GRP_ROWS     # 2048B（= 2 个 atom）
ELEM = 2                             # fp16 字节数

def slice_to_3d(row, j):
    """切片坐标 (row, j) -> (group, row, col)，对应 reshape(16,2,64).transpose(1,0,2)"""
    return j // 64, row, j % 64

def smem_addr(g, r, c):
    """3D 坐标 -> SMEM 字节地址（含写入时 swizzle）"""
    s, o = divmod(c, 8)              # 第 s 个 16B sector、sector 内第 o 个元素
    s_sw = s ^ (r % 8)               # 128B swizzle：XOR 键 = atom 内行号
    return g * GRP_BYTES + r * ROW_BYTES + s_sw * 16 + o * ELEM

def bank_sector(addr):
    """16B sector 访问覆盖的 bank sector 编号（S0..S7）"""
    return (addr >> 4) % 8
```

**任务 B：核对第 2 个 atom 的元素地址。** SMEM 中 atom 按 g0 行 0–7（第 1 个）、g0 行 8–15（**第 2 个**）、g1 行 0–7（第 3 个）、g1 行 8–15（第 4 个）排序。对下列切片坐标调用 `slice_to_3d` + `smem_addr`，应得到：

| 切片坐标 (row, j) | 3D 坐标 (g, r, c) | sector (s, o) | swizzle 后 \(s \oplus (r\bmod 8)\) | SMEM 字节地址 |
| --- | --- | --- | --- | --- |
| (8, 0) | (0, 8, 0) | (0, 0) | 0 | 1024 |
| (10, 45) | (0, 10, 45) | (5, 5) | 7 | 1402 |
| (12, 20) | (0, 12, 20) | (2, 4) | 6 | 1640 |
| (15, 63) | (0, 15, 63) | (7, 7) | 0 | 1934 |

第一个点 `(8, 0)` 落在 1024，正是第 2 个 atom 的基址；四点全部落在 `[1024, 2048)` 区间内，验证"第 2 个 atom = g0 的后 8 行"。

**任务 C：两项公式核对。**

1. **与 128B swizzle 行布局公式核对（模块三）**：取切片列 `j = 5`（g=0，sector s=0），对行 8–15（第 2 个 atom 内）逐行算 `bank_sector(smem_addr(...))`：键 = \(0 \oplus (r \bmod 8) = 0..7\)，得到 8 个互不相同的 bank sector——无冲突；再用模块三布局 B 的公式 \( 0 \oplus ((2r+0) \bmod 8) \) 重算同一列，只得到 4 个不同值——2-way 冲突。这正是 4.3.4 手算的脚本版。
2. **双射性核对（u4-l4 的检验工具）**：把整个切片 16×128 = 2048 个元素的地址收集成集合，断言 `len(set) == 2048` 且 `min/max` 恰为 `0 / 4094`——每行内 XOR 是双射（行内 128B 被填满），行与行、组与组区间互不重叠，整块恰好铺满 4KB。

**任务 D：与演示交叉验证（可选，需浏览器）。** 打开 `tma_3d.html`，悬停全局侧第 10 行、蓝色区域内第 6 个 sector（即 sector 列 5），箭头应标注 `col 5 XOR (row 10 % 8) = sector 7`，与任务 B 中 `(10, 45)` 一行的 swizzle 结果一致（演示按 sector 粒度显示，`s = 45 // 8 = 5`）。再点 `Col offset = 128`，确认只有指令坐标的 group 分量变为 2。

**预期结果**：任务 B 表格、任务 C 的两组 bank sector 序列与双集合计数全部与上述数值一致；任务 D 的悬停标注与脚本一致。以上为确定性计算，本机运行脚本即可验证；无浏览器环境时任务 A–C 已构成完整闭环。

## 6. 本讲小结

- **swizzle atom 是重排的边界**：`SWIZZLE_128B` 的 atom 是 8 行 × 128B（1024B）的重复单元，地址重排只在 atom 内部发生，因此 box 最内连续维不得超过 swizzle 宽度（fp16 下 64 个元素）；64B/32B 模式的 atom 更窄、XOR 键按 `row//2`、`row//4` 共享。
- **3D TMA 用"换解释"取代"搬两次"**：行宽超限时按 `group = j // 64` 切组，把切片重解释成 `(group, row, col)` 三维视图——数据不动，只是 tensor map 换一种坐标解释；一条 `cp.async.bulk.tensor.3d` 就能把多个 group（即多个 atom）一次搬完，两个 group 在 SMEM 里是两块连续的 2KB。
- **维度顺序从内到外**：`globalDim = [64, 16, 4]`、指令坐标 `{col, row, group}` 都把最内维排在最前，与图示的 `(group, row, col)` 顺序相反；`globalStrides` 以字节计、按除最内维之外的维度排列。
- **XOR 键 = 128B 跨度编号 % 8**：行距 128B 时键每行 +1，8 行取遍 8 个 bank sector（`local_col XOR (row % 8)`，无冲突）；保留 256B 行步长时键每行 +2，只剩 4 个键（`local_col XOR ((2*row + span) % 8)`，2-way 冲突）——**行步长决定冲突**。
- **约束的收束**：box 最内维 ≤ swizzle 宽度；数据更窄时 SMEM 仍按完整宽度预留；模式在 128B/64B/32B 中按 tile 宽度与访问模式选择。GEMM 章取 `BLK_K=64`（fp16 恰 128B）正是落在这些约束上。
- **一致性纪律贯穿始终**：tensor map、SMEM 布局与 MMA 指令描述同一物理排布；本讲把这条纪律落实成了可逐元素核对的地址公式。

## 7. 下一步学习建议

- **u6-l3（TMA 完成机制）**：本讲的 3D 拷贝一次搬 4KB，load 侧如何用 `mbarrier` 的 `expect_tx/try_wait` 追踪这些字节、store 侧如何用 `commit_group/wait_group` 判断源缓冲可复用（正文 L129-L172）。
- **单元七（Tensor Core 与 TMEM）**：tcgen05.mma 经矩阵描述符从 SMEM 读 A/B 时，描述符里的 swizzle 模式字段必须与本讲的写入模式一致——u5-l2 的描述符 + 本讲的 atom 几何 = 读取端的完整约定。
- **单元十二（GEMM Step 4–5）**：看 `SWIZZLE_128B_ATOM` 布局如何随 `BLK_K` 选择、TMA 加载如何进入双缓冲流水线；届时重读本讲的 tensor map 字段推导会有"每个数字都有出处"的感觉。
- **动手延伸**：把第 5 节的脚本改造成 fp32 版本（`ELEM=4`、每 group 32 列、4 个 group），重新生成任务 B 的地址表——这一步能同时检验你对模块一 atom 几何与模块二 group 切分的理解。
