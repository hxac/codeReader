# dit 主模块：输入输出与工作缓冲

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `dit` 模块里**五块缓冲**各自的名字、容量与职责。
- 解释 **A/B 翻转标志**（`_A` / `_B`）为什么用「两个寄存器」来表示一块缓冲的「满/空」，并能手算它们的翻转序列。
- 画出一次完整 FFT 中数据从 `bufferin` 流经 `bufferX/bufferY` 再到 `bufferout` 的通路。
- 说明输入双缓存如何让「接收下一帧」与「计算当前帧」**并行**进行，以及什么情况下会触发 `overflow`。
- 解释 `updatedX` / `updatedY` 这两张 N 位位图如何防止控制状态机「读到还没写好的位置」。

本讲只聚焦**缓冲与数据搬运**，刻意不展开控制状态机的四状态转移细节（那是 u3-l2）和蝶形地址的位运算推导（那是 u3-l3）。我们只会引用到状态机里「触发缓冲翻转」的那几行，点到为止。

## 2. 前置知识

本讲假设你已掌握前几讲的内容：

- **复数定点编码（高实低虚）**（u2-l1）：一个复数被打包成 `2*X_WDTH` 位整数，实部在高 `X_WDTH` 位、虚部在低 `X_WDTH` 位。本讲里每个缓冲单元 `bufferX[N-1:0]` 存的都是这种打包复数。
- **蝶形运算**（u2-l2）：`YA = XA + W·XB`、`YB = XA − W·XB`，是 FFT 的计算原子；它有若干拍流水延迟。
- **DIT 按级合并**（u1-l1）：N 点 FFT 分 `NLOG2` 级完成，每级要把 N 个数重新读一遍、写一遍。

此外需要三个 Verilog 小概念，先用一句话解释：

- **`reg` 的多驱动冲突**：一个 `reg` 只能由**一个 `always` 块**赋值。如果两个 `always` 块都对同一个 `reg` 赋值，综合会报错或行为未定义。本讲反复出现的「为什么拆成两个寄存器」正是为了绕开这条限制。
- **归约运算符 `&expr`**：把一个向量的所有位做按位与，结果是 1 位。例如 `&bufferin_addr` 为真，当且仅当 `bufferin_addr` 所有位都是 1，即地址等于 `N-1`（最后一个地址）。
- **乒乓（ping-pong）**：用两块存储区交替读写——读 A 写 B，下一轮读 B 写 A——让生产者和消费者永远不会争抢同一块内存。

## 3. 本讲源码地图

本讲的全部主角都在同一个文件里：

| 文件 | 作用 |
| --- | --- |
| [`dit.v`](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v) | 顶层主控模块；五块缓冲、输入写进程、输出读进程、蝶形输出接收进程都在这里。 |
| [`qa_dit.py`](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py) | MyHDL 测试台；其中的 `sendnth`（输入节流）与 `overflow` 检测直接对应输入双缓存的行为。 |

`dit` 内部的五块缓冲一览（声明集中在 [`dit.v:L50-L84`](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L50-L84)）：

| 缓冲 | 数量 | 单元位宽 | 深度 | 作用 |
| --- | --- | --- | --- | --- |
| `bufferin0` / `bufferin1` | 2 块 | `2*X_WDTH` | `N` | 输入双缓存，接收端口 `in_x` |
| `bufferX` / `bufferY` | 2 块 | `2*X_WDTH` | `N` | 工作缓存，级间乒乓 |
| `bufferout` | 1 块 | `2*X_WDTH` | `N` | 输出缓存，汇聚最后一级，排出 `out_x` |

外加两张 N 位位图 `updatedX` / `updatedY`，标记工作缓存每个槽位是否已写入新数据。

## 4. 核心概念与源码讲解

### 4.1 缓冲全景：数据如何在 dit 中流动

#### 4.1.1 概念说明

FFT 的计算被切成 `NLOG2` 级（例如 N=16 就是 4 级）。每一级都要**读完整 N 个数、再写回 N 个数**。如果只拿一块 RAM 同时承担读写，本级还没算完，下一级就要来抢数据；而且端口 `in_x` 还在不间断地送来**下一帧**输入。`dit` 用「分级缓冲 + 乒乓」来化解这些冲突：

- **输入侧**用**两块** `bufferin` 交替，让「接收新帧」和「第一级计算」互不干扰。
- **中间级**用**两块** `bufferX/bufferY` 乒乓，每级读一块、写另一块。
- **输出侧**用**一块** `bufferout` 汇聚最后一级结果，再由独立进程慢慢排出。

#### 4.1.2 核心流程

一次完整 FFT 的数据通路如下（箭头表示数据流向）：

```
          ┌──────────────────────────┐
 in_x ──► │  bufferin0 / bufferin1   │  输入双缓存（写进程 ping-pong）
          │  （第一级从这里读）        │
          └────────────┬─────────────┘
                       │ 第一级读
                       ▼
          ┌──────────────────────────┐
          │   bufferX  ⇄  bufferY     │  工作缓存（每级 readbuf_switch 翻转）
          │   （中间级读/写交替）       │
          └────────────┬─────────────┘
                       │ 最后一级写
                       ▼
          ┌──────────────────────────┐
 out_x ◄── │       bufferout          │  输出缓存（读进程排出）
          └──────────────────────────┘
```

要点：

1. 端口 `in_x` 由**一个独立的写进程**送入 `bufferin0` 或 `bufferin1`。
2. 控制状态机在**第一级**时从某一块 `bufferin` 读取，把蝶形结果写进 `bufferX`/`bufferY` 之一。
3. 之后的每一级在 `bufferX` 与 `bufferY` 之间乒乓：读一块、写另一块，级末翻转角色。
4. **最后一级**的蝶形结果改写到 `bufferout`。
5. `bufferout` 满后，由**另一个独立的读进程**逐拍把结果送上 `out_x`。

#### 4.1.3 源码精读

五块缓冲和位图的声明全部集中在一起，先建立整体印象（[dit.v:L50-L84](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L50-L84)）：

```verilog
// Input buffer.
reg [X_WDTH*2-1:0]           bufferin0[N-1:0];
reg                          bufferin_full0_A;
reg                          bufferin_full0_B;
...
reg [X_WDTH*2-1:0]           bufferin1[N-1:0];
...
// Working buffers.
reg [X_WDTH*2-1:0]           bufferX[N-1:0];
reg [X_WDTH*2-1:0]           bufferY[N-1:0];
// Output buffer.
reg [X_WDTH*2-1:0]           bufferout[N-1:0];
...
reg [N-1:0]                  updatedX;
reg [N-1:0]                  updatedY;
```

注意每块 `bufferin`/`bufferout` 都配了**两个**满标志 `_A` 和 `_B`——这正是本讲 4.2、4.5 要讲的核心机制。`bufferX`/`bufferY` 没有满标志，因为它们的「是否可读」由 `updatedX`/`updatedY` 位图逐槽位管理（4.4）。

#### 4.1.4 代码实践

**实践目标**：在 4.1.2 的方框图上标注每一级的「读哪块、写哪块」。

**操作步骤**：

1. 打开 [`dit.v`](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v)，找到第 281–282 行 `in0`/`in1` 的选择语句（第一级 vs 工作缓存的分流）。
2. 找到第 524–539 行的写入分发（最后一级 vs 工作缓存）。
3. 假设 `N=16`（`NLOG2=4`），在方框图的每条箭头旁标出该级 `S` 的取值与读/写目标。

**需要观察的现象**：你会看到第一级的输入来自 `bufferin`，而后面几级在 `bufferX`/`bufferY` 之间来回弹跳，最后一级汇入 `bufferout`。

**预期结果**：得到一张「级 → S → 读 → 写」的表格。参考答案见 4.3.4，你可以先自己填再对照。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `bufferX`/`bufferY` 不需要像 `bufferin` 那样配 `_A`/`_B` 满标志？

> **答**：因为工作缓存由**同一个**蝶形输出接收进程写入、由同一个状态机读取，且通过 `readbuf_switch` 严格交替读写不同块，不会出现「生产者消费者同时争一块」的局面；需要的只是「某个槽位是否已被本级写好」，这件事用逐槽位的 `updatedX`/`updatedY` 位图来表达更精确（见 4.4）。

**练习 2**：`&bufferin_addr` 为真代表地址是多少？为什么用它判断「一块缓存写满了」？

> **答**：`bufferin_addr` 是 `NLOG2` 位，归约与为真即所有位为 1，即地址 = `N-1`（最后一个槽）。再 `+1` 就回绕到 0，所以「写到第 N-1 个槽」正好意味着这一块被写满了 N 个样本。

---

### 4.2 输入双缓存 bufferin0/bufferin1 与 A/B 翻转机制

#### 4.2.1 概念说明

`bufferin` 的满/空检测用了一个非常巧妙的小技巧——**A/B 双标志**。源码里有一段直白的注释（[dit.v:L73-L75](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L73-L75)）：

> 我们用两个寄存器，因为它们由不同的进程驱动。`_A` 随缓冲「被填满」来回翻转；`_B` 随缓冲「被排空」来回翻转。

关键在于「不同的进程驱动」。`bufferin` 的**写入方**是输入写进程（一个 `always` 块），**读取方**是控制状态机（另一个 `always` 块）。如果它们要共同维护一个「满标志」，就必须有两个 `always` 块对同一个 `reg` 赋值——这在 Verilog 里是非法的多驱动冲突。解决办法是**给每方各发一个寄存器**：写方翻自己的 `_A`，读方翻自己的 `_B`，再用异或把它们合成最终的满标志：

```verilog
assign bufferin_full0 = bufferin_full0_A + bufferin_full0_B;  // 1 位相加 = 异或
```

#### 4.2.2 核心流程

A/B 标志本质是一个「单 bit 翻转握手」：生产者每填满一次就翻一下 `_A`，消费者每排空一次就翻一下 `_B`，**满 = 两标志相异**（异或为 1）。

| 事件 | `_A`（写方） | `_B`（读方） | `full = _A ⊕ _B` | 状态 |
| --- | --- | --- | --- | --- |
| 初始 | 0 | 0 | 0 | 空 |
| 写方填满一次 | 1 | 0 | 1 | 满 |
| 读方排空一次 | 1 | 1 | 0 | 空 |
| 写方填满二次 | 0 | 1 | 1 | 满 |
| 读方排空二次 | 0 | 0 | 0 | 空 |

> 数学上，`full` 就是「（填满次数 − 排空次数）的奇偶性」：奇 → 满，偶 → 空。

输入侧还在此基础上做了一层**双缓存乒乓**：有两块物理内存 `bufferin0` 和 `bufferin1`，各自带一套 `_A/_B`。写进程用 `bufferin_write_switch` 在两块之间切换，读进程用 `bufferin_read_switch` 切换。于是：

- 读进程正在排空 `bufferin0` 时，写进程可以同时去填 `bufferin1`，反之亦然；
- 两者永远瞄准**不同的物理块**，这就是「接收下一帧」与「计算当前帧」能并行的根因。

如果**写方比读方快**（输入来得太密、FFT 还没算完），写方瞄准的那块仍是满的，无处可写——此时置位 `overflow`。

#### 4.2.3 源码精读

**写进程**（[dit.v:L104-L136](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L104-L136)）负责把 `in_x` 写进当前选中的 `bufferin`：

```verilog
if (in_nd) begin
   if (bufferin_write_full)               // 要写的块还是满的 → 溢出
     overflow <= 1'b1;
   if (bufferin_write_switch)
     bufferin1[bufferin_addr] <= in_x;    // 写块 1
   else
     bufferin0[bufferin_addr] <= in_x;    // 写块 0
   bufferin_addr <= bufferin_addr + 1;
   if (&bufferin_addr) begin              // 写满 N 个样本
      bufferin_write_switch <= ~bufferin_write_switch;  // 切到另一块
      if (bufferin_write_switch)
        bufferin_full1_A <= ~bufferin_full1_A;          // 翻刚填满那块的 _A
      else
        bufferin_full0_A <= ~bufferin_full0_A;
   end
end
```

注意 `overflow` 是**粘住（sticky）**的：一旦在第 119–120 行被置 1，就再也不会被这个进程清零，只有复位（第 111 行）才能清——所以哪怕只溢出一次也会被测试台抓到。

**读方翻转 `_B`** 发生在状态机里（[dit.v:L376-L384](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L376-L384)），第一级读完一整块输入时：

```verilog
if (first_stage) begin
   if (bufferin_read_switch)
     bufferin_full1_B <= ~bufferin_full1_B;   // 翻刚排空那块的 _B
   else
     bufferin_full0_B <= ~bufferin_full0_B;
   bufferin_read_switch <= ~bufferin_read_switch;  // 读指针切到另一块
end
```

**哪块可读 / 哪块可写**由两个 `assign` 一句话给出（[dit.v:L65-L66](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L65-L66)）：

```verilog
assign bufferin_read_full  = bufferin_read_switch  ? bufferin_full1 : bufferin_full0;
assign bufferin_write_full = bufferin_write_switch ? bufferin_full1 : bufferin_full0;
```

状态机在 `FSM_ST_IDLE` 里检查 `bufferin_read_full`，为真才开始第一级（[dit.v:L355-L359](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L355-L359)）。

#### 4.2.4 代码实践

**实践目标**：亲手触发一次输入缓冲溢出，观察 `overflow` 如何报告「DUT 跟不上输入」。

**操作步骤**：

1. 打开 [`qa_dit.py`](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py)，定位 `test_basic` 中第 200 行 `sendnth = 2`（每 2 拍送一个输入样本）。
2. 把它改成 `sendnth = 1`（每拍都送），保存后运行 `python qa_dit.py`。
3. 观察测试台 `control()` 里的这段（[qa_dit.py:L171-L172](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L171-L172)）：
   ```python
   if self.overflow:
       raise StandardError("DIT couldn't keep up with input.")
   ```

**需要观察的现象**：仿真会抛出 `StandardError: DIT couldn't keep up with input.`。这是因为输入每拍到达，而 N=16 的 FFT 还没算完前一块，写方瞄准的 `bufferin` 仍是满的，第 119 行把 `overflow` 拉高，被测试台捕获。

**预期结果**：`sendnth = 2` 时测试通过，`sendnth = 1` 时抛出溢出错误。源码注释（[qa_dit.py:L198-L199](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L198-L199)）也提示「大 FFT 必须用更大的 `sendnth`，否则溢出」。

> 若本机没有 iverilog/MyHDL 环境，则把步骤 2 视为「待本地验证」，但你可以通过阅读第 119–120 行与第 171–172 行推断出上述结论。

#### 4.2.5 小练习与答案

**练习 1**：把 `bufferin_full0_A + bufferin_full0_B` 里的 `+` 换成 `&` 会怎样？

> **答**：`+` 在 1 位运算上等价于异或（结果取最低位）；换成 `&` 就变成「两标志都为 1 才满」，语义完全错误。初始 `0&0=0`（空，对），写满后 `1&0=0`（变成「空」，错）——读方会以为还没数据而永远等待。

**练习 2**：为什么 `overflow` 设计成「粘住」的，而不是每拍重新计算？

> **答**：溢出是一个「已发生就不可挽回」的事件——被覆盖的那帧数据已经丢失。粘住标志能确保即便只发生一瞬间，下游（这里是测试台）也一定能采样到，从而停机报错，而不是偶发漏检。

---

### 4.3 工作缓存 bufferX/bufferY 与 readbuf_switch 乒乓

#### 4.3.1 概念说明

`bufferX` 和 `bufferY` 是中间各级的「擂台」：某一级从其中一块读入蝶形的两个操作数，把结果写进另一块；级末翻转角色，下一级反向。哪块当读、哪块当写，由一个 1 位寄存器 `readbuf_switch` 决定：

- `readbuf_switch == 1` → 从 `bufferX` 读、向 `bufferY` 写；
- `readbuf_switch == 0` → 从 `bufferY` 读、向 `bufferX` 写。

每过一级，`readbuf_switch` 翻转一次，于是两块缓存交替扮演读/写角色。第一级是特例：它的**输入来自 `bufferin` 而不是工作缓存**，但它仍然要把结果写进 `bufferX`/`bufferY` 之一，为第二级准备好数据。

#### 4.3.2 核心流程

读侧选择（第一级读 `bufferin`，其余按 `readbuf_switch` 读工作缓存）：

```
first_stage?  读 bufferin0/bufferin1
否则:         readbuf_switch? 读 bufferX : 读 bufferY
```

写侧选择（最后一级写 `bufferout`，否则按 `readbuf_switch` 的「相反方向」写工作缓存，见 4.5）：

```
last_stage?   写 bufferout
否则:         readbuf_switch_z? 写 bufferY : 写 bufferX   （注意是反向）
```

级末（`FSM_ST_CALC` 检测到本级最后一个蝶形）做两件事：`readbuf_switch <= ~readbuf_switch`，并把刚被读空那块的 `updated` 位图清零（见 4.4）。

> 细节：写侧用的是 `readbuf_switch_z`（带 `_z` 后缀），因为蝶形模块有几拍流水延迟——当前输出的结果，对应的是**几拍之前**送进去的输入，那时 `readbuf_switch` 可能还没翻转。`_z` 是延迟后的副本，保证「写哪块」与「当初读的是哪块」一致。这部分时序细节属于 u2-l3。

#### 4.3.3 源码精读

**读侧分流**（[dit.v:L281-L282](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L281-L282)），用三元表达式区分第一级与其余级：

```verilog
assign in0 = first_stage ? (bufferin_read_switch ? bufferin1[in0_addr] : bufferin0[in0_addr])
                         : (readbuf_switch       ? bufferX[in0_addr]  : bufferY[in0_addr]);
assign in1 = first_stage ? (bufferin_read_switch ? bufferin1[in1_addr] : bufferin0[in1_addr])
                         : (readbuf_switch       ? bufferX[in1_addr]  : bufferY[in1_addr]);
```

`first_stage` / `last_stage` 的判定很简洁（[dit.v:L273-L277](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L273-L277)）：

```verilog
assign first_stage = (S == {1'b1,{NLOG2-1{1'b0}}});  // S == N/2
assign last_stage  = (S == 1);
```

**级末翻转**（[dit.v:L393](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L393)）就一行：

```verilog
readbuf_switch <= ~readbuf_switch;
```

**写侧分发**（[dit.v:L530-L539](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L530-L539)）：非最后一级时，按 `readbuf_switch_z` 写入对侧工作缓存，并置位对应槽位的 `updated` 位：

```verilog
if ((readbuf_switch_z & z_nd)|(readbuf_switch_z_old[0] & ~z_nd)) begin
   bufferY[out_addr_z] <= z;  updatedY[out_addr_z] <= 1'b1;
end else begin
   bufferX[out_addr_z] <= z;  updatedX[out_addr_z] <= 1'b1;
end
```

#### 4.3.4 代码实践

**实践目标**：列出 N=16（4 级）每一级读/写的工作缓存，验证乒乓规律。

**操作步骤**：

1. 假设复位后 `readbuf_switch = 0`（见 [dit.v:L301](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L301)）。
2. 第一级是 `first_stage`，输入来自 `bufferin`，结果按 `readbuf_switch_z=0` 写入 `bufferX`。
3. 级末 `readbuf_switch` 翻为 1，进入第二级……依次类推。

**预期结果（参考答案）**：

| 级 | S | first/last | 读 | 写 |
| --- | --- | --- | --- | --- |
| 1 | 8 | first | `bufferin0/1` | `bufferX` |
| 2 | 4 | — | `bufferX` | `bufferY` |
| 3 | 2 | — | `bufferY` | `bufferX` |
| 4 | 1 | last | `bufferX` | `bufferout` |

可以看到工作缓存在 X、Y 之间来回弹跳，最后一级跳出工作缓存、汇入 `bufferout`。

#### 4.3.5 小练习与答案

**练习 1**：如果 `N=8`（`NLOG2=3`），上表会变成几行？最后一级写到哪里？

> **答**：3 级，3 行（S=4, 2, 1）。第一级写 `bufferX`，第二级读 X 写 Y，第三级（last）读 Y 写 `bufferout`。规律不变：奇偶级交替，最后一级汇入 `bufferout`。

**练习 2**：为什么写侧用 `readbuf_switch_z` 而读侧用 `readbuf_switch`？

> **答**：读侧在「当前」拍取数，用当前值即可；写侧的输出是几拍前送入的蝶形算出来的，必须用与那次输入相对应的、延迟过的 `readbuf_switch_z`，才能把结果写回当初读取数据的「对侧」缓存。

---

### 4.4 updatedX/updatedY：防止读到还没写好的位置

#### 4.4.1 概念说明

`bufferX`/`bufferY` 每个槽位的写入顺序由蝶形地址（`out0_addr` 等）驱动，并不是简单的 0,1,2,…。这就带来一个风险：当前级想读的两个槽位，**上一级可能还没写到**。如果直接读，就会读到陈旧甚至未初始化的数据，FFT 结果全错。

`updatedX` 和 `updatedY` 就是两张 **N 位的「新鲜度」位图**，每个槽位对应 1 位：

- 某槽位被蝶形写入时，对应位置 1（4.3.3 里的 `updatedX[out_addr_z] <= 1'b1`）；
- 读取前先查这两张表，只有当两个输入槽都为 1 时才把数据送进蝶形；
- 级末把「即将成为写目标」的那块整块清零，等待新一轮写入。

#### 4.4.2 核心流程

```
每个时钟：
  if 蝶形输出有效:
      写入工作缓存某槽位 → 该槽 updated 位置 1
  if 本级结束（CALC 且最后一个蝶形）:
      把「刚被读空、即将被写入」那块的 updated 整块清 0

状态机发送侧（SEND）:
  updated0 = first_stage ? 1 : 当前读块在 in0_addr 处的 updated 位
  updated1 = first_stage ? 1 : 当前读块在 in1_addr 处的 updated 位
  只有 updated0 & updated1 都为 1，才拉高 x_nd 把数据送进蝶形
```

第一级固定为 1，因为输入数据在 `bufferin` 里早已就绪，不存在「等写」的问题。

#### 4.4.3 源码精读

**读侧查询**（[dit.v:L285-L288](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L285-L288)）：

```verilog
assign updated0 = first_stage ? 1 : (readbuf_switch ? updatedX[in0_addr] : updatedY[in0_addr]);
assign updated1 = first_stage ? 1 : (readbuf_switch ? updatedX[in1_addr] : updatedY[in1_addr]);
```

**SEND 状态的等待**（[dit.v:L418-L420](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L418-L420)）：两个输入都新鲜才发车。

```verilog
if (updated0 & updated1) begin
   x_nd <= 1'b1;
   ...
```

**级末清零**有一个值得注意的细节。它现在位于**蝶形输出接收进程**里（[dit.v:L502-L508](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L502-L508)）：

```verilog
if ((fsm_state == FSM_ST_CALC) & (&(out1_addr))) begin
   if (readbuf_switch)  updatedX <= {N{1'b0}};
   else                 updatedY <= {N{1'b0}};
end
```

为什么放在这里？因为 `updatedX`/`updatedY` 已经在同一个 `always` 块里被赋值（第 533、538 行的「置 1」）。Verilog 不允许两个 `always` 块驱动同一个 `reg`。源码在第 396–402 行留下了一段被注释掉的旧代码和一句说明「Moved later so we drive from same process as we set」——这段清零逻辑原来写在状态机 `always` 块里，后来为了**避免多驱动冲突**被搬到了接收进程里。这是一处典型的「把对同一寄存器的所有写操作集中到一个进程」的修法。

清零的目标是「`readbuf_switch` 指向的那块」——也就是**刚刚被读、即将被写**的那块。它上面的旧 `updated=1` 描述的是已被消费掉的数据，必须清掉，否则下一级会误以为这些槽位已经有了新数据而提前「发车」。

#### 4.4.4 代码实践

**实践目标**：通过阅读源码，理解「如果没有 `updated` 位图会出什么错」。

**操作步骤**：

1. 读 [dit.v:L418-L420](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L418-L420)，确认 `SEND` 状态在 `updated0 & updated1` 为假时会原地等待（落入第 432–435 行的 `else`，打印 `Waiting for data to be written.`）。
2. 假设把第 418 行改成 `if (1)`（即永远不等），在脑子里推演：某级刚开局时，`in1_addr` 指向的槽位上一级还没写到，蝶形会吃到什么？

**需要观察的现象（思维实验）**：去掉等待后，状态机会在数据尚未就绪时就把陈旧的 `bufferX/Y` 内容送进蝶形，得到错误的 `YA/YB`，并继续污染后续所有级。

**预期结果**：最终 `out_x` 与 numpy 参考答案对不上。因此 `updated` 位图是「正确性」的守门员，不是可选项。

> 这是「源码阅读型实践」，不需要运行仿真；重点是理解第 418 行那一个 `&` 为什么不能省。

#### 4.4.5 小练习与答案

**练习 1**：第一级为什么把 `updated0/updated1` 固定成 1？

> **答**：第一级读的是 `bufferin`，里面是输入端口连续写入的整帧数据，进入第一级前已由 `bufferin_read_full` 保证整块就绪，不存在「部分槽位未写」的问题，所以无需查询、直接放行。

**练习 2**：级末清零时，为什么清的是 `readbuf_switch` 指向的那块，而不是另一块？

> **答**：`readbuf_switch` 指向「本级刚读完」的那块，它马上要变成下一级的**写目标**。写目标上的旧新鲜度位已经失效，必须清零，好让下一级的 `updated` 位随新写入逐位重新置起。另一块是下一级的读目标，它的 `updated` 位此刻全为 1（刚被本级写满），正是下一级要查询的，不能清。

---

### 4.5 输出缓存 bufferout：最后一级的汇聚与排出

#### 4.5.1 概念说明

`bufferout` 是单块输出缓存，职责有二：

1. **汇聚**：最后一级（`last_stage`）的蝶形结果不再写进 `bufferX/Y`，而是改写到 `bufferout`，把完整 N 点结果凑齐。
2. **排出**：凑齐后，由一个独立的读进程逐拍把结果送上端口 `out_x`，并拉高 `out_nd`。

和输入侧一样，`bufferout` 也用 **A/B 双标志**来表示满/空——因为它的「写方」（蝶形输出接收进程）和「读方」（输出排出进程）同样是两个不同的 `always` 块。

#### 4.5.2 核心流程

```
写方（接收进程）:
  最后一级每个蝶形输出 z → bufferout[out_addr_z]
  收到 finished_z_old[1]（一整帧结果写完）→ 翻转 bufferout_full_A

读方（排出进程）:
  if bufferout_full:
      out_x <= bufferout[bufferout_addr]; out_nd <= 1
      地址 +1；写满 N 个后翻转 bufferout_full_B
  else:
      out_nd <= 0
```

满标志 `bufferout_full = bufferout_full_A ⊕ bufferout_full_B`，与输入侧完全同构：写方翻 `_A`、读方翻 `_B`、相异即满。

#### 4.5.3 源码精读

`bufferout` 与其 A/B 标志的声明带着一段说明性注释（[dit.v:L70-L79](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L70-L79)），与 4.2 的输入侧遥相呼应：

```verilog
// Whether the output buffer is full.
// We have two registers since they are drive by different processes.
// 'A' flips back and forth as the buffer is fulled.
// 'B' flips back and forth as the buffer is emptied.
reg bufferout_full_A;
reg bufferout_full_B;
wire bufferout_full;
assign bufferout_full = bufferout_full_A + bufferout_full_B;
```

**写方**：一帧写完翻转 `_A`（[dit.v:L518-L520](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L518-L520)）；最后一级的数据改写进 `bufferout`（[dit.v:L524-L527](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L524-L527)）：

```verilog
if (finished_z_old[1])
  bufferout_full_A <= ~bufferout_full_A;   // 一帧结果齐了
...
if ((last_stage_z & z_nd)|(last_stage_z_old[0] & ~z_nd))
  bufferout[out_addr_z] <= z;              // 最后一级结果入 bufferout
```

**读方（排出进程）**（[dit.v:L154-L175](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L154-L175)）：满则逐拍输出，空则静默：

```verilog
if (bufferout_full) begin
   out_x <= bufferout[bufferout_addr];
   out_nd <= 1'b1;
   bufferout_addr <= bufferout_addr + 1;
   if (&bufferout_addr)
     bufferout_full_B <= ~bufferout_full_B;   // N 个排空，翻 _B
end
else
   out_nd <= 1'b0;
```

注意输出进程与输入写进程（4.2）、状态机是**三个相互独立**的 `always` 块，分别管「排出口」「入口」「计算调度」——这就是 dit 能让数据流连续不断的结构性原因。

#### 4.5.4 代码实践

**实践目标**：验证输出是「凑齐 N 个后排成连续 N 拍」送出的。

**操作步骤**：

1. 读 [dit.v:L164-L175](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L164-L175)，确认只要 `bufferout_full` 为 1，每个上升沿都会 `out_nd <= 1` 且地址递增。
2. 再看测试台如何收集输出（[qa_dit.py:L174-L175](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L174-L175)）：每收到一个 `out_nd` 就把 `out_data` 追加到 `self.output`，最后按每 N 个一组与 numpy 比对（[qa_dit.py:L214-L229](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L214-L229)）。

**需要观察的现象**：`out_nd` 会在 `bufferout` 满后连续拉高 N 拍（一帧），然后拉低等待下一帧填满。

**预期结果**：测试台 `self.output` 的长度恰等于输入总样本数（`assertEqual(len(tb.output), len(data))`，[qa_dit.py:L214](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L214)），且按 N 个一组可正确切分成多帧 FFT。

> 若无仿真环境，上述为「源码阅读型实践」，结论可由第 164–175 行直接读出。

#### 4.5.5 小练习与答案

**练习 1**：`bufferout` 只有一块，为什么不会出现「写方和读方争同一块」的问题？

> **答**：A/B 标志保证了「满才读、空才写」的互斥：`bufferout_full` 为 1 时读方排空、写方不会同时写（因为一帧只在 `finished_z_old[1]` 那一拍触发写方的 `_A` 翻转，且新帧的最后一级数据要在状态机进入新一轮后才会到来）；为 0 时写方填充、读方静默。读写被满标志隔开到不同时段，单块即够用。

**练习 2**：为什么 `bufferout` 用了和 `bufferin` 完全相同的 A/B 机制？

> **答**：两者面对的是同一个底层问题——一个缓冲被两个不同的 `always` 进程一写一读，无法共用单个满标志 `reg`。A/B 双标志 + 异或是这个问题的通用解法，所以输入侧和输出侧形态一致；区别只在于输入侧额外把缓冲**复制成两块**做乒乓（因为入口流速受外部控制、需要更大缓冲余量），而输出侧单块即可。

---

## 5. 综合实践

把本讲的所有缓冲知识串起来，完成下面这个贯穿性任务。

**任务**：在 `dit.v` 上追踪一次完整 FFT 的数据旅程，并解释双缓存的并行意义。

**步骤**：

1. **画通路**。在一张纸上画出 4.1.2 的方框图，然后用不同颜色的笔标注：
   - 第一帧数据的路径：`in_x → bufferin0 →（第一级）→ bufferX →（第二级）→ bufferY →（第三级）→ bufferX →（第四级/最后）→ bufferout → out_x`（以 N=16 为例，按 4.3.4 的表）。
   - 同时标注 `updatedX/updatedY` 在每级开始/结束时的状态（级末清写目标、级中逐位置 1）。
2. **解释并行**。回答：当状态机正在算第一帧的第二级（读写 `bufferX/Y`）时，端口 `in_x` 送来的**第二帧**数据会进哪里？为什么不会和第一帧的计算冲突？
3. **定位溢出**。指出是哪一行代码（[dit.v:L119-L120](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L119-L120)）负责在「并行被打破」时报警，并说明触发条件。
4.（可选，需本地环境）把 [`qa_dit.py`](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py) 的 `DEBUGMODE` 打开（在 `dut_dit.v` 例化 `dit` 时补上 `DEBUGMODE=1` 参数），运行仿真，从 `$display` 日志里观察 `Input Buffer No Longer Full`、`NEXT STAGE`、`FINISHED LAST STAGE` 等信息，把它们与你画的通路一一对应。

**参考答案要点**：

- 第 2 步：第二帧进 `bufferin1`（因为写方 `bufferin_write_switch` 已在第一帧填满 `bufferin0` 后切到 `bufferin1`）。第一帧的计算只碰 `bufferX/Y`，两者物理上与 `bufferin1` 无交集，所以并行不冲突——这正是输入**双**缓存（而非单缓存）的价值。
- 第 3 步：第 119–120 行，当 `bufferin_write_full`（写方瞄准的块仍满）时置 `overflow`。它表示读方（状态机第一级）还没排空上一块，写方又来抢，并行节奏被打破。

## 6. 本讲小结

- `dit` 用**五块缓冲**组织数据流：输入双缓存 `bufferin0/bufferin1`、工作缓存 `bufferX/bufferY`、输出缓存 `bufferout`，外加两张新鲜度位图 `updatedX/updatedY`。
- **A/B 双标志**（`_A` 写方、`_B` 读方、`full = _A ⊕ _B`）解决了「两个 `always` 进程不能驱动同一个 `reg`」的难题，输入侧和输出侧都用它。
- 输入侧在 A/B 之上再做**双块乒乓**（`bufferin_write_switch` / `bufferin_read_switch`），让「接收下一帧」与「计算当前帧」并行；写方过快则触发粘住的 `overflow`。
- 工作缓存 `bufferX/bufferY` 靠 `readbuf_switch` 每级翻转实现乒乓，第一级读 `bufferin`、最后一级写 `bufferout`。
- `updatedX/updatedY` 逐槽位标记「已写入」，`SEND` 状态据此等待，防止读到未写好的位置；级末清零「即将被写」那块，且必须与置位操作放在同一个 `always` 块以避免多驱动冲突。
- 入口写进程、出口读进程、计算状态机是三个独立 `always` 块，构成 dit 连续数据流的骨架。

## 7. 下一步学习建议

- 想搞清「各级之间到底什么时候切换、`finished` 何时拉高」，请进入 **u3-l2 dit 控制状态机：INIT/IDLE/CALC/SEND**，那里会逐状态拆解本讲里点到为止的 `FSM_ST_*` 转移。
- 想搞清「`in0_addr` / `in1_addr` / `out0_addr` / `tf_addr` 到底怎么算」，请进入 **u3-l3 地址计算**，那里会解释 `dit.v` 顶部那段 E_k/O_k 数学注释如何变成位运算。
- 若想再看一遍缓冲在真实波形里的表现，可回到 **u4-l1 MyHDL 协同仿真**，结合本讲的通路图观察 `din_nd` / `dout_nd` / `overflow` 的时序。
