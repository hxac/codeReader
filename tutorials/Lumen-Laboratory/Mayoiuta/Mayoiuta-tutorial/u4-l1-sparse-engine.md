# 稀疏计算引擎 Sparse Engine

## 1. 本讲目标

本讲进入 Mayoiuta 的「高级加速」环节，精读 `hardware/rtl/sparse/sparse_engine.v` 中的 `Sparse_Engine` 模块。学完后你应当能够：

- 说清楚「稀疏（sparsity）」在神经网络计算中为什么值得专门做一块硬件来处理。
- 解释 `Sparse_Engine` 的三步设计意图：**零值检测 → 稀疏度阈值判断 → 压缩 + 稀疏乘法**。
- 手算一个矩阵的零值比例，并据 `SPARSE_THRESH` 判断 `sparse_valid` 是否置位。
- 识别本模块引用的 `sparse_encoder` 是仓库**未提供源码**的外部子模块（待确认）。
- 如实指出本模块若干「骨架级」写法（非综合、占位语义）上的待确认之处，不臆造其行为。

本讲承接 [u2-l1 处理单元与脉动阵列 PE Array](u2-l1-pe-array.md)：那里讲的是「逐元素不挑、全部乘加」的密集（dense）计算路径；本讲讲的是「遇到零就跳过」的另一条稀疏（sparse）加速路径。两者是 NPU 核内互补的计算选择。

---

## 2. 前置知识

### 2.1 什么是稀疏

一个神经网络里的张量（权重或激活）往往含大量 **0**。例如经过 ReLU 激活后，负值全部变成 0，一张特征图里常常有一半以上是 0。我们用**稀疏度**来衡量「零有多多」：

\[
r = \frac{\text{零值个数}}{\text{总元素个数}}, \qquad 0 \le r \le 1
\]

\(r\) 越接近 1，矩阵越「稀疏」；越接近 0，越「密集」。

### 2.2 为什么要跳过零

任何数乘 0 都得 0，乘 0 再累加还是 0。也就是说：

\[
a \times 0 = 0, \qquad \text{acc} + 0 = \text{acc}
\]

既然如此，对零元素做乘加（MAC）就是**纯浪费功耗和带宽**——把 0 从存储里读出来、送进 PE、做一次乘法、再累加，全程白干。稀疏计算的核心思想就是：**先把零找出来，只对非零元素搬运和计算**。这样在稀疏度高的网络里，能省下可观的算力与能耗。

### 2.3 压缩格式：值 + 索引

跳过零之后，矩阵不再是一个「方方正正、按位置填满」的二维表，而变成「一串非零值 + 它们各自在原矩阵中的位置（索引 index）」。这就是稀疏压缩格式的基本形态：

```
原矩阵:    [3 0 5]      压缩后:  values  = [3, 5, 2]
           [0 2 0]               indices = [(0,0), (0,2), (1,1)]
```

只存非零的值，和它们原来的坐标。后续乘法时按索引去「对齐」相乘即可。

### 2.4 需要的前置术语

- **激活（activation）**：神经网络某一层的输出张量，作为下一层卷积/矩阵乘的输入。
- **权重（weight）**：卷积核或全连接层的参数。
- **MAC**：乘加（Multiply–Accumulate），见 [u2-l1](u2-l1-pe-array.md)。
- **阈值（threshold）**：一条判定线，超过才算「足够稀疏、值得走稀疏路径」。

---

## 3. 本讲源码地图

本讲只涉及一个文件，但其中例化了一个仓库**未提供**的子模块：

| 路径 | 模块 | 作用 | 是否提供源码 |
| --- | --- | --- | --- |
| `hardware/rtl/sparse/sparse_engine.v` | `Sparse_Engine` | 零值检测、稀疏度阈值判断、稀疏矩阵乘法 | ✅ 提供（57 行） |
| （被例化，无对应文件） | `sparse_encoder` | 把矩阵压成「值 + 索引」格式 | ❌ **待确认**（仓库未提供） |

> 提示：`hardware/rtl/sparse/` 目录下**只有** `sparse_engine.v` 一个文件。`sparse_encoder` 在本模块里被例化了两次（[sparse_engine.v:37](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/sparse/sparse_engine.v#L37) 与 [sparse_engine.v:45](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/sparse/sparse_engine.v#L45)），但全仓库搜不到它的 `module` 定义。这一点我们会在 4.3 节详细标注为待确认。

模块的整体端口与参数先看一眼，建立印象：

[sparse_engine.v:1-10](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/sparse/sparse_engine.v#L1-L10) —— 定义 `Sparse_Engine`，声明稀疏度阈值参数 `SPARSE_THRESH = 0.3`，以及 32×32 的 `activation`/`weight` 输入、16 位 `sparse_result` 与 1 位 `sparse_valid` 输出。

---

## 4. 核心概念与源码讲解

### 4.1 稀疏计算动机与模块总览

#### 4.1.1 概念说明

`Sparse_Engine` 想做一件事：**先看一眼这一批数据够不够稀疏，够稀疏就走「跳零」的快路径，不够就走普通的密集乘加**。它对应一个「条件加速」的设计意图：

- 输入：一张 32×32 的激活矩阵和一张 32×32 的权重矩阵（每元素 8 位）。
- 判定：统计两者中零值的总数，超过阈值 `SPARSE_THRESH` 才认为「值得稀疏化」。
- 输出：一个 16 位结果 `sparse_result` 和一个「是否启用了稀疏路径」的标志 `sparse_valid`。

这是一个典型的**运行时自适应**思路：硬件不预先假设数据一定稀疏，而是边看边决定。

#### 4.1.2 核心流程

模块逻辑可拆成三步（其中第 2、3 步在源码里是并行描述的两个 `always` 块与一组例化）：

```
        ┌──────────────────────────┐
activation ──▶│ ① 零值检测 zero_count     │
weight    ──▶│   遍历 32×32，统计零的个数 │
        └────────────┬─────────────┘
                     │ zero_count
                     ▼
        ┌──────────────────────────┐
        │ ② 阈值判断                │
        │  zero_count > 1024*0.3 ? │──▶ sparse_valid
        └────────────┬─────────────┘
                     │ 若足够稀疏
        ┌────────────▼─────────────┐
        │ ③ 压缩 + 稀疏乘法          │
        │  sparse_encoder(值+索引)  │──▶ sparse_result
        │  按索引对齐做乘累加         │
        └──────────────────────────┘
```

#### 4.1.3 源码精读

先看端口与参数（已在第 3 节给出链接），要点：

- `parameter SPARSE_THRESH = 0.3`：稀疏度判定线，30%。
- `input wire [7:0] activation [0:31][0:31]`：**二维非压缩数组**，32 行 × 32 列，每元素 8 位，共 1024 字节。
- `input wire [7:0] weight [0:31][0:31]`：同上。
- `output reg [15:0] sparse_result`：16 位结果。
- `output reg sparse_valid`：稀疏路径有效标志。
- `input wire clk, rst_n`：时钟与低有效复位。

> 待确认①（端口声明）：`rst_n` 被声明却**从未在模块体内使用**，复位语义缺失。

#### 4.1.4 代码实践

**实践目标**：建立「输入规模」的直观量级，为后面的阈值计算做准备。

**操作步骤**：

1. 打开 [sparse_engine.v:6-7](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/sparse/sparse_engine.v#L6-L7)。
2. 手算单个输入矩阵的元素总数：\(32 \times 32 = 1024\) 个 8 位元素。
3. 计算阈值对应的零值个数的「门槛」：\(1024 \times 0.3 = 307.2\)。

**预期结果**：当 32×32 矩阵中零值总数 \(> 307.2\)，即至少 **308** 个零时，理论上应触发稀疏路径。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `SPARSE_THRESH` 调大到 0.9，对模块行为有什么影响？

**答案**：判定线变高，只有当零值超过 \(1024 \times 0.9 = 921.6\)（即至少 922 个零）时才走稀疏路径。绝大多数实际数据达不到这么高的稀疏度，因此 `sparse_valid` 几乎不会置位，模块基本退化成「总是走密集路径」。

**练习 2**：`activation` 与 `weight` 的元素位宽是多少？整张矩阵共占多少 bit？

**答案**：每元素 8 位，共 1024 个元素，所以一张矩阵占 \(8 \times 1024 = 8192\) bit = 1024 字节。`activation` 与 `weight` 合计 2048 字节。

---

### 4.2 零值检测与稀疏度阈值判断（sparse_valid 判定）

#### 4.2.1 概念说明

这是模块「做决定」的部分。它要做两件事：

1. **数零**：遍历 `activation` 和 `weight` 的每个元素，只要任意一方在该位置为 0，就把计数器 `zero_count` 加 1。
2. **比阈值**：若 `zero_count` 超过 \(32 \times 32 \times \text{SPARSE\_THRESH}\)，就把 `sparse_valid` 拉高，表示「这批数据够稀疏，启用稀疏路径」。

注意它的统计口径用的是**逻辑或**：`activation[i][j] == 0 || weight[i][j] == 0`。也就是说，只要这一对乘法 operand 中有一个是 0，这次乘法就是「无效计算」，就该被算进可省去的零里。这比单看某一边更贴近「乘积必然为 0」的真相。

#### 4.2.2 核心流程

判定条件的数学表达：

\[
\text{zero\_count} > 32 \times 32 \times \text{SPARSE\_THRESH}
= 1024 \times 0.3 = 307.2
\]

由于 `zero_count` 是整数，等价于：

\[
\text{zero\_count} \ge 308 \;\Rightarrow\; \text{sparse\_valid} = 1
\]

源码中的伪逻辑：

```
zero_count = 0;
for i in 0..31:
    for j in 0..31:
        if activation[i][j]==0 or weight[i][j]==0:
            zero_count += 1
if zero_count > 307.2:
    sparse_valid <= 1
else:
    sparse_valid <= 0
```

#### 4.2.3 源码精读

零值检测与阈值判断写在同一个 `always @(posedge clk)` 块里：

[sparse_engine.v:12-32](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/sparse/sparse_engine.v#L12-L32) —— 用嵌套 `for` 遍历 32×32，遇零（activation 或 weight 任一为 0）则递增 `zero_count`；随后与 \(32\times32\times\text{SPARSE\_THRESH}\) 比较，决定 `sparse_valid` 与 `sparse_result`。

关键几行单独看：

- [sparse_engine.v:18](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/sparse/sparse_engine.v#L18) —— `if (activation[i][j] == 0 || weight[i][j] == 0)`，逻辑或口径的零值判定。
- [sparse_engine.v:25](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/sparse/sparse_engine.v#L25) —— `if (zero_count > (32*32*SPARSE_THRESH))`，阈值比较，307.2。

> 待确认②（计数器写法，重要）：`zero_count` 的统计逻辑混合了**阻塞**与**非阻塞**赋值。[sparse_engine.v:15](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/sparse/sparse_engine.v#L15) 用阻塞 `zero_count = 0` 清零，而 [sparse_engine.v:19](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/sparse/sparse_engine.v#L19) 的 `zero_count <= zero_count + 1` 用的是非阻塞。在 Verilog 语义下，同一个时钟沿里所有 `<=` 的右值都基于「更新前」的 `zero_count` 求值，于是 1024 次自增并不会真正累加——`zero_count` 实际最多只到 1（只要至少检测到一个零），而不是真实的零值总数。这意味着本节的阈值判断**在真实仿真里几乎永远不成立**。正确的统计写法应把循环里的 `<=` 改成阻塞 `=`，或把统计拆成纯组合逻辑。**本节讲解的「数零 → 比阈值」是设计意图；真实计数行为待自建 testbench 验证。**

> 待确认③（结果赋值，重要）：[sparse_engine.v:28](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/sparse/sparse_engine.v#L28) 写着 `sparse_result <= activation * weight;`，注释说是「使用专用稀疏乘法器」。但 `activation` 与 `weight` 都是 32×32 的二维非压缩数组，Verilog 的 `*` 运算符**不能直接作用于数组**，这一行不可综合、也无法表达矩阵乘。应理解为占位（placeholder），真实稀疏乘法由 4.4 节的第二个 `always` 块与 `sparse_encoder` 协作完成。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：把 32×32 的阈值判定缩小到 4×4，亲手走一遍「数零 → 比阈值 → 判 sparse_valid」的完整流程。

给定一个 4×4 的 `activation`（其余位置理解为也有对应的 `weight`，本练习只看激活一侧；若把 `weight` 视作全非零，则零仅来自 activation）：

```
3  0  5  0
0  2  0  7
1  0  0  4
0  6  0  0
```

**操作步骤**：

1. 数零：逐行统计 0 的个数。
   - 第 1 行 `3 0 5 0`：2 个零。
   - 第 2 行 `0 2 0 7`：2 个零。
   - 第 3 行 `1 0 0 4`：2 个零。
   - 第 4 行 `0 6 0 0`：3 个零。
   - 合计 \(2+2+2+3 = 9\) 个零。
2. 计算稀疏度：\(r = 9 / 16 = 0.5625\)。
3. 套用模块的判定式（把 32×32 换成 4×4 来缩放）：门槛 \(= 4 \times 4 \times 0.3 = 16 \times 0.3 = 4.8\)。
4. 比较：`zero_count(9) > 4.8` 成立吗？

**预期结果**：\(9 > 4.8\) 成立，因此 `sparse_valid` 应置 1，表示这批数据足够稀疏、值得走稀疏路径。

**需要观察的现象**：稀疏度 \(r=0.5625\) 远高于阈值 0.3，所以判定为「稀疏」。如果把矩阵换成只有 1 个零（\(r=1/16=0.0625 < 0.3\)），则 `sparse_valid` 应为 0。

> ⚠️ 待本地验证：以上是**按设计意图**的手算结果。受待确认②影响，真实仿真里 `zero_count` 不会等于 9。若你自建 testbench 跑这段代码，观察到的 `sparse_valid` 行为会与手算不符——这正好印证了待确认②的计数器缺陷。

#### 4.2.5 小练习与答案

**练习 1**：把上面 4×4 矩阵里第 4 行的 `6` 也改成 `0`，重新判断 `sparse_valid`。

**答案**：零值变为 10 个，\(r = 10/16 = 0.625\)，门槛仍为 4.8，\(10 > 4.8\) 成立，`sparse_valid` 仍为 1，且比之前更稀疏。

**练习 2**：若 `activation` 某位置非零、但 `weight` 同位置为零，按源码口径这个位置算不算「零」？

**答案**：算。因为 [sparse_engine.v:18](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/sparse/sparse_engine.v#L18) 用的是 `||`（逻辑或），任一为 0 即计入 `zero_count`——这与「乘积必为 0」的物理含义一致。

---

### 4.3 稀疏压缩：sparse_encoder 与「值 + 索引」格式

#### 4.3.1 概念说明

判定为稀疏后，真正省力的办法不是「带零一起算」，而是先把零**压缩掉**，只保留非零值和它们的位置。负责这件事的就是 `sparse_encoder`——把一个稠密矩阵压成两路输出：

- `compressed`：压缩后的（非零）值。
- `index`：这些值在原矩阵中的索引（位置）。

本模块对 `activation` 和 `weight` 各例化了一个 `sparse_encoder`，分别压缩。

#### 4.3.2 核心流程

```
activation(32×32) ──▶ sparse_encoder ──▶ compressed_act  (值)
                                        act_idx          (索引)

weight(32×32)    ──▶ sparse_encoder ──▶ compressed_weight(值)
                                        weight_idx       (索引)
```

后续的稀疏乘法就用这两对「值 + 索引」去做按位置对齐的乘累加，跳过所有零元素。

#### 4.3.3 源码精读

例化代码如下：

[sparse_engine.v:34-49](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/sparse/sparse_engine.v#L34-L49) —— 声明压缩输出线网，并例化两个 `sparse_encoder`：`encoder` 压缩 `activation`，`encoder_w` 压缩 `weight`。

单独看激活侧的例化：

[sparse_engine.v:37-41](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/sparse/sparse_engine.v#L37-L41) —— `sparse_encoder encoder(.in_data(activation), .compressed(compressed_act), .index(act_idx));`

> 待确认④（子模块源码缺失，**本模块头号待确认项**）：全仓库没有 `module sparse_encoder` 的定义。`hardware/rtl/sparse/` 下只有 `sparse_engine.v` 一个文件，`hardware/` 与 `driver/` 全树搜索也搜不到。因此 `sparse_encoder` 的真实压缩算法（按什么顺序收集非零值、索引如何编码、能否处理 1024 个元素）**完全未知**。本节描述的「值 + 索引」格式是基于端口名 `compressed` / `index` 与通用稀疏存储常识的**合理推断**，属设计意图，不代表该子模块的真实实现。

> 待确认⑤（位宽失配，占位写法）：[sparse_engine.v:35-36](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/sparse/sparse_engine.v#L35-L36) 把 `compressed_act` 声明为仅 8 位、`act_idx` 仅 5 位（范围 0–31）。而输入 `activation` 有 1024 个元素，即便只存非零值，也很难塞进「1 个字节 + 1 个 5 位索引」。这进一步说明当前是一份**骨架/示意**实现，真实压缩格式的位宽与粒度待确认。

> 优点：两个例化分别取名 `encoder` 与 `encoder_w`（[sparse_engine.v:37](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/sparse/sparse_engine.v#L37) 与 [sparse_engine.v:45](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/sparse/sparse_engine.v#L45)），实例名唯一，这点是规范的。

#### 4.3.4 代码实践

**实践目标**：亲手做一次「稀疏压缩」，体会值与索引两路输出的含义。

**操作步骤**：

1. 取如下 4×4 矩阵（与 4.2 同款）：

   ```
   3  0  5  0
   0  2  0  7
   1  0  0  4
   0  6  0  0
   ```
2. 按行优先扫描，挑出所有非零值，并记录其（行,列）坐标。
3. 写成两路：`values` 与 `indices`。

**预期结果**：

```
values  = [3, 5, 2, 7, 1, 4, 6]
indices = [(0,0), (0,2), (1,1), (1,3), (2,0), (2,3), (3,1)]
```

**需要观察的现象**：原矩阵 16 个元素，压缩后只剩 7 个值 + 7 个坐标。若每个值仍用 8 位、坐标用行/列各 2 位（4×4 只需 2 位定位），总 bit 数比原来的 128 bit 少——这就是稀疏压缩省存储与带宽的直观来源。当然，真实 `sparse_encoder` 的索引编码方式待确认（见待确认④）。

#### 4.3.5 小练习与答案

**练习 1**：上面的矩阵如果换成**全非零**（没有 0），压缩后 `values` 有几个元素？此时稀疏压缩还划算吗？

**答案**：`values` 会有 16 个元素、`indices` 也有 16 个，加上索引开销后总信息量比原来的稠密矩阵还多。所以稀疏压缩在**高稀疏度**时才划算，密集数据反而更亏——这正是 4.2 节要先做阈值判断的原因。

**练习 2**：`act_idx` 在源码中是 5 位宽（[sparse_engine.v:36](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/sparse/sparse_engine.v#L36)），它能表示多少个不同的索引值？对 32×32 矩阵够用吗？

**答案**：5 位可表示 \(2^5 = 32\) 个不同值。而 32×32 矩阵有 1024 个位置，定位一个位置至少需要 \(\lceil \log_2 1024 \rceil = 10\) 位。5 位远不够，这印证了待确认⑤——当前位宽只是占位。

---

### 4.4 稀疏矩阵乘法核心

#### 4.4.1 概念说明

拿到两对「值 + 索引」之后，最后一步是做**稀疏乘法**：只有当激活与权重在**同一索引位置**都非零时，才把它们的值相乘并累加。其余位置因为至少有一方是零（被压缩掉了），自然跳过。这就是稀疏矩阵乘法相对密集乘法的省力之处——乘法次数等于「双方非零位置的交集大小」，而不是 \(N^2\)。

#### 4.4.2 核心流程

```
compressed_act   ─┐
act_idx          ─┤
                  ├──▶ 若 act_idx == weight_idx：
compressed_weight─┤      sparse_result <= compressed_act * compressed_weight
weight_idx       ─┘
```

源码用「索引相等」作为触发条件，把对齐位置的值相乘写入 `sparse_result`。

#### 4.4.3 源码精读

第二个 `always` 块描述了这个乘法核心：

[sparse_engine.v:51-56](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/sparse/sparse_engine.v#L51-L56) —— 当 `act_idx == weight_idx` 时，把 `compressed_act * compressed_weight` 写入 `sparse_result`。

要点：

- [sparse_engine.v:53](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/sparse/sparse_engine.v#L53) —— 索引相等才相乘，体现「按位置对齐」的稀疏乘法思想。
- [sparse_engine.v:54](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/sparse/sparse_engine.v#L54) —— 两个 8 位压缩值相乘得 16 位，正好落在 `sparse_result [15:0]`，这一行的位宽是自洽的。

> 待确认⑥（多驱动，重要）：`sparse_result` 同时被**两个** `always @(posedge clk)` 块驱动——[sparse_engine.v:28](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/sparse/sparse_engine.v#L28)（密集分支，且本身不可综合）与 [sparse_engine.v:54](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/sparse/sparse_engine.v#L54)（稀疏分支）。同一个 `reg` 被多个时序块赋值会造成**多驱动冲突**，综合阶段通常报错或行为未定义。规范的写法应合并到同一个 `always` 块、用 `if/else` 互斥分支选择结果来源。

> 待确认⑦（语义简化）：真实稀疏矩阵乘法需要对「双方非零位置交集」做**逐项乘累加**，得到一个标量或一个矩阵。而 [sparse_engine.v:53-54](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/sparse/sparse_engine.v#L53-L54) 只比较了一对索引、做了一次乘法、没有累加器。应把它理解为稀疏乘法核心的**最小示意**，完整 SpMM/SpMV 行为待确认（也依赖待确认④的 `sparse_encoder` 输出格式）。

#### 4.4.4 代码实践

**实践目标**：理解「索引对齐才相乘」的语义，并体会它与密集乘法的差别。

**操作步骤**：

1. 假设 4.3 节压缩后的 `activation` 在位置 `(0,0)` 的值是 `3`。
2. 假设 `weight` 压缩后在位置 `(0,0)` 的值是 `4`，在位置 `(0,2)` 的值是 `5`（别处为 0，已压缩掉）。
3. 对照源码 [sparse_engine.v:53](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/sparse/sparse_engine.v#L53) 的判定：当两边索引都指向 `(0,0)` 时，结果是多少？

**预期结果**：`act_idx == weight_idx`（同为 `(0,0)`）成立，`sparse_result <= compressed_act * compressed_weight = 3 * 4 = 12`。

**需要观察的现象**：只有两边非零位置**重合**才产生有效乘积。`weight` 在 `(0,2)` 虽非零，但若 `activation` 在 `(0,2)` 是零（被压缩掉、不出现在 `act_idx` 里），那个位置就不会触发相乘——省掉了一次乘法。

> ⚠️ 待本地验证：受待确认④⑤⑦影响，真实的索引表示与累加语义无法在源码层面确认。本练习是按「值 + 索引对齐」的通用稀疏乘法模型手算，结果仅作概念演示。

#### 4.4.5 小练习与答案

**练习 1**：为什么稀疏乘法的乘法次数通常远少于密集乘法的 \(N^2\)？

**答案**：因为只有「激活非零且权重也非零」的交集位置才需要相乘。零元素在压缩阶段已被丢弃，既不参与索引比对，也不参与乘法，所以乘法次数约等于双方非零位置的交集大小，远小于 \(N^2\)。

**练习 2**：如果 `act_idx` 与 `weight_idx` 永远不相等，`sparse_result` 会是什么行为？

**答案**：按 [sparse_engine.v:52-56](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/sparse/sparse_engine.v#L52-L56)，`if` 不成立时该块不对 `sparse_result` 赋值，`sparse_result` 保持上一拍的值（寄存器保持特性）。需要注意它仍被另一个 `always` 块驱动（待确认⑥），综合后真实行为待确认。

---

## 5. 综合实践

把本讲三步串起来，做一次「mini 稀疏引擎」的纸上推演。

**任务**：给定 4×4 的 `activation` 与 `weight`，完整走一遍 `Sparse_Engine` 的设计意图流程，并标注每一步哪些是「已实现」、哪些「待确认」。

```
activation:                weight:
3  0  5  0                  0  0  4  0
0  2  0  7                  0  0  0  0
1  0  0  4                  0  0  0  0
0  6  0  0                  0  0  0  0
```

**步骤 1 —— 零值检测（对应 4.2）**：

- `activation` 的零：第 1 行 2 个、第 2 行 2 个、第 3 行 2 个、第 4 行 3 个，共 9 个。
- `weight` 的零：全部 16 个。
- 按源码 `||` 口径，`zero_count` = 任一为 0 的位置数 = **16**（因为 weight 全零，每个位置都计入）。
- 稀疏度 \(r = 16/16 = 1.0\)，远超 0.3，门槛 4.8，故 `sparse_valid = 1`。

**步骤 2 —— 压缩（对应 4.3）**：

- `activation` 压缩后 `values = [3,5,2,7,1,4,6]`，`indices = [(0,0),(0,2),(1,1),(1,3),(2,0),(2,3),(3,1)]`。
- `weight` 全零，压缩后 `values = []`（空），`indices = []`。

**步骤 3 —— 稀疏乘法（对应 4.4）**：

- 双方非零位置的交集为空（weight 没有任何非零位置），因此没有索引能匹配，不产生任何乘法。
- `sparse_result` 保持原值。

**反思与标注**：用一张表总结流程中每一步的实现状态：

| 步骤 | 设计意图 | 源码对应 | 状态 |
| --- | --- | --- | --- |
| ① 数零 | 统计零值个数 | [sparse_engine.v:14-23](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/sparse/sparse_engine.v#L14-L23) | ⚠️ 计数器写法有缺陷（待确认②） |
| ② 阈值判断 | `zero_count > 307.2` | [sparse_engine.v:25](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/sparse/sparse_engine.v#L25) | ✅ 判定式清晰，但因 ① 受影响 |
| ③ 稀疏压缩 | 值 + 索引格式 | [sparse_engine.v:37-49](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/sparse/sparse_engine.v#L37-L49) | ❌ `sparse_encoder` 源码缺失（待确认④）、位宽占位（待确认⑤） |
| ④ 稀疏乘法 | 索引对齐相乘 | [sparse_engine.v:51-56](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/sparse/sparse_engine.v#L51-L56) | ⚠️ 多驱动（待确认⑥）、无累加（待确认⑦） |

**结论**：`Sparse_Engine` 的**设计蓝图清晰**（数零 → 判阈值 → 压缩 → 稀疏乘法），但**当前实现是一份不可综合的骨架**：计数器、`activation*weight`、`sparse_encoder` 缺失、多驱动等问题都需要补全后才能真正运行。本讲把这些环节如实标注，避免读者误以为这是一份能直接上板的代码。

---

## 6. 本讲小结

- **稀疏计算的动机**：神经网络里大量 0，乘 0 白耗功耗；先找零、跳零，只对非零元素搬运与计算，能省算力与能耗。
- **三步设计意图**：`Sparse_Engine` = 零值检测 → 稀疏度阈值判断（`SPARSE_THRESH=0.3`）→ 压缩 + 稀疏乘法。
- **零值口径**：用 `activation==0 || weight==0` 的逻辑或，因为任一为 0 乘积即为 0；32×32 的门槛是 \(1024 \times 0.3 = 307.2\)，即至少 308 个零才置 `sparse_valid`。
- **压缩格式**：把稠密矩阵压成「值 + 索引」两路，由 `sparse_encoder` 完成；后续按索引对齐做稀疏乘法。
- **头号待确认**：`sparse_encoder` 在仓库中**没有源码**，压缩算法完全未知；其压缩输出的位宽（8 位值 + 5 位索引）也只是占位。
- **骨架级缺陷**：计数器混用阻塞/非阻塞（不累加）、`activation*weight` 不可综合、`sparse_result` 多驱动——本模块是设计蓝图而非可直接综合的实现，相关数值结论均待自建 testbench 验证。

---

## 7. 下一步学习建议

- **横向对比密集路径**：回头重读 [u2-l1 PE Array](u2-l1-pe-array.md)，对比「逐元素全乘加」与「跳零稀疏乘」两条路径的适用场景——稀疏引擎在高稀疏度网络（如 ReLU 后的激活、剪枝后的权重）里才划算。
- **看能效**：稀疏省的是功耗，下一篇 [u4-l2 DVFS](u4-l2-dvfs.md) 讲另一条省电思路——动态电压频率调节，把两者放在一起理解「NPU 的能效工具箱」。
- **补全工程视角**：如果你想动手，可以尝试为 `sparse_encoder` 写一个最小定义（按行优先扫描、输出非零值与索引），并修正 4.2 节的计数器写法（循环内改用阻塞 `=`），然后自建 testbench 验证 `sparse_valid` 是否如本讲手算那样置位——这是把这份骨架变成可运行代码的好练手。
- **系统视角**：学完本单元后进入 [u4-l3 全系统数据通路](u4-l3-system-integration.md)，看稀疏引擎如何与存储、重排、PE 阵列一起串成端到端的数据通路。
