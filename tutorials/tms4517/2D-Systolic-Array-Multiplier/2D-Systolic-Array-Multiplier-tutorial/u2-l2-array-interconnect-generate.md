# 阵列互联与 generate 生成

## 1. 本讲目标

u2-l1 把镜头推到了「最小细胞」PE，本讲把镜头拉远，看这些细胞是如何被**连成一张二维网格**的。如果说 PE 是一块块积木，那么 `systolicArray.sv` 就是把这些积木按行、列规则拼接起来的「图纸」——而且这张图纸是用 SystemVerilog 的 `generate` 在编译期自动展开的。

读完本讲，你应当能够：

- 说出 `rowInterConnect` 与 `colInterConnect` 这两个互连网（interconnect）的维度为什么是「行方向 N、流方向 N+1」，以及多出来的那一格承担什么角色。
- 解释首行、首列的 **dummy interconnect**（哑互联）如何把顶层的行/列矩阵接进阵列的边缘 PE，为什么需要单独叫它「dummy」。
- 看懂两层嵌套的 `generate`（`PerRow` × `PerCol`）如何用 `genvar` 把 \(N\times N\) 个 PE 实例化出来，并能逐条说出每个 PE 的 `i_a/i_b/o_a/o_b/o_y` 分别接到互联网的哪个位置。
- 把这些连接关系落到一张纸上，亲手为 \(4\times4\) 阵列画出完整的「水平线 + 垂直线」互联图。

本讲是 u2-l1 的承接：u2-l1 讲了单个 PE 的内部，本讲讲 PE 之间的「布线」；两者合起来，就完整解释了 `systolicArray.sv` 这个模块。它同时为 u2-l3（行/列矩阵变换）和 u2-l4（控制时序）打下结构基础——那两讲都会用到本讲建立的「边缘端口」与「阵列边界」概念。

## 2. 前置知识

在拆源码前，先用三段话建立两条关键直觉。

### 2.1 什么是 generate（生成块）

`generate` 是 SystemVerilog 的**编译期**（elaboration，也叫细化）机制。它允许你用一段 `for` 循环 + `genvar`（生成变量，必须是编译期常量）来「批量」实例化硬件。与传统 `for`（在仿真时按拍执行、描述的是某个时刻的行为）不同，`generate for` 在综合/仿真开始之前就被**展开**成具体的实例：写一次 `pe u_pe`，外面套两层 `generate for`，综合器就会把它复制成 \(N\times N\) 个真实的 PE。

```text
你写的源码（1 个 pe 实例 + 2 层 generate for）
        │  编译期展开（elaboration）
        ▼
真实的 N×N 个 pe 实例，层次名 u_systolicArray.PerRow[i].PerCol[j].u_pe
```

正因如此，改一个参数 `N`，阵列规模就整体改变——这正是本项目「可配置乘 2<N<17 方阵」的底层实现手段（见 u3-l2 参数化设计）。

### 2.2 为什么要用「网格」而不是「总线」

如果让所有 PE 同时去读同一份输入矩阵，那就退化成普通的并行乘法器，全局布线会很长、很慢。脉动架构的精髓（承接 u1-l1）是：**数据只在相邻 PE 之间流动**。所以 PE 之间不是挂在一根总线上，而是用一张「网格」——水平方向的线把同一行的 PE 串起来，垂直方向的线把同一列的 PE 串起来。本讲要看的 `rowInterConnect` / `colInterConnect` 就是这两组线。

### 2.3 网格的「边界」由谁来驱动

一张 \(N\times N\) 的 PE 网格里，每个内部 PE 都有左邻居和上邻居给它喂数据。但**最左一列**没有左邻居，**最顶一行**没有上邻居——这两条边的输入从哪来？答案是从顶层的行/列矩阵来。本讲的「dummy interconnect」就是负责把外部矩阵接到这两条边上的桥。理解了「边缘如何接、内部如何连」，整张网就通了。

> 约定提醒（承接 u2-l1）：PE 的 `i_a`/`i_b` 是输入、`o_a`/`o_b` 是寄存后透传给邻居的输出；`o_y` 是该 PE 的 32 位累加结果。本讲只关心**这些端口如何连到一起**，PE 内部细节请回看 u2-l1。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到的地方 |
|------|------|----------------|
| [rtl/systolicArray.sv](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/systolicArray.sv) | 用 `generate` 把 \(N\times N\) 个 PE 连成二维阵列，定义两组互连网与首行/首列的 dummy 接入。 | 全文精读，这是本讲绝对主角 |
| [rtl/pe.sv](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/pe.sv) | 定义单个 PE（8 位乘、32 位累加、`o_a`/`o_b` 透传）。 | 仅引用其端口表，内部逻辑见 u2-l1 |
| [rtl/topSystolicArray.sv](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/topSystolicArray.sv) | 顶层；生成 `row_q`/`col_q` 与 `doProcess_q` 并实例化本模块。 | 仅引用 `i_row`/`i_col`/`i_doProcess` 的来源，承接 u1-l3 |

> 提示：项目主 README 提到 `rtl/README.md` 会讲实现细节，但仓库里并不存在该文件，因此本讲所有结论都来自上面三个 `.sv` 源码本身。

## 4. 核心概念与源码讲解

本讲按「先看网线、再看边缘接线、最后看批量打桩」的顺序，拆成三个最小模块：

1. **互连网结构**：`rowInterConnect` / `colInterConnect` 的维度设计与数据流向。
2. **首行首列 dummy 接入**：`PerDummyRowColInterconnect` 如何把行/列矩阵接进边缘 PE。
3. **嵌套 generate 实例化 PE**：`PerRow` / `PerCol` 如何把 \(N\times N\) 个 PE 打进网格。

### 4.1 rowInterConnect / colInterConnect 互连网结构

#### 4.1.1 概念说明

互连网要解决的问题：**为阵列里每一行提供一条水平数据通路，为每一列提供一条垂直数据通路**。一条水平通路把同一行 N 个 PE 的 `i_a`/`o_a` 串成一根「线」——左邻居的 `o_a` 就是右邻居的 `i_a`；垂直通路同理，把上邻居的 `o_b` 接到下邻居的 `i_b`。

本项目没有为每条 PE 之间的连接单独声明一根线，而是**用两个 packed 多维数组把整张网一次性声明出来**：

- `rowInterConnect`：水平网，承担「行内左→右」的数据流（接 PE 的 `i_a`/`o_a`）。
- `colInterConnect`：垂直网，承担「列内上→下」的数据流（接 PE 的 `i_b`/`o_b`）。

这两个数组就是整个阵列的「血管」，所有数据流动都发生在它们上面。

#### 4.1.2 核心流程

先从最直观的二维网格图开始。文件开头用注释画出了 \(4\times4\) 阵列的互联关系，水平线是行互联、垂直线是列互联，箭头是数据流向：

[rtl/systolicArray.sv:6-15](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/systolicArray.sv#L6-L15) 用 ASCII 画出了 4×4 阵列的互联示意：

```text
PE[0][0] --> PE[0][1] --> PE[0][2] --> PE[0][3]
   |            |            |            |
   v            v            v            v
PE[1][0] --> PE[1][1] --> PE[1][2] --> PE[1][3]
   |            |            |            |
   v            v            v            v
PE[2][0] --> PE[2][1] --> PE[2][2] --> PE[2][3]
   |            |            |            |
   v            v            v            v
PE[3][0] --> PE[3][1] --> PE[3][2] --> PE[3][3]
```

- `-->`（水平）：`o_a` 向右送给同行的下一个 PE 的 `i_a`。
- `|` / `v`（垂直）：`o_b` 向下送给同列的下一个 PE 的 `i_b`。

现在把这张图抽象成「网格坐标」。给每个 PE 编号 `PE[i][j]`（`i` 是行、`j` 是列，都从 0 开始），那么它读取与写出的互联网位置是：

```text
PE[i][j] 的连接：
   i_a = rowInterConnect[i][j]      // 读：来自左侧
   o_a = rowInterConnect[i][j+1]    // 写：送给右侧
   i_b = colInterConnect[i][j]      // 读：来自上方
   o_b = colInterConnect[i+1][j]    // 写：送给下方
```

由这个规则就能反推出两个互联网的维度：

- `rowInterConnect[i][j+1]` 中 `j` 的取值范围是 \(0 \dots N-1\)，所以 `j+1` 的范围是 \(1 \dots N\)。再加上被读取的下标 \(j = 0\)，列方向总共需要 \(0 \dots N\) 共 **\(N+1\)** 个位置。
- 行方向（第一个维度）只有 \(N\) 行（每行一条水平网），所以是 **\(N\)**。

因此：

\[
\texttt{rowInterConnect} \in [N-1:0][N:0][7:0]
\]

垂直网同理，只是「流方向」换成了行方向：

\[
\texttt{colInterConnect} \in [N:0][N-1:0][7:0]
\]

把两个互联网的维度并排对比，能清楚看到「流方向多一格」的对称美：

| 互联网 | 承载的数据流 | 第 1 维（非流方向） | 第 2 维（流方向） | 每元素位宽 |
|--------|--------------|---------------------|-------------------|------------|
| `rowInterConnect` | 水平，行内左→右（`i_a`/`o_a`） | \(N\) 行 `[N-1:0]` | \(N+1\) 列 `[N:0]` | 8 位 |
| `colInterConnect` | 垂直，列内上→下（`i_b`/`o_b`） | \(N+1\) 行 `[N:0]` | \(N\) 列 `[N-1:0]` | 8 位 |

**那多出来的一格去哪了？** 它是「溢出口」（spillover）：

- `rowInterConnect[i][N]`：由最右一列 PE（`PE[i][N-1]`）的 `o_a` 写入，但右邊再没有 PE 去读它——它是「写了但没人读」的位置。
- `colInterConnect[N][j]`：由最底一行 PE（`PE[N-1][j]`）的 `o_b` 写入，下面再没有 PE 去读它——同样是「写了没人读」。

正因为这些位没人读，Verilator 的 `UNUSED` 检查会报警，所以源码用 `lint_off` 把它压掉（见 4.1.3）。这也从侧面说明：数据是「流动」的，流到边缘就自然溢出丢弃，阵列不需要回收它。

#### 4.1.3 源码精读

模块端口的输入侧直接体现了这两张网要喂给谁：

[rtl/systolicArray.sv:19-30](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/systolicArray.sv#L19-L30) 定义了 `systolicArray` 的端口：参数 `N`、时钟与异步复位、全阵列共享的 `i_doProcess`、行矩阵 `i_row` 与列矩阵 `i_col`，以及结果矩阵 `o_c`：

```systemverilog
module systolicArray
  #(parameter int unsigned N = 4)
  ( input  var logic                         i_clk
  , input  var logic                         i_arst
  , input  var logic                         i_doProcess
  , input  var logic [N-1:0][(2*N)-2:0][7:0] i_row
  , input  var logic [N-1:0][(2*N)-2:0][7:0] i_col
  , output var logic [N-1:0][N-1:0][31:0]    o_c
  );
```

两个互联网的声明紧随其后，注意它们的维度正是 4.1.2 推导出的「流方向 N+1」：

[rtl/systolicArray.sv:35-39](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/systolicArray.sv#L35-L39) 声明水平网 `rowInterConnect [N-1:0][N:0][7:0]`（\(N\) 行 × \(N+1\) 列）与垂直网 `colInterConnect [N:0][N-1:0][7:0]`（\(N+1\) 行 × \(N\) 列），并配注释说明 `o_a` 接右侧 PE 的 `i_a`、`o_b` 接下方 PE 的 `i_b`：

```systemverilog
logic [N-1:0][N:0][7:0] rowInterConnect;
logic [N:0][N-1:0][7:0] colInterConnect;
```

紧贴这两行声明的，是一对 Verilator 编译指令：

[rtl/systolicArray.sv:32](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/systolicArray.sv#L32) 与 [rtl/systolicArray.sv:40](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/systolicArray.sv#L40) 用 `/* verilator lint_off UNUSED */` 关闭 `UNUSED` 告警——因为两个互联网最右一列 / 最底一行（溢出口）被写入却从不被读取，否则 Verilator 会报这些位未被使用：

```systemverilog
/* verilator lint_off UNUSED */
logic [N-1:0][N:0][7:0] rowInterConnect;
logic [N:0][N-1:0][7:0] colInterConnect;
/* verilator lint_off UNUSED */
```

> 读者留意：常见的写法是「`lint_off` ... `lint_on`」配对，把抑制范围限定在这两行声明之间。这里第 40 行再次写了 `lint_off` 而非 `lint_on`，属于重复关闭而非恢复——对本模块无害（其后没有别的会触发 `UNUSED` 的信号），但与 off/on 配对的惯例不符。这是阅读源码时值得注意的一个细节，不需要修改。

至于每个 PE 如何把 `o_a`/`o_b` 写进对应网格位置、又如何从网格读 `i_a`/`i_b`，那是 4.3 节嵌套 `generate` 的工作；这两张网本身到此就「铺设」完毕了。

#### 4.1.4 代码实践

**实践目标**：亲手验证「流方向多一格」这件事，把抽象的维度落到具体的坐标上。

**操作步骤**（纸上推导型）：

1. 取 \(N=4\)。写出 `rowInterConnect` 的全部下标：行 `i = 0..3`，列 `j = 0..4`，共 \(4 \times 5 = 20\) 个 8 位位置。
2. 对每个列下标 `j`，判断它是「被读」还是「被写」：
   - `j = 0`：只被 dummy 写入、被 `PE[i][0]` 读（`i_a`）→ 边界入口。
   - `j = 1,2,3`：被左侧 PE 的 `o_a` 写、被右侧 PE 的 `i_a` 读 → 内部中继。
   - `j = 4`：只被最右 PE `PE[i][3]` 的 `o_a` 写，没人读 → **溢出口**。
3. 对 `colInterConnect` 做同样的事：行 `i = 0..4`，列 `j = 0..3`；行 `i = 4` 是溢出口。

**需要观察的现象**：每个互联网都有且只有「一条边」是溢出口——水平网在右边缘（列 `N`），垂直网在底边缘（行 `N`）。这正好对应数据「流到边缘就丢」的语义。

**预期结果**：你会在纸上得到两张 \(N \times (N+1)\) 与 \((N+1) \times N\) 的坐标表，并标注出唯一的溢出口边。这是后面画完整互联图的基础。

#### 4.1.5 小练习与答案

**练习 1**：如果误把 `rowInterConnect` 声明成 `[N-1:0][N-1:0][7:0]`（流方向只有 \(N\) 而非 \(N+1\)），会发生什么？

> **答案**：最右一列 PE 的 `o_a` 要写入 `rowInterConnect[i][N]`，但该下标越界（最大只有 `N-1`）。综合/Verilator 会报越界错误，或静默截断导致最右 PE 的 `o_a` 悬空。所以「流方向多一格」是硬性需求，不能省。

**练习 2**：为什么水平网行数是 \(N\)、垂直网列数也是 \(N\)，而不是 \(N+1\)？

> **答案**：行数对应「有几条水平数据通路」——每行 PE 共用一条，共 \(N\) 条，所以是 \(N\)。垂直网列数同理，每列一条共 \(N\) 条。只有「沿数据流方向」才需要多一格来容纳溢出口，垂直于流向的维度等于 PE 数 \(N\) 即可。

**练习 3**：`rowInterConnect` 与 `colInterConnect` 各有多少个 8 位「格子」？两者相等吗？

> **答案**：各有 \(N \times (N+1)\) 与 \((N+1) \times N\) 个，都等于 \(N^2+N\)，所以相等。两个互联网大小相同、方向正交，共同织成阵列的网格。

### 4.2 PerDummyRowColInterconnect 首行首列接入

#### 4.2.1 概念说明

4.1 节铺好了网，但留下一个问题：**网格的「入口边」由谁来驱动？**

- `rowInterConnect[i][0]`（每行最左一格）没有左侧 PE 给它写 `o_a`——它需要从外部「喂」进来。
- `colInterConnect[0][j]`（每列最上一格）没有上方 PE 给它写 `o_b`——同样需要从外部喂进来。

这两条入口边的数据来源，就是顶层的行矩阵 `i_row` 和列矩阵 `i_col`。本节的 `PerDummyRowColInterconnect`（按行/列哑互联）专门负责把外部矩阵接到入口边上。

为什么叫「dummy（哑）」？因为互联网的**内部**格子都是被某个 PE 的 `o_a`/`o_b` 驱动的（有「实体」来源），而入口边的格子没有对应的 PE——它们由一段简单的 `always_comb` 赋值驱动，相当于一个「替身」驱动器。源码注释里也把它们称为 *dummy interconnects*，意即「不是真 PE 产生的、用来补齐边界」的连接。

#### 4.2.2 核心流程

dummy 接入用**一个 `generate for` 同时处理两条入口边**（用同一个 `genvar i`），里面放两段 `always_comb`：

```text
对每一行 i（i = 0 .. N-1）：
   水平入口：rowInterConnect[i][0] = i_row[i][0]   // 行矩阵 → 第 i 行首列 PE 的 i_a
   垂直入口：colInterConnect[0][i] = i_col[i][0]   // 列矩阵 → 第 i 列首行 PE 的 i_b
```

两件事看起来都用到下标 `[i]`，但含义不同，要分清：

- 水平入口里 `i` 是**行号**：`rowInterConnect[i][0]` 是「第 `i` 行的入口」，喂给 `PE[i][0]`（第 `i` 行最左的 PE）的 `i_a`。
- 垂直入口里 `i` 是**列号**：`colInterConnect[0][i]` 是「第 `i` 列的入口」，喂给 `PE[0][i]`（第 `i` 列最顶的 PE）的 `i_b`。

合起来，dummy 把行矩阵的 N 个元素接到「最左一列 N 个 PE」的水平输入，把列矩阵的 N 个元素接到「最顶一行 N 个 PE」的垂直输入：

\[
\underbrace{i\_row[i][0]}_{\text{行矩阵第 }i\text{ 行当前元素}} \;\longrightarrow\; \underbrace{rowInterConnect[i][0]}_{\text{水平入口}} \;\longrightarrow\; PE[i][0].i\_a
\]

\[
\underbrace{i\_col[i][0]}_{\text{列矩阵第 }i\text{ 列当前元素}} \;\longrightarrow\; \underbrace{colInterConnect[0][i]}_{\text{垂直入口}} \;\longrightarrow\; PE[0][i].i\_b
\]

> 关于 `[0]` 这一维：`i_row` / `i_col` 的维度是 `[N-1:0][(2*N)-2:0][7:0]`，第二维有 \(2N-1\) 个元素，`[0]` 取的是最低位元素。顶层每拍把行/列矩阵右移 8 位（一个元素），于是 `i_row[i][0]` 就是「当前这一拍该行要送进阵列的元素」。至于这串元素怎么由原始矩阵变换而来，是 u2-l3（行/列矩阵变换）的主题，本讲只需把它当成「每拍喂一个新元素」即可。

#### 4.2.3 源码精读

dummy 接入是文件里第一个 `generate` 块：

[rtl/systolicArray.sv:42-54](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/systolicArray.sv#L42-L54) 用一个 `generate for`（`genvar i = 0..N-1`）同时给水平入口边和垂直入口边各写一段 `always_comb`，分别把 `i_row[i][0]` 接到 `rowInterConnect[i][0]`、把 `i_col[i][0]` 接到 `colInterConnect[0][i]`：

```systemverilog
for (genvar i = 0; i < N; i++) begin: PerDummyRowColInterconnect

  // 水平入口：行矩阵 → 首列 PE 的 i_a
  always_comb
    rowInterConnect[i][0] = i_row[i][0];

  // 垂直入口：列矩阵 → 首行 PE 的 i_b
  always_comb
    colInterConnect[0][i] = i_col[i][0];

end: PerDummyRowColInterconnect
```

几个值得注意的写法：

- **`always_comb` 而非 `assign`**：作者统一用 `always_comb` 描述组合逻辑（这与 PE 内部 `mult`、`o_y` 的风格一致，见 u2-l1）。两者都能表达「持续驱动」，`always_comb` 还会自动推断敏感列表并在多驱动时报警，更适合复杂赋值。
- **`begin: PerDummyRowColInterconnect ... end:` 带标签**：给 `generate` 块取了名字。展开后这 N 份复制会在层次结构里形成 `PerDummyRowColInterconnect[0..N-1]`，方便在波形里定位。
- **同一个 `i` 干两件事**：循环体里两段 `always_comb` 互不干扰，一个负责水平、一个负责垂直，只是恰好都用循环变量 `i` 作下标。

这段代码完成后，阵列的**入口边**就被外部矩阵持续驱动了。剩下的内部格子，则交给下一节的 PE 实例化来逐个连通。

#### 4.2.4 代码实践

**实践目标**：把「同一个 `i`，水平里当行号、垂直里当列号」这件容易绕晕的事，用具体下标固定下来。

**操作步骤**（纸上追踪型）：

1. 取 \(N=4\)，逐个 `i` 写出 dummy 赋值：
   - `i=0`：`rowInterConnect[0][0] = i_row[0][0]`；`colInterConnect[0][0] = i_col[0][0]`
   - `i=1`：`rowInterConnect[1][0] = i_row[1][0]`；`colInterConnect[0][1] = i_col[1][0]`
   - `i=2`：`rowInterConnect[2][0] = i_row[2][0]`；`colInterConnect[0][2] = i_col[2][0]`
   - `i=3`：`rowInterConnect[3][0] = i_row[3][0]`；`colInterConnect[0][3] = i_col[3][0]`
2. 对每个赋值，标注它最终喂给了哪个 PE：
   - 水平入口 `rowInterConnect[i][0]` → `PE[i][0].i_a`（第 `i` 行最左 PE）。
   - 垂直入口 `colInterConnect[0][i]` → `PE[0][i].i_b`（第 `i` 列最顶 PE）。

**需要观察的现象**：`i_row` 的 4 个元素恰好喂给最左一列的 4 个 PE（`PE[0][0]`~`PE[3][0]` 的 `i_a`）；`i_col` 的 4 个元素恰好喂给最顶一行的 4 个 PE（`PE[0][0]`~`PE[0][3]` 的 `i_b`）。两条入口边正交，在 `PE[0][0]` 处交汇。

**预期结果**：你会得到一张「边缘接线图」——左边缘标着 `i_row[0..3]`、上边缘标着 `i_col[0..3]`，内部空着（内部由 4.3 节的 PE 接力填充）。这就是综合实践里完整互联图的「骨架」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 dummy 用 `always_comb` 把 `i_row[i][0]` 接到互联网，而不是再加一个 PE 来产生这个值？

> **答案**：因为入口边的数据来自**外部**（顶层变换好的行/列矩阵），不是阵列内部某个 PE 计算出来的。加一个 PE 反而需要给它喂数据，徒增循环。一段 `always_comb` 把外部信号「转发」到互联网入口，是最直接的边界处理方式。

**练习 2**：`PE[0][0]` 同时被水平入口和垂直入口喂入，它的 `i_a` 和 `i_b` 分别来自哪里？

> **答案**：`i_a = rowInterConnect[0][0] = i_row[0][0]`（行矩阵第 0 行），`i_b = colInterConnect[0][0] = i_col[0][0]`（列矩阵第 0 列）。它是阵列里唯一一个两个输入都直接来自 dummy 入口的 PE。

**练习 3**：dummy 只驱动了入口边，那内部格子（如 `rowInterConnect[1][2]`）由谁驱动？

> **答案**：由 PE 的输出驱动。具体地，`rowInterConnect[i][j]`（\(j \ge 1\)）由 `PE[i][j-1]` 的 `o_a` 写入——这正是下一节 `PerRow`/`PerCol` 实例化时 `.o_a (rowInterConnect[i][j+1])` 的效果。dummy 只负责 \(j=0\)（或 \(i=0\) 的垂直边），其余全部交给 PE。

### 4.3 PerRow/PerCol 嵌套 generate 实例化 PE

#### 4.3.1 概念说明

前两节铺好了网、接好了入口。最后一步：**把 \(N \times N\) 个 PE 放进网格的每个交叉点**，并把它们的 4 个数据端口（`i_a/i_b/o_a/o_b`）连到正确的网格位置。

如果手写，\(N=4\) 就要写 16 个 PE 实例，且每个的连接只差一点点——既繁琐又容易错，改 `N` 时更是灾难。本节的**两层嵌套 `generate`** 把这件事压缩成了几行：外层 `PerRow`（按行）、内层 `PerCol`（按列），循环体里只写**一个** `pe` 实例，编译期自动复制成 \(N \times N\) 份。这是「参数化 + 生成块」组合的典型威力，也是本项目能用一个 `N` 驱动整个阵列规模的核心机制（详见 u3-l2）。

#### 4.3.2 核心流程

两层循环的结构与每个 PE 的连接规则：

```text
对每个行 i（i = 0 .. N-1）:  PerRow
   对每个列 j（j = 0 .. N-1）:  PerCol
       实例化一个 pe，端口连接如下：
         i_clk, i_arst, i_doProcess  ← 全阵列广播（每拍都一样）
         i_a = rowInterConnect[i][j]      ← 读左侧（首列来自 dummy）
         i_b = colInterConnect[i][j]      ← 读上方（首行来自 dummy）
         o_a = rowInterConnect[i][j+1]    ← 写右侧
         o_b = colInterConnect[i+1][j]    ← 写下方
         o_y = o_c[i][j]                  ← 本 PE 的累加结果直通输出矩阵
```

几个要点：

- **三个共享信号广播**：`i_clk`、`i_arst`、`i_doProcess` 对每个 PE 都一样（全阵列同步时钟、同步复位、同步开关），所以直接连模块的输入端口，无需按 `i/j` 区分。`i_doProcess` 正是 u2-l1 讲的「一信号两用」的全阵列共享开关。
- **四个数据端口按坐标区分**：`i_a`/`i_b`/`o_a`/`o_b` 的连接随 `(i,j)` 变化，构成网格内的局部通信。
- **`o_y` 一对一映射到输出**：每个 PE 的累加结果 `o_y` 直接对应输出矩阵的一个元素 `o_c[i][j]`。也就是说，\(N\times N\) 个 PE 的 `o_y` 正好拼成 \(N\times N\) 的结果矩阵。

把这套规则套到 4.1 的坐标上，每个 PE 就像一个「交叉点的焊点」：从左、上读，向右、下写，并向上送出结果。

#### 4.3.3 源码精读

两层嵌套 `generate` 在这里：

[rtl/systolicArray.sv:56-74](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/systolicArray.sv#L56-L74) 用 `PerRow`（`genvar i`）套 `PerCol`（`genvar j`）两层 `generate for`，循环体里实例化一个 `pe`，把 `i_a`/`i_b` 接到 `rowInterConnect[i][j]`/`colInterConnect[i][j]`，把 `o_a`/`o_b` 写到 `rowInterConnect[i][j+1]`/`colInterConnect[i+1][j]`，把 `o_y` 直接连到 `o_c[i][j]`：

```systemverilog
for (genvar i = 0; i < N; i++) begin: PerRow
  for (genvar j = 0; j < N; j++) begin: PerCol

    pe u_pe
    ( .i_clk
    , .i_arst
    , .i_doProcess
    , .i_a (rowInterConnect[i][j])
    , .i_b (colInterConnect[i][j])
    , .o_a (rowInterConnect[i][j+1])
    , .o_b (colInterConnect[i+1][j])
    , .o_y (o_c[i][j])
    );

  end: PerCol
end: PerRow
```

逐条拆解端口连接：

- **`.i_clk` / `.i_arst` / `.i_doProcess`**：只写了端口名、没写括号——这是 SystemVerilog 的**隐式命名连接**简写，等价于 `.i_clk(i_clk)`（端口名与外部信号同名时省略）。三个信号全阵列共享，故无需按坐标区分。
- **`.i_a (rowInterConnect[i][j])`**：从水平网「当前列」读，即来自左侧（首列时左侧就是 4.2 节的 dummy 入口）。
- **`.i_b (colInterConnect[i][j])`**：从垂直网「当前行」读，即来自上方（首行时上方就是 dummy 入口）。
- **`.o_a (rowInterConnect[i][j+1])`**：写到水平网「下一列」，即送给右侧邻居——`j+1` 正是 4.1 节推导出「流方向多一格」的由来。
- **`.o_b (colInterConnect[i+1][j])`**：写到垂直网「下一行」，即送给下方邻居。
- **`.o_y (o_c[i][j])`**：累加结果直接对应输出矩阵第 `i` 行第 `j` 列元素。

> **层次命名**：带标签的 `generate` 块会在展开后形成层次路径。综合/仿真后，第 `(i,j)` 个 PE 的全名大致是 `topSystolicArray.u_systolicArray.PerRow[i].PerCol[j].u_pe`。在 gtkwave 里看 `waveform.vcd` 时，就是按这条路径找到某个具体 PE 的内部信号（如 `mac_q`）——这正是 u2-l1 综合实践里「暴露 PE 内部信号」时要走的路径。

文件最后是两条收尾指令，与 PE 文件首尾呼应：

[rtl/systolicArray.sv:76](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/systolicArray.sv#L76) 与 [rtl/systolicArray.sv:78](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/systolicArray.sv#L78) 的 `endmodule` 后紧跟 `` `resetall ``，恢复模块开头 [rtl/systolicArray.sv:17](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/systolicArray.sv#L17) `` `default_nettype none `` 设置的编译环境，避免这些指令泄漏到后续编译的文件——这是与 `pe.sv` 一致的工程级健壮写法（详见 u2-l1、u3-l2）。

到此，`systolicArray.sv` 的三件事——**铺网、接边、打桩**——全部闭合：互联网就位、入口由 dummy 驱动、内部由 \(N\times N\) 个 PE 的 `o_a`/`o_b` 逐格连通，整张数据流动网络完整可用。

#### 4.3.4 代码实践

**实践目标**：亲手「展开」一次 `generate`，体会「写一个实例 = 复制成 \(N^2\) 个」。

**操作步骤**（纸上展开型）：

1. 取 \(N=4\)，仿照 [systolicArray.sv:56-74](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/systolicArray.sv#L56-L74)，把 `PerRow[0].PerCol[0..3]` 这第一行的 4 个 PE 展开成 4 段显式实例（把 `i=0` 代入，`j` 取 0~3）。
2. 对每个展开后的实例，填出 `i_a`/`o_a` 的具体坐标，例如：
   - `j=0`：`i_a = rowInterConnect[0][0]`，`o_a = rowInterConnect[0][1]`
   - `j=1`：`i_a = rowInterConnect[0][1]`，`o_a = rowInterConnect[0][2]`
   - ……直到 `j=3`：`o_a = rowInterConnect[0][4]`（溢出口）。

**需要观察的现象**：相邻两个 PE 的 `o_a` 与 `i_a` 指向**同一个**坐标（`PE[0][0].o_a = rowInterConnect[0][1] = PE[0][1].i_a`）。这正是「左邻居的输出 = 右邻居的输入」在坐标上的体现——`generate` 写一次就能保证整行首尾相接。

**预期结果**：展开后第一行的 4 个实例构成一条 `rowInterConnect[0][0] → [0][1] → [0][2] → [0][3] → [0][4]` 的链，从 dummy 入口一路流到溢出口。其余三行结构完全相同，只是行号 `i` 不同——这就是「参数化」省下的重复劳动。

#### 4.3.5 小练习与答案

**练习 1**：`.i_clk` 这种只写端口名、不写连接的写法叫什么？它在什么条件下才合法？

> **答案**：叫**隐式命名端口连接**（implicit named port connection），等价于 `.i_clk(i_clk)`。它合法的条件是：外部存在一个**与端口同名**的信号（这里是模块输入端口 `i_clk`）。若外部没有同名信号，就必须写成显式的 `.i_clk(某个信号)`，否则会报错或悬空。

**练习 2**：把 `.o_y (o_c[i][j])` 改成 `.o_y (o_c[j][i])`（行列写反），会对结果造成什么影响？

> **答案**：`o_y` 是每个 PE 的累加结果，对应输出矩阵的一个元素。`PE[i][j]` 本应映射到 `o_c[i][j]`（第 `i` 行第 `j` 列），写反成 `o_c[j][i]` 会让结果矩阵**转置**——每个累加值被放到了镜像位置，输出与期望不符。这说明端口的坐标映射必须与 PE 的行列定义严格一致。

**练习 3**：两层 `generate for` 一共会实例化多少个 PE？如果把内层 `PerCol` 的 `j` 上限误写成 `N`（即 `j < N+1`），会怎样？

> **答案**：正常是 \(N \times N = N^2\) 个。若内层变成 `j` 取 \(0..N\) 共 \(N+1\) 个值，则每行多出一个 PE，且这个多出来的 PE（`j=N`）的 `i_a = rowInterConnect[i][N]`（溢出口，没人写过它，是 X）——既实例化了不存在的第 \(N+1\) 列，又读了未驱动信号，导致规模与功能都错。所以循环上限必须与阵列规模 `N` 严格对齐。

## 5. 综合实践

把三个最小模块串起来，画出 \(4\times4\) 阵列的**完整互联图**——这是本讲的核心实践任务，也是检验你是否真正读懂 `systolicArray.sv` 的试金石。

**实践目标**：用一张图同时体现「水平网 + 垂直网 + dummy 入口 + 溢出口 + \(N^2\) 个 PE 的端口连接」。

**操作步骤**（纸上画图型，参考 [systolicArray.sv:1-15](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/systolicArray.sv#L1-L15) 的注释示意图）：

1. 画一个 \(4\times4\) 的 PE 方阵（16 个方格），标号 `PE[i][j]`，`i` 为行、`j` 为列。
2. **画水平网**：在每行 PE 之间画水平箭头 `-->`，共 4 行。在每个箭头上标 `rowInterConnect[i][j+1]`（例如 `PE[0][0]-->PE[0][1]` 标 `rowInterConnect[0][1]`）。
3. **画垂直网**：在每列 PE 之间画垂直箭头 `|/v`，共 4 列。在每个箭头上标 `colInterConnect[i+1][j]`。
4. **标 dummy 入口**：在最左边缘画 4 个入口，标 `i_row[0..3][0] → rowInterConnect[i][0] → PE[i][0].i_a`；在最上边缘画 4 个入口，标 `i_col[0..3][0] → colInterConnect[0][i] → PE[0][i].i_b`。
5. **标溢出口**：在最右边缘标 `rowInterConnect[0..3][4]`（最右 PE 的 `o_a`，无人读）；在最下边缘标 `colInterConnect[4][0..3]`（最底 PE 的 `o_b`，无人读）。
6. **标输出**：在每个 PE 方格里写 `o_y → o_c[i][j]`。

**需要观察的现象**：

- 从任意一个 dummy 入口（如 `i_row[2][0]`）出发，数据沿水平箭头逐格向右流，每过一格延迟一拍（承接 u2-l1 的 `o_a` 透传）。
- 同理，`i_col` 的数据沿垂直箭头逐格向下流。
- 每个内部 PE 同时有一个水平输入（来自左）和一个垂直输入（来自上），二者在某一拍相遇并相乘累加——这正是矩阵乘法所需的对齐（错峰机制详见 u2-l3）。
- `PE[0][0]` 是唯一两个输入都直接来自 dummy 入口的 PE；其余边缘 PE 只有一个直接入口。

**预期结果**：一张层次清晰的二维网格图，水平网与垂直网正交交织，四条边各司其职（左/上为入口，右/下为溢出口），16 个 PE 各自通过 `o_y` 对应输出矩阵的一个元素。这张图与 [systolicArray.sv:6-15](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/systolicArray.sv#L6-L15) 的注释图一致，且额外标注了坐标与入口/出口。

> 说明：本实践为纯画图推导，无需运行命令，结果可立即自查。若想用波形验证「数据沿对角线推进」，可在 `waveform.vcd` 中按层次路径 `...PerRow[i].PerCol[j].u_pe` 查看相邻 PE 的 `a_q` 波形是否「错开一拍」（见 u2-l1 综合实践），具体波形「待本地验证」。

## 6. 本讲小结

- `systolicArray.sv` 用**两个 packed 多维互联网**承载数据流：水平网 `rowInterConnect [N-1:0][N:0][7:0]`（行内左→右，接 `i_a`/`o_a`）与垂直网 `colInterConnect [N:0][N-1:0][7:0]`（列内上→下，接 `i_b`/`o_b`）。
- 两个互联网的「流方向」都多一格（\(N+1\)），用来容纳最右一列 / 最底一行的**溢出口**（被写却不被读），因此源码用 `lint_off UNUSED` 压掉告警。
- **dummy interconnect**（`PerDummyRowColInterconnect`）用 `always_comb` 把外部行/列矩阵 `i_row[i][0]`、`i_col[i][0]` 接到入口边，驱动最左一列 PE 的 `i_a` 与最顶一行 PE 的 `i_b`。
- **两层嵌套 `generate`**（`PerRow` × `PerCol`）在编译期把一个 `pe` 实例复制成 \(N^2\) 份，每个 PE 按坐标 `(i,j)` 连到互联网的 `[i][j]`（读）与 `[i][j+1]`/`[i+1][j]`（写），`o_y` 直通 `o_c[i][j]`。
- `i_clk`/`i_arst`/`i_doProcess` 全阵列广播（用 `.port` 隐式连接），保证所有 PE 同步时钟、同步复位、同步开关；数据端口才按坐标区分。
- 整个模块首尾用 `` `default_nettype none `` / `` `resetall `` 收尾，与 `pe.sv` 风格一致；改参数 `N` 即可在编译期整体改变阵列规模。

## 7. 下一步学习建议

- **横向 u2-l3（行/列矩阵变换）**：本讲把 `i_row[i][0]`、`i_col[i][0]` 当成「每拍喂一个元素」的黑盒，下一讲回到顶层，拆解 `invertedRowElements`、补零（`APPEND_ZERO`）、按 `i*8` 左移与每拍右移 8 位如何把原始矩阵变成这串喂入元素——它会解释为什么数据能在正确时刻到达正确的 PE。
- **纵向 u2-l4（控制计数器与时钟门控）**：本讲的 `i_doProcess` 是「全阵列广播」的开关，它的产生、`MULT_CYCLES = 3N-2` 的由来、计满后拉低清零的时序，都在那里完整图解。
- **专家 u3-l2（参数化设计与 SV 编码风格）**：本讲的 `generate` + `genvar` + packed 多维数组是参数化的核心，下一阶段会系统总结改 `N` 需要同步修改的所有位置，以及 `` `default_nettype none `` / `` `resetall `` / `$error` 等编码约定。
- **专家 u3-l3（Quartus FPGA 综合）**：本讲实例化的每个 PE 最终会映射成一个 DSP 单元（README 提到「DSP 利用数 = PE 数」），可在那里看到 \(N^2\) 个 PE 落到 Cyclone V 上的资源代价。
