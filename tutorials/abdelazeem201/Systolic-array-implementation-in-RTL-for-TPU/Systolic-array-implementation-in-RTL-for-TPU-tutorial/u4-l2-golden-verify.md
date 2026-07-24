# golden 参考比对与结果验证流程

## 1. 本讲目标

在 u4-l1 里，我们把三批 8×8 矩阵「整形」后预填进了四块读 SRAM（a0/a1 装 weight、b0/b1 装 data）。但 TPU 真正算完之后，结果落在三块**输出 SRAM**（c0/c1/c2）里——我们怎么知道这些结果是对的？

本讲就回答这个问题。读完本讲，你应当能够：

1. 说清楚 `golden_transform` 任务做了什么：把**按行存放**的 golden 参考答案，重排成**按反对角线存放**的 `trans_golden`。
2. 解释**为什么**必须做这次重排：因为硬件（脉动阵列 + `write_out`）天然就是按反对角线把结果写进输出 SRAM 的，golden 与 SRAM 的「存储顺序」不同，不能直接逐地址比。
3. 看懂 testbench 里 `c0/c1/c2` 三段逐地址比对的流程，以及「相等就 PASS、不等就打印明细并 `$finish`」的判定逻辑。
4. 理解 `cycle_cnt` 如何统计一次完整三批矩阵乘所消耗的时钟周期数。

本讲是整个 u4（端到端仿真与验证闭环）的收口：它把 u4-l1 的数据加载、u2 的阵列计算、u3-l3 的写回，最终「钉死」在一个 PASS/FAIL 的结论上。

## 2. 前置知识

在进入源码前，先用通俗语言澄清三个概念。

- **golden（黄金参考）**：在写硬件之前，我们先用软件（比如 Python/C）把同样的矩阵乘算一遍，把「正确答案」存成文件。这份答案就叫 golden。硬件跑完之后，把硬件输出和 golden 对比，一致才算对。本项目里 golden 是三批矩阵乘的三个 8×8 结果，分别存于 `golden1.txt / golden2.txt / golden3.txt`。

- **行优先（row-major）**：golden 文件里，第 `i` 行的 128 位（8 个 16 位元素拼起来）就是结果矩阵的第 `i` 行。这是人最习惯的存放方式。

- **反对角线（anti-diagonal）**：在矩阵里，把「行号 + 列号相等」的那些格子归为一组，叫一条反对角线。例如所有满足 `r + c == 3` 的格子 `{(0,3),(1,2),(2,1),(3,0)}` 组成第 3 号反对角线。一个 8×8 矩阵共有 \(2N-1 = 15\) 条反对角线（编号 0..14）。脉动阵列里，**同一条反对角线上的 cell 会几乎同时算完**，于是硬件就按反对角线一条一条地把结果搬运出去——这就和 golden 的「行优先」顺序对不上了。本讲的核心，就是用 `golden_transform` 把行优先「翻译」成反对角线顺序。

> 一句话直觉：golden 是「横着读」的，SRAM 是「斜着写」的；要逐地址比对，就得先把 golden 也「斜着摆」。

承接 u2-l3 与 u3-l3 的结论：`systolic` 的结果收集（gather）按反对角线挑选 cell，`write_out` 再按 `matrix_index`（对应反对角线编号）把它们写进输出 SRAM，**每个 SRAM 地址存放一条反对角线的全部有效元素**。这是理解本讲「为什么」的物理基础。

## 3. 本讲源码地图

本讲几乎只涉及一个文件，但它和好几个上下游模块紧密相关。

| 文件 | 作用 | 本讲用到的部分 |
|------|------|----------------|
| `Pre-Synthesis_Simulation/test_tpu.v` | 仿真顶层 testbench | golden 加载、`golden_transform` 任务、`c0/c1/c2` 比对主流程、`cycle_cnt` |
| `Pre-Synthesis_Simulation/golden1.txt` 等 | golden 参考答案（二进制） | 8 行 × 128 位，行优先，由 `$readmemb` 载入 |
| `Pre-Synthesis_Simulation/sram_16x128b.v` | 输出 SRAM 行为模型 | 其内部数组 `mem[0:15]` 被 testbench 直接「偷看」用来比对 |
| `rtl/write_out.v`（u3-l3 已讲） | 写回模块 | 决定了 SRAM 「按反对角线存放」的布局 |
| `rtl/systolic.v`（u2-l3 已讲） | 阵列与结果收集 | gather 按 `matrix_index` 选反对角线 |

永久链接基准（当前 HEAD）：
`https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/`

## 4. 核心概念与源码讲解

### 4.1 golden 的加载与「行优先」布局

#### 4.1.1 概念说明

验证的第一步是把参考答案读进仿真内存。testbench 用 Verilog 的 `$readmemb`（read memory binary）把三个 golden 文件分别装进三个数组 `golden1/golden2/golden3`。每个数组有 8 个元素，每个元素 128 位（= `ARRAY_SIZE * OUT_DATA_WIDTH` = 8 × 16）。

关键是要记住 golden 的**存放顺序是行优先**：

- `golden1[i]` 这个 128 位的字，存放的是**结果矩阵第 `i` 行**的 8 个元素。
- 这 8 个元素从高位到低位依次是第 `i` 行的第 0 列、第 1 列 …… 第 7 列。
- 即：`golden1[i][ ((j+1)*16-1) -: 16 ]` = 结果矩阵第 `i` 行第 `j` 列的元素。（`-:` 是 Verilog 的「从某位开始往下取若干位」运算符。）

可以用数学式概括 golden 的二维索引：

\[
\text{golden}[i]\big[((j+1)\cdot W-1) : j\cdot W\big] = C_{i,j},\quad W=16
\]

其中 \(C_{i,j}\) 是结果矩阵的元素。

#### 4.1.2 核心流程

1. 仿真开始（`initial` 块），用 `$readmemb` 把三个 golden 文件分别载入 `golden1/golden2/golden3`。
2. 同期还载入两份输入矩阵 `mat1/mat2`（每份 24 行 = 三批 × 8 行）。
3. 随后调用两个预处理任务：`data2sram`（u4-l1 已讲，把输入填进读 SRAM）和 `golden_transform`（本讲重点，把 golden 重排）。
4. 之后再拉起 `tpu_start` 启动硬件。

注意 `$readmemb` 的路径：源码里写的是 `"golden/golden1.txt"`，即期望文件位于 `golden/` 子目录下。你实际运行仿真时，要保证仿真器的工作目录下能按此相对路径找到文件（待本地验证：不同仿真器的路径解析略有差异）。

#### 4.1.3 源码精读

三个 golden 数组的声明（注意位宽 `ARRAY_SIZE*OUT_DATA_WIDTH` = 128）：

[Pre-Synthesis_Simulation/test_tpu.v:L226-L232](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/Pre-Synthesis_Simulation/test_tpu.v#L226-L232) —— 声明 `golden1/2/3`（行优先参考答案，每个 128 位）以及稍后会填的 `trans_golden1/2/3`（反对角线顺序，各 15 个字）。

用 `$readmemb` 载入文件：

[Pre-Synthesis_Simulation/test_tpu.v:L245-L249](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/Pre-Synthesis_Simulation/test_tpu.v#L245-L249) —— 把 `mat1/mat2` 与三个 golden 文件载入对应数组；`golden/golden*.txt` 是相对路径。

载入后立即调用两个预处理任务（顺序很重要：先填输入 SRAM，再重排 golden）：

[Pre-Synthesis_Simulation/test_tpu.v:L253-L254](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/Pre-Synthesis_Simulation/test_tpu.v#L253-L254) —— `data2sram;` 与 `golden_transform;` 两个任务调用。

可以打开 `golden1.txt` 看一眼：它正好 8 行，每行 128 个 `0/1` 字符，对应 `golden1[0..7]` 这 8 个字——印证了「行优先、8 行」的布局。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：亲手确认 golden 的位宽与行数，建立「行优先」直觉。
2. **步骤**：
   - 打开 `Pre-Synthesis_Simulation/golden1.txt`，数一下行数（应为 8）和每行字符数（应为 128）。
   - 在 test_tpu.v 里找到 `golden1` 的声明，核对它的元素个数 `[0:ARRAY_SIZE-1]` 与位宽 `ARRAY_SIZE*OUT_DATA_WIDTH`。
3. **观察现象**：行数 = `ARRAY_SIZE` = 8；每行位数 = 8 × 16 = 128。
4. **预期结果**：文件物理布局与数组声明完全吻合。
5. 结论：`golden1[i]` 的 128 位 = 结果矩阵第 `i` 行的 8 个 16 位元素。

#### 4.1.5 小练习与答案

**练习 1**：`golden1[2]` 里，结果矩阵第 2 行第 5 列的元素，占据这 128 位中的哪一段？
**答**：按 `((j+1)*16-1) -: 16`，`j=5` → 第 `95 : 80` 位（从高位数第 5 个 16 位段）。

**练习 2**：如果把 `ARRAY_SIZE` 从 8 改成别的值，golden 文件需要怎么变？
**答**：行数变为 `ARRAY_SIZE`，每行位数变为 `ARRAY_SIZE*16`，同时 `golden_transform` 里写死的 `8*16`、`7*OUT_DATA_WIDTH` 等常量也要相应改（本项目是「半参数化」，见 u1-l4）。

---

### 4.2 golden_transform：把行优先重排成反对角线顺序

#### 4.2.1 概念说明

这是本讲最关键的一步。`golden_transform` 任务做的事，用一句话讲就是：

> 按「反对角线编号」重新打包 golden，使得 `trans_golden[k]`（`k = 0..14`）这个 128 位的字里，装的全是结果矩阵第 `k` 条反对角线上的元素，不足 8 个的用 0 补齐。

**为什么非做不可？** 因为硬件输出 SRAM 就是这么存的。回顾 u2-l3 与 u3-l3：

- `systolic` 的 gather 逻辑按 `matrix_index` 挑选 cell，而 `matrix_index` 与反对角线编号一一对应。
- `write_out` 把每一条反对角线的有效结果写进**同一个 SRAM 地址**：地址 `addr`（0..14）里装的就是第 `addr` 条反对角线的元素，128 位一个字，不足 8 个补 0。

也就是说，输出 SRAM 的 `mem[addr]` 与 golden 的「行」根本不是同一个东西——一个是「斜着的一组」，一个是「横着的一组」。要逐地址比，就得先把 golden 也摆成「斜着的一组」。`golden_transform` 就是这个翻译器：

\[
\text{trans\_golden}[k] = \text{pack}\{\, C_{r,c} \;\big|\; r+c = k \,\},\quad k=0,\dots,14
\]

8×8 结果矩阵共有 \(2\cdot 8 - 1 = 15\) 条反对角线，所以 `trans_golden` 每组有 15 个字（声明见 4.1.3 的 `[0:(ARRAY_SIZE*2-1)-1]` = `[0:14]`）。

> 顺带交代输出 SRAM 与三套 golden 的对应关系（来自 testbench 的例化，详见 4.3.3）：硬件的写回端口 `a/b/c` 分别接到 SRAM `c0/c1/c2`，对应 `golden1/golden2/golden3`。其中 `a`（→c0）在 `data_set==0` 时活跃，`c`（→c2）在 `data_set==1` 时活跃，`b`（→c1）横跨两个 `data_set` 承接交界结果（仲裁细节见 u3-l3）。所以三批矩阵乘的三个结果，分别落进 c0/c1/c2，再分别和 golden1/2/3 比。

#### 4.2.2 核心流程

`golden_transform` 用三重循环完成重排，伪代码如下：

```
先把 trans_golden1/2/3[0..14] 全部清零          # 防止上一轮残留，也顺便给「不足 8 个元素」的反对角线补 0
for k = 0 .. 14:                                 # 遍历每一条反对角线
    for i = 0 .. 7:                              # 行
        for j = 0 .. 7:                          # 列
            if (i + j == k):                     # 这个格子在反对角线 k 上
                # 把 golden[i] 第 j 列的 16 位，拼到 trans_golden[k] 的最高 16 位，原内容下移
                trans_golden[k] = { golden[i][列 j],  trans_golden[k] 的高 7 段 }
```

要点：

- **外层 `k`**：反对角线编号，0..14，正好对应输出 SRAM 的地址 0..14。
- **判定 `i+j == k`**：把落在第 `k` 条反对角线上的所有格子挑出来。
- **拼接 `{ 新元素, 旧值高 7 段 }`**：每次把新元素塞到 128 位字的**最高位端**，旧内容整体往低位让一格——所以这是一边挑、一边往高位「摞」的过程。
- **清零**：循环前先把 15 个字清零，既清除残留，也让那些不足 8 个元素的反对角线（例如 `k=0` 只有 1 个元素）自动在低位补 0。

#### 4.2.3 源码精读

先清零 15 个字（`ARRAY_SIZE*2-1 = 15`）：

[Pre-Synthesis_Simulation/test_tpu.v:L421-L425](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/Pre-Synthesis_Simulation/test_tpu.v#L421-L425) —— 把 `trans_golden1/2/3` 全部清零，等价于给短反对角线补 0。

三重循环 + 反对角线判定 + 高位拼接：

[Pre-Synthesis_Simulation/test_tpu.v:L426-L436](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/Pre-Synthesis_Simulation/test_tpu.v#L426-L436) —— 核心重排：`if((this_i+this_j)==this_k)` 挑出第 `this_k` 条反对角线上的格子，把 `goldenN[this_i]` 的第 `this_j` 列元素拼到 `trans_goldenN[this_k]` 最高 16 位。三个 golden 用同样代码并行处理。

末尾还有一段 `$write` 把 `trans_golden1` 打印出来供人工核对：

[Pre-Synthesis_Simulation/test_tpu.v:L437-L443](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/Pre-Synthesis_Simulation/test_tpu.v#L437-L443) —— 调试用打印：逐条反对角线、按段以有符号十进制输出 `trans_golden1`。

> 关于「字内元素顺序」：`trans_golden[k]` 内部 8 个 16 位段的具体排列，是由 `systolic` 的 gather（u2-l3）、`write_out` 的反向摆位（u3-l3）和这里的拼接顺序三方共同决定、并且被作者调成最终一致。本讲只强调最稳的可验证事实——**「地址 `k` ↔ 第 `k` 条反对角线」**这一对应关系；字内每一段的精确归属建议你跑一遍仿真用波形确认（待本地验证）。

#### 4.2.4 代码实践（纸笔跟踪型）

1. **目标**：亲手走一遍 `k=0` 与 `k=14` 两条反对角线的打包，体会「短反对角线补 0」。
2. **步骤**：
   - `k=0`：满足 `i+j==0` 的只有 `(0,0)`。所以 `trans_golden[0]` 只有最高 16 位是 `golden[0][列0]`，其余 112 位为 0。
   - `k=14`：满足 `i+j==14` 的只有 `(7,7)`。`trans_golden[14]` 最高 16 位是 `golden[7][列7]`，其余为 0。
   - `k=7`：满足 `i+j==7` 的有 8 个格子 `{(0,7),(1,6),...,(7,0)}`，正好填满一个 128 位字，没有补 0。
3. **观察现象**：反对角线越靠两端（0 或 14），有效元素越少、补的 0 越多；中间 `k=7` 恰好 8 个全满。
4. **预期结果**：与输出 SRAM 里 `mem[0]/mem[7]/mem[14]` 的「有效元素个数」完全一致——这正是硬件按反对角线写回的必然结果。
5. 结论：`trans_golden` 的形状（哪些地址满、哪些地址有空位）与 SRAM 的形状天然对齐，逐地址比对才成为可能。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `trans_golden` 每组是 15 个字，而不是 8 个或 16 个？
**答**：8×8 矩阵的反对角线条数 = \(2N-1 = 15\)，编号 0..14，所以是 15。SRAM 虽然深 16（`mem[0:15]`），但地址 15 是写空（`matrix_index==15` 的 mix 分支无可写元素），不参与比对。

**练习 2**：如果把重排前的清零步骤删掉，会发生什么？
**答**：上一轮残留会让短反对角线的低位出现「假数据」，导致与 SRAM 里那些补 0 的位段对不上，本该 PASS 的地址误报 FAIL。

---

### 4.3 c0/c1/c2 逐地址比对、PASS/FAIL 与 cycle_cnt

#### 4.3.1 概念说明

golden 重排好、硬件也跑完之后，就到了最终判定。testbench 用一个很直接的策略：**逐地址比 128 位整字**。对每一块输出 SRAM（c0/c1/c2），从地址 0 到 14，把 `trans_goldenN[i]` 和 SRAM 内部数组 `mem[i]` 做**严格相等**比较：

- 相等 → 打印 `PASS!!`，继续下一个地址；
- 不等 → 打印「实际值 vs golden 值」明细，然后 `$finish` 立刻结束仿真（**发现第一处不一致就停**）。

这里有两个细节值得注意：

1. **比对用的是 SRAM 的内部数组 `mem[i]`，而不是读端口 `rdata`**。这是仿真里的「后门」：testbench 直接越过读端口时序，去偷看 SRAM 模型里的存储数组（和 u4-l1 的 `char2sram` 是同一思路）。这样比对就纯粹是「数据是否一致」，不受读延迟干扰。
2. **`$finish` 在第一处错误就触发**。所以一次仿真你最多只能看到一个出错地址——这正是本讲实践任务要改进的地方。

另外，主流程里有一个 `cycle_cnt` 计数器，专门用来统计「硬件算完三批矩阵乘一共花了多少个时钟周期」，是衡量性能的关键指标。

#### 4.3.2 核心流程

完整的验证主流程（简化伪代码）：

```
# —— 启动阶段 ——
cycle_cnt = 0
拉低 srstn 复位 → 拉高 srstn
给 tpu_start 一个一周期脉冲            # 单脉冲启动

# —— 等待硬件跑完，顺便数周期 ——
while (~tpu_finish):
    @(negedge clk)
    cycle_cnt += 1                     # 数 TPU 跑了多少拍

# —— 逐地址比对三块输出 SRAM ——
for n in [c0↔golden1, c1↔golden2, c2↔golden3]:
    for i = 0 .. 14:
        if trans_goldenN[i] == sram_cN.mem[i]:
            打印 "address i PASS!!"
        else:
            打印 实际值 / golden 值 明细
            $finish                     # 第一处错就停

打印 "Total cycle count = cycle_cnt"
$finish
```

启动时序的几个关键点（来自 u3-l1）：`tpu_start` 是**单周期脉冲**；`tpu_finish`（即硬件的 `tpu_done`）也是控制器发出的单周期脉冲，它在 `matrix_index==15 且 data_set==1` 时拉高，标志三批结果全部写完。`while` 循环一检测到 `tpu_finish` 就退出，开始比对。

#### 4.3.3 源码精读

启动与等待（复位 → 单脉冲 → 计数循环）：

[Pre-Synthesis_Simulation/test_tpu.v:L269-L281](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/Pre-Synthesis_Simulation/test_tpu.v#L269-L281) —— `cycle_cnt=0` 后做复位脉冲与 `tpu_start` 单脉冲；`while(~tpu_finish)` 每个下降沿 `cycle_cnt+1`，直到 `tpu_finish` 拉高。注释 `//it's mean that your sram c0, c1, c2 can be tested` 说明此刻三块输出 SRAM 已就绪。

输出 SRAM 与 golden 的对应（来自顶层例化，决定了「c0 比 golden1」等关系）：

[Pre-Synthesis_Simulation/test_tpu.v:L106-L116](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/Pre-Synthesis_Simulation/test_tpu.v#L106-L116) —— 把 `tpu_top` 的写回端口 `a/b/c` 分别接到 `c0/c1/c2`：`a→c0`、`b→c1`、`c→c2`。所以后续 `c0↔golden1`、`c1↔golden2`、`c2↔golden3`。

c0（即 golden1）的逐地址比对，含 PASS 分支与失败明细：

[Pre-Synthesis_Simulation/test_tpu.v:L286-L295](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/Pre-Synthesis_Simulation/test_tpu.v#L286-L295) —— `for(i=0; i<15; i++)`：`trans_golden1[i]==sram_16x128b_c0.mem[i]` 则 PASS，否则按段打印「实际值」与「golden 值」（`$signed(...)` 以有符号十进制显示 8 个元素），并 `$finish`。

c1（golden2）与 c2（golden3）的比对结构完全相同：

[Pre-Synthesis_Simulation/test_tpu.v:L296-L305](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/Pre-Synthesis_Simulation/test_tpu.v#L296-L305) —— c1 比对（注意失败明细只打印 4 个元素，因为 c1/group b 横跨两个 data_set，每个地址的有效元素数与 a/c 不同，详见 u3-l3）。

[Pre-Synthesis_Simulation/test_tpu.v:L306-L315](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/Pre-Synthesis_Simulation/test_tpu.v#L306-L315) —— c2 比对（打印信息里写的是 `#c1`，是源码里的一处笔误，实际比的是 c2/golden3）。

最后打印总周期数并结束：

[Pre-Synthesis_Simulation/test_tpu.v:L317-L318](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/Pre-Synthesis_Simulation/test_tpu.v#L317-L318) —— `$display("Total cycle count C after three matrix evaluation = %d", cycle_cnt);`，然后 `$finish`。

> 提示：源码里还有一段被注释掉的 `End_CYCLE` 看门狗（约 L234-L243），原本用于「跑到上限周期还没结束就强制判 FAIL」。目前它被注释掉了，所以现在靠 `tpu_finish` 自然结束。你调试时如果担心硬件卡死，可以把它打开。

#### 4.3.4 代码实践（改 testbench：失败时输出十六进制）

这是本讲的主实践。任务有两部分。

**第一部分：用自己的话说明「为什么 golden 必须先经过 `trans_golden` 重排」。**

参考答案要点：硬件的输出 SRAM 按**反对角线**存放结果（一个地址 = 一条反对角线的全部有效元素，由 `systolic` 的 gather 与 `write_out` 的写回共同决定），而 golden 文件是**行优先**存放（一个字 = 结果矩阵的一行）。两者「存储顺序」不同，无法直接逐地址比。`golden_transform` 把行优先重排成「地址 `k` ↔ 第 `k` 条反对角线」的形式，使 `trans_golden[k]` 与 `mem[k]` 描述的是同一组数据，于是比对退化为简单的 128 位整字相等比较。

**第二部分：设计一个最小改动，让第一处不一致不仅打印明细，还输出该地址期望值与实际值的十六进制。**

当前失败分支只打印**有符号十进制**（`%d` 配 `$signed`），定位「到底是哪一位不一致」不够直观。补两行十六进制打印即可。下面是针对 c0（`golden1`）分支的示例改动（**示例代码，非项目原有代码**，请在你自己的本地副本上尝试，不要改动仓库源文件）：

```verilog
// 示例代码：在 test_tpu.v 第 288~293 行的 c0 失败分支里追加
else begin
    $write("You have wrong answer in the sram #c0 !!!\n\n");
    // —— 新增：整字十六进制，便于一眼定位不一致的位段 ——
    $write("addr %0d  expected(hex) = %h\n", i[5:0], trans_golden1[i]);
    $write("addr %0d  actual   (hex) = %h\n", i[5:0], sram_16x128b_c0.mem[i]);
    $write("Your answer at address %d is \n%d %d %d %d %d %d %d %d \n", i[5:0],
           $signed(sram_16x128b_c0.mem[i][(ARRAY_SIZE*16-1)-:OUT_DATA_WIDTH]),
           /* ...其余 7 段保持原样... */ );
    $write("But the golden answer is  \n%d %d %d %d %d %d %d %d \n", /* ...原样... */);
    $finish;
end
```

操作步骤：

1. **目标**：在失败时同时看到十六进制的期望值与实际值。
2. **步骤**：在你的本地 testbench 副本里，给 c0、c1、c2 三个失败分支各加两行 `%h` 打印（c1 把 `trans_golden1` 换成 `trans_golden2`、`sram_16x128b_c0` 换成 `_c1`；c2 同理换成 3）。
3. **观察现象**：若硬件有错，仿真日志里每个出错地址会多出两行 `expected(hex) / actual(hex)`，你可以直接拿这两个 128 位十六进制数做按位异或，找出最早出现差异的 16 位段。
4. **预期结果**：定位错误的速度比只看十进制明细更快；PASS 时这两行不触发，不影响正常流程。
5. **进阶思考（待本地验证）**：把 `$finish` 换成「记录错误数后继续」，可以看到全部出错地址而不仅是第一处——但要小心，继续比对前要确认后续地址的数据是否已被硬件真正写满。

#### 4.3.5 小练习与答案

**练习 1**：为什么比对用的是 `sram_16x128b_c0.mem[i]`，而不是读端口 `sram_rdata_c0`？
**答**：`mem[i]` 是 SRAM 模型的内部存储数组，越过读端口及其一拍延迟，直接反映「写进去的最终内容」；用它能做纯粹的数据一致性比对，不受读时序影响。这是仿真后门（与 u4-l1 的 `char2sram` 同源）。

**练习 2**：三段比对循环的 `i` 上界是 `ARRAY_SIZE*2-1 = 15`，即 `i = 0..14`，为什么不是 0..15？
**答**：8×8 结果矩阵只有 15 条反对角线（编号 0..14）。SRAM 地址 15 在 `matrix_index==15` 的 mix 分支下写进去的是全 0（无可写元素），且 `trans_golden` 数组本身就只有 15 项（`[0:14]`），所以地址 15 既无意义也不参与比对。

**练习 3**：`cycle_cnt` 到底数的是哪段时间？
**答**：从 `tpu_start` 单脉冲结束、进入 `while(~tpu_finish)` 循环开始，到 `tpu_finish` 拉高为止，每个下降沿 `+1`。它近似等于「TPU 跑完三批矩阵乘所用的时钟周期数」（不含前面预填 SRAM 的时间）。

---

### 4.4 cycle_cnt：用周期数衡量性能

#### 4.4.1 概念说明

功能正确（PASS）只是及格线，性能（多少周期算完）才是加速器的核心卖点。`cycle_cnt` 就是为衡量性能而设：它是一个普通 `integer`，在 `while(~tpu_finish)` 循环里每过一个下降沿就加 1，最终用 `$display` 打印出来。

#### 4.4.2 核心流程

1. 启动前 `cycle_cnt = 0`。
2. 进入 `while(~tpu_finish)`：每个 `@(negedge clk)` 加 1。
3. `tpu_finish` 拉高，退出循环。
4. 三块 SRAM 比对完毕后，打印 `Total cycle count C after three matrix evaluation = <cycle_cnt>`。

由 u3-l1 可知，硬件内部 ROLLING 阶段约 41 拍产出 256 个元素；这里 `cycle_cnt` 统计的是从外部看「一次三批矩阵乘」的端到端周期，涵盖 LOAD_DATA、WAIT1、ROLLING 等所有阶段。具体数值待本地验证（取决于仿真器与机器）。

#### 4.4.3 源码精读

`cycle_cnt` 的声明与清零：

[Pre-Synthesis_Simulation/test_tpu.v:L216-L216](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/Pre-Synthesis_Simulation/test_tpu.v#L216-L216) —— `integer cycle_cnt;` 声明。

计数与打印的代码已在 4.3.3 给出（L269-L281 的循环、L317-L318 的打印），此处不重复贴。

#### 4.4.4 代码实践（性能观察型）

1. **目标**：跑通仿真后读出 `cycle_cnt`，并尝试理解它的构成。
2. **步骤**：在你的本地环境用 Verilog 仿真器（如 VCS、Verilator、Icarus Verilog 等）编译运行 `test_tpu.v` 及其依赖；运行结束后在日志里找 `Total cycle count`。
3. **观察现象**：得到一个整数（待本地验证），它与 u3-l1 推算的「启动 + 灌满 9 拍 + 写回 32 拍 × 数据组数」量级应大致吻合。
4. **预期结果**：若你日后改动阵列或控制器，`cycle_cnt` 的变化能直观反映性能得失——这就是它作为性能探针的价值。
5. 注意：若仿真因某处 FAIL 而 `$finish`，`cycle_cnt` 不会被打印（它在比对之后）。所以想看周期数，先得让比对通过。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `tpu_start` 的单脉冲不小心写成了持续高电平，`cycle_cnt` 还准吗？
**答**：不一定。`tpu_start` 应为单周期脉冲（u3-l1 的状态机据此从 IDLE 跳到 LOAD_DATA）；若持续为高，可能让状态机行为异常，`cycle_cnt` 也就失去意义。

**练习 2**：为什么 `cycle_cnt` 在**下降沿**递增，而不是上升沿？
**答**：testbench 在下降沿更新控制信号（如 `srstn`、`tpu_start`），与 DUT（上升沿采样）错开半拍，避免竞争；在下降沿数周期是常见的 testbench 写法，保证读到的是稳定值。

---

## 5. 综合实践

把本讲知识串起来，完成下面这个端到端的小任务：

**任务：画出从「golden 文件」到「PASS/FAIL」的完整数据通路，并标注每一处的「存储顺序」。**

要求：

1. 画一条数据流：`golden1.txt` → `$readmemb` → `golden1[]`（行优先）→ `golden_transform` → `trans_golden1[]`（反对角线顺序，15 个字）。
2. 再画另一条：硬件 `systolic` gather（按 `matrix_index`/反对角线选 cell）→ `quantize` → `write_out`（按 `matrix_index` 写地址）→ `c0` SRAM 的 `mem[]`（反对角线顺序，地址 0..14）。
3. 在两条流的末端用「`trans_golden1[i] == mem[i]`？」连起来，标出 PASS / FAIL（含 `$finish`）的分支。
4. 在图上用不同颜色或标注区分两种「存储顺序」：行优先 vs 反对角线。指出**唯一需要翻译**的环节就是 `golden_transform`。
5. 进阶：在你的本地 testbench 副本上实现 4.3.4 的十六进制改动，然后故意改错一个 golden 文件的某一位，观察日志中 `expected(hex)` 与 `actual(hex)` 的差异，确认你能定位到出错的具体 16 位段。

完成后，你应当能脱稿讲清楚：**为什么这个 testbench 需要一个 `golden_transform`，它如何把「人写的行优先答案」翻译成「机器写的反对角线答案」，从而让验证变成一句简单的 128 位相等比较。**

## 6. 本讲小结

- golden 参考答案以**行优先**存于 `golden1/2/3`（每个 128 位 = 结果矩阵的一行），由 `$readmemb` 从文件载入。
- 硬件输出 SRAM 按**反对角线**存放结果：地址 `addr`（0..14）= 第 `addr` 条反对角线的全部有效元素，不足 8 个补 0；这是 `systolic` gather 与 `write_out` 共同决定的（u2-l3、u3-l3）。
- `golden_transform` 是「翻译器」：按 `i+j==k` 把行优先重排成反对角线顺序 `trans_golden[k]`（`k=0..14`），使两边的存储顺序对齐。
- 验证 = 逐地址比 128 位整字：`trans_goldenN[i] == sram_cN.mem[i]`，相等 PASS，不等打印明细并 `$finish`（第一处错即停）。比对走的是 SRAM 内部数组 `mem`，是仿真后门。
- 输出 SRAM 与 golden 的对应：`c0↔golden1`、`c1↔golden2`、`c2↔golden3`，分别对应写回端口 `a/b/c`（u3-l3）。
- `cycle_cnt` 在 `while(~tpu_finish)` 里数下降沿，统计一次三批矩阵乘的端到端周期数，是性能探针。

## 7. 下一步学习建议

到此，u4（端到端仿真与验证闭环）完成：你已经能把数据喂进去（u4-l1）、把结果验出来（本讲）。接下来的学习方向有两条：

- **向后端走（u5）**：RTL 仿真对了，只是「算得对」。下一单元进入 `syn/`（Design Compiler 综合）与 `pnr/`（ICC2 布局布线），看这份通过验证的 RTL 如何变成真正的门级网表乃至版图。建议从 u5-l1 的 `syn.tcl` 综合流程读起。
- **向扩展架构走（u6）**：`rtl/RTL_modified/` 是一条更完整的 TPU 支线（带 `master_control`、`accumTable`、`reluArr`、Avalon 总线、主机软件 API）。如果你对「TPU 如何接入 SoC、如何被软件驱动」更感兴趣，可以从 u6-l1 的 `top.v` 数据通路读起。

无论走哪条路，本讲建立的「按反对角线组织数据」的直觉都会反复出现——它是脉动阵列类设计的通用语言。
