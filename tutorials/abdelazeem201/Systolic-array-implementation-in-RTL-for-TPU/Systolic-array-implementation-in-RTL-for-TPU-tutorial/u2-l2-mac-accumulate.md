# MAC 乘加计算与累加时序

## 1. 本讲目标

上一讲（u2-l1）我们看清了 `weight_queue` / `data_queue` 两个移位队列如何把 weight 自上而下、data 自左而右地灌进 8×8 阵列，并在每个 cell 里制造出到达时间差。本讲要回答紧接着的下一个问题：**每个 cell 拿到本地的 weight 和 data 之后，究竟在哪个时钟周期把它们相乘？乘完是「新起一项」还是「累加到旧值」上？什么时候保持不动？**

读完本讲你应该能够：

1. 读懂 `systolic.v` 里那块组合 `always@(*)` 乘加逻辑，说清 `mul_result = weight_queue[i][j] * data_queue[i][j]` 的含义。
2. 说清 `cycle_num` 是怎样像一道「闸门」一样，把每个 cell 在每个周期分流到**首入 / 累加 / 保持**三条分支之一。
3. 推导出 cell 的反对角线编号 \(s=i+j\) 与 `cycle_num` 的关系，解释**为什么 cell(i,j) 要等到第 \(s+1\) 个周期才开始累加、累加满 8 个乘积后又在第 \(s+9\) 个周期「首入」下一项**。
4. 理解 `matrix_mul_2D`（时序寄存器）与 `matrix_mul_2D_nx`（组合下一拍）这套「双份」写法的配合，以及符号扩展 `{5{mul_result[15]}}` 为什么能把 16bit 乘积安全地塞进 21bit 累加器。

---

## 2. 前置知识

在进入源码前，先用三段话把本讲要用到的直觉补齐。

**(1) MAC = Multiply–Accumulate。** 神经网络里最频繁的运算是 \(C = A\times B\) 的矩阵乘，本质是成千上万次「取一对数、相乘、再加到累计值上」。把这三步打包成一个硬件单元，就叫 MAC 单元。本项目的每个 cell 就是一个 MAC：本地存着当前的 weight 和 data，每拍做一次乘法并把结果累加进一个寄存器。

**(2) 点积需要 8 次累加。** 两个 \(8\times8\) 矩阵相乘，结果矩阵的每一个元素 \(C[i][j]\) 是一行与一列的点积，即 8 个乘积之和。所以**每个 cell 必须连续累加恰好 `ARRAY_SIZE=8` 个乘积，才得到一个完整的输出元素**。本讲的全部时序都是围绕「8 个乘积一组、一组算完立刻开始下一组」这件事设计的。

**(3) 反对角线决定波前。** 由于 weight 滞后 `i` 拍、data 滞后 `j` 拍才到达 cell(i,j)（见 u2-l1），位于同一反对角线 \(i+j=s\) 上的所有 cell 会**同时**进入同一阶段。所以整个阵列的运算像一道「波」从左上角 \(s=0\) 向右下角 \(s=14\) 推进。本讲你会看到，`cycle_num` 与 `s` 的关系正是用这道波来表达的。

> 关于定点格式：weight/data 是 8 位有符号 Q4.4，单次乘积是 16 位 Q8.8，累加器是 21 位 Q13.8。这些在 u1-l4 已建立，本讲第 4.4 节会再结合符号扩展落地一次。

---

## 3. 本讲源码地图

本讲只盯一个文件：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `rtl/systolic.v` | 8×8 脉动阵列本体 | 第 26–28 行的 `FIRST_OUT`/`PARALLEL_START`/`OUTCOME_WIDTH`；第 30–35 行的累加/乘积寄存器声明；第 79–90 行的时序寄存块；第 92–118 行的组合乘加块 |

为佐证「`cycle_num` 是自由递增计数器、`ARRAY_SIZE+1` 是首个输出出现的节拍」，本讲还会附带引用控制器 `rtl/systolic_controll.v` 中产生 `cycle_num` 的片段（控制器本身的精读在 u3-l1）。

本讲**不**重复 u2-l1 已讲过的移位队列填入与移位逻辑（第 44–76 行），也**不**展开第 121–151 行的结果收集逻辑（那是 u2-l3 的主题）。本讲的边界就是：从「队列里的值」到「`matrix_mul_2D` 累加器里的值」之间这段乘加决策。

---

## 4. 核心概念与源码讲解

### 4.1 时序/组合双份设计：matrix_mul_2D 与 matrix_mul_2D_nx

#### 4.1.1 概念说明

先看清楚累加结果存在哪里。`systolic.v` 声明了**两个**内容相同的二维寄存器阵列：

```verilog
reg signed [OUTCOME_WIDTH-1:0] matrix_mul_2D    [0:ARRAY_SIZE-1][0:ARRAY_SIZE-1]; // 时序：当前值
reg signed [OUTCOME_WIDTH-1:0] matrix_mul_2D_nx  [0:ARRAY_SIZE-1][0:ARRAY_SIZE-1]; // 组合：下一拍值
```

这是一种非常标准的 RTL 写法——把「下一拍该变成什么」（组合，后缀 `_nx` = next）和「现在是什么」（时序寄存器）分开：

- `matrix_mul_2D_nx[i][j]`：由组合 `always@(*)` 算出来，代表「如果现在就打一拍，cell(i,j) 的累加值应该变成多少」。
- `matrix_mul_2D[i][j]`：真正的寄存器，在每个时钟上升沿把 `_nx` 的值「提交」进来。

这样做最大的好处是**时序收敛**：组合逻辑里那段「读旧值 → 乘 → 加 → 产生新值」是一条很长的组合路径，把它和「寄存器更新」拆开后，综合工具只需要让这条组合路径在一个时钟周期内跑完，结构清晰、便于优化。

#### 4.1.2 核心流程

时序块只做一件事——复位清零，否则把 `_nx` 原样搬进寄存器：

```text
每个 posedge clk：
  if (复位无效)
      对所有 (i,j): matrix_mul_2D[i][j] <= matrix_mul_2D_nx[i][j]
  else
      对所有 (i,j): matrix_mul_2D[i][j] <= 0
```

注意：这个时序块**不看 `alu_start`**。它永远在搬运 `_nx`。真正决定「累加器要不要变」的是组合块（4.2 节）：当 `alu_start=0` 时，组合块会让 `_nx = matrix_mul_2D`（原地保持），于是寄存器虽然每拍都在「搬」，但搬的是同一个值，效果就是**冻结**。

#### 4.1.3 源码精读

时序寄存块（注意它不依赖 `alu_start`，只做 `_nx → _2D` 的提交）：

[rtl/systolic.v:79-90](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L79-L90) —— 复位时把整个 8×8 累加矩阵清零；否则每个时钟沿把组合算出的 `matrix_mul_2D_nx` 提交进寄存器 `matrix_mul_2D`。

两个阵列与乘积寄存器的声明：

[rtl/systolic.v:30-35](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L30-L35) —— `matrix_mul_2D`/`matrix_mul_2D_nx` 都是 `signed [20:0]`（21bit）的 8×8 阵列；`mul_result` 是 `signed [15:0]`（16bit）的单个乘积暂存器（被所有 cell 复用）。

#### 4.1.4 代码实践

**目标：** 亲手验证「`alu_start=0` 时累加器被冻结」这一论断。

**步骤：**

1. 打开 [rtl/systolic.v:92-118](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L92-L118)，找到组合块末尾的 `else` 分支（`alu_start` 为假时）。
2. 阅读该分支：它对每个 (i,j) 执行 `matrix_mul_2D_nx[i][j] = matrix_mul_2D[i][j]`。
3. 结合 4.1.3 的时序块推出：`_nx` 等于当前值 → 下一拍 `_2D` 仍等于当前值。

**观察/预期：** 当控制器把 `alu_start` 拉低（例如 `LOAD_DATA`、`WAIT1` 状态，见 u3-l1）时，即便 `weight_queue`/`data_queue` 因移位而变化，`matrix_mul_2D` 也**一个 bit 都不会变**——累加结果被安全冻结，直到下一次 `alu_start=1`。

> 是否真的不变属于运行期行为，**待本地验证**（可在 testbench 里于 `alu_start` 拉低前后各打印一次 `matrix_mul_2D` 比对）。

#### 4.1.5 小练习与答案

**练习 1：** 如果把组合块的逻辑直接合并进一个 `always@(posedge clk)` 块（即在同一块里既算乘加又写寄存器），会有什么缺点？

**参考答案：** 会让「乘法 + 加法 + 寄存器写入」挤在同一条时序路径里，组合深度变大，关键路径变长，最高时钟频率下降；而且难以在别处（如结果收集块）复用「当前值」。拆成 `_nx`（组合）+ `_2D`（时序）后，关键路径只剩一次乘加，时序更干净。

**练习 2：** 时序块为什么不需要判断 `alu_start`？

**参考答案：** 因为「要不要变」的决策已经由组合块完成并编码进 `_nx`。`alu_start=0` 时组合块令 `_nx = _2D`，时序块照搬即可，自然实现冻结。把使能判断集中在组合块一处，避免逻辑散落。

---

### 4.2 乘加 always@(*) 块：cycle_num 门控的三条分支

#### 4.2.1 概念说明

这是本讲的心脏。组合块在每个 cell、每个周期都要做一个三选一决策：

| 分支 | 口语名称 | 动作 | 何时触发（由 `cycle_num` 与 \(s=i+j\) 决定） |
| --- | --- | --- | --- |
| 分支 1 | **首入**（fresh start） | 用本次乘积**覆盖**累加器 | 标志着一个新输出元素的第一个乘积 |
| 分支 2 | **累加**（accumulate） | 把本次乘积**加到**累加器上 | 一个输出元素的第 2~8 个乘积 |
| 分支 3 | **保持**（hold） | 累加器原样不动 | 该 cell 本拍还不该参与运算 |

为什么必须有「首入」分支？因为同一个 cell 在时间上会**连续生产多个**输出元素（第一批算完 C 的一个元素，紧接着算下一个）。如果不「覆盖」、只会「累加」，那么第二批的第一个乘积就会被错误地加到第一批的最终结果上。分支 1 的职责就是：**在每个新输出元素的起点，把累加器清零并写入第一个乘积**（用覆盖代替「先清零再累加」两步）。

#### 4.2.2 核心流程

组合块用 `if / else if / else` 实现优先级固定的三选一（**分支 1 优先级最高**）：

```text
always @(*)
  if (alu_start)
    对每个 cell(i,j)，s = i+j：
      if (命中「首入」时机)              // 分支 1：覆盖
          mul_result = weight_queue[i][j] * data_queue[i][j]
          matrix_mul_2D_nx[i][j] = 符号扩展(mul_result)
      else if (该 cell 已进入累加窗口)   // 分支 2：累加
          mul_result = weight_queue[i][j] * data_queue[i][j]
          matrix_mul_2D_nx[i][j] = matrix_mul_2D[i][j] + 符号扩展(mul_result)
      else                              // 分支 3：保持
          mul_result = 0
          matrix_mul_2D_nx[i][j] = matrix_mul_2D[i][j]
  else  // alu_start=0
      全部保持
```

两条乘法指令 `mul_result = weight_queue[i][j] * data_queue[i][j]` 完全相同——都是「取本 cell 此刻队列里的 weight 和 data 做有符号乘法」。差别只在结果如何并入累加器：**覆盖** 还是 **相加**。

#### 4.2.3 源码精读

整块组合乘加逻辑：

[rtl/systolic.v:92-118](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L92-L118) —— 在 `alu_start` 有效时，对 8×8 每个 cell 按优先级判断走「首入 / 累加 / 保持」之一；`alu_start` 无效时全部保持。注释 `based on the mul_row_num, decode how many row operations need to do` 点明了这是按周期数解码每个 cell 该做什么。

三条分支本体：

[rtl/systolic.v:97-108](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L97-L108) —— 第 97–100 行是分支 1（首入：乘积符号扩展后**直接赋值**），第 101–104 行是分支 2（累加：乘积符号扩展后**与旧值相加**），第 105–108 行是分支 3（保持：`mul_result=0`，`_nx` 等于旧值）。

> 小提示：`mul_result` 是一个被所有 (i,j) 复用的共享 `reg`。在组合 `for` 循环里它每次被赋值后立即被使用，循环结束后它只保留最后一个 cell 的值——这是合法的，因为它的用途仅限「当拍就地消费」。

#### 4.2.4 代码实践

**目标：** 确认三条分支的优先级，并预测一个最简单 cell 的行为。

**步骤：**

1. 在 [rtl/systolic.v:97-108](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L97-L108) 中确认 `if`（分支 1）→ `else if`（分支 2）→ `else`（分支 3）的顺序。
2. 取 cell(0,0)，\(s=0\)。假设 `alu_start=1`，逐周期判断：`cycle_num=0` 时三条分支都不满足（分支 1 要 `cycle_num>=9`，分支 2 要 `cycle_num>=1`），所以走分支 3（保持）。

**预期结果：** `cycle_num=0` 这一拍 cell(0,0) 保持不动；从 `cycle_num=1` 起进入分支 2（累加），直到 `cycle_num=9` 改走分支 1（首入）。完整的逐拍表见 4.3.4。

#### 4.2.5 小练习与答案

**练习 1：** 如果把分支 1 和分支 2 的判断顺序对调（先判累加、再判首入），会发生什么错误？

**参考答案：** 在本该「首入」的节拍（如 cell(0,0) 的 `cycle_num=9`），分支 2 的条件 `i+j <= cycle_num-1`（`0 <= 8`）也成立，于是会先走累加分支，把新一批的第一个乘积**加到**上一批的最终结果上，导致每个输出元素都多了一份上一批的残留，结果全错。这正是分支 1 必须优先的原因。

**练习 2：** 分支 3 里为什么仍要把 `mul_result` 赋为 0？

**参考答案：** `mul_result` 是组合 `reg`，综合工具通常要求它在所有路径都有确定值以避免生成锁存器（latch）。在「保持」分支里给它一个明确的 0，是为了满足组合逻辑「always 有赋值」的良好编码风格，并非参与运算（该分支的 `_nx` 只依赖旧值 `matrix_mul_2D`）。

---

### 4.3 FIRST_OUT / PARALLEL_START 与 (i+j) 启动时机

#### 4.3.1 概念说明

三个 `localparam` 是本讲的「魔法数字」，但它们都有清晰来历：

```verilog
localparam FIRST_OUT      = ARRAY_SIZE + 1;            // = 9
localparam PARALLEL_START = ARRAY_SIZE + ARRAY_SIZE + 1; // = 17
localparam OUTCOME_WIDTH  = DATA_WIDTH + DATA_WIDTH + 5; // = 21  （本节用不到，4.4 详讲）
```

- **`FIRST_OUT = 9`**：第一批**首个输出元素完成**的节拍。一个 cell 从开始累加到攒满 8 个乘积需要 8 拍；最早开工的 cell(0,0) 在第 1 拍开始累加，所以它在第 8 拍攒满，**第 9 拍**就可以输出并开始下一项。`FIRST_OUT` 就是这个「9」。
- **`PARALLEL_START = 17`**：稳态下，每 `ARRAY_SIZE=8` 拍就有一个新输出元素在某 cell 上起步。`17 = 9 + 8`，它是 `FIRST_OUT` 之后「隔了一个完整点积长度」的第二个首入节拍。之所以需要第二个 `localparam`，是因为分支 1 的判断用了 `%16`（周期为 \(2\times\)`ARRAY_SIZE`=16），单个取模只能命中一个余数类，**两个相隔 8 的首入节拍必须用两条余数条件分别表达**——这就是「双份」`FIRST_OUT`/`PARALLEL_START` 的全部意义。

#### 4.3.2 核心流程：从 (i+j) 推出每个 cell 的时间表

设 cell 的反对角线编号 \(s=i+j\)（\(0\le s\le 14\)）。把分支 1、2 的条件翻译成「中文时间表」：

- **分支 2（累加）条件**：`cycle_num >= 1` 且 \(s \le \text{cycle\_num}-1\)，即 `cycle_num >= s+1`。
  → cell(i,j) 从第 \(s+1\) 拍起具备累加资格。
- **分支 1（首入）条件**：`cycle_num >= FIRST_OUT` 且 \(s == (\text{cycle\_num}-\text{FIRST\_OUT})\%16\)，即 `cycle_num == s + 9`（首批，未取模前）。
  → cell(i,j) 在第 \(s+9\) 拍首次「首入」。

由于 \(s+1 \le s+8 < s+9\)，把首批时间表连起来就是一条干净的金线：

\[
\boxed{\text{cell}(i,j)\text{ 在第 }s+1\sim s+8\text{ 拍累加 8 个乘积，第 }s+9\text{ 拍首入下一项}}
\]

用代数验证 `FIRST_OUT`：首个首入节拍 \(= s+9 = s + 1 + \text{ARRAY\_SIZE} = s + (\text{ARRAY\_SIZE}+1) = s + \text{FIRST\_OUT}\)。这就解释了 `FIRST_OUT = ARRAY_SIZE + 1` 的来源——它是「起始偏移 1」加上「点积长度 `ARRAY_SIZE`」。

为什么控制器也在 `cycle_num >= ARRAY_SIZE+1`（即 `>=9`）时才拉高 `sram_write_enable`？因为那正是最早一批 cell 攒满 8 个乘积、结果可写的时刻——控制器与阵列共用同一个 `FIRST_OUT` 语义：

[rtl/systolic_controll.v:157-170](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v#L157-L170) —— `ROLLING` 状态里 `cycle_num_nx = cycle_num + 1`（自由递增），且仅当 `cycle_num >= ARRAY_SIZE+1`（= `FIRST_OUT`）才开始递增 `matrix_index` 并置 `sram_write_enable=1`，与阵列侧「第 9 拍首个结果就绪」完全对齐。

#### 4.3.3 源码精读

`localparam` 定义：

[rtl/systolic.v:26-28](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L26-L28) —— `FIRST_OUT=9`、`PARALLEL_START=17`、`OUTCOME_WIDTH=21`。

分支 1 的两条首入条件（用 `||` 并联两条余数类）：

[rtl/systolic.v:97-100](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L97-L100) —— 左半句对应 `FIRST_OUT` 这条余数类，右半句对应 `PARALLEL_START` 这条余数类；两者合起来表达「每 8 拍一次首入」。

#### 4.3.4 代码实践（本讲主实践）

**目标：** 选定一个具体 cell，列出 `cycle_num` 递增时它依次命中哪条分支，亲手验证 4.3.2 的时间公式。

**示例 cell：** 取 cell(2,1)，\(s=i+j=3\)。

**操作步骤：** 对每个 `cycle_num`，按「分支 1 → 分支 2 → 分支 3」顺序判断（注意分支 1 用 `FIRST_OUT=9`、`PARALLEL_START=17`、`%16`）。得到下表（只列首批与第二批衔接处）：

| cycle_num | 分支 1 条件（首入） | 分支 2 条件（累加） | 命中分支 | cell(2,1) 动作 |
| --- | --- | --- | --- | --- |
| 0 | 否（<9） | 否（<1） | 分支 3 保持 | 不动 |
| 1,2,3 | 否 | 否（\(3 \le 0/1/2\) 假） | 分支 3 保持 | 不动 |
| 4 | 否 | 是（\(3\le 3\)） | **分支 2 累加** | 第 1 个乘积（加到 0 上） |
| 5,6,7,8 | 否 | 是 | **分支 2 累加** | 第 2~5 个乘积 |
| 9,10,11 | 否（\((c-9)\%16=0,1,2 \ne 3\)） | 是 | **分支 2 累加** | 第 6~8 个乘积 |
| 12 | **是**（\((12-9)\%16=3=s\)） | — | **分支 1 首入** | 覆盖：下一元素的第 1 个乘积 |
| 13~19 | 否 | 是 | 分支 2 累加 | 下一元素的第 2~8 个乘积 |
| 20 | **是**（\((20-17)\%16=3=s\)，第二余数类） | — | 分支 1 首入 | 再下一元素的第一个乘积 |

**需要观察的现象 / 预期结果：**

- 累加窗口恰好是 `cycle_num = s+1 ~ s+8`，即 4~11，共 **8 个乘积**（对应点积长度 `ARRAY_SIZE=8`）。✅
- 首入发生在 `cycle_num = s+9 = 12`，恰好接在累加窗口末尾的下一拍。✅
- 之后每 **8 拍**一次首入：12（走 `FIRST_OUT` 余数类）→ 20（走 `PARALLEL_START` 余数类）→ 28 → 36 …，两条余数类交替，正好对应「每 8 拍产出 1 个新输出元素」的稳态流水。✅

**解释 (i+j) 与 cycle_num 的关系：** \(s=i+j\) 决定了 cell 在「波前」里的位置。\(s\) 越小，cell 越靠近左上角，越早开工（累加起点 \(s+1\) 越早、首入点 \(s+9\) 越早）；\(s\) 越大越靠右下角，越晚。同一反对角线上的 cell 同步推进，整张阵列像一道从左上扫向右下的波。

> 上表是依据源码条件手算的**逻辑推导**；若要在仿真里逐拍观察 `matrix_mul_2D_nx[2][1]`，需在 testbench 中接入该内部信号（**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1：** 阵列里哪个 cell 最先开始累加？哪个最后？分别在第几拍？

**参考答案：** cell(0,0)（\(s=0\)）最先，第 1 拍开始累加、第 9 拍首入。cell(7,7)（\(s=14\)）最后，第 15 拍才开始累加、第 23 拍（\(14+9\)）首入。

**练习 2：** 为什么 `FIRST_OUT` 恰好是 `ARRAY_SIZE + 1` 而不是 `ARRAY_SIZE`？

**参考答案：** 最早的 cell(0,0) 在第 1 拍（不是第 0 拍）开始累加，攒满 8 个乘积要算到第 8 拍，于是「下一个元素的首入」落在第 \(1 + 8 = 9\) 拍，即 `ARRAY_SIZE + 1`。这里的 `+1` 来自「累加从第 1 拍起」的那一格偏移。

**练习 3：** 如果把 `ARRAY_SIZE` 改成 16，`FIRST_OUT`、`PARALLEL_START` 与 `%16` 分别要改成什么才匹配？

**参考答案：** `FIRST_OUT` 会自动变成 `16+1=17`，`PARALLEL_START` 变成 `16+16+1=33`（二者都由 `localparam` 表达式自动跟随）。但 `%16` 这个**写死的**取模周期不会自动跟随——它应改成 `%32`（\(2\times\)`ARRAY_SIZE`）。这印证了 u1-l4 的结论：本项目是「半参数化」，直接改 `ARRAY_SIZE` 并不能即插即用。

---

### 4.4 符号扩展与定点累加位宽

#### 4.4.1 概念说明

现在回答最后一个细节：16bit 的乘积是怎么安全进入 21bit 累加器的？

- `mul_result` 是 `signed [15:0]`（16bit），来自两个 `signed [7:0]` 相乘。有符号乘法的位宽 = 操作数位宽之和 = 8+8 = 16，刚好。
- 累加器 `matrix_mul_2D` 是 `signed [20:0]`（21bit），要装下「最多 8 个乘积之和」。

要把 16bit 有符号数塞进 21bit 而不改变数值，必须做**符号扩展**：把最高位（符号位，bit 15）复制 5 份补到高位。代码里的 `{ {5{mul_result[15]}} , mul_result }` 干的就是这件事——`{5{mul_result[15]}}` 是把 bit 15 复制 5 次，再拼上原来的 16 位，得到 21 位。

定点语义上（沿用 u1-l4 的 Qm.n 记法）：乘积是 Q8.8（16bit：8 整 8 小），累加器是 Q13.8（21bit：13 整 8 小）。符号扩展不改变小数点位置，只是把整数部分的符号位拉宽，使加法不会因为「负数高位被当成 0」而出错。

#### 4.4.2 核心流程

累加的数值旅程：

\[
\text{weight}(Q4.4)\times\text{data}(Q4.4) \;\Rightarrow\; \text{mul\_result}(Q8.8,\,16\text{bit})
\;\xrightarrow{\text{符号扩展}}\; \text{Q13.8},\,21\text{bit}
\;\Rightarrow\; \text{累加进 } \text{matrix\_mul\_2D}
\]

- 正数 `mul_result`（bit15=0）：扩展等价于高位补 0，数值不变。
- 负数 `mul_result`（bit15=1）：扩展等价于高位补 1，在 21bit 补码下仍表示同一个负数。

8 个最大幅度乘积之和最多需要 \(\lceil\log_2 8\rceil = 3\) 位额外进位，理论上 16+3=19bit 即够；本项目用 21bit（多 2 位冗余）留出余量，这就是 `OUTCOME_WIDTH = DATA_WIDTH+DATA_WIDTH+5` 里那个 `+5`（3 位刚需 + 2 位冗余）。

#### 4.4.3 源码精读

乘积寄存器与符号扩展：

[rtl/systolic.v:35](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L35) —— `mul_result` 声明为 `reg signed [DATA_WIDTH+DATA_WIDTH-1:0]`，即 16bit 有符号。

[rtl/systolic.v:99](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L99) —— 分支 1（首入）：`matrix_mul_2D_nx[i][j] = { {5{mul_result[15]}} , mul_result };` 把 16bit 乘积符号扩展成 21bit 后**覆盖**累加器。

[rtl/systolic.v:103](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L103) —— 分支 2（累加）：`matrix_mul_2D_nx[i][j] = matrix_mul_2D[i][j] + { {5{mul_result[15]}} , mul_result };` 符号扩展后**加到**旧值上。两处扩展写法完全一致，保证无论首入还是累加，乘积都以正确的 21bit 补码参与运算。

#### 4.4.4 代码实践

**目标：** 手算符号扩展，体会「漏掉它会出错」。

**步骤：** 取一个负乘积做纸面推演。

1. 设 `mul_result = 16'b1111_1111_0000_0000`（= −256 的 16bit 补码，bit15=1）。
2. 符号扩展：`{ {5{1}} , 16'b1111_1111_0000_0000 }` = `21'b1_1111_1111_1111_0000_0000`（= −256 的 21bit 补码）。
3. 若**不做**符号扩展、直接高位补 0：得到 `21'b0_0000_1111_1111_0000_0000` = +4096，符号和数值全错。

**预期结果：** 一个本应是 −256 的数，漏掉符号扩展会变成 +4096，正负颠倒。可见 `{5{mul_result[15]}}` 不可省。

> 上述数值是按补码定义手算的确定结果；若要在仿真里观察 `mul_result` 为负时的 `matrix_mul_2D_nx`，需接入内部信号（**待本地验证**）。

#### 4.4.5 小练习与答案

**练习 1：** 为什么扩展位数恰好是 5，而不是 3？

**参考答案：** 累加器 21bit − 乘积 16bit = 5，所以扩展 5 位。理论上 8 个乘积之和只需 3 位额外进位（19bit 足够），但设计者把累加器定成 21bit、多留 2 位冗余（见 u1-l4），于是扩展位数相应是 5。

**练习 2：** 如果把 `{5{mul_result[15]}}` 误写成 `{5{1'b0}}`（即恒补 0），哪种输入会算错？

**参考答案：** 所有**负的**乘积都会算错：负数的最高位本应扩展为 1，恒补 0 会把它变成一个很大的正数，进而使累加结果偏大、甚至溢出符号位。在 Q4.4 输入下，只要 weight 与 data 异号（乘积为负）就会触发。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「从队列到累加器」的完整手算追踪。

**任务：** 选 cell(0,0)（\(s=0\)），用一组示例数据手算它**首批 8 个乘积**的累加过程，并解释结果对应矩阵乘的哪一个输出元素。

**示例数据（非项目真实测试数据，仅为手算演示，已标注为「示例代码/数据」）：**

- 设连续 8 拍到达 cell(0,0) 的 `weight_queue[0][0]` 依次为（8 位有符号 Q4.4）：\(w_0\ldots w_7 = 1, 2, 3, 4, 5, 6, 7, 8\)。
- 对应 8 拍的 `data_queue[0][0]` 依次为：\(d_0\ldots d_7 = 8, 7, 6, 5, 4, 3, 2, 1\)。

**操作步骤：**

1. 查表确定累加窗口：\(s=0\) ⇒ `cycle_num = 1~8` 为分支 2（累加），`cycle_num=9` 为分支 1（首入）。
2. 逐拍计算（注意首批累加器初值为 0，分支 2 等价于「加到 0」再逐拍累加）：

   | cycle_num | \(w\times d\) | 累加器 `matrix_mul_2D[0][0]` |
   | --- | --- | --- |
   | 1 | \(1\times8=8\) | 8 |
   | 2 | \(2\times7=14\) | 22 |
   | 3 | \(3\times6=18\) | 40 |
   | 4 | \(4\times5=20\) | 60 |
   | 5 | \(5\times4=20\) | 80 |
   | 6 | \(6\times3=18\) | 98 |
   | 7 | \(7\times2=14\) | 112 |
   | 8 | \(8\times1=8\) | **120** |

3. 第 9 拍分支 1 首入：累加器被**覆盖**为下一元素的第 1 个乘积（本例不再展开）。

**预期结果与解读：**

- cell(0,0) 首批累加结果为 **120**，它就是某一个输出矩阵元素 \(C[\,?\,][\,?\,]\) 的定点值（具体行列由 u2-l3 的反对角线收集与 u3-l2 的地址歪斜共同决定）。
- 这 8 个乘积之所以能「配成对」相加，是因为 u2-l1 的移位 + u3-l2 的地址歪斜保证了第 \(k\) 拍到达的 weight 与 data 恰好是点积的第 \(k\) 项。
- 整个过程只用到本讲的三个机制：**双份寄存器**提交（4.1）、**cycle_num 三分支**调度（4.2）、**符号扩展**保证负数不翻转（4.4）；而 (i+j) 公式（4.3）告诉我们这一切发生在第 1~8 拍。

**进阶（可选，待本地验证）：** 在 `Pre-Synthesis_Simulation/test_tpu.v` 里跑一次仿真（参见 u1-l5 / u4-l2），把真实 `mat1/mat2` 数据下 cell(0,0) 的累加过程打印出来，与你的手算对照；最终结果应与 `golden` 参考一致。

---

## 6. 本讲小结

- `systolic.v` 用**两个** 8×8 阵列把累加拆成「组合下一拍 `matrix_mul_2D_nx`」+「时序当前值 `matrix_mul_2D`」，时序块只做 `_nx → _2D` 的提交，关键路径只剩一次乘加，利于时序收敛。
- 组合乘加块按 **`cycle_num` 门控**做三选一：**分支 1 首入**（覆盖，标志新输出元素起点，优先级最高）> **分支 2 累加**（相加，元素的第 2~8 个乘积）> **分支 3 保持**（不动）。
- 三条分支里的乘法都是 `mul_result = weight_queue[i][j] * data_queue[i][j]`，差别只在结果**覆盖**还是**相加**。
- cell 的反对角线编号 \(s=i+j\) 决定时序：累加窗口为第 \(s+1\sim s+8\) 拍（共 `ARRAY_SIZE=8` 个乘积），第 \(s+9\) 拍首入下一项；同一反对角线的 cell 同步推进，整阵呈左上→右下的波前。
- `FIRST_OUT=ARRAY_SIZE+1=9` 是首个输出就绪节拍（控制器在 `cycle_num>=9` 才开始写回，两侧语义一致）；`PARALLEL_START=2×ARRAY_SIZE+1=17` 是相隔一个点积长度的第二个首入节拍，二者配合 `%16` 表达「每 8 拍一次首入」的稳态流水。
- 16bit 有符号乘积经 `{ {5{mul_result[15]}} , mul_result }` **符号扩展**成 21bit 后再入累加器，保证负乘积不会因高位补 0 而翻转。

---

## 7. 下一步学习建议

本讲搞清楚了「每个 cell 如何、何时累加」，但还有两个紧邻的问题悬而未决：

1. **结果怎么取走？** `matrix_mul_2D` 攒满 8 个乘积后，第 121–151 行的 `always@(*)` 块用 `matrix_index` 算出 `upper_bound`/`lower_bound`，按反对角线挑出 8 个有效结果拼成 `mul_outcome`。这正是**下一讲 u2-l3（结果收集与 mul_outcome 输出索引）**的主题，它承接本讲的 `matrix_mul_2D` 与 `matrix_index`。
2. **歪斜从何而来？** 本讲反复依赖「第 \(k\) 拍到达的 weight/data 恰好配成点积第 \(k\) 项」。这种时间对齐是 `addr_sel.v` 制造的地址歪斜提供的，建议随后阅读 **u3-l2（地址选择与输入歪斜 addr_sel）** 把这条因果链补全。

建议阅读顺序：**u2-l3 → u3-l1（控制器状态机）→ u3-l2（addr_sel 歪斜）**，便可完整理解「控制器发令 → 地址歪斜喂入 → 阵列乘加（本讲）→ 结果收集写出」的整条主链路。
