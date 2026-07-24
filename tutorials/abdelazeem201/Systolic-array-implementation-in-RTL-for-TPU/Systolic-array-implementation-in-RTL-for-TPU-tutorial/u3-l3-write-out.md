# 输出写回 write_out（a/b/c 三端口）

## 1. 本讲目标

本讲精读 `write_out.v`——TPU 数据通路的最后一站。学完后你应该能够：

- 看懂 `write_out`「一时序块 + 三组合块」的结构，以及 `write_enable = 0 表示写入`这条低有效约定。
- 说清楚 `data_set` 与 `matrix_index` 这两个控制信号如何共同仲裁出当前周期该写 a、b 还是 c，并画出三者的互斥表。
- 理解 `quantized_data` 的 8 个元素如何按 `MAX_INDEX`（即 `ARRAY_SIZE-1`）做反对角线重排（顺序反转）与补零，再拼成 128 位的 `sram_wdata`。
- 解释写地址 `sram_waddr` 在 a/b/c 三组上的生成规则，并回答「为什么一批矩阵乘的结果需要三组输出 SRAM 来承接」。

## 2. 前置知识

- **反对角线（anti-diagonal）**：在 8×8 结果矩阵里，把满足 \(i+j=s\) 的 cell 称为一条反对角线。u2-l3 已说明 `systolic` 每个 `matrix_index` 周期恰好沿「互补反对角线对」挑出 8 个有效结果，打包成 168 位的 `mul_outcome`（即顶层的 `ori_data`）。
- **量化数据 `quantized_data`**：u3-l4 / `quantize.v` 把 21 位中间结果饱和量化成 16 位，8 段拼成 128 位。本讲的输入就是这个 128 位字，元素 i 占据位段 \([i \times 16 +: 16]\)（元素 0 在最低位）。
- **控制器命令**：u3-l1 已说明 `systolic_controll` 在 `ROLLING` 状态、当 `cycle_num >= ARRAY_SIZE+1`（=9）时拉高 `sram_write_enable`，并逐拍推进 `matrix_index`（0→15）与 `data_set`（0→1）。本讲消费这三个信号。
- **低有效（active-low）**：本模块用 0 表示「动作」。SRAM 模型端口名 `wsb`（write strobe bar）的 `b` 就是 bar（取反）的意思，所以 0 才触发写入。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [rtl/write_out.v](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/write_out.v) | 本讲主角。把 `quantized_data` 按 `data_set`/`matrix_index` 重排后写入 a/b/c 三组输出 SRAM |
| [rtl/tpu_top.v](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v) | 顶层例化 `write_out`，把它与 `quantize`（上游）和三组外部 SRAM（下游）连起来 |
| [rtl/systolic_controll.v](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v) | 产生 `sram_write_enable`、`matrix_index`、`data_set` 三个输入信号的「总指挥」 |
| [Pre-Synthesis_Simulation/test_tpu.v](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/Pre-Synthesis_Simulation/test_tpu.v) | 仿真里把 a/b/c 三端口分别接到 7 块输出 SRAM，验证写回正确性 |

---

## 4. 核心概念与源码讲解

### 4.1 模块全貌：时序骨架与「0 表示写」约定

#### 4.1.1 概念说明

`write_out` 是一个**纯写回接口模块**：它不参与任何计算，只负责把上游 `quantize` 给出的 128 位量化结果，按控制器的节拍搬运到外部输出 SRAM 里。它的难点不在算，而在「搬得对」——每拍 8 个元素要拆开、重排、补零，还要决定写到哪块 SRAM、哪个地址、这一拍到底写不写。

为了给外部 SRAM 提供干净、无毛刺的控制信号，模块采用经典的「**组合算下一拍 + 时序寄存器打一拍**」结构：三个组合 `always@(*)` 块各自算出 a/b/c 的「下一拍值」（统一用后缀 `_nx` 表示 next），再由唯一的 `always@(posedge clk)` 块统一寄存到输出端口。

#### 4.1.2 核心流程

```text
控制器输入                         组合逻辑（next）              时序逻辑（posedge clk）
─────────────                     ───────────────              ──────────────────────
sram_write_enable ─┐
data_set ──────────┼──►  always@(*)  ──►  _nx 值  ──►  always@(posedge clk)  ──►  输出端口
matrix_index ──────┤      (a/b/c 三块)                                       (a/b/c)
quantized_data ────┘
```

1. 控制器每拍给出 `sram_write_enable`、`data_set`、`matrix_index`，以及上游来的 `quantized_data`。
2. 三块组合逻辑并行算出 a、b、c 各自的 `_nx`（写使能、写数据、写地址）。
3. 时序块在时钟上升沿把 `_nx` 锁存到真正的输出寄存器。
4. 外部 SRAM 在下一拍看到稳定的写控制信号并完成写入。

#### 4.1.3 源码精读

模块端口声明了 a/b/c 三组结构完全对称的输出，每组含一个写使能、一个 128 位写数据、一个 6 位写地址：

[rtl/write_out.v:17-27](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/write_out.v#L17-L27) — a/b/c 三组输出端口，结构与位宽完全对称。

唯一的 localparam `MAX_INDEX = ARRAY_SIZE - 1`（默认 = 7），是后面元素重排的关键常数：

[rtl/write_out.v:30-44](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/write_out.v#L30-L44) — 声明循环变量 `i`、`MAX_INDEX`，以及所有 `_nx`（next）中间寄存器。

时序块只有一段，干两件事：复位时把所有写使能置 1（=不写，安全默认），其余寄存器清零；否则把 `_nx` 锁进输出：

[rtl/write_out.v:47-74](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/write_out.v#L47-L74) — 唯一的时序块。注意复位分支第 49–51 行把三个写使能都置为 1，即「复位期间绝不写入」。

顶层 `tpu_top` 把它和上游 `quantize`、控制器、三组外部 SRAM 连起来——`write_out` 不带任何额外参数透传，只接收 `ARRAY_SIZE` 与 `OUTPUT_DATA_WIDTH`：

[rtl/tpu_top.v:140-165](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L140-L165) — `write_out` 的例化，`quantized_data` 来自 `quantize`，三个命令信号来自 `systolic_controll`。

> 注意「0 表示写」这条约定贯穿全模块：代码注释 `write_enable_X0 = 0 means write` 反复出现在 [rtl/write_out.v:77](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/write_out.v#L77)、[rtl/write_out.v:120](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/write_out.v#L120)、[rtl/write_out.v:178](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/write_out.v#L178)。读到 `_nx = 0` 就是「这拍要写」，`_nx = 1` 就是「这拍不写」。

#### 4.1.4 代码实践

1. **目标**：确认「0 表示写」约定与复位安全默认。
2. **步骤**：打开 `rtl/write_out.v`，找到第 49–51 行复位分支；再打开 `Pre-Synthesis_Simulation/test_tpu.v` 里输出 SRAM 的例化（如 `sram_16x128b_c0`），看 `.wsb(...)` 接的是哪个信号。
3. **观察**：复位期间三个 `sram_write_enable_*0` 都是 1，对应 SRAM 的 `wsb=1` 不写。
4. **预期**：上电复位阶段不会发生任何误写入；只有控制器进入 `ROLLING` 且 `cycle_num>=9` 后，`write_out` 才可能把某组使能拉到 0。

#### 4.1.5 小练习与答案

**练习 1**：为什么把写使能设计成低有效（0=写），而不是高有效？
**答案**：与外部 SRAM 模型的 `wsb`（write strobe bar）端口直接对接，省去一级取反；同时复位默认值 1 表示「不写」，是安全默认，避免上电瞬间误写入。

**练习 2**：模块只有一段 `always@(posedge clk)`，其余三段都是 `always@(*)`。如果把这三段也改成时序逻辑，会有什么副作用？
**答案**：会多引入一拍延迟，导致 `quantized_data` 与写使能/地址错位，写到错误地址；当前设计刻意让组合块「算下一拍」、时序块「统一打一拍」，既保证信号干净又只引入恰好一拍的确定性延迟。

---

### 4.2 a/b/c 三段写逻辑：data_set × matrix_index 的互斥仲裁

#### 4.2.1 概念说明

三块组合逻辑结构几乎一样，唯一的区别是**各自认领不同的 `(data_set, matrix_index)` 区间**。可以把 a/b/c 想成三个值班窗口：

- **a 窗口**只服务 `data_set==0` 这一趟；
- **c 窗口**只服务 `data_set==1` 这一趟；
- **b 窗口**是个「跨界窗口」，专门承接两趟交接处的三角形结果。

控制器让 `matrix_index` 在每趟内从 0 数到 15。a 和 c 各自认领本趟的全部 16 个 `matrix_index`；b 则只认领每趟里「半数」的 `matrix_index`，并且在两趟里认领的是不同的半段。这样设计的结果是：**任意一个时钟周期，最多只有两个窗口同时写（且写向不同 SRAM、不同地址），绝不会出现 a 与 c 同写、或三组齐写的冲突。**

#### 4.2.2 核心流程

把三块逻辑的使能条件整理成下面这张**互斥仲裁表**（✓=本拍写该 SRAM，✗=不写）：

| `data_set` | `matrix_index` | a 组 | b 组 | c 组 | 同时写的组 |
|------------|----------------|------|------|------|------------|
| 0 | 0..7（正常型） | ✓ | ✗ | ✗ | 仅 a |
| 0 | 8..15（混合型） | ✓ | ✓ | ✗ | a + b |
| 1 | 0..7（正常型） | ✗ | ✓ | ✓ | b + c |
| 1 | 8..15（混合型） | ✗ | ✗ | ✓ | 仅 c |

关键不变量：**a 与 c 永不共存**（它们由不同的 `data_set` 互斥）；混合型周期里 a+b 或 b+c 成对出现，但写地址不同（见 4.4）。

#### 4.2.3 源码精读

**a 组**只认 `data_set==0`：在 `case(data_set)` 里只写了 `0:` 分支，其余落到 `default:` 即关闭写：

[rtl/write_out.v:78-118](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/write_out.v#L78-L118) — a 组逻辑。`data_set==0` 内再按 `matrix_index < ARRAY_SIZE` 分「正常型」（第 82–91 行）与「混合型」（第 92–101 行）；`default` 与 `sram_write_enable==0` 时一律关闭。

**c 组**结构与 a 完全镜像，只是认 `data_set==1`（`case` 里只写 `1:` 分支）：

[rtl/write_out.v:179-219](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/write_out.v#L179-L219) — c 组逻辑，是 a 组的 `data_set` 镜像。

**b 组**最特殊，同时跨两趟：`data_set==0` 时只接「混合型」（`matrix_index>=8`），`data_set==1` 时只接「正常型」（`matrix_index<8`）：

[rtl/write_out.v:121-176](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/write_out.v#L121-L176) — b 组逻辑。注意第 124–130 行（`data_set==0` 且 `matrix_index<8`）和第 154–159 行（`data_set==1` 且 `matrix_index>=8`）都明确把使能置 1（不写），把那段区间「让给」a 或 c。

> 三块开头都有同一道总闸：`if(sram_write_enable) ... else 关闭`。这个 `sram_write_enable` 来自控制器，只在 `cycle_num>=9` 的写回阶段为 1。所以阵列灌满的前 9 拍里，a/b/c 全部静默——这就是 u3-l1 里「灌满 9 拍不写」在写回侧的体现。

#### 4.2.4 代码实践

1. **目标**：亲手验证互斥表，回答「为什么需要三组输出 SRAM」。
2. **步骤**：
   - 在 `rtl/write_out.v` 里分别定位 a/b/c 三块的 `case(data_set)` 分支与使能赋值。
   - 对 `data_set∈{0,1}` × `matrix_index∈{0,7,8,15}` 这 8 个代表点，逐一判断 a/b/c 各自的 `sram_write_enable_*0_nx` 是 0 还是 1。
3. **观察**：确认 a 与 c 永远不同时为 0；混合型周期里 a+b 或 b+c 成对为 0。
4. **预期结果**：得到与本讲 4.2.2 完全一致的互斥表。
5. **为什么需要三组 SRAM（写在实践报告里）**：在一个 `matrix_index` 周期里，阵列吐出的 8 元素反对角线有时要被**拆成两半**分别落到两个输出区域（正常型那半 + 混合型那半），而这两半必须在**同一拍**并行写下去——阵列下一拍就吐新结果，不能停。一拍要并行写两处，就至少需要两个写端口；再加上 a/c 分属两趟 `data_set`、b 要跨趟共享，三个区域又各有独立的地址空间，于是天然需要 a/b/c **三组**输出 SRAM 才能无冲突地接住全部反对角线结果。

#### 4.2.5 小练习与答案

**练习 1**：`data_set==0` 且 `matrix_index==3` 时，哪几组在写？`data_set==1` 且 `matrix_index==3` 时呢？
**答案**：前者只有 a 组写（正常型，b/c 关闭）；后者是 b + c 同时写（b 接 `data_set==1` 的正常型区间，c 也写）。

**练习 2**：为什么 a 组的 `case` 里没有 `1:` 分支，c 组没有 `0:` 分支？
**答案**：a 专属第 0 趟、c 专属第 1 趟，二者用 `data_set` 天然分工；不属于自己的趟次直接落到 `default:` 关闭写，避免跨趟污染对方的 SRAM。

**练习 3**：把控制器给出的总闸 `sram_write_enable` 拿掉（恒为 1）会怎样？
**答案**：阵列灌满的前 9 拍里 `quantized_data` 还是无效/垃圾值，会被提前写进输出 SRAM，破坏结果；这道总闸保证只有首个有效输出就绪（`cycle_num>=9`）之后才允许写回。

---

### 4.3 MAX_INDEX 反对角线重排与补零

#### 4.3.1 概念说明

仲裁解决了「写到哪块 SRAM」，本节解决「**128 位的 `sram_wdata` 里每个元素摆在什么位置**」。答案是两条规则：

1. **顺序反转**：`quantized_data` 的元素 i（位于位段 \([i\times16 +: 16]\)，元素 0 在最低位）被放到 `sram_wdata` 的位段 \([(MAX\_INDEX-i)\times16 +: 16]\)，也就是位置 \(7-i\)。元素 0 → 最高位段，元素 7 → 最低位段——整条字顺序被反转。
2. **补零**：不是每个位置都有有效元素。本拍反对角线上有效元素的数量取决于 `matrix_index`，无效位置统一填 0。

之所以反转，是因为 `quantized_data` 的段号 = 阵列**行号 i**（u2-l3），而外部 SRAM 把 128 位字当作一列/一行存储时，习惯把行号小的放在高位（地址空间的「上方」），反转后读回的顺序才与矩阵的自然行列顺序一致。

#### 4.3.2 核心流程

`matrix_index` 把一趟切成长度各 8 的两段，重排规则随之切换：

- **正常型**（`matrix_index < ARRAY_SIZE`，即 0..7）：是一条**不断变长**的反对角线。`matrix_index=k` 时有 \(k+1\) 个有效元素（条件 `i <= matrix_index`），其余补零。像在右上角逐步长出一个三角形。
- **混合型**（`matrix_index >= ARRAY_SIZE`，即 8..15）：是一条**不断变短**的反对角线。`matrix_index=k` 时有效元素个数随 k 增大而减少（条件 `i < 15-matrix_index`），像左下角逐步收缩的三角形。

两段合起来，正好把一趟的全部反对角线结果填满，每条线再按 7-i 反转摆位。

#### 4.3.3 源码精读

a 组正常型的重排与补零（条件 `i <= matrix_index` 决定取量化数据还是填 0，目标位置恒为 `(7-i)*16`）：

[rtl/write_out.v:84-90](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/write_out.v#L84-L90) — 正常型：`i<=matrix_index` 的位置取 `quantized_data[i*16 +: 16]`，否则填 0；地址 `= matrix_index`。

a 组混合型（条件改成 `i < 15-matrix_index`，且数据源下标偏移成 `i+1+(matrix_index-ARRAY_SIZE)`）：

[rtl/write_out.v:94-100](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/write_out.v#L94-L100) — 混合型：有效条件 `i < 15-matrix_index`，数据源 `(i+1+(matrix_index-8))*16`；地址仍是 `matrix_index`。

b 组的两段也遵循同样的「反转 + 补零」，只是认领的元素下标区间不同（data_set=0 混合型取前段 `i`、data_set=1 正常型取偏移段 `i+1+matrix_index`）：

[rtl/write_out.v:133-139](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/write_out.v#L133-L139) — b 在 `data_set==0` 混合型：条件 `i <= matrix_index-8`，源 `i*16`。

[rtl/write_out.v:146-152](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/write_out.v#L146-L152) — b 在 `data_set==1` 正常型：条件 `i < 7-matrix_index`，源 `(i+1+matrix_index)*16`。

> 一个细节：`15` 与 `7` 这两个常数其实是 `2*ARRAY_SIZE-1` 与 `ARRAY_SIZE-1`，但代码里写死了数字（见第 95、147 行）。这与 u1-l4 指出的「半参数化」一致——位宽骨架随 `ARRAY_SIZE` 走，但这些边界判断仍按 8×8 写死，直接改 `ARRAY_SIZE` 这里不会自动跟随。

#### 4.3.4 代码实践

1. **目标**：手算一条反对角线的重排结果。
2. **步骤**：取 a 组、`data_set==0`、`matrix_index==2`（正常型）。设 `quantized_data` 8 个元素依次为 \(e_0,e_1,\dots,e_7\)。
3. **观察**：对 i=0..7 逐一套用规则 `i<=2 ? 取 e_i : 0`，目标位置 `(7-i)*16`。
4. **预期结果**：`sram_wdata_a` 的 8 个位段（从高位到低位）依次为 \(e_0,e_1,e_2,0,0,0,0,0\)，写地址 `sram_waddr_a = 2`。可以看到元素顺序相对 `quantized_data`（\(e_0\) 在 LSB）被反转到了高位。
5. 待本地验证：若用仿真器跑，可在 `write_out` 输出端 dump `sram_wdata_a` 与 `sram_waddr_a` 对照。

#### 4.3.5 小练习与答案

**练习 1**：a 组 `matrix_index==7`（正常型最后一拍）时，`sram_wdata_a` 8 个位段分别是什么？
**答案**：`i<=7` 全部满足，位段（高位→低位）为 \(e_0,e_1,e_2,e_3,e_4,e_5,e_6,e_7\)，无补零；这是反对角线最长的一拍。

**练习 2**：a 组混合型 `matrix_index==15` 时会写入几个有效元素？
**答案**：条件 `i < 15-15 = 0`，没有任何 i 满足，全部补零——这是混合型收缩到最短（空）的一拍，但仍会产生一次写（写全 0），地址 `= 15`。

---

### 4.4 写地址生成与「为何需要三组 SRAM」

#### 4.4.1 概念说明

每组每拍除了决定「写什么数据」，还要决定「写到哪个地址」。a 与 c 的地址规则简单：**直接用 `matrix_index` 当地址**，所以各自占满 0..15 共 16 个地址。b 组最巧妙：它在两趟里分别写到**地址空间的上下两半**，从而把两趟的跨界三角形拼成连续的 16 个地址。

- b 在 `data_set==0` 混合型：地址 `= matrix_index - ARRAY_SIZE`（把 8..15 映射到 0..7）。
- b 在 `data_set==1` 正常型：地址 `= matrix_index + ARRAY_SIZE`（把 0..7 映射到 8..15）。

这三套地址规则共同保证：**任意一块 SRAM 的同一个地址，在一趟完整计算里至多被写一次**，不会发生后写覆盖前写。

#### 4.4.2 核心流程

三组 SRAM 的地址空间占用示意（每个格子是一个 128 位字，共 16 个地址）：

```text
地址:    0 1 2 3 4 5 6 7 8 9 ... 15
a-SRAM: [     data_set=0, 正常型+混合型，waddr=matrix_index(0..15)      ]
b-SRAM: [ ds=0 混合型(0..7)            | ds=1 正常型(8..15)              ]
c-SRAM: [     data_set=1, 正常型+混合型，waddr=matrix_index(0..15)      ]
```

时间上：data_set=0 阶段写 a 全部 16 字 + b 的低 8 字；data_set=1 阶段写 c 全部 16 字 + b 的高 8 字。b 的低半与高半在不同时间、不同地址写入，互不覆盖。

#### 4.4.3 源码精读

a/c 的地址恒等于 `matrix_index`（无论正常型还是混合型）：

[rtl/write_out.v:90](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/write_out.v#L90) 与 [rtl/write_out.v:100](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/write_out.v#L100) — a 组两种类型的地址都是 `matrix_index`。

b 组的「跨半」地址：

[rtl/write_out.v:139](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/write_out.v#L139) — b 在 `data_set==0` 混合型：地址 `= matrix_index - ARRAY_SIZE`（落入 0..7）。

[rtl/write_out.v:152](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/write_out.v#L152) — b 在 `data_set==1` 正常型：地址 `= matrix_index + ARRAY_SIZE`（落入 8..15）。

控制器的 `matrix_index`/`data_set` 推进规则，决定了上述地址被遍历的时序：

[rtl/systolic_controll.v:157-175](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v#L157-L175) — `ROLLING` 中 `cycle_num>=ARRAY_SIZE+1` 后开始递增 `matrix_index`（0→15），到 15 后清零并让 `data_set+1`，同时拉高 `sram_write_enable`。

#### 4.4.4 代码实践

1. **目标**：把地址生成与时序串起来，确认「无地址冲突」。
2. **步骤**：
   - 列出 `data_set=0` 全过程（`matrix_index` 0→15）中 a 与 b 各自写过的地址集合。
   - 再列出 `data_set=1` 全过程中 b 与 c 各自写过的地址集合。
3. **观察**：a 在第 0 趟写地址 {0..15}；b 在第 0 趟写 {0..7}、第 1 趟写 {8..15}；c 在第 1 趟写 {0..15}。
4. **预期结果**：同一块 SRAM 的任一地址在一趟内只被写一次；不同 SRAM 之间的同周期并行写（a+b 或 b+c）地址都不同。
5. **结论**：三组 SRAM 既提供了「同拍双写」所需的两个端口，又用 a/c 分趟 + b 跨半的地址分配避免了任何覆盖，这正是「需要三组」的完整理由。

#### 4.4.5 小练习与答案

**练习 1**：b 组在 `data_set==0`、`matrix_index==11` 时，写地址是多少？
**答案**：`matrix_index - ARRAY_SIZE = 11 - 8 = 3`（落入 b-SRAM 低半）。

**练习 2**：如果只剩两块输出 SRAM（去掉 c），还能正确写回吗？
**答案**：不能。`data_set==1` 的结果将无处可写（a 专属第 0 趟、b 已被第 0 趟尾部占用低半且要承接第 1 趟头部高半），第 1 趟的正常型/混合型结果会丢失或被错误覆盖。三组是兼顾「同拍双写端口」与「两趟 + 跨界」地址空间的最小配置。

---

## 5. 综合实践

把本讲四节串起来，完成一次「**纸上端到端追踪**」：

1. 假设控制器刚进入写回阶段，给出 `sram_write_enable=1`。
2. 自选两个有代表性的拍：一拍 `data_set=0, matrix_index=5`（a 正常型），一拍 `data_set=0, matrix_index=10`（a+b 混合型）。
3. 对每一拍，写出：
   - a/b/c 各组的 `sram_write_enable_*0_nx`（0 或 1）；
   - 被写入的 SRAM 及其 `sram_waddr`；
   - `sram_wdata` 中哪些位段是有效量化元素（用 \(e_i\) 表示）、哪些补零（按 7-i 反转后的顺序）。
4. 最后用一段话说明：这两拍如何共同体现「同拍最多双写」「地址不冲突」「元素反转 + 补零」三条性质。

如果本地有 Icarus Verilog 等仿真器，可进一步在 `Pre-Synthesis_Simulation/test_tpu.v` 的输出 SRAM（`sram_16x128b_c0/c1/c2` 等）读端口加临时 `$display`，把仿真值与你的纸上推算对照（待本地验证）。

## 6. 本讲小结

- `write_out` 是纯写回接口：**一时序块统一打一拍 + 三组合块算下一拍**，复位默认「不写」（写使能为 1）。
- 写使能遵循 **「0 表示写」**的低有效约定，且受控制器总闸 `sram_write_enable`（仅 `cycle_num>=9` 为 1）全局门控。
- a/b/c 三组由 **`data_set` × `matrix_index` 互斥仲裁**：a 专属第 0 趟、c 专属第 1 趟、b 跨趟承接交界三角形；同拍最多 a+b 或 b+c 双写，a 与 c 永不共存。
- 元素按 **`MAX_INDEX`（=7）做顺序反转**（元素 i → 位置 7-i），并按 `matrix_index` 分正常型/混合型做**补零**，构成 128 位 `sram_wdata`。
- 地址生成：a/c 用 `matrix_index` 直寻址（0..15），b 用 `matrix_index∓8` 跨半寻址；三组共同保证「同地址一趟内只写一次」。
- 「为何三组」= 同拍双写需要两端口 + 两趟/跨界需要三个独立地址空间，三者是最小配置。

## 7. 下一步学习建议

- **向后**：阅读 u3-l4（`quantize` 饱和量化），理解 `quantized_data` 这 128 位是怎么从 21 位中间结果压出来的，与本讲的「写什么」首尾相接。
- **闭环**：进入 u4 单元（端到端仿真），看 `test_tpu.v` 如何把 a/b/c 三端口接到 7 块输出 SRAM，并用 `golden` 参考做逐地址比对——那是验证本讲写回正确性的最终裁判。
- **进阶**：对比 u6-l3 扩展架构里的 `accumTable` + `outputMem` 写回方案，体会「固定阵列尺寸 + 分块累加」与本项目「反对角线流式写回 + 三组 SRAM」两种思路的差异。
