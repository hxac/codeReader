# 累加表 accumTable 与 ReLU 激活输出

## 1. 本讲目标

本讲聚焦扩展架构 `rtl/RTL_modified/top.v` **输出侧**的三个关键部件：累加表 `accumTable`、它的读/写控制 `accumTableWr_control` / `accumTableRd_control`，以及激活阵列 `reluArr`。学完后你应当能够：

- 说清 **为什么** 一个 16×16 的脉动阵列需要一张「累加表」才能算出 128×128 的大矩阵乘；
- 用子矩阵索引 `submat_m` / `submat_n` 与块内行号 `mat_row` 解释 `accumTable` 的地址空间是如何组织的；
- 说明 `accumTableWr_control` 如何把脉动阵列的「列有效」信号 `mmu_col_valid_out` 转成写使能与写地址，`accumTableRd_control` 又如何组合出读地址；
- 指出 `accum_clear`（清空累加表）与 `relu_en`（激活）分别在主机软件调用的哪个阶段生效，并解释 `reluArr` 把结果写进 `outputMem` 的完整链路。

## 2. 前置知识

在进入本讲前，请确认你已掌握以下概念（来自 u6-l1、u6-l2）：

- **分块（tiling）**：大矩阵乘法被切成若干个 16×16 的小块来算。`top.v` 中阵列尺寸 `WIDTH_HEIGHT=16`，而输出矩阵最大边长 `MAX_MAT_WH=128`，二者之比 `MAX_MAT_WH/WIDTH_HEIGHT = 8` 决定了「分块网格」是 8×8。
- **输出固定 vs 权重固定**：核心 `rtl/` 采用输出固定（output-stationary），而本扩展架构采用**权重固定（weight-stationary）**思路——权重经 `weightFifo` 歪斜喂入阵列（见 u6-l2），输入流过阵列，结果送到输出侧的 `accumTable`。
- **主机软件 API**：扩展架构不是「拉高一根 start 引脚就跑完」的独立加速器，而是挂上 Avalon 总线、由 HPS 主机（ARM 核）通过一组指令驱动（见 `instr_set.h`）。本讲的 `accum_clear`、`relu_en`、子矩阵索引等信号，本质上都是主机指令在硬件侧的投影。

> ⚠️ **重要说明（与 u6-l1 一致）**：仓库**只收录了 `top.v`**，本讲涉及的 `accumTable`、`accumTableWr_control`、`accumTableRd_control`、`reluArr`、`outputArr`、`master_control` 等模块的源码文件**都不在仓库内**。因此它们的内部行为只能依据 `top.v` 里的端口连接、`defparam` 参数、行内注释，以及主机软件 API（`instr_set.h`）来**推断**。下文凡涉及模块内部细节（如地址位拼接方式、清零逻辑）且无法从源码直接证实的，都会标注「推断」或「待确认」。可证实的部分（端口、位宽、连接关系、参数）全部来自真实源码。

## 3. 本讲源码地图

本讲只看一个文件，但它的几个片段各司其职：

| 文件 / 片段 | 作用 |
| --- | --- |
| [`rtl/RTL_modified/top.v`](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v) 第 41–43 行 | 顶层参数 `WIDTH_HEIGHT=16`、`DATA_WIDTH=8`、`MAX_MAT_WH=128`，是整个分块设计的尺度基准 |
| 同文件 第 272–286 行 | `accumTable` 例化及其 `defparam`，是本讲的主角 |
| 同文件 第 288–301 行 | `accumTableWr_control`（写控制）例化 |
| 同文件 第 303–312 行 | `accumTableRd_control`（读控制）例化 |
| 同文件 第 314–320 行 | `reluArr`（ReLU 激活阵列）例化 |
| 同文件 第 322–331 行 | `outputMem`（输出存储）例化，是 ReLU 结果的落点 |
| 同文件 第 353–361 行 | `data_mem_calc_done` 的或归约，把 `mmu_col_valid_out` 转成「阵列在产出」的反馈 |
| [`rtl/RTL_modified/software/instr_set.h`](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/software/instr_set.h) | 主机 API，文档化了 `accum_row/accum_col`、`activate`(ReLU)、`clear`(清空) 的语义 |

输出侧数据流主线（本讲全程围绕）：

```
sysArr ──maccout(256b)──> accumTable ──rd_data(256b)──> reluArr ──out(256b)──> outputMem
            │                   ▲                          │
   mmu_col_valid_out(16b)       │                    relu_en（门控）
            │                   │
            ▼                   │
 accumTableWr_control ──wr_en/wr_addr──┘
 master_control ──wr/rd 子矩阵索引──> Wr/Rd_control
 master_control ──accum_clear──> accumTable.clear
```

## 4. 核心概念与源码讲解

### 4.1 accumTable 与分块累加（子矩阵索引）

#### 4.1.1 概念说明

脉动阵列一次只能算一个 16×16 的小块。但我们要算的输出矩阵 \(C = A \times B\) 可以大到 128×128。问题分两层：

1. **空间不够**：输出有 128×128 个元素，阵列一次只产出 16×16。
2. **内维不够**：即便输出块只是 16×16，乘法的内维（contraction 维）\(K\) 也可能远大于 16。例如 \(C[i][j] = \sum_{k=0}^{K-1} A[i][k]\cdot B[k][j]\)，当 \(K=64\) 时，每个 \(C[i][j]\) 要累加 64 项，而阵列一次只能贡献其中连续的 16 项。

`accumTable` 就是用来同时解决这两个问题的**片上暂存 + 累加存储**：

- 它按输出块（sub-matrix）来组织地址，每个块 16×16，整个表最多容纳 \((128/16)\times(128/16) = 8\times 8 = 64\) 个块，即 16384 个元素。
- 它**支持累加**：同一个块地址可以被多次写入而**不是覆盖**，新结果加到旧值上。这样把内维 \(K\) 切成若干 16 长的条带，每算一条带做一次 `tpu_mat_mult`，多次结果就自动累加进同一个块，最终得到完整的点积。

这正是「分块累加（tiled accumulation）」。数学上：

\[
C[i][j] \;=\; \sum_{b=0}^{K/16-1}\;\Big(\sum_{t=0}^{15} A[i][\,16b+t\,]\cdot B[\,16b+t\,][j]\Big)
\]

外层每个 \(b\) 对应一次阵列计算（一次 `tpu_mat_mult`），括号里是阵列一次能算出的 16 项部分和；`accumTable` 把连续多次的部分和**加**到同一个元素位置上。

#### 4.1.2 核心流程

地址由三个量共同决定（均来自 `master_control`）：

- `submat_m`（块行号，0..7）：输出块在 8×8 网格里的**行**。
- `submat_n`（块列号，0..7）：输出块在 8×8 网格里的**列**。
- `mat_row`（块内行号，0..15）：当前正在写入该 16×16 块的**哪一行**。

一次写入会同时写「一整行 16 个元素」（阵列 16 列并行输出），故每个时钟只要选定 `mat_row` + `submat_m` + `submat_n`。三个量的位宽加起来 \(4 + 3 + 3 = 10\)，正好等于每个存储 bank 的地址位宽（见 4.1.3 的位宽推导）。

块内的「列」不需要进地址，因为 16 列被分散到 16 个并行 bank，bank 编号即列号——这和核心 `rtl/` 里 a/b/c 三组 SRAM 用反对角线重排的思路不同，这里改成了**并行多 bank**。

#### 4.1.3 源码精读

先看顶层参数，它们是整个分块设计的尺度基准：

[rtl/RTL_modified/top.v:41-43](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L41-L43) 定义了 `WIDTH_HEIGHT=16`（阵列边长）、`DATA_WIDTH=8`（元素位宽）、`MAX_MAT_WH=128`（最大矩阵边长）。8×8 的分块网格就来自 `MAX_MAT_WH/WIDTH_HEIGHT`。

再看 `accumTable` 的例化：

[rtl/RTL_modified/top.v:272-286](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L272-L286) 是本讲主角。几个关键端口与参数：

```verilog
.clear  ({WIDTH_HEIGHT{reset_global}} | {WIDTH_HEIGHT{accum_clear}}),
.rd_en  ({WIDTH_HEIGHT{1'b1}}),         // FIXME: figure out where this signal should come from
.wr_en  (accumTable_wr_en_in),          // from accumTableWr_control
.rd_addr(accumTable_rd_addr),           // from accumTableRd_control
.wr_addr(accumTable_wr_addr),           // from accumTableWr_control
.rd_data(accumTable_data_out_to_relu),  // to reluArr
.wr_data(accumTable_wr_data)            // from sysArr
```

注意四件事：

1. **清零由两路触发**：`.clear` 是 `reset_global`（全局复位）与 `accum_clear`（来自 `master_control`）的按位或。也就是说，除了上电复位，主机也能在「读走结果之后」主动清空整张表，为下一次独立计算准备干净起点。
2. **读使能常开**：`.rd_en` 恒为全 1（行内 `FIXME` 注释说明作者也不确定该信号应该来自哪里）。读操作受读地址驱动、由上层时序控制，而非由 `rd_en` 门控。
3. **`wr_data` 来自 `sysArr`**：`accumTable_wr_data`（256 位）直接接阵列的 `maccout` 输出，即 16 个 16 位乘加结果。
4. **参数定义了表的形状**：`defparam` 把 `SYS_ARR_ROWS=SYS_ARR_COLS=16`、`DATA_WIDTH=2*DATA_WIDTH=16`（每个累加值 16 位，对应核心 `rtl/` 的 Q8.8 输出）、`MAX_OUT_ROWS=MAX_OUT_COLS=128` 传下去。

地址位宽在端口声明处即可推出（这完全可证实）：

[rtl/RTL_modified/top.v:85](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L85) `accumTable_wr_addr` 的位宽是 `$clog2(MAX_MAT_WH*(MAX_MAT_WH/WIDTH_HEIGHT))*WIDTH_HEIGHT`：

\[
\text{bankAddrBits} = \lceil\log_2(128 \times 8)\rceil = \lceil\log_2 1024\rceil = 10,\quad \text{总线宽} = 10 \times 16 = 160\ \text{位}
\]

即 16 个 bank、每个 bank 10 位地址（最多 1024 个表项）。每个 bank 对应输出块的一列，故「块内列」不需要进地址。配套的 `wr_en` 是 [top.v:86](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L86) `accumTable_wr_en_in`（16 位，每列一个使能），写数据 [top.v:84](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L84) 是 256 位。

子矩阵索引的输入端口位宽也可证实：

[rtl/RTL_modified/top.v:58-59](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L58-L59) `accum_table_submat_row_in` / `accum_table_submat_col_in` 都是 `[$clog2(MAX_MAT_WH/WIDTH_HEIGHT)-1:0]`，即 `[2:0]`，3 位，范围 0..7——正是 8×8 分块网格的索引。

> 推断：`accumTable` 内部应是一组带「读-改-写」能力的寄存器/存储，命中已存数据则做加法、否则直接写入；但该模块源码不在库内，具体的累加实现与地址位拼接顺序**待确认**。

#### 4.1.4 代码实践

**实践目标**：用子矩阵索引把「128×128 大矩阵乘」拆成对 `accumTable` 的访问序列。

**操作步骤**（纯源码阅读 + 纸上推演）：

1. 假设要算 \(C_{128\times128} = A_{128\times64} \times B_{64\times128}\)。
2. 输出分块网格为 8×8，共 64 个 16×16 输出块；内维 64 要切成 \(64/16=4\) 条带。
3. 对每个输出块 \((m, n)\)（`submat_m=m`，`submat_n=n`），需要 4 次 `tpu_mat_mult`，分别喂入 \(A\) 的第 \(m\) 块行、第 \(b\) 块列 与 \(B\) 的第 \(b\) 块行、第 \(n\) 块列（\(b=0..3\)）。

**需要观察的现象**：这 4 次调用的 `accum_row/accum_col`（即 `submat_m/submat_n`）必须**完全相同**，才能让 4 个部分和落到同一个输出块并相加。

**预期结果**：你应该得到「64 个块 × 4 次累加 = 256 次 `tpu_mat_mult` 调用」的调度表；同一块的第 1 次调用之前必须保证该块已被清空（见 4.4 的 `accum_clear`）。

> 待本地验证：仓库不含 `accumTable.v`，故「多次写入是否真的累加而非覆盖」无法仿真确认，只能由软件 API 与架构注释推断。

#### 4.1.5 小练习与答案

**练习 1**：若把 `MAX_MAT_WH` 从 128 改成 256（阵列仍是 16×16），`accum_table_submat_row_in` 需要几位？`accumTable_wr_addr` 每个 bank 的地址需要几位？

**答案**：分块网格变成 \(256/16 = 16\)，所以 `submat_row` 需要 \(\lceil\log_2 16\rceil = 4\) 位；每个 bank 容量变成 \(256 \times 16 = 4096\)，地址需 \(\lceil\log_2 4096\rceil = 12\) 位。

**练习 2**：为什么「块内列号」不出现在 `accumTable` 的地址里？

**答案**：因为输出块的 16 列被并行分散到 16 个独立 bank，列号即 bank 号（隐含在总线分段里），故每个 bank 的地址只需定位「哪个块 + 块内哪一行」。

---

### 4.2 accumTableWr_control 与 accumTableRd_control

#### 4.2.1 概念说明

脉动阵列每拍会在各列产出有效结果，但「有效」是用一根叫 `mmu_col_valid_out` 的 16 位总线表达的——某一位为 1 表示对应列「此刻有可信结果」。`accumTable` 自己并不知道**该写到哪个地址、是否本拍该写**，这两件事分别由两个控制模块完成：

- **`accumTableWr_control`（写控制）**：把 `mmu_col_valid_out` 的有效性，配合当前 `mat_row / submat_m / submat_n`，翻译成「写使能 + 写地址」交给 `accumTable`。
- **`accumTableRd_control`（读控制）**：根据读侧的 `mat_row / submat_m / submat_n`，组合出读地址，把累加好的结果取出来送给 `reluArr`。

注意两者都不做任何「计算」或「激活」，它们纯粹是**地址与使能的发生器**。

#### 4.2.2 核心流程

- 写侧流程：`sysArr` 产出 `accumTable_wr_data` 与 `mmu_col_valid_out` → `accumTableWr_control` 用列 0 的有效位（`mmu_col_valid_out[0]`）作为「一行就绪」的触发 → 结合控制器给的三元索引生成 `accumTable_wr_en_in`（16 位）与 `accumTable_wr_addr`（160 位）→ `accumTable` 在该地址做写入/累加。
- 读侧流程：主机发出「取结果」指令 → `master_control` 给出读侧三元索引 → `accumTableRd_control`（纯组合）算出 `accumTable_rd_addr` → `accumTable` 读出 256 位 → 送 `reluArr`。

#### 4.2.3 源码精读

写控制例化：

[rtl/RTL_modified/top.v:288-301](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L288-L301) 给出了 `accumTableWr_control` 的全部端口连接：

```verilog
.wr_en_in   (mmu_col_valid_out[0]),     // from sysArr
.sub_row    (wr_accumTable_mat_row),    // from master_control
.submat_m   (wr_accumTable_submat_row), // from master_control
.submat_n   (wr_accumTable_submat_col), // from master_control
.wr_en_out  (accumTable_wr_en_in),      // to accumTable
.wr_addr_out(accumTable_wr_addr)        // to accumTable
```

要点：

- **触发源是列 0**：`.wr_en_in` 只取了 `mmu_col_valid_out[0]`。推断：作者用第 0 列的有效信号作为「整行 16 列都已就绪」的代表——因为脉动阵列同一反对角线上的列会同步产出，第 0 列就绪即意味着本拍该写入的整行可写。**待确认**：这是否会在某些非满行（块边界）工况下错写，需看 `accumTableWr_control.v` 内部对 `wr_en_out` 各位的展开逻辑。
- 端口命名有点误导：模块端口叫 `sub_row`，接的却是 `wr_accumTable_mat_row`（块内行号），并非「sub-matrix row」。真正的块行号是 `submat_m`。读代码时要把 `sub_row` 理解成「block-internal row」。
- `wr_en_out` 是 16 位，对应 `accumTable` 的 16 个 bank 各自的写使能。

读控制例化：

[rtl/RTL_modified/top.v:303-312](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L303-L312) 给出了 `accumTableRd_control`。与写控制相比，它**没有 `clk`、没有 `reset`、没有 `wr_en_in`**，只有三个索引输入和一个读地址输出——说明它是**纯组合**的地址发生器：

```verilog
accumTableRd_control accumTableRd_control (
    .sub_row    (rd_accumTable_mat_row),    // from master_control
    .submat_m   (rd_accumTable_submat_row), // from master_control
    .submat_n   (rd_accumTable_submat_col), // from master_control
    .rd_addr_out(accumTable_rd_addr)        // to accumTable
);
```

读/写两套索引分别由 `master_control` 的两组输出驱动，对应的内部 wire 见 [top.v:108-114](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L108-L114)（`wr_accumTable_*` 与 `rd_accumTable_*`）。

补充一个贯穿性的连接点：`mmu_col_valid_out` 这根总线除驱动写控制外，还被 OR 在一起反馈给控制器。见 [top.v:353-361](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L353-L361)：

```verilog
for (i = 0; i < WIDTH_HEIGHT; i=i+1) begin
    // OR MMU column done signals to tell when entire MMU is done
    data_mem_calc_done = data_mem_calc_done | mmu_col_valid_out[i];
end
```

即「只要有任何一列在产出，`data_mem_calc_done` 就为 1」。这个信号回到 `master_control`，参与调度——同一根列有效总线，**一边**触发累加表写入，**一边**告诉控制器「阵列正在工作」。（u6-l1 已指出其语义是「有列在产出」而非「全部完成」。）

#### 4.2.4 代码实践

**实践目标**：理清 `mmu_col_valid_out` 的「一信号两用途」，并标注写地址的来源。

**操作步骤**：

1. 在 `top.v` 中用 `Grep` 搜索 `mmu_col_valid_out` 的所有出现处（应共 3 处：声明、写控制 `wr_en_in`、或归约循环）。
2. 画一张表，列出 `accumTable` 的每个输入端口（`clear / rd_en / wr_en / rd_addr / wr_addr / rd_data / wr_data`）的**来源模块**与**位宽**。

**需要观察的现象**：你会看到写侧（`wr_en`、`wr_addr`）都来自 `accumTableWr_control`，读地址来自 `accumTableRd_control`，而 `wr_data` 直接来自 `sysArr`、`rd_data` 直接送 `reluArr`——`accumTable` 本身不产生任何控制信号，是纯被驱动的存储。

**预期结果**：得到一张「端口 ↔ 来源 ↔ 位宽」对照表，证明三个输出侧模块的职责严格分工：阵列算、控制译址、表存累加。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `accumTableRd_control` 没有 `clk` 和 `reset`，而 `accumTableWr_control` 有？

**答案**：读控制只做组合地址译码（索引 → 地址），无需保存状态；写控制要采样 `mmu_col_valid_out[0]` 并按时钟节拍生成/寄存写使能与地址，是时序部件，故需要 `clk` 与复位。

**练习 2**：若 `mmu_col_valid_out[0]` 因故障恒为 0，`accumTable` 会出现什么现象？

**答案**：`accumTableWr_control` 的 `wr_en_in` 恒为 0，于是写使能永远不打开，累加表不会被写入，最终读出的结果会是清零后的值（或上次残留），计算结果错误。

---

### 4.3 reluArr 激活与 outputMem 写入

#### 4.3.1 概念说明

神经网络的每一层线性变换之后，通常紧跟一个非线性激活函数，最常见的就是 **ReLU**：\( \text{ReLU}(x) = \max(0, x) \)，把所有负值钳到 0。

在扩展架构里，这一步用 `reluArr` 硬件完成——它是 16 路并行的 ReLU 单元，放在 `accumTable` 的**读出侧**与 `outputMem` 之间。也就是说：累加表里的值是「裸的乘加和」，要不要做 ReLU，由主机在「取结果」时决定；做完激活（或不激活）再落盘到 `outputMem`，供主机回读。

#### 4.3.2 核心流程

读出 → 激活 → 落盘三步：

1. `accumTable` 按 `accumTableRd_control` 给的地址读出 256 位（16 个 16 位有符号数）。
2. `reluArr` 在 `relu_en` 使能下，对每个 16 位有符号数做 \(\max(0, x)\)；`relu_en=0` 时原值直通（透传）。
3. 结果（256 位）写入 `outputMem`；主机随后用「回读输出」指令把 `outputMem` 的内容逐行取走。

`relu_en` 由 `master_control` 下发，本质上对应主机 API 里「是否激活」的开关（见 4.4）。

#### 4.3.3 源码精读

`reluArr` 例化：

[rtl/RTL_modified/top.v:314-320](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L314-L320)：

```verilog
reluArr reluArr (
    .en  (relu_en),                          // from master_control
    .in  (accumTable_data_out_to_relu),      // from accumTable
    .out (outputMem_wr_data)                 // to outputMem
);
defparam reluArr.DATA_WIDTH = 2*DATA_WIDTH;  // 16
defparam reluArr.ARR_INPUTS = WIDTH_HEIGHT;  // 16
```

可证实的事实：

- `DATA_WIDTH = 2*DATA_WIDTH = 16`：每个 ReLU 单元处理 16 位有符号数，这与核心 `rtl/` 的 Q8.8 输出（8 整 8 小）一致。
- `ARR_INPUTS = 16`：16 路并行，对应阵列 16 列输出，一拍处理完整一行。
- `.in` 接 `accumTable_data_out_to_relu`（256 位 = 16×16，[top.v:88](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L88)），`.out` 接 `outputMem_wr_data`（也是 256 位，[top.v:89](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L89)）。位宽不变，ReLU 只是把负数清零。

> 推断（`reluArr.v` 不在库内）：当 `en=0` 时 `out = in`（透传），`en=1` 时 `out = (in[15] ? 0 : in)`，即用符号位判断负数。具体实现**待确认**。

`outputMem`（类型 `outputArr`）例化：

[rtl/RTL_modified/top.v:322-331](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L322-L331)：

```verilog
.wr_en  (outputMem_wr_en),                 // from master_control
.wr_data(outputMem_wr_data),               // from reluArr
.wr_addr({WIDTH_HEIGHT{outputMem_wr_addr}}), // from master_control
```

注意写使能与写地址**都来自 `master_control`**，而非来自某个 `wr_control` 子模块（与 `accumTable` 不同）。这说明 `outputMem` 的写入节奏完全由主控制器直接管理，`reluArr` 只是数据通路上的一个组合环节。

#### 4.3.4 代码实践

**实践目标**：确认 ReLU 在数据通路上的位置——它发生在累加「之后」、落盘「之前」，且是否激活由主机决定。

**操作步骤**：

1. 沿 `accumTable_data_out_to_relu` 这根线（[top.v:88](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L88)）追溯：它从 `accumTable.rd_data` 出发，进 `reluArr.in`，从 `reluArr.out` 出来变成 `outputMem_wr_data`，最终进 `outputMem.wr_data`。
2. 在纸上把这条链路画成三段：`accumTable → reluArr → outputMem`，并标出 `relu_en` 是从 `master_control` 侧面接入的「旁路开关」。

**需要观察的现象**：ReLU 与累加表是**串联**的，没有任何反馈回路；`relu_en=0` 时数据应无损直通。

**预期结果**：一张清晰的「读出—激活—落盘」三站图，能解释为什么 ReLU 放在累加表读出侧（而不是阵列输出侧）：因为同一个累加结果可能被读出多次，是否激活属于「输出格式」选择，应由最后取结果的指令决定，而非每次部分和累加时都做。

#### 4.3.5 小练习与答案

**练习 1**：`reluArr` 的 `DATA_WIDTH` 为什么是 `2*DATA_WIDTH`（16 位）而不是 `DATA_WIDTH`（8 位）？

**答案**：阵列的乘加结果是两个 8 位定点数相乘并累加后的值，位宽翻倍到 16 位（Q8.8），与核心 `rtl/` 的量化输出一致；ReLU 作用在这个 16 位有符号结果上，而不是 8 位输入上。

**练习 2**：如果把 `reluArr` 从通路里去掉、把 `accumTable.rd_data` 直接接 `outputMem.wr_data`，功能上损失了什么？

**答案**：丢失了硬件激活能力；后续若需要 ReLU 就得由主机软件逐元素做 \(\max(0,x)\)，既慢又占用 CPU，违背了「把神经网络算子卸载到加速器」的设计初衷。

---

### 4.4 accum_clear 与 relu_en 的生效阶段（串联主机 API）

#### 4.4.1 概念说明

`accum_clear` 与 `relu_en` 是两根来自 `master_control` 的控制线，但它们真正的「语义来源」在主机软件 API（`instr_set.h`）。把它们和软件指令对应起来，才能说清「在什么阶段生效」：

- **`accum_clear`（清空累加表）**：对应两条软件路径——初始化时清表、以及「取完结果后」清表。
- **`relu_en`（ReLU 使能）**：对应「取结果」指令里的「是否激活」开关。

#### 4.4.2 核心流程（软件指令 → 硬件信号）

典型的一次完整计算生命周期：

1. **初始化** `tpu_init()`：发复位、**清空累加表**（对应 `accum_clear` / `reset_global` 生效，`accumTable.clear` 被拉高）。
2. **写输入/权重**：`tpu_rd_input` / `tpu_rd_weight` 把矩阵送进 `inputMem` / `weightMem`。
3. **填权重 FIFO**：`tpu_fill_fifo` 把权重从 `weightMem` 装进 `weightFifo`（u6-l2）。
4. **分块乘累加**（可重复多次）：`tpu_mat_mult(addr, num_rows, num_cols, accum_row, accum_col)` 驱动阵列算一次 16×16，结果**累加**进 `accumTable` 的 `(accum_row, accum_col)` 块。这里 `accum_row→submat_m`、`accum_col→submat_n` 由主机直接给出（顶层端口 `accum_table_submat_row_in/col_in`）。
5. **取结果** `tpu_store_outputs(addr, num_rows, num_cols, accum_row, accum_col, activate, clear)`：从累加表读出指定块，按 `activate` 决定是否做 ReLU（→`relu_en`），写入 `outputMem`；按 `clear` 决定**读后是否清空累加表**（→`accum_clear`）。
6. **回读输出** `tpu_wr_outputs`：把 `outputMem` 的结果逐行取回主机。

#### 4.4.3 源码精读

先看 `accum_clear` 在硬件侧如何汇入清零：

[rtl/RTL_modified/top.v:104](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L104) 声明 `wire accum_clear;`，它由 `master_control` 输出（[top.v:163](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L163) `.accum_clear(accum_clear)`）。最终在 [top.v:274](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L274) 与 `reset_global` 一起 OR 到 `accumTable.clear`。所以清表有两个时机：全局复位、主机主动清空。

`relu_en` 同理来自 `master_control`（[top.v:164](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L164) `.relu_en(relu_en)`），接到 `reluArr.en`（[top.v:315](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L315)）。

再看软件侧的语义证据：

[instr_set.h:4-12](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/software/instr_set.h#L4-L12) `tpu_init` 的注释明确写着「emptying the accumulator table」——这就是 `accum_clear` 在初始化阶段的来源。

[instr_set.h:79-97](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/software/instr_set.h#L79-L97) `tpu_store_outputs` 的参数与注释是本讲最关键的软件证据：

```c
int tpu_store_outputs(uint16_t addr, size_t num_rows, size_t num_cols,
                      int accum_row, int accum_col, int activate, int clear);
```

注释说明：

- `@activate: Perform activation on outputs (1 for ReLU, 0 for none)` —— 这正是 `relu_en` 的语义来源。
- `@clear: Empty the accumulator table after reading (1 to empty, 0 not empty)` —— 这正是「读后清空」，对应 `accum_clear`。

[instr_set.h:61-77](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/software/instr_set.h#L61-L77) `tpu_mat_mult` 的参数 `accum_row / accum_col` 则说明：每次部分矩阵乘的目标块由主机指定，这正是 `submat_m / submat_n`（也即顶层 `accum_table_submat_row_in / col_in`）的来源；多次同块调用即完成内维累加。

#### 4.4.4 代码实践

**实践目标**（对应本讲规格里的实践任务）：解释 `accumTable` 如何让 16×16 阵列算出大矩阵乘，并说清 `accum_clear` 与 `relu_en` 各在哪个阶段生效。

**操作步骤**：

1. 对照上面 4.4.2 的六步生命周期，为每一步标注它驱动了 `top.v` 里的哪些信号（如 `tpu_init → accum_clear`、`tpu_mat_mult → wr_accumTable_* + wr_en`、`tpu_store_outputs → rd_accumTable_* + relu_en + accum_clear`）。
2. 构造一个最小调度剧本：算一个内维 \(K=32\)（即 2 条带）的 16×16 输出块。
   - 第 1 步：保证目标块已清空（`accum_clear` 生效，通常由上一次 `tpu_store_outputs(...,clear=1)` 或 `tpu_init` 完成）。
   - 第 2、3 步：连续两次 `tpu_mat_mult`，`accum_row/accum_col` 相同，分别喂两条带 → `accumTable` 累加 2 次。
   - 第 4 步：`tpu_store_outputs(..., activate=1, clear=1)` → `relu_en` 生效做 ReLU、读后 `accum_clear` 生效清表。

**需要观察的现象**：

- `accum_clear` **绝不**在累加过程中（连续 `tpu_mat_mult` 之间）生效，否则会把已累加的部分和清掉；它只在「开始前」或「全部读完后」生效。
- `relu_en` 只在「取结果」阶段生效，因为它是输出格式选择，不应干扰累加值。

**预期结果**：你应能用一句话总结——「`accum_clear` 在初始化与读后清理两阶段生效；`relu_en` 仅在 `tpu_store_outputs` 取结果阶段生效」。并指出 `accum_clear` 与累加阶段**互斥**这个关键约束。

> 待本地验证：因 `master_control.v` 不在库内，「`accum_clear` 具体在哪一拍被拉高」无法仿真确认，只能依据软件 API 语义推断其阶段归属。

#### 4.4.5 小练习与答案

**练习 1**：如果在两次 `tpu_mat_mult`（同块、不同内维条带）之间误触发了 `accum_clear`，会发生什么？

**答案**：第一次累加的部分和会被清零，第二次的部分和写到一个空表上，最终结果只等于第二条带的部分和，丢失了第一条带的贡献——大矩阵乘结果错误。所以 `accum_clear` 必须避开累加过程。

**练习 2**：主机想要「直接取回原始乘加和、不做 ReLU」，该如何调用？

**答案**：调用 `tpu_store_outputs(..., activate=0, clear=<视情况>)`，使 `relu_en=0`，`reluArr` 透传原值；`clear` 按是否还要复用该块结果决定。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成一次「带分块累加 + ReLU」的端到端追踪。

**任务**：跟踪一个 32×32 输出块（内维 \(K=32\)）从阵列到 `outputMem` 的完整旅程，并回答三个问题。

**背景设定**：

- 输出块位于 8×8 网格的 \((m,n)=(2,3)\)，即 `submat_m=2`、`submat_n=3`。
- 内维 32 要切成 2 条带（\(b=0,1\)），故需 2 次 `tpu_mat_mult`，每次产出 16 项部分和。

**要求**：

1. **分块累加**：写出 2 次 `tpu_mat_mult` 的调用参数（`accum_row/accum_col` 应是什么），说明它们的 `wr_en_in`（来自 `mmu_col_valid_out[0]`）何时拉高、写地址 `wr_addr` 由哪三元索引（`mat_row, submat_m, submat_n`）决定。引用 [top.v:288-301](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L288-L301)。
2. **读出与激活**：写一次 `tpu_store_outputs(..., activate=1, clear=1)`，说明 `accumTableRd_control` 用哪三元索引算出读地址、`reluArr` 在 `relu_en=1` 下对 16 路并行做了什么。引用 [top.v:303-320](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L303-L320)。
3. **阶段判断**：在上述时间线上标注 `accum_clear` 的两个允许生效点（初始化、读后）与一个禁止生效区间（两次累加之间），引用 [top.v:274](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L274) 与 [instr_set.h:79-97](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/software/instr_set.h#L79-L97)。

**交付物**：一张时间轴图，横轴是指令顺序，纵轴分四行（阵列输出 / 写地址三元组 / 累加表状态 / 控制信号 `accum_clear` 与 `relu_en`），在图上能直观看到「累加期间 `accum_clear` 必须为 0、取结果时 `relu_en` 才为 1」。

> 待本地验证：由于相关模块源码缺失，本实践为「源码阅读 + 调度推演」型，无法在仿真器中跑通；若日后取得完整 RTL，可补一个 testbench 验证累加语义与 ReLU 时序。

## 6. 本讲小结

- `accumTable` 是输出侧的**分块累加存储**：用 `submat_m`（块行）、`submat_n`（块列）、`mat_row`（块内行）三元索引定位，让 16×16 阵列通过多次 `tpu_mat_mult` 累加出最大 128×128 的大矩阵乘；其写地址总线宽 160 位（16 bank × 10 位）。
- `accumTableWr_control` 把脉动阵列的列有效信号 `mmu_col_valid_out[0]` 翻译成写使能（16 位）+ 写地址（160 位），是「阵列产出 → 表写入」的桥梁。
- `accumTableRd_control` 是**纯组合**读地址发生器，无 `clk`/`reset`，根据读侧三元索引算出读地址供结果抽取。
- `mmu_col_valid_out` 一信号两用途：既触发写控制，又 OR 归约成 `data_mem_calc_done` 反馈给 `master_control`。
- `reluArr` 是 16 路 16 位并行 ReLU，位于累加表读出侧、`outputMem` 之前，由 `relu_en` 门控（`en=0` 透传、`en=1` 钳负到 0）；是否激活由主机 `tpu_store_outputs` 的 `activate` 参数决定。
- `accum_clear` 只在初始化与「读后清理」两阶段生效，与累加过程**互斥**；它和 `reset_global` 一起 OR 到 `accumTable.clear`。相关模块源码不在库内，内部行为依据端口、参数、注释与 `instr_set.h` 推断。

## 7. 下一步学习建议

本讲把输出侧的「累加—激活—落盘」讲完，扩展架构的数据通路就此闭合。建议下一步：

- **u6-l4（Avalon 总线接口）**：去看 `busConn.v`（`matrixMultiplier` 模块），理解本讲反复提到的 `accum_row/accum_col`、`activate`、`clear` 等主机参数是如何在 Avalon-MM 地址空间里被编码成 `RESET/FILL_FIFO/DRAIN_FIFO/MULTIPLY` 等控制命令、再驱动 `top` 端口的——这将把「软件 API → 总线命令 → 本讲的 `accum_clear`/`relu_en`」整条链路打通。
- 若有机会取得缺失模块源码（`accumTable.v`、`accumTableWr_control.v`、`reluArr.v`、`master_control.v`），重点验证两件事：累加是否真的是「读-改-写加法」而非覆盖；`accum_clear` 在 `master_control` 状态机里的确切生效拍。这两点是本讲所有「推断」中风险最高的。
- 对照核心 `rtl/`（u3-l3 的 `write_out`）：同一件事（把阵列结果写进输出存储）在核心架构里用「a/b/c 三组 SRAM + 反对角线重排」，在扩展架构里改用「多 bank 累加表 + 子矩阵索引」。比较这两种输出策略，能加深对「输出固定 vs 权重固定」两种数据流取舍的理解。
