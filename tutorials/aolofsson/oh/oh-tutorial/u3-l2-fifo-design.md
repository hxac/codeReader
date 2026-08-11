# FIFO 设计：同步、异步与跨时钟域

## 1. 本讲目标

FIFO（First-In First-Out，先进先出队列）是芯片里最常用的“弹性缓冲”：生产端和消费端速度不匹配时，用它吸收突发、解耦两侧时序。本讲学完后你应该能够：

- 说清**同步 FIFO** 如何用“读写指针 + 计数器”判断满和空，并能手算 `wr_count` / `wr_full` / `rd_empty` 的时序。
- 说清为什么跨时钟域 FIFO 必须用**格雷码指针**，以及 `oh_bin2gray` / `oh_gray2bin` 的转换原理。
- 读懂 OH! 的**异步 FIFO**（`oh_fifo_async`）如何把上一讲的 `oh_dsync` / `oh_rsync` 与本讲的格雷指针拼在一起。
- 会使用 `almost_full` / `prog_full` 做提前反压，并理解 `oh_fifo_cdc` 这一层 valid/ready 封装。

本讲承接 [u3-l1 存储原语](u3-l1-memory-primitives.md)（FIFO 内部用的 `oh_dpram`）和 [u2-l4 跨时钟域同步原语](u2-l4-cdc-synchronizers.md)（同步器与亚稳态），把这两块拼成一个完整组件。

## 2. 前置知识

- **同步逻辑与时钟**：见 [u2-l2 时序原语](u2-l2-sequential-flops.md)。FIFO 的指针本质是一组带时钟的计数器。
- **双口 RAM**：见 [u3-l1 存储原语](u3-l1-memory-primitives.md)。FIFO 的存储体就是 `oh_dpram`——一个写口、一个读口。
- **亚稳态与同步器**：见 [u2-l4 跨时钟域同步原语](u2-l4-cdc-synchronizers.md)。异步 FIFO 的核心难点就是把一个时钟域里的指针“安全地”递交给另一个时钟域。
- **soft/hard 双实现**：同一份 RTL 用字符串参数（`SYN`/`TYPE`/`TARGET`）+ `generate if` 在可综合 RTL 与 ASIC 硬核之间切换。本讲会再次看到这个模式，并指出 `oh_fifo_sync` 里一个参数名对不上的历史遗留问题。

**一句话直觉**：FIFO = 一块环形 RAM + 一个“写到哪了”的指针 + 一个“读到哪了”的指针。所谓满和空，就是这两个指针的相对关系。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `stdlib/rtl/oh_fifo_sync.v` | 同步 FIFO（单时钟），读写指针 + count + almost_full，内部用 `oh_dpram` |
| `stdlib/rtl/oh_bin2gray.v` | 二进制 → 格雷码编码器（一行 `assign`） |
| `stdlib/rtl/oh_gray2bin.v` | 格雷码 → 二进制译码器（`for` 循环做前缀异或） |
| `stdlib/rtl/oh_fifo_async.v` | 异步 FIFO（双时钟），用格雷码指针 + `oh_dsync` 跨域 |
| `stdlib/rtl/oh_fifo_cdc.v` | 在异步 FIFO 外面再包一层 valid/ready 握手的 CDC 接口 |
| `stdlib/testbench/dut_fifo_generic.v` | 把 `oh_fifo_cdc` 接入标准测试平台的 DUT 包装范例 |

> 阅读提示：OH! 是真实流片过的库，但仓库里存在文档/参数名与实际代码漂移的历史问题。本讲凡遇到这类不一致，都会显式标出，并请以**源码实际端口/参数**为事实。

## 4. 核心概念与源码讲解

### 4.1 同步 FIFO：指针、计数与满空判断

#### 4.1.1 概念说明

同步 FIFO 的“同步”指：写口和读口**共用同一个时钟** `clk`。因为没有跨时钟域问题，指针可以直接用普通二进制计数器，满空判断也只是组合逻辑。

关键设计点是**给指针多留 1 位**。设 FIFO 深度为 `DEPTH`，地址位宽 `AW = $clog2(DEPTH)`，那么真正寻址 RAM 只需要 `AW` 位；但指针本身被声明为 `AW+1` 位，多出来的最高位（MSB）叫**回绕位（wrap bit）**。它的作用是区分“满”和“空”这两种“两个指针看起来相等”的情况：

- 两个指针的**低 `AW` 位相等、MSB 也相等** → 写指针追上读指针、没多绕一圈 → **空**。
- 两个指针的**低 `AW` 位相等、MSB 不同** → 写指针比读指针多绕了一整圈 → **满**。

这样就不必精确数 FIFO 里到底有几个数，只需比较指针即可。

#### 4.1.2 核心流程

```
每个 clk 上升沿（同步 FIFO）：
  fifo_write = wr_en && !wr_full      // 写许可
  fifo_read  = rd_en && !rd_empty     // 读许可

  if (fifo_write) wr_addr ++          // 写指针前进
  if (fifo_read)  rd_addr ++          // 读指针前进

  ptr_match = (wr_addr[AW-1:0] == rd_addr[AW-1:0])   // 低地址位相等？
  wr_full   = ptr_match && (wr_addr[AW] != rd_addr[AW])  // 多绕一圈 → 满
  rd_empty  = ptr_match && (wr_addr[AW] == rd_addr[AW])  // 没绕 → 空

  wr_count 随 fifo_write/+1、fifo_read/-1 更新
  wr_almost_full = (wr_count == PROGFULL)            // 提前告警
```

满空判断的“回绕位”直觉可以用数学表示。设指针宽 \(n+1\) 位、深度 \(2^n\)，写指针为 \(W\)、读指针为 \(R\)：

\[
\text{empty} \iff W = R
\]

\[
\text{full} \iff W = R + 2^n \pmod{2^{n+1}}
\]

后者在二进制下的表现正是“低 \(n\) 位全等、最高位翻转”。

#### 4.1.3 源码精读

参数与端口——注意 `AW` 是由 `DEPTH` 派生的派生参数，`PROGFULL` 默认取 `DEPTH-1`：

[stdlib/rtl/oh_fifo_sync.v:8-17](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_fifo_sync.v#L8-L17) — 定义位宽 `N`、深度 `DEPTH`、可编程满阈值 `PROGFULL`，以及派生的 `AW = $clog2(DEPTH)`。

满空判断的核心，就是上一节那几行组合逻辑：

[stdlib/rtl/oh_fifo_sync.v:65-70](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_fifo_sync.v#L65-L70) — `fifo_write`/`fifo_read` 是带满空保护的写读许可；`wr_full` 用“低地址相等且 MSB 不同”判定；`rd_empty` 用“低地址相等且 MSB 相同”判定；`wr_almost_full` 直接比 `wr_count`。

指针与计数器在一个 `always` 块里更新，按“既写又读 / 只写 / 只读”分支处理，保证 `wr_count` 与指针一致：

[stdlib/rtl/oh_fifo_sync.v:72-99](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_fifo_sync.v#L72-L99) — 异步低有效复位 `nreset` 与同步 `clear` 都把指针和计数清零；“既写又读”时计数不变（一进一出）；“只写”+1，“只读”-1。

存储体就是把 `oh_dpram` 例化进来，地址分别接 `wr_addr`/`rd_addr` 的低 `AW` 位：

[stdlib/rtl/oh_fifo_sync.v:111-116](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_fifo_sync.v#L111-L116) — 用 `wr_addr[AW-1:0]` / `rd_addr[AW-1:0]` 寻址，回绕位不参与寻址，只用于满空比较。

> ⚠️ **排错提示（待本地验证）**：这里把 `.SYN()` / `.TYPE()` 传给了 `oh_dpram`，但 [oh_dpram 的参数叫 `TARGET`](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dpram.v#L8-L15)（并没有 `SYN`/`TYPE`）。参数名对不上，编译时这两个命名参数会被忽略或告警。这与 u3-l1 已指出的“`oh_fifo_sync` 向 `oh_dpram` 误传参数名”是同一个历史遗留。仿真若报参数相关错误，先查这里。

> ⚠️ **阅读提示**：文件第 28 行声明了 `output wr_prog_full`、第 105 行用到了 `empty`，但通篇找不到对它们的驱动（实际有效的空标志是 `rd_empty`）。这两个属于历史残留信号，读代码时请以 `rd_empty` / `wr_full` / `wr_almost_full` 为准。

#### 4.1.4 代码实践

**目标**：不写一行仿真代码，先用纸笔把 `oh_fifo_sync` 的关键时序算出来，验证你真的理解了满空判断；再（可选）上仿真确认。

**操作步骤**：

1. 取默认 `DEPTH=4`，但把 `PROGFULL` 从默认的 `3` 改成 `2`。算出 `AW = $clog2(4) = 2`，指针为 3 位 `[2:0]`，`wr_count` 为 2 位 `[1:0]`。
2. 假设**只写不读**，从复位态出发，逐拍写下表中的值（`wr_en=1, rd_en=0`）。

| 拍 | wr_addr[2:0] | rd_addr[2:0] | wr_count | wr_almost_full | wr_full | rd_empty |
|----|--------------|--------------|----------|----------------|---------|----------|
| 复位 | 000 | 000 | 0 | 0 | 0 | 1 |
| 写1 | 001 | 000 | 1 | 0 | 0 | 0 |
| 写2 | 010 | 000 | 2 | **1** | 0 | 0 |
| 写3 | 011 | 000 | 3 | 0 | 0 | 0 |
| 写4 | 100 | 000 | 0¹ | 0 | **1** | 0 |
| 写5 | 100（被 wr_full 挡住） | 000 | 0 | 0 | 1 | 0 |

¹ `wr_count` 只有 `AW=2` 位（能表示 0..3），第 4 次写入后 3+1 回绕成 0。这是 `wr_count` 位宽的固有局限，注释里把它叫“pessimistic report（悲观计数）”正是这个意思——它无法表示“恰好满”。

**需要观察的现象**：

- `wr_almost_full` 在 `wr_count` 命中 `PROGFULL=2` 的那一拍（写2）拉高，比真正满**提前**——这正是提前反压的用途。
- `wr_full` 在第 4 次写入后、写指针回绕位翻转为 1 时拉高，第 5 次写被挡住。
- `rd_empty` 在第一个数据写入后立即为 0。

**预期结果**：上表与你的手算一致。

**可选仿真验证（待本地验证）**：仿照 [u1-l3 仿真环境搭建](u1-l3-simulation-setup.md) 用 iverilog 编译，写一个只产生 `clk`/`nreset`/`wr_en` 的最小 testbench 例化 `oh_fifo_sync`，用 gtkwave 看上述信号。注意先把 4.1.3 提到的 `.SYN()`/`.TYPE()` 参数名问题处理掉（改成 `oh_dpram` 实际的 `.TARGET()`），否则可能无法顺利编译。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `DEPTH` 设成 8，`wr_count` 几位？最大能表示到几？  
**答**：`AW = $clog2(8) = 3`，`wr_count` 是 3 位，能表示 0..7；而 FIFO 真正满时是 8 项，所以 `wr_count` 在“恰好满”时同样会回绕，不能用来判断绝对满。

**练习 2**：为什么满空判断里要单独比较 MSB（回绕位），而不能只比较低 `AW` 位？  
**答**：低 `AW` 位相等既可能是“空”（两指针重合）也可能是“满”（写指针绕了一整圈追上读指针）。只有再看 MSB 是否翻转，才能区分这两种情况。

**练习 3**：`ptr_match` 为真、`wr_addr[AW]==rd_addr[AW]` 时是空还是满？  
**答**：是空（`rd_empty`）。MSB 相同表示写指针没有多绕一圈。

---

### 4.2 格雷码指针：让指针能安全跨时钟域

#### 4.2.1 概念说明

异步 FIFO 的读写指针分别在两个不同步的时钟域里跑。要把“写指针”送给读时钟域去判空、把“读指针”送给写时钟域去判满，就必须跨时钟域（CDC）。

问题在于：一个 6 位二进制数从 `011111` 变到 `100000` 时，**所有 6 位同时翻转**。接收域的采样沿若恰好卡在中间，由于各比特走线延迟不同，可能采到 `000000`、`111111` 甚至任意乱码——这就是 [u2-l4](u2-l4-cdc-synchronizers.md) 讲过的多比特 CDC 灾难。

**格雷码（Gray Code）** 的妙处在于：**相邻两个数只有 1 位不同**。这样指针每拍只翻转 1 位，接收域即使采到翻转瞬间，结果也只是“旧值或新值二者之一”，绝不会出现多位乱码；再串一级同步器（`oh_dsync`）消除亚稳态，就安全了。

#### 4.2.2 核心流程

设二进制指针为 \(B = b_{n-1}\dots b_0\)，格雷码为 \(G = g_{n-1}\dots g_0\)。

**二进制 → 格雷码**：本位与高一位异或。

\[
g_{n-1} = b_{n-1},\qquad g_i = b_i \oplus b_{i+1}\ (i<n-1)
\]

写成整体就是把二进制数右移一位再与自身异或：\(G = B \oplus (B \gg 1)\)。

**格雷码 → 二进制**：从最高位开始做前缀异或。

\[
b_{n-1} = g_{n-1},\qquad b_i = g_i \oplus g_{i+1} \oplus \dots \oplus g_{n-1}
\]

（在 FIFO 里其实很少需要 gray2bin——满空判断直接拿格雷码比就行，`oh_gray2bin` 更多用在测试或调试里把指针还原成可读的二进制。）

**关键性质**：相邻整数 \(k\) 与 \(k+1\) 的格雷码恰好差 1 位。

#### 4.2.3 源码精读

`oh_bin2gray` 简洁到只有一行——正是 \(G = B \oplus (B \gg 1)\)：

[stdlib/rtl/oh_bin2gray.v:16](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_bin2gray.v#L16) — `{1'b0, in[N-1:1]}` 就是把输入右移一位、最高位补 0，再与原值异或。

`oh_gray2bin` 用双层 `for` 循环实现“前缀异或”：最高位照抄，每一位 \(b_i\) 等于所有 \(g_{j\ge i}\) 的异或：

[stdlib/rtl/oh_gray2bin.v:22-31](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_gray2bin.v#L22-L31) — `bin[N-1]=gray[N-1]`；对每个 `i`，内层循环把 `gray[i..N-1]` 逐位异或进 `bin[i]`，正好实现前缀异或公式。这是纯组合逻辑（`always @*` + 阻塞赋值）。

#### 4.2.4 代码实践

**目标**：用纸笔验证“相邻格雷码差 1 位”，体会它为何对 CDC 安全。

**操作步骤**：

1. 取 4 位指针（n=4），手算 `0..8` 的二进制与格雷码（套用 \(G = B \oplus (B\gg1)\)）。
2. 填表，逐行检查“当前行与上一行差几位”。

**参考答案（前几行）**：

| 十进制 | 二进制 B | 格雷码 G = B^(B>>1) | 与上行差异位数 |
|--------|----------|---------------------|----------------|
| 0 | 0000 | 0000 | — |
| 1 | 0001 | 0001 | 1 位 |
| 2 | 0010 | 0011 | 1 位 |
| 3 | 0011 | 0010 | 1 位 |
| 4 | 0100 | 0110 | 1 位 |
| 5 | 0101 | 0111 | 1 位 |

**需要观察的现象**：每一行与上一行都恰好差 1 位。

**预期结果**：因此采样跨域指针时，最坏情况只是采到“上一拍或本拍”之一，不会出现多位乱码——这就是异步 FIFO 敢于用单比特同步器传送多比特指针的根本原因。

**可选练习**：用 `oh_gray2bin` 把你算出的格雷码再译回二进制，确认能还原原值（round-trip）。

#### 4.2.5 小练习与答案

**练习 1**：格雷码 `0110` 对应的二进制是多少？  
**答**：从高位做前缀异或。b3=g3=0；b2=g2⊕b3=1⊕0=1；b1=g1⊕b2=1⊕1=0；b0=g0⊕b1=0⊕0=0。即二进制 `0100`（十进制 4）。

**练习 2**：为什么异步 FIFO 跨域传指针用格雷码，而同步 FIFO 不用？  
**答**：同步 FIFO 读写同域，指针在本域内比较，没有采样错位问题，用二进制即可，逻辑也更简单。跨域才需要格雷码保证“每次只变 1 位”。

---

### 4.3 异步 FIFO：双时钟域的满空判断

#### 4.3.1 概念说明

`oh_fifo_async` 把前面的积木拼起来：两套独立时钟 `wr_clk` / `rd_clk`，两套二进制指针，各自转成格雷码后，用 `oh_dsync`（[u2-l4](u2-l4-cdc-synchronizers.md) 的电平同步器）送到对岸去比较。复位也要先过 `oh_rsync`（异步生效、同步释放），保证两个域不会在复位释放瞬间各自看到不同的复位状态。

它内部仍是 `oh_dpram` 做存储体；数据通路本身不需要跨域同步（数据写到 RAM、从 RAM 读出，各自由本侧时钟控制），**真正需要跨域的只有“指针”这一个信号**。

#### 4.3.2 核心流程

```
写侧（wr_clk 域）：
  wr_addr 二进制计数 → bin2gray → wr_addr_gray
                                        │
                       oh_dsync(到 rd_clk) ──► 读侧判空
  读侧（rd_clk 域）：
  rd_addr 二进制计数 → bin2gray → rd_addr_gray
                                        │
                       oh_dsync(到 wr_clk) ──► 写侧判满

判空（在 rd_clk 域，用同步过来的写指针）：
  rd_empty = (rd_addr_gray == wr_addr_gray_sync)        // 全等 → 空

判满（在 wr_clk 域，用同步过来的读指针）：
  wr_full  = (wr_gray 低 AW 位 == rd_gray_sync 低 AW 位)
           & (wr_gray 最高位 != rd_gray_sync 最高位)
```

**判空为什么安全**：读侧拿同步过来的写指针和自己比。即便同步把写指针“看旧了”，也只是把 FIFO 当成更空一些——**保守判空是安全的**：最多少读几个本可读的数据，绝不会读出无效数据。

**判满的标准做法**（教科书规则，见 Cummings 的经典论文）：两个二进制指针“低 n 位相等、最高位不同”等效为满；映射到格雷码后，对应的是**最高两位都翻转、其余位相等**。

> ⚠️ **阅读提示（待本地验证）**：OH! 在 [oh_fifo_async.v:128-129](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_fifo_async.v#L128-L129) 写的判满条件是“低 `AW` 位（含次高位）全等、且最高位不同”，只翻了最高 1 位，与上面“最高两位都翻转”的教科书规则不完全一致。字面上看，当写指针绕满一整圈（如 `DEPTH=32`、`wr` 从 0 走到 32、`rd` 仍为 0）时，这条 `wr_full` 未必按预期拉高。加之 4.3.3 会指出 `wr_almost_full`/`wr_prog_full` 在本文件里并未被驱动，**实际工程中通常依赖外层 `oh_fifo_cdc` 的反压来避免溢出**。请把这段当作“读源码时的排错线索”，行为以本地仿真为准。

#### 4.3.3 源码精读

复位先各自同步到本域（异步生效、同步释放）：

[stdlib/rtl/oh_fifo_async.v:54-64](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_fifo_async.v#L54-L64) — 分别为写域、读域各例化一个 `oh_rsync`，得到 `wr_nreset` / `rd_nreset`。

写指针（二进制）→ 格雷码 → 同步到读域，用于判空：

[stdlib/rtl/oh_fifo_async.v:92-102](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_fifo_async.v#L92-L102) — `oh_bin2gray` 把 `wr_addr` 转成 `wr_addr_gray`；`oh_dsync[AW:0]` 是**位数组例化**（数组 `[AW:0]` 表示把同步器复制 AW+1 份，每份同步 1 位），把写指针同步到 `rd_clk` 域。

对称地，读指针 → 格雷码 → 同步到写域，用于判满：

[stdlib/rtl/oh_fifo_async.v:108-118](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_fifo_async.v#L108-L118) — 读指针走完全对称的路径送到 `wr_clk` 域。

满空最终比较（直接比格雷码，不需要 gray2bin）：

[stdlib/rtl/oh_fifo_async.v:125-129](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_fifo_async.v#L125-L129) — `rd_empty` 是格雷码全等；`wr_full` 比低 `AW` 位与最高位（如 4.3.2 所述，此处与教科书规则有出入，待本地验证）。

存储体 `oh_dpram`——注意这里用的是正确的 `.TARGET()` 参数（和同步 FIFO 的 `.SYN()/.TYPE()` 形成对比）：

[stdlib/rtl/oh_fifo_async.v:135-139](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_fifo_async.v#L135-L139) — 异步 FIFO 给 `oh_dpram` 传 `.TARGET()`，参数名是匹配的；BIST/电源口接固定常数（异步 FIFO 不暴露这些）。

> ⚠️ **阅读提示**：本文件端口表里声明了 `wr_almost_full`、`wr_prog_full`、`wr_count`、`rd_count`（[L28-L36](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_fifo_async.v#L28-L36)），但通篇没有驱动它们。可用的状态输出只有 `wr_full` 与 `rd_empty`。其它视为占位/预留。

#### 4.3.4 代码实践

**目标**：把异步 FIFO 的“两个同步方向”画清楚，确认你理解谁把谁同步给谁。

**操作步骤**：

1. 打开 `oh_fifo_async.v`，用两种颜色分别标出“写→读”和“读→写”两条同步路径。
2. 回答三个问题：
   - 判空在哪一域完成？用到哪个同步器？
   - 判满在哪一域完成？用到哪个同步器？
   - 为什么 `wr_addr` 本身（二进制）不跨域，而是先转格雷码？

**需要观察的现象 / 预期结果**：

- 判空在 **读域**，用的是 **写指针同步到读域** 的那条 `oh_dsync`（L97-L102）。
- 判满在 **写域**，用的是 **读指针同步到写域** 的那条 `oh_dsync`（L113-L118）。
- 二进制不跨域是因为多位同时翻转会被采成乱码（4.2 已述）；格雷码每拍只翻 1 位，配合同步器才安全。

**可选上板（待本地验证）**：给 `wr_clk` 和 `rd_clk` 两个不同频率的方波，写若干笔数据再读出，用 gtkwave 观察 `wr_addr_gray` 与 `wr_addr_gray_sync` 之间的延迟与“最多差一两拍”的保守性。

#### 4.3.5 小练习与答案

**练习 1**：`oh_dsync[AW:0]` 这种带数组下标的例化是什么意思？  
**答**：这是 Verilog 的数组例化（instance array），把同一个模块复制 `AW+1` 份，每份处理指针的 1 位，从而用一个模块名同步整条总线。

**练习 2**：为什么异步 FIFO 的复位要过 `oh_rsync`，而同步 FIFO 不用？  
**答**：异步 FIFO 两个域各自复位释放，若不同步释放，一侧可能先开始计数、另一侧还在复位，指针错乱。`oh_rsync` 保证复位“异步生效、同步释放”到各自域。同步 FIFO 单域，直接用 `nreset` 即可。

**练习 3**：判空时“把写指针看旧一点”为什么是安全的，而“看新一点”就不安全？  
**答**：看旧 → 认为 FIFO 更空 → 可能少读几个本来已有的数据（保守，不丢数据）；看新 → 认为 FIFO 更满 → 可能多读还没真正写完的数据（危险）。格雷码 + 同步保证接收端不会“看新”，只会“看旧或当前”。

---

### 4.4 CDC FIFO 封装：valid/ready 握手

#### 4.4.1 概念说明

`oh_fifo_async` 暴露的是底层 `wr_en`/`rd_en`/`wr_full`/`rd_empty` 这类信号，对上层使用者不够友好。`oh_fifo_cdc` 在外面再包一层，把它换成更通用的 **valid/ready 握手**：

- **写侧**（`clk_in`）：`valid_in` + `packet_in` + `ready_out`（生产者看 `ready_out` 决定能不能写）。
- **读侧**（`clk_out`）：`valid_out` + `packet_out` + `ready_in`（消费者给 `ready_in` 表示自己能接）。

这层封装是后续 emesh/elink 等模块复用 FIFO 时的标准接口（`packet_in/out` 就是一整个 emesh 包）。

#### 4.4.2 核心流程

```
wr_en      = valid_in                                  // 生产者发起即写
rd_en      = ~empty & ready_in                         // 非空且消费者就绪才读
ready_out  = ~(wr_almost_full | wr_full | wr_prog_full)// 任一“将满”标志即反压

valid_out  <= rd_en  （当 ready_in 时）                 // 把读使能对齐到读延迟
```

`ready_out` 把三个“将满”标志“或”起来做反压——只要 FIFO 快满就提前告诉生产者别再塞，留出 CDC 同步延迟的余量。

#### 4.4.3 源码精读

核心三句控制逻辑就集中在文件开头：

[stdlib/rtl/oh_fifo_cdc.v:41-43](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_fifo_cdc.v#L41-L43) — `wr_en=valid_in`；`rd_en` 要同时满足“FIFO 非空”和“消费者 ready”；`ready_out` 是三个满标志的或非（任一拉高即反压）。

读侧 `valid_out` 用一个寄存器对齐读延迟（因为 FIFO 读出数据本身有一拍延迟）：

[stdlib/rtl/oh_fifo_cdc.v:52-56](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_fifo_cdc.v#L52-L56) — `valid_out` 在 `ready_in` 时跟随 `rd_en`，保证 valid 与数据同拍到达下游。

内部直接例化 `oh_fifo_async`：

[stdlib/rtl/oh_fifo_cdc.v:59-62](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_fifo_cdc.v#L59-L62) — 把 `ready_out` 反压需要的 `wr_full`/`wr_almost_full`/`wr_prog_full` 都接出来（注意 4.3.3 指出后两者在 `oh_fifo_async` 里未被驱动，实际反压主要靠 `wr_full`，待本地验证）。

> ⚠️ **排错提示（待本地验证）**：测试包装 [dut_fifo_generic.v:39-40](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dut_fifo_generic.v#L39-L40) 用 `oh_fifo_cdc #(.DW(PW), .DEPTH(16))` 例化，但 `oh_fifo_cdc` 的宽度参数叫 **`N`** 而不是 `DW`（见 [L8-L13](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_fifo_cdc.v#L8-L13)）。名字对不上，`N` 会取默认值 32，而 `packet_in[PW-1:0]`（PW=104）会变宽——这是仓库里又一处历史参数漂移，跑仿真前需要核对。

#### 4.4.4 代码实践

**目标**：读懂 `dut_fifo_generic.v` 如何把 `oh_fifo_cdc` 接入标准测试平台，理解 valid/ready 数据流。

**操作步骤**：

1. 打开 `stdlib/testbench/dut_fifo_generic.v`，找到 `oh_fifo_cdc` 的例化（L39-L52）。
2. 画出数据流：`access_in`/`packet_in`（来自测试激励）→ `clk_in` 域写进 FIFO → `clk_out` 域读出 → `access_out`/`packet_out`（回到测试平台）。
3. 在图上标出反压回路：`wr_full` 等满标志 → `ready_out` → 测试平台的 `wait_in`。

**需要观察的现象**：

- 这是一个**回环式**的 DUT：激励从一侧塞包，从另一侧取包，FIFO 在两个时钟域（`clk1`/`clk2`）之间做缓冲。
- `dut_active=1`、`clkout=clk2` 这些是把 DUT 接入 `dv_top` 测试骨架的固定 tie-off（详见 [u4 通用测试平台](u4-l1-testbench-framework.md)）。

**预期结果**：你能向同伴讲清“一个包从 `packet_in` 到 `packet_out` 经过哪些信号、FIFO 满了如何通过 `ready_out`/`wait_in` 让激励停下来”。

#### 4.4.5 小练习与答案

**练习 1**：`ready_out = ~(wr_almost_full | wr_full | wr_prog_full)` 中，为什么要把三个标志“或”起来而不是只用 `wr_full`？  
**答**：只用 `wr_full` 反压太晚——CDC 同步有延迟，等 `wr_full` 传到生产者时 FIFO 可能已经溢出。提前用 `almost_full`/`prog_full` 反压，给同步延迟留余量。

**练习 2**：`valid_out <= rd_en`（当 `ready_in`）为什么要寄存一拍？  
**答**：FIFO 读数据本身有一拍延迟（`rd_en` 当拍发出，数据下一拍才到 `packet_out`），所以 valid 也要延迟一拍，才能与数据对齐。

---

## 5. 综合实践

**任务**：为 `oh_fifo_sync` 设计一次“写突发 + 读突发”的完整时序，并预测每个关键信号的翻转点。

设定：`DEPTH=8`，`PROGFULL=5`，复位后先连续写 8 个数据（`wr_en=1, rd_en=0`），第 9 拍起改为 `wr_en=0, rd_en=1` 连续读空。

要求：

1. 先算 `AW`、指针位宽、`wr_count` 位宽。
2. 画出（或列表）从复位到读空全过程中，每拍的 `wr_addr`、`rd_addr`、`wr_count`、`wr_full`、`wr_almost_full`、`rd_empty`。
3. 标出三个关键时刻：`wr_almost_full` 首次拉高的拍、`wr_full` 拉高的拍、读空后 `rd_empty` 再次拉高的拍。
4. 思考：如果把它换成 `oh_fifo_cdc`（两域），`ready_out` 会在大约哪一拍开始反压？为什么比 `wr_full` 更早？

**完成方式**：先纯纸笔推导（确定性答案，不需要环境），再（可选）写一个最小 testbench 用 iverilog/gtkwave 验证，遇到 4.1.3 / 4.4.3 提到的参数名问题先修正。仿真结果若与手算不符，以源码实际行为为准并记录差异。

## 6. 本讲小结

- **同步 FIFO** 用“读写指针 + 回绕位”判满空：低地址相等且 MSB 相同为空、MSB 不同为满；`wr_count` 受位宽限制无法表示“恰好满”，是悲观计数。
- **`wr_almost_full` / `prog_full`** 提供提前告警，是反压设计的支点；同步 FIFO 里它们可用，异步 FIFO 里部分输出未被驱动（需核对）。
- **跨时钟域指针必须用格雷码**，因为相邻值只差 1 位，配合 `oh_dsync` 可安全传输；`oh_bin2gray` 是 \(G=B\oplus(B\gg1)\)，`oh_gray2bin` 是前缀异或。
- **异步 FIFO** 满空判断分别在做：判空在读域（用同步过来的写指针）、判满在写域（用同步过来的读指针）；保守判空安全、保守判满靠外层反压兜底。
- **`oh_fifo_cdc`** 把底层 FIFO 包成 valid/ready 握手，并用 `ready_out = ~(满标志们)` 做提前反压。
- 本讲多次发现仓库的**参数名漂移**（`.SYN/.TYPE` vs `.TARGET`、`.DW` vs `.N`）与若干**未驱动输出**——这是阅读 OH! 真实源码必须具备的“以代码为准、留意历史遗留”的习惯。

## 7. 下一步学习建议

- **横向应用**：去看 FIFO 在系统里怎么用——`elink/hdl/erx_fifo.v`、`elink/hdl/etx_fifo.v`（[第 7 单元 elink](u7-l1-elink-overview.md)）、`spi/hdl/spi_master_fifo.v`，它们都是对本讲 FIFO 的真实封装。
- **纵向深化 CDC**：回到 [u2-l4](u2-l4-cdc-synchronizers.md) 对照 `oh_pulse2pulse` 的开环同步，体会“开环脉冲同步 vs 闭环 FIFO 同步”的适用场景。
- **存储体细节**：若想搞清 `REG` 参数对读延迟/频率的影响，回头读 [u3-l1 的 `oh_dpram`](u3-l1-memory-primitives.md) 的输出寄存器分支。
- **测试平台**：想完整跑通 `dut_fifo_generic` 的端到端仿真，继续学 [u4 通用测试平台](u4-l1-testbench-framework.md)，理解 `.emf` 激励如何驱动 valid/ready 接口。
