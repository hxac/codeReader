# 异步 FIFO 的满空标志与读指针重放

## 1. 本讲目标

本讲是「FIFO 设计」单元的收尾，专门回答三个在上一讲（u9-l3 格雷码指针）里被刻意按下不表的问题：

1. **满（full）和空（empty）到底怎么判？** 上一讲只说了「空比格雷码相等」，满标志的判定留到这里细讲——它不是简单的「指针相等」，而是「最高两位都不同、其余位都相同」。
2. **`words_stored`（当前存量）怎么算？** 异步 FIFO 读写分处两个时钟域，没有一个时钟能同时安全地看到两个指针，存量必须在一个域里「反推」。
3. **`reset_read_pointer` 是什么？** 这是一个同步 FIFO 没有的特殊机制：把读指针清零、写指针和存储内容原封不动，从而让同一批数据被「重放」读出第二遍。

学完本讲，你应该能够：

- 用二进制→格雷码的位运算，亲手推出「满 = 最高两位镜像 + 低位相同」这条判定的来历，而不仅是记住结论。
- 说清 `empty` 属于读时钟域、`full` 属于写时钟域，并解释为什么这样划分。
- 看懂 `words_stored` 在读时钟域里「把同步过来的写指针转回二进制、再减读指针」的算法，以及它为什么是一个保守（偏小）的近似值。
- 理解 `reset_read_pointer` 的重放语义，并能指出它在三套厂商架构里行为并不一致——只有自研行为级实现真正实现了重放。
- 动手给现有测试台补一个「写→重放→再读」的用例，把本讲三个知识点串起来验证。

## 2. 前置知识

本讲默认你已学完 **u9-l3（异步 FIFO 与格雷码指针）**，掌握以下概念（下文直接使用，不再重复定义）：

- **跨时钟域（CDC）与数据撕裂**：多比特信号跨时钟域若各比特翻转步调不一致，目的域会采到源端从未产生过的「混搭值」。
- **格雷码（Gray code）**：相邻整数只差一个比特（汉明距离为 1），跨域至多慢一拍、绝不撕裂。
- **二进制 ↔ 格雷码互换**：`binary_to_gray` 即 `b ⊕ (b≫1)`；`gray_to_binary` 是从高位到低位累积异或。
- **折回位（turn bit）**：异步 FIFO 指针比 RAM 地址多一位最高位，用来区分「绕了一圈回来」与「原地不动」。
- **`ff_synchroniser_vector`**：多比特同步器，`fifo_async` 用它把写指针同步进读域、读指针同步进写域。
- **Cummings 异步 FIFO 设计方法**：本模块的算法蓝本。

另外回顾一个 VHDL 细节：`work` 库里的 `to_bits(n)`（来自 `utils_pkg` 子模块，实现细节标注「待确认」）返回表示自然数 n 所需的位数，本模块用它推导地址宽度。本讲在讲指针宽度时会用符号化的「地址位数 + 1」，不依赖 `to_bits` 的精确返回值。

如果你对「同一 entity 多架构」「厂商库」还不熟，建议先翻 **u2-l1** 与 **u2-l2**。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [ip/memories/fifo/fifo_async.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd) | 异步 FIFO，三套架构 | `own_behavioural_async_fifo` 里的满空标志、`words_stored`、`reset_read_pointer` |
| [ip/memories/fifo/tb/tb_fifo_async.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_async.vhd) | 异步 FIFO 测试台 | 现有 9 个用例如何用断言覆盖满空与计数；以及它**没有**覆盖 `reset_read_pointer` 这一事实 |

辅助理解（非本讲精读，但会引用）：

- [ip/communication/spi/spi_interface.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_interface.vhd) —— `reset_read_pointer` 的真实消费者（多片选 TX 重放），是理解「为什么要重放」的最佳现实用例。
- [ip/ff_synchroniser/ff_synchroniser_vector.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser_vector.vhd) —— 指针跨域同步的叶子模块（u9-l3 已详讲）。
- [ip/memories/ram/dual_clock_dual_port_ram.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/dual_clock_dual_port_ram.vhd) —— 存储底座（u6-l3 已详讲）。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，对应规格里的三个知识点：

- **4.1 满空标志的判定逻辑**（`full` / `empty` 怎么算）
- **4.2 `words_stored` 填充水位计算**（存量怎么算）
- **4.3 `reset_read_pointer` 与数据重放**（重放怎么实现）

三个模块共享同一份 `own_behavioural_async_fifo` 架构。我们先把这份架构里和本讲相关的信号列出来，作为后文的「公共底图」。

### 公共底图：指针与同步链

`own_behavioural_async_fifo` 架构里，指针是一个比地址宽一位的无符号数（这就是「折回位」）：

[ip/memories/fifo/fifo_async.vhd:188-201](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L188-L201) —— 声明 `pointer_t`（地址宽度 +1 位）、二进制与格雷码形态的读写指针、以及同步到对端域的副本，还派生出实际的 RAM 地址（去掉最高位）。

为后文叙述方便，设：

- 地址位数 \(A\)（即 `ADDRESS_WIDTH`），存储深度 \(D = 2^A\)。
- 指针位数 \(A+1\)，下标从 `0`（最低位）到 `A`（最高位，即 `'high`，也就是**折回位**）。
- 二进制指针记作 \(b\)，格雷码指针记作 \(g\)。

两条同步链（u9-l3 已详讲）把指针搬进对端时钟域：

[ip/memories/fifo/fifo_async.vhd:258-282](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L258-L282) —— 写指针格雷码同步进读域得到 `write_pointer_gray_sync`；读指针格雷码同步进写域得到 `read_pointer_gray_sync`。`in_data_valid` 恒接 `'1'`。

记住一个关键归属：

| 信号 | 产生于 | 消费于 | 说明 |
|------|--------|--------|------|
| `empty` | 读域（用本域读指针 + 同步过来的写指针） | 读域（读者用它决定能不能读） | 读侧标志 |
| `full` | 写域（用本域写指针 + 同步过来的读指针） | 写域（写者用它决定能不能写） | 写侧标志 |
| `words_stored` | 读域 | 读域（或上层监控） | 读域视角的存量 |

「谁用就在谁的域里算」是异步 FIFO 避免亚稳态的铁律。

---

### 4.1 满空标志的判定逻辑

#### 4.1.1 概念说明

同步 FIFO（u9-l1）判满空很简单：维护一个统一的填充水位计数器，满 = 水位到顶、空 = 水位为 0。但那是**单时钟域**，一个计数器谁都看得见。

异步 FIFO 读写两个时钟域互相看不见对方的指针。如果还想用「水位计数器」，这个计数器到底归哪个时钟？无论归哪边，另一边的增减都得跨域同步进来，又回到多比特 CDC 的老问题。

Cummings 的解法是**不维护单独的水位计数器，而是直接比较两个指针**。但比较又分两种情况：

- **空**：读指针追上了写指针，两者指向同一处 → 指针相等。
- **满**：写指针比读指针多走了整整一圈（\(D\) 步）→ 两者**在低位（地址位）上相等，但「绕了几圈」不同**。

问题来了：二进制指针「绕一圈」时低位全相等、只有最高位（折回位）不同，这很好判；可我们跨域传的是**格雷码**指针，不能直接套二进制的判定。于是需要把「满」的二进制几何特征，翻译成格雷码上的特征——这就是本模块要讲的核心。

> 一句话直觉：**空 = 两个格雷码指针完全相同；满 = 两个格雷码指针的最高两位都「镜像」、其余位都相同。** 下面我们从二进制严格推出来。

#### 4.1.2 核心流程

**空标志**（读域）：

1. 取本域读指针格雷码 `read_pointer_gray`。
2. 取同步过来的写指针格雷码 `write_pointer_gray_sync`。
3. 两者按位完全相等 → `empty = 1`。

> 为什么格雷码可以直接比相等？因为二进制→格雷码是**一一映射（双射）**：两个格雷码值相等，当且仅当它们对应的二进制值相等。所以「格雷码相等」⟺「二进制相等」⟺「指向同一处」。判空不需要反变换。

**满标志**（写域）：

1. 取本域写指针格雷码 `write_pointer_gray`。
2. 取同步过来的读指针格雷码 `read_pointer_gray_sync`。
3. 检查三件事：
   - 最高位（折回位，下标 `A`）**不同**；
   - 次高位（下标 `A-1`）**也不同**；
   - 剩下的低位（下标 `A-2 downto 0`）**全部相同**。
4. 三者同时成立 → `full = 1`。

**为什么是「最高两位都不同、低位全同」？** 用二进制→格雷码的定义推一遍（这是本讲最该吃透的一段）：

- 设「满」时写指针比读指针正好领先 \(D = 2^A\) 步。在 \(A+1\) 位二进制里，加 \(2^A\) **只翻转最高位（下标 \(A\)）、低位（\(A-1 \dots 0\)）完全不变**。即满的二进制特征是：\(b_w\) 与 \(b_r\) 仅在下标 \(A\) 上不同。
- 现在看格雷码 \(g_i = b_i \oplus b_{i+1}\)（最高位 \(g_A = b_A\)）：
  - 最高位 \(g_A = b_A\)：两端 \(b_A\) 不同 → **\(g_A\) 不同**。
  - 次高位 \(g_{A-1} = b_{A-1} \oplus b_A\)：两端 \(b_{A-1}\) 相同、\(b_A\) 不同 → 异或结果**不同**，即 **\(g_{A-1}\) 也不同**。
  - 更低位 \(g_i = b_i \oplus b_{i+1}\)（\(i < A-1\)）：两端 \(b_i, b_{i+1}\) 都相同 → \(g_i\) 相同。

把这三条合起来，正是源码里的判定。用数学写出来：

\[
\text{full} \iff \bigl(g_w[A] \neq g_r[A]\bigr)\ \land\ \bigl(g_w[A{-}1] \neq g_r[A{-}1]\bigr)\ \land\ \bigl(g_w[A{-}2{:}0] = g_r[A{-}2{:}0]\bigr)
\]

> **保守性**：由于 `read_pointer_gray_sync` 滞后于真实的读指针（同步链带来的几拍延迟），写域看到的读指针「偏旧」——读端实际已经读走了一些数据，但写端还不知道。结果是 `full` 可能**提前**（保守地）拉高：FIFO 其实还能再塞一两个，但写端会以为满了。这只损失一点点吞吐，**绝不会写溢出**，所以是安全的。同理，读域判空时用的 `write_pointer_gray_sync` 也滞后，`empty` 可能提前拉高、但绝不会读空读出脏数据。

#### 4.1.3 源码精读

空标志是一条并发赋值（组合逻辑），属于读域：

[ip/memories/fifo/fifo_async.vhd:284](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L284) —— `read_pointer_gray`（本域读指针）等于 `write_pointer_gray_sync`（同步进读域的写指针）即判空。两者都是格雷码，直接比相等即可。

满标志是一个 `process (all)` 的组合进程，属于写域，把上面三条判定翻译成三个布尔变量：

[ip/memories/fifo/fifo_async.vhd:286-295](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L286-L295) —— 三个变量分别对应「最高位不同」「次高位不同」「低位全同」，三者相与得 `full`。注意它比较的是 `write_pointer_gray`（本域）与 `read_pointer_gray_sync`（同步进写域的读指针）。

把进程体抽出来就是：

```vhdl
-- 最高位（折回位）：两端「绕圈次数」是否不同
pointers_msbs_are_different := write_pointer_gray(write_pointer_gray'high)
                            /= read_pointer_gray_sync(read_pointer_gray_sync'high);
-- 次高位：两端是否差了「半圈」
addresses_msbs_are_different := write_pointer_gray(write_pointer_gray'high - 1)
                            /= read_pointer_gray_sync(read_pointer_gray_sync'high - 1);
-- 其余低位：地址是否完全重合
lower_address_parts_are_equal := write_pointer_gray(write_pointer_gray'high - 2 downto 0)
                              = read_pointer_gray_sync(read_pointer_gray_sync'high - 2 downto 0);
full <= '1' when pointers_msbs_are_different
            and addresses_msbs_are_different
            and lower_address_parts_are_equal else '0';
```

这里的 `'high` 就是 \(A\)。三个条件逐位对应 4.1.2 里的三条推导。

> **边界提示**：这套判定要求指针至少 3 位（\(A \ge 2\)，即 `ADDRESS_WIDTH >= 2`，深度 \(\ge 4\)），否则 `'high - 2` 的范围会退化。本库默认 `FIFO_DEPTH_IN_BITS = 2`（深度 4），正好满足下界。

#### 4.1.4 代码实践

**目标**：用读源码 + 手算的方式，验证「满 = 最高两位镜像 + 低位全同」这条判定。

**操作步骤**（纯纸笔 + 读源码，无需仿真器）：

1. 取一个小例子：深度 \(D = 8\)，即地址位数 \(A = 3\)，指针 4 位（下标 3..0）。
2. 假设读指针停在二进制 `0000`（格雷码 `0000`），写指针已经写了 8 个字、绕了一圈回到地址 0，二进制 `1000`。
3. 手算写指针 `1000` 的格雷码：`b ⊕ (b≫1)` = `1000 ⊕ 0100` = `1100`。
4. 套源码三条判定（`'high = 3`）：
   - 最高位：`g_w[3]=1` vs `g_r[3]=0` → 不同 ✓
   - 次高位：`g_w[2]=1` vs `g_r[2]=0` → 不同 ✓
   - 低位（下标 1..0）：`g_w[1..0]=00` vs `g_r[1..0]=00` → 相同 ✓
   - 三条全中 → `full = 1`。
5. 对比「空」情形：读写都在 `0000`，格雷码都 `0000`，三个判定里最高位就相同 → `full = 0`；而空判定 `read_pointer_gray = write_pointer_gray_sync` 成立 → `empty = 1`。

**需要观察的现象**：同一对「指针相等」在满空两种语境下含义相反——满时低位相等但最高两位镜像，空时所有位都相等。这就是「折回位」存在的全部意义。

**预期结果**：你应能在纸上独立推出，写指针二进制 `1000`（满）与 `0000`（空）这两种「地址都是 0」的情形，如何靠最高两位区分开。

> 若手算结果与源码判定不一致，先检查格雷码换算：常见错误是把 `binary_to_gray` 写成单纯右移而忘了异或。

#### 4.1.5 小练习与答案

**练习 1**：深度 8（指针 4 位），读指针二进制 `0010`，写指针绕一圈到二进制 `1010`。这是满吗？请用格雷码三条判定验证。

**参考答案**：是满。读格雷码 `0010`→`0011`（`0010 ⊕ 0001`）；写格雷码 `1010`→`1111`（`1010 ⊕ 0101`）。最高位 `1` vs `0` 不同；次高位 `1` vs `1`——等等，这里次高位相同？让我们重算：写二进制 `1010`：\(g_3=b_3=1\)，\(g_2=b_2\oplus b_3=0\oplus1=1\)，\(g_1=b_1\oplus b_2=1\oplus0=1\)，\(g_0=b_0\oplus b_1=0\oplus1=1\) → `1111`。读二进制 `0010`：\(g_3=0\)，\(g_2=0\oplus0=0\)，\(g_1=1\oplus0=1\)，\(g_0=0\oplus1=1\) → `0011`。逐位比：最高位 `1/0` 不同 ✓；次高位 `1/0` 不同 ✓；低位 `[1..0]` `11/11` 相同 ✓ → 满。

**练习 2**：如果把满判定改成「只要最高位不同就满」（丢掉次高位和低位两个条件），会在什么情况下误判？

**参考答案**：会在「写指针只比读指针多了 \(D/2\) 步」（半圈）时误报满——此时最高位相同、次高位不同，正确判定是「不满」。反过来，丢掉低位相等条件会让「低位并不重合」的中间状态也判满，导致满标志乱跳。三条条件缺一不可，分别锁定「绕了一圈」「不是半圈」「地址重合」。

---

### 4.2 `words_stored` 填充水位计算

#### 4.2.1 概念说明

`words_stored` 是给上层（比如 SPI 控制器或监控逻辑）看的「当前 FIFO 里存了几个字」。同步 FIFO（u9-l1）有个统一的 `fifo_fill_level` 计数器，直接输出即可。异步 FIFO 没有这个统一的计数器——读写两个域各自维护自己的指针。

那 `words_stored` 怎么算？**在读时钟域里，用「同步过来的写指针 − 本域读指针」反推。** 因为存量是「写入的 − 读出的」，而读端能同时看到「本域的读指针」和「跨域同步过来的写指针」（虽然后者有延迟）。

这里有个关键选择：指针跨域传的是**格雷码**（为了不撕裂），但格雷码没法直接做减法。所以算存量时，要先把同步过来的写指针格雷码 `gray_to_binary` 转回二进制，再和同样是二进制的读指针相减。

> **为什么不直接给上层一个格雷码存量？** 因为「存量」是一个要做算术（减法、比较）的数值，算术必须在二进制下做。格雷码只在「跨域传输」这一环节有用，一旦进了本域、要参与计算就得变回二进制。

#### 4.2.2 核心流程

1. 在读时钟域，每个 `read_clk` 上升沿：
2. 把同步过来的写指针 `write_pointer_gray_sync` 用 `gray_to_binary` 转回二进制 `write_ptr_sync`。
3. 计算 `diff = write_ptr_sync − read_pointer_binary`（两者都是本域可见的二进制指针）。
4. 如果 `full` 有效，把输出钳位到子类型上界（即深度 \(D\)）；否则输出 `diff`。

数学上：

\[
\text{words\_stored} = \begin{cases} D & \text{当 } \textit{full}=1 \\ W_{\text{sync}} - R & \text{否则} \end{cases}
\]

其中 \(W_{\text{sync}}\) 是同步进读域的写指针（二进制），\(R\) 是读域本地的读指针（二进制）。

> **保守性（偏小）**：\(W_{\text{sync}}\) 滞后于真实写指针（同步链延迟）。写入刚发生时，读域要过几拍才「看得到」，所以 `words_stored` 在写入活跃期间会**偏小**（少报）。这对「读端决定要不要读」是安全的——绝不会因为多报而读空。`full` 时钳位到 \(D\)，是为了在「写端已满、读端还没同步到」的过渡窗里，把读数稳定钉在最大值，避免给上层一个忽大忽小的数。

#### 4.2.3 源码精读

`words_stored` 由一个读时钟进程计算：

[ip/memories/fifo/fifo_async.vhd:217-226](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L217-L226) —— 把 `write_pointer_gray_sync` 转回二进制，减去读指针得到 `diff`；`full` 时钳位到 `words_stored'subtype'high`，否则输出 `diff`。

抽出来看：

```vhdl
words_stored_calc: process (read_clk)
    variable write_ptr_sync: unsigned(ADDRESS_WIDTH downto 0);
    variable diff: integer;
begin
    if rising_edge(read_clk) then
        write_ptr_sync := gray_to_binary(write_pointer_gray_sync);  -- 格雷码→二进制
        diff := to_integer(write_ptr_sync) - to_integer(read_pointer_binary);
        words_stored <= words_stored'subtype'high when full else diff;
    end if;
end process;
```

几个要点：

- `write_ptr_sync` 和 `read_pointer_binary` 都是 `pointer_t`（\(A+1\) 位），减出来的 `diff` 是 `integer`，范围 \(0 \dots D\)。
- `words_stored` 的端口类型见 [entity 声明 L34](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L34)：`natural range 0 to 2**FIFO_DEPTH_IN_BITS`，即 `0 to D`，所以 `'subtype'high` 正好是 \(D\)。
- `gray_to_binary` 的实现见 [ip/memories/fifo/fifo_async.vhd:207-215](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L207-L215)，是从高到低的累积异或（必须用 `variable` 才能逐位传递）。

> **对比厂商架构**：Xilinx 架构里 `words_stored` 直接取自 xpm 宏的 `wr_data_count`（[L67](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L67)）；Intel 架构取自 dcfifo 的 `wrusedw` 并在满时钳位（[L180](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L180)）。只有自研行为级实现是「亲手算」的，这也是它最适合用来学原理的原因。

#### 4.2.4 代码实践

**目标**：用现有测试台 `test_word_count_accuracy` 用例，观察 `words_stored` 随写/读变化的节拍。

**操作步骤**：

1. 打开 [ip/memories/fifo/tb/tb_fifo_async.vhd:681-753](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_async.vhd#L681-L753)，读 `test_word_count_accuracy` 的逻辑。
2. 注意它每写一个字就 `wait_write_clock_cycles(5); wait_read_clock_cycles(5);` 再检查 `fifo_words_stored = i`——这两段等待正是为了让写指针经同步链抵达读域。
3. 在本地（需 VUnit + 厂商库，因 DUT 叶子同步器是 Xilinx 架构，见 4.3.3 的移植提示）运行该用例：
   ```
   python ip/test_runner.py
   ```
   并在 `test_suite` 循环里只保留 `run("test_word_count_accuracy")` 跑这一个用例（注释掉其余分支）以加速观察。

**需要观察的现象**：写入后若**不等**那几个同步周期就去读 `fifo_words_stored`，读到的会比实际写入数小（少报）；等够同步周期后，读数才追上。

**预期结果**：测试台现有的断言 `check_equal(fifo_words_stored, i, ...)` 全部通过，说明只要给足同步时间，`words_stored` 与实际存量精确一致。若你把等待周期从 5 改成 0，断言会失败——这就是「保守性」的可观测证据。

> 若本地无厂商仿真库，此实践可降级为「源码阅读型」：在 [L691-L706](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_async.vhd#L691-L706) 处把 `wait_write_clock_cycles(5)` 改读成 0，预测断言结果，再说明理由。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `words_stored` 在「写读同时进行」的稳态下（每拍各走一步）会保持不变？

**参考答案**：因为 `words_stored = W_sync − R`。稳态下每拍 W 和 R 各 +1，差值不变。注意测试台 `test_simultaneous_read_write`（[L599-L679](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_async.vhd#L599-L679)）正是用 `check_equal(fifo_words_stored = FIFO_DEPTH/2 or = FIFO_DEPTH/2-1, true)` 来验证存量基本恒定（允许 ±1 的同步抖动）。

**练习 2**：把 `words_stored` 钳位条件从 `when full` 改成 `when diff > D` 会更安全吗？

**参考答案**：不会更安全，反而可能更晚触发。`full` 是组合逻辑、一旦指针满足满条件立刻拉高，而 `diff` 受 `W_sync` 滞后影响、在满的瞬间可能还没到 \(D\)。用 `full` 钳位能保证「写端一旦判定满，读端计数立即钉在 \(D\)」，给上层一个稳定信号。`diff > D` 在正常情况下根本不会发生（指针几何保证了 \(W-R \le D\)），是冗余条件。

---

### 4.3 `reset_read_pointer` 与数据重放

#### 4.3.1 概念说明

普通 FIFO 一旦读走数据，数据就「没了」——读指针只往前走。但有些场景需要「把同一批数据再读一遍」。本库最典型的例子是 **SPI 多片选广播**（详见 u10-l4）：主设备要把同一帧数据发给挂在不同片选上的多片从机。与其为每片从机各备一份缓存，不如写一份到 FIFO，然后：

1. 选中第 1 片，从 FIFO 把数据读出来发出去；
2. 选中第 2 片前，把**读指针清回 0**（写指针和数据不动），于是同一批数据又能从头读；
3. 选中第 2 片，再读一遍发给它；
4. 依此类推。

这就是 `reset_read_pointer` 的用途——**数据重放（replay）**。它的语义是「只回退读指针、保留写指针与存储内容」，与 `aclr`（全清，读写指针都归零、数据作废）完全不同。

> **现实用例**：SPI 顶层 `spi_interface` 把 `fifo_async` 当作 TX FIFO，在状态机里对每片从机触发一次 `reset_read_pointer`。见 [ip/communication/spi/spi_interface.vhd:169](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_interface.vhd#L169)（拉高重放信号）与 [L248](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_interface.vhd#L248)（连到 FIFO 的 `reset_read_pointer` 端口）。

#### 4.3.2 核心流程

`reset_read_pointer` 在**读时钟域**生效（它操作的是读指针）：

1. 读指针推进进程在 `read_clk` 上升沿检查优先级：`aclr`（异步全清）> `reset_read_pointer`（同步重放）> 正常读。
2. 当 `reset_read_pointer = 1`：
   - `read_pointer_binary` ← 0
   - `read_pointer_gray` ← 0
   - **不触碰** `write_pointer_*`，也**不触碰** RAM 内容。
3. 下一拍起，读地址从 0 重新开始，RAM 里原本的数据被原样再读一遍。
4. 读指针归零后，经同步链传到写域，`full` 会（在几拍后）因「读端似乎退回了」而重新评估，腾出空间；读域的 `words_stored` 也会跳升（\(W_{\text{sync}} − 0\) 变大）。

```
           reset_read_pointer=1 (持续 ≥1 个 read_clk)
                    │
                    ▼
   ┌─────────────────────────────────┐
   │ read_pointer_binary ← 0         │   ← 只动读指针
   │ read_pointer_gray   ← 0         │
   │ write_pointer_*     (不变)      │   ← 写指针保留
   │ RAM 内容             (不变)      │   ← 数据保留
   └─────────────────────────────────┘
                    │
                    ▼  下一拍起从地址 0 重读 → 同一批数据再出一次
```

> **与 `aclr` 的区别**：`aclr` 是异步复位，把读写指针**都**清零、`words_stored` 归零、`read_data_valid` 清零，相当于「清空 FIFO」。`reset_read_pointer` 只回退读指针，是有目的的「倒带」。测试台里 `test_reset_behavior`（[L450-L513](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_async.vhd#L450-L513)）验证的是 `aclr` 行为，不是重放。

#### 4.3.3 源码精读

`reset_read_pointer` 的端口声明带默认值 `'0'`，所以不连也能工作（保持不重放）：

[ip/memories/fifo/fifo_async.vhd:36-37](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L36-L37) —— 端口注释写明「Reset read pointer for data replay (keeps write pointer intact)」。

真正的重放逻辑在 `own_behavioural_async_fifo` 的读指针进程里，`reset_read_pointer` 优先于正常读推进：

[ip/memories/fifo/fifo_async.vhd:241-256](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L241-L256) —— `aclr` 异步清零在最高优先级；其次 `reset_read_pointer` 把读指针（二进制与格雷码）同步清零；再次才是 `read_enable and not empty` 的正常推进。

关键代码段：

```vhdl
elsif rising_edge(read_clk) then
    if reset_read_pointer then
        -- Reset read pointer to replay data (write pointer stays intact)
        read_pointer_binary <= (others => '0');
        read_pointer_gray   <= (others => '0');
    elsif read_enable and not empty then
        read_pointer_binary <= read_pointer_binary + 1;
        read_pointer_gray   <= binary_to_gray(read_pointer_binary + 1);
    end if;
```

写指针进程 [L228-L239](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L228-L239) 完全不含 `reset_read_pointer`——这是「写指针保留」的源码证据。

⚠️ **重要：三套架构的重放行为并不一致**（这是真实源码事实，移植时务必留意）：

- **`own_behavioural_async_fifo`**（本讲所讲）：真正把读指针清零，**完整实现重放语义**。
- **`xilinx_behavioural_async_fifo`**：`reset_read_pointer` 只是用来**屏蔽读请求**——[L103](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L103) `rd_en => read_enable and not reset_read_pointer`。它**不会**把 xpm 宏内部的读指针清零（xpm 的指针归零只能靠 `rst => aclr`，那是全清）。所以 Xilinx 版并不真正支持无损重放。
- **`intel_behavioural_async_fifo`**：[L123-L181](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L123-L181) 里 `rdreq => read_enable`，**完全没引用** `reset_read_pointer`，重放信号被静默忽略。

换句话说，「读指针重放」目前只在自研行为级实现里是真实有效的；厂商宏版要么退化成读屏蔽、要么直接忽略。这也是为什么 SPI 顶层（u10-l4）依赖该特性时，需要确认所用架构是否为 `own_behavioural_async_fifo`。

> **移植提示（承接 u9-l3）**：`own_behavioural_async_fifo` 的两条叶子同步链被硬编码为 `xilinx_behavioural_ff_synchroniser_vector`（[L258](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L258)、[L271](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L271)），仿真仍需 xpm 库。

#### 4.3.4 代码实践

**目标**：现有测试台 **没有** 连接 `reset_read_pointer`（见 [DUT 例化 L791-L808](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_async.vhd#L791-L808)，端口映射里没有这一项，故取默认 `'0'`），也没有任何重放用例。本实践要你**亲手给测试台补上重放测试**，把本讲三个知识点串起来。

**操作步骤**（需要修改测试台，属「读者需添加的示例代码」，不改动设计源码）：

1. 在测试台 architecture 的信号声明区，加一个驱动信号：
   ```vhdl
   signal fifo_reset_read_pointer: std_ulogic := '0';
   ```
2. 在 DUT 例化的 `port map` 里补一行：
   ```vhdl
   reset_read_pointer => fifo_reset_read_pointer
   ```
3. 在 `checker` 进程里新增一个过程（示例代码，仿照 `test_basic_operations` 风格）：
   ```vhdl
   procedure test_reset_read_pointer_replay is
       variable expected_data : std_ulogic_vector(DATA_WIDTH-1 downto 0);
   begin
       info("Test R) reset_read_pointer replay");
       read_clk_select <= normal;
       reset_fifo;

       -- 写 4 个字：100,101,102,103
       for i in 0 to 3 loop
           wait_write_clock_cycles(1);
           fifo_write_enable <= '1';
           fifo_write_data <= std_ulogic_vector(to_unsigned(i + 100, DATA_WIDTH));
           wait_write_clock_cycles(1);
           fifo_write_enable <= '0';
           wait_write_clock_cycles(2);
       end loop;
       wait_write_clock_cycles(5);
       wait_read_clock_cycles(5);
       check_equal(fifo_empty, '0', msg => "should have data before replay");

       -- 第一次读：应得 100,101,102,103
       for i in 0 to 3 loop
           wait_read_clock_cycles(1);
           fifo_read_enable <= '1';
           wait until fifo_read_data_valid;
           fifo_read_enable <= '0';
           expected_data := std_ulogic_vector(to_unsigned(i + 100, DATA_WIDTH));
           check_equal(fifo_read_data, expected_data, msg => "first pass");
           wait_read_clock_cycles(2);
       end loop;

       -- 触发重放：把读指针清零，写指针与数据不动
       wait_read_clock_cycles(2);
       fifo_reset_read_pointer <= '1';
       wait_read_clock_cycles(2);
       fifo_reset_read_pointer <= '0';
       wait_read_clock_cycles(2);

       -- 第二次读：应再次得到 100,101,102,103（重放成功）
       for i in 0 to 3 loop
           wait_read_clock_cycles(1);
           fifo_read_enable <= '1';
           wait until fifo_read_data_valid;
           fifo_read_enable <= '0';
           expected_data := std_ulogic_vector(to_unsigned(i + 100, DATA_WIDTH));
           check_equal(fifo_read_data, expected_data, msg => "replay pass");
           wait_read_clock_cycles(2);
       end loop;

       info("Test R) replay completed successfully." & LF);
   end procedure;
   ```
4. 在 `test_suite` 循环里登记它（仿照 [L759-L781](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_async.vhd#L759-L781) 的写法）：
   ```vhdl
   elsif run("test_reset_read_pointer_replay") then
       test_reset_read_pointer_replay;
   ```
5. 运行：`python ip/test_runner.py`（需要 VUnit + 厂商库环境；见 u1-l3、u1-l4）。

**需要观察的现象**：重放后第二次读出的数据序列与第一次**完全相同**；`words_stored` 在触发 `reset_read_pointer` 后会**跳升**（因为读指针归零、\(W_{\text{sync}} - 0\) 变大），随后随第二次读取逐步下降。

**预期结果**：所有 `check_equal` 通过，证明同一批数据被无损重放。若把 DUT 换成 `intel_behavioural_async_fifo`，第二次读会**读不到**预期数据（重放被忽略，读指针继续往前走到空）——这就验证了 4.3.3 所说的「架构不一致」。

> **待本地验证**：上述过程体依赖 `reset_fifo`、`wait_*_clock_cycles`、`test_data_queue` 等测试台既有设施，命名以磁盘真实文件为准；本环境未运行仿真器，未实测通过，请在本地或 EDA Playground 验证。

#### 4.3.5 小练习与答案

**练习 1**：`reset_read_pointer` 之后，`empty` 会不会立刻变成 `'0'`？为什么？

**参考答案**：会变 `'0'`（前提是 FIFO 里确实有数据）。`empty` 由 `read_pointer_gray = write_pointer_gray_sync` 决定（[L284](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L284)）。重放把 `read_pointer_gray` 清零，只要 `write_pointer_gray_sync` 非零（即写过数据），两者就不等 → `empty` 立刻为 `'0'`。这是读域内部信号，不需要等跨域同步。

**练习 2**：如果在 `reset_read_pointer` 拉高的同一拍同时 `read_enable = '1'`，会发生什么？

**参考答案**：读指针归零、不会推进。因为 `reset_read_pointer` 在 `if-elsif` 里优先于 `read_enable` 分支（[L247-L254](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L247-L254)）。这是「重放优先于普通读」的刻意设计：重放期间读地址被钉在 0，下一拍 `reset_read_pointer` 释放后才开始从 0 正常读。

**练习 3**：为什么 `reset_read_pointer` 只在读域生效，而不需要在写域也做点什么？

**参考答案**：因为重放只回退读指针，写指针和数据都不变，写域无需知情。读指针归零这个事实，会经读→写同步链自然传到写域（`read_pointer_gray_sync` 变小），写域据此重新评估 `full`（腾出空间）。整个过程只需读域主动发起、写域被动跟随，是单向的。

---

## 5. 综合实践

把本讲三个模块串成一个端到端的小任务：**在 `tb_fifo_async` 里构造一个完整的「填满 → 验证满 → 读空 → 验证空 → 重放」场景，并记录 `words_stored` 在每个阶段的取值。**

建议步骤：

1. 复用现有 `test_full_flag`（[L224-L278](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_async.vhd#L224-L278)）与 `test_empty_flag`（[L280-L322](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_async.vhd#L280-L322)）的写法，新写一个过程 `test_flags_and_replay_e2e`：
   - 写满整个 FIFO（循环到 `fifo_full = '1'`），记录此刻 `fifo_words_stored` 应等于深度；
   - 尝试在满时再写一个字，验证它被忽略（`words_stored` 不增）；
   - 逐字读空到 `fifo_empty = '1'`，记录 `words_stored` 沿途递减到 0；
   - 重新写入若干字，触发 `reset_read_pointer`，再次读出验证重放。
2. 在波形（用配套的 `tb/tb_fifo_async.do` 脚本，见 [u11-l3](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_async.do)）里重点观察：
   - `full` 拉高与 `write_pointer_gray` / `read_pointer_gray_sync` 最高两位镜像、低位重合的对应关系；
   - `empty` 拉高与两个格雷码指针完全相等的对应关系；
   - `words_stored` 在 `reset_read_pointer` 触发瞬间的「跳升」。
3. 画一张时序图，把 `write_pointer_binary`、`read_pointer_binary`、`full`、`empty`、`words_stored`、`reset_read_pointer` 对齐画出，标注满、空、重放三个时刻。

**交付物**：一段可运行的过程代码 + 一张标注清楚的时序草图 + 一份「阶段 → words_stored 预期值」对照表。

> **待本地验证**：本实践涉及仿真运行与波形采集，请在具备 VUnit + 厂商库的环境中完成；若环境受限，可只交付「源码阅读型」结论——即在不运行的前提下，根据本讲的算法预测每个阶段的 `words_stored` 值，并标注依据的源码行。

## 6. 本讲小结

- **空 = 两个格雷码指针完全相等**（[L284](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L284)）；**满 = 最高两位都镜像、低位全同**（[L286-L295](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L286-L295)）。满判定可由「写指针领先 \(D\) 步 → 二进制仅最高位不同」经格雷码换算严格推出。
- **`empty` 属于读域、`full` 属于写域**，各自用「本域指针 + 同步过来的对端指针」计算，「谁用就在谁的域算」是 CDC 安全的铁律。
- **两者都因同步链滞后而保守**：`full` 可能提前拉高（少写不溢出）、`empty` 可能提前拉高（少读不读空），都不影响正确性。
- **`words_stored` 在读域反推**：把同步过来的写指针格雷码 `gray_to_binary` 转回二进制，减去本域读指针；`full` 时钳位到深度 \(D\)（[L217-L226](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L217-L226)）。它是保守偏小的近似值。
- **`reset_read_pointer` 实现数据重放**：读时钟域里把读指针清零、写指针与 RAM 内容不动（[L241-L256](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L241-L256)），优先级高于普通读、低于 `aclr`。
- **三套架构重放行为不一致**：仅 `own_behavioural_async_fifo` 真正无损重放；Xilinx 版只屏蔽 `rd_en`、Intel 版完全忽略——移植与依赖该特性的上层（如 SPI 多片选）须注意。

## 7. 下一步学习建议

- **进入 u10（SPI 通信接口）**：本讲的 `reset_read_pointer` 在 **u10-l4（SPI 顶层接口）** 才有真实用武之地——`spi_interface` 把 `fifo_async` 当 TX FIFO，靠多片选状态机 + 重放实现「一份数据广播给多片从机」。读完 u10-l4 你会真正理解本讲 4.3 的工程动机。
- **复习 u11（验证方法学）**：本讲多次引用测试台的 `check_equal`、`test_suite`/`run()`、`watchdog` 等，这些在 **u11-l1（VUnit 测试台结构）** 系统讲解；本讲的「补一个重放用例」就是 u11-l1 综合实践的好素材。
- **延伸阅读**：Clifford E. Cummings, *Simulation and Synthesis Techniques for Asynchronous FIFO Design*（源码头部 [L4](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L4) 注明的设计蓝本），对照阅读满空判定的原始推导，能加深对本讲 4.1 的理解。
- **动手挑战**：尝试给 `own_behavioural_async_fifo` 的两条叶子同步链去掉硬编码（改用 generic 选择 Xilinx/Intel 架构，见 u9-l3 移植提示），并用本讲补好的重放用例做回归，验证改动不破坏满空与重放语义。
